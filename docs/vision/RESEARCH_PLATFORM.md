# Nimbus as a Research Platform

## The Idea

Nimbus Adaptive Controller sits at an unusual intersection: it is both a **functional assistive tool** and a **data collection surface** for understanding how people with disabilities interact with games and applications. Every session generates rich, anonymizable behavioral data — which inputs are used, how axes are configured, what profiles are loaded, how long sessions last, where users struggle — that no academic lab or game studio currently has at scale.

> *"We don't actually know how disabled gamers play. We have surveys. We don't have telemetry."*

Nimbus could change that — with user consent, with IRB-appropriate design, and with the disability community as genuine partners rather than subjects.

---

## What Data Nimbus Could Collect

The existing architecture already captures or could trivially capture:

| Data Point | What It Reveals | Already Available |
|------------|----------------|-------------------|
| Profile configuration (axis mappings, deadzones, sensitivity curves) | What adaptations users make | ✅ JSON profiles |
| Layout type used (flight_sim, xbox, adaptive, custom) | What control paradigms work | ✅ Profile system |
| Session duration | Fatigue patterns, engagement | Easy to add |
| Input frequency per axis/button | Which inputs are hardest to sustain | Easy to add |
| Profile switching frequency | How often users need to adapt mid-session | Easy to add |
| Error/retry patterns | Where the UI causes friction | Easy to add |
| Game focus mode usage | Which games require workarounds | Easy to add |
| Voice command usage (Pro tier) | Which commands are most used/failed | Easy to add |
| Hardware connected (vJoy vs ViGEm) | What hardware ecosystem users have | Easy to add |

**What this data could answer:**
- What are the most common adaptive configurations for specific disability types?
- Which games are most frequently played with adaptive setups?
- How does input fatigue manifest over a session?
- What profile features are used vs. ignored?
- Are there configuration patterns that predict successful gameplay?

---

## Why This Matters

### For the Research Community

There is almost no large-scale behavioral data on how people with disabilities actually play games. Existing research relies on:
- Small lab studies (n=10–30)
- Self-reported surveys
- Anecdotal reports from occupational therapists

Nimbus could provide **longitudinal, real-world, at-scale behavioral data** from actual gaming sessions — something no university lab can replicate.

### For Game Studios

Studios want to make accessible games but don't know what "accessible" means in practice. Aggregate, anonymized data from Nimbus users could tell them:
- Which control schemes are most commonly adapted
- What deadzone and sensitivity settings disabled players actually use
- Which games are most played with adaptive controllers (and thus worth prioritizing for accessibility)

This data has commercial value to studios — a potential revenue stream or partnership angle.

### For Funders and Partners

Research platform framing opens doors that a "gaming tool" framing doesn't:
- **NIH / NSF grants** (human-computer interaction, assistive technology)
- **University partnerships** (IRB-approved data sharing agreements)
- **AbleGamers, SpecialEffect** as co-investigators
- **Microsoft Research, Google Research** as industry partners

---

## Ethical Framework

This only works if it's done right. The disability community has a long history of being researched *on* rather than researched *with*. The framework must be:

### Nothing Without Consent
- Opt-in telemetry only — never on by default
- Clear, plain-language explanation of what is collected
- Easy opt-out at any time
- No data collection for free-tier users unless they explicitly opt in

### Community Ownership
- Aggregate findings shared back with the community
- Disability organizations (AbleGamers, SpecialEffect, etc.) have input on research questions
- Users can request deletion of their data

### IRB / Ethics Review
- Any formal research use requires IRB approval (or equivalent)
- University partnership model: Nimbus provides the platform, researchers provide IRB oversight
- Data sharing agreements with academic partners

### Anonymization
- No PII collected
- Device fingerprinting only with consent
- Profiles stripped of any identifying information before aggregation

---

## Architecture: How Telemetry Would Work

### Opt-In Telemetry Module

```
src/
└── telemetry/
    ├── collector.py       # Captures session events locally
    ├── anonymizer.py      # Strips/hashes any identifying data
    ├── uploader.py        # Sends to research backend (with consent)
    └── consent_manager.py # Manages user opt-in state
```

### Data Flow

```
User session
    ↓
collector.py captures events locally (always, even if not uploading)
    ↓
[If user has opted in]
    ↓
anonymizer.py strips PII, hashes device ID
    ↓
uploader.py sends to research API (nimbus-platform)
    ↓
Research database (aggregated, never individual-level public)
    ↓
Academic partners / published findings / community reports
```

### What "Opt In" Looks Like in the UI

A one-time prompt on first launch (after the user has used the app for a session or two):

> *"Help us understand how people with disabilities game.*
> *Nimbus Adaptive Controller can anonymously share your session data — which inputs you use, how long you play, how you configure your controller — with disability gaming researchers.*
> *No personal information is collected. You can opt out at any time.*
> [**Yes, contribute to research**] [**No thanks**] [**Learn more**]*"

---

## Potential Research Partners

| Organization | Type | Why They'd Care |
|-------------|------|----------------|
| **AbleGamers** | Nonprofit | Core mission alignment; could co-publish findings |
| **SpecialEffect** | Nonprofit (UK) | Deep OT expertise; international data |
| **Shirley Ryan AbilityLab** | Rehab hospital | Clinical research infrastructure, IRB |
| **University of Pittsburgh SHRS** | Academic | Rehab science, AT research |
| **Microsoft Research** | Industry | Accessibility AI, gaming research |
| **Carnegie Mellon HCII** | Academic | HCI, accessibility, game research |
| **Georgia Tech Assistive Technology** | Academic | AT research center |
| **Craig Hospital** | Rehab hospital | SCI/TBI population, gaming therapy |

---

## Research Questions Worth Pursuing

1. **Configuration patterns by disability type** — Do users with SCI configure differently than users with CP or MS? What are the common patterns?
2. **Fatigue signatures** — Can input frequency data detect fatigue onset? Could Nimbus proactively suggest breaks or profile adjustments?
3. **Game accessibility correlation** — Which games have the most adaptive configurations? Does this correlate with official accessibility ratings?
4. **Voice vs. physical input** — When voice commands are available, how do users blend them with physical inputs?
5. **Profile evolution** — How do users' configurations change over time? Do they converge on stable setups or keep experimenting?
6. **Spectator+ effectiveness** — When AI assistance is available, how much do users actually use it? What assistance levels are most common?

---

## Connection to Funding

The research platform angle significantly strengthens grant applications:

- **NIH R21/R01** (exploratory/full research grants) — AT + HCI research
- **NSF CISE** (human-centered computing)
- **Patient-Centered Outcomes Research Institute (PCORI)** — patient-engaged research
- **Robert Wood Johnson Foundation** — health equity, disability
- **Wellcome Trust** (UK, for SpecialEffect partnership)

See [`RELEASE_STRATEGY.md`](../distribution/RELEASE_STRATEGY.md) for the full grant landscape.

---

## Next Steps

1. **Define the minimum telemetry set** — What 5–10 data points would be most valuable? Start small.
2. **Draft a consent UI** — Plain language, disability-community reviewed
3. **Identify a university partner** — One IRB-approved research partner to start
4. **Build `src/telemetry/collector.py`** — Local-only first; upload later
5. **Write a research brief** — 1-page document for potential academic partners

---

## Related Documents

- [Spectator+ v0 (Game Test Harness, section 4.7)](GAME_TEST_HARNESS.md): scripted primitives through the bridge; assisted play would generate additional behavioural data
- [Spectator+ v1 (Game Test Harness, section 4.8)](GAME_TEST_HARNESS.md): the closed loop, which is what makes "how much assistance did this user ask for, and did it help" a measurable question rather than a survey one
- [Voice Command Integration](../distribution/VOICE_COMMAND.md) — voice usage patterns are a research signal
- [Release Strategy](../distribution/RELEASE_STRATEGY.md) — grant funding landscape
- [AAC Integration](AAC_INTEGRATION.md) — research platform extends to AAC use cases
- [Modular Control Surface](MODULAR_CONTROL_SURFACE.md) — research extends beyond gaming
