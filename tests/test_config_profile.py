"""Verification script for config_profile.py's load-time validation
(docs/13 items 13.2, 13.3, 13.9) - run directly (`py tests/test_config_profile.py`).

Live CAN/DID data has always been plausibility-checked before it reaches
SharedState. Two load-from-disk paths did NOT get the same treatment until
2026-08-03: the last-known-good cache (raw JSON, no validation) and the
management/charge-emulation config in profile.json (no bounds clamp at all,
unlike the GUI which clamps every keystroke). Both are fixed here.
"""
import sys
import tempfile
import os
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import config_profile
from bridge.management_engine import ManagementEngine, FEATURE_FIELD_BOUNDS
from bridge.state import SharedState

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


# ── 13.2: last_known_good.json is validated on load ─────────────────────────
def test_last_known_good_rejects_implausible_cached_values():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'last_known_good.json')
        with open(path, 'w') as f:
            json.dump({'pack_v': 355.0, 'cell_01': 999.0, 'current': 5.0}, f)
        valid, rejected = config_profile.load_last_known_good(path)
        check('a plausible cached value (pack_v) is kept', valid.get('pack_v') == 355.0)
        check('a plausible cached value (current) is kept', valid.get('current') == 5.0)
        check('an implausible cached value (cell_01=999V) is rejected, not silently loaded',
              'cell_01' not in valid and 'cell_01' in rejected)


def test_last_known_good_survives_corrupted_non_numeric_value():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'last_known_good.json')
        with open(path, 'w') as f:
            json.dump({'pack_v': 355.0, 'cell_02': 'garbage'}, f)
        valid, rejected = config_profile.load_last_known_good(path)
        check('a non-numeric corrupted value does not crash the loader',
              True)  # reaching this line at all is the assertion
        check('the corrupted non-numeric value is rejected, not loaded',
              'cell_02' not in valid and 'cell_02' in rejected)
        check('the rest of the file still loads fine', valid.get('pack_v') == 355.0)


def test_last_known_good_missing_file_returns_empty():
    valid, rejected = config_profile.load_last_known_good('/nonexistent/path/last_known_good.json')
    check('a missing cache file returns an empty valid dict, not an error', valid == {})
    check('a missing cache file returns an empty rejected dict too', rejected == {})


def test_seeded_rejected_cache_never_reaches_get_input():
    state = SharedState()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'last_known_good.json')
        with open(path, 'w') as f:
            json.dump({'cell_03': -50.0}, f)   # implausible
        valid, _rejected = config_profile.load_last_known_good(path)
        state.seed_last_known_good(valid)
        check('an implausible cached value never reaches SharedState.get_input() at all',
              state.get_input('cell_03') is None)


# ── 13.3/13.9: profile.json thresholds are clamped on load, matching the GUI
def test_management_from_dict_clamps_out_of_bounds_threshold():
    loaded = {'low_voltage_cutoff': {'emergency_low_v': -50.0}}
    mgmt = ManagementEngine.from_dict(loaded)
    lo, hi = FEATURE_FIELD_BOUNDS[('low_voltage_cutoff', 'emergency_low_v')]
    check('a wildly out-of-bounds threshold from a hand-edited/corrupted profile.json is '
          'clamped to the same bounds the GUI enforces, not loaded as-is',
          mgmt.config['low_voltage_cutoff']['emergency_low_v'] == lo,
          f"got {mgmt.config['low_voltage_cutoff']['emergency_low_v']}")


def test_management_from_dict_survives_non_numeric_corruption():
    loaded = {'low_voltage_cutoff': {'emergency_low_v': 'not-a-number'}}
    mgmt = ManagementEngine.from_dict(loaded)
    default_val = ManagementEngine().config['low_voltage_cutoff']['emergency_low_v']
    check('a non-numeric corrupted threshold value does not crash loading',
          True)
    check('a non-numeric corrupted threshold falls back to the safe default, not garbage',
          mgmt.config['low_voltage_cutoff']['emergency_low_v'] == default_val)


def test_management_from_dict_keeps_a_valid_in_range_edit():
    loaded = {'low_voltage_cutoff': {'emergency_low_v': 2.75}}
    mgmt = ManagementEngine.from_dict(loaded)
    check('a legitimate, in-range user edit is NOT altered by the new clamp',
          mgmt.config['low_voltage_cutoff']['emergency_low_v'] == 2.75)


def test_charge_emulation_profile_load_clamps_out_of_bounds():
    state = SharedState()
    config_profile._apply_charge_emulation(state, {'ac_full_v': 999.0})
    lo, hi = __import__('bridge.leaf_signals', fromlist=['x']).CHARGE_EMULATION_BOUNDS['ac_full_v']
    check('a wildly out-of-bounds AC-charger threshold from profile.json is clamped, matching '
          'what ChargeEmulationPanel itself would allow',
          state.charge_emulation['ac_full_v'] == hi, f"got {state.charge_emulation['ac_full_v']}")


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
