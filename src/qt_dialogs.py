"""
Settings dialogs for Nimbus Adaptive Controller.

Hosts the Qt dialogs reachable from the menu bar / QML and shared between
the legacy widget shell and the QML front end:

* :class:`AxisSettingsQt` — unified per-axis sensitivity/deadzone editor
  with a live curve preview.
* :class:`JoystickSettingsQt` — sensitivity / deadzone / extremity-deadzone
  for both joysticks with a live curve preview.
* :class:`SliderSettingsQt` — same shape as JoystickSettingsQt but for
  trigger/slider axes; title adapts to the active profile.
* :class:`AxisMappingQt` — map UI joystick axes to vJoy axes.
* :class:`ButtonSettingsQt` — view / edit the button-to-vJoy mapping and
  per-button toggle mode.

Internal helpers:

* ``_LabeledSlider`` — :class:`QSlider` with a live ``"<title>: NN%"``
  caption.
* ``_CurvePreview`` — paints the input -> output curve so users can see
  the effect of their settings before applying.

All dialogs read and write :class:`~src.config.ControllerConfig`. Curve
math in the previews is kept in lock-step with
:func:`~src.config.ControllerConfig.apply_joystick_dialog_curve` so the
preview matches runtime feel exactly.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QSlider,
    QPushButton,
    QWidget,
    QGridLayout,
    QCheckBox,
    QDialogButtonBox,
    QComboBox,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QFrame,
    QSplitter,
    QGroupBox,
    QSizePolicy,
    QScrollArea,
)
from PySide6.QtGui import QPainter, QPen, QBrush, QColor, QFont

from .config import ControllerConfig


class _LabeledSlider(QWidget):
    """Horizontal :class:`QSlider` paired with a live ``"<title>: NN%"`` label.

    Displays values as integer percentages. Used by the settings dialogs to
    keep a consistent look-and-feel; not part of the public API.
    """

    def __init__(self, title: str, min_val: int, max_val: int, value: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = QLabel(f"{title}: {value}%", self)
        self._slider = QSlider(Qt.Horizontal, self)
        self._slider.setRange(min_val, max_val)
        self._slider.setValue(value)
        self._title = title
        self._slider.valueChanged.connect(self._on_changed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._label)
        lay.addWidget(self._slider)

    def _on_changed(self, v: int) -> None:
        self._label.setText(f"{self._title}: {v}%")

    def value(self) -> int:
        return int(self._slider.value())

    def setValue(self, v: int) -> None:
        self._slider.setValue(int(v))


class _CurvePreview(QWidget):
    """
    Draws a live XY curve preview based on sensitivity, deadzone, and extremity deadzone.
    Matches the behavior of the existing Pygame dialogs.
    """
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.sensitivity = 50.0
        self.deadzone = 10.0
        self.extremity = 5.0

    def setParams(self, sensitivity: float, deadzone: float, extremity: float) -> None:
        changed = (
            abs(self.sensitivity - sensitivity) > 1e-6 or
            abs(self.deadzone - deadzone) > 1e-6 or
            abs(self.extremity - extremity) > 1e-6
        )
        self.sensitivity = float(sensitivity)
        self.deadzone = float(deadzone)
        self.extremity = float(extremity)
        if changed:
            self.update()

    def _calc_output(self, input_value: float) -> float:
        # Ported from pygame dialogs
        deadzone = (self.deadzone / 100.0) * 0.25
        extremity_deadzone = self.extremity / 100.0
        sensitivity = self.sensitivity / 100.0

        if abs(input_value) < deadzone:
            return 0.0

        sign = 1.0 if input_value >= 0 else -1.0
        abs_input = abs(input_value)
        available_range = 1.0 - deadzone
        normalized_input = (abs_input - deadzone) / max(1e-6, available_range)

        if abs(sensitivity - 0.5) < 1e-9:
            output = normalized_input
        elif sensitivity < 0.5:
            power = 1.0 + (0.5 - sensitivity) * 6.0
            output = pow(normalized_input, power)
        else:
            power = 1.0 - (sensitivity - 0.5) * 1.8
            output = pow(normalized_input, max(0.1, power))

        if extremity_deadzone > 0:
            max_output = 1.0 - extremity_deadzone
            output *= max_output

        return output * sign

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # Background
        p.fillRect(self.rect(), QColor(30, 30, 30))

        # Graph rect with margins
        margin = 16
        gx = margin
        gy = margin
        gw = max(1, self.width() - margin * 2)
        gh = max(1, self.height() - margin * 2)

        # Border
        p.setPen(QPen(QColor(100, 100, 100), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(gx, gy, gw, gh)

        # Axes
        cx = gx + gw // 2
        cy = gy + gh // 2
        p.setPen(QPen(QColor(150, 150, 150), 2))
        p.drawLine(gx, cy, gx + gw, cy)
        p.drawLine(cx, gy, cx, gy + gh)

        # Grid (optional light)
        p.setPen(QPen(QColor(60, 60, 60), 1))
        for i in range(1, 5):
            # verticals
            x = gx + i * gw // 5
            p.drawLine(x, gy, x, gy + gh)
            # horizontals
            y = gy + i * gh // 5
            p.drawLine(gx, y, gx + gw, y)

        # Curve points
        p.setPen(QPen(QColor(100, 150, 255), 3))

        def to_screen(ix: float, oy: float) -> tuple[int, int]:
            # ix in [-1,1] maps to x across gw
            # oy in [-1,1] maps to y across gh (invert for screen)
            x = cx + int(ix * (gw / 2))
            y = cy - int(oy * (gh / 2))
            return x, y

        # Positive side 0..1
        prev = None
        for i in range(0, 201):
            iv = i / 200.0
            ov = self._calc_output(iv)
            x, y = to_screen(iv, ov)
            if prev is not None:
                p.drawLine(prev[0], prev[1], x, y)
            prev = (x, y)

        # Negative side -1..0
        prev = None
        for i in range(0, 201):
            iv = -i / 200.0
            ov = self._calc_output(iv)
            x, y = to_screen(iv, ov)
            if prev is not None:
                p.drawLine(prev[0], prev[1], x, y)
            prev = (x, y)


class AxisSettingsQt(QDialog):
    """Modern unified axis sensitivity settings dialog with per-axis configuration."""
    
    # Default values for each axis type
    DEFAULTS = {
        "joystick": {"sensitivity": 35.0, "deadzone": 0.0, "extremity_deadzone": 38.0},
        "trigger": {"sensitivity": 50.0, "deadzone": 10.0, "extremity_deadzone": 5.0},
    }
    
    def __init__(self, config: ControllerConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Axis Sensitivity Settings")
        self.setMinimumSize(700, 500)
        self.config = config
        
        # Get layout type for profile-aware axis names
        self.layout_type = self.config.get_layout_type()
        
        # Define axes based on profile
        self.axes = self._get_axes_for_profile()
        
        # Store settings for each axis
        self.axis_settings: dict[str, dict[str, float]] = {}
        self.axis_mappings: dict[str, str] = {}  # axis_id -> vjoy_axis
        self._load_all_settings()
        self._load_all_mappings()
        
        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(16, 16, 16, 16)
        
        # Apply dark theme
        self.setStyleSheet("""
            QDialog { background-color: #1e1e1e; }
            QLabel { color: #e0e0e0; }
            QGroupBox { 
                color: #e0e0e0; 
                border: 1px solid #444;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QListWidget {
                background-color: #2a2a2a;
                border: 1px solid #444;
                border-radius: 6px;
                color: #e0e0e0;
                outline: none;
            }
            QListWidget::item {
                padding: 10px 14px;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #0078d4;
                color: white;
            }
            QListWidget::item:hover:!selected {
                background-color: #3a3a3a;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #3a3a3a;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #0078d4;
                width: 16px;
                height: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
            QSlider::handle:horizontal:hover {
                background: #1a8cff;
            }
            QPushButton {
                background-color: #3a3a3a;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 8px 16px;
                color: #e0e0e0;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
            }
            QPushButton:pressed {
                background-color: #2a2a2a;
            }
            QPushButton#primaryBtn {
                background-color: #0078d4;
                border: none;
            }
            QPushButton#primaryBtn:hover {
                background-color: #1a8cff;
            }
            QComboBox {
                background-color: #2a2a2a;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 6px 12px;
                color: #e0e0e0;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 6px solid #888;
                margin-right: 8px;
            }
            QComboBox QAbstractItemView {
                background-color: #2a2a2a;
                border: 1px solid #444;
                selection-background-color: #0078d4;
                color: #e0e0e0;
            }
        """)
        
        # Content splitter (axis list | settings panel)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        
        # Left panel - axis list
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 8, 0)
        
        axis_label = QLabel("Axes")
        axis_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        left_layout.addWidget(axis_label)
        
        self.axis_list = QListWidget()
        self.axis_list.setMinimumWidth(180)
        for axis_id, axis_name in self.axes:
            item = QListWidgetItem(axis_name)
            item.setData(Qt.UserRole, axis_id)
            self.axis_list.addItem(item)
        self.axis_list.currentRowChanged.connect(self._on_axis_selected)
        left_layout.addWidget(self.axis_list)
        
        splitter.addWidget(left_panel)
        
        # Right panel - scrollable settings
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.NoFrame)
        right_scroll.setStyleSheet("""
            QScrollArea { background: transparent; }
            QScrollBar:vertical {
                background: #2a2a2a;
                width: 10px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #555;
                border-radius: 5px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #777;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 0, 0, 0)
        
        # Axis name header
        self.axis_header = QLabel("Select an axis")
        self.axis_header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        right_layout.addWidget(self.axis_header)
        
        # VJoy axis mapping dropdown
        mapping_layout = QHBoxLayout()
        mapping_label = QLabel("VJoy Axis:")
        mapping_label.setStyleSheet("font-weight: 600;")
        self.vjoy_axes = ["none", "x", "y", "z", "rx", "ry", "rz"]
        self.mapping_combo = QComboBox()
        self.mapping_combo.addItems([a.upper() for a in self.vjoy_axes])
        self.mapping_combo.currentIndexChanged.connect(self._on_mapping_changed)
        mapping_layout.addWidget(mapping_label)
        mapping_layout.addWidget(self.mapping_combo, 1)
        right_layout.addLayout(mapping_layout)
        
        # Copy from dropdown
        copy_layout = QHBoxLayout()
        copy_label = QLabel("Copy sensitivity from:")
        self.copy_combo = QComboBox()
        self.copy_combo.addItem("-- Select --", None)
        for axis_id, axis_name in self.axes:
            self.copy_combo.addItem(axis_name, axis_id)
        self.copy_combo.currentIndexChanged.connect(self._on_copy_from)
        copy_layout.addWidget(copy_label)
        copy_layout.addWidget(self.copy_combo, 1)
        right_layout.addLayout(copy_layout)
        
        # Settings group
        settings_group = QGroupBox("Sensitivity Settings")
        settings_layout = QVBoxLayout(settings_group)
        settings_layout.setSpacing(16)
        
        # Sensitivity slider
        self.sens_slider = _LabeledSlider("Sensitivity", 0, 100, 50, self)
        settings_layout.addWidget(self.sens_slider)
        
        # Deadzone slider
        self.dead_slider = _LabeledSlider("Deadzone", 0, 100, 0, self)
        settings_layout.addWidget(self.dead_slider)
        
        # Extremity deadzone slider
        self.ext_slider = _LabeledSlider("Extremity Deadzone", 0, 100, 5, self)
        settings_layout.addWidget(self.ext_slider)
        
        right_layout.addWidget(settings_group)
        
        # Curve preview
        preview_group = QGroupBox("Response Curve Preview")
        preview_layout = QVBoxLayout(preview_group)
        self.preview = _CurvePreview(self)
        self.preview.setMinimumSize(280, 180)
        preview_layout.addWidget(self.preview)
        right_layout.addWidget(preview_group)
        
        # Connect sliders to preview
        self.sens_slider._slider.valueChanged.connect(self._on_setting_changed)
        self.dead_slider._slider.valueChanged.connect(self._on_setting_changed)
        self.ext_slider._slider.valueChanged.connect(self._on_setting_changed)
        
        # Reset button
        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.clicked.connect(self._reset_current)
        right_layout.addWidget(reset_btn)
        
        right_scroll.setWidget(right_panel)
        splitter.addWidget(right_scroll)
        splitter.setSizes([200, 500])
        
        main_layout.addWidget(splitter, 1)
        
        # Dialog buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        save_btn = QPushButton("Save")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_and_close)
        btn_layout.addWidget(save_btn)
        
        main_layout.addLayout(btn_layout)
        
        # Select first axis
        if self.axis_list.count() > 0:
            self.axis_list.setCurrentRow(0)
    
    def _get_axes_for_profile(self) -> list[tuple[str, str]]:
        """Get list of (axis_id, display_name) for current profile."""
        axes = [
            ("left_x", "Left Stick X"),
            ("left_y", "Left Stick Y"),
            ("right_x", "Right Stick X"),
            ("right_y", "Right Stick Y"),
        ]
        
        if self.layout_type == "adaptive":
            axes.extend([
                ("left_trigger", "Left Trigger (LT)"),
                ("right_trigger", "Right Trigger (RT)"),
            ])
        else:
            axes.extend([
                ("throttle", "Throttle"),
                ("rudder", "Rudder"),
            ])
        
        return axes
    
    def _get_config_key(self, axis_id: str, setting: str) -> str:
        """Get the config key for an axis setting."""
        # Map axis IDs to config keys
        if axis_id in ("left_x", "left_y", "right_x", "right_y"):
            # Joystick axes use joystick_settings
            return f"axis_sensitivity.{axis_id}.{setting}"
        else:
            # Triggers/throttle/rudder use their own keys
            return f"axis_sensitivity.{axis_id}.{setting}"
    
    def _get_default_for_axis(self, axis_id: str) -> dict[str, float]:
        """Get default values for an axis."""
        if axis_id in ("left_x", "left_y", "right_x", "right_y"):
            return self.DEFAULTS["joystick"].copy()
        else:
            return self.DEFAULTS["trigger"].copy()
    
    def _load_all_settings(self) -> None:
        """Load settings for all axes from config."""
        for axis_id, _ in self.axes:
            defaults = self._get_default_for_axis(axis_id)
            
            # Try to load from new per-axis config first
            sens = self.config.get(f"axis_sensitivity.{axis_id}.sensitivity")
            dead = self.config.get(f"axis_sensitivity.{axis_id}.deadzone")
            ext = self.config.get(f"axis_sensitivity.{axis_id}.extremity_deadzone")
            
            # Fall back to legacy settings if per-axis not set
            if sens is None:
                if axis_id in ("left_x", "left_y", "right_x", "right_y"):
                    sens = self.config.get("joystick_settings.sensitivity", defaults["sensitivity"])
                else:
                    sens = self.config.get("rudder_settings.sensitivity", defaults["sensitivity"])
            
            if dead is None:
                if axis_id in ("left_x", "left_y", "right_x", "right_y"):
                    dead = self.config.get("joystick_settings.deadzone", defaults["deadzone"])
                else:
                    dead = self.config.get("rudder_settings.deadzone", defaults["deadzone"])
            
            if ext is None:
                if axis_id in ("left_x", "left_y", "right_x", "right_y"):
                    ext = self.config.get("joystick_settings.extremity_deadzone", defaults["extremity_deadzone"])
                else:
                    ext = self.config.get("rudder_settings.extremity_deadzone", defaults["extremity_deadzone"])
            
            self.axis_settings[axis_id] = {
                "sensitivity": float(sens),
                "deadzone": float(dead),
                "extremity_deadzone": float(ext),
            }
    
    def _load_all_mappings(self) -> None:
        """Load VJoy axis mappings for all axes from config."""
        for axis_id, _ in self.axes:
            mapping = self.config.get(f"axis_mapping.{axis_id}", "none")
            self.axis_mappings[axis_id] = str(mapping).lower()
    
    def _on_axis_selected(self, row: int) -> None:
        """Handle axis selection change."""
        if row < 0:
            return
        
        item = self.axis_list.item(row)
        axis_id = item.data(Qt.UserRole)
        axis_name = item.text()
        
        self.axis_header.setText(axis_name)
        
        # Load settings for this axis
        settings = self.axis_settings.get(axis_id, self._get_default_for_axis(axis_id))
        
        # Block signals while updating controls
        self.sens_slider._slider.blockSignals(True)
        self.dead_slider._slider.blockSignals(True)
        self.ext_slider._slider.blockSignals(True)
        self.mapping_combo.blockSignals(True)
        
        self.sens_slider.setValue(int(settings["sensitivity"]))
        self.dead_slider.setValue(int(settings["deadzone"]))
        self.ext_slider.setValue(int(settings["extremity_deadzone"]))
        
        # Load VJoy mapping
        current_mapping = self.axis_mappings.get(axis_id, "none").lower()
        try:
            idx = self.vjoy_axes.index(current_mapping)
            self.mapping_combo.setCurrentIndex(idx)
        except ValueError:
            self.mapping_combo.setCurrentIndex(0)
        
        self.sens_slider._slider.blockSignals(False)
        self.dead_slider._slider.blockSignals(False)
        self.ext_slider._slider.blockSignals(False)
        self.mapping_combo.blockSignals(False)
        
        # Update preview
        self.preview.setParams(
            settings["sensitivity"],
            settings["deadzone"],
            settings["extremity_deadzone"],
        )
        
        # Reset copy combo
        self.copy_combo.setCurrentIndex(0)
    
    def _on_setting_changed(self, _: int = 0) -> None:
        """Handle slider value change."""
        item = self.axis_list.currentItem()
        if not item:
            return
        
        axis_id = item.data(Qt.UserRole)
        
        # Update stored settings
        self.axis_settings[axis_id] = {
            "sensitivity": float(self.sens_slider.value()),
            "deadzone": float(self.dead_slider.value()),
            "extremity_deadzone": float(self.ext_slider.value()),
        }
        
        # Update preview
        self.preview.setParams(
            self.sens_slider.value(),
            self.dead_slider.value(),
            self.ext_slider.value(),
        )
    
    def _on_copy_from(self, index: int) -> None:
        """Copy settings from another axis."""
        if index <= 0:
            return
        
        source_axis_id = self.copy_combo.itemData(index)
        if not source_axis_id:
            return
        
        current_item = self.axis_list.currentItem()
        if not current_item:
            return
        
        current_axis_id = current_item.data(Qt.UserRole)
        if source_axis_id == current_axis_id:
            self.copy_combo.setCurrentIndex(0)
            return
        
        # Copy settings
        source_settings = self.axis_settings.get(source_axis_id, self._get_default_for_axis(source_axis_id))
        
        self.sens_slider.setValue(int(source_settings["sensitivity"]))
        self.dead_slider.setValue(int(source_settings["deadzone"]))
        self.ext_slider.setValue(int(source_settings["extremity_deadzone"]))
        
        # Reset combo
        self.copy_combo.setCurrentIndex(0)
    
    def _on_mapping_changed(self, index: int) -> None:
        """Handle VJoy axis mapping change."""
        item = self.axis_list.currentItem()
        if not item:
            return
        
        axis_id = item.data(Qt.UserRole)
        self.axis_mappings[axis_id] = self.vjoy_axes[index]
    
    def _reset_current(self) -> None:
        """Reset current axis to defaults."""
        item = self.axis_list.currentItem()
        if not item:
            return
        
        axis_id = item.data(Qt.UserRole)
        defaults = self._get_default_for_axis(axis_id)
        
        self.sens_slider.setValue(int(defaults["sensitivity"]))
        self.dead_slider.setValue(int(defaults["deadzone"]))
        self.ext_slider.setValue(int(defaults["extremity_deadzone"]))
    
    def _save_and_close(self) -> None:
        """Save all axis settings and mappings."""
        # Save per-axis settings
        for axis_id, settings in self.axis_settings.items():
            self.config.set(f"axis_sensitivity.{axis_id}.sensitivity", settings["sensitivity"])
            self.config.set(f"axis_sensitivity.{axis_id}.deadzone", settings["deadzone"])
            self.config.set(f"axis_sensitivity.{axis_id}.extremity_deadzone", settings["extremity_deadzone"])
        
        # Save axis mappings
        for axis_id, vjoy_axis in self.axis_mappings.items():
            self.config.set(f"axis_mapping.{axis_id}", vjoy_axis)
        
        # Also update legacy settings for backward compatibility
        # Use left_x as the reference for joystick_settings
        if "left_x" in self.axis_settings:
            js = self.axis_settings["left_x"]
            self.config.set("joystick_settings.sensitivity", js["sensitivity"])
            self.config.set("joystick_settings.deadzone", js["deadzone"])
            self.config.set("joystick_settings.extremity_deadzone", js["extremity_deadzone"])
        
        # Use throttle/rudder or triggers for rudder_settings
        for axis_id in ("throttle", "rudder", "left_trigger", "right_trigger"):
            if axis_id in self.axis_settings:
                rs = self.axis_settings[axis_id]
                self.config.set("rudder_settings.sensitivity", rs["sensitivity"])
                self.config.set("rudder_settings.deadzone", rs["deadzone"])
                self.config.set("rudder_settings.extremity_deadzone", rs["extremity_deadzone"])
                break
        
        self.config.save_config()
        self.config.save_current_profile()
        self.accept()


class JoystickSettingsQt(QDialog):
    """Joystick sensitivity / deadzone / extremity-deadzone editor.

    Provides three percent sliders backed by ``joystick_settings.*`` config
    keys and a live :class:`_CurvePreview` so the user can see how the
    response curve changes before clicking Apply. Persists on Apply via
    :meth:`ControllerConfig.save_config`.
    """

    def __init__(self, config: ControllerConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Joystick Settings")
        self.config = config

        self.sensitivity = int(self.config.get("joystick_settings.sensitivity", 50.0))
        self.deadzone = int(self.config.get("joystick_settings.deadzone", 10.0))
        self.extremity = int(self.config.get("joystick_settings.extremity_deadzone", 5.0))

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.sens_slider = _LabeledSlider("Sensitivity", 0, 100, self.sensitivity, self)
        self.dead_slider = _LabeledSlider("Deadzone", 0, 100, self.deadzone, self)
        self.ext_slider = _LabeledSlider("Extremity Deadzone", 0, 100, self.extremity, self)
        layout.addWidget(self.sens_slider)
        layout.addWidget(self.dead_slider)
        layout.addWidget(self.ext_slider)

        # Curve preview
        self.preview = _CurvePreview(self)
        self.preview.setParams(self.sensitivity, self.deadzone, self.extremity)
        layout.addWidget(self.preview)

        # Live updates
        def _update_preview(_: int) -> None:
            self.preview.setParams(
                float(self.sens_slider.value()),
                float(self.dead_slider.value()),
                float(self.ext_slider.value()),
            )
        self.sens_slider._slider.valueChanged.connect(_update_preview)
        self.dead_slider._slider.valueChanged.connect(_update_preview)
        self.ext_slider._slider.valueChanged.connect(_update_preview)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults, parent=self)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        btns.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._reset)
        layout.addWidget(btns)

    def _reset(self) -> None:
        self.sens_slider.setValue(50)
        self.dead_slider.setValue(10)
        self.ext_slider.setValue(5)

    def _accept(self) -> None:
        self.config.set("joystick_settings.sensitivity", float(self.sens_slider.value()))
        self.config.set("joystick_settings.deadzone", float(self.dead_slider.value()))
        self.config.set("joystick_settings.extremity_deadzone", float(self.ext_slider.value()))
        self.config.save_config()
        self.config.save_current_profile()  # Persist to current profile
        self.accept()


# --- New: Axis mapping dialog for Qt ---
class AxisMappingQt(QDialog):
    """Qt dialog to map UI joystick axes to VJoy axes (replaces pygame axis_config_dialog for Qt shell)."""
    def __init__(self, config: ControllerConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Axis Mapping")
        self.config = config

        # Axis lists
        # Only offer axes supported by VJoyInterface.update_axis
        self.vjoy_axes = [
            "none", "x", "y", "z", "rx", "ry", "rz"
        ]
        
        # Get profile-aware axis labels
        self.ui_axes = self._get_profile_axes()

        # Layout
        layout = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)

        header1 = QLabel("UI Axis")
        header2 = QLabel("VJoy Axis")
        header1.setStyleSheet("font-weight: 600;")
        header2.setStyleSheet("font-weight: 600;")
        grid.addWidget(header1, 0, 0)
        grid.addWidget(header2, 0, 1)

        # Build rows
        self.combos: dict[str, QComboBox] = {}
        for row, (key, label) in enumerate(self.ui_axes, start=1):
            grid.addWidget(QLabel(label), row, 0)
            cb = QComboBox(self)
            cb.addItems(self.vjoy_axes)
            current = str(self.config.get(f"axis_mapping.{key}", "none"))
            try:
                cb.setCurrentIndex(self.vjoy_axes.index(current))
            except ValueError:
                cb.setCurrentIndex(0)
            self.combos[key] = cb
            grid.addWidget(cb, row, 1)

        layout.addLayout(grid)

        # Buttons (Save/Close)
        btns = QDialogButtonBox(QDialogButtonBox.Close | QDialogButtonBox.Save, parent=self)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self._save)
        layout.addWidget(btns)

    def _get_profile_axes(self) -> list[tuple[str, str]]:
        """Get axis list with profile-appropriate labels."""
        layout_type = self.config.get_layout_type()
        
        # Common axes for all profiles
        axes = [
            ("left_x", "Left Stick X"),
            ("left_y", "Left Stick Y"),
            ("right_x", "Right Stick X"),
            ("right_y", "Right Stick Y"),
        ]
        
        # Profile-specific axes
        if layout_type == "adaptive":
            axes.extend([
                ("left_trigger", "Left Trigger (LT)"),
                ("right_trigger", "Right Trigger (RT)"),
            ])
        else:  # flight_sim or xbox
            axes.extend([
                ("throttle", "Throttle"),
                ("rudder", "Rudder"),
            ])
        
        return axes

    def _save(self) -> None:
        for key, cb in self.combos.items():
            self.config.set(f"axis_mapping.{key}", cb.currentText())
        self.config.save_config()
        self.config.save_current_profile()  # Persist to current profile
        self.accept()


class SliderSettingsQt(QDialog):
    """Settings dialog for slider/trigger sensitivity. Title adapts to current profile."""
    def __init__(self, config: ControllerConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        
        # Profile-aware title
        layout_type = self.config.get_layout_type()
        if layout_type == "adaptive":
            self.setWindowTitle("Trigger Sensitivity")
        else:
            self.setWindowTitle("Throttle/Rudder Settings")

        self.sensitivity = int(self.config.get("rudder_settings.sensitivity", 50.0))
        self.deadzone = int(self.config.get("rudder_settings.deadzone", 10.0))
        self.extremity = int(self.config.get("rudder_settings.extremity_deadzone", 5.0))

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.sens_slider = _LabeledSlider("Sensitivity", 0, 100, self.sensitivity, self)
        self.dead_slider = _LabeledSlider("Deadzone", 0, 100, self.deadzone, self)
        self.ext_slider = _LabeledSlider("Extremity Deadzone", 0, 100, self.extremity, self)
        layout.addWidget(self.sens_slider)
        layout.addWidget(self.dead_slider)
        layout.addWidget(self.ext_slider)

        # Curve preview
        self.preview = _CurvePreview(self)
        self.preview.setParams(self.sensitivity, self.deadzone, self.extremity)
        layout.addWidget(self.preview)

        # Live updates
        def _update_preview(_: int) -> None:
            self.preview.setParams(
                float(self.sens_slider.value()),
                float(self.dead_slider.value()),
                float(self.ext_slider.value()),
            )
        self.sens_slider._slider.valueChanged.connect(_update_preview)
        self.dead_slider._slider.valueChanged.connect(_update_preview)
        self.ext_slider._slider.valueChanged.connect(_update_preview)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults, parent=self)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        btns.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._reset)
        layout.addWidget(btns)

    def _reset(self) -> None:
        self.sens_slider.setValue(50)
        self.dead_slider.setValue(10)
        self.ext_slider.setValue(5)

    def _accept(self) -> None:
        self.config.set("rudder_settings.sensitivity", float(self.sens_slider.value()))
        self.config.set("rudder_settings.deadzone", float(self.dead_slider.value()))
        self.config.set("rudder_settings.extremity_deadzone", float(self.ext_slider.value()))
        self.config.save_config()
        self.config.save_current_profile()  # Persist to current profile
        self.accept()


class ButtonSettingsQt(QDialog):
    """Button settings dialog showing VJoy mapping and controller labels."""
    
    def __init__(self, config: ControllerConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Button Settings")
        self.setMinimumSize(500, 450)
        self.config = config
        
        # Get layout type for profile-aware button names
        self.layout_type = self.config.get_layout_type()
        
        # Apply dark theme
        self.setStyleSheet("""
            QDialog { background-color: #1e1e1e; }
            QLabel { color: #e0e0e0; }
            QCheckBox { color: #e0e0e0; }
            QCheckBox::indicator {
                width: 20px;
                height: 20px;
                border-radius: 4px;
                border: 2px solid #555;
                background-color: #2a2a2a;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QCheckBox::indicator:hover {
                border-color: #888;
            }
            QPushButton {
                background-color: #3a3a3a;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 8px 16px;
                color: #e0e0e0;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
            }
            QPushButton#primaryBtn {
                background-color: #0078d4;
                border: none;
            }
            QPushButton#primaryBtn:hover {
                background-color: #1a8cff;
            }
            QScrollArea {
                border: none;
                background-color: transparent;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)
        
        # Title
        title = QLabel("Button Mode Configuration")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        layout.addWidget(title)
        
        # Description
        desc = QLabel("Toggle mode: button stays pressed until clicked again.\nMomentary mode: button only active while held down.")
        desc.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(desc)
        
        # Grid layout for buttons inside a scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; }
            QScrollBar:vertical {
                background: #2a2a2a;
                width: 10px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #555;
                border-radius: 5px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #777;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(10)
        
        # Headers
        headers = ["VJoy #", "Controller", "Label", "Toggle Mode"]
        header_style = "font-weight: 600; color: #aaa; font-size: 11px;"
        for col, header in enumerate(headers):
            lbl = QLabel(header)
            lbl.setStyleSheet(header_style)
            grid.addWidget(lbl, 0, col)
        
        # Get button labels from profile
        button_info = self._get_button_info()
        
        self.checks: dict[int, QCheckBox] = {}
        for row, (vjoy_num, controller_btn, label) in enumerate(button_info, start=1):
            # VJoy button number
            vjoy_lbl = QLabel(f"Button {vjoy_num}")
            vjoy_lbl.setStyleSheet("color: #888;")
            grid.addWidget(vjoy_lbl, row, 0)
            
            # Controller button name (A, B, X, Y, etc.)
            ctrl_lbl = QLabel(controller_btn)
            ctrl_lbl.setStyleSheet("font-weight: 600; color: #fff;")
            grid.addWidget(ctrl_lbl, row, 1)
            
            # Custom label from profile
            label_lbl = QLabel(label)
            label_lbl.setStyleSheet("color: #aaa;")
            grid.addWidget(label_lbl, row, 2)
            
            # Toggle checkbox
            chk = QCheckBox()
            chk.setChecked(bool(self.config.get(f"buttons.button_{vjoy_num}.toggle_mode", False)))
            self.checks[vjoy_num] = chk
            grid.addWidget(chk, row, 3)
        
        scroll.setWidget(grid_widget)
        layout.addWidget(scroll, 1)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        save_btn = QPushButton("Save")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)
        
        layout.addLayout(btn_layout)
    
    def _get_button_info(self) -> list[tuple[int, str, str]]:
        """Get button info: (vjoy_num, controller_button, label) based on profile."""
        if self.layout_type == "adaptive":
            # Xbox-style button mapping for adaptive with Greek symbols
            return [
                (1, "A", self.config.get("buttons.button_1.label", "A")),
                (2, "B", self.config.get("buttons.button_2.label", "B")),
                (3, "X", self.config.get("buttons.button_3.label", "X")),
                (4, "Y", self.config.get("buttons.button_4.label", "Y")),
                (5, "LB", self.config.get("buttons.button_5.label", "LB")),
                (6, "D-Up", self.config.get("buttons.button_6.label", "DPad Up")),
                (7, "D-Down", self.config.get("buttons.button_7.label", "DPad Down")),
                (8, "D-Left", self.config.get("buttons.button_8.label", "DPad Left")),
                (9, "D-Right", self.config.get("buttons.button_9.label", "DPad Right")),
                (10, "RB", self.config.get("buttons.button_10.label", "RB")),
                (11, "L3", self.config.get("buttons.button_11.label", "LS")),
                (12, "R3", self.config.get("buttons.button_12.label", "RS")),
                (13, "α (View)", self.config.get("buttons.button_13.label", "View/Back")),
                (14, "Unused", self.config.get("buttons.button_14.label", "Unused")),
                (15, "Ω (Guide)", self.config.get("buttons.button_15.label", "Guide")),
                (16, "β (Menu)", self.config.get("buttons.button_16.label", "Menu/Start")),
                (17, "⬚ (Share)", self.config.get("buttons.button_17.label", "Share")),
            ]
        else:
            # Flight sim style
            return [
                (1, "Btn 1", self.config.get("buttons.button_1.label", "Button 1")),
                (2, "Btn 2", self.config.get("buttons.button_2.label", "Button 2")),
                (3, "Btn 3", self.config.get("buttons.button_3.label", "Button 3")),
                (4, "Btn 4", self.config.get("buttons.button_4.label", "Button 4")),
                (5, "Btn 5", self.config.get("buttons.button_5.label", "Button 5")),
                (6, "Btn 6", self.config.get("buttons.button_6.label", "Button 6")),
                (7, "Btn 7", self.config.get("buttons.button_7.label", "Button 7")),
                (8, "Btn 8", self.config.get("buttons.button_8.label", "Button 8")),
                (9, "ARM", self.config.get("buttons.button_9.label", "ARM")),
                (10, "RTH", self.config.get("buttons.button_10.label", "RTH")),
            ]

    def _save(self) -> None:
        for i, chk in self.checks.items():
            self.config.set(f"buttons.button_{i}.toggle_mode", bool(chk.isChecked()))
        self.config.save_config()
        self.config.save_current_profile()
        self.accept()


# Backward compatibility alias
RudderSettingsQt = SliderSettingsQt
