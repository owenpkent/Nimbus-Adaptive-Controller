"""
Gate C guest monitor: count every input that reaches this Windows session.

Runs inside the guest (or on the host, for a loopback check of the tooling)
and answers over HTTP, so the host-side driver (vm\\gate_c_host.py) can read
what arrived while it sweeps the host mouse, works Nimbus widgets and
presses pad buttons.

What is counted
---------------
Raw Input (``WM_INPUT`` with ``RIDEV_INPUTSINK``, so focus does not matter):
    ``mouse_input`` (relative and absolute split out), ``keyboard_input``.
    This is what a game reads. Zero here during a host mouse sweep is the
    isolation claim.
Low-level hooks (``WH_MOUSE_LL`` / ``WH_KEYBOARD_LL``):
    ``ll_mouse`` and ``ll_keyboard``, each split into ``hardware`` and
    ``injected`` (``LLMHF_INJECTED``). A viewer that forwards the pointer
    with ``SendInput`` shows up as injected; the Hyper-V synthetic mouse of a
    basic vmconnect session shows up as hardware. Keys in the
    ``VK_GAMEPAD_*`` range are counted apart (``*_gamepad_vk``): Windows
    synthesizes them from the pad itself, so they are evidence the pad
    arrived, not that a keyboard did. Every key is also logged as a
    ``key`` event with its virtual-key code.
XInput (polled at ``--poll-hz``):
    per pad slot: connected, packet number, buttons, sticks, triggers, and
    an event list of transitions (connect, button_down, button_up,
    stick_held, stick_neutral, trigger) with timestamps, so the host can
    check that each intended event arrived once and in order, and how long
    a "stop with a stick held" took to read neutral.

HTTP
----
``GET /snapshot``   the counters, pad states and events (``?since=N`` for
                    events after sequence number N)
``POST /reset``     zero the counters and drop the events
``GET /health``     ``ok``

Run::

    python gate_c_monitor.py [--bind 0.0.0.0] [--port 47100] [--poll-hz 250]

Pure ctypes and the standard library, so the embeddable Python is enough.
The firewall rule for the port is opened by guest\\setup.ps1.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import sys
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

if sys.platform != "win32":
    sys.exit("Windows only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---- Win32 -----------------------------------------------------------------
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_INPUT = 0x00FF
WS_OVERLAPPEDWINDOW = 0x00CF0000
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIM_TYPEHID = 2
MOUSE_MOVE_ABSOLUTE = 0x0001
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_MOUSE = 0x02
HID_USAGE_GENERIC_KEYBOARD = 0x06
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
LLMHF_INJECTED = 0x00000001
LLKHF_INJECTED = 0x00000010
PM_REMOVE = 0x0001

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wintypes.USHORT), ("ulButtons", wintypes.ULONG),
                ("ulRawButtons", wintypes.ULONG), ("lLastX", wintypes.LONG),
                ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                   ctypes.POINTER(wintypes.UINT), wintypes.UINT]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE      # 64-bit handle: the default c_int return would truncate it
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]

# ---- XInput ------------------------------------------------------------------
XINPUT_BUTTON_NAMES = {
    0x0001: "dpad_up", 0x0002: "dpad_down", 0x0004: "dpad_left", 0x0008: "dpad_right",
    0x0010: "start", 0x0020: "back", 0x0040: "left_thumb", 0x0080: "right_thumb",
    0x0100: "left_shoulder", 0x0200: "right_shoulder", 0x1000: "a", 0x2000: "b", 0x4000: "x", 0x8000: "y",
}
ERROR_DEVICE_NOT_CONNECTED = 1167


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


def _load_xinput():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            dll = ctypes.WinDLL(name)
            dll.XInputGetState.restype = wintypes.DWORD
            dll.XInputGetState.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_STATE)]
            return dll, name
        except OSError:
            continue
    return None, None


# ---- state ------------------------------------------------------------------
STICK_HELD = 0.5
STICK_NEUTRAL = 0.1
TRIGGER_HELD = 0.5


class Monitor:
    """All counters and the event ring, guarded by one lock."""

    def __init__(self, keep_events: int = 2000) -> None:
        self.lock = threading.Lock()
        self.started = time.time()
        self.keep_events = keep_events
        self.seq = 0
        self.events: List[Dict[str, Any]] = []
        self.counters: Dict[str, int] = {}
        self.pads: Dict[int, Dict[str, Any]] = {}
        self.xinput_dll: Optional[str] = None
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.counters = {
                "mouse_input": 0, "mouse_input_relative": 0, "mouse_input_absolute": 0,
                "mouse_input_buttons": 0, "keyboard_input": 0, "keyboard_input_gamepad_vk": 0, "hid_input": 0,
                "ll_mouse_hardware": 0, "ll_mouse_injected": 0,
                "ll_keyboard_hardware": 0, "ll_keyboard_injected": 0, "ll_keyboard_gamepad_vk": 0,
                "pad_packets": 0,
            }
            self.events = []
            self.reset_at = time.time()

    def bump(self, key: str, n: int = 1) -> None:
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + n

    def event(self, kind: str, **detail: Any) -> None:
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, "t": time.time(), "kind": kind}
            ev.update(detail)
            self.events.append(ev)
            if len(self.events) > self.keep_events:
                del self.events[: len(self.events) - self.keep_events]

    def snapshot(self, since: int = 0) -> Dict[str, Any]:
        with self.lock:
            return {
                "t": time.time(),
                "uptime_s": round(time.time() - self.started, 3),
                "reset_at": self.reset_at,
                "xinput": self.xinput_dll,
                "counters": dict(self.counters),
                "pads": {str(k): dict(v) for k, v in self.pads.items()},
                "seq": self.seq,
                "events": [e for e in self.events if e["seq"] > since],
            }


MON = Monitor()

# ---- raw input window and hooks (main thread) --------------------------------
_keep_alive: List[Any] = []


def _wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_INPUT:
        size = wintypes.UINT(0)
        user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        buf = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if got == size.value and size.value >= ctypes.sizeof(RAWINPUTHEADER):
            header = RAWINPUTHEADER.from_buffer_copy(buf.raw[: ctypes.sizeof(RAWINPUTHEADER)])
            if header.dwType == RIM_TYPEMOUSE:
                off = ctypes.sizeof(RAWINPUTHEADER)
                mouse = RAWMOUSE.from_buffer_copy(buf.raw[off: off + ctypes.sizeof(RAWMOUSE)])
                MON.bump("mouse_input")
                if mouse.usFlags & MOUSE_MOVE_ABSOLUTE:
                    MON.bump("mouse_input_absolute")
                elif mouse.lLastX or mouse.lLastY:
                    MON.bump("mouse_input_relative")
                if mouse.ulButtons & 0x3FF:
                    MON.bump("mouse_input_buttons")
            elif header.dwType == RIM_TYPEKEYBOARD:
                off = ctypes.sizeof(RAWINPUTHEADER)
                kb = RAWKEYBOARD.from_buffer_copy(buf.raw[off: off + ctypes.sizeof(RAWKEYBOARD)])
                MON.bump("keyboard_input_gamepad_vk" if is_gamepad_vk(int(kb.VKey)) else "keyboard_input")
            else:
                MON.bump("hid_input")
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _mouse_hook(n_code, wparam, lparam):
    if n_code >= 0:
        info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        MON.bump("ll_mouse_injected" if info.flags & LLMHF_INJECTED else "ll_mouse_hardware")
    return user32.CallNextHookEx(None, n_code, wparam, lparam)


WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
# VK_GAMEPAD_A (0xC3) through VK_GAMEPAD_RIGHT_THUMBSTICK_LEFT (0xDA): the
# virtual keys Windows itself synthesizes from an XInput pad for shell and
# XAML navigation. They arrive as injected keyboard input, one per pad
# button edge and auto-repeating while a stick is held, and they are the
# guest's reflection of the pad, not anyone's keyboard. Seen on
# 2026-09-13: 14 "keyboard" events during a stick hold were all 0xD5.
VK_GAMEPAD_FIRST = 0xC3
VK_GAMEPAD_LAST = 0xDA


def is_gamepad_vk(vk: int) -> bool:
    return VK_GAMEPAD_FIRST <= vk <= VK_GAMEPAD_LAST


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT),
                ("VKey", wintypes.USHORT), ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


def _keyboard_hook(n_code, wparam, lparam):
    if n_code >= 0:
        info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        injected = bool(info.flags & LLKHF_INJECTED)
        gamepad = is_gamepad_vk(int(info.vkCode))
        if gamepad:
            MON.bump("ll_keyboard_gamepad_vk")
        else:
            MON.bump("ll_keyboard_injected" if injected else "ll_keyboard_hardware")
        # Every key that reaches the session is worth naming, because a game
        # would see exactly these.
        MON.event("key", vk=int(info.vkCode), scan=int(info.scanCode), injected=injected, gamepad_vk=gamepad,
                  down=(wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)))
    return user32.CallNextHookEx(None, n_code, wparam, lparam)


def install_input_capture() -> int:
    """Create the hidden sink window, register raw input, set the hooks. Returns the hwnd."""
    hinst = kernel32.GetModuleHandleW(None)
    proc = WNDPROC(_wndproc)
    _keep_alive.append(proc)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = "NimbusGateCMonitor"
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "Nimbus Gate C monitor", WS_OVERLAPPEDWINDOW,
                                  0, 0, 200, 100, None, None, hinst, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    devices = (RAWINPUTDEVICE * 2)()
    devices[0] = RAWINPUTDEVICE(HID_USAGE_PAGE_GENERIC, HID_USAGE_GENERIC_MOUSE, RIDEV_INPUTSINK, hwnd)
    devices[1] = RAWINPUTDEVICE(HID_USAGE_PAGE_GENERIC, HID_USAGE_GENERIC_KEYBOARD, RIDEV_INPUTSINK, hwnd)
    if not user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        raise ctypes.WinError(ctypes.get_last_error())
    mh = HOOKPROC(_mouse_hook)
    kh = HOOKPROC(_keyboard_hook)
    _keep_alive.extend([mh, kh])
    for ident, cb in ((WH_MOUSE_LL, mh), (WH_KEYBOARD_LL, kh)):
        if not user32.SetWindowsHookExW(ident, cb, hinst, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    return hwnd


def pump_forever(stop: threading.Event) -> None:
    msg = wintypes.MSG()
    while not stop.is_set():
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_QUIT:
                stop.set()
                return
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.002)


# ---- XInput poll thread -----------------------------------------------------
def _norm(v: int) -> float:
    return max(-1.0, min(1.0, v / 32767.0))


def xinput_loop(stop: threading.Event, poll_hz: int) -> None:
    dll, name = _load_xinput()
    MON.xinput_dll = name
    if dll is None:
        MON.event("xinput_missing")
        return
    period = 1.0 / max(50, poll_hz)
    prev: Dict[int, Dict[str, Any]] = {}
    state = XINPUT_STATE()
    while not stop.is_set():
        t0 = time.perf_counter()
        for slot in range(4):
            rc = dll.XInputGetState(slot, ctypes.byref(state))
            was = prev.get(slot)
            if rc != 0:
                if was is not None and was.get("connected"):
                    MON.event("disconnect", pad=slot)
                cur = {"connected": False}
                prev[slot] = cur
                with MON.lock:
                    MON.pads[slot] = dict(cur)
                continue
            g = state.Gamepad
            lx, ly, rx, ry = _norm(g.sThumbLX), _norm(g.sThumbLY), _norm(g.sThumbRX), _norm(g.sThumbRY)
            lt, rt = g.bLeftTrigger / 255.0, g.bRightTrigger / 255.0
            cur = {
                "connected": True, "packet": state.dwPacketNumber, "buttons": g.wButtons,
                "lx": round(lx, 4), "ly": round(ly, 4), "rx": round(rx, 4), "ry": round(ry, 4),
                "lt": round(lt, 4), "rt": round(rt, 4),
                "left_mag": round(math.hypot(lx, ly), 4), "right_mag": round(math.hypot(rx, ry), 4),
                "left_held": False, "right_held": False, "lt_held": lt >= TRIGGER_HELD, "rt_held": rt >= TRIGGER_HELD,
            }
            if was is None or not was.get("connected"):
                MON.event("connect", pad=slot)
                was = {"connected": True, "buttons": 0, "packet": -1, "left_held": False, "right_held": False,
                       "lt_held": False, "rt_held": False}
            if cur["packet"] != was.get("packet"):
                MON.bump("pad_packets")
            changed = cur["buttons"] ^ was["buttons"]
            for bit, bname in XINPUT_BUTTON_NAMES.items():
                if changed & bit:
                    MON.event("button_down" if cur["buttons"] & bit else "button_up", pad=slot, button=bname)
            for side, mag_key, held_key in (("left", "left_mag", "left_held"), ("right", "right_mag", "right_held")):
                held = was.get(held_key, False)
                if not held and cur[mag_key] >= STICK_HELD:
                    held = True
                    MON.event("stick_held", pad=slot, stick=side, x=cur[side[0] + "x"], y=cur[side[0] + "y"])
                elif held and cur[mag_key] <= STICK_NEUTRAL:
                    held = False
                    MON.event("stick_neutral", pad=slot, stick=side)
                cur[held_key] = held
            for key, name_ in (("lt_held", "left"), ("rt_held", "right")):
                if cur[key] != was.get(key, False):
                    MON.event("trigger_held" if cur[key] else "trigger_released", pad=slot, trigger=name_)
            prev[slot] = cur
            with MON.lock:
                MON.pads[slot] = dict(cur)
        dt = time.perf_counter() - t0
        if dt < period:
            time.sleep(period - dt)


# ---- HTTP -------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "NimbusGateC/1"

    def log_message(self, fmt, *args):   # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            self._send(200, b"ok", "text/plain")
        elif url.path == "/snapshot":
            since = int(parse_qs(url.query).get("since", ["0"])[0])
            self._send(200, json.dumps(MON.snapshot(since)).encode("utf-8"))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/reset":
            MON.reset()
            MON.event("reset")
            self._send(200, b'{"ok": true}')
        else:
            self._send(404, b"not found", "text/plain")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Gate C guest input monitor")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=47100)
    parser.add_argument("--poll-hz", type=int, default=250)
    parser.add_argument("--seconds", type=float, default=0, help="exit after this long (0: run until killed)")
    args = parser.parse_args(argv)

    stop = threading.Event()
    hwnd = install_input_capture()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    threading.Thread(target=xinput_loop, args=(stop, args.poll_hz), daemon=True, name="xinput").start()
    print(f"gate_c_monitor listening on http://{args.bind}:{args.port}/snapshot  (hwnd 0x{hwnd:X}, poll {args.poll_hz} Hz)", flush=True)
    if args.seconds > 0:
        threading.Timer(args.seconds, stop.set).start()
    try:
        pump_forever(stop)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
