"""
Pure-logic checks for the game compatibility database and window-matching
helpers in ``src/borderless.py``.

Covers ``get_compatible_games``, ``get_game_by_name``, ``auto_detect_game``,
``is_borderless``, ``find_window_by_title``, and the ``GameCompatEntry`` /
``WindowInfo`` dataclasses. Never touches a real window: every check that
needs a window list monkeypatches the module-level ``enumerate_windows``
attribute with a fake returning hand-built ``WindowInfo`` objects, so this
runs the same on Linux/CI (``QT_QPA_PLATFORM=offscreen``, no Win32) as on
a Windows dev box.

Run::

    python -m tests.test_borderless_gamedb
"""
from __future__ import annotations

import os
import sys
from typing import Callable, List

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import src.borderless as borderless  # noqa: E402
from src.borderless import (  # noqa: E402
    GAME_COMPATIBILITY,
    GameCompatEntry,
    WindowInfo,
    auto_detect_game,
    find_window_by_title,
    get_compatible_games,
    get_game_by_name,
    is_borderless,
)

FAILS = 0
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """
    Record and print one pass/fail check.

    Parameters
    ----------
    name : str
        Human-readable description of the assertion.
    cond : bool
        The already-evaluated condition.
    detail : str, optional
        Extra context to print alongside a failure or pass.
    """
    global FAILS, PASSES
    if cond:
        PASSES += 1
    else:
        FAILS += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  ({detail})" if detail else ""))


def with_fake_windows(windows: List[WindowInfo]) -> Callable:
    """
    Build a stand-in for ``borderless.enumerate_windows`` that returns a
    fixed, hand-built window list instead of calling into Win32.

    Parameters
    ----------
    windows : list of WindowInfo
        The windows the fake should report.

    Returns
    -------
    Callable
        A function with the same call signature as ``enumerate_windows``
        (accepts and ignores ``include_invisible``).
    """
    def _fake(include_invisible: bool = False) -> List[WindowInfo]:
        return list(windows)
    return _fake


def check_dataclasses() -> None:
    print("\nDataclass field integrity")

    w = WindowInfo(hwnd=12345, title="Test Window", class_name="TestClass", pid=999)
    check("WindowInfo.hwnd", w.hwnd == 12345)
    check("WindowInfo.title", w.title == "Test Window")
    check("WindowInfo.class_name", w.class_name == "TestClass")
    check("WindowInfo.pid", w.pid == 999)
    check("WindowInfo.x defaults to 0", w.x == 0)
    check("WindowInfo.y defaults to 0", w.y == 0)
    check("WindowInfo.width defaults to 0", w.width == 0)
    check("WindowInfo.height defaults to 0", w.height == 0)
    check("WindowInfo.is_borderless defaults to False", w.is_borderless is False)

    w2 = WindowInfo(hwnd=1, title="t", class_name="c", pid=1, x=10, y=20,
                     width=800, height=600, is_borderless=True)
    check("WindowInfo accepts explicit geometry/flag",
          (w2.x, w2.y, w2.width, w2.height, w2.is_borderless) == (10, 20, 800, 600, True))

    g = GameCompatEntry(name="Test Game", status="verified", input_method="clipcursor",
                         notes="Some notes", window_title_hint="Test Game Window",
                         needs_borderless=True, needs_cursor_release=True,
                         recommended_interval_ms=25)
    check("GameCompatEntry.name", g.name == "Test Game")
    check("GameCompatEntry.status", g.status == "verified")
    check("GameCompatEntry.input_method", g.input_method == "clipcursor")
    check("GameCompatEntry.notes", g.notes == "Some notes")
    check("GameCompatEntry.window_title_hint", g.window_title_hint == "Test Game Window")
    check("GameCompatEntry.needs_borderless", g.needs_borderless is True)
    check("GameCompatEntry.needs_cursor_release", g.needs_cursor_release is True)
    check("GameCompatEntry.recommended_interval_ms", g.recommended_interval_ms == 25)

    g_defaults = GameCompatEntry(name="Bare", status="incompatible", input_method="rawinput",
                                  notes="n/a")
    check("GameCompatEntry.window_title_hint defaults to empty string",
          g_defaults.window_title_hint == "")
    check("GameCompatEntry.needs_borderless defaults to False",
          g_defaults.needs_borderless is False)
    check("GameCompatEntry.needs_cursor_release defaults to False",
          g_defaults.needs_cursor_release is False)
    check("GameCompatEntry.recommended_interval_ms defaults to 50",
          g_defaults.recommended_interval_ms == 50)


def check_get_compatible_games() -> None:
    print("\nget_compatible_games()")

    all_games = get_compatible_games()
    check("no filter returns every entry", len(all_games) == len(GAME_COMPATIBILITY))
    check("no filter returns a copy, not the live list", all_games is not GAME_COMPATIBILITY)

    for status in ("verified", "likely", "partial", "incompatible"):
        expected = [g for g in GAME_COMPATIBILITY if g.status == status]
        got = get_compatible_games(status)
        check(f"status_filter={status!r} matches the {len(expected)} entries with that status",
              got == expected, f"got {len(got)}")
        check(f"status_filter={status!r} entries all report that status",
              all(g.status == status for g in got))

    check("at least one verified game exists",
          len(get_compatible_games("verified")) > 0)
    check("at least one incompatible game exists",
          len(get_compatible_games("incompatible")) > 0)

    unknown = get_compatible_games("this-status-does-not-exist")
    check("an unknown status_filter returns an empty list (no error, no fallback to all)",
          unknown == [])

    check("an empty-string status_filter is falsy and behaves like no filter",
          get_compatible_games("") == all_games)


def check_get_game_by_name() -> None:
    print("\nget_game_by_name()")

    hit = get_game_by_name("stardew")
    check("lowercase substring hits Stardew Valley",
          hit is not None and hit.name == "Stardew Valley")

    hit_upper = get_game_by_name("STARDEW VALLEY")
    check("uppercase substring is case-insensitive",
          hit_upper is not None and hit_upper.name == "Stardew Valley")

    hit_partial = get_game_by_name("elden")
    check("partial substring hits Elden Ring",
          hit_partial is not None and hit_partial.name == "Elden Ring")

    miss = get_game_by_name("Definitely Not A Real Game Title XYZ")
    check("a non-matching substring returns None", miss is None)


def check_auto_detect_game() -> None:
    print("\nauto_detect_game() (enumerate_windows patched)")

    original = borderless.enumerate_windows
    try:
        # ---- Match: a window title containing a game's window_title_hint ----
        match_windows = [
            WindowInfo(hwnd=1, title="Program Manager", class_name="Progman", pid=1),
            WindowInfo(hwnd=2, title="Nimbus Adaptive Controller", class_name="QWindow", pid=2),
            WindowInfo(hwnd=3, title="Stardew Valley 1.6.15", class_name="MonoGameWindow", pid=3),
        ]
        borderless.enumerate_windows = with_fake_windows(match_windows)
        result = auto_detect_game()
        check("matches on WindowInfo.title containing GameCompatEntry.window_title_hint",
              result is not None and result[0].hwnd == 3 and result[1].name == "Stardew Valley")

        # ---- Case-insensitivity of the hint match ----
        borderless.enumerate_windows = with_fake_windows([
            WindowInfo(hwnd=4, title="STARDEW VALLEY", class_name="MonoGameWindow", pid=4),
        ])
        result_ci = auto_detect_game()
        check("hint match is case-insensitive",
              result_ci is not None and result_ci[1].name == "Stardew Valley")

        # ---- No match: only benign/system-ish windows, none carrying a hint ----
        no_match_windows = [
            WindowInfo(hwnd=10, title="Program Manager", class_name="Progman", pid=10),
            WindowInfo(hwnd=11, title="Nimbus Adaptive Controller", class_name="QWindow", pid=11),
            WindowInfo(hwnd=12, title="File Explorer", class_name="CabinetWClass", pid=12),
        ]
        borderless.enumerate_windows = with_fake_windows(no_match_windows)
        check("no match returns None when nothing carries a hint", auto_detect_game() is None)

        # ---- The Nimbus window itself never produces a match ----
        # (it has no window_title_hint anywhere in GAME_COMPATIBILITY, but this
        # guards against a future accidental hint collision)
        nimbus_hints_hit = any(
            g.window_title_hint and g.window_title_hint.lower() in "nimbus adaptive controller"
            for g in GAME_COMPATIBILITY
        )
        check("no game's window_title_hint would match the Nimbus window's own title",
              not nimbus_hints_hit)

        # ---- Incompatible entries carry no window_title_hint and can't match ----
        incompatible = [g for g in GAME_COMPATIBILITY if g.status == "incompatible"]
        check("incompatible entries have no window_title_hint (never auto-detected)",
              all(not g.window_title_hint for g in incompatible), f"{len(incompatible)} entries")
    finally:
        borderless.enumerate_windows = original

    check("enumerate_windows restored to the original after patching",
          borderless.enumerate_windows is original)


def check_is_borderless() -> None:
    print("\nis_borderless()")

    check("an hwnd never passed to make_borderless is not borderless", is_borderless(999999) is False)
    check("hwnd 0 is not borderless", is_borderless(0) is False)


def check_find_window_by_title() -> None:
    print("\nfind_window_by_title() (enumerate_windows patched)")

    original = borderless.enumerate_windows
    try:
        windows = [
            WindowInfo(hwnd=1, title="Program Manager", class_name="Progman", pid=1),
            WindowInfo(hwnd=2, title="Terraria", class_name="TERRARIA", pid=2),
            WindowInfo(hwnd=3, title="Notepad - untitled", class_name="Notepad", pid=3),
        ]
        borderless.enumerate_windows = with_fake_windows(windows)

        hit = find_window_by_title("terraria")
        check("lowercase substring finds the Terraria window",
              hit is not None and hit.hwnd == 2)

        hit_ci = find_window_by_title("TERRARIA")
        check("uppercase substring is case-insensitive",
              hit_ci is not None and hit_ci.hwnd == 2)

        hit_partial = find_window_by_title("untitled")
        check("substring match works mid-title",
              hit_partial is not None and hit_partial.hwnd == 3)

        miss = find_window_by_title("No Such Window Title")
        check("a non-matching substring returns None", miss is None)
    finally:
        borderless.enumerate_windows = original

    check("enumerate_windows restored to the original after patching",
          borderless.enumerate_windows is original)


def main() -> int:
    print("Borderless game-compatibility DB checks")

    check_dataclasses()
    check_get_compatible_games()
    check_get_game_by_name()
    check_auto_detect_game()
    check_is_borderless()
    check_find_window_by_title()

    print(f"\n{PASSES}/{PASSES + FAILS} checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
