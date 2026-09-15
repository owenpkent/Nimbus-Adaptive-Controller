# Virtual Machine Feasibility on a Windows Host

**Status:** Research, with the tooling built and Gates A, B and C passed on the dev machine (section 10). The scripts for every gate are in [`vm/`](../../vm/README.md). Hyper-V is enabled on THREADMASTER, a guest exists, its partition of the RX 6600 XT renders, and the isolation claim of Gate C held through the real Moonlight, Sunshine and ViGEm chain with the real app driving. Gate D's latency axis was measured (section 10.8): input to photon through the guest is two to three display frames at the median (30 to 46 ms) and three to five at the 95th percentile, against one natively, so it loses the one axis measured; the rest of Gate D (a real game in the guest, a second machine) was not run. The conclusion of section 1 stands: keep the research, do not integrate.
**Problem statement:** [Input Ownership: Problem Statement](PROBLEM_STATEMENT.md) judges this track against every other boundary on one yardstick (sections 4 and 5). For its first target game, Forza Horizon 6, the guest's Direct3D 11 and OpenGL scope matters before latency does: the game is DirectX 12.
**Date:** 2026-09-11, repointed at a Windows host and remeasured 2026-09-12; tooling built and Gate A answered 2026-09-13, Gates B, C and D's latency axis run the same evening.
**Host:** Windows. Nimbus runs on the host exactly as it ships today; the guest runs the game.
**Question:** On a Windows host, does putting the game in a guest VM buy Nimbus anything it does not already have?
**Answer in one line:** Almost certainly not, and the two facts that decide it are cheap to check before any VM is built.

## 1. Assessment

The isolation a guest would provide is the same property [Host Mode and Input Isolation](HOST_MODE_ISOLATION.md) section 8.4 already measured on this machine: the game sees no physical mouse while Nimbus does. The Nimbus Mouse Filter delivers that on one machine, at zero added latency, with no second operating system, no second Steam library, and no video encoder in the loop. A guest has to beat a working local answer, not an absent one.

There is exactly one argument left for the guest, and it is worth stating fairly: **isolation without asking the user to install a kernel driver.** The filter needs attestation signing, Partner Center, per-vendor anti-cheat goodwill (section 7.6 of Host Mode), and its failure mode is a user with no mouse. A VM needs none of that. If that argument is the reason to keep this track alive, say so explicitly, because every other reason has been overtaken.

Against it, the guest trades a policy risk for a hard block. The filter's anti-cheat risk is "we resemble a XIM and may be misclassified." The guest's is "EAC, BattlEye and Vanguard may refuse to start at all," which lands on precisely the titles that motivate isolation. That is a worse position, not a hedge.

Two gates decide the whole question and both are cheap:

1. **Does GPU paravirtualization work on this GPU?** The dev machine has one AMD adapter and no integrated graphics (section 3). Full passthrough would take the only display away from the host, which is incompatible with a Nimbus panel beside the game. That leaves Hyper-V GPU-PV, whose community tooling is documented mainly against NVIDIA and Intel. AMD is the least-reported path and is unverified here.
2. **Do the target titles run in a guest at all?** The earlier draft removed the blanket claim that every anti-cheat refuses every VM, correctly, and then did not replace it with anything. The replacement is a per-title inventory, and the game harness can now produce one.

If either gate fails, stop. Neither needs a VM built to answer.

*2026-09-13:* both passed on the dev machine (section 10.7), and so did Gate C. Gate D's latency axis was then measured (section 10.8) and the guest lost it: two to three frames at the median and three to five at the 95th percentile, against one natively; the game and second-machine comparisons were not run. Nothing measured moves the answer in the line above.

## 2. What a Windows host actually permits

The option set collapses hard once the host is Windows. This is the main consequence of the change and it removes most of the earlier draft.

| Route | Status on a Windows host |
|---|---|
| Discrete Device Assignment (DDA) | Windows Server only. Microsoft does not support it on client Windows. Out. |
| KVM/QEMU with VFIO passthrough | Requires a Linux host. That is the [Linux Gaming Proposal](LINUX_GAMING_PROPOSAL.md) track, a different document and a different machine. Out here. |
| Looking Glass | Needs a shared-memory device between guest and host (QEMU's `ivshmem`). Hyper-V has no equivalent, so the zero-copy display path does not exist on this host. Out, and sections about its release channels no longer apply. |
| VMware Workstation / VirtualBox 3D | Translated 3D, not an accelerated game path. Out. |
| WSL2 / WSLg | Gives a Linux guest GPU access through `/dev/dxg`, but the Vulkan support Proton needs is not a supported path there. Not a gaming route. |
| **Hyper-V GPU-PV** | **The only credible option.** Shares one physical adapter with the guest. Officially Server 2025 only and stated unsupported on client Windows; community tooling (Easy-GPU-PV and similar) makes it work by copying host driver files into the guest. |

So "a VM on the Windows host" means precisely one thing: a Hyper-V guest using GPU-PV, viewed through a video encoder. Everything else in the earlier draft described a Linux host and belongs in the Linux document.

**Operational warning before anyone enables it.** Turning on Hyper-V makes Windows run as a root partition above the hypervisor, and it requires a reboot. THREADMASTER is the machine every driver measurement in `docs/vision/` was taken on, with a test-signed kernel mouse filter, vJoy and ViGEmBus installed and Secure Boot off. Do not enable Hyper-V there casually: it perturbs the baseline for the filter work, and a reboot ends the working session. If this track is pursued, do it on a machine that is not the driver baseline, or plan the reboot and the re-validation.

*2026-09-13:* it was enabled on THREADMASTER that evening, with the reboot planned and the mouse filter not installed at the time (section 10.7). Every driver measurement from that date on is taken above the hypervisor; `vm\10-enable-hyperv.ps1 -Disable` and a reboot take the host back if a measurement needs the old baseline. vJoy and ViGEmBus kept working (the Gate C runs used both).

## 3. The dev machine, measured 2026-09-12

Replacing the hardware inventory in the earlier draft, which described the sandbox the document was written in rather than any machine this project uses.

| Item | Observation | Consequence |
|---|---|---|
| Host | THREADMASTER, Windows 11 Pro 10.0.26200 | Pro, so Hyper-V is available as an optional feature |
| CPU | AMD Ryzen 9 3950X, 16 cores / 32 threads, virtualization enabled in firmware | CPU is not a constraint. The earlier 2 vCPU budget was sized for a four-core laptop that does not exist here |
| Memory | 31.9 GiB | Not a constraint. A 16 GiB guest leaves the host comfortable |
| GPU | One AMD Radeon RX 6600 XT, 8 GiB, driver 31.0.14043.7000 | **The deciding fact.** No integrated graphics on a 3950X, so there is no second adapter to leave with the host |
| Disk | 297 GiB free of 930 GiB on C: | A guest plus a small Steam library fits. A large library does not |
| Hyper-V | Role not installed: no Hyper-V PowerShell module, no `vmms` or `vmcompute` service (until 2026-09-13; enabled since, section 10.7) | Nothing had been changed on this machine. Enabling it is a deliberate, rebooting act |
| Other | TeamViewer Virtual Monitor Adapter present | Relevant to display-path experiments, and a reminder that TeamViewer injects above the mouse class filter |

The single-GPU finding is what makes this short. The earlier draft listed "single-GPU full passthrough" as "exclude initially" on general principle. On this machine it is excluded on fact, because the host would go dark and Nimbus is a panel on the host desktop.

## 4. What the guest would buy, and what it costs

**Buys:** an input boundary that needs no kernel driver, no code signing and no anti-cheat allow-listing. The physical mouse stays on the host; the guest has no path to it unless a viewer, a redirection service or a passed-through device supplies one. That last clause is the whole safety argument and it is a configuration property, not an architectural guarantee: every viewer's pointer forwarding, USB redirection and management console counts as a path and has to be disabled and verified.

It is also, on paper, stronger isolation than the Linux alternative. Host Mode section 2 notes that an `EVIOCGRAB` silences a device without making it disappear, so some games still see a phantom. A guest that never receives the device has no phantom to see. That is the strongest form of the argument and the earlier draft did not make it.

**Costs, on a Windows host specifically:**

- A second Windows license and a second Steam installation, kept updated.
- A video encode and decode round trip on the same machine, because Looking Glass does not apply under Hyper-V. That is latency and CPU spent to show the guest on the host's own monitor, and it is the cost Host Mode's Option A at least pays for a second machine's worth of GPU.
- An unsupported graphics configuration: GPU-PV on client Windows, on the vendor with the least community coverage.
- A new controller transport and its lifecycle (section 5), where none is needed today.
- Anti-cheat exposure on the titles that motivate the work.

**The comparison the earlier draft omitted.** Host Mode ranks two physical machines (Option A) as the most likely to actually work and the only option where the kernel-anti-cheat tier genuinely runs, needing no Nimbus code at all: the pad already travels the Moonlight to Sunshine to ViGEm chain correctly. A Hyper-V guest spends a second Windows license, an encoder and an unsupported GPU configuration to reach a worse anti-cheat outcome than a second machine does. Any case for the guest has to be made against Option A, not against doing nothing.

## 5. Controller transport into the guest

If the gates in section 1 pass, the guest needs the pad. Two routes, in order of cost.

**Reuse the existing chain first.** Nimbus creates its pad on the host, Moonlight consumes it, Sunshine inside the guest recreates it. This is Host Mode Option A's transport with the second machine replaced by a guest, it is already known to work for gamepads, and it answers the transport question without new code. Sunshine controls controller, keyboard, mouse and pen input separately: enable controller and disable the rest, then verify what the guest actually received, including through every other console attached to the VM. Record the installed Sunshine release and which virtual HID driver it selected; recent releases ship a separately licensed driver with a ViGEmBus fallback, so the default is not what older guides describe.

**Only if that proves insufficient, build a direct receiver.** A dedicated virtio-serial or Hyper-V socket channel carrying normalized controller frames, with a small guest receiver driving a virtual pad. Before specifying that protocol from scratch, note that Nimbus already has one of almost exactly this shape: `driver/nimbus_moufilter/nimbus_moufilter_ioctl.h` and `src/mouse_isolation_win.py` define a versioned interface, a session and generation model, parked reads, bounded queues, a liveness heartbeat and a watchdog that neutralizes when the client dies, matured through four interface revisions. Reuse that contract rather than reinventing it, per the repository's own "reconcile rather than duplicate" rule.

Two semantics are worth carrying over regardless of route, because they are the ones that bite:

- **Stop must be acknowledged.** A paused guest cannot run its own timeout, so a host Stop that is not acknowledged leaves held input in the guest until it executes again. On resume, invalidate the previous generation before replaying anything. If neutral-before-resume cannot be established, armed pause and resume is unsupported and the VM is disarmed before pausing.
- **Host and guest clocks are not synchronized.** Measure round-trip time and guest-local processing separately, or establish synchronization first. Do not report a one-way number you did not earn.

The guest backend question the earlier draft raised is already settled upstream of this document: Nimbus dropped `vgamepad` on 2026-09-09 for `src/padbus_client.py`, a pure-ctypes ViGEmBus client, and [PAD_BUS_FORK_PLAN.md](PAD_BUS_FORK_PLAN.md) is the answer to ViGEmBus being end-of-life.

## 6. Display return path

Under Hyper-V there is no shared-memory display, so the guest is shown through a video encoder: Parsec, Sunshine with Moonlight, or Enhanced Session Mode over RDP. RDP's GPU access is poor for games. That leaves a local encode and decode on the same GPU that is also rendering the game and compositing the host desktop and Nimbus's panel.

Measure the stages separately, not as one blended number: input to host state, host to guest transport, guest polling, rendering, encode, decode, host presentation. Start at 1280x720 at 60 Hz, then 1920x1080 at 60 Hz, with the same scene, settings and input pattern, and record p50, p95 and p99 alongside frame times, dropped frames and host responsiveness. A percentage from an unrelated benchmark establishes nothing about this configuration.

Also test whether controller input continues to reach the guest while Nimbus holds host focus. A viewer that stops forwarding when unfocused breaks the entire form factor, since the panel is meant to be used while the game runs.

## 7. Experiment and gates

Reordered from the earlier draft so the two cheap killers come first. Nothing below requires building a VM until gate C.

### Gate A: per-title VM policy (hours, no VM)

Establish, per title, whether it runs in a guest. FACEIT's published rules prohibit its anti-cheat in a VM or cloud service outright, so exclude that service. For the rest, check the actual policy rather than assuming one. The harness already scripts an EAC title (`eldenring`) and a BattlEye title (`arma3`), so this is a short run rather than a research project.

**Pass:** at least one title that Nimbus users actually want, that is Raw Input, and that starts in a guest. **Fail:** stop. The track has no workload.

### Gate B: GPU-PV on this adapter (hours, reversible, needs a reboot)

On a machine that is not the driver baseline, enable Hyper-V and establish whether the RX 6600 XT partitions and whether a guest initializes an accelerated driver against it. This is the unverified assumption the whole track rests on.

**Pass:** a guest renders 3D through the partitioned adapter. **Fail:** stop. There is no other route on a Windows host.

### Gate C: isolation proof (a day, basic guest, no 3D)

With a basic guest and the Sunshine or Moonlight transport, verify the actual claim. Disable pointer forwarding and USB redirection in every viewer and console. Run host pointer sweeps, operate Nimbus widgets, switch host focus, open a Nimbus dialog, exercise every mapped button, and stop with a stick held, while a guest-side raw-input monitor counts what arrived.

**Pass:** host mouse activity produces zero guest mouse events; intended controller events arrive once and in order; Nimbus dialogs stay usable; Stop clears guest state within 500 ms of acknowledgement. **Fail:** the isolation claim is a configuration accident, not a property. Stop.

### Gate D: playability against Option A (days)

Compare the guest against two things, not one: the host running the game natively with the mouse filter, and a second physical machine over Moonlight. Include the full viewer and transport cost on the guest side. Record driver and software versions, the actual rendering GPU and a reproducible workload.

**Pass:** the guest beats both on some axis a user would notice. **Fail, and this is the expected outcome:** keep the research, do not integrate.

*2026-09-13:* the latency axis was measured with a synthetic workload (section 10.8): the guest loses it. The two comparisons with a real game and with a second machine were not run.

### Gate E: minimal integration (only if D passes)

Add an optional `guest_controller` transport behind the existing output abstraction. Capability checks, session pairing, backend status, per-game selection and receiver recovery. A reconnect or snapshot restore creates a new generation and requires explicit rearming. VM provisioning, GPU binding and OS installation stay in Hyper-V's own tools; Nimbus does not become a VM manager.

## 8. Remaining questions

Section 10 answers the first four from published policy and from the runs of 2026-09-13; the last three are still open and are what Gate D and a real user would answer.

- Does any title that Nimbus users want both need isolation and start inside a Hyper-V guest? *Narrowly yes (the Source titles, and the EAC titles in their no-anti-cheat modes); no kernel-anti-cheat title with its anti-cheat on is known to (10.2).*
- Does GPU-PV partition an RX 6600 XT, and does a guest driver initialize against it? *Yes and yes, on driver 23.4.1 (10.7).*
- What does enabling Hyper-V do to the mouse filter, vJoy and ViGEmBus on the same machine? *vJoy and ViGEmBus kept working; the filter was not installed at the time, so its behaviour above the hypervisor is unmeasured (10.7).*
- Does the encode and decode round trip on a single GPU leave acceptable frame times while that GPU also renders the game? *Without a game load, input to photon through the chain is two to three frames at the median, three to five at the 95th percentile and up to six at the 99th, against one natively (10.8); with a game rendering on the same partition, not measured.*
- Does the viewer keep forwarding the pad while Nimbus holds host focus? *Yes, with Moonlight's background gamepad setting on (10.7).*
- Is there any user for whom "no kernel driver" outweighs a second Windows license and an encoder?
- If the answer to the last question is a real user, does a second physical machine serve them better?

## 9. Sources

- [Steam Big Picture](https://help.steampowered.com/en/faqs/view/3725-76D3-3F31-FB63)
- [Partition and share GPUs with virtual machines on Hyper-V, Microsoft Learn](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/gpu-partitioning)
- [Plan for deploying devices with Discrete Device Assignment, Microsoft Learn](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/plan/plan-for-deploying-devices-using-discrete-device-assignment)
- [FACEIT anti-cheat policy](https://support.faceit.com/hc/en-us/articles/360015788779-What-is-deemed-to-be-a-cheat)
- [Sunshine configuration](https://docs.lizardbyte.dev/projects/sunshine/master/md_docs_2configuration.html)
- [ViGEmBus end-of-life statement](https://docs.nefarius.at/projects/ViGEm/End-of-Life/)
- [Looking Glass requirements](https://looking-glass.io/docs/B7/requirements/), retained only to record that its shared-memory transport has no Hyper-V equivalent

Added 2026-09-13, for section 10:

- [Troubleshooting Hyper-V GPU assignment, partitioning and passthrough, Microsoft Learn](https://learn.microsoft.com/en-us/troubleshoot/windows-server/virtualization/troubleshoot-hyper-v-gpu-assignment-partitioning-passthrough-issues): DDA and GPU-P unsupported on client Windows
- [Easy-GPU-PV](https://github.com/jamesstringer90/Easy-GPU-PV) (archived 2026-06-01) and its issues [#392](https://github.com/jamesstringer90/Easy-GPU-PV/issues/392) (AMD 24.7 to 24.10 regression), [#265](https://github.com/jamesstringer90/Easy-GPU-PV/issues/265) (Smart Access Memory), [#461](https://github.com/jamesstringer90/Easy-GPU-PV/issues/461) (OpenGL and Vulkan packages); [App Sandbox](https://github.com/jamesstringer90/appsandbox), its successor
- [Hyper-V GPU Paravirtualization Manager](https://github.com/DanielChrobak/Hyper-V-GPU-Paravirtualization-Manager): the parameter probing the `vm/` scripts copy
- [Set-VMGpuPartitionAdapter](https://learn.microsoft.com/en-us/powershell/module/hyper-v/set-vmgpupartitionadapter?view=windowsserver2025-ps), [Set-VMKeyProtector](https://learn.microsoft.com/en-us/powershell/module/hyper-v/set-vmkeyprotector), [Enable-VMTPM](https://learn.microsoft.com/en-us/powershell/module/hyper-v/enable-vmtpm), Microsoft Learn
- [Sunshine v2026.906.222525 release notes](https://github.com/LizardByte/Sunshine/releases/tag/v2026.906.222525) and [configuration](https://docs.lizardbyte.dev/projects/sunshine/latest/md_docs_2configuration.html); [moonlight-qt SettingsView.qml](https://github.com/moonlight-stream/moonlight-qt/blob/master/app/gui/SettingsView.qml) (the background gamepad setting)
- [VirtualDrivers/Virtual-Display-Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver/releases), [nomi-san/parsec-vdd](https://github.com/nomi-san/parsec-vdd)
- [Fido](https://github.com/pbatard/Fido); [Windows 11 Enterprise evaluation](https://www.microsoft.com/en-us/evalcenter/evaluate-windows-11-enterprise) (registration form, no direct link)
- [Easy Anti-Cheat interfaces](https://dev.epicgames.com/docs/game-services/anti-cheat/anti-cheat-interfaces) ("does not support virtual machines"); [BattlEye on VMs, 2020](https://x.com/TheBattlEye/status/1289027672186720263); [BattlEye hypervisor detection](https://secret.club/2020/01/12/battleye-hypervisor-detection.html); [Halo MCC anti-cheat disabled mode](https://support.halowaypoint.com/hc/en-us/articles/360037475251); [Riot Vanguard issue 50](https://github.com/RiotVanguard/Vanguard/issues/50)

## 10. Build log, 2026-09-13: the tooling exists, the reboot does not

Asked to build the VM solution on this machine. What stands at the end of the session: every script from the host preflight to the Gate C proof is in [`vm/`](../../vm/README.md), the parts that need no hypervisor have run, and the track is parked at the one step this session could not take. Enabling the Hyper-V role needs an elevated prompt and a reboot; the account the work ran under is a standard user; and section 2's warning about this host being the driver baseline stands. Nothing on the host changed except a new `C:\NimbusVM` holding logs and the install ISO.

### 10.1 The host, re-measured

Section 3 holds. Added: the display driver package GPU-PV copies into a guest is `u0390319.inf_amd64_32d8157dec983dab`, 1,493 MB (AMD Software 23.4.1, driver 31.0.14043.7000). VirtualBox 7.2.8 is installed and will fall back to the Hyper-V API once the role is on. The mouse filter is not installed and test signing is off, so `driver\check-mouse-filter.ps1` reports SAFE and the reboot cannot take the mouse. No reboot is pending. Owen's account is not in Hyper-V Administrators; step 1 adds the console user, after which the VM cmdlets and PowerShell Direct work without elevation. No Windows ISO was on any drive; one is now: `C:\NimbusVM\iso\Win11_25H2_English_x64_v2.iso`, 8.47 GB, retail multi-edition, the same 25H2 as the host, SHA256 recorded in `iso.json`. `vm\00-host-preflight.ps1` says READY.

### 10.2 Gate A, answered from published policy

Gate A asked for a per-title inventory and said the harness could produce one. It did not need to: the answer comes from the vendors' published rules and the community record, and no first-hand Hyper-V report exists for any anti-cheat title. Published policy (P), community report (C), unknown (U).

| Title | Anti-cheat | VM policy | Starts in a Hyper-V guest |
|---|---|---|---|
| Left 4 Dead 2, Half-Life 2 | VAC (L4D2), none (HL2 single player) | P: VAC bans modifications, says nothing about VMs | No blocker known |
| ELDEN RING | Easy Anti-Cheat | P: "does not support virtual machines"; C: refused under KVM until the SMBIOS was hidden. Official offline launch skips EAC | Unknown with EAC; no blocker without |
| Halo: The Master Chief Collection | Easy Anti-Cheat | Same policy; official "Anti-Cheat Disabled" launch option; C: ran under KVM in 2022 | Unknown with EAC; no blocker without |
| Arma 3 | BattlEye | P (2020): "countermeasures in several games"; detection is a CPUID timing check. The game starts; BattlEye servers kick | Starts; single player works; server join fails |
| Total War (Warhammer III, Pharaoh) | None; Denuvo | U; Denuvo limits new machines to five a day | Unknown |
| EVE Online, No Man's Sky, ACE COMBAT 7, Kerbal Space Program, PowerWash Simulator, Halo Wars DE, Carrier Command 2, Battlefront 2004, Liftoff, DRL Simulator | None | U | Unknown; the two drone simulators need the pad to reach the guest |
| Riot Vanguard titles | Vanguard | P: unsupported (VAN 9100) | No |
| FACEIT | FACEIT AC | P: a VM is listed as a cheat | No |

The overall picture for the two anti-cheats that matter: Epic's position is unconditional and enforcement is per title, undocumented, and tightened in late 2025 (a cloud provider reported EAC titles stopping in December with studios unaware). BattlEye does not block launch at all; it kicks at a server. So Gate A passes on its narrow reading: the two Source titles are Raw Input, wanted, and have no blocker, and both EAC titles have official no-anti-cheat modes that remove the question for single player. It does not pass on the reading the argument needs, which is a kernel-anti-cheat title running with its anti-cheat on inside a guest. None is known to. Section 1's comparison stands: the guest's anti-cheat outcome is no better than the filter's and is worse than a second machine's.

### 10.3 What the research changed in sections 2, 5 and 6

- **Easy-GPU-PV is archived** (2026-06-01); the author's successor, App Sandbox, uses Virtual Machine Platform and its own display driver and claims DirectX 12 and Vulkan in the guest. The `vm/` scripts call the Hyper-V cmdlets directly and probe their parameters the way the Paravirtualization Manager does, so they depend on neither project.
- **Microsoft's stated scope for a GPU-P guest is Direct3D 11 and OpenGL.** DirectX 12 reports are mixed. Elden Ring is a DirectX 12 title, so Gate D should be run on Left 4 Dead 2 or Half-Life 2, which the harness already calibrates.
- **AMD specifics:** Adrenalin 24.7.1 to 24.10.1 broke hardware encoding in the guest (black screens in Parsec and Sunshine); 24.3.1 and 24.12.1 onward work; this host's 23.4.1 predates the range. Smart Access Memory on the host crashed games in an RX 6800 XT guest, so it goes off before Gate D. Newer AMD drivers split OpenGL and Vulkan into `amdogl` and `amdvlk` packages that the guest also needs; step 5 copies them when present. Guest and host builds must match or the guest blue-screens, which is why the ISO is 25H2.
- **The transport of section 5 still works, with one change.** Sunshine's 2026 releases moved to a separately licensed virtual HID driver (a paid licence per machine); ViGEmBus remains as an Xbox 360 fallback and is no longer bundled. The guest therefore installs ViGEmBus 1.22.0 itself and pins `gamepad = x360`, with `keyboard` and `mouse` disabled. Moonlight has the setting section 6 asked for: "Process gamepad input when Moonlight is in the background".
- **The evaluation ISO has no scriptable link** (its download lands on a registration form). Fido asks Microsoft's page for the retail link, and an unactivated Pro guest is the second-licence cost of section 4 made concrete.
- **A vTPM needs no Host Guardian Service:** `Set-VMKeyProtector -NewLocalKeyProtector` then `Enable-VMTPM`, confirmed on Microsoft Learn.

### 10.4 What was built, and what has run

The runbook is [`vm/README.md`](../../vm/README.md). In order: `00-host-preflight.ps1` (read-only verdict), `10-enable-hyperv.ps1` (elevated; refuses while the mouse filter would not survive the reboot or another reboot is pending), `20-check-gpu-partition.ps1` (Gate B part 1), `30-fetch-iso.ps1`, `40-new-guest.ps1` (the image applied straight onto a VHDX with the answer file and the guest tooling on it, so Windows Setup never runs and the first boot needs no hand on the console; Enhanced Session Mode off), `50-attach-gpu.ps1` (the partition adapter and the host driver files into the guest's HostDriverStore; `-Verify` is Gate B part 2), `60-guest-stream.ps1` (the virtual display, Moonlight, pairing by PIN through Sunshine's API), and the Gate C pair: `guest/gate_c_monitor.py` in the guest counts Raw Input and low-level-hook mouse and keyboard events, hardware and injected apart, and logs every XInput transition; `gate_c_host.py` runs the section 7 script against it with either the pad direct or the real app through the harness's actuator.

Run today, on the host:

| Check | Result |
|---|---|
| `00-host-preflight.ps1` | READY; VirtualBox warning; filter SAFE |
| `guest/render_check.py` on the host | RX 6600 XT creates a Direct3D 11 device at feature level 11_0: the check itself works |
| Monitor and host driver in loopback (`--guest 127.0.0.1 --loopback --actuator pad`) | 5 of 5 judged checks: 14 buttons as 28 events in order, 6 stick and trigger events in order, a held stick through host input, Stop to neutral in 15 ms. The host's 300 synthesized moves arrived as 183 coalesced Raw Input packets and 302 low-level hook events, all classified injected, which is what a leaking viewer would look like from inside the guest |
| `tests/test_vm_gate_c.py` (in the fast suite) | 19 checks: a faithful actuator passes; a dropped button, swapped edges and a Stop that leaves the stick held each fail their phase |
| Every `.ps1` parsed; 10, 40 and 50 refuse unelevated; 20 exits 2 with Hyper-V off | as designed |
| `30-fetch-iso.ps1` | the 25H2 ISO, hash recorded |

Not run at that point, because each needs the hypervisor: 20, 40, 50, 60 and Gate C against a guest. Inside those, four things were written from documentation: whether the answer file's first-logon command runs elevated, whether winget works under PowerShell Direct for the virtual display driver, whether Sunshine's `/api/pin` still takes basic authentication on the 2026 release, and Moonlight's background-gamepad setting. All four met reality the same evening; section 10.7 says how each came out (three as hoped, one with a twist: the API takes basic authentication but also needs a `pairing_id`).

### 10.5 The handoff (run the same evening; kept as the procedure)

From an elevated prompt, with nobody depending on the machine for the next ten minutes:

```powershell
C:\Users\Owen\dev\Nimbus-Adaptive-Controller\vm\10-enable-hyperv.ps1
Restart-Computer
```

After the reboot, from a normal prompt in the repo:

```powershell
vm\20-check-gpu-partition.ps1                      # Gate B part 1; FAIL ends the track
```

Then, elevated, `vm\40-new-guest.ps1 -Wait` (about twenty minutes, unattended), `vm\50-attach-gpu.ps1 -Verify` (Gate B part 2), and from a normal prompt `vm\60-guest-stream.ps1`, which prints the two `gate_c_host.py` commands. Re-run the mouse filter suites before trusting any driver number measured after the role is on, and expect VirtualBox to be slower. `vm\10-enable-hyperv.ps1 -Disable` and a reboot put the host back.

### 10.6 The decision, unchanged

The tooling makes the two cheap gates one reboot away and the expensive ones a day each; it does not move the conclusion of section 1. Gate A found a workload but not the one that would justify a guest, and every fact the research added (unsupported on client, DirectX 11 only officially, an AMD driver window that breaks it, a paid pad driver in the guest, a second licence) is a cost on the guest's side of the ledger.

### 10.7 The evening: Gates B and C run

The handoff ran later the same day. Every elevated step (10, 40, 50) was one line pasted into an elevated prompt by the owner; everything else ran from a standard session, because step 10 had put the console user in Hyper-V Administrators and PowerShell Direct into the guest works from there. Sections 10.4 and 10.5 above describe the afternoon; this is what the scripts met when they ran.

**Gate B, part 1: PASS.** After the reboot `Get-VMHostPartitionableGpu` lists the RX 6600 XT on driver 23.4.1 (31.0.14043.7000) with 32 valid partitions. The host reports VRAM, decode and compute totals of 1,000,000,000 and an encode total of 2^64 minus 1: the percentage-in-disguise values the community scripts expect, so step 5's 50 percent share became 500,000,000 of each and 2^63 of encode.

**The guest built unattended.** Step 4 applied "Windows 11 Pro" (index 6 of 11, matched by exact name) from the retail 25H2 ISO onto the VHDX, and the first boot ran specialize, OOBE and the automatic logon by itself. The answer file's first-logon command ran elevated, which was the first of the four unknowns in 10.4, and `setup.ps1` finished every phase 1 step in 38 seconds: Python 3.11.9, ViGEmBus 1.22.0, Sunshine 2026.906.222525 with keyboard and mouse forwarding off, the monitor on port 47100. The guest took 172.17.227.55 on the Default Switch.

**Gate B, part 2: PASS.** Step 5 copied the display package, the `amdxe` package and 96 loose AMD files into the guest's HostDriverStore. On the next boot the guest listed a second display adapter, "AMD Radeon RX 6600 XT" on `PCI\VEN_1414&DEV_008E` (Microsoft's paravirtual GPU device, the driver behind it loaded from HostDriverStore), status OK. `render_check.py` created a Direct3D 11 device on it at feature level 11_0 with 8,147 MB of dedicated memory, and Sunshine in the guest found the AMD hardware encoders (`h264_amf`, `hevc_amf`) through the partition; its stream ran HEVC Main at 1280x720 and 60 Hz. One driver-age note from its log: the 10-bit HEVC encoder is refused on AMD drivers older than 23.30, which this host's 23.4.1 is.

**The virtual display.** winget's `VirtualDrivers.Virtual-Display-Driver` package is the portable VDD Control app (an exe, `devcon.exe`, the signed driver and a settings file), extracted and nothing installed, and the control app installs its driver from a GUI. The guest setup now does that work itself: the settings file to `C:\VirtualDisplayDriver`, the driver's signer (SignPath Foundation) into TrustedPublisher, because a fresh guest refuses a silent install with 0xE0000242 otherwise, `pnputil /add-driver /install`, then `devcon install MttVDD.inf Root\MttVDD`. The guest then has a second, VDD-backed monitor at 2560x1440 beside the Hyper-V one. The winget path under PowerShell Direct, the second unknown, worked.

**The transport.** Sunshine's 2026 API keeps a list of pending pairing requests (`GET /api/pin`), and the PIN has to be posted with that request's `pairing_id`; the third unknown, resolved by reading the pending list after Moonlight's `pair` starts and answering the id that appeared. The JSON body has to travel as a file, because PowerShell 5.1 re-quotes a literal `{"pin":...}` argument on its way into curl. Moonlight 6.1.0's command line has everything section 6 asked for: `--display-mode windowed`, `--background-gamepad`, `--resolution`, `--fps`. And one fact about the chain that no document mentioned: Moonlight forwards every host gamepad, so the host's vJoy device appeared in the guest as player 0 the moment the stream started, and Nimbus's pad lands in slot 1. `gate_c_host.py` therefore looks for the slot that appears after it plugs its own pad.

**Gate C: PASS, 7 of 7 judged checks, with the pad direct and with the real app.** The host input sweep ran with Moonlight's window focused, which is the case where a viewer forwards everything: 300 synthesized relative moves, 40 cursor jumps, a click and a key tap on the host produced zero mouse and zero keyboard events in the guest on every path the monitor watches (Raw Input and both low-level hooks). The 14 buttons arrived once each and in order; the stick holds arrived in order; a held left stick survived a second sweep with the viewer in the background, and that sweep also produced nothing in the guest. Stop with a stick held read neutral in the guest in 79 to 94 ms with the pad direct (an unplug, seen as a disconnect) and 54 to 62 ms through the app (a release, seen as the stick returning), against the 500 ms criterion.

| Check | Pad direct | Nimbus app |
|---|---|---|
| Host sweep, viewer focused: guest mouse and keyboard events | 0 and 0 | 0 and 0 |
| Our pad's slot | 1 (vJoy in 0) | 1 (vJoy in 0) |
| 14 buttons, in order | 28 events | 28 events |
| Stick and trigger holds, in order | 6 events | 4 events (no trigger widget in the bundled profile; step skipped) |
| Held stick through a background sweep | held, 0 drops, 0 guest input | held, 0 drops, 0 guest input |
| Stop with a stick held | 79 to 94 ms | 54 to 62 ms |

Two things the run taught the tooling. First, a wrong alarm: with a stick held, the guest counted 14 to 15 "keyboard" events per sweep, all injected. Every one was a Windows `VK_GAMEPAD_*` virtual key (0xC3 for A, 0xD5 for the left thumbstick right, auto-repeating every 100 ms while held), the guest shell's own reflection of the pad it had just received; a game does not read those as a keyboard. The monitor now counts that range apart and logs every key by code. Second, the real app holds a stick through a synthesized press on its widget, so the second sweep's click, landing on the Nimbus window that held the host focus, was a legitimate release; the sweep now starts with the cursor over the viewer and sends no click, and the click's path stays covered by the first sweep.

**What Gate C did not test.** Every host input was synthesized with `SendInput`, which Moonlight forwards exactly as it forwards a physical pointer once its window is focused, but the physical mouse and the mouse class stack were not exercised, and nobody opened a basic vmconnect console during the runs. The synthetic Hyper-V mouse of that console is the one remaining pointer path into the guest, and it forwards only while someone clicks inside it.

**Where that leaves the question.** Gates A, B and C passed; the isolation is a configuration property, as section 4 said, and the configuration held. Gate D, the comparison that decides anything (the guest against the host running the game natively under the mouse filter, and against a second machine over Moonlight, on the DirectX 11 titles the harness can measure), was then run on the one axis the bench allowed, latency, in section 10.8. The costs in 10.6 are unchanged, and two of them now have numbers: the pad path through the chain costs about 60 to 90 ms at the Stop (with the monitor's poll and an HTTP round trip inside that), and input to photon through the whole chain is two to three frames at the median and three to five at the 95th percentile, against one natively.

A second document was reviewed the same evening, [TARGET_AWARE_AIM_PLAN.md](TARGET_AWARE_AIM_PLAN.md), and its section 13.2 records how the two tracks relate.

### 10.8 Gate D, the axis that could be measured: latency

Gate D as written compares the guest against a game running natively under the filter and against a second machine over Moonlight. Neither was on the bench: a game in the guest needs an interactive Steam login, and there is no second Windows machine. What could be measured, and was, is the axis section 6 asks for first: the input and display path's latency, stage by stage where the tooling allows, with a synthetic workload that runs identically on both sides.

**The instrument.** `vm/guest/beacon.py` is a window that goes light while pad button A is held, or for a moment on request over HTTP; `vm/gate_d_latency.py` presses A on the host's pad (the same `X360Pad` Nimbus uses) and times, with a GDI probe of the host's composed desktop, when the change reaches the host's screen. In guest mode the beacon runs in the guest and the probe watches Moonlight's window, so the path is Moonlight's forwarding, Sunshine's ViGEm pad, the guest's XInput poll and repaint, Sunshine's capture and HEVC encode, the stream, Moonlight's decode, and the host's presentation. In native mode the beacon runs on the host and the path is the pad, the poll, the repaint and the presentation. The probe returns the latest composed frame and costs one refresh, so every sample is one display frame at 60 Hz: the numbers are quantized to frames, which is what "photon" means on this display, and the distributions average the phase out. The stream was 1280x720 at 60 Hz, HEVC, Moonlight's frame pacing off; the guest's primary display is the Hyper-V one at 1024x768, pillarboxed into the window.

**The numbers** (milliseconds, press of A to the light reaching the host's screen; release is the other edge of the same trial):

| Run | Trials | Press p50 | Press p95 | Press p99 | Release p50 | Release p95 |
|---|---|---|---|---|---|---|
| Native (beacon on the host) | 40 | 12.8 | 13.5 | 14.1 | 16.6 | 17.5 |
| Guest, run 1 | 40 | 46.0 | 79.7 | 96.1 | 49.6 | 67.5 |
| Guest, run 2 | 40 | 29.9 | 46.9 | 62.5 | 49.9 | 82.7 |
| Guest, run 3 | 80 | 30.1 | 63.4 | 96.6 | 49.7 | 83.4 |

Native is one display frame, as it should be. Through the guest the median press-to-photon is 30 to 46 ms (two to three frames), the 95th percentile 47 to 80 ms, the 99th 63 to 97 ms, with a worst sample of 113 ms; the release edge sits near 50 ms at the median and 83 ms at the tail in every run. The request-driven flash, which excludes the pad path, measured 63 ms at the median and 80 ms at the 95th through the guest against 30 ms native, but both carry about a frame of the beacon's own thread hand-off, so that pair says the display path alone costs the guest roughly 30 ms at the median and it does not split the pad transport out cleanly; the Gate C Stop timings (54 to 94 ms, which include the monitor's 4 ms poll and an HTTP round trip) are the nearest thing to the pad path on its own.

**What this says.** On the latency axis the guest does not beat the host running natively; it takes two to three frames at the median, three to five at the 95th percentile and up to six at the 99th, against one natively, with run-to-run variation that a player would feel as inconsistency rather than as a fixed delay. The mouse filter's isolation costs none of this: on the host the game's pad path is the native row. Gate D's pass condition, the guest beating both comparisons on some axis a user would notice, is not met on the one axis measured, and the other two comparisons (a real game in the guest, a second physical machine) remain unrun. The expected outcome stands: keep the research, do not integrate.

**Not measured.** Frame drops and host responsiveness while the partitioned GPU also renders a game (section 6), the 1920x1080 step, and anything with a game in the guest. Sunshine also logged, on this host's 23.4.1 driver, that 10-bit HEVC is unavailable below driver 23.30, so the stream ran 8-bit.

**A hazard the tooling now states.** The beacon flashes, up to twice a second for a minute or more, and the viewer window is brought to the front of the host's desktop for the measurement, so the flashing is on the host's screen. The owner saw it during these runs. The beacon is now a 300 px light-gray square by default, the trial count is lower, and the scripts say not to run them while anyone is looking at the screen, and never near someone who is photosensitive. The short form, from this side: a guest hides an assist process from a game's anti-cheat and that buys nothing, because detection is behavioural and uses decoys drawn into the frame, which a host reading the stream engages just the same; the games where a guest is usable (Gate A) are the games where assist would be allowed, so the two tracks measure on the same titles with the same harness; and a guest makes assist worse, since perception would see only Moonlight's decoded stream and the clipboard oracle the assist plan's phase 1 uses is a channel the isolation configuration closes on purpose. Neither track waits on the other.
