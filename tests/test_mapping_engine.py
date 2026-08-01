"""Verification script for bridge/mapping_engine.py's default_ties() - run
directly (`py tests/test_mapping_engine.py`). Covers two mappings confirmed
on real hardware 2026-07-31 (user's own Leaf + this project's own bench
RZ450e pack):
- soc_correction (0x59E byte 7) = soc_pct x 2.0, i.e. raw 0-200 = 0-100% on
  the physical dash SOC display. Resolves docs/10-open-questions.md item 10
  (inherited from Leaf_BMS_Emulator as unsolved there).
- capacity_bars_raw (0x5BC, ChargeBars/CapacityBars) = capacity_pack1_ah x
  0.07, i.e. raw 0-200Ah maps to the confirmed 0-14 "full bar display"
  range (15 is a separate all-off sentinel, never part of this linear
  range).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import leaf_signals
from bridge.mapping_engine import MappingEngine, default_ties
from bridge.state import SharedState

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


def _find_tie(ties, output):
    return next((t for t in ties if t.output == output), None)


def test_default_ties_include_soc_correction():
    ties = default_ties()
    tie = _find_tie(ties, 'soc_correction')
    check('a default tie targeting soc_correction exists', tie is not None)
    check('it reads from soc_pct', tie.inputs == ['soc_pct'])
    check('it is a linear combine', tie.combine == 'linear')
    check('scale is 2.0 (confirmed: 2 raw counts per percent)', tie.params.get('scale') == 2.0)
    check('offset is 0.0 (pure linear, no shift)', tie.params.get('offset', 0.0) == 0.0)


def test_soc_correction_computes_correctly_end_to_end():
    engine = MappingEngine(default_ties())
    state = SharedState()

    for soc_pct, expected_raw in [(0.0, 0.0), (45.0, 90.0), (82.0, 164.0), (100.0, 200.0)]:
        state.update_input('soc_pct', soc_pct)
        out = engine.apply(state)
        check(f'soc_pct={soc_pct}% -> soc_correction raw={expected_raw}',
              out.get('soc_correction') == expected_raw,
              f"got {out.get('soc_correction')}")


def test_leaf_signals_default_and_range_updated():
    check('DEFAULTS[soc_correction] is within the confirmed 0-200 range (not the old invalid 241)',
          0 <= leaf_signals.DEFAULTS['soc_correction'] <= 200,
          f"DEFAULTS value = {leaf_signals.DEFAULTS['soc_correction']}")

    slider = next(s for group in leaf_signals.SLIDERS.values() for s in group if s[0] == 'soc_correction')
    _key, _label, lo, hi, _step, default = slider
    check('slider range is the confirmed 0-200 (was 0-255)', (lo, hi) == (0, 200), f'(lo, hi) = ({lo}, {hi})')
    check('slider default (90) is consistent with usable_soc/fine_soc_pct\'s own ~45% default',
          default == 90, f'default = {default}')


def test_default_ties_include_capacity_bars_raw():
    ties = default_ties()
    tie = _find_tie(ties, 'capacity_bars_raw')
    check('a default tie targeting capacity_bars_raw exists', tie is not None)
    check('it reads from capacity_pack1_ah', tie.inputs == ['capacity_pack1_ah'])
    check('it is a linear combine', tie.combine == 'linear')
    check('scale is 0.07 (confirmed: 14/200 Ah->bars)', tie.params.get('scale') == 0.07)
    check('offset is 0.0 (pure linear, no shift)', tie.params.get('offset', 0.0) == 0.0)


def test_capacity_bars_raw_computes_correctly_end_to_end():
    engine = MappingEngine(default_ties())
    state = SharedState()

    for capacity_ah, expected_raw in [(0.0, 0.0), (100.0, 7.0), (200.0, 14.0)]:
        state.update_input('capacity_pack1_ah', capacity_ah)
        out = engine.apply(state)
        got = out.get('capacity_bars_raw')
        check(f'capacity_pack1_ah={capacity_ah}Ah -> capacity_bars_raw={expected_raw}',
              got is not None and abs(got - expected_raw) < 1e-9,
              f"got {got}")


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
