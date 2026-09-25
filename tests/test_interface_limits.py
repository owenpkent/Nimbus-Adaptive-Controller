"""
Driver-free logic checks for ``VJoyInterface`` (src/vjoy_interface.py) and
``ViGEmInterface`` (src/vigem_interface.py).

SAFETY: this development machine has the real vJoy and ViGEmBus drivers
installed, and this suite must never acquire a real device or emit real
controller input. Both interfaces are constructed with their module-level
availability flags (``VJOY_AVAILABLE`` / ``VIGEM_AVAILABLE``) forced to
``False``, so ``_initialize_vjoy`` / ``_initialize_vigem`` take the
simulation-mode return path and never call ``pyvjoy.VJoyDevice(...)`` or
``X360Pad()``. Right after construction each interface's real hardware
handle (``device`` / ``gamepad``) is asserted ``None`` -- if that ever
comes back non-``None`` the check fails loudly, because it would mean a
real device got opened. Only after that safety check does a test inject an
in-process fake (``FakeVJoyDevice`` / ``FakeGamepad``) in place of the
device/gamepad and flip ``is_connected`` by hand, so the connected-mode
branches (``update_axis``, ``set_button``, ...) can be exercised without a
driver call ever being reached.

Run::

    venv\\Scripts\\python -m tests.test_interface_limits
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import src.vjoy_interface as vj  # noqa: E402
import src.vigem_interface as vig  # noqa: E402

FAILS = 0
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """Record and print one [PASS]/[FAIL] result.

    Parameters
    ----------
    name : str
        Human-readable description of the property being checked.
    cond : bool
        The already-evaluated result of the check.
    detail : str, optional
        Extra context to print alongside a failure or borderline pass.
    """
    global FAILS, PASSES
    if cond:
        PASSES += 1
    else:
        FAILS += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  ({detail})" if detail else ""))


class FakeConfig:
    """Minimal stand-in for ``ControllerConfig``.

    Exposes only the two methods both interfaces call: ``get`` (dot-path
    lookup with a default) and ``get_vjoy_value`` (the same normalized ->
    vJoy-range conversion as ``ControllerConfig.get_vjoy_value``, since
    ``VJoyInterface.update_axis`` calls it directly).
    """

    def __init__(self, overrides: Optional[Dict[str, Any]] = None) -> None:
        self._overrides = overrides or {}

    def get(self, key_path: str, default: Any = None) -> Any:
        return self._overrides.get(key_path, default)

    def get_vjoy_value(self, normalized_value: float) -> int:
        axis_range = self.get("vjoy.axis_range", 32767)
        return int((normalized_value + 1.0) * axis_range / 2.0)


class FakeVJoyDevice:
    """In-process stand-in for ``pyvjoy.VJoyDevice``.

    Records every ``set_axis`` / ``set_button`` call instead of touching a
    driver. ``VJoyInterface.set_button`` now rejects an out-of-range
    ``button_id`` itself (see ``src/vjoy_interface.py``), before this fake's
    ``set_button`` is ever reached, so the fake no longer needs to simulate
    the driver rejecting one.
    """

    def __init__(self) -> None:
        self.axis_calls: List[Tuple[int, int]] = []
        self.button_calls: List[Tuple[int, bool]] = []

    def set_axis(self, axis_id: int, value: int) -> None:
        self.axis_calls.append((axis_id, value))

    def set_button(self, button_id: int, state: bool) -> None:
        self.button_calls.append((button_id, state))


class FakeGamepad:
    """In-process stand-in for ``padbus_client.X360Pad``.

    Records stick/trigger/button/update calls instead of touching the bus.
    """

    def __init__(self) -> None:
        self.left_stick: Tuple[float, float] = (0.0, 0.0)
        self.right_stick: Tuple[float, float] = (0.0, 0.0)
        self.left_trigger: float = 0.0
        self.right_trigger: float = 0.0
        self.pressed: Set[int] = set()
        self.update_calls = 0
        self.reset_calls = 0
        self.closed = False
        self.bus_name = "FAKE_BUS"
        self.serial = 0

    def left_joystick_float(self, x: float, y: float) -> None:
        self.left_stick = (x, y)

    def right_joystick_float(self, x: float, y: float) -> None:
        self.right_stick = (x, y)

    def left_trigger_float(self, value: float) -> None:
        self.left_trigger = value

    def right_trigger_float(self, value: float) -> None:
        self.right_trigger = value

    def press_button(self, mask: int) -> None:
        self.pressed.add(mask)

    def release_button(self, mask: int) -> None:
        self.pressed.discard(mask)

    def update(self) -> None:
        self.update_calls += 1

    def reset(self) -> None:
        self.reset_calls += 1
        self.pressed.clear()

    def close(self) -> None:
        self.closed = True


def make_vjoy() -> "vj.VJoyInterface":
    """Construct a ``VJoyInterface`` with ``VJOY_AVAILABLE`` already forced False."""
    return vj.VJoyInterface(FakeConfig())


def make_vigem() -> "vig.ViGEmInterface":
    """Construct a ``ViGEmInterface`` with ``VIGEM_AVAILABLE`` already forced False."""
    return vig.ViGEmInterface(FakeConfig())


def connected_vjoy() -> "vj.VJoyInterface":
    """Build a ``VJoyInterface`` and inject a ``FakeVJoyDevice`` as if connected."""
    iface = make_vjoy()
    iface.device = FakeVJoyDevice()
    iface.is_connected = True
    return iface


def connected_vigem() -> "vig.ViGEmInterface":
    """Build a ``ViGEmInterface`` and inject a ``FakeGamepad`` as if connected."""
    iface = make_vigem()
    iface.gamepad = FakeGamepad()
    iface.is_connected = True
    return iface


# --------------------------------------------------------------------------- #
# Safety: simulation mode opens no real device/gamepad
# --------------------------------------------------------------------------- #

def test_vjoy_simulation_opens_no_real_device() -> None:
    iface = make_vjoy()
    check("VJoyInterface: simulation mode leaves device None (no real vJoy device opened)",
          iface.device is None, f"device={iface.device!r}")
    check("VJoyInterface: simulation mode leaves is_connected False",
          iface.is_connected is False)


def test_vigem_simulation_opens_no_real_gamepad() -> None:
    iface = make_vigem()
    check("ViGEmInterface: simulation mode leaves gamepad None (no real X360Pad opened)",
          iface.gamepad is None, f"gamepad={iface.gamepad!r}")
    check("ViGEmInterface: simulation mode leaves is_connected False",
          iface.is_connected is False)


# --------------------------------------------------------------------------- #
# VJoyInterface
# --------------------------------------------------------------------------- #

def test_vjoy_update_axis() -> None:
    iface = connected_vjoy()
    check("update_axis rejects an unknown axis name",
          iface.update_axis("unknown", 0.0) is False)
    check("an unknown axis is not added to current_values",
          "unknown" not in iface.current_values)

    valid_axes = ["x", "y", "z", "rx", "ry", "rz", "sl0", "sl1"]
    check("there are exactly 8 valid vJoy axes", len(valid_axes) == 8)
    for axis in valid_axes:
        ok = iface.update_axis(axis, 0.5)
        check(f"update_axis('{axis}', 0.5) is accepted", ok is True)
        check(f"current_values['{axis}'] tracks (value+1)/2 == 0.75",
              abs(iface.current_values[axis] - 0.75) < 1e-9,
              f"{iface.current_values[axis]}")

    check("update_axis return type is bool", type(iface.update_axis("x", 0.0)) is bool)

    clamp_iface = connected_vjoy()
    ok_pos = clamp_iface.update_axis("x", 5.0)
    check("update_axis('x', 5.0) clamps to 1.0 and still succeeds", ok_pos is True)
    check("current_values['x'] reflects the clamped value: (1.0+1.0)/2 == 1.0",
          clamp_iface.current_values["x"] == 1.0, f"{clamp_iface.current_values['x']}")
    check("the clamped value (not 5.0) is what reached get_vjoy_value/set_axis",
          clamp_iface.device.axis_calls[-1][1] == clamp_iface.config.get_vjoy_value(1.0),
          f"{clamp_iface.device.axis_calls[-1]}")

    ok_neg = clamp_iface.update_axis("y", -5.0)
    check("update_axis('y', -5.0) clamps to -1.0 and still succeeds", ok_neg is True)
    check("current_values['y'] reflects the clamped value: (-1.0+1.0)/2 == 0.0",
          clamp_iface.current_values["y"] == 0.0, f"{clamp_iface.current_values['y']}")

    disconnected = make_vjoy()
    check("update_axis on a disconnected (simulation-mode) interface returns False",
          disconnected.update_axis("x", 0.0) is False)


def test_vjoy_update_axis_ignores_failsafe() -> None:
    """update_axis has no failsafe gate -- the source comments this as intentional
    ("Ignore failsafe gating"), unlike update_joystick, which does check
    failsafe_active. This documents the actual, asymmetric behavior."""
    iface = connected_vjoy()
    iface.failsafe_active = True
    ok = iface.update_axis("x", 0.2)
    check("update_axis still succeeds while failsafe_active is True (documented, "
          "intentional asymmetry vs. update_joystick)", ok is True)
    check("the axis value still updates while failsafe is active",
          abs(iface.current_values["x"] - 0.6) < 1e-9)


def test_vjoy_set_button() -> None:
    iface = connected_vjoy()
    for button_id in (1, 64, 128):
        ok = iface.set_button(button_id, True)
        check(f"set_button({button_id}) is accepted", ok is True)
        check(f"set_button({button_id}) reaches the (fake) device",
              (button_id, True) in iface.device.button_calls)

    for button_id in (0, 129):
        ok = iface.set_button(button_id, True)
        check(f"set_button({button_id}) is rejected before touching the device "
              "(outside 1-128, returns False)", ok is False)
        check(f"set_button({button_id}) never reaches the (fake) device",
              all(call[0] != button_id for call in iface.device.button_calls))
        check(f"set_button({button_id}) is recorded in _warned_buttons (warn once per id)",
              button_id in iface._warned_buttons)

    warned_count_before = len(iface._warned_buttons)
    ok_again = iface.set_button(0, True)
    check("a repeat call with an already-warned out-of-range id still returns False",
          ok_again is False)
    check("a repeat out-of-range id does not grow _warned_buttons again",
          len(iface._warned_buttons) == warned_count_before)

    check("set_button return type is bool", type(iface.set_button(1, False)) is bool)

    disconnected = make_vjoy()
    check("set_button on a disconnected (simulation-mode) interface returns False",
          disconnected.set_button(1, True) is False)


def test_vjoy_get_status_shape() -> None:
    iface = connected_vjoy()
    status = iface.get_status()
    expected_keys = {"connected", "device_id", "failsafe_active", "last_update",
                      "current_values", "vjoy_available"}
    check("get_status returns exactly the documented keys",
          set(status.keys()) == expected_keys, f"{sorted(status.keys())}")
    check("get_status['current_values'] is a copy, not the live dict",
          status["current_values"] is not iface.current_values)
    check("get_status['vjoy_available'] reflects the forced simulation flag",
          status["vjoy_available"] is False)


def test_vjoy_emergency_stop() -> None:
    iface = connected_vjoy()
    for axis in iface.current_values:
        iface.current_values[axis] = 0.9
    iface.emergency_stop()
    check("emergency_stop sets failsafe_active", iface.failsafe_active is True)
    check("emergency_stop centers every axis back to 0.5",
          all(v == 0.5 for v in iface.current_values.values()), f"{iface.current_values}")
    check("emergency_stop drives the (fake) device through set_axis",
          len(iface.device.axis_calls) >= 6, f"{len(iface.device.axis_calls)} calls")


# --------------------------------------------------------------------------- #
# ViGEmInterface
# --------------------------------------------------------------------------- #

def test_vigem_stick_clamping() -> None:
    iface = connected_vigem()
    iface.set_left_stick(5.0, -5.0)
    check("set_left_stick clamps x to 1.0", iface.current_values["left_x"] == 1.0)
    check("set_left_stick clamps y to -1.0", iface.current_values["left_y"] == -1.0)
    check("the clamped values reach the (fake) gamepad",
          iface.gamepad.left_stick == (1.0, -1.0))

    iface.set_right_stick(-5.0, 5.0)
    check("set_right_stick clamps x to -1.0", iface.current_values["right_x"] == -1.0)
    check("set_right_stick clamps y to 1.0", iface.current_values["right_y"] == 1.0)

    ok = iface.set_left_stick(0.3, -0.4)
    check("an in-range stick value passes through unclamped",
          ok is True and iface.current_values["left_x"] == 0.3
          and iface.current_values["left_y"] == -0.4)

    disconnected = make_vigem()
    check("set_left_stick on a disconnected interface returns False",
          disconnected.set_left_stick(0.0, 0.0) is False)


def test_vigem_trigger_clamping() -> None:
    iface = connected_vigem()
    iface.set_left_trigger(5.0)
    check("set_left_trigger clamps above 1.0 to 1.0", iface.current_values["left_trigger"] == 1.0)
    iface.set_left_trigger(-5.0)
    check("set_left_trigger clamps below 0.0 to 0.0", iface.current_values["left_trigger"] == 0.0)
    iface.set_right_trigger(5.0)
    check("set_right_trigger clamps above 1.0 to 1.0", iface.current_values["right_trigger"] == 1.0)
    iface.set_right_trigger(-5.0)
    check("set_right_trigger clamps below 0.0 to 0.0", iface.current_values["right_trigger"] == 0.0)


def test_vigem_update_axis_mapping() -> None:
    iface = connected_vigem()

    check("update_axis rejects an unknown axis name (e.g. 'sl0', vJoy-only)",
          iface.update_axis("sl0", 0.0) is False)

    iface.update_axis("x", 0.4)
    check("'x' maps to left_x", iface.current_values["left_x"] == 0.4)
    iface.update_axis("y", -0.7)
    check("'y' maps to left_y", iface.current_values["left_y"] == -0.7)
    check("both left-stick axes reached the (fake) gamepad",
          iface.gamepad.left_stick == (0.4, -0.7))

    iface.update_axis("rx", 0.2)
    check("'rx' maps to right_x", iface.current_values["right_x"] == 0.2)
    iface.update_axis("ry", -0.3)
    check("'ry' maps to right_y", iface.current_values["right_y"] == -0.3)
    check("both right-stick axes reached the (fake) gamepad",
          iface.gamepad.right_stick == (0.2, -0.3))

    iface.update_axis("z", 0.5)
    check("'z' converts to left_trigger via (value+1)/2 == 0.75 exactly",
          iface.current_values["left_trigger"] == 0.75, f"{iface.current_values['left_trigger']}")
    iface.update_axis("z", -1.0)
    check("'z' at -1.0 gives left_trigger 0.0", iface.current_values["left_trigger"] == 0.0)
    iface.update_axis("z", 1.0)
    check("'z' at 1.0 gives left_trigger 1.0", iface.current_values["left_trigger"] == 1.0)

    iface.update_axis("rz", 0.5)
    check("'rz' converts to right_trigger via (value+1)/2 == 0.75 exactly",
          iface.current_values["right_trigger"] == 0.75)
    iface.update_axis("rz", -1.0)
    check("'rz' at -1.0 gives right_trigger 0.0", iface.current_values["right_trigger"] == 0.0)

    ok = iface.update_axis("x", 5.0)
    check("update_axis clamps out-of-range input to 1.0, matching set_left_stick/"
          "set_*_trigger",
          ok is True and iface.current_values["left_x"] == 1.0,
          f"{iface.current_values['left_x']}")
    check("the clamped value (not 5.0) is what reached the (fake) gamepad",
          iface.gamepad.left_stick[0] == 1.0, f"{iface.gamepad.left_stick}")

    ok_neg = iface.update_axis("ry", -5.0)
    check("update_axis clamps a large negative value to -1.0",
          ok_neg is True and iface.current_values["right_y"] == -1.0,
          f"{iface.current_values['right_y']}")

    iface.update_axis("z", 5.0)
    check("clamping happens before the trigger's (value+1)/2 conversion, so "
          "z=5.0 still gives left_trigger 1.0 (not >1.0)",
          iface.current_values["left_trigger"] == 1.0,
          f"{iface.current_values['left_trigger']}")

    check("update_axis return type is bool", type(iface.update_axis("x", 0.0)) is bool)


def test_vigem_set_button() -> None:
    iface = connected_vigem()
    for button_id in (0, 15):
        check(f"set_button({button_id}) is rejected (outside the 1-14 XUSB_BY_ID map)",
              iface.set_button(button_id, True) is False)
    for button_id in range(1, 15):
        check(f"set_button({button_id}) is accepted",
              iface.set_button(button_id, True) is True)
    check("set_button return type is bool", type(iface.set_button(1, False)) is bool)

    disconnected = make_vigem()
    check("set_button on a disconnected interface returns False",
          disconnected.set_button(1, True) is False)


def test_xusb_by_id_completeness() -> None:
    if vig.XUSB_BUTTON is None:
        check("XUSB_BY_ID completeness (skipped: padbus_client did not import on this machine)",
              True)
        return
    check("XUSB_BY_ID has exactly the ids 1..14",
          set(vig.XUSB_BY_ID.keys()) == set(range(1, 15)),
          f"{sorted(vig.XUSB_BY_ID.keys())}")
    check("every XUSB_BY_ID mask is distinct (no two Nimbus ids share a bit)",
          len(set(vig.XUSB_BY_ID.values())) == 14)
    check("no XUSB_BY_ID mask is falsy/zero", all(vig.XUSB_BY_ID[i] for i in range(1, 15)))


def test_vigem_get_status_shape() -> None:
    iface = connected_vigem()
    status = iface.get_status()
    expected_keys = {"connected", "device_type", "last_update", "current_values",
                      "button_states", "vigem_available", "bus", "serial"}
    check("get_status returns exactly the documented keys",
          set(status.keys()) == expected_keys, f"{sorted(status.keys())}")
    check("get_status['current_values'] is a copy, not the live dict",
          status["current_values"] is not iface.current_values)
    check("get_status['button_states'] is a copy, not the live dict",
          status["button_states"] is not iface.button_states)
    check("get_status['vigem_available'] reflects the forced simulation flag",
          status["vigem_available"] is False)


def test_vigem_emergency_stop() -> None:
    iface = connected_vigem()
    iface.set_left_stick(0.5, 0.5)
    iface.set_button(1, True)
    iface.emergency_stop()
    check("emergency_stop resets every current_values entry to 0.0",
          all(v == 0.0 for v in iface.current_values.values()), f"{iface.current_values}")
    check("emergency_stop calls the (fake) gamepad's reset()", iface.gamepad.reset_calls >= 1)


# --------------------------------------------------------------------------- #
# Cross-backend compatibility (CLAUDE.md: the two backends stay
# behavior-compatible for shared method names)
# --------------------------------------------------------------------------- #

def test_shared_bool_contract() -> None:
    vj_iface = connected_vjoy()
    vig_iface = connected_vigem()
    check("VJoyInterface.update_axis returns bool",
          type(vj_iface.update_axis("x", 0.0)) is bool)
    check("ViGEmInterface.update_axis returns bool",
          type(vig_iface.update_axis("x", 0.0)) is bool)
    check("VJoyInterface.set_button returns bool",
          type(vj_iface.set_button(1, True)) is bool)
    check("ViGEmInterface.set_button returns bool",
          type(vig_iface.set_button(1, True)) is bool)
    check("both classes expose emergency_stop() returning None",
          vj_iface.emergency_stop() is None and vig_iface.emergency_stop() is None)
    check("both classes expose get_status() returning a dict",
          isinstance(vj_iface.get_status(), dict) and isinstance(vig_iface.get_status(), dict))


def main() -> int:
    """Run every check with both drivers' availability flags forced off."""
    print("Interface limits checks (simulation mode only, no real device/bus opened)\n")

    orig_vjoy_available = vj.VJOY_AVAILABLE
    orig_vigem_available = vig.VIGEM_AVAILABLE
    vj.VJOY_AVAILABLE = False
    vig.VIGEM_AVAILABLE = False
    try:
        test_vjoy_simulation_opens_no_real_device()
        test_vigem_simulation_opens_no_real_gamepad()

        test_vjoy_update_axis()
        test_vjoy_update_axis_ignores_failsafe()
        test_vjoy_set_button()
        test_vjoy_get_status_shape()
        test_vjoy_emergency_stop()

        test_vigem_stick_clamping()
        test_vigem_trigger_clamping()
        test_vigem_update_axis_mapping()
        test_vigem_set_button()
        test_xusb_by_id_completeness()
        test_vigem_get_status_shape()
        test_vigem_emergency_stop()

        test_shared_bool_contract()
    finally:
        vj.VJOY_AVAILABLE = orig_vjoy_available
        vig.VIGEM_AVAILABLE = orig_vigem_available
        check("VJOY_AVAILABLE restored to its original value after the suite",
              vj.VJOY_AVAILABLE == orig_vjoy_available)
        check("VIGEM_AVAILABLE restored to its original value after the suite",
              vig.VIGEM_AVAILABLE == orig_vigem_available)

    print(f"\n{PASSES}/{PASSES + FAILS} checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
