"""
Gate D beacon: a window that goes light while gamepad button A is held, or
for a moment on request, so a probe on the host can time when the change
reaches the host's screen.

**It flashes.** A run toggles it up to twice a second for a minute or two,
and on the host the viewer window is brought to the front, so the flashing
is on the host's screen too. Do not run a measurement while anyone is
looking at that screen, and never near someone who is photosensitive. The
defaults are a 300 px square in light gray (not white) for that reason;
the probe's thresholds accept it.

Run inside the guest (streamed by Sunshine) for the guest measurement, and
on the host itself for the native baseline; ``vm/gate_d_latency.py`` does
the timing. Pure ctypes and the standard library, like the monitor.

Two triggers, both applied on the window's own thread:

``gamepad``  XInput is polled at about 1 kHz; the window is white while A
             is down on any slot (or ``--slot N``). Press-to-white and
             release-to-black are both measurable edges.
``/flash``   ``GET /flash?ms=250`` turns the window white for that long.
             This edge excludes the pad path, so the difference between
             the two is the pad's transport.

Run::

    python beacon.py [--x 0 --y 0 --size 300] [--port 47101] [--slot N]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

if sys.platform != "win32":
    sys.exit("Windows only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_QUIT = 0x0012
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
PM_REMOVE = 0x0001
XINPUT_GAMEPAD_A = 0x1000
LIGHT = 0x00B0B0B0      # the "on" colour: light gray, brightness 176, well above the probe's 128 and easier on the eyes than white

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.BeginPaint.restype = wintypes.HDC
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]


def _load_xinput():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            dll = ctypes.WinDLL(name)
            dll.XInputGetState.restype = wintypes.DWORD
            dll.XInputGetState.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_STATE)]
            return dll
        except OSError:
            continue
    return None


class Beacon:
    def __init__(self, x: int, y: int, size: int, slot: Optional[int]) -> None:
        self.white = False
        self.flash_until = 0.0
        self.lock = threading.Lock()
        self.slot = slot
        self.edges = 0
        self.last_change = 0.0
        self._proc = WNDPROC(self._wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "NimbusGateDBeacon"
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = user32.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, wc.lpszClassName, "Nimbus Beacon",
                                           WS_POPUP | WS_VISIBLE, x, y, size, size, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.UpdateWindow(self.hwnd)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            brush = gdi32.CreateSolidBrush(LIGHT if self.white else 0x00000000)
            user32.FillRect(hdc, ctypes.byref(rect), brush)
            gdi32.DeleteObject(brush)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def set_white(self, on: bool) -> None:
        if on == self.white:
            return
        self.white = on
        self.edges += 1
        self.last_change = time.time()
        user32.InvalidateRect(self.hwnd, None, False)
        user32.UpdateWindow(self.hwnd)

    def flash(self, ms: int) -> None:
        with self.lock:
            self.flash_until = time.perf_counter() + ms / 1000.0

    def state(self) -> dict:
        rect = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        return {"white": self.white, "edges": self.edges, "last_change": self.last_change, "slot": self.slot,
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                "screen": [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]}   # the primary display, which a streamer captures


BEACON: Optional[Beacon] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "NimbusBeacon/1"
    # Headers and body leave as two small writes; with Nagle on, the second
    # waits for the client's delayed ACK and a local round trip costs 17 ms.
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):
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
        elif url.path == "/flash":
            ms = int(parse_qs(url.query).get("ms", ["250"])[0])
            BEACON.flash(ms)
            self._send(200, b'{"ok": true}')
        elif url.path == "/state":
            self._send(200, json.dumps(BEACON.state()).encode("utf-8"))
        else:
            self._send(404, b"not found", "text/plain")


def main(argv: List[str]) -> int:
    global BEACON
    parser = argparse.ArgumentParser(description="Gate D beacon window")
    parser.add_argument("--x", type=int, default=0)
    parser.add_argument("--y", type=int, default=0)
    parser.add_argument("--size", type=int, default=300)
    parser.add_argument("--port", type=int, default=47101)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--slot", type=int, default=None, help="only this XInput slot (default: any)")
    parser.add_argument("--seconds", type=float, default=0, help="exit after this long (0: until killed)")
    args = parser.parse_args(argv)

    # The window loop below polls without pause; with the default 5 ms
    # switch interval the HTTP thread waited about 20 ms for the GIL per
    # request, which the host measured as a fat round trip.
    sys.setswitchinterval(0.0005)
    BEACON = Beacon(args.x, args.y, args.size, args.slot)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    xinput = _load_xinput()
    print(f"beacon window 0x{BEACON.hwnd:X} at ({args.x},{args.y}) size {args.size}; http on {args.bind}:{args.port}; "
          f"xinput {'ok' if xinput else 'missing'}", flush=True)

    state = XINPUT_STATE()
    slots = [args.slot] if args.slot is not None else [0, 1, 2, 3]
    msg = wintypes.MSG()
    t_end = time.perf_counter() + args.seconds if args.seconds > 0 else None
    try:
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    return 0
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            want = False
            if xinput is not None:
                for s in slots:
                    if xinput.XInputGetState(s, ctypes.byref(state)) == 0 and (state.Gamepad.wButtons & XINPUT_GAMEPAD_A):
                        want = True
                        break
            with BEACON.lock:
                if time.perf_counter() < BEACON.flash_until:
                    want = True
            BEACON.set_white(want)
            if t_end is not None and time.perf_counter() > t_end:
                return 0
            time.sleep(0.0005)
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
