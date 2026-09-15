"""
Validate the Linux stack on real hardware. Linux only, run by a person.

Everything in PRs #15 to #20 was written and checked on a Windows machine, so
the uinput and evdev halves are argued from code and from tests that mock the
kernel away. This probe is the part only a Linux box can answer. It checks the
claims those PRs make, against real devices:

  A  environment: uinput and evdev actually reachable
  B  the ControllerOutput port builds uinput back ends (#16)
  C  the keep-alive pulse cannot undo a stick release, read back from the
     kernel rather than from the interface's own bookkeeping (#15)
  D  pointer classification against this machine's real devices, including a
     touchpad if there is one (#15)
  E  no stray pass-through keyboards are left behind (#17)
  F  the bridge dispatches to the software-cursor implementation here (#18)

Safe by default: it creates its own virtual devices and never grabs the
mouse you are using. ``--grab`` additionally exercises the grab-failure
cleanup path, which briefly grabs a device; do not pass it over SSH-less
single-pointer setups unless you have a keyboard to recover with.

    python -m tests.probe_linux_stack
    python -m tests.probe_linux_stack --grab      # adds the E2 failure path

Exit code is 0 when every check that could run passed. An unmet gate is not
a failure: the sections that depend on it are skipped with the gate named,
so a machine that is not set up yet reports what to fix rather than a list
of FAILs that read like regressions.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASSES = 0
FAILS = 0
SKIPS = 0
GATES_UNMET = []
ENV = {}                     # gate name -> met, filled in by section A


def check(label, condition, detail=""):
    global PASSES, FAILS
    if condition:
        PASSES += 1
        print(f"  [PASS] {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILS += 1
        print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))
    return bool(condition)


def gate(label, condition, fix=""):
    """A precondition of this machine, not a claim about the code.

    Permissions and plugged-in hardware decide these. Reporting them as
    failures alongside the real checks is what makes a setup problem read as a
    regression, so they are counted apart and the summary says which kind of
    trouble the run is in.
    """
    global PASSES
    if condition:
        PASSES += 1
        print(f"  [PASS] {label}")
        return True
    GATES_UNMET.append((label, fix))
    print(f"  [GATE] {label}")
    if fix:
        print(f"         -> {fix}")
    return False


def skip(label, why):
    global SKIPS
    SKIPS += 1
    print(f"  [SKIP] {label}  ({why})")


def section(title):
    print(f"\n{title}\n" + "-" * len(title))


# ---------------------------------------------------------------- A

def probe_environment():
    section("A. Environment")
    if not sys.platform.startswith("linux"):
        skip("the whole probe", f"this is {sys.platform}; it only means anything on Linux")
        return False
    from src.uinput_interface import UINPUT_AVAILABLE
    from src.mouse_isolation import MOUSE_ISOLATION_AVAILABLE
    check("UINPUT_AVAILABLE", UINPUT_AVAILABLE)
    check("MOUSE_ISOLATION_AVAILABLE", MOUSE_ISOLATION_AVAILABLE)
    ENV["uinput"] = gate("/dev/uinput is writable", os.access("/dev/uinput", os.W_OK),
         "sudo modprobe uinput; install build_tools/linux/60-nimbus-uinput.rules, "
         "then: sudo udevadm control --reload; sudo udevadm trigger --name-match=uinput")
    readable = [p for p in Path("/dev/input").glob("event*") if os.access(p, os.R_OK)]
    ENV["readable"] = gate("at least one /dev/input/event* is readable", bool(readable),
         "sudo usermod -aG input \"$USER\", then log out of the graphical session "
         "entirely and back in; a new shell is not enough. Confirm with: id -nG")
    print(f"    python {sys.version.split()[0]}, uid {os.getuid()}")
    return True


# ---------------------------------------------------------------- B

def probe_output_backends():
    section("B. Output back ends built through ControllerOutput (#16)")
    from src.uinput_interface import (UINPUT_AVAILABLE, UInputXboxInterface,
                                      UInputJoystickInterface)
    from src.controller_output import ControllerOutput

    class Cfg:
        def get_layout_type(self):
            return "xbox"

        def get(self, key, default=None):
            return default

        def set(self, key, value):
            pass

    # Exactly the shape src/bridge.py constructs on this platform.
    out = ControllerOutput(Cfg(),
                           UInputJoystickInterface if UINPUT_AVAILABLE else None,
                           UInputXboxInterface if UINPUT_AVAILABLE else None,
                           UINPUT_AVAILABLE)
    out.initialize()
    active = out.active
    check("xbox profile selects the uinput pad", isinstance(active, UInputXboxInterface),
          type(active).__name__)
    check("the pad reports connected", getattr(active, "is_connected", False))
    node = getattr(getattr(active, "device", None), "event_node", None)
    check("the pad has a kernel event node", bool(node), str(node))

    from src.mouse_isolation import list_input_devices
    names = [d["name"] for d in list_input_devices()]
    check("the pad is visible to the kernel as an Xbox 360 pad",
          any("X-Box 360" in n or "Xbox 360" in n for n in names),
          next((n for n in names if "360" in n), "not found"))

    out.select("vjoy")
    check("switching to vjoy selects the uinput joystick",
          isinstance(out.active, UInputJoystickInterface), type(out.active).__name__)
    for iface in (out.vjoy, out.vigem):
        if iface is not None:
            try:
                iface.shutdown()
            except Exception:
                pass
    return True


# ---------------------------------------------------------------- C

def _wait_for_readable(node, timeout=2.0):
    """Wait for udev to finish granting read access to a just-created node.

    The kernel publishes the event node as soon as ``UI_DEV_CREATE`` returns,
    but the ``uaccess`` ACL is applied afterwards by udev processing the event.
    On this machine that lands ~50 ms later, so a read opened immediately after
    device creation loses a race it looks like a permission failure.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.access(node, os.R_OK):
            return True
        time.sleep(0.01)
    return False


def _open_abs_reader(node):
    """Open one evdev client on ``node``, to be held across a whole sequence.

    An evdev client only receives events generated after it opens the node, so
    a reader opened per step sees nothing the step just emitted. Raises
    ``OSError`` if the node cannot be opened: that is a different finding from
    a device that reported no events, and collapsing the two reports a
    permission problem as ``ABS_X = None``.
    """
    return os.open(node, os.O_RDONLY | os.O_NONBLOCK)


def _read_last_abs(fd, timeout=0.15):
    """Drain what an open reader has buffered, returning the last ABS values."""
    from src.uinput_interface import _INPUT_EVENT
    EV_ABS = 0x03
    seen = {}
    size = _INPUT_EVENT.size
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = os.read(fd, size * 64)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        if not data:
            break
        for i in range(0, len(data) - size + 1, size):
            _s, _us, etype, code, value = _INPUT_EVENT.unpack(data[i:i + size])
            if etype == EV_ABS:
                seen[code] = value
    return seen


def probe_pulse_cannot_undo_a_release():
    section("C. The keep-alive pulse cannot undo a release, read from the kernel (#15)")
    from src.uinput_interface import UInputXboxInterface
    from src import controller_pulse
    ABS_X = 0x00

    class Cfg:
        def get(self, key, default=None):
            return default

        def set(self, key, value):
            pass

    pad = UInputXboxInterface(Cfg())
    if not getattr(pad, "is_connected", False):
        skip("pulse checks", "the pad did not open; see section A")
        return False
    node = pad.device.event_node
    fd = None
    try:
        if not _wait_for_readable(node):
            skip("pulse checks", f"udev did not grant read access to {node}; see section A")
            return False
        fd = _open_abs_reader(node)
        for name in ("emit_left_stick_transient", "restore_left_stick", "pulse_left_stick"):
            check(f"the pad exposes {name}", hasattr(pad, name))

        pad.set_left_stick(0.6, 0.0)
        time.sleep(0.05)
        _read_last_abs(fd)                        # drain

        pad.pulse_left_stick(0.08, 0.0)
        time.sleep(0.05)
        after = _read_last_abs(fd)
        check("a pulse leaves the commanded value untouched",
              abs(pad.current_values["left_x"] - 0.6) < 1e-6,
              f"current_values left_x = {pad.current_values['left_x']}")
        check("the kernel's last position after a pulse is the commanded one",
              after.get(ABS_X) == pad._stick_raw(0.6),
              f"kernel {after.get(ABS_X)} vs commanded {pad._stick_raw(0.6)}")

        # The failing interleaving: release, then let an in-flight pulse finish.
        pad.set_left_stick(0.0, 0.0)
        pad.pulse_left_stick(0.08, 0.0)
        time.sleep(0.05)
        after = _read_last_abs(fd)
        check("after a release, a later pulse leaves the stick centred",
              after.get(ABS_X) == 0, f"kernel ABS_X = {after.get(ABS_X)}")

        # And the same through the burst the pulse thread sends at start-up.
        pad.set_left_stick(0.7, 0.0)
        pad.set_left_stick(0.0, 0.0)              # the user lets go
        controller_pulse._send_burst(pad, count=2, delay=0)
        time.sleep(0.05)
        after = _read_last_abs(fd)
        check("a burst after a release re-centres rather than restoring the old value",
              after.get(ABS_X) == 0, f"kernel ABS_X = {after.get(ABS_X)}")
    finally:
        if fd is not None:
            os.close(fd)
        try:
            pad.shutdown()
        except Exception:
            pass
    return True


# ---------------------------------------------------------------- D

def probe_pointer_classification():
    section("D. Pointer classification against this machine's devices (#15)")
    from src.mouse_isolation import list_input_devices, list_pointer_devices, pointer_support

    devices = list_input_devices()
    check("/proc/bus/input/devices parsed", bool(devices), f"{len(devices)} devices")
    pointers = list_pointer_devices()
    print(f"\n    {'device':<40} {'rel':<5} {'abs':<5} {'grabbable':<10} reason")
    for d in pointers:
        ok, why = pointer_support(d)
        print(f"    {d['name'][:38]:<40} {str(d.get('has_rel')):<5} "
              f"{str(d.get('has_abs')):<5} {str(ok):<10} {why}")
    print()

    supported = [d for d in pointers if pointer_support(d)[0]]
    gate("at least one pointer is grabbable", bool(supported),
         "plug in a USB mouse. A touchpad-only machine has nothing this reader can "
         "translate, and declining it is the classifier working, not failing")

    absolute = [d for d in pointers if d.get("has_abs") and not d.get("has_rel")]
    if absolute:
        every = all(not pointer_support(d)[0] for d in absolute)
        check("every absolute-only pointer is declined", every,
              ", ".join(d["name"] for d in absolute))
        check("the refusal explains itself",
              all("freeze the pointer" in pointer_support(d)[1] for d in absolute))
    else:
        skip("absolute-pointer refusal", "no absolute-only pointer on this machine; "
             "a laptop touchpad is the case this protects")
    return True


# ---------------------------------------------------------------- E

def probe_passthrough_leak(do_grab):
    section("E. No pass-through keyboard is left behind (#17)")
    from src.mouse_isolation import list_input_devices

    def strays():
        return [d["name"] for d in list_input_devices() if "(Nimbus passthrough)" in d["name"]]

    before = strays()
    check("no stray pass-through devices before starting", not before, ", ".join(before))

    if not do_grab:
        skip("forced grab-failure cleanup", "pass --grab to exercise it")
        return True
    if not (ENV.get("uinput") and ENV.get("readable")):
        skip("forced grab-failure cleanup", "gate unmet: it needs /dev/uinput writable and event nodes readable")
        return True

    from src.mouse_isolation import MouseIsolation, list_pointer_devices, pointer_support
    target = next((d for d in list_pointer_devices() if pointer_support(d)[0]), None)
    if target is None:
        skip("forced grab-failure cleanup", "no grabbable pointer")
        return True

    noop_motion, noop_button = (lambda dx, dy: None), (lambda code, pressed: None)
    first = MouseIsolation(noop_motion, noop_button)
    second = MouseIsolation(noop_motion, noop_button)
    try:
        first.start(nodes=[target["node"]])
        time.sleep(0.2)
        failed = False
        try:
            second.start(nodes=[target["node"]])   # the kernel refuses a second grab
        except Exception:
            failed = True
        check("a second grab of the same device fails", failed)
    finally:
        for inst in (second, first):
            try:
                inst.stop("probe")
            except Exception:
                pass
        time.sleep(0.3)
    after = strays()
    check("the failed attempt left no pass-through keyboard behind", not after,
          ", ".join(after))
    return True


# ---------------------------------------------------------------- F

def probe_bridge_dispatch():
    section("F. The bridge dispatches to the software cursor here (#18)")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from src import bridge as bridge_module
    except Exception as exc:
        skip("bridge dispatch", f"bridge did not import: {exc}")
        return True
    check("_ISO_CURSOR_RELAY is False on Linux", bridge_module._ISO_CURSOR_RELAY is False)
    C = bridge_module.ControllerBridge
    for stem in ("_on_iso_motion", "_on_iso_button", "_on_iso_wheel",
                 "_on_iso_stopped", "_iso_send_mouse"):
        check(f"{stem} has both implementations and one dispatcher",
              hasattr(C, f"{stem}_relay") and hasattr(C, f"{stem}_sw") and hasattr(C, stem))
    import inspect
    body = inspect.getsource(C._on_iso_motion)
    check("the dispatcher is conditional, not hardwired",
          "_ISO_CURSOR_RELAY" in body and "_relay" in body and "_sw" in body)
    return True


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--grab", action="store_true",
                    help="also exercise the grab-failure cleanup path")
    args = ap.parse_args()

    print("Nimbus Linux stack probe")
    print("========================")
    if not probe_environment():
        print()
        print("Nothing to do: this probe only means anything on Linux.")
        return 0
    if ENV.get("uinput"):
        probe_output_backends()
        probe_pulse_cannot_undo_a_release()
    else:
        section("B and C. Output back ends and the pulse")
        skip("sections B and C", "gate unmet: /dev/uinput is not writable, so no pad can be created")
    probe_pointer_classification()
    probe_passthrough_leak(args.grab)
    probe_bridge_dispatch()

    print(f"\n{PASSES}/{PASSES + FAILS} checks passed, {SKIPS} skipped, "
          f"{len(GATES_UNMET)} environment gate(s) unmet")

    if GATES_UNMET:
        print("\nGates are properties of this machine, not of the code. Nothing below")
        print("is a regression; each one leaves part of the probe unable to run:")
        for label, fix in GATES_UNMET:
            print(f"  - {label}")
            if fix:
                print(f"      {fix}")

    if FAILS:
        print("\nA FAILED check is a real finding: these are claims about the code,")
        print("checked against a real kernel. Report the section and the printed")
        print("values. Do not confuse them with the gates above.")
    elif GATES_UNMET:
        print("\nEvery check that could run, passed. Close the gates and re-run to")
        print("cover the rest.")

    # An unmet gate is not a failure: it means less was checked, not that
    # something is broken. Only a failed check fails the run.
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
