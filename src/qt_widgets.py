"""
Custom QtWidgets controls used by the legacy widget shell.

Provides two interactive widgets:

* :class:`JoystickWidget` — a circular two-axis joystick that emits
  normalized ``(x, y)`` in ``[-1, 1]``.
* :class:`SliderWidget` — a one-axis slider/throttle that emits a
  normalized value in ``[-1, 1]``.

Both widgets:

* Read sizing / DPI scale from :class:`~src.config.ControllerConfig`.
* Use *relative* drag tracking (the handle does not jump to the click
  point) so a partial-range physical input maps 1:1 to the on-screen
  control.
* Animate a gentle exponential return-to-center on mouse release rather
  than snapping, so the downstream vJoy/ViGEm signal does not pop.

The QML front end uses its own controls; these widgets are only consumed
by :mod:`src.qt_main`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QPointF, QRectF, QSize, QTimer
from PySide6.QtGui import QPainter, QPen, QBrush, QColor
from PySide6.QtWidgets import QWidget, QSizePolicy

from .config import ControllerConfig


class JoystickWidget(QWidget):
    """Circular two-axis joystick with normalized output.

    Emits :attr:`valueChanged` with ``(x, y)`` in ``[-1, 1]`` whenever the
    handle position changes. Up is positive Y. The handle is clamped to the
    unit circle — diagonal pulls cannot exceed magnitude 1.

    Args:
        config: Application configuration (used for scaled sizing).
        which: Logical identifier — ``"left"`` or ``"right"``. Used by
            callers to disambiguate when wiring multiple instances.
        parent: Optional Qt parent.

    Signals:
        valueChanged(float, float): Emitted with normalized ``(x, y)``.
    """

    valueChanged = Signal(float, float)  # x, y in [-1, 1]

    def __init__(self, config: ControllerConfig, which: str = "left", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.which = which
        self._value = QPointF(0.0, 0.0)  # normalized [-1,1]
        self._dragging = False
        self._drag_start_pos = QPointF(0.0, 0.0)
        self._start_value = QPointF(0.0, 0.0)
        # Timer for gentle recentring
        self._return_timer = QTimer(self)
        self._return_timer.setInterval(16)  # ~60 FPS
        self._return_timer.timeout.connect(self._tick_return_to_center)
        self.setMouseTracking(True)
        size = self.config.get_scaled_int(200)
        target = int(self.config.get("ui.joystick_size", 200))
        self.setMinimumSize(target, target)
        self.setMaximumSize(target, target)
        # Keep joystick size constrained; don't let it expand too much
        policy = QSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        # Make the widget background transparent to avoid overlay issues
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")

    # Ensure the widget stays square to prevent clipping and dead space
    def sizeHint(self) -> QSize:
        # Square target based on configured joystick size
        target = int(self.config.get("ui.joystick_size", 200))
        return QSize(target, target)

    def minimumSizeHint(self) -> QSize:
        target = int(self.config.get("ui.joystick_size", 200))
        min_side = int(target * 0.70)
        return QSize(min_side, min_side)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return w

    # --- Scaling API ---
    def apply_scale(self) -> None:
        """Refresh size hints and bounds based on current config scale."""
        target = int(self.config.get("ui.joystick_size", 200))
        min_side = int(target * 0.70)
        self.setMinimumSize(min_side, min_side)
        # Notify layout system
        self.updateGeometry()
        self.update()

    # --- Public API ---
    def center(self) -> None:
        self._value = QPointF(0.0, 0.0)
        self.valueChanged.emit(0.0, 0.0)
        self.update()

    # --- Events ---
    def _norm_from_pos(self, pos: QPointF) -> QPointF:
        r = min(self.width(), self.height()) / 2.0
        cx, cy = self.width() / 2.0, self.height() / 2.0
        dx = (pos.x() - cx) / r
        dy = (pos.y() - cy) / r
        # Clamp to unit circle
        mag2 = dx * dx + dy * dy
        if mag2 > 1.0:
            mag = mag2 ** 0.5
            dx /= mag
            dy /= mag
        # Convert to conventional joystick: up is +Y in our app? Existing uses -1..1 with natural mapping; keep y positive up by inverting dy
        return QPointF(dx, -dy)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            # Start drag without jumping the current value
            self._dragging = True
            self._drag_start_pos = event.position()
            self._start_value = QPointF(self._value)
            self._return_timer.stop()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and (event.buttons() & Qt.LeftButton):
            # Compute delta from press and convert to normalized change
            # Use the same effective radius as painting so the handle cannot cross the boundary
            pen_w = 2
            handle_r = self.config.get_scaled_int(10)
            pad = self.config.get_scaled_int(6) + pen_w + handle_r
            base_r = max(1.0, min(self.width(), self.height()) / 2.0 - pad)
            effective_r = max(1.0, base_r - handle_r)
            dx = (event.position().x() - self._drag_start_pos.x()) / effective_r
            dy = (event.position().y() - self._drag_start_pos.y()) / effective_r
            # Invert Y to keep up as +
            delta = QPointF(dx, -dy)
            new = QPointF(self._start_value.x() + delta.x(), self._start_value.y() + delta.y())
            # Clamp to unit circle
            mag2 = new.x() * new.x() + new.y() * new.y()
            if mag2 > 1.0:
                mag = mag2 ** 0.5
                new.setX(new.x() / mag)
                new.setY(new.y() / mag)
            if abs(new.x() - self._value.x()) > 1e-6 or abs(new.y() - self._value.y()) > 1e-6:
                self._value = new
                self.valueChanged.emit(self._value.x(), self._value.y())
                self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._dragging = False
            # Start gentle return to center instead of instant jump
            self._return_timer.start()

    def _tick_return_to_center(self) -> None:
        # Exponential decay towards (0,0)
        decay = 0.15  # adjust for speed of return
        x = self._value.x()
        y = self._value.y()
        nx = x * (1.0 - decay)
        ny = y * (1.0 - decay)
        # Snap to zero when sufficiently close
        if abs(nx) < 0.01:
            nx = 0.0
        if abs(ny) < 0.01:
            ny = 0.0
        if nx == 0.0 and ny == 0.0:
            self._return_timer.stop()
        if abs(nx - x) > 1e-6 or abs(ny - y) > 1e-6:
            self._value = QPointF(nx, ny)
            self.valueChanged.emit(self._value.x(), self._value.y())
            self.update()

    # --- Painting ---
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        
        # Don't fill background - let it be transparent to avoid overlay issues
        # try:
        #     bg = self.config.get("ui.background_color", (20, 20, 20))
        #     p.fillRect(self.rect(), QColor(*bg))
        # except Exception:
        #     pass
        
        # Debug: draw widget bounds to see what's happening
        # p.setPen(QPen(QColor(255, 0, 0), 1))
        # p.drawRect(self.rect().adjusted(0, 0, -1, -1))

        # Joystick base circle (centered); pad for pen and handle to avoid clipping
        pen_w = 2
        handle_r = self.config.get_scaled_int(10)
        pad = self.config.get_scaled_int(6) + pen_w + handle_r
        r = max(1.0, min(self.width(), self.height()) / 2.0 - pad)
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        # Outline-only circle (no red fill)
        p.setPen(QPen(QColor(100, 150, 255), pen_w))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)

        # Current handle position
        effective_r = max(0.0, r - handle_r)
        hx = cx + self._value.x() * effective_r
        hy = cy - self._value.y() * effective_r

        # Crosshair centered at the handle position and clamped to the circle boundary
        p.setPen(QPen(QColor(50, 100, 200), 2))
        # Horizontal line y = hy intersects circle at x = cx ± sqrt(r^2 - (hy - cy)^2)
        dy = hy - cy
        if r > 0 and abs(dy) <= r:
            dx_lim = (r * r - dy * dy) ** 0.5
            x1 = cx - dx_lim
            x2 = cx + dx_lim
            p.drawLine(int(x1), int(hy), int(x2), int(hy))
        # Vertical line x = hx intersects at y = cy ± sqrt(r^2 - (hx - cx)^2)
        dx = hx - cx
        if r > 0 and abs(dx) <= r:
            dy_lim = (r * r - dx * dx) ** 0.5
            y1 = cy - dy_lim
            y2 = cy + dy_lim
            p.drawLine(int(hx), int(y1), int(hx), int(y2))

        # Handle
        p.setPen(QPen(QColor(100, 150, 255), 2))
        p.setBrush(QBrush(QColor(50, 100, 200)))
        p.drawEllipse(QPointF(hx, hy), handle_r, handle_r)


class SliderWidget(QWidget):
    """One-axis slider/throttle widget with normalized output.

    Emits :attr:`valueChanged` with a single float in ``[-1, 1]`` whenever
    the handle position changes. Vertical sliders treat *up* as positive.

    Args:
        config: Application configuration (used for scaled sizing).
        orientation: ``Qt.Horizontal`` or ``Qt.Vertical``.
        label: Optional display label (used by callers for layout titles).
        auto_center: If True, the handle returns to zero on release.
        parent: Optional Qt parent.
        gentle_return: If True, return-to-center is animated via exponential
            decay rather than snapping.

    Signals:
        valueChanged(float): Emitted with the new normalized value.
    """

    valueChanged = Signal(float)  # value in [-1, 1]

    def __init__(self, config: ControllerConfig, orientation: Qt.Orientation, label: str = "", auto_center: bool = True, parent: QWidget | None = None, gentle_return: bool = True) -> None:
        super().__init__(parent)
        self.config = config
        self.orientation = orientation
        self.label = label
        self.auto_center = auto_center
        self.gentle_return = gentle_return
        self._value = 0.0
        self._dragging = False
        self._drag_start_pos = QPointF(0.0, 0.0)
        self._start_value = 0.0
        # Timer for gentle recentring
        self._return_timer = QTimer(self)
        self._return_timer.setInterval(16)
        self._return_timer.timeout.connect(self._tick_return_to_center)
        self.setMouseTracking(True)
        if self.orientation == Qt.Horizontal:
            # Slimmer horizontal slider to free space for joysticks
            self.setMinimumSize(self.config.get_scaled_int(240), self.config.get_scaled_int(24))
        else:
            # Shorter vertical slider to avoid overlap on small windows
            self.setMinimumSize(self.config.get_scaled_int(40), self.config.get_scaled_int(200))

    def setValue(self, v: float) -> None:
        v = max(-1.0, min(1.0, v))
        if abs(v - self._value) > 1e-6:
            self._value = v
            self.valueChanged.emit(self._value)
            self.update()

    # --- Scaling API ---
    def apply_scale(self) -> None:
        """Refresh min sizes based on current scale."""
        if self.orientation == Qt.Horizontal:
            self.setMinimumSize(self.config.get_scaled_int(240), self.config.get_scaled_int(24))
        else:
            self.setMinimumSize(self.config.get_scaled_int(40), self.config.get_scaled_int(200))
        self.updateGeometry()
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            # Begin drag without jumping; store start position and value
            self._dragging = True
            self._drag_start_pos = event.position()
            self._start_value = float(self._value)
            self._return_timer.stop()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and (event.buttons() & Qt.LeftButton):
            # Compute relative change based on drag distance since press
            if self.orientation == Qt.Horizontal:
                total = max(1, self.width())
                delta = (event.position().x() - self._drag_start_pos.x()) / total
                new_value = self._start_value + delta * 2.0  # map width to [-1,1]
            else:
                total = max(1, self.height())
                delta = (event.position().y() - self._drag_start_pos.y()) / total
                new_value = self._start_value - delta * 2.0  # invert for natural up/down
            self.setValue(max(-1.0, min(1.0, new_value)))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._dragging = False
            # Start gentle drift to center instead of instant jump when releasing
            if self.gentle_return and self.auto_center:
                self._return_timer.start()

    def _tick_return_to_center(self) -> None:
        decay = 0.15
        v = self._value * (1.0 - decay)
        if abs(v) < 0.01:
            v = 0.0
        if v == 0.0:
            self._return_timer.stop()
        if abs(v - self._value) > 1e-6:
            self._value = v
            self.valueChanged.emit(self._value)
            self.update()

    def _update_from_pos(self, pos: QPointF) -> None:
        if self.orientation == Qt.Horizontal:
            t = (pos.x() - self.config.get_scaled_int(8)) / max(1, self.width() - self.config.get_scaled_int(16))
            self.setValue(t * 2.0 - 1.0)
        else:
            t = (pos.y() - self.config.get_scaled_int(8)) / max(1, self.height() - self.config.get_scaled_int(16))
            self.setValue(-(t * 2.0 - 1.0))

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        bg = self.config.get("ui.background_color", (20, 20, 20))
        p.fillRect(self.rect(), QColor(*bg))

        track_color = QColor(15, 30, 60)
        accent = QColor(100, 150, 255)
        hover = QColor(50, 100, 200)

        pad = self.config.get_scaled_int(8)
        if self.orientation == Qt.Horizontal:
            track = QRectF(pad, self.height() / 2 - self.config.get_scaled_int(5), self.width() - 2 * pad, self.config.get_scaled_int(10))
            p.fillRect(track, track_color)
            # Center line
            p.fillRect(QRectF(self.width() / 2 - 1, track.top(), 2, track.height()), hover)
            # Handle
            handle_w = self.config.get_scaled_int(16)
            cx = pad + (self._value + 1.0) / 2.0 * (self.width() - 2 * pad)
            handle = QRectF(cx - handle_w / 2, track.top() - self.config.get_scaled_int(5), handle_w, track.height() + self.config.get_scaled_int(10))
        else:
            track = QRectF(self.width() / 2 - self.config.get_scaled_int(6), pad, self.config.get_scaled_int(12), self.height() - 2 * pad)
            p.fillRect(track, track_color)
            # Center line
            p.fillRect(QRectF(track.left(), self.height() / 2 - 1, track.width(), 2), hover)
            # Handle
            handle_h = self.config.get_scaled_int(18)
            cy = pad + (1.0 - (self._value + 1.0) / 2.0) * (self.height() - 2 * pad)
            handle = QRectF(track.left() - self.config.get_scaled_int(6), cy - handle_h / 2, track.width() + self.config.get_scaled_int(12), handle_h)

        p.setPen(QPen(accent, 2))
        p.setBrush(QBrush(hover))
        p.drawRoundedRect(handle, 4, 4)
