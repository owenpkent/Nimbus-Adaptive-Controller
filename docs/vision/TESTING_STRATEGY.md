# Testing Strategy: From Liveness Checks to Regression Tests

> **Status:** planned 2026-09-09 and built the same day, sections 4.1 to 4.6 all of it. Written after adding Halo Wars: Definitive Edition to the game harness ([GAME_TEST_HARNESS.md](GAME_TEST_HARNESS.md) section 8), which passed 11/11 on both actuators while containing one verdict that was measurably false. Sections 1 to 7 are the plan as it was written, kept because the reasoning is the record; section 8 says what was built, what the saved frames changed about the design before a game was launched, and what the rerun of every game found. The false verdict now reads STILL, the fast suite runs in one command and in CI, and Left 4 Dead 2 and Half-Life 2 hold their measured rates to a band across runs. Later the same day Arma 3 joined as a sixth game, the BattlEye title, with a pose oracle of its own (its scripting, over the clipboard) and bands of its own; "five games" below is the count at the time of writing. On 2026-09-13 the suite reached seventeen files, four of them from the target-assist work ([TARGET_AWARE_AIM_PLAN.md](TARGET_AWARE_AIM_PLAN.md)): the policy's refusals, the servo against a simulated plant, the target source and its selector, and the closed-loop primitive driving a simulated game on a real Qt timer. The last of those is the first file in the suite whose checks are about *timing*, and it failed in CI the first time for the classic reason: it judged a snap by the wall clock around it rather than by the loop's own trace, and a shared runner is slower than this machine.

---

## 1. Summary

Nimbus has two kinds of test and a gap between them.

- **Fast tests** in `tests/test_*.py`: pure Python, no hardware, seconds to run. There are eight of them and they all pass. Almost nobody can run them, because running one the obvious way fails on an import error (section 2.1).
- **Real-game tests** in `tests/probe_game_harness_windows.py`: four minutes each, need Steam, a driver and a GPU. They prove the pipeline reaches a real game. They do not check that what reaches it is *correct* (section 2.2).

The gap is regression coverage. A change that halves stick sensitivity, inverts a curve, or drops the anti-deadzone would pass every check in both suites today. The fast suite would pass because it does not exercise the widget-to-axis path end to end; the game suite would pass because its rules are "more than 10 degrees" and "the picture changed".

Six changes close it, in increasing order of cost. The first is half an hour and turns eight dormant files into a CI net. The middle two are what convert the game runs from liveness checks into regression tests. The last three remove known sources of false results.

---

## 2. Where testing stands today

### 2.1 The fast suite exists and is dark

Eight files under `tests/` are pure-Python checks with their own pass counting. Run as a path, seven of the eight fail before they execute a line:

```
> venv\Scripts\python tests\test_bridge_services.py
ModuleNotFoundError: No module named 'src'
```

Six fail on `src` and one (`test_application_startup.py`) on `tests`, because the modules are package-qualified and the repo root is not on `sys.path` when a file is run by path. Run as modules from the repo root, **all eight pass**:

```
> venv\Scripts\python -m tests.test_bridge_services
OK
```

| File | Run as a path | Run as a module |
|---|---|---|
| `test_application_services.py` | `No module named 'src'` | OK |
| `test_application_startup.py` | `No module named 'tests'` | OK |
| `test_bridge_services.py` | `No module named 'src'` | OK |
| `test_controller_output.py` | `No module named 'src'` | OK |
| `test_profile_repository.py` | `No module named 'src'` | OK |
| `test_stick_shaping.py` | OK, 27/27 | OK, 27/27 |
| `test_telemetry_privacy.py` | `No module named 'src'` | OK |
| `test_updater_requests.py` | `No module named 'src'` | OK |

So the code is healthy and the invocation is the whole problem. `CLAUDE.md` currently says these "use stale imports", which is what stops anyone looking again. Of the eight, only `test_vjoy.py` (not in the list, and not one of the eight) needs a driver; three construct a `QApplication` and so need `QT_QPA_PLATFORM=offscreen` on a headless runner. Nothing else in the set touches hardware, because the Windows and driver modules are already imported behind `*_AVAILABLE` flags.

### 2.2 The game checks assert liveness, not correctness

The pass rules, as they stand:

| Check | Rule | Passes when |
|---|---|---|
| G4 yaw control | `abs(d_yaw) > 10` degrees | the camera turns 12 degrees, or 460 |
| G5 sweep, console | some magnitude moved | any threshold at all is found |
| G5 sweep, frame diff | some magnitude was MOVED | the picture changed at some magnitude |
| G8 move | `d_horiz > 20` units | the player moves at all |
| N1 full drag | more than 10 degrees, bridge sent its ceiling | the ceiling is right and something happened |
| N2 one-pixel drag | more than 1 degree, bridge sent its floor | the floor is right and something happened |

Every one of these is a connectivity test. The bridge-side halves (the ceiling and floor assertions) are real and come from the bridge's own resolved parameters, which is right, but the game-side halves have no expected value to compare against. Left 4 Dead 2's true numbers already exist in `src/spectator/calibrations/left4dead2.json` (0.40 gives 13.6 deg/s, 0.60 gives 33) and no check reads them.

### 2.3 On games with no console the verdict is binary, and sometimes wrong

Three of the five games (Elden Ring, PowerWash Simulator, Halo Wars) have no console, so every verdict is MOVED or STILL from a count of changed pixel samples against a noise floor measured in one idle second. Halo Wars showed two ways that fails.

**The floor is sampled by luck.** The same scene measured 802 on the pad run and 87 on the Nimbus run, moving the MOVED threshold from 2406 to 261. A step that changed about 1,300 samples in both runs was therefore called STILL in one and MOVED in the other.

**A count cannot tell a camera from a HUD.** The MOVED one was the wrong one. A 4x4 grid of the changed fraction separates them without ambiguity:

| Step | Verdict | Top row of the grid | Rest |
|---|---|---|---|
| N1, full drag, RX 0.950 | MOVED, correct | 70.2, 65.6, 67.2, 42.9 | 42 to 80 percent everywhere |
| N2, 1 px drag, RX 0.289 | MOVED, **false** | 0.1, 0.0, 0.0, 0.0 | up to 5.8 percent, bottom half only |
| pad, RX 0.40 | STILL, correct | 0.0, 0.0, 0.0, 0.0 | 1.7 to 5.6 percent, bottom half only |

A camera turn moves the whole picture. The 0.289 drag left the top of the screen untouched and animated the unit panel and the ground beside the base, which is what the pad run's 0.40 step (correctly called STILL) also did. The two runs agree about the game and disagree only in their verdicts.

The consequence reaches beyond the harness: N2 is the check that decides whether Nimbus's anti-deadzone floor clears a game's threshold, and it reported that it did when it did not ([AIM_ASSISTANCE.md](AIM_ASSISTANCE.md) section 12.3).

### 2.4 Fullscreen games cost more than they look

Three of the five run borderless fullscreen at the desktop resolution, 2560x1440 here.

- **The app under test is invisible.** `place_beside` has nowhere to put the Nimbus window, so it goes off screen. A Nimbus run cannot be watched, and a failure cannot be seen.
- **Captures are expensive.** Every `grab` copies a full screen and every saved frame is 3 to 6 MB, about thirty per run.
- **The window rect is not stable.** The two Halo Wars Nimbus runs disagreed about the game's own window: the one that looked at 2 s found `(8,31) 2560x1421`, a normal window with a title bar, and the one that looked at 4 s found `(0,0) 2560x1440`, borderless fullscreen. The harness polls every 2 s and takes the first match, so which it sees depends on when it happens to look.

Left 4 Dead 2 and Half-Life 2 avoid all of this with `-windowed -noborder -w 1280 -h 720` on the launch line. The other three have no such argument. PowerWash Simulator was measured ignoring its own, applying a saved preference instead, and Halo Wars keeps no settings file that could be edited: not in `%APPDATA%`, not in `%LOCALAPPDATA%`, not under `HKCU\Software\Microsoft\Halo Wars`, not in the game directory, not in Steam's `userdata`.

---

## 3. Options considered

| # | Option | Cost | Decision |
|---|---|---|---|
| 1 | A runner for the eight fast tests, plus CI | ~30 min | **Do first.** Largest ratio in the document |
| 2 | Force games into a window with the app's own `borderless` module | ~1 h | **Do.** Answers 2.4 and makes every later run cheaper |
| 3 | Measure motion (a pixel shift) instead of counting changed samples | ~2 h | **Do.** The only fix that makes 2.3 go away rather than get papered over |
| 4 | Expected values with tolerance bands in the recipes | ~1 h | **Do,** after 3 |
| 5 | A reset for console-less games, checked against a reference frame | ~1 h | Do, best served by 3 |
| 6 | A noise floor from several idle samples | ~20 min | Do, folded into 3's rebaselining |
| 7 | Per-game settings-file patching for windowed mode | ~1 h per game | Rejected: does not generalise, and Halo Wars has no such file |
| 8 | A vision-language model as judge for console-less games | API key, seconds per verdict | Not now. Option 3 gives numbers, which a judge does not |
| 9 | Memory reading or hooks for ground truth | Cheat Engine style tooling | Rejected in the harness plan and still rejected: wrong side of the line for an accessibility project |

---

## 4. Design

### 4.1 A runner for the fast tests, and CI

`tests/run_fast_tests.py`: discover `tests/test_*.py`, skip the two that match the pattern and are not tests (`test_vjoy.py`, a vJoy driver diagnostic; `test_dialog.py`, which turned out to be an interactive pygame loop for the legacy shell whose imports no longer resolve, so it is skipped for good), run each as `python -m tests.<name>` in a subprocess from the repo root, print one line per file and a total, exit non-zero on any failure. A subprocess per file rather than importing them all keeps one file's `QApplication` or `sys.exit` from taking the runner with it. Built 2026-09-09: nine files run (the eight above plus `test_frame_motion.py` from section 4.3), all pass, in about four seconds, and they pass again with `vgamepad` hidden from the interpreter, which is what a CI runner sees; the first GitHub run agreed, 9/9 in 58 seconds.

CI is a `windows-latest` GitHub Actions job (`.github/workflows/fast-tests.yml`): install `requirements.txt` less `pyinstaller`, set `QT_QPA_PLATFORM=offscreen`, run the runner. Until 2026-09-09 it also filtered out `vgamepad`, on purpose: that package shipped as an sdist only, and its `setup.py` ran the ViGEmBus MSI installer when the driver was missing, which a CI runner must not attempt. The pure-Python bus client that replaced it (`src/padbus_client.py`, phase 0 of `PAD_BUS_FORK_PLAN.md`) touches no driver at install time, so the filter went with the package, and `tests/test_padbus_client.py` joined the suite as its tenth file. The game harness stays out of CI permanently: it needs Steam, a signed-in account, a GPU and a virtual pad driver. The split to state plainly is that **CI covers the fast suite and a person runs the game suite**, which is the same split the driver probes already have.

`CLAUDE.md` had its claim corrected in the same change: the tests are not stale, they are run as modules, and the runner does that.

### 4.2 A `window` key in recipes

```json
"window": {"w": 1280, "h": 720, "x": 0, "y": 0, "borderless": true}
```

Applied in `GameEnv.launch()` once the window is found, and again after `wait_ready()`, because a game re-asserts its own mode while it loads. Implemented with `src/borderless.py`, which already has `resize_window`, `make_borderless`, `restore_window` with the original style and rect saved, and `release_clip_cursor` for games that clip the pointer. `close()` restores.

Two things this buys beyond the obvious. Captures at 1280x720 are about a ninth of the pixels, so steps run faster and the frames directory stops growing in megabytes. And it **exercises the app's own window management**, which today has no test at all, against five real games.

It has to verify rather than assume: after applying, re-read the client rect and record whether it took. A game in exclusive fullscreen will ignore `SetWindowPos`, and that is a property of the game worth recording in its recipe, not a run failure. Built as designed, with one addition: the window is looked up again before each application, because a game may replace it while loading, and applied at most once per stage (launch, before the ready sequence, at readiness) and only when the client rect is not already the one asked for. Whether the three fullscreen games accept it was the open question in section 6; section 8 has the answer per game.

### 4.3 Motion instead of a changed-sample count

Replace the binary verdict with a measured global shift, from phase correlation:

1. Grayscale and downsample both frames, apply a Hann window.
2. Cross-power spectrum of the two FFTs, normalised.
3. Inverse FFT; the peak's offset is the translation in pixels, and the peak height against the mean is the confidence.

Numpy only, no new dependency, a few lines. Each step then records `d_px_x`, `d_px_y` and `motion_conf` beside the existing `changed`, and MOVED becomes "a coherent shift above N pixels", not "many pixels differ".

This is what makes 2.3 go away rather than get a bigger threshold. A HUD animation is not a global translation, so it produces no coherent peak and low confidence, whatever its size. The N2 false positive would have been caught with no tuning.

What it unlocks is larger than what it fixes. A pixels-per-second figure per magnitude is a **calibration table for a game with no console**, which is the missing piece for running Spectator+ primitives outside Source games (`GAME_TEST_HARNESS.md` section 4.7 lists exactly this as the limit). It also gives a direction, so `turn_right_sign` becomes checkable without a console.

A rotating camera is the awkward case, and Halo Wars is the game that has one: `rx` there swings the camera rather than translating it, so a translation model fits badly. A log-polar transform before the correlation recovers rotation and scale instead, selected per recipe (`"motion": {"kind": "rotate"}`) since it costs more and suits few games. Zoom (`ry` on Halo Wars) is the scale term of the same transform.

### 4.4 Expected values and tolerance bands

```json
"expect": {
    "G5": {"0.40": {"deg_per_s": -13.6, "tol_pct": 15},
           "0.60": {"deg_per_s": -33.0, "tol_pct": 15}},
    "G8": {"units_per_s": 200, "tol_pct": 20}
}
```

The runner compares and fails outside the band, and `--write-expect` fills the block from a good run exactly as `--write-calibration` already does for the primitives. Console games can be banded today, from numbers that already exist in `src/spectator/calibrations/`. Console-less games need 4.3 first, and then band `px_per_s` in the same shape.

This is the change that makes the game runs answer "did anything change since last time", which is the question a test is for. It also makes the suite fail loudly on the thing it is really guarding: the shaping chain in the bridge.

### 4.5 A reset for console-less games

```json
"reset_buttons": [13, 10]
```

`GameEnv.reset()` presses them when there is no console, and a reference frame captured at first ready gives G2 a pass rule at last: after a reset the view matches the reference. Halo Wars has the pair proven already, d-pad left to jump to the base and right-stick click to put the rotation back to north, and it is the reason this is worth building: with it, three of the five games get a real G2 and every step in a run starts from the same view instead of wherever the last one drifted to.

The pass rule should come from 4.3 rather than from a changed-sample percentage. Measured on Halo Wars, the reset landed 40 percent of samples away from the stored view against 71 percent for a lost camera, which is a clear separation but a poor threshold to hard-code; a motion vector near zero is the honest test.

### 4.6 A noise floor worth trusting

`check_idle` takes several samples (three to five) and the floor becomes their maximum rather than a single second's count. Cheap on its own, but it changes every frame verdict already logged for Elden Ring, PowerWash Simulator and Halo Wars, so it has to land together with 4.3 and be followed by one rebaselining pass over those three games, about twenty minutes of runs, with section 8 of the harness document rewritten from the new numbers.

---

## 5. What the commands become

```
venv\Scripts\python tests\run_fast_tests.py                 # seconds, no hardware, what CI runs
venv\Scripts\python -m tests.test_bridge_services            # one fast file
venv\Scripts\python tests\probe_game_harness_windows.py --game halowars --actuator pad
venv\Scripts\python tests\probe_game_harness_windows.py --game halowars --actuator pad --write-expect
```

---

## 6. Limits and open questions

- **Does `SetWindowPos` hold on the three fullscreen games?** Answered per game in section 8. Halo Wars took it at launch (2560x1440 to 1280x720 borderless in one call) and held it through the menu walk and the match.
- **Is `test_dialog.py` interactive?** Yes: a pygame event loop for the legacy shell, importing modules that no longer exist at that path. It is skipped by name for good, beside `test_vjoy.py`; `simple_vjoy_test.py` does not match the pattern.
- **Does `requirements.txt` install on a runner with no drivers?** `pyvjoy` does (a wheel, a DLL loaded at import that only talks to the driver on device open). `vgamepad` did not safely (an sdist whose `setup.py` ran the ViGEmBus MSI installer when the driver was absent), which is one of the reasons `src/padbus_client.py` replaced it on 2026-09-09; since then nothing in the file touches a driver at install time.
- **Does the workflow run on GitHub?** Yes. The first push ran it twice (the push and the branch's pull request), and the job passed 9/9 in 58 seconds on `windows-latest`, so the requirements filter, the offscreen platform and the runner all hold on a machine with no driver. The one annotation was the checkout and setup-python actions targeting a deprecated Node, fixed by taking their current majors.
- **Phase correlation assumes a rigid shift.** A camera that rotates, a scene with moving actors in it, or a zoom breaks that assumption to different degrees. Log-polar covers rotation and scale about the image centre; a scene like Left 4 Dead 2's, with bots walking through the view, will still have a lower confidence than an RTS base. The console stays the better oracle wherever there is one.
- **Tolerance bands need a stable machine.** Half-Life 2's primitives were repeatable to a tenth of a unit and its full-deflection turn was not repeatable at all. Bands should be written from the checks that are stable and left off the ones that are not, exactly as `--cal-mags 0.4,0.6` already does for that game's calibration.
- **None of this tests the QML layer.** Every fast test is Python. The widget geometry, the drag handling and the dialogs are covered only by the Nimbus actuator's synthesized presses in a game run. A Qt Quick test harness is a separate question and not in this plan.

---

## 7. Order of work

1. **4.1**, the fast runner and CI. Independent of everything, half an hour, and it is what catches a regression before a game run is worth starting.
2. **4.2**, the `window` key. Independent, and it makes every run after it faster and watchable.
3. **4.3 with 4.6**, motion measurement and the honest noise floor, then one rebaselining pass over the three console-less games so their logged numbers all come from the same method.
4. **4.4**, tolerance bands, first for the console games from the existing calibrations, then for the rest once 4.3 gives them numbers.
5. **4.5**, the reset, last, because its pass rule is best expressed in 4.3's terms.

---

## 8. What was built, and what the frames said first

Everything in section 4, in the order of section 7, on 2026-09-09. The pieces:

| Piece | Where | State |
|---|---|---|
| Fast runner | `tests/run_fast_tests.py` | ten files since 2026-09-09 (`test_padbus_client.py` joined), about four seconds, all pass, with no driver needed |
| CI | `.github/workflows/fast-tests.yml` | passed on GitHub on the first push, 9/9 in 58 seconds on `windows-latest` |
| Motion measurement | `tests/frame_motion.py`, `tests/test_frame_motion.py` | 47 synthetic checks; wired into every step, every reset and the idle floor |
| `window` key | `GameEnv.apply_window`, three recipes | took on Halo Wars and Elden Ring, refused by PowerWash Simulator |
| Expect bands | `expect` in recipes, `--write-expect`, `<check>e` lines | seeded for the console games from their 2026-09-07 runs; both held on rerun |
| Console-less reset | `reset_buttons`, a reference frame, G2 | real on Halo Wars, landing at static 0.97 and 0.99 |
| Several idle samples | `--idle-samples`, `Thresholds.from_idle` | three by default; the floor is the largest, the shift threshold twice the largest believed idle shift |

### 8.1 What the saved frames changed before a game was launched

The design in 4.3 was tried on the frames the five games' earlier runs had saved, with their console readings as ground truth where there was one, before the harness was touched. Three things it had not allowed for:

- **A static overlay wins a single-peak correlation.** The HUD, a weapon viewmodel, PowerWash Simulator's wand: whatever stayed put makes the tallest peak, at the origin, and a single "the peak's offset" estimate read a whole sweep of PowerWash turns as no shift while the changed-sample count grew monotonically through it. The measurement now reports the origin peak as `static` and the three tallest peaks away from it as the motion, so a turn behind an overlay registers, and a bobbing overlay (the wand moves 8 px on its own) does not hide the world's shift behind it.
- **A sidelobe ratio alone lets an idle picture through.** The confidence the plan described, the peak's height against the rest of the surface, reached 9 to 11 on Halo Wars pictures that had not moved at all, because the surface of a near-identical pair is a spike and a floor, and any ripple on the floor stands out. The peak's absolute height separates the cases without ambiguity across all five games: real moves 0.06 and up, ripples 0.04 and down. Both are required now.
- **The log-polar map is only isotropic on a square.** A 5 degree turn of a 400 by 240 synthetic picture came back as 8, and 20 as nonsense, because the two frequency axes of a non-square spectrum have different resolutions and a rotation in the picture is a shear in the map. On a square it is exact to a degree; the rotation is measured over the central square. And on the real thing it recovered nothing: Halo Wars' one-second swings at 0.60 take the camera past the overlap, and the whole-picture rule carries those verdicts. The `rotate` kind is built, tested and honest about where it applies.

Two more came out of the rerun itself, from the pair of frames the new PowerWash Simulator Nimbus run saved for its one-pixel drag, which the run had called STILL on 9,956 changed samples: the world's 63 px shift was in the surface as two halves of 0.035, one each side of the zero row, because a pure yaw with a little bob the other way lands its peak on the line, and neither half cleared the floor alone. Raw peaks within two and a half samples of a taller one are now summed before the floors apply. And summing made a new false positive possible, which the next look found: an idle Elden Ring picture with 97 changed samples carried a merged 48 px "shift" built from two ripples, because a repeating texture correlates with itself a period away whether or not anything moved, and the same offsets appear in every one of that scene's idle samples. So the idle samples now also give the scene's own offsets, and a step's peak within 12 px of one of them is the scene, not the camera; a merged pair in an idle picture goes to that list rather than raising the threshold. Re-deriving every verdict of the day's runs from their saved frames under the final module changed exactly two: that PowerWash drag to MOVED, and Left 4 Dead 2's 0.20 step to STILL, where the console had read zero and the old frame verdict had followed the flashlight.

Two more facts worth having about the frames: Left 4 Dead 2's safe room has wallpaper striped every 56 px, and phase correlation there finds the true shift modulo a stripe, sign included (a 34 degree turn, about 480 px, read as +80; every magnitude in the sweep fits the same period), so a pixel figure is a number to band only where a run shows it repeats; and the idle flashlight sway in that scene is a coherent 54 px, so the run's own threshold there is 108 px and the frame oracle in a Source scene stays what section 7 of the harness document always said it was.

### 8.2 What the rerun found

The results are in [GAME_TEST_HARNESS.md](GAME_TEST_HARNESS.md) section 8, entry of 2026-09-09 afternoon. In short: Halo Wars' one-pixel drag reads STILL, and its recipe now says the floor does not move that game, so the check verifies the bridge sent the floor and the game ignored it; Left 4 Dead 2 held all four bands two days after they were measured (yaw at 0.40 and 0.60, pitch, walk); Half-Life 2 held its four, one of them at 13 percent of a 15 percent band; the window key took on two of the three full-screen games, and Elden Ring needed the re-application at readiness the plan asked for; PowerWash Simulator's sweep repeats its shifts to the pixel at 0.26 and 0.28 and is banded there, where the default 0.40 and 0.60 are past what the correlation can pin behind the wand; Elden Ring at 1280x720 reads its threshold at 0.30 by an 8 px floor that is about 0.6 degrees, closer to how a console is read than the old count was.

### 8.3 Still open

- Nothing bands a Halo Wars rate: the rotation estimate needs holds short enough to keep a swing inside the overlap, and nobody has measured what hold that is.
- PowerWash Simulator's window. The only way in is its saved preference file, which section 3 rejected; running it full screen costs capture time and nothing else.
- The pixel shift is a rate to band only where it repeats. Two magnitudes on one game repeat so far; the rest of the console-less checks stay liveness checks with a much better idea of what "moved" means.
- The QML layer is still untested except through the Nimbus actuator's synthesized presses in a game run.

## 9. A bug the suite could not see (2026-09-12)

Merging the Linux branch produced a defect the whole fast suite passed straight
through, before and after it existed. It is the clearest argument this document
has for what the suite is actually for, so it is recorded here rather than in a
commit message.

### 9.1 What happened

`main` and the Linux branch each defined the mouse-isolation slots once, in
different regions of `bridge.py`. Git merged both sets into the same class body.
Python binds the **later** definition, silently, so on Windows the Linux
software-cursor methods overrode the cursor relay and the relay became
unreachable. Eleven methods, plus the `mouseIsolationActive` property.

Nothing failed. No warning, no error, no test. The suite was green with the
Windows isolation path dead, because nothing in it exercised that path on either
platform. It was found by reading the file while working on something else.

### 9.2 Why the existing shape of the suite missed it

Two gaps, and they are different:

- **Structural.** A duplicate definition is invisible to any test that imports
  the module, because importing only shows the definitions that won. Seeing it
  requires parsing the source.
- **Behavioural.** Even with one definition per name, nothing checked that the
  isolation entry points route to the right implementation, or that synthetic
  events arrive where they are aimed.

Either gap alone would have hidden this. Closing one would not have closed the
other, which is why there are now two tests rather than one.

### 9.3 What was added

`tests/test_bridge_no_duplicate_methods.py` parses `bridge.py` with `ast` and
fails on any method defined twice, excluding property and setter pairs. Its
`KNOWN` list is empty; adding to it is meant to need a reason, because a
duplicate always means one of the two bodies is dead.

`tests/test_bridge_isolation.py` drives the bridge's isolation handlers against
a real offscreen `QWindow`: the dispatch is checked by **running** it for every
entry point on both platforms, synthetic events are checked to arrive at the
window that should receive them, a modal dialog is checked to receive them
instead of the main window, and the cursor is checked to survive a target
change rather than snapping to a corner.

The guard catches what parsing catches; the functional test catches an entry
point that calls one implementation unconditionally, which parsing never would.

### 9.4 The rule that came out of it

Both tests were validated by re-introducing the defects they exist for, rather
than by being written and assumed to work. Removing the dispatch fails two
named tests; removing the modal targeting fails two others. A test that has
never failed is a guess about a failure mode, not a check on one.

The same principle applies to `tests/test_linux_input_safety.py`, which covers
the keep-alive pulse undoing a stick release: it replays the exact interleaving
from the review rather than asserting that a lock exists.

### 9.5 What this still does not cover

The suite tests the bridge, not the devices. The kernel filter, the evdev grab,
the pass-through keyboard and the real cursor relay have no automated coverage,
and a mock can only ever agree with the code that wrote it. That is why
`tests/probe_linux_stack.py` reads the stick position back out of the kernel
instead of asserting on `current_values`, and why the hardware probes remain
something a person runs.

Measured on Windows after the merge, on the dev machine: the pad bus probe
14/14 against real ViGEmBus, the stick shaping probe 19/19 through the real QML
app including an output-device switch, and the Left 4 Dead 2 harness 19/19 end
to end with both `expect` bands matching their pre-merge values (-2.4 deg/s on a
1 px drag, +200.0 units/s on a full left stick).

### 9.6 What the Linux box found, and what that says about probes

The Linux half ran on 2026-09-12 (Ubuntu 22.04, kernel 6.8). It found four
things, and only one of them was in the code the probe was aimed at.

- **`src/padbus_client.py` was wrong off Windows.** `ULONG` aliased
  `ctypes.c_ulong`, which follows the host C ABI and is 8 bytes on LP64, so
  every ioctl struct came out double width (`XUSB_GET_USER_INDEX` at 24 bytes
  against 12). Win32 `ULONG` is 32-bit on every target, so the alias is now
  `c_uint32`: identical bytes on Windows, and `test_padbus_client.py` goes from
  44 passed with 9 failed to 53 passed on Linux. A fast suite meant to run
  anywhere had never been run anywhere else.
- **The shipped udev rule covered only the write side.** It granted
  `/dev/uinput`, but the event node the kernel then publishes is `root:input
  0660`, so nothing could read back a device Nimbus had just created without
  joining the `input` group.
- **A bridge test asserted Windows lifecycle on every platform.** It is now
  gated, and on Linux pins the stronger property it was reaching for:
  `shutdown()` is the only call the bridge may make on a retired interface.
- **The probe's own section C had never read a single event.** It opened the
  node fresh on each call, but an evdev client only receives events generated
  after it opens, so every read drained a buffer filled before it existed; and
  `except OSError: return seen` collapsed "cannot open" into "reported
  nothing", so both faults printed as `kernel ABS_X = None`.

That last one is the finding worth keeping. udev applies the `uaccess` ACL
*after* `UI_DEV_CREATE` has returned, measured at 51 ms against the probe's
first read at 50 ms, which is why it failed every time rather than
intermittently and therefore looked like a deterministic code fault rather than
a race. A probe is code, and it needs the same suspicion as the thing it
measures: this one would have reported a healthy stack as broken, and the
reading that mattered was the one it never took.

With it fixed, the pulse claim is checked against a real kernel for the first
time: a pulse leaves the commanded position at 19660 of 19660, and both release
paths centre at 0. That is section 9.4's rule arriving from the other
direction. The test was validated by re-introducing the defect; the probe was
validated by a kernel disagreeing with it.

---

## Related Documents

- [Game Test Harness](GAME_TEST_HARNESS.md): the harness this plan changed, its recipes, and the results log whose section 8 records the Halo Wars run that prompted it and the rebaselining pass that followed.
- [Aim Assistance](AIM_ASSISTANCE.md): section 12.3, the per-game deadzone table that the N2 check feeds, and the false positive it recorded.
- [Windows Mouse Filter Plan](WINDOWS_MOUSE_FILTER_PLAN.md): section 5, the probe and results-log conventions this document follows.
- [Research Platform](RESEARCH_PLATFORM.md): why measured per-game numbers matter beyond regression testing.
