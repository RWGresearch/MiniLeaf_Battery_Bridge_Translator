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
5. **Real-time engine parameters** — per-message TX periods, staleness-watchdog timeouts and
   escalation windows (`06-realtime-engine-and-watchdog.md`) — these are numeric parameters, not
   just Python-app behavior, so firmware needs the same numbers.
6. **Startup/shutdown timing constants** — the staged bring-up/wind-down timings
   (`07-startup-shutdown-plan.md`) — currently hardcoded in the Leaf project's own source, but
   should be represented as data here so firmware doesn't have to hardcode them independently.

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
  "vehicle": { "car_gen": "ZE1", "battery_gen": "ZE1", "battery_kwh": 40 },
  "mappings": [
    { "inputs": ["current"], "combine": "linear", "output": "pack_current_a",
      "params": {"scale": -1.0, "offset": 0.0},
      "name": "current -> pack_current_a (SIGN INVERTED: RZ450e +discharge -> Leaf +charge)" }
  ],
  "management_features": {
    "low_voltage_cutoff": {"enabled": true, "min_cell_v": 3.0, "min_soc_pct": 8.0,
                           "emergency_low_v": 2.6, "soft_cut_persistence_s": 2.0},
    "discharge_power_taper": {"enabled": true, "taper_start_v": 3.0, "taper_zero_v": 2.6,
                              "recovery_ramp_s": 3.0},
    "charge_target_taper": {"enabled": true, "regen_full_v": 4.0, "regen_zero_v": 4.15,
                            "emergency_high_v": 4.2, "recovery_ramp_s": 3.0},
    "over_temperature_derate": {"enabled": true, "charge_derate_low_start_f": 50.0,
                                "charge_low_block_f": 32.0, "charge_derate_start_f": 90.0,
                                "charge_hard_stop_f": 113.0, "discharge_derate_start_f": 131.0,
                                "discharge_hard_stop_f": 140.0, "emergency_temp_f": 141.8},
    "cell_imbalance_monitor": {"enabled": true, "warn_delta_v": 0.1},
    "overcurrent_monitor": {"enabled": true, "continuous_discharge_warn_a": 150.0,
                            "continuous_charge_warn_a": 30.0, "persistence_s": 5.0},
    "staleness_watchdog": {"enabled": true, "soft_cut_s": 60.0, "hard_escalation_s": 5.0},
    "cell_data_cross_check": {"enabled": true, "max_delta_v": 0.15, "soft_cut_s": 60.0,
                              "hard_escalation_s": 5.0},
    "input_validation": {"enabled": true},
    "checksum_validation": {"enabled": true}
  },
  "generated_signals": {"prun": true, "voltage_latch_toggle": true, "heartbeat_1c2": true,
                        "code_1dc": true, "chg_time_5bc": true, "hist_5c0": true, "seq_5eb": true},
  "charge_emulation": {"charge_emulate": 1, "ac_taper_enabled": 1, "extended_mode": 0,
                       "require_live_data_to_charge": 1,
                       "charge_target_kw": 92.2, "chg_uprate_level": 7,
                       "ac_full_v": 4.0, "ac_zero_v": 4.15, "ac_emergency_v": 4.2,
                       "daily_target_pct": 80.0, "extended_target_pct": 100.0}
}
```

**`charge_target_taper` vs. `charge_emulation`'s AC fields (split 2026-08-01)** — as of this split,
`charge_target_taper` governs **only** `charge_limit_kw` (the regen/general-acceptance ceiling,
active regardless of charging context); the AC-charger-specific taper (`charger_limit_kw`) and the
daily/extended SoC target now live in `charge_emulation`'s `ac_full_v`/`ac_zero_v`/`ac_emergency_v`/
`daily_target_pct`/`extended_target_pct`/`extended_mode` fields instead, alongside the pre-existing
charger-ramp-emulation controls (`charge_target_kw`/`chg_uprate_level`) — regen (up to ~0.5C into
the pack) and AC charging (~0.09C) are physically different enough to need independently-tunable
curves. Firmware porting this schema must replicate BOTH as separate per-cell tapers driving their
own respective Leaf output field, not recombine them into one. Neither has an SoC-gating field (e.g.
no `taper_start_pct`) — both tapers are driven purely by continuous per-cell voltage;
`daily_target_pct`/`extended_target_pct` are only the separate AC SoC stop-point, not part of either
taper ramp. `regen_full_v`/`ac_full_v` are also both deliberately well below the pack's actual NMC
ceiling (~4.2V) — a proactive design choice for a slow-responding VCM, not the cell's real safety
limit; firmware inheriting this schema should keep that gap, not "tighten" it thinking it's overly
conservative. `charge_target_taper` also carries the same fast-attack/slow-release hysteresis as
`discharge_power_taper` (below) via its own `recovery_ramp_s` - stateful, not a pure function of
instantaneous voltage.

`discharge_power_taper` has the same proactive shape mirrored for the low end (full power at/above
`taper_start_v`, zero at/below `taper_zero_v`) — **plus real runtime state**, not just a stateless
formula: `recovery_ramp_s` implements fast-attack/slow-release hysteresis (snap down immediately on
a voltage dip, rate-limited ramp back up once voltage recovers), which needs an "applied factor"
value and a last-update timestamp carried between control-loop iterations. Firmware must replicate
this as stateful logic, not recompute the output purely from the current instantaneous voltage each
cycle, or the hysteresis (and the point of it — avoiding power hunting near the threshold) is lost.

Note `low_voltage_cutoff`'s `min_soc_pct` is a **backup check only** (2026-07-31 fix) — firmware
must evaluate it purely for cross-checking/logging (agrees vs. disagrees with the cell-voltage
decision), never as an independent condition that can fire the cutoff on its own. `min_cell_v` and
`emergency_low_v` (both per-cell voltage) are the only fields allowed to actually trigger a cutoff
here. This is a general rule across every safety feature in this schema, not specific to this one:
real-time per-cell voltage is the sole authoritative signal for every cutoff/derate decision: SoC
never independently triggers anything.

Still to be added to this schema as milestone 1 continues: real-time engine parameters (per-message
TX periods, watchdog timeouts) and startup/shutdown timing constants — currently hardcoded in
`bridge/leaf_signals.py` rather than represented as profile data, which is fine for the Python app
but will need to be exposed here before a firmware codegen script can consume them without reading
Python source.

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
