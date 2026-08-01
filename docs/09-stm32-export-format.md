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
just an illustrative sketch — real example, one tie + the current battery-management schema:

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
    "low_voltage_cutoff": {"enabled": true, "min_cell_v": 3.0, "min_soc_pct": 10.0, "emergency_low_v": 2.8},
    "discharge_power_taper": {"enabled": true, "taper_start_v": 3.5, "taper_zero_v": 3.0,
                              "recovery_ramp_s": 3.0},
    "charge_target_taper": {"enabled": true, "regen_full_v": 3.9, "regen_zero_v": 4.1,
                            "emergency_high_v": 4.3, "daily_target_pct": 80.0,
                            "extended_target_pct": 100.0, "extended_mode": false},
    "over_temperature_derate": {"enabled": true, "charge_derate_start_f": 90.0, "charge_hard_stop_f": 113.0,
                                "charge_low_block_f": 32.0, "discharge_derate_start_f": 131.0,
                                "discharge_hard_stop_f": 140.0},
    "staleness_watchdog": {"enabled": true, "soft_cut_s": 60.0, "hard_escalation_s": 5.0}
  },
  "generated_signals": {"prun": true, "voltage_latch_toggle": true, "heartbeat_1c2": true,
                        "code_1dc": true, "chg_time_5bc": true, "hist_5c0": true, "seq_5eb": true}
}
```

Note `charge_target_taper` has **no SoC-gating field** (e.g. no `taper_start_pct`) — per the
2026-07-31 correction in `05-battery-management-safety.md`, the taper is driven purely by
continuous per-cell voltage (`regen_full_v`/`regen_zero_v`); `daily_target_pct`/`extended_target_pct`
are only the separate SoC stop-point, not part of the taper ramp. Firmware must replicate that same
separation, not re-introduce an SoC-gated taper. Also note `regen_full_v` (3.9V) is deliberately
well below the pack's actual NMC ceiling (~4.2V) — this is a proactive design choice for a
slow-responding VCM, not the cell's real safety limit; firmware inheriting this schema should keep
that gap, not "tighten" it thinking it's overly conservative.

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
