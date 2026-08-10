"""Standalone diagnostic (check_*.py, not pass/fail - see CLAUDE.md): replays
the REAL cell-voltage trace from a captured .trc log through the actual,
current bridge.management_engine.ManagementEngine.apply() (real elapsed
time between calls, not synthetic ticks) with ac_full_v/ac_min_v set to
whatever was actually configured for that test session - so a real-hardware
"did this fix actually help" question can be answered against real data,
not just a synthetic scenario.

Written 2026-08-06 for the AC charger taper hunting investigation
(docs/13 items 17.3/17.5/17.6): the 2026-08-05 cutoff-test log's own
charger_limit_kw oscillation was numerically confirmed to exactly match
the OLD (zero-hysteresis) taper formula with ac_full_v=3.62V/ac_min_v=3.64V
(the user's own bracketed test values, not the code defaults) and a
ramped_kw base of 92.3kW - see this script's own output for the comparison
table this was derived from.

Usage: py tests/check_ac_taper_log_replay.py "<path to .trc>" [ac_full_v] [ac_min_v]
"""
import sys
import time as time_module

sys.path.insert(0, '.')

from bridge.trc_log import read_trc_rows
from bridge.management_engine import ManagementEngine
from bridge.state import SharedState


def decode_020_cellmax(data_hex):
    d = [int(x, 16) for x in data_hex.split()]
    if len(d) < 5:
        return None
    return ((d[3] << 4) | (d[4] >> 4)) * 5.0 / 4096.0


def main(path, ac_full_v, ac_min_v, ramped_kw):
    rows = list(read_trc_rows(path))
    t0 = float(rows[0]['timestamp'])

    cell_events = []
    for row in rows:
        if row['bus'] == 'rz450e' and row['dir'] == 'RX' and int(row['can_id_hex'], 16) == 0x020:
            v = decode_020_cellmax(row['data_hex'])
            if v is not None:
                cell_events.append((float(row['timestamp']) - t0, v))

    eng = ManagementEngine()
    eng.config['low_voltage_cutoff']['enabled'] = False
    eng.config['charge_target_taper']['enabled'] = False
    eng.config['discharge_power_taper']['enabled'] = False
    eng.config['over_temperature_derate']['enabled'] = False
    eng.config['staleness_watchdog']['enabled'] = False
    rz = SharedState()
    rz.charge_emulation['ac_full_v'] = ac_full_v
    rz.charge_emulation['ac_min_v'] = ac_min_v
    rz.charge_emulation['ac_cutoff_v'] = 5.0   # push out of the way, not under test here
    rz.charge_emulation['ac_emergency_v'] = 5.0
    rz.update_input('charge_permission_input', 1.0)   # 2026-08-07: ac_charge_taper is now charging-only - this replay simulates an active charge session
    for i in range(1, 97):
        rz.update_input(f'cell_{i:02d}', 3.60)
    rz.update_input('temp_max', 25.0)
    rz.update_input('current', 0.0)

    fake_now = [0.0]
    time_module.monotonic = lambda: fake_now[0]

    print(f'Replaying {len(cell_events)} real cell_max samples through the CURRENT taper code '
          f'(ac_full_v={ac_full_v}, ac_min_v={ac_min_v}, ramped_kw={ramped_kw})\n')

    last_printed = None
    worst_jump = 0.0
    prev_kw = None
    for t_rel, v in cell_events:
        fake_now[0] = t_rel
        for i in range(1, 97):
            rz.rz450e[f'cell_{i:02d}'] = v
        leaf_state = dict(_DEFAULT_LEAF_STATE)
        leaf_state['charger_limit_kw'] = ramped_kw
        out = eng.apply(leaf_state, rz)
        kw = out['charger_limit_kw']
        if prev_kw is not None:
            worst_jump = max(worst_jump, abs(kw - prev_kw))
        prev_kw = kw
        if last_printed is None or abs(kw - last_printed) >= 0.3:
            print(f't={t_rel:8.3f}s  cell_max={v:.4f}V  charger_limit_kw={kw:6.2f} kW  '
                  f'(uprate level={eng.ac_uprate_level})')
            last_printed = kw

    print(f'\nWorst single-call jump across the entire real replay: {worst_jump:.3f}kW')


_DEFAULT_LEAF_STATE = {
    'discharge_limit_kw': 0.0, 'charge_limit_kw': 0.0, 'charger_limit_kw': 0.0,
    'relay_cut_request': 0, 'interlock': 1, 'capacity_empty': 0, 'full_charge_flag': 0,
}


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    ac_full_v = float(sys.argv[2]) if len(sys.argv) > 2 else 3.62
    ac_min_v = float(sys.argv[3]) if len(sys.argv) > 3 else 3.64
    main(sys.argv[1], ac_full_v, ac_min_v, ramped_kw=92.3)
