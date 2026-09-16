<#
.SYNOPSIS
    Read-only health check for the Nimbus Mouse Filter dev install. Answers
    one question: would the mouse survive the next reboot?

.DESCRIPTION
    A class upper filter is mandatory once listed. install-dev.ps1 verifies
    that at install time and rolls back if the driver does not load, but
    nothing stops the machine from drifting out from under a filter that
    installed cleanly. Anything that stops Code Integrity accepting the
    test-signed .sys leaves the filter registered and unloadable, and the
    mouse does not start at the next boot:

      * a Windows update that resets boot configuration
      * "bcdedit /set testsigning off" (what you do to play an anti-cheat game)
      * Secure Boot switched back on in firmware
      * a rebuild signed with a certificate no longer in the machine stores

    Run this before rebooting, before turning test signing off, and after
    Windows updates. It needs no elevation and changes nothing.

    Exit codes:
      0  safe (filter not installed, or installed and loadable)
      1  ARMED AND UNLOADABLE: the filter is registered but would not load.
         The mouse dies at the next mouse restart or reboot. Fix it now.
      2  the mouse is already broken by the filter.
#>
[CmdletBinding()]
param(
    [string]$Sys = (Join-Path $env:SystemRoot 'System32\drivers\nimbus_moufilter.sys')
)

$ErrorActionPreference = 'Continue'
$service = 'nimbus_moufilter'
$classKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4D36E96F-E325-11CE-BFC1-08002BE10318}'

function Test-TestSigningActive {
    # Code Integrity's view of the *running* boot. "bcdedit /enum" shows the
    # stored setting, which is what the next boot will use; the two disagree
    # exactly when it matters. Kept in step with install-dev.ps1.
    #   0x1 CODEINTEGRITY_OPTION_ENABLED    set on every normal boot
    #   0x2 CODEINTEGRITY_OPTION_TESTSIGN   what this is about
    try {
        if (-not ('Nimbus.CodeIntegrityCheck' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Nimbus {
    public static class CodeIntegrityCheck {
        [StructLayout(LayoutKind.Sequential)]
        public struct Info { public uint Length; public uint Options; }
        [DllImport("ntdll.dll")]
        static extern int NtQuerySystemInformation(int infoClass, ref Info info, int length, out int returnLength);
        public static uint Options() {
            Info i = new Info(); i.Length = 8; int rl;
            int st = NtQuerySystemInformation(103, ref i, 8, out rl);
            if (st != 0) throw new InvalidOperationException("NtQuerySystemInformation failed 0x" + st.ToString("X8"));
            return i.Options;
        }
    }
}
'@
        }
        $options = [Nimbus.CodeIntegrityCheck]::Options()
        return [pscustomobject]@{
            Known    = $true
            Options  = $options
            TestSign = (($options -band 0x2) -ne 0)
            CiOff    = (($options -band 0x1) -eq 0)
        }
    } catch {
        return [pscustomobject]@{ Known = $false; Options = 0; TestSign = $false; CiOff = $false }
    }
}

Write-Host '=== Nimbus Mouse Filter health ==='

$filters = (Get-ItemProperty -Path $classKey -Name UpperFilters -ErrorAction SilentlyContinue).UpperFilters
$filters = if ($filters) { @($filters) } else { @() }
$listed = $filters -contains $service
Write-Host "Mouse class UpperFilters : $(if ($filters) { $filters -join ', ' } else { '(none)' })"
Write-Host "Filter registered        : $listed"

$drv = Get-CimInstance Win32_SystemDriver -Filter "Name='$service'" -ErrorAction SilentlyContinue
$state = if ($drv) { $drv.State } else { 'not registered' }
Write-Host "Driver service           : $state"

$ci = Test-TestSigningActive
if ($ci.Known) {
    Write-Host ("Code Integrity options   : 0x{0:X8} (test signing this boot: {1})" -f $ci.Options, $ci.TestSign)
} else {
    Write-Host 'Code Integrity options   : could not query'
}
$stored = $null
try { $stored = (bcdedit /enum '{current}' 2>$null | Select-String 'testsigning') -join ' ' } catch { }
if ($stored) { Write-Host "bcdedit stored setting   : $($stored.Trim()) (applies to the NEXT boot)" }

$msSigned = $false
$sigStatus = 'no file'
if (Test-Path $Sys) {
    $sig = Get-AuthenticodeSignature $Sys
    $sigStatus = $sig.Status
    $msSigned = [bool]($sig.SignerCertificate -and $sig.SignerCertificate.Subject -like '*Microsoft Windows*')
    Write-Host "Driver file              : $Sys"
    Write-Host "Signature                : $sigStatus$(if ($msSigned) { ' (Microsoft-signed)' })"
} else {
    Write-Host "Driver file              : not present ($Sys)"
}

$mice = @(Get-PnpDevice -Class Mouse -PresentOnly -ErrorAction SilentlyContinue)
$bad = @($mice | Where-Object { $_.Status -ne 'OK' })
Write-Host "Mice present             : $($mice.Count) ($($bad.Count) with problems)"
foreach ($m in $bad) {
    $code = ''
    try { $code = " problem code $(($m | Get-PnpDeviceProperty -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction Stop).Data)" } catch { }
    Write-Warning "  [$($m.Status)] $($m.FriendlyName)$code"
}

$guard = Get-ScheduledTask -TaskName 'NimbusMouseFilterGuard' -ErrorAction SilentlyContinue
if ($guard) {
    $guardText = $guard.State
} else {
    # The task runs as SYSTEM, so from a standard shell Get-ScheduledTask
    # cannot read it and returns nothing, which is not the same as absent.
    # schtasks tells the two apart: "Access is denied" means the task exists
    # and this shell may not read it, "cannot find" means it is not there.
    # cmd merges its stderr so PowerShell 5.1 does not wrap it in errors.
    $probe = cmd /c 'schtasks /query /tn NimbusMouseFilterGuard 2>&1' | Out-String
    if ($probe -match 'Access is denied') { $guardText = 'registered (run elevated to see its state)' }
    else { $guardText = 'not registered' }
}
Write-Host "Boot guard task          : $guardText"

$recent = Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-PnP'; Id=219} -MaxEvents 1 -ErrorAction SilentlyContinue
if ($recent -and $recent.Message -match 'nimbus_moufilter') {
    Write-Host "Last load failure        : $($recent.TimeCreated)"
    ($recent.Message -split "`n" | Where-Object { $_ -match 'Status:' }) | ForEach-Object { Write-Host "  $($_.Trim())" }
}

Write-Host ''
$wouldLoad = $msSigned -or $ci.TestSign -or $ci.CiOff

if (-not $listed) {
    Write-Host 'SAFE: the filter is not registered as a mouse class filter, so it cannot stop the mouse.'
    if ($drv) { Write-Host 'The service and .sys are still installed; install-dev.ps1 re-arms it.' }
    exit 0
}

if ($bad.Count -gt 0 -and $bad.Count -eq $mice.Count -and $state -ne 'Running') {
    Write-Warning 'BROKEN NOW: the filter is registered, it is not running, and no mouse is starting.'
    Write-Warning 'Fix:  elevated  driver\recover-mouse.ps1'
    exit 2
}

if (-not $wouldLoad) {
    Write-Warning 'ARMED AND UNLOADABLE: the filter is registered but Code Integrity will not load it in this boot.'
    Write-Warning 'The mouse stops at the next mouse restart or reboot.'
    Write-Warning 'Fix now, either way:'
    Write-Warning '  detach the filter :  elevated  driver\recover-mouse.ps1'
    Write-Warning '  or re-arm signing :  elevated  driver\enable-testsigning.ps1  then reboot'
    exit 1
}

if ($sigStatus -ne 'Valid' -and -not $ci.CiOff) {
    Write-Warning "ARMED AND UNLOADABLE: the driver's signature does not verify ($sigStatus), so it will not load."
    Write-Warning 'Fix:  elevated  driver\recover-mouse.ps1   (or rebuild and re-run install-dev.ps1)'
    exit 1
}

Write-Host 'SAFE: the filter is registered and loadable.'
if ($state -ne 'Running' -and $mice.Count -gt 0) {
    Write-Host 'It is not running right now, which is normal when no mouse has started since the last restart.'
}
exit 0
