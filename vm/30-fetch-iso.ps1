<#
.SYNOPSIS
    Download a Windows 11 ISO for the guest into <VmRoot>\iso. No elevation.

.DESCRIPTION
    There is no scriptable link for the Enterprise evaluation ISO (its
    fwlink lands on a registration form), so this uses Fido, pbatard's
    script that asks Microsoft's own download page for the retail
    multi-edition ISO link, the same way Rufus does. The guest then runs
    Windows 11 Pro unactivated, which is fine for a lab guest and is the
    "second Windows license" cost the feasibility document counts.

    Pass -Url to use any ISO link you already have (an eval ISO downloaded
    by hand, a VLSC image). The file is resumed if the download breaks.
    Match the guest to the host's build where possible: the GPU-PV
    community's experience is that mismatched host and guest builds cause
    blue screens (feasibility document, section 10).

    Writes <VmRoot>\iso\iso.json (path, source, size, SHA256) for
    40-new-guest.ps1.

.PARAMETER Url
    Direct ISO link. Skips Fido.

.PARAMETER Edition
    Fido edition: Pro (default), Home, Edu.

.PARAMETER Release
    Fido release: Latest (default) or a version such as 25H2.

.PARAMETER NoHash
    Skip the SHA256, which takes about a minute on a 6 GB file.
#>
[CmdletBinding()]
param(
    [string]$VmRoot,
    [string]$Url,
    [ValidateSet('Pro', 'Home', 'Edu')][string]$Edition = 'Pro',
    [string]$Release = 'Latest',
    [string]$Lang = 'English',
    [switch]$NoHash
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$VmRoot = Get-VmRoot $VmRoot
$isoDir = Join-Path $VmRoot 'iso'
$curl = Join-Path $env:SystemRoot 'System32\curl.exe'
if (-not (Test-Path $curl)) { throw 'curl.exe not found (ships with Windows 10 1803 and later)' }

$source = 'url'
if (-not $Url) {
    $source = "fido:$Edition/$Release"
    $fido = Join-Path $isoDir 'Fido.ps1'
    Write-Step 'Fetching Fido.ps1 (pbatard/Fido, MIT)'
    & $curl -L -sS -o $fido 'https://raw.githubusercontent.com/pbatard/Fido/master/Fido.ps1'
    if ($LASTEXITCODE -ne 0) { throw "curl exited $LASTEXITCODE fetching Fido" }
    Write-Step "Asking Microsoft's download page for Windows 11 $Edition $Release x64 $Lang"
    # Continue for this call only: under Stop, Windows PowerShell 5.1 turns
    # any stderr line Fido prints (through 2>&1) into a terminating error,
    # and the guidance below would never be reached.
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $link = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $fido -Win 11 -Rel $Release -Ed $Edition -Lang $Lang -Arch x64 -GetUrl 2>&1 |
            Where-Object { "$_" -match '^https?://' } | Select-Object -Last 1
    } finally {
        $ErrorActionPreference = $eap
    }
    $Url = if ($link) { "$link".Trim() } else { $null }
    if (-not $Url) { throw 'Fido returned no link (rate limited, or the page changed). Get an ISO by hand from https://www.microsoft.com/software-download/windows11 and pass -Url or copy it into ' + $isoDir }
}
Write-Host "  link: $($Url.Substring(0, [math]::Min(120, $Url.Length)))..."

$name = ([uri]$Url).Segments[-1]
if (-not $name.ToLower().EndsWith('.iso')) { $name = "Win11_$Edition.iso" }
$dest = Join-Path $isoDir $name

Write-Step "Downloading to $dest (resumable)"
& $curl -L --retry 5 --retry-delay 5 -C - -o $dest $Url
if ($LASTEXITCODE -ne 0) { throw "curl exited $LASTEXITCODE" }
$file = Get-Item $dest
Write-Host ("  {0:N0} bytes ({1} GB)" -f $file.Length, [math]::Round($file.Length / 1GB, 2))
if ($file.Length -lt 3GB) { throw "the file is too small for a Windows ISO; the link probably expired (they last 24 h). Re-run." }

$hash = $null
if (-not $NoHash) {
    Write-Step 'SHA256'
    $hash = (Get-FileHash -Path $dest -Algorithm SHA256).Hash
    Write-Host "  $hash"
}

$record = [ordered]@{
    path = $dest; source = $source; url_host = ([uri]$Url).Host; bytes = $file.Length
    sha256 = $hash; downloaded = (Get-Date).ToString('o'); edition_hint = $Edition
}
$record | ConvertTo-Json | Set-Content -Path (Join-Path $isoDir 'iso.json') -Encoding UTF8
Write-VmLog $VmRoot "iso downloaded: $dest ($($file.Length) bytes, $source)"
Write-Host ''
Write-Host "Next: elevated  vm\40-new-guest.ps1" -ForegroundColor Green
