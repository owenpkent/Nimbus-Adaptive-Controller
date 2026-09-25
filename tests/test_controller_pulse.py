"""
Fast-suite checks for the driver-agnostic keep-alive pulse in
``src/controller_pulse.py``.

Pure Python: no Qt, no uinput, no hardware. ``tests/test_linux_input_safety.py``
already covers the pulse-versus-release race (a release landing mid-pulse or
mid-burst must not be undone), so that race is not re-tested here. This file
instead covers the module's other public contract:

- ``pulse_hz`` clamped to ``[5, 120]`` in :func:`start_controller_mode`.
- The start/stop lifecycle: ``is_controller_mode_active()``, the
  ``callback(bool)`` calls, a redundant start while already active, and a
  redundant stop.
- :func:`rebind_interface`: its guard conditions, and that a running pulse's
  writes actually move to the new interface.
- :func:`send_controller_burst`: the alternating +/-``BURST_AMPLITUDE``
  deflection pattern and that the commanded stick (``current_values``) is
  never overwritten by the transient burst.
- :func:`get_controller_mode_stats`: its keys, and that ``pulses_sent`` grows
  while the pulse is active.

The module keeps its state as globals rather than in a class, so tests run
sequentially in one process and each one leaves the pulse stopped (in a
``finally``) before the next starts.

Run::

    python -m tests.test_controller_pulse
"""
from __future__ import annotations

import os
import sys
import time
from typing import Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from src import controller_pulse  # noqa: E402

PASSES = 0
FAILS = 0


def check(label: str, condition: bool) -> None:
    """Record and print one pass/fail line, numpy-doc style for consistency.

    Parameters
    ----------
    label : str
        What the check asserts.
    condition : bool
        The already-evaluated result.
    """
    global PASSES, FAILS
    if condition:
        PASSES += 1
        print(f"  [PASS] {label}")
    else:
        FAILS += 1
        print(f"  [FAIL] {label}")


class FakeInterface:
    """A minimal Xbox-style interface that records writes instead of emitting them.

    Implements the slice of the interface contract ``controller_pulse``
    depends on: ``current_values``, ``set_left_stick`` (commanded, updates
    ``current_values``), ``emit_left_stick_transient`` /
    ``restore_left_stick`` / ``pulse_left_stick`` (transient, never touch
    ``current_values``), and ``set_button``. This mirrors the commanded-versus
    -transient split ``UInputXboxInterface`` implements for real.
    """

    def __init__(self, connected: bool = True) -> None:
        self.is_connected = connected
        self.current_values: Dict[str, float] = {
            "left_x": 0.0, "left_y": 0.0, "right_x": 0.0, "right_y": 0.0,
        }
        self.stick_writes: List[Tuple[str, float, float]] = []
        self.button_events: List[Tuple[int, bool]] = []

    def set_left_stick(self, x: float, y: float) -> bool:
        self.current_values["left_x"], self.current_values["left_y"] = x, y
        self.stick_writes.append(("commanded", x, y))
        return True

    def emit_left_stick_transient(self, x: float, y: float) -> bool:
        self.stick_writes.append(("transient", x, y))
        return True

    def restore_left_stick(self) -> bool:
        return self.emit_left_stick_transient(
            self.current_values["left_x"], self.current_values["left_y"])

    def pulse_left_stick(self, dx: float, dy: float) -> bool:
        x, y = self.current_values["left_x"], self.current_values["left_y"]
        ok = self.emit_left_stick_transient(x + dx, y + dy)
        return self.emit_left_stick_transient(x, y) and ok

    def set_button(self, button_id: int, pressed: bool) -> bool:
        self.button_events.append((button_id, pressed))
        return True

    def transients(self) -> List[Tuple[float, float]]:
        """All transient (x, y) writes, in order."""
        return [(x, y) for kind, x, y in self.stick_writes if kind == "transient"]

    def last_transient(self) -> Optional[Tuple[float, float]]:
        vals = self.transients()
        return vals[-1] if vals else None


def _ensure_stopped() -> None:
    if controller_pulse.is_controller_mode_active():
        controller_pulse.stop_controller_mode()


def test_pulse_hz_clamping() -> None:
    print("\npulse_hz is clamped to [5, 120]")
    try:
        iface = FakeInterface()
        controller_pulse.start_controller_mode(iface, pulse_hz=1)
        check("below the floor clamps to 5",
              controller_pulse.get_controller_mode_stats()["pulse_hz"] == 5)
        controller_pulse.stop_controller_mode()

        iface2 = FakeInterface()
        controller_pulse.start_controller_mode(iface2, pulse_hz=500)
        check("above the ceiling clamps to 120",
              controller_pulse.get_controller_mode_stats()["pulse_hz"] == 120)
        controller_pulse.stop_controller_mode()

        iface3 = FakeInterface()
        controller_pulse.start_controller_mode(iface3, pulse_hz=45)
        check("an in-range value passes through unchanged",
              controller_pulse.get_controller_mode_stats()["pulse_hz"] == 45)
    finally:
        _ensure_stopped()


def test_start_requires_a_connected_interface() -> None:
    print("\nstart_controller_mode refuses a disconnected or missing interface")
    try:
        disconnected = FakeInterface(connected=False)
        started = controller_pulse.start_controller_mode(disconnected, pulse_hz=30)
        check("a disconnected interface is refused", started is False)
        check("mode is not active after a refused start",
              not controller_pulse.is_controller_mode_active())

        started_none = controller_pulse.start_controller_mode(None, pulse_hz=30)
        check("a None interface is refused", started_none is False)
    finally:
        _ensure_stopped()


def test_start_stop_lifecycle_and_callback() -> None:
    print("\nstart/stop toggle activity and fire the status callback")
    calls: List[bool] = []
    try:
        iface = FakeInterface()
        started = controller_pulse.start_controller_mode(
            iface, pulse_hz=60, callback=calls.append)
        check("start reports success", started is True)
        check("mode is active immediately after start",
              controller_pulse.is_controller_mode_active())
        check("the callback fired with True on start", calls == [True])

        controller_pulse.stop_controller_mode()
        check("mode is inactive after stop",
              not controller_pulse.is_controller_mode_active())
        check("the callback fired with False on stop", calls == [True, False])

        controller_pulse.stop_controller_mode()
        check("a second stop is a safe no-op",
              not controller_pulse.is_controller_mode_active())
        check("the redundant stop does not re-fire the callback",
              calls == [True, False])
    finally:
        _ensure_stopped()


def test_start_while_already_active_is_a_noop() -> None:
    print("\na second start while active does not replace the running interface")
    try:
        iface_a = FakeInterface()
        iface_b = FakeInterface()
        controller_pulse.start_controller_mode(iface_a, pulse_hz=60)
        check("the first start is driving iface_a",
              controller_pulse.active_interface() is iface_a)

        again = controller_pulse.start_controller_mode(iface_b, pulse_hz=90)
        check("the redundant start still reports success", again is True)
        check("the active interface is unchanged by the redundant start",
              controller_pulse.active_interface() is iface_a)
        check("pulse_hz from the redundant start is ignored",
              controller_pulse.get_controller_mode_stats()["pulse_hz"] == 60)
    finally:
        _ensure_stopped()


def test_rebind_interface_guards() -> None:
    print("\nrebind_interface's guard conditions")
    try:
        iface = FakeInterface()
        check("rebind is refused while no pulse is running",
              controller_pulse.rebind_interface(iface) is False)

        controller_pulse.start_controller_mode(iface, pulse_hz=60)
        check("rebind to None is refused",
              controller_pulse.rebind_interface(None) is False)
        check("rebind to a disconnected interface is refused",
              controller_pulse.rebind_interface(FakeInterface(connected=False)) is False)

        other = FakeInterface()
        check("rebind to a valid interface succeeds",
              controller_pulse.rebind_interface(other) is True)
        check("active_interface reflects the rebind",
              controller_pulse.active_interface() is other)
    finally:
        _ensure_stopped()


def test_pulses_land_on_new_interface_after_rebind() -> None:
    print("\na running pulse actually moves its writes to the rebound interface")
    try:
        old_iface = FakeInterface()
        new_iface = FakeInterface()
        controller_pulse.start_controller_mode(old_iface, pulse_hz=120)
        time.sleep(0.08)
        check("the old interface received writes before the rebind",
              len(old_iface.stick_writes) > 0)

        controller_pulse.rebind_interface(new_iface)
        # The fixed ~0.21s startup burst (10 deflections + the A press) runs
        # synchronously against the interface captured when the thread
        # started, so it keeps writing to old_iface for a moment even after
        # the rebind. Sleep past it so the keep-alive loop gets ticks on
        # new_iface before we stop.
        time.sleep(0.3)
        controller_pulse.stop_controller_mode()

        check("the new interface received writes after the rebind",
              len(new_iface.stick_writes) > 0)
        check("the final restore-on-stop landed on the new interface, not the old one",
              new_iface.last_transient() is not None)
    finally:
        _ensure_stopped()


def test_send_controller_burst_pattern_and_restore() -> None:
    print("\nsend_controller_burst: alternating amplitude, A press, and the restore contract")
    try:
        iface = FakeInterface()
        iface.set_left_stick(0.3, -0.2)  # the user's own commanded position
        stats_before = controller_pulse.get_controller_mode_stats()["controller_bursts_sent"]

        controller_pulse.send_controller_burst(iface, count=4)

        pattern = iface.transients()[:4]
        expected = [
            (controller_pulse.BURST_AMPLITUDE, 0.0),
            (-controller_pulse.BURST_AMPLITUDE, 0.0),
            (controller_pulse.BURST_AMPLITUDE, 0.0),
            (-controller_pulse.BURST_AMPLITUDE, 0.0),
        ]
        check("the burst alternates +/-BURST_AMPLITUDE on x with y held at 0",
              pattern == expected)
        check("button A was pressed then released",
              iface.button_events == [(controller_pulse.BUTTON_A, True),
                                       (controller_pulse.BUTTON_A, False)])
        check("the commanded stick (current_values) is untouched by the burst",
              iface.current_values["left_x"] == 0.3
              and iface.current_values["left_y"] == -0.2)
        check("the burst restores the user's commanded position, not a burst value",
              iface.last_transient() == (0.3, -0.2))
        check("controller_bursts_sent incremented by exactly one",
              controller_pulse.get_controller_mode_stats()["controller_bursts_sent"]
              == stats_before + 1)
    finally:
        _ensure_stopped()


def test_send_controller_burst_defaults_to_the_active_interface() -> None:
    print("\nsend_controller_burst with no interface argument uses the active pulse target")
    try:
        iface = FakeInterface()
        controller_pulse.start_controller_mode(iface, pulse_hz=60)
        controller_pulse.send_controller_burst(count=2)
        check("the default-target burst landed on the active interface",
              len(iface.stick_writes) > 0)
    finally:
        _ensure_stopped()


def test_get_controller_mode_stats() -> None:
    print("\nget_controller_mode_stats: keys, and pulses_sent grows while active")
    try:
        stats_idle = controller_pulse.get_controller_mode_stats()
        check("expected keys are present",
              set(stats_idle) == {"pulses_sent", "controller_bursts_sent",
                                   "mode_started_at", "active", "pulse_hz"})
        check("stats report inactive before any start",
              stats_idle["active"] is False)

        iface = FakeInterface()
        controller_pulse.start_controller_mode(iface, pulse_hz=120)
        # 120Hz gives an ~8ms tick; this comfortably clears the fixed ~0.21s
        # startup burst (10 deflections + the A press) plus a few keep-alive
        # ticks.
        time.sleep(0.35)
        stats_running = controller_pulse.get_controller_mode_stats()
        check("stats report active while the pulse is running",
              stats_running["active"] is True)
        check("pulse_hz reflects the running rate", stats_running["pulse_hz"] == 120)
        check("pulses_sent has grown while active",
              stats_running["pulses_sent"] > 0)

        controller_pulse.stop_controller_mode()
        check("stats report inactive again after stop",
              controller_pulse.get_controller_mode_stats()["active"] is False)
    finally:
        _ensure_stopped()


def main() -> int:
    print("Controller pulse checks")
    test_pulse_hz_clamping()
    test_start_requires_a_connected_interface()
    test_start_stop_lifecycle_and_callback()
    test_start_while_already_active_is_a_noop()
    test_rebind_interface_guards()
    test_pulses_land_on_new_interface_after_rebind()
    test_send_controller_burst_pattern_and_restore()
    test_send_controller_burst_defaults_to_the_active_interface()
    test_get_controller_mode_stats()
    print(f"\n{PASSES}/{PASSES + FAILS} checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
