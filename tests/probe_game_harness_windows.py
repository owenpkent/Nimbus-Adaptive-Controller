"""
Game harness runner (throwaway, not Nimbus code).

Launches a real game from a recipe in ``tests/games/``, owns the pad it
reads, and measures what stick and button input do to it, in the game's own
units where the recipe has a console oracle. ``docs/vision/GAME_TEST_HARNESS.md``
is the plan and the results log; ``tests/game_harness.py`` is the machinery.

Actuators
---------
pad (default)
    A ViGEm pad of the harness's own. Calibrates the game:

    G0  launch: the game window appears within the recipe's timeout, and the
        recipe's ``window`` (size, position, borderless) is applied and re-applied
        as the game loads, recording whether it took
    G1  ready: the oracle answers (a pose is read: from the console log on
        Source, from the clipboard on Arma 3)
    G2  reset: the player is put back within 2 units and 1 degree; with no
        console but ``reset_buttons`` in the recipe, a second of left stick
        then the buttons brings the picture back to a reference frame
    G3  idle: several seconds of nothing leave the pose alone; the noise
        floor and the motion thresholds come from all of them
    G4  yaw control: right stick full right turns more than 10 degrees, the recipe's way
    G5  yaw sweep: degrees per second at each magnitude; the game's deadzone;
        the frame verdict beside the ground truth for each step, from the
        measured motion (tests/frame_motion.py) rather than a changed count
    G6  yaw left: opposite sign, rate within 25 percent of the right turn
    G7  pitch: right stick up changes the pitch by more than 1 degree
    G8  move: left stick up for a second moves the player more than the recipe's
        walk_min_units (20 by default; metres on Arma 3)
    G9  button: the pad button bound to an echo marker reaches the oracle
    G10 release: nothing is stuck afterwards
    G11 latency: the first pose sample whose yaw moved after the stick went on
    G12 calibration: yaw against hold time at several magnitudes, and walk
        against hold time; ``--write-calibration`` writes the table into
        ``src/spectator/calibrations/<game>.json`` for the primitives
nimbus
    The real Nimbus app in-process, driving the same environment through its
    widgets, on a throwaway copy of the bundled profile (``--profile <id>``
    runs an existing one instead); what the bridge sent is recorded beside
    what the game did, and the expected values come from the bridge's own
    resolved parameters, never from a number in the docs:

    N0  app: window, ViGEm mode, an rx/ry stick found, game launched second and ready
    N1  full drag: turns more than 10 degrees, the bridge sent its ceiling (0.95 bundled)
    N2  one-pixel drag: turns more than 1 degree (the anti-deadzone floor, in degrees)
    N3  release: the bridge reads zero and the pose is stable
    N4  button: a click on the bumper widget produces the echo marker
    N5  left stick: a full drag up moves the player more than 20 units, ceiling sent

    then the Spectator+ v0 primitives through the bridge's runner, planned
    from the calibration G12 wrote and measured by the game:

    then, with --assist, the closed loop of TARGET_AWARE_AIM_PLAN.md phase 1,
    which needs a game whose script publishes where a target is (Arma 3):

    T0  gate: the app refuses this title, an anti-cheat refuses it outright,
        and the harness's own allowlist entry is what permits the run
    T1  geometry: pixels per degree from the oracle's own sightings and from
        phase correlation on the frames, and the loop's measured dead time
    T2  snap: a target 5, 15 and 30 degrees off centre, settled in degrees
    T3  snap onto a walking target, with the velocity lead and without
    T4  silence: a target on screen, nothing asked for, nothing sent
    T5  the user's own stick cancels a snap and is what the driver is left with
    T6  the target is deleted mid-snap: the command decays to zero
    T7  the kill switch releases the axes
    T8  the servo never exceeds the stick ceiling it was given

    P0  the bridge's runner loads this game's calibration
    P1  turn right 90 degrees, P2 turn left 45, P3 turn right 10: within
        15 percent or 5 degrees
    P4  walk 100 units: within 20 percent or 10 units
    P5  stop: a long walk cut short releases the stick and the player stops

Run (from the repo root, venv with PySide6; ViGEmBus installed;
Steam able to sign in without a prompt)::

    venv\\Scripts\\python tests\\probe_game_harness_windows.py --game left4dead2 --actuator pad
    venv\\Scripts\\python tests\\probe_game_harness_windows.py --game left4dead2 --actuator nimbus
    venv\\Scripts\\python tests\\probe_game_harness_windows.py --game arma3_nobe --actuator nimbus --assist

Run them one at a time: each session's pad has to be player one, and the
runner quits the game at the end unless ``--keep-game`` is given. A recipe
whose ``reset_pose`` is null takes the first pose read as the session's reset
pose and prints it; ``--write-reset-pose`` writes it into the recipe.

Expected values. A recipe's ``expect`` block bands what a check measured
last time (degrees, units or pixels per second, with a tolerance), and the
runner records a ``<check>e`` line for each band, so a run fails when the
game's answer changed and not only when it stopped answering.
``--write-expect`` fills the block from a good run for the checks in
``--expect-checks`` (G5 at ``--expect-mags``, G7, G8, N2, N5 by default),
merging with what is there, so the pad run and the Nimbus run each keep
their own.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

if sys.platform != "win32":
    sys.exit("Windows only")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from frame_motion import describe, moving_peak, rot_coherent  # noqa: E402
from game_harness import (  # noqa: E402
    FRAMES_DIR, GameEnv, NimbusActuator, PadActuator, load_recipe, on_qt, pose_delta, pose_error, wrap_deg,
    write_expect, write_reset_pose,
)

DEFAULT_SWEEP = "0.20,0.26,0.28,0.30,0.40,0.60,0.80,1.00"
DEFAULT_CAL_MAGS = "0.40,0.60,1.00"
DEFAULT_CAL_HOLDS = "0.10,0.25,0.50,1.00"
DEFAULT_WALK_HOLDS = "0.25,0.50,1.00"
DEFAULT_EXPECT_MAGS = "0.40,0.60"
DEFAULT_EXPECT_CHECKS = "G5,G7,G8,N2,N5"
# The default band per unit when --write-expect fills a recipe: (percent, absolute floor).
EXPECT_TOL = {"deg_per_s": (15.0, 1.0), "units_per_s": (20.0, 10.0), "px_per_s": (25.0, 10.0)}
RESULTS: List[Dict[str, Any]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "note": note})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({note})" if note else ""), flush=True)


def fmt_pose(p: Optional[Dict[str, Any]]) -> str:
    if not p:
        return "no pose"
    x, y, z = p["pos"]
    pitch, yaw, roll = p["ang"]
    return f"pos=({x:.1f}, {y:.1f}, {z:.1f}) ang=({pitch:.1f}, {yaw:.1f}, {roll:.1f})"


def sgn(v: float) -> int:
    return (v > 0) - (v < 0)


# ---- expected values ------------------------------------------------------------
def measured_rate(env: GameEnv, r: Dict[str, Any], axis: str) -> Optional[tuple]:
    """``(unit, value)`` for a step: degrees, units or pixels per second.

    From the console where there is one (``deg_per_s`` for yaw and pitch,
    ``units_per_s`` for a walk). Otherwise from the frames: the rotation for
    a ``rotate`` kind when it was believed, else the believed moving peak,
    signed along the axis the stick drives (``px_per_s``, horizontal for
    yaw, vertical for pitch, the magnitude for a walk). ``None`` when nothing
    believable was measured, which is what a picture that changed wholesale
    or a turn too far to correlate gives.
    """
    hold = float(r.get("hold") or 1.0)
    if axis == "yaw" and "d_yaw" in r:
        return "deg_per_s", float(r["d_yaw"]) / hold
    if axis == "pitch" and "d_pitch" in r:
        return "deg_per_s", float(r["d_pitch"]) / hold
    if axis == "walk" and "d_horiz" in r:
        return "units_per_s", float(r["d_horiz"]) / hold
    m = r.get("motion")
    if not m:
        return None
    if env.motion_kind == "rotate" and axis == "yaw" and rot_coherent(m, env.thresholds):
        return "deg_per_s", float(m["deg"]) / hold
    p = moving_peak(m, env.thresholds)
    if p is None:
        return None
    if axis == "yaw":
        return "px_per_s", float(p["dx"]) / hold
    if axis == "pitch":
        return "px_per_s", float(p["dy"]) / hold
    return "px_per_s", float(p["px"]) / hold


def note_measured(env: GameEnv, check: str, key: str, rate: Optional[tuple]) -> None:
    """Keep a measured rate for ``--write-expect``."""
    if rate is None:
        return
    env.measured.setdefault(check, {})[key] = {"unit": rate[0], "value": round(float(rate[1]), 3)}


def expect_check(env: GameEnv, check: str, key: str, rate: Optional[tuple]) -> None:
    """Compare a measured rate with the recipe's ``expect`` block for
    ``check`` (and ``key``, a magnitude, or ``-`` for a check with one
    value), recording a line only where the recipe has an expectation.

    This is what turns a run into a regression test: the pass rules say
    the game answered, the band says it answered the way it did last time.
    """
    block = (env.recipe.get("expect") or {}).get(check)
    if not block:
        return
    exp = block if key == "-" else block.get(key)
    if not isinstance(exp, dict):
        return
    unit = next((u for u in EXPECT_TOL if u in exp), None)
    if unit is None:
        return
    target = float(exp[unit])
    tol_pct = float(exp.get("tol_pct", EXPECT_TOL[unit][0]))
    tol_abs = float(exp.get("tol_abs", EXPECT_TOL[unit][1]))
    tol = max(abs(target) * tol_pct / 100.0, tol_abs)
    label = f"{check}e {'' if key == '-' else key + ' '}expected {unit} {target:+.1f} within {tol:.1f}"
    if rate is None:
        record(label, False, "nothing comparable was measured")
    elif rate[0] != unit:
        record(label, False, f"measured {rate[0]} {rate[1]:+.1f}, a different unit")
    else:
        record(label, abs(rate[1] - target) <= tol, f"measured {rate[1]:+.1f}")


def build_expect(env: GameEnv, args: argparse.Namespace) -> Dict[str, Any]:
    """The ``expect`` block ``--write-expect`` writes: what this run measured
    for the checks in ``--expect-checks``, with the default bands."""
    wanted = {c.strip() for c in args.expect_checks.split(",") if c.strip()}

    def band(e: Dict[str, Any]) -> Dict[str, Any]:
        return {e["unit"]: e["value"], "tol_pct": EXPECT_TOL[e["unit"]][0], "tol_abs": EXPECT_TOL[e["unit"]][1]}

    out: Dict[str, Any] = {}
    for check, keys in env.measured.items():
        if check not in wanted or not keys:
            continue
        out[check] = band(keys["-"]) if "-" in keys else {k: band(e) for k, e in keys.items()}
    return out


# ---- shared checks -------------------------------------------------------------
def launch_and_ready(env: GameEnv, launch_name: str, ready_name: str) -> bool:
    ok = env.launch()
    record(launch_name, ok, f"window after {env.window_at - env.launched_at:.0f}s" if ok else
           f"no window titled {env.recipe['title']!r} owned by {env.recipe.get('process')} "
           f"within {env.recipe.get('window_timeout_s')}s")
    if not ok:
        return False
    env.run_sequence()
    ready = env.wait_ready()
    p = env.pose() if ready else None
    console = env.oracle.has_pose
    log = getattr(env.oracle, "log", None)      # only the Source oracle reads a console log
    log_ok = (not console) or log is None or log.exists()
    record(ready_name, ready and log_ok and (not console or p is not None),
           (f"ready after {env.ready_at - env.launched_at:.0f}s; " if ready else "not ready in time; ")
           + (f"console log {'present' if log_ok else 'missing'}; " if log is not None else "")
           + fmt_pose(p))
    return ready and log_ok


def establish_reset_pose(env: GameEnv, args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    p = env.pose()
    if env.oracle.reset_pose is None and p is not None:
        env.set_reset_pose(p)
        print(f"[harness] no reset_pose in the recipe; using the first pose read: {fmt_pose(p)}", flush=True)
        print(f'[harness] recipe line: "reset_pose": {{"pos": {[round(v, 3) for v in p["pos"]]}, '
              f'"ang": {[round(v, 3) for v in p["ang"]]}}}', flush=True)
        if args.write_reset_pose:
            write_reset_pose(env.recipe, p)
            print(f"[harness] wrote reset_pose into {env.recipe['_path']}", flush=True)
    if not env.oracle.has_pose and env.can_reset():
        # A game with reset buttons: press them once, and what they leave on
        # screen is the reference every later reset has to reproduce.
        env.reset()
        env.capture_reference()
        print("[harness] reset buttons pressed and the reference frame captured", flush=True)
    return p


def check_reset(env: GameEnv, name: str) -> None:
    ok = env.reset()
    q = env.pose()
    rp = env.oracle.reset_pose
    dist, dang = pose_error(rp, q) if (q and rp) else (math.inf, math.inf)
    angle_note = ""
    if q and rp and not env.oracle.resets_pitch:
        # only the yaw can be set on this game (Arma 3: setDir); the pitch is
        # wherever the last test left it and is not part of the reset
        dang = abs(wrap_deg(q["ang"][1] - rp["ang"][1]))
        angle_note = " (yaw only; this game cannot reset pitch)"
    record(f"{name} reset puts the player back within 2 units and 1 degree",
           ok and dist < 2.0 and dang < 1.0, f"dist={dist:.2f} dang={dang:.2f}{angle_note} {fmt_pose(q)}")


def check_reset_frames(env: GameEnv, name: str) -> None:
    """The reset check for a game with no console: a second of left stick
    moves the view off the reference, the recipe's reset buttons are pressed,
    and the picture has to be back on the reference frame (its motion from
    the reference reads STILL). Measured before it was built: on Halo Wars
    the pair landed 40 percent of samples from the stored view against 71
    for a lost camera, which is why the rule is a motion verdict and not a
    percentage."""
    drift = env.step({"ly": 1.0}, 1.0, "reset_drift")
    ok = env.reset()
    lr = env.last_reset or {}
    record(f"{name} reset: the reset buttons bring the view back to the reference after a second of left stick",
           ok, f"drift {env.motion_note(drift)}; landing changed={lr.get('changed')} "
               f"{describe(lr.get('motion'))} -> {lr.get('verdict')}")


def measure_idle(env: GameEnv, label: str, samples: int) -> List[Dict[str, Any]]:
    """``samples`` idle seconds; the noise floor and the motion thresholds come from all of them."""
    recs = [env.step({}, 1.0, label if i == 0 else f"{label}_{i + 1}") for i in range(max(1, int(samples)))]
    env.set_noise(recs)
    return recs


def record_idle(env: GameEnv, name: str, recs: List[Dict[str, Any]], with_thresholds: bool) -> None:
    posed = [r for r in recs if r.get("pose_before") and r.get("pose_after")]
    if posed:
        stable = all(abs(r["d_yaw"]) < 0.5 and r["d_horiz"] < 1.0 for r in posed)
        note = "; ".join(f"d_yaw={r['d_yaw']:+.2f} d_horiz={r['d_horiz']:.2f}" for r in posed)
    else:
        stable = not env.oracle.has_pose
        note = "no pose"
    note += f"; changed={[r['changed'] for r in recs]}; " + describe(recs[0].get("motion"))
    if with_thresholds:
        t = env.thresholds.as_dict()
        note += (f"; floor {env.noise}, a move is a coherent shift past {t['min_px']:.0f} px"
                 + (f" or a rotation past {t['min_deg']:.1f} deg" if env.motion_kind == "rotate" else ""))
    seconds = "one second" if len(recs) == 1 else f"{len(recs)} seconds"
    record(f"{name} idle: {seconds} of nothing leaves the pose alone", stable, note)


def check_idle(env: GameEnv, name: str, label: str, set_noise: bool, samples: int = 1) -> List[Dict[str, Any]]:
    recs = measure_idle(env, label, samples) if set_noise else [env.step({}, 1.0, label)]
    record_idle(env, name, recs, set_noise)
    return recs


def yaw_note(r: Dict[str, Any], env: GameEnv) -> str:
    if "d_yaw" not in r:
        return env.motion_note(r) + " (no pose)"
    return (f"d_yaw={r['d_yaw']:+.2f} deg in {r['hold']:.2f}s = {r['d_yaw'] / r['hold']:+.1f} deg/s; "
            + env.motion_note(r))


# ---- pad -----------------------------------------------------------------------
def pad_checks(env: GameEnv, args: argparse.Namespace) -> None:
    recipe = env.recipe
    sign = int(recipe.get("turn_right_sign", -1))
    walk_min = float(recipe.get("walk_min_units", 20.0))   # Source units by default; metres on Arma 3
    hold = float(args.hold)
    mags = [float(m) for m in args.sweep.split(",") if m.strip()]
    console = env.oracle.has_pose

    expect_mags = {f"{float(m):.2f}" for m in args.expect_mags.split(",") if m.strip()}
    establish_reset_pose(env, args)
    idle_recs: Optional[List[Dict[str, Any]]] = None
    if console:
        check_reset(env, "G2")
    elif env.can_reset():
        # the floor first, because the reset's landing is judged with it
        idle_recs = measure_idle(env, "idle", args.idle_samples)
        check_reset_frames(env, "G2")
    else:
        record("G2 reset", True, "frame_diff oracle and no reset_buttons in the recipe, skipped")

    # GS walk survey: turn the reset pose to face the longest clear run, so
    # the walk checks and the walk calibration are not capped by a wall
    if console and args.survey_walk and env.oracle.reset_pose:
        base = env.oracle.reset_pose
        rows = []
        for yaw in range(0, 360, 45):
            env.set_reset_pose({"pos": list(base["pos"]), "ang": [0.0, wrap_deg(float(yaw)), 0.0]})
            env.reset()
            r = env.step({"ly": 1.0}, 1.0, f"survey_{yaw}", with_frame=False)
            rows.append((yaw, float(r.get("d_horiz", 0.0))))
            print(f"    yaw {yaw:>3}: {rows[-1][1]:6.1f} units in 1.0s", flush=True)
        best = max(rows, key=lambda t: t[1])
        chosen = {"pos": [round(v, 3) for v in base["pos"]], "ang": [0.0, wrap_deg(float(best[0])), 0.0]}
        env.set_reset_pose(chosen)
        env.reset()
        record("GS walk survey: the reset pose faces the longest clear run", best[1] > 150.0,
               f"best yaw {best[0]} at {best[1]:.1f} units; " + ", ".join(f"{y}:{d:.0f}" for y, d in rows))
        if args.write_reset_pose:
            write_reset_pose(env.recipe, chosen)
            print(f"[harness] wrote the surveyed reset_pose into {env.recipe['_path']}", flush=True)
    if idle_recs is None:
        idle_recs = measure_idle(env, "idle", args.idle_samples)
    record_idle(env, "G3", idle_recs, True)
    t = env.thresholds.as_dict()
    print(f"[harness] idle floor {env.noise} over {len(idle_recs)} samples; a move is a coherent shift past "
          f"{t['min_px']:.0f} px" + (f", a rotation past {t['min_deg']:.1f} deg" if env.motion_kind == "rotate" else "")
          + f", {t['whole_cells']} of 16 cells changed, or {t['spread_cells']} cells and "
          f"{t['spread_factor']:.0f}x the floor", flush=True)

    # G4 control
    r = env.step({"rx": 1.0}, hold, "rx_1.00")
    if console and "d_yaw" in r:
        ok = abs(r["d_yaw"]) > 10.0 and sgn(r["d_yaw"]) == sign
    else:
        ok = r["verdict"] == "MOVED"
    record("G4 yaw control: right stick full right turns more than 10 degrees, the recipe's way", ok, yaw_note(r, env))
    env.reset()
    if not ok:
        print("[harness] the game is not reading the right stick; the sweep would measure nothing", flush=True)
        return

    # G5 sweep
    rows = []
    for m in mags:
        r = env.step({"rx": m}, hold, f"rx_{m:.2f}")
        env.reset()
        dyaw = r.get("d_yaw")
        key = f"{m:.2f}"
        rate = measured_rate(env, r, "yaw")
        if key in expect_mags:
            note_measured(env, "G5", key, rate)
        rows.append({"magnitude": m, "d_yaw": dyaw, "rate": (dyaw / hold) if dyaw is not None else None,
                     "changed": r["changed"], "verdict": r["verdict"], "motion": r.get("motion"), "measured": rate})
        print(f"    rx={m:.2f}  " + (f"d_yaw={dyaw:+8.2f}  {dyaw / hold:+7.1f} deg/s  " if dyaw is not None else "")
              + f"changed={r['changed']:>6}  {describe(r.get('motion'))} -> {r['verdict']}", flush=True)
        expect_check(env, "G5", key, rate)
    moved = [row for row in rows if row["d_yaw"] is not None and abs(row["d_yaw"]) > 1.0]
    first = min((row["magnitude"] for row in moved), default=None)
    still = [row["magnitude"] for row in rows if row["d_yaw"] is not None and abs(row["d_yaw"]) <= 1.0
             and (first is None or row["magnitude"] < first)]
    disagreements = [row["magnitude"] for row in rows if row["d_yaw"] is not None
                     and ((row["verdict"] == "MOVED") != (abs(row["d_yaw"]) > 1.0))]
    env.sweep_rows = rows
    if console:
        record("G5 yaw sweep: the game's right-stick deadzone",
               first is not None,
               (f"first moved at {first:.2f}" if first is not None else "never moved")
               + (f", still at {max(still):.2f}" if still else "")
               + f"; frame verdict disagreed with the pose at {disagreements or 'no magnitude'}")
    else:
        moved_v = [row["magnitude"] for row in rows if row["verdict"] == "MOVED"]
        record("G5 yaw sweep (frame verdicts only)", bool(moved_v),
               f"first MOVED at {min(moved_v):.2f}" if moved_v else "never MOVED")

    # G6 left
    right = next((row for row in rows if abs(row["magnitude"] - 0.6) < 1e-6), None)
    if right is None or right["d_yaw"] is None:
        r_right = env.step({"rx": 0.6}, hold, "rx_0.60")
        env.reset()
        right = {"magnitude": 0.6, "d_yaw": r_right.get("d_yaw"), "rate": (r_right.get("d_yaw") or 0) / hold}
    r = env.step({"rx": -0.6}, hold, "rx_-0.60")
    env.reset()
    if console and "d_yaw" in r and right["d_yaw"] is not None and right["d_yaw"] != 0:
        ratio = abs(r["d_yaw"]) / abs(right["d_yaw"])
        ok = sgn(r["d_yaw"]) == -sgn(right["d_yaw"]) and 0.75 <= ratio <= 1.25
        note = yaw_note(r, env) + f"; right turn at 0.60 was {right['d_yaw']:+.2f}, ratio {ratio:.2f}"
    else:
        ok = r["verdict"] == "MOVED"
        note = yaw_note(r, env)
    record("G6 yaw left: opposite sign, rate within 25 percent of the right turn", ok, note)

    # G7 pitch
    r = env.step({"ry": 0.6}, hold, "ry_0.60")
    env.reset()
    rate = measured_rate(env, r, "pitch")
    note_measured(env, "G7", "-", rate)
    if console and "d_pitch" in r:
        ok = abs(r["d_pitch"]) > 1.0
        note = (f"d_pitch={r['d_pitch']:+.2f} deg ({'up' if r['d_pitch'] < 0 else 'down'} for stick up); "
                + env.motion_note(r))
    else:
        ok = r["verdict"] == "MOVED"
        note = env.motion_note(r)
    record("G7 pitch: right stick up changes the pitch by more than 1 degree", ok, note)
    expect_check(env, "G7", "-", rate)

    # G8 move
    r = env.step({"ly": 1.0}, 1.0, "ly_1.00")
    env.reset()
    rate = measured_rate(env, r, "walk")
    note_measured(env, "G8", "-", rate)
    if console and "d_horiz" in r:
        ok = r["d_horiz"] > walk_min
        note = f"d_horiz={r['d_horiz']:.1f} units in 1.0s, d_z={r['d_z']:+.1f}; " + env.motion_note(r)
    else:
        ok = r["verdict"] == "MOVED"
        note = env.motion_note(r)
    record("G8 move: left stick up for a second moves the player more than 20 units", ok, note)
    expect_check(env, "G8", "-", rate)

    # G9 button
    echo = env.oracle.echo_buttons()
    if not recipe.get("pad_buttons_reach_game", True):
        # A measured property of the game, not a fault in the run: Half-Life 2
        # acts on the pad's axes and never on its buttons, with the binds
        # confirmed in place by key_listboundkeys and the stick turning the view
        # between one ignored press and the next.
        record("G9 button", True, "this game does not read the pad's buttons at all; skipped")
        echo = []
    elif not echo:
        record("G9 button", not console, "no console oracle, skipped" if not console else "the recipe binds no echo button")
    for bid, marker in echo:
        env.front()
        off = env.log_offset()
        t0 = time.monotonic()
        env.actuator.apply({"buttons": [bid]})
        ok = env.oracle.wait_echo(marker, off, 2.0)
        dt = (time.monotonic() - t0) * 1000.0
        time.sleep(0.1)
        env.actuator.release()
        record(f"G9 button: pad button {bid} reaches the oracle as {marker}", ok,
               f"{dt:.0f} ms" if ok else "no echo within 2 s")
    time.sleep(0.3)

    # G10 release
    check_idle(env, "G10", "idle_after", set_noise=False)

    # G11 latency
    if console:
        env.front()
        p0 = env.oracle.pose()
        samples = []
        first = None
        t0 = time.monotonic()
        env.actuator.apply({"rx": 1.0})
        while time.monotonic() - t0 < 1.5:
            p = env.oracle.pose(timeout=0.5, tries=1)
            t = time.monotonic() - t0
            d = wrap_deg(p["ang"][1] - p0["ang"][1]) if (p and p0) else None
            samples.append((round(t * 1000), None if d is None else round(d, 2)))
            if d is not None and abs(d) > 0.5:
                first = t
                break
        env.actuator.release()
        env.reset()
        record("G11 latency: the first pose sample whose yaw moved after the stick went on", first is not None,
               (f"{first * 1000:.0f} ms (a bound: one key press and a log read per sample); " if first else "")
               + f"samples (ms, deg)={samples}")

    # G12 calibration: yaw against hold time at several magnitudes, and walk
    # against hold time. This is the table a turn or walk primitive is planned
    # from (src/spectator/calibration.py), so the holds are short as well as
    # long: games ramp stick input, and one rate would not do.
    if console:
        cal_mags = [float(m) for m in args.cal_mags.split(",") if m.strip()]
        cal_holds = [float(h) for h in args.cal_holds.split(",") if h.strip()]
        walk_holds = [float(h) for h in args.walk_holds.split(",") if h.strip()]
        yaw_table: Dict[str, List[List[float]]] = {}
        monotone = True
        for m in cal_mags:
            rows: List[List[float]] = []
            for h in cal_holds:
                r = env.step({"rx": m}, h, f"cal_rx_{m:.2f}_{h:.2f}", with_frame=False)
                env.reset()
                rows.append([h, round(float(r.get("d_yaw", 0.0)), 3)])
            yaw_table[f"{m:.2f}"] = rows
            amounts = [abs(a) for _, a in rows]
            monotone = monotone and all(b >= a for a, b in zip(amounts, amounts[1:]))
            print(f"    rx={m:.2f}  " + "  ".join(f"{h:.2f}s: {a:+.1f}" for h, a in rows), flush=True)
        walk_rows: List[List[float]] = []
        for h in walk_holds:
            r = env.step({"ly": 1.0}, h, f"cal_ly_1.00_{h:.2f}", with_frame=False)
            env.reset()
            walk_rows.append([h, round(float(r.get("d_horiz", 0.0)), 2)])
        print("    ly=1.00  " + "  ".join(f"{h:.2f}s: {u:.1f}" for h, u in walk_rows), flush=True)
        walk_ok = bool(walk_rows) and walk_rows[-1][1] > walk_min and all(
            b >= a for (_, a), (_, b) in zip(walk_rows, walk_rows[1:]))
        record("G12 calibration: yaw and walk grow with hold time at every magnitude", monotone and walk_ok,
               f"yaw at {list(yaw_table)} for holds {cal_holds}; walk {walk_rows}")
        env.calibration = {"game": recipe["name"], "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "turn_right_sign": sign, "yaw": yaw_table, "walk": {"1.00": walk_rows}}
        if args.write_calibration:
            from src.spectator.calibration import CALIBRATIONS_DIR
            os.makedirs(CALIBRATIONS_DIR, exist_ok=True)
            path = os.path.join(CALIBRATIONS_DIR, f"{recipe['name']}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(env.calibration, fh, indent=4)
                fh.write("\n")
            print(f"[harness] wrote {path}", flush=True)


# ---- nimbus --------------------------------------------------------------------
def nimbus_checks(env: GameEnv, act: NimbusActuator, args: argparse.Namespace) -> None:
    recipe = env.recipe
    sign = int(recipe.get("turn_right_sign", -1))
    walk_min = float(recipe.get("walk_min_units", 20.0))   # Source units by default; metres on Arma 3
    hold = float(args.hold)
    console = env.oracle.has_pose

    establish_reset_pose(env, args)
    if console:
        check_reset(env, "N0b")
        check_idle(env, "N0c", "nimbus_idle", set_noise=True, samples=args.idle_samples)
    elif env.can_reset():
        idle_recs = measure_idle(env, "nimbus_idle", args.idle_samples)
        check_reset_frames(env, "N0b")
        record_idle(env, "N0c", idle_recs, True)
    else:
        check_idle(env, "N0c", "nimbus_idle", set_noise=True, samples=args.idle_samples)

    # N1 full drag. The expected value is the bridge's own ceiling for this
    # stick (0.95 on the bundled profile; a user's copy may differ), so the
    # check is that the game turned and that the bridge sent what it says it
    # would, not a number copied from the docs.
    exp_full = act.expected("right", 1.0)
    r = env.step({"rx": 1.0}, hold, "nimbus_rx_full")
    env.reset()
    rate = measured_rate(env, r, "yaw")
    note_measured(env, "N1", "-", rate)
    sent = float((r.get("sent") or {}).get("right_x", 0.0))
    sent_ok = abs(sent - exp_full) <= 0.02
    if console and "d_yaw" in r:
        ok = abs(r["d_yaw"]) > 10.0 and sgn(r["d_yaw"]) == sign and sent_ok
    else:
        ok = r["verdict"] == "MOVED" and sent_ok
    record("N1 full drag turns more than 10 degrees and the bridge sent its ceiling", ok,
           f"sent RX={sent:+.3f} (bridge ceiling {exp_full:.3f}, travel {act.travel('right'):.0f} px); "
           + yaw_note(r, env))
    expect_check(env, "N1", "-", rate)
    if not ok and not sent_ok:
        print("[harness] the bridge did not send its own ceiling: the drag missed the stick or hit another widget",
              flush=True)
    elif not ok:
        # The bridge sent the right value and the game did nothing with it.
        # Two causes, and the frames say which: the game is not reading this
        # pad (is it player one?), or the ready sequence never reached the
        # world and the checks are being run against a menu, which a game
        # with an animated menu background passes the live-picture test in.
        print("[harness] the bridge sent its ceiling and the game did not move: either the game is not "
              "reading Nimbus's pad (is it player one?) or the ready sequence stopped in a menu; "
              "check the saved before/after frames", flush=True)

    # N2 one-pixel drag: the anti-deadzone floor, in degrees
    exp_nudge = act.expected("right", float(args.nudge) / act.travel("right"))
    r = env.step({"rx_px": float(args.nudge)}, hold, "nimbus_rx_1px")
    env.reset()
    rate = measured_rate(env, r, "yaw")
    note_measured(env, "N2", "-", rate)
    sent = float((r.get("sent") or {}).get("right_x", 0.0))
    sent_ok = abs(sent - exp_nudge) <= 0.02
    if console and "d_yaw" in r:
        moved = abs(r["d_yaw"]) > 1.0 and sgn(r["d_yaw"]) == sign
    else:
        moved = r["verdict"] == "MOVED"
    # A game whose own threshold sits above the bridge's floor (Half-Life 2
    # at 0.30 to 0.40, Halo Wars at 0.40 to 0.60, both measured by the pad
    # sweep) can never pass "the camera turned", and a check that can never
    # pass tests nothing. Such a recipe says so with floor_moves_camera
    # false, and the check becomes: the bridge sent its floor and the game,
    # as measured, did not act on it. A game that starts moving would fail
    # it, which is the regression worth catching there.
    if recipe.get("floor_moves_camera", True):
        ok = moved and sent_ok
        record(f"N2 a {args.nudge:g} px drag turns the camera by more than 1 degree", ok,
               f"sent RX={sent:+.3f} (bridge floor {exp_nudge:.3f}); " + yaw_note(r, env))
    else:
        ok = (not moved) and sent_ok
        record(f"N2 a {args.nudge:g} px drag sends the bridge's floor and, this game's threshold sitting above it "
               f"(measured), the camera stays still", ok,
               f"sent RX={sent:+.3f} (bridge floor {exp_nudge:.3f}); " + yaw_note(r, env))
    expect_check(env, "N2", "-", rate)

    # N3 release
    sent_now = act.sent()
    zero = all(abs(float(sent_now.get(k, 0.0))) < 1e-6 for k in ("left_x", "left_y", "right_x", "right_y"))
    check_idle(env, "N3a", "nimbus_idle_after", set_noise=False)
    record("N3 release: the bridge reads zero on every stick", zero, f"sent={sent_now}")

    # N4 button
    echo = env.oracle.echo_buttons()
    if not recipe.get("pad_buttons_reach_game", True):
        # The same measured property G9 skips on: no widget click can produce a
        # button the game never reads, so this says nothing about the bridge.
        record("N4 button", True, "this game does not read the pad's buttons at all; skipped")
        echo = []
    elif not echo:
        record("N4 button", not console, "no console oracle, skipped" if not console else "the recipe binds no echo button")
    for bid, marker in echo:
        if bid not in act.buttons:
            record(f"N4 button: the profile has no widget for button {bid}", False, f"widgets={act.buttons}")
            continue
        env.front()
        off = env.log_offset()
        t0 = time.monotonic()
        act.apply({"buttons": [bid]})
        ok = env.oracle.wait_echo(marker, off, 2.0)
        dt = (time.monotonic() - t0) * 1000.0
        time.sleep(0.1)
        act.release()
        record(f"N4 button: a click on the {act.buttons[bid]} widget produces {marker}", ok,
               f"{dt:.0f} ms" if ok else "no echo within 2 s")
    time.sleep(0.3)

    # N5 left stick
    if "left" not in act.sticks:
        record("N5 left stick", False, "the profile has no joystick mapped to x/y")
        return
    exp_left = act.expected("left", 1.0)
    r = env.step({"ly": 1.0}, 1.0, "nimbus_ly_full")
    env.reset()
    rate = measured_rate(env, r, "walk")
    note_measured(env, "N5", "-", rate)
    sent = (r.get("sent") or {})
    ly = float(sent.get("left_y", 0.0))
    sent_ok = abs(ly - exp_left) <= 0.02
    if console and "d_horiz" in r:
        ok = r["d_horiz"] > walk_min and sent_ok
        note = (f"sent LX={float(sent.get('left_x', 0)):+.3f} LY={ly:+.3f} (bridge ceiling {exp_left:.3f}); "
                f"d_horiz={r['d_horiz']:.1f} units in 1.0s; " + env.motion_note(r))
    else:
        ok = r["verdict"] == "MOVED" and sent_ok
        note = f"sent LY={ly:+.3f} (bridge ceiling {exp_left:.3f}); " + env.motion_note(r)
    record("N5 left stick: a full drag up moves the player more than 20 units and the bridge sent its ceiling",
           ok, note)
    expect_check(env, "N5", "-", rate)

    primitive_checks(env, act, args)
    if args.assist:
        assist_checks(env, act, args)


def primitive_checks(env: GameEnv, act: NimbusActuator, args: argparse.Namespace) -> None:
    """Spectator+ v0 primitives through the bridge's runner, measured by the game."""
    from src.spectator.calibration import CALIBRATIONS_DIR
    recipe = env.recipe
    sign = int(recipe.get("turn_right_sign", -1))
    walk_min = float(recipe.get("walk_min_units", 20.0))   # Source units by default; metres on Arma 3
    if not env.oracle.has_pose:
        record("P0 primitives", True, "no console oracle to measure a turn with, skipped")
        return
    game = str(recipe.get("calibration") or recipe["name"])
    cal_path = os.path.join(CALIBRATIONS_DIR, f"{game}.json")
    runner = on_qt(lambda: act.bridge.get_spectator())
    loaded = on_qt(lambda: runner.use_game(game))
    record("P0 the bridge's Spectator+ runner loads this game's calibration", loaded,
           cal_path + ("" if loaded else " is missing: run the pad actuator with --write-calibration first"))
    if not loaded:
        return

    cases = [
        ("P1 turn right 90", lambda: runner.turn(90.0), "d_yaw", sign * 90.0),
        ("P2 turn left 45", lambda: runner.turn(-45.0), "d_yaw", -sign * 45.0),
        ("P3 turn right 10", lambda: runner.turn(10.0), "d_yaw", sign * 10.0),
        ("P4 walk 100 units", lambda: runner.walk(100.0), "d_horiz", 100.0),
    ]
    for name, call, key, expected in cases:
        env.front()
        p0 = env.oracle.pose()
        plan = on_qt(call)
        if not plan:
            record(name, False, "the runner refused the plan (busy, or no calibration row)")
            continue
        deadline = time.monotonic() + float(plan["hold"]) + 3.0
        while on_qt(lambda: runner.busy) and time.monotonic() < deadline:
            time.sleep(0.03)
        done = not on_qt(lambda: runner.busy)
        time.sleep(0.4)
        # Deliberately no front() between the two reads of a delta. It warps
        # the cursor to the middle of the game window, and a game reading the
        # mouse for look takes that as a turn, so the one case where it would
        # help (the foreground was lost, and the pose key went elsewhere) is
        # also the case where it corrupts the measurement rather than just
        # failing to take it. front() belongs before p0, where anything it
        # disturbs lands ahead of the baseline.
        p1 = env.oracle.pose()
        d = pose_delta(p0, p1)
        env.reset()
        got = d.get(key)
        tol = max(5.0, 0.15 * abs(expected)) if key == "d_yaw" else max(10.0, 0.2 * abs(expected))
        ok = done and got is not None and abs(got - expected) <= tol
        record(f"{name}: within {tol:.0f} of {expected:+.0f}", ok,
               (f"got {got:+.1f}" if got is not None else "no pose")
               + f" with axis {plan['axis_value']:+.2f} held {plan['hold']:.3f}s"
               + ("" if done else "; the runner never finished"))

    # P5 stop: a long walk cut short releases the stick and the player stops
    env.front()
    p0 = env.oracle.pose()
    plan = on_qt(lambda: runner.walk(400.0))
    time.sleep(0.3)
    on_qt(runner.stop)
    still_busy = on_qt(lambda: runner.busy)
    sent = act.sent()
    time.sleep(0.5)
    p1 = env.oracle.pose()
    before = pose_delta(p0, p1).get("d_horiz", 0.0)
    after = env.step({}, 1.0, "primitive_stop_idle", with_frame=False).get("d_horiz", 1e9)
    env.reset()
    ok = plan is not None and not still_busy and abs(float(sent.get("left_y", 0.0))) < 1e-6 and after < 1.0
    record("P5 stop: a walk cut short releases the stick and the player stops", ok,
           f"moved {before:.1f} units before the stop, {after:.1f} in the next second; sent LY={sent.get('left_y')}")

# ---- assist: the closed loop (TARGET_AWARE_AIM_PLAN.md phase 1) ------------------
def assist_servo(env: GameEnv, act: NimbusActuator, dead_time: float, ceiling: float, **kw):
    """A servo for this game: its calibrated rates, the measured geometry and
    dead time, and the user's own stick ceiling."""
    from src.spectator.calibration import GameCalibration
    from src.spectator.servo import Servo, steady_rates
    recipe = env.recipe
    cal = GameCalibration.load(str(recipe.get("calibration") or recipe["name"]))
    servo = Servo(steady_rates(cal), env.assist_focal, dead_time=dead_time, ceiling=ceiling, **kw)
    pitch_rate = float(recipe.get("pitch_rate_deg_per_s") or 0.0)
    yaw_rate = servo.rate_for_magnitude(0.6)
    if pitch_rate > 0 and yaw_rate > 0:
        servo.pitch_scale = pitch_rate / yaw_rate
    return servo


def assist_dead_time(env: GameEnv, act: NimbusActuator, magnitude: float = 0.6, tries: int = 3) -> Optional[float]:
    """Seconds between commanding a rate and the loop seeing the view move.

    The whole round trip as the servo meets it: the driver, the game's next
    poll, its render, and the oracle's own publishing rate. Measured rather
    than assumed, because a servo told less than the truth overshoots by
    roughly the difference (tests/test_assist_servo.py).
    """
    samples = []
    for _ in range(tries):
        env.front()
        base = env.oracle.pose()
        if not base:
            continue
        on_qt(lambda: act.bridge.setAxis("rx", magnitude))
        t0 = time.monotonic()
        seen = None
        while time.monotonic() - t0 < 1.5:
            p = env.oracle.pose(timeout=0.2, tries=1)
            if p and abs(wrap_deg(p["ang"][1] - base["ang"][1])) > 0.5:
                seen = time.monotonic() - t0
                break
        on_qt(lambda: act.bridge.setAxis("rx", 0.0))
        time.sleep(0.5)
        env.reset()
        if seen:
            samples.append(seen)
    if not samples:
        return None
    return sorted(samples)[len(samples) // 2]


def assist_snap(env: GameEnv, act: NimbusActuator, runner, servo, cfg: Dict[str, Any],
                max_hold: float, settle_deg: float = 1.0, wait: bool = True) -> Dict[str, Any]:
    """Run one ``look_at`` through the bridge's own runner and score it."""
    from game_harness import clipboard_read
    from src.spectator.targets import ScriptTargetSource, TargetSelector
    height = env.h * env.assist_y_span
    source = ScriptTargetSource(lambda: clipboard_read(tries=3), (env.w, height),
                                crosshair=(env.w / 2.0, height / 2.0))
    selector = TargetSelector(fov_px=float(env.w))
    settle_px = abs(servo.px_from_deg(settle_deg))
    dwell = float(cfg.get("settle_ms", 120.0))
    started = time.monotonic()
    plan = on_qt(lambda: runner.look_at(source, servo, max_hold=max_hold + dwell / 1000.0 + 0.2,
                                        settle_px=settle_px, settle_ms=dwell,
                                        cancel_px=float(cfg.get("cancel_px", 6.0)),
                                        selector=selector))
    out: Dict[str, Any] = {"plan": plan, "started": started, "runner": runner, "servo": servo,
                           "settle_px": settle_px, "budget": max_hold}
    if plan is None:
        out.update({"elapsed": 0.0, "look": {}, "overshoot": None, "settled_at": None})
        return out
    if wait:
        deadline = started + max_hold + dwell / 1000.0 + 2.5
        while on_qt(lambda: runner.busy) and time.monotonic() < deadline:
            time.sleep(0.01)
        out.update(assist_result(env, runner, started, settle_px))
    return out


def assist_keep(env: GameEnv, label: str, res: Dict[str, Any], before: Optional[Dict[str, float]],
                after: Optional[Dict[str, float]]) -> None:
    """Keep a closed-loop run in the results file, trace and all."""
    servo = res.get("servo")
    env.assist_runs.append({"check": label, "reason": res.get("reason"), "settled": res.get("settled"),
                            "elapsed_ms": round(res.get("elapsed", 0.0) * 1000, 1),
                            "overshoot_deg": res.get("overshoot"),
                            "before": before, "after": after,
                            "kp": round(servo.kp, 3) if servo else None,
                            "floor_deg": round(servo.settle_floor_deg, 3) if servo else None,
                            "ceiling": round(servo.ceiling, 3) if servo else None,
                            "lead": bool(servo.lead) if servo else None,
                            "samples": (res.get("look") or {}).get("samples")})


def assist_result(env: GameEnv, runner, started: float, settle_px: float = 0.0) -> Dict[str, Any]:
    """What a finished ``look_at`` did: when it arrived, why it ended, how far past.

    ``settled_at`` is the tick after which the error never left the
    tolerance again, which is what settling means. The primitive itself runs
    on for the dwell that confirms it, so its own elapsed time is always
    that much longer and is not the number to gate on.
    """
    look = on_qt(lambda: dict(runner.last_look))
    samples = [s for s in (look.get("samples") or []) if "deg_x" in s]
    over = None
    settled_at = None
    if samples:
        sign = 1.0 if samples[0]["deg_x"] >= 0 else -1.0
        over = max(0.0, max(-sign * float(s["deg_x"]) for s in samples))
        for sample in samples:
            inside = math.hypot(float(sample.get("ex", 0.0)), float(sample.get("ey", 0.0))) <= settle_px
            if not inside:
                settled_at = None
            elif settled_at is None:
                settled_at = float(sample["t"])
    return {"elapsed": time.monotonic() - started, "look": look, "overshoot": over, "settled_at": settled_at,
            "reason": look.get("reason"), "settled": bool(look.get("settled"))}


def assist_note(res: Dict[str, Any], target: Optional[Dict[str, float]]) -> str:
    look = res.get("look") or {}
    arrived = res.get("settled_at")
    bits = [(f"arrived at {arrived * 1000:.0f} ms, " if arrived is not None else "never arrived, ")
            + f"{res.get('reason')} at {res.get('elapsed', 0.0) * 1000:.0f} ms",
            f"{look.get('ticks', 0)} ticks, {look.get('frames', 0)} with a target"]
    if res.get("overshoot") is not None:
        bits.append(f"overshoot {res['overshoot']:.2f} deg")
    if target:
        bits.append(f"left {target['bearing']:+.2f} deg off, {target['elevation']:+.2f} in pitch")
    return "; ".join(bits)


def assist_place(env: GameEnv, act: NimbusActuator, oracle, bearing: float, range_m: float,
                 tries: int = 4) -> bool:
    """Foreground the game, put a target at that bearing, and get it on screen.

    Two things make the last part necessary. ``front()`` warps the cursor to
    the middle of the window, and a game that reads the mouse for look takes
    that as a turn in pitch as well as yaw. And on Arma 3 the pose's own
    pitch field does not follow the aim, so ``level_pitch`` reads zero and
    does nothing however far the view is tilted: the first run of these
    checks measured a "5 degree" snap that was really 20 degrees of pitch
    error, and later ones could not find the target at all. The target
    itself is the reference that works, because the mission publishes its
    elevation whether or not the engine draws it.
    """
    env.front()
    time.sleep(0.2)
    if not oracle.spawn_target(bearing, range_m):
        return False
    rate = float(env.recipe.get("pitch_rate_deg_per_s", 25.0))
    for _ in range(tries):
        seen = oracle.target()
        if not seen:
            return False
        if seen.get("visible"):
            return True
        elevation = float(seen.get("elevation") or 0.0)
        if abs(elevation) < 1.0:
            return False     # not the pitch keeping it off screen, so nothing here will fix it
        act.apply({"ry": 0.6 if elevation > 0 else -0.6})
        time.sleep(max(0.05, min(1.5, abs(elevation) / rate)))
        act.release()
        time.sleep(0.3)
    seen = oracle.target()
    return bool(seen and seen.get("visible"))


def assist_checks(env: GameEnv, act: NimbusActuator, args: argparse.Namespace) -> None:
    """Target-aware assistance, phase 1: the closed loop against a script oracle.

    T0 the gate, T1 the geometry and the loop's own dead time, T2 a snap onto
    a target standing still, T3 onto one walking, T4 silence, T5 the user
    taking over, T6 the target gone, T7 the kill switch, T8 the ceiling.
    Everything here needs a target oracle, which for now means Arma 3's
    generated mission; any other game skips.
    """
    from src.spectator.policy import AssistPolicy, foreground_window, running_anticheat, running_process_names
    from src.spectator.servo import fov_deg
    oracle = env.oracle
    if not hasattr(oracle, "spawn_target"):
        record("T0 assist", True, "this game publishes no targets; the closed loop needs an oracle, skipped")
        return
    cfg = dict(env.recipe.get("assist") or {})
    range_m = float(cfg.get("range_m", 30.0))
    runner = on_qt(lambda: act.bridge.get_spectator())

    # T0 the gate: who may run this at all
    env.front()
    title, image = foreground_window()
    running = running_process_names()
    app = AssistPolicy().check(title, image, running)
    harness = AssistPolicy(allow_test_entries=True).check(title, image, running)
    guarded = AssistPolicy(allow_test_entries=True).check(title, image, list(running) + ["BEService_x64.exe"])
    anticheat = running_anticheat(running)
    record("T0a gate: the app refuses this title, which is a harness entry and not a shipped one",
           not app.allowed, app.reason)
    record("T0b gate: an anti-cheat refuses it whatever the allowlist says", not guarded.allowed, guarded.reason)
    if anticheat:
        record(f"T0c gate: {anticheat} is running, so nothing target-aware runs here at all",
               not harness.allowed, harness.reason)
        print("[harness] this run is the anti-cheat control: the policy refused before any loop started. "
              "The recipe without it is where the closed loop is measured.", flush=True)
        return
    record("T0c gate: with no anti-cheat running, the harness's own entry allows the loop", harness.allowed,
           harness.reason)
    if not harness.allowed:
        return

    # T1 the geometry: pixels to degrees from the oracle, and from the frames
    fits = []
    for bearing in [float(b) for b in cfg.get("fit_bearings", [-15.0, -5.0, 5.0, 15.0])]:
        env.front()
        time.sleep(0.2)
        if not oracle.spawn_target(bearing, range_m):
            continue
        seen = oracle.target()
        # A unit placed but not drawn (the view pitched away) comes back with
        # no screen position; skip that bearing rather than crash the series.
        if not seen or not seen.get("visible") or seen.get("sx") is None or abs(seen["bearing"]) < 0.5:
            continue
        dx = (seen["sx"] - 0.5) * env.w
        fits.append(dx / math.tan(math.radians(seen["bearing"])))
    oracle.delete_target()
    if not fits:
        record("T1 px/deg: the mission never put a target on screen", False,
               "worldToScreen returned nothing, or the unit was not placed")
        return
    focal = sorted(fits)[len(fits) // 2]
    spread = (max(fits) - min(fits)) / abs(focal) if focal else 9.9
    if focal <= 0:
        record("T1 px/deg: the screen's x axis runs the way the target source assumes", False,
               f"a target to the right of the view came back left of centre ({focal:.0f} px per radian): "
               f"the source would have to mirror x, and every error would otherwise be steered backwards")
        return
    env.assist_focal = focal
    env.measured.setdefault("T1", {})["focal_px"] = {"unit": "px", "value": round(focal, 1)}
    record("T1a px/deg: the oracle's own sightings agree on one focal length", spread <= 0.05,
           f"{focal:.0f} px per radian, {abs(focal) * math.pi / 180.0:.1f} px per degree at the centre, "
           f"a {fov_deg(env.w, focal):.1f} degree field of view across {env.w} px, spread {spread * 100:.1f}%")
    # A small turn on purpose: phase correlation finds the shift modulo any
    # repeating texture's period, and a big turn across Arma's gridded VR
    # ground reads as a fraction of itself (measured: a 21 degree turn came
    # back as 42 px instead of 363). The smallest magnitude the game moves
    # at, held long enough to clear the frame tools' 10 px threshold, is the
    # turn least likely to be aliased.
    env.front()
    r = env.step({"rx": 0.4}, 0.4, "assist_px_per_deg")
    env.reset()
    peak = moving_peak(r.get("motion"), env.thresholds)
    if peak is None or not r.get("d_yaw"):
        record("T1b px/deg: the frames agree with the oracle", True,
               "no believable frame shift to compare with; the oracle's own figure stands")
    else:
        frames = abs(float(peak["dx"])) / abs(math.tan(math.radians(float(r["d_yaw"]))))
        off = abs(frames - focal) / abs(focal)
        record("T1b px/deg: the frames agree with the oracle within 15 percent", off <= 0.15,
               f"{frames:.0f} px per radian from a {r['d_yaw']:+.2f} degree turn ({abs(float(peak['dx'])):.0f} px "
               f"by phase correlation), against {focal:.0f} from the oracle, {off * 100:.1f}% apart")
    env.front()
    time.sleep(0.2)
    if oracle.spawn_target(0.0, range_m):
        before_pitch = oracle.target()
        on_qt(lambda: act.bridge.setAxis("ry", 0.6))
        time.sleep(0.35)
        on_qt(lambda: act.bridge.setAxis("ry", 0.0))
        time.sleep(0.4)
        after_pitch = oracle.target()
        if (before_pitch and after_pitch and before_pitch.get("sy") is not None
                and after_pitch.get("sy") is not None):
            d_el = after_pitch["elevation"] - before_pitch["elevation"]
            d_sy = after_pitch["sy"] - before_pitch["sy"]
            flat = abs(d_sy * env.h / math.tan(math.radians(abs(d_el)))) if abs(d_el) > 0.2 else 0.0
            if flat > 0:
                env.assist_y_span = focal / flat
            record("T1d geometry: the screen's y axis runs down, and the harness measures its scale rather "
                   "than assuming the two axes share one",
                   abs(d_el) > 1.0 and d_el * d_sy < 0 and 0.5 <= env.assist_y_span <= 2.0,
                   f"the stick up moved the view {-d_el:+.1f} degrees up and the target {d_sy * env.h:+.0f} px "
                   f"down a frame {env.h:.0f} px tall, which is {flat:.0f} px per radian against {focal:.0f} "
                   f"across: this game's y fraction spans {env.assist_y_span:.3f} client heights "
                   f"({env.assist_y_span * env.h:.0f} px), and a source that took it for the client height "
                   f"would under-read every vertical error by a quarter")
        else:
            record("T1d geometry: the screen's y axis runs down", False, "no target to watch while pitching")
        oracle.delete_target()
        env.reset()
        env.level_pitch()
    dead = assist_dead_time(env, act)
    if dead is None:
        record("T1c the loop's own dead time", False, "the view never moved for a stick command")
        return
    floor_s = float(cfg.get("dead_time_floor_ms", 50.0)) / 1000.0
    env.assist_dead_time = max(floor_s, min(0.25, dead * float(cfg.get("dead_time_pessimism", 1.4))))
    env.measured.setdefault("T1", {})["dead_time_ms"] = {"unit": "ms", "value": round(dead * 1000.0, 1)}
    record("T1c the loop's own dead time is measured, and is under the 60 to 120 ms the plan budgeted "
           "for a loop that watches a rendered frame",
           0.0 < dead <= 0.20,
           f"{dead * 1000:.0f} ms from the command to the view moving, which is at the resolution of this "
           f"oracle (it streams at 50 Hz, and 0.5 degrees at the magnitude used is 14 ms of turning on its "
           f"own): a script oracle reads the game's state rather than its picture, so this is a floor and "
           f"phase 2's capture path will be far slower. The servo is told "
           f"{env.assist_dead_time * 1000:.0f} ms, the larger of that times "
           f"{float(cfg.get('dead_time_pessimism', 1.4)):.1f} and the recipe's floor, because a servo told "
           f"less than the truth overshoots by about the difference and one told far more is merely slow")

    ceiling = act.expected("right", 1.0)
    snap_ms = float(cfg.get("snap_ms", 600.0)) / 1000.0
    wide_ms = float(cfg.get("wide_snap_ms", 1200.0)) / 1000.0
    settle_deg = float(cfg.get("settle_deg", 1.0))

    # T2 a snap onto a target standing still. The tolerance is the plan's one
    # degree or the servo's own floor, whichever is larger: under that floor
    # the rate wanted is below the game's stick deadzone and the servo
    # commands nothing, so a tighter gate would be a check that cannot pass.
    visible = math.degrees(math.atan(0.92 * (env.w / 2.0) / env.assist_focal))
    for bearing in [float(b) for b in cfg.get("snap_bearings", [5.0, 15.0, 30.0])]:
        if abs(bearing) > visible:
            record(f"T2 snap: a target {bearing:.0f} degrees off centre", True,
                   f"past the edge of the {fov_deg(env.w, env.assist_focal):.0f} degree window this game "
                   f"draws (anything over {visible:.0f} degrees off centre), and nothing may engage a "
                   f"target the user cannot see; skipped")
            continue
        if not assist_place(env, act, oracle, bearing, range_m):
            record(f"T2 snap {bearing:.0f} degrees", False, "the mission did not put a target on screen")
            continue
        budget = snap_ms if abs(bearing) <= 15.0 else wide_ms
        before = oracle.target()
        servo = assist_servo(env, act, env.assist_dead_time, ceiling)
        tol = max(settle_deg, 1.2 * servo.settle_floor_deg)
        res = assist_snap(env, act, runner, servo, cfg, max_hold=budget, settle_deg=tol)
        after = oracle.target()
        arrived = res.get("settled_at")
        ok = (res.get("settled") and arrived is not None and arrived <= budget
              and after is not None and abs(after["bearing"]) <= tol
              and (res.get("overshoot") or 0.0) <= 2.0)
        env.measured.setdefault("T2", {})[f"{bearing:.0f}"] = {
            "unit": "ms", "value": round((arrived if arrived is not None else res.get("elapsed", 0.0)) * 1000, 1)}
        assist_keep(env, f"T2 {bearing:.0f} deg", res, before, after)
        record(f"T2 snap: a target {bearing:.0f} degrees off settles inside {budget * 1000:.0f} ms, "
               f"within {tol:.2f} degrees, without overshooting", bool(ok),
               (f"from {before['bearing']:+.1f} deg; " if before else "") + assist_note(res, after)
               + f"; the servo stops commanding under {servo.settle_floor_deg:.2f} degrees, "
                 f"which is this game's stick deadzone over the gain")
        oracle.delete_target()
        env.reset()

    # T3 a snap onto one that is walking, with the velocity lead and without
    walk_range = float(cfg.get("walk_range_m", 20.0))
    walk_bearing = float(cfg.get("walk_bearing", 20.0))
    for speed in [float(s) for s in cfg.get("walk_speeds", [1.0, 2.0])]:
        results = {}
        for lead in (True, False):
            if not assist_place(env, act, oracle, walk_bearing, walk_range):
                continue
            oracle.walk_target(speed)
            time.sleep(0.5)
            servo = assist_servo(env, act, env.assist_dead_time, ceiling, lead=lead)
            res = assist_snap(env, act, runner, servo, cfg, max_hold=wide_ms,
                              settle_deg=max(1.5, 1.2 * servo.settle_floor_deg))
            after = oracle.target()
            results[lead] = (res, after)
            assist_keep(env, f"T3 {speed:.0f} m/s {'lead' if lead else 'no lead'}", res, None, after)
            oracle.delete_target()
            env.reset()
        if True not in results:
            record(f"T3 snap onto a target walking at {speed:.0f} m/s", False, "no target was placed")
            continue
        res, after = results[True]
        crossing = abs(math.degrees(math.atan(speed / max(1.0, walk_range))))
        tol = max(1.5, 1.2 * res["servo"].settle_floor_deg)
        ok = (res.get("settled") and res.get("settled_at") is not None
              and after is not None and abs(after["bearing"]) <= tol)
        record(f"T3 snap: a target walking at {speed:.0f} m/s at {walk_range:.0f} m "
               f"(about {crossing:.1f} degrees a second) settles within {tol:.2f} degrees", bool(ok),
               assist_note(res, after))
        if False in results:
            blind, blind_after = results[False]
            with_lead = abs(after["bearing"]) if after else 9.9
            without = abs(blind_after["bearing"]) if blind_after else 9.9
            floor = res["servo"].settle_floor_deg
            standing = crossing / res["servo"].kp
            # A comparison is only a comparison where the thing being compared
            # is larger than the smallest correction the servo can make. Half
            # as much again as the floor is the margin: below it the two runs
            # differ by less than one command.
            if without < 1.5 * floor:
                record(f"T3b the velocity lead at {speed:.0f} m/s", True,
                       f"turning it off left {without:.2f} degrees, against the {floor:.2f} this game's stick "
                       f"deadzone stops the servo correcting below, so there is nothing here to tell the two "
                       f"apart with ({with_lead:.2f} with the lead; a proportional term with no prediction "
                       f"would have left {standing:.2f}). The lead is worth what the loop's real delay costs, "
                       f"and a script oracle's delay is 16 ms: the simulation and phase 2's capture path are "
                       f"where it earns its place")
            else:
                record(f"T3b the velocity lead is what closes the standing error at {speed:.0f} m/s",
                       with_lead <= without,
                       f"{with_lead:.2f} degrees off with the lead, {without:.2f} without, against the "
                       f"{standing:.2f} a proportional term alone would leave")

    # T4 silence: a target on screen, nothing asked for, nothing sent
    if assist_place(env, act, oracle, 10.0, range_m):
        act.release()
        time.sleep(0.2)
        before = env.oracle.pose()
        worst = 0.0
        for _ in range(30):
            sent = act.sent()
            worst = max(worst, abs(float(sent.get("right_x", 0.0))), abs(float(sent.get("right_y", 0.0))))
            time.sleep(0.05)
        after_pose = env.oracle.pose()
        moved = abs(pose_delta(before, after_pose).get("d_yaw", 0.0)) if before and after_pose else 9.9
        record("T4 silence: a target on screen with nothing asked for sends nothing and moves nothing",
               worst < 1e-6 and moved <= 1.0 and not on_qt(lambda: runner.busy),
               f"largest right stick value {worst:.4f} over 1.5 s, view moved {moved:.2f} degrees")
        oracle.delete_target()
        env.reset()

    # T5 the user takes over
    if assist_place(env, act, oracle, 25.0, range_m):
        servo = assist_servo(env, act, env.assist_dead_time, ceiling)
        started = assist_snap(env, act, runner, servo, cfg, max_hold=2.0, wait=False)
        time.sleep(0.25)
        nudge = float(cfg.get("cancel_drag_px", 10.0))
        act.apply({"rx_px": nudge})
        time.sleep(0.08)
        busy = on_qt(lambda: runner.busy)
        sent = float(act.sent().get("right_x", 0.0))
        res = assist_result(env, runner, started["started"], started["settle_px"])
        expected = act.expected("right", nudge / act.travel("right"))
        act.release()
        record("T5 the user takes over: a drag on their own stick stops the snap and the driver is left "
               "holding the user's vector, not the loop's",
               (not busy) and res.get("reason") == "user" and abs(sent - expected) <= 0.02,
               f"{res.get('reason')}; the driver has RX={sent:+.3f} and the bridge's own value for a "
               f"{nudge:.0f} px drag is {expected:+.3f}")
        oracle.delete_target()
        env.reset()

    # T6 the target disappears
    if assist_place(env, act, oracle, 20.0, range_m):
        servo = assist_servo(env, act, env.assist_dead_time, ceiling)
        started = assist_snap(env, act, runner, servo, cfg, max_hold=3.0, wait=False)
        time.sleep(0.3)
        oracle.delete_target()
        gone_at = time.monotonic()
        while on_qt(lambda: runner.busy) and time.monotonic() - gone_at < 2.0:
            time.sleep(0.01)
        released = time.monotonic() - gone_at
        res = assist_result(env, runner, started["started"], started["settle_px"])
        sent = act.sent()
        tail = [float(s.get("mx", 0.0)) for s in (res.get("look", {}).get("samples") or [])][-4:]
        record("T6 the target goes: the command decays to nothing and the primitive ends uncompleted",
               res.get("reason") == "lost" and released <= 0.45
               and abs(float(sent.get("right_x", 0.0))) < 1e-6,
               f"{res.get('reason')} {released * 1000:.0f} ms after the unit was deleted, last commands "
               f"{[round(v, 3) for v in tail]}, driver RX={float(sent.get('right_x', 0.0)):+.3f}")
        env.reset()

    # T7 the kill switch
    if assist_place(env, act, oracle, 25.0, range_m):
        servo = assist_servo(env, act, env.assist_dead_time, ceiling)
        started = assist_snap(env, act, runner, servo, cfg, max_hold=3.0, wait=False)
        time.sleep(0.25)
        t0 = time.monotonic()
        on_qt(lambda: act.bridge.stopControllerMode())
        busy = on_qt(lambda: runner.busy)
        took = time.monotonic() - t0
        sent = act.sent()
        res = assist_result(env, runner, started["started"], started["settle_px"])
        record("T7 kill switch: the path Ctrl+Alt+F12 takes releases the axes at once",
               (not busy) and abs(float(sent.get("right_x", 0.0))) < 1e-6 and res.get("reason") == "stopped",
               f"{res.get('reason')} in {took * 1000:.0f} ms, driver RX={float(sent.get('right_x', 0.0)):+.3f}")
        oracle.delete_target()
        env.reset()

    # T8 the ceiling
    low = min(0.5, ceiling)
    far = min(float(cfg.get("ceiling_bearing", 30.0)), visible)
    if assist_place(env, act, oracle, far, range_m):
        servo = assist_servo(env, act, env.assist_dead_time, low)
        res = assist_snap(env, act, runner, servo, cfg, max_hold=wide_ms, settle_deg=settle_deg)
        sent_max = max([abs(float(s.get("mx", 0.0))) for s in (res.get("look", {}).get("samples") or [])] or [9.9])
        after = oracle.target()
        assist_keep(env, "T8 ceiling", res, None, after)
        record(f"T8 ceiling: with the stick capped at {low:.2f} the servo never asks for more",
               sent_max <= low + 1e-9,
               f"largest magnitude {sent_max:.3f} against a {low:.2f} ceiling ({ceiling:.2f} is what this "
               f"widget gives the user); at that cap this game turns at {servo.rate_for_magnitude(low):.0f} "
               f"degrees a second, so a {far:.0f} degree snap cannot finish inside the budget and is not "
               f"meant to; " + assist_note(res, after))
        oracle.delete_target()
        env.reset()

# ---- drivers -------------------------------------------------------------------
def summary(env: Optional[GameEnv], args: argparse.Namespace) -> int:
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed", flush=True)
    if env is not None:
        out = env.write({"checks": RESULTS, "sweep": getattr(env, "sweep_rows", None),
                         "calibration": getattr(env, "calibration", None)})
        print(f"wrote {out}", flush=True)
        if args.write_expect:
            exp = build_expect(env, args)
            if exp:
                write_expect(env.recipe, exp)
                print(f"[harness] wrote expect for {sorted(exp)} into {env.recipe['_path']}", flush=True)
            else:
                print("[harness] nothing this run measured could be banded; expect not written", flush=True)
    return 1 if failed or not RESULTS else 0


def run_pad(args: argparse.Namespace, recipe: Dict[str, Any]) -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841  (the screen grab needs one)
    try:
        act = PadActuator()       # before the game launches: Source looks for the pad at start-up
    except Exception as exc:      # noqa: BLE001
        print(f"no ViGEm pad: {exc}")
        return 2
    env = GameEnv(recipe, act, frames_dir=args.frames, skip_top=args.skip_top, save_frames=not args.no_frames)
    try:
        if launch_and_ready(env, "G0 launch: the game window appears", "G1 ready: the oracle answers"):
            pad_checks(env, args)
    except Exception as exc:      # noqa: BLE001
        traceback.print_exc()
        record("run crashed", False, f"{type(exc).__name__}: {exc}")
    finally:
        env.close(keep_game=args.keep_game)
        act.close()
    return summary(env, args)


def run_nimbus(args: argparse.Namespace, recipe: Dict[str, Any]) -> int:
    act = NimbusActuator(profile=args.profile)
    if not act.start():
        return 2
    holder: Dict[str, GameEnv] = {}

    def scenario() -> None:
        err = act.prepare()
        env = GameEnv(recipe, act, call=on_qt, frames_dir=args.frames, skip_top=args.skip_top,
                      save_frames=not args.no_frames)
        holder["env"] = env
        if err:
            record("N0 app", False, err)
            return
        try:
            if not launch_and_ready(env, "N0 launch: the game window appears (Nimbus's pad already exists)",
                                    "N0 ready: the oracle answers"):
                return
            act.place_beside(env)
            record("N0 app: window, ViGEm, the profile's sticks and buttons", True,
                   f"sticks={act.sticks} buttons={sorted(act.buttons)}")
            nimbus_checks(env, act, args)
        finally:
            env.close(keep_game=args.keep_game)

    act.run(scenario)
    return summary(holder.get("env"), args)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", default="left4dead2", help="recipe name in tests/games/ or a path to a recipe")
    ap.add_argument("--actuator", choices=("pad", "nimbus"), default="pad")
    ap.add_argument("--hold", type=float, default=1.0, help="seconds to hold each stick step")
    ap.add_argument("--sweep", default=DEFAULT_SWEEP, help="right-stick magnitudes for the yaw sweep")
    ap.add_argument("--nudge", type=float, default=1.0, help="nimbus: pixels of drag for the floor check")
    ap.add_argument("--profile", default=None,
                    help="nimbus: an existing profile id to run instead of the throwaway copy of the bundled one")
    ap.add_argument("--keep-game", action="store_true", help="leave the game running at the end")
    ap.add_argument("--assist", action="store_true",
                    help="nimbus: run the target-aware closed loop (T0 to T8) against the recipe's target "
                         "oracle, after the primitives; needs a game that publishes targets (Arma 3)")
    ap.add_argument("--write-reset-pose", action="store_true",
                    help="write the first pose read into the recipe when it has none")
    ap.add_argument("--cal-mags", default=DEFAULT_CAL_MAGS, help="G12: right-stick magnitudes for the hold-time table")
    ap.add_argument("--cal-holds", default=DEFAULT_CAL_HOLDS, help="G12: hold times for the yaw table")
    ap.add_argument("--walk-holds", default=DEFAULT_WALK_HOLDS, help="G12: hold times for the walk table")
    ap.add_argument("--write-calibration", action="store_true",
                    help="G12: write the table into src/spectator/calibrations/<game>.json")
    ap.add_argument("--survey-walk", action="store_true",
                    help="after G2, walk a second in eight directions and turn the reset pose to the clearest one "
                         "(with --write-reset-pose, into the recipe)")
    ap.add_argument("--idle-samples", type=int, default=3,
                    help="idle seconds the noise floor and the motion thresholds are taken from")
    ap.add_argument("--expect-mags", default=DEFAULT_EXPECT_MAGS,
                    help="G5 magnitudes --write-expect bands (the stable ones, not the folding stop)")
    ap.add_argument("--expect-checks", default=DEFAULT_EXPECT_CHECKS,
                    help="checks --write-expect bands; add N1 for the full drag where it is repeatable")
    ap.add_argument("--write-expect", action="store_true",
                    help="write what this run measured into the recipe's expect block, with the default bands")
    ap.add_argument("--skip-top", type=int, default=0)
    ap.add_argument("--no-frames", action="store_true", help="do not save before/after frames")
    ap.add_argument("--frames", default=FRAMES_DIR)
    args = ap.parse_args()
    recipe = load_recipe(args.game)
    print(f"[harness] {recipe['name']} ({recipe['title']}), oracle {recipe.get('oracle', {}).get('type', 'frame_diff')}, "
          f"actuator {args.actuator}", flush=True)
    return run_nimbus(args, recipe) if args.actuator == "nimbus" else run_pad(args, recipe)


if __name__ == "__main__":
    sys.exit(main())
