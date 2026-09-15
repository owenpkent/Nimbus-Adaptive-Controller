"""
Checks on ``src/spectator/policy.py``, the rule that decides where
target-aware assistance may run. Pure Python, no hardware, no Windows
calls: the live helpers are exercised only for their off-Windows fallback
and, on Windows, for not raising.

What is checked: the shipped allowlist loads and is what the plan says it
is (two shipped single-player titles, two test-only harness titles, no
shipped entry with a competitive mode); an unknown game is refused with a
reason that says so; an anti-cheat process refuses everything regardless
of the allowlist; a test-only title is refused unless the caller opts in;
a competitive title is refused without a recorded decision and allowed
with one; matching is case-insensitive on the process and a substring on
the title; a malformed entry raises rather than loading.

Run from the repo root (the runner does this)::

    venv\\Scripts\\python -m tests.test_assist_policy
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

from src.spectator.policy import (
    ANTICHEAT_PROCESSES, GAMES_DIR, AssistPolicy, GameEntry, Verdict, foreground_window, load_entries,
    running_anticheat, running_process_names,
)

PASSES = 0
FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSES, FAILS
    if ok:
        PASSES += 1
    else:
        FAILS += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


IDLE = ["explorer.exe", "steam.exe", "svchost.exe"]

print("The shipped allowlist")
entries = load_entries()
names = sorted(e.name for e in entries)
check("loads from src/spectator/games", len(entries) >= 4, str(names))
shipped = [e for e in entries if not e.test_only]
test_only = [e for e in entries if e.test_only]
check("shipped entries are Half-Life 2 and PowerWash Simulator", sorted(e.name for e in shipped) == ["halflife2", "powerwashsimulator"])
check("test-only entries are Arma 3 and Left 4 Dead 2", sorted(e.name for e in test_only) == ["arma3", "left4dead2"])
check("no shipped entry has a competitive mode", all(not e.competitive for e in shipped))
check("every entry records who decided and why", all(e.decided and e.decided.get("by") and e.decided.get("why") for e in entries))
check("every entry names a target source the plan knows", all(e.target_source in ("native", "script", "color", "onnx") for e in entries))
check("the Arma 3 entry is the script oracle", next(e for e in entries if e.name == "arma3").target_source == "script")

print("The rule")
policy = AssistPolicy(entries)
v = policy.check("ELDEN RING", r"C:\Games\ELDEN RING\Game\eldenring.exe", IDLE)
check("an unknown title is refused", not v.allowed and v.entry is None, v.reason)
check("the refusal names the allowlist", "allowlist" in v.reason.lower())
v = policy.check("Half-Life 2", r"C:\Steam\steamapps\common\Half-Life 2\hl2.exe", IDLE)
check("Half-Life 2 is allowed", v.allowed and v.entry is not None and v.entry.name == "halflife2", v.reason)
check("the verdict names the source", "color" in v.reason)
check("a Verdict is truthy when allowed", bool(v) is True and bool(Verdict(False, "no")) is False)
v = policy.check("half-life 2 - some map", r"C:\x\HL2.EXE", IDLE)
check("process match is case-insensitive and title match is a substring", v.allowed, v.reason)
v = policy.check("Half-Life 2", r"C:\x\hl2.exe", IDLE + ["BEService.exe"])
check("a running anti-cheat refuses even an allowed title", not v.allowed and "BattlEye" in v.reason, v.reason)
v = policy.check("PowerWash Simulator", r"C:\x\PowerWashSimulator.exe", IDLE + [r"C:\Program Files\EasyAntiCheat_EOS\EasyAntiCheat_EOS.exe"])
check("an anti-cheat given as a full path still refuses", not v.allowed and "Easy Anti-Cheat" in v.reason, v.reason)
v = policy.check("Arma 3", r"C:\x\arma3_x64.exe", IDLE)
check("a test-only title is refused to the app", not v.allowed and "test title" in v.reason, v.reason)
harness = AssistPolicy(entries, allow_test_entries=True)
v = harness.check("Arma 3", r"C:\x\arma3_x64.exe", IDLE)
check("the harness may use a test-only title", v.allowed and v.entry.target_source == "script", v.reason)
v = harness.check("Arma 3", r"C:\x\arma3_x64.exe", IDLE + ["BEService_x64.exe"])
check("BattlEye running refuses Arma 3 even to the harness", not v.allowed, v.reason)
v = policy.check("", "", IDLE)
check("no window at all is refused with a reason", not v.allowed and v.reason)

print("Competitive titles")
undecided = GameEntry(name="versus", title="Versus Arena", process="versus.exe", modes=("single", "competitive"), target_source="color")
decided = GameEntry(name="versus2", title="Versus Arena 2", process="versus2.exe", modes=("single", "competitive"),
                    target_source="color", competitive_decision={"allow": True, "on": "2026-09-13", "why": "test"})
refused = GameEntry(name="versus3", title="Versus Arena 3", process="versus3.exe", modes=("single", "competitive"),
                    target_source="color", competitive_decision={"allow": False, "on": "2026-09-13", "why": "test"})
p = AssistPolicy([undecided, decided, refused])
v = p.check("Versus Arena", "versus.exe", IDLE)
check("a competitive title with no decision is refused", not v.allowed and "competitive" in v.reason, v.reason)
check("a competitive title with an allowing decision runs", p.check("Versus Arena 2", "versus2.exe", IDLE).allowed)
check("a competitive title with a refusing decision is refused", not p.check("Versus Arena 3", "versus3.exe", IDLE).allowed)

print("Entry validation")
with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "bad.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "bad", "title": "Bad", "process": "bad", "modes": ["single"], "target_source": "color"}, fh)
    try:
        load_entries(tmp)
        check("a process that is not an image name raises", False)
    except ValueError as exc:
        check("a process that is not an image name raises", ".exe" in str(exc), str(exc))
    with open(os.path.join(tmp, "bad.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "bad", "title": "Bad", "process": "bad.exe", "modes": ["ranked"], "target_source": "color"}, fh)
    try:
        load_entries(tmp)
        check("an unknown mode raises", False)
    except ValueError as exc:
        check("an unknown mode raises", "modes" in str(exc), str(exc))
    with open(os.path.join(tmp, "bad.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "bad", "title": "Bad", "process": "bad.exe", "modes": ["single"]}, fh)
    try:
        load_entries(tmp)
        check("a missing key raises", False)
    except ValueError as exc:
        check("a missing key raises", "target_source" in str(exc), str(exc))
    for n in ("a", "b"):
        with open(os.path.join(tmp, f"{n}.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": "same", "title": "Same", "process": "same.exe", "modes": ["single"], "target_source": "color"}, fh)
    os.remove(os.path.join(tmp, "bad.json"))
    try:
        load_entries(tmp)
        check("duplicate names raise", False)
    except ValueError as exc:
        check("duplicate names raise", "duplicate" in str(exc), str(exc))
check("an empty directory loads nothing", load_entries(os.path.join(GAMES_DIR, "does-not-exist")) == [])

print("Anti-cheat table and the live helpers")
check("the table names EAC, BattlEye, Vanguard and FACEIT", {"Easy Anti-Cheat", "BattlEye", "Riot Vanguard", "FACEIT Anti-Cheat"} <= set(ANTICHEAT_PROCESSES.values()))
check("running_anticheat finds by basename, case-insensitive", running_anticheat([r"C:\x\VGC.EXE"]) == "Riot Vanguard" and running_anticheat(IDLE) is None)
procs = running_process_names()
if sys.platform == "win32":
    check("running_process_names lists this interpreter's image", any(os.path.basename(p).lower().startswith("python") for p in procs), f"{len(procs)} processes")
    title, image = foreground_window()
    check("foreground_window returns strings", isinstance(title, str) and isinstance(image, str))
else:
    check("running_process_names is empty off Windows", procs == [])
    check("foreground_window is empty off Windows", foreground_window() == ("", ""))
    check("check_foreground refuses off Windows", not AssistPolicy(entries).check_foreground().allowed)

print(f"\n{PASSES} passed, {FAILS} failed")
sys.exit(1 if FAILS else 0)
