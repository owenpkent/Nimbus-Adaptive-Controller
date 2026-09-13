"""
Checks on ``src/spectator/targets.py``: the script target source and the
selector that decides which target the loop is on. Pure Python, no game,
no clipboard: the channel is a function, so the mission's own line format
is exercised here rather than only in front of Arma 3.

What is checked:

- a pose line with a target parses into pixels, and the screen fractions
  are scaled by the client area the harness measured
- ``tgt=none`` is a frame with no targets, which is how the loop learns the
  target is gone rather than merely late, and so is ``tgt=off,...``, which is
  the mission saying a target is placed where the engine will not draw it
- a line whose tick has not advanced is the same frame, and keeps its own
  capture time, so ``age`` means what it says
- a line that is not ours (someone else used the clipboard) leaves the last
  frame standing
- the ground truth the mission also publishes rides in ``extra`` and is not
  part of any target
- the selector takes the nearest target inside the radius, keeps it a
  little way outside, and drops it when it leaves for good

Run from the repo root (the fast runner does this)::

    venv\\Scripts\\python -m tests.test_target_sources
"""
from __future__ import annotations

import sys
import time

from src.spectator.targets import ScriptTargetSource, Target, TargetFrame, TargetSelector, nearest

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


def pose_line(tick: float, target: str = "none") -> str:
    """A line in the shape ``tests/game_harness.py`` writes from the mission."""
    return (f"NIMBUS_POSE t={tick} x=4096.1 y=4096.2 z=5.001 yaw=12.5 pitch=-1.2 dir=12.4 "
            f"buttons= execs=0 tgt={target}")


class Channel:
    """A clipboard stand-in: hands back whatever was last put on it."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.text


print("The mission's line")
channel = Channel(pose_line(101.5, "0.6,0.4,0.05,0.2,7.5,-1.0,42.3"))
source = ScriptTargetSource(channel, (WIDTH, HEIGHT))
frame = source.poll()
check("a line with a target gives one frame with one target", frame is not None and len(frame.targets) == 1)
target = frame.targets[0]
check("the screen fractions become client pixels",
      abs(target.cx - 768.0) < 1e-6 and abs(target.cy - 288.0) < 1e-6, f"({target.cx:.1f}, {target.cy:.1f})")
check("so does the box", abs(target.w - 64.0) < 1e-6 and abs(target.h - 144.0) < 1e-6)
check("the crosshair is the centre of the client area by default", frame.crosshair == (640.0, 360.0))
check("the error is the target less the crosshair, y down positive",
      frame.error_px(target) == (128.0, -72.0))
check("the script's own ground truth is in extra, not in the target",
      frame.extra["bearing_deg"] == 7.5 and frame.extra["range_m"] == 42.3
      and not hasattr(target, "bearing_deg"), str(sorted(frame.extra)))
check("a script target is certain of itself", target.conf == 1.0 and target.kind == "unit")
check("the frame is fresh", frame.age() < 0.5)

print("Nothing there, and nothing new")
channel.text = pose_line(102.0)
empty = source.poll()
check("tgt=none is a frame with no targets", empty is not None and empty.targets == ())
check("which is a different frame from the one before", empty is not frame)
channel.text = pose_line(102.0)
again = source.poll()
check("the same tick is the same frame, with its original capture time", again is empty)
time.sleep(0.06)
check("so it ages", again.age() >= 0.05, f"{again.age() * 1000:.0f} ms old")
channel.text = pose_line(103.0, "off,-31.2,-18.4,30.0")
gone = source.poll()
check("a target the engine will not draw is a frame with no targets, not a missing frame",
      gone is not None and gone is not empty and gone.targets == (),
      "placed but off screen: nothing may steer onto a target the user cannot see")
channel.text = "some other application's clipboard"
foreign = source.poll()
check("a line that is not ours leaves the last frame standing", foreign is gone)
channel.text = ""
check("and so does an empty channel", source.poll() is gone)


class Unreadable:
    def __call__(self):
        return None


unread = ScriptTargetSource(Unreadable(), (WIDTH, HEIGHT))
check("a channel that cannot be read at all has no frame yet", unread.poll() is None)
check("the source counts what it read", source.reads >= 4 and source.frames == 3,
      f"{source.reads} reads, {source.frames} frames")

print("A crosshair the game does not put in the middle")
low = ScriptTargetSource(Channel(pose_line(1.0, "0.5,0.5,0.1,0.2,0,0,10")), (WIDTH, HEIGHT), crosshair=(640.0, 400.0))
shifted = low.poll()
check("the error is measured from where the reticle is",
      shifted.error_px(shifted.targets[0]) == (0.0, -40.0))

print("The selector")
centre = (640.0, 360.0)


def frame_with(*points) -> TargetFrame:
    return TargetFrame(time.monotonic(), centre, tuple(Target(x, y, 40.0, 90.0, 1.0, "unit") for x, y in points))


selector = TargetSelector(fov_px=120.0)
check("its margin and jump default off the radius", selector.margin_px == 30.0 and selector.jump_px == 60.0)
check("nothing on an empty frame", selector.select(frame_with()) is None and selector.select(None) is None)
check("nothing outside the radius", selector.select(frame_with((900.0, 360.0))) is None)
picked = selector.select(frame_with((700.0, 360.0), (640.0, 320.0)))
check("the nearest inside the radius", picked is not None and (picked.cx, picked.cy) == (640.0, 320.0),
      f"{(picked.cx, picked.cy) if picked else None}")
# The held target walks up and out of the frame, 50 px a step, which is
# what a target does; the crosshair stays where it is.
walk = [(640.0, 270.0), (640.0, 230.0), (640.0, 180.0)]
kept = [selector.select(frame_with((700.0, 360.0), point)) for point in walk]
check("a held target is followed while it is inside the radius",
      kept[0] is not None and (kept[0].cx, kept[0].cy) == (640.0, 270.0), "90 px out of 120")
check("and is still held just outside it",
      kept[1] is not None and (kept[1].cx, kept[1].cy) == (640.0, 230.0),
      "130 px out, radius 120 plus a 30 px margin")
check("one that keeps going is dropped, and the other target is taken instead",
      kept[2] is not None and (kept[2].cx, kept[2].cy) == (700.0, 360.0))
selector.reset()
check("reset lets go", selector.held is None)
lost = TargetSelector(fov_px=120.0)
lost.select(frame_with((640.0, 300.0)))
check("an empty frame lets go too", lost.select(frame_with()) is None and lost.held is None)
far = TargetSelector(fov_px=120.0)
far.select(frame_with((640.0, 300.0)))
check("something that jumps further than a target can is not the target that was held",
      far.select(frame_with((640.0, 220.0))) is None,
      "80 px of jump against a 60 px limit, and 140 px from the crosshair, so only the hysteresis "
      "could have kept it and identity is what the hysteresis is for")

print("nearest")
check("nearest picks the nearest", nearest([Target(0.0, 0.0), Target(10.0, 0.0)], (9.0, 0.0)).cx == 10.0)
check("nearest of nothing is nothing", nearest([], (0.0, 0.0)) is None)

print(f"\n{PASSES} passed, {FAILS} failed")
sys.exit(1 if FAILS else 0)
