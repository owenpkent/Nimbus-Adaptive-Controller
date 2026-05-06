"""
QML <-> Python bridge for Nimbus Adaptive Controller.

This module exposes :class:`ControllerBridge`, the single ``QObject`` that
backs the QML user interface. The bridge fans QML interactions out to the
underlying subsystems:

* :class:`~src.vjoy_interface.VJoyInterface` — DirectInput virtual joystick
* :class:`~src.vigem_interface.ViGEmInterface` — XInput Xbox 360 emulation
* :mod:`~src.borderless` — borderless windowed mode + ClipCursor release
* :mod:`~src.mouse_hider` — controller-mode keep-alive (game voluntarily
  releases the mouse)
* :mod:`~src.window_utils` — ``WS_EX_NOACTIVATE`` "Game Focus" mode
* :class:`~src.config.ControllerConfig` — persistent settings + profiles

Bridge responsibilities
-----------------------
* Translate QML method calls (``Slot``\\ s) into back-end operations.
* Apply per-axis sensitivity curves and optional smoothing before forwarding
  values to the active controller interface.
* Persist UI state (scale factor, debug borders, recent profiles, etc.) via
  :class:`ControllerConfig`.
* Emit Qt signals (``profileChanged``, ``vjoyConnectionChanged`` ...) so QML
  can react to back-end state changes.

Threading model
---------------
The bridge runs on the Qt main thread. A single :class:`QTimer`
(``_smooth_timer``) ticks at the configured vJoy update rate and applies
exponential smoothing to axes that are routed through vJoy. ViGEm bypasses
the smoothing tick and is updated directly because it has its own internal
update loop. Cursor-release polling and controller-mode keep-alive run on
dedicated worker threads owned by their respective modules.

The bridge is wired into QML in :mod:`src.qt_qml_app` as the context
property ``controller``.
"""
from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import QObject, Slot, Signal, Property, QTimer
from PySide6.QtGui import QCursor, QWindow

from .config import ControllerConfig
from .vjoy_interface import VJoyInterface
from .qt_dialogs import AxisMappingQt, JoystickSettingsQt, ButtonSettingsQt, SliderSettingsQt, AxisSettingsQt

# Try to import ViGEm for Xbox controller emulation (preferred for modern games)
try:
    from .vigem_interface import ViGEmInterface, VIGEM_AVAILABLE
except ImportError:
    VIGEM_AVAILABLE = False
    ViGEmInterface = None

# Import window utilities for game focus mode (Windows only)
try:
    from .window_utils import (
        make_window_no_activate,
        remove_window_no_activate,
        get_qt_window_handle,
        is_no_activate_enabled,
        save_foreground_window,
        on_window_activated,
    )
    WINDOW_UTILS_AVAILABLE = True
except Exception:
    WINDOW_UTILS_AVAILABLE = False

# Import borderless gaming module (Windows only)
try:
    from . import borderless as _borderless
    BORDERLESS_AVAILABLE = True
except Exception:
    BORDERLESS_AVAILABLE = False
    _borderless = None

# Import controller mode enforcement module (Windows only)
try:
    from . import mouse_hider as _mouse_hider
    MOUSE_HIDER_AVAILABLE = True
except Exception:
    MOUSE_HIDER_AVAILABLE = False
    _mouse_hider = None


class ControllerBridge(QObject):
    """QObject bridge between the QML UI and the Python back end.

    Exposed to QML as the context property ``controller`` (see
    :func:`src.qt_qml_app.main`). All ``@Slot``-decorated methods are callable
    from QML; all signals declared on the class are observable from QML.

    The bridge owns the active controller interface — either a
    :class:`~src.vjoy_interface.VJoyInterface` (DirectInput) or a
    :class:`~src.vigem_interface.ViGEmInterface` (XInput / Xbox 360
    emulation), selected based on the current profile's ``layout_type`` and
    the ``controller.prefer_vigem`` config flag. ViGEm is preferred for
    ``xbox`` / ``adaptive`` / ``custom`` profiles because it works with
    XInput-only games (e.g. *No Man's Sky*).

    Args:
        config: Application configuration object.
        parent: Optional Qt parent for ownership.

    Signals:
        scaleFactorChanged(float): UI scale factor changed.
        vjoyConnectionChanged(bool): Active controller connect/disconnect.
        debugBordersChanged(bool): Debug border overlay toggled.
        buttonsVersionChanged(int): Bumped when button modes change so QML
            re-evaluates its bindings.
        profileChanged(str): Active profile ID changed.
        layoutTypeChanged(str): Active profile's layout type changed.
        profilesListChanged(): Profile inventory changed (add/delete).
        profileSaved(bool): Save attempt completed (True == success).
        noFocusModeChanged(bool): Game Focus mode toggled.
        cursorReleaseChanged(bool): ClipCursor release polling toggled.
        borderlessModeChanged(int, bool): Game window borderless state
            changed; ``(hwnd, is_borderless)``.
        controllerModeChanged(bool): Controller-mode enforcement toggled.
        outputModeChanged(str): Output device switched (``'vjoy'`` or
            ``'vigem'``).
        recentProfilesChanged(): Recent-profiles list updated.
    """

    scaleFactorChanged = Signal(float)
    vjoyConnectionChanged = Signal(bool)
    debugBordersChanged = Signal(bool)
    buttonsVersionChanged = Signal(int)
    profileChanged = Signal(str)  # Emits new profile ID
    layoutTypeChanged = Signal(str)  # Emits new layout type
    profilesListChanged = Signal()  # Emits when profile list changes (add/delete)
    profileSaved = Signal(bool)  # Emits save result
    noFocusModeChanged = Signal(bool)  # Emits when no-focus mode changes
    cursorReleaseChanged = Signal(bool)  # Emits when cursor release polling starts/stops
    borderlessModeChanged = Signal(int, bool)  # Emits (hwnd, is_borderless)
    controllerModeChanged = Signal(bool)  # Emits when controller mode enforcement starts/stops
    outputModeChanged = Signal(str)  # Emits "vjoy" or "vigem" when output device changes
    recentProfilesChanged = Signal()  # Emits when the recently-used profile list changes

    def __init__(self, config: ControllerConfig, parent: Optional[QObject] = None) -> None:
        """Initialize the bridge and probe for available controller back ends.

        Selects ViGEm or vJoy based on the current profile's layout type and
        the ``controller.prefer_vigem`` config flag, starts the smoothing
        timer at the configured vJoy update rate, and emits the initial
        ``vjoyConnectionChanged`` signal.

        Args:
            config: Application configuration object.
            parent: Optional Qt parent for ownership.
        """
        super().__init__(parent)
        self._config = config
        self._window: Optional[QWindow] = None
        self._no_focus_mode = False
        self._cursor_release_active = False
        self._borderless_game_hwnd: int = 0
        self._controller_mode_active = False
        # Load recent profiles from persistent config (most-recent first)
        self._recent_profiles: list = list(self._config.get("ui.recent_profiles", []))
        
        # Determine which controller interface to use based on profile layout type
        # ViGEm (Xbox emulation) is preferred for xbox/adaptive profiles as it works with XInput games
        # vJoy is used for flight_sim profiles or as fallback
        self._use_vigem = False
        self._vigem: Optional[ViGEmInterface] = None
        self._vjoy: Optional[VJoyInterface] = None
        
        self._init_controller_interface()
        
        self._scale = float(self._config.get("ui.scale_factor", 1.0))
        self._debug_borders = bool(self._config.get("ui.debug_borders", False))
        self._buttons_version = 0
        # Axis smoothing state: per-axis current and target in [-1,1]
        self._axis_state: dict[str, dict[str, float]] = {}
        # Timer to apply smoothing at vJoy update rate
        self._smooth_timer = QTimer(self)
        try:
            hz = int(self._config.get("vjoy.update_rate", 60))
            hz = max(10, min(240, hz))
            self._smooth_timer.setInterval(max(1, int(1000 / hz)))
        except Exception:
            self._smooth_timer.setInterval(16)
        self._smooth_timer.timeout.connect(self._smoothing_tick)
        self._smooth_timer.start()
        # Emit initial status
        self.vjoyConnectionChanged.emit(self._is_controller_connected())
    
    def _init_controller_interface(self) -> None:
        """Initialize the appropriate controller interface based on profile type."""
        layout_type = self._config.get_layout_type()
        use_vigem_config = self._config.get("controller.prefer_vigem", True)
        
        # Use ViGEm for Xbox/Adaptive/Custom profiles if available (works with XInput games like No Man's Sky)
        if layout_type in ("xbox", "adaptive", "custom") and VIGEM_AVAILABLE and use_vigem_config:
            print(f"Profile '{layout_type}' detected - using ViGEm Xbox controller emulation")
            print("This provides XInput compatibility for games like No Man's Sky")
            if self._vigem is None:
                self._vigem = ViGEmInterface(self._config)
            self._use_vigem = True
            # Also init vJoy as fallback
            if self._vjoy is None:
                self._vjoy = VJoyInterface(self._config)
        else:
            # Use vJoy for flight sim profiles or if ViGEm unavailable
            if layout_type in ("xbox", "adaptive") and not VIGEM_AVAILABLE:
                print(f"Warning: ViGEm not available for {layout_type} profile")
                print("Install with: pip install vgamepad")
                print("Falling back to vJoy (may not work with XInput-only games)")
            if self._vjoy is None:
                self._vjoy = VJoyInterface(self._config)
            self._use_vigem = False
    
    def _is_controller_connected(self) -> bool:
        """Check if the active controller interface is connected."""
        if self._use_vigem and self._vigem:
            return self._vigem.is_connected
        elif self._vjoy:
            return self._vjoy.is_connected
        return False
    
    def _get_active_interface(self):
        """Get the currently active controller interface."""
        if self._use_vigem and self._vigem:
            return self._vigem
        return self._vjoy

    # ----- Scale factor property -----
    def _get_scale(self) -> float:
        return float(self._scale)

    def _set_scale(self, value: float) -> None:
        value = float(value)
        if value <= 0:
            return
        if abs(value - self._scale) < 1e-6:
            return
        self._scale = value
        self._config.set_scale_factor(value)
        self._config.save_config()
        self.scaleFactorChanged.emit(self._scale)

    scaleFactor = Property(float, _get_scale, _set_scale, notify=scaleFactorChanged)

    # ----- Debug borders property -----
    def _get_debug(self) -> bool:
        return bool(self._debug_borders)

    def _set_debug(self, value: bool) -> None:
        value = bool(value)
        if value == self._debug_borders:
            return
        self._debug_borders = value
        self._config.set("ui.debug_borders", self._debug_borders)
        self._config.save_config()
        self.debugBordersChanged.emit(self._debug_borders)

    debugBorders = Property(bool, _get_debug, _set_debug, notify=debugBordersChanged)

    # ----- Buttons version property (to refresh QML toggle states) -----
    def _get_buttons_version(self) -> int:
        return int(self._buttons_version)

    buttonsVersion = Property(int, _get_buttons_version, notify=buttonsVersionChanged)

    # ----- No-focus mode property (prevents stealing focus from games) -----
    def _get_no_focus_mode(self) -> bool:
        return bool(self._no_focus_mode)
    
    def _set_no_focus_mode(self, enabled: bool) -> None:
        if self._no_focus_mode == enabled:
            return
        
        if not WINDOW_UTILS_AVAILABLE:
            print("No-focus mode: window_utils not available")
            return
        
        if self._window is None:
            print("No-focus mode: window not set yet")
            return
        
        hwnd = get_qt_window_handle(self._window)
        
        if enabled:
            success = make_window_no_activate(hwnd)
            if success:
                self._no_focus_mode = True
                self._config.set("ui.no_focus_mode", True)
                self._config.save_config()
                self.noFocusModeChanged.emit(True)
                print("No-focus mode ENABLED - window will not steal focus from games")
        else:
            success = remove_window_no_activate(hwnd)
            if success:
                self._no_focus_mode = False
                self._config.set("ui.no_focus_mode", False)
                self._config.save_config()
                self.noFocusModeChanged.emit(False)
                print("No-focus mode DISABLED - normal window behavior restored")
    
    noFocusMode = Property(bool, _get_no_focus_mode, _set_no_focus_mode, notify=noFocusModeChanged)
    
    @Slot(QWindow)
    def setWindow(self, window: QWindow) -> None:  # noqa: N802
        """Set the window reference for no-focus mode. Called from QML after window is ready."""
        self._window = window
        # Only restore no-focus mode if user explicitly enabled it (not from game mode auto-enable)
        if self._config.get("ui.no_focus_mode_user", False):
            self._set_no_focus_mode(True)
    
    @Slot(result=bool)
    def isNoFocusModeAvailable(self) -> bool:  # noqa: N802
        """Check if no-focus mode is available on this platform."""
        return WINDOW_UTILS_AVAILABLE and sys.platform == "win32"
    
    @Slot()
    def clipCursorToWindow(self) -> None:  # noqa: N802
        """Confine the mouse cursor to the application window bounds (Windows only)."""
        if sys.platform != "win32" or not self._window:
            return
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = int(self._window.winId())
            rect = wintypes.RECT()
            ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
            # Convert client coords to screen coords
            pt_tl = wintypes.POINT(rect.left, rect.top)
            pt_br = wintypes.POINT(rect.right, rect.bottom)
            ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt_tl))
            ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt_br))
            clip_rect = wintypes.RECT(pt_tl.x, pt_tl.y, pt_br.x, pt_br.y)
            ctypes.windll.user32.ClipCursor(ctypes.byref(clip_rect))
        except Exception as e:
            print(f"ClipCursor failed: {e}")

    @Slot(int, int, int, int)
    def clipCursorToRect(self, screen_x: int, screen_y: int, width: int, height: int) -> None:  # noqa: N802
        """Confine the mouse cursor to a specific screen rectangle (Windows only).

        Used to lock cursor to the joystick widget area so a wheelchair joystick
        maps 1:1 to the virtual joystick.
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            clip_rect = wintypes.RECT(screen_x, screen_y, screen_x + width, screen_y + height)
            ctypes.windll.user32.ClipCursor(ctypes.byref(clip_rect))
        except Exception as e:
            print(f"ClipCursorToRect failed: {e}")

    @Slot(int, int)
    def setCursorPos(self, screen_x: int, screen_y: int) -> None:  # noqa: N802
        """Move the mouse cursor to a specific screen position.

        Uses Qt's QCursor.setPos() which handles DPI scaling correctly,
        unlike Windows SetCursorPos which expects physical pixels.
        """
        try:
            from PySide6.QtCore import QPoint
            QCursor.setPos(QPoint(screen_x, screen_y))
        except Exception as e:
            print(f"setCursorPos failed: {e}")

    @Slot()
    def unclipCursor(self) -> None:  # noqa: N802
        """Release the mouse cursor confinement."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            ctypes.windll.user32.ClipCursor(None)
        except Exception as e:
            print(f"UnclipCursor failed: {e}")

    @Slot()
    def onMousePressed(self) -> None:  # noqa: N802
        """Called from QML when mouse is pressed on any interactive element.
        
        In game focus mode, this saves the current foreground window
        so we can restore focus to it later.
        """
        if WINDOW_UTILS_AVAILABLE and self._no_focus_mode:
            save_foreground_window()
    
    @Slot()
    def onMouseReleased(self) -> None:  # noqa: N802
        """Called from QML when mouse is released.
        
        In game focus mode, this restores focus to the previous foreground window.
        """
        if WINDOW_UTILS_AVAILABLE and self._no_focus_mode:
            on_window_activated()

    # ----- Slots callable from QML -----
    @Slot(str, float)
    def setAxis(self, axis: str, value: float) -> None:  # noqa: N802 (Qt slot naming)
        """
        Set an axis value coming from QML (-1.0 .. 1.0 recommended).
        """
        try:
            iface = self._get_active_interface()
            if iface:
                iface.update_axis(axis.lower(), float(value))
        except Exception:
            pass

    @Slot(int, bool)
    def setButton(self, button_id: int, pressed: bool) -> None:  # noqa: N802
        try:
            iface = self._get_active_interface()
            if iface:
                iface.set_button(int(button_id), bool(pressed))
        except Exception:
            pass

    @Slot(result=str)
    def getControllerStateText(self) -> str:  # noqa: N802
        """Return a one-line controller state summary for the live monitor bar."""
        try:
            iface = self._get_active_interface()
            if not iface or not iface.is_connected:
                return "Controller: not connected"
            cv = iface.current_values
            if self._use_vigem:
                lx = cv.get('left_x', 0.0)
                ly = cv.get('left_y', 0.0)
                rx = cv.get('right_x', 0.0)
                ry = cv.get('right_y', 0.0)
                lt = cv.get('left_trigger', 0.0)
                rt = cv.get('right_trigger', 0.0)
                btns = [str(b) for b, p in getattr(iface, 'button_states', {}).items() if p]
                btns_str = "Btn:" + ",".join(btns) if btns else ""
                parts = [
                    f"Xbox360",
                    f"LX:{lx:+.2f}",
                    f"LY:{ly:+.2f}",
                    f"RX:{rx:+.2f}",
                    f"RY:{ry:+.2f}",
                ]
                if lt > 0.01:
                    parts.append(f"LT:{lt:.2f}")
                if rt > 0.01:
                    parts.append(f"RT:{rt:.2f}")
                if btns_str:
                    parts.append(btns_str)
                return "  ".join(parts)
            else:
                parts = ["vJoy"]
                for k, v in cv.items():
                    if abs(float(v)) > 0.01:
                        parts.append(f"{k}:{float(v):+.2f}")
                return "  ".join(parts) if len(parts) > 1 else "vJoy  (all axes at zero)"
        except Exception as e:
            return f"Controller error: {e}"

    @Slot(float)
    def setScaleFactor(self, value: float) -> None:  # noqa: N802
        self._set_scale(value)

    @Slot(int, result=int)
    def scaled(self, base: int) -> int:  # noqa: N802
        """Return a config-scaled integer for a given base pixel value."""
        try:
            return int(self._config.get_scaled_int(int(base)))
        except Exception:
            return int(base)

    # High-level control slots that apply curves and mapping, mirroring widget UI behavior
    @Slot(float, float)
    def setLeftStick(self, x: float, y: float) -> None:  # noqa: N802
        try:
            px = self._config.apply_sensitivity_curve(float(x), 'left', 'x')
            py = self._config.apply_sensitivity_curve(float(y), 'left', 'y')
            if self._is_controller_connected():
                # For ViGEm, use direct stick control
                if self._use_vigem and self._vigem:
                    self._vigem.set_left_stick(px, py)
                else:
                    ax = str(self._config.get("axis_mapping.left_x", "x"))
                    ay = str(self._config.get("axis_mapping.left_y", "y"))
                    if ax != "none":
                        self._set_axis_target(ax, px)
                    if ay != "none":
                        self._set_axis_target(ay, py)
        except Exception:
            pass

    @Slot(float, float)
    def setRightStick(self, x: float, y: float) -> None:  # noqa: N802
        try:
            px = self._config.apply_sensitivity_curve(float(x), 'right', 'x')
            py = self._config.apply_sensitivity_curve(float(y), 'right', 'y')
            if self._is_controller_connected():
                # For ViGEm, use direct stick control
                if self._use_vigem and self._vigem:
                    self._vigem.set_right_stick(px, py)
                else:
                    ax = str(self._config.get("axis_mapping.right_x", "rx"))
                    ay = str(self._config.get("axis_mapping.right_y", "ry"))
                    if ax != "none":
                        self._set_axis_target(ax, px)
                    if ay != "none":
                        self._set_axis_target(ay, py)
        except Exception:
            pass

    @Slot(float)
    def setThrottle(self, value: float) -> None:  # noqa: N802
        try:
            if self._is_controller_connected():
                v = float(value)
                v = max(0.0, min(1.0, v))
                # For ViGEm, use left trigger
                if self._use_vigem and self._vigem:
                    self._vigem.set_left_trigger(v)
                else:
                    axis = str(self._config.get("axis_mapping.throttle", "z"))
                    if axis != "none":
                        normalized = v * 2.0 - 1.0
                        self._vjoy.update_axis(axis, normalized)
        except Exception:
            pass

    @Slot(float)
    def setRudder(self, value: float) -> None:  # noqa: N802
        try:
            if self._is_controller_connected():
                # Apply Rudder Settings dialog curve
                v = self._config.apply_rudder_sensitivity_curve(float(value))
                # For ViGEm, use right trigger (convert from -1..1 to 0..1)
                if self._use_vigem and self._vigem:
                    trigger_val = (v + 1.0) / 2.0
                    self._vigem.set_right_trigger(trigger_val)
                else:
                    axis = str(self._config.get("axis_mapping.rudder", "rz"))
                    if axis != "none":
                        self._set_axis_target(axis, v)
        except Exception:
            pass

    # ----- Internal smoothing helpers -----
    def _set_axis_target(self, axis: str, target: float) -> None:
        try:
            a = axis.lower()
            t = max(-1.0, min(1.0, float(target)))
            st = self._axis_state.get(a)
            if st is None:
                st = {"current": 0.0, "target": t}
                self._axis_state[a] = st
            else:
                st["target"] = t
            # If smoothing disabled, snap immediately
            if not bool(self._config.get("safety.enable_smoothing", True)):
                st["current"] = t
                if self._vjoy.is_connected:
                    self._vjoy.update_axis(a, t)
        except Exception:
            pass

    def _smoothing_tick(self) -> None:
        try:
            # Skip smoothing for ViGEm (it handles its own updates)
            if self._use_vigem:
                return
            if not self._vjoy or not self._vjoy.is_connected:
                return
            alpha = float(self._config.get("safety.smoothing_factor", 0.1))
            alpha = max(0.01, min(1.0, alpha))
            enabled = bool(self._config.get("safety.enable_smoothing", True))
            for a, st in list(self._axis_state.items()):
                cur = float(st.get("current", 0.0))
                tgt = float(st.get("target", 0.0))
                if enabled:
                    nxt = cur + (tgt - cur) * alpha
                    # snap if close to avoid lingering
                    if abs(nxt - tgt) < 1e-3:
                        nxt = tgt
                else:
                    nxt = tgt
                st["current"] = nxt
                self._vjoy.update_axis(a, nxt)
        except Exception:
            pass

    # ----- Button mode helpers for QML -----
    @Slot(int, result=bool)
    def isButtonToggle(self, button_id: int) -> bool:  # noqa: N802
        try:
            return bool(self._config.get(f"buttons.button_{int(button_id)}.toggle_mode", False))
        except Exception:
            return False

    # Settings openers (can be wired later or remain placeholders)
    @Slot()
    def openJoystickSettings(self) -> None:  # noqa: N802
        try:
            dlg = JoystickSettingsQt(self._config, None)
            dlg.exec()
        except Exception:
            pass

    @Slot()
    def openAxisSettings(self) -> None:  # noqa: N802
        """Open unified per-axis sensitivity settings dialog."""
        try:
            dlg = AxisSettingsQt(self._config, None)
            dlg.exec()
        except Exception:
            pass

    @Slot()
    def openButtonSettings(self) -> None:  # noqa: N802
        try:
            dlg = ButtonSettingsQt(self._config, None)
            if dlg.exec():
                # Bump version so QML re-evaluates bindings that depend on button modes
                self._buttons_version += 1
                self.buttonsVersionChanged.emit(self._buttons_version)
        except Exception:
            pass

    @Slot()
    def openSliderSettings(self) -> None:  # noqa: N802
        """Open slider/trigger sensitivity settings dialog."""
        try:
            dlg = SliderSettingsQt(self._config, None)
            dlg.exec()
        except Exception:
            pass

    @Slot()
    def openAxisMapping(self) -> None:  # noqa: N802
        try:
            dlg = AxisMappingQt(self._config, None)
            dlg.exec()
        except Exception:
            pass

    # ----- Expose some status -----
    @Slot(result=str)
    def getVersion(self) -> str:  # noqa: N802
        """Get the application version string."""
        try:
            import src as _src
            return _src.__version__
        except Exception:
            pass
        try:
            import importlib
            _m = importlib.import_module("src")
            return _m.__version__
        except Exception:
            return "1.4.2"

    @Slot(result=bool)
    def isVJoyConnected(self) -> bool:  # noqa: N802
        return self._is_controller_connected()
    
    @Slot(result=str)
    def getControllerType(self) -> str:  # noqa: N802
        """Get the type of controller interface being used."""
        if self._use_vigem:
            return "Xbox 360 (ViGEm)"
        return "vJoy (DirectInput)"

    @Slot(result=str)
    def getOutputMode(self) -> str:  # noqa: N802
        """Get current output mode: 'vigem' or 'vjoy'."""
        return "vigem" if self._use_vigem else "vjoy"

    @Slot(result=bool)
    def isVigemAvailable(self) -> bool:  # noqa: N802
        """Check if ViGEm (vgamepad) is available on this system."""
        return VIGEM_AVAILABLE

    @Slot(str)
    def setOutputMode(self, mode: str) -> None:  # noqa: N802
        """Switch output device. mode is 'vjoy' or 'vigem'."""
        mode = mode.lower().strip()
        if mode not in ("vjoy", "vigem"):
            return
        want_vigem = mode == "vigem"
        if want_vigem == self._use_vigem:
            return
        if want_vigem and not VIGEM_AVAILABLE:
            print("Cannot switch to ViGEm — vgamepad not installed")
            return
        # Initialize the target interface if needed
        if want_vigem and self._vigem is None:
            self._vigem = ViGEmInterface(self._config)
        if not want_vigem and self._vjoy is None:
            self._vjoy = VJoyInterface(self._config)
        self._use_vigem = want_vigem
        self._config.set("controller.prefer_vigem", want_vigem)
        self._config.save_config()
        self.outputModeChanged.emit(mode)
        self.vjoyConnectionChanged.emit(self._is_controller_connected())
        print(f"Output mode switched to: {self.getControllerType()}")

    # ----- Borderless gaming -----
    @Slot(result=bool)
    def isBorderlessAvailable(self) -> bool:  # noqa: N802
        """Check if borderless gaming module is available."""
        return BORDERLESS_AVAILABLE

    @Slot(result="QVariantList")
    def getWindowList(self) -> list:  # noqa: N802
        """Get list of visible windows for the game picker."""
        if not BORDERLESS_AVAILABLE:
            return []
        try:
            windows = _borderless.enumerate_windows()
            return [
                {
                    "hwnd": w.hwnd,
                    "title": w.title,
                    "className": w.class_name,
                    "pid": w.pid,
                    "x": w.x,
                    "y": w.y,
                    "width": w.width,
                    "height": w.height,
                    "isBorderless": w.is_borderless,
                }
                for w in windows
            ]
        except Exception as e:
            print(f"[bridge] getWindowList error: {e}")
            return []

    @Slot(result="QVariantMap")
    def autoDetectGame(self) -> dict:  # noqa: N802
        """Auto-detect a known game from running windows."""
        if not BORDERLESS_AVAILABLE:
            return {}
        try:
            result = _borderless.auto_detect_game()
            if result:
                win, game = result
                return {
                    "hwnd": win.hwnd,
                    "title": win.title,
                    "gameName": game.name,
                    "status": game.status,
                    "notes": game.notes,
                    "recommendedInterval": game.recommended_interval_ms,
                }
        except Exception as e:
            print(f"[bridge] autoDetectGame error: {e}")
        return {}

    @Slot(int, result=bool)
    def makeGameBorderless(self, hwnd: int) -> bool:  # noqa: N802
        """Make a game window borderless (keep current position/size)."""
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            return _borderless.make_borderless(hwnd)
        except Exception as e:
            print(f"[bridge] makeGameBorderless error: {e}")
            return False

    @Slot(int, int, int, int, int, result=bool)
    def makeGameBorderlessAt(self, hwnd: int, x: int, y: int, w: int, h: int) -> bool:  # noqa: N802
        """Make a game window borderless at a specific position and size."""
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            return _borderless.make_borderless(hwnd, x, y, w, h)
        except Exception as e:
            print(f"[bridge] makeGameBorderlessAt error: {e}")
            return False

    @Slot(int, result=bool)
    def makeGameBorderlessFill(self, hwnd: int) -> bool:  # noqa: N802
        """Make a game window borderless and fill the entire monitor."""
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            return _borderless.make_borderless(hwnd, fill_monitor=True)
        except Exception as e:
            print(f"[bridge] makeGameBorderlessFill error: {e}")
            return False

    @Slot(int, int, int, int, int, result=bool)
    def resizeGameWindow(self, hwnd: int, x: int, y: int, w: int, h: int) -> bool:  # noqa: N802
        """Reposition/resize a game window to specific coordinates."""
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            return _borderless.resize_window(hwnd, x, y, w, h)
        except Exception as e:
            print(f"[bridge] resizeGameWindow error: {e}")
            return False

    @Slot(int, result=bool)
    def restoreGameWindow(self, hwnd: int) -> bool:  # noqa: N802
        """Restore a game window's original decorations."""
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            return _borderless.restore_window(hwnd)
        except Exception as e:
            print(f"[bridge] restoreGameWindow error: {e}")
            return False

    @Slot(int, int, result=bool)
    def applyBorderlessAndRelease(self, hwnd: int, interval_ms: int) -> bool:  # noqa: N802
        """Make borderless AND start aggressive cursor release."""
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            return _borderless.apply_borderless_and_release(hwnd, interval_ms)
        except Exception as e:
            print(f"[bridge] applyBorderlessAndRelease error: {e}")
            return False

    @Slot(int, result=bool)
    def restoreAndStopRelease(self, hwnd: int) -> bool:  # noqa: N802
        """Restore window and stop cursor release."""
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            return _borderless.restore_and_stop_release(hwnd)
        except Exception as e:
            print(f"[bridge] restoreAndStopRelease error: {e}")
            return False

    @Slot(int)
    def startCursorRelease(self, interval_ms: int) -> None:  # noqa: N802
        """Start cursor release without borderless (standalone)."""
        if not BORDERLESS_AVAILABLE:
            return
        try:
            _borderless.start_cursor_release(interval_ms, game_hwnd=0)
        except Exception as e:
            print(f"[bridge] startCursorRelease error: {e}")

    @Slot(int, int)
    def startCursorReleaseWithHwnd(self, interval_ms: int, game_hwnd: int) -> None:  # noqa: N802
        """Start cursor release with game HWND for thread-attached release."""
        if not BORDERLESS_AVAILABLE:
            return
        try:
            _borderless.start_cursor_release(interval_ms, game_hwnd=game_hwnd)
        except Exception as e:
            print(f"[bridge] startCursorReleaseWithHwnd error: {e}")

    @Slot()
    def stopCursorRelease(self) -> None:  # noqa: N802
        """Stop cursor release."""
        if not BORDERLESS_AVAILABLE:
            return
        try:
            _borderless.stop_cursor_release()
        except Exception as e:
            print(f"[bridge] stopCursorRelease error: {e}")

    @Slot(result=bool)
    def isCursorReleaseActive(self) -> bool:  # noqa: N802
        """Check if cursor release is currently running."""
        if not BORDERLESS_AVAILABLE:
            return False
        return _borderless.is_cursor_release_active()

    @Slot(result="QVariantList")
    def getGameCompatList(self) -> list:  # noqa: N802
        """Get the game compatibility database for the QML compatibility tab."""
        if not BORDERLESS_AVAILABLE:
            return []
        try:
            return [
                {
                    "name": g.name,
                    "status": g.status,
                    "inputMethod": g.input_method,
                    "notes": g.notes,
                }
                for g in _borderless.get_compatible_games()
            ]
        except Exception as e:
            print(f"[bridge] getGameCompatList error: {e}")
            return []

    # ----- Profile system -----
    @Slot(result=str)
    def getCurrentProfile(self) -> str:  # noqa: N802
        """Get the current profile ID."""
        return self._config.get_current_profile()

    @Slot(result=str)
    def getLayoutType(self) -> str:  # noqa: N802
        """Get the layout type of the current profile."""
        return self._config.get_layout_type()

    @Slot(result="QVariantList")
    def getAvailableProfiles(self) -> list:  # noqa: N802
        """Get list of available profiles for QML menu."""
        return self._config.get_available_profiles()

    @Slot(str, result=bool)
    def switchProfile(self, profile_id: str) -> bool:  # noqa: N802
        """Switch to a different profile."""
        success = self._config.switch_profile(profile_id)
        if success:
            self.profileChanged.emit(profile_id)
            self.layoutTypeChanged.emit(self._config.get_layout_type())
            self._buttons_version += 1
            self.buttonsVersionChanged.emit(self._buttons_version)
            # Track recently used (keep last 5, most-recent first, no duplicates)
            if profile_id in self._recent_profiles:
                self._recent_profiles.remove(profile_id)
            self._recent_profiles.insert(0, profile_id)
            self._recent_profiles = self._recent_profiles[:5]
            self._config.set("ui.recent_profiles", self._recent_profiles)
            self._config.save_config()
            self.recentProfilesChanged.emit()
        return success

    @Slot(result="QVariantList")
    def getRecentProfiles(self) -> list:  # noqa: N802
        """Get recently used profile IDs (most-recent first, up to 5)."""
        all_profiles = {p["id"]: p for p in self._config.get_available_profiles()}
        return [all_profiles[pid] for pid in self._recent_profiles if pid in all_profiles]

    @Slot(result=bool)
    def isBundledProfile(self) -> bool:  # noqa: N802
        """Return True if the current profile is a built-in (bundled) profile."""
        return self._config.is_builtin_profile(self._config.get_current_profile())

    @Slot(str, str, result=str)
    def createProfileAs(self, name: str, description: str = "") -> str:  # noqa: N802
        """Create a new blank profile and return its ID (empty string on failure)."""
        new_id = self._config.create_profile_as(name, description)
        if new_id:
            self.profilesListChanged.emit()
        return new_id or ""

    @Slot(int, result=str)
    def getButtonLabel(self, button_id: int) -> str:  # noqa: N802
        """Get the label for a button based on current profile."""
        return self._config.get_button_label(button_id)

    @Slot(result=bool)
    def saveCurrentProfile(self) -> bool:  # noqa: N802
        """Save current settings to the active profile."""
        success = self._config.save_current_profile()
        self.profileSaved.emit(success)
        return success

    @Slot(str, result=bool)
    def resetProfile(self, profile_id: str) -> bool:  # noqa: N802
        """Reset a profile to its default settings."""
        success = self._config.reset_profile(profile_id)
        if success and profile_id == self._config.get_current_profile():
            # Refresh UI if we reset the current profile
            self._buttons_version += 1
            self.buttonsVersionChanged.emit(self._buttons_version)
        return success

    @Slot(str, str, result=str)
    def duplicateProfile(self, source_id: str, new_name: str) -> str:  # noqa: N802
        """Duplicate a profile with a new name. Returns new profile ID or empty string."""
        new_id = self._config.duplicate_profile(source_id, new_name)
        if new_id:
            self.profilesListChanged.emit()
        return new_id if new_id else ""

    @Slot(str, str, result=str)
    def createProfileAs(self, name: str, description: str) -> str:  # noqa: N802
        """Create a new profile from current settings with given name and description."""
        new_id = self._config.create_profile_as(name, description)
        if new_id:
            self.profilesListChanged.emit()
        return new_id if new_id else ""

    @Slot(str, result=bool)
    def deleteProfile(self, profile_id: str) -> bool:  # noqa: N802
        """Delete a user-created profile."""
        success = self._config.delete_profile(profile_id)
        if success:
            self.profilesListChanged.emit()
        return success

    @Slot(str, result=bool)
    def isBuiltinProfile(self, profile_id: str) -> bool:  # noqa: N802
        """Check if a profile is a built-in profile."""
        return self._config.is_builtin_profile(profile_id)

    @Slot(result=str)
    def getUserProfilesPath(self) -> str:  # noqa: N802
        """Get the path to the user profiles directory."""
        return self._config.get_user_profiles_path()

    # ----- Custom layout system -----
    @Slot(result=str)
    def getCustomLayout(self) -> str:  # noqa: N802
        """Get custom layout widgets as JSON string for QML."""
        try:
            profile_data = self._config.get_current_profile_data()
            if profile_data and "custom_layout" in profile_data:
                import json
                widgets = profile_data["custom_layout"].get("widgets", [])
                return json.dumps(widgets)
        except Exception:
            pass
        return "[]"

    @Slot(result=int)
    def getCustomLayoutGridSnap(self) -> int:  # noqa: N802
        """Get grid snap size for custom layout."""
        try:
            profile_data = self._config.get_current_profile_data()
            if profile_data and "custom_layout" in profile_data:
                return int(profile_data["custom_layout"].get("grid_snap", 10))
        except Exception:
            pass
        return 10

    @Slot(result=bool)
    def getCustomLayoutShowGrid(self) -> bool:  # noqa: N802
        """Get whether grid is shown for custom layout."""
        try:
            profile_data = self._config.get_current_profile_data()
            if profile_data and "custom_layout" in profile_data:
                return bool(profile_data["custom_layout"].get("show_grid", True))
        except Exception:
            pass
        return True

    @Slot(str, int, bool)
    def saveCustomLayout(self, widgets_json: str, grid_snap: int, show_grid: bool) -> None:  # noqa: N802
        """Save custom layout widgets from QML (silent — no profileSaved signal)."""
        try:
            import json
            widgets = json.loads(widgets_json)
            self._config.save_custom_layout(widgets, int(grid_snap), bool(show_grid))
        except Exception as e:
            print(f"Error saving custom layout: {e}")

    @Slot(str, str, int, bool)
    def saveCustomLayoutAs(self, name: str, widgets_json: str, grid_snap: int, show_grid: bool) -> None:  # noqa: N802
        """Save custom layout as a new profile with a custom name."""
        try:
            import json
            import copy
            widgets = json.loads(widgets_json)
            # Duplicate current profile with new name
            profile_data = copy.deepcopy(self._config.get_current_profile_data() or {})
            profile_id = name.lower().replace(" ", "_").replace("-", "_")
            profile_data["name"] = name
            profile_data["description"] = f"Custom layout: {name}"
            profile_data["layout_type"] = "custom"
            if "custom_layout" not in profile_data:
                profile_data["custom_layout"] = {}
            profile_data["custom_layout"]["widgets"] = widgets
            profile_data["custom_layout"]["grid_snap"] = int(grid_snap)
            profile_data["custom_layout"]["show_grid"] = bool(show_grid)
            # Save as new profile
            self._config.save_profile_as(profile_id, profile_data)
            print(f"Saved custom layout as: {name} (id: {profile_id})")
            self.profileSaved.emit(True)
        except Exception as e:
            print(f"Error saving custom layout as '{name}': {e}")
            self.profileSaved.emit(False)

    @Slot()
    def openProfilesFolder(self) -> None:  # noqa: N802
        """Open the user profiles folder in the system file explorer."""
        import subprocess
        import sys
        path = self._config.get_user_profiles_path()
        if sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    # ----- Borderless gaming integration -----
    @Slot(result=bool)
    def isBorderlessAvailable(self) -> bool:  # noqa: N802
        """Check if borderless gaming features are available."""
        return BORDERLESS_AVAILABLE and sys.platform == "win32"

    @Slot(result=str)
    def getRunningWindows(self) -> str:  # noqa: N802
        """Get list of running windows as JSON for QML game picker.
        
        Returns JSON array of objects with: hwnd, title, className, pid,
        x, y, width, height, isBorderless.
        """
        import json
        if not BORDERLESS_AVAILABLE:
            return "[]"
        try:
            windows = _borderless.enumerate_windows()
            result = []
            for w in windows:
                result.append({
                    "hwnd": w.hwnd,
                    "title": w.title,
                    "className": w.class_name,
                    "pid": w.pid,
                    "x": w.x,
                    "y": w.y,
                    "width": w.width,
                    "height": w.height,
                    "isBorderless": w.is_borderless,
                })
            return json.dumps(result)
        except Exception as e:
            print(f"getRunningWindows failed: {e}")
            return "[]"

    @Slot(result=str)
    def getGameCompatibility(self) -> str:  # noqa: N802
        """Get game compatibility database as JSON for QML.
        
        Returns JSON array of objects with: name, status, inputMethod,
        notes, needsBorderless, needsCursorRelease, recommendedIntervalMs.
        """
        import json
        if not BORDERLESS_AVAILABLE:
            return "[]"
        try:
            games = _borderless.get_compatible_games()
            result = []
            for g in games:
                result.append({
                    "name": g.name,
                    "status": g.status,
                    "inputMethod": g.input_method,
                    "notes": g.notes,
                    "windowTitleHint": g.window_title_hint,
                    "needsBorderless": g.needs_borderless,
                    "needsCursorRelease": g.needs_cursor_release,
                    "recommendedIntervalMs": g.recommended_interval_ms,
                })
            return json.dumps(result)
        except Exception as e:
            print(f"getGameCompatibility failed: {e}")
            return "[]"

    @Slot(result=str)
    def autoDetectGame(self) -> str:  # noqa: N802
        """Auto-detect a running game from the compatibility database.
        
        Returns JSON object with window info and game compat data, or empty string.
        """
        import json
        if not BORDERLESS_AVAILABLE:
            return ""
        try:
            result = _borderless.auto_detect_game()
            if result:
                w, g = result
                return json.dumps({
                    "window": {
                        "hwnd": w.hwnd,
                        "title": w.title,
                        "className": w.class_name,
                    },
                    "game": {
                        "name": g.name,
                        "status": g.status,
                        "inputMethod": g.input_method,
                        "notes": g.notes,
                        "needsBorderless": g.needs_borderless,
                        "needsCursorRelease": g.needs_cursor_release,
                        "recommendedIntervalMs": g.recommended_interval_ms,
                    },
                })
        except Exception as e:
            print(f"autoDetectGame failed: {e}")
        return ""

    @Slot(int, result=bool)
    def makeGameBorderless(self, hwnd: int) -> bool:  # noqa: N802
        """Make a game window borderless fullscreen.
        
        Args:
            hwnd: Window handle of the game.
        
        Returns:
            True on success.
        """
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            success = _borderless.make_borderless(hwnd)
            if success:
                self._borderless_game_hwnd = hwnd
                self.borderlessModeChanged.emit(hwnd, True)
            return success
        except Exception as e:
            print(f"makeGameBorderless failed: {e}")
            return False

    @Slot(int, result=bool)
    def restoreGameWindow(self, hwnd: int) -> bool:  # noqa: N802
        """Restore a game window's original decorations.
        
        Args:
            hwnd: Window handle previously passed to makeGameBorderless().
        
        Returns:
            True on success.
        """
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            success = _borderless.restore_window(hwnd)
            if success:
                if self._borderless_game_hwnd == hwnd:
                    self._borderless_game_hwnd = 0
                self.borderlessModeChanged.emit(hwnd, False)
            return success
        except Exception as e:
            print(f"restoreGameWindow failed: {e}")
            return False

    @Slot(int, result=bool)
    def isGameBorderless(self, hwnd: int) -> bool:  # noqa: N802
        """Check if a game window has been made borderless by us."""
        if not BORDERLESS_AVAILABLE:
            return False
        return _borderless.is_borderless(hwnd)

    @Slot(int)
    def startCursorRelease(self, interval_ms: int = 50) -> None:  # noqa: N802
        """Start continuous ClipCursor release polling.
        
        This fights games that re-apply ClipCursor every frame.
        
        Args:
            interval_ms: Release interval in milliseconds (default 50).
        """
        if not BORDERLESS_AVAILABLE:
            return
        try:
            def _on_change(active: bool):
                self._cursor_release_active = active
                self.cursorReleaseChanged.emit(active)
            
            _borderless.start_cursor_release(interval_ms, _on_change)
        except Exception as e:
            print(f"startCursorRelease failed: {e}")

    @Slot()
    def stopCursorRelease(self) -> None:  # noqa: N802
        """Stop the continuous ClipCursor release."""
        if not BORDERLESS_AVAILABLE:
            return
        try:
            _borderless.stop_cursor_release()
            self._cursor_release_active = False
            self.cursorReleaseChanged.emit(False)
        except Exception as e:
            print(f"stopCursorRelease failed: {e}")

    @Slot(result=bool)
    def isCursorReleaseActive(self) -> bool:  # noqa: N802
        """Check if cursor release polling is currently active."""
        if not BORDERLESS_AVAILABLE:
            return False
        return _borderless.is_cursor_release_active()

    @Slot(result=str)
    def getClipCursorRect(self) -> str:  # noqa: N802
        """Get the current ClipCursor rectangle as JSON.
        
        Returns JSON string like '{"left":0,"top":0,"right":1920,"bottom":1080}'
        or empty string if no clip is active.
        """
        import json
        if not BORDERLESS_AVAILABLE:
            return ""
        try:
            rect = _borderless.get_clip_cursor_rect()
            if rect:
                return json.dumps({
                    "left": rect[0], "top": rect[1],
                    "right": rect[2], "bottom": rect[3],
                })
        except Exception as e:
            print(f"getClipCursorRect failed: {e}")
        return ""

    @Slot(int, int, result=bool)
    def applyBorderlessAndRelease(self, hwnd: int, interval_ms: int = 50) -> bool:  # noqa: N802
        """One-call: make game borderless AND start cursor release.
        
        This is the recommended approach for most games.
        
        Args:
            hwnd: Game window handle.
            interval_ms: Cursor release interval.
        
        Returns:
            True if borderless was applied successfully.
        """
        if not BORDERLESS_AVAILABLE:
            return False
        try:
            success = _borderless.make_borderless(hwnd)
            if success:
                self._borderless_game_hwnd = hwnd
                self.borderlessModeChanged.emit(hwnd, True)
                
                def _on_change(active: bool):
                    self._cursor_release_active = active
                    self.cursorReleaseChanged.emit(active)
                
                _borderless.start_cursor_release(interval_ms, _on_change)
            return success
        except Exception as e:
            print(f"applyBorderlessAndRelease failed: {e}")
            return False

    @Slot(int)
    def restoreAndStopRelease(self, hwnd: int) -> None:  # noqa: N802
        """Restore game window and stop cursor release."""
        if not BORDERLESS_AVAILABLE:
            return
        try:
            _borderless.stop_cursor_release()
            self._cursor_release_active = False
            self.cursorReleaseChanged.emit(False)
            
            success = _borderless.restore_window(hwnd)
            if success:
                if self._borderless_game_hwnd == hwnd:
                    self._borderless_game_hwnd = 0
                self.borderlessModeChanged.emit(hwnd, False)
        except Exception as e:
            print(f"restoreAndStopRelease failed: {e}")

    # ----- Controller Mode Enforcement (v1.4.1) -----
    @Slot(result=bool)
    def isControllerModeAvailable(self) -> bool:  # noqa: N802
        """Check if controller mode enforcement is available.
        
        Requires ViGEm (virtual Xbox controller) and mouse_hider module.
        """
        return (MOUSE_HIDER_AVAILABLE and self._use_vigem and
                self._vigem is not None and self._vigem.is_connected)

    @Slot(int, int, result=bool)
    def startControllerMode(self, game_hwnd: int, pulse_hz: int = 30) -> bool:  # noqa: N802
        """Start Controller Mode Enforcement.
        
        Makes the game think only a controller is connected by sending
        a constant stream of ViGEm keep-alive signals. The game switches
        to controller mode and voluntarily releases the mouse cursor.
        
        This is fundamentally different from ClipCursor release — instead
        of fighting the game's mouse capture, we make the game STOP capturing.
        
        Args:
            game_hwnd: HWND of the game window.
            pulse_hz: Keep-alive frequency (10-60 Hz recommended).
        
        Returns:
            True if started successfully.
        """
        if not MOUSE_HIDER_AVAILABLE or not _mouse_hider:
            print("[bridge] mouse_hider module not available")
            return False
        if not self._use_vigem or not self._vigem or not self._vigem.gamepad:
            print("[bridge] ViGEm not available — controller mode requires Xbox emulation")
            return False
        
        try:
            # Get Nimbus window handle for mouse passthrough
            nimbus_hwnd = 0
            if self._window:
                nimbus_hwnd = int(self._window.winId())
            
            def _on_change(active: bool):
                self._controller_mode_active = active
                self.controllerModeChanged.emit(active)
            
            success = _mouse_hider.start_controller_mode(
                gamepad=self._vigem.gamepad,
                vigem_interface=self._vigem,
                game_hwnd=game_hwnd,
                nimbus_hwnd=nimbus_hwnd,
                pulse_hz=max(5, min(120, pulse_hz)),
                use_mouse_hook=bool(game_hwnd),
                callback=_on_change,
            )
            return success
        except Exception as e:
            print(f"[bridge] startControllerMode failed: {e}")
            return False

    @Slot()
    def stopControllerMode(self) -> None:  # noqa: N802
        """Stop Controller Mode Enforcement."""
        if not MOUSE_HIDER_AVAILABLE or not _mouse_hider:
            return
        try:
            _mouse_hider.stop_controller_mode()
            self._controller_mode_active = False
            self.controllerModeChanged.emit(False)
        except Exception as e:
            print(f"[bridge] stopControllerMode failed: {e}")

    @Slot(result=bool)
    def isControllerModeActive(self) -> bool:  # noqa: N802
        """Check if controller mode enforcement is currently running."""
        if not MOUSE_HIDER_AVAILABLE or not _mouse_hider:
            return False
        return _mouse_hider.is_controller_mode_active()

    @Slot()
    def sendControllerBurst(self) -> None:  # noqa: N802
        """Send a one-shot burst of controller signals.
        
        Forces the game into controller mode without enabling the
        continuous keep-alive. Useful as a quick fix or test.
        """
        if not MOUSE_HIDER_AVAILABLE or not _mouse_hider:
            return
        if not self._use_vigem or not self._vigem or not self._vigem.gamepad:
            print("[bridge] ViGEm not available for controller burst")
            return
        try:
            _mouse_hider.send_controller_burst(self._vigem.gamepad)
        except Exception as e:
            print(f"[bridge] sendControllerBurst failed: {e}")

    @Slot(result=str)
    def getControllerModeStats(self) -> str:  # noqa: N802
        """Get controller mode statistics as JSON string."""
        import json
        if not MOUSE_HIDER_AVAILABLE or not _mouse_hider:
            return "{}"
        try:
            return json.dumps(_mouse_hider.get_controller_mode_stats())
        except Exception as e:
            print(f"[bridge] getControllerModeStats failed: {e}")
            return "{}"

    @Slot(int, int, result=bool)
    def startFullGameMode(self, game_hwnd: int, pulse_hz: int = 30) -> bool:  # noqa: N802
        """Start the full game integration: borderless + cursor release + controller mode.
        
        This is the recommended one-call approach that combines all
        mechanisms for maximum compatibility:
          0. Enable Game Focus Mode (WS_EX_NOACTIVATE — clicks don't steal focus)
          1. Make game borderless (if not already)
          2. Start ClipCursor release polling (fights cursor confinement)
          3. Start Controller Mode (makes game voluntarily release mouse)
        
        Args:
            game_hwnd: HWND of the game window.
            pulse_hz: Controller keep-alive frequency.
        
        Returns:
            True if at least one mechanism started successfully.
        """
        success = False
        
        # Step 0: Enable Game Focus Mode so clicking Nimbus doesn't steal focus
        # NOTE: This is session-only — NOT saved to config, so it resets on restart.
        if WINDOW_UTILS_AVAILABLE and self._window and not self._no_focus_mode:
            try:
                hwnd = get_qt_window_handle(self._window)
                if make_window_no_activate(hwnd):
                    self._no_focus_mode = True
                    self.noFocusModeChanged.emit(True)
                    print("[bridge] Full Game Mode: Game Focus Mode auto-enabled (session-only)")
            except Exception as e:
                print(f"[bridge] Full Game Mode: focus mode failed ({e}), continuing...")
        
        # Step 1: ClipCursor release (fights mouse confinement)
        if BORDERLESS_AVAILABLE:
            try:
                def _on_release_change(active: bool):
                    self._cursor_release_active = active
                    self.cursorReleaseChanged.emit(active)
                _borderless.start_cursor_release(2, _on_release_change, game_hwnd=game_hwnd)
                success = True
                print("[bridge] Full Game Mode: cursor release started")
            except Exception as e:
                print(f"[bridge] Full Game Mode: cursor release failed ({e}), continuing...")
        
        # Step 2: Controller Mode — ALWAYS try to get a ViGEm gamepad
        # Even if the profile normally uses vJoy, Game Mode needs ViGEm for
        # controller-mode-enforcement (XInput signals to trick the game).
        gamepad = None
        if self._vigem and self._vigem.gamepad:
            gamepad = self._vigem.gamepad
            print("[bridge] Full Game Mode: using existing ViGEm gamepad")
        elif VIGEM_AVAILABLE:
            # Create a ViGEm gamepad on demand for Game Mode
            try:
                print("[bridge] Full Game Mode: profile doesn't use ViGEm, creating one for Game Mode...")
                if self._vigem is None:
                    self._vigem = ViGEmInterface(self._config)
                if self._vigem.is_connected and self._vigem.gamepad:
                    gamepad = self._vigem.gamepad
                    print("[bridge] Full Game Mode: on-demand ViGEm gamepad created!")
                else:
                    print("[bridge] Full Game Mode: ViGEm gamepad creation failed")
                    print("[bridge]   -> Is ViGEmBus driver installed? Run: pip install vgamepad")
            except Exception as e:
                print(f"[bridge] Full Game Mode: ViGEm init error: {e}")
        else:
            print("[bridge] Full Game Mode: ViGEm NOT available")
            print("[bridge]   -> vgamepad package or ViGEmBus driver not installed")
            print("[bridge]   -> Controller mode enforcement requires ViGEm")
            print("[bridge]   -> Install: pip install vgamepad")
        
        if MOUSE_HIDER_AVAILABLE and gamepad:
            try:
                nimbus_hwnd = int(self._window.winId()) if self._window else 0
                def _on_ctrl_change(active: bool):
                    self._controller_mode_active = active
                    self.controllerModeChanged.emit(active)
                _mouse_hider.start_controller_mode(
                    gamepad=gamepad,
                    vigem_interface=self._vigem,
                    game_hwnd=game_hwnd,
                    nimbus_hwnd=nimbus_hwnd,
                    pulse_hz=pulse_hz,
                    use_mouse_hook=True,
                    callback=_on_ctrl_change,
                )
                success = True
                print("[bridge] Full Game Mode: controller mode STARTED")
            except Exception as e:
                print(f"[bridge] Full Game Mode: controller mode failed ({e})")
        elif not MOUSE_HIDER_AVAILABLE:
            print("[bridge] Full Game Mode: mouse_hider module not available")
        elif not gamepad:
            print("[bridge] Full Game Mode: no ViGEm gamepad — controller mode SKIPPED")
        
        if success:
            print("[bridge] Full Game Mode ACTIVE")
        else:
            print("[bridge] Full Game Mode: no mechanisms could be started")
        
        return success

    @Slot(int)
    def stopFullGameMode(self, game_hwnd: int) -> None:  # noqa: N802
        """Stop all game integration mechanisms.
        
        Reverses startFullGameMode: stops controller mode, cursor release,
        and restores the game window.
        """
        # Stop controller mode
        if MOUSE_HIDER_AVAILABLE and _mouse_hider:
            try:
                _mouse_hider.stop_controller_mode()
                self._controller_mode_active = False
                self.controllerModeChanged.emit(False)
            except Exception:
                pass
        
        # Stop cursor release
        if BORDERLESS_AVAILABLE:
            try:
                _borderless.stop_cursor_release()
                self._cursor_release_active = False
                self.cursorReleaseChanged.emit(False)
            except Exception:
                pass
        
        # Restore window
        if BORDERLESS_AVAILABLE and game_hwnd:
            try:
                success = _borderless.restore_window(game_hwnd)
                if success:
                    if self._borderless_game_hwnd == game_hwnd:
                        self._borderless_game_hwnd = 0
                    self.borderlessModeChanged.emit(game_hwnd, False)
            except Exception:
                pass
        
        # Disable game focus mode — restore normal window activation
        if WINDOW_UTILS_AVAILABLE and self._window:
            try:
                hwnd = get_qt_window_handle(self._window)
                remove_window_no_activate(hwnd)
                self._no_focus_mode = False
                self.noFocusModeChanged.emit(False)
                print("[bridge] Full Game Mode: Game Focus Mode disabled, window is activatable again")
            except Exception:
                pass
        
        print("[bridge] Full Game Mode stopped")

    @Slot(result="QVariantMap")
    def getGameModeDiagnostics(self) -> dict:  # noqa: N802
        """Return diagnostic info about Game Mode readiness for the UI."""
        import sys as _sys
        result = {
            "vigem_package": VIGEM_AVAILABLE,
            "vigem_gamepad": bool(self._vigem and self._vigem.gamepad),
            "vigem_connected": bool(self._vigem and self._vigem.is_connected),
            "mouse_hider": MOUSE_HIDER_AVAILABLE,
            "borderless": BORDERLESS_AVAILABLE,
            "window_utils": WINDOW_UTILS_AVAILABLE,
            "profile": str(self._config.get_layout_type()),
            "use_vigem": self._use_vigem,
            "controller_mode_active": self._controller_mode_active,
            "driver_installed": False,
        }
        # Check if ViGEmBus driver is installed on Windows
        if _sys.platform == "win32":
            try:
                import subprocess
                r = subprocess.run(
                    ["sc", "query", "ViGEmBus"],
                    capture_output=True, text=True, timeout=3
                )
                result["driver_installed"] = "RUNNING" in r.stdout
                result["driver_query"] = r.stdout.strip()[:200]
            except Exception:
                result["driver_installed"] = False
                result["driver_query"] = "query failed"
        return result

    # =================================================================
    # Account, Telemetry & Updater — QML-callable slots
    # =================================================================

    # ---- Account ----

    @Slot(str, str, result=bool)
    def loginWithEmail(self, email: str, password: str) -> bool:  # noqa: N802
        """Sign in with email and password.  Returns True on success."""
        try:
            from .cloud_client import CloudClient
            cloud: CloudClient = self.parent().findChild(CloudClient)
            if cloud:
                return cloud.login_with_email(email, password)
        except Exception:
            pass
        return False

    @Slot(str)
    def loginWithProvider(self, provider: str) -> None:  # noqa: N802
        """Open the system browser for OAuth login (google / facebook)."""
        try:
            from .cloud_client import CloudClient
            cloud: CloudClient = self.parent().findChild(CloudClient)
            if cloud:
                cloud.login_with_browser(provider)
        except Exception:
            pass

    @Slot()
    def logoutAccount(self) -> None:  # noqa: N802
        """Sign out and clear all stored tokens."""
        try:
            from .cloud_client import CloudClient
            cloud: CloudClient = self.parent().findChild(CloudClient)
            if cloud:
                cloud.logout()
        except Exception:
            pass

    # ---- Telemetry ----

    @Slot(bool)
    def setAnalyticsEnabled(self, enabled: bool) -> None:  # noqa: N802
        """Toggle anonymous usage analytics on or off."""
        self._config.set("telemetry.analytics_enabled", enabled)
        self._config.save_config()

    @Slot(bool)
    def setCrashReportsEnabled(self, enabled: bool) -> None:  # noqa: N802
        """Toggle crash report collection on or off."""
        self._config.set("telemetry.crash_reports_enabled", enabled)
        self._config.save_config()

    @Slot(result=bool)
    def isAnalyticsEnabled(self) -> bool:  # noqa: N802
        """Return whether usage analytics is currently enabled."""
        return bool(self._config.get("telemetry.analytics_enabled", False))

    @Slot(result=bool)
    def isCrashReportsEnabled(self) -> bool:  # noqa: N802
        """Return whether crash reporting is currently enabled."""
        return bool(self._config.get("telemetry.crash_reports_enabled", False))

    # ---- Updater ----

    @Slot()
    def checkForUpdates(self) -> None:  # noqa: N802
        """Manually trigger an update check."""
        try:
            from .updater import UpdateChecker
            checker: UpdateChecker = self.parent().findChild(UpdateChecker)
            if checker:
                checker.check()
        except Exception:
            pass

    @Slot()
    def openDownloadPage(self) -> None:  # noqa: N802
        """Open the download page for the latest version."""
        try:
            from .updater import UpdateChecker
            checker: UpdateChecker = self.parent().findChild(UpdateChecker)
            if checker:
                checker.open_download_page()
        except Exception:
            import webbrowser
            webbrowser.open("https://github.com/owenpkent/Nimbus-Adaptive-Controller/releases/latest")

    @Slot()
    def dismissUpdate(self) -> None:  # noqa: N802
        """Dismiss the current update notification."""
        try:
            from .updater import UpdateChecker
            checker: UpdateChecker = self.parent().findChild(UpdateChecker)
            if checker:
                checker.dismiss()
        except Exception:
            pass
