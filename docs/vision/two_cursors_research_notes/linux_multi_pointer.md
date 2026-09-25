# Linux multi-pointer (X11 MPX and Wayland multi-seat) as an alternative to EVIOCGRAB plus a software cursor

Research date: 2026-09-25. All web content was treated as data. Source code was read from the current `master`/`main`/`dev` branches on GitHub at research time (Wine, SDL2 branch, SDL3 main, Qt qtbase dev, Xwayland mirror); line-level behaviour may drift. Most MPX tutorial material found is 2009-2012 and is marked stale where used.

## 1. X11 MPX: if the physical mouse drives master pointer B, does a game on master A still receive its motion?

### Takeaway
Partly, and it depends on how the game reads the mouse. XI 2.1+ raw events go to every root window whatever the master or grab, so a native SDL2/SDL3 game in relative mouse mode on X11 **will** see pointer B's motion as its own camera input. Wine/Proton filters raw motion to its ClientPointer master and would ignore pointer B. MPX by itself therefore does not isolate the mouse from native SDL games, and EVIOCGRAB does.

### Cited Findings
- XI2 spec: "RawEvents are sent exclusively to all root windows." And: "Clients supporting XI 2.0 receive raw events when the device is not grabbed, or when the device is grabbed by the client but not when the device is grabbed by another client. Clients supporting XI 2.1 or later receive raw events at all times, even when the device is grabbed by another client." [XI2proto.txt](https://xorg.freedesktop.org/archive/current/doc/inputproto/XI2proto.txt)
- XI2 spec: raw events carry `sourceid`, "The source device that originally generated the event." Selecting with `XIAllMasterDevices` delivers the event with `deviceid` set to the master the slave is attached to (the spec's device-selection model; the exact master/slave `deviceid` wording was not quoted in the fetched excerpt). [XI2proto.txt](https://xorg.freedesktop.org/archive/current/doc/inputproto/XI2proto.txt)
- SDL3 (main): selects `XI_RawMotion` with `deviceid = XIAllMasterDevices` on `DefaultRootWindow`. The raw-motion handler looks up `rawev->deviceid`, skips pens, and forwards motion when `mouse->relative_mode && SDL_GetKeyboardFocus()`, calling `SDL_SendMouseMotion(rawev->time, mouse->focus, (SDL_MouseID)rawev->sourceid, true, ...)`. It does **not** filter by master: there is "no explicit master/slave filtering in the raw motion handler". [SDL3 SDL_x11xinput2.c](https://github.com/libsdl-org/SDL/blob/main/src/video/x11/SDL_x11xinput2.c)
- SDL2 (SDL2 branch): the same `XIAllMasterDevices` root selection. The `XI_RawMotion` case returns early only `if (!mouse->relative_mode || mouse->relative_mode_warp)`, then sends `SDL_SendMouseMotion(mouse->focus, mouse->mouseID, 1, ...)`. No master filter. [SDL2 SDL_x11xinput2.c](https://github.com/libsdl-org/SDL/blob/SDL2/src/video/x11/SDL_x11xinput2.c)
- Wine winex11.drv (master): `x11drv_xinput2_init` stores the ClientPointer: `if (!pXIGetClientPointer( data->display, None, &data->xinput2_pointer ))`. `map_raw_event_coords` then drops anything else: `if (event->deviceid != thread_data->xinput2_pointer) return pt;`. Raw motion is selected on the root window, and cursor position is only produced while `clipping_cursor` (the relative/ClipCursor case). [Wine mouse.c](https://github.com/wine-mirror/wine/blob/master/dlls/winex11.drv/mouse.c)
- Wine history: a 2016 patch selected `XI_RawMotion` on `XIAllDevices`; current code uses `XIAllMasterDevices` and "listen[s] to master device events only". [wine-patches 2016 (stale, superseded)](https://www.winehq.org/pipermail/wine-patches/2016-December/156437.html); [Wine mouse.c](https://github.com/wine-mirror/wine/blob/master/dlls/winex11.drv/mouse.c)
- A 2019 Wine patch made winex11 advertise XI 2.1 so it gets RawMotion "even when a mouse grab is active". [wine-devel 2019](https://list.winehq.org/pipermail/wine-devel/2019-November/154726.html)
- XI2 spec on ClientPointer: "For each client, one master pointer is designated as this clients's 'ClientPointer'. Whenever a client sends an ambiguous request (e.g. QueryPointer), the ClientPointer or the keyboard paired with the ClientPointer is chosen to provide the data for this request." [XI2proto.txt](https://xorg.freedesktop.org/archive/current/doc/inputproto/XI2proto.txt)

### Inferences
- Native Linux games using SDL2 in non-warp relative mode, or SDL3 in relative mode while they have keyboard focus (which they do, since Nimbus never takes focus), would turn their camera when the user moves the mouse over Nimbus with pointer B. Moving the mouse to operate the controller would also aim in-game. That is the exact failure EVIOCGRAB prevents today.
- SDL2 in `relative_mode_warp` (the warp fallback, set via hint or when raw input is unavailable) ignores raw motion and uses core/XI2 motion over its window. Pointer B stays over Nimbus, so it would likely not leak in that mode. Most SDL2 games use raw XI2 relative mode by default, so this is the minority case.
- Proton/Wine games: since Wine's ClientPointer is normally the first master (A), pointer B's raw motion is dropped. Proton titles would probably be isolated, subject to the gap below on ClientPointer defaults.
- Games or engines that call `XISelectEvents(root, XIAllDevices, XI_RawMotion)` or read evdev/hidraw directly (some anti-cheat, some engines, Steam Input for mice) see everything regardless of MPX. EVIOCGRAB blocks the evdev path for other readers; MPX blocks nothing below the X server.
- A related X-only option (not MPX): `xinput float <slave>` detaches the physical mouse from every master. Clients selecting on `XIAllMasterDevices` (SDL2, SDL3, Wine) would then get nothing from it, and Nimbus could read the floating slave through XI2 with `XIAllDevices`. It still needs a software cursor, like EVIOCGRAB, and it is weaker isolation (anyone selecting `XIAllDevices` or reading evdev still sees it). It is not a clear improvement over EVIOCGRAB.

### Gaps
- Not verified on hardware. The SDL3 claim rests on the fetched handler; whether `mouse->relative_mode` is true in every shipping SDL3 title varies per game.
- The spec text on which master Xorg picks as the default ClientPointer (`PickPointer` in the server) was not fetched. The claim that it is the Virtual Core Pointer (A) rests on server behaviour, not a quoted source.
- Unreal/Unity native Linux builds: not checked. Unity uses its own X11 input layer, and whether it filters by master is unknown.

## 2. Focus semantics in MPX, Qt 6 support, and window-manager support (2026)

### Takeaway
Each master pointer is permanently paired with its own master keyboard, and focus is per master keyboard. That works for "click Nimbus with B, game keeps A's focus", but it also creates stuck-focus hazards. Qt 6 notices MPX but has one global cursor and mouse-grabber model. Mutter on X11 only grabs master id 2, and GNOME 50 (Ubuntu 26.04) has removed the X11 session entirely, so MPX has no future on the target desktops.

### Cited Findings
- XI2 spec: "Each master pointer is paired with the respective master keyboard and vice versa, and this pairing is constant for the lifetime of both input devices." [XI2proto.txt](https://xorg.freedesktop.org/archive/current/doc/inputproto/XI2proto.txt)
- XI2 spec: ambiguous core requests use "the ClientPointer or the keyboard paired with the ClientPointer". So a non-MPX-aware WM calling core `SetInputFocus` sets focus for its own ClientPointer's keyboard (normally A's), not for the keyboard paired with whichever pointer clicked. [XI2proto.txt](https://xorg.freedesktop.org/archive/current/doc/inputproto/XI2proto.txt)
- Real 2026 failure from a project using MPX pairs for input injection (Ubuntu 26.04, Xorg via xorgxrdp, XFCE/xfwm4): "X11 can attach a window's focus to it, and once that happens the real keyboard, which feeds the _core_ master, no longer reaches that window." The symptom was "clicks work, typing doesn't", and it persisted when master devices leaked on an abnormal exit. They also note "Pointer input keeps working, because it follows position rather than focus attachment." [trycua/cua #3337 (2026-08-23)](https://github.com/trycua/cua/issues/3337)
- The same project reports that `XIRemoveMaster` "churns the XInput hierarchy, and mutter 42.x segfaults processing that churn". It had to stop tearing master pairs down on idle. [trycua/cua PR #4053](https://github.com/trycua/cua/pull/4053) (seen as a search snippet only, not fetched in full)
- Mutter issue "managing multiple pointers" (#367), open: "Second pointer is not grabbed by Mutter. Only the first master is." Root cause: "`#define META_VIRTUAL_CORE_POINTER_ID 2`" is hard-coded. [GNOME/mutter #367](https://gitlab.gnome.org/GNOME/mutter/-/work_items/367)
- Stale user report of Xorg crashing when a slave is attached to a new master. [Arch forums (about 2013, stale)](https://bbs.archlinux.org/viewtopic.php?id=166088). The old how-tos are from 2010. [dbrgn 2010 (stale)](https://blog.dbrgn.ch/2010/7/14/how-to-use-mpx-to-control-several-pointers-at-the-same-time/); [ao2.it 2010 (stale)](https://ao2.it/en/blog/2010/01/19/poor-mans-multi-touch-using-multiple-mice-xorg)
- Qt 6 (qtbase dev, xcb plugin): selects XI2 button/motion/enter/leave with `XCB_INPUT_DEVICE_ALL`. It records `m_xiMasterPointerIds` and drops slave-pointer events (`if (m_xiSlavePointerIds.contains(xiEvent->deviceid) ...) return;`). It only logs `"multi-pointer X detected"` when `m_xiMasterPointerIds.size() > 1`, and re-runs `xi2SetupDevices()` on `XI_HierarchyChanged` master/slave add/remove. [qxcbconnection_xi2.cpp](https://github.com/qt/qtbase/blob/dev/src/plugins/platforms/xcb/qxcbconnection_xi2.cpp)
- Older Qt bug on XI2 device discovery skipping non-`XISlavePointer` uses (floating slaves). [QTBUG-45494](https://bugreports.qt.io/browse/QTBUG-45494) (not fetched; seen via search snippet)
- GNOME 50 (released 2026-03-19) removed the X11 session; Ubuntu 26.04 LTS ships it, and "You cannot log into an X11 GNOME session even if X.org is installed". X11 apps continue under XWayland. [Aliteq](https://aliteq.com/gnome-50-removes-x11-ubuntu-26-04-wayland); [gHacks 2026-03-20](https://www.ghacks.net/2026/03/20/gnome-50-releases-with-x11-session-support-removed-and-wayland/) (secondary news sources; they agree with each other)
- A tutorialspoint page claims "GNOME and KDE provide built-in MPX support with minimal configuration". This is contradicted by the open Mutter issue above and should be disregarded. [tutorialspoint (low quality)](https://www.tutorialspoint.com/article/multi-pointer-x-mpx-support)

### Inferences
- Nimbus sets `Qt.WindowDoesNotAcceptFocus` (WM_HINTS input=False), so a well-behaved WM will not focus it on a pointer-B click, and B's paired keyboard is unused. The focus risk is mostly the reverse: pointer B's click lands on WM frames or passive grabs that only listen on master 2 (Mutter), so it is ignored, or the WM treats it as core input and moves A's focus.
- Qt treats all master pointers as one mouse (a single `QCursor::pos()`, answered via `QueryPointer` on the ClientPointer A, and one mouse grabber/implicit grab). If pointer A (the game's, possibly warped to screen centre) ever crosses the Nimbus window mid-drag on B, Qt's press/move/release state for a stick drag could be corrupted. Hover and tooltip logic keyed on `QCursor::pos()` would follow A, not B.
- The master pair lifecycle is itself a hazard: a crash of Nimbus leaves the pair and the reattached mouse behind (as in cua #3337). That is the same class of bug as a leaked EVIOCGRAB, but it survives the process, while an evdev grab is released when the fd closes.
- KWin on X11: no evidence found either way; KDE is also moving off X11.

### Gaps
- No Qt bug tracker entry was found specifically for two master pointers delivering interleaved events to one QQuickWindow. Qt's behaviour here is inferred from its source, not from tests.
- KWin X11 MPX behaviour and KDE's X11-session removal timeline were not verified.
- Whether xfwm4, i3 and similar WMs route the pointer-B click into a core focus change was not verified.

## 3. Does the uinput virtual gamepad path interact with MPX?

### Takeaway
No interaction is expected. The virtual pad is an evdev joystick that games read through SDL's joystick/gamecontroller layer, not through the X pointer hierarchy.

### Cited Findings
- SDL3's X11 raw-motion path only deals with pointer and pen devices (`X11_FindPenByDeviceID`, `SDL_SendMouseMotion`); gamepads are not part of the XI2 path. [SDL3 SDL_x11xinput2.c](https://github.com/libsdl-org/SDL/blob/main/src/video/x11/SDL_x11xinput2.c)

### Inferences
- The uinput pad has no REL_X/REL_Y or BTN_LEFT, so the X server's libinput driver should not add it as a pointer slave, and reattaching mice between masters cannot touch it. Keyboard focus pairing does not matter for joystick input, which SDL reads regardless of window focus by default (subject to SDL's background-events hint).
- The one real interaction is Nimbus's own controller pulse (`controller_pulse.py`), which emits through the pad, not the pointer. It is unaffected.

### Gaps
- The libinput "does not handle joysticks" statement was not fetched and cited this session. Verify against the libinput docs before relying on it.

## 4. Wayland: multi-seat in one session, per-seat focus, and XWayland/Proton isolation

### Takeaway
Wayland's model is the right shape: each `wl_seat` has its own cursor, pointer focus and keyboard focus, and relative-pointer events go only to the surface focused by that seat's pointer. But only wlroots-family compositors (Sway, River) and Weston support several seats in one session. GNOME/Mutter and KDE/KWin, the desktops Nimbus users actually run, do not. Xwayland folds all seats into one X master, and SDL3 multi-seat is new and partial.

### Cited Findings
- Survey (2026-07-28): Weston "Deeply integrated!" with per-seat focus; Sway 4/5 with multi-seat support ("devices can duplicate if reassigned"); River 5/5, "implements `ext-transient-seat-v1`"; Niri "No support yet". GNOME and KDE are not listed as supporting multiple seats. Toolkits: GTK 4/5 but "New seats are not registered while app is running"; SDL 3/5, "supports multi-seat since v3.3.4 but requires relative mouse mode for device IDs"; Qt "not investigated"; Chromium "doesn't seem to react to events from additional seats at all". [blinry, State of multi-player Wayland](https://blinry.org/multi-seat-wayland/)
- Sway: seat-to-device assignment with `swaymsg seat <name> attach <input>`. wlroots gained multi-seat in 2017 and sway 1.0 has per-seat focus. There is a 1:1 seat/cursor relationship "because there can only be a single pointer location within a surface per seat". Multi-focus "won't work well for Xwayland because it can only have one window focused at a time". [wlroots #1499](https://github.com/swaywm/wlroots/issues/1499); [sway #1588](https://github.com/swaywm/sway/issues/1588) (search snippets; the Xwayland focus quote comes from these threads)
- SDL merged Wayland multi-seat (Apr 2025): per-seat state, and "multiple windows can have keyboard and/or mouse focus at the same time on some compositors", which "is not currently a well-handled case in SDL, and will require more work to support". Tested on Sway. [Phoronix 2025-04-12](https://www.phoronix.com/news/SDL-Merges-Wayland-Multi-Seat); [SDL PR #12626](https://github.com/libsdl-org/SDL/pull/12626)
- Xwayland: each `wl_seat` gets an `xwl_seat`, but `add_device` creates its devices with `dev->type = SLAVE;` and activates them into the normal hierarchy, so there are no per-seat X masters. Pointer warp/confine/relative-pointer emulation is per `xwl_seat`, with no cross-seat coordination. [xwayland-input.c](https://github.com/mirror/xserver/blob/master/hw/xwayland/xwayland-input.c)
- GNOME/KDE "multiseat" in the logind sense (seat1 with its own GDM/SDDM greeter and its own session) is a different feature: separate login sessions per seat, historically X11-only for non-seat0 seats. It is not two cursors in one session. [GNOME Discourse](https://discourse.gnome.org/t/only-one-seat-uses-wayland-in-multiseat-setup/5516); [Debian gdm3 bug 2023](https://alioth-lists.debian.net/pipermail/pkg-gnome-maintainers/2023-February/175978.html); [ArchWiki Multiseat](https://wiki.archlinux.org/title/Multiseat)
- Mutter's pointer handling is hard-coded around a single core pointer (X11 backend, issue #367 above). No source was found showing Mutter or KWin exposing a second `wl_seat` to clients. [GNOME/mutter #367](https://gitlab.gnome.org/GNOME/mutter/-/work_items/367)

### Inferences
- On Sway/River with Nimbus as a **native Wayland** Qt client on seat B and the game on seat A: the compositor sends B's `wl_pointer` and relative-pointer events only to the surface under B's pointer (Nimbus). A Proton game (via Xwayland) or a native SDL Wayland game never receives B's motion, because Wayland has no equivalent of X11's root-window raw events. This is genuine isolation, and a click on Nimbus changes only seat B's keyboard focus.
- If Nimbus runs under XWayland instead (Qt xcb, the current X11 path), pointer B's events over Nimbus enter Xwayland as a slave of the same single X master that the Proton game's Wine reads raw motion from. The leak comes back. So a Wayland multi-seat design requires `QT_QPA_PLATFORM=wayland`, and the `Qt.WindowDoesNotAcceptFocus` / X11-specific paths would need Wayland equivalents.
- Qt's Wayland plugin handling of more than one `wl_seat` is untested per the survey. `QCursor::pos()` and mouse-grab semantics would need verification.
- Nimbus cannot create a seat itself. Seat assignment is compositor configuration (sway config) or `ext-transient-seat-v1` on River. On GNOME and KDE, the dominant desktops including Ubuntu 26.04, there is no path at all.

### Gaps
- No primary source found for Mutter/KWin plans to support multiple seats per session in 2026.
- Qt 6 Wayland multi-seat behaviour: no source found.
- Not verified that Xwayland attaches the second seat's slave to the Virtual Core Pointer (inferred from `dev->type = SLAVE` plus default attachment).

## 5. Comparison with the current EVIOCGRAB plus software cursor approach

### Takeaway
MPX buys a native cursor and native hover/click delivery to the Qt window. It costs isolation (raw XI2 motion leaks to native SDL games), and it adds WM bugs (Mutter ignores the second master), focus-pairing hazards, a master pair that outlives a crash, and a dead end on GNOME 50/Ubuntu 26.04 where X11 sessions are gone. Wayland multi-seat has the right semantics but only on wlroots compositors and Weston. EVIOCGRAB remains the only approach that isolates the mouse from every reader on every desktop Nimbus targets.

### Cited Findings
- XI 2.1 raw events reach clients "at all times, even when the device is grabbed by another client", so X-level isolation of a mouse from SDL-style readers is not achievable with MPX. [XI2proto.txt](https://xorg.freedesktop.org/archive/current/doc/inputproto/XI2proto.txt)
- SDL2/SDL3 raw-motion handlers take any master's motion; Wine takes only its ClientPointer's. [SDL3](https://github.com/libsdl-org/SDL/blob/main/src/video/x11/SDL_x11xinput2.c); [SDL2](https://github.com/libsdl-org/SDL/blob/SDL2/src/video/x11/SDL_x11xinput2.c); [Wine](https://github.com/wine-mirror/wine/blob/master/dlls/winex11.drv/mouse.c)
- Mutter X11 grabs only master id 2. [mutter #367](https://gitlab.gnome.org/GNOME/mutter/-/work_items/367). GNOME 50 has no X11 session. [Aliteq](https://aliteq.com/gnome-50-removes-x11-ubuntu-26-04-wayland)
- Leaked MPX pairs break keyboard focus persistently. [cua #3337](https://github.com/trycua/cua/issues/3337)

### Inferences
Summary table (inferred from the findings above; nothing here was tested on hardware):

| Criterion | EVIOCGRAB + software cursor (current) | X11 MPX second master | Wayland second seat (Sway/River only) |
|---|---|---|---|
| Native SDL2/SDL3 game on X11 sees the user's mouse | No | **Yes (raw XI2 leak)** | n/a (no raw broadcast), if Nimbus is a native Wayland client |
| Proton/Wine game sees it | No | Probably no (ClientPointer filter) | No, if Nimbus is native Wayland; yes if Nimbus is under XWayland |
| Direct evdev readers see it | No | Yes | Yes (compositor opens evdev; EVIOCGRAB is the only evdev-level block) |
| Native cursor and hover | No (drawn by Nimbus) | Yes | Yes |
| Works on GNOME/KDE (X11) | Yes | Mutter ignores master 2+; buggy | n/a |
| Works on GNOME 50 / Ubuntu 26.04 | Yes (evdev is compositor-independent; needs /dev/input access) | No (no X11 session) | No (Mutter has one seat) |
| Failure on crash | Grab released when the fd closes | Master pair and reattachment persist; focus stuck | Seat config persists (compositor config) |
| Needs privileged setup | Read access to the mouse evdev node plus uinput | None beyond X access | Compositor config |

- Recommendation implied by the evidence: keep EVIOCGRAB as the Linux isolation mechanism. MPX is not worth building: it fails the core requirement for native SDL games and targets a session type that the main desktop has deleted. A Wayland seat-B mode could be an optional extra for Sway/River users later, with Nimbus forced onto the Qt Wayland platform, but it cannot replace EVIOCGRAB.
- One idea worth taking from MPX without adopting it: the pain the software cursor solves is only visual/hover. Improving the software cursor (or on Wayland, a layer-shell overlay cursor) is cheaper than any multi-pointer route.

### Gaps
- No hands-on test was run. The user asked not to run `xinput` reattach commands on this machine. A safe test would use a uinput virtual mouse as the slave for a throwaway master (`xinput create-master`, reattach only the virtual device), with an SDL2 relative-mode test app to observe the leak.
- KDE Plasma's X11 session status in 2026 was not verified.
