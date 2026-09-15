<#
.SYNOPSIS
    Read-only inventory of the host for the guest VM track. Needs no
    elevation and changes nothing.

.DESCRIPTION
    Answers, before anyone enables Hyper-V, the questions
    docs\vision\VIRTUAL_MACHINE_FEASIBILITY.md sections 2 and 3 ask of the
    machine: is this a Pro or Enterprise edition, does the firmware expose
    virtualization, is Hyper-V already on, is there one GPU or two, how much
    memory and disk are free, is VirtualBox installed (Hyper-V slows it
    down), would the Nimbus Mouse Filter survive the reboot, and is a reboot
    already pending. Prints a verdict: READY (10-enable-hyperv.ps1 can run),
    ALREADY ENABLED (skip to 20-check-gpu-partition.ps1) or BLOCKED (why).

    Exit codes: 0 ready or already enabled, 1 blocked, 2 could not decide.

.PARAMETER VmRoot
    Where the guest's files will live (default C:\NimbusVM or
    $env:NIMBUS_VM_ROOT). Only its drive's free space is checked here.

.PARAMETER Json
    Also write the inventory to <VmRoot>\logs\preflight-<timestamp>.json.
#>
[CmdletBinding()]
param(
    [string]$VmRoot,
    [switch]$Json
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'common.ps1')

$VmRoot = Get-VmRoot $VmRoot
$blocks = @()
$warnings = @()

Write-Host '=== Nimbus guest VM: host preflight ==='
Write-Host "Host        : $env:COMPUTERNAME"

$hv = Get-HyperVState
Write-Host "Windows     : $($hv.Product) $($hv.OsVersion)"
$editionOk = $hv.Product -match 'Pro|Enterprise|Education'
if (-not $editionOk) { $blocks += 'Hyper-V needs Windows Pro, Enterprise or Education (Home has no hypervisor role)' }

Write-Host ("Firmware VT : {0}   SLAT: {1}   monitor mode: {2}" -f $hv.FirmwareVt, $hv.Slat, $hv.MonitorModeExt)
if ($hv.FirmwareVt -eq $false) { $blocks += 'virtualization is disabled in firmware (SVM / VT-x)' }
if ($hv.Slat -eq $false) { $blocks += 'no second level address translation' }

$featureWord = switch ($hv.Features['Microsoft-Hyper-V']) { 1 { 'enabled' } 2 { 'disabled' } 3 { 'absent' } default { 'unknown' } }
Write-Host ("Hyper-V     : feature {0}; hypervisor running: {1}; vmms running: {2}; module: {3}" -f
    $featureWord, $hv.HypervisorPresent, $hv.VmmsRunning, $hv.ModulePresent)

$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$memGB = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
$freeGB = [math]::Round($os.FreePhysicalMemory * 1KB / 1GB, 1)
Write-Host ("CPU         : {0}  ({1} cores / {2} threads)" -f $cpu.Name.Trim(), $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors)
Write-Host ("Memory      : {0} GiB total, {1} GiB free now" -f $memGB, $freeGB)
if ($memGB -lt 24) { $warnings += "only $memGB GiB: a 16 GiB guest leaves the host tight; use -MemoryGB 8 on 40-new-guest.ps1" }

$gpu = Get-DisplayDriverInfo
if ($gpu) {
    Write-Host ("GPU         : {0}  driver {1} ({2:yyyy-MM-dd})  {3}" -f $gpu.Name, $gpu.DriverVersion, $gpu.DriverDate, $gpu.InfName)
    Write-Host ("Driver store: {0}  ({1} MB, copied into the guest by 50-attach-gpu.ps1)" -f $gpu.StoreDir, $gpu.StoreMB)
    Write-Host ("PCI adapters: {0}  (one means the host keeps the display and the guest gets a partition, never passthrough)" -f $gpu.Adapters)
    if (-not $gpu.StoreDir) { $blocks += 'could not find the display driver package in the driver store' }
} else {
    $blocks += 'no PCI display adapter found'
}

$drive = (Get-Item $VmRoot).PSDrive
$diskFree = [math]::Round((Get-PSDrive $drive.Name).Free / 1GB, 1)
Write-Host ("VM root     : {0}  ({1} GB free on {2}:)" -f $VmRoot, $diskFree, $drive.Name)
if ($diskFree -lt 40) { $blocks += "less than 40 GB free on $($drive.Name): for the ISO and a 128 GB dynamic disk" }
elseif ($diskFree -lt 150) { $warnings += "$diskFree GB free: enough for the guest and one or two games, not a Steam library" }

$vbox = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like 'Oracle VirtualBox*' } | Select-Object -First 1
if ($vbox) {
    Write-Host ("VirtualBox  : {0} installed" -f $vbox.DisplayName)
    $warnings += 'VirtualBox is installed: with Hyper-V on it falls back to the Hyper-V API and its guests run markedly slower'
}

$ts = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
Write-Host ("Display ver : {0}" -f $ts.DisplayVersion)

$pendingCbs = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
$pendingWu = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
Write-Host ("Reboot pend : CBS {0}, Windows Update {1}" -f $pendingCbs, $pendingWu)
if ($pendingCbs -or $pendingWu) { $warnings += 'a reboot is already pending; let it happen before enabling Hyper-V so one reboot does not hide the other' }

$filterCheck = Join-Path $script:RepoRoot 'driver\check-mouse-filter.ps1'
if (Test-Path $filterCheck) {
    Write-Host '--- driver\check-mouse-filter.ps1 (would the mouse survive the reboot?)'
    & $filterCheck | ForEach-Object { Write-Host "    $_" }
    $filterExit = $LASTEXITCODE
    if ($filterExit -ne 0) { $blocks += "check-mouse-filter.ps1 exited ${filterExit}: fix the mouse filter before any reboot" }
} else {
    $warnings += 'driver\check-mouse-filter.ps1 not found; check the mouse filter by hand before rebooting'
}

$me = Get-InteractiveUser
$hvAdmins = @()
try { $hvAdmins = @(Get-LocalGroupMember -Group 'Hyper-V Administrators' -ErrorAction Stop | Select-Object -ExpandProperty Name) } catch { }
$inGroup = $hvAdmins -contains $me
Write-Host ("Console user: {0}  (Hyper-V Administrators: {1})" -f $me, $inGroup)
if ($hv.FeatureEnabled -and -not $inGroup -and -not (Test-Elevated)) {
    $warnings += "$me is not in Hyper-V Administrators, so the VM cmdlets need elevation; 10-enable-hyperv.ps1 adds the console user"
}

Write-Host ''
foreach ($w in $warnings) { Write-Host "WARN  $w" -ForegroundColor Yellow }
foreach ($b in $blocks) { Write-Host "BLOCK $b" -ForegroundColor Red }

$verdict = if ($blocks.Count -gt 0) { 'BLOCKED' } elseif ($hv.FeatureEnabled -and $hv.HypervisorPresent) { 'ALREADY ENABLED' }
           elseif ($hv.FeatureEnabled -and -not $hv.HypervisorPresent) { 'ENABLED, REBOOT PENDING' } else { 'READY' }
Write-Host ''
Write-Host "VERDICT: $verdict" -ForegroundColor $(if ($verdict -eq 'BLOCKED') { 'Red' } else { 'Green' })
switch ($verdict) {
    'READY'                   { Write-Host 'Next: elevated  vm\10-enable-hyperv.ps1  (then reboot)' }
    'ENABLED, REBOOT PENDING' { Write-Host 'Next: reboot, then  vm\20-check-gpu-partition.ps1' }
    'ALREADY ENABLED'         { Write-Host 'Next: vm\20-check-gpu-partition.ps1' }
}

if ($Json) {
    $out = [ordered]@{
        timestamp = (Get-Date).ToString('o'); host = $env:COMPUTERNAME; verdict = $verdict
        product = $hv.Product; os_version = $hv.OsVersion; display_version = $ts.DisplayVersion
        hyperv = @{ feature = $featureWord; hypervisor_present = $hv.HypervisorPresent; module = $hv.ModulePresent; vmms = $hv.VmmsRunning }
        firmware_vt = $hv.FirmwareVt; slat = $hv.Slat
        cpu = $cpu.Name.Trim(); cores = $cpu.NumberOfCores; threads = $cpu.NumberOfLogicalProcessors
        memory_gib = $memGB; memory_free_gib = $freeGB
        gpu = if ($gpu) { @{ name = $gpu.Name; driver = $gpu.DriverVersion; inf = $gpu.InfName; store_dir = $gpu.StoreDir; store_mb = $gpu.StoreMB; pci_adapters = $gpu.Adapters } } else { $null }
        vm_root = $VmRoot; disk_free_gb = $diskFree
        virtualbox = if ($vbox) { $vbox.DisplayName } else { $null }
        reboot_pending = ($pendingCbs -or $pendingWu)
        console_user = $me; hyperv_administrator = $inGroup
        warnings = $warnings; blocks = $blocks
    }
    $path = Join-Path $VmRoot ('logs\preflight-{0:yyyyMMdd-HHmmss}.json' -f (Get-Date))
    $out | ConvertTo-Json -Depth 4 | Set-Content -Path $path -Encoding UTF8
    Write-Host "Written: $path"
}

if ($blocks.Count -gt 0) { exit 1 }
exit 0
