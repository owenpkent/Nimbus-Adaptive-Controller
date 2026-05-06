"""
Legacy QtWidgets main window for Nimbus Adaptive Controller.

This module hosts the original :class:`QMainWindow`-based UI assembled from
:class:`~src.qt_widgets.JoystickWidget` and
:class:`~src.qt_widgets.SliderWidget`. It is retained as a fallback shell
and for development; the primary user-facing UI is the QML application
launched from :mod:`src.qt_qml_app`.

The window wires its widgets directly to a :class:`VJoyInterface` and
exposes settings dialogs via the menu bar. It does not use the
:class:`~src.bridge.ControllerBridge`; the bridge is only used by the QML
front end.
"""
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMenuBar,
    QMenu,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QSpacerItem,
    QSizePolicy,
    QDockWidget,
)
from PySide6.QtGui import QIcon, QAction, QActionGroup
from PySide6.QtCore import Qt, QTimer
import sys

from .config import ControllerConfig
from .vjoy_interface import VJoyInterface
from .qt_widgets import JoystickWidget, SliderWidget
from .qt_dialogs import JoystickSettingsQt, ButtonSettingsQt, SliderSettingsQt, AxisMappingQt


class MainWindow(QMainWindow):
    """Legacy QtWidgets main window assembling joystick / slider widgets.

    Loads :class:`ControllerConfig`, instantiates a :class:`VJoyInterface`
    directly, and wires widget value-changed signals to vJoy axis updates.
    Provides a menu bar with access to per-axis sensitivity, button, slider,
    and axis-mapping dialogs.
    """

    def __init__(self) -> None:
        super().__init__()

        # Load configuration
        self.config = ControllerConfig()
        is_valid, error = self.config.validate_config()
        if not is_valid:
            # Validation failed; continue with defaults without showing a dialog
            pass

        # Sanitize any legacy axis mappings to supported set
        self._sanitize_axis_mappings()

        # Force UI scale to 100% at startup to avoid stale zoom effects
        # Do this before building the menu so the correct item is checked.
        self.config.set_scale_factor(1.0)
        self.config.save_config()

        # Window geometry from config
        self._apply_window_geometry_from_config()
        self.setWindowTitle("Nimbus Adaptive Controller - Virtual Controller (Qt Shell)")

        # Initialize debug borders flag early so menus can reflect its state
        self._debug_borders = bool(self.config.get("ui.debug_borders", False))

        # Menu bar
        self._build_menubar()

        # Initialize VJoy
        self.vjoy = VJoyInterface(self.config)

        # Central UI - Use a grid layout for better control
        central = QWidget(self)
        root = QGridLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)
        try:
            # Tighten columns specifically; keep some vertical breathing room
            root.setHorizontalSpacing(0)
            root.setVerticalSpacing(4)
        except Exception:
            pass

        # Debug border toggle already initialized in __init__ before menubar

        self.left_stick = JoystickWidget(self.config, which="left", parent=central)
        self.right_stick = JoystickWidget(self.config, which="right", parent=central)

        # No more titled columns - just use joysticks directly

        # Create button panels
        self.left_buttons_panel = self._create_number_buttons_panel(ids=(1, 2, 3, 4), parent=central)
        self.right_buttons_panel = self._create_number_buttons_panel(ids=(5, 6, 7, 8), parent=central)

        # Throttle - 15% shorter to avoid rudder overlap
        self.throttle = SliderWidget(self.config, Qt.Vertical, label="Throttle", auto_center=False, parent=central, gentle_return=False)
        self.throttle.setMinimumWidth(self.config.get_scaled_int(54))
        self.throttle.setMaximumWidth(self.config.get_scaled_int(72))
        self.throttle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        # Set maximum height to make it 15% shorter than the joystick size
        js = int(self.config.get("ui.joystick_size", 280))
        max_throttle_height = int(js * 0.85)
        self.throttle.setMaximumHeight(max_throttle_height)
        # Wrap throttle in a debug frame to outline center column, row 0
        self.throttle_wrap = QWidget(central)
        self._set_border(self.throttle_wrap, "rgba(240, 200, 0, 180)")
        # Keep wrapper from expanding horizontally so the center column stays narrow
        self.throttle_wrap.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        tlay = QVBoxLayout(self.throttle_wrap)
        tlay.setContentsMargins(0, 0, 0, 0)
        tlay.addWidget(self.throttle, 0, Qt.AlignCenter)

        # Grid layout: 
        # Row 0: Left stick, Throttle, Right stick (no labels)
        # Row 1: Left buttons, Rudder + ARM/RTH, Right buttons  
        
        # Nudge left joystick further left and up
        self.left_wrap = QWidget(central)
        self._set_border(self.left_wrap, "rgba(220, 80, 80, 180)")
        left_v = QVBoxLayout(self.left_wrap)
        left_v.setContentsMargins(0, 0, 0, 0)
        left_v.setSpacing(0)
        # Stronger upward bias
        left_v.addStretch(0)
        left_h = QHBoxLayout()
        left_h.setContentsMargins(0, 0, 0, 0)
        left_h.setSpacing(0)
        # Remove horizontal padding to bring border edge closer to center
        left_h.addStretch(0)
        left_h.addWidget(self.left_stick, 0, Qt.AlignTop)
        left_h.addStretch(0)
        left_v.addLayout(left_h)
        left_v.addStretch(5)

        # Nudge right joystick further left and up
        self.right_wrap = QWidget(central)
        self._set_border(self.right_wrap, "rgba(80, 120, 220, 180)")
        right_v = QVBoxLayout(self.right_wrap)
        right_v.setContentsMargins(0, 0, 0, 0)
        right_v.setSpacing(0)
        right_v.addStretch(0)
        right_h = QHBoxLayout()
        right_h.setContentsMargins(0, 0, 0, 0)
        right_h.setSpacing(0)
        # Remove horizontal padding to bring border edge closer to center
        right_h.addStretch(0)
        right_h.addWidget(self.right_stick, 0, Qt.AlignTop)
        right_h.addStretch(0)
        right_v.addLayout(right_h)
        right_v.addStretch(5)

        # Add only the center column widgets to the central grid
        root.addWidget(self.throttle_wrap, 0, 1, Qt.AlignCenter)
        
        # Button panels are now inside dock widgets; do not add to central layout
        self._set_border(self.left_buttons_panel, "rgba(220, 80, 80, 160)")
        self._set_border(self.right_buttons_panel, "rgba(80, 120, 220, 160)")
        
        # Center column: Rudder above ARM/RTH buttons
        self.center_widget = QWidget(central)
        self._set_border(self.center_widget, "rgba(240, 200, 0, 160)")
        center_layout = QVBoxLayout(self.center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(8)
        
        # Rudder
        self.rudder = SliderWidget(self.config, Qt.Horizontal, label="Rudder", auto_center=True, parent=central)
        # Keep the center column narrow: clamp rudder to a tighter fixed width
        rudder_w = self.config.get_scaled_int(160)
        self.rudder.setMinimumWidth(rudder_w)
        self.rudder.setMaximumWidth(rudder_w)
        self.rudder.setMinimumHeight(self.config.get_scaled_int(32))
        self.rudder.setMaximumHeight(self.config.get_scaled_int(44))
        center_layout.addWidget(self.rudder, 0, Qt.AlignCenter)
        
        # ARM/RTH buttons
        arm_rth_widget = QWidget(central)
        arm_rth_widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        arm_rth_widget.setMaximumWidth(rudder_w)
        arm_rth_layout = QHBoxLayout(arm_rth_widget)
        arm_rth_layout.setContentsMargins(0, 0, 0, 0)
        arm_rth_layout.addStretch(1)
        
        self.arm_btn = QPushButton("ARM", central)
        self.rth_btn = QPushButton("RTH", central)
        btn_blue_style = (
            "QPushButton:pressed, QPushButton:checked {"
            "  background-color: rgb(50,100,200);"
            "  border: 1px solid rgb(100,150,255);"
            "}"
        )
        self.arm_btn.setStyleSheet(btn_blue_style)
        self.rth_btn.setStyleSheet(btn_blue_style)
        self.arm_btn.setMinimumWidth(self.config.get_scaled_int(80))
        self.rth_btn.setMinimumWidth(self.config.get_scaled_int(80))
        
        arm_rth_layout.addWidget(self.arm_btn)
        arm_rth_layout.addSpacing(12)
        arm_rth_layout.addWidget(self.rth_btn)
        arm_rth_layout.addStretch(1)
        
        center_layout.addWidget(arm_rth_widget, 0, Qt.AlignCenter)

        # Add a horizontal bias wrapper to nudge slightly left, and top-align the group
        self.bias = QWidget(central)
        self._set_border(self.bias, "rgba(240, 200, 0, 200)")
        bias_lay = QHBoxLayout(self.bias)
        bias_lay.setContentsMargins(0, 0, 0, 0)
        bias_lay.setSpacing(0)
        bias_lay.addStretch(3)   # larger left stretch to bias content left within column
        bias_lay.addWidget(self.center_widget, 0, Qt.AlignTop)
        bias_lay.addStretch(1)

        # Constrain center column width so it doesn't push joysticks outward
        self.center_widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.center_widget.setMaximumWidth(rudder_w)
        self.bias.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.bias.setMaximumWidth(rudder_w)

        # Add bias wrapper to middle column
        root.addWidget(self.bias, 1, 1, Qt.AlignTop)
        
        # Add some spacing for the buttons row (slightly smaller to move items further up)
        root.setRowMinimumHeight(1, self.config.get_scaled_int(84))
        
        # Set row/column stretch factors - only the center column is used; zero-out unused columns (0 and 2)
        root.setRowStretch(0, 5)  # Top row (center controls)
        root.setRowStretch(1, 1)  # Bottom row (center controls)
        root.setColumnStretch(0, 0)
        root.setColumnStretch(1, 0)
        root.setColumnStretch(2, 0)
        # Ensure unused columns don't reserve space
        root.setColumnMinimumWidth(0, 0)
        root.setColumnMinimumWidth(2, 0)
        # Keep center column at minimum equal to throttle's minimum
        root.setColumnMinimumWidth(1, self.throttle.minimumWidth())

        # Set central widget (center column only)
        self.setCentralWidget(central)

        # Create left/right dock widgets for sticks + buttons
        self.leftDock = QDockWidget("Left Panel", self)
        self.leftDock.setAllowedAreas(Qt.LeftDockWidgetArea)
        self.leftDock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        left_container = QWidget(self)
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(self.left_wrap)
        left_layout.addWidget(self.left_buttons_panel, 0, Qt.AlignHCenter)
        self.leftDock.setWidget(left_container)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.leftDock)

        self.rightDock = QDockWidget("Right Panel", self)
        self.rightDock.setAllowedAreas(Qt.RightDockWidgetArea)
        self.rightDock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        right_container = QWidget(self)
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addWidget(self.right_wrap)
        right_layout.addWidget(self.right_buttons_panel, 0, Qt.AlignHCenter)
        self.rightDock.setWidget(right_container)
        self.addDockWidget(Qt.RightDockWidgetArea, self.rightDock)

        # Nudge the right dock strongly to the left by widening it substantially
        try:
            js = int(self.config.get("ui.joystick_size", 280))
            padding = self.config.get_scaled_int(64)
            target_w = js + padding
            self.rightDock.setMinimumWidth(target_w)
            self.rightDock.setMaximumWidth(target_w)
            # Resize both docks together so Qt can adjust the right edge inward
            left_w = max(self.leftDock.width(), js)  # keep left reasonable
            self.resizeDocks([self.leftDock, self.rightDock], [left_w, target_w], Qt.Horizontal)
        except Exception:
            pass

        # Remove status bar to eliminate "Ready" message and resize grip
        self.setStatusBar(None)

        # Apply initial scaling to all widgets now that they exist
        self._apply_scaled_sizes()
        # If debug borders are enabled in config, apply them now
        self._apply_debug_borders()

        # After the window is constructed, adjust dock sizes to push the right dock left
        QTimer.singleShot(0, self._adjust_dock_sizes)

        # Connections
        self.left_stick.valueChanged.connect(self._on_left_stick)
        self.right_stick.valueChanged.connect(self._on_right_stick)
        self.throttle.valueChanged.connect(self._on_throttle)
        self.rudder.valueChanged.connect(self._on_rudder)
        # ARM/RTH back to standard IDs
        self.arm_btn.pressed.connect(lambda: self._set_button(9, True))
        self.arm_btn.released.connect(lambda: self._set_button(9, False))
        self.rth_btn.pressed.connect(lambda: self._set_button(10, True))
        self.rth_btn.released.connect(lambda: self._set_button(10, False))

        # Apply current toggle/momentary modes for buttons 1-8
        self._apply_button_modes()

    # ----- Menu construction -----
    def _build_menubar(self) -> None:
        menubar: QMenuBar = self.menuBar()

        # File menu
        file_menu: QMenu = menubar.addMenu("&File")

        # Configure Axes placeholder (wired later to show existing dialog or new Qt dialog)
        self._action_configure_axes = QAction("Configure Axes", self)
        self._action_configure_axes.setShortcut("C")
        self._action_configure_axes.triggered.connect(self._on_configure_axes)
        file_menu.addAction(self._action_configure_axes)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # View menu
        view_menu: QMenu = menubar.addMenu("&View")
        size_menu: QMenu = view_menu.addMenu("Size")

        # Size options and checkable group
        self._scale_group = QActionGroup(self)
        self._scale_group.setExclusive(True)

        self._scale_options = [
            ("50%", 0.5), ("75%", 0.75), ("90%", 0.9), ("100%", 1.0),
            ("110%", 1.1), ("125%", 1.25), ("150%", 1.5), ("175%", 1.75), ("200%", 2.0),
        ]

        current_scale = float(self.config.get("ui.scale_factor", 1.0))
        for label, factor in self._scale_options:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setData(factor)
            act.triggered.connect(self._on_scale_selected)
            if abs(factor - current_scale) < 1e-6:
                act.setChecked(True)
            self._scale_group.addAction(act)
            size_menu.addAction(act)

        # Debug Borders toggle
        self._act_debug_borders = QAction("Debug Borders", self)
        self._act_debug_borders.setCheckable(True)
        self._act_debug_borders.setChecked(self._debug_borders)
        self._act_debug_borders.setShortcut("Ctrl+D")
        self._act_debug_borders.triggered.connect(self._on_toggle_debug_borders)
        view_menu.addSeparator()
        view_menu.addAction(self._act_debug_borders)

        # Profiles menu
        self._profiles_menu: QMenu = menubar.addMenu("&Profiles")
        self._profile_group = QActionGroup(self)
        self._profile_group.setExclusive(True)
        self._rebuild_profiles_menu()

        # Settings menus (each opens a dialog)
        js_menu: QMenu = menubar.addMenu("&Joystick Settings")
        js_open = QAction("Open...", self)
        js_open.triggered.connect(self._open_joystick_settings)
        js_menu.addAction(js_open)

        btn_menu: QMenu = menubar.addMenu("&Button Settings")
        btn_open = QAction("Open...", self)
        btn_open.triggered.connect(self._open_button_settings)
        btn_menu.addAction(btn_open)

        slider_menu: QMenu = menubar.addMenu("&Slider Settings")
        slider_open = QAction("Open...", self)
        slider_open.triggered.connect(self._open_slider_settings)
        slider_menu.addAction(slider_open)

    # ----- Profile management -----
    def _rebuild_profiles_menu(self) -> None:
        """Rebuild the profiles menu with available profiles."""
        self._profiles_menu.clear()
        # Clear old actions from the group
        for act in self._profile_group.actions():
            self._profile_group.removeAction(act)

        profiles = self.config.get_available_profiles()
        current = self.config.get_current_profile()

        for prof in profiles:
            act = QAction(prof["name"], self)
            act.setCheckable(True)
            act.setData(prof["id"])
            act.triggered.connect(self._on_profile_selected)
            if prof["id"] == current:
                act.setChecked(True)
            self._profile_group.addAction(act)
            self._profiles_menu.addAction(act)

    def _on_profile_selected(self) -> None:
        """Handle profile selection from menu."""
        action = self.sender()
        if not isinstance(action, QAction):
            return
        profile_id = str(action.data())
        if self.config.switch_profile(profile_id):
            # Refresh button modes after profile switch
            self._apply_button_modes()

    # ----- Actions -----
    def _on_configure_axes(self) -> None:
        # Open the Qt-native axis mapping dialog (no popups after save)
        dlg = AxisMappingQt(self.config, self)
        if dlg.exec():
            # Ensure mappings are valid after save
            self._sanitize_axis_mappings()

    def _on_scale_selected(self) -> None:
        action = self.sender()
        if not isinstance(action, QAction):
            return
        factor = float(action.data())

        # Update config and persist
        self.config.set_scale_factor(factor)
        self.config.save_config()

        # Apply new size immediately to the Qt window and widgets (so it feels responsive)
        self._apply_window_geometry_from_config()
        self._apply_scaled_sizes()
        # Do not show transient notifications to avoid layout/jitter

        # Note: Pygame canvas and widgets will be resized when we embed them.

    def _on_toggle_debug_borders(self, checked: bool) -> None:
        # Update local flag and persist
        self._debug_borders = bool(checked)
        self.config.set("ui.debug_borders", self._debug_borders)
        self.config.save_config()
        # Apply immediately
        self._apply_debug_borders()

    def _set_border(self, w: QWidget, rgba_css: str) -> None:
        if not isinstance(w, QWidget):
            return
        if self._debug_borders:
            w.setStyleSheet(f"border: 1px dashed {rgba_css};")
        else:
            # clear any previous border
            w.setStyleSheet("")

    def _apply_debug_borders(self) -> None:
        # Reapply all known borders using stored references
        self._set_border(getattr(self, 'left_wrap', None), "rgba(220, 80, 80, 180)")
        self._set_border(getattr(self, 'right_wrap', None), "rgba(80, 120, 220, 180)")
        self._set_border(getattr(self, 'throttle_wrap', None), "rgba(240, 200, 0, 180)")
        self._set_border(getattr(self, 'left_buttons_panel', None), "rgba(220, 80, 80, 160)")
        self._set_border(getattr(self, 'right_buttons_panel', None), "rgba(80, 120, 220, 160)")
        self._set_border(getattr(self, 'center_widget', None), "rgba(240, 200, 0, 160)")
        self._set_border(getattr(self, 'bias', None), "rgba(240, 200, 0, 200)")

    def _adjust_dock_sizes(self) -> None:
        """Aggressively shift the right dock left by allocating it more width and minimizing center column."""
        try:
            # Available width for docks = window width - center (throttle) - frame
            center_min = max(self.throttle_wrap.sizeHint().width(), self.throttle.minimumWidth())
            total = max(0, self.width() - center_min)
            if total <= 0:
                return
            # Give the right dock a large share to move it left (e.g., 65% of dock space)
            right_share = 0.65
            right_target = int(total * right_share)
            left_target = max(1, total - right_target)
            # Apply sizes; allow docks to be resized later by user
            self.rightDock.setMinimumWidth(1)
            self.rightDock.setMaximumWidth(16777215)
            self.leftDock.setMinimumWidth(1)
            self.leftDock.setMaximumWidth(16777215)
            self.resizeDocks([self.leftDock, self.rightDock], [left_target, right_target], Qt.Horizontal)
        except Exception:
            pass

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Keep the right dock biased left when window size changes
        QTimer.singleShot(0, self._adjust_dock_sizes)

    def _sanitize_axis_mappings(self) -> None:
        """Ensure axis_mapping.* values are within supported set to avoid 'Unknown axis' warnings."""
        supported = {"none", "x", "y", "z", "rx", "ry", "rz"}
        keys = [
            "axis_mapping.left_x",
            "axis_mapping.left_y",
            "axis_mapping.right_x",
            "axis_mapping.right_y",
            "axis_mapping.throttle",
            "axis_mapping.rudder",
        ]
        changed = False
        for k in keys:
            v = str(self.config.get(k, "none")).lower()
            if v not in supported:
                self.config.set(k, "none")
                changed = True
        if changed:
            self.config.save_config()

    # ----- Settings dialogs -----
    def _open_joystick_settings(self) -> None:
        dlg = JoystickSettingsQt(self.config, self)
        dlg.exec()

    def _open_button_settings(self) -> None:
        dlg = ButtonSettingsQt(self.config, self)
        if dlg.exec():
            # No status notifications
            # Refresh button behaviors after settings change
            self._apply_button_modes()

    def _open_slider_settings(self) -> None:
        dlg = SliderSettingsQt(self.config, self)
        dlg.exec()

    # ----- Input handlers -----
    def _apply_joystick_curve(self, x: float, y: float, which: str) -> tuple[float, float]:
        px = self.config.apply_sensitivity_curve(x, which, 'x')
        py = self.config.apply_sensitivity_curve(y, which, 'y')
        return px, py

    def _on_left_stick(self, x: float, y: float) -> None:
        x, y = self._apply_joystick_curve(x, y, 'left')
        if self.vjoy.is_connected:
            left_x_axis = self.config.get("axis_mapping.left_x", "x")
            left_y_axis = self.config.get("axis_mapping.left_y", "y")
            if left_x_axis != "none":
                self.vjoy.update_axis(left_x_axis, x)
            if left_y_axis != "none":
                self.vjoy.update_axis(left_y_axis, y)

    def _on_right_stick(self, x: float, y: float) -> None:
        x, y = self._apply_joystick_curve(x, y, 'right')
        if self.vjoy.is_connected:
            right_x_axis = self.config.get("axis_mapping.right_x", "rx")
            right_y_axis = self.config.get("axis_mapping.right_y", "ry")
            if right_x_axis != "none":
                self.vjoy.update_axis(right_x_axis, x)
            if right_y_axis != "none":
                self.vjoy.update_axis(right_y_axis, y)

    def _on_throttle(self, value: float) -> None:
        throttle_axis = self.config.get("axis_mapping.throttle", "z")
        if self.vjoy.is_connected and throttle_axis != "none":
            self.vjoy.update_axis(throttle_axis, value)

    def _on_rudder(self, value: float) -> None:
        # For now, apply same sensitivity approach as joysticks on X (optional to add dedicated curve later)
        processed = self.config.apply_sensitivity_curve(value, 'right', 'x')
        rudder_axis = self.config.get("axis_mapping.rudder", "rz")
        if self.vjoy.is_connected and rudder_axis != "none":
            self.vjoy.update_axis(rudder_axis, processed)

    def _set_button(self, button_id: int, pressed: bool) -> None:
        if self.vjoy.is_connected:
            self.vjoy.set_button(button_id, pressed)

    def _apply_window_geometry_from_config(self) -> None:
        w = int(self.config.get("ui.window_width", 1024))
        h = int(self.config.get("ui.window_height", 850))
        # Enforce a strict 16:9 aspect ratio and disable manual resizing.
        # Prefer width from config/scale and derive height to maintain 16:9.
        h_16_9 = max(1, int(round(w * 9 / 16)))
        w_16_9 = max(1, int(round(h * 16 / 9)))

        # If height from config doesn't match 16:9, override it using width as source of truth.
        # This ensures zoom (which sets width) drives the size deterministically.
        h = h_16_9

        # Apply fixed size so users cannot manually resize the window.
        self.setFixedSize(w, h)

    def closeEvent(self, event) -> None:
        try:
            if hasattr(self, 'vjoy') and self.vjoy:
                self.vjoy.shutdown()
        finally:
            super().closeEvent(event)

    # ----- Buttons 1-8 helpers -----
    def _create_number_buttons_panel(self, ids: tuple[int, int, int, int], parent: QWidget) -> QWidget:
        panel = QWidget(parent)
        grid = QGridLayout(panel)
        # No margins needed - grid layout handles spacing
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        # Ensure storage dict exists
        if not hasattr(self, 'number_buttons'):
            self.number_buttons: dict[int, QPushButton] = {}

        size = self.config.get_scaled_int(40)
        for idx, bid in enumerate(ids):
            r = idx // 2
            c = idx % 2
            btn = QPushButton(str(bid), panel)
            btn.setMinimumSize(size, size)
            self.number_buttons[bid] = btn
            # Apply blue pressed/checked style to number buttons only
            btn.setStyleSheet(
                "QPushButton:pressed, QPushButton:checked {"
                "  background-color: rgb(50,100,200);"
                "  border: 1px solid rgb(100,150,255);"
                "}"
            )
            grid.addWidget(btn, r, c)

        # Connect press/release now; toggle behavior will be configured in _apply_button_modes()
        for bid in ids:
            btn = self.number_buttons[bid]
            # Disconnect any previous specific callbacks to avoid duplicates
            if hasattr(btn, "_nimbus_press_cb") and btn._nimbus_press_cb is not None:
                try:
                    btn.pressed.disconnect(btn._nimbus_press_cb)
                except Exception:
                    pass
            if hasattr(btn, "_nimbus_release_cb") and btn._nimbus_release_cb is not None:
                try:
                    btn.released.disconnect(btn._nimbus_release_cb)
                except Exception:
                    pass
            if hasattr(btn, "_nimbus_toggle_cb") and btn._nimbus_toggle_cb is not None:
                try:
                    btn.toggled.disconnect(btn._nimbus_toggle_cb)
                except Exception:
                    pass
            # Default to momentary wiring; will adjust in _apply_button_modes
            btn.setCheckable(False)
            press_cb = (lambda b=bid: self._set_button(b, True))
            release_cb = (lambda b=bid: self._set_button(b, False))
            btn._nimbus_press_cb = press_cb
            btn._nimbus_release_cb = release_cb
            btn._nimbus_toggle_cb = None
            btn.pressed.connect(press_cb)
            btn.released.connect(release_cb)

        return panel

    def _apply_button_modes(self) -> None:
        if not hasattr(self, 'number_buttons'):
            return
        for bid, btn in self.number_buttons.items():
            toggle_mode = bool(self.config.get(f"buttons.button_{bid}.toggle_mode", False))
            # Disconnect specific previously stored callbacks to reconfigure cleanly
            if hasattr(btn, "_nimbus_press_cb") and btn._nimbus_press_cb is not None:
                try:
                    btn.pressed.disconnect(btn._nimbus_press_cb)
                except Exception:
                    pass
                btn._nimbus_press_cb = None
            if hasattr(btn, "_nimbus_release_cb") and btn._nimbus_release_cb is not None:
                try:
                    btn.released.disconnect(btn._nimbus_release_cb)
                except Exception:
                    pass
                btn._nimbus_release_cb = None
            if hasattr(btn, "_nimbus_toggle_cb") and btn._nimbus_toggle_cb is not None:
                try:
                    btn.toggled.disconnect(btn._nimbus_toggle_cb)
                except Exception:
                    pass
                btn._nimbus_toggle_cb = None
            if toggle_mode:
                btn.setCheckable(True)
                # Initialize checked state from config? default False
                btn.setChecked(False)
                toggle_cb = (lambda state, b=bid: self._set_button(b, bool(state)))
                btn._nimbus_toggle_cb = toggle_cb
                btn.toggled.connect(toggle_cb)
            else:
                btn.setCheckable(False)
                press_cb = (lambda b=bid: self._set_button(b, True))
                release_cb = (lambda b=bid: self._set_button(b, False))
                btn._nimbus_press_cb = press_cb
                btn._nimbus_release_cb = release_cb
                btn.pressed.connect(press_cb)
                btn.released.connect(release_cb)

    def _apply_scaled_sizes(self) -> None:
        """Reapply scaled minimum sizes to all widgets after a zoom change."""
        try:
            # Sticks
            if hasattr(self, 'left_stick'):
                self.left_stick.apply_scale()
            if hasattr(self, 'right_stick'):
                self.right_stick.apply_scale()
            # Sliders
            if hasattr(self, 'throttle'):
                self.throttle.apply_scale()
                # Keep throttle custom width constraints
                self.throttle.setMinimumWidth(self.config.get_scaled_int(54))
                self.throttle.setMaximumWidth(self.config.get_scaled_int(72))
                # Keep throttle 15% shorter than current joystick size
                js = int(self.config.get("ui.joystick_size", 280))
                max_throttle_height = int(js * 0.85)
                self.throttle.setMaximumHeight(max_throttle_height)
                self.throttle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            if hasattr(self, 'rudder'):
                self.rudder.apply_scale()
                # Fixed sizing for grid layout (keep center column narrow)
                fixed_w = self.config.get_scaled_int(200)
                self.rudder.setMinimumWidth(fixed_w)
                self.rudder.setMaximumWidth(fixed_w)
                self.rudder.setMinimumHeight(self.config.get_scaled_int(32))
                self.rudder.setMaximumHeight(self.config.get_scaled_int(44))
            # Number buttons
            if hasattr(self, 'number_buttons'):
                size = self.config.get_scaled_int(26)  # Slightly smaller
                for btn in self.number_buttons.values():
                    btn.setMinimumSize(size, size)
            # ARM/RTH widths (smaller)
            if hasattr(self, 'arm_btn'):
                self.arm_btn.setMinimumWidth(self.config.get_scaled_int(80))
            if hasattr(self, 'rth_btn'):
                self.rth_btn.setMinimumWidth(self.config.get_scaled_int(80))
        except Exception:
            pass


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    print("=" * 60)
    print("WARNING: You are running the alternative Qt Widgets shell.")
    print("The primary UI is the Qt Quick (QML) app.")
    print("Please use 'python run.py' to launch the main application.")
    print("=" * 60)
    print()
    raise SystemExit(main())
