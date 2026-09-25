"""
Driver-agnostic controller keep-alive pulse (Controller Mode Enforcement).

The core trick behind Full Game Mode on Windows (see :mod:`src.mouse_hider`)
is not the mouse hook, it is the gamepad: a game with dual input detection
switches to "controller mode" as soon as it sees gamepad activity, shows
gamepad prompts, and voluntarily stops capturing the mouse. This module is
that trick alone, written against the public interface API so it works with
any back end that exposes ``set_left_stick`` / ``set_button`` /
``current_values``:

* :class:`~src.vigem_interface.ViGEmInterface` (Windows, ViGEmBus)
* :class:`~src.uinput_interface.UInputXboxInterface` (Linux, uinput)

:mod:`src.bridge` uses this module on non-Windows platforms. Windows keeps
using :mod:`src.mouse_hider`, which bundles the same pulse with the Win32
mouse hook, ClipCursor release, and the ``Ctrl+Alt+F12`` emergency hotkey.

Mechanism
---------
* **Burst** on start: ten alternating left-stick deflections of 0.5 (above
  any deadzone) followed by a brief A-button press. Unambiguous "a gamepad
  is in use" for Unreal, Unity, and FromSoftware titles.
* **Keep-alive**: a tiny circular left-stick oscillation (amplitude 0.08,
  below typical 0.15-0.3 deadzones) at ``pulse_hz``. Each pulse saves the
  stick values the user is actually sending, writes the offset, and restores
  the real values in the same tick so live input is never clobbered.

Safety
------
There is no global hotkey on Linux (reading keyboards needs evdev access),
so the stop path is the Game Mode button in the UI, :func:`stop_controller_mode`,
or process exit. Stopping always re-centres the stick.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Dict, Optional

_active = False
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_interface: Any = None
_pulse_hz: int = 30
_status_callback: Optional[Callable[[bool], None]] = None

_stats_lock = threading.Lock()
_stats: Dict[str, Any] = {
    "pulses_sent": 0,
    "controller_bursts_sent": 0,
    "mode_started_at": 0.0,
}

PULSE_AMPLITUDE = 0.08
BURST_AMPLITUDE = 0.5
BUTTON_A = 1


def _saved_sticks(iface: Any) -> tuple:
    cv = getattr(iface, "current_values", {}) or {}
    return (
        float(cv.get("left_x", 0.0)), float(cv.get("left_y", 0.0)),
        float(cv.get("right_x", 0.0)), float(cv.get("right_y", 0.0)),
    )


def _transient(iface: Any, x: float, y: float) -> None:
    """Emit a left-stick position without claiming authorship of it.

    Falls back to ``set_left_stick`` for interfaces that predate the transient
    API, which is the old behaviour and the old race.
    """
    emit = getattr(iface, "emit_left_stick_transient", None)
    if emit is not None:
        emit(x, y)
    else:
        iface.set_left_stick(x, y)


def _restore(iface: Any) -> None:
    """Re-emit whatever the user currently commands, not a stale snapshot."""
    restore = getattr(iface, "restore_left_stick", None)
    if restore is not None:
        restore()
    else:
        lx, ly, _rx, _ry = _saved_sticks(iface)
        iface.set_left_stick(lx, ly)


def _send_burst(iface: Any, count: int = 10, delay: float = 0.016) -> None:
    """Send ``count`` alternating strong deflections plus an A press, then re-centre.

    The deflections are transient, so the burst never overwrites the commanded
    stick position. A release that lands mid-burst is therefore what gets
    re-emitted at the end, instead of being undone.

    While a pulse is running, every write re-reads ``active_interface()``, so
    a ``rebind_interface`` during the burst takes effect immediately instead
    of the burst finishing against an interface being torn down.
    """
    try:
        for i in range(count):
            live = active_interface() or iface
            val = BURST_AMPLITUDE * (1.0 if i % 2 == 0 else -1.0)
            _transient(live, val, 0.0)
            time.sleep(delay)
        live = active_interface() or iface
        try:
            live.set_button(BUTTON_A, True)
            time.sleep(0.05)
            live.set_button(BUTTON_A, False)
        except Exception:
            pass
        _restore(active_interface() or iface)
        with _stats_lock:
            _stats["controller_bursts_sent"] += 1
        print(f"[controller_pulse] Sent {count}-pulse controller burst (amplitude={BURST_AMPLITUDE} + A press)")
    except Exception as exc:
        print(f"[controller_pulse] Burst error: {exc}")


def active_interface() -> Any:
    """The interface the pulse is currently writing to, or None."""
    with _lock:
        return _interface if _active else None


def rebind_interface(interface: Any) -> bool:
    """Point a running pulse at a different interface.

    Used when the output device changes: the old interface is about to be
    destroyed, and a pulse still holding it would write to a closed device.

    Parameters
    ----------
    interface : Any
        A connected Xbox-style interface to continue against.

    Returns
    -------
    bool
        True if the running pulse was rebound. False if it is not running, or
        the replacement is unusable, in which case the caller should stop.
    """
    global _interface
    with _lock:
        if not _active:
            return False
        if interface is None or not getattr(interface, "is_connected", False):
            return False
        _interface = interface
        print("[controller_pulse] Pulse rebound to the new output interface")
        return True


def _pulse_loop() -> None:
    global _active
    iface = active_interface()
    if iface is None:
        print("[controller_pulse] No interface; pulse thread exiting")
        return
    interval = 1.0 / max(1, _pulse_hz)
    tick = 0
    print(f"[controller_pulse] Pulse loop started at {_pulse_hz}Hz (interval={interval * 1000:.1f}ms)")
    _send_burst(iface, count=10, delay=0.016)

    while _active:
        try:
            # Re-read every tick: the output device can change under us, and
            # writing to the retired interface would hit a closed device.
            current = active_interface()
            if current is not None and current is not iface:
                iface = current
            angle = (tick % 60) * (2.0 * math.pi / 60.0)
            micro_x = PULSE_AMPLITUDE * math.cos(angle)
            micro_y = PULSE_AMPLITUDE * math.sin(angle)
            pulse = getattr(iface, "pulse_left_stick", None)
            if pulse is not None:
                # Read, offset and restore under the interface's lock, so a
                # release cannot be undone by a pulse already in flight.
                pulse(micro_x, micro_y)
            else:
                lx, ly, _rx, _ry = _saved_sticks(iface)
                iface.set_left_stick(lx + micro_x, ly + micro_y)
                iface.set_left_stick(lx, ly)
            with _stats_lock:
                _stats["pulses_sent"] += 1
            tick += 1
        except Exception as exc:
            if _active:
                print(f"[controller_pulse] Pulse error: {exc}")
        time.sleep(interval)

    try:
        _restore(active_interface() or iface)
    except Exception:
        pass
    print("[controller_pulse] Pulse loop stopped")


def start_controller_mode(
    interface: Any,
    pulse_hz: int = 30,
    callback: Optional[Callable[[bool], None]] = None,
) -> bool:
    """Start the keep-alive pulse on ``interface``.

    Args:
        interface: A connected Xbox-style interface (``set_left_stick``,
            ``set_button``, ``current_values``).
        pulse_hz: Keep-alive frequency, clamped to 5..120.
        callback: Optional ``callback(active: bool)`` for status changes.

    Returns:
        ``True`` if the pulse thread was started (or was already running).
    """
    global _active, _thread, _interface, _pulse_hz, _status_callback
    with _lock:
        if _active:
            print("[controller_pulse] Controller mode already active")
            return True
        if interface is None or not getattr(interface, "is_connected", False):
            print("[controller_pulse] No connected gamepad interface; cannot start")
            return False
        _interface = interface
        _pulse_hz = max(5, min(120, int(pulse_hz)))
        _status_callback = callback
        _active = True
        with _stats_lock:
            _stats["pulses_sent"] = 0
            _stats["controller_bursts_sent"] = 0
            _stats["mode_started_at"] = time.time()
    _thread = threading.Thread(target=_pulse_loop, daemon=True, name="ControllerPulse")
    _thread.start()
    print(f"[controller_pulse] Controller Mode started (pulse={_pulse_hz}Hz)")
    if callback:
        callback(True)
    return True


def stop_controller_mode() -> None:
    """Stop the keep-alive pulse and re-centre the stick."""
    global _active, _thread, _interface, _status_callback
    with _lock:
        if not _active:
            return
        _active = False
    if _thread and _thread.is_alive():
        _thread.join(timeout=1.0)
    _thread = None
    with _stats_lock:
        elapsed = time.time() - _stats["mode_started_at"]
        print(f"[controller_pulse] Controller Mode stopped after {elapsed:.1f}s "
              f"(pulses={_stats['pulses_sent']}, bursts={_stats['controller_bursts_sent']})")
    cb = _status_callback
    _status_callback = None
    _interface = None
    if cb:
        cb(False)


def is_controller_mode_active() -> bool:
    """Return ``True`` while the pulse thread is running."""
    return _active


def send_controller_burst(interface: Any = None, count: int = 15) -> None:
    """Send a one-shot burst without starting the keep-alive.

    Args:
        interface: Interface to drive; defaults to the active pulse interface.
        count: Number of deflections in the burst.
    """
    iface = interface or _interface
    if iface is None:
        print("[controller_pulse] No interface available for burst")
        return
    _send_burst(iface, count=count)


def get_controller_mode_stats() -> Dict[str, Any]:
    """Return a copy of the pulse statistics plus ``active`` and ``pulse_hz``."""
    with _stats_lock:
        result = dict(_stats)
    result["active"] = _active
    result["pulse_hz"] = _pulse_hz
    return result
