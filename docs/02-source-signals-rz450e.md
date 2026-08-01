# Source Signals — Lexus RZ450e Battery

Adapted from `Refrance/RZ450e_battery_can_decode_Project/project overview dump/` (generated
2026-07-29 from `rx450e_can_analyzer.py` rev 141, sign-off pass 2026-07-30). **Only signals marked
`confirmed` in that project are listed here** — this project inherits that confirmed/unconfirmed
discipline rather than re-litigating it. If a signal isn't here, treat it as unavailable until the
source project promotes it (check `Refrance/RZ450e_battery_can_decode_Project/project overview
dump/OPEN_VALIDATION_ITEMS.md` for what's pending and why).

## Hardware / addressing

- Lexus RZ450e HV battery, bench-mounted (no motor/inverter/body ECUs present in the source
  project's test rig — this project's own bridge context restores that missing "vehicle" role, on
  the Leaf side).
- 96 cells in series across 4 physical sub-packs, NMC chemistry, ~400V nominal platform, ~348V
  resting measured, ~201Ah nameplate / 194.97Ah measured (97.0% SOH).
- Two logical CAN buses: **Bus 1** (main/diagnostic, 500kbit/s, standard OBD2 + Toyota extended
  UDS) and **Bus 2** (internal cell-monitoring, periodic broadcast only, no diagnostic requests
  answered). **On this project's actual hardware these are not two separate physical connections**
  — one PEAK PCAN-USB adapter carries both, split purely by CAN ID (the two logical buses' ID
  ranges don't overlap), matching the "combined bus" mode the RZ450e decode project's own
  `rx450e_can_analyzer.py` already supports. The bridge app therefore has **one** RZ450e connection,
  not two — see `docs/08-gui-design.md`.
- Addressing: standard OBD2 functional broadcast `0x7DF`; Toyota extended tester→BMS `0x747`;
  Toyota extended BMS response `0x74F`.

## Fast raw-CAN broadcast signals (bus 2 unless noted) — PRIMARY sources for the bridge

These update every ~8ms-500ms and should be preferred over DID/PID polling wherever a quantity is
available both ways (see `04-signal-mapping.md`).

| CAN ID | Signal(s) | Scale/unit | Notes |
|---|---|---|---|
| `0x020` | `pack_v` (whole V), `cell_min`, `cell_max` | cell fields: `raw × 5/4096` V | Pack-level summary — use as a **sanity cross-check** against the per-cell messages below, not as the primary per-cell source (per the RZ450e project's own confirmed design intent). |
| `0x023` | `current`, `current_b` | `raw × 0.1 − 204.8` A (12-bit, saturates ±204.7A) | **+ = discharge (out of pack), − = charge (into pack)**. Fastest current signal available (~8ms). **The ±204.7A ceiling is a decode/encoding limit of this specific 12-bit CAN signal, NOT a limit of the battery itself** (user correction, 2026-07-31 — see the note below the table). This bench pack has never been tested past ~200A (a bench-setup limitation), but the real pack is rated well beyond what this sensor can even represent. |
| `0x4A7` | `temp_max`, `temp_min`, + probe # | `raw × 1.8 + 32` °F | Pack-level temperature extremes. |
| `0x4A9` + `0x4C0` | `cell_01`..`cell_96` (muxed) | `raw × 5/4096` V (12-bit) | **All 96 individual cell voltages.** This is the primary per-cell source — use for the low/high-voltage protection features in `05-battery-management-safety.md`, not the `0x020` pack summary. |
| `0x4AA` | `temp_01`..`temp_16` (muxed) | `raw × 1.8 + 32` °F | All 16 temperature probes. |
| `0x1BE` (bus 1) | `temp_coolant` | `raw × 1.8 + 32` °F | |
| `0x353` | `pack1_avg_v`, `pack2_avg_v`, `pack3_avg_v` | `raw × 5/4096` V | |
| `0x357` | `pack4_avg_v` | `raw × 5/4096` V | |
| `0x358` | `charge_permission_input` (bit flag), `alive_counter` | — | **Confirmed electrical input via a physical wire-toggle test.** Design intent (user, 2026-07-30): use as a **charging interlock** — if not active, a charge request must not proceed. `alive_counter` usable for the staleness watchdog (`06-realtime-engine-and-watchdog.md`). |
| `0x3F1` | `alive_counter` | 4-bit, wraps every 16 frames | Usable for the staleness watchdog. |
| `0x424` | `counter_5s` | +1 every 5.000s, wraps mod 73 | Usable for the staleness watchdog. |

**Checksum**: 10 of these 12 messages carry Toyota's additive checksum in byte 7 —
`(ID_hi + ID_lo + DLC + sum(bytes[0:7])) & 0xFF`, confirmed 100% match across thousands of frames.
The RZ450e project chose not to wire this into its own downstream logic, but **this project
should**, as an additional staleness/corruption check (`06-realtime-engine-and-watchdog.md`).

**Multiplexing**: `0x4A9`/`0x4C0` round-robin `cell_block_mux` 0,4,8,...92 (4 cells per frame);
`0x4AA` round-robins `temp_block_mux` 0,7,14. A cantools-based decode against the DBC handles this
automatically — see `Refrance/RZ450e_battery_can_decode_Project/project overview
dump/confirmed_signals_only.dbc`.

### Real pack current capability vs. this project's current SENSOR ceiling (user correction, 2026-07-31)

Don't confuse three separate, easily-conflated numbers:

1. **`0x023`'s own 12-bit encoding ceiling: ±204.7A.** This is a property of the CAN *signal*
   itself (a fixed-width field), not the battery. Above this, the raw value simply cannot represent
   a larger number — there is no way to tell the difference between "204.7A" and "400A" from this
   signal alone.
2. **This bench setup's own test history: never exceeded ~39.5A, never intentionally pushed past
   ~200A.** That's a limitation of the bench rig (fuses/wiring/test conditions used so far), not a
   statement about what the pack can safely do.
3. **The real pack's actual rated capability, per the user directly (2026-07-31), is far beyond
   both of the above**:
   - **AC (L2) charging**: 6.6 kW via the Leaf's onboard charger ≈ 19A — matches the existing
     `05-battery-management-safety.md` figure, no conflict here.
   - **DC fast charging**: the pack is rated for **150 kW** DC fast charging — at ~348V nominal,
     that's roughly **~430A** of charge current. **Not currently in scope for this bridge** (no DC
     fast-charge signal path exists in `03-target-signals-leaf.md` today) — see the new open
     question in `10-open-questions.md`.
   - **Discharge**: the pack is protected by an in-line **500A slow-blow fuse** — the real
     hard ceiling for sustained discharge current.
   - **Peak/burst discharge**: the factory RZ450E has a rated max output power of **230 kW**, which
     at ~348V is roughly **~660A** for short bursts — well past both the fuse's steady rating and
     this project's own current sensor's encoding ceiling.

**Net effect: this project's only fast current signal (`0x023`) cannot see, and therefore cannot
protect against, anywhere near this pack's real operating current range.** Any current-based
feature (see `overcurrent_monitor`, `05-battery-management-safety.md`) is fundamentally limited by
this sensor gap, not by the pack's real capability — a future hardware revision (a wider-range
current sensor/shunt, or a second signal source) would be needed to actually monitor current up
near the 500A fuse rating or the 230kW/~660A peak figure.

## Diagnostic (DID/PID) signals — SECONDARY sources, slow (4-9s poll)

Use only for quantities with no fast raw-CAN equivalent above; hold the last value between polls.

| Signal | Request | Decode | Poll cadence |
|---|---|---|---|
| **SoC** | DID `0x1F5B`: `03 22 1F 5B` (or PID `0x5B` via `0x7DF`) | `d[3] × 100.0 / 255.0` → % | Effectively continuous but internally re-aggregated; ~4-9s meaningful update |
| **Pack capacity / SOH** (×4 packs) | DID `0x1D3E`: `03 22 1D 3E` | `uint16@N / 100.0` → Ah per pack; `SOH% = measured_Ah / 201.00 × 100` | Very slow-changing (SOH), poll infrequently |
| **Primary voltage + current reference** | DID `0x1F9A`: `03 22 1F 9A` | Voltage `uint16@5 / 64.0` → V (**use over 0x1002, which is confirmed wrong**); current `int16@7 × 0.1` → A (same sign convention as `0x023`) | ~9.0s/poll — slowest regular signal in the system, too slow for live control, only useful as a cross-check |
| **96 cell voltages** (full snapshot) | DID `0x182E`: `03 22 18 2E` | `uint16@(3+(N-1)*2) × 5.0/65535.0` → V | Needs ~10s between reads — **use the raw-CAN `0x4A9`/`0x4C0` broadcast instead for anything live**; this DID matters for the future UDS/LeafSpy responder (milestone 2), which will answer the Leaf's own diagnostic queries with this same underlying per-cell data. |
| **16 temperatures** (full snapshot) | DID `0x1814`: `03 22 18 14` | `(uint16@(3+(N-1)*2)/256.0 − 50.0) × 9/5 + 32` → °F | Same note as above — prefer `0x4AA` broadcast for live use. |
| **Outlet air temps A/B** | DID `0x1832`: `03 22 18 32` | `(d[3 or 4] − 50) × 9/5 + 32` → °F | |

## Current sign convention (critical, see `04-signal-mapping.md`)

**RZ450e: positive = discharge (out of pack), negative = charge (into pack).** Validated at
100.0000% agreement across 162k+/342k+ samples between raw CAN `0x023` and two independent
DID/PID current references. Applies to `0x023`, DID `0x1F9A`, and PID `0x9A`. **This is the
opposite convention from the Leaf side** — see the mapping doc for the required inversion.

## Explicitly excluded (do not use)

- DID `0x1002` (pack voltage) — confirmed wrong (tracks ~1/10th real rate, sometimes wrong
  direction).
- DID `0x10A1` (current) — frozen at 0.000A, dead mirror on this pack.
- PID `0x46` (ambient temp) — no physical sensor wired on the bench setup.
- Pack-stat blocks `0x313`/`0x314`/`0x351`/`0x352` — held back by the source project pending a
  full charge/discharge cycle validation.
- `0x371`/`0x021` (wide-range current/power tap) — amps-vs-kW is still genuinely unresolved
  upstream; do not use until that's settled (see `10-open-questions.md`).
- `0x4AF` (usage-history table) — row index confirmed, payload meaning not.
- DID `0x182F` (cell internal resistance) — reads exactly 0.0000Ω in every sample so far, no
  moving data to confirm the scale against.

## Source of truth

This is a snapshot. If a number here is ever found wrong, or a new signal gets confirmed
upstream, treat `Refrance/RZ450e_battery_can_decode_Project/` as authoritative and update this
file — never the reverse. Full evidence trails live in that project's `reports/*.md` and its own
memory system.
