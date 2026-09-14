"""
Netpad protocol: packets, the receiver's rules and the sender's side of them.

Research prototype for ``docs/vision/SEPARATE_GAME_MACHINE.md`` section 3.2.
Nothing here opens a socket or a pad: the clock, the random source and the
pad itself (a :class:`Sink`) are injected, so every rule below is testable
without a network or a driver (``tests/test_netpad.py``). ``receiver.py``
adds the socket loop and the real pads.

The agreed contract (2026-09-14)
--------------------------------
Values chosen by the project owner; each is one constant below.

- Keepalive at 60 Hz. The pad goes neutral when no fresh, authenticated
  packet has arrived for 150 ms, and is destroyed after 2 s with no live
  session. At most 500 packets per second per session are accepted; the
  excess is dropped.
- Authentication by a shared 256-bit key file, copied to the sender by hand.
  Each session starts with a nonce handshake that derives a session key, and
  every packet carries an HMAC-SHA256 tag (truncated to 128 bits). PIN
  pairing is deliberately not here: it needs a reviewed password-authenticated
  key exchange (phase 2 of the plan).
- One live session. A second sender is refused while one is live. After a
  timeout the old session is dead for good; the sender resumes by opening a
  new session whose first state must be neutral, with no extra action.

Messages
--------
Every datagram is ``header | body | tag``: the header is ``b"NPAD"``, the
version and the type. ``HELLO``, ``WELCOME``, ``REFUSE`` and ``EXPIRED`` are
tagged with the long-term key (under a label, so a control tag can never
pass as a state tag); ``STATE`` is tagged with the session key.

``HELLO``    sender to receiver: a client nonce.
``WELCOME``  the client nonce echoed, a server nonce, the session id. Both
             sides derive the session key from the three.
``STATE``    session id, sequence number, the sender's loop tick, both
             sticks (int16, +y up, XInput's convention), both triggers
             (uint8), the button mask (bit ``i`` is Nimbus button ``i + 1``),
             flags (``END``) and a press counter per button (uint8, wraps).
``REFUSE``   another session is live.
``EXPIRED``  the session the packet named is over (timed out, ended, or
             unknown because the receiver restarted); open a new one.

What each rule is for
---------------------
- Full state in every packet: a lost packet costs one interval and can never
  leave a control stuck.
- Press counters: full state alone loses a tap whose pressed state travelled
  only in lost packets. When a counter has advanced further than the
  arriving state explains, the receiver plays the missing presses back as
  taps held :data:`TAP_HOLD_S`, in order, before the live state.
- The loop tick: a packet refreshes the watchdog only if the sender's tick
  advanced. The sender's tick advances only in :meth:`NetpadSender.tick`,
  which the app calls from the loop that updates the controls, so a frozen
  UI stops refreshing the watchdog even if something keeps transmitting.
- Watchdog before acceptance: a session is checked for timeout before a
  packet is applied, so a burst of delayed packets arriving after the 150 ms
  have passed is rejected rather than resuming held input.
- Session ids and keys are fresh per handshake and live only in memory, so
  captured packets fail after a timeout and after a receiver restart.
- Sources outside the private, loopback and link-local ranges are ignored,
  which also catches traffic arriving through a router's port forward.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable, Deque, Dict, List, Optional, Tuple

# ---- wire ---------------------------------------------------------------------
MAGIC = b"NPAD"
VERSION = 1

T_HELLO = 1
T_WELCOME = 2
T_STATE = 3
T_REFUSE = 4
T_EXPIRED = 5

KEY_LEN = 32
NONCE_LEN = 16
SESSION_ID_LEN = 8
TAG_LEN = 16
BUTTONS = 14

FLAG_END = 0x01

REASON_BUSY = 1
REASON_TIMEOUT = 2
REASON_ENDED = 3
REASON_UNKNOWN = 4
REASON_NOT_NEUTRAL = 5
REASON_NAMES = {REASON_BUSY: "busy", REASON_TIMEOUT: "timeout", REASON_ENDED: "ended", REASON_UNKNOWN: "unknown",
                REASON_NOT_NEUTRAL: "first state not neutral"}

# ---- the agreed contract (2026-09-14) -------------------------------------------
KEEPALIVE_HZ = 60
WATCHDOG_S = 0.150
DESTROY_S = 2.0
RATE_CAP_PER_S = 500

# ---- implementation choices, not part of the owner's decision ------------------
#: How long a replayed (lost) press is held, and the release after it. Two
#: frames at 30 fps; a game that samples slower than that could still miss one.
TAP_HOLD_S = 0.050
TAP_GAP_S = 0.050
HELLO_RETRY_S = 0.25
REFUSED_RETRY_S = 1.0
PENDING_TTL_S = 2.0
MAX_PENDING = 8
CLOSED_TTL_S = 10.0
REPLY_CAP_PER_S = 20

_HEADER = struct.Struct("<4sBB")
_HELLO = struct.Struct("<16s")
_WELCOME = struct.Struct("<16s16s8s")
_REFUSE = struct.Struct("<16s8sB")
_EXPIRED = struct.Struct("<8sB")
_STATE = struct.Struct("<8sIIhhhhBBHB14s")

_BODY = {T_HELLO: _HELLO, T_WELCOME: _WELCOME, T_STATE: _STATE, T_REFUSE: _REFUSE, T_EXPIRED: _EXPIRED}

_CTL_LABEL = b"netpad-ctl-v1"
_STATE_LABEL = b"netpad-state-v1"
_SESSION_LABEL = b"netpad-session-v1"

Addr = Tuple  # (host, port) as the socket module hands it over


# ---- keys -----------------------------------------------------------------------
def new_key() -> bytes:
    return secrets.token_bytes(KEY_LEN)


def key_fingerprint(key: bytes) -> str:
    """Eight hex digits that let two machines confirm they hold the same key."""
    return hashlib.sha256(b"netpad-fingerprint" + key).hexdigest()[:8]


def write_key(path: str, key: Optional[bytes] = None) -> bytes:
    """Write a new key as hex, readable only by its owner where the OS allows."""
    key = key or new_key()
    with open(path, "w", encoding="ascii") as fh:
        fh.write(key.hex() + "\n")
    if sys.platform != "win32":
        os.chmod(path, 0o600)
    return key


def load_key(path: str) -> bytes:
    with open(path, "r", encoding="ascii") as fh:
        key = bytes.fromhex(fh.read().strip())
    if len(key) != KEY_LEN:
        raise ValueError(f"{path}: a netpad key is {KEY_LEN} bytes, this is {len(key)}")
    return key


def session_key(key: bytes, client_nonce: bytes, server_nonce: bytes, session_id: bytes) -> bytes:
    return hmac.new(key, _SESSION_LABEL + client_nonce + server_nonce + session_id, hashlib.sha256).digest()


def _tag(key: bytes, label: bytes, signed: bytes) -> bytes:
    return hmac.new(key, label + signed, hashlib.sha256).digest()[:TAG_LEN]


# ---- values ---------------------------------------------------------------------
def stick_to_i16(value: float) -> int:
    """The pad bus client's conversion (``round(v * 32767)``, clamped), so both ends agree."""
    return max(-32768, min(32767, int(round(float(value) * 32767))))


def trigger_to_u8(value: float) -> int:
    return max(0, min(255, int(round(float(value) * 255))))


@dataclass(frozen=True)
class PadState:
    """One Xbox 360 pad report: sticks int16 (+y up), triggers uint8, 14-bit button mask."""

    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    lt: int = 0
    rt: int = 0
    buttons: int = 0

    @property
    def is_neutral(self) -> bool:
        return self == NEUTRAL

    def pressed(self, button_id: int) -> bool:
        return bool(self.buttons & (1 << (button_id - 1)))


NEUTRAL = PadState()


# ---- encoding -------------------------------------------------------------------
def _pack(ptype: int, body: bytes, key: bytes, label: bytes) -> bytes:
    signed = _HEADER.pack(MAGIC, VERSION, ptype) + body
    return signed + _tag(key, label, signed)


def encode_hello(key: bytes, client_nonce: bytes) -> bytes:
    return _pack(T_HELLO, _HELLO.pack(client_nonce), key, _CTL_LABEL)


def encode_welcome(key: bytes, client_nonce: bytes, server_nonce: bytes, session_id: bytes) -> bytes:
    return _pack(T_WELCOME, _WELCOME.pack(client_nonce, server_nonce, session_id), key, _CTL_LABEL)


def encode_refuse(key: bytes, client_nonce: bytes, session_id: bytes, reason: int) -> bytes:
    return _pack(T_REFUSE, _REFUSE.pack(client_nonce, session_id, reason), key, _CTL_LABEL)


def encode_expired(key: bytes, session_id: bytes, reason: int) -> bytes:
    return _pack(T_EXPIRED, _EXPIRED.pack(session_id, reason), key, _CTL_LABEL)


def encode_state(session_key_: bytes, session_id: bytes, seq: int, tick: int, state: PadState,
                 counters: bytes, flags: int = 0) -> bytes:
    body = _STATE.pack(session_id, seq & 0xFFFFFFFF, tick & 0xFFFFFFFF, state.lx, state.ly, state.rx, state.ry,
                       state.lt, state.rt, state.buttons & 0x3FFF, flags, bytes(counters))
    return _pack(T_STATE, body, session_key_, _STATE_LABEL)


def split(data: bytes) -> Optional[Tuple[int, bytes, bytes, bytes]]:
    """``(type, body, tag, signed)`` for a well-formed datagram of a known type, else None."""
    if len(data) < _HEADER.size + TAG_LEN:
        return None
    magic, version, ptype = _HEADER.unpack_from(data)
    if magic != MAGIC or version != VERSION or ptype not in _BODY:
        return None
    if len(data) != _HEADER.size + _BODY[ptype].size + TAG_LEN:
        return None
    signed = data[:-TAG_LEN]
    return ptype, data[_HEADER.size:-TAG_LEN], data[-TAG_LEN:], signed


def control_tag_ok(key: bytes, signed: bytes, tag: bytes) -> bool:
    return hmac.compare_digest(tag, _tag(key, _CTL_LABEL, signed))


def state_tag_ok(session_key_: bytes, signed: bytes, tag: bytes) -> bool:
    return hmac.compare_digest(tag, _tag(session_key_, _STATE_LABEL, signed))


def is_local_source(host: str) -> bool:
    """True for private, loopback and link-local addresses (IPv4-mapped IPv6 unwrapped)."""
    try:
        ip = ipaddress.ip_address(str(host).split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


# ---- receiver -------------------------------------------------------------------
class Sink:
    """The pad a receiver drives. ``open`` may take a while (a bus plugs a device)."""

    def open(self) -> None:
        raise NotImplementedError

    def apply(self, state: PadState) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class RecordingSink(Sink):
    """Keeps every call with the clock reading, for tests and ``--sink log``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.calls: List[Tuple[float, str, Optional[PadState]]] = []
        self.is_open = False
        self.state = NEUTRAL

    def open(self) -> None:
        self.is_open = True
        self.calls.append((self.clock(), "open", None))

    def apply(self, state: PadState) -> None:
        self.state = state
        self.calls.append((self.clock(), "apply", state))

    def close(self) -> None:
        self.is_open = False
        self.state = NEUTRAL
        self.calls.append((self.clock(), "close", None))

    def button_edges(self, button_id: int) -> List[Tuple[float, bool]]:
        """``(time, pressed)`` for every change of one button across applies."""
        out: List[Tuple[float, bool]] = []
        last = False
        for t, kind, st in self.calls:
            now = st.pressed(button_id) if (kind == "apply" and st is not None) else (False if kind == "close" else last)
            if now != last:
                out.append((t, now))
                last = now
        return out


class _Session:
    __slots__ = ("sid", "key", "addr", "client_nonce", "server_nonce", "created", "status", "last_seq",
                 "last_tick", "last_progress", "counters", "window_start", "window_count", "closed_at",
                 "close_reason")

    def __init__(self, sid: bytes, key: bytes, addr: Addr, client_nonce: bytes, server_nonce: bytes,
                 now: float) -> None:
        self.sid = sid
        self.key = key
        self.addr = addr
        self.client_nonce = client_nonce
        self.server_nonce = server_nonce
        self.created = now
        self.status = "pending"
        self.last_seq = -1
        self.last_tick = -1
        self.last_progress = now
        self.counters = bytes(BUTTONS)
        self.window_start = now
        self.window_count = 0
        self.closed_at = 0.0
        self.close_reason = 0


class ReceiverCore:
    """
    The receiver's rules, with no socket. Feed it datagrams with :meth:`handle`
    and call :meth:`service` often (every few milliseconds): it runs the
    watchdog, destroys an idle pad and plays back replayed taps.

    Parameters
    ----------
    key : bytes
        The shared 32-byte key.
    sink : Sink
        The pad to drive.
    clock, rng, allow_source : callable
        Injected for tests: a monotonic clock in seconds, ``n -> n random
        bytes``, and ``host -> bool`` for the source-address rule.
    """

    def __init__(self, key: bytes, sink: Sink, clock: Callable[[], float] = time.monotonic,
                 rng: Callable[[int], bytes] = secrets.token_bytes,
                 allow_source: Callable[[str], bool] = is_local_source) -> None:
        if len(key) != KEY_LEN:
            raise ValueError("key must be 32 bytes")
        self.key = key
        self.sink = sink
        self._clock = clock
        self._rng = rng
        self._allow = allow_source
        self.sessions: Dict[bytes, _Session] = {}
        self.live: Optional[_Session] = None
        self.pad_open = False
        self.idle_since: Optional[float] = None
        self.live_state = NEUTRAL
        self._segments: List[Deque[Tuple[bool, float, float]]] = [deque() for _ in range(BUTTONS)]
        self._applied: Optional[PadState] = None
        self._reply_window = (0.0, 0)
        self.stats: Dict[str, int] = {}
        self.events: Deque[Dict[str, object]] = deque(maxlen=200)

    # ---- bookkeeping ----
    def _bump(self, name: str, n: int = 1) -> None:
        self.stats[name] = self.stats.get(name, 0) + n

    def _event(self, kind: str, **detail: object) -> None:
        ev: Dict[str, object] = {"t": round(self._clock(), 4), "kind": kind}
        ev.update(detail)
        self.events.append(ev)

    def _reply(self, now: float, packet: bytes) -> List[bytes]:
        start, count = self._reply_window
        if now - start >= 1.0:
            start, count = now, 0
        if count >= REPLY_CAP_PER_S:
            self._bump("replies_capped")
            self._reply_window = (start, count)
            return []
        self._reply_window = (start, count + 1)
        return [packet]

    def _sink_call(self, what: str, *args: object) -> bool:
        try:
            getattr(self.sink, what)(*args)
            return True
        except Exception as exc:  # a pad driver failing must not take the watchdog with it
            self._bump("sink_errors")
            self._event("sink_error", call=what, error=str(exc)[:200])
            return False

    # ---- the pad ----
    def effective_state(self, now: Optional[float] = None) -> PadState:
        """The live state with any replayed taps still playing laid over it."""
        now = self._clock() if now is None else now
        buttons = self.live_state.buttons
        for i, segs in enumerate(self._segments):
            while segs and segs[0][2] <= now:
                segs.popleft()
            if segs:
                bit = 1 << i
                buttons = (buttons | bit) if segs[0][0] else (buttons & ~bit)
        return replace(self.live_state, buttons=buttons)

    def _apply(self, force: bool = False) -> None:
        if not self.pad_open:
            return
        state = self.effective_state()
        if force or state != self._applied:
            # After a failed write the pad's real state is unknown, so the next
            # write must happen even if it equals the last good one. Otherwise
            # a neutral that matches an old success is skipped and the failed
            # held state is what the pad keeps.
            self._applied = state if self._sink_call("apply", state) else None

    def _neutralize(self) -> None:
        self.live_state = NEUTRAL
        for segs in self._segments:
            segs.clear()
        self._apply()

    def _end(self, session: _Session, reason: int, now: float) -> None:
        session.status = "closed"
        session.closed_at = now
        session.close_reason = reason
        if self.live is session:
            self.live = None
            self._neutralize()
            self.idle_since = now
        self._event("session_end", reason=REASON_NAMES.get(reason, reason), sid=session.sid.hex())

    def _expire(self, now: float) -> None:
        live = self.live
        if live is not None and now - live.last_progress > WATCHDOG_S:
            self._bump("timeouts")
            self._end(live, REASON_TIMEOUT, now)
        for sid in list(self.sessions):
            s = self.sessions[sid]
            if s.status == "pending" and now - s.created > PENDING_TTL_S:
                del self.sessions[sid]
            elif s.status == "closed" and now - s.closed_at > CLOSED_TTL_S:
                del self.sessions[sid]

    def service(self) -> None:
        """Watchdog, idle destroy and tap playback. Call every few milliseconds."""
        now = self._clock()
        self._expire(now)
        if self.live is None and self.pad_open and self.idle_since is not None \
                and now - self.idle_since >= DESTROY_S:
            self._sink_call("close")
            self.pad_open = False
            self._applied = None
            self._event("pad_destroyed")
        self._apply()

    # ---- datagrams ----
    def handle(self, data: bytes, addr: Addr) -> List[bytes]:
        """Process one datagram from ``addr``; returns the replies to send back to it."""
        now = self._clock()
        self._bump("rx")
        if not self._allow(addr[0]):
            self._bump("not_local")
            return []
        parts = split(data)
        if parts is None:
            self._bump("malformed")
            return []
        ptype, body, tag, signed = parts
        if ptype == T_HELLO:
            return self._on_hello(body, tag, signed, addr, now)
        if ptype == T_STATE:
            return self._on_state(body, tag, signed, addr, now)
        self._bump("unexpected_type")
        return []

    def _on_hello(self, body: bytes, tag: bytes, signed: bytes, addr: Addr, now: float) -> List[bytes]:
        if not control_tag_ok(self.key, signed, tag):
            self._bump("bad_tag")
            return []
        (client_nonce,) = _HELLO.unpack(body)
        self._expire(now)
        if self.live is not None:
            self._bump("refused")
            return self._reply(now, encode_refuse(self.key, client_nonce, bytes(SESSION_ID_LEN), REASON_BUSY))
        for s in self.sessions.values():   # a retried HELLO gets the same answer
            if s.status == "pending" and s.client_nonce == client_nonce and s.addr == addr:
                return self._reply(now, encode_welcome(self.key, client_nonce, s.server_nonce, s.sid))
        pending = sorted((s for s in self.sessions.values() if s.status == "pending"), key=lambda s: s.created)
        while len(pending) >= MAX_PENDING:
            del self.sessions[pending.pop(0).sid]
        sid = self._rng(SESSION_ID_LEN)
        server_nonce = self._rng(NONCE_LEN)
        s = _Session(sid, session_key(self.key, client_nonce, server_nonce, sid), addr, client_nonce,
                     server_nonce, now)
        self.sessions[sid] = s
        self._bump("hellos")
        return self._reply(now, encode_welcome(self.key, client_nonce, server_nonce, sid))

    def _on_state(self, body: bytes, tag: bytes, signed: bytes, addr: Addr, now: float) -> List[bytes]:
        sid = body[:SESSION_ID_LEN]
        s = self.sessions.get(sid)
        if s is None:
            self._bump("unknown_session")
            return self._reply(now, encode_expired(self.key, sid, REASON_UNKNOWN))
        if addr != s.addr:
            self._bump("wrong_addr")
            return []
        if not state_tag_ok(s.key, signed, tag):
            self._bump("bad_tag")
            return []
        (_sid, seq, tick, lx, ly, rx, ry, lt, rt, buttons, flags, counters) = _STATE.unpack(body)
        # Timeout first: a delayed packet must not refresh a session whose 150 ms already ran out.
        self._expire(now)
        if s.status == "closed":
            self._bump("after_close")
            return self._reply(now, encode_expired(self.key, sid, s.close_reason))
        if seq <= s.last_seq:
            self._bump("stale_seq")
            return []
        if now - s.window_start >= 1.0:
            s.window_start, s.window_count = now, 0
        if s.window_count >= RATE_CAP_PER_S:
            self._bump("rate_dropped")
            return []
        s.window_count += 1
        s.last_seq = seq
        state = PadState(lx, ly, rx, ry, lt, rt, buttons & 0x3FFF)

        if s.status == "pending":
            if self.live is not None:
                self._bump("refused")
                s.status, s.closed_at, s.close_reason = "closed", now, REASON_BUSY
                return self._reply(now, encode_refuse(self.key, s.client_nonce, sid, REASON_BUSY))
            if not state.is_neutral or flags & FLAG_END:
                # Also what a lost or late neutral opener looks like. Close it and
                # say so, or a sender that believes it is live would never learn.
                self._bump("not_neutral_first")
                s.status, s.closed_at, s.close_reason = "closed", now, REASON_NOT_NEUTRAL
                return self._reply(now, encode_expired(self.key, sid, REASON_NOT_NEUTRAL))
            s.status = "live"
            s.counters = counters
            s.last_tick = tick
            self.live = s
            self.idle_since = None
            self.live_state = NEUTRAL
            for segs in self._segments:
                segs.clear()
            if not self.pad_open:
                if not self._sink_call("open"):
                    self._end(s, REASON_ENDED, now)
                    return []
                self.pad_open = True
                self._event("pad_created")
            s.last_progress = self._clock()   # plugging a pad can take a while; do not count it against the watchdog
            self._bump("sessions")
            self._event("session_live", sid=sid.hex(), addr=str(addr[0]))
            self._apply(force=True)
            return []

        if tick > s.last_tick:
            s.last_tick = tick
            s.last_progress = now
        else:
            self._bump("stalled_tick")
        if flags & FLAG_END:
            self._bump("ends")
            self._end(s, REASON_ENDED, now)
            return []
        self._take(s, state, counters, now)
        self._apply()
        return []

    def _take(self, s: _Session, state: PadState, counters: bytes, now: float) -> None:
        was_buttons = self.live_state.buttons
        for i in range(BUTTONS):
            delta = (counters[i] - s.counters[i]) & 0xFF
            if delta == 0:
                continue
            bit = 1 << i
            pressed_now = bool(state.buttons & bit)
            was = bool(was_buttons & bit)
            # Presses the arriving state accounts for: the one it holds, if it holds one.
            missed = delta - (1 if pressed_now else 0)
            plan: List[Tuple[bool, float]] = []
            if was and (missed > 0 or pressed_now):
                plan.append((False, TAP_GAP_S))     # the release that was lost
            for _ in range(max(0, missed)):
                plan.extend([(True, TAP_HOLD_S), (False, TAP_GAP_S)])
            if plan:
                segs = self._segments[i]
                t = segs[-1][2] if segs else now
                for pressed, duration in plan:
                    segs.append((pressed, t, t + duration))
                    t += duration
            if missed > 0:
                self._bump("replayed_taps", missed)
        s.counters = counters
        self.live_state = state

    def snapshot(self) -> Dict[str, object]:
        now = self._clock()
        live = self.live
        return {
            "pad_open": self.pad_open,
            "live": live is not None,
            "live_age_s": round(now - live.created, 3) if live else None,
            "since_progress_ms": round((now - live.last_progress) * 1000, 1) if live else None,
            "state": self.effective_state(now).__dict__ if self.pad_open else None,
            "stats": dict(self.stats),
            "events": list(self.events),
            "contract": {"keepalive_hz": KEEPALIVE_HZ, "watchdog_s": WATCHDOG_S, "destroy_s": DESTROY_S,
                         "rate_cap_per_s": RATE_CAP_PER_S, "tap_hold_s": TAP_HOLD_S},
        }


# ---- sender ---------------------------------------------------------------------
def _no_udp_connreset(sock: socket.socket) -> None:
    """Windows reports an earlier ICMP port-unreachable as a recv error; turn that off."""
    ctl = getattr(socket, "SIO_UDP_CONNRESET", None)
    if ctl is not None:
        try:
            sock.ioctl(ctl, False)
        except (OSError, ValueError):
            pass


class NetpadSender:
    """
    The Nimbus side: holds the commanded controls and speaks the protocol.

    It never starts a thread. Call :meth:`tick` from the loop that updates the
    controls (at least every 50 ms; 60 Hz or faster is normal): that is the
    only place the loop tick advances, handshakes progress and keepalives go
    out, so the receiver's watchdog measures the loop, not a socket. Control
    setters send at once while a session is live.

    Parameters
    ----------
    key : bytes
        The shared key.
    addr : (host, port)
        The receiver.
    clock, rng, sock
        Injected for tests.
    """

    def __init__(self, key: bytes, addr: Addr, clock: Callable[[], float] = time.monotonic,
                 rng: Callable[[int], bytes] = secrets.token_bytes, sock: Optional[socket.socket] = None) -> None:
        self.key = key
        self.addr = addr
        self._clock = clock
        self._rng = rng
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _no_udp_connreset(sock)
        sock.setblocking(False)
        self.sock = sock
        self.status = "idle"          # idle, handshake, live, refused
        self.state = NEUTRAL
        self.counters = bytearray(BUTTONS)
        self.loop_tick = 0
        self._seq = 0
        self._sid: Optional[bytes] = None
        self._ks: Optional[bytes] = None
        self._nonce: Optional[bytes] = None
        self._last_hello = float("-inf")
        self._last_send = float("-inf")
        self.stats: Dict[str, int] = {}
        #: When a list, every datagram sent is appended (tests replay them).
        self.capture: Optional[List[bytes]] = None

    # ---- controls ----
    def set_left_stick(self, x: float, y: float) -> None:
        self._set(replace(self.state, lx=stick_to_i16(x), ly=stick_to_i16(y)))

    def set_right_stick(self, x: float, y: float) -> None:
        self._set(replace(self.state, rx=stick_to_i16(x), ry=stick_to_i16(y)))

    def set_left_trigger(self, value: float) -> None:
        self._set(replace(self.state, lt=trigger_to_u8(value)))

    def set_right_trigger(self, value: float) -> None:
        self._set(replace(self.state, rt=trigger_to_u8(value)))

    def set_button(self, button_id: int, pressed: bool) -> bool:
        i = int(button_id) - 1
        if not 0 <= i < BUTTONS:
            return False
        bit = 1 << i
        was = bool(self.state.buttons & bit)
        if pressed and not was:
            self.counters[i] = (self.counters[i] + 1) & 0xFF
        buttons = (self.state.buttons | bit) if pressed else (self.state.buttons & ~bit)
        self._set(replace(self.state, buttons=buttons))
        return True

    def _set(self, state: PadState) -> None:
        if state == self.state:
            return
        self.state = state
        if self.status == "live":
            self.send_state_now()

    # ---- lifecycle ----
    def start(self) -> None:
        if self.status == "idle":
            self.status = "handshake"
            self._nonce = self._rng(NONCE_LEN)
            self._hello(self._clock())

    def stop(self) -> None:
        """Explicit Stop: neutral controls and END, at once. :meth:`start` opens a new session."""
        self.state = NEUTRAL
        if self.status == "live" and self._ks is not None and self._sid is not None:
            self._seq += 1
            self._send(encode_state(self._ks, self._sid, self._seq, self.loop_tick, NEUTRAL,
                                    bytes(self.counters), FLAG_END))
        self.status = "idle"
        self._sid = self._ks = self._nonce = None

    def close(self) -> None:
        self.stop()
        self.sock.close()

    def tick(self) -> None:
        """One iteration of the app loop: replies, handshake retries, keepalive."""
        now = self._clock()
        self.loop_tick += 1
        self._poll(now)
        if self.status == "handshake" and now - self._last_hello >= HELLO_RETRY_S:
            self._hello(now)
        elif self.status == "refused" and now - self._last_hello >= REFUSED_RETRY_S:
            self._nonce = self._rng(NONCE_LEN)   # a late WELCOME for the refused attempt must not match
            self._hello(now)
        elif self.status == "live" and now - self._last_send >= 1.0 / KEEPALIVE_HZ:
            self.send_state_now()

    def send_state_now(self) -> None:
        """Send the current state with the current loop tick (tests call this to imitate a stuck loop)."""
        if self.status != "live" or self._ks is None or self._sid is None:
            return
        self._seq += 1
        self._send(encode_state(self._ks, self._sid, self._seq, self.loop_tick, self.state, bytes(self.counters)))

    # ---- wire ----
    def _bump(self, name: str) -> None:
        self.stats[name] = self.stats.get(name, 0) + 1

    def _send(self, packet: bytes) -> None:
        if self.capture is not None:
            self.capture.append(packet)
        try:
            self.sock.sendto(packet, self.addr)
            self._last_send = self._clock()
        except OSError:
            self._bump("send_errors")

    def _hello(self, now: float) -> None:
        if self._nonce is None:
            self._nonce = self._rng(NONCE_LEN)
        self._last_hello = now
        self._bump("hellos")
        self._send(encode_hello(self.key, self._nonce))

    def _poll(self, now: float) -> None:
        for _ in range(64):
            try:
                data, _addr = self.sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            parts = split(data)
            if parts is None:
                continue
            ptype, body, tag, signed = parts
            if not control_tag_ok(self.key, signed, tag):
                self._bump("bad_reply_tag")
                continue
            if ptype == T_WELCOME:
                client_nonce, server_nonce, sid = _WELCOME.unpack(body)
                if self.status in ("handshake", "refused") and client_nonce == self._nonce:
                    self._sid = sid
                    self._ks = session_key(self.key, client_nonce, server_nonce, sid)
                    self._seq = 0
                    self.status = "live"
                    self._bump("sessions")
                    held = self.state
                    self.state = NEUTRAL
                    self.send_state_now()            # the receiver accepts nothing else first;
                    self.send_state_now()            # twice, so one lost opener does not cost a handshake
                    self.state = held
                    if held != NEUTRAL:
                        self.send_state_now()
            elif ptype == T_REFUSE:
                client_nonce, sid, _reason = _REFUSE.unpack(body)
                if client_nonce == self._nonce and self.status in ("handshake", "live"):
                    if self.status == "live" and sid != self._sid:
                        continue
                    self.status = "refused"
                    self._sid = self._ks = None
                    self._bump("refused")
            elif ptype == T_EXPIRED:
                sid, _reason = _EXPIRED.unpack(body)
                if self.status == "live" and sid == self._sid:
                    self._bump("expired")
                    self.status = "handshake"
                    self._sid = self._ks = None
                    self._nonce = self._rng(NONCE_LEN)
                    self._hello(now)
