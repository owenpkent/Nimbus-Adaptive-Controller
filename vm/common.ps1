<#
.SYNOPSIS
    Helpers shared by the vm\ scripts (dot-sourced).

.DESCRIPTION
    One place for the guest's name, where its files live, elevation checks,
    logging, the display driver lookup and the guest credential, so the
    numbered scripts stay short and agree with each other. Nothing here
    changes the machine.
#>

$script:VmName = 'NimbusGuest'
$script:VmRootDefault = 'C:\NimbusVM'
$script:GuestAdmin = 'nimbus'
$script:MonitorPort = 47100
$script:RepoRoot = Split-Path -Parent $PSScriptRoot

function Get-VmRoot {
    <#
    .SYNOPSIS
        The folder that holds the guest's ISO, disks and logs.
    .DESCRIPTION
        -VmRoot on the calling script, then $env:NIMBUS_VM_ROOT, then
        C:\NimbusVM. Created on first use. Kept outside the repository and
        outside the user profile: Hyper-V's service (running as SYSTEM)
        opens the disk, and a profile path makes that a permissions puzzle.
    #>
    param([string]$VmRoot)
    if (-not $VmRoot) { $VmRoot = $env:NIMBUS_VM_ROOT }
    if (-not $VmRoot) { $VmRoot = $script:VmRootDefault }
    foreach ($sub in '', 'iso', 'disks', 'logs') {
        $p = Join-Path $VmRoot $sub
        if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
    }
    return $VmRoot
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-Elevated {
    param([string]$Because = 'this step changes the machine')
    if (-not (Test-Elevated)) {
        throw "Run this from an elevated PowerShell ($Because). From a standard session: Start-Process powershell -Verb RunAs"
    }
}

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-VmLog {
    <#
    .SYNOPSIS
        Append one line to <VmRoot>\logs\vm.log with a timestamp, and echo it.
    #>
    param([string]$VmRoot, [string]$Message)
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $Message
    Add-Content -Path (Join-Path $VmRoot 'logs\vm.log') -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-InteractiveUser {
    <#
    .SYNOPSIS
        The account logged on at the console, DOMAIN\user, even when this
        script runs elevated under a different administrator account.
    #>
    $u = (Get-CimInstance Win32_ComputerSystem).UserName
    if ($u) { return $u }
    return "$env:USERDOMAIN\$env:USERNAME"
}

function Get-HyperVState {
    <#
    .SYNOPSIS
        Where Hyper-V stands on this host, without needing the Hyper-V module.
    .DESCRIPTION
        Win32_OptionalFeature InstallState: 1 enabled, 2 disabled, 3 absent.
        HypervisorPresent is what the running boot says, which differs from
        the feature state exactly between enabling and rebooting.
    #>
    $features = Get-CimInstance Win32_OptionalFeature -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in 'Microsoft-Hyper-V', 'Microsoft-Hyper-V-Hypervisor', 'Microsoft-Hyper-V-Services',
                                   'Microsoft-Hyper-V-Management-PowerShell', 'VirtualMachinePlatform', 'HypervisorPlatform' }
    $state = @{}
    foreach ($f in $features) { $state[$f.Name] = $f.InstallState }
    $ci = Get-ComputerInfo -Property HyperVisorPresent, HyperVRequirementVirtualizationFirmwareEnabled,
        HyperVRequirementSecondLevelAddressTranslation, HyperVRequirementVMMonitorModeExtensions, WindowsProductName, OsVersion
    [pscustomobject]@{
        Features           = $state
        FeatureEnabled     = ($state['Microsoft-Hyper-V'] -eq 1)
        ModulePresent      = [bool](Get-Module -ListAvailable -Name Hyper-V)
        HypervisorPresent  = [bool]$ci.HyperVisorPresent
        FirmwareVt         = $ci.HyperVRequirementVirtualizationFirmwareEnabled
        Slat               = $ci.HyperVRequirementSecondLevelAddressTranslation
        MonitorModeExt     = $ci.HyperVRequirementVMMonitorModeExtensions
        Product            = $ci.WindowsProductName
        OsVersion          = $ci.OsVersion
        VmmsRunning        = ((Get-Service vmms -ErrorAction SilentlyContinue).Status -eq 'Running')
    }
}

function ConvertTo-PnpInstanceId {
    <#
    .SYNOPSIS
        The PnP instance id inside a device interface path.
    .DESCRIPTION
        Get-VMHostPartitionableGpu names a GPU by interface path, e.g.
        \\?\PCI#VEN_1002&DEV_73FF&...#6&174d5041&0&00000019#{064092b3-...}\GPUPARAV,
        while Win32_VideoController.PNPDeviceID is the instance id,
        PCI\VEN_1002&DEV_73FF&...\6&174D5041&0&00000019. Drop the prefix and
        the interface class, and '#' becomes '\'. Compare case-insensitively.
    #>
    param([Parameter(Mandatory)][string]$InterfacePath)
    $p = $InterfacePath -replace '^\\\\\?\\', ''
    $cut = $p.IndexOf('#{')
    if ($cut -ge 0) { $p = $p.Substring(0, $cut) }
    $p -replace '#', '\'
}

function Get-DisplayDriverInfo {
    <#
    .SYNOPSIS
        The physical display adapter and the driver package behind it.
    .DESCRIPTION
        Returns Name, PnpId, DriverVersion, InfName (the oemNN.inf the class
        driver installed from), the driver store folder that GPU-PV copies
        into the guest (Windows\System32\DriverStore\FileRepository\<inf>_amd64_<hash>),
        and its size. Works without elevation. The folder is found by the
        INF's original name, which is the CatalogFile stem in the oem INF.
    .PARAMETER PnpId
        Describe this adapter. Without it, the first PCI display adapter,
        which is only right on a host with one GPU; anything that copies a
        driver for a partition must pass the partitioned GPU's id, or a host
        with an integrated GPU listed first gets the wrong package.
    #>
    param([string]$PnpId)
    $ctrl = Get-CimInstance Win32_VideoController |
        Where-Object { $_.PNPDeviceID -like 'PCI\*' -and (-not $PnpId -or $_.PNPDeviceID -eq $PnpId) } |
        Select-Object -First 1
    if (-not $ctrl) { return $null }
    $signed = Get-CimInstance Win32_PnPSignedDriver |
        Where-Object { $_.DeviceClass -eq 'DISPLAY' -and $_.DeviceID -eq $ctrl.PNPDeviceID } | Select-Object -First 1
    $inf = if ($signed) { $signed.InfName } else { $null }
    $storeDir = $null
    $storeMB = $null
    if ($inf) {
        $infPath = Join-Path $env:SystemRoot "INF\$inf"
        $original = $null
        if (Test-Path $infPath) {
            $m = Select-String -Path $infPath -Pattern '^\s*CatalogFile\s*=\s*([^\s;]+)' | Select-Object -First 1
            if ($m) { $original = [IO.Path]::GetFileNameWithoutExtension($m.Matches[0].Groups[1].Value).ToLower() }
        }
        if ($original) {
            $repo = Join-Path $env:SystemRoot 'System32\DriverStore\FileRepository'
            $dir = Get-ChildItem $repo -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like "$original.inf_amd64_*" } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($dir) {
                $storeDir = $dir.FullName
                $storeMB = [math]::Round((Get-ChildItem $dir.FullName -Recurse -File -ErrorAction SilentlyContinue |
                    Measure-Object Length -Sum).Sum / 1MB, 0)
            }
        }
    }
    [pscustomobject]@{
        Name          = $ctrl.Name
        PnpId         = $ctrl.PNPDeviceID
        DriverVersion = $ctrl.DriverVersion
        DriverDate    = $ctrl.DriverDate
        InfName       = $inf
        StoreDir      = $storeDir
        StoreMB       = $storeMB
        Adapters      = @(Get-CimInstance Win32_VideoController | Where-Object { $_.PNPDeviceID -like 'PCI\*' }).Count
    }
}

function Get-GuestCredential {
    <#
    .SYNOPSIS
        The guest's local administrator as a PSCredential for PowerShell Direct.
    .DESCRIPTION
        The password is whatever 40-new-guest.ps1 wrote into the answer file;
        it is stored beside the guest in <VmRoot>\guest-password.txt so the
        later scripts can reach the guest unattended. A throwaway lab guest,
        not a secret worth a keyring.
    #>
    param([Parameter(Mandatory)][string]$VmRoot)
    $file = Join-Path $VmRoot 'guest-password.txt'
    if (-not (Test-Path $file)) { throw "no guest password recorded at $file; run 40-new-guest.ps1 first" }
    $pw = (Get-Content $file -Raw).Trim() | ConvertTo-SecureString -AsPlainText -Force
    return New-Object System.Management.Automation.PSCredential("$script:VmName\$script:GuestAdmin", $pw)
}

function Wait-GuestSession {
    <#
    .SYNOPSIS
        Block until PowerShell Direct answers inside the guest, or time out.
    #>
    param(
        [Parameter(Mandatory)][pscredential]$Credential,
        [int]$TimeoutSec = 1800,
        [string]$Marker = ''
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-Command -VMName $script:VmName -Credential $Credential -ErrorAction Stop -ScriptBlock {
                param($m)
                if ($m -and -not (Test-Path $m)) { return 'setup-pending' }
                return 'ready'
            } -ArgumentList $Marker
            if ($r -eq 'ready') { return $true }
        } catch {
            # not booted, not logged on, or the account does not exist yet
        }
        Start-Sleep -Seconds 15
    }
    return $false
}
