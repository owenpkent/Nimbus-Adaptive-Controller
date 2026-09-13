"""
Spectator+ primitives: timed axis and button sequences on the Qt thread.

A primitive is a small plan (set an axis, wait, release it) run by a
``QTimer`` so nothing blocks the UI thread and every plan ends with the
axes it touched back at zero. The runner writes through whatever the bridge
gives it, normally ``ControllerBridge.setAxis`` and ``setButton``, so the
output goes through the active driver interface and its limits, and bypasses
the widget shaping on purpose: a turn of 90 degrees has to be the same turn
whatever curve the user has on their own stick.

``turn``, ``walk`` and ``press`` are open loop: a plan is computed from the
calibration and executed blind. ``look_at`` is the closed one, the A3 tier
of ``docs/vision/TARGET_AWARE_AIM_PLAN.md``: each tick it asks a target
source where the target is, asks a :class:`~src.spectator.servo.Servo` what
to send, writes it, and ends on settle, on timeout, on the target being
lost, or on the user touching their own stick. It is bounded in time by
construction, it produces nothing without a command, and it always
releases.

One primitive runs at a time; ``stop()`` cancels it and zeroes the output.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from .calibration import GameCalibration
from .targets import TargetSelector

AxisSink = Callable[[str, float], None]
ButtonSink = Callable[[int, bool], None]


class PrimitiveRunner(QObject):
    """Executes Spectator+ primitives against an axis and a button sink.

    Parameters
    ----------
    set_axis : callable
        ``set_axis(axis_name, value)`` with ``value`` in -1 to 1 (0 to 1 for
        triggers), for example ``ControllerBridge.setAxis``.
    set_button : callable
        ``set_button(button_id, pressed)``, for example ``ControllerBridge.setButton``.
    parent : QObject, optional
        Qt parent; the runner's timer lives on the parent's thread.

    Signals
    -------
    started(str)
        A primitive began; the argument names it.
    finished(str, bool)
        A primitive ended; ``True`` when it ran to completion, ``False`` when
        ``stop()`` cut it short.
    """

    started = Signal(str)
    finished = Signal(str, bool)

    def __init__(self, set_axis: AxisSink, set_button: ButtonSink, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._set_axis = set_axis
        self._set_button = set_button
        self._calibration: Optional[GameCalibration] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._advance)
        self._steps: List[Tuple[float, Callable[[], None]]] = []
        self._index = 0
        self._t0 = 0.0
        self._name = ""
        self._touched_axes: set = set()
        self._touched_buttons: set = set()
        self.last_plan: Dict[str, float] = {}
        self._loop: Optional[Callable[[], None]] = None
        self._user_ref: Optional[Tuple[float, float]] = None
        self._user_cancel = False
        self._cancel_px = 0.0
        self.last_look: Dict[str, Any] = {}

    # ----- configuration -----
    @property
    def busy(self) -> bool:
        return bool(self._name)

    @property
    def calibration(self) -> Optional[GameCalibration]:
        return self._calibration

    def use_calibration(self, calibration: GameCalibration) -> None:
        self._calibration = calibration

    def use_game(self, game: str) -> bool:
        """Load the bundled calibration for ``game``; ``False`` when there is none."""
        try:
            self._calibration = GameCalibration.load(game)
            return True
        except (OSError, ValueError):
            return False

    # ----- primitives -----
    def turn(self, degrees: float) -> Optional[Dict[str, float]]:
        """Turn the view by ``degrees`` (positive right). Returns the plan, or
        ``None`` when there is no calibration, the runner is busy, or the
        angle is zero."""
        if self._calibration is None or self.busy or abs(degrees) < 1e-6:
            return None
        plan = self._calibration.plan_turn(degrees)
        if plan is None:
            return None
        magnitude, hold = plan
        self.last_plan = {"axis_value": magnitude, "hold": hold, "degrees": float(degrees)}
        self._run("turn", [(0.0, lambda: self._axis("rx", magnitude)),
                           (hold, lambda: self._axis("rx", 0.0))])
        return dict(self.last_plan)

    def walk(self, units: float) -> Optional[Dict[str, float]]:
        """Walk ``units`` forward (negative walks backward). Returns the plan or ``None``."""
        if self._calibration is None or self.busy or abs(units) < 1e-6:
            return None
        plan = self._calibration.plan_walk(units)
        if plan is None:
            return None
        magnitude, hold = plan
        self.last_plan = {"axis_value": magnitude, "hold": hold, "units": float(units)}
        self._run("walk", [(0.0, lambda: self._axis("y", magnitude)),
                           (hold, lambda: self._axis("y", 0.0))])
        return dict(self.last_plan)

    def press(self, button_id: int, hold: float = 0.12) -> bool:
        """Press and release a button. Needs no calibration."""
        if self.busy:
            return False
        bid = int(button_id)
        self.last_plan = {"button": float(bid), "hold": float(hold)}
        self._run("press", [(0.0, lambda: self._button(bid, True)),
                            (max(0.02, float(hold)), lambda: self._button(bid, False))])
        return True

    def look_at(self, source, servo, max_hold: float = 0.6, settle_px: float = 6.0,
                settle_ms: float = 120.0, tick_ms: int = 16, max_age: float = 0.15,
                selector: Optional[TargetSelector] = None, cancel: Optional[Callable[[], bool]] = None,
                cancel_px: float = 6.0, axes: Tuple[str, str] = ("rx", "ry"),
                name: str = "look_at") -> Optional[Dict[str, Any]]:
        """Steer the view onto a target, closed loop, for a bounded time.

        Each tick reads the newest frame from ``source``, picks a target
        with ``selector``, hands the error in pixels to ``servo`` and writes
        what it returns to ``axes``. The primitive ends, always with the
        axes released, when the error has been inside ``settle_px`` for
        ``settle_ms`` (completed), when ``max_hold`` runs out, when the
        target has been missing long enough for the servo to give up, or
        when the user moves their own stick (:meth:`note_user_stick`) or
        ``cancel`` says so.

        Parameters
        ----------
        source : TargetSource
            Anything with ``poll() -> Optional[TargetFrame]``.
        servo : Servo
            The control law; its ceiling is the user's own stick ceiling.
        max_hold : float
            Seconds the snap may last, the plan's ``snap_ms``.
        settle_px, settle_ms : float
            How close, and for how long, counts as done.
        tick_ms : int
            The loop's period. 16 is the bridge's own 60 Hz tick.
        max_age : float
            Seconds after which a frame is too old to steer by; an older one
            is treated as no frame at all.
        selector : TargetSelector, optional
            Which target, with hysteresis. Default: one with a generous
            radius, which is what a snap driven by a button wants.
        cancel : callable, optional
            Polled each tick; ``True`` ends the primitive.
        cancel_px : float
            How far the user's own stick may move, in widget pixels, before
            the primitive hands control straight back.
        axes : tuple of str
            The axis names to write, horizontal then vertical.
        name : str
            What the ``started`` and ``finished`` signals call it.

        Returns
        -------
        dict or None
            The parameters the loop started with, or ``None`` when the
            runner is busy or was given no source or servo.
        """
        if self.busy or source is None or servo is None:
            return None
        sel = selector if selector is not None else TargetSelector(fov_px=max(self._frame_span(source), 240.0))
        servo.reset()
        self._user_ref = None
        self._user_cancel = False
        self._cancel_px = max(0.0, float(cancel_px))
        samples: List[Dict[str, float]] = []
        state = {"inside_since": None, "last_tick": time.monotonic()}
        self.last_plan = {"max_hold": float(max_hold), "settle_px": float(settle_px),
                          "settle_ms": float(settle_ms), "tick_ms": float(tick_ms)}
        self.last_look = {"reason": "running", "settled": False, "samples": samples,
                          "ticks": 0, "frames": 0, "elapsed": 0.0}

        def tick() -> None:
            now = time.monotonic()
            dt = max(1e-4, now - float(state["last_tick"]))
            state["last_tick"] = now
            elapsed = now - self._t0
            self.last_look["ticks"] = int(self.last_look["ticks"]) + 1
            self.last_look["elapsed"] = elapsed
            if self._user_cancel or (cancel is not None and cancel()):
                self._finish(False, "cancelled")
                return
            frame = source.poll()
            fresh = frame is not None and frame.age(now) <= max_age
            target = sel.select(frame) if fresh else None
            if target is not None:
                self.last_look["frames"] = int(self.last_look["frames"]) + 1
                ex, ey = frame.error_px(target)
                mx, my = servo.command((ex, ey), dt)
                distance = math.hypot(ex, ey)
                samples.append({"t": round(elapsed, 4), "ex": round(ex, 2), "ey": round(ey, 2),
                                "deg_x": round(servo.deg_from_px(ex), 3), "deg_y": round(servo.deg_from_px(ey), 3),
                                "mx": round(mx, 4), "my": round(my, 4)})
                if distance <= settle_px:
                    if state["inside_since"] is None:
                        state["inside_since"] = now
                    elif (now - float(state["inside_since"])) * 1000.0 >= settle_ms:
                        self.last_look["settled"] = True
                        self._finish(True, "settled")
                        return
                else:
                    state["inside_since"] = None
            else:
                mx, my = servo.hold(dt)
                samples.append({"t": round(elapsed, 4), "mx": round(mx, 4), "my": round(my, 4),
                                "lost": round(servo.lost_for, 3)})
                state["inside_since"] = None
            self._axis(axes[0], mx)
            self._axis(axes[1], my)
            if target is None and servo.gave_up:
                self._finish(False, "lost")
                return
            if elapsed >= max_hold:
                self._finish(False, "timeout")
                return
            self._timer.start(max(1, int(tick_ms)))

        self._name = name
        self._steps = []
        self._index = 0
        self._t0 = time.monotonic()
        self._loop = tick
        self.started.emit(name)
        tick()
        return dict(self.last_plan)

    @staticmethod
    def _frame_span(source) -> float:
        """Half the diagonal of a source's frame, when it has a size; 0 otherwise."""
        size = getattr(source, "size_px", None)
        try:
            return math.hypot(float(size[0]), float(size[1])) / 2.0
        except (TypeError, ValueError, IndexError):
            return 0.0

    # ----- the user's own hand -----
    @property
    def watching_user(self) -> bool:
        """Whether a running primitive hands control back on the user's own stick motion."""
        return self._loop is not None and self._cancel_px > 0.0

    def note_user_stick(self, x_px: float, y_px: float) -> bool:
        """Tell the runner where the user's own stick is, in widget pixels.

        The bridge calls this from ``setStickInput`` while a closed-loop
        primitive runs, before it drives the stick itself. The first sample
        is the reference; a move of more than ``cancel_px`` from it cancels
        the primitive on the spot, so the axes are released before the
        user's own vector is sent and the driver ends up holding what the
        user commanded, not what the loop last asked for.

        Returns
        -------
        bool
            Whether this sample cancelled the primitive.
        """
        if not self.watching_user or self._user_cancel:
            return False
        point = (float(x_px), float(y_px))
        if self._user_ref is None:
            self._user_ref = point
            return False
        if math.hypot(point[0] - self._user_ref[0], point[1] - self._user_ref[1]) <= self._cancel_px:
            return False
        self._user_cancel = True
        self._finish(False, "user")
        return True

    def stop(self) -> None:
        """Cancel the running primitive and zero everything it touched."""
        if not self.busy:
            return
        self._finish(False, "stopped")

    # ----- machinery -----
    def _axis(self, axis: str, value: float) -> None:
        self._touched_axes.add(axis)
        self._set_axis(axis, float(value))

    def _button(self, button_id: int, pressed: bool) -> None:
        if pressed:
            self._touched_buttons.add(button_id)
        else:
            self._touched_buttons.discard(button_id)
        self._set_button(button_id, bool(pressed))

    def _release_all(self) -> None:
        for axis in list(self._touched_axes):
            try:
                self._set_axis(axis, 0.0)
            except Exception:
                pass
        for bid in list(self._touched_buttons):
            try:
                self._set_button(bid, False)
            except Exception:
                pass
        self._touched_axes.clear()
        self._touched_buttons.clear()

    def _finish(self, completed: bool, reason: str) -> None:
        """End whatever is running: stop the timer, release, say why, emit."""
        if not self._name:
            return
        name = self._name
        self._timer.stop()
        self._steps = []
        self._loop = None
        self._user_ref = None
        self._release_all()
        if self.last_look and self.last_look.get("reason") == "running":
            self.last_look["reason"] = reason
            self.last_look["settled"] = bool(self.last_look.get("settled"))
        self._name = ""
        self.finished.emit(name, bool(completed))

    def _run(self, name: str, steps: List[Tuple[float, Callable[[], None]]]) -> None:
        self._name = name
        self._steps = sorted(steps, key=lambda s: s[0])
        self._index = 0
        self._loop = None
        self._t0 = time.monotonic()
        self.started.emit(name)
        self._advance()

    def _advance(self) -> None:
        if self._loop is not None:
            self._loop()
            return
        # Run every step whose time has come, then arm the timer for the next
        # one. Steps are relative to the start, so timer drift does not add up.
        while self._index < len(self._steps):
            at, action = self._steps[self._index]
            remaining = at - (time.monotonic() - self._t0)
            if remaining > 0.0005:
                self._timer.start(max(1, int(remaining * 1000)))
                return
            self._index += 1
            try:
                action()
            except Exception:
                self._release_all()
                name, self._name = self._name, ""
                self.finished.emit(name, False)
                return
        self._release_all()
        name, self._name = self._name, ""
        self.finished.emit(name, True)
