"""
Gate D, the latency axis: input to photon through the guest, against the
same thing natively on the host.

**This flashes the screen.** The beacon toggles up to twice a second for
the length of a run, and in guest mode the viewer window is brought to the
front of the host's desktop, so the flashing is on the host's screen. Run
it unattended, with nobody looking at that screen, and never near someone
who is photosensitive. The beacon is a 300 px light-gray square by default
for that reason; keep ``--trials`` modest.

The feasibility document's section 6 asks for the stages of the guest's
display and input path measured separately, and its Gate D compares the
guest against the host running natively. A game in the guest needs an
interactive Steam login and a second machine is not on the bench, so this
measures the chain with a synthetic workload that runs identically on both
sides: ``vm/guest/beacon.py``, a window that is white while pad button A is
held or for a moment on request, watched by a screen probe on the host.

Three numbers, each a distribution over ``--trials``:

``press_to_photon``  A pressed on the host's pad, to the beacon's white
                     reaching the host's screen. In guest mode that is
                     Moonlight's forwarding, Sunshine's ViGEm pad, the
                     guest's XInput poll, its repaint, Sunshine's capture
                     and encode, the stream, Moonlight's decode and the
                     host's presentation. In native mode it is the pad,
                     the beacon's poll, its repaint and the presentation.
``release_to_dark``  the other edge of the same trial.
``flash_to_photon``  the beacon told to go white over HTTP, to white on the
                     host's screen: the display path without the pad path.

The probe is a GDI copy of a small square of the composed desktop. Under
DWM that copy returns the latest composed frame and costs about one
refresh (16.7 ms at 60 Hz here), so a sample is one display frame: the
numbers are quantized to frames, which is what "photon" means on a 60 Hz
display, and the distributions over trials average the phase out.

``flash_to_photon`` carries about one extra frame of the beacon's own
overhead in both modes (the request is handled on another thread and
applied by the window loop), so compare it between modes, not against
``press_to_photon``; the pad edge is the clean number.

Run, with Moonlight streaming the guest desktop in a window and the beacon
running in the guest at the top-left corner of its primary display (the
probe position is computed from the beacon's rectangle and the viewer's
letterbox)::

    python vm/gate_d_latency.py --mode guest --beacon http://172.17.227.55:47101 --window Moonlight

Then, with Moonlight closed, the native baseline (the beacon is started
locally)::

    python vm/gate_d_latency.py --mode native

Pass ``--json`` to keep the samples. Exit code 0 when every metric got its
trials, 1 otherwise; the numbers are the result.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.parse
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional, Tuple

if sys.platform != "win32":
    sys.exit("Windows only")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from gate_c_host import bring_to_front, find_window, window_title  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0
XUSB_GAMEPAD_A = 0x1000


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]      # a 64-bit handle; the default int argument overflows
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]


class ScreenProbe:
    """Mean brightness of a small square of the composed desktop, a few ms a sample."""

    def __init__(self, x: int, y: int, size: int = 24) -> None:
        self.x, self.y, self.size = x, y, size
        self.screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        self.bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.screen, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(self.bits), None, 0)
        if not self.bmp:
            raise ctypes.WinError(ctypes.get_last_error())
        gdi32.SelectObject(self.mem, self.bmp)

    def brightness(self) -> float:
        if not gdi32.BitBlt(self.mem, 0, 0, self.size, self.size, self.screen, self.x, self.y, SRCCOPY):
            raise ctypes.WinError(ctypes.get_last_error())
        raw = ctypes.string_at(self.bits, self.size * self.size * 4)
        total = 0
        n = self.size * self.size
        for i in range(0, n * 4, 4):
            total += raw[i] + raw[i + 1] + raw[i + 2]
        return total / (3.0 * n)

    def wait_for(self, pred: Callable[[float], bool], timeout_s: float) -> Tuple[Optional[float], float, int]:
        """(seconds until pred(brightness) held, last brightness, samples)."""
        t0 = time.perf_counter()
        samples = 0
        last = 0.0
        while True:
            last = self.brightness()
            samples += 1
            if pred(last):
                return time.perf_counter() - t0, last, samples
            if time.perf_counter() - t0 > timeout_s:
                return None, last, samples


def client_origin(hwnd: int) -> Tuple[int, int, int, int]:
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, rect.right, rect.bottom


def http_get(url: str, timeout: float = 3.0) -> bytes:
    """One GET over a fresh socket with Nagle off: urllib added a 15 to 25 ms stall per request here."""
    parts = urllib.parse.urlsplit(url)
    host, port = parts.hostname, parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii"))
        chunks = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    if not head.startswith(b"HTTP/1.1 200") and not head.startswith(b"HTTP/1.0 200"):
        raise OSError(f"HTTP error from {url}: {head[:60]!r}")
    return body


def summarize(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"n": 0}
    s = sorted(values)

    def pct(p: float) -> float:
        k = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
        return s[k]

    return {"n": len(s), "min": s[0], "p50": statistics.median(s), "p95": pct(95), "p99": pct(99), "max": s[-1],
            "mean": statistics.fmean(s)}


def fmt(summary: Dict[str, float]) -> str:
    if summary.get("n", 0) == 0:
        return "no samples"
    return (f"n={summary['n']}  p50 {summary['p50']:.1f}  p95 {summary['p95']:.1f}  p99 {summary['p99']:.1f}  "
            f"min {summary['min']:.1f}  max {summary['max']:.1f}  mean {summary['mean']:.1f} ms")


BRIGHT = 128.0
DARK = 60.0


def run(args) -> Dict[str, Any]:
    local_beacon = None
    beacon = args.beacon
    if args.mode == "native":
        py = sys.executable
        script = os.path.join(HERE, "guest", "beacon.py")
        local_beacon = subprocess.Popen([py, script, "--x", str(args.native_x), "--y", str(args.native_y),
                                         "--size", "300", "--port", str(args.native_port), "--bind", "127.0.0.1"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        beacon = f"http://127.0.0.1:{args.native_port}"
        window = "Nimbus Beacon"
    else:
        window = args.window

    try:
        for _ in range(50):
            try:
                if http_get(beacon + "/health") == b"ok":
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit(f"no beacon at {beacon}")

        hwnd = find_window(window)
        if not hwnd:
            raise SystemExit(f"no window with '{window}' in its title")
        if args.mode == "guest":
            # The probe reads the composed desktop, so the viewer has to be
            # in front of everything else (an editor on top of it reads as
            # its own background). The native beacon is topmost by itself.
            if not bring_to_front(hwnd):
                print("warning: could not bring the viewer to the front", flush=True)
            time.sleep(0.5)
        ox, oy, cw, ch = client_origin(hwnd)
        if args.mode == "native":
            px, py_ = ox + cw // 2, oy + ch // 2
        elif args.probe_at:
            dx, dy = (int(v) for v in args.probe_at.split(","))
            px, py_ = ox + dx, oy + dy
        else:
            # Where the beacon's centre lands in the viewer: the streamer
            # captures the guest's primary display and the viewer letterboxes
            # it into its window (a 1024x768 guest in a 1280x720 window has
            # 160 px black bars at the sides, which is where a naive probe
            # ends up).
            st = json.loads(http_get(beacon + "/state").decode("utf-8"))
            sw, sh = st["screen"]
            bl, bt, br, bb = st["rect"]
            bx = min(max((bl + br) / 2.0, 0), sw)
            by = min(max((bt + bb) / 2.0, 0), sh)
            scale = min(cw / sw, ch / sh)
            offx = (cw - sw * scale) / 2.0
            offy = (ch - sh * scale) / 2.0
            px, py_ = int(ox + offx + bx * scale), int(oy + offy + by * scale)
            print(f"guest display {sw}x{sh}, beacon rect {st['rect']}, viewer scale {scale:.3f}, bars ({offx:.0f},{offy:.0f})")
        probe = ScreenProbe(px, py_, 24)
        print(f"mode {args.mode}: beacon {beacon}, window '{window_title(hwnd)}' client {cw}x{ch} at ({ox},{oy}), "
              f"probe at ({px},{py_}); brightness now {probe.brightness():.0f}")

        # the probe's own cost
        t0 = time.perf_counter()
        for _ in range(50):
            probe.brightness()
        probe_ms = (time.perf_counter() - t0) / 50 * 1000

        # the beacon must be dark and the probe must see it go white on a flash: a self-check
        http_get(beacon + "/flash?ms=400")
        seen, level, _ = probe.wait_for(lambda b: b > BRIGHT, 3.0)
        if seen is None:
            raise SystemExit(f"the probe never saw the beacon go white (brightness {level:.0f}); is the probe over the beacon?")
        probe.wait_for(lambda b: b < DARK, 3.0)

        from src.padbus_client import X360Pad
        pad = X360Pad()
        pad.update()
        time.sleep(1.0)     # let the chain (or the local beacon) see the pad

        results: Dict[str, List[float]] = {"press_to_photon": [], "release_to_dark": [], "flash_to_photon": [],
                                           "flash_http_rtt": []}
        misses = {"press": 0, "release": 0, "flash": 0}
        for i in range(args.trials):
            if args.mode == "guest" and user32.GetForegroundWindow() != hwnd:
                bring_to_front(hwnd)
                time.sleep(0.3)
            dark, _, _ = probe.wait_for(lambda b: b < DARK, 2.0)
            if dark is None:
                misses["press"] += 1
                continue
            time.sleep(0.12)
            t0 = time.perf_counter()
            pad.press_button(XUSB_GAMEPAD_A)
            pad.update()
            seen, _, _ = probe.wait_for(lambda b: b > BRIGHT, 1.5)
            if seen is None:
                misses["press"] += 1
                pad.release_button(XUSB_GAMEPAD_A)
                pad.update()
                continue
            results["press_to_photon"].append(seen * 1000)
            time.sleep(0.15)
            t2 = time.perf_counter()
            pad.release_button(XUSB_GAMEPAD_A)
            pad.update()
            seen, _, _ = probe.wait_for(lambda b: b < DARK, 1.5)
            if seen is None:
                misses["release"] += 1
            else:
                results["release_to_dark"].append(seen * 1000)
            time.sleep(0.12)

        for i in range(args.trials):
            dark, _, _ = probe.wait_for(lambda b: b < DARK, 2.0)
            if dark is None:
                misses["flash"] += 1
                continue
            time.sleep(0.12)
            t0 = time.perf_counter()
            http_get(beacon + "/flash?ms=200")
            t_req = time.perf_counter()
            seen, _, _ = probe.wait_for(lambda b: b > BRIGHT, 1.5)
            if seen is None:
                misses["flash"] += 1
                continue
            results["flash_to_photon"].append((t_req - t0 + seen) * 1000)
            results["flash_http_rtt"].append((t_req - t0) * 1000)
            probe.wait_for(lambda b: b < DARK, 1.5)

        pad.close()
        summary = {k: summarize(v) for k, v in results.items()}
        print(f"probe sample cost {probe_ms:.2f} ms")
        for k in ("press_to_photon", "release_to_dark", "flash_to_photon", "flash_http_rtt"):
            print(f"  {k:<18} {fmt(summary[k])}")
        print(f"  misses {misses}")
        return {"mode": args.mode, "beacon": beacon, "window": window_title(hwnd), "probe_at": [px, py_],
                "probe_sample_ms": probe_ms, "trials": args.trials, "summary_ms": summary, "samples_ms": results,
                "misses": misses, "timestamp": time.time()}
    finally:
        if local_beacon is not None:
            local_beacon.terminate()


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Gate D latency: input to photon through the guest and natively")
    parser.add_argument("--mode", choices=("guest", "native"), required=True)
    parser.add_argument("--beacon", default="http://172.17.227.55:47101", help="guest mode: the beacon's URL")
    parser.add_argument("--window", default="Moonlight", help="guest mode: title substring of the viewer window")
    parser.add_argument("--probe-at", default=None, help="guest mode: probe offset inside the viewer's client area (default: computed from the beacon's position and the viewer's letterbox)")
    parser.add_argument("--native-x", type=int, default=1500)
    parser.add_argument("--native-y", type=int, default=200)
    parser.add_argument("--native-port", type=int, default=47102)
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    print("WARNING: the beacon flashes for the length of the run; nobody should be looking at this screen.", flush=True)
    result = run(args)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    ok = all(result["summary_ms"][k]["n"] >= args.trials * 0.8 for k in ("press_to_photon", "release_to_dark", "flash_to_photon"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
