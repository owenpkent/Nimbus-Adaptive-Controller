"""
Spectator+: the user directs, the bridge executes.

Version 0 is scripted primitives (turn by an angle, walk for a distance,
press a button) executed as timed axis and button sequences through the
bridge's output, calibrated per game from measurements made by the game
test harness (``tests/probe_game_harness_windows.py``, ``--write-calibration``).
See ``docs/vision/GAME_TEST_HARNESS.md`` section 4.7.

Version 1 closes the loop: ``servo.py`` is the control law, ``targets.py``
says where the targets are, ``policy.py`` says where any of it may run at
all, and ``PrimitiveRunner.look_at`` is the primitive that joins them.
See ``docs/vision/TARGET_AWARE_AIM_PLAN.md``.
"""
from .calibration import CALIBRATIONS_DIR, GameCalibration
from .policy import GAMES_DIR, AssistPolicy, GameEntry, Verdict
from .primitives import PrimitiveRunner
from .servo import Servo, blend, focal_px, steady_rates
from .targets import ScriptTargetSource, Target, TargetFrame, TargetSelector, TargetSource

__all__ = ["CALIBRATIONS_DIR", "GAMES_DIR", "AssistPolicy", "GameCalibration", "GameEntry", "PrimitiveRunner",
           "ScriptTargetSource", "Servo", "Target", "TargetFrame", "TargetSelector", "TargetSource", "Verdict",
           "blend", "focal_px", "steady_rates"]
