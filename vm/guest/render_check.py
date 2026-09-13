"""
Gate B, part 2: does this Windows session render 3D on a real GPU?

Enumerates the DXGI adapters and creates a Direct3D 11 device on each
hardware adapter, printing the feature level it came up at. Meant to run
inside the guest after 50-attach-gpu.ps1, where the partitioned adapter
should appear by its host name (for example "AMD Radeon RX 6600 XT") and
create a device at feature level 11_0 or better. The Hyper-V basic display
("Microsoft Hyper-V Video") and the WARP software rasterizer are listed but
do not count. Also runs on the host, which is how it was verified.

Pure ctypes, no packages, so the guest only needs the embeddable Python.

Run::

    python render_check.py [--json out.json]

Exit code 0 when a hardware adapter created a device at 11_0 or better,
1 when none did.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
from ctypes import wintypes
from typing import Any, Dict, List

if sys.platform != "win32":
    sys.exit("Windows only")

DXGI_ADAPTER_FLAG_SOFTWARE = 2
D3D_DRIVER_TYPE_UNKNOWN = 0
D3D11_SDK_VERSION = 7
FEATURE_LEVEL_NAMES = {
    0x9100: "9_1", 0x9200: "9_2", 0x9300: "9_3",
    0xA000: "10_0", 0xA100: "10_1",
    0xB000: "11_0", 0xB100: "11_1",
    0xC000: "12_0", 0xC100: "12_1", 0xC200: "12_2",
}
GOOD_LEVEL = 0xB000


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", LUID), ("Flags", wintypes.UINT),
    ]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "GUID":
        text = text.strip("{}")
        parts = text.split("-")
        g = cls()
        g.Data1 = int(parts[0], 16)
        g.Data2 = int(parts[1], 16)
        g.Data3 = int(parts[2], 16)
        tail = bytes.fromhex(parts[3] + parts[4])
        for i, b in enumerate(tail):
            g.Data4[i] = b
        return g


IID_IDXGIFactory1 = GUID.of("770aae78-f26f-4dba-a829-253c83d1b387")


def _vcall(obj: ctypes.c_void_p, index: int, restype, *argtypes):
    """Call a COM vtable slot on ``obj`` (an interface pointer)."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        _vcall(obj, 2, ctypes.c_ulong)(obj)


def enumerate_adapters() -> List[Dict[str, Any]]:
    dxgi = ctypes.WinDLL("dxgi")
    d3d11 = ctypes.WinDLL("d3d11")
    factory = ctypes.c_void_p()
    hr = dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory))
    if hr != 0:
        raise OSError(f"CreateDXGIFactory1 failed 0x{hr & 0xFFFFFFFF:08X}")
    enum_adapters1 = _vcall(factory, 12, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
    results: List[Dict[str, Any]] = []
    index = 0
    while True:
        adapter = ctypes.c_void_p()
        hr = enum_adapters1(factory, index, ctypes.byref(adapter))
        if hr != 0:   # DXGI_ERROR_NOT_FOUND ends the list
            break
        desc = DXGI_ADAPTER_DESC1()
        _vcall(adapter, 10, ctypes.c_long, ctypes.POINTER(DXGI_ADAPTER_DESC1))(adapter, ctypes.byref(desc))
        software = bool(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)
        level = wintypes.UINT(0)
        # With ppDevice and ppImmediateContext NULL the call only proves the
        # device could be created and reports the level: S_FALSE (1) on
        # success, per the D3D11CreateDevice documentation.
        hr = d3d11.D3D11CreateDevice(
            adapter, D3D_DRIVER_TYPE_UNKNOWN, None, 0, None, 0, D3D11_SDK_VERSION,
            None, ctypes.byref(level), None)
        ok = hr in (0, 1)
        entry = {
            "index": index,
            "description": desc.Description,
            "vendor_id": f"0x{desc.VendorId:04X}",
            "device_id": f"0x{desc.DeviceId:04X}",
            "dedicated_vram_mb": int(desc.DedicatedVideoMemory // (1024 * 1024)),
            "software": software,
            "create_device_hr": f"0x{hr & 0xFFFFFFFF:08X}",
            "feature_level": FEATURE_LEVEL_NAMES.get(level.value, f"0x{level.value:X}") if ok else None,
            "hardware_3d": (ok and not software and level.value >= GOOD_LEVEL),
        }
        results.append(entry)
        _release(adapter)
        index += 1
    _release(factory)
    return results


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", help="also write the adapter list here")
    args = parser.parse_args(argv)
    adapters = enumerate_adapters()
    for a in adapters:
        kind = "software" if a["software"] else "hardware"
        print(f"[{a['index']}] {a['description']}  ({kind}, {a['dedicated_vram_mb']} MB VRAM)  "
              f"D3D11CreateDevice {a['create_device_hr']}  feature level {a['feature_level']}")
    good = [a for a in adapters if a["hardware_3d"]]
    verdict = "PASS" if good else "FAIL"
    print(f"GATE B (part 2) {verdict}: "
          + (", ".join(a["description"] for a in good) + " renders at 11_0 or better" if good
             else "no hardware adapter created a Direct3D 11 device"))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"verdict": verdict, "adapters": adapters}, fh, indent=2)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
