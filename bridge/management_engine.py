"""Battery management / safety layer: curated, named protection features with
researched defaults (docs/05-battery-management-safety.md). Not a generic
rule engine - each feature below is a fixed function with typed config, so
every feature stays portable to a future STM32 C function.

Soft cut = capacity_empty / full_charge_flag (no dash error).
Hard cut = relay_cut_request + interlock drop (RED dash error) - reserved
for emergency-tier thresholds and the staleness watchdog's escalation.
"""
import time

from bridge import rz450e_signals
from bridge.fault_log import FaultLog


# Registered (lo, hi) numeric bounds per (feature, field) - shared by BOTH
# the GUI (gui/panels.py's ManagementPanel, clamps on every keystroke) and
# from_dict() below (clamps on every profile/file load), so the two paths
# can never silently diverge (docs/13 items 13.3/13.9, fixed 2026-08-03).
# Previously this table only existed in gui/panels.py and from_dict() didn't
# use it at all - a hand-edited or corrupted profile.json (including the one
# loaded automatically at every app startup) could set any threshold to an
# arbitrary, physically-nonsensical value with nothing standing in the way,
# and the only defense (typing in the GUI) never even ran. Deliberately
# generous (same "sanity range, not an operating threshold" philosophy as
# rz450e_signals.PLAUSIBLE_RANGES) - this exists to catch a mistyped/
# corrupted value, not to second-guess a deliberately extreme but valid
# threshold choice.
FEATURE_FIELD_BOUNDS = {
    ('low_voltage_cutoff', 'min_cell_v'): (0.0, 5.0),
    ('low_voltage_cutoff', 'emergency_low_v'): (0.0, 5.0),
    ('low_voltage_cutoff', 'soft_cut_persistence_s'): (0.0, 60.0),
    ('low_voltage_cutoff', 'min_soc_pct'): (0.0, 100.0),
    ('discharge_power_taper', 'taper_start_v'): (0.0, 5.0),
    ('discharge_power_taper', 'taper_zero_v'): (0.0, 5.0),
    ('discharge_power_taper', 'recovery_ramp_s'): (0.01, 60.0),
    # discharge_min_kw/discharge_max_kw (added 2026-08-08, docs/16 audit - user directive: "both
    # need min and max settings by the user", same pattern as charger_limit_kw's ac_min_kw/
    # ac_max_kw) - bound matches leaf_signals.RANGES['discharge_limit_kw'] (0, 255.75), the
    # hardware-encodable ceiling for this CAN field, same reasoning as every other bound here.
    ('discharge_power_taper', 'discharge_min_kw'): (0.0, 255.75),
    ('discharge_power_taper', 'discharge_max_kw'): (0.0, 255.75),
    ('charge_target_taper', 'regen_full_v'): (0.0, 5.0),
    ('charge_target_taper', 'regen_zero_v'): (0.0, 5.0),
    ('charge_target_taper', 'emergency_high_v'): (0.0, 5.0),
    ('charge_target_taper', 'recovery_ramp_s'): (0.01, 60.0),
    # regen_min_kw/regen_max_kw (added 2026-08-08, same directive/pattern as discharge above) -
    # bound matches leaf_signals.RANGES['charge_limit_kw'] (0, 255.75).
    ('charge_target_taper', 'regen_min_kw'): (0.0, 255.75),
    ('charge_target_taper', 'regen_max_kw'): (0.0, 255.75),
    ('over_temperature_derate', 'charge_derate_low_start_c'): (-51.1, 121.1),
    ('over_temperature_derate', 'charge_low_block_c'): (-51.1, 121.1),
    ('over_temperature_derate', 'charge_derate_start_c'): (-51.1, 121.1),
    ('over_temperature_derate', 'charge_hard_stop_c'): (-51.1, 121.1),
    ('over_temperature_derate', 'discharge_derate_start_c'): (-51.1, 121.1),
    ('over_temperature_derate', 'discharge_hard_stop_c'): (-51.1, 121.1),
    ('over_temperature_derate', 'emergency_temp_c'): (-51.1, 121.1),
    ('cell_imbalance_monitor', 'warn_delta_v'): (0.0, 2.0),
    ('overcurrent_monitor', 'continuous_discharge_warn_a'): (0.0, 210.0),
    ('overcurrent_monitor', 'continuous_charge_warn_a'): (0.0, 210.0),
    ('overcurrent_monitor', 'persistence_s'): (0.0, 120.0),
    ('staleness_watchdog', 'soft_cut_s'): (1.0, 600.0),
    ('staleness_watchdog', 'hard_escalation_s'): (0.0, 600.0),
    ('cell_data_cross_check', 'max_delta_v'): (0.0, 2.0),
    ('cell_data_cross_check', 'soft_cut_s'): (1.0, 600.0),
    ('cell_data_cross_check', 'hard_escalation_s'): (0.0, 600.0),
    ('temp_data_cross_check', 'max_delta_c'): (0.0, 33.3),
    ('temp_data_cross_check', 'soft_cut_s'): (1.0, 600.0),
    ('temp_data_cross_check', 'hard_escalation_s'): (0.0, 600.0),
}


# One-time migration (added 2026-08-09): over_temperature_derate/
# temp_data_cross_check switched their config keys from °F to °C storage -
# bridge/rz450e_signals.py's decode functions now emit temp_max/temp_min in
# °C directly (see main.py's changelog), so these thresholds are compared
# against °C values from this point on. A saved profile.json from before
# this change still carries the old _f-suffixed keys with Fahrenheit values;
# ManagementEngine.from_dict() translates each one to its new _c-suffixed
# key, converted to Celsius, so a user's real tuned threshold survives
# instead of silently reverting to the new Celsius default - same
# "translate, don't just drop" reasoning as config_profile.py's pre-existing
# ac_zero_v -> ac_min_v migration.
_TEMP_ABS_F_TO_C_KEY_MIGRATIONS = {
    'over_temperature_derate': [
        ('charge_derate_low_start_f', 'charge_derate_low_start_c'),
        ('charge_low_block_f', 'charge_low_block_c'),
        ('charge_derate_start_f', 'charge_derate_start_c'),
        ('charge_hard_stop_f', 'charge_hard_stop_c'),
        ('discharge_derate_start_f', 'discharge_derate_start_c'),
        ('discharge_hard_stop_f', 'discharge_hard_stop_c'),
        ('emergency_temp_f', 'emergency_temp_c'),
    ],
}
# Deltas (a temperature DIFFERENCE, not an absolute reading) convert without
# the +/-32 offset - F_delta * 5/9, not (F-32)*5/9.
_TEMP_DELTA_F_TO_C_KEY_MIGRATIONS = {
    'temp_data_cross_check': [('max_delta_f', 'max_delta_c')],
}


def _migrate_temp_f_keys(feature, values):
    """Returns a copy of `values` with any old °F-suffixed key translated to
    its new °C-suffixed equivalent, only when the new key isn't already
    present (a profile saved post-migration must never be overwritten by a
    stale leftover old key)."""
    values = dict(values)
    for old_key, new_key in _TEMP_ABS_F_TO_C_KEY_MIGRATIONS.get(feature, []):
        if old_key in values and new_key not in values:
            try:
                values[new_key] = (float(values.pop(old_key)) - 32.0) * 5.0 / 9.0
            except (TypeError, ValueError):
                values.pop(old_key, None)
    for old_key, new_key in _TEMP_DELTA_F_TO_C_KEY_MIGRATIONS.get(feature, []):
        if old_key in values and new_key not in values:
            try:
                values[new_key] = float(values.pop(old_key)) * 5.0 / 9.0
            except (TypeError, ValueError):
                values.pop(old_key, None)
    return values


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _ramp_factor(value, floor, ceiling):
    """1.0 at/above `ceiling`, 0.0 at/below `floor`, linear between."""
    if ceiling <= floor:
        return 1.0 if value >= ceiling else 0.0
    return _clamp((value - floor) / (ceiling - floor), 0.0, 1.0)


# AC charger taper convergence rate table (added 2026-08-06, replacing the
# fixed-time-constant hysteresis from the same day - see ac_charge_taper's
# own comment in ManagementEngine.apply() for the full rationale: a real
# bench test showed the taper hunting - a repeating full-cycle oscillation,
# including a same-tick multi-kW jump - because "instant response, either
# direction" is the wrong model for a CC-CV charging control loop, unlike
# the discharge/regen tapers (which genuinely do need instant response to
# arrest real cell sag under load). User directive: reuse the existing 0-7
# chg_uprate_level rate table (leaf_signals.CHG_RAMP_RAW_PER_S, real-
# hardware-confirmed rates - 2.0kW/s at level 7, halving per level down) as
# a DYNAMICALLY-SELECTED rate instead of a fixed time constant - always
# start at level 7 (fastest) when convergence begins, downshift (and
# upshift, symmetrically) as the remaining distance to the target narrows -
# gentler the closer it gets, matching real CC-CV taper behavior.
#
# These 7 thresholds (level 7's threshold down through level 1's; below the
# last -> level 0) are NEW, TUNED STARTING VALUES sized for the AC
# charger's realistic 0.5-6.6kW span (ac_min_kw/ac_max_kw defaults) - not
# researched or real-hardware-confirmed, same as any other new tunable
# constant this project adds (docs/11). Real tuning happens against a
# repeat of the exact test that surfaced this bug (docs/15 B20).
_AC_LEVEL_DOWNSHIFT_KW = (3.0, 1.5, 0.75, 0.4, 0.2, 0.1, 0.05)
# Upshift hysteresis: require `remaining` to grow past this multiple of the
# NEXT level up's own downshift threshold before actually upshifting - the
# deadband that keeps the SELECTED LEVEL ITSELF from flapping right at a
# boundary (nudge across a threshold, jump a level, nudge back, jump back),
# the same class of problem an automatic transmission solves with separate
# up-shift/down-shift points.
_AC_LEVEL_HYSTERESIS_MULT = 1.5


def _select_ac_uprate_level(remaining_kw, current_level):
    """Picks the 0-7 uprate level to converge `remaining_kw` (the absolute
    distance still left to close) toward zero, with hysteresis on the
    switch thresholds so the selected level doesn't flap at a boundary.
    `current_level` is the level in use on the PREVIOUS tick (persisted by
    the caller) - downshifts immediately once `remaining_kw` drops below
    the current level's own threshold, but only upshifts once it grows past
    `_AC_LEVEL_HYSTERESIS_MULT` times the threshold for the next level up."""
    # Level N (7..1) threshold is _AC_LEVEL_DOWNSHIFT_KW[7-N]; level 0 has
    # no threshold of its own (it's the floor - "below the last").
    def threshold_for(level):
        if level <= 0:
            return 0.0
        return _AC_LEVEL_DOWNSHIFT_KW[7 - level]

    level = current_level
    # Downshift: drop while the CURRENT level's own threshold isn't met.
    while level > 0 and remaining_kw < threshold_for(level):
        level -= 1
    # Upshift: climb while comfortably past the NEXT level's threshold
    # (the hysteresis margin), never past 7.
    while level < 7 and remaining_kw >= threshold_for(level + 1) * _AC_LEVEL_HYSTERESIS_MULT:
        level += 1
    return level


def _clear_disabled_feature(fault_log, entries):
    """Docs/13 item 14.5: every feature's tracked fault_log
    entries were only ever `update()`d while that feature's own `enabled`
    flag was True - unchecking a feature mid-active-fault froze its entry at
    whatever active/inactive state it last had instead of reflecting that
    it's no longer being evaluated at all. User directive: this system is
    already a real-time engine (config changes apply live, no restart
    needed) - a disabled feature's own fault entries should be live too, not
    a special frozen case. Called once per disabled feature per tick so its
    entries immediately (not eventually) show 'disabled', not a stale
    trigger. `entries`: iterable of (key, label, level)."""
    for key, label, level in entries:
        fault_log.update(key, label, level, False, 'feature disabled')


# Cross-field ordering sanity (docs/05, added 2026-08-01, user directive):
# protects a hand-edited profile.json too, not just live GUI typing -
# gui/panels.py's ManagementPanel separately clamps each field to its own
# (lo, hi) numeric bounds, but can't catch a relationship BETWEEN two
# fields (e.g. an emergency threshold typed less extreme than its own soft
# tier). (feature, field_a, field_b, description) - checked as
# cfg[feature][field_a] < cfg[feature][field_b].
_CONFIG_SANITY_CHECKS = [
    ('low_voltage_cutoff', 'emergency_low_v', 'min_cell_v',
     'the emergency tier should be more extreme (lower) than the soft-cut tier'),
    ('discharge_power_taper', 'taper_zero_v', 'taper_start_v',
     'zero-power point should be below the full-power point'),
    ('discharge_power_taper', 'discharge_min_kw', 'discharge_max_kw',
     'minimum discharge power request should be below maximum discharge power request'),
    ('charge_target_taper', 'regen_full_v', 'regen_zero_v',
     'full-power point should be below the zero-power point'),
    ('charge_target_taper', 'regen_zero_v', 'emergency_high_v',
     'the emergency tier should be more extreme (higher) than the zero-power point'),
    ('charge_target_taper', 'regen_min_kw', 'regen_max_kw',
     'minimum regen power request should be below maximum regen power request'),
    ('over_temperature_derate', 'charge_low_block_c', 'charge_derate_low_start_c',
     'the cold block point should be below the cold-derate-start point'),
    ('over_temperature_derate', 'charge_derate_start_c', 'charge_hard_stop_c',
     'the derate-start point should be below the hard-stop point'),
    ('over_temperature_derate', 'discharge_derate_start_c', 'discharge_hard_stop_c',
     'the derate-start point should be below the hard-stop point'),
    ('over_temperature_derate', 'discharge_hard_stop_c', 'emergency_temp_c',
     'the emergency tier should be more extreme (hotter) than the soft hard-stop point'),
]


_CHARGE_EMULATION_SANITY_CHECKS = [
    ('ac_full_v', 'ac_min_v', 'full-power point should be below the minimum-power point', False),
    # allow_equal=True (user directive, 2026-08-07: "i wanted to trigger
    # full charge cutoff at the same time we reached min charger taper.
    # thats why i had them set the same value") - ac_min_v==ac_cutoff_v is a
    # deliberate, valid configuration (the taper reaches its ac_min_kw floor
    # and the stop-charging cutoff fire at the same instant, with no
    # separate floor-hold window), not an inverted/nonsensical ordering -
    # only ac_min_v > ac_cutoff_v (floor above the cutoff) is actually wrong.
    ('ac_min_v', 'ac_cutoff_v', 'the minimum-power point should be at or below the stop-charging cutoff', True),
    ('ac_cutoff_v', 'ac_emergency_v', 'the emergency tier should be more extreme (higher) than the stop-charging cutoff', False),
    ('ac_min_kw', 'ac_max_kw', 'AC minimum power request should be below AC maximum power request', False),
]


def _check_config_sanity(cfg, charge_emulation=None):
    """Returns a list of human-readable violation descriptions - does not
    change or block anything itself; the engine keeps running with whatever
    values are actually present (clamp_state() still protects the CAN bus
    output either way). Surfaced as a fault_log warning, not a crash or a
    silent misbehavior. `charge_emulation` (state.charge_emulation, added
    2026-08-01) is checked too since the AC-charger taper's thresholds live
    there, not in `cfg`, after the regen/AC-charger split."""
    violations = []
    for feature, field_a, field_b, desc in _CONFIG_SANITY_CHECKS:
        fcfg = cfg.get(feature)
        if not fcfg or field_a not in fcfg or field_b not in fcfg:
            continue
        a, b = fcfg[field_a], fcfg[field_b]
        if not (a < b):
            violations.append(f'{feature}.{field_a}={a:g} should be < {feature}.{field_b}={b:g} ({desc})')
    if charge_emulation:
        for field_a, field_b, desc, allow_equal in _CHARGE_EMULATION_SANITY_CHECKS:
            if field_a in charge_emulation and field_b in charge_emulation:
                a, b = charge_emulation[field_a], charge_emulation[field_b]
                ok = (a <= b) if allow_equal else (a < b)
                if not ok:
                    op = '<=' if allow_equal else '<'
                    violations.append(
                        f'charge_emulation.{field_a}={a:g} should be {op} charge_emulation.{field_b}={b:g} ({desc})')
    return violations


def default_config():
    """Researched NMC/lithium-ion defaults, cross-checked against this
    pack's confirmed real-world range (docs/05). Shipped ENABLED, not
    blank - per explicit user confirmation, since everything here is
    editable and gets refined against real hardware over time."""
    return {
        'low_voltage_cutoff': {
            # emergency_low_v 2.80->2.60V and min_soc_pct 10.0->8.0% (user
            # edit, 2026-08-01, docs/13-review-checklist item Part 7): lower
            # emergency threshold so it's less likely to trigger until the
            # cell has truly reached a genuinely extreme low, and a lower
            # SoC backup floor for consistency/redundancy alongside it (SoC
            # remains a backup check only - never an independent trigger).
            'enabled': True, 'min_cell_v': 3.00, 'min_soc_pct': 8.0,
            'emergency_low_v': 2.60,
            # Persistence window (docs/12 finding F5, 2026-07-31): the soft
            # cut only latches once the min-cell condition has held
            # continuously for this many seconds - guards against a single-
            # tick sag transient under a spike load (cold-pack IR roughly
            # doubles vs. 25C) tripping capacity_empty when the cell would
            # have rebounded the instant load dropped. The discharge power
            # taper reaching zero at the same voltage already collapses
            # current/sag well before this window elapses in the normal
            # case; this is the backstop for a fast, brief spike. Emergency
            # tier stays instantaneous - no persistence there.
            'soft_cut_persistence_s': 2.0,
        },
        'discharge_power_taper': {
            'enabled': True,
            # Proactive discharge-power curve (user-specified 2026-07-31,
            # mirroring the regen/charge taper's design): driven by
            # individual cell voltage, not SoC - a single weak/imbalanced
            # cell sags under heavy discharge load well before pack-average
            # SoC would suggest. Full power at/above taper_start_v, ramping
            # to zero at/below taper_zero_v (matches low_voltage_cutoff's
            # min_cell_v soft-cut floor by default, so the ramp reaches zero
            # right as the soft cut engages - a smooth transition instead of
            # full-power-then-sudden-stop).
            # taper_start_v 3.50->3.00V and taper_zero_v 3.00->2.60V (user
            # edit, 2026-08-01): re-anchored to the same 2.60V now used by
            # low_voltage_cutoff's emergency tier, so the taper still
            # reaches zero right around where the cutoff tiers sit.
            'taper_start_v': 3.00, 'taper_zero_v': 2.60,
            # Hysteresis (user-specified): fast to respond to a dip (snap
            # down immediately - cell protection can't wait), slow to
            # recover back to full power once voltage comes back up (avoids
            # power hunting/oscillation if voltage bounces near the
            # threshold under intermittent acceleration). Time in seconds to
            # go from 0% back to 100% power once voltage has recovered.
            'recovery_ramp_s': 3.0,
            # discharge_min_kw/discharge_max_kw (added 2026-08-08, docs/16
            # parameter-clamping audit - user directive: "both need min and
            # max settings by the user", same pattern as charger_limit_kw's
            # ac_min_kw/ac_max_kw). min_kw=0.0 preserves existing behavior
            # exactly (the taper still reaches true zero at taper_zero_v,
            # matching tests/test_management_engine.py's existing hysteresis
            # test). max_kw=110.0 matches the existing static
            # leaf_signals.DEFAULTS['discharge_limit_kw'] AND is independently
            # well-grounded: docs/12-nmc-bms-design-research.md §6 researched
            # "Leaf drive power 80-110 kW peak ~= 230-315A ~= 1.1-1.6C
            # discharge peak - well within NMC capability" - 110.0 sits right
            # at the top of that researched range, not an arbitrary carry-
            # over number.
            'discharge_min_kw': 0.0, 'discharge_max_kw': 110.0,
        },
        'charge_target_taper': {
            'enabled': True,
            # REGEN ONLY as of 2026-08-01 (user directive: "regen and AC
            # charging is not the same... I can regen WAY more power than I
            # can AC charge, so the parameters need to be split"). Drives
            # ONLY charge_limit_kw ("Charge/regen power limit," docs/03) -
            # the Leaf's shared ceiling for regenerative braking while
            # driving AND general charge acceptance. The AC-charger-specific
            # taper (charger_limit_kw) and the daily/extended AC SoC target
            # both moved to state.charge_emulation / the Charge Emulation
            # GUI tab - see this file's own `ac_charge_taper` block further
            # down (NOT a RealtimeEngine method - it lives directly in
            # ManagementEngine.apply(), same as this block) and
            # leaf_signals.py's CHARGE_SLIDERS/CHARGE_CHECKS - since they
            # only ever mattered while actually plugged in anyway.
            #
            # Proactive curve (user-specified, values updated 2026-08-01):
            # the VCM is slow to respond to a charge_limit_kw change, so the
            # taper must start well before the danger zone, not right at its
            # edge. Full power at/below regen_full_v, zero at/above
            # regen_zero_v, linear between.
            'regen_full_v': 4.00, 'regen_zero_v': 4.15,
            # emergency_high_v 4.30->4.20V (user edit, 2026-08-03, docs/13
            # item 15.3) - set to the standard NMC charge ceiling exactly
            # (docs/05's researched 4.20V), tightening the margin above the
            # 4.15V zero-power point to 0.05V (still passes the
            # regen_zero_v < emergency_high_v config-sanity check).
            'emergency_high_v': 4.20,
            # Hysteresis (added 2026-08-01, user request: "regen we should
            # add some hysteresis? same as discharge?") - same fast-attack/
            # slow-release pattern as discharge_power_taper's recovery_ramp_s.
            'recovery_ramp_s': 3.0,
            # regen_min_kw/regen_max_kw (added 2026-08-08, docs/16 parameter-
            # clamping audit - same directive/pattern as discharge_power_
            # taper's discharge_min_kw/discharge_max_kw above). min_kw=0.0
            # preserves existing behavior exactly (existing hysteresis test
            # asserts true zero at the ramp's floor). max_kw=70.0 matches the
            # existing static leaf_signals.DEFAULTS['charge_limit_kw'] - but
            # UNLIKE discharge_max_kw, this is NOT independently researched:
            # docs/12-nmc-bms-design-research.md §6 puts real Leaf regen at
            # "up to a few tens of kW ~= up to ~0.5C into the pack" (~36kW for
            # this pack's ~200Ah rating), notably lower than 70.0kW. Kept at
            # 70.0kW deliberately on rollout (user directive 2026-08-08: ship
            # with no behavior change rather than silently capping regen
            # lower than it can reach today) - tune down toward the
            # researched ~36kW figure once ready, this is not a "confirmed
            # correct" default.
            'regen_min_kw': 0.0, 'regen_max_kw': 70.0,
        },
        'over_temperature_derate': {
            'enabled': True,
            # Cold-side (docs/12 findings F1 + F3, 2026-07-31 research pass):
            # charge_low_block_c and charge_derate_low_start_c are evaluated
            # against the COLDEST probe (temp_min), never the hottest -
            # lithium plating happens in the coldest cells, so a pack whose
            # coldest corner is below freezing must be blocked even if its
            # warmest probe reads fine. Ramps from 0 (charge_low_block_c, 0C)
            # to full (charge_derate_low_start_c, 10C) - research shows
            # plating risk rises well above 0C at meaningful charge current;
            # our real exposure is regen into a cold-soaked pack (~0.5C), not
            # the 0.09C AC charger.
            'charge_derate_low_start_c': 10.0, 'charge_low_block_c': 0.0,
            # Hot-side charge ramp, evaluated against the hottest probe
            # (temp_max) - unchanged range, but charge_hard_stop_c is now a
            # pure soft ramp-to-zero point, not also a hard-cut trigger (see
            # emergency_temp_c below).
            'charge_derate_start_c': 32.0, 'charge_hard_stop_c': 45.0,
            # Discharge ramp, hottest probe - discharge_hard_stop_c is also
            # now a pure soft ramp-to-zero point, matching the "Soft (ramp)"
            # tier this feature is documented as in docs/05's feature table.
            'discharge_derate_start_c': 55.0, 'discharge_hard_stop_c': 60.0,
            # Emergency hard-cut tier (docs/12 finding F6, 2026-07-31): a
            # second, more extreme threshold above discharge_hard_stop_c,
            # evaluated against the hottest probe - mirrors the two-tier
            # soft/emergency structure the voltage features already have.
            # Self-heating onset for a cell with any plated lithium can begin
            # as low as ~60C. Tightened 2026-08-01 (user edit) from 65C to
            # 61C exactly - only 1C of margin above the 60C soft stop,
            # deliberately thinner than the original researched default
            # because there's very little real margin above it in the
            # chemistry to begin with. (Values converted from °F to °C
            # storage 2026-08-09 - same physical thresholds, no behavior
            # change: this field used to be stored/edited as 141.8°F.)
            'emergency_temp_c': 61.0,
        },
        'cell_imbalance_monitor': {
            # Monitor/warn-tier only (docs/12 finding F4, 2026-07-31) - never
            # cuts or derates anything. We can't balance cells (that would be
            # the RZ450e pack's own internal cell-supervision hardware, if it
            # still operates in this configuration - see docs/10 open
            # question), but we uniquely see all 96 cells at high rate, so
            # flagging a growing spread is cheap and catches a developing bad
            # cell early: a cell resting 30-50mV below its neighbors is the
            # classic early signature of elevated self-discharge / an
            # internal defect.
            # warn_delta_v 0.05->0.10V (user edit, 2026-08-01) - widened
            # from the original researched 50mV starting point to 100mV.
            'enabled': True, 'warn_delta_v': 0.10,
        },
        'overcurrent_monitor': {
            # Monitor/warn-tier only (docs/12 finding F2, 2026-07-31) - never
            # cuts or derates. Our envelope is gentle by EV standards (Leaf AC
            # charge ~19A/0.09C, drive peak ~230-315A/1.1-1.6C) and no cell
            # datasheet exists to source a real continuous/peak limit from,
            # so this is deliberately NOT wired to an active cutoff yet.
            # Defaults are derived from THIS project's own confirmed specs,
            # not an invented number: continuous_discharge_warn_a sits well
            # below the 0x023 sensor's +/-204.7A saturation ceiling (so a
            # warning still means something - above ~205A the true magnitude
            # is unknown to us regardless), and continuous_charge_warn_a
            # sits above the Leaf onboard AC charger's ~19A max so normal
            # charging never trips it, catching only abnormal charge/regen
            # current. persistence_s avoids flagging a brief acceleration
            # spike as sustained overcurrent.
            'enabled': True, 'continuous_discharge_warn_a': 150.0,
            'continuous_charge_warn_a': 30.0, 'persistence_s': 5.0,
        },
        'staleness_watchdog': {
            'enabled': True, 'soft_cut_s': 60.0, 'hard_escalation_s': 5.0,
        },
        'cell_data_cross_check': {
            # Live redundancy check between the per-cell array
            # (authoritative) and the 0x020 pack-level cell_min/cell_max
            # summary (added 2026-08-01, user directive). docs/02 and
            # docs/04 both describe the pack summary as a "sanity cross-
            # check" against the per-cell messages, but no comparison
            # actually existed in code before this - cell_min/cell_max were
            # only ever used as a complete fallback when the per-cell array
            # was empty. Same soft-then-hard escalation STRUCTURE as the
            # staleness watchdog ("just like the watchdog," per the user),
            # independently tunable from it - same 60s/5s starting point.
            'enabled': True, 'max_delta_v': 0.15, 'soft_cut_s': 60.0, 'hard_escalation_s': 5.0,
        },
        'temp_data_cross_check': {
            # Live redundancy check between the pack-level temperature
            # extremes (0x4A7's temp_max/temp_min) and the actual min/max of
            # all 16 individually-read temp probes (0x4AA temp_01..temp_16) -
            # added 2026-08-04 (docs/13 item 16.2), the same pattern as
            # cell_data_cross_check above, applied to temperature: docs/02
            # documents 0x4A7 as "pack-level temperature extremes," but
            # nothing previously cross-checked that against the individual
            # probes it's presumably derived from. This matters specifically
            # because temp_min feeds the cold-side charge-block/derate logic
            # (docs/12 finding F1) - a decode/mux fault that passes each
            # field's own PLAUSIBLE_RANGES independently (e.g. a byte swap
            # producing a temp_min reading that's actually higher than
            # temp_max) would otherwise go completely undetected.
            # max_delta_c is deliberately wider than a "genuine fault"
            # margin needs to be, same reasoning as cell_data_cross_check's
            # 150mV: real spatial temperature gradient across the pack's 4
            # physical sub-packs under load is a legitimate, expected source
            # of disagreement between a single hottest/coldest probe and
            # whatever internal aggregation 0x4A7 performs, and this is a
            # data-integrity check, not a temperature-level protection
            # feature (that's over_temperature_derate's job) - documented
            # starting point, not yet confirmed against real thermal
            # gradient data on this pack (docs/11). (Converted from °F to °C
            # storage 2026-08-09 - was 10.0°F delta, now the equivalent
            # 5.6°C delta, no behavior change.)
            'enabled': True, 'max_delta_c': 5.6, 'soft_cut_s': 60.0, 'hard_escalation_s': 5.0,
        },
        # Both below: given a real enable/disable toggle 2026-08-03 (docs/13
        # items 15.14/15.15, user directive) - previously hardcoded always-on
        # with no config field at all. Default ON (these are genuine safety
        # nets, not something to casually leave off) - the toggle exists so a
        # deliberately-corrupted/synthetic test value can be pushed through
        # unfiltered to confirm downstream handling, and so it's a visible
        # feature (checkbox + live status) rather than invisible fixed logic.
        # No threshold fields - RealtimeEngine reads these same 'enabled'
        # flags to decide whether to actually run rz450e_signals.
        # validate_inputs()/frame_checksum_ok() at all, not just whether to
        # report on it.
        'input_validation': {'enabled': True},
        'checksum_validation': {'enabled': True},
    }


class ManagementEngine:
    # NOTE (added 2026-08-01): `.config`/`.status` are read/written from both
    # the GUI thread (gui/panels.py's ManagementPanel/ChargeEmulationPanel,
    # on every keystroke) and the TX thread (apply(), every tick) with no
    # lock of its own - same class of gap as bridge/state.py's
    # `generated_enabled`/`charge_emulation` (see that file's comment).
    # Individual dict item reads/writes remain safe under CPython's GIL;
    # deliberately not retrofitted with a lock this pass, pending the same
    # STM32-architecture discussion - see docs/13-review-checklist-2026-08-01.md.
    def __init__(self, config=None):
        self.config = config or default_config()
        self.status = {}
        self._stale_since = None
        self._discharge_factor_applied = 1.0
        self._regen_factor_applied = 1.0
        # AC-charger taper convergence state (added 2026-08-06, reworked
        # same day to a dynamically-selected rate - see the ac_charge_taper
        # block in apply() and _select_ac_uprate_level()'s own comments for
        # the full rationale). `_ac_applied_kw` is the actual output being
        # converged toward the taper's instantaneous target; `_ac_current_level`
        # persists the in-use 0-7 rate level between ticks (for the level-
        # switching hysteresis); `ac_uprate_level` (public, no leading
        # underscore) is None whenever the taper isn't actively converging
        # (full power/disabled/no data/emergency) and an int 0-7 whenever it
        # is - read by RealtimeEngine._compose_leaf_state() to override the
        # transmitted 0x1DC uprate bits, since that field is a real signal
        # or 'used somewhere else in the system' (user directive) and must
        # genuinely represent the rate actually in use while converging.
        self._ac_applied_kw = 0.0
        self._ac_current_level = 7
        self.ac_uprate_level = None
        self._last_apply_time = None
        self._low_v_condition_since = None
        self._overcurrent_since = None
        self._overcurrent_direction = None
        self._cross_check_since = None
        self._temp_cross_check_since = None
        # Hard-cut latch (docs/12 finding F8, fixed 2026-08-01 per user
        # directive: "it should only reset AFTER the car has been powered
        # down and back on OR if the charger is unplugged and replugged").
        # Previously every hard cut self-cleared the instant the triggering
        # reading recovered - a brief emergency-level spike (voltage or
        # temperature) would assert relay_cut_request/interlock for exactly
        # one tick and clear itself the next, silently. Scoped to HARD cuts
        # only, matching docs/12 §8's own researched distinction (emergency-
        # tier faults latch; derate-tier/soft responses release
        # automatically) - soft cuts (capacity_empty) keep auto-clearing.
        # Cleared only via notify_session_start()/notify_charge_replug(),
        # called by RealtimeEngine on the two real-world conditions above -
        # no manual GUI "unlatch" control.
        self._hard_latched = False
        # AC charge-stop latch (added 2026-08-06, real bench log
        # minileaf_20260806_182106: with ac_cutoff_v bracketed to 3.64V, the
        # cutoff fired for ~1 tick, dropped charger_limit_kw to 0, the cell
        # relaxed back under 3.64V within a tick or two, and the unlatched
        # per-tick check below let full_charge_flag fall back to 0 and
        # charging resume - a repeating hunt, never actually stopping the
        # session. A deliberate charge-stop decision (either the per-cell
        # ac_cutoff_v trigger or the SoC-target-reached stop, same
        # full_charge_flag/-10kW convention) must stick once made, same
        # reasoning as `_hard_latched` above: give the real Leaf VCM time to
        # actually react and end the session instead of re-offering power the
        # instant the triggering reading recovers. Cleared only via
        # notify_session_start()/notify_charge_replug() - same two real-world
        # conditions as the hard-cut latch, "wait for the plug to be
        # reinserted to charge again" (user directive).
        self._ac_charge_stop_latched = False
        # Staleness-specific hard cut, THIS tick only (docs/13 item 14.3,
        # fixed 2026-08-03) - RealtimeEngine's ShutdownSequencer needs to
        # know specifically whether the CURRENT hard cut came from the
        # staleness watchdog, not just that some hard cut is active: the
        # 5th wind-down trigger is deliberately staleness-only (a genuine
        # "we can no longer safely read the battery" condition), never any
        # other emergency-tier cut. A voltage/temp/cross-check emergency
        # must latch relay_cut_request/interlock and keep the bridge running
        # and broadcasting that cut indefinitely - not wind down and go
        # silent on the Leaf bus, which would only make the emergency less
        # visible, not resolve it. User directive: "the bridge should not
        # wind down unless there is a real trigger to do so... a hard cut
        # should not stop the bridge... all fail safes are triggered and the
        # bridge still operates."
        self.staleness_hard_cut = False
        self.fault_log = FaultLog()
        # First-ever apply() call, i.e. the moment the bridge actually starts
        # running (docs/13 item 13.1, user directive 2026-08-03): a signal
        # that has NEVER arrived at all must not be treated as "nothing to
        # worry about" forever - it should start aging from the moment this
        # engine starts actively managing, same as a signal that went stale
        # after being live. See the staleness_watchdog block below.
        self._first_apply_time = None

    def notify_session_start(self):
        """Called by RealtimeEngine when the bridge begins a fresh session
        (ShutdownSequencer transitions waiting_for_wake -> startup) - the
        closest analog this bridge has to "the car being powered down and
        back on." Clears a latched hard cut and a latched AC charge-stop."""
        self._hard_latched = False
        self._ac_charge_stop_latched = False

    def notify_charge_replug(self):
        """Called by RealtimeEngine when a fresh charge request follows a
        period with none active - a genuine unplug/replug. Clears a latched
        hard cut and a latched AC charge-stop (the other real-world
        condition, alongside a fresh session start, that's allowed to)."""
        self._hard_latched = False
        self._ac_charge_stop_latched = False

    def apply(self, leaf_state, rz_state):
        """leaf_state: dict of Leaf output values already produced by
        DEFAULTS + the mapping engine. Returns a new dict with this layer's
        overrides applied (power-limit derating, soft/hard cut flags)."""
        out = dict(leaf_state)
        status = {}
        cfg = self.config

        now = time.monotonic()
        dt = (now - self._last_apply_time) if self._last_apply_time is not None else 0.0
        self._last_apply_time = now
        if self._first_apply_time is None:
            self._first_apply_time = now

        cell_min_raw = rz_state.get_input('cell_min')
        cell_max_raw = rz_state.get_input('cell_max')
        cells = [rz_state.get_input(k) for k in rz450e_signals.cell_voltage_keys()]
        cells = [c for c in cells if c is not None]
        worst_low = min(cells) if cells else cell_min_raw
        worst_high = max(cells) if cells else cell_max_raw
        soc = rz_state.get_input('soc_pct')
        temp_max = rz_state.get_input('temp_max')

        soft_cut = False
        hard_cut = False
        self.staleness_hard_cut = False   # recomputed below (docs/13 item 14.3)

        f = cfg['low_voltage_cutoff']
        if f['enabled']:
            # Cell voltage is the SOLE authoritative trigger (user directive,
            # 2026-07-31): real-time per-cell data determines every safety
            # cutoff. SoC is a BACKUP CHECK ONLY - it never independently
            # fires a cutoff on its own (an earlier version OR'd the two
            # together, which meant a low SoC reading alone could trigger
            # capacity_empty even with every cell perfectly healthy - fixed).
            # SoC is still evaluated every tick and surfaced in status: it
            # confirms the voltage-based decision when they agree, and
            # raises a visible (non-acting) warning when they disagree,
            # which is itself useful information (e.g. a SoC calibration
            # problem) without ever being allowed to act on its own.
            soc_low = soc is not None and soc <= f['min_soc_pct']
            if worst_low is not None and worst_low <= f['emergency_low_v']:
                hard_cut = True
                self._low_v_condition_since = None
                status['low_voltage_cutoff'] = f'EMERGENCY hard cut ({worst_low:.3f}V <= {f["emergency_low_v"]}V)'
            elif worst_low is not None and worst_low <= f['min_cell_v']:
                # Persistence window (docs/12 finding F5): require the
                # condition to hold continuously for soft_cut_persistence_s
                # before latching capacity_empty - guards against a single-
                # tick sag transient under a spike load. The discharge power
                # taper (same zero-point by default) is already collapsing
                # current/sag on its own faster ramp, so this window is a
                # backstop, not the primary defense.
                if self._low_v_condition_since is None:
                    self._low_v_condition_since = now
                held = now - self._low_v_condition_since
                if held >= f['soft_cut_persistence_s']:
                    soft_cut = True
                    agree = ' (SoC backup check agrees)' if soc_low else ' (SoC backup check does not confirm)'
                    status['low_voltage_cutoff'] = (
                        f'soft cut active - cell {worst_low:.3f}V <= {f["min_cell_v"]}V{agree if soc is not None else ""}')
                else:
                    status['low_voltage_cutoff'] = (
                        f'condition present {held:.1f}s/{f["soft_cut_persistence_s"]:.1f}s before soft cut '
                        f'latches (transient-sag guard) - cell {worst_low:.3f}V <= {f["min_cell_v"]}V')
            elif soc_low:
                self._low_v_condition_since = None
                cell_display = f'{worst_low:.3f}V' if worst_low is not None else 'no data'
                status['low_voltage_cutoff'] = (
                    f'SoC backup check reads {soc:.1f}% <= {f["min_soc_pct"]}% floor, but per-cell voltage '
                    f'({cell_display}) does not confirm - cell voltage is authoritative, NOT cutting off; '
                    f'check SoC calibration')
            else:
                self._low_v_condition_since = None
                status['low_voltage_cutoff'] = 'ok'

            # Fault log (docs/14): records every trigger/clear, independent
            # of the cut's own auto-clear behavior - see bridge/fault_log.py.
            self.fault_log.update('low_voltage_emergency', 'Low-voltage EMERGENCY hard cut (per-cell)',
                                  'hard', worst_low is not None and worst_low <= f['emergency_low_v'],
                                  status['low_voltage_cutoff'])
            self.fault_log.update('low_voltage_soft', 'Low-voltage soft cut (capacity_empty)',
                                  'soft', soft_cut, status['low_voltage_cutoff'])
        else:
            status['low_voltage_cutoff'] = 'disabled'
            _clear_disabled_feature(self.fault_log, [
                ('low_voltage_emergency', 'Low-voltage EMERGENCY hard cut (per-cell)', 'hard'),
                ('low_voltage_soft', 'Low-voltage soft cut (capacity_empty)', 'soft')])

        f = cfg['discharge_power_taper']
        if f['enabled']:
            if worst_low is not None:
                instant_factor = _ramp_factor(worst_low, f['taper_zero_v'], f['taper_start_v'])
            else:
                instant_factor = 1.0

            # Hysteresis: fast attack (snap down immediately on a dip - cell
            # protection can't wait for a slow ramp), slow release (rate-
            # limited recovery back to full power, avoids power hunting if
            # voltage bounces near the threshold under intermittent load).
            if instant_factor < self._discharge_factor_applied:
                self._discharge_factor_applied = instant_factor
            elif instant_factor > self._discharge_factor_applied:
                max_step = dt / max(f['recovery_ramp_s'], 1e-6)
                self._discharge_factor_applied = min(instant_factor, self._discharge_factor_applied + max_step)

            # Floor/ceiling (added 2026-08-08, docs/16 audit - see
            # default_config()'s own comment for the default-value rationale):
            # `max(floor_kw, ...)` guards against a misconfigured max_kw <
            # min_kw (the sanity check above only warns, per
            # _check_config_sanity()'s own docstring - it doesn't block
            # anything) or a mapping-tie baseline naturally below the floor,
            # either of which would otherwise make (ceiling - floor) negative
            # and push output BELOW the floor at high factor values.
            floor_kw, max_kw = f['discharge_min_kw'], f['discharge_max_kw']
            ceiling_kw = max(floor_kw, min(out.get('discharge_limit_kw', 0.0), max_kw))
            out['discharge_limit_kw'] = floor_kw + (ceiling_kw - floor_kw) * self._discharge_factor_applied
            if worst_low is not None:
                status['discharge_power_taper'] = (
                    f'applied factor={self._discharge_factor_applied:.2f} (instant={instant_factor:.2f}, '
                    f'worst cell {worst_low:.3f}V - full >= {f["taper_start_v"]}V, zero <= {f["taper_zero_v"]}V)')
            else:
                status['discharge_power_taper'] = (
                    f'applied factor={self._discharge_factor_applied:.2f} (no per-cell voltage data yet)')
        else:
            status['discharge_power_taper'] = 'disabled'
            self._discharge_factor_applied = 1.0   # docs/13 item 14.5 - don't resume a stale ramped-down factor if re-enabled later

        f = cfg['charge_target_taper']
        if f['enabled']:
            # REGEN ONLY (split 2026-08-01, see default_config()'s comment
            # for the full rationale) - drives ONLY charge_limit_kw, the
            # Leaf's shared "Charge/regen power limit" (docs/03), active
            # regardless of SoC or charging context (regen happens while
            # driving, not just plugged in). worst_high is the max of all 96
            # individually read cell voltages, not a pack-level summary.
            floor_kw, max_kw = f['regen_min_kw'], f['regen_max_kw']
            if worst_high is not None and worst_high >= f['emergency_high_v']:
                hard_cut = True
                self._regen_factor_applied = 0.0
                cell_factor = 0.0
                cell_status = f'EMERGENCY hard cut ({worst_high:.3f}V >= {f["emergency_high_v"]}V)'
                # Literal zero - a floor must NOT keep feeding power into an
                # overvoltage emergency (added 2026-08-08 alongside
                # regen_min_kw/regen_max_kw - matches the sibling
                # ac_charge_taper feature's own emergency branch, which also
                # unconditionally zeroes its output, bypassing ac_min_kw).
                out['charge_limit_kw'] = 0.0
            else:
                if worst_high is not None:
                    instant_factor = 1.0 - _ramp_factor(worst_high, f['regen_full_v'], f['regen_zero_v'])
                    cell_status = (
                        f'proactive regen taper factor={instant_factor:.2f} (worst cell '
                        f'{worst_high:.3f}V - full <= {f["regen_full_v"]}V, zero >= {f["regen_zero_v"]}V)')
                else:
                    instant_factor = 1.0
                    cell_status = 'no per-cell voltage data yet - full power'
                # Hysteresis (added 2026-08-01, user request: "regen we
                # should add some hysteresis? same as discharge?") - same
                # fast-attack/slow-release pattern as discharge_power_taper.
                if instant_factor < self._regen_factor_applied:
                    self._regen_factor_applied = instant_factor
                elif instant_factor > self._regen_factor_applied:
                    max_step = dt / max(f['recovery_ramp_s'], 1e-6)
                    self._regen_factor_applied = min(instant_factor, self._regen_factor_applied + max_step)
                cell_factor = self._regen_factor_applied
                # Floor/ceiling (added 2026-08-08, docs/16 audit) - see
                # discharge_power_taper's own comment above for the
                # max(floor_kw, ...) guard's rationale.
                ceiling_kw = max(floor_kw, min(out.get('charge_limit_kw', 0.0), max_kw))
                out['charge_limit_kw'] = floor_kw + (ceiling_kw - floor_kw) * cell_factor

            status['charge_target_taper'] = cell_status + f' | applied factor={cell_factor:.2f}'

            self.fault_log.update('overvoltage_emergency', 'Cell overvoltage EMERGENCY hard cut - regen (per-cell)',
                                  'hard', worst_high is not None and worst_high >= f['emergency_high_v'], cell_status)
        else:
            status['charge_target_taper'] = 'disabled'
            self._regen_factor_applied = 1.0   # docs/13 item 14.5 - don't resume a stale ramped-down factor if re-enabled later
            _clear_disabled_feature(self.fault_log, [
                ('overvoltage_emergency', 'Cell overvoltage EMERGENCY hard cut - regen (per-cell)', 'hard')])

        # AC-charger taper (split out 2026-08-01 from the old combined
        # charge_target_taper - see default_config()'s comment). Config
        # lives in state.charge_emulation / the Charge Emulation GUI tab,
        # not `cfg`, since it's specifically about the charger, not a
        # general battery-management threshold - `rz_state` here IS the
        # SharedState instance (RealtimeEngine passes self.state), so
        # .charge_emulation is directly available. Drives ONLY
        # charger_limit_kw ("Max power for charger," docs/03) and owns the
        # daily/extended AC SoC target + full_charge_flag, both still gated
        # on charge_permission_input (only ever mattered while plugged in).
        ac_cfg = rz_state.charge_emulation
        target = ac_cfg['extended_target_pct'] if ac_cfg.get('extended_mode') else ac_cfg['daily_target_pct']
        # RZ450e's confirmed charging interlock (0x358) - the user's own
        # design intent for this signal ("if not active, a charge request
        # must not proceed", docs/02). None/missing is treated as NOT
        # charging (safe default - never assert a charge-stop we can't
        # actually confirm the context for).
        charging_active = bool(rz_state.get_input('charge_permission_input'))
        ac_min_kw = ac_cfg.get('ac_min_kw', 0.0)

        if not ac_cfg.get('ac_taper_enabled', True):
            status['ac_charge_taper'] = 'disabled'
            # docs/13 item 14.5 - don't resume stale convergence state if
            # re-enabled later; no need to touch _ac_applied_kw itself, the
            # full-power pass-through branch below resyncs it the moment
            # this feature is next active.
            self.ac_uprate_level = None
            _clear_disabled_feature(self.fault_log, [
                ('ac_overvoltage_emergency', 'Cell overvoltage EMERGENCY hard cut - AC charger (per-cell)', 'hard'),
                ('ac_cutoff_stop', 'AC charger stop-charging cutoff reached (per-cell)', 'warn')])
        elif not charging_active:
            # Fully separate control path from driving (user directive,
            # 2026-08-07: "there are 2 different ways of controlling the
            # output. one under driving and one under charging, those are
            # handled differently by different algorithms... Per-cell regen
            # power limit for driving and AC charger overvoltage taper for
            # AC charging. that's a different control means all together").
            # Matches the real reference hardware too (Leaf_BMS_Emulator,
            # confirmed bit-level diff of idle vs. real charge-session
            # captures): LB_MAX_POWER_FOR_CHARGER sits fixed at the 1023/
            # 92.3kW idle placeholder whenever not actually charging, only
            # becoming a live taper-managed value during a genuine charge
            # session. Previously this whole block (taper AND its own
            # ac_emergency_v hard cut) ran every tick regardless of
            # charging_active, letting an AC-charging-specific feature
            # reduce or even hard-cut "Max power for charger" while simply
            # driving with nothing plugged in - driving-mode overvoltage
            # protection is entirely charge_target_taper's job (the regen
            # taper right above this block), not this one's.
            self.ac_uprate_level = None   # same precedent as the disabled-feature branch below - resyncs on next active tick
            cutoff_active = self._ac_charge_stop_latched
            status['ac_charge_taper'] = (
                'not actively charging per RZ450e interlock - AC taper inactive, "Max power for '
                "charger\" left at its mapped/idle value (driving-mode overvoltage protection is "
                "charge_target_taper's per-cell regen power limit, above, not this feature)")
            self.fault_log.update(
                'ac_overvoltage_emergency', 'Cell overvoltage EMERGENCY hard cut - AC charger (per-cell)',
                'hard', False, status['ac_charge_taper'])
            self.fault_log.update(
                'ac_cutoff_stop', 'AC charger stop-charging cutoff reached (per-cell)',
                'warn', cutoff_active, status['ac_charge_taper'])
        else:
            if worst_high is not None and worst_high >= ac_cfg['ac_emergency_v']:
                hard_cut = True
                self._ac_applied_kw = 0.0
                self.ac_uprate_level = None   # a real emergency stop, not a rate-controlled convergence state
                tapered_kw = 0.0
                ac_status = f'EMERGENCY hard cut ({worst_high:.3f}V >= {ac_cfg["ac_emergency_v"]}V)'
            else:
                ramped_kw = out.get('charger_limit_kw', 0.0)
                if worst_high is not None:
                    instant_factor = 1.0 - _ramp_factor(worst_high, ac_cfg['ac_full_v'], ac_cfg['ac_min_v'])
                    ac_status = (
                        f'AC charger taper factor={instant_factor:.2f} (worst cell {worst_high:.3f}V - '
                        f'full <= {ac_cfg["ac_full_v"]}V, min >= {ac_cfg["ac_min_v"]}V, floor {ac_min_kw:g}kW)')
                else:
                    instant_factor = 1.0
                    ac_status = 'no per-cell voltage data yet - full power'

                if instant_factor >= 1.0 or ramped_kw <= ac_min_kw:
                    # Full power (nothing to converge to) or the ramp's own
                    # pre-taper value hasn't reached the floor yet - pass
                    # through untouched and keep _ac_applied_kw tracking it
                    # 1:1 (2026-08-06), so there's no stale lag the instant
                    # the taper DOES need to engage, and the ramp's own
                    # startup precision is never fought by this taper.
                    self._ac_applied_kw = ramped_kw
                    self.ac_uprate_level = None
                    tapered_kw = ramped_kw
                else:
                    # Dynamically-selected convergence rate (added
                    # 2026-08-06, replacing a fixed-time-constant hysteresis
                    # added earlier the same day - see
                    # _select_ac_uprate_level()'s own comment for the full
                    # rationale: a real bench test showed a repeating
                    # full-cycle hunt, including a same-tick multi-kW jump,
                    # because instant response in either direction is the
                    # wrong model for a CC-CV charging control loop). Always
                    # starts at level 7 the moment convergence begins (user
                    # directive), then downshifts/upshifts (hysteresis on
                    # the switch itself) as the remaining distance narrows/
                    # grows - gentler the closer it gets to the target,
                    # never overshoots (the step is clamped to `remaining`
                    # itself so it lands exactly on target instead of
                    # asymptotically approaching forever).
                    instant_target_kw = ac_min_kw + (ramped_kw - ac_min_kw) * instant_factor
                    remaining = instant_target_kw - self._ac_applied_kw
                    if self.ac_uprate_level is None:
                        self._ac_current_level = 7   # "always start at #7" - fresh entry into the convergence window
                    level = _select_ac_uprate_level(abs(remaining), self._ac_current_level)
                    self._ac_current_level = level
                    self.ac_uprate_level = level
                    rate_kw_s = 2.0 / (2 ** (7 - level))   # same doubling formula as CHG_RAMP_RAW_PER_S, in kW/s
                    max_step = rate_kw_s * dt
                    step = _clamp(remaining, -max_step, max_step)
                    self._ac_applied_kw += step
                    tapered_kw = self._ac_applied_kw
                    ac_status += (f' | target={instant_target_kw:.2f}kW, applied={tapered_kw:.2f}kW '
                                  f'(level {level})')

            # Only reaches here while charging_active (see the not-charging
            # branch above, added 2026-08-07) - a real, RZ450e-authorized
            # charge session, so overriding charger_limit_kw is this
            # feature's own field to control, not a conflict with driving.
            out['charger_limit_kw'] = tapered_kw

            self.fault_log.update(
                'ac_overvoltage_emergency', 'Cell overvoltage EMERGENCY hard cut - AC charger (per-cell)',
                'hard', worst_high is not None and worst_high >= ac_cfg['ac_emergency_v'], ac_status)

            # Stop-charging cutoff (added 2026-08-06, user directive:
            # "make a new value called cutoff or stop charging for a set
            # voltage... the charger should stop triggering the full
            # bit [on its own reaction to 0kW]" - see CHARGE_SLIDERS'
            # ac_cutoff_v comment). A deliberate, controlled stop distinct
            # from the emergency hard cut above: same full_charge_flag/
            # -10kW convention as the SoC-target-reached stop right below,
            # so the session ends explicitly instead of relying on the
            # vehicle reacting to a near-zero power request on its own.
            cutoff_now = worst_high is not None and worst_high >= ac_cfg['ac_cutoff_v']
            target_now = soc is not None and soc >= target
            # Latch (added 2026-08-06, real bench log - see
            # _ac_charge_stop_latched's own comment in __init__): once
            # either trigger fires THIS tick, keep asserting the stop on
            # every subsequent tick even if the triggering reading itself
            # (worst_high sagging back under ac_cutoff_v the instant power
            # drops to 0, or a SoC re-read) goes back below its threshold,
            # until notify_session_start()/notify_charge_replug() clears it -
            # identical mechanism to `_hard_latched` above, just without the
            # contactor drop (this is a controlled stop, not an emergency).
            if cutoff_now or target_now:
                self._ac_charge_stop_latched = True
            if self._ac_charge_stop_latched:
                out['full_charge_flag'] = 1
                out['charge_limit_kw'] = 0.0
                out['charger_limit_kw'] = -10.0
                if cutoff_now:
                    ac_status += (f' | AC stop-charging cutoff reached ({worst_high:.3f}V >= '
                                  f'{ac_cfg["ac_cutoff_v"]}V) - full_charge_flag latched')
                elif target_now:
                    ac_status += f' | AC charge target {target:.0f}% reached - full_charge_flag latched'
                else:
                    ac_status += (' | AC charge stop latched (session ended - unplug/replug '
                                  'to resume)')
            status['ac_charge_taper'] = ac_status

            self.fault_log.update(
                'ac_cutoff_stop', 'AC charger stop-charging cutoff reached (per-cell)',
                'warn', self._ac_charge_stop_latched, status['ac_charge_taper'])

        f = cfg['over_temperature_derate']
        if f['enabled'] and temp_max is None:
            # Parity fix (docs/13 item 13.7, added 2026-08-03): previously
            # this whole feature just went silent with no status and no
            # fault_log entries when temp_max was missing - unlike every
            # voltage-based feature, which explicitly reports "no per-cell
            # voltage data yet" even with zero data (see docs/13 item 13.1
            # for the separate, already-addressed question of what factor
            # gets APPLIED with no data - this fix is purely about making
            # "no temperature data" visible, not changing that behavior).
            status['over_temperature_derate'] = 'no temperature data yet - full power (see docs/13 13.1)'
            for key, label in (
                    ('over_temp_emergency', 'Over-temperature EMERGENCY hard cut (hottest probe)'),
                    ('charge_cold_block', 'Charge/regen blocked - coldest probe at/below freezing'),
                    ('discharge_temp_zero', 'Discharge power at zero - over-temperature'),
                    ('charge_temp_zero', 'Charge/regen power at zero - over-temperature')):
                self.fault_log.update(key, label, 'warn' if key != 'over_temp_emergency' else 'hard',
                                      False, status['over_temperature_derate'])
        elif f['enabled'] and temp_max is not None:
            # Cold-side decisions use the COLDEST probe (docs/12 finding F1,
            # fixed 2026-07-31) - lithium plating happens in the coldest
            # cells, so testing temp_max here would let charging continue
            # into a partly-frozen pack as long as the warmest corner reads
            # above the block temp. Falls back to temp_max only if temp_min
            # truly isn't available - both decode from the same 0x4A7 frame,
            # so this is a same-frame fallback, not an independent
            # assume-it's-fine default.
            temp_min = rz_state.get_input('temp_min')
            cold_ref = temp_min if temp_min is not None else temp_max

            if temp_max >= f['emergency_temp_c']:
                hard_cut = True
                d_factor = 0.0
                c_factor = 0.0
                status['over_temperature_derate'] = (
                    f'EMERGENCY hard cut - hottest probe {temp_max:.1f}C >= {f["emergency_temp_c"]}C')
            else:
                d_factor = 1.0 - _ramp_factor(temp_max, f['discharge_derate_start_c'], f['discharge_hard_stop_c'])

                # Cold-side derate (docs/12 finding F3): ramps charge/regen
                # acceptance down approaching 0C instead of a single on/off
                # block at the freezing line - our real exposure here is
                # regen into a cold-soaked pack, not the 0.09C AC charger.
                cold_factor = _ramp_factor(cold_ref, f['charge_low_block_c'], f['charge_derate_low_start_c'])
                hot_factor = 1.0 - _ramp_factor(temp_max, f['charge_derate_start_c'], f['charge_hard_stop_c'])
                c_factor = min(cold_factor, hot_factor)

                status['over_temperature_derate'] = (
                    f'discharge_factor={d_factor:.2f}, charge_factor={c_factor:.2f} '
                    f'(coldest probe {cold_ref:.1f}C, hottest probe {temp_max:.1f}C)')

            out['discharge_limit_kw'] = out.get('discharge_limit_kw', 0.0) * d_factor
            out['charge_limit_kw'] = out.get('charge_limit_kw', 0.0) * c_factor
            out['charger_limit_kw'] = out.get('charger_limit_kw', 0.0) * c_factor

            self.fault_log.update('over_temp_emergency', 'Over-temperature EMERGENCY hard cut (hottest probe)',
                                  'hard', temp_max >= f['emergency_temp_c'], status['over_temperature_derate'])
            self.fault_log.update('charge_cold_block', 'Charge/regen blocked - coldest probe at/below freezing',
                                  'warn', cold_ref <= f['charge_low_block_c'], status['over_temperature_derate'])
            self.fault_log.update('discharge_temp_zero', 'Discharge power at zero - over-temperature',
                                  'warn', d_factor <= 0.0, status['over_temperature_derate'])
            self.fault_log.update('charge_temp_zero', 'Charge/regen power at zero - over-temperature',
                                  'warn', c_factor <= 0.0, status['over_temperature_derate'])
        elif not f['enabled']:
            status['over_temperature_derate'] = 'disabled'
            _clear_disabled_feature(self.fault_log, [
                ('over_temp_emergency', 'Over-temperature EMERGENCY hard cut (hottest probe)', 'hard'),
                ('charge_cold_block', 'Charge/regen blocked - coldest probe at/below freezing', 'warn'),
                ('discharge_temp_zero', 'Discharge power at zero - over-temperature', 'warn'),
                ('charge_temp_zero', 'Charge/regen power at zero - over-temperature', 'warn')])

        f = cfg['cell_imbalance_monitor']
        if f['enabled']:
            if worst_high is not None and worst_low is not None:
                delta = worst_high - worst_low
                if delta >= f['warn_delta_v']:
                    status['cell_imbalance_monitor'] = (
                        f'WARNING - spread {delta * 1000:.0f}mV >= {f["warn_delta_v"] * 1000:.0f}mV '
                        f'(worst high {worst_high:.3f}V, worst low {worst_low:.3f}V) - monitor only, '
                        f'no cutoff action')
                else:
                    status['cell_imbalance_monitor'] = f'ok (spread {delta * 1000:.0f}mV)'
            else:
                status['cell_imbalance_monitor'] = 'no per-cell voltage data yet'

            self.fault_log.update('cell_imbalance_warn', 'Cell imbalance warning (spread)', 'warn',
                                  worst_high is not None and worst_low is not None
                                  and (worst_high - worst_low) >= f['warn_delta_v'],
                                  status['cell_imbalance_monitor'])
        else:
            status['cell_imbalance_monitor'] = 'disabled'
            _clear_disabled_feature(self.fault_log, [('cell_imbalance_warn', 'Cell imbalance warning (spread)', 'warn')])

        f = cfg['overcurrent_monitor']
        if f['enabled']:
            current = rz_state.get_input('current')
            discharge_warn_active = False
            charge_warn_active = False
            if current is None:
                status['overcurrent_monitor'] = 'no current data yet'
                self._overcurrent_since = None
                self._overcurrent_direction = None
            else:
                discharging = current > 0
                magnitude = abs(current)
                direction = 'discharge' if discharging else 'charge'
                warn_a = f['continuous_discharge_warn_a'] if discharging else f['continuous_charge_warn_a']
                # 0x023 saturates at +/-204.7A (docs/02) - well below this
                # pack's real physical capability (bench setup has never
                # discharged past 200A; the real pack is fuse-rated to 500A
                # and the factory RZ450E outputs up to 230kW/~660A in short
                # bursts, per user 2026-07-31) - so a reading at/near
                # saturation means "at/above this value, true magnitude
                # unknown," not a precise number, and this monitor can never
                # see the pack's real high-current range at all.
                sat_note = (' - NOTE: at/near 0x023 sensor saturation (+/-204.7A), true magnitude above '
                            'this is unknown (this pack is rated well past this - see docs/02)') if magnitude >= 204.0 else ''

                if magnitude >= warn_a:
                    if self._overcurrent_since is None or self._overcurrent_direction != direction:
                        self._overcurrent_since = now
                        self._overcurrent_direction = direction
                    held = now - self._overcurrent_since
                    if held >= f['persistence_s']:
                        if discharging:
                            discharge_warn_active = True
                        else:
                            charge_warn_active = True
                        status['overcurrent_monitor'] = (
                            f'WARNING - sustained {direction} current {magnitude:.1f}A >= {warn_a}A for '
                            f'{held:.1f}s (monitor only, no cutoff action){sat_note}')
                    else:
                        status['overcurrent_monitor'] = (
                            f'{direction} current {magnitude:.1f}A elevated, {held:.1f}s/'
                            f'{f["persistence_s"]:.1f}s before warning{sat_note}')
                else:
                    self._overcurrent_since = None
                    self._overcurrent_direction = None
                    status['overcurrent_monitor'] = f'ok ({direction} {magnitude:.1f}A){sat_note}'

            self.fault_log.update('overcurrent_discharge_warn', 'Overcurrent warning - discharge', 'warn',
                                  discharge_warn_active, status['overcurrent_monitor'])
            self.fault_log.update('overcurrent_charge_warn', 'Overcurrent warning - charge/regen', 'warn',
                                  charge_warn_active, status['overcurrent_monitor'])
        else:
            status['overcurrent_monitor'] = 'disabled'
            self._overcurrent_since = None
            self._overcurrent_direction = None
            _clear_disabled_feature(self.fault_log, [
                ('overcurrent_discharge_warn', 'Overcurrent warning - discharge', 'warn'),
                ('overcurrent_charge_warn', 'Overcurrent warning - charge/regen', 'warn')])

        sanity_violations = _check_config_sanity(cfg, rz_state.charge_emulation)
        status['config_sanity'] = 'ok' if not sanity_violations else '; '.join(sanity_violations)
        self.fault_log.update(
            'config_sanity', 'Battery-management config has an inverted/nonsensical threshold ordering',
            'warn', bool(sanity_violations), status['config_sanity'])

        # Input plausibility rejections (docs/05, added 2026-08-01) - was
        # always on with no enable flag; given a real toggle (docs/13 item
        # 15.14, user directive 2026-08-03: "we should add enable disable
        # for our app for testing... default to on") so a corrupted/synthetic
        # test value can deliberately be pushed through unfiltered to confirm
        # the rest of the pipeline handles it, and so it's visible as a real
        # feature (checkbox + status line) rather than invisible hardcoded
        # logic. Default ON - this is a genuine safety net, not something to
        # casually leave off. RealtimeEngine._ingest_validated() reads this
        # same cfg['input_validation']['enabled'] flag to decide whether to
        # actually run rz450e_signals.validate_inputs() at all - disabling
        # here doesn't just hide the fault light, it stops the rejection
        # from happening upstream too.
        if cfg['input_validation']['enabled']:
            recent_rejections = rz_state.recent_rejections()
            if recent_rejections:
                status['input_validation'] = 'REJECTED (implausible): ' + '; '.join(
                    f'{k}={v!r}' for k, v in recent_rejections.items())
            else:
                status['input_validation'] = 'ok'
            self.fault_log.update('input_validation_reject', 'Input plausibility check rejected a value', 'warn',
                                  bool(recent_rejections), status['input_validation'])
        else:
            status['input_validation'] = 'disabled'
            _clear_disabled_feature(self.fault_log, [
                ('input_validation_reject', 'Input plausibility check rejected a value', 'warn')])

        # Checksum failures (docs/13 item 13.5, added 2026-08-03) - same
        # live-fault_log pattern as input_validation_reject above, tracking
        # rz450e_signals.frame_checksum_ok() rejections instead. Also given a
        # real toggle (docs/13 item 15.15, same user directive as above,
        # default ON) - RealtimeEngine._ingest_rz_bus() reads cfg[
        # 'checksum_validation']['enabled'] to decide whether to actually run
        # the checksum check at all.
        if cfg['checksum_validation']['enabled']:
            recent_checksum_failures = rz_state.recent_checksum_failures()
            if recent_checksum_failures:
                status['checksum_validation'] = 'REJECTED (checksum mismatch): ' + '; '.join(
                    f'0x{arb_id:03X} x{count}' for arb_id, count in recent_checksum_failures.items())
            else:
                status['checksum_validation'] = 'ok'
            self.fault_log.update('checksum_reject', 'Toyota checksum validation rejected a corrupt frame', 'warn',
                                  bool(recent_checksum_failures), status['checksum_validation'])
        else:
            status['checksum_validation'] = 'disabled'
            _clear_disabled_feature(self.fault_log, [
                ('checksum_reject', 'Toyota checksum validation rejected a corrupt frame', 'warn')])

        f = cfg['cell_data_cross_check']
        if f['enabled']:
            cross_soft_active = False
            cross_hard_active = False
            if cells and cell_min_raw is not None and cell_max_raw is not None:
                delta = max(abs(worst_low - cell_min_raw), abs(worst_high - cell_max_raw))
                if delta >= f['max_delta_v']:
                    if self._cross_check_since is None:
                        self._cross_check_since = now
                    held = now - self._cross_check_since
                    if held >= f['soft_cut_s']:
                        soft_cut = True
                        cross_soft_active = True
                        if held - f['soft_cut_s'] >= f['hard_escalation_s']:
                            hard_cut = True
                            cross_hard_active = True
                            status['cell_data_cross_check'] = (
                                f'MISMATCH {delta * 1000:.0f}mV vs 0x020 pack summary for {held:.0f}s - '
                                f'escalated to HARD cut')
                        else:
                            status['cell_data_cross_check'] = (
                                f'MISMATCH {delta * 1000:.0f}mV vs 0x020 pack summary for {held:.0f}s - soft cut')
                    else:
                        status['cell_data_cross_check'] = (
                            f'mismatch {delta * 1000:.0f}mV present {held:.1f}s/{f["soft_cut_s"]:.0f}s before '
                            f'soft cut latches')
                else:
                    self._cross_check_since = None
                    status['cell_data_cross_check'] = f'ok (delta {delta * 1000:.0f}mV)'
            else:
                self._cross_check_since = None
                status['cell_data_cross_check'] = 'no data to cross-check yet'

            self.fault_log.update(
                'cell_data_mismatch', 'Cell data cross-check mismatch (per-cell vs 0x020 pack summary)',
                'soft', cross_soft_active, status['cell_data_cross_check'])
            self.fault_log.update(
                'cell_data_mismatch_hard', 'Cell data cross-check mismatch - hard cut escalation',
                'hard', cross_hard_active, status['cell_data_cross_check'])
        else:
            status['cell_data_cross_check'] = 'disabled'
            self._cross_check_since = None
            _clear_disabled_feature(self.fault_log, [
                ('cell_data_mismatch', 'Cell data cross-check mismatch (per-cell vs 0x020 pack summary)', 'soft'),
                ('cell_data_mismatch_hard', 'Cell data cross-check mismatch - hard cut escalation', 'hard')])

        # Temperature data cross-check (docs/13 item 16.2, added 2026-08-04) -
        # same soft->hard escalation pattern as cell_data_cross_check above,
        # applied to temperature: compares 0x4A7's pack-level temp_max/
        # temp_min against the actual min/max of all 16 individually-read
        # temp probes (0x4AA). See default_config()'s comment for the full
        # rationale - this specifically protects the cold-side plating-
        # prevention logic (over_temperature_derate, keyed on temp_min) from
        # a decode/mux fault that individually-plausible-range checks alone
        # can't catch.
        f = cfg['temp_data_cross_check']
        if f['enabled']:
            temp_soft_active = False
            temp_hard_active = False
            probe_temps = [rz_state.get_input(k) for k in rz450e_signals.temp_probe_keys()]
            probe_temps = [t for t in probe_temps if t is not None]
            if probe_temps and temp_max is not None:
                temp_min_for_check = rz_state.get_input('temp_min')
                probe_max = max(probe_temps)
                probe_min = min(probe_temps)
                delta = abs(temp_max - probe_max)
                if temp_min_for_check is not None:
                    delta = max(delta, abs(temp_min_for_check - probe_min))
                if delta >= f['max_delta_c']:
                    if self._temp_cross_check_since is None:
                        self._temp_cross_check_since = now
                    held = now - self._temp_cross_check_since
                    if held >= f['soft_cut_s']:
                        soft_cut = True
                        temp_soft_active = True
                        if held - f['soft_cut_s'] >= f['hard_escalation_s']:
                            hard_cut = True
                            temp_hard_active = True
                            status['temp_data_cross_check'] = (
                                f'MISMATCH {delta:.1f}F vs 0x4A7 pack-extremes summary for {held:.0f}s - '
                                f'escalated to HARD cut')
                        else:
                            status['temp_data_cross_check'] = (
                                f'MISMATCH {delta:.1f}F vs 0x4A7 pack-extremes summary for {held:.0f}s - soft cut')
                    else:
                        status['temp_data_cross_check'] = (
                            f'mismatch {delta:.1f}F present {held:.1f}s/{f["soft_cut_s"]:.0f}s before '
                            f'soft cut latches')
                else:
                    self._temp_cross_check_since = None
                    status['temp_data_cross_check'] = f'ok (delta {delta:.1f}F)'
            else:
                self._temp_cross_check_since = None
                status['temp_data_cross_check'] = 'no data to cross-check yet'

            self.fault_log.update(
                'temp_data_mismatch', 'Temperature data cross-check mismatch (0x4A7 extremes vs 0x4AA per-probe)',
                'soft', temp_soft_active, status['temp_data_cross_check'])
            self.fault_log.update(
                'temp_data_mismatch_hard', 'Temperature data cross-check mismatch - hard cut escalation',
                'hard', temp_hard_active, status['temp_data_cross_check'])
        else:
            status['temp_data_cross_check'] = 'disabled'
            self._temp_cross_check_since = None
            _clear_disabled_feature(self.fault_log, [
                ('temp_data_mismatch', 'Temperature data cross-check mismatch (0x4A7 extremes vs 0x4AA per-probe)', 'soft'),
                ('temp_data_mismatch_hard', 'Temperature data cross-check mismatch - hard cut escalation', 'hard')])

        f = cfg['staleness_watchdog']
        if f['enabled']:
            # Covers EVERY registered input signal (docs/06 section 3, user
            # directive 2026-08-01) - all 96 per-cell voltages, all 16 temp
            # probes, and every fast/slow scalar - not just a hand-picked
            # subset. A key that has never been seen this session (age None)
            # is excluded, same "never seen != stale" principle as before
            # (docs/06 section 0) - only a key that WAS live and then stopped
            # updating counts as stale. Batched into one lock acquisition
            # (SharedState.ages_of) rather than 100+ separate age_of() calls
            # per tick.
            # "Never seen at all this session" is now treated as aging from
            # the moment THIS bridge started actively running (docs/13 item
            # 13.1, user directive 2026-08-03) rather than excluded forever -
            # a signal that has never arrived is a strictly WORSE case than
            # one that went stale after being live, and must hit the same
            # 60s/65s soft/hard cut, not sit outside the watchdog's view
            # indefinitely. `since_start` anchors that clock to this
            # engine's own first apply() call, which only starts once the
            # sequencer reaches 'startup' (real bus wake) - see
            # RealtimeEngine._tx_loop.
            since_start = now - self._first_apply_time
            ages_by_key = rz_state.ages_of(rz450e_signals.INPUT_SIGNAL_KEYS)
            worst_age = 0.0
            worst_key = None
            for key, a in ages_by_key.items():
                effective = a if a is not None else since_start
                if effective > worst_age:
                    worst_age = effective
                    worst_key = key if a is not None else f'{key} (never seen)'
            for ck in ('alive_3f1', 'alive_358', 'counter_5s'):
                a = rz_state.counter_stale_age(ck)
                effective = a if a is not None else since_start
                if effective > worst_age:
                    worst_age = effective
                    worst_key = f'counter:{ck}' if a is not None else f'counter:{ck} (never seen)'
            stale_soft_active = False
            stale_hard_active = False
            if worst_age >= f['soft_cut_s']:
                if self._stale_since is None:
                    self._stale_since = time.monotonic()
                soft_cut = True
                stale_soft_active = True
                # Stale safety-relevant input data must not just soft-cut -
                # it must also stop charging outright (user directive
                # 2026-08-01, reaffirmed 2026-08-03: "it should hard stop
                # and trigger the stop charging flag"): we can no longer
                # verify it's safe to keep accepting charge/regen if the
                # data behind that decision is stale, so force an explicit
                # charge-stop here rather than relying on capacity_empty
                # alone. full_charge_flag added 2026-08-03 alongside the
                # existing charge_limit_kw/charger_limit_kw zeroing - this
                # is also what RealtimeEngine._apply_charge_ramp() sets for
                # every other "Leaf wants to charge but something is
                # blocking it" mismatch, so staleness now uses the exact
                # same real-hardware-confirmed "instant stop, needs a fresh
                # charge request to retry" bit instead of a different,
                # weaker response. Scoped to THIS feature only (not every
                # soft cut - e.g. a low-voltage soft cut should NOT block
                # charging, which is exactly what a depleted pack needs).
                out['charge_limit_kw'] = 0.0
                out['charger_limit_kw'] = -10.0
                out['full_charge_flag'] = 1
                if time.monotonic() - self._stale_since >= f['hard_escalation_s']:
                    hard_cut = True
                    stale_hard_active = True
                    self.staleness_hard_cut = True
                    status['staleness_watchdog'] = (
                        f'STALE {worst_age:.0f}s ({worst_key}) - escalated to HARD cut, charging stopped')
                else:
                    status['staleness_watchdog'] = (
                        f'STALE {worst_age:.0f}s ({worst_key}) - soft cut, charging stopped')
            else:
                self._stale_since = None
                status['staleness_watchdog'] = f'ok (worst age {worst_age:.1f}s, {worst_key or "n/a"})'

            self.fault_log.update('staleness_soft', 'Staleness watchdog - soft cut (stale data)', 'soft',
                                  stale_soft_active, status['staleness_watchdog'])
            self.fault_log.update('staleness_hard', 'Staleness watchdog - hard cut escalation', 'hard',
                                  stale_hard_active, status['staleness_watchdog'])
        else:
            status['staleness_watchdog'] = 'disabled'
            self._stale_since = None
            _clear_disabled_feature(self.fault_log, [
                ('staleness_soft', 'Staleness watchdog - soft cut (stale data)', 'soft'),
                ('staleness_hard', 'Staleness watchdog - hard cut escalation', 'hard')])

        # Explicit clear, not just conditional set (docs/13 item 16.3, fixed
        # 2026-08-04): capacity_empty/relay_cut_request/interlock each have
        # exactly ONE authority within this function (soft_cut and
        # _hard_latched respectively) - unlike full_charge_flag, which is
        # legitimately also set by RealtimeEngine._apply_charge_ramp() in a
        # different module/moment and so must NOT be force-cleared here (see
        # that field's own note - an unconditional clear would race against
        # and undo a legitimate charge-ramp-mismatch stop). Previously these
        # three were only ever forced TOWARD their cut state, never back
        # in the other direction - relying entirely on leaf_signals.DEFAULTS
        # (0/0/1) as the implicit "clear" value. That was already correct
        # AS LONG AS nothing else ever wrote to these fields - which stopped
        # being guaranteed the moment they were removed as Signal Mapping
        # targets this same fix (leaf_signals.MANAGEMENT_EXCLUSIVE_KEYS):
        # closing the mapping-target exposure and making this function the
        # sole, explicit, unconditional authority over these three fields
        # together fully close the gap, not just one half of it.
        out['capacity_empty'] = 1 if soft_cut else 0
        # Latching (added 2026-08-01, see __init__'s comment): a hard cut
        # THIS tick sets the latch; the latch, once set, keeps asserting the
        # cut on every subsequent tick regardless of whether `hard_cut`
        # itself goes back to False, until notify_session_start()/
        # notify_charge_replug() clears it.
        if hard_cut:
            self._hard_latched = True
        if self._hard_latched:
            out['relay_cut_request'] = 3
            out['interlock'] = 0
            status['hard_cut_latch'] = 'LATCHED - clears on a fresh session start or charger replug'
        else:
            out['relay_cut_request'] = 0
            out['interlock'] = 1
        # Fault log entry for the LATCH itself (bug fix, 2026-08-01, found by
        # an independent review pass): every individual hard-tier fault_log
        # entry above (low_voltage_emergency, overvoltage_emergency, etc.)
        # is intentionally still keyed on its own INSTANTANEOUS condition -
        # that's genuinely useful ("did this specific trigger recur?") - but
        # none of them reflect that the CUT ITSELF is still latched once its
        # own trigger has recovered. Without this entry, the Fault History
        # window would show every entry "cleared" while the vehicle is
        # still hard-cut - actively misleading, not just incomplete.
        self.fault_log.update('hard_cut_latch', 'Hard cut LATCHED (relay_cut_request/interlock still asserted)',
                              'hard', self._hard_latched, status.get('hard_cut_latch', 'not latched'))

        self.status = status
        return out

    def to_dict(self):
        return self.config

    @classmethod
    def from_dict(cls, d):
        cfg = default_config()
        for feature, values in (d or {}).items():
            if feature in cfg:
                # °F -> °C key migration (2026-08-09) must run before the
                # "only pull in keys the current schema still defines" check
                # below, or an old _f-suffixed key would just look like dead
                # config and be silently dropped instead of translated.
                values = _migrate_temp_f_keys(feature, values)
                # Only pull in keys the current schema still defines - a saved
                # profile from an older revision may carry fields that have
                # since been removed (e.g. taper_start_pct, removed rev 2)
                # and blindly merging them back in would silently resurrect
                # dead config forever.
                for key, value in values.items():
                    if key not in cfg[feature]:
                        continue
                    # Bounds clamp (docs/13 items 13.3/13.9, fixed
                    # 2026-08-03): a hand-edited or corrupted profile.json
                    # must not be able to set a threshold to an arbitrary
                    # value just because it skips the GUI's own clamp - see
                    # FEATURE_FIELD_BOUNDS above. A value that can't even be
                    # coerced to a number (corruption, wrong type) is
                    # dropped entirely, leaving the safe default in place,
                    # rather than written through as-is.
                    bounds = FEATURE_FIELD_BOUNDS.get((feature, key))
                    if bounds is not None:
                        try:
                            value = max(bounds[0], min(bounds[1], float(value)))
                        except (TypeError, ValueError):
                            continue
                    cfg[feature][key] = value
        return cls(cfg)
