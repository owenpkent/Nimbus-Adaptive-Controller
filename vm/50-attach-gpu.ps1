<#
.SYNOPSIS
    Give the guest a partition of the host GPU and the host's display
    driver files, the way Easy-GPU-PV does. Elevated. Gate B, part 2 is
    what the guest then reports (-Verify).

.DESCRIPTION
    1. Stops the guest (a partition adapter is added to a VM that is off).
    2. Set-VM: guest-controlled cache types, 1 GB low and 32 GB high
       memory-mapped I/O space, checkpoints off, stop action shut down.
       These are the values the community scripts settled on.
    3. Add-VMGpuPartitionAdapter (with -InstancePath when the cmdlet has
       it, as on Windows 11) and Set-VMGpuPartitionAdapter with a share of
       every resource the host reports (VRAM, Encode, Decode, Compute):
       min = max = optimal = Total * -Percent, falling back to
       MaxPartition, then to the 1000000000 the older tutorials used.
       Client drivers report these as percentages in disguise.
    4. Copies the display driver package into the guest's
       Windows\System32\HostDriverStore\FileRepository and every other file
       the driver's PnP entry references (Win32_PnPSignedDriverCIMDataFile)
       to the same path on the guest, plus the OpenGL and Vulkan packages
       (amdogl, amdvlk) when the host has them split out. The guest's
       dxgkrnl looks for the host driver there. The VHDX is mounted for the
       copy, so the guest must be off.
    5. Starts the guest and, with -Verify, waits for it and runs
       vm\guest\render_check.py inside it: PASS when the partitioned
       adapter creates a Direct3D 11 device. The result is
       <VmRoot>\logs\gate-b-guest.json.

    Re-run after every host display driver update, or the guest's copy
    drifts from the host's and the adapter stops (Code 43). Run with
    -Remove to take the partition away again.

    Known AMD hazards from the community record (feasibility document,
    section 10): Smart Access Memory (Resizable BAR) on the host crashed
    games in the guest on an RX 6800 XT; Adrenalin 24.7 to 24.10 broke
    hardware encoding in the guest. This host runs 23.4.1.

.PARAMETER Percent
    Share of each GPU resource for the guest. Default 50.

.PARAMETER SkipDrivers
    Only the partition; no file copy.

.PARAMETER Verify
    Start the guest and run the in-guest render check.
#>
[CmdletBinding()]
param(
    [string]$VmRoot,
    [int]$Percent = 50,
    [string]$InstancePath,
    [switch]$SkipDrivers,
    [switch]$Remove,
    [switch]$NoStart,
    [switch]$Verify
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Elevated -Because 'mounting the guest disk needs it'
Import-Module Hyper-V -ErrorAction Stop
$VmRoot = Get-VmRoot $VmRoot
$vm = Get-VM -Name $script:VmName -ErrorAction Stop

function Stop-Guest {
    if ($vm.State -eq 'Off') { return }
    Write-Step 'Shutting the guest down'
    try { Stop-VM -Name $script:VmName -Force -ErrorAction Stop } catch { Stop-VM -Name $script:VmName -TurnOff -Force }
    for ($i = 0; $i -lt 60 -and (Get-VM -Name $script:VmName).State -ne 'Off'; $i++) { Start-Sleep -Seconds 2 }
}

if ($Remove) {
    Stop-Guest
    Get-VMGpuPartitionAdapter -VMName $script:VmName -ErrorAction SilentlyContinue | Remove-VMGpuPartitionAdapter
    Write-VmLog $VmRoot 'GPU partition adapter removed from the guest'
    exit 0
}

$gpus = @(Get-VMHostPartitionableGpu)
if ($InstancePath) { $gpu = $gpus | Where-Object { $_.Name -eq $InstancePath } | Select-Object -First 1 }
else { $gpu = $gpus | Select-Object -First 1 }
if (-not $gpu) { throw 'no partitionable GPU on this host (20-check-gpu-partition.ps1 says why)' }
Write-Host "GPU: $($gpu.Name)"

Stop-Guest

# ---- VM settings and the partition -----------------------------------------
Write-Step 'VM settings for GPU-PV'
Set-VM -Name $script:VmName -GuestControlledCacheTypes $true -LowMemoryMappedIoSpace 1GB -HighMemoryMappedIoSpace 32GB `
       -CheckpointType Disabled -AutomaticStopAction ShutDown

$existing = Get-VMGpuPartitionAdapter -VMName $script:VmName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Step 'Adding the partition adapter'
    $add = Get-Command Add-VMGpuPartitionAdapter
    if ($add.Parameters.ContainsKey('InstancePath')) { Add-VMGpuPartitionAdapter -VMName $script:VmName -InstancePath $gpu.Name }
    else { Add-VMGpuPartitionAdapter -VMName $script:VmName }
} else {
    Write-Host '  partition adapter already present'
}

Write-Step "Partition share: $Percent percent of each resource"
$set = @{ VMName = $script:VmName }
foreach ($res in 'VRAM', 'Encode', 'Decode', 'Compute') {
    $total = $null
    foreach ($src in "Total$res", "MaxPartition$res", "Available$res") {
        $p = $gpu.PSObject.Properties[$src]
        if ($p -and $p.Value -and [uint64]$p.Value -gt 0) { $total = [uint64]$p.Value; break }
    }
    if (-not $total) { $total = [uint64]1000000000 }
    $share = [uint64][math]::Floor([double]$total * $Percent / 100.0)
    if ($share -lt 1) { $share = 1 }
    $set["MinPartition$res"] = $share
    $set["MaxPartition$res"] = $share
    $set["OptimalPartition$res"] = $share
    Write-Host ("  {0,-8} total {1,22}  share {2}" -f $res, $total, $share)
}
Set-VMGpuPartitionAdapter @set
Write-VmLog $VmRoot "GPU partition set: $($gpu.Name), $Percent percent"

# ---- driver files ------------------------------------------------------------
if (-not $SkipDrivers) {
    $pnp = ConvertTo-PnpInstanceId $gpu.Name
    $info = Get-DisplayDriverInfo -PnpId $pnp
    if (-not $info) { throw "no display adapter with PnP id $pnp, the one the partition was taken from" }
    if (-not $info.InfName -or -not $info.StoreDir) { throw "no driver package found for $($info.Name) ($pnp)" }
    Write-Step "Copying the display driver into the guest ($($info.InfName), $($info.StoreMB) MB)"
    $vhdx = ($vm | Get-VMHardDiskDrive | Select-Object -First 1).Path
    $disk = Mount-VHD -Path $vhdx -Passthru | Get-Disk
    try {
        $osVol = Get-Partition -DiskNumber $disk.Number | Get-Volume | Where-Object { $_.FileSystem -eq 'NTFS' } |
            Sort-Object Size -Descending | Select-Object -First 1
        if (-not $osVol.DriveLetter) {
            $part = Get-Partition -DiskNumber $disk.Number | Where-Object { $_.Type -eq 'Basic' } | Sort-Object Size -Descending | Select-Object -First 1
            $part | Add-PartitionAccessPath -AssignDriveLetter
            $osVol = $part | Get-Volume
        }
        $g = "$($osVol.DriveLetter):"
        if (-not (Test-Path "$g\Windows\System32")) { throw "no Windows on $g" }
        $hostStore = Join-Path $env:SystemRoot 'System32\DriverStore\FileRepository'
        $guestStore = "$g\Windows\System32\HostDriverStore\FileRepository"
        New-Item -ItemType Directory -Path $guestStore -Force | Out-Null

        $packages = New-Object System.Collections.Generic.HashSet[string]
        $loose = New-Object System.Collections.Generic.List[string]
        $refs = Get-CimInstance Win32_PnPSignedDriverCIMDataFile | Where-Object { $_.Antecedent.DeviceID -eq $info.PnpId }
        foreach ($r in $refs) {
            $path = $r.Dependent.Name
            if (-not $path) { continue }
            if ($path -match '(?i)\\DriverStore\\FileRepository\\([^\\]+)\\') { [void]$packages.Add($Matches[1]) }
            else { $loose.Add($path) }
        }
        if ($info.StoreDir) { [void]$packages.Add((Split-Path $info.StoreDir -Leaf)) }
        foreach ($extra in 'amdogl', 'amdvlk', 'amdxe') {
            Get-ChildItem $hostStore -Directory -Filter "$extra.inf_amd64_*" -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1 | ForEach-Object { [void]$packages.Add($_.Name) }
        }
        foreach ($pkg in $packages) {
            $src = Join-Path $hostStore $pkg
            if (-not (Test-Path $src)) { continue }
            Write-Host "  package $pkg"
            & robocopy.exe $src (Join-Path $guestStore $pkg) /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE) on $pkg" }
        }
        $copied = 0
        foreach ($f in $loose | Sort-Object -Unique) {
            if (-not (Test-Path $f)) { continue }
            $rel = $f.Substring(3)                      # strip C:\
            $dst = Join-Path $g $rel
            New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null
            Copy-Item -Path $f -Destination $dst -Force
            $copied++
        }
        Write-Host "  $($packages.Count) package(s), $copied loose file(s) copied"
        Write-VmLog $VmRoot "driver files copied into the guest: packages $($packages -join ', '); $copied loose files"
    } finally {
        Dismount-VHD -Path $vhdx -ErrorAction SilentlyContinue
    }
}

if ($NoStart) { Write-Host 'Done, guest left off (-NoStart).'; exit 0 }
Write-Step 'Starting the guest'
Start-VM -Name $script:VmName

if ($Verify) {
    Write-Step 'Gate B, part 2: the guest renders through the partition?'
    $cred = Get-GuestCredential -VmRoot $VmRoot
    if (-not (Wait-GuestSession -Credential $cred -TimeoutSec 600)) { throw 'the guest did not answer PowerShell Direct within 10 minutes' }
    Start-Sleep -Seconds 20     # let PnP finish with the new adapter
    $result = Invoke-Command -VMName $script:VmName -Credential $cred -ScriptBlock {
        $ctrls = Get-CimInstance Win32_VideoController | Select-Object Name, Status, ConfigManagerErrorCode, DriverVersion
        $py = 'C:\nimbus\python\python.exe'
        $out = if (Test-Path $py) { & $py 'C:\nimbus\render_check.py' --json 'C:\nimbus\logs\gate-b.json' 2>&1 } else { 'no guest python yet (setup.ps1 not finished?)' }
        $json = if (Test-Path 'C:\nimbus\logs\gate-b.json') { Get-Content 'C:\nimbus\logs\gate-b.json' -Raw } else { $null }
        [pscustomobject]@{ controllers = $ctrls; render_check = ($out | Out-String); json = $json }
    }
    $result.controllers | Format-Table -AutoSize | Out-String | Write-Host
    Write-Host $result.render_check
    $renderJson = $null
    if ($result.json) { $renderJson = $result.json | ConvertFrom-Json }
    $record = [ordered]@{ timestamp = (Get-Date).ToString('o'); gpu = $gpu.Name; percent = $Percent
                          controllers = $result.controllers; render_check = $result.render_check
                          render_json = $renderJson }
    $record | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $VmRoot 'logs\gate-b-guest.json') -Encoding UTF8
    $pass = $result.render_check -match 'GATE B \(part 2\) PASS'
    Write-VmLog $VmRoot ("gate B part 2: " + $(if ($pass) { 'PASS' } else { 'FAIL' }))
    if (-not $pass) { exit 1 }
}
Write-Host ''
Write-Host 'Next: vm\60-guest-stream.ps1 (Sunshine, virtual display, Moonlight pairing), then vm\gate_c_host.py' -ForegroundColor Green
