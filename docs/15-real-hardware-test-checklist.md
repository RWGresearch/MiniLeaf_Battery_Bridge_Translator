# Real-Hardware Test Checklist

**Preliminary working document, added 2026-08-04.** Purpose: a concrete, one-at-a-time list of
everything to actually go test against the **real Leaf car + real RZ450e bench pack**, now that
the software side (`docs/13`, 67/67 items) is closed out. Two different kinds of confirmation live
here, per the user's own split:

1. **Data confirmation** — done in bulk, via the `.trc` data logger (`bridge/trc_log.py`, Start
   Log/Stop Log in the main window), not one item at a time. Part A below.
2. **Safety-feature confirmation** — done **one feature at a time**, live, watching the Dashboard
   and Fault History panel while a specific condition is produced. Part B below.

A real RZ450e-emulator app (separate future project, out of scope here) would eventually let every
threshold be hit deliberately and safely, including genuine extremes. Until that exists, Part B is
organized so as much as possible is testable **right now** against the real bench pack, using one
technique in particular:

## Technique: bracket the threshold, not the battery

Every safety threshold in this app is a **live-editable config value**, not a compiled constant.
For most features, you do not need the real pack to actually reach a dangerous voltage/temp/current
to test the *logic* — you can instead temporarily move the feature's threshold to sit just past
wherever the pack's real, current, everyday value already is, confirm the feature reacts exactly as
designed, then put the threshold back to its real default. Example: if the pack is resting at
3.70V/cell, temporarily set `discharge_power_taper`'s `taper_zero_v` to 3.75V — the real bench pack
now sits "past zero-power" on that curve with zero real risk, and you can watch `discharge power
limit` actually ramp down on the real Leaf dash exactly as it would at the true 2.60V default. This
confirms the wiring, the math, the Leaf-side effect, and the fault-log entry all work correctly on
real hardware — it just doesn't confirm the *default number itself* is the physically-correct one
(that part genuinely does need either real extremes or the future emulator — flagged per-item
below as **[NUMBER UNCONFIRMED]**, separate from **[LOGIC TESTABLE NOW]**).

**Always restore the real default value after each bracketed test** — use
`tests/check_profile_drift.py` afterward to confirm `config/profile.json` doesn't accidentally keep
a test threshold as the new saved default.

## How to use this doc

Check items off as they're actually run against real hardware, one session at a time. For each,
fill in date/result/notes, then fold the outcome back into `11-manual-verification-checklist.md`
(promoting the feature from "Documented" to "Confirmed" or "Needs Validation"), same discipline
this project already uses. This doc doesn't replace `14-validation-test-plan.md` (that one tracks
software-testable TODOs plus a flat list of hardware items surfaced during the 2026-08 review
pass) — this one is the organized, step-by-step "how to actually run the test" companion, covering
every safety feature in `05-battery-management-safety.md`, not just the recently-changed ones.

---

## Part A — Data confirmation (bulk, via `.trc` log)

Start a log at the top of a normal session (driving and/or a charge session), let it run, then
review it (PCAN-Explorer/PCAN-View, or a quick Python read of the `.trc`) against the live Dashboard
values shown at the same moments.

**Also check the `<name>_log_output.txt` companion file Start Log creates alongside the `.trc`**
(added 2026-08-08, `docs/08-gui-design.md`'s Log panel section, `main.py` Rev 63) — it has the exact
settings this session actually ran with (every mapping tie, every management threshold, vehicle
spec, charge-emulation config) plus a timestamped mirror of everything the Log panel printed during
the capture (connection events, sequencer phase changes, cut/warning assertions). When something in
the `.trc` looks unexpected, this is the first place to check *what the app was actually configured
to do at that moment* before assuming a code bug.

- [ ] All 96 per-cell voltages decode into a plausible 3.6-4.2V range, none frozen/stuck at one
      value the whole session.
- [ ] Pack voltage (`0x020`) reads ~348V resting, consistent with cell-voltage sum.
- [ ] Current sign convention: discharge reads positive, charge/regen reads negative (or vice
      versa per your confirmed convention) — cross-check against LeafSpy's own sign on the Leaf
      side during the same moment.
- [ ] All 16 temp probes plausible (no stuck values), roughly consistent with each other and with
      `temp_max`/`temp_min` (`0x4A7`).
- [ ] SoC / GIDS / capacity bars track a real charge or discharge session sensibly (no jumps,
      freezes, or sign flips) — cross-check dash SOC% and capacity bars against LeafSpy/real dash.
- [ ] Fault History panel shows **no** `input_validation_reject` or checksum-reject entries during
      genuinely normal operation (a real false-positive here would mean the plausibility ranges or
      checksum formula don't actually match this live pack).
- [ ] `alive_358`/`alive_3F1`/`counter_5s` all visibly increment in the log, never stuck (these feed
      the staleness watchdog directly).

---

## Part B — Safety features, one at a time

Recommended order: lifecycle/plumbing first (nothing else can be trusted if these are wrong), then
the features that gate charging, then the voltage/temp/current protection features, then the
monitor-only/data-integrity features.

### B1. Startup/shutdown sequencing
- [ ] **[LOGIC TESTABLE NOW]** Power up the real Leaf (ignition on) with the bridge already
      running and armed (Start Bridge pressed, `waiting_for_wake`). Confirm the sequencer advances
      to `startup` only once real Leaf-bus traffic appears, and the staged startup timing plays out
      correctly on the real VCM (no dash errors, contactors close normally).
- [ ] **[LOGIC TESTABLE NOW]** Power down the Leaf normally. Confirm all four shutdown triggers +
      the fifth (staleness-driven) still leave the sequencer in a sane end state, and that
      `LB_RefusetoSleep` correctly reflects ignition freshness on the real bus.
- [ ] **[LOGIC TESTABLE NOW]** Confirm the post-shutdown re-arm actually waits for a genuinely
      quiet bus on the real VCM (some real Leaf VCMs keep transmitting briefly after a manual
      power-down) rather than re-arming on a flat timer.
- Result / date / notes:

### B2. Staleness watchdog (`06-realtime-engine-and-watchdog.md` section 3)
- [ ] **[LOGIC TESTABLE NOW]** With the bridge running and the Leaf on, physically unplug the
      RZ450e adapter (or disconnect that PCAN channel in the app). Confirm: soft cut
      (`capacity_empty`, `full_charge_flag=1`) at ~60s, hard cut (`relay_cut_request`) at ~65s
      total, both visible in the Fault History panel and reflected on the real Leaf dash.
      Reconnect and confirm it clears once fresh data resumes (subject to B3's latch behavior
      below).
- Result / date / notes:

### B3. Hard-cut latch + re-arm (`_hard_latched`, `notify_session_start`/`notify_charge_replug`)
- [ ] **[LOGIC TESTABLE NOW, via bracketing]** Force any hard cut using the bracket technique
      above (e.g. temporarily raise `emergency_low_v` above the pack's real resting voltage).
      Confirm the condition latches, then restore the threshold to its real default and confirm
      the fault stays latched (does NOT silently clear on its own).
- [ ] **[LOGIC TESTABLE NOW]** With a hard cut latched, press Stop Bridge then Start Bridge (no
      real ignition cycle). Confirm the latch does **not** clear (this was the bug fixed in
      `docs/13` item 12.1 — re-confirm on real hardware, not just the unit test).
- [ ] **[LOGIC TESTABLE NOW]** With a hard cut latched, perform a real ignition off/on cycle on
      the Leaf. Confirm the latch clears via `notify_session_start()` (assuming the underlying
      condition is no longer present).
- [ ] **[LOGIC TESTABLE NOW, if a charger is available]** With a hard cut latched during a charge
      session, physically unplug and replug the charger (a real gap, not a quick reconnect).
      Confirm the latch clears only after a genuine ≥3.0s absence of `charge_permission_input`,
      not on a brief blip.
- Result / date / notes:

### B4. Charge-start data gate (`require_live_data_to_charge`)
- [ ] **[LOGIC TESTABLE NOW]** Start the bridge with the RZ450e adapter disconnected, then plug
      in the Leaf charger / request a charge. Confirm the ramp does **not** start (gate blocks it).
      Then connect the RZ450e adapter and let all 96 cell voltages + `temp_max`/`temp_min` go live;
      confirm the ramp starts once that's satisfied, without needing a fresh charge request.
- Result / date / notes:

### B5. Charger-request ramp emulation (dual trigger)
- [ ] **[LOGIC TESTABLE NOW, real charge session]** Plug in the real Leaf to charge. Confirm the
      ramp only becomes active once BOTH a real `0x1F2` request AND `charge_permission_input` are
      present, and the ramp rate/cap match the confirmed 2.0kW/s-at-level-7 formula.
- [ ] **[LOGIC TESTABLE NOW]** During a real charge session, withhold/disable
      `charge_permission_input` on the RZ450e side (if controllable) while the Leaf keeps
      requesting. Confirm `full_charge_flag`/`charge_limit_kw=0`/`charger_limit_kw=-10` assert, and
      the sequencer starts its charge-end wind-down.
- Result / date / notes:

### B6. AC charge target + taper (`ac_charge_taper`)
- [ ] **[NUMBER + LOGIC TESTABLE NOW]** Let a real charge session run to the daily target (80%
      default). Confirm charging stops and `full_charge_flag` sets at the real target, and that the
      dash reflects a normal, non-error charge-complete state (not a RED error).
- [ ] **[LOGIC TESTABLE NOW]** Toggle `extended_mode` and confirm a real session instead targets
      100%.
- [ ] **[LOGIC TESTABLE NOW, via bracketing near end-of-charge]** Near the top of a real charge
      session (cells naturally approaching 4.0V+), watch `charger_limit_kw` taper down live as the
      worst cell rises, and settle at the configured `ac_min_kw` floor (not zero) — no bracketing
      needed if the pack is genuinely charged near full; otherwise temporarily lower `ac_full_v`/
      `ac_min_v` (renamed from `ac_zero_v` 2026-08-06 — see B20) to bracket the pack's actual
      present voltage. See B20 for the fuller 2026-08-06 rework test coverage (min-kW floor, cutoff
      voltage, hysteresis) — this item is the original, still-valid "does the taper track voltage at
      all" check.
- [ ] **[LOGIC TESTABLE NOW]** Confirm `ac_charge_taper` (and `charger_limit_kw`) ONLY reacts while
      actually charging — see B20's new 2026-08-07 item for the "must stay flat while driving"
      counterpart test.
- Result / date / notes:

### B7. Charge/regen power limit (`charge_target_taper`, regen-only)
- [ ] **[LOGIC TESTABLE NOW, via bracketing or real regen near-full]** During real regenerative
      braking (ideally with the pack already at a higher SoC, closer to the 4.00-4.15V window), confirm
      `charge_limit_kw` ramps down as the worst cell rises. If the pack isn't naturally near that
      window, bracket `regen_full_v`/`regen_zero_v` down to the pack's actual present voltage and
      confirm the same ramping behavior and fast-attack/slow-release hysteresis on recovery.
- [ ] **[NUMBER UNCONFIRMED]** Whether 4.00V/4.15V/4.20V are actually early/wide enough given the
      real VCM's response lag to a `charge_limit_kw` change — needs an actual descending-hill regen
      event near a genuinely high SoC.
- Result / date / notes: **FINDING (2026-08-09, real ZE1 40kWh Leaf)**: `regen_min_kw` reaches true
  zero cleanly. `regen_max_kw` only does anything once set BELOW what the real car can actually
  accept — on this real 40kWh test vehicle, anything from 70kW down to ~40kW is a no-op, since the
  car's own regen ceiling sits around 40kW (a Leaf limit, not a bridge setting). Peak regen capability
  by generation/pack, for reference when testing a different car: 1st-gen LEAF (24/30kWh) ~20-30kW,
  2nd-gen LEAF (40kWh) just over 40kW, LEAF PLUS (62kWh) ~60-80kW peak bursts. Logged in
  `docs/05`'s regen min/max default row, `docs/16` A3, and the regen help text (`gui/panels.py`).
  Not a full B7 pass — the voltage-ramp behavior itself still needs its own live retest.

### B8. Discharge power taper
- [ ] **[LOGIC TESTABLE NOW, via bracketing]** During real driving, bracket `taper_start_v`/
      `taper_zero_v` up to just above the pack's actual present cell voltage and confirm discharge
      power visibly ramps down on the real dash, then recovers only gradually (not instantly) once
      the bracket is removed — confirming the fast-attack/slow-release hysteresis on real hardware.
- [ ] **[NUMBER UNCONFIRMED]** Whether the real 3.00V/2.60V window and 3.0s recovery ramp feel right
      under genuine acceleration-load sag — needs actual hard-acceleration testing at a real,
      naturally low SoC (or after extended real discharge).
- Result / date / notes: **FINDING (2026-08-09, real ZE1 40kWh Leaf)**: `discharge_max_kw` around
  40kW brings the turtle (reduced-power) dash icon on; `discharge_min_kw` below roughly 3kW makes
  the car start turning off power systems entirely. Both are the real Leaf's own reaction to the
  transmitted `discharge_limit_kw` value, not a taper bug. Matches the user's own currently-saved
  profile (`discharge_min_kw`=5.0, `discharge_max_kw`=40.0 — already set with margin above/below
  these observed thresholds). Logged in `docs/05`'s discharge min/max default row, `docs/16` A2, and
  the discharge help text (`gui/panels.py`). Not a full B8 pass — exact thresholds not yet swept,
  and the voltage-ramp behavior itself still needs its own live retest.

### B9. Low-voltage cutoff (`capacity_empty` soft, `relay_cut_request` hard)
- [ ] **[LOGIC TESTABLE NOW, via bracketing]** Bracket `emergency_low_v` (hard) and the min-cell
      cutoff (soft) up to just above the pack's real present voltage one at a time; confirm soft
      cut fires with the 2.0s persistence window (a brief bracket-and-immediately-restore should
      NOT latch it), and hard cut fires instantly with no persistence.
- [ ] **[NUMBER UNCONFIRMED]** The real 3.00V/2.60V thresholds themselves, only confirmable via an
      actual deep-discharge test (or the future emulator).
- Result / date / notes:

### B10. Over-temperature derate
- [ ] **[LOGIC TESTABLE NOW, negative test]** At normal ambient/room temperature, confirm the
      status text reports no derate active on either the hot or cold side (nothing should be
      falsely triggering at everyday temps).
- [ ] **[LOGIC TESTABLE NOW, via bracketing]** Bracket `charge_derate_low_start_f`/
      `charge_low_block_f` (cold side) and the hot-side derate/hard-stop values down/up to just past
      the pack's real current temp reading one at a time; confirm charge/discharge power ramps
      correctly, cold-side keys off the coldest probe (not hottest), hot-side keys off the hottest
      probe (not coldest).
- [ ] **[NUMBER UNCONFIRMED]** Real cold-derate-start/low-block (10°C/0°C), hot-side derate/hard-stop
      (32/45°C charge, 55/60°C discharge), and emergency (61°C) — genuinely need a real cold-soak or
      hot-soak test, or the future emulator.
- Result / date / notes:

### B11. Cell imbalance monitor (monitor only)
- [ ] **[LOGIC TESTABLE NOW]** Passively observe the real pack's natural cell spread over a normal
      session — confirm the status text reports the correct live spread value and never asserts a
      cutoff regardless of spread (monitor-only, by design).
- [ ] **[LOGIC TESTABLE NOW, via bracketing]** Temporarily lower `warn_delta_v` below the pack's
      actual observed spread; confirm the warning text appears, then restore and confirm it clears.
- Result / date / notes:

### B12. Overcurrent monitor (monitor only)
- [ ] **[LOGIC TESTABLE NOW]** During real driving and/or a real charge session, confirm the
      status text tracks real current correctly, with the 5.0s persistence guard visibly not
      warning on brief accel/regen spikes.
- [ ] **[LOGIC TESTABLE NOW, via bracketing]** Temporarily lower `continuous_discharge_warn_a`/
      `continuous_charge_warn_a` below real observed driving/charging current; confirm the warning
      fires after the persistence window, never derates/cuts power.
- [ ] **[NUMBER UNCONFIRMED — sensor-limited]** The real 150A/30A thresholds against an actual
      drive cycle; also structurally can't see past the `0x023` sensor's ±204.7A ceiling regardless
      (docs/10 #8) — a future wider-range sensor would be needed to close this further.
- Result / date / notes:

### B13. Cell data cross-check (per-cell vs. `0x020` pack summary)
- [ ] **[LOGIC TESTABLE NOW]** Passively confirm the status text reports "ok" during normal
      operation with real data (no false mismatch between the two sources).
- [ ] **[NEEDS FAULT INJECTION — defer to emulator]** Forcing a genuine per-cell-vs-pack-summary
      *disagreement* on real hardware isn't safely producible without either a real decode fault or
      synthetic data — this specific direction of test is realistically deferred to the future
      RZ450e emulator.
- Result / date / notes:

### B14. Temperature data cross-check (`0x4A7` extremes vs. 16 `0x4AA` probes)
- [ ] **[LOGIC TESTABLE NOW]** Passively confirm "ok" status during normal operation.
- [ ] **[NUMBER UNCONFIRMED]** Whether the 10°F default is wide enough to avoid nuisance-tripping
      from real spatial thermal gradient across the pack's 4 physical sub-packs under real load —
      needs observation during genuine sustained driving, not just at rest.
- [ ] **[NEEDS FAULT INJECTION — defer to emulator]** Forcing a genuine mismatch (real decode
      fault) — same reasoning as B13.
- Result / date / notes:

### B15. Input plausibility validation / Toyota checksum validation
- [ ] **[NEEDS FAULT INJECTION — defer to emulator]** Both features' actual rejection behavior
      against a genuinely corrupted real frame isn't safely producible without deliberately
      injecting bad data onto the real bus. Passive confirmation that neither ever false-rejects
      normal real traffic is already covered by Part A above.
- Result / date / notes:

### B16. Hardware connection health
- [ ] **[LOGIC TESTABLE NOW]** Confirm every expected RZ450e CAN ID (diagnostic/DID traffic and
      the fast internal-bus broadcasts) actually arrives on the single combined adapter connection
      with no ID collisions (docs/10 #5 — confirmed 2026-08-04 that one channel sees both buses;
      this item is the detailed per-ID follow-through of that same confirmation).
- [ ] **[LOGIC TESTABLE NOW]** Unplug the adapter mid-session; confirm the TX-OK indicator,
      reconnect counter, and `send_errors` counter all behave correctly, and reconnecting recovers
      cleanly.
- Result / date / notes:

### B17. Signal mapping (current sign, GIDS/QC capacity)
- [ ] **[LOGIC TESTABLE NOW]** Cross-check current sign convention against LeafSpy live (already
      listed in Part A — repeated here since it's also a distinct "safety-adjacent" mapping
      confirmation, not just a data-plausibility check: a flipped sign here could make the discharge
      taper watch the wrong direction).
- [ ] **[LOGIC TESTABLE NOW]** Real SoC sweep (a full charge or discharge session), cross-checked
      against LeafSpy's own GIDS display, to confirm the derived GIDS/QC-capacity formula.
- Result / date / notes:

### B18. Log save location (added 2026-08-06)
- [x] **[SOFTWARE-VERIFIED]** `config_profile.LOGS_DIR` resolves to the real `logs/` folder and
      `_toggle_trc_log()`'s save dialog defaults there. Confirmed directly by inspecting the
      resolved path against the real folder - no real-hardware component to this item at all (a
      GUI file-dialog default, not a CAN-facing behavior).
- Result / date / notes: **RESOLVED 2026-08-06** - `LOGS_DIR` == the real `logs/` folder that
  already contains today's two test captures. Nothing further to verify.

### B19. 6th wind-down trigger - bus-silence timeout (`leaf_signals.BUS_SILENCE_TIMEOUT_S`, added 2026-08-06)
- [ ] **[LOGIC TESTABLE NOW, via bracketing]** Temporarily lower `BUS_SILENCE_TIMEOUT_S` (code
      constant, not currently GUI-exposed) to a few seconds, get the bridge into `running` with a
      real Leaf connected, then physically stop all Leaf-bus traffic (unplug the Leaf-side adapter,
      not the RZ450e side) without going through a normal ignition-off/charge-end sequence. Confirm
      the sequencer winds down after the shortened timeout even though none of the other 4 (or
      5th/staleness) triggers ever fired.
- [ ] **[NEEDS FAULT REPRODUCTION]** The original real-bench discrepancy this trigger was added to
      guard against (`docs/10` #16 - a real charge cycle that stayed awake ~110s with no explanation
      found in the capture) has not been reproduced or explained. If it recurs, capture a fresh
      `.trc` log with the Log panel's timestamps cross-referenced, and re-run `tests/
      check_shutdown_sequencer_replay.py` against it immediately (don't wait) to see whether the
      real Leaf-bus traffic during that specific window differs from what the replay assumes.
- Result / date / notes:

### B20. AC charger taper rework - min-kW floor, stop-charging cutoff, self-adjusting convergence rate (added 2026-08-06, twice)

**Root cause confirmed against real data, per `docs/13` item 17.6.** The 2026-08-05 bench log's
oscillation was initially checked against the CODE DEFAULT `ac_full_v`/`ac_min_v` (4.00V/4.15V),
which the log's voltage (3.616-3.640V) never reaches - but the user confirmed the actual test used
a deliberately bracketed 3.62V/3.64V window instead (this doc's own "bracket the threshold, not
the battery" technique), which the log's voltage sits squarely inside. Re-checked with the correct
values: the OLD zero-hysteresis formula reproduces the log's real observed `charger_limit_kw`
values to within encoding rounding at every sample checked - confirmed, not just plausible.
Retested with the NEW code too, replaying the real captured voltage trace through the current
algorithm (`tests/check_ac_taper_log_replay.py`, new) at the actual bracketed values - worst
single-call jump across the entire ~1780s real replay is 0.819kW, vs. the original real multi-kW
same-tick jumps. This is strong real-data software confirmation, but still a replay, not a live
bench session - the items below are the live real-hardware retest this still needs before
promoting to Confirmed.

- [x] **[LOGIC + PRECISION TESTABLE NOW, via bracketing]** Repeat the original test: bracket
      `ac_full_v`/`ac_min_v` to sit just below/above the pack's current resting voltage (as was
      actually done 2026-08-05, e.g. 3.62V/3.64V), start a real charge session, and confirm
      `charger_limit_kw` now moves SMOOTHLY through the taper with no hunting/oscillation like the
      original log showed, settling at (not below) the configured `ac_min_kw` floor instead of
      drifting toward zero. **CONFIRMED 2026-08-06** (`minileaf_20260806_182106-charge-test-with-
      low-set-points-on-v.trc`, `ac_full_v`=3.63V/`ac_min_v`=3.64V per the user's own real test) -
      user directly confirmed "the ramp that we implemented worked great," no hunting on the taper
      itself.
- [ ] **[LOGIC TESTABLE NOW]** Watch the AC taper's live status text during that convergence -
      confirm it visibly starts at level 7 and downshifts as `charger_limit_kw` approaches the
      target, getting progressively gentler (smaller steps) the closer it gets, not a fixed-rate
      ramp all the way in.
- [ ] **[LOGIC TESTABLE NOW]** Cross-check the same session's `.trc` capture: confirm the
      TRANSMITTED 0x1DC uprate bits actually track the taper's selected level while it's converging
      (not stuck at the manually-configured `chg_uprate_level` the whole time) - this is the real
      point of driving the transmitted field dynamically, not just an internal computation.
- [ ] **[LOGIC TESTABLE NOW - RETEST NEEDED, bug found and fixed 2026-08-06]** During that same
      session, bracket `ac_cutoff_v` down to just above the pack's current voltage. Confirm
      `full_charge_flag`/`charge_limit_kw=0`/`charger_limit_kw=-10` assert and the session ends
      cleanly (no RED dash error), distinct from the emergency hard-cut path (which should still
      require `ac_emergency_v`, not `ac_cutoff_v`, to fire). **BUG FOUND 2026-08-06** in the same
      session as the taper retest above (`ac_cutoff_v`=3.64V): the cutoff fired but did NOT
      actually stop the session - `full_charge_flag` pulsed on/off 29 times (~0.6s per pulse,
      confirmed by decoding the real `.trc`'s `0x1DB` frames) instead of latching, because the
      per-tick check re-evaluated the trigger fresh every tick with no memory - the instant power
      cut to 0, the triggering cell relaxed back under 3.64V, and the flag reset, letting charging
      resume. Fixed in main.py Rev 58 (`ManagementEngine._ac_charge_stop_latched` - latches the
      stop once triggered, cleared only via `notify_session_start()`/`notify_charge_replug()`, same
      mechanism as `_hard_latched`). Needs a fresh live retest to confirm the fix holds on real
      hardware (software-side regression test added: `tests/test_management_engine.py`
      `test_ac_stop_charging_cutoff_latches_once_triggered`).
- [ ] **[LOGIC TESTABLE NOW]** Confirm the min-kW floor does NOT force the ramp's own low startup
      value upward - watch `charger_limit_kw` at the very start of a fresh charge request (should
      still visibly ramp from ~0kW, not jump straight to `ac_min_kw`).
- [ ] **[NUMBER UNCONFIRMED]** Whether 4.15V (`ac_min_v`) / 4.18V (`ac_cutoff_v`) are actually the
      right real-world voltages for this pack, and whether the 7 level-selection thresholds
      (`_AC_LEVEL_DOWNSHIFT_KW` = 3.0/1.5/0.75/0.4/0.2/0.1/0.05kW) give a good feel in practice (too
      twitchy at the high end, too slow to converge at the low end?) - none of these are
      real-hardware-confirmed, only internally consistent. This is the retest most likely to need a
      follow-up tuning pass, not a one-shot pass/fail.
- [ ] **[LOGIC TESTABLE NOW - NEW, bug found and fixed 2026-08-07]** With `ac_full_v`/`ac_min_v`/
      `ac_cutoff_v` still bracketed near the pack's resting voltage (or at real defaults), just
      DRIVE (charger unplugged, `charge_permission_input` not active) with the worst cell sitting
      inside/above the AC taper's window. Confirm `charger_limit_kw` ("Max power for charger")
      stays completely flat at whatever Signal Mapping/idle produces - it must NOT taper down or
      hard-cut just from driving. **BUG FOUND 2026-08-07**: `ac_charge_taper`'s effect on
      `charger_limit_kw` (the taper reduction AND its own `ac_emergency_v` hard cut) previously ran
      every tick regardless of `charge_permission_input` - only the `ac_cutoff_v`/`full_charge_flag`
      portion was actually gated. With a bracketed-low `ac_full_v` left in place from charge
      testing, this meant `charger_limit_kw` could get reduced (or the AC emergency tier could
      latch a hard cut) while simply driving with nothing plugged in - confirmed against
      `Refrance/Leaf_BMS_Emulator`'s real-hardware notes that `LB_MAX_POWER_FOR_CHARGER` sits fixed
      at its 1023/92.3kW idle placeholder whenever not actually charging. Fixed in main.py Rev 59 -
      the whole `ac_charge_taper` block is now gated on `charge_permission_input`; driving-mode
      overvoltage protection is entirely `charge_target_taper`'s job instead (see B7). Software-side
      regression test added: `tests/test_management_engine.py`
      `test_ac_taper_leaves_charger_limit_kw_untouched_while_driving`. Needs a live retest to
      confirm on real hardware.
- Result / date / notes:

### B21. Charger ramp target - AC min/max kW clamp + symmetric rate-limited precision (added 2026-08-06)
- [ ] **[LOGIC TESTABLE NOW]** With a real charge session ramping toward a configured target,
      LOWER `charge_target_kw` live mid-ramp (e.g. from 5.0kW down to 0.6kW). Confirm
      `charger_limit_kw` steps DOWN gradually at the configured ramp rate, not an instant jump to
      the new target in a single tick - this is the exact bug the 2026-08-05 log exposed.
- [ ] **[LOGIC TESTABLE NOW]** Set `charge_target_kw` above `ac_max_kw` (6.6kW default) and confirm
      the ramp caps at `ac_max_kw`, not the higher configured target. Set it below `ac_min_kw`
      (0.5kW default) and confirm the ramp floors at `ac_min_kw` instead.
- Result / date / notes:

### B22. Charge Emulation tab input validation feedback (added 2026-08-06)
- [x] **[SOFTWARE-VERIFIED]** Scripted an empty-string entry against a live `ChargeEmulationPanel`
      field - flag correctly read "invalid", config value stayed at its last-good value. Scripted
      an out-of-range entry - flag correctly read "clamped", config value clamped to the field's
      bound. No real-hardware component to this item (pure GUI input validation).
- Result / date / notes: **RESOLVED 2026-08-06** - confirmed directly, not just by inspection. Feel
  free to also just try typing garbage into the real running app for a sanity check, but this one
  doesn't need real CAN hardware to be considered done.

---

## After each test session

- Restore any bracketed threshold to its real default (`tests/check_profile_drift.py` to confirm).
- Fold the result into `11-manual-verification-checklist.md` (Documented → Confirmed / Needs
  Validation).
- If a test reveals a wrong number or a real bug, update the threshold/logic and bump `main.py`'s
  `REVISION` changelog, same as any other change.
