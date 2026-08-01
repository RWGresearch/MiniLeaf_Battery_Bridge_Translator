# Project Goals

## Read this order

This `docs/` folder follows the same numbered, cross-linked reference style used by both source
projects in `Refrance/` (each file states what's confirmed vs. inferred, and links to where the
data actually came from). Read in order:

1. **`01-project-goals.md`** (this file) — mission, phases, what "done" looks like.
2. **`02-source-signals-rz450e.md`** — what data is available from the Lexus RZ450e battery.
3. **`03-target-signals-leaf.md`** — what data the Nissan Leaf needs to receive.
4. **`04-signal-mapping.md`** — how the two are wired together, and the 5 structural mismatches.
5. **`05-battery-management-safety.md`** — the configurable BMS/protection layer sitting between
   input and output (this is the most important doc in this set — this project is not a passive
   translator, it actively manages the battery).
6. **`06-realtime-engine-and-watchdog.md`** — the always-on-schedule TX engine, startup cache, and
   staleness watchdog.
7. **`07-startup-shutdown-plan.md`** — the ported Leaf power-up/power-down state machine.
8. **`08-gui-design.md`** — the actual screen layout.
9. **`09-stm32-export-format.md`** — the config schema this app exports, which doubles as the spec
   for the future hardware port.
10. **`10-open-questions.md`** — everything still unresolved, inherited or new.
11. **`11-manual-verification-checklist.md`** — working doc, tracks default-vs-real-hardware-
    confirmed status per feature as testing happens.
12. **`12-nmc-bms-design-research.md`** — researched NMC/BMS design fundamentals (voltage,
    temperature, C-rate, imbalance, protection architecture) with sources, plus an audit of our
    design against them. Findings F1, F3, F4, F5, F6 implemented; F2 implemented as monitor-only
    by deliberate scope choice; F8 (fault latching) discovered but not yet implemented — needs a
    decision.

## Mission

Take a **Lexus RZ450e high-voltage battery pack** (96 cells, NMC chemistry, ~348V, fully
reverse-engineered in `Refrance/RZ450e_battery_can_decode_Project/`) and drive it as a **working
replacement for a Nissan Leaf HV battery** (fully reverse-engineered and emulated in
`Refrance/Leaf_BMS_Emulator/`), so a real Leaf's VCM/BCM believes it's talking to a real Leaf
pack — while also actively managing the RZ450e pack's safety limits, since its native BMS
protocol is now hidden behind this bridge instead of exposed to a Toyota vehicle.

This is **not** a simple signal-relay/protocol-translator. It is a small standalone BMS: it reads
real RZ450e data, decides what's safe, and drives the Leaf-side outputs (including cutoffs and
power derating) accordingly. See `05-battery-management-safety.md`.

## Two phases

### Phase 1 (this project, active now)

A Python GUI application, run on a PC with two (up to eight selectable) PEAK PCAN-USB adapters:
- One combined connection to the RZ450e battery (one physical adapter carries every RZ450e CAN ID
  — diagnostic/DID traffic and the fast internal-bus broadcasts together, split by CAN ID rather
  than by physical bus — reads real cell voltages, temps, current, SoC, etc. See
  `02-source-signals-rz450e.md`.)
- One connection to the Leaf car (transmits real HVBAT frames the VCM/BCM expects)

This app is **both**:
- **A configurator** — build, edit, and save the signal-mapping + battery-management config
  without writing code.
- **A live bridge** — actually reads live RZ450e CAN data and transmits live Leaf CAN data. This
  is the test rig used to confirm every mapping, conversion, and safety feature actually works
  against real hardware before anything is ported to a standalone board.

Whatever configuration this app saves is also the artifact phase 2 needs (see
`09-stm32-export-format.md`).

### Phase 2 (separate future project)

Port the same bridge logic to an STM32 microcontroller, so the whole system can run standalone in
a vehicle without a PC. Driven directly by the config this app exports — not a rewrite from
scratch.

## Definition of done for phase 1, milestone 1 ("core bridge")

- Two PCAN adapters connect (one combined RZ450e connection, one Leaf connection), selectable from
  up to 8 detected channels, with auto-reconnect — same pattern as `rx450e_can_analyzer.py`'s
  `detect_pcan_channels()`.
- Every confirmed RZ450e input signal (raw CAN + DID/PID) is available in a live registry, visible
  in the GUI.
- Every Leaf output signal (from the Leaf's `02-signal-dictionary.md`) is available as a mapping
  target, with generated/opaque fields represented as send/don't-send checkboxes instead.
- The battery-management panel (`05-battery-management-safety.md`) ships with the researched
  default thresholds, fully editable and saveable.
- The Leaf-facing TX loop sends every HVBAT ID on its correct fixed period, continuously, driven
  by the real-time engine in `06-realtime-engine-and-watchdog.md` — not just when new RZ450e data
  arrives.
- The ported startup/shutdown state machine (`07-startup-shutdown-plan.md`) runs correctly against
  a real Leaf VCM, gated by a manual **Start Bridge / Stop Bridge** control rather than starting
  automatically at app launch — RZ450e monitoring and mapping/threshold edits work before Start is
  pressed; transmission itself only begins once armed AND real Leaf-bus traffic is seen (added
  2026-07-31, see `06-realtime-engine-and-watchdog.md` section 0).
- Config (mapping + thresholds + last-known-good values) saves and reloads across restarts.
- A bottom log panel (connection events, sequencer phase transitions, soft/hard-cut assertions) and
  a separate large Dashboard window (bar gauges, input→conversion→output per signal) — both added
  after the user's first review pass, see `08-gui-design.md`.

Milestone 2 (the UDS/LeafSpy diagnostic responder) and milestone 3 (STM32 port) are separate
follow-on efforts — not required for milestone 1's "done."

## Hardware

- **RZ450e side**: bench-mounted Lexus RZ450e HV battery, two internal CAN buses (main/diagnostic
  at 500kbit/s, internal cell-monitoring bus) but **one physical adapter connection** — split by
  CAN ID internally, not two separate connections. Addressed exactly as documented in
  `02-source-signals-rz450e.md`.
- **Leaf side**: Nissan Leaf HVBAT position on the EV-CAN bus (AZE0 or ZE1 generation).
- **Adapters**: PEAK PCAN-USB (IPEH-002022 or similar), the same hardware family both reference
  projects already use. Up to 8 selectable channels, 2 required minimum (one RZ450e, one Leaf).

## GUI toolkit

**Plain tkinter/ttk** — both reference apps' own choice (`ttk.LabelFrame`, grid layout,
`StringVar`/`tk.Variable` bindings, a `ttk.Style` dark theme). Milestone 1 briefly used
customtkinter instead (a "clean, modern" skin on top of tkinter), but the user reverted that on
2026-07-31: customtkinter's per-window DPI-scaling tracker and custom canvas-based widget
rendering made the app noticeably slower to load than the reference apps' plain-tkinter style,
which is what was actually wanted. `gui/theme.py` ports the Leaf project's own `_style()` dark
theme (color constants, `ttk.Style` configuration) plus a small `VScrollFrame` helper (ttk has no
native scrollable-frame widget) — no GUI dependency beyond the Python standard library.

## What `Refrance/` is, and isn't

`Refrance/Leaf_BMS_Emulator/` and `Refrance/RZ450e_battery_can_decode_Project/` are two other,
independent Claude Code projects (each with their own git repo), kept here **read-only** as
reference material. This project's own docs adapt and re-derive what's needed from them, but
never edits them. If something here turns out wrong and the live reference project has since been
corrected, treat the reference project as authoritative and update this project's docs, not the
other way around — same rule both reference projects already apply to each other's supporting
files (DBCs, memory, etc.).
