<#
.SYNOPSIS
    Put the netpad receiver in NimbusGuest and start it (research tooling).

.DESCRIPTION
    For the end-to-end check of docs/vision/SEPARATE_GAME_MACHINE.md section
    3.2: the guest is the "game machine", reached over the Hyper-V switch, and
    the Gate C monitor already running there reads the pad through XInput.

    1. Starts the guest if it is off and -StartVm is given (nothing else about
       the guest is changed: no GPU partition, no disks, no deletion).
    2. Waits for PowerShell Direct, and for the Gate C monitor's /health.
    3. Writes a shared key to <VmRoot>\netpad.key on the host if there is none.
    4. Copies protocol.py, receiver.py, src\padbus_client.py and the key flat
       into C:\nimbus\netpad (the embeddable Python ignores the script's folder,
       so receiver.py adds it itself).
    5. Opens UDP 47200 and TCP 47201 (the read-only status page) in the guest
       firewall.
    6. Runs the receiver as the logon task NimbusNetpadReceiver in the guest's
       auto-logged-on session, logging to C:\nimbus\logs\netpad.log, and waits
       for its status page from the host.

    -Stop ends the task and deletes it; the files and firewall rules stay.
    No elevation on the host: step 10 of vm\README.md put the console user in
    Hyper-V Administrators.

.EXAMPLE
    powershell -File netpad\deploy-guest.ps1 -StartVm
    venv\Scripts\python -m netpad.e2e_check --receiver <ip printed above>
#>
[CmdletBinding()]
param(
    [switch]$StartVm,
    [switch]$Stop,
    [int]$Port = 47200,
    [int]$StatusPort = 47201,
    [string]$VmRoot
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vm\common.ps1')

$repo = Split-Path -Parent $PSScriptRoot
$root = Get-VmRoot $VmRoot
$cred = Get-GuestCredential -VmRoot $root
$task = 'NimbusNetpadReceiver'

$vm = Get-VM -Name $script:VmName
if ($vm.State -ne 'Running') {
    if (-not $StartVm) { throw "$($script:VmName) is $($vm.State); pass -StartVm to start it" }
    Write-Step "starting $($script:VmName)"
    Start-VM -Name $script:VmName
}
Write-Step 'waiting for PowerShell Direct'
if (-not (Wait-GuestSession -Credential $cred -TimeoutSec 900)) { throw 'the guest did not answer PowerShell Direct within 15 minutes' }

$session = New-PSSession -VMName $script:VmName -Credential $cred
try {
    if ($Stop) {
        Invoke-Command -Session $session -ScriptBlock {
            param($t)
            & schtasks.exe /End /TN $t 2>$null | Out-Null
            & schtasks.exe /Delete /F /TN $t 2>$null | Out-Null
            Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
                Where-Object { $_.CommandLine -like '*netpad*receiver.py*' } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        } -ArgumentList $task
        Write-Step 'receiver stopped'
        return
    }

    $key = Join-Path $root 'netpad.key'
    if (-not (Test-Path $key)) {
        Write-Step "writing a new key to $key"
        $py = Join-Path $repo 'venv\Scripts\python.exe'
        Push-Location $repo
        try { & $py -m netpad.receiver keygen $key } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw 'keygen failed' }
    }

    Write-Step 'copying the receiver into C:\nimbus\netpad'
    Invoke-Command -Session $session -ScriptBlock {
        param($t)
        & schtasks.exe /End /TN $t 2>$null | Out-Null
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.CommandLine -like '*netpad*receiver.py*' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        New-Item -ItemType Directory -Path 'C:\nimbus\netpad' -Force | Out-Null
    } -ArgumentList $task
    foreach ($f in @((Join-Path $PSScriptRoot 'protocol.py'), (Join-Path $PSScriptRoot 'receiver.py'),
                     (Join-Path $repo 'src\padbus_client.py'), $key)) {
        Copy-Item -ToSession $session -Path $f -Destination 'C:\nimbus\netpad\' -Force
    }

    Write-Step 'firewall, task, start'
    $result = Invoke-Command -Session $session -ScriptBlock {
        param($t, $port, $statusPort, $user)
        foreach ($r in @(@{ Name = 'Nimbus netpad UDP'; Protocol = 'UDP'; Port = "$port" },
                         @{ Name = 'Nimbus netpad status'; Protocol = 'TCP'; Port = "$statusPort" })) {
            if (-not (Get-NetFirewallRule -DisplayName $r.Name -ErrorAction SilentlyContinue)) {
                New-NetFirewallRule -DisplayName $r.Name -Direction Inbound -Action Allow -Protocol $r.Protocol -LocalPort $r.Port -Profile Any | Out-Null
            }
        }
        $cmdFile = 'C:\nimbus\netpad\run-receiver.cmd'
        $line = "C:\nimbus\python\python.exe -u C:\nimbus\netpad\receiver.py run --key C:\nimbus\netpad\netpad.key --port $port --status-port $statusPort > C:\nimbus\logs\netpad.log 2>&1"
        Set-Content -Path $cmdFile -Value $line -Encoding ASCII
        & schtasks.exe /Create /F /TN $t /SC ONLOGON /RU $user /TR $cmdFile | Out-Null
        & schtasks.exe /Run /TN $t | Out-Null
        $monitor = $false
        try { $monitor = ((Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:47100/health' -TimeoutSec 5).Content -eq 'ok') } catch { }
        if (-not $monitor) {
            & schtasks.exe /Run /TN NimbusGateCMonitor | Out-Null
        }
        [pscustomobject]@{ MonitorWasUp = $monitor }
    } -ArgumentList $task, $Port, $StatusPort, $script:GuestAdmin
    if (-not $result.MonitorWasUp) { Write-Host '    the Gate C monitor was not answering; its task was started' }
} finally {
    Remove-PSSession $session
}

$ip = (Get-VMNetworkAdapter -VMName $script:VmName).IPAddresses | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1
if (-not $ip) { throw 'the guest has no IPv4 address yet' }
Write-Step "waiting for the receiver at $ip"
$deadline = (Get-Date).AddSeconds(60)
$up = $false
while ((Get-Date) -lt $deadline -and -not $up) {
    try {
        $null = Invoke-WebRequest -UseBasicParsing "http://${ip}:$StatusPort/status" -TimeoutSec 3
        $up = $true
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $up) { throw "no status page at http://${ip}:$StatusPort/status (guest log: C:\nimbus\logs\netpad.log)" }
$monitorUp = $false
try { $monitorUp = ((Invoke-WebRequest -UseBasicParsing "http://${ip}:47100/health" -TimeoutSec 5).Content -eq 'ok') } catch { }
Write-Host "receiver: udp ${ip}:$Port  status http://${ip}:$StatusPort/status  monitor $(if ($monitorUp) { 'up' } else { 'NOT answering' })"
Write-Host "next: venv\Scripts\python -m netpad.e2e_check --receiver $ip --key $key"
