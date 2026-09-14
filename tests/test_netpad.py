"""
Checks for ``netpad/``, the network pad prototype of
``docs/vision/SEPARATE_GAME_MACHINE.md`` section 3.2.

Pure Python, no pad driver. The protocol core runs on a fake clock and a
fake network, so every timing rule is checked exactly; one short section at
the end runs the real socket loop over UDP loopback with generous bounds.

- wire: sizes, round trips, a flipped byte anywhere fails, control and state
  tags cannot stand in for each other, value conversion equals the pad bus
  client's, the button order equals ``vigem_interface.XUSB_BY_ID``
- handshake, wrong key, source-address rule, neutral first state (and a
  sender whose neutral openers are all lost recovers)
- watchdog at 150 ms, pad destroyed 2 s later, explicit Stop, a stalled loop
  tick treated as silence, delayed packets after a timeout rejected, replay
  after a timeout, after a receiver restart and within a session
- resume after a timeout (new session, neutral first), a second sender
  refused and then admitted, the rate cap, packets from the wrong address
- press counters: lost taps replayed in order, a lost release plus re-press
- a seeded run with loss, duplication and reordering: every tap arrives
- a sink that throws cannot disable the watchdog

Run (from the repo root)::

    venv\\Scripts\\python -m tests.test_netpad
"""
from __future__ import annotations

import heapq
import itertools
import os
import random
import sys
import threading
import time
from collections import deque
from typing import Callable, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from netpad import protocol as P  # noqa: E402
from netpad import receiver as R  # noqa: E402

FAILS = 0
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILS, PASSES
    if cond:
        PASSES += 1
    else:
        FAILS += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  ({detail})" if detail else ""))


KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
SENDER_ADDR = ("192.168.50.2", 50000)
SENDER_B_ADDR = ("192.168.50.3", 50001)
RECEIVER_ADDR = ("192.168.50.9", R.DEFAULT_PORT)


class FakeClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeSock:
    def __init__(self) -> None:
        self.sent: List[bytes] = []
        self.inbox: deque = deque()

    def setblocking(self, flag: bool) -> None:
        pass

    def sendto(self, data: bytes, addr: tuple) -> int:
        self.sent.append(bytes(data))
        return len(data)

    def recvfrom(self, n: int) -> Tuple[bytes, tuple]:
        if not self.inbox:
            raise BlockingIOError()
        return self.inbox.popleft()

    def close(self) -> None:
        pass


class Rig:
    """A sender, a receiver core and a recording sink on one fake clock."""

    def __init__(self, key: bytes = KEY) -> None:
        self.clock = FakeClock()
        self.sink = P.RecordingSink(self.clock)
        self.core = P.ReceiverCore(KEY, self.sink, clock=self.clock)
        self.sock = FakeSock()
        self.sender = P.NetpadSender(key, RECEIVER_ADDR, clock=self.clock, sock=self.sock)

    def deliver(self, sender: Optional[P.NetpadSender] = None, addr: tuple = SENDER_ADDR,
                drop: Optional[Callable[[bytes], bool]] = None) -> List[bytes]:
        """Hand everything the sender sent to the receiver, and the replies back, until quiet."""
        sender = sender or self.sender
        replies_all: List[bytes] = []
        for _ in range(20):
            if not sender.sock.sent:
                break
            batch, sender.sock.sent = sender.sock.sent, []
            for pkt in batch:
                if drop is not None and drop(pkt):
                    continue
                for rep in self.core.handle(pkt, addr):
                    replies_all.append(rep)
                    sender.sock.inbox.append((rep, RECEIVER_ADDR))
            sender._poll(self.clock())
        return replies_all

    def connect(self) -> None:
        self.sender.start()
        self.deliver()

    def run(self, seconds: float, tick_hz: float = 120.0, deliver: bool = True, ticking: bool = True) -> None:
        """Advance time in 1 ms steps, ticking the sender and servicing the receiver."""
        steps = int(round(seconds / 0.001))
        next_tick = self.clock.t
        for _ in range(steps):
            if ticking and self.clock.t >= next_tick:
                self.sender.tick()
                next_tick += 1.0 / tick_hz
            if deliver:
                self.deliver()
            self.core.service()
            self.clock.advance(0.001)


def ptype(pkt: bytes) -> int:
    parts = P.split(pkt)
    return parts[0] if parts else -1


def first_time(sink: P.RecordingSink, pred: Callable[[P.PadState], bool], after: float) -> Optional[float]:
    for t, kind, st in sink.calls:
        if t >= after and kind == "apply" and st is not None and pred(st):
            return t
    return None


# ---------------------------------------------------------------------------
def test_wire() -> None:
    print("wire")
    sid, cn, sn = b"S" * 8, b"C" * 16, b"N" * 16
    ks = P.session_key(KEY, cn, sn, sid)
    counters = bytes(range(14))
    st = P.PadState(1000, -2000, 32767, -32768, 7, 255, 0x2AAA)
    packets = {
        "hello": P.encode_hello(KEY, cn),
        "welcome": P.encode_welcome(KEY, cn, sn, sid),
        "refuse": P.encode_refuse(KEY, cn, sid, P.REASON_BUSY),
        "expired": P.encode_expired(KEY, sid, P.REASON_TIMEOUT),
        "state": P.encode_state(ks, sid, 5, 9, st, counters, P.FLAG_END),
    }
    sizes = {"hello": 38, "welcome": 62, "refuse": 47, "expired": 31, "state": 65}
    for name, pkt in packets.items():
        check(f"{name} is {sizes[name]} bytes", len(pkt) == sizes[name], f"got {len(pkt)}")
    parts = P.split(packets["state"])
    check("state splits back to its type", parts is not None and parts[0] == P.T_STATE)
    fields = P._STATE.unpack(parts[1])
    check("state fields round-trip", fields == (sid, 5, 9, 1000, -2000, 32767, -32768, 7, 255, 0x2AAA, P.FLAG_END, counters))
    check("state tag verifies with the session key", P.state_tag_ok(ks, parts[3], parts[2]))
    check("state tag fails with the long-term key", not P.state_tag_ok(KEY, parts[3], parts[2]))
    hello = P.split(packets["hello"])
    check("hello tag verifies", P.control_tag_ok(KEY, hello[3], hello[2]))
    check("a control tag is not a state tag, even under the same key", not P.state_tag_ok(KEY, hello[3], hello[2]))
    flipped_ok = 0
    raw = packets["state"]
    for i in range(len(raw)):
        bad = bytearray(raw)
        bad[i] ^= 0x01
        p = P.split(bytes(bad))
        if p is not None and p[0] == P.T_STATE and P.state_tag_ok(ks, p[3], p[2]):
            flipped_ok += 1
    check("flipping any single bit of a state packet fails it", flipped_ok == 0, f"{flipped_ok} passed")
    check("short, long, bad magic and bad version are rejected",
          P.split(raw[:-1]) is None and P.split(raw + b"x") is None
          and P.split(b"XPAD" + raw[4:]) is None and P.split(raw[:4] + bytes([9]) + raw[5:]) is None)

    from src.padbus_client import X360ReportState, XUSB_BUTTON  # noqa: E402
    rep = X360ReportState()
    same = True
    for v in (-1.2, -1.0, -0.5, -0.00002, 0.0, 0.3333, 0.5, 0.99999, 1.0, 1.5):
        rep.left_joystick_float(v, v)
        rep.left_trigger_float(max(0.0, v))
        same &= rep.report.sThumbLX == P.stick_to_i16(v) and rep.report.bLeftTrigger == P.trigger_to_u8(max(0.0, v))
    check("stick and trigger conversion equal the pad bus client's", same)
    from src.vigem_interface import XUSB_BY_ID  # noqa: E402
    order = [int(getattr(XUSB_BUTTON, n)) for n in R.XUSB_ORDER]
    check("button bit i is Nimbus button i + 1 on the ViGEm pad", order == [int(XUSB_BY_ID[i]) for i in range(1, 15)])

    good = ["192.168.1.2", "10.0.0.5", "172.17.227.55", "127.0.0.1", "169.254.3.4", "fe80::1", "::1",
            "::ffff:192.168.0.1", "fd12::1"]
    bad = ["8.8.8.8", "100.64.0.1", "2001:4860::8888", "::ffff:8.8.8.8", "not-an-ip", ""]
    check("private, loopback and link-local sources are allowed", all(P.is_local_source(h) for h in good),
          str([h for h in good if not P.is_local_source(h)]))
    check("public and malformed sources are refused", not any(P.is_local_source(h) for h in bad),
          str([h for h in bad if P.is_local_source(h)]))


def test_handshake() -> None:
    print("handshake")
    rig = Rig()
    rig.connect()
    check("the sender goes live", rig.sender.status == "live", rig.sender.status)
    check("the receiver has one live session", rig.core.live is not None)
    check("the pad was opened", rig.sink.is_open)
    check("the first apply is neutral", [c for c in rig.sink.calls if c[1] == "apply"][0][2] == P.NEUTRAL)
    rig.sender.set_left_stick(0.5, -0.25)
    rig.deliver()
    check("a stick change arrives", rig.sink.state.lx == P.stick_to_i16(0.5) and rig.sink.state.ly == P.stick_to_i16(-0.25))

    wrong = Rig(key=OTHER_KEY)
    wrong.sender.start()
    replies = wrong.deliver()
    check("a sender with the wrong key gets no reply", replies == [] and wrong.sender.status == "handshake")
    check("and is counted as a bad tag", wrong.core.stats.get("bad_tag", 0) >= 1)

    far = Rig()
    far.sender.start()
    replies = far.deliver(addr=("8.8.8.8", 40000))
    check("a public source is ignored without a reply", replies == [] and far.core.stats.get("not_local") == 1)

    retry = Rig()
    retry.sender.start()
    hello = retry.sock.sent[0]
    retry.sock.sent = []
    r1 = retry.core.handle(hello, SENDER_ADDR)
    r2 = retry.core.handle(hello, SENDER_ADDR)
    check("a retried HELLO gets the same WELCOME", r1 == r2 and len(r1) == 1)

    # Neutral first: a pending session whose first state holds a stick is closed with EXPIRED.
    nn = Rig()
    nn.sender.set_left_stick(1.0, 0.0)
    nn.sender.start()
    welcome = nn.core.handle(nn.sock.sent.pop(0), SENDER_ADDR)[0]
    nn.sock.inbox.append((welcome, RECEIVER_ADDR))
    nn.sender._poll(nn.clock())                    # sends neutral, neutral, held
    held = nn.sock.sent[-1]
    nn.sock.sent = []
    reply = nn.core.handle(held, SENDER_ADDR)
    check("a non-neutral first state is refused with EXPIRED", len(reply) == 1 and ptype(reply[0]) == P.T_EXPIRED
          and nn.core.live is None and nn.core.stats.get("not_neutral_first") == 1)

    # A sender whose neutral openers are all lost recovers through a new handshake.
    lost = Rig()
    lost.sender.set_left_stick(1.0, 0.0)
    dropped = {"n": 0}

    def drop_openers(pkt: bytes) -> bool:
        parts = P.split(pkt)
        if parts and parts[0] == P.T_STATE and dropped["n"] < 2:
            dropped["n"] += 1
            return True
        return False

    lost.sender.start()
    lost.deliver(drop=drop_openers)
    lost.run(0.05)
    check("with both neutral openers lost, the sender re-handshakes and the held stick arrives",
          lost.sink.state.lx == 32767 and lost.core.stats.get("sessions") == 1
          and lost.sender.stats.get("expired", 0) == 1, f"{lost.core.stats} {lost.sender.stats}")


def test_watchdog_and_stop() -> None:
    print("watchdog, stop, replay")
    rig = Rig()
    rig.connect()
    rig.sender.set_left_stick(1.0, 0.0)
    rig.run(0.5)
    check("a held stick stays held while the loop ticks", rig.sink.state.lx == 32767 and rig.core.live is not None)
    capture: List[bytes] = []
    rig.sender.capture = capture
    rig.run(0.2)
    rig.sender.capture = None
    last_progress = rig.core.live.last_progress
    rig.run(0.149 - (rig.clock.t - last_progress), ticking=False)
    check("still held just inside 150 ms of silence", rig.sink.state.lx == 32767)
    rig.run(0.004, ticking=False)
    t_neutral = first_time(rig.sink, lambda s: s.is_neutral, last_progress)
    check("neutral within 150 ms plus one service step", t_neutral is not None
          and 0.150 <= t_neutral - last_progress <= 0.152, f"{None if t_neutral is None else round((t_neutral - last_progress) * 1000, 2)} ms")
    check("the timeout is counted and the pad stays plugged", rig.core.stats.get("timeouts") == 1 and rig.sink.is_open)

    replies: List[bytes] = []
    for pkt in capture:
        replies += rig.core.handle(pkt, SENDER_ADDR)
    rig.core.service()
    check("replaying the session's packets after the timeout moves nothing", rig.sink.state.is_neutral
          and rig.core.stats.get("after_close", 0) == len(capture), f"{rig.core.stats}")
    check("and each is answered EXPIRED (up to the reply cap)",
          replies and all(ptype(r) == P.T_EXPIRED for r in replies))

    t_idle = rig.core.idle_since
    rig.run(1.99 - (rig.clock.t - t_idle), ticking=False, deliver=False)
    check("the pad is still plugged just before 2 s idle", rig.sink.is_open)
    rig.run(0.02, ticking=False, deliver=False)
    closes = [t for t, kind, _ in rig.sink.calls if kind == "close"]
    check("the pad is destroyed 2 s after the session ended", len(closes) == 1 and 2.0 <= closes[0] - t_idle <= 2.002,
          f"{[round(c - t_idle, 4) for c in closes]}")

    restarted = P.ReceiverCore(KEY, P.RecordingSink(rig.clock), clock=rig.clock)
    replies = []
    for pkt in capture:
        replies += restarted.handle(pkt, SENDER_ADDR)
    check("replay against a restarted receiver: unknown session, no pad",
          restarted.stats.get("unknown_session") == len(capture) and not restarted.pad_open
          and all(ptype(r) == P.T_EXPIRED for r in replies))

    # Within a live session: duplicates and older sequence numbers are dropped.
    live = Rig()
    live.connect()
    live.sender.capture = []
    live.sender.set_button(1, True)
    live.sender.set_button(1, False)
    press, release = live.sender.capture
    live.sender.capture = None
    live.deliver()
    before = list(live.sink.calls)
    live.core.handle(press, SENDER_ADDR)
    live.core.handle(release, SENDER_ADDR)
    live.core.service()
    check("a replayed press within the session is a stale sequence number, not a press",
          live.sink.calls == before and live.core.stats.get("stale_seq") == 2)

    # Explicit Stop: neutral at once, END closes the session, later packets EXPIRED.
    stop = Rig()
    stop.connect()
    stop.sender.set_left_stick(0.0, 1.0)
    stop.run(0.1)
    t0 = stop.clock.t
    stop.sender.stop()
    stop.deliver()
    check("Stop goes neutral in the same instant", stop.sink.state.is_neutral
          and first_time(stop.sink, lambda s: s.is_neutral, t0) == t0)
    check("and ends the session", stop.core.live is None and stop.core.stats.get("ends") == 1)
    stop.run(2.01, ticking=False, deliver=False)
    check("the pad is destroyed 2 s after Stop", not stop.sink.is_open)

    # A loop that keeps transmitting but no longer ticks is silence.
    stall = Rig()
    stall.connect()
    stall.sender.set_left_stick(-1.0, 0.0)
    stall.run(0.2)
    stall.sender.send_state_now()            # the receiver has now seen the loop's latest tick
    stall.deliver()
    t_stall = stall.core.live.last_progress
    for _ in range(40):                      # 16 ms apart for 640 ms, the tick never advancing
        stall.sender.send_state_now()
        stall.deliver()
        for _ in range(16):
            stall.core.service()
            stall.clock.advance(0.001)
    t_neutral = first_time(stall.sink, lambda s: s.is_neutral, t_stall)
    check("a stalled loop tick goes neutral at 150 ms even with packets flowing",
          t_neutral is not None and 0.150 <= t_neutral - t_stall <= 0.152 and stall.core.stats.get("stalled_tick", 0) >= 9,
          f"{None if t_neutral is None else round((t_neutral - t_stall) * 1000, 2)} ms, {stall.core.stats.get('stalled_tick')}")


def test_resume_refuse_rate() -> None:
    print("resume, second sender, rate cap")
    rig = Rig()
    rig.connect()
    first_sid = rig.core.live.sid
    rig.sender.set_left_stick(1.0, 0.0)
    rig.run(0.2)
    rig.run(0.3, ticking=False)             # the loop freezes past the watchdog
    check("frozen loop: neutral", rig.sink.state.is_neutral and rig.core.live is None)
    t_resume = rig.clock.t
    rig.run(0.1)                            # the loop comes back, stick still commanded
    check("the loop coming back opens a new session", rig.core.live is not None and rig.core.live.sid != first_sid
          and rig.sender.stats.get("expired") == 1)
    applies = [st for t, kind, st in rig.sink.calls if kind == "apply" and t >= t_resume]
    check("the resumed session starts neutral, then the held stick", len(applies) >= 1 and applies[-1].lx == 32767
          and all(a.is_neutral for a in applies[:-1]), f"{[(a.lx) for a in applies]}")

    # A second sender while one is live.
    a = Rig()
    a.connect()
    sock_b = FakeSock()
    b = P.NetpadSender(KEY, RECEIVER_ADDR, clock=a.clock, sock=sock_b)
    b.start()
    a.deliver(sender=b, addr=SENDER_B_ADDR)
    check("a second sender is refused while a session is live", b.status == "refused" and a.core.stats.get("refused") == 1)
    b.set_button(2, True)
    a.deliver(sender=b, addr=SENDER_B_ADDR)
    check("and what it presses goes nowhere", not a.sink.state.pressed(2))
    a.sender.stop()
    a.deliver()
    for _ in range(1100):
        b.tick()
        a.deliver(sender=b, addr=SENDER_B_ADDR)
        a.core.service()
        a.clock.advance(0.001)
    check("once the first session ends, the second sender is admitted on its retry", b.status == "live")
    check("and its held button arrives", a.sink.state.pressed(2))

    # A session caught between WELCOME and its first state while another went live.
    c = Rig()
    c.sender.start()
    welcome = c.core.handle(c.sock.sent.pop(0), SENDER_ADDR)
    sock_d = FakeSock()
    d = P.NetpadSender(KEY, RECEIVER_ADDR, clock=c.clock, sock=sock_d)
    d.start()
    c.deliver(sender=d, addr=SENDER_B_ADDR)          # d goes live first
    c.sock.inbox.append((welcome[0], RECEIVER_ADDR))
    c.sender._poll(c.clock())                         # c now sends its neutral opener
    c.deliver()
    check("a session whose opener arrives after another went live is refused", c.sender.status == "refused"
          and c.core.live is not None and c.core.live.addr == SENDER_B_ADDR)

    # Rate cap: 500 accepted per session per second.
    r = Rig()
    r.connect()
    for _ in range(700):
        r.sender.loop_tick += 1
        r.sender.send_state_now()
    r.deliver()
    accepted = r.core.live.window_count
    check("the rate cap accepts at most 500 packets a second", accepted == P.RATE_CAP_PER_S
          and r.core.stats.get("rate_dropped") == 700 - (P.RATE_CAP_PER_S - 2), f"accepted {accepted}, {r.core.stats}")
    r.clock.advance(1.0)
    r.sender.set_button(3, True)
    r.deliver()
    check("a new second accepts packets again", r.sink.state.pressed(3))

    w = Rig()
    w.connect()
    w.sender.set_button(4, True)
    w.deliver(addr=("192.168.50.77", 50000))
    check("a live session's packet from another address is dropped", not w.sink.state.pressed(4)
          and w.core.stats.get("wrong_addr") == 1)


def test_press_counters() -> None:
    print("press counters")
    rig = Rig()
    rig.connect()
    drop_states = lambda pkt: ptype(pkt) == P.T_STATE  # noqa: E731
    for _ in range(3):
        rig.sender.set_button(1, True)
        rig.sender.set_button(1, False)
    rig.deliver(drop=drop_states)
    check("three taps whose packets were all lost are not seen yet", not rig.sink.state.pressed(1))
    t0 = rig.clock.t
    rig.sender.send_state_now()
    rig.deliver()
    rig.run(0.5)
    edges = [(round(t - t0, 3), p) for t, p in rig.sink.button_edges(1)]
    presses = [t for t, p in edges if p]
    holds = [edges[i + 1][0] - edges[i][0] for i in range(0, len(edges) - 1, 2)]
    check("the next packet replays three taps", len(presses) == 3 and rig.core.stats.get("replayed_taps") == 3, str(edges))
    check("each replayed press is held 50 ms and they come in order",
          all(abs(h - P.TAP_HOLD_S) < 0.0015 for h in holds) and presses == sorted(presses), str(edges))
    check("the button ends released", not rig.sink.state.pressed(1))

    rp = Rig()
    rp.connect()
    rp.sender.set_button(1, True)
    rp.deliver()
    rp.sender.set_button(1, False)
    rp.sender.set_button(1, True)
    rp.deliver(drop=drop_states)                    # the release and the re-press are lost
    rp.sender.send_state_now()
    rp.deliver()
    rp.run(0.3)
    edges = [p for _, p in rp.sink.button_edges(1)]
    check("a lost release plus re-press shows as release then press", edges == [True, False, True], str(edges))


def test_fuzz() -> None:
    print("loss, duplication, reordering")
    for seed, loss, dup, reorder in ((1, 0.2, 0.1, 0.1), (2, 0.3, 0.05, 0.2), (3, 0.1, 0.3, 0.3)):
        rnd = random.Random(seed)
        clock = FakeClock()
        sink = P.RecordingSink(clock)
        core = P.ReceiverCore(KEY, sink, clock=clock)
        sock = FakeSock()
        sender = P.NetpadSender(KEY, RECEIVER_ADDR, clock=clock, sock=sock)
        sender.start()
        for _ in range(5):                       # a clean handshake; loss starts afterwards
            for pkt in sock.sent:
                for rep in core.handle(pkt, SENDER_ADDR):
                    sock.inbox.append((rep, RECEIVER_ADDR))
            sock.sent = []
            sender._poll(clock())
        taps = 50
        actions = []
        for k in range(taps):
            t = clock.t + 0.3 + k * 0.2
            actions.append((t, lambda: sender.set_button(1, True)))
            actions.append((t + 0.03, lambda: sender.set_button(1, False)))
            actions.append((t + 0.06, lambda k=k: sender.set_left_stick((k % 5) / 5.0, 0.0)))
        actions.sort(key=lambda a: a[0])
        heap: list = []
        order = itertools.count()
        end = clock.t + 0.3 + taps * 0.2 + 0.6
        next_tick = clock.t
        ai = 0
        while clock.t < end:
            while ai < len(actions) and actions[ai][0] <= clock.t:
                actions[ai][1]()
                ai += 1
            if clock.t >= next_tick:
                sender.tick()
                next_tick += 1.0 / 60
            for pkt in sock.sent:
                if rnd.random() < loss:
                    continue
                delay = 0.001 + (0.02 if rnd.random() < reorder else 0.0)
                heapq.heappush(heap, (clock.t + delay, next(order), pkt))
                if rnd.random() < dup:
                    heapq.heappush(heap, (clock.t + delay + 0.002, next(order), pkt))
            sock.sent = []
            while heap and heap[0][0] <= clock.t:
                _, _, pkt = heapq.heappop(heap)
                for rep in core.handle(pkt, SENDER_ADDR):
                    sock.inbox.append((rep, RECEIVER_ADDR))
            core.service()
            clock.advance(0.001)
        presses = sum(1 for _, p in sink.button_edges(1) if p)
        label = f"seed {seed}: {int(loss * 100)}% loss, {int(dup * 100)}% dup, {int(reorder * 100)}% reordered"
        timeouts = core.stats.get("timeouts", 0)
        if loss <= 0.2:
            check(f"{label}: no timeout", timeouts == 0, str(core.stats))
        else:
            # At 30% loss a run of nine lost keepalives outlasts 150 ms now and
            # then; that is the watchdog doing its job. What must hold is that
            # every timeout resumed. (Seeded, so the count is stable: one here,
            # and it falls between taps.)
            check(f"{label}: every timeout resumed a session ({timeouts} timeouts)",
                  core.stats.get("sessions", 0) == timeouts + 1 and sender.status == "live", str(core.stats))
        check(f"{label}: all {taps} taps arrive exactly once", presses == taps,
              f"{presses} presses, replayed {core.stats.get('replayed_taps', 0)}, stale {core.stats.get('stale_seq', 0)}")
        check(f"{label}: ends released with the last stick value", not sink.state.pressed(1)
              and sink.state.lx == P.stick_to_i16(((taps - 1) % 5) / 5.0))


def test_sink_failure() -> None:
    print("sink failure")

    class Broken(P.RecordingSink):
        def apply(self, state: P.PadState) -> None:
            super().apply(state)
            if state.lx:
                raise OSError("bus refused the report")

    clock = FakeClock()
    sink = Broken(clock)
    core = P.ReceiverCore(KEY, sink, clock=clock)
    sock = FakeSock()
    sender = P.NetpadSender(KEY, RECEIVER_ADDR, clock=clock, sock=sock)
    rig = Rig()
    rig.clock, rig.sink, rig.core, rig.sock, rig.sender = clock, sink, core, sock, sender
    rig.connect()
    sender.set_left_stick(1.0, 0.0)
    rig.run(0.1)
    rig.run(0.3, ticking=False)
    check("a throwing sink is counted", core.stats.get("sink_errors", 0) >= 1)
    check("and the watchdog still ends the session and applies neutral", core.live is None
          and core.stats.get("timeouts") == 1 and sink.state.is_neutral)


def test_loopback() -> None:
    print("UDP loopback (real sockets, real time)")
    sink = P.RecordingSink()
    core = P.ReceiverCore(KEY, sink)
    sock = R.open_socket("127.0.0.1", 0)
    port = sock.getsockname()[1]
    rx = R.Receiver(core, sock)
    thread = threading.Thread(target=rx.serve, daemon=True)
    thread.start()
    sender = P.NetpadSender(KEY, ("127.0.0.1", port))
    try:
        sender.start()
        deadline = time.monotonic() + 3.0
        while sender.status != "live" and time.monotonic() < deadline:
            sender.tick()
            time.sleep(0.005)
        check("handshake over loopback", sender.status == "live")
        sender.set_button(1, True)
        sender.set_left_stick(1.0, 0.0)
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            sender.tick()
            time.sleep(1 / 120)
        with rx.lock:
            held = sink.state.pressed(1) and sink.state.lx == 32767
        check("the held button and stick arrive and stay held for 0.5 s", held)
        # Measured from the last packet the loop sent, which can be a keepalive
        # interval before the loop stopped.
        t_last = sender._last_send
        neutral_at = None
        while time.monotonic() - t_last < 1.5:
            with rx.lock:
                if sink.state.is_neutral:
                    neutral_at = time.monotonic()
                    break
            time.sleep(0.001)
        took = None if neutral_at is None else (neutral_at - t_last) * 1000
        # Real time on a shared CI runner: the bound here is loose on purpose;
        # the exact 150 ms rule is checked on the fake clock above.
        check("silence goes neutral near the watchdog (145 to 400 ms after the last packet)",
              took is not None and 145 <= took <= 400, f"{None if took is None else round(took, 1)} ms")
    finally:
        sender.close()
        rx.stop.set()
        thread.join(timeout=2)
        rx.shutdown()
        sock.close()


def main() -> int:
    print("Netpad protocol checks")
    test_wire()
    test_handshake()
    test_watchdog_and_stop()
    test_resume_refuse_rate()
    test_press_counters()
    test_fuzz()
    test_sink_failure()
    test_loopback()
    print(f"\n{PASSES} passed, {FAILS} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
