"""
Hardware-free checks on the Gate C tooling in vm/: the guest monitor's
bookkeeping (vm/guest/gate_c_monitor.py) and the host driver's verdicts
(vm/gate_c_host.py), with a fake guest and a fake actuator standing in for
the VM and the pad. Nothing here touches ViGEm, XInput or the mouse; the
phases that synthesize host input are not run.

What is checked: a faithful actuator passes every judged phase; a dropped
button, a swapped press order, and a Stop that leaves the stick held each
fail the phase that exists to catch them; INFO rows never decide the
verdict; the monitor's counters, event ring and since-filter behave.

Windows only (both modules refuse to import elsewhere); prints SKIP and
exits 0 on other platforms so the Linux validation run is not failed by it.

Run from the repo root (the runner does this)::

    venv\\Scripts\\python -m tests.test_vm_gate_c
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if sys.platform != "win32":
    print("SKIP: Windows only (the modules under test are)")
    sys.exit(0)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, rel))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


gch = _load("gate_c_host", os.path.join("vm", "gate_c_host.py"))
gcm = _load("gate_c_monitor", os.path.join("vm", "guest", "gate_c_monitor.py"))
gch.time.sleep = lambda s: None      # the phases pace the real pad; the fakes need no pacing
gch.print = lambda *a, **k: None     # the phases narrate every row; the planted faults would flood the output

PASSES = 0
FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSES, FAILS
    if ok:
        PASSES += 1
    else:
        FAILS += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


class FakeMonitor:
    """An in-memory guest: the fake actuator pushes the events a real pad would produce."""

    def __init__(self) -> None:
        self.seq = 0
        self.events: List[Dict[str, Any]] = []
        self.counters = {k: 0 for k in gch.INPUT_KEYS}
        self.pads: Dict[str, Dict[str, Any]] = {"0": {"connected": True, "left_held": False, "right_held": False}}

    def reset(self) -> None:
        self.events = []

    def push(self, kind: str, **detail: Any) -> None:
        self.seq += 1
        ev = {"seq": self.seq, "t": time.time(), "kind": kind, "pad": 0}
        ev.update(detail)
        self.events.append(ev)

    def snapshot(self, since: int = 0) -> Dict[str, Any]:
        return {"t": time.time(), "seq": self.seq, "counters": dict(self.counters),
                "pads": {k: dict(v) for k, v in self.pads.items()},
                "events": [e for e in self.events if e["seq"] > since]}

    def wait_event(self, since: int, match: Callable[[Dict[str, Any]], bool],
                   timeout_s: float) -> Tuple[Optional[Dict[str, Any]], float]:
        for ev in self.events:
            if ev["seq"] > since and match(ev):
                return ev, 0.0
        return None, timeout_s


class FakeActuator:
    """Turns apply() into the guest events a real pad produces, with optional faults."""

    def __init__(self, mon: FakeMonitor, drop_button: Optional[int] = None,
                 swap_edges: bool = False, dead_stop: bool = False) -> None:
        self.mon = mon
        self.drop_button = drop_button
        self.swap_edges = swap_edges
        self.dead_stop = dead_stop
        self.held: set = set()
        self.left_held = False
        self.right_held = False
        self.lt_held = False

    def apply(self, action: Dict[str, Any]) -> None:
        want = {int(b) for b in action.get("buttons", [])}
        for b in sorted(want - self.held):
            if b != self.drop_button:
                self.mon.push("button_up" if self.swap_edges else "button_down", button=gch.expected_button_name(b))
        for b in sorted(self.held - want):
            if b != self.drop_button:
                self.mon.push("button_down" if self.swap_edges else "button_up", button=gch.expected_button_name(b))
        self.held = want
        lmag = (float(action.get("lx", 0)) ** 2 + float(action.get("ly", 0)) ** 2) ** 0.5
        rmag = (float(action.get("rx", 0)) ** 2 + float(action.get("ry", 0)) ** 2) ** 0.5
        for side, mag, attr in (("left", lmag, "left_held"), ("right", rmag, "right_held")):
            held = getattr(self, attr)
            if not held and mag >= 0.5:
                setattr(self, attr, True)
                self.mon.push("stick_held", stick=side, x=action.get(side[0] + "x", 0), y=action.get(side[0] + "y", 0))
            elif held and mag <= 0.1:
                setattr(self, attr, False)
                self.mon.push("stick_neutral", stick=side)
        self.mon.pads["0"]["left_held"] = self.left_held
        self.mon.pads["0"]["right_held"] = self.right_held
        lt = float(action.get("lt", 0))
        if not self.lt_held and lt >= 0.5:
            self.lt_held = True
            self.mon.push("trigger_held", trigger="left")
        elif self.lt_held and lt < 0.5:
            self.lt_held = False
            self.mon.push("trigger_released", trigger="left")

    def stop(self) -> None:
        if self.dead_stop:
            return
        self.mon.pads["0"]["connected"] = False
        self.mon.push("disconnect")


def run_pad_phases(act_kwargs: Dict[str, Any]):
    mon = FakeMonitor()
    act = FakeActuator(mon, **act_kwargs)
    rep = gch.Report()
    slot = gch.phase_pad_present(mon, rep)
    gch.phase_buttons(mon, rep, act)
    gch.phase_sticks(mon, rep, act)
    gch.phase_stop_held(mon, rep, act)
    return rep, slot


def rows(rep, phase: str):
    return [r for r in rep.rows if r["phase"] == phase]


print("Gate C host driver against a fake guest")
rep, slot = run_pad_phases({})
check("faithful actuator: pad present on slot 0", slot == 0)
check("faithful actuator: every judged phase passes", rep.judged_ok(), str([r["check"] for r in rep.rows if not r["pass"]]))
check("faithful actuator: 14 buttons reported as 28 events", "28 events" in rows(rep, "buttons")[0]["detail"])

rep, _ = run_pad_phases({"drop_button": 7})
check("a dropped button fails the buttons phase", rows(rep, "buttons")[0]["pass"] is False)
check("a dropped button leaves the sticks phase passing", rows(rep, "sticks")[0]["pass"] is True)
check("a dropped button fails the run", not rep.judged_ok())

rep, _ = run_pad_phases({"swap_edges": True})
check("swapped press and release edges fail the buttons phase", rows(rep, "buttons")[0]["pass"] is False)

rep, _ = run_pad_phases({"dead_stop": True})
check("a Stop that leaves the stick held fails stop_held", rows(rep, "stop_held")[0]["pass"] is False,
      rows(rep, "stop_held")[0]["detail"])

rep = gch.Report()
rep.add("x", "info row", None, "loopback")
rep.add("x", "pass row", True, "")
check("INFO rows do not decide the verdict", rep.judged_ok())
rep.add("x", "fail row", False, "")
check("one FAIL row fails the verdict", not rep.judged_ok())

crashed = gch.Report()


def dies_after_one_check() -> None:
    crashed.add("pad", "pad present", True, "")
    raise TimeoutError("the monitor stopped answering (planted by the test)")


guard = gch.RunGuard(crashed)
guard(dies_after_one_check)
guard.settle()
check("a run that dies after a passing check fails the verdict",
      not crashed.judged_ok() and [r["pass"] for r in crashed.rows] == [True, False],
      str([r["check"] for r in crashed.rows]))
never = gch.Report()
never.add("pad", "pad present", True, "")
gch.RunGuard(never).settle()
check("a run that never reached its end fails too", not never.judged_ok())
whole = gch.Report()
ok_guard = gch.RunGuard(whole)
ok_guard(lambda: whole.add("pad", "pad present", True, ""))
ok_guard.settle()
check("a run that finishes adds no row of its own", whole.judged_ok() and len(whole.rows) == 1)

print("Bridge button ids map onto XInput names")
names = [gch.expected_button_name(i) for i in range(1, 15)]
check("14 distinct XInput names for bridge buttons 1..14", len(set(names)) == 14, ",".join(names))
check("bridge button 1 is A and 8 is Start", names[0] == "a" and names[7] == "start")

print("Guest monitor bookkeeping")
m = gcm.Monitor(keep_events=5)
m.bump("mouse_input")
m.bump("mouse_input", 2)
m.event("button_down", pad=0, button="a")
snap = m.snapshot()
check("counters add up", snap["counters"]["mouse_input"] == 3)
check("events carry a sequence number", snap["events"][0]["seq"] == 1 and snap["seq"] == 1)
for i in range(7):
    m.event("button_up", pad=0, button="a")
check("event ring keeps the newest keep_events entries", len(m.snapshot()["events"]) == 5 and m.snapshot()["events"][0]["seq"] == 4)
check("since filter returns only later events", [e["seq"] for e in m.snapshot(since=6)["events"]] == [7, 8])
m.reset()
snap = m.snapshot()
check("reset zeroes counters and drops events, keeps the sequence", snap["counters"]["mouse_input"] == 0 and not snap["events"] and snap["seq"] == 8)
check("input keys the host judges all exist on the monitor", all(k in snap["counters"] for k in gch.INPUT_KEYS))
check("pad-derived key counters exist on the monitor", all(k in snap["counters"] for k in gch.GAMEPAD_VK_KEYS))
check("VK_GAMEPAD_A and the thumbstick keys classify as pad-derived, Shift does not",
      gcm.is_gamepad_vk(0xC3) and gcm.is_gamepad_vk(0xD5) and gcm.is_gamepad_vk(0xDA) and not gcm.is_gamepad_vk(0x10) and not gcm.is_gamepad_vk(0xDB))
check("monitor button names match the host's expectations", set(gcm.XINPUT_BUTTON_NAMES.values()) == set(gch.XINPUT_NAME_BY_FLAG.values()))

print(f"\n{PASSES} passed, {FAILS} failed")
sys.exit(1 if FAILS else 0)
