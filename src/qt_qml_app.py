"""
Application entry point for Nimbus Adaptive Controller (QML front end).

Bootstraps the :class:`QApplication`, displays a splash screen while the
heavyweight subsystems initialize (vJoy/ViGEm probing, telemetry, cloud
client, update checker), then loads ``qml/Main.qml`` via
:class:`QQmlApplicationEngine`.

Context properties exposed to QML
---------------------------------
* ``controller`` — :class:`~src.bridge.ControllerBridge`
* ``config`` — :class:`~src.config.ControllerConfig`
* ``telemetry`` — :class:`~src.telemetry.TelemetryClient`
* ``cloud`` — :class:`~src.cloud_client.CloudClient`
* ``updater`` — :class:`~src.updater.UpdateChecker`

Run with ``python -m src.qt_qml_app`` or via ``run.py`` / ``run.bat``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QIcon, QPixmap, QColor, QPainter, QFont
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication, QSplashScreen

from .bridge import ControllerBridge
from .config import ControllerConfig
from .telemetry import TelemetryClient
from .cloud_client import CloudClient
from .updater import UpdateChecker
from . import __version__


def qml_path() -> Path:
    """Return the absolute path to the root QML file (``qml/Main.qml``)."""
    here = Path(__file__).resolve().parent
    project_root = here.parent
    return project_root / "qml" / "Main.qml"


def _create_splash(project_root: Path) -> QSplashScreen | None:
    """Build and show a dark splash screen with the logo, version, and tagline.

    Args:
        project_root: Repository root used to locate ``logo.png``.

    Returns:
        The shown :class:`QSplashScreen`, or ``None`` if the logo file is
        missing or unreadable (the app still starts; just no splash).
    """
    logo_path = project_root / "logo.png"
    if not logo_path.exists():
        return None

    logo = QPixmap(str(logo_path))
    if logo.isNull():
        return None

    # Create a dark background splash (400x360)
    splash_w, splash_h = 400, 360
    pixmap = QPixmap(splash_w, splash_h)
    pixmap.fill(QColor(17, 17, 17))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    # Draw rounded border
    painter.setPen(QColor(74, 158, 255))
    painter.drawRoundedRect(1, 1, splash_w - 2, splash_h - 2, 12, 12)

    # Scale and center the logo
    logo_size = 180
    scaled_logo = logo.scaled(logo_size, logo_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    logo_x = (splash_w - scaled_logo.width()) // 2
    painter.drawPixmap(logo_x, 30, scaled_logo)

    # Version text
    painter.setPen(QColor(120, 120, 120))
    painter.setFont(QFont("Segoe UI", 10))
    painter.drawText(0, 230, splash_w, 24, Qt.AlignHCenter, f"v{__version__}")

    # Loading text
    painter.setPen(QColor(170, 170, 170))
    painter.setFont(QFont("Segoe UI", 11))
    painter.drawText(0, 270, splash_w, 24, Qt.AlignHCenter, "Initializing controllers...")

    # Tagline
    painter.setPen(QColor(80, 80, 80))
    painter.setFont(QFont("Segoe UI", 9))
    painter.drawText(0, 320, splash_w, 20, Qt.AlignHCenter, "Virtual controller for adaptive gaming")

    painter.end()

    splash = QSplashScreen(pixmap, Qt.WindowStaysOnTopHint)
    splash.show()
    return splash


def main() -> int:
    """Application entry point: build subsystems, load QML, run the event loop.

    Returns:
        The Qt event loop exit code (``0`` on clean shutdown, non-zero on
        QML load failure).
    """
    app = QApplication(sys.argv)
    app.setApplicationName("Nimbus Adaptive Controller")

    # Resolve project root
    here = Path(__file__).resolve().parent
    project_root = here.parent

    # Show splash screen while initializing
    splash = _create_splash(project_root)
    if splash:
        app.processEvents()

    # Config and bridge (heaviest init — vJoy/ViGEm probing)
    if splash:
        splash.showMessage("  Loading configuration...", Qt.AlignBottom | Qt.AlignLeft, QColor(120, 120, 120))
        app.processEvents()
    config = ControllerConfig()

    if splash:
        splash.showMessage("  Initializing controllers...", Qt.AlignBottom | Qt.AlignLeft, QColor(120, 120, 120))
        app.processEvents()
    bridge = ControllerBridge(config)

    # Telemetry (opt-in analytics + crash reporting)
    if splash:
        splash.showMessage("  Starting telemetry...", Qt.AlignBottom | Qt.AlignLeft, QColor(120, 120, 120))
        app.processEvents()
    telemetry = TelemetryClient(config)

    # Cloud client (user accounts, profile sync)
    cloud = CloudClient(config)

    # Auto-updater (lightweight version check)
    updater = UpdateChecker(config)

    if splash:
        splash.showMessage("  Loading interface...", Qt.AlignBottom | Qt.AlignLeft, QColor(120, 120, 120))
        app.processEvents()

    engine = QQmlApplicationEngine()
    # Expose bridge, config, and new services to QML
    engine.rootContext().setContextProperty("controller", bridge)
    engine.rootContext().setContextProperty("config", config)
    engine.rootContext().setContextProperty("telemetry", telemetry)
    engine.rootContext().setContextProperty("cloud", cloud)
    engine.rootContext().setContextProperty("updater", updater)

    # Load QML
    main_qml = qml_path()
    engine.load(QUrl.fromLocalFile(str(main_qml)))

    if not engine.rootObjects():
        if splash:
            splash.close()
        return 1

    # Close splash once main window is ready
    if splash:
        splash.close()

    # Track session start
    vjoy_ok = bridge._vjoy.is_connected if bridge._vjoy else False
    vigem_ok = bridge._vigem.is_connected if bridge._vigem else False
    output_mode = "vigem" if bridge._use_vigem else "vjoy"
    telemetry.track_session_start(__version__, output_mode, vjoy_ok, vigem_ok)

    exit_code = app.exec()

    # Graceful shutdown
    telemetry.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
