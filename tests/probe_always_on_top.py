#!/usr/bin/env python3
"""
Always-on-Top check: does the Nimbus panel survive a fullscreen game?

This is probe criterion P6 from docs/vision/LINUX_PROBE_PLAN.md, measured
rather than assumed. It focuses a fullscreen window on top of a pinned panel
and counts how much of the panel the screen still shows.

  1. panel raised, screenshot                -> reference
  2. fullscreen window focused, screenshot   -> the panel must look identical

With no arguments it uses stand-ins for both windows, so it answers "does this
compositor honour _NET_WM_STATE_ABOVE over a fullscreen window" without a game
or Nimbus running:

    ./venv/bin/python tests/probe_always_on_top.py
    ./venv/bin/python tests/probe_always_on_top.py --no-pin      # control: expect FAIL

Point it at the real thing to check an actual session (start Nimbus, tick
View > Always on Top, then run this with the game already running):

    ./venv/bin/python tests/probe_always_on_top.py \
        --panel-window "Nimbus Adaptive Controller" --game-window "ELDEN RING"

Requirements: an X11 session with xdotool. Wayland has no client-settable
"above" state, so the probe reports that instead of measuring. Frames are
written to ``--out`` for inspection.

With ``--panel-window`` the pixel measure is a before/after diff, which a
third always-on-top window parked over the panel would flatter; the stacking
check printed alongside it is the decisive one in that mode.

Exit codes: 0 pass, 3 the panel was covered, 2 a requirement of this session
is unmet (not Linux, no xdotool, Qt not on xcb, no panel pixels visible),
which ``tests/run_linux_validation.sh`` reports as a gate rather than a
failure.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

PANEL_COLOR = QColor(0, 190, 120)
GAME_COLOR = QColor(190, 0, 0)


def xdo(*args: str) -> str:
    return subprocess.run(["xdotool", *args], capture_output=True, text=True).stdout.strip()


def find_window(name: str) -> int:
    """Return the newest visible window whose title contains ``name``."""
    for _ in range(5):
        ids = xdo("search", "--onlyvisible", "--name", name).split()
        if ids:
            return int(ids[-1])
        time.sleep(1)
    raise SystemExit(f"no visible window matching {name!r}")


def geometry(wid: int) -> tuple:
    """Return the client-area ``(x, y, w, h)`` of an X11 window."""
    g = dict(line.split("=") for line in xdo("getwindowgeometry", "--shell", str(wid)).split())
    return int(g["X"]), int(g["Y"]), int(g["WIDTH"]), int(g["HEIGHT"])


def wm_state(wid: int) -> str:
    out = subprocess.run(["xprop", "-id", str(wid), "_NET_WM_STATE"],
                         capture_output=True, text=True).stdout.strip()
    return out.split("=", 1)[-1].strip() or "(none)"


class Fill(QWidget):
    """A window that paints one flat colour, used as a stand-in."""

    def __init__(self, color: QColor, title: str) -> None:
        super().__init__()
        self._color = color
        self.setWindowTitle(title)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        QPainter(self).fillRect(self.rect(), self._color)


def settle(app: QApplication, seconds: float) -> None:
    """Pump the Qt event loop so the stand-in windows actually paint."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def _samples(rect: tuple, step: int, *images: QImage):
    """Yield ``(x, y)`` sample points inside ``rect`` that every image covers."""
    x0, y0, w, h = rect
    x1 = min([x0 + w] + [i.width() for i in images])
    y1 = min([y0 + h] + [i.height() for i in images])
    for y in range(max(0, y0), y1, step):
        for x in range(max(0, x0), x1, step):
            yield x, y


def rect_unchanged(a: QImage, b: QImage, rect: tuple, step: int, threshold: int) -> tuple:
    """Count sampled pixels inside ``rect`` that are identical in both frames."""
    same = total = 0
    for x, y in _samples(rect, step, a, b):
        pa, pb = a.pixel(x, y), b.pixel(x, y)
        total += 1
        if (abs(((pa >> 16) & 255) - ((pb >> 16) & 255))
                + abs(((pa >> 8) & 255) - ((pb >> 8) & 255))
                + abs((pa & 255) - (pb & 255))) <= threshold:
            same += 1
    return same, total


def rect_survives_color(before: QImage, after: QImage, rect: tuple, color: QColor,
                        step: int, threshold: int) -> tuple:
    """Of the samples showing ``color`` before, count how many still do after.

    Used for the stand-in panel, whose colour is known. Taking the "before"
    frame as the baseline ignores any area some third window already owned, so
    an unrelated always-on-top window on screen cannot skew the result in
    either direction.
    """
    want = (color.red(), color.green(), color.blue())

    def is_panel(img: QImage, x: int, y: int) -> bool:
        p = img.pixel(x, y)
        return (abs(((p >> 16) & 255) - want[0]) + abs(((p >> 8) & 255) - want[1])
                + abs((p & 255) - want[2])) <= threshold

    hit = total = 0
    for x, y in _samples(rect, step, before, after):
        if not is_panel(before, x, y):
            continue
        total += 1
        if is_panel(after, x, y):
            hit += 1
    return hit, total


def stacked_above(panel_id: int, game_id: int) -> bool | None:
    """True if the panel sits above the game in ``_NET_CLIENT_LIST_STACKING``."""
    out = subprocess.run(["xprop", "-root", "_NET_CLIENT_LIST_STACKING"],
                         capture_output=True, text=True).stdout
    ids = [int(v, 16) for v in out.replace(",", " ").split() if v.startswith("0x")]
    if panel_id not in ids or game_id not in ids:
        return None
    return ids.index(panel_id) > ids.index(game_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--panel-window", help="title substring of an already-running panel (e.g. Nimbus)")
    parser.add_argument("--game-window", help="title substring of an already-running fullscreen game")
    parser.add_argument("--no-pin", action="store_true",
                        help="control run: do not pin the stand-in panel, so it should be covered")
    parser.add_argument("--step", type=int, default=4, help="pixel sampling step (default 4)")
    parser.add_argument("--threshold", type=int, default=60, help="per-sample RGB delta counted as changed")
    parser.add_argument("--settle", type=float, default=1.5, help="seconds to wait after each raise")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "probe_frames"),
                        help="directory for the captured frames")
    args = parser.parse_args()

    if not sys.platform.startswith("linux"):
        print("Linux only."); return 2
    if not subprocess.run(["which", "xdotool"], capture_output=True).stdout:
        print("Needs xdotool (sudo apt install xdotool)."); return 2

    app = QApplication([])
    if app.platformName() != "xcb":
        print(f"Qt platform is {app.platformName()!r}, not 'xcb'.")
        print("Wayland has no client-settable 'above' state and no root-window grab;")
        print("run this from an X11 session, or set QT_QPA_PLATFORM=xcb.")
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    screen = app.primaryScreen()
    size = screen.geometry()
    owned = []

    # The panel: either a real window (Nimbus) or a pinned stand-in.
    owned_panel = not args.panel_window
    if args.panel_window:
        panel_id = find_window(args.panel_window)
        panel_note = "existing window"
    else:
        panel = Fill(PANEL_COLOR, "Nimbus probe panel")
        if not args.no_pin:
            panel.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        panel.setGeometry(size.width() // 2 - 300, size.height() // 2 - 180, 600, 360)
        panel.show()
        owned.append(panel)
        settle(app, 1.0)
        panel_id = int(panel.winId())
        panel_note = "stand-in, pinned" if not args.no_pin else "stand-in, NOT pinned (control)"

    # The coverer: either a real game or a fullscreen stand-in.
    if args.game_window:
        game_id = find_window(args.game_window)
        game_note = "existing window"
    else:
        game = Fill(GAME_COLOR, "Probe fullscreen stand-in")
        game.showFullScreen()
        owned.append(game)
        settle(app, 1.0)
        game_id = int(game.winId())
        game_note = "stand-in, fullscreen"

    rect = geometry(panel_id)
    print(f"panel: {panel_id} {xdo('getwindowname', str(panel_id))!r} ({panel_note})")
    print(f"       geometry {rect}  _NET_WM_STATE {wm_state(panel_id)}")
    print(f"game:  {game_id} {xdo('getwindowname', str(game_id))!r} ({game_note})")

    try:
        xdo("windowactivate", "--sync", str(panel_id))
        settle(app, args.settle)
        before = screen.grabWindow(0).toImage().convertToFormat(QImage.Format.Format_RGB32)
        before.save(str(out / "ontop_0_panel_raised.png"))

        xdo("windowactivate", "--sync", str(game_id))
        settle(app, args.settle)
        after = screen.grabWindow(0).toImage().convertToFormat(QImage.Format.Format_RGB32)
        after.save(str(out / "ontop_1_game_focused.png"))

        if owned_panel:
            # The stand-in's colour is known, so measure it directly. A frame
            # diff would call the panel "unchanged" if some third always-on-top
            # window covered the same area in both frames.
            hit, total = rect_survives_color(before, after, rect, PANEL_COLOR,
                                             args.step, args.threshold)
            metric = "panel pixels that survived the game"
        else:
            hit, total = rect_unchanged(before, after, rect, args.step, args.threshold)
            metric = "panel area unchanged by the game"
        if not total:
            print("no panel pixels were visible to begin with; move other windows aside")
            return 2
        pct = 100.0 * hit / total
        above = stacked_above(panel_id, game_id)
        active = xdo("getactivewindow", "getwindowname")
        print()
        print(f"{metric:<38}{hit:>8} / {total} samples ({pct:.1f}%)")
        print(f"{'panel above the game in the stack':<38}{str(above):>8}")
        print(f"{'focused window afterwards':<38}{active!r:>8}")
        print(f"panel _NET_WM_STATE now: {wm_state(panel_id)}")
        verdict = pct >= 98.0 and above is not False
        print()
        print("VERDICT:", "PASS: the panel stays visible over the focused fullscreen window"
              if verdict else "FAIL: the fullscreen window covered the panel")
        print(f"frames: {out}")
        return 0 if verdict else 3
    finally:
        for w in owned:
            w.close()
        app.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())
