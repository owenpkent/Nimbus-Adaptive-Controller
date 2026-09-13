<#
.SYNOPSIS
    The pad's road into the guest and the picture's road out: finish the
    guest (virtual display, render check), install Moonlight on the host,
    pair it with the guest's Sunshine, and print how to start Gate C.
    Needs the Hyper-V Administrators group or elevation; no reboot.

.DESCRIPTION
    Transport, per the feasibility document section 5: reuse the existing
    chain first. Nimbus makes its pad on the host, Moonlight reads it and
    forwards it, Sunshine in the guest recreates it on ViGEmBus. Sunshine
    was configured by guest\setup.ps1 with keyboard and mouse forwarding
    OFF, so the only input Moonlight can put into the guest is the pad.

    Steps
      1. Guest phase 2 over PowerShell Direct (setup.ps1 -Phase2): the
         virtual display driver and the render check. Gate B, part 2.
      2. Moonlight on the host through winget, if missing.
      3. Pairing without a hand on either side: Moonlight's command line
         pairs with a PIN, and the PIN is posted to Sunshine's API inside
         the guest with the credentials setup.ps1 set.
      4. Moonlight's "process gamepad input when in the background"
         setting, which the Gate C form factor needs (Nimbus holds the
         host focus while the game runs). Written to Moonlight's settings
         file and printed as a checkbox to verify, since the key is a
         private detail of the client.

    Prints the guest IP, the monitor URL and the two commands that follow:
    the stream, then vm\gate_c_host.py.

.PARAMETER Pin
    The pairing PIN. Any four digits.

.PARAMETER SkipPhase2
    Do not run the in-guest phase 2 again.
#>
[CmdletBinding()]
param(
    [string]$VmRoot,
    [string]$Pin = '4321',
    [switch]$SkipPhase2,
    [switch]$SkipPair
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Import-Module Hyper-V -ErrorAction Stop
$VmRoot = Get-VmRoot $VmRoot
$cred = Get-GuestCredential -VmRoot $VmRoot
$vm = Get-VM -Name $script:VmName -ErrorAction Stop
if ($vm.State -ne 'Running') { Start-VM -Name $script:VmName }

Write-Step 'Guest address'
$ip = $null
for ($i = 0; $i -lt 40 -and -not $ip; $i++) {
    $ip = (Get-VMNetworkAdapter -VMName $script:VmName).IPAddresses | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1
    if (-not $ip) { Start-Sleep -Seconds 3 }
}
if (-not $ip) { throw 'the guest reported no IPv4 address (integration services not up yet?)' }
Write-Host "  $ip"

if (-not $SkipPhase2) {
    Write-Step 'Guest phase 2 (virtual display driver, render check, Sunshine restart)'
    if (-not (Wait-GuestSession -Credential $cred -TimeoutSec 600)) { throw 'PowerShell Direct did not answer' }
    $out = Invoke-Command -VMName $script:VmName -Credential $cred -ScriptBlock {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\nimbus\setup.ps1' -Phase2 2>&1 | Out-String
        if (Test-Path 'C:\nimbus\phase2-done.json') { Get-Content 'C:\nimbus\phase2-done.json' -Raw }
    }
    Write-Host ($out | Out-String)
    Write-VmLog $VmRoot 'guest phase 2 ran'
}

Write-Step 'Moonlight on the host'
$moonlight = @('C:\Program Files\Moonlight Game Streaming\Moonlight.exe',
               "$env:LOCALAPPDATA\Programs\Moonlight Game Streaming\Moonlight.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $moonlight) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw 'Moonlight is not installed and winget is unavailable; install MoonlightGameStreamingProject.Moonlight by hand' }
    & winget.exe install --id MoonlightGameStreamingProject.Moonlight -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $moonlight = @('C:\Program Files\Moonlight Game Streaming\Moonlight.exe',
                   "$env:LOCALAPPDATA\Programs\Moonlight Game Streaming\Moonlight.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $moonlight) { throw 'Moonlight installed but its executable was not found' }
    Write-VmLog $VmRoot 'Moonlight installed on the host'
}
Write-Host "  $moonlight"

# Background gamepad: the setting Gate C needs. Moonlight (Qt) keeps its
# settings in an ini; the key name is the client's own. Verified by eye in
# Settings > Input, which the last lines below ask for.
$ini = Join-Path $env:APPDATA 'Moonlight Game Streaming Project\Moonlight.ini'
if (Test-Path $ini) {
    $text = Get-Content $ini -Raw
    if ($text -notmatch '(?m)^backgroundgamepad=') {
        $text = $text -replace '(?m)^\[General\]\s*$', "[General]`r`nbackgroundgamepad=true"
        Set-Content -Path $ini -Value $text -Encoding UTF8
        Write-Host '  wrote backgroundgamepad=true to Moonlight.ini'
    }
} else {
    Write-Host '  Moonlight.ini not there yet (first run creates it); set the checkbox by hand, see below'
}

if (-not $SkipPair) {
    Write-Step "Pairing Moonlight with Sunshine at $ip (PIN $Pin)"
    $pairProc = Start-Process -FilePath $moonlight -ArgumentList 'pair', $ip, '--pin', $Pin -PassThru
    Start-Sleep -Seconds 4
    $guestUser = $script:GuestAdmin
    $guestPw = (Get-Content (Join-Path $VmRoot 'guest-password.txt') -Raw).Trim()
    # Sunshine's 2026 API keeps a list of pending pairing requests
    # (GET /api/pin) and the PIN must be posted with that request's
    # pairing_id. The JSON goes through a file: PowerShell 5.1 re-quotes a
    # literal {"pin":...} argument on its way into curl and Sunshine sees '{p'.
    $posted = Invoke-Command -VMName $script:VmName -Credential $cred -ScriptBlock {
        param($u, $p, $pin, $name)
        $curl = "$env:SystemRoot\System32\curl.exe"
        $pending = (& $curl -k -sS -u "${u}:${p}" 'https://localhost:47990/api/pin' 2>&1 | Out-String | ConvertFrom-Json).pairings
        $id = ($pending | Select-Object -Last 1).id
        if (-not $id) { return 'no pending pairing request in Sunshine (did Moonlight reach it?)' }
        [IO.File]::WriteAllText('C:\nimbus\pin.json', "{`"pin`":`"$pin`",`"name`":`"$name`",`"pairing_id`":`"$id`"}")
        & $curl -k -sS -u "${u}:${p}" -X POST 'https://localhost:47990/api/pin' -H 'Content-Type: application/json' -d '@C:\nimbus\pin.json' 2>&1 | Out-String
    } -ArgumentList $guestUser, $guestPw, $Pin, $env:COMPUTERNAME
    Write-Host "  Sunshine: $($posted.Trim())"
    if (-not $pairProc.WaitForExit(60000)) { Write-Warning 'Moonlight pair is still running after 60 s; check its window' }
    else { Write-Host "  Moonlight pair exited $($pairProc.ExitCode)" }
    Write-VmLog $VmRoot "pairing attempted with $ip (Sunshine said: $($posted.Trim()))"
}

Write-Host ''
Write-Host ("Guest monitor:  http://{0}:{1}/snapshot" -f $ip, $script:MonitorPort)
Write-Host ''
Write-Host 'Check in Moonlight > Settings > Input: "Process gamepad input when Moonlight is in the background" is ON,'
Write-Host 'and that no mouse or keyboard capture option is needed (Sunshine ignores both anyway).'
Write-Host ''
Write-Host 'Stream the guest desktop (leave it running, then run Gate C from another prompt). Windowed so Nimbus'
Write-Host 'can sit beside it, and background gamepad input so the pad keeps flowing while Nimbus has the focus:'
Write-Host ("  & '{0}' stream {1} Desktop --display-mode windowed --resolution 1280x720 --fps 60 --background-gamepad --no-quit-after" -f $moonlight, $ip)
Write-Host 'Gate C (the pad direct, then the real app), with the viewer focused for the host input sweep:'
Write-Host ("  venv\Scripts\python.exe vm\gate_c_host.py --guest {0} --actuator pad --viewer-title Moonlight --json {1}\logs\gate-c-pad.json" -f $ip, $VmRoot)
Write-Host ("  venv\Scripts\python.exe vm\gate_c_host.py --guest {0} --actuator nimbus --viewer-title Moonlight --json {1}\logs\gate-c-nimbus.json" -f $ip, $VmRoot)
Write-Host 'Moonlight forwards every host gamepad, so the vJoy device shows up in the guest as player 0 as soon as the'
Write-Host 'stream starts; gate_c_host.py looks for the slot that appears after it plugs its own pad.'
