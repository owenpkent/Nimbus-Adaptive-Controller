# Nimbus Adaptive Controller

### A Modular, Mouse-Driven Virtual Game Controller for Accessible Computing

**Technical White Paper** — Version 1.0
Owen Kent · May 2026

---

## Abstract

The Nimbus Adaptive Controller is a free, open-source software platform that synthesises virtual joystick output from arbitrary pointer-class input devices on Windows. It targets a class of users for whom commercially available game controllers are physically inaccessible, and it does so without dedicated hardware: any mouse, trackball, head tracker, eye tracker, or alternative HID device that produces 2-D pointer events can be re-shaped, through a fully user-configurable layout, into DirectInput (vJoy) or XInput (ViGEm) controller output consumable by any Windows game or simulation. This paper describes the system architecture, the input-processing pipeline, the modular layout engine, the persistence model, and the design decisions behind treating the user interface itself — rather than the physical input device — as the primary point of customisation. We argue that this inversion is the principal contribution of the project: by making the *layout* programmable and the *input device* fixed, Nimbus delivers an accessibility surface comparable in flexibility to the Xbox Adaptive Controller at zero hardware cost, and provides a foundation on which voice control, AI-assisted execution, AAC, and adaptive-hardware bridging can be layered as software extensions of the same control surface.

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

- **Cross-platform parity.** Linux and macOS lack a mature equivalent of vJoy/ViGEm at the kernel level. Nimbus is Windows-first by necessity.
- **Replacement of physical adaptive hardware.** Users who already own a QuadStick or sip-and-puff device should continue to use it; Nimbus aims to *wrap* such devices, not displace them.
- **A general-purpose macro engine.** Nimbus emits HID-class output by design. Keyboard-output mode is on the roadmap (§ 12) but is downstream of the core controller pipeline.

---

## 3. System Overview

Nimbus is a Python 3.8+ application built on PySide6 (Qt 6) with a Qt Quick (QML) presentation layer and a small Python core. It links to one of two virtual-driver bindings — `pyvjoy` for vJoy and `vgamepad` for ViGEm — and exposes its runtime to QML through a single bridge object.

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

Two deadzones are applied per axis:

- **Centre deadzone** — a circular region around the origin in which output is forced to zero. This eliminates tremor, switch chatter, and the unintentional micro-movements typical of head- and eye-tracking systems.
- **Extremity deadzone** — a scaling factor that reduces the maximum reachable output. This prevents over-travel for users who cannot consistently *avoid* the rim of a joystick widget, and is the symmetric counterpart of the centre deadzone.

Both deadzones are configured as percentages (0–100 %) and stored per widget in the profile JSON.

### 4.4 Sensitivity Curve

Post-deadzone, each axis is shaped by a sensitivity curve. Nimbus uses a symmetric power curve parameterised by a single sensitivity slider:

```
sensitivity ∈ [0, 100]
exponent = sensitivity_to_exponent(sensitivity)
output  = sign(input) · |input| ^ exponent
```

A sensitivity of 50 % yields an exponent of 1.0 (linear). Values below 50 % flatten the curve near the centre, providing fine control for small movements at the cost of speed at the rim. Values above 50 % steepen the curve, giving rapid response near centre — desirable for combat games or rover yaw control, but unsuitable for sustained precision work like a UAV gimbal. The same formula is implemented in two places — `config.py:apply_joystick_dialog_curve()` for Python-side processing and `_applyCurve()` in `DraggableWidget.qml` for live preview — and the dialog draws the curve in real time as the user adjusts it.

### 4.5 Smoothing

The bridge maintains, per axis, a target value updated by the QML layer at the user's input rate, and an interpolated *current* value advanced by a `QTimer` at a fixed 60 Hz. Output is sent to the driver from the interpolated value. Smoothing factor is bounded; an upper bound prevents perceptible lag, and a lower bound prevents the audible step-noise that a real driver would produce when fed a non-smoothed mouse trajectory.

### 4.6 Driver Emission

The smoothed, curved, deadzoned value — still in normalised `[-1, 1]` form — is mapped onto the driver's integer axis range and submitted via `pyvjoy.VJoyDevice.set_axis()` or `vgamepad.VX360Gamepad.left_joystick_float()`. Buttons are submitted as boolean state changes; toggle-mode buttons are debounced at the QML layer before reaching the bridge.

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

Nimbus supports two virtual controller drivers. The choice is per-profile and is auto-selected based on layout type, with vJoy preferred for layouts requiring more than 14 buttons or more than 4 axes, and ViGEm preferred elsewhere because XInput is the dominant modern game-controller protocol on Windows.

### 6.1 vJoy (DirectInput)

vJoy presents up to 16 virtual sticks, each with up to 8 axes (X, Y, Z, RX, RY, RZ, SL0, SL1), 128 buttons, and 4 POV hats. Nimbus binds to vJoy device 1 by default. The strength of vJoy is its capacity — DirectInput games and many simulators (DCS, MSFS, X-Plane, Mission Planner, ArduPilot Ground Control Stations) consume large axis and button counts that XInput cannot supply.

The weakness of vJoy is age and platform drift. Many modern Windows games — anything built on the assumption that *controller* means *Xbox controller* — ignore DirectInput devices entirely. No Man's Sky is the canonical example.

### 6.2 ViGEm (XInput)

ViGEmBus provides an in-kernel virtual Xbox 360 controller. Games receive an XInput device indistinguishable from a wired Xbox 360 pad. This solves the modern-games-don't-see-vJoy problem at the cost of a much smaller resource budget: 2 analog sticks, 2 triggers, and 14 buttons.

### 6.3 Backend Indirection

The bridge owns a single controller-interface object whose concrete type is `VJoyInterface` or `ViGEmInterface`. Both expose the same surface — `update_axis(name, value)`, `set_button(id, pressed)`, `center_all()`, `get_status()` — so the QML layer is unaware of which backend is active. Switching backend at runtime is a matter of profile reload.

---

## 7. Profile System

Profiles are versioned, JSON-serialised configurations stored in:

| Platform | Path |
|----------|------|
| Windows | `%APPDATA%\ProjectNimbus\profiles\` |
| macOS   | `~/Library/Application Support/ProjectNimbus/profiles/` |
| Linux   | `~/.local/share/ProjectNimbus/profiles/` |

A profile carries every piece of information needed to reproduce the user's controller end-to-end: layout type, custom layout (if applicable), per-axis sensitivity and deadzone, button toggle states, axis mapping, and metadata (display name, description). On first run, the bundled `adaptive_platform_2.json` is copied into the user data directory; thereafter the user may duplicate, rename, modify, and delete profiles freely. There is no central server, no account requirement, and no upload; profile sharing is a matter of copying a JSON file.

This deliberate primitivity is itself an accessibility feature. A user who can navigate File Explorer can back up their profiles. A caregiver setting up a new machine can do so by copying one folder. Nothing about the profile format prevents inspection or modification with a text editor.

---

## 8. Borderless Gaming and Cursor Liberation

A latent obstacle to a mouse-driven controller is that many games re-confine the mouse cursor to their own window every frame using `ClipCursor()`, and many games run in exclusive full-screen mode that captures the cursor entirely. The user cannot reach the Nimbus window because the game has hidden it, and even if the Nimbus window is visible, the cursor cannot leave the game's bounds.

Nimbus solves this with a built-in borderless-gaming layer (`src/borderless.py`):

- **Window flattening.** `make_borderless()` strips `WS_CAPTION` and `WS_THICKFRAME` from the target window's style and resizes it to fill the monitor. The game now occupies the screen but does not own it; the Windows compositor still arbitrates focus and z-order.
- **Cursor liberation.** A background thread polls `ClipCursor(NULL)` at a configurable interval (16 ms aggressive, 200 ms gentle) to undo any clipping the game applies. This is cooperative — the thread cannot prevent the game from re-clipping in the next frame, only undo it on the next polling cycle — but in practice the latency is below the perceptual threshold for cursor jump.
- **Auto-detection.** A built-in compatibility table covering 30+ games is consulted on launch to suggest known-working configurations.

The trade-off is that this technique is detectable and could be misclassified by anti-cheat systems. Nimbus does not modify game memory, hook game APIs, or interfere with input on the game's side; it operates entirely on the Windows window/cursor layer, the same layer used by routine accessibility tools. To date no anti-cheat false positives have been reported, but the design choice to operate at the OS layer rather than at the game layer is deliberate and should be preserved.

---

## 9. Game Focus Mode

A second focus-related obstacle: certain games pause or stop accepting input the moment they lose foreground status. A user clicking on the Nimbus window to operate a button thereby pauses their own game. *Game Focus Mode* circumvents this:

1. On every Nimbus mouse press, the foreground window handle is captured.
2. Nimbus briefly takes focus to register the input.
3. On mouse release, focus is restored to the captured window via `SetForegroundWindow()`, with `AttachThreadInput()` used to bypass Windows' anti-focus-stealing protection.

The user experiences a single uninterrupted game session; the game experiences a sub-100-ms focus blip per click, which most titles tolerate without pausing.

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

## 11. Optional Account, Sync, and Telemetry Layer

An optional cloud layer is in development (per `CHANGELOG.md`'s Unreleased section):

- **Account system** via Supabase, with email + Google + Facebook OAuth. Tokens are stored in the OS credential vault (`keyring` → Windows Credential Manager). Sessions restore silently and refresh in the background; offline operation is preserved.
- **Profile sync** for subscribers, using last-write-wins per profile ID. Push on save, pull on startup.
- **Opt-in, anonymous telemetry** with SHA-256-hashed identifiers, batched and flushed every 5 minutes. No PII is collected. Crash reports are routed via Sentry.
- **Auto-updater** consuming a static JSON manifest with stable / beta / dev channels and a minimum-supported-version field for force-update warnings.

Three principles govern this layer:

1. **All cloud features are off by default.** Nimbus is fully operational with no network connection.
2. **The free tier is permanent.** No accessibility feature will move behind an account or paywall.
3. **Privacy is documented in-app, not buried in a EULA.** The Privacy Settings dialog enumerates exactly what is collected and what is never collected.

---

## 12. Roadmap

The platform is positioned to extend in four directions, each preserving the same input pipeline and adding an alternate signal source or output sink.

**Voice command integration.** Buttons, axes, and macros triggered by speech, using Faster-Whisper or Vosk for offline low-latency execution. Time-critical commands act on interim recognition results.

**Spectator+ — AI-assisted execution.** A trained agent translates high-level intent (issued by voice, dwell, or switch) into precise sequences of axis and button events through the existing virtual driver bridge. The user remains the cognitive and tactical authority; the AI executes the motor sequence.

**Keyboard-output mode.** Any widget emits native keyboard shortcuts. This re-purposes the platform as a Stream Deck replacement, drawing-tablet express-key surface, or DAW controller, and re-uses the entire layout / persistence stack.

**Hardware integration.** Nimbus reads existing adaptive hardware (XAC, QuadStick, foot pedals, head trackers) via DirectInput/XInput, applies its sensitivity curves and macro layer on top, and re-emits through vJoy/ViGEm. This positions Nimbus not as a competitor to physical adaptive hardware but as a software shell that increases its expressivity.

**AAC.** The same widget surface that controls a game emits spoken phrases via TTS, navigates AAC vocabulary pages, or fires shortcuts in dedicated AAC software. Windows Eye Control already operates Nimbus today; this direction formalises that capability.

**Research platform.** With opt-in telemetry, Nimbus could function as a research instrument for studying how people with disabilities engage with games, in collaboration with AbleGamers, Shirley Ryan AbilityLab, CMU HCII, and similar institutions.

---

## 13. Build, Distribution, and Engineering Practices

Nimbus is distributed in three forms:

- **Source.** `python run.py` bootstraps a virtual environment, installs dependencies (PySide6, pyvjoy, vgamepad, numpy, pywin32, keyring, httpx, sentry-sdk), and launches the QML app.
- **Portable executable.** A single-file PyInstaller build with all assets and dependencies bundled.
- **Installer.** An NSIS installer that detects existing vJoy and ViGEmBus installations (via 64-bit registry views), creates Start Menu shortcuts, and launches the application post-install at the user's privilege level rather than the installer's elevated level.

The codebase follows PEP 8 with type annotations throughout. Tests live in `tests/` and run under `pytest`. Configuration is validated at load time; the system is designed to *degrade gracefully* — if neither vJoy nor ViGEm is installed the UI launches, indicates the missing driver, and continues to allow layout editing. This degraded mode is itself accessibility-relevant: a user can configure their layout on a fresh machine before installing the driver.

---

## 14. Conclusions

Nimbus Adaptive Controller is, at its core, a thirty-line argument: *given a user who can move a cursor, and given a kernel-level virtual controller driver, a sufficiently expressive layout engine in user space is a complete adaptive controller.* The hardware industry has converged on a parallel argument that requires hundreds of pounds of physical hubs and switches; Nimbus shows that the software path produces a comparable result at zero marginal cost, and that the *layout* — not the device — is the appropriate locus of customisation.

The technical contributions are modest individually and load-bearing collectively: a single dual-mode widget component, a JSON-defined canvas, a uniform input pipeline with deadzones / curves / smoothing / failsafe, a backend-agnostic driver abstraction, a pop-out palette window, a triple-click lock, a cooperative cursor-liberation thread, and a focus-restoration shim. None of these is novel in isolation. Their composition is what produces an accessibility surface that a user can *self-administer* — without sighted assistance, without expert configuration, and without proprietary hardware.

The roadmap directions — voice, AI-assisted execution, AAC, hardware bridging, keyboard-output mode — are each instances of *the same surface, a different signal source or signal sink*. The architecture's principal property is that those extensions slot in without disturbing the input pipeline or the layout engine. That is the property the project is most concerned to preserve.

---

## Appendix A. Hardware Capability Reference

| Resource | vJoy (DirectInput) | ViGEm (XInput / Xbox 360) |
|----------|--------------------|----------------------------|
| Analog axes | 8 (X, Y, Z, RX, RY, RZ, SL0, SL1) | 4 + 2 triggers |
| Joysticks | Up to 4 | 2 |
| Buttons | Up to 128 | 14 |
| POV hats | Yes | D-pad (4 directions) |
| Game compatibility | Older / simulator / pro flight & ground-control | Modern PC games / Steam |

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
