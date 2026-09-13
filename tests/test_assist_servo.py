"""
Checks on ``src/spectator/servo.py``, the control law behind target-aware
assistance. Pure Python, no hardware, no game, no Qt: the plant is
simulated here (an integrator with dead time, driven through the real
Arma 3 calibration) so the one engineering unknown in the plan, whether a
rate-controlled stick can be servoed through 100 ms of dead time without
oscillating, has an answer before anybody launches a game.

The answer is yes, but not with a proportional loop: the two checks that
failed here first, and the fixes they forced, are in ``servo.py``'s
docstring. A plain proportional servo slow enough not to overshoot takes
over a second to settle a 30 degree snap; with the calibration used as a
plant model to subtract the turn already in flight, the same snap settles
in a third of a second and a 15 degree one inside the plan's 600 ms.

What is checked:

- the calibrated rate table inverts monotonically, returns nothing under
  the game's own deadzone, and never exceeds the user's ceiling
- pixels to degrees is a pinhole and round-trips
- the gain closes the stated fraction of the error per dead time and stays
  well under the stability bound for an integrator with delay
- the simulated loop settles 5, 15 and 30 degree errors inside their
  budgets with no overshoot, at dead times from 50 to 200 ms, and still
  converges when the plant is 30 percent off the table, has half the delay
  it was told, or ramps into every turn
- the prediction never commands a turn away from a target the game has not
  reached, and a measured overshoot is still corrected
- a moving target is tracked closer with the velocity lead than without
- a lost target holds, then decays to zero, inside 250 ms
- ``blend`` (the A1 and A2 tiers) returns the user's vector unchanged with
  no target, zero with a zero user vector, and never lengthens it

Run from the repo root (the fast runner does this)::

    venv\\Scripts\\python -m tests.test_assist_servo
"""
from __future__ import annotations

import math
import sys
from collections import deque
from typing import Dict, List, Optional, Tuple

from src.spectator.calibration import GameCalibration
from src.spectator.servo import (
    Servo, blend, focal_from_px_per_deg, focal_px, fov_deg, steady_rates,
)

PASSES = 0
FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSES, FAILS
    if ok:
        PASSES += 1
    else:
        FAILS += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


# The dev machine's game window and Arma 3's measured response, so the
# numbers below are the ones the harness will meet.
WIDTH, HEIGHT = 1280.0, 720.0
ARMA = GameCalibration.load("arma3")
RATES = steady_rates(ARMA)
FOCAL = focal_px(WIDTH, 70.0)


def make_servo(**kw) -> Servo:
    params = dict(dead_time=0.1, ceiling=0.95, pitch_scale=0.69)
    params.update(kw)
    return Servo(RATES, FOCAL, **params)


def simulate(servo: Servo, error_deg: float, target_rate: float = 0.0, dt: float = 1.0 / 60.0,
             duration: float = 1.5, true_delay: Optional[float] = None, split: float = 0.5,
             gain: float = 1.0, ramp: float = 0.0) -> List[Tuple[float, float, float]]:
    """Run the servo against an integrating plant with dead time.

    The plant is the game: it turns at the calibrated rate for whatever
    magnitude it is sent, and the loop is late at both ends, the servo
    seeing an error that is ``split`` of the delay old and its commands
    taking the rest of it to reach the view.

    Parameters
    ----------
    servo : Servo
        The servo under test; its ``dead_time`` is what it believes.
    error_deg : float
        Where the target starts, in degrees off the centre.
    target_rate : float
        Degrees per second the target crosses the view at.
    true_delay : float, optional
        The delay the plant actually has. Default: what the servo believes,
        which is the case where the model is right.
    gain : float
        How much faster than the calibration the plant really turns.
    ramp : float
        Seconds of first-order lag on the plant's rate, for a game that
        accelerates into a turn rather than starting at its full rate.

    Returns
    -------
    list of tuple
        ``(t, true error in degrees, magnitude sent)`` per tick.
    """
    delay = servo.dead_time if true_delay is None else float(true_delay)
    steps = max(1, int(round(delay / dt)))
    seen_steps = int(steps * split)
    act_steps = steps - seen_steps
    seen: deque = deque([float(error_deg)] * seen_steps)
    pipe: deque = deque()
    camera, target, turning = 0.0, float(error_deg), 0.0
    trace: List[Tuple[float, float, float]] = []
    servo.reset()
    for step in range(int(duration / dt)):
        err = target - camera
        seen.append(err)
        shown = seen.popleft() if seen_steps else err
        mx, _ = servo.command((servo.px_from_deg(shown), 0.0), dt, age=seen_steps * dt)
        pipe.append((math.copysign(servo.rate_for_magnitude(mx), mx) if mx else 0.0) * gain)
        wanted = pipe.popleft() if len(pipe) > act_steps else 0.0
        turning = wanted if ramp <= 0 else turning + (wanted - turning) * (1.0 - math.exp(-dt / ramp))
        camera += turning * dt
        target += target_rate * dt
        trace.append((step * dt, err, mx))
    return trace


def settle_time(trace: List[Tuple[float, float, float]], tol_deg: float) -> Optional[float]:
    """When the error last entered ``tol_deg`` and stayed there, or None."""
    when = None
    for t, err, _ in trace:
        if abs(err) > tol_deg:
            when = None
        elif when is None:
            when = t
    return when


def overshoot(trace: List[Tuple[float, float, float]]) -> float:
    """How far past zero the error went, in degrees, on the other side of where it started."""
    if not trace:
        return 0.0
    sign = 1.0 if trace[0][1] >= 0 else -1.0
    return max(0.0, max(-sign * err for _, err, _ in trace))


print("The calibrated rate table")
check("the Arma 3 calibration gives a rate for every magnitude", sorted(RATES) == [0.4, 0.6, 1.0],
      ", ".join(f"{m:.2f}: {r:.1f} deg/s" for m, r in RATES.items()))
check("the rates rise with magnitude", list(RATES.values()) == sorted(RATES.values()))
servo = make_servo()
samples = [servo.magnitude_for_rate(r) for r in range(0, 400, 5)]
check("magnitude_for_rate is monotone", all(b >= a for a, b in zip(samples, samples[1:])))
check("a rate under the game's deadzone commands nothing",
      servo.magnitude_for_rate(servo.min_rate * 0.9) == 0.0 and servo.magnitude_for_rate(0.0) == 0.0,
      f"min rate {servo.min_rate:.2f} deg/s at magnitude {min(RATES):.2f}")
check("the smallest rate it will command is the smallest calibrated magnitude",
      abs(servo.magnitude_for_rate(servo.min_rate) - min(RATES)) < 1e-9)
check("nothing exceeds the ceiling", max(samples) <= 0.95 + 1e-9, f"max {max(samples):.3f}")
low = make_servo(ceiling=0.5)
check("a lower ceiling clamps every command", max(low.magnitude_for_rate(r) for r in range(0, 400, 5)) <= 0.5 + 1e-9)
check("rate_for_magnitude inverts magnitude_for_rate",
      all(abs(servo.magnitude_for_rate(servo.rate_for_magnitude(m)) - m) < 1e-6 for m in (0.40, 0.55, 0.75, 0.95)))
check("a table row that does not rise is dropped rather than averaged",
      sorted(Servo({0.4: 10.0, 0.6: 8.0, 1.0: 50.0}, FOCAL).rates) == [0.4, 1.0])

print("Pixels, degrees and the gain")
square = Servo(RATES, focal_px(WIDTH, 90.0))
check("a 90 degree field of view puts its edge at half the width",
      abs(square.deg_from_px(WIDTH / 2.0) - 45.0) < 1e-9, f"focal {square.focal:.1f} px")
check("fov_deg inverts focal_px", abs(fov_deg(WIDTH, focal_px(WIDTH, 70.0)) - 70.0) < 1e-9)
check("degrees round-trip through pixels",
      all(abs(servo.deg_from_px(servo.px_from_deg(d)) - d) < 1e-9 for d in (0.5, 5.0, 15.0, 30.0)))
check("focal_from_px_per_deg agrees with focal_px near the centre",
      abs(focal_from_px_per_deg(servo.px_from_deg(1.0)) - FOCAL) < 0.2,
      f"{servo.px_from_deg(1.0):.2f} px per degree at the centre")
bound = math.pi / (2.0 * servo.dead_time)
check("the gain closes the stated fraction of the error per dead time",
      abs(servo.kp * servo.dead_time - 0.6) < 1e-9, f"Kp {servo.kp:.2f}/s at {servo.dead_time * 1000:.0f} ms")
check("the gain stays well under the dead time's stability bound",
      servo.kp < bound / 2.0, f"Kp {servo.kp:.2f}/s, bound {bound:.2f}/s")
check("a longer dead time lowers the gain", make_servo(dead_time=0.2).kp < servo.kp)

print("The loop, simulated (an integrator with dead time, the Arma 3 table)")
# The plant a snap will really meet: it ramps a little into a turn, it is a
# fifth faster than the table says, and its delay is shorter than what the
# servo was told, which is the pessimism the harness applies to the latency
# it measures. Over-stating the dead time costs settling time; under-stating
# it is what overshoots, so the harness always rounds the wrong way on
# purpose.
REAL = {"true_delay": 0.067, "ramp": 0.05, "gain": 1.2}
for angle, budget in ((5.0, 0.6), (15.0, 0.6), (30.0, 1.0)):
    s = make_servo()
    trace = simulate(s, angle, **REAL)
    settled = settle_time(trace, 1.0)
    over = overshoot(trace)
    final = abs(trace[-1][1])
    check(f"a {angle:.0f} degree error settles inside {budget * 1000:.0f} ms with no overshoot",
          settled is not None and settled <= budget and final <= 1.0 and over <= 2.0,
          f"settled {settled if settled is None else round(settled, 3)} s, final {final:.2f} deg, "
          f"overshoot {over:.2f} deg")
blind = simulate(make_servo(predict=False), 15.0, **REAL)
seeing = simulate(make_servo(predict=True), 15.0, **REAL)
check("the prediction is what makes that possible",
      (settle_time(seeing, 1.0) or 9.9) < (settle_time(blind, 1.0) or 9.9) and overshoot(seeing) < overshoot(blind),
      f"with it {settle_time(seeing, 1.0)} s and {overshoot(seeing):.2f} deg of overshoot, "
      f"without it {settle_time(blind, 1.0)} s and {overshoot(blind):.2f} deg")
check("with the model right, a static target is approached from one side only",
      all(m >= -1e-9 for _, _, m in simulate(make_servo(), 20.0)),
      "the prediction never commands a turn away from a target the game says it has not reached")
past = simulate(make_servo(), 20.0, true_delay=0.067, gain=2.2, duration=2.0)
check("a measured overshoot is corrected, which is the only thing that may reverse the stick",
      min(e for _, e, _ in past) < -1.0 and any(m < 0 for _, _, m in past) and abs(past[-1][1]) <= 1.0,
      f"a plant 2.2 times the table's rate went {abs(min(e for _, e, _ in past)):.1f} degrees past and came "
      f"back to {abs(past[-1][1]):.2f}; past about 2.4 times it limit-cycles, which is a calibration to redo")
for tau in (0.05, 0.1, 0.15, 0.2):
    s = make_servo(dead_time=tau)
    tol = max(1.0, 1.2 * s.settle_floor_deg)
    trace = simulate(s, 15.0, true_delay=tau * 0.7, ramp=0.03)
    check(f"with {tau * 1000:.0f} ms of dead time it converges without oscillating",
          settle_time(trace, tol) is not None and overshoot(trace) <= 2.0,
          f"settled {settle_time(trace, tol)} s, overshoot {overshoot(trace):.2f} deg, "
          f"floor {s.settle_floor_deg:.2f} deg")
for label, plant in (("30 percent faster than the table", {"gain": 1.3}),
                     ("30 percent slower than the table", {"gain": 0.7}),
                     ("half the delay it was told", {"true_delay": 0.05}),
                     ("ramping hard into every turn", {"ramp": 0.08, "true_delay": 0.067})):
    trace = simulate(make_servo(), 15.0, **plant)
    check(f"a plant {label} still converges", settle_time(trace, 1.5) is not None and overshoot(trace) <= 2.5,
          f"settled {settle_time(trace, 1.5)} s, overshoot {overshoot(trace):.2f} deg")
floor = make_servo().settle_floor_deg
check("the error it can no longer correct is the game's own deadzone over the gain",
      abs(floor - make_servo().min_rate / make_servo().kp) < 1e-9 and abs(floor - 0.68) < 0.05,
      f"{floor:.2f} degrees for Arma 3 at 100 ms of dead time")
check("the command never exceeds the ceiling in a real run",
      max(abs(m) for _, _, m in simulate(make_servo(), 30.0, **REAL)) <= 0.95 + 1e-9)
tight = make_servo(ceiling=0.45)
check("a user with a low ceiling is still never exceeded",
      max(abs(m) for _, _, m in simulate(tight, 30.0, duration=2.0, **REAL)) <= 0.45 + 1e-9)

print("A moving target")
with_lead = simulate(make_servo(lead=True), 10.0, target_rate=6.0, duration=2.0, **REAL)
without = simulate(make_servo(lead=False), 10.0, target_rate=6.0, duration=2.0, **REAL)
err_lead = max(abs(e) for _, e, _ in with_lead[-30:])
err_plain = max(abs(e) for _, e, _ in without[-30:])
check("the velocity lead tracks a moving target closer than proportional alone",
      err_lead < err_plain and err_lead <= 1.5,
      f"{err_lead:.2f} deg with the lead, {err_plain:.2f} without, target crossing at 6 deg/s")

print("Losing the target")
s = make_servo()
s.command((s.px_from_deg(10.0), 0.0), 1.0 / 60.0)
held = s.last_command
kept, kept_up = s.hold(0.10), s.gave_up
mid, mid_up = s.hold(0.10), s.gave_up
gone, gone_up = s.hold(0.06), s.gave_up
check("the last command is held for 150 ms", kept == held and not kept_up, f"{kept}")
check("it decays part way at 200 ms", 0.0 < abs(mid[0]) < abs(held[0]) and not mid_up, f"{mid}")
check("it is zero and given up by 260 ms", gone == (0.0, 0.0) and gone_up, f"lost for {s.lost_for:.2f} s")
s.command((s.px_from_deg(10.0), 0.0), 1.0 / 60.0)
check("a fresh command clears the loss timer", s.lost_for == 0.0 and not s.gave_up)

print("Pitch")
s = make_servo()
mx, my = s.command((0.0, 200.0), 1.0 / 60.0)     # target below the crosshair
check("a target below the crosshair pitches down", my < 0.0 and mx == 0.0, f"ry={my:+.3f}")
s.reset()
_, up = s.command((0.0, -200.0), 1.0 / 60.0)
check("a target above the crosshair pitches up", up > 0.0, f"ry={up:+.3f}")
s.reset()
_, flipped = Servo(RATES, FOCAL, pitch_up_sign=-1.0).command((0.0, 200.0), 1.0 / 60.0)
check("pitch_up_sign flips the vertical axis", flipped > 0.0, f"ry={flipped:+.3f}")
slow = make_servo(pitch_scale=0.5)
fast = make_servo(pitch_scale=1.0)
check("a game that pitches slower gets a larger magnitude for the same angle",
      abs(slow.command((0.0, 200.0), 1.0 / 60.0)[1]) > abs(fast.command((0.0, 200.0), 1.0 / 60.0)[1]))

print("blend: the A1 and A2 tiers on the user's own vector")
user = (0.6, 0.2)
target_dir = (0.0, 1.0)
check("no target leaves the vector alone", blend(user, None, "gravity", 1.0) == user)
check("an unknown tier leaves the vector alone", blend(user, target_dir, "magnet", 1.0) == user)
check("strength zero leaves the vector alone", blend(user, target_dir, "gravity", 0.0) == user)
check("a zero user vector stays zero in every tier",
      all(blend((0.0, 0.0), target_dir, tier, 1.0, 10.0, 120.0) == (0.0, 0.0)
          for tier in ("sticky", "gravity", "off")))
rotated = blend(user, target_dir, "gravity", 0.5, 10.0, 120.0)
check("gravity preserves the magnitude", abs(math.hypot(*rotated) - math.hypot(*user)) < 1e-9,
      f"{math.hypot(*user):.4f} -> {math.hypot(*rotated):.4f}")
full = blend(user, target_dir, "gravity", 1.0, 10.0, 120.0)
check("gravity at full strength points at the target",
      abs(math.atan2(full[1], full[0]) - math.atan2(target_dir[1], target_dir[0])) < 1e-9)
half_angle = math.atan2(rotated[1], rotated[0])
between = min(math.atan2(user[1], user[0]), math.atan2(target_dir[1], target_dir[0])) <= half_angle <= \
    max(math.atan2(user[1], user[0]), math.atan2(target_dir[1], target_dir[0]))
check("gravity at half strength lands between the two", between)
check("gravity outside the radius does nothing", blend(user, target_dir, "gravity", 1.0, 200.0, 120.0) == user)
check("sticky at the target scales by the strength",
      abs(math.hypot(*blend(user, target_dir, "sticky", 0.5, 0.0, 120.0)) - 0.5 * math.hypot(*user)) < 1e-9)
check("sticky at the edge of the radius does nothing",
      blend(user, target_dir, "sticky", 0.5, 120.0, 120.0) == user)
check("sticky ramps with distance",
      math.hypot(*blend(user, target_dir, "sticky", 0.5, 30.0, 120.0))
      < math.hypot(*blend(user, target_dir, "sticky", 0.5, 90.0, 120.0)))
check("no tier ever lengthens the user's vector",
      all(math.hypot(*blend(user, target_dir, tier, s, d, 120.0)) <= math.hypot(*user) + 1e-9
          for tier in ("sticky", "gravity") for s in (0.25, 0.5, 1.0) for d in (0.0, 30.0, 90.0, 200.0)))

print(f"\n{PASSES} passed, {FAILS} failed")
sys.exit(1 if FAILS else 0)
