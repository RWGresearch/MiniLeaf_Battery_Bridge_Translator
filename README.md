# MiniLeaf Battery Bridge Translator

Drive a **Lexus RZ450e high-voltage battery pack** (96 cells, NMC, ~348V) as a working
replacement for a **Nissan Leaf HV battery**, so a real Leaf's VCM/BCM believes it's talking to a
real Leaf pack — while a configurable management layer actively protects the RZ450e pack (voltage,
temperature, SoC, charge tapering) the way a real BMS would.

This is a **bridge with a brain**, not a passive protocol translator: it reads live RZ450e CAN
data, applies user-configured safety limits and power management, and transmits correct,
on-schedule Leaf HVBAT CAN frames.

## Status

Documentation/planning stage. See [`docs/01-project-goals.md`](docs/01-project-goals.md) for the
full picture. No application code has been written yet.

## Two phases

- **Phase 1 (active)** — a Python/customtkinter GUI app, run on a PC with two (up to eight) PEAK
  PCAN-USB adapters. It's both a **configurator** (build the signal-mapping and battery-management
  config with no code) and a **live bridge** (real CAN in from the RZ450e pack, real CAN out to the
  Leaf car) — used to validate every mapping and safety feature against real hardware.
- **Phase 2 (future, separate project)** — an STM32 firmware port of the same bridge logic, driven
  directly by the config this app exports.

## Where the underlying reverse-engineering comes from

This project builds on two other, independent projects, kept here read-only as reference material
under `Refrance/` (each has its own git history and is the living source of truth for anything not
yet adapted into `docs/`):

- **`Refrance/RZ450e_battery_can_decode_Project/`** — the confirmed, real-hardware-validated CAN
  bus decode of the Lexus RZ450e HV battery (raw broadcast signals + UDS diagnostic DIDs/PIDs).
- **`Refrance/Leaf_BMS_Emulator/`** — the confirmed, real-hardware-validated Nissan Leaf HVBAT
  emulator (what the Leaf's VCM/BCM needs to see on the EV-CAN bus, including startup/shutdown
  timing).

## Documentation

Start at [`docs/01-project-goals.md`](docs/01-project-goals.md) — it lists every doc in this set
in reading order, covering: available RZ450e signals, required Leaf signals, how they map to each
other, the battery-management/safety layer, the real-time CAN engine, startup/shutdown, the GUI
design, the config format that doubles as the future STM32 export spec, open questions, and a
working manual-verification checklist.
