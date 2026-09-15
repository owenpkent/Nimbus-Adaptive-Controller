"""
Checks on ``PrimitiveRunner.look_at``, the closed-loop Spectator+ primitive,
against a simulated game on a real Qt timer. No hardware, no real game, but
real time: a snap here takes as long as a snap does.

The game is a stand-in that integrates whatever the runner writes to its
axes, a dead time late, and publishes where the target is on screen through
the same ``ScriptTargetSource`` the Arma 3 mission feeds, so what is
exercised is the whole chain the harness will run, minus Arma.

What is checked:

- a snap settles on a target and finishes as completed, with the axes at
  zero afterwards
- nothing is written before the primitive is asked for, and nothing after
  it ends: a target on screen and no command is silence (the T4 property)
- the user's own stick hands control straight back (T5), before the driver
  sees anything else
- a target that disappears decays the command to zero and finishes as not
  completed, inside 250 ms (T6)
- ``stop()`` releases within a tick (the kill switch's path, T7)
- the command never exceeds the ceiling the widget gives it (T8)
- a second primitive cannot start while one is running

Run from the repo root (the fast runner does this)::

    venv\\Scripts\\python -m tests.test_look_at
"""
from __future__ import annotations

import math
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.spectator.calibration import GameCalibration  # noqa: E402
from src.spectator.primitives import PrimitiveRunner  # noqa: E402
from src.spectator.servo import Servo, focal_px, steady_rates  # noqa: E402
from src.spectator.targets import ScriptTargetSource, TargetSelector  # noqa: E402

PASSES = 0
FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSES, FAILS
    if ok:
        PASSES += 1
    else:
        FAILS += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


WIDTH, HEIGHT = 1280.0, 720.0
RATES = steady_rates(GameCalibration.load("arma3"))
FOCAL = focal_px(WIDTH, 70.0)
DEAD_TIME = 0.1


class FakeGame:
    """A game that turns when the stick says so, and says where the target is.

    The view integrates the commanded rate from the same calibration the
    servo inverts, but only ``act_delay`` seconds after the command; what it
    publishes is where the target was ``meas_delay`` ago, on the mission's
    own line format. Deleting the target makes the line say ``tgt=none``,
    which is what Arma 3 does when the unit is gone.
    """

    def __init__(self, bearing_deg: float, target_rate: float = 0.0,
                 meas_delay: float = DEAD_TIME / 2, act_delay: float = DEAD_TIME / 2) -> None:
        self.meas_delay = meas_delay
        self.act_delay = act_delay
        self.target_rate = target_rate
        self.alive = True
        self.t0 = time.monotonic()
        self.commands: List[Tuple[float, float]] = [(0.0, 0.0)]
        self.sent: List[Tuple[float, str, float]] = []
        self.bearing0 = bearing_deg
        self.tick = 0.0

    # the driver side
    def set_axis(self, axis: str, value: float) -> None:
        now = time.monotonic() - self.t0
        self.sent.append((now, axis, float(value)))
        if axis == "rx":
            rate = math.copysign(rate_for(abs(value)), value) if value else 0.0
            self.commands.append((now, rate))

    def set_button(self, button_id: int, pressed: bool) -> None:
        self.sent.append((time.monotonic() - self.t0, f"button{button_id}", 1.0 if pressed else 0.0))

    # the world
    def camera_at(self, when: float) -> float:
        """Degrees the view has turned by ``when``, the commands being late."""
        total = 0.0
        for index, (at, rate) in enumerate(self.commands):
            start = at + self.act_delay
            end = (self.commands[index + 1][0] + self.act_delay) if index + 1 < len(self.commands) else when
            if end > when:
                end = when
            if end > start:
                total += rate * (end - start)
        return total

    def bearing_at(self, when: float) -> float:
        """Where the target is relative to the view, in degrees, at ``when``."""
        return self.bearing0 + self.target_rate * when - self.camera_at(when)

    def line(self) -> str:
        """The mission's pose line as it would be right now."""
        now = time.monotonic() - self.t0
        self.tick += 0.02
        seen = max(0.0, now - self.meas_delay)
        if not self.alive:
            return f"NIMBUS_POSE t={self.tick:.3f} x=0 y=0 z=0 yaw=0 pitch=0 dir=0 buttons= execs=0 tgt=none"
        bearing = self.bearing_at(seen)
        x = 0.5 + FOCAL * math.tan(math.radians(bearing)) / WIDTH
        return (f"NIMBUS_POSE t={self.tick:.3f} x=0 y=0 z=0 yaw=0 pitch=0 dir=0 buttons= execs=0 "
                f"tgt={x:.6f},0.5,0.05,0.2,{bearing:.4f},0.0,40.0")

    @property
    def error_deg(self) -> float:
        return self.bearing_at(time.monotonic() - self.t0)

    def axis_now(self, axis: str) -> float:
        for when, name, value in reversed(self.sent):
            if name == axis:
                return value
        return 0.0


def rate_for(magnitude: float) -> float:
    """The game's own turn rate for a magnitude, its calibration read forward."""
    points = sorted(RATES.items())
    if magnitude < points[0][0]:
        return 0.0
    for (m0, r0), (m1, r1) in zip(points, points[1:]):
        if m1 >= magnitude:
            return r0 + (magnitude - m0) * (r1 - r0) / (m1 - m0)
    return points[-1][1]


def run_until(done: Dict[str, object], timeout: float) -> None:
    """Spin the Qt loop until ``done['finished']`` or the timeout."""
    loop = QEventLoop()
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(int(timeout * 1000))
    poll = QTimer()
    poll.timeout.connect(lambda: loop.quit() if done.get("finished") is not None else None)
    poll.start(5)
    loop.exec()
    poll.stop()
    guard.stop()


def arrival(look: Dict[str, object], settle_px: float) -> Optional[float]:
    """When the error entered ``settle_px`` and stayed, from the loop's own trace.

    That is what settling means, and it is the number to judge a snap by.
    The primitive runs on for the dwell that confirms it, and the wall clock
    around it also carries the poll cadence and whatever else the machine is
    doing, so its elapsed time is longer and much noisier: the first version
    of this file gated on it and a shared CI runner failed at 610 ms against
    a 600 ms budget for a snap that had arrived well inside it.
    """
    entered = None
    for sample in (look.get("samples") or []):
        if "ex" not in sample:
            continue
        if math.hypot(float(sample["ex"]), float(sample["ey"])) > settle_px:
            entered = None
        elif entered is None:
            entered = float(sample["t"])
    return entered


def snap(game: FakeGame, runner: PrimitiveRunner, ceiling: float = 0.95, timeout: float = 3.0,
         **kwargs) -> Dict[str, object]:
    """Run one ``look_at`` against ``game`` and return what happened."""
    source = ScriptTargetSource(game.line, (WIDTH, HEIGHT))
    servo = Servo(RATES, FOCAL, dead_time=DEAD_TIME, ceiling=ceiling)
    done: Dict[str, object] = {}
    runner.finished.connect(lambda name, ok: done.setdefault("finished", (name, ok)))
    started = time.monotonic()
    plan = runner.look_at(source, servo, selector=TargetSelector(fov_px=WIDTH), **kwargs)
    done["plan"] = plan
    done["source"] = source
    if plan is not None:
        run_until(done, timeout)
    done["elapsed"] = time.monotonic() - started
    done["look"] = dict(runner.last_look)
    return done


app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

print("A snap onto a target that is standing still")
game = FakeGame(bearing_deg=12.0)
runner = PrimitiveRunner(game.set_axis, game.set_button)
check("nothing is written before the primitive is asked for", game.sent == [])
SETTLE_PX = 16.0    # a degree at this focal length, the plan's own tolerance
result = snap(game, runner, max_hold=1.2, settle_px=SETTLE_PX, settle_ms=120.0)
name, ok = result["finished"] if result.get("finished") else ("", False)
look = result["look"]
check("it finishes, completed, having settled", ok and look.get("reason") == "settled",
      f"{look.get('reason')} after {result['elapsed'] * 1000:.0f} ms and {look.get('ticks')} ticks")
check("the view ended on the target", abs(game.error_deg) <= 1.0, f"{game.error_deg:+.2f} degrees off")
arrived = arrival(look, SETTLE_PX)
check("it arrived inside the plan's 600 ms snap", arrived is not None and arrived <= 0.6,
      f"arrived at {arrived if arrived is None else round(arrived * 1000)} ms by the loop's own trace, "
      f"released at {result['elapsed'] * 1000:.0f} ms after the dwell that confirms it")
check("both axes were released at the end", game.axis_now("rx") == 0.0 and game.axis_now("ry") == 0.0)
check("nothing was written after it finished",
      max(when for when, _, _ in game.sent) <= result["elapsed"] + 0.05)
check("the runner is free again", not runner.busy)
check("the trace is there to score", len(look.get("samples") or []) >= 5 and look.get("frames", 0) >= 5,
      f"{len(look.get('samples') or [])} samples, {look.get('frames')} of them with a target")

print("A target that is walking")
moving = FakeGame(bearing_deg=8.0, target_rate=6.0)
runner = PrimitiveRunner(moving.set_axis, moving.set_button)
result = snap(moving, runner, max_hold=1.8, settle_px=SETTLE_PX, settle_ms=150.0)
check("it settles on a target crossing at 6 degrees a second",
      result["look"].get("reason") == "settled" and abs(moving.error_deg) <= 1.5,
      f"{result['look'].get('reason')}, {moving.error_deg:+.2f} degrees off after "
      f"{result['elapsed'] * 1000:.0f} ms")

print("The user takes over")
game = FakeGame(bearing_deg=25.0)
runner = PrimitiveRunner(game.set_axis, game.set_button)
source = ScriptTargetSource(game.line, (WIDTH, HEIGHT))
servo = Servo(RATES, FOCAL, dead_time=DEAD_TIME, ceiling=0.95)
done: Dict[str, object] = {}
runner.finished.connect(lambda n, ok: done.setdefault("finished", (n, ok)))
runner.look_at(source, servo, max_hold=2.0, cancel_px=6.0, selector=TargetSelector(fov_px=WIDTH))
QTimer.singleShot(150, lambda: runner.note_user_stick(0.0, 0.0))
QTimer.singleShot(200, lambda: runner.note_user_stick(2.0, 0.0))
QTimer.singleShot(260, lambda: runner.note_user_stick(9.0, 0.0))
run_until(done, 2.0)
check("a small wobble on the user's stick does not cancel anything",
      runner.last_look.get("ticks", 0) > 3, f"{runner.last_look.get('ticks')} ticks before the drag")
check("a drag past the threshold cancels the snap, not completed",
      done.get("finished") == ("look_at", False) and runner.last_look.get("reason") == "user",
      str(runner.last_look.get("reason")))
check("and the axes are released by the time it returns", game.axis_now("rx") == 0.0)
check("the runner is not watching the user's stick any more", not runner.watching_user)
check("a further stick sample is ignored", runner.note_user_stick(40.0, 0.0) is False)

print("The target disappears")
game = FakeGame(bearing_deg=20.0)
runner = PrimitiveRunner(game.set_axis, game.set_button)
source = ScriptTargetSource(game.line, (WIDTH, HEIGHT))
servo = Servo(RATES, FOCAL, dead_time=DEAD_TIME, ceiling=0.95)
done = {}
runner.finished.connect(lambda n, ok: done.setdefault("finished", (n, ok)))
runner.look_at(source, servo, max_hold=3.0, selector=TargetSelector(fov_px=WIDTH))
killed: Dict[str, float] = {}


def kill() -> None:
    game.alive = False
    killed["at"] = time.monotonic()


QTimer.singleShot(150, kill)
run_until(done, 3.0)
gone = time.monotonic() - killed.get("at", time.monotonic())
check("it gives up rather than steering by a memory",
      done.get("finished") == ("look_at", False) and runner.last_look.get("reason") == "lost",
      str(runner.last_look.get("reason")))
check("inside the 250 ms of servo time the fail-safe rule allows, plus a tick and a poll",
      gone <= 0.45, f"{gone * 1000:.0f} ms of wall clock after the unit went")
check("the last thing the driver saw was zero", game.axis_now("rx") == 0.0)
tail = [value for _, axis, value in game.sent if axis == "rx"][-4:]
check("and the command decayed rather than being cut",
      all(abs(b) <= abs(a) + 1e-9 for a, b in zip(tail, tail[1:])), str([round(v, 3) for v in tail]))

print("The kill switch, the ceiling, and one at a time")
game = FakeGame(bearing_deg=30.0)
runner = PrimitiveRunner(game.set_axis, game.set_button)
source = ScriptTargetSource(game.line, (WIDTH, HEIGHT))
servo = Servo(RATES, FOCAL, dead_time=DEAD_TIME, ceiling=0.45)
done = {}
runner.finished.connect(lambda n, ok: done.setdefault("finished", (n, ok)))
runner.look_at(source, servo, max_hold=2.0, selector=TargetSelector(fov_px=WIDTH))
check("a second primitive cannot start while one runs",
      runner.look_at(source, servo) is None and runner.turn(90.0) is None)
QTimer.singleShot(200, runner.stop)
run_until(done, 2.0)
check("stop ends it, not completed", done.get("finished") == ("look_at", False)
      and runner.last_look.get("reason") == "stopped", str(runner.last_look.get("reason")))
check("the axes are zero straight after the stop", game.axis_now("rx") == 0.0)
check("nothing the servo sent ever exceeded the widget's ceiling",
      max(abs(v) for _, axis, v in game.sent if axis == "rx") <= 0.45 + 1e-9,
      f"largest {max(abs(v) for _, axis, v in game.sent if axis == 'rx'):.3f} against a 0.45 ceiling")

print("Silence")
quiet = FakeGame(bearing_deg=5.0)
idle = PrimitiveRunner(quiet.set_axis, quiet.set_button)
source = ScriptTargetSource(quiet.line, (WIDTH, HEIGHT))
for _ in range(20):
    source.poll()
    time.sleep(0.005)
check("a target on screen with nothing asked for produces no output at all",
      quiet.sent == [] and not idle.busy, "the source was polled twenty times")
check("look_at refuses without a source or a servo",
      idle.look_at(None, None) is None and idle.look_at(source, None) is None)

print("A source that raises mid-snap")
game = FakeGame(bearing_deg=25.0)
runner = PrimitiveRunner(game.set_axis, game.set_button)
inner = ScriptTargetSource(game.line, (WIDTH, HEIGHT))


class BrokenSource:
    """Serves real frames, then raises, the way a clipboard read can."""
    size_px = (WIDTH, HEIGHT)

    def __init__(self) -> None:
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.polls > 5:
            raise OSError("clipboard went away (planted by the test)")
        return inner.poll()


done = {}
runner.finished.connect(lambda n, ok: done.setdefault("finished", (n, ok)))
runner.look_at(BrokenSource(), Servo(RATES, FOCAL, dead_time=DEAD_TIME, ceiling=0.95), max_hold=2.0,
               selector=TargetSelector(fov_px=WIDTH))
run_until(done, 2.0)
check("an exception inside the loop ends the primitive, not completed",
      done.get("finished") == ("look_at", False) and runner.last_reason == "error", runner.last_reason)
check("with the axes released", game.axis_now("rx") == 0.0 and game.axis_now("ry") == 0.0)
check("and the runner free for the next primitive", not runner.busy)


class FirstTickBroken:
    size_px = (WIDTH, HEIGHT)

    def poll(self):
        raise OSError("broken from the start (planted by the test)")


game = FakeGame(bearing_deg=25.0)
runner = PrimitiveRunner(game.set_axis, game.set_button)
runner.look_at(FirstTickBroken(), Servo(RATES, FOCAL, dead_time=DEAD_TIME, ceiling=0.95), max_hold=2.0,
               selector=TargetSelector(fov_px=WIDTH))
check("an exception on the very first tick is caught the same way",
      not runner.busy and runner.last_reason == "error", runner.last_reason)

print("Whose stick moved, and from where")
game = FakeGame(bearing_deg=25.0)
runner = PrimitiveRunner(game.set_axis, game.set_button)
source = ScriptTargetSource(game.line, (WIDTH, HEIGHT))
runner.look_at(source, Servo(RATES, FOCAL, dead_time=DEAD_TIME, ceiling=0.95), max_hold=2.0, cancel_px=6.0,
               selector=TargetSelector(fov_px=WIDTH))
check("a press that lands off centre is a move, even as the first sample",
      runner.note_user_stick(60.0, 0.0, key="right") is True and runner.last_reason == "user",
      runner.last_reason)
check("and it released the axes", game.axis_now("rx") == 0.0)

game = FakeGame(bearing_deg=25.0)
runner = PrimitiveRunner(game.set_axis, game.set_button)
source = ScriptTargetSource(game.line, (WIDTH, HEIGHT))
runner.look_at(source, Servo(RATES, FOCAL, dead_time=DEAD_TIME, ceiling=0.95), max_hold=2.0, cancel_px=6.0,
               selector=TargetSelector(fov_px=WIDTH))
held = runner.note_user_stick(82.0, 0.0, key="left", start_px=(80.0, 0.0))
nudge = runner.note_user_stick(2.0, 0.0, key="right")
check("a stick already held when the loop started is measured from where it was",
      held is False and runner.busy)
check("and one stick is never measured against another", nudge is False and runner.busy)
check("the held stick still cancels once it really moves",
      runner.note_user_stick(95.0, 0.0, key="left") is True and runner.last_reason == "user")

print("Why it ended")
game = FakeGame(bearing_deg=30.0)
runner = PrimitiveRunner(game.set_axis, game.set_button)
runner.look_at(ScriptTargetSource(game.line, (WIDTH, HEIGHT)),
               Servo(RATES, FOCAL, dead_time=DEAD_TIME, ceiling=0.95), max_hold=2.0,
               selector=TargetSelector(fov_px=WIDTH))
runner.stop()
check("a stop is reported as stopped, which the bridge reads to leave neutral",
      runner.last_reason == "stopped", runner.last_reason)
done = {}
runner.finished.connect(lambda n, ok: done.setdefault("finished", (n, ok)))
runner.press(1, hold=0.03)
run_until(done, 1.0)
check("a timed primitive that ran to the end is reported as done", runner.last_reason == "done", runner.last_reason)

print(f"\n{PASSES} passed, {FAILS} failed")
sys.exit(1 if FAILS else 0)
