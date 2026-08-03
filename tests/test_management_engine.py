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
    # Tightened (docs/13 item 6.4, 2026-08-03): was a loose `> 0.0` check - the
    # exact value is computable (cold_factor=1.0 since coldest probe 70F is
    # above charge_derate_low_start_f=50F; hot_factor=1.0 since hottest probe
    # 20F is nowhere near charge_derate_start_f=90F; c_factor=min(1.0,1.0)=1.0;
    # cell_v=3.70V default doesn't trigger the regen taper either) - full
    # power, unreduced by any feature.
    check('F1: charge NOT blocked on cold-side when coldest probe is warm, regardless of hottest probe reading',
          out2['charge_limit_kw'] == leaf_signals.DEFAULTS['charge_limit_kw'],
          f"charge_limit_kw={out2['charge_limit_kw']} (expected {leaf_signals.DEFAULTS['charge_limit_kw']}, full power)")


# ── F3: cold-side charge/regen derate ramps between block and derate-start ─
def test_f3_cold_derate_ramp():
    eng, rz = fresh()
    base_inputs(rz, temp_min=41.0)  # midpoint of 32F block / 50F full-power default window
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    factor = out['charge_limit_kw'] / leaf_signals.DEFAULTS['charge_limit_kw']
    # Tightened (docs/13 item 6.4, 2026-08-03): was a loose 0.35-0.65 range -
    # the linear ramp formula gives an exact expected factor of 0.5 at this
    # exact midpoint ((41-32)/(50-32) = 9/18 = 0.5), and nothing else in this
    # scenario reduces charge_limit_kw further (default cell_v=3.70V doesn't
    # trigger the regen taper; hottest probe stays at the base_inputs default
    # 77F, well below any hot-side derate).
    check('F3: cold-side derate ramps to EXACTLY half power at the midpoint temp (41F, between 32F/50F)',
          abs(factor - 0.5) < 1e-9, f'factor={factor!r} (expected exactly 0.5)')

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
    # Never checked before (docs/14 Part 1 TODO) - the status text was tested, the actual
    # fault_log entry never was.
    check('F4: cell_imbalance_warn fault_log entry is active',
          eng.fault_log.entries['cell_imbalance_warn']['active'] is True)

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
    base_inputs(rz, cell_v=4.35)  # above both the regen and AC emergency thresholds (4.20V default)
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
    base_inputs(rz, temp_max=200.0)  # well above the 141.8F emergency tier
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
    rz.update_input('temp_max', 77.0)
    rz.update_input('temp_min', 77.0)
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
    rz2.update_input('temp_max', 77.0)
    rz2.update_input('temp_min', 77.0)
    rz2.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('cell_data_mismatch clears once the sources agree again (auto-clear, not latched)',
          eng.fault_log.entries['cell_data_mismatch']['active'] is False)
    check('cell_data_mismatch_hard also clears',
          eng.fault_log.entries['cell_data_mismatch_hard']['active'] is False)


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
    rz.update_input('temp_max', 77.0)
    rz.update_input('temp_min', 77.0)
    rz.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)   # establish the factor=1.0 baseline

    set_all_cells(rz, 2.50)   # sudden dip well below taper_zero_v (2.60V)
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
    rz.update_input('temp_max', 77.0)
    rz.update_input('temp_min', 77.0)
    rz.update_input('current', 0.0)
    rz.update_input('charge_permission_input', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)   # establish the factor=1.0 baseline

    # 4.18V: above regen_zero_v (4.15V), so the taper's OWN hysteresis already
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
    base_inputs(rz, temp_max=141.7, temp_min=77.0)   # just BELOW 141.8F emergency
    out = eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: just below 141.8F emergency temp - no hard cut',
          out.get('relay_cut_request', 0) == 0, f"relay_cut_request={out.get('relay_cut_request')}")

    eng2, rz2 = fresh()
    base_inputs(rz2, temp_max=141.8, temp_min=77.0)   # EXACTLY at 141.8F (the check is >=)
    out2 = eng2.apply(dict(leaf_signals.DEFAULTS), rz2)
    check('boundary: EXACTLY at 141.8F emergency temp - hard cut fires (>= comparison)',
          out2.get('relay_cut_request', 0) == 3, f"relay_cut_request={out2.get('relay_cut_request')}")

    eng3, rz3 = fresh()
    base_inputs(rz3, temp_max=141.9, temp_min=77.0)   # just ABOVE 141.8F emergency
    out3 = eng3.apply(dict(leaf_signals.DEFAULTS), rz3)
    check('boundary: just above 141.8F emergency temp - hard cut fires',
          out3.get('relay_cut_request', 0) == 3, f"relay_cut_request={out3.get('relay_cut_request')}")


def test_boundary_cell_imbalance_warn_delta():
    eng, rz = fresh()
    exceptions = {'cell_01': 3.551}   # 99mV below the rest - just BELOW the 100mV warn threshold
    set_all_cells(rz, 3.650, exceptions)
    rz.update_input('temp_max', 77.0)
    rz.update_input('temp_min', 77.0)
    rz.update_input('current', 0.0)
    eng.apply(dict(leaf_signals.DEFAULTS), rz)
    check('boundary: 99mV spread does NOT trigger the 100mV warn threshold',
          eng.fault_log.entries['cell_imbalance_warn']['active'] is False)

    eng2, rz2 = fresh()
    exceptions2 = {'cell_01': 3.549}   # 101mV below the rest - just ABOVE the 100mV warn threshold
    set_all_cells(rz2, 3.650, exceptions2)
    rz2.update_input('temp_max', 77.0)
    rz2.update_input('temp_min', 77.0)
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
    rz.update_input('temp_max', 77.0)
    rz.update_input('temp_min', 77.0)
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
    rz2.update_input('temp_max', 77.0)
    rz2.update_input('temp_min', 77.0)
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


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
