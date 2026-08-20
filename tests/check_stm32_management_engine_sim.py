"""Multi-tick simulation cross-checking STM32_MiniLeaf_Bridge_Translator_uVision's
management_engine.c against the real bridge/management_engine.py ManagementEngine.

No ARM/x86 C toolchain is available in this dev environment (only a bare-
metal RISC-V-only clang, per the STM32 port session notes) so this does NOT
compile or execute management_engine.c itself. Instead it re-derives the
same control logic independently in Python (a from-scratch re-typing of the
C source, NOT importing bridge.management_engine's internals for the
algorithm) and drives BOTH implementations tick-by-tick through a long,
randomized-but-persistent synthetic scenario (so hysteresis/persistence-
window/latch behavior actually gets exercised, not just instantaneous
per-tick values), diffing their Leaf-bound outputs every tick.

Threshold VALUES are pulled directly from bridge.management_engine.
default_config()/bridge.state's charge_emulation defaults - the same source
tools/export_stm32_config.py generates bridge_config_gen.h from (already
separately confirmed identical by that script's own --check output) - so
this isolates ALGORITHM correctness, not a second transcription of ~80
numeric constants.

Two subtleties this harness deliberately gets right (each cost a real debug
session to find, so future edits to this file should preserve them):
1. A tick's raw reading of None means "no NEW reading," not "value unknown" -
   the real SharedState.get_input() (and the C port's RzSignal.value) both
   PERSIST the last real value until a fresh one arrives; only the AGE used
   by the staleness watchdog should reflect a gap. See SimState.observe().
2. SharedState.update_input(key, None) would still stamp a fresh timestamp
   (a real ingest path never calls it that way) - this harness's `feed()"
   skips the call entirely for a None reading, matching real ingest and the
   C port's rz450e_ingest.c only writing RzSignal.last_update_tick on an
   actual decode.

Run: py tests/check_stm32_management_engine_sim.py [--ticks N] [--seed S]
"""
import argparse
import os
import random
import sys
import time as time_mod

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge.management_engine as me_mod
import bridge.state as state_mod

# ── Shared fake clock, patched into the real `time` module process-wide ────
_fake_now = [0.0]


def fake_monotonic():
    return _fake_now[0]


time_mod.monotonic = fake_monotonic

CFG = me_mod.default_config()
_STATE0 = state_mod.SharedState()   # only used to read charge_emulation defaults
CE_CFG = dict(_STATE0.charge_emulation)


# ── Independent re-derivation of management_engine.c (hand-typed from the C
# source, not imported/copy-pasted) ─────────────────────────────────────────
def clampf(v, lo, hi):
    return max(lo, min(hi, v))


def ramp_factor(value, floor, ceiling):
    if ceiling <= floor:
        return 1.0 if value >= ceiling else 0.0
    return clampf((value - floor) / (ceiling - floor), 0.0, 1.0)


_AC_LEVEL_DOWNSHIFT_KW = (3.0, 1.5, 0.75, 0.4, 0.2, 0.1, 0.05)
_AC_LEVEL_HYSTERESIS_MULT = 1.5


def ac_threshold_for(level):
    if level <= 0:
        return 0.0
    return _AC_LEVEL_DOWNSHIFT_KW[7 - level]


def select_ac_uprate_level(remaining_kw_abs, current_level):
    level = current_level
    while level > 0 and remaining_kw_abs < ac_threshold_for(level):
        level -= 1
    while level < 7 and remaining_kw_abs >= ac_threshold_for(level + 1) * _AC_LEVEL_HYSTERESIS_MULT:
        level += 1
    return level


class SimState:
    def __init__(self):
        self.last_apply_t = None
        self.first_apply_t = None
        self.discharge_factor_applied = 1.0
        self.regen_factor_applied = 1.0
        self.ac_applied_kw = 0.0
        self.ac_current_level = 7
        self.ac_uprate_level = None
        self.low_v_since = None
        self.cell_cross_since = None
        self.temp_cross_since = None
        self.stale_since = None
        self.hard_latched = False
        self.ac_charge_stop_latched = False
        self.ac_charge_temp_stop_latched = False
        self.counters = {}
        # Per-key last-actually-seen timestamp AND last real value (mirrors
        # the real SharedState.get_input()/ages_of() - rz450e/rz450e_ts -
        # AND the C port's per-signal RzSignal.value/last_update_tick).
        self.last_seen = {}
        self.last_value = {}

    def observe(self, key, raw_value, now):
        """Returns (held_value, age_seconds) for `key` given this tick's raw
        reading (or None). held_value is None only if `key` has never had a
        real reading at all."""
        if raw_value is not None:
            self.last_seen[key] = now
            self.last_value[key] = raw_value
        held = self.last_value.get(key)
        age = (now - self.last_seen[key]) if key in self.last_seen else (now - self.first_apply_t)
        return held, age

    def counter_age(self, key, raw_value, now):
        """Mirrors SharedState.counter_stale_age(): a raw_value of None this
        tick means no frame carrying this counter arrived - continue
        tracking time since its last REAL change, don't reset to "never
        seen" just because this one tick had no frame."""
        last_value, last_change_t, seen = self.counters.get(key, (None, None, False))
        if raw_value is not None and (not seen or last_value != raw_value):
            last_value, last_change_t, seen = raw_value, now, True
            self.counters[key] = (last_value, last_change_t, seen)
        if not seen:
            return now - self.first_apply_t
        return now - last_change_t


def sim_apply(sim, rz, now, out):
    """rz: dict with 'cells' (list of 96 Optional[float]), 'cell_min',
    'cell_max', 'pack_v', 'temp_max', 'temp_min', 'temp_probes' (list of 16
    Optional[float]), 'current', 'current_b', 'soc', 'charge_permission_input',
    'alive_3f1', 'alive_358', 'counter_5s' (raw ints, or None if this tick
    has no frame for that counter at all)."""
    dt = (now - sim.last_apply_t) if sim.last_apply_t is not None else 0.0
    sim.last_apply_t = now
    if sim.first_apply_t is None:
        sim.first_apply_t = now

    cell_min_raw, age_cell_min = sim.observe('cell_min', rz['cell_min'], now)
    cell_max_raw, age_cell_max = sim.observe('cell_max', rz['cell_max'], now)
    cell_values, cell_ages = [], []
    for i, raw in enumerate(rz['cells']):
        v, a = sim.observe(f'cell_{i}', raw, now)
        cell_values.append(v)
        cell_ages.append(a)
    cells = [c for c in cell_values if c is not None]
    have_cells = bool(cells)
    cell_lo = min(cells) if cells else None
    cell_hi = max(cells) if cells else None
    worst_low = cell_lo if have_cells else cell_min_raw
    worst_high = cell_hi if have_cells else cell_max_raw
    soc, age_soc = sim.observe('soc', rz['soc'], now)
    temp_max, age_temp_max = sim.observe('temp_max', rz['temp_max'], now)
    temp_min, age_temp_min = sim.observe('temp_min', rz['temp_min'], now)
    temp_probe_values, temp_probe_ages = [], []
    for i, raw in enumerate(rz['temp_probes']):
        v, a = sim.observe(f'temp_probe_{i}', raw, now)
        temp_probe_values.append(v)
        temp_probe_ages.append(a)
    current, age_current = sim.observe('current', rz['current'], now)
    current_b, age_current_b = sim.observe('current_b', rz['current_b'], now)
    charge_permission_input, age_charge_permission_input = sim.observe(
        'charge_permission_input', rz['charge_permission_input'], now)
    pack_v, age_pack_v = sim.observe('pack_v', rz['pack_v'], now)

    soft_cut = False
    hard_cut = False

    f = CFG['low_voltage_cutoff']
    if f['enabled']:
        if worst_low is not None and worst_low <= f['emergency_low_v']:
            hard_cut = True
            sim.low_v_since = None
        elif worst_low is not None and worst_low <= f['min_cell_v']:
            if sim.low_v_since is None:
                sim.low_v_since = now
            if now - sim.low_v_since >= f['soft_cut_persistence_s']:
                soft_cut = True
        else:
            sim.low_v_since = None

    f = CFG['discharge_power_taper']
    if f['enabled']:
        v_factor = ramp_factor(worst_low, f['taper_min_v'], f['taper_start_v']) if worst_low is not None else 1.0
        soc_factor = ramp_factor(soc, f['taper_min_soc_pct'], f['taper_start_soc_pct']) if soc is not None else 1.0
        instant = min(v_factor, soc_factor)
        if instant < sim.discharge_factor_applied:
            sim.discharge_factor_applied = instant
        elif instant > sim.discharge_factor_applied:
            max_step = dt / max(f['recovery_ramp_s'], 1e-6)
            sim.discharge_factor_applied = min(instant, sim.discharge_factor_applied + max_step)
        floor_kw, max_kw = f['discharge_min_kw'], f['discharge_max_kw']
        ceiling_kw = max(floor_kw, min(out['discharge_limit_kw'], max_kw))
        out['discharge_limit_kw'] = floor_kw + (ceiling_kw - floor_kw) * sim.discharge_factor_applied
    else:
        sim.discharge_factor_applied = 1.0

    f = CFG['charge_target_taper']
    if f['enabled']:
        floor_kw, max_kw = f['regen_min_kw'], f['regen_max_kw']
        if worst_high is not None and worst_high >= f['emergency_high_v']:
            hard_cut = True
            sim.regen_factor_applied = 0.0
            out['charge_limit_kw'] = 0.0
        else:
            v_factor = 1.0 - ramp_factor(worst_high, f['regen_full_v'], f['regen_min_v']) if worst_high is not None else 1.0
            soc_factor = 1.0 - ramp_factor(soc, f['regen_full_soc_pct'], f['regen_min_soc_pct']) if soc is not None else 1.0
            instant = min(v_factor, soc_factor)
            if instant < sim.regen_factor_applied:
                sim.regen_factor_applied = instant
            elif instant > sim.regen_factor_applied:
                max_step = dt / max(f['recovery_ramp_s'], 1e-6)
                sim.regen_factor_applied = min(instant, sim.regen_factor_applied + max_step)
            ceiling_kw = max(floor_kw, min(out['charge_limit_kw'], max_kw))
            out['charge_limit_kw'] = floor_kw + (ceiling_kw - floor_kw) * sim.regen_factor_applied
    else:
        sim.regen_factor_applied = 1.0

    charging_active = bool(charge_permission_input)
    target = CE_CFG['extended_target_pct'] if CE_CFG.get('extended_mode') else CE_CFG['daily_target_pct']
    ac_min_kw = CE_CFG.get('ac_min_kw', 0.0)

    if not CE_CFG.get('ac_taper_enabled', True):
        sim.ac_uprate_level = None
    elif not charging_active:
        sim.ac_uprate_level = None
    else:
        if worst_high is not None and worst_high >= CE_CFG['ac_emergency_v']:
            hard_cut = True
            sim.ac_applied_kw = 0.0
            sim.ac_uprate_level = None
            tapered_kw = 0.0
        else:
            ramped_kw = out['charger_limit_kw']
            instant_factor = 1.0 - ramp_factor(worst_high, CE_CFG['ac_full_v'], CE_CFG['ac_min_v']) if worst_high is not None else 1.0
            if instant_factor >= 1.0 or ramped_kw <= ac_min_kw:
                sim.ac_applied_kw = ramped_kw
                sim.ac_uprate_level = None
                tapered_kw = ramped_kw
            else:
                instant_target_kw = ac_min_kw + (ramped_kw - ac_min_kw) * instant_factor
                remaining = instant_target_kw - sim.ac_applied_kw
                if sim.ac_uprate_level is None:
                    sim.ac_current_level = 7
                level = select_ac_uprate_level(abs(remaining), sim.ac_current_level)
                sim.ac_current_level = level
                sim.ac_uprate_level = level
                rate_kw_s = 2.0 / (2 ** (7 - level))
                max_step = rate_kw_s * dt
                step = clampf(remaining, -max_step, max_step)
                sim.ac_applied_kw += step
                tapered_kw = sim.ac_applied_kw
        out['charger_limit_kw'] = tapered_kw

        cutoff_now = worst_high is not None and worst_high >= CE_CFG['ac_cutoff_v']
        target_now = soc is not None and soc >= target
        if cutoff_now or target_now:
            sim.ac_charge_stop_latched = True
        if sim.ac_charge_stop_latched:
            out['full_charge_flag'] = 1
            out['charge_limit_kw'] = 0.0
            out['charger_limit_kw'] = -10.0

    if not CE_CFG.get('ac_temp_derate_enabled', True):
        pass
    elif not charging_active:
        pass
    elif temp_max is None:
        pass
    else:
        ac_cold_ref = temp_min if temp_min is not None else temp_max
        cold_factor = ramp_factor(ac_cold_ref, CE_CFG['ac_low_block_c'], CE_CFG['ac_derate_low_start_c'])
        hot_factor = 1.0 - ramp_factor(temp_max, CE_CFG['ac_derate_start_c'], CE_CFG['ac_hard_stop_c'])
        ac_temp_factor = min(cold_factor, hot_factor)
        out['charger_limit_kw'] = out['charger_limit_kw'] * ac_temp_factor
        if temp_max >= CE_CFG['ac_hard_stop_c']:
            sim.ac_charge_temp_stop_latched = True
        if sim.ac_charge_temp_stop_latched:
            out['full_charge_flag'] = 1
            out['charge_limit_kw'] = 0.0
            out['charger_limit_kw'] = -10.0

    f = CFG['over_temperature_derate']
    if f['enabled'] and temp_max is not None:
        cold_ref = temp_min if temp_min is not None else temp_max
        emergency = temp_max >= f['emergency_temp_c']
        if emergency:
            hard_cut = True
            d_factor = 0.0
            c_factor = 0.0
        else:
            d_factor = 1.0 - ramp_factor(temp_max, f['discharge_derate_start_c'], f['discharge_hard_stop_c'])
            cold_factor = ramp_factor(cold_ref, f['charge_low_block_c'], f['charge_derate_low_start_c'])
            hot_factor = 1.0 - ramp_factor(temp_max, f['charge_derate_start_c'], f['charge_hard_stop_c'])
            c_factor = min(cold_factor, hot_factor)
        out['discharge_limit_kw'] = out['discharge_limit_kw'] * d_factor
        out['charge_limit_kw'] = out['charge_limit_kw'] * c_factor
        if emergency:
            out['charger_limit_kw'] = 0.0

    f = CFG['cell_data_cross_check']
    if f['enabled']:
        if have_cells and cell_min_raw is not None and cell_max_raw is not None:
            delta = max(abs(cell_lo - cell_min_raw), abs(cell_hi - cell_max_raw))
            if delta >= f['max_delta_v']:
                if sim.cell_cross_since is None:
                    sim.cell_cross_since = now
                held = now - sim.cell_cross_since
                if held >= f['soft_cut_s']:
                    soft_cut = True
                    if held - f['soft_cut_s'] >= f['hard_escalation_s']:
                        hard_cut = True
            else:
                sim.cell_cross_since = None
        else:
            sim.cell_cross_since = None

    f = CFG['temp_data_cross_check']
    if f['enabled']:
        probes = [t for t in temp_probe_values if t is not None]
        if probes and temp_max is not None:
            probe_hi = max(probes)
            probe_lo = min(probes)
            delta = abs(temp_max - probe_hi)
            if temp_min is not None:
                delta = max(delta, abs(temp_min - probe_lo))
            if delta >= f['max_delta_c']:
                if sim.temp_cross_since is None:
                    sim.temp_cross_since = now
                held = now - sim.temp_cross_since
                if held >= f['soft_cut_s']:
                    soft_cut = True
                    if held - f['soft_cut_s'] >= f['hard_escalation_s']:
                        hard_cut = True
            else:
                sim.temp_cross_since = None
        else:
            sim.temp_cross_since = None

    # temp_probe_cross_check - stubbed (no DID data), matches C port Phase 4/5

    f = CFG['staleness_watchdog']
    if f['enabled']:
        worst_age = max(age_pack_v, age_cell_min, age_cell_max, age_temp_max, age_temp_min,
                         age_current, age_current_b, age_soc, age_charge_permission_input,
                         max(cell_ages), max(temp_probe_ages))
        for key in ('alive_3f1', 'alive_358', 'counter_5s'):
            worst_age = max(worst_age, sim.counter_age(key, rz[key], now))

        if worst_age >= f['soft_cut_s']:
            if sim.stale_since is None:
                sim.stale_since = now
            soft_cut = True
            out['charge_limit_kw'] = 0.0
            out['charger_limit_kw'] = -10.0
            out['full_charge_flag'] = 1
            if now - sim.stale_since >= f['hard_escalation_s']:
                hard_cut = True
        else:
            sim.stale_since = None

    out['capacity_empty'] = 1 if soft_cut else 0
    if hard_cut:
        sim.hard_latched = True
    if sim.hard_latched:
        out['relay_cut_request'] = 3
        out['interlock'] = 0
    else:
        out['relay_cut_request'] = 0
        out['interlock'] = 1

    return out


# ── Scenario generator + driver ─────────────────────────────────────────────
def new_phase(rnd):
    kind = rnd.choice([
        'normal', 'low_soft', 'low_emergency', 'regen_taper', 'ac_emergency',
        'hot_emergency', 'cold_block', 'cell_cross_mismatch', 'temp_cross_mismatch',
        'data_missing', 'overcurrent', 'ac_charging', 'target_soc',
    ])
    p = {'kind': kind}
    p['charge_permission_input'] = rnd.choice([0, 1, 1, None])

    if kind == 'low_soft':
        p['worst_low'] = rnd.uniform(2.65, 2.99)
        p['worst_high'] = rnd.uniform(3.5, 3.9)
    elif kind == 'low_emergency':
        p['worst_low'] = rnd.uniform(2.0, 2.59)
        p['worst_high'] = rnd.uniform(3.5, 3.9)
    elif kind == 'regen_taper':
        p['worst_low'] = rnd.uniform(3.3, 3.9)
        p['worst_high'] = rnd.uniform(4.0, 4.19)
    elif kind == 'ac_emergency':
        p['worst_low'] = rnd.uniform(3.3, 3.9)
        p['worst_high'] = rnd.uniform(4.20, 4.4)
        p['charge_permission_input'] = 1
    elif kind == 'hot_emergency':
        p['temp_max'] = rnd.uniform(61.0, 75.0)
        p['temp_min'] = rnd.uniform(10.0, 30.0)
    elif kind == 'cold_block':
        p['temp_max'] = rnd.uniform(10.0, 30.0)
        p['temp_min'] = rnd.uniform(-30.0, -0.5)
    elif kind == 'overcurrent':
        p['current'] = rnd.choice([rnd.uniform(151, 210), -rnd.uniform(31, 210)])
    elif kind == 'ac_charging':
        p['charge_permission_input'] = 1
        p['worst_low'] = rnd.uniform(3.5, 3.9)
        p['worst_high'] = rnd.uniform(3.6, 4.0)
    elif kind == 'target_soc':
        p['charge_permission_input'] = 1
        p['soc'] = rnd.uniform(80.5, 100.0)
        p['worst_low'] = rnd.uniform(3.5, 3.9)
        p['worst_high'] = rnd.uniform(3.6, 3.9)
    else:
        p['worst_low'] = rnd.uniform(3.2, 3.9)
        p['worst_high'] = rnd.uniform(3.6, 4.0)

    p.setdefault('worst_low', rnd.uniform(3.2, 3.9))
    p.setdefault('worst_high', max(p['worst_low'], rnd.uniform(3.6, 4.0)))
    p.setdefault('temp_max', rnd.uniform(10.0, 45.0))
    p.setdefault('temp_min', min(p['temp_max'], rnd.uniform(-5.0, 30.0)))
    p.setdefault('current', rnd.choice([rnd.uniform(-140, 140), None]))
    p.setdefault('soc', rnd.choice([rnd.uniform(5, 95), None]))

    p['cell_cross_mismatch'] = (kind == 'cell_cross_mismatch')
    p['temp_cross_mismatch'] = (kind == 'temp_cross_mismatch')
    p['data_missing'] = (kind == 'data_missing')
    p['baseline_discharge'] = rnd.choice([110.0, 40.0, 5.0])
    p['baseline_charge'] = rnd.choice([70.0, 30.0, 2.0])
    p['baseline_charger'] = rnd.choice([92.3, 6.6, 1.0])
    p['counters_frozen'] = rnd.random() < 0.15
    p['counters_missing'] = rnd.random() < 0.05
    return p


def build_rz(p, rnd, tick_counter):
    if p['data_missing']:
        return {
            'cells': [None] * 96, 'cell_min': None, 'cell_max': None, 'pack_v': None,
            'temp_max': None, 'temp_min': None, 'temp_probes': [None] * 16,
            'current': None, 'current_b': None, 'soc': None,
            'charge_permission_input': p['charge_permission_input'],
            'alive_3f1': None, 'alive_358': None, 'counter_5s': None,
        }

    cells = [3.70 + rnd.uniform(-0.01, 0.01) for _ in range(96)]
    cells[0] = p['worst_low']
    cells[1] = p['worst_high']
    cell_min, cell_max = p['worst_low'], p['worst_high']
    if p['cell_cross_mismatch']:
        cell_min = p['worst_low'] - 0.3
        cell_max = p['worst_high'] + 0.3

    temp_probes = [(p['temp_max'] + p['temp_min']) / 2.0 for _ in range(16)]
    temp_probes[0] = p['temp_min']
    temp_probes[1] = p['temp_max']
    temp_max, temp_min = p['temp_max'], p['temp_min']
    if p['temp_cross_mismatch']:
        temp_max = p['temp_max'] + 10.0
        temp_min = p['temp_min'] - 10.0

    if p['counters_missing']:
        a3f1 = a358 = c5s = None
    elif p['counters_frozen']:
        a3f1, a358, c5s = 5, 9, 40
    else:
        a3f1 = tick_counter % 16
        a358 = tick_counter % 16
        c5s = tick_counter % 73

    return {
        'cells': cells, 'cell_min': cell_min, 'cell_max': cell_max, 'pack_v': 378.5,
        'temp_max': temp_max, 'temp_min': temp_min, 'temp_probes': temp_probes,
        'current': p['current'], 'current_b': p['current'], 'soc': p['soc'],
        'charge_permission_input': p['charge_permission_input'],
        'alive_3f1': a3f1, 'alive_358': a358, 'counter_5s': c5s,
    }


def run(n_ticks=60000, seed=777, dt_min=0.01, dt_max=0.4, max_report=40):
    rnd = random.Random(seed)
    real_engine = me_mod.ManagementEngine(me_mod.default_config())
    real_state = state_mod.SharedState()
    real_state.charge_emulation.update(CE_CFG)
    sim = SimState()

    def feed(key, value):
        # Real ingest only ever calls update_input() on an actual decode -
        # never with None (which would still stamp a fresh timestamp).
        if value is not None:
            real_state.update_input(key, value)

    phase_ticks_left = 0
    phase = {}
    mismatches = []
    now = 0.0
    tick_counter = 0

    for tick in range(n_ticks):
        if phase_ticks_left <= 0:
            phase = new_phase(rnd)
            phase_ticks_left = rnd.randint(5, 400)
        phase_ticks_left -= 1
        tick_counter += 1

        now += rnd.uniform(dt_min, dt_max)
        _fake_now[0] = now

        rz = build_rz(phase, rnd, tick_counter)

        out_sim = {
            'discharge_limit_kw': phase['baseline_discharge'],
            'charge_limit_kw': phase['baseline_charge'],
            'charger_limit_kw': phase['baseline_charger'],
            'capacity_empty': 0, 'relay_cut_request': 0, 'interlock': 1, 'full_charge_flag': 0,
        }
        out_sim = sim_apply(sim, rz, now, out_sim)

        feed('pack_v', rz['pack_v'])
        feed('cell_min', rz['cell_min'])
        feed('cell_max', rz['cell_max'])
        for i, v in enumerate(rz['cells']):
            feed(f'cell_{i + 1:02d}', v)
        feed('temp_max', rz['temp_max'])
        feed('temp_min', rz['temp_min'])
        for i, v in enumerate(rz['temp_probes']):
            feed(f'temp_{i + 1:02d}', v)
        feed('current', rz['current'])
        feed('current_b', rz['current_b'])
        feed('soc_pct', rz['soc'])
        feed('charge_permission_input', rz['charge_permission_input'])
        for key in ('alive_3f1', 'alive_358'):
            if rz[key] is not None:
                real_state.note_counter(key, rz[key])
        if rz['counter_5s'] is not None:
            real_state.note_counter('counter_5s', rz['counter_5s'])

        # The real engine's staleness watchdog scans rz450e_signals.
        # INPUT_SIGNAL_KEYS, which also includes capacity_pack1-4_ah/
        # primary_pack_v/primary_current_a/temp_NN_did/temp_NN_can - all
        # DID-sourced fields that don't exist in the C port's RzState until
        # Phase 6 (see rz450e_ingest.h). Keep them always-fresh here so this
        # comparison isolates Phase 3/4's CURRENT scope (matching the C
        # port's own documented "PORT PHASE 6 TODO" staleness-scan gap)
        # rather than re-proving an already-known, already-documented scope
        # difference.
        for key in ('capacity_pack1_ah', 'capacity_pack2_ah', 'capacity_pack3_ah', 'capacity_pack4_ah',
                    'primary_pack_v', 'primary_current_a'):
            real_state.update_input(key, 1.0)
        for i, v in enumerate(rz['temp_probes']):
            feed(f'temp_{i + 1:02d}_did', v)
            feed(f'temp_{i + 1:02d}_can', v)

        leaf_state = {
            'discharge_limit_kw': phase['baseline_discharge'],
            'charge_limit_kw': phase['baseline_charge'],
            'charger_limit_kw': phase['baseline_charger'],
            'full_charge_flag': 0,
            'capacity_empty': 0, 'relay_cut_request': 0, 'interlock': 1,
        }
        out_real = real_engine.apply(leaf_state, real_state)

        for key in ('discharge_limit_kw', 'charge_limit_kw', 'charger_limit_kw'):
            if abs(out_sim[key] - out_real[key]) > 1e-6:
                mismatches.append((tick, phase['kind'], key, out_sim[key], out_real[key]))
        for key in ('capacity_empty', 'relay_cut_request', 'interlock', 'full_charge_flag'):
            if out_sim[key] != out_real[key]:
                mismatches.append((tick, phase['kind'], key, out_sim[key], out_real[key]))

        if len(mismatches) > max_report:
            break

    print(f"Ran {tick_counter} ticks (~{now:.0f}s simulated), {len(mismatches)} mismatches")
    for m in mismatches[:max_report]:
        print(" ", m)
    return len(mismatches) == 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticks', type=int, default=60000)
    parser.add_argument('--seed', type=int, default=777)
    args = parser.parse_args()
    ok = run(n_ticks=args.ticks, seed=args.seed)
    sys.exit(0 if ok else 1)
