"""
QML <-> Python bridge for Nimbus Adaptive Controller.

This module exposes :class:`ControllerBridge`, the single ``QObject`` that
backs the QML user interface. The bridge fans QML interactions out to the
underlying subsystems:

* :class:`~src.vjoy_interface.VJoyInterface` — DirectInput virtual joystick
* :class:`~src.vigem_interface.ViGEmInterface` — XInput Xbox 360 emulation
* :class:`~src.uinput_interface.UInputXboxInterface` /
  :class:`~src.uinput_interface.UInputJoystickInterface` — Linux ``uinput``
  stand-ins for the two Windows drivers (same method names, chosen
  automatically on Linux)
* :mod:`~src.borderless` — borderless windowed mode + ClipCursor release
* :mod:`~src.mouse_hider` — controller-mode keep-alive (game voluntarily
  releases the mouse) with Win32 mouse hook and hotkey (Windows)
* :mod:`~src.controller_pulse` — the same keep-alive without the Win32
  pieces, used on every other platform
* :mod:`~src.mouse_isolation` — Linux ``EVIOCGRAB`` of the physical mouse;
  the bridge turns its deltas into a software cursor and synthetic Qt
  mouse events so the widgets keep working while games see no mouse
* :mod:`~src.window_utils` — ``WS_EX_NOACTIVATE`` "Game Focus" mode
* :class:`~src.config.ControllerConfig` — persistent settings + profiles
* :mod:`~src.mouse_isolation_win`: the Nimbus Mouse Filter driver plus the
  cursor relay (Full Game Mode takes the physical mouse away from the game
  while the real cursor keeps working)

Bridge responsibilities
-----------------------
* Translate QML method calls (``Slot``\\ s) into back-end operations.
* Shape every axis before it reaches a driver: the tremor filter, the
  precision modifier, the radial deadzone and response curve, the output
  anti-deadzone, the extremity cap, and per-widget inversion all happen
  here (:meth:`ControllerBridge.setStickInput`,
  :meth:`ControllerBridge.setAxisInput`). QML sends raw geometry only.
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

import json
import sys
import time
from typing import Any, Dict, Optional, Tuple

from PySide6.QtCore import QObject, Slot, Signal, Property, QTimer
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent, QWheelEvent
from PySide6.QtGui import QCursor, QWindow

from .config import (
    ControllerConfig,
    DEFAULT_ANTI_DEADZONE_BUFFER,
    DEFAULT_DEAD_ZONE_PCT,
    DEFAULT_EXTREMITY_PCT_AXIS,
    DEFAULT_EXTREMITY_PCT_STICK,
    DEFAULT_PRECISION_GAIN,
    DEFAULT_SENSITIVITY_PCT,
    DEFAULT_TREMOR_FILTER,
    XINPUT_LEFT_THUMB_DEADZONE,
    XINPUT_RIGHT_THUMB_DEADZONE,
    shape_magnitude,
    shape_vector,
)
from .vjoy_interface import VJoyInterface
from .controller_output import ControllerOutput
from .qt_dialogs import AxisMappingQt, JoystickSettingsQt, ButtonSettingsQt, SliderSettingsQt, AxisSettingsQt

# Try to import ViGEm for Xbox controller emulation (preferred for modern games)
try:
    from .vigem_interface import ViGEmInterface, VIGEM_AVAILABLE
except Exception:
    VIGEM_AVAILABLE = False
    ViGEmInterface = None

# Linux: kernel uinput virtual devices stand in for both Windows drivers
try:
    from .uinput_interface import (
        UInputXboxInterface,
        UInputJoystickInterface,
        UINPUT_AVAILABLE,
    )
except Exception:
    UINPUT_AVAILABLE = False
    UInputXboxInterface = None
    UInputJoystickInterface = None

# "Xbox gamepad" output exists on Windows (ViGEm) and Linux (uinput)
XBOX_OUTPUT_AVAILABLE = VIGEM_AVAILABLE or UINPUT_AVAILABLE

# Import window utilities for game focus mode (Windows only)
try:
    from .window_utils import (
        make_window_no_activate,
        remove_window_no_activate,
        get_qt_window_handle,
        is_no_activate_enabled,
        save_foreground_window,
        on_window_activated,
        set_foreground_window,
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

# Driver-agnostic keep-alive pulse: controller mode without the Win32 hooks
try:
    from . import controller_pulse as _controller_pulse
    CONTROLLER_PULSE_AVAILABLE = True
except Exception:
    CONTROLLER_PULSE_AVAILABLE = False
    _controller_pulse = None

# Windows routes controller mode through mouse_hider (pulse + mouse hook +
# ClipCursor release + emergency hotkey); every other platform uses the
# plain pulse against the active Xbox-style interface.
_USE_MOUSE_HIDER = sys.platform == "win32"

# Pixels of drag for full deflection when a joystick widget sets no
# travel_px of its own (QML then uses the drawn radius, which only QML
# knows). Used to read a normalised deflection back as pixels.
DEFAULT_TRAVEL_PX = 100.0

# Mouse isolation has two bridge-side implementations in this file. Windows
# drives the real cursor from the filter's packets (the cursor relay); every
# other platform draws its own cursor and synthesises Qt events. This flag
# picks one at each entry point, because defining the methods twice and
# letting the later definition win is how the Windows relay silently became
# dead code once already.
_ISO_CURSOR_RELAY = sys.platform == "win32"

# Mouse isolation: the physical mouse is taken away from every other
# application. Windows uses the Nimbus Mouse Filter driver plus the cursor
# relay (src/mouse_isolation_win.py), and its flag is True only when the
# driver's control device exists, so a machine without the driver keeps
# today's mouse_hider Game Mode. Linux uses an exclusive evdev grab plus a
# software cursor (src/mouse_isolation.py). The two MouseIsolation classes
# keep the same constructor, start/stop and callback signatures, so every
# call site below this import is platform-independent.
try:
    if sys.platform == "win32":
        from . import mouse_isolation_win as _mouse_isolation
    else:
        from . import mouse_isolation as _mouse_isolation
    MOUSE_ISOLATION_AVAILABLE = bool(_mouse_isolation and _mouse_isolation.MOUSE_ISOLATION_AVAILABLE)
except Exception:
    MOUSE_ISOLATION_AVAILABLE = False
    _mouse_isolation = None

# evdev button code -> Qt button, for clicks that land on Nimbus's own window.
# BTN_SIDE and BTN_EXTRA are here too: without them a side-button click over
# Nimbus fell through to SendInput and a foreground Raw Input game saw it,
# which is the leak the relay exists to close.
_ISO_BUTTON_MAP = {0x110: Qt.MouseButton.LeftButton, 0x111: Qt.MouseButton.RightButton,
                   0x112: Qt.MouseButton.MiddleButton, 0x113: Qt.MouseButton.BackButton,
                   0x114: Qt.MouseButton.ForwardButton}


class _IsolationRelay(QObject):
    """Marshals mouse-isolation reader-thread callbacks onto the Qt thread."""

    motion = Signal(int, int)
    button = Signal(int, bool)
    wheel = Signal(int, int)
    stopped = Signal(str)


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
        alwaysOnTopChanged(bool): Always-on-Top window pinning toggled.
    """

    mouseIsolationChanged = Signal(bool)  # Emits when the physical mouse is taken from, or given back to, the game
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
    mouseIsolationChanged = Signal(bool)  # Emits when the physical-mouse grab starts/stops
    alwaysOnTopChanged = Signal(bool)  # Emits when the window is pinned above other windows
    isolationCursorMoved = Signal(float, float)  # Software cursor position while isolated (window coords)
    accountStateChanged = Signal()
    privacyChanged = Signal()
    syncCompleted = Signal(bool)
    updateAvailable = Signal(str, str, str)
    forceUpdateRequired = Signal(str, str)
    noUpdateAvailable = Signal()
    checkFailed = Signal(str)

    def __init__(self, config: ControllerConfig, parent: Optional[QObject] = None,
                 *, output: Optional[ControllerOutput] = None, services: Any = None) -> None:
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
        self._services = services
        if services is not None:
            services.cloud.authStateChanged.connect(self._notify_account)
            services.cloud.userChanged.connect(self._notify_account)
            services.cloud.entitlementChanged.connect(self._notify_account)
            services.cloud.syncCompleted.connect(self._on_sync_completed)
            services.cloud.profileUpdated.connect(self._on_remote_profile_updated)
            services.updater.updateAvailable.connect(self.updateAvailable)
            services.updater.forceUpdateRequired.connect(self.forceUpdateRequired)
            services.updater.noUpdateAvailable.connect(self.noUpdateAvailable)
            services.updater.checkFailed.connect(self.checkFailed)
        self._window: Optional[QWindow] = None
        self._no_focus_mode = False
        self._always_on_top = False
        # Set while Full Game Mode pins the window itself, so stopping it
        # only unpins a window the user had not pinned deliberately.
        self._always_on_top_by_game_mode = False
        self._cursor_release_active = False
        self._borderless_game_hwnd: int = 0
        self._controller_mode_active = False
        # Mouse isolation. Windows: kernel filter plus cursor relay, where the
        # relay object turns reader-thread callbacks into queued signals and
        # the hwnds drive the per-window click policy. Linux: evdev grab plus
        # a software cursor, whose position is _iso_x/_iso_y.
        self._iso = None
        self._iso_active = False
        self._iso_x = 0.0
        self._iso_y = 0.0
        # The window _iso_x/_iso_y are measured in, so a modal dialog opening
        # can re-anchor them instead of letting the cursor jump.
        self._iso_target: Optional[QWindow] = None
        self._iso_game_hwnd = 0
        self._iso_nimbus_hwnd = 0
        self._iso_buttons = Qt.MouseButton.NoButton
        self._iso_last_press = None  # (monotonic time, button, x, y)
        self._iso_relay = _IsolationRelay(self)
        self._iso_relay.motion.connect(self._on_iso_motion, Qt.ConnectionType.QueuedConnection)
        self._iso_relay.button.connect(self._on_iso_button, Qt.ConnectionType.QueuedConnection)
        self._iso_relay.wheel.connect(self._on_iso_wheel, Qt.ConnectionType.QueuedConnection)
        self._iso_relay.stopped.connect(self._on_iso_stopped, Qt.ConnectionType.QueuedConnection)
        # Load recent profiles from persistent config (most-recent first)
        self._recent_profiles: list = list(self._config.get("ui.recent_profiles", []))
        
        # Determine which controller interface to use based on profile layout type
        # ViGEm (Xbox emulation) is preferred for xbox/adaptive profiles as it works with XInput games
        # vJoy is used for flight_sim profiles or as fallback
        # ControllerOutput owns backend construction; the bridge only decides
        # which factories it gets. That one choice is the whole of the
        # platform split for output: everything downstream asks _output.
        self._output = output if output is not None else ControllerOutput(
            config,
            UInputJoystickInterface if UINPUT_AVAILABLE else VJoyInterface,
            UInputXboxInterface if UINPUT_AVAILABLE else ViGEmInterface,
            XBOX_OUTPUT_AVAILABLE)
        
        self._init_controller_interface()
        
        self._scale = float(self._config.get("ui.scale_factor", 1.0))
        self._debug_borders = bool(self._config.get("ui.debug_borders", False))
        self._buttons_version = 0
        # Axis smoothing state: per-axis current and target in [-1,1]
        self._axis_state: dict[str, dict[str, float]] = {}
        # Per-widget shaping: the current profile's custom_layout widgets by
        # id, the tremor filter's EMA state, the last raw input (so a modifier
        # change can re-shape a held stick), and the held modifiers.
        self._widget_shaping: Dict[str, Dict[str, Any]] = {}
        self._ema: Dict[str, Tuple[float, float]] = {}
        self._last_raw: Dict[str, Tuple[float, float]] = {}
        self._modifiers: Dict[str, bool] = {}
        self._spectator = None   # Spectator+ primitive runner, created on first use (get_spectator)
        self._reload_widget_shaping()
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
        self._output.initialize()
        self._retire_inactive_interface()

    @property
    def _vjoy(self):
        return self._output.vjoy

    @_vjoy.setter
    def _vjoy(self, value):
        self._output.vjoy = value

    @property
    def _vigem(self):
        return self._output.vigem

    @_vigem.setter
    def _vigem(self, value):
        self._output.vigem = value

    @property
    def _use_vigem(self):
        return self._output.use_vigem

    @_use_vigem.setter
    def _use_vigem(self, value):
        self._output.use_vigem = value

    def _release_pulse_from(self, doomed: Any) -> None:
        """Never let the keep-alive pulse outlive the interface it writes to.

        Called before an interface is shut down. If the pulse is running
        against that interface it is moved to the surviving one, so switching
        output device does not silently end controller mode; if there is
        nothing usable to move to, the pulse is stopped instead of being left
        writing to a closed device.
        """
        if not (CONTROLLER_PULSE_AVAILABLE and _controller_pulse):
            return
        if _controller_pulse.active_interface() is not doomed:
            return
        survivor = self._vigem if self._use_vigem else self._vjoy
        if survivor is not None and getattr(survivor, "is_connected", False):
            if _controller_pulse.rebind_interface(survivor):
                return
        print("[bridge] Output device changed with no usable pad; stopping controller mode")
        self._stop_pulse_only()

    def _retire_inactive_interface(self) -> None:
        """On Linux, destroy whichever uinput device is not the active output.

        Windows keeps both drivers attached (vJoy as a fallback for ViGEm),
        which is harmless there. On Linux each back end is a separate virtual
        gamepad that games can see, so only the active one is kept alive; the
        other is re-created on demand by the factory ``ControllerOutput`` holds.
        """
        if not UINPUT_AVAILABLE:
            return
        doomed = self._vjoy if self._use_vigem else self._vigem
        if doomed is None:
            return
        # Order matters: the pulse runs on its own thread and must stop
        # touching this interface before it is shut down.
        self._release_pulse_from(doomed)
        try:
            doomed.shutdown()
        except Exception:
            pass
        if self._use_vigem:
            self._vjoy = None
        else:
            self._vigem = None
    def _is_controller_connected(self) -> bool:
        """Check if the active controller interface is connected."""
        return self._output.connected
    
    def _get_active_interface(self):
        """Get the currently active controller interface."""
        return self._output.active

    # ----- Spectator+ -----
    @Slot(result=QObject)
    def get_spectator(self):
        """The Spectator+ primitive runner bound to this bridge's output.

        Created on first use. Its primitives (turn by an angle, walk for a
        distance, press a button) write through :meth:`setAxis` and
        :meth:`setButton`, so they go through the active driver interface
        and its limits, and bypass the widget shaping on purpose: that
        shaping is for the user's own hand, and a turn of 90 degrees has to
        be the same turn whatever curve the user has. A profile switch or
        Ctrl+Alt+F12 stops a running primitive. See ``src/spectator``.

        Returns:
            The :class:`~src.spectator.primitives.PrimitiveRunner`.
        """
        if self._spectator is None:
            from .spectator.primitives import PrimitiveRunner
            self._spectator = PrimitiveRunner(self.setAxis, self.setButton, parent=self)
        return self._spectator

    def _stop_spectator(self) -> None:
        """Cancel a running primitive, if any, and zero what it touched."""
        if self._spectator is not None:
            try:
                self._spectator.stop()
            except Exception:
                pass

    def _spectator_user_stick(self, widget_id: str, nx: float, ny: float) -> None:
        """Hand control back when the user moves their own stick under a closed loop.

        Section 5's fail-safe rule: any user stick motion above a threshold
        cancels a running snap. This runs before the stick is driven, so the
        primitive's axes are released first and what the driver is left
        holding is the user's own vector rather than the loop's last command.
        The widget's ``travel_px`` turns the normalised deflection back into
        the pixels the threshold is written in.
        """
        runner = self._spectator
        if runner is None or not getattr(runner, "watching_user", False):
            return
        try:
            w = self._widget_shaping.get(str(widget_id)) or {}
            travel = float(w.get("travel_px") or 0.0) or DEFAULT_TRAVEL_PX
            runner.note_user_stick(float(nx) * travel, float(ny) * travel)
        except Exception:
            pass

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

    @staticmethod
    def _x11_session() -> bool:
        """True when Qt is running on the xcb (X11) platform plugin."""
        try:
            return QGuiApplication.platformName() == "xcb"
        except Exception:
            return False

    def _apply_no_focus_flag(self, enabled: bool) -> bool:
        """Toggle Qt's ``WindowDoesNotAcceptFocus`` on the main window.

        On X11 a window with this flag still receives pointer input but never
        takes keyboard focus, which is the Linux counterpart of the Windows
        ``WS_EX_NOACTIVATE`` Game Focus Mode. As in
        :meth:`_apply_always_on_top`, the geometry is restored afterwards
        because changing a flag can make the xcb plugin recreate the native
        window.

        Returns:
            True if the flag was applied.
        """
        if self._window is None:
            return False
        try:
            geom = self._window.geometry()
            was_visible = self._window.isVisible()
            self._window.setFlag(Qt.WindowDoesNotAcceptFocus, bool(enabled))
            if self._window.geometry() != geom:
                self._window.setGeometry(geom)
            if was_visible and not self._window.isVisible():
                self._window.show()
            return True
        except Exception as e:
            print(f"No-focus flag failed: {e}")
            return False
    
    def _set_no_focus_mode(self, enabled: bool) -> None:
        if self._no_focus_mode == enabled:
            return
        
        if sys.platform != "win32":
            if self._window is None:
                print("No-focus mode: window not set yet")
                return
            if self._apply_no_focus_flag(enabled):
                self._no_focus_mode = bool(enabled)
                self._config.set("ui.no_focus_mode", self._no_focus_mode)
                self._config.save_config()
                self.noFocusModeChanged.emit(self._no_focus_mode)
                print(f"No-focus mode {'ENABLED' if enabled else 'DISABLED'} (Qt WindowDoesNotAcceptFocus)")
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

    # ------------------------------------------------------------------
    # Always on Top
    # ------------------------------------------------------------------

    def _apply_always_on_top(self, enabled: bool) -> bool:
        """Pin or unpin the main window above other windows.

        Qt's ``WindowStaysOnTopHint`` maps to ``WS_EX_TOPMOST`` on Windows and
        to ``_NET_WM_STATE_ABOVE`` on X11, which is what keeps the panel
        visible over a fullscreen game. Changing a flag can make the xcb
        plugin recreate the native window, so the geometry is captured first
        and restored afterwards and the window is re-shown and raised.

        Args:
            enabled: True to pin the window above others.

        Returns:
            True if the flag was applied.
        """
        if self._window is None:
            return False
        try:
            geom = self._window.geometry()
            was_visible = self._window.isVisible()
            self._window.setFlag(Qt.WindowStaysOnTopHint, bool(enabled))
            # A recreated native window can come back at the wrong place or
            # hidden; put it back exactly where the user had it.
            if self._window.geometry() != geom:
                self._window.setGeometry(geom)
            if was_visible and not self._window.isVisible():
                self._window.show()
            if enabled and was_visible:
                self._window.raise_()
            return True
        except Exception as e:
            print(f"Always-on-top flag failed: {e}")
            return False

    def _get_always_on_top(self) -> bool:
        return bool(self._always_on_top)

    def _set_always_on_top(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._always_on_top == enabled:
            return
        if self._window is None:
            print("Always on Top: window not set yet")
            return
        if not self._apply_always_on_top(enabled):
            return
        self._always_on_top = enabled
        # An explicit toggle is the user's choice, so Game Mode must not undo it.
        self._always_on_top_by_game_mode = False
        self._config.set("ui.always_on_top", enabled)
        self._config.save_config()
        self.alwaysOnTopChanged.emit(enabled)
        print(f"Always on Top {'ENABLED' if enabled else 'DISABLED'}")

    alwaysOnTop = Property(bool, _get_always_on_top, _set_always_on_top, notify=alwaysOnTopChanged)

    @Slot(result=bool)
    def isAlwaysOnTopAvailable(self) -> bool:  # noqa: N802
        """Check whether the window can be pinned above others on this platform.

        Windows and X11 both honour ``WindowStaysOnTopHint``. Wayland has no
        client-settable "above" state in xdg-shell, so the compositor decides
        and the toggle is reported unavailable there.
        """
        if sys.platform == "win32":
            return True
        return self._x11_session()

    def _game_mode_pin_window(self) -> None:
        """Pin the window above the game for the duration of Full Game Mode.

        A fullscreen game covers the panel otherwise, which on Linux also
        hides the software cursor and the Game Mode button used to stop.
        Session-only: not written to config, so it resets on restart.
        """
        if self._always_on_top or self._window is None:
            return
        if not self.isAlwaysOnTopAvailable():
            return
        if self._apply_always_on_top(True):
            self._always_on_top = True
            self._always_on_top_by_game_mode = True
            self.alwaysOnTopChanged.emit(True)
            print("[bridge] Full Game Mode: window pinned above the game (session-only)")

    def _game_mode_unpin_window(self) -> None:
        """Undo :meth:`_game_mode_pin_window`, leaving a user-set pin alone."""
        if not self._always_on_top_by_game_mode or self._window is None:
            return
        if self._apply_always_on_top(False):
            self._always_on_top = False
            self._always_on_top_by_game_mode = False
            self.alwaysOnTopChanged.emit(False)
            print("[bridge] Full Game Mode: window unpinned")

    @Slot(QWindow)
    def setWindow(self, window: QWindow) -> None:  # noqa: N802
        """Set the window reference for no-focus mode. Called from QML after window is ready."""
        self._window = window
        # Only restore no-focus mode if user explicitly enabled it (not from game mode auto-enable)
        if self._config.get("ui.no_focus_mode_user", False):
            self._set_no_focus_mode(True)
        # Always on Top is a plain user preference, so restore it as saved.
        if self._config.get("ui.always_on_top", False) and self.isAlwaysOnTopAvailable():
            if self._apply_always_on_top(True):
                self._always_on_top = True
                self.alwaysOnTopChanged.emit(True)
    
    @Slot(result=bool)
    def isNoFocusModeAvailable(self) -> bool:  # noqa: N802
        """Check if no-focus mode is available on this platform.

        Windows uses ``WS_EX_NOACTIVATE`` (window_utils); X11 uses Qt's
        ``WindowDoesNotAcceptFocus`` flag. Wayland compositors own focus
        policy, so the mode is reported unavailable there.
        """
        if sys.platform == "win32":
            return WINDOW_UTILS_AVAILABLE
        return self._x11_session()
    
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
        # While the physical mouse is grabbed the real pointer is frozen, so
        # cursor warps (joystick lock mode) move the software cursor instead.
        if self._iso_active and self._window is not None:
            local = self._window.mapFromGlobal(QPoint(int(screen_x), int(screen_y)))
            self._iso_set_cursor(local.x(), local.y())
            self._iso_send_mouse(QEvent.Type.MouseMove, Qt.MouseButton.NoButton)
            return
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

    # ----- Stick and axis shaping -----
    # Every axis value the QML sends is raw geometry. The bridge resolves the
    # widget's settings, shapes the value, and forwards it to the driver.

    def _reload_widget_shaping(self, widgets: Optional[list] = None) -> None:
        """Rebuild the per-widget settings cache from the current profile.

        Args:
            widgets: The widget list to cache. When omitted it is read from
                the current profile's ``custom_layout``.
        """
        try:
            if widgets is None:
                profile = self._config.get_current_profile_data() or {}
                widgets = (profile.get("custom_layout") or {}).get("widgets", []) or []
            cache: Dict[str, Dict[str, Any]] = {}
            for w in widgets:
                if isinstance(w, dict) and w.get("id"):
                    cache[str(w["id"])] = w
            self._widget_shaping = cache
        except Exception:
            self._widget_shaping = {}
        self._ema.clear()
        self._last_raw.clear()
        # Modifiers belong to the layout that defined the buttons. QML recreates
        # the widget delegates with their toggle visuals reset, so a latched
        # precision left set here would silently slow every stick in a profile
        # that has no button to unlatch it.
        self._modifiers.clear()

    def _default_anti_deadzone(self, axis: str) -> float:
        """The output anti-deadzone a widget gets when it sets none.

        The documented XInput stick deadzones when the output is ViGEm, zero
        for vJoy (DirectInput games vary too much to guess) and for
        trigger axes.
        """
        if not self._use_vigem:
            return 0.0
        a = str(axis or "").lower()
        if a in ("x", "y"):
            return XINPUT_LEFT_THUMB_DEADZONE
        if a in ("rx", "ry"):
            return XINPUT_RIGHT_THUMB_DEADZONE
        return 0.0

    def _widget_params(self, w: Dict[str, Any]) -> Dict[str, float]:
        """Resolve a widget's shaping parameters, filling in the defaults."""
        wtype = str(w.get("type", "joystick"))
        mapping = w.get("mapping") or {}
        if wtype == "joystick":
            axis = str(mapping.get("axis_x") or "")
            ext_default = DEFAULT_EXTREMITY_PCT_STICK
        else:
            axis = str(mapping.get("axis") or "")
            ext_default = DEFAULT_EXTREMITY_PCT_AXIS
        return {
            "sensitivity": float(w.get("sensitivity", DEFAULT_SENSITIVITY_PCT)),
            "dead_zone": float(w.get("dead_zone", DEFAULT_DEAD_ZONE_PCT)),
            "extremity_dead_zone": float(w.get("extremity_dead_zone", ext_default)),
            "anti_deadzone": float(w.get("anti_deadzone", self._default_anti_deadzone(axis))),
            "anti_deadzone_buffer": float(w.get("anti_deadzone_buffer", DEFAULT_ANTI_DEADZONE_BUFFER)),
        }

    @staticmethod
    def _params_from_json(params_json: str) -> Dict[str, float]:
        """Shaping parameters from the config dialog's unsaved slider values."""
        raw = json.loads(params_json) if params_json else {}
        out: Dict[str, float] = {}
        for key, default in (("sensitivity", DEFAULT_SENSITIVITY_PCT),
                             ("dead_zone", DEFAULT_DEAD_ZONE_PCT),
                             ("extremity_dead_zone", DEFAULT_EXTREMITY_PCT_STICK),
                             ("anti_deadzone", 0.0),
                             ("anti_deadzone_buffer", DEFAULT_ANTI_DEADZONE_BUFFER)):
            try:
                out[key] = float(raw.get(key, default))
            except (TypeError, ValueError):
                out[key] = float(default)
        return out

    def _filter_tremor(self, key: str, tremor_filter: float, nx: float, ny: float) -> Tuple[float, float]:
        """Apply the per-widget EMA tremor filter to a raw input vector.

        An input of exactly (0, 0) is a release: the filter state is dropped
        and the output snaps to centre, so a heavily filtered stick can never
        be left holding a residual deflection after the pointer lets go.
        """
        if nx == 0.0 and ny == 0.0:
            self._ema.pop(key, None)
            return 0.0, 0.0
        tf = max(0.0, min(10.0, float(tremor_filter)))
        if tf <= 0.0:
            self._ema.pop(key, None)
            return nx, ny
        alpha = 1.0 - (tf / 10.0) * 0.9   # 1.0 = no smoothing, 0.1 = heavy
        sx, sy = self._ema.get(key, (0.0, 0.0))
        fx = sx + (nx - sx) * alpha
        fy = sy + (ny - sy) * alpha
        self._ema[key] = (fx, fy)
        return fx, fy

    def _precision_gain(self, w: Dict[str, Any]) -> float:
        """Gain to apply right now: the widget's precision gain while the modifier is held."""
        if not self._modifiers.get("precision"):
            return 1.0
        try:
            return max(0.0, min(1.0, float(w.get("precision_gain", DEFAULT_PRECISION_GAIN))))
        except (TypeError, ValueError):
            return DEFAULT_PRECISION_GAIN

    def _dispatch_stick(self, w: Dict[str, Any], ox: float, oy: float) -> None:
        """Send a shaped stick vector to the axes a joystick widget maps."""
        if not self._is_controller_connected():
            return
        mapping = w.get("mapping") or {}
        ax = str(mapping.get("axis_x") or "none").lower()
        ay = str(mapping.get("axis_y") or "none").lower()
        if self._use_vigem and self._vigem:
            if (ax, ay) == ("x", "y"):
                self._vigem.set_left_stick(ox, oy)
                return
            if (ax, ay) == ("rx", "ry"):
                self._vigem.set_right_stick(ox, oy)
                return
            if ax != "none":
                self._vigem.update_axis(ax, ox)
            if ay != "none":
                self._vigem.update_axis(ay, oy)
        else:
            if ax != "none":
                self._set_axis_target(ax, ox)
            if ay != "none":
                self._set_axis_target(ay, oy)

    def _drive_stick(self, widget_id: str, w: Dict[str, Any], nx: float, ny: float,
                     advance_filter: bool = True) -> Tuple[float, float]:
        """Shape a raw stick vector for a widget and send it to the driver.

        Returns the shaped vector in the widget's screen orientation (before
        the y-axis flip and inversion), which is what the UI displays.

        Args:
            advance_filter: When False the tremor filter's state is reused
                rather than stepped. A modifier change is not a new pointer
                sample, so stepping the EMA there would move the stick as well
                as rescale it.
        """
        if advance_filter:
            fx, fy = self._filter_tremor(widget_id, float(w.get("tremor_filter", DEFAULT_TREMOR_FILTER)), nx, ny)
        else:
            fx, fy = self._ema.get(widget_id, (nx, ny))
        ox, oy = shape_vector(fx, fy, gain=self._precision_gain(w), **self._widget_params(w))
        # ny is screen-down positive; controller Y is up positive, so flip,
        # then apply the widget's own inversion on top.
        out_x = -ox if w.get("invert_x") else ox
        out_y = oy if w.get("invert_y") else -oy
        self._dispatch_stick(w, out_x, out_y)
        return ox, oy

    @Slot(str, float, float)
    def setStickInput(self, widget_id: str, nx: float, ny: float) -> None:  # noqa: N802
        """Raw deflection from a custom-layout joystick widget.

        Args:
            widget_id: The widget's profile id; selects its settings and mapping.
            nx: Normalised deflection, right positive, before any shaping.
            ny: Normalised deflection, screen-down positive, before any shaping.
                (0, 0) is a release and always centres the output.
        """
        try:
            w = self._widget_shaping.get(str(widget_id))
            if w is None:
                return
            raw = (float(nx), float(ny))
            if raw == (0.0, 0.0):
                self._last_raw.pop(str(widget_id), None)
            else:
                self._last_raw[str(widget_id)] = raw
            self._spectator_user_stick(str(widget_id), raw[0], raw[1])
            self._drive_stick(str(widget_id), w, raw[0], raw[1])
        except Exception:
            pass

    @Slot(str, float)
    def setAxisInput(self, widget_id: str, value: float) -> None:  # noqa: N802
        """Raw value from a custom-layout slider or wheel widget.

        A slider that holds or returns to zero is a unipolar control, so
        ``value`` is 0 to 1 and is shaped from its bottom end: triggers (z
        and rz under ViGEm) take the shaped value directly, any other axis
        gets it spread over the full range. A centre-sprung slider and a
        wheel are bipolar, so ``value`` is -1 to 1 and is shaped around the
        centre.
        """
        try:
            w = self._widget_shaping.get(str(widget_id))
            if w is None:
                return
            mapping = w.get("mapping") or {}
            axis = str(mapping.get("axis") or "none").lower()
            if axis == "none":
                return
            params = self._widget_params(w)
            gain = self._precision_gain(w) if w.get("type") == "wheel" else 1.0
            unipolar = w.get("type") == "slider" and str(w.get("snap_mode", "none")) != "center"
            v = float(value)
            if unipolar:
                fv, _ = self._filter_tremor(str(widget_id), float(w.get("tremor_filter", DEFAULT_TREMOR_FILTER)), v, 0.0)
                m = shape_magnitude(max(0.0, min(1.0, fv)), gain=gain, **params)
                if not self._is_controller_connected():
                    return
                if self._use_vigem and self._vigem and axis in ("z", "rz"):
                    if axis == "z":
                        self._vigem.set_left_trigger(m)
                    else:
                        self._vigem.set_right_trigger(m)
                    return
                out = m * 2.0 - 1.0
            else:
                fv, _ = self._filter_tremor(str(widget_id), float(w.get("tremor_filter", DEFAULT_TREMOR_FILTER)), v, 0.0)
                m = shape_magnitude(abs(fv), gain=gain, **params)
                out = m if fv >= 0 else -m
            iface = self._get_active_interface()
            if iface:
                iface.update_axis(axis, out)
        except Exception:
            pass

    @Slot(str, bool)
    def setModifier(self, name: str, active: bool) -> None:  # noqa: N802
        """Hold or release a modifier such as ``"precision"``.

        A held stick is re-shaped immediately so the change is felt without
        waiting for the next pointer event.
        """
        try:
            key = str(name).lower()
            active = bool(active)
            if self._modifiers.get(key, False) == active:
                return
            self._modifiers[key] = active
            for widget_id, (nx, ny) in list(self._last_raw.items()):
                w = self._widget_shaping.get(widget_id)
                if w is not None:
                    # No new pointer sample arrived, so re-shape the filtered
                    # vector the stick is already holding instead of feeding
                    # the raw one through the EMA again. Otherwise toggling the
                    # modifier walks a filtered stick toward its raw position,
                    # changing where it points and not just how far.
                    self._drive_stick(widget_id, w, nx, ny, advance_filter=False)
        except Exception:
            pass

    @Slot(str, result=bool)
    def isModifierActive(self, name: str) -> bool:  # noqa: N802
        """Whether a modifier is currently held or latched."""
        return bool(self._modifiers.get(str(name).lower(), False))

    @Slot(str, result=float)
    def defaultAntiDeadzone(self, axis: str) -> float:  # noqa: N802
        """The anti-deadzone default for an axis under the current output mode."""
        return float(self._default_anti_deadzone(axis))

    @Slot(str, result="QVariantList")
    def shapeCurve(self, params_json: str) -> list:  # noqa: N802
        """Output magnitudes for inputs 0.00 to 1.00 in steps of 0.01.

        The config dialog's curve preview draws these so the preview and the
        runtime share one formula.

        Args:
            params_json: JSON object with any of ``sensitivity``,
                ``dead_zone``, ``extremity_dead_zone`` (percent),
                ``anti_deadzone``, ``anti_deadzone_buffer`` (fractions).
        """
        try:
            params = self._params_from_json(params_json)
            return [float(shape_magnitude(i / 100.0, **params)) for i in range(101)]
        except Exception:
            return [i / 100.0 for i in range(101)]

    @Slot(str, float, float, str, bool, result="QVariantList")
    def previewStick(self, widget_id: str, nx: float, ny: float, params_json: str, drive: bool) -> list:  # noqa: N802
        """Shape a test vector with unsaved dialog settings, optionally driving the output.

        Lets the user calibrate the anti-deadzone against a running game: the
        dialog's test pad sends its deflection here, shows the numbers that
        come back, and, when ``drive`` is set, the shaped vector also goes to
        the widget's mapped axes. No tremor filter or modifier is applied.

        Returns:
            ``[out_x, out_y, magnitude]`` in the widget's screen orientation.
        """
        try:
            params = self._params_from_json(params_json)
            ox, oy = shape_vector(float(nx), float(ny), **params)
            if drive:
                w = self._widget_shaping.get(str(widget_id))
                if w is not None:
                    out_x = -ox if w.get("invert_x") else ox
                    out_y = oy if w.get("invert_y") else -oy
                    self._dispatch_stick(w, out_x, out_y)
            return [float(ox), float(oy), float((ox * ox + oy * oy) ** 0.5)]
        except Exception:
            return [0.0, 0.0, 0.0]

    # Legacy layouts (adaptive, xbox, flight_sim): sticks without per-widget
    # settings, shaped with the profile's global joystick_settings.
    @Slot(float, float)
    def setLeftStick(self, x: float, y: float) -> None:  # noqa: N802
        try:
            gain = 1.0
            if self._modifiers.get("precision"):
                gain = float(self._config.get("joystick_settings.precision_gain", DEFAULT_PRECISION_GAIN))
            px, py = self._config.shape_stick(float(x), float(y), 'left', self.getOutputMode(), gain)
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
            gain = 1.0
            if self._modifiers.get("precision"):
                gain = float(self._config.get("joystick_settings.precision_gain", DEFAULT_PRECISION_GAIN))
            px, py = self._config.shape_stick(float(x), float(y), 'right', self.getOutputMode(), gain)
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
            return "Xbox 360 (uinput)" if UINPUT_AVAILABLE else "Xbox 360 (ViGEm)"
        return "Joystick (uinput)" if UINPUT_AVAILABLE else "vJoy (DirectInput)"

    @Slot(result=str)
    def getOutputMode(self) -> str:  # noqa: N802
        """Get current output mode: 'vigem' or 'vjoy'.

        The mode names are kept platform-neutral for profiles and QML: on
        Linux ``'vigem'`` means the uinput Xbox 360 pad and ``'vjoy'`` the
        uinput generic joystick.
        """
        return "vigem" if self._use_vigem else "vjoy"

    @Slot(str, result=str)
    def getOutputModeLabel(self, mode: str) -> str:  # noqa: N802
        """Human-readable menu label for an output mode on this platform.

        Args:
            mode: ``'vjoy'`` or ``'vigem'``.
        """
        mode = str(mode).lower().strip()
        if UINPUT_AVAILABLE:
            return "Xbox 360 gamepad (uinput)" if mode == "vigem" else "Generic joystick (uinput)"
        return "ViGEm Xbox 360 (XInput)" if mode == "vigem" else "vJoy (DirectInput)"

    @Slot(result=bool)
    def isVigemAvailable(self) -> bool:  # noqa: N802
        """Whether Xbox-style gamepad output is available.

        A virtual gamepad bus (ViGEmBus) on Windows, uinput on Linux; which one
        is decided by the factory ``ControllerOutput`` was built with.
        """
        return self._output.vigem_available

    @Slot(str)
    def setOutputMode(self, mode: str) -> None:  # noqa: N802
        """Switch output device. mode is 'vjoy' or 'vigem'."""
        if not self._output.select(mode):
            return
        mode = self._output.mode
        self._retire_inactive_interface()
        self._config.set("controller.prefer_vigem", self._output.use_vigem)
        self._config.save_config()
        self.outputModeChanged.emit(mode)
        self.vjoyConnectionChanged.emit(self._is_controller_connected())
        print(f"Output mode switched to: {self.getControllerType()}")

    # ----- Borderless gaming -----
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

    @Slot(int, int)
    def startCursorReleaseWithHwnd(self, interval_ms: int, game_hwnd: int) -> None:  # noqa: N802
        """Start cursor release with game HWND for thread-attached release."""
        if not BORDERLESS_AVAILABLE:
            return
        try:
            _borderless.start_cursor_release(interval_ms, game_hwnd=game_hwnd)
        except Exception as e:
            print(f"[bridge] startCursorReleaseWithHwnd error: {e}")

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
        self._stop_spectator()
        success = self._config.switch_profile(profile_id)
        if success:
            self._refresh_active_profile()
            # Track recently used (keep last 5, most-recent first, no duplicates)
            if profile_id in self._recent_profiles:
                self._recent_profiles.remove(profile_id)
            self._recent_profiles.insert(0, profile_id)
            self._recent_profiles = self._recent_profiles[:5]
            self._config.set("ui.recent_profiles", self._recent_profiles)
            self._config.save_config()
            self.recentProfilesChanged.emit()
        return success

    def _refresh_active_profile(self) -> None:
        self._reload_widget_shaping()
        self.profileChanged.emit(self._config.get_current_profile())
        self.layoutTypeChanged.emit(self._config.get_layout_type())
        self._buttons_version += 1
        self.buttonsVersionChanged.emit(self._buttons_version)

    @Slot(result="QVariantList")
    def getRecentProfiles(self) -> list:  # noqa: N802
        """Get recently used profile IDs (most-recent first, up to 5)."""
        all_profiles = {p["id"]: p for p in self._config.get_available_profiles()}
        return [all_profiles[pid] for pid in self._recent_profiles if pid in all_profiles]

    @Slot(result=bool)
    def isBundledProfile(self) -> bool:  # noqa: N802
        """Return True if the current profile is a built-in (bundled) profile."""
        return self._config.is_builtin_profile(self._config.get_current_profile())

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
            self._refresh_active_profile()
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
        previous = self._config.get_current_profile()
        success = self._config.delete_profile(profile_id)
        if success:
            if previous != self._config.get_current_profile():
                self._refresh_active_profile()
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
            widgets = json.loads(widgets_json)
            if self._config.save_custom_layout(widgets, int(grid_snap), bool(show_grid)):
                self._reload_widget_shaping(widgets)
            else:
                self.profileSaved.emit(False)
        except Exception as e:
            print(f"Error saving custom layout: {e}")
            self.profileSaved.emit(False)

    @Slot(str, str, int, bool)
    def saveCustomLayoutAs(self, name: str, widgets_json: str, grid_snap: int, show_grid: bool) -> None:  # noqa: N802
        """Save custom layout as a new profile with a custom name."""
        try:
            import copy
            widgets = json.loads(widgets_json)
            # Duplicate current profile with new name
            profile_data = copy.deepcopy(self._config.get_current_profile_data() or {})
            profile_id = self._config.profiles.unique_id(name)
            profile_data["name"] = name
            profile_data["description"] = f"Custom layout: {name}"
            profile_data["layout_type"] = "custom"
            if "custom_layout" not in profile_data:
                profile_data["custom_layout"] = {}
            profile_data["custom_layout"]["widgets"] = widgets
            profile_data["custom_layout"]["grid_snap"] = int(grid_snap)
            profile_data["custom_layout"]["show_grid"] = bool(show_grid)
            # Save as new profile
            success = self._config.save_profile_as(profile_id, profile_data)
            if success:
                # unique_id never overwrites, so a second "Save As" under a name
                # that already exists lands on name_1 rather than replacing it.
                # Say which id was used: silently writing somewhere other than
                # where the user expected is worse than the old clobber.
                if profile_id != name:
                    print(f"Saved custom layout as: {name} (id: {profile_id})")
                self.profilesListChanged.emit()
            self.profileSaved.emit(success)
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
        backend_ok = MOUSE_HIDER_AVAILABLE if _USE_MOUSE_HIDER else CONTROLLER_PULSE_AVAILABLE
        return (backend_ok and self._use_vigem and
                self._vigem is not None and self._vigem.is_connected)

    def _start_pulse_only(self, pulse_hz: int) -> bool:
        """Start controller mode through the driver-agnostic pulse (non-Windows)."""
        if not CONTROLLER_PULSE_AVAILABLE or not _controller_pulse:
            print("[bridge] controller_pulse module not available")
            return False

        def _on_change(active: bool):
            self._controller_mode_active = active
            self.controllerModeChanged.emit(active)

        return _controller_pulse.start_controller_mode(
            self._vigem, pulse_hz=max(5, min(120, int(pulse_hz))), callback=_on_change)

    def _stop_pulse_only(self) -> None:
        """Stop the driver-agnostic pulse and report the state change."""
        if CONTROLLER_PULSE_AVAILABLE and _controller_pulse:
            try:
                _controller_pulse.stop_controller_mode()
            except Exception as e:
                print(f"[bridge] stop pulse failed: {e}")
        self._controller_mode_active = False
        self.controllerModeChanged.emit(False)

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
        if not self._use_vigem or not self._vigem or not self._vigem.gamepad:
            print("[bridge] Xbox output not active: controller mode requires Xbox emulation")
            return False
        if not _USE_MOUSE_HIDER:
            return self._start_pulse_only(pulse_hz)
        if not MOUSE_HIDER_AVAILABLE or not _mouse_hider:
            print("[bridge] mouse_hider module not available")
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
        self._stop_spectator()   # Ctrl+Alt+F12 also cancels a running primitive
        if not _USE_MOUSE_HIDER:
            self._stop_pulse_only()
            return
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
        if not _USE_MOUSE_HIDER:
            return bool(CONTROLLER_PULSE_AVAILABLE and _controller_pulse
                        and _controller_pulse.is_controller_mode_active())
        if not MOUSE_HIDER_AVAILABLE or not _mouse_hider:
            return False
        return _mouse_hider.is_controller_mode_active()

    @Slot()
    def sendControllerBurst(self) -> None:  # noqa: N802
        """Send a one-shot burst of controller signals.
        
        Forces the game into controller mode without enabling the
        continuous keep-alive. Useful as a quick fix or test.
        """
        if not self._use_vigem or not self._vigem or not self._vigem.gamepad:
            print("[bridge] Xbox output not active for controller burst")
            return
        if not _USE_MOUSE_HIDER:
            if CONTROLLER_PULSE_AVAILABLE and _controller_pulse:
                _controller_pulse.send_controller_burst(self._vigem)
            return
        if not MOUSE_HIDER_AVAILABLE or not _mouse_hider:
            return
        try:
            _mouse_hider.send_controller_burst(self._vigem.gamepad)
        except Exception as e:
            print(f"[bridge] sendControllerBurst failed: {e}")

    @Slot(result=str)
    def getControllerModeStats(self) -> str:  # noqa: N802
        """Get controller mode statistics as JSON string."""
        import json
        if not _USE_MOUSE_HIDER:
            if CONTROLLER_PULSE_AVAILABLE and _controller_pulse:
                return json.dumps(_controller_pulse.get_controller_mode_stats())
            return "{}"
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
          4. Mouse isolation (Windows with the filter driver installed: the
             game stops seeing the physical mouse, the real cursor keeps
             working through the cursor relay, never over the game window)
          5. Bring the game to the foreground, so its gamepad counts and the
             user need not click it first
        
        Args:
            game_hwnd: HWND of the game window.
            pulse_hz: Controller keep-alive frequency.
        
        Returns:
            True if at least one mechanism started successfully.
        """
        success = False
        
        # Step 0: Enable Game Focus Mode so clicking Nimbus doesn't steal focus
        # NOTE: This is session-only — NOT saved to config, so it resets on restart.
        if sys.platform != "win32" and self._window and not self._no_focus_mode:
            if self._x11_session() and self._apply_no_focus_flag(True):
                self._no_focus_mode = True
                self.noFocusModeChanged.emit(True)
                print("[bridge] Full Game Mode: no-focus flag enabled (session-only)")
        if WINDOW_UTILS_AVAILABLE and self._window and not self._no_focus_mode:
            try:
                hwnd = get_qt_window_handle(self._window)
                if make_window_no_activate(hwnd):
                    self._no_focus_mode = True
                    self.noFocusModeChanged.emit(True)
                    print("[bridge] Full Game Mode: Game Focus Mode auto-enabled (session-only)")
            except Exception as e:
                print(f"[bridge] Full Game Mode: focus mode failed ({e}), continuing...")

        # Step 0b: Pin above the game. A fullscreen game hides the panel
        # otherwise, and on Linux that also hides the software cursor and the
        # button used to stop.
        self._game_mode_pin_window()
        
        # Step 1: ClipCursor release (fights mouse confinement) - Win32 only
        if BORDERLESS_AVAILABLE and sys.platform == "win32":
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
        elif self._output.vigem_available:
            # Create the Xbox-style pad on demand for Game Mode
            try:
                print("[bridge] Full Game Mode: profile doesn't use ViGEm, creating one for Game Mode...")
                self._output.ensure_vigem()
                if self._vigem and self._vigem.is_connected and self._vigem.gamepad:
                    gamepad = self._vigem.gamepad
                    print("[bridge] Full Game Mode: on-demand ViGEm gamepad created!")
                else:
                    print("[bridge] Full Game Mode: ViGEm gamepad creation failed")
                    print("[bridge]   -> Is the ViGEmBus driver installed and started?")
            except Exception as e:
                print(f"[bridge] Full Game Mode: ViGEm init error: {e}")
        else:
            print("[bridge] Full Game Mode: ViGEm NOT available")
            print("[bridge]   -> ViGEmBus driver not installed or not started")
            print("[bridge]   -> Controller mode enforcement requires ViGEm")
            print("[bridge]   -> The Nimbus installer includes ViGEmBus")
        
        if not _USE_MOUSE_HIDER and gamepad:
            if self._start_pulse_only(pulse_hz):
                success = True
                print("[bridge] Full Game Mode: controller pulse STARTED")
        # Step 3 (Linux): take the physical mouse away from the game entirely
        if MOUSE_ISOLATION_AVAILABLE and bool(self._config.get("controller.game_mode_isolate_mouse", True)):
            if self.startMouseIsolation():
                success = True
                print("[bridge] Full Game Mode: mouse isolation STARTED")
            else:
                print("[bridge] Full Game Mode: mouse isolation unavailable, continuing without it")
        elif MOUSE_HIDER_AVAILABLE and gamepad:
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
        
        # Step 3: take the physical mouse away from the game entirely (kernel
        # filter + cursor relay). Independent of controller mode: the hook in
        # mouse_hider still covers games that read the cursor position.
        if MOUSE_ISOLATION_AVAILABLE and bool(self._config.get("controller.game_mode_isolate_mouse", True)):
            self._iso_game_hwnd = int(game_hwnd)
            if self._start_isolation():
                success = True
                print("[bridge] Full Game Mode: mouse isolation STARTED (cursor relay)")
            else:
                print("[bridge] Full Game Mode: mouse isolation unavailable, continuing without it")

        # The game must hold the foreground for its gamepad to count and for
        # Raw Input to be its own; do it here so the user need not click the
        # game first. Nimbus itself is WS_EX_NOACTIVATE from step 0.
        if success and WINDOW_UTILS_AVAILABLE and game_hwnd:
            try:
                if set_foreground_window(int(game_hwnd)):
                    print("[bridge] Full Game Mode: game brought to the foreground")
            except Exception:
                pass

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
        # Release the physical mouse first so the user gets the pointer back
        if MOUSE_ISOLATION_AVAILABLE:
            self.stopMouseIsolation()
        self._iso_game_hwnd = 0
        # Stop controller mode
        if not _USE_MOUSE_HIDER:
            self._stop_pulse_only()
        if MOUSE_HIDER_AVAILABLE and _mouse_hider:
            try:
                _mouse_hider.stop_controller_mode()
                self._controller_mode_active = False
                self.controllerModeChanged.emit(False)
            except Exception:
                pass
        
        # Stop cursor release (Win32 only)
        if BORDERLESS_AVAILABLE and sys.platform == "win32":
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
        
        # Disable game focus mode — restore normal window activation (Win32 path)
        if WINDOW_UTILS_AVAILABLE and self._window and sys.platform == "win32":
            try:
                hwnd = get_qt_window_handle(self._window)
                remove_window_no_activate(hwnd)
                self._no_focus_mode = False
                self.noFocusModeChanged.emit(False)
                print("[bridge] Full Game Mode: Game Focus Mode disabled, window is activatable again")
            except Exception:
                pass
        
        if sys.platform != "win32" and self._no_focus_mode and self._window:
            if self._apply_no_focus_flag(False):
                self._no_focus_mode = False
                self.noFocusModeChanged.emit(False)
                print("[bridge] Full Game Mode: no-focus flag disabled")

        # Drop the session-only pin, unless the user had turned it on themselves
        self._game_mode_unpin_window()
        # Linux: drop an on-demand Xbox device when the profile's output is the joystick
        self._retire_inactive_interface()

        print("[bridge] Full Game Mode stopped")

    # =================================================================
    # Mouse isolation: the physical mouse is taken away from the game.
    # Windows: Nimbus Mouse Filter driver + cursor relay (the real cursor keeps
    # working, policed per point so it never wanders over the game). The
    # Linux branch uses the same names with an evdev grab and a software cursor.
    # =================================================================

    # ---- Mouse isolation: one API, two implementations -------------------
    #
    # Each entry point picks the relay (Windows) or the software cursor
    # (everything else). The implementations are the ``_relay`` and ``_sw``
    # methods further down; nothing outside this block should call them
    # directly, so a caller cannot accidentally bind the wrong platform.

    def _get_iso_active(self) -> bool:
        if _ISO_CURSOR_RELAY:
            return self._get_iso_active_relay()
        return self._get_iso_active_sw()

    mouseIsolationActive = Property(bool, _get_iso_active, notify=mouseIsolationChanged)

    @Slot(result=bool)
    def isMouseIsolationAvailable(self) -> bool:  # noqa: N802
        if _ISO_CURSOR_RELAY:
            return self._iso_available_relay()
        return self._iso_available_sw()

    @Slot(result=bool)
    def isMouseIsolationActive(self) -> bool:  # noqa: N802
        if _ISO_CURSOR_RELAY:
            return self._iso_is_active_relay()
        return self._iso_is_active_sw()

    @Slot(result=bool)
    def startMouseIsolation(self) -> bool:  # noqa: N802
        if _ISO_CURSOR_RELAY:
            return self._iso_start_relay()
        return self._iso_start_sw()

    def _start_isolation(self, nodes=None) -> bool:
        """Start isolation. ``nodes`` is the Linux device list; the relay
        takes none, because the filter driver owns every mouse."""
        if _ISO_CURSOR_RELAY:
            return self._start_isolation_relay()
        return self._start_isolation_sw(nodes)

    @Slot()
    def stopMouseIsolation(self) -> None:  # noqa: N802
        if _ISO_CURSOR_RELAY:
            self._iso_stop_relay()
        else:
            self._iso_stop_sw()

    def _iso_send_mouse(self, ev_type, button) -> None:
        if _ISO_CURSOR_RELAY:
            self._iso_send_mouse_relay(ev_type, button)
        else:
            self._iso_send_mouse_sw(ev_type, button)

    @Slot(int, int)
    def _on_iso_motion(self, dx: int, dy: int) -> None:
        if _ISO_CURSOR_RELAY:
            self._on_iso_motion_relay(dx, dy)
        else:
            self._on_iso_motion_sw(dx, dy)

    @Slot(int, bool)
    def _on_iso_button(self, code: int, pressed: bool) -> None:
        if _ISO_CURSOR_RELAY:
            self._on_iso_button_relay(code, pressed)
        else:
            self._on_iso_button_sw(code, pressed)

    @Slot(int, int)
    def _on_iso_wheel(self, horizontal: int, vertical: int) -> None:
        if _ISO_CURSOR_RELAY:
            self._on_iso_wheel_relay(horizontal, vertical)
        else:
            self._on_iso_wheel_sw(horizontal, vertical)

    @Slot(str)
    def _on_iso_stopped(self, reason: str) -> None:
        if _ISO_CURSOR_RELAY:
            self._on_iso_stopped_relay(reason)
        else:
            self._on_iso_stopped_sw(reason)

    def _get_iso_active_relay(self) -> bool:
        return bool(self._iso_active)


    def _iso_available_relay(self) -> bool:  # noqa: N802
        """True when the isolation driver is installed and attached to a mouse."""
        if not MOUSE_ISOLATION_AVAILABLE or not _mouse_isolation:
            return False
        try:
            return bool(_mouse_isolation.list_pointer_devices())
        except Exception:
            return False

    def _iso_is_active_relay(self) -> bool:  # noqa: N802
        return bool(self._iso_active)

    def _iso_start_relay(self) -> bool:  # noqa: N802
        """Take the physical mouse away from every other application.

        On Windows the real cursor keeps working (cursor relay) and the game,
        which keeps the foreground, receives no mouse input at all.
        ``Ctrl+Alt+F12``, :meth:`stopMouseIsolation`, closing Nimbus, or the
        driver's watchdog give it back.
        """
        return self._start_isolation()

    def _start_isolation_relay(self) -> bool:
        if not MOUSE_ISOLATION_AVAILABLE or not _mouse_isolation:
            print("[bridge] mouse isolation: driver not available")
            return False
        if self._iso_active:
            return True
        if self._window is None:
            print("[bridge] mouse isolation: window not set yet")
            return False
        try:
            self._iso_nimbus_hwnd = int(self._window.winId())
        except Exception:
            self._iso_nimbus_hwnd = 0
        relay = self._iso_relay
        iso = _mouse_isolation.MouseIsolation(
            on_motion=lambda dx, dy: relay.motion.emit(int(dx), int(dy)),
            on_button=lambda code, pressed: relay.button.emit(int(code), bool(pressed)),
            on_wheel=lambda h, v: relay.wheel.emit(int(h), int(v)),
            on_stopped=lambda reason: relay.stopped.emit(str(reason)),
            hotkey=True,
            cursor_relay=self._iso_relay_allowed,
        )
        try:
            iso.start()
        except Exception as exc:
            print(f"[bridge] mouse isolation failed: {exc}")
            return False
        self._iso = iso
        self._iso_active = True
        self._iso_buttons = Qt.MouseButton.NoButton
        self._iso_last_press = None
        self._iso_park_cursor_if_stuck()
        self.mouseIsolationChanged.emit(True)
        return True

    def _iso_park_cursor_if_stuck(self) -> bool:
        """Move the real cursor onto Nimbus if it sits on the game and not on us.

        The relay policy never moves the cursor deeper into the game window,
        so a cursor that is already there (the game was clicked, or a window
        moved under it) could not leave. Parking it on Nimbus's centre gives
        the user a cursor that works again. Safe from any thread.
        """
        game, nimbus = self._iso_game_hwnd, self._iso_nimbus_hwnd
        if not (game and nimbus) or not _mouse_isolation:
            return False
        x, y = _mouse_isolation.cursor_position()
        if _mouse_isolation.point_in_window(game, x, y) and not _mouse_isolation.point_in_window(nimbus, x, y):
            cx, cy = _mouse_isolation.window_center(nimbus)
            return bool(cx or cy) and _mouse_isolation.set_cursor_position(cx, cy)
        return False

    def _iso_stop_relay(self) -> None:  # noqa: N802
        """Give the physical mouse back (no-op when inactive)."""
        iso = self._iso
        if iso is not None and iso.active:
            iso.stop("requested")
        elif self._iso_active:
            self._on_iso_stopped("requested")

    def _iso_relay_allowed(self, x: int, y: int) -> bool:
        """Cursor-relay policy, asked on the reader thread for every packet.

        The real cursor may go anywhere except over the game window, unless
        that spot is also covered by Nimbus (the overlay sits on the game).
        Same rule as the ``WH_MOUSE_LL`` hook in :mod:`mouse_hider`, applied
        before the cursor moves instead of after, so games that read the
        cursor position see nothing either.
        """
        game = self._iso_game_hwnd
        if game and _mouse_isolation.point_in_window(game, x, y):
            nimbus = self._iso_nimbus_hwnd
            if nimbus and _mouse_isolation.point_in_window(nimbus, x, y):
                return True
            # Refused. If the cursor is already sitting on the game, park it
            # on Nimbus instead of leaving it stuck there.
            self._iso_park_cursor_if_stuck()
            return False
        return True

    def _iso_cursor_over_nimbus(self) -> bool:
        return bool(self._iso_nimbus_hwnd) and _mouse_isolation.hwnd_at_cursor() == self._iso_nimbus_hwnd

    def _iso_send_mouse_relay(self, ev_type, button) -> None:
        """Deliver a synthetic mouse event to our window at the real cursor position."""
        if self._window is None:
            return
        global_pos = QCursor.pos()
        local = QPointF(self._window.mapFromGlobal(global_pos))
        ev = QMouseEvent(ev_type, local, local, QPointF(global_pos), button, self._iso_buttons,
                         Qt.KeyboardModifier.NoModifier)
        QCoreApplication.sendEvent(self._window, ev)

    def _on_iso_motion_relay(self, dx: int, dy: int) -> None:
        # The relay already moved the real cursor on the reader thread, and Qt
        # receives the ordinary hover moves for it. Only a synthetic button
        # that is still held needs move events, so the pressed widget keeps
        # dragging.
        if self._iso_active and self._iso_buttons != Qt.MouseButton.NoButton:
            self._iso_send_mouse(QEvent.Type.MouseMove, Qt.MouseButton.NoButton)

    def _on_iso_button_relay(self, code: int, pressed: bool) -> None:
        if not self._iso_active:
            return
        button = _ISO_BUTTON_MAP.get(int(code))
        # Over our own window the click is synthesised into Qt: nothing is
        # injected, so the game sees no button either. Anywhere else it is
        # replayed into Windows (the desktop, or the game's own menus), which
        # a foreground Raw Input game does see as a button, never as motion.
        if button is None:
            synthesize = False
        elif pressed:
            synthesize = self._iso_cursor_over_nimbus()
        else:
            synthesize = bool(self._iso_buttons & button)   # release goes where the press went
        if not synthesize:
            _mouse_isolation.inject_button(int(code), bool(pressed))
            return
        if pressed:
            now = time.monotonic()
            interval = QGuiApplication.styleHints().mouseDoubleClickInterval() / 1000.0
            pos = QCursor.pos()
            last = self._iso_last_press
            is_double = (last is not None and last[1] == button and now - last[0] <= interval
                         and abs(last[2] - pos.x()) < 6 and abs(last[3] - pos.y()) < 6)
            self._iso_buttons |= button
            self._iso_last_press = None if is_double else (now, button, pos.x(), pos.y())
            self._iso_send_mouse(QEvent.Type.MouseButtonDblClick if is_double else QEvent.Type.MouseButtonPress,
                                 button)
        else:
            self._iso_buttons &= ~button
            self._iso_send_mouse(QEvent.Type.MouseButtonRelease, button)

    def _on_iso_wheel_relay(self, horizontal: int, vertical: int) -> None:
        if not self._iso_active or self._window is None:
            return
        if not self._iso_cursor_over_nimbus():
            _mouse_isolation.inject_wheel(int(horizontal), int(vertical))
            return
        global_pos = QCursor.pos()
        local = QPointF(self._window.mapFromGlobal(global_pos))
        ev = QWheelEvent(local, QPointF(global_pos), QPoint(0, 0),
                         QPoint(int(horizontal) * 120, int(vertical) * 120),
                         self._iso_buttons, Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        QCoreApplication.sendEvent(self._window, ev)

    def _on_iso_stopped_relay(self, reason: str) -> None:
        if not self._iso_active:
            return
        # Release any synthetic button still held so widgets do not stick
        for _code, button in _ISO_BUTTON_MAP.items():
            if self._iso_buttons & button:
                self._iso_buttons &= ~button
                self._iso_send_mouse(QEvent.Type.MouseButtonRelease, button)
        self._iso_active = False
        self._iso = None
        print(f"[bridge] mouse isolation stopped ({reason})")
        self.mouseIsolationChanged.emit(False)

    @Slot(result="QVariantMap")
    def getGameModeDiagnostics(self) -> dict:  # noqa: N802
        """Return diagnostic info about Game Mode readiness for the UI."""
        import sys as _sys
        result = {
            "vigem_package": self._output.vigem_available,
            "uinput": UINPUT_AVAILABLE,
            "mouse_isolation": MOUSE_ISOLATION_AVAILABLE,
            "mouse_isolation_active": self._iso_active,
            "platform": sys.platform,
            "vigem_gamepad": bool(self._vigem and self._vigem.gamepad),
            "vigem_connected": bool(self._vigem and self._vigem.is_connected),
            "mouse_hider": MOUSE_HIDER_AVAILABLE,
            "borderless": BORDERLESS_AVAILABLE,
            "window_utils": WINDOW_UTILS_AVAILABLE,
            "profile": str(self._config.get_layout_type()),
            "use_vigem": self._use_vigem,
            "controller_mode_active": self._controller_mode_active,
            "mouse_isolation": MOUSE_ISOLATION_AVAILABLE,
            "mouse_isolation_active": self._iso_active,
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
    # Mouse Isolation (Linux): grab the physical mouse, software cursor
    # =================================================================

    def _get_iso_active_sw(self) -> bool:
        return bool(self._iso_active)

    def _get_iso_x(self) -> float:
        return float(self._iso_x)

    def _get_iso_y(self) -> float:
        return float(self._iso_y)

    isolationCursorX = Property(float, _get_iso_x, notify=isolationCursorMoved)
    isolationCursorY = Property(float, _get_iso_y, notify=isolationCursorMoved)

    def _iso_available_sw(self) -> bool:  # noqa: N802
        """True on Linux when at least one pointer device is present."""
        if not MOUSE_ISOLATION_AVAILABLE or not _mouse_isolation:
            return False
        try:
            return bool(_mouse_isolation.list_pointer_devices())
        except Exception:
            return False

    @Slot(result=str)
    def getMouseIsolationDevices(self) -> str:  # noqa: N802
        """JSON list of pointer devices (name, node, readable, is_keyboard)."""
        import json
        if not MOUSE_ISOLATION_AVAILABLE or not _mouse_isolation:
            return "[]"
        try:
            return json.dumps([{k: d[k] for k in ("name", "node", "readable", "is_keyboard")}
                               for d in _mouse_isolation.list_pointer_devices()])
        except Exception:
            return "[]"

    def _iso_is_active_sw(self) -> bool:  # noqa: N802
        return bool(self._iso_active)

    def _iso_start_sw(self) -> bool:  # noqa: N802
        """Grab every physical pointer device and drive a software cursor.

        The desktop pointer freezes (games, and the X server, stop receiving
        the mouse); Nimbus draws its own cursor and delivers synthetic mouse
        events to its window. ``Ctrl+Alt+F12`` or the same toggle releases it.
        """
        return self._start_isolation(None)

    def _start_isolation_sw(self, nodes) -> bool:
        if not MOUSE_ISOLATION_AVAILABLE or not _mouse_isolation:
            print("[bridge] mouse isolation is only available on Linux")
            return False
        if self._iso_active:
            return True
        if self._window is None:
            print("[bridge] mouse isolation: window not set yet")
            return False
        relay = self._iso_relay
        iso = _mouse_isolation.MouseIsolation(
            on_motion=lambda dx, dy: relay.motion.emit(int(dx), int(dy)),
            on_button=lambda code, pressed: relay.button.emit(int(code), bool(pressed)),
            on_wheel=lambda h, v: relay.wheel.emit(int(h), int(v)),
            on_stopped=lambda reason: relay.stopped.emit(str(reason)),
        )
        try:
            iso.start(nodes)
        except Exception as exc:
            print(f"[bridge] mouse isolation failed: {exc}")
            return False
        self._iso = iso
        self._iso_active = True
        self._iso_buttons = Qt.MouseButton.NoButton
        self._iso_last_press = None
        # Software cursor starts at the window centre; park the (now frozen)
        # real pointer there too and hide it over our window.
        self._iso_set_cursor(self._window.width() / 2.0, self._window.height() / 2.0)
        try:
            if self._x11_session():
                QCursor.setPos(self._window.mapToGlobal(QPoint(int(self._iso_x), int(self._iso_y))))
            self._window.setCursor(QCursor(Qt.CursorShape.BlankCursor))
        except Exception:
            pass
        self.mouseIsolationChanged.emit(True)
        return True

    def _iso_stop_sw(self) -> None:  # noqa: N802
        """Release the physical mouse grab (no-op when inactive)."""
        iso = self._iso
        if iso is not None and iso.active:
            iso.stop("requested")
        elif self._iso_active:
            self._on_iso_stopped("requested")

    def _iso_event_target(self) -> Optional[QWindow]:
        """The window synthetic isolation events must be delivered to.

        Axis, Joystick and Button Settings open as modal dialogs through
        ``exec()``. A modal window takes every input event while it is up, so
        events posted straight to the main QML window never arrive. With the
        physical pointer grabbed, that left the user facing a dialog they
        could not click and could not dismiss.
        """
        modal = QGuiApplication.modalWindow()
        if modal is not None and modal.isVisible():
            return modal
        return self._window

    def _iso_retarget(self) -> Optional[QWindow]:
        """Follow the event target, carrying the cursor position across.

        ``_iso_x``/``_iso_y`` are local to whichever window currently receives
        events. Translating through global coordinates when that window
        changes keeps the cursor where the user left it, instead of snapping
        to a corner every time a dialog opens or closes.
        """
        target = self._iso_event_target()
        previous = self._iso_target
        if target is not previous:
            if previous is not None and target is not None:
                try:
                    glob = previous.mapToGlobal(QPoint(int(self._iso_x), int(self._iso_y)))
                    local = target.mapFromGlobal(glob)
                    self._iso_x, self._iso_y = float(local.x()), float(local.y())
                except Exception:
                    self._iso_x = self._iso_y = 0.0
            self._iso_target = target
        return target

    def _iso_set_cursor(self, x: float, y: float) -> None:
        target = self._iso_retarget()
        if target is None:
            return
        w = max(1, target.width())
        h = max(1, target.height())
        self._iso_x = min(max(0.0, float(x)), w - 1.0)
        self._iso_y = min(max(0.0, float(y)), h - 1.0)
        self.isolationCursorMoved.emit(self._iso_x, self._iso_y)

    def _iso_send_mouse_sw(self, ev_type, button) -> None:
        target = self._iso_retarget()
        if target is None:
            return
        local = QPointF(self._iso_x, self._iso_y)
        global_pos = QPointF(target.mapToGlobal(QPoint(int(self._iso_x), int(self._iso_y))))
        ev = QMouseEvent(ev_type, local, local, global_pos, button, self._iso_buttons,
                         Qt.KeyboardModifier.NoModifier)
        QCoreApplication.sendEvent(target, ev)

    def _on_iso_motion_sw(self, dx: int, dy: int) -> None:
        if not self._iso_active:
            return
        speed = float(self._config.get("controller.isolation_cursor_speed", 1.0))
        self._iso_set_cursor(self._iso_x + dx * speed, self._iso_y + dy * speed)
        self._iso_send_mouse(QEvent.Type.MouseMove, Qt.MouseButton.NoButton)

    def _on_iso_button_sw(self, code: int, pressed: bool) -> None:
        if not self._iso_active:
            return
        button = _ISO_BUTTON_MAP.get(int(code))
        if button is None:
            return
        if pressed:
            now = time.monotonic()
            interval = QGuiApplication.styleHints().mouseDoubleClickInterval() / 1000.0
            last = self._iso_last_press
            is_double = (last is not None and last[1] == button and now - last[0] <= interval
                         and abs(last[2] - self._iso_x) < 6 and abs(last[3] - self._iso_y) < 6)
            self._iso_buttons |= button
            # X11 sequence for rapid clicks is Press, Release, DblClick, Release, Press ...
            self._iso_last_press = None if is_double else (now, button, self._iso_x, self._iso_y)
            self._iso_send_mouse(QEvent.Type.MouseButtonDblClick if is_double else QEvent.Type.MouseButtonPress, button)
        else:
            self._iso_buttons &= ~button
            self._iso_send_mouse(QEvent.Type.MouseButtonRelease, button)

    def _on_iso_wheel_sw(self, horizontal: int, vertical: int) -> None:
        if not self._iso_active or self._window is None:
            return
        local = QPointF(self._iso_x, self._iso_y)
        global_pos = QPointF(self._window.mapToGlobal(QPoint(int(self._iso_x), int(self._iso_y))))
        ev = QWheelEvent(local, global_pos, QPoint(0, 0), QPoint(int(horizontal) * 120, int(vertical) * 120),
                         self._iso_buttons, Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        QCoreApplication.sendEvent(self._window, ev)

    def _on_iso_stopped_sw(self, reason: str) -> None:
        if not self._iso_active:
            return
        # Release any synthetic button still held so widgets do not stick
        for code, button in _ISO_BUTTON_MAP.items():
            if self._iso_buttons & button:
                self._iso_buttons &= ~button
                self._iso_send_mouse(QEvent.Type.MouseButtonRelease, button)
        self._iso_active = False
        self._iso = None
        try:
            if self._window is not None:
                self._window.unsetCursor()
                if self._x11_session():
                    QCursor.setPos(self._window.mapToGlobal(QPoint(int(self._iso_x), int(self._iso_y))))
        except Exception:
            pass
        print(f"[bridge] mouse isolation stopped ({reason})")
        self.mouseIsolationChanged.emit(False)

    # =================================================================
    # Account, Telemetry & Updater — QML-callable slots
    # =================================================================

    # ---- Account ----

    @Slot()
    def _notify_account(self) -> None:
        self.accountStateChanged.emit()

    @Slot(bool)
    def _on_sync_completed(self, success: bool) -> None:
        if success:
            self.profilesListChanged.emit()
        self.syncCompleted.emit(success)

    @Slot(str)
    def _on_remote_profile_updated(self, profile_id: str) -> None:
        if profile_id == self._config.get_current_profile():
            if self._config.switch_profile(profile_id):
                self._refresh_active_profile()
        self.profilesListChanged.emit()

    @Property(bool, notify=accountStateChanged)
    def accountAuthenticated(self) -> bool:
        return bool(self._services and self._services.cloud.is_authenticated)

    @Property(str, notify=accountStateChanged)
    def accountDisplayName(self) -> str:
        return self._services.cloud.display_name if self._services else "Not signed in"

    @Property(str, notify=accountStateChanged)
    def accountEmail(self) -> str:
        user = self._services.cloud.user if self._services else None
        return (user or {}).get("email", "")

    @Property(str, notify=accountStateChanged)
    def accountTier(self) -> str:
        return self._services.cloud.tier if self._services else "free"

    @Property(bool, notify=accountStateChanged)
    def accountPremium(self) -> bool:
        return bool(self._services and self._services.cloud.is_premium)

    @Slot(str, str, result=bool)
    def loginWithEmail(self, email: str, password: str) -> bool:  # noqa: N802
        """Sign in with email and password.  Returns True on success."""
        return bool(self._services and self._services.cloud.login_with_email(email, password))

    @Slot(str, str, result=bool)
    def signupWithEmail(self, email: str, password: str) -> bool:  # noqa: N802
        """Create an account through the injected cloud service."""
        return bool(self._services and self._services.cloud.signup_with_email(email, password))

    @Slot(result=bool)
    def syncProfiles(self) -> bool:  # noqa: N802
        """Synchronize profiles through the injected cloud service."""
        return bool(self._services and self._services.cloud.sync_profiles())

    @Slot(str)
    def loginWithProvider(self, provider: str) -> None:  # noqa: N802
        """Open the system browser for OAuth login (google / facebook)."""
        if self._services:
            self._services.cloud.login_with_browser(provider)

    @Slot()
    def logoutAccount(self) -> None:  # noqa: N802
        """Sign out and clear all stored tokens."""
        if self._services:
            self._services.cloud.logout()

    # ---- Telemetry ----

    @Slot(bool)
    def setAnalyticsEnabled(self, enabled: bool) -> None:  # noqa: N802
        """Toggle anonymous usage analytics on or off."""
        if self._services:
            self._services.telemetry.analytics_enabled = enabled
        else:
            self._config.set("telemetry.analytics_enabled", enabled)
            self._config.save_config()
        self.privacyChanged.emit()

    @Slot(bool)
    def setCrashReportsEnabled(self, enabled: bool) -> None:  # noqa: N802
        """Toggle crash report collection on or off."""
        if self._services:
            self._services.telemetry.crash_reports_enabled = enabled
        else:
            self._config.set("telemetry.crash_reports_enabled", enabled)
            self._config.save_config()
        self.privacyChanged.emit()

    @Slot(result=bool)
    def isAnalyticsEnabled(self) -> bool:  # noqa: N802
        """Return whether usage analytics is currently enabled."""
        if self._services:
            return self._services.telemetry.analytics_enabled
        return bool(self._config.get("telemetry.analytics_enabled", False))

    @Slot(result=bool)
    def isCrashReportsEnabled(self) -> bool:  # noqa: N802
        """Return whether crash reporting is currently enabled."""
        if self._services:
            return self._services.telemetry.crash_reports_enabled
        return bool(self._config.get("telemetry.crash_reports_enabled", False))

    analyticsEnabled = Property(bool, isAnalyticsEnabled, notify=privacyChanged)
    crashReportsEnabled = Property(bool, isCrashReportsEnabled, notify=privacyChanged)

    # ---- Updater ----

    @Slot()
    def checkForUpdates(self) -> None:  # noqa: N802
        """Manually trigger an update check."""
        if self._services:
            self._services.updater.check()

    @Slot()
    def openDownloadPage(self) -> None:  # noqa: N802
        """Open the download page for the latest version."""
        if self._services:
            self._services.updater.open_download_page()

    @Slot()
    def dismissUpdate(self) -> None:  # noqa: N802
        """Dismiss the current update notification."""
        if self._services:
            self._services.updater.dismiss()
