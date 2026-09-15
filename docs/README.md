# Project Nimbus Documentation

> **Quick Start**: See [../DIRECTORY.md](../DIRECTORY.md) for project structure overview.

## Setup & Installation
- [Installation Guide](setup/INSTALLATION.md) — installer, vJoy setup, prerequisites, troubleshooting
- [Linux Setup](setup/LINUX.md) — running on Linux: uinput permissions, output modes, Steam/Proton, Wayland notes
- [Profile System](setup/PROFILES.md) — profile storage, JSON structure, custom layout widget properties
- [Packaging Guide](setup/PACKAGING.md) — **build & distribute** — PyInstaller, NSIS, code signing, release checklist

## Borderless Gaming
- [Game Compatibility](GAME_COMPATIBILITY.md) — verified/likely/partial/incompatible games, tips by genre, how ClipCursor release works
- [Host Mode & Input Isolation](vision/HOST_MODE_ISOLATION.md) — research on the Raw Input tier; on Linux it is solved by Mouse Isolation (measured in section 5)

## Architecture
- [Architecture Overview](architecture/architecture.md) — codebase structure, QML/Python bridge, widget system, borderless module
- [V-Droid Driver Brainstorm](architecture/VDROID_DRIVER_BRAINSTORM.md) — custom virtual HID driver plans (future)

## Development
- [LLM Notes](development/LLM_NOTES.md) — **for AI assistants** — implementation details, conventions, known fixes
- [Integration Guide](development/INTEGRATION_GUIDE.md) — how to add new widgets and features
- [Start Here](development/START_HERE.md) — quick orientation for new contributors
- [Widget Ideas](development/WIDGET_IDEAS.md) — planned and brainstormed widget concepts

## Accessibility
- [Accessibility Spotlight Nomination](accessibility/ACCESSIBILITY_SPOTLIGHT_NOMINATION.md)

## Vision & Expansion
- [Research Platform](vision/RESEARCH_PLATFORM.md) — Nimbus as a disability gaming research tool; telemetry, IRB, university partnerships
- [AAC Integration](vision/AAC_INTEGRATION.md) — Augmentative & Alternative Communication; phrase buttons, scanning, TTS output
- [Modular Control Surface](vision/MODULAR_CONTROL_SURFACE.md) — beyond gaming: video editing, drawing, DAW, streaming, any application
- [Hardware Integration](vision/HARDWARE_INTEGRATION.md) — wrapping XAC, QuadStick, and other adaptive hardware through vJoy; input pipeline architecture
- [Keyboard Output](vision/KEYBOARD_OUTPUT.md) — native keystroke/shortcut emission via SendInput; no external dependencies; bundled in installer
- [Host Mode & Input Isolation](vision/HOST_MODE_ISOLATION.md) — solving the Raw Input tier; VMs, cloud gaming, two-PC streaming, Linux/evdev, and a mouse-class filter driver
- [Aim Assistance](vision/AIM_ASSISTANCE.md): why aiming is hard, a code audit of the stick pipeline, the assistance options ranked by effort, and the tier 1 build (one radial shaping pass, anti-deadzone, travel, precision modifier) with its five-layer test plan and the measurements against Left 4 Dead 2
- [Game Test Harness](vision/GAME_TEST_HARNESS.md): automated tests against real games with ground truth from the game's console (Source engine) or frame differencing; recipes, oracles, a pad and a Nimbus actuator, the environment Spectator+ will run in, and the Left 4 Dead 2 calibration
- [Testing Strategy](vision/TESTING_STRATEGY.md): from liveness checks to regression tests; the fast-test runner and CI, the frame oracle as a motion measurement, windowed games, a console-less reset and expected-value bands, with what the saved frames changed about the design and what the rerun of every game found
- [Linux Probe Plan](vision/LINUX_PROBE_PLAN.md): the EVIOCGRAB and uinput experiment against a real EAC game. Probe 1 is complete, all four criteria pass against Elden Ring under EAC; Wayland (Probe 2) is still open
- [Windows Mouse Filter Plan](vision/WINDOWS_MOUSE_FILTER_PLAN.md): the Windows counterpart, a mouclass upper filter that hands the physical mouse to Nimbus, motivated by the Raw Input measurements in section 8 of Host Mode
- [Pad Bus Fork Plan](vision/PAD_BUS_FORK_PLAN.md): forking and modernizing the archived ViGEmBus into a Nimbus-owned virtual gamepad bus driver; what the driver really does, a pure-Python client that drops the `vgamepad` dependency first, the rename and coexistence inventory, and the anti-cheat gate that decides whether we ever sign it
- [Nimbus Mouse Filter driver README](../driver/README.md): building, test-signing, and dev-installing the kernel filter (prototype, not in any release)
- [Driver release signing](../driver/SIGNING.md): Partner Center registration, attestation signing, and where attestation stands after the April 2026 Windows Driver Policy
- [Linux Gaming Technical Proposal](vision/LINUX_GAMING_PROPOSAL.md): a Linux/X11 port of Nimbus, input ownership, session recovery, game presets, and phased acceptance criteria; a separate platform track, not the host this project targets
- [Virtual Machine Feasibility](vision/VIRTUAL_MACHINE_FEASIBILITY.md): running the game in a guest on the Windows host, why the single-GPU dev machine and Hyper-V leave only GPU-PV, the controller transport that would be needed, and why the measured mouse filter beats all of it; section 10 is the 2026-09-13 build and run (the `vm/` scripts, Gate A answered from published policy, Gates B and C passed on the dev machine through the real Moonlight, Sunshine and ViGEm chain, and Gate D's latency axis measured: the guest takes two to three frames at the median and three to five at the 95th percentile, against one natively, so the conclusion holds)
- [Input Ownership: Problem Statement](vision/PROBLEM_STATEMENT.md): the problem behind the SteamOS, VM, separate-machine and network pad tracks stated once, with facts, decisions, the section 4 yardstick, every boundary from game settings to a second computer with its evidence, and the questions that decide it for Forza Horizon 6
- [Input Ownership: Solution Pass](vision/INPUT_OWNERSHIP_SOLUTION.md): a proposal against that statement; test the game's own behavior and the device's input path first, add native Linux on the same PC, give the pad a lifetime independent of the UI with a lease-based failsafe, and keep the Windows filter conditional on recovery evidence
- [Separate Game Machine](vision/SEPARATE_GAME_MACHINE.md): the game on its own computer (SteamOS, a Steam Deck, another PC) and Nimbus on the player's; a streamed layout and a game-on-its-own-screen layout, two software pad transports (a streaming client, a network receiver over uinput), what Nimbus gains and loses, and a phased test plan (software only, no hardware); section 10 is the receiver prototype in `netpad/`, built and measured against the VM guest on 2026-09-14 (Stop 15 ms, connection loss 156 ms, 21 end-to-end checks pass)

## Distribution & Sustainability
The business model, open-core playbook, release strategy, sponsorship outreach, voice command and Spectator+ concept documents are kept in a private repository and are not part of this one. What exists of Spectator+ in code is described here:
- [Game Test Harness, section 4.7](vision/GAME_TEST_HARNESS.md): Spectator+ v0, scripted primitives (turn, walk, press) calibrated per game and measured in a real game, and what is not built yet

## Media
- [Screenshots](screenshots/) — application UI screenshots
- [Video](video/) — demo videos

---

## Key Files for AI Assistants

| Task | File |
|------|------|
| Understand project structure | [../DIRECTORY.md](../DIRECTORY.md) |
| Make UI changes | `qml/Main.qml`, `qml/layouts/CustomLayout.qml` |
| Add QML↔Python features | `src/bridge.py` |
| Build releases | [setup/PACKAGING.md](setup/PACKAGING.md) |
| Bug fixes / implementation notes | [development/LLM_NOTES.md](development/LLM_NOTES.md) |
