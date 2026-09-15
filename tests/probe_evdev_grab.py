#!/usr/bin/env python3
"""
EVIOCGRAB probe for the Linux input-isolation question.

Answers Probe 1's "does an exclusive evdev grab hide a mouse from the
desktop" without touching your real mouse: it creates a *virtual* mouse via
uinput, grabs that device's evdev node with ``EVIOCGRAB``, synthesises
motion, and checks whether the desktop pointer moved (``xdotool``). Every
grab is released in a ``finally`` block.

    ./venv/bin/python tests/probe_evdev_grab.py
    ./venv/bin/python tests/probe_evdev_grab.py --real-mouse /dev/input/by-id/...-event-mouse

The optional ``--real-mouse`` check only opens and grabs the given node for
0.2 s to prove permissions are sufficient; it reads no events.

Requirements: X11 session with ``xdotool``, write access to ``/dev/uinput``,
and read access to mouse-class event nodes (``sudo usermod -aG input $USER``,
then log out and back in). See docs/vision/LINUX_PROBE_PLAN.md.

Exit codes: 0 pass, 1 a finding, 2 a requirement of this machine is unmet
(not Linux, no X11 DISPLAY, event node not readable), which
``tests/run_linux_validation.sh`` reports as a gate rather than a failure.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.uinput_interface import (  # noqa: E402
    _ioc, _IOC_WRITE, _UINPUT_SETUP, _INPUT_EVENT, _ui_get_sysname,
    UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY,
    EV_KEY, EV_SYN, SYN_REPORT, BUS_USB,
)

EV_REL, REL_X, REL_Y = 0x02, 0x00, 0x01
BTN_LEFT, BTN_RIGHT = 0x110, 0x111
UI_SET_RELBIT = _ioc(_IOC_WRITE, "U", 102, 4)
EVIOCGRAB = _ioc(_IOC_WRITE, "E", 0x90, 4)


def pointer() -> tuple:
    out = subprocess.run(["xdotool", "getmouselocation", "--shell"], capture_output=True, text=True).stdout
    d = dict(line.split("=") for line in out.split())
    return int(d["X"]), int(d["Y"])


def emit(fd: int, dx: int, dy: int, reps: int = 5) -> None:
    for _ in range(reps):
        os.write(fd, _INPUT_EVENT.pack(0, 0, EV_REL, REL_X, dx)
                 + _INPUT_EVENT.pack(0, 0, EV_REL, REL_Y, dy)
                 + _INPUT_EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0))
        time.sleep(0.02)
    time.sleep(0.4)


def drain_rel(fd: int) -> int:
    n = 0
    while select.select([fd], [], [], 0.05)[0]:
        data = os.read(fd, 4096)
        n += sum(1 for off in range(0, len(data), _INPUT_EVENT.size)
                 if _INPUT_EVENT.unpack_from(data, off)[2] == EV_REL)
    return n


def create_virtual_mouse() -> tuple:
    ufd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(ufd, UI_SET_EVBIT, EV_REL)
    fcntl.ioctl(ufd, UI_SET_EVBIT, EV_KEY)
    for code in (REL_X, REL_Y):
        fcntl.ioctl(ufd, UI_SET_RELBIT, code)
    for code in (BTN_LEFT, BTN_RIGHT):
        fcntl.ioctl(ufd, UI_SET_KEYBIT, code)
    fcntl.ioctl(ufd, UI_DEV_SETUP, _UINPUT_SETUP.pack(BUS_USB, 0x1209, 0x4E50, 1, b"Nimbus Probe Mouse", 0))
    fcntl.ioctl(ufd, UI_DEV_CREATE)
    buf = bytearray(64)
    fcntl.ioctl(ufd, _ui_get_sysname(64), buf)
    sysname = bytes(buf).split(b"\0")[0].decode()
    node = next(f"/dev/input/{e}" for e in os.listdir(f"/sys/devices/virtual/input/{sysname}") if e.startswith("event"))
    return ufd, node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--real-mouse", metavar="EVENT_NODE", help="also grab+release this real mouse node for 0.2 s")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        print("Linux only."); return 2
    if not os.environ.get("DISPLAY"):
        print("Needs an X11 DISPLAY (xdotool reads the pointer position)."); return 2

    ufd, node = create_virtual_mouse()
    print(f"virtual mouse: {node}; waiting for the X server to hotplug it")
    time.sleep(1.5)
    readable = os.access(node, os.R_OK)
    print(f"node perms: {oct(os.stat(node).st_mode & 0o777)} readable_by_me={readable}")
    if not readable:
        print("Cannot open the node: add your user to the 'input' group (sudo usermod -aG input $USER), log back in, retry.")
        fcntl.ioctl(ufd, UI_DEV_DESTROY); os.close(ufd)
        return 2

    gfd = None
    ok = False
    try:
        p0 = pointer(); emit(ufd, 12, 8); p1 = pointer()
        print(f"[control]  pointer {p0} -> {p1}  moved={p1 != p0}")
        emit(ufd, -12, -8)
        gfd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        fcntl.ioctl(gfd, EVIOCGRAB, 1); drain_rel(gfd)
        p2 = pointer(); emit(ufd, 12, 8); got = drain_rel(gfd); p3 = pointer()
        print(f"[grabbed]  pointer {p2} -> {p3}  moved={p3 != p2}; grabber received {got} REL events")
        fcntl.ioctl(gfd, EVIOCGRAB, 0); os.close(gfd); gfd = None
        p4 = pointer(); emit(ufd, -12, -8); p5 = pointer()
        print(f"[released] pointer {p4} -> {p5}  moved={p5 != p4}")
        ok = (p1 != p0) and (p3 == p2) and got > 0 and (p5 != p4)
        print("RESULT:", "PASS: EVIOCGRAB hides the device from the desktop while the grabber still reads it" if ok else "FAIL")
        if args.real_mouse:
            real = os.path.realpath(args.real_mouse)
            try:
                rfd = os.open(real, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    fcntl.ioctl(rfd, EVIOCGRAB, 1); time.sleep(0.2)
                finally:
                    try:
                        fcntl.ioctl(rfd, EVIOCGRAB, 0)
                    except OSError:
                        pass
                    os.close(rfd)
                print(f"[real mouse] {real}: grab+release OK")
            except OSError as exc:
                print(f"[real mouse] {real}: cannot grab ({exc})")
    finally:
        if gfd is not None:
            try:
                fcntl.ioctl(gfd, EVIOCGRAB, 0)
            except OSError:
                pass
            os.close(gfd)
        fcntl.ioctl(ufd, UI_DEV_DESTROY); os.close(ufd)
        print("virtual mouse destroyed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
