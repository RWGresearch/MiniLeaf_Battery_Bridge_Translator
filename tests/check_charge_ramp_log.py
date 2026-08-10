"""Standalone diagnostic (not pass/fail - see CLAUDE.md's check_*.py naming
convention): decodes charger_limit_kw (0x1DC TX) and cell_min/cell_max (0x020
RX) out of a captured .trc log so a real-hardware ramp-down test can actually
be inspected, instead of guessing at what happened from memory. Written for
diagnosing the 2026-08-05 "ramp down jumps around" report - reusable for any
future .trc capture of the same test.

Usage: py tests/check_charge_ramp_log.py "<path to .trc>"
"""
import sys

from bridge.trc_log import read_trc_rows


def decode_1dc_charger_kw(data_hex):
    b = [int(x, 16) for x in data_hex.split()]
    if len(b) < 4:
        return None
    raw_g = ((b[2] & 0xF) << 6) | (b[3] >> 2)
    return raw_g * 0.1 - 10


def decode_1dc_uprate(data_hex):
    """LB_BPCMAX_UPRATE (0x1DC byte index 4, bits 5-7) - added 2026-08-06 to
    confirm the AC taper's dynamically-selected convergence level (docs/15
    B20) actually shows up in the transmitted uprate bits while converging,
    not just in the internal computation. See leaf_signals.build_1dc()."""
    b = [int(x, 16) for x in data_hex.split()]
    if len(b) < 5:
        return None
    return (b[4] >> 5) & 7


def decode_020_cells(data_hex):
    d = [int(x, 16) for x in data_hex.split()]
    if len(d) < 5:
        return None, None
    cell_min = (((d[1] & 0x0F) << 8) | d[2]) * 5.0 / 4096.0
    cell_max = ((d[3] << 4) | (d[4] >> 4)) * 5.0 / 4096.0
    return cell_min, cell_max


def decode_1db_flags(data_hex):
    b = [int(x, 16) for x in data_hex.split()]
    if len(b) < 4:
        return None
    full_charge_flag = (b[3] >> 4) & 1
    interlock = (b[3] >> 3) & 1
    relay_cut_request = (b[1] >> 3) & 3
    return full_charge_flag, interlock, relay_cut_request


def main(path):
    t0 = None
    last_kw = None
    last_uprate = None
    last_flags = None
    last_leaf_tx_t = None
    events = []
    for row in read_trc_rows(path):
        ts = float(row['timestamp'])
        if t0 is None:
            t0 = ts
        t_rel = ts - t0
        can_id = int(row['can_id_hex'], 16)
        if row['bus'] == 'leaf' and row['dir'] == 'TX':
            if last_leaf_tx_t is not None and (t_rel - last_leaf_tx_t) >= 2.0:
                events.append((t_rel, 'GAP', f'{t_rel - last_leaf_tx_t:.2f}s since last Leaf TX frame'))
            last_leaf_tx_t = t_rel
        if row['bus'] == 'leaf' and row['dir'] == 'TX' and can_id == 0x1DC:
            kw = decode_1dc_charger_kw(row['data_hex'])
            if kw is None:
                continue
            if last_kw is None or abs(kw - last_kw) >= 0.05:
                events.append((t_rel, 'charger_limit_kw', round(kw, 2)))
            last_kw = kw
            uprate = decode_1dc_uprate(row['data_hex'])
            if uprate is not None and uprate != last_uprate:
                events.append((t_rel, 'uprate_level', uprate))
                last_uprate = uprate
        elif row['bus'] == 'leaf' and row['dir'] == 'TX' and can_id == 0x1DB:
            flags = decode_1db_flags(row['data_hex'])
            if flags is not None and flags != last_flags:
                events.append((t_rel, 'full_charge/interlock/relay_cut', flags))
                last_flags = flags
        elif row['bus'] == 'rz450e' and row['dir'] == 'RX' and can_id == 0x020:
            cell_min, cell_max = decode_020_cells(row['data_hex'])
            if cell_min is not None:
                events.append((t_rel, 'cell_min/max', (round(cell_min, 3), round(cell_max, 3))))

    # Print only charger_limit_kw changes plus every ~2s of cell voltage, so
    # the output stays readable for a long capture.
    last_cell_print = -999
    for t_rel, kind, val in events:
        if kind == 'charger_limit_kw':
            print(f'{t_rel:8.3f}s  charger_limit_kw = {val:6.2f} kW')
        elif kind == 'uprate_level':
            print(f'{t_rel:8.3f}s  uprate_level = {val}')
        elif kind in ('full_charge/interlock/relay_cut', 'GAP'):
            print(f'{t_rel:8.3f}s  {kind} = {val}')
        elif t_rel - last_cell_print >= 2.0:
            print(f'{t_rel:8.3f}s  cell_min/max = {val}')
            last_cell_print = t_rel


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
