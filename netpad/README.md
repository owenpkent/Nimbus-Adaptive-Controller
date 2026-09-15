# netpad: the network pad prototype

Research tooling for [Separate Game Machine](../docs/vision/SEPARATE_GAME_MACHINE.md), section 3.2: the game runs on one computer, Nimbus on another, and the pad crosses the local network. Not part of the app, `run.py` or the installer, and nothing in `src/` imports it. Bridge integration is phase 1 of that document and has not started.

## What is here

| File | What it is |
|---|---|
| `protocol.py` | The packets and every rule: handshake, HMAC tags, the watchdog, press counters, the rate cap, one live session. Pure Python with the clock, randomness and the pad injected. The module docstring is the contract. |
| `receiver.py` | The game machine's side: the socket loop, the pads (`vigem` on Windows through `src/padbus_client.py`, `uinput` on Linux through `src/uinput_interface.py`, `log` anywhere), a read-only `/status` page, `keygen`. |
| `e2e_check.py` | A real sender on this machine against a receiver on another, read back through XInput by the Gate C monitor. |
| `deploy-guest.ps1` | Puts the receiver in `NimbusGuest` (the `vm/` guest) and starts it. |
| `../tests/test_netpad.py` | The fast checks (in the fast suite, no driver). |

## The contract (decided 2026-09-14)

- Keepalive 60 Hz; neutral after 150 ms without a fresh authenticated packet; pad destroyed after 2 s with no live session; at most 500 packets a second per session.
- A shared 256-bit key file copied to the sender by hand; a nonce handshake per session; HMAC-SHA256 on every packet. PIN pairing waits for a reviewed key exchange.
- One live session: a second sender is refused. After a timeout the sender opens a new session whose first state must be neutral, with no extra action.

Each is one constant at the top of `protocol.py`. Changing one is a "when to ask" item under `CLAUDE.md`.

## Run it

Anywhere, no pad (two terminals, repo root):

```
venv\Scripts\python -m netpad.receiver keygen netpad.key
venv\Scripts\python -m netpad.receiver run --key netpad.key --sink log --status-port 47201
```

On the Linux box, a real pad (`/dev/uinput` access as in `docs/setup/LINUX.md`):

```
./venv/bin/python -m netpad.receiver run --key netpad.key --sink uinput
```

End to end against the guest (no elevation; the guest must exist, see `vm/README.md`):

```
powershell -File netpad\deploy-guest.ps1 -StartVm
venv\Scripts\python -m netpad.e2e_check --receiver <guest ip> --key C:\NimbusVM\netpad.key
powershell -File netpad\deploy-guest.ps1 -Stop
```

The end-to-end check synthesizes no mouse or keyboard input on either machine and flashes nothing, so it is safe with someone at the screen. It writes its results to `C:\NimbusVM\logs\netpad-e2e-*.json`.

## Things to know

- **The pad exists only while a session is live (plus 2 s).** A game that scans for pads only at launch (Source games) will not see it unless a session is live when the game starts, and measured on 2026-09-14 it will not see it come back either: after a 2.6 s freeze destroyed and re-created the pad, Left 4 Dead 2 never read the new one.
- **A real game measures no difference from the direct pad.** `tests\probe_game_harness_windows.py --game left4dead2 --actuator netpad` (22/22) matched `--actuator pad` (17/17) on deadzone, yaw rates, pitch, walk and button echo; see `docs/vision/GAME_TEST_HARNESS.md`, results log, 2026-09-14.
- **Loss past the watchdog is real.** At 30% packet loss a run of nine lost keepalives outlasts 150 ms now and then (one timeout in about ten seconds in the seeded test); the session resumes, but input in that gap is dropped by design.
- **A pad write that fails forces the next write**, so a watchdog neutral is never skipped because it matches an older successful write. The fast suite caught the version that skipped it.
- The embeddable Python in the guest ignores the script's own folder on `sys.path`; `receiver.py` adds it when run as a file, and falls back from `src.padbus_client` to a flat `padbus_client`.
