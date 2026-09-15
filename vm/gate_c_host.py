"""
Gate C, host side: the isolation proof, driven from the host against a guest
that runs ``vm\\guest\\gate_c_monitor.py``.

The claim under test (docs/vision/VIRTUAL_MACHINE_FEASIBILITY.md, section 7,
Gate C): host mouse and keyboard activity produce zero input events in the
guest; the pad events Nimbus intends arrive once and in order; a held stick
survives host focus changes; and a Stop with a stick held reads neutral in
the guest within 500 ms.

Phases
------
host_input      relative ``SendInput`` sweeps, ``SetCursorPos`` jumps, a
                click and a key tap on the host. The guest's raw input and
                low-level hook counters must not move.
pad_present     the guest sees an XInput pad after the actuator plugs one.
buttons         bridge buttons 1..14 pressed and released in order; the
                guest's button_down/button_up events must match exactly.
sticks          left stick held then released, right stick, left trigger;
                the guest's stick and trigger events must match in order.
held_through    a held left stick stays held in the guest while the host
                mouse sweeps (the "Nimbus has focus, the game keeps the
                pad" property), and the sweep still produces no guest input.
stop_held       with the left stick held, the actuator stops; the time
                until the guest reports the stick neutral (or the pad
                gone) must be at most 500 ms.

Actuators
---------
``--actuator pad``     an Xbox 360 pad from ``src.padbus_client`` (default):
                       what is asked for is what leaves the host.
``--actuator nimbus``  the real QML app in-process through the harness's
                       ``NimbusActuator``: widgets driven by synthesized
                       pointer events, the bridge shaping applied. Needs
                       the venv Python (PySide6).

The pad has to reach the guest somehow (Moonlight to Sunshine, section 5 of
the feasibility document); this script does not care how. With
``--loopback`` the monitor runs on this same host, which verifies the
tooling but not the isolation: the host_input counters are then reported
and not judged.

Run::

    python vm/gate_c_host.py --guest 172.x.y.z [--actuator pad|nimbus] [--json out.json]
    python vm/gate_c_host.py --guest 127.0.0.1 --loopback

Exit code 0 when every judged check passed.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
import traceback
import urllib.request
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional, Tuple

if sys.platform != "win32":
    sys.exit("Windows only")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10

XINPUT_NAME_BY_FLAG = {
    0x0001: "dpad_up", 0x0002: "dpad_down", 0x0004: "dpad_left", 0x0008: "dpad_right",
    0x0010: "start", 0x0020: "back", 0x0040: "left_thumb", 0x0080: "right_thumb",
    0x0100: "left_shoulder", 0x0200: "right_shoulder", 0x1000: "a", 0x2000: "b", 0x4000: "x", 0x8000: "y",
}
STOP_LIMIT_S = 0.5


# ---- SendInput --------------------------------------------------------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(inp: INPUT) -> int:
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def host_mouse_sweep(count: int = 300, step: int = 6, spacing_s: float = 0.003) -> int:
    sent = 0
    for i in range(count):
        d = step if (i // 10) % 2 == 0 else -step
        inp = INPUT(type=INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(d, d // 2, 0, MOUSEEVENTF_MOVE, 0, None)
        sent += _send(inp)
        time.sleep(spacing_s)
    return sent


def host_cursor_jumps(count: int = 40) -> None:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    for i in range(count):
        user32.SetCursorPos(pt.x + (30 if i % 2 else -30), pt.y + (20 if i % 2 else -20))
        time.sleep(0.004)
    user32.SetCursorPos(pt.x, pt.y)


def host_click() -> None:
    for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        inp = INPUT(type=INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(0, 0, 0, flag, 0, None)
        _send(inp)
        time.sleep(0.03)


def host_key_tap() -> None:
    for flag in (0, KEYEVENTF_KEYUP):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(VK_SHIFT, 0, flag, 0, None)
        _send(inp)
        time.sleep(0.03)


def host_input_burst(click: bool = True) -> Dict[str, int]:
    moves = host_mouse_sweep()
    host_cursor_jumps()
    if click:
        host_click()
    host_key_tap()
    return {"relative_moves": moves, "cursor_jumps": 40, "clicks": int(click), "key_taps": 1}


def park_cursor_over(hwnd: int) -> bool:
    """Put the host cursor at the centre of a window, so a sweep starts away from Nimbus's widgets."""
    if not hwnd:
        return False
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    user32.SetCursorPos((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    time.sleep(0.05)
    return True


# ---- the viewer window ------------------------------------------------------
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetConsoleWindow.restype = wintypes.HWND
VK_MENU = 0x12
# A console's title is the command line that started it, which carries
# "--viewer-title Moonlight" itself, so a console must never match.
CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow")


def find_window(title_substring: str) -> int:
    """First visible top-level window whose title contains the substring, or 0.

    Consoles and terminals are skipped (their title is the command line,
    which contains the substring whenever it was passed as an argument).
    """
    found: List[int] = []
    needle = title_substring.lower()
    own_console = _kernel32.GetConsoleWindow() or 0

    def cb(hwnd, _lparam):
        if hwnd == own_console:
            return True
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        if cls.value in CONSOLE_CLASSES:
            return True
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if needle in buf.value.lower():
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else 0


def bring_to_front(hwnd: int) -> bool:
    """SetForegroundWindow, with the Alt tap that lifts the foreground lock."""
    for _ in range(4):
        for flag in (0, KEYEVENTF_KEYUP):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.u.ki = KEYBDINPUT(VK_MENU, 0, flag, 0, None)
            _send(inp)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


class ViewerFocus:
    """Puts the viewer (Moonlight) in the foreground for a block, then restores what was there.

    With the viewer focused it captures the host pointer and forwards every
    move and key to Sunshine, which is the path a leaking configuration
    would use. Without a title the block runs against whatever is in front.
    """

    def __init__(self, title: Optional[str]) -> None:
        self.hwnd = find_window(title) if title else 0
        self.prev = 0
        self.focused = False

    def __enter__(self) -> "ViewerFocus":
        if self.hwnd:
            self.prev = user32.GetForegroundWindow()
            self.focused = bring_to_front(self.hwnd)
            time.sleep(0.3)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.hwnd and self.prev:
            bring_to_front(self.prev)
            time.sleep(0.2)

    def describe(self) -> str:
        if not self.hwnd:
            return "viewer not focused (no window asked for or found)"
        return f"viewer '{window_title(self.hwnd)}' focused={self.focused}"


# ---- the guest monitor ------------------------------------------------------
class GuestMonitor:
    """HTTP client for gate_c_monitor.py."""

    def __init__(self, host: str, port: int) -> None:
        self.base = f"http://{host}:{port}"

    def _get(self, path: str) -> Any:
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=5) as r:
                return r.read() == b"ok"
        except Exception:
            return False

    def reset(self) -> None:
        req = urllib.request.Request(self.base + "/reset", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()

    def snapshot(self, since: int = 0) -> Dict[str, Any]:
        return self._get(f"/snapshot?since={since}")

    def wait_event(self, since: int, match: Callable[[Dict[str, Any]], bool],
                   timeout_s: float) -> Tuple[Optional[Dict[str, Any]], float]:
        t0 = time.perf_counter()
        while True:
            snap = self.snapshot(since)
            for ev in snap["events"]:
                if match(ev):
                    return ev, time.perf_counter() - t0
            if time.perf_counter() - t0 > timeout_s:
                return None, time.perf_counter() - t0
            time.sleep(0.01)


# ---- actuators ----------------------------------------------------------------
class PadDirect:
    """The harness's pad actuator without the harness: what is asked for is what leaves the host."""

    name = "pad"

    def __init__(self) -> None:
        from src.padbus_client import X360Pad
        from src.vigem_interface import XUSB_BY_ID
        self.pad = X360Pad()
        self.pad.update()
        self._xusb = dict(XUSB_BY_ID)
        self._held: set = set()

    def apply(self, action: Dict[str, Any]) -> None:
        want = {int(b) for b in action.get("buttons", [])}
        self.pad.left_joystick_float(x_value_float=float(action.get("lx", 0.0)), y_value_float=float(action.get("ly", 0.0)))
        self.pad.right_joystick_float(x_value_float=float(action.get("rx", 0.0)), y_value_float=float(action.get("ry", 0.0)))
        self.pad.left_trigger_float(value_float=float(action.get("lt", 0.0)))
        self.pad.right_trigger_float(value_float=float(action.get("rt", 0.0)))
        for b in self._held - want:
            self.pad.release_button(button=self._xusb[b])
        for b in want - self._held:
            self.pad.press_button(button=self._xusb[b])
        self._held = want
        self.pad.update()

    def release(self) -> None:
        self.apply({})

    def stop(self) -> None:
        """What a Nimbus Stop does to the pad: unplug it."""
        self.pad.close()

    def close(self) -> None:
        try:
            self.pad.close()
        except Exception:
            pass


def expected_button_name(bridge_id: int) -> str:
    from src.vigem_interface import XUSB_BY_ID
    return XINPUT_NAME_BY_FLAG[int(XUSB_BY_ID[bridge_id])]


# ---- the phases --------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def add(self, phase: str, check: str, ok: Optional[bool], detail: str) -> None:
        self.rows.append({"phase": phase, "check": check, "pass": ok, "detail": detail})
        mark = "PASS" if ok else ("INFO" if ok is None else "FAIL")
        print(f"  [{mark}] {phase}: {check}  {detail}", flush=True)

    def judged_ok(self) -> bool:
        return all(r["pass"] is not False for r in self.rows)


class RunGuard:
    """Makes a run that stops partway fail the gate instead of passing on the rows it reached.

    ``judged_ok`` is ``all()`` over the rows that exist, so a run that dies
    after one passing check would otherwise print PASS. The harness's
    ``NimbusActuator.run`` also swallows a scenario's exception, so the
    guard records the failure itself, and :meth:`settle` catches a run
    that never reached its end for any other reason.
    """

    CHECK = "the checks ran to the end"

    def __init__(self, rep: "Report") -> None:
        self.rep = rep
        self.accounted = False

    def __call__(self, body: Callable[[], None]) -> None:
        try:
            body()
        except Exception as exc:   # noqa: BLE001
            traceback.print_exc()
            self.rep.add("run", self.CHECK, False, f"stopped by {type(exc).__name__}: {exc}")
        self.accounted = True

    def settle(self) -> None:
        if not self.accounted:
            self.rep.add("run", self.CHECK, False, "the run ended before its checks finished")


def _counter_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, int]:
    b, a = before["counters"], after["counters"]
    return {k: a.get(k, 0) - b.get(k, 0) for k in a}


INPUT_KEYS = ("mouse_input", "keyboard_input", "ll_mouse_hardware", "ll_mouse_injected",
              "ll_keyboard_hardware", "ll_keyboard_injected")
# Reported beside the judged counters, never judged: Windows turns a pad
# into VK_GAMEPAD_* key events inside the guest, so these rise whenever the
# pad is used and say nothing about the host's keyboard.
GAMEPAD_VK_KEYS = ("keyboard_input_gamepad_vk", "ll_keyboard_gamepad_vk")


def phase_host_input(mon: GuestMonitor, rep: Report, loopback: bool, viewer_title: Optional[str] = None) -> None:
    with ViewerFocus(viewer_title) as vf:
        # The baseline comes after focusing: bring_to_front taps Alt to lift
        # the foreground lock, and a queued Alt-up reaching the viewer once it
        # is in front is the harness's own key, not a leak.
        time.sleep(0.2)
        before = mon.snapshot()
        sent = host_input_burst()
        time.sleep(0.4)
        after = mon.snapshot()
    d = _counter_delta(before, after)
    seen = {k: d.get(k, 0) for k in INPUT_KEYS}
    detail = f"{vf.describe()}; host sent {sent}; guest saw {seen}"
    if loopback:
        rep.add("host_input", "guest input counters (loopback: same machine, not judged)", None, detail)
    else:
        rep.add("host_input", "no guest mouse or keyboard input during host activity", all(v == 0 for v in seen.values()), detail)


def connected_slots(mon: GuestMonitor) -> List[int]:
    return sorted(int(k) for k, v in mon.snapshot()["pads"].items() if v.get("connected"))


def phase_pad_present(mon: GuestMonitor, rep: Report, known: Tuple[int, ...] = ()) -> Optional[int]:
    """The slot our pad landed in: one that was not connected before we plugged it.

    A viewer forwards every host gamepad (Moonlight took the host's vJoy
    device as player 0 on 2026-09-13), so the first connected slot is not
    necessarily ours.
    """
    deadline = time.time() + 8.0
    while time.time() < deadline:
        slots = connected_slots(mon)
        new = [s for s in slots if s not in known]
        if new:
            rep.add("pad_present", "guest sees our XInput pad", True, f"slot {new[0]} (connected slots {slots}, before plug {list(known)})")
            return new[0]
        time.sleep(0.1)
    rep.add("pad_present", "guest sees our XInput pad", False,
            f"no new XInput slot within 8 s (connected {connected_slots(mon)}, before plug {list(known)})")
    return None


def _on_slot(slot: Optional[int]) -> Callable[[Dict[str, Any]], bool]:
    return (lambda e: True) if slot is None else (lambda e: e.get("pad") == slot)


def _pad_events(mon: GuestMonitor, since: int, kinds: Tuple[str, ...], slot: Optional[int] = None) -> List[Dict[str, Any]]:
    on = _on_slot(slot)
    return [e for e in mon.snapshot(since)["events"] if e["kind"] in kinds and on(e)]


def phase_buttons(mon: GuestMonitor, rep: Report, act, slot: Optional[int] = None) -> None:
    since = mon.snapshot()["seq"]
    expected: List[Tuple[str, str]] = []
    for bid in range(1, 15):
        name = expected_button_name(bid)
        act.apply({"buttons": [bid]})
        time.sleep(0.08)
        act.apply({})
        time.sleep(0.08)
        expected += [("button_down", name), ("button_up", name)]
    time.sleep(0.3)
    got = [(e["kind"], e["button"]) for e in _pad_events(mon, since, ("button_down", "button_up"), slot)]
    ok = got == expected
    detail = f"{len(got)} events" if ok else f"expected {expected}, got {got}"
    rep.add("buttons", "14 buttons arrive once each, in order", ok, detail)


def phase_sticks(mon: GuestMonitor, rep: Report, act, slot: Optional[int] = None) -> None:
    since = mon.snapshot()["seq"]
    script = [
        ({"lx": 1.0}, "stick_held", {"stick": "left"}),
        ({}, "stick_neutral", {"stick": "left"}),
        ({"ry": 1.0}, "stick_held", {"stick": "right"}),
        ({}, "stick_neutral", {"stick": "right"}),
    ]
    if getattr(act, "supports_triggers", True):
        script += [
            ({"lt": 1.0}, "trigger_held", {"trigger": "left"}),
            ({}, "trigger_released", {"trigger": "left"}),
        ]
    else:
        rep.add("sticks", "trigger hold", None, "skipped: this actuator drives no trigger (the bundled profile has no trigger widget)")
    expected = []
    for action, kind, attrs in script:
        act.apply(action)
        time.sleep(0.35)
        expected.append((kind, tuple(sorted(attrs.items()))))
    time.sleep(0.3)
    kinds = ("stick_held", "stick_neutral", "trigger_held", "trigger_released")
    got = []
    for e in _pad_events(mon, since, kinds, slot):
        attrs = {k: e[k] for k in ("stick", "trigger") if k in e}
        got.append((e["kind"], tuple(sorted(attrs.items()))))
    ok = got == expected
    rep.add("sticks", "stick and trigger holds arrive in order", ok, f"{len(got)} events" if ok else f"expected {expected}, got {got}")


def phase_held_through(mon: GuestMonitor, rep: Report, act, slot: int, loopback: bool,
                       viewer_title: Optional[str] = None) -> None:
    """A held stick through host mouse and keyboard activity with the viewer in the background.

    The sweep starts with the cursor over the viewer's window and sends no
    click: with the real app holding the stick through a synthesized press
    on its widget, a real click on that widget is a legitimate release, and
    a sweep that starts on the Nimbus window would be the user letting go,
    not a leak. The click's path is covered by phase_host_input.
    """
    act.apply({"lx": 0.9})
    time.sleep(0.3)
    snap = mon.snapshot()
    held0 = snap["pads"].get(str(slot), {}).get("left_held", False)
    since = snap["seq"]
    parked = park_cursor_over(find_window(viewer_title) if viewer_title else 0)
    sent = host_input_burst(click=False)
    sent["cursor_parked_over_viewer"] = parked
    time.sleep(0.3)
    after = mon.snapshot(since)
    held1 = after["pads"].get(str(slot), {}).get("left_held", False)
    dropped = [e for e in after["events"] if e["kind"] in ("stick_neutral", "disconnect") and e.get("pad") == slot]
    ok = held0 and held1 and not dropped
    rep.add("held_through", "held left stick survives host mouse and keyboard activity", ok,
            f"held before {held0}, after {held1}, drop events {len(dropped)}")
    d = _counter_delta(snap, after)
    seen = {k: d.get(k, 0) for k in INPUT_KEYS}
    pad_vk = {k: d.get(k, 0) for k in GAMEPAD_VK_KEYS}
    if loopback:
        rep.add("held_through", "guest input counters during the sweep (loopback, not judged)", None, f"{seen}; pad-derived keys {pad_vk}")
    else:
        rep.add("held_through", "no guest input during the sweep", all(v == 0 for v in seen.values()),
                f"host sent {sent}; guest saw {seen}; pad-derived VK_GAMEPAD keys (not judged) {pad_vk}")
    act.apply({})
    time.sleep(0.3)


def phase_stop_held(mon: GuestMonitor, rep: Report, act, slot: Optional[int] = None) -> None:
    on = _on_slot(slot)
    since = mon.snapshot()["seq"]
    act.apply({"lx": 1.0})
    ev, _ = mon.wait_event(since, lambda e: e["kind"] == "stick_held" and e.get("stick") == "left" and on(e), 2.0)
    if ev is None:
        rep.add("stop_held", "stick held before the stop", False, "guest never reported the left stick held")
        return
    since = ev["seq"]
    t0 = time.perf_counter()
    act.stop()
    ev2, elapsed = mon.wait_event(since, lambda e: e["kind"] in ("stick_neutral", "disconnect") and on(e), 3.0)
    elapsed = time.perf_counter() - t0
    ok = ev2 is not None and elapsed <= STOP_LIMIT_S
    rep.add("stop_held", f"stop with a stick held reads neutral within {int(STOP_LIMIT_S * 1000)} ms", ok,
            f"{elapsed * 1000:.0f} ms" + (f" ({ev2['kind']})" if ev2 else " (no neutral or disconnect seen)"))


def run_phases(mon: GuestMonitor, rep: Report, act, loopback: bool, skip_host_input: bool,
               known_slots: Tuple[int, ...] = (), viewer_title: Optional[str] = None) -> None:
    mon.reset()
    if not skip_host_input:
        phase_host_input(mon, rep, loopback, viewer_title)
    slot = phase_pad_present(mon, rep, known_slots)
    if slot is None:
        return
    phase_buttons(mon, rep, act, slot)
    phase_sticks(mon, rep, act, slot)
    if not skip_host_input:
        phase_held_through(mon, rep, act, slot, loopback, viewer_title)
    phase_stop_held(mon, rep, act, slot)


# ---- main -------------------------------------------------------------------
def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Gate C isolation proof, host side")
    parser.add_argument("--guest", required=True, help="IP of the guest running gate_c_monitor.py")
    parser.add_argument("--port", type=int, default=47100)
    parser.add_argument("--actuator", choices=("pad", "nimbus"), default="pad")
    parser.add_argument("--loopback", action="store_true", help="the monitor runs on this host: tooling check only")
    parser.add_argument("--skip-host-input", action="store_true", help="do not synthesize host mouse or keyboard input")
    parser.add_argument("--viewer-title", help="title substring of the viewer window (e.g. Moonlight) to focus during the host input sweep")
    parser.add_argument("--json", help="write the report here")
    args = parser.parse_args(argv)

    mon = GuestMonitor(args.guest, args.port)
    if not mon.health():
        print(f"no monitor at {mon.base} (start vm\\guest\\gate_c_monitor.py there, and open its port)")
        return 2
    rep = Report()
    print(f"Gate C against {mon.base}, actuator {args.actuator}{' (loopback)' if args.loopback else ''}")
    known = tuple(connected_slots(mon))     # pads the guest already has, before ours is plugged
    if known:
        print(f"  guest already has XInput slot(s) {list(known)} connected (a viewer forwards every host pad)")

    if args.actuator == "pad":
        act = PadDirect()
        guard = RunGuard(rep)
        try:
            guard(lambda: run_phases(mon, rep, act, args.loopback, args.skip_host_input, known, args.viewer_title))
        finally:
            act.close()
        guard.settle()
    else:
        from tests.game_harness import NimbusActuator
        act = NimbusActuator()

        class _NimbusStop:
            """The harness actuator with a stop(): release everything, which is what the app's Stop sends."""

            supports_triggers = False     # NimbusActuator.apply drives sticks and buttons only

            def __init__(self, inner) -> None:
                self.inner = inner

            def apply(self, action: Dict[str, Any]) -> None:
                self.inner.apply(action)

            def stop(self) -> None:
                self.inner.release()

        if not act.start():
            print("Nimbus failed to start")
            return 2

        guard = RunGuard(rep)

        def body() -> None:
            err = act.prepare(None)
            if err:
                rep.add("nimbus", "app ready", False, err)
                return
            run_phases(mon, rep, _NimbusStop(act), args.loopback, args.skip_host_input, known, args.viewer_title)

        act.run(lambda: guard(body))
        guard.settle()

    ok = rep.judged_ok()
    judged = [r for r in rep.rows if r["pass"] is not None]
    print(f"\nGATE C {'PASS' if ok else 'FAIL'}: {sum(1 for r in judged if r['pass'])}/{len(judged)} judged checks passed"
          + (" (loopback: isolation not judged)" if args.loopback else ""))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"guest": mon.base, "actuator": args.actuator, "loopback": args.loopback,
                       "pass": ok, "rows": rep.rows, "timestamp": time.time()}, fh, indent=2)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
