"""
Spectator+: the user directs, the bridge executes.

Version 0 is scripted primitives (turn by an angle, walk for a distance,
press a button) executed as timed axis and button sequences through the
bridge's output, calibrated per game from measurements made by the game
test harness (``tests/probe_game_harness_windows.py``, ``--write-calibration``).
See ``docs/vision/GAME_TEST_HARNESS.md`` section 4.7.
"""
from .calibration import CALIBRATIONS_DIR, GameCalibration
from .policy import GAMES_DIR, AssistPolicy, GameEntry, Verdict
from .primitives import PrimitiveRunner

__all__ = ["CALIBRATIONS_DIR", "GAMES_DIR", "AssistPolicy", "GameCalibration", "GameEntry", "PrimitiveRunner", "Verdict"]
