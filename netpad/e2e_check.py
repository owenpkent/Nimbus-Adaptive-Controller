"""
Netpad end to end: a real sender on this machine, the receiver on another,
the pad read back through XInput on the far side.

Research tooling for ``docs/vision/SEPARATE_GAME_MACHINE.md`` section 3.2,
run against NimbusGuest (``netpad\\deploy-guest.ps1``) with the Gate C monitor
(``vm/guest/gate_c_monitor.py``) as the ground truth. It moves no mouse and
presses no key on this machine, and flashes nothing: safe with someone at
the screen.

Checks, each against the guest's own XInput reading
---------------------------------------------------
E1  handshake; a new pad connects in the guest
E2  the 14 buttons, pressed and released in order (28 events)
E3  both sticks and both triggers, held and released in order
E4  explicit Stop: time until the guest reads the held stick neutral
E5  the pad is unplugged about 2 s after Stop
E6  connection loss (the loop stops): neutral 150 ms after the last packet,
    then the loop resumes and the stick is held again through a new session
E7  a stalled loop tick with packets still flowing: neutral the same way
E8  replaying a timed-out session's packets moves nothing
E9  a second sender is refused while a session is live, and admitted after
E10 loss, duplication and reordering through a relay: every tap arrives once
E11 nothing but the pad reached the guest: zero mouse and keyboard input
    for the whole run (the pad's own VK_GAMEPAD keys are counted apart)

Latency bounds are wide on purpose: each reading includes the guest's 4 ms
XInput poll and an HTTP round trip to the monitor.

Run (from the repo root)::

    venv\\Scripts\\python -m netpad.e2e_check --receiver 172.17.227.55 --key C:\\NimbusVM\\netpad.key
"""
from __future__ import annotations

import argparse
import heapq
import itertools
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import protocol as P

BUTTON_NAMES = ["a", "b", "x", "y", "left_shoulder", "right_shoulder", "back", "start", "left_thumb",
                "right_thumb", "dpad_up", "dpad_down", "dpad_left", "dpad_right"]
QUIET_COUNTERS = ["mouse_input", "keyboard_input", "ll_mouse_hardware", "ll_mouse_injected",
                  "ll_keyboard_hardware", "ll_keyboard_injected"]

RESULTS: List[Dict[str, Any]] = []


def check(name: str, ok: bool, detail: str = "", **data: Any) -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail, **data})
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  ({detail})" if detail else ""), flush=True)
    return ok


class Guest:
    def __init__(self, host: str, monitor_port: int, status_port: int) -> None:
        self.monitor = f"http://{host}:{monitor_port}"
        self.status_url = f"http://{host}:{status_port}/status"

    def _get(self, url: str, timeout: float = 3.0) -> Any:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def snapshot(self, since: int = 0) -> Dict[str, Any]:
        return self._get(f"{self.monitor}/snapshot?since={since}")

    def reset(self) -> None:
        req = urllib.request.Request(f"{self.monitor}/reset", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=3.0).read()

    def status(self) -> Dict[str, Any]:
        return self._get(self.status_url)


class Loop:
    """Ticks senders at 120 Hz while a condition is polled, like an app loop would."""

    def __init__(self, *senders: P.NetpadSender) -> None:
        self.senders = list(senders)

    def run(self, seconds: float, until: Optional[Callable[[], bool]] = None, tick: bool = True,
            poll_every: float = 0.0) -> bool:
        end = time.monotonic() + seconds
        next_tick = time.monotonic()
        next_poll = time.monotonic()
        while time.monotonic() < end:
            now = time.monotonic()
            if tick and now >= next_tick:
                for s in self.senders:
                    s.tick()
                next_tick += 1 / 120
            if until is not None and now >= next_poll:
                if until():
                    return True
                next_poll = now + poll_every
            time.sleep(0.001)
        return until() if until is not None else True


def pad_events(snap: Dict[str, Any], slot: Optional[int]) -> List[Dict[str, Any]]:
    return [e for e in snap["events"] if slot is None or e.get("pad") == slot]


def wait_connect(guest: Guest, loop: Loop, since: int, seconds: float = 10.0) -> Optional[int]:
    found: List[int] = []

    def connected() -> bool:
        for e in guest.snapshot(since)["events"]:
            if e["kind"] == "connect":
                found.append(e["pad"])
                return True
        return False

    loop.run(seconds, connected, poll_every=0.05)
    return found[0] if found else None


def left_mag(guest: Guest, slot: int) -> float:
    pad = guest.snapshot(10 ** 12)["pads"].get(str(slot), {})
    return float(pad.get("left_mag", 0.0)) if pad.get("connected") else 0.0


def time_to_neutral(guest: Guest, slot: int, t_ref: float, loop: Loop, tick: bool,
                    during: Optional[Callable[[], None]] = None, limit: float = 2.0) -> Optional[float]:
    """Milliseconds from ``t_ref`` until the guest reads the left stick neutral."""
    end = time.monotonic() + limit
    next_side = time.monotonic()
    while time.monotonic() < end:
        now = time.monotonic()
        if during is not None and now >= next_side:
            during()
            next_side = now + 1 / 60
        if tick:
            for s in loop.senders:
                s.tick()
        if left_mag(guest, slot) <= 0.1:
            return (time.monotonic() - t_ref) * 1000
        time.sleep(0.001)
    return None


class Relay:
    """UDP relay with seeded loss, duplication and reordering on the way to the receiver."""

    def __init__(self, target: Tuple[str, int], loss: float, dup: float, reorder: float, seed: int) -> None:
        self.target = target
        self.loss, self.dup, self.reorder = loss, dup, reorder
        self.rnd = random.Random(seed)
        self.front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.front.bind(("127.0.0.1", 0))
        self.back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        P._no_udp_connreset(self.front)
        P._no_udp_connreset(self.back)
        self.front.setblocking(False)
        self.back.setblocking(False)
        self.addr = self.front.getsockname()
        self.client: Optional[Tuple[str, int]] = None
        self.stop = threading.Event()
        self.counts = {"up": 0, "dropped": 0, "duplicated": 0, "delayed": 0}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        heap: list = []
        order = itertools.count()
        while not self.stop.is_set():
            now = time.monotonic()
            for _ in range(64):
                try:
                    data, addr = self.front.recvfrom(2048)
                except (BlockingIOError, OSError):
                    break
                self.client = addr
                self.counts["up"] += 1
                if self.rnd.random() < self.loss:
                    self.counts["dropped"] += 1
                    continue
                delay = 0.0
                if self.rnd.random() < self.reorder:
                    delay = 0.020
                    self.counts["delayed"] += 1
                heapq.heappush(heap, (now + delay, next(order), data))
                if self.rnd.random() < self.dup:
                    self.counts["duplicated"] += 1
                    heapq.heappush(heap, (now + delay + 0.002, next(order), data))
            while heap and heap[0][0] <= now:
                _, _, data = heapq.heappop(heap)
                try:
                    self.back.sendto(data, self.target)
                except OSError:
                    pass
            for _ in range(64):
                try:
                    data, _addr = self.back.recvfrom(2048)
                except (BlockingIOError, OSError):
                    break
                if self.client is not None:
                    try:
                        self.front.sendto(data, self.client)
                    except OSError:
                        pass
            time.sleep(0.0005)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        self.front.close()
        self.back.close()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--receiver", required=True, help="the guest's address")
    ap.add_argument("--key", default=r"C:\NimbusVM\netpad.key")
    ap.add_argument("--port", type=int, default=47200)
    ap.add_argument("--monitor-port", type=int, default=47100)
    ap.add_argument("--status-port", type=int, default=47201)
    ap.add_argument("--json", default="", help="write the results here")
    args = ap.parse_args(argv)

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.WinDLL("winmm").timeBeginPeriod(1)
        except OSError:
            pass

    key = P.load_key(args.key)
    target = (args.receiver, args.port)
    guest = Guest(args.receiver, args.monitor_port, args.status_port)
    print(f"netpad end to end against {args.receiver} (key {P.key_fingerprint(key)})", flush=True)
    try:
        st0 = guest.status()
        guest.snapshot()
    except Exception as exc:
        print(f"cannot reach the receiver's status page or the monitor: {exc}")
        return 2
    if st0.get("live"):
        print("a session is already live on the receiver; wait for it to end")
        return 2
    guest.reset()
    started = time.time()

    sender = P.NetpadSender(key, target)
    loop = Loop(sender)

    # E1 ------------------------------------------------------------------
    print("E1 handshake", flush=True)
    seq0 = guest.snapshot()["seq"]
    sender.start()
    slot = wait_connect(guest, loop, seq0)
    check("E1 the sender goes live", sender.status == "live", sender.status)
    if not check("E1 a pad connects in the guest", slot is not None, f"slot {slot}"):
        return finish(args, started, guest)

    # E2 ------------------------------------------------------------------
    print("E2 buttons", flush=True)
    loop.run(0.3)
    seq = guest.snapshot()["seq"]
    for bid in range(1, 15):
        sender.set_button(bid, True)
        loop.run(0.08)
        sender.set_button(bid, False)
        loop.run(0.08)
    loop.run(0.2)
    evs = [(e["kind"], e["button"]) for e in pad_events(guest.snapshot(seq), slot) if e["kind"].startswith("button_")]
    expect = [(k, n) for n in BUTTON_NAMES for k in ("button_down", "button_up")]
    check("E2 14 buttons, in order, once each", evs == expect, f"{len(evs)} events" + ("" if evs == expect else f": {evs}"))

    # E3 ------------------------------------------------------------------
    print("E3 sticks and triggers", flush=True)
    seq = guest.snapshot()["seq"]
    for setter, on, off in (
        (sender.set_left_stick, (0.9, 0.0), (0.0, 0.0)),
        (sender.set_right_stick, (0.0, 0.9), (0.0, 0.0)),
        (sender.set_left_trigger, (1.0,), (0.0,)),
        (sender.set_right_trigger, (1.0,), (0.0,)),
    ):
        setter(*on)
        loop.run(0.3)
        setter(*off)
        loop.run(0.3)
    kinds = [(e["kind"], e.get("stick") or e.get("trigger")) for e in pad_events(guest.snapshot(seq), slot)
             if e["kind"] in ("stick_held", "stick_neutral", "trigger_held", "trigger_released")]
    expect3 = [("stick_held", "left"), ("stick_neutral", "left"), ("stick_held", "right"), ("stick_neutral", "right"),
               ("trigger_held", "left"), ("trigger_released", "left"), ("trigger_held", "right"),
               ("trigger_released", "right")]
    check("E3 sticks and triggers held and released in order", kinds == expect3, str(kinds))

    # E4, E5 --------------------------------------------------------------
    print("E4 explicit Stop, E5 unplug", flush=True)
    sender.set_left_stick(1.0, 0.0)
    loop.run(0.4)
    seq = guest.snapshot()["seq"]
    t_stop = time.monotonic()
    sender.stop()
    ms = time_to_neutral(guest, slot, t_stop, loop, tick=False)
    check("E4 Stop reads neutral in the guest within 100 ms", ms is not None and ms <= 100,
          f"{None if ms is None else round(ms, 1)} ms", stop_ms=ms)
    gone: List[float] = []

    def unplugged() -> bool:
        if any(e["kind"] == "disconnect" for e in pad_events(guest.snapshot(seq), slot)):
            gone.append((time.monotonic() - t_stop) * 1000)
            return True
        return False

    Loop().run(5.0, unplugged, tick=False, poll_every=0.02)
    check("E5 the pad is unplugged 2 s after Stop (2000 to 3000 ms)", bool(gone) and 2000 <= gone[0] <= 3000,
          f"{round(gone[0]) if gone else None} ms", unplug_ms=gone[0] if gone else None)

    seq0 = guest.snapshot()["seq"]
    sender.start()
    slot = wait_connect(guest, loop, seq0)
    if not check("reconnect after Stop", slot is not None and sender.status == "live", f"slot {slot}"):
        return finish(args, started, guest)

    # E6 ------------------------------------------------------------------
    print("E6 connection loss", flush=True)
    loss_ms: List[float] = []
    for trial in range(5):
        sender.set_left_stick(1.0, 0.0)
        ok = loop.run(1.0, lambda: left_mag(guest, slot) >= 0.9, poll_every=0.02)
        if not ok:
            check(f"E6 trial {trial + 1}: the stick is held before the loss", False)
            continue
        loop.run(0.2)
        t_last = sender._last_send
        ms = time_to_neutral(guest, slot, t_last, loop, tick=False)
        if ms is not None:
            loss_ms.append(ms)
        loop.run(0.3)                       # the loop resumes; the stick is still commanded
        held_again = loop.run(1.5, lambda: left_mag(guest, slot) >= 0.9, poll_every=0.02)
        check(f"E6 trial {trial + 1}: neutral 150 to 250 ms after the last packet, then held again",
              ms is not None and 150 <= ms <= 250 and held_again,
              f"{None if ms is None else round(ms, 1)} ms, held again {held_again}")
    sender.set_left_stick(0.0, 0.0)
    loop.run(0.3)
    RESULTS.append({"check": "E6 samples", "ok": True, "loss_ms": loss_ms})

    # E7 ------------------------------------------------------------------
    print("E7 stalled loop tick", flush=True)
    sender.set_left_stick(0.0, 1.0)
    loop.run(1.0, lambda: left_mag(guest, slot) >= 0.9, poll_every=0.02)
    loop.run(0.2)
    sender.send_state_now()
    t_last = time.monotonic()
    stalled_before = guest.status()["stats"].get("stalled_tick", 0)
    ms = time_to_neutral(guest, slot, t_last, loop, tick=False, during=sender.send_state_now)
    stalled = guest.status()["stats"].get("stalled_tick", 0) - stalled_before
    # The side sender shares this loop with the HTTP poll, so about five packets
    # fit in 150 ms rather than nine (5 in both runs on 2026-09-14). The count
    # only has to show packets kept flowing.
    check("E7 packets without a tick go neutral 150 to 250 ms after the last tick", ms is not None and 150 <= ms <= 250
          and stalled >= 3, f"{None if ms is None else round(ms, 1)} ms, {stalled} stalled packets", stall_ms=ms)
    held_again = loop.run(1.5, lambda: left_mag(guest, slot) >= 0.9, poll_every=0.02)
    check("E7 the loop ticking again resumes the held stick", held_again)
    sender.set_left_stick(0.0, 0.0)
    loop.run(0.3)

    # E8 ------------------------------------------------------------------
    print("E8 replay after a timeout", flush=True)
    sender.capture = []
    sender.set_left_stick(-1.0, 0.0)
    loop.run(0.4)
    captured, sender.capture = sender.capture, None
    time_to_neutral(guest, slot, time.monotonic(), loop, tick=False)
    Loop().run(0.1, tick=False)
    before = guest.status()["stats"]
    seq = guest.snapshot()["seq"]
    for pkt in captured:
        sender.sock.sendto(pkt, target)
        time.sleep(0.002)
    Loop().run(0.5, tick=False)
    after = guest.status()["stats"]
    moved = [e for e in pad_events(guest.snapshot(seq), slot) if e["kind"] == "stick_held"]
    rejected = after.get("after_close", 0) - before.get("after_close", 0)
    check("E8 replaying the timed-out session's packets moves nothing", not moved and rejected == len(captured),
          f"{len(captured)} replayed, {rejected} rejected as closed, {len(moved)} stick events")
    sender.set_left_stick(0.0, 0.0)
    loop.run(1.0, lambda: sender.status == "live" and guest.status().get("live"), poll_every=0.05)

    # E9 ------------------------------------------------------------------
    print("E9 second sender", flush=True)
    second = P.NetpadSender(key, target)
    both = Loop(sender, second)
    second.start()
    both.run(0.6)
    check("E9 a second sender is refused while a session is live", second.status == "refused", second.status)
    seq = guest.snapshot()["seq"]
    second.set_button(4, True)
    both.run(0.4)
    ys = [e for e in guest.snapshot(seq)["events"] if e.get("button") == "y"]
    check("E9 what the refused sender presses goes nowhere", not ys, f"{len(ys)} y events")
    sender.stop()
    admitted = Loop(second).run(3.0, lambda: second.status == "live", poll_every=0.01)
    check("E9 the second sender is admitted once the first session ends", admitted, second.status)
    got_y = Loop(second).run(2.0, lambda: any(e.get("button") == "y" and e["kind"] == "button_down"
                                              for e in guest.snapshot(seq)["events"]), poll_every=0.05)
    check("E9 and its held button arrives", got_y)
    new_slot = [e["pad"] for e in guest.snapshot(seq)["events"] if e["kind"] == "button_down" and e.get("button") == "y"]
    second.set_button(4, False)
    Loop(second).run(0.3)
    second.close()
    Loop().run(0.4, tick=False)
    if new_slot:
        slot = new_slot[0]

    # E10 -----------------------------------------------------------------
    print("E10 loss, duplication, reordering through a relay (about 12 s)", flush=True)
    relay = Relay(target, loss=0.2, dup=0.1, reorder=0.1, seed=10)
    relayed = P.NetpadSender(key, relay.addr)
    rloop = Loop(relayed)
    try:
        stats_before = guest.status()["stats"]
        seq0 = guest.snapshot()["seq"]
        relayed.start()
        live = rloop.run(5.0, lambda: relayed.status == "live", poll_every=0.01)
        rslot = None
        if live:
            rloop.run(0.5)
            evs = guest.snapshot(seq0)["events"]
            connects = [e["pad"] for e in evs if e["kind"] == "connect"]
            rslot = connects[-1] if connects else slot
        seq = guest.snapshot()["seq"]
        taps = 40
        for _ in range(taps):
            relayed.set_button(1, True)
            rloop.run(0.04)
            relayed.set_button(1, False)
            rloop.run(0.21)
        rloop.run(0.6)
        evs = [e["kind"] for e in pad_events(guest.snapshot(seq), rslot) if e.get("button") == "a"]
        downs = evs.count("button_down")
        stats_after = guest.status()["stats"]
        timeouts = stats_after.get("timeouts", 0) - stats_before.get("timeouts", 0)
        replayed = stats_after.get("replayed_taps", 0) - stats_before.get("replayed_taps", 0)
        alternating = evs == ["button_down", "button_up"] * (len(evs) // 2)
        check(f"E10 {taps} taps through 20% loss, 10% duplication, 10% reordering arrive exactly once",
              live and downs == taps and alternating,
              f"{downs} presses, {replayed} replayed from counters, {timeouts} timeouts, relay {relay.counts}",
              relay=relay.counts, replayed=replayed, timeouts=timeouts)
    finally:
        relayed.close()
        relay.close()

    return finish(args, started, guest)


def finish(args: argparse.Namespace, started: float, guest: Guest) -> int:
    try:
        counters = guest.snapshot(10 ** 12)["counters"]
        noisy = {k: counters.get(k, 0) for k in QUIET_COUNTERS if counters.get(k, 0)}
        check("E11 nothing but the pad reached the guest (mouse and keyboard counters zero)", not noisy,
              str(noisy) if noisy else f"pad VK keys counted apart: {counters.get('ll_keyboard_gamepad_vk', 0)}")
        status = guest.status()
    except Exception as exc:
        check("E11 read the guest's counters", False, str(exc))
        status = {}
    failed = [r for r in RESULTS if not r["ok"]]
    passed = [r for r in RESULTS if r["ok"] and not r["check"].endswith("samples")]
    print(f"\n{len(passed)} passed, {len(failed)} failed", flush=True)
    out = args.json or os.path.join(r"C:\NimbusVM\logs", time.strftime("netpad-e2e-%Y%m%d-%H%M%S.json"))
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"started": started, "receiver": args.receiver, "results": RESULTS,
                       "receiver_stats": status.get("stats")}, fh, indent=2)
        print(f"results: {out}")
    except OSError as exc:
        print(f"could not write {out}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
