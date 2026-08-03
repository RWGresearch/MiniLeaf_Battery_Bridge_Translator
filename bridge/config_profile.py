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


def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def save_profile(state, mapping_engine, management_engine, path=DEFAULT_PROFILE_PATH):
    _ensure_dir()
    profile = {
        'profile_name': os.path.splitext(os.path.basename(path))[0],
        'vehicle': dict(state.vehicle),
        'mappings': mapping_engine.to_list(),
        'management_features': management_engine.to_dict(),
        'generated_signals': dict(state.generated_enabled),
        'charge_emulation': dict(state.charge_emulation),
    }
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
    state.vehicle.update(profile.get('vehicle', {}))
    state.generated_enabled.update(profile.get('generated_signals', {}))
    _apply_charge_emulation(state, profile.get('charge_emulation', {}))
    mapping = MappingEngine.from_list(profile.get('mappings', []))
    management = ManagementEngine.from_dict(profile.get('management_features', {}))
    return mapping, management


def _apply_charge_emulation(state, loaded):
    """Clamps every numeric field to leaf_signals.CHARGE_EMULATION_BOUNDS
    before applying it (docs/13 items 13.3/13.9, fixed 2026-08-03) - same
    reasoning as ManagementEngine.from_dict()'s bounds clamp: a hand-edited
    or corrupted profile.json must not be able to set an AC-charger/regen
    threshold to an arbitrary value just because it bypasses the GUI. A
    value that can't be coerced to a number is dropped, leaving the
    existing default in place, rather than written through as-is."""
    from bridge import leaf_signals
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
