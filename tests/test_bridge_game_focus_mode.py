"""Game Focus Mode has to survive a restart, and only when the user asked for it.

Two things set the same flag. Full Game Mode turns it on for the length of a
session and deliberately does not save it; the View menu is an explicit choice
and should come back next time. The bridge kept those apart with two config
keys, ``ui.no_focus_mode`` for the live state and ``ui.no_focus_mode_user`` for
the choice, and then never wrote the second one. The restore read a key nothing
set, so the menu came back unchecked after every restart while the config still
said the mode was on.

Nothing reports that, and the consequence is the one that is hardest to see
from inside the app: the window takes focus again, and a game stops receiving
the controller Nimbus is presenting to it.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QWindow

from tests.test_bridge_services import ControllerBridge, QApplication

from src.config import ControllerConfig


class GameFocusModePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_bridge(self, directory):
        config = ControllerConfig(str(Path(directory) / "settings.json"))
        output = Mock()
        output.is_connected = True
        output.current_values = {}
        output.button_states = {}
        bridge = ControllerBridge(config, output=output)
        # Hold a reference: without one the bridge is collected while the test
        # is still running and its timer's C++ object goes with it.
        self.bridges.append(bridge)
        return bridge, config

    def setUp(self):
        self.bridges = []

    def tearDown(self):
        for bridge in self.bridges:
            try:
                bridge._smooth_timer.stop()
            except RuntimeError:
                pass
        self.bridges = []

    def test_the_menu_choice_is_remembered_as_a_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge, config = self.make_bridge(directory)
            # The platform flag is the part this test cannot have; what it is
            # about is what gets written down.
            bridge._window = QWindow()
            with patch.object(ControllerBridge, "_apply_no_focus_flag", return_value=True), \
                 patch("src.bridge.sys.platform", "linux"):
                bridge.noFocusMode = True

            self.assertTrue(config.get("ui.no_focus_mode"), "the live state is saved")
            self.assertTrue(config.get("ui.no_focus_mode_user"),
                            "and so is the fact that the user chose it, which is what gets restored")

    def test_turning_it_off_is_remembered_too(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge, config = self.make_bridge(directory)
            bridge._window = QWindow()
            with patch.object(ControllerBridge, "_apply_no_focus_flag", return_value=True), \
                 patch("src.bridge.sys.platform", "linux"):
                bridge.noFocusMode = True
                bridge.noFocusMode = False

            self.assertFalse(config.get("ui.no_focus_mode"))
            self.assertFalse(config.get("ui.no_focus_mode_user"),
                             "otherwise the mode would come back on after it was turned off")

    def test_a_remembered_choice_is_applied_when_the_window_arrives(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge, config = self.make_bridge(directory)
            config.set("ui.no_focus_mode_user", True)

            applied = []
            with patch.object(ControllerBridge, "_apply_no_focus_flag",
                              side_effect=lambda enabled: applied.append(enabled) or True), \
                 patch("src.bridge.sys.platform", "linux"):
                bridge.setWindow(QWindow())

            self.assertEqual(applied, [True], "the saved choice is applied on startup")
            self.assertTrue(bridge.noFocusMode)

    def test_a_session_only_auto_enable_is_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge, config = self.make_bridge(directory)
            # What Full Game Mode leaves behind: the live key set, no choice recorded.
            config.set("ui.no_focus_mode", True)

            applied = []
            with patch.object(ControllerBridge, "_apply_no_focus_flag",
                              side_effect=lambda enabled: applied.append(enabled) or True), \
                 patch("src.bridge.sys.platform", "linux"):
                bridge.setWindow(QWindow())

            self.assertEqual(applied, [], "a mode the user never chose does not come back")
            self.assertFalse(bridge.noFocusMode)


if __name__ == "__main__":
    unittest.main()
