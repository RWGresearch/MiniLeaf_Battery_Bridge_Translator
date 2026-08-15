"""Verification script for bridge/management_engine.py's safety features -
run directly (`py tests/test_management_engine.py`), not a pytest suite (no
test framework dependency yet, matching this project's own minimal-
dependency stance). Cites results back into docs/11-manual-verification-
checklist.md and docs/12-nmc-bms-design-research.md per this project's
"write verification scripts into the repo, not a scratchpad" convention.

Covers the 2026-07-31 docs/12 research-pass fixes: F1 (cold-charge block now
keys on temp_min), F3 (cold-side charge/regen derate curve), F4 (cell-
imbalance monitor), F5 (low-voltage soft-cut persistence window), F6
(separate emergency over-temp hard-cut tier), F2 (overcurrent monitor).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import leaf_signals
from bridge.management_engine import ManagementEngine
from bridge.state import SharedState

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


def fresh():
    rz = SharedState()
    return ManagementEngine(), rz


def set_all_cells(rz, voltage, exceptions=None):
    exceptions = exceptions or {}
    for i in range(1, 97):
        key = f'cell_{i:02d}'
        rz.update_input(key, exceptions.get(key, voltage))


def set_all_temps(rz, temp_c, exceptions=None):
    exceptions = exceptions or {}
    for i in range(1, 17):
        key = f'temp_{i:02d}'
        rz.update_input(key, exceptions.get(key, temp_c))


def set_all_temps_did(rz, temp_c, exceptions=None):
    exceptions = exceptions or {}
    for i in range(1, 17):
        key = f'temp_{i:02d}_did'
        rz.update_input(key, exceptions.get(key, temp_c))


def set_all_temps_can(rz, temp_c, exceptions=None):
    exceptions = exceptions or {}
    for i in range(1, 17):
        key = f'temp_{i:02d}_can'
        rz.update_input(key, exceptions.get(key, temp_c))


def base_inputs(rz, cell_v=3.70, temp_max=25.0, temp_min=25.0, current=0.0, soc=60.0):
    set_all_cells(rz, cell_v)
    rz.update_input('temp_max', temp_max)
    rz.update_input('temp_min', temp_min)
    rz.update_input('current', current)
    rz.update_input('soc_pct', soc)
    rz.update_input('charge_permission_input', 0.0)


# ── F1: cold-charge block must key on temp_min, not temp_max ───────────────
def test_f1_cold_block_uses_coldest_probe():
    eng, rz = fresh()
    base_inputs(rz, temp_max=21.1, temp_min=-6.7)  # hottest probe warm, coldest probe well below freezing
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('F1: charge blocked when COLDEST probe is below freezing (even though hottest probe is warm)',
          out['charge_limit_kw'] == 0.0,
          f"charge_limit_kw={out['charge_limit_kw']} (expected 0.0)")

    eng2, rz2 = fresh()
    base_inputs(rz2, temp_max=-6.7, temp_min=21.1)  # hottest probe frozen-cold, coldest probe warm (contrived, sanity check the other direction)
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    # Tightened (docs/13 item 6.4, 2026-08-03): was a loose `> 0.0` check - the
    # exact value is computable (cold_factor=1.0 since coldest probe 21.1C is
    # above charge_derate_low_start_c=10C; hot_factor=1.0 since hottest probe
    # -6.7C is nowhere near charge_derate_start_c=32C; c_factor=min(1.0,1.0)=1.0;
    # cell_v=3.70V default doesn't trigger the regen taper either) - full
    # power, unreduced by any feature.
    check('F1: charge NOT blocked on cold-side when coldest probe is warm, regardless of hottest probe reading',
          out2['charge_limit_kw'] == leaf_signals.DEFAULTS['charge_limit_kw'],
          f"charge_limit_kw={out2['charge_limit_kw']} (expected {leaf_signals.DEFAULTS['charge_limit_kw']}, full power)")


# ── F3: cold-side charge/regen derate ramps between block and derate-start ─
def test_f3_cold_derate_ramp():
    eng, rz = fresh()
    base_inputs(rz, temp_min=5.0)  # midpoint of 0C block / 10C full-power default window
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    factor = out['charge_limit_kw'] / leaf_signals.DEFAULTS['charge_limit_kw']
    # Tightened (docs/13 item 6.4, 2026-08-03): was a loose 0.35-0.65 range -
    # the linear ramp formula gives an exact expected factor of 0.5 at this
    # exact midpoint ((5-0)/(10-0) = 5/10 = 0.5), and nothing else in this
    # scenario reduces charge_limit_kw further (default cell_v=3.70V doesn't
    # trigger the regen taper; hottest probe stays at the base_inputs default
    # 25C, well below any hot-side derate).
    check('F3: cold-side derate ramps to EXACTLY half power at the midpoint temp (5C, between 0C/10C)',
          abs(factor - 0.5) < 1e-9, f'factor={factor!r} (expected exactly 0.5)')

    eng2, rz2 = fresh()
    base_inputs(rz2, temp_min=15.6)  # above the cold-derate window entirely
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('F3: full charge/regen power once above the cold-derate window (15.6C)',
          out2['charge_limit_kw'] == leaf_signals.DEFAULTS['charge_limit_kw'],
          f"charge_limit_kw={out2['charge_limit_kw']}")


# ── F4: cell-imbalance monitor warns but never cuts/derates ────────────────
def test_f4_cell_imbalance_monitor():
    eng, rz = fresh()
    exceptions = {'cell_01': 3.55}  # 100mV below the rest -> above the 50mV default warn threshold
    set_all_cells(rz, 3.65, exceptions)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    status = eng.status.get('cell_imbalance_monitor', '')
    check('F4: 100mV spread trips the warning text', 'WARNING' in status, status)
    check('F4: monitor never asserts capacity_empty or relay_cut_request',
          out.get('capacity_empty', 0) == 0 and out.get('relay_cut_request', 0) == 0,
          f"capacity_empty={out.get('capacity_empty')}, relay_cut_request={out.get('relay_cut_request')}")
    # Never checked before (docs/14 Part 1 TODO) - the status text was tested, the actual
    # fault_log entry never was.
    check('F4: cell_imbalance_warn fault_log entry is active',
          eng.fault_log.entries['cell_imbalance_warn']['active'] is True)

    eng2, rz2 = fresh()
    set_all_cells(rz2, 3.65)  # all cells identical -> 0mV spread
    rz2.update_input('temp_max', 25.0)
    rz2.update_input('temp_min', 25.0)
    rz2.update_input('current', 0.0)
    eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('F4: balanced pack reports ok, not a warning', 'ok' in eng2.status.get('cell_imbalance_monitor', ''))


# ── F5: low-voltage soft cut requires persistence, emergency stays instant ─
def test_f5_soft_cut_persistence():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.90)  # below 3.00V soft-cut floor, above 2.80V emergency floor
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('F5: soft cut does NOT latch on the very first tick (persistence guard)',
          out.get('capacity_empty', 0) == 0, f"capacity_empty={out.get('capacity_empty')}")

    time.sleep(2.1)  # exceed the default 2.0s persistence window
    out2 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('F5: soft cut DOES latch once the condition has persisted past the window',
          out2.get('capacity_empty', 0) == 1, f"capacity_empty={out2.get('capacity_empty')}")

    eng2, rz2 = fresh()
    base_inputs(rz2, cell_v=2.50)  # below the 2.60V emergency floor (2026-08-01 user edit, was 2.80V)
    out3 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('F5: emergency low-voltage hard cut fires INSTANTLY, no persistence delay',
          out3.get('relay_cut_request', 0) == 3, f"relay_cut_request={out3.get('relay_cut_request')}")


# ── F6: emergency over-temp is a separate, more extreme tier ──────────────
def test_f6_emergency_temp_tier():
    eng, rz = fresh()
    base_inputs(rz, temp_max=60.0, temp_min=21.1)  # exactly at the old hard_stop_c, now soft-only
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('F6: at the soft discharge_hard_stop_c (60C), discharge power reaches zero but NO hard cut fires',
          out['discharge_limit_kw'] == 0.0 and out.get('relay_cut_request', 0) == 0,
          f"discharge_limit_kw={out['discharge_limit_kw']}, relay_cut_request={out.get('relay_cut_request')}")

    eng2, rz2 = fresh()
    # 61.1C - just above the 61C emergency tier (2026-08-01 user edit, was
    # 65C) - close to the real boundary, not just "clearly above"
    base_inputs(rz2, temp_max=61.1, temp_min=21.1)
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('F6: above the emergency_temp_c (61C), a genuine hard cut fires',
          out2.get('relay_cut_request', 0) == 3, f"relay_cut_request={out2.get('relay_cut_request')}")


# ── F2: overcurrent monitor warns after sustained current, never cuts ─────
def test_f2_overcurrent_monitor():
    eng, rz = fresh()
    base_inputs(rz, current=160.0)  # above the 150A discharge-warn default
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('F2: elevated current does not warn on the very first tick (persistence guard)',
          'elevated' in eng.status.get('overcurrent_monitor', ''), eng.status.get('overcurrent_monitor'))

    time.sleep(5.1)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('F2: sustained elevated current DOES warn once past the persistence window',
          'WARNING' in eng.status.get('overcurrent_monitor', ''), eng.status.get('overcurrent_monitor'))
    check('F2: overcurrent monitor never cuts or derates power',
          out['discharge_limit_kw'] == leaf_signals.DEFAULTS['discharge_limit_kw'],
          f"discharge_limit_kw={out['discharge_limit_kw']}")
    # Never checked before (docs/14 Part 1 TODO) - the status text was tested, the actual
    # fault_log entry never was.
    check('F2: overcurrent_discharge_warn fault_log entry is active',
          eng.fault_log.entries['overcurrent_discharge_warn']['active'] is True)
    check('F2: overcurrent_charge_warn fault_log entry stays inactive (this is a discharge, not a charge)',
          eng.fault_log.entries['overcurrent_charge_warn']['active'] is False)

    eng2, rz2 = fresh()
    base_inputs(rz2, current=210.0)  # above the 0x023 sensor's 204.7A saturation ceiling
    eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('F2: saturation note appears once current is at/near the sensor ceiling',
          'saturation' in eng2.status.get('overcurrent_monitor', ''), eng2.status.get('overcurrent_monitor'))

    # Charge/regen direction (negative current) - never exercised at all before.
    eng3, rz3 = fresh()
    base_inputs(rz3, current=-35.0)  # above the 30A charge/regen-warn default (magnitude)
    eng3.apply(dict(leaf_signals.DEFAULTS), rz3)
    time.sleep(5.1)
    eng3.apply(dict(leaf_signals.DEFAULTS), rz3)
    check('F2: overcurrent_charge_warn fault_log entry is active for sustained elevated charge/regen current',
          eng3.fault_log.entries['overcurrent_charge_warn']['active'] is True)
    check('F2: overcurrent_discharge_warn stays inactive during a charge/regen event',
          eng3.fault_log.entries['overcurrent_discharge_warn']['active'] is False)


# ── Fault History integration: every trigger point actually feeds the log ─
def test_fault_log_records_low_voltage_soft_and_emergency():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.90)  # below soft floor, above emergency floor
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    time.sleep(2.1)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)  # persistence window elapses -> soft cut latches
    check('a latched low-voltage soft cut is recorded in the fault log',
          eng.fault_log.entries['low_voltage_soft']['count'] == 1,
          eng.fault_log.entries.get('low_voltage_soft'))
    check('the emergency tier is NOT recorded (never crossed that threshold)',
          eng.fault_log.entries['low_voltage_emergency']['count'] == 0)

    # cell recovers -> soft cut auto-clears, but the fault log remembers it happened
    rz2 = SharedState()
    base_inputs(rz2, cell_v=3.70)
    eng.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('after recovery, the entry shows cleared (not active) but keeps its count',
          eng.fault_log.entries['low_voltage_soft']['active'] is False
          and eng.fault_log.entries['low_voltage_soft']['count'] == 1,
          eng.fault_log.entries.get('low_voltage_soft'))


def test_fault_log_records_over_temp_emergency():
    eng, rz = fresh()
    base_inputs(rz, temp_max=61.1, temp_min=21.1)  # above the 61C emergency tier
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('an over-temperature emergency hard cut is recorded in the fault log',
          eng.fault_log.entries['over_temp_emergency']['count'] == 1,
          eng.fault_log.entries.get('over_temp_emergency'))


def test_fault_log_manual_reset_does_not_change_live_cut_decision():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.50)  # below the 2.60V emergency floor - hard cut fires every tick, instantaneous
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('hard cut is asserted while the condition is genuinely true',
          out.get('relay_cut_request', 0) == 3)
    eng.fault_log.manual_reset('low_voltage_emergency')
    check('manual reset zeroes the fault log entry',
          eng.fault_log.entries['low_voltage_emergency']['count'] == 0)
    out2 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('the manual reset does NOT clear the actual hard cut - it is still driven by live '
          'sensor data, unchanged by acknowledging the log entry',
          out2.get('relay_cut_request', 0) == 3, f"relay_cut_request={out2.get('relay_cut_request')}")
    check('the fault log immediately re-counts the still-active condition on the next tick',
          eng.fault_log.entries['low_voltage_emergency']['count'] == 1)


# ── Added 2026-08-01: AC "charge target reached" contactor-drop path had
# zero test coverage - a genuine hard safety action (full_charge_flag,
# instant contactor drop per docs/03) force-set purely from crossing the
# daily/extended AC SoC target WHILE actually plugged in and charging. ────
def test_ac_charge_target_reached_sets_full_charge_flag():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70, soc=85.0)  # above the 80% daily target
    rz.update_input('charge_permission_input', 1.0)  # actually plugged in and charging
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('AC charge target reached (85% >= 80% daily target) while actually charging sets full_charge_flag',
          out.get('full_charge_flag', 0) == 1, f"full_charge_flag={out.get('full_charge_flag')}")
    check('charge_limit_kw is forced to 0.0 when the AC target is reached',
          out.get('charge_limit_kw') == 0.0, f"charge_limit_kw={out.get('charge_limit_kw')}")
    check('charger_limit_kw is forced to -10.0 (raw idle-stop value) when the AC target is reached',
          out.get('charger_limit_kw') == -10.0, f"charger_limit_kw={out.get('charger_limit_kw')}")

    # Same SoC but NOT actually charging must NOT set full_charge_flag - this
    # exact scenario was a real safety bug, fixed 2026-07-31 (main.py rev 7).
    eng2, rz2 = fresh()
    base_inputs(rz2, cell_v=3.70, soc=85.0)  # charge_permission_input defaults to 0 via base_inputs
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('the same 85% SoC while NOT actually charging must NOT set full_charge_flag (e.g. driving)',
          out2.get('full_charge_flag', 0) == 0, f"full_charge_flag={out2.get('full_charge_flag')}")


# ── AC charger taper rework (added 2026-08-06, real-bench-test-driven -
# see main.py's Rev 41/42 changelog): min-kW floor instead of true zero, a
# new explicit stop-charging cutoff voltage, and (after a same-day rework,
# see the dynamic-level tests further below) a gentle, self-adjusting
# convergence rate instead of an instant snap. ─────────────────────────────
def test_ac_taper_converges_to_min_kw_floor_instead_of_zero():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70)
    rz.update_input('charge_permission_input', 1.0)   # actually plugged in and charging (2026-08-07: AC taper is charging-only)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 80.0   # simulate the charge-ramp having raised it
    eng.apply(leaf_state, rz)   # establish full-power baseline tracking

    base_inputs(rz, cell_v=4.15)   # exactly at ac_min_v - taper factor bottoms out
    rz.update_input('charge_permission_input', 1.0)
    eng._last_apply_time = time.monotonic() - 1000.0   # force full convergence in one tick
    out = eng.apply(dict(leaf_state), rz)
    check('AC taper converges to the configured ac_min_kw floor (0.5kW default), not true zero',
          out['charger_limit_kw'] == 0.5, f"charger_limit_kw={out['charger_limit_kw']}")


def test_ac_taper_does_not_force_ramp_value_up_to_floor():
    eng, rz = fresh()
    base_inputs(rz, cell_v=4.15)   # taper factor bottoms out (same as above)
    rz.update_input('charge_permission_input', 1.0)   # actually plugged in and charging (2026-08-07: AC taper is charging-only)
    leaf_state = dict(leaf_signals.DEFAULTS)
    # Simulate the charge-ramp still climbing from 0kW, below ac_min_kw (0.5)
    # - the taper must not fight the ramp's own low startup value by forcing
    # it UP to the floor.
    leaf_state['charger_limit_kw'] = 0.2
    out = eng.apply(leaf_state, rz)
    check('AC taper leaves a ramp value already below ac_min_kw untouched (does not force it up)',
          out['charger_limit_kw'] == 0.2, f"charger_limit_kw={out['charger_limit_kw']}")


def test_ac_taper_leaves_charger_limit_kw_untouched_while_driving():
    # Regression test for the 2026-08-07 fix (user directive: "there are 2
    # different ways of controlling the output. one under driving and one
    # under charging... that's a different control means all together") -
    # previously this whole block (including the ac_emergency_v hard cut)
    # ran every tick regardless of charging_active, so a per-cell voltage
    # inside/above the AC taper's window while simply driving (nothing
    # plugged in) would still reduce or even hard-cut "Max power for
    # charger" - contradicting the reference project's confirmed real-
    # hardware idle-placeholder behavior. Driving-mode overvoltage
    # protection is charge_target_taper's job (the regen taper), not this
    # one's.
    eng, rz = fresh()
    base_inputs(rz, cell_v=4.19)   # inside the AC taper window AND above ac_cutoff_v - NOT charging (base_inputs defaults charge_permission_input to 0)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 92.3   # idle placeholder, as mapping/ramp would leave it while driving
    out = eng.apply(leaf_state, rz)
    check('charger_limit_kw is left completely untouched by the AC taper while not actually charging',
          out['charger_limit_kw'] == 92.3, f"charger_limit_kw={out['charger_limit_kw']}")
    check('full_charge_flag is NOT set even though the voltage is above ac_cutoff_v - only matters while charging',
          out.get('full_charge_flag', 0) == 0, f"full_charge_flag={out.get('full_charge_flag')}")
    check('the AC-charger stop-charging cutoff fault_log entry correctly stays inactive while driving',
          eng.fault_log.entries['ac_cutoff_stop']['active'] is False,
          eng.fault_log.entries.get('ac_cutoff_stop'))

    # Now push past ac_emergency_v (4.20V) too - even a genuine overvoltage
    # extreme must not touch charger_limit_kw or latch relay_cut_request via
    # THIS feature while driving (charge_target_taper's own emergency_high_v,
    # tested elsewhere, is what actually protects the pack while driving).
    eng2, rz2 = fresh()
    base_inputs(rz2, cell_v=4.35)   # above ac_emergency_v (4.20V default), normal temp so only voltage is under test
    leaf_state2 = dict(leaf_signals.DEFAULTS)
    leaf_state2['charger_limit_kw'] = 92.3
    out2 = eng2.apply(leaf_state2, rz2)
    check('AC-charger emergency tier (ac_emergency_v) does not fire while driving - charger_limit_kw untouched',
          out2['charger_limit_kw'] == 92.3, f"charger_limit_kw={out2['charger_limit_kw']}")
    check('ac_overvoltage_emergency fault_log entry stays inactive while driving (charging-only tier)',
          eng2.fault_log.entries['ac_overvoltage_emergency']['active'] is False,
          eng2.fault_log.entries.get('ac_overvoltage_emergency'))


def test_ac_stop_charging_cutoff_sets_full_charge_flag_while_charging():
    eng, rz = fresh()
    base_inputs(rz, cell_v=4.19)   # above ac_cutoff_v (4.18V default), below ac_emergency_v (4.20V)
    rz.update_input('charge_permission_input', 1.0)   # actually plugged in and charging
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 80.0
    out = eng.apply(leaf_state, rz)
    check('stop-charging cutoff sets full_charge_flag once per-cell voltage crosses ac_cutoff_v',
          out.get('full_charge_flag', 0) == 1, f"full_charge_flag={out.get('full_charge_flag')}")
    check('charge_limit_kw forced to 0.0 by the cutoff', out.get('charge_limit_kw') == 0.0)
    check('charger_limit_kw forced to -10.0 (raw idle-stop value) by the cutoff',
          out.get('charger_limit_kw') == -10.0)
    check('this is the deliberate cutoff, not the separate emergency hard cut (still below 4.20V)',
          out.get('relay_cut_request', 0) == 0)


def test_ac_stop_charging_cutoff_gated_on_charging_active():
    eng, rz = fresh()
    base_inputs(rz, cell_v=4.19)   # same voltage as above, but NOT actually charging
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('the stop-charging cutoff must NOT fire full_charge_flag while simply driving '
          '(same safety gating as the SoC-target-reached stop)',
          out.get('full_charge_flag', 0) == 0, f"full_charge_flag={out.get('full_charge_flag')}")


# Regression test for the real 2026-08-06 bench log
# (minileaf_20260806_182106-charge-test-with-low-set-points-on-v.trc, taken
# with ac_cutoff_v bracketed to 3.64V): the cutoff fired for about one tick,
# charger_limit_kw dropped to 0, the worst cell relaxed back under 3.64V
# within a tick or two, and full_charge_flag fell straight back to 0 -
# repeating in a hunt roughly every 10-20s for the rest of the session
# instead of ever actually stopping it (confirmed by decoding every real
# 0x1DB frame's full_charge_flag bit from the .trc: 29 separate on/off
# pulses, each ~0.6s long). Fixed by latching the stop once triggered - see
# ManagementEngine._ac_charge_stop_latched.
def test_ac_stop_charging_cutoff_latches_once_triggered():
    eng, rz = fresh()
    rz.update_input('charge_permission_input', 1.0)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 80.0

    base_inputs(rz, cell_v=4.19)   # above ac_cutoff_v (4.18V default) - cutoff fires this tick
    rz.update_input('charge_permission_input', 1.0)   # base_inputs() resets this to 0.0
    out = eng.apply(leaf_state, rz)
    check('cutoff fires and sets full_charge_flag on the triggering tick',
          out.get('full_charge_flag', 0) == 1, f"full_charge_flag={out.get('full_charge_flag')}")

    # Simulate the real-world sequence that produced the hunt: charger power
    # cut to 0 collapses the sag, the worst cell relaxes back BELOW
    # ac_cutoff_v on the very next tick, but charging is still nominally
    # active (the real Leaf VCM hasn't reacted yet).
    base_inputs(rz, cell_v=4.10)
    rz.update_input('charge_permission_input', 1.0)
    out2 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('full_charge_flag stays latched once the triggering voltage recovers '
          '(does not un-trigger and resume charging)',
          out2.get('full_charge_flag', 0) == 1, f"full_charge_flag={out2.get('full_charge_flag')}")
    check('charge_limit_kw stays forced to 0.0 while latched',
          out2.get('charge_limit_kw') == 0.0)
    check('charger_limit_kw stays forced to -10.0 while latched',
          out2.get('charger_limit_kw') == -10.0)

    # Latch must survive many more ticks at a fully-recovered voltage.
    for _ in range(20):
        base_inputs(rz, cell_v=3.70)
        rz.update_input('charge_permission_input', 1.0)
        out3 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('full_charge_flag remains latched across many subsequent ticks at a safe voltage',
          out3.get('full_charge_flag', 0) == 1, f"full_charge_flag={out3.get('full_charge_flag')}")

    # Only a genuine replug clears it.
    eng.notify_charge_replug()
    base_inputs(rz, cell_v=3.70)
    rz.update_input('charge_permission_input', 1.0)
    out4 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('notify_charge_replug() clears the latch, allowing charging to resume',
          out4.get('full_charge_flag', 0) == 0, f"full_charge_flag={out4.get('full_charge_flag')}")


# ── AC taper convergence rate: dynamically-selected 0-7 uprate levels,
# replacing the fixed-time-constant hysteresis above (added 2026-08-06,
# reworked the SAME day - see main.py's Rev 42 changelog and docs/13 items
# 17.3's correction / 17.5). User-diagnosed root cause: an instant downward
# step (even with a slow release afterward) is the wrong model for a CC-CV
# charging control loop - it lets voltage sag more than necessary, which the
# taper then reads as safe and overshoots recovering, hunting. Fix: reuse
# the existing 0-7 chg_uprate_level rate table as a DYNAMICALLY-SELECTED
# rate (always starts at level 7, downshifts/upshifts with hysteresis on
# the level switch itself as remaining distance narrows/grows), and that
# same selected level is what's transmitted in 0x1DC's own uprate bits
# while genuinely converging (`eng.ac_uprate_level`), not just an internal
# computation - see bridge/management_engine.py's _select_ac_uprate_level(). ──
def test_ac_charge_taper_starts_at_level_7_and_does_not_snap_instantly():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70)   # full power, well below ac_full_v (4.00V)
    rz.update_input('charge_permission_input', 1.0)   # actually plugged in and charging (2026-08-07: AC taper is charging-only)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 80.0
    eng.apply(leaf_state, rz)   # establishes _ac_applied_kw=80.0 (full-power pass-through tracking)
    check('ac_uprate_level is None while at full power (nothing to converge to)',
          eng.ac_uprate_level is None)

    base_inputs(rz, cell_v=4.16)   # enters the taper window, above ac_min_v but below ac_cutoff_v (4.18) - target collapses toward the 0.5kW floor without also tripping the stop-charging cutoff
    rz.update_input('charge_permission_input', 1.0)
    out = eng.apply(dict(leaf_state), rz)
    check('convergence starts at level 7 (fastest) the moment the taper window is entered '
          '("always start at #7", per user directive)',
          eng.ac_uprate_level == 7, f"ac_uprate_level={eng.ac_uprate_level}")
    check('does NOT snap instantly to the floor - the whole point of this fix is a gradual, '
          'rate-limited step even on first entry, not an instant multi-kW jump',
          out['charger_limit_kw'] > 70.0, f"charger_limit_kw={out['charger_limit_kw']}")


def test_ac_charge_taper_converges_without_overshoot_even_with_a_huge_dt():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70)
    rz.update_input('charge_permission_input', 1.0)   # actually plugged in and charging (2026-08-07: AC taper is charging-only)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 5.0
    eng.apply(leaf_state, rz)   # establishes _ac_applied_kw=5.0

    base_inputs(rz, cell_v=4.16)   # target collapses to the 0.5kW floor, below ac_cutoff_v (4.18)
    rz.update_input('charge_permission_input', 1.0)
    eng._last_apply_time = time.monotonic() - 1000.0   # absurdly large dt - would overshoot without clamping
    out = eng.apply(dict(leaf_state), rz)
    check('a huge dt does not overshoot past the target - the step is clamped to `remaining` '
          'itself, landing exactly on target instead of asymptotically approaching forever',
          out['charger_limit_kw'] == 0.5, f"charger_limit_kw={out['charger_limit_kw']}")

    # The level REPORTED on the tick that closes a large gap correctly
    # reflects the level that accomplished it (7, since level 7's rate
    # covered the whole distance in one oversized step) - level only reads
    # 0 on a SUBSEQUENT tick once `remaining` is genuinely ~0 at the start
    # of that tick.
    out2 = eng.apply(dict(leaf_state), rz)
    check('once fully converged, a follow-up tick (remaining ~0 at its own start) settles at '
          'level 0 (near-hold, nothing left to close)',
          eng.ac_uprate_level == 0, f"ac_uprate_level={eng.ac_uprate_level}")
    check('stays exactly at the floor on that follow-up tick, no drift',
          out2['charger_limit_kw'] == 0.5, f"charger_limit_kw={out2['charger_limit_kw']}")


def test_ac_charge_taper_downshifts_progressively_while_converging():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70)
    rz.update_input('charge_permission_input', 1.0)   # actually plugged in and charging (2026-08-07: AC taper is charging-only)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 5.0
    eng.apply(leaf_state, rz)

    base_inputs(rz, cell_v=4.16)   # below ac_cutoff_v (4.18)
    rz.update_input('charge_permission_input', 1.0)
    levels_seen = []
    for _ in range(400):   # 400 * 0.05s = 20s of simulated convergence time - the level table's
        eng._last_apply_time = time.monotonic() - 0.05   # lower rates need real simulated time to
        out = eng.apply(dict(leaf_state), rz)             # actually close a multi-kW gap, not just a
        levels_seen.append(eng.ac_uprate_level)            # few ticks
    check('level progressively downshifts (gentler) as the remaining distance narrows, not stuck '
          'at level 7 the whole way in',
          levels_seen[-1] < levels_seen[0], f"levels_seen[0]={levels_seen[0]}, levels_seen[-1]={levels_seen[-1]}")
    check('converges exactly to the ac_min_kw floor with no overshoot below it',
          out['charger_limit_kw'] == 0.5, f"charger_limit_kw={out['charger_limit_kw']}")


def test_ac_charge_taper_level_hysteresis_does_not_flap_at_a_boundary():
    # Direct test of the level-selection helper itself (also exercised
    # indirectly above) - oscillating right at the level1/level2 threshold
    # boundary (0.1kW) must not flap the SELECTED level back and forth.
    from bridge.management_engine import _select_ac_uprate_level
    level = 2
    seen = []
    for remaining in [0.09, 0.11, 0.09, 0.11, 0.09]:
        level = _select_ac_uprate_level(remaining, level)
        seen.append(level)
    check('level stays locked at 1 through oscillation right at a boundary, not flapping to 2 and back',
          seen == [1, 1, 1, 1, 1], f"seen={seen}")
    level = _select_ac_uprate_level(0.16, level)
    check('upshifts once remaining genuinely grows past the hysteresis margin (1.5x the next threshold)',
          level == 2, f"level={level}")


def test_ac_uprate_level_none_when_disabled_or_emergency():
    eng, rz = fresh()
    base_inputs(rz, cell_v=4.35)   # above ac_emergency_v (4.20V default)
    rz.update_input('charge_permission_input', 1.0)   # actually plugged in and charging (2026-08-07: AC taper is charging-only)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('ac_uprate_level is None during an emergency hard cut (not a rate-controlled convergence state)',
          eng.ac_uprate_level is None)

    eng2, rz2 = fresh()
    rz2.charge_emulation['ac_taper_enabled'] = False
    base_inputs(rz2, cell_v=4.18)
    eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('ac_uprate_level is None when the AC taper is disabled',
          eng2.ac_uprate_level is None)


def test_ac_charge_taper_does_not_force_low_ramp_value_up_to_floor():
    # Same principle as the min-kW-floor test above, now also checked
    # against the dynamic-level mechanism: a fresh ramp value below
    # ac_min_kw (still climbing from 0 at charge-request start) must be
    # left untouched, not forced up - and must report ac_uprate_level=None
    # since there's nothing to converge (this IS the pass-through case).
    eng, rz = fresh()
    base_inputs(rz, cell_v=4.16)   # inside the taper window, below ac_cutoff_v (4.18)
    rz.update_input('charge_permission_input', 1.0)   # actually plugged in and charging (2026-08-07: AC taper is charging-only)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 0.2   # below ac_min_kw (0.5) - ramp still climbing from 0
    out = eng.apply(leaf_state, rz)
    check('a ramp value already below ac_min_kw is passed through untouched',
          out['charger_limit_kw'] == 0.2, f"charger_limit_kw={out['charger_limit_kw']}")
    check('ac_uprate_level is None in this pass-through case (nothing to converge yet)',
          eng.ac_uprate_level is None)


# ── Synthetic regression scenario constructed to exercise the new mechanism
# directly (added 2026-08-06) - NOT a replay of the 2026-08-05 log's own
# byte sequence. That log's cell voltage never actually reached ac_full_v
# (stayed 3.616-3.640V the whole session, confirmed by decoding every 0x020
# frame in the capture) so the taper was never engaged there at all - see
# docs/13 item 17.3's correction note for the full trace. This test instead
# puts voltage genuinely INSIDE the taper's window (unlike that log) at the
# same rapid tick cadence real hardware uses, confirming the new algorithm
# is robust to noisy/oscillating voltage where the taper actually IS active. ──
def test_ac_charge_taper_handles_noisy_voltage_inside_its_window_without_jumping():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70)
    rz.update_input('charge_permission_input', 1.0)   # actually plugged in and charging (2026-08-07: AC taper is charging-only)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 6.6
    eng.apply(leaf_state, rz)

    # Oscillate cell voltage right at the ac_full_v/ac_min_v midpoint (4.075V)
    # with small noise, at a ~10ms tick cadence matching the real TX loop.
    import random
    random.seed(42)
    worst_jump = 0.0
    prev_kw = eng._ac_applied_kw
    for _ in range(300):   # 300 * 10ms = 3.0s
        noisy_v = 4.075 + random.uniform(-0.01, 0.01)
        base_inputs(rz, cell_v=noisy_v)
        rz.update_input('charge_permission_input', 1.0)   # base_inputs() resets this to 0.0 every call
        eng._last_apply_time = time.monotonic() - 0.01
        out = eng.apply(dict(leaf_state), rz)
        worst_jump = max(worst_jump, abs(out['charger_limit_kw'] - prev_kw))
        prev_kw = out['charger_limit_kw']
    # At level 7 (2.0kW/s) over a 10ms tick, the theoretical max single-tick
    # step is 0.02kW - allow generous headroom for level/target recompute
    # interaction, but this must stay orders of magnitude below the kind of
    # multi-kW same-tick jump the old zero-hysteresis design could produce.
    check('no single-tick jump exceeds a small bound even under continuously noisy voltage '
          'inside the taper\'s active window',
          worst_jump < 0.2, f"worst single-tick jump={worst_jump:.3f}kW")


def test_ac_charge_emulation_sanity_checks_new_fields():
    eng, rz = fresh()
    rz.charge_emulation['ac_min_v'] = 4.19   # inverted vs. ac_cutoff_v (4.18 default)
    base_inputs(rz, cell_v=3.70)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('config-sanity check flags an inverted ac_min_v/ac_cutoff_v ordering',
          'ac_min_v' in eng.status.get('config_sanity', '') or 'ac_cutoff_v' in eng.status.get('config_sanity', ''),
          eng.status.get('config_sanity'))


# ── Added 2026-08-01: the overvoltage emergency hard-cut tiers (both the
# regen-side charge_target_taper and the AC-side ac_charge_taper, split
# 2026-08-01) previously had thin/indirect fault_log coverage - only
# relay_cut_request was checked, never the fault_log entries themselves. ──
def test_overvoltage_emergency_fault_log_entries():
    eng, rz = fresh()
    base_inputs(rz, cell_v=4.35)  # above both the regen and AC emergency thresholds (4.20V default)
    rz.update_input('charge_permission_input', 1.0)   # AC-charger emergency tier is charging-only (2026-08-07) - regen's own tier (checked below) stays unconditional
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('overvoltage emergency hard cut fires', out.get('relay_cut_request', 0) == 3,
          f"relay_cut_request={out.get('relay_cut_request')}")
    check('regen overvoltage_emergency fault_log entry is active',
          eng.fault_log.entries['overvoltage_emergency']['active'] is True,
          eng.fault_log.entries.get('overvoltage_emergency'))
    check('AC-charger ac_overvoltage_emergency fault_log entry is active',
          eng.fault_log.entries['ac_overvoltage_emergency']['active'] is True,
          eng.fault_log.entries.get('ac_overvoltage_emergency'))


# ── Hard-cut latching (docs/12 finding F8, added 2026-08-01) ──────────────
def test_hard_cut_latches_and_survives_recovery():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.50)  # below the 2.60V emergency floor
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('hard cut fires while the condition is genuinely true', out.get('relay_cut_request', 0) == 3)

    rz2 = SharedState()
    base_inputs(rz2, cell_v=3.70)  # condition fully recovers
    out2 = eng.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('the hard cut STAYS asserted after the triggering condition recovers (latched, '
          'not auto-cleared) - docs/12 finding F8',
          out2.get('relay_cut_request', 0) == 3, f"relay_cut_request={out2.get('relay_cut_request')}")
    check('the hard_cut_latch fault_log entry is active while latched',
          eng.fault_log.entries['hard_cut_latch']['active'] is True)


def test_hard_cut_latch_clears_on_notify_session_start():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.50)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('sanity: latched', eng._hard_latched is True)

    eng.notify_session_start()
    rz2 = SharedState()
    base_inputs(rz2, cell_v=3.70)  # condition also recovered
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('notify_session_start() clears the latch once the underlying condition has also recovered',
          out.get('relay_cut_request', 0) == 0, f"relay_cut_request={out.get('relay_cut_request')}")


def test_hard_cut_latch_clears_on_notify_charge_replug():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.50)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('sanity: latched', eng._hard_latched is True)

    eng.notify_charge_replug()
    rz2 = SharedState()
    base_inputs(rz2, cell_v=3.70)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('notify_charge_replug() also clears the latch',
          out.get('relay_cut_request', 0) == 0, f"relay_cut_request={out.get('relay_cut_request')}")


def test_hard_cut_re_latches_immediately_if_condition_is_still_bad():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.50)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)

    eng.notify_session_start()
    rz2 = SharedState()
    base_inputs(rz2, cell_v=2.50)  # condition is STILL genuinely bad
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('a session start does NOT bypass a still-active condition - it re-latches immediately, '
          'same as a fault_log manual reset against a still-true condition',
          out.get('relay_cut_request', 0) == 3, f"relay_cut_request={out.get('relay_cut_request')}")


# ── docs/13 item 13.1a: "never seen" data must age from the bridge's first
# apply() call, not sit outside the watchdog forever ────────────────────────
def test_staleness_watchdog_catches_data_that_never_arrived_at_all():
    eng, rz = fresh()
    eng.config['staleness_watchdog']['soft_cut_s'] = 0.05
    eng.config['staleness_watchdog']['hard_escalation_s'] = 0.05
    # rz has ZERO inputs ever set - not "went stale," genuinely never arrived.
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('first tick: not yet stale long enough to soft-cut',
          out.get('capacity_empty', 0) == 0)
    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('data that has NEVER arrived hits the soft cut on the same schedule as data that went '
          'stale, instead of being excluded from the watchdog forever',
          out.get('capacity_empty', 0) == 1, f"capacity_empty={out.get('capacity_empty')}")
    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('...and escalates to a hard cut after the escalation window too',
          out.get('relay_cut_request', 0) == 3, f"relay_cut_request={out.get('relay_cut_request')}")


def test_staleness_watchdog_clock_starts_at_first_apply_not_object_creation():
    eng, rz = fresh()
    time.sleep(0.1)   # simulate time passing before the bridge actually starts ticking
    eng.config['staleness_watchdog']['soft_cut_s'] = 0.05
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('the "never seen" clock starts at the first apply() call, not at ManagementEngine '
          'construction - a 0.1s gap before the first tick must not already look stale',
          out.get('capacity_empty', 0) == 0)


def test_staleness_soft_cut_also_sets_full_charge_flag():
    # docs/13 item 13.1, user directive 2026-08-03: "it should hard stop and
    # trigger the stop charging flag" - this is what actually stops an
    # active charge session when the general watchdog fires, since this is
    # the SAME watchdog charging relies on once its one-time live-data gate
    # has passed (bridge/realtime_engine.py's _charge_data_ready).
    eng, rz = fresh()
    eng.config['staleness_watchdog']['soft_cut_s'] = 0.05
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)   # rz has zero inputs - never seen
    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('staleness soft cut sets capacity_empty', out.get('capacity_empty', 0) == 1)
    check('staleness soft cut ALSO sets full_charge_flag (the "stop charging flag")',
          out.get('full_charge_flag', 0) == 1, f"full_charge_flag={out.get('full_charge_flag')}")
    check('staleness soft cut zeroes charge_limit_kw', out.get('charge_limit_kw') == 0.0)
    check('staleness soft cut sets charger_limit_kw to the idle-stop value',
          out.get('charger_limit_kw') == -10.0)


# ── docs/13 item 13.7: missing temperature data must be visible, not silent ─
def test_missing_temp_data_reports_status_and_fault_log_entries():
    eng, rz = fresh()
    # Populate everything EXCEPT temperature - matches base_inputs() minus temp.
    set_all_cells(rz, 3.70)
    rz.update_input('current', 0.0)
    rz.update_input('soc_pct', 60.0)
    rz.update_input('charge_permission_input', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('a status entry exists for over_temperature_derate even with zero temp data',
          'over_temperature_derate' in eng.status)
    check('the status text says so explicitly',
          'no temperature data' in eng.status.get('over_temperature_derate', ''),
          eng.status.get('over_temperature_derate'))
    for key in ('over_temp_emergency', 'charge_cold_block', 'discharge_temp_zero', 'charge_temp_zero'):
        check(f'fault_log entry "{key}" exists (not silently absent) with no temp data',
              key in eng.fault_log.entries)
        check(f'fault_log entry "{key}" is correctly inactive (no data != a real trigger)',
              eng.fault_log.entries[key]['active'] is False)


# ── docs/13 item 14.3: staleness_hard_cut must be staleness-specific, not
# set for any other hard cut - RealtimeEngine uses this exact flag to gate
# the 5th wind-down trigger, so a non-staleness emergency (voltage/temp/
# cross-check) must latch and keep the bridge running, never wind it down.
def test_staleness_hard_cut_flag_not_set_by_a_non_staleness_hard_cut():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.50)  # emergency low-voltage hard cut, NOT staleness
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('a non-staleness hard cut (emergency low voltage) asserts relay_cut_request',
          out.get('relay_cut_request', 0) == 3)
    check('...but does NOT set staleness_hard_cut - only the staleness watchdog may wind the '
          'bridge down', eng.staleness_hard_cut is False)


def test_staleness_hard_cut_flag_set_by_the_staleness_watchdog_escalation():
    eng, rz = fresh()
    eng.config['staleness_watchdog']['soft_cut_s'] = 0.05
    eng.config['staleness_watchdog']['hard_escalation_s'] = 0.05
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    time.sleep(0.08)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('staleness watchdog hard escalation sets relay_cut_request',
          out.get('relay_cut_request', 0) == 3)
    check('...and DOES set staleness_hard_cut, the only condition allowed to wind the bridge down',
          eng.staleness_hard_cut is True)


# ── docs/13 item 14.5: disabling a feature mid-active-fault must clear its
# fault_log entries immediately (live), not freeze them at "active" ────────
def test_disabling_a_feature_mid_fault_clears_its_fault_log_entries_live():
    eng, rz = fresh()
    base_inputs(rz, temp_max=90.0)  # well above the 61C emergency tier
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('sanity: over_temp_emergency is active before disabling the feature',
          eng.fault_log.entries['over_temp_emergency']['active'] is True)

    eng.config['over_temperature_derate']['enabled'] = False
    eng.apply(dict(leaf_signals.DEFAULTS), rz)   # same still-hot input, but the feature is now off
    for key in ('over_temp_emergency', 'charge_cold_block', 'discharge_temp_zero', 'charge_temp_zero'):
        check(f'"{key}" immediately shows inactive once the feature is disabled, not frozen "active"',
              eng.fault_log.entries[key]['active'] is False)
    check('the status text reflects "disabled" rather than the stale hot-probe reading',
          eng.status.get('over_temperature_derate') == 'disabled')


# ── cell_data_cross_check: soft->hard escalation on a genuine per-cell-vs-
# pack-summary disagreement (docs/14 Part 1 TODO - never had any test at all
# before this, despite being a real soft->hard escalating cutoff) ──────────
def test_cell_data_cross_check_soft_and_hard_escalation():
    eng, rz = fresh()
    eng.config['cell_data_cross_check']['soft_cut_s'] = 0.05
    eng.config['cell_data_cross_check']['hard_escalation_s'] = 0.05
    set_all_cells(rz, 3.70)
    rz.update_input('cell_min', 3.00)   # 0x020 pack summary disagrees with the per-cell array by 0.70V
    rz.update_input('cell_max', 4.00)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)

    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('cross-check: not yet latched on the very first tick (persistence guard)',
          out.get('capacity_empty', 0) == 0)
    check('...but the mismatch is already visible in the status text (counting down to the soft cut)',
          'mismatch' in eng.status.get('cell_data_cross_check', '').lower(), eng.status.get('cell_data_cross_check'))
    check('cell_data_mismatch fault_log entry NOT yet active (persistence not elapsed)',
          eng.fault_log.entries['cell_data_mismatch']['active'] is False)

    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('cross-check: soft cut fires once the mismatch has persisted past soft_cut_s',
          out.get('capacity_empty', 0) == 1)
    check('cell_data_mismatch fault_log entry is now active',
          eng.fault_log.entries['cell_data_mismatch']['active'] is True)
    check('cell_data_mismatch_hard NOT yet active (only +hard_escalation_s beyond soft)',
          eng.fault_log.entries['cell_data_mismatch_hard']['active'] is False)

    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('cross-check: escalates to a hard cut after the additional hard_escalation_s',
          out.get('relay_cut_request', 0) == 3)
    check('cell_data_mismatch_hard fault_log entry is now active',
          eng.fault_log.entries['cell_data_mismatch_hard']['active'] is True)

    # Recovery: bring the per-cell array and the pack summary back into agreement.
    rz2 = SharedState()
    set_all_cells(rz2, 3.70)
    rz2.update_input('cell_min', 3.69)
    rz2.update_input('cell_max', 3.71)
    rz2.update_input('temp_max', 25.0)
    rz2.update_input('temp_min', 25.0)
    rz2.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('cell_data_mismatch clears once the sources agree again (auto-clear, not latched)',
          eng.fault_log.entries['cell_data_mismatch']['active'] is False)
    check('cell_data_mismatch_hard also clears',
          eng.fault_log.entries['cell_data_mismatch_hard']['active'] is False)


# ── temp_data_cross_check (docs/13 item 16.2, added 2026-08-04): 0x4A7 pack-
# extremes vs 0x4AA per-probe min/max - same soft->hard escalation pattern
# as the cell data cross-check above, applied to temperature ───────────────
def test_temp_data_cross_check_soft_and_hard_escalation():
    eng, rz = fresh()
    eng.config['temp_data_cross_check']['soft_cut_s'] = 0.05
    eng.config['temp_data_cross_check']['hard_escalation_s'] = 0.05
    set_all_cells(rz, 3.70)
    set_all_temps(rz, 25.0)
    rz.update_input('temp_max', 35.0)   # 0x4A7 summary disagrees with the 16 probes (all 25.0C) by 10C
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)

    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('temp cross-check: not yet latched on the very first tick (persistence guard)',
          out.get('capacity_empty', 0) == 0)
    check('...but the mismatch is already visible in the status text (counting down to the soft cut)',
          'mismatch' in eng.status.get('temp_data_cross_check', '').lower(), eng.status.get('temp_data_cross_check'))
    check('temp_data_mismatch fault_log entry NOT yet active (persistence not elapsed)',
          eng.fault_log.entries['temp_data_mismatch']['active'] is False)

    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('temp cross-check: soft cut fires once the mismatch has persisted past soft_cut_s',
          out.get('capacity_empty', 0) == 1)
    check('temp_data_mismatch fault_log entry is now active',
          eng.fault_log.entries['temp_data_mismatch']['active'] is True)
    check('temp_data_mismatch_hard NOT yet active (only +hard_escalation_s beyond soft)',
          eng.fault_log.entries['temp_data_mismatch_hard']['active'] is False)

    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('temp cross-check: escalates to a hard cut after the additional hard_escalation_s',
          out.get('relay_cut_request', 0) == 3)
    check('temp_data_mismatch_hard fault_log entry is now active',
          eng.fault_log.entries['temp_data_mismatch_hard']['active'] is True)

    # Recovery: bring the pack-extremes summary and the 16 probes back into agreement.
    rz2 = SharedState()
    set_all_cells(rz2, 3.70)
    set_all_temps(rz2, 25.0)
    rz2.update_input('temp_max', 25.6)
    rz2.update_input('temp_min', 24.4)
    rz2.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('temp_data_mismatch clears once the sources agree again (auto-clear, not latched)',
          eng.fault_log.entries['temp_data_mismatch']['active'] is False)
    check('temp_data_mismatch_hard also clears',
          eng.fault_log.entries['temp_data_mismatch_hard']['active'] is False)


def test_temp_data_cross_check_no_data_yet_does_not_false_trigger():
    """Every existing test in this file uses base_inputs()/only sets
    temp_max/temp_min, never the 16 individual temp_XX probes - confirms the
    new feature correctly reports 'no data' (not a false mismatch) when the
    individual-probe side of the comparison has never arrived, so this
    feature can't retroactively break any of the other tests in this file."""
    eng, rz = fresh()
    base_inputs(rz)   # sets temp_max/temp_min only, no individual temp_XX probes
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('no individual probe data yet -> reports "no data", does not cut',
          eng.status.get('temp_data_cross_check') == 'no data to cross-check yet',
          eng.status.get('temp_data_cross_check'))
    check('does not assert capacity_empty from missing probe data alone',
          out.get('capacity_empty', 0) == 0)


def test_temp_data_cross_check_disabling_clears_fault_log_live():
    eng, rz = fresh()
    eng.config['temp_data_cross_check']['soft_cut_s'] = 0.0
    eng.config['temp_data_cross_check']['hard_escalation_s'] = 0.0
    set_all_cells(rz, 3.70)
    set_all_temps(rz, 25.0)
    rz.update_input('temp_max', 35.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('sanity: mismatch is active before disabling',
          eng.fault_log.entries['temp_data_mismatch_hard']['active'] is True)

    eng.config['temp_data_cross_check']['enabled'] = False
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('disabling the feature clears temp_data_mismatch live, same tick',
          eng.fault_log.entries['temp_data_mismatch']['active'] is False)
    check('disabling the feature clears temp_data_mismatch_hard live, same tick',
          eng.fault_log.entries['temp_data_mismatch_hard']['active'] is False)
    # Correctly does NOT clear relay_cut_request - a hard cut LATCHES
    # (docs/12 finding F8) regardless of the triggering feature later being
    # disabled; only a genuine notify_session_start()/notify_charge_replug()
    # re-arm clears it. Disabling the feature stops it from being able to
    # trigger a NEW hard cut, it doesn't retroactively un-latch one already
    # asserted - same behavior every other hard-cut feature has.
    check('the hard-cut latch correctly stays asserted (disabling a feature does not un-latch it)',
          out.get('relay_cut_request', 0) == 3)


# ── temp_probe_cross_check (added 2026-08-14, user directive: "add the cross
# check. this is key to make sure we have good data") - DID 0x1814 (primary)
# vs 0x4AA CAN (backup), per-probe, distinct from temp_data_cross_check
# above (which only compares the 0x4A7 pack-extremes summary against the
# probe array as a whole) ───────────────────────────────────────────────────
def test_temp_probe_cross_check_soft_and_hard_escalation():
    eng, rz = fresh()
    eng.config['temp_probe_cross_check']['soft_cut_s'] = 0.05
    eng.config['temp_probe_cross_check']['hard_escalation_s'] = 0.05
    set_all_cells(rz, 3.70)
    set_all_temps_did(rz, 25.0)
    set_all_temps_can(rz, 25.0, exceptions={'temp_09_can': 15.0})  # probe 9 disagrees by 10C
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)

    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('probe cross-check: not yet latched on the very first tick (persistence guard)',
          out.get('capacity_empty', 0) == 0)
    check('...but the mismatch is already visible in the status text, naming the worst probe',
          'probe 9' in eng.status.get('temp_probe_cross_check', ''), eng.status.get('temp_probe_cross_check'))
    check('temp_probe_mismatch fault_log entry NOT yet active (persistence not elapsed)',
          eng.fault_log.entries['temp_probe_mismatch']['active'] is False)

    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('probe cross-check: soft cut fires once the mismatch has persisted past soft_cut_s',
          out.get('capacity_empty', 0) == 1)
    check('temp_probe_mismatch fault_log entry is now active',
          eng.fault_log.entries['temp_probe_mismatch']['active'] is True)
    check('temp_probe_mismatch_hard NOT yet active (only +hard_escalation_s beyond soft)',
          eng.fault_log.entries['temp_probe_mismatch_hard']['active'] is False)

    time.sleep(0.08)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('probe cross-check: escalates to a hard cut after the additional hard_escalation_s',
          out.get('relay_cut_request', 0) == 3)
    check('temp_probe_mismatch_hard fault_log entry is now active',
          eng.fault_log.entries['temp_probe_mismatch_hard']['active'] is True)

    # Recovery: bring the DID and CAN readings for every probe back into agreement.
    rz2 = SharedState()
    set_all_cells(rz2, 3.70)
    set_all_temps_did(rz2, 25.0)
    set_all_temps_can(rz2, 25.0)
    rz2.update_input('temp_max', 25.0)
    rz2.update_input('temp_min', 25.0)
    rz2.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('temp_probe_mismatch clears once the sources agree again (auto-clear, not latched)',
          eng.fault_log.entries['temp_probe_mismatch']['active'] is False)
    check('temp_probe_mismatch_hard also clears',
          eng.fault_log.entries['temp_probe_mismatch_hard']['active'] is False)


def test_temp_probe_cross_check_no_data_yet_does_not_false_trigger():
    """base_inputs() never sets any temp_XX_did/temp_XX_can key (only the
    0x4A7 temp_max/temp_min pair) - confirms this feature correctly reports
    'no data' (not a false mismatch) before either source has ever arrived,
    e.g. before the app's DID poll loop has completed its first temp probe
    request."""
    eng, rz = fresh()
    base_inputs(rz)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('no DID/CAN probe data yet -> reports "no data", does not cut',
          eng.status.get('temp_probe_cross_check') == 'no data to cross-check yet',
          eng.status.get('temp_probe_cross_check'))
    check('does not assert capacity_empty from missing probe data alone',
          out.get('capacity_empty', 0) == 0)


def test_temp_probe_cross_check_disabling_clears_fault_log_live():
    eng, rz = fresh()
    eng.config['temp_probe_cross_check']['soft_cut_s'] = 0.0
    eng.config['temp_probe_cross_check']['hard_escalation_s'] = 0.0
    set_all_cells(rz, 3.70)
    set_all_temps_did(rz, 25.0)
    set_all_temps_can(rz, 15.0)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('sanity: mismatch is active before disabling',
          eng.fault_log.entries['temp_probe_mismatch_hard']['active'] is True)

    eng.config['temp_probe_cross_check']['enabled'] = False
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('disabling the feature clears temp_probe_mismatch live, same tick',
          eng.fault_log.entries['temp_probe_mismatch']['active'] is False)
    check('disabling the feature clears temp_probe_mismatch_hard live, same tick',
          eng.fault_log.entries['temp_probe_mismatch_hard']['active'] is False)
    check('the hard-cut latch correctly stays asserted (disabling a feature does not un-latch it)',
          out.get('relay_cut_request', 0) == 3)


# ── ManagementEngine must be the sole, EXPLICIT authority over capacity_
# empty/relay_cut_request/interlock - not just conditionally force them
# toward a cut, but also explicitly clear them back (docs/13 item 16.3,
# added 2026-08-04). Regression guard for the scenario the fix closes: a
# stray non-zero value sitting in leaf_state (formerly reachable via a
# Signal Mapping tie, now also structurally impossible per
# test_management_exclusive_keys_are_not_mapping_targets) must not survive
# a healthy tick. full_charge_flag is deliberately NOT covered here - see
# apply()'s own comment for why an unconditional clear there would be wrong
# (RealtimeEngine._apply_charge_ramp() legitimately sets it from a
# different module) ─────────────────────────────────────────────────────────
def test_management_explicitly_clears_capacity_empty_and_hard_cut_fields_when_healthy():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70)   # perfectly healthy - no cutoff condition anywhere
    leaf_state = dict(leaf_signals.DEFAULTS)
    # Simulate what a stray mapping tie (or any other bug) could have left
    # sitting in leaf_state before management runs, per the real order of
    # operations in RealtimeEngine._compose_leaf_state() (mapping applies
    # BEFORE management).
    leaf_state['capacity_empty'] = 1
    leaf_state['relay_cut_request'] = 3
    leaf_state['interlock'] = 0

    out = eng.apply(leaf_state, rz)
    check('capacity_empty is explicitly cleared back to 0 when healthy, not left stuck at 1',
          out.get('capacity_empty') == 0, out.get('capacity_empty'))
    check('relay_cut_request is explicitly cleared back to 0 when healthy, not left stuck at 3',
          out.get('relay_cut_request') == 0, out.get('relay_cut_request'))
    check('interlock is explicitly cleared back to 1 when healthy, not left stuck at 0',
          out.get('interlock') == 1, out.get('interlock'))


# ── config_sanity: a deliberately-inverted threshold must be caught (docs/14
# Part 1 TODO - never had any test before this) ────────────────────────────
def test_config_sanity_detects_inverted_threshold():
    eng, rz = fresh()
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('config_sanity reports ok with the default (non-inverted) thresholds',
          eng.status.get('config_sanity') == 'ok', eng.status.get('config_sanity'))
    check('config_sanity fault_log entry is inactive by default',
          eng.fault_log.entries['config_sanity']['active'] is False)

    eng.config['low_voltage_cutoff']['emergency_low_v'] = 3.50   # now HIGHER than min_cell_v (3.00) - inverted
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    status = eng.status.get('config_sanity', '')
    check('config_sanity detects the inverted emergency/soft-cut ordering',
          'low_voltage_cutoff' in status and 'emergency' in status.lower(), status)
    check('config_sanity fault_log entry is now active',
          eng.fault_log.entries['config_sanity']['active'] is True)

    eng.config['low_voltage_cutoff']['emergency_low_v'] = 2.60   # fix it
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('config_sanity clears once the ordering is corrected',
          eng.status.get('config_sanity') == 'ok')
    check('config_sanity fault_log entry clears too',
          eng.fault_log.entries['config_sanity']['active'] is False)


# ── Hysteresis: fast-attack/slow-release, both tapers (docs/14 Part 1 TODO -
# claimed discharge_power_taper already had a direct test; it did NOT, and
# neither did the regen side - both closed here) ───────────────────────────
def test_discharge_power_taper_hysteresis_fast_attack_slow_release():
    eng, rz = fresh()
    eng.config['discharge_power_taper']['recovery_ramp_s'] = 1.0
    set_all_cells(rz, 3.70)   # full power, well above taper_start_v (3.00V)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)   # establish the factor=1.0 baseline

    set_all_cells(rz, 2.50)   # sudden dip well below taper_min_v (2.60V)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('fast attack: discharge power snaps straight to zero the SAME tick as the dip',
          out['discharge_limit_kw'] == 0.0, f"discharge_limit_kw={out['discharge_limit_kw']}")

    set_all_cells(rz, 3.70)   # voltage recovers instantly back to full-power territory
    time.sleep(0.4)   # ~40% of the way through the 1.0s recovery ramp
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    frac = out['discharge_limit_kw'] / leaf_signals.DEFAULTS['discharge_limit_kw']
    check('slow release: only a PARTIAL recovery after ~40% of the ramp time, not instant full power',
          0.25 < frac < 0.65, f"discharge_limit_kw={out['discharge_limit_kw']} (fraction={frac:.2f})")

    time.sleep(0.7)   # now well past the full 1.0s ramp
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('fully recovers to full power once the ramp has had enough total time to complete',
          out['discharge_limit_kw'] == leaf_signals.DEFAULTS['discharge_limit_kw'],
          f"discharge_limit_kw={out['discharge_limit_kw']}")


def test_charge_target_taper_regen_hysteresis_fast_attack_slow_release():
    eng, rz = fresh()
    eng.config['charge_target_taper']['recovery_ramp_s'] = 1.0
    set_all_cells(rz, 3.70)   # full regen power, well below regen_full_v (4.00V)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    rz.update_input('charge_permission_input', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)   # establish the factor=1.0 baseline

    # 4.18V: above regen_min_v (4.15V), so the taper's OWN hysteresis already
    # reads instant_factor=0.0 - deliberately still below emergency_high_v
    # (4.20V) so this exercises the taper's ramp logic, not the separate
    # emergency-tier direct-set-to-0 branch.
    set_all_cells(rz, 4.18)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('fast attack: regen power snaps straight to zero the SAME tick as the rise',
          out['charge_limit_kw'] == 0.0, f"charge_limit_kw={out['charge_limit_kw']}")
    check('sanity: this is the proactive taper, not the emergency hard cut (still below 4.20V)',
          out.get('relay_cut_request', 0) == 0)

    set_all_cells(rz, 3.70)   # voltage recovers back to safe territory
    time.sleep(0.4)   # ~40% of the way through the 1.0s recovery ramp
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    frac = out['charge_limit_kw'] / leaf_signals.DEFAULTS['charge_limit_kw']
    check('slow release: only a PARTIAL recovery after ~40% of the ramp time, not instant full power',
          0.25 < frac < 0.65, f"charge_limit_kw={out['charge_limit_kw']} (fraction={frac:.2f})")

    time.sleep(0.7)   # now well past the full 1.0s ramp
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('fully recovers to full regen power once the ramp has had enough total time to complete',
          out['charge_limit_kw'] == leaf_signals.DEFAULTS['charge_limit_kw'],
          f"charge_limit_kw={out['charge_limit_kw']}")


# ── discharge_min_kw/discharge_max_kw + regen_min_kw/regen_max_kw (added
# 2026-08-08, docs/16 parameter-clamping audit - user directive: "both need
# min and max settings by the user", same pattern as charger_limit_kw's
# ac_min_kw/ac_max_kw). Defaults (0.0/110.0, 0.0/70.0) are backward-compatible
# no-ops - the two hysteresis tests above already confirm default behavior is
# unchanged. These tests explicitly configure a nonzero floor/lowered ceiling
# to confirm the NEW behavior actually engages. ───────────────────────────
def test_discharge_power_taper_respects_configured_floor_and_ceiling():
    eng, rz = fresh()
    eng.config['discharge_power_taper']['discharge_min_kw'] = 10.0
    eng.config['discharge_power_taper']['discharge_max_kw'] = 80.0
    set_all_cells(rz, 3.70)   # full power, well above taper_start_v (3.00V)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('ceiling: full-power output caps at the configured 80.0kW, not the static 110.0kW default',
          out['discharge_limit_kw'] == 80.0, f"discharge_limit_kw={out['discharge_limit_kw']}")

    set_all_cells(rz, 2.50)   # well below taper_min_v (2.60V) - taper factor bottoms out
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('floor: bottomed-out output holds at the configured 10.0kW, not true zero',
          out['discharge_limit_kw'] == 10.0, f"discharge_limit_kw={out['discharge_limit_kw']}")


def test_charge_target_taper_regen_respects_configured_floor_and_ceiling():
    eng, rz = fresh()
    eng.config['charge_target_taper']['regen_min_kw'] = 5.0
    eng.config['charge_target_taper']['regen_max_kw'] = 40.0
    set_all_cells(rz, 3.70)   # full regen power, well below regen_full_v (4.00V)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    rz.update_input('charge_permission_input', 0.0)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('ceiling: full-power regen output caps at the configured 40.0kW, not the static 70.0kW default',
          out['charge_limit_kw'] == 40.0, f"charge_limit_kw={out['charge_limit_kw']}")

    set_all_cells(rz, 4.18)   # above regen_min_v (4.15V), still below emergency_high_v (4.20V)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('floor: bottomed-out regen output holds at the configured 5.0kW, not true zero',
          out['charge_limit_kw'] == 5.0, f"charge_limit_kw={out['charge_limit_kw']}")


def test_charge_target_taper_emergency_bypasses_regen_min_kw_floor():
    # Safety-critical case (docs/16 audit finding): a nonzero regen_min_kw
    # floor must NOT keep feeding power into a cell that just hit the
    # OVERVOLTAGE EMERGENCY threshold - matches ac_charge_taper's own
    # emergency branch, which also unconditionally zeroes its output,
    # bypassing ac_min_kw entirely.
    eng, rz = fresh()
    eng.config['charge_target_taper']['regen_min_kw'] = 5.0
    set_all_cells(rz, 4.35)   # above emergency_high_v (4.20V default)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    rz.update_input('charge_permission_input', 0.0)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('regen emergency hard cut is literal 0.0, NOT the configured 5.0kW floor',
          out['charge_limit_kw'] == 0.0, f"charge_limit_kw={out['charge_limit_kw']}")
    check('sanity: this really is the emergency tier (hard cut fires)',
          out.get('relay_cut_request', 0) == 3)


def test_discharge_regen_min_max_sanity_checks():
    eng, rz = fresh()
    eng.config['discharge_power_taper']['discharge_min_kw'] = 90.0
    eng.config['discharge_power_taper']['discharge_max_kw'] = 10.0   # inverted
    eng.config['charge_target_taper']['regen_min_kw'] = 90.0
    eng.config['charge_target_taper']['regen_max_kw'] = 10.0   # inverted
    base_inputs(rz, cell_v=3.70)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('config-sanity check flags an inverted discharge_min_kw/discharge_max_kw ordering',
          'discharge_min_kw' in eng.status.get('config_sanity', ''), eng.status.get('config_sanity'))
    check('config-sanity check flags an inverted regen_min_kw/regen_max_kw ordering',
          'regen_min_kw' in eng.status.get('config_sanity', ''), eng.status.get('config_sanity'))


# ── Staleness watchdog: a signal that WAS live, then stopped updating mid-
# session (docs/14 Part 1 TODO - existing tests only covered "never arrived
# at all", never a signal going fresh->stale while everything else stays
# fresh) ─────────────────────────────────────────────────────────────────
def test_staleness_watchdog_flags_a_signal_that_went_stale_mid_session():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('sanity: watchdog reports ok on the first tick with genuinely fresh data',
          out.get('capacity_empty', 0) == 0, eng.status.get('staleness_watchdog'))

    # temp_max specifically goes stale mid-session - it WAS live (unlike the
    # "never arrived" case other tests already cover), then simply stopped
    # updating while everything else stays fresh.
    with rz.lock:
        rz.rz450e_ts['temp_max'] = time.monotonic() - 61.0
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('the watchdog fires from the one signal that went stale after being live',
          out.get('capacity_empty', 0) == 1, eng.status.get('staleness_watchdog'))
    status = eng.status.get('staleness_watchdog', '')
    check('the status text names temp_max specifically as the stale signal', 'temp_max' in status, status)
    check('...NOT flagged as "never seen" - it really was live before going stale',
          'never seen' not in status, status)


# ── Boundary-value sweeps (docs/13 item 6.3) - existing tests checked well
# past a threshold/persistence window but never right at its boundary; each
# test below checks just-before AND just-after the exact configured value ──
def test_boundary_low_voltage_soft_cut_persistence():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.90)  # below 3.00V soft floor, above 2.60V emergency floor
    eng.apply(dict(leaf_signals.DEFAULTS), rz)   # t=0, condition first observed
    time.sleep(1.9)   # just BEFORE the default 2.0s persistence window
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: soft cut does NOT latch just before the 2.0s persistence window (1.9s)',
          out.get('capacity_empty', 0) == 0, f"capacity_empty={out.get('capacity_empty')}")

    time.sleep(0.2)   # now just past the full 2.0s
    out2 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: soft cut DOES latch just after the 2.0s persistence window',
          out2.get('capacity_empty', 0) == 1, f"capacity_empty={out2.get('capacity_empty')}")


def test_boundary_overcurrent_persistence():
    eng, rz = fresh()
    base_inputs(rz, current=160.0)  # above the 150A discharge-warn default
    eng.apply(dict(leaf_signals.DEFAULTS), rz)   # t=0
    time.sleep(4.9)   # just BEFORE the default 5.0s persistence window
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: overcurrent does NOT warn just before the 5.0s persistence window (4.9s)',
          eng.fault_log.entries['overcurrent_discharge_warn']['active'] is False)

    time.sleep(0.2)   # now just past the full 5.0s
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: overcurrent DOES warn just after the 5.0s persistence window',
          eng.fault_log.entries['overcurrent_discharge_warn']['active'] is True)


def test_boundary_emergency_temp():
    eng, rz = fresh()
    base_inputs(rz, temp_max=60.9, temp_min=25.0)   # just BELOW 61C emergency
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: just below 61C emergency temp - no hard cut',
          out.get('relay_cut_request', 0) == 0, f"relay_cut_request={out.get('relay_cut_request')}")

    eng2, rz2 = fresh()
    base_inputs(rz2, temp_max=61.0, temp_min=25.0)   # EXACTLY at 61C (the check is >=)
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('boundary: EXACTLY at 61C emergency temp - hard cut fires (>= comparison)',
          out2.get('relay_cut_request', 0) == 3, f"relay_cut_request={out2.get('relay_cut_request')}")

    eng3, rz3 = fresh()
    base_inputs(rz3, temp_max=61.1, temp_min=25.0)   # just ABOVE 61C emergency
    out3 = eng3.apply(dict(leaf_signals.DEFAULTS), rz3)
    check('boundary: just above 61C emergency temp - hard cut fires',
          out3.get('relay_cut_request', 0) == 3, f"relay_cut_request={out3.get('relay_cut_request')}")


def test_boundary_cell_imbalance_warn_delta():
    eng, rz = fresh()
    exceptions = {'cell_01': 3.551}   # 99mV below the rest - just BELOW the 100mV warn threshold
    set_all_cells(rz, 3.650, exceptions)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: 99mV spread does NOT trigger the 100mV warn threshold',
          eng.fault_log.entries['cell_imbalance_warn']['active'] is False)

    eng2, rz2 = fresh()
    exceptions2 = {'cell_01': 3.549}   # 101mV below the rest - just ABOVE the 100mV warn threshold
    set_all_cells(rz2, 3.650, exceptions2)
    rz2.update_input('temp_max', 25.0)
    rz2.update_input('temp_min', 25.0)
    rz2.update_input('current', 0.0)
    eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('boundary: 101mV spread DOES trigger the 100mV warn threshold',
          eng2.fault_log.entries['cell_imbalance_warn']['active'] is True)


def test_boundary_cell_data_cross_check_delta_and_escalation_timing():
    # max_delta_v boundary (150mV default) - no persistence needed to observe this part.
    eng, rz = fresh()
    set_all_cells(rz, 3.70)
    rz.update_input('cell_min', 3.551)   # delta = 149mV, just BELOW the 150mV threshold
    rz.update_input('cell_max', 3.70)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: 149mV cross-check delta does NOT trigger the 150mV threshold',
          'ok' in eng.status.get('cell_data_cross_check', ''), eng.status.get('cell_data_cross_check'))

    # Escalation timing boundary - shrunk soft_cut_s/hard_escalation_s (0.15s
    # each) so the test doesn't need to wait the real 60s/+5s, with ~30ms
    # margins on either side of each transition.
    eng2, rz2 = fresh()
    eng2.config['cell_data_cross_check']['soft_cut_s'] = 0.15
    eng2.config['cell_data_cross_check']['hard_escalation_s'] = 0.15
    set_all_cells(rz2, 3.70)
    rz2.update_input('cell_min', 3.549)   # delta = 151mV, just ABOVE the 150mV threshold
    rz2.update_input('cell_max', 3.70)
    rz2.update_input('temp_max', 25.0)
    rz2.update_input('temp_min', 25.0)
    rz2.update_input('current', 0.0)
    eng2.apply(dict(leaf_signals.DEFAULTS), rz2)   # t=0, mismatch first observed

    time.sleep(0.12)
    out = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('boundary: cross-check soft cut NOT yet active just before soft_cut_s',
          out.get('capacity_empty', 0) == 0, f"capacity_empty={out.get('capacity_empty')}")

    time.sleep(0.06)   # ~t=0.18, past the 0.15s soft_cut_s
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('boundary: cross-check soft cut ACTIVE just after soft_cut_s',
          out2.get('capacity_empty', 0) == 1, f"capacity_empty={out2.get('capacity_empty')}")
    check('boundary: cross-check hard cut NOT yet active immediately after soft_cut_s',
          out2.get('relay_cut_request', 0) == 0, f"relay_cut_request={out2.get('relay_cut_request')}")

    time.sleep(0.09)   # ~t=0.27, still just before soft_cut_s+hard_escalation_s (0.30)
    out3 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('boundary: cross-check hard cut NOT yet active just before soft_cut_s+hard_escalation_s',
          out3.get('relay_cut_request', 0) == 0, f"relay_cut_request={out3.get('relay_cut_request')}")

    time.sleep(0.06)   # ~t=0.33, past soft_cut_s+hard_escalation_s
    out4 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('boundary: cross-check hard cut ACTIVE just after soft_cut_s+hard_escalation_s',
          out4.get('relay_cut_request', 0) == 3, f"relay_cut_request={out4.get('relay_cut_request')}")


def test_boundary_temp_probe_cross_check_delta():
    # max_delta_c boundary (2.0C default) - no persistence needed to observe this part.
    eng, rz = fresh()
    set_all_cells(rz, 3.70)
    set_all_temps_did(rz, 25.0)
    set_all_temps_can(rz, 25.0, exceptions={'temp_05_can': 23.01})  # delta = 1.99C, just BELOW 2.0C
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: 1.99C probe delta does NOT trigger the 2.0C threshold',
          'ok' in eng.status.get('temp_probe_cross_check', ''), eng.status.get('temp_probe_cross_check'))

    eng2, rz2 = fresh()
    set_all_cells(rz2, 3.70)
    set_all_temps_did(rz2, 25.0)
    set_all_temps_can(rz2, 25.0, exceptions={'temp_05_can': 22.99})  # delta = 2.01C, just ABOVE 2.0C
    rz2.update_input('temp_max', 25.0)
    rz2.update_input('temp_min', 25.0)
    rz2.update_input('current', 0.0)
    eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('boundary: 2.01C probe delta DOES trigger the 2.0C threshold (mismatch counting down to soft cut)',
          'mismatch probe 5' in eng2.status.get('temp_probe_cross_check', ''),
          eng2.status.get('temp_probe_cross_check'))


def test_boundary_staleness_watchdog_soft_and_hard_escalation():
    eng, rz = fresh()
    # Shrunk soft_cut_s/hard_escalation_s (0.15s each) so the test doesn't
    # need to wait the real 60s/+5s, with ~30ms margins around each boundary.
    eng.config['staleness_watchdog']['soft_cut_s'] = 0.15
    eng.config['staleness_watchdog']['hard_escalation_s'] = 0.15
    eng.apply(dict(leaf_signals.DEFAULTS), rz)   # t=0 - this IS the engine's first apply() call

    time.sleep(0.12)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: staleness soft cut NOT yet active just before soft_cut_s',
          out.get('capacity_empty', 0) == 0, f"capacity_empty={out.get('capacity_empty')}")

    time.sleep(0.06)   # ~t=0.18, past the 0.15s soft_cut_s
    out2 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: staleness soft cut ACTIVE just after soft_cut_s',
          out2.get('capacity_empty', 0) == 1, f"capacity_empty={out2.get('capacity_empty')}")

    time.sleep(0.09)   # ~t=0.27, still just before soft_cut_s+hard_escalation_s (0.30)
    out3 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: staleness hard cut NOT yet active just before soft_cut_s+hard_escalation_s',
          out3.get('relay_cut_request', 0) == 0, f"relay_cut_request={out3.get('relay_cut_request')}")

    time.sleep(0.06)   # ~t=0.33, past soft_cut_s+hard_escalation_s
    out4 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: staleness hard cut ACTIVE just after soft_cut_s+hard_escalation_s',
          out4.get('relay_cut_request', 0) == 3, f"relay_cut_request={out4.get('relay_cut_request')}")


# ── AC charger temperature derate (added 2026-08-11, user report: "we dont
# have any heat regulation for charging... we need separate inputs for
# those temps for when charging in the charger tab. there not the same
# values"). Same split already applied to voltage (ac_charge_taper vs.
# charge_target_taper): independently-tunable cold/hot thresholds, only
# ever touches charger_limit_kw, only while a real charge session is
# active. ────────────────────────────────────────────────────────────────
def test_ac_charge_temp_derate_leaves_charger_limit_kw_untouched_while_driving():
    eng, rz = fresh()
    base_inputs(rz, temp_max=50.0, temp_min=50.0)   # well inside the hot-derate window - NOT charging (base_inputs defaults charge_permission_input to 0)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 92.3   # idle placeholder, as mapping/ramp would leave it while driving
    out = eng.apply(leaf_state, rz)
    check('charger_limit_kw is left completely untouched by ac_charge_temp_derate while not actually charging',
          out['charger_limit_kw'] == 92.3, f"charger_limit_kw={out['charger_limit_kw']}")


def test_ac_charge_temp_derate_cold_side_ramps_and_blocks_no_latch():
    eng, rz = fresh()
    base_inputs(rz, temp_max=-6.7, temp_min=-6.7)   # well below ac_low_block_c (0.0C default)
    rz.update_input('charge_permission_input', 1.0)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 6.6
    out = eng.apply(leaf_state, rz)
    check('AC charging blocked (charger_limit_kw=0) when coldest probe is at/below ac_low_block_c',
          out['charger_limit_kw'] == 0.0, f"charger_limit_kw={out['charger_limit_kw']}")
    check('ac_charge_cold_block fault_log entry is active',
          eng.fault_log.entries['ac_charge_cold_block']['active'] is True)
    check('cold-side block does NOT latch a session stop (no full_charge_flag)',
          out.get('full_charge_flag', 0) == 0, f"full_charge_flag={out.get('full_charge_flag')}")

    # Warming back up above ac_derate_low_start_c must auto-resume with no
    # latch/replug needed - mirrors driving-mode's own cold-side behavior.
    base_inputs(rz, temp_max=15.0, temp_min=15.0)
    rz.update_input('charge_permission_input', 1.0)
    out2 = eng.apply(dict(leaf_state), rz)
    check('AC charging auto-resumes to full power once the coldest probe warms back above ac_derate_low_start_c',
          out2['charger_limit_kw'] == 6.6, f"charger_limit_kw={out2['charger_limit_kw']}")
    check('ac_charge_cold_block fault_log entry auto-clears',
          eng.fault_log.entries['ac_charge_cold_block']['active'] is False)


def test_ac_charge_temp_derate_hot_side_ramps_and_latches_stop():
    eng, rz = fresh()
    base_inputs(rz, temp_max=45.0, temp_min=25.0)   # exactly at ac_hard_stop_c (45.0C default)
    rz.update_input('charge_permission_input', 1.0)
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 6.6
    out = eng.apply(leaf_state, rz)
    check('reaching ac_hard_stop_c sets full_charge_flag (session ends, not just a derate)',
          out.get('full_charge_flag', 0) == 1, f"full_charge_flag={out.get('full_charge_flag')}")
    check('charge_limit_kw is forced to 0.0 at the AC charging hard-stop temp',
          out.get('charge_limit_kw') == 0.0, f"charge_limit_kw={out.get('charge_limit_kw')}")
    check('charger_limit_kw is forced to -10.0 (raw idle-stop value) at the AC charging hard-stop temp',
          out.get('charger_limit_kw') == -10.0, f"charger_limit_kw={out.get('charger_limit_kw')}")
    check('ac_charge_temp_stop fault_log entry is active',
          eng.fault_log.entries['ac_charge_temp_stop']['active'] is True)

    # Latch must survive the pack cooling back down - same discipline as
    # ac_cutoff_v's own stop-charging latch (test above).
    base_inputs(rz, temp_max=25.0, temp_min=25.0)
    rz.update_input('charge_permission_input', 1.0)
    out2 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('AC charge stop stays latched once the triggering temp recovers (does not silently resume)',
          out2.get('full_charge_flag', 0) == 1, f"full_charge_flag={out2.get('full_charge_flag')}")

    # Only a genuine replug clears it.
    eng.notify_charge_replug()
    base_inputs(rz, temp_max=25.0, temp_min=25.0)
    rz.update_input('charge_permission_input', 1.0)
    out3 = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('notify_charge_replug() clears the AC charging temp-stop latch, allowing charging to resume',
          out3.get('full_charge_flag', 0) == 0, f"full_charge_flag={out3.get('full_charge_flag')}")


def test_ac_charge_temp_derate_disabled_clears_fault_log_live():
    eng, rz = fresh()
    base_inputs(rz, temp_max=45.0, temp_min=25.0)   # would otherwise latch a stop
    rz.update_input('charge_permission_input', 1.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('sanity: ac_charge_temp_stop is active before disabling',
          eng.fault_log.entries['ac_charge_temp_stop']['active'] is True)

    rz.charge_emulation['ac_temp_derate_enabled'] = False
    base_inputs(rz, temp_max=45.0, temp_min=25.0)
    rz.update_input('charge_permission_input', 1.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('disabling ac_charge_temp_derate immediately clears ac_charge_temp_stop live, not frozen active',
          eng.fault_log.entries['ac_charge_temp_stop']['active'] is False)
    check('disabling ac_charge_temp_derate immediately clears ac_charge_cold_block live too',
          eng.fault_log.entries['ac_charge_cold_block']['active'] is False)


def test_over_temperature_derate_no_longer_touches_charger_limit_kw_except_true_emergency():
    # Regression coverage for the 2026-08-11 fix: over_temperature_derate's
    # graduated (non-emergency) ramp used to also multiply charger_limit_kw
    # using the DRIVING-mode thresholds - now that's ac_charge_temp_derate's
    # own job (with its own thresholds, tested above), so the graduated
    # ramp here must leave charger_limit_kw alone. The true pack-wide
    # emergency tier is the one exception - it must still zero
    # charger_limit_kw as the final universal backstop.
    eng, rz = fresh()
    base_inputs(rz, temp_max=40.0, temp_min=25.0)   # inside over_temperature_derate's charge_derate_start_c(32)..charge_hard_stop_c(45) ramp window - a real graduated derate, not full power
    rz.update_input('charge_permission_input', 0.0)   # driving - ac_charge_temp_derate stays inactive, isolating this check to over_temperature_derate alone
    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 92.3
    out = eng.apply(leaf_state, rz)
    check('graduated over_temperature_derate no longer reduces charger_limit_kw (that is ac_charge_temp_derate\'s job now)',
          out['charger_limit_kw'] == 92.3, f"charger_limit_kw={out['charger_limit_kw']}")
    check('sanity: the graduated ramp DID reduce charge_limit_kw (regen), confirming the feature is genuinely active this tick',
          out['charge_limit_kw'] < leaf_signals.DEFAULTS['charge_limit_kw'],
          f"charge_limit_kw={out['charge_limit_kw']}")

    eng2, rz2 = fresh()
    base_inputs(rz2, temp_max=65.0, temp_min=25.0)   # above emergency_temp_c (61.0C default) - a genuine pack-wide emergency
    rz2.update_input('charge_permission_input', 0.0)
    leaf_state2 = dict(leaf_signals.DEFAULTS)
    leaf_state2['charger_limit_kw'] = 92.3
    out2 = eng2.apply(leaf_state2, rz2)
    check('the TRUE over-temperature emergency still zeroes charger_limit_kw as the final universal backstop',
          out2['charger_limit_kw'] == 0.0, f"charger_limit_kw={out2['charger_limit_kw']}")


# ── SoC blending for discharge_power_taper / charge_target_taper (added
# 2026-08-13, user directive: "use SOC as the primary control and the
# battery voltage as a secondary... SOC does not dramatically drop like
# voltage does" - see tests/check_taper_smoothness.py for the investigation
# that motivated this and default_config()'s own comment for the full
# design rationale). Combined via min(voltage_factor, soc_factor), never a
# straight replacement of the pre-existing voltage-only behavior. Uses
# fresh()'s code defaults (default_config()), NOT config/profile.json's own
# tuned values - taper_start_v=3.00/taper_min_v=2.60 (discharge),
# regen_full_v=4.00/regen_min_v=4.15 (regen). ─────────────────────────────
def test_discharge_soc_factor_tapers_with_healthy_voltage():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70, soc=14.0)   # healthy voltage (full power), SoC exactly at the midpoint of the default 20%/8% window
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    factor = out['discharge_limit_kw'] / leaf_signals.DEFAULTS['discharge_limit_kw']
    check('SoC alone tapers discharge power to exactly half at the midpoint of its window (14% between '
          '20%/8%), even with fully healthy per-cell voltage',
          abs(factor - 0.5) < 1e-9, f'factor={factor!r} (expected exactly 0.5)')
    check('status text names SoC as the binding (more restrictive) factor',
          'binding=SoC' in eng.status.get('discharge_power_taper', ''), eng.status.get('discharge_power_taper'))


def test_discharge_voltage_still_provides_quick_cutoff_despite_healthy_soc():
    eng, rz = fresh()
    base_inputs(rz, cell_v=2.80, soc=60.0)   # voltage at the midpoint of its 3.00/2.60 window, SoC well above its own 20% full-power floor
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    factor = out['discharge_limit_kw'] / leaf_signals.DEFAULTS['discharge_limit_kw']
    check('a real voltage sag still restricts discharge power even with a fully healthy SoC reading - the '
          '"quick cutoff still works with voltage" property the SoC blending must not remove',
          abs(factor - 0.5) < 1e-9, f'factor={factor!r} (expected exactly 0.5)')
    check('status text names voltage as the binding (more restrictive) factor',
          'binding=voltage' in eng.status.get('discharge_power_taper', ''), eng.status.get('discharge_power_taper'))


def test_discharge_full_power_when_both_soc_and_voltage_are_healthy():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70, soc=60.0)   # both well above their respective full-power floors
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('full discharge power when neither voltage nor SoC is anywhere near its taper window',
          out['discharge_limit_kw'] == leaf_signals.DEFAULTS['discharge_limit_kw'],
          f"discharge_limit_kw={out['discharge_limit_kw']}")


def test_discharge_missing_soc_falls_back_to_voltage_only():
    eng, rz = fresh()
    # SoC deliberately never set - simulates it never having arrived yet
    # (a genuinely slow DID-polled signal, docs/06), unlike base_inputs()
    # which always sets it.
    set_all_cells(rz, 2.80)   # voltage at the midpoint of its taper window
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    rz.update_input('charge_permission_input', 0.0)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    factor = out['discharge_limit_kw'] / leaf_signals.DEFAULTS['discharge_limit_kw']
    check('with no SoC data at all, the combined factor degrades exactly to the pre-existing voltage-only '
          'behavior (SoC factor defaults to 1.0, never restricts)',
          abs(factor - 0.5) < 1e-9, f'factor={factor!r} (expected exactly 0.5, matching voltage alone)')


def test_regen_soc_factor_tapers_with_healthy_voltage():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70, soc=90.0)   # healthy voltage (full power), SoC exactly at the midpoint of the default 80%/100% window
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    factor = out['charge_limit_kw'] / leaf_signals.DEFAULTS['charge_limit_kw']
    check('SoC alone tapers regen power to exactly half at the midpoint of its window (90% between '
          '80%/100%), even with fully healthy per-cell voltage',
          abs(factor - 0.5) < 1e-9, f'factor={factor!r} (expected exactly 0.5)')
    check('status text names SoC as the binding (more restrictive) factor',
          'binding=SoC' in eng.status.get('charge_target_taper', ''), eng.status.get('charge_target_taper'))


def test_regen_voltage_still_provides_quick_cutoff_despite_healthy_soc():
    eng, rz = fresh()
    base_inputs(rz, cell_v=4.075, soc=60.0)   # voltage at the midpoint of its 4.00/4.15 window, SoC well below its own 80% full-power ceiling
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    factor = out['charge_limit_kw'] / leaf_signals.DEFAULTS['charge_limit_kw']
    check('a real voltage rise still restricts regen power even with a fully healthy SoC reading',
          abs(factor - 0.5) < 1e-9, f'factor={factor!r} (expected exactly 0.5)')
    check('status text names voltage as the binding (more restrictive) factor',
          'binding=voltage' in eng.status.get('charge_target_taper', ''), eng.status.get('charge_target_taper'))


def test_regen_full_power_when_both_soc_and_voltage_are_healthy():
    eng, rz = fresh()
    base_inputs(rz, cell_v=3.70, soc=60.0)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('full regen power when neither voltage nor SoC is anywhere near its taper window',
          out['charge_limit_kw'] == leaf_signals.DEFAULTS['charge_limit_kw'],
          f"charge_limit_kw={out['charge_limit_kw']}")


def test_regen_missing_soc_falls_back_to_voltage_only():
    eng, rz = fresh()
    set_all_cells(rz, 4.075)   # voltage at the midpoint of its regen taper window
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    rz.update_input('charge_permission_input', 0.0)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    factor = out['charge_limit_kw'] / leaf_signals.DEFAULTS['charge_limit_kw']
    check('with no SoC data at all, regen degrades exactly to the pre-existing voltage-only behavior',
          abs(factor - 0.5) < 1e-9, f'factor={factor!r} (expected exactly 0.5, matching voltage alone)')


def test_config_sanity_flags_inverted_soc_ordering():
    eng, rz = fresh()
    eng.config['discharge_power_taper']['taper_min_soc_pct'] = 25.0   # inverted vs. taper_start_soc_pct (20.0 default)
    base_inputs(rz, cell_v=3.70, soc=60.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('config-sanity check flags an inverted discharge taper SoC ordering',
          'taper_min_soc_pct' in eng.status.get('config_sanity', ''), eng.status.get('config_sanity'))

    eng2, rz2 = fresh()
    eng2.config['charge_target_taper']['regen_full_soc_pct'] = 100.0   # not < regen_min_soc_pct (100.0 default)
    base_inputs(rz2, cell_v=3.70, soc=60.0)
    eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('config-sanity check flags an inverted/equal regen taper SoC ordering',
          'regen_full_soc_pct' in eng2.status.get('config_sanity', ''), eng2.status.get('config_sanity'))


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
