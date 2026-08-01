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

    def arm(self):
        """Start Bridge pressed: begin waiting for real Leaf bus traffic."""
        with self.lock:
            self.phase = 'waiting_for_wake'
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

    def _should_wind_down(self, hard_cut_this_tick, charge_authorized=True):
        with self.lock:
            now = time.monotonic()
            if self._manual_shutdown_requested:
                self._manual_shutdown_requested = False
                return True
            if hard_cut_this_tick:
                return True   # 5th trigger, specific to this bridge (docs/06/07)

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

            return False

    def tick(self, hard_cut_this_tick, charge_authorized=True):
        """Advance the phase state machine. Returns (phase, t_ms_or_elapsed).
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
                if self._should_wind_down(hard_cut_this_tick, charge_authorized):
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
        self.log_fn = lambda msg: None   # replaced by the GUI with the log panel's put()

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
        while self._running:
            try:
                kind, _label, msg = self.rz_bus.rx_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind != 'rx':
                continue
            arb_id, data = msg.arbitration_id, bytes(msg.data)
            if self.did_client.feed(arb_id, data):
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
                self.state.update_inputs(vals)

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
            self.sequencer.note_leaf_rx(msg.arbitration_id, bytes(msg.data))

    def _did_poll_loop(self):
        cycle = [
            (rz450e_signals.DID_SOC, rz450e_signals.decode_soc),
            (rz450e_signals.DID_CAPACITY, rz450e_signals.decode_capacity),
            (rz450e_signals.DID_PRIMARY_V_I, rz450e_signals.decode_primary_v_i),
        ]
        idx = 0
        while self._running:
            did, decoder = cycle[idx % len(cycle)]
            idx += 1
            resp = self.did_client.request(did)
            if resp:
                vals = decoder(resp)
                if vals:
                    self.state.update_inputs(vals)
            time.sleep(rz450e_signals.DID_POLL_INTERVAL_S)

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
        per-cell overvoltage taper (`charge_target_taper`), so that safety
        feature still gets the final say and can reduce the ramped value
        further - it's never bypassed.

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
        rz_authorized = bool(self.state.get_input('charge_permission_input'))
        active = emulate_on and leaf_wants_charge and rz_authorized

        if active:
            level = int(chg_cfg.get('chg_uprate_level', 0)) & 7
            rate = leaf_signals.CHG_RAMP_RAW_PER_S / (2 ** (7 - level))
            if self._chg_ramp_raw is None:
                self._chg_ramp_raw = float(leaf_signals.CHG_RAMP_START_RAW)
                self.log_fn(f'0x1F2 charge request active + RZ450e permission granted - starting '
                            f'0x1DC charger ramp: level {level}, 0 kW rising {rate * 0.1:.3g} kW/s')
            target_kw = float(chg_cfg.get('charge_target_kw', 0.0))
            target_raw = round((target_kw + 10) / 0.1)
            self._chg_ramp_raw = min(self._chg_ramp_raw + rate * dt,
                                      float(target_raw), leaf_signals.CHG_IDLE_RAW - 1.0)
            raw = max(leaf_signals.CHG_RAMP_START_RAW, int(self._chg_ramp_raw))
            leaf_state['charger_limit_kw'] = raw * 0.1 - 10
            self._chg_uprate_current = level
        else:
            if self._chg_ramp_raw is not None:
                self.log_fn('Charge request ended (or permission withdrawn) - 0x1DC charger ramp '
                            'back to idle (uprate 0, charger limit from slider)')
            self._chg_ramp_raw = None
            self._chg_uprate_current = 0

            # Mismatch: the Leaf is actively asking to charge but RZ450e has
            # not authorized it (user directive, 2026-07-31) - force an
            # explicit stop instead of leaving whatever static/mapped value
            # is already sitting in leaf_state. full_charge_flag is the
            # confirmed real-hardware "instant charge stop + contactor drop,
            # needs a physical replug" bit (docs/03) - exactly matching the
            # desired "charger stops, sleeps till replugged" behavior, since
            # a real replug is what makes the Leaf send a fresh 0x1F2 request
            # in the first place. ShutdownSequencer._should_wind_down's
            # `charge_authorized` parameter treats this same mismatch as "not
            # really charging" too, so the bridge is free to wind down/sleep
            # here rather than staying awake forever just because the Leaf
            # keeps asking.
            if emulate_on and leaf_wants_charge and not rz_authorized:
                leaf_state['full_charge_flag'] = 1
                leaf_state['charge_limit_kw'] = 0.0
                leaf_state['charger_limit_kw'] = -10.0
        return leaf_state

    def _compose_leaf_state(self):
        leaf_state = dict(leaf_signals.DEFAULTS)
        leaf_state.update(self.mapping.apply(self.state))
        leaf_state.update(derive_capacity_outputs(self.state))
        leaf_state = self._apply_charge_ramp(leaf_state)
        leaf_state = self.management.apply(leaf_state, self.state)

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
        prun, tick10, latch = self._prun, self._tick10, self._latch
        gen = self.state.generated_enabled

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
            return leaf_signals.build_1dc(s, prun, uprate=self._chg_uprate_current)

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
            return leaf_signals.build_5bc(s, tick10, gids_raw=gids_raw)

        if arb_id == 0x59E:
            return leaf_signals.build_59e(s)

        if arb_id == 0x5C0:
            return leaf_signals.build_5c0(s, tick10)

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
        while self._running:
            now = time.monotonic()

            # Bridge not started (idle) or armed but no real Leaf traffic
            # seen yet (waiting_for_wake): nothing to compose, nothing to
            # send - RZ450e monitoring/mapping edits keep working via the
            # ingest threads regardless (see main.py rev 6 changelog).
            if self.sequencer.phase in ('idle', 'waiting_for_wake'):
                phase = self.sequencer.phase
                if phase != last_phase:
                    self.log_fn(f'Sequencer phase: {last_phase} -> {phase}')
                    last_phase = phase
                time.sleep(0.02)
                continue

            leaf_state = self._compose_leaf_state()
            self.state.set_leaf_tx_many(leaf_state)
            self.state.management_status = dict(self.management.status)

            hard_cut = leaf_state.get('relay_cut_request', 0) not in (0, None)
            soft_cut = bool(leaf_state.get('capacity_empty')) or bool(leaf_state.get('full_charge_flag'))
            if hard_cut and not last_hard_cut:
                self.log_fn('HARD CUT asserted (relay_cut_request) - ' + '; '.join(
                    f'{k}: {v}' for k, v in self.management.status.items() if 'EMERGENCY' in v or 'STALE' in v))
            elif soft_cut and not last_soft_cut and not hard_cut:
                self.log_fn('Soft cut asserted (capacity_empty/full_charge_flag)')
            last_hard_cut, last_soft_cut = hard_cut, soft_cut

            charge_authorized = bool(self.state.get_input('charge_permission_input'))
            phase, timing = self.sequencer.tick(hard_cut, charge_authorized)
            if phase != last_phase:
                self.log_fn(f'Sequencer phase: {last_phase} -> {phase}')
                last_phase = phase

            if phase in ('idle', 'waiting_for_wake', 'stopped'):
                time.sleep(0.02)
                continue

            active_ids = leaf_signals.hvbat_ids_for(
                self.state.vehicle['battery_gen'], self.state.vehicle['battery_kwh'])

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

            time.sleep(0.001)
