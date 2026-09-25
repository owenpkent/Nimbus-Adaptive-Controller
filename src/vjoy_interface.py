"""
VJoy interface wrapper for sending virtual controller signals.
Provides a safe and stable interface to the VJoy driver.
"""

import sys
import time
import threading
from typing import Optional, Tuple, Dict, Any
from .config import ControllerConfig

try:
    import pyvjoy
    print(f"PyVjoy imported. Available functions: {[attr for attr in dir(pyvjoy) if not attr.startswith('_')]}")
    
    # Check for different possible API versions
    if hasattr(pyvjoy, 'vJoyEnabled') and hasattr(pyvjoy, 'VJoyDevice'):
        VJOY_AVAILABLE = True
        VJOY_API_VERSION = "standard"
    elif hasattr(pyvjoy, 'VJoyDevice'):
        # Some versions might not have vJoyEnabled but still work
        VJOY_AVAILABLE = True
        VJOY_API_VERSION = "minimal"
        print("Warning: Using minimal PyVjoy API (no vJoyEnabled function)")
    else:
        VJOY_AVAILABLE = False
        VJOY_API_VERSION = "none"
        print("Warning: PyVjoy version incompatible. Missing VJoyDevice class.")
except (Exception, SystemExit) as e:
    # pyvjoy calls sys.exit() rather than raising ImportError when it cannot
    # load vJoyInterface.dll, which is every non-Windows platform. SystemExit
    # derives from BaseException, so neither `except ImportError` nor a plain
    # `except Exception` catches it and the process dies at import time.
    VJOY_AVAILABLE = False
    VJOY_API_VERSION = "none"
    if sys.platform == "win32":
        # On Linux the bridge uses uinput and vJoy is never expected, so the
        # warning would be noise at every start.
        print(f"Warning: PyVjoy not available: {e!r}")


class VJoyInterface:
    """
    Wrapper for VJoy interface with safety features and error handling.
    
    This class provides a stable interface to VJoy with failsafe mechanisms,
    rate limiting, and comprehensive error handling for drone control applications.
    """
    
    def __init__(self, config: ControllerConfig):
        """
        Initialize the VJoy interface.
        
        Args:
            config: Configuration manager instance
        """
        self.config = config
        self.device: Optional[Any] = None
        self.device_id = config.get("vjoy.device_id", 1)
        self.is_connected = False
        self.last_update_time = 0.0
        self.update_rate = config.get("vjoy.update_rate", 60)
        self.min_update_interval = 1.0 / min(config.get("safety.max_update_rate", 100), 1000)
        
        # Current axis values
        self.current_values = {
            'x': 0.5,  # Center position (0.0 to 1.0)
            'y': 0.5,
            'z': 0.5,
            'rx': 0.5,
            'ry': 0.5,
            'rz': 0.5,
            'sl0': 0.5,
            'sl1': 0.5
        }

        # Button IDs already warned about, so a bad mapping cannot spam the log
        self._warned_buttons: set = set()
        
        # Failsafe system (disabled per user preference)
        self.failsafe_enabled = False
        self.failsafe_timeout = config.get("safety.failsafe_timeout", 5.0)
        self.last_command_time = time.time()
        self.failsafe_active = False
        
        # Threading for failsafe monitoring
        self.failsafe_thread: Optional[threading.Thread] = None
        self.shutdown_event = threading.Event()
        
        # Initialize VJoy connection
        self._initialize_vjoy()
        
        # Failsafe monitoring disabled
    
    def _initialize_vjoy(self) -> None:
        """Initialize VJoy device connection."""
        global VJOY_API_VERSION
        
        if not VJOY_AVAILABLE:
            print("VJoy interface not available - running in simulation mode")
            if sys.platform != "win32":
                print("(vJoy is a Windows driver; on Linux the bridge uses src/uinput_interface.py instead)")
                return
            print("To enable VJoy:")
            print("1. Download and install VJoy driver from: http://vjoystick.sourceforge.net/")
            print("2. Configure VJoy device #1 with at least 6 axes")
            print("3. Ensure the device is enabled in VJoy configuration")
            return
        
        try:
            print(f"Using VJoy API version: {VJOY_API_VERSION}")
            
            # Check VJoy driver status (if function available)
            if VJOY_API_VERSION == "standard":
                if not pyvjoy.vJoyEnabled():
                    raise RuntimeError("VJoy driver is not enabled or not installed properly")
                
                # Get VJoy driver version if available
                try:
                    dll_ver, driver_ver = pyvjoy.GetvJoyVersion()
                    print(f"VJoy Driver Version: {driver_ver}, DLL Version: {dll_ver}")
                except AttributeError:
                    print("VJoy version information not available")
                
                # Check device status
                try:
                    status = pyvjoy.GetVJDStatus(self.device_id)
                    if status == pyvjoy.VJD_STAT_OWN:
                        print(f"VJoy device {self.device_id} already owned by this application")
                    elif status == pyvjoy.VJD_STAT_FREE:
                        print(f"VJoy device {self.device_id} is free")
                    elif status == pyvjoy.VJD_STAT_BUSY:
                        raise RuntimeError(f"VJoy device {self.device_id} is busy")
                    elif status == pyvjoy.VJD_STAT_MISS:
                        raise RuntimeError(f"VJoy device {self.device_id} is missing - configure device #{self.device_id} in VJoy")
                    else:
                        raise RuntimeError(f"VJoy device {self.device_id} has unknown status: {status}")
                except AttributeError:
                    print("Warning: Cannot check VJoy device status - proceeding anyway")
            
            # Acquire the device
            print(f"Creating VJoyDevice({self.device_id})...")
            self.device = pyvjoy.VJoyDevice(self.device_id)
            print("VJoyDevice created successfully")
            
            # Test basic functionality
            try:
                # Try to set an axis to test if the device works
                axis_value = 16384  # Center position
                print(f"Testing axis control with value {axis_value}...")
                
                if hasattr(pyvjoy, 'HID_USAGE_X'):
                    self.device.set_axis(pyvjoy.HID_USAGE_X, axis_value)
                    print("[OK] X axis test successful")
                else:
                    # Try alternative axis constants
                    self.device.set_axis(1, axis_value)  # X axis ID
                    print("[OK] X axis test successful (using axis ID)")
                
                # Test additional axes
                axes_to_test = ['HID_USAGE_Y', 'HID_USAGE_Z', 'HID_USAGE_RX', 'HID_USAGE_RY', 'HID_USAGE_RZ']
                axis_ids = [2, 3, 4, 5, 6]  # Fallback axis IDs
                
                for i, axis_name in enumerate(axes_to_test):
                    try:
                        if hasattr(pyvjoy, axis_name):
                            self.device.set_axis(getattr(pyvjoy, axis_name), axis_value)
                        else:
                            self.device.set_axis(axis_ids[i], axis_value)
                        print(f"[OK] {axis_name} test successful")
                    except Exception as e:
                        print(f"[WARN] {axis_name} test failed: {e}")
                
            except Exception as e:
                raise RuntimeError(f"VJoy device test failed: {e}")
            
            # Reset all axes to center
            self._reset_axes()
            
            self.is_connected = True
            print(f"[OK] VJoy device {self.device_id} initialized successfully")
            print("VJoy is now receiving input from Project Nimbus")
            
        except Exception as e:
            print(f"[ERROR] Failed to initialize VJoy device {self.device_id}: {e}")
            print("\nTroubleshooting steps:")
            print("1. Install VJoy driver from: http://vjoystick.sourceforge.net/")
            print("2. Run VJoy configuration tool and enable device #1")
            print("3. Configure device #1 with at least 6 axes (X, Y, Z, RX, RY, RZ)")
            print("4. Try running this application as administrator")
            print("5. Check if another application is using VJoy device #1")
            print("6. Restart your computer after VJoy installation")
            self.is_connected = False
            self.device = None
    
    def _reset_axes(self) -> None:
        """Reset all axes to center position."""
        if not self.device:
            return
        
        try:
            axis_range = self.config.get("vjoy.axis_range", 32767)
            center_value = axis_range // 2
            
            # Reset main axes - use constants if available, otherwise use axis IDs
            if hasattr(pyvjoy, 'HID_USAGE_X'):
                self.device.set_axis(pyvjoy.HID_USAGE_X, center_value)
                self.device.set_axis(pyvjoy.HID_USAGE_Y, center_value)
                self.device.set_axis(pyvjoy.HID_USAGE_Z, center_value)
                self.device.set_axis(pyvjoy.HID_USAGE_RX, center_value)
                self.device.set_axis(pyvjoy.HID_USAGE_RY, center_value)
                self.device.set_axis(pyvjoy.HID_USAGE_RZ, center_value)
                # Slider axes (SL0, SL1)
                if hasattr(pyvjoy, 'HID_USAGE_SL0'):
                    self.device.set_axis(pyvjoy.HID_USAGE_SL0, center_value)
                if hasattr(pyvjoy, 'HID_USAGE_SL1'):
                    self.device.set_axis(pyvjoy.HID_USAGE_SL1, center_value)
            else:
                # Fallback to axis IDs
                self.device.set_axis(1, center_value)  # X
                self.device.set_axis(2, center_value)  # Y
                self.device.set_axis(3, center_value)  # Z
                self.device.set_axis(4, center_value)  # RX
                self.device.set_axis(5, center_value)  # RY
                self.device.set_axis(6, center_value)  # RZ
                self.device.set_axis(7, center_value)  # SL0
                self.device.set_axis(8, center_value)  # SL1
            
            # Update current values
            for axis in self.current_values:
                self.current_values[axis] = 0.5
                
        except Exception as e:
            print(f"Error resetting axes: {e}")
    
    def _start_failsafe_monitor(self) -> None:
        """Start the failsafe monitoring thread."""
        if self.failsafe_thread and self.failsafe_thread.is_alive():
            return
        
        self.failsafe_thread = threading.Thread(target=self._failsafe_monitor, daemon=True)
        self.failsafe_thread.start()
    
    def _failsafe_monitor(self) -> None:
        """Monitor for failsafe conditions and activate if needed."""
        while not self.shutdown_event.is_set():
            try:
                current_time = time.time()
                time_since_command = current_time - self.last_command_time
                
                if time_since_command > self.failsafe_timeout and not self.failsafe_active:
                    print("Failsafe activated - no commands received within timeout period")
                    self._activate_failsafe()
                elif time_since_command <= self.failsafe_timeout and self.failsafe_active:
                    print("Failsafe deactivated - commands resumed")
                    self.failsafe_active = False
                
                time.sleep(0.1)  # Check every 100ms
                
            except Exception as e:
                print(f"Error in failsafe monitor: {e}")
                time.sleep(1.0)
    
    def _activate_failsafe(self) -> None:
        """Activate failsafe mode - center all axes."""
        self.failsafe_active = True
        if self.device:
            try:
                self._reset_axes()
                print("Failsafe: All axes centered")
            except Exception as e:
                print(f"Error during failsafe activation: {e}")
    
    def update_axis(self, axis: str, value: float) -> bool:
        """
        Update a single axis value.
        
        Args:
            axis: Axis name ('x', 'y', 'z', 'rx', 'ry', 'rz', 'sl0', 'sl1')
            value: Normalized value (-1.0 to 1.0)
            
        Returns:
            True if update was successful
        """
        if not self.is_connected or not self.device:
            return False
        
        # Rate limiting disabled - was blocking Y axis updates
        current_time = time.time()
        # if current_time - self.last_update_time < self.min_update_interval:
        #     return False
        
        # Update command timestamp for failsafe
        self.last_command_time = current_time
        
        # Ignore failsafe gating

        # Clamp like the uinput backend, so get_vjoy_value never produces an
        # out-of-range device value
        value = max(-1.0, min(1.0, value))

        try:
            # Convert normalized value to VJoy range
            vjoy_value = self.config.get_vjoy_value(value)
            
            # Map axis names to VJoy constants or IDs
            if hasattr(pyvjoy, 'HID_USAGE_X'):
                axis_mapping = {
                    'x': pyvjoy.HID_USAGE_X,
                    'y': pyvjoy.HID_USAGE_Y,
                    'z': pyvjoy.HID_USAGE_Z,
                    'rx': pyvjoy.HID_USAGE_RX,
                    'ry': pyvjoy.HID_USAGE_RY,
                    'rz': pyvjoy.HID_USAGE_RZ,
                }
                # Add slider axes if available
                if hasattr(pyvjoy, 'HID_USAGE_SL0'):
                    axis_mapping['sl0'] = pyvjoy.HID_USAGE_SL0
                if hasattr(pyvjoy, 'HID_USAGE_SL1'):
                    axis_mapping['sl1'] = pyvjoy.HID_USAGE_SL1
            else:
                # Fallback to axis IDs for minimal PyVjoy
                axis_mapping = {
                    'x': 1,   # X axis
                    'y': 2,   # Y axis  
                    'z': 3,   # Z axis
                    'rx': 4,  # RX axis
                    'ry': 5,  # RY axis
                    'rz': 6,  # RZ axis
                    'sl0': 7, # Slider 0
                    'sl1': 8  # Slider 1
                }
            
            if axis not in axis_mapping:
                print(f"Unknown axis: {axis}")
                return False
            
            # Update the axis
            self.device.set_axis(axis_mapping[axis], vjoy_value)
            self.last_update_time = current_time
            
            self.current_values[axis] = (value + 1.0) / 2.0  # Convert to 0.0-1.0 range
            
            return True
            
        except Exception as e:
            print(f"Error updating axis {axis}: {e}")
            return False
    
    def update_joystick(self, left_x: float, left_y: float, 
                       right_x: float, right_y: float) -> bool:
        """
        Update both joysticks simultaneously.
        
        Args:
            left_x: Left joystick X axis (-1.0 to 1.0)
            left_y: Left joystick Y axis (-1.0 to 1.0)
            right_x: Right joystick X axis (-1.0 to 1.0)
            right_y: Right joystick Y axis (-1.0 to 1.0)
            
        Returns:
            True if update was successful
        """
        if not self.is_connected or not self.device:
            return False
        
        # Rate limiting
        current_time = time.time()
        if current_time - self.last_update_time < self.min_update_interval:
            return False
        
        # Update command timestamp for failsafe
        self.last_command_time = current_time
        
        # Skip update if failsafe is active
        if self.failsafe_active:
            return False
        
        try:
            # Update all axes
            success = True
            success &= self.update_axis('x', left_x)
            success &= self.update_axis('y', left_y)
            success &= self.update_axis('rx', right_x)
            success &= self.update_axis('ry', right_y)
            
            return success
            
        except Exception as e:
            print(f"Error updating joysticks: {e}")
            return False
    
    def set_button(self, button_id: int, pressed: bool) -> bool:
        """
        Set button state.
        
        Args:
            button_id: Button ID (1-128)
            pressed: Whether button is pressed
            
        Returns:
            True if update was successful
        """
        if not self.is_connected or not self.device:
            return False

        # pyvjoy forwards out-of-range IDs to the DLL, which reports failure
        # through a return value this wrapper never sees, so reject them here
        if not 1 <= button_id <= 128:
            if button_id not in self._warned_buttons:
                self._warned_buttons.add(button_id)
                print(f"Unknown button ID: {button_id} (vJoy supports 1-128)")
            return False

        try:
            self.device.set_button(button_id, pressed)
            self.last_command_time = time.time()
            return True
            
        except Exception as e:
            print(f"Error setting button {button_id}: {e}")
            return False
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get current interface status.
        
        Returns:
            Dictionary containing status information
        """
        return {
            'connected': self.is_connected,
            'device_id': self.device_id,
            'failsafe_active': self.failsafe_active,
            'last_update': self.last_update_time,
            'current_values': self.current_values.copy(),
            'vjoy_available': VJOY_AVAILABLE
        }
    
    def emergency_stop(self) -> None:
        """Emergency stop - immediately center all axes and activate failsafe."""
        print("EMERGENCY STOP ACTIVATED")
        self.failsafe_active = True
        if self.device:
            try:
                self._reset_axes()
            except Exception as e:
                print(f"Error during emergency stop: {e}")
    
    def shutdown(self) -> None:
        """Shutdown the VJoy interface safely."""
        print("Shutting down VJoy interface...")
        
        # Signal shutdown to failsafe thread
        self.shutdown_event.set()
        
        # Wait for failsafe thread to finish
        if self.failsafe_thread and self.failsafe_thread.is_alive():
            self.failsafe_thread.join(timeout=2.0)
        
        # Reset axes and release device
        if self.device:
            try:
                self._reset_axes()
                # Note: pyvjoy doesn't have an explicit release method
                # The device is automatically released when the object is destroyed
                self.device = None
            except Exception as e:
                print(f"Error during shutdown: {e}")
        
        self.is_connected = False
        print("VJoy interface shutdown complete")
    
    def __del__(self):
        """Destructor - ensure clean shutdown."""
        try:
            self.shutdown()
        except:
            pass  # Ignore errors during destruction
