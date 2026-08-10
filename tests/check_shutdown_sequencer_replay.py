"""Standalone diagnostic (check_*.py, not pass/fail - see CLAUDE.md): replays
the real Leaf-bus RX frames from a captured .trc log through the ACTUAL
bridge.realtime_engine.ShutdownSequencer class (time.monotonic patched to
the log's own relative clock, tick() called on the same ~cadence the real TX
loop uses) so a real-hardware "why didn't it sleep" report can be diagnosed
against the real state machine instead of read-and-guess. Written 2026-08-05
for the "bridge never shuts off after charger unplug" report.

Usage: py tests/check_shutdown_sequencer_replay.py "<path to .trc>" [charge_authorized_0_or_1]
"""
import sys
import time as time_module

sys.path.insert(0, '.')

from bridge.trc_log import read_trc_rows
from bridge.realtime_engine import ShutdownSequencer


def decode_358(data_hex):
    b = [int(x, 16) for x in data_hex.split()]
    if len(b) < 1:
        return None
    return b[0] & 1


def main(path, force_charge_authorized):
    rows = list(read_trc_rows(path))
    t0 = float(rows[0]['timestamp'])

    leaf_rx = []
    charge_perm_events = []
    for row in rows:
        t_rel = float(row['timestamp']) - t0
        can_id = int(row['can_id_hex'], 16)
        if row['bus'] == 'leaf' and row['dir'] == 'RX':
            data = bytes(int(x, 16) for x in row['data_hex'].split()) if row['data_hex'].strip() else b''
            leaf_rx.append((t_rel, can_id, data))
        elif row['bus'] == 'rz450e' and row['dir'] == 'RX' and can_id == 0x358:
            perm = decode_358(row['data_hex'])
            if perm is not None:
                charge_perm_events.append((t_rel, perm))

    fake_now = [0.0]
    time_module.monotonic = lambda: fake_now[0]

    seq = ShutdownSequencer()
    seq.arm()

    charge_authorized = True if force_charge_authorized is None else bool(force_charge_authorized)
    perm_idx = 0
    last_phase = None
    t_end = leaf_rx[-1][0] if leaf_rx else 0.0

    # Merge RX events with a tick every 20ms (matches the real TX loop's
    # tight polling of sequencer.tick() while running) up to the last RX
    # timestamp plus a tail window, so wind-down triggers get evaluated on
    # the same cadence they would in the live app, not just at RX moments.
    step = 0.02
    rx_idx = 0
    t = 0.0
    while t <= t_end + 120.0:
        while rx_idx < len(leaf_rx) and leaf_rx[rx_idx][0] <= t:
            rt, arb_id, data = leaf_rx[rx_idx]
            fake_now[0] = rt
            if force_charge_authorized is None:
                while perm_idx < len(charge_perm_events) and charge_perm_events[perm_idx][0] <= rt:
                    charge_authorized = bool(charge_perm_events[perm_idx][1])
                    perm_idx += 1
            seq.note_leaf_rx(arb_id, data)
            rx_idx += 1
        fake_now[0] = t
        phase, timing = seq.tick(False, charge_authorized)
        if phase != last_phase:
            print(f'{t:8.3f}s  phase: {last_phase} -> {phase}  '
                  f'(chg_seen_active={seq.chg_seen_active}, charge_authorized={charge_authorized})')
            last_phase = phase
        t += step
        if rx_idx >= len(leaf_rx) and t > t_end + 120.0:
            break


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    forced = None
    if len(sys.argv) >= 3:
        forced = bool(int(sys.argv[2]))
    main(sys.argv[1], forced)
