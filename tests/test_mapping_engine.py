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
from bridge.mapping_engine import MappingEngine, default_ties, derive_capacity_outputs, NAMEPLATE_CAPACITY_AH
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


# ── Management-exclusive safety flags must never be selectable mapping
# targets (docs/13 item 16.3, added 2026-08-04) ─────────────────────────────
def test_management_exclusive_keys_are_not_mapping_targets():
    out_keys = {s['key'] for s in leaf_signals.OUTPUT_SIGNALS}
    for key in leaf_signals.MANAGEMENT_EXCLUSIVE_KEYS:
        check(f'{key} is NOT in OUTPUT_SIGNALS (not a selectable Signal Mapping target)',
              key not in out_keys)


def test_management_exclusive_keys_still_have_defaults_and_ranges():
    """Excluding these from OUTPUT_SIGNALS must not remove them from
    DEFAULTS/RANGES - both are still needed for leaf_signals.DEFAULTS
    seeding, output clamping, and the Dashboard's direct SLIDERS/CHECKS-
    based display (neither of which goes through OUTPUT_SIGNALS)."""
    for key in leaf_signals.MANAGEMENT_EXCLUSIVE_KEYS:
        check(f'{key} still has a DEFAULTS entry', key in leaf_signals.DEFAULTS)
        check(f'{key} still has a RANGES entry (for clamp_state())', key in leaf_signals.RANGES)


# ── GIDS / QC capacity formula fix (added 2026-08-08, docs/16 parameter-
# clamping audit finding): the OLD formula computed GIDs from gross pack
# capacity (capacity_ah x live pack_v), overstating GIDs for any pack with a
# real top/bottom usable-capacity buffer. Worked numbers below are the user's
# own by-hand calculation for their real 72kWh-gross/64kWh-usable pack: 64kWh
# usable x 94% SOH = 60.16kWh = 752 GIDs at 100% SOC - this is the target the
# fix must reproduce exactly. capacity_ah=188.94 = 0.94 x NAMEPLATE_CAPACITY_AH
# (201.00), giving soh_pct=94.0 via the same soh_percent formula soh_pct
# itself uses. ──────────────────────────────────────────────────────────────
def test_gids_qc_capacity_uses_usable_capacity_not_gross():
    state = SharedState()
    state.vehicle['usable_capacity_kwh'] = 64.0
    state.update_input('capacity_pack1_ah', 0.94 * NAMEPLATE_CAPACITY_AH)   # 94% SOH

    state.update_input('soc_pct', 100.0)
    out = derive_capacity_outputs(state)
    check('gids at 100% SOC / 94% SOH matches the user\'s own worked calculation (752)',
          abs(out['gids'] - 752.0) < 1e-6, f"gids={out['gids']}")
    check('qc_full_wh is the 80%-SOC-ceiling energy (60.16kWh x 0.80 = 48128Wh), not the full 60.16kWh',
          abs(out['qc_full_wh'] - 48128.0) < 1e-6, f"qc_full_wh={out['qc_full_wh']}")
    check('qc_remain_wh is 0 once already at/above the QC ceiling (100% SOC is well past 80%)',
          out['qc_remain_wh'] == 0.0, f"qc_remain_wh={out['qc_remain_wh']}")

    state.update_input('soc_pct', 50.0)
    out = derive_capacity_outputs(state)
    check('gids at 50% SOC is exactly half of the 100%-SOC value (376)',
          abs(out['gids'] - 376.0) < 1e-6, f"gids={out['gids']}")
    check('qc_full_wh is unchanged at 50% SOC (SOH-only, not SOC-dependent)',
          abs(out['qc_full_wh'] - 48128.0) < 1e-6, f"qc_full_wh={out['qc_full_wh']}")
    check('qc_remain_wh reflects the real gap below the QC ceiling at 50% SOC (18048Wh)',
          abs(out['qc_remain_wh'] - 18048.0) < 1e-6, f"qc_remain_wh={out['qc_remain_wh']}")


def test_gids_qc_capacity_defaults_without_explicit_vehicle_config():
    # Backward-compat / default-value case: don't touch state.vehicle at all,
    # confirm the shipped defaults (usable_capacity_kwh=64.0,
    # qc_max_soc_pct=80.0) reproduce the same numbers as the explicit-config
    # test above.
    state = SharedState()
    state.update_input('capacity_pack1_ah', 0.94 * NAMEPLATE_CAPACITY_AH)
    state.update_input('soc_pct', 100.0)
    out = derive_capacity_outputs(state)
    check('default usable_capacity_kwh (64.0) reproduces the same 752 gids with no explicit config',
          abs(out['gids'] - 752.0) < 1e-6, f"gids={out['gids']}")
    check('default qc_max_soc_pct (80.0) reproduces the same 48128Wh qc_full_wh with no explicit config',
          abs(out['qc_full_wh'] - 48128.0) < 1e-6, f"qc_full_wh={out['qc_full_wh']}")


# ── nameplate_capacity_ah / qc_max_soc_pct configurability (added 2026-08-08
# follow-up, user directive: "soh_fraction could be configured" + "the 80%
# QC needs to be on the charge emulation") - confirms both are genuinely
# LIVE config, not just present-with-a-default-that-happens-to-match. ──────
def test_nameplate_capacity_ah_is_configurable():
    state = SharedState()
    state.update_input('capacity_pack1_ah', 180.0)
    state.update_input('soc_pct', 100.0)

    state.vehicle['nameplate_capacity_ah'] = 201.00
    out_default_nameplate = derive_capacity_outputs(state)

    state.vehicle['nameplate_capacity_ah'] = 180.0   # same as measured capacity -> SOH reads 100%
    out_lowered_nameplate = derive_capacity_outputs(state)
    check('lowering nameplate_capacity_ah raises the computed SOH fraction, raising gids',
          out_lowered_nameplate['gids'] > out_default_nameplate['gids'],
          f"default={out_default_nameplate['gids']}, lowered={out_lowered_nameplate['gids']}")
    check('nameplate_capacity_ah=measured capacity_ah -> SOH reads exactly 100%, usable_capacity_kwh unscaled',
          abs(out_lowered_nameplate['gids'] - (state.vehicle['usable_capacity_kwh'] * 1000.0 / 80.0)) < 1e-6,
          f"gids={out_lowered_nameplate['gids']}")


def test_qc_max_soc_pct_lives_in_charge_emulation_and_is_configurable():
    state = SharedState()
    state.update_input('capacity_pack1_ah', NAMEPLATE_CAPACITY_AH)   # 100% SOH
    state.update_input('soc_pct', 50.0)

    check('qc_max_soc_pct defaults into state.charge_emulation (leaf_signals.CHARGE_SLIDERS), not state.vehicle',
          'qc_max_soc_pct' in state.charge_emulation and 'qc_max_soc_pct' not in state.vehicle)

    state.charge_emulation['qc_max_soc_pct'] = 50.0   # QC ceiling exactly at the current SoC
    out = derive_capacity_outputs(state)
    check('lowering qc_max_soc_pct to match current SoC makes qc_remain_wh read exactly 0',
          out['qc_remain_wh'] == 0.0, f"qc_remain_wh={out['qc_remain_wh']}")

    state.charge_emulation['qc_max_soc_pct'] = 90.0   # QC ceiling well above current SoC
    out2 = derive_capacity_outputs(state)
    check('raising qc_max_soc_pct above current SoC makes qc_remain_wh positive again',
          out2['qc_remain_wh'] > 0.0, f"qc_remain_wh={out2['qc_remain_wh']}")


def test_gids_qc_capacity_missing_inputs_returns_empty():
    state = SharedState()
    check('no soc_pct/capacity_ah at all -> derive_capacity_outputs() returns {}',
          derive_capacity_outputs(state) == {})
    state.update_input('soc_pct', 50.0)
    check('soc_pct alone, no capacity_ah -> still returns {} (capacity_ah is required)',
          derive_capacity_outputs(state) == {})


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
