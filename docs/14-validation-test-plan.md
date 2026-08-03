# Validation / Test Plan

Working document, added 2026-08-01, gathering every "needs a test," "needs real hardware," or
"can't validate yet" note scattered across `docs/13-review-checklist-2026-08-01.md` and this
session's implementation pass, so they don't get lost. Two different kinds of item live here:

- **Software-testable** — can be written as a `tests/test_*.py` script and run on a desk, no
  hardware needed. These are concrete TODOs.
- **Real-hardware-only** — can only be closed by testing against the actual RZ450e bench pack
  and/or a real Leaf, per `11-manual-verification-checklist.md`'s existing discipline. Listed here
  as what the test will need to check, not something this doc itself can resolve.

Nothing in this doc is a promise about *when* — it's a checklist to work through, same spirit as
`13-review-checklist-2026-08-01.md`.

## How to use this doc

Check items off as they're written/run. For software-testable items, note the test file/function
once it exists. For real-hardware items, note the date and result once tested, and fold the finding
back into `11-manual-verification-checklist.md` (promoting a threshold from "Documented" to
"Confirmed"), same as this project's existing verification discipline.

---

## Part 1 — Software-testable (no hardware needed)

### Boundary-value sweeps (docs/13 item 6.3)
Several existing tests check well past a threshold/persistence window but never right at its
boundary. For each, add a test at (or just before/after) the exact configured value, not just
"clearly inside" / "clearly outside":
- [ ] `low_voltage_cutoff.soft_cut_persistence_s` (2.0s) - just-before (e.g. 1.9s) should NOT latch
- [ ] `overcurrent_monitor.persistence_s` (5.0s) - just-before should NOT warn
- [ ] `over_temperature_derate.emergency_temp_f` (141.8°F/61°C as of 2026-08-01) - just below vs.
      just above
- [ ] `cell_imbalance_monitor.warn_delta_v` (100mV as of 2026-08-01) - just below vs. just above
- [ ] `cell_data_cross_check.max_delta_v` (150mV, new 2026-08-01) - just below vs. just above, plus
      its own soft/hard escalation timing (60s/+5s)
- [ ] `staleness_watchdog` - just-before/just-after both the 60s soft and +5s hard escalation

### Tighten loose assertions (docs/13 item 6.4)
- [ ] `test_f1_cold_block_uses_coldest_probe`'s second check - assert the exact expected
      `charge_limit_kw` value (the unclamped `DEFAULTS` value), not just `> 0.0`
- [ ] `test_f3_cold_derate_ramp`'s midpoint check - assert close to the exact computed 0.5 factor,
      not a loose `0.35 < factor < 0.65` range

### New this session - not yet covered by any test
- [x] AC "charge target reached" contactor-drop path - **done 2026-08-01**,
      `tests/test_management_engine.py::test_ac_charge_target_reached_sets_full_charge_flag`
- [x] Overvoltage emergency `fault_log` entries (both regen and AC-charger tiers) - **done
      2026-08-01**, `tests/test_management_engine.py::test_overvoltage_emergency_fault_log_entries`
- [x] `manual_reset` against an already-auto-cleared soft/warn entry - **done 2026-08-01**,
      `tests/test_fault_log.py::test_manual_reset_on_already_auto_cleared_soft_entry`
- [ ] Staleness watchdog with a signal that goes stale mid-session (not just never-seen) - the
      watchdog now covers all ~127 registered input signals (item 1.1); no test currently drives
      an individual signal from fresh to stale and checks the watchdog fires
- [ ] `rz450e_signals.validate_inputs()` - no test exercises an actual out-of-plausible-range
      decoded value being rejected and `note_rejected_input`/the `input_validation_reject` fault
      firing
- [ ] `cell_data_cross_check` - no test drives a genuine per-cell-vs-pack-summary disagreement
      (e.g. per-cell array reads 3.70V but `cell_min`/`cell_max` reads something 200mV+ off) through
      its soft/hard escalation
- [ ] `_check_config_sanity()` - no test constructs a deliberately-inverted config (e.g.
      `emergency_low_v > min_cell_v`) and confirms the `config_sanity` fault fires with the right
      description
- [ ] Hard-cut latching (`ManagementEngine._hard_latched`, item 5.1) - no test confirms: (a) a hard
      cut stays asserted after the triggering reading recovers, (b) `notify_session_start()` clears
      it, (c) `notify_charge_replug()` clears it, (d) it does NOT clear on anything else (e.g. the
      reading merely recovering)
- [ ] `BusConnection`'s reconnect-race fix (items 3.1/3.2) - no automated test simulates the
      disconnect-then-reconnect-within-the-monitor-window scenario against the real lock/Event
      logic; worth a threading-level test using short timeouts, not just manual verification
- [ ] Regen taper hysteresis (`_regen_factor_applied`) - `discharge_power_taper`'s hysteresis has a
      direct test; the newly-added regen-side equivalent doesn't yet (fast-attack-slow-release curve
      + emergency tier forcing the applied factor straight to 0)

---

## Part 2 — Real-hardware-only (bench pack / real Leaf required)

Every numeric threshold changed in this session needs the same real-hardware confirmation pass
`11-manual-verification-checklist.md` already tracks for the rest of the safety envelope - **none of
these are hardware-validated, they are edited numbers pending confirmation**:

- [ ] Low-voltage emergency cutoff: 2.60V (was 2.80V)
- [ ] Low-voltage SoC backup floor: 8% (was 10%)
- [ ] Discharge taper window: 3.00V full power -> 2.60V zero (was 3.50V -> 3.00V)
- [ ] Regen taper window: 4.00V full power -> 4.15V zero, plus new hysteresis (was 3.90V -> 4.10V,
      no hysteresis)
- [ ] AC-charger taper window: 4.00V -> 4.15V (new feature, split from the old combined taper)
- [ ] Over-temp emergency hard cut: 141.8°F/61°C exactly (was 149°F/65°C) - deliberately thin margin
      above the 140°F/60°C soft stop, worth confirming this doesn't nuisance-trip during normal
      hot-weather operation
- [ ] Cell imbalance warn spread: 100mV (was 50mV)
- [ ] Cell data cross-check delta: 150mV (brand new feature/threshold)

Other real-hardware-only items, some carried over from `docs/10`:

- [ ] **docs/10 #7** - does the RZ450e pack's own internal cell-balancing hardware still run in
      this configuration? No way to answer without extended real-hardware observation (cell spread
      over multiple sessions).
- [ ] **docs/10 #8** - overcurrent monitor thresholds (150A discharge / 30A charge-regen) are not
      tuned to a real drive cycle; the `0x023` sensor is structurally unable to see the pack's real
      ~500A/660A range at all - a future wider-range current sensor would be needed before that gap
      can close.
- [ ] **docs/10 #9** - DC fast charging (~430A) is entirely outside this project's current scope;
      every charge-side safety feature was sized against AC-charger-scale current.
- [ ] **docs/10 #13** - `temp_segment_pct`'s provisional mapping formula (added 2026-08-01) has
      never been checked against a real dash display, unlike `soc_correction`/`capacity_bars_raw`.
- [ ] Fault latching's two re-arm triggers (`notify_session_start()`/`notify_charge_replug()|) need
      confirming against a REAL bus-wake/replug event, not just the software-level phase transition
      - does a genuine ignition cycle or charger replug on real hardware actually produce the
      `waiting_for_wake -> startup` / fresh-`0x1F2` transitions this logic depends on, reliably?
- [ ] CAN connection-health lights (TX OK indicator, reconnect counter, `send_errors` counter, item
      2.1) - never observed against a real adapter fault (unplug mid-session, bus-off condition)
- [ ] `.trc` data logger (new, item N) - confirm a logged session actually opens cleanly in
      PCAN-Explorer/PCAN-View, not just that the Python-side writer runs without error
- [ ] Mouse-wheel-disabled comboboxes (item 4.3) - quick manual GUI check that scrolling the page
      while hovering a dropdown no longer changes its value, on the actual Windows environment this
      runs on (Tk's wheel-event behavior can vary slightly by platform)

---

## Part 3 — Design decisions flagged during the coverage audit, not yet resolved

Found auditing every Leaf output signal for whether it has a live driver (docs/13 item 4.3's
"confirm we are not missing any other maps"). Not test items - decisions for the user, added to the
bottom of `docs/13-review-checklist-2026-08-01.md` directly rather than here.
