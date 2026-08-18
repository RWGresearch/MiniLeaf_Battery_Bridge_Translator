"""Standalone diagnostic (check_*.py, not pass/fail - see CLAUDE.md): plots
raw pack voltage, SoC, pack current, calculated GIDs, and pack temperature
(max/min) over time from a real .trc capture - one PNG per log, one stacked
subplot per signal, output filename == the log's own stem (so it sorts next
to its .trc/_log_output.txt in a file listing).

GIDs are not a real RZ450e signal - reproduces the exact same formula
bridge/mapping_engine.py's derive_capacity_outputs() uses live (usable_wh /
80.0, usable_wh scaled by SOH from live capacity_ah / nameplate_capacity_ah),
fed by this session's own captured SoC (DID 0x1F5B) and pack capacity (DID
0x1D3E, pack1 falling back through pack2-4) samples, using the vehicle
usable_capacity_kwh/nameplate_capacity_ah this session actually had
configured (auto-loaded from the companion "..._log_output.txt" settings
snapshot if present, else mapping_engine's own defaults) - same
diagnose-against-actual-config discipline as check_soc_taper_log_replay.py.

Usage: py tests/check_signal_history_plots.py ["<path to .trc>" ...]
(bare filenames are resolved against logs/; defaults to the 4 sessions named
in the 2026-08-14 request if no args given)
Writes PNGs to logs/<log stem>.png
"""
import json
import re
import shutil
import subprocess
import sys
import time as time_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from bridge import rz450e_signals
from bridge.mapping_engine import NAMEPLATE_CAPACITY_AH
from bridge.trc_log import _TRC_ROW_RE, read_trc_rows

LOGS_DIR = Path(__file__).resolve().parent.parent / 'logs'

DEFAULT_LOGS = [
    'minileaf_20260813_191119_log_Discharge.trc',
    'minileaf_20260809_080913-power and regen settings test.trc',
    'minileaf_20260814_182841_DishChargeBelow20.trc',
    'minileaf_20260806_182106-charge-test-with-low-set-points-on-v After Fix.trc',
]

_WATCH_IDS = {rz450e_signals.ID_PACK_V, rz450e_signals.ID_CURRENT,
              rz450e_signals.ID_TEMP_MINMAX, rz450e_signals.TOYOTA_RESP_ID}
_ID_HEX = {i: f'{i:04X}' for i in _WATCH_IDS}


def _log_output_path(trc_path):
    return trc_path.with_name(trc_path.stem + '_log_output.txt')


def load_vehicle_cfg(trc_path):
    """Same companion-file pattern as check_soc_taper_log_replay.py - reads
    the real usable_capacity_kwh/nameplate_capacity_ah this session actually
    had configured, falling back to mapping_engine's own defaults if no
    companion file exists (Log Output wasn't started that session)."""
    log_path = _log_output_path(trc_path)
    usable_kwh, nameplate_ah = 64.0, NAMEPLATE_CAPACITY_AH
    if log_path.exists():
        text = log_path.read_text(encoding='utf-8', errors='replace')
        m = re.search(r'--- Settings snapshot at log start ---\s*\n(\{.*?\n\})\s*\n', text, re.DOTALL)
        if m:
            settings = json.loads(m.group(1))
            vehicle = settings.get('vehicle', {})
            usable_kwh = vehicle.get('usable_capacity_kwh', usable_kwh)
            nameplate_ah = vehicle.get('nameplate_capacity_ah', nameplate_ah)
    else:
        print(f'  (no companion {log_path.name} found - using mapping_engine defaults)')
    return usable_kwh, nameplate_ah


def _extract_rows_grep(path):
    pattern = r'1  Rx        (' + '|'.join(_ID_HEX.values()) + r') -'
    proc = subprocess.run(['grep', '-E', pattern, str(path)],
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
        if arb_id not in _WATCH_IDS:
            continue
        ts = float(row['timestamp'])
        if t0 is None:
            t0 = ts
        d = [int(x, 16) for x in row['data_hex'].split()]
        rows.append((ts - t0, arb_id, d))
    return rows


def reassemble_uds(rows):
    """rows: (t, data) pairs for TOYOTA_RESP_ID frames only, in chronological
    order. Returns [(t, payload_list), ...] - payload starts with the 0x62
    positive-response echo byte, same shape rz450e_signals.decode_soc/
    decode_capacity expect (same reassembly rules as DidClient.request, just
    replayed offline from an already-captured log instead of live)."""
    out = []
    payload = None
    expected_len = None
    start_t = None
    for t, d in rows:
        if not d:
            continue
        pci = d[0] >> 4
        if pci == 0x0:
            length = d[0] & 0x0F
            out.append((t, list(d[1:1 + length])))
            payload = None
        elif pci == 0x1:
            expected_len = ((d[0] & 0x0F) << 8) | d[1]
            payload = list(d[2:8])
            start_t = t
        elif pci == 0x2 and payload is not None:
            payload += list(d[1:8])
            if len(payload) >= expected_len:
                out.append((start_t, payload[:expected_len]))
                payload = None
    return out


def load_session(path):
    have_grep = shutil.which('grep') is not None
    print(f'Reading {path.name} ({path.stat().st_size / 1e6:.0f} MB) via '
          f'{"grep prefilter" if have_grep else "pure-Python scan (slower)"}...')
    t_start = time_module.time()
    rows = _extract_rows_grep(path) if have_grep else _extract_rows_python(path)
    print(f'  {len(rows)} candidate frames in {time_module.time() - t_start:.1f}s')

    pv, cur, temp, uds = [], [], [], []
    for t, arb_id, d in rows:
        if arb_id == rz450e_signals.ID_PACK_V:
            vals = rz450e_signals.decode_020(d)
            if vals:
                pv.append((t, vals['pack_v']))
        elif arb_id == rz450e_signals.ID_CURRENT:
            vals = rz450e_signals.decode_023(d)
            if vals:
                cur.append((t, vals['current']))
        elif arb_id == rz450e_signals.ID_TEMP_MINMAX:
            vals = rz450e_signals.decode_temp_minmax(d)
            if vals:
                temp.append((t, vals['temp_max'], vals['temp_min']))
        elif arb_id == rz450e_signals.TOYOTA_RESP_ID:
            uds.append((t, d))

    soc, capacity_ah = [], []
    for t, payload in reassemble_uds(uds):
        if not payload or payload[0] != 0x62 or len(payload) < 3:
            continue
        did = (payload[1], payload[2])
        if did == rz450e_signals.DID_SOC:
            vals = rz450e_signals.decode_soc(payload)
            if vals:
                soc.append((t, vals['soc_pct']))
        elif did == rz450e_signals.DID_CAPACITY:
            vals = rz450e_signals.decode_capacity(payload)
            for key in ('capacity_pack1_ah', 'capacity_pack2_ah',
                        'capacity_pack3_ah', 'capacity_pack4_ah'):
                if vals.get(key):
                    capacity_ah.append((t, vals[key]))
                    break

    print(f'  pack_v={len(pv)} current={len(cur)} temp={len(temp)} '
          f'soc={len(soc)} capacity_ah={len(capacity_ah)} samples')
    return pv, cur, temp, soc, capacity_ah


def compute_gids(soc, capacity_ah, usable_kwh, nameplate_ah):
    """Forward-fills capacity_ah onto each SoC sample (capacity DID polls far
    less often than SoC) and reproduces mapping_engine.
    derive_capacity_outputs()'s exact formula (gids = usable_wh / 80.0)."""
    gids = []
    cap_i = 0
    last_cap = nameplate_ah   # SOH=100% until the first real reading arrives
    for t, soc_pct in soc:
        while cap_i < len(capacity_ah) and capacity_ah[cap_i][0] <= t:
            last_cap = capacity_ah[cap_i][1]
            cap_i += 1
        soh_fraction = last_cap / nameplate_ah
        usable_kwh_at_soh = usable_kwh * soh_fraction
        usable_wh = (soc_pct / 100.0) * usable_kwh_at_soh * 1000.0
        gids.append((t, usable_wh / 80.0))
    return gids


def plot(pv, cur, temp, soc, gids, title, out_path):
    fig, axes = plt.subplots(5, 1, figsize=(14, 16), sharex=True)

    t, v = zip(*pv) if pv else ([], [])
    axes[0].plot(t, v, color='tab:blue', lw=0.6)
    axes[0].set_ylabel('Pack voltage (V)')
    axes[0].set_title(f'{title} - pack voltage')

    t, s = zip(*soc) if soc else ([], [])
    axes[1].plot(t, s, color='tab:purple', lw=0.8, marker='.', ms=2)
    axes[1].set_ylabel('SoC (%)')

    t, i = zip(*cur) if cur else ([], [])
    axes[2].plot(t, i, color='tab:red', lw=0.5)
    axes[2].axhline(0, color='gray', lw=0.5, ls=':')
    axes[2].set_ylabel('Current (A)\n(+discharge/-charge)')

    t, g = zip(*gids) if gids else ([], [])
    axes[3].plot(t, g, color='tab:green', lw=0.8, marker='.', ms=2)
    axes[3].set_ylabel('Calculated GIDs')

    if temp:
        tt, tmax, tmin = zip(*temp)
        axes[4].plot(tt, tmax, color='tab:orange', lw=0.7, label='temp_max')
        axes[4].plot(tt, tmin, color='tab:cyan', lw=0.7, label='temp_min')
        axes[4].legend(fontsize=8, loc='upper right')
    axes[4].set_ylabel('Temperature (°C)')
    axes[4].set_xlabel('Time (s, relative to capture start)')

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f'  -> {out_path}')


def main(paths):
    for path_str in paths:
        path = Path(path_str)
        if not path.is_absolute() and not path.exists():
            path = LOGS_DIR / path_str
        if not path.exists():
            print(f'Not found, skipping: {path}')
            continue

        usable_kwh, nameplate_ah = load_vehicle_cfg(path)
        print(f'{path.name}: usable_capacity_kwh={usable_kwh}, nameplate_capacity_ah={nameplate_ah}')

        pv, cur, temp, soc, capacity_ah = load_session(path)
        if not pv and not soc:
            print('  No pack_v/SoC frames found - skipping.')
            continue

        gids = compute_gids(soc, capacity_ah, usable_kwh, nameplate_ah)
        out_path = LOGS_DIR / f'{path.stem}.png'
        plot(pv, cur, temp, soc, gids, path.stem, out_path)


if __name__ == '__main__':
    main(sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_LOGS)
