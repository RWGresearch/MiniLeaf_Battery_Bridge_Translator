"""Standalone diagnostic (check_*.py, not pass/fail - see CLAUDE.md): replays
a real captured session's cell voltage AND SoC through the actual
discharge_power_taper/charge_target_taper (regen) using the EXACT settings
that session's own "..._log_output.txt" companion file recorded (auto-loaded,
not hand-transcribed - a real session's settings vary run to run), to see how
the SoC-primary/voltage-secondary blending (main.py Rev 69) actually behaved
on real data.

Also parses that same companion file's "Live log" mirror to find the
session's real `running`-phase window(s) and any logged connect/disconnect/
cut events, so the zoomed per-window plots and event annotations are derived
from the actual log instead of hand-transcribed per file (fixed after the
first run of this script hardcoded one specific session's window boundaries
and event list - a second real session immediately showed why that doesn't
generalize).

Perf note: a real capture can run 700MB-1GB+ (multi-hour sessions). A pure-
Python line-by-line scan of the whole file is noticeably slow at that size.
Shells out to `grep -E` first (a single pass, a few seconds even on a 1GB+
file - verified directly) to pre-filter to just the two CAN IDs this script
cares about (0x020 cell_min/max, 0x74F UDS DID responses), then parses that
much smaller subset in pure Python. Falls back to a pure-Python scan (via
bridge/trc_log.py's read_trc_rows) if grep isn't on PATH.

Usage: py tests/check_soc_taper_log_replay.py ["<path to .trc>"]
(defaults to the most-recently-modified "*discharge*regen*.trc"/"*_log_*.trc"
under logs/ if no path is given - see _find_default_log())
Writes PNGs to logs/soc_taper_replay_*.png
"""
import json
import re
import shutil
import subprocess
import sys
import time as time_module
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from bridge.management_engine import ManagementEngine
from bridge.state import SharedState
from bridge.trc_log import _TRC_ROW_RE, read_trc_rows
from bridge import rz450e_signals

LOGS_DIR = Path(__file__).resolve().parent.parent / 'logs'

DEFAULT_LEAF_STATE = {
    'discharge_limit_kw': 0.0, 'charge_limit_kw': 0.0, 'charger_limit_kw': 0.0,
    'relay_cut_request': 0, 'interlock': 1, 'capacity_empty': 0, 'full_charge_flag': 0,
}

# Fallback only - used if a .trc is pointed at with no companion
# "..._log_output.txt" (Log Output wasn't started that session). Real runs
# should always have the companion file; this just keeps the script from
# hard-crashing on an older/partial capture.
_FALLBACK_DISCHARGE_CFG = dict(taper_start_v=3.3, taper_min_v=3.0, taper_start_soc_pct=20.0,
                                taper_min_soc_pct=8.0, recovery_ramp_s=3.0,
                                discharge_min_kw=3.0, discharge_max_kw=110.0)
_FALLBACK_REGEN_CFG = dict(regen_full_v=4.05, regen_min_v=4.1, emergency_high_v=4.2,
                            recovery_ramp_s=3.0, regen_min_kw=0.0, regen_max_kw=70.0,
                            regen_full_soc_pct=80.0, regen_min_soc_pct=100.0)


def _find_default_log():
    candidates = sorted(LOGS_DIR.glob('*.trc'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f'no .trc files under {LOGS_DIR}')
    return candidates[0]


def _log_output_path(trc_path):
    return trc_path.with_name(trc_path.stem + '_log_output.txt')


def load_session_metadata(trc_path):
    """Parses the companion "..._log_output.txt" (same pattern this project's
    own docs cite it by - see feature_log_output_companion_file memory):
    returns (discharge_cfg, regen_cfg, session_start_dt, live_log_lines) -
    live_log_lines is a list of (datetime, message) for every "Live log"
    mirror line. Falls back to hardcoded defaults + None/[] if the companion
    file doesn't exist."""
    log_path = _log_output_path(trc_path)
    if not log_path.exists():
        print(f'  (no companion {log_path.name} found - using fallback default settings)')
        return dict(_FALLBACK_DISCHARGE_CFG), dict(_FALLBACK_REGEN_CFG), None, []

    text = log_path.read_text(encoding='utf-8', errors='replace')
    m = re.search(r'--- Settings snapshot at log start ---\s*\n(\{.*?\n\})\s*\n', text, re.DOTALL)
    settings = json.loads(m.group(1)) if m else {}
    mf = settings.get('management_features', {})
    discharge_cfg = dict(_FALLBACK_DISCHARGE_CFG)
    discharge_cfg.update(mf.get('discharge_power_taper', {}))
    regen_cfg = dict(_FALLBACK_REGEN_CFG)
    regen_cfg.update(mf.get('charge_target_taper', {}))

    start_m = re.search(r'^Started:\s*([\d-]+ [\d:]+)', text, re.MULTILINE)
    session_start_dt = datetime.strptime(start_m.group(1), '%Y-%m-%d %H:%M:%S') if start_m else None

    live_log_lines = []
    if session_start_dt:
        for line in text.splitlines():
            lm = re.match(r'^(\d{2}):(\d{2}):(\d{2})\s+(.*)$', line)
            if lm:
                h, mi, s, msg = lm.groups()
                ts = session_start_dt.replace(hour=int(h), minute=int(mi), second=int(s))
                live_log_lines.append((ts, msg))
    return discharge_cfg, regen_cfg, session_start_dt, live_log_lines


def find_running_windows(live_log_lines, session_start_dt):
    """Scans the Log-panel mirror for "phase: X -> running" / "phase:
    running -> Y" pairs and returns [(start_s, end_s), ...] relative to
    session_start_dt - the periods the bridge was actually transmitting/
    tapering, auto-detected instead of hand-transcribed per session."""
    if not session_start_dt:
        return []
    windows = []
    open_start = None
    for ts, msg in live_log_lines:
        rel = (ts - session_start_dt).total_seconds()
        if 'Sequencer phase:' not in msg:
            continue
        if msg.endswith('-> running'):
            open_start = rel
        elif re.search(r'running -> \w+', msg) and open_start is not None:
            windows.append((open_start, rel))
            open_start = None
    if open_start is not None:
        windows.append((open_start, None))   # still running when the log ended
    return windows


def _extract_rows_grep(path):
    """Fast path: `grep -E` pre-filters bus-1 (rz450e) RX frames for CAN ID
    0x020 or 0x74F, then each matched line is parsed with the same row regex
    trc_log.py itself uses. Returns a list of (t_rel_seconds, can_id_int,
    data_bytes) tuples, in file order (== chronological order)."""
    proc = subprocess.run(
        ['grep', '-E', r'1  Rx        (0020|074F) -', str(path)],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    rows = []
    t0 = None
    for line in proc.stdout.splitlines():
        m = _TRC_ROW_RE.match(line)
        if not m:
            continue
        offset_ms, _bus, _typ, id_hex, _dlc, data_str = m.groups()
        t = float(offset_ms) / 1000.0
        if t0 is None:
            t0 = t
        d = [int(x, 16) for x in data_str.split()]
        rows.append((t - t0, int(id_hex, 16), d))
    return rows


def _extract_rows_python(path):
    """Portable fallback if `grep` isn't available - slower on a huge file."""
    rows = []
    t0 = None
    for row in read_trc_rows(str(path)):
        if row['bus'] != 'rz450e' or row['dir'] != 'RX':
            continue
        try:
            arb_id = int(row['can_id_hex'], 16)
        except ValueError:
            continue
        if arb_id not in (rz450e_signals.ID_PACK_V, 0x74F):
            continue
        ts = float(row['timestamp'])
        if t0 is None:
            t0 = ts
        d = [int(x, 16) for x in row['data_hex'].split()]
        rows.append((ts - t0, arb_id, d))
    return rows


def load_session(path):
    """Returns (cell_events, soc_events): cell_events is a list of
    (t, cell_min, cell_max); soc_events is a list of (t, soc_pct) - only
    SINGLE-FRAME UDS responses for DID 0x1F5B (SoC) are decoded (PCI=0x0,
    echo 0x62 0x1F 0x5B) - the other two DIDs in this app's poll cycle
    (capacity, primary V/I) are multi-frame and irrelevant to this taper
    analysis, so they're simply skipped rather than reassembled."""
    have_grep = shutil.which('grep') is not None
    print(f'Reading {path.name} ({path.stat().st_size / 1e6:.0f} MB) via '
          f'{"grep prefilter" if have_grep else "pure-Python scan (slower)"}...')
    t_start = time_module.time()
    rows = _extract_rows_grep(path) if have_grep else _extract_rows_python(path)
    print(f'  {len(rows)} candidate frames in {time_module.time() - t_start:.1f}s')

    cell_events, soc_events = [], []
    for t, arb_id, d in rows:
        if arb_id == rz450e_signals.ID_PACK_V:
            vals = rz450e_signals.decode_020(d)
            if vals:
                cell_events.append((t, vals['cell_min'], vals['cell_max']))
        elif arb_id == 0x74F and len(d) >= 4 and (d[0] >> 4) == 0x0:
            length = d[0] & 0x0F
            payload = d[1:1 + length]
            if len(payload) >= 4 and payload[0] == 0x62 and payload[1] == 0x1F and payload[2] == 0x5B:
                vals = rz450e_signals.decode_soc(payload)
                if vals:
                    soc_events.append((t, vals['soc_pct']))
    print(f'  {len(cell_events)} cell_min/max samples, {len(soc_events)} SoC samples')
    return cell_events, soc_events


def replay(cell_events, soc_events, discharge_cfg, regen_cfg):
    """Merges both event streams in chronological order and feeds them
    through a fresh ManagementEngine with ONLY the two tapers enabled (same
    isolation pattern as check_taper_smoothness.py), using the real captured
    dt between events so recovery_ramp_s hysteresis behaves exactly as it
    would have live. Returns arrays: t, cell_min, cell_max, soc, d_kw, r_kw."""
    events = sorted(
        [(t, 'cell', cmin, cmax) for t, cmin, cmax in cell_events] +
        [(t, 'soc', soc, None) for t, soc in soc_events],
        key=lambda e: e[0])

    eng = ManagementEngine()
    for name in eng.config:
        eng.config[name]['enabled'] = name in ('discharge_power_taper', 'charge_target_taper')
    eng.config['discharge_power_taper'].update(discharge_cfg)
    eng.config['charge_target_taper'].update(regen_cfg)

    rz = SharedState()
    rz.update_input('temp_max', 25.0)
    rz.update_input('temp_min', 25.0)
    rz.update_input('current', 0.0)
    rz.update_input('charge_permission_input', 0.0)

    fake_now = [0.0]
    time_module.monotonic = lambda: fake_now[0]

    t_out, cmin_out, cmax_out, soc_out, d_kw_out, r_kw_out = [], [], [], [], [], []
    last_cmin = last_cmax = last_soc = None
    for t, kind, a, b in events:
        fake_now[0] = t
        if kind == 'cell':
            rz.update_input('cell_min', a)
            rz.update_input('cell_max', b)
            last_cmin, last_cmax = a, b
        else:
            rz.update_input('soc_pct', a)
            last_soc = a
        leaf_state = dict(DEFAULT_LEAF_STATE)
        leaf_state['discharge_limit_kw'] = 110.0
        leaf_state['charge_limit_kw'] = 70.0
        out = eng.apply(leaf_state, rz)
        t_out.append(t)
        cmin_out.append(last_cmin)
        cmax_out.append(last_cmax)
        soc_out.append(last_soc)
        d_kw_out.append(out['discharge_limit_kw'])
        r_kw_out.append(out['charge_limit_kw'])
    return t_out, cmin_out, cmax_out, soc_out, d_kw_out, r_kw_out


def build_annotations(live_log_lines, session_start_dt):
    """Turns every non-routine Log-panel line (skips the routine phase noise
    itself - windows are drawn separately - keeps disconnects/cuts/reconnects/
    manual bridge stop-start) into (rel_seconds, label) pairs for plot
    annotation."""
    if not session_start_dt:
        return []
    keep_markers = ('disconnected', 'connected on', 'HARD CUT', 'Soft cut',
                     'Bridge stopped', 'Bridge armed', 'attempting reconnect')
    out = []
    for ts, msg in live_log_lines:
        if any(k in msg for k in keep_markers):
            rel = (ts - session_start_dt).total_seconds()
            out.append((rel, msg))
    return out


def plot(t, cmin, cmax, soc, d_kw, r_kw, discharge_cfg, regen_cfg, annotations, out_path, title_suffix=''):
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    axes[0].plot(t, cmin, label='cell_min (drives discharge taper)', color='tab:blue', lw=0.7)
    axes[0].plot(t, cmax, label='cell_max (drives regen taper)', color='tab:orange', lw=0.7)
    axes[0].axhline(discharge_cfg['taper_start_v'], color='green', ls=':', lw=0.8)
    axes[0].axhline(discharge_cfg['taper_min_v'], color='red', ls=':', lw=0.8)
    axes[0].axhline(regen_cfg['regen_full_v'], color='green', ls='--', lw=0.8)
    axes[0].axhline(regen_cfg['regen_min_v'], color='red', ls='--', lw=0.8)
    axes[0].set_ylabel('cell voltage (V)')
    axes[0].legend(fontsize=8, loc='upper right')
    axes[0].set_title(f'Real captured session{title_suffix} - cell voltage')

    soc_t = [ti for ti, s in zip(t, soc) if s is not None]
    soc_v = [s for s in soc if s is not None]
    axes[1].plot(soc_t, soc_v, color='tab:purple', marker='.', ms=2, lw=0.8)
    axes[1].axhline(discharge_cfg['taper_start_soc_pct'], color='green', ls=':', lw=0.8,
                     label='discharge full>=/min<=')
    axes[1].axhline(discharge_cfg['taper_min_soc_pct'], color='red', ls=':', lw=0.8)
    axes[1].axhline(regen_cfg['regen_full_soc_pct'], color='green', ls='--', lw=0.8,
                     label='regen full<=/min>=')
    axes[1].axhline(regen_cfg['regen_min_soc_pct'], color='red', ls='--', lw=0.8)
    axes[1].set_ylabel('SoC (%)')
    axes[1].legend(fontsize=8, loc='upper right')

    axes[2].plot(t, d_kw, color='tab:red', lw=0.7, label='discharge_limit_kw')
    axes[2].plot(t, r_kw, color='tab:green', lw=0.7, label='charge_limit_kw (regen)')
    axes[2].set_ylabel('kW output')
    axes[2].set_xlabel('time (s, relative to session start)')
    axes[2].legend(fontsize=8, loc='upper right')

    for rel, label in annotations:
        if t and t[0] - 30 <= rel <= t[-1] + 30:
            for ax in axes:
                ax.axvline(rel, color='gray', ls='-', lw=0.5, alpha=0.5)
            axes[0].annotate(label, xy=(rel, axes[0].get_ylim()[1]), xytext=(2, -8),
                              textcoords='offset points', rotation=90, fontsize=6,
                              va='top', ha='left', color='dimgray')

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f'  -> {out_path}')


def jumpiness_stats(name, t, kw):
    diffs = [abs(kw[i] - kw[i - 1]) for i in range(1, len(kw))]
    if not diffs:
        print(f'  [{name}] no data')
        return
    tv = sum(diffs)
    worst = max(diffs)
    reversals = sum(1 for i in range(2, len(kw))
                     if (kw[i] - kw[i - 1]) * (kw[i - 1] - kw[i - 2]) < 0)
    print(f'  [{name}] total_variation={tv:.1f}kW  worst_tick_jump={worst:.2f}kW  '
          f'reversals={reversals}  n={len(kw)}')


def main(path_str=None):
    path = Path(path_str) if path_str else _find_default_log()
    if not path.exists():
        print(f'Not found: {path}')
        sys.exit(1)

    discharge_cfg, regen_cfg, session_start_dt, live_log_lines = load_session_metadata(path)
    if session_start_dt:
        print(f'Session started {session_start_dt} (from companion _log_output.txt)')
    print(f'  discharge_power_taper: {discharge_cfg}')
    print(f'  charge_target_taper:   {regen_cfg}')

    cell_events, soc_events = load_session(path)
    if not cell_events:
        print('No 0x020 frames found - nothing to replay.')
        sys.exit(1)
    if cell_events:
        print(f'  capture span: {cell_events[-1][0]:.1f}s ({cell_events[-1][0] / 3600.0:.2f}h), '
              f'first-to-last 0x020 frame')

    running_windows = find_running_windows(live_log_lines, session_start_dt)
    if running_windows:
        total_running = sum((e if e is not None else cell_events[-1][0]) - s for s, e in running_windows)
        print(f'  {len(running_windows)} running-phase window(s), '
              f'{total_running:.1f}s ({total_running / 3600.0:.2f}h) total actively running')
    annotations = build_annotations(live_log_lines, session_start_dt)

    print('Replaying through the actual discharge_power_taper/charge_target_taper '
          '(this session\'s own settings)...')
    t0 = time_module.time()
    t, cmin, cmax, soc, d_kw, r_kw = replay(cell_events, soc_events, discharge_cfg, regen_cfg)
    print(f'  {len(t)} engine ticks in {time_module.time() - t0:.1f}s')

    jumpiness_stats('discharge_limit_kw (full session)', t, d_kw)
    jumpiness_stats('charge_limit_kw regen (full session)', t, r_kw)

    stem = re.sub(r'[^A-Za-z0-9_-]+', '_', path.stem)
    plot(t, cmin, cmax, soc, d_kw, r_kw, discharge_cfg, regen_cfg, annotations,
         LOGS_DIR / f'soc_taper_replay_{stem}_full.png', ' (full session)')

    # Auto-detected running windows - zoomed views, since a multi-hour full
    # session compresses badly onto one x-axis.
    for i, (w_start, w_end) in enumerate(running_windows):
        w_end_eff = w_end if w_end is not None else t[-1]
        idx = [j for j, ti in enumerate(t) if w_start <= ti <= w_end_eff]
        if not idx:
            continue
        label = chr(ord('A') + i)
        wt = [t[j] for j in idx]
        wcmin = [cmin[j] for j in idx]
        wcmax = [cmax[j] for j in idx]
        wsoc = [soc[j] for j in idx]
        wd = [d_kw[j] for j in idx]
        wr = [r_kw[j] for j in idx]
        print(f'\nWindow {label} ({w_start:.0f}-{w_end_eff:.0f}s, {len(idx)} ticks):')
        jumpiness_stats(f'discharge_limit_kw (window {label})', wt, wd)
        jumpiness_stats(f'charge_limit_kw regen (window {label})', wt, wr)
        plot(wt, wcmin, wcmax, wsoc, wd, wr, discharge_cfg, regen_cfg, annotations,
             LOGS_DIR / f'soc_taper_replay_{stem}_window_{label}.png', f' - window {label}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else None)
