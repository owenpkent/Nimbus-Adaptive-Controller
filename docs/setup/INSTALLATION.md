# Installation Guide

## Prerequisites

- **Windows 10/11** (64-bit), or **Linux** (see the [Linux Setup guide](LINUX.md); no driver install needed, the kernel provides uinput)
- Nothing else. The installer includes both controller drivers and installs whichever one is missing:
  - **vJoy 2.1.9.1** for DirectInput profiles ([source](https://github.com/jshafer817/vJoy); newer 2.2.x builds do not load on Windows 11)
  - **ViGEmBus 1.22.0** for Xbox 360 profiles and Game Mode ([source](https://github.com/nefarius/ViGEmBus))
- Running from source instead? Install both drivers by hand from the links above, or run the installer once (it leaves the drivers in place if Nimbus is uninstalled). The app talks to ViGEmBus directly through `src/padbus_client.py` and needs no client package.

## Installing Nimbus Adaptive Controller

### From Installer (Recommended)

1. Download `Nimbus-Adaptive-Controller-Setup-X.Y.Z.exe` from the [Releases page](https://github.com/owenpkent/Nimbus-Adaptive-Controller/releases)
2. Run the installer
3. **UAC admin prompt** — Enter your Windows admin password (required for proper installation)
4. The installer wizard will:
   - Check if Nimbus Adaptive Controller is already running and offer to close it
   - Detect any previous installation (per-user or system-wide) and offer to remove it
   - Let you choose the installation directory (default: `%LOCALAPPDATA%\Programs\Project Nimbus\`)
   - **Drivers page**: shows whether vJoy and ViGEmBus are already present, and offers to install whichever is missing. Both are included in the installer, so no internet connection is needed. Untick either one to skip it; Nimbus still starts without them, with the matching profile types unavailable
   - **Shortcut options page** — Choose whether to create Desktop and/or Start Menu shortcuts
   - Install the executable and create uninstaller
   - Install the drivers you ticked, then check they really are present and say so if one is not
   - Register in Windows Add/Remove Programs
5. Click "Launch Nimbus Adaptive Controller" on the final page to start

### From Source (Development)

```bash
git clone https://github.com/owenpkent/Nimbus-Adaptive-Controller.git
cd Nimbus-Adaptive-Controller
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

## Installer Behavior

### Previous Version Detection

The installer checks **both** registry locations for existing installations:
- `HKCU\...\Uninstall\project-nimbus-virtual-controller` (per-user installs)
- `HKLM\...\Uninstall\project-nimbus-virtual-controller` (system-wide installs)

If found:
- Prompts you to remove the old version first (recommended)
- The old version's silent uninstaller (`/S` flag) is run if you choose to remove it
- If you skip removal, the new version installs to your chosen directory

### Running Instance Detection

If `Nimbus-Adaptive-Controller.exe` is already running, the installer offers to close it via `taskkill` before proceeding.

### Settings Preservation

**User settings are preserved across installs and upgrades.** Here's why:

| Data | Location | Survives Reinstall? |
|------|----------|-------------------|
| User profiles | `%APPDATA%\ProjectNimbus\profiles\` | ✅ Yes |
| App config | `controller_config.json` (in install dir) | ⚠️ Deleted on uninstall |
| Bundled profiles | Inside the executable (PyInstaller) | Regenerated on first run |

- **Profiles** (custom layouts, widget configurations, sensitivity settings) are stored in `%APPDATA%\ProjectNimbus\profiles\` which is **never touched by the installer or uninstaller**
- **Bundled default profiles** (flight_simulator, xbox, adaptive_platform, etc.) are embedded in the executable. On first run, any missing profiles are copied to the user directory. Existing user profiles are never overwritten.
- **`controller_config.json`** stores global settings (current profile, window size, vjoy device ID). This file is in the install directory and **is deleted on full uninstall**. It is regenerated with defaults on next launch.

### Uninstall

The uninstaller removes:
- The executable and uninstaller from the install directory
- `controller_config.json` from the install directory
- Desktop and Start Menu shortcuts (if created)
- Windows registry entries (both HKCU and HKLM)

**Optional user data removal**: At the end of uninstall, you'll be asked if you want to remove your saved profiles and settings from `%APPDATA%\ProjectNimbus\`. Choose "No" to keep them for future installations.

## Building the Installer

Requirements:
- [NSIS](https://nsis.sourceforge.io/) (Nullsoft Scriptable Install System)
- PyInstaller-built executable in `dist/Nimbus-Adaptive-Controller.exe`
- Icon file in `build_tools/Nimbus-Adaptive-Controller.ico`

```bash
# Build the executable
pyinstaller Nimbus-Adaptive-Controller.spec

# Build the installer
cd build_tools
makensis installer.nsi
```

The installer is output to `dist/Nimbus-Adaptive-Controller-Setup-X.Y.Z.exe`.

### Code Signing

The executable and installer should be signed with an EV code certificate for SmartScreen trust:

```bash
build_tools\sign_exe.bat dist\Nimbus-Adaptive-Controller.exe
build_tools\sign_exe.bat dist\Nimbus-Adaptive-Controller-Setup-X.Y.Z.exe
```

## vJoy Setup

The Nimbus installer does all of this for you: it installs vJoy 2.1.9.1 and
configures device 1 with 8 axes and 128 buttons. Follow the steps below only if
you are running from source, or if you unticked vJoy during installation.

1. Download vJoy 2.1.9.1 from [jshafer817's releases](https://github.com/jshafer817/vJoy/releases/tag/v2.1.9.1). Its driver is Microsoft-signed. Avoid the newer 2.2.x builds: their driver fails to load on Windows 11 (njz3/vJoy issue 17), leaving a device with 0 buttons.
2. Run the installer (requires admin rights)
3. Open **vJoyConf** (vJoy Configuration) and ensure:
   - Device 1 is enabled
   - At least 8 axes are configured (X, Y, Z, RX, RY, RZ, Slider 0, Slider 1)
   - At least 128 buttons are enabled
4. Launch Nimbus Adaptive Controller — it should detect vJoy automatically

## Linux Installation

Windows is the primary platform; there is no standalone Linux executable. From
source, Xbox/Adaptive/Custom-layout profiles work today with no code changes,
because [vgamepad](https://github.com/yannbouteiller/vgamepad) — the same
package `ViGEmInterface` uses on Windows — ships its own Linux backend that
creates a virtual Xbox 360 pad through the kernel's `uinput` device via
`libevdev`, rather than ViGEmBus.

### Prerequisites

- Python 3.8+
- `libevdev` (`sudo apt install libevdev2` on Debian/Ubuntu; `libevdev` on
  Fedora/Arch) — the system library vgamepad's Linux backend binds against
- A writable `/dev/uinput`

### Setup

```bash
git clone https://github.com/owenpkent/Nimbus-Adaptive-Controller.git
cd Nimbus-Adaptive-Controller
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 run.py
```

`requirements.txt` also lists `pyvjoy`, a Windows-only package. It installs
fine (pure Python wheel) but can never load its DLL on Linux; the "PyVjoy not
available" message this prints at startup is expected and does not affect the
Xbox/Adaptive/Custom output path.

### `/dev/uinput` access

On most desktop distributions, systemd-logind grants the active graphical
session's user write access to `/dev/uinput` automatically (the `uaccess`
udev tag) — nothing to configure. If Nimbus reports the controller as
disconnected, check:

```bash
sudo modprobe uinput             # load the kernel module if missing
ls -l /dev/uinput                # confirm it exists
```

If it exists but is not writable by your user, add a udev rule granting
access (for example, tagging it `uaccess`, or granting a group your user
belongs to) and log out and back in.

### Verified working

Confirmed end-to-end this cycle: raw `/dev/input/js*` reads, SDL2/pygame
joystick enumeration, the browser Gamepad API, mouse input through the actual
on-screen joystick and button widgets, and a live Steam game running under
Proton (menu navigation, button-glyph switching from keyboard to Xbox icons).

### Known limitations on Linux

- **Flight-sim / vJoy-preferring profiles are capped at 56 buttons.** vJoy
  itself is a Windows kernel driver with no Linux equivalent, so
  `VJoyInterface` stays in simulation mode, but the bridge substitutes
  `UInputJoystickInterface` (8 axes over raw `uinput`, vJoy value range) and
  these profiles do produce real output. Buttons 57 to 128 are unavailable:
  evdev has only 56 generic joystick button codes against vJoy's 128. Not yet
  verified against a real flight sim under Proton.
- **Borderless Gaming and Game Focus Mode** are correctly disabled in the menu
  on non-Windows platforms (both check for Win32 at the API level).
- **The "Game Mode" ribbon button** (Full Game Mode) is not yet hidden on
  Linux and currently has no effect there — it depends on the same Win32-only
  mechanisms as Borderless Gaming.
- **Wayland is untested.** Verification above was under X11; see
  [`docs/vision/LINUX_PROBE_PLAN.md`](../vision/LINUX_PROBE_PLAN.md) for the
  open questions around window management on Wayland.

## Troubleshooting

- **"Failed to initialize VJoy device 1"** — vJoy driver not installed, or another application is using device 1. Close other vJoy apps and retry.
- **"Cannot acquire vJoy Device because it is not in VJD_STAT_FREE"** — Previous instance of Nimbus Adaptive Controller didn't shut down cleanly. Wait a few seconds and try again, or restart vJoy via Device Manager.
- **SmartScreen warning** — The executable must be signed with an EV code certificate. Unsigned builds will trigger SmartScreen warnings.
- **Linux: controller shows disconnected** — `/dev/uinput` is likely missing or unwritable. See [`/dev/uinput` access](#devuinput-access) above.
- **Linux: "vgamepad not available"** — `libevdev` is missing. Install `libevdev2` (Debian/Ubuntu) or `libevdev` (Fedora/Arch).
- **Linux: vJoy-preferring profile does nothing** — Expected; see [Known limitations](#known-limitations-on-linux) above.
