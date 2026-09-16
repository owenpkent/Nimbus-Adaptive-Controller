# Release signing the Nimbus Mouse Filter

How the driver goes from the test-signed dev build to something that loads on a
stranger's retail Windows 11 with Secure Boot on and anti-cheat running. The dev
loop (test signing, `install-dev.ps1`) is in [README.md](README.md); this file is
only about the release path.

**Status: nothing here has been done yet.** The Hardware Developer Program
account does not exist, no CAB has been submitted, and no signed build exists.
`package.ps1` is written and its unsigned dry run passes; every step below is
still ahead of us. Checked against Microsoft's docs on 2026-09-06.

## 1. What attestation signing is, and what it is not

Attestation signing takes a CAB of the driver package, signed with an EV
certificate registered to a Hardware Developer Program account, and hands back
the same driver signed by Microsoft (publisher: *Microsoft Windows Hardware
Compatibility Publisher*). No HLK testing, usually a same-day turnaround. That
signature is what lets the filter load on a retail machine with Secure Boot on
and test signing off, which is also the only configuration in which
EasyAntiCheat, BattlEye and Vanguard will start.

What it does not give us:

- **No Windows Update distribution.** Attestation-signed drivers cannot be
  published to Windows Update for retail audiences. Ours ships in the Nimbus
  installer, so this costs us nothing.
- **No Windows Server, ever.** Windows Server 2016 and later refuse attested
  device and filter driver submissions outright, and only load HLK-passed
  drivers. Out of scope for us.
- **No compatibility claim.** "When a driver receives attestation signing, it's
  not Windows Certified." Microsoft signs it; it does not vouch for it. Our own
  probe suites remain the only evidence the thing works.
- **Windows 10 Desktop and later only.** Fine: the client half needs Windows 10
  anyway.

### The 2026 policy picture, and the risk to plan around

Two separate changes get conflated constantly, so keep them apart:

- **The April 2026 Windows Driver Policy** removes default trust for drivers
  signed through the deprecated **cross-signed** root program on Windows 11
  24H2, 25H2, 26H1 and Server 2025, rolling out in evaluation mode first, with
  an allow list for a limited set of reputable cross-signed drivers. This is not
  about us. An attestation signature chains to a Microsoft CA, not to a
  cross-signed third-party root, so it is on the surviving side of that change.
  The evidence is on this machine: `ViGEmBus.sys` is signed by the *Microsoft
  Windows Hardware Compatibility Publisher* under *Microsoft Windows Third Party
  Component CA 2014*, and it keeps loading.
- **Microsoft's framing of attestation itself has moved.** The Driver Signing
  Options page (updated 2026-03-23) now files attestation under "Attestation
  signed drivers **for testing scenarios**" and opens with "For testing purposes
  only, you can submit your drivers for attestation signing". The listed
  restrictions are still only the ones above (no retail Windows Update, no
  Server, no certification claim); nothing published says attestation-signed
  drivers stop loading, and no end date exists. But the direction is
  unmistakable, and every public statement now points at WHCP as the path for
  drivers "intended for general Windows ecosystem use".

So: attestation is the right move now, and it is not a destination. Treat the
signed build as good for this release cycle, not as settled infrastructure.
The fallback if attestation is withdrawn is WHCP with HLK testing, which is a
different order of work (a test rig, a playlist run, a real submission). Worth
watching: Microsoft expanded HLK/DevFund coverage for software and filter
drivers in the 26H1 HLK (from 22 to 41 applicable tests), though that work is
aimed mostly at file system minifilters rather than mouse class filters. Read
that blog before assuming our filter has an HLK path.

## 2. Prerequisites, in order

1. **A company identity.** Registration is an organization flow: company
   details or a D-U-N-S number, a legal contact with authority to sign
   agreements who can receive external email from Microsoft, and a questionnaire
   that arrives afterwards. Registration does not proceed until the legal
   contact answers, so use a mailbox that is actually read.
2. **A Microsoft Entra ID tenant, and sign in as its global administrator.**
   One can be created for free during registration. Approval emails and the
   questionnaire go to the global administrator mailbox.
3. **The EV certificate.** We already hold one on the SafeNet token, the same
   one `build_tools/sign_exe.bat` uses for the app installer. It is uploaded
   once on the Manage certificates page to get the account approved, and used
   again to sign each submission CAB. Two things to confirm before starting:
   the certificate is valid at the time of submission, and the subject matches
   the company registered on the account. Note that code signing certificate
   lifetimes were cut to one year for certificates issued from 15 February 2026;
   check the renewal terms with the CA, because an expired certificate blocks
   submissions even though already-signed drivers keep working.
4. **Agreements.** The Code Signing Agreement, Windows Hardware Compatibility
   Agreement, Microsoft Marks License Agreement and Windows Analytics Agreement
   are accepted during registration. The Code Signing Agreement is the one that
   matters here: it makes us responsible for what we submit, which is why we
   submit only our own code (see section 7).

Registration entry point: the Hardware Developer Program enrollment in Partner
Center. Budget days to weeks for the questionnaire and approval, not hours, and
start it before the signed build is on the critical path.

## 3. Build the submission

```powershell
driver\build.ps1                                          # Release x64
driver\package.ps1 -SkipSign -DriverVersion 1.0.0.5       # dry run, no token needed
driver\package.ps1 -DriverVersion 1.0.0.5 -Thumbprint <EV cert thumbprint>
```

`package.ps1` stages `inf` + `sys` + `pdb`, re-stamps `DriverVer`, regenerates
the catalog with Inf2Cat so it matches the staged INF, re-signs the `.sys` and
`.cat` with the EV certificate (replacing the WDKTestCert signatures the build
applies), packs everything into `driver\out\submission\nimbus_moufilter-x64.cab`
under a `nimbus_moufilter\` folder, signs the CAB and verifies it.

The rules it enforces, each of which is a silent rejection otherwise:

| Rule | Why |
|---|---|
| Every file in a subfolder, never at the CAB root | Dashboard requirement. |
| Folder name under 40 characters, no special characters | Dashboard requirement. `nimbus_moufilter` is 16. |
| No UNC paths anywhere in the DDF | "You must use a mapped drive letter for the CAB to be valid." |
| `.pdb` included | Required for Microsoft's automated crash analysis. |
| `.cat` included | Used for company verification only. Microsoft regenerates it and replaces ours. |
| CAB signed SHA-256 only, RFC-3161 timestamp | **Do not reuse `build_tools\sign_exe.bat` here.** That script dual-signs SHA-1 then SHA-256 for the app installer. A driver submission signed that way is rejected. |
| Explicit certificate selection | This machine holds a WDKTestCert. `package.ps1` refuses `/a` auto-selection and fails if the resulting signer is the test certificate. |

**Architecture.** x64 only for the first submission. Each driver folder in a CAB
must support the same set of architectures, and the INF is generated per
architecture from the `.inx` (`NT$ARCH$` becomes `NTamd64`), so an ARM64 build
produces a second, different INF of the same name. Adding ARM64 later means
either a second submission or reworking the `.inx` into one INF carrying both
`NTamd64` and `NTarm64` sections. Nothing in the driver has ever run on ARM64,
so this is not the release to find out.

**DriverVer.** The build stamps the current date and time. Pass an explicit
`-DriverVersion` for anything submitted: Windows picks the higher `DriverVer`
when a package is replaced, so the number needs to be ours and monotonic. The
convention to start with is `1.0.0.<interface version>`, currently `1.0.0.5`.

## 4. Submit

1. Partner Center hardware dashboard, **Submit new hardware**.
2. Product name: `Nimbus Mouse Filter <version>`. It is visible if the driver is
   ever shared with another company, so keep it clean.
3. Upload the signed CAB.
4. **Leave both test-signing options unchecked.** Checking them produces a build
   that only loads on test-signing machines, which defeats the entire exercise.
5. Requested signatures: Windows 10 and Windows 11 x64. Not Server (it is
   refused for filter drivers regardless).
6. Submit, then download the signed package when it completes.

## 5. Verify what comes back, before it goes anywhere near a user

```powershell
driver\package.ps1 -VerifySigned <path to the extracted download>
```

This fails unless every binary is `Valid` and signed by the *Microsoft Windows
Hardware Compatibility Publisher*. A returned package that still shows our own
certificate, or the WDKTestCert, is the wrong download.

Then, on a machine with **test signing off and Secure Boot on**:

1. Install with `pnputil /add-driver nimbus_moufilter.inf /install` (the INF is
   a primitive `DefaultInstall` package, so pnputil runs the install), add the
   class `UpperFilters` entry, restart the mice. `install-dev.ps1` is not that
   path: it copies the `.sys` and creates the service with `sc.exe`, and it
   deliberately refuses to run against a Driver Store install. A release
   installer script is still to be written; see the plan doc, section 6, item 4.
2. Confirm the driver loads with no Code Integrity event 3004 and no test-mode
   watermark.
3. Re-run all three suites against the signed build: the attended probe, the
   battle test, and the real-app relay harness (plan doc, section 5). A signed
   build is a different binary layout; do not assume the dev results carry over.
4. Only then, the test the dev machine has never been able to run: an anti-cheat
   title (Elden Ring) with the filter loaded and Game Mode isolating.

## 6. Then, and only then, ship it

The signed driver is not the release. Still needed before it reaches users: the
installer work (driver install, class filter registration with rollback, mouse
restart, clean uninstall), the Code Integrity 3076/3077 diagnostics, and the
disclosure step in the plan doc, section 6, item 5. Publishing the driver's name
and purpose, and opening the anti-cheat conversation, comes before the first
release that carries it, not after.

## 7. What we do not sign: ViGEmBus

Do not put ViGEmBus, or any repackaged build of it, in a Nimbus submission.
It is not our code, the Code Signing Agreement makes us answerable for what we
submit, and a re-signed fork would collide with the ViGEmBus installs that
DS4Windows and every other input mapper share (same service, same device
interface, and a version race in both directions).

It also does not need us. `ViGEmBus.sys` is already signed by the *Microsoft
Windows Hardware Compatibility Publisher*, so the April 2026 cross-signing
change does not touch it, and a signature stays valid past the signing
certificate's expiry because it is timestamped.

The real gap is a packaging one, and it belongs with the installer work rather
than here. Three facts, all checked on 2026-09-06:

- `pip install vgamepad` silently installs **ViGEmBus 1.17.333.0** from a
  bundled MSI, which is what this dev machine was running. (Since 2026-09-09
  neither holds: the app speaks the bus protocol itself through
  `src/padbus_client.py` and `vgamepad` is gone from `requirements.txt`, and
  this machine runs the installer's build, `ViGEmBus.sys` 1.21.442.0.)
- The last and final release is **1.22.0** (2023-11-02). The project was
  archived that day after a trademark dispute; it is BSD-3-Clause and ships an
  all-in-one signed setup.
- The installer's driver page had never installed anything. It downloaded
  `ViGEmBus_Setup_1.22.0.exe`, which 404s (the asset is
  `ViGEmBus_1.22.0_x64_x86_arm64.exe`), with `NSISdl::download`, which speaks
  plain HTTP and cannot fetch an `https://` URL at all. The vJoy download two
  blocks above it was broken the same two ways.

Fixed on 2026-09-06, as installer work rather than signing work: both setups are
now bundled and verified at build time by `build_tools/fetch_redist.ps1`, the
installer installs them silently, and it re-detects each driver afterwards
instead of trusting an exit code. See
[docs/setup/PACKAGING.md](../docs/setup/PACKAGING.md).

That is also where the mouse filter joins once it is signed: the same page, the
same detect-install-verify shape, plus the class filter registration and its
rollback. Nothing about the filter goes in there until the download from
Partner Center verifies. Writing our own
virtual gamepad bus driver is a much larger project than this filter and is not
on the table now, but it is the thing to reconsider if attestation signing ever
closes and we end up building an HLK pipeline anyway.

**Since 2026-09-09 there is a plan for that reconsideration:**
[docs/vision/PAD_BUS_FORK_PLAN.md](../docs/vision/PAD_BUS_FORK_PLAN.md) works
out what forking ViGEmBus would take, and it argues against this section on two
of its three points. The collision argument is answered by a rename and new
GUIDs (its section 8). The "not our code" and Code Signing Agreement arguments
are accepted, and become the reason it makes hardening, static analysis and
Driver Verifier mandatory before any submission: we would have to actually own
the code, not merely rename it. Nothing changes here until that plan is
accepted, and it is gated on an anti-cheat question that has not been asked
yet. If it is accepted, this section gets rewritten rather than deleted.

## Sources

- [Attestation sign Windows drivers](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/code-signing-attestation)
- [Driver signing options](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/driver-signing-offerings)
- [Driver code signing requirements](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/code-signing-reqs)
- [Register for the Windows Hardware Developer Program](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/hardware-program-register)
- [Advancing Windows driver security: removing trust for the cross-signed driver program](https://techcommunity.microsoft.com/blog/windows-itpro-blog/advancing-windows-driver-security-removing-trust-for-the-cross-signed-driver-pro/4504818)
- [The Windows Driver Policy](https://support.microsoft.com/en-us/windows/hardware/drivers/the-windows-driver-policy)
- [HLK testing updates for software and filter drivers](https://techcommunity.microsoft.com/blog/windowsdriverdev/hlk-testing-updates-for-software-and-filter-drivers/4495781)
- [ViGEmBus end-of-life statement](https://docs.nefarius.at/projects/ViGEm/End-of-Life/) and [releases](https://github.com/nefarius/ViGEmBus/releases)
