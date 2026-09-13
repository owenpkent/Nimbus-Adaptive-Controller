# Target-Aware Aim Assistance: Implementation Plan

**Status:** Proposal, 2026-09-13. Nothing in this document is built. It asks for one decision before any code (section 2) and then lays out four phases, each with its own test gate.
**Question:** Can Nimbus help a user land on what they are already aiming at, using knowledge of where the targets are, without becoming the thing that gets its users banned or that the accessibility community cannot stand behind?
**Relationship to the existing work:** [AIM_ASSISTANCE.md](AIM_ASSISTANCE.md) fixed the pipeline and drew a line at target awareness (its section 10). [GAME_TEST_HARNESS.md](GAME_TEST_HARNESS.md) section 4.7 built Spectator+ v0, open-loop primitives, and named the closed loop as the next step. This plan is that closed loop, applied to aiming.

---

## 1. Summary

An "aimbot" in the usual sense (read the screen, find enemies, move the crosshair onto them, fire) is the wrong product for Nimbus for three reasons that survive any amount of good intent: every anti-cheat vendor treats it as cheating whatever it is called, the detection they ship is behavioural and includes decoy targets that a screen-reading tool will lock onto, and the commercial tools that already market this exact thing as "accessibility" are the reason publishers now use the word "masquerade". Building one would put Nimbus's users, who are the people most likely to be banned by mistake, into the category that gets banned on purpose.

What is defensible, and useful, is narrower and still a real feature: **target-aware assistance in single-player games, that only ever steers input the user is already producing, or acts once on a command the user gives, with a strength the user sets, using the game's own data where it exists and a local detector where it does not.** That is the shape of every lock-on option a first-party studio has shipped (The Last of Us, God of War, Spider-Man 2, Skyrim Access), it matches the academic frame of a software copilot, and it slots into the architecture Nimbus already has: a single seam in the bridge where the shaped stick vector exists, a 60 Hz timer that idles under ViGEm, a calibration table in degrees per second per stick magnitude, a runner that executes timed axis plans, and a game harness with ground-truth oracles to measure it all.

The plan in one table:

| Phase | What ships | Target data from | Gate to pass |
|---|---|---|---|
| 0 | The policy: a revised non-goal, an allowlist, an anti-cheat refusal, the user-facing rules | none | Owner decision (section 2), `tests/test_assist_policy.py` |
| 1 | The closed loop, proved: snap-to-target as a Spectator+ primitive, measured in degrees | the game's script (Arma 3 oracle, test only) | Settles within tolerance with no overshoot, and produces nothing without a command |
| 2 | Sticky and gravity assist on a user's own stick, with a colour-keyed detector | screen capture, colour outlines | Real-game numbers in Left 4 Dead 2, the no-input-no-output property, the perceptibility check |
| 3 | A trained detector as an optional extra | screen capture, ONNX model | Detection rate and latency on the dev machine's AMD GPU and on CPU, licensing settled |
| 4 | Track-while-held, per-user strength, overlay | as above | Only after phases 1 to 3 have been used by real users |

Phase 1 needs no capture, no model and no new dependency. It is the cheapest way to find out whether a rate-controlled stick can be servoed onto a target through a 60 to 120 ms loop at all, and it is the phase to build first.

---

## 2. The line this crosses, and the decision it needs

`README.md` line 43 says, as a published non-goal:

> **What Nimbus will not do**: it reshapes input the user produces and never originates aim. No screen reading, no target detection, no synthetic stick motion the user did not command.

[AIM_ASSISTANCE.md](AIM_ASSISTANCE.md) section 10 is the reasoning behind it, and its conclusion is unchanged by the research below: **screen-capture target detection driving the stick is an aimbot regardless of intent** in any game where another person is on the other end. The research in section 4 strengthens that half of the line. Nothing here proposes touching it.

What this plan proposes to revise is the other half: that Nimbus never knows where a target is and never acts on it. The narrower position it recommends:

> Nimbus reshapes input the user produces. In single-player games it can also help the user land on what they are already aiming at: it will slow or steer a stick the user is moving, or turn to a target once when the user asks. It never plays for the user, never fires, never hides what it is doing, and refuses to run target-aware assistance in any game with anti-cheat or competitive play.

This is an owner decision, not an engineering one, for three reasons:

1. **It changes a public commitment.** The README, the aim document, and `HOST_MODE_ISOLATION.md` section 7.6 all argue that being clearly and openly *not* a cheat is Nimbus's asset with anti-cheat vendors and with organisations like AbleGamers. A target-aware feature, however gated, gives a reader a sentence to quote out of context. The mitigation is to be more explicit than the tools that muddy the water (section 5), not less.
2. **The gate can name a game, not a mode.** A title with both single-player and competitive modes (Left 4 Dead 2 has Versus; Arma 3 has servers) cannot be told apart from the outside. So the allowlist has to be by title, and any title with a competitive mode is out until someone decides otherwise per title. That excludes most of what people ask for.
3. **It commits to a support posture.** If a user is banned with Nimbus running, the project will be asked what the feature does. The answer has to be short, true and already written down. Section 5 is a draft of it.

If the decision is no, phases 1 and 2 still have value with the perception removed: the servo and the closed-loop primitive are what Spectator+ needs for "turn to face the door" from voice, and the per-game screen calibration is useful on its own. Section 10 says what survives.

---

## 3. What already exists

Everything below was checked in the tree on 2026-09-13. File and line references are to the current `main`.

### 3.1 The seam in the bridge

`ControllerBridge._drive_stick` (`src/bridge.py:1010-1032`) is the only place where a custom-layout stick exists as a shaped vector in controller orientation together with the widget dictionary that configured it. Its last line is `self._dispatch_stick(w, out_x, out_y)`, which routes to `set_left_stick` / `set_right_stick` under ViGEm or to the vJoy smoothing targets. An assist stage is one call inserted before that line. `setModifier` (`:1104`) already shows the re-drive pattern the assist needs: when the precision modifier changes, every held stick is re-shaped from `self._last_raw` with `advance_filter=False`, so a state change moves the output without a new pointer event.

`_smooth_timer` (`:346-354`) fires at `vjoy.update_rate`, default 60 Hz, on the Qt thread, and `_smoothing_tick` returns immediately under ViGEm (`:1281`). That is the tick a control loop can hang off, on the thread the driver interface expects.

### 3.2 Spectator+ v0

`src/spectator/calibration.py` holds, per game, yaw against hold time for each right-stick magnitude (`left4dead2.json`: 0.40, 0.60, 1.00; `arma3.json`: three magnitudes; `halflife2.json`: two, because full deflection folds). `hold_for` interpolates a hold for a wanted angle. `src/spectator/primitives.py` `PrimitiveRunner` executes timed axis steps on a precise `QTimer`, one at a time, always ending in a release, cancelled by a profile switch and by Ctrl+Alt+F12. It writes through `setAxis` (`bridge.py:804`), which does no shaping on purpose. `get_spectator()` (`:440`) is not a `@Slot`, so nothing in QML can reach it yet. Two gaps the harness doc already lists: no closed loop, and the calibrations are not bundled by `build_tools/Nimbus-Adaptive-Controller.spec` (it collects `src/**/*.py` only, line 49 to 54).

### 3.3 Capture and measurement

The only frame grabber in the tree is `grab(hwnd)` in `tests/probe_game_mouselook_windows.py:116`, a `QScreen.grabWindow` of the client rectangle, which needs the Qt thread. `tests/frame_motion.py` measures a camera move between two frames by phase correlation and a change grid; `phase_correlate` returns a shift in full-resolution pixels. The harness (`tests/game_harness.py`) knows how to launch six games, find their windows, size them to 1280x720 borderless, read a pose from the Source console or from Arma 3's script, and measure a stick hold in degrees.

### 3.4 Ground truth that could include targets

The Arma 3 oracle (`GAME_TEST_HARNESS.md` section 4.3) is a generated mission whose `init.sqf` publishes the player's pose through the clipboard thirty times a second and runs `NIMBUS_EXEC <sqf>` lines sent back. SQF has `worldToScreen`, `selectionPosition "head"` and `boundingBoxReal`, so the same script can publish a placed unit's head in screen coordinates every frame, and its box corners. That is a target oracle with no computer vision in it, and it is what phase 1 runs against. Left 4 Dead 2 draws survivors with a glow outline whose colour is a client cvar (`cl_glow_survivor_r/g/b`), which is what phase 2's colour detector keys on.

### 3.5 Cursor position

`src/mouse_isolation_win.py` delivers relative deltas only; the absolute cursor is `cursor_position()` (`:469`) or `QCursor.pos()`. The bridge knows the game window as `_iso_game_hwnd` (`:2249`) and its own as `_iso_nimbus_hwnd` (`:2462`) while Full Game Mode is on, which is what the capture crop needs to exclude Nimbus's own overlay.

### 3.6 Schema and packaging

A joystick widget carries `mapping`, `travel_px` and the shaping keys the bridge reads by id (`anti_deadzone`, `anti_deadzone_buffer`, `precision_gain`, `tremor_filter`, `sensitivity`, `dead_zone`, `extremity_dead_zone`, `invert_x`, `invert_y`); `qml/layouts/CustomLayout.qml:150-180` is the schema of record. A button's `modifier` is one of two literals in exactly two places in that file (`:506` and `:1627`), and the bridge accepts any string, so a new modifier is a two-line QML change plus a consumer. `requirements.txt` is numpy, PySide6, httpx, keyring, sentry-sdk and pyvjoy; there is no OpenCV, ONNX Runtime, mss or dxcam anywhere. CI runs Python 3.11 on `windows-latest`; the documented floor is 3.8.

### 3.7 The dev machine

AMD Radeon RX 6600 XT (no CUDA, no TensorRT; DirectML is the GPU path), Ryzen 9 3950X, 32 GB, 2560x1440 at 60 Hz, Python 3.11.4 in `venv/`. Whatever detector phase 3 picks has to be measured here, on DirectML and on CPU.

---

## 4. What the research found

Three surveys were run on 2026-09-13: the tools and numbers behind screen-reading aim assistance, the policy and product landscape, and the academic work. The full record, with every number, quote and unverified item, is [TARGET_AWARE_AIM_RESEARCH.md](TARGET_AWARE_AIM_RESEARCH.md); sources are at the end of both. The findings that shape the design:

### 4.1 First-party lock-on exists, and it is always single-player and always tunable

The Last of Us Part I ships **Lock-On Aim** (snap to the enemy's centre on aim, with a right-stick shift to head or legs), a **Lock-On Strength** slider from 1 to 10 described as "the pull strength", and an **Auto-Target** mode that moves to the next enemy including off-screen ones. God of War Ragnarök has Classic and Classic+ aim assist (nearest target near the reticle, or anywhere on screen) plus Auto-Target. Spider-Man 2 has Enhanced Auto-Aim. Ghost of Tsushima's assist holds the target centred while aiming. Skyrim Access, a community SKSE plugin for blind players, adds bow auto-aim and enemy lock-on. Forza's One Touch Driving is the racing analogue. Tobii's Aim at Gaze guideline recommends snapping to a target near the gaze point when aim mode starts. Every one of these is single-player or PvE, authored with ground-truth positions, and strength-adjustable. None of the multiplayer shooters ship more than slowdown and bounded rotation, and Black Ops 7 reduced hands-off rotation in its launch patch.

### 4.2 Anti-cheat is behavioural, explicit about "accessibility" tools, and includes decoys

Activision's Season 02 post (2026-02-03) says of XIM, Cronus and their kind: "They are cheating tools, even if they masquerade as accessibility devices," and describes detection that analyses "input timing, consistency, and response patterns". On 2026-05-22 RICOCHET banned WheeledGamer, a paralysed streamer on a QuadStick, and reversed it a day later under public pressure; there is no published exemption process. Respawn (2026-03-04): altering input behaviour to gain an advantage "is cheating. Full stop," permanent, no appeals, with a promise to differentiate accessibility tools that has no described mechanism. Bungie's policy is the clearest and the most relevant, because it names PvE: external aids that mitigate "challenges all players face, such as reduce recoil or increase aim assist" are banned, and the definition explicitly includes "automation via artificial intelligence". Ubisoft is the only publisher with a stated appeal channel for disabled players. Riot refused a disabled player's touchpad in 2022 and never reversed it.

On the technical side, no vendor claims to detect a capture API or an inference process. The published direction is the opposite: RICOCHET's "Hallucinations" draws decoy players that only flagged accounts can see, and a June 2026 paper (AimTrap) proposes honeypot textures that bait computer-vision aimbots with a 96.9 percent detection rate. A screen-reading assist that engages a decoy generates its own ban evidence, and no gate on Nimbus's side can prevent that in a game that draws them.

### 4.3 The commercial "accessibility" aimbots are the problem, not the precedent

Aimmy ("Universal Second Eye for Gamers with Impairments"), NobleAIM and aimassist.ai all market YOLO-on-screen-capture aim to players with tremor, arthritis and cerebral palsy. Aimmy's feature list includes a triggerbot, anti-recoil, jitter "humanization", a Stream Guard that hides its overlay from stream capture, five mouse-injection methods ranked by how detected they are, and a wiki that rates anti-cheats by risk and links to circumvention tools. That is the feature set the word "masquerade" refers to, and it is why any Nimbus feature in this space has to be visibly the opposite on every one of those points (section 5).

### 4.4 The research says which techniques help and which are noticed

The games-HCI taxonomy (Bateman 2011, Vicencio-Moreira 2014, Schneider 2023) separates area cursor, bullet magnetism, sticky targets (slow the cursor over a target), target gravity (steer toward it) and target lock (snap and hold, "rare in shooter games" because of its power). Across three FPS studies: area cursor and magnetism helped most and were noticed least; target gravity improved headshots; sticky targets "had no effect on performance but was perceived"; techniques strong enough to matter in a real map with occlusion and movement were "too perceptible". Assistance did not impede learning (Gutwin 2016). For motor-impaired desktop users, area cursors cut small-target selection time by 19 percent and errors by up to 82 percent (Findlater 2010). Ahmetovic et al. (CHI 2026, GamePals) is the frame that fits Nimbus exactly: partial automation as a software copilot for unmodified third-party games, evaluated with upper-limb-impaired players, who were open to software replacing a human copilot.

Translation: build gravity and a bounded snap, keep sticky as the gentlest option, and expect anything strong enough to help in a real scene to be noticeable. That is acceptable in a single-player game, which is the other reason the scope is single-player.

### 4.5 The technology, with numbers

- **Capture.** On Windows the choices are DXGI Desktop Duplication (`dxcam` 0.3.0, MIT, revived March 2026, Python 3.10+; 239 fps at 1080p on an RTX 3090, paced by the compositor so nothing above the refresh rate is real), Windows.Graphics.Capture (`windows-capture` 2.0.1, MIT, maintained, Python 3.9+, per-window so Nimbus's own overlay is excluded from the frame), and GDI `BitBlt` (`mss` 10.2, MIT, Python 3.9+, about 3 ms a 1080p frame, cost proportional to region so a 320 px crop is about a millisecond; only the older `mss` 9.x or PIL keep the 3.8 floor). All fail on the secure desktop and inside a disconnected session, and DDA must be recreated after any fullscreen transition.
- **Detection.** YOLO nano-class models at 640 px run in 1.5 to 2.5 ms on a T4 with TensorRT and 40 to 56 ms on a server CPU; at 320 px on a centre crop expect 3 to 6 ms on a mid-range GPU and 15 to 40 ms on CPU (extrapolated, to be measured). Ultralytics code and weights are AGPL-3.0; YOLOX and RT-DETR are Apache-2.0. ONNX Runtime with the DirectML execution provider is the only GPU path that covers the dev machine's AMD card. Colour keying on a game's outline colour costs well under a millisecond and is what Riot's own anti-cheat director says "you can almost do with just an algorithm" in Valorant.
- **Latency.** No project publishes a measured capture-to-input figure. Assembled from parts: frame age 8 to 17 ms at 60 Hz, readback 1 to 4 ms, inference 2 to 40 ms, the game's next XInput poll up to 16.7 ms, render and display 40 to 56 ms. The loop sees the effect of its own command 60 to 120 ms later.
- **Rate control.** Every public tool drives a mouse, which is a displacement. A stick is a velocity, so the controller is a servo on angular error with dead time, not a one-shot move. JoyShockMapper (degrees per second at full tilt, calibrated by a full turn) and XIM's velocity calibration are the two established ways to express that, and Nimbus's harness calibration is the same measurement. No public project drives a virtual gamepad stick from a detector; this is new ground, and the reason phase 1 exists.

---

## 5. Design rules

These are the properties every phase has to keep, and the sentence each one gives the project if asked. They are also the concrete negation of section 4.3.

| Rule | What it means in code | The sentence |
|---|---|---|
| **The user directs** | Sticky and gravity produce output only while the user's own stick is deflected; the assist can rotate that deflection toward a target, never enlarge it. Snap runs once per press for a bounded time, then releases. Track runs only while a button is held. | "It never moves unless the user is moving or asked." |
| **Bounded strength** | Every tier has a strength in [0, 1] the user sets; the assist's output magnitude never exceeds the user's own stick ceiling (the widget's extremity cap through `shape_magnitude`). | "It cannot turn faster than the user could." |
| **Single-player only, enforced** | A positive allowlist by title (process image plus window title), each entry declaring its modes and target source; a negative check for anti-cheat services (EasyAntiCheat, BattlEye's BEService, Vanguard's vgc/vgk) and for known competitive titles. Unknown game: refuse, with the reason on screen. | "It refuses to run where another player could be on the other end." |
| **No fire, no recoil, no lead** | No trigger automation, no recoil compensation, no ballistic prediction. Velocity lead in the servo exists only to cancel the loop's own dead time on a moving target, capped at that dead time. | "It aims, it does not shoot." |
| **Nothing hidden** | No jitter or "humanization", no injection-method choice, no stream guard. A visible status badge and a local log line whenever assist is active. The overlay, if any, is visible to any capture. | "It announces itself." |
| **Local only** | Frames never leave the machine; nothing about them is logged or sent, telemetry stays opt-in and records at most that a tier was enabled. | "It does not upload your screen." |
| **Fail safe** | Target lost for more than 150 ms: the assist contribution decays to zero over 100 ms. Capture or detector failure: assist off, badge says so. Ctrl+Alt+F12, a profile switch and any user stick motion above a threshold cancel a running snap or track. | "Losing sight of the target hands control back." |
| **Ground truth first** | For any allowlisted title, the order of preference is: the game's own accessibility option (document it, do nothing), a script or mod data path, a colour or template detector, a trained detector. | "It uses the game's own data when it can." |

The one rule the anti-cheat research makes non-negotiable: **never engage a target the user cannot see.** A decoy visible only to the anti-cheat is by construction on screen for a flagged account, so this cannot be guaranteed by code in a game that draws decoys. It is guaranteed by the allowlist: no title known to draw them, and no title with a server on the other end.

---

## 6. The assist ladder

Four tiers above the existing baseline, ordered by how much authority the software takes. Each is independently switchable per widget and per profile, and each has one strength.

| Tier | The user does | The assist does | Output when the user is idle | Noticed (section 4.4) | Ships in |
|---|---|---|---|---|---|
| A0 (exists) | drags the stick | nothing target-aware; the anti-deadzone floor lets the game's own assist engage | none | n/a | done |
| A1 Sticky | drags the stick across a target | scales the shaped magnitude down by up to `strength` while the reticle is within `fov_px` of a target, ramping with distance | none | least | phase 2 |
| A2 Gravity | drags the stick near a target | rotates the shaped vector toward the target by up to `strength` times the angle between them; magnitude unchanged | none | moderate | phase 2 |
| A3 Snap | presses a button | servos rx/ry onto the nearest target inside `fov_px` for at most `snap_ms` (default 600), then releases; cancelled by user motion | none (one bounded action per press) | most | phase 1 |
| A4 Track | holds a button | A3 sustained on the same target while held, re-acquiring only inside `fov_px` | none (nothing while released) | most | phase 4 |

The A1 and A2 rule "magnitude unchanged, direction steered" is what makes the no-input-no-output property a mathematical fact rather than a policy: with a zero user vector there is nothing to rotate. It also means A2 cannot finish an approach the user is not making, which is the intended trade.

A3 is the tier phase 1 proves, because it is the only one whose success can be measured in degrees against an oracle without a human in the loop, and because it is the one that needs the servo. A1 and A2 are then that servo's direction and gain applied to the user's vector rather than to the axis directly.

---

## 7. Architecture

New code lives under `src/spectator/`, since this is the closed loop the Spectator+ section already describes, and reuses its calibration and runner. Nothing new goes into `qml/` except the fields and actions in section 8.

```
game window ──► capture worker (thread) ──► target source ──► TargetFrame mailbox
                                                                     │
                 Qt thread:  60 Hz tick ──► AssistStage ◄────────────┘
                                             │  reads calibration, policy, widget settings
              _drive_stick ──► shape ──► AssistStage.blend (A1/A2) ──► _dispatch_stick ──► driver
              PrimitiveRunner.look_at (A3/A4) ──► servo ──► setAxis ──► driver
```

### 7.1 Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `src/spectator/policy.py` | `AssistPolicy.check(game_window) -> Verdict(allowed, reason, entry)`. Loads `src/spectator/games/*.json` (the allowlist), matches the foreground game by process image and title, scans running processes for anti-cheat services, refuses anything unmatched. Pure Python, testable with fake process lists. | nothing new |
| `src/spectator/games/*.json` | One entry per allowlisted title: `process`, `title`, `modes` (`single`, `coop`, `competitive`), `anticheat` (name or `none`), `target_source` (`script`, `color`, `onnx`), its parameters, `fov_deg` or `px_per_deg`, and the `calibration` name. Competitive in `modes` means refused until a per-title decision is recorded in the entry. | nothing new |
| `src/spectator/capture.py` | `FrameGrabber` protocol: `start(hwnd, crop)`, `latest() -> Optional[Frame]`, `stop()`. `WgcGrabber` (windows-capture, per window), `MssGrabber` (GDI, fallback, region-cropped, excludes the Nimbus rectangle), `QtGrabber` (the existing `grabWindow`, tests only). Runs on its own thread, latest frame wins, each frame stamped with a monotonic capture time. Imported under try/except into `ASSIST_CAPTURE_AVAILABLE`. | windows-capture or mss, optional |
| `src/spectator/targets.py` | `TargetSource` protocol: `poll() -> Optional[TargetFrame]`, where a `TargetFrame` is `(t_capture, crosshair_px, targets)` and each `Target` is `(cx, cy, w, h, conf, kind)` in game-window pixels. Implementations: `ScriptTargetSource` (reads the Arma 3 clipboard channel; test only), `ColorTargetSource` (HSV window on a centre crop, connected components, aim point at the top third of each blob, numpy only), `OnnxTargetSource` (phase 3). Target selection with hysteresis lives here: nearest to the crosshair inside `fov_px`, kept until it leaves `fov_px` plus a margin. | numpy; onnxruntime optional |
| `src/spectator/servo.py` | Pure Python control law (section 7.3). `Servo.command(error_px, target_vel_px, dt) -> (mx, my)` and `blend(user_vec, target_dir, tier, strength) -> vec`. Owns the calibration inversion and the pixel-to-degree conversion. | `calibration.py` |
| `src/spectator/assist.py` | `AssistStage(QObject)` on the Qt thread: holds the policy verdict, the grabber, the source, the servo, the per-widget settings; `blend(widget, ox, oy)` for A1/A2; a `QTimer` at the bridge's update rate that re-drives held sticks (the `setModifier` pattern) so a stationary pointer still follows a moving target; `snap(widget)` and `track(widget, held)` that hand the servo to the runner. Emits `assistStateChanged(str)` for the badge. | all of the above |
| `src/spectator/primitives.py` | Gains `look_at(source, servo, max_hold)`: a closed-loop primitive that on each tick reads the latest target, asks the servo, writes rx/ry through `setAxis`, and finishes on settle (error under `settle_px` for `settle_ms`) or timeout, always releasing. Cancelled by `stop()` like the others. | existing runner |
| `src/bridge.py` | One line in `_drive_stick` before `_dispatch_stick`; `@Slot` wrappers `setAssistEnabled`, `assistSnap(widget_id)`, `assistTrack(widget_id, held)`; the badge property; `_stop_spectator` also stops the assist; the modifier names `snap` and `track` routed to the assist rather than `_modifiers`. `get_spectator()` becomes a `@Slot`. | |

### 7.2 Threads and timing

Capture and detection run on one worker thread and never touch Qt (the same rule as the isolation reader thread; only queued signals cross). The worker loop is: grab the latest frame, run the source, publish a `TargetFrame` into a single-slot mailbox, sleep to the frame period. It does not try to run faster than the game's refresh, because a compositor-paced grabber cannot deliver anything new sooner.

The Qt-side tick is the existing 60 Hz timer. On each tick the `AssistStage` reads the mailbox once, ages it (`now - t_capture`), and drops it if older than 150 ms. A1 and A2 then re-drive any held stick whose widget has assist on; A3 and A4 advance the runner's `look_at` step.

`QtGrabber` cannot be the shipped grabber, because `grabWindow` needs the Qt thread and would contend with the UI and the tick; it stays for offscreen tests. `MssGrabber` is the fallback that needs no new binary dependency (pinned to `mss` 9.x if the 3.8 floor is kept); `WgcGrabber` is preferred on Windows 10 1903+ because per-window capture excludes Nimbus's own always-on-top window from the frame, which otherwise has to be masked out of every crop.

### 7.3 The control law

The plant is an integrator with dead time: the stick commands an angular rate, the game integrates it into an angle, and the assist sees the result 60 to 120 ms later. Three consequences drive the design.

1. **Pixels to degrees.** Near the screen centre, `px_per_deg = (W / 2) / tan(hfov / 2)`. The recipe may carry `fov_deg`; otherwise the harness measures `px_per_deg` directly by running a known turn from the calibration and reading the shift with `frame_motion.phase_correlate`. Both tools exist; the measurement is a new harness check (section 9).
2. **Degrees per second to magnitude.** The calibration tables give yaw against hold per magnitude. The steady rate is the one-second row (or the longest hold present). `Servo.magnitude_for_rate(deg_per_s)` inverts that monotone table by interpolation, clamps to the smallest calibrated magnitude below and to the user's ceiling above, and returns zero for a rate under the game's own deadzone rather than a floor value, so the assist never sends "just above the deadzone" into a game that has a cliff there (Halo Wars, Half-Life 2 in `AIM_ASSISTANCE.md` section 12.3).
3. **Gain against dead time.** With loop delay τ and an integrating plant, proportional gain `Kp` is stable below about `π / (2τ)` and comfortable below a third of that. At τ = 0.1 s that is `Kp` at most 5 per second: a 10 degree error commands a 50 degree per second turn and settles in about half a second. A first-order filter on the error (the tremor EMA's shape) and a velocity lead of `target_vel_px * τ` on a moving target complete it. No integral term: a steady-state offset of a few pixels is preferable to windup against a target that steps away.

Per tier:

- **A1** `out = user * (1 - strength * (1 - d / fov_px))` for `d < fov_px`, else `user`.
- **A2** `out = rotate(user, toward=target_dir, by=strength * angle(user, target_dir))`, magnitude preserved.
- **A3/A4** `(mx, my) = magnitude_for_rate(Kp * error_deg + lead)` per axis, written through `setAxis`, cancelled if the user's own raw vector moves more than `cancel_px` from where it was at the press.

`tests/test_assist_servo.py` checks all of it with synthetic targets and the checked-in calibrations, including that A1 and A2 return exactly the user's vector when there is no target and exactly zero when the user's vector is zero.

### 7.4 Target sources, in the order the rules prefer

| Source | Where it applies | Cost | What can go wrong |
|---|---|---|---|
| Native option | any title with lock-on in its accessibility menu | none; document in `GAME_COMPATIBILITY.md` | nothing; this is always the first answer |
| Script (`ScriptTargetSource`) | the Arma 3 harness mission, and any title with a script or mod API (UE4SS, BepInEx, SKSE) | none at run time | per-title work; test-only until a shipped mod path is built |
| Colour (`ColorTargetSource`) | titles that draw outlines or highlights in a configurable colour (Left 4 Dead 2 survivors via `cl_glow_survivor_*`) | under 1 ms | HUD elements in the same colour, outline colour fading with distance, no outline on the thing the user wants |
| Trained detector (`OnnxTargetSource`) | anything else on the allowlist | 3 to 40 ms | per-game models, false positives, licensing, a 50 MB runtime |

### 7.5 Data for a trained detector, without hand labelling

The Arma 3 oracle can publish, per frame, the screen-space bounding box of every unit from `boundingBoxReal` through `worldToScreen`, alongside the pose it already streams. The harness can save frames with those boxes as labels, which is a labelled dataset for that title at no labelling cost, in every lighting and pose the mission can be scripted into. That is the phase 3 path to a model whose provenance is entirely Nimbus's own, which matters for the licensing question in section 11.

---

## 8. Settings surface and schema

This is a schema change to `custom_layout.widgets[]` and to `controller_config.json` and needs sign-off per `CLAUDE.md` before implementation. Flat keys, matching the existing shaping keys.

```jsonc
// custom_layout.widgets[] additions for type "joystick"
"assist_mode": "off",        // "off" | "sticky" | "gravity"
"assist_strength": 0.5,      // 0..1
"assist_fov_px": 120         // engage radius around the crosshair, game-window pixels

// button widget: two new values for the existing "modifier" key
"modifier": "snap"           // one bounded look_at per press
"modifier": "track"          // look_at while held
```

```jsonc
// controller_config.json
"assist": {
  "enabled": false,          // master switch; nothing target-aware runs while false
  "capture": "auto",         // "auto" | "wgc" | "mss"
  "detector": "auto",        // "auto" | "color" | "onnx"
  "snap_ms": 600,
  "cancel_px": 6,
  "show_badge": true
}
```

QML changes: the joystick dialog gains an Assist row (mode, strength, radius) beside Anti-DZ and Travel; the button Action combo (`CustomLayout.qml:760`) gains "Snap to target" and "Track target", and the two literal lists at `:506` and `:1627` gain the matching values; the status ribbon gains the badge bound to `assistStateChanged`. The bridge reads the new keys in `_widget_params`'s neighbour, an `_assist_params(w)`, so `_reload_widget_shaping` keeps the single cache.

No profile-level game key: the policy decides from the foreground game window, the same way `autoDetectGame` matches titles today, so a profile is not tied to a title and cannot enable the assist in a game the allowlist does not name.

---

## 9. Test plan

Five layers, the same shape as `AIM_ASSISTANCE.md` section 12. The first three are unattended; the T-series joins the harness beside the G, N and P series.

| Layer | Proves | Tool | Needs |
|---|---|---|---|
| 1 Pure Python | the servo's properties; the policy's refusals; the colour source on synthetic frames | `tests/test_assist_servo.py`, `tests/test_assist_policy.py`, `tests/test_target_sources.py` | nothing; in the fast suite |
| 2 The app in-process | the wiring: assist changes output only under an allowed verdict; snap and track through the button widget; kill switch; a fake source feeding the stage | `tests/probe_assist_windows.py`, modelled on `probe_stick_shaping_windows.py` | ViGEmBus; safe over TeamViewer |
| 3 A real game with ground truth | the closed loop in degrees | `tests/probe_game_harness_windows.py --game arma3_nobe --actuator nimbus --assist` | Arma 3, the machine left alone |
| 4 A real game with a detector | detection rate and the felt result | `--game left4dead2 --actuator nimbus --assist` with the colour source; saved frames | Left 4 Dead 2 |
| 5 Hands on | whether it helps, and whether it is noticed | a person with the default profile, strength swept 0.2 to 0.8 | a person at the console |

### 9.1 Layer 1 checks

- Servo: `magnitude_for_rate` is monotone, returns 0 under the game's deadzone and never above the ceiling; `Kp` respects the dead-time bound; error filtering and lead are bounded; A1 and A2 return the user's vector unchanged with no target and zero with a zero user vector; A2 preserves magnitude to within floating error; a lost target decays to zero within 250 ms of ticks.
- Policy: an unknown title is refused with a reason; a title with `competitive` in its modes is refused; a running `EasyAntiCheat_EOS`, `BEService`, `vgc` or `vgk` refuses regardless of the allowlist; an allowlisted single-player title with none of those is allowed and names its target source.
- Colour source: on synthetic frames with a coloured blob, the aim point lands on the blob's top third within 2 px; two blobs pick the nearer to the crosshair; hysteresis keeps a target that steps 10 px outside the radius and drops one that steps 40 px; a same-colour bar across the top of the frame (a HUD) is rejected by the aspect filter.

### 9.2 Layer 3 checks (T-series, Arma 3, script targets)

The mission places one unit at a scripted bearing and range and publishes its head in screen pixels every frame.

| Check | Measures | Passes when |
|---|---|---|
| T0 gate | the policy on the running game | Arma 3 is allowed only through the harness's own test entry; a run without `--assist` sends nothing target-aware |
| T1 px/deg | the shift from a calibrated 10 degree turn by `phase_correlate` against `fov_deg` | within 5 percent |
| T2 snap, static | time to settle and the final error for targets at 5, 15, 30 degrees off centre | under `snap_ms`, final error under 1 degree, no overshoot past 2 degrees |
| T3 snap, moving | the same on a unit walking across at 1 and 2 m/s at 20 m | settles within 1.5 degrees with the velocity lead on, worse with it off |
| T4 silence | a target on screen, no button pressed, the stick idle | the bridge sends zero on rx/ry for the whole hold |
| T5 cancel | a snap in progress, then a 10 px drag on the user's stick | the runner stops within one tick and the user's vector is what the driver sees |
| T6 loss | the unit is deleted mid-snap | output decays to zero within 250 ms and the runner finishes with `completed=False` |
| T7 kill switch | Ctrl+Alt+F12 during a track | axes released within one tick |
| T8 ceiling | a target far off centre with a low user ceiling on the widget | the commanded magnitude never exceeds the ceiling |

Recipes gain an `assist` block (`fov_deg`, the target script, the tolerance bands) and `expect` bands for T2 and T3 once a run is good, the way the other series work.

### 9.3 Layer 4 checks (Left 4 Dead 2, colour source)

L4D2 has no target oracle, so this layer measures the detector against saved frames and the felt result against the console pose: with a survivor bot in view, A2 at strength 0.5 on the aim stick, a 40 px drag that passes within `fov_px` of the survivor should end nearer the survivor's bearing (from the console) than the same drag with assist off, by a margin the recipe bands. Detection rate on the saved frames is reported, not gated, in the first version.

### 9.4 Layer 5

Hands on, with the dev machine's profile and with a caregiver at the console: strength swept, the question asked each time is "did that help" and "did you notice it". Section 4.4 predicts that anything that helps in a real scene will be noticed. Record both.

---

## 10. Order of work

1. **Phase 0, policy and words (half a day).** Decide section 2. Write `policy.py` and the allowlist format with the Arma 3 harness entry marked test-only; `tests/test_assist_policy.py`; revise the README line and `AIM_ASSISTANCE.md` section 10 to the narrower position; add the "what this does not do" list to the README. Nothing target-aware runs yet.
2. **Phase 1, the closed loop (two to three days).** `servo.py` and its tests; `ScriptTargetSource` over the Arma 3 clipboard channel with the mission extended to publish a target; `look_at` in the runner; `get_spectator` as a slot; the T1 px/deg check and T2 to T8 in the harness; the calibration JSON added to the PyInstaller spec. Exit: T2 and T3 pass on `arma3_nobe`, and `arma3` with BattlEye running behaves identically, which is the same control the pad client used.
3. **Phase 2, assist on the user's stick (three to four days).** `capture.py` with `MssGrabber` first and `WgcGrabber` behind a flag; `ColorTargetSource`; `AssistStage` with A1 and A2 in `_drive_stick`; the schema and QML from section 8; the badge; `probe_assist_windows.py`; the L4D2 layer 4 run; the layer 5 session. Exit: the no-input-no-output property holds in the app (not just the unit test), the L4D2 margin bands, and at least one hands-on session recorded.
4. **Phase 3, a trained detector as an extra (open-ended; do not start before phase 2 has users).** Settle licensing (section 11); collect the Arma 3 auto-labelled set; train a permissively licensed nano model; `OnnxTargetSource` behind `onnxruntime-directml` with CPU fallback, installed from a `requirements-assist.txt` rather than the main file; measure on the RX 6600 XT and on CPU; decide whether it ships in the installer or as a download.
5. **Phase 4, track and tuning.** A4; a per-user strength suggestion from observed aiming (Schneider 2023's model is the reference); an overlay that draws the engaged target, visible to any capture on purpose; opt-in telemetry events limited to tier and strength.

Phases 1 and 2 can start on this branch. Phase 0's document changes should land first and separately, because they are the ones that need the decision.

---

## 11. Risks, and what ends the work

| Risk | Likelihood | What is done about it | What would stop the work |
|---|---|---|---|
| A user is banned in a game the allowlist should not have named | low if the list stays single-player; certain if it drifts | the list is positive, small, and every entry records who decided it and why; competitive modes refuse by default | a ban attributed to Nimbus's assist |
| The feature is quoted as "Nimbus ships an aimbot" | medium | the README states what it does not do before what it does; the rules in section 5 are visible in the app | a partner (AbleGamers or similar) saying it makes Nimbus impossible to endorse |
| The loop cannot be servoed through 100 ms of dead time without oscillating | low; the bound in 7.3 is standard | phase 1 measures it before anything perceptual exists | T2 failing at any stable gain |
| Perception is too slow on CPU-only machines | medium for a trained model; nil for the colour source | phase 2 ships without a model; phase 3 measures on CPU and can stay a GPU extra | none; it narrows who gets phase 3 |
| Nimbus's own window is in the frame | certain with DDA or GDI | `WgcGrabber` per-window capture; the GDI path masks the Nimbus rectangle | none |
| Model licensing (Ultralytics is AGPL) | certain if ignored | permissive architectures (YOLOX, RT-DETR), Nimbus's own data from 7.5 | none; it decides the trainer |
| The Python floor | certain for `windows-capture` (3.9), `dxcam` (3.10) and current `mss` (3.9) | `mss` 9.x or PIL keep 3.8; raising the floor to 3.10 is a separate decision, CI already runs 3.11 | none |
| The ViGEm resemblance to a XIM (`HOST_MODE_ISOLATION.md` 7.6) | already present | unchanged by single-player scope; the disclosure and engagement path there still applies | nothing new |
| VAC on Source titles | low | Valve signatures known cheat binaries; Nimbus is open source and named, and Left 4 Dead 2 is a test title, not a shipped allowlist entry | a VAC action on any user |

---

## 12. Open questions to settle before phase 1

1. **Section 2.** Yes, no, or yes for phases 1 and 2 only. This gates the document changes, not the servo.
2. **Allowlist strictness.** Refuse unknown titles outright (recommended), or warn and allow with the assist limited to A1. Refusing is the position that can be defended in a sentence.
3. **Shipped titles.** Which single-player games go on the first list, and whether Left 4 Dead 2 (Versus mode, VAC) is a test title only. Recommended: test only. Candidates for the first shipped entries are titles the harness can already run that have no competitive mode: Half-Life 2 and PowerWash Simulator, the second being a non-violent aim task (a nozzle onto dirt) that is a good demonstration for exactly the audience this is for.
4. **Naming.** "Target assist" in the UI, under the Spectator+ heading. Not "aim bot", not "auto aim".
5. **The Python floor**, as above.
6. **Whether A4 ships at all.** It is the tier that reads as playing for the user. Recommended: build it in phase 4 only if phase 2's hands-on sessions ask for it.

---

## 13. Review, 2026-09-13, and the relation to the VM track

Read the same evening, after the guest VM tooling of [VIRTUAL_MACHINE_FEASIBILITY.md](VIRTUAL_MACHINE_FEASIBILITY.md) section 10 had landed and Gate B part 1 had passed on the dev machine. Five comments on the plan, then what the two tracks have to do with each other.

### 13.1 Comments

1. **Phase 1 needs no section 2 decision.** It is harness work: a servo, a `look_at` primitive, and a target published by our own Arma 3 mission. Nothing ships that knows where a target is, and a snap on a button press is the "You direct, the AI executes" model Spectator+ already publishes (README lines 149 to 150). It also answers the one engineering unknown in the plan, whether a rate-controlled stick can be servoed through 100 ms of dead time, and yields "turn to face the door" for voice control whatever is decided later. It can start without touching the README.
2. **Phase 2 is where the line moves.** Screen capture and a colour detector in the shipped app are the first thing a reader can quote against the non-goal. That decision is better taken with T2 and T3 numbers in hand than now.
3. **Left 4 Dead 2 is the wrong phase 2 test title.** Its glow outlines are on survivors, so a colour source there aims at allies, and it has Versus and VAC. PowerWash Simulator, which section 12 already names for the shipped list, is the better first target: no anti-cheat, no opponents, already in the harness, and a nozzle onto dirt is the demonstration this audience would actually want. Whether dirt is detectable by colour is the open question, and it is a cheaper one than the survivor glow's per-state behaviour (research item 10).
4. **The allowlist and the anti-cheat scan are a posture, not an enforcement mechanism.** They are a JSON file and a process list. Their value is that the project can say in one sentence where the feature runs; the README wording should not present them as a guarantee.
5. **Costs to carry into phase 0.** The schema change in section 8 is a "when to ask" item under `CLAUDE.md`. The capture libraries raise the Python floor to 3.9 or 3.10. And since 2026-09-13 the dev machine runs above a hypervisor (Hyper-V was enabled for the VM track), so phase 1's loop timing is measured on that baseline, the same caveat the mouse filter suites now carry.

Recommendation: approve phase 1 only, as harness and Spectator+ work with no README change, and revisit section 2 when T2 and T3 have numbers.

### 13.2 Relation to the VM track

The two tracks are independent and share one boundary and one toolbox. One thing they do not do for each other needs saying plainly.

- **The guest does not make assist safer.** Running the game in a VM hides the assist process from the game's anti-cheat, and that is worthless: section 4.2 shows detection is behavioural (input timing and patterns, which reach the game identically from a guest) plus decoys drawn into the frame, which a host capturing the stream would engage just the same. The anti-cheats that would care refuse VMs anyway (VM track, Gate A), and section 5 refuses to run assist where any anti-cheat is present. Both tracks end at the same wall from different sides: neither reaches a kernel-anti-cheat title.
- **Same workload.** Gate A's usable set (Left 4 Dead 2, Half-Life 2, PowerWash Simulator: no anti-cheat, no opponents, in the harness) is the allowlist this plan would ship. A Gate D playability run and a phase 2 assist run would be measured on the same games with the same harness, calibrations and frame tools.
- **A guest makes assist worse, mechanically.** Assist lives in the host bridge at the shaping seam. With the game in a guest, perception sees only Moonlight's decoded stream: compression smears the outline colours a colour detector keys on, and the encode and decode chain adds tens of milliseconds to a loop budgeted at 60 to 120 ms of dead time, which forces a lower servo gain (section 7.3). Actuation gains the same transport on the pad path. The phase 1 oracle publishes through the clipboard, which the guest's isolation configuration deliberately closes (Enhanced Session Mode off); if the oracle were ever wanted from a guest, the Gate C monitor's HTTP pattern (`vm/guest/gate_c_monitor.py`) is the shape it would take.
- **Sequencing.** Neither waits on the other. The VM track passed Gates B and C the same evening (the guest renders on the partitioned GPU and the isolation held through Moonlight and Sunshine) and is parked before Gate D, still expected to end in "keep the research, do not integrate". This plan is parked on section 2. They compete only for the machine, since both need it unattended while a game runs.

---

## Related Documents

- [Target-Aware Aim Research](TARGET_AWARE_AIM_RESEARCH.md): the dossier behind section 4, with the verification ledger
- [Aim Assistance](AIM_ASSISTANCE.md): the pipeline this sits after, the shaping seam, and section 10, which this plan proposes to narrow
- [Game Test Harness](GAME_TEST_HARNESS.md): the oracles, the calibration, Spectator+ v0 (section 4.7), and the Arma 3 script channel this plan extends
- [Testing Strategy](TESTING_STRATEGY.md): the frame motion measurement the px/deg check reuses, and the expect-band convention
- [Host Mode and Input Isolation](HOST_MODE_ISOLATION.md): section 7.6 on the XIM resemblance and the disclosure path
- [Hardware Integration](HARDWARE_INTEGRATION.md): the "Enable Spectator+ Assist" layer this would be, over a physical device's input
- [Research Platform](RESEARCH_PLATFORM.md): Spectator+ effectiveness as a research question
- [Virtual Machine Feasibility](VIRTUAL_MACHINE_FEASIBILITY.md): the guest VM track; section 13.2 above says why a guest neither protects nor helps target-aware assist

## Sources

Policy and product:

- [Call of Duty Black Ops 7 RICOCHET Season 02](https://www.callofduty.com/blog/2026/02/call-of-duty-black-ops-7-ricochet-anti-cheat-season-02), reported by [Windows Central](https://www.windowscentral.com/gaming/call-of-duty/black-ops-7-season-2-anti-cheat-update) and [HipHopWired](https://hiphopwired.com/3029460/call-of-duty-ricochet-cronus-xim-cheaters-details/): the "masquerade as accessibility devices" line and behavioural detection
- [Dexerto on the WheeledGamer ban and reversal, May 2026](https://www.dexerto.com/twitch/paralyzed-cod-warzone-streamer-begs-activision-for-help-after-accessibility-controller-ban-3367476/)
- [EA, Apex Legends anti-cheat update, March 2026](https://www.ea.com/games/apex-legends/apex-legends/news/breach-anti-cheat-update)
- [Bungie TWAB, 2023-04-13](https://www.bungie.net/7/en/News/article/twab-04-13-2023-best-dressed): the external accessibility aids policy, including "automation via artificial intelligence"
- [Ubisoft, MouseTrap on consoles](https://www.ubisoft.com/en-us/game/rainbow-six/siege/news-updates/65UBprZeK2lHJw1qKI8ygM/mouse-and-keyboard-anticheat-feature-on-consoles): the disabled-player appeal channel
- [Activision Security and Enforcement Policy](https://support.activision.com/articles/call-of-duty-security-and-enforcement-policy), [Steam Subscriber Agreement](https://store.steampowered.com/subscriber_agreement/), [EA User Agreement](https://www.ea.com/legal/user-agreement), [Riot Terms of Service](https://www.riotgames.com/en/terms-of-service)
- [AimTrap, arXiv 2606.25734](https://arxiv.org/abs/2606.25734) and [Shaikh, Ni, Dacier, arXiv 2606.07650](https://arxiv.org/html/2606.07650v1): honeypot textures against visual aimbots; [RICOCHET Season 04 2023](https://www.callofduty.com/blog/2023/06/call-of-duty-ricochet-anti-cheat-season-04-update): Hallucinations
- [TechCrunch, Riot on fighting hackers, May 2025](https://techcrunch.com/2025/05/03/how-riot-games-is-fighting-the-war-against-video-game-hackers/): colour outlines and "you can almost do it with just an algorithm"
- [The Last of Us Part I accessibility features](https://blog.playstation.com/2022/08/26/the-last-of-us-part-i-full-list-of-accessibility-features/), [God of War Ragnarök accessibility deep dive](https://caniplaythat.com/2022/11/03/god-of-war-ragnarok-accessibility-menu-deep-dive/), [Spider-Man 2 accessibility](https://support.insomniac.games/hc/en-us/articles/46730041467027-What-Accessibility-options-does-Marvel-s-Spider-Man-2-feature), [Skyrim Access](https://www.nexusmods.com/skyrimspecialedition/mods/181131), [Tobii Aim at Gaze](https://developer.tobii.com/pc-gaming/design-guidelines/explored-features/aim-at-gaze/)
- [Aimmy](https://github.com/Babyhamsta/Aimmy) and its [wiki](https://github.com/Babyhamsta/Aimmy/wiki/Common-Questions-Fixes), [NobleAIM accessibility page](https://nobleaim.co.uk/accessibility/): the tools section 4.3 describes
- [The Register on Copilot on Xbox winding down, May 2026](https://www.theregister.com/personal-tech/2026/05/06/its-game-over-for-copilot-on-xbox/5230456), [WinBuzzer on Gaming Copilot screenshots, Oct 2025](https://winbuzzer.com/2025/10/26/microsoft-defends-gaming-copilot-privacy-after-backlash-over-hidden-screenshot-data-capturing-xcxwbn/)

Research:

- [Bateman et al., CHI 2011](https://dl.acm.org/doi/10.1145/1978942.1979287), [Vicencio-Moreira et al., CHI 2014](https://dl.acm.org/doi/10.1145/2556288.2557308), [Gutwin et al., CHI PLAY 2016](https://dl.acm.org/doi/abs/10.1145/2967934.2968101), [Schneider and Graham, CHI 2023](https://equis.cs.queensu.ca/~equis/pubs/2023/schneider-chi-2023.pdf), [Refai, Bateman and Fleming 2020](https://www.frontiersin.org/articles/10.3389/fcomp.2020.00017/full)
- [Grossman and Balakrishnan, the bubble cursor, CHI 2005](https://www.dgp.toronto.edu/papers/tgrossman_CHI2005.pdf), [Findlater et al., enhanced area cursors, UIST 2010](https://faculty.washington.edu/wobbrock/pubs/uist-10.pdf)
- [Ahmetovic et al., shared control interviews, arXiv 2509.02132](https://arxiv.org/abs/2509.02132) and [GamePals, arXiv 2601.11218](https://arxiv.org/abs/2601.11218)
- [Isokoski et al., gaze aiming in FPS, UAIS 2009](https://homepages.tuni.fi/oleg.spakov/publications/Isokoski_UAIS_09.pdf)

Technology:

- [dxcam](https://github.com/ra1nty/DXcam), [windows-capture](https://github.com/NiiightmareXD/windows-capture), [python-mss](https://github.com/BoboTiG/python-mss), [Microsoft, Desktop Duplication API](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api), [Windows.Graphics.Capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture)
- [Ultralytics YOLO11](https://docs.ultralytics.com/models/yolo11/) and [YOLO26](https://docs.ultralytics.com/models/yolo26/) benchmark tables; [ONNX Runtime DirectML on AMD](https://github.com/ChharithOeun/onnxruntime-directml-setup)
- [sunone_aimbot config](https://github.com/SunOner/sunone_aimbot/blob/main/config.ini) and [RootKit AI-Aimbot](https://github.com/RootKit-Org/AI-Aimbot): the public control-law vocabulary (FOV radius, smoothing, offset, prediction), examined for architecture only
- [JoyShockMapper](https://github.com/JibbSmart/JoyShockMapper/blob/master/README.md) and [XIM velocity calibration](https://guide.xim.tech/Velocity-Calibration/): rate-control calibration by a full turn
- [NVIDIA Reflex latency chain](https://www.nvidia.com/en-us/geforce/news/reflex-low-latency-platform/)
- [Left 4 Dead 2 outline glow cvars](https://steamcommunity.com/sharedfiles/filedetails/?id=942284942); Arma 3 `worldToScreen` and `selectionPosition` usage in [community player-tag scripts](https://github.com/HesienBurger/ArmA-3-Life/blob/master/ArmA3Life.LakesideValley/core/functions/fn_playerTags.sqf)
