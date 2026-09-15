"""
Where the targets are: the source protocol, and the script oracle.

A target source answers one question, "what is on screen right now", in
game-window pixels, and stamps the answer with the time it was captured so
the loop can tell a fresh frame from a stale one. Everything downstream
(``servo.py``, the ``look_at`` primitive) sees only pixels, which is what
keeps the same code honest whatever the source is: a script that knows the
truth, a colour key, or a model.

The order of preference for a title is the plan's rule "ground truth
first" (``docs/vision/TARGET_AWARE_AIM_PLAN.md`` section 5): the game's own
accessibility option, then a script or mod data path, then a colour or
template detector, then a trained detector.

:class:`ScriptTargetSource` is the second of those and, for now, the only
one: the Arma 3 harness mission publishes a target's head through
``worldToScreen`` on the same clipboard line as the player's pose
(``tests/game_harness.py``, ``Arma3Oracle``), so phase 1 measures the
closed loop against ground truth with no capture, no model and no new
dependency. It is deliberately given its channel rather than opening one:
the reader is the harness's, this module only parses what it returns, and
``src/spectator/games/arma3.json`` is test-only so the app cannot reach it.

The ground truth the mission also publishes (the target's true bearing and
range) rides in :attr:`TargetFrame.extra` and is never read by the servo.
It is there so the harness can score a run in degrees rather than in the
same pixels the loop was steering by.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Vec = Tuple[float, float]

#: ``tgt=`` payload on the Arma 3 mission's pose line: screen x and y as
#: fractions of the screen, the box width and height as fractions, the
#: bearing and elevation from the camera's own direction in degrees, and the
#: range in metres. ``tgt=none`` when nothing is placed, and ``tgt=off,...``
#: when something is placed that the engine will not draw: both are a frame
#: with no targets here, because nothing may steer onto a target the user
#: cannot see.
SCRIPT_TARGET_RE = re.compile(r"tgt=(?:none|off\S*|([-\d.e+]+),([-\d.e+]+),([-\d.e+]+),([-\d.e+]+),"
                              r"([-\d.e+]+),([-\d.e+]+),([-\d.e+]+))")
SCRIPT_TICK_RE = re.compile(r"NIMBUS_POSE t=([-\d.e+]+)")


@dataclass(frozen=True)
class Target:
    """One thing worth aiming at, in game-window pixels.

    Attributes
    ----------
    cx, cy : float
        The aim point, not necessarily the centre of the box: a colour
        source puts it in the blob's top third, the Arma 3 script publishes
        the head directly.
    w, h : float
        The target's extent, for a source that has one (0 when it does not).
    conf : float
        0 to 1. A script oracle is 1.0 by construction.
    kind : str
        What the source calls it, for the log and for a per-kind filter later.
    """

    cx: float
    cy: float
    w: float = 0.0
    h: float = 0.0
    conf: float = 1.0
    kind: str = ""

    def distance_to(self, point: Vec) -> float:
        """Pixels from ``point`` to this target's aim point."""
        return math.hypot(self.cx - float(point[0]), self.cy - float(point[1]))


@dataclass(frozen=True)
class TargetFrame:
    """What a source saw once, and when.

    Attributes
    ----------
    t_capture : float
        ``time.monotonic()`` when the frame was captured, so the consumer
        can age it and drop anything the loop would be steering by too late.
    crosshair : tuple of float
        Where the game's reticle is, in the same pixels as the targets.
    targets : tuple of Target
        Possibly empty: a frame with nothing in it is still a frame, and is
        what tells the loop the target is gone rather than merely late.
    extra : dict
        Source-specific ground truth for the harness. Nothing in the control
        path reads it.
    """

    t_capture: float
    crosshair: Vec
    targets: Tuple[Target, ...] = ()
    extra: Dict[str, Any] = field(default_factory=dict)

    def age(self, now: Optional[float] = None) -> float:
        """Seconds since the frame was captured."""
        return max(0.0, (time.monotonic() if now is None else float(now)) - self.t_capture)

    def error_px(self, target: Target) -> Vec:
        """Target minus crosshair: x right positive, y screen-down positive."""
        return (target.cx - float(self.crosshair[0]), target.cy - float(self.crosshair[1]))


class TargetSource:
    """What a source has to do: answer :meth:`poll`, and clean up after itself.

    A source is polled from the loop's thread and must not block it for
    long. A capture-based source (phase 2) does its work on its own thread
    and :meth:`poll` reads a single-slot mailbox; the script source below
    reads a channel that is already cheap.
    """

    kind = "none"

    def start(self) -> bool:
        """Begin producing frames. ``True`` when the source is usable."""
        return True

    def poll(self) -> Optional[TargetFrame]:
        """The most recent frame, or ``None`` when there has not been one yet."""
        return None

    def stop(self) -> None:
        """Release whatever :meth:`start` took."""


class ScriptTargetSource(TargetSource):
    """Targets published by the game's own script, over a channel someone else owns.

    Parameters
    ----------
    read_line : callable
        Returns the channel's current text, or ``None`` when it could not be
        read this time. For Arma 3 the harness passes its clipboard reader.
    size_px : tuple of float
        The game client area's width and height in pixels; the script's
        screen fractions are multiplied by these.
    crosshair : tuple of float, optional
        Where the reticle is in pixels. Default: the centre of the client area.
    kind : str
        What to call the targets this source produces.

    Notes
    -----
    The same line carries the player's pose, and the mission rewrites it
    fifty times a second, so a line whose tick has not advanced is the same
    frame read again rather than a new one: the source keeps the capture
    time of the first read, which is what makes ``TargetFrame.age`` mean
    what it says.
    """

    kind = "script"

    def __init__(self, read_line: Callable[[], Optional[str]], size_px: Vec,
                 crosshair: Optional[Vec] = None, kind: str = "unit") -> None:
        self._read = read_line
        self.size_px = (float(size_px[0]), float(size_px[1]))
        self.crosshair = (float(crosshair[0]), float(crosshair[1])) if crosshair else \
            (self.size_px[0] / 2.0, self.size_px[1] / 2.0)
        self.target_kind = str(kind)
        self._latest: Optional[TargetFrame] = None
        self._tick = float("nan")
        self.reads = 0
        self.frames = 0

    def parse(self, line: str, now: Optional[float] = None) -> Optional[TargetFrame]:
        """A pose line to a frame, or ``None`` when it is not one of ours."""
        tick = SCRIPT_TICK_RE.match(line or "")
        if not tick:
            return None
        found = SCRIPT_TARGET_RE.search(line)
        if not found:
            return None
        stamp = time.monotonic() if now is None else float(now)
        extra: Dict[str, Any] = {"tick": float(tick.group(1))}
        if found.group(1) is None:
            return TargetFrame(stamp, self.crosshair, (), extra)
        sx, sy, sw, sh, az, el, dist = (float(g) for g in found.groups())
        width, height = self.size_px
        target = Target(cx=sx * width, cy=sy * height, w=sw * width, h=sh * height,
                        conf=1.0, kind=self.target_kind)
        extra.update({"bearing_deg": az, "elevation_deg": el, "range_m": dist,
                      "screen": [sx, sy]})
        return TargetFrame(stamp, self.crosshair, (target,), extra)

    def poll(self) -> Optional[TargetFrame]:
        """Read the channel; a line whose tick has not advanced keeps its own age."""
        line = self._read()
        if line is None:
            return self._latest
        self.reads += 1
        frame = self.parse(line)
        if frame is None:
            return self._latest
        tick = float(frame.extra.get("tick", 0.0))
        if self._latest is not None and tick == self._tick:
            return self._latest
        self._tick = tick
        self._latest = frame
        self.frames += 1
        return frame

    @property
    def latest(self) -> Optional[TargetFrame]:
        """The last frame read, without touching the channel."""
        return self._latest


class TargetSelector:
    """Which target the loop is on, and when it lets go.

    Nearest to the crosshair inside ``fov_px``, and then kept until it
    leaves ``fov_px + margin_px``, so a target hovering on the boundary does
    not flicker between engaged and not. A frame with several targets keeps
    the one already held as long as it can still be recognised: the nearest
    target to where the held one was, within ``jump_px``.

    Parameters
    ----------
    fov_px : float
        Engage radius around the crosshair in pixels.
    margin_px : float, optional
        How far outside that radius a held target survives. Default: a
        quarter of ``fov_px``.
    jump_px : float, optional
        How far a held target may move between frames and still be the same
        target. Default: half of ``fov_px``, at least 40 px.
    """

    def __init__(self, fov_px: float, margin_px: Optional[float] = None,
                 jump_px: Optional[float] = None) -> None:
        self.fov_px = float(fov_px)
        self.margin_px = float(margin_px) if margin_px is not None else 0.25 * self.fov_px
        self.jump_px = float(jump_px) if jump_px is not None else max(40.0, 0.5 * self.fov_px)
        self._held: Optional[Target] = None

    @property
    def held(self) -> Optional[Target]:
        """The target the selector is on, or ``None``."""
        return self._held

    def reset(self) -> None:
        """Let go, so the next frame starts the acquisition again."""
        self._held = None

    def select(self, frame: Optional[TargetFrame]) -> Optional[Target]:
        """The target to steer onto for this frame, ``None`` when there is none."""
        if frame is None or not frame.targets:
            self._held = None
            return None
        crosshair = frame.crosshair
        if self._held is not None:
            near = min(frame.targets, key=lambda t: t.distance_to((self._held.cx, self._held.cy)))
            if near.distance_to((self._held.cx, self._held.cy)) <= self.jump_px \
                    and near.distance_to(crosshair) <= self.fov_px + self.margin_px:
                self._held = near
                return near
        candidates = [t for t in frame.targets if t.distance_to(crosshair) <= self.fov_px]
        self._held = min(candidates, key=lambda t: t.distance_to(crosshair)) if candidates else None
        return self._held


def nearest(targets: Sequence[Target], point: Vec) -> Optional[Target]:
    """The target nearest ``point``, or ``None`` when there are none."""
    items: List[Target] = list(targets)
    return min(items, key=lambda t: t.distance_to(point)) if items else None
