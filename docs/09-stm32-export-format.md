# STM32 Export Format

Phase 2 is a separate future project: porting this bridge to standalone STM32 firmware. Rather
than building a separate "export" feature, **the app's own saved config profile format is
designed to double as the hardware-port spec from day one** — whatever the GUI saves is already
everything phase 2 needs to start firmware codegen from.

This doc specs that format. It is a **schema spec only** — no firmware code is written as part of
this project.

## What must be in the config profile

Per the user's explicit framing ("think about what we would export as a placeholder to build the
code from... inputs and outputs, how they're tied together, what their conversion is, whether
there's any special functions we need to build in or automatic data that needs to be generated in
the background"):

1. **Vehicle/battery configuration** — car generation (AZE0/ZE1), battery generation/capacity
   (30/40/62kWh) — determines which Leaf CAN IDs are active (`03-target-signals-leaf.md`).
2. **Signal mapping ties** — for each active tie: input signal(s) (by RZ450e signal ID), combine
   function + parameters, output signal (by Leaf signal ID). One entry per row in the GUI's Signal
   Mapping tab (`08-gui-design.md`).
3. **Battery management / protection features** — for each curated feature
   (`05-battery-management-safety.md`): enabled flag, source signal(s), threshold value(s), which
   output(s) it drives. This is the most safety-critical section — firmware must replicate this
   exactly, not re-derive it.
4. **Generated-signal send flags** — which internally-generated fields are enabled
   (`03-target-signals-leaf.md`'s checkbox list) — firmware needs to know which opaque replay
   tables/counters to actually run.
5. **Real-time engine parameters** — staleness-watchdog timeouts and escalation windows
   (`06-realtime-engine-and-watchdog.md`, in `management_features.staleness_watchdog`) plus, as of
   2026-08-14, DID-polling cadence (`engine_timing.did_response_timeout_s`/
   `did_inter_request_gap_s`/`did_temp_poll_interval_s`/`did_temp_fresh_window_s`) — DONE, see the
   `engine_timing` section below. Per-message TX periods (`leaf_signals.TX_PERIOD_MS`) are still
   NOT yet represented as data - remain hardcoded in `bridge/leaf_signals.py` (correctly so for now,
   since those are bit-verified real-Leaf protocol values, not a user tunable - but firmware would
   still need them as data, just not as an editable field).
6. **Startup/shutdown timing constants** — the staged bring-up/wind-down triggers
   (`07-startup-shutdown-plan.md`)'s wind-down TRIGGER timing (ignition quiet/off-delay/grace,
   charge-end/stall timeouts, bus-silence timeout) is now represented as data as of 2026-08-14
   (`engine_timing`, see below) - DONE for that half. The staged bring-up TIMELINE and shutdown
   STAGING (the ms-offset tables, `T_1DB_START` etc./`PWRDOWN_STAGE*_MS`) are bit-verified
   real-Leaf protocol values (not a user tunable) and remain hardcoded in
   `bridge/leaf_signals.py` - still NOT yet represented as data, same reasoning as the TX periods
   above.

## Format

**JSON.** Human-readable (useful for diffing/reviewing config changes), trivially parseable by a
firmware codegen script (Python, run offline — not on the STM32 itself), and matches the "no
generic scripting, curated fixed structure" philosophy from `04-signal-mapping.md` and
`05-battery-management-safety.md` — every section above maps to a fixed, well-typed JSON shape,
not an arbitrary expression tree.

**This is now the actual output of `bridge/config_profile.py`** (milestone 1 is implemented), not
just an illustrative sketch. Regenerated 2026-08-01 directly from `default_config()`/
`SharedState().charge_emulation` (never hand-transcribed - see this doc's own changelog note below)
- real example, one tie + the full current battery-management schema:

```jsonc
{
  "profile_name": "profile",
  "vehicle": { "car_gen": "ZE1", "battery_gen": "ZE1", "battery_kwh": 40,
              "usable_capacity_kwh": 64.0, "nameplate_capacity_ah": 201.0 },
  "mappings": [
    { "inputs": ["current"], "combine": "linear", "output": "pack_current_a",
      "params": {"scale": -1.0, "offset": 0.0},
      "name": "current -> pack_current_a (SIGN INVERTED: RZ450e +discharge -> Leaf +charge)" }
  ],
  "management_features": {
    "low_voltage_cutoff": {"enabled": true, "min_cell_v": 3.0, "min_soc_pct": 8.0,
                           "emergency_low_v": 2.6, "soft_cut_persistence_s": 2.0},
    "discharge_power_taper": {"enabled": true, "taper_start_v": 3.0, "taper_min_v": 2.6,
                              "taper_start_soc_pct": 20.0, "taper_min_soc_pct": 8.0,
                              "recovery_ramp_s": 3.0,
                              "discharge_min_kw": 0.0, "discharge_max_kw": 110.0},
    "charge_target_taper": {"enabled": true, "regen_full_v": 4.0, "regen_min_v": 4.15,
                            "regen_full_soc_pct": 80.0, "regen_min_soc_pct": 100.0,
                            "emergency_high_v": 4.2, "recovery_ramp_s": 3.0,
                            "regen_min_kw": 0.0, "regen_max_kw": 70.0},
    "over_temperature_derate": {"enabled": true, "charge_derate_low_start_c": 10.0,
                                "charge_low_block_c": 0.0, "charge_derate_start_c": 32.0,
                                "charge_hard_stop_c": 45.0, "discharge_derate_start_c": 55.0,
                                "discharge_hard_stop_c": 60.0, "emergency_temp_c": 61.0},
    "cell_imbalance_monitor": {"enabled": true, "warn_delta_v": 0.1},
    "overcurrent_monitor": {"enabled": true, "continuous_discharge_warn_a": 150.0,
                            "continuous_charge_warn_a": 30.0, "persistence_s": 5.0},
    "staleness_watchdog": {"enabled": true, "soft_cut_s": 60.0, "hard_escalation_s": 5.0},
    "cell_data_cross_check": {"enabled": true, "max_delta_v": 0.15, "soft_cut_s": 60.0,
                              "hard_escalation_s": 5.0},
    "temp_data_cross_check": {"enabled": true, "max_delta_c": 5.6, "soft_cut_s": 60.0,
                              "hard_escalation_s": 5.0},
    "temp_probe_cross_check": {"enabled": true, "max_delta_c": 2.0, "soft_cut_s": 60.0,
                               "hard_escalation_s": 5.0},
    "input_validation": {"enabled": true},
    "checksum_validation": {"enabled": true}
  },
  "generated_signals": {"prun": true, "voltage_latch_toggle": true, "heartbeat_1c2": true,
                        "code_1dc": true, "chg_time_5bc": true, "hist_5c0": true, "seq_5eb": true},
  "charge_emulation": {"charge_emulate": 1, "ac_taper_enabled": 1, "extended_mode": 0,
                       "require_live_data_to_charge": 1, "ac_temp_derate_enabled": 1,
                       "charge_target_kw": 6.6, "chg_uprate_level": 7,
                       "ac_full_v": 4.0, "ac_min_v": 4.15, "ac_cutoff_v": 4.18, "ac_emergency_v": 4.2,
                       "ac_min_kw": 0.5, "ac_max_kw": 6.6,
                       "dc_min_kw": 5.0, "dc_max_kw": 50.0, "qc_max_soc_pct": 80.0,
                       "daily_target_pct": 80.0, "extended_target_pct": 100.0,
                       "ac_derate_low_start_c": 10.0, "ac_low_block_c": 0.0,
                       "ac_derate_start_c": 32.0, "ac_hard_stop_c": 45.0},
  "engine_timing": {"did_response_timeout_s": 5.0, "did_inter_request_gap_s": 0.3,
                    "did_temp_poll_interval_s": 10.0, "did_temp_fresh_window_s": 20.0,
                    "ignition_quiet_s": 0.5, "ignition_off_delay_s": 10.0, "ignition_grace_s": 10.0,
                    "chg_end_stop_s": 3.0, "chg_stall_timeout_s": 15.0, "chg_cmd_fresh_s": 0.5,
                    "bus_silence_timeout_s": 30.0}
}
```
`ac_temp_derate_enabled`/`ac_derate_low_start_c`/`ac_low_block_c`/`ac_derate_start_c`/
`ac_hard_stop_c` added 2026-08-11 - `ac_charge_temp_derate`, the AC-charger-specific counterpart to
`over_temperature_derate` above (same split already applied to voltage: `ac_charge_taper` vs.
`charge_target_taper`). Drives only `charger_limit_kw`, only while `charge_permission_input` is
active; reaching `ac_hard_stop_c` also sets `full_charge_flag` (session ends, same convention as
`ac_cutoff_v` above) rather than just ramping to zero. See `05-battery-management-safety.md`'s "AC
charger temperature derate" section for the full rationale.
Note: `ac_charge_taper`'s convergence rate is NOT a config field (unlike every other taper's
`recovery_ramp_s`) - see the "AC taper" note below for why, and what a firmware port needs to
replicate instead.

**Discharge/regen power request floor+ceiling (added 2026-08-08, docs/16 parameter-clamping
audit).** `discharge_power_taper` and `charge_target_taper` each gained a `_min_kw`/`_max_kw` pair,
same pattern as `ac_charge_taper`'s pre-existing `ac_min_kw`/`ac_max_kw` (below): `output = min_kw +
(ceiling_kw - min_kw) * factor`, where `ceiling_kw = max(min_kw, min(baseline, max_kw))` and
`factor` is each taper's own 0.0-1.0 hysteresis-applied ramp value - firmware porting this must
replicate the floor/ceiling clamp, not just the multiplicative factor the pre-2026-08-08 formula
used. **Exception, safety-critical:** `charge_target_taper`'s EMERGENCY branch (worst cell ≥
`emergency_high_v`) always outputs literal `0.0`, bypassing `regen_min_kw` entirely - a floor must
never keep feeding power into an overvoltage emergency, matching `ac_charge_taper`'s own emergency
branch (which likewise bypasses `ac_min_kw`). `discharge_power_taper` has no emergency tier of its
own (that's `low_voltage_cutoff`'s job, which doesn't touch `discharge_limit_kw`), so no equivalent
exception applies there.

**Vehicle capacity spec (added 2026-08-08, docs/16 audit; `nameplate_capacity_ah` split out and
`qc_max_soc_pct` relocated the same day, user follow-up).** `vehicle.usable_capacity_kwh`/
`vehicle.nameplate_capacity_ah` feed `mapping_engine.derive_capacity_outputs()` (GIDS/QC capacity,
see `04-signal-mapping.md`'s rewritten formula) - genuine RZ450e-source-pack specs, not Leaf-side
generation selection like the other three `vehicle` keys, but placed in the same section since it's
the closest existing "pack spec" surface. `nameplate_capacity_ah` was a hardcoded
`mapping_engine.NAMEPLATE_CAPACITY_AH` module constant until this same follow-up made it
configurable (still the fallback default; the separate `soh_pct` mapping tie's own `nameplate_ah`
tie-param is independently editable and NOT wired to track this value live - two intentionally
separate "nameplate capacity" numbers for now). Both validated against
`mapping_engine.VEHICLE_FIELD_BOUNDS` on profile load (`bridge/config_profile.py`'s
`_apply_vehicle()`), same clamp-on-load discipline as `FEATURE_FIELD_BOUNDS`/
`CHARGE_EMULATION_BOUNDS` below. `qc_max_soc_pct` does **NOT** live in `vehicle` - it's in
`charge_emulation` instead (below), next to the DC placeholder fields, since it's charging behavior
(when the GIDS/QC formula caps its "full" reading) not a pack spec. A profile saved during the brief
window `qc_max_soc_pct` lived in `vehicle` migrates automatically on load (`apply_profile()`'s own
one-time migration, same pattern as the pre-existing `ac_zero_v` → `ac_min_v` migration below).

**`charge_target_taper` vs. `charge_emulation`'s AC fields (split 2026-08-01)** — as of this split,
`charge_target_taper` governs **only** `charge_limit_kw` (the regen/general-acceptance ceiling,
active regardless of charging context); the AC-charger-specific taper (`charger_limit_kw`) and the
daily/extended SoC target now live in `charge_emulation`'s AC fields instead, alongside the
pre-existing charger-ramp-emulation controls (`charge_target_kw`/`chg_uprate_level`) — regen (up to
~0.5C into the pack) and AC charging (~0.09C) are physically different enough to need
independently-tunable curves. Firmware porting this schema must replicate BOTH as separate per-cell
tapers driving their own respective Leaf output field, not recombine them into one. Neither has an
SoC-gating field (e.g. no `taper_start_pct`) — both tapers are driven purely by continuous per-cell
voltage; `daily_target_pct`/`extended_target_pct` are only the separate AC SoC stop-point, not part
of either taper ramp. `regen_full_v`/`ac_full_v` are also both deliberately well below the pack's
actual NMC ceiling (~4.2V) — a proactive design choice for a slow-responding VCM, not the cell's
real safety limit; firmware inheriting this schema should keep that gap, not "tighten" it thinking
it's overly conservative. `charge_target_taper` also carries the same fast-attack/slow-release
hysteresis as `discharge_power_taper` (below) via its own `recovery_ramp_s` - stateful, not a pure
function of instantaneous voltage.

**AC taper three-tier voltage structure + min-kW floor (reworked 2026-08-06)** — `ac_full_v`
(full power) → `ac_min_v` (renamed from `ac_zero_v`; taper bottoms out here and **holds**, does
NOT drive to true 0kW) → `ac_cutoff_v` (new; crossing this deliberately ENDS the session -
`full_charge_flag`/`charge_limit_kw=0`/`charger_limit_kw=-10`, same convention as the existing
SoC-target-reached stop) → `ac_emergency_v` (unchanged, hard cut). Firmware porting this must
replicate all four as distinct tiers in that order, not collapse `ac_min_v`/`ac_cutoff_v` back into
a single zero-point - the whole reason for the split (2026-08-06, real bench-hardware test) was to
stop relying on the Leaf's own charger reacting to a near-zero power request as an implicit "stop,"
in favor of an explicit one. The taper's output floor is `ac_min_kw` (new; the AC min/max kW request
bounds also clamp the manual `charge_target_kw` ramp target - see `RealtimeEngine.
_apply_charge_ramp()`), not zero: `tapered_kw = ac_min_kw + (ramped_kw - ac_min_kw) * factor` once
`ramped_kw` exceeds `ac_min_kw`, otherwise passed through untouched (so the taper never fights the
charger-ramp's own low-value startup ramp-up). `dc_min_kw`/`dc_max_kw` are a **placeholder only** -
not read by any active logic yet (`docs/10-open-questions.md` #9), included so the schema has a home
for them ahead of DC fast-charge support actually being built. `qc_max_soc_pct` (added 2026-08-08)
sits next to them (same GUI section, moved there from `vehicle` the same day) but IS live - unlike
the DC placeholders, it actively caps `mapping_engine.derive_capacity_outputs()`'s QC capacity
display fields today, PROVISIONAL/untested against real DC fast charging per that function's own
docstring.

**AC taper convergence rate - dynamically self-selected, NOT a fixed config value (2026-08-06,
twice the same day).** A first fix gave this taper `ac_recovery_ramp_s`, a fixed-time-constant fast-
attack/slow-release hysteresis matching `charge_target_taper`/`discharge_power_taper`'s own pattern -
removed hours later, same day, once a closer look at the real bench log that prompted it showed a
repeating full-cycle hunt (not just a rough jump), because "instant response, either direction" is
the wrong control model for a CC-CV charging loop (unlike the discharge/regen tapers, which
genuinely need instant response to arrest real cell sag under load - their hysteresis stays
unchanged). The final design instead **dynamically self-selects one of the existing 0-7
`chg_uprate_level` rates** (`CHG_RAMP_RAW_PER_S`, real-hardware-confirmed - 2.0kW/s at level 7,
halving per level down) based on the remaining distance to the taper's instantaneous target: always
starts at level 7, downshifts/upshifts (with hysteresis on the level switch itself, so the selected
level doesn't flap right at a boundary) as that distance narrows or grows, and each step is clamped
to land exactly on target rather than asymptotically approaching. **The dynamically-selected level
is also what's transmitted in `0x1DC`'s own uprate bits while the taper is actively converging** -
overriding the manually-configured `chg_uprate_level` only during that window - per the user's own
directive that this transmitted field is a real signal that may be "used somewhere else in the
system" and must genuinely represent the rate in use, not be decoupled from it. Firmware porting
this must replicate the level-selection algorithm itself (`bridge/management_engine.py`'s
`_select_ac_uprate_level()` and its 7 threshold constants - new, tuned starting values, not
real-hardware-confirmed) as real control logic, not just a config value like every other taper's
`recovery_ramp_s`.

`discharge_power_taper` has the same proactive shape mirrored for the low end (full power at/above
`taper_start_v`, its configured min-power floor at/below `taper_min_v`) — **plus real runtime
state**, not just a stateless formula: `recovery_ramp_s` implements fast-attack/slow-release
hysteresis (snap down immediately on a voltage dip, rate-limited ramp back up once voltage
recovers), which needs an "applied factor" value and a last-update timestamp carried between
control-loop iterations. Firmware must replicate this as stateful logic, not recompute the output
purely from the current instantaneous voltage each cycle, or the hysteresis (and the point of it —
avoiding power hunting near the threshold) is lost.

**Both `discharge_power_taper` and `charge_target_taper` also blend in a second, independent SoC
factor as of 2026-08-13** (`taper_start_soc_pct`/`taper_min_soc_pct` and
`regen_full_soc_pct`/`regen_min_soc_pct` above) — combined with the voltage factor via
`min(voltage_factor, soc_factor)`, not averaged. Firmware must replicate this as two parallel
ramp-factor computations feeding one `min()`, not just add a third input to the existing voltage
formula: SoC is the primary/smoothing input (changes slowly, wide window), voltage stays the
independent secondary/quick-cutoff input (must still be able to restrict power on its own,
regardless of what SoC reports) — see `05-battery-management-safety.md`'s "SoC + voltage combined
taper" section for the full rationale (a real per-cell-voltage quantization/narrow-window
interaction this blending was built to fix).

Note `low_voltage_cutoff`'s `min_soc_pct` is a **backup check only** (2026-07-31 fix) — firmware
must evaluate it purely for cross-checking/logging (agrees vs. disagrees with the cell-voltage
decision), never as an independent condition that can fire the cutoff on its own. `min_cell_v` and
`emergency_low_v` (both per-cell voltage) are the only fields allowed to actually trigger a cutoff
here. This is a general rule across every safety feature in this schema, not specific to this one:
real-time per-cell voltage is the sole authoritative signal for every cutoff/derate decision: SoC
never independently triggers anything.

**Temp probe primary (DID `0x1814`) / backup (`0x4AA` CAN) source selection, added 2026-08-14**:
`temp_01`-`temp_16` are stateful the same way the tapers above are — firmware must track, per
probe, whether the last-accepted value came from the DID or the CAN source, and the timestamp of
the last DID `0x1814` response, not just recompute from whichever frame arrived most recently. The
rule: DID always wins the instant a response arrives; CAN only supplies the value when the DID
response is older than `engine_timing.did_temp_fresh_window_s`. See
`06-realtime-engine-and-watchdog.md` section 1b for the full state machine `RealtimeEngine`
implements (`_ingest_rz_bus`'s `ID_TEMPS` branch + `_did_poll_loop`'s temp gate) — this is
temp-probe-specific, not a general "prefer fast broadcast over DID" rule (the rest of this schema's
DID-sourced fields, e.g. `soc_pct`, stay simple last-value-wins).

Still to be added to this schema as milestone 1 continues: Leaf per-message TX periods and the
startup-timeline/shutdown-staging ms-offset tables — currently hardcoded in
`bridge/leaf_signals.py` rather than represented as profile data (correctly hardcoded, since
they're bit-verified real-Leaf protocol values, not a user tunable - but firmware would still need
them as data to codegen from, just not as an editable field). DID-polling cadence and wind-down-
TRIGGER timing (as opposed to the fixed startup/shutdown ms tables) moved OUT of this "still
hardcoded" category on 2026-08-14 - see the `engine_timing` section above.

**Fault containment does NOT export as schema data, but the underlying requirement carries over to
firmware (docs/13 item 16.1, added 2026-08-04, direct answer to a user question: "if we fix this
here, will it carry over when we port to the STM32?").** `bridge/realtime_engine.py`'s three
background loops (`_tx_loop`, `_ingest_rz_bus`, `_did_poll_loop`) now catch an unexpected exception,
log it, and continue on the next iteration instead of letting the thread die permanently and
silently — Python's `try`/`except` itself is not something a C port replicates line-for-line. But
the SAFETY PRINCIPLE it exists for is not Python-specific and is in fact a harder requirement in
firmware than in this desktop app: a real automotive ECU's standard answer to "the main loop hit an
unexpected internal fault" is a hardware/software watchdog timer that forces a defined safe state
(or resets the MCU) if the loop stalls past a bounded time — stronger than this app's own
best-effort catch-and-continue, not weaker. Concretely, the STM32 port needs an equivalent for each
of these three loops' failure modes: (1) the TX loop stalling must not leave the Leaf bus silently
un-updated past some bounded time (this app's answer: a 2.0s software heartbeat check, `gui/app.py`'s
`HEARTBEAT_STALE_S` — firmware's answer should be a real watchdog reset, since there's no separate
GUI thread to notice a stall from), and (2) a single malformed/unexpected RZ450e frame or DID
response must not be allowed to wedge the ingest path forever (this app's answer: catch, log, drop
that one frame, keep going — firmware needs the equivalent defensive parsing, not an unguarded
decode that can panic/hang the whole ingest routine). Treat this as a new, explicit firmware
requirement alongside everything else in this doc, not something the JSON schema itself needs a
field for.

**Fixed-logic safety nets that are NOT profile data, added 2026-08-01, but still must be
replicated in firmware** (same category as output clamping, which this doc already didn't cover
explicitly): `bridge/rz450e_signals.py`'s `PLAUSIBLE_RANGES` table (the actual plausibility bounds
per signal) and `bridge/management_engine.py`'s `_check_config_sanity()` (cross-field threshold-
ordering check, e.g. an emergency tier typed less extreme than its own soft tier). Both are fixed
Python constants/logic, not something the GUI edits or the profile exports - firmware needs the
same bounds/checks hardcoded, not derived from this schema. Likewise `bridge/rz450e_signals.py`'s
`CHECKSUM_IDS`/`frame_checksum_ok()` (docs/13 item 13.5) - the checksum formula itself and which 5
IDs carry it (`0x020`/`0x023`/`0x358`/`0x3F1`/`0x424`, see `02-source-signals-rz450e.md`) are fixed,
not exported.

**Whether these two checks actually RUN at all is profile data, though** (docs/13 items 15.14/
15.15, added 2026-08-03) - `management_features.input_validation.enabled` and `management_features.
checksum_validation.enabled` (both default `true`, both shown in the example above) gate whether
`RealtimeEngine` calls `validate_inputs()`/`frame_checksum_ok()` at all, not just whether a
rejection gets reported. Firmware should treat these the same as every other feature's `enabled`
flag - read from the profile, not assumed always-on. Also note `last_known_good.json` is validated
through the same `validate_inputs()` plausibility check on load (not just live RX data, and subject
to the same `input_validation.enabled` flag), and both `profile.json` management-feature values and
`charge_emulation` values are clamped to documented bounds (`FEATURE_FIELD_BOUNDS`/
`CHARGE_EMULATION_BOUNDS`) when a profile is loaded, not just when edited live in the GUI -
firmware reading a saved profile directly should apply the same bounds rather than trusting the
file blindly.

## What this is NOT

- Not a runtime file read by the Python app moment-to-moment (that's the internal state model,
  `06-realtime-engine-and-watchdog.md`).
- Not the last-known-good data cache (also `06-realtime-engine-and-watchdog.md`) — that's live
  data, this is configuration.
- Not firmware code itself — phase 2 still has to write the actual STM32 C/C++ that reads this
  schema and implements the logic it describes.
- **Not the log panel or Dashboard window** (`08-gui-design.md`) — both are Python-GUI-only
  conveniences for a human operator watching a live PC session. Neither has any bearing on
  firmware behavior and neither is persisted in this schema.
