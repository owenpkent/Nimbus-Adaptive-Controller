"""
Property checks for the pure version logic in ``src/updater.py``.

Pure Python: no Qt event loop, no network, no manifest fetch — that side is
covered by ``tests/test_updater_requests.py``. This file checks
:func:`src.updater.parse_version`, :func:`src.updater.is_newer` and
:func:`src.updater.is_below_minimum` against the version-string regex and
the pre-release ordering (``dev < alpha < beta < rc < stable``) documented
in ``src/updater.py``.

Run::

    python -m tests.test_updater_versions
"""
from __future__ import annotations

import logging
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import src.updater as updater_module  # noqa: E402
from src.updater import is_below_minimum, is_newer, parse_version  # noqa: E402


class _CaptureHandler(logging.Handler):
    """Collects log records instead of printing them, so a check can inspect
    what (if anything) ``src.updater``'s logger emitted."""

    def __init__(self) -> None:
        super().__init__()
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

FAILS = 0
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILS, PASSES
    if cond:
        PASSES += 1
    else:
        FAILS += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  ({detail})" if detail else ""))


def main() -> int:
    print("Version parsing")

    # Valid parses
    check('"1.5.0" parses to (1, 5, 0, 99, 0)', parse_version("1.5.0") == (1, 5, 0, 99, 0),
          str(parse_version("1.5.0")))
    check('"v2.0.0-beta.2" parses to (2, 0, 0, 2, 2)', parse_version("v2.0.0-beta.2") == (2, 0, 0, 2, 2),
          str(parse_version("v2.0.0-beta.2")))
    check('"1.6.0-rc.1" parses to (1, 6, 0, 3, 1)', parse_version("1.6.0-rc.1") == (1, 6, 0, 3, 1),
          str(parse_version("1.6.0-rc.1")))
    check('"0.0.0" parses to (0, 0, 0, 99, 0)', parse_version("0.0.0") == (0, 0, 0, 99, 0))
    check('pre-release without a number: "1.5.0-beta" defaults pre_num to 0',
          parse_version("1.5.0-beta") == (1, 5, 0, 2, 0), str(parse_version("1.5.0-beta")))
    check('pre-release without a dot: "1.5.0-rc" still parses',
          parse_version("1.5.0-rc") == (1, 5, 0, 3, 0), str(parse_version("1.5.0-rc")))
    check('uppercase tag: "1.5.0-BETA.2" parses the same as lowercase',
          parse_version("1.5.0-BETA.2") == parse_version("1.5.0-beta.2"))
    check('leading/trailing whitespace is stripped', parse_version("  1.5.0  ") == (1, 5, 0, 99, 0))

    # Invalid strings raise ValueError
    for bad in ("1.5", "not-a-version", "", "1.5.0.1", "1.5.0-nightly.1"):
        try:
            parse_version(bad)
            check(f'"{bad}" raises ValueError', False, "no exception raised")
        except ValueError:
            check(f'"{bad}" raises ValueError', True)

    print("\nPre-release ordering (dev < alpha < beta < rc < stable)")

    dev = parse_version("1.0.0-dev.1")
    alpha = parse_version("1.0.0-alpha.1")
    beta = parse_version("1.0.0-beta.1")
    rc = parse_version("1.0.0-rc.1")
    stable = parse_version("1.0.0")
    check("dev < alpha", dev < alpha)
    check("alpha < beta", alpha < beta)
    check("beta < rc", beta < rc)
    check("rc < stable", rc < stable)
    check("a higher pre-release number outranks a lower one at the same tag",
          parse_version("1.0.0-beta.2") > parse_version("1.0.0-beta.1"))

    print("\nis_newer")

    check("higher major is newer", is_newer("2.0.0", "1.9.9") is True)
    check("lower major is not newer", is_newer("1.9.9", "2.0.0") is False)
    check("higher minor (same major) is newer", is_newer("1.6.0", "1.5.9") is True)
    check("lower minor is not newer", is_newer("1.5.9", "1.6.0") is False)
    check("higher patch (same major.minor) is newer", is_newer("1.5.1", "1.5.0") is True)
    check("lower patch is not newer", is_newer("1.5.0", "1.5.1") is False)
    check("a stable release is newer than its own rc", is_newer("1.5.0", "1.5.0-rc.1") is True)
    check("an rc is not newer than the stable release it precedes", is_newer("1.5.0-rc.1", "1.5.0") is False)
    check("rc is newer than beta at the same triple", is_newer("1.5.0-rc.1", "1.5.0-beta.5") is True)
    check("a higher pre-release number is newer at the same tag",
          is_newer("1.5.0-beta.2", "1.5.0-beta.1") is True)
    check("a lower pre-release number is not newer at the same tag",
          is_newer("1.5.0-beta.1", "1.5.0-beta.2") is False)
    check("equal versions are not newer", is_newer("1.5.0", "1.5.0") is False)
    check("equal pre-release versions are not newer", is_newer("1.5.0-beta.1", "1.5.0-beta.1") is False)
    check("an invalid remote string returns False rather than raising",
          is_newer("not-a-version", "1.5.0") is False)
    check("an invalid local string returns False rather than raising",
          is_newer("1.5.0", "not-a-version") is False)
    check("both strings invalid returns False rather than raising",
          is_newer("garbage", "also-garbage") is False)

    print("\nis_below_minimum")

    check("below the minimum is True", is_below_minimum("1.3.0", "1.4.0") is True)
    check("above the minimum is False", is_below_minimum("1.5.0", "1.4.0") is False)
    check("equal to the minimum is False", is_below_minimum("1.4.0", "1.4.0") is False)
    check("a pre-release below its own stable minimum is True",
          is_below_minimum("1.4.0-rc.1", "1.4.0") is True)
    check("an invalid current version returns False rather than raising",
          is_below_minimum("not-a-version", "1.4.0") is False)
    check("an invalid minimum version returns False rather than raising",
          is_below_minimum("1.4.0", "not-a-version") is False)

    print("\nunparseable-version logging (our own broken version string is logged; "
          "untrusted manifest data is not)")

    handler = _CaptureHandler()
    original_level = updater_module.logger.level
    updater_module.logger.addHandler(handler)
    updater_module.logger.setLevel(logging.WARNING)
    try:
        handler.records.clear()
        check("is_newer with an invalid local version still returns False",
              is_newer("1.5.0", "not-a-version") is False)
        check("...and logs a warning naming the bad local version",
              any(r.levelno == logging.WARNING and "not-a-version" in r.getMessage()
                  for r in handler.records))

        handler.records.clear()
        check("is_newer with an invalid remote (valid local) still returns False",
              is_newer("not-a-version", "1.5.0") is False)
        check("...and does NOT log a warning (remote is untrusted manifest data)",
              len(handler.records) == 0)

        handler.records.clear()
        check("is_below_minimum with an invalid current version still returns False",
              is_below_minimum("not-a-version", "1.4.0") is False)
        check("...and logs a warning naming the bad current version",
              any(r.levelno == logging.WARNING and "not-a-version" in r.getMessage()
                  for r in handler.records))

        handler.records.clear()
        check("is_below_minimum with an invalid minimum (valid current) still returns False",
              is_below_minimum("1.4.0", "not-a-version") is False)
        check("...and does NOT log a warning (minimum is untrusted manifest data)",
              len(handler.records) == 0)
    finally:
        updater_module.logger.removeHandler(handler)
        updater_module.logger.setLevel(original_level)

    print(f"\n{PASSES}/{PASSES + FAILS} checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
