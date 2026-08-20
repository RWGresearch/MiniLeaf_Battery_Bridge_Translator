"""Diffs a captured STM32 board-output .trc (tools/stm32_bench_replay.py's
CAPTURE side) against what bridge/ itself computes for the SAME source
RZ450e .trc that was replayed onto CAN1 - the "does the real output match
what it SHOULD be" half of Phase 7's bench validation (the INJECT/CAPTURE
tools are the "known-good input, real board output" half; this is the diff).

Reuses the REAL bridge/ engine code wherever possible - bridge.rz450e_
signals' decode_020/decode_023/decode_soc/etc., bridge.state.SharedState,
MappingEngine.apply(), derive_capacity_outputs(), ManagementEngine.apply(),
leaf_signals.clamp_state() - the exact same call sequence bridge/realtime_
engine.py's _compose_leaf_state() uses (and bridge_sequencer.c's tick()
mirrors on the STM32 side), driven with a fake time.monotonic() clock -
same pattern as tests/check_shutdown_sequencer_replay.py, extended to cover
the full mapping+management+clamp pipeline instead of just sequencer phase.

Deliberately SKIPS bridge/realtime_engine.py's _apply_charge_ramp() - NOT
ported to the STM32 firmware (documented gap, "Phase 5b" in project notes),
so diffing against it would produce false mismatches on charger_limit_kw/
charge_limit_kw during an active charge session.

Only compares captured frames sent AFTER leaf_signals.T_RUNNING (ms, from
the capture file's own t=0 - the wake frame is sent right before recording
starts, so the board's own wake instant and the capture's t=0 are close,
within normal small scheduling jitter). Before that, the board sends fixed,
hardcoded startup-only byte patterns (byte-verified against real captures,
same tier as CODE_1DC/SEQ_5EB - see leaf_output.c's leaf_build_1db_startup())
that were never derived from RZ450e data at all, so diffing them against
"what mapping/management would compute" would compare apples to oranges.

Compares 9 fields carried by 0x1DB/0x1DC/0x55B/0x5BC - the ones that
actually flow through mapping_engine/management_engine (the two modules a
porting bug is most likely to hit), not the opaque/generated fields
(PRUN, heartbeat, CODE_1DC/CHG_TIME_5BC/HIST5C0/SEQ_5EB cycle bytes) which
are already separately verified byte-for-byte at codegen time.

IMPORTANT: --profile must match whatever config/profile.json actually
produced the bridge_config_gen.h the board was FLASHED with - if the
profile has changed since that flash, this comparison is meaningless (it
would be diffing the board's old behavior against new expectations).

Usage:
    py tests/check_stm32_golden_vectors.py \\
        --source "logs/some_capture.trc" \\
        --board-output "logs/Hardwere_Output_Tests/some_capture_board_output.trc"
    py tests/check_stm32_golden_vectors.py --source ... --board-output ... \\
        --profile config/some-other-profile.json
"""
import argparse
import os
import sys
import time as time_module

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge import config_profile, leaf_signals, rz450e_signals, state as state_mod
from bridge.mapping_engine import derive_capacity_outputs
from bridge.trc_log import read_trc_rows
from tools.stm32_bench_replay import BROADCAST_IDS, TOYOTA_REQ_ID, TOYOTA_RESP_ID, _detect_rz450e_bus_num

DID_SOC = (0x1F, 0x5B)
DID_CAPACITY = (0x1D, 0x3E)
DID_PRIMARY_VI = (0x1F, 0x9A)

_DID_DECODERS = {
    DID_SOC: rz450e_signals.decode_soc,
    DID_CAPACITY: rz450e_signals.decode_capacity,
    DID_PRIMARY_VI: rz450e_signals.decode_primary_v_i,
}


def _iter_did_payloads(rows, rz_bus_num):
    """Yields (t_rel, did_tuple, payload_list) for every complete UDS
    ReadDataByIdentifier response on the rz450e channel - the FLAT, PCI-
    stripped payload convention bridge.rz450e_signals.DidClient.request()
    itself returns (d[0]=0x62 echo, d[1:3]=DID, data from d[3] on), NOT
    tools/stm32_bench_replay.py's load_capture(), which deliberately keeps
    raw frames verbatim (including ISO-TP headers) for UDS-responder replay
    - a different purpose that needs the original wire bytes, not a
    decoded payload."""
    t0 = float(rows[0]['timestamp'])
    pending_did = None
    payload = None
    expected_len = None
    for row in rows:
        if row['bus_num'] != rz_bus_num:
            continue
        can_id = int(row['can_id_hex'], 16)
        data = bytes(int(x, 16) for x in row['data_hex'].split()) if row['data_hex'].strip() else b''
        t_rel = float(row['timestamp']) - t0

        if row['dir'] == 'TX' and can_id == TOYOTA_REQ_ID and len(data) >= 4 and data[0] == 0x03 and data[1] == 0x22:
            pending_did = (data[2], data[3])
            payload = None
            expected_len = None
            continue

        if row['dir'] == 'RX' and can_id == TOYOTA_RESP_ID and data:
            pci = data[0] >> 4
            if pci == 0x0:
                length = data[0] & 0x0F
                if pending_did is not None:
                    yield t_rel, pending_did, list(data[1:1 + length])
                pending_did = None
            elif pci == 0x1:
                expected_len = ((data[0] & 0x0F) << 8) | data[1]
                payload = list(data[2:8])
            elif pci == 0x2 and payload is not None:
                payload += list(data[1:8])
                if expected_len is not None and len(payload) >= expected_len:
                    if pending_did is not None:
                        yield t_rel, pending_did, payload[:expected_len]
                    pending_did = None
                    payload = None


# ── Leaf frame decoders - exact inverses of bridge/leaf_signals.py's
# build_1db/build_1dc/build_55b/build_5bc. Verified against those real
# encode functions by _verify_decoders() below before every run, not just
# trusted by inspection - the same "don't trust a hand-derived formula
# without checking it against the real one" discipline this whole port
# has used throughout. ─────────────────────────────────────────────────
def _decode_1db(data):
    raw_i = (data[0] << 3) | (data[1] >> 5)
    if raw_i >= 1024:      # 11-bit two's complement (pack_current_a is signed, -400..200)
        raw_i -= 2048
    raw_v = (data[2] << 2) | (data[3] >> 6)
    return {'pack_current_a': raw_i / 2.0, 'pack_voltage_v': raw_v / 2.0}


def _decode_1dc(data):
    raw_d = (data[0] << 2) | (data[1] >> 6)
    raw_c = ((data[1] & 0x3F) << 4) | (data[2] >> 4)
    raw_g = ((data[2] & 0x0F) << 6) | (data[3] >> 2)
    return {
        'discharge_limit_kw': raw_d * 0.25,
        'charge_limit_kw': raw_c * 0.25,
        'charger_limit_kw': raw_g * 0.1 - 10.0,
    }


def _decode_55b(data):
    raw_soc = (data[0] << 2) | (data[1] >> 6)
    return {'fine_soc_pct': raw_soc / 10.0}


def _decode_5bc(data):
    return {
        'soh_pct': float((data[4] >> 1) & 0x7F),
        'capacity_bars_raw': float(data[2] >> 4),
        'temp_segment_pct': data[3] * 0.4166666,
    }


_FRAME_DECODERS = {0x1DB: _decode_1db, 0x1DC: _decode_1dc, 0x55B: _decode_55b, 0x5BC: _decode_5bc}

# Half the real encode quantization step (+ a hair of margin) - the largest
# a correctly-encoded value could ever be off from what was actually meant.
# soh_pct/capacity_bars_raw use int() truncation (not round()) in the real
# encoder, so their tolerance is a full step, not half.
#
# pack_current_a is deliberately looser (2.0A, not 0.26A) - unlike voltage
# (which barely moves tick to tick), real pack current can swing several
# amps within 50ms under normal driving (measured directly against a real
# capture 2026-08-18: up to 3.9A/50ms) - a small, unavoidable time-alignment
# gap between two independently-timestamped capture files (source vs.
# board-output) means comparing "closest in time" samples of a fast-moving
# signal will legitimately disagree by more than the raw quantization step
# even with zero bug. 2.0A safely absorbs that while still catching a real
# scale/sign-inversion bug (which would show deltas an order of magnitude
# bigger, not a few amps).
_TOLERANCES = {
    'pack_voltage_v': 0.26, 'pack_current_a': 2.0,
    'discharge_limit_kw': 0.13, 'charge_limit_kw': 0.13, 'charger_limit_kw': 0.06,
    'fine_soc_pct': 0.06,
    'soh_pct': 1.01, 'capacity_bars_raw': 1.01, 'temp_segment_pct': 0.22,
}


def _verify_decoders(iterations=2000, seed=20260818):
    """Round-trips random values through the REAL build_1db/1dc/55b/5bc
    encoders and these hand-written decoders - catches a decode-formula bug
    in THIS script before it produces a misleading diff report."""
    import random
    rng = random.Random(seed)
    mismatches = []
    for _ in range(iterations):
        s = dict(leaf_signals.DEFAULTS)
        s['pack_current_a'] = rng.uniform(-399.0, 199.0)
        s['pack_voltage_v'] = rng.uniform(0.0, 449.5)
        s['relay_cut_request'] = rng.randint(0, 3)
        s['failsafe_status'] = rng.randint(0, 7)
        s['main_relay_on'] = rng.randint(0, 1)
        s['full_charge_flag'] = rng.randint(0, 1)
        s['interlock'] = rng.randint(0, 1)
        s['discharge_pwr_sts'] = rng.randint(0, 3)
        s['usable_soc'] = rng.randint(0, 100)
        frame = leaf_signals.build_1db(s, rng.randint(0, 3), rng.randint(0, 1))
        got = _decode_1db(frame)
        for k in ('pack_current_a', 'pack_voltage_v'):
            if abs(got[k] - s[k]) > 0.26:
                mismatches.append(('1db', k, s[k], got[k]))

        s['discharge_limit_kw'] = rng.uniform(0.0, 255.5)
        s['charge_limit_kw'] = rng.uniform(0.0, 255.5)
        s['charger_limit_kw'] = rng.uniform(-9.9, 92.2)
        s['charge_pwr_sts'] = rng.randint(0, 3)
        frame = leaf_signals.build_1dc(s, rng.randint(0, 3), uprate=rng.randint(0, 7))
        got = _decode_1dc(frame)
        for k in ('discharge_limit_kw', 'charge_limit_kw', 'charger_limit_kw'):
            if abs(got[k] - s[k]) > 0.13:
                mismatches.append(('1dc', k, s[k], got[k]))

        s['fine_soc_pct'] = rng.uniform(0.0, 102.2)
        s['ir_malfunction'] = rng.randint(0, 1)
        s['capacity_empty'] = rng.randint(0, 1)
        frame = leaf_signals.build_55b(s, rng.randint(0, 3))
        got = _decode_55b(frame)
        if abs(got['fine_soc_pct'] - s['fine_soc_pct']) > 0.06:
            mismatches.append(('55b', 'fine_soc_pct', s['fine_soc_pct'], got['fine_soc_pct']))

        s['gids'] = rng.uniform(0, 1000)
        s['soh_pct'] = rng.randint(0, 127)
        s['capacity_bars_raw'] = rng.randint(0, 14)
        s['temp_segment_pct'] = rng.uniform(0.0, 99.9)
        s['pwr_limit_reason'] = rng.randint(0, 7)
        frame = leaf_signals.build_5bc(s, rng.randint(0, 6))
        got = _decode_5bc(frame)
        for k in ('soh_pct', 'capacity_bars_raw', 'temp_segment_pct'):
            if abs(got[k] - s[k]) > 1.01:
                mismatches.append(('5bc', k, s[k], got[k]))
    return mismatches


def _build_rz_oracle(source_rows, rz_bus_num):
    """Returns [(t_rel, key, value), ...] sorted by time - every RZ450e
    signal update the source capture contains, decoded with the REAL
    bridge.rz450e_signals functions (not re-derived)."""
    t0 = float(source_rows[0]['timestamp'])
    events = []
    for row in source_rows:
        if row['bus_num'] != rz_bus_num or row['dir'] != 'RX':
            continue
        can_id = int(row['can_id_hex'], 16)
        if can_id not in BROADCAST_IDS:
            continue
        data = bytes(int(x, 16) for x in row['data_hex'].split()) if row['data_hex'].strip() else b''
        t_rel = float(row['timestamp']) - t0
        for k, v in rz450e_signals.decode_frame(can_id, data).items():
            events.append((t_rel, k, v))

    # DID-sourced signals (soc_pct, capacity_packN_ah, primary_pack_v/
    # current_a) are DELIBERATELY NOT fed as a time-varying series matching
    # the source file's own original DID-response timestamps - found
    # 2026-08-18 to be a real modeling bug, not a firmware one: tools/
    # stm32_bench_replay.py's UdsResponder always answers the board's live
    # DID request with the LAST complete response recorded anywhere in the
    # whole source file (see load_capture()'s did_responses dict), served
    # whenever the board happens to ask - completely decoupled from when
    # that response originally appeared in the source recording. Modeling
    # these as time-varying (as the first version of this script did)
    # produced 100% spurious mismatches on fine_soc_pct even though the
    # board was behaving exactly as the real bench-replay tooling intends.
    # Fed once at t=0 here - available "from wake," a reasonable proxy given
    # comparisons only start at T_RUNNING (2.1s) anyway, plenty of time for
    # a real DID round-robin cycle to have completed at least once.
    last_did_payload = {}
    for _t_rel, did, payload in _iter_did_payloads(source_rows, rz_bus_num):
        last_did_payload[did] = payload
    for did, payload in last_did_payload.items():
        decoder = _DID_DECODERS.get(did)
        if not decoder:
            continue
        for k, v in decoder(payload).items():
            events.append((0.0, k, v))

    events.sort(key=lambda e: e[0])
    return events


def run_diff(source_path, board_output_path, profile_path, log_fn=print):
    raw_profile = config_profile.load_profile(profile_path) or {}
    state = state_mod.SharedState()
    mapping, mgmt = config_profile.apply_profile(raw_profile, state)

    log_fn(f'Loading source (RZ450e) capture: {source_path}')
    source_rows = list(read_trc_rows(source_path))
    if not source_rows:
        raise ValueError(f'{source_path}: no parseable rows found')
    rz_bus_num = _detect_rz450e_bus_num(source_path, log_fn)
    events = _build_rz_oracle(source_rows, rz_bus_num)
    log_fn(f'Built RZ450e signal timeline: {len(events)} update(s)')

    log_fn(f'Loading board-output capture: {board_output_path}')
    board_rows = list(read_trc_rows(board_output_path))
    if not board_rows:
        raise ValueError(f'{board_output_path}: no parseable rows found')
    board_t0 = float(board_rows[0]['timestamp'])

    fake_now = [0.0]
    real_monotonic = time_module.monotonic
    time_module.monotonic = lambda: fake_now[0]

    skip_before_s = leaf_signals.T_RUNNING / 1000.0
    # Tick management_engine.apply() on a regular ~10ms grid from t=0 (wake),
    # NOT just when a comparison is due - found 2026-08-19 to matter for real:
    # discharge/charge tapers are STATEFUL (a carried applied-factor +
    # timestamp, ramping toward a target over real elapsed time/ticks, not
    # recomputed fresh each call - see docs/05's "not recomputed fresh from
    # instantaneous voltage each tick" rule). The real board calls apply()
    # continuously from the moment it wakes; only ticking it starting at
    # T_RUNNING (skipping the whole 0-2.1s warm-up) meant this script's FIRST
    # ever apply() call landed with dt=0 (self._last_apply_time is None -> a
    # fresh-start special case), which can snap a ramp straight to a settled
    # value instead of replicating the real gradual ramp-up from a
    # conservative startup default - produced spurious charge_limit_kw/
    # charger_limit_kw mismatches during the board's real (and correct) first
    # few seconds of ramping up to full power.
    TICK_STEP_S = 0.01
    ev_idx = 0
    next_tick_t = 0.0
    expected = dict(leaf_signals.DEFAULTS)
    results = {k: {'checked': 0, 'mismatches': 0, 'max_delta': 0.0} for k in _TOLERANCES}
    examples = []

    def _tick_to(t_target):
        nonlocal ev_idx, next_tick_t, expected
        while next_tick_t <= t_target:
            while ev_idx < len(events) and events[ev_idx][0] <= next_tick_t:
                ev_t, key, value = events[ev_idx]
                fake_now[0] = ev_t
                state.update_input(key, value)
                ev_idx += 1
            fake_now[0] = next_tick_t
            # Mirrors bridge/realtime_engine.py's _compose_leaf_state() /
            # bridge_sequencer.c's tick(), MINUS _apply_charge_ramp() (not
            # ported to the STM32 firmware - see this file's own docstring).
            e = dict(leaf_signals.DEFAULTS)
            e.update(mapping.apply(state))
            e.update(derive_capacity_outputs(state))
            e = mgmt.apply(e, state)
            e, _clamped = leaf_signals.clamp_state(e)
            expected = e
            next_tick_t += TICK_STEP_S

    try:
        for row in board_rows:
            if row['bus_num'] != 2 or row['dir'] != 'RX':
                continue
            can_id = int(row['can_id_hex'], 16)
            decoder = _FRAME_DECODERS.get(can_id)
            if decoder is None:
                continue
            t_rel = float(row['timestamp']) - board_t0
            if t_rel < 0:
                continue
            data = bytes(int(x, 16) for x in row['data_hex'].split()) if row['data_hex'].strip() else b''
            if len(data) < 8:
                continue

            _tick_to(t_rel)
            if t_rel < skip_before_s:
                continue

            actual = decoder(data)
            for key, actual_val in actual.items():
                if key not in _TOLERANCES:
                    continue
                exp_val = expected.get(key)
                if exp_val is None:
                    continue
                tol = _TOLERANCES[key]
                r = results[key]
                r['checked'] += 1
                delta = abs(actual_val - exp_val)
                r['max_delta'] = max(r['max_delta'], delta)
                if delta > tol:
                    r['mismatches'] += 1
                    if len(examples) < 20:
                        examples.append((t_rel, f'{can_id:03X}', key, exp_val, actual_val, delta))
    finally:
        time_module.monotonic = real_monotonic

    return results, examples


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', required=True, help='The RZ450e .trc that was replayed onto CAN1')
    parser.add_argument('--board-output', required=True,
                         help='The captured board-output .trc (tools/stm32_bench_replay.py CAPTURE side)')
    parser.add_argument('--profile', default=config_profile.DEFAULT_PROFILE_PATH,
                         help='Profile the board was actually FLASHED with (default: config/profile.json) - '
                              'must match, or this comparison is meaningless')
    args = parser.parse_args()

    print('Verifying this script\'s own frame decoders against the real encoders...')
    decoder_bugs = _verify_decoders()
    if decoder_bugs:
        print(f'ABORT: {len(decoder_bugs)} decoder self-check mismatch(es) - this script\'s decode formulas '
              f'do not match bridge/leaf_signals.py\'s real encoders, fix before trusting any diff below:')
        for frame, key, expected, got in decoder_bugs[:10]:
            print(f'  {frame} {key}: encoded {expected!r}, decoded back {got!r}')
        sys.exit(2)
    print('Decoder self-check passed (2000 round-trips x 4 frame types, 0 mismatches).\n')

    print(f'WARNING: this assumes {args.profile} matches what the board was actually flashed with - '
          f'if the profile has changed since that flash, this comparison is meaningless.\n')

    results, examples = run_diff(args.source, args.board_output, args.profile)

    print('\n--- Golden-vector diff results ---')
    total_checked = 0
    total_mismatches = 0
    for key, r in results.items():
        total_checked += r['checked']
        total_mismatches += r['mismatches']
        status = 'OK' if r['mismatches'] == 0 else 'MISMATCH'
        print(f'  {key:20s} checked={r["checked"]:5d}  mismatches={r["mismatches"]:5d}  '
              f'max_delta={r["max_delta"]:8.3f}  [{status}]')

    if examples:
        print('\nFirst mismatches (t, frame ID, field, expected, actual, delta):')
        for t_rel, frame_id, key, exp_val, actual_val, delta in examples:
            print(f'  t={t_rel:7.2f}s  0x{frame_id}  {key:20s} expected={exp_val:8.3f} '
                  f'actual={actual_val:8.3f} delta={delta:7.3f}')

    print(f'\nTotal: {total_checked} value(s) checked, {total_mismatches} mismatch(es).')
    sys.exit(0 if total_mismatches == 0 else 1)


if __name__ == '__main__':
    main()
