"""Bench-tests the STM32 firmware port by emulating a real RZ450e battery on
real CAN hardware, and (optionally, with a second adapter) capturing the
board's real output at the same time.

INJECT (CAN1): replays a previously-captured .trc file's RZ450e broadcast
traffic onto a PCAN adapter wired to the board's CAN1 header, using that
session's real inter-frame timing (so the board's staleness watchdog,
tapers, etc. see realistic pacing, not an instant burst) - and, by default,
also acts as a UDS responder: whenever the board sends a DID request
(0x747) matching one this capture actually saw, immediately answers with
that SAME session's real captured response (0x74F, replayed verbatim
including its real ISO-TP framing), so rz450e_uds.c gets exercised on real
hardware too, not just the broadcast decode path.

CAPTURE (CAN2, optional, needs a SECOND adapter): with --capture-channel
set, simultaneously listens on a second PCAN adapter wired to the board's
CAN2/Leaf-facing header and records every frame the board actually
transmits into a new .trc file - this is the "board's real output" half of
Phase 7's bench validation. See tests/check_stm32_golden_vectors.py (not
yet built) for diffing that captured file against what bridge/ itself
computes for the same input.

The board only starts transmitting ANYTHING on CAN2 once it has seen at
least one frame arrive there (bridge_sequencer.c/bridge/realtime_engine.py's
wake condition - matches a real Leaf VCM's own constant broadcast traffic).
A bare bench rig with no real VCM would never satisfy that on its own, so
the capture side sends a few synthetic wake frames automatically before it
starts recording (see WAKE_FRAME_ID/_send_wake_frames()) - disable with
--no-wake-frame only if a real VCM (or something else) is already present.

Deliberately NOT replayed: the ORIGINAL capture's own Leaf-bus traffic -
that's the real Leaf VCM's own traffic from whenever this capture was
taken, not something to feed back into a different Leaf VCM sitting on the
bench today. Also not replayed: 0x747 requests themselves (those were the
ORIGINAL Python app's own outgoing DID polls) - only used here as the key
to look up which cached response to answer the BOARD's own live requests
with.

Which channel in the source .trc is actually "the RZ450e bus" is DETECTED,
not trusted from the file's own bus-1/bus-2 labeling (bridge/trc_log.py's
normal convention) - a past session that connected the RZ450e/Leaf adapters
to the GUI's two connection roles the "wrong way around" produces a file
that's internally consistent but mislabeled. See
_detect_rz450e_bus_num().

Requires PEAK PCAN-USB adapter(s) and python-can + the PCAN driver
installed (same requirement as the main GUI app - see
bridge/can_backend.py). Use --list-channels to see what's detected,
--dry-run to sanity-check parsing/pacing without opening real hardware.

Usage:
    py tools/stm32_bench_replay.py --list-channels
    py tools/stm32_bench_replay.py --identify PCAN_USBBUS1
    py tools/stm32_bench_replay.py --trc "logs/some_capture.trc" --dry-run
    py tools/stm32_bench_replay.py --trc "logs/some_capture.trc" --channel PCAN_USBBUS1
    py tools/stm32_bench_replay.py --trc "logs/some_capture.trc" --channel PCAN_USBBUS1 --speed 2.0
    py tools/stm32_bench_replay.py --trc "logs/some_capture.trc" --channel PCAN_USBBUS1 --no-uds-responder
    py tools/stm32_bench_replay.py --trc "logs/some_capture.trc" --channel PCAN_USBBUS1 \\
        --capture-channel PCAN_USBBUS2 --capture-output "logs/some_capture_board_output.trc"
"""
import argparse
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import can_backend, config_profile, leaf_signals, state as state_mod
from bridge.trc_log import TrcLogger, read_trc_rows

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Captured board-output .trc files default here (added 2026-08-18, user
# request, after a real bench test's captured output turned out to be all
# UDS 0x747 REQUEST frames - see default_capture_output_path()'s comment) -
# a dedicated folder for these instead of scattering them alongside the
# source captures in logs/.
HARDWARE_OUTPUT_DIR = os.path.join(REPO_ROOT, 'logs', 'Hardwere_Output_Tests')

TOYOTA_REQ_ID = 0x747
TOYOTA_RESP_ID = 0x74F

# Real-Leaf CAN IDs (bridge/leaf_signals.py) - used by identify_channel() to
# recognize "this is the vehicle/CAN2-side bus" the same way BROADCAST_IDS
# below recognizes "this is the battery/CAN1-side bus".
LEAF_IDS = set(leaf_signals.TX_PERIOD_MS.keys()) | set(leaf_signals.IGNITION_IDS)

# The 10 confirmed RZ450e broadcast IDs this bridge decodes (docs/02) -
# everything else on the rz450e bus in a capture (UDS traffic, anything
# unrecognized) is excluded from the broadcast replay stream; UDS is handled
# separately by the responder below.
BROADCAST_IDS = {0x020, 0x023, 0x358, 0x3F1, 0x424, 0x4A7, 0x4A9, 0x4C0, 0x4AA}

# Only the first this-many data rows are sampled to detect which channel is
# the RZ450e bus (see _detect_rz450e_bus_num) - broadcast IDs repeat every
# 10-500ms, so a small prefix is enough; scanning a whole multi-hundred-MB-
# to-1GB+ capture twice just to detect this would be wasteful.
DETECT_SAMPLE_ROWS = 8000


# How long to give BusConnection.connect() to actually resolve before
# concluding it failed - found 2026-08-19 on real hardware: BusConnection's
# own internal wait (bridge/can_backend.py's _finish_worker_start(), a fixed
# 0.15s "enough for the common case") is not always enough, and every
# connect-and-check site in this file used to trust that single wait,
# reporting a false "could not connect ... - None" (None because nothing
# had actually failed yet - the worker just hadn't finished trying) even on
# a channel that would have connected fine a few hundred ms later. This
# polls instead of trusting one fixed sleep.
CONNECT_WAIT_TIMEOUT_S = 2.0
CONNECT_POLL_S = 0.1


def _wait_for_connect(bus, timeout_s=CONNECT_WAIT_TIMEOUT_S):
    """Polls bus.connected/bus.error up to timeout_s (bus.connect() has
    already been called) instead of trusting BusConnection's own single
    0.15s internal wait, which isn't always long enough. Returns True once
    connected, False if timeout_s elapses without connecting OR a real
    error appears first (no point waiting out the full timeout once the
    worker has already reported a definite failure)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if bus.connected:
            return True
        if bus.error:
            return False
        time.sleep(CONNECT_POLL_S)
    return bus.connected


def _connect_fail_reason(bus, timeout_s=CONNECT_WAIT_TIMEOUT_S):
    """Formats why _wait_for_connect() returned False - distinguishes a
    real driver error from a plain timeout (bus.error still None), which
    otherwise printed a useless '- None' with no hint of what to check."""
    if bus.error:
        return str(bus.error)
    return (f'timed out after {timeout_s:.1f}s with no error reported - the channel may be in use '
            f'elsewhere (check the main app\'s own connection panels aren\'t holding it) or slow to open')


def _parse_data_hex(data_hex):
    data_hex = data_hex.strip()
    if not data_hex:
        return b''
    return bytes(int(x, 16) for x in data_hex.split())


def _detect_rz450e_bus_num(path, log_fn, sample_rows=DETECT_SAMPLE_ROWS):
    """Returns the raw numeric .trc channel (1 or 2) that actually carries
    RZ450e broadcast traffic in THIS file - empirically, by counting
    BROADCAST_IDS hits per channel in the first `sample_rows` data rows,
    rather than trusting the file's own bus-1/bus-2 = rz450e/leaf labeling
    convention. Real, found-on-this-project's-own-historical-captures
    reason this matters: if a past bench/log session connected the RZ450e
    adapter to whichever GUI connection role got selected as "Leaf-facing"
    (or vice versa), the resulting .trc is internally consistent but
    mislabeled - the traffic on "bus 2" really is RZ450e broadcast frames
    even though the header says bus 2 = leaf."""
    hits = {}
    for i, row in enumerate(read_trc_rows(path)):
        if i >= sample_rows:
            break
        try:
            can_id = int(row['can_id_hex'], 16)
        except ValueError:
            continue
        if can_id in BROADCAST_IDS:
            hits[row['bus_num']] = hits.get(row['bus_num'], 0) + 1
    if not hits:
        raise ValueError(f'{path}: no RZ450e broadcast IDs found in the first {sample_rows} rows - '
                          f'cannot auto-detect which channel is the RZ450e bus')
    rz_bus_num = max(hits, key=hits.get)
    breakdown = ", ".join(f"channel {k}: {v} hit(s)" for k, v in sorted(hits.items()))
    log_fn(f'Auto-detected RZ450e bus: channel {rz_bus_num} ({breakdown}, sampled from the first '
           f'{sample_rows} rows)')
    return rz_bus_num


def _extract_leaf_track(rows, rz_bus_num, t0):
    """Real Leaf VCM traffic from the source .trc - [(t_rel_s, can_id,
    data_bytes), ...] sorted by time, using the SAME t0 as broadcast_frames
    so replaying both together reproduces the original session's real
    relative timing between the battery and the vehicle - including
    whatever genuine startup/keep-alive/wind-down pattern the real Leaf VCM
    actually showed (added 2026-08-19, user idea: the synthetic one-time
    wake frame this file used before couldn't reproduce a real startup or
    wind-down, only a bare "is anything listening" ping - replaying the
    ACTUAL recorded VCM traffic does).

    Only `dir == 'RX'` rows (bus_num != rz_bus_num) - the real Leaf VCM's
    own genuine outgoing broadcasts. Deliberately excludes `dir == 'TX'`
    rows (bus_num != rz_bus_num) - those are the ORIGINAL Python app's OWN
    simulated battery output from whenever this was captured; replaying
    that old canned output onto CAN2 now would collide with the STM32
    board's own live output on the same IDs/bus."""
    track = []
    for row in rows:
        if row['bus_num'] == rz_bus_num or row['dir'] != 'RX':
            continue
        can_id = int(row['can_id_hex'], 16)
        data = _parse_data_hex(row['data_hex'])
        t_rel = float(row['timestamp']) - t0
        track.append((t_rel, can_id, data))
    track.sort(key=lambda f: f[0])
    return track


def load_capture(path, log_fn=print):
    """Returns (broadcast_frames, did_responses, leaf_track).
    broadcast_frames: [(t_rel_s, can_id, data_bytes), ...] sorted by time,
        RX rows on the auto-detected rz450e channel with an ID in
        BROADCAST_IDS.
    did_responses: {(did_hi, did_lo): [frame_bytes, ...]} - the LAST complete
        ISO-TP response sequence this capture saw for each DID, keyed off
        the 0x747 TX request that preceded it (both on the rz450e channel,
        since the ORIGINAL Python app is what sent 0x747 in this capture).
    leaf_track: see _extract_leaf_track() - empty list if this capture never
        saw any real Leaf-bus traffic (a pure RZ450e-only capture)."""
    rz_bus_num = _detect_rz450e_bus_num(path, log_fn)
    rows = list(read_trc_rows(path))
    if not rows:
        raise ValueError(f'{path}: no parseable rows found')
    t0 = float(rows[0]['timestamp'])

    broadcast_frames = []
    did_responses = {}
    pending_did = None       # (hi, lo) of the most recent 0x747 request seen, awaiting its response
    collecting = []          # frames collected so far for pending_did
    expected_len = None
    received_len = 0

    for row in rows:
        if row['bus_num'] != rz_bus_num:
            continue
        can_id = int(row['can_id_hex'], 16)
        data = _parse_data_hex(row['data_hex'])
        t_rel = float(row['timestamp']) - t0

        if row['dir'] == 'TX' and can_id == TOYOTA_REQ_ID and len(data) >= 4 and data[0] == 0x03 and data[1] == 0x22:
            pending_did = (data[2], data[3])
            collecting = []
            expected_len = None
            received_len = 0
            continue

        if row['dir'] == 'RX' and can_id == TOYOTA_RESP_ID:
            collecting.append(data)
            if data:
                pci = data[0] >> 4
                if pci == 0x0:
                    received_len = data[0] & 0x0F
                    expected_len = received_len
                elif pci == 0x1:
                    expected_len = ((data[0] & 0x0F) << 8) | data[1]
                    received_len = 6
                elif pci == 0x2:
                    received_len += 7
            if pending_did is not None and expected_len is not None and received_len >= expected_len:
                did_responses[pending_did] = list(collecting)
                pending_did = None
            continue

        if row['dir'] == 'RX' and can_id in BROADCAST_IDS:
            broadcast_frames.append((t_rel, can_id, data))

    broadcast_frames.sort(key=lambda f: f[0])
    leaf_track = _extract_leaf_track(rows, rz_bus_num, t0)
    if leaf_track:
        log_fn(f'Real Leaf-bus traffic found in this capture: {len(leaf_track)} frame(s), '
               f'{leaf_track[0][0]:.1f}s to {leaf_track[-1][0]:.1f}s into the capture - can replay this '
               f'on CAN2 instead of a synthetic wake frame for a realistic startup/keep-alive/wind-down.')
    return broadcast_frames, did_responses, leaf_track


class UdsResponder(threading.Thread):
    """Watches the injector's rx_queue for a live 0x747 request from the
    board and answers it with this capture's cached real response for that
    DID (verbatim, including its real ISO-TP framing) - a small fixed
    inter-frame gap between response frames, not the original capture's own
    (irrelevant) timing for this part."""

    def __init__(self, bus, did_responses, log_fn):
        super().__init__(daemon=True, name='UdsResponder')
        self.bus = bus
        self.did_responses = did_responses
        self.log_fn = log_fn
        self._stop = threading.Event()
        self.requests_seen = 0
        self.responses_sent = 0
        self.unknown_dids = set()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                kind, _label, msg = self.bus.rx_queue.get(timeout=0.2)
            except Exception:
                continue
            if kind != 'rx':
                continue
            data = bytes(msg.data)
            if msg.arbitration_id != TOYOTA_REQ_ID or len(data) < 4 or data[0] != 0x03 or data[1] != 0x22:
                continue
            did = (data[2], data[3])
            self.requests_seen += 1
            frames = self.did_responses.get(did)
            if not frames:
                if did not in self.unknown_dids:
                    self.unknown_dids.add(did)
                    self.log_fn(f'UDS request for DID {did[0]:02X}{did[1]:02X} - '
                                f'no cached response in this capture, not answering')
                continue
            for frame in frames:
                self.bus.send(TOYOTA_RESP_ID, frame)
                time.sleep(0.005)
            self.responses_sent += 1
            self.log_fn(f'UDS request for DID {did[0]:02X}{did[1]:02X} -> replayed cached response '
                        f'({len(frames)} frame(s))')


def run_replay(broadcast_frames, did_responses, channel, speed, use_responder, log_fn, stop_event=None):
    """`stop_event` (optional threading.Event) lets a caller cancel a
    replay early - e.g. a GUI's Stop button (gui/stm32_bench_window.py).
    The CLI's own KeyboardInterrupt path (below) still works standalone
    without one. Sleeps between frames in short slices so a Stop request
    is noticed within ~0.1s even during a long inter-frame gap, rather than
    only being checked once per frame."""
    bus = can_backend.BusConnection('inject', demo=False)
    bus.log_fn = log_fn
    bus.connect(channel)
    if not _wait_for_connect(bus):
        log_fn(f'ERROR: could not connect to {channel} - {_connect_fail_reason(bus)}')
        return False

    responder = None
    if use_responder:
        responder = UdsResponder(bus, did_responses, log_fn)
        responder.start()
        log_fn(f'UDS responder active - {len(did_responses)} DID(s) with a cached response available')

    log_fn(f'Replaying {len(broadcast_frames)} broadcast frames at {speed}x speed on {channel}...')
    t_prev = 0.0
    sent = 0
    stopped = False
    try:
        for t_rel, can_id, data in broadcast_frames:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            gap = (t_rel - t_prev) / speed
            while gap > 0:
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    break
                slice_s = min(gap, 0.1)
                time.sleep(slice_s)
                gap -= slice_s
            if stopped:
                break
            t_prev = t_rel
            bus.send(can_id, data)
            sent += 1
            if sent % 2000 == 0:
                log_fn(f'  ...{sent}/{len(broadcast_frames)} frames sent (t={t_rel:.1f}s of capture)')
    except KeyboardInterrupt:
        stopped = True
    finally:
        if responder:
            responder.stop()
        bus.disconnect()

    if stopped:
        log_fn(f'Stopped - {sent}/{len(broadcast_frames)} broadcast frames sent.')
    else:
        log_fn(f'Done - {sent} broadcast frames sent.')
    if responder:
        log_fn(f'UDS responder: {responder.requests_seen} request(s) seen, '
               f'{responder.responses_sent} answered, {len(responder.unknown_dids)} unknown DID(s).')
    return not stopped


# The board (bridge_sequencer.c / bridge/realtime_engine.py's
# ShutdownSequencer.note_leaf_rx()) only starts transmitting ANYTHING on
# CAN2 once it has seen at least one frame arrive THERE - any arbitration
# ID, any content; that's the whole condition. In a real car this is
# satisfied automatically by the real Leaf VCM's own constant broadcast
# traffic. In a bare bench rig with no real VCM (just an injector on CAN1
# and a listener on CAN2), that condition is NEVER satisfied on its own -
# the board would sit in WAITING_FOR_WAKE forever and CAN2 would show
# nothing, no matter how correct the wiring or how much RZ450e data is
# flowing on CAN1 (found 2026-08-18: this, not just a wiring mixup, is why
# a real bench capture came back essentially empty of real board output).
# Sent 3 times, not 1, purely as margin against the very first frame
# landing before the board's CAN2 filter/peripheral is ready to receive.
# Deliberately only ONE distinct ID (0x108, an ignition ID) rather than all
# 3 leaf_signals.IGNITION_IDS: ShutdownSequencer's ignition-OFF wind-down
# trigger only arms once ALL 3 distinct ignition IDs have been seen at
# least once - sending just one means that trigger can never fire and
# interrupt an otherwise-clean bench run.
WAKE_FRAME_ID = 0x108
WAKE_FRAME_REPEATS = 3
WAKE_FRAME_GAP_S = 0.05


def _send_wake_frames(bus, log_fn):
    for _ in range(WAKE_FRAME_REPEATS):
        bus.send(WAKE_FRAME_ID, bytes(8))
        time.sleep(WAKE_FRAME_GAP_S)
    log_fn(f'Sent {WAKE_FRAME_REPEATS} synthetic Leaf-bus wake frame(s) (ID {WAKE_FRAME_ID:03X}) - '
           f'the board only starts transmitting on CAN2 after seeing real traffic there.')


def capture_output(channel, out_path, bitrate, log_fn, stop_event=None, ready_event=None, send_wake=True,
                    resend_wake_event=None, leaf_track=None, speed=1.0):
    """Listens on `channel` (wired to the STM32 board's CAN2/Leaf-facing
    header) and records every RX frame it sees into a NEW .trc file at
    `out_path`, logged under the 'leaf' role (bridge/trc_log.py) - this is
    the OUTPUT half of the golden-vector bench test; run_replay() above is
    the INPUT half. Meant to run concurrently with run_replay() in its own
    thread/adapter - see replay_and_capture() below, which wires both
    together. `ready_event` (optional) is set once the capture bus is
    connected and actively draining, so a caller can hold off starting the
    CAN1 replay until the board's response is actually being recorded.

    `leaf_track` (optional, load_capture()'s 3rd return value) - if given
    and non-empty, replays this REAL captured Leaf VCM traffic on `channel`
    with its own real relative timing (scaled by `speed`, same convention
    as run_replay()'s CAN1 pacing) INTERLEAVED with listening/recording -
    added 2026-08-19, user idea: a genuine recorded startup/keep-alive/
    wind-down pattern is far more representative than a synthetic one-time
    "is anyone listening" ping, and (since it's the SAME original session,
    same t0 as the RZ450e broadcast_frames run_replay() sends on CAN1)
    naturally preserves the real original timing relationship between the
    two buses with no extra synchronization logic needed. `send_wake` is
    ignored when `leaf_track` is non-empty (the track's own first frame(s)
    serve the same "the board isn't alone on this bus" purpose, for real).

    `resend_wake_event` (optional threading.Event) - when an external caller
    sets this, this thread (which owns the only handle to `bus`) sends a
    synthetic wake-frame burst without stopping the capture, then clears the
    event - used by replay_and_capture()'s test_wind_down mode to simulate
    the Leaf "waking back up" after a real staged wind-down + silence
    cooldown, so the board's natural re-arm (bridge_sequencer.c's
    WAITING_FOR_WAKE -> STARTUP, rearmed_naturally=1) can be tested even
    once leaf_track itself has finished replaying."""
    bus = can_backend.BusConnection('capture', demo=False)
    bus.log_fn = log_fn
    bus.connect(channel)
    if not _wait_for_connect(bus):
        log_fn(f'ERROR: capture bus could not connect to {channel} - {_connect_fail_reason(bus)}')
        if ready_event is not None:
            ready_event.set()
        return False

    use_leaf_track = bool(leaf_track)
    if use_leaf_track:
        log_fn(f'Replaying {len(leaf_track)} real Leaf-bus frame(s) on {channel} at {speed}x speed '
               f'({leaf_track[0][0]:.1f}s-{leaf_track[-1][0]:.1f}s of the original capture) instead of a '
               f'synthetic wake frame - genuine recorded startup/keep-alive/wind-down.')
    elif send_wake:
        _send_wake_frames(bus, log_fn)

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    logger = TrcLogger()
    logger.start(out_path, bitrate)
    log_fn(f'Capturing board output on {channel} -> {out_path}')
    if ready_event is not None:
        ready_event.set()

    frame_count = 0
    sent_count = 0
    leaf_idx = 0
    t_leaf_start = time.monotonic()
    try:
        while stop_event is None or not stop_event.is_set():
            if resend_wake_event is not None and resend_wake_event.is_set():
                _send_wake_frames(bus, log_fn)
                resend_wake_event.clear()

            wait_timeout = 0.2
            if use_leaf_track and leaf_idx < len(leaf_track):
                due_in = (leaf_track[leaf_idx][0] / speed) - (time.monotonic() - t_leaf_start)
                wait_timeout = max(0.0, min(due_in, 0.2))

            try:
                kind, _label, msg = bus.rx_queue.get(timeout=wait_timeout)
            except queue.Empty:
                pass
            else:
                if kind == 'rx':
                    data = bytes(msg.data)
                    logger.log_frame('leaf', False, msg.arbitration_id, len(data), data)
                    frame_count += 1

            while use_leaf_track and leaf_idx < len(leaf_track):
                t_rel, can_id, data = leaf_track[leaf_idx]
                if (time.monotonic() - t_leaf_start) * speed < t_rel:
                    break
                bus.send(can_id, data)
                leaf_idx += 1
                sent_count += 1
    finally:
        logger.stop()
        bus.disconnect()
    suffix = f', {sent_count}/{len(leaf_track)} Leaf-track frame(s) replayed' if use_leaf_track else ''
    log_fn(f'Capture stopped - {frame_count} frame(s) recorded to {out_path}{suffix}')
    return True


# Grace period after the CAN1 replay finishes before stopping the CAN2
# capture - the board may still be mid-TX-cycle or finishing a delayed DID
# response right after the last injected frame; without this the capture
# could cut off the tail end of the board's own reaction to the last frame.
CAPTURE_TRAILING_GRACE_S = 1.0

# Margin added on top of the real profile's own wind-down timers - covers
# real-world scheduling/CAN-bus jitter so the wait is never a few hundred ms
# short of what bridge_sequencer.c actually needs.
WIND_DOWN_WAIT_MARGIN_S = 5.0
# How much longer to keep capturing after re-sending the wake burst - long
# enough to see the board's real 0x1DB/0x1DC/etc. resume and for
# management_engine_notify_session_start() to have visibly cleared any
# latch in the captured output.
DEFAULT_WIND_DOWN_TAIL_S = 6.0


def default_wind_down_wait_s(profile_path=None, log_fn=print):
    """Computes the minimum real time the board needs to go from 'CAN2 has
    gone quiet' to 'reached WAITING_FOR_WAKE, ready to naturally re-arm' -
    bus_silence_timeout_s (the profile's own defensive-fallback wind-down
    trigger - the only one reachable on a bare bench rig with no ignition/
    charge-request traffic) + the staged power-down's own last stage
    (PWRDOWN_STAGE4_MS) + the quiet-cooldown before WAITING_FOR_WAKE
    (PWRDOWN_DEFAULT_COOLDOWN_S) - plus WIND_DOWN_WAIT_MARGIN_S for real-
    world jitter. Loads the real profile so this always matches whatever
    engine_timing.bus_silence_timeout_s is actually configured, not just
    the 30.0s default."""
    raw_profile = config_profile.load_profile(profile_path or config_profile.DEFAULT_PROFILE_PATH) or {}
    state = state_mod.SharedState()
    try:
        config_profile.apply_profile(raw_profile, state)
        bus_silence_timeout_s = state.engine_timing['bus_silence_timeout_s']
    except Exception as exc:  # noqa: BLE001 - fall back to the documented default, don't crash a bench test over this
        log_fn(f'WARNING: could not load {profile_path or config_profile.DEFAULT_PROFILE_PATH} to compute the '
               f'wind-down wait ({exc}) - using the 30.0s default.')
        bus_silence_timeout_s = 30.0
    total = (bus_silence_timeout_s + leaf_signals.PWRDOWN_STAGE4_MS / 1000.0
             + leaf_signals.PWRDOWN_DEFAULT_COOLDOWN_S + WIND_DOWN_WAIT_MARGIN_S)
    return total


def _interruptible_sleep(seconds, stop_event, log_fn=None):
    """Sleeps in short slices so `stop_event` is noticed within ~0.2s
    instead of only being checked once per call - same pattern as
    run_replay()'s own frame-to-frame sleep. Returns False if interrupted
    early, True if it slept the full duration."""
    remaining = seconds
    while remaining > 0:
        if stop_event is not None and stop_event.is_set():
            if log_fn:
                log_fn('Wind-down wait interrupted by user.')
            return False
        slice_s = min(remaining, 0.2)
        time.sleep(slice_s)
        remaining -= slice_s
    return True


def replay_and_capture(broadcast_frames, did_responses, inject_channel, capture_channel, capture_out_path,
                        speed, use_responder, log_fn, stop_event=None, send_wake=True, leaf_track=None,
                        test_wind_down=False, wind_down_wait_s=None, wind_down_tail_s=DEFAULT_WIND_DOWN_TAIL_S,
                        profile_path=None):
    """Runs capture_output() in a background thread and run_replay() in the
    calling thread, so a single call drives both adapters together - the
    normal way to actually run Phase 7's bench test (inject known-good
    RZ450e input on CAN1, capture the board's real output on CAN2).
    `send_wake` (default True) is passed straight through to
    capture_output() - see its own docstring for why this is normally
    needed at all in a bare bench rig with no real Leaf VCM present.
    `leaf_track` (optional, load_capture()'s 3rd return value) - if given,
    passed straight through to capture_output() to replay the REAL captured
    Leaf VCM traffic on CAN2 instead of a synthetic wake (see that
    function's own docstring) - `send_wake` is ignored whenever this is
    non-empty.

    `test_wind_down` (default False, opt-in - adds real time to the bench
    run): after the CAN1 replay AND leaf_track (if any) both finish, goes
    silent on CAN2 for `wind_down_wait_s` (default: default_wind_down_wait_s(),
    computed from the real profile) to let the board's own defensive "total
    Leaf-bus silence" trigger run through its full staged wind-down and reach
    WAITING_FOR_WAKE, then sends a fresh SYNTHETIC wake-frame burst on the
    SAME capture bus to test the board's natural re-arm (management_engine_
    notify_session_start(), which clears hard_latched/ac_charge_stop_
    latched/ac_charge_temp_stop_latched - see the 2026-08-19 fault-clearing
    discussion in project notes for why this is the only currently-working
    clear path, and why it needed a bench-testable way to exercise it) -
    useful even with a real leaf_track, e.g. if that track's own real
    session never happened to include a real wind-down. Keeps recording for
    `wind_down_tail_s` afterward so the recovery is visible in the captured
    .trc."""
    capture_ready = threading.Event()
    capture_stop = threading.Event()
    resend_wake = threading.Event()
    capture_thread = threading.Thread(
        target=capture_output,
        args=(capture_channel, capture_out_path, can_backend.BITRATE, log_fn),
        kwargs={'stop_event': capture_stop, 'ready_event': capture_ready, 'send_wake': send_wake,
                'resend_wake_event': resend_wake, 'leaf_track': leaf_track, 'speed': speed},
        daemon=True, name='CaptureOutput')
    capture_thread.start()
    if not capture_ready.wait(timeout=10.0):
        log_fn('WARNING: capture bus did not confirm ready within 10s - starting the replay anyway.')

    replay_start = time.monotonic()
    try:
        ok = run_replay(broadcast_frames, did_responses, inject_channel, speed, use_responder, log_fn,
                         stop_event=stop_event)
        if test_wind_down and ok and leaf_track:
            # leaf_track plays out inside the OTHER thread (capture_output())
            # on its own clock - if it runs longer than the CAN1 replay just
            # finished above, wait for it too before going deliberately
            # silent, so the synthetic wind-down test doesn't talk over the
            # tail of the real recorded Leaf traffic.
            leaf_remaining = (leaf_track[-1][0] / speed) - (time.monotonic() - replay_start)
            if leaf_remaining > 0:
                log_fn(f'Waiting {leaf_remaining:.1f}s more for the real Leaf-track replay to finish '
                       f'before starting the wind-down test...')
                _interruptible_sleep(leaf_remaining, stop_event, log_fn=log_fn)
        if test_wind_down and ok:
            wait_s = wind_down_wait_s if wind_down_wait_s is not None else default_wind_down_wait_s(
                profile_path, log_fn=log_fn)
            log_fn(f'Testing natural wind-down/re-arm: CAN2 goes silent for {wait_s:.1f}s '
                   f'(bus_silence_timeout_s + staged power-down + cooldown + margin) - still recording throughout.')
            if _interruptible_sleep(wait_s, stop_event, log_fn=log_fn):
                log_fn('Sending a fresh CAN2 wake burst to test the board\'s natural re-arm/latch-clear...')
                resend_wake.set()
                _interruptible_sleep(wind_down_tail_s, stop_event, log_fn=log_fn)
    finally:
        time.sleep(CAPTURE_TRAILING_GRACE_S)
        capture_stop.set()
        capture_thread.join(timeout=5.0)
    return ok


def default_capture_output_path(trc_path):
    base = os.path.splitext(os.path.basename(trc_path))[0]
    return os.path.join(HARDWARE_OUTPUT_DIR, base + '_board_output.trc')


# How long identify_channel() listens before reporting - long enough to
# catch at least one cycle of the slowest RZ450e broadcast ID (0x5C0/0x5EB
# at 500ms) or a Leaf TX period, short enough to still feel instant in a GUI.
IDENTIFY_LISTEN_S = 3.0


def identify_channel(channel, log_fn, listen_s=IDENTIFY_LISTEN_S):
    """Briefly connects to `channel` and classifies what it sees, so a bench
    setup can be verified BEFORE starting a real replay/capture instead of
    discovering a wiring mixup after the fact - added 2026-08-18 after a
    real bench capture came back as nothing but UDS 0x747 REQUEST frames:
    that ID only ever appears on the battery/CAN1-side bus (the board
    sending it TO the battery), so a capture adapter that's supposed to be
    on CAN2/Leaf-side but sees 0x747 is actually wired to CAN1 instead.

    Returns (counts_dict, verdict_string), or None if the connection failed.
    counts_dict keys: 'rz450e_broadcast', 'leaf', 'uds_request',
    'uds_response', 'other'."""
    bus = can_backend.BusConnection('identify', demo=False)
    bus.log_fn = log_fn
    bus.connect(channel)
    if not _wait_for_connect(bus):
        log_fn(f'ERROR: could not connect to {channel} - {_connect_fail_reason(bus)}')
        return None

    counts = {'rz450e_broadcast': 0, 'leaf': 0, 'uds_request': 0, 'uds_response': 0, 'other': 0}
    t_end = time.monotonic() + listen_s
    try:
        while time.monotonic() < t_end:
            try:
                kind, _label, msg = bus.rx_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind != 'rx':
                continue
            can_id = msg.arbitration_id
            if can_id in BROADCAST_IDS:
                counts['rz450e_broadcast'] += 1
            elif can_id in LEAF_IDS:
                counts['leaf'] += 1
            elif can_id == TOYOTA_REQ_ID:
                counts['uds_request'] += 1
            elif can_id == TOYOTA_RESP_ID:
                counts['uds_response'] += 1
            else:
                counts['other'] += 1
    finally:
        bus.disconnect()

    total = sum(counts.values())
    if total == 0:
        verdict = ('No traffic seen at all - check the physical connection, or nothing is '
                    'transmitting on this bus right now.')
    elif counts['rz450e_broadcast'] >= counts['leaf'] and counts['rz450e_broadcast'] > 0:
        verdict = 'Battery/RZ450e-side bus (CAN1) - real RZ450e broadcast traffic seen.'
    elif counts['leaf'] > 0:
        verdict = 'Vehicle/Leaf-side bus (CAN2) - real Leaf-format traffic seen.'
    elif counts['uds_request'] > 0:
        verdict = ('Battery-side bus (CAN1) - seeing UDS/DID REQUEST frames (0x747) only, which the '
                    'board sends TO the battery - this is CAN1, not CAN2, even with no broadcast '
                    'traffic seen yet.')
    elif counts['uds_response'] > 0:
        verdict = 'Battery-side bus (CAN1) - seeing UDS/DID RESPONSE frames (0x74F).'
    else:
        verdict = f'Unrecognized traffic only ({counts["other"]} frame(s)) - cannot confidently classify.'

    log_fn(f'Identify {channel} ({listen_s:.0f}s): {total} frame(s) - '
           f'rz450e_broadcast={counts["rz450e_broadcast"]} leaf={counts["leaf"]} '
           f'uds_request={counts["uds_request"]} uds_response={counts["uds_response"]} '
           f'other={counts["other"]} -> {verdict}')
    return counts, verdict


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--trc', help='Path to the captured .trc file to replay')
    parser.add_argument('--channel', help='PCAN channel wired to the board\'s CAN1 (e.g. PCAN_USBBUS1)')
    parser.add_argument('--speed', type=float, default=1.0,
                         help='Playback speed multiplier (1.0 = real-time pacing from the capture, default)')
    parser.add_argument('--no-uds-responder', action='store_true',
                         help='Disable answering the board\'s own UDS/DID requests with cached responses')
    parser.add_argument('--capture-channel',
                         help='PCAN channel wired to the board\'s CAN2/Leaf-facing header - if given, '
                              'records the board\'s real output to --capture-output while replaying')
    parser.add_argument('--capture-output',
                         help='Path to write the captured board-output .trc (default: logs/'
                              'Hardwere_Output_Tests/<trc name>_board_output.trc)')
    parser.add_argument('--no-wake-frame', action='store_true',
                         help='Do not send the synthetic CAN2 wake frame before capturing - the board only '
                              'starts transmitting on CAN2 after seeing traffic there, so this only makes '
                              'sense if a real Leaf VCM (or something else) is already providing that')
    parser.add_argument('--no-leaf-track', action='store_true',
                         help='Do not replay this capture\'s own real Leaf-bus traffic on CAN2, even if present - '
                              'falls back to the synthetic wake frame instead')
    parser.add_argument('--test-wind-down', action='store_true',
                         help='After the CAN1 replay, go silent on CAN2 long enough for the board\'s own '
                              'defensive wind-down (bus_silence_timeout_s + staged power-down + cooldown) to '
                              'reach WAITING_FOR_WAKE, then send a fresh wake burst to test its natural re-arm/'
                              'latch-clear - requires --capture-channel, adds real time to the bench run '
                              '(default_wind_down_wait_s(), typically ~37s)')
    parser.add_argument('--wind-down-wait', type=float,
                         help='Override the silence duration for --test-wind-down (seconds) - default is '
                              'computed from the real profile\'s bus_silence_timeout_s')
    parser.add_argument('--dry-run', action='store_true',
                         help='Parse and summarize the capture only - do not open any hardware or send anything')
    parser.add_argument('--list-channels', action='store_true',
                         help='List detected PCAN channels and exit')
    parser.add_argument('--identify',
                         help='Briefly listen on this PCAN channel and report what kind of traffic it sees '
                              '(RZ450e broadcast / Leaf / UDS request-response) - use before wiring up a real '
                              'replay to confirm which adapter is on which board header, then exit')
    args = parser.parse_args()

    if args.list_channels:
        for ch in can_backend.detect_pcan_channels():
            print(ch)
        return

    def log_fn(msg):
        print(msg)

    if args.identify:
        identify_channel(args.identify, log_fn)
        return

    if not args.trc:
        parser.error('--trc is required (unless using --list-channels/--identify)')

    print(f'Loading {args.trc} ...')
    broadcast_frames, did_responses, leaf_track = load_capture(args.trc, log_fn=log_fn)
    duration_s = broadcast_frames[-1][0] if broadcast_frames else 0.0
    print(f'{len(broadcast_frames)} broadcast frames spanning {duration_s:.1f}s of capture, '
          f'{len(did_responses)} DID(s) with a cached response.')
    for did in sorted(did_responses):
        print(f'  DID {did[0]:02X}{did[1]:02X}: {len(did_responses[did])} frame(s) in the cached response')
    if args.no_leaf_track:
        leaf_track = None

    if args.dry_run:
        print('--dry-run: not opening any hardware, nothing sent.')
        return

    if not args.channel:
        parser.error('--channel is required unless using --dry-run/--list-channels')

    if args.test_wind_down and not args.capture_channel:
        parser.error('--test-wind-down requires --capture-channel')

    if args.capture_channel:
        capture_out_path = args.capture_output or default_capture_output_path(args.trc)
        ok = replay_and_capture(broadcast_frames, did_responses, args.channel, args.capture_channel,
                                 capture_out_path, args.speed, not args.no_uds_responder, log_fn,
                                 send_wake=not args.no_wake_frame, leaf_track=leaf_track,
                                 test_wind_down=args.test_wind_down, wind_down_wait_s=args.wind_down_wait)
    else:
        ok = run_replay(broadcast_frames, did_responses, args.channel, args.speed,
                         not args.no_uds_responder, log_fn)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
