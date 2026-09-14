# Game Test Harness: Automated Tests Against Real Games

> **Status:** designed, built and run 2026-09-07 on `feature/game-test-harness`. The harness is `tests/game_harness.py`, the runner is `tests/probe_game_harness_windows.py`, the recipes are in `tests/games/`, and the first Spectator+ feature it made possible is in `src/spectator/` (section 4.7). Against Left 4 Dead 2 the pad calibration passes 14/14 and the Nimbus end-to-end run 17/17, primitives included; Elden Ring, with no console, passes 11/11 on frame verdicts; Half-Life 2, the second console game, passes 13/13 on the pad and 13/17 through Nimbus, reads its sticks but not its buttons, and is where the anti-deadzone floor turned out to sit under a game's own threshold; PowerWash Simulator, added 2026-09-08, passes 11/11 on both actuators and is the game whose menus are driven by a virtual pointer rather than a highlight; Halo Wars: Definitive Edition, added 2026-09-09, is the first real-time strategy title and has the highest stick threshold of the five, between 0.40 and 0.60. On the afternoon of 2026-09-09 the frame oracle became a motion measurement, the recipes gained a window, a button reset and expected-value bands ([TESTING_STRATEGY.md](TESTING_STRATEGY.md)), and every game was rerun under it: Left 4 Dead 2 17/17 on the pad twice with its four bands holding, Half-Life 2 16/17 with only the documented calibration fold failing, Elden Ring 11/11 on both actuators at 1280x720, PowerWash Simulator 11/11 on both, Halo Wars 11/11 on the pad with a real reset check and 12/12 through Nimbus once its recipe said the anti-deadzone floor does not move that game, which the morning's run had reported as a pass it was not. The numbers and what the games taught are in section 8.
>
> **Relationship to Spectator+:** the loop this harness runs (put the game in a known state, send controller input, read what the game did) is the loop a Spectator+ agent runs. Section 4.7 says what carries over.

---

## 1. Summary

Nimbus is tested against real games by hand, and once by script: `tests/probe_game_deadzone_windows.py` (aim assistance, layer 3) launches nothing, waits for a game window, holds a virtual right stick at a few magnitudes, and decides "moved" or "still" from how many pixels of the game window changed. That found the game's deadzone and proved the anti-deadzone clears it. It cannot say how far the camera turned, whether the player moved forward, or whether a button reached the game, and it needs the game launched by hand in the right order.

This document is the plan and the record for a harness that makes those measurements unattended, in numbers, from the game itself:

- **Recipes** (`tests/games/<game>.json`): how to launch a game into a playable state, what window to look for, which oracle reads its state, and where to put the player for a repeatable start.
- **Oracles**: the Source engine's own console (`getpos`, `setpos`, `setang`, echoes, all through a console log on disk) for ground truth in degrees and units; Arma 3's own scripting (a generated mission that publishes the pose through the clipboard and takes commands back from it, since 2026-09-09); and the existing frame differencing for games with neither.
- **Actuators**: a ViGEm pad the harness owns (fast, exact, for calibrating the game), and the real Nimbus app in-process, driven by synthesized pointer events on its widgets (the thing being tested).
- **An environment** with `launch`, `wait_ready`, `reset`, `step`, `observe` and `close`: the environment shape Spectator+ needs, delivered first as a test fixture.
- **A runner** that produces a results table per game and per actuator, in the style of the driver probes.

The first target is Left 4 Dead 2: no anti-cheat, so it starts under test signing; a Source engine, so it has a console; already installed on the dev machine with a known launch line.

---

## 2. What automated game testing has to answer

Three questions, in increasing order of difficulty:

1. **Does the game read the pad at all?** (Player one, XInput present at launch, controller mode on.) Frame differencing answers this.
2. **What does a given stick input do?** Degrees per second of yaw, units per second of movement, which direction, above what magnitude. Frame differencing cannot answer this; it says only "changed".
3. **Does Nimbus's smallest, largest and typical input do the right thing in the game?** The end-to-end claim of the aim work, restated in game units: a one-pixel drag turns the camera by so many degrees per second.

Spectator+ adds a fourth: **can a scripted or learned action reach a goal?** "Turn to face 90 degrees left" needs the answer to question 2 to be calibrated and the answer to question 3 to be executed through the real bridge. So the oracle is the whole problem. A harness without ground truth can only test question 1.

### 2.1 Where ground truth can come from

| Oracle | Games | Cost | What it gives |
|---|---|---|---|
| Frame differencing (exists) | any | none | moved / still against a noise floor |
| Game console log (Source: `-condebug`, `getpos`, `setpos`, `setang`, `echo`) | Source engine titles (Left 4 Dead 2, Half-Life 2, Portal 2, Team Fortress 2, Counter-Strike: Source) | a cfg file and a key bind | position, view angles, button echoes, deterministic resets |
| Game scripting (Arma 3: SQF in a generated mission, the clipboard as the channel both ways) | Arma 3, and any game with a script language, a clipboard command and a way to auto-start a mission | a mission folder and a launch parameter | position, view angles, held actions, resets and arbitrary script, streamed (added 2026-09-09) |
| HUD reading (OpenCV template matching, OCR) | any with a readable HUD | opencv, easyocr; per-game templates | compass, ammo, health, crosshair state |
| Vision-language judge (a model asked a yes or no question about two frames) | any | an API key or a local model; seconds per answer | coarse semantic verdicts, no numbers |
| Memory reading, hooks | any without anti-cheat | Cheat Engine style tooling | exact state; fragile, and the wrong side of the line for an accessibility project |
| A purpose-built test game | none real | a small pygame or Godot target | exact state over a socket; says nothing about real games |

The console log is the only one that is both exact and cheap. It is limited to Source games, which is acceptable: Left 4 Dead 2 is already the reference title for everything that cannot run under anti-cheat, and the harness is built so a second oracle slots in per recipe. That second oracle exists since 2026-09-09: Arma 3 has no console the harness can type into but runs scripts, and a mission of the harness's own publishes the pose and takes commands over the clipboard (section 4.3, `arma3`), which is exact, cheap and streamed rather than sampled.

---

## 3. Options considered

| # | Option | Testing gets | Spectator+ gets | Decision |
|---|---|---|---|---|
| 1 | A recipe runner over the existing frame-differencing probe | a regression matrix over games and modes | "the game is reachable" | the runner shape is kept; frame differencing stays as the fallback oracle |
| 2 | Ground truth from the game console | numbers: degrees, units, echoes; deterministic resets | an honest `reset` and `observe` | **built** for Source games |
| 3 | A vision oracle (templates, OCR, a model as judge) | game-agnostic verdicts | the observation layer for non-console games | later, when a non-Source game matters |
| 4 | A Gymnasium-style environment around the real game | conformance tests: reset, step, observe, latency | the environment itself | **built** as `GameEnv`, oracle and actuator pluggable |
| 5 | Spectator+ v0 as scripted primitives with a real-game test each | the spec, as tests | the first user-facing feature | **built** on top of 2 and 4; section 4.7, measured in section 8 |
| 6 | A purpose-built deterministic test game | a hardware-free pipeline check | nothing | not now |

---

## 4. Design

### 4.1 Recipes

One JSON file per game in `tests/games/`. A recipe says how to launch the game into a playable state and how to read it:

```json
{
    "name": "left4dead2",
    "title": "Left 4 Dead 2",
    "process": "left4dead2.exe",
    "steam_exe": "C:\\Program Files (x86)\\Steam\\steam.exe",
    "steam_app_id": 550,
    "game_dir": "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Left 4 Dead 2",
    "launch_args": ["-novid", "-windowed", "-noborder", "-w", "1280", "-h", "720",
                    "-condebug", "+sv_cheats", "1", "+map", "c1m2_streets", "+exec", "nimbus_harness"],
    "window_timeout_s": 300,
    "ready_timeout_s": 240,
    "oracle": {
        "type": "source_console",
        "mod_dir": "left4dead2",
        "cfg_name": "nimbus_harness",
        "base_cfg": "360controller",
        "pose_command": "getpos_exact",
        "pose_key": "F7",
        "reset_key": "F8",
        "button_echo": {"L_SHOULDER": "NIMBUS_BUTTON_LB"}
    },
    "reset_pose": null,
    "turn_right_sign": -1,
    "notes": "..."
}
```

- `title` is the window-title substring the harness looks for; `process` is what it kills at the end.
- `launch_args` is the exact Steam launch line, kept literal so a person can paste it. For a `source_console` recipe the harness checks that `-condebug` and `+exec <cfg_name>` are present.
- `oracle.type` selects the oracle; `frame_diff` needs no other keys.
- `pad_buttons_reach_game` (optional, default true) says whether the game acts on the pad's buttons at all. Half-Life 2 does not: it reads the axes and ignores every button, with the binds confirmed present. Setting it false skips the button check with that reason rather than leaving a check that can never pass.
- `floor_moves_camera` (optional, default true) says whether the bridge's anti-deadzone floor is above the game's own stick threshold. Half-Life 2 (threshold 0.30 to 0.40) and Halo Wars (0.40 to 0.60) sit above the 0.289 floor, measured by their pad sweeps, so the one-pixel-drag check can never see the camera turn there. With it false, N2 checks that the bridge sent its floor and that the camera, as measured, stayed still; a game that started answering the floor would fail it, which is the change worth noticing.
- `reset_pose` is where `reset()` puts the player: `{"pos": [x, y, z], "ang": [pitch, yaw, roll]}`. When it is `null` the first pose read after the game is ready becomes the session's reset pose, and the runner prints it so it can be written into the recipe. A recipe with a fixed pose is a repeatable test; one without is a first run.
- `ready_sequence` (optional) is the list of waits and button presses that take a game from its title screen into a map, for games with no `+map`: `{"wait": 25}`, `{"press": [1], "hold": 0.2}`, `{"wait_until_control": {"rx": 1.0}, "interval": 5, "timeout": 150}`, which holds the stick every few seconds until the picture moves, and `{"press_until_control": [1], "action": {"rx": 1.0}, ...}`, which does the same but presses the buttons after each failed test. Menus and loading screens ignore a stick and the world does not, and "moved" is a twentieth of the sampled frame, because a menu's shimmer changes a few hundred samples and a camera turn tens of thousands. The test runs before the press each cycle, so once the stick works nothing more is pressed. Each step takes an optional `note`. `warmup_s` is the fixed wait the `frame_diff` oracle counts as readiness. The Elden Ring recipe is a wait for the logos and one `press_until_control`.
- `{"pointer": [x fraction, y fraction], "press": [1]}` is for the other kind of menu: one driven by a virtual pointer the left stick moves, where A clicks whatever is under the pointer and does nothing at all when the pointer is over nothing. There is no highlight to walk with a d-pad and no way to place the pointer directly, so the step parks it in the top left corner, where it clamps against the screen edge, and then moves one axis at a time by time at the recipe's `pointer_px_per_s` (`pointer_park_s`, default 2.5 s, is the park hold, which has to be long enough to cross the screen from anywhere). Fractions are of the client rect, so they survive a resolution change while the speed is in screen pixels. It lands within about 50 px over a half-second move, which is well inside a menu card. PowerWash Simulator is the recipe that needed it, and its pointer ignores `SetCursorPos` and synthesized clicks, so the mouse is not an alternative. `pointer_ref_w` is the client width the speed was measured at; a pointer drawn by a UI canvas that scales with the window moves in proportion, so the speed is scaled by the current width over it.
- `window` (optional), `{"w": 1280, "h": 720, "x": 0, "y": 0, "borderless": true}`, puts the game into that window with the app's own `src/borderless.py` (`make_borderless`, or `resize_window` for a framed window): once the window is found, again before the ready sequence and again at readiness if the game re-asserted its own mode meanwhile (Halo Wars shows a framed window at 2 s and a borderless full-screen one at 4 s). The window is looked up again each time, in case the game replaced it, and each application records the client rect before and after and whether it took, in the log and the results JSON; a game in exclusive full screen ignores `SetWindowPos`, which is a property of the game and not a failure. Left 4 Dead 2 and Half-Life 2 have `-windowed -noborder -w 1280 -h 720` on the launch line and need no `window`; the three games with no such argument carry one, so their captures are a ninth of the pixels, their saved frames a fraction of the size, and the Nimbus window has somewhere to sit. It is also the only test the app's window management has.
- `motion` (optional), `{"kind": "rotate"}`, says the camera swings and zooms rather than pans, so the frame oracle measures a rotation and a scale beside the shift (section 4.3). Halo Wars is the one recipe that carries it. The default is `shift`.
- `reset_buttons` (optional), a list of the bridge's button ids pressed in turn by `reset()` on a game with no console. Halo Wars' `[13, 10]` is d-pad left (jump to the base) then right-stick click (rotation back to north). The first reset's picture becomes the reference frame, and every later reset is measured against it the way a step is: its motion from the reference has to read STILL. `reset_press_s`, `reset_gap_s` and `reset_settle_s` (0.15, 0.4 and 1.5 s) tune the presses and the wait for the camera to arrive.
- `expect` (optional) holds bands on the rates checks measured last time, and turns a run into a regression test: `{"G5": {"0.40": {"deg_per_s": -13.8, "tol_pct": 15, "tol_abs": 1.0}}, "G8": {"units_per_s": 199.7, "tol_pct": 20, "tol_abs": 10}}`. The runner records a `<check>e` line for each band, passing when the measured rate is within the larger of the percentage and the absolute tolerance. Units are `deg_per_s` (a console's yaw or pitch, or a `rotate` recipe's measured rotation), `units_per_s` (a console's walk) and `px_per_s` (a believed shift on a frame game, horizontal for yaw, vertical for pitch). `--write-expect` fills the block from a run, for the checks in `--expect-checks` (G5 at `--expect-mags`, G7, G8, N2 and N5 by default; N1 is left out because a full deflection folds on Half-Life 2) with the default bands (15 percent or 1 deg/s, 20 percent or 10 units/s, 25 percent or 10 px/s), merging into what is there so a pad run and a Nimbus run each keep their own checks. Write bands from the checks a game repeats and leave them off the ones it does not.
- `exe` (optional) launches the game by its own executable in `game_dir` instead of through `steam.exe -applaunch`, with `launch_args` as its arguments and Steam started first if it is not running. Arma 3 needs it: Steam's launch opens the Arma 3 Launcher and waits for a click, while `arma3battleye.exe 2 1 1 -exe arma3_x64.exe ...` starts the game with BattlEye directly (its first run shows BattlEye's privacy notice, a one-time click).
- `window_min_client` (optional), `[w, h]`: a matching window smaller than this is not the game window. Arma 3 keeps a 506x250 start-up window alive beside the real one for minutes, and matching the title alone took it, and then a resize applied to it; the finder now takes the largest visible match and this key says what is too small.
- `window_stages` (optional), a list drawn from `launch`, `sequence` and `ready`: the stages at which the recipe's `window` is applied (all three by default). Arma 3 hung before starting its mission when resized while loading, so its recipes name `ready` only.
- `walk_min_units` (optional, default 20) is the distance a second of full left stick has to cover in G8, N5 and the calibration, in the game's units. The default is Source's; Arma 3 walks in metres, about five a second, and its recipes say 2.5.
- `pitch_rate_deg_per_s` (optional, default 25) is what `level_pitch` assumes for the right stick at 0.6, on a game whose reset cannot set the pitch (section 4.3, `arma3`).
- `oracle.type` `arma3` takes `mission` (the folder name under the game's `Missions`, world suffix included), `spawn` (`[x, y]` on that world) and `button_echo` keyed by the harness's button ids with the game's `inputAction` names as markers (`{"1": "Action"}`: A reads as `Action` while held).

### 4.2 The launcher, and the launch-order rule

`Launcher.launch()` deletes the previous console log, writes the harness cfg (section 5), and runs `steam.exe -applaunch <id> <args>`, or the recipe's `exe` from `game_dir` when it names one (Arma 3's BattlEye launcher). Steam starts if it is not running, and is left running at the end; only the game process is killed.

The pad exists before the game is launched. Source decides at start-up whether an XInput controller is present and never reads one created later (measured 2026-09-06 in the aim work: the mouse moved the camera, the pad did not, and the HUD showed the generic JOY3 glyph). The environment therefore builds its actuator in `__init__` and launches the game afterwards, so the rule is enforced by construction rather than by remembering the order of two commands. In Nimbus mode it is Nimbus's pad that has to exist first, so the app is started in-process, then the game is launched.

### 4.3 Oracles

An oracle answers `pose()` (a position and view angles, or `None` when the game has none), `ready()` (the game is in a state where input does something), `reset(pose)`, `wait_echo(marker)` for button checks, and `frame()` for the picture. Both oracles keep the frame-differencing path, so every step also records how many samples of the window changed, and the two verdicts can be compared.

**`source_console`.** The game is launched with `-condebug`, which appends every console line to `<mod_dir>/console.log`, flushed per line. The harness cannot type into the console (it is disabled in controller mode and typing into a game is fragile), so it does not: a generated cfg binds two keyboard keys, F7 to `getpos_exact` and F8 to `exec nimbus_harness_reset`, a second generated cfg holding `sv_cheats 1; setpos_exact x y z; setang p y r; echo NIMBUS_RESET_DONE` for the reset pose (rewritten whenever the pose changes, since `exec` re-reads the file), and the harness presses them with `SendInput` while the game is in the foreground and reads the answer from the log. `getpos_exact` prints a line of the form `setpos_exact x y z;setang p y r` giving the player's origin rather than the eye position, so a reset lands the player on the floor instead of dropping it from eye height; the line is parsed with a regular expression from the part of the log written after the key press. Readiness is "a pose answers", polled every few seconds, since the command prints nothing until the local player exists in a map. A button check rebinds one pad button to `echo <marker>` and waits for the marker in the log. The cfg runs `exec 360controller` first, because that file starts with `unbindall`, then adds the binds, so nothing the harness needs can be undone by the game's own controller setup. F9 and F10 are avoided: F10 is a system key on Windows and arrives as a different message.

**`frame_diff`.** The capture of `tests/probe_game_mouselook_windows.py` and, since 2026-09-09, the motion measurement of `tests/frame_motion.py` between a capture before the action and one at the end of the hold. It used to be a count of changed samples against three times one idle second's count, and a count cannot tell a camera from a HUD: on Halo Wars an animated unit panel changed about 1,300 samples, which one run's idle floor called STILL and the next run's called MOVED (section 8). What is measured now:

- **A shift**, by phase correlation: grayscale, downsampled four times, Hann window, the normalised cross-power spectrum of the two FFTs, inverse FFT. The surface has a peak at every offset by which some part of the picture moved. The peak at the origin is everything that stayed put (the HUD, a weapon, PowerWash Simulator's wand) and is reported as `static`; the three tallest peaks away from it are the parts that moved, each with a height and a sidelobe ratio, both of which have to clear a floor before the offset is believed (measured on five games' saved frames, real moves peak at 0.06 and up, sidelobes at 0.04 and down). Keeping the peaks apart is what lets a turn behind a big static overlay register at all: a single-peak estimate locked onto the overlay and read a whole sweep of PowerWash Simulator turns as no shift, and a bobbing wand hid the world's shift behind it until the list was three deep. A pure yaw with a little bob the other way lands its peak on the zero row and comes out as two halves, one each side, each under the floor while the shift is plain (PowerWash Simulator's one-pixel drag: 63 px as two halves of 0.035); raw peaks within two and a half samples of a taller one are summed into it before the floors apply. And the idle pictures of a run say which offsets are the scene's own: a repeating texture correlates with itself a period away whether or not anything moved (Elden Ring's idle frames carry peaks near 48 px vertically in every sample, at under a hundred changed samples), and a scene's idle animation has its own offset (the Left 4 Dead 2 flashlight), so every idle peak of any real height is an offset a later step may not claim as motion, within 12 px.
- **A rotation and a zoom**, for a recipe whose `motion.kind` is `rotate`: the same correlation on the log-polar resampling of the two magnitude spectra, over the central square of the picture (the map is only isotropic when both frequency axes have the same resolution). It recovers a rotation of the picture to a degree on synthetic pictures; a game camera swinging round a 3D scene is only approximately that, and a swing past the overlap gives no peak at all (section 7).
- **A grid**, 4 by 4, of the changed-sample fraction per cell, from the same sampler as before. A camera move changes every cell; a HUD animation a few at the bottom; a yaw under an empty sky the lower rows only.

The verdict is MOVED for a believed shift past the threshold (or, on a `rotate` recipe, a believed rotation or zoom past theirs), for a picture that changed in at least 12 of the 16 cells (the camera outran the overlap), or for a change spread over 3 cells and 3 times the idle floor (a walk forward is an expansion, not a shift, and has no single offset); STILL for a confident peak at the origin with at most two cells changed, whatever animated inside them; INCONCLUSIVE otherwise. The thresholds come from `--idle-samples` idle seconds (three by default): the floor is the largest changed count over them, and the shift threshold is twice the largest believed idle shift, never under 8 px, so a scene that sways while idle sets its own bar. The changed count is still recorded beside the motion, and every step's line reads `changed=N shift=(dx,dy) px peak=... conf=... static=... cells=k/16 -> VERDICT`. `pose()` is `None`, `reset()` presses the recipe's `reset_buttons` when it has them and is otherwise a no-op, readiness is a fixed warm-up.

**`arma3`** (2026-09-09). The harness cannot type into Arma 3, but Arma can run a script, so the oracle writes a mission and lets the game do the talking. `prepare_launch` puts a one-soldier mission on the VR world (flat, gridded, so the frame oracle sees a turn too) into the game's `Missions` folder, `mission.sqm` and an `init.sqf`, and the recipe launches straight into it with `-init=playMission['','nimbus_harness.VR',true]`. The script publishes the player's pose about thirty times a second with `copyToClipboard`: position (ASL), the yaw and pitch of `getCameraViewDirection` (a compass heading, clockwise, so stick right is a positive delta and the recipe's `turn_right_sign` is 1; pitch up positive; `eyeDirection` was tried first and does not follow the aim's pitch), the body heading, which user actions are held (`inputAction` over a fixed list, which is what the button check reads), and a count of commands taken. Each line also goes to the game's report file with `diag_log`, which trails the clipboard by a steady third of a second (measured), a usable fallback for readiness and echoes but too slow for tracking a turn through a hold. The way back is the same channel: the script reads the clipboard before each pose it writes, and `NIMBUS_RESET x y z dir` puts the player back with `setPosASL` and `setDir` (0.33 s round trip, under 0.3 degrees of yaw), while `NIMBUS_EXEC <sqf>` runs a line of script, a code channel that exists only while the throwaway mission runs and is what settled, empirically, that nothing in SQF sets the aim's pitch (`setVectorDirAndUp`, `switchMove`, `lookAt`, `playMoveNow` all left it where it was). So `resets_pitch` is false on this oracle: the reset check judges the yaw only, and `GameEnv.level_pitch` brings the pitch back with the actuator itself, a closed loop on the right stick at 0.6 using the recipe's `pitch_rate_deg_per_s`, which under Nimbus means the aim widget being dragged. A pose is believed only when its tick time has moved on since the last read, and readiness needs two of them advancing, so a line left on the clipboard by a previous instance reads as no pose; the clipboard is cleared at launch. The user's clipboard is clobbered for the length of a run, and a copy made during one interrupts the channel for one read.

Three things Arma 3 taught that live in this oracle and the recipe keys above. The game lists every controller it found at start-up in the player's profile and gives a new XInput pad `mode="Disabled"`; with that the sticks and buttons do nothing, and `enable_pad_in_profile` sets the engine's own Xbox scheme, `SchemeMovementLeftBrakeTriggerAccTrigger` (the name comes from the game's config; `Custom` and `Default` are accepted too and drive the sticks the same way), behind a `.nimbus-harness.bak` copy, which means the first run on a machine enables nothing (the entry does not exist yet) and the second works. A config error in the profile (a joystick entry with no `mode`, which one of the day's hand edits caused) comes up as a modal "No entry" box that stalls the game for minutes; the oracle never removes a line. And pressing Start opens the pause menu, which in single player freezes the mission's script and with it the pose stream, so nothing here presses it.

### 4.4 Actuators

An actuator takes an action, a dictionary with any of `lx`, `ly`, `rx`, `ry` (minus one to one, right and up positive as XInput has it), `lt`, `rt` (zero to one) and `buttons` (a list of the bridge's button ids, 1 to 14 under ViGEm), and applies it. `release()` centres everything.

**`pad`.** An Xbox 360 pad the harness owns, an `X360Pad` from `src/padbus_client.py` (vgamepad until 2026-09-09). Exact and fast: what the harness asks for is what the game receives, so this is the actuator for calibrating a game.

**`nimbus`.** The real QML app in-process, the way `tests/probe_stick_shaping_windows.py` runs it: the bridge, the QML engine and the profile are the real ones, the pointer is synthesized `QMouseEvent`s on the joystick and button widgets, and the ViGEm pad is the bridge's own. A stick action becomes a drag of `value * travel` pixels from the widget's centre (`travel` is the widget's `travel_px`, else its drawn radius), so the whole shaping chain runs (deadzone, curve, anti-deadzone floor, extremity cap); the actuator reports what the bridge actually sent from the ViGEm interface's `current_values` beside what the game did, and the expected values come from the bridge's own resolved parameters through `shape_magnitude`, never from a number in this document. The app is placed beside the game window, never over it. By default the run uses a throwaway copy of the bundled `adaptive_platform_2` profile written into the user profiles folder and removed afterwards, so the result does not depend on what the user has done to their own copy; `--profile <id>` runs an existing profile instead. When several widgets could take the press (the user's copy of the bundled profile has two overlapping left sticks), the actuator picks the topmost one whose centre nothing later in the layout covers, because that is where a real press would land. `controller_config.json` is restored afterwards.

**`netpad`** (2026-09-14). The pad reaches the game through the network pad prototype of [Separate Game Machine](SEPARATE_GAME_MACHINE.md) section 10: a `NetpadSender` in the harness, ticked by its own 120 Hz loop thread standing in for the app's loop, and `netpad.receiver` as a separate process that owns the ViGEm pad, over UDP loopback. It runs every pad check, so the recipe's pad bands compare the two directly, and then its own: the session survived the run (NP1), a frozen loop stops the camera mid-turn and resumes it (NP2), an explicit Stop does the same (NP3), and whether the game reads a pad the receiver destroyed and re-created (NP4, a finding). Built before the launch like the pad actuator, and it waits for the receiver's pad to be plugged. It adds the whole netpad path and a second process; it does not add a network, because the game has to run on this machine.

A button a `ready_sequence` asks for that the profile draws no widget for is pressed straight at the bridge (`ControllerBridge.setButton`) and the run says so on the line it happens. The bundled layout has A, B, X, Y and the two bumpers; Halo Wars needs the d-pad to walk the main menu and Start to begin the match, and before 2026-09-09 those presses were dropped in silence, which left the game sitting in the campaign menu while the in-world checks measured it (section 8). A menu walk is how the game is reached and not part of what is measured, so driving it at the bridge is honest; every check that produces a number still drives a real widget through the whole shaping chain.

### 4.5 The environment

```python
env = GameEnv(recipe, actuator="pad", frames_dir=...)   # the pad exists now
env.launch()                                            # Steam launch, window found
env.wait_ready()                                        # the oracle says the game is playable
env.reset()                                             # known pose (or record one)
obs0 = env.observe()                                    # {"pose": {...}, "frame": ..., "t": ...}
obs1 = env.step({"rx": 0.6}, hold=1.0)                  # apply, hold, release, settle, observe
env.close()                                             # release, kill the game, restore
```

`step` returns the observation after the hold plus the deltas from the observation before it: yaw and pitch, horizontal distance, vertical distance, the changed-sample count of the frame difference, the measured motion (`motion`: shift, static peak, rotation where the recipe asks, grid) and the verdict made from it. With a console oracle the pose is also read through the hold, about twenty times a second, and the yaw delta is the sum of the wrapped steps between reads: a full deflection turns past 180 degrees inside a second (480 degrees in one second on Left 4 Dead 2, measured once the harness stopped folding it), so a single before-and-after read gives the wrong angle. The folded value is kept beside it as `d_yaw_wrapped`. The results JSON is a list of those.

Sign conventions, so numbers in the results log read the same way everywhere: `rx` positive is stick right; Source yaw increases turning left, and Left 4 Dead 2's `joy_yawsensitivity` is negative, so stick right should give a negative yaw delta. Pitch is positive looking down in Source. Both are measured, not assumed; the runner's direction checks say what was found.

### 4.6 The runner and its checks

`tests/probe_game_harness_windows.py --game left4dead2 --actuator pad` runs, in order:

| Check | Pass rule |
|---|---|
| G0 launch | the window appears within `window_timeout_s`; with `source_console`, the console log exists; the recipe's `window` is applied and whether it took is logged |
| G1 ready | the oracle reports ready within `ready_timeout_s`; a pose is read |
| G2 reset | after `reset()` the pose is within 2 units and 1 degree of the reset pose; on a game with no console but `reset_buttons`, a second of left stick moves the view off, the buttons are pressed, and the picture's motion from the reference frame reads STILL |
| GS walk survey (`--survey-walk`) | a second of left stick in each of eight headings from the reset spot; the reset pose is turned to the longest run, which must exceed 150 units; `--write-reset-pose` writes it into the recipe |
| G3 idle | `--idle-samples` seconds of no input: yaw within 0.5 degrees, position within 1 unit; the noise floor and the motion thresholds come from all of them (on a game with reset buttons they are measured before G2, whose landing is judged with them) |
| G4 yaw control | `rx` at 1.0 for the hold: more than 10 degrees, and the direction is the recipe's turn-right sign |
| G5 yaw sweep | `rx` at each of the sweep magnitudes; the table of degrees per second; the first magnitude past 1 degree is the game's deadzone; the motion and the frame verdict for each step are recorded beside the ground truth |
| `<check>e` expected | for every band in the recipe's `expect` block (G5 per magnitude, G7, G8, and N1, N2, N5 on the Nimbus run): the measured rate is within the band; a check with a band and nothing comparable measured fails |
| G6 yaw left | `rx` at minus 0.6: the opposite sign, and a rate within 25 percent of the right turn at 0.6 |
| G7 pitch | `ry` at 0.6: more than 1 degree of pitch; the sign is recorded |
| G8 move | `ly` at 1.0 for one second: more than the recipe's `walk_min_units` of horizontal travel (20 by default, Source units; 2.5 on Arma 3, in metres); units per second recorded |
| G9 button | the pad button bound to the echo marker: the marker appears in the log within two seconds |
| G10 release | after everything: one second idle, pose unchanged (nothing is stuck) |
| G11 latency | `rx` at 1.0 with the pose polled every 60 ms: the first sample whose yaw moved, as a coarse latency bound |
| G12 calibration | yaw against hold time (0.1, 0.25, 0.5 and 1 s) at 0.40, 0.60 and 1.00, and forward travel against hold time at full left stick; every column grows with the hold. `--write-calibration` writes the table to `src/spectator/calibrations/<game>.json` |

With `--actuator nimbus` the same environment runs with the real app, and the checks are the end-to-end ones:

| Check | Pass rule |
|---|---|
| N0 app | the QML window registered with the bridge, ViGEm mode, the profile's rx/ry stick found, game launched second and ready; then the same reset and idle checks as G2 and G3 (N0b, N0c) |
| N1 full drag | a drag to the stick's edge turns the camera more than 10 degrees, and the bridge sent its own ceiling for that stick (0.95 on the bundled profile) within 0.02 |
| N2 one-pixel drag | a 1 px drag turns the camera by more than 1 degree, and the bridge sent its own floor for that stick: the anti-deadzone clears the game's threshold, now in degrees. On a recipe with `floor_moves_camera` false the rule inverts: the floor was sent and the camera stayed still |
| N3 release | the stick released reads exactly zero at the bridge and the pose is stable |
| N4 button | a click on the LB widget produces the echo marker in the log |
| N5 left stick | a full drag up on the left stick moves the player more than the recipe's `walk_min_units`, and the bridge sent its ceiling |

then the Spectator+ primitives (section 4.7) through the bridge's own runner, measured by the game:

| Check | Pass rule |
|---|---|
| P0 calibration | the bridge's runner loads `src/spectator/calibrations/<game>.json` |
| P1, P2, P3 turn | turn right 90, left 45, right 10 degrees: the yaw delta is within 15 percent or 5 degrees, whichever is larger |
| P4 walk | walk 100 units: the horizontal travel is within 20 percent or 10 units |
| P5 stop | a 400-unit walk cut short after 0.3 s: the bridge reads zero on the stick and the player stays put for the next second |

Each check prints one line and lands in `tests/probe_frames/harness_<game>_<actuator>.json` with every observation. Frames are saved beside it.

### 4.7 Spectator+ v0: scripted primitives

Built the same day as the harness, as the first Spectator+ feature a user could touch, with no model behind it. `src/spectator/` holds two modules:

- **`calibration.py`.** A game's measured stick response, from G12: for each right-stick magnitude, yaw delta against hold time; for the left stick up, travel against hold time. `hold_for(samples, amount)` interpolates the hold that yields an amount, linearly between samples and along the last segment beyond them. `plan_turn(degrees)` picks the smallest magnitude whose hold fits between 0.2 and 2 s (a small angle gets a slow stick so timing error stays small; a large one gets the fastest), `plan_walk(units)` does the same for travel. The table needs several holds per magnitude because Source ramps stick input above `joy_lowend`: the first tenth of a second turns far less than a tenth of the one-second figure.
- **`primitives.py`.** `PrimitiveRunner`, a `QObject` that executes a plan as timed steps on a precise `QTimer`, never blocking the UI thread: set the axis, wait the hold, zero it. One primitive at a time; `turn`, `walk` and `press` return the plan or `None`; `stop()` cancels and zeroes everything the plan touched; every plan ends with a release whatever happens; `started` and `finished(name, completed)` signals for a future UI.

The bridge owns the runner through `get_spectator()`, created on first use and bound to `setAxis` and `setButton`, so a primitive's output goes through the active driver interface and its limits. It bypasses the widget shaping on purpose: the shaping is for the user's own hand, and "turn 90 degrees" has to be the same turn whatever curve the user has on their stick. A profile switch and the Ctrl+Alt+F12 stop also cancel a running primitive.

What is not there yet: a way to trigger a primitive from the UI or from voice (the concept document's command palette), a closed loop (the runner cannot read the game, so it trusts the calibration; the harness measures how far that trust goes, section 8), and calibrations for any game but Left 4 Dead 2. The environment stays in `tests/` until the app has a consumer for it.

---

## 5. Source engine details

What the `source_console` oracle relies on, checked against the Left 4 Dead 2 install on the dev machine:

- `-condebug` on the launch line writes `left4dead2/console.log`, appending; the harness deletes it before each launch. `con_logfile` exists too but is not needed.
- `getpos`, `getpos_exact`, `setpos`, `setpos_exact` (client), `setang` and `sv_cheats` (client, server, engine) are present in the binaries. `setpos`, `setpos_exact` and `setang` need `sv_cheats 1`, which the launch line sets before `+map` and the reset cfg sets again. `getpos` prints the eye position, `getpos_exact` the origin; the harness uses the exact pair so a reset is a teleport onto the floor.
- `cfg/valve.rc` runs `exec autoexec.cfg` and then `stuffcmds` (the `+` commands from the launch line), in order. The harness uses `+exec nimbus_harness`, not `autoexec.cfg`, so nothing persists between sessions; the generated `cfg/nimbus_harness.cfg` and `cfg/nimbus_harness_reset.cfg` are removed at close.
- `cfg/360controller.cfg` begins with `unbindall`, sets `joy_yawsensitivity -1.5`, `joy_pitchsensitivity 1.0`, `joy_response_look 1`, `joy_lowend 0.65`, `joy_lowmap 0.15`, `joy_accelscale 3.0` and `joy_accelmax 4.0`, and binds the pad buttons (`L_SHOULDER` to `toggle_duck`, `BACK` to `togglescores`). The harness cfg execs it first and then binds `F7`, `F8` and the echo button, so the pad keeps its normal layout except for the one button used as a marker.
- Keys are injected with `SendInput` virtual-key events carrying their scan code, because Source maps keys by scan code; they arrive through the normal message path even with `joystick 1`. The console itself stays disabled (`con_enable 0` in the user's config).
- A map started with `+map` is a local listen server with the player in the first safe room of `c1m2_streets`, which the aim work found stable enough for measurements (the infected cannot enter, the bots stay).

---

## 6. How to run

The pad calibration, unattended, about four minutes including the game start:

```
venv\Scripts\python tests\probe_game_harness_windows.py --game left4dead2 --actuator pad
```

The end-to-end run with the real app, same environment, including the Spectator+ primitives:

```
venv\Scripts\python tests\probe_game_harness_windows.py --game left4dead2 --actuator nimbus
```

Arma 3, the BattlEye title, with and without the anti-cheat (the second is the control; run them one after the other, never together):

```
venv\Scripts\python tests\probe_game_harness_windows.py --game arma3 --actuator pad
venv\Scripts\python tests\probe_game_harness_windows.py --game arma3 --actuator nimbus
venv\Scripts\python tests\probe_game_harness_windows.py --game arma3_nobe --actuator pad
```

Two things about the first time on a machine: BattlEye's launcher shows its privacy notice once, an OK to click, and Arma only lists the pad in the player's profile after a launch with the pad present, so the first run finds nothing to enable and the second works. During a run, leave the clipboard alone as well as the mouse and keyboard: the pose travels through it.

The primitives need the calibration the pad run writes with `--write-calibration` (checked in as `src/spectator/calibrations/left4dead2.json`; rerun it after a change to the game's controller cfg or to the harness's hold timing). The second Source game, which needs no flags and takes about two minutes:

```
venv\Scripts\python tests\probe_game_harness_windows.py --game halflife2 --actuator pad
```

A game with no console, launched through its title screen by the recipe's `ready_sequence`, about six minutes:

```
venv\Scripts\python tests\probe_game_harness_windows.py --game eldenring --actuator pad
```

The real-time strategy game, whose sticks drive a camera rather than a player, about four minutes on either actuator:

```
venv\Scripts\python tests\probe_game_harness_windows.py --game halowars --actuator pad
venv\Scripts\python tests\probe_game_harness_windows.py --game halowars --actuator nimbus
```

A run is a regression test once its recipe carries an `expect` block. Write one from a run that looked right, and every later run compares itself with it:

```
venv\Scripts\python tests\probe_game_harness_windows.py --game left4dead2 --actuator pad --write-expect
```

None of this is the fast suite. That is `venv\Scripts\python tests\run_fast_tests.py`, seconds, no hardware, and what CI runs; `tests/test_frame_motion.py` in it covers the frame oracle's measurement with synthetic pictures.

Run them in separate invocations, with the game quit in between (the runner does this), so each session's pad is player one. `--keep-game` leaves the game running for a look (a window the recipe resized is restored first); `--sweep 0.2,0.26,0.28,0.3,0.4,0.6,0.8,1.0` and `--hold 1.0` change the yaw sweep; `--idle-samples 3` sets how many idle seconds the noise floor and the motion thresholds come from; `--write-reset-pose` writes the first pose read into the recipe; `--write-expect` writes the rates this run measured into the recipe's `expect` block as bands, for the checks in `--expect-checks` at the magnitudes in `--expect-mags`, and a later run fails where the game answers differently; `--profile <id>` makes the Nimbus run use an existing profile instead of the throwaway copy of the bundled one. Both need ViGEmBus. Both are safe over TeamViewer: every stimulus is injected, and the game window is left in the foreground only while a step runs. Steam must be able to sign in without a prompt. A pad run takes about two and a half minutes from launch to the game quitting; a Nimbus run about the same.

---

## 7. Limits and open questions

- **Anti-cheat titles** run when test signing is off: Elden Ring (Easy Anti-Cheat) on frame differencing, and since 2026-09-09 Arma 3 (BattlEye) with a pose from its own scripting, launched by the harness through BattlEye's launcher. Under test signing EAC refuses to start; whether BattlEye does is untested. Neither run touches a server, which is where BattlEye's kicks happen.
- **The pose is sampled, not streamed, on Source.** Each pose read there costs a key press and a log read, about 47 ms on the dev machine, so `observe()` is good to about 50 to 100 ms and the latency check is a bound, not a measurement. Arma 3's oracle streams it instead, about thirty poses a second on the clipboard, and a read costs a few milliseconds. Fine for tests and for scripted primitives; a learned agent at frame rate would need `cl_showpos` on screen and a reader, or a plugin.
- **The safe room** has walls within 114 to 140 units in three of eight headings from the reset spot. `--survey-walk` measures all eight and turns the reset pose to the clearest (200 units at yaw 135); a game or map change should rerun it with `--write-reset-pose`.
- **The frame oracle is not trustworthy in the Source scenes, and now says so itself.** The survivor bots walk through Left 4 Dead 2's safe room and the flashlight beam sways while idle: the idle second measures a coherent 54 px sway of the beam, so the run's shift threshold is 108 px and a 1.5 degree turn reads STILL against a console that saw it. The verdict is recorded beside the ground truth and never used for a pass on a Source game. A recipe for a game with no console should pick a scene without moving actors.
- **A repeating texture aliases the shift.** Phase correlation finds the true shift modulo the texture's period: the safe room's wallpaper is striped every 56 px, and every turn in the saved sweep measured its true shift less a whole number of stripes, sign and all (34 degrees, about 480 px, read as +80). The verdict survives, because any coherent move past the threshold is a move; the pixel figure is a number to band only where a run shows it repeats.
- **A camera swing is not a rotation of the picture.** The log-polar estimate recovers a rotation of a picture about its centre to within a degree on synthetic pictures, and recovered nothing on Halo Wars' one-second swings at 0.60 and above: the camera goes past the overlap, and a perspective camera orbiting a 3D scene is not an image rotation anyway. Those steps are MOVED by the whole-picture rule. A calibration table for that game in degrees would need holds short enough to keep the swing inside the overlap, which is a run nobody has made yet.
- **The pad's own deadzone is not the game's.** The harness sends exact XInput values, so the sweep measures the game's threshold (0.26 to 0.28 here). A physical pad would add its own.
- **The noise floor and the camera-or-HUD question are settled, and the old numbers are not comparable.** Every frame verdict logged before 2026-09-09 (Elden Ring, PowerWash Simulator, Halo Wars) was a changed-sample count against one idle second; every one after is a motion verdict against three. Section 8 carries a rebaselining pass over the three games so their numbers come from one method.
- **An animated menu passes the readiness test.** For a game with no console, readiness is a fixed warm-up plus a live picture, and a menu with a moving background or a scrolling tip ticker is a live picture. If a `ready_sequence` stops in a menu the run continues and measures the menu; Halo Wars did exactly this when its d-pad presses were being dropped. The in-world checks fail, which is the signal, but the reason has to be read off the saved frames.
- **Where does `GameEnv` live?** In `tests/` until Spectator+ has code in `src/` that imports it.

---

## 8. Results log

Filled in by the runs. Each entry: date, machine, game and map, actuator, the table, and what the game taught.

### 2026-09-07, dev machine, Left 4 Dead 2, `c1m2_streets` safe room, windowed 1280x720, pad actuator

Three runs the same afternoon. **Run 1** parsed nothing: `getpos_exact` prints `setpos_exact x y z;setang_exact p y r`, with `setang_exact` rather than `setang`, so the pattern was widened. **Run 2** (11/12) exposed the readiness gap: the server-side player has a pose while the client still shows the loading screen (an all-zero pose early in the load, then the spawn position), so the harness declared the game ready 23 s after launch on a static picture, measured a frame noise floor of 2, and its idle step then watched the spawn move the player 65 units. Readiness now also needs a live picture (two captures half a second apart differing by more than 50 samples; the loading screen differs by 17) and a pose that holds still for 1.5 s. **Run 3, 12/12**, launch to window 4 s (Steam already running; 24 s when Steam had to start), launch to ready 29 s, frame noise floor 100 in the map, reset pose recorded into the recipe. The right-stick yaw sweep, one second per magnitude, from the same reset pose each time:

| `rx` | yaw delta (deg) | deg/s | changed samples | frame verdict | truth |
|---|---|---|---|---|---|
| 0.20 | +0.00 | 0.0 | 2326 | MOVED | still |
| 0.26 | +0.00 | 0.0 | 4329 | MOVED | still |
| 0.28 | -1.51 | -1.5 | 2598 | MOVED | moved |
| 0.30 | -3.55 | -3.6 | 3683 | MOVED | moved |
| 0.40 | -13.75 | -13.8 | 7307 | MOVED | moved |
| 0.60 | -34.38 | -34.4 | 8584 | MOVED | moved |
| 0.80 | -55.24 | -55.2 | 9213 | MOVED | moved |
| 1.00 | -464.78 (run 3 read -122.78, folded past 180) | -464.8 | 10972 | MOVED | moved |

- **Deadzone** between 0.26 and 0.28, where the frame-differencing probe put it on 2026-09-06 and where the XInput constant of 0.265 says it is. At 0.28 the camera turns 1.5 degrees per second: the anti-deadzone floor is above the threshold, but only just, which is the number to remember when a game feels sluggish at the smallest movement.
- **Rates.** 0.40 gives 14 degrees per second, 0.60 gives 34, 0.80 gives 54, each linear in time. Full deflection is another regime: **481 degrees in one second** (465 in the sweep step), a full circle and a third, because `360controller.cfg` turns on Source's stick acceleration (`joy_accelscale 3`, `joy_accelmax 4`) at the stop and the rate ramps up through the second: 8 degrees in the first tenth, 37 by a quarter second, 124 by half. Runs 2 to 4 reported 116 to 123 for the same step because a single before-and-after read folds anything past 180 degrees back into the range, and the sweep table above carried that folded value until run 5; the harness now reads the pose through the hold (section 4.5) and keeps the folded number beside the real one. A turn primitive has to be calibrated per magnitude and per hold, not by one slope.
- **Symmetry.** Left at 0.60 turned +34.3 degrees per second against -34.4 to the right (ratio 1.00). **Pitch** at 0.60 up: -21.2 degrees in a second (up is negative pitch in Source, so stick up looks up on this config).
- **Movement.** Left stick full up moved the player 114 units in a second from the first reset pose, which turned out to face a wall 114 units away (runs 3 and 4 gave the same 114.1 at every hold). Run 5's walk survey tried eight headings from the same spot (121, 142, 184, 200, 140, 195, 114 and 195 units for yaw 0 to 315) and turned the reset pose to yaw 135, where a second of stick gives 200 units on the floor (`d_z` 0), the survivor's run speed.
- **Button.** The left bumper landed in the console log 31 ms after the pad update, in both runs.
- **Latency.** With the stick at 1.00 and the pose polled as fast as the key-and-log loop allows, the first sample (47 ms) still read zero and the second (94 ms) read 2.3 degrees. So the pad-to-game path is under 94 ms and probably close to 47; a finer bound needs a faster oracle. (Run 5 read zero at 47 and 78 ms and 0.8 degrees at 125 ms: at full deflection the ramp starts slowly, so the first visible degree lags the input path.)
- **Nothing stuck** afterwards: one idle second, pose unchanged.
- **Frame differencing over-reports in this scene.** At 0.20 and 0.26 the camera did not turn at all and the frame still changed by 2,300 and 4,300 samples, far above the 300 threshold: the survivor bots walk through the view and the flashlight glare flickers, and a one-second noise floor does not cover them. The 2026-09-06 measurement got the right answer with the same method because the bots happened to be quiet. With ground truth beside it, the frame verdict is now recorded but never trusted on its own for a Source game.

What the game taught, beyond the numbers: the spawn pose differs per launch (yaw -89.6 in one run, -46.9 in the next, 90 units apart), so the recipe carries a fixed reset pose rather than taking the first read; and `+sv_cheats 1` before `+map` does carry into the listen server, so `setpos_exact` and `setang` work from the first reset.

**Runs 4 and 5**, the same day, added G12, the calibration table for the primitives. Run 4 (12/13) is where the fold showed: at full deflection the half-second hold read 124 degrees and the one-second hold 91, which cannot both be true unless the second wrapped, and the walk table stopped at 114 units for every hold, a wall. Run 5, **14/14**, with the pose read through the hold and the walk survey (GS) turning the reset pose to face the clear run, wrote `src/spectator/calibrations/left4dead2.json`:

| Hold | `rx` 0.40 | `rx` 0.60 | `rx` 1.00 | `ly` 1.00 (units) |
|---|---|---|---|---|
| 0.10 s | -1.2 | -3.1 | -7.7 | |
| 0.25 s | -3.3 | -8.2 | -36.6 | 54 |
| 0.50 s | -6.7 | -16.6 | -123.7 | 120 |
| 1.00 s | -13.6 | -32.9 | -456.5 | 200 |

Below the stop the turn is linear in time at 13.6 and 33 degrees per second. At the stop it is not: a tenth of a second gives 8 degrees, a quarter 37, a half 124 and a second 457, the acceleration ramp. The walk is 200 units per second with about 4 units of coasting after a short hold. The rest of run 5 matched run 3 to a tenth of a degree everywhere below the stop, and this time the frame verdicts agreed with the console at every magnitude, which says how much the earlier disagreement was the bots.

### 2026-09-07, the same game, Nimbus actuator

**Run 1, 10/11.** `switchProfile("adaptive_platform_2")` resolves to the user's copy in the profiles folder, which on the dev machine has an aim stick at sensitivity 18 and extremity 40 and a second, larger left stick laid over the first. Everything the bridge did was right and the one failure was the check's own assumption: a full drag sent 0.600, which is that stick's ceiling, and the game turned 36.6 degrees per second, what the pad calibration gives for 0.60 (34.4); a 1 px drag sent 0.285, that stick's floor, and turned 2.2 degrees per second (the pad: 1.5 at 0.28, 3.6 at 0.30); a click on the LB widget echoed in 62 ms; releasing read exactly zero and left the pose still; the left stick's full drag moved the player 114 units but sent 0.794 instead of 0.95, because the press at the smaller stick's centre landed on the larger stick drawn over it (travel 72.8 px against 56.4, and 56.4 / 72.8 = 0.775 raw, which shapes to 0.794). Three changes followed: the Nimbus run uses a throwaway copy of the bundled profile unless `--profile` says otherwise, the expected values come from the bridge's own parameters, and the actuator picks the topmost widget under a press.

**Run 2, 11/11**, on the throwaway copy of the bundled profile (aim stick travel 160 px, left stick drawn radius 81 px), launch to ready 29 s, noise floor 508 with a bot in view:

| Step | Bridge sent | Expected from the bridge's parameters | Game |
|---|---|---|---|
| full drag on the aim stick | RX +0.950 | 0.950 | -140.4 degrees in the step |
| 1 px drag on the aim stick | RX +0.289 | 0.289 | -2.7 degrees in the step |
| release | all sticks 0.0 | 0 | pose still for a second |
| click on the LB widget | button 5 | | echo in the log after 47 ms |
| full drag up on the left stick | LY +0.950 | 0.950 | 114 units forward in a second |

The step numbers are consistent with the pad calibration once the drag time is counted: the synthesized press and three moves take 125 ms before the one-second hold starts, so the stick is on for about 1.13 s, and 140 degrees over 1.13 s is 125 degrees per second against the pad's 123 at full deflection; the 1 px drag's 2.7 degrees sits between the pad's 1.5 at 0.28 and 3.6 at 0.30, where 0.289 belongs. That is the aim work's claim (section 14 of the aim document) restated in the game's units: the smallest movement Nimbus can make turns this game's camera at about 2.5 degrees per second. (The "125 at full drag" this run reported was the folded figure; run 3 below has the real one.) The throwaway profile was removed and `controller_config.json` restored at the end.

**Run 3, 17/17**, after the yaw unwrapping, the surveyed reset pose, and the Spectator+ primitives. The N-series repeated (a 0.95 drag now reads 406 degrees in the step, the 1 px drag 2.4, the left stick 200 units on the clear run, the button 62 ms), and then the primitives ran through `ControllerBridge.get_spectator()` with the calibration from run 5, each measured by the console:

| Primitive | Plan | Result |
|---|---|---|
| turn right 90 | `rx` +1.00 for 0.403 s | -86.4 degrees (tolerance 14) |
| turn left 45 | `rx` -0.60 for 1.370 s | +45.7 degrees (tolerance 7) |
| turn right 10 | `rx` +0.40 for 0.741 s | -9.9 degrees (tolerance 5) |
| walk 100 units | `y` +1.00 for 0.425 s | 105.6 units (tolerance 20) |
| stop a 400-unit walk after 0.3 s | | 72.7 units before the stop, 0.0 in the next second, stick at zero |

The planner's choices are what section 4.7 intends: small angles go to the slow stick and long holds, the 90 goes to the stop where timing matters most, and that is where the error is largest (4 degrees short on a 0.4 s hold, about 10 ms of ramp). A closed loop would fix it; for v0 the tolerance is the honest number.

### 2026-09-07, dev machine, Elden Ring 1.17, full screen 2560x1440, pad actuator, frame-differencing oracle

The second recipe, for a game with no console: verdicts, not numbers. Three runs. **Run 1** (4/5) never left the title screen: a notice that the last session was not quit from the menu ("Quit Game or Return to Desktop might not have been selected") sits in front of it with an OK button, and it will always be there, because the harness kills the game at the end of a run; the fixed sequence's presses came before it appeared and the control step saw zero changed samples. **Run 2** (4/5) reached the main menu and stopped there with Continue highlighted: the third press came before the menu was up, and the menu's gold shimmer (200 of 102,480 samples) passed the old fixed threshold of 150 for "the stick moved the picture". **Run 3, 11/11**, with a 30 s wait for the logos and one `press_until_control` step (test the stick, press A after a failed test, every 5 s, "moved" a twentieth of the frame) reached the world 69 s after launch, 19,726 samples moving under the stick. Against a noise floor of 330 in the world:

| Step | changed samples | verdict |
|---|---|---|
| control `rx` 1.00 | 31,787 | MOVED |
| 0.20 | 1,057 | MOVED (marginal: the threshold was 990) |
| 0.26 | 3,053 | MOVED |
| 0.28 | 5,205 | MOVED |
| 0.30 | 6,762 | MOVED |
| 0.40 | 11,880 | MOVED |
| 0.60 | 37,223 | MOVED |
| 0.80 | 44,191 | MOVED |
| 1.00 | 15,947 | MOVED |
| left 0.60 | 13,772 | MOVED |
| pitch up 0.60 | 8,619 | MOVED |
| walk 1.00 | 7,707 | MOVED |
| idle after | 2,131 | (open ground, more grass in the wind) |

What the game taught: its camera deadzone is at or below 0.20 by this measure (0.20 is marginal, 0.26 is not), lower than Left 4 Dead 2's 0.28, which is worth knowing for the anti-deadzone default; the full-deflection step changed fewer samples than 0.80 because the camera came most of the way round in the second and the frame was compared with something like itself, which is the frame oracle's blind spot and exactly the fold the console oracle had to be taught to unwrap; and with no console there is no reset, no calibration and no primitives for this game until another oracle exists (section 2.1). The unclean-exit notice is the harness's own doing; quitting through the menu would need a scripted path through the System menu, which is a later refinement.

### 2026-09-07, dev machine, all three suites rerun back to back

A validation pass over the checked-in harness with nothing changed since the runs above: **Left 4 Dead 2 pad 13/13**, **Left 4 Dead 2 Nimbus 17/17**, **Elden Ring pad 11/11**, run one at a time with the game quit in between. Steam was already running, so both Left 4 Dead 2 launches showed a window in 2 s and were ready in 29 s; Elden Ring took 6 s to a window and 69 s to the world, the same as its run 3.

The pad calibration reproduces itself. Yaw per second at 0.28, 0.30, 0.40, 0.60, 0.80 and 1.00 read -1.5, -3.6, -13.6, -34.2, -54.8 and -466.0 against the -1.5, -3.6, -13.8, -34.4, -55.2 and -464.8 of run 5, the deadzone still falls between 0.26 and 0.28, the walk is 199.7 units in a second, pitch is -21.3 at 0.60 up, the button echo landed in 32 ms and the latency bound was 94 ms. G12's table matched the checked-in calibration everywhere below the stop and to within 7 degrees at the stop (-449.5 against -456.5 for a one-second hold), so `src/spectator/calibrations/left4dead2.json` was left alone.

The Nimbus run repeated its numbers too: 0.950 sent at full drag for 400.4 degrees in the step, the 0.289 floor for 2.4 degrees, 200.0 units on the left stick, 62 ms to the button echo, and the primitives at -83.7 for a 90, +45.2 for a 45, -9.9 for a 10, 105.9 units for a 100, and a clean stop. The throwaway profile was removed and `controller_config.json` restored.

Two things the reruns confirmed about the frame oracle, both already suspected:

- **The Source scene's noise floor is what it is.** This run measured 2960 idle samples, against 100 in run 3 and a quiet scene in run 5, so the frame verdict disagreed with the console at 0.28, 0.30 and 0.40 (STILL, STILL and INCONCLUSIVE where the camera plainly turned). Run 5's agreement at every magnitude was the bots being quiet, not the method improving. The rule stands: on a Source game the verdict is recorded and never trusted.
- **The floor moves the Elden Ring verdicts near the threshold.** Its idle read 456 samples here against 330 in run 3, which lifts the moved threshold from 990 to 1368 and turns 0.20 from a marginal MOVED (1057) into INCONCLUSIVE (1102). Same picture, different label: the deadzone is at or below 0.26 and 0.20 is the edge, but where the save leaves the character decides which side of the line it lands on. A magnitude this close to the floor needs the console oracle or a longer noise measurement to call. The full-deflection blind spot repeated exactly (1.00 changed 32,388 samples against 0.80's 46,855), and the post-run idle read 3,970 in open grass.

### 2026-09-07, dev machine, Half-Life 2 (20th anniversary), `d1_eli_01`, windowed 1280x720, pad actuator

The second Source recipe, written the same day Half-Life 2 was installed, and the one that separated what the harness knew about Source from what it knew about Left 4 Dead 2. Four runs. The map is Black Mesa East, picked for having no hostiles, so nothing can kill the player or knock it out of the reset pose mid-run. Launch to window 4 s, to ready 12 to 14 s, both faster than Left 4 Dead 2.

Three things the recipe could not have guessed, each of which cost a run and each of which is now fixed in the harness rather than in the recipe:

- **`getpos` writes its two halves separately, and prints between them.** This engine has no `getpos_exact`, and the `getpos` it does have prints a `Map name: <map>` line of its own between `setpos x y z;` and `setang p y r`, landing mid-line about one read in ten: `setpos 149.279999 4453.520020 -1342.167480;Map name: d1_eli_01` and then `setang` on the line after. `POSE_RE` wanted the two adjacent, so 39 of 366 reads returned nothing, no reset pose was ever established, and every check ran from wherever the previous one left the player. The pattern now allows anything without a `;` between the halves, which parses every pairable read and still refuses to pair one read's position with a later read's angle. Checked against the captured log rather than by another launch: 327 matches before, 356 after, and the 10 that remain are two overlapping reads where the first position genuinely has no angle of its own.
- **A read could take the previous read's answer.** The console writes lag the key press far enough that a read searching from the offset it captured before pressing would match the pose still arriving for the read before it, which pairs a fresh position with a stale one. Every measurement built on two reads was wrong in a way that looked like noise. The pose key now echoes a marker before asking, and the pose is taken from after that marker.
- **`exec` does not run inline.** The harness cfg's own loaded marker printed at log line 45 and the joystick output that `exec 360controller` triggers at 54 and 62, so binds written after the exec were taken back by the game's own controller cfg, which binds every pad button. The harness now writes its binds to a separate file exec'd after the base cfg, which is correct whether the engine appends the exec'd file or inserts it. Left 4 Dead 2 behaves the other way round and passes either way.

Then the finding that is not a harness bug at all:

- **Half-Life 2 never acts on the pad's buttons, only its axes.** `key_listboundkeys` shows every bind in place (`"JOY5" = "echo NIMBUS_BUTTON_LB"`), the `A_BUTTON` style names Left 4 Dead 2 uses are rejected here as invalid keys, and pressing all fourteen pad buttons puts nothing in the log. The control that makes it conclusive: turning the stick immediately before and after each ignored press moved the view by +68 to +176 degrees every time, so the game was reading the pad throughout. This is worth more than the test: **a Nimbus user driving Half-Life 2 gets the sticks and no buttons.** The recipe records it as `pad_buttons_reach_game: false` so the check is skipped with its reason rather than failing forever.

What the game measures at, and what it cannot:

| `rx` | deg/s | note |
|---|---|---|
| 0.20 to 0.30 | 0.0 | still, frame verdict agrees |
| 0.40 | -47 to -51 | -47.24, -47.26 and -50.68 across three runs |
| 0.60 | -178 | -178.87, -178.99 and -177.82; left at 0.60 gave +188.2, ratio 1.06 |
| 0.80, 1.00 | not measurable | see below |

The final run of the recipe as it stands is **12/13**, the one failure being the calibration table, which cannot be built while the full-deflection row folds.

- **The deadzone sits between 0.30 and 0.40**, well above Left 4 Dead 2's 0.26 to 0.28 and above Elden Ring's, which is worth keeping in mind for an anti-deadzone default: a floor tuned to one of these three games is wrong for the other two.
- **Above about 0.60 the camera outruns the oracle.** The same full-deflection check has read -99.97, -327.78, -167.01 and +41.52 on separate runs of the same recipe, and 0.80 has read -31.13 twice and -45.88 once: the turn passes 180 degrees between pose samples, so it folds, and the sign itself stops being trustworthy. Shortening the hold does not rescue it, because a shorter hold leaves the tracking loop fewer samples, not more. A pose read costs about 50 ms and no hold this harness can set keeps a full-deflection turn under 180 degrees between two of them. Trust this recipe at 0.40 and 0.60; a real number at the stop needs a streaming oracle (`cl_showpos` and a reader), which is section 7's open question arriving with a concrete case behind it.
- **Walk** is about 220 units in a second (216.6, 219.4 and 239.3 across runs) against Left 4 Dead 2's 200. `--survey-walk` turned the reset pose to yaw 135, the longest clear run at 253 units against 118 at yaw 315.
- **Reset** lands within 0.87 units. `getpos` prints the eye position and `setpos` sets the origin, so a reset teleports the player up and drops it, and the pose settles; the exact pair on Left 4 Dead 2 lands within 0.00.
- The install has two game dirs and they disagree: the game writes `config.cfg` into `hl2_complete/cfg` but `-condebug` writes `hl2/console.log`, and a `+exec` finds a cfg in `hl2/cfg` and not one in `hl2_complete/cfg`. Both checked by putting a marker cfg in each and reading the log. `mod_dir` is `hl2`, which is right for the log and the cfg at once.
- `360controller.cfg` here has no `unbindall`, sets `joy_yawsensitivity -1.25` rather than -1.5, and sets `joy_accelscale 1.4` with no `joy_accelmax` at all.

### 2026-09-07, Half-Life 2, the same map, Nimbus actuator

**13/17**, and the two things it found are worth more than the score. The pad calibration it plans from was written with `--cal-mags 0.4,0.6 --write-calibration`, deliberately leaving out the full-deflection row that folds; with that row gone the pad suite is **13/13**.

The stick path through the real app is sound: a full drag sent the bridge's 0.950 ceiling, a full drag up walked 215.7 units, the release read exactly zero on every axis, and the reset landed within 0.87 units. The full drag's turn is one of the readings this game folds (-242 and -162 deg/s on two runs), so it counts as "the camera moved a lot" and nothing finer.

- **The finding: Nimbus's anti-deadzone floor is below this game's deadzone.** A one-pixel drag sent `RX=+0.289`, which is exactly the floor the bridge should send, and the camera did not move at all (N2). Half-Life 2 does not start turning until 0.30 to 0.40, where the same 0.289 clears Left 4 Dead 2's 0.26 to 0.28 and turns its camera 2.4 degrees a second. So the smallest movement a Nimbus user can make is invisible in this game. Three games now bracket the default from both sides, and the write-up lives in [AIM_ASSISTANCE.md](AIM_ASSISTANCE.md) section 12.3, because it is an argument about the product's default rather than about the harness.
- **A planner bug, found and fixed.** Asked to turn 10 degrees, the Spectator+ planner answered `0.60 held 0.013 s` and the game turned 73. `GameCalibration._plan` walked the magnitudes upward and, when none fitted the hold window, returned the last one tried, the largest. That is right for a request too big for the table and wrong for one too small, where the largest magnitude is the worst possible answer. Left 4 Dead 2 never showed it because its slowest calibrated row is 1.2 degrees; Half-Life 2's is 12.6, so anything finer degenerated. It now takes the smallest magnitude at `min_hold` when that lands within a quarter of what was asked, and otherwise refuses, since a caller can act on "no plan" and cannot act on a turn twice the size it wanted. The clamp is what keeps a walk of 100 units, which needs 0.199 s against a 0.2 s floor, from being failed over one millisecond.

| Primitive | Plan | Result |
|---|---|---|
| turn right 90 | `rx` +0.60 for 0.252 s | -130.3 degrees (tolerance 14) |
| turn left 45 | `rx` -0.40 for 1.246 s | +42.4 degrees (tolerance 7) |
| turn right 10 | refused | no plan: finer than the calibration can command |
| walk 100 units | `y` +1.00 for 0.200 s | 59.8 units |
| stop a 400-unit walk | | 79.8 units before the stop, 0.0 in the next second |

**The misses are repeatable, and they are the calibration not transferring.** The turn of 90 read -132.7, -130.3 and -127.6 on three runs, and the walk of 100 read 59.8 twice to the tenth of a unit, so this is not jitter: the game is deterministic from the same reset pose, and the primitives are consistently long on the turn and short on the walk. What differs is the path. The calibration was measured with the harness's own pad, and the primitives drive the same axes through the bridge, where the same nominal hold does not put the stick on the game for the same length of time. That matters here and not on Left 4 Dead 2 because of how front-loaded this game's response is: 0.60 gives 79 of its 141 degrees in the first tenth of a second, and the walk averages 500 units a second over the first quarter second against 210 over a full one. Where most of the movement happens in the first fraction of the hold, a few milliseconds of difference between the two paths is worth tens of degrees or units; where the response is close to linear, as it is on Left 4 Dead 2 below the stop, it is worth almost nothing. The fix, when it matters, is to write the calibration from the actuator the primitives will use rather than from the pad.

**Regression.** Left 4 Dead 2's Nimbus suite was rerun after the planner change and passed **17/17**, with all three planned turns choosing exactly the plans they chose before (`1.00` for 0.403 s, `0.60` for 1.370 s, `0.40` for 0.741 s) and landing at -84.2, +45.7 and -10.0 degrees against -86.4, +45.7 and -9.9, and the walk at 104.8 units.

**Leave the machine alone while a run is going.** Two runs before that one scored 16/17 and 12/17, with a pose read returning nothing in the first and a 90 degree turn reading +24.9 in the second, and the cause was a person moving the mouse: these games read the mouse for look, the harness puts the game in the foreground for each step, and a hand on the mouse turns the camera inside the window the measurement is taken across. Nothing in the harness distinguishes that from the pad's own input. The runs are unattended in the sense that they need no help, not in the sense that the machine can be used while they run. It is also why `front()` is called before the first pose of a delta and never between the two: it warps the cursor to the middle of the game window, so asserting the foreground mid-measurement would inject exactly the same disturbance. `pose()` now tries three times rather than two, which is worth having anyway.

Left 4 Dead 2's pad suite was also rerun with all three harness changes in and passed **13/13** with the numbers unmoved: 0.28 gave -1.52 deg/s, 0.40 gave -13.79, 0.60 gave -34.20, 0.80 gave -54.61, the walk 199.7 units, the button echo 31 ms, and G12 matched the checked-in calibration, so `src/spectator/calibrations/left4dead2.json` was left alone.

### 2026-09-08, dev machine, PowerWash Simulator, career job `Clean the Back Garden`, borderless fullscreen 2560x1440, both actuators

The fourth game and the second with no console. **Pad 11/11, Nimbus 11/11**, both after one failed run that was entirely about the menus.

**Run 1 (1/2) never got into a job**, and the recipe's own UNVERIFIED note had guessed the right failure for the wrong reason. It reached the career screen and then sat there for 240 s of A presses, because this game's menus are driven by a *virtual pointer*, not by a highlight: A clicks whatever the pointer is over, and over empty space it does nothing at all. The pointer had been left at the middle of the window, in the gap between two cards. A live desktop capture during the run is what showed it; nothing in the log could have.

What an hour of poking at the live menus with a pad established, all of it now in the recipe:

- **The left stick moves the pointer, at about 1570 px/s at full deflection**, measured at 2560x1440 (786 px in 0.5 s, 1257 px in 0.8 s), and it **clamps at the screen edges**. That clamp is the only fixed reference the menu offers, so the new `pointer` step parks in the top left corner and moves one axis at a time by time. Predicted (813, 576) landed (859, 554), inside 50 px, which is nothing against a menu card.
- **The right stick does not move the pointer**, so the readiness stick test is safe to run inside a menu and cannot disturb it.
- **The mouse is not a way in.** `SetCursorPos` moved the OS cursor with the game's pointer staying exactly where the stick had left it, and a synthesized click did nothing. That is the same finding as the cursor relay's, from the other side: this game reads the mouse through Raw Input, which `SetCursorPos` never produces.
- **The last two screens aim themselves.** On the job details screen and on the DEFAULT CONTROLS screen the game snaps its pointer onto RESUME JOB and CONTINUE, so those two are left to `press_until_control`, which stops the moment the stick turns the camera.
- **The launch args were fiction and are gone.** `-screen-fullscreen 0 -screen-width 1280 -screen-height 720` gives a 1280x720 window for a few seconds and then the game applies its own saved preference and goes borderless fullscreen at the desktop resolution. Captures survive it because `grab()` re-reads the client rect every time, but the recipe can no longer claim a deterministic window size, and `place_beside` has nowhere to put the Nimbus window beside a fullscreen game (it goes off screen, which turns out to be harmless: the actuator posts its events to the QML window rather than clicking the screen).

Launch to window 4 s with Steam warm (26 s cold), launch to the world 72 s, of which about 45 s is the menu walk. Frame noise floor 1259 in the garden, well above Elden Ring's 330 to 456, because the washer wand and the foliage never stop moving; moved above 3777, still at or below 2518.

| Step | changed samples | verdict |
|---|---|---|
| control `rx` 1.00 | 54,639 | MOVED |
| 0.20 | 3,119 | INCONCLUSIVE |
| 0.26 | 5,834 | MOVED |
| 0.28 | 9,659 | MOVED |
| 0.30 | 13,132 | MOVED |
| 0.40 | 20,247 | MOVED |
| 0.60 | 30,141 | MOVED |
| 0.80 | 48,741 | MOVED |
| 1.00 | 61,995 | MOVED |
| left 0.60 | 64,773 | MOVED |
| pitch up 0.60 | 70,702 | MOVED |
| walk 1.00 | 51,861 | MOVED |
| idle after | 577 | |

Two things the table says. **The deadzone is below 0.26**, with 0.20 the marginal one, so this game sits with Elden Ring at the low end and nowhere near Half-Life 2's 0.30 to 0.40: four games now and the spread is the whole argument for the per-profile anti-deadzone rather than a constant. And **there is no full-deflection blind spot here**: 1.00 changed the most samples of any magnitude, where Elden Ring's 1.00 changed fewer than its 0.80 because the camera came most of the way round inside the second. A slow camera is easier to measure by frame differencing than a fast one.

The Nimbus run repeated all of it through the real app on the throwaway `nimbus_harness` profile, including the menu walk, which the app's own left-stick widget drove at its 0.95 ceiling without the aimed clicks missing. Ready in 75 s, idle floor 1202. The full drag sent RX +0.950 for 53,496 samples, the left stick +0.950 for 42,901, the release read zero on every axis, and **the 0.289 anti-deadzone floor cleared this game's threshold**, 10,516 samples MOVED, where the same floor sits *under* Half-Life 2's own deadzone and moves nothing. The button check and the primitives skip themselves for want of a console, as they do on Elden Ring, so 11 checks rather than 17. `controller_config.json` came back byte for byte and the throwaway profile was removed.

### 2026-09-09, dev machine, Halo Wars: Definitive Edition, skirmish on Chasms (1v1) against an AI, borderless fullscreen 2560x1440, both actuators

The fifth game, the third with no console, and the first real-time strategy title. **Pad 11/11 on the first run, Nimbus 11/11 on the second**, the first Nimbus run having found a real harness defect rather than anything about the game.

**An RTS fits the harness better than it has any right to.** There is no player and no view, so the checks measure a camera; the game's own CONTROL DIAGRAM screen names the mapping and the probe confirmed every line of it. The left stick is CAMERA and scrolls the map with the cursor staying fixed near the middle of the screen, the right stick is ROTATE CAMERA with `rx` swinging around the map point under the camera and `ry` zooming. That means the three yaw checks (G4, G5, G6) drive rotation, which does not translate the camera, so they measure the same scene every time and the run barely drifts: only G8's second of left stick moves the camera off the base at all. LT is FAST SCROLL, a modifier for the left stick and not an action, which is why holding it alone changed 16 samples of 102,480 and reads as a dead trigger.

Launch to window 4 s, launch to the world 83 s, of which 71 s is the recipe's timed menu walk. **The quietest scene in the harness**: two captures 28 s apart with no input differed by 676 samples, and the idle floor read 802 and 87 on the two runs, against PowerWash Simulator's 1259 and Elden Ring's 330 to 456. Nothing in an RTS base animates much.

| `rx` | changed samples | verdict |
|---|---|---|
| 0.20 | 1,230 | STILL |
| 0.26 | 1,188 | STILL |
| 0.28 | 1,156 | STILL |
| 0.30 | 1,196 | STILL |
| 0.40 | 1,208 | STILL |
| 0.60 | 53,441 | MOVED |
| 0.80 | 66,268 | MOVED |
| 1.00 | 62,641 | MOVED |
| left 0.60 | 52,260 | MOVED |
| zoom `ry` 0.60 | 53,225 | MOVED |
| scroll `ly` 1.00 | 72,876 | MOVED |
| idle after | 0 | |

**The threshold is between 0.40 and 0.60, the highest of the five games, and the step across it is a cliff**: five magnitudes sit flat at about 1,200 changed samples and the sixth jumps to 53,441. The five games now run 0.20 or below (Elden Ring), below 0.26 (PowerWash Simulator), 0.26 to 0.28 (Left 4 Dead 2), 0.30 to 0.40 (Half-Life 2) and 0.40 to 0.60 here. **Nimbus's 0.289 anti-deadzone floor is far below this one**, so the smallest movement a user can make does nothing at all in this game, which is the Half-Life 2 finding again and worse. Whether that is Halo Wars' stick deadzone or the point where its camera rotation starts is not separable by frame differencing.

**The first Nimbus run scored 8/11 and the three failures were the harness's.** N1 reported that a full drag sent the bridge's 0.950 ceiling and the camera did not move, and the printed diagnosis blamed the pad. The saved frames said otherwise: the game was sitting in the CAMPAIGN submenu the whole time. The bundled profile draws widgets for buttons 1 to 6, the recipe's menu walk needs d-pad down and Start, and `NimbusActuator.apply` dropped any button with no widget in silence, so A was the only press that landed: it cleared the title screen and then opened CAMPAIGN instead of SKIRMISH. Two fixes followed, both in section 4.4 and section 7: a button with no widget is now pressed at the bridge and the run says so on the line, and the diagnosis now names the menu as the other possibility. With them the second run reached the match in 79 s and passed 11/11.

| Step | Bridge sent | Game |
|---|---|---|
| full drag on the aim stick | RX +0.950 | 69,837 samples MOVED |
| 1 px drag on the aim stick | RX +0.289 | 1,407 samples, **called MOVED and it did not move** |
| release | every axis 0.0 | picture still |
| full drag up on the left stick | LY +0.950 | 76,624 samples MOVED, the camera scrolled |

**N2's pass is a false positive, and it is the most useful thing in this run.** The bridge sent exactly the 0.289 floor it should have, and the check called it MOVED on 1,407 changed samples because that run's idle second happened to be exceptionally quiet: a floor of 87 put the MOVED threshold at 261, where the pad run's floor of 802 put it at 2,406 and called the same size of change STILL. Where the pixels changed settles it. A 4x4 grid of the changed fraction has the full drag at 43 to 80 percent in every cell, a whole picture turning, while the 1 px drag leaves the top row at 0.0 to 0.1 percent and scatters 5 percent at most through the bottom cells, which is the unit panel and the ground beside the base animating. The pad run's 0.40 step, correctly called STILL, has the same signature to within a decimal: top row 0.0, bottom cells 1.7 to 5.6. So the two runs agree about the game and disagree only in their verdicts, and the disagreement is a one-second noise floor. The conclusion to carry is the physical one: **at 0.289 this game's camera does not rotate**, and the harness reported a pass anyway.

What the game taught about menus. They are highlight-driven, so this recipe needs none of PowerWash Simulator's pointer machinery, but **they wrap**, so there is no walking to a known end and counting from it: four presses of down from the pause menu's CONTROL DIAGRAM went past the bottom and landed on OPTIONS. The sequence therefore trusts a cold launch to open with CAMPAIGN highlighted, which both runs confirmed. And **the readiness stick test cannot be used here at all**, which is why this recipe is fixed waits where Elden Ring's and PowerWash Simulator's are `press_until_control`: the title screen plays a full-screen attract reel, so a stick test against the picture reports MOVED on the title screen. A game with animated menus needs a timed sequence, and the timings carry about a threefold margin.

**There is a real reset here and the harness cannot use it.** d-pad left jumps the camera to the base and right-stick click returns the rotation to north, and the pair restores the opening view from anywhere on the map (40 percent of samples differing from a stored base view, against 71 percent for a lost camera). `reset()` is a no-op without a console and there is no pose to check a landing against, so the run drifts exactly as Elden Ring's does. This is the concrete case for a reference-frame reset on console-less games, and with it G2 would become a real check for three of the five games.

One game setting scales everything measured here: OPTIONS > CONTROLS > Scroll Speed, about three quarters of the way to Fast on this install, with Camera Rotation, Default Zoom and Crosshair Stickiness beside it. Input Device reads Gamepad once a pad exists at launch, which is the launch-order rule showing up in the game's own UI.

### 2026-09-09, dev machine, the rebaselining pass under the motion oracle: every game, most on both actuators

The testing strategy ([TESTING_STRATEGY.md](TESTING_STRATEGY.md)) was built the same afternoon it was written, and this pass reran the games under it so that every frame number in this document from here on comes from one method. What changed between the entries above and this one: frame verdicts come from `tests/frame_motion.py` (a believed shift, a wholesale or spread change, a confident origin peak) instead of a changed-sample count; the noise floor and the shift threshold come from three idle seconds instead of one; the three console-less recipes carry a `window` key; Halo Wars carries `reset_buttons` and a `rotate` motion kind; and the console recipes carry `expect` bands seeded from their 2026-09-07 runs. None of the changed counts below is comparable with the tables above where the window took, because a 1280x720 capture has 25,600 samples against 102,480.

**The window key, per game.** Halo Wars took 1280x720 borderless in one call at launch (from 2560x1440) and held it through the menu walk and the match; on the next launch it came up framed at 1280x720 by itself, at (8,31), and the harness made it borderless at the origin. Elden Ring took it at launch, undid it while loading (the readiness stick test still counted 102,480 samples) and took it again when the harness re-applied it at readiness, which is the case the plan's "apply again after wait_ready" was written for. PowerWash Simulator refused it at all three stages: `SetWindowPos` lands and the client rect reads 2560x1440 again a moment later, the Unity saved preference re-asserting itself, so that recipe runs full screen and says so. Two of three, recorded rather than fixed, and the Nimbus window now has somewhere to sit beside two of the games.

**Halo Wars, pad, 11/11.** Three idle seconds changed 14, 10 and 234 samples, so the floor is 234, and the third sample is exactly what a single second would have missed or over-read. G2 is a real check now: a second of left stick scrolled the camera off (19,166 changed, 15 of 16 cells, MOVED), the two reset buttons brought it back (285 changed, static peak 0.97, STILL). The sweep at 1280x720:

| `rx` | changed samples | motion | verdict |
|---|---|---|---|
| 0.20 to 0.40 | 218 to 289 | static 0.94 to 0.98, no believed peak, 0 cells | STILL |
| 0.60 | 16,421 | 15 of 16 cells | MOVED |
| 0.80 | 16,935 | 15 of 16 cells | MOVED |
| 1.00 | 19,559 | 16 of 16 cells | MOVED |
| left 0.60 | 16,492 | 15 of 16 cells | MOVED |
| zoom `ry` 0.60 | 15,651 | 15 of 16 cells | MOVED |
| scroll `ly` 1.00 | 19,185 | 15 of 16 cells | MOVED |

The cliff between 0.40 and 0.60 is where it was. The rotation estimate believed nothing on any of these (peaks 0.01 to 0.05 against a floor of 0.05): a one-second swing at 0.60 takes the camera past the overlap, and the verdicts above are the whole-picture rule's. A degrees table for this game needs shorter holds.

**Halo Wars, Nimbus, 11/12, and the one failure is the point.** The reset landed at static 0.99 (62 changed samples) after a scroll that changed all 16 cells; the idle floor was 391 over three seconds; the full drag changed all 16 cells and the left stick likewise. The one-pixel drag sent the bridge's 0.289 floor and changed 376 samples with the static peak at 0.95 and no cell past the threshold: STILL, and N2 failed, where the 2026-09-09 run above passed it on 1,407 changed samples against a lucky floor of 87. The physical conclusion stands and the harness now agrees with it. Since a check that can never pass tests nothing, the recipe gained `floor_moves_camera: false` (section 4.1), under which N2 checks that the floor was sent and the camera stayed still; the rerun under that rule is below.

**PowerWash Simulator, pad, 11/11, full screen.** Idle changed 2,285, 2,372 and 1,368; floor 2,372; the shift threshold rose to 13 px because the idle picture carries a coherent 6.5 px bob (the wand). The sweep repeats the 2026-09-08 frames to the pixel where a shift was found at all: 0.20 STILL (3,559 changed, a 5 px bob, no cell past the threshold); 0.26 MOVED on a horizontal shift of 16 px at peak 0.13 (the saved frames had 16); 0.28 MOVED on 48 px (they had 50); 0.30 MOVED by spread, six cells, with the world's 79 px shift as the second peak behind the bob; 0.40 nine cells; 0.60 ten; 0.80 and 1.00 fifteen and sixteen. So the threshold is below 0.26 as before, and now with a rate: 16 px/s at 0.26 and 48 px/s at 0.28 leftward, banded into the recipe at those two magnitudes (`--expect-mags 0.26,0.28` is the setting that fits this game, because from 0.30 the world outruns what the correlation can pin behind the wand).

**Elden Ring, pad, 11/11, at 1280x720.** Idle changed 79, 64 and 77; floor 79; threshold 8 px. The sweep reads differently from the full-screen entries above, and the difference is the method rather than the game: 0.20 STILL (58 changed); 0.26 STILL on a coherent 4.9 px shift at peak 0.24; 0.28 STILL on 4 px; 0.30 MOVED on 8 px; 0.40 MOVED on a 28 px peak; 0.60 six cells; 0.80 twelve; 1.00 seven cells and 8,846 changed (the blind spot again: the camera came most of the way round). At this width 8 px is about 0.6 degrees, close to the 1 degree the console games count as a turn, so "moved from 0.30, a fraction of a degree per second below it" is the reading a console would have given, where the old count against a full-screen capture had put the threshold at or below 0.20. Under an empty sky a yaw changes the lower rows only, which is why the walk (2,610 changed, four cells) and the pitch (4,114, five) are MOVED by the spread rule and not the wholesale one. No bands were written: the 0.30 shift sits on the floor and the 0.40 peak is the character's bob, not a rate to hold a run to.

**Left 4 Dead 2, pad, 17/17 twice: the regression test works.** The four bands seeded from the 2026-09-07 run held two days later, and held again on a rerun under the final module an hour after that: 0.40 measured -13.5 then -13.8 deg/s against -13.8 (within 2.1), 0.60 -33.7 then -34.4 against -34.2 (within 5.1), pitch -21.3 then -20.9 against -21.3 (within 3.2), walk 199.7 both times against 199.7 (within 39.9). Those four `G5e`, `G7e` and `G8e` lines are what the plan meant by a run that answers "did anything change since last time". The frame side behaved as section 7 says it does in this scene, both times: the idle floor was 1,118 and then 514 with a coherent 28 px peak in it each time, so the shift threshold was 56 and then 55 px; the frame verdict called 0.20 MOVED on a 40 by 64 px peak the flashlight makes as it sways (its offset differs from one idle second to the next, so the idle-offset exclusion caught it in the offline re-derivation of the first run and not in the second), called 0.26 STILL, and called 0.28 MOVED on a 13 px peak the console put at 1.5 degrees, behind the flashlight's. Recorded beside the console, never trusted over it.

**Half-Life 2, pad, 16/17.** The one failure is the one the recipe documents: G12's full-deflection row folds past 180 degrees between pose samples (+80.2 at a tenth of a second, -167.7 at a second) and the table is only buildable with `--cal-mags 0.4,0.6`, which this pass did not pass. The bands held, one of them narrowly: 0.40 measured -50.0 deg/s against -48.2 (within 7.2), 0.60 -202.1 against -178.9 (within 26.8, and 13 percent off, so the 15 percent band on that magnitude is doing real work: the 2026-09-07 runs put it at -178.9, -179.0 and -177.8, and this one sits outside that spread), pitch -89.0 against -89.0 (the clamp, not a rate, but a stable number), walk 219.4 units/s against 216.6. The deadzone read 0.30 still and 0.40 moved, as before. The frame verdicts agreed with the console at every magnitude, which they never quite managed in the Left 4 Dead 2 scene: Black Mesa East has no bots and no flashlight, its idle floor was 1,455 with no coherent peak worth believing, and every turn from 0.40 changed all 16 cells.

**Elden Ring, Nimbus, 11/11, at 1280x720.** The window took at readiness as on the pad run. Idle changed 119, 97 and 85; floor 119; threshold 11 px, because the idle picture carries a coherent 5 px bob of its own (the character breathing). The full drag sent 0.950 and changed eight cells (10,931 samples); the one-pixel drag sent the 0.289 floor and shifted the picture 146 px leftward in the second (peak 0.09, eight cells), which at this width is about ten degrees, so N2 is a pass with a number behind it and a long way from any ripple; the left stick sent 0.950 and changed six cells. The frame verdicts came out the same when re-derived under the final module, whose idle-offset exclusion swallowed the nine texture offsets that scene's idle frames correlate at.

**Halo Wars, Nimbus, rerun, 12/12.** Under `floor_moves_camera: false`. The window came up framed at 1280x720 and was made borderless at the origin in 2 s. The reset landed on 3 changed samples after a scroll that changed 15 cells; the idle floor was 312 over three seconds; the one-pixel drag sent 0.289 and changed 360 samples with the static peak at 0.96 and no cell past the threshold, STILL, which is now what the check asks for; the full drag and the left stick changed 15 cells each. Beside the 11/11 of the morning, which reported N2 as a pass it was not, this is the same game measured honestly.

**PowerWash Simulator, Nimbus, 10/11 then 11/11, full screen.** The first run, under the module as it stood before the merge, failed N2: the one-pixel drag sent the 0.289 floor and changed 9,956 samples, about what the pad's 0.28 step changes, but the only believed peak was the wand's 9 px bob, and the world's shift was in the surface as two halves of 0.035 either side of the zero row, at 63 px, each under the floor. That pair of frames is where the peak merging in section 4.3 came from. The rerun under the final module passed 11/11: the same drag, 9,711 changed samples, a merged peak of 0.08 at 58 px leftward in the second, between the pad's 48 px at 0.28 and 79 px at 0.30, which is where 0.289 belongs. So the 2026-09-08 conclusion stands, the floor clears this game's threshold, and it now stands on a measured shift rather than a changed count; the rate is banded as this recipe's N2. The idle floor was 2,409 over three seconds, the full drag changed 13 cells, the left stick 11.

---

### 2026-09-09, dev machine, Arma 3 (BattlEye on and off), the generated VR mission, 1280x720 borderless, both actuators

The BattlEye title of `PAD_BUS_FORK_PLAN.md` section 13, and the first game with a pose oracle that is not a Source console: the `arma3` oracle of section 4.3, built and measured this afternoon on the pure-Python pad client that replaced vgamepad the same day. Two recipes, `arma3` (launched by `arma3battleye.exe`, BattlEye's service running, `-beservice` on the game's command line) and `arma3_nobe` (the game executable directly), the same mission and oracle. Runs: `arma3` pad 13/13, `arma3` Nimbus 17/17 including the five Spectator+ primitives from the calibration the pad run wrote, `arma3_nobe` pad 13/13. Bands were written from the pad runs and a Nimbus run afterwards.

| Measure | BattlEye | No BattlEye |
|---|---|---|
| Window found, ready | 22 s, 32 s | 8 s, 18 s |
| Right stick 1.00 | +305.0 deg/s | +305.2 deg/s |
| Right stick 0.60 | +36.66 deg/s | +36.67 deg/s |
| Left at 0.60 | -36.67, ratio 1.00 | -36.67, ratio 1.00 |
| Deadzone (sweep) | still at 0.30, moved at 0.40 | the same |
| Pitch, 0.60 held | +24.9 deg | +25.4 deg |
| Walk, full stick | 5.1 m/s (0.95, 2.37, 4.91 m at 0.25, 0.5, 1 s) | 5.1 m/s (1.05, 2.46, 4.91) |
| Button A | `Action` in 32 ms | 32 ms |
| 1280x720 borderless at readiness | took | took |

Nimbus on the BattlEye recipe: full drag +240.3 deg/s at the bridge's 0.95 ceiling (the game's response is steeper past 0.95, which is why the pad's full stick reads 305), the 1 px drag sends the 0.289 floor and the camera stays still (`floor_moves_camera` false: the game's threshold is above the floor, like Half-Life 2 and Halo Wars), the A widget echoes `Action` in 47 ms, a full drag up walks 3.9 m in the second, and the primitives: turn right 90 got 88.4, turn left 45 got -45.5, turn right 10 got 10.1, walk 100 m got 99.3 in 19.7 s, and a cut-short walk stopped in 1.4 m.

So under BattlEye a stock ViGEmBus pad is indistinguishable from no BattlEye, in single player, on every number the harness measures: the anti-cheat neither blocks the virtual pad nor changes what it does. That is the baseline a fork of the bus has to match, and it says nothing yet about a server join, where BattlEye's kicks happen; that remains a manual step. What the day cost, all recorded in section 4.3 and the recipe keys: the pad is disabled in the profile until enabled (a scheme name from the game's config), the profile's start-up window is not the game window, a resize while loading hangs the game, Start freezes the mission's script, `eyeDirection` does not follow the aim's pitch, nothing in SQF sets that pitch (so the actuator levels it), and a joystick entry with a missing `mode` (a hand edit's doing) brings up a modal box that stalled one run for three minutes. The clipboard channel is the one part that needs the machine left alone in a new way: a copy during a run interrupts it for a read, and the tests take the foreground and press Escape, so the "leave the mouse and keyboard alone" rule covers copy and paste here too.

### 2026-09-14, dev machine, Left 4 Dead 2, `c1m2_streets` safe room, windowed 1280x720, pad then netpad actuator

The first real game through the network pad (`--actuator netpad`, section 4.4), run back to back with the direct pad as the same-day baseline: Hyper-V has been on since 2026-09-13, so the older rows are not the comparison. Pad 17/17, netpad 22/22, every expect band met by both.

| Measure | pad | netpad |
|---|---|---|
| Window found, ready | 16 s, 44 s | 2 s, 28 s (a warm start; a new window, the first run had killed the game) |
| Deadzone (sweep) | still at 0.26, moved at 0.28 | the same |
| Right stick 0.28, 0.30 | -1.49, -3.51 deg/s | -1.50, -3.53 deg/s |
| Right stick 0.40 (band -13.8 within 2.1) | -13.74 | -13.70 |
| Right stick 0.60 (band -34.2 within 5.1) | -33.72 | -34.36 |
| Right stick 0.80 | -54.74 | -54.61 |
| Right stick 1.00 (the acceleration ramp) | -474.0 | -498.9 |
| Left at 0.60, ratio to right | +33.66, 1.00 | +33.71, 0.98 |
| Pitch, 0.60 held (band -21.3 within 3.2) | -20.92 | -21.21 |
| Walk, full stick 1 s (band 199.7 within 39.9) | 200.0 | 199.7 |
| LB echo | 31 ms | 31 ms |
| First pose sample that moved (a bound) | 125 ms | 78 ms |
| G12 yaw at 0.60 for 0.10, 0.25, 0.50, 1.00 s | -3.2, -8.4, -16.4, -33.2 | -3.0, -8.2, -16.2, -33.2 |
| G12 yaw at 1.00 | -8.3, -37.4, -123.2, -443.4 | -8.4, -38.1, -127.2, -439.1 |
| G12 walk at 0.25, 0.5, 1.0 s | 62.9, 112.9, 199.7 | 54.3, 120.1, 200.0 |

The two latency bounds are one pose sample apart (each read is about 47 ms), not netpad being faster, and the full-deflection and short-walk rows vary as much between two pad runs. On everything the game measures, the network pad is the direct pad.

Netpad's own checks: **NP1** one session for the whole run, 3,039 packets, no timeout, failed pad write or rejected packet, and the sender's loop thread stalled past 50 ms once (63 ms, under the 150 ms watchdog) while the harness grabbed frames. **NP2** turning at 0.60 (-16.5 degrees in 0.6 s), then the loop frozen: +0.00 degrees over 0.6 s measured from 0.35 s after the freeze, one receiver timeout, and -22.4 degrees in 0.8 s once the loop resumed. **NP3** explicit Stop mid-turn: +0.00 degrees, a new session live within a second, -23.5 degrees after. **NP4, the finding:** a 2.6 s freeze destroyed the pad and the resumed session re-created it, and **Left 4 Dead 2 did not read the re-created pad** (0.0 degrees in 1 s at 0.60). The launch-order rule of section 4.2 applies to a pad that comes back as much as to one that arrives late, so on a Source game any interruption past the 2 s destroy leaves the player without a controller until the game restarts. Receiver totals at the end: 3,593 packets, 4 sessions (the run, NP2's resume, NP3's new session, NP4's re-creation), 2 timeouts, 1 end.

## Related Documents

- [Testing Strategy](TESTING_STRATEGY.md): what is weak about testing across the project and in what order to fix it. Section 7's open questions are its sections 4.3 to 4.6, with costs and an order of work.
- [Aim Assistance](AIM_ASSISTANCE.md): sections 12.3 and 14, the frame-differencing measurement this harness replaces and the numbers it has to reproduce.
- [Windows Mouse Filter Plan](WINDOWS_MOUSE_FILTER_PLAN.md): section 5, the probe style and the results-log convention.
- [Host Mode & Input Isolation](HOST_MODE_ISOLATION.md): section 8, the measurements on what Left 4 Dead 2 and Elden Ring read.
- [Research Platform](RESEARCH_PLATFORM.md): Spectator+ effectiveness as a research question.
