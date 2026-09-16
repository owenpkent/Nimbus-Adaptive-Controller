<#
.SYNOPSIS
    Builds the Partner Center attestation-signing submission for the Nimbus
    Mouse Filter, and verifies the package that comes back.

.DESCRIPTION
    Attestation signing takes a CAB file, signed with the EV certificate that
    is registered on the Hardware Developer Program account, and returns the
    same driver signed by Microsoft. This script produces that CAB:

      1. (optional) builds Release x64 with build.ps1
      2. stages inf + sys + pdb into out\submission\nimbus_moufilter\
      3. (optional) re-stamps DriverVer with an explicit version
      4. regenerates the catalog with Inf2Cat so it matches the staged INF
      5. re-signs the .sys and .cat with the EV certificate, replacing the
         WDKTestCert signatures the build applies
      6. packs the folder into a CAB with makecab
      7. signs the CAB (SHA-256 only, RFC-3161 timestamp) and verifies it

    The rules the submission has to satisfy, and the reason each step is here,
    are in driver\SIGNING.md. The two that bite silently: the CAB must be
    SHA-256 signed only (never dual-signed with SHA-1, unlike the app
    installer in build_tools\sign_exe.bat), and every file must sit in a
    subfolder of the CAB, never at its root.

    Run -VerifySigned against the package downloaded from Partner Center to
    confirm Microsoft signed it before it goes anywhere near a user.

.PARAMETER Build
    Run build.ps1 first instead of packaging whatever is already built.

.PARAMETER DriverVersion
    Four-part version to stamp into DriverVer (for example 1.0.0.5). Omitted,
    the build's own stamp is kept, which is the build date and time. A release
    should pass an explicit version: Windows picks the driver with the higher
    DriverVer when a package is replaced.

.PARAMETER Thumbprint
    SHA-1 thumbprint of the EV certificate. Preferred over -CertSubject: the
    machine also holds a WDKTestCert, and picking the wrong one produces a CAB
    that Partner Center rejects.

.PARAMETER CertSubject
    Substring of the EV certificate subject, passed to signtool /n.

.PARAMETER SkipSign
    Build the CAB without signing it. For inspecting the layout without the
    hardware token plugged in. The result contains test-signed binaries and
    must not be submitted.

.PARAMETER VerifySigned
    Path to the driver folder (or a .sys/.cat inside it) returned by Partner
    Center. Checks the signature chains up to the Microsoft Windows Hardware
    Compatibility Publisher, and reports the signer of every file. Does not
    build anything.
#>
param(
    [ValidateSet('Release', 'Debug')] [string]$Configuration = 'Release',
    [ValidateSet('x64')] [string]$Platform = 'x64',
    [switch]$Build,
    [ValidatePattern('^\d+\.\d+\.\d+\.\d+$')] [string]$DriverVersion,
    [string]$Thumbprint,
    [string]$CertSubject,
    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [switch]$SkipSign,
    [string]$VerifySigned
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$name = 'nimbus_moufilter'          # also the CAB subfolder: max 39 chars, no special characters
$kitBin = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"

# The publisher Microsoft signs attestation and WHQL submissions with. Anything
# else on a returned package means the download is not the signed one.
$msPublisher = 'Microsoft Windows Hardware Compatibility Publisher'

function Get-KitTool([string]$Tool, [string]$Arch) {
    $found = Get-ChildItem $kitBin -Filter $Tool -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.DirectoryName -like "*\$Arch" } |
        Sort-Object { $_.DirectoryName } -Descending
    if (-not $found) { throw "$Tool not found under $kitBin. Install the Windows SDK or WDK 10.0.26100." }
    return $found[0].FullName
}

function Invoke-Tool([string]$Exe, [string[]]$ToolArgs, [string]$What) {
    & $Exe @ToolArgs
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

function Show-Signature([string]$Path) {
    $sig = Get-AuthenticodeSignature $Path
    $subject = '(unsigned)'
    if ($sig.SignerCertificate) { $subject = $sig.SignerCertificate.Subject }
    "  {0,-26} {1,-9} {2}" -f (Split-Path $Path -Leaf), $sig.Status, $subject
}

# ---------------------------------------------------------------- verify mode
if ($VerifySigned) {
    if (-not (Test-Path $VerifySigned)) { throw "Not found: $VerifySigned" }
    $item = Get-Item $VerifySigned
    if ($item.PSIsContainer) {
        $files = Get-ChildItem $item.FullName -Recurse -Include *.sys, *.cat, *.dll, *.exe -File
    } else {
        $files = @($item)
    }
    if (-not $files) { throw "No signed binaries found under $VerifySigned" }

    $signtool = Get-KitTool 'signtool.exe' 'x64'
    Write-Host "Verifying $($files.Count) file(s) with $signtool"
    $bad = @()
    foreach ($f in $files) {
        Write-Host (Show-Signature $f.FullName)
        $sig = Get-AuthenticodeSignature $f.FullName
        if ($sig.Status -ne 'Valid') { $bad += "$($f.Name): signature status $($sig.Status)" }
        elseif ($sig.SignerCertificate.Subject -notlike "*$msPublisher*") {
            $bad += "$($f.Name): signed by '$($sig.SignerCertificate.Subject)', not the $msPublisher"
        }
        # /pa is the Authenticode policy. A driver binary and its catalog also
        # have to satisfy the kernel-mode signing policy, which is a different
        # and stricter chain: /pa can pass on a file the loader would still
        # refuse. Both are run here, and the failure names which policy it was,
        # because "signed" and "loadable" are not the same question.
        if ($f.Extension -eq '.cat' -or $f.Extension -eq '.sys') {
            & $signtool verify /pa /v $f.FullName | Out-Null
            if ($LASTEXITCODE -ne 0) { $bad += "$($f.Name): signtool verify /pa (Authenticode policy) failed" }
            & $signtool verify /kp /v $f.FullName | Out-Null
            if ($LASTEXITCODE -ne 0) { $bad += "$($f.Name): signtool verify /kp (kernel-mode policy) failed" }
        }
    }
    if ($bad) {
        Write-Host ''
        $bad | ForEach-Object { Write-Warning $_ }
        throw 'Package did not verify as Microsoft-signed. Do not ship it.'
    }
    Write-Host ''
    Write-Host "OK: every file is signed by the $msPublisher." -ForegroundColor Green
    Write-Host 'Next: install it with pnputil, load it, and re-run the three probe suites (plan doc, section 5).'
    return
}

# --------------------------------------------------------------- package mode
if (-not $SkipSign -and -not $Thumbprint -and -not $CertSubject) {
    throw 'Pass -Thumbprint (preferred) or -CertSubject to pick the EV certificate, or -SkipSign to build an unsigned CAB for inspection. Auto-selection is refused because this machine also holds a WDKTestCert.'
}
if ($root -like '\\*') {
    throw "The repo is on a UNC path ($root). MakeCab submissions must use a mapped drive letter; Partner Center rejects UNC-built CABs."
}

if ($Build) {
    Write-Host "Building $Configuration|$Platform"
    & (Join-Path $root 'build.ps1') -Configuration $Configuration -Platform $Platform
}

$bin = Join-Path $root "$name\$Platform\$Configuration"
$pkg = Join-Path $bin $name                     # inf + sys + cat, as the build lays it out
if (-not (Test-Path (Join-Path $pkg "$name.inf"))) {
    throw "No build output in $pkg. Run driver\build.ps1 (or pass -Build) first."
}

$out = Join-Path $root 'out\submission'
$stage = Join-Path $out $name
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null

Copy-Item (Join-Path $pkg "$name.inf") $stage -Force
Copy-Item (Join-Path $pkg "$name.sys") $stage -Force
# The .pdb is required: Microsoft's automated crash analysis uses it.
$pdb = Join-Path $bin "$name.pdb"
if (-not (Test-Path $pdb)) { throw "Symbols missing ($pdb). The submission has to carry the .pdb." }
Copy-Item $pdb $stage -Force

$stagedInf = Join-Path $stage "$name.inf"
if ($DriverVersion) {
    $stampinf = Get-KitTool 'stampinf.exe' 'x86'
    Write-Host "Stamping DriverVer version $DriverVersion"
    Invoke-Tool $stampinf @('-f', $stagedInf, '-d', '*', '-v', $DriverVersion) 'stampinf'
}
$driverVer = (Select-String -Path $stagedInf -Pattern '^\s*DriverVer\s*=' | Select-Object -First 1).Line.Trim()

# Rebuild the catalog: it hashes the INF, so a re-stamped INF invalidates the
# one the build produced. Microsoft regenerates the catalog anyway; the one in
# the CAB is used for company verification only.
$inf2cat = Get-KitTool 'Inf2Cat.exe' 'x86'
Write-Host 'Generating the catalog (Inf2Cat, 10_X64)'
Invoke-Tool $inf2cat @("/driver:$stage", '/os:10_X64', '/uselocaltime') 'Inf2Cat'

# ------------------------------------------------------------------- signing
$signtool = Get-KitTool 'signtool.exe' 'x64'
if ($SkipSign) {
    Write-Warning 'Signing skipped. The binaries still carry their WDKTestCert signatures; this CAB is for inspection only, do not submit it.'
} else {
    if ($Thumbprint) { $certArgs = @('/sha1', ($Thumbprint -replace '\s', '')) }
    else { $certArgs = @('/n', $CertSubject) }

    # SHA-256 only. Never add a SHA-1 pass here: the app installer dual-signs
    # for old Windows, a driver submission signed that way is rejected.
    foreach ($f in @((Join-Path $stage "$name.sys"), (Join-Path $stage "$name.cat"))) {
        Write-Host "Signing $(Split-Path $f -Leaf) with the EV certificate"
        Invoke-Tool $signtool (@('sign') + $certArgs + @('/fd', 'sha256', '/tr', $TimestampUrl, '/td', 'sha256', '/v', $f)) 'signtool sign'
        $sig = Get-AuthenticodeSignature $f
        if ($sig.SignerCertificate.Subject -like '*WDKTestCert*') {
            throw "Signed $f with the WDK test certificate. Pass the EV certificate's -Thumbprint."
        }
    }
}

# ----------------------------------------------------------------- the cabinet
# Every file sits under a subfolder; a CAB with files at its root is rejected.
# Folder names: fewer than 40 characters, no special characters, no UNC paths.
if ($name.Length -ge 40) { throw "CAB subfolder name '$name' is $($name.Length) characters; the limit is 39." }

$cabName = "$name-$Platform.cab"
$ddf = Join-Path $out "$name.ddf"
$ddfText = @"
;*** $name.ddf: attestation submission for the Nimbus Mouse Filter
;    Generated by driver\package.ps1. Do not edit by hand.
.OPTION EXPLICIT
.Set CabinetFileCountThreshold=0
.Set FolderFileCountThreshold=0
.Set FolderSizeThreshold=0
.Set MaxCabinetSize=0
.Set MaxDiskFileCount=0
.Set MaxDiskSize=0
.Set CompressionType=MSZIP
.Set Cabinet=on
.Set Compress=on
.Set CabinetNameTemplate=$cabName
.Set DiskDirectory1=$out
.Set DestinationDir=$name
"@
foreach ($f in @("$name.inf", "$name.sys", "$name.cat", "$name.pdb")) {
    $ddfText += "`r`n" + '"' + (Join-Path $stage $f) + '"'
}
Set-Content -Path $ddf -Value $ddfText -Encoding ASCII

Write-Host "Packing $cabName"
Push-Location $out
try {
    Invoke-Tool 'makecab.exe' @('/V1', '/f', $ddf) 'MakeCab'
} finally {
    Pop-Location
}
Remove-Item (Join-Path $out 'setup.inf'), (Join-Path $out 'setup.rpt') -Force -ErrorAction SilentlyContinue

$cab = Join-Path $out $cabName
if (-not (Test-Path $cab)) { throw "MakeCab did not produce $cab" }

if (-not $SkipSign) {
    Write-Host 'Signing the CAB with the EV certificate (SHA-256, RFC-3161)'
    Invoke-Tool $signtool (@('sign') + $certArgs + @('/fd', 'sha256', '/tr', $TimestampUrl, '/td', 'sha256', '/v', $cab)) 'signtool sign (cab)'
    Invoke-Tool $signtool @('verify', '/pa', '/v', $cab) 'signtool verify (cab)'
}

# ------------------------------------------------------------------- summary
Write-Host ''
Write-Host "Submission package: $cab"
Write-Host "  $driverVer"
Get-ChildItem $stage | ForEach-Object { "  {0,-26} {1,8} bytes" -f $_.Name, $_.Length }
Write-Host '  signatures:'
Get-ChildItem $stage -Include *.sys, *.cat -File -Recurse | ForEach-Object { Write-Host (Show-Signature $_.FullName) }
Write-Host (Show-Signature $cab)
Write-Host ("  SHA-256: {0}" -f (Get-FileHash $cab -Algorithm SHA256).Hash)
Write-Host ''
if ($SkipSign) {
    Write-Host 'Unsigned dry run. Re-run with -Thumbprint and the token plugged in to produce a submittable CAB.'
} else {
    Write-Host 'Next: Partner Center, Submit new hardware, upload this CAB, leave both test-signing options unchecked,'
    Write-Host 'request Windows 10/11 x64 signatures, then run this script with -VerifySigned on the download.'
    Write-Host 'The full runbook is in driver\SIGNING.md.'
}
