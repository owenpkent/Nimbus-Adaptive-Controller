# Hardware and device-class approaches: giving one app exclusive use of a pointing device without a kernel filter

Scope: ways to change what the physical pointer looks like to Windows (adapter hardware, HID usage tricks, driver swaps, second devices) so a foreground game never sees its motion in WM_INPUT / GameInput, while Nimbus (Python + ctypes, user mode) reads it and draws its own cursor. Researched 2026-09-25. All web content treated as data.

Baseline fact that frames everything below. Microsoft's table of "HID clients supported in Windows" lists which top-level collections (TLCs) Windows claims and how:

| Usage page | Usage | Client | Access |
| --- | --- | --- | --- |
| 0x01 | 0x01-0x02 | Mouse class driver and mapper | Exclusive |
| 0x01 | 0x04-0x05 | Game controllers | Shared |
| 0x01 | 0x06-0x07 | Keyboard/keypad | Exclusive |
| 0x0D | 0x01 / 0x02 | External / integrated pen | Exclusive |
| 0x0D | 0x04 | Touchscreen | Exclusive |
| 0x0D | 0x05 | Precision touchpad | Exclusive |
| 0x0C, 0x20, 0x84, 0x8C | various | Consumer, sensors, UPS, barcode | Shared |

"For security reasons, Raw Input Manager (RIM) opens all such devices exclusively. If RIM opens a device in exclusive mode, the user can still open a HID device interface without requesting read and write permissions and obtain HID device information via HIDClass support routines (HidD_GetXxx)." Vendor-defined pages (0xFF00-0xFFFF) are not in the table. [HID Architecture, Microsoft Learn](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/hid-architecture)

## 1. USB host-to-device adapter re-enumerating as a vendor-defined (raw) HID

### Takeaway
This is the most promising non-kernel option and it genuinely keeps the device out of the mouse path: a microcontroller that hosts the real mouse and presents only a vendor-defined-usage TLC gets no mouse class driver, does not move the cursor, and is only visible in Raw Input to an app that registers for that exact vendor usage page/usage; Nimbus can read it from user mode with hid.dll/ReadFile (or hidapi) with no driver install. Cost is roughly USD 17.50 for an off-the-shelf RP2040 USB-host board, but it requires custom firmware (no existing project found that does exactly "mouse in, vendor-defined HID out" off the shelf), and the user must buy/flash hardware.

### Cited Findings
- Windows attaches the mouse class driver and mapper only to TLCs with usage page 0x01, usage 0x01-0x02, and RIM opens those exclusively; vendor-defined pages are not claimed by any inbox client in the table. [Microsoft Learn: HID Architecture](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/hid-architecture)
- User-mode apps open a HID collection by finding it with SetupDi functions, calling CreateFile, HidD_ routines for preparsed data, ReadFile for input reports, HidP_ routines to parse them. [Microsoft Learn: Opening HID Collections](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/opening-hid-collections)
- Raw Input is opt-in per TLC: "By default, no application receives raw input"; an app registers a usage page + usage, and "receives raw input from any HID whose top level collection (TLC) matches a TLC from the registration." Non-mouse/keyboard devices arrive as RIM_TYPEHID, not RIM_TYPEMOUSE. [Microsoft Learn: Raw Input Overview](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input); [RAWINPUTHEADER](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputheader)
- Precedent for vendor-defined HID read from user mode with stock OS drivers: QMK keyboards expose a "Raw HID" interface at usage page 0xFF60, usage 0x61, and host software uses hidapi / pywinusb / the Python `hid` package on Windows; QMK advises filtering by usage page and usage "to avoid accidentally opening the interface on the same device for the keyboard, or mouse". [QMK docs: Raw HID](https://docs.qmk.fm/features/rawhid)
- Chrome's WebHID refuses to expose keyboard, mouse, pointer and similar protected-usage TLCs but does expose other (e.g. vendor-defined) TLCs to web pages, which run without admin rights. [Chrome for Developers: Connect to uncommon HID devices](https://developer.chrome.com/docs/capabilities/hid)
- HID Remapper (jfedor2) is an existing open-source RP2040 firmware that sits between a USB mouse/keyboard and the PC; it runs on a single Pico, a dual Pico, the Adafruit Feather RP2040 USB Host, or custom boards; it is configured "through a web browser using WebHID" (so it already carries a vendor-defined config TLC alongside its mouse/keyboard TLCs); a Bluetooth variant runs on nRF52840 boards (Feather nRF52840 Express, Xiao nRF52840) and "translates Bluetooth inputs to USB". [GitHub: jfedor2/hid-remapper](https://github.com/jfedor2/hid-remapper)
- Adafruit Feather RP2040 with USB Type A Host: USD 17.50; USB host is emulated with the RP2040 PIO (sekigon's Pico-PIO-USB), dedicating the second core and one PIO block; TinyUSB "dual role" examples include HID mouse. [Adafruit product 5723](https://www.adafruit.com/product/5723); [Circuit Cellar](https://circuitcellar.com/newsletter/adafruit-releases-feather-rp2040-board-with-two-usb-ports/)
- GameInput 3.4 (released 2026-05-21) added raw HID reports: "Titles can now read raw HID input reports from devices and send raw HID output reports through GameInput"; the announcement does not say which devices are enumerated for this. [Microsoft Game Dev: GameInput 3.4](https://developer.microsoft.com/en-us/games/articles/2026/05/gameinput-update-now-available/); [GameInputKind reference](https://learn.microsoft.com/en-us/gaming/gdk/docs/reference/input/gameinput/enums/gameinputkind?view=gdk-2604)

### Inferences
- A device whose only TLC is vendor-defined (usage page 0xFF00+) will not get mouhid/mouclass, so it cannot move the system cursor or generate RIM_TYPEMOUSE / WM_MOUSEMOVE. A game that registers Raw Input for 0x01/0x02 (the universal mouse registration) never receives it. A game would have to deliberately register the exact vendor page, or use GameInput raw reports against that specific device, which no game has a reason to do. This meets the "game never sees it" requirement far better than any other option here.
- The TLC must contain no mouse usage at all. If the firmware keeps a mouse TLC (as HID Remapper's normal output does) alongside the vendor TLC, Windows still claims the mouse TLC and the game sees motion. The firmware must drop the mouse interface entirely while in "Nimbus mode" (a mode switch, e.g. a button chord, could restore normal mouse output for desktop use).
- Nimbus user-mode read path: SetupDiGetClassDevs(GUID_DEVINTERFACE_HID) to enumerate, HidD_GetAttributes / HidP_GetCaps to match VID/PID and UsagePage, CreateFile with GENERIC_READ and FILE_SHARE_READ|WRITE, overlapped ReadFile on a reader thread. All of it is plain ctypes over hid.dll/setupapi.dll/kernel32, or the `hid` (hidapi) PyPI package. No admin rights should be needed for a non-exclusive vendor TLC (consistent with WebHID and QMK Raw HID practice), but I found no Microsoft page stating the default ACL of HID collection PDOs explicitly (see Gaps).
- Report format is under Nimbus's control: firmware can forward the raw mouse deltas/buttons in a fixed little packet (e.g. dx, dy as int16, buttons, wheel), which is simpler to parse than arbitrary mouse report descriptors.
- Practical costs for the user: buy a ~USD 17.50 board (or a Pico plus a USB-A breakout), flash a UF2 by drag-and-drop (no admin, no driver), plug the mouse into the board. Bluetooth pointers would need the nRF52840 variant or a Raspberry Pi acting as BT host (see section 6). For an accessibility audience the hardware step is a real barrier, but it is a one-time plug-in with no Windows configuration and no test signing, which is a large safety improvement over a mandatory mouse-class upper filter (the failure mode noted in CLAUDE.md where Windows starts with no mouse at all cannot happen; unplug the adapter and the mouse works normally again).
- Alternative output class: making the adapter present a gamepad (usage 0x01/0x04-0x05) instead is a bad fit, because game controllers are "Shared" and games actively enumerate them, so the game would see a second pad next to Nimbus's virtual pad.

### Gaps
- No existing firmware found that ships "USB mouse in, vendor-defined-only HID out" as a ready product; HID Remapper is the closest base (open source, RP2040, already has a WebHID vendor TLC), but whether its output can be restricted to a vendor TLC only was not confirmed from its README. GP2040-CE, Teensy 4.1 USBHost_t36, ESP32-S3 and USB Host Shield projects were not checked individually.
- Could not find Microsoft documentation stating the default security descriptor on HID collection device interfaces (i.e. an explicit statement that a standard user can open a vendor-defined collection for read). Practice (WebHID, hidapi, QMK tooling) strongly indicates yes.
- Whether GameInput 3.4's raw-report enumeration surfaces arbitrary vendor-defined HID devices to titles by default was not documented in what I found; a game would still have to opt in to reading that device.
- Latency/polling limits of PIO USB host (full/low speed only, so high-speed 4-8 kHz gaming mice would be down-clocked) were not sourced.

## 2. Re-enumerating as a digitizer (touchscreen, pen) instead

### Takeaway
Not useful. Every digitizer usage Windows supports (pen, touchscreen, precision touchpad) is opened exclusively by RIM, is fed into the pointer stack that drives the system cursor / promoted mouse messages, and therefore cannot be read by Nimbus via ReadFile and does reach the foreground app.

### Cited Findings
- Usage page 0x0D usages 0x01 (external pen), 0x02 (integrated pen), 0x04 (touchscreen), 0x05 (precision touchpad) are all "Exclusive" and opened by RIM. [Microsoft Learn: HID Architecture](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/hid-architecture)
- Precision touchpad input is delivered as mouse raw input with a null device handle: "hDevice can be zero if an input is received from a precision touchpad." [Microsoft Learn: RAWINPUTHEADER](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputheader)
- Apps can also register raw input for touchpads explicitly with HID_USAGE_PAGE_DIGITIZER / HID_USAGE_DIGITIZER_TOUCH_PAD. [Microsoft Learn: Using Raw Input](https://learn.microsoft.com/en-us/windows/win32/inputdev/using-raw-input)
- Pen events arrive as WM_MOUSEMOVE etc. tagged with the MOUSEEVENTF_FROMTOUCH signature; Windows 8 unified mouse, touch and pen under the pointer input API (WM_POINTER). [The Witness: WM_TOUCH is totally bananas](http://the-witness.net/news/2012/10/wm_touch-is-totally-bananas/) (developer blog, 2012; older but consistent with Microsoft's pointer model)

### Inferences
- A digitizer-class adapter would move the real system cursor (or inject pointer/touch contacts into whichever window is under the contact), so clicks and focus would go to the game: the opposite of the goal. And since RIM holds the collection exclusively, Nimbus could not open it for ReadFile.
- A PTP-class device is the worst case: it shows up as mouse raw input to every game.

### Gaps
- I did not find a Microsoft statement on whether pen or touchscreen contacts also generate WM_INPUT RIM_TYPEMOUSE for apps registered on 0x01/0x02. It does not matter for the conclusion (they are exclusive and drive the cursor either way).

## 3. Swapping the mouse's driver to WinUSB / libusbK with Zadig and reading with libusb / pyusb

### Takeaway
It works for USB (including USB-dongle wireless) HID mice and does remove the device from Windows' mouse path entirely, with no custom kernel code and no test signing, but it needs admin/UAC to install, makes the device a non-mouse everywhere (not per-app, not per-session), is awkward to revert, can be silently rolled back by Windows, and does nothing for Bluetooth pointers. As a toggle for "game mode" it is impractical.

### Cited Findings
- Zadig warning: "If you replace the existing driver for an USB keyboard or mouse, it will no longer be usable for input in Windows but instead will only work with a custom application." [Zadig wiki (libwdi)](https://github.com/pbatard/libwdi/wiki/Zadig)
- Zadig requires administrative privileges (UAC prompt at start); with its default drivers it can generate and install a self-signed certificate on the machine to sign the generated driver catalog. [Zadig wiki](https://github.com/pbatard/libwdi/wiki/Zadig)
- Driver options are WinUSB, libusb-win32/libusb0, and libusbK; "List All Devices" shows mice already bound to HidUsb; composite devices expose per-interface (MI) entries that can be targeted individually. [Zadig wiki](https://github.com/pbatard/libwdi/wiki/Zadig); [Zadig site](https://zadig.akeo.ie/)
- Windows may roll a libusbK install back to the previous driver after a reboot or reconnection, because it keeps choosing the "best" ranked driver. Reverting means removing the competing package with `pnputil /delete-driver` after identifying its oemNN.inf. [Zadig wiki](https://github.com/pbatard/libwdi/wiki/Zadig)
- Zadig itself does not offer a "restore HidUsb" button; a 2012 feature request for exactly that (libwdi issue #8) shows no implemented resolution. [libwdi issue #8](https://github.com/pbatard/libwdi/issues/8)
- pnputil (the removal/uninstall tool) must be run from an elevated prompt; `/delete-driver <oem#.inf> /uninstall` removes the package from devices using it. [Microsoft Learn: PnPUtil Command Syntax](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/pnputil-command-syntax)

### Inferences
- Once on WinUSB, the device is no longer a HID class device, so neither RIM, mouclass, Raw Input nor GameInput sees it. Nimbus could read its interrupt IN endpoint with pyusb + libusb-1.0 (WinUSB backend) and parse the mouse's own report (boot protocol, or its report descriptor), all in user mode. WinUSB itself is an inbox, Microsoft-signed driver, so unlike the Nimbus filter this does not need test signing; only the catalog is self-signed by Zadig.
- Risks specific to this project's users: if the swapped mouse is the user's only pointer, they lose the cursor on the desktop too (Nimbus's software cursor would have to cover the whole desktop, not just the game session). Swapping back per session needs admin each time and device re-enumeration. Many gaming mice and all Logitech Unifying/Bolt receivers are composite (a keyboard interface next to the mouse one); swapping the whole device or the wrong MI kills the keyboard function too.
- Bluetooth mice are enumerated through BTHHID, not USB, so WinUSB/libusb cannot claim them. Built-in laptop touchpads (I2C/SPI HID) are likewise out of reach.
- Net: technically sound for a dedicated second USB pointer the user reserves for Nimbus; not a safe general replacement for the filter driver.

### Gaps
- Did not verify how libwdi handles re-signing when Secure Boot is on (whether the self-signed cert path works unchanged on current Windows 11 builds with Secure Boot, which the Nimbus filter cannot). The Zadig wiki implies it does, since it installs a cert into the machine stores rather than requiring test mode.
- Did not find measurements of Windows' auto-rollback frequency for WinUSB (vs libusbK) installs on HID devices.

## 4. Disabling the mouse device (SetupAPI / pnputil) and reading it another way

### Takeaway
Not viable. Disabling a device tears down its driver stack, so there is nothing left for a user-mode app to open, and disabling requires admin anyway.

### Cited Findings
- `pnputil /disable-device` (Windows 10 2004+) disables devices; pnputil is documented to be run from an elevated ("Run as Administrator") prompt; `/force` exists to "disable even if device provides critical system functionality". [Microsoft Learn: PnPUtil Command Syntax](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/pnputil-command-syntax)
- User-mode HID access goes through a HID collection device interface opened with CreateFile. [Microsoft Learn: Opening HID Collections](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/opening-hid-collections)

### Inferences
- A disabled devnode has no function driver loaded and its device interfaces are disabled, so CreateFile/ReadFile (or WinUSB) have nothing to open. Disabling only the "HID-compliant mouse" child devnode is the same thing: that devnode is the HID collection itself, so its interface disappears with it. Reading a disabled device requires a driver below it (which is what the Nimbus filter is).
- The only way "disable" helps is paired with a driver swap (section 3), which is a driver change, not a disable.

### Gaps
- No Microsoft page found that states verbatim that device interfaces of a disabled devnode are unavailable; this is standard PnP behaviour but unsourced here.

## 5. Accessibility pointing devices: SDKs or non-mouse modes

### Takeaway
The eye-tracker route is real: a Tobii consumer tracker is not a HID mouse at all; cursor control is done by vendor software, and Tobii ships SDKs (Tobii Game Integration, Stream Engine) that give an app gaze and head-pose streams directly, so Nimbus could drive its own cursor from gaze without the game seeing any pointer. Camera head mice (TrackerPro 2, SmartNav) present as ordinary mice as far as found; GlassOuse Pro has a Bluetooth "Joystick mode", but that makes it a game controller the game will see.

### Cited Findings
- Tobii Game Integration (TGI) is a native C++ API with C# bindings providing "eye tracking, head tracking and presence data streams", gaze remapping and filtering, aimed specifically at PC games; about 150 games used it by mid-2022; use is governed by a Tobii license agreement. [Tobii Developer Zone: Tobii Game Integration](https://developer.tobii.com/pc-gaming/develop/tobii-game-integration/)
- Tobii distinguishes TGI (gaming-focused consumer signals) from Stream Engine, which "supports all Tobii streams, products and use cases". [Tobii Developer Zone](https://developer.tobii.com/pc-gaming/develop/); [Tobii SDKs overview](https://www.tobii.com/products/software/applications-and-developer-kits)
- TrackerPro 2: a reflective dot on the forehead/glasses; "small head movements control the mouse cursor movement". [Spectronics: TrackerPro 2](https://www.spectronics.com.au/product/trackerpro-2)
- SmartNav: monitor-mounted IR camera tracking a dot, "software maps head motion to cursor motion". [RJ Cooper: SmartNav](https://store.rjcooper.com/products/smartnav-head-tracker)
- GlassOuse PRO: Bluetooth (BT 4.0) head mouse; for gaming the user must "change to Joystick mode" and "the game should support joystick functionality"; listed at USD 901.59 (reduced from 1,199.11) at Rehabmart. [Rehabmart: GlassOuse PRO](https://www.rehabmart.com/product/next-generation-assistive-device-glassouse-pro-head-mouse-and-switch-control-interface-53000.html)
- Cedar Integrated Joystick (Neil Squire / OpenAT, open source): a small USB joystick with three switch jacks that "can act as either an HID gamepad or as a mouse"; in joystick mode it works with the Xbox Adaptive Controller; the related Forest Hub switches between USB Mouse mode and USB Gamepad mode with a switch. [Neil Squire: OpenAT Joysticks](https://www.neilsquire.ca/openat-joysticks-to-increase-digital-access-for-canadians-with-disabilities/)

### Inferences
- For eye-gaze users, the cleanest architecture is Nimbus reading gaze directly through a Tobii SDK and drawing its own cursor, with the Windows cursor left free (or driven by Tobii's own software only when the user wants desktop control). No HID mouse is involved, so nothing reaches WM_INPUT. Python would need a small wrapper around the C API (ctypes over the Stream Engine DLL is plausible).
- For camera head mice (TrackerPro, SmartNav), the vendor software/firmware emits a normal mouse, so they have the same problem as any mouse; an alternative is that Nimbus does its own camera head tracking (not researched here).
- Devices with a "joystick mode" (GlassOuse, Cedar, Forest Hub) become Shared-access game controllers (section 6), so the game can see them.

### Gaps
- Tobii license terms (whether TGI or Stream Engine may be used by a non-game accessibility app that redistributes nothing, and any restrictions on consumer-tracker data) were not retrievable from the pages fetched; must be read from the actual agreements.
- Whether TrackerPro 2 or SmartNav offer any SDK or non-mouse output mode: none found. SmartNav's current availability (NaturalPoint) not confirmed.
- Sip-and-puff devices (e.g. Jouse / JouseLite, QuadJoy) were not examined in detail; the Jouse Lite is marketed as a mouth-controlled joystick mouse. [Rehabmart: Jouse Lite](https://www.rehabmart.com/product/mouthcontrolled-joystick-handsfree-assistive-technology-for-accessibility-compusult-54684.html)

## 6. A second physical device of gamepad/joystick class driving a Nimbus-drawn cursor

### Takeaway
Works at the input level (Nimbus can read a gamepad/joystick with shared access, from user mode, and the Windows cursor stays with the real mouse), but it does not hide the device from the game: game controllers are Shared TLCs that games enumerate through DirectInput, Raw Input, GameInput, SDL and Steam Input, so the game may bind it or confuse it with Nimbus's virtual pad. The vendor-defined adapter (section 1) is strictly better when the goal is invisibility.

### Cited Findings
- Game controllers (0x01/0x04-0x05) are "Shared" access HID clients in Windows. [Microsoft Learn: HID Architecture](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/hid-architecture)
- GameInput enumerates HID devices and exposes "the full list of buttons, switches, and axes available on a device ... defined by the device's HID report descriptor"; titles can register device callbacks. [Microsoft Learn: GameInput device interfaces](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/common/input/hardware/input-hardware-interfaces?view=gdk-2604)
- Makers Making Change Wheelchair Bluetooth Joystick Adapter: a Raspberry Pi acts as a Bluetooth HID host for powerchair modules that emit a Bluetooth HID mouse (Q-Logic, LiNX REM400, R-Net), and outputs a USB HID gamepad; materials about USD 65 plus 20-25 shipping; MIT software, CC BY-SA 4.0 docs; v0.1 (May 2024) "built and tested with a few users. Further development and testing is required." [GitHub: makersmakingchange/Wheelchair-Bluetooth-Adapter](https://github.com/makersmakingchange/Wheelchair-Bluetooth-Adapter)
- Cedar joystick / Forest Hub switch between USB mouse and USB gamepad output. [Neil Squire](https://www.neilsquire.ca/openat-joysticks-to-increase-digital-access-for-canadians-with-disabilities/)

### Inferences
- The Makers Making Change adapter proves the Bluetooth-host-to-USB-HID pattern on cheap hardware for exactly the powerchair population; retargeting its USB output from "gamepad" to "vendor-defined" would give a Bluetooth-capable version of section 1 (and HID Remapper's nRF52840 build is another base).
- If a gamepad-class second device is used anyway, Nimbus should read it with HidD_/ReadFile (shared) or Raw Input RIM_TYPEHID, and the user must make sure the game picks Nimbus's virtual pad as player one. Hiding a real controller from games normally needs a filter driver (e.g. the signed HidHide), which is outside the "non-kernel" scope.

### Gaps
- Did not verify how individual games choose among multiple connected pads (first enumerated vs last active), which decides how harmful a visible second controller is.
- HidHide's current signing status and behaviour were not sourced in this pass.
