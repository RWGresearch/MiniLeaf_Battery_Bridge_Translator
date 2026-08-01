"""Verification script for bridge/fault_log.py - run directly
(`py tests/test_fault_log.py`). Covers the fault/event history added
2026-07-31 (user request: know if a fault/over-under condition triggered,
even if it auto-clears, with a count and a manual per-entry reset)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.fault_log import FaultLog

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


def test_rising_edge_counts_once():
    fl = FaultLog()
    fl.update('x', 'Test fault', 'soft', True, 'detail1')
    fl.update('x', 'Test fault', 'soft', True, 'detail1')
    fl.update('x', 'Test fault', 'soft', True, 'detail1')
    e = fl.entries['x']
    check('a condition held active across 3 ticks counts as ONE trigger, not three',
          e['count'] == 1, f"count={e['count']}")
    check('active flag reflects the live state', e['active'] is True)


def test_auto_clear_is_remembered():
    fl = FaultLog()
    fl.update('x', 'Test fault', 'soft', True, 'tripped')
    fl.update('x', 'Test fault', 'soft', False, '')
    e = fl.entries['x']
    check('after the condition clears, active is False but count is preserved',
          e['active'] is False and e['count'] == 1, f"active={e['active']}, count={e['count']}")
    check('last_cleared timestamp is recorded on the falling edge', e['last_cleared'] is not None)


def test_re_trigger_increments_again():
    fl = FaultLog()
    fl.update('x', 'Test fault', 'soft', True, '')
    fl.update('x', 'Test fault', 'soft', False, '')
    fl.update('x', 'Test fault', 'soft', True, '')
    check('a second independent trigger increments the count to 2',
          fl.entries['x']['count'] == 2, f"count={fl.entries['x']['count']}")


def test_manual_reset_clears_but_condition_can_retrigger():
    fl = FaultLog()
    fl.update('x', 'Test fault', 'soft', True, '')
    fl.update('x', 'Test fault', 'soft', True, '')  # still active, count stays 1
    fl.manual_reset('x')
    check('manual reset zeroes the count', fl.entries['x']['count'] == 0)
    check('manual reset clears the active flag (acknowledged)', fl.entries['x']['active'] is False)
    # condition is STILL true in reality - the very next update() call should
    # see a fresh rising edge and start counting again from 1, exactly like
    # clearing a code on a real BMS scan tool for a fault that's still present
    fl.update('x', 'Test fault', 'soft', True, '')
    check('a still-active condition immediately re-triggers after a manual reset',
          fl.entries['x']['count'] == 1, f"count={fl.entries['x']['count']}")


def test_reset_all():
    fl = FaultLog()
    fl.update('a', 'A', 'warn', True, '')
    fl.update('b', 'B', 'hard', True, '')
    fl.reset_all()
    check('reset_all zeroes every entry',
          all(e['count'] == 0 for e in fl.entries.values()))


def test_persistence_round_trip_never_restores_active():
    fl = FaultLog()
    fl.update('x', 'Test fault', 'hard', True, 'still tripped at save time')
    d = fl.to_dict()
    fl2 = FaultLog.from_dict(d)
    check('count survives a save/load round trip', fl2.entries['x']['count'] == 1)
    check('active is NEVER restored True from disk - re-evaluated live on the next tick, '
          'not assumed from a stale save',
          fl2.entries['x']['active'] is False, f"active={fl2.entries['x']['active']}")
    check('label/level survive the round trip',
          fl2.entries['x']['label'] == 'Test fault' and fl2.entries['x']['level'] == 'hard')


def test_from_dict_handles_empty():
    fl = FaultLog.from_dict(None)
    check('from_dict(None) returns an empty, usable log', fl.entries == {})
    fl2 = FaultLog.from_dict({})
    check('from_dict({}) also returns an empty log', fl2.entries == {})


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
