# Nimbus Mouse Filter (Windows kernel driver)

A KMDF upper filter on the mouse device class. With no client attached it
passes every mouse packet through unchanged. While Nimbus holds the control
device open and turns isolation on, the driver withholds physical mouse packets
from `mouclass` (so the cursor, Raw Input, and every game stop seeing the mouse)
and delivers them to Nimbus instead. Nimbus drives its virtual stick from them
and, on Windows, moves the real cursor itself with `SetCursorPos` (the cursor
relay in `src/mouse_isolation_win.py`), which moves the cursor without creating
an input event: the mouse keeps working everywhere, the game keeps the
foreground, and the game's Raw Input sees nothing.

This is the Windows equivalent of the one-line `EVIOCGRAB` grab that
`src/mouse_isolation.py` uses on Linux (on the `linux-uinput-support` branch,
not yet merged). The reason it has to be a kernel driver,
and the measurements behind it, are in
[docs/vision/HOST_MODE_ISOLATION.md](../docs/vision/HOST_MODE_ISOLATION.md)
(sections 8 and 9). The design is in
[docs/vision/WINDOWS_MOUSE_FILTER_PLAN.md](../docs/vision/WINDOWS_MOUSE_FILTER_PLAN.md).

**Status:** dev build loaded and validated on hardware on 2026-09-05 (Windows 11
25H2, Logitech USB mouse): the attended probe passed 9/9, with the fake Raw Input
game receiving zero `WM_INPUT` while the driver captured 1,017 packets and none
were dropped, and the unattended robustness set (client kill and suspension,
open races, parked-read draining, malformed requests, a 30 s idle soak) passed
16/16, then 16/16 again under Driver Verifier (standard checks plus WDF
verification) after a reboot that loaded the filter cleanly, and the filter
survived mouse restarts both with a client isolating and with none. Interface
v3 (heartbeat ticks, so a frozen Nimbus loses the mouse within 2 s) and the
cursor relay in the client were written the same evening; v3 is installed and
passed the probe 17/17 under Driver Verifier, including the frozen-client
release. The relay is wired into the bridge's Full Game Mode, and the real
Nimbus app passed the relay harness 8/8 against the fake Raw Input game
(`tests/probe_nimbus_relay_windows.py`), and a first hands-on run of Game
Mode through TeamViewer worked once two usability gaps were closed (the game
comes to the foreground by itself; a cursor found over the game is parked
onto Nimbus). On 2026-09-06 the unattended battle test
(`tests/probe_mouse_filter_stress_windows.py`: IOCTL and read storms, a
512-read flood, open/close storms in threads and across processes with random
kills, chaos kills, an inherited-handle leak, CPU starvation, a fuzz of every
file API the device can receive, a 120 s soak) passed 14/14 against the loaded
build, and the static checks ran: Code Analysis found three warnings (two
paging violations, one false positive), fixed in the source; that rebuild was
installed the same afternoon and passed the three suites again (17/17, 14/14,
8/8), then once more under Driver Verifier with no bugcheck. One client-side
finding was fixed the
same day: with the machine saturated by HIGH-priority processes the reader
thread was starved past the watchdog and lost the mouse, so it now runs at
time-critical priority. The same evening brought **interface v4**: the
heartbeat tick went from 1 s to 250 ms, so a live client survives a stall of
at least 1.5 s instead of 0.75 s, and the client pauses isolation while the
secure desktop has the input (lock screen, UAC) and resumes after. v4 is
built and installed and validated the same evening (17/17, 15/15 with the new desktop-pause check, 8/8, then the same three under Driver Verifier with no bugcheck). Details in the plan doc, section 5 items 7 and 11.
**Interface v5** (2026-09-07) closed the open review comments: the watchdog
releases on idle time alone (parked reads no longer postpone it), the service
callback decides under the lock, a read already dequeued re-checks isolation
before completing, and the client polls `Ctrl+Alt+F12` between reads and never
closes the driver handle under a reader that is still running. v5 was built
and passed Code Analysis and CodeQL clean on 2026-09-16 and was installed the
same afternoon, the first filter on the machine since the 2026-09-07 recovery
and the first run on the post-Hyper-V baseline: probe 17/17, relay harness
8/8, battle test 15/15 (14/15 on the first pass, B2b having still encoded
v4's per-parked-read cost; v5 released in 2.06 to 2.10 s whatever N was,
which is the fix working, and the check now asserts that). Not yet under
Driver Verifier.
Not attestation-signed, not validated against an anti-cheat game, not in any
release. Do not ship it yet. The route to a signed build, and what it does and
does not buy, is in [SIGNING.md](SIGNING.md).

## Layout

| File | What it is |
|---|---|
| `nimbus_moufilter/nimbus_moufilter.c` | The driver. `NimbusFilter_ServiceCallback` is where packets are dropped or passed. |
| `nimbus_moufilter/nimbus_moufilter.h` | Private declarations and driver-wide state. |
| `nimbus_moufilter/nimbus_moufilter_ioctl.h` | The user/kernel contract. `src/mouse_isolation_win.py` mirrors these values. |
| `nimbus_moufilter/nimbus_moufilter.inx` | INF template (service + file only; the class filter entry is added by `install-dev.ps1`). |
| `nimbus_moufilter/nimbus_moufilter.vcxproj` | KMDF driver project, `WindowsKernelModeDriver10.0` toolset. |
| `build.ps1` | Build and collect outputs into `out/`. |
| `package.ps1` | Build the CAB for Partner Center attestation signing, and verify the package that comes back. See [SIGNING.md](SIGNING.md). |
| `enable-testsigning.ps1` | Install the test cert and turn on test signing (elevated, one reboot). |
| `disable-testsigning.ps1` | Turn test signing back off **safely**: detaches the filter first, then changes the setting. Use this instead of bare `bcdedit`, see [If the machine stops being able to load the driver](#if-the-machine-stops-being-able-to-load-the-driver). |
| `install-dev.ps1` / `uninstall-dev.ps1` | Register/unregister the class filter for development (elevated). `install-dev.ps1` also updates a loaded build: it detaches the filter, replaces the file, and re-attaches, and it registers the boot guard below. |
| `check-mouse-filter.ps1` | Read-only health check, no elevation: would the mouse survive the next reboot? Run it before rebooting, before turning test signing off, and after Windows updates. |
| `recover-mouse.ps1` | Emergency (elevated): detach the filter and restart the mice, so the mouse comes back without a reboot. Leaves the service and `.sys` in place. Self-contained on purpose. |
| `nimbus-mouse-guard.ps1` | The self-heal. `install-dev.ps1` copies it to `%ProgramData%\ProjectNimbus\driver` and registers it as the `NimbusMouseFilterGuard` scheduled task (SYSTEM, at startup and daily). It detaches the filter if it is registered but can no longer load. |
| `pnp-common.ps1` | Shared by the scripts above: `Restart-Mice`, which restarts every mouse with `pnputil /restart-device` so the filter attaches or detaches without a reboot. |

## Build

Needs Visual Studio 2022 with the "Windows Driver Kit" component, the
Spectre-mitigated libraries for the pinned MSVC toolset (14.38 as of WDK
10.0.26100.6584, **not** the "Latest" one), and WDK 10.0.26100.

```powershell
driver\build.ps1              # Release x64 -> driver\out\
```

`build.ps1` passes the kit root explicitly and uses the 64-bit MSBuild, because
the 64-bit `KitsRoot10` registry value can point at the wrong folder and the
32-bit MSBuild cannot load `InfVerif`. See the plan doc, section 6.

## Release signing

The dev build is test-signed and loads only with `testsigning` on, which is
exactly the state anti-cheat refuses to start in. A build users can load needs a
Microsoft signature through Partner Center attestation signing:

```powershell
driver\package.ps1 -SkipSign -DriverVersion 1.0.0.4    # dry run, no token needed
driver\package.ps1 -DriverVersion 1.0.0.4 -Thumbprint <EV cert thumbprint>
```

Registration prerequisites, the submission rules, how to verify the package that
comes back, and where attestation signing stands after the April 2026 driver
policy are all in [SIGNING.md](SIGNING.md). None of it has been done yet.

## Install for development (elevated, at the machine)

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
& "C:\path\to\Nimbus-Adaptive-Controller\driver\enable-testsigning.ps1"   # once; then reboot
& "C:\path\to\Nimbus-Adaptive-Controller\driver\install-dev.ps1"          # copies the .sys, creates the service, adds the class UpperFilters, restarts the mice
& "C:\path\to\Nimbus-Adaptive-Controller\driver\check-mouse-filter.ps1"   # any time, no elevation: would the mouse survive the next reboot?
```

Use the full path: an elevated PowerShell starts in `C:\WINDOWS\system32`, where
`driver\install-dev.ps1` is taken for a module name and fails with "The module
'driver' could not be loaded".

The reboot is not optional: `bcdedit` stores the setting for the *next* boot,
and a self-signed driver attached before that reboot fails to load with
`0xC0000428` (Code Integrity event 3004, "invalid root certificate"), which the
script's rollback then undoes. After the reboot the desktop shows a "Test Mode"
watermark, and `install-dev.ps1` prints the running boot's Code Integrity
options (bit `0x2` is test signing) and refuses to attach the filter unless it
is set or the driver is Microsoft-signed.

Run `install-dev.ps1` again after every rebuild. If the previous build is
loaded it detaches it first (the mice restart twice), because a loaded driver
holds its `.sys` open and the copy would otherwise fail.

`install-dev.ps1` inserts `nimbus_moufilter` **in front of** `mouclass` in the
mouse class `UpperFilters` list. Filters attach in list order, first listed
closest to the function driver, so this puts the filter between `mouhid` and
`mouclass`, which is where it has to be to receive `IOCTL_INTERNAL_MOUSE_CONNECT`.
On a stock machine the list reads `nimbus_moufilter, mouclass` afterwards.

`install-dev.ps1` also registers the `NimbusMouseFilterGuard` scheduled task,
which detaches the filter if this machine ever stops being able to load it.
That is a different risk from a bad install, and it is the one that has
actually happened here: see
[If the machine stops being able to load the driver](#if-the-machine-stops-being-able-to-load-the-driver).
`-NoGuard` skips it, which is only sensible if you have a second pointing
device or are deliberately testing the unguarded failure.

Then, from the repo root:

```powershell
venv\Scripts\python -m src.mouse_isolation_win --status      # driver reachable? isolating?
venv\Scripts\python -m src.mouse_isolation_win --grab 5       # isolate for 5 s (the desktop cursor should freeze)
venv\Scripts\python -m src.mouse_isolation_win --grab 5 --relay   # same, but the cursor keeps working (cursor relay)
```

Remove it with `driver\uninstall-dev.ps1`. Do not also install the INF with
`pnputil /add-driver`: that points the service at a Driver Store copy that
`install-dev.ps1` does not update, so rebuilds would keep loading the old
driver. `install-dev.ps1` refuses to continue if it finds such a service, and
`uninstall-dev.ps1` removes the Driver Store package.

**While test signing is on, anti-cheat games (EasyAntiCheat, BattlEye, Vanguard)
refuse to start.** Validate the filter against the fake-game probe and a
non-anti-cheat game under test signing; Elden Ring validation waits for an
attestation-signed build. To go and play one, use
`driver\disable-testsigning.ps1`, **not** `bcdedit` on its own; the next
section is why.

## If the machine stops being able to load the driver

**This is the one failure that takes the whole mouse away, and it does not
happen at install time.**

A class upper filter is mandatory once listed. If `nimbus_moufilter` is named
in the mouse class `UpperFilters` and Windows will not load it, Windows does
not start the mouse devices at all. Not a degraded mouse, and not a mouse
without isolation. No mouse.

`install-dev.ps1` checks the signature and the running boot's Code Integrity
state before it attaches anything, verifies afterwards, and rolls back if the
driver did not load, so the install itself is safe. The dangerous state is the
**drift afterwards**: the filter stays registered while the machine quietly
stops accepting the test-signed `.sys`. Anything that does that arms the trap.

- A Windows update that resets boot configuration.
- `bcdedit /set testsigning off`, which is exactly what you do to play an
  anti-cheat game or to get rid of the Test Mode watermark.
- Secure Boot switched back on in firmware.
- A rebuild signed with a certificate that is no longer in the machine stores.

Nothing warns you when it happens. The mouse keeps working until the next
mouse restart or reboot, and then it is gone.

### What it looks like

| Where | What you see |
|---|---|
| Device Manager / `Get-PnpDevice -Class Mouse` | `Status: Error`, `DEVPKEY_Device_ProblemCode` **52** (`CM_PROB_UNSIGNED_DRIVER`), problem status `0xC0000428` (`STATUS_INVALID_IMAGE_HASH`). Other load failures give Code 39 or Code 19 instead. |
| `sc.exe query nimbus_moufilter` | `STOPPED`, win32 exit code `1077` (never started). |
| System event log, Kernel-PnP id 219 | `The driver \Driver\nimbus_moufilter failed to load. Status: 0xC0000428`, once per device start attempt. |
| The `UpperFilters` value | Still reads `nimbus_moufilter, mouclass`. That is the thing to remove. |

`Get-AuthenticodeSignature` is not enough to tell you this is coming: with the
test certificate still in the machine stores it reports `Valid` while Code
Integrity refuses the driver anyway. The question is whether test signing is
active in **this boot**, which `check-mouse-filter.ps1` reads from
`NtQuerySystemInformation(SystemCodeIntegrityInformation)` rather than from
`bcdedit` (which shows the stored setting, and therefore the *next* boot).

**A remote desktop tool still working is misleading.** TeamViewer and the like
move the cursor with `SendInput` in user mode, which never goes through
`mouclass`, so the remote cursor keeps working perfectly over a machine whose
physical mouse device is dead. Do not read that as the mouse being fine. Do
use it to accept the UAC prompt for the fix.

### The guards

1. **Before**: `driver\check-mouse-filter.ps1`, read-only and no elevation.
   Exits 0 when safe, 1 when the filter is registered but would not load, and
   2 when the mouse is already broken. Run it before a reboot, before turning
   test signing off, and after Windows updates. Unelevated it can tell that
   the guard task exists but not read its state: the task runs as SYSTEM and
   `Get-ScheduledTask` returns nothing for it from a standard shell, so the
   script asks `schtasks` and treats "Access is denied" as registered.
2. **Instead of the sharp edge**: `driver\disable-testsigning.ps1` detaches the
   filter first and only then changes the boot setting, which is the same two
   steps in the only order that cannot strand you.
3. **After, automatically**: the `NimbusMouseFilterGuard` scheduled task, which
   `install-dev.ps1` registers (skip with `-NoGuard`). It runs
   `nimbus-mouse-guard.ps1` as SYSTEM at startup and once a day, and detaches
   the filter when it is registered but unloadable. The daily run is the one
   that matters most: it disarms the trap while the mouse still works. The
   startup run is the backstop, and costs one repaired boot rather than a dead
   machine. It logs to `%ProgramData%\ProjectNimbus\logs\mouse-guard.log` and
   to the Application event log (source `Nimbus Mouse Filter Guard`). It is
   deliberately timid: it acts only when it can show the filter is registered
   and will not load, it leaves a partly-broken set of mice alone, it does
   nothing when it cannot read the Code Integrity state, and it never writes an
   `UpperFilters` list without `mouclass`.

### Recovery

The keyboard is never filtered, so this is always recoverable without a mouse:
`Win+X`, then `A` for an elevated PowerShell.

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
& "C:\path\to\Nimbus-Adaptive-Controller\driver\recover-mouse.ps1"
```

That detaches the filter and restarts the mice, and the mouse is back with no
reboot. It leaves the service and the `.sys` alone, so `install-dev.ps1`
re-arms it later. Use `uninstall-dev.ps1` instead to remove the dev install
entirely. With no repo reachable, the same fix by hand:

```
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E96F-E325-11CE-BFC1-08002BE10318}" /v UpperFilters /t REG_MULTI_SZ /d mouclass /f
```

then replug the mouse or reboot. **Never delete the value**: `mouclass` is the
mouse class driver itself and has to stay in it.

To get the filter back afterwards: `enable-testsigning.ps1`, reboot, then
`install-dev.ps1`.

### It has happened

2026-09-07, the development machine. The filter had been installed and verified
on 2026-09-06. Test signing was off by the next boot, so Kernel-PnP logged
`0xC0000428` at every device start from 18:08 on 2026-09-06 and the machine
came up on 2026-09-07 with no mouse, problem code 52, while TeamViewer worked
normally and hid how total the failure was. `recover-mouse.ps1` did not exist
yet; the fix was the `UpperFilters` edit plus `pnputil /restart-device`, which
is what that script now does. Every guard above was written in response.

## Safety

- The control device is exclusive: one client at a time. A second open fails
  with `ERROR_ACCESS_DENIED`.
- Isolation is cleared when the client's handle closes (crash, kill, exit).
- A watchdog clears isolation if no read has arrived for 2 s while isolating.
  It runs only while isolating. Since interface v3 a read that has been
  parked with nothing to deliver is completed empty after
  `NIMBUS_MOUFILTER_TICK_MS` (a heartbeat tick: 1 s on v3, 250 ms since v4)
  and the client issues the next one; a client that is frozen or
  suspended cannot, so it loses the mouse within 2 s of its last read (on v2
  a parked read kept isolation alive by design; on v3 a client frozen with
  `NtSuspendProcess` was released 2 s after its last read, probe check U11).
  The stall a live client survives is 2 s minus the age of its parked read,
  which it re-issues after each tick: 0.75 to 2 s on v3, 1.5 to 2 s on v4
  (stress probe B5). The client's reader thread also runs at
  `THREAD_PRIORITY_TIME_CRITICAL`, which kept the mouse through a
  HIGH-priority CPU burn that starved a normal-priority reader past the
  watchdog (B6). Before v5 a client that parked N reads and froze was released
  after max(2 s, tick + N x 250 ms), one tick per watchdog period; since v5
  the watchdog counts idle time alone, so the release lands at 2.0 to 2.25 s
  whatever N is and the reads still parked fail with `ERROR_NOT_READY` (B2b).
- While the secure desktop has the input (lock screen, UAC prompt,
  Ctrl+Alt+Del) the cursor relay cannot reach the cursor and the hotkey
  cannot be seen, so the client checks the input desktop every 100 ms and
  gives the mouse back for as long as another desktop has it, keeping the
  handle and taking the mouse again when its desktop returns (off within
  40 ms, on again at once, stress probe B13; by hand, the sign-in prompt
  and UAC prompts paused it within about 100 ms and it resumed within
  about 100 ms of their closing). The Windows 11 lock screen itself sits on
  the Default desktop, so isolation stays on there and the relay keeps the
  cursor working. The driver itself is unaware of desktops.
  Once isolation is off, every read fails with `ERROR_NOT_READY`, so
  the client notices a watchdog release at its next read and reports the
  stop. Every release path (IOCTL, handle cleanup, watchdog) drains reads
  that were already parked, so a read cannot outlive a release. Measured on
  v2: release 2.1 s after the last read activity; a killed client frees the
  device in about 30 ms.
- Coverage: the filter sees every pointer that reports through `mouclass`
  (USB, Bluetooth and PS/2 mice, touchpads in legacy mouse mode). Precision
  Touchpads report through the HID digitizer path straight to `win32k` and
  are expected to bypass it (not yet measured on hardware). `--status` shows
  `connected_mice`, and `MouseIsolation.start()` refuses to report success
  when it is 0, since the real cursor would keep moving with nothing captured.
- The keyboard is never filtered, so `Ctrl+Alt+Del` always works and every
  recovery below can be done from the keyboard. `Ctrl+Alt+F12` releases
  isolation: the client polls it with `GetAsyncKeyState` on its reader thread
  every 100 ms, both while a read is parked and between reads, so a mouse
  that never stops moving cannot starve it. It needs no focus and works when
  Nimbus's UI thread is stuck. The `Ctrl+Alt+F12` listener in
  `src/mouse_hider.py` still stops the Controller Mode pulse. Note that this
  is polled by the **client**, so it does not release a device held by some
  other process; see "Security model" below.
- A class upper filter is **mandatory once listed**: if the driver fails to
  load, Windows does not start the mouse devices (Device Manager Code 39 or
  Code 19, or Code 52 when the signature is what was rejected) until the
  `UpperFilters` entry is removed. `install-dev.ps1` therefore creates a
  restore point first, verifies after attaching, and rolls the registry entry
  back automatically if the driver is not running or any mouse reports a
  problem. That covers the install. It cannot cover the machine changing
  afterwards, which is the case that actually bit us, so a filter that is
  registered but no longer loadable is detached by the
  `NimbusMouseFilterGuard` task at startup and daily. See
  [If the machine stops being able to load the driver](#if-the-machine-stops-being-able-to-load-the-driver),
  which is the section to read before turning test signing off.

## Security model

The control device is `\\.\NimbusMouseFilter`, created with the SDDL
`D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;IU)`: full control for SYSTEM and
Builtin Administrators, `GENERIC_READ | GENERIC_WRITE` for Interactive Users
(`IU`, S-1-5-4). Both control codes use `FILE_ANY_ACCESS`, and the device is
exclusive (`WdfDeviceInitSetExclusive`), one handle at a time. The grant to
`IU` is **deliberate and necessary**: Nimbus runs unprivileged as the
logged-in user, because an accessibility tool that needs an elevation prompt
to start is not usable by the people it is for.

What the grant permits: any process holding an interactive token can open the
device, send `IOCTL_NIMBUS_SET_ISOLATION(1)` and keep issuing reads. While it
does, physical mouse packets are withheld from `mouclass`, so the cursor, Raw
Input and every application stop seeing the mouse. The watchdog does not help
here: it releases only when reads stop arriving, and a cooperating process
keeps reading.

### Who can hold the device

1. **A stuck or buggy client, including Nimbus itself.** Covered. Handle
   cleanup (crash, kill, exit) restores pass-through, and the watchdog
   releases within `NIMBUS_MOUFILTER_WATCHDOG_MS` once reads stop arriving.
2. **A hostile process in the same session.** Not a new capability. A
   same-session process already receives the whole mouse stream through Raw
   Input with `RIDEV_INPUTSINK`: no driver, no isolation, nothing visible to
   the user. The repo demonstrates this itself in
   `tests/probe_rawinput_windows.py`. A `WH_MOUSE_LL` hook can already
   swallow mouse events too. What the filter adds is that suppression also
   reaches Raw Input, and that recovery is harsher. Degree, not kind.
3. **A hostile process in another session.** This is the one with no
   user-mode equivalent. Low-level hooks are scoped to the installing
   thread's desktop and window station, so they cannot reach another
   session. The filter sits on the mouse class stack, below the session
   boundary, and its symlink `\DosDevices\NimbusMouseFilter` lives in the
   global object namespace. `IU` is present in every interactive logon
   token, RDP included. So a standard user in an RDP session can suppress
   the physical mouse of the administrator at the console, and nothing in
   user mode can observe or undo it. That routes around the boundary UIPI
   exists to maintain.

### A tempting attack that does not work

Isolate, read every packet, then replay motion with `SetCursorPos` and clicks
with `SendInput`, so the mouse looks normal while it is being logged. This is
Nimbus's own cursor relay repurposed. It **buys an attacker nothing**:
same-session capture is already available through `RIDEV_INPUTSINK` without
any of it, and the cross-session version fails because `SetCursorPos` moves
the attacker's own session's cursor, leaving the console user with a mouse
that is simply dead. Recorded because it is where people look first.

### Recovery today

- `Ctrl+Alt+F12` is polled by the **client**, on its reader thread. It
  protects against a Nimbus that is stuck, and not at all against a process
  that is holding the device deliberately.
- Against a hostile holder the recovery is: **kill the process from the
  keyboard** (handle cleanup releases immediately), or remove the
  `UpperFilters` entry and restart the mice.
- Every mitigation in the "If it gets stuck" table below is
  **keyboard-based**, which is the weakest possible fallback for this
  project's users, some of whom cannot reliably use a keyboard.

### What is not being done

A privileged broker service was considered and rejected. The broker would
have to serve the same unprivileged caller, so deciding which callers to
trust degrades to image path or signature checks, which a process running as
that user defeats by injecting into the real Nimbus or by driving it. That
**relocates the trust boundary without closing it**, at the cost of a
service, an IPC surface and a new attack surface.

### Before the first release that ships this

- Scope the ACL to a **specific SID recorded at install**, rather than to
  `IU`. That names who instead of what kind of session, closes the
  cross-session case, and leaves the unavoidable case (code running as the
  user) exactly where it already was. A token session check in
  `EvtDeviceFileCreate` is the alternative; the callback slot is currently
  unused.
- The exclusive device means that while anything holds it, nobody can even
  call `IOCTL_NIMBUS_GET_STATUS`, so the condition **cannot be diagnosed**.
  Splitting status onto a non-exclusive read-only path would let a support
  script report that isolation is on and held by another process.

### Attestation

None of this blocks submission. Exploiting any of the above requires the
driver to be installed by choice, and an attacker with the administrator
rights needed to install a driver does not need this one. The exposed surface
is small: both control codes are `METHOD_BUFFERED`, reads use
`WdfDeviceIoBuffered`, no user-mode pointer is dereferenced, and 19 malformed
request cases have been fuzzed. This is **a policy question, not a
memory-safety one**, and a denial of service rather than an elevation of
privilege.

## If it gets stuck

| Symptom | What happened | Recovery |
|---|---|---|
| Mouse dead right after `install-dev.ps1`, script reported a rollback | Driver did not load (signature, test signing, or a load-time bug) | Nothing to do; the rollback already removed the entry. Replug the mouse if it has not come back. Read the reason the script printed. |
| Mouse dead, no rollback (script interrupted, or `-NoRollback`) | `UpperFilters` still names a driver that will not start | Keyboard: `Win+X`, `A` for an elevated PowerShell, run `driver\recover-mouse.ps1`, which detaches the filter and restarts the mice (`uninstall-dev.ps1` also works and removes the whole dev install). Or in `regedit`, under `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E96F-E325-11CE-BFC1-08002BE10318}`, edit `UpperFilters` so it reads only `mouclass` (the list normally holds `nimbus_moufilter` above `mouclass`; **`mouclass` must stay**, it is the mouse class driver itself). |
| Mouse dead after a reboot or a Windows update, with an install that had been working | The machine stopped accepting the test-signed driver while the filter stayed registered, so no mouse device starts: problem code 52, Kernel-PnP 219 with `0xC0000428`. A remote desktop tool still works and hides how total this is | Elevated `driver\recover-mouse.ps1`, no reboot needed. Then read [If the machine stops being able to load the driver](#if-the-machine-stops-being-able-to-load-the-driver); the `NimbusMouseFilterGuard` task exists to repair this by itself, so if it did not, check the guard's log at `%ProgramData%\ProjectNimbus\logs\mouse-guard.log`. |
| Blue screen when a mouse starts (possibly at every boot) | A bug in the filter | Windows opens the recovery environment after two failed boots (or hold Shift while clicking Restart). Troubleshoot, Advanced options, System Restore, pick the "Before Nimbus Mouse Filter dev install" point. Alternative from the recovery Command Prompt: `reg load HKLM\sys C:\Windows\System32\config\SYSTEM`, then `reg add "HKLM\sys\ControlSet001\Control\Class\{4D36E96F-E325-11CE-BFC1-08002BE10318}" /v UpperFilters /t REG_MULTI_SZ /d mouclass /f`, then `reg unload HKLM\sys`. Never delete the value outright: `mouclass` has to remain in it. |
| Cursor frozen while Nimbus is running | Isolation is on without the cursor relay, or the relay's reader thread is stuck | With the relay the cursor should keep moving; frozen means the reader is not running. `Ctrl+Alt+F12` releases (polled by the client). Or close Nimbus (Alt+F4 or Task Manager from the keyboard); closing the handle releases immediately. The watchdog releases within 2 s if Nimbus stops issuing reads, including when it is frozen (interface v3). |
| Mouse dead on the lock screen or a UAC prompt while Nimbus is in Game Mode | The client's secure-desktop pause did not kick in (it checks the input desktop every 100 ms) | The keyboard works: unlock or dismiss the prompt from it and the mouse is back on the desktop. Then file the bug; `Ctrl+Alt+F12` cannot reach the secure desktop. |
| Cursor frozen and Nimbus is gone | Should not happen (handle cleanup clears isolation) | Replug the mouse (a fresh device instance), or reboot; the flag does not survive a driver reload. Then file the bug with the output of `--status`. |
| Mouse dead and Nimbus is not the one holding it | Some other process opened the control device and is isolating (see "Security model"): the watchdog will not release it while that process keeps reading, and `Ctrl+Alt+F12` is polled by Nimbus, not by the holder | The device is exclusive, so `--status` cannot answer while it is held. Find the owner of a handle to `\Device\NimbusMouseFilter` (Process Explorer, "Find Handle or DLL", or `handle.exe NimbusMouseFilter`) and end that process from the keyboard; handle cleanup releases immediately. Failing that, elevated `driver\uninstall-dev.ps1` and replug the mouse. |
| "Test Mode" watermark, anti-cheat games refuse to start | Test signing is on | Elevated `driver\disable-testsigning.ps1`, then reboot. Do this before playing EAC/BattlEye/Vanguard titles. **Do not just run `bcdedit /set testsigning off`**: the dev driver cannot load without test signing, and a filter that is registered and cannot load means the mice do not start at all, so you would reboot into a machine with no mouse. The script detaches the filter first, which is the only ordering that is safe. |
