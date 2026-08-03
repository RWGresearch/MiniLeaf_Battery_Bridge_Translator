"""Profile-vs-code-defaults drift report - run directly
(`py tests/check_profile_drift.py [path/to/profile.json]`, defaults to
config/profile.json).

NOT a pass/fail test like the other tests/test_*.py scripts (deliberately
named check_* instead of test_*, and not picked up by any "run every
test_*.py" loop) - a saved profile is EXPECTED to differ from code defaults
whenever the user has deliberately tuned something, so "differs" is not
itself a failure. This is a diagnostic report for a human to read after a
code change: "here's exactly what's different between the shipped defaults
and what's actually saved, field by field" - so a default retuned in code
(e.g. emergency_high_v 4.30->4.20) is never silently missed just because
nothing crashed. Prompted directly by a real, found-live case: config/
profile.json turned out to still carry forward min_soc_pct=10.0,
emergency_low_v=2.8, discharge taper 3.5/3.0, regen 3.9/4.1,
emergency_temp_f=149.0, and warn_delta_v=0.05 - the ORIGINAL researched
defaults from early in this project, none of the many 2026-08-01/03
retunings ever having been reflected in it because saving only happens when
the GUI is used to edit something, and it had otherwise just been silently
reloaded-and-resaved unchanged, session after session.

Three categories of drift are worth distinguishing, and this reports all
three separately:
- CHANGED: the field exists in both, but the values differ.
- MISSING FROM PROFILE: a field/feature the current code defines but the
  saved file doesn't have at all - either a new feature added since this
  file was last saved (e.g. input_validation/checksum_validation, added
  2026-08-03), or the file predates that field's existence entirely.
  ManagementEngine.from_dict()/config_profile.py already handle this safely
  at load time (the code default is used) - this is purely visibility.
- ORPHANED IN PROFILE: a field/feature the saved file has that current code
  no longer defines at all (renamed or removed). Already silently dropped
  on load (`ManagementEngine.from_dict()`'s "only pull in keys the current
  schema still defines" behavior) - again, purely visibility, so a rename
  is never invisible.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import config_profile, leaf_signals
from bridge.management_engine import default_config


def _charge_emulation_defaults():
    d = {k: v for k, _, v in leaf_signals.CHARGE_CHECKS}
    d.update({k: v for k, _, _, _, _, v in leaf_signals.CHARGE_SLIDERS})
    return d


def _generated_signals_defaults():
    return {k: v for k, _, v in leaf_signals.GENERATED_SIGNALS}


VEHICLE_DEFAULTS = {'car_gen': 'ZE1', 'battery_gen': 'ZE1', 'battery_kwh': 40}


def _diff_flat(label, profile_dict, default_dict, lines):
    profile_dict = profile_dict or {}
    changed = missing = orphaned = 0
    for key, default_val in sorted(default_dict.items()):
        if key not in profile_dict:
            lines.append(f'  [MISSING FROM PROFILE] {label}.{key} - code default is {default_val!r}')
            missing += 1
        elif profile_dict[key] != default_val:
            lines.append(f'  [CHANGED] {label}.{key}: profile={profile_dict[key]!r}  code_default={default_val!r}')
            changed += 1
    for key in sorted(set(profile_dict) - set(default_dict)):
        lines.append(f'  [ORPHANED IN PROFILE] {label}.{key} = {profile_dict[key]!r} - not defined in current code')
        orphaned += 1
    return changed, missing, orphaned


def report(path=None):
    path = path or config_profile.DEFAULT_PROFILE_PATH
    profile = config_profile.load_profile(path)
    if profile is None:
        print(f'No profile at {path} - nothing to compare (a fresh app launch would use pure code defaults).')
        return

    lines = []
    total = [0, 0, 0]   # changed, missing, orphaned

    lines.append(f'== vehicle ==')
    c, m, o = _diff_flat('vehicle', profile.get('vehicle'), VEHICLE_DEFAULTS, lines)
    total[0] += c; total[1] += m; total[2] += o

    lines.append(f'== management_features ==')
    cfg = default_config()
    profile_features = profile.get('management_features', {})
    for feature, defaults in sorted(cfg.items()):
        c, m, o = _diff_flat(feature, profile_features.get(feature), defaults, lines)
        total[0] += c; total[1] += m; total[2] += o
    for feature in sorted(set(profile_features) - set(cfg)):
        lines.append(f'  [ORPHANED IN PROFILE] management_features.{feature} - not a feature in current code')
        total[2] += 1

    lines.append(f'== generated_signals ==')
    c, m, o = _diff_flat('generated_signals', profile.get('generated_signals'), _generated_signals_defaults(), lines)
    total[0] += c; total[1] += m; total[2] += o

    lines.append(f'== charge_emulation ==')
    c, m, o = _diff_flat('charge_emulation', profile.get('charge_emulation'), _charge_emulation_defaults(), lines)
    total[0] += c; total[1] += m; total[2] += o

    print(f'Profile-vs-code-defaults drift report: {path}\n')
    for line in lines:
        print(line)
    print(f'\n{total[0]} changed, {total[1]} missing from profile, {total[2]} orphaned in profile.')
    print('Not a failure by itself - review each CHANGED line and confirm it\'s either a deliberate '
          'tuning you want to keep, or a default that moved in code and this profile should be re-saved.')


if __name__ == '__main__':
    report(sys.argv[1] if len(sys.argv) > 1 else None)
