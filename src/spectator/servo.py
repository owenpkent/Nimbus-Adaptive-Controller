"""
The control law behind target-aware assistance.

The plant is not a mouse. A stick commands an angular *rate*, the game
integrates it into an angle, and whatever watches the result sees it 60 to
120 ms later (``docs/vision/TARGET_AWARE_AIM_PLAN.md`` section 7.3). So the
thing that puts a reticle on a target is a servo on angular error with dead
time, not a one-shot move, and it needs three conversions the rest of the
project already measures:

1. **Pixels to degrees.** A pinhole camera: ``focal_px = (W / 2) / tan(hfov / 2)``
   and an offset of ``p`` pixels from the centre is ``atan(p / focal_px)``.
   The harness measures ``focal_px`` for a game rather than trusting a
   number in a menu (the T1 check).
2. **Degrees per second to stick magnitude.** The per-game calibration
   (``src/spectator/calibrations/<game>.json``) holds yaw against hold time
   for each magnitude; the steady rate is the longest hold's row, and
   :meth:`Servo.magnitude_for_rate` inverts that monotone table. Under the
   game's own deadzone it returns zero rather than a floor value, so the
   assist never sends "just above the deadzone" into a game that has a
   cliff there (Half-Life 2, Halo Wars).
3. **Gain against dead time.** The only period this loop can be reasoned
   about in is the dead time, because that is how long every command takes
   to become visible. So the gain is written as the fraction of the visible
   error to command away in one of them: ``kp = kp_fraction / dead_time``,
   0.6 by default, well inside the ``pi / (2 * tau)`` stability bound for an
   integrator with delay.

Two things are needed on top of that, and both were put there by a
simulation that failed without them (``tests/test_assist_servo.py``):

- **The plant model.** A proportional loop slow enough not to overshoot
  through 100 ms of dead time takes over a second to settle a 30 degree
  error, which fails the plan's 600 ms gate. So the servo subtracts the
  turn it has already commanded and not yet seen, integrated from the same
  calibration table (a Smith predictor, with the calibration as the model).
  It is never allowed to predict past the target: only a measurement may
  say the view has overshot.
- **The target's own rate.** A proportional term alone leaves a standing
  error on a moving target, so the servo estimates that rate and feeds it
  forward. It can do that without being told: the error moves at the
  target's rate minus the camera's, and the camera's is the servo's own
  command, delayed. The estimate is taken over a quarter of a second rather
  than tick to tick, on the error as measured rather than the filtered one.
  Both matter: the filter's lag leaves part of the camera's own turn in the
  estimate, and a tick-long baseline is mostly the quantisation of when a
  command landed, which showed up in the simulation as the servo giving a
  small command away from a target it had already reached.

No integral term: a steady offset of a few pixels is better than windup
against a target that steps away.

Nothing here reads the screen, and nothing here decides whether assistance
may run at all (``policy.py`` does). Section 5's rules that live in this
file: the command never exceeds the user's ceiling, a lost target decays to
zero, and :func:`blend` cannot enlarge a vector the user is not producing.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .calibration import GameCalibration, amount_for

Vec = Tuple[float, float]

#: Tiers :func:`blend` implements (A1 and A2 of the plan's ladder). A3 (snap)
#: and A4 (track) are the servo driving the axes directly, not a blend.
TIERS = ("off", "sticky", "gravity")


def focal_px(width_px: float, fov_deg: float) -> float:
    """Pinhole focal length in pixels for a horizontal field of view.

    Parameters
    ----------
    width_px : float
        Width of the game's client area in pixels.
    fov_deg : float
        Horizontal field of view in degrees.

    Returns
    -------
    float
        Pixels per radian at the centre of the frame.
    """
    half = math.radians(max(1e-3, min(179.0, float(fov_deg)))) / 2.0
    return (float(width_px) / 2.0) / math.tan(half)


def fov_deg(width_px: float, focal: float) -> float:
    """The horizontal field of view a focal length in pixels implies."""
    return 2.0 * math.degrees(math.atan((float(width_px) / 2.0) / max(1e-6, float(focal))))


def focal_from_px_per_deg(px_per_deg: float) -> float:
    """Focal length in pixels from a pixels-per-degree measured at the centre."""
    return float(px_per_deg) * 180.0 / math.pi


def steady_rates(calibration: GameCalibration) -> Dict[float, float]:
    """Degrees per second of yaw at each calibrated stick magnitude.

    The calibration holds several ``(hold, degrees)`` samples per magnitude
    because games ramp their turn rate; the steady rate is the longest hold
    present, which is the one least contaminated by that ramp.

    Parameters
    ----------
    calibration : GameCalibration
        A loaded per-game calibration.

    Returns
    -------
    dict
        ``{magnitude: degrees per second}``, ascending by magnitude, with
        any magnitude whose samples do not grow left out.
    """
    rates: Dict[float, float] = {}
    for magnitude, samples in calibration.yaw.items():
        holds = [float(t) for t, _ in samples if float(t) > 0]
        if not holds:
            continue
        longest = max(holds)
        amount = amount_for(samples, longest)
        if amount and amount > 0:
            rates[float(magnitude)] = amount / longest
    return dict(sorted(rates.items()))


class Servo:
    """A rate-controlled aim servo: angular error in, stick magnitudes out.

    Parameters
    ----------
    rates : dict
        ``{stick magnitude: degrees per second}``, normally from
        :func:`steady_rates`. Must be monotone in magnitude to be invertible;
        rows that are not are dropped, ascending, at construction.
    focal : float
        Focal length in pixels (:func:`focal_px`).
    dead_time : float
        Seconds between commanding a rate and seeing its effect. 0.1 is the
        plan's assembled estimate for a 60 Hz game.
    kp_fraction : float
        How much of the error the servo can see it should command away in
        one dead time, which is what sets the gain: ``kp = kp_fraction /
        dead_time``. At 0.6 the loop converges by at least a third of the
        remaining error per dead time even if the calibrated table is 30
        percent wrong in either direction, and never turns past the target
        while it is less than 60 percent wrong. The stability bound for
        this plant is ``pi / 2`` in the same units (section 7.3), so 0.6 is
        well inside it.
    ceiling : float
        The largest magnitude the servo may command, which is the user's own
        stick ceiling. The command never exceeds it.
    error_tau : float
        Seconds of first-order filtering on the error, the tremor filter's
        shape, to keep detector jitter out of the command.
    vel_tau : float
        Seconds of first-order filtering on the estimated target rate.
    vel_window : float
        Seconds of baseline the target's rate is estimated over. Until that
        much has been seen there is no estimate and no lead, which costs
        nothing: the start of a snap is the proportional term's business.
    vel_quiet : float
        Degrees the view may have turned across that window for the estimate
        to be believed. The estimate is the error's own motion with the
        camera's taken back out of it, so whatever the plant model gets
        wrong lands in it as an apparent target rate: while the view is
        slewing, that mistake is larger than anything a target does. Three
        degrees is chosen so that a model wrong by a third still cannot
        produce a rate above the game's own stick deadzone, which is the
        rate below which the servo commands nothing at all. Measured
        without it: a settled snap drifting back off a stationary target at
        a third of a degree a second.
    predict : bool
        Subtract the turn already commanded but not yet visible in the
        measurement, so the loop steers by where the view is about to be
        rather than where it was. This is what makes a snap settle inside
        the plan's 600 ms: a proportional loop slow enough not to overshoot
        through 100 ms of dead time takes over a second. Off is the control
        the harness runs to show the difference.
    pitch_scale : float
        Pitch rate over yaw rate at the same magnitude (Arma 3 measured 0.69).
        The vertical axis asks the table for ``rate / pitch_scale``.
    pitch_up_sign : float
        +1 when a positive vertical axis value pitches the view up, which is
        the ViGEm and vJoy convention this project uses.
    lead : bool
        Feed the estimated target rate forward and aim a dead time ahead of
        it. Off is the control for the harness's T3 check.
    hold_s, decay_s : float
        A target lost for ``hold_s`` keeps the last command, then it ramps to
        zero over ``decay_s``; after both the servo has :attr:`gave_up`.
        Section 5's fail-safe rule: 0.15 s and 0.10 s.
    """

    def __init__(self, rates: Dict[float, float], focal: float, dead_time: float = 0.1,
                 kp_fraction: float = 0.6, ceiling: float = 1.0, error_tau: float = 0.03,
                 vel_tau: float = 0.08, vel_window: float = 0.25, vel_quiet: float = 3.0,
                 pitch_scale: float = 1.0, pitch_up_sign: float = 1.0,
                 lead: bool = True, predict: bool = True,
                 hold_s: float = 0.15, decay_s: float = 0.10) -> None:
        self.rates = self._monotone(rates)
        self.focal = float(focal)
        self.dead_time = max(1e-3, float(dead_time))
        self.kp_fraction = float(kp_fraction)
        self.predict = bool(predict)
        self.ceiling = max(0.0, min(1.0, float(ceiling)))
        self.error_tau = max(0.0, float(error_tau))
        self.vel_tau = max(0.0, float(vel_tau))
        self.vel_window = max(0.0, float(vel_window))
        self.vel_quiet = max(0.0, float(vel_quiet))
        self.pitch_scale = max(1e-3, float(pitch_scale))
        self.pitch_up_sign = 1.0 if float(pitch_up_sign) >= 0 else -1.0
        self.lead = bool(lead)
        self.hold_s = max(0.0, float(hold_s))
        self.decay_s = max(1e-3, float(decay_s))
        self._filtered: Optional[Vec] = None      # filtered error, degrees
        self._rate: Vec = (0.0, 0.0)              # estimated target rate, degrees per second
        self._predicted: Vec = (0.0, 0.0)         # error after the prediction, degrees
        self._last: Vec = (0.0, 0.0)              # last command, stick magnitudes
        self._missing = 0.0                       # seconds since the last target
        self._history: List[Tuple[float, float, float]] = []   # (age, yaw rate, pitch rate) delivered
        self._errors: List[Tuple[float, float, float]] = []    # (age, x, y) error as measured, degrees
        self._age = 0.0

    @classmethod
    def from_calibration(cls, calibration: GameCalibration, focal: float, **kwargs) -> "Servo":
        """A servo for a game, from its measured calibration."""
        return cls(steady_rates(calibration), focal, **kwargs)

    # ----- the table -----
    @staticmethod
    def _monotone(rates: Dict[float, float]) -> Dict[float, float]:
        """The rows of ``rates`` that rise with magnitude, ascending.

        A table that does not rise cannot be inverted, and the row that
        breaks it is a measurement to redo rather than something to average
        over, so it is dropped and the rest kept.
        """
        out: Dict[float, float] = {}
        best = 0.0
        for magnitude in sorted(float(m) for m in rates):
            rate = float(rates[magnitude])
            if rate > best:
                out[magnitude] = rate
                best = rate
        return out

    @property
    def min_rate(self) -> float:
        """The slowest rate the game will actually turn at, its deadzone in degrees per second."""
        return min(self.rates.values()) if self.rates else 0.0

    @property
    def max_rate(self) -> float:
        """The fastest calibrated rate, which the ceiling may cut below."""
        return max(self.rates.values()) if self.rates else 0.0

    @property
    def settle_floor_deg(self) -> float:
        """The error the servo can no longer correct, in degrees.

        Under it the rate the gain asks for is below the game's own stick
        deadzone, and the servo commands nothing rather than sending a
        value the game will either ignore or, where it has a cliff at its
        deadzone, act on far too strongly. It is the game's deadzone over
        the gain, so it grows with the dead time, and it is the floor any
        "settled within" figure has to be read against.
        """
        return self.min_rate / self.kp if self.kp > 0 else 0.0

    @property
    def kp(self) -> float:
        """Proportional gain, per second: the error covered per dead time.

        ``kp_fraction / dead_time``. A tick commands the rate that would
        close that fraction of the error it can see over the time it takes
        to see the result, which is the only period a loop with dead time
        can be reasoned about in. The linear stability bound for an
        integrator with delay is ``pi / (2 * dead_time)``, so this is inside
        it by the same factor ``pi / 2`` divided by ``kp_fraction``.
        """
        return self.kp_fraction / self.dead_time

    def magnitude_for_rate(self, rate: float) -> float:
        """The stick magnitude that turns at ``rate`` degrees per second.

        Piecewise-linear inversion of the calibrated table, clamped to the
        largest calibrated magnitude and to :attr:`ceiling`. A rate under the
        game's own deadzone returns 0: a floor value there would either do
        nothing or, in a game with a cliff at its deadzone, do far too much.

        Parameters
        ----------
        rate : float
            Wanted turn rate in degrees per second; the sign is ignored.

        Returns
        -------
        float
            A magnitude in 0 to :attr:`ceiling`.
        """
        want = abs(float(rate))
        if not self.rates or want < self.min_rate:
            return 0.0
        points = sorted(self.rates.items())
        for (m0, r0), (m1, r1) in zip(points, points[1:]):
            if r1 >= want:
                m = m0 + (want - r0) * (m1 - m0) / (r1 - r0)
                return min(m, self.ceiling)
        return min(points[-1][0], self.ceiling)

    def rate_for_magnitude(self, magnitude: float) -> float:
        """The rate a magnitude turns at, the forward reading of the same table."""
        want = abs(float(magnitude))
        if not self.rates:
            return 0.0
        points = sorted(self.rates.items())
        if want < points[0][0]:
            return 0.0
        for (m0, r0), (m1, r1) in zip(points, points[1:]):
            if m1 >= want:
                return r0 + (want - m0) * (r1 - r0) / (m1 - m0)
        return points[-1][1]

    # ----- geometry -----
    def deg_from_px(self, px: float) -> float:
        """Degrees off centre for an offset of ``px`` pixels."""
        return math.degrees(math.atan2(float(px), self.focal))

    def px_from_deg(self, deg: float) -> float:
        """Pixels off centre for an angle of ``deg`` degrees."""
        return self.focal * math.tan(math.radians(float(deg)))

    # ----- the loop -----
    def reset(self) -> None:
        """Forget the filters, the rate estimate and the loss timer."""
        self._filtered = None
        self._rate = (0.0, 0.0)
        self._predicted = (0.0, 0.0)
        self._last = (0.0, 0.0)
        self._missing = 0.0
        self._history = []
        self._errors = []
        self._age = 0.0

    @property
    def last_command(self) -> Vec:
        """The magnitudes returned by the last :meth:`command` or :meth:`hold`."""
        return self._last

    @property
    def error_deg(self) -> Vec:
        """The filtered error in degrees, ``(0, 0)`` before the first sample."""
        return self._filtered or (0.0, 0.0)

    @property
    def predicted_deg(self) -> Vec:
        """The error the last command was actually computed from: the filtered
        error less the turn already in flight."""
        return self._predicted

    @property
    def target_rate(self) -> Vec:
        """The estimated angular rate of the target itself, degrees per second."""
        return self._rate

    @property
    def lost_for(self) -> float:
        """Seconds since the last frame that had a target."""
        return self._missing

    @property
    def gave_up(self) -> bool:
        """Whether the target has been missing long enough for the command to be zero."""
        return self._missing >= self.hold_s + self.decay_s

    def _in_flight(self, window: float) -> Vec:
        """Degrees of turn commanded inside the last ``window`` seconds.

        The measurement the loop is holding was taken before these commands
        could show up in it, so this is how far the view has moved, or is
        about to move, that the error does not know about yet. Subtracting
        it is the prediction: it turns a loop with dead time into one
        without, to the accuracy of the calibration it integrates.
        """
        return self._integrate(self._age - max(0.0, float(window)), self._age)

    def _integrate(self, start: float, stop: float) -> Vec:
        """Degrees of turn the servo commanded between two ages."""
        sum_x = sum_y = 0.0
        history = self._history
        for index, (age, yaw, pitch) in enumerate(history):
            end = history[index + 1][0] if index + 1 < len(history) else self._age
            low, high = max(age, start), min(end, stop)
            if high > low:
                sum_x += yaw * (high - low)
                sum_y += pitch * (high - low)
        return (sum_x, sum_y)

    def _measure_rate(self) -> Optional[Vec]:
        """The target's own angular rate over :attr:`vel_window`, or None.

        The error moved by what the target did less what the camera did, and
        the camera did what this servo commanded a dead time earlier, so
        adding that turn back over the same span leaves the target's own
        motion. ``None`` until there is a span to measure over.
        """
        if self.vel_window <= 0.0 or len(self._errors) < 2:
            return None
        want = self._age - self.vel_window
        old = self._errors[0]
        for entry in self._errors:
            if entry[0] <= want:
                old = entry
            else:
                break
        span = self._age - old[0]
        if span < self.vel_window * 0.8 or span <= 0.0:
            return None
        turned = self._integrate(old[0] - self.dead_time, self._age - self.dead_time)
        now = self._errors[-1]
        rate = [(now[1] - old[1] + turned[0]) / span, (now[2] - old[2] + turned[1]) / span]
        for axis in (0, 1):
            if abs(turned[axis]) > self.vel_quiet:
                rate[axis] = 0.0
        return (rate[0], rate[1])

    @staticmethod
    def _deduct(error: float, flight: float) -> float:
        """The error less the turn already in flight, and never past zero.

        The prediction is allowed to say "that turn is already covered"; it
        is not allowed to say "you have overshot", because only a
        measurement can say that. Without the clamp a plant that ramps into
        a turn (the command is delivered late, so less of it has happened
        than the calibration says) makes the servo command a reversal,
        which subtracts from the in-flight total, which asks for the
        original direction again: a chatter at the period of the dead time
        that leaves the view nowhere near the target. Measured in the
        simulation as 24 degrees of error on a 30 degree snap.
        """
        if error >= 0.0:
            return max(0.0, error - max(0.0, flight))
        return min(0.0, error - min(0.0, flight))

    def _delivered(self, ago: float) -> Vec:
        """The rates the servo was delivering ``ago`` seconds ago, from its own history."""
        want = self._age - max(0.0, ago)
        best: Vec = (0.0, 0.0)
        for age, yaw, pitch in self._history:
            if age <= want:
                best = (yaw, pitch)
            else:
                break
        return best

    def command(self, error_px: Vec, dt: float, target_vel_px: Optional[Vec] = None,
                age: Optional[float] = None) -> Vec:
        """One tick of the loop: the error now, the magnitudes to send.

        Parameters
        ----------
        error_px : tuple of float
            Target minus crosshair in game-window pixels: x right positive,
            y screen-down positive.
        dt : float
            Seconds since the last tick.
        target_vel_px : tuple of float, optional
            The target's own screen velocity in pixels per second, when
            something knows it. Left out, the servo estimates it from the
            change in the error and the rate it was itself delivering one
            dead time ago.
        age : float, optional
            How old this error reading is, in seconds. A moving target is
            that much further on by now, so the position lead aims there.
            Default: :attr:`dead_time`, the pessimistic assumption.

        Returns
        -------
        tuple of float
            ``(horizontal, vertical)`` stick magnitudes in -1 to 1, ready for
            ``setAxis("rx", ...)`` and ``setAxis("ry", ...)``. Vertical is
            positive up when ``pitch_up_sign`` is +1.
        """
        step = max(1e-4, float(dt))
        self._age += step
        self._missing = 0.0
        raw = (self.deg_from_px(float(error_px[0])), self.deg_from_px(float(error_px[1])))

        previous = self._filtered
        if previous is None or self.error_tau <= 0.0:
            filtered = raw
        else:
            alpha = 1.0 - math.exp(-step / self.error_tau)
            filtered = (previous[0] + (raw[0] - previous[0]) * alpha,
                        previous[1] + (raw[1] - previous[1]) * alpha)
        self._filtered = filtered

        # The target's own angular rate. What the error does is the target's
        # motion minus the camera's, and the camera is doing what this servo
        # asked for one dead time ago, so adding that back leaves the target.
        # The difference is taken on the error as measured, not on the
        # filtered one: the filter's lag would leave part of the camera's own
        # turn in the estimate, and a servo that reads its own motion as the
        # target's feeds it forward and chases itself. That was measured, as
        # 8 degrees of overshoot on a 15 degree snap.
        self._errors.append((self._age, raw[0], raw[1]))
        horizon = self.vel_window + self.dead_time + 0.2
        if len(self._errors) > 8 and self._errors[0][0] < self._age - horizon:
            self._errors = [e for e in self._errors if e[0] >= self._age - horizon]
        if target_vel_px is not None:
            measured = (self.deg_from_px(float(target_vel_px[0])), self.deg_from_px(float(target_vel_px[1])))
        else:
            measured = self._measure_rate() or (0.0, 0.0)
        cap = self.max_rate
        measured = (max(-cap, min(cap, measured[0])), max(-cap, min(cap, measured[1])))
        if self.vel_tau <= 0.0 or target_vel_px is not None:
            self._rate = measured
        else:
            beta = 1.0 - math.exp(-step / self.vel_tau)
            self._rate = (self._rate[0] + (measured[0] - self._rate[0]) * beta,
                          self._rate[1] + (measured[1] - self._rate[1]) * beta)

        lead_x, lead_y = self._rate if self.lead else (0.0, 0.0)
        stale = self.dead_time if age is None else max(0.0, float(age))
        want = (filtered[0] + lead_x * stale, filtered[1] + lead_y * stale)
        flight = self._in_flight(max(self.dead_time, stale)) if self.predict else (0.0, 0.0)
        self._predicted = (self._deduct(want[0], flight[0]), self._deduct(want[1], flight[1]))
        want_x = self.kp * self._predicted[0] + lead_x
        want_y = self.kp * self._predicted[1] + lead_y

        mx = math.copysign(self.magnitude_for_rate(want_x), want_x) if want_x else 0.0
        my_mag = self.magnitude_for_rate(want_y / self.pitch_scale)
        my = -self.pitch_up_sign * math.copysign(my_mag, want_y) if want_y else 0.0
        self._last = (mx, my)
        self._history.append((self._age,
                              math.copysign(self.rate_for_magnitude(mx), mx) if mx else 0.0,
                              math.copysign(self.rate_for_magnitude(abs(my)) * self.pitch_scale, want_y) if my else 0.0))
        if len(self._history) > 256:
            del self._history[:128]
        return self._last

    def hold(self, dt: float) -> Vec:
        """One tick with no target: hold, then decay, then nothing.

        Section 5's fail-safe rule. The last command stands for
        :attr:`hold_s` (a detector dropping one frame should not jolt the
        stick), then ramps to zero over :attr:`decay_s`, after which
        :attr:`gave_up` is true and the caller should stop.

        Parameters
        ----------
        dt : float
            Seconds since the last tick.

        Returns
        -------
        tuple of float
            The magnitudes to send this tick.
        """
        step = max(0.0, float(dt))
        self._age += step
        self._missing += step
        if self._missing <= self.hold_s:
            return self._last
        fade = 1.0 - (self._missing - self.hold_s) / self.decay_s
        fade = max(0.0, min(1.0, fade))
        return (self._last[0] * fade, self._last[1] * fade)


def blend(user: Vec, target_dir: Optional[Vec], tier: str, strength: float,
          distance_px: float = 0.0, fov_px: float = 0.0) -> Vec:
    """Steer a vector the user is producing, without enlarging it.

    The A1 and A2 tiers of the plan's ladder. Sticky scales the magnitude
    down while the reticle is near a target; gravity rotates the vector
    toward the target and leaves its magnitude alone. Both are the user's
    own vector transformed, which is what makes "no input, no output" a
    property of the arithmetic rather than a promise: a zero vector has
    nothing to scale and nothing to rotate.

    Parameters
    ----------
    user : tuple of float
        The user's shaped stick vector.
    target_dir : tuple of float, optional
        Unit-ish direction from the reticle to the target in the same frame
        as ``user``. ``None`` means no target, and the vector passes through.
    tier : str
        ``"sticky"``, ``"gravity"``, or ``"off"`` (and anything unknown) for
        no change.
    strength : float
        0 to 1, the user's setting.
    distance_px : float
        Reticle to target distance in pixels, for sticky's ramp.
    fov_px : float
        Engage radius in pixels. Outside it nothing happens.

    Returns
    -------
    tuple of float
        The vector to send, never longer than ``user``.
    """
    ux, uy = float(user[0]), float(user[1])
    magnitude = math.hypot(ux, uy)
    if magnitude <= 0.0:
        return (0.0, 0.0)
    s = max(0.0, min(1.0, float(strength)))
    if s <= 0.0 or tier not in ("sticky", "gravity"):
        return (ux, uy)
    if tier == "sticky":
        if fov_px <= 0.0 or distance_px >= fov_px:
            return (ux, uy)
        near = 1.0 - max(0.0, float(distance_px)) / float(fov_px)
        return (ux * (1.0 - s * near), uy * (1.0 - s * near))
    if target_dir is None:
        return (ux, uy)
    tx, ty = float(target_dir[0]), float(target_dir[1])
    reach = math.hypot(tx, ty)
    if reach <= 0.0 or (fov_px > 0.0 and distance_px >= fov_px):
        return (ux, uy)
    # Rotate by a fraction of the signed angle from the user's vector to the
    # target's, magnitude untouched.
    angle = math.atan2(ux * ty - uy * tx, ux * tx + uy * ty)
    turn = s * angle
    cos_t, sin_t = math.cos(turn), math.sin(turn)
    return (ux * cos_t - uy * sin_t, ux * sin_t + uy * cos_t)
