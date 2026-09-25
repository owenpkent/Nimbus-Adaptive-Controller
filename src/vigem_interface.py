"""
ViGEmBus interface wrapper for emulating Xbox 360 controllers.
This provides XInput-compatible virtual controllers that work with modern games
like No Man's Sky that don't support DirectInput (vJoy).

The pad itself comes from ``src/padbus_client.py``, a pure-Python client that
speaks the bus protocol directly. ``VIGEM_AVAILABLE`` is True only when a bus
device interface is present at import (driver installed and started), so a
machine without ViGEmBus runs in simulation mode, as it always has.
"""

import sys
import time
import threading
from typing import Optional, Dict, Any
from .config import ControllerConfig

try:
    from .padbus_client import X360Pad, XUSB_BUTTON, PadBusError, PADBUS_AVAILABLE
    VIGEM_AVAILABLE = PADBUS_AVAILABLE
    if VIGEM_AVAILABLE:
        print("[OK] Virtual gamepad bus found - Xbox 360 controller emulation available")
    elif sys.platform == "win32":
        print("Warning: no virtual gamepad bus present (ViGEmBus not installed or not started)")
        print("The Nimbus installer includes ViGEmBus; Xbox 360 emulation is off until it is installed")
    # On Linux the bridge uses uinput, so an absent ViGEmBus is expected and
    # saying so at every start would be noise.
except Exception as e:  # pragma: no cover - the client imports anywhere; this is belt and braces
    VIGEM_AVAILABLE = False
    X360Pad = None  # type: ignore[assignment,misc]
    XUSB_BUTTON = None  # type: ignore[assignment,misc]
    PadBusError = Exception  # type: ignore[assignment,misc]
    print(f"Warning: virtual gamepad client not available: {e}")


#: Nimbus button ids (1-based, the same ids the bridge and the profiles use) to
#: the report masks. The harness imports this so it presses what the app presses.
XUSB_BY_ID: Dict[int, int] = {} if XUSB_BUTTON is None else {
    1: XUSB_BUTTON.XUSB_GAMEPAD_A,
    2: XUSB_BUTTON.XUSB_GAMEPAD_B,
    3: XUSB_BUTTON.XUSB_GAMEPAD_X,
    4: XUSB_BUTTON.XUSB_GAMEPAD_Y,
    5: XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
    6: XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
    7: XUSB_BUTTON.XUSB_GAMEPAD_BACK,
    8: XUSB_BUTTON.XUSB_GAMEPAD_START,
    9: XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
    10: XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
    11: XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
    12: XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
    13: XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
    14: XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
}


class ViGEmInterface:
    """
    Wrapper for ViGEmBus Xbox 360 controller emulation.

    This creates a virtual Xbox 360 controller that games see as a real
    XInput device, solving compatibility issues with games like No Man's Sky.
    """

    def __init__(self, config: ControllerConfig):
        """
        Initialize the ViGEm interface.

        Args:
            config: Configuration manager instance
        """
        self.config = config
        self.gamepad: Optional[Any] = None
        self.is_connected = False
        self.last_update_time = 0.0
        self.update_rate = config.get("vjoy.update_rate", 60)

        # Current axis values (normalized -1.0 to 1.0)
        self.current_values = {
            'left_x': 0.0,
            'left_y': 0.0,
            'right_x': 0.0,
            'right_y': 0.0,
            'left_trigger': 0.0,
            'right_trigger': 0.0
        }

        # Button states
        self.button_states: Dict[int, bool] = {}

        # Initialize ViGEm connection
        self._initialize_vigem()

    def _initialize_vigem(self) -> None:
        """Initialize ViGEmBus Xbox 360 controller."""
        if not VIGEM_AVAILABLE:
            print("ViGEm interface not available - running in simulation mode")
            print("\nTo enable Xbox controller emulation:")
            print("1. Install the ViGEmBus driver (the Nimbus installer includes it)")
            print("2. Restart this application")
            return

        try:
            print("Creating virtual Xbox 360 controller...")
            self.gamepad = X360Pad()
            self.is_connected = True
            print(f"[OK] Virtual Xbox 360 controller created on {self.gamepad.bus_name} "
                  f"(pad serial {self.gamepad.serial})")
            print("Games should now see this as 'Xbox 360 Controller for Windows'")

            # Reset to center position
            self._reset_axes()

        except Exception as e:
            print(f"[ERROR] Failed to create virtual Xbox controller: {e}")
            print("\nTroubleshooting steps:")
            print("1. Make sure the ViGEmBus driver is installed and its device has started")
            print("2. Restart your computer after a driver installation")
            print("3. Check that no other program has exhausted the bus's pad slots")
            self.is_connected = False
            self.gamepad = None

    def _reset_axes(self) -> None:
        """Reset all axes to center/zero position."""
        if not self.gamepad:
            return

        try:
            # Reset joysticks to center
            self.gamepad.left_joystick_float(0.0, 0.0)
            self.gamepad.right_joystick_float(0.0, 0.0)

            # Reset triggers to zero
            self.gamepad.left_trigger_float(0.0)
            self.gamepad.right_trigger_float(0.0)

            # Apply the update
            self.gamepad.update()

            # Update current values
            for axis in self.current_values:
                self.current_values[axis] = 0.0

        except Exception as e:
            print(f"Error resetting axes: {e}")

    def set_left_stick(self, x: float, y: float) -> bool:
        """
        Set left joystick position.

        Args:
            x: X axis value (-1.0 to 1.0)
            y: Y axis value (-1.0 to 1.0)

        Returns:
            True if update was successful
        """
        if not self.is_connected or not self.gamepad:
            return False

        try:
            # Clamp values
            x = max(-1.0, min(1.0, x))
            y = max(-1.0, min(1.0, y))

            self.gamepad.left_joystick_float(x, y)
            self.gamepad.update()

            self.current_values['left_x'] = x
            self.current_values['left_y'] = y
            self.last_update_time = time.time()

            return True

        except Exception as e:
            print(f"Error setting left stick: {e}")
            return False

    def set_right_stick(self, x: float, y: float) -> bool:
        """
        Set right joystick position.

        Args:
            x: X axis value (-1.0 to 1.0)
            y: Y axis value (-1.0 to 1.0)

        Returns:
            True if update was successful
        """
        if not self.is_connected or not self.gamepad:
            return False

        try:
            # Clamp values
            x = max(-1.0, min(1.0, x))
            y = max(-1.0, min(1.0, y))

            self.gamepad.right_joystick_float(x, y)
            self.gamepad.update()

            self.current_values['right_x'] = x
            self.current_values['right_y'] = y
            self.last_update_time = time.time()

            return True

        except Exception as e:
            print(f"Error setting right stick: {e}")
            return False

    def set_left_trigger(self, value: float) -> bool:
        """
        Set left trigger value.

        Args:
            value: Trigger value (0.0 to 1.0)

        Returns:
            True if update was successful
        """
        if not self.is_connected or not self.gamepad:
            return False

        try:
            value = max(0.0, min(1.0, value))
            self.gamepad.left_trigger_float(value)
            self.gamepad.update()

            self.current_values['left_trigger'] = value
            self.last_update_time = time.time()

            return True

        except Exception as e:
            print(f"Error setting left trigger: {e}")
            return False

    def set_right_trigger(self, value: float) -> bool:
        """
        Set right trigger value.

        Args:
            value: Trigger value (0.0 to 1.0)

        Returns:
            True if update was successful
        """
        if not self.is_connected or not self.gamepad:
            return False

        try:
            value = max(0.0, min(1.0, value))
            self.gamepad.right_trigger_float(value)
            self.gamepad.update()

            self.current_values['right_trigger'] = value
            self.last_update_time = time.time()

            return True

        except Exception as e:
            print(f"Error setting right trigger: {e}")
            return False

    def set_button(self, button_id: int, pressed: bool) -> bool:
        """
        Set button state using Xbox button mapping.

        Button mapping (matching Nimbus Adaptive Controller conventions):
            1: A
            2: B
            3: X
            4: Y
            5: LB (Left Bumper)
            6: RB (Right Bumper)
            7: Back/View
            8: Start/Menu
            9: LS (Left Stick Click)
            10: RS (Right Stick Click)
            11: DPad Up
            12: DPad Down
            13: DPad Left
            14: DPad Right

        Args:
            button_id: Button ID (1-14)
            pressed: Whether button is pressed

        Returns:
            True if update was successful
        """
        if not self.is_connected or not self.gamepad:
            return False

        try:
            button = XUSB_BY_ID.get(button_id)
            if button is None:
                print(f"Unknown button ID: {button_id}")
                return False

            if pressed:
                self.gamepad.press_button(button)
            else:
                self.gamepad.release_button(button)

            self.gamepad.update()
            self.button_states[button_id] = pressed
            self.last_update_time = time.time()

            return True

        except Exception as e:
            print(f"Error setting button {button_id}: {e}")
            return False

    def update_axis(self, axis: str, value: float) -> bool:
        """
        Update a single axis value (compatibility with vJoy interface).

        Args:
            axis: Axis name ('x', 'y', 'z', 'rx', 'ry', 'rz')
            value: Normalized value (-1.0 to 1.0)

        Returns:
            True if update was successful
        """
        if not self.is_connected or not self.gamepad:
            return False

        # Clamp like the set_* methods, so both call paths into the same
        # stick state honor the hardware range
        value = max(-1.0, min(1.0, value))

        try:
            # Map vJoy axis names to Xbox controller
            if axis == 'x':
                self.current_values['left_x'] = value
                self.gamepad.left_joystick_float(value, self.current_values['left_y'])
            elif axis == 'y':
                self.current_values['left_y'] = value
                self.gamepad.left_joystick_float(self.current_values['left_x'], value)
            elif axis == 'rx':
                self.current_values['right_x'] = value
                self.gamepad.right_joystick_float(value, self.current_values['right_y'])
            elif axis == 'ry':
                self.current_values['right_y'] = value
                self.gamepad.right_joystick_float(self.current_values['right_x'], value)
            elif axis == 'z':
                # Z axis -> Left Trigger (convert from -1..1 to 0..1)
                trigger_value = (value + 1.0) / 2.0
                self.current_values['left_trigger'] = trigger_value
                self.gamepad.left_trigger_float(trigger_value)
            elif axis == 'rz':
                # RZ axis -> Right Trigger (convert from -1..1 to 0..1)
                trigger_value = (value + 1.0) / 2.0
                self.current_values['right_trigger'] = trigger_value
                self.gamepad.right_trigger_float(trigger_value)
            else:
                print(f"Unknown axis: {axis}")
                return False

            self.gamepad.update()
            self.last_update_time = time.time()
            return True

        except Exception as e:
            print(f"Error updating axis {axis}: {e}")
            return False

    def get_status(self) -> Dict[str, Any]:
        """
        Get current interface status.

        Returns:
            Dictionary containing status information
        """
        return {
            'connected': self.is_connected,
            'device_type': 'Xbox 360 Controller',
            'last_update': self.last_update_time,
            'current_values': self.current_values.copy(),
            'button_states': self.button_states.copy(),
            'vigem_available': VIGEM_AVAILABLE,
            'bus': getattr(self.gamepad, 'bus_name', None),
            'serial': getattr(self.gamepad, 'serial', 0),
        }

    def emergency_stop(self) -> None:
        """Emergency stop - immediately center all axes."""
        print("EMERGENCY STOP ACTIVATED")
        if self.gamepad:
            try:
                self._reset_axes()
                # Release all buttons
                self.gamepad.reset()
                self.gamepad.update()
            except Exception as e:
                print(f"Error during emergency stop: {e}")

    def shutdown(self) -> None:
        """Shutdown the ViGEm interface safely."""
        print("Shutting down ViGEm interface...")

        if self.gamepad:
            try:
                self._reset_axes()
                self.gamepad.reset()
                self.gamepad.update()
                # Unplug the pad and close the bus handle; closing alone would
                # unplug it too, since the bus owns pads by file handle.
                self.gamepad.close()
                self.gamepad = None
            except Exception as e:
                print(f"Error during shutdown: {e}")

        self.is_connected = False
        print("ViGEm interface shutdown complete")

    def __del__(self):
        """Destructor - ensure clean shutdown."""
        try:
            self.shutdown()
        except:
            pass
