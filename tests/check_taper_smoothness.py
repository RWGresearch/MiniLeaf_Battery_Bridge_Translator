"""Standalone diagnostic (check_*.py, not pass/fail - see CLAUDE.md): plots how
smooth or jumpy two safety features' kW output looks under different voltage
conditions, using bridge.management_engine.ManagementEngine.apply() with real
elapsed time between calls (same faked-monotonic-clock technique as
check_ac_taper_log_replay.py) so recovery_ramp_s hysteresis math runs exactly
as it would live:

1. AC charge taper (predictable, monotonically-rising voltage during a real
   charge session) - expected to be smooth.
2. Discharge power taper (voltage that sags under load, synthetically noisy)
   - expected to be jumpy, using the current profile's own taper_start_v/
   taper_min_v (3.3V/3.0V).
3. A REPLAY of the real cell_min/cell_max trace captured in
   logs/minileaf_20260809_080913-power and regen settings test.trc, run
   through the discharge/regen tapers using the EXACT settings that session's
   own _log_output.txt companion file recorded (taper_start_v=3.62V/
   taper_min_v=3.60V discharge, regen_full_v=3.61V/regen_min_v=3.63V regen -
   a deliberately narrow 20mV window for that test, NOT the current profile's
   wider one) - answers "how jumpy was the real output really."
4. The same real trace re-run at several recovery_ramp_s values, to see what
   more hysteresis on the recovery side would (and wouldn't) have done to it -
   the recovery-side ramp never affects the DROP, which is deliberately
   instant (cell protection can't wait) - see management_engine.py's own
   "fast attack, slow release" comments.

Usage: py tests/check_taper_smoothness.py
Writes PNGs to logs/taper_smoothness_*.png
"""
import random
import sys
import time as time_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from bridge.management_engine import ManagementEngine
from bridge.state import SharedState
from bridge.trc_log import read_trc_rows
from bridge import rz450e_signals

LOGS_DIR = Path(__file__).resolve().parent.parent / 'logs'
REAL_LOG = LOGS_DIR / 'minileaf_20260809_080913-power and regen settings test.trc'

DEFAULT_LEAF_STATE = {
    'discharge_limit_kw': 0.0, 'charge_limit_kw': 0.0, 'charger_limit_kw': 0.0,
    'relay_cut_request': 0, 'interlock': 1, 'capacity_empty': 0, 'full_charge_flag': 0,
}


def _fresh_engine(only_feature=None):
    """A ManagementEngine with every cfg feature disabled except
    `only_feature` (input_validation/checksum_validation left alone - they
    don't touch kW outputs), so the plotted curve isolates exactly the one
    mechanism under test."""
    eng = ManagementEngine()
    for name in eng.config:
        if name in ('input_validation', 'checksum_validation'):
            continue
        eng.config[name]['enabled'] = (name == only_feature)
    return eng


def jumpiness_stats(t, kw):
    """Total variation (sum of |delta| between consecutive samples) and worst
    single-tick jump - two simple numeric stand-ins for "how jumpy," to put a
    number on what the plot shows."""
    diffs = [abs(kw[i] - kw[i - 1]) for i in range(1, len(kw))]
    total_variation = sum(diffs)
    worst = max(diffs) if diffs else 0.0
    reversals = sum(1 for i in range(2, len(kw))
                     if (kw[i] - kw[i - 1]) * (kw[i - 1] - kw[i - 2]) < 0)
    return total_variation, worst, reversals


LSB_V = 5.0 / 4096.0   # raw sensor quantization for cell_min/cell_max/cell_XX (rz450e_signals.py decode_020/decode_frame) - fixed by the RZ450e's own 12-bit/5V CAN encoding, not adjustable from this bridge


# ── 0. Quantization staircase: is the jump really the input LSB, not the
# output encoding? Sweeps voltage ONE RAW ADC COUNT AT A TIME (the finest
# possible input step - no synthetic noise at all) through the real engine,
# for both the Aug-9 test session's narrow 20mV window and the current
# profile's wider 300mV one, to show the per-count jump directly rather than
# just computing it analytically. ───────────────────────────────────────────
def scenario_quantization_staircase():
    configs = [
        ('Aug-9 test session (20mV window)', dict(taper_start_v=3.62, taper_min_v=3.60,
                                                     discharge_min_kw=0.0, discharge_max_kw=110.0)),
        ('current profile.json (300mV window)', dict(taper_start_v=3.30, taper_min_v=3.00,
                                                        discharge_min_kw=3.0, discharge_max_kw=110.0)),
    ]
    fig, axes = plt.subplots(len(configs), 1, figsize=(10, 4 * len(configs)))
    print('\n[quantization staircase] one-raw-ADC-count-at-a-time sweep, no noise at all:')
    for ax, (label, cfg) in zip(axes, configs):
        start_v, zero_v = cfg['taper_start_v'], cfg['taper_min_v']
        start_count = round(start_v / LSB_V)
        zero_count = round(zero_v / LSB_V)
        counts = list(range(start_count + 3, zero_count - 3, -1))   # step down one raw count at a time
        eng = _fresh_engine(only_feature='discharge_power_taper')
        eng.config['discharge_power_taper'].update(cfg)
        eng.config['discharge_power_taper']['recovery_ramp_s'] = 1e-9   # irrelevant here (monotonic falling, attack path only) - keep negligible so it can't mask the step size
        rz = SharedState()
        rz.update_input('temp_max', 25.0); rz.update_input('temp_min', 25.0)
        rz.update_input('current', 0.0); rz.update_input('charge_permission_input', 0.0)
        fake_now = [0.0]
        time_module.monotonic = lambda: fake_now[0]
        kw = []
        for i, c in enumerate(counts):
            fake_now[0] = i * 1.0   # 1s apart - plenty of time for recovery_ramp_s to be a non-factor
            v = c * LSB_V
            rz.update_input('cell_min', v); rz.update_input('cell_max', v)
            leaf_state = dict(DEFAULT_LEAF_STATE)
            leaf_state['discharge_limit_kw'] = cfg['discharge_max_kw']
            out = eng.apply(leaf_state, rz)
            kw.append(out['discharge_limit_kw'])
        steps = [kw[i] - kw[i - 1] for i in range(1, len(kw)) if abs(kw[i] - kw[i - 1]) > 1e-6]
        avg_step = sum(abs(s) for s in steps) / len(steps) if steps else 0.0
        predicted = (cfg['discharge_max_kw'] - cfg['discharge_min_kw']) / (start_v - zero_v) * LSB_V
        print(f'  {label}: measured avg |step| = {avg_step:.3f}kW  (predicted = {predicted:.3f}kW, '
              f'{len(steps)} stepped ticks out of {len(kw)})')
        ax.step([c * LSB_V for c in counts], kw, where='post', color='tab:red', marker='.')
        ax.set_title(f'{label} - discharge_limit_kw per single raw ADC count (1.22mV) of input change')
        ax.set_xlabel('cell voltage (V) - each point is exactly one raw ADC count apart')
        ax.set_ylabel('discharge_limit_kw')
    fig.tight_layout()
    out_path = LOGS_DIR / 'taper_smoothness_quantization_staircase.png'
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f'  -> {out_path}')


# ── 1. AC charge taper: smooth, monotonic voltage rise ─────────────────────
def scenario_ac_charge_sweep():
    ac = dict(charge_emulate=1, ac_taper_enabled=1, ac_full_v=4.13, ac_min_v=4.17,
              ac_cutoff_v=4.18, ac_emergency_v=4.2, ac_min_kw=1.0, ac_max_kw=8.0)
    eng = _fresh_engine(only_feature=None)   # AC taper isn't gated by cfg[...] - see ac_charge_taper's own block
    rz = SharedState()
    rz.charge_emulation.update(ac)
    rz.update_input('charge_permission_input', 1.0)
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)

    duration_s, hz = 300.0, 10.0
    n = int(duration_s * hz)
    fake_now = [0.0]
    time_module.monotonic = lambda: fake_now[0]

    t, v, kw, level = [], [], [], []
    for i in range(n):
        ti = i / hz
        # Linear rise from 3.90V to just past ac_cutoff_v (4.18V) over the window.
        vi = 3.90 + (ac['ac_cutoff_v'] + 0.02 - 3.90) * (ti / duration_s)
        fake_now[0] = ti
        rz.update_input('cell_min', vi)
        rz.update_input('cell_max', vi)
        leaf_state = dict(DEFAULT_LEAF_STATE)
        leaf_state['charger_limit_kw'] = ac['ac_max_kw']   # simulate the AC ramp already at its ceiling
        out = eng.apply(leaf_state, rz)
        t.append(ti); v.append(vi); kw.append(out['charger_limit_kw']); level.append(eng.ac_uprate_level)

    tv, worst, rev = jumpiness_stats(t, kw)
    print(f'[AC charge sweep] total_variation={tv:.3f}kW  worst_tick_jump={worst:.4f}kW  '
          f'direction_reversals={rev}  (settings: ac_full_v={ac["ac_full_v"]}, '
          f'ac_min_v={ac["ac_min_v"]}, ac_cutoff_v={ac["ac_cutoff_v"]})')

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    ax1.plot(t, v, color='tab:blue')
    ax1.axhline(ac['ac_full_v'], color='green', ls='--', lw=0.8, label='ac_full_v')
    ax1.axhline(ac['ac_min_v'], color='orange', ls='--', lw=0.8, label='ac_min_v')
    ax1.axhline(ac['ac_cutoff_v'], color='red', ls='--', lw=0.8, label='ac_cutoff_v')
    ax1.set_ylabel('cell voltage (V)'); ax1.legend(fontsize=8); ax1.set_title(
        'AC charge taper - smooth synthetic voltage sweep (3.90V -> past cutoff)')
    ax2.plot(t, kw, color='tab:purple')
    ax2.set_ylabel('charger_limit_kw'); ax2.set_xlabel('time (s)')
    ax2b = ax2.twinx()
    ax2b.plot(t, level, color='tab:gray', alpha=0.4, drawstyle='steps-post')
    ax2b.set_ylabel('ac_uprate_level (0-7)', color='tab:gray')
    fig.tight_layout()
    out_path = LOGS_DIR / 'taper_smoothness_ac_charge_sweep.png'
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f'  -> {out_path}')


# ── 2. Discharge power taper: noisy, load-sagging voltage ──────────────────
def _noisy_discharge_voltage(t, rng, taper_start_v, taper_min_v):
    """Baseline sits mid-taper-window; sporadic ~0.2V sag pulses simulate a
    load spike, each lasting a fraction of a second, matching the user's
    description ("voltage dropped .2V sporadically during the discharge")."""
    mid = (taper_start_v + taper_min_v) / 2.0
    v = mid + 0.02 * rng.uniform(-1, 1)   # small continuous ripple
    pulse_until = 0.0
    pulse_depth = 0.0
    out = []
    for ti in t:
        if ti >= pulse_until and rng.random() < 0.02:   # ~2%/tick chance to start a new sag pulse
            pulse_depth = rng.uniform(0.12, 0.22)
            pulse_until = ti + rng.uniform(0.2, 0.8)
        depth = pulse_depth if ti < pulse_until else 0.0
        vi = mid + 0.02 * rng.uniform(-1, 1) - depth
        out.append(vi)
    return out


def scenario_discharge_noisy(recovery_ramp_values=(3.0, 8.0, 20.0)):
    taper_start_v, taper_min_v = 3.3, 3.0   # current profile.json values
    duration_s, hz = 60.0, 10.0
    n = int(duration_s * hz)
    t = [i / hz for i in range(n)]
    rng = random.Random(42)
    v = _noisy_discharge_voltage(t, rng, taper_start_v, taper_min_v)

    fig, axes = plt.subplots(len(recovery_ramp_values) + 1, 1, figsize=(10, 3 + 2.2 * len(recovery_ramp_values)),
                              sharex=True)
    axes[0].plot(t, v, color='tab:blue')
    axes[0].axhline(taper_start_v, color='green', ls='--', lw=0.8, label='taper_start_v (full power)')
    axes[0].axhline(taper_min_v, color='red', ls='--', lw=0.8, label='taper_min_v (zero power)')
    axes[0].set_ylabel('cell voltage (V)'); axes[0].legend(fontsize=8)
    axes[0].set_title('Discharge power taper - synthetic sporadic 0.12-0.22V load-sag pulses')

    for ax, rr in zip(axes[1:], recovery_ramp_values):
        eng = _fresh_engine(only_feature='discharge_power_taper')
        eng.config['discharge_power_taper'].update(
            taper_start_v=taper_start_v, taper_min_v=taper_min_v,
            discharge_min_kw=3.0, discharge_max_kw=110.0, recovery_ramp_s=rr)
        rz = SharedState()
        rz.update_input('temp_max', 25.0); rz.update_input('temp_min', 25.0)
        rz.update_input('current', 0.0); rz.update_input('charge_permission_input', 0.0)

        fake_now = [0.0]
        time_module.monotonic = lambda: fake_now[0]
        kw = []
        for ti, vi in zip(t, v):
            fake_now[0] = ti
            rz.update_input('cell_min', vi); rz.update_input('cell_max', vi)
            leaf_state = dict(DEFAULT_LEAF_STATE)
            leaf_state['discharge_limit_kw'] = 110.0
            out = eng.apply(leaf_state, rz)
            kw.append(out['discharge_limit_kw'])

        tv, worst, rev = jumpiness_stats(t, kw)
        print(f'[Discharge noisy, recovery_ramp_s={rr:5.1f}] total_variation={tv:8.2f}kW  '
              f'worst_tick_jump={worst:.3f}kW  direction_reversals={rev}')
        ax.plot(t, kw, color='tab:red')
        ax.set_ylabel(f'discharge_limit_kw\n(recovery_ramp_s={rr:g}s)')
    axes[-1].set_xlabel('time (s)')
    fig.tight_layout()
    out_path = LOGS_DIR / 'taper_smoothness_discharge_noisy.png'
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f'  -> {out_path}')


# ── 3/4. Real captured log replay ───────────────────────────────────────────
def _load_real_cell_trace(path, t_start, t_end):
    """Streams the real .trc, decoding every rz450e-bus (CAN bus 1) 0x020
    (pack cell_min/cell_max summary) RX frame in [t_start, t_end) relative to
    the capture start. This file is ~2.7 hours/17.7M lines - a cheap fixed-
    substring pre-check (bus 1, Rx, ID 0020) skips every other line before
    the row regex ever runs, same idea as `grep -F` prefiltering, so this
    stays plain Python without shelling out. Still stops reading the instant
    t_end is passed - no need to scan past the window actually plotted."""
    from bridge.trc_log import _TRC_ROW_RE
    t, cmin, cmax = [], [], []
    t0 = None
    needle = ' 1  Rx        0020 -'
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if needle not in line:
                continue
            m = _TRC_ROW_RE.match(line)
            if not m:
                continue
            offset_ms, _bus_num, _typ, _id_hex, _dlc, data_str = m.groups()
            t_rel = float(offset_ms) / 1000.0
            if t0 is None:
                t0 = t_rel
            t_rel -= t0
            if t_rel < t_start:
                continue
            if t_rel > t_end:
                break
            d = [int(x, 16) for x in data_str.split()]
            vals = rz450e_signals.decode_020(d)
            if not vals:
                continue
            t.append(t_rel); cmin.append(vals['cell_min']); cmax.append(vals['cell_max'])
    return t, cmin, cmax


def scenario_real_log_replay():
    if not REAL_LOG.exists():
        print(f'[real log replay] SKIPPED - not found: {REAL_LOG}')
        return None

    # Settings transcribed from that session's own
    # "..._log_output.txt" settings snapshot (NOT the current profile.json -
    # this session used a deliberately narrow 20mV test window).
    discharge_cfg = dict(taper_start_v=3.62, taper_min_v=3.60, recovery_ramp_s=3.0,
                          discharge_min_kw=0.0, discharge_max_kw=110.0)
    regen_cfg = dict(regen_full_v=3.61, regen_min_v=3.63, emergency_high_v=4.2,
                      recovery_ramp_s=3.0, regen_min_kw=0.0, regen_max_kw=70.0)

    # Window chosen by scanning the full ~2.68hr session for where cell_min
    # actually spends time down near/below taper_min_v (3.60V) - the first
    # ~2 hours sit just above taper_start_v the whole time (discharge taper
    # never engages there, see this script's own earlier full-session scan),
    # only the final ~14 minutes dip into the active taper band.
    t_start, t_end = 8700.0, 9666.0
    print(f'\n[real log replay] reading {REAL_LOG.name} window t={t_start:g}s..{t_end:g}s ...')
    t, cmin, cmax = _load_real_cell_trace(REAL_LOG, t_start, t_end)
    print(f'  {len(t)} real 0x020 samples loaded  '
          f'(cell_min {min(cmin):.4f}..{max(cmin):.4f}V, cell_max {min(cmax):.4f}..{max(cmax):.4f}V)')

    def replay(recovery_ramp_s):
        eng = ManagementEngine()
        for name in eng.config:
            eng.config[name]['enabled'] = False
        eng.config['discharge_power_taper'].update(discharge_cfg)
        eng.config['discharge_power_taper']['recovery_ramp_s'] = recovery_ramp_s
        eng.config['discharge_power_taper']['enabled'] = True
        eng.config['charge_target_taper'].update(regen_cfg)
        eng.config['charge_target_taper']['recovery_ramp_s'] = recovery_ramp_s
        eng.config['charge_target_taper']['enabled'] = True
        rz = SharedState()
        rz.update_input('temp_max', 25.0); rz.update_input('temp_min', 25.0)
        rz.update_input('current', 0.0); rz.update_input('charge_permission_input', 0.0)
        fake_now = [0.0]
        time_module.monotonic = lambda: fake_now[0]
        d_kw, r_kw = [], []
        for ti, lo, hi in zip(t, cmin, cmax):
            fake_now[0] = ti
            rz.update_input('cell_min', lo); rz.update_input('cell_max', hi)
            leaf_state = dict(DEFAULT_LEAF_STATE)
            leaf_state['discharge_limit_kw'] = 110.0
            leaf_state['charge_limit_kw'] = 70.0
            out = eng.apply(leaf_state, rz)
            d_kw.append(out['discharge_limit_kw']); r_kw.append(out['charge_limit_kw'])
        return d_kw, r_kw

    d_kw, r_kw = replay(discharge_cfg['recovery_ramp_s'])
    tv_d, worst_d, rev_d = jumpiness_stats(t, d_kw)
    tv_r, worst_r, rev_r = jumpiness_stats(t, r_kw)
    print(f'  [discharge] total_variation={tv_d:8.1f}kW  worst_tick_jump={worst_d:.2f}kW  reversals={rev_d}')
    print(f'  [regen]     total_variation={tv_r:8.1f}kW  worst_tick_jump={worst_r:.2f}kW  reversals={rev_r}')

    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)
    axes[0].plot(t, cmin, label='cell_min (drives discharge taper)', color='tab:blue')
    axes[0].plot(t, cmax, label='cell_max (drives regen taper)', color='tab:orange')
    axes[0].axhline(discharge_cfg['taper_start_v'], color='green', ls=':', lw=0.8)
    axes[0].axhline(discharge_cfg['taper_min_v'], color='red', ls=':', lw=0.8)
    axes[0].axhline(regen_cfg['regen_full_v'], color='green', ls='--', lw=0.8)
    axes[0].axhline(regen_cfg['regen_min_v'], color='red', ls='--', lw=0.8)
    axes[0].set_ylabel('cell voltage (V)'); axes[0].legend(fontsize=8)
    axes[0].set_title(f'REAL captured trace ({REAL_LOG.name}), t={t_start:g}-{t_end:g}s - '
                       f'actual session settings (20mV taper windows)')
    axes[1].plot(t, d_kw, color='tab:red')
    axes[1].set_ylabel('discharge_limit_kw')
    axes[2].plot(t, r_kw, color='tab:green')
    axes[2].set_ylabel('charge_limit_kw (regen)'); axes[2].set_xlabel('time (s)')
    fig.tight_layout()
    out_path = LOGS_DIR / 'taper_smoothness_real_log_replay.png'
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f'  -> {out_path}')
    return t, cmin, cmax, discharge_cfg


def scenario_real_log_ramp_sensitivity(t, cmin, discharge_cfg):
    """Re-runs the SAME real captured cell_min trace at several
    recovery_ramp_s values - answers "would slower hysteresis smooth this
    out." The attack side stays instant regardless (deliberate - see
    management_engine.py's discharge_power_taper comment)."""
    ramp_values = (3.0, 8.0, 20.0, 45.0)
    fig, axes = plt.subplots(len(ramp_values) + 1, 1, figsize=(11, 3 + 2.0 * len(ramp_values)), sharex=True)
    axes[0].plot(t, cmin, color='tab:blue')
    axes[0].axhline(discharge_cfg['taper_start_v'], color='green', ls='--', lw=0.8, label='taper_start_v')
    axes[0].axhline(discharge_cfg['taper_min_v'], color='red', ls='--', lw=0.8, label='taper_min_v')
    axes[0].set_ylabel('cell_min (V)'); axes[0].legend(fontsize=8)
    axes[0].set_title('Real trace replayed at increasing recovery_ramp_s (discharge taper only)')

    print('\n[real log ramp sensitivity - discharge taper]')
    for ax, rr in zip(axes[1:], ramp_values):
        eng = ManagementEngine()
        for name in eng.config:
            eng.config[name]['enabled'] = False
        eng.config['discharge_power_taper'].update(discharge_cfg)
        eng.config['discharge_power_taper']['recovery_ramp_s'] = rr
        eng.config['discharge_power_taper']['enabled'] = True
        rz = SharedState()
        rz.update_input('temp_max', 25.0); rz.update_input('temp_min', 25.0)
        rz.update_input('current', 0.0); rz.update_input('charge_permission_input', 0.0)
        fake_now = [0.0]
        time_module.monotonic = lambda: fake_now[0]
        kw = []
        for ti, lo in zip(t, cmin):
            fake_now[0] = ti
            rz.update_input('cell_min', lo); rz.update_input('cell_max', lo)
            leaf_state = dict(DEFAULT_LEAF_STATE)
            leaf_state['discharge_limit_kw'] = 110.0
            out = eng.apply(leaf_state, rz)
            kw.append(out['discharge_limit_kw'])
        tv, worst, rev = jumpiness_stats(t, kw)
        print(f'  recovery_ramp_s={rr:5.1f}  total_variation={tv:8.1f}kW  worst_tick_jump={worst:.2f}kW  '
              f'reversals={rev}  pct_time_at_zero={100*sum(1 for k in kw if k <= 0.01)/len(kw):.1f}%  '
              f'pct_time_at_full={100*sum(1 for k in kw if k >= 109.9)/len(kw):.1f}%')
        ax.plot(t, kw, color='tab:red')
        ax.set_ylabel(f'discharge_limit_kw\n(recovery={rr:g}s)')
    axes[-1].set_xlabel('time (s)')
    fig.tight_layout()
    out_path = LOGS_DIR / 'taper_smoothness_real_log_ramp_sensitivity.png'
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f'  -> {out_path}')


if __name__ == '__main__':
    print('=== 0. Quantization staircase (one raw ADC count at a time, no noise) ===')
    scenario_quantization_staircase()
    print('\n=== 1. AC charge taper (smooth synthetic sweep) ===')
    scenario_ac_charge_sweep()
    print('\n=== 2. Discharge power taper (noisy synthetic sag pulses) ===')
    scenario_discharge_noisy()
    print('\n=== 3. Real captured log replay (actual session settings) ===')
    result = scenario_real_log_replay()
    if result:
        t, cmin, cmax, discharge_cfg = result
        print('\n=== 4. Real log replay - recovery_ramp_s sensitivity ===')
        scenario_real_log_ramp_sensitivity(t, cmin, discharge_cfg)
