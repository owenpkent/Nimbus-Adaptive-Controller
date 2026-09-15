# Nimbus Adaptive Controller — Directory Structure

> **Purpose**: Quick reference for developers and AI assistants to understand the codebase layout.  
> **Last updated**: September 2026 (v1.4.3, plus the unreleased `driver/` and `vm/` work)

---

## Root Files

| File | Purpose |
|------|---------|
| `run.py` | **Main entry point** — Run this to start the app in development |
| `run.bat` | Windows batch launcher (activates venv, runs run.py) |
| `README.md` | Project overview, features, installation, usage |
| `DIRECTORY.md` | This file — project structure guide |
| `CHANGELOG.md` | Version history with semantic versioning |
| `TODO.md` | Roadmap, completed features, backlog |
| `LICENSE` | MIT License |
| `requirements.txt` | Python dependencies |
| `controller_config.json` | Runtime config (generated, gitignored in production) |
| `logo.png` | Application logo (used in splash screen, about dialog) |

---

## Source Code: `src/`

Python backend — Qt/QML bridge, configuration, hardware interfaces.

| File | Purpose |
|------|---------|
| `__init__.py` | Package init, version string |
| `qt_qml_app.py` | **Main app entry** — QML engine setup, splash screen, window init |
| `bridge.py` | **QML↔Python bridge** — All `@Slot` methods callable from QML |
| `config.py` | Configuration manager — profiles, settings, JSON persistence |
| `vjoy_interface.py` | vJoy driver communication — axis/button output |
| `vigem_interface.py` | ViGEm Xbox controller emulation (optional) |
| `uinput_interface.py` | Linux `uinput` back ends: Xbox 360 pad + 8-axis joystick (stand-ins for ViGEm/vJoy) |
| `controller_pulse.py` | Driver-agnostic controller keep-alive pulse (controller mode off Windows) |
| `mouse_isolation.py` | Linux `EVIOCGRAB` of the physical mouse + keyboard pass-through (Mouse Isolation) |
| `qt_dialogs.py` | Native Qt dialogs — Joystick Settings, Button Settings, Axis Mapping |
| `qt_widgets.py` | Custom Qt widget components |
| `borderless.py` | Borderless window mode + ClipCursor release (Windows) |
| `mouse_hider.py` | Controller Mode Enforcement — keep-alive pulse + mouse hook (Windows) |
| `window_utils.py` | Game Focus Mode — save/restore foreground window (Windows) |
| `mouse_isolation_win.py` | Mouse isolation client for the Nimbus Mouse Filter kernel driver (Windows); same class API as the Linux `mouse_isolation.py` on the `linux-uinput-support` branch. Drives Full Game Mode's mouse isolation with the cursor relay: the real cursor keeps working, the game sees no mouse |
| `spectator/` | Spectator+ v0: `calibration.py` (a game's measured stick response and the plans built from it), `primitives.py` (`PrimitiveRunner`: turn, walk and press as timed axis sequences on a `QTimer`, reached through `ControllerBridge.get_spectator()`), `calibrations/<game>.json` written by the game test harness |
| `telemetry.py` | Opt-in anonymous analytics + crash reporting (local buffer, batch flush) |
| `cloud_client.py` | User accounts (Email/Google/Facebook OAuth), token management, profile sync |
| `updater.py` | Lightweight auto-update checker with version manifest and update channels |
| `qt_main.py` | Legacy Qt Widgets main (not used in QML UI) |
| `legacy/` | Old pygame-based UI (deprecated, kept for reference) |

### Key Classes

- **`ControllerBridge`** (`bridge.py`) — Singleton exposed to QML as `controller`. All QML↔Python communication goes through here.
- **`ControllerConfig`** (`config.py`) — Profile management, settings persistence, sensitivity curve calculations.
- **`VJoyInterface`** (`vjoy_interface.py`) — Low-level vJoy API wrapper.
- **`UInputXboxInterface` / `UInputJoystickInterface`** (`uinput_interface.py`) — Linux equivalents of ViGEm/vJoy over `/dev/uinput`, same method names.
- **`TelemetryClient`** (`telemetry.py`) — Opt-in event tracking with local buffer and batch HTTP flush.
- **`CloudClient`** (`cloud_client.py`) — Supabase auth, OAuth, token vault, profile sync.
- **`UpdateChecker`** (`updater.py`) — Background version manifest fetch with QML signal integration.

---

## QML UI: `qml/`

Qt Quick (QML) frontend — all UI components and layouts.

```
qml/
├── Main.qml                 # Root ApplicationWindow, menu bar, layout loader
├── components/              # Reusable UI components
│   ├── DraggableWidget.qml  # Universal drag/resize/config wrapper for all widgets
│   ├── WidgetPalette.qml    # Floating toolbar for adding widgets (edit mode)
│   ├── BorderlessGamingDialog.qml  # Borderless gaming & cursor release UI
│   ├── MacroEditorDialog.qml       # Macro joystick zone editor
│   ├── AccountDialog.qml           # Sign-in / account management (Email, Google, Facebook)
│   ├── SettingsPrivacyDialog.qml   # Telemetry opt-in/out with data schema transparency
│   ├── UpdateNotification.qml      # Non-intrusive update ribbon
│   └── ...
└── layouts/                 # Layout implementations
    ├── CustomLayout.qml     # ★ Default — drag-and-drop canvas with widget system
    ├── FlightSimLayout.qml  # Fixed dual-joystick layout (legacy, not default)
    ├── XboxLayout.qml       # Xbox gamepad layout (legacy, not default)
    └── AdaptiveLayout.qml   # Accessibility fixed layout (legacy, not default)
```

### Key QML Components

- **`Main.qml`** — Entry point, menu bar, profile switching, layout loader
- **`CustomLayout.qml`** — Canvas with edit mode, widget repeater, joystick lock overlay, config dialog
- **`DraggableWidget.qml`** — Wraps all widget types with drag, resize, delete, and double-click config

### Widget Types (in CustomLayout)

| Type | Description |
|------|-------------|
| `joystick` | 2-axis analog with triple-click mouse lock, FPS-style delta tracking |
| `button` | Single press, toggle/momentary modes, color/shape options |
| `slider` | Horizontal/vertical, 3 snap modes (none, left, center) |
| `dpad` | 4-directional digital button cluster |
| `wheel` | Rotational single-axis steering wheel |

---

## Profiles: `profiles/`

JSON profile files — copied to `%APPDATA%\ProjectNimbus\profiles\` on first run.

| File | Layout Type |
|------|-------------|
| `adaptive_platform_2.json` | `custom` — Default drag-and-drop canvas (opens on first launch) |

### Profile JSON Structure

```json
{
  "name": "Display Name",
  "description": "Profile description",
  "layout_type": "custom",
  "custom_layout": {
    "widgets": [...],
    "grid_snap": 10,
    "show_grid": true
  },
  "joystick_settings": { "sensitivity": 50, "deadzone": 0, "extremity_deadzone": 5 },
  "rudder_settings": { ... },
  "buttons": { "button_1": { "label": "A", "toggle_mode": false }, ... }
}
```

---

## Build Tools: `build_tools/`

Everything needed to package and distribute the app.

| File | Purpose |
|------|---------|
| `Project-Nimbus.spec` | PyInstaller spec — defines bundling, paths, hidden imports |
| `launcher.py` | Entry point for frozen executable |
| `installer.nsi` | NSIS installer script: wizard, shortcuts, version detection, bundled vJoy and ViGEmBus install |
| `linux/60-nimbus-uinput.rules` | udev rule for Linux: write access to `/dev/uinput`, plus `uaccess` on the event nodes of Nimbus's own virtual devices so they can be read back |
| `fetch_redist.ps1` | Downloads the vJoy and ViGEmBus setups the installer bundles, pinned by SHA-256 and publisher signature, into the gitignored `redist/`. Run before `makensis` |
| `sign_exe.bat` | Code signing script (EV certificate) |
| `Project-Nimbus.ico` | Application icon (multi-resolution) |
| `Project-Nimbus.manifest` | Windows manifest (UIAccess, DPI awareness) |
| `version_info.txt` | Windows version resource info |

### Build Commands

```powershell
# Build executable
venv\Scripts\pyinstaller.exe build_tools\Project-Nimbus.spec --noconfirm

# Fetch the bundled driver setups (required before makensis)
powershell -ExecutionPolicy Bypass -File build_tools\fetch_redist.ps1

# Build installer
& "C:\Program Files (x86)\NSIS\makensis.exe" build_tools\installer.nsi

# Sign both
cmd /c build_tools\sign_exe.bat
```

---

## Kernel Driver: `driver/`

The Nimbus Mouse Filter, a KMDF upper filter on the mouse class that hands the physical mouse to Nimbus for Raw Input games. Windows only, built separately from the app (needs Visual Studio 2022 with the WDK). Not part of `run.py` and not in any release yet.

| File | Purpose |
|------|---------|
| `README.md` | Build, test-signing, and dev-install instructions |
| `SIGNING.md` | Release path: Partner Center registration, attestation signing, and where that stands after the April 2026 driver policy |
| `nimbus_moufilter/nimbus_moufilter.c` | The driver: filter callback, control device, isolation IOCTLs, watchdog |
| `nimbus_moufilter/nimbus_moufilter_ioctl.h` | User/kernel contract, mirrored by `src/mouse_isolation_win.py` |
| `nimbus_moufilter/nimbus_moufilter.inx` | INF template (service install; class filter entry is added by the install script) |
| `build.ps1` | Build and collect outputs into `driver/out/` (gitignored) |
| `package.ps1` | Build and EV-sign the attestation submission CAB; `-VerifySigned` checks the package Microsoft returns |
| `enable-testsigning.ps1`, `install-dev.ps1`, `uninstall-dev.ps1` | Elevated dev loop; the installer verifies the load and rolls back automatically |
| `pnp-common.ps1` | Shared helper: restarts every mouse with `pnputil /restart-device` so the filter attaches or detaches without a reboot |

---

## Guest VM Tooling: `vm/`

The scripts behind `docs/vision/VIRTUAL_MACHINE_FEASIBILITY.md`: a Windows 11 guest on the Windows host sharing the GPU through Hyper-V GPU-PV, the pad in through Moonlight and Sunshine, and the isolation proof (Gate C). Research tooling, not part of the app or the installer; `vm/README.md` is the runbook.

| File | Purpose |
|------|---------|
| `00-host-preflight.ps1` | Read-only host inventory and verdict (edition, firmware, one GPU, VirtualBox, the mouse filter's reboot safety) |
| `10-enable-hyperv.ps1` | Elevated: the Hyper-V role plus Hyper-V Administrators for the console user; refuses if the mouse would not survive the reboot |
| `20-check-gpu-partition.ps1` | Gate B part 1: `Get-VMHostPartitionableGpu` |
| `30-fetch-iso.ps1` | A Windows 11 ISO via Fido, or `-Url` |
| `40-new-guest.ps1` | Elevated: image applied straight to a VHDX, answer file and guest tooling on it, Generation 2 VM with vTPM, Enhanced Session Mode off |
| `50-attach-gpu.ps1` | Elevated: partition adapter, MMIO and cache settings, host driver files into the guest's HostDriverStore; `-Verify` is Gate B part 2 |
| `60-guest-stream.ps1` | Guest phase 2, Moonlight on the host, pairing by PIN through Sunshine's API |
| `gate_c_host.py` | Gate C, host side: input sweeps, 14 buttons, stick holds, Stop within 500 ms; `--actuator pad` or `nimbus` |
| `gate_d_latency.py`, `guest/beacon.py` | Gate D, the latency axis: input to photon through the guest against the host natively; the beacon flashes, so unattended only |
| `guest/setup.ps1`, `guest/unattend.xml` | The guest's unattended first boot and setup |
| `guest/gate_c_monitor.py`, `guest/render_check.py` | Guest side: the input counter served over HTTP, and the Direct3D 11 device check |
| `common.ps1` | Shared names, paths, elevation and credential helpers |

---

## Documentation: `docs/`

```
docs/
├── README.md                    # Docs index
├── GAME_COMPATIBILITY.md        # Borderless gaming game compatibility list
├── vision/                      # Research and plans
│   ├── HOST_MODE_ISOLATION.md   # Raw Input tier: options, Windows measurements, prior art
│   ├── WINDOWS_MOUSE_FILTER_PLAN.md  # The kernel filter design and status
│   ├── PAD_BUS_FORK_PLAN.md     # Forking and modernizing ViGEmBus; the client first, the driver on a gate
│   ├── LINUX_PROBE_PLAN.md      # The Linux EVIOCGRAB experiment
│   ├── LINUX_GAMING_PROPOSAL.md # A Linux/X11 port of Nimbus; a separate platform track
│   └── VIRTUAL_MACHINE_FEASIBILITY.md  # A guest VM on the Windows host; why it loses to the filter
├── setup/                       # Installation & configuration
│   ├── INSTALLATION.md          # Install guide, vJoy setup
│   ├── PROFILES.md              # Profile system, save locations
│   ├── ACCOUNTS.md              # User accounts setup (Email, Google, Facebook OAuth)
│   ├── TELEMETRY.md             # Telemetry & privacy guide
│   ├── UPDATER.md               # Auto-updater setup & hosting
│   └── PACKAGING.md             # Build & distribute guide
├── architecture/                # Technical design
│   ├── architecture.md          # Codebase structure, data flow
│   └── VDROID_DRIVER_BRAINSTORM.md  # Future custom driver plans
├── development/                 # For contributors & AI
│   ├── LLM_NOTES.md             # Implementation details, conventions, bug fixes
│   ├── INTEGRATION_GUIDE.md     # How to add new widgets/features
│   ├── START_HERE.md            # Quick orientation
│   └── WIDGET_IDEAS.md          # Planned widget concepts
├── accessibility/               # Accessibility-related
│   └── ACCESSIBILITY_SPOTLIGHT_NOMINATION.md
├── screenshots/                 # UI screenshots
└── video/                       # Demo videos
```

---

## Other Directories

| Directory | Purpose |
|-----------|---------|
| `tests/` | vJoy diagnostics plus Windows input probes: `probe_rawinput_windows.py` (which countermeasures stop `WM_INPUT`), `probe_game_mouselook_windows.py` (in-game camera motion), `probe_mouse_filter_windows.py` (the kernel filter), `probe_mouse_filter_stress_windows.py` (the filter's battle test: storms, floods, process chaos, CPU starvation, an API fuzz, a soak), `probe_nimbus_relay_windows.py` (the real app in Full Game Mode with the cursor relay), `probe_installer_drivers_windows.ps1` (the installer's vJoy and ViGEmBus bootstrap, unattended after one elevation), `test_stick_shaping.py` (property checks on the stick shaping formula, no hardware), `probe_stick_shaping_windows.py` (the real app driven by synthesized pointer events, reading what reached ViGEm), `probe_game_deadzone_windows.py` (a running game's real stick deadzone, and whether a 1 px Nimbus drag clears it), `game_harness.py` and `probe_game_harness_windows.py` (the game test harness: launches a game from a recipe in `games/`, owns the pad, reads ground truth from a Source console or frame differencing, calibrates the game, runs the real app end to end, and measures the Spectator+ primitives; recipes for `left4dead2`, `halflife2`, `eldenring` and `powerwashsimulator`; `docs/vision/GAME_TEST_HARNESS.md`). Linux: `test_uinput.py` (round-trips every axis and button through the kernel), `probe_evdev_grab.py` (the EVIOCGRAB mechanism), `probe_game_mouselook.py` (in-game isolation check), `test_linux_input_safety.py` (the pulse-versus-release race and the absolute-pointer refusal, no hardware), `probe_linux_stack.py` (the whole Linux stack against a real kernel: uinput back ends through `ControllerOutput`, the pulse-versus-release property read back from the device's own event node, pointer classification against this machine's devices, and the bridge's platform dispatch), and `run_linux_validation.sh` (one command: the fast suite plus every Linux probe, into a single log). Hardware-free bridge coverage is `test_bridge_services.py` and `test_bridge_no_duplicate_methods.py` (parses `bridge.py` with `ast`, so a method defined twice cannot hide behind the one Python binds), `test_bridge_isolation.py` || `research/` | Research notes, reference materials |
| `build/` | PyInstaller build cache (gitignored) |
| `dist/` | Built executables and installers (gitignored) |
| `venv/` | Python virtual environment (gitignored) |

---

## Data Flow

```
User Input (mouse/keyboard)
    ↓
QML UI (Main.qml → CustomLayout.qml → DraggableWidget.qml)
    ↓
ControllerBridge (@Slot methods in bridge.py)
    ↓
ControllerConfig (sensitivity curves, deadzones)
    ↓
VJoyInterface (vjoy_interface.py)
    ↓
vJoy Driver → Game/Application
```

---

## Key Entry Points for AI Assistants

| Task | Start Here |
|------|------------|
| Understanding the app | `README.md`, then `docs/architecture/architecture.md` |
| Making UI changes | `qml/Main.qml`, `qml/layouts/CustomLayout.qml` |
| Adding QML↔Python features | `src/bridge.py` (add @Slot methods) |
| Changing settings/profiles | `src/config.py` |
| Building releases | `docs/setup/PACKAGING.md` |
| User accounts / auth | `src/cloud_client.py`, `docs/setup/ACCOUNTS.md` |
| Telemetry / privacy | `src/telemetry.py`, `docs/setup/TELEMETRY.md` |
| Auto-updater | `src/updater.py`, `docs/setup/UPDATER.md` |
| Bug fixes / implementation details | `docs/development/LLM_NOTES.md` |
| Adding new widget types | `docs/development/INTEGRATION_GUIDE.md` |

---

## Conventions

- **Python**: PEP 8, type hints where practical
- **QML**: camelCase for properties/functions, PascalCase for component filenames
- **Commits**: Conventional commits (`feat:`, `fix:`, `docs:`, etc.)
- **Versioning**: Semantic versioning (MAJOR.MINOR.PATCH)
- **Profiles**: JSON files in `%APPDATA%\ProjectNimbus\profiles\`
- **Config**: `controller_config.json` in working directory (dev) or install directory (production)
