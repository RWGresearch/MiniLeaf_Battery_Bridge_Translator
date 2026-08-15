"""Verification script for the 2026-08-13 NaN-validation fix (main.py Rev 68)
- run directly (`py tests/test_nan_validation.py`). Confirms the root-cause
bug: `float("nan")` does NOT raise ValueError, and `nan < x`/`nan > x` are
BOTH False in Python, so every bounds-check in this project (GUI
_set_float()-style handlers, leaf_signals.clamp_state(), every profile-load
bounds clamp) previously let a NaN sail straight through as if it were
in-range - a NaN written into a safety-tier threshold like emergency_low_v
permanently and invisibly disables that cutoff. Confirms every layer now
rejects/neutralizes a live NaN instead of accepting it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import leaf_signals
from bridge.management_engine import ManagementEngine, default_config
from bridge.mapping_engine import MappingTie

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


def test_python_nan_semantics_confirm_the_root_cause():
    # Documents WHY every bare-comparison clamp was vulnerable - not a
    # project-specific quirk, this is IEEE-754/Python float() behavior.
    nan = float('nan')
    check('float("nan") does not raise (the actual root cause)', nan != nan)
    check('nan < 5.0 is False (a bare clamp check can never catch it)', not (nan < 5.0))
    check('nan > 5.0 is False (neither direction catches it)', not (nan > 5.0))


def test_parse_finite_float_rejects_non_finite():
    for bad in ('nan', 'NaN', 'NAN', 'inf', '-inf', 'Infinity', '  nan  '):
        try:
            leaf_signals.parse_finite_float(bad)
            check(f'parse_finite_float rejects {bad!r}', False)
        except ValueError:
            check(f'parse_finite_float rejects {bad!r}', True)


def test_parse_finite_float_accepts_normal_values():
    check('parse_finite_float accepts a normal float string',
          leaf_signals.parse_finite_float('3.14') == 3.14)
    check('parse_finite_float accepts an int-valued string',
          leaf_signals.parse_finite_float('5') == 5.0)
    check('parse_finite_float still rejects garbage text (unchanged ValueError behavior)',
          _raises_value_error(lambda: leaf_signals.parse_finite_float('abc')))


def _raises_value_error(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


def test_clamp_state_backstop_catches_nan():
    s = dict(leaf_signals.DEFAULTS)
    s['discharge_limit_kw'] = float('nan')
    out, clamped = leaf_signals.clamp_state(s)
    result = out['discharge_limit_kw']
    lo = leaf_signals.RANGES['discharge_limit_kw'][0]
    check('clamp_state() no longer lets a NaN through unclamped (result is finite)',
          result == result, f'got {result}')   # NaN != NaN is the standard finiteness test
    check('a non-finite value clamps to the documented lower bound',
          result == lo, f'got {result}, expected lo={lo}')
    check('the NaN clamp event is reported (visible, not silently absorbed)',
          any(k == 'discharge_limit_kw' for k, _, _ in clamped))


def test_management_engine_from_dict_drops_nan_keeps_default():
    default_val = default_config()['low_voltage_cutoff']['emergency_low_v']
    me = ManagementEngine.from_dict({'low_voltage_cutoff': {'emergency_low_v': float('nan')}})
    got = me.config['low_voltage_cutoff']['emergency_low_v']
    check('a NaN emergency_low_v from a corrupted profile.json is dropped, safe default kept',
          got == default_val, f'got {got}, default is {default_val}')


def test_mapping_tie_from_dict_coerces_non_finite_scale_offset():
    tie = MappingTie.from_dict({'inputs': ['soc_pct'], 'combine': 'linear', 'output': 'x',
                                 'params': {'scale': float('nan'), 'offset': float('inf')}})
    check('a NaN tie scale is coerced to the safe default (1.0)', tie.params['scale'] == 1.0)
    check('an infinite tie offset is coerced to the safe default (0.0)', tie.params['offset'] == 0.0)


def test_mapping_tie_from_dict_preserves_valid_scale_offset():
    tie = MappingTie.from_dict({'inputs': ['soc_pct'], 'combine': 'linear', 'output': 'x',
                                 'params': {'scale': 2.5, 'offset': -1.0}})
    check('a legitimate scale/offset is left untouched',
          tie.params['scale'] == 2.5 and tie.params['offset'] == -1.0)


def test_ac_charger_temp_thresholds_have_ordering_sanity_checks():
    from bridge.management_engine import _check_config_sanity
    cfg = default_config()
    cold_violations = _check_config_sanity(
        cfg, {'ac_low_block_c': 10.0, 'ac_derate_low_start_c': 5.0})
    check('an inverted AC-charger cold-side pair is now flagged (was silent before Rev 68)',
          any('ac_low_block_c' in v for v in cold_violations))
    hot_violations = _check_config_sanity(
        cfg, {'ac_derate_start_c': 50.0, 'ac_hard_stop_c': 40.0})
    check('an inverted AC-charger hot-side pair is now flagged (was silent before Rev 68)',
          any('ac_derate_start_c' in v for v in hot_violations))
    ok_violations = _check_config_sanity(
        cfg, {'ac_low_block_c': 0.0, 'ac_derate_low_start_c': 10.0,
              'ac_derate_start_c': 32.0, 'ac_hard_stop_c': 45.0})
    check('the shipped default AC-charger temp thresholds pass both new checks',
          not any('ac_low_block_c' in v or 'ac_derate_start_c' in v for v in ok_violations))


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
