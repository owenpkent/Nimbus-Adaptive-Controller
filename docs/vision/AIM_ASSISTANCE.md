# Aim Assistance: Why Aiming Is Hard and What To Do About It

**Status:** Research plus a code audit of the live pipeline (section 2, 2026-09-06). Tier 1 (section 4) and steps 1 to 7 of section 11 are implemented as of 2026-09-06 on `feature/aim-assistance-pipeline`; section 14 records what was built and where it departs from the proposal. Section 6 is still proposed work; sections 7 to 9 are options that need no code.
**Question:** Users report that aiming through Nimbus is hard. How much of that is our pipeline, how much is the mouse-to-stick paradigm, and what assistance can we legitimately provide?

---

## 1. Summary

Four things stack against the user, and three of them are ours:

| Cause | Owner | Fix effort |
|---|---|---|
| A 200 px stick widget gives only 81 px of mouse travel for full deflection | Nimbus | Low |
| No output anti-deadzone, so the game's own 24 to 26.5% inner deadzone eats the first quarter of our range | Nimbus | Low |
| The sensitivity curve is applied twice, once in QML and once in the bridge | Nimbus | Low |
| Deadzone and curve are per-axis on a circular control, so the dead region is a cross and diagonals are distorted | Nimbus | Low |
| A mouse is a position-control device and a stick is a rate-control device | Physics | Not fixable, only narrowable |

The first four are a day of work between them and they compound: fixing the anti-deadzone is also what makes the game's *own* aim assist start working for our users (section 7).

---

## 2. The measured problem

### 2.1 The stick has 81 pixels of travel

`qml/components/DraggableWidget.qml` lines 294 to 297:

```qml
readonly property real joyRadius: Math.max(1, Math.min(width, height) / 2 - borderWidth / 2)
readonly property real thumbRadius: Math.min(width, height) * 0.18 * 0.5
readonly property real effectiveRadius: Math.max(1, joyRadius - thumbRadius)
```

For the 200 px sticks in `profiles/adaptive_platform_2.json`: `joyRadius` is 99, `thumbRadius` is 18, `effectiveRadius` is **81 px**. The drag handler at line 615 converts pixel delta straight to normalized deflection by dividing by that radius.

So 81 px of mouse movement covers zero to full turn rate. One pixel is 1.2% of deflection. We are asking for sub-pixel precision from a hand that, for many of our users, does not have sub-pixel precision.

The tuning knob for this is control-display gain, and the direction is counter-intuitive: **more travel is more precision**, bounded only by desk space and screen area. Lock mode currently runs the wrong way. `qml/layouts/CustomLayout.qml` line 306 computes `scale = lockSensitivity * 2`, which at the default of 4 is an 8x gain, meaning full deflection in roughly 12 px.

### 2.2 The game discards the first quarter of our range

`controller.prefer_vigem` is `true` by default, so output is XInput. The standard constants are:

| Constant | Raw | Fraction of range |
|---|---|---|
| `XINPUT_GAMEPAD_LEFT_THUMB_DEADZONE` | 7849 | 24.0% |
| `XINPUT_GAMEPAD_RIGHT_THUMB_DEADZONE` | 8689 | 26.5% |
| `XINPUT_GAMEPAD_TRIGGER_THRESHOLD` | 30 | 11.7% |

Nimbus has no anti-deadzone anywhere in the pipeline. Games that use the documented values (most of them) therefore see nothing at all until our output magnitude passes 0.265 on the right stick, and then the crosshair starts at a quarter-speed turn rather than easing in from zero. That discontinuity at the center is the on-off feel users describe as "twitchy" or "it does nothing then it does too much."

### 2.3 The curve is applied twice

`qml/components/DraggableWidget.qml` lines 459 to 460 apply `_applyCurve` per axis, then `src/bridge.py` lines 525 to 526 (`setLeftStick`) and 544 to 545 (`setRightStick`) call `ControllerConfig.apply_sensitivity_curve`, which takes the `apply_joystick_dialog_curve` branch because `joystick_settings.sensitivity` is set in every generated config.

With current defaults (widget: deadzone 0, extremity 5, sensitivity 50; global: deadzone 10, extremity 5, sensitivity 50) the chain is:

```
n  ->  QML:    0.95 * n
   ->  bridge: ((0.95n - 0.025) / 0.975) * 0.95
```

Maximum output is **0.901**, not 1.0. The user can never reach full turn rate. Two extremity caps and two deadzones are stacking.

It gets much worse the moment either sensitivity slider is touched, because the two power curves multiply. A widget at 20% (power 2.8) through a global setting at 20% (power 2.8) is an effective power of **7.84**, at which point 50% stick travel produces 0.4% output. This is almost certainly what a user hits when they try to make aiming easier by lowering sensitivity and find it becomes dramatically worse.

This also violates the rule in `CLAUDE.md`: curves, deadzones and smoothing belong in the bridge, never in QML.

### 2.4 Combined effect

Taking 2.1 through 2.3 together for the right stick against a standard 26.5% game deadzone:

| Mouse travel from center | What the game sees |
|---|---|
| 0 to 2 px | Nothing (our own stacked deadzone) |
| 2 to 25 px | Nothing (inside the game's inner deadzone) |
| 25 px | Turn starts, abruptly, at 26.5% rate |
| 81 px | Maximum, capped at 90% of full rate |

**The entire usable aiming range is 56 px of mouse travel**, entered through a 25 px dead region and never reaching full deflection. That is the headline finding.

### 2.5 Two smaller defects found in the same audit

**Per-axis shaping on a circular control.** Both `_applyCurve` in QML and `apply_sensitivity_curve` in the bridge operate on x and y independently, while the drag handler clamps magnitude to the unit circle. The result is a cross-shaped dead region rather than a circular one, and diagonal response that does not match cardinal response. Microsoft's own XInput guidance recommends a radial (magnitude-based) deadzone for exactly this reason.

A related consequence: with a convex curve (sensitivity above 50, power below 1) a clamped diagonal of 0.707 per axis becomes 0.841 per axis, a magnitude of 1.19. Both backends clamp per axis to [-1, 1] (`src/vigem_interface.py` lines 130 and 162; `src/vjoy_interface.py` `update_axis`), so nothing overflows, but the effective gate becomes square: diagonals are faster than cardinals, and the thumb position on screen stops matching the output.

**No smoothing on the path we actually use.** `src/bridge.py` line 615 returns early from `_smoothing_tick` when ViGEm is active, and the per-widget `tremorFilter` EMA is only wired into the lock-mode handler at `qml/layouts/CustomLayout.qml` line 312. A normal drag on the default ViGEm path gets no filtering at all. For a user with tremor this is the difference between playable and not.

---

## 3. The part that is not our fault

A mouse is a **position control** device: displacement of the device maps to displacement of the controlled object. A stick is a **rate control** device: displacement maps to velocity, and the object keeps moving while the stick is held. The HCI literature is consistent that position control outperforms rate control for pointing tasks, and Nimbus's whole premise converts the better paradigm into the worse one.

That is the price of the architecture and it buys compatibility with every game that accepts a gamepad. It cannot be removed, but the gap can be narrowed from both ends:

- Make the rate-control mapping as good as it can be (sections 4 to 6).
- Let the game's own assistance do the last few degrees (section 7).
- Where a game supports it, offer a genuine position-control mode (section 6.4).

---

## 4. Tier 1: fix the pipeline

Highest value per hour of work, no new concepts, no ethical questions.

### 4.1 Single shaping function, radial, in the bridge

Replace per-axis shaping with one function that takes the whole vector. Proposed for `src/config.py`:

```python
def shape_stick(self, x: float, y: float, joystick: str) -> Tuple[float, float]:
    """
    Shape a raw stick vector into driver output.

    Applies, in order: unit-circle clamp, radial deadzone, response curve,
    output anti-deadzone, and the extremity cap. Radial rather than per-axis
    so the dead region is a circle and diagonals match cardinals.

    Parameters
    ----------
    x, y : float
        Raw normalized deflection from the widget, before any shaping.
    joystick : str
        "left" or "right"; selects the per-stick settings block.

    Returns
    -------
    Tuple[float, float]
        Shaped output in [-1, 1] per axis, magnitude never exceeding 1.
    """
```

The QML side then sends raw values and stops calling `_applyCurve`. Delete the second application in `setLeftStick` / `setRightStick` and route both through `shape_stick`.

This alone restores the missing 10% of range and removes the curve-multiplication trap.

### 4.2 Output anti-deadzone

After the curve, remap non-zero output from `[0, 1]` onto `[anti_deadzone + buffer, 1]` so the smallest real movement produces the smallest movement the game will accept. Steam Input ships exactly this pair of controls, "Output Anti-Deadzone" and "Anti-Deadzone Buffer", and the buffer exists so the user can re-introduce a small dead region of their own choosing once the anti-deadzone is calibrated.

Proposed defaults, since we know the backend:

| Output mode | Stick | Anti-deadzone default |
|---|---|---|
| ViGEm (XInput) | Left | 0.24 |
| ViGEm (XInput) | Right | 0.265 |
| vJoy (DirectInput) | Any | 0.0 (games vary; user calibrates) |

These are defaults, not constants. Games override the XInput values freely, so the setting must be adjustable per profile, and the settings dialog needs a **live readout of the post-shaping output value** so the user can raise the anti-deadzone until the smallest movement produces visible motion. Calibrating this blind is miserable.

### 4.3 Decouple travel from widget size

Add a `travel_px` property to axis widgets: the number of mouse pixels required for full deflection, independent of how large the widget is drawn. A 200 px stick should be able to demand 400 px of travel. Default it to the current `effectiveRadius` so existing profiles do not change behavior.

While there, reconsider lock mode: `lockSensitivity * 2` should probably become a divisor or be re-expressed in the same `travel_px` units so that one concept covers both paths.

### 4.4 Precision modifier

A button that, while held or latched, multiplies gain by a configurable factor (0.25 is a reasonable default) and optionally steepens the curve. This is the standard sniper-toggle pattern and it is the cheapest large win after anti-deadzone, because it lets a single profile serve both "swing the camera around" and "hold on a target."

### 4.5 Tremor filter on the drag path

The EMA already exists at `qml/layouts/CustomLayout.qml` lines 312 to 320. Move it into the bridge (per the `CLAUDE.md` rule) and apply it on every axis path, not just lock mode. Note that filtering costs latency, so it must stay off by default and be exposed per widget.

### 4.6 Response curve default

The sensitivity slider already produces powers from 0.1 to 4.0. For aiming specifically, a wide curve that gives more travel to the low end is the right default, and 50% (linear) is the current one. Worth changing the default for `joystick` widgets specifically, or shipping an "Aiming" preset.

---

## 5. Proposed settings surface

New keys under a profile's config. **This is a schema change and touches the three-way contract** (profile JSON, `CustomLayout.qml` plus `DraggableWidget.qml`, and the bridge mapping), so it needs sign-off before implementation per `CLAUDE.md`.

```jsonc
// controller_config.json / profile, per stick
"aim": {
  "anti_deadzone": 0.265,      // 0..1, fraction of output range to skip
  "anti_deadzone_buffer": 0.02, // 0..1, user-chosen dead region after remap
  "radial": true,               // radial shaping instead of per-axis
  "precision_gain": 0.25,       // gain while the precision modifier is active
  "settle_ms": 0                // dwell-to-settle, 0 = off (section 6.1)
}
```

```jsonc
// custom_layout.widgets[] additions for type "joystick"
"travel_px": 240,        // mouse pixels for full deflection
"anti_deadzone": 0.265,  // per-widget override of the profile value
"tremor_filter": 0.0     // already in the schema, extend to the drag path
```

---

## 6. Tier 2: assistance that is not target-aware

These help without the software knowing anything about what is on screen, which keeps them defensible everywhere (see section 10).

### 6.1 Dwell-to-settle

When the pointer stops moving for N milliseconds, ease output toward zero. Stops the slow drift that happens when a user cannot return the stick exactly to center, which is the most common complaint pattern for head and mouth control.

### 6.2 Axis lock

A modifier that zeroes one axis while held, so horizontal tracking cannot drift vertically. Very effective for tremor, and trivially implemented in the bridge.

### 6.3 Reversal rate limit

Cap how fast output may swing through the center. Kills the overshoot oscillation that happens when a user corrects an overshoot with another overshoot.

### 6.4 Absolute (position) aim mode

For games that tolerate it, drive the stick from the cursor's distance from a fixed screen anchor rather than from drag delta. This keeps the mouse in its native position-control paradigm and is close to what Steam Input calls Mouse Region. It will not work everywhere, but where it works it is a step change rather than an improvement.

This one interacts with the mouse isolation work in `WINDOWS_MOUSE_FILTER_PLAN.md`, since it needs the physical cursor position and needs the game not to see it.

---

## 7. Tier 3: the game's own aim assist, which we already qualify for

Because ViGEm presents a real XInput device, games apply the **controller aim assist path** to Nimbus users with no code on our side. That is two distinct mechanics:

- **Slowdown / friction**: the reticle slows as it crosses a target.
- **Rotational aim assist**: the camera tracks a moving target.

The detail that matters for us: **rotational aim assist only activates while the game registers stick input above its deadzone.** Standing still yields slowdown only. So section 4.2 is not merely a feel fix, it is the precondition for the strongest assistance already available to our users. Today the first 25 px of travel produce input the game does not count as input at all.

**The structural limit.** Rotational assist is strongest when the left stick is also moving, and a single pointer cannot drive both sticks at once. Legitimate mitigations are a latched strafe button or a toggle-hold movement widget, where the user chooses the direction and the software only holds it. Automatically injecting left-stick motion to farm rotational assist is not on the table; see section 10.

## 8. Tier 4: per-game settings, which cost nothing

The best aim assist is usually already in the game and switched off by default. Candidates to document per title in `docs/GAME_COMPATIBILITY.md`, as a new column or a companion table:

| Setting | What it does |
|---|---|
| Aim assist strength | Direct multiplier, often 0 to 100 |
| Lock-on / auto-target | Snaps and holds on the nearest target |
| Aim assist slowdown | Reticle friction over targets |
| Zoom snap | Small snap toward a target on ADS |
| Hold vs toggle aim | Removes a sustained hold from the user |
| Analog vs digital aim | Digital removes the fine-control requirement entirely |

The Last of Us Part I is the reference implementation with more than 60 accessibility options including aim assist strength, and is worth citing in user-facing docs as the standard to look for.

## 9. Tier 5: a different input device

For some users this is the honest answer, and it fits the position already stated in `docs/WHITEPAPER.md` that Nimbus wraps adaptive hardware rather than displacing it. The established pattern in the QuadStick community is **eye tracking for aiming, QuadStick for buttons**. Tobii Eye Tracker 5 does head and eye tracking with native support in 170+ titles.

Nimbus's role there is unchanged: it is the layer that turns whatever pointer the user has into gamepad output. A head tracker or eye tracker producing pointer events flows through the same pipeline, and every fix in section 4 benefits it.

---

## 10. The line we do not cross

Screen-capture target detection driving the stick is an aimbot regardless of intent, and so is injecting synthetic stick motion the user did not command in order to trigger the game's assistance.

As of 2026 both Activision and Respawn have stated publicly that genuine accessibility hardware is exempt and encouraged, and that XIM and Cronus are not accessibility tools despite being marketed as such. Respawn classified XIM adapters as cheating devices in March 2026 with permanent bans and no appeals; Activision's Ricochet update for Black Ops 7 Season 2 describes detection built to recognize *classes of machine-driven behavior* rather than specific device signatures. `HOST_MODE_ISOLATION.md` section 8 already reaches the same conclusion from the driver side: at the device layer Nimbus is not distinguishable from a XIM, so the distinction has to be behavioral.

The property that keeps Nimbus on the right side of that line is simple and worth stating explicitly in the README as a non-goal:

> **Nimbus reshapes input the user produces. It never originates aim.**

Everything in sections 4 through 6 satisfies that. Nothing in this document proposes reading the screen, detecting targets, or generating input the user did not command.

A proposal to narrow this line for single-player games only, with the research behind it and the rules it would keep, is [TARGET_AWARE_AIM_PLAN.md](TARGET_AWARE_AIM_PLAN.md) (2026-09-13). It is not adopted; nothing here changes until its section 2 is decided.

---

## 11. Order of work

1. Delete `_applyCurve` from the QML send path; send raw normalized values. (Fixes 2.3, restores 10% of range.)
2. Add `shape_stick` to `ControllerConfig` with radial deadzone and curve; route both stick slots through it. (Fixes 2.5.)
3. Add output anti-deadzone and buffer with per-backend defaults. (Fixes 2.2, unlocks section 7.)
4. Add `travel_px` to axis widgets. (Fixes 2.1.)
5. Add the precision modifier.
6. Move the tremor EMA into the bridge and apply it on all axis paths.
7. Add a live post-shaping output readout to the joystick settings dialog, so 3 can be calibrated.
8. Then reassess. Sections 6 and 8 come after, and section 6.4 should wait for the isolation work to settle.

Steps 1 and 2 are a behavior change for every existing profile. They need a note in `CHANGELOG.md` and probably a one-time migration that halves any non-default sensitivity value, since users who compensated for the double curve will suddenly find their settings twice as aggressive.

## 12. How to verify

Five layers, in order of how much each tells us. The first three are unattended scripts in `tests/`, in the style of the driver probes; the last two need a person. Results go in section 14.

| Layer | What it proves | Tool | Needs |
|---|---|---|---|
| 1. Shaping math | The formula's properties: radial symmetry, magnitude never above 1, floor and ceiling, deadzone, gain | `tests/test_stick_shaping.py` | nothing |
| 2. The app end to end | Raw geometry in the QML becomes the right driver output: floor, ceiling, travel, radial, precision, tremor release, triggers, vJoy, lock mode, the config dialog, cache refresh | `tests/probe_stick_shaping_windows.py` | ViGEmBus; safe over TeamViewer |
| 3. The game's real deadzone | The magnitude at which a real game starts to move its camera, and that a 1 px Nimbus drag lands above it | `tests/probe_game_deadzone_windows.py` | a running game whose view follows the right stick |
| 4. Gamepad tester | A human sanity check of layer 2 | joy.cpl or a browser gamepad tester | a person; TeamViewer is enough |
| 5. Hands on | Whether aiming feels better | the game, the widget dialog's test pad | a person at the console |

### 12.1 Shaping math

```
venv\Scripts\python tests\test_stick_shaping.py
```

Pure Python, no Qt, no driver. Linear defaults land at 0.95 and the old double chain at 0.901; the smallest movement lands at floor = anti-deadzone + buffer and full deflection still hits the ceiling; the floor never exceeds the ceiling and a buffer above a zero anti-deadzone adds nothing; the radial deadzone zeroes a diagonal that is inside it; diagonals match cardinals in magnitude across the sensitivity range and nothing exceeds 1, including convex curves and input that overflows the circle; the slider-to-exponent mapping is unchanged; gain scales the post-deadzone magnitude; direction and sign are preserved.

### 12.2 The app end to end

```
venv\Scripts\python tests\probe_stick_shaping_windows.py
```

Loads the real QML app in-process as the relay probe does, writes a throwaway profile (`nimbus_probe_shaping`: an auto-travel stick on x/y, a 160 px-travel aim stick and a tremor stick on rx/ry, an RT slider, a plain button and a precision button) into the user profiles folder, switches to it, drives the widgets with synthesized Qt mouse events, and reads what the bridge sent to ViGEm. Expected values come from the bridge's own resolved parameters through `shape_magnitude`, so the checks are about wiring, not the formula. The profile is deleted and `controller_config.json` restored afterwards.

- S0 a plain button still presses and releases a gamepad button
- S1 floor: a 2 px drag on the aim stick reads the anti-deadzone plus buffer (about 0.29), not 0.01
- S2 ceiling: a full drag reads 0.95, the widget's own cap
- S3 travel: 80 px on the 160 px stick is well under 80 px on the auto stick (this is the ruler test, in code)
- S4 radial: a diagonal drag has the magnitude of a cardinal one, and screen-down is controller-down
- S5 release recentres exactly
- S6 precision: the modifier button sets the bridge modifier, and holding the modifier re-shapes a held stick without a new pointer event
- S7 tremor: a filtered stick lags its input, rises with more samples, and a release still reads exactly 0
- S8 triggers: the RT slider reads 1.00 pulled and 0.00 released
- S9 vJoy output has no anti-deadzone by default, and switching back restores it
- S10 lock mode: triple-click, a hover offset drives the stick, triple-click again centres it
- S11 the config dialog: opens by double-click in edit mode, shows the resolved defaults (Anti-DZ 26.5 for a right stick under ViGEm, Travel 160, Precision 25), the bridge-drawn preview reports floor and ceiling, the test pad reports the shaped vector and, with Drive on, moves the real stick, Apply writes the profile; a screenshot lands in `tests/probe_frames/shaping_dialog.png`
- S12 the value applied in S11 is what the bridge shapes with next (the cache refreshed through `saveCustomLayout`)

### 12.3 The game's real deadzone

```
venv\Scripts\python tests\probe_game_deadzone_windows.py --title "Left 4 Dead 2" --wait-for-window 300 --warmup 75
"C:\Program Files (x86)\Steam\steam.exe" -applaunch 550 -novid -windowed -noborder -w 1280 -h 720 +map c1m2_streets +exec 360controller
```

then quit the game and repeat with `--mode nimbus`. The order matters: Source decides at launch whether an XInput controller is present and never reads one created later, so the probe (which owns the pad, or runs Nimbus) starts first and waits for the game window. Without the flags the probe expects a game that is already in a map with its view following the right stick. The sweep holds a ViGEm right stick at each magnitude in `--magnitudes` (0.10 to 0.40 by default) for `--hold` seconds, game in the foreground, and counts changed frame samples against the idle noise floor, reusing the capture and differencing of `probe_game_mouselook_windows.py`. The first magnitude that moves the camera is the game's inner deadzone: the number the anti-deadzone has to clear, to compare with the XInput constant of 0.265. `--mode nimbus` runs the real app in-process beside the game, finds the current profile's rx/ry stick, and checks that a full drag moves the camera (control) and a `--nudge` px drag (default 1) does too: the floor clears the threshold. Left 4 Dead 2 is the candidate while test signing is on; Elden Ring is the reference XInput title once EAC can start. The game test harness ([GAME_TEST_HARNESS.md](GAME_TEST_HARNESS.md)) has since taken over this layer for Source games: it launches the game itself in the right order and reads the turn in degrees from the game's console instead of counting changed pixels.

#### Measured thresholds, and the default that cannot serve all five (2026-09-07, extended 2026-09-08 and 2026-09-09)

The harness has now put five games' right-stick thresholds on the same scale, and the default floor of **0.289** (anti-deadzone 26.5 plus buffer) sits in a different place relative to each:

| Game | Threshold | Where 0.289 lands | Through Nimbus, 1 px drag |
|---|---|---|---|
| Elden Ring | 0.30 by the motion oracle at 1280x720 (at or below 0.20 by the old count) | above | moves: 146 px of picture a second, about ten degrees |
| Left 4 Dead 2 | 0.26 to 0.28 | just above, by design | 2.4 degrees a second |
| Half-Life 2 | 0.30 to 0.40 | **below it** | **nothing at all** |
| PowerWash Simulator | below 0.26 (0.20 still) | comfortably above | moves: 58 px of picture a second, between the pad's 0.28 and 0.30 |
| Halo Wars: Definitive Edition | 0.40 to 0.60 | **far below it** | **nothing at all** (the check said otherwise once; it agrees now, see below) |

Half-Life 2 and Halo Wars are the cases the default gets wrong, and Halo Wars is worse by a distance. The bridge does exactly what it should, sending its 0.289 floor for a one-pixel drag, and the camera does not move, because neither game starts turning until 0.30 to 0.40 and 0.40 to 0.60 respectively. So the smallest movement a Nimbus user can make is invisible in both, which is the same on-off complaint section 2.2 describes, arrived at from the other side: not the game eating the first quarter of our range, but our floor sitting under the game's own threshold. Left 4 Dead 2's 0.28, Elden Ring's 0.26 to 0.30 (its camera answers 0.26 with a fraction of a degree a second, which the motion oracle counts as still, and answers the floor with about ten degrees a second) and PowerWash Simulator's sub-0.26 bracket them on the other side, so no single constant is right for all five: Halo Wars needs its Anti-DZ raised to about 60 and Half-Life 2 to about 40, where Elden Ring and PowerWash want it lowered. Three of the five sit below the default and two above it, and the spread is now more than a factor of three, which is the shape of the problem: the outliers are not rare enough to ignore and not consistent enough to design a default around.

**Halo Wars also showed how a frame verdict could flatter the floor, and that is fixed.** Its N2 check reported a pass on 2026-09-09 morning, and it was wrong: the run's idle second happened to be exceptionally quiet, which dropped the MOVED threshold to a quarter of the pad run's, and about 1,300 changed samples of scene animation was read as a camera turn. Where the pixels changed settled it, because a rotation moves the whole picture and this change left the top of the screen untouched. The same afternoon the frame oracle was replaced with a motion measurement (a shift by phase correlation, a change grid, thresholds from three idle seconds; [TESTING_STRATEGY.md](TESTING_STRATEGY.md)) and the rerun read the same drag as STILL: 376 changed samples, the static peak at 0.95, no cell past the threshold. The recipe now says `floor_moves_camera: false`, as Half-Life 2's does, so the check verifies the bridge sent its floor and the game, as measured, ignored it. The detail is in [GAME_TEST_HARNESS.md](GAME_TEST_HARNESS.md) section 8.

This is the concrete argument for the per-profile setting plus the dialog's live readout that section 4.2 already asks for, and for section 8's per-game settings: the calibration loop is the feature, not the constant. Measured by `--game halflife2 --actuator nimbus` (N2) against the pad sweep's `--game halflife2 --actuator pad` (G5); both are in [GAME_TEST_HARNESS.md](GAME_TEST_HARNESS.md), section 8.

### 12.4 Gamepad tester

Open joy.cpl (Set up USB game controllers, Xbox 360 Controller for Windows, Properties) or a browser gamepad tester, then nudge a Nimbus stick one pixel. The axis should jump to about 0.29 rather than creep from zero, a full drag should stop at 0.95, and a diagonal should stay inside the circle. Works over TeamViewer, since the drag path only needs the injected pointer.

### 12.5 Hands on

Play with the default profile, then the calibration loop of section 4.2: Edit Layout, double-click the aim stick, turn on "Drive the controller" on the test pad, nudge until the crosshair moves, and raise or lower Anti-DZ until the smallest movement is the smallest the game accepts. Set a button's Action to Precision aim and hold it on a target. For a before-and-after comparison, check out `main` and this branch with the same profile.

## 13. Open questions

- Should anti-deadzone default to the XInput constants, or to zero with a first-run calibration prompt? Defaulting to 0.265 is right for most games and wrong and confusing for the ones that already compensate. **Resolved (section 14):** the constants, per widget, with the dialog's test pad as the calibration loop; Left 4 Dead 2 measured at exactly that constant. The game test harness ([GAME_TEST_HARNESS.md](GAME_TEST_HARNESS.md), 2026-09-07) then put the floor in degrees: through Nimbus a 1 px drag sends 0.289 and turns Left 4 Dead 2's camera 2.4 degrees a second, and found Elden Ring's threshold at or below 0.20, so on that game the default floor is a little above what is needed and Anti-DZ can come down. Half-Life 2 (2026-09-07) then found the other edge: its threshold is between 0.30 and 0.40, so the same 1 px drag sends 0.289 and the camera does not move at all, and PowerWash Simulator (2026-09-08) landed with Elden Ring below 0.26. Four games, four thresholds, one of them above the default: the constant is a reasonable start and cannot be the answer on its own, so the per-profile setting and the calibration loop are the feature. See section 12.3 for the table.
- Does the migration in section 11 need to be automatic, or is a changelog note plus a "reset to defaults" button enough? **Resolved (section 14):** no migration is needed; with the global block at its defaults the second pass was near linear, so nothing doubles.
- Is `travel_px` the right unit, or should it be expressed as a gain multiplier so it survives DPI changes? Mouse DPI and Windows pointer speed both affect the physical distance a pixel represents. **Open.** Shipped as pixels, which is what the user can measure on their desk; a gain multiplier on top of the drawn radius would be just as DPI-dependent.
- Where does the precision modifier live in a layout that has no spare buttons? A dwell zone, a second pointer button, and a screen-edge region are all candidates. **Open.** Shipped as a button widget action (`modifier: "precision"`), momentary or latched; the other candidates are section 6 work.

## 14. Implementation notes (2026-09-06)

Steps 1 to 7 of section 11 are in (`src/config.py`, `src/bridge.py`, `qml/components/DraggableWidget.qml`, `qml/layouts/CustomLayout.qml`, `profiles/adaptive_platform_2.json`). What was built, and where it departs from sections 4 and 5:

- **One shaping function.** `shape_magnitude` and `shape_vector` in `src/config.py` do the whole chain on the vector's magnitude: inner deadzone, gain, power curve, then the remap onto `[floor, ceiling]`. The bridge resolves each custom-layout widget's settings from the profile by id and shapes in `setStickInput` / `setAxisInput`, so QML sends raw geometry and `_applyCurve` is gone. `shape_stick` covers the legacy layouts with the global `joystick_settings` block. The curve preview asks the bridge for its points.
- **Floor inside the ceiling.** Section 4.1 orders the anti-deadzone before the extremity cap. It is the other way round in code: the remap is onto `[anti_deadzone + buffer, 1 - extremity]`, because applying a 5% cap after the floor would push a calibrated 0.265 down to 0.252, back under the game's threshold. The buffer only counts on top of a non-zero anti-deadzone.
- **No profile-level `aim` block.** Section 5 proposed per-stick profile keys with per-widget overrides. Everything is per widget (`anti_deadzone`, `anti_deadzone_buffer`, `travel_px`, `precision_gain`, and the existing `tremor_filter`), since the widget dialog is the only place a custom-layout user edits shaping. `joystick_settings` gained optional `anti_deadzone`, `anti_deadzone_buffer` and `precision_gain` keys for the legacy layouts only. `radial` is not a setting: shaping is always radial. `settle_ms` waits for section 6.1.
- **The precision modifier is a button widget with `modifier: "precision"`**, so no new widget type; `toggle_mode` latches it. The gain is per joystick, so a movement stick can be left at 100%. A held stick is re-shaped the moment the modifier changes. Open question 4 (where it lives in a layout with no spare button) stays open.
- **Defaults.** Anti-deadzone defaults to the XInput constants under ViGEm and 0 under vJoy, resolving open question 1 in favour of the constants; a user whose game already compensates lowers the slider, and the dialog's test pad with "Drive the controller" on is the calibration loop of section 4.2. Sticks keep the 5% extremity cap; sliders and wheels default to none, which also fixes the RT trigger idling at 5% and topping out at 95% (it went through the widget curve and then the rudder curve). Hold and return-to-zero sliders are shaped as the unipolar controls they are. The bundled right stick ships with `travel_px` 160.
- **No migration**, resolving open question 2. With the global block at its defaults the second pass was near linear, so no sensitivity doubles; the visible change is the top 5% of range returning and the global 2.5% deadzone going. A changelog note covers it. The Axis Configuration dialog that edits the global block is hidden for custom layouts anyway.
- **Lock mode is unchanged** apart from its EMA moving into the bridge. `lockSensitivity * 2` still stands; the second paragraph of section 4.3 is open.
- **A release always centres.** The tremor EMA in the bridge treats an exact (0, 0) as a release and drops its state, so a heavily filtered stick can never be left holding a residual deflection after the pointer lets go. This matters more now than in lock mode, where nothing ever released.
- **Verified** (section 12 layers, dev machine, 2026-09-06). Layer 1, `tests/test_stick_shaping.py`: 27/27. Layer 2, `tests/probe_stick_shaping_windows.py`: 17/17, including the ruler test in code (80 px on the 160 px aim stick reads 0.618, 80 px on the auto stick 0.941), a 2 px drag landing at 0.293, a diagonal (0.451, -0.451) matching its cardinal at 0.638, the precision button taking a held 0.618 to 0.368 and back with no pointer event, a tremor-8 stick rising 0.469, 0.604, 0.698, 0.769, 0.817 and releasing to exactly 0, vJoy at 0.506 (no floor) and the ViGEm default back afterwards, lock mode at 0.95 and unlock at 0, and the dialog opening on a double-click with Anti-DZ 26.5, the preview reporting 0.292 and 0.950, the test pad at 0.855 driving the real stick, and an applied 10% reaching the bridge (a 2 px drag then reads 0.130). `tests/probe_nimbus_relay_windows.py` still 8/8. Two things the probe taught: three presses on one stick within 400 ms are a triple-click lock, as designed, so the probe spaces its presses; and a `MouseButtonDblClick` sent straight to the window does not reach QML's `doubleClicked`, so the dialog is opened through QTest's platform-level double-click, the path real input takes. Layer 3, `tests/probe_game_deadzone_windows.py` against Left 4 Dead 2 (windowed 1280x720, `+map c1m2_streets +exec 360controller`, the pad created before the game launched): idle noise 246 changed samples, full deflection 11815; the right stick at 0.10, 0.20, 0.22, 0.24 and 0.26 left the camera still (209 to 297), at 0.28 it moved (2534), then 4019, 6296 and 7096 at 0.30, 0.35 and 0.40. The game's inner deadzone is between 0.26 and 0.28, which is the XInput constant of 0.265 the default assumes, and the default floor of 0.285 clears it. Two things the game taught: Source decides at launch whether an XInput controller is present, so a pad created after the game starts is never read (the mouse moved the camera, the pad did not, and the HUD showed the generic JOY3 glyph); the probe therefore waits for the game window and the game is launched second. And a 0.15 hold read 564, between the still and moved thresholds: an animated safe-room prop, not motion. The `--mode nimbus` pass then ran the real app beside the game on the dev machine's own profile (aim stick at sensitivity 46, extremity 34, anti-deadzone left at the default): a full drag sent RX +0.660 and moved the camera (8368 against a noise floor of 154), and a 1 px drag sent RX +0.287 and moved it too (2123). That is the section 4.2 claim measured end to end: the smallest movement Nimbus can make is a movement the game accepts. Before this change a 1 px drag sent about 0.01 and the game discarded it.

---

## Related Documents

- [Game Compatibility](../GAME_COMPATIBILITY.md): where per-title aim assist settings should be documented (section 8)
- [Host Mode & Input Isolation](HOST_MODE_ISOLATION.md): section 8 on the XIM resemblance, which section 10 depends on
- [Windows Mouse Filter Plan](WINDOWS_MOUSE_FILTER_PLAN.md): the cursor relay that absolute aim mode (6.4) would need
- [Hardware Integration](HARDWARE_INTEGRATION.md): the input pipeline this all sits in, and the eye/head tracking path in section 9
- [Whitepaper](../WHITEPAPER.md): the "wrap, don't displace" position that section 9 follows from

## Sources

- [Getting Started with XInput, Microsoft](https://github.com/MicrosoftDocs/win32/blob/docs/desktop-src/xinput/getting-started-with-xinput.md): deadzone constants and the radial deadzone recommendation
- [Steam Input Wiki: Joystick Mouse](https://steaminput.wiki/en/input-styles/joystick-mouse) and [Steam Input Essentials: Joystick Move](https://bryanrumsey.wordpress.com/2019/05/29/steam-input-essentials-eps-8-joystick-move/): anti-deadzone, anti-deadzone buffer, and response curve terminology
- [Stick Drift, Deadzones, and Aim Assist](https://gamepadtest.app/guides/aim-assist-stick-drift) and [What Is Aim Assist, GPADLAB](https://gpadlab.com/learn/glossary/aim-assist): slowdown vs rotational assist, and the stick-input requirement
- [RubberEdge: Reducing Clutching by Combining Position and Rate Control](https://arxiv.org/pdf/0804.0556): position vs rate control, and control-display gain
- [RICOCHET Anti-Cheat Season 02 Update](https://www.callofduty.com/blog/2026/02/call-of-duty-black-ops-7-ricochet-anti-cheat-season-02) and [Apex Legends XIM & Cronus ban wave 2026](https://www.versaciboosts.com/blog/apex-legends-xim-cronus-titan-two-ban-2026-safe-boosting): the accessibility exemption and the machine-driven-behavior detection class
- [Helping Hand challenge pattern, Accessible Games](https://accessible.games/accessible-player-experiences/challenge-patterns/helping-hand/): the vocabulary used in section 8
- [Tobii Eye Tracker 5](https://gaming.tobii.com/product/eye-tracker-5/) and [QuadStick community discussion](https://groups.google.com/g/quadstick/c/rrgQD5uB1RI): the eye-tracking-plus-switch-input pattern
