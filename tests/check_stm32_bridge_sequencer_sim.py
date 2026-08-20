"""Multi-tick simulation cross-checking STM32_MiniLeaf_Bridge_Translator_uVision's
bridge_sequencer.c phase state machine against the real
bridge/realtime_engine.py ShutdownSequencer.

No ARM/x86 C toolchain is available in this dev environment - see
check_stm32_rz_ingest_decode.py's own docstring for the full explanation.
This re-derives bridge_sequencer.c's phase/wind-down-trigger logic
independently in Python (hand-typed from the C source, not imported) and
drives it tick-by-tick against the real, unmodified ShutdownSequencer with a
shared fake clock and the same randomized Leaf-bus event stream (ignition
IDs, 0x1F2 charge-request frames, generic "other" traffic, and silence
gaps), comparing (phase, timing) every tick.

staleness_hard_cut/charge_authorized are driven directly as scenario inputs
here (not derived from a live management/RZ450e simulation) - this isolates
sequencer correctness, which is already what ShutdownSequencer.tick()'s own
signature takes as external parameters; management_engine.c's own staleness
detection is separately verified by check_stm32_management_engine_sim.py.

One deliberate behavior difference, NOT a bug: the C port skips the Python
app's manual 'idle' phase (no Start Bridge button on this hardware) and
arms immediately at boot - this harness calls real_seq.arm() once up front
to put the real sequencer in the same 'waiting_for_wake' starting state.

Run: py tests/check_stm32_bridge_sequencer_sim.py [--ticks N] [--seed S]
"""
import argparse
import os
import random
import sys
import time as time_mod

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge.leaf_signals as ls
from bridge.realtime_engine import ShutdownSequencer

_fake_now = [0.0]


def fake_monotonic():
    return _fake_now[0]


time_mod.monotonic = fake_monotonic

ENGINE_TIMING = {k: d for (k, _l, _lo, _hi, _s, d) in ls.ENGINE_TIMING_FIELDS}


class SimSequencer:
    """Independent re-derivation of bridge_sequencer.c's phase state
    machine (hand-typed, NOT imported from the C or from
    bridge/realtime_engine.py)."""

    def __init__(self):
        self.phase = 'waiting_for_wake'   # C port skips 'idle' - arms immediately at boot
        self.session_start = 0.0
        self.shutdown_t0 = 0.0
        self.last_leaf_rx = None
        self.rearmed_naturally = False
        self.ignition_seen = [False, False, False]
        self.ignition_last_seen = [0.0, 0.0, 0.0]
        self.chg_last_frame = None
        self.chg_trans = -1
        self.chg_cmd = 0
        self.chg_trans_last_change = None
        self.chg_seen_active = False
        self.ignition_off_pending = False
        self.ignition_off_since = 0.0
        self.chg_end_pending = False
        self.chg_end_since = 0.0
        self.chg_stall_pending = False
        self.chg_stall_since = 0.0

    def run_state_fresh(self, now):
        if not any(self.ignition_seen):
            return False
        newest = max(t for t, seen in zip(self.ignition_last_seen, self.ignition_seen) if seen)
        return (now - newest) <= ENGINE_TIMING['ignition_quiet_s']

    def ignition_off_detected(self, now):
        if now - self.session_start < ENGINE_TIMING['ignition_grace_s']:
            return False
        if not all(self.ignition_seen):
            return False
        newest = max(self.ignition_last_seen)
        return (now - newest) > ENGINE_TIMING['ignition_quiet_s']

    def chg_fresh(self, now):
        return self.chg_last_frame is not None and (now - self.chg_last_frame) <= ENGINE_TIMING['chg_cmd_fresh_s']

    def charge_active(self, now):
        if not self.chg_fresh(now):
            return False
        return self.chg_trans == 1 or self.chg_cmd > ls.CHG_CMD_IDLE

    def note_leaf_rx(self, arb_id, data, now):
        self.last_leaf_rx = now
        if self.phase == 'waiting_for_wake':
            self.phase = 'startup'
            self.session_start = now
            # notify_session_start() effect not modeled here (no latch state
            # in this isolated sequencer-only test) - only phase/timing is compared.

        if arb_id == 0x108:
            self.ignition_seen[0] = True
            self.ignition_last_seen[0] = now
        elif arb_id == 0x1CB:
            self.ignition_seen[1] = True
            self.ignition_last_seen[1] = now
        elif arb_id == 0x284:
            self.ignition_seen[2] = True
            self.ignition_last_seen[2] = now
        elif arb_id == 0x1F2 and len(data) >= 3:
            trans = (data[2] >> 5) & 3
            cmd = ((data[0] & 3) << 8) | data[1]
            if trans != self.chg_trans:
                self.chg_trans_last_change = now
            self.chg_trans = trans
            self.chg_cmd = cmd
            self.chg_last_frame = now
            if trans == 1 or cmd > ls.CHG_CMD_IDLE:
                self.chg_seen_active = True

    def should_wind_down(self, now, staleness_hard_cut, charge_authorized):
        if staleness_hard_cut:
            return True
        chg_is_active = self.charge_active(now)
        chg_effective = chg_is_active and charge_authorized
        if chg_effective:
            self.ignition_off_pending = False
            self.chg_end_pending = False
            self.chg_stall_pending = False
            return False

        if self.ignition_off_detected(now):
            if not self.ignition_off_pending:
                self.ignition_off_pending = True
                self.ignition_off_since = now
            if now - self.ignition_off_since >= ENGINE_TIMING['ignition_off_delay_s']:
                return True
        else:
            self.ignition_off_pending = False

        if self.chg_seen_active and not chg_effective and not self.run_state_fresh(now):
            if not self.chg_end_pending:
                self.chg_end_pending = True
                self.chg_end_since = now
            if now - self.chg_end_since >= ENGINE_TIMING['chg_end_stop_s']:
                return True
        else:
            self.chg_end_pending = False

        if self.chg_fresh(now) and not self.chg_seen_active and not self.run_state_fresh(now):
            anchor = self.chg_trans_last_change if self.chg_trans_last_change is not None else now
            if not self.chg_stall_pending or anchor > self.chg_stall_since:
                self.chg_stall_since = anchor
                self.chg_stall_pending = True
            if now - self.chg_stall_since >= ENGINE_TIMING['chg_stall_timeout_s']:
                return True
        else:
            self.chg_stall_pending = False

        if self.last_leaf_rx is not None and (now - self.last_leaf_rx) >= ENGINE_TIMING['bus_silence_timeout_s']:
            return True

        return False

    def advance(self, now, staleness_hard_cut, charge_authorized):
        if self.phase in ('startup', 'running'):
            t_ms = (now - self.session_start) * 1000.0
            if t_ms >= ls.T_RUNNING:
                self.phase = 'running'
            if self.should_wind_down(now, staleness_hard_cut, charge_authorized):
                self.phase = 'winding_down'
                self.shutdown_t0 = now
            return t_ms
        if self.phase == 'winding_down':
            elapsed_ms = (now - self.shutdown_t0) * 1000.0
            if elapsed_ms >= ls.PWRDOWN_STAGE4_MS:
                self.phase = 'stopped'
            return elapsed_ms
        if self.phase == 'stopped':
            quiet_for = 0.0 if self.last_leaf_rx is None else now - self.last_leaf_rx
            if quiet_for >= ls.PWRDOWN_DEFAULT_COOLDOWN_S:
                self.phase = 'waiting_for_wake'
                self.rearmed_naturally = True
                self.ignition_seen = [False, False, False]
                self.chg_seen_active = False
            return 0.0
        return 0.0


def new_phase(rnd):
    kind = rnd.choice([
        'normal_driving', 'ignition_off', 'charge_active', 'charge_end',
        'charge_stall', 'bus_silence', 'staleness', 'not_authorized_charge',
        'intermittent',
    ])
    p = {'kind': kind}
    p['send_ignition'] = kind not in ('bus_silence',)
    p['ignition_rate'] = 1.0 if kind != 'intermittent' else 0.3
    p['send_other_traffic'] = kind != 'bus_silence'
    p['staleness_hard_cut'] = (kind == 'staleness') and rnd.random() < 0.3
    p['charge_authorized'] = kind != 'not_authorized_charge'
    if kind in ('charge_active', 'not_authorized_charge'):
        p['chg_trans'] = 1
        p['chg_cmd'] = rnd.randint(0, 4000)
        p['send_ignition'] = False   # matches driving scenario being "plugged in, not moving"
    elif kind == 'charge_stall':
        p['chg_trans'] = 0
        p['chg_cmd'] = rnd.randint(0, ls.CHG_CMD_IDLE)   # below idle threshold - a "request but never activates"
        p['send_ignition'] = False
    elif kind == 'charge_end':
        p['chg_trans'] = None   # was active before, now nothing - handled via phase transition below
    else:
        p['chg_trans'] = None
    return p


def run(n_ticks=8000, seed=42, dt_min=0.05, dt_max=1.0, max_report=30):
    rnd = random.Random(seed)
    real_seq = ShutdownSequencer(config=dict(ENGINE_TIMING))
    real_seq.arm()
    sim = SimSequencer()

    now = 0.0
    phase_ticks_left = 0
    phase = {}
    mismatches = []
    was_charging = False

    for tick in range(n_ticks):
        if phase_ticks_left <= 0:
            phase = new_phase(rnd)
            phase_ticks_left = rnd.randint(3, 60)   # dwell time in TICKS (not seconds - dt varies)
        phase_ticks_left -= 1

        now += rnd.uniform(dt_min, dt_max)
        _fake_now[0] = now

        # Ignition traffic
        if phase['send_ignition'] and rnd.random() < phase['ignition_rate']:
            for arb_id in (0x108, 0x1CB, 0x284):
                if rnd.random() < 0.9:   # occasionally drop one of the 3 IDs this tick, matching real bus jitter
                    real_seq.note_leaf_rx(arb_id, b'')
                    sim.note_leaf_rx(arb_id, b'', now)

        # 0x1F2 charge-request traffic
        if phase['kind'] == 'charge_end' and was_charging:
            pass   # deliberately stop sending 0x1F2 this phase - simulates the request just disappearing
        elif phase.get('chg_trans') is not None:
            trans = phase['chg_trans']
            cmd = phase['chg_cmd']
            data = bytes([(cmd >> 8) & 3, cmd & 0xFF, (trans & 3) << 5])
            real_seq.note_leaf_rx(0x1F2, data)
            sim.note_leaf_rx(0x1F2, data, now)
            was_charging = (trans == 1 or cmd > ls.CHG_CMD_IDLE)

        # Generic "other" Leaf-bus traffic (keeps last_leaf_rx fresh unless in a bus_silence phase)
        if phase['send_other_traffic'] and rnd.random() < 0.95:
            real_seq.note_leaf_rx(0x5A5, b'\x00')
            sim.note_leaf_rx(0x5A5, b'\x00', now)

        staleness_hard_cut = phase['staleness_hard_cut']
        charge_authorized = phase['charge_authorized']

        real_phase, real_timing = real_seq.tick(staleness_hard_cut, charge_authorized)
        sim_timing = sim.advance(now, staleness_hard_cut, charge_authorized)
        sim_phase = sim.phase

        if real_phase != sim_phase:
            mismatches.append((tick, phase['kind'], 'phase', sim_phase, real_phase))
        elif abs(sim_timing - real_timing) > 1.0:   # 1ms tolerance (C port uses integer ms ticks)
            mismatches.append((tick, phase['kind'], 'timing', sim_timing, real_timing))

        if len(mismatches) > max_report:
            break

    print(f"Ran {tick} ticks (~{now:.0f}s simulated), {len(mismatches)} mismatches")
    for m in mismatches[:max_report]:
        print(" ", m)
    return len(mismatches) == 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticks', type=int, default=8000)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    ok = run(n_ticks=args.ticks, seed=args.seed)
    sys.exit(0 if ok else 1)
