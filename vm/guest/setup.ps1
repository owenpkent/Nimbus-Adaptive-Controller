<#
.SYNOPSIS
    Runs INSIDE the guest. Phase 1 at the first logon (from the answer
    file's FirstLogonCommands): make the session persistent, open the
    firewall, fetch a Python for the tooling, install ViGEmBus and
    Sunshine, register the Gate C monitor to run at every logon. Phase 2
    (-Phase2, invoked by the host's 60-guest-stream.ps1 after the GPU
    partition is attached): the virtual display driver and the render
    check.

.DESCRIPTION
    Every step is best effort and recorded in C:\nimbus\setup-done.json
    (phase 1) or C:\nimbus\phase2-done.json, so the host can read what
    worked. The transcript is in C:\nimbus\logs. Inputs come from
    C:\nimbus\guest.json, written by 40-new-guest.ps1.

    Phase 1 steps
      autologon   the guest logs its administrator on at every boot, so the
                  monitor and Sunshine have an interactive desktop
      power       never sleep, never blank
      firewall    the monitor's port, Sunshine's ports, ping
      python      the embeddable CPython (no installer, no PATH change)
      vigembus    ViGEmBus 1.22.0, the pad driver Sunshine falls back to
                  (its 2026 releases prefer a separately licensed driver)
      sunshine    the MSI, silent; then controller on, keyboard and mouse
                  OFF, pad type x360, web credentials set
      monitor     scheduled task NimbusGateCMonitor at logon, started now
      render      render_check.py before the GPU is attached (expected
                  FAIL on the basic display: a baseline)

    Phase 2 steps
      vdd         Virtual Display Driver (VirtualDrivers) through winget,
                  so Sunshine has a display on the partitioned adapter to
                  capture. Installed after the GPU driver on purpose: the
                  community record says Hyper-V's own display driver wins
                  when the VDD arrives first.
      render      render_check.py again: Gate B, part 2
      sunshine    service restart so it re-enumerates displays and pads

    Versions are pinned below; edit them together with vm\README.md.
#>
[CmdletBinding()]
param([switch]$Phase2)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$root = 'C:\nimbus'
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
Start-Transcript -Path (Join-Path $logs ('setup-{0}-{1:yyyyMMdd-HHmmss}.log' -f $(if ($Phase2) { 'phase2' } else { 'phase1' }), (Get-Date))) | Out-Null

$PythonUrl = 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip'
$ViGEmUrl = 'https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/ViGEmBus_1.22.0_x64_x86_arm64.exe'
$SunshineVersion = '2026.906.222525'
$SunshineUrl = "https://github.com/LizardByte/Sunshine/releases/download/v$SunshineVersion/Sunshine-Windows-AMD64-installer.msi"
$SunshineDir = 'C:\Program Files\Sunshine'
$curl = "$env:SystemRoot\System32\curl.exe"

$cfg = Get-Content (Join-Path $root 'guest.json') -Raw | ConvertFrom-Json
$user = $cfg.user
$password = $cfg.password
$port = [int]$cfg.monitor_port
$results = [ordered]@{ started = (Get-Date).ToString('o'); phase = $(if ($Phase2) { 2 } else { 1 }) }

$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$results.elevated = $elevated
if (-not $elevated) { Write-Warning 'not elevated: installs and firewall rules will fail; run from an elevated prompt' }

function Step {
    <#
    A step is ok only if its body neither threw nor raised a cmdlet error.
    With $ErrorActionPreference at Continue a failed cmdlet does not throw,
    so the errors the body added to $Error are counted too. Two kinds are
    not failures: a native tool's stderr captured with 2>&1 (Windows
    PowerShell 5.1 wraps each line as NativeCommandError; the exit code is
    what a step checks), and a lookup allowed to find nothing, which uses
    -ErrorAction Ignore and so never reaches $Error. Stop is not an option
    here, because under 5.1 it turns that captured stderr into a throw.
    #>
    param([string]$Name, [scriptblock]$Body)
    Write-Host "==> $Name"
    $before = $Error.Count
    try {
        $out = & $Body
        $new = @(if ($Error.Count -gt $before) { $Error[0..($Error.Count - $before - 1)] }) |
            Where-Object { -not ($_ -is [Management.Automation.ErrorRecord] -and "$($_.FullyQualifiedErrorId)" -like 'NativeCommandError*') }
        if ($new) {
            $first = @($new)[-1]    # $Error is newest first
            $results[$Name] = "failed: $first"
            Write-Warning "$Name failed: $first"
        } else {
            $results[$Name] = if ($out) { "ok: $out" } else { 'ok' }
        }
    } catch {
        $results[$Name] = "failed: $($_.Exception.Message)"
        Write-Warning "$Name failed: $($_.Exception.Message)"
    }
}

function Fetch {
    param([string]$Url, [string]$Dest)
    if (Test-Path $Dest) { return $Dest }
    & $curl -L -sS --retry 5 --retry-delay 5 -o $Dest $Url
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Dest)) { throw "download failed ($LASTEXITCODE): $Url" }
    return $Dest
}

if (-not $Phase2) {
    Step 'autologon' {
        $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        Set-ItemProperty -Path $wl -Name AutoAdminLogon -Value '1' -Type String
        Set-ItemProperty -Path $wl -Name DefaultUserName -Value $user -Type String
        Set-ItemProperty -Path $wl -Name DefaultPassword -Value $password -Type String
        Set-ItemProperty -Path $wl -Name DefaultDomainName -Value $env:COMPUTERNAME -Type String
        Remove-ItemProperty -Path $wl -Name AutoLogonCount -ErrorAction Ignore   # usually absent
    }
    Step 'power' {
        & powercfg.exe /change monitor-timeout-ac 0 | Out-Null
        & powercfg.exe /change standby-timeout-ac 0 | Out-Null
        & powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
    }
    Step 'firewall' {
        $rules = @(
            @{ Name = 'Nimbus Gate C monitor'; Protocol = 'TCP'; Port = "$port" },
            @{ Name = 'Sunshine TCP'; Protocol = 'TCP'; Port = '47984,47989,47990,48010' },
            @{ Name = 'Sunshine UDP'; Protocol = 'UDP'; Port = '47998-48000,48002,48010' }
        )
        foreach ($r in $rules) {
            if (-not (Get-NetFirewallRule -DisplayName $r.Name -ErrorAction Ignore)) {
                New-NetFirewallRule -DisplayName $r.Name -Direction Inbound -Action Allow -Protocol $r.Protocol -LocalPort ($r.Port -split ',') -Profile Any | Out-Null
            }
        }
        if (-not (Get-NetFirewallRule -DisplayName 'Nimbus ping' -ErrorAction Ignore)) {
            New-NetFirewallRule -DisplayName 'Nimbus ping' -Direction Inbound -Action Allow -Protocol ICMPv4 -IcmpType 8 -Profile Any | Out-Null
        }
    }
    Step 'python' {
        $dir = Join-Path $root 'python'
        if (-not (Test-Path (Join-Path $dir 'python.exe'))) {
            $zip = Fetch $PythonUrl (Join-Path $root 'python-embed.zip')
            Expand-Archive -Path $zip -DestinationPath $dir -Force
        }
        & (Join-Path $dir 'python.exe') --version
    }
    Step 'vigembus' {
        if (Get-Service ViGEmBus -ErrorAction Ignore) { return 'already installed' }
        $exe = Fetch $ViGEmUrl (Join-Path $root 'ViGEmBus_1.22.0.exe')
        $p = Start-Process -FilePath $exe -ArgumentList '/exenoui', '/qn', '/norestart' -Wait -PassThru
        if ($p.ExitCode -notin 0, 3010) { throw "installer exited $($p.ExitCode)" }
        "exit $($p.ExitCode)"
    }
    Step 'sunshine' {
        if (-not (Test-Path (Join-Path $SunshineDir 'sunshine.exe'))) {
            $msi = Fetch $SunshineUrl (Join-Path $root "Sunshine-$SunshineVersion.msi")
            $p = Start-Process -FilePath msiexec.exe -ArgumentList '/i', "`"$msi`"", '/qn', '/norestart' -Wait -PassThru
            if ($p.ExitCode -notin 0, 3010) { throw "msiexec exited $($p.ExitCode)" }
        }
        $confDir = Join-Path $SunshineDir 'config'
        New-Item -ItemType Directory -Path $confDir -Force | Out-Null
        $conf = Join-Path $confDir 'sunshine.conf'
        $lines = if (Test-Path $conf) { @(Get-Content $conf) } else { @() }
        # Ordered, and written without a byte order mark: Set-Content -Encoding
        # UTF8 on Windows PowerShell 5.1 puts a BOM in front of the first line,
        # which would glue it to whichever key came first (an unordered
        # hashtable can put mouse or keyboard there) and hide that setting.
        $want = [ordered]@{ controller = 'enabled'; keyboard = 'disabled'; mouse = 'disabled'; gamepad = 'x360'; origin_web_ui_allowed = 'lan' }
        $lines = @($lines | ForEach-Object { "$_".TrimStart([char]0xFEFF) })
        foreach ($k in $want.Keys) {
            $lines = @($lines | Where-Object { $_ -notmatch "^\s*$k\s*=" })
            $lines += "$k = $($want[$k])"
        }
        [IO.File]::WriteAllLines($conf, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
        & (Join-Path $SunshineDir 'sunshine.exe') --creds $user $password 2>&1 | Out-Null
        $svc = Get-Service SunshineService -ErrorAction Ignore
        if ($svc) { Restart-Service SunshineService }   # a failed restart leaves the old config live: a failure
        "config written; keyboard and mouse forwarding disabled"
    }
    Step 'monitor' {
        $py = Join-Path $root 'python\python.exe'
        $cmd = "`"$py`" `"$root\gate_c_monitor.py`" --port $port"
        & schtasks.exe /Create /F /TN NimbusGateCMonitor /SC ONLOGON /RU $user /TR $cmd | Out-Null
        & schtasks.exe /Run /TN NimbusGateCMonitor | Out-Null
        Start-Sleep -Seconds 3
        $ok = $false
        try { $ok = ((Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/health" -TimeoutSec 5).Content -eq 'ok') } catch { }
        if (-not $ok) { throw 'the monitor did not answer on localhost' }
        "listening on $port"
    }
    Step 'render' {
        $py = Join-Path $root 'python\python.exe'
        (& $py (Join-Path $root 'render_check.py') --json (Join-Path $logs 'gate-b-before-gpu.json') 2>&1 | Out-String).Trim()
    }
    $results.finished = (Get-Date).ToString('o')
    $results | ConvertTo-Json | Set-Content -Path (Join-Path $root 'setup-done.json') -Encoding UTF8
} else {
    Step 'vdd' {
        # winget's package is the portable VDD Control app: an exe, devcon,
        # the signed driver and a settings file, extracted and nothing
        # installed. The control app installs the driver from a GUI, so do
        # its work here: settings file where the driver reads it, the
        # signer into TrustedPublisher (a fresh guest has never seen
        # SignPath Foundation, and a silent install fails on that with
        # 0xE0000242), the package into the driver store, then a
        # root-enumerated device node with the bundled devcon.
        $winget = Get-Command winget.exe -ErrorAction Ignore
        if (-not $winget) { throw 'winget is not available in this session; install VirtualDrivers.Virtual-Display-Driver by hand (github.com/VirtualDrivers/Virtual-Display-Driver)' }
        $pkg = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Directory -ErrorAction Ignore |
            Where-Object { $_.Name -like 'VirtualDrivers.Virtual-Display-Driver*' } | Select-Object -First 1
        if (-not $pkg) {
            $out = & winget.exe install --id VirtualDrivers.Virtual-Display-Driver -e --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-String
            if ($LASTEXITCODE -ne 0) { throw "winget exited $LASTEXITCODE`: $($out.Trim())" }
            $pkg = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Directory |
                Where-Object { $_.Name -like 'VirtualDrivers.Virtual-Display-Driver*' } | Select-Object -First 1
            if (-not $pkg) { throw 'winget reported success but the package folder is missing' }
        }
        $drv = Join-Path $pkg.FullName 'SignedDrivers\x86\VDD'      # the folder name says x86; the INF is NTamd64
        $inf = Join-Path $drv 'MttVDD.inf'
        if (-not (Test-Path $inf)) { throw "no MttVDD.inf under $drv" }
        if (Get-PnpDevice -Class Display -ErrorAction Ignore | Where-Object { $_.FriendlyName -eq 'Virtual Display Driver' -and $_.Status -eq 'OK' }) { return 'already installed' }
        New-Item -ItemType Directory -Path 'C:\VirtualDisplayDriver' -Force | Out-Null
        Copy-Item (Join-Path $pkg.FullName 'Dependencies\vdd_settings.xml') 'C:\VirtualDisplayDriver\vdd_settings.xml' -Force
        $sig = Get-AuthenticodeSignature (Join-Path $drv 'mttvdd.cat')
        if ($sig.Status -ne 'Valid') { throw "driver catalog signature is $($sig.Status)" }
        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store('TrustedPublisher', 'LocalMachine')
        $store.Open('ReadWrite'); $store.Add($sig.SignerCertificate); $store.Close()
        $out = & pnputil.exe /add-driver $inf /install 2>&1 | Out-String
        if ($LASTEXITCODE -notin 0, 259) { throw "pnputil exited $LASTEXITCODE`: $($out.Trim())" }
        $hwid = (Select-String -Path $inf -Pattern '(Root\\[A-Za-z0-9_]+)' | Select-Object -First 1).Matches[0].Groups[1].Value
        $node = Get-PnpDevice -ErrorAction Ignore | Where-Object { $_.HardwareID -contains $hwid }
        if (-not $node) {
            & (Join-Path $pkg.FullName 'Dependencies\devcon.exe') install $inf $hwid 2>&1 | Out-Null
        }
        Start-Sleep -Seconds 8
        $mon = Get-PnpDevice -Class Monitor -ErrorAction Ignore | Where-Object { $_.FriendlyName -match 'VDD' -and $_.Status -eq 'OK' }
        if (-not $mon) { throw 'the driver installed but no VDD monitor appeared' }
        "installed; signer $($sig.SignerCertificate.Subject); monitor $($mon.FriendlyName)"
    }
    Step 'render' {
        $py = Join-Path $root 'python\python.exe'
        (& $py (Join-Path $root 'render_check.py') --json (Join-Path $logs 'gate-b.json') 2>&1 | Out-String).Trim()
    }
    Step 'sunshine' {
        Restart-Service SunshineService -ErrorAction Stop
        'restarted'
    }
    $results.finished = (Get-Date).ToString('o')
    $results | ConvertTo-Json | Set-Content -Path (Join-Path $root 'phase2-done.json') -Encoding UTF8
}

$results | Format-List | Out-String | Write-Host
Stop-Transcript | Out-Null
