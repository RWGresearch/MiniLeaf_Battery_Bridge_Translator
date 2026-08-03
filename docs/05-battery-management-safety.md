# Battery Management & Safety

This is the most important doc in this set. **This project is a small standalone BMS, not a
passive signal translator.** Direct quote of user intent: *"we are building the data bridge but in
between we are managing the battery at the end of the day with the bridge... it needs to be
configurable."*

Once this bridge is between the RZ450e pack and the Leaf car, the RZ450e's own native protection
logic (whatever it does internally) is no longer visible to, or trusted by, the vehicle — this
bridge has to make the same category of decisions a real BMS makes: when to cut off, when to back
off power, and when to let the Leaf's own VCM logic just do its thing.

## Design philosophy

- **A missing/unwired interlock signal always fails safe to "not permitted," never "permitted."**
  Deliberate, written policy as of 2026-08-01 (previously true in code but only as an emergent side
  effect - see `10-open-questions.md` item 4 for the full history): `charge_permission_input`
  (`0x358`) is read everywhere as `bool(rz_state.get_input(...))`, and `SharedState.get_input()`
  returns `None` for a signal that's never arrived this session - `bool(None)` is `False`, so no
  charge-permission-gated behavior (`charge_target_taper`, `ac_charge_taper`, the charger-ramp
  emulation) can ever fire from a hardware revision that simply never wired this signal up. This is
  a separate concern from the charger-ramp's own "dual-trigger" requirement (needing this signal
  granted AND a real Leaf request) - that one assumes the signal exists and asks whether it's
  currently active; this one covers the signal not existing at all.

- **Curated, named features — not a generic rule engine.** Each protection feature below has its
  own specific config fields (enable flag, source signal(s), threshold(s)). This matches the
  mapping engine's "fixed preset list" design (`04-signal-mapping.md`) and keeps every feature
  independently portable to a C struct/function on the eventual STM32 firmware.
- **Defaults, not blanks.** Every feature below ships **enabled**, with a real starting threshold —
  never a disabled/unconfigured placeholder for anything safety-relevant. The user confirmed this
  explicitly: since everything is editable in the app, ship sensible defaults now and refine them
  against real hardware over time, saving new numbers as the new defaults. This is the same
  iterative loop both reference projects already use (`DEFAULTS` dict + a manual-verification
  checklist — see `11-manual-verification-checklist.md`).
- **Prefer soft cut over hard cut.** The Leaf side has two distinct cutoff tiers (see
  `03-target-signals-leaf.md`): **soft cut** (`capacity_empty` / `full_charge_flag`, no RED dash
  error) and **hard cut** (`relay_cut_request` / interlock drop, RED "service EV system" error).
  Per user instruction: routine protective action always lands on soft cut; hard cut is reserved
  for genuine emergencies (a second, more extreme threshold tier, or total data loss — see
  `06-realtime-engine-and-watchdog.md`).
- **Fast sources for anything in a control loop.** The Leaf's onboard AC charger tops out around
  6.6kW / <20A — a small absolute range with little margin for a sluggish or laggy control loop.
  Any feature that actively ramps power (not just a one-shot cutoff) must be driven by RZ450e's
  fast raw-CAN signals (`0x4A9`/`0x4C0` per-cell voltage, `0x023` current), never a 4-9s DID poll.
- **Real-time per-cell voltage is the SOLE authoritative signal for every safety cutoff; SoC is a
  backup check only, never an independent trigger (user directive, 2026-07-31).** SoC is a
  pack-average *estimate*, not a precise real-time measurement — it can't see a single weak or
  imbalanced cell, and it lags reality more than the fast per-cell voltage broadcast does. Every
  cutoff/derate feature below must make its actual go/no-go decision from individual cell voltage
  only; SoC may still be evaluated and surfaced (confirms the decision when it agrees, flags a
  visible warning — e.g. a possible SoC-calibration problem — when it doesn't) but must never be
  allowed to fire a cutoff by itself. This was a real bug, not just a design preference: an earlier
  version of `low_voltage_cutoff` OR'd a low-SoC reading in as an equal independent trigger, which
  meant a low SoC estimate alone could cut the pack off even with every cell perfectly healthy —
  see that feature's entry below for the fix. The one legitimate exception is the AC-charge daily/
  extended target ("charge to 80%/100%") — that's a user *preference* for how full to charge, not
  an overvoltage safety cutoff (which is separately, and fully, covered by per-cell voltage), so SoC
  is the correct metric there by definition.

## NMC pack parameters (researched, cross-checked against this specific pack)

The RZ450e pack is 96 NMC cells, ~348V resting, confirmed 3.6-4.2V real-world cell range, 3.7V
nominal (matches Toyota's own published bZ4X spec). Researched general NMC/lithium-ion safety
ranges, used to set the defaults below:

- Nominal: 3.6-3.7V/cell. Standard charge ceiling: 4.20V/cell (some high-voltage NMC variants go
  to 4.35-4.4V, but nothing suggests this pack is one of those). Typical BMS discharge floor:
  2.75-3.0V/cell; below ~2.5V risks permanent damage (copper dissolution at the anode).
- Charging safe temperature range: 0-45°C. Charging below 0°C risks lithium plating. BMS
  charge-power derating commonly begins around 32°C.
- Discharging is far more tolerant: −20°C to 60°C.

(Sources: TYCORUN battery-voltage and NMC-cell-voltage guides, Highstar's lithium cell voltage
guide, Toyota bZ4X cell/module design writeup, EBL and Large-Battery lithium-ion temperature
management guides — general industry references, not RZ450e-specific datasheets, since Toyota
doesn't publish per-cell limits. Treat as a documented starting point, to be confirmed/refined
against this specific pack per `11-manual-verification-checklist.md`.)

- **Operating SoC window** (user-specified): 10% min / 80% max for typical daily use, with the
  ability to go up to a safe 100% when explicitly requested (e.g. long road trips). The min-SoC
  *backup check* (`low_voltage_cutoff.min_soc_pct`, never an independent trigger — see Design
  philosophy above) was later retuned to 8% (user edit, 2026-08-01) for consistency alongside the
  voltage-tier retuning that session; 10%/80%/100% remain the daily/extended charge-target design
  intent this whole section is built around.

## Curated protection/management features

| Feature | Source signal(s) | Config | Cut tier | Drives |
|---|---|---|---|---|
| Low-voltage cutoff, cell-voltage authoritative | RZ450e per-cell voltages (`0x4A9`/`0x4C0`, authoritative) + pack `cell_min` (`0x020`, sanity check) + SoC (DID `0x1F5B`, backup check only, never acts alone) | min-cell-voltage cutoff, soft-cut persistence window, min SoC % (backup), emergency low voltage | Soft (persistence-qualified) → Hard | `capacity_empty` then `relay_cut_request` |
| Discharge power taper | RZ450e per-cell voltages (`0x4A9`/`0x4C0`, primary) | full-power voltage, zero-power voltage, recovery-ramp time (fast-attack/slow-release hysteresis) | Soft (ramp) | `discharge power limit` |
| Charge/regen power limit (`charge_target_taper`, 2026-08-01 split — regen only) | RZ450e per-cell voltages (`0x4A9`/`0x4C0`, primary) + pack `cell_max` (`0x020`, sanity check) | full-power voltage, zero-power voltage (proactive taper), emergency voltage | Soft (ramp) → Hard | `charge_limit_kw` ONLY — active regardless of charging context (driving or plugged in) |
| AC charge target + taper (`ac_charge_taper`, 2026-08-01 split — lives in `charge_emulation`, AC-charger only) | RZ450e per-cell voltages (`0x4A9`/`0x4C0`, primary) + SoC (DID `0x1F5B`) + `charge_permission_input` (`0x358`, gates the target/flag entirely) | full-power voltage, zero-power voltage, emergency voltage, daily target %, extended target % | Soft (ramp then stop) → Hard | `charger_limit_kw` — and, only while `charge_permission_input` is active: `full_charge_flag` at target SoC |
| Over-temperature derate | RZ450e `temp_max` (hottest probe) + `temp_min` (coldest probe, cold-side charge decisions only) (fast, `0x4AA`/`0x4A7`) | cold-derate-start/low-block temps (coldest probe), charge derate-start/hard-stop temps (hottest probe), discharge derate-start/hard-stop temps (hottest probe), emergency temp (hottest probe) | Soft (ramp, both hot and cold side) → Hard | `discharge power limit`, `charge power limit` then `relay_cut_request` |
| Cell imbalance monitor (added 2026-07-31) | All 96 RZ450e per-cell voltages (`0x4A9`/`0x4C0`) | warn-spread threshold | **Monitor only — never cuts or derates** | status text only |
| Overcurrent monitor (added 2026-07-31) | RZ450e `current` (fast, `0x023`) | continuous discharge/charge warn thresholds, persistence window | **Monitor only — never cuts or derates** | status text only |
| Cell data cross-check (added 2026-08-03) | Per-cell voltages (`0x4A9`/`0x4C0`) vs. pack `cell_min`/`cell_max` summary (`0x020`) | max allowed disagreement, soft-cut delay, hard escalation delay | Soft → Hard | `capacity_empty` then `relay_cut_request` — catches the per-cell broadcast and the pack summary silently disagreeing (e.g. a decode fault on one source), which neither source alone would detect |
| Staleness watchdog | `0x358`/`0x3F1` alive counters, `0x424` tick, Toyota checksums | per-group timeout (soft, then hard after a short escalation window) | **Soft → Hard** | `capacity_empty` (+ `full_charge_flag`, added 2026-08-03) then `relay_cut_request` — see `06-realtime-engine-and-watchdog.md` |
| Charge-start data gate (`require_live_data_to_charge`, added 2026-08-03) | Every per-cell voltage + `temp_max`/`temp_min`, checked against the current bridge session's start time | on/off only (default on) | N/A — one-time startup gate, not an ongoing cut | Blocks the charger ramp from starting at all until genuinely live data has arrived this session; see "Charge-start data gate" section below |
| Input plausibility validation (`input_validation`, given a real toggle 2026-08-03) | Every decoded RZ450e value, checked against `PLAUSIBLE_RANGES` | on/off only (default on) | N/A — an ingest-side data-integrity gate, not a cutoff | A rejected value is simply never written to live state at all — it keeps aging under its last-good value, eventually caught by the staleness watchdog if sustained. See `02-source-signals-rz450e.md`. Was always-on with no config until 2026-08-03; kept as an editable feature so a deliberately-corrupted test value can be pushed through to confirm downstream handling. |
| Toyota checksum validation (`checksum_validation`, given a real toggle 2026-08-03) | The 5 confirmed checksum-bearing RZ450e IDs (`0x020`/`0x023`/`0x358`/`0x3F1`/`0x424`) | on/off only (default on) | N/A — an ingest-side data-integrity gate, not a cutoff | A frame that fails its checksum is rejected before decode entirely, same downstream effect as a plausibility rejection. See `02-source-signals-rz450e.md`. Was always-on with no config until 2026-08-03; same reasoning as input plausibility validation above. |
| Emergency over/under-voltage or over-temp | Same fast sources as above, a second, more extreme threshold per source | per-source emergency threshold | **Hard** | `relay_cut_request` / interlock drop |

### Researched default values (all editable in the GUI, saveable as new defaults)

| Threshold | Default | Basis |
|---|---|---|
| Min cell voltage cutoff (soft → `capacity_empty`) | 3.00 V | standard NMC BMS floor, margin above the ~2.5V permanent-damage line |
| Emergency low-voltage (hard cut) | 2.60 V | user edit, 2026-08-01 — second, more extreme tier, before the damage line (was 2.80V) |
| Discharge taper: full power at/above | 3.00 V | user edit, 2026-08-01 — re-anchored to `low_voltage_cutoff`'s soft-cut floor (was 3.50V) |
| Discharge taper: zero power at/below | 2.60 V | user edit, 2026-08-01 — matches the emergency low-voltage tier (was 3.00V), so discharge power reaches zero right around where the cutoff tiers sit |
| Discharge taper: recovery ramp | 3.0 s | user-specified — deliberately slow (vs. the instant fast-attack on a dip) to avoid the power limit hunting/oscillating if cell voltage bounces near the threshold under intermittent acceleration |
| Regen full power at/below (proactive taper start, `charge_target_taper`) | 4.00 V | as of the 2026-08-01 regen/AC split; deliberately well below the ~4.20V NMC ceiling, since the VCM is slow to respond to a `charge_limit_kw` change and the taper must act well ahead of the danger zone, not at its edge |
| Regen zero power at/above (proactive taper end, `charge_target_taper`) | 4.15 V | as of the 2026-08-01 regen/AC split; still under the standard 4.20V NMC ceiling, so regen is fully backed off before a cell is anywhere near its actual limit |
| Regen emergency high-voltage (hard cut, `charge_target_taper`) | 4.20 V | user edit, 2026-08-03 (was 4.30V) — set to the standard NMC charge ceiling exactly, tightening the margin above the 4.15V zero-regen point to 0.05V; second tier, above the zero-regen point — if a cell keeps climbing after regen is already at zero, something else is charging it |
| AC charge full/zero/emergency (`ac_charge_taper`, split into its own feature 2026-08-01) | 4.00 V / 4.15 V / 4.20 V | emergency tier user edit, 2026-08-03 (was 4.30V, matching the regen-side change above); defaulted to the same starting curve as the regen taper above, not yet independently tuned now that it's its own feature — a real charger session may warrant a different curve than a regen event |
| Low-voltage soft-cut persistence window (added 2026-07-31) | 2.0 s | researched — guards the min-cell soft cut against a single-tick voltage sag transient under a spike load (cold-pack internal resistance roughly doubles vs. 25°C); the discharge power taper is already collapsing current/sag on a faster ramp by the time this window matters in the normal case. Emergency low-voltage stays instantaneous, no persistence. |
| Charge cold-derate start (added 2026-07-31, coldest probe) | 10°C (50°F) | researched — ramps charge/regen acceptance down approaching the freezing line instead of a single on/off block; plating risk rises well above 0°C at meaningful charge current. Full power at/above this, ramping to zero at "Charge temp low block." |
| Charge temp low block (coldest probe, not hottest — **bug fixed 2026-07-31**) | 0°C (32°F) | lithium-plating risk below this while charging. Previously evaluated against the hottest probe, which let charging continue into a partly-frozen pack as long as the warmest corner read above freezing — fixed to key on the coldest probe, since plating happens in the coldest cells. |
| Charge temp derate start (hottest probe) | 32°C (90°F) | typical BMS charge-derate onset |
| Charge temp hard stop (hottest probe, soft ramp only) | 45°C (113°F) | upper charging-safe limit — ramps charge/regen to zero by this point; no longer also triggers a hard cut directly (see Emergency temp below) |
| Discharge temp derate start (hottest probe) | 55°C (131°F) | margin below the 60°C discharge ceiling |
| Discharge temp hard stop (hottest probe, soft ramp only) | 60°C (140°F) | upper discharge-safe limit (discharge tolerates a much wider range than charge — no low-temp discharge cutoff needed); ramps discharge power to zero by this point, no longer also triggers a hard cut directly (see Emergency temp below) |
| Emergency temp (hard cut, hottest probe) | 61°C (141.8°F) | user edit, 2026-08-01 — tightened from the original researched 65°C/149°F starting point to sit closer above the 60°C soft stop; a genuine second, more extreme tier above the soft discharge/charge hard-stops, mirroring the voltage features' soft/emergency split. Deliberately thin margin: a cell with any plated lithium can begin self-heating as low as ~60°C, leaving very little real margin in the chemistry. |
| Cell imbalance warn spread (monitor only) | 100 mV | user edit, 2026-08-01 — widened from the original researched 50mV starting point. A cell resting 30-50mV below its neighbors at rest is a documented early signature of elevated self-discharge (a developing internal defect); monitor/warn only, this bridge cannot balance cells. |
| Cell data cross-check delta (added 2026-08-03) | 150 mV | new feature — max allowed disagreement between the per-cell broadcast and the `0x020` pack min/max summary before it's treated as a data-integrity fault, not just normal reporting jitter between two independently-sampled sources |
| Cell data cross-check soft-cut delay / hard escalation | 60 s / +5 s | matches the staleness watchdog's own timing, independently tunable from it |
| Overcurrent discharge warn (added 2026-07-31, monitor only) | 150 A | derived from this project's own confirmed spec, not an invented number — set comfortably below the `0x023` current sensor's own ±204.7A saturation ceiling (docs/02), so a warning still means something (above ~205A the true magnitude is unmeasurable regardless). That ceiling is a sensor/encoding limit, not a battery limit — the real pack is rated to a 500A discharge fuse and ~660A short-burst peak (user correction, 2026-07-31, docs/02) — so this monitor cannot see anywhere near the pack's real operating range. No cell datasheet exists to source a real cutoff threshold from, so this is monitor-only, not wired to any derate/cut. |
| Overcurrent charge/regen warn (added 2026-07-31, monitor only) | 30 A | derived from this project's own confirmed spec — set above the Leaf's onboard AC charger's documented ~19A/6.6kW max, so ordinary AC charging never trips it; catches only abnormal charge/regen current. Monitor-only, same reasoning as above. |
| Overcurrent monitor persistence | 5.0 s | researched — avoids flagging a brief acceleration or regen spike as sustained overcurrent |
| Min SoC (backup check only) | 8% | user edit, 2026-08-01 — corroborates the cell-voltage-based cutoff, never triggers it alone (2026-07-31 fix); was 10% |
| Charge target, daily | 80% | user-specified |
| Charge target, extended/road-trip | 100% | user-specified |
| Staleness watchdog: soft-cut delay | 60 s | user-specified; soft stage now also sets `full_charge_flag` in addition to `capacity_empty`/zeroed power limits (added 2026-08-03, `docs/13` item 13.1), to force an explicit charge-stop rather than relying on `capacity_empty` alone during an active charge session |
| Staleness watchdog: hard-cut escalation | +5 s after soft cut (65s total) | user-specified — gives a transient CAN hiccup a brief window to self-clear |
| Charge-start data gate (`require_live_data_to_charge`) | on (default) | added 2026-08-03 — see "Charge-start data gate" section below |

## CC→CV charge/regen taper — how it actually behaves

**Corrected design (2026-07-31 user review): the taper is driven ONLY by individual cell voltage,
continuously, never gated by SoC.** An earlier version of this design used SoC to decide *when*
tapering should start (e.g. "only taper above 80% SoC") — the user explicitly rejected this: SoC
isn't precise enough to gate a charge-current cutback, and real cell-to-cell imbalance can push a
single cell toward the ceiling at any SoC, not just near the top of the pack. The taper must react
to that regardless of what the pack's average SoC says.

**Split into two independent features, 2026-08-01 (`ManagementEngine`'s `charge_target_taper` vs.
the separate `ac_charge_taper` block).** The original design used one shared taper for both
regenerative braking and AC-charger acceptance, on the reasoning that the Leaf's `charge_limit_kw`
field (`0x1DC`, "Charge/regen power limit," `03-target-signals-leaf.md`) is one shared VCM ceiling.
That's still true for `charge_limit_kw` itself, but the two use cases warrant independently tunable
curves and config, so they were split into two features that happen to default to the same curve:

- **`charge_target_taper` (regen only)** — drives `charge_limit_kw` ONLY, active continuously
  regardless of charging context (driving or plugged in). Config: `regen_full_v`/`regen_zero_v`
  (proactive taper window) and `emergency_high_v` (hard-cut tier).
- **`ac_charge_taper` (AC charger only, config lives in `charge_emulation` alongside the rest of
  the charger-ramp controls)** — drives `charger_limit_kw` ("Max power for charger," `0x1ED`) and
  owns the daily/extended target SoC + `full_charge_flag`, ALL gated on `charge_permission_input`
  (the RZ450e charging interlock) being active. Config: `ac_full_v`/`ac_zero_v`/`ac_emergency_v`
  (same shape as the regen taper, defaulted to the same starting curve but independently tunable),
  `daily_target_pct`/`extended_target_pct`, `extended_mode` (toggle between the two targets).

Both features:
- Monitor all 96 individual cell voltages (primary: `0x4A9`/`0x4C0`, not the `0x020` pack-level
  summary) and ramp their respective power-limit field down as the **worst (highest) individual
  cell** rises from the configured full-power point to the configured zero-power point — pure
  voltage-feedback control. This is exactly what a real CC→CV charge algorithm does: hold current
  until voltage approaches the ceiling, then taper current to hold voltage instead — and it's what
  protects an imbalanced cell that reaches the ceiling early, from any charge source.
- Are **proactive by design (user-specified 2026-07-31)**: the taper window is deliberately wide
  and starts well below the pack's actual NMC ceiling — **default full power at/below 4.00V/cell,
  linearly down to zero at/above 4.15V/cell** for both — because the real Leaf VCM is slow to
  respond to a power-limit change. A narrow margin right at the ceiling doesn't give a slow-reacting
  VCM enough lead time to actually back off before a cell gets close to real danger.
- Have an **emergency tier**: if any individual cell reaches the feature's own emergency-high-
  voltage threshold (default 4.20V, above the zero-power point — user-tightened 2026-08-03 from
  4.30V to the standard NMC charge ceiling exactly), this escalates to a hard cut
  (`relay_cut_request`) — if a cell is still climbing after power is already fully backed off,
  something else is charging it. Per-cell voltage is always the authority, not a pack average.

**Deliberate asymmetry, decided 2026-08-03: only `charge_target_taper` (regen) has hysteresis.**
`charge_target_taper` carries fast-attack/slow-release state between ticks (`recovery_ramp_s`,
same pattern as `discharge_power_taper`) - `ac_charge_taper` does not; `ac_factor` is a pure
function of the current instantaneous voltage, recomputed fresh every tick with no smoothing.
Explicit decision, not an oversight ("let's leave it and mark it as such so it's not confusing in
the future") - if this asymmetry is ever revisited, it needs a real decision first, not a silent
"fix."

`ac_charge_taper`'s target-SoC/`full_charge_flag` logic specifically:
  - **Gated on `charge_permission_input` for a reason found and fixed 2026-07-31**: the
    SoC-target-reached logic originally set `full_charge_flag` from SoC alone with no
    charging-context check at all. Since `full_charge_flag` triggers an **instant main-contactor
    drop** on the real Leaf (`03-target-signals-leaf.md`), this meant simply *driving* above the
    target SoC (e.g. having charged to 85% overnight, then driving) would have dropped HV
    contactors mid-drive. While not actively charging per the interlock, only `charge_limit_kw`'s
    always-on `charge_target_taper` (the regen-side protection) applies — the AC target and
    `full_charge_flag` stay completely inert until a real charge session is detected.
  - Once gated on: once SoC reaches the active target (80% daily / 100% extended, toggled via
    `extended_mode`), charging stops entirely and `full_charge_flag` sets — a legitimate, common
    EV-conversion feature ("charge to 80% for daily use, 100% for road trips"), fine to base on SoC
    since it's just "stop when full enough," not the precision-critical taper ramp itself.
- Because the Leaf's onboard charger is a small absolute range (~6.6kW/<20A), this control loop
  must respond quickly and must use the fast per-cell broadcast, not a DID poll — see "Fast sources
  for anything in a control loop" above.

## Charge-start data gate (`require_live_data_to_charge`, added 2026-08-03)

A separate concern from both tapers above and from the general staleness watchdog
(`06-realtime-engine-and-watchdog.md`): before the charger ramp is allowed to start at all, every
per-cell voltage and both `temp_max`/`temp_min` must have a **genuinely live** reading (not a
cached/last-known-good/previous-session value) since this bridge session began. This is a one-time
per-session startup gate, not an ongoing timer — once satisfied it stays satisfied for the rest of
the session, since ongoing staleness protection during an active charge is entirely the general
watchdog's job (same 60s soft / +5s hard escalation used while driving). Design intent, direct
quote: *"it should be waiting for good data for at least 60 seconds... the 60 seconds is the
watchdog, just like the driving is... it's the same safety and data validation as driving, just
with a different startup, instead of default values."* Concretely: `RealtimeEngine._charge_data_
ready()` compares `SharedState.timestamps_of()` for the 96 cell keys + temp_max/temp_min against
`ShutdownSequencer.get_session_start()` — a session boundary is a real bus wake
(`waiting_for_wake -> startup`), not just app launch, so a sleep/wake cycle with the RZ450e now
disconnected correctly re-blocks the ramp rather than trusting data that went live in an earlier
session. Default on, editable via the Charge Emulation panel's checkbox.

## Discharge power taper — the low-end mirror, added 2026-07-31

Same reasoning applied to the other end of the pack: `discharge power limit` (`0x1DC`) is ramped
down as the **worst (lowest) individual cell voltage** approaches empty — driven by per-cell
voltage, not SoC, for the same reason as the charge/regen taper (a single weak or imbalanced cell
sags under heavy discharge load before pack-average SoC would suggest a problem).

- **The curve**: full discharge power at/above 3.00V/cell, linearly down to zero at/below 2.60V/cell
  (re-anchored 2026-08-01, was 3.50V/3.00V) — the zero point deliberately matches
  `low_voltage_cutoff`'s emergency tier, and the full-power point matches its soft-cut floor, so
  discharge power reaches zero right around where the cutoff tiers sit, a smooth transition into
  the cutoff rather than full-power-then-sudden-stop.
- **Hysteresis — fast attack, slow release (user-specified)**: unlike the charge/regen taper (a
  pure function of the current instantaneous voltage), this feature carries state between control-
  loop ticks. If voltage dips, the applied power limit snaps down immediately — cell protection
  can't wait for a slow ramp. But once voltage recovers, the applied limit only climbs back up at a
  rate bounded by the configured recovery-ramp time (default 3.0s to go from 0% to 100%), not
  instantly. This specifically avoids the discharge power limit hunting/oscillating if cell voltage
  bounces up and down near the threshold under intermittent acceleration — a real concern this
  feature has that the charge/regen taper doesn't, since driving load is far spikier than charging
  load.
- **Implementation note**: because of the hysteresis, this feature is NOT a stateless
  voltage-to-power-factor formula — it requires an "applied factor" value and a last-update
  timestamp carried between evaluations (`bridge/management_engine.py`'s `_discharge_factor_applied`
  / `_last_apply_time`). A future STM32 port must replicate this as stateful control-loop logic, not
  recompute the output purely from the current voltage each cycle (see `09-stm32-export-format.md`).

## Over-temperature derate — restructured 2026-07-31 (docs/12 research pass)

A research pass through general NMC/BMS literature (`12-nmc-bms-design-research.md`) found one bug
and two structural gaps in this feature, all now fixed:

- **Bug fixed (F1): cold-side charge decisions now key on the COLDEST probe (`temp_min`), not the
  hottest.** The original implementation tested `temp_max` for the sub-freezing charge block, which
  meant charging (and regen) could continue into a partly-frozen pack as long as the *warmest*
  corner still read above freezing — backwards, since lithium plating happens in the *coldest*
  cells. Hot-side decisions (both charge and discharge derate) correctly stay on `temp_max`, since
  heat risk is set by the hottest region.
- **Added (F3): a cold-side derate ramp, not just an on/off block.** Research shows plating risk
  rises well above the freezing line at meaningful charge current — a cell charged at a moderate
  rate at 10°C can lose cycle life 100x faster than the same cell at 20°C. The feature now ramps
  charge/regen acceptance from zero at "Charge temp low block" (0°C/32°F) up to full power at
  "Charge cold-derate start" (10°C/50°F), rather than snapping straight from 0% to 100% at the
  freezing line. This mainly protects against **regen into a cold-soaked pack** (up to ~0.5C) — the
  Leaf's onboard AC charger (~0.09C) poses negligible plating risk at any above-freezing
  temperature.
- **Added (F6): a genuine emergency hard-cut tier, separate from the soft ramp ceilings.** The
  original implementation asserted a hard cut (`relay_cut_request`) the instant `temp_max` reached
  either `discharge_hard_stop_f` or `charge_hard_stop_f` — i.e. the "top of the soft ramp" and "the
  hard-cut trigger" were the same number, even though this feature's row in the table above is
  documented as "Soft (ramp)." Both hard-stop values are now pure soft ramp-to-zero points; a new
  "Emergency temp" (61°C/141.8°F — tightened from an original researched 65°C/149°F, user edit
  2026-08-01) is the actual hard-cut trigger, mirroring the two-tier soft/emergency structure the
  voltage features already have. It sits close above the 60°C discharge soft stop deliberately —
  self-heating onset for a cell with any plated lithium can begin as low as ~60°C, so there's very
  little real margin above it in the chemistry to work with.

## Cell imbalance monitor — added 2026-07-31 (docs/12 finding F4)

**Monitor/warn-tier only — never cuts or derates anything.** Watches the spread between the worst
(highest) and worst (lowest) of all 96 individually read cell voltages and surfaces a warning in the
status text once it reaches the configured threshold (default 50 mV).

This bridge cannot balance cells — that's the RZ450e pack's own internal cell-supervision hardware,
if it still operates once the pack is running in this configuration (open question, see
`10-open-questions.md`). But this project uniquely sees all 96 cells at high rate, and a growing
spread is the cheapest early-warning signal available: research shows a cell resting 30-50mV below
its neighbors at rest is a documented early signature of elevated self-discharge — i.e. a developing
internal defect — well before it becomes a bigger problem. A growing spread also silently shrinks
the usable pack window over time (the highest cell hits the charge taper early, the lowest cell hits
the discharge taper early).

## Overcurrent monitor — added 2026-07-31 (docs/12 finding F2)

**Monitor/warn-tier only — never cuts or derates anything.** Surfaces a warning in the status text
after *sustained* (not momentary) elevated current in either direction, using RZ450e's fast `0x023`
current signal.

Deliberately **not** wired to an active cutoff: there is no cell datasheet to source a real
continuous/peak current limit from (Toyota doesn't publish one), and this project's own fast current
sensor saturates at ±204.7A (`02-source-signals-rz450e.md`) — a sensor/encoding ceiling, not a
battery limit. **The real pack is rated well past what this sensor can even represent** (user
correction, 2026-07-31): a 500A slow-blow fuse in-line for discharge, 150kW DC fast charging
(~430A), and the factory RZ450E's 230kW peak output (~660A for short bursts) — all far above both
this sensor's ±204.7A ceiling and this bench setup's own ~200A test history. An active cutoff
couldn't measure how far beyond ~205A a real fault current (or the pack's own normal high-current
operation) would go, so this monitor is fundamentally limited by the sensor, not a statement about
what the pack can safely do — see `02-source-signals-rz450e.md`'s "Real pack current capability"
note. The warn thresholds are derived from this project's own already-confirmed specs rather than
an invented "reasonable-looking" number: the discharge default (150A) sits comfortably below the
sensor's saturation ceiling so a warning still means something, and the charge/regen default (30A)
sits above the Leaf's onboard AC charger's documented ~19A max so ordinary charging never trips it.
Treat both as provisional pending real drive-cycle current logging (see
`11-manual-verification-checklist.md`) — and note a wider-range current sensor would be needed
before this pack's real 500A/660A-class operation could ever be monitored at all.

## `full_charge_flag` re-arm without a physical replug — resolved 2026-08-03

The real Leaf requires a physical unplug/replug to resume charging after `full_charge_flag` fires.
This bridge has no direct equivalent physical signal from the RZ450e side, but a **hard-cut latch**
(`docs/13` items 13.1/13.4) was added that applies here too: once any hard-cut condition fires,
`ManagementEngine._hard_latched` stays asserted even after the triggering reading recovers, and is
only cleared by one of two genuine re-arm events —
- **`notify_session_start()`** — a real bus wake (`waiting_for_wake -> startup` phase transition),
  i.e. the car was actually power-cycled, not just the bridge's own Stop/Start Bridge button.
- **`notify_charge_replug()`** — `charge_permission_input` (`0x358`) going continuously absent for
  at least `CHG_END_STOP_S` (3.0s, the same threshold the shutdown sequencer uses to decide a
  charge session has really ended) before a new request arrives. A brief dropout shorter than this
  does NOT count as a replug and does NOT clear a latch — only a genuine unplug/replug-length gap
  does. This mirrors the real Leaf's own physical-replug requirement using the closest signal this
  bridge actually has.

## Verification

Every default threshold above starts as **documented, not yet user-confirmed** against this
specific pack. `11-manual-verification-checklist.md` tracks the promotion of each one to
"user-confirmed" as real-hardware testing happens, mirroring the Leaf project's own
`10-manual-verification-checklist.md` process.
