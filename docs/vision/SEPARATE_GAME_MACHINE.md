# Separate Game Machine

**Status:** Proposal, 2026-09-14. A receiver prototype was built and measured the same day (section 10): research tooling in [`netpad/`](../../netpad/README.md), with no bridge, QML or configuration change; its fast checks and 21 end-to-end checks against the VM guest pass, and Left 4 Dead 2 measured the same through it as through the direct pad (10.3). Nothing else is built. It extends [Host Mode](HOST_MODE_ISOLATION.md) Option A (two Windows PCs over Moonlight) in three directions: a SteamOS game machine, a layout where the game keeps its own screen, and a Nimbus-owned way to carry the pad over the network. **Decided 2026-09-14: software only.** No dongle or other hardware is part of this track (section 3.3). Reviewed the same day; the review's nine points are folded into sections 2.1, 3.1, 3.2, 4.1, 6, 7 and 8.
**Question:** If the game runs on its own computer (a SteamOS box, a Steam Deck, another PC) and Nimbus runs on the computer the player already uses, what is the best way to connect them, and what does Nimbus need for it?
**Answer in one line:** Start with streaming on a computer that already exists, which needs no Nimbus code; build a network receiver only if a real play session gives it a clear purpose, because it removes Nimbus's hardest single-machine problems but brings a protocol, a failsafe and a pairing scheme of its own.

## 1. Why this is worth developing

Every hard problem Nimbus has fought on one machine comes from the game and the Nimbus panel sharing a mouse, a screen and a keyboard focus:

- the Raw Input tier, answered on Windows by a kernel mouse filter ([Windows Mouse Filter Plan](WINDOWS_MOUSE_FILTER_PLAN.md)) and on Linux by `EVIOCGRAB` plus a software cursor;
- dual input detection, which the controller pulse (`mouse_hider.py`, `controller_pulse.py`) fights by keeping a stick moving under the deadzone;
- window stacking and focus: Always on Top, Game Focus Mode, borderless conversion, ClipCursor polling;
- on SteamOS, Game Mode itself. Gamescope composites a single external overlay (the `GAMESCOPE_EXTERNAL_OVERLAY` atom), SteamOS's performance overlay holds that slot even when hidden, and so no second application can draw over a game. Nimbus on the same SteamOS machine is a Desktop Mode application or nothing.

With the game on a different computer none of these exist. The game machine has no mouse attached, so there is nothing for the game to switch its prompts to and nothing to leak. The Nimbus computer runs no game, so the panel can fill its screen and keep focus. What is left is carrying a pad from one computer to the other, which is a narrower problem than any item above.

It is also the shape of the product Nimbus is an alternative to. The Xbox Adaptive Controller is a control surface that plugs into whatever runs the game. A Nimbus computer plus a pad link is the same thing with a software surface.

**Costs, stated first.** A second computer able to run the games, with its own Steam installation. Depending on the layout, either a video stream's latency or a second screen the player has to be able to see. And the parts of Nimbus that read game state on the same machine (Spectator+, the harness's console oracles) do not reach across (section 4.3).

## 2. Two layouts

### 2.1 Layout S: the game comes back as a stream

The game machine sits on the wired network, headless or on a TV. The Nimbus computer shows the game in a streaming client window (Moonlight, or Steam's own Remote Play client) beside the Nimbus panel, and the client forwards Nimbus's pad to the game machine.

- The player watches one screen, the one they already use.
- It works through a remote-desktop tool the same way a local game does, at the price of a second encode (the stream, then the remote-desktop tool's).
- **Latency is a round trip.** Input travels out; the rendered frame comes back through capture, encode, the network, decode, the client's frame scheduling and the display, and each stage contributes. Gate D measured that whole path for a Hyper-V guest on the same machine at 30 to 46 ms press-to-photon at the median against 12.8 ms natively ([Virtual Machine Feasibility](VIRTUAL_MACHINE_FEASIBILITY.md) section 10.8). A published LAN comparison of Steam Remote Play and Moonlight is often quoted at around 20 ms for both (TechSpot), but that figure reached this document through a search summary of an article that could not be opened, and whether it is added or total latency is unknown, so treat it as unverified. Whether a second machine beats the guest is a hypothesis, not an expectation: its own GPU removes the GPU sharing, not the encode, network and decode stages. Measure the median, the 95th percentile and the worst observed sample against a native baseline on the same display with comparable stream settings.
- Isolation is a configuration property, as Gate C put it. With Sunshine's keyboard and mouse input turned off and the pad on, Gate C counted zero guest mouse and keyboard events while the host swept the pointer over a focused Moonlight window (section 10.7 of the same document). Steam's client has not been measured and may not offer the same switch.
- Nimbus code: none. This is Option A with the game machine swapped.

### 2.2 Layout D: the game keeps its own screen

The game machine drives its own TV or monitor. The Nimbus computer is only the control surface, anything from a desktop to a small laptop or a touch tablet placed where the player can reach it.

- No video stream: display latency on the game machine is native.
- The player has to be able to see both screens, so this suits someone playing in the room, not someone operating the machine from elsewhere.
- The pad link is the network receiver of section 3.2, which needs building.
- The Nimbus computer only has to run the Qt app.

### 2.3 Side by side

| | Layout S, stream | Layout D, receiver |
|---|---|---|
| Screens the player watches | One | Two |
| Added latency | A video round trip (unmeasured here) | LAN plus a user-space hop, expected small when wired (unmeasured) |
| Usable from a remote-desktop session | Yes, with a double encode | No |
| Nimbus code | None | An output backend, bridge integration (4.1) and a receiver |
| New attack surface | The streaming host's pairing | A network service that injects input |
| Game machine | SteamOS, Bazzite, any Linux or Windows that hosts the client's protocol | SteamOS, Bazzite, any Linux with uinput; Windows with ViGEmBus |
| Hardware beyond the two computers | Wired network | Wired network |

## 3. Carrying the pad

### 3.1 A streaming client (Layout S)

**Moonlight to Sunshine.** Proven for this purpose on the dev machine: Gate C forwarded the Nimbus pad with 14 buttons and the sticks in order, driven by the real app. Moonlight forwards every host gamepad, so a vJoy device arrives as player 0 and the Nimbus pad as player 1 unless vJoy is disabled. The catch on a SteamOS game machine is the host side. Gamescope offers neither portal nor KWin screen capture, so Sunshine has to use KMS capture, which the Flatpak build cannot do and which needs `CAP_SYS_ADMIN` on the Sunshine binary. On SteamOS's read-only root that is an unsupported install. Bazzite documents its own Sunshine setup, which makes it the easier game machine for this client.

**Steam Remote Play.** Steam lists SteamOS among its supported host systems, alongside Windows, macOS and Linux, so a SteamOS game machine installs nothing. The client on the Nimbus computer is the Steam client or the Steam Link app. Three things are unknown:

1. **Does Remote Play host from Game Mode on the game machine?** Unconfirmed; Steam's host list does not say which SteamOS session. This is the SteamOS gate of phase 0b.
2. **Does the Windows client accept and forward a ViGEm Xbox 360 pad created by a Python process?** Nothing found documents a problem for this case. The nearest documented behavior is DS4Windows': Steam detects `DS4Windows.exe` by name and then ignores PlayStation controllers, which users fix by renaming the executable. That is a reason to test, not evidence of a block.
3. **Does it keep forwarding the pad while its window is not focused, and can its mouse and keyboard forwarding be turned off?** The first is what lets the player click the panel without the pad dropping. Gate C ran Moonlight with `--background-gamepad` for this; whether Moonlight needs it was not tested.

### 3.2 A network receiver (Layout D)

A small service on the game machine creates the pad with the uinput code Nimbus already ships (`UInputXboxInterface` in `src/uinput_interface.py`, verified end to end on Linux including a live Proton game) and applies state packets Nimbus sends over the LAN. Steam sees the same `Microsoft X-Box 360 pad` it sees from Nimbus on Linux today. On a Windows game machine the same receiver would drive `padbus_client.py` instead.

A sketch, not a decision. Every number below is an example; the failsafe values, the rate limit and the credential handling are "when to ask" items under CLAUDE.md.

**Packets.** UDP on the local network. Each datagram carries a protocol version, a session identifier, a sequence number, both sticks, both triggers, a button mask sized to the ViGEm backend's limits (4 axes, 2 triggers, 14 buttons) and a small press counter per button. Nimbus sends one on every state change and a keepalive at a fixed rate (for example 60 Hz).

What full state guarantees, and what it does not:

- A later packet always restores where the controls are, so a lost packet cannot leave a button held forever.
- It does not deliver every change. A short tap whose pressed state travelled only in lost packets would vanish, and a run of losses delays a release until a packet gets through or the watchdog fires.
- The press counters close the first gap: when a counter has advanced but the button arrives released, the receiver emits the press and release it missed, held for a defined minimum time. That minimum has to be chosen and tested against games that sample input slowly.
- Duplicates and older sequence numbers are dropped.

Whether this behaves like a local pad is a question for the loss, duplication and reordering tests in section 7, not a property of the design.

**Sender liveness.** Keepalives come from the loop that updates controller state (the bridge on the Qt main thread), never from an independent worker, and each packet carries that loop's tick count. A frozen UI then stops the packets, or at least stops the tick, and the receiver treats a stalled tick like silence. A background thread that kept sending the last held stick from a hung app is exactly the failure this rules out.

**Two stop paths, two bounds.**

- **Explicit Stop** (the Stop button, quitting, `emergency_stop`): Nimbus sends neutral state and a session end at once. Its bound is comparable to Gate C's Stop measurements, 54 to 62 ms through the real app, a figure that also includes Moonlight and the monitor's poll.
- **Connection loss:** no authenticated, fresh packet within the watchdog timeout, and the receiver sets neutral on its own, because Nimbus may be the thing that died. Its bound is the timeout plus the receiver's reaction, and the timeout has to sit above the keepalive interval with margin for jitter. A longer silence destroys the device.

The two paths are measured and accepted separately; this document picks no timeout value.

**Recovery.** A timed-out session is over. Delayed packets from it are discarded, never applied, so a burst arriving after the watchdog fired cannot resume held input. Resuming takes a new session whose first state is neutral controls; the receiver accepts non-neutral input only after that. Whether the player must also re-arm explicitly in the UI is an open contract question (section 9).

**Pairing and session ownership.**

- **Pairing.** A PIN short enough to type is too weak to use as a key directly, since anyone who captured the exchange could guess it offline. Use a reviewed mechanism rather than inventing one: for example a password-authenticated key exchange for the one-time pairing step, after which sessions authenticate with the long-term keys it established. The choice needs a security review before anything ships.
- **Sessions.** Each session starts with a handshake that yields a fresh session identifier and fresh key material. Packets captured from an earlier session, including one from before a receiver restart, then fail authentication, and sequence numbers are per session.
- **One controlling client.** A second client is refused while a session is live. Whether and how a takeover is allowed is open (section 9).
- **The watchdog.** Only authenticated packets from the live session with a newer sequence number refresh it.
- **Reachability.** "Local network only" is enforced, not assumed. The receiver binds to the chosen interface, refuses source addresses outside the private and link-local ranges (which also catches traffic arriving through a router's port forward), and its install step records what firewall, if any, the game machine runs.

**On SteamOS.** Valve's `60-steam-input.rules` tags `/dev/uinput` with `uaccess`, so the logged-in user should be able to create the pad without `sudo`. That is the permission hypothesis, and an interactive `tests/test_uinput.py` run is only its first gate. The receiver runs as a systemd user unit under the home directory, which OS updates do not replace, but that preserves the files, not the behavior. A switch between Game Mode and Desktop Mode may restart the user's session, and with it user services and the device ACL. Test the installed service:

- at boot straight into Game Mode;
- after switching to Desktop Mode and back;
- after suspend and resume;
- after an OS update.

It has no window, so Game Mode's overlay limit does not apply.

**Discovery.** Typing the game machine's address once is acceptable for a first version; mDNS can come later.

### 3.3 Not pursued: a USB dongle

Considered on 2026-09-14 and ruled out the same day: this track is software only. The idea was a Raspberry Pi Pico that enumerates as a wired Xbox 360 pad on the game machine (GP2040-CE's XInput mode already does that half and works on Steam Deck) and takes Nimbus's state over a USB to UART cable. It would have given about a millisecond of transport latency, no network service and reach to game machines with no receiver installed. It is recorded here so the question is not reopened by accident; reopening it is a decision for the project owner.

## 4. What changes in Nimbus

### 4.1 Where a remote backend fits

`ControllerOutput` (`src/controller_output.py`) builds backends from factories injected at construction, and the bridge picks them once, so a network pad supplied as the Xbox-side factory is the right integration point. It is not a drop-in. Checked against `src/bridge.py` on 2026-09-14, the bridge reaches past the factory in these places:

- **State text.** `getControllerStateText` reads the active backend's `current_values` and `button_states` directly. The remote backend has to keep both accurate, as a record of what was sent.
- **Game Mode.** The controller-mode paths use `self._vigem.gamepad` directly: the `mouse_hider` burst (`send_controller_burst(self._vigem.gamepad)`) and the pulse code around it take the local gamepad object, and `getGameModeDiagnostics` reports whether one exists. A remote backend has no local gamepad. Those operations should be gated off for a remote target (section 4.2 says they are not needed), not emulated, and the diagnostics should say why.
- **Availability.** `XBOX_OUTPUT_AVAILABLE` is `VIGEM_AVAILABLE or UINPUT_AVAILABLE`, so a Nimbus computer with neither driver would report no Xbox output even with a working remote target. Availability has to count the remote target.
- **The joystick backend.** `ControllerOutput.initialize` always constructs the local joystick (vJoy) backend. On a computer used only as a remote surface that has to degrade quietly, as it already does when vJoy is missing, rather than become a requirement.
- **Status.** `is_connected` and the status bar have to mean "a receiver session is live", not "a socket is open".

The backend implements the surface the bridge uses on `ViGEmInterface`: `set_left_stick`, `set_right_stick`, `set_left_trigger`, `set_right_trigger`, `set_button`, `update_axis`, `get_status`, `emergency_stop`, `shutdown`, and the `is_connected`, `current_values` and `button_states` attributes, within the ViGEm limits. Shaping stays in the bridge; the backend only transports. A backend that adds no method needs nothing mirrored into vJoy, ViGEm and uinput. One that wants more (link quality, rumble coming back from the game machine) is a mirrored interface change.

**Integration checklist for phase 1:** every item in the first list resolved, plus a fast-suite test that runs the bridge against a fake remote backend on a machine with no local virtual-controller driver, and checks the status text, availability, connection state and the Game Mode gating.

Two items need an answer before any code:

- Choosing a remote target adds keys to `controller_config.json`, a "when to ask" item. The paired key belongs in the OS keyring, the way the cloud credentials are kept, never in the JSON, which is credential handling and also a "when to ask" item.
- The Output Device menu gains a target picker and the status bar a link indicator, both through bridge slots and signals.

A vJoy-shaped remote output (the generic joystick's 8 axes) is out of scope for a first version.

### 4.2 What the player no longer needs

In Layout D, and in Layout S once the client forwards nothing but the pad: Game Mode's controller pulse, Mouse Isolation (the filter driver on Windows, the grab on Linux), borderless conversion and ClipCursor polling. In Layout D also Always on Top and Game Focus Mode. For a remote target these are gated off (4.1). A "separate machine" preset that turns them off is simpler than explaining each one.

### 4.3 What is lost or has to move

- Spectator+ and the aim work read game state on the same machine (the Source console, the Arma 3 clipboard pose channel). Across machines they would need an agent on the game machine or analysis of the stream. Neither exists.
- The harness's console oracles are local and Windows-side. In Layout S its frame oracle (`tests/frame_motion.py`) could watch the streaming client's window, as Gate D's probe watched Moonlight's. Untested.

## 5. The game machine

| Candidate | Notes |
|---|---|
| The project's Linux validation box | Already runs `tests/run_linux_validation.sh` on Ubuntu 22.04; GPU not recorded. It can host Steam Remote Play, or Sunshine, as it is, so phase 0a needs no new install, and Layout D with a receiver can be tried on it too. Installing SteamOS over it would cost the validation machine, so use a spare drive for 0b. |
| A PC with an AMD GPU on SteamOS 3.8 | Official since late June 2026, labelled beta on custom hardware. RX 6000 and 7000 series are the recommended cards. Secure Boot must be off; Re-image erases the whole target drive; Valve's installer does not dual boot. |
| A Steam Deck, docked | SteamOS native; the reference Game Mode. |
| Valve's Steam Machine | On sale since the end of June 2026 from 1,049 USD, allocated through a queue. |
| Bazzite on any PC | The same Game Mode, plus NVIDIA support, dual boot and a documented Sunshine setup. |
| Windows | Layout S works unchanged; this is Option A. The receiver would drive ViGEmBus through `padbus_client.py` on the game machine. |

The dev machine's RX 6600 XT is exactly the GPU SteamOS recommends, but THREADMASTER is the Windows baseline for the mouse filter, the harness and the VM track, so it stays the Nimbus computer.

## 6. Safety

- **Only the pad reaches the game machine, and that is verified, not assumed.** In Layout S that means the client's pointer and keyboard forwarding, checked the way Gate C checked them. In Layout D it means the game machine has no pointer attached.
- **Two stop paths with separate bounds** (3.2). An explicit Stop is measured like Gate C's Stop; connection loss is measured as the watchdog timeout plus the receiver's reaction. The tests:
  - press Stop with a stick held;
  - kill Nimbus, and suspend it;
  - freeze its UI thread while the network stays up;
  - pull the network cable, and drop the game machine's Wi-Fi if it has one;
  - restart the receiver mid-press;
  - replay captured packets after a timeout and after a receiver restart;
  - connect a second client during a live session.
- **Only authenticated, fresh packets from the live session count** (3.2).
- **The receiver caps the rate of state changes it accepts.** The cap and the timeouts are failsafe and rate-limit behavior, so their values are "when to ask" items.

## 7. Testing it

| Check | Layout S | Layout D |
|---|---|---|
| The pad arrives, buttons and sticks in order | Gate C's actuator side (`vm/gate_c_host.py --actuator nimbus`) against a Linux counterpart of `vm/guest/gate_c_monitor.py` reading evdev on the game machine (new; `tests/probe_linux_stack.py` already has the reading code) | The same monitor, driven through the new backend |
| Nothing but the pad reaches the game machine | The same monitor, counting pointer and keyboard devices | The same |
| Loss, duplication, reordering | The client's protocol; not ours to test | A fault-injecting relay between Nimbus and the receiver drops, duplicates, reorders and bursts packets; every tap must arrive with its minimum hold, and no release may arrive later than the loss bound |
| Stop | Gate C's Stop timing | Explicit Stop and connection loss measured separately, with the section 6 cases |
| Service lifecycle | Not applicable | Boot into Game Mode, a Game Mode to Desktop Mode round trip, suspend and resume, an OS update (3.2) |
| Latency | `vm/gate_d_latency.py` against the client window, with a Linux beacon on the game machine (new; `vm/guest/beacon.py` is Win32). Report the median, 95th percentile and worst sample against native on the same display. **The beacon flashes: never run it while anyone is looking at that screen, and never near someone photosensitive.** | Transport only: packet sent to evdev event on the game machine, with clocks aligned by a round trip. The display is native. |
| A real game | Left 4 Dead 2 or Half-Life 2 under Proton on the game machine, through the client | The same, on the game machine's own screen |

**Player acceptance, both layouts.** Input correctness does not show that the arrangement improves access; this check does. In a normal session the intended player does everything themselves, with nobody touching the game machine:

1. launch a game;
2. navigate its menus;
3. play;
4. stop;
5. recover a disconnected controller;
6. resume;
7. quit.

For Layout D, also record whether the player can comfortably move their attention between the control surface and the game screen through the whole session.

## 8. Phases

- **0a. Streaming on a computer that exists, no code.** The Linux box as it is (Steam Remote Play as host first, Moonlight and Sunshine second), Nimbus on THREADMASTER. The question is whether streaming improves an actual play session.
  - Pass: the player acceptance check through the client, clicking the panel does not drop the pad, nothing else reaches the game machine, and latency is recorded against native.
  - Answers unknowns 2 and 3 of section 3.1.
- **0b. The SteamOS gate.** Only if SteamOS or Bazzite is meant to be the game machine: does Remote Play host from Game Mode (unknown 1 of 3.1), and how does SteamOS's own install and session switching behave. It is separate because 0a on Ubuntu cannot answer it.
- **Decision point.** The receiver stays optional until 0a gives it a clear purpose: latency the player notices, or a problem with watching the stream that Layout D would fix.
- **1. Receiver prototype.** Days of work for a prototype that passes the Layout D rows of section 7 on the Linux box. It comes after the section 4.1 checklist and the "when to ask" answers (configuration keys, failsafe and rate-limit values, credential storage, the pairing mechanism). *Built 2026-09-14, ahead of 0a at the owner's direction, as standalone tooling with the owner's failsafe, rate-limit, credential and session answers; measured against the VM guest rather than the Linux box (section 10). The section 4.1 checklist and the configuration keys are untouched because the bridge is.*
- **2. Shippable receiver.** Not estimated yet. That waits until pairing, session recovery, the watchdog, installation (including the SteamOS lifecycle) and the player acceptance check are resolved and tested, and it includes a security review of the pairing.
- **3. Decide what ships.** Candidates: a documented Layout S setup (Host Mode section 6 item 3 already recommended documenting Option A) and the receiver as a Nimbus feature.

## 9. Open questions

1. Where does the player sit? Operating remotely points at Layout S; playing in the room points at Layout D.
2. Which computer is the game machine, and what GPU does the Linux box have?
3. The "when to ask" items: the configuration keys, the failsafe and rate-limit values, and where the paired key is stored.
4. After a connection-loss timeout, is a neutral first state enough to resume, or must the player also re-arm explicitly?
5. When a second client connects during a live session: always refuse, or allow a takeover, and on what signal?
6. Which pairing mechanism, chosen with a security review?
7. Should the receiver keep the pad plugged in (neutral) while no session is live? As built, the pad exists only while a session is live and for 2 s after, so a game that looks for pads only at launch (Source games do) never sees one that arrives later. Measured on 2026-09-14 (10.3): after a 2.6 s interruption the pad was destroyed and re-created, and Left 4 Dead 2 never read the new one. Keeping it plugged changes the agreed destroy-after-2 s rule, so it is the owner's call.

## 10. Prototype, built and measured 2026-09-14

On the owner's direction the receiver was built before phase 0a, as research tooling that nothing in the app imports. The owner chose the values the sketch in 3.2 left open:

- **Failsafe:** keepalive at 60 Hz, neutral after 150 ms, pad destroyed after 2 s idle, at most 500 packets a second per session.
- **Credentials:** a shared 256-bit key file copied by hand, a nonce handshake per session, and HMAC-SHA256 on every packet. PIN pairing waits for a reviewed key exchange.
- **Sessions:** a second sender is refused while one is live; after a timeout the sender resumes through a new session whose first state is neutral, with no extra action.

### 10.1 What was built

| File | Role |
|---|---|
| `netpad/protocol.py` | Packets and every rule, pure Python, with the clock, randomness and pad injected; the sender side (`NetpadSender`, ticked by the app loop, never a thread of its own) |
| `netpad/receiver.py` | The socket loop; the ViGEm pad through `src/padbus_client.py`, the uinput pad through `src/uinput_interface.py`, a log sink; a read-only `/status` page; `keygen` |
| `netpad/e2e_check.py` | A real sender on the host against the receiver in the guest, read back through the Gate C monitor's XInput poll |
| `netpad/deploy-guest.ps1` | Starts `NimbusGuest`, copies the receiver in, opens its ports, runs it as a logon task |
| `tests/test_netpad.py` | 72 fast checks on a fake clock and a fake network, plus a real UDP loopback pass; in the fast suite |

Choices beyond the 3.2 sketch:

- **Neutral opener.** A non-neutral first state closes the pending session and answers EXPIRED, and the sender sends its neutral opener twice. Without this, a lost opener left a sender that believed it was live talking to a receiver that silently dropped everything.
- **Replayed taps.** A lost press is played back held 50 ms with a 50 ms release after it. That is an implementation value, not part of the owner's contract.
- **Session binding.** A session is bound to the source address it opened from.
- **Replies to strangers.** Someone without the key gets at most EXPIRED (31 bytes) in answer to a 65-byte state packet naming an unknown session, capped at 20 replies a second, so the receiver cannot be used to amplify traffic.

**What the fast suite found:** after a failed pad write, the receiver remembered the last *successful* write, so a watchdog neutral that equalled it was skipped and the pad kept the failed held state. A failed write now forces the next one.

**Also found:** at 30% packet loss, a run of nine lost keepalives occasionally outlasts the 150 ms watchdog (once in the seeded ten-second run). The session resumed and no tap fell in the gap, but input inside such a gap is dropped by design.

### 10.2 Measured against the VM guest

The receiver ran in `NimbusGuest` over the Hyper-V Default Switch, with the ViGEm pad; the sender and every check ran on THREADMASTER. The reading is the guest's own XInput poll (Gate C's monitor, 4 ms), fetched over HTTP, so each latency includes that poll and a round trip. Two full runs:

| Check | Run 1 | Run 2 |
|---|---|---|
| E1 handshake, a pad connects in the guest | pass | pass |
| E2 14 buttons in order, once each | 28 events | 28 events |
| E3 both sticks and triggers, held and released in order | 8 events | 8 events |
| E4 explicit Stop to neutral in the guest | 15 ms | 16 ms |
| E5 pad unplugged after Stop | 2015 ms | 2016 ms |
| E6 connection loss to neutral, after the last packet, five trials; held again after resuming | 156 to 157 ms, all resumed | 156 to 157 ms, all resumed |
| E7 packets still flowing but the loop tick stalled | 156 ms | 156 ms |
| E8 a timed-out session's packets replayed | 13 of 13 rejected, no stick movement | the same |
| E9 second sender | refused, its presses went nowhere; admitted when the first ended, its held button arrived | the same |
| E10 40 taps through a relay with 20% loss, 10% duplication, 10% reordering | 40 presses once each, 1 replayed from a counter, no timeout | the same (the relay is seeded) |
| E11 guest mouse and keyboard input during the run | 0 | 0 |

**What the numbers say.**

- **Explicit Stop:** 15 ms to neutral as the guest reads it. Gate C's Stop through Moonlight and Sunshine was 54 to 62 ms (VIRTUAL_MACHINE_FEASIBILITY.md 10.7). The paths differ, but both include the same monitor poll and HTTP round trip.
- **Connection loss:** 156 ms, which is the 150 ms watchdog plus about 6 ms for the receiver's service loop, the guest's XInput poll and the fetch. The spread over ten samples was 1 ms.
- **Stalled tick:** the rule held with packets still flowing, at the same 156 ms.

**What this does not show.**

- **No game in the guest:** nothing ran there (Steam's login is interactive, as for Gate D); XInput is the reading. The game test is 10.3, on this machine.
- **No physical network:** only a virtual switch. Wi-Fi and a real LAN are unmeasured.
- **No Linux pad:** `--sink uinput` has not run; that needs the Linux box.
- **No SteamOS service lifecycle** (3.2).
- **No clean transport latency:** the packet-to-pad row of section 7 was not measured separately.
- **No player acceptance check.**
- **No bridge integration:** section 4.1's checklist stands.
- **The pad only exists during a session.** A Source game launched before a session starts would not see it (open question 7).

### 10.3 A real game: Left 4 Dead 2 through the network pad

The game cannot run in the guest, so it ran natively on THREADMASTER with its controller supplied by netpad: the game harness's `netpad` actuator ([Game Test Harness](GAME_TEST_HARNESS.md) section 4.4) runs a sender with its own 120 Hz loop thread and the receiver as a separate process owning the ViGEm pad, over UDP loopback. That is the whole netpad path in a real game; the network is the one part it leaves out, and 10.2 measured that over a virtual switch. The direct pad ran first, the same afternoon, as the baseline. Full numbers are in that document's results log for 2026-09-14.

- **The game measures no difference.** Pad 17/17, netpad 22/22, and every expect band met by both. The deadzone lies between 0.26 and 0.28 on both. Yaw at 0.40 read -13.74 and -13.70 deg/s, at 0.60 -33.72 and -34.36, at 0.80 -54.74 and -54.61. Pitch read -20.92 and -21.21, walk 200.0 and 199.7 units/s, and the LB echo was 31 ms on both.
- **The session held for the whole run:** one session, 3,039 packets, no timeout, no failed write, no rejected packet. The sender's loop stalled once for 63 ms while the harness grabbed frames, under the 150 ms watchdog.
- **The failsafe works in the game.** With the loop frozen mid-turn, the camera moved +0.00 degrees over 0.6 s and turned again once the loop resumed. An explicit Stop mid-turn did the same, and a new session turned it again within a second.
- **The finding: Left 4 Dead 2 does not read a re-created pad.** A 2.6 s freeze destroyed the pad as agreed, the resumed session re-created it, and the game never moved again (0.0 degrees in 1 s). On a Source game, any interruption past the 2 s destroy leaves the player without a controller until the game restarts. That turns open question 7 from a guess into a measured cost.

A fresh ViGEm pad also reads a small stale deflection on both sticks until its first non-zero report. That is a property of the bus, shared with the app's own pad, and is recorded in `PAD_BUS_FORK_PLAN.md` section 17, finding 4.

## Sources

- [TheSixthAxis: SteamOS 3.8 on standard PCs with AMD GPUs](https://www.thesixthaxis.com/2026/06/22/you-can-now-install-steamos-3-8-on-your-standard-gaming-pc-with-amd-gpu/)
- [GamingOnLinux: build your own SteamOS Steam Machine](https://www.gamingonlinux.com/2026/06/have-an-amd-gpu-you-can-build-your-own-steamos-steam-machine/)
- [PC Gamer: Valve greenlights SteamOS installs on normal PCs](https://www.pcgamer.com/hardware/steam-machines/valve-greenlights-steamos-installs-on-normal-pcs-with-amd-gpus-so-you-can-go-make-your-own-steam-machine-if-you-dont-wanna-fork-over-usd1-049/)
- [PCGamesN: Steam Machine launch](https://www.pcgamesn.com/steam-machine/launch)
- [Phoronix: SteamOS 3.8 preview](https://www.phoronix.com/news/SteamOS-3.8-Preview)
- [The Register: KDE Plasma sets a date to drop X11](https://www.theregister.com/2025/11/28/kde_6_8_wayland_only/)
- [bquenin/interpreter issue 222: Game Mode overlay slot](https://github.com/bquenin/interpreter/issues/222)
- [Steam Community: reinstall versus reimage on a dual-boot drive](https://steamcommunity.com/groups/steamuniverse/discussions/0/506208224013284159/)
- [Steam Link on Steam: supported host systems](https://store.steampowered.com/app/353380/Steam_Link)
- [Steamworks: Remote Play](https://partner.steamgames.com/doc/features/remoteplay)
- [TechSpot: Steam Remote Play versus Moonlight](https://www.techspot.com/article/2198-steam-remote-play-vs-moonlight/) (not opened; see section 2.1)
- [DS4Windows: preventing conflicts with Steam](https://kanuan.github.io/DS4WSite/guides/prevent-steam-conflict/)
- [Sunshine documentation: setup and KMS capture](https://docs.lizardbyte.dev/projects/sunshine/latest/md_docs_2getting__started.html)
- [Sunshine issue 2840: KMS capture instructions](https://github.com/LizardByte/Sunshine/issues/2840)
- [Bazzite: setting up Sunshine](https://docs.bazzite.gg/Advanced/sunshine/)
- [ValveSoftware/steam-devices: 60-steam-input.rules](https://github.com/ValveSoftware/steam-devices/blob/master/60-steam-input.rules)
- [GP2040-CE](https://github.com/OpenStickCommunity/GP2040-CE) (the dongle considered in section 3.3)
