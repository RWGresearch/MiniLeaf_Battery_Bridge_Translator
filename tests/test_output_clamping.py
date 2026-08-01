"""Verification script for bridge/leaf_signals.py's clamp_state() - run
directly (`py tests/test_output_clamping.py`). Confirms the 2026-07-31 fix:
frame builders bitmask-pack values (e.g. `& 0x3FF`), which WRAPS an
out-of-range value instead of saturating it - confirmed directly below that
a negative discharge_limit_kw used to decode back to 251.0kW (full power,
the opposite of the safety intent) before this fix existed. clamp_state()
is the single choke point that now guarantees every field is inside its
documented encodable range before any frame is built."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import leaf_signals

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


def test_in_range_passes_through_unchanged():
    s = dict(leaf_signals.DEFAULTS)
    out, clamped = leaf_signals.clamp_state(s)
    check('a state built entirely from DEFAULTS needs no clamping', clamped == [], f'clamped={clamped}')
    check('in-range values pass through unchanged', out['discharge_limit_kw'] == s['discharge_limit_kw'])


def test_over_max_clamps_to_documented_hi():
    s = dict(leaf_signals.DEFAULTS)
    s['discharge_limit_kw'] = 300.0   # above the documented 255.75 max
    out, clamped = leaf_signals.clamp_state(s)
    check('discharge_limit_kw=300.0 clamps down to the documented max (255.75), not left at 300',
          out['discharge_limit_kw'] == 255.75, f"got {out['discharge_limit_kw']}")
    check('the clamp event is reported', any(k == 'discharge_limit_kw' for k, _, _ in clamped))

    # Confirm the historical bug this replaces: encoding 300.0 directly with
    # the bitmask instead of clamping first silently wraps to a bogus value.
    raw_wrapped = int(round(300.0 / 0.25)) & 0x3FF
    check('documenting the OLD bug for context: unclamped 300.0kW would have '
          'wrapped to a nonsense raw value if encoded directly',
          raw_wrapped * 0.25 != 300.0, f'{raw_wrapped * 0.25} != 300.0 confirms the wraparound')


def test_negative_clamps_to_zero_not_full_power():
    s = dict(leaf_signals.DEFAULTS)
    s['discharge_limit_kw'] = -5.0   # e.g. a bad mapping tie or arithmetic edge case
    out, clamped = leaf_signals.clamp_state(s)
    check('a negative discharge_limit_kw clamps to its documented floor (0.0), not left negative',
          out['discharge_limit_kw'] == 0.0, f"got {out['discharge_limit_kw']}")

    # Confirm the historical bug: encoding -5.0 directly would have wrapped
    # to a raw value that decodes back to 251.0 kW - essentially full power,
    # the exact opposite of the safety intent.
    raw_wrapped = int(round(-5.0 / 0.25)) & 0x3FF
    decoded_kw = raw_wrapped * 0.25
    check('documenting the OLD bug: an unclamped -5.0kW would have wrapped to ~251kW (full power) '
          'if encoded directly - this is exactly why clamp_state() exists',
          decoded_kw > 200.0, f'decoded_kw={decoded_kw}')


def test_derived_gids_and_qc_capacity_can_legitimately_overflow():
    # A realistic large pack (200Ah, 400V) legitimately computes values above
    # the documented 10-bit-encodable range - not a contrived edge case.
    s = dict(leaf_signals.DEFAULTS)
    s['qc_full_wh'] = 80000.0    # documented max 51100
    s['gids'] = 900.0            # documented max 1023 - in range, sanity check
    s['qc_remain_wh'] = 90000.0  # also above the 51100 max
    out, clamped = leaf_signals.clamp_state(s)
    check('qc_full_wh (80000) clamps to the documented max (51100)',
          out['qc_full_wh'] == 51100, f"got {out['qc_full_wh']}")
    check('qc_remain_wh (90000) clamps to the documented max (51100)',
          out['qc_remain_wh'] == 51100, f"got {out['qc_remain_wh']}")
    check('gids (900, in range) is left unchanged', out['gids'] == 900.0)


def test_flags_clamp_to_0_1():
    s = dict(leaf_signals.DEFAULTS)
    s['capacity_empty'] = 2   # should never happen, but confirm the floor/ceiling holds
    out, clamped = leaf_signals.clamp_state(s)
    check('a flag value above 1 clamps down to 1', out['capacity_empty'] == 1)


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
