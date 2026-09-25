# Game mouse-input paths on Windows, what user-mode tricks reach each, and anti-cheat stance

Scope note: research done 2026-09-25. All web content was treated as data. Project measurements quoted below (WH_MOUSE_LL does not stop WM_INPUT; Elden Ring stops reading the pad when it loses foreground) come from the task brief, not from a web source. Separator style: each finding ends with its source in parentheses.

## 1. Which input paths do games use to read the mouse?

### Takeaway
Modern PC games read the mouse almost entirely through Raw Input (WM_INPUT or GetRawInputBuffer); DirectInput mouse is itself a thread reading WM_INPUT, SDL2/SDL3 relative mode and Unreal's high-precision mode are Raw Input, and GameInput is Microsoft's newer path whose exact sourcing on PC is undocumented. Only older or menu/cursor-driven code uses WM_MOUSEMOVE/GetCursorPos with recentering.

### Cited Findings
- Microsoft lists three primary methods for games to get mouse movement: WM_MOUSEMOVE, WM_INPUT, DirectInput ([MS Learn, Taking Advantage of High-Definition Mouse Movement, ms.date 2018-05-31, updated 2025-03](https://learn.microsoft.com/en-us/windows/win32/dxtecharts/taking-advantage-of-high-dpi-mouse-movement)).
- WM_MOUSEMOVE is limited to screen resolution and has pointer ballistics applied; "WM_INPUT messages are read directly from the Human Interface Device (HID) stack and reflect high-definition results"; the recommended method for camera control is WM_INPUT ([same page](https://learn.microsoft.com/en-us/windows/win32/dxtecharts/taking-advantage-of-high-dpi-mouse-movement)).
- "Internally, DirectInput creates a second thread to read WM_INPUT data"; Microsoft discourages DirectInput for mouse/keyboard ([same page](https://learn.microsoft.com/en-us/windows/win32/dxtecharts/taking-advantage-of-high-dpi-mouse-movement)). Its sample uses `DISCL_NONEXCLUSIVE | DISCL_FOREGROUND` cooperative level (same page).
- The same Microsoft sample registers the mouse with `RIDEV_INPUTSINK` and a target hwnd, i.e. a game copying the sample receives raw mouse even in the background ([same page](https://learn.microsoft.com/en-us/windows/win32/dxtecharts/taking-advantage-of-high-dpi-mouse-movement)).
- Raw input has two read methods: standard (GetMessage + GetRawInputData per WM_INPUT) and buffered (GetRawInputBuffer drains all accumulated events, recommended for 1000 Hz mice) ([MS Learn, Raw Input Overview, ms.date 2025-07-14](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input)).
- A 2024 engine-developer postmortem (PH3, Peter "Durante" Thoman) confirms shipped games use buffered raw input, and notes "Raw input affects the entire process, even though the API takes a window handle"; RIDEV_NOLEGACY breaks window-management interaction, while leaving legacy messages on caused FPS to collapse from ~360 to ~133 with an 8 kHz mouse ([PH3 blog, 2024-09-16](https://ph3at.github.io/posts/Windows-Input/)).
- SDL2 relative mouse mode uses Raw Input by default; `SDL_HINT_MOUSE_RELATIVE_MODE_WARP=1` forces cursor warping, and SDL2 falls back to warping on Windows if RegisterRawInputDevices fails ([SDL2 wiki](https://wiki.libsdl.org/SDL2/SDL_HINT_MOUSE_RELATIVE_MODE_WARP)).
- SDL3 removed the warp hint as rarely tested and complicating mouse handling ([SDL PR #11668](https://github.com/libsdl-org/SDL/pull/11668)); SDL3 relative mode is set per window with SDL_SetWindowRelativeMouseMode ([SDL3 wiki](https://wiki.libsdl.org/SDL3/SDL_SetWindowRelativeMouseMode)).
- Unreal Engine's `FReply::UseHighPrecisionMouseMovement` enables "high precision (raw input) mouse movement"; releasing mouse capture deactivates it ([Epic docs, UE 5.8](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/SlateCore/FReply/UseHighPrecisionMouseMovement?lang=en-US)). UE4 also had a separate RawInput plugin (for joysticks/HID) ([UE 4.27 docs](https://docs.unrealengine.com/4.27/en-US/InteractiveExperiences/Input/RawInput)).
- GameInput: described as a "functional superset" of XInput, DirectInput, Raw Input, HID and WinRT, "built directly on top of the Windows hardware interfaces for the lowest possible input latency"; available on PC via NuGet back to Windows 10 19H1; Microsoft says to use it "for all new code" ([MS Learn, GameInput introduction, edited 2026-02-19](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/common/input/overviews/input-overview)).
- GameInput mouse is reported per physical mouse as accumulated deltas (positionX/Y are not screen coordinates), and "The accumulated delta value is only for the mouse events that a process receives while it has input focus" ([MS Learn, GameInput keyboard and mouse, updated 2026-07](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/common/input/advanced/input-keyboard-mouse?view=gdk-2510)).
- On PC, GameInput input goes to the focused application by default and this is changeable with SetFocusPolicy (search-result summary of [GameInput fundamentals](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/common/input/overviews/input-fundamentals?view=gdk-2510); page not fetched in full).

### Inferences
- Path map (synthesis of the above):

| Path | Data source | Focus rule | Used by |
|---|---|---|---|
| WM_INPUT / GetRawInputBuffer | HID/mouse class stack via win32k raw input | foreground (keyboard focus) by default; background with RIDEV_INPUTSINK | most FPS/3D games, SDL2/3 relative mode, UE high-precision mode |
| DirectInput8 mouse | a DirectInput thread reading WM_INPUT | DISCL_FOREGROUND typical | older titles |
| WM_MOUSEMOVE + SetCursorPos recenter / ClipCursor | legacy cursor pipeline (post-ballistics) | window under cursor or capture | old titles, menus, SDL2 warp fallback |
| GetCursorPos polling | system cursor position | none (global) | menus, RTS edge-scroll |
| GameInput | "Windows hardware interfaces" (undocumented internals) | focused process by default, SetFocusPolicy | newer GDK/Xbox-PC titles |

- Because the game must stay foreground (Elden Ring drops the pad otherwise, per the brief), every foreground-gated path above is "open" to the physical mouse. The isolation scheme cannot rely on the game losing focus.

### Gaps
- Whether GameInput on PC reads mice below win32k Raw Input (e.g. through its own service or driver) or on top of it is not stated in any Microsoft page I found. This decides whether a user-mode raw-input trick could ever affect GameInput games; only the kernel filter approach is safe to assume covers it.
- Unity's legacy Input Manager and new Input System Windows backends: no primary source found on whether they use WM_INPUT (a [Unity forum thread](https://discussions.unity.com/t/new-input-system-get-raw-mouse-delta-from-wm_input/857382) suggests users had to add raw deltas themselves; not verified).
- SDL3's optional GameInput backend for mouse/keyboard: not verified.

## 2. Which user-mode tricks affect which path (WH_MOUSE_LL, SendInput, ClipCursor/SetCursorPos)?

### Takeaway
WH_MOUSE_LL blocking stops the legacy cursor pipeline (cursor movement, WM_MOUSEMOVE, clicks) but not WM_INPUT, so it cannot silence Raw Input, DirectInput, SDL relative mode or Unreal high-precision games. SendInput injects into the cursor pipeline and can show up in raw input with hDevice = 0, which games and anti-cheats can distinguish and some games handle badly.

### Cited Findings
- Project measurement (task brief): a WH_MOUSE_LL hook dropping 100% of events does not stop WM_INPUT reaching the game.
- Consistent community report: a WH_MOUSE_LL hook works on the desktop and in paused menus but not in-game while aiming, because the game uses raw input; users turn to the Interception driver (AutoHotInterception) to block raw movement ([AutoHotkey forum](https://www.autohotkey.com/boards/viewtopic.php?style=19&t=117743)).
- Mouse and keyboard data "can come through both the HID message WM_INPUT and from traditional messages"; the app selects via RAWINPUTDEVICE flags ([Raw Input Overview](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input)).
- RIDEV_NOLEGACY stops WM_LBUTTONDOWN/WM_MOUSEMOVE etc. being generated "for the application" (a per-process setting, not system-wide) ([RAWINPUTDEVICE](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputdevice)).
- RAWINPUTHEADER.hDevice is 0 for injected input, and can also be 0 for precision-touchpad input (search-result summary of [RAWINPUTHEADER docs](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputheader) and a [GuidedHacking thread on detecting SendInput](https://guidedhacking.com/threads/more-ways-to-detect-sendinput.14823/); neither fetched in full).
- A May 2026 bug report on Apollo (game streaming host) says games using raw input (FiveM / GTA V) get an unusable camera from SendInput-injected mouse and that "SendInput cannot replicate this behavior from userspace; it always goes through the Win32 cursor pipeline"; NVIDIA GameStream (signed kernel HID driver injecting MOUSE_INPUT_DATA into the mouhid stack) and Parsec (parsecvusba virtual USB driver) work ([Apollo issue #1479, opened 2026-05-01](https://github.com/ClassicOldSong/Apollo/issues/1479)). Caveat: the issue attributes the failure to absolute-coordinate packets in Apollo's own path; it is one report, not a controlled test.
- Raw input lets an application "distinguish the source of the input even if it is from the same type of device", via hDevice and GetRawInputDeviceInfo ([Raw Input Overview](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input)).

### Inferences
- Effect matrix (synthesis; rows are tricks, columns are paths):

| Trick (user mode) | WM_INPUT / RawInputBuffer | DirectInput8 mouse | WM_MOUSEMOVE / GetCursorPos | GameInput |
|---|---|---|---|---|
| WH_MOUSE_LL returning nonzero | not blocked (measured) | not blocked (it reads WM_INPUT) | blocked (cursor does not move, no messages) | unknown, assume not blocked |
| ClipCursor / SetCursorPos pinning | not affected (raw deltas are pre-cursor) | not affected | defeats it (cursor stuck) | unknown, assume not affected |
| Hiding/moving the cursor only | not affected | not affected | partly | not affected |
| RIDEV_NOLEGACY in Nimbus process | only affects Nimbus's own legacy messages | no | no | no |
| SendInput to drive a second cursor | adds hDevice=0 raw events to the game if it is foreground and registered | same | moves the one system cursor | unknown |

- Consequence for "two cursors": if Nimbus drives its own cursor via SendInput while the game is foreground, those synthetic moves would themselves reach the game's raw input stream (hDevice = 0) unless the game filters hDevice = 0, so SendInput-based second cursors are worse than drawing a software cursor and hit-testing it inside Nimbus.
- A software cursor fed by Nimbus's own RIDEV_INPUTSINK registration is compatible with a WH_MOUSE_LL block (keeps the system cursor still over the game), but the game still sees WM_INPUT, which is exactly the measured failure.

### Gaps
- LLMHF_INJECTED semantics were not re-verified from MS docs in this pass (the MSLLHOOKSTRUCT page was not fetched); it is widely known to flag SendInput-originated events to LL hooks, and games or anti-cheats could check it via their own LL hook, but I have no source showing a specific anti-cheat acting on it.
- No controlled measurement found of which shipped games drop hDevice = 0 raw events.

## 3. Is Raw Input delivery foreground-only, and can any process take a device exclusively?

### Takeaway
By default raw input goes to the window with keyboard focus; RIDEV_INPUTSINK adds background delivery without removing foreground delivery, and RIDEV_EXINPUTSINK only fires when the foreground app is not registered. There is no documented flag that makes one process the exclusive receiver of a mouse, so a background process cannot starve the foreground game via Raw Input.

### Cited Findings
- "By default, the operating system sends raw input from devices with the specified TLC to the registered application as long as it has the window focus"; if hwndTarget is NULL, "raw input events follow the keyboard focus" ([RAWINPUTDEVICE](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputdevice)).
- RIDEV_INPUTSINK: "enables the caller to receive the input even when the caller is not in the foreground" (hwndTarget required) (same page).
- RIDEV_EXINPUTSINK: "receive input in the background only if the foreground application does not process it. In other words, if the foreground application is not registered for raw input, then the background application that is registered will receive the input" (same page).
- RIDEV_CAPTUREMOUSE: "the mouse button click does not activate the other window"; only valid with RIDEV_NOLEGACY for a mouse (same page).
- The full documented flag list is REMOVE, EXCLUDE, PAGEONLY, NOLEGACY, INPUTSINK, CAPTUREMOUSE, NOHOTKEYS, APPKEYS, EXINPUTSINK, DEVNOTIFY; none describes exclusive ownership (same page).
- "An application can receive data when it is in the foreground and when it is in the background (if registered with RIDEV_INPUTSINK)" ([Raw Input Overview](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input)).
- Raw input affects the whole process regardless of the hwnd passed ([PH3 blog](https://ph3at.github.io/posts/Windows-Input/)).

### Inferences
- Nimbus can register INPUTSINK to read the physical mouse while the game is foreground (it already can), but this is additive: the foreground game receives the same events. No user-mode registration diverts them.
- RIDEV_CAPTUREMOUSE plus NOLEGACY in Nimbus does not help: it only affects Nimbus's own window activation behavior, and Nimbus is already WS_EX_NOACTIVATE.
- The only way to make the game see nothing is to remove the events before win32k's raw input thread sees them: a mouse-class filter (current Nimbus driver), an existing third-party filter (Interception), or hiding the device (HidHide-style, which works at the HID/PnP layer and is designed for gamepads). All are kernel components.

### Gaps
- No source found on whether HidHide can hide a mouse from win32k's raw input (it hides devices from user-mode enumeration by process; the system's own mouse stack is kernel-side, so likely not). Unverified.

## 4. Touch / pen (WM_POINTER) injection: do games treat it as mouse, and does it steal focus?

### Takeaway
Synthetic pen/touch via InjectSyntheticPointerInput produces WM_POINTER messages that DefWindowProc promotes to WM_MOUSE* for the target window, so it goes to the window under the contact (Nimbus), not to the foreground game's raw mouse stream; a primary pointer contact may activate a window, which WS_EX_NOACTIVATE should prevent. Whether touch promotion also moves the system cursor or produces raw mouse input is not well documented.

### Cited Findings
- InjectSyntheticPointerInput simulates pen or touch input through a device from CreateSyntheticPointerDevice ([MS Learn](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-injectsyntheticpointerinput)).
- When a WM_POINTER event reaches DefWindowProc it is "promoted" to WM_MOUSE*; promotion only works for events Windows actually sent, i.e. real devices or the synthetic pointer API ([jchv/WmPointerDemo notes](https://github.com/jchv/WmPointerDemo/blob/master/Syscalls.md); community reverse-engineering, not Microsoft).
- The "Primary" pointer flag: when a primary pointer contacts a window, "it might provide the window an opportunity to activate" ([MS Learn, InjectedInputPointerOptions](https://learn.microsoft.com/en-us/uwp/api/windows.ui.input.preview.injection.injectedinputpointeroptions)).
- Mouse messages generated from pen/touch carry MI_WP_SIGNATURE: `GetMessageExtraInfo() & 0xFFFFFF00 == 0xFF515700` ([MS Learn, System Events and Mouse Messages](https://learn.microsoft.com/en-us/windows/win32/tablet/system-events-and-mouse-messages)); a Q&A reports the signature missing in some cases, notably RDP-simulated touch ([Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/285548/getmessageextrainfo-signature-missing-for-touch-in)).
- Touchscreens are HID digitizers (usage page 0x0D), a different TLC from the generic-desktop mouse (0x01/0x02) that games register ([RAWINPUTDEVICE, usage page links](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputdevice)).

### Inferences
- A "second cursor" built on synthetic pen/touch aimed at the Nimbus window would give Nimbus clicks without moving the game's mouse stream, but it does not solve the core problem: the physical mouse that the user moves to steer that second cursor still reaches the game's WM_INPUT.
- Games that only register the mouse TLC (0x01/0x02) should not receive digitizer raw input; promoted mouse messages go to the window under the contact.

### Gaps
- No authoritative source on whether touch-promoted mouse input moves the system cursor or generates RIM_TYPEMOUSE raw input on Windows 10/11; the 2012 [Witness blog](http://the-witness.net/news/2012/10/wm_touch-is-totally-bananas/) is stale and ambiguous. Needs a local probe.
- No source on how games react to injected pointer input specifically, or on anti-cheat treatment of InjectSyntheticPointerInput.

## 5. Do games stop reading the mouse when the cursor is over another topmost window or outside their window?

### Takeaway
For Raw Input, DirectInput and GameInput games, cursor position is irrelevant: delivery is gated by foreground/focus, not by which window is under the cursor. Only WM_MOUSEMOVE-based games (and menus) care about the cursor being over their window. I found no systematic measurement across titles.

### Cited Findings
- Raw input follows keyboard focus by default and is not described as depending on cursor position ([RAWINPUTDEVICE](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputdevice)).
- GameInput accumulates mouse deltas only while the process has input focus ([GameInput keyboard and mouse](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/common/input/advanced/input-keyboard-mouse?view=gdk-2510)).
- Unreal ties high-precision (raw) mouse mode to mouse capture; releasing capture ends it ([Epic docs](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/SlateCore/FReply/UseHighPrecisionMouseMovement?lang=en-US)).

### Inferences
- In borderless windowed mode with an unclipped cursor, a raw-input game still turns the camera while the user's cursor is over a no-activate Nimbus window, which matches the project's observation.
- Some engines (UE when capture is lost, games that release relative mode when the cursor is shown) may stop reading mouse while their own UI shows a cursor; this is game-specific and cannot be relied on.

### Gaps
- No published cross-title survey of "cursor over another window" behavior was found; this needs per-game testing with the project's harness.

## 6. Anti-cheat stance on the relevant techniques

### Takeaway
Test-signing mode is a hard block for EAC, BattlEye and Vanguard (and the Nimbus test-signed driver needs it), which makes the current driver unusable with most competitive online games. Vanguard specifically refuses to start with Interception loaded. ViGEmBus/DS4Windows is generally tolerated by EAC and BattlEye but has had blocks, and Vanguard is the most restrictive. BattlEye kicks rather than bans for non-cheat tools like macros. No vendor publishes an accessibility exception.

### Cited Findings
- EAC: test-signing mode is not supported; users must disable it or obtain a signed driver ([Facepunch support](https://support.facepunchstudios.com/hc/en-us/articles/214240705-Driver-Signature-Enforcement-has-been-disabled); Steam threads for [7 Days to Die](https://steamcommunity.com/app/251570/discussions/0/624076027695934731/) and [Magicka](https://steamcommunity.com/app/202090/discussions/0/624074858771349474/), older).
- BattlEye: "cannot support systems running in test-signing mode"; kernel debugging also must be disabled; blocks drivers "which contain known security issues that can be exploited by cheats"; overlays like ReShade are "generally supported unless desired otherwise by the game developers"; it "might decide to kick (not ban) you" for macro tools, and bans only for actual cheats ([BattlEye FAQ](https://www.battleye.com/support/faq/)). Error text: "Failed to initialize BattlEye Service: Windows Test-Signing Mode not supported." ([Epic Games help](https://www.epicgames.com/help/c-202300000001619/c-202300000001668/failed-to-initialize-battleye-service-windows-test-signing-mode-not-supported-error-a202300000012901)).
- Vanguard: checks that test-signing is disabled and driver signature enforcement is on; `bcdedit -set TESTSIGNING OFF` and `NOINTEGRITYCHECKS OFF` are the fixes ([KeenGamer troubleshooting article](https://www.keengamer.com/articles/features/troubleshooting/5-ways-to-fix-the-valorant-vanguard-not-initialized-error-on-windows-pcs/), secondary; Riot's [Vanguard error codes page](https://support.riotgames.com/en-us/riot/performance/vanguard-error-codes) not fetched).
- Vanguard and Interception: after a 2020 hotfix Riot stopped blocking most drivers, but the Interception driver "is still not supported"; the game will not launch while it is loaded ([devtrackers.gg Riot post, 2020](https://devtrackers.gg/valorant/p/d7d37c44-when-this-post-is-1-hours-old-riot-will-release-a-hotfix-to-vanguard-to-stop-vanguard-from-blocking-drivers-in-most-cases); [GINX](https://www.ginx.tv/en/valorant/valorant-vanguard-hotfix-mouse-keyboard-input-device-driver)). Stale (2020); current status not re-verified.
- Vanguard also blocks mouse input in RDP streaming sessions (a 2026 bug report) ([Apollo issue #1471](https://github.com/ClassicOldSong/Apollo/issues/1471), title only).
- FACEIT: blocks drivers with known vulnerabilities ("Forbidden driver") and has an "Incompatible driver detected" error tied to DMA remapping requirements ([FACEIT forbidden driver](https://support.faceit.com/hc/en-us/articles/360014237259--Forbidden-driver-error-message-and-blocked-drivers); [FACEIT incompatible driver](https://support.faceit.com/hc/en-us/articles/25296204531996-Incompatible-driver-detected)); pages not fetched in full.
- ViGEmBus/DS4Windows: EA forum reports of anticheat blocking DS4Windows in Battlefield V ([EA Forums](https://forums.ea.com/discussions/battlefield-v-en/anticheat-blocking-ds4windows/6847116)); a fan site says EAC and BattlEye generally tolerate DS4Windows while Vanguard may block controller emulation ([ds4win.com](https://ds4win.com/ds4windows-get-you-banned/), low-authority secondary source).
- The Interception driver is "very low level, so it fools more things into thinking faked input really is coming from actual devices", which is why anti-cheats target it ([UCR wiki, Core Interception](https://github.com/snoothy/ucr/wiki/Core_Interception)). An EA forum thread discusses Interception with Apex (EAC) ([EA Forums](https://forums.ea.com/discussions/apex-legends-technical-issues-en/interception/12261311), not fetched).
- Kernel anti-cheats enumerate loaded drivers and flag boot-chain tampering (test signing, DSE off) ([s4dbrd, How Kernel Anti-Cheats Work](https://s4dbrd.github.io/posts/how-kernel-anti-cheats-work/), independent researcher blog).

### Inferences
- Risk table (synthesis; "block" = game refuses to start, not a ban):

| Technique | EAC | BattlEye | Vanguard | FACEIT | VAC | Ricochet |
|---|---|---|---|---|---|---|
| Test-signing on (needed for current Nimbus driver) | block | block | block | likely block (unverified) | no source | no source |
| Production-signed Nimbus mouse filter (after attestation/EV signing) | likely allowed unless flagged | allowed unless on vulnerable-driver list | case by case, Riot allowlists | allowed unless on blocklist | n/a (user mode) | no source |
| Interception driver | reported issues | risk of kick | block (game will not start) | no source | no source | no source |
| HidHide | no reports found | no reports found | no source | no source | no source | no source |
| ViGEmBus virtual pad | generally tolerated, occasional blocks | generally tolerated | may block emulation (weak source) | no source | tolerated (Steam Input exists) | no source |
| SendInput / LLMHF_INJECTED input | no official stance found | macro tools may be kicked, not banned | no source | no source | no source | no source |
| Synthetic pointer injection | no source | no source | no source | no source | no source | no source |
| Topmost overlay window (Nimbus UI) | no source | non-cheat overlays generally supported | no source | no source | no source | no source |

- For Nimbus: the biggest anti-cheat blocker is test-signing itself, not what the filter does. A user-mode "two cursors" scheme avoids that, but no user-mode scheme found here stops WM_INPUT reaching a foreground raw-input game, so it trades anti-cheat compatibility for not solving the core problem in raw-input titles.
- A properly signed (attestation or WHQL) filter driver removes the test-signing block; it can still be flagged individually by a vendor, but that is a per-driver allowlist conversation rather than a structural block.

### Gaps
- VAC and Ricochet: no sources found on test-signing, virtual pads, Interception, or injection. VAC is widely described as user mode, but no primary source was fetched.
- No vendor (EAC, BattlEye, Vanguard, FACEIT, VAC, Ricochet) publishes an accessibility exception or allowlist program that I could find; the searches returned only user discussions.
- No official statement from any vendor on LLMHF_INJECTED, synthetic pointer input, or HidHide.
