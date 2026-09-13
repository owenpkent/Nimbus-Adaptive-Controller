"""
Game test harness (throwaway tooling, not Nimbus code).

Launches a real game, owns the controller it reads, puts the game in a
known state, sends controller input, and reads what the game did from the
game itself. ``docs/vision/GAME_TEST_HARNESS.md`` is the plan and the
results log; ``tests/probe_game_harness_windows.py`` is the runner; the
recipes live in ``tests/games/``.

Pieces
------
Recipe
    ``tests/games/<name>.json``: the Steam launch line, the window to look
    for, the oracle that reads the game, and the reset pose.
Launcher
    Steam launch, window lookup by title substring and owning process, kill.
Oracles
    ``SourceConsoleOracle``: ground truth for Source engine games through
    ``-condebug`` (every console line appended to ``console.log``) and two
    bound keys the harness presses with ``SendInput``: one prints the
    player's origin and view angles, the other execs a reset cfg. A pad
    button rebound to ``echo <marker>`` gives a ground-truth button check.
    ``FrameDiffOracle``: no pose, a fixed warm-up; only the frame
    differencing every step records anyway.
    ``Arma3Oracle``: ground truth for Arma 3 through a generated mission
    whose script publishes the pose over the clipboard and takes reset
    and script commands back from it (section 4.3 of the plan).
Actuators
    ``PadActuator``: a ViGEm pad the harness owns, exact and fast, for
    calibrating a game. ``NimbusActuator``: the real QML app in-process,
    driven by synthesized pointer events on its widgets, so the whole bridge
    runs; it reports what the bridge sent beside what the game did.
GameEnv
    ``launch``, ``wait_ready``, ``reset``, ``step``, ``observe``, ``close``.
    The actuator is built before the game is launched, because Source
    decides at start-up whether an XInput controller exists.

Actions are dictionaries: ``lx``, ``ly``, ``rx``, ``ry`` in -1 to 1 (right
and up positive, as XInput has it), ``lt``, ``rt`` in 0 to 1, ``buttons`` a
list of the bridge's button ids (1 to 14 under ViGEm). The Nimbus actuator
also takes ``rx_px``, ``ry_px``, ``lx_px``, ``ly_px`` for a drag of exactly
that many pixels.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional, Tuple

if sys.platform != "win32":
    sys.exit("Windows only")

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
os.environ.setdefault("QT_SCALE_FACTOR", "1")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")
GAMES_DIR = os.path.join(TESTS, "games")
FRAMES_DIR = os.path.join(TESTS, "probe_frames")
sys.path.insert(0, REPO)
sys.path.insert(0, TESTS)

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QRectF, QTimer, QUrl, Qt, Signal, Slot  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

from frame_motion import Thresholds, describe, measure, verdict as motion_verdict  # noqa: E402
from probe_game_mouselook_windows import client_rect_on_screen, frame_diff, grab, save_frame  # noqa: E402
from probe_rawinput_windows import (  # noqa: E402
    INPUT, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP, _hwnd_int, bring_to_front, kernel32, user32,
)

try:
    # The app's own window management, so a recipe's ``window`` key exercises
    # it against a real game (it has no other test).
    from src.borderless import make_borderless, resize_window, restore_window
    BORDERLESS_AVAILABLE = True
except Exception:  # noqa: BLE001
    BORDERLESS_AVAILABLE = False

# ---- Win32 -----------------------------------------------------------------------
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SWP_NOSIZE, SWP_NOZORDER = 0x0001, 0x0004

VK = {f"F{i}": 0x6F + i for i in range(1, 13)}
VK["ESCAPE"] = 0x1B     # closes a menu a pad button opened (Arma 3)


def process_image(hwnd: int) -> str:
    """The image name (``left4dead2.exe``) of the process owning ``hwnd``."""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def find_game_window(title: str, process: Optional[str] = None) -> Optional[int]:
    """A visible window with ``title`` in its title, a client area over 200 px
    each way, and (when given) owned by ``process``; Steam's own "launching"
    dialog carries the game's name too, which is why the process matters."""
    needle = title.lower()
    want = (process or "").lower()
    found: List[int] = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if needle not in buf.value.lower():
            return True
        r = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        if r.right - r.left <= 200 or r.bottom - r.top <= 200:
            return True
        if want and process_image(hwnd).lower() != want:
            return True
        found.append(((r.right - r.left) * (r.bottom - r.top), _hwnd_int(hwnd)))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    # The largest, not the first: Arma 3 keeps a 506x250 start-up window
    # alive beside the game window for minutes, and enumeration order put
    # it first on every poll (2026-09-09).
    return max(found)[1] if found else None


def process_running(name: str) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                         capture_output=True, text=True).stdout
    return name.lower() in out.lower()


def kill_process(name: str, wait_s: float = 10.0) -> bool:
    subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not process_running(name):
            return True
        time.sleep(0.5)
    return not process_running(name)


def tap_key(name: str, hold_s: float = 0.03) -> None:
    """Press and release a function key with ``SendInput``, scan code included
    (Source maps keys by scan code, not virtual key)."""
    vk = VK[name.upper()]
    scan = user32.MapVirtualKeyW(vk, 0)
    down = INPUT(type=INPUT_KEYBOARD)
    down.ki = KEYBDINPUT(vk, scan, 0, 0, None)
    up = INPUT(type=INPUT_KEYBOARD)
    up.ki = KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP, 0, None)
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(hold_s)
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


# ---- pose math -------------------------------------------------------------------
def wrap_deg(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


def pose_delta(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Yaw and pitch (wrapped), horizontal and vertical travel from pose ``a`` to ``b``."""
    if not a or not b:
        return {}
    dx = b["pos"][0] - a["pos"][0]
    dy = b["pos"][1] - a["pos"][1]
    dz = b["pos"][2] - a["pos"][2]
    return {
        "d_yaw": wrap_deg(b["ang"][1] - a["ang"][1]),
        "d_pitch": wrap_deg(b["ang"][0] - a["ang"][0]),
        "d_horiz": math.hypot(dx, dy),
        "d_z": dz,
    }


def pose_error(a: Dict[str, Any], b: Dict[str, Any]) -> Tuple[float, float]:
    """Distance in units and the largest angle difference in degrees."""
    dist = math.sqrt(sum((b["pos"][i] - a["pos"][i]) ** 2 for i in range(3)))
    dang = max(abs(wrap_deg(b["ang"][i] - a["ang"][i])) for i in range(2))
    return dist, dang


# ---- recipes ---------------------------------------------------------------------
def recipe_path(name: str) -> str:
    if os.path.isfile(name):
        return os.path.abspath(name)
    return os.path.join(GAMES_DIR, f"{name}.json")


def load_recipe(name: str) -> Dict[str, Any]:
    path = recipe_path(name)
    with open(path, "r", encoding="utf-8") as fh:
        r = json.load(fh)
    r["_path"] = path
    return r


def write_reset_pose(recipe: Dict[str, Any], pose: Dict[str, Any]) -> None:
    """Rewrite the recipe file with ``reset_pose`` set (the first run records it)."""
    path = recipe["_path"]
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["reset_pose"] = {"pos": [round(v, 3) for v in pose["pos"]], "ang": [round(v, 3) for v in pose["ang"]]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)
        fh.write("\n")


def write_expect(recipe: Dict[str, Any], expect: Dict[str, Any]) -> None:
    """Merge ``expect`` into the recipe file's ``expect`` block, check by
    check (``--write-expect``): a pad run writes the G checks it measured, a
    Nimbus run the N checks, and neither touches the other's."""
    path = recipe["_path"]
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    block = dict(data.get("expect") or {})
    block.update(expect)
    data["expect"] = block
    recipe["expect"] = block
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)
        fh.write("\n")


# ---- the console log ------------------------------------------------------------
# ``getpos`` prints ``setpos x y z;setang p y r``; ``getpos_exact`` prints
# ``setpos_exact x y z;setang_exact p y r`` (measured on Left 4 Dead 2 build 10097).
# Half-Life 2 writes the two halves as separate console writes and prints a
# ``Map name: <map>`` line of its own between them, which lands in the middle of
# the pose line about one read in ten:
#     setpos 149.279999 4453.520020 -1342.167480;Map name: d1_eli_01
#     setang 0.000000 -90.000000 0.000000
# So anything without a ``;`` in it is allowed between the halves, which lets a
# newline and the interloping line through while still refusing to pair one
# read's position with a later read's angle (every read starts with a ``;`` of
# its own, and the span is capped). Matching the last ``setpos`` before a
# ``setang`` is what the non-greedy span gives, which is the right pairing.
POSE_RE = re.compile(
    r"setpos(?:_exact)?\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*;"
    r"[^;]{0,200}?"
    r"setang(?:_exact)?\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)")


# The pose key echoes this immediately before it asks for the pose, so a read
# can tell its own answer from one still arriving for the read before it. Half-
# Life 2 needs it: its console writes lag far enough behind the key press that a
# read which searches the log from the offset it captured before pressing will
# happily match the previous read's pose, and every measurement built on a pair
# of reads (degrees per second, units per second, latency) then pairs a fresh
# position with a stale one. That is what put 0.60 above 0.80 in the yaw sweep
# and read a walk as twice its length.
POSE_MARKER = "NIMBUS_POSE"
POSE_MARK_RE = re.compile(POSE_MARKER)


class ConsoleLog:
    """``console.log`` as written by ``-condebug``: appended, flushed per line."""

    def __init__(self, path: str) -> None:
        self.path = path

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def read_from(self, offset: int) -> str:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(offset)
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def wait_for(self, offset: int, pattern: "re.Pattern[str]", timeout: float) -> Optional["re.Match[str]"]:
        deadline = time.monotonic() + timeout
        while True:
            m = pattern.search(self.read_from(offset))
            if m:
                return m
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)


# ---- oracles ---------------------------------------------------------------------
# Source bind names for the pad buttons, and the bridge button id that reaches each
# (src/vigem_interface.py, set_button).
SOURCE_BUTTON_IDS = {
    "A_BUTTON": 1, "B_BUTTON": 2, "X_BUTTON": 3, "Y_BUTTON": 4,
    "L_SHOULDER": 5, "R_SHOULDER": 6, "BACK": 7, "START": 8,
    "STICK1": 9, "STICK2": 10, "UP": 11, "DOWN": 12, "LEFT": 13, "RIGHT": 14,
    # Source 2013 (Half-Life 2 and its episodes) has no named pad buttons: it
    # binds the joystick by index, in the order the input system reports XInput,
    # which is the same order as the names above. So JOY1 to JOY10 alias A to
    # STICK2. The d-pad is a hat there (POV_UP and friends), not buttons 11 to
    # 14, so it has no alias.
    "JOY1": 1, "JOY2": 2, "JOY3": 3, "JOY4": 4,
    "JOY5": 5, "JOY6": 6, "JOY7": 7, "JOY8": 8,
    "JOY9": 9, "JOY10": 10,
}


class Oracle:
    """What the harness can ask a game. The base answers nothing.

    ``has_pose`` says whether ``pose()`` returns ground truth (a position and
    view angles) or ``None``; the environment and the runner branch on it,
    never on the oracle's kind, so a new oracle with a pose gets the console
    games' checks for free.
    """

    kind = "none"
    has_pose = False
    resets_pitch = True       # a reset restores pitch too; False where only yaw can be set

    def __init__(self, recipe: Dict[str, Any]) -> None:
        self.recipe = recipe
        self.reset_pose: Optional[Dict[str, Any]] = recipe.get("reset_pose")

    def prepare_launch(self) -> None:
        pass

    def ready(self) -> bool:
        return True

    def pose(self, timeout: float = 1.5, tries: int = 2) -> Optional[Dict[str, Any]]:
        return None

    def set_reset_pose(self, pose: Dict[str, Any]) -> None:
        self.reset_pose = pose

    def reset(self) -> bool:
        return True

    def log_offset(self) -> int:
        return 0

    def wait_echo(self, marker: str, offset: int, timeout: float = 2.0) -> bool:
        return False

    def echo_buttons(self) -> List[Tuple[int, str]]:
        return []

    def cleanup(self) -> None:
        pass


class FrameDiffOracle(Oracle):
    kind = "frame_diff"

    def __init__(self, recipe: Dict[str, Any]) -> None:
        super().__init__(recipe)
        self.warmup = float(recipe.get("warmup_s", 75.0))
        self._t0 = 0.0

    def prepare_launch(self) -> None:
        self._t0 = time.time()

    def ready(self) -> bool:
        return time.time() - self._t0 >= self.warmup


class SourceConsoleOracle(Oracle):
    """Ground truth for a Source engine game through its console log."""

    kind = "source_console"
    has_pose = True

    def __init__(self, recipe: Dict[str, Any]) -> None:
        super().__init__(recipe)
        o = recipe["oracle"]
        self.mod_dir = os.path.join(recipe["game_dir"], o["mod_dir"])
        self.cfg_dir = os.path.join(self.mod_dir, "cfg")
        self.cfg_name = str(o.get("cfg_name", "nimbus_harness"))
        self.reset_cfg_name = self.cfg_name + "_reset"
        self.binds_cfg_name = self.cfg_name + "_binds"
        self.base_cfg = o.get("base_cfg")
        self.pose_command = str(o.get("pose_command", "getpos_exact"))
        self.setpos_command = "setpos_exact" if self.pose_command.endswith("_exact") else "setpos"
        self.pose_key = str(o.get("pose_key", "F7"))
        self.reset_key = str(o.get("reset_key", "F8"))
        self.button_echo: Dict[str, str] = dict(o.get("button_echo", {}))
        self.log = ConsoleLog(os.path.join(self.mod_dir, "console.log"))
        args = [str(a) for a in recipe.get("launch_args", [])]
        if "-condebug" not in args:
            raise ValueError("a source_console recipe needs -condebug in launch_args")
        execs = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "+exec"]
        if self.cfg_name not in execs:
            raise ValueError(f"a source_console recipe needs '+exec {self.cfg_name}' in launch_args")

    # cfg files
    def _cfg_path(self, name: str) -> str:
        return os.path.join(self.cfg_dir, f"{name}.cfg")

    def _write_cfg(self) -> None:
        """Write the launch cfg and, beside it, the binds it ends with.

        The binds are a separate file rather than more lines in the same one
        because ``exec`` does not run inline: on Half-Life 2 everything after
        ``exec 360controller`` finished *before* that file did (the harness's
        own loaded marker is printed nine lines ahead of the joystick output it
        triggers), so binds written after the exec were overwritten by the
        game's own controller cfg, which binds every pad button. As two execs
        queued in order, the base cfg is always finished with before the
        harness's binds are applied, whether the engine appends the exec'd file
        or inserts it. Left 4 Dead 2 behaves the other way round and is happy
        either way."""
        header = (f"// generated by tests/game_harness.py {time.strftime('%Y-%m-%d %H:%M:%S')}; "
                  "removed when the harness closes")
        binds = [header,
                 f'bind "{self.pose_key}" "echo {POSE_MARKER}; {self.pose_command}"',
                 f'bind "{self.reset_key}" "exec {self.reset_cfg_name}"']
        for button, marker in self.button_echo.items():
            binds.append(f'bind "{button}" "echo {marker}"')
        binds.append("echo NIMBUS_HARNESS_CFG_LOADED")
        with open(self._cfg_path(self.binds_cfg_name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(binds) + "\n")

        lines = [header, "sv_cheats 1"]
        if self.base_cfg:
            lines.append(f"exec {self.base_cfg}")
        lines.append(f"exec {self.binds_cfg_name}")
        with open(self._cfg_path(self.cfg_name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _write_reset_cfg(self) -> None:
        lines = ["sv_cheats 1"]
        if self.reset_pose:
            x, y, z = self.reset_pose["pos"]
            p, yaw, r = self.reset_pose["ang"]
            lines.append(f"{self.setpos_command} {x:.3f} {y:.3f} {z:.3f}")
            lines.append(f"setang {p:.3f} {yaw:.3f} {r:.3f}")
        # The echo binds are repeated here, not just in the harness cfg, because
        # a game may re-apply its own controller configuration after the harness
        # cfg has run and take the pad binds back with it. Half-Life 2 does:
        # "Using joystick 'Xbox360 controller' configuration" appears twice more
        # after the harness cfg loads, which rebinds JOY5 to +speed and loses the
        # marker. Every reset restores them, and a reset runs before the button
        # check. Rebinding is idempotent, so a game that never does this (Left 4
        # Dead 2) is unaffected.
        for button, marker in self.button_echo.items():
            lines.append(f'bind "{button}" "echo {marker}"')
        lines.append("echo NIMBUS_RESET_DONE")
        with open(self._cfg_path(self.reset_cfg_name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def prepare_launch(self) -> None:
        try:
            os.remove(self.log.path)
        except OSError:
            pass
        os.makedirs(self.cfg_dir, exist_ok=True)
        self._write_cfg()
        self._write_reset_cfg()

    def cleanup(self) -> None:
        for name in (self.cfg_name, self.reset_cfg_name, self.binds_cfg_name):
            try:
                os.remove(self._cfg_path(name))
            except OSError:
                pass

    # queries
    def log_offset(self) -> int:
        return self.log.size()

    def pose(self, timeout: float = 1.5, tries: int = 3) -> Optional[Dict[str, Any]]:
        """The player's position and view angles, or ``None``.

        The pose is taken from after this read's own marker, never from the
        first one in the log after the key press: on a game whose console
        writes lag the press, the latter is the previous read's answer.

        Three tries rather than two because a read that comes back empty does
        not fail a measurement, it fails whichever check was measuring, and
        that was seen twice in one afternoon of runs (reported as "no pose"
        and as a walk of 1e9). A press can be missed outright, most often when
        something else has taken the foreground, so a lost marker is worth
        pressing again for.
        """
        for _ in range(tries):
            off = self.log.size()
            tap_key(self.pose_key)
            mark = self.log.wait_for(off, POSE_MARK_RE, timeout)
            if mark is None:
                continue      # the press did not land; press again
            m = self.log.wait_for(off + mark.end(), POSE_RE, timeout)
            if m:
                return {"pos": [float(m.group(i)) for i in (1, 2, 3)],
                        "ang": [float(m.group(i)) for i in (4, 5, 6)]}
        return None

    def ready(self) -> bool:
        """The map is loaded and the player exists: the pose command answers
        with something other than the all-zero pose it prints while the map
        is still loading."""
        if not self.log.exists():
            return False
        p = self.pose(timeout=1.0, tries=1)
        return p is not None and any(abs(v) > 1e-6 for v in p["pos"] + p["ang"])

    def set_reset_pose(self, pose: Dict[str, Any]) -> None:
        self.reset_pose = pose
        self._write_reset_cfg()

    def reset(self) -> bool:
        if not self.reset_pose:
            return False
        off = self.log.size()
        tap_key(self.reset_key)
        ok = self.log.wait_for(off, re.compile("NIMBUS_RESET_DONE"), 2.0) is not None
        time.sleep(0.5)
        return ok

    def wait_echo(self, marker: str, offset: int, timeout: float = 2.0) -> bool:
        return self.log.wait_for(offset, re.compile(re.escape(marker)), timeout) is not None

    def echo_buttons(self) -> List[Tuple[int, str]]:
        return [(SOURCE_BUTTON_IDS[b], m) for b, m in self.button_echo.items() if b in SOURCE_BUTTON_IDS]


# ---- Arma 3: a mission that publishes the player's pose through the clipboard ----
ARMA_MISSION_SQM = '''version=53;
class EditorData
{
	moveGridStep=1;
	angleGridStep=0.2617994;
	scaleGridStep=1;
	autoGroupingDist=10;
	toggles=1;
	class ItemIDProvider
	{
		nextID=2;
	};
	class Camera
	{
		pos[]={%(x)s,20,%(y_back)s};
		dir[]={0,-0.7,0.7};
		up[]={0,0.7,0.7};
		aside[]={1,0,0};
	};
};
binarizationWanted=0;
sourceName="nimbus_harness";
addons[]=
{
	"A3_Characters_F"
};
randomSeed=1;
class ScenarioData
{
	author="Nimbus harness";
};
class Mission
{
	class Intel
	{
		timeOfChanges=1800.0002;
		startWeather=0;
		startWind=0;
		startWaves=0;
		forecastWeather=0;
		forecastWind=0;
		forecastWaves=0;
		forecastLightnings=0;
		year=2035;
		month=6;
		day=24;
		hour=12;
		minute=0;
		startFogDecay=0.014;
		forecastFogDecay=0.014;
	};
	class Entities
	{
		items=1;
		class Item0
		{
			dataType="Group";
			side="West";
			class Entities
			{
				items=1;
				class Item0
				{
					dataType="Object";
					class PositionInfo
					{
						position[]={%(x)s,0,%(y)s};
					};
					side="West";
					flags=7;
					class Attributes
					{
						isPlayer=1;
					};
					id=1;
					type="B_Soldier_F";
				};
			};
			class Attributes
			{
			};
			id=0;
		};
	};
};
'''

ARMA_INIT_SQF = '''// generated by tests/game_harness.py %(stamp)s; removed when the harness closes
enableSaving [false, false];
NIMBUS_TGT = objNull;
NIMBUS_TGT_GEN = 0;
// Place a target at a bearing and range from where the player is looking,
// standing still, harmless and unable to act. The closed-loop primitive
// (src/spectator, TARGET_AWARE_AIM_PLAN.md phase 1) is measured against it.
NIMBUS_SPAWN = {
	params ["_bearing", "_range", ["_type", "B_Soldier_F"]];
	private _eye = getCameraViewDirection player;
	private _dir = ((_eye select 0) atan2 (_eye select 1)) + _bearing;
	private _p = getPosATL player;
	private _pos = [(_p select 0) + _range * sin _dir, (_p select 1) + _range * cos _dir, 0];
	call NIMBUS_KILL;
	NIMBUS_TGT = createAgent [_type, _pos, [], 0, "CAN_COLLIDE"];
	NIMBUS_TGT setPosATL _pos;
	NIMBUS_TGT allowDamage false;
	NIMBUS_TGT disableAI "ALL";
	NIMBUS_TGT setDir (_dir + 180);
	_pos
};
// Walk it across the line of sight at a speed in metres a second. The
// generation counter stops the walker when the target is replaced or gone.
NIMBUS_WALK = {
	params ["_speed"];
	private _gen = NIMBUS_TGT_GEN;
	[_speed, _gen] spawn {
		params ["_speed", "_gen"];
		private _last = diag_tickTime;
		while { _gen == NIMBUS_TGT_GEN && !isNull NIMBUS_TGT } do {
			private _now = diag_tickTime;
			private _step = _speed * (_now - _last);
			_last = _now;
			private _d = (getDir NIMBUS_TGT) + 90;
			private _p = getPosATL NIMBUS_TGT;
			NIMBUS_TGT setPosATL [(_p select 0) + _step * sin _d, (_p select 1) + _step * cos _d, _p select 2];
			sleep 0.02;
		};
	};
};
NIMBUS_KILL = {
	NIMBUS_TGT_GEN = NIMBUS_TGT_GEN + 1;
	if (!isNull NIMBUS_TGT) then { deleteVehicle NIMBUS_TGT; };
	NIMBUS_TGT = objNull;
};
[] spawn {
	waitUntil { !isNull player && alive player && !isNull (findDisplay 46) };
	player allowDamage false;
	diag_log "NIMBUS_LOOP_START";
	private _actions = %(actions)s;
	private _execs = 0;
	while { true } do {
		private _cb = copyFromClipboard;
		if ((_cb select [0, 11]) == "NIMBUS_EXEC") then {
			copyToClipboard "NIMBUS_EXEC_TAKEN";
			_execs = _execs + 1;
			call compile (_cb select [12]);
		};
		if ((_cb select [0, 12]) == "NIMBUS_RESET") then {
			private _parts = (_cb select [13]) splitString " ";
			if (count _parts >= 4) then {
				player setPosASL [parseNumber (_parts select 0), parseNumber (_parts select 1), parseNumber (_parts select 2)];
				player setDir (parseNumber (_parts select 3));
				player setVelocity [0, 0, 0];
			};
		};
		private _p = getPosASL player;
		private _e = getCameraViewDirection player;
		private _yaw = (_e select 0) atan2 (_e select 1);
		if (_yaw < 0) then { _yaw = _yaw + 360; };
		private _pitch = asin (_e select 2);
		private _active = _actions select { (inputAction _x) > 0 };
		// The target, in the same units a screen detector would produce: where
		// its head is as a fraction of the window, how big it is, and, for the
		// harness to score the run with, the true bearing and range. Nothing in
		// the control path reads those last three.
		private _tgt = "none";
		if (!isNull NIMBUS_TGT) then {
			private _head = NIMBUS_TGT modelToWorld (NIMBUS_TGT selectionPosition "head");
			private _cam = positionCameraToWorld [0, 0, 0];
			private _rx = (_head select 0) - (_cam select 0);
			private _ry = (_head select 1) - (_cam select 1);
			private _rz = (_head select 2) - (_cam select 2);
			private _flat = sqrt (_rx * _rx + _ry * _ry);
			private _dist = sqrt (_rx * _rx + _ry * _ry + _rz * _rz);
			private _az = (_rx atan2 _ry) - _yaw;
			if (_az > 180) then { _az = _az - 360; };
			if (_az < -180) then { _az = _az + 360; };
			private _el = (if (_flat > 0.01) then { atan (_rz / _flat) } else { 0 }) - _pitch;
			private _s = worldToScreen _head;
			if (count _s == 2) then {
				private _sf = worldToScreen (NIMBUS_TGT modelToWorld [0, 0, 0]);
				private _h = if (count _sf == 2) then { abs ((_sf select 1) - (_s select 1)) } else { 0 };
				_tgt = format ["%%1,%%2,%%3,%%4,%%5,%%6,%%7", _s select 0, _s select 1, _h / 3, _h, _az, _el, _dist];
			} else {
				// Placed, but the engine will not project it: behind the camera or
				// past the edge of the view. The loop must treat that as no target
				// at all, and the harness needs to know where it went, so the line
				// says so rather than falling back to "none".
				_tgt = format ["off,%%1,%%2,%%3", _az, _el, _dist];
			};
		};
		private _line = format ["NIMBUS_POSE t=%%1 x=%%2 y=%%3 z=%%4 yaw=%%5 pitch=%%6 dir=%%7 buttons=%%8 execs=%%9 tgt=%%10", diag_tickTime, _p select 0, _p select 1, _p select 2, _yaw, _pitch, getDir player, _active joinString ",", _execs, _tgt];
		copyToClipboard _line;
		diag_log _line;
		sleep 0.02;
	};
};
'''

#: The user actions the mission reports as held, by their ``inputAction`` names;
#: which pad button drives which is the game's controller scheme's business and
#: is learnt with the pad, then written into the recipe's ``button_echo``.
ARMA_ACTIONS = ["Fire", "Jump", "GetOver", "Reload", "Action", "Zoom", "ZoomTemp", "SwitchWeapon",
                "NextWeapon", "PrevWeapon", "Throw", "Crouch", "Prone", "Stand", "Turbo", "Watch",
                "Optics", "Salute", "Sitdown", "Compass", "ShowMap", "Diary", "Gear", "Headlights",
                "TacticalView", "HandGunOn", "LookAround", "LeanLeft", "LeanRight", "EvasiveLeft",
                "EvasiveRight", "MoveFastForward", "MoveSlowForward", "Talk", "Chat", "DefaultAction",
                "WeaponModeSwitch"]

ARMA_POSE_RE = re.compile(r"NIMBUS_POSE t=([-\d.e+]+) x=([-\d.e+]+) y=([-\d.e+]+) z=([-\d.e+]+) "
                          r"yaw=([-\d.e+]+) pitch=([-\d.e+]+) dir=([-\d.e+]+) buttons=(\S*)(?: execs=(\d+))?"
                          r"(?: tgt=(\S+))?")

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Own handles with the types set, because a clipboard handle truncated to a
# 32-bit int (ctypes' default return type) is a crash on the next call.
_clip_user32 = ctypes.WinDLL("user32", use_last_error=True)
_clip_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_clip_user32.OpenClipboard.argtypes = [wintypes.HWND]
_clip_user32.OpenClipboard.restype = wintypes.BOOL
_clip_user32.CloseClipboard.restype = wintypes.BOOL
_clip_user32.EmptyClipboard.restype = wintypes.BOOL
_clip_user32.GetClipboardData.argtypes = [wintypes.UINT]
_clip_user32.GetClipboardData.restype = wintypes.HANDLE
_clip_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_clip_user32.SetClipboardData.restype = wintypes.HANDLE
_clip_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_clip_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_clip_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_clip_kernel32.GlobalLock.restype = ctypes.c_void_p
_clip_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_clip_kernel32.GlobalUnlock.restype = wintypes.BOOL
_clip_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_clip_kernel32.GlobalFree.restype = wintypes.HGLOBAL


def clipboard_read(tries: int = 20) -> Optional[str]:
    """The clipboard's text, ``""`` when it holds none, ``None`` when it could
    not be opened (another process had it for every try)."""
    for _ in range(tries):
        if _clip_user32.OpenClipboard(None):
            try:
                h = _clip_user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                p = _clip_kernel32.GlobalLock(h)
                try:
                    return ctypes.wstring_at(p) if p else ""
                finally:
                    _clip_kernel32.GlobalUnlock(h)
            finally:
                _clip_user32.CloseClipboard()
        time.sleep(0.002)
    return None


def clipboard_write(text: str, tries: int = 20) -> bool:
    """Put ``text`` on the clipboard; the system owns the memory afterwards."""
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(tries):
        if _clip_user32.OpenClipboard(None):
            try:
                _clip_user32.EmptyClipboard()
                h = _clip_kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not h:
                    return False
                p = _clip_kernel32.GlobalLock(h)
                ctypes.memmove(p, data, len(data))
                _clip_kernel32.GlobalUnlock(h)
                if not _clip_user32.SetClipboardData(CF_UNICODETEXT, h):
                    _clip_kernel32.GlobalFree(h)
                    return False
                return True
            finally:
                _clip_user32.CloseClipboard()
        time.sleep(0.002)
    return False


class Arma3Oracle(Oracle):
    """
    Ground truth for Arma 3 through a generated mission and the clipboard.

    The harness cannot type into Arma, but Arma can run a script: the oracle
    writes a one-soldier mission into the game's ``Missions`` folder (the VR
    world, flat and gridded, so the frame oracle sees a turn too) whose
    ``init.sqf`` publishes the player's pose about fifty times a second with
    ``copyToClipboard``: position (ASL), the eye direction's yaw (compass
    heading, clockwise) and pitch (up positive), the body heading, and which
    user actions are held. The recipe launches the game straight into it
    with ``-init=playMission['','<mission>',true]``. The way back is the same
    channel: ``reset`` writes ``NIMBUS_RESET x y z dir`` to the clipboard,
    which the script reads before each pose it writes and acts on, so no key
    press is needed at all. A pose is only believed when its tick time has
    moved on since the last read, so a stale clipboard (the game gone, or
    not yet in the mission) reads as no pose. The user's clipboard is
    clobbered for the length of a run.
    """
    kind = "arma3"
    has_pose = True
    resets_pitch = False      # setDir levels the yaw; nothing in SQF sets the aim pitch

    def __init__(self, recipe: Dict[str, Any]) -> None:
        super().__init__(recipe)
        o = recipe["oracle"]
        self.mission = str(o.get("mission", "nimbus_harness.VR"))
        self.mission_dir = os.path.join(recipe["game_dir"], "Missions", self.mission)
        self.spawn = [float(v) for v in o.get("spawn", [4096.0, 4096.0])]
        self.button_echo: Dict[str, str] = dict(o.get("button_echo", {}))
        self._last_t = -1.0
        self.last_line = ""
        self.last_foreign = ""
        args = [str(a) for a in recipe.get("launch_args", [])]
        want = f"-init=playMission['','{self.mission}',true]"
        if want not in args:
            raise ValueError(f"an arma3 recipe needs {want} in launch_args")

    def enable_pad_in_profile(self) -> Optional[str]:
        """
        Turn on the XInput controller in the player's profile.

        Arma lists every controller it found at start-up in the profile's
        ``JoysticksList`` and gives a new XInput pad ``mode="Disabled"``, to be
        enabled by hand in Options, Controls, Controllers. With it disabled the
        sticks and buttons do nothing (measured 2026-09-09: a pad-first launch
        listed "Controller (XBOX 360 For Windows)" as disabled and every
        action read as unheld; set to ``Custom`` the same pad drove the game).
        The profile only carries the entry after one launch with the pad
        present, so the first run on a machine enables nothing and the second
        one works; the change is made once, behind a ``.nimbus-harness.bak``
        copy, and left in place, since it is what a player would set.
        Returns a note for the log, or ``None`` when nothing was changed.
        """
        docs = os.path.join(os.path.expanduser("~"), "Documents", "Arma 3")
        try:
            names = [n for n in os.listdir(docs) if n.endswith(".Arma3Profile") and ".vars." not in n]
        except OSError:
            return None
        notes = []
        for name in names:
            path = os.path.join(docs, name)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            disabled = 'isXInput=1;\n\t\tmode="Disabled";'
            if disabled not in text:
                continue
            bak = path + ".nimbus-harness.bak"
            if not os.path.exists(bak):
                shutil.copy2(path, bak)
            text = text.replace(disabled, 'isXInput=1;\n\t\tmode="Custom";')
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            notes.append(f"enabled the XInput controller in {name} (backup beside it)")
        return "; ".join(notes) or None

    def prepare_launch(self) -> None:
        note = self.enable_pad_in_profile()
        if note:
            print(f"[harness] {note}", flush=True)
        os.makedirs(self.mission_dir, exist_ok=True)
        x, y = self.spawn
        sqm = ARMA_MISSION_SQM % {"x": f"{x:.1f}", "y": f"{y:.1f}", "y_back": f"{y - 16:.1f}"}
        actions = "[" + ",".join(f'"{a}"' for a in ARMA_ACTIONS) + "]"
        sqf = ARMA_INIT_SQF % {"stamp": time.strftime("%Y-%m-%d %H:%M:%S"), "actions": actions}
        with open(os.path.join(self.mission_dir, "mission.sqm"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(sqm)
        with open(os.path.join(self.mission_dir, "init.sqf"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(sqf)
        # A pose line left on the clipboard by the previous instance would read
        # as one fresh pose and make ready() true before the game has loaded.
        clipboard_write("")
        self._last_t = -1.0

    def cleanup(self) -> None:
        try:
            if self.last_line:
                self.exec("call NIMBUS_KILL", timeout=1.0)
        except Exception:
            pass
        try:
            shutil.rmtree(self.mission_dir)
        except OSError:
            pass

    # queries
    def _parse(self, line: str) -> Optional[Dict[str, Any]]:
        m = ARMA_POSE_RE.match(line or "")
        if not m:
            return None
        t = float(m.group(1))
        return {"t": t,
                "pos": [float(m.group(2)), float(m.group(3)), float(m.group(4))],
                "ang": [float(m.group(6)), float(m.group(5)), 0.0],
                "dir": float(m.group(7)),
                "buttons": [b for b in m.group(8).split(",") if b],
                "execs": int(m.group(9) or 0),
                "tgt": m.group(10) or "none"}

    def _fresh(self, timeout: float) -> Optional[Dict[str, Any]]:
        """The next pose whose tick time differs from the last one returned."""
        deadline = time.monotonic() + timeout
        while True:
            line = clipboard_read() or ""
            p = self._parse(line)
            if p and p["t"] != self._last_t:
                self._last_t = p["t"]
                self.last_line = line
                return p
            if time.monotonic() >= deadline:
                if line and not p:
                    # someone else's clipboard, or a stale line: worth seeing in the log
                    self.last_foreign = line[:80]
                return None
            time.sleep(0.004)

    def log_offset(self) -> int:
        return int(self._last_t * 1000)

    def pose(self, timeout: float = 1.5, tries: int = 2) -> Optional[Dict[str, Any]]:
        p = self._fresh(timeout * tries)
        if p is None:
            return None
        return {"pos": p["pos"], "ang": p["ang"]}

    def ready(self) -> bool:
        """Two poses with advancing tick times: the mission's loop is live, not
        a line that happened to be on the clipboard."""
        a = self._fresh(1.0)
        b = self._fresh(1.0) if a else None
        return bool(a and b and b["t"] > a["t"])

    def reset(self) -> bool:
        if not self.reset_pose:
            return False
        x, y, z = self.reset_pose["pos"]
        yaw = self.reset_pose["ang"][1]
        for _ in range(3):
            clipboard_write(f"NIMBUS_RESET {x:.3f} {y:.3f} {z:.3f} {yaw:.3f}")
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                p = self.pose(timeout=0.3, tries=1)
                if p and pose_error(p, self.reset_pose)[0] < 0.5 and abs(wrap_deg(p["ang"][1] - yaw)) < 1.0:
                    time.sleep(0.3)
                    return True
        return False

    def exec(self, sqf: str, timeout: float = 2.0) -> bool:
        """Run one line of SQF inside the mission, through the clipboard: the
        script takes the command on its next pass and marks it taken. For
        diagnosis and calibration on the dev machine; the mission is a
        throwaway one and the clipboard is local, but it is a code channel,
        so it exists only while the harness mission runs."""
        before = self._fresh(0.5)
        n0 = before["execs"] if before else 0
        clipboard_write("NIMBUS_EXEC " + sqf)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            p = self._fresh(0.2)
            if p and p["execs"] > n0:
                return True
        return False

    # the target oracle (TARGET_AWARE_AIM_PLAN.md phase 1)
    def spawn_target(self, bearing_deg: float, range_m: float = 30.0) -> bool:
        """Place a target that far off where the player is looking, and that far away.

        The unit is harmless (damage off, AI off, the player's own side) and
        the mission publishes its head on every pose line, which is the
        ground truth the closed loop is measured against. Returns whether
        the mission reported the placement.
        """
        if not self.exec(f"[{float(bearing_deg):.3f}, {float(range_m):.2f}] call NIMBUS_SPAWN"):
            return False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            p = self._fresh(0.3)
            if p and p.get("tgt", "none") != "none":
                return True      # placed; whether the engine will draw it is target()'s business
        return False

    def walk_target(self, speed_m_s: float) -> bool:
        """Walk the placed target across the line of sight at that speed."""
        return self.exec(f"[{float(speed_m_s):.2f}] call NIMBUS_WALK")

    def delete_target(self) -> bool:
        """Remove the target; the pose line goes back to ``tgt=none``."""
        if not self.exec("call NIMBUS_KILL"):
            return False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            p = self._fresh(0.3)
            if p and p.get("tgt", "none") == "none":
                return True
        return False

    def target(self) -> Optional[Dict[str, Any]]:
        """Where the target is right now, or ``None`` when there is not one.

        ``visible`` says whether the engine projected it to the screen. A
        target that is placed but not drawn still reports its bearing,
        elevation and range, which is what tells the harness where the view
        has to move before it can measure anything: the pose's own pitch
        field cannot be trusted for that on this game.
        """
        p = self._fresh(1.0)
        if not p or p.get("tgt", "none") == "none":
            return None
        parts = str(p["tgt"]).split(",")
        try:
            if parts[0] == "off":
                az, el, dist = (float(v) for v in parts[1:4])
                return {"visible": False, "sx": None, "sy": None, "w": 0.0, "h": 0.0,
                        "bearing": az, "elevation": el, "range": dist}
            sx, sy, w, h, az, el, dist = (float(v) for v in parts)
        except (ValueError, IndexError):
            return None
        return {"visible": True, "sx": sx, "sy": sy, "w": w, "h": h,
                "bearing": az, "elevation": el, "range": dist}

    def wait_echo(self, marker: str, offset: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            p = self._fresh(0.3)
            if p and marker in p["buttons"]:
                return True
        return False

    def echo_buttons(self) -> List[Tuple[int, str]]:
        out = []
        for b, m in self.button_echo.items():
            try:
                out.append((int(b), m))
            except ValueError:
                pass
        return out


def make_oracle(recipe: Dict[str, Any]) -> Oracle:
    kind = str((recipe.get("oracle") or {}).get("type", "frame_diff"))
    if kind == "source_console":
        return SourceConsoleOracle(recipe)
    if kind == "frame_diff":
        return FrameDiffOracle(recipe)
    if kind == "arma3":
        return Arma3Oracle(recipe)
    raise ValueError(f"unknown oracle type {kind!r}")


# ---- Qt thread hop ---------------------------------------------------------------
class _QtCall(QObject):
    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        fn()


_QT: Optional[_QtCall] = None


def on_qt(fn: Callable[[], Any], timeout: float = 10.0) -> Any:
    """Run ``fn`` on the Qt thread and return its result. Never nest."""
    assert _QT is not None, "NimbusActuator.start() has not run"
    done = threading.Event()
    box: Dict[str, Any] = {}

    def wrapper() -> None:
        try:
            box["r"] = fn()
        except Exception as exc:   # noqa: BLE001
            box["e"] = exc
        finally:
            done.set()

    _QT.call.emit(wrapper)
    if not done.wait(timeout):
        raise TimeoutError("the Qt thread did not answer in time")
    if "e" in box:
        raise box["e"]
    return box.get("r")


# ---- actuators -------------------------------------------------------------------
def _xusb_by_id() -> Dict[int, Any]:
    # The app's own id-to-mask table, so the pad actuator presses what the app presses.
    from src.vigem_interface import XUSB_BY_ID
    return dict(XUSB_BY_ID)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


class PadActuator:
    """A ViGEm Xbox 360 pad the harness owns: what it is asked for is what the game gets."""

    name = "pad"

    def __init__(self) -> None:
        from src.padbus_client import X360Pad
        self.pad = X360Pad()
        self.pad.update()
        self._xusb = _xusb_by_id()
        self._held: set = set()
        self.last_sent: Dict[str, Any] = {}

    def prepare(self, env: "GameEnv") -> Optional[str]:
        return None

    def apply(self, action: Dict[str, Any]) -> None:
        lx = _clamp(action.get("lx", 0.0), -1, 1)
        ly = _clamp(action.get("ly", 0.0), -1, 1)
        rx = _clamp(action.get("rx", 0.0), -1, 1)
        ry = _clamp(action.get("ry", 0.0), -1, 1)
        lt = _clamp(action.get("lt", 0.0), 0, 1)
        rt = _clamp(action.get("rt", 0.0), 0, 1)
        want = {int(b) for b in action.get("buttons", [])}
        self.pad.left_joystick_float(x_value_float=lx, y_value_float=ly)
        self.pad.right_joystick_float(x_value_float=rx, y_value_float=ry)
        self.pad.left_trigger_float(value_float=lt)
        self.pad.right_trigger_float(value_float=rt)
        for b in self._held - want:
            self.pad.release_button(button=self._xusb[b])
        for b in want - self._held:
            self.pad.press_button(button=self._xusb[b])
        self._held = want
        self.pad.update()
        self.last_sent = {"left_x": lx, "left_y": ly, "right_x": rx, "right_y": ry,
                          "left_trigger": lt, "right_trigger": rt, "buttons": sorted(want)}

    def release(self) -> None:
        self.apply({})

    def sent(self) -> Dict[str, Any]:
        return dict(self.last_sent)

    def close(self) -> None:
        if self.pad is not None:
            try:
                self.release()
            except Exception:
                pass
            self.pad = None


class NimbusActuator:
    """The real Nimbus QML app in-process, its widgets driven by synthesized pointer events.

    ``start()`` runs on the main thread and builds the app; ``run(scenario)``
    executes the Qt event loop with ``scenario`` on a worker thread. Every
    other method is called from that worker and hops to the Qt thread where
    it must. The game is launched after ``prepare()``, so Nimbus's pad is the
    one the game finds at start-up.
    """

    name = "nimbus"
    PROBE_PROFILE_ID = "nimbus_harness"

    def __init__(self, profile: Optional[str] = None) -> None:
        self.profile = profile            # None: a throwaway copy of the bundled profile
        self._probe_profile_path: Optional[str] = None
        self.app = None
        self.bridge = None
        self.engine = None
        self.config = None
        self.original_profile = None
        self.config_path = os.path.join(REPO, "controller_config.json")
        self.config_backup: Optional[bytes] = None
        self.sticks: Dict[str, str] = {}      # "right" / "left" -> widget id
        self.buttons: Dict[int, str] = {}     # bridge button id -> widget id
        self._travel: Dict[str, float] = {}
        self._centre: Dict[str, QPointF] = {}
        self._held_sticks: set = set()
        self._held_buttons: set = set()
        self._bridge_only: set = set()    # buttons pressed at the bridge for want of a widget
        self._last_press: Dict[str, float] = {}
        self._telemetry = None

    # lifecycle (main thread)
    def start(self) -> bool:
        global _QT
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root object wraps as a bare QWindow without it)
        from PySide6.QtWidgets import QApplication
        from src.bridge import ControllerBridge
        from src.cloud_client import CloudClient
        from src.config import ControllerConfig
        from src.qt_qml_app import qml_path
        from src.telemetry import TelemetryClient
        from src.updater import UpdateChecker

        if os.path.exists(self.config_path):
            with open(self.config_path, "rb") as fh:
                self.config_backup = fh.read()
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setApplicationName("Nimbus Adaptive Controller")
        _QT = _QtCall()
        self.config = ControllerConfig()
        self.original_profile = self.config.get_current_profile()
        self.bridge = ControllerBridge(self.config)
        self._telemetry = TelemetryClient(self.config)
        cloud = CloudClient(self.config)
        updater = UpdateChecker(self.config)
        self.engine = QQmlApplicationEngine()
        ctx = self.engine.rootContext()
        ctx.setContextProperty("controller", self.bridge)
        ctx.setContextProperty("config", self.config)
        ctx.setContextProperty("telemetry", self._telemetry)
        ctx.setContextProperty("cloud", cloud)
        ctx.setContextProperty("updater", updater)
        self._keep = (cloud, updater)
        self.engine.load(QUrl.fromLocalFile(str(qml_path())))
        if not self.engine.rootObjects():
            print("QML failed to load", flush=True)
            return False
        return True

    def run(self, scenario: Callable[[], None]) -> None:
        def body() -> None:
            try:
                scenario()
            except Exception as exc:   # noqa: BLE001
                import traceback
                traceback.print_exc()
                print(f"scenario crashed: {type(exc).__name__}: {exc}", flush=True)
            finally:
                try:
                    self.close()
                except Exception:
                    pass
                on_qt(self.app.quit)

        QTimer.singleShot(1500, lambda: threading.Thread(target=body, daemon=True, name="HarnessScenario").start())
        try:
            self.app.exec()
        finally:
            try:
                self._telemetry.shutdown()
            except Exception:
                pass
            if self.config_backup is not None:
                with open(self.config_path, "wb") as fh:
                    fh.write(self.config_backup)
                print("[harness] controller_config.json restored", flush=True)

    # worker thread
    def _items(self) -> list:
        out = []
        stack = [self.engine.rootObjects()[0].contentItem()]
        while stack:
            it = stack.pop()
            out.append(it)
            stack.extend(it.childItems())
        return out

    def _find(self, object_name: str):
        for it in self._items():
            if it.objectName() == object_name:
                return it
        return None

    def _travel_of(self, wid: str) -> float:
        """Pixels of drag for full deflection: the widget's ``travel_px`` when set,
        else the joystick's drawn radius (``travelRadius`` on its inner item)."""
        w = self.bridge._widget_shaping.get(wid, {})
        if float(w.get("travel_px", 0) or 0) > 0:
            return float(w["travel_px"])
        item = self._find("widget_" + wid)
        stack = [item] if item is not None else []
        while stack:
            it = stack.pop()
            v = it.property("travelRadius")
            if v is not None:
                return float(v)
            stack.extend(it.childItems())
        return float(item.width()) / 2.0 if item is not None else 100.0

    def prepare(self, env: Optional["GameEnv"] = None) -> Optional[str]:
        """Wait for the window, switch profiles, find the widgets. An error string or ``None``."""
        for _ in range(100):
            if self.bridge._window is not None:
                break
            time.sleep(0.1)
        if self.bridge._window is None:
            return "the QML window never registered with the bridge"
        if not on_qt(lambda: self.bridge._is_controller_connected()) or self.bridge.getOutputMode() != "vigem":
            return f"needs ViGEm output (connected={self.bridge._is_controller_connected()} mode={self.bridge.getOutputMode()})"
        if self.profile is None:
            # A throwaway copy of the bundled profile, so the run does not depend
            # on what the user has done to their own copy (the shaping probe does
            # the same). Removed at close.
            with open(os.path.join(REPO, "profiles", "adaptive_platform_2.json"), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["name"] = "Nimbus Harness"
            data["description"] = "Written by tests/game_harness.py for one run; safe to delete"
            self._probe_profile_path = os.path.join(self.config.get_user_profiles_path(),
                                                    f"{self.PROBE_PROFILE_ID}.json")
            os.makedirs(os.path.dirname(self._probe_profile_path), exist_ok=True)
            with open(self._probe_profile_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=4)
            self.profile = self.PROBE_PROFILE_ID
        if self.config.get_current_profile() != self.profile:
            on_qt(lambda: self.bridge.switchProfile(self.profile))
            time.sleep(1.2)

        def scan():
            sticks, buttons, centres = {}, {}, {}
            candidates: Dict[str, List[Tuple[str, QPointF]]] = {}
            rects: List[Tuple[str, QRectF]] = []
            for it in self._items():
                name = it.objectName()
                if not name.startswith("widget_"):
                    continue
                wid = name[7:]
                w = self.bridge._widget_shaping.get(wid, {})
                wtype = it.property("widgetType") or w.get("type")
                c = it.mapToScene(QPointF(it.width() / 2.0, it.height() / 2.0))
                rects.append((wid, it.mapRectToScene(QRectF(0.0, 0.0, it.width(), it.height()))))
                if wtype == "joystick":
                    m = w.get("mapping") or it.property("mapping") or {}
                    side = {"rx": "right", "x": "left"}.get(str(m.get("axis_x", "")).lower())
                    if side:
                        candidates.setdefault(side, []).append((wid, c))
                    centres[wid] = c
                elif wtype == "button" and w.get("button_id") is not None:
                    buttons[int(w["button_id"])] = wid
                    centres[wid] = c
            # Widgets later in the profile are drawn on top, and a synthesized
            # press at a centre that a later widget covers lands on that widget
            # (the user's copy of the bundled profile has two overlapping left
            # sticks). So take the topmost candidate whose centre nothing later
            # covers, or the last one when every centre is covered.
            order = {wid: i for i, wid in enumerate(self.bridge._widget_shaping)}
            for side, cands in candidates.items():
                free = [(wid, c) for wid, c in cands
                        if not any(order.get(o, -1) > order.get(wid, -1) and r.contains(c) for o, r in rects)]
                sticks[side] = (free or cands)[-1][0]
            return sticks, buttons, centres

        self.sticks, self.buttons, self._centre = on_qt(scan)
        if "right" not in self.sticks:
            return f"profile {self.profile!r} has no joystick mapped to rx/ry"
        for side, wid in self.sticks.items():
            self._travel[wid] = on_qt(lambda wid=wid: self._travel_of(wid))
        print(f"[harness] Nimbus sticks={self.sticks} travel={self._travel} buttons={self.buttons}", flush=True)
        return None

    def place_beside(self, env: "GameEnv") -> None:
        """Put the Nimbus window to the right of the game, never over its client area."""
        screen_w = user32.GetSystemMetrics(0)
        if env.x + env.w + 10 + 1024 > screen_w:
            user32.SetWindowPos(env.hwnd, 0, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
            time.sleep(0.5)
            env.refresh_rect()
        nx = env.x + env.w + 10
        win = self.bridge._window
        on_qt(lambda: (win.setPosition(nx, max(0, env.y)), win.raise_()))
        time.sleep(0.8)
        # the centres are window-relative, so they survive the move
        self._centre = on_qt(lambda: {wid: self._find("widget_" + wid).mapToScene(
            QPointF(self._find("widget_" + wid).width() / 2.0, self._find("widget_" + wid).height() / 2.0))
            for wid in self._centre})

    def _send(self, ev_type, pos: QPointF, button, buttons) -> None:
        win = self.bridge._window
        g = QPointF(win.mapToGlobal(pos.toPoint()))
        QCoreApplication.sendEvent(win, QMouseEvent(ev_type, pos, pos, g, button, buttons,
                                                    Qt.KeyboardModifier.NoModifier))

    def _press(self, wid: str, pos: QPointF) -> None:
        # three presses on one stick inside 400 ms are a triple-click lock, by design
        gap = 0.45 - (time.monotonic() - self._last_press.get(wid, 0.0))
        if gap > 0:
            time.sleep(gap)
        on_qt(lambda: self._send(QEvent.Type.MouseButtonPress, pos, Qt.MouseButton.LeftButton,
                                 Qt.MouseButton.LeftButton))
        self._last_press[wid] = time.monotonic()

    def _move(self, pos: QPointF) -> None:
        on_qt(lambda: self._send(QEvent.Type.MouseMove, pos, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))

    def _release(self, pos: QPointF) -> None:
        on_qt(lambda: self._send(QEvent.Type.MouseButtonRelease, pos, Qt.MouseButton.LeftButton,
                                 Qt.MouseButton.NoButton))

    def _bridge_button(self, button_id: int, pressed: bool) -> None:
        """Press a button the profile draws no widget for, straight at the bridge.

        Only a ``ready_sequence`` needs this. A game's menus may be walked
        with buttons the bundled layout does not draw (Halo Wars needs the
        d-pad to move the highlight and Start to begin the match, and the
        layout has A, B, X, Y and the bumpers), and the menu walk is how the
        game is reached rather than any part of what is measured: every
        check that produces a number drives a real widget. Without this the
        press was silently dropped, the game sat in whatever menu the walk
        had reached, and the in-world checks read a menu as a still picture.
        It says so the first time each button takes this path, so no result
        is ever read as a widget press it was not.
        """
        if pressed and button_id not in self._bridge_only:
            self._bridge_only.add(button_id)
            print(f"[harness] button {button_id} has no widget in this profile; pressing it at the "
                  f"bridge for the menu walk (the measured checks all drive widgets)", flush=True)
        on_qt(lambda: self.bridge.setButton(int(button_id), bool(pressed)))

    def _drive_stick(self, side: str, dx: float, dy: float) -> None:
        wid = self.sticks.get(side)
        if wid is None:
            return
        c = self._centre[wid]
        if dx == 0.0 and dy == 0.0:
            if wid in self._held_sticks:
                self._release(c)
                self._held_sticks.discard(wid)
            return
        if wid not in self._held_sticks:
            self._press(wid, c)
            self._held_sticks.add(wid)
            time.sleep(0.02)
        steps = 3
        for i in range(1, steps + 1):
            self._move(QPointF(c.x() + dx * i / steps, c.y() + dy * i / steps))
            time.sleep(0.015)

    def apply(self, action: Dict[str, Any]) -> None:
        for side, kx, ky in (("right", "rx", "ry"), ("left", "lx", "ly")):
            wid = self.sticks.get(side)
            if wid is None:
                continue
            travel = self._travel.get(wid, 100.0)
            if kx + "_px" in action or ky + "_px" in action:
                dx = float(action.get(kx + "_px", 0.0))
                dy = -float(action.get(ky + "_px", 0.0))
            else:
                dx = _clamp(action.get(kx, 0.0), -1, 1) * travel
                dy = -_clamp(action.get(ky, 0.0), -1, 1) * travel   # screen down is stick down
            self._drive_stick(side, dx, dy)
        want = {int(b) for b in action.get("buttons", [])}
        for b in self._held_buttons - want:
            wid = self.buttons.get(b)
            if wid:
                self._release(self._centre[wid])
            else:
                self._bridge_button(b, False)
        for b in want - self._held_buttons:
            wid = self.buttons.get(b)
            if wid:
                self._press(wid, self._centre[wid])
            else:
                self._bridge_button(b, True)
        self._held_buttons = set(want)
        time.sleep(0.05)

    def release(self) -> None:
        self.apply({})

    def sent(self) -> Dict[str, Any]:
        if self.bridge is None or self.bridge._vigem is None:
            return {}
        return on_qt(lambda: dict(self.bridge._vigem.current_values))

    def travel(self, side: str) -> float:
        """Pixels of drag for full deflection on the stick of ``side``."""
        return float(self._travel.get(self.sticks[side], 100.0))

    def expected(self, side: str, raw: float) -> float:
        """What the bridge should send for a raw deflection of ``raw`` (0 to 1)
        on the stick of ``side``, from the bridge's own resolved parameters."""
        from src.config import shape_magnitude
        w = self.bridge._widget_shaping.get(self.sticks[side], {})
        return float(shape_magnitude(max(0.0, min(1.0, float(raw))), **self.bridge._widget_params(w)))

    def close(self) -> None:
        try:
            self.release()
        except Exception:
            pass
        try:
            if self.bridge._vigem:
                on_qt(lambda: self.bridge._vigem._reset_axes())
            if self.original_profile and self.config.get_current_profile() != self.original_profile:
                on_qt(lambda: self.bridge.switchProfile(self.original_profile))
        except Exception:
            pass
        if self._probe_profile_path:
            try:
                os.remove(self._probe_profile_path)
                print(f"[harness] removed {self._probe_profile_path}", flush=True)
            except OSError:
                pass


# ---- the environment -------------------------------------------------------------
class GameEnv:
    """A real game as an environment: launch, wait_ready, reset, step, observe, close.

    Parameters
    ----------
    recipe : dict
        A loaded recipe (``load_recipe``).
    actuator : PadActuator or NimbusActuator
        Built before the game is launched.
    call : callable
        Runs a capture on the right thread: identity for the pad, ``on_qt``
        for Nimbus (the screen grab needs the Qt thread there).
    frames_dir, tag : str
        Where frames and the results JSON go, and their name prefix.
    """

    def __init__(self, recipe: Dict[str, Any], actuator, call: Callable[[Callable[[], Any]], Any] = lambda fn: fn(),
                 frames_dir: str = FRAMES_DIR, tag: str = "", skip_top: int = 0, save_frames: bool = True) -> None:
        self.recipe = recipe
        self.actuator = actuator
        self.oracle = make_oracle(recipe)
        self._call = call
        self.frames_dir = frames_dir
        self.tag = tag or f"{recipe['name']}_{getattr(actuator, 'name', 'pad')}"
        self.skip_top = skip_top
        self.save_frames = save_frames
        self.hwnd = 0
        self.x = self.y = self.w = self.h = 0
        self.noise = 0
        self.thresholds = Thresholds()
        self.motion_kind = str((recipe.get("motion") or {}).get("kind", "shift"))
        self.reference = None                 # the frame a reset should bring the view back to
        self.reference_at = 0.0
        self.last_reset: Optional[Dict[str, Any]] = None
        self.window_wanted: Dict[str, Any] = dict(recipe.get("window") or {})
        self.window_log: List[Dict[str, Any]] = []
        self._window_stages: set = set()
        self.measured: Dict[str, Dict[str, Dict[str, Any]]] = {}   # check -> key -> {unit, value}
        self.assist_focal = 0.0        # px per radian at the centre, measured by the T1 check
        self.assist_dead_time = 0.1    # seconds the servo is told the loop takes, from T1c
        self.assist_y_span = 1.0       # the screen height the oracle's y fraction spans, in client heights
        self.assist_runs: List[Dict[str, Any]] = []   # one entry per closed-loop run, for the log
        self.records: List[Dict[str, Any]] = []
        self.launched_at = 0.0
        self.window_at = 0.0
        self.ready_at = 0.0
        os.makedirs(frames_dir, exist_ok=True)

    # lifecycle
    def launch(self) -> bool:
        r = self.recipe
        self.oracle.prepare_launch()
        args = [str(a) for a in r.get("launch_args", [])]
        cwd = None
        if r.get("exe"):
            # A game started by its own executable rather than through Steam's
            # -applaunch: for Arma 3 that is arma3battleye.exe, because Steam
            # would open the Arma 3 Launcher and wait for a click. The Steam
            # client still has to be running for the game's DRM, so it is
            # started first if it is not.
            if r.get("steam_exe") and not process_running("steam.exe"):
                print("[harness] starting Steam", flush=True)
                subprocess.Popen([r["steam_exe"], "-silent"])
                time.sleep(15.0)
            cmd = [os.path.join(r["game_dir"], str(r["exe"]))] + args
            cwd = r["game_dir"]
        else:
            cmd = [r["steam_exe"], "-applaunch", str(r["steam_app_id"])] + args
        print(f"[harness] launching: {subprocess.list2cmdline(cmd)}", flush=True)
        self.launched_at = time.time()
        subprocess.Popen(cmd, cwd=cwd)
        deadline = self.launched_at + float(r.get("window_timeout_s", 300))
        while time.time() < deadline:
            hwnd = find_game_window(r["title"], r.get("process"))
            if hwnd and not self.window_big_enough(hwnd):
                hwnd = 0                  # a start-up or splash window; the real one is still coming
            if hwnd:
                self.hwnd = hwnd
                self.window_at = time.time()
                self.refresh_rect()
                print(f"[harness] game window {hwnd} after {self.window_at - self.launched_at:.0f}s, "
                      f"client=({self.x},{self.y}) {self.w}x{self.h}", flush=True)
                self.apply_window("launch")
                return True
            time.sleep(2.0)
        return False

    def refresh_rect(self) -> None:
        self.x, self.y, self.w, self.h = client_rect_on_screen(self.hwnd)

    # the recipe's window
    def window_holds(self) -> bool:
        """Whether the client rect is the recipe's ``window`` size, within 8 px."""
        if not self.window_wanted or not self.hwnd:
            return True
        self.refresh_rect()
        return (abs(self.w - int(self.window_wanted.get("w", self.w))) <= 8
                and abs(self.h - int(self.window_wanted.get("h", self.h))) <= 8)

    def apply_window(self, stage: str) -> bool:
        """Put the game into the recipe's ``window``, once per ``stage``, and
        record whether it took.

        ``{"w": 1280, "h": 720, "x": 0, "y": 0, "borderless": true}`` is
        applied with the app's own ``src.borderless`` (``make_borderless``
        strips the frame and sizes the window; ``resize_window`` sizes a
        framed one, corrected once for the frame it keeps). It is applied
        when the window is found, again before the ready sequence and again
        at readiness if it did not hold, because a game re-asserts its own
        mode while it loads: Halo Wars shows a framed window at 2 s and a
        borderless full-screen one at 4 s. The window is looked up again
        each time, since a game may have replaced it. A game in exclusive
        full screen ignores ``SetWindowPos``; that is recorded as a
        property of the game, in the log and the results JSON, not a
        failure of the run.
        """
        stages = self.recipe.get("window_stages")
        if stages and stage not in stages:
            # A game mid-load can hang on a style change (Arma 3 stalled before
            # "Starting mission" when resized at launch, 2026-09-09), so a recipe
            # can name the stages at which its window is applied.
            return False
        want = self.window_wanted
        if not want or not self.hwnd or stage in self._window_stages:
            return True
        self._window_stages.add(stage)
        if not BORDERLESS_AVAILABLE:
            print("[harness] window: src.borderless is not importable, leaving the game as it is", flush=True)
            return False
        hwnd = find_game_window(self.recipe["title"], self.recipe.get("process"))
        if hwnd:
            self.hwnd = hwnd
        self.refresh_rect()
        before = [self.x, self.y, self.w, self.h]
        x, y = int(want.get("x", 0)), int(want.get("y", 0))
        w, h = int(want["w"]), int(want["h"])
        borderless = bool(want.get("borderless", True))
        if borderless:
            make_borderless(self.hwnd, x, y, w, h)
        else:
            resize_window(self.hwnd, x, y, w, h)
        time.sleep(0.6)
        self.refresh_rect()
        if not borderless and (self.w != w or self.h != h) and 0 < self.w < w and 0 < self.h < h:
            resize_window(self.hwnd, x, y, w + (w - self.w), h + (h - self.h))
            time.sleep(0.6)
            self.refresh_rect()
        took = abs(self.w - w) <= 8 and abs(self.h - h) <= 8
        self.window_log.append({"stage": stage, "before": before, "after": [self.x, self.y, self.w, self.h],
                                "took": took, "at_s": round(time.time() - self.launched_at, 1)})
        print(f"[harness] window ({stage}): client was ({before[0]},{before[1]}) {before[2]}x{before[3]}, "
              f"asked for {w}x{h}{' borderless' if borderless else ''}, now ({self.x},{self.y}) {self.w}x{self.h}: "
              f"{'took' if took else 'the game did not accept it'}", flush=True)
        return took

    def pointer_to(self, xf: float, yf: float) -> None:
        """Put a game's own pad pointer at a fraction of the client rect.

        Some menus are driven by a virtual pointer that the left stick moves
        and A clicks on, rather than by a highlight the d-pad walks.
        PowerWash Simulator is one, and its pointer ignores ``SetCursorPos``
        (measured 2026-09-08: the pointer did not follow the cursor and a
        synthesized click did nothing), so the only way to aim it is to park
        it in a corner, where it clamps, and then move by time at the
        recipe's measured ``pointer_px_per_s``. One axis at a time, because a
        diagonal may be normalized and the speed is only known along an axis.
        The speed was measured at one window width, ``pointer_ref_w``; a
        pointer drawn by a UI canvas that scales with the window moves in
        proportion, so the speed is scaled by the current client width over
        that reference when the recipe gives one.
        """
        speed = float(self.recipe.get("pointer_px_per_s", 0.0) or 0.0)
        if speed <= 0:
            print("[harness] sequence: the recipe has no pointer_px_per_s, so the pointer cannot be aimed",
                  flush=True)
            return
        self.front()
        self.refresh_rect()
        ref_w = float(self.recipe.get("pointer_ref_w", 0.0) or 0.0)
        if ref_w > 0 and self.w > 0 and abs(self.w - ref_w) > 1:
            speed *= self.w / ref_w
            print(f"[harness] sequence: pointer speed scaled to {speed:.0f} px/s for a {self.w} px wide window",
                  flush=True)
        self.actuator.apply({"lx": -1.0, "ly": 1.0})
        time.sleep(float(self.recipe.get("pointer_park_s", 2.5)))
        self.actuator.release()
        time.sleep(0.15)
        for axis, sign, dist in (("lx", 1.0, xf * self.w), ("ly", -1.0, yf * self.h)):
            hold = dist / speed
            if hold <= 0.01:
                continue
            self.actuator.apply({axis: sign})
            time.sleep(hold)
            self.actuator.release()
            time.sleep(0.15)
        print(f"[harness] sequence: pointer parked top left, then to ({xf:.3f}, {yf:.3f}) "
              f"of {self.w}x{self.h}", flush=True)

    def run_sequence(self) -> None:
        """Play the recipe's ``ready_sequence`` after the window appears: the
        button presses that take a game from its title screen into a map.

        Each step is ``{"wait": seconds}``, ``{"press": [button ids], "hold":
        seconds}``, ``{"pointer": [x fraction, y fraction]}``, which aims a
        pad-pointer menu (``pointer_to``) and is usually given with a
        ``press`` in the same step, or ``{"wait_until_control": {action},
        "hold": s, "interval": s, "timeout": s}``, which holds the action (a right stick,
        say) every ``interval`` seconds until the picture moves against an
        idle capture: the world is loaded and the stick steers it. Menus and
        loading screens ignore a stick, so this is the readiness test for a
        game with no console, and it presses nothing in-world. Steps carry an
        optional ``note``. Games with a console oracle usually need none of
        this, because ``+map`` does the work.
        """
        if self.window_wanted and not self.window_holds():
            self.apply_window("sequence")
        for step in self.recipe.get("ready_sequence", []) or []:
            note = f" ({step['note']})" if step.get("note") else ""
            if step.get("wait"):
                print(f"[harness] sequence: wait {float(step['wait']):.0f}s{note}", flush=True)
                time.sleep(float(step["wait"]))
            if step.get("pointer"):
                xf, yf = (float(v) for v in list(step["pointer"])[:2])
                self.pointer_to(xf, yf)
            if step.get("press"):
                print(f"[harness] sequence: press {list(step['press'])}{note}", flush=True)
                self.front()
                self.actuator.apply({"buttons": [int(b) for b in step["press"]]})
                time.sleep(float(step.get("hold", 0.15)))
                self.actuator.release()
                time.sleep(0.25)
            if step.get("wait_until_control") or step.get("press_until_control"):
                # A stick test first: the world answers a stick, menus and
                # loading screens do not. "Moved" is a twentieth of the
                # sampled frame, because a menu's shimmer changes a few
                # hundred samples and a camera turn tens of thousands. With
                # ``press_until_control`` a failed test is followed by a press
                # (OK on a notice, "press any button", Continue), so no press
                # lands in the world once the stick works.
                action = dict(step.get("wait_until_control") or step.get("action") or {"rx": 1.0})
                buttons = [int(b) for b in (step.get("press_until_control") or [])]
                hold = float(step.get("hold", 0.6))
                interval = float(step.get("interval", 5.0))
                deadline = time.monotonic() + float(step.get("timeout", 150.0))
                what = f"press {buttons} until" if buttons else "wait until"
                print(f"[harness] sequence: {what} {action} moves the picture{note}", flush=True)
                while time.monotonic() < deadline:
                    self.front()
                    a = self.grab()
                    self.actuator.apply(action)
                    time.sleep(hold)
                    b = self.grab()
                    self.actuator.release()
                    moved = frame_diff(a, b, skip_top=self.skip_top)
                    total = a[self.skip_top::6, ::6].shape[0] * a[self.skip_top::6, ::6].shape[1]
                    if moved > max(150, total // 20):
                        print(f"[harness] sequence: the picture moved ({moved} of {total} samples)", flush=True)
                        break
                    if buttons:
                        self.actuator.apply({"buttons": buttons})
                        time.sleep(float(step.get("press_hold", 0.15)))
                        self.actuator.release()
                    time.sleep(interval)
                else:
                    print("[harness] sequence: the stick never moved the picture before the timeout", flush=True)

    def wait_ready(self, live_min: int = 50, stable_s: float = 1.5) -> bool:
        """Wait until the game is playable: the oracle answers, the picture is
        live, and the pose has stopped moving.

        The oracle alone is not enough: on Left 4 Dead 2 the server-side
        player has a pose while the client still shows the loading screen,
        and the spawn moves it 65 units when loading ends. A loading screen
        is a static picture (two captures half a second apart differ in a
        handful of samples; an in-game view differs in hundreds), so the
        picture has to change by more than ``live_min`` samples and the pose
        has to hold still for ``stable_s`` before the game counts as ready.
        """
        deadline = time.time() + float(self.recipe.get("ready_timeout_s", 240))
        last_pose = None
        gone_noted = False
        while time.time() < deadline:
            if not self.relocate_window():
                if not gone_noted:
                    print("[harness] the game window is gone; waiting for it to come back", flush=True)
                    gone_noted = True
                time.sleep(2.0)
                continue
            self.front()
            if self.oracle.ready():
                if self.window_wanted and not self.window_holds():
                    self.apply_window("ready")
                a = self.grab()
                time.sleep(0.5)
                b = self.grab()
                live = frame_diff(a, b, skip_top=self.skip_top)
                p0 = self.oracle.pose()
                time.sleep(stable_s)
                p1 = self.oracle.pose()
                if p0 and p1:
                    moved = pose_error(p0, p1)[0]
                else:
                    # a console that stopped answering is not ready; an
                    # oracle with no pose at all has nothing to hold still
                    moved = math.inf if self.oracle.has_pose else 0.0
                if live > live_min and moved < 1.0:
                    self.ready_at = time.time()
                    print(f"[harness] ready after {self.ready_at - self.launched_at:.0f}s "
                          f"(picture live: {live} samples changed; pose still)", flush=True)
                    return True
                if p1 != last_pose:
                    print(f"[harness] not ready yet: live={live} moved={moved:.1f}", flush=True)
                last_pose = p1
            time.sleep(2.0)
        return False

    def close(self, keep_game: bool = False) -> None:
        try:
            self.actuator.release()
        except Exception:
            pass
        if keep_game and self.window_log and BORDERLESS_AVAILABLE and self.hwnd and user32.IsWindow(self.hwnd):
            restore_window(self.hwnd)
        if not keep_game and self.recipe.get("process"):
            if process_running(self.recipe["process"]):
                print(f"[harness] killing {self.recipe['process']}", flush=True)
                kill_process(self.recipe["process"])
        self.oracle.cleanup()

    # the game window
    def level_pitch(self, target: float = 1.0, passes: int = 4) -> float:
        """Bring the pitch back to level with the right stick, for a game whose
        reset can only set the yaw (Arma 3). A closed loop: read the pitch,
        hold the stick the other way at 0.6 for as long as the recipe's
        ``pitch_rate_deg_per_s`` (at 0.6; 25 on Arma 3) says it takes, read
        again, at most ``passes`` times. Works through either actuator, so
        under Nimbus it is the aim widget being dragged. Returns the pitch left."""
        rate = float(self.recipe.get("pitch_rate_deg_per_s", 25.0))
        pitch = 0.0
        for _ in range(passes):
            p = self.oracle.pose()
            if not p:
                break
            pitch = p["ang"][0]
            if abs(pitch) < target:
                break
            hold = max(0.08, min(1.5, abs(pitch) / rate))
            self.actuator.apply({"ry": -0.6 if pitch > 0 else 0.6})
            time.sleep(hold)
            self.actuator.release()
            time.sleep(0.35)
        return pitch

    def window_big_enough(self, hwnd: int) -> bool:
        """Whether ``hwnd``'s client area meets the recipe's ``window_min_client``
        (``[w, h]``, default none). Arma 3 shows a 506x250 window while it starts
        and destroys it once the game window exists; matching the title alone
        takes the wrong one."""
        mw, mh = (list(self.recipe.get("window_min_client") or [0, 0]) + [0, 0])[:2]
        if not mw and not mh:
            return True
        try:
            _x, _y, w, h = client_rect_on_screen(hwnd)
        except Exception:
            return False
        return w >= int(mw) and h >= int(mh)

    def relocate_window(self) -> bool:
        """The game window, found again if the game replaced it: Arma 3 shows
        one window while it starts and another once loaded, so the handle from
        launch goes dead. True when there is a live window."""
        if self.hwnd and user32.IsWindow(self.hwnd):
            return True
        hwnd = find_game_window(self.recipe["title"], self.recipe.get("process"))
        if not hwnd or not self.window_big_enough(hwnd):
            return False
        print(f"[harness] the game replaced its window: {self.hwnd} -> {hwnd}", flush=True)
        self.hwnd = hwnd
        self.refresh_rect()
        return True

    def front(self) -> None:
        if not self.hwnd or not self.relocate_window():
            return
        if _hwnd_int(user32.GetForegroundWindow()) == self.hwnd:
            return
        bring_to_front(self.hwnd)
        user32.SetCursorPos(self.x + self.w // 2, self.y + self.h // 2)
        time.sleep(0.3)

    def grab(self):
        return self._call(lambda: grab(self.hwnd))

    def set_noise(self, idle: List[Dict[str, Any]]) -> None:
        """Set the noise floor and the motion thresholds from several idle
        steps, and re-verdict those steps with them.

        One idle second was too thin a floor: Halo Wars measured 802 on one
        run and 87 on the next for the same scene, which moved the old
        MOVED threshold fourfold and turned a HUD flicker into a camera
        move. The floor is now the largest changed count over the samples,
        and the shift threshold twice the largest coherent idle shift, so a
        scene with a swinging flashlight (Left 4 Dead 2) or a bobbing wand
        (PowerWash Simulator) sets its own bar.
        """
        motions = [r["motion"] for r in idle if r.get("motion")]
        changed = [int(r["changed"]) for r in idle if r.get("changed") is not None]
        self.thresholds = Thresholds.from_idle(motions, changed)
        self.noise = self.thresholds.noise
        for r in idle:
            r["verdict"] = self.verdict(r)

    def verdict(self, rec: Dict[str, Any]) -> str:
        """MOVED, STILL or INCONCLUSIVE for a step record, from its motion measurement."""
        if rec.get("changed") is None:
            return "n/a"
        return motion_verdict(rec.get("motion"), self.thresholds, rec.get("changed"))

    def motion_note(self, rec: Dict[str, Any]) -> str:
        """The frame side of a results line: changed count, the measured motion, the verdict."""
        return f"changed={rec.get('changed')} {describe(rec.get('motion'))} -> {rec.get('verdict') or self.verdict(rec)}"

    # observations
    def pose(self, **kw) -> Optional[Dict[str, Any]]:
        self.front()
        return self.oracle.pose(**kw)

    def observe(self, with_frame: bool = True) -> Dict[str, Any]:
        self.front()
        return {"t": time.time(), "pose": self.oracle.pose(), "frame": self.grab() if with_frame else None}

    def log_offset(self) -> int:
        return self.oracle.log_offset()

    def set_reset_pose(self, pose: Dict[str, Any]) -> None:
        self.oracle.set_reset_pose(pose)

    def can_reset(self) -> bool:
        """Whether ``reset`` does anything: a console to teleport through, or
        ``reset_buttons`` the game answers."""
        return self.oracle.has_pose or bool(self.recipe.get("reset_buttons"))

    def capture_reference(self) -> None:
        """Keep the current picture as what a reset should bring the view back to."""
        self.reference = self.grab()
        self.reference_at = time.time()

    def reset(self) -> bool:
        """Put the game back at its start: the console's teleport where there
        is one, else the recipe's ``reset_buttons`` pressed in turn.

        Halo Wars has no console but does have a reset: d-pad left jumps the
        camera to the base and right-stick click puts the rotation back to
        north, so ``[13, 10]`` restores the opening view from anywhere on
        the map. With a reference frame captured after the first reset, the
        landing is measured the way a step is: the motion from the reference
        to the picture now has to read STILL. ``reset_press_s``,
        ``reset_gap_s`` and ``reset_settle_s`` tune the presses and the wait
        for the camera to arrive.
        """
        self.front()
        if self.oracle.has_pose:
            ok = self.oracle.reset()
            if ok and not self.oracle.resets_pitch:
                self.level_pitch()
            return ok
        buttons = [int(b) for b in (self.recipe.get("reset_buttons") or [])]
        if not buttons:
            return True
        for b in buttons:
            self.actuator.apply({"buttons": [b]})
            time.sleep(float(self.recipe.get("reset_press_s", 0.15)))
            self.actuator.release()
            time.sleep(float(self.recipe.get("reset_gap_s", 0.4)))
        time.sleep(float(self.recipe.get("reset_settle_s", 1.5)))
        if self.reference is None:
            return True
        now = self.grab()
        m = measure(self.reference, now, kind=self.motion_kind, skip_top=self.skip_top)
        changed = frame_diff(self.reference, now, skip_top=self.skip_top)
        v = motion_verdict(m, self.thresholds, changed)
        self.last_reset = {"motion": m, "changed": changed, "verdict": v}
        return v == "STILL"

    def step(self, action: Dict[str, Any], hold: float, label: str, settle: float = 0.4,
             with_frame: bool = True) -> Dict[str, Any]:
        """Apply ``action``, hold it, release, settle, and report what changed.

        The frame difference is between a capture before the action and one
        taken at the end of the hold (before the release), as the aim probe
        did; the pose delta is between reads before the action and after the
        settle, so it includes the hold and the release.
        """
        self.front()
        p0 = self.oracle.pose()
        off = self.oracle.log_offset()
        a = self.grab() if with_frame else None
        t0 = time.monotonic()
        self.actuator.apply(action)
        t_applied = time.monotonic() - t0
        # Read the pose through the hold and sum the wrapped steps. A full
        # deflection turns past 180 degrees inside a second (measured: 480
        # degrees in one second on Left 4 Dead 2), and a single before-and-
        # after read folds that back into the wrong angle. Each read costs
        # about 50 ms, so the per-sample step stays far below 180.
        track = p0 is not None and self.oracle.has_pose
        total_yaw = 0.0
        last_yaw = p0["ang"][1] if p0 else 0.0
        samples = 0
        if track:
            while time.monotonic() - t0 < hold - 0.06:
                p = self.oracle.pose(timeout=0.3, tries=1)
                if p:
                    total_yaw += wrap_deg(p["ang"][1] - last_yaw)
                    last_yaw = p["ang"][1]
                    samples += 1
        remaining = hold - (time.monotonic() - t0)
        if remaining > 0:
            time.sleep(remaining)
        b = self.grab() if with_frame else None
        sent = self.actuator.sent()
        self.actuator.release()
        time.sleep(settle)
        p1 = self.oracle.pose()
        changed = frame_diff(a, b, skip_top=self.skip_top) if with_frame else None
        motion = measure(a, b, kind=self.motion_kind, skip_top=self.skip_top) if with_frame else None
        deltas = pose_delta(p0, p1)
        if track and p1:
            total_yaw += wrap_deg(p1["ang"][1] - last_yaw)
            deltas["d_yaw_wrapped"] = deltas["d_yaw"]
            deltas["d_yaw"] = total_yaw
            deltas["yaw_samples"] = samples
        rec: Dict[str, Any] = {"label": label, "action": action, "hold": hold, "apply_s": round(t_applied, 3),
                               "sent": sent, "pose_before": p0, "pose_after": p1, "changed": changed,
                               "motion": motion, "log_offset": off, **deltas}
        rec["verdict"] = self.verdict(rec)
        if with_frame and self.save_frames:
            safe = re.sub(r"[^A-Za-z0-9_]+", "_", label)
            save_frame(a, os.path.join(self.frames_dir, f"harness_{self.tag}_{safe}_before.png"))
            save_frame(b, os.path.join(self.frames_dir, f"harness_{self.tag}_{safe}_after.png"))
        self.records.append(rec)
        return rec

    def write(self, extra: Dict[str, Any]) -> str:
        out = os.path.join(self.frames_dir, f"harness_{self.tag}.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"game": self.recipe["name"], "actuator": getattr(self.actuator, "name", "?"),
                       "oracle": self.oracle.kind, "hwnd": self.hwnd, "client": [self.x, self.y, self.w, self.h],
                       "noise": self.noise, "thresholds": self.thresholds.as_dict(), "motion_kind": self.motion_kind,
                       "window": {"wanted": self.window_wanted, "log": self.window_log},
                       "reference_at_s": round(self.reference_at - self.launched_at, 1) if self.reference_at else None,
                       "last_reset": self.last_reset, "measured": self.measured,
                       "expect": self.recipe.get("expect"), "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "launch_to_window_s": round(self.window_at - self.launched_at, 1) if self.window_at else None,
                       "launch_to_ready_s": round(self.ready_at - self.launched_at, 1) if self.ready_at else None,
                       "reset_pose": self.oracle.reset_pose, "steps": self.records,
                       "assist": {"focal_px": round(self.assist_focal, 1),
                                  "dead_time_s": round(self.assist_dead_time, 4),
                                  "y_span": round(self.assist_y_span, 4),
                                  "runs": self.assist_runs} if self.assist_focal else None,
                       **extra}, fh, indent=2)
        return out
