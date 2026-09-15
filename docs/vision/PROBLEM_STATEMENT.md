# Input Ownership: Problem Statement

**Status:** Draft for discussion, 2026-09-14, revised the same day after review. The targets in section 4 are proposals.
**Why this exists:** the SteamOS research, the VM track, the separate-machine proposal and the network pad were each an answer to the same underlying problem, stated a different way each time. This document states that problem once, from first principles, so every option is judged against one yardstick.
**Companion:** [Input Ownership: A First-Principles Solution Pass](INPUT_OWNERSHIP_SOLUTION.md) proposes a design against this statement.

## 1. The situation

A player controls a computer with **one pointing device**: an assistive input that pairs with only one computer at a time. In the reference setup it is paired with the player's most capable PC (an RTX 5090, two monitors), and the owner has decided it stays there. Nimbus runs on that PC and turns the pointer into a virtual gamepad through an on-screen control surface.

The first target game is **Forza Horizon 6**, released on PC on 2026-05-19. It is DirectX 12 and built around a controller. As far as public reports go, no Forza Horizon game has shipped kernel-level anti-cheat; detection is server-side, and Forza Horizon 6 added exploit detection for its leaderboards. Players report that it switches rapidly between controller and keyboard-and-mouse prompts when it sees mouse or keyboard input, and name Steam Input as one trigger. These are reports, not measurements. The edition to test (Steam or the Xbox app) is still to be recorded, since the two can behave differently. Sources are at the end.

## 2. The problem in one sentence

**On the computer that holds both the player's only pointer and the best GPU, the pointer must reach only the control surface and only Nimbus's intended commands may reach the game, with both on screen together, without costing the game its performance or responsiveness, and without ever leaving the player without their pointer.**

"Only intended commands" is deliberately narrower than "a pad only the game can see". A virtual XInput controller is visible to every process. What matters is that nothing else moves the game, and that nothing (Steam Input, a desktop mapper) turns the pad back into mouse or keyboard input.

## 3. First principles

### 3.1 Facts

The facts any solution has to respect. None of them is a preference.

1. **One pointer, one computer at a time.** Everything the player does, including the control surface, the desktop and recovering when something breaks, goes through that single device. A solution cannot assume a second input channel. In particular there is no keyboard: a hotkey, such as Game Mode's Ctrl+Alt+F12 emergency stop, is not a recovery path for this player.
2. **The control surface lives where the pointer lives, and is seen with the game.** Nimbus is operated by pointing at it while watching the game. Two monitors make "together" possible without the two fighting over one screen.
3. **Operating systems deliver input to programs, not to what the player is looking at.** A Windows game reads the mouse through one of two paths, and they fail differently ([Host Mode, section 8](HOST_MODE_ISOLATION.md#8-measured-on-windows-2026-09-05)):
   - **The cursor path** (`WM_MOUSEMOVE`, cursor position). A low-level mouse hook can withhold it. Measured: Carrier Command 2, and Left 4 Dead 2 with `m_rawinput 0`, stopped seeing the mouse under the hook.
   - **Raw Input** (`WM_INPUT`). A foreground game registered for it receives every pointing device's motion, wherever the cursor is. A hook dropping every event leaves it untouched. The only user-mode action that stops it is taking the foreground away, and a game registered with `RIDEV_INPUTSINK` keeps receiving even then. Measured on a fake Raw Input game and on Elden Ring.

   Games also watch the most recent input device to decide prompts and control schemes, and many pause or stop reading the pad when they lose focus: Elden Ring ignores XInput while unfocused. So to the game, pointing at Nimbus looks like the player picking up a mouse.
4. **Anything placed between a device and the game is judged by the game.** Anti-cheat and anti-tamper decide, title by title, whether a filter driver, a virtual pad, a hypervisor or injected input is acceptable, and more of that judgment is now server-side and behavioral. That verdict is outside the project's control.
5. **The pointer is also the recovery path.** A mechanism whose failure leaves the pointer dead strands the player with no way to fix it; it happened with the unsigned mouse filter on 2026-09-07 ([driver README](../../driver/README.md#it-has-happened)). Three consequences:
   - The faults a solution must survive are listed in section 4. Kernel crashes, failed hardware and loss of power are outside any software guarantee.
   - When recovering the pointer and keeping the game isolated conflict, **the pointer wins**. A recovery may let the game see mouse input again.
   - Recovery from a frozen control surface must be automatic. A deliberate stop must be reachable with the pointer while Nimbus is healthy.
6. **Delay is paid by the player's hands.** Play is a feedback loop through a person. Every millisecond added between a command and the picture is felt, and most in fast genres such as racing.
7. **A held input must not outlive the control surface.** If Nimbus freezes or quits, the game must see neutral controls within a bounded time. "Frozen" means the control surface stops handling input or stops drawing, not that the pointer is still: a held throttle under a motionless pointer is play, not a fault. The game must also still have a controller when Nimbus comes back: Left 4 Dead 2 never read a pad that had been destroyed and re-created ([Separate Game Machine, section 10.3](SEPARATE_GAME_MACHINE.md#103-a-real-game-left-4-dead-2-through-the-network-pad)).
8. **The player and their caregivers maintain whatever is built.** Elevated installs, driver signing, a second operating system and re-copying drivers after updates are recurring costs, not one-time steps.
9. **Nimbus serves many players.** Most have one computer, one GPU and probably one monitor. A solution that only works in the reference setup is a data point, not the product.

### 3.2 Decisions

These were chosen, not discovered. They hold until the owner revisits them.

- **A. The pointer stays paired with the RTX 5090 PC.** Re-pairing it with another computer is out of scope.
- **B. The game runs on that PC's GPU.** Moving the game to another machine trades what makes the game good for clean input. This is a judgment about Forza on this setup, not about every game.
- **C. Software only** (2026-09-14). No dongle or other hardware.

**Open:** whether Windows stays the operating system. Changing it on the main PC is costly (fact 8), but nothing above forbids it.

## 4. What "solved" means

Measured per target game on the reference setup. Every result names the game build and edition, the device, the backend, the display refresh rate and what the measurement's two endpoints are.

A **full session** is: launch, menus, play, leaving play for the desktop (the Steam overlay, a UAC prompt, another app) and returning, recovering from a fault, and quitting.

| Requirement | Measure | Proposed target |
|---|---|---|
| Pointer isolation | Reactions to the pointer while in play and operating Nimbus: steering or camera movement, input-mode switches, pauses. Deliberately leaving play is not counted; returning to play must restore isolation | None over a full session |
| Leaving and returning | Starting and stopping isolation, and reaching the desktop mid-session | With the pointer alone: no keyboard, no helper |
| Pad delivery | Command to the game's response, Nimbus's pad against a physical controller on the same PC, compared as distributions (median and 95th percentile) at a stated refresh rate | Within one display frame (16.7 ms at 60 Hz, 6.9 ms at 144 Hz) |
| Performance | Average frame rate and 1 percent lows with the solution in place against without it, same settings, uncapped, including any throttling while the game is unfocused | At least 95 percent on both |
| Visibility | Game and control surface both on screen | Two monitors: neither covers the other, always. One monitor: any overlap is stated per game and accepted by the player |
| Failsafe | Control surface frozen (fact 7) or closed, until an independent reader of the pad sees neutral; then whether the game reads the same pad when Nimbus returns | Neutral within 150 ms, and the game keeps its controller. The 150 ms comes from the network pad's timeout (`netpad/protocol.py`) and is still to be confirmed for a local pad |
| Pointer recovery | The same faults, until the desktop pointer moves again | Automatic, no keyboard, within a bound still to be decided (the filter's watchdog releases in about 2.1 s today) |
| Pointer safety | Each listed fault: Nimbus crashes or hangs; its capture process hangs; a driver fails to load or is refused after a Windows update; a Nimbus update fails. Not covered: kernel crashes, failed hardware, loss of power | The pointer still works after each, with no helper |
| Game acceptance | The anti-cheat or anti-tamper verdict, single player and online | Recorded per title |
| Upkeep | Steps needing elevation or a reboot, and what recurs after updates | Stated, and as few as possible |
| Independence | A full session, including recovering a dropped controller, with nobody else touching the computer | Passes |

## 5. Where the boundary can be drawn

Keeping "the pointer for Nimbus" apart from "the pad for the game" needs a boundary somewhere on the path from the device to the game. The first six rows keep Windows and move the boundary from the game toward the device. The last four change the operating system or the machine.

| Boundary | How it works | Cost against section 4 | Evidence so far |
|---|---|---|---|
| The game's own settings | The game ignores mouse and keyboard, or stops switching modes | Only where the game offers it | None for Forza Horizon 6 |
| Focus | Nimbus takes the foreground; the game runs unfocused on the other monitor. The opposite of Game Focus Mode, which keeps the game in the foreground on purpose | Works only if the game keeps reading the pad and rendering at full rate while unfocused, and did not register for background Raw Input | Untested on Forza. Failed on Elden Ring, which stops reading XInput when unfocused ([Host Mode 8.2](HOST_MODE_ISOLATION.md#82-real-games), 2026-09-05) |
| Input mode | Full Game Mode's pulse keeps the game in controller mode | Treats the symptom: the mouse still arrives, so steering, camera, menus and pauses have to be checked, not just prompts. Varies by game | Works on some titles ([Game Compatibility](../GAME_COMPATIBILITY.md#controller-mode-enforcement-full-game-mode)) |
| Low-level mouse hook | Full Game Mode's `WH_MOUSE_LL` hook suppresses mouse motion over the game window | Invisible to Raw Input (fact 3), so it helps only games that read the cursor | Hid the mouse from Carrier Command 2 and from Left 4 Dead 2 with `m_rawinput 0`; did not from Elden Ring or `m_rawinput 1` ([Host Mode 8.2 and 8.5](HOST_MODE_ISOLATION.md#85-measured-setcursorpos-is-invisible-to-raw-input-2026-09-05)) |
| Device filter in the kernel | The pointer's packets go to Nimbus and never reach the mouse class stack; a relayed cursor drives Nimbus | Driver signing; anti-cheat verdicts; capture is class-wide today (every mouse, not one device); a filter Windows refuses to load leaves no mouse at all, and no runtime watchdog runs to fix it (fact 5) | Isolation measured 2026-09-05 with a physical mouse against the probe's fake Raw Input window ([Host Mode 8.4](HOST_MODE_ISOLATION.md#84-measured-with-the-filter-2026-09-05-dev-build-under-test-signing)); the cursor relay it depends on was invisible to Left 4 Dead 2's Raw Input (8.5). A physical mouse under the filter in a real game is not yet run ([filter plan](WINDOWS_MOUSE_FILTER_PLAN.md#5-test-plan), section 5, item 3). It adds no stream, but its own latency was not measured |
| The device's own software | The assistive device's software feeds Nimbus coordinates and clicks and stops its own mouse output during play | Exists only if the device's software offers it; it must restore the desktop mouse by itself when Nimbus fails, or the lockout risk only moves | Unknown until the device is identified (section 6, question 1) |
| Linux on the same PC | Forza under Proton on the same PC and GPU; Nimbus grabs the pointer (`EVIOCGRAB`) and drives a uinput pad, both already in `src/` | Reinstalling or dual-booting the main PC; the device, its software and the owned edition must work on Linux, and the grab declines absolute pointers; Proton performance on the RTX 5090; anti-cheat under Proton; Linux upkeep | Grab plus pad passed against Elden Ring under EAC ([Linux Probe Plan](LINUX_PROBE_PLAN.md#results-so-far-2026-09-02)) and in Carrier Command 2 under Proton. Forza is Steam Deck Verified; reports of early Proton performance and GPU bugs. Untested here |
| A second OS on the same PC, Windows host | The game runs in a Hyper-V guest sharing the GPU; the pointer stays on the host; the picture returns through Moonlight | Microsoft supports only Direct3D 11 and OpenGL in a GPU-P guest and Forza is DirectX 12, so this likely fails before latency matters ([VM Feasibility 10.3](VIRTUAL_MACHINE_FEASIBILITY.md#103-what-the-research-changed-in-sections-2-5-and-6)); stream delay; anti-tamper in a guest unknown; Hyper-V on the main PC; upkeep | Gates B and C passed 2026-09-13 on the RX 6600 XT. Gate D, synthetic beacon, no game: median 30 to 46 ms (two to three frames at 60 Hz) and 95th percentile 47 to 80 ms, against 12.8 ms (one frame) natively ([VM Feasibility 10.8](VIRTUAL_MACHINE_FEASIBILITY.md#108-gate-d-the-axis-that-could-be-measured-latency)) |
| A second OS on the same PC, Linux host | KVM with the GPU passed through to a Windows guest, Looking Glass for the picture | Rebuilding the main PC; the host needs its own display GPU (an integrated GPU may do; a second card is hardware, which decision C rules out); VM verdicts | Literature only ([Host Mode, Option D](HOST_MODE_ISOLATION.md#option-d-full-vm-with-gpu-passthrough-dda--vfio)) |
| A separate computer | The game runs elsewhere, streamed in or on its own display | Gives up the best GPU (decision B) | Moonlight and the network pad, measured 2026-09-13 and 14 ([Separate Game Machine, section 10](SEPARATE_GAME_MACHINE.md#10-prototype-built-and-measured-2026-09-14)) |

Within each group, the further down the table, the more complete the separation and the higher the cost. Rows can combine: Full Game Mode already runs the pulse and the hook together. **The cheapest boundary that meets section 4 for the target game is the answer.**

## 6. What decides it, and how to find out

1. **How does the assistive pointer reach Windows?** A standard HID mouse, an absolute or digitizer device, a receiver, or events generated by companion software, plus any keyboard-like click action. Does its software offer a way to feed Nimbus and suspend its own mouse output? This decides whether a boundary can target that one device, whether the Linux grab accepts it, and whether the device-software row exists. It is an inspection on the RTX 5090 PC; nothing gets disabled.
2. **Does Forza read the mouse through Raw Input or the cursor?** `tests/probe_game_mouselook_windows.py` with the hook on answers it. If the hook hides the mouse, Full Game Mode may already be enough.
3. **Does Forza react to the pointer while Nimbus is used on the same PC?** Mode switches, steering or camera movement, pausing.
4. **Does it keep reading the pad, and rendering at full rate, while unfocused?** If yes, and it does not use background Raw Input, the focus boundary may be enough.
5. **If 3 and 4 are bad, does Full Game Mode's pulse hold it in controller mode?** Checked on steering, camera, menus and pauses, not only prompts.
6. **How much delay is too much in Forza, for this player?** Only a play session answers this, and it decides whether any streamed option is acceptable.
7. **If Windows is not required:** do the device, its software and the owned edition of Forza work on Linux on the RTX 5090 PC?

Questions 1 to 5 are cheap: an inspection, a probe that already exists, and runs of the game with and without Nimbus's modes. They may end the search in the first four rows of section 5. Running them honestly needs care:

- **There is no Forza recipe** in `tests/games/` yet, and frame differencing is a weak judge of an animated driving scene. Judge by the HUD or by control responses.
- **The harness foregrounds the game** around its steps (`front()`), which spoils question 4. That run must not use those steps, and must record the foreground window throughout.
- **The harness injects input with `SendInput`,** which Windows marks as injected and which enters above any kernel filter. A game may treat it differently from the physical device. An injected pass is a control; the deciding run uses the real assistive device on the RTX 5090 PC.
- **THREADMASTER can answer the behavioral questions** if Forza runs on its RX 6600 XT. Performance and throttling numbers come from the RTX 5090 PC.
- **Record per run:** Game Focus Mode, Game Mode, Isolate Mouse, Steam Input and the edition, so a pass can be traced to one cause.

## 7. Not part of the problem

- Aim or driving assistance; a separate track.
- Hardware; decided 2026-09-14, software only (decision C).
- Re-pairing the pointer with another computer (decision A).
- Building or maintaining a hypervisor.
- A pad no other process can see. The requirement is that only intended commands reach the game (section 2), which is a different and smaller thing.

## Sources

- [Forza Horizon 6 is Verified on Steam Deck (forza.net)](https://forza.net/news/forza-horizon-6-steam-deck)
- [Forza Horizon 6 is out, Valve update Proton Hotfix for Linux (GamingOnLinux)](https://www.gamingonlinux.com/2026/05/forza-horizon-6-is-out-valve-update-proton-hotfix-for-linux-initial-thoughts/)
- [Forza Horizon 6 works on Steam Deck and Linux after a Proton hotfix (TweakTown)](https://www.tweaktown.com/news/111699/forza-horizon-6-finally-works-on-the-steam-deck-and-linux-gaming-pcs-thanks-to-a-proton-hotfix-valve-silently-pushed-to-steam/index.html)
- [Forza Horizon 6 will launch with built-in anti-cheat for leaderboards (Operation Sports)](https://www.operationsports.com/forza-horizon-6-will-launch-with-built-in-anti-cheat-for-leaderboards/)
- [Steam discussion: kernel level anti-cheat](https://steamcommunity.com/app/2483190/discussions/0/832746094345649906/)
- [Steam discussion: game rapidly switching between controller and keyboard and mouse](https://steamcommunity.com/app/2483190/discussions/0/839502760396272133/)
- [How to fix Forza Horizon 6 controller not working on PC (Appuals)](https://appuals.com/forza-horizon-6-controller-not-working-pc/)
