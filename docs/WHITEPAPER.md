# Nimbus Adaptive Controller

### A Modular, Mouse-Driven Virtual Game Controller for Accessible Computing

**Technical White Paper** — Version 1.0
Owen Kent · May 2026

---

## Abstract

The Nimbus Adaptive Controller is a free, open-source software platform that synthesises virtual joystick output from arbitrary pointer-class input devices on Windows and Linux. It targets a class of users for whom commercially available game controllers are physically inaccessible, and it does so without dedicated hardware: any mouse, trackball, head tracker, eye tracker, or alternative HID device that produces 2-D pointer events can be re-shaped, through a fully user-configurable layout, into DirectInput (vJoy) or XInput (ViGEm) controller output consumable by any Windows game or simulation. This paper describes the system architecture, the input-processing pipeline, the modular layout engine, the persistence model, and the design decisions behind treating the user interface itself — rather than the physical input device — as the primary point of customisation. We argue that this inversion is the principal contribution of the project: by making the *layout* programmable and the *input device* fixed, Nimbus delivers an accessibility surface comparable in flexibility to the Xbox Adaptive Controller at zero hardware cost, and provides a foundation on which voice control, AI-assisted execution, AAC, and adaptive-hardware bridging can be layered as software extensions of the same control surface.

---

## 1. Introduction

Mainstream game controllers — DualShock, Xbox Wireless, Joy-Con — assume a user with bilateral fine motor control over both hands and bilateral thumb dexterity. Users who do not match that profile have historically had two options. They can buy specialised hardware, of which the Xbox Adaptive Controller (XAC) is the dominant example: a £75 hub with 3.5 mm jacks for switches and analog inputs, around which a constellation of additional QuadStick-class joysticks, sip-and-puff devices, foot pedals, and proximity switches must be assembled. A complete XAC rig commonly exceeds £500 and requires sighted or assisted setup. Alternatively, users can attempt to drive a standard controller through generic HID-remapping utilities (reWASD, JoyToKey, Xpadder), which were not designed for accessibility, lack documentation aimed at non-developers, and do not provide a configurable visual surface that the user themselves can manipulate.

Nimbus takes a third approach. The user keeps whatever pointer device they already use to operate Windows — a mouse, a trackball, a head-tracking camera, an eye-gaze system, a switch-driven cursor — and uses it to interact with an on-screen, fully customisable controller layout. Each on-screen widget (joystick, button, slider, D-pad, steering wheel) is mapped to a virtual axis or button on a kernel-level virtual controller driver. Games and simulators see a standard DirectInput or XInput device; they have no way of knowing the underlying input is a mouse moving across a window.

This paper documents how that pipeline is constructed, what design choices it embodies, and what it implies for the wider problem space of accessible computing.

---

## 2. Goals and Non-Goals

### 2.1 Goals

- **Hardware independence.** Operate with any device that produces standard Windows pointer events. No bespoke hardware, no driver development, no firmware.
- **Layout programmability.** The on-screen surface is the unit of configuration. Users place, resize, label, colour, and remap widgets without touching code or JSON.
- **Game compatibility parity.** The output device must be indistinguishable from a real DirectInput or XInput controller to the game.
- **Persistence and portability.** Configurations are user-readable JSON, stored in standard OS data directories, trivially backed up, shared, and version-controlled.
- **Zero-cost accessibility.** The core platform is MIT-licensed and will remain free for accessibility use indefinitely.

### 2.2 Non-Goals

- **macOS.** Windows (vJoy, ViGEm) and Linux (kernel `uinput`, § 6.3) are supported runtimes. macOS has no comparable kernel facility for virtual HID devices and is not supported.
- **Replacement of physical adaptive hardware.** Users who already own a QuadStick or sip-and-puff device should continue to use it; Nimbus aims to *wrap* such devices, not displace them.
- **A general-purpose macro engine.** Nimbus emits HID-class output by design. Keyboard-output mode is on the roadmap (§ 12) but is downstream of the core controller pipeline.

---

## 3. System Overview

Nimbus is a Python 3.8+ application built on PySide6 (Qt 6) with a Qt Quick (QML) presentation layer and a small Python core. On Windows it drives one of two virtual drivers, vJoy through `pyvjoy` and ViGEmBus through its own pure-Python bus client (`src/padbus_client.py`); on Linux it drives the kernel `uinput` module directly through `ioctl` with no compiled dependency. Either way it exposes its runtime to QML through a single bridge object.

```
   ┌──────────────────────────────────────────────────────────────┐
   │                       User Pointer Device                    │
   │      (mouse, trackball, head tracker, eye gaze, switch)      │
   └────────────────────────────┬─────────────────────────────────┘
                                │  Windows pointer events
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                      Qt Quick (QML) UI                       │
   │   CustomLayout.qml ── DraggableWidget.qml × N                │
   │   joystick / button / slider / dpad / wheel                  │
   └────────────────────────────┬─────────────────────────────────┘
                                │  normalized axis / button events
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                ControllerBridge  (src/bridge.py)             │
   │      smoothing · curve application · failsafe · slots        │
   └────────────────┬───────────────────────────────┬─────────────┘
                    │                               │
                    ▼                               ▼
       ┌────────────────────────┐      ┌────────────────────────┐
       │   VJoyInterface        │      │   ViGEmInterface       │
       │   8 axes · 128 buttons │      │   2 sticks · 14 btns   │
       │   DirectInput          │      │   XInput (Xbox 360)    │
       └───────────┬────────────┘      └───────────┬────────────┘
                   │                               │
                   ▼                               ▼
   ┌──────────────────────────────────────────────────────────────┐
   │            Virtual Controller Kernel Driver                  │
   │              (vJoy.sys or ViGEmBus.sys)                      │
   └────────────────────────────┬─────────────────────────────────┘
                                │  HID device enumeration
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │       Game / Simulator / MAVLink Ground Control Station      │
   └──────────────────────────────────────────────────────────────┘
```

The architectural intent is that all *behaviour* lives in user-space Python and QML, and the kernel driver is treated as a dumb pass-through. The driver contributes HID enumeration; everything semantic — curves, deadzones, snap modes, smoothing, button toggling, failsafe — is implemented above the driver and is therefore inspectable, testable, and modifiable by the user.

---

## 4. Input-Processing Pipeline

A single user-visible movement on a joystick widget passes through six discrete stages before reaching the game. Understanding these stages is essential because each stage exists to address a specific failure mode observed during accessibility testing.

### 4.1 Capture

QML `MouseArea` items report pointer position in widget-local pixel coordinates. The widget computes a normalised vector `(nx, ny)` in the unit disc with `(0, 0)` at the centre. Joysticks support a *relative drag* model — the cursor does not jump to the click point — to accommodate users who cannot reliably hit a small target on first contact.

### 4.2 Triple-Click Lock

A user with limited grip strength cannot hold a mouse button down for the duration of a flight, drive, or game level. Nimbus implements a *triple-click lock*: three rapid clicks on a joystick toggle a global capture mode. While locked, the joystick tracks raw cursor position relative to the window via a canvas-spanning `MouseArea` overlay, and a fourth triple-click releases it. Lock state is per-widget, allowing a user to dual-stick across two locked joysticks if their pointer device permits.

### 4.3 Deadzone

Two deadzones are applied to the stick vector's magnitude, so the dead region is a circle and diagonals respond like cardinals:

- **Centre deadzone** — a circular region around the origin in which output is forced to zero. This eliminates tremor, switch chatter, and the unintentional micro-movements typical of head- and eye-tracking systems.
- **Extremity deadzone** — a scaling factor that reduces the maximum reachable output. This prevents over-travel for users who cannot consistently *avoid* the rim of a joystick widget, and is the symmetric counterpart of the centre deadzone.

Both deadzones are configured as percentages (0–100 %) and stored per widget in the profile JSON.

A third control works on the other side of the driver. Most games discard stick input below an inner deadzone of their own (24 % and 26.5 % for the documented XInput constants), which would swallow the first quarter of the widget's travel. The **output anti-deadzone** lifts the smallest non-zero output to that floor, so the smallest movement the user can make is the smallest movement the game will accept, and the game's own aim assistance, which engages only above its deadzone, sees the input at all. It defaults to the XInput constants when the output is an Xbox controller and is calibrated per widget against a running game.

### 4.4 Sensitivity Curve

Post-deadzone, each axis is shaped by a sensitivity curve. Nimbus uses a symmetric power curve parameterised by a single sensitivity slider:

```
sensitivity ∈ [0, 100]
exponent = sensitivity_to_exponent(sensitivity)
output  = sign(input) · |input| ^ exponent
```

A sensitivity of 50 % yields an exponent of 1.0 (linear). Values below 50 % flatten the curve near the centre, providing fine control for small movements at the cost of speed at the rim. Values above 50 % steepen the curve, giving rapid response near centre — desirable for combat games or rover yaw control, but unsuitable for sustained precision work like a UAV gimbal. The formula is implemented once, in `config.py` (`shape_magnitude`), and applied by the bridge; the dialog's live preview asks the bridge for the curve's points, so what is drawn is what the driver receives.

### 4.5 Smoothing

The bridge maintains, per axis, a target value updated by the QML layer at the user's input rate, and an interpolated *current* value advanced by a `QTimer` at a fixed 60 Hz. Output is sent to the driver from the interpolated value. Smoothing factor is bounded; an upper bound prevents perceptible lag, and a lower bound prevents the audible step-noise that a real driver would produce when fed a non-smoothed mouse trajectory.

### 4.6 Driver Emission

The smoothed, curved, deadzoned value, still in normalised `[-1, 1]` form, is mapped onto the driver's integer axis range and submitted via `pyvjoy.VJoyDevice.set_axis()` or `X360Pad.left_joystick_float()` from `src/padbus_client.py`. Buttons are submitted as boolean state changes; toggle-mode buttons are debounced at the QML layer before reaching the bridge.

### 4.7 Failsafe

A watchdog observes the inter-event arrival time on the bridge. If no axis update is received within a configurable timeout (default 5 s) — for instance because the host application has frozen, the user's pointer device has disconnected, or a modal Windows dialog has stolen focus — every axis is forced to centre and every button is released. The intent is borrowed from MAVLink autopilot conventions: if the operator stops driving, the vehicle should stop responding, not continue at last commanded value.

---

## 5. The Modular Layout System

The architectural centrepiece of Nimbus is the *custom layout* — a JSON-defined widget canvas that supplants any notion of a fixed controller faceplate.

### 5.1 Schema

A custom layout is an array of widget records:

```jsonc
{
  "id": "left_stick",
  "type": "joystick",
  "x": 120, "y": 240,
  "width": 280, "height": 280,
  "label": "Move",
  "mapping": { "axis_x": "x", "axis_y": "y" },
  "sensitivity": 55,
  "dead_zone": 12,
  "extremity_dead_zone": 5
}
```

Widget types, their type-specific fields, and their mapping targets are summarised below:

| Type | Type-specific fields | Mapping target |
|------|----------------------|----------------|
| `joystick` | `mapping.axis_x`, `mapping.axis_y` | Two analog axes |
| `button` | `button_id`, `color`, `shape`, `toggle_mode` | One vJoy button (1–128) or XInput button |
| `slider` | `mapping.axis`, `orientation`, `snap_mode`, `click_mode` | One analog axis |
| `dpad` | `mapping.{up,down,left,right}` | Four buttons or a POV hat |
| `wheel` | `mapping.axis` | One rotational axis |

### 5.2 Edit Mode and Play Mode

The same `DraggableWidget.qml` component renders both a configurable widget and an interactive control. In edit mode it exposes a drag handle, a corner resize grip, a delete button, and a double-click target that opens a per-widget configuration dialog. In play mode all editing affordances are hidden and a `Loader` instantiates the appropriate interactive component (`joystickContent`, `buttonContent`, `sliderContent`, `dpadContent`, `wheelContent`). The mode toggle is exposed as a single button in the canvas footer, and changes auto-save on transition.

This dual-role component is one of the load-bearing simplifications in the system. Because every widget is rendered through the same wrapper, adding a new widget type requires defining only two QML `Component` blocks — its edit-mode appearance and its play-mode behaviour — and one entry in the configuration dialog. There is no central layout engine to modify, no state machine to extend, and no driver-side change.

### 5.3 The Widget Palette as an OS Window

The palette from which widgets are added is rendered as a separate top-level Qt `Window` with `Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint`, with its own draggable title bar. This is a deliberate departure from the more conventional embedded-sidebar approach. Two reasons:

1. **Canvas real estate.** A user designing a layout for a flight simulator needs the entire window area as canvas. An embedded palette consumes 200–300 px of horizontal space and forces the user to scroll their layout.
2. **Multi-monitor users.** The palette can be parked on a second monitor, leaving the primary monitor entirely as canvas.

The palette window auto-shows on entering edit mode and auto-hides on leaving it.

### 5.4 Persistence

Layouts are persisted on every meaningful state change — widget move, widget resize, configuration dialog dismiss, edit-mode exit — through `ControllerConfig.save_custom_layout()`. Persistence is synchronous and to disk, not in-memory; an OS crash between two state changes loses at most one widget operation. The trade-off (file I/O on every drag) is acceptable because the JSON is small (typically <10 KB) and writes are debounced upstream by the drag handler.

---

## 6. Output Backends

Nimbus supports two virtual controller drivers. The choice is per-profile and is auto-selected from the profile's declared `layout_type`. Profiles of type `xbox`, `adaptive`, or `custom` route through ViGEm (these are layouts a user is most likely to point at modern XInput-only titles), while `flight_sim` profiles route through vJoy, since flight and ground-control software depends on the larger DirectInput axis and button budget. The user can override the auto-selection at any time from the status ribbon, and `controller.prefer_vigem` in the config is consulted as a tiebreaker; vJoy is also used as a fallback when the ViGEmBus driver is absent. On Linux the same two modes are backed by `uinput` devices (§ 6.3); the mode names `vjoy` and `vigem` are kept as platform-neutral identifiers so profiles move between operating systems unchanged.

### 6.1 vJoy (DirectInput)

vJoy presents up to 16 virtual sticks, each with up to 8 axes (X, Y, Z, RX, RY, RZ, SL0, SL1), 128 buttons, and 4 POV hats. Nimbus binds to vJoy device 1 by default. The strength of vJoy is its capacity — DirectInput games and many simulators (DCS, MSFS, X-Plane, Mission Planner, ArduPilot Ground Control Stations) consume large axis and button counts that XInput cannot supply.

The weakness of vJoy is age and platform drift. Many modern Windows games — anything built on the assumption that *controller* means *Xbox controller* — ignore DirectInput devices entirely. No Man's Sky is the canonical example.

### 6.2 ViGEm (XInput)

ViGEmBus provides an in-kernel virtual Xbox 360 controller. Games receive an XInput device indistinguishable from a wired Xbox 360 pad. This solves the modern-games-don't-see-vJoy problem at the cost of a much smaller resource budget: 2 analog sticks, 2 triggers, and 14 buttons.

### 6.3 uinput (Linux)

Linux ships the `uinput` kernel module, which lets a process with write access to `/dev/uinput` create a virtual input device that SDL, Steam, Proton, and native games treat as hardware. `src/uinput_interface.py` provides two such devices, chosen by the same rule as above: `UInputXboxInterface` is an Xbox 360 pad carrying the vendor/product IDs the kernel `xpad` driver reports, so SDL and Steam Input apply their built-in mapping without configuration (verified: `SDL_IsGameController()` is true and every control maps correctly); `UInputJoystickInterface` is a generic 8-axis joystick with 56 buttons, the evdev limit for generic joystick codes. Only the active device is kept alive so games never see two Nimbus controllers. Access to `/dev/uinput` is granted by a udev rule (Steam installs an equivalent one); the same rule additionally tags Nimbus's own virtual devices with `uaccess` so their event nodes can be read back, which Steam's does not do and which the Linux diagnostics depend on.

### 6.4 Backend Indirection

The bridge owns a single controller-interface object whose concrete type is `VJoyInterface` or `ViGEmInterface` on Windows and `UInputJoystickInterface` or `UInputXboxInterface` on Linux. Both expose the same surface — `update_axis(name, value)`, `set_button(id, pressed)`, `center_all()`, `get_status()` — so the QML layer is unaware of which backend is active. Switching backend at runtime is a matter of profile reload.

---

## 7. Profile System

Profiles are versioned, JSON-serialised configurations stored under the platform's standard user-data directory:

| Platform | Path |
|----------|------|
| Windows  | `%APPDATA%\ProjectNimbus\profiles\` |
| macOS    | `~/Library/Application Support/ProjectNimbus/profiles/` *(path resolver only; macOS is not a supported runtime — see § 2.2)* |
| Linux    | `~/.local/share/ProjectNimbus/profiles/` (or `$XDG_DATA_HOME/ProjectNimbus/profiles/`) |

A profile carries every piece of information needed to reproduce the user's controller end-to-end: layout type, custom layout (if applicable), per-axis sensitivity and deadzone, button toggle states, axis mapping, and metadata (display name, description). On first run, the bundled `adaptive_platform_2.json` is copied into the user data directory; thereafter the user may duplicate, rename, modify, and delete profiles freely. There is no central server, no account requirement, and no upload; profile sharing is a matter of copying a JSON file.

This deliberate primitivity is itself an accessibility feature. A user who can navigate File Explorer can back up their profiles. A caregiver setting up a new machine can do so by copying one folder. Nothing about the profile format prevents inspection or modification with a text editor.

---

## 8. Borderless Gaming and Cursor Liberation

A latent obstacle to a mouse-driven controller is that many games re-confine the mouse cursor to their own window every frame using `ClipCursor()`, and many games run in exclusive full-screen mode that captures the cursor entirely. The user cannot reach the Nimbus window because the game has hidden it, and even if the Nimbus window is visible, the cursor cannot leave the game's bounds.

Nimbus solves this with a built-in borderless-gaming layer (`src/borderless.py`):

- **Window flattening.** `make_borderless()` strips `WS_CAPTION` and `WS_THICKFRAME` from the target window's style and resizes it to fill the monitor. The game now occupies the screen but does not own it; the Windows compositor still arbitrates focus and z-order.
- **Cursor liberation.** A dedicated, priority-elevated thread releases `ClipCursor(NULL)` on a tight loop. The default interval is 2 ms, chosen explicitly to outrace a 60 fps game's per-frame re-clip (~16 ms); a gentler 50–100 ms mode is exposed for titles that re-clip less aggressively or for which CPU cost matters more than latency. The thread also performs `AttachThreadInput` to the game's input thread so the release applies to the same input state the game is mutating, and force-expands the clip rectangle to the full virtual screen if the game has somehow won the race. This is still cooperative — the thread cannot prevent the next frame's re-clip, only undo it — but in practice cursor escape is below the perceptual threshold for jump.
- **Auto-detection.** A built-in compatibility table covering 30+ games is consulted on launch to suggest known-working configurations.

The trade-off is that this technique is detectable and could be misclassified by anti-cheat systems. Nimbus does not modify game memory, hook game APIs, or interfere with input on the game's side; it operates entirely on the Windows window/cursor layer, the same layer used by routine accessibility tools. To date no anti-cheat false positives have been reported, but the design choice to operate at the OS layer rather than at the game layer is deliberate and should be preserved.

### 8.1 The Raw Input tier

Cursor liberation and the `WH_MOUSE_LL` hook cover games that read the mouse through the cursor and `WM_MOUSEMOVE`. Games that register for Raw Input (`WM_INPUT`) are a separate tier, and it was measured on Windows 11 25H2 in September 2026 rather than reasoned about: a low-level hook that drops every mouse event leaves `WM_INPUT` untouched, the only user-mode action that stops `WM_INPUT` is taking the foreground away from the game, and Elden Ring then ignores the gamepad as well. Microsoft's HID architecture opens mouse collections exclusively for the Raw Input Manager, so no user-mode process can read or block them. The measurements and the survey of existing drivers are in `docs/vision/HOST_MODE_ISOLATION.md`, sections 8 and 9.

The consequence is architectural: for that tier Nimbus must own the mouse below `win32k`. On Linux that is one `EVIOCGRAB` ioctl (the `linux-uinput-support` branch). On Windows it is a small KMDF upper filter on the mouse class, the Nimbus Mouse Filter in `driver/`, which passes everything through until Nimbus asks for isolation and then hands the packets to Nimbus instead of `mouclass`. On Windows, Nimbus then moves the real cursor itself with `SetCursorPos`, which creates no input event: the cursor keeps working on the desktop and on Nimbus, the game keeps the foreground and its gamepad, and the game's Raw Input stream stays empty (the cursor relay; on Linux, where re-injection would be visible to the game, Nimbus draws its own cursor instead). A test-signed development build was validated on hardware in September 2026: with isolation on, a Raw Input consumer received nothing while the driver captured every packet from the physical mouse, the mouse returned on release, Left 4 Dead 2 with raw input on stayed still under relayed motion, and the real application drove its virtual stick from a captured drag while the game saw nothing. It is not yet attestation-signed, not validated against an anti-cheat title, and not part of any release. Design and status: `docs/vision/WINDOWS_MOUSE_FILTER_PLAN.md`.

---

## 9. Game Focus Mode

A second focus-related obstacle: certain games pause or stop accepting input the moment they lose foreground status. A user clicking on the Nimbus window to operate a button thereby pauses their own game. *Game Focus Mode* circumvents this without ever transferring focus.

The Nimbus window's extended style is augmented with `WS_EX_NOACTIVATE`, and `WM_MOUSEACTIVATE` is intercepted to return `MA_NOACTIVATE`. The combined effect is that the window remains fully interactive — clicks, drags, and hover events reach Nimbus widgets normally — but Windows never elevates the window to foreground status. Focus stays with the game across the entire session; the game has no event to react to, because no focus transition occurs.

This design supersedes an earlier (v1.4.0) save-and-restore-foreground approach, which was correct in principle but produced a sub-100-ms focus blip on every click. The blip was sufficient to trigger pause menus in titles such as Minecraft. The current `WS_EX_NOACTIVATE` design eliminates the blip entirely. Implementation is in `src/window_utils.py`.

On Linux under X11 the same outcome needs no platform code: setting Qt's `WindowDoesNotAcceptFocus` flag on the main window clears the `WM_HINTS` input flag, after which the window still receives pointer events but the window manager never assigns it keyboard focus (measured: the previously active window stays active across clicks on Nimbus). Wayland compositors own focus policy, so the mode is reported unavailable there. The controller-mode keep-alive that complements it on Windows (`src/mouse_hider.py`) is provided off Windows by the driver-agnostic `src/controller_pulse.py`, which drives the uinput pad through the same interface API.

Linux adds a mechanism Windows cannot offer in user mode: *Mouse Isolation* (`src/mouse_isolation.py`). An exclusive `EVIOCGRAB` on the pointer's evdev node removes the physical mouse from the X server, the compositor, and every game; Nimbus reads the deltas itself, maintains a software cursor, and delivers synthetic `QMouseEvent`s to its own window so the widget layer is unchanged. Keys from combined keyboard-and-touchpad devices are re-emitted through a uinput pass-through keyboard, `Ctrl+Alt+F12` releases the grab from any keyboard, and the kernel drops the grab automatically when the process ends. This closes the Raw Input tier described in § 1 on Linux; it was verified against Elden Ring under Easy Anti-Cheat, where the raw-input camera stopped responding to an isolated mouse while the anti-cheat accepted the session and the virtual pad drove the camera.

---

## 10. Accessibility Considerations

The design choices documented above are not abstractly motivated. Each addresses a category of user need observed during testing.

| User population | Observed limitation | Nimbus response |
|-----------------|---------------------|-----------------|
| Spinal cord injury, mid- to high-level | Cannot hold mouse buttons; tremor in available range of motion | Triple-click lock; centre deadzone; smoothing |
| Cerebral palsy | Variable tremor amplitude; targeting difficulty | Tunable centre and extremity deadzones; large widget sizes |
| ALS, mid-progression | Eye-gaze pointer; switch button input | Layouts using only large dwell-style buttons; toggle mode for sustained inputs |
| Limb difference, single-hand | Standard controller is two-handed by design | Layouts using only widgets reachable from one quadrant |
| RSI, professional | Avoid sustained gripping | Mouse-only operation of full controller surface |

In each case, the user-side intervention is a layout — a JSON file — not a code change, a hardware purchase, or a configuration of an arcane utility. The cost of accommodating a new user need is the cost of placing widgets on a canvas.

Nimbus also interoperates today with Windows Eye Control. Eye Control treats the Nimbus window as any other application surface; widgets are dwell-targetable; the platform composes naturally with Microsoft's own accessibility stack.

---

## 11. Optional Cloud Layer

An optional cloud layer ships with Nimbus and is fully off by default:

- **Account system** (`src/cloud_client.py`) via Supabase, with email + Google + Facebook OAuth. Tokens are stored in the OS credential vault (`keyring` → Windows Credential Manager); no plaintext tokens are written to disk. Sessions restore silently on launch and refresh in the background, and offline operation is preserved if any of those steps fail.
- **Profile sync** for Nimbus+ subscribers, using last-write-wins per profile ID. Push on save, pull on startup. Conflicts use the most recent timestamp.
- **Opt-in, anonymous telemetry** (`src/telemetry.py`) with SHA-256-hashed identifiers, buffered locally and batch-flushed every 5 minutes. No PII is collected, and the on-disk fallback is bounded to prevent unbounded growth. Crash reports are routed via Sentry when enabled.
- **Auto-updater** (`src/updater.py`) consuming a static JSON manifest with stable / beta / dev channels and a minimum-supported-version field for force-update warnings. No background downloads, no silent installs — the user clicks through to the release page.

Three principles govern this layer:

1. **All cloud features are off by default.** Nimbus is fully operational with no network connection.
2. **The free tier is permanent.** No accessibility feature will move behind an account or paywall.
3. **Privacy is documented in-app, not buried in a EULA.** The Privacy Settings dialog enumerates exactly what is collected and what is never collected.

---

## 12. Roadmap

The platform is positioned to extend in four directions, each preserving the same input pipeline and adding an alternate signal source or output sink.

**Voice command integration.** Buttons, axes, and macros triggered by speech, using Faster-Whisper or Vosk for offline low-latency execution. Time-critical commands act on interim recognition results.

**Spectator+, AI-assisted execution.** A trained agent translates high-level intent (issued by voice, dwell, or switch) into precise sequences of axis and button events through the existing virtual driver bridge. The user remains the cognitive and tactical authority; the AI executes the motor sequence. The first step is in the tree without a model: scripted primitives (turn by an angle, walk for a distance, press a button) executed by the bridge from a per-game calibration that a game test harness measures from the game's own console. In Left 4 Dead 2 they land within a few degrees of the angle asked for; in Half-Life 2, whose stick response puts more than half the turn into the first tenth of a second, the same primitives are repeatably long, which is the honest measure of how far open-loop execution goes and why the closed loop was the next step (`docs/vision/GAME_TEST_HARNESS.md`, section 4.7). That step was taken on 2026-09-13: a servo on angular error, fed by a target the game itself publishes, steers the view onto it in a third of a second and stops within a quarter of a degree, with the user's own stick able to take over at any tick (section 4.8, and `docs/vision/TARGET_AWARE_AIM_PLAN.md` for the rules that bound it).

**Keyboard-output mode.** Any widget emits native keyboard shortcuts. This re-purposes the platform as a Stream Deck replacement, drawing-tablet express-key surface, or DAW controller, and re-uses the entire layout / persistence stack.

**Hardware integration.** Nimbus reads existing adaptive hardware (XAC, QuadStick, foot pedals, head trackers) via DirectInput/XInput, applies its sensitivity curves and macro layer on top, and re-emits through vJoy/ViGEm. This positions Nimbus not as a competitor to physical adaptive hardware but as a software shell that increases its expressivity.

**AAC.** The same widget surface that controls a game emits spoken phrases via TTS, navigates AAC vocabulary pages, or fires shortcuts in dedicated AAC software. Windows Eye Control already operates Nimbus today; this direction formalises that capability.

**Research platform.** With opt-in telemetry, Nimbus could function as a research instrument for studying how people with disabilities engage with games, in collaboration with AbleGamers, Shirley Ryan AbilityLab, CMU HCII, and similar institutions.

**Mouse isolation.** Closing the Raw Input tier (§ 8.1) with the Nimbus Mouse Filter on Windows and the `EVIOCGRAB` grab on Linux, so that Elden Ring-class games see only the virtual pad. The bridge half (the isolation API, the click policy, the Isolate Mouse toggle) is shared between platforms; the source of deltas and what happens to the cursor differ (the cursor relay on Windows, a software cursor on Linux).

---

## 13. Build, Distribution, and Engineering Practices

Nimbus is distributed in three forms:

- **Source.** `python run.py` (Windows) or `./run.sh` (Linux) bootstraps a virtual environment, installs dependencies (PySide6, numpy, keyring, httpx, sentry-sdk, plus pyvjoy on Windows only), and launches the QML app. All Win32 integration uses the standard-library `ctypes` module. There is no `pywin32` dependency, by design.
- **Portable executable.** A single-file PyInstaller build with all assets and dependencies bundled.
- **Installer.** An NSIS installer that detects existing vJoy and ViGEmBus installations (via 64-bit registry views), creates Start Menu shortcuts, and launches the application post-install at the user's privilege level rather than the installer's elevated level.

The codebase follows PEP 8 with type annotations throughout. Tests live in `tests/` and run under `pytest`. Configuration is validated at load time; the system is designed to *degrade gracefully* — if neither vJoy nor ViGEm is installed the UI launches, indicates the missing driver, and continues to allow layout editing. This degraded mode is itself accessibility-relevant: a user can configure their layout on a fresh machine before installing the driver.

---

## 14. Conclusions

Nimbus Adaptive Controller is, at its core, a one-paragraph argument: *given a user who can move a cursor, and given a kernel-level virtual controller driver, a sufficiently expressive layout engine in user space is a complete adaptive controller.* The hardware industry has converged on a parallel argument that requires hundreds of pounds of physical hubs and switches; Nimbus shows that the software path produces a comparable result at zero marginal cost, and that the *layout* — not the device — is the appropriate locus of customisation.

The technical contributions are modest individually and load-bearing collectively: a single dual-mode widget component, a JSON-defined canvas, a uniform input pipeline with deadzones / curves / smoothing / failsafe, a backend-agnostic driver abstraction, a pop-out palette window, a triple-click lock, a cooperative cursor-liberation thread, and a focus-restoration shim. None of these is novel in isolation. Their composition is what produces an accessibility surface that a user can *self-administer* — without sighted assistance, without expert configuration, and without proprietary hardware.

The roadmap directions — voice, AI-assisted execution, AAC, hardware bridging, keyboard-output mode — are each instances of *the same surface, a different signal source or signal sink*. The architecture's principal property is that those extensions slot in without disturbing the input pipeline or the layout engine. That is the property the project is most concerned to preserve.

---

## Appendix A. Hardware Capability Reference

| Resource | vJoy (DirectInput) | ViGEm (XInput / Xbox 360) |
|----------|--------------------|----------------------------|
| Analog axes | 8 (X, Y, Z, RX, RY, RZ, SL0, SL1) | 4 + 2 triggers |
| Joysticks | Up to 4 | 2 |
| Buttons | Up to 128 | 10 face/shoulder/stick buttons + 4-way D-pad (commonly counted as 14) |
| POV hats | Yes | D-pad (4 directions) |
| Game compatibility | Older / simulator / pro flight & ground-control | Modern PC games / Steam |

On Linux the `uinput` back ends expose the same shapes, with two differences: the generic joystick has 56 buttons rather than 128 and no POV hat, and the Xbox pad reports its D-pad as a hat axis pair exactly as a wired Xbox 360 controller does.

## Appendix B. Profile JSON — Minimal Custom Layout

```jsonc
{
  "name": "One-Hand Drive",
  "description": "Single-hand layout for racing games on ViGEm",
  "layout_type": "custom",
  "custom_layout": {
    "grid_snap": 16,
    "show_grid": false,
    "widgets": [
      {
        "id": "wheel",
        "type": "wheel",
        "x": 80,  "y": 60,
        "width": 360, "height": 360,
        "mapping": { "axis": "x" },
        "sensitivity": 45,
        "dead_zone": 8
      },
      {
        "id": "throttle",
        "type": "slider",
        "x": 480, "y": 60,
        "width": 80,  "height": 360,
        "orientation": "vertical",
        "snap_mode": "none",
        "mapping": { "axis": "rt" }
      },
      {
        "id": "brake",
        "type": "slider",
        "x": 580, "y": 60,
        "width": 80,  "height": 360,
        "orientation": "vertical",
        "snap_mode": "none",
        "mapping": { "axis": "lt" }
      }
    ]
  }
}
```

## Appendix C. Glossary

- **DirectInput** — Legacy Microsoft input API supporting up to 8 axes, 128 buttons per device. Nimbus targets it via vJoy.
- **XInput** — Microsoft's Xbox-controller-shaped input API. Two sticks, two triggers, 14 buttons. Targeted via ViGEm.
- **vJoy** — Open-source kernel driver presenting a virtual DirectInput device to user space.
- **ViGEm / ViGEmBus** — Open-source kernel driver presenting virtual Xbox 360 / DualShock 4 devices.
- **MAVLink** — Drone telemetry / control protocol; ground-control stations consume DirectInput as pilot input.
- **AAC** — Augmentative and Alternative Communication. Software and hardware that supplements speech for users with communication disabilities.
- **XAC** — Xbox Adaptive Controller. Microsoft's hardware adaptive-controller hub.

---

*Nimbus Adaptive Controller is MIT-licensed. Source, profiles, and documentation are available at <https://github.com/owenpkent/Nimbus-Adaptive-Controller>.*
