# Guest VM track: Hyper-V GPU-PV on the Windows host

The tooling for [docs/vision/VIRTUAL_MACHINE_FEASIBILITY.md](../docs/vision/VIRTUAL_MACHINE_FEASIBILITY.md):
a Windows 11 guest on the dev machine that shares the host's GPU through
Hyper-V GPU paravirtualization, receives Nimbus's pad through Moonlight and
Sunshine, and is shown on the host through the same stream. The scripts
follow the document's gates in order and each one refuses to run before its
predecessor has passed. Built 2026-09-13; see the document's section 10 for
what has and has not been exercised.

The document's conclusion has not changed: this track exists to answer two
cheap questions (does the GPU partition at all, do wanted titles start in a
guest) and to measure a guest against the mouse filter, not to ship a VM.
Nimbus does not manage VMs.

## Order of operations

| Step | Script | Elevation | Reboot | What it decides |
|---|---|---|---|---|
| 0 | `00-host-preflight.ps1` | none | no | Edition, firmware, one GPU, memory, disk, VirtualBox, the mouse filter's reboot safety, pending reboots. Verdict READY, ALREADY ENABLED or BLOCKED. |
| 1 | `10-enable-hyperv.ps1` | **yes** | **yes, after** | Enables the Hyper-V role and puts the console user in Hyper-V Administrators. Refuses while the mouse filter would not survive a reboot or a reboot is already pending. |
| 2 | `20-check-gpu-partition.ps1` | Hyper-V admin | no | **Gate B, part 1:** `Get-VMHostPartitionableGpu` lists the adapter, or the track ends here. |
| 3 | `30-fetch-iso.ps1` | none | no | A Windows 11 ISO into `C:\NimbusVM\iso` (Fido asks Microsoft's page for the retail link; `-Url` for one you have). |
| 4 | `40-new-guest.ps1` | **yes** | no | Applies the image to a new VHDX, writes the answer file and `C:\nimbus` onto it, creates the Generation 2 VM with a vTPM, turns host Enhanced Session Mode off, starts it. First boot is unattended. |
| 5 | `50-attach-gpu.ps1` | **yes** | no | The partition adapter, the memory-mapped I/O and cache settings, the driver files into the guest's HostDriverStore. `-Verify` runs the in-guest render check: **Gate B, part 2.** |
| 6 | `60-guest-stream.ps1` | Hyper-V admin | no | Guest phase 2 (virtual display, render check), Moonlight on the host, pairing by PIN through Sunshine's API, the background-gamepad setting. |
| 7 | `gate_c_host.py` | none | no | **Gate C:** host input sweeps against the guest's counters, 14 buttons in order, stick holds, a held stick through host focus, Stop within 500 ms. `--actuator pad` then `--actuator nimbus`. |

Everything lives under `C:\NimbusVM` (override with `-VmRoot` or
`NIMBUS_VM_ROOT`): `iso\`, `disks\`, `logs\` (every gate writes a JSON
record there and `vm.log` is the running account), and
`guest-password.txt`, the guest administrator's password, a lab secret the
later scripts read for PowerShell Direct.

Elevated steps from a standard session:

```powershell
Start-Process powershell -Verb RunAs -ArgumentList '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', 'C:\Users\Owen\dev\Nimbus-Adaptive-Controller\vm\10-enable-hyperv.ps1'
```

## Guest side (`guest\`)

Copied to `C:\nimbus` on the guest disk by step 4.

- `unattend.xml`: the answer file. Local administrator `nimbus`, automatic
  logon, Remote Desktop off, first logon runs `setup.ps1`.
- `setup.ps1`: phase 1 at first logon (persistent autologon, no sleep,
  firewall, embeddable Python, ViGEmBus 1.22.0, Sunshine with keyboard and
  mouse forwarding **off** and the pad on, the monitor as a logon task);
  phase 2 from step 6 (virtual display driver, render check).
- `render_check.py`: enumerates DXGI adapters and creates a Direct3D 11
  device on each; PASS when a hardware adapter reaches feature level 11_0.
- `gate_c_monitor.py`: counts Raw Input and low-level-hook mouse and
  keyboard events (hardware and injected apart), polls XInput and logs pad
  transitions, answers on `http://<guest>:47100/snapshot`.

## Before you enable Hyper-V here

- **It reboots the driver baseline.** THREADMASTER is the machine every
  mouse filter number in `docs/vision` was measured on. With the role on,
  Windows itself runs as a partition above the hypervisor. Re-run the filter
  suites before trusting a new driver measurement, and note the change in
  the results log.
- **VirtualBox 7.2.8 is installed.** With Hyper-V on it falls back to the
  Hyper-V API and its guests run markedly slower. `10-enable-hyperv.ps1 -Disable`
  reverses the role (another reboot).
- **The mouse filter.** Step 1 runs `driver\check-mouse-filter.ps1` and
  refuses if the filter is registered but unloadable, because that is the
  configuration that boots with no mouse. On 2026-09-13 the filter was not
  installed and test signing was off, so the reboot was safe.

## Known hazards from the community record

- Match the guest's Windows build to the host's; mismatches blue-screen.
- AMD Adrenalin 24.7.1 to 24.10.1 broke hardware encoding in the guest
  (black screens in Parsec and Sunshine); 24.3.1 and 24.12.1 onward work.
  This host has 23.4.1 (31.0.14043.7000).
- Smart Access Memory (Resizable BAR) on the host crashed games in an
  RX 6800 XT guest. Turn it off in firmware before Gate D.
- Guests get Direct3D 11 and OpenGL officially; DirectX 12 reports are
  mixed. Newer AMD drivers split OpenGL and Vulkan into their own packages
  (`amdogl`, `amdvlk`); step 5 copies them when the host has them.
- Sunshine's 2026 releases moved to a separately licensed virtual HID
  driver; ViGEmBus remains as an Xbox 360 fallback, which is why the guest
  installs ViGEmBus and pins `gamepad = x360`.
- Re-run step 5 after every host display driver update, or the guest's
  copy no longer matches the host's and the adapter stops.

## Teardown

```powershell
vm\50-attach-gpu.ps1 -Remove          # elevated: the partition adapter
Stop-VM NimbusGuest -Force; Remove-VM NimbusGuest -Force
Remove-Item C:\NimbusVM -Recurse       # disks, ISO, logs
vm\10-enable-hyperv.ps1 -Disable       # elevated, then reboot
```
