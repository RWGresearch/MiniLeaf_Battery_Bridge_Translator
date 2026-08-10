"""The real-time engine: RZ450e ingest (raw CAN + slow DID polling), the
fixed-period Leaf TX loop (docs/06-realtime-engine-and-watchdog.md), and the
ported Leaf startup/shutdown sequencer (docs/07-startup-shutdown-plan.md).

Architecture: ingest threads only ever WRITE into SharedState. The TX loop
only ever READS SharedState and its own local timing state, on a fixed wall
clock, independent of when RZ450e data actually arrives - this decoupling is
a hard requirement, not a style preference (see docs/06, section 1).

0x1F2 charge-request decode (CommandedChargePower / Charge_StatusTransition
Reqest) is ported byte-for-byte from Leaf_BMS_Emulator's Core._charge_state.
"""
import queue
import threading
import time

from bridge import leaf_signals, rz450e_signals
from bridge.mapping_engine import derive_capacity_outputs

CHG_ID = 0x1F2


class ShutdownSequencer:
    """Ported logic (not verbatim code, no direct equivalent function to
    copy) from Leaf_BMS_Emulator's Core: staged startup timing plus the four
    independent wind-down triggers, using that project's own confirmed
    constants (bridge/leaf_signals.py)."""

    def __init__(self):
        self.lock = threading.RLock()
        # idle -> waiting_for_wake -> startup -> running -> winding_down -> stopped -> waiting_for_wake
        # 'idle': bridge not started (Start Bridge not yet pressed) - nothing
        #   transmits, nothing is evaluated.
        # 'waiting_for_wake': armed (Start Bridge pressed), waiting for real
        #   Leaf VCM traffic before the staged startup sequence begins -
        #   matches docs/07's real trigger condition ("wait for CAN traffic
        #   on the bus"), not just "the app process started."
        self.phase = 'idle'
        self.session_start = time.monotonic()
        self.shutdown_t0 = None
        self.stopped_since = None
        self.last_leaf_rx_t = None   # ANY Leaf-bus frame, not just ignition/charge IDs - see the 'stopped' branch of tick()
        self.ignition_last_seen = {}
        self.chg_last_frame_t = None
        self.chg_trans = None
        self.chg_cmd = None
        self.chg_trans_last_change_t = None
        self.chg_seen_active = False
        self._ignition_off_since = None
        self._chg_end_since = None
        self._chg_stall_since = None
        self._manual_shutdown_requested = False
        # Bug fix, 2026-08-01: distinguishes a NATURAL re-arm (the sequencer
        # itself detected a genuine wind-down - ignition-off, staleness, or
        # manual "Simulate power-down" - completed, and the bus went quiet
        # and woke up again) from a MANUAL re-arm (the user simply pressed
        # Stop Bridge then Start Bridge, restarting this bridge's software
        # with no requirement the car ever actually lost power). Only a
        # natural re-arm is close enough to "the car being powered down and
        # back on" to be allowed to clear a latched hard cut
        # (ManagementEngine.notify_session_start(), called from
        # RealtimeEngine on a waiting_for_wake -> startup transition) -
        # found by an independent review pass: the bridge originally called
        # notify_session_start() on EVERY such transition, so simply
        # toggling Stop/Start Bridge while the car's VCM was still powered
        # on (never touching the ignition) silently cleared an emergency-
        # tier latch with zero relation to the car being power-cycled.
        self.rearmed_naturally = False

    def arm(self):
        """Start Bridge pressed: begin waiting for real Leaf bus traffic.
        Marks the upcoming wake as a MANUAL re-arm (bug fix, 2026-08-01 -
        see `rearmed_naturally` below) - pressing Stop Bridge then Start
        Bridge restarts this bridge's own software, not the car; the car's
        VCM may never have actually lost power, so this must not be treated
        as the "car powered down and back on" condition that's allowed to
        clear a latched hard cut."""
        with self.lock:
            self.phase = 'waiting_for_wake'
            self.rearmed_naturally = False
            self.ignition_last_seen = {}
            self.chg_last_frame_t = None
            self.chg_trans = None
            self.chg_cmd = None
            self.chg_seen_active = False
            self._ignition_off_since = None
            self._chg_end_since = None
            self._chg_stall_since = None

    def disarm(self):
        """Stop Bridge pressed: stop transmitting immediately (not a staged
        wind-down - the user is taking manual control, e.g. to edit mappings
        and restart clean)."""
        with self.lock:
            self.phase = 'idle'

    def note_leaf_rx(self, arb_id, data):
        with self.lock:
            now = time.monotonic()
            self.last_leaf_rx_t = now
            if self.phase == 'waiting_for_wake':
                self.phase = 'startup'
                self.session_start = now
            if arb_id in leaf_signals.IGNITION_IDS:
                self.ignition_last_seen[arb_id] = now
            elif arb_id == CHG_ID and len(data) >= 3:
                cmd = ((data[0] & 3) << 8) | data[1]
                trans = (data[2] >> 5) & 3
                if trans != self.chg_trans:
                    self.chg_trans_last_change_t = now
                self.chg_trans = trans
                self.chg_cmd = cmd
                self.chg_last_frame_t = now
                if trans == 1 or cmd > leaf_signals.CHG_CMD_IDLE:
                    self.chg_seen_active = True

    def request_shutdown(self):
        with self.lock:
            self._manual_shutdown_requested = True

    def get_session_start(self):
        """Thread-safe read of when the CURRENT bridge session began (the
        last waiting_for_wake -> startup transition - real Leaf-bus traffic
        seen after Start Bridge was pressed, whether that wake was manual or
        a natural re-arm). Added 2026-08-04 for RealtimeEngine.
        _charge_data_ready(), which needs actual bridge-session boundaries -
        see docs/06 section 3."""
        with self.lock:
            return self.session_start

    def _run_state_fresh(self, now):
        times = list(self.ignition_last_seen.values())
        return bool(times) and (now - max(times) <= leaf_signals.IGNITION_QUIET_S)

    def refuse_sleep_value(self, now):
        """LB_RefusetoSleep (0x55B byte 6, bits 5-6) - previously hardcoded
        to 0 always (bug, found 2026-07-31 auditing this bridge's shutdown
        behavior against Leaf_BMS_Emulator's confirmed real-capture finding).
        That project confirmed via a real capture that this bit tracks
        ignition state directly: 0 while the car is on, flipping to 1 within
        ~150ms of key-off - and forces it to 1 unconditionally during their
        staged power-down sequence, since power-down only ever runs on/after
        a key-off (exactly the real-capture condition for it being 1).
        Ported directly: derive from the same run-state-freshness check the
        ignition-off detector itself uses during normal operation, force 1
        during winding_down."""
        with self.lock:
            if self.phase == 'winding_down':
                return 1
            return 0 if self._run_state_fresh(now) else 1

    def _ignition_off_detected(self, now):
        if now - self.session_start < leaf_signals.IGNITION_GRACE_S:
            return False
        if len(self.ignition_last_seen) < len(leaf_signals.IGNITION_IDS):
            return False
        newest = max(self.ignition_last_seen.values())
        return (now - newest) > leaf_signals.IGNITION_QUIET_S

    def _chg_fresh(self, now):
        return (self.chg_last_frame_t is not None and
                now - self.chg_last_frame_t <= leaf_signals.CHG_CMD_FRESH_S)

    def charge_active(self, now):
        """Public (was an inline local in _should_wind_down) - True while a
        real 0x1F2 charge request is active: Charge_StatusTransitionReqest
        == 1, or CommandedChargePower above its idle threshold, seen
        recently enough to still count as 'fresh'. Exposed so
        RealtimeEngine's charger-request ramp (docs/06) can reuse the exact
        same detection the shutdown triggers already use, instead of
        duplicating the 0x1F2 decode logic."""
        with self.lock:
            return self._chg_fresh(now) and \
                (self.chg_trans == 1 or (self.chg_cmd or 0) > leaf_signals.CHG_CMD_IDLE)

    def _should_wind_down(self, staleness_hard_cut, charge_authorized=True):
        """`staleness_hard_cut` (docs/13 item 14.3, fixed 2026-08-03; was
        `hard_cut_this_tick`, ANY hard cut) - deliberately narrowed to the
        staleness watchdog's own hard-cut escalation specifically, per user
        directive: "the bridge should not wind down unless there is a real
        trigger to do so... a hard cut should not stop the bridge... all
        fail safes are triggered and the bridge still operates" unless one
        of the four Leaf-side triggers (or this staleness-specific 5th one)
        actually fires. A voltage/temperature/cross-check emergency hard cut
        latches relay_cut_request/interlock (see ManagementEngine._hard_
        latched) and the bridge keeps running and broadcasting that cut
        indefinitely - it does NOT wind down, which would make the bridge go
        silent on the Leaf bus during an active emergency instead of holding
        the cut asserted. Staleness is different: it means the bridge can no
        longer safely read the battery AT ALL, which is a genuine reason to
        stop transmitting entirely, not just assert a cut."""
        with self.lock:
            now = time.monotonic()
            if self._manual_shutdown_requested:
                self._manual_shutdown_requested = False
                return True
            if staleness_hard_cut:
                return True   # 5th trigger, specific to this bridge (docs/06/07) - staleness ONLY

            # `charge_authorized` (user directive, 2026-07-31): the Leaf
            # asking to charge (0x1F2) is only treated as a REAL, ongoing
            # charge session - one worth keeping the bridge awake for - while
            # the RZ450e-side charge_permission_input interlock also grants
            # it. If the Leaf keeps asking but the pack never authorizes it,
            # that is NOT a reason to stay awake indefinitely; it falls
            # through to the same chg_seen_active/chg_end_since path below as
            # an ordinary charge-session end, so the bridge is free to wind
            # down and go back to sleep until a genuine replug.
            chg_active = self.charge_active(now)
            chg_effective = chg_active and charge_authorized
            if chg_effective:
                self._ignition_off_since = None
                self._chg_end_since = None
                self._chg_stall_since = None
                return False

            if self._ignition_off_detected(now):
                if self._ignition_off_since is None:
                    self._ignition_off_since = now
                if now - self._ignition_off_since >= leaf_signals.IGNITION_OFF_DELAY_S:
                    return True
            else:
                self._ignition_off_since = None

            if self.chg_seen_active and not chg_effective and not self._run_state_fresh(now):
                if self._chg_end_since is None:
                    self._chg_end_since = now
                if now - self._chg_end_since >= leaf_signals.CHG_END_STOP_S:
                    return True
            else:
                self._chg_end_since = None

            if self._chg_fresh(now) and not self.chg_seen_active and not self._run_state_fresh(now):
                anchor = self.chg_trans_last_change_t or now
                if self._chg_stall_since is None or self._chg_stall_since < anchor:
                    self._chg_stall_since = anchor
                if now - self._chg_stall_since >= leaf_signals.CHG_STALL_TIMEOUT_S:
                    return True
            else:
                self._chg_stall_since = None

            # 6th trigger, bridge-specific defensive fallback (docs/07,
            # added 2026-08-06 - see leaf_signals.BUS_SILENCE_TIMEOUT_S's own
            # comment for the full rationale): if the Leaf bus has gone
            # completely silent (no frame of ANY ID, not just ignition/charge
            # IDs) for this long while still in startup/running, wind down
            # regardless of what the other triggers are doing. last_leaf_rx_t
            # is guaranteed non-None here (reaching startup/running at all
            # required at least one prior note_leaf_rx() call).
            if now - self.last_leaf_rx_t >= leaf_signals.BUS_SILENCE_TIMEOUT_S:
                return True

            return False

    def tick(self, staleness_hard_cut, charge_authorized=True):
        """Advance the phase state machine. Returns (phase, t_ms_or_elapsed).
        `staleness_hard_cut` (docs/13 item 14.3, fixed 2026-08-03; renamed
        from `hard_cut_this_tick` - see _should_wind_down's docstring): ONLY
        the staleness watchdog's own hard-cut escalation, not any hard cut.
        `charge_authorized` (docs/06/07, added 2026-07-31): whether RZ450e's
        own charge_permission_input interlock currently grants a charge
        request the Leaf is making - see _should_wind_down. Defaults to True
        so any caller not passing it (e.g. direct unit tests of this class in
        isolation) keeps the original always-authorized behavior."""
        now = time.monotonic()
        with self.lock:
            if self.phase in ('idle', 'waiting_for_wake'):
                # Nothing to advance - 'idle' waits for arm(), 'waiting_for_
                # wake' waits for note_leaf_rx() to see real Leaf traffic.
                return self.phase, 0.0
            if self.phase in ('startup', 'running'):
                t_ms = (now - self.session_start) * 1000.0
                if t_ms >= leaf_signals.T_RUNNING:
                    self.phase = 'running'
                if self._should_wind_down(staleness_hard_cut, charge_authorized):
                    self.phase = 'winding_down'
                    self.shutdown_t0 = now
                return self.phase, t_ms
            if self.phase == 'winding_down':
                elapsed_ms = (now - self.shutdown_t0) * 1000.0
                if elapsed_ms >= leaf_signals.PWRDOWN_STAGE4_MS:
                    self.phase = 'stopped'
                    self.stopped_since = now
                return self.phase, elapsed_ms
            if self.phase == 'stopped':
                # BUG FIX (found 2026-07-31): this used to re-arm after a flat
                # PWRDOWN_DEFAULT_COOLDOWN_S since `stopped_since` elapsed,
                # regardless of whether the Leaf bus was still transmitting.
                # Leaf_BMS_Emulator hit exactly this bug (their own rev 20)
                # and fixed it in rev 21 after a real capture showed a VCM
                # that never stopped talking instantly re-triggering the wake
                # detector the moment the emulator re-armed on a fixed timer
                # (zero RX gaps >100ms across two power-down button presses).
                # Their fix requires the bus to have gone GENUINELY quiet for
                # the cooldown period, re-checked continuously - waiting
                # indefinitely if it never does, rather than re-arming on a
                # timer regardless. `last_leaf_rx_t` is guaranteed non-None
                # here: reaching 'stopped' at all required at least one prior
                # note_leaf_rx() call to leave 'waiting_for_wake'.
                quiet_for = 0.0 if self.last_leaf_rx_t is None else now - self.last_leaf_rx_t
                if quiet_for >= leaf_signals.PWRDOWN_DEFAULT_COOLDOWN_S:
                    self.phase = 'waiting_for_wake'
                    self.rearmed_naturally = True   # see __init__'s comment - this IS a genuine wind-down/re-wake
                    self.ignition_last_seen = {}
                    self.chg_seen_active = False
                return self.phase, 0.0
            return self.phase, 0.0

    @staticmethod
    def id_active_during_winddown(arb_id, elapsed_ms):
        if arb_id in (0x59E, 0x5C0, 0x5EB):
            return False
        if arb_id in (0x55B, 0x5BC):
            return elapsed_ms < leaf_signals.PWRDOWN_STAGE2_MS
        if arb_id in (0x1DB, 0x1DC):
            return elapsed_ms < leaf_signals.PWRDOWN_STAGE3_MS
        return elapsed_ms < leaf_signals.PWRDOWN_STAGE4_MS   # 0x1C2 / 0x1ED


class RealtimeEngine:
    def __init__(self, state, mapping_engine, management_engine,
                 rz_bus, leaf_bus):
        self.state = state
        self.mapping = mapping_engine
        self.management = management_engine
        self.rz_bus = rz_bus
        self.leaf_bus = leaf_bus
        self.did_client = rz450e_signals.DidClient(rz_bus)
        self.sequencer = ShutdownSequencer()
        self._running = False
        self._threads = []
        self._prun = 0
        self._tick10 = 0
        self._latch = 0
        self._active_clamp_keys = set()
        # Charger-request ramp state (docs/06), added 2026-07-31 - see
        # _apply_charge_ramp().
        self._chg_ramp_raw = None
        self._chg_ramp_last_t = None
        self._chg_uprate_current = 0
        # Last computed reason the charge ramp itself (not ManagementEngine)
        # is holding at 0/blocked, if any (docs/13 item 14.4, added
        # 2026-08-03) - None while the ramp is active or idle-with-no-
        # request. Read by charge_status_summary() below so the GUI can show
        # the SAME reason that's actually logged, instead of a hardcoded
        # guess - see that method's own docstring for why this exists.
        self._last_charge_gate_reason = None
        # Charge-replug edge detector (added 2026-08-01) - see
        # _apply_charge_ramp()'s notify_charge_replug() call below.
        self._prev_charge_active = False
        # How long charge_active() has been continuously False (added
        # 2026-08-03, docs/13 item 13.4) - a resumption only counts as a
        # genuine replug if this gap was long enough; see _apply_charge_ramp.
        self._chg_inactive_since = None
        # Heartbeat (added 2026-08-01, user request) - updated every _tx_loop
        # iteration. sequencer.phase alone can't reveal a dead TX thread: if
        # _tx_loop crashes, phase just freezes at its last value and the GUI
        # would otherwise show "Bridge: running" forever with no warning.
        # The GUI compares this against wall-clock time to detect a stalled
        # loop instead of trusting phase alone.
        self.last_tick_monotonic = None
        self.log_fn = lambda msg: None   # replaced by the GUI with the log panel's put()
        # .trc capture (added 2026-08-01) - optional bridge.trc_log.TrcLogger,
        # set by the GUI's Start Log button. RX frames are logged here
        # (_ingest_rz_bus/_ingest_leaf_bus); TX frames are logged from
        # bridge/can_backend.py's BusConnection.send() instead, since that's
        # the single choke point for everything each bus actually transmits.
        self.trc_logger = None

    def start(self):
        """Starts the always-on monitoring threads: RZ450e ingest (raw CAN +
        DID polling), Leaf-bus wake detection, and the PRUN tick. Call once
        at app launch. Does NOT begin transmitting to the Leaf bus - the
        bridge starts in the 'idle' phase until start_bridge() is called, so
        RZ450e data can be watched and mappings edited before anything is
        actually sent (see main.py rev 6 changelog)."""
        if self._running:
            return
        self._running = True
        self.sequencer = ShutdownSequencer()
        self._chg_ramp_raw = None
        self._chg_ramp_last_t = None
        self._chg_uprate_current = 0
        self._threads = [
            threading.Thread(target=self._ingest_rz_bus, daemon=True, name='ingest-rz-bus'),
            threading.Thread(target=self._ingest_leaf_bus, daemon=True, name='ingest-leaf-bus'),
            threading.Thread(target=self._did_poll_loop, daemon=True, name='did-poll'),
            threading.Thread(target=self._prun_tick_loop, daemon=True, name='prun-tick'),
            threading.Thread(target=self._tx_loop, daemon=True, name='leaf-tx'),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        self._running = False

    def start_bridge(self):
        """'Start Bridge' button: arm the sequencer to wait for real Leaf bus
        traffic, then run the staged startup sequence once it sees any.
        RZ450e monitoring/mapping-edits already work before this is pressed;
        this only gates actual transmission to the Leaf bus."""
        self.sequencer.arm()
        self.log_fn('Bridge armed - waiting for Leaf bus traffic to begin the startup sequence.')

    def stop_bridge(self):
        """'Stop Bridge' button: stop transmitting immediately (manual
        control, not the staged wind-down - use 'Simulate power-down' for
        that while the bridge is running)."""
        self.sequencer.disarm()
        self.state.clear_leaf_tx()
        self.log_fn('Bridge stopped.')

    def request_shutdown(self):
        self.sequencer.request_shutdown()

    # ── Ingest ───────────────────────────────────────────────────────────
    def _ingest_rz_bus(self):
        """Single combined RZ450e connection - one physical adapter carries
        every ID (diagnostic/DID traffic and the fast internal-bus broadcasts
        together), split by CAN ID rather than by physical bus, matching how
        this project's hardware is actually wired."""
        # Fault containment (docs/13 item 16.1, added 2026-08-04) - see
        # _tx_loop's own comment for the full rationale. This thread has no
        # heartbeat of its own; before this fix, a single bad/unexpected
        # frame throwing anywhere past the queue.get() (decode, checksum
        # check, validation, state update) permanently killed RZ450e ingest
        # for the rest of the session - the only symptom would have been
        # every input signal eventually going stale and tripping the
        # staleness watchdog ~60s later, indistinguishable from a genuine
        # hardware disconnect. Now one bad frame is logged and dropped; the
        # next frame is processed normally. Log spam rate-limited to
        # once/second, same pattern as _tx_loop.
        last_exc_log_t = 0.0
        exc_count_since_log = 0
        while self._running:
            try:
                kind, _label, msg = self.rz_bus.rx_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind != 'rx':
                continue
            try:
                arb_id, data = msg.arbitration_id, bytes(msg.data)
                if self.trc_logger:
                    self.trc_logger.log_frame('rz450e', False, arb_id, len(data), data)
                if self.did_client.feed(arb_id, data):
                    continue
                # Checksum validation (docs/13 item 13.5, added 2026-08-03, user
                # directive): reject a frame that fails its confirmed Toyota
                # additive checksum BEFORE decoding it at all - this project's
                # own docs said this check "should" be wired in as a corruption
                # detector, but it never actually was until now. A frame that
                # fails this is never decoded, same treatment as a plausibility-
                # check rejection (see _ingest_validated below). Toggleable
                # (docs/13 item 15.15, added 2026-08-03, default ON) - reading
                # this live every frame (not cached) so flipping the checkbox
                # takes effect immediately, same as every other management
                # feature; when off, corrupt frames are decoded anyway (for
                # deliberately testing what happens downstream with bad data).
                if self.management.config['checksum_validation']['enabled'] \
                        and not rz450e_signals.frame_checksum_ok(arb_id, data):
                    self.state.note_checksum_failure(arb_id)
                    self.log_fn(f'REJECTED corrupt frame (checksum mismatch): CAN 0x{arb_id:03X}')
                    continue
                if arb_id == rz450e_signals.ID_TICK_424:
                    vals = rz450e_signals.decode_424(data)
                    if vals:
                        self.state.note_counter('counter_5s', int(vals['counter_5s']))
                    continue
                if arb_id == rz450e_signals.ID_ALIVE_3F1:
                    vals = rz450e_signals.decode_3f1(data)
                    if vals:
                        self.state.note_counter('alive_3f1', int(vals['alive_3f1']))
                    continue
                if arb_id == rz450e_signals.ID_CHARGE_PERM:
                    vals = rz450e_signals.decode_358(data)
                    if vals:
                        self.state.update_input('charge_permission_input', vals['charge_permission_input'])
                        self.state.note_counter('alive_358', int(vals.get('alive_358', 0)))
                    continue
                vals = rz450e_signals.decode_frame(arb_id, data)
                if vals:
                    self._ingest_validated(f'CAN 0x{arb_id:03X}', vals)
            except Exception as exc:
                exc_count_since_log += 1
                now = time.monotonic()
                if now - last_exc_log_t >= 1.0:
                    self.log_fn(f'RZ450e INGEST ERROR (frame dropped, thread continuing) - '
                                f'{exc_count_since_log}x in the last ~1s, most recent: {exc!r}')
                    last_exc_log_t = now
                    exc_count_since_log = 0

    def _ingest_validated(self, source_label, vals):
        """Plausibility-check a freshly-decoded {key: value} dict
        (rz450e_signals.validate_inputs(), docs/05, added 2026-08-01) before
        it ever reaches SharedState - shared by both the raw-CAN ingest loop
        (source_label = 'CAN 0x...') and the DID poll loop below
        (source_label = 'DID ...'), so validation applies uniformly to every
        source, fast or slow. A rejection is logged and recorded via
        note_rejected_input() so ManagementEngine.apply() can surface a
        fault_log entry for it on the normal tick cycle.

        Toggleable (docs/13 item 15.14, added 2026-08-03, default ON) - read
        live every call, not cached, so the checkbox takes effect on the very
        next frame. When off, every decoded value passes straight through
        unfiltered (for deliberately testing what happens downstream with an
        implausible value, rather than having it silently dropped here)."""
        if not self.management.config['input_validation']['enabled']:
            self.state.update_inputs(vals)
            return
        valid, rejected = rz450e_signals.validate_inputs(vals)
        if valid:
            self.state.update_inputs(valid)
        if rejected:
            for key, value in rejected.items():
                self.state.note_rejected_input(key, value)
            self.log_fn(f'REJECTED implausible input ({source_label}): ' +
                        '; '.join(f'{k}={v!r}' for k, v in rejected.items()))

    def _ingest_leaf_bus(self):
        """Watches for real VCM traffic (ignition/charge-request IDs) for the
        shutdown sequencer. In a standalone-battery (no real car) bench setup
        this queue simply stays empty, which is fine - the sequencer's
        ignition-off/charge rules just never fire, matching a bench rig with
        no VCM present."""
        while self._running:
            try:
                kind, _label, msg = self.leaf_bus.rx_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind != 'rx':
                continue
            arb_id, data = msg.arbitration_id, bytes(msg.data)
            if self.trc_logger:
                self.trc_logger.log_frame('leaf', False, arb_id, len(data), data)
            self.sequencer.note_leaf_rx(arb_id, data)

    def _did_poll_loop(self):
        """Reworked 2026-08-01 (user directive): wait up to
        DID_RESPONSE_TIMEOUT_S (5.0s) for each DID's response, then move to
        the next one immediately once it arrives - rather than always
        sleeping a flat 5.0s after every single request regardless of how
        fast it actually answered (the old behavior meant any one specific
        DID was really only re-polled every ~15s, not "every 5s" as it
        looked). Only a small DID_INTER_REQUEST_GAP_S pacing delay between
        requests, so the bus still isn't hit with back-to-back polls."""
        cycle = [
            (rz450e_signals.DID_SOC, rz450e_signals.decode_soc),
            (rz450e_signals.DID_CAPACITY, rz450e_signals.decode_capacity),
            (rz450e_signals.DID_PRIMARY_V_I, rz450e_signals.decode_primary_v_i),
        ]
        idx = 0
        # Fault containment (docs/13 item 16.1, added 2026-08-04) - same
        # pattern/rationale as _tx_loop and _ingest_rz_bus. `idx` is
        # incremented BEFORE the risky request()/decode() call so a failure
        # on one DID doesn't get the loop stuck retrying that same DID
        # forever - it still advances to the next one in the round-robin.
        last_exc_log_t = 0.0
        exc_count_since_log = 0
        while self._running:
            did, decoder = cycle[idx % len(cycle)]
            idx += 1
            try:
                resp = self.did_client.request(did, timeout=rz450e_signals.DID_RESPONSE_TIMEOUT_S)
                if resp:
                    vals = decoder(resp)
                    if vals:
                        self._ingest_validated(f'DID 0x{did[0]:02X}{did[1]:02X}', vals)
            except Exception as exc:
                exc_count_since_log += 1
                now = time.monotonic()
                if now - last_exc_log_t >= 1.0:
                    self.log_fn(f'DID POLL ERROR (thread continuing) - '
                                f'{exc_count_since_log}x in the last ~1s, most recent: {exc!r}')
                    last_exc_log_t = now
                    exc_count_since_log = 0
            time.sleep(rz450e_signals.DID_INTER_REQUEST_GAP_S)

    # ── TX side ──────────────────────────────────────────────────────────
    def _prun_tick_loop(self):
        """Free-running 10ms tick: PRUN counter + voltage-latch toggle. Kept
        on its own precise cadence, independent of the TX-due-check loop
        below (docs/03: 'just a free-running counter, generate internally')."""
        next_due = time.monotonic()
        while self._running:
            now = time.monotonic()
            if now >= next_due:
                self._prun = (self._prun + 1) % 4
                self._latch ^= 1
                self._tick10 += 1
                next_due += 0.01
                if next_due < now:
                    next_due = now + 0.01
            time.sleep(0.001)

    def _apply_charge_ramp(self, leaf_state):
        """Charger-request ramp emulation (docs/06), added 2026-07-31 -
        ported from Leaf_BMS_Emulator, confirmed against real hardware
        there (bit-level diff of every HVBAT ID, idle vs. real charge-
        session captures): while a real 0x1F2 charge request is active AND
        the user has "Emulate charger request" enabled, `charger_limit_kw`
        ramps from 0.0 kW up to the configured ramp target at a rate set by
        the uprate level, OVERRIDING whatever static/mapped value is
        already in `leaf_state` - instead of just sending a static number,
        matching what a real onboard charger's own power-negotiation curve
        looks like on the bus. Runs BEFORE the management engine's own
        per-cell overvoltage taper (`ac_charge_taper`, split from
        `charge_target_taper` 2026-08-01 - see management_engine.py), so
        that safety feature still gets the final say and can reduce the
        ramped value further - it's never bypassed.

        Requires BOTH the Leaf-side 0x1F2 request AND the RZ450e-side
        `charge_permission_input` interlock (user directive, 2026-07-31) -
        the ramp only ever represents an ACTUALLY-AUTHORIZED charge session,
        not just the Leaf asking. If the Leaf is asking to charge but
        RZ450e hasn't granted permission, this asserts an explicit charger-
        stop rather than silently falling back to whatever static/mapped
        charger_limit_kw would otherwise apply - see the `else` branch
        below.

        Uses a real measured dt (not a fixed-tick assumption like the
        reference project's own steady 10ms loop) since this bridge's tick
        loop doesn't run on a fixed period."""
        now = time.monotonic()
        dt = (now - self._chg_ramp_last_t) if self._chg_ramp_last_t is not None else 0.0
        self._chg_ramp_last_t = now

        chg_cfg = self.state.charge_emulation
        emulate_on = bool(chg_cfg.get('charge_emulate'))
        leaf_wants_charge = self.sequencer.charge_active(now)

        # Replug detection (docs/13 item 13.4, fixed 2026-08-03): a bare
        # rising edge of leaf_wants_charge is NOT reliable evidence of an
        # actual physical unplug/replug - a single dropped/delayed 0x1F2
        # frame (goes stale >CHG_CMD_FRESH_S=0.5s, then resumes) or the VCM's
        # own charge-negotiation retry behavior can produce the exact same
        # rising edge with the plug never having moved, which would silently
        # clear an emergency-tier latch with no relation to a real replug -
        # exactly the gap an independent review pass found in the original
        # (2026-08-01) version of this fix. Require the request to have been
        # genuinely ABSENT for at least leaf_signals.CHG_END_STOP_S (3.0s -
        # the same threshold the shutdown sequencer itself uses to decide a
        # charge session has really ended, not a new invented number) before
        # a resumption counts as a "replug" allowed to clear the latch. A
        # shorter gap just resumes the ramp normally without touching it.
        if not leaf_wants_charge:
            if self._chg_inactive_since is None:
                self._chg_inactive_since = now
        else:
            if not self._prev_charge_active:
                gap = (now - self._chg_inactive_since) if self._chg_inactive_since is not None else 0.0
                if gap >= leaf_signals.CHG_END_STOP_S:
                    self.management.notify_charge_replug()
                else:
                    self.log_fn(f'0x1F2 charge request resumed after only {gap:.2f}s - too brief to '
                                f'count as a real unplug/replug (need >= {leaf_signals.CHG_END_STOP_S:g}s); '
                                f'ramp resumes normally, any latched hard cut is NOT cleared by this')
            self._chg_inactive_since = None
        self._prev_charge_active = leaf_wants_charge

        rz_authorized = bool(self.state.get_input('charge_permission_input'))

        # Data-presence gate (docs/13 item 13.1b, added 2026-08-03, REWORKED
        # same day after user clarification): driving is allowed to run on
        # cached/last-known-good values until the general staleness watchdog
        # (bridge/management_engine.py) would object - charging must not,
        # full stop. This checks only that every required signal has been
        # seen LIVE at least once this session (not a custom freshness
        # timer - a separate stricter timer was the first version of this
        # fix and was explicitly rejected by the user in favor of reusing
        # the same watchdog driving already gets). Once this gate passes
        # once, ongoing staleness protection is entirely the general
        # watchdog's job - it already forces a charge stop (and, per the
        # same clarification, now also sets full_charge_flag) when it
        # fires, so there is no separate/duplicate timer here. See
        # _charge_data_ready().
        data_ready, missing_key = True, None
        if chg_cfg.get('require_live_data_to_charge', True):
            data_ready, missing_key = self._charge_data_ready()

        active = emulate_on and leaf_wants_charge and rz_authorized and data_ready

        if active:
            self._last_charge_gate_reason = None   # docs/13 item 14.4 - no gate reason while genuinely ramping
            level = int(chg_cfg.get('chg_uprate_level', 0)) & 7
            rate = leaf_signals.CHG_RAMP_RAW_PER_S / (2 ** (7 - level))
            if self._chg_ramp_raw is None:
                self._chg_ramp_raw = float(leaf_signals.CHG_RAMP_START_RAW)
                self.log_fn(f'0x1F2 charge request active + RZ450e permission granted + battery data '
                            f'genuinely live - starting 0x1DC charger ramp: level {level}, 0 kW rising '
                            f'{rate * 0.1:.3g} kW/s')
            # Clamp the configured ramp target into [ac_min_kw, ac_max_kw]
            # (added 2026-08-06, user directive: "the maximum AC charging is
            # only 6.6 kilowatt for the leaf... make this configurable, min
            # and max kW request for AC") before converting to raw counts -
            # protects against a target left above the Leaf's real AC
            # charger ceiling (or below its floor) regardless of what the
            # slider's own wider GUI bounds allow.
            ac_min_kw = float(chg_cfg.get('ac_min_kw', 0.0))
            ac_max_kw = float(chg_cfg.get('ac_max_kw', leaf_signals.CHARGE_EMULATION_BOUNDS['charge_target_kw'][1]))
            target_kw = max(ac_min_kw, min(ac_max_kw, float(chg_cfg.get('charge_target_kw', 0.0))))
            target_raw = round((target_kw + 10) / 0.1)
            target_raw = max(leaf_signals.CHG_RAMP_START_RAW, min(target_raw, leaf_signals.CHG_IDLE_RAW - 1.0))
            # Symmetric rate-limited step toward the target (bug fix,
            # 2026-08-06, found from a real bench test log showing
            # charger_limit_kw jumping straight from 5.20kW to 5.00kW in a
            # single 10ms tick - physically impossible at the configured
            # ramp rate): previously used min(current + rate*dt, target),
            # which correctly rate-limits an INCREASING target but lets a
            # DECREASING target (e.g. the user lowering charge_target_kw
            # live, or the ac_min_kw/ac_max_kw clamp above moving the
            # effective target) snap the raw value straight down to the new
            # target on the very next tick with no rate limit applied at
            # all - the actual cause of the observed "ramp down jumps
            # around" symptom. Now moves toward the target by at most
            # rate*dt per tick in EITHER direction, giving real fine
            # (100W-scale, per the user's "fine 100W precision" request)
            # control over a falling target, matching the existing rising
            # behavior exactly.
            step = rate * dt
            if self._chg_ramp_raw < target_raw:
                self._chg_ramp_raw = min(self._chg_ramp_raw + step, float(target_raw))
            elif self._chg_ramp_raw > target_raw:
                self._chg_ramp_raw = max(self._chg_ramp_raw - step, float(target_raw))
            raw = max(leaf_signals.CHG_RAMP_START_RAW, int(self._chg_ramp_raw))
            leaf_state['charger_limit_kw'] = raw * 0.1 - 10
            self._chg_uprate_current = level
        else:
            if self._chg_ramp_raw is not None:
                self.log_fn('Charge request ended (or permission/data withheld) - 0x1DC charger ramp '
                            'back to idle (uprate 0, charger limit from slider)')
            self._chg_ramp_raw = None
            self._chg_uprate_current = 0

            # Mismatch: the Leaf is actively asking to charge but something
            # is blocking it - either RZ450e hasn't authorized it (user
            # directive, 2026-07-31) or the battery data isn't fresh/present
            # enough to trust (2026-08-03) - force an explicit stop instead
            # of leaving whatever static/mapped value is already sitting in
            # leaf_state. full_charge_flag is the confirmed real-hardware
            # "instant charge stop + contactor drop, needs a physical
            # replug" bit (docs/03) - exactly matching the desired "charger
            # stops, sleeps till replugged" behavior. ShutdownSequencer.
            # _should_wind_down's `charge_authorized` parameter treats this
            # same mismatch as "not really charging" too, so the bridge is
            # free to wind down/sleep here rather than staying awake forever
            # just because the Leaf keeps asking.
            if emulate_on and leaf_wants_charge and not active:
                if not rz_authorized:
                    reason = 'RZ450e has not granted charge_permission_input'
                elif not data_ready:
                    reason = f'no genuinely live battery data yet, cannot start on cached/default values (missing: {missing_key})'
                else:
                    reason = 'blocked'
                self._last_charge_gate_reason = reason
                leaf_state['full_charge_flag'] = 1
                leaf_state['charge_limit_kw'] = 0.0
                leaf_state['charger_limit_kw'] = -10.0
                self.log_fn(f'Charge stop - Leaf wants to charge but {reason}')
            else:
                # Not this gate's doing (docs/13 item 14.4) - either the ramp
                # is genuinely idle (no request/not enabled) or it's active;
                # clear any stale reason from a PREVIOUS stop so
                # charge_status_summary() doesn't keep repeating an old
                # explanation once this specific gate is no longer the cause.
                self._last_charge_gate_reason = None
        return leaf_state

    def charge_status_summary(self):
        """One accurate, human-readable line describing the charger-request
        ramp's current state, for both GUI surfaces (gui/panels.py's
        ChargeEmulationPanel, gui/dashboard.py's Charge emulation section) to
        share instead of each re-deriving their own (docs/13 item 14.4, fixed
        2026-08-03).

        Bug this replaces: both GUI spots used to check only `bool(tx.get(
        'full_charge_flag'))` and, if set, always print "RZ450e permission
        not granted" - but full_charge_flag can now be forced true for
        SEVERAL different reasons that have nothing to do with the interlock:
        the live-data gate (13.1b) not yet satisfied, the general staleness
        watchdog (13.1) escalating mid-charge, or - not a fault at all - the
        AC daily/extended target SoC being reached (a normal, successful
        charge-complete). Showing "permission not granted" for any of those
        actively misleads a user troubleshooting why charging won't start
        when RZ450e's permission was never the actual problem.

        Resolves the reason from the SAME live status text/state each
        subsystem already produces, in priority order (most specific/most
        likely to be the actual current cause first), rather than guessing:
        1. `ManagementEngine`'s own staleness_watchdog status (covers the
           general watchdog forcing a charge-stop, docs/13 item 13.1).
        2. `ManagementEngine`'s own ac_charge_taper status (covers the target
           SoC reached - a SUCCESS case, not a fault).
        3. This engine's own `_last_charge_gate_reason` (covers the ramp's
           own dual/triple-gate check: RZ450e permission or the live-data
           gate)."""
        cfg = self.state.charge_emulation
        if not bool(cfg.get('charge_emulate')):
            return 'disabled - "Max power for charger" uses the Signal Mapping value'

        tx = self.state.snapshot_leaf_tx()
        full_stop = bool(tx.get('full_charge_flag'))
        if full_stop:
            mgmt_status = self.state.snapshot_management_status()
            stale = mgmt_status.get('staleness_watchdog', '')
            if 'charging stopped' in stale:
                return f'STOPPED - staleness watchdog: {stale}'
            ac_status = mgmt_status.get('ac_charge_taper', '')
            if 'target' in ac_status and 'reached' in ac_status:
                return f'CHARGE COMPLETE - {ac_status}'
            reason = self._last_charge_gate_reason or 'blocked (reason unavailable)'
            return f'STOPPED - Leaf wants to charge but {reason}'

        now = time.monotonic()
        leaf_wants = self.sequencer.charge_active(now)
        rz_auth = bool(self.state.get_input('charge_permission_input'))
        if leaf_wants and rz_auth:
            return 'ramping/active - both triggers present'
        return 'idle - no active, authorized charge request'

    def _charge_data_ready(self):
        """True if every one of the 96 per-cell voltages AND the pack temp
        extremes has been seen LIVE at least once during the CURRENT bridge
        session (docs/13 item 13.1b, reworked 2026-08-03, corrected
        2026-08-04). Deliberately strict - ALL 96 cells, not a pack-level
        summary or a fallback to last_known_good - since the whole point of
        this gate is "don't start moving current into/out of the pack on
        data we haven't actually confirmed is flowing right now."

        "Current session" means since the last waiting_for_wake -> startup
        transition (ShutdownSequencer.get_session_start()), NOT since the
        app process launched. Bug found 2026-08-04: this originally checked
        SharedState.age_of() is not None, i.e. "has this key EVER updated
        since SharedState was created" - since SharedState lives for the
        whole app run and rz450e_ts is never cleared, once a cell went live
        ONCE this gate stayed satisfied FOREVER, even across a full sleep
        -> wake cycle where RZ450e might now be completely disconnected.
        Comparing each signal's last-update timestamp against the CURRENT
        session's start time closes that gap: a contact from a previous
        session doesn't count, only one from this one does.

        This is still NOT an ongoing freshness/age check - once a signal has
        gone live this session it stays "ready" here even if it later goes
        stale within the same session; that ongoing case is deliberately
        left to the general staleness watchdog (bridge/management_engine.py),
        which already forces a charge stop (see its full_charge_flag note)
        on the same 60s/+5s schedule driving gets - no separate/duplicate
        timer at this layer. Returns (ready, first_missing_key_found)."""
        session_start = self.sequencer.get_session_start()
        keys = rz450e_signals.cell_voltage_keys() + ['temp_max', 'temp_min']
        timestamps = self.state.timestamps_of(keys)
        for key in keys:
            ts = timestamps.get(key)
            if ts is None or ts < session_start:
                return False, key
        return True, None

    def _compose_leaf_state(self):
        leaf_state = dict(leaf_signals.DEFAULTS)
        leaf_state.update(self.mapping.apply(self.state))
        leaf_state.update(derive_capacity_outputs(self.state))
        leaf_state = self._apply_charge_ramp(leaf_state)
        leaf_state = self.management.apply(leaf_state, self.state)

        # AC-taper dynamic uprate override (added 2026-08-06, user
        # directive: the transmitted 0x1DC uprate bits are a real signal
        # "used somewhere else in the system" and must genuinely represent
        # the rate actually in use, not just the manually-configured
        # chg_uprate_level). ManagementEngine.ac_uprate_level is None
        # whenever the AC taper isn't actively converging (full power,
        # disabled, no data, emergency) - only overrides the ramp's own
        # static-level assignment (_apply_charge_ramp, above) while the
        # taper is genuinely rate-limiting, so it never fights the user's
        # manual setting outside that window. See management_engine.py's
        # ac_charge_taper block / _select_ac_uprate_level().
        if self.management.ac_uprate_level is not None:
            self._chg_uprate_current = self.management.ac_uprate_level

        # Final safety net (docs/06): guarantees every field is inside its
        # documented encodable range before frame-building bitpacks it - the
        # bitmask encode WRAPS an out-of-range value instead of saturating it
        # (confirmed 2026-07-31: a negative discharge_limit_kw wrapped to a
        # raw value that decoded back to 251.0kW - full power, the opposite
        # of the safety intent). Logged into the fault history (never
        # silently absorbed) since needing to clamp means something upstream
        # (a mapping tie, a derived-signal formula) is producing an out-of-
        # spec number.
        leaf_state, clamped = leaf_signals.clamp_state(leaf_state)
        clamped_keys_now = {key for key, _, _ in clamped}
        if clamped_keys_now and not self._active_clamp_keys:
            self.log_fn('OUTPUT CLAMPED - ' + '; '.join(
                f'{k}: {orig!r} -> {new!r}' for k, orig, new in clamped))
        for key in clamped_keys_now | self._active_clamp_keys:
            match = next((c for c in clamped if c[0] == key), None)
            active = match is not None
            detail = f'{match[1]!r} -> {match[2]!r} (out of documented encodable range)' if match else ''
            self.management.fault_log.update(f'clamp_{key}', f'Output clamped: {key}', 'warn', active, detail)
        self._active_clamp_keys = clamped_keys_now

        return leaf_state

    def _build_frame(self, arb_id, s, t_ms, in_startup):
        tick10, latch = self._tick10, self._latch
        gen = self.state.generated_enabled
        # 'prun' checkbox (fixed 2026-08-01, item 12.5): previously had zero
        # effect - unchecking it now forces the free-running PRUN counter to
        # a constant 0 in every frame that carries it, instead of the real
        # rolling value. Doesn't touch CRC/timing, purely the PRUN field
        # itself and whichever opaque table entry it happens to index.
        prun = self._prun if gen.get('prun', True) else 0

        if arb_id == 0x1DB:
            if in_startup and t_ms < leaf_signals.T_VALID:
                return leaf_signals.build_1db_startup(s, prun, t_ms)
            s2 = dict(s)
            if in_startup and t_ms < leaf_signals.T_RUNNING:
                s2['failsafe_status'] = 0
            use_latch = latch if gen.get('voltage_latch_toggle', True) else 0
            return leaf_signals.build_1db(s2, prun, use_latch)

        if arb_id == 0x1DC:
            if in_startup and t_ms < leaf_signals.T_VALID:
                return leaf_signals.build_1dc_startup(prun, first_frame=(tick10 <= 1))
            # Uprate bits come from the charger-ramp state computed once per
            # tick in _apply_charge_ramp() (docs/06) - 0 whenever no charge
            # request is active, matching the confirmed real-hardware
            # behavior that idle frames always carry uprate 0. Previously
            # this read the static chg_uprate_level slider directly any
            # time the checkbox was on, regardless of charge state - wrong
            # per that same confirmation.
            code_override = None if gen.get('code_1dc', True) else (0, 0, 0)
            return leaf_signals.build_1dc(s, prun, uprate=self._chg_uprate_current,
                                           code_1dc_override=code_override)

        if arb_id == leaf_signals.HVBAT_ID_1C2:
            if not gen.get('heartbeat_1c2', True):
                return None
            return leaf_signals.build_1c2(tick10)

        if arb_id == leaf_signals.HVBAT_ID_1ED:
            return leaf_signals.build_1ed(s, prun)

        if arb_id == 0x55B:
            ir_raw = 0x3FF if (in_startup and t_ms < leaf_signals.T_VALID) else 904
            refuse_sleep = self.sequencer.refuse_sleep_value(time.monotonic())
            return leaf_signals.build_55b(s, prun, ir_raw=ir_raw, refuse_sleep=refuse_sleep)

        if arb_id == 0x5BC:
            if in_startup and tick10 <= 1:
                return leaf_signals.build_5bc_first(s, tick10)
            gids_raw = 0x3FF if (in_startup and t_ms < leaf_signals.T_VALID + 50) else None
            chg_time_override = None if gen.get('chg_time_5bc', True) else (0, 0, 0)
            return leaf_signals.build_5bc(s, tick10, gids_raw=gids_raw,
                                           chg_time_override=chg_time_override)

        if arb_id == 0x59E:
            return leaf_signals.build_59e(s)

        if arb_id == 0x5C0:
            hist_override = None if gen.get('hist_5c0', True) else (0, 0, 0)
            return leaf_signals.build_5c0(s, tick10, hist_override=hist_override)

        if arb_id == leaf_signals.HVBAT_ID_5EB:
            if not gen.get('seq_5eb', True):
                return None
            return leaf_signals.build_5eb(tick10)

        return None

    def _tx_loop(self):
        next_due = {arb_id: 0.0 for arb_id in leaf_signals.TX_PERIOD_MS}
        last_phase = None
        last_soft_cut = False
        last_hard_cut = False
        # Fault containment (docs/13 item 16.1, added 2026-08-04): this loop
        # previously had ZERO exception handling anywhere in its body - an
        # uncaught error inside _compose_leaf_state() or frame-building
        # (a KeyError from an unexpected config shape, any bug) permanently
        # killed this thread for the rest of the app session, with only the
        # heartbeat below (item 2.2's fix) eventually noticing 2s later and
        # showing "NOT RESPONDING," never recovering without a full app
        # restart. Now a single bad tick is caught, logged, and skipped -
        # the loop keeps running and the very next tick tries again.
        # last_tick_monotonic is deliberately only stamped at each
        # successful exit point BELOW, inside the try, not unconditionally
        # at the top of the loop as it was before - this matters: if it were
        # stamped unconditionally, a PERSISTENTLY failing tick (not a
        # one-off) would look identical to a healthy loop to the heartbeat
        # check, silently defeating the exact warning item 2.2 built. With
        # this placement, one-off failures self-heal invisibly (the next
        # good tick refreshes the heartbeat well inside the 2.0s window) but
        # a persistent failure still correctly trips "NOT RESPONDING" -
        # catch-and-continue must never mask an ongoing problem as "fine."
        # Log spam is rate-limited to once/second (with a count of how many
        # occurred) since a fast-repeating failure could otherwise flood the
        # Log panel faster than a human (or the Dashboard's mirrored log)
        # could read it.
        last_exc_log_t = 0.0
        exc_count_since_log = 0
        while self._running:
            now = time.monotonic()
            try:
                # Bridge not started (idle) or armed but no real Leaf traffic
                # seen yet (waiting_for_wake): nothing to compose, nothing to
                # send - RZ450e monitoring/mapping edits keep working via the
                # ingest threads regardless (see main.py rev 6 changelog).
                if self.sequencer.phase in ('idle', 'waiting_for_wake'):
                    phase = self.sequencer.phase
                    if phase != last_phase:
                        self.log_fn(f'Sequencer phase: {last_phase} -> {phase}')
                        last_phase = phase
                    self.last_tick_monotonic = now
                    time.sleep(0.02)
                    continue

                leaf_state = self._compose_leaf_state()
                self.state.set_leaf_tx_many(leaf_state)
                self.state.set_management_status(self.management.status)

                hard_cut = leaf_state.get('relay_cut_request', 0) not in (0, None)
                soft_cut = bool(leaf_state.get('capacity_empty')) or bool(leaf_state.get('full_charge_flag'))
                if hard_cut and not last_hard_cut:
                    self.log_fn('HARD CUT asserted (relay_cut_request) - ' + '; '.join(
                        f'{k}: {v}' for k, v in self.management.status.items() if 'EMERGENCY' in v or 'STALE' in v))
                elif soft_cut and not last_soft_cut and not hard_cut:
                    self.log_fn('Soft cut asserted (capacity_empty/full_charge_flag)')
                last_hard_cut, last_soft_cut = hard_cut, soft_cut

                charge_authorized = bool(self.state.get_input('charge_permission_input'))
                # Wind-down trigger 5 is staleness-specific ONLY (docs/13 item
                # 14.3, fixed 2026-08-03) - `hard_cut` above (used for the Log
                # panel/last_hard_cut tracking) reflects ANY hard-cut source, but
                # only the staleness watchdog's own escalation is allowed to wind
                # the bridge down; every other hard cut just latches and keeps
                # the bridge running/transmitting (see ManagementEngine.
                # staleness_hard_cut's docstring).
                phase, timing = self.sequencer.tick(self.management.staleness_hard_cut, charge_authorized)
                if phase != last_phase:
                    if phase == 'startup' and last_phase == 'waiting_for_wake' and self.sequencer.rearmed_naturally:
                        # A waiting_for_wake -> startup transition following a
                        # NATURAL re-arm (the sequencer itself completed a real
                        # wind-down, not just a Stop/Start Bridge button press -
                        # see ShutdownSequencer.rearmed_naturally) is the closest
                        # analog this bridge has to "the car being powered down
                        # and back on" (docs/12 finding F8, added 2026-08-01) -
                        # clears a latched hard cut. Bug fixed 2026-08-01 (found
                        # by an independent review pass): this originally fired
                        # on EVERY such transition, including a bare Stop/Start
                        # Bridge toggle with the car's VCM never having lost
                        # power at all - silently clearing an emergency-tier
                        # latch with no relation to an actual power cycle.
                        self.management.notify_session_start()
                    self.log_fn(f'Sequencer phase: {last_phase} -> {phase}')
                    last_phase = phase

                if phase in ('idle', 'waiting_for_wake', 'stopped'):
                    self.last_tick_monotonic = now
                    time.sleep(0.02)
                    continue

                vehicle = self.state.snapshot_vehicle()
                active_ids = leaf_signals.hvbat_ids_for(vehicle['battery_gen'], vehicle['battery_kwh'])

                for arb_id, period_ms in leaf_signals.TX_PERIOD_MS.items():
                    if arb_id not in active_ids:
                        continue
                    if phase == 'winding_down' and not self.sequencer.id_active_during_winddown(arb_id, timing):
                        continue
                    if arb_id in (leaf_signals.HVBAT_ID_1C2,):
                        pass  # heartbeat is immediate from bus-wake, no start-offset gate
                    elif arb_id in (0x1DB, 0x1DC) and phase == 'startup' and timing < leaf_signals.T_1DB_START:
                        continue
                    elif arb_id in (0x55B, 0x5BC) and phase == 'startup' and timing < leaf_signals.T_55B_START:
                        continue
                    elif arb_id in (0x59E, 0x5C0, leaf_signals.HVBAT_ID_5EB) and phase == 'startup' \
                            and timing < leaf_signals.T_59E_START:
                        continue

                    if now < next_due[arb_id]:
                        continue
                    next_due[arb_id] = now + period_ms / 1000.0

                    frame = self._build_frame(arb_id, leaf_state, timing, phase == 'startup')
                    if frame is not None:
                        self.leaf_bus.send(arb_id, frame)

                self.last_tick_monotonic = now
            except Exception as exc:
                exc_count_since_log += 1
                if now - last_exc_log_t >= 1.0:
                    self.log_fn(f'TX LOOP ERROR (tick skipped, thread continuing) - '
                                f'{exc_count_since_log}x in the last ~1s, most recent: {exc!r}')
                    last_exc_log_t = now
                    exc_count_since_log = 0
                time.sleep(0.01)
                continue

            time.sleep(0.001)
