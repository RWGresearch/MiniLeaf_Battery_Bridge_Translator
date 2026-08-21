"""Standalone diagnostic (check_*.py, not pass/fail - see CLAUDE.md): runs
docs/15-real-hardware-test-checklist.md's Part A "data confirmation" checks
against a real captured .trc, decoding with the REAL bridge.rz450e_signals
functions (not re-derived) - plausible cell-voltage/pack-voltage/current/
temp ranges, no stuck/frozen values, alive counters actually incrementing,
plus a basic capture-integrity check (both bus tracks start/end together,
no huge one-sided gap like the truncation bug documented in
check_startup_winddown_candidates.py's own docstring).

Written 2026-08-20 to vet a batch of 6 new real-car captures before using
any of them as a tools/stm32_bench_replay.py source.

Usage:
    py tests/check_realcar_log_data_quality.py [path ...]
With no args, scans every *.trc directly under logs/ (not logs/Hardwere_Output_Tests/).
"""
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.trc_log import read_trc_rows
from bridge.rz450e_signals import decode_frame, ID_CELLS_A, ID_CELLS_B

BROADCAST_IDS = {0x020, 0x023, 0x358, 0x3F1, 0x424, 0x4A7, 0x4A9, 0x4C0, 0x4AA}
CELL_V_RANGE = (3.0, 4.3)   # a bit wider than the "plausible" 3.6-4.2V so a
                             # resting/near-flat pack doesn't spuriously fail
TEMP_C_RANGE = (-20.0, 70.0)
PACK_V_MIN_PLAUSIBLE = 250.0


def detect_rz_bus(path, sample_rows=8000):
    counts = {}
    for i, row in enumerate(read_trc_rows(path)):
        if i >= sample_rows:
            break
        try:
            can_id = int(row['can_id_hex'], 16)
        except ValueError:
            continue
        if can_id in BROADCAST_IDS:
            counts[row['bus_num']] = counts.get(row['bus_num'], 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def scan(path):
    print(f'=== {path} ===')
    rz_bus = detect_rz_bus(path)
    if rz_bus is None:
        print('  could not detect RZ450e bus, skipping')
        return
    leaf_bus = 2 if rz_bus == 1 else 1

    cell_vals = {}       # cell_NN -> [min, max]
    cell_last_seen = {}  # cell_NN -> last raw value (stuck-value check)
    cell_stuck_count = {}
    temp_vals = {}
    temp_last_seen = {}
    temp_stuck_count = {}
    pack_v_vals = []
    current_vals = []
    alive_358_seen = set()
    alive_3f1_seen = set()
    counter_5s_seen = set()

    rz_t0 = rz_t_last = None
    leaf_t0 = leaf_t_last = None
    rz_rows = leaf_rows = 0
    n_cells_seen = set()
    n_temps_seen = set()

    for row in read_trc_rows(path):
        try:
            can_id = int(row['can_id_hex'], 16)
            t = float(row['timestamp'])
        except ValueError:
            continue
        data = bytes(int(x, 16) for x in row['data_hex'].split()) if row['data_hex'].strip() else b''

        if row['bus_num'] == rz_bus:
            rz_rows += 1
            if rz_t0 is None:
                rz_t0 = t
            rz_t_last = t
            if row['dir'] != 'RX':
                continue
            decoded = decode_frame(can_id, data)
            if can_id in (ID_CELLS_A, ID_CELLS_B):
                for k, v in decoded.items():
                    n_cells_seen.add(k)
                    lo, hi = cell_vals.get(k, (v, v))
                    cell_vals[k] = (min(lo, v), max(hi, v))
                    if cell_last_seen.get(k) == v:
                        cell_stuck_count[k] = cell_stuck_count.get(k, 0) + 1
                    else:
                        cell_stuck_count[k] = 0
                    cell_last_seen[k] = v
            elif can_id == 0x4AA:
                for k, v in decoded.items():
                    n_temps_seen.add(k)
                    lo, hi = temp_vals.get(k, (v, v))
                    temp_vals[k] = (min(lo, v), max(hi, v))
                    if temp_last_seen.get(k) == v:
                        temp_stuck_count[k] = temp_stuck_count.get(k, 0) + 1
                    else:
                        temp_stuck_count[k] = 0
                    temp_last_seen[k] = v
            elif can_id == 0x020:
                if 'pack_v' in decoded:
                    pack_v_vals.append(decoded['pack_v'])
            elif can_id == 0x023:
                if 'current' in decoded:
                    current_vals.append(decoded['current'])
            elif can_id == 0x358:
                if 'alive_358' in decoded:
                    alive_358_seen.add(decoded['alive_358'])
            elif can_id == 0x3F1:
                if 'alive_3f1' in decoded:
                    alive_3f1_seen.add(decoded['alive_3f1'])
            elif can_id == 0x424:
                if 'counter_5s' in decoded:
                    counter_5s_seen.add(decoded['counter_5s'])
        elif row['bus_num'] == leaf_bus:
            leaf_rows += 1
            if leaf_t0 is None:
                leaf_t0 = t
            leaf_t_last = t

    if rz_t0 is None or leaf_t0 is None:
        print('  MISSING one or both bus tracks entirely - not usable')
        print()
        return

    rz_dur = rz_t_last - rz_t0
    leaf_dur = leaf_t_last - leaf_t0
    print(f'  RZ450e bus_num={rz_bus}: {rz_rows} rows, duration={rz_dur:.1f}s (t0={rz_t0 - min(rz_t0, leaf_t0):.2f}, '
          f't_last={rz_t_last - min(rz_t0, leaf_t0):.2f})')
    print(f'  Leaf    bus_num={leaf_bus}: {leaf_rows} rows, duration={leaf_dur:.1f}s (t0={leaf_t0 - min(rz_t0, leaf_t0):.2f}, '
          f't_last={leaf_t_last - min(rz_t0, leaf_t0):.2f})')
    # The known truncation bug (see check_startup_winddown_candidates.py's
    # docstring) is the RZ450e track being cut SHORT independent of the Leaf
    # track's own timestamps - i.e. RZ ending well BEFORE Leaf, or starting
    # well AFTER it. The opposite (RZ outliving Leaf, e.g. the pack keeps
    # broadcasting after the sequencer parks and Leaf traffic quiets down,
    # and/or the RZ450e adapter connects a few seconds before Leaf traffic
    # begins) is normal, expected session shape, not a truncation symptom.
    rz_starts_late = (rz_t0 - leaf_t0) > 2.0
    rz_ends_early = (leaf_t_last - rz_t_last) > 2.0
    if rz_starts_late or rz_ends_early:
        print(f'  WARNING - RZ450e track looks cut short relative to Leaf track '
              f'(starts_late={rz_starts_late}, ends_early={rz_ends_early}) - possible truncation bug')
    else:
        print('  RZ450e/Leaf track alignment OK (no sign of the CAN1-truncated-independently-of-Leaf bug)')

    # Cell voltage plausibility
    print(f'  cells seen: {len(n_cells_seen)}/96')
    bad_range = [k for k, (lo, hi) in cell_vals.items()
                 if lo < CELL_V_RANGE[0] or hi > CELL_V_RANGE[1]]
    stuck = [k for k, c in cell_stuck_count.items() if c > 0 and c == cell_stuck_count[k]]
    # "frozen the whole session" = never changed even once (min==max) with >1 sighting
    frozen = [k for k, (lo, hi) in cell_vals.items() if lo == hi]
    print(f'  cell voltage out-of-plausible-range ({CELL_V_RANGE[0]}-{CELL_V_RANGE[1]}V): '
          f'{len(bad_range)} {bad_range[:5] if bad_range else ""}')
    print(f'  cells that never changed value all session (possibly frozen/stuck): '
          f'{len(frozen)} {sorted(frozen)[:10] if frozen else ""}')

    # Temp plausibility
    print(f'  temp probes seen: {len(n_temps_seen)}/16')
    bad_temp = [k for k, (lo, hi) in temp_vals.items()
                if lo < TEMP_C_RANGE[0] or hi > TEMP_C_RANGE[1]]
    frozen_temp = [k for k, (lo, hi) in temp_vals.items() if lo == hi]
    print(f'  temp probes out-of-plausible-range ({TEMP_C_RANGE[0]}-{TEMP_C_RANGE[1]}C): '
          f'{len(bad_temp)} {bad_temp if bad_temp else ""}')
    print(f'  temp probes that never changed value all session: '
          f'{len(frozen_temp)} {sorted(frozen_temp) if frozen_temp else ""}')

    # Pack voltage
    if pack_v_vals:
        print(f'  pack_v range: {min(pack_v_vals):.1f}-{max(pack_v_vals):.1f}V '
              f'({len(pack_v_vals)} samples) '
              f'[{"OK" if min(pack_v_vals) >= PACK_V_MIN_PLAUSIBLE else "WARNING - below plausible resting range"}]')
    else:
        print('  NO pack_v (0x020) samples decoded')

    # Current sign / range
    if current_vals:
        print(f'  current range: {min(current_vals):.1f}A to {max(current_vals):.1f}A '
              f'({len(current_vals)} samples)')
    else:
        print('  NO current (0x023) samples decoded')

    # Alive counters actually incrementing
    def _alive_report(name, seen):
        if not seen:
            print(f'  {name}: NO samples decoded')
        elif len(seen) == 1:
            print(f'  {name}: STUCK at single value {seen} the whole session')
        else:
            print(f'  {name}: incrementing OK ({len(seen)} distinct values seen)')

    _alive_report('alive_358', alive_358_seen)
    _alive_report('alive_3f1', alive_3f1_seen)
    _alive_report('counter_5s (0x424)', counter_5s_seen)
    print()


def main():
    paths = sys.argv[1:] or sorted(p for p in glob.glob('logs/*.trc'))
    for path in paths:
        scan(path)


if __name__ == '__main__':
    main()
