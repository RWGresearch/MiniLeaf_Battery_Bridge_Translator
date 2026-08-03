"""Verification script for bridge/realtime_engine.py's ingest-side validation
toggles (docs/13 items 15.14/15.15) - run directly
(`py tests/test_realtime_engine.py`), not a pytest suite, matching this
project's other test scripts.

Covers: input_validation.enabled and checksum_validation.enabled (added
2026-08-03, default ON) actually gate whether RealtimeEngine performs the
plausibility/checksum check at all, not just whether ManagementEngine
reports on it - and that disabling either clears its fault_log entry live
(docs/13 item 14.5's pattern, applied to these two new toggles).
"""
import queue
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import leaf_signals, rz450e_signals
from bridge.management_engine import ManagementEngine
from bridge.mapping_engine import MappingEngine
from bridge.realtime_engine import RealtimeEngine
from bridge.state import SharedState

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


class _FakeBus:
    """Only what RealtimeEngine's ingest side actually touches - a real
    rx_queue. No live PCAN connection needed for these tests."""
    def __init__(self):
        self.rx_queue = queue.Queue()


def fresh_engine():
    state = SharedState()
    mgmt = ManagementEngine()
    rz_bus = _FakeBus()
    leaf_bus = _FakeBus()
    eng = RealtimeEngine(state, MappingEngine(), mgmt, rz_bus, leaf_bus)
    return eng, state, mgmt, rz_bus


def _push_frame(rz_bus, arb_id, data):
    msg = types.SimpleNamespace(arbitration_id=arb_id, data=bytes(data))
    rz_bus.rx_queue.put(('rx', 'rz450e', msg))


# ── docs/13 item 15.14 ──────────────────────────────────────────────────────
def test_input_validation_toggle_lets_implausible_values_through_when_disabled():
    eng, state, mgmt, _rz_bus = fresh_engine()
    # Directly exercises _ingest_validated() - it takes an already-decoded
    # {key: value} dict, no queue/thread needed.
    eng._ingest_validated('CAN 0xTEST', {'soc_pct': 999.0})   # wildly outside PLAUSIBLE_RANGES
    check('enabled (default): an implausible value is rejected, never written to state',
          state.get_input('soc_pct') is None)

    mgmt.config['input_validation']['enabled'] = False
    eng._ingest_validated('CAN 0xTEST', {'soc_pct': 999.0})
    check('disabled: the same implausible value now passes straight through unfiltered',
          state.get_input('soc_pct') == 999.0)


def test_disabling_input_validation_clears_its_fault_log_entry_live():
    eng, state, mgmt, _rz_bus = fresh_engine()
    state.note_rejected_input('soc_pct', 999.0)
    mgmt.apply(dict(leaf_signals.DEFAULTS), state)
    check('sanity: input_validation_reject is active with a recent rejection',
          mgmt.fault_log.entries['input_validation_reject']['active'] is True)

    mgmt.config['input_validation']['enabled'] = False
    mgmt.apply(dict(leaf_signals.DEFAULTS), state)
    check('disabled: input_validation_reject immediately shows inactive, not frozen "active"',
          mgmt.fault_log.entries['input_validation_reject']['active'] is False)
    check('the status text reflects "disabled"', mgmt.status.get('input_validation') == 'disabled')


# ── docs/13 item 15.15 ──────────────────────────────────────────────────────
def test_checksum_validation_toggle_lets_corrupt_frames_through_when_disabled():
    eng, state, mgmt, rz_bus = fresh_engine()
    eng._running = True
    # d[0]/d[1] chosen so decode_020's pack_v = (d[0]<<4)|(d[1]>>4) = 0x15E =
    # 350.0V - a physically plausible value (PLAUSIBLE_RANGES['pack_v'] is
    # 0-500V), so this test isolates the checksum gate specifically and
    # doesn't get separately rejected by input_validation (still enabled by
    # default here) once decoded.
    data = [0x15, 0xE0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
    valid_checksum = rz450e_signals.toyota_sum_checksum(rz450e_signals.ID_PACK_V, data)
    corrupt = bytes(data[:7]) + bytes([(valid_checksum + 1) & 0xFF])   # deliberately wrong

    t = threading.Thread(target=eng._ingest_rz_bus, daemon=True)
    t.start()
    try:
        _push_frame(rz_bus, rz450e_signals.ID_PACK_V, corrupt)
        time.sleep(0.3)
        check('enabled (default): a checksum-corrupt 0x020 frame is rejected, pack_v not written',
              state.get_input('pack_v') is None)

        mgmt.config['checksum_validation']['enabled'] = False
        _push_frame(rz_bus, rz450e_signals.ID_PACK_V, corrupt)
        time.sleep(0.3)
        check('disabled: the same corrupt frame is now decoded anyway',
              state.get_input('pack_v') is not None)
    finally:
        eng._running = False
        t.join(timeout=1.0)


def test_disabling_checksum_validation_clears_its_fault_log_entry_live():
    eng, state, mgmt, _rz_bus = fresh_engine()
    state.note_checksum_failure(rz450e_signals.ID_PACK_V)
    mgmt.apply(dict(leaf_signals.DEFAULTS), state)
    check('sanity: checksum_reject is active with a recent failure',
          mgmt.fault_log.entries['checksum_reject']['active'] is True)

    mgmt.config['checksum_validation']['enabled'] = False
    mgmt.apply(dict(leaf_signals.DEFAULTS), state)
    check('disabled: checksum_reject immediately shows inactive, not frozen "active"',
          mgmt.fault_log.entries['checksum_reject']['active'] is False)
    check('the status text reflects "disabled"', mgmt.status.get('checksum_validation') == 'disabled')


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
