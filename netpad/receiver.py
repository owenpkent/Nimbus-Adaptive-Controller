"""
Netpad receiver: the game machine's side. Creates an Xbox 360 pad and drives
it from authenticated state packets (``protocol.py`` holds the rules).

Research prototype for ``docs/vision/SEPARATE_GAME_MACHINE.md`` section 3.2.

Pads
----
``vigem``   Windows: ViGEmBus through ``src/padbus_client.py`` (pure ctypes).
``uinput``  Linux: ``UInputXboxInterface`` from ``src/uinput_interface.py``,
            the same ``Microsoft X-Box 360 pad`` Nimbus creates today.
``log``     no pad; print every change (for trying the protocol anywhere).
``auto``    vigem on Windows, uinput on Linux.

The pad is created when a session goes live and destroyed after 2 s with
none, so a game that only scans for pads at launch (Source games do) will
not see it unless a session is live when the game starts.

Run
---
From the repo root::

    python -m netpad.receiver keygen netpad.key
    python -m netpad.receiver run --key netpad.key [--port 47200] [--status-port 47201]

Deployed flat (``protocol.py``, ``receiver.py`` and ``padbus_client.py`` in
one folder, as ``deploy-guest.ps1`` does for the embeddable Python that
ignores the script's folder)::

    python receiver.py run --key netpad.key

``--status-port`` serves ``GET /status`` (counters, live session, recent
events) read-only. It accepts no commands.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import protocol as P  # type: ignore[no-redef]
else:
    from . import protocol as P

DEFAULT_PORT = 47200

#: Bit ``i`` of the mask is Nimbus button ``i + 1``; the names are padbus_client's
#: XUSB_BUTTON members, in the order of vigem_interface.XUSB_BY_ID.
XUSB_ORDER = [
    "XUSB_GAMEPAD_A", "XUSB_GAMEPAD_B", "XUSB_GAMEPAD_X", "XUSB_GAMEPAD_Y",
    "XUSB_GAMEPAD_LEFT_SHOULDER", "XUSB_GAMEPAD_RIGHT_SHOULDER", "XUSB_GAMEPAD_BACK", "XUSB_GAMEPAD_START",
    "XUSB_GAMEPAD_LEFT_THUMB", "XUSB_GAMEPAD_RIGHT_THUMB",
    "XUSB_GAMEPAD_DPAD_UP", "XUSB_GAMEPAD_DPAD_DOWN", "XUSB_GAMEPAD_DPAD_LEFT", "XUSB_GAMEPAD_DPAD_RIGHT",
]


def _import_padbus() -> Any:
    try:
        from src import padbus_client  # type: ignore[import-not-found]
    except ImportError:
        import padbus_client  # type: ignore[import-not-found,no-redef]
    return padbus_client


class ViGEmSink(P.Sink):
    """An Xbox 360 pad on ViGEmBus."""

    def __init__(self) -> None:
        self.pad: Any = None
        self.masks: List[int] = []

    def open(self) -> None:
        pb = _import_padbus()
        self.masks = [int(getattr(pb.XUSB_BUTTON, name)) for name in XUSB_ORDER]
        self.pad = pb.X360Pad()
        print(f"[netpad] pad plugged: {self.pad.bus_name} serial {self.pad.serial}", flush=True)

    def apply(self, state: P.PadState) -> None:
        r = self.pad.report
        r.sThumbLX, r.sThumbLY, r.sThumbRX, r.sThumbRY = state.lx, state.ly, state.rx, state.ry
        r.bLeftTrigger, r.bRightTrigger = state.lt, state.rt
        mask = 0
        for i, m in enumerate(self.masks):
            if state.buttons & (1 << i):
                mask |= m
        r.wButtons = mask
        self.pad.update()

    def close(self) -> None:
        if self.pad is not None:
            self.pad.close()
            self.pad = None
            print("[netpad] pad unplugged", flush=True)


class _NullConfig:
    def get(self, _key: str, default: Any = None) -> Any:
        return default


class UInputSink(P.Sink):
    """The Linux uinput Xbox 360 pad Nimbus already ships."""

    def __init__(self) -> None:
        self.dev: Any = None
        self.last = P.NEUTRAL

    def open(self) -> None:
        from src.uinput_interface import UInputXboxInterface  # type: ignore[import-not-found]
        dev = UInputXboxInterface(_NullConfig())
        if not dev.is_connected:
            raise RuntimeError("could not create the uinput pad (see the message above)")
        self.dev = dev
        self.last = P.NEUTRAL

    def apply(self, state: P.PadState) -> None:
        d = self.dev
        d.set_left_stick(state.lx / 32767.0, state.ly / 32767.0)
        d.set_right_stick(state.rx / 32767.0, state.ry / 32767.0)
        d.set_left_trigger(state.lt / 255.0)
        d.set_right_trigger(state.rt / 255.0)
        changed = state.buttons ^ self.last.buttons
        for i in range(P.BUTTONS):
            if changed & (1 << i):
                d.set_button(i + 1, bool(state.buttons & (1 << i)))
        self.last = state

    def close(self) -> None:
        if self.dev is not None:
            self.dev.shutdown()
            self.dev = None


class LogSink(P.RecordingSink):
    def open(self) -> None:
        super().open()
        print("[netpad] (log) pad created", flush=True)

    def apply(self, state: P.PadState) -> None:
        super().apply(state)
        print(f"[netpad] (log) {state}", flush=True)

    def close(self) -> None:
        super().close()
        print("[netpad] (log) pad destroyed", flush=True)


def make_sink(kind: str) -> P.Sink:
    if kind == "auto":
        kind = "vigem" if sys.platform == "win32" else "uinput"
    if kind == "vigem":
        return ViGEmSink()
    if kind == "uinput":
        return UInputSink()
    if kind == "log":
        return LogSink()
    raise ValueError(f"unknown sink {kind!r}")


class Receiver:
    """The socket loop around a :class:`protocol.ReceiverCore`."""

    def __init__(self, core: P.ReceiverCore, sock: socket.socket) -> None:
        self.core = core
        self.sock = sock
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def serve(self, seconds: float = 0.0) -> None:
        deadline = time.monotonic() + seconds if seconds > 0 else None
        while not self.stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                ready, _, _ = select.select([self.sock], [], [], 0.002)
            except (OSError, ValueError):
                break
            if ready:
                for _ in range(256):
                    try:
                        data, addr = self.sock.recvfrom(2048)
                    except (BlockingIOError, InterruptedError):
                        break
                    except ConnectionResetError:
                        continue
                    except OSError:
                        break
                    with self.lock:
                        replies = self.core.handle(data, addr)
                    for packet in replies:
                        try:
                            self.sock.sendto(packet, addr)
                        except OSError:
                            pass
            with self.lock:
                self.core.service()

    def shutdown(self) -> None:
        with self.lock:
            live = self.core.live
            if live is not None:
                self.core._end(live, P.REASON_ENDED, time.monotonic())
            if self.core.pad_open:
                self.core._sink_call("close")
                self.core.pad_open = False


def open_socket(bind: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    P._no_udp_connreset(sock)
    sock.bind((bind, port))
    sock.setblocking(False)
    return sock


def serve_status(receiver: Receiver, bind: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "NetpadStatus/1"
        disable_nagle_algorithm = True

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.split("?")[0] != "/status":
                self.send_response(404)
                self.end_headers()
                return
            with receiver.lock:
                body = json.dumps(receiver.core.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="status").start()
    return server


def _fine_timer() -> None:
    """Windows sleeps in 15.6 ms steps by default; the watchdog wants 1 ms."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.WinDLL("winmm").timeBeginPeriod(1)
        except OSError:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Netpad receiver (research prototype)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    kg = sub.add_parser("keygen", help="write a new shared key")
    kg.add_argument("path")
    run = sub.add_parser("run", help="receive and drive a pad")
    run.add_argument("--key", required=True)
    run.add_argument("--bind", default="0.0.0.0")
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--sink", default="auto", choices=["auto", "vigem", "uinput", "log"])
    run.add_argument("--status-port", type=int, default=0, help="serve GET /status on this TCP port (0: off)")
    run.add_argument("--seconds", type=float, default=0.0, help="exit after this long (0: until killed)")
    run.add_argument("--exit-on-stdin-eof", action="store_true",
                     help="exit (unplugging the pad) when stdin closes, so a parent that dies takes the receiver with it")
    args = ap.parse_args(argv)

    if args.cmd == "keygen":
        if os.path.exists(args.path):
            print(f"{args.path} exists; not overwriting", file=sys.stderr)
            return 1
        key = P.write_key(args.path)
        print(f"wrote {args.path}  fingerprint {P.key_fingerprint(key)}")
        return 0

    key = P.load_key(args.key)
    _fine_timer()
    sink = make_sink(args.sink)
    core = P.ReceiverCore(key, sink)
    receiver = Receiver(core, open_socket(args.bind, args.port))
    status = serve_status(receiver, args.bind, args.status_port) if args.status_port else None
    if args.exit_on_stdin_eof:
        def _watch_stdin() -> None:
            try:
                sys.stdin.buffer.read()
            except (OSError, ValueError):
                pass
            receiver.stop.set()
        threading.Thread(target=_watch_stdin, daemon=True, name="stdin-eof").start()
    print(f"[netpad] receiving on udp {args.bind}:{args.port}, key {P.key_fingerprint(key)}, sink {args.sink}"
          + (f", status on tcp {args.status_port}" if status else ""), flush=True)
    try:
        receiver.serve(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        receiver.shutdown()
        if status is not None:
            status.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
