<#
.SYNOPSIS
    Enable the Hyper-V role (hypervisor, services, PowerShell module) and
    let the console user manage VMs without elevation. Elevated. Needs a
    reboot afterwards, which this script does NOT perform.

.DESCRIPTION
    The one irreversible-feeling step of the track, so it is deliberate:

    1. Runs driver\check-mouse-filter.ps1 and refuses if the mouse would not
       survive the reboot (the filter's failure mode is no mouse at all).
    2. Refuses if a reboot is already pending, so the reboot that enables
       Hyper-V is not confused with one that does something else.
    3. Enable-WindowsOptionalFeature Microsoft-Hyper-V-All (-NoRestart).
    4. Adds the console user to the local "Hyper-V Administrators" group, so
       New-VM, Start-VM, Get-VMNetworkAdapter and PowerShell Direct work
       from a standard session afterwards. Disk mounting still needs
       elevation, which is why 40 and 50 stay elevated.
    5. Records what it did to <VmRoot>\logs\vm.log and prints the reboot
       warning from docs\vision\VIRTUAL_MACHINE_FEASIBILITY.md section 2.

    After the reboot: vm\20-check-gpu-partition.ps1.

    Reversal: -Disable removes the role again (also needs a reboot) and
    takes the user back out of the group. It does not delete any VM.

.PARAMETER User
    Account to add to Hyper-V Administrators. Default: the console user
    (DOMAIN\name), even when this runs under a different admin account.

.PARAMETER Force
    Skip the mouse filter and pending-reboot refusals. Only with a second
    pointing device or remote access that survives a dead mouse.

.PARAMETER Disable
    Turn the role off instead.
#>
[CmdletBinding()]
param(
    [string]$VmRoot,
    [string]$User,
    [switch]$Force,
    [switch]$Disable
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Elevated -Because 'enabling a Windows feature and editing a local group need it'
$VmRoot = Get-VmRoot $VmRoot
if (-not $User) { $User = Get-InteractiveUser }

$features = @('Microsoft-Hyper-V-All')

if ($Disable) {
    Write-Step "Disabling Hyper-V ($($features -join ', '))"
    $r = Disable-WindowsOptionalFeature -Online -FeatureName $features -NoRestart
    try { Remove-LocalGroupMember -Group 'Hyper-V Administrators' -Member $User -ErrorAction Stop; Write-Host "  removed $User from Hyper-V Administrators" } catch { }
    Write-VmLog $VmRoot "hyper-v disabled by $env:USERNAME (restart needed: $($r.RestartNeeded))"
    Write-Host 'Reboot to finish removing the hypervisor.' -ForegroundColor Yellow
    exit 0
}

$hv = Get-HyperVState
if ($hv.FeatureEnabled -and $hv.HypervisorPresent) {
    Write-Host 'Hyper-V is already enabled and the hypervisor is running.'
} elseif ($hv.FeatureEnabled) {
    Write-Host 'Hyper-V is already enabled; the hypervisor starts at the next reboot.' -ForegroundColor Yellow
}

# 1. the mouse must survive the reboot
$filterCheck = Join-Path $script:RepoRoot 'driver\check-mouse-filter.ps1'
if (Test-Path $filterCheck) {
    Write-Step 'Mouse filter reboot check'
    & $filterCheck | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE -ne 0 -and -not $Force) {
        throw "check-mouse-filter.ps1 exited ${LASTEXITCODE}: the mouse would not survive the reboot. Fix that first (driver\recover-mouse.ps1) or pass -Force with another pointing device at hand."
    }
}

# 2. one reboot, one reason
$pending = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
           (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')
if ($pending -and -not $Force) {
    throw 'A reboot is already pending (servicing or Windows Update). Reboot first, then run this, so the two do not overlap. -Force overrides.'
}

# 3. the role
if (-not $hv.FeatureEnabled) {
    Write-Step "Enabling $($features -join ', ') (no restart)"
    $r = Enable-WindowsOptionalFeature -Online -FeatureName $features -All -NoRestart
    Write-Host "  RestartNeeded: $($r.RestartNeeded)"
    Write-VmLog $VmRoot "hyper-v enabled by $env:USERNAME (restart needed: $($r.RestartNeeded))"
}

# 4. the console user manages VMs without a prompt
Write-Step "Adding $User to Hyper-V Administrators"
try {
    Add-LocalGroupMember -Group 'Hyper-V Administrators' -Member $User -ErrorAction Stop
    Write-Host '  added (takes effect at the next logon, which the reboot provides)'
    Write-VmLog $VmRoot "$User added to Hyper-V Administrators"
} catch {
    if ($_.Exception.Message -match 'already a member') { Write-Host '  already a member' }
    else { throw }
}

Write-Host ''
Write-Host 'REBOOT REQUIRED to start the hypervisor. Nothing runs under it until then.' -ForegroundColor Yellow
Write-Host 'From the feasibility document, section 2: after the reboot Windows itself runs as a partition above the'
Write-Host 'hypervisor. This host is the baseline for every mouse filter measurement in docs\vision, so re-run the'
Write-Host 'filter suites after enabling before trusting a new driver number, and expect VirtualBox to slow down.'
Write-Host ''
Write-Host 'After the reboot:  vm\20-check-gpu-partition.ps1   (Gate B, part 1: does the GPU partition at all)'
exit 0
