"""Scans real captured .trc sessions under logs/ for genuine ignition-off
periods (real car parked, not just a bench-truncated file) - looking for a
source file suitable for bench-testing the STM32 sequencer's startup AND
natural wind-down/re-arm against REAL, time-aligned RZ450e + Leaf-bus data
(unlike minileaf_20260813_191119_log_Discharge_shortend_for_bentch_test.trc,
whose CAN1 portion was truncated independently of its Leaf-track timestamps -
see 2026-08-19 session notes).

Standalone diagnostic, not pass/fail - run manually:
    py tests/check_startup_winddown_candidates.py [path ...]
With no args, scans every *.trc directly under logs/ (not logs/Hardwere_Output_Tests/).
"""
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.trc_log import read_trc_rows

IGNITION_IDS = {0x108, 0x1CB, 0x284}
BROADCAST_IDS = {0x20, 0x50, 0x59, 0x60, 0x3F1, 0x358, 0x4A9, 0x4AA, 0x4C0}
QUIET_GAP_S = 20.0  # real "car went off" gap, not just normal inter-ID spacing


def detect_leaf_bus(path, sample_rows=8000):
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
    rz_bus = max(counts, key=counts.get)
    return rz_bus


def scan(path):
    print(f'=== {path} ===')
    rz_bus = detect_leaf_bus(path)
    if rz_bus is None:
        print('  could not detect RZ450e bus, skipping')
        return
    print(f'  RZ450e bus_num={rz_bus} (leaf bus_num={"2" if rz_bus==1 else "1"})')

    t0 = None
    t_last = None
    ignition_times = []
    row_count = 0
    for row in read_trc_rows(path):
        row_count += 1
        if row['bus_num'] == rz_bus:
            continue
        try:
            can_id = int(row['can_id_hex'], 16)
            t = float(row['timestamp'])
        except ValueError:
            continue
        if t0 is None:
            t0 = t
        t_last = t
        if can_id in IGNITION_IDS:
            ignition_times.append(t - t0)

    if t0 is None:
        print('  no rows at all')
        return
    duration = t_last - t0
    print(f'  duration={duration:.1f}s ({duration/60:.1f} min), {row_count} total rows, '
          f'{len(ignition_times)} ignition-ID sightings')

    if not ignition_times:
        print('  NO ignition traffic seen at all in this file')
        return

    print(f'  first ignition sighting: t={ignition_times[0]:.1f}s '
          f'({"at/near start - car already on when capture began" if ignition_times[0] < 5 else "AFTER capture start - possible real startup event"})')
    print(f'  last ignition sighting: t={ignition_times[-1]:.1f}s '
          f'({"at/near end - car still on when capture stopped" if (duration - ignition_times[-1]) < 5 else "BEFORE capture end - possible real wind-down event"})')

    gaps = []
    for a, b in zip(ignition_times, ignition_times[1:]):
        if b - a >= QUIET_GAP_S:
            gaps.append((a, b, b - a))
    if gaps:
        print(f'  {len(gaps)} genuine quiet gap(s) >= {QUIET_GAP_S:.0f}s in ignition traffic '
              f'(candidate wind-down -> later re-start events):')
        for a, b, g in gaps:
            print(f'    ignition present until t={a:.1f}s, silent for {g:.1f}s, resumes at t={b:.1f}s')
    else:
        print(f'  no quiet gaps >= {QUIET_GAP_S:.0f}s - ignition traffic continuous throughout '
              f'(no real wind-down/re-arm event captured in this file)')
    print()


def main():
    paths = sys.argv[1:] or sorted(
        p for p in glob.glob('logs/*.trc')
    )
    for path in paths:
        scan(path)


if __name__ == '__main__':
    main()
