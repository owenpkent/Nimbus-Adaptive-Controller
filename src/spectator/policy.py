"""
Target-aware assist policy: where target-aware assistance may run at all.

The rule (``docs/vision/TARGET_AWARE_AIM_PLAN.md``, section 5): a positive
allowlist by title, each entry declaring its modes and its target source;
a refusal whenever an anti-cheat service is running; a refusal for any
title with a competitive mode until a per-title decision is recorded in
its entry; and a refusal, with the reason on screen, for anything unknown.
Nothing target-aware runs without an ``allowed`` verdict from here.

This is a posture, not an enforcement mechanism: the allowlist is a JSON
file and the anti-cheat scan is a process list. Its value is that the
project can say in one sentence where the feature runs, and that the
sentence is checked by code and by ``tests/test_assist_policy.py``.

The pure part (``AssistPolicy.check``) takes the foreground window's title,
its process image and the names of the running processes, so it is testable
with fake lists anywhere. The Windows part (``check_foreground``) reads
those three from the live session and works only there.

Entries live in ``src/spectator/games/*.json``::

    {
      "name": "halflife2",
      "title": "Half-Life 2",              window title substring, case-insensitive
      "process": "hl2.exe",                process image, case-insensitive
      "modes": ["single"],                 single | coop | competitive
      "anticheat": null,                   informational; the refusal is by running services
      "target_source": "color",            native | script | color | onnx
      "target_params": {},                 for that source
      "calibration": "halflife2",          src/spectator/calibrations/<name>.json, or null
      "test_only": false,                  harness use only; refused unless the caller opts in
      "competitive_decision": null,        {"allow": bool, "on": date, "why": text} for a title with a competitive mode
      "decided": {"by": ..., "on": ..., "why": ...},
      "notes": "..."
    }
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

GAMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "games")

MODES = ("single", "coop", "competitive")
TARGET_SOURCES = ("native", "script", "color", "onnx")

# Process images that mean an anti-cheat is running in this session. Any of
# these refuses target-aware assistance regardless of the allowlist: the
# anti-cheat's presence is the fact, whatever title started it.
ANTICHEAT_PROCESSES: Dict[str, str] = {
    "easyanticheat_eos.exe": "Easy Anti-Cheat",
    "easyanticheat.exe": "Easy Anti-Cheat",
    "beservice.exe": "BattlEye",
    "beservice_x64.exe": "BattlEye",
    "vgc.exe": "Riot Vanguard",
    "vgtray.exe": "Riot Vanguard",
    "faceitservice.exe": "FACEIT Anti-Cheat",
    "faceitclient.exe": "FACEIT Anti-Cheat",
}

REQUIRED_KEYS = ("name", "title", "process", "modes", "target_source")


@dataclass(frozen=True)
class GameEntry:
    """One allowlisted title."""

    name: str
    title: str
    process: str
    modes: Tuple[str, ...]
    target_source: str
    anticheat: Optional[str] = None
    target_params: Dict[str, Any] = field(default_factory=dict)
    calibration: Optional[str] = None
    test_only: bool = False
    competitive_decision: Optional[Dict[str, Any]] = None
    decided: Optional[Dict[str, Any]] = None
    notes: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source: str = "<dict>") -> "GameEntry":
        missing = [k for k in REQUIRED_KEYS if k not in data]
        if missing:
            raise ValueError(f"{source}: missing {', '.join(missing)}")
        modes = tuple(str(m).lower() for m in data["modes"])
        bad = [m for m in modes if m not in MODES]
        if bad or not modes:
            raise ValueError(f"{source}: modes must be one or more of {MODES}, got {list(modes)}")
        source_kind = str(data["target_source"]).lower()
        if source_kind not in TARGET_SOURCES:
            raise ValueError(f"{source}: target_source must be one of {TARGET_SOURCES}, got {source_kind!r}")
        process = str(data["process"]).strip()
        if not process.lower().endswith(".exe"):
            raise ValueError(f"{source}: process must be an image name ending in .exe, got {process!r}")
        if not str(data["title"]).strip():
            raise ValueError(f"{source}: title must not be empty")
        return cls(
            name=str(data["name"]),
            title=str(data["title"]),
            process=process,
            modes=modes,
            target_source=source_kind,
            anticheat=data.get("anticheat") or None,
            target_params=dict(data.get("target_params") or {}),
            calibration=data.get("calibration") or None,
            test_only=bool(data.get("test_only", False)),
            competitive_decision=data.get("competitive_decision") or None,
            decided=data.get("decided") or None,
            notes=str(data.get("notes", "")),
        )

    @property
    def competitive(self) -> bool:
        return "competitive" in self.modes

    @property
    def competitive_allowed(self) -> bool:
        """A title with a competitive mode runs only on a recorded per-title decision."""
        if not self.competitive:
            return True
        return bool(self.competitive_decision and self.competitive_decision.get("allow") is True)

    def matches(self, window_title: str, process_image: str) -> bool:
        image = os.path.basename(process_image or "").lower()
        return image == self.process.lower() and self.title.lower() in (window_title or "").lower()


@dataclass(frozen=True)
class Verdict:
    """Whether target-aware assistance may run right now, and why in one sentence."""

    allowed: bool
    reason: str
    entry: Optional[GameEntry] = None

    def __bool__(self) -> bool:
        return self.allowed


def load_entries(directory: str = GAMES_DIR) -> List[GameEntry]:
    """Every ``*.json`` in the directory, validated; a bad file raises rather than being skipped."""
    entries: List[GameEntry] = []
    if not os.path.isdir(directory):
        return entries
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(directory, name)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        entries.append(GameEntry.from_dict(data, source=path))
    names = [e.name for e in entries]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"{directory}: duplicate entry names {dupes}")
    return entries


def running_anticheat(running: Iterable[str]) -> Optional[str]:
    """The name of the first anti-cheat whose process is in ``running``, or None."""
    for image in running:
        hit = ANTICHEAT_PROCESSES.get(os.path.basename(str(image)).lower())
        if hit:
            return hit
    return None


class AssistPolicy:
    """Decides, for a foreground game, whether target-aware assistance may run.

    Parameters
    ----------
    entries : list of GameEntry, optional
        The allowlist. Default: ``load_entries()`` from ``GAMES_DIR``.
    allow_test_entries : bool
        Let ``test_only`` entries pass. The game harness sets this; the
        app never does.
    """

    def __init__(self, entries: Optional[List[GameEntry]] = None, allow_test_entries: bool = False) -> None:
        self.entries: List[GameEntry] = list(entries) if entries is not None else load_entries()
        self.allow_test_entries = allow_test_entries

    def find(self, window_title: str, process_image: str) -> Optional[GameEntry]:
        for entry in self.entries:
            if entry.matches(window_title, process_image):
                return entry
        return None

    def check(self, window_title: str, process_image: str, running: Iterable[str] = ()) -> Verdict:
        """The rule, in order: an anti-cheat running, an unknown title, a test-only title, a competitive title."""
        anticheat = running_anticheat(running)
        if anticheat:
            return Verdict(False, f"{anticheat} is running; target assist never runs beside an anti-cheat.")
        entry = self.find(window_title, process_image)
        if entry is None:
            shown = (window_title or "").strip() or os.path.basename(process_image or "") or "this game"
            return Verdict(False, f"{shown} is not on the target assist allowlist; unknown games are refused.")
        if entry.test_only and not self.allow_test_entries:
            return Verdict(False, f"{entry.title} is a test title for the game harness, not a shipped entry.", entry)
        if not entry.competitive_allowed:
            return Verdict(False, f"{entry.title} has a competitive mode and no recorded decision allows it.", entry)
        return Verdict(True, f"{entry.title}: target assist allowed, source {entry.target_source}.", entry)

    def check_foreground(self) -> Verdict:
        """The live check on Windows: the foreground window, its process, the running processes."""
        if sys.platform != "win32":
            return Verdict(False, "Target assist runs on Windows only.")
        title, image = foreground_window()
        if not image:
            return Verdict(False, "No foreground game window.")
        return self.check(title, image, running_process_names())


# ---- Windows helpers ---------------------------------------------------------
def running_process_names() -> List[str]:
    """Image names of every running process (Toolhelp snapshot), or [] off Windows."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return []
    names: List[str] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.append(entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return names


def foreground_window() -> Tuple[str, str]:
    """(title, process image path) of the foreground window, or ('', '') off Windows or with none."""
    if sys.platform != "win32":
        return "", ""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, buf, 512)
    pid = wintypes.DWORD(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    image = ""
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if handle:
        try:
            path = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                image = path.value
        finally:
            k32.CloseHandle(handle)
    return buf.value, image
