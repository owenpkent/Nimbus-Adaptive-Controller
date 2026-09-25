"""
Auto-updater for Nimbus Adaptive Controller.

Provides a lightweight, non-intrusive update mechanism:

1. On startup, checks a version manifest JSON hosted at a known URL.
2. Compares the remote ``latest`` version against the running version.
3. If an update is available, emits a signal so the UI can show a ribbon.
4. User clicks the ribbon → system browser opens the download page.

No background downloads, no silent installs — the user is always in control.

Version Manifest Format
-----------------------
Hosted at ``https://nimbus.app/version.json`` (or override via env var):

.. code-block:: json

    {
        "latest": "1.6.0",
        "min_supported": "1.4.0",
        "download_url": "https://github.com/owenpkent/Nimbus-Adaptive-Controller/releases/latest",
        "release_notes": "Bug fixes and performance improvements.",
        "release_date": "2026-04-01",
        "channels": {
            "stable": "1.6.0",
            "beta": "1.6.1-beta.1",
            "dev": "1.7.0-dev.42"
        }
    }

Update Channels
---------------
- ``stable`` — default for all users; major/minor releases, fully tested
- ``beta`` — opt-in feature previews and release candidates
- ``dev`` — every merge to ``main``; for contributors only

Users select their channel in Settings → Updates. Default is ``stable``.

Usage::

    from src.updater import UpdateChecker

    checker = UpdateChecker(config)
    checker.updateAvailable.connect(on_update_available)
    checker.check()  # non-blocking; emits signal if update found
"""

from __future__ import annotations

import json
import logging
import os
import re
import webbrowser
from typing import Any, Dict, Optional, Tuple

from PySide6.QtCore import QObject, Signal, Slot, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Version manifest URL.  Override with NIMBUS_VERSION_URL for development.
DEFAULT_VERSION_URL = "https://nimbus.app/version.json"
VERSION_URL = os.environ.get("NIMBUS_VERSION_URL", DEFAULT_VERSION_URL)

# Default download page if manifest doesn't specify one.
DEFAULT_DOWNLOAD_URL = "https://github.com/owenpkent/Nimbus-Adaptive-Controller/releases/latest"

# How long after startup (ms) to check for updates — slight delay to let the
# UI finish loading before hitting the network.
STARTUP_CHECK_DELAY_MS = 10_000  # 10 seconds

# How often to re-check (ms) while the app is running.  Default: every 6 hours.
RECHECK_INTERVAL_MS = 6 * 60 * 60 * 1000
REQUEST_DEADLINE_MS = 10_000
MAX_MANIFEST_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Version comparison helpers
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)"  # major.minor.patch
    r"(?:[-.]?(alpha|beta|dev|rc)\.?(\d+)?)?$",  # optional pre-release tag
    re.IGNORECASE,
)

# Pre-release ordering: dev < alpha < beta < rc < (stable)
_PRERELEASE_ORDER = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}


def parse_version(version_str: str) -> Tuple[int, int, int, int, int]:
    """
    Parse a semantic version string into a comparable tuple.

    Returns ``(major, minor, patch, pre_order, pre_num)`` where:
    - ``pre_order`` is 99 for stable releases, lower for pre-releases
    - ``pre_num`` is the pre-release number (0 if absent)

    Examples::

        parse_version("1.5.0")           → (1, 5, 0, 99, 0)
        parse_version("1.6.0-beta.2")    → (1, 6, 0, 2, 2)
        parse_version("v2.0.0-rc.1")     → (2, 0, 0, 3, 1)
    """
    m = _VERSION_RE.match(version_str.strip())
    if not m:
        raise ValueError(f"Cannot parse version string: {version_str!r}")

    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre_tag = (m.group(4) or "").lower()
    pre_num = int(m.group(5)) if m.group(5) else 0

    if pre_tag:
        pre_order = _PRERELEASE_ORDER.get(pre_tag, 99)
    else:
        pre_order = 99  # stable

    return (major, minor, patch, pre_order, pre_num)


def is_newer(remote: str, local: str) -> bool:
    """Return True if *remote* is a newer version than *local*.

    An unparseable *remote* (untrusted manifest data) is simply not newer.
    An unparseable *local* is our own broken version string, so it is logged
    rather than silently disabling every future update check.
    """
    try:
        local_parsed = parse_version(local)
    except ValueError:
        logger.warning("Local version %r is unparseable; update checks are disabled.", local)
        return False
    try:
        return parse_version(remote) > local_parsed
    except ValueError:
        return False


def is_below_minimum(current: str, minimum: str) -> bool:
    """Return True if *current* is below the minimum supported version.

    An unparseable *minimum* (untrusted manifest data) imposes no minimum.
    An unparseable *current* is our own broken version string, so it is
    logged rather than silently skipping the check.
    """
    try:
        current_parsed = parse_version(current)
    except ValueError:
        logger.warning("Current version %r is unparseable; minimum-version check skipped.", current)
        return False
    try:
        return current_parsed < parse_version(minimum)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# UpdateChecker
# ---------------------------------------------------------------------------

class UpdateChecker(QObject):
    """
    Non-blocking update checker for Nimbus Adaptive Controller.

    Signals
    -------
    updateAvailable(str, str, str)
        Emitted when a newer version is found.
        Arguments: ``(latest_version, download_url, release_notes)``.
    forceUpdateRequired(str, str)
        Emitted when the running version is below ``min_supported``.
        Arguments: ``(min_version, download_url)``.
    noUpdateAvailable()
        Emitted when the running version is already the latest.
    checkFailed(str)
        Emitted when the version check could not complete.
        Argument: error description.

    Parameters
    ----------
    config : ControllerConfig
        Application configuration object.  Reads:

        - ``updater.channel`` (str) — ``"stable"``, ``"beta"``, or ``"dev"``
        - ``updater.auto_check`` (bool) — whether to check on startup (default True)
    """

    updateAvailable = Signal(str, str, str)    # version, url, notes
    forceUpdateRequired = Signal(str, str)     # min_version, url
    noUpdateAvailable = Signal()
    checkFailed = Signal(str)

    def __init__(self, config: Any, parent: Optional[QObject] = None, *,
                 network_manager: Optional[QNetworkAccessManager] = None) -> None:
        super().__init__(parent)
        self._config = config

        # Read settings
        self._channel: str = config.get("updater.channel", "stable")
        self._auto_check: bool = bool(config.get("updater.auto_check", True))

        # Current running version
        self._current_version = self._get_current_version()

        # Latest known version info (populated after check)
        self._latest_version: Optional[str] = None
        self._download_url: str = DEFAULT_DOWNLOAD_URL
        self._release_notes: str = ""

        self._network = network_manager if network_manager is not None else QNetworkAccessManager(self)
        self._reply: Optional[QNetworkReply] = None
        self._response = bytearray()
        self._closed = False
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(REQUEST_DEADLINE_MS)
        self._deadline.timeout.connect(lambda: self._fail_request("Update request exceeded its deadline"))

        # Periodic re-check timer
        self._recheck_timer = QTimer(self)
        self._recheck_timer.setInterval(RECHECK_INTERVAL_MS)
        self._recheck_timer.timeout.connect(self.check)

        # Auto-check on startup (delayed)
        if self._auto_check:
            QTimer.singleShot(STARTUP_CHECK_DELAY_MS, self.check)
            self._recheck_timer.start()
            logger.info("Auto-update check enabled (channel=%s, current=%s).",
                        self._channel, self._current_version)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current_version(self) -> str:
        """The currently running application version."""
        return self._current_version

    @property
    def latest_version(self) -> Optional[str]:
        """The latest available version (None if not yet checked)."""
        return self._latest_version

    @property
    def channel(self) -> str:
        """The active update channel."""
        return self._channel

    @channel.setter
    def channel(self, value: str) -> None:
        if value in ("stable", "beta", "dev"):
            self._channel = value
            self._config.set("updater.channel", value)
            self._config.save_config()

    @property
    def has_update(self) -> bool:
        """Whether a newer version is available."""
        if self._latest_version:
            return is_newer(self._latest_version, self._current_version)
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @Slot()
    def check(self) -> None:
        """
        Start a non-blocking version check.

        Fetches the version manifest asynchronously. When complete,
        one of the signals is emitted: ``updateAvailable``,
        ``forceUpdateRequired``, ``noUpdateAvailable``, or ``checkFailed``.
        """
        if self._closed:
            return
        if self._reply is not None:
            logger.debug("Update check already in progress — skipping.")
            return

        logger.debug("Starting update check against %s (channel=%s).",
                      VERSION_URL, self._channel)

        request = QNetworkRequest(QUrl(VERSION_URL))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self._response.clear()
        self._reply = self._network.get(request)
        self._reply.setReadBufferSize(MAX_MANIFEST_BYTES + 1)
        reply = self._reply
        reply.readyRead.connect(lambda: self._read_response(reply))
        reply.finished.connect(lambda: self._finish_response(reply))
        self._deadline.start()

    def shutdown(self) -> None:
        """Cancel requests without waiting on network progress or a worker."""
        self._closed = True
        self._recheck_timer.stop()
        self._cancel_request()

    def _cancel_request(self) -> None:
        self._deadline.stop()
        reply, self._reply = self._reply, None
        self._response.clear()
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _fail_request(self, message: str) -> None:
        if self._reply is None:
            return
        self._cancel_request()
        self._on_check_error(message)

    def _read_response(self, reply: QNetworkReply) -> None:
        if self._closed or reply is not self._reply:
            return
        self._response.extend(bytes(reply.readAll()))
        if len(self._response) > MAX_MANIFEST_BYTES:
            self._fail_request("Update manifest is too large")

    def _finish_response(self, reply: QNetworkReply) -> None:
        if self._closed or reply is not self._reply:
            return
        self._read_response(reply)
        if reply is not self._reply:
            return
        self._deadline.stop()
        self._reply = None
        try:
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if reply.error() != QNetworkReply.NetworkError.NoError or status != 200:
                self._on_check_error("Version check failed: HTTP " + str(status))
                return
            manifest = json.loads(bytes(self._response))
            if not isinstance(manifest, dict):
                raise ValueError("Update manifest must be an object")
            self._on_manifest_received(manifest)
        except (ValueError, TypeError, AttributeError) as exc:
            self._on_check_error("Invalid update manifest: " + str(exc))
        finally:
            self._response.clear()
            reply.deleteLater()

    @Slot()
    def open_download_page(self) -> None:
        """Open the download page for the latest version in the system browser."""
        webbrowser.open(self._download_url)

    @Slot()
    def dismiss(self) -> None:
        """
        Dismiss the current update notification.

        The user won't be reminded again until the next app launch or until
        a newer version is published.
        """
        self._config.set("updater.dismissed_version", self._latest_version)
        self._config.save_config()
        logger.debug("Update notification dismissed for %s.", self._latest_version)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_current_version(self) -> str:
        try:
            from . import __version__
            return __version__
        except Exception:
            return "0.0.0"

    def _on_manifest_received(self, manifest: Dict[str, Any]) -> None:
        """Process the downloaded version manifest."""
        if self._closed or not manifest:
            return

        # Determine the target version for the active channel
        channels = manifest.get("channels", {})
        if self._channel in channels:
            target_version = channels[self._channel]
        else:
            target_version = manifest.get("latest", "")

        if not target_version:
            self.checkFailed.emit("Version manifest missing 'latest' field.")
            return

        self._latest_version = target_version
        self._download_url = manifest.get("download_url", DEFAULT_DOWNLOAD_URL)
        self._release_notes = manifest.get("release_notes", "")

        # Check if current version is below minimum supported
        min_supported = manifest.get("min_supported", "0.0.0")
        if is_below_minimum(self._current_version, min_supported):
            logger.warning("Current version %s is below minimum %s — force update required.",
                           self._current_version, min_supported)
            self.forceUpdateRequired.emit(min_supported, self._download_url)
            return

        # Check if update is available
        if is_newer(target_version, self._current_version):
            # Skip if user already dismissed this version
            dismissed = self._config.get("updater.dismissed_version", "")
            if dismissed == target_version:
                logger.debug("Update %s already dismissed by user.", target_version)
                self.noUpdateAvailable.emit()
                return

            logger.info("Update available: %s → %s",
                        self._current_version, target_version)
            self.updateAvailable.emit(
                target_version, self._download_url, self._release_notes
            )
        else:
            logger.debug("No update available (current=%s, latest=%s).",
                         self._current_version, target_version)
            self.noUpdateAvailable.emit()

    def _on_check_error(self, message: str) -> None:
        """Handle version check failure (non-critical)."""
        if self._closed:
            return
        logger.debug("Update check failed: %s", message)
        self.checkFailed.emit(message)
