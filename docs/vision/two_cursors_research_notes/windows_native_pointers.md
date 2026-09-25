# Windows-native mechanisms for a second pointer (Windows 10/11, as of September 2026)

Scope: whether Windows natively supports a second, independent pointer next to the system mouse cursor, and whether any such pointer could let a user operate Nimbus (a WS_EX_NOACTIVATE Qt Quick window) while the foreground game receives nothing from the physical mouse. Legend: "Verified" = stated in a primary Microsoft or Qt doc. "Secondhand" = third-party report. "Unverified" = reasoning or recollection with no source found in this pass; must be measured on Windows before relying on it.

Bottom line for the report writer: Windows has exactly one OS mouse cursor and no MPX-style multi-pointer API. The only native "second pointers" are synthetic touch and pen contacts from the pointer injection APIs, which can target a window without activating it and are drawn by the OS as contact or pen feedback, not as a second mouse cursor. None of them change the core problem: the user's source is still the physical mouse, and no documented user-mode mechanism lets one process take a mouse's raw input away from the foreground process. A two-cursor design could at most replace the "relay/click" half of the current driver approach, never the "swallow" half.

## 1. Windows MultiPoint Mouse SDK, Mouse Mischief, MultiPoint Server/Services

### Takeaway
MultiPoint Mouse was an in-app illusion: the SDK read each mouse separately and drew its own cursors inside a WPF window, taking over the app's mouse handling; it was not an OS-level second cursor and did not stop other processes from seeing the mice. All three products are discontinued or no longer developed.

### Cited Findings
- The MultiPoint Mouse SDK "intercepts low-level Windows messages and enables concurrent use of multiple mouse devices. This means that most of the standard WPF controls do not respond to mouse devices. The MultiPoint Mouse SDK includes a control that you should use to trap mouse events." (archived doc dated 2010-07-30, marked NOINDEX/archived) — [Building a MultiPoint Mouse-enabled UI (archived)](https://learn.microsoft.com/en-us/previous-versions/msdn10/ff853248(v=msdn.10))
- The SDK supported up to 25 mouse devices concurrently on one computer and let an app independently handle clicks from different users (search-result summary of archived MSDN pages; not re-read in full) — [MultiPoint SDK archive pages](https://learn.microsoft.com/en-us/previous-versions/msdn10/ff853267(v=msdn.10))
- MultiPoint Mouse technology was conceived in 2005 and launched December 2006; an SDK 1.5 existed; aimed at shared-computer classrooms in developing countries — [Wikipedia: Windows MultiPoint Mouse](https://en.wikipedia.org/wiki/Windows_MultiPoint_Mouse)
- Raw Input gives a per-device handle, which is how multiple-mouse apps distinguish mice (the standard technique MultiPoint-style apps rely on) — [Adam Sawicki: Handling Multiple Mice with Raw Input](https://asawicki.info/news_1533_handling_multiple_mice_with_raw_input); [Raw Input Overview](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input) ("An application can distinguish the source of the input even if it is from the same type of device. For example, two mouse devices.")
- Mouse Mischief was a PowerPoint 2007/2010 add-in; its download has been removed from the Microsoft Download Center and is now catalogued by Legacy Update's archive of deleted downloads — [Legacy Update: Microsoft Mouse Mischief](https://legacyupdate.net/download-center/download/8010/microsoft-mouse-mischief-1)
- Microsoft lists MultiPoint Services as no longer developed in Windows Server — [Features Removed or No Longer Developed in Windows Server](https://learn.microsoft.com/en-us/windows-server/get-started/removed-deprecated-features-windows-server); Server 2019/2022 installs report the MultiPointServerRole missing — [Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/595921/server-2019-and-2022-remote-desktop-services-insta)

### Inferences
- Because the SDK worked by capturing mouse input into its own WPF window and rendering cursors there, it only worked inside SDK-enabled apps. It could not give a second cursor over another process's window, and nothing in it hides mouse input from a separate foreground process. It is irrelevant as a replacement for the Nimbus filter driver, and it is .NET/WPF-era software that is not supported on Windows 10/11 (Unverified that it fails to install; it is certainly unmaintained).
- The same pattern (Raw Input per device, draw your own cursor) is available to Nimbus today with no SDK, but it has the same limitation: it adds a drawn cursor, it does not remove the mouse from the game.

### Gaps
- I did not find the archived pages stating whether MultiPoint hid the system cursor or used RIDEV_NOLEGACY/RIDEV_INPUTSINK; the full MultiPoint SDK 2.0 docs were not retrieved.
- No official end-of-life date for the MultiPoint Mouse SDK was found.

## 2. Pointer Input Stack: InjectTouchInput, CreateSyntheticPointerDevice + InjectSyntheticPointerInput

### Takeaway
Windows can inject synthetic touch (up to 256 contacts) and pen (1 contact) pointers that are separate from the mouse pointer, target whatever window is under them, and can refuse activation (WM_POINTERACTIVATE / WS_EX_NOACTIVATE). Qt 6 handles WM_POINTER natively. The unresolved and decisive risk is mouse promotion: if the pointer message falls through to DefWindowProc, Windows synthesizes mouse input, and there is a secondhand report that promoted touch arrives as WM_INPUT in raw-input apps, and there is no documentation on whether that raw data reaches the foreground game.

### Cited Findings
- InjectSyntheticPointerInput "Simulates pointer input (pen or touch)". Minimum client Windows 10, version 1809. For PT_TOUCH, count must be > 0 and <= MAX_TOUCH_COUNT; for PT_PEN it must be 1. ptPixelLocation is relative to the top left of the virtual screen — [InjectSyntheticPointerInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-injectsyntheticpointerinput)
- CreateSyntheticPointerDevice: pointerType "Must be either PT_TOUCH or PT_PEN" (no PT_MOUSE); third parameter is "The contact visualization mode" (POINTER_FEEDBACK_MODE) — [CreateSyntheticPointerDevice](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-createsyntheticpointerdevice)
- InjectTouchInput (Windows 8+): "The injected input is sent to the desktop of the session where the injection process is running." Touch has a hover state ("INRANGE | UPDATE: Touch hover starts or moves") and an interactive state; "Hover state represents touch input that is not in contact with the screen and cannot interact with applications." All contacts are cancelled on WM_DISPLAYCHANGE — [InjectTouchInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-injecttouchinput)
- InitializeTouchInjection: maxCount "must be greater than 0 and less than or equal to MAX_TOUCH_COUNT (256)". dwMode is the contact visualization mode: TOUCH_FEEDBACK_DEFAULT (feedback "may get suppressed by the end-user settings in the Pen and Touch control panel"), TOUCH_FEEDBACK_INDIRECT (feedback "overrides the end-user settings"), or TOUCH_FEEDBACK_NONE — [InitializeTouchInjection](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-initializetouchinjection)
- WM_POINTERDOWN "targets the window over which the pointer makes contact, and the pointer is implicitly captured to the window". "If the application does not process this message, DefWindowProc may generate one or more WM_GESTURE messages ... If a gesture is not recognized, DefWindowProc may generate mouse input." "If an application selectively consumes some pointer input and passes the rest to DefWindowProc, the resulting behavior is undefined." — [WM_POINTERDOWN](https://learn.microsoft.com/en-us/windows/win32/inputmsg/wm-pointerdown)
- WM_POINTERACTIVATE is "Sent to an inactive window when a primary pointer generates a WM_POINTERDOWN over the window"; the window returns PA_ACTIVATE or PA_NOACTIVATE. With multiple simultaneous pointers the activation opportunity applies only to the first, so "they may still receive input from pointers while they are inactive" — [WM_POINTERACTIVATE](https://learn.microsoft.com/en-us/windows/win32/inputmsg/wm-pointeractivate)
- Promotion internals (reverse-engineering notes, not Microsoft docs): when WM_POINTER* reaches DefWindowProc it is "promoted" to WM_MOUSE*; native pen/touch use win32u!NtUserPromotePointer; "there is no way for a fake event to go through this promotion process unless it is sent via the synthetic pointer API" (i.e. synthetic-pointer-API input IS promoted like real touch/pen); EnableMouseInPointer is permanent for the process lifetime — [jchv/WmPointerDemo Syscalls.md](https://github.com/jchv/WmPointerDemo/blob/master/Syscalls.md) (Secondhand)
- Mouse messages promoted from pen or touch are tagged: GetMessageExtraInfo masked with 0xFFFFFF00 equals 0xFF515700 (MI_WP_SIGNATURE); bit 0x80 distinguishes touch (1) from pen (0). "Both pen events and mouse messages are sent to an application, regardless of whether the pen or the mouse is used." — [System Events and Mouse Messages](https://learn.microsoft.com/en-us/windows/win32/tablet/system-events-and-mouse-messages)
- Secondhand (2012, Windows 7/8 era, from The Witness developers, via search snippet; the page itself could not be fetched): touch delivered to a raw-input app was converted to mouse motion, arriving as WM_INPUT (0x00FF) when raw input was registered and as WM_MOUSEMOVE otherwise, and DefWindowProc dispatched WM_TOUCH when it saw a "specially-marked" WM_INPUT or WM_MOUSEMOVE — [The Witness: WM_TOUCH is totally bananas](http://the-witness.net/news/2012/10/wm_touch-is-totally-bananas/)
- Qt: the Windows platform plugin uses Pointer Input Messages by default; `-platform windows:nowmpointer` "Switches from Pointer Input Messages handling to legacy mouse handling (since Qt 5.12)"; `nomousefromtouch` "Ignores mouse events synthesized from touch events by the operating system" — [QGuiApplication (Qt 6)](https://doc.qt.io/qt-6/qguiapplication.html)
- Qt 6 unifies input under QPointerEvent (mouse, touchscreen, tablet stylus); mouse events synthesized from touch/tablet carry their originating device — [Input Events in Qt 6](https://www.qt.io/blog/input-events-in-qt-6)
- Weylus (open-source remote tablet app) uses InjectSyntheticPointerInput with PT_PEN for pressure/tilt pen injection on Windows, a working real-world example of the API — [DeepWiki: Weylus Windows input](https://deepwiki.com/H-M-H/Weylus/5.4-windows-input) (Secondhand summary)

### Inferences
- A synthetic pen (hover plus contact) is the closest native thing to "a second cursor": it has its own position, is not the mouse, supports hover, and Qt Quick would receive it as QTabletEvent/pointer events without Windows needing to promote it to mouse (Qt handles WM_POINTER itself). A synthetic touch contact works too but has no useful hover.
- Clicking a WS_EX_NOACTIVATE window with it should not activate the window: Nimbus can additionally answer WM_POINTERACTIVATE with PA_NOACTIVATE via a native event filter. Unverified whether Qt's windows plugin already does this for WS_EX_NOACTIVATE windows.
- The decisive unknowns for the game: (a) whether an injected touch/pen contact moves the system mouse cursor (my recollection is that promoted primary touch does update the cursor position while hiding it, Unverified), and (b) whether promotion produces WM_INPUT mouse data delivered to the foreground raw-input game. The Witness report suggests promoted touch surfaces as WM_INPUT in the app receiving it; whether the foreground process also gets a raw mouse packet when the touch lands on another (non-activating) window is undocumented. If Qt fully consumes WM_POINTER (does not call DefWindowProc), promotion may never happen, which would be the good case.
- Even in the best case this only replaces the relay/click half. The synthetic pointer must be steered by something, and that something is the physical mouse, whose own packets still reach the game (section 5).

### Gaps
- No Microsoft doc found stating whether injected touch/pen moves the system cursor or generates WM_INPUT (mouse usage page) seen by other processes. Needs a Windows probe: inject pen hover/tap onto a Nimbus-like no-activate Qt window while a test process registered for raw mouse (default flags, foreground) logs WM_INPUT, GetCursorPos, and foreground HWND.
- POINTER_FEEDBACK_MODE value semantics for CreateSyntheticPointerDevice were not quoted in the fetched page (it just says "contact visualization mode").
- UIPI/integrity rules and whether touch injection needs the injector to be foreground were not found in the fetched docs (Unverified; games running elevated or with anti-cheat could matter).
- Whether anti-cheat or games treat injected pen/touch specially is unknown.

## 3. Multiple touch contacts and pen hover cursors: rendering and coexistence with the mouse cursor

### Takeaway
The OS draws touch/pen feedback (contact visuals, pen cursor) independently of the mouse cursor, and feedback for injected touch can be forced on or off. This is visual feedback, not a second interactive mouse cursor, and I found no primary source on whether the mouse cursor stays visible while a synthetic contact is active.

### Cited Findings
- Injected touch feedback is controllable per injector: DEFAULT (subject to Pen and Touch settings), INDIRECT (overrides user settings), NONE — [InitializeTouchInjection](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-initializetouchinjection)
- Up to 256 simultaneous injected touch contacts; pen is limited to 1 per synthetic device — [InitializeTouchInjection](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-initializetouchinjection); [InjectSyntheticPointerInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-injectsyntheticpointerinput)
- Pen hover has no mouse-message equivalent in the legacy tablet model ("Pen hover | Mouse hover ... No mouse message equivalent") — [System Events and Mouse Messages](https://learn.microsoft.com/en-us/windows/win32/tablet/system-events-and-mouse-messages)
- Pointer messages identify multiple simultaneous pointers by pointer id and a "primary" flag (IS_POINTER_PRIMARY_WPARAM) — [WM_POINTERDOWN](https://learn.microsoft.com/en-us/windows/win32/inputmsg/wm-pointerdown)

### Inferences
- Windows 10/11 commonly hide the mouse cursor while touch is in use and show a small pen cursor for a hovering pen (general platform behavior, Unverified here). If that applies to synthetic input, a synthetic-pen "second cursor" might hide the real cursor rather than coexist with it. Nimbus could instead draw its own cursor in a transparent topmost window (the MultiPoint approach), which needs no OS support.

### Gaps
- No primary source retrieved on mouse-cursor visibility during touch/pen activity, or on how the pen cursor is drawn for synthetic pen devices.

## 4. Two OS-level mouse cursors (X11 MPX equivalent) or Windows multi-seat

### Takeaway
No. Windows has a single system cursor per session and no documented API for a second OS mouse cursor; the multi-user single-machine products (MultiPoint Server/Services) are discontinued.

### Cited Findings
- The cursor APIs expose one global cursor: GetCursorInfo "retrieves information about the global cursor", GetCursorPos retrieves "the position of the mouse cursor" in screen coordinates — [Cursors (Win32)](https://learn.microsoft.com/en-us/windows/win32/menurc/cursors); [GetCursorPos](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getcursorpos)
- Community answers on "multi cursors in Windows" point to MultiPoint-style drawn cursors rather than an OS feature — [MSDN forum: How To Have Multi Cursors in windows?](https://social.msdn.microsoft.com/Forums/en-US/0e257f53-c3ff-43f8-b7f2-835e1db207d0/how-to-have-multi-cursors-in-windows?forum=csharpgeneral) (search-result title only; not read in full)
- MultiPoint Services is no longer developed — [Features Removed or No Longer Developed in Windows Server](https://learn.microsoft.com/en-us/windows-server/get-started/removed-deprecated-features-windows-server); [Microsoft Q&A: What is happening with the multipoint technology](https://learn.microsoft.com/en-us/answers/questions/169833/what-is-happening-with-the-multipoint-technology)
- Patents describe multiple-mouse schemes built on top of Windows (e.g. per-device IDs, RDP sessions), confirming it is not an OS feature — [US8788950B2](https://patents.google.com/patent/US8788950); [US7928959](https://patents.justia.com/patent/7928959)

### Inferences
- Even a separate session (RDP/multi-session) would not help: the game and Nimbus must share a desktop, and the physical mouse belongs to the console session.

### Gaps
- I found no Windows 11 (22H2 through 25H2) feature for multiple local cursors or multi-seat; absence is inferred from lack of any documentation, not from a Microsoft statement.

## 5. Critical: can one process receive a specific mouse's movement while the foreground game does not?

### Takeaway
No documented user-mode mechanism does this. Raw Input registration is per process and per top-level collection (usage page/usage), not an exclusive claim on a device; every flag that suppresses anything (RIDEV_NOLEGACY, RIDEV_CAPTUREMOUSE) affects only the registering application, and the background flags (RIDEV_INPUTSINK, RIDEV_EXINPUTSINK) add a listener or yield to the foreground. This matches the project's measurement that WH_MOUSE_LL cannot stop WM_INPUT reaching the game. Exclusive per-device capture on Windows requires a kernel filter driver (the current approach).

### Cited Findings
- Registration is by top level collection (usage page + usage). "By default, the operating system sends raw input from devices with the specified top level collection (TLC) to the registered application as long as it has the window focus." — [RAWINPUTDEVICE](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputdevice)
- RIDEV_INPUTSINK: "enables the caller to receive the input even when the caller is not in the foreground" (adds a receiver; does not remove the foreground one). RIDEV_EXINPUTSINK: "receive input in the background only if the foreground application does not process it ... if the foreground application is not registered for raw input, then the background application that is registered will receive the input." — [RAWINPUTDEVICE](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputdevice)
- RIDEV_NOLEGACY: "if the mouse TLC is set with RIDEV_NOLEGACY, WM_LBUTTONDOWN and related legacy mouse messages are not generated", and "the system does not generate any legacy message for that device for the application" (scope: the registering app). RIDEV_CAPTUREMOUSE: "the mouse button click does not activate the other window", only valid with RIDEV_NOLEGACY — [RAWINPUTDEVICE](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputdevice)
- RIDEV_EXCLUDE / RIDEV_PAGEONLY select which TLCs this app listens to ("an application can ask for input from all telephony devices except for answering machines"); they filter what the caller receives, not what others receive — [RAWINPUTDEVICE](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-rawinputdevice); [Raw Input Overview](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input)
- "Only one window per raw input device class may be registered to receive raw input within a process" — registration is per process — [RegisterRawInputDevices](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerrawinputdevices); a game-engine write-up likewise notes "Raw input affects the entire process, even though the API takes a window handle", and that RIDEV_NOLEGACY disables system interactions such as window moving for that process — [PH3 Blog: Things you really should know about Windows Input](https://ph3at.github.io/posts/Windows-Input/)
- An app "can receive data when it is in the foreground and when it is in the background (if registered with RIDEV_INPUTSINK)"; per-device distinction is by hDevice — [Raw Input Overview](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input)
- Raw mouse data carries no ballistics and is separate from the OS pointer (WM_MOUSEMOVE), which is why games use it for camera look — [Taking Advantage of High-Definition Mouse Movement](https://docs.microsoft.com/windows/win32/dxtecharts/taking-advantage-of-high-dpi-mouse-movement)

### Inferences
- Per-device selectivity exists only on the receiving side (an app can tell mice apart by hDevice and ignore some), never on the delivery side. The game registers the generic mouse TLC (0x01/0x02) and receives every mouse's packets while it is foreground, regardless of what Nimbus registers. So a "Nimbus owns mouse B, game owns mouse A" split is impossible natively, even with two physical mice, unless the game itself filters by hDevice.
- Consequently a second pointer does not remove the need for a blocking layer. Options that do block are all below user mode: the Nimbus KMDF mouse-class upper filter (current), or a third-party filter such as Interception (also a kernel driver, and also signed-driver/anti-cheat sensitive). Hiding the device from the game via HidHide applies to HID game controllers, not the system mouse stack (Unverified, not researched here).
- A realistic hybrid that a two-pointer design could offer: keep the filter to swallow the physical mouse, and replace the cursor-relay (moving the real system cursor into Nimbus) with a synthetic pen/touch pointer, if a probe shows synthetic pointer input neither moves the system cursor nor yields WM_INPUT in the game. That could reduce focus/cursor side effects, not the driver dependency.
- Without any blocking layer, the only native way to stop the game reading the mouse is taking foreground away from it, which the project already measured makes games stop reading the pad.

### Gaps
- Whether SendInput-injected mouse input (used by a user-mode relay) reaches foreground games as WM_INPUT with hDevice NULL is widely reported but I did not retrieve a primary source in this pass.
- No source found on whether a WH_MOUSE_LL hook's blocking skips raw input by design (the project's own measurement is the evidence).
- Behavior of GameInput (the newer Microsoft gaming input API) for mouse exclusivity was not researched; if any game uses it, delivery rules may differ.
