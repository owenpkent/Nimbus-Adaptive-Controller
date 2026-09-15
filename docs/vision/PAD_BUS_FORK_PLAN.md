# Pad Bus Fork Plan

Forking and modernizing **ViGEmBus** into a Nimbus-owned virtual gamepad bus
driver, and what has to be true before that is a good idea.

**Status:** phase 0 built and measured on 2026-09-09 (section 17); nothing
past it is built or decided. This is the decision document plus the work
breakdown behind it. Everything below was checked against upstream sources
and a fresh clone of `nefarius/ViGEmBus@master` on 2026-09-09.

**Related docs:** [VDROID_DRIVER_BRAINSTORM.md](../architecture/VDROID_DRIVER_BRAINSTORM.md)
(this plan is that document's Option C, carried out, and it corrects one of its
premises in section 10). [driver/SIGNING.md](../../driver/SIGNING.md) section 7
is the standing position this plan argues against. The build, test and signing
machinery all reuse patterns already proven by
[WINDOWS_MOUSE_FILTER_PLAN.md](WINDOWS_MOUSE_FILTER_PLAN.md).
[docs/setup/PACKAGING.md](../setup/PACKAGING.md) owns the installer side.

**The recommendation in one paragraph.** Phase 0 is done (2026-09-09,
section 17): `vgamepad` is replaced by a pure-Python client that speaks the
bus protocol directly. It removed a dependency that ran an MSI at pip time and
the CI workaround, took a day, carried zero kernel risk, and found a silent
dead-pad bug in the library it replaced on the way. Do phases 1
and 2 (fork, modernize, build, test-sign, validate on the game harness) as a
funded side project, because upstream is archived and unmaintained and our
entire XInput path currently rests on a binary nobody will fix. Do **not**
attestation-sign and ship it until the anti-cheat question in section 13 is
answered with evidence, because a brand new bus driver with zero reputation can
make our users worse off than the widely-tolerated ViGEmBus does today. Design
the client so it speaks to either driver, and the last step stops being a
migration and becomes a runtime switch.

---

## 1. What we depend on today, exactly

| Thing | Value |
|---|---|
| Python package | `vgamepad>=0.1.0` in `requirements.txt`, used only by `src/vigem_interface.py` |
| Driver installed by pip | ViGEmBus **1.17.333.0**, from an MSI that `vgamepad`'s `setup.py` runs at install time |
| Driver bundled by our installer | **ViGEmBus 1.22.0** (`ViGEmBus_1.22.0_x64_x86_arm64.exe`), fetched by `build_tools/fetch_redist.ps1` |
| Client library | `ViGEmClient.dll`, shipped inside the `vgamepad` wheel |
| API surface we actually use | `VX360Gamepad`, `left_joystick_float`, `right_joystick_float`, `left_trigger_float`, `right_trigger_float`, `press_button`, `release_button`, `update`, `reset`, `XUSB_BUTTON` |
| What the app needs from it | One Xbox 360 pad: 2 sticks, 2 triggers, 14 buttons. Nothing else. No rumble, no LED index, no DS4. |

The whole dependency is one file. `grep -l "vg\."` finds only
[src/vigem_interface.py](../../src/vigem_interface.py), and the bridge talks to
it through `set_left_stick` / `set_right_stick` / `set_left_trigger` /
`set_right_trigger` / `set_button` / `update_axis`. That is the entire contract
a replacement has to honor.

## 2. What is wrong with that

1. **The upstream driver is abandoned.** Archived 2023-11-02, last release
   v1.22.0 the same day, no successor shipped (section 3). If a Windows update
   breaks it, or Microsoft blocklists it, nobody upstream will fix it and we
   have no build we can patch.
2. **Two different versions are in play.** A developer machine that ran
   `pip install vgamepad` has 1.17.333.0. A machine that ran our installer has
   1.22.0. They race in both directions, and we have never tested which one a
   user ends up with.
3. **`pip install` runs a kernel driver installer.** That is why
   [.github/workflows/fast-tests.yml](../../.github/workflows/fast-tests.yml)
   has to filter `vgamepad` out of `requirements.txt` before installing. CI
   works around a design flaw in a dependency.
4. **We ship a third-party bootstrapper we cannot rebuild.** `ViGEmBus_1.22.0_..._.exe`
   is an Advanced Installer package (commercial tooling) built by someone else
   from a repo whose CI account we do not control.
5. **No kernel failsafe.** If Nimbus hangs with a stick deflected, ViGEmBus
   keeps reporting the last submitted report until the file handle closes. Our
   mouse filter solved exactly this class of problem with a heartbeat
   (interface v4, 250 ms ticks). The pad has no equivalent, and a stuck stick
   in a game is the same category of accessibility failure as a stuck mouse.

6. **The client library hides dead pads.** Found while building phase 0
   (section 17): `ViGEmClient`'s `vigem_target_x360_update` returns
   `VIGEM_ERROR_NONE` for every failed report submit except access denied.
   The bus has a replug gap in which reports fail with
   `ERROR_NO_MORE_ITEMS`, and under vgamepad those failures were swallowed,
   so a pad could go silently dead after a replug and Nimbus would keep
   reporting it connected. Our client surfaces and heals it; the fork's
   IOCTL audit (section 9, item 6) should cover the client contract too.

None of these is breaking anything today. Item 1 is the one that is a matter of
when rather than whether; item 6 was breaking things silently.

## 3. Upstream, as of 2026-09-09

| Fact | Value |
|---|---|
| Repo | `nefarius/ViGEmBus`, **archived** 2023-11-02, 4,232 stars, 403 forks |
| Reason | Trademark conflict with ViGEM GmbH, settled by agreement. All use of the phrase "ViGEm" was retired. |
| Last release | v1.22.0, 2023-11-02 |
| Driver license | **BSD-3-Clause**, "Copyright (c) 2016-2020, Nefarius Software Solutions e.U." (INF header says 2018-2022 "and Contributors") |
| Client license | `nefarius/ViGEmClient`, **MIT**, Copyright (c) 2018 Benjamin Höglinger-Stelzer |
| Successor | "VirtualPad" announced, nothing public. Nefarius is actively pushing to other repos (`DsHidMini`, `MultiPadTester` both touched 2026-09-09), so this is a deliberate non-revival, not neglect. |
| Viable community fork | **None.** The most-starred forks have 1 to 3 stars. `awalol` is 2 commits ahead on an audio experiment marked "bad stability"; `DerGoogler` is 28 commits ahead with messages like "bvc" and ".fg,,"; the rest are unchanged mirrors. |

So there is no shoulder to stand on. Anyone who wants a maintained version of
this driver has to become its maintainer.

**Licensing consequences of BSD-3-Clause**, which are mild and worth stating
plainly: we may fork, modify, relicense-in-combination and ship binaries,
commercially, provided we retain the copyright notice and the disclaimer in
source and in binary distribution (so: keep `LICENSE`, keep the per-file
headers, add ours alongside), and clause 3 forbids using the copyright holder's
or contributors' names to endorse the fork. Combined with the ViGEM GmbH
trademark, that settles the naming question: the fork cannot be called ViGEm
anything, and cannot say "by Nefarius" or imply endorsement. It can and must
say "derived from ViGEmBus, BSD-3-Clause, copyright Nefarius Software Solutions
e.U." in the notices.

## 4. What the driver actually does

Worth understanding before deciding, because the interesting part is not the
part people assume.

It is a **root-enumerated KMDF bus driver** (`Class=System`, hardware ID
`Nefarius\ViGEmBus\Gen1`) whose FDO exposes a device interface to user mode and
whose child PDOs pretend to be USB devices:

```
user mode  ──IOCTL──▶  bus FDO (ViGEmBus.sys)
                          │  creates child PDO with hardware ID
                          │  USB\VID_045E&PID_028E  +  USB\MS_COMP_XUSB10
                          ▼
                       xusb22.sys  (Microsoft's inbox Xbox 360 driver)
                          ▼
                       XInput1_4.dll  ──▶  the game
```

The PDO does not merely claim a USB hardware ID. It **emulates a USB device
stack**: `EmulationTargetPDO.cpp` handles `IOCTL_INTERNAL_USB_SUBMIT_URB` and
services `URB_FUNCTION_CONTROL_TRANSFER`, `URB_FUNCTION_SELECT_CONFIGURATION`,
`URB_FUNCTION_SELECT_INTERFACE` and `URB_FUNCTION_BULK_OR_INTERRUPT_TRANSFER`,
returning hand-written device, configuration and string descriptors, and
parking the interrupt-IN transfer that `xusb22.sys` issues until user mode
submits a report.

**This is why the driver cannot be replaced with something simpler.** XInput
only enumerates devices bound to `xusb22.sys`, and `xusb22.sys` binds to the
USB compatible ID `USB\MS_COMP_XUSB10`. A HID gamepad, a VHF source driver, a
UMDF HID minidriver: none of them are visible to `XInput1_4.dll`, no matter how
correct their report descriptor is. USB emulation is the price of XInput, and
the ~1,500 lines that implement it are the real asset in this repository.

Size, by the numbers (`sys/`, 6,484 lines of C++ total):

| Part | Lines | Note |
|---|---|---|
| `EmulationTargetPDO.cpp/.hpp` | 1,542 | The USB stack emulation. Shared. |
| `Ds4Pdo.cpp/.hpp` | 1,547 | DualShock 4 target. **We need none of it.** |
| `XusbPdo.cpp/.hpp` | 1,307 | Xbox 360 target. The one we use. |
| `Driver.cpp`, `Queue.cpp`, `busenum.cpp`, `buspdo.cpp`, headers | 2,088 | Bus lifecycle, IOCTL dispatch, child list, file-object sessions. |

Six and a half thousand lines is a weekend of reading, not a research project.
For comparison, `driver/nimbus_moufilter/nimbus_moufilter.c` is a fraction of
that and we already own and operate it.

**The user-mode protocol** (`sdk/include/ViGEm/km/BusShared.h`, MIT) is ten
buffered IOCTLs on `FILE_DEVICE_BUS_EXTENDER` (0x2a), base function 0x801. Six
matter to us:

| IOCTL | Code | Payload |
|---|---|---|
| `IOCTL_VIGEM_CHECK_VERSION` | `0x002AA00C` | `{ULONG Size; ULONG Version;}`, Version = 0x0001 |
| `IOCTL_VIGEM_PLUGIN_TARGET` | `0x002AA004` | `{ULONG Size; ULONG SerialNo; ULONG TargetType; USHORT VendorId; USHORT ProductId;}` |
| `IOCTL_VIGEM_WAIT_DEVICE_READY` | `0x002AA010` | `{ULONG Size; ULONG SerialNo;}` |
| `IOCTL_XUSB_SUBMIT_REPORT` | `0x002AA808` | `{ULONG Size; ULONG SerialNo; XUSB_REPORT Report;}` |
| `IOCTL_XUSB_REQUEST_NOTIFICATION` | `0x002AE804` | in/out, returns `{... UCHAR LargeMotor; UCHAR SmallMotor; UCHAR LedNumber;}`, pends until rumble |
| `IOCTL_VIGEM_UNPLUG_TARGET` | `0x002AA008` | `{ULONG Size; ULONG SerialNo;}` |

`XUSB_REPORT` is 12 bytes: `USHORT wButtons; BYTE bLeftTrigger; BYTE
bRightTrigger; SHORT sThumbLX, sThumbLY, sThumbRX, sThumbRY`. Target type
`Xbox360Wired = 0`. Devices are owned by the file handle, so closing it (or
dying) unplugs the pad. The codes above are derived from the header's
`CTL_CODE` macros; the client should compute them the same way rather than
hardcode them.

That is the entire thing. A Python client is an afternoon, not a project.

## 5. The decision

| Option | What it means | Verdict |
|---|---|---|
| **A. Stay exactly as we are** | Keep `vgamepad` and both bundled ViGEmBus builds | Rejected. Two driver versions, an MSI at pip time, and a CI workaround, all for a driver nobody maintains. |
| **B. Client only** | Drop `vgamepad`, talk to stock ViGEmBus ourselves | **Do this now.** Fixes items 2, 3 in section 2 for a few days of work and no kernel risk. Does not fix item 1. |
| **C. Fork the bus driver** | Rename, modernize, build, sign, ship it ourselves | **Plan for this, gated.** The only option that fixes the abandonment risk and the only one that can add a kernel failsafe. Costs a signature, a support surface, and reputation risk (section 13). |
| **D. Write a new HID driver (VHF or UMDF)** | Start from `vhidmini2` or VHF, no USB emulation | Rejected as a replacement. **It cannot do XInput** (section 4), which is the entire reason we use ViGEm. Viable only as an addition, and section 10 shows the fork already contains a cheaper route to the same thing. |
| **E. Multi-device vJoy** | The old Option E, more vJoy devices | Orthogonal, and separately weakened by the DirectInput ceiling in section 10. Does not touch XInput at all. |

**Take B now, C on a gate.** The gate is section 13.

The thing that makes this safe is that B and C share a client. If the Python
client is written against the protocol rather than against a DLL, then
supporting our own bus later is "add a second device interface GUID to probe
for", and the app can prefer ours, fall back to stock ViGEmBus, and be honest
in the UI about which one it found. There is never a flag day.

## 6. Phase 0: the client, before any driver work

Replace `vgamepad` with `src/padbus_client.py`: pure `ctypes`, no wheel, no
bundled DLL, no MSI.

**What it does**

*Built 2026-09-09; the list below is the spec, with what changed in building
it marked. Section 17 has the measurements.*

1. Find the bus. `SetupDiGetClassDevs` + `SetupDiEnumDeviceInterfaces` +
   `SetupDiGetDeviceInterfaceDetail` on the interface GUID, then `CreateFileW`
   with `FILE_FLAG_OVERLAPPED | FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH`,
   which is what `ViGEmClient.cpp` does. Probe our own GUID first, then
   ViGEmBus's `{96E42B22-F5E9-42F8-B043-ED0F932F014F}`. *As built: the
   device interface list comes from cfgmgr32 (`CM_Get_Device_Interface_ListW`,
   the same result in a tenth of the code), and the handle is opened with
   `FILE_FLAG_OVERLAPPED` alone; the two buffering flags mean nothing on a
   device IOCTL path.*
2. `CHECK_VERSION`, then `PLUGIN_TARGET` with a random serial, then
   `WAIT_DEVICE_READY`. *As built: not a random serial. Every distinct serial
   leaves a phantom devnode behind, so the client walks 1 to 16 like
   ViGEmClient, skips serials whose child device is still present, and proves
   readiness with real reports, because the ready wait can complete on a
   child that is being removed (section 17, finding 1).*
3. `SUBMIT_REPORT` on every update, from a 12-byte `ctypes.Structure`.
4. Optional: a daemon thread parked on `REQUEST_NOTIFICATION` for rumble and
   the LED slot index. We do not use rumble today, but `LedNumber` is the
   XInput user index, which is genuinely useful for the game harness (it tells
   us whether our pad is player one, which
   [docs/vision/GAME_TEST_HARNESS.md](GAME_TEST_HARNESS.md) currently assumes).
5. Close the handle on shutdown. The driver unplugs the pad for us.

**Rules it must follow**

- Same graceful-degradation pattern as every other Windows module:
  `PADBUS_AVAILABLE` is True only when the interface is actually present, and
  the import lives behind `try/except` so the app starts on a machine with no
  driver and on non-Windows.
- Keep `ViGEmInterface`'s public methods byte-for-byte compatible. The bridge
  and both backends' behavior compatibility rule in `CLAUDE.md` do not change.
- One `update()` per submitted report, exactly as now. Do not batch or
  rate-limit here; shaping stays in the bridge.

**Done when:** `requirements.txt` has no `vgamepad`, the CI workflow's
filtering step is deleted, `tests/probe_stick_shaping_windows.py` passes with
its existing expected values, and
`tests/probe_game_harness_windows.py --game left4dead2 --actuator nimbus`
passes inside its existing `expect` bands. If the harness numbers move, the
client is wrong, and that is exactly the signal we built the harness to give.

**Effort:** 2 to 4 days including the probe work; it took one, most of it on
the replug gap. **Risk:** low, and fully
reversible (keep `vigem_interface.py` on the `vgamepad` path behind a config
flag for one release).

## 7. The new repo

**Yes, a separate repo.** The mouse filter lives in `driver/` inside this repo
because we wrote it and it is useless outside Nimbus. This is the opposite
case: it is someone else's history under a different license that we want to
keep merging from, and it is useful to every input mapper on Windows. Vendoring
6,500 lines of BSD-3-Clause C++ into `driver/` would tangle our license
notices, bloat our clone, and throw away the ability to `git log upstream/master`.

| Decision | Value |
|---|---|
| Name | **`nimbus-padbus`**, product name "Nimbus Pad Bus". Consistent with `nimbus_moufilter`. Contains no "ViGEm", per the trademark settlement and BSD clause 3. |
| Visibility | Public from day one. It is a derivative of a public BSD-3 project and its whole value to the community is being visible and maintained. |
| License | BSD-3-Clause, retaining Nefarius's copyright and adding ours. `NOTICE.md` states the lineage explicitly. |
| Bootstrap | `git clone` upstream, `git remote rename origin upstream`, then push to the new remote. Keeps all 1,300 upstream commits and blame. |
| Consumed by Nimbus as | Release artifacts (signed `.sys` + `.inf` + `.cat`) fetched by `build_tools/fetch_redist.ps1`, exactly as ViGEmBus is today. **Not** a submodule: our installer needs a signed binary, not source. |

Proposed layout, mirroring what we already know works in `driver/`:

```
nimbus-padbus/
  sys/                      the driver (renamed from ViGEmBus's sys/)
  include/nimbus/padbus.h   the user/kernel contract, the one shared header
  build.ps1                 VS2022 + WDK 26100, Release x64, outputs to out/
  package.ps1               CAB for Partner Center, and verify what comes back
  install-dev.ps1           test-signed dev install / uninstall
  tests/                    the probes (section 12)
  docs/PROTOCOL.md          the IOCTL contract, versioned
  NOTICE.md  LICENSE  README.md
```

`build.ps1` and `package.ps1` are near-copies of ours. That is the point:
[driver/SIGNING.md](../../driver/SIGNING.md) already encodes every CAB rule
that causes a silent Partner Center rejection, and none of it has to be
rediscovered.

**What we delete on day one:** `build/` (NUKE, a .NET build system for a C++
driver), `build.ps1`/`build.sh`/`build.cmd` (its bootstrappers), `appveyor.yml`
and `stage0.ps1` (CI that downloads artifacts from Nefarius's AppVeyor account
with his token), `setup/ViGEmBus.aip` (Advanced Installer, commercial tooling
we do not own), `patches/dmf.diff` (647 lines whose entire purpose is pinning
DMF to WDK 10.0.19041.0, obsolete the moment we retarget), `drivers/` (a devcon
license) and `app/` (a 53-line test harness we will replace).

That is the majority of the repository by file count, and none of it is the
driver.

## 8. The rename and coexistence inventory

The hard requirement: **a machine with ViGEmBus installed and a machine with
both installed must behave identically for every other application.** DS4Windows
and a dozen other tools share the ViGEmBus install; a fork that collides with
it is worse than no fork. Every identifier below has to change, and each one is
a silent, hard-to-debug failure if missed.

| Identifier | Upstream | Fork |
|---|---|---|
| Binary / service | `ViGEmBus.sys`, service `ViGEmBus` | `nimbus_padbus.sys`, service `nimbus_padbus` |
| Hardware ID | `Nefarius\ViGEmBus\Gen1` | `Nimbus\PadBus\Gen1` |
| Device interface GUID | `{96E42B22-F5E9-42F8-B043-ED0F932F014F}` | **new GUID**, `uuidgen`, never derived from theirs |
| Setup class | `System` `{4D36E97D-...}` | unchanged (correct for a root bus) |
| INF strings | "Nefarius Virtual Gamepad Emulation Bus" | "Nimbus Pad Bus" |
| Catalog | `ViGEmBus.cat` | `nimbus_padbus.cat` |
| Pool tags | upstream tags | `Nmbp` family, so the leak check in `WINDOWS_MOUSE_FILTER_PLAN.md` section 5 works |
| WPP trace GUID | upstream | new GUID |
| Protocol version | `VIGEM_COMMON_VERSION 0x0001` | `NIMBUS_PADBUS_INTERFACE_VERSION 1`, bumped on every contract change, same rule as `nimbus_moufilter_ioctl.h` |

**Do not change** the emulated USB VID/PID (`045E`/`028E`). That is Microsoft's
Xbox 360 pad, and it is what `xusb22.sys` binds to. Changing it breaks XInput,
which is the whole point. Emulating that device is what this driver is for and
is no more of a claim on Microsoft's identifiers than ViGEmBus has made for a
decade, but it is worth being deliberate about rather than accidental.

The IOCTL codes may stay numerically identical (they are namespaced by the
device interface, and keeping them means one client code path serves both
drivers), but the protocol version constant must be ours, and the client must
never assume a driver it found on our GUID speaks ViGEmBus's version 1.

## 9. Modernization work list

In dependency order. Each item has a "done when" so it can be closed.

1. **Retarget the build.** VS2019 + WDK 10.0.19041.0 becomes VS2022 + WDK
   10.0.26100, `WindowsKernelModeDriver10.0`, `TargetVersion=Windows10`, the
   pinned Spectre-mitigated MSVC toolset (14.38 as of WDK 10.0.26100.6584, the
   same trap documented in `driver/README.md`). *Done when `build.ps1` produces
   a Release x64 `.sys` on a clean VS2022 install with no manual steps.*
2. **`Driver_SpectreMitigation=Spectre` on every configuration.** Upstream sets
   it on two of eight. *Done when a grep finds it in every `PropertyGroup`.*
3. **Delete the WDF co-installer from the INF.** `[ViGEmBus_Device.NT.CoInstallers]`,
   `CoInstallers32`, `WdfCoInstaller$KMDFCOINSTALLERVERSION$.dll`. This is not
   cleanup, it is a **blocker**: Microsoft removed co-installer redistributables,
   they are unnecessary on Windows 10 and later, and an INF containing
   `CoInstallers32` fails `InfVerif /k` and is rejected by attestation signing
   with "Found legacy AddReg operation defining co-installers". Upstream's INF
   as written cannot be signed today. *Done when `infverif /k /w` passes clean.*
4. **DMF: unpin, or drop.** Upstream uses three DMF modules (`IoctlHandler`,
   `NotifyUserWithRequestMultiple`, `BufferQueue`) and carries a 647-line patch
   to force DMF onto WDK 19041. DMF itself is MIT, actively maintained
   (v1.1.159, 2026-05-07) and tracks current WDKs, so the patch should simply
   die. Decide then: keep DMF (a maintained Microsoft dependency, ~40 lines of
   our code depend on it) or replace the three modules with plain WDF
   (~300 lines, removes a submodule and a build stage). **Recommendation: keep
   DMF for the first signed build, revisit after.** Changing the IOCTL dispatch
   and the pended-request bookkeeping at the same time as everything else is
   how bugchecks get shipped. *Done when the DMF submodule builds unpatched
   against WDK 26100.*
5. **Drop the DS4 target.** 1,547 lines, a Sony VID/PID we have no business
   emulating, and 24% of the driver's attack surface, for a feature Nimbus does
   not have and does not plan. Removing it also removes
   `IOCTL_DS4_SUBMIT_REPORT`, `IOCTL_DS4_REQUEST_NOTIFICATION`,
   `IOCTL_DS4_AWAIT_OUTPUT_AVAILABLE` and the `BufferQueue` module. Keep the
   code in git history; section 10 wants its shape back for a different
   purpose. *Done when the IOCTL table has six entries and the binary shrinks.*
6. **Harden the IOCTL surface.** Upstream sets **no SDDL anywhere** in `sys/`,
   so device security is whatever the setup class default gives it, and every
   handler trusts DMF's size check plus its own reading of `SerialNo`. Audit
   every handler for: input length exactly as specified, `SerialNo` validated
   against the caller's own file object (not merely against the child list),
   output buffer length checked before the copy, and integer arithmetic on
   anything caller-supplied. Then decide the access policy deliberately, with
   `WdfDeviceInitAssignSDDLString`. *Done when every handler has an explicit
   length and ownership check, and the SDDL is a stated decision in
   `docs/PROTOCOL.md` rather than an accident.*
7. **The kernel failsafe.** The one genuinely new capability, and the reason
   this is more than a rename. Copy the mouse filter's interface v4 design: the
   client sends a heartbeat, and if it stops for N ticks the driver **centers
   the pad and releases every button by itself**, without waiting for the
   handle to close. A hung Nimbus can then never leave a stick pinned or a
   trigger held in a game. Tick length and tolerance should start from the
   filter's measured numbers (250 ms tick, survives a stall of at least 1.5 s)
   and be re-measured, not assumed. *Done when a probe that `SuspendThread`s
   the client with the stick at full deflection sees the pad center within the
   stated tolerance.* Note: this touches failsafe behavior, which `CLAUDE.md`
   lists under "when to ask". Ask before implementing, not after.
8. **Static analysis, on the same terms as the filter.** WDK Code Analysis
   clean, CodeQL clean, before any commit that touches the driver. The filter
   found three real warnings this way. *Done when both run in the repo's CI and
   fail the build on a finding.*
9. **HVCI and Driver Verifier.** Confirm HVCI compliance (no executable pool,
   `DrvRtPoolNxOptIn` is already called in `DriverEntry`), then run the full
   probe suite under Driver Verifier standard checks plus WDF verification,
   which is the bar the mouse filter had to clear. *Done when the suite passes
   twice under Verifier with no bugcheck.*
10. **CI.** GitHub Actions on `windows-latest` with the WDK, building x64
    Release and running Code Analysis. Upstream's AppVeyor cannot be reused;
    it authenticates as its author. *Done when a PR to `nimbus-padbus` gets a
    build and an analysis result.*
11. **x64 only for the first submission.** Same reasoning as
    `driver/SIGNING.md`: one INF per architecture out of one `.inx`, and
    nothing here has ever run on ARM64. Upstream ships x86 and ARM64; we should
    not inherit claims we cannot test.

## 10. The wide HID target, and a correction to the VDroid brainstorm

[VDROID_DRIVER_BRAINSTORM.md](../architecture/VDROID_DRIVER_BRAINSTORM.md)
frames vJoy's 8 axes as a vJoy limitation to be escaped with a custom driver
offering "32 axes + 256 buttons". That premise is wrong in an important way,
and it should be corrected there rather than built on.

**8 axes, 4 hats and 128 buttons is DirectInput's shape, not vJoy's.**
`DIJOYSTATE2` carries `lX, lY, lZ, lRx, lRy, lRz` plus `rglSlider[2]`, four
POVs and 128 buttons. vJoy exposes exactly that because that is what a game
reading DirectInput can see. The struct does also carry velocity, acceleration
and force axis banks (another 24 axis slots), which a HID device can populate
with `Vx/Vy/Vz` style usages, but essentially no game reads them. A device with
32 axes is a device that games read 8 of.

So the honest case for a wide HID target on our bus is **not** more axes for
games. It is:

- **Dropping the vJoy dependency.** vJoy is the other unmaintained third-party
  kernel driver in our install path, on the DirectInput side. One Nimbus bus
  driver that enumerates both an XInput pad and a DirectInput pad means one
  install, one signature, one uninstall and one support surface instead of
  three drivers from three sources.
- **Multiple devices from one driver**, which is where the real axis count
  comes from (the old Option E, 16 devices, but without needing vJoy).
- **Windows.Gaming.Input reach.** A HID gamepad (usage page 0x01, usage 0x05)
  is visible to WGI titles. It is still invisible to `XInput1_4.dll`; only the
  XUSB target reaches those.

And the implementation is much cheaper than the brainstorm assumed, because of
what section 4 found: **`Ds4Pdo.cpp` is already a generic USB-HID PDO.** It
claims `USB\Class_03&SubClass_00&Prot_00` so that `hidusb` and HIDClass load on
it, and it answers HID descriptor requests over control transfers. A wide
gamepad target is that file with a different report descriptor and none of the
Sony-specific behavior. That is a variation on working code, not the new
architecture (Option B/D) the brainstorm imagined, and it is a strong argument
for deleting DS4 in item 5 *by turning it into* this rather than merely
removing it.

This is phase 3. It does not gate anything above it, and it should not start
until the XUSB path is signed and shipped.

## 11. Signing

[driver/SIGNING.md](../../driver/SIGNING.md) is the procedure and it applies
unchanged: EV certificate on the SafeNet token, Hardware Developer Program
account, CAB via `package.ps1`, attestation submission, verify what comes back
before it touches a user. Do not re-derive any of it. Three deltas:

1. **Section 7 of that document currently says do not do this.** Its reasoning
   is that ViGEmBus is not our code, that the Code Signing Agreement makes us
   answerable for what we submit, and that a re-signed fork would collide with
   existing ViGEmBus installs. The first two are exactly right and are the
   reason items 6, 8 and 9 of section 9 are mandatory before submission: we
   have to actually own this code, having read and hardened it, not merely
   rename it. The third is solved by section 8's coexistence inventory. When
   this plan is accepted, SIGNING.md section 7 must be rewritten to point here,
   otherwise the repo contradicts itself.
2. **The INF blocker is real and is upstream's.** See section 9 item 3. The
   co-installer section must go before the first CAB is built, or the
   submission is rejected.
3. **`DriverVer` convention:** `1.0.0.<interface version>`, monotonic, passed
   explicitly with `-DriverVersion`, same as the filter.

The policy risk noted in SIGNING.md applies identically: Microsoft now files
attestation under "for testing scenarios" and points at WHCP for drivers
"intended for general Windows ecosystem use". Attestation is right for now and
is not a destination. A second attestation-signed driver doubles our exposure
to that change, which is an argument for doing the mouse filter's submission
first and learning from it.

## 12. Test plan

The good news is that almost none of this has to be built. We already have a
real-game harness that drives a pad and measures what the game did.

| Layer | Suite | Notes |
|---|---|---|
| Protocol, no driver | `tests/test_padbus_client.py` (built 2026-09-09, 53 checks) | Struct packing, IOCTL code derivation, button mapping. Pure Python, joins `run_fast_tests.py`. |
| Client against stock ViGEmBus | `tests/probe_padbus_windows.py` (built 2026-09-09, 14 checks) | Plug, submit, unplug, handle-death unplug, rapid replug, timing, notifications, the app path. Run before any driver exists. Its P7 and P9 are what found the replug gap. |
| Shaping unchanged | `tests/probe_stick_shaping_windows.py` | Existing expected values must not move. This is the regression gate for phase 0. |
| Real games | `tests/probe_game_harness_windows.py --actuator nimbus` | **The strongest tool we have.** Run L4D2 and Half-Life 2 against stock ViGEmBus, record `--write-expect` bands, then run the same recipes against the fork. If the measured turn rates fall inside the bands, the fork is behavior-compatible with the thing it replaces, measured in a real game rather than asserted. |
| Driver robustness | new `tests/probe_padbus_stress_windows.py` | Port `probe_mouse_filter_stress_windows.py`: IOCTL storms, plug/unplug storms across processes with random kills, inherited-handle leak, the file-API fuzz, a soak. That suite found real bugs in the filter and the shape transfers directly. |
| Failsafe | in the stress probe | Suspend the client at full deflection, assert the pad centers (section 9 item 7). |
| Under Verifier | all of the above | Standard checks plus WDF verification, twice, no bugcheck. |

House rules from `CLAUDE.md` that apply: run one game at a time so each
session's pad is player one, and leave the mouse and keyboard alone while a
harness run is going.

## 13. Risks, and the gate

| Risk | Severity | Response |
|---|---|---|
| **Anti-cheat rejects an unknown bus driver** | **High. This is the gate.** | ViGEmBus is tolerated by EAC, BattlEye and Vanguard largely through years of reputation and ubiquity via DS4Windows. A byte-identical fork under a new name has none of that. We could ship a driver that is technically better and leaves users unable to play Elden Ring. **Before any attestation submission**, test-signed builds must be run against at least one EAC title and one BattlEye title, and the disclosure conversation in `driver/SIGNING.md` section 6 must cover both drivers. If a title blocks it, phase 2 stops and phase 0's dual-driver client is what saves us. |
| We become responsible for a kernel driver | High | Items 6, 8, 9 of section 9 are non-negotiable, and a security contact and disclosure policy ship with the repo. |
| Bugcheck on a user's machine | High | Driver Verifier gates, staged rollout, and the installer keeps stock ViGEmBus as a fallback until the fork has run in the wild for a release. |
| Attestation signing withdrawn | Medium | Shared with the mouse filter. Watch it once, not twice. |
| Two drivers installed, wrong one used | Medium | Section 8's inventory, plus the client reports which interface it opened in `get_status()` and the UI shows it. |
| Effort overruns and we ship neither | Medium | Phase 0 delivers value on its own and is where the plan stops if funding does. |
| Upstream reappears (VirtualPad) | Low | Would be good news. The client abstraction means adopting it is another GUID to probe. |

**The gate, stated plainly:** do not submit for attestation signing until (a)
the probe suites and the game harness pass against the test-signed fork, (b) an
EAC title and a BattlEye title have been tested with it loaded, and (c) the
mouse filter's own attestation submission has completed, so we have done this
once before doing it twice.

**Which titles, and one confound (added 2026-09-09).** Elden Ring is already
in the harness and is the EAC title. For BattlEye, **Arma 3**: Steam, single
player works offline, BattlEye is a launcher toggle so the same session can
be run with and without it, it has an Xbox controller preset, and its editor
has a scripting console that can read the player's heading, which makes a
pose oracle of the kind the Source games give us. **Unturned** is the free
smoke test (BattlEye on by default, single player, a few gigabytes) for "does
the pad still work with the driver loaded". Neither exercises a server join,
which is where BattlEye kicks; a short manual join covers that. The confound:
the gate says test-signed builds, but Vanguard refuses to run at all with
test signing enabled, and EAC and BattlEye may treat that mode as suspicious
too, so a test-signed build cannot answer the question cleanly. The way round
it is to attestation-sign a build we do not ship, which is exactly the "for
testing scenarios" use Microsoft now files attestation under, test with that,
and ship only if it passes. That splits phase 3 into "submit" and "ship".

**The BattlEye baseline exists (2026-09-09, later the same day).** Arma 3 is
in the harness (`tests/games/arma3.json` with BattlEye, `arma3_nobe.json`
without; the `arma3` oracle in `tests/game_harness.py`; the results entry in
`GAME_TEST_HARNESS.md` section 8). With stock ViGEmBus and the pure-Python
client, BattlEye on and off measure the same to the decimal: 305 degrees a
second at full stick either way, 36.7 at 0.6, the deadzone between 0.30 and
0.40, 5.1 m/s walking, a 32 ms button; pad run 13/13 and Nimbus run 17/17
with BattlEye running. So the gate's question for the fork is now concrete:
run the same two recipes with the fork loaded and compare against these
bands (`expect` in the recipes). A server join, where BattlEye actually kicks,
is still a manual step.

## 14. Order of work

| Phase | Work | Effort | Gate to start |
|---|---|---|---|
| **0** | `src/padbus_client.py`, drop `vgamepad`, delete the CI workaround, protocol tests, `probe_padbus_windows.py`, regression through the shaping probe and one game recipe | 2 to 4 days (took one) | **Done 2026-09-09**, section 17 |
| **1** | Create `nimbus-padbus`, strip dead infrastructure, retarget to VS2022 + WDK 26100, unpin DMF, delete DS4, fix the INF, rename everything in section 8, build and test-sign, `install-dev.ps1` | 1 to 2 weeks | Phase 0 done |
| **2** | Harden the IOCTL surface, kernel failsafe, Code Analysis + CodeQL + Verifier, port the stress probe, full game-harness comparison against stock ViGEmBus, anti-cheat testing | 2 to 3 weeks | Phase 1 builds and loads |
| **3** | Attestation submission, installer integration, staged rollout with ViGEmBus fallback | 1 week plus Partner Center latency | **Section 13's gate** |
| **4** | The wide HID target, and dropping vJoy | 2 to 4 weeks | Phase 3 shipped and quiet |

Phases 0 and 1 are worth doing even if 2 through 4 never happen: phase 0 is a
straight improvement, and phase 1 gives us a buildable, patchable copy of the
driver our product depends on, which is insurance whether or not we ever sign
it.

## 15. Open questions to settle before phase 1

1. **Repo name and owner.** `nimbus-padbus` under `owenpkent`, or a project
   org? An org is easier to hand over and looks less like a personal fork,
   which matters for the community adoption argument.
2. **Do we want community adoption at all?** A driver other apps install
   changes the support burden completely. It is also the only way the fork
   earns the anti-cheat reputation that section 13 says it lacks. This is the
   strategic question underneath the whole plan.
3. **The failsafe design** touches `CLAUDE.md`'s "when to ask" list. Settle the
   tick length, the tolerance and what "center" means (does it release buttons,
   or only axes?) before writing it.
4. **x86.** Upstream ships it; we would not. Does any Nimbus user run 32-bit
   Windows? Almost certainly not, and dropping it halves the submission matrix.
5. **Does phase 0 keep a `vgamepad` fallback path**, or is the cut clean?
   Answered 2026-09-09: clean cut. `X360Pad` keeps vgamepad's method names,
   so the fallback is one import away if ever needed, and the previous
   commit is the other fallback.
6. **A user-mode failsafe before the kernel one.** The review of 2026-09-09
   argued that most of section 9 item 7 can be had against stock ViGEmBus in
   user mode: an in-process watchdog thread that submits a neutral report
   when the main thread stops calling update (the pattern that already
   exists, disabled, in `src/vjoy_interface.py`), and a supervisor process
   that terminates Nimbus when its heartbeat stops, which closes the handle
   and unplugs the pad. Decide that before designing the kernel one; it is
   the same "when to ask" item.

## 17. Phase 0 log

**2026-09-09, built and measured.** `src/padbus_client.py`,
`tests/test_padbus_client.py` (53 checks, in the fast suite) and
`tests/probe_padbus_windows.py` (14 checks against the real bus). `vgamepad`
is out of `requirements.txt`, the CI filter and the PyInstaller DLL block are
gone, `ViGEmInterface`'s public methods are unchanged, and the harness,
`mouse_hider` and the installer probe use the same `X360Pad`. The bus was
found through cfgmgr32's device interface list rather than SetupDi (same
result, less code), and the IOCTL codes were re-derived from `CTL_CODE` and
confirmed against the bus: the user index query is `0x801 + 0x206`, not
`0x207` as first guessed (`0x207` is the DualShock output wait, which
answered "buffer too small" and then pended).

**The machine.** `ViGEmBus.sys` here is **1.21.442.0**, the file inside the
1.22.0 setup the installer bundles, not the 1.17.333.0 that SIGNING.md
recorded on the 6th; the installer probe's reinstall had already replaced it.
Item 2 of section 2 was true of dev machines in general, and this one had
already crossed over.

**Measured** (the probe, 14/14; `update` numbers over 2,000 reports):

| What | Result |
|---|---|
| Plug and ready wait | 3 to 7 ms on a recently used serial, about 40 ms on a fresh one |
| XInput reads the submitted report back, field for field | within 7 to 26 ms |
| `update()` | median 22 us, p99 46 us, max 289 us |
| Unplug, or the handle closing | gone from XInput in 10 to 22 ms |
| A killed process's pad | gone in 10 to 20 ms, three of three |
| 20 x plug, report, close | 0 failures after the fix below; 4 reports needed a retry |
| User index query | accepted, nothing written back (0 bytes returned, sentinel intact), so `None` |
| Output notifications | a fresh pad hands over 4 queued reports, then the call pends; rumble 30000/10000 from `XInputSetState` arrives as 117/39 |

**Finding 1, the replug gap.** The bus finds a pad by serial with
`WdfChildListRetrievePdo`, which returns nothing while a serial's device
object is between an old child dying and its replacement starting, and every
report in that gap fails with `ERROR_NO_MORE_ITEMS` (259). The gap opens when
a serial released a moment ago is plugged again: PnP removes the old child
asynchronously (its devnode stays present for about 20 ms, longer while an
XInput reader that has not re-polled holds it), the plug lands on it, and the
ready wait completes on it, so it proves nothing. A first report can even
succeed and the next one fail. Measured: 5 to 12 dead pads in a 20-iteration
close-and-plug storm with XInput loaded in the process, and the pad of a
process started right after another one unplugged (which is the shape of
"Nimbus restarted while the game was running"). A deliberate same-serial
replug with a pause lived; the timing decides. What the client does about it,
in three layers: skip serials whose child devnode is still present
(`present_child_serials`, cfgmgr32), prove readiness with real reports for up
to the ready timeout instead of trusting the ready wait, and in `update()`
retry a failed report for 150 ms and re-plug on another serial if the pad is
really gone, so a game sees a brief reconnect rather than a stick that never
moves again. With that the storm passes with 0 failures (4 reports recovered
by retry, no re-plug needed), and the killed-process pad is gone in 10 ms
every time.

**Finding 2, ViGEmClient hides it.** vgamepad survived the same storm with
"0 errors", which looked like a regression in our client until the reference
source explained it: `vigem_target_x360_update` returns `VIGEM_ERROR_NONE`
for every failed submit except access denied. The failed reports were
dropped silently. So under vgamepad the same dead pads happened and Nimbus
kept saying "connected". Recorded as section 2 item 6.

**Finding 3, serials leave devnodes.** Every distinct serial leaves a
phantom devnode under `Enum\USB\VID_045E&PID_028E` (the instance id is the
serial, `%02d`; three test serials from today are there: 12345, 2147483647
and -1). So the client keeps serials in 1 to 16 and walks them like
ViGEmClient does; the "random serial" in section 6 is withdrawn.

**Finding 4 (2026-09-14), a fresh pad reads stale stick values.** Seen while
building the network pad (`netpad/`), then reproduced with `X360Pad` alone:
three plug, neutral `update`, read cycles each read, through
`XInputGetState` on slot 0, LX -3356, LY -1869, RX -3255, RY -848 (about
-0.10, -0.06, -0.10, -0.03) with packet number 1, identical every time, and a
second all-zero `update` did not clear it (the packet number stayed 1). The
first report that differs from all zeros replaces them, and a return to zero
after that reads 0. So a new pad shows a small left-and-down deflection on
both sticks until something moves. Every game measured has a deadzone above
it (Elden Ring's is the lowest, at or below 0.20), which is why nothing has
shown it, but it applies to the app's own ViGEm pad too. Where the values come
from (XInput's slot state or the bus's child) is not known. A plausible fix, a
one-count report followed by zero at plug time, is not made: it is a
`padbus_client.py` change, so it wants the bus probe and the shaping probe
around it.

**Not done, on purpose:** the user-mode failsafe (section 15, question 6)
and the SetupDi enumeration the plan specified (cfgmgr32 does the same job).
`request_notification` and `user_index` exist for the harness and the probe;
Nimbus uses neither.

**Regression gate, all passed on the first run (2026-09-09):**

| Gate | Result |
|---|---|
| Fast suite | 10/10 files (the nine before plus `test_padbus_client.py`), about four seconds |
| `probe_stick_shaping_windows.py` | 19/19; every expected value unchanged (2 px drag RX=0.293 expected 0.293, full drag 0.950, diagonal 0.638, RT slider 0.995 and 0.005) |
| `probe_game_harness_windows.py --game left4dead2 --actuator nimbus` | 19/19; full drag -404.4 deg/s, 1 px drag -2.4 deg/s against an expected -2.4 within 1.0, walk 200.0 units/s against an expected 200.9 within 40.2, all five Spectator+ primitives within tolerance |
| `probe_game_harness_windows.py --game left4dead2 --actuator pad` | 17/17; the harness's own pad is now an `X360Pad` too: yaw at 0.40 measured -13.8 deg/s against an expected -13.8 within 2.1, at 0.60 -34.3 against -34.2, pitch -20.9 against -21.3, walk 199.7 units/s against 199.7, deadzone first moved at 0.28 as before, button latency 31 ms |

The harness numbers did not move, which is what the plan's "done when" asked
for: the client is behaviour-compatible with the library it replaced, measured
in a real game.

## 16. Sources

Checked 2026-09-09 against a fresh clone of `nefarius/ViGEmBus@master`
(`sys/`, `ViGEmBus.inf`, `ViGEmBus.vcxproj`, `patches/dmf.diff`, `stage0.ps1`)
and `nefarius/ViGEmClient@master` (`include/ViGEm/km/BusShared.h`,
`include/ViGEm/Common.h`, `src/ViGEmClient.cpp`).

- [ViGEmBus repository](https://github.com/nefarius/ViGEmBus) (archived 2023-11-02, BSD-3-Clause)
- [ViGEmBus end-of-life statement](https://docs.nefarius.at/projects/ViGEm/End-of-Life/)
- [ViGEmClient repository](https://github.com/nefarius/ViGEmClient) (MIT)
- [Microsoft DMF](https://github.com/microsoft/DMF) (MIT, v1.1.159, 2026-05-07)
- [Removing co-installers from driver packages](https://learn.microsoft.com/en-us/windows-hardware/drivers/develop/removing-coinstallers)
- [Attestation signing failure: legacy CoInstallers32 fails InfVerif /k](https://learn.microsoft.com/en-sg/answers/questions/1650535/attestation-signing-failure-with-found-legacy-addr)
- [Virtual HID Framework (VHF)](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/virtual-hid-framework--vhf-)
- [Attestation sign Windows drivers](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/code-signing-attestation)
- [driver/SIGNING.md](../../driver/SIGNING.md), section 7 in particular
- [3-Clause BSD License](https://opensource.org/license/bsd-3-clause)
