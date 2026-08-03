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


def base_inputs(rz, cell_v=3.70, temp_max=77.0, temp_min=77.0, current=0.0, soc=60.0):
    set_all_cells(rz, cell_v)
    rz.update_input('temp_max', temp_max)
    rz.update_input('temp_min', temp_min)
    rz.update_input('current', current)
    rz.update_input('soc_pct', soc)
    rz.update_input('charge_permission_input', 0.0)


# ── F1: cold-charge block must key on temp_min, not temp_max ───────────────
def test_f1_cold_block_uses_coldest_probe():
    eng, rz = fresh()
    base_inputs(rz, temp_max=70.0, temp_min=20.0)  # hottest probe warm, coldest probe well below freezing
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('F1: charge blocked when COLDEST probe is below freezing (even though hottest probe is warm)',
          out['charge_limit_kw'] == 0.0,
          f"charge_limit_kw={out['charge_limit_kw']} (expected 0.0)")

    eng2, rz2 = fresh()
    base_inputs(rz2, temp_max=20.0, temp_min=70.0)  # hottest probe frozen-cold, coldest probe warm (contrived, sanity check the other direction)
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('F1: charge NOT blocked on cold-side when coldest probe is warm, regardless of hottest probe reading',
          out2['charge_limit_kw'] > 0.0,
          f"charge_limit_kw={out2['charge_limit_kw']} (expected > 0.0)")


# ── F3: cold-side charge/regen derate ramps between block and derate-start ─
def test_f3_cold_derate_ramp():
    eng, rz = fresh()
    base_inputs(rz, temp_min=41.0)  # midpoint of 32F block / 50F full-power default window
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    factor = out['charge_limit_kw'] / leaf_signals.DEFAULTS['charge_limit_kw']
    check('F3: cold-side derate ramps to roughly half power at the midpoint temp (41F, between 32F/50F)',
          0.35 < factor < 0.65, f'factor={factor:.2f}')

    eng2, rz2 = fresh()
    base_inputs(rz2, temp_min=60.0)  # above the cold-derate window entirely
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('F3: full charge/regen power once above the cold-derate window (60F)',
          out2['charge_limit_kw'] == leaf_signals.DEFAULTS['charge_limit_kw'],
          f"charge_limit_kw={out2['charge_limit_kw']}")


# ── F4: cell-imbalance monitor warns but never cuts/derates ────────────────
def test_f4_cell_imbalance_monitor():
    eng, rz = fresh()
    exceptions = {'cell_01': 3.55}  # 100mV below the rest -> above the 50mV default warn threshold
    set_all_cells(rz, 3.65, exceptions)
    rz.update_input('temp_max', 77.0)
    rz.update_input('temp_min', 77.0)
    rz.update_input('current', 0.0)
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    status = eng.status.get('cell_imbalance_monitor', '')
    check('F4: 100mV spread trips the warning text', 'WARNING' in status, status)
    check('F4: monitor never asserts capacity_empty or relay_cut_request',
          out.get('capacity_empty', 0) == 0 and out.get('relay_cut_request', 0) == 0,
          f"capacity_empty={out.get('capacity_empty')}, relay_cut_request={out.get('relay_cut_request')}")

    eng2, rz2 = fresh()
    set_all_cells(rz2, 3.65)  # all cells identical -> 0mV spread
    rz2.update_input('temp_max', 77.0)
    rz2.update_input('temp_min', 77.0)
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
    base_inputs(rz, temp_max=140.0, temp_min=70.0)  # exactly at the old hard_stop_f, now soft-only
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('F6: at the soft discharge_hard_stop_f (140F), discharge power reaches zero but NO hard cut fires',
          out['discharge_limit_kw'] == 0.0 and out.get('relay_cut_request', 0) == 0,
          f"discharge_limit_kw={out['discharge_limit_kw']}, relay_cut_request={out.get('relay_cut_request')}")

    eng2, rz2 = fresh()
    # 142.0F - just above the 141.8F/61C emergency tier (2026-08-01 user
    # edit, was 149F/65C) - close to the real boundary, not just "clearly above"
    base_inputs(rz2, temp_max=142.0, temp_min=70.0)
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('F6: above the emergency_temp_f (141.8F/61C), a genuine hard cut fires',
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

    eng2, rz2 = fresh()
    base_inputs(rz2, current=210.0)  # above the 0x023 sensor's 204.7A saturation ceiling
    eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('F2: saturation note appears once current is at/near the sensor ceiling',
          'saturation' in eng2.status.get('overcurrent_monitor', ''), eng2.status.get('overcurrent_monitor'))


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
    base_inputs(rz, temp_max=142.0, temp_min=70.0)  # above the 141.8F/61C emergency tier
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


# ── Added 2026-08-01: the overvoltage emergency hard-cut tiers (both the
# regen-side charge_target_taper and the AC-side ac_charge_taper, split
# 2026-08-01) previously had thin/indirect fault_log coverage - only
# relay_cut_request was checked, never the fault_log entries themselves. ──
def test_overvoltage_emergency_fault_log_entries():
    eng, rz = fresh()
    base_inputs(rz, cell_v=4.35)  # above both the regen and AC emergency thresholds (4.30V default)
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


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
