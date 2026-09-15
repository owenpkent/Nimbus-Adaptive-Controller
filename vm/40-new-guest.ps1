<#
.SYNOPSIS
    Build the guest: apply the Windows image straight onto a new VHDX, drop
    the answer file and the guest tooling on it, create a Generation 2 VM
    with a virtual TPM, and start it. Elevated (disk mounting needs it).

.DESCRIPTION
    Windows Setup never runs. The install image is applied with DISM
    (Expand-WindowsImage), the boot files are written with bcdboot, and
    vm\guest\unattend.xml (placeholders filled) goes to
    Windows\Panther\unattend.xml so the first boot runs specialize and OOBE
    by itself: a local administrator that logs on automatically and runs
    C:\nimbus\setup.ps1 once. This avoids the "press any key to boot from
    DVD" prompt of a Generation 2 DVD boot, which would need a hand on the
    console, and it lets the guest tooling ride along on the disk.

    VM settings: Generation 2, Secure Boot with the Microsoft Windows
    template, a vTPM under a local key protector (no Host Guardian
    Service), fixed memory, checkpoints off, automatic stop = shut down,
    the Default Switch (NAT, the guest reaches the internet and the host
    reaches the guest by IP). Enhanced Session Mode is switched OFF on the
    host, because that console forwards the host pointer into the guest
    over RDP and Gate C needs every such path closed. A basic vmconnect
    session still works for watching the guest, and it forwards the
    pointer only while you click inside it.

    Isolation is a configuration property here, not an architectural one
    (feasibility document, section 4). What this script closes: Enhanced
    Session Mode, RDP in the guest (answer file). What it leaves open on
    purpose: the basic vmconnect console, for the human. Do not click inside
    it during a Gate C run.

.PARAMETER Iso
    The ISO. Default: the one recorded by 30-fetch-iso.ps1.

.PARAMETER ImageName
    Which edition to apply when the ISO holds several. Matched as a
    substring of the image name; default "Pro". -ImageIndex overrides.

.PARAMETER Password
    The guest administrator's password. Random when omitted. Written to
    <VmRoot>\guest-password.txt for the later scripts; a lab secret only.

.PARAMETER Wait
    After starting, wait for the guest's first-logon setup to finish (marker
    C:\nimbus\setup-done.json), up to 45 minutes.

.PARAMETER Recreate
    Remove an existing guest of the same name (and its disk) first.
#>
[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingPlainTextForPassword', 'Password',
    Justification = 'The answer file needs the plain text; this is a throwaway lab guest, and the file is documented as such.')]
param(
    [string]$VmRoot,
    [string]$Iso,
    [string]$ImageName = 'Pro',
    [int]$ImageIndex = 0,
    [int]$MemoryGB = 16,
    [int]$Cpus = 8,
    [int]$DiskGB = 128,
    [string]$Password,
    [string]$SwitchName = 'Default Switch',
    [string]$TimeZone = (Get-TimeZone).Id,
    [switch]$NoStart,
    [switch]$Wait,
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Elevated -Because 'mounting the ISO and the new disk and applying the image need it'
Import-Module Hyper-V -ErrorAction Stop
$VmRoot = Get-VmRoot $VmRoot
$guestSrc = Join-Path $PSScriptRoot 'guest'

# ---- inputs -----------------------------------------------------------------
if (-not $Iso) {
    $isoJson = Join-Path $VmRoot 'iso\iso.json'
    if (-not (Test-Path $isoJson)) { throw "no ISO given and no $isoJson; run 30-fetch-iso.ps1 or pass -Iso" }
    $Iso = (Get-Content $isoJson -Raw | ConvertFrom-Json).path
}
if (-not (Test-Path $Iso)) { throw "ISO not found: $Iso" }
if (-not (Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue)) {
    throw "no virtual switch named '$SwitchName' (the Default Switch appears once Hyper-V is enabled and rebooted)"
}

$existing = Get-VM -Name $script:VmName -ErrorAction SilentlyContinue
if ($existing) {
    if (-not $Recreate) { throw "a VM named $script:VmName already exists; pass -Recreate to replace it" }
    Write-Step "Removing the existing $script:VmName"
    if ($existing.State -ne 'Off') { Stop-VM -Name $script:VmName -TurnOff -Force }
    $oldDisks = @($existing | Get-VMHardDiskDrive | Select-Object -ExpandProperty Path)
    Remove-VM -Name $script:VmName -Force
    foreach ($d in $oldDisks) { if (Test-Path $d) { Remove-Item $d -Force } }
    Write-VmLog $VmRoot "removed previous guest and its disk(s): $($oldDisks -join ', ')"
}

if (-not $Password) {
    $Password = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 16 | ForEach-Object { [char]$_ })
}
$pwFile = Join-Path $VmRoot 'guest-password.txt'
Set-Content -Path $pwFile -Value $Password -Encoding ASCII -NoNewline

$vhdx = Join-Path $VmRoot "disks\$($script:VmName).vhdx"
if (Test-Path $vhdx) { Remove-Item $vhdx -Force }

# ---- the image --------------------------------------------------------------
Write-Step "Mounting $Iso"
$isoImage = Mount-DiskImage -ImagePath $Iso -PassThru
$isoLetter = $null
for ($i = 0; $i -lt 20 -and -not $isoLetter; $i++) {
    Start-Sleep -Milliseconds 500
    $isoLetter = ($isoImage | Get-Volume -ErrorAction SilentlyContinue).DriveLetter
}
if (-not $isoLetter) { throw 'the ISO mounted without a drive letter' }
$wim = "$($isoLetter):\sources\install.wim"
if (-not (Test-Path $wim)) { $wim = "$($isoLetter):\sources\install.esd" }
if (-not (Test-Path $wim)) { Dismount-DiskImage -ImagePath $Iso | Out-Null; throw 'no sources\install.wim or install.esd on the ISO' }

$images = @(Get-WindowsImage -ImagePath $wim)
Write-Host '  images on the ISO:'
$images | ForEach-Object { Write-Host ("    [{0}] {1}" -f $_.ImageIndex, $_.ImageName) }
if ($ImageIndex -gt 0) {
    $pick = $images | Where-Object { $_.ImageIndex -eq $ImageIndex }
} else {
    $pick = $images | Where-Object { $_.ImageName -eq "Windows 11 $ImageName" } | Select-Object -First 1
    if (-not $pick) { $pick = $images | Where-Object { $_.ImageName -like "*$ImageName*" -and $_.ImageName -notlike '*N' -and $_.ImageName -notlike '*Education*' -and $_.ImageName -notlike '*Workstations*' } | Select-Object -First 1 }
    if (-not $pick) { $pick = $images | Where-Object { $_.ImageName -like "*$ImageName*" } | Select-Object -First 1 }
}
if (-not $pick) { Dismount-DiskImage -ImagePath $Iso | Out-Null; throw "no image matching '$ImageName' (index $ImageIndex)" }
Write-Host ("  applying [{0}] {1}" -f $pick.ImageIndex, $pick.ImageName)

$mounted = $false
try {
    # ---- the disk -----------------------------------------------------------
    Write-Step "Creating $vhdx ($DiskGB GB, dynamic)"
    New-VHD -Path $vhdx -SizeBytes ($DiskGB * 1GB) -Dynamic | Out-Null
    $disk = Mount-VHD -Path $vhdx -Passthru | Get-Disk
    $mounted = $true
    Initialize-Disk -Number $disk.Number -PartitionStyle GPT -PassThru | Out-Null

    # EFI: created as basic data so Format-Volume accepts it and it keeps a
    # drive letter for bcdboot; retyped to the EFI System Partition GUID at
    # the end, after everything that needs the letter has run.
    $efi = New-Partition -DiskNumber $disk.Number -Size 260MB -AssignDriveLetter
    $efi | Format-Volume -FileSystem FAT32 -NewFileSystemLabel 'System' -Confirm:$false | Out-Null
    $efi = Get-Partition -DiskNumber $disk.Number -PartitionNumber $efi.PartitionNumber
    New-Partition -DiskNumber $disk.Number -Size 16MB -GptType '{e3c9e316-0b5c-4db8-817d-f92df00215ae}' | Out-Null
    $os = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter
    $os | Format-Volume -FileSystem NTFS -NewFileSystemLabel 'Windows' -Confirm:$false | Out-Null
    $os = Get-Partition -DiskNumber $disk.Number -PartitionNumber $os.PartitionNumber
    $osRoot = "$($os.DriveLetter):"
    $efiRoot = "$($efi.DriveLetter):"
    if (-not $os.DriveLetter -or -not $efi.DriveLetter) { throw 'the new partitions got no drive letters' }

    # ---- apply --------------------------------------------------------------
    Write-Step "Applying the image to $osRoot (several minutes)"
    Expand-WindowsImage -ImagePath $wim -Index $pick.ImageIndex -ApplyPath "$osRoot\" | Out-Null
    Write-Step 'Writing boot files'
    & "$env:SystemRoot\System32\bcdboot.exe" "$osRoot\Windows" /s $efiRoot /f UEFI | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "bcdboot exited $LASTEXITCODE" }

    # ---- answer file and guest tooling ------------------------------------
    Write-Step 'Answer file and C:\nimbus'
    $xml = Get-Content (Join-Path $guestSrc 'unattend.xml') -Raw
    $xml = $xml.Replace('{{COMPUTERNAME}}', $script:VmName.ToUpper()).Replace('{{TIMEZONE}}', $TimeZone)
    $xml = $xml.Replace('{{USERNAME}}', $script:GuestAdmin).Replace('{{PASSWORD}}', [System.Security.SecurityElement]::Escape($Password))
    $panther = Join-Path $osRoot 'Windows\Panther'
    New-Item -ItemType Directory -Path $panther -Force | Out-Null
    Set-Content -Path (Join-Path $panther 'unattend.xml') -Value $xml -Encoding UTF8
    $nimbusDir = Join-Path $osRoot 'nimbus'
    New-Item -ItemType Directory -Path (Join-Path $nimbusDir 'logs') -Force | Out-Null
    Get-ChildItem $guestSrc -File | Where-Object { $_.Name -ne 'unattend.xml' } | Copy-Item -Destination $nimbusDir -Force
    $guestJson = [ordered]@{
        host = $env:COMPUTERNAME; vm_name = $script:VmName; user = $script:GuestAdmin; password = $Password
        monitor_port = $script:MonitorPort; created = (Get-Date).ToString('o'); image = $pick.ImageName
    }
    $guestJson | ConvertTo-Json | Set-Content -Path (Join-Path $nimbusDir 'guest.json') -Encoding UTF8

    # Last: make the boot partition an EFI System Partition. Done after
    # bcdboot and the copies because retyping can drop the drive letter.
    Set-Partition -DiskNumber $disk.Number -PartitionNumber $efi.PartitionNumber -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
} finally {
    if ($mounted) { Dismount-VHD -Path $vhdx -ErrorAction SilentlyContinue }
    Dismount-DiskImage -ImagePath $Iso -ErrorAction SilentlyContinue | Out-Null
}

# ---- the VM -----------------------------------------------------------------
Write-Step "Creating $script:VmName"
$vmhost = Get-VMHost
if ($vmhost.EnableEnhancedSessionMode) {
    Set-VMHost -EnableEnhancedSessionMode $false
    Write-VmLog $VmRoot 'host Enhanced Session Mode was ON; turned OFF (it forwards the host pointer into guests over RDP)'
}
New-VM -Name $script:VmName -Generation 2 -MemoryStartupBytes ($MemoryGB * 1GB) -VHDPath $vhdx -Path $VmRoot -SwitchName $SwitchName | Out-Null
Set-VMProcessor -VMName $script:VmName -Count $Cpus
Set-VMMemory -VMName $script:VmName -DynamicMemoryEnabled $false
Set-VM -Name $script:VmName -CheckpointType Disabled -AutomaticCheckpointsEnabled $false -AutomaticStopAction ShutDown -AutomaticStartAction Nothing
Set-VMFirmware -VMName $script:VmName -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows
Set-VMKeyProtector -VMName $script:VmName -NewLocalKeyProtector
Enable-VMTPM -VMName $script:VmName
Write-VmLog $VmRoot "guest created: $($pick.ImageName), $MemoryGB GB, $Cpus vCPU, $DiskGB GB disk, switch '$SwitchName', vTPM on, ESM off"

if ($NoStart) { Write-Host 'Created, not started (-NoStart).'; exit 0 }
Write-Step 'Starting the guest (first boot: specialize, OOBE, automatic logon, C:\nimbus\setup.ps1)'
Start-VM -Name $script:VmName
Write-Host ("  watch it with:  vmconnect.exe localhost {0}   (basic session; do not click inside during Gate C)" -f $script:VmName)

if ($Wait) {
    Write-Step 'Waiting for the first-logon setup to finish (marker C:\nimbus\setup-done.json)'
    $cred = Get-GuestCredential -VmRoot $VmRoot
    if (Wait-GuestSession -Credential $cred -TimeoutSec 2700 -Marker 'C:\nimbus\setup-done.json') {
        $done = Invoke-Command -VMName $script:VmName -Credential $cred -ScriptBlock { Get-Content 'C:\nimbus\setup-done.json' -Raw }
        Write-Host $done
        Write-VmLog $VmRoot 'guest first-logon setup finished'
    } else {
        Write-Warning 'the guest did not report setup-done within 45 minutes; look at it in vmconnect'
    }
}
Write-Host ''
Write-Host 'Next: elevated  vm\50-attach-gpu.ps1' -ForegroundColor Green
