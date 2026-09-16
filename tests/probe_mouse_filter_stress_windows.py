"""
Nimbus Mouse Filter battle test (throwaway, not Nimbus code).

Adversarial, unattended stress of the kernel filter in ``driver/`` through the
same user-mode surface Nimbus uses (``src/mouse_isolation_win.py``). It goes
beyond ``probe_mouse_filter_windows.py`` (U1 to U17, which it imports for its
helpers) with storms, floods, process chaos, CPU starvation, a fuzz of every
file API the control device can receive, and a long soak. Nothing here needs a
hand on the mouse, nor an elevated prompt. The physical mouse is isolated in
short stretches and for the soak; a remote-control pointer is unaffected
because it enters above the filter.

The one invariant every check comes back to: **no client, no isolation**. A
fresh open must never find isolation already on, and once every handle is
gone (closed, killed, or inherited and released) the driver must report
``isolating 0`` with no read parked.

  B1   IOCTL and read storm on one shared handle (toggles, status, reads,
       CancelIoEx from ten threads): no unexpected error, nothing stuck
  B2   512 reads parked: SET_ISOLATION(0) fails them all at once, CloseHandle
       drains them; and the heartbeat's cost with N parked reads and a frozen
       client (release lands about 1 s + N x 250 ms in, measured for N=1, 8, 24)
  B3   open/close storm from 8 threads, half of them closing while isolating
       with a read parked: a fresh open never sees isolation on
  B4   chaos kills: a client terminated at a random point of its start-up,
       30 times: the device is free and pass-through within a second each time
  B5   suspend precision: a frozen client keeps the mouse for stalls under 2 s
       (1.2, 1.5, 1.7, 1.9 s) and loses it for a 2.4 s stall
  B6   CPU starvation: cores + 8 busy processes for 10 s at NORMAL and at HIGH
       priority against a NORMAL client, plus a client whose reader thread runs
       at THREAD_PRIORITY_TIME_CRITICAL; does the heartbeat survive a busy game?
  B7   inherited handle: a client that leaks its handle into a child and dies;
       the child never reads, so the watchdog must release within 2.25 s and the
       device must be free once the child exits. Also: the production client's
       handle is not inheritable
  B8   fuzz: 4000 random IOCTL codes (all methods, device types including
       FILE_DEVICE_FILE_SYSTEM which routes through NtFsControlFile), invalid
       user pointers, NtReadFile edge cases, every other IRP major the device
       can receive (query/set information, volume information, flush, lock,
       directory, EA, security), 40 open variants, and a DuplicateHandle storm
  B9   soak (``--soak``, default 120 s): the production client isolating with
       nobody moving the mouse; ticks about once a second, no watchdog release
  B11  multi-process churn with kills: six client processes open, isolate, park,
       close in a loop for 12 s while one of them is terminated every 400 ms and
       respawned, and a monitor checks the invariant at every free moment
  B12  synchronous reads: CancelSynchronousIo, CloseHandle from another thread,
       TerminateThread on the reading thread (watchdog must then release), and
       an overlapped read whose thread exits (cancelled, isolation continues)
  B13  the input switched to a private desktop (the unattended stand-in for the
       lock screen and UAC): the client pauses isolation within the poll
       interval, keeps the device, and resumes when the desktop comes back

Run::

    venv\\Scripts\\python tests\\probe_mouse_filter_stress_windows.py
    venv\\Scripts\\python tests\\probe_mouse_filter_stress_windows.py --only B1,B8
    venv\\Scripts\\python tests\\probe_mouse_filter_stress_windows.py --soak 600
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import io
import os
import queue
import random
import subprocess
import sys
import threading
import time
import ctypes
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

if sys.platform != "win32":
    sys.exit("Windows only")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from src import mouse_isolation_win as iso  # noqa: E402
import probe_mouse_filter_windows as base  # noqa: E402
from probe_mouse_filter_windows import (  # noqa: E402
    _Child, _Read, _handle_count, _parse_kv, _wait_device_free, ERROR_IO_INCOMPLETE, PACKET, WAIT_OBJECT_0)

_k32 = iso._k32
_u32 = iso._u32
_ntdll = base._ntdll
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_u32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p]
_u32.CreateDesktopW.restype = wintypes.HANDLE
_u32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_u32.OpenDesktopW.restype = wintypes.HANDLE
_u32.SwitchDesktop.argtypes = [wintypes.HANDLE]
_u32.SwitchDesktop.restype = wintypes.BOOL
DESKTOP_SWITCHDESKTOP = 0x0100
GENERIC_ALL = 0x10000000

ERROR_INVALID_FUNCTION = 1
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_NOT_READY = iso.ERROR_NOT_READY            # 21
ERROR_INVALID_PARAMETER = 87
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_OPERATION_ABORTED = 995
ERROR_IO_PENDING = iso.ERROR_IO_PENDING          # 997
ERROR_NOACCESS = 998
STATUS_PENDING = 0x103
IOCTL_SET = iso.IOCTL_NIMBUS_SET_ISOLATION
IOCTL_GET = iso.IOCTL_NIMBUS_GET_STATUS
NORMAL_PRIORITY_CLASS = 0x20
IDLE_PRIORITY_CLASS = 0x40
HIGH_PRIORITY_CLASS = 0x80
THREAD_PRIORITY_TIME_CRITICAL = 15

_k32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
_k32.CancelSynchronousIo.restype = wintypes.BOOL
_k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenThread.restype = wintypes.HANDLE
_k32.TerminateThread.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.TerminateThread.restype = wintypes.BOOL
_k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.SetPriorityClass.restype = wintypes.BOOL
_k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
_k32.SetThreadPriority.restype = wintypes.BOOL
_k32.GetThreadPriority.argtypes = [wintypes.HANDLE]
_k32.GetThreadPriority.restype = ctypes.c_int
_k32.GetCurrentThread.restype = wintypes.HANDLE
_k32.GetCurrentThreadId.restype = wintypes.DWORD
_k32.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.GetHandleInformation.restype = wintypes.BOOL
_k32.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
                                 wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.DuplicateHandle.restype = wintypes.BOOL
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_k32.Process32FirstW.restype = wintypes.BOOL
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_k32.Process32NextW.restype = wintypes.BOOL
_k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
_k32.VirtualAlloc.restype = ctypes.c_void_p
_k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
_k32.VirtualFree.restype = wintypes.BOOL
_k32.ReadFileEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
_k32.ReadFileEx.restype = wintypes.BOOL
_k32.SleepEx.argtypes = [wintypes.DWORD, wintypes.BOOL]
_k32.SleepEx.restype = wintypes.DWORD
_psapi.GetPerformanceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_psapi.GetPerformanceInfo.restype = wintypes.BOOL

NTSTATUS = ctypes.c_uint32
for _name, _args in {
    "NtReadFile": [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p],
    "NtDeviceIoControlFile": [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                              wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG],
    "NtFsControlFile": [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                        wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG],
    "NtQueryInformationFile": [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, ctypes.c_int],
    "NtSetInformationFile": [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, ctypes.c_int],
    "NtQueryVolumeInformationFile": [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, ctypes.c_int],
    "NtFlushBuffersFile": [wintypes.HANDLE, ctypes.c_void_p],
    "NtLockFile": [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, wintypes.BOOLEAN, wintypes.BOOLEAN],
    "NtQueryDirectoryFile": [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.ULONG, ctypes.c_int, wintypes.BOOLEAN, ctypes.c_void_p,
                             wintypes.BOOLEAN],
    "NtQueryEaFile": [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, wintypes.BOOLEAN,
                      ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.BOOLEAN],
    "NtQuerySecurityObject": [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p],
    "NtCreateFile": [ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                     wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG],
    "NtClose": [wintypes.HANDLE],
    "NtWaitForSingleObject": [wintypes.HANDLE, wintypes.BOOLEAN, ctypes.c_void_p],
}.items():
    fn = getattr(_ntdll, _name)
    fn.argtypes = _args
    fn.restype = NTSTATUS
_ntdll.RtlInitUnicodeString.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_ntdll.RtlInitUnicodeString.restype = None


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_int32), ("_pad", ctypes.c_int32), ("Information", ctypes.c_size_t)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE), ("ObjectName", ctypes.c_void_p),
                ("Attributes", wintypes.ULONG), ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable", "SystemCache",
        "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
        ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]


RESULTS: List[Dict[str, object]] = []
SPAWNED: List["Role"] = []
EXPECTED_VERSION_DEFAULT = iso.INTERFACE_VERSION


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append({"check": name, "ok": ok, "note": note})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {note}", flush=True)


def note(text: str) -> None:
    print(f"      {text}", flush=True)


def kernel_pool() -> Tuple[int, int]:
    """(nonpaged bytes, paged bytes) system-wide, from GetPerformanceInfo."""
    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    if not _psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        return 0, 0
    return pi.KernelNonpaged * pi.PageSize, pi.KernelPaged * pi.PageSize


def driver_pool() -> str:
    """Live pool allocations under the driver's own tag.

    KMDF tags everything it allocates on a driver's behalf (requests, queues,
    contexts) with the first four letters of the service name, ``nimb`` here,
    so ``SystemPoolTagInformation`` (no privilege needed) gives a per-driver
    leak count that the system-wide pool numbers cannot.
    """
    import struct
    size = 1 << 20
    while True:
        buf = ctypes.create_string_buffer(size)
        ret = wintypes.ULONG(0)
        st = _ntdll.NtQuerySystemInformation(22, buf, size, ctypes.byref(ret))
        if st == 0xC0000004:
            size *= 2
            continue
        break
    if st != 0:
        return f"pool tags unavailable ({st:#x})"
    count = struct.unpack_from("<I", buf, 0)[0]
    off = 8
    for _ in range(count):
        tag, pa, pf, pu, na, nf, nu = struct.unpack_from("<4sII4xQIIQ", buf, off)
        off += 40
        if tag == b"nimb":
            return f"tag nimb: nonpaged live={na - nf} ({nu} bytes, {na} allocs), paged live={pa - pf} ({pu} bytes)"
    return "tag nimb: not present"


def status_of(handle: int) -> Dict[str, int]:
    return iso._query_status(handle)


def set_iso(handle: int, on: bool) -> None:
    iso._set_isolation(handle, on)


def children_of(pid: int) -> List[int]:
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
    out: List[int] = []
    if not snap or snap == iso.INVALID_HANDLE_VALUE:
        return out
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.th32ParentProcessID == pid:
                out.append(pe.th32ProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        _k32.CloseHandle(snap)
    return out


def terminate_pid(pid: int) -> bool:
    h = _k32.OpenProcess(0x0001, False, pid)
    if not h:
        return False
    try:
        return bool(_k32.TerminateProcess(h, 1))
    finally:
        _k32.CloseHandle(h)


def terminate_tree(pid: int) -> int:
    """TerminateProcess on pid's children (the venv launcher's interpreter), then pid."""
    n = 0
    for child_pid in children_of(pid):
        n += terminate_tree(child_pid)
    if terminate_pid(pid):
        n += 1
    return n


class Role:
    """This script re-run with ``--role``; its stdout lines land in a queue.

    ``Popen`` returns the venv launcher, not the interpreter (see ``_Child`` in
    the base probe), so roles print ``READY pid=<interpreter pid>`` and kills
    go by pid, then to the launcher.
    """

    def __init__(self, role: str, *extra: str, inherit: bool = False) -> None:
        cmd = [sys.executable, os.path.abspath(__file__), "--role", role, *extra]
        if iso.INTERFACE_VERSION != EXPECTED_VERSION_DEFAULT:
            cmd += ["--expect-version", str(iso.INTERFACE_VERSION)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1, close_fds=not inherit)
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self.seen: List[str] = []
        self.pid = 0
        self.role = role
        self.t_spawn = time.time()
        SPAWNED.append(self)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.lines.put(line.rstrip())
        except (OSError, ValueError):
            pass
        self.lines.put(None)

    def expect(self, prefix: str, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            self.seen.append(line)
            if line.startswith("READY"):
                self.pid = int(_parse_kv(line).get("pid", "0"))
            if line.startswith(prefix):
                return line

    def drain(self, timeout: float) -> List[str]:
        """Collect every line until the pipe closes or ``timeout`` passes."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                break
            self.seen.append(line)
            if line.startswith("READY"):
                self.pid = int(_parse_kv(line).get("pid", "0"))
        return self.seen

    def flush(self) -> None:
        """Move every line already queued into ``seen`` without waiting."""
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                return
            if line is None:
                self.lines.put(None)
                return
            self.seen.append(line)
            if line.startswith("READY"):
                self.pid = int(_parse_kv(line).get("pid", "0"))

    def send(self, command: str) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()
        except OSError:
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def process_handle(self) -> int:
        """A handle to the interpreter with SUSPEND_RESUME and QUERY_LIMITED rights."""
        if not self.pid:
            raise RuntimeError("child pid unknown (no READY line yet)")
        h = _k32.OpenProcess(0x0800 | 0x1000 | 0x0001, False, self.pid)
        if not h:
            raise RuntimeError(f"OpenProcess({self.pid}) failed: {ctypes.get_last_error()}")
        return h

    def kill(self) -> int:
        """TerminateProcess on the interpreter (by pid or by tree), then the launcher."""
        n = 0
        if self.pid:
            n += 1 if terminate_pid(self.pid) else 0
        else:
            n += terminate_tree(self.proc.pid)
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return n

    def close(self) -> None:
        if self.alive():
            self.kill()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        if self in SPAWNED:
            SPAWNED.remove(self)


# ---- roles (child processes) -------------------------------------------------

def role_client(reader_priority: str) -> int:
    """The production client, answering ``status`` and ``quit`` on stdin.

    ``reader_priority`` "default" leaves the module's reader-thread priority
    alone; "normal" forces it back to THREAD_PRIORITY_NORMAL as the control
    case for B6. A watcher thread prints ``TICK`` lines as heartbeat ticks
    arrive, so the parent knows when the parked read was last re-issued.
    """
    def on_stopped(reason: str) -> None:
        print(f"STOPPED {reason.replace(' ', '_')} t={time.time():.3f}", flush=True)

    m = iso.MouseIsolation(lambda dx, dy: None, lambda c, p: None, on_stopped=on_stopped)
    with contextlib.redirect_stdout(io.StringIO()):
        m.start()
    tid = m._thread.native_id if m._thread else 0
    priority = None
    if tid:
        th = _k32.OpenThread(0x20 | 0x40, False, tid)
        if th:
            if reader_priority == "normal":
                _k32.SetThreadPriority(th, 0)
            priority = _k32.GetThreadPriority(th)
            _k32.CloseHandle(th)
    st = m.status()
    print(f"READY pid={os.getpid()} tid={tid} reader_priority={priority} isolating={st['isolating']} "
          f"pending_reads={st['pending_reads']} watchdog_releases={st['watchdog_releases']} t={time.time():.3f}",
          flush=True)

    def tick_watch() -> None:
        last = m.ticks
        while m.active:
            if m.ticks != last:
                last = m.ticks
                print(f"TICK n={last} t={time.time():.3f}", flush=True)
            time.sleep(0.004)

    threading.Thread(target=tick_watch, daemon=True).start()
    for line in sys.stdin:
        command = line.strip()
        if command == "status":
            try:
                st = m.status()
                print(f"STATUS active={m.active} isolating={st['isolating']} pending_reads={st['pending_reads']} "
                      f"watchdog_releases={st['watchdog_releases']} ticks={m.ticks} "
                      f"stop_reason={m.stop_reason.replace(' ', '_') or '-'} t={time.time():.3f}", flush=True)
            except RuntimeError as exc:
                print(f"STATUS active={m.active} error={str(exc).replace(' ', '_')} ticks={m.ticks} "
                      f"stop_reason={m.stop_reason.replace(' ', '_') or '-'} t={time.time():.3f}", flush=True)
        elif command == "quit":
            break
    with contextlib.redirect_stdout(io.StringIO()):
        m.stop("client quit")
    return 0


def role_burn(deadline: float, priority: str) -> int:
    """One busy core until the wall-clock deadline, at the given priority class."""
    cls = {"normal": NORMAL_PRIORITY_CLASS, "high": HIGH_PRIORITY_CLASS, "idle": IDLE_PRIORITY_CLASS}[priority]
    _k32.SetPriorityClass(_k32.GetCurrentProcess(), cls)
    x = 0
    while time.time() < deadline:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
    return 0


def role_churn(deadline: float, seed: int) -> int:
    """Open, check the invariant, isolate, maybe park a read, close, until the deadline."""
    rnd = random.Random(seed)
    print(f"READY pid={os.getpid()} t={time.time():.3f}", flush=True)
    opens = busy = violations = 0
    while time.time() < deadline:
        try:
            h = iso._open_device()
        except iso.DriverBusyError:
            busy += 1
            time.sleep(rnd.random() * 0.002)
            continue
        opens += 1
        rd = None
        try:
            st = status_of(h)
            if st["isolating"] != 0 or st["pending_reads"] != 0:
                violations += 1
                print(f"VIOLATION pid={os.getpid()} fresh open found isolating={st['isolating']} "
                      f"pending_reads={st['pending_reads']} t={time.time():.3f}", flush=True)
            set_iso(h, True)
            if rnd.random() < 0.5:
                rd = _Read(h, PACKET)
            time.sleep(rnd.random() * 0.003)
            if rnd.random() < 0.5:
                set_iso(h, False)
        except RuntimeError as exc:
            print(f"ERROR pid={os.getpid()} {str(exc).replace(' ', '_')}", flush=True)
        finally:
            _k32.CloseHandle(h)
            if rd is not None:
                rd.wait(1000)
                rd.close()
    print(f"CHURN pid={os.getpid()} opens={opens} busy={busy} violations={violations} t={time.time():.3f}", flush=True)
    return 0


def role_inherit_parent(seconds: float) -> int:
    """Open inheritably, isolate, hand the handle to a child, and die without reading."""
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    h = _k32.CreateFileW(iso.DEVICE_PATH, iso.GENERIC_READ | iso.GENERIC_WRITE,
                         iso.FILE_SHARE_READ | iso.FILE_SHARE_WRITE, ctypes.byref(sa),
                         iso.OPEN_EXISTING, iso.FILE_FLAG_OVERLAPPED, None)
    if h == iso.INVALID_HANDLE_VALUE or h is None:
        print(f"ERROR open failed {ctypes.get_last_error()}", flush=True)
        return 1
    set_iso(h, True)
    t_on = time.time()
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--role", "inherit-child",
                              "--handle", str(h), "--seconds", str(seconds)], close_fds=False)
    print(f"READY pid={os.getpid()} handle={h} isolating=1 child={child.pid} t={t_on:.3f}", flush=True)
    time.sleep(0.5)
    print(f"PARENT exiting t={time.time():.3f}", flush=True)
    sys.stdout.flush()
    os._exit(0)


def role_inherit_child(handle: int, seconds: float) -> int:
    """Poll GET_STATUS through the inherited handle; never read. Report the release."""
    print(f"CHILD pid={os.getpid()} handle={handle} t={time.time():.3f}", flush=True)
    end = time.time() + seconds
    last: Optional[int] = None
    while time.time() < end:
        try:
            st = status_of(handle)
        except RuntimeError as exc:
            print(f"CHILD error {str(exc).replace(' ', '_')} t={time.time():.3f}", flush=True)
            break
        if st["isolating"] != last:
            last = st["isolating"]
            print(f"CHILD isolating={st['isolating']} watchdog_releases={st['watchdog_releases']} "
                  f"t={time.time():.3f}", flush=True)
        time.sleep(0.025)
    print(f"CHILD exiting t={time.time():.3f}", flush=True)
    return 0


def role_sync_terminate() -> int:
    """Block a thread in a synchronous ReadFile, TerminateThread it, watch the watchdog."""
    h = _k32.CreateFileW(iso.DEVICE_PATH, iso.GENERIC_READ | iso.GENERIC_WRITE,
                         iso.FILE_SHARE_READ | iso.FILE_SHARE_WRITE, None, iso.OPEN_EXISTING, 0, None)
    if h == iso.INVALID_HANDLE_VALUE or h is None:
        print(f"ERROR open failed {ctypes.get_last_error()}", flush=True)
        return 1
    set_iso(h, True)
    t_on = time.time()
    tid = wintypes.DWORD(0)
    outcome = {"err": None}

    def blocked_read() -> None:
        tid.value = _k32.GetCurrentThreadId()
        buf = ctypes.create_string_buffer(PACKET)
        n = wintypes.DWORD(0)
        outcome["t_read"] = time.time()
        ok = _k32.ReadFile(h, buf, PACKET, ctypes.byref(n), None)
        outcome["err"] = 0 if ok else ctypes.get_last_error()

    th = threading.Thread(target=blocked_read, daemon=True)
    th.start()
    time.sleep(min(0.3, iso.TICK_MS / 1000.0 * 0.4))   # before the heartbeat completes the read
    t_read = float(outcome.get("t_read", t_on))
    hthread = _k32.OpenThread(0x0001 | 0x0002, False, tid.value)   # TERMINATE | SUSPEND_RESUME
    killed = bool(_k32.TerminateThread(hthread, 0)) if hthread else False
    t_kill = time.time()
    print(f"READY pid={os.getpid()} tid={tid.value} terminated={killed} t={t_kill:.3f}", flush=True)
    end = time.time() + 4.0
    t_release = None
    releases = None
    while time.time() < end:
        try:
            st = status_of(h)   # the file object lock is free once the thread's IRP is cancelled
        except RuntimeError as exc:
            print(f"ERROR status {str(exc).replace(' ', '_')} t={time.time():.3f}", flush=True)
            break
        if st["isolating"] == 0:
            t_release = time.time()
            releases = st["watchdog_releases"]
            break
        time.sleep(0.02)
    print(f"RESULT read_err={outcome['err']} t_read={t_read:.3f} t_kill={t_kill:.3f} "
          f"t_release={t_release if t_release is None else f'{t_release:.3f}'} watchdog_releases={releases} "
          f"t={time.time():.3f}", flush=True)
    _k32.CloseHandle(h)
    sys.stdout.flush()
    os._exit(0)


# ---- helpers used by the checks ------------------------------------------------

def bounded_ioctl(handle: int, code: int, payload: Optional[bytes], out_size: int, ms: int = 2000) -> Tuple[int, int]:
    """DeviceIoControl that never waits longer than ``ms``. Returns (error, bytes returned)."""
    in_buf = ctypes.create_string_buffer(payload, len(payload)) if payload else None
    out_buf = ctypes.create_string_buffer(out_size) if out_size else None
    returned = wintypes.DWORD(0)
    event = _k32.CreateEventW(None, True, False, None)
    ov = iso._OVERLAPPED()
    ov.hEvent = event
    try:
        ok = _k32.DeviceIoControl(handle, code, in_buf, len(payload) if payload else 0,
                                  out_buf, out_size, ctypes.byref(returned), ctypes.byref(ov))
        err = 0 if ok else ctypes.get_last_error()
        if err == ERROR_IO_PENDING:
            if _k32.WaitForSingleObject(event, ms) != WAIT_OBJECT_0:
                _k32.CancelIoEx(handle, ctypes.byref(ov))
                _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(returned), True)
                return ERROR_IO_INCOMPLETE, 0
            ok = _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(returned), False)
            err = 0 if ok else ctypes.get_last_error()
        return err, returned.value
    finally:
        _k32.CloseHandle(event)


def raw_pointer_ioctl(handle: int, code: int, in_ptr: int, in_len: int, out_ptr: int, out_len: int) -> int:
    """DeviceIoControl with arbitrary (possibly invalid) buffer addresses."""
    returned = wintypes.DWORD(0)
    event = _k32.CreateEventW(None, True, False, None)
    ov = iso._OVERLAPPED()
    ov.hEvent = event
    try:
        ok = _k32.DeviceIoControl(handle, code, ctypes.c_void_p(in_ptr), in_len, ctypes.c_void_p(out_ptr), out_len,
                                  ctypes.byref(returned), ctypes.byref(ov))
        err = 0 if ok else ctypes.get_last_error()
        if err == ERROR_IO_PENDING:
            if _k32.WaitForSingleObject(event, 2000) != WAIT_OBJECT_0:
                _k32.CancelIoEx(handle, ctypes.byref(ov))
                _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(returned), True)
                return ERROR_IO_INCOMPLETE
            ok = _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(returned), False)
            err = 0 if ok else ctypes.get_last_error()
        return err
    finally:
        _k32.CloseHandle(event)


def nt_wait(handle: int, event: Optional[int], iosb: IO_STATUS_BLOCK, status: int, ms: int = 1000) -> int:
    """Resolve STATUS_PENDING by waiting on the event (or the file handle), cancelling on timeout."""
    if status != STATUS_PENDING:
        return status
    waited = _k32.WaitForSingleObject(event if event else handle, ms)
    if waited != WAIT_OBJECT_0:
        _k32.CancelIoEx(handle, None)
        _k32.WaitForSingleObject(event if event else handle, 1000)
    return iosb.Status & 0xFFFFFFFF


def nt_open(path: str, access: int, share: int, disposition: int, options: int) -> Tuple[int, int]:
    """NtCreateFile by NT path. Returns (status, handle or 0)."""
    us = UNICODE_STRING()
    wbuf = ctypes.create_unicode_buffer(path)
    us.Length = (len(path)) * 2
    us.MaximumLength = us.Length + 2
    us.Buffer = ctypes.cast(wbuf, ctypes.c_void_p)
    oa = OBJECT_ATTRIBUTES(ctypes.sizeof(OBJECT_ATTRIBUTES), None, ctypes.addressof(us), 0x40, None, None)
    iosb = IO_STATUS_BLOCK()
    h = wintypes.HANDLE(0)
    st = _ntdll.NtCreateFile(ctypes.byref(h), access, ctypes.byref(oa), ctypes.byref(iosb), None, 0x80,
                             share, disposition, options, None, 0)
    return st, (h.value or 0) if st == 0 else 0


def open_variant(path: str, access: int, share: int, disposition: int, flags: int) -> Tuple[Optional[int], int]:
    sa = None
    h = _k32.CreateFileW(path, access, share, sa, disposition, flags, None)
    if h == iso.INVALID_HANDLE_VALUE or h is None:
        return None, ctypes.get_last_error()
    return h, 0


def free_and_clean(timeout: float = 3.0) -> Tuple[Optional[Dict[str, int]], str]:
    st = _wait_device_free(timeout)
    if st is None:
        return None, "device still busy"
    return st, f"isolating={st['isolating']} pending_reads={st['pending_reads']}"


# ---- checks -----------------------------------------------------------------------

def check_ioctl_storm(seconds: float) -> None:
    name = "B1 IOCTL and read storm on one handle"
    h = iso._open_device()
    before = status_of(h)
    stop = threading.Event()
    unexpected: List[str] = []
    read_errors: collections.Counter = collections.Counter()
    counts: collections.Counter = collections.Counter()
    lock = threading.Lock()

    def toggler(seed: int) -> None:
        rnd = random.Random(seed)
        while not stop.is_set():
            op = rnd.random()
            try:
                if op < 0.4:
                    set_iso(h, True)
                    key = "on"
                elif op < 0.8:
                    set_iso(h, False)
                    key = "off"
                else:
                    status_of(h)
                    key = "status"
            except RuntimeError as exc:
                with lock:
                    unexpected.append(str(exc))
                continue
            with lock:
                counts[key] += 1

    def reader(seed: int) -> None:
        rnd = random.Random(seed)
        while not stop.is_set():
            rd = _Read(h, PACKET * rnd.choice((1, 4, 256)))
            err = rd.wait(rnd.randint(0, 40))
            if err == ERROR_IO_INCOMPLETE:
                rd.cancel()
                err = rd.wait(1000)
            rd.close()
            with lock:
                read_errors[err] += 1
                counts["reads"] += 1

    def canceller() -> None:
        while not stop.is_set():
            _k32.CancelIoEx(h, None)
            with lock:
                counts["cancel_all"] += 1
            time.sleep(0.002)

    threads = ([threading.Thread(target=toggler, args=(i,), daemon=True) for i in range(6)]
               + [threading.Thread(target=reader, args=(100 + i,), daemon=True) for i in range(3)]
               + [threading.Thread(target=canceller, daemon=True)])
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    stuck = sum(1 for t in threads if t.is_alive())
    elapsed = time.perf_counter() - t0
    set_iso(h, False)
    after = status_of(h)
    _k32.CloseHandle(h)
    st, clean = free_and_clean()
    allowed = {0, ERROR_NOT_READY, ERROR_OPERATION_ABORTED}
    bad_reads = {k: v for k, v in read_errors.items() if k not in allowed}
    ok = (not unexpected and not bad_reads and stuck == 0 and after["isolating"] == 0 and after["pending_reads"] == 0
          and st is not None and st["watchdog_releases"] == before["watchdog_releases"])
    record(name, ok, f"{elapsed:.1f} s, 10 threads: {dict(counts)}; read completions {dict(read_errors)} "
                     f"(21 not ready, 995 cancelled expected); unexpected={unexpected[:3]} stuck_threads={stuck}; "
                     f"after: isolating={after['isolating']} pending_reads={after['pending_reads']} "
                     f"watchdog_releases {before['watchdog_releases']} -> {st['watchdog_releases'] if st else '?'}; {clean}")


def check_read_flood() -> None:
    name = "B2 512 parked reads: release fails all, close drains all"
    h = iso._open_device()
    before = status_of(h)
    n = 512
    rounds: List[str] = []
    all_ok = True
    for _ in range(10):
        set_iso(h, True)
        reads = [_Read(h, PACKET) for _ in range(n)]
        pend = status_of(h)["pending_reads"]
        t0 = time.perf_counter()
        set_iso(h, False)
        errs = collections.Counter(rd.wait(2000) for rd in reads)
        dt = time.perf_counter() - t0
        for rd in reads:
            rd.close()
        st = status_of(h)
        round_ok = pend == n and errs == {ERROR_NOT_READY: n} and st["pending_reads"] == 0 and st["isolating"] == 0
        all_ok = all_ok and round_ok
        rounds.append(f"{pend}p/{dict(errs)}/{dt * 1000:.0f}ms")
    # close with the flood parked
    set_iso(h, True)
    reads = [_Read(h, PACKET) for _ in range(n)]
    pend_close = status_of(h)["pending_reads"]
    t0 = time.perf_counter()
    _k32.CloseHandle(h)
    t_close = time.perf_counter() - t0
    errs_close = collections.Counter(rd.wait(2000) for rd in reads)
    for rd in reads:
        rd.close()
    st, clean = free_and_clean()
    close_ok = (pend_close == n and set(errs_close) <= {ERROR_NOT_READY, ERROR_OPERATION_ABORTED}
                and sum(errs_close.values()) == n and st is not None and st["isolating"] == 0
                and st["pending_reads"] == 0)
    record(name, all_ok and close_ok,
           f"10 rounds of {n} parked reads then SET_ISOLATION(0): {rounds[0]} ... {rounds[-1]} (all rounds ok={all_ok}); "
           f"then {pend_close} parked and CloseHandle took {t_close * 1000:.1f} ms, completions {dict(errs_close)}; "
           f"{clean}, watchdog_releases {before['watchdog_releases']} -> {st['watchdog_releases'] if st else '?'}")

    # the heartbeat's cost with N parked reads and a frozen client. Before v5 the
    # tick retired one parked read per period and the watchdog needed the queue
    # empty first, so N parked reads cost N periods. Since v5 the watchdog runs on
    # idle time alone: the release lands at 2.0 to 2.25 s whatever N is, and every
    # read still parked fails with ERROR_NOT_READY.
    tick = iso.TICK_MS / 1000.0
    idle_only = iso.INTERFACE_VERSION >= 5
    if idle_only:
        name2 = "B2b watchdog release with N parked reads and no re-issue (2.0 to 2.25 s whatever N, v5)"
    else:
        name2 = f"B2b watchdog release with N parked reads and no re-issue (max(2 s, {tick:.2g} s + N x 250 ms) expected)"
    rows: List[str] = []
    ok2 = True
    for count in (1, 8, 24):
        h = iso._open_device()
        set_iso(h, True)
        reads = [_Read(h, PACKET) for _ in range(count)]
        t0 = time.perf_counter()
        t_release = None
        ticks = 0
        others: collections.Counter = collections.Counter()
        deadline = t0 + (2.0 if idle_only else 1.0 + count * 0.25) + 3.0
        while time.perf_counter() < deadline:
            st = status_of(h)
            if st["isolating"] == 0:
                t_release = time.perf_counter() - t0
                break
            time.sleep(0.03)
        for rd in reads:
            err = rd.wait(0)
            if err == 0 and rd.returned.value == 0:
                ticks += 1
            else:
                others[err] += 1   # 21 expected; 996 would be a read left parked after the release
            rd.close()
        _k32.CloseHandle(h)
        if idle_only:
            # 2 s of idle sampled every 250 ms: 2.0 to 2.25 s, and nothing left parked.
            expected = 2.0
            settled = set(others) <= {ERROR_NOT_READY}
        else:
            # One tick per 250 ms period once idle > the tick length, and the release
            # needs the queue empty and idle > 2 s: max(2.0, tick + N x 0.25) plus one period.
            expected = max(2.0, tick + count * 0.25)
            settled = True
        rows.append(f"N={count}: release {t_release:.2f} s, {ticks} ticks, other completions {dict(others)}"
                    if t_release is not None else f"N={count}: NOT released within {deadline - t0:.1f} s")
        ok2 = ok2 and t_release is not None and expected - 0.1 <= t_release <= expected + 0.6 and settled
    st, clean = free_and_clean()
    tail = ("Parked reads no longer postpone the watchdog (v5): the release lands at 2.0 to 2.25 s whatever N is "
            "and fails every read still parked with 21."
            if idle_only else
            "Each parked read costs one 250 ms watchdog period before the release; Nimbus parks exactly one, "
            "so its bound stays 2.25 s.")
    record(name2, ok2 and st is not None, "; ".join(rows) + f". {tail} {clean}")


def check_open_close_storm(seconds: float) -> None:
    name = "B3 open/close storm from 8 threads, closing while isolating"
    st0 = iso.get_status()
    stop = threading.Event()
    lock = threading.Lock()
    counts: collections.Counter = collections.Counter()
    violations: List[str] = []
    errors: List[str] = []
    holders = [0]
    max_holders = [0]

    def worker(seed: int) -> None:
        rnd = random.Random(seed)
        while not stop.is_set():
            try:
                h = iso._open_device()
            except iso.DriverBusyError:
                with lock:
                    counts["busy"] += 1
                continue
            except Exception as exc:
                with lock:
                    errors.append(str(exc))
                continue
            with lock:
                holders[0] += 1
                max_holders[0] = max(max_holders[0], holders[0])
                counts["opens"] += 1
            rd = None
            try:
                st = status_of(h)
                if st["isolating"] != 0 or st["pending_reads"] != 0:
                    with lock:
                        violations.append(f"isolating={st['isolating']} pending={st['pending_reads']}")
                set_iso(h, True)
                if rnd.random() < 0.5:
                    rd = _Read(h, PACKET)
                    with lock:
                        counts["parked"] += 1
                time.sleep(rnd.random() * 0.003)
                if rnd.random() < 0.5:
                    set_iso(h, False)
                else:
                    with lock:
                        counts["closed_isolating"] += 1
            except RuntimeError as exc:
                with lock:
                    errors.append(str(exc))
            finally:
                with lock:
                    holders[0] -= 1
                _k32.CloseHandle(h)
                if rd is not None:
                    rd.wait(1000)
                    rd.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(8)]
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    stuck = sum(1 for t in threads if t.is_alive())
    st, clean = free_and_clean()
    ok = (not violations and not errors and stuck == 0 and max_holders[0] == 1 and st is not None
          and st["watchdog_releases"] == st0["watchdog_releases"])
    record(name, ok, f"{seconds:.0f} s: {dict(counts)}; max simultaneous holders={max_holders[0]}; "
                     f"fresh opens that found isolation on: {len(violations)} {violations[:3]}; errors={errors[:3]}; "
                     f"stuck_threads={stuck}; watchdog_releases {st0['watchdog_releases']} -> "
                     f"{st['watchdog_releases'] if st else '?'}; {clean}")


def check_chaos_kill(n: int) -> None:
    name = f"B4 chaos kills at random points of client start-up (x{n})"
    st0 = iso.get_status()
    rnd = random.Random(7)
    phases: collections.Counter = collections.Counter()
    failures: List[str] = []
    slowest = 0.0
    for i in range(n):
        delay = rnd.uniform(0.0, 0.8)
        ch = _Child()
        try:
            ready = ch.expect("READY", delay)
            if ready is not None:
                time.sleep(rnd.uniform(0.0, 0.05))
                phase = "after READY (isolating, read parked)"
                t0 = time.perf_counter()
                ch.kill()
            else:
                phase = "before READY"
                t0 = time.perf_counter()
                killed = terminate_tree(ch.proc.pid)
                ch.proc.kill()
                ch.proc.wait(timeout=5)
                phases[f"processes terminated pre-READY={killed}"] += 0   # informational bucket
            st = _wait_device_free(2.0)
            dt = time.perf_counter() - t0
        finally:
            ch.close()
        phases[phase] += 1
        slowest = max(slowest, dt)
        if st is None or st["isolating"] != 0 or st["pending_reads"] != 0:
            failures.append(f"#{i} {phase}: " + ("device busy 2 s" if st is None else
                                                 f"isolating={st['isolating']} pending={st['pending_reads']}"))
    st, clean = free_and_clean()
    ok = not failures and st is not None and st["watchdog_releases"] == st0["watchdog_releases"]
    record(name, ok, f"{dict(phases)}; device free and pass-through after every kill, slowest {slowest * 1000:.0f} ms; "
                     f"failures={failures[:3]}; watchdog_releases {st0['watchdog_releases']} -> "
                     f"{st['watchdog_releases'] if st else '?'} (handle cleanup, never the watchdog); {clean}")


def check_suspend_precision() -> None:
    """The watchdog counts from the last read arrival, and the client's parked
    read was issued right after the previous tick, so a frozen client is
    released when (age of the parked read + stall) passes 2 s: the stall a
    client survives is 2 s minus that age, between about 0.75 s (read ticked
    at 1.25 s) and 2 s. Each case sets the age deliberately.
    """
    name = "B5 stall tolerance: parked-read age + stall under 2 s keeps the mouse, over 2 s loses it"
    # The parked read is at most tick + one 250 ms period old, so the ages
    # tried stay under the tick length (1 s on v3, 250 ms on v4).
    tick = iso.TICK_MS / 1000.0
    a = min(0.8, tick * 0.6)
    b = min(0.8, tick * 0.9)
    plan = [(0.05, 1.80, True), (a, 1.90 - a, True), (b, 1.80 - b, True), (a, 2.40 - a, False), (0.05, 2.35, False)]
    rows: List[str] = []
    ok = True
    client: Optional[Role] = None
    phandle = 0
    try:
        for age, hold, keep in plan:
            if client is None:
                client = Role("client")
                if client.expect("READY", 15.0) is None:
                    record(name, False, "client never reported READY: " + " | ".join(client.seen[-3:]))
                    return
                phandle = client.process_handle()
            client.flush()
            tick_line = client.expect("TICK", 3.0)
            if tick_line is None:
                rows.append("no heartbeat tick within 3 s")
                ok = False
                break
            t_tick = float(_parse_kv(tick_line)["t"])
            delay = t_tick + age - time.time()
            if delay > 0:
                time.sleep(delay)
            # another tick may have landed meanwhile; the age is measured from the newest
            client.flush()
            newest = [ln for ln in client.seen if ln.startswith("TICK")]
            if newest:
                t_tick = float(_parse_kv(newest[-1])["t"])
            t_s = time.time()
            _ntdll.NtSuspendProcess(phandle)
            time.sleep(hold)
            _ntdll.NtResumeProcess(phandle)
            t_r = time.time()
            time.sleep(0.4)
            client.send("status")
            line = client.expect("STATUS", 5.0)
            kv = _parse_kv(line) if line else {}
            active = kv.get("active") == "True"
            case_ok = active == keep
            if not active:
                stopped = [ln for ln in client.seen if ln.startswith("STOPPED")]
                case_ok = case_ok and any("watchdog" in s for s in stopped)
            rows.append(f"age {t_s - t_tick:.2f} + stall {t_r - t_s:.2f} = idle {t_r - t_tick:.2f} s -> "
                        f"{'kept' if active else 'released'}{'' if case_ok else ' (unexpected)'}")
            ok = ok and case_ok
            if not active:
                client.send("quit")
                client.drain(3.0)
                _k32.CloseHandle(phandle)
                client.close()
                client = None
                _wait_device_free(3.0)
    finally:
        if client is not None:
            client.send("quit")
            client.drain(3.0)
            if phandle:
                _k32.CloseHandle(phandle)
            client.close()
    st, clean = free_and_clean()
    record(name, ok and st is not None, "; ".join(rows) + f". A client stalled longer than 2 s minus the age of "
           f"its parked read (up to {tick + 0.25:.2f} s with a {iso.TICK_MS} ms tick) loses the mouse; {clean}")


def check_cpu_starvation(seconds: float) -> None:
    cores = os.cpu_count() or 8
    burners = cores + 8
    # (label, burner priority class, reader priority, must keep the mouse)
    configs = [("burn NORMAL vs the production client", "normal", "default", True),
               ("burn HIGH vs the production client (reader at TIME_CRITICAL)", "high", "default", True),
               ("burn HIGH vs the reader forced back to NORMAL (control, expected to lose it)", "high", "normal", False)]
    for label, prio, reader_priority, must_keep in configs:
        name = f"B6 CPU starvation: {label}"
        client = Role("client", "--reader-priority", reader_priority)
        procs: List[subprocess.Popen] = []
        try:
            ready = client.expect("READY", 15.0)
            if ready is None:
                record(name, False, "client never reported READY: " + " | ".join(client.seen[-3:]))
                continue
            kv = _parse_kv(ready)
            before = int(kv["watchdog_releases"])
            deadline = time.time() + seconds + 1.5
            t0 = time.time()
            procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "--role", "burn",
                                       "--deadline", f"{deadline:.3f}", "--priority", prio],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                     for _ in range(burners)]
            for p in procs:
                p.wait(timeout=seconds + 30)
            burned = time.time() - t0
            time.sleep(0.3)
            client.send("status")
            line = client.expect("STATUS", 10.0)
            skv = _parse_kv(line) if line else {}
            stopped = [ln for ln in client.seen if ln.startswith("STOPPED")]
            active = skv.get("active") == "True"
            ticks = int(skv.get("ticks", "0"))
            client.send("quit")
            client.drain(5.0)
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
            client.close()
        st, clean = free_and_clean()
        kept = active and not stopped and st is not None and st["watchdog_releases"] == before
        lost = (not active) and any("watchdog" in s for s in stopped) and st is not None
        ok = kept if must_keep else lost
        t_stop = ""
        if stopped:
            t_stop = f", released {float(_parse_kv(stopped[0]).get('t', '0')) - t0:.2f} s into the burn"
        record(name, ok, f"{burners} busy processes ({prio}) on {cores} cores for {burned:.1f} s; reader thread "
                         f"priority {kv.get('reader_priority')}; afterwards active={active} ticks={ticks} (about "
                         f"{seconds:.0f} expected) stopped={[s.split(' t=')[0] for s in stopped]}{t_stop}; "
                         f"watchdog_releases {before} -> {st['watchdog_releases'] if st else '?'}; {clean}")


def check_inherited_handle() -> None:
    name = "B7 inherited handle: parent dies, child never reads, watchdog releases, device free on child exit"
    # first: the production client's handle must not be inheritable
    m = iso.MouseIsolation(lambda dx, dy: None, lambda c, p: None)
    with contextlib.redirect_stdout(io.StringIO()):
        m.start()
    flags = wintypes.DWORD(0)
    got = _k32.GetHandleInformation(m._handle, ctypes.byref(flags))
    inheritable = bool(flags.value & 0x1)
    with contextlib.redirect_stdout(io.StringIO()):
        m.stop("probe")
    st0 = iso.get_status()
    parent = Role("inherit-parent", "--seconds", "5", inherit=True)
    try:
        ready = parent.expect("READY", 15.0)
        if ready is None:
            record(name, False, "parent never reported READY: " + " | ".join(parent.seen[-3:]))
            return
        t_on = float(_parse_kv(ready)["t"])
        parent.proc.wait(timeout=10)                 # launcher exits with the parent interpreter
        t_dead = time.time()
        time.sleep(0.2)
        try:
            iso._open_device()
            busy_after_parent = False
        except iso.DriverBusyError:
            busy_after_parent = True
        except Exception:
            busy_after_parent = False
        lines = parent.drain(12.0)
    finally:
        parent.close()
    child_states = [_parse_kv(ln) for ln in lines if ln.startswith("CHILD isolating=")]
    t_release = None
    saw_on = False
    for kv in child_states:
        if kv.get("isolating") == "1":
            saw_on = True
        elif kv.get("isolating") == "0" and saw_on and t_release is None:
            t_release = float(kv["t"]) - t_on
    st, clean = free_and_clean(5.0)
    ok = (got and not inheritable and busy_after_parent and saw_on and t_release is not None
          and 1.9 <= t_release <= 2.4 and st is not None and st["watchdog_releases"] == st0["watchdog_releases"] + 1)
    record(name, ok, f"production handle inheritable={inheritable}; parent died {t_dead - t_on:.2f} s after "
                     f"isolating, device busy afterwards={busy_after_parent} (child holds it); child saw isolating "
                     f"on={saw_on}, off at {t_release if t_release is None else f'{t_release:.2f}'} s after "
                     f"SET_ISOLATION(1); watchdog_releases {st0['watchdog_releases']} -> "
                     f"{st['watchdog_releases'] if st else '?'}; {clean}")


def check_fuzz(n_codes: int) -> None:
    name = "B8 fuzz: IOCTL codes, bad pointers, NtReadFile edges, other IRP majors, open variants"
    findings: List[str] = []
    ours = {IOCTL_SET, IOCTL_GET}
    h = iso._open_device()
    st0 = status_of(h)
    rnd = random.Random(20260906)

    # (a) random control codes with valid buffers
    hist: collections.Counter = collections.Counter()
    t0 = time.perf_counter()
    for _ in range(n_codes):
        dev = rnd.choice((0x22, 0x22, 0x0F, 0x0B, 0x09, 0x2A, 0x00, rnd.randint(0, 0xFFFF)))
        code = (dev << 16) | (rnd.randint(0, 3) << 14) | (rnd.randint(0, 0xFFF) << 2) | rnd.randint(0, 3)
        if code in ours:
            continue
        insz = rnd.choice((0, 0, 1, 4, 8, 24, 64, 4096))
        outsz = rnd.choice((0, 0, 4, 32, 64, 4096, 65536))
        payload = bytes(rnd.getrandbits(8) for _ in range(insz)) if insz else None
        err, ret = bounded_ioctl(h, code, payload, outsz)
        hist[err] += 1
        if err == 0:
            findings.append(f"IOCTL {code:#010x} in={insz} out={outsz} succeeded, {ret} bytes")
        elif err == ERROR_IO_INCOMPLETE:
            findings.append(f"IOCTL {code:#010x} hung 2 s")
    fuzz_time = time.perf_counter() - t0
    note(f"(a) {n_codes} random control codes in {fuzz_time:.1f} s, errors {dict(hist)} "
         f"(1 invalid function expected; FILE_DEVICE_FILE_SYSTEM codes go through NtFsControlFile)")

    # (b) our codes and random ones with invalid user pointers
    ptr_hist: collections.Counter = collections.Counter()
    bad_ptrs = (0x1, 0xDEADBEEF, 0x7FFFFFFF0000, 0xFFFF800000000000, 0x10000)
    for code in (IOCTL_SET, IOCTL_GET) + tuple((0x22 << 16) | (rnd.randint(0, 0xFFF) << 2) | rnd.randint(0, 3)
                                               for _ in range(60)):
        for ptr in bad_ptrs:
            for in_len, out_len in ((4, 0), (0, 32), (4, 32), (4096, 4096)):
                err = raw_pointer_ioctl(h, code, ptr, in_len, ptr, out_len)
                ptr_hist[err] += 1
                if err == 0:
                    findings.append(f"IOCTL {code:#010x} with buffers at {ptr:#x} succeeded")
                elif err == ERROR_IO_INCOMPLETE:
                    findings.append(f"IOCTL {code:#010x} with buffers at {ptr:#x} hung")
    note(f"(b) invalid user pointers: errors {dict(ptr_hist)} (998 no access expected for buffered codes)")
    set_iso(h, False)

    # (c) NtReadFile edge cases
    read_rows: List[str] = []
    set_iso(h, True)
    for label, buf_ptr, length in (("buffer at 0x1", 0x1, PACKET), ("buffer at 0xDEADBEEF", 0xDEADBEEF, PACKET),
                                   ("length 0xFFFFFFFF", None, 0xFFFFFFFF), ("length 0x7FFFFFFF", None, 0x7FFFFFFF),
                                   ("kernel address", 0xFFFF800000000000, PACKET)):
        buf = ctypes.create_string_buffer(PACKET)
        ptr = buf_ptr if buf_ptr is not None else ctypes.addressof(buf)
        iosb = IO_STATUS_BLOCK()
        ev = _k32.CreateEventW(None, True, False, None)
        st = _ntdll.NtReadFile(h, ev, None, None, ctypes.byref(iosb), ctypes.c_void_p(ptr), length, ctypes.byref(ctypes.c_int64(0)), None)
        st = nt_wait(h, ev, iosb, st, 500)
        _k32.CloseHandle(ev)
        read_rows.append(f"{label}: {st:#010x}")
        if st == 0:
            findings.append(f"NtReadFile {label} succeeded")
    # a parked read whose buffer is freed before it completes
    mem = _k32.VirtualAlloc(None, 4096, 0x3000, 0x04)
    iosb = IO_STATUS_BLOCK()
    ev = _k32.CreateEventW(None, True, False, None)
    st = _ntdll.NtReadFile(h, ev, None, None, ctypes.byref(iosb), ctypes.c_void_p(mem), PACKET,
                           ctypes.byref(ctypes.c_int64(0)), None)
    parked = st == STATUS_PENDING
    _k32.VirtualFree(mem, 0, 0x8000)
    set_iso(h, False)                                  # completes the parked read
    st = nt_wait(h, ev, iosb, st, 1000)
    _k32.CloseHandle(ev)
    read_rows.append(f"buffer freed while parked (parked={parked}), then release: {st:#010x}")
    # ReadFileEx with an APC completion routine
    set_iso(h, True)
    apc_seen: List[Tuple[int, int]] = []
    APC = ctypes.WINFUNCTYPE(None, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)

    def apc(err: int, nbytes: int, ov: int) -> None:
        apc_seen.append((err, nbytes))

    cb = APC(apc)
    ov = iso._OVERLAPPED()
    buf = ctypes.create_string_buffer(PACKET)
    ok = _k32.ReadFileEx(h, buf, PACKET, ctypes.byref(ov), cb)
    set_iso(h, False)
    _k32.SleepEx(500, True)
    read_rows.append(f"ReadFileEx apc issued={bool(ok)} completions={apc_seen}")
    note("(c) NtReadFile: " + "; ".join(read_rows) + " (0xC0000005 access violation and 0xC00000A3 not ready expected)")

    # (d) other IRP majors
    major_rows: List[str] = []
    for cls in (4, 5, 9, 14, 16, 17, 18, 34):
        buf = ctypes.create_string_buffer(1024)
        iosb = IO_STATUS_BLOCK()
        st = _ntdll.NtQueryInformationFile(h, ctypes.byref(iosb), buf, 1024, cls)
        st = nt_wait(h, None, iosb, st)
        major_rows.append(f"QueryInfo({cls})={st:#x}")
    for cls, payload in ((14, (0).to_bytes(8, "little")), (13, b"\x01"), (20, (0).to_bytes(8, "little")),
                         (4, bytes(40))):
        buf = ctypes.create_string_buffer(payload, len(payload))
        iosb = IO_STATUS_BLOCK()
        st = _ntdll.NtSetInformationFile(h, ctypes.byref(iosb), buf, len(payload), cls)
        st = nt_wait(h, None, iosb, st)
        major_rows.append(f"SetInfo({cls})={st:#x}")
        if cls == 13 and st == 0:
            findings.append("FileDispositionInformation (delete on close) accepted")
    for cls in (1, 3, 4, 5):
        buf = ctypes.create_string_buffer(256)
        iosb = IO_STATUS_BLOCK()
        st = _ntdll.NtQueryVolumeInformationFile(h, ctypes.byref(iosb), buf, 256, cls)
        st = nt_wait(h, None, iosb, st)
        major_rows.append(f"QueryVolume({cls})={st:#x}")
    iosb = IO_STATUS_BLOCK()
    st = nt_wait(h, None, iosb, _ntdll.NtFlushBuffersFile(h, ctypes.byref(iosb)))
    major_rows.append(f"Flush={st:#x}")
    for code in (0x90000, 0x9003C, 0x900C0, 0x110018):      # FSCTL_REQUEST_OPLOCK_LEVEL_1, GET_COMPRESSION, ..., pipe
        iosb = IO_STATUS_BLOCK()
        ev = _k32.CreateEventW(None, True, False, None)
        buf = ctypes.create_string_buffer(64)
        st = _ntdll.NtFsControlFile(h, ev, None, None, ctypes.byref(iosb), code, buf, 64, buf, 64)
        st = nt_wait(h, ev, iosb, st)
        _k32.CloseHandle(ev)
        major_rows.append(f"Fsctl({code:#x})={st:#x}")
    iosb = IO_STATUS_BLOCK()
    ev = _k32.CreateEventW(None, True, False, None)
    off = ctypes.c_int64(0)
    ln = ctypes.c_int64(24)
    st = _ntdll.NtLockFile(h, ev, None, None, ctypes.byref(iosb), ctypes.byref(off), ctypes.byref(ln), 0, True, True)
    st = nt_wait(h, ev, iosb, st)
    _k32.CloseHandle(ev)
    major_rows.append(f"Lock={st:#x}")
    iosb = IO_STATUS_BLOCK()
    ev = _k32.CreateEventW(None, True, False, None)
    buf = ctypes.create_string_buffer(1024)
    st = _ntdll.NtQueryDirectoryFile(h, ev, None, None, ctypes.byref(iosb), buf, 1024, 1, False, None, True)
    st = nt_wait(h, ev, iosb, st)
    _k32.CloseHandle(ev)
    major_rows.append(f"QueryDirectory={st:#x}")
    iosb = IO_STATUS_BLOCK()
    buf = ctypes.create_string_buffer(256)
    st = nt_wait(h, None, iosb, _ntdll.NtQueryEaFile(h, ctypes.byref(iosb), buf, 256, False, None, 0, None, True))
    major_rows.append(f"QueryEa={st:#x}")
    buf = ctypes.create_string_buffer(1024)
    needed = wintypes.ULONG(0)
    st = _ntdll.NtQuerySecurityObject(h, 0x1 | 0x4, buf, 1024, ctypes.byref(needed))
    major_rows.append(f"QuerySecurity(owner|dacl)={st:#x}")
    note("(d) other IRP majors: " + " ".join(major_rows) + " (0xC0000010 invalid device request expected from the "
         "framework; the I/O manager answers a few itself)")
    healthy = status_of(h)
    _k32.CloseHandle(h)

    # (e) open variants (sequential: the device is exclusive)
    P = iso.DEVICE_PATH
    GR, GW = iso.GENERIC_READ, iso.GENERIC_WRITE
    SH = iso.FILE_SHARE_READ | iso.FILE_SHARE_WRITE
    OE = iso.OPEN_EXISTING
    OV = iso.FILE_FLAG_OVERLAPPED
    variants: List[Tuple[str, Tuple[str, int, int, int, int]]] = [
        ("trailing component", (P + r"\extra", GR | GW, SH, OE, OV)),
        ("trailing backslash", (P + "\\", GR | GW, SH, OE, OV)),
        ("deep path", (P + r"\a\b\c", GR | GW, SH, OE, OV)),
        ("dot-dot", (P + r"\..\NimbusMouseFilter", GR | GW, SH, OE, OV)),
        ("stream syntax", (P + ":stream", GR | GW, SH, OE, OV)),
        ("\\\\?\\ prefix", (r"\\?\NimbusMouseFilter", GR | GW, SH, OE, OV)),
        ("lower case", (r"\\.\nimbusmousefilter", GR | GW, SH, OE, OV)),
        ("GLOBALROOT with component", (r"\\.\GLOBALROOT\Device\NimbusMouseFilter\x", GR | GW, SH, OE, OV)),
        ("access 0 (query only)", (P, 0, SH, OE, OV)),
        ("SYNCHRONIZE only", (P, 0x100000, SH, OE, OV)),
        ("GENERIC_EXECUTE", (P, 0x20000000, SH, OE, OV)),
        ("MAXIMUM_ALLOWED", (P, 0x02000000, SH, OE, OV)),
        ("DELETE", (P, 0x10000, SH, OE, OV)),
        ("WRITE_DAC", (P, 0x40000, SH, OE, OV)),
        ("WRITE_OWNER", (P, 0x80000, SH, OE, OV)),
        ("ACCESS_SYSTEM_SECURITY", (P, 0x01000000, SH, OE, OV)),
        ("share none", (P, GR | GW, 0, OE, OV)),
        ("share delete", (P, GR | GW, SH | 0x4, OE, OV)),
        ("CREATE_ALWAYS", (P, GR | GW, SH, 2, OV)),
        ("CREATE_NEW", (P, GR | GW, SH, 1, OV)),
        ("OPEN_ALWAYS", (P, GR | GW, SH, 4, OV)),
        ("TRUNCATE_EXISTING", (P, GR | GW, SH, 5, OV)),
        ("DELETE_ON_CLOSE", (P, GR | GW, SH, OE, OV | 0x04000000)),
        ("BACKUP_SEMANTICS", (P, GR | GW, SH, OE, OV | 0x02000000)),
        ("NO_BUFFERING", (P, GR | GW, SH, OE, OV | 0x20000000)),
        ("WRITE_THROUGH", (P, GR | GW, SH, OE, OV | 0x80000000)),
        ("OPEN_REPARSE_POINT", (P, GR | GW, SH, OE, OV | 0x00200000)),
        ("SESSION_AWARE", (P, GR | GW, SH, OE, OV | 0x00800000)),
        ("non-overlapped", (P, GR | GW, SH, OE, 0)),
    ]
    open_rows: List[str] = []
    for label, (path, access, share, disp, flags) in variants:
        hh, err = open_variant(path, access, share, disp, flags)
        if hh is None:
            open_rows.append(f"{label}: error {err}")
            continue
        try:
            err2, ret = bounded_ioctl(hh, IOCTL_GET, None, 32)
            rd_err = "-"
            if flags & 0x20000000:
                rd_err = str(base._read_once(hh, PACKET, 200))
            open_rows.append(f"{label}: opened, status {'ok' if err2 == 0 else err2}"
                             + (f", read {rd_err}" if rd_err != "-" else ""))
            if label == "DELETE_ON_CLOSE":
                findings.append("open with FILE_FLAG_DELETE_ON_CLOSE succeeded")
        finally:
            _k32.CloseHandle(hh)
    nt_variants = [
        ("NtCreateFile FILE_DIRECTORY_FILE", (r"\??\NimbusMouseFilter", GR | GW | 0x100000, SH, 1, 0x1 | 0x20)),
        ("NtCreateFile FILE_NON_DIRECTORY_FILE", (r"\??\NimbusMouseFilter", GR | GW | 0x100000, SH, 1, 0x40 | 0x20)),
        ("NtCreateFile FILE_OPEN_BY_FILE_ID", (r"\??\NimbusMouseFilter", GR | GW, SH, 1, 0x2000)),
        ("NtCreateFile \\Device path", (r"\Device\NimbusMouseFilter", GR | GW, SH, 1, 0)),
        ("NtCreateFile \\Device path + component", (r"\Device\NimbusMouseFilter\y", GR | GW, SH, 1, 0)),
        ("NtCreateFile FILE_OPEN_REQUIRING_OPLOCK", (r"\??\NimbusMouseFilter", GR | GW, SH, 1, 0x10000)),
        ("NtCreateFile FILE_OPEN_REPARSE_POINT + FILE_SYNCHRONOUS_IO_NONALERT",
         (r"\??\NimbusMouseFilter", GR | GW | 0x100000, SH, 1, 0x200000 | 0x20)),
    ]
    for label, (path, access, share, disp, opts) in nt_variants:
        st, hh = nt_open(path, access, share, disp, opts)
        if hh:
            err2, _ = bounded_ioctl(hh, IOCTL_GET, None, 32)
            open_rows.append(f"{label}: opened, status {'ok' if err2 == 0 else err2}")
            _ntdll.NtClose(hh)
        else:
            open_rows.append(f"{label}: {st:#x}")
    note("(e) open variants: " + "; ".join(open_rows))

    # (f) DuplicateHandle storm: isolation survives until the last duplicate closes
    h = iso._open_device()
    set_iso(h, True)
    dups: List[int] = []
    me = _k32.GetCurrentProcess()
    for _ in range(1000):
        d = wintypes.HANDLE(0)
        if _k32.DuplicateHandle(me, h, me, ctypes.byref(d), 0, False, 0x2):
            dups.append(d.value)
    _k32.CloseHandle(h)
    still_on = status_of(dups[-1])["isolating"] if dups else -1
    for d in dups[:-1]:
        _k32.CloseHandle(d)
    still_on2 = status_of(dups[-1])["isolating"] if dups else -1
    _k32.CloseHandle(dups[-1])
    stf, clean = free_and_clean()
    dup_ok = len(dups) == 1000 and still_on == 1 and still_on2 == 1 and stf is not None and stf["isolating"] == 0
    note(f"(f) {len(dups)} duplicates: isolating after original closed={still_on}, after 999 duplicates closed="
         f"{still_on2}, after the last={stf['isolating'] if stf else '?'}")
    ok = (not findings and dup_ok and healthy["version"] == iso.INTERFACE_VERSION
          and stf is not None and stf["connected_mice"] == st0["connected_mice"])
    record(name, ok, f"findings={findings if findings else 'none'}; duplicate-handle rule holds={dup_ok}; "
                     f"device healthy afterwards (v{healthy['version']}, mice {stf['connected_mice'] if stf else '?'}); {clean}")


def check_soak(seconds: float) -> None:
    name = f"B9 {seconds:.0f} s soak with the production client"
    stops: List[str] = []
    m = iso.MouseIsolation(lambda dx, dy: None, lambda c, p: None, on_stopped=stops.append)
    with contextlib.redirect_stdout(io.StringIO()):
        m.start()
    wd0 = m.status()["watchdog_releases"]
    np0, pp0 = kernel_pool()
    h0 = _handle_count()
    samples: List[Tuple[float, int, int, int]] = []
    t0 = time.perf_counter()
    bad = 0
    try:
        while time.perf_counter() - t0 < seconds:
            time.sleep(min(10.0, seconds - (time.perf_counter() - t0)) if seconds > 10 else 1.0)
            if not m.active:
                break
            st = m.status()
            samples.append((time.perf_counter() - t0, st["isolating"], st["pending_reads"], m.ticks))
            if st["isolating"] != 1 or st["watchdog_releases"] != wd0:
                bad += 1
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            m.stop("soak done")
    np1, pp1 = kernel_pool()
    h1 = _handle_count()
    ticks = samples[-1][3] if samples else 0
    elapsed = samples[-1][0] if samples else 0.0
    rate = ticks / elapsed if elapsed else 0.0
    st, clean = free_and_clean()
    per_second = 1000.0 / iso.TICK_MS
    ok = (bad == 0 and stops == ["soak done"] and st is not None and st["watchdog_releases"] == wd0
          and per_second * 0.45 <= rate <= per_second + 0.1 and h1 - h0 <= 2)
    record(name, ok, f"{len(samples)} samples over {elapsed:.0f} s, bad={bad}; ticks={ticks} ({rate:.2f}/s, about "
                     f"{per_second * 0.5:.1f} to {per_second:.1f} expected); stops={stops}; process handles {h0} -> {h1}; kernel nonpaged pool "
                     f"{np0 / 1e6:.1f} -> {np1 / 1e6:.1f} MB, paged {pp0 / 1e6:.1f} -> {pp1 / 1e6:.1f} MB (system-wide, "
                     f"informational); {clean}")


def check_process_churn(seconds: float) -> None:
    name = "B11 six client processes churning with a kill every 400 ms"
    st0 = iso.get_status()
    deadline = time.time() + seconds
    live: List[Role] = [Role("churn", "--deadline", f"{deadline:.3f}", "--seed", str(i)) for i in range(6)]
    finished: List[Role] = []
    kills = 0
    monitor_stop = threading.Event()
    monitor = {"opens": 0, "violations": 0}

    def monitor_loop() -> None:
        while not monitor_stop.is_set():
            try:
                h = iso._open_device()
            except Exception:
                time.sleep(0.005)
                continue
            try:
                st = status_of(h)
                monitor["opens"] += 1
                if st["isolating"] != 0 or st["pending_reads"] != 0:
                    monitor["violations"] += 1
            except RuntimeError:
                pass
            finally:
                _k32.CloseHandle(h)
            time.sleep(0.005)

    mt = threading.Thread(target=monitor_loop, daemon=True)
    mt.start()
    rnd = random.Random(11)
    seed = 100
    try:
        while time.time() < deadline - 0.5:
            time.sleep(0.4)
            for r in live:
                if not r.pid:
                    r.expect("READY", 0.01)
            victims = [r for r in live if r.pid and r.alive()]
            if not victims:
                continue
            victim = rnd.choice(victims)
            victim.kill()
            kills += 1
            live.remove(victim)
            finished.append(victim)
            live.append(Role("churn", "--deadline", f"{deadline:.3f}", "--seed", str(seed)))
            seed += 1
        for r in live:
            try:
                r.proc.wait(timeout=seconds + 10)
            except subprocess.TimeoutExpired:
                r.kill()
        monitor_stop.set()
        mt.join(timeout=5)
        all_lines: List[str] = []
        for r in live + finished:
            all_lines.extend(r.drain(2.0))
    finally:
        for r in live + finished:
            r.close()
    violations = [ln for ln in all_lines if ln.startswith("VIOLATION")]
    errors = [ln for ln in all_lines if ln.startswith("ERROR")]
    summaries = [_parse_kv(ln) for ln in all_lines if ln.startswith("CHURN")]
    opens = sum(int(s.get("opens", 0)) for s in summaries)
    busy = sum(int(s.get("busy", 0)) for s in summaries)
    st, clean = free_and_clean()
    ok = (not violations and not errors and monitor["violations"] == 0 and st is not None
          and st["watchdog_releases"] == st0["watchdog_releases"])
    record(name, ok, f"{seconds:.0f} s: {kills} kills, {len(summaries)} processes ran to the end with {opens} opens "
                     f"and {busy} busy refusals; monitor opened {monitor['opens']} times and found isolation on "
                     f"{monitor['violations']} times; client violations={len(violations)} errors={errors[:2]}; "
                     f"watchdog_releases {st0['watchdog_releases']} -> {st['watchdog_releases'] if st else '?'}; {clean}")


def check_sync_reads() -> None:
    name = "B12 synchronous reads: CancelSynchronousIo, CloseHandle, TerminateThread, thread exit"
    rows: List[str] = []
    ok = True

    def sync_open() -> int:
        h = _k32.CreateFileW(iso.DEVICE_PATH, iso.GENERIC_READ | iso.GENERIC_WRITE,
                             iso.FILE_SHARE_READ | iso.FILE_SHARE_WRITE, None, iso.OPEN_EXISTING, 0, None)
        if h == iso.INVALID_HANDLE_VALUE or h is None:
            raise RuntimeError(f"sync open failed: {ctypes.get_last_error()}")
        return h

    def blocked_read(h: int, out: Dict[str, object]) -> None:
        out["tid"] = _k32.GetCurrentThreadId()
        buf = ctypes.create_string_buffer(PACKET)
        n = wintypes.DWORD(0)
        t0 = time.perf_counter()
        got = _k32.ReadFile(h, buf, PACKET, ctypes.byref(n), None)
        out["err"] = 0 if got else ctypes.get_last_error()
        out["dt"] = time.perf_counter() - t0

    # The heartbeat completes a parked read after TICK_MS, so the cancel and the
    # close below have to come sooner than that to find the read still blocked;
    # if the tick wins the race anyway, the read returns 0 bytes, which proves
    # the same thing (a synchronous reader is never stuck).
    settle = min(0.3, iso.TICK_MS / 1000.0 * 0.4)

    # (a) CancelSynchronousIo
    h = sync_open()
    set_iso(h, True)
    out: Dict[str, object] = {}
    th = threading.Thread(target=blocked_read, args=(h, out), daemon=True)
    th.start()
    time.sleep(settle)
    hthread = _k32.OpenThread(0x0001, False, int(out["tid"]))
    cancelled = bool(_k32.CancelSynchronousIo(hthread))
    _k32.CloseHandle(hthread)
    th.join(timeout=3)
    still = status_of(h)["isolating"]
    a_ok = (((cancelled and out.get("err") == ERROR_OPERATION_ABORTED) or (not cancelled and out.get("err") == 0))
            and still == 1 and not th.is_alive())
    rows.append(f"(a) CancelSynchronousIo={cancelled}, read error {out.get('err')} (995 cancelled, or 0 if the "
                f"tick got there first), isolation still {still}")
    ok = ok and a_ok
    set_iso(h, False)
    _k32.CloseHandle(h)

    # (b) CloseHandle from another thread while the read blocks
    h = sync_open()
    set_iso(h, True)
    out = {}
    th = threading.Thread(target=blocked_read, args=(h, out), daemon=True)
    th.start()
    time.sleep(settle)
    t0 = time.perf_counter()
    closed = bool(_k32.CloseHandle(h))
    t_close = time.perf_counter() - t0
    th.join(timeout=3)
    st, clean = free_and_clean()
    # NtClose takes the file object lock of a synchronous handle, so the close
    # waits for the blocked read; the heartbeat completes it (0 bytes, error 0)
    # within about a second, which bounds the close. On v2 this close would
    # have waited for a mouse packet.
    b_ok = (closed and not th.is_alive() and out.get("err") in (0, ERROR_NOT_READY, ERROR_OPERATION_ABORTED,
                                                                 ERROR_INVALID_HANDLE) and t_close < 1.5
            and st is not None)
    rows.append(f"(b) CloseHandle from another thread returned {closed} after {t_close * 1000:.0f} ms (it waits for "
                f"the synchronous read, which the heartbeat tick completes: error {out.get('err')}), {clean}")
    ok = ok and b_ok

    # (c) TerminateThread on the reading thread, in a child process (the interpreter cannot recover from it)
    r = Role("sync-terminate")
    try:
        ready = r.expect("READY", 15.0)
        result = r.expect("RESULT", 8.0)
        r.proc.wait(timeout=5)
    finally:
        r.close()
    kv = _parse_kv(result) if result else {}
    t_release = kv.get("t_release", "None")
    c_ok = (ready is not None and kv.get("terminated") != "False" and t_release != "None"
            and 1.8 <= float(t_release) - float(kv.get("t_read", "0")) <= 2.6)
    rows.append(f"(c) TerminateThread on the blocked reader: terminated={_parse_kv(ready).get('terminated') if ready else '?'}, "
                f"watchdog released {float(t_release) - float(kv['t_read']):.2f} s after the read was issued"
                if t_release not in ("None", None) and kv.get("t_read") else
                f"(c) TerminateThread: no release seen ({result})")
    ok = ok and c_ok
    st, clean = free_and_clean()
    ok = ok and st is not None

    # (d) overlapped read parked, its thread exits normally
    h = iso._open_device()
    set_iso(h, True)
    holder: Dict[str, object] = {}

    def park_and_exit() -> None:
        holder["rd"] = _Read(h, PACKET)

    th = threading.Thread(target=park_and_exit, daemon=True)
    th.start()
    th.join(timeout=3)
    time.sleep(0.2)
    rd = holder["rd"]
    err = rd.wait(500)
    rd.close()
    pending_after = status_of(h)["pending_reads"]
    rd2 = _Read(h, PACKET)
    err2 = rd2.wait(50)
    rd2.close()
    st = status_of(h)
    d_ok = err == ERROR_OPERATION_ABORTED and pending_after == 0 and err2 == ERROR_IO_INCOMPLETE and st["isolating"] == 1
    rows.append(f"(d) read parked by a thread that exited: completed with {err} (995 expected), pending after={pending_after}, "
                f"a new read from the main thread parks={err2 == ERROR_IO_INCOMPLETE}, isolation still {st['isolating']}")
    ok = ok and d_ok
    set_iso(h, False)
    _k32.CloseHandle(h)
    st, clean = free_and_clean()
    record(name, ok and st is not None, "; ".join(rows) + f"; {clean}")


def check_desktop_pause() -> None:
    """Switch the input to a private desktop for a second: isolation must pause, then resume.

    The lock screen and the UAC prompt live on the Winlogon desktop, which a
    user process cannot switch to, so this uses a desktop of its own: the
    client sees "not my desktop" either way. The screen goes blank for the
    duration; a timer switches back after 6 s no matter what, and
    Ctrl+Alt+Del would recover a stuck switch by hand.
    """
    name = "B13 isolation pauses while another desktop has the input, resumes when it returns"
    stops: List[str] = []
    m = iso.MouseIsolation(lambda dx, dy: None, lambda c, p: None, on_stopped=stops.append)
    with contextlib.redirect_stdout(io.StringIO()):
        m.start()
    time.sleep(0.5)
    before = m.status()
    own = _u32.OpenDesktopW("Default", 0, False, DESKTOP_SWITCHDESKTOP)
    priv = _u32.CreateDesktopW("NimbusProbeDesktop", None, None, 0, GENERIC_ALL, None)
    if not own or not priv:
        with contextlib.redirect_stdout(io.StringIO()):
            m.stop("probe")
        record(name, False, f"could not open the desktops: own={own} private={priv} error={ctypes.get_last_error()}")
        return
    guard = threading.Timer(6.0, lambda: _u32.SwitchDesktop(own))
    guard.daemon = True
    guard.start()
    t_paused: Optional[float] = None
    t_resumed: Optional[float] = None
    switched = back = still = False
    try:
        t0 = time.perf_counter()
        switched = bool(_u32.SwitchDesktop(priv))
        while switched and time.perf_counter() - t0 < 2.0:
            if m.status()["isolating"] == 0:
                t_paused = time.perf_counter() - t0
                break
            time.sleep(0.02)
        time.sleep(1.0)
        mid = m.status()
        still = mid["isolating"] == 0 and m.active and m.paused and not stops
    finally:
        back = bool(_u32.SwitchDesktop(own))
        guard.cancel()
    t1 = time.perf_counter()
    while time.perf_counter() - t1 < 2.0:
        if m.status()["isolating"] == 1:
            t_resumed = time.perf_counter() - t1
            break
        time.sleep(0.02)
    time.sleep(0.6)
    st = m.status()
    paused_after = m.paused
    ticks = m.ticks
    with contextlib.redirect_stdout(io.StringIO()):
        m.stop("probe")
    _u32.CloseDesktop(priv)
    _u32.CloseDesktop(own)
    stf, clean = free_and_clean()
    ok = (switched and back and t_paused is not None and still and t_resumed is not None and not paused_after
          and st["isolating"] == 1 and stops == ["probe"] and stf is not None
          and stf["watchdog_releases"] == before["watchdog_releases"])
    record(name, ok, f"switched to a private desktop={switched}: isolation off after "
                     f"{'never' if t_paused is None else f'{t_paused * 1000:.0f} ms'}, still paused with the client "
                     f"active 1 s later={still}; switched back={back}: isolation on again after "
                     f"{'never' if t_resumed is None else f'{t_resumed * 1000:.0f} ms'}, paused flag cleared="
                     f"{not paused_after}, ticks={ticks}, stops={stops}; watchdog_releases "
                     f"{before['watchdog_releases']} -> {stf['watchdog_releases'] if stf else '?'}; {clean}")


# ---- main ---------------------------------------------------------------------------

CHECKS: List[Tuple[str, Callable[[argparse.Namespace], None]]] = [
    ("B1", lambda a: check_ioctl_storm(3.0 if a.quick else 8.0)),
    ("B2", lambda a: check_read_flood()),
    ("B3", lambda a: check_open_close_storm(3.0 if a.quick else 8.0)),
    ("B12", lambda a: check_sync_reads()),
    ("B13", lambda a: check_desktop_pause()),
    ("B8", lambda a: check_fuzz(1000 if a.quick else 4000)),
    ("B4", lambda a: check_chaos_kill(10 if a.quick else 30)),
    ("B5", lambda a: check_suspend_precision()),
    ("B7", lambda a: check_inherited_handle()),
    ("B11", lambda a: check_process_churn(6.0 if a.quick else 12.0)),
    ("B6", lambda a: check_cpu_starvation(5.0 if a.quick else 10.0)),
    ("B9", lambda a: check_soak(a.soak)),
]


def emergency_release() -> str:
    """Last resort at exit: kill anything we spawned and make sure the mouse is back."""
    for r in list(SPAWNED):
        try:
            r.close()
        except Exception:
            pass
    iso.stop_all("probe exit")
    st = _wait_device_free(5.0)
    if st is None:
        return "device still held by another process at exit"
    if st["isolating"]:
        try:
            h = iso._open_device()
            set_iso(h, False)
            _k32.CloseHandle(h)
            return "isolation was still on at exit and was cleared"
        except Exception as exc:
            return f"isolation still on at exit and could not be cleared: {exc}"
    return f"pass-through at exit (isolating=0, connected_mice={st['connected_mice']})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated subset, e.g. B1,B8")
    ap.add_argument("--quick", action="store_true", help="shorter storms and fewer iterations")
    ap.add_argument("--soak", type=float, default=120.0, help="length of the soak (B9)")
    ap.add_argument("--expect-version", type=int, default=0,
                    help="run against a driver of this interface version instead of the client's own")
    ap.add_argument("--role", help=argparse.SUPPRESS)
    ap.add_argument("--reader-priority", default="default", help=argparse.SUPPRESS)
    ap.add_argument("--deadline", type=float, default=0.0, help=argparse.SUPPRESS)
    ap.add_argument("--priority", default="normal", help=argparse.SUPPRESS)
    ap.add_argument("--seed", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--seconds", type=float, default=5.0, help=argparse.SUPPRESS)
    ap.add_argument("--handle", type=int, default=0, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.expect_version:
        iso.INTERFACE_VERSION = args.expect_version
        iso.TICK_MS = {3: 1000}.get(args.expect_version, iso.TICK_MS)

    if args.role == "client":
        return role_client(args.reader_priority)
    if args.role == "burn":
        return role_burn(args.deadline, args.priority)
    if args.role == "churn":
        return role_churn(args.deadline, args.seed)
    if args.role == "inherit-parent":
        return role_inherit_parent(args.seconds)
    if args.role == "inherit-child":
        return role_inherit_child(args.handle, args.seconds)
    if args.role == "sync-terminate":
        return role_sync_terminate()

    try:
        st = iso.get_status()
    except RuntimeError as exc:
        print(f"driver not reachable: {exc}")
        return 2
    if st["version"] != iso.INTERFACE_VERSION or st["connected_mice"] == 0:
        print(f"unexpected driver state: {st}")
        return 2
    np0, pp0 = kernel_pool()
    print(f"Nimbus Mouse Filter v{st['version']}, connected_mice={st['connected_mice']}, isolating={st['isolating']}, "
          f"packets passed={st['packets_passed']} captured={st['packets_captured']} dropped={st['packets_dropped']} "
          f"watchdog_releases={st['watchdog_releases']}; kernel nonpaged pool {np0 / 1e6:.1f} MB, "
          f"{os.cpu_count()} logical processors; {driver_pool()}")
    selected = {s.strip().upper() for s in args.only.split(",")} if args.only else None
    t_start = time.perf_counter()
    try:
        for key, fn in CHECKS:
            if selected and key not in selected:
                continue
            print(f"\n{key}", flush=True)
            try:
                fn(args)
            except Exception as exc:
                record(f"{key} crashed", False, f"{type(exc).__name__}: {exc}")
                iso.stop_all("check crashed")
                for r in list(SPAWNED):
                    r.close()
                _wait_device_free(5.0)
    finally:
        outcome = emergency_release()
    st = iso.get_status()
    np1, pp1 = kernel_pool()
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\nFinal: {outcome}; v{st['version']} connected_mice={st['connected_mice']} isolating={st['isolating']} "
          f"pending_reads={st['pending_reads']} packets passed={st['packets_passed']} captured={st['packets_captured']} "
          f"dropped={st['packets_dropped']} watchdog_releases={st['watchdog_releases']}; kernel nonpaged pool "
          f"{np0 / 1e6:.1f} -> {np1 / 1e6:.1f} MB (system-wide); {driver_pool()}; {time.perf_counter() - t_start:.0f} s")
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
