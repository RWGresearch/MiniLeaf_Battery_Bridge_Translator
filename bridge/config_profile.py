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
    state.charge_emulation.update(profile.get('charge_emulation', {}))
    mapping = MappingEngine.from_list(profile.get('mappings', []))
    management = ManagementEngine.from_dict(profile.get('management_features', {}))
    return mapping, management


def save_last_known_good(state, path=LAST_KNOWN_GOOD_PATH):
    _ensure_dir()
    with open(path, 'w') as f:
        json.dump(state.last_known_good, f, indent=2)
    return path


def load_last_known_good(path=LAST_KNOWN_GOOD_PATH):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


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
