<#
.SYNOPSIS
    Gate B, part 1: does this host's GPU driver offer a partition at all?
    Needs Hyper-V enabled and a reboot behind it; no VM, nothing changed.

.DESCRIPTION
    Get-VMHostPartitionableGpu lists every adapter whose WDDM driver
    supports GPU paravirtualization. On a client edition it is empty when
    the driver does not, and that is the whole answer: there is no other
    route on a Windows host (feasibility document, section 2). When it is
    populated, the script records the instance path and the partition
    counts the driver allows, which 50-attach-gpu.ps1 reads back.

    Also reports the host's Enhanced Session Mode setting (the RDP-over-VMBus
    console that forwards the host pointer into a guest; Gate C needs it
    off) and whether the console user can run the VM cmdlets unelevated.

    Exit codes: 0 PASS (a partitionable GPU), 1 FAIL (none), 2 Hyper-V is
    not running (enable it, reboot, come back).

.PARAMETER VmRoot
    Where to write logs\gate-b-partitionable.json.
#>
[CmdletBinding()]
param([string]$VmRoot)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$VmRoot = Get-VmRoot $VmRoot

$hv = Get-HyperVState
if (-not $hv.HypervisorPresent -or -not $hv.ModulePresent) {
    Write-Host ('Hyper-V is not running on this boot (feature enabled: {0}, hypervisor present: {1}, module: {2}).' -f $hv.FeatureEnabled, $hv.HypervisorPresent, $hv.ModulePresent)
    Write-Host 'Run vm\10-enable-hyperv.ps1 elevated, reboot, then this.'
    exit 2
}

try {
    Import-Module Hyper-V -ErrorAction Stop
} catch {
    Write-Host "Hyper-V module failed to load: $($_.Exception.Message)"
    exit 2
}

Write-Step 'Partitionable GPUs (Get-VMHostPartitionableGpu)'
$gpus = @()
try {
    $gpus = @(Get-VMHostPartitionableGpu -ErrorAction Stop)
} catch {
    Write-Host "  Get-VMHostPartitionableGpu failed: $($_.Exception.Message)"
    Write-Host '  (a standard user needs the Hyper-V Administrators group, and a fresh logon after being added)'
    exit 2
}

$driver = Get-DisplayDriverInfo
$record = [ordered]@{
    timestamp = (Get-Date).ToString('o'); host = $env:COMPUTERNAME
    display_adapter = $driver.Name; driver_version = $driver.DriverVersion; inf = $driver.InfName
    partitionable = @()
}
foreach ($g in $gpus) {
    Write-Host "  Name                 : $($g.Name)"
    Write-Host "  ValidPartitionCounts : $($g.ValidPartitionCounts -join ', ')"
    Write-Host "  PartitionCount       : $($g.PartitionCount)"
    foreach ($p in 'TotalVRAM', 'AvailableVRAM', 'MinPartitionVRAM', 'MaxPartitionVRAM', 'OptimalPartitionVRAM',
                   'TotalEncode', 'AvailableEncode', 'TotalDecode', 'AvailableDecode', 'TotalCompute', 'AvailableCompute') {
        if ($g.PSObject.Properties[$p]) { Write-Host ("  {0,-21}: {1}" -f $p, $g.$p) }
    }
    $entry = [ordered]@{ name = $g.Name; valid_partition_counts = @($g.ValidPartitionCounts); partition_count = $g.PartitionCount }
    foreach ($p in $g.PSObject.Properties) { if ($p.Name -match 'VRAM|Encode|Decode|Compute') { $entry[$p.Name] = $p.Value } }
    $record.partitionable += $entry
}

Write-Step 'Host settings that matter for Gate C'
$vmhost = Get-VMHost
Write-Host "  EnableEnhancedSessionMode : $($vmhost.EnableEnhancedSessionMode)   (40-new-guest.ps1 turns this off; it is the host pointer's path into a guest)"
Write-Host "  VirtualMachinePath        : $($vmhost.VirtualMachinePath)"
$record.enhanced_session_mode = $vmhost.EnableEnhancedSessionMode
$record.elevated = Test-Elevated

$path = Join-Path $VmRoot 'logs\gate-b-partitionable.json'
$record | ConvertTo-Json -Depth 4 | Set-Content -Path $path -Encoding UTF8
Write-Host "Written: $path"

if ($gpus.Count -gt 0) {
    Write-Host ''
    Write-Host "GATE B (part 1) PASS: $($gpus.Count) partitionable adapter(s). The guest can now be built: vm\30-fetch-iso.ps1, vm\40-new-guest.ps1, vm\50-attach-gpu.ps1." -ForegroundColor Green
    exit 0
}
Write-Host ''
Write-Host "GATE B (part 1) FAIL: no partitionable GPU. The $($driver.Name) driver $($driver.DriverVersion) offers no partition on this host." -ForegroundColor Red
Write-Host 'A newer AMD driver may; otherwise this is the end of the Windows-host VM track (feasibility document, section 7).'
exit 1
