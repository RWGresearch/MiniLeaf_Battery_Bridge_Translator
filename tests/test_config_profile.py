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


# ── vehicle profile-load validation (added 2026-08-08, docs/16 audit): the
# two capacity-formula fields (usable_capacity_kwh/nameplate_capacity_ah) get
# the same bounds-clamp treatment every other profile-loaded numeric field
# already has - previously state.vehicle had ZERO profile-load validation
# at all (a real, pre-existing gap this change also closes). qc_max_soc_pct
# moved OUT of state.vehicle the same day (user directive: "the 80% QC needs
# to be on the charge emulation") into state.charge_emulation instead - it
# gets the pre-existing _apply_charge_emulation()/CHARGE_EMULATION_BOUNDS
# clamp path automatically, tested separately below. ───────────────────────
def test_vehicle_profile_load_clamps_out_of_bounds_capacity_fields():
    from bridge.mapping_engine import VEHICLE_FIELD_BOUNDS
    state = SharedState()
    config_profile._apply_vehicle(state, {'usable_capacity_kwh': 9999.0, 'nameplate_capacity_ah': -5.0})
    check('a wildly out-of-bounds usable_capacity_kwh from profile.json is clamped to its max',
          state.vehicle['usable_capacity_kwh'] == VEHICLE_FIELD_BOUNDS['usable_capacity_kwh'][1],
          f"got {state.vehicle['usable_capacity_kwh']}")
    check('a negative nameplate_capacity_ah from profile.json is clamped to its min (0.0)',
          state.vehicle['nameplate_capacity_ah'] == VEHICLE_FIELD_BOUNDS['nameplate_capacity_ah'][0],
          f"got {state.vehicle['nameplate_capacity_ah']}")


def test_vehicle_profile_load_keeps_a_valid_in_range_edit():
    state = SharedState()
    config_profile._apply_vehicle(state, {'usable_capacity_kwh': 60.0, 'nameplate_capacity_ah': 195.0})
    check('a legitimate, in-range usable_capacity_kwh edit is NOT altered by the clamp',
          state.vehicle['usable_capacity_kwh'] == 60.0, f"got {state.vehicle['usable_capacity_kwh']}")
    check('a legitimate, in-range nameplate_capacity_ah edit is NOT altered by the clamp',
          state.vehicle['nameplate_capacity_ah'] == 195.0, f"got {state.vehicle['nameplate_capacity_ah']}")


def test_vehicle_profile_load_survives_non_numeric_corruption():
    state = SharedState()
    default = dict(state.vehicle)
    config_profile._apply_vehicle(state, {'usable_capacity_kwh': 'not-a-number'})
    check('a non-numeric corrupted usable_capacity_kwh falls back to the safe default, not garbage',
          state.vehicle['usable_capacity_kwh'] == default['usable_capacity_kwh'],
          f"got {state.vehicle['usable_capacity_kwh']}")


def test_vehicle_profile_load_car_gen_fields_unclamped():
    # car_gen/battery_gen/battery_kwh are enum-like/int selections, not
    # continuous values - they deliberately keep the original unclamped
    # dict.update() path (no VEHICLE_FIELD_BOUNDS entry for them at all).
    state = SharedState()
    config_profile._apply_vehicle(state, {'car_gen': 'AZE0', 'battery_kwh': 62})
    check('car_gen still applies via the unclamped path', state.vehicle['car_gen'] == 'AZE0')
    check('battery_kwh still applies via the unclamped path', state.vehicle['battery_kwh'] == 62)


def test_qc_max_soc_pct_profile_load_clamps_out_of_bounds():
    # qc_max_soc_pct lives in state.charge_emulation (leaf_signals.
    # CHARGE_SLIDERS), not state.vehicle - uses the pre-existing
    # _apply_charge_emulation()/CHARGE_EMULATION_BOUNDS path, same as
    # ac_min_kw/ac_max_kw/dc_min_kw/dc_max_kw.
    from bridge import leaf_signals
    state = SharedState()
    config_profile._apply_charge_emulation(state, {'qc_max_soc_pct': 150.0})
    lo, hi = leaf_signals.CHARGE_EMULATION_BOUNDS['qc_max_soc_pct']
    check('an out-of-bounds qc_max_soc_pct from profile.json is clamped to its max',
          state.charge_emulation['qc_max_soc_pct'] == hi, f"got {state.charge_emulation['qc_max_soc_pct']}")


def test_vehicle_round_trips_through_save_and_load():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'profile.json')
        state = SharedState()
        state.vehicle['usable_capacity_kwh'] = 55.0
        state.vehicle['nameplate_capacity_ah'] = 198.0
        state.charge_emulation['qc_max_soc_pct'] = 90.0
        from bridge.mapping_engine import MappingEngine
        from bridge.management_engine import ManagementEngine as ME
        config_profile.save_profile(state, MappingEngine(), ME(), path=path)

        loaded_state = SharedState()
        profile = config_profile.load_profile(path=path)
        config_profile.apply_profile(profile, loaded_state)
        check('usable_capacity_kwh round-trips through save -> load unchanged',
              loaded_state.vehicle['usable_capacity_kwh'] == 55.0,
              f"got {loaded_state.vehicle['usable_capacity_kwh']}")
        check('nameplate_capacity_ah round-trips through save -> load unchanged',
              loaded_state.vehicle['nameplate_capacity_ah'] == 198.0,
              f"got {loaded_state.vehicle['nameplate_capacity_ah']}")
        check('qc_max_soc_pct round-trips through save -> load unchanged',
              loaded_state.charge_emulation['qc_max_soc_pct'] == 90.0,
              f"got {loaded_state.charge_emulation['qc_max_soc_pct']}")


def test_engine_timing_round_trips_through_save_and_load():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'profile.json')
        state = SharedState()
        state.engine_timing['did_temp_fresh_window_s'] = 12.0
        state.engine_timing['bus_silence_timeout_s'] = 45.0
        from bridge.mapping_engine import MappingEngine
        from bridge.management_engine import ManagementEngine as ME
        config_profile.save_profile(state, MappingEngine(), ME(), path=path)

        loaded_state = SharedState()
        profile = config_profile.load_profile(path=path)
        config_profile.apply_profile(profile, loaded_state)
        check('did_temp_fresh_window_s round-trips through save -> load unchanged',
              loaded_state.engine_timing['did_temp_fresh_window_s'] == 12.0,
              f"got {loaded_state.engine_timing['did_temp_fresh_window_s']}")
        check('bus_silence_timeout_s round-trips through save -> load unchanged',
              loaded_state.engine_timing['bus_silence_timeout_s'] == 45.0,
              f"got {loaded_state.engine_timing['bus_silence_timeout_s']}")


def test_engine_timing_clamps_out_of_bounds_value_from_profile():
    from bridge import leaf_signals
    state = SharedState()
    lo, hi = leaf_signals.ENGINE_TIMING_BOUNDS['did_response_timeout_s']
    profile = {'engine_timing': {'did_response_timeout_s': hi + 100.0}}
    config_profile.apply_profile(profile, state)
    check('a wildly out-of-bounds engine_timing value from profile.json is clamped, '
          'matching what EngineTimingPanel itself would allow',
          state.engine_timing['did_response_timeout_s'] == hi,
          f"got {state.engine_timing['did_response_timeout_s']}")


def test_engine_timing_missing_from_profile_keeps_code_defaults():
    """A profile saved before this feature existed has no 'engine_timing'
    key at all - every field must stay at its code default, not error or
    silently zero out."""
    state = SharedState()
    default_bus_silence = state.engine_timing['bus_silence_timeout_s']
    config_profile.apply_profile({'vehicle': {}, 'charge_emulation': {}}, state)
    check('engine_timing keeps its code default when the profile has no engine_timing section at all',
          state.engine_timing['bus_silence_timeout_s'] == default_bus_silence)


def test_qc_max_soc_pct_migrates_from_old_vehicle_section():
    # One-time migration (added 2026-08-08, same day qc_max_soc_pct moved
    # from 'vehicle' to 'charge_emulation') - a profile saved during the
    # brief window it lived in 'vehicle' must not silently lose a real
    # tuned value, same precedent as the pre-existing ac_zero_v -> ac_min_v
    # migration.
    state = SharedState()
    profile = {'vehicle': {'car_gen': 'ZE1', 'battery_gen': 'ZE1', 'battery_kwh': 40,
                            'qc_max_soc_pct': 65.0},
               'charge_emulation': {}}
    config_profile.apply_profile(profile, state)
    check('a legacy vehicle.qc_max_soc_pct value migrates into charge_emulation.qc_max_soc_pct',
          state.charge_emulation['qc_max_soc_pct'] == 65.0,
          f"got {state.charge_emulation['qc_max_soc_pct']}")
    check('the legacy key does not also stick around in state.vehicle',
          'qc_max_soc_pct' not in state.vehicle)


def test_qc_max_soc_pct_migration_does_not_override_an_explicit_new_value():
    state = SharedState()
    profile = {'vehicle': {'qc_max_soc_pct': 65.0},   # stale legacy value
               'charge_emulation': {'qc_max_soc_pct': 70.0}}   # genuinely re-saved value
    config_profile.apply_profile(profile, state)
    check('an explicitly-saved charge_emulation.qc_max_soc_pct wins over the stale legacy vehicle one',
          state.charge_emulation['qc_max_soc_pct'] == 70.0,
          f"got {state.charge_emulation['qc_max_soc_pct']}")


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
