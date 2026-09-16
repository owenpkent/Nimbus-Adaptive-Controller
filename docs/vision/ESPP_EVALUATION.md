# espp: What It Is, and What Nimbus Could Build With It

**Status:** Research note, 2026-09-16. Nothing built, nothing measured, no code or dependency added. Every fact below is read from the espp and `esp-usb-ble-hid` sources and READMEs on that date, and from Espressif's documentation; nothing was flashed to a board here.
**Prompted by:** [finger563/esp-usb-ble-hid](https://github.com/finger563/esp-usb-ble-hid), an ESP32-S3 dongle firmware built on espp.
**Problem statement:** [Input Ownership: Problem Statement](PROBLEM_STATEMENT.md) is the yardstick. Section 5 below judges every idea here against its section 4.
**Decision in force:** **C. Software only** ([Problem Statement 3.2](PROBLEM_STATEMENT.md#32-decisions), 2026-09-14). A USB dongle was considered and ruled out the same day ([Separate Game Machine 3.3](SEPARATE_GAME_MACHINE.md#33-not-pursued-a-usb-dongle)), which records that reopening it is the project owner's decision. **This document does not reopen it.** It exists so that if the question ever is reopened, it is reopened against facts rather than a memory of a rejected sketch.

**Question:** espp is a large, actively maintained C++ library for ESP32 that already contains an Xbox 360 controller USB device implementation. If Nimbus ever wanted a hardware pad, what exactly would it get for free, what would it still have to build, and what would it still not solve?

**Answer in one line:** espp reduces the firmware half of a Nimbus pad device from a project to a weekend, and it is the best answer in the repository to the game-acceptance and failsafe rows of section 4; but it does nothing at all for pointer isolation, which is the actual problem, and the shape that could actually ship turns out to serve the one output path no bundled profile uses ([section 4.5](#45-the-bind-these-four-shapes-are-in)).

**Side effect of writing this:** checking the one claim the recommendation rested on found three stale documents. Flight-sim profiles have had real output on Linux since 2026-09-12 and CLAUDE.md, the README and `docs/setup/INSTALLATION.md` all still said they had none. Corrected on 2026-09-16; the chain is in [4.2](#42-a-joystick-for-the-vjoy-shaped-profiles).

---

## 1. Why this document exists

The dongle idea recorded in [Separate Game Machine 3.3](SEPARATE_GAME_MACHINE.md#33-not-pursued-a-usb-dongle) was a Raspberry Pi Pico running GP2040-CE, fed by a USB-to-UART cable. It was ruled out in the same day it was raised, with the cost side of the judgement carried mostly by "this track is software only".

Two facts found on 2026-09-16 change the shape of the option enough that the old sketch is no longer an accurate description of what was rejected:

1. **The ESP32-S3 has WiFi and BLE.** The Pico sketch needed a cable between the Nimbus computer and the game machine, which is why it only suited Layout D with the two machines adjacent. A wireless part removes that constraint entirely.
2. **espp already implements the hard half.** `espp::UsbDevice` has a first-class X-Input function that presents the chip as a wired Xbox 360 controller, with a working example. The firmware work is not "write a USB gamepad", it is "wire a link to an existing one".

Neither fact is an argument that a dongle is a good idea. They are an argument that the decision, if revisited, should be revisited against this rather than against the Pico sketch.

---

## 2. What espp is

### 2.1 Scope and maturity

| Fact | Value |
|---|---|
| Repository | [esp-cpp/espp](https://github.com/esp-cpp/espp), "C++ components for ESP" |
| License | MIT |
| Language | C++ (the portable library requires C++23; the Python package builds with C++20) |
| Components | About 130 directories under `components/` |
| ESP-IDF target | ESP-IDF 6.0, with v5.5.1 support maintained |
| Latest release | v1.3.3, 2026-09-15 |
| Release cadence | Six releases between 2026-08-08 and 2026-09-15 |
| Last push | 2026-09-16 |
| Stars / forks | 116 / 31 |
| Distribution | ESP Component Registry, namespace `espp` (`idf.py add-dependency "espp/usb_device^1.0"`), plus a GitHub template repository, plus PyPI |
| CI | 10 workflows, including host sanitizers, static analysis, wheel builds, docs, and two interoperability suites (RTPS, ODrive) |
| Host tests | 14 `*_host_test.cpp` files for the ESP-free protocol cores |

This is not a hobby dump. The protocol cores are deliberately written to be dependency-free and host-testable, sanitizers and static analysis run in CI, and the components are versioned and published. That matters for the maintenance row of section 4 more than any feature does.

The counterweight: it is one maintainer's library, the pace is fast, and the project tracks ESP-IDF closely enough that its own README warns the code "may not be supported on older ESP-IDF targets". Nimbus would be pinning to a moving dependency.

### 2.2 The components that matter here

| Component | What it gives | Relevance |
|---|---|---|
| `usb_device` | Composable native USB device on the S2/S3/P4 USB-OTG peripheral: CDC-ACM, vendor + WebUSB, HID with an application-supplied report descriptor, and **X-Input**. Interfaces, endpoints and string indices are allocated sequentially and checked against the endpoint budget | The core of every idea in section 4 |
| `usb_device/include/xinput.hpp` | The Xbox 360 wired protocol: identity `0x045E:0x028E`, `bcdDevice 0x0114`, interface class `0xFF` / subclass `0x5D` / protocol `0x01`, 20-byte input report, 8-byte rumble and LED output report, endpoint size 32, IN `bInterval` 4 ms, OUT `bInterval` 8 ms. Dependency-free and host-testable | This is `ViGEmInterface` and `UInputXboxInterface`, in silicon, with rumble coming back |
| `hid-rp` | A wrapper around [intergatedcircuits/hid-rp](https://github.com/intergatedcircuits/hid-rp), a compile-time HID report descriptor builder with the full set of usage pages (including Simulation Controls, Physical Input Device, Game, Eye and Head Trackers, Braille Display), plus ready-made Switch Pro, Xbox, PlayStation, PS4 and 3Dconnexion SpaceMouse reports | The route to a DirectInput joystick with a descriptor Nimbus chooses |
| `hid_service` + `ble_gatt_server` | A standard BLE HID service over NimBLE, with battery and device-information services, driven by the same `hid-rp` reports | A BLE gamepad to a PC, phone or tablet, with no USB port used |
| `usb_host` | The S2/S3/P4 as a USB **host**, enumerating HID devices: mice, keyboards, gamepads, vendor HID | The [Hardware Integration](HARDWARE_INTEGRATION.md) track done in hardware |
| `ota` | Transport-agnostic firmware update engine plus a framed stream protocol and a browser uploader at `esp-cpp.github.io/espp/apps/ota_console.html` | Updating a device with no installer |
| `coredump` | Flash core dumps, a formatted crash report with the exact `addr2line` command, and a browser tool to download `core.elf` | Diagnosing a device in a player's home |
| `dispatcher` + `stream_frame` | Multiplexes independent framed protocols over one byte stream by a module byte. The v2 wire format is fully specified, CRC-32 verified, resynchronizing, and ESP-free | How OTA, crash dumps and an app protocol share one link |
| `nvs`, `wifi`, `socket`, `task`, `timer`, `logger`, `state_machine`, `event_manager` | The ordinary scaffolding | Settings, network, structure |
| `t-dongle-s3`, `qtpy` | Board support for the two boards `esp-usb-ble-hid` targets | Display, RGB LED, button |
| `wdi` | See [section 8](#8-the-adjacent-find-a-wheelchair-interface-in-the-library) | Adjacent, and worth knowing about |
| `joystick`, `filters`, `math` | Analog joystick mapping and calibration, low-pass and Butterworth filters, Bézier curves, range mapping, PID | **Deliberately not used.** See [section 7](#7-what-must-not-move-into-firmware) |

### 2.3 The Python package

espp publishes its portable subset to PyPI as [`espp`](https://pypi.org/project/espp/), v1.3.3, generated with pybind11, with wheels for `win_amd64`, manylinux x86-64 and aarch64, and macOS universal2, for CPython 3.10 through 3.14. It carries `py.typed` stubs. It exposes `Task` and `Timer` on real OS threads, UDP and TCP sockets, RTSP, RTPS, CDR, COBS, alpaca serialization, the math and filter set, and utilities.

Three things follow, and the third is the one that matters:

- It is a real possibility that the Nimbus side of a device protocol and the firmware side could share an implementation rather than two hand-matched ones.
- `espp.Task` runs on a native thread, which is **exactly what the netpad sender-liveness rule forbids**: keepalives must come from the bridge's Qt main-thread loop, never an independent worker, so that a frozen UI stops the packets ([Separate Game Machine 3.2](SEPARATE_GAME_MACHINE.md#32-a-network-receiver-layout-d)). If espp ever appeared on the Nimbus side, `espp.Task` would be a trap, not a convenience.
- **It requires Python 3.10 or newer.** Nimbus targets Python 3.8+ (CLAUDE.md, `requirements.txt`). So espp is not a dependency Nimbus can take today without raising its floor, which is a separate decision with its own cost. The sharing idea is therefore theoretical until that changes.

There is a better way to share anyway: `stream_frame`'s v2 format and `xinput.hpp`'s report packing are both written as dependency-free, host-testable specifications. Re-implementing either in pure Python is a small, exactly-specified job with a reference implementation to test against, and it costs Nimbus no new dependency at all.

---

## 3. The firmware that prompted this

[finger563/esp-usb-ble-hid](https://github.com/finger563/esp-usb-ble-hid) (MIT, v3.0.0 released 2026-09-15, ESP-IDF v6.1) is a dongle that pairs with an Xbox controller over BLE and presents itself to a Nintendo Switch as a wired Pro Controller. The Switch half is irrelevant to Nimbus. Three things about it are not.

**It is already split at the seam Nimbus would cut.** `main/ble.cpp` is the input source, `main/usb.cpp` is the output device, and between them sits a `GamepadDevice` abstract base (`get_report_descriptor`, `set_gamepad_inputs`, `on_hid_report`, `on_attach`) carrying a `GamepadInputs` struct. Replacing "BLE central" with "a link to Nimbus" is a source swap against a defined interface, not a rewrite.

**Its input struct is already the Nimbus output surface.** `GamepadInputs` is two sticks as floats in `[-1, 1]`, two triggers as floats in `[0, 1]`, and a button bitfield. That is `ViGEmInterface`'s shape exactly: 4 axes, 2 triggers, 14 buttons. The netpad `STATE` packet (`netpad/protocol.py`, `<8sIIhhhhBBHB14s`) carries the same thing in 43 bytes of body.

**It has learned things Nimbus would otherwise learn the hard way.** Two examples from `main/usb.cpp`: the HID sender runs on its own task rather than TinyUSB's, because writing a report from the TinyUSB task and then waiting for the endpoint to drain deadlocks; and if a host that had the device configured lets the link drop without re-enumerating, the firmware re-arms the USB pull-up after 2.5 seconds (`tud_disconnect()`, wait, `tud_connect()`) to force re-enumeration. That second one is the hardware analogue of the [fresh-pad stale-sticks finding](SEPARATE_GAME_MACHINE.md#103-a-real-game-left-4-dead-2-through-the-network-pad): hosts drop pads, and something has to notice.

What is **not** reusable: the Switch Pro protocol engine, the Xbox BLE parsing, and the Nintendo USB identity. What is reusable is the skeleton, the WebUSB console (`web/dongle_console.html`), the service registration pattern, and the two lessons above.

---

## 4. Four shapes a Nimbus device could take

Ranked by how much they are worth, not by how easy they are.

### 4.1 A netpad receiver in hardware

The device plugs into the game machine and enumerates as a wired Xbox 360 pad. Nimbus sends `netpad/protocol.py` packets to it over WiFi. The game machine installs nothing at all.

This deletes most of the cost column of [Separate Game Machine 3.2](SEPARATE_GAME_MACHINE.md#32-a-network-receiver-layout-d):

- No receiver service to install, and so no `/dev/uinput` permission hypothesis, no systemd user unit, and nothing to survive a SteamOS Game Mode to Desktop Mode switch, a suspend, or an OS update.
- No ViGEmBus on a Windows game machine.
- The "new attack surface: a network service that injects input" row moves off the game machine and onto a device that does nothing else.

It also improves on the software receiver in one specific, measured way. netpad destroys the pad after 2 seconds with no live session, and Left 4 Dead 2 never read a pad that had been destroyed and re-created. A USB device stays enumerated. Its watchdog sets neutral, and the pad is still the same pad when Nimbus comes back. That is [fact 7](PROBLEM_STATEMENT.md#31-facts)'s second clause satisfied by construction rather than by protocol.

The firmware to write: a UDP listener, HMAC-SHA256 verification (the S3 has hardware SHA), the watchdog and press-counter rules from `netpad/protocol.py`, and a call to `update_xinput_state()`. The protocol is already specified and already tested in Python.

### 4.2 A joystick for the vJoy-shaped profiles

A HID joystick device with an honest USB identity (espp's default VID is `0x1209`, the pid.codes community VID) presents the vJoy shape on both operating systems with no driver at all.

**What this does and does not fix, checked against the source on 2026-09-16.** Three documents claimed that flight-sim profiles have no output on Linux. All three were stale, and checking why produced a dated chain worth recording:

| Date | Commit | What changed |
|---|---|---|
| 2026-09-02 | `9573bc3` | `UInputJoystickInterface` written: a generic 8-axis joystick over `uinput`, its docstring calling it a "drop-in replacement for `VJoyInterface` on Linux", mapping `x, y, z, rx, ry, rz, sl0, sl1` onto `ABS_X..ABS_RZ, ABS_THROTTLE, ABS_RUDDER` at the vJoy value range. **Not wired to anything** |
| 2026-09-11 | `d1ea5eb` | The README and `docs/setup/INSTALLATION.md` "Known limitations on Linux" entries written. **Correct on the day**: the class existed but nothing constructed it, so those profiles really had no output |
| 2026-09-12 | `ba93419c` | The Linux branch merged onto `ControllerOutput`. `_create_joystick_interface` collapsed into the single factory line, which made `UInputJoystickInterface` the joystick backend on Linux. The capability arrived as a side effect of a refactor, and no document caught up |

So the flight-sim path on Linux has had real output since 2026-09-12. It is asserted by `tests/probe_linux_stack.py` (which checks the active backend's type) and constructed by `tests/test_uinput.py`, and the UI labels it "Joystick (uinput)". CLAUDE.md, the README and `INSTALLATION.md` were corrected on 2026-09-16 as part of writing this document.

What the Linux path does **not** have is buttons 57 to 128: evdev offers only 56 generic joystick button codes (16 in the `BTN_JOYSTICK` block plus 40 `BTN_TRIGGER_HAPPY`), against vJoy's 128. A HID device closes exactly that, and nothing else, on Linux. It is also unverified against a real flight sim under Proton, unlike the gamepad path.

**The live friction is on Windows.** vJoy is a third-party kernel driver the user must install, pinned by `docs/setup/INSTALLATION.md` to 2.1.9.1 because the 2.2.x driver "fails to load on Windows 11 (njz3/vJoy issue 17), leaving a device with 0 buttons". A HID device removes that dependency, that version pin and that failure mode outright. This is the strongest argument for this shape, and it is about Windows, not Linux.

**What it would actually look like.** Concrete enough to cost, since every technical objection to this shape turns out to be small:

| Piece | Detail |
|---|---|
| Report | 8 axes at 16 bits (16 bytes) plus 128 button bits (16 bytes) plus a report ID: 33 bytes, inside one 64-byte full-speed interrupt packet |
| Ceiling | Windows DirectInput tops out at 8 axes, 128 buttons and 4 POV hats, which is exactly vJoy's shape. Not a coincidence: vJoy was built to that ceiling, so matching vJoy and matching DirectInput are the same job |
| Rate | A HID interrupt IN endpoint can take `bInterval = 1`, so 1 kHz, four times faster than the X-Input endpoint's fixed 4 ms |
| Descriptor | `hid-rp` carries the usage pages needed (Generic Desktop for X/Y/Z/Rx/Ry/Rz/Slider, Simulation Controls for Rudder, Throttle, Aileron, Elevator, Brake). The stock `GamepadInputReport<>` is fixed at 4 axes, so this is a custom report type: roughly a day |
| Link | CDC on the same cable. Nimbus writes a fixed state frame, the firmware hands it to `write_hid_report()`. No pairing, no HMAC, no session handshake, no sequence numbers |
| Failsafe | No frame for a chosen interval, center the axes and release the buttons, stay enumerated. The frame's tick must come from the bridge's loop and never a worker thread, the same rule netpad's sender liveness already follows |
| Integration | A `HidPadInterface` matching `VJoyInterface`'s surface, passed as `ControllerOutput`'s joystick factory at the one construction site. It adds no method, so nothing needs mirroring into ViGEm or uinput |

**And the honest counterweight.** Neither bundled profile exercises this path. `profiles/adaptive_platform_2.json` and `profiles/xbox_controller.json` are both `layout_type: custom` using 6 and 10 buttons, and `ControllerOutput.initialize` routes `xbox`, `adaptive` and `custom` to the Xbox backend. `flight_sim` is the fallback layout type (`config.py`, `get_layout_type`), so the joystick path is reachable and is what an unlabelled profile gets, but nothing shipped drives it and nothing in the repository uses more than 10 buttons. The 128-button ceiling is a capability, not a complaint anyone has made.

**The HID function composes; the X-Input function does not.** This is the most decision-relevant difference between this shape and the other three, and it is easy to miss. The endpoint budget on the ESP32-S3 is about 5 usable data IN and 5 OUT besides EP0. CDC costs 2 IN / 1 OUT, vendor plus WebUSB costs 1 IN / 1 OUT, HID costs 1 IN and optionally 1 OUT. So **CDC + vendor + HID fits comfortably** at 4 IN / 3 OUT, and espp's README names that combination explicitly. X-Input cannot join them, for the reason in [section 6](#6-constraints-that-are-not-negotiable).

What that buys is the whole of the awkwardness in 4.1 and 4.3 disappearing. One USB cable to one PC carries all three at once: the joystick the game reads, a CDC serial link Nimbus writes state over at roughly a millisecond, and the WebUSB console for settings, firmware and crash dumps. No WiFi, no 2.4 GHz jitter, no pairing, no HMAC, no session handshake, no watchdog protocol, and no unmeasured link. The firmware failsafe becomes "CDC went quiet, go neutral", which is a few lines.

One accuracy point that is easy to get wrong: espp's stock `GamepadInputReport<>` template makes `BUTTON_COUNT` a template parameter, so 128 buttons is a type argument, but it is **fixed at 4 joystick axes plus 2 trigger axes**. The vJoy shape of 8 axes (X, Y, Z, RX, RY, RZ, SL0, SL1) is not the stock template. It would need a descriptor written directly against `hid-rp`, which is entirely reasonable (the library carries the Generic Desktop and Simulation Controls usage pages, the latter holding Rudder, Throttle, Aileron, Elevator, Brake and Accelerator), but it is work, not a template argument.

This shape is the one that is least entangled with the input-ownership problem and gives the clearest user-visible win.

### 4.3 A local pad in place of ViGEm

The device plugs into the RTX 5090 PC and is that PC's gamepad. It removes the ViGEmBus install, and it gives a pad that is a physical USB device to anything that looks.

It is the weakest of the four:

- Decision C forbids it most directly.
- The X-Input function must be the **only** enabled USB function, so the same port cannot also carry a CDC serial link from Nimbus. The link has to be WiFi, BLE, or a second physical port (a two-port ESP32-S3 devkit has the native USB-OTG port and a separate USB-UART bridge on UART0, so a wired ~1 ms link is possible, but then the device needs two cables to one PC).
- It is slower than ViGEm, which is a virtual bus with no USB polling at all.
- It does not touch the pointer, which is the actual problem on that machine.

### 4.4 A front end for physical adaptive hardware

`espp::usb_host` enumerates HID devices, so an ESP could read a QuadStick, an Xbox Adaptive Controller or a foot pedal directly and re-emit a combined pad: the [Hardware Integration](HARDWARE_INTEGRATION.md) track without Windows in the middle.

The blocker is stated in espp's own README: only one `espp::UsbHost` may exist, and **it cannot coexist with `espp::UsbDevice`**, because both claim the USB-OTG peripheral. On the ESP32-S3 that is the end of it: one peripheral, one role. The ESP32-P4 has two USB 2.0 OTG controllers (one high-speed, one full-speed) plus USB-Serial-JTAG, so the hardware allows host on one and device on the other, but espp's restriction is written as absolute and would have to be tested rather than assumed. The P4 also has no built-in WiFi, which removes 4.1's link.

Two chips talking over UART is the boring answer and probably the right one if this is ever wanted.

### 4.5 The bind these four shapes are in

Stating it plainly, because it is the finding that matters most and it is not obvious until all four are laid out:

**The one shape that could actually ship serves the path with the fewest users, and the path with the users needs an identity that cannot ship.**

4.2 is shippable because a HID joystick can carry an honest pid.codes VID. It serves the `flight_sim` joystick path, which no bundled profile drives. 4.1 and 4.3 serve the Xbox path, which is what both bundled profiles and the whole [Problem Statement](PROBLEM_STATEMENT.md) target game use, and the only USB identity a PC's XUSB driver binds is Microsoft's.

The ways out, none of them free:

| Way out | Cost |
|---|---|
| Accept DirectInput for everything | Modern Xbox-first titles, including the target game, do not read DirectInput |
| Let Steam Input map the HID pad to XInput | It works, but [Problem Statement section 1](PROBLEM_STATEMENT.md#1-the-situation) names Steam Input as a reported trigger for the target game's controller and keyboard-and-mouse prompt switching. Building on it runs against that document |
| Keep ViGEm for the Xbox path, use a device only for the joystick path | Two output stories to explain, document and support, for a capability nobody has asked for |
| Obtain a licensed XInput identity | Not available to this project |
| Build no hardware | Costs nothing and loses nothing measured |

Nothing here argues for the last row over the others. It is listed because it is the option in force, and because the first four have to beat it rather than merely exist.

---

## 5. Judged against the problem statement

The honest headline first: **[PROBLEM_STATEMENT.md](PROBLEM_STATEMENT.md) is about input ownership, and everything in section 4 above is an output device.** A pad on a wire does not stop Forza seeing the pointer. Nothing here belongs in [section 5's boundary table](PROBLEM_STATEMENT.md#5-where-the-boundary-can-be-drawn), because it draws no boundary. It is a different axis.

With that said, against section 4's requirement rows:

| Requirement | What a device changes | What is unknown |
|---|---|---|
| Pointer isolation | **Nothing.** It is not on the pointer path | Not applicable |
| Leaving and returning | Nothing | Not applicable |
| Pad delivery | Adds the X-Input endpoint's 4 ms `bInterval` (so up to 4 ms of polling, about 2 ms on average) plus the link, against a virtual bus that adds neither. Against Layout S it removes a whole video round trip | Everything. 2.4 GHz WiFi jitter in a real room is the risk, and the S3 has no 5 GHz radio. Must be measured as distributions at a stated refresh rate, like Gate D |
| Performance | Nothing. No host CPU cost, one fewer kernel driver | Nothing material |
| Visibility | Nothing | Not applicable |
| Failsafe | **Better than anything in the repository.** A firmware watchdog is independent of the PC entirely, and the device is never destroyed and re-created, so the game keeps its controller by construction | The neutral bound in firmware, unmeasured |
| Pointer recovery | Nothing, except that removing ViGEmBus is one fewer driver a Windows update can refuse. ViGEmBus failing never killed a mouse, so this is minor | Nothing material |
| Pointer safety | Nothing | Not applicable |
| Game acceptance | **The strongest position available.** A device enumerating as a wired Xbox 360 controller is not a virtual pad, an injected input or a filter driver. It is a controller | Whether anti-cheat that fingerprints USB descriptors, timing or report cadence tells the difference. Unknown and untested |
| Upkeep | WebUSB console and OTA mean configuration and updates need no installer and no elevation, which is a real answer to [fact 8](PROBLEM_STATEMENT.md#31-facts). Against that: a physical object to keep plugged in, and firmware to keep updated | How a caregiver recovers a device that will not enumerate |
| Independence | One more thing that can be unplugged or lost | Not applicable |

**Read plainly:** a device is a strong answer to *game acceptance* and *failsafe*, a good answer to *upkeep*, an unmeasured risk on *pad delivery*, and an irrelevance to the five pointer rows that are the reason [PROBLEM_STATEMENT.md](PROBLEM_STATEMENT.md) exists.

---

## 6. Constraints that are not negotiable

These are properties of the parts, not opinions about them.

1. **X-Input must be the only enabled USB function.** espp says so twice: a PC's XUSB driver only binds a recognized Xbox 360 VID/PID, and espp's own vendor class also claims interface class `0xFF`. So no WebUSB console, no CDC log, and no second interface on that port while it is a pad. Configuration and updates need a different mode, a different port, or a different link. **This constraint is specific to X-Input.** The plain HID function composes freely with CDC and vendor/WebUSB inside the endpoint budget (4 IN / 3 OUT of about 5 / 5), which is why [4.2](#42-a-joystick-for-the-vjoy-shaped-profiles) needs none of the machinery the other shapes need. A device wanting both would need two modes selected at boot, and the X-Input mode would still have no second interface.
2. **The Xbox 360 identity is not shippable.** espp's own words: "These are Microsoft's identifiers, for emulation / testing of your own device only", and "a shipped product must not enumerate under them". Fine for a bench prototype. A wall for anything given to a player. The honest-VID path is 4.2's HID joystick, which is DirectInput rather than XInput, or a licensed identity, which Nimbus is not going to obtain.
3. **The ESP32-S3 USB-OTG is full-speed only**, 12 Mbps. Enough for any gamepad report, but it sets the `bInterval` floor.
4. **On the ESP32-S3 the USB-Serial-JTAG controller shares the native USB port's PHY with USB-OTG.** Keeping the console there while emulating a controller contends and, per espp's example README, reboot-loops the device. The console goes to UART0, which means a USB-UART adapter for development.
5. **USB host and USB device cannot coexist in espp.** See 4.4.
6. **The espp Python package needs Python 3.10+**; Nimbus's floor is 3.8.
7. **New toolchain.** ESP-IDF, C++23, `idf.py`, a board per user. Nimbus is Python and QML with a KMDF driver already; this would be a third language and a third build system.

---

## 7. What must not move into firmware

espp has `joystick` (mapping and calibration), `filters` (low-pass, Butterworth) and `math` (Bézier curves, range mapping). It would be natural, and wrong, to push Nimbus's shaping into the device.

CLAUDE.md is unambiguous: sensitivity curves, deadzones and smoothing are applied **inside the bridge**, never in QML and never in the interfaces; the formula is `shape_magnitude` and `shape_vector` in `src/config.py`, and the widget dialog's curve preview asks the bridge for its points (`shapeCurve`) rather than copying it. A device is an interface. Shaping in firmware would mean two implementations of the curve, a preview that lies, and a per-widget parameter set that has to cross the link.

A device backend implements the same surface every other backend does, listed in [Separate Game Machine 4.1](SEPARATE_GAME_MACHINE.md#41-where-a-remote-backend-fits): `set_left_stick`, `set_right_stick`, `set_left_trigger`, `set_right_trigger`, `set_button`, `update_axis`, `get_status`, `emergency_stop`, `shutdown`, plus `is_connected`, `current_values` and `button_states`. It transports. Nothing more.

The one thing that genuinely belongs in firmware is the **failsafe**, because its whole value is that it survives the PC.

---

## 8. The adjacent find: a wheelchair interface in the library

espp has a `wdi` component implementing the [Open Mobility Hub Wheelchair Digital Interface](https://open-mobility-hub.github.io/wheelchair-digital-interface/) v3.2: a standard bidirectional interface between a powered wheelchair and an app or accessory, over USB HID or BLE, so that "special switches, an alternative joystick, a phone app, a companion MCU" can drive the chair and receive status back. espp implements both roles (device and host) over both transports, with a host-testable protocol core.

Recorded here because it is genuinely surprising to find in a hobby ESP32 library, and because the accessory population the spec is written for is Nimbus's population.

Three things keep it out of scope, and they should be said clearly:

- **It does not give Nimbus an input.** The Control report is accessory to chair (joystick X/Y plus control flags). The Feedback report is status, speed, velocity and odometer. A chair's own joystick position is not exposed to an accessory, so this is not a route to using a chair joystick as a Nimbus input.
- **It is safety-critical in a way Nimbus is not.** Anything that drives a powered wheelchair is a different product with a different standard of care. Nimbus moves a camera in a game.
- **It needs a chair that implements the spec**, which is a question about Owen's actual chair and not about software.

If the input-ownership question ever comes back to "how does the pointer reach Nimbus without becoming a mouse" ([Problem Statement section 6, question 1](PROBLEM_STATEMENT.md#6-what-decides-it-and-how-to-find-out)), this component is evidence that a standardized, non-mouse assistive input path over USB HID and BLE exists and has a C++ implementation. That is the useful thing to remember about it.

---

## 9. What it would cost

| Cost | Detail |
|---|---|
| Hardware per user | One ESP32-S3 board. The two `esp-usb-ble-hid` targets are the LilyGo T-Dongle-S3 (USB-A plug, LCD, RGB LED, button, micro-SD; espp has a BSP for it) and the Adafruit QT Py ESP32-S3 |
| A third toolchain | ESP-IDF, C++23, a second CI target, a second release artifact |
| A moving dependency | espp ships roughly fortnightly and tracks ESP-IDF closely |
| A published promise broken | [WHITEPAPER.md:30](../WHITEPAPER.md#L30) still says "Hardware independence. Operate with any device that produces standard Windows pointer events. No bespoke hardware, no driver development, no firmware." The driver half of that sentence already went when `driver/` landed. A device would take the other two thirds, and the whitepaper would need rewriting rather than quietly contradicting |
| A support surface | A physical object in a player's home that can fail to enumerate, and a caregiver who has to recover it |

---

## 10. What to measure first, if anyone ever does

**Step 0, and it needs no hardware at all: verify the Linux joystick path that already exists.** `UInputJoystickInterface` has been the Linux joystick backend since 2026-09-12, but the end-to-end verification CLAUDE.md records (raw evdev, SDL2, the browser Gamepad API, mouse input through the real QML widgets, a live Steam/Proton game) was done for the gamepad path, not this one. Run a real flight sim under Proton against it. This is free, it exercises the same capability a device would sell, and the result sets the size of the prize: if it passes, the entire hardware case for [4.2](#42-a-joystick-for-the-vjoy-shaped-profiles) shrinks to "buttons 57 to 128, and no vJoy driver install on Windows". Do this before spending anything.

Then, in order, cheapest first. None of these needs a Nimbus change, and the first one answers the only question that matters before the rest are worth doing.

1. **Does it look like a real pad?** Flash espp's `xinput_example` to any ESP32-S3 devkit, plug it into the RTX 5090 PC, and check `joy.cpl`, then read it back the way `tests/probe_padbus_windows.py` reads ViGEm. An afternoon. If Windows does not bind it, everything above is moot.
2. **Does a game read it?** Run it through `tests/probe_game_harness_windows.py` as a new actuator alongside `pad`, `nimbus` and `netpad`. The harness already knows how to ask Left 4 Dead 2 what it received.
3. **What does it cost in latency?** The same distributions Gate D used: median, 95th percentile and worst sample, at a stated refresh rate, against the ViGEm pad on the same PC. Separately over the UART link and over WiFi, because they are different answers.
4. **Does the failsafe hold?** Kill Nimbus mid-hold and measure, with an independent reader, how long until neutral and whether the game still has its controller afterwards. This is the row where a device should beat everything else, so it is worth proving.
5. **What does anti-cheat make of it?** Arma 3 through BattlEye is already in the harness (`tests/games/arma3.json`), with `arma3_nobe` as the control. That is the existing apparatus for this question.

If any of these is run, the result belongs in [Problem Statement section 5](PROBLEM_STATEMENT.md#5-where-the-boundary-can-be-drawn)'s evidence column as well, per CLAUDE.md, with the refresh rate and percentile named.

---

## 11. Open questions, and what would be a decision

**Questions of fact, answerable by a probe:**

1. Does Windows XUSB bind espp's X-Input function, and does a real game read it? (Step 1 and 2 above.)
2. What is the latency distribution over each link?
3. Can `espp::UsbHost` and `espp::UsbDevice` coexist on an ESP32-P4's two OTG controllers, or is espp's restriction absolute?

**Questions that are the project owner's decision, not a probe's:**

- Reopening decision C at all. [Separate Game Machine 3.3](SEPARATE_GAME_MACHINE.md#33-not-pursued-a-usb-dongle) says so explicitly, and this document does not.
- Raising the Python floor from 3.8 to 3.10, if the shared-implementation idea in 2.3 were ever wanted.
- Shipping any USB identity, since the only one that gets XInput is Microsoft's.
- Rewriting the whitepaper's hardware-independence claim.

Under CLAUDE.md's "when to ask", a device backend would also touch: the `controller_config.json` shape (a target picker and a device identity), credential handling (the netpad key, which belongs in the keyring), and a driver interface that could inject unintended input. All three are ask-first items.

---

## 12. Sources

Read on 2026-09-16.

- [esp-cpp/espp](https://github.com/esp-cpp/espp) and its [documentation](https://esp-cpp.github.io/espp/)
- [espp `usb_device` README](https://github.com/esp-cpp/espp/blob/main/components/usb_device/README.md) and [`xinput_example`](https://github.com/esp-cpp/espp/tree/main/components/usb_device/xinput_example)
- [espp `xinput.hpp`](https://github.com/esp-cpp/espp/blob/main/components/usb_device/include/xinput.hpp)
- [espp `hid-rp`](https://github.com/esp-cpp/espp/tree/main/components/hid-rp), wrapping [intergatedcircuits/hid-rp](https://github.com/intergatedcircuits/hid-rp)
- [espp `usb_host`](https://github.com/esp-cpp/espp/tree/main/components/usb_host), [`hid_service`](https://github.com/esp-cpp/espp/tree/main/components/hid_service), [`ota`](https://github.com/esp-cpp/espp/tree/main/components/ota), [`coredump`](https://github.com/esp-cpp/espp/tree/main/components/coredump), [`dispatcher`](https://github.com/esp-cpp/espp/tree/main/components/dispatcher), [`stream_frame`](https://github.com/esp-cpp/espp/tree/main/components/stream_frame), [`wdi`](https://github.com/esp-cpp/espp/tree/main/components/wdi)
- [`espp` on PyPI](https://pypi.org/project/espp/), v1.3.3
- [finger563/esp-usb-ble-hid](https://github.com/finger563/esp-usb-ble-hid), v3.0.0
- [Open Mobility Hub Wheelchair Digital Interface](https://open-mobility-hub.github.io/wheelchair-digital-interface/)
- [ESP32-S3 USB device stack (Espressif)](https://docs.espressif.com/projects/esp-usb/en/latest/esp32s3/usb_device.html) and [USB-OTG overview (ESP-IoT-Solution)](https://docs.espressif.com/projects/esp-iot-solution/en/latest/usb/usb_overview/usb_otg.html)
- [ESP32-P4 USB host (Espressif)](https://docs.espressif.com/projects/esp-idf/en/latest/esp32p4/api-reference/peripherals/usb_host.html)
- [wiredopposite/OGX-Wireless-Lite](https://github.com/wiredopposite/OGX-Wireless-Lite), an independent ESP32-S3 firmware doing BLE gamepad to XInput, as corroboration that the X-Input path works on this silicon
- [GP2040-CE](https://github.com/OpenStickCommunity/GP2040-CE), the RP2040 firmware behind the dongle sketch in [Separate Game Machine 3.3](SEPARATE_GAME_MACHINE.md#33-not-pursued-a-usb-dongle)
