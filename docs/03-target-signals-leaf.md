# Target Signals — Nissan Leaf HVBAT

Adapted from `Refrance/Leaf_BMS_Emulator/battery emulator overview/` (a byte-level, real-hardware-
confirmed extraction of `leaf_hvbat_emulator.py`, rev 42+). **Exact frame bit-packing is not
re-derived here** — this project should port/reuse the Leaf project's own `build_1db`, `build_1dc`,
`build_55b`, `build_5bc`, `build_59e`, `build_5c0`, `build_5eb`, `build_1c2`, `build_1ed`, and
`crc8()` functions directly rather than re-transcribing byte formulas by hand, to avoid introducing
transcription bugs into a safety-relevant CAN transmitter. This doc covers what each mapping
*target* means and its scale/range, which is what the GUI and mapping engine need.

## What battery

Nissan Leaf HV battery pack, two generations: **AZE0** (2013-2017, 30kWh) and **ZE1** (2018+, 40 or
62kWh). Car and battery generation can differ independently (e.g. a ZE1 pack swapped into an AZE0
car) — see `Refrance/Leaf_BMS_Emulator/battery emulator overview/07-vehicle-variants.md` for which
CAN IDs exist on which combination. This project's GUI needs a vehicle/battery-generation selector
mirroring the Leaf app's own `Core.set_vehicle()`.

## Periodic CAN IDs (all must transmit on a fixed schedule — see `06-realtime-engine-and-watchdog.md`)

Base 6, present on every generation/capacity: `0x1DB`, `0x1DC`, `0x55B`, `0x5BC`, `0x59E`, `0x5C0`.
Plus `0x1C2` always; `0x5EB` if battery=ZE1; `0x1ED` if battery=ZE1 62kWh.

## Mapping targets (signals a source can drive)

| Item | CAN ID | Scale | Range | Unit | Confirmed default |
|---|---|---|---|---|---|
| Pack voltage | `0x1DB` | 0.5/bit | 0-450 | V | 378.5 |
| Pack current | `0x1DB` | 0.5/bit | −400…+200 | A | 0.0 — **positive = charging** (opposite of RZ450e, see `04-signal-mapping.md`) |
| Usable SOC | `0x1DB` | 1/bit | 0-100 | % | 45 (VCM-internal use, not the dash %) |
| Discharge power limit | `0x1DC` | 0.25/bit | 0-255.75 | kW | 110.0 — managed by `05-battery-management-safety.md`, not a raw passthrough |
| Charge/regen power limit | `0x1DC` | 0.25/bit | 0-255.75 | kW | 70.0 — managed |
| Max power for charger | `0x1DC` | 0.1/bit | −10…92.3 | kW | 92.3 idle — **the critical live AC-charge control**, managed by the CC→CV taper |
| Fine SOC | `0x55B` | 0.1/bit | 0-102.3 | % | 46.3 |
| SOH / capacity deterioration | `0x5BC` | 1/bit | 0-127 | % | 100 — derive from RZ450e's `SOH% = measured_Ah / 201.00 × 100` |
| Remaining capacity (GIDS) | `0x5BC` | 1/bit | 0-1023 | gids (~80Wh each) | 120 — derived, no RZ450e equivalent, see `04-signal-mapping.md` |
| ChargeBars/CapacityBars raw | `0x5BC` | 1/bit | 0-15 | raw | 6 — real mapping (2026-07-30 test): 0-14 = full bar display, 15 = all segments off. **Live RZ450e-driven mapping confirmed 2026-07-31**: `capacity_pack1_ah` (0-200 Ah working range, DID `0x1D3E`) → this field, linear `scale = 0.07` (i.e. `raw = capacity_ah × 14/200`), user-confirmed against the real dash SOH/capacity bar display — shipped as a default tie in `bridge/mapping_engine.py`. |
| Dash temperature segment | `0x5BC` | 0.4167/bit | 0-100 | % | 58.3 |
| QC full/remaining capacity | `0x59E` | 100/bit | 0-51100 | Wh | 23000 / 10500 — derived |
| Dash SOC % display (`soc_correction`) | `0x59E` | 2 counts/% | 0-200 raw = 0-100% | % | 90 — **this is the actual physical dash SOC%** (not Usable SOC above); raw-to-percent formula user-confirmed on real hardware 2026-07-31 (`soc_correction_raw = rz450e_soc_pct × 2.0`), resolving an open question inherited from the Leaf project — see `04-signal-mapping.md` mismatch #2 |
| Battery temperature | `0x5C0` | 1/bit | −40…86 | °C | 37 |
| 0x1ED charger-limit field | `0x1ED` | 0.1/bit | 10-204 | kW | **UNVERIFIED upstream** — no real 62kWh capture exists yet in the source project |

## Charge-request ramp emulation (added 2026-07-31, see `06-realtime-engine-and-watchdog.md`)

"Max power for charger" above is normally just whatever the Signal Mapping tab (or the idle
92.3kW placeholder) produces. The **Charge Emulation** GUI tab adds a feature (default on as of
2026-08-01, individually toggleable), ported from `Refrance/Leaf_BMS_Emulator`'s confirmed
real-hardware behavior (bit-level diff of every HVBAT ID, idle vs. real charge-session captures):

- **`charge_emulate`** (checkbox, default **on** as of 2026-08-01 — "the charge option should be
  set to on as default") — enables the feature at all.
- **`charge_target_kw`** (0-92.2 kW) — the ramp's target ceiling.
- **`chg_uprate_level`** (0-7) — ramp rate; level 7 = 2.0 kW/s, each level down halves it. Rides
  along on the `0x1DC` uprate bits (byte 4 bits 5-7), 0 at idle.
- **`require_live_data_to_charge`** (checkbox, default on) — a one-time per-session startup gate:
  the ramp will not begin until every per-cell voltage and both `temp_max`/`temp_min` have a
  genuinely live (not cached/default/previous-session) reading this bridge session. Not an ongoing
  freshness timer — once satisfied for the session it stays satisfied; ongoing staleness protection
  during an active charge is the general staleness watchdog's job, same as driving
  (`06-realtime-engine-and-watchdog.md`).

**Requires BOTH triggers at once** (user directive, 2026-07-31) — a real `0x1F2` charge request
from the Leaf (`Charge_StatusTransitionReqest == 1` or `CommandedChargePower` above idle) **and**
RZ450e's own `charge_permission_input` interlock (`0x358`) granting it. With both present, "Max
power for charger" snaps from 92.3kW idle to 0.0 kW and ramps to `charge_target_kw`, overriding
whatever the mapping engine produced for that field.

If the Leaf is asking to charge but RZ450e has **not** authorized it, this is treated as a genuine
mismatch, not "keep waiting": it forces `full_charge_flag = 1` (see below — the confirmed
"instant stop, needs a physical replug" bit) plus `charge_limit_kw = 0.0` and
`charger_limit_kw = -10.0`. The same mismatch also feeds `ShutdownSequencer._should_wind_down`'s
`charge_authorized` parameter (`06-realtime-engine-and-watchdog.md`), so the bridge is free to wind
down/sleep instead of staying awake indefinitely just because the Leaf keeps asking — a genuine
replug is what produces a fresh `0x1F2` request in the first place. Like the rest of this bridge,
this is computed fresh every tick (not a sticky latch) — see the open re-arm question below.

The per-cell overvoltage taper (`05-battery-management-safety.md`'s `ac_charge_taper` — split from
`charge_target_taper` 2026-08-01, since `charge_target_taper` now governs only `charge_limit_kw`)
still gets the final say over the ramped value every tick while a real charge session is active — it
can reduce it further; it is never bypassed by this feature.

**Charging and driving are two fully separate control paths for these two fields** (user directive,
2026-08-07 — confirmed against `Refrance/Leaf_BMS_Emulator`'s bit-level-diff real-hardware notes:
`LB_MAX_POWER_FOR_CHARGER` sits fixed at the 1023/92.3kW idle placeholder whenever not actually
charging, never becoming a live per-cell-voltage-managed value outside a real charge session).
`ac_charge_taper` — the taper reduction above AND its own `ac_emergency_v` hard cut — only runs
while RZ450e's `charge_permission_input` interlock is actually granted; while just driving,
`charger_limit_kw` is left completely untouched (whatever the Signal Mapping tab or the idle
92.3kW placeholder already produced). Driving-mode overvoltage protection is entirely
`charge_target_taper`'s job instead (the regen taper, unconditional by design — it drives the
shared `charge_limit_kw` field for both regen-while-driving and charge-while-plugged-in).

## Two cutoff tiers (drives the battery-management design in `05-battery-management-safety.md`)

**Soft cut** — no RED dash error:
- **`capacity_empty`** (`0x55B`) — gentle depleted-battery cutoff. Contactor re-closes once brake
  + start are pressed again with the flag cleared.
- **`full_charge_flag`** (`0x1DB`) — instant charge stop + contactor drop. Unlike `capacity_empty`,
  clearing it does **not** by itself resume charging — the real car needs a physical replug. Set
  from RZ450e-side inputs in two places now: `ac_charge_taper`'s AC-target/SoC check
  (`05-battery-management-safety.md` — moved here 2026-08-01, was `charge_target_taper`'s, see the
  regen/AC-charger split note above), and (added 2026-07-31) the charge-ramp emulation's
  "Leaf wants to charge but RZ450e permission not granted" mismatch, above. Both are recomputed
  fresh every tick rather than a true sticky latch (this flag stays a SOFT cut, unaffected by the
  hard-cut latching below) - a genuine "needs a physical replug to clear" re-arm rule is still an
  open question (see `10-open-questions.md`).

**Hard cut** — RED "service EV system, no power" dash message:
- **`relay_cut_request`** (`0x1DB`) — nonzero triggers main relay cut + RED message. Self-resets
  ~3 minutes after the car is powered off, not on next key-on. **This bridge's own hard cuts now
  LATCH as of 2026-08-01** (docs/12 finding F8) - once asserted, `relay_cut_request` stays asserted
  every tick even after the triggering reading recovers, until a fresh session start or charger
  replug clears the latch (`05-battery-management-safety.md`) - a deliberate change from the
  self-clearing-every-tick behavior every other bridge-driven flag on this page still has.
- **`interlock_connected`** (`0x1DB`, clearing it) — RED message appears **instantly**. Also part
  of the same latch as `relay_cut_request` above - both assert/clear together.
- **`main_relay_on`** (`0x1DB`, clearing it) — also prevents contactor closure, but with a delay
  before the RED message appears (different timing from interlock, otherwise similar effect).
  Always sends `1` (no live driver) — **user decision** (`docs/13` item 12.5): this signal only has
  any effect during startup, so a static `1` is fine; not wired into the hard-cut path.

Per user instruction (`05-battery-management-safety.md`): default to soft cut for routine
protection, reserve hard cut for genuine emergencies and the staleness-watchdog fault.

**All four of `relay_cut_request`, `interlock`, `capacity_empty`, and `full_charge_flag` are
management-exclusive, not Signal Mapping targets (`bridge/leaf_signals.MANAGEMENT_EXCLUSIVE_KEYS`,
fixed 2026-08-04, docs/13 item 16.3).** A stale code comment near `relay_cut_request`'s own
definition previously claimed this was already true for that one field ("not itself in
SLIDERS/CHECKS... never a direct mapping target") - it wasn't; `relay_cut_request` had been sitting
in `SLIDERS`, fully user-mappable, the entire time, and `capacity_empty`/`full_charge_flag`/
`interlock` were never excluded either. Since `ManagementEngine.apply()` only ever forced these
fields *toward* their cut/latched state and never explicitly cleared them, a user-created (or
hand-edited-profile) mapping tie targeting any of the four could hold it stuck asserted
indefinitely, invisible to the Fault History window (a mapping-layer effect, not something
`ManagementEngine`'s own fault_log ever sees). Fixed two ways together: these four keys are now
excluded from `leaf_signals.OUTPUT_SIGNALS` (can't be selected as a mapping output at all), and
`ManagementEngine.apply()` now explicitly clears `capacity_empty`/`relay_cut_request`/`interlock`
back to their safe value every tick when no condition holds (not just conditionally setting the cut
value) - `full_charge_flag` is deliberately excluded from that second half of the fix, since it's
legitimately also set by `RealtimeEngine._apply_charge_ramp()` in a different module/moment; an
unconditional clear inside `ManagementEngine.apply()` would race against and undo that. All four
remain visible on the Dashboard - the three `CHECKS` fields via the "Flags" section, and
`relay_cut_request` there too as of the same fix (previously only visible in the main per-signal bar
list, under `SLIDERS`).

## Fields that are NOT mapping targets — internally generated, shown as send/don't-send checkboxes

None of these have any real-world meaning to map from RZ450e data; the Leaf project replicates
them as opaque replay tables/counters. This project does the same, and — per user instruction —
surfaces each as a checkbox in the GUI (default checked/sent) rather than hiding them, so nothing
is missed visually or forgotten when porting to STM32 firmware:

- **PRUN** — 2-bit free-running counter (0→1→2→3→0), packed into `0x1DB`/`0x1DC`/`0x55B` byte 6 and
  `0x1ED` byte 1.
- **Voltage latch toggle bit** — flips every 10ms, `0x1DB` byte 3 bit 0. Purpose unconfirmed.
- **`0x1C2` heartbeat byte** — `0x50 | (counter & 0xF)`.
- **`0x1DC` bytes 4-6 ("CODE_1DC")** — 4-tuple cycle keyed by PRUN.
- **`0x5BC` bytes 5-7 ("charge-time remaining")** — 7-slot cycle, undecoded meaning, replay
  verbatim.
- **`0x5C0` byte 0 mux + bytes 3-5** — 6-state cycle.
- **`0x5EB`** (ZE1 only) — entire 8-byte frame is a 45-step opaque cycle, copy the source project's
  `SEQ_5EB` table verbatim.
- **`LB_RefusetoSleep`** (`0x55B` byte 6 bits 5-6) — derived from ignition-state tracking, not a
  manual input (see `07-startup-shutdown-plan.md`). **Was hardcoded to 0 (bug, fixed 2026-07-31)**
  — actually wired to `ShutdownSequencer.refuse_sleep_value()` now; see `07`'s writeup for the fix
  and its real-capture citation.

## Cell-level data — only via diagnostics (milestone 2)

None of the 6 periodic frames carry per-cell data. The only place it can appear on this bus is the
`0x79B`/`0x7BB` UDS diagnostic responder (LeafSpy compatibility). **Per user decision, this project
builds no replay/placeholder version of this responder** — it stays fully unbuilt until milestone
2, at which point it's wired to real RZ450e per-cell data (`0x4A9`/`0x4C0` or DID `0x182E`) from
day one.

## Source of truth

Snapshot of `Refrance/Leaf_BMS_Emulator/battery emulator overview/02-signal-dictionary.md` and its
"User Tested Notes" section (real ZE1 40kWh vehicle sweep, 2026-07-30). If the Leaf project's own
tests later correct something here, update this file to match — never the reverse.
