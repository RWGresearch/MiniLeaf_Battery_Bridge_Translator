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

**Real-hardware testing itself is organized in `15-real-hardware-test-checklist.md` (added
2026-08-04)** — that doc is the step-by-step "how to actually run the test" companion, covering
every safety feature one at a time against the real Leaf + real bench pack, not just the items
below. Part 2 here stays as the flat list of what was flagged during the 2026-08 review pass;
`docs/15` is the place new real-hardware test planning should go from here on.

## How to use this doc

Check items off as they're written/run. For software-testable items, note the test file/function
once it exists. For real-hardware items, note the date and result once tested, and fold the finding
back into `11-manual-verification-checklist.md` (promoting a threshold from "Documented" to
"Confirmed"), same as this project's existing verification discipline.

## `config/` is now git-tracked (changed 2026-08-03, docs/13 discussion after item 15.3/15.4)

`config/*.json` was gitignored from the start of this project; the user asked for it to be tracked
instead, treating it as a running backup - `.gitignore`'s `config/*.json` line was removed.
Everything in `config/` is tracked now: `profile.json` (the auto-loaded/auto-saved default
profile), `last_known_good.json` and `fault_log.json` (continuously autosaved every 5s while the
bridge runs - genuinely noisy git history, tracked anyway per explicit user choice over the
lower-noise "settings files only" alternative), and any named snapshot profiles
(`7-31-2026-*.json`).

## `tests/check_profile_drift.py` — profile-vs-code-defaults drift report (added 2026-08-03)

Prompted by a real, found-live case: `config/profile.json` turned out to still be carrying forward
several pre-2026-08-01 researched defaults (`min_soc_pct=10.0`, `emergency_low_v=2.8`, discharge
taper `3.5/3.0`, regen taper `3.9/4.1`, `emergency_temp_f=149.0`, `warn_delta_v=0.05`) - none of the
many threshold retunings done since had ever been reflected in it, because saving only happens when
the GUI edits something, and the file had otherwise just been silently reloaded-and-resaved
unchanged, session after session. The user asked for "a method to be sure that in the future we
don't miss something like that by checking against the profile.json and see what is changed and
why."

**Deliberately NOT one of the pass/fail `tests/test_*.py` scripts** (named `check_*` instead, and
not swept up by any "run every `test_*.py`" loop) - a saved profile *should* differ from code
defaults whenever the user has deliberately tuned something, so "differs" isn't a failure by
itself. It's a diagnostic report for a human to read after a code change touches a default: run
`py tests/check_profile_drift.py [path]` (defaults to `config/profile.json`) and it prints, field
by field, three categories:
- **CHANGED** — exists in both, values differ (either a deliberate tuning to keep, or a code
  default that moved and this profile should be re-saved - the report can't tell which, a human
  has to).
- **MISSING FROM PROFILE** — the current code defines this field but the saved file predates it
  (e.g. `input_validation`/`checksum_validation`, added 2026-08-03) - already handled safely at
  load time (the code default applies), this is purely visibility.
- **ORPHANED IN PROFILE** — the saved file has a field current code no longer defines (renamed/
  removed) - already silently dropped at load time (`ManagementEngine.from_dict()`'s known-fields
  filter), again purely visibility.

Run it any time after changing a default in `bridge/management_engine.py`'s `default_config()` or
`bridge/leaf_signals.py`'s slider/check tables, to see exactly what it does to the actual saved
profile rather than assuming nothing's affected.

---

## Part 1 — Software-testable (no hardware needed)

### Boundary-value sweeps (docs/13 item 6.3)
Several existing tests check well past a threshold/persistence window but never right at its
boundary. For each, add a test at (or just before/after) the exact configured value, not just
"clearly inside" / "clearly outside":
- [x] `low_voltage_cutoff.soft_cut_persistence_s` (2.0s) - just-before (e.g. 1.9s) should NOT latch
      - **done 2026-08-03**, `tests/test_management_engine.py::test_boundary_low_voltage_soft_cut_persistence`
- [x] `overcurrent_monitor.persistence_s` (5.0s) - just-before should NOT warn - **done 2026-08-03**,
      `tests/test_management_engine.py::test_boundary_overcurrent_persistence`
- [x] `over_temperature_derate.emergency_temp_f` (141.8°F/61°C as of 2026-08-01) - just below vs.
      just above - **done 2026-08-03**, `tests/test_management_engine.py::test_boundary_emergency_temp`
      (also confirms the `>=` comparison fires exactly at 141.8°F)
- [x] `cell_imbalance_monitor.warn_delta_v` (100mV as of 2026-08-01) - just below vs. just above -
      **done 2026-08-03**, `tests/test_management_engine.py::test_boundary_cell_imbalance_warn_delta`
- [x] `cell_data_cross_check.max_delta_v` (150mV, new 2026-08-01) - just below vs. just above, plus
      its own soft/hard escalation timing (60s/+5s) - **done 2026-08-03**,
      `tests/test_management_engine.py::test_boundary_cell_data_cross_check_delta_and_escalation_timing`
      (escalation windows shrunk to 0.15s each in the test so it doesn't need to wait the real 60s/+5s)
- [x] `staleness_watchdog` - just-before/just-after both the 60s soft and +5s hard escalation -
      **done 2026-08-03**,
      `tests/test_management_engine.py::test_boundary_staleness_watchdog_soft_and_hard_escalation`
      (same shrunk-window pattern)

### Tighten loose assertions (docs/13 item 6.4)
- [x] `test_f1_cold_block_uses_coldest_probe`'s second check - assert the exact expected
      `charge_limit_kw` value (the unclamped `DEFAULTS` value), not just `> 0.0` - **done 2026-08-03**
- [x] `test_f3_cold_derate_ramp`'s midpoint check - assert close to the exact computed 0.5 factor,
      not a loose `0.35 < factor < 0.65` range - **done 2026-08-03**

### New this session - not yet covered by any test
- [x] AC "charge target reached" contactor-drop path - **done 2026-08-01**,
      `tests/test_management_engine.py::test_ac_charge_target_reached_sets_full_charge_flag`
- [x] Overvoltage emergency `fault_log` entries (both regen and AC-charger tiers) - **done
      2026-08-01**, `tests/test_management_engine.py::test_overvoltage_emergency_fault_log_entries`
- [x] `manual_reset` against an already-auto-cleared soft/warn entry - **done 2026-08-01**,
      `tests/test_fault_log.py::test_manual_reset_on_already_auto_cleared_soft_entry`
- [x] Staleness watchdog with a signal that goes stale mid-session (not just never-seen) - **done
      2026-08-03**, `tests/test_management_engine.py::
      test_staleness_watchdog_flags_a_signal_that_went_stale_mid_session` (backdates one live
      signal's timestamp directly, confirms it's named specifically and not lumped in with "never
      seen").
- [x] `rz450e_signals.validate_inputs()` rejecting an actual out-of-plausible-range decoded value
      and firing `input_validation_reject` - **done 2026-08-03**,
      `tests/test_realtime_engine.py::test_disabling_input_validation_clears_its_fault_log_entry_live`
      (drives a real rejection through `note_rejected_input`) and
      `test_input_validation_toggle_lets_implausible_values_through_when_disabled` (drives it through
      the actual `_ingest_validated()` ingest path).
- [x] `cell_data_cross_check` soft/hard escalation on a genuine per-cell-vs-pack-summary
      disagreement - **done 2026-08-03**, `tests/test_management_engine.py::
      test_cell_data_cross_check_soft_and_hard_escalation` (this feature had ZERO test coverage at
      all before this, despite being a real soft->hard escalating cutoff).
- [x] `_check_config_sanity()` detecting a deliberately-inverted config - **done 2026-08-03**,
      `tests/test_management_engine.py::test_config_sanity_detects_inverted_threshold` (also had
      zero coverage before this).
- [x] Hard-cut latching (`ManagementEngine._hard_latched`, item 5.1) - **done 2026-08-01/03**,
      `tests/test_management_engine.py::test_hard_cut_latches_and_survives_recovery` (a),
      `test_hard_cut_latch_clears_on_notify_session_start` (b),
      `test_hard_cut_latch_clears_on_notify_charge_replug` (c),
      `test_hard_cut_re_latches_immediately_if_condition_is_still_bad` (d, plus the staleness-
      specific 14.3 tests confirm a non-staleness hard cut never wind-down-clears itself either).
- [x] Both tapers' fast-attack/slow-release hysteresis - **done 2026-08-03**,
      `tests/test_management_engine.py::test_discharge_power_taper_hysteresis_fast_attack_slow_release`
      and `test_charge_target_taper_regen_hysteresis_fast_attack_slow_release`. **Correction**: this
      item previously (incorrectly) claimed `discharge_power_taper`'s hysteresis "has a direct
      test" - it did not, found during a full sweep; neither taper's hysteresis had ever actually
      been tested despite both features documenting the behavior extensively. Both closed together.
- [x] `BusConnection`'s reconnect-race fix (items 3.1/3.2) - **partially done 2026-08-03**, new
      `tests/test_can_backend.py` (first coverage for this module):
      `test_disconnect_interrupts_the_monitor_promptly_not_after_the_full_interval` (deterministic -
      confirms the monitor thread exits within ~1s of `disconnect()`, not the full
      `RECONNECT_INTERVAL_S`=3.0s a reverted-to-`time.sleep()` implementation would take - this is
      the core of the item 3.1 fix) and
      `test_rapid_disconnect_reconnect_cycling_never_leaves_the_connection_without_a_monitor`
      (stress-cycles connect/disconnect 10x, confirms the bus always ends up connected with exactly
      one live monitor thread - no leaked-away monitor). Both stable across 5 repeated runs.
      **Deliberately does NOT attempt the exact microsecond-scale race in item 3.2** (a reconnect
      completing right as `disconnect()` fires) - not practically testable deterministically without
      adding test-only synchronization hooks to `bridge/can_backend.py` itself, which this project
      doesn't have; real-hardware/manual verification remains the only way to close that specific
      sub-case (see Part 2 below).

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
      Note: the input (`temp_max` from `0x4A7`) is already cross-checked live against the actual
      max of the 16 individual `0x4AA` probes (`temp_data_cross_check`, docs/13 item 16.2) - it's
      specifically the 32-140°F -> 0-100% window that needs confirming against a real dash, not
      which temperature source feeds it.
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

- [ ] `require_live_data_to_charge` (docs/13 item 13.1b, added 2026-08-03, reworked same day to a
      one-time "has genuinely live data ever arrived" startup gate rather than an ongoing timer) -
      never confirmed against real RZ450e hardware that a fresh bridge session actually gets all 96
      per-cell voltages populated before a real charge request shows up in practice (i.e. the gate
      doesn't accidentally block every real charge attempt at startup); and separately confirm it
      correctly stays blocked with the adapter disconnected/not yet sending.
- [ ] Staleness watchdog now also sets `full_charge_flag` on its soft-cut stage (docs/13 item 13.1,
      added 2026-08-03) - logic is unit-tested, but never confirmed on real hardware that this is
      the right response for an ACTIVE charge session specifically (vs. just zeroing the power
      limits as before) - watch for any unexpected real-vehicle side effect of `full_charge_flag`
      firing from a data-staleness event rather than an actual RZ450e-side condition.
- [ ] Toyota checksum validation (docs/13 item 13.5, added 2026-08-03) - logic is unit-tested against
      synthetic corrupted frames, but never observed rejecting a REAL corrupted frame from real
      hardware (bus noise, a marginal termination, etc.) - and conversely, confirm it does NOT
      false-reject genuine real traffic (i.e. this project's own checksum formula still matches the
      real ECU's on live bus, not just in the confirmed historical logs it was derived from).
- [ ] Replug minimum-gap threshold (docs/13 item 13.4, added 2026-08-03, reuses `CHG_END_STOP_S` =
      3.0s) - never confirmed against a real physical unplug/replug cycle; confirm 3.0s is neither
      so short a quick VCM retry burst could still exceed it, nor so long a real quick replug fails
      to register as one.

---

## Part 3 — Design decisions flagged during the coverage audit, not yet resolved

Found auditing every Leaf output signal for whether it has a live driver (docs/13 item 4.3's
"confirm we are not missing any other maps"). Not test items - decisions for the user, added to the
bottom of `docs/13-review-checklist-2026-08-01.md` directly rather than here.
