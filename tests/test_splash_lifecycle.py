"""Splash screen lifecycle: shown during init, closed before the event loop runs."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_bridge_services import ControllerBridge, QApplication
from tests.test_application_startup import FakeOutput
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QSplashScreen

from src import qt_qml_app
from src.application_services import ApplicationServices
from src.cloud_client import CloudClient
from src.config import ControllerConfig
from src.profile_repository import ProfileRepository

PROJECT_ROOT = Path(qt_qml_app.__file__).resolve().parent.parent


class FakeSplash:
    """Recorder standing in for the QSplashScreen ``main()`` drives.

    Records every ``showMessage``/``close`` call in order so tests can
    assert the splash was closed exactly once, after all progress
    messages, and before the event loop started.
    """

    def __init__(self):
        self.events = []

    def showMessage(self, message, *args, **kwargs):
        self.events.append(("message", message.strip()))

    def close(self):
        self.events.append(("close",))

    @property
    def closed(self) -> bool:
        return ("close",) in self.events


class SplashLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _patched_main(self, splash, root_dir, qml_override=None):
        """Run ``qt_qml_app.main()`` with external effects replaced.

        Parameters
        ----------
        splash : FakeSplash
            Returned from the patched ``_create_splash``.
        root_dir : Path
            Temporary directory for config, profiles, and caches.
        qml_override : Path, optional
            Replacement result for ``qml_path()`` (missing file forces the
            QML-load failure branch).

        Returns
        -------
        int
            The exit code from ``main()``.
        """
        bundled = root_dir / "bundled"
        bundled.mkdir(exist_ok=True)
        ProfileRepository(bundled, root_dir / "unused").save("adaptive_platform_2",
            {"name": "Default", "layout_type": "custom", "custom_layout": {"widgets": []}})
        config_path = root_dir / "settings.json"
        config_constructor = ControllerConfig

        def create_config():
            return config_constructor(str(config_path))

        patches = [
            patch.object(ControllerConfig, "_get_user_data_dir", return_value=root_dir / "data"),
            patch.object(ControllerConfig, "_get_bundled_profiles_dir", return_value=bundled),
            patch.object(qt_qml_app, "ControllerConfig", side_effect=create_config),
            patch.object(qt_qml_app, "_create_splash", return_value=splash),
            patch.object(CloudClient, "_restore_session"),
            patch("src.cloud_client._get_cache_dir", return_value=root_dir / "cache"),
            patch("src.telemetry._get_telemetry_dir", return_value=root_dir),
            patch("src.bridge.VJoyInterface", FakeOutput),
            patch("src.bridge.ViGEmInterface", FakeOutput),
            patch("src.updater.QNetworkAccessManager", return_value=Mock()),
            patch("httpx.post"), patch("httpx.get"),
        ]
        if qml_override is not None:
            patches.append(patch.object(qt_qml_app, "qml_path", return_value=qml_override))
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            return qt_qml_app.main()

    def _cleanup_singletons(self):
        bridge = self.app.findChild(ControllerBridge)
        if bridge is not None:
            bridge._smooth_timer.stop()
            bridge.deleteLater()
        services = self.app.findChild(ApplicationServices)
        if services is not None:
            self.assertTrue(services._closed)
            services.deleteLater()

    def test_splash_closes_before_event_loop_starts(self):
        splash = FakeSplash()
        failures = []
        ran = []

        def exercise():
            # main() pumps processEvents() while the splash is up, which can
            # fire this timer during init. The QML window only exists after
            # engine.load(), and no processEvents() runs between load and
            # exec(), so once the window is there we are inside app.exec().
            bridge = self.app.findChild(ControllerBridge)
            if bridge is None or bridge._window is None:
                QTimer.singleShot(0, exercise)
                return
            ran.append(True)
            try:
                # Inside app.exec(): the splash must already be gone, or its
                # stay-on-top window covers the app all session.
                self.assertTrue(splash.closed, "splash still open once the event loop is running")
            except Exception as exc:
                failures.append(exc)
            finally:
                bridge._window.close()
                self.app.quit()

        with tempfile.TemporaryDirectory() as directory:
            QTimer.singleShot(0, exercise)
            QTimer.singleShot(10000, self.app.quit)   # backstop: never hang the suite
            self.assertEqual(self._patched_main(splash, Path(directory)), 0)
        self._cleanup_singletons()
        if failures:
            raise failures[0]
        self.assertTrue(ran, "event-loop probe never ran; cannot trust this test")
        # The finally may close a second time (idempotent), but no progress
        # message may follow the first close.
        first_close = splash.events.index(("close",))
        self.assertTrue(all(event == ("close",) for event in splash.events[first_close:]))
        messages = [event[1] for event in splash.events if event[0] == "message"]
        self.assertIn("Loading configuration...", messages)
        self.assertIn("Initializing controllers...", messages)
        self.assertIn("Loading interface...", messages)

    def test_splash_closes_when_qml_fails_to_load(self):
        splash = FakeSplash()
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "does_not_exist.qml"
            self.assertEqual(self._patched_main(splash, Path(directory), qml_override=missing), 1)
        self._cleanup_singletons()
        self.assertTrue(splash.closed, "splash left open after QML load failure")

    def test_create_splash_returns_none_without_logo(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(qt_qml_app._create_splash(Path(directory)))

    @unittest.skipUnless((PROJECT_ROOT / "logo.png").exists(), "logo.png not present")
    def test_create_splash_builds_stay_on_top_window(self):
        splash = qt_qml_app._create_splash(PROJECT_ROOT)
        try:
            self.assertIsInstance(splash, QSplashScreen)
            self.assertTrue(splash.windowFlags() & Qt.WindowStaysOnTopHint)
            self.assertFalse(splash.pixmap().isNull())
        finally:
            if splash is not None:
                splash.close()
                splash.deleteLater()


if __name__ == "__main__":
    unittest.main()
