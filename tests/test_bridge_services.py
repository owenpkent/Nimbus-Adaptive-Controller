"""Qt bridge integration with fake services and no native input modules."""
import os
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"

from PySide6.QtCore import QObject, Signal, QUrl, QMetaObject, Qt
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtWidgets import QApplication

from src.controller_output import ControllerOutput

native_modules = {}
for module_name in ("src.vjoy_interface", "src.vigem_interface", "src.window_utils",
                    "src.borderless", "src.mouse_hider", "src.mouse_isolation_win"):
    native_modules[module_name] = types.ModuleType(module_name)
native_modules["src.vjoy_interface"].VJoyInterface = Mock
native_modules["src.vigem_interface"].ViGEmInterface = Mock
native_modules["src.vigem_interface"].VIGEM_AVAILABLE = True
native_modules["src.mouse_isolation_win"].MOUSE_ISOLATION_AVAILABLE = False
previous_modules = {name: sys.modules.get(name) for name in native_modules}
sys.modules.update(native_modules)
try:
    from src.bridge import ControllerBridge, UINPUT_AVAILABLE
finally:
    for module_name, previous in previous_modules.items():
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


class FakeCloud(QObject):
    authStateChanged = Signal(bool)
    userChanged = Signal(str)
    entitlementChanged = Signal(str)
    syncCompleted = Signal(bool)
    profileUpdated = Signal(str)

    def __init__(self):
        super().__init__()
        self.is_authenticated = False
        self.display_name = "Not signed in"
        self.user = None
        self.tier = "free"
        self.is_premium = False
        self.login_with_email = Mock(return_value=True)
        self.signup_with_email = Mock(return_value=True)
        self.login_with_browser = Mock()
        self.sync_profiles = Mock(return_value=True)
        self.logout = Mock()


class FakeUpdater(QObject):
    updateAvailable = Signal(str, str, str)
    forceUpdateRequired = Signal(str, str)
    noUpdateAvailable = Signal()
    checkFailed = Signal(str)

    def __init__(self):
        super().__init__()
        self.check = Mock()
        self.open_download_page = Mock()
        self.dismiss = Mock()


class BridgeServicesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Mock()
        self.config.get.side_effect = lambda key, default=None: default
        self.config.get_layout_type.return_value = "custom"
        self.config.get_current_profile.return_value = "test"
        self.config.get_current_profile_data.return_value = {"custom_layout": {"widgets": []}}
        self.config.get_available_profiles.return_value = []
        self.config.is_builtin_profile.return_value = False
        self.services = types.SimpleNamespace(
            cloud=FakeCloud(), updater=FakeUpdater(),
            telemetry=types.SimpleNamespace(analytics_enabled=False, crash_reports_enabled=False))
        self.output = ControllerOutput(self.config, Mock(return_value=Mock(is_connected=True)),
                                       Mock(return_value=Mock(is_connected=True)), True)
        self.bridge = ControllerBridge(self.config, output=self.output, services=self.services)
        self.addCleanup(self.bridge._smooth_timer.stop)

    def test_service_commands_do_not_require_a_qobject_parent(self):
        self.assertIsNone(self.bridge.parent())
        self.assertTrue(self.bridge.loginWithEmail("test@example.invalid", "test-only"))
        self.services.cloud.login_with_email.assert_called_once_with("test@example.invalid", "test-only")
        self.assertTrue(self.bridge.signupWithEmail("test@example.invalid", "test-only"))
        self.assertTrue(self.bridge.syncProfiles())
        self.bridge.loginWithProvider("google")
        self.services.cloud.login_with_browser.assert_called_once_with("google")
        self.bridge.logoutAccount()
        self.services.cloud.logout.assert_called_once_with()
        self.bridge.checkForUpdates()
        self.services.updater.check.assert_called_once_with()
        self.bridge.dismissUpdate()
        self.services.updater.dismiss.assert_called_once_with()

    def test_account_and_privacy_properties_are_in_qt_metadata(self):
        meta = self.bridge.metaObject()
        for name in ("accountAuthenticated", "accountDisplayName", "accountEmail", "accountTier",
                     "accountPremium", "analyticsEnabled", "crashReportsEnabled"):
            self.assertGreaterEqual(meta.indexOfProperty(name), 0, name)
        for signature in ("signupWithEmail(QString,QString)", "syncProfiles()", "setAnalyticsEnabled(bool)"):
            self.assertGreaterEqual(meta.indexOfMethod(signature), 0, signature)

    def test_privacy_changes_reach_live_services(self):
        notifications = []
        self.bridge.privacyChanged.connect(lambda: notifications.append(True))
        self.bridge.setAnalyticsEnabled(True)
        self.bridge.setCrashReportsEnabled(True)
        self.assertTrue(self.services.telemetry.analytics_enabled)
        self.assertTrue(self.bridge.crashReportsEnabled)
        self.bridge.setAnalyticsEnabled(False)
        self.assertFalse(self.bridge.analyticsEnabled)
        self.assertEqual(len(notifications), 3)

    def test_service_signals_reach_bridge(self):
        accounts = []
        updates = []
        self.bridge.accountStateChanged.connect(lambda: accounts.append(True))
        self.bridge.updateAvailable.connect(lambda *args: updates.append(args))
        self.services.cloud.is_authenticated = True
        self.services.cloud.authStateChanged.emit(True)
        self.services.updater.updateAvailable.emit("2.0.0", "https://example.invalid", "Notes")
        self.assertTrue(self.bridge.accountAuthenticated)
        self.assertEqual(accounts, [True])
        self.assertEqual(updates, [("2.0.0", "https://example.invalid", "Notes")])

    def test_bridge_uses_output_owner_without_extra_input(self):
        original = self.output.vigem
        self.bridge.setOutputMode("vjoy")
        self.assertIs(self.bridge._get_active_interface(), self.output.vjoy)
        if UINPUT_AVAILABLE:
            # On Linux each back end is a separate virtual pad that games can
            # see, so the deselected one is destroyed rather than left attached
            # (ControllerBridge._retire_inactive_interface). Shutting it down is
            # the only thing the bridge may do to it: no input, no reset.
            self.assertIsNone(self.bridge._vigem)
            self.assertEqual(original.mock_calls, [call.shutdown()])
        else:
            # Windows keeps both drivers attached, vJoy standing by as the
            # ViGEm fallback, and touches neither on a mode switch.
            self.assertIs(self.bridge._vigem, original)
            self.assertEqual(original.mock_calls, [])
        self.config.set.assert_called_with("controller.prefer_vigem", False)

    def test_qml_components_load_and_react_without_service_context_objects(self):
        engine = QQmlEngine()
        engine.rootContext().setContextProperty("controller", self.bridge)
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(str(message) for message in messages))
        component = QQmlComponent(engine)
        source = b'''import QtQuick
import QtQuick.Controls
import "components" as Comp
ApplicationWindow {
    width: 1024; height: 768
    Comp.AccountDialog { objectName: "account" }
    Comp.SettingsPrivacyDialog { objectName: "privacy" }
    Comp.UpdateNotification { objectName: "updates" }
}'''
        base = Path(__file__).resolve().parents[1] / "qml" / "Main.qml"
        component.setData(source, QUrl.fromLocalFile(str(base)))
        self.assertEqual(component.errors(), [])
        window = component.create()
        self.assertIsNotNone(window, [error.toString() for error in component.errors()])
        account = window.findChild(QObject, "account")
        self.assertEqual(account.property("title"), "Sign In")
        self.services.cloud.is_authenticated = True
        self.services.cloud.authStateChanged.emit(True)
        self.assertEqual(account.property("title"), "Account")
        toggle = window.findChild(QObject, "analyticsSwitch")
        toggle.setProperty("checked", True)
        self.assertTrue(QMetaObject.invokeMethod(toggle, "toggled", Qt.ConnectionType.DirectConnection))
        self.assertTrue(self.services.telemetry.analytics_enabled)
        self.services.updater.updateAvailable.emit("2.0.0", "https://example.invalid", "Notes")
        self.assertEqual(window.findChild(QObject, "updates").property("latestVersion"), "2.0.0")
        self.assertEqual(warnings, [])
        window.close()

    def test_application_startup_injects_services_and_shuts_them_down(self):
        from src import qt_qml_app
        self.services.shutdown = Mock()
        self.services.telemetry.track_session_start = Mock()
        engine = qt_qml_app.QQmlApplicationEngine()
        with patch.object(qt_qml_app, "ControllerConfig", return_value=self.config), \
                patch.object(qt_qml_app, "ApplicationServices", return_value=self.services), \
                patch.object(qt_qml_app, "ControllerBridge", return_value=self.bridge) as factory, \
                patch.object(qt_qml_app, "QQmlApplicationEngine", return_value=engine), \
                patch.object(qt_qml_app, "_create_splash", return_value=None), \
                patch.object(self.app, "exec", return_value=0):
            self.assertEqual(qt_qml_app.main(), 0)
        factory.assert_called_once_with(self.config, self.app, services=self.services)
        self.services.shutdown.assert_called_once_with()
        self.assertTrue(engine.rootObjects())
        for window in engine.rootObjects():
            window.close()

    def test_failed_layout_save_does_not_replace_live_shaping(self):
        results = []
        self.bridge.profileSaved.connect(results.append)
        self.config.save_custom_layout.return_value = False
        with patch.object(self.bridge, "_reload_widget_shaping") as reload_shaping:
            self.bridge.saveCustomLayout("[]", 10, True)
            reload_shaping.assert_not_called()
        self.assertEqual(results, [False])

    def test_layout_save_as_reports_result_and_preserves_active_cache(self):
        results = []
        notifications = []
        self.bridge.profileSaved.connect(results.append)
        self.bridge.profilesListChanged.connect(lambda: notifications.append(True))
        self.config.profiles.unique_id.return_value = "safe_name"
        self.bridge._widget_shaping = {"existing": {"id": "existing"}}
        self.config.save_profile_as.return_value = False
        self.bridge.saveCustomLayoutAs("../name", "[]", 10, True)
        self.assertEqual(results, [False])
        self.assertEqual(notifications, [])
        self.config.save_profile_as.return_value = True
        self.bridge.saveCustomLayoutAs("../name", "[]", 10, True)
        self.assertEqual(results, [False, True])
        self.assertEqual(notifications, [True])
        self.assertEqual(self.config.save_profile_as.call_args.args[0], "safe_name")
        self.assertIn("existing", self.bridge._widget_shaping)

    def test_reset_updates_real_canvas_before_its_next_save(self):
        from src.config import ControllerConfig
        from src.profile_repository import ProfileRepository
        with tempfile.TemporaryDirectory() as directory:
            root_dir = Path(directory)
            bundled = root_dir / "bundled"
            bundled.mkdir()
            defaults = ProfileRepository(bundled, root_dir / "unused")
            defaults.save("adaptive_platform_2", {"name": "Default", "layout_type": "custom",
                "custom_layout": {"widgets": [{"id": "stock", "type": "button", "button_id": 1}]}})
            with patch.object(ControllerConfig, "_get_user_data_dir", return_value=root_dir / "data"), \
                    patch.object(ControllerConfig, "_get_bundled_profiles_dir", return_value=bundled):
                config = ControllerConfig(str(root_dir / "settings.json"))
            config.save_custom_layout([{"id": "edited", "type": "button", "button_id": 2}])
            output = ControllerOutput(config, Mock(return_value=Mock(is_connected=True)),
                                       Mock(return_value=Mock(is_connected=True)), True)
            bridge = ControllerBridge(config, output=output)
            engine = QQmlEngine()
            engine.rootContext().setContextProperty("controller", bridge)
            component = QQmlComponent(engine)
            component.setData(b'import QtQuick; import "layouts"; Item { width: 1024; height: 600; CustomLayout { anchors.fill: parent } }',
                              QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "qml" / "Main.qml")))
            window = component.create()
            self.assertIsNotNone(window, component.errors())
            layout = window.findChild(QObject, "customLayout")
            self.assertTrue(bridge.resetProfile("adaptive_platform_2"))
            self.assertEqual([widget["id"] for widget in layout.property("widgetModel").toVariant()], ["stock"])
            QMetaObject.invokeMethod(layout, "_saveLayout", Qt.ConnectionType.DirectConnection)
            self.assertEqual(config.get_current_profile_data()["custom_layout"]["widgets"][0]["id"], "stock")
            self.assertEqual(list(bridge._widget_shaping), ["stock"])
            bridge._smooth_timer.stop()

    def test_remote_profile_update_refreshes_active_profile(self):
        notifications = []
        self.bridge.profileChanged.connect(notifications.append)
        self.services.cloud.profileUpdated.emit("test")
        self.config.switch_profile.assert_called_once_with("test")
        self.assertEqual(notifications, ["test"])

    def test_a_primitive_ending_gives_back_a_held_stick_but_a_stop_leaves_neutral(self):
        runner = self.bridge.get_spectator()
        w = {"id": "rs", "type": "joystick", "mapping": {"axis_x": "rx", "axis_y": "ry"}}
        self.bridge._widget_shaping = {"rs": w}
        self.bridge._last_raw = {"rs": (0.5, 0.0)}
        with patch.object(self.bridge, "_drive_stick") as drive:
            runner.last_reason = "settled"
            runner.finished.emit("look_at", True)
            drive.assert_called_once_with("rs", w, 0.5, 0.0, advance_filter=False)
            drive.reset_mock()
            runner.last_reason = "stopped"
            runner.finished.emit("look_at", False)
            drive.assert_not_called()

    def test_user_stick_is_measured_from_where_that_widget_was(self):
        runner = Mock(watching_user=True)
        self.bridge._spectator = runner
        self.bridge._widget_shaping = {"rs": {"id": "rs", "travel_px": 100}}
        with patch.object(self.bridge, "_drive_stick"):
            self.bridge.setStickInput("rs", 0.25, 0.0)
            self.bridge.setStickInput("rs", 0.75, 0.0)
        first, second = runner.note_user_stick.call_args_list
        self.assertEqual(first, call(25.0, 0.0, key="rs", start_px=(0.0, 0.0)))
        self.assertEqual(second, call(75.0, 0.0, key="rs", start_px=(25.0, 0.0)))

    def test_full_game_mode_cannot_bypass_injected_factory(self):
        from src import bridge as bridge_module
        self.output.vigem = None
        self.output.use_vigem = False
        factory = self.output._vigem_factory
        factory.reset_mock()
        with patch.multiple(bridge_module, WINDOW_UTILS_AVAILABLE=False, BORDERLESS_AVAILABLE=False,
                            MOUSE_HIDER_AVAILABLE=False, MOUSE_ISOLATION_AVAILABLE=False), \
                patch.object(bridge_module, "ViGEmInterface") as global_factory:
            self.bridge.startFullGameMode(0, 30)
            factory.assert_called_once_with(self.config)
            global_factory.assert_not_called()
            self.assertFalse(self.output.use_vigem)
            self.output.vigem = None
            self.output.vigem_available = False
            factory.reset_mock()
            self.bridge.startFullGameMode(0, 30)
            factory.assert_not_called()
            global_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()