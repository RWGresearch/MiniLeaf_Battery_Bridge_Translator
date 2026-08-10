"""Config profile save/load (mapping ties, management thresholds, generated-
signal flags, vehicle selection) and the separate last-known-good data cache.

Two distinct files, per docs/06-realtime-engine-and-watchdog.md: the profile
is *what to do with data* (deliberately edited/saved by the user); the cache
is *the data itself* (persisted automatically, just to bridge the startup gap
before live data arrives). The profile format is also the schema seed for
the future STM32 export (docs/09-stm32-export-format.md).
"""
import json
import os

from bridge.fault_log import FaultLog
from bridge.mapping_engine import MappingEngine
from bridge.management_engine import ManagementEngine
from bridge import rz450e_signals

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config')
DEFAULT_PROFILE_PATH = os.path.join(CONFIG_DIR, 'profile.json')
LAST_KNOWN_GOOD_PATH = os.path.join(CONFIG_DIR, 'last_known_good.json')
FAULT_LOG_PATH = os.path.join(CONFIG_DIR, 'fault_log.json')
# .trc capture default save location (added 2026-08-06, user request: "change
# the log location to the log folder, which I've created") - a sibling of
# config/, not inside it, since these are per-session test captures, not a
# deliberately-tracked running backup like config/*.json.
LOGS_DIR = os.path.join(os.path.dirname(CONFIG_DIR), 'logs')


def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _ensure_logs_dir():
    os.makedirs(LOGS_DIR, exist_ok=True)


def build_profile_dict(state, mapping_engine, management_engine, profile_name=None):
    """The full editable-settings snapshot (vehicle spec, mapping ties,
    management thresholds, generated-signal flags, charge emulation) - the
    same dict `save_profile` writes to config/profile.json, factored out so
    other callers (e.g. gui/app.py's log_output companion file) can get an
    identical settings dump without also writing/overwriting the saved
    profile on disk."""
    return {
        'profile_name': profile_name or 'profile',
        'vehicle': dict(state.vehicle),
        'mappings': mapping_engine.to_list(),
        'management_features': management_engine.to_dict(),
        'generated_signals': dict(state.generated_enabled),
        'charge_emulation': dict(state.charge_emulation),
    }


def save_profile(state, mapping_engine, management_engine, path=DEFAULT_PROFILE_PATH):
    _ensure_dir()
    profile = build_profile_dict(state, mapping_engine, management_engine,
                                  profile_name=os.path.splitext(os.path.basename(path))[0])
    with open(path, 'w') as f:
        json.dump(profile, f, indent=2)
    return path


def load_profile(path=DEFAULT_PROFILE_PATH):
    if not os.path.exists(path):
        return None
    with open(path, 'r') as f:
        profile = json.load(f)
    return profile


def apply_profile(profile, state):
    """Returns (mapping_engine, management_engine) built from a loaded
    profile dict, and updates `state`'s vehicle/generated-signal settings
    in place."""
    if not profile:
        return MappingEngine(), ManagementEngine()
    vehicle_loaded = dict(profile.get('vehicle', {}))
    charge_emu_loaded = dict(profile.get('charge_emulation', {}))
    # One-time field migration (added 2026-08-08, same day as the field's own
    # move): qc_max_soc_pct lived in the 'vehicle' section for about a day
    # before moving to 'charge_emulation' (user directive: "the 80% QC needs
    # to be on the charge emulation"). Same pattern as the pre-existing
    # ac_zero_v -> ac_min_v migration below - carry the user's real tuned
    # value across from the old section (only if the new section doesn't
    # already have an explicitly saved value) so a profile saved during that
    # brief window doesn't silently lose it, rather than assuming the
    # default was never touched.
    if 'qc_max_soc_pct' in vehicle_loaded and 'qc_max_soc_pct' not in charge_emu_loaded:
        charge_emu_loaded['qc_max_soc_pct'] = vehicle_loaded.pop('qc_max_soc_pct')
    _apply_vehicle(state, vehicle_loaded)
    state.generated_enabled.update(profile.get('generated_signals', {}))
    _apply_charge_emulation(state, charge_emu_loaded)
    mapping = MappingEngine.from_list(_migrate_temp_mapping_ties(profile.get('mappings', [])))
    management = ManagementEngine.from_dict(profile.get('management_features', {}))
    return mapping, management


def _migrate_temp_mapping_ties(ties):
    """One-time migration (added 2026-08-09, same day RZ450e temp decoding
    switched from °F to °C - bridge/rz450e_signals.py's decode_temp_msg()/
    decode_temp_minmax()). The two default mapping ties that read temp_max
    (batt_temp_c, temp_segment_pct) used to convert °F->°C/°F->% themselves;
    now that temp_max ARRIVES already in °C, an untouched OLD saved tie would
    apply that same F->C-shaped formula a SECOND time to an already-Celsius
    value, silently producing garbage output - a real correctness bug, not
    just a cosmetic label mismatch. Only rewrites a tie whose params exactly
    match the OLD pre-conversion default (the fingerprint of "never
    hand-edited away from the shipped default") - a genuinely customized
    tie is left alone, since this project has no way to know what a hand-
    tuned formula was meant to mean in the new unit."""
    import math
    migrated = []
    for tie in ties:
        t = dict(tie)
        if (t.get('inputs') == ['temp_max'] and t.get('combine') == 'linear'
                and t.get('output') == 'batt_temp_c'):
            params = t.get('params', {})
            if (math.isclose(params.get('scale', 0), 5.0 / 9.0, rel_tol=1e-9)
                    and math.isclose(params.get('offset', 0), -32.0 * 5.0 / 9.0, rel_tol=1e-9)):
                t = dict(t)
                t['params'] = {'scale': 1.0, 'offset': 0.0}
                t['name'] = 'temp_max (°C) -> batt_temp_c (°C) (identity - both already °C as of 2026-08-09)'
        elif (t.get('inputs') == ['temp_max'] and t.get('combine') == 'linear'
                and t.get('output') == 'temp_segment_pct'):
            params = t.get('params', {})
            if (math.isclose(params.get('scale', 0), 100.0 / 108.0, rel_tol=1e-9)
                    and math.isclose(params.get('offset', 0), -32.0 * 100.0 / 108.0, rel_tol=1e-9)):
                t = dict(t)
                t['params'] = {'scale': 100.0 / 60.0, 'offset': 0.0}
        migrated.append(t)
    return migrated


def _apply_vehicle(state, loaded):
    """Applies a loaded profile's 'vehicle' section (added 2026-08-08, docs/16
    audit). `car_gen`/`battery_gen`/`battery_kwh` are enum-like/int selections
    from a fixed GUI Combobox list, not continuous values, so they keep the
    original unclamped `dict.update()` path. `usable_capacity_kwh`/
    `nameplate_capacity_ah` are continuous capacity-formula inputs
    (mapping_engine.derive_capacity_outputs()) - clamped to
    mapping_engine.VEHICLE_FIELD_BOUNDS before applying, same reasoning as
    `_apply_charge_emulation()` below (a hand-edited or corrupted
    profile.json must not be able to set an arbitrary value just because it
    bypasses the GUI's own clamp) - this closes a real, pre-existing gap:
    `state.vehicle` had zero profile-load validation at all before this."""
    from bridge.mapping_engine import VEHICLE_FIELD_BOUNDS
    state.vehicle.update({k: v for k, v in loaded.items() if k not in VEHICLE_FIELD_BOUNDS})
    for key, bounds in VEHICLE_FIELD_BOUNDS.items():
        if key not in loaded:
            continue
        try:
            value = max(bounds[0], min(bounds[1], float(loaded[key])))
        except (TypeError, ValueError):
            continue
        state.vehicle[key] = value


def _apply_charge_emulation(state, loaded):
    """Clamps every numeric field to leaf_signals.CHARGE_EMULATION_BOUNDS
    before applying it (docs/13 items 13.3/13.9, fixed 2026-08-03) - same
    reasoning as ManagementEngine.from_dict()'s bounds clamp: a hand-edited
    or corrupted profile.json must not be able to set an AC-charger/regen
    threshold to an arbitrary value just because it bypasses the GUI. A
    value that can't be coerced to a number is dropped, leaving the
    existing default in place, rather than written through as-is."""
    from bridge import leaf_signals
    # One-time field migration (added 2026-08-06): ac_zero_v was renamed to
    # ac_min_v when the AC taper was reworked to hold at a configurable
    # minimum instead of driving to true zero (see leaf_signals.py's
    # CHARGE_SLIDERS comment) - an older saved profile still has the old key
    # name. Copy its real tuned value across (only if the new key isn't
    # already present, so a profile saved post-migration is never
    # overwritten) before the normal bounds-clamp loop below runs, so the
    # user's actual tuned voltage survives instead of silently reverting to
    # the new default.
    if 'ac_zero_v' in loaded and 'ac_min_v' not in loaded:
        loaded = dict(loaded)
        loaded['ac_min_v'] = loaded.pop('ac_zero_v')
    for key, value in loaded.items():
        if key not in state.charge_emulation:
            continue
        bounds = leaf_signals.CHARGE_EMULATION_BOUNDS.get(key)
        if bounds is not None:
            try:
                value = max(bounds[0], min(bounds[1], float(value)))
            except (TypeError, ValueError):
                continue
        state.charge_emulation[key] = value


def save_last_known_good(state, path=LAST_KNOWN_GOOD_PATH):
    _ensure_dir()
    with open(path, 'w') as f:
        json.dump(state.last_known_good, f, indent=2)
    return path


def load_last_known_good(path=LAST_KNOWN_GOOD_PATH):
    """Returns (valid, rejected) - docs/13 item 13.2 (fixed 2026-08-03): live
    CAN/DID data is plausibility-checked (rz450e_signals.validate_inputs())
    before it ever reaches SharedState, but this disk-persisted cache
    previously was NOT - a corrupted file, a hand edit, or a copy-pasted
    cache from a different pack would inject an unvalidated number straight
    into every safety cutoff/taper calculation via SharedState.get_input()'s
    fallback. Now runs through the exact same check as live data. `rejected`
    is for the caller to log - a rejected key is just dropped, same as a
    rejected live sample."""
    if not os.path.exists(path):
        return {}, {}
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}, {}
    if not isinstance(raw, dict):
        return {}, {}
    return rz450e_signals.validate_inputs(raw)


def save_fault_log(management_engine, path=FAULT_LOG_PATH):
    """Fault/event history (bridge/fault_log.py) is DATA about what happened
    (like last_known_good), not a deliberately-edited setting - kept in its
    own file, separate from profile.json, so switching mapping profiles
    mid-session doesn't wipe out the accumulated fault record."""
    _ensure_dir()
    with open(path, 'w') as f:
        json.dump(management_engine.fault_log.to_dict(), f, indent=2)
    return path


def load_fault_log(management_engine, path=FAULT_LOG_PATH):
    """Attaches the persisted fault history onto a (possibly freshly-
    constructed) ManagementEngine - call this after building/replacing a
    ManagementEngine (startup, or a manual profile load) so fault counts
    survive across both app restarts and mid-session profile switches."""
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    management_engine.fault_log = FaultLog.from_dict(data)
