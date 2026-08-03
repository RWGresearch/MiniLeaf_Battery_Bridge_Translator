"""Verification script for bridge/realtime_engine.py's ShutdownSequencer -
run directly (`py tests/test_shutdown_sequencer.py`). Covers the two auto-
sleep/shutdown gaps found 2026-07-31 by auditing this bridge against
Leaf_BMS_Emulator's confirmed real-hardware findings:

F1 - LB_RefusetoSleep (0x55B) was hardcoded to 0 (always "ignition on")
     instead of being derived from ignition-state freshness, per the
     reference project's confirmed real-capture behavior.
F2 - the post-shutdown re-arm used a flat timer since entering 'stopped'
     instead of requiring the Leaf bus to have gone GENUINELY quiet, which
     is exactly the bug the reference project hit (their own rev 20) and
     fixed (rev 21) after a real capture showed a still-talking VCM
     instantly re-triggering the wake detector.

Also covers the `charge_authorized` parameter added 2026-07-31 (user
directive, alongside the charger-request ramp feature): an active 0x1F2
charge request only keeps the bridge awake while RZ450e's own
charge_permission_input interlock also authorizes it - a request the Leaf
keeps making without authorization must NOT be treated as a reason to stay
awake indefinitely.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import leaf_signals
from bridge.realtime_engine import CHG_ID, ShutdownSequencer

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


# ── F1: LB_RefusetoSleep derivation ────────────────────────────────────────
def test_refuse_sleep_zero_while_ignition_fresh():
    seq = ShutdownSequencer()
    seq.arm()
    seq.note_leaf_rx(0x108, b'\x00')
    check('refuse_sleep is 0 ("ignition on") right after an ignition ID is seen',
          seq.refuse_sleep_value(time.monotonic()) == 0)


def test_refuse_sleep_one_when_never_seen():
    seq = ShutdownSequencer()
    check('refuse_sleep is 1 when no ignition ID has ever been seen this session',
          seq.refuse_sleep_value(time.monotonic()) == 1)


def test_refuse_sleep_one_after_ignition_goes_stale():
    seq = ShutdownSequencer()
    seq.arm()
    seq.note_leaf_rx(0x108, b'\x00')
    time.sleep(leaf_signals.IGNITION_QUIET_S + 0.15)
    check('refuse_sleep flips to 1 once ignition IDs go stale (matches the real-capture '
          '~150ms key-off behavior, at this watchdog\'s granularity)',
          seq.refuse_sleep_value(time.monotonic()) == 1)


def test_refuse_sleep_forced_one_during_winding_down():
    seq = ShutdownSequencer()
    seq.arm()
    seq.note_leaf_rx(0x108, b'\x00')   # fresh ignition - would normally read 0
    seq.phase = 'winding_down'
    check('refuse_sleep is forced to 1 during winding_down regardless of ignition freshness '
          '(power-down only ever runs on/after a key-off, per the reference project)',
          seq.refuse_sleep_value(time.monotonic()) == 1)


# ── F2: genuine bus-quiet re-arm ───────────────────────────────────────────
def test_stopped_does_not_rearm_while_bus_still_active():
    seq = ShutdownSequencer()
    seq.arm()
    seq.note_leaf_rx(0x1DB, b'\x00')   # enters 'startup', sets last_leaf_rx_t
    check('sanity: sequencer entered startup', seq.phase == 'startup')

    # Force straight to the tail end of winding_down (the staged-timing
    # mechanics that get it there aren't what's under test here).
    seq.phase = 'winding_down'
    seq.shutdown_t0 = time.monotonic() - (leaf_signals.PWRDOWN_STAGE4_MS / 1000.0) - 0.05
    phase, _ = seq.tick(False)
    check('sanity: reached stopped once PWRDOWN_STAGE4_MS elapsed', phase == 'stopped', phase)

    # Simulate a VCM that's STILL actively transmitting (an "already
    # in-flight" frame, the exact scenario the reference project's real
    # capture caught) - even past where the OLD flat-timer bug would have
    # already re-armed.
    seq.note_leaf_rx(0x1DB, b'\x00')
    time.sleep(leaf_signals.PWRDOWN_DEFAULT_COOLDOWN_S + 0.15)
    seq.note_leaf_rx(0x1DB, b'\x00')   # bus is still chattering right up to the check
    phase, _ = seq.tick(False)
    check('does NOT re-arm while the bus is still actively transmitting, even past the '
          'old flat-timer window (this is exactly the bug that got fixed)',
          phase == 'stopped', f'phase={phase}')


def test_stopped_rearms_once_bus_genuinely_quiet():
    seq = ShutdownSequencer()
    seq.arm()
    seq.note_leaf_rx(0x1DB, b'\x00')
    seq.phase = 'winding_down'
    seq.shutdown_t0 = time.monotonic() - (leaf_signals.PWRDOWN_STAGE4_MS / 1000.0) - 0.05
    phase, _ = seq.tick(False)
    check('sanity: reached stopped', phase == 'stopped', phase)

    time.sleep(leaf_signals.PWRDOWN_DEFAULT_COOLDOWN_S + 0.15)   # bus goes genuinely quiet
    phase, _ = seq.tick(False)
    check('re-arms to waiting_for_wake once the bus has been GENUINELY quiet for the cooldown',
          phase == 'waiting_for_wake', f'phase={phase}')
    check('a NATURAL re-arm (genuine wind-down completed, not a button press) sets '
          'rearmed_naturally=True - added 2026-08-01, this is what is allowed to clear a '
          'latched hard cut (see test_management_engine.py)',
          seq.rearmed_naturally is True)


# ── Bug fix, 2026-08-01: a MANUAL re-arm (Stop Bridge then Start Bridge) ────
# must NOT be mistaken for a genuine car power-cycle - found by an
# independent review pass: notify_session_start() originally fired on EVERY
# waiting_for_wake -> startup transition, so simply toggling Stop/Start
# Bridge while the car's VCM never lost power could silently clear a latched
# emergency-tier hard cut. rearmed_naturally distinguishes the two cases. ──
def test_manual_arm_does_not_mark_natural_rearm():
    seq = ShutdownSequencer()
    # Simulate having JUST come from a genuine natural re-arm (as the
    # previous test confirms happens) - then the user manually stops and
    # restarts the bridge.
    seq.rearmed_naturally = True
    seq.arm()
    check('arm() (Start Bridge) always clears rearmed_naturally, even if the PREVIOUS '
          'wake was natural - a fresh manual press must not inherit that status',
          seq.rearmed_naturally is False)


# ── charge_authorized: an unauthorized 0x1F2 request must not keep the ─────
# bridge awake forever (user directive, 2026-07-31, added with the ramp) ────
def _chg_request_frame():
    return bytes([0x00, 0x00, 0x20])   # trans=1 -> Charge_StatusTransitionReqest active


def test_authorized_charge_request_keeps_bridge_awake():
    seq = ShutdownSequencer()
    seq.arm()
    seq.note_leaf_rx(CHG_ID, _chg_request_frame())
    check('sanity: charge_active is True right after the request', seq.charge_active(time.monotonic()) is True)
    check('authorized -> should_wind_down returns False (stays awake, unchanged prior behavior)',
          seq._should_wind_down(False, charge_authorized=True) is False)
    check('authorized -> does not start the chg_end_since wind-down timer',
          seq._chg_end_since is None)


def test_unauthorized_charge_request_does_not_keep_bridge_awake():
    seq = ShutdownSequencer()
    seq.arm()
    seq.note_leaf_rx(CHG_ID, _chg_request_frame())
    seq._should_wind_down(False, charge_authorized=False)
    check('an active-but-unauthorized request starts the chg_end_since timer instead of '
          'resetting it (treated the same as "not really charging")',
          seq._chg_end_since is not None)


def test_unauthorized_charge_request_eventually_winds_down():
    seq = ShutdownSequencer()
    seq.arm()
    seq.note_leaf_rx(CHG_ID, _chg_request_frame())
    seq._should_wind_down(False, charge_authorized=False)
    check('sanity: chg_end_since timer started', seq._chg_end_since is not None)
    seq._chg_end_since = time.monotonic() - leaf_signals.CHG_END_STOP_S - 0.1   # force elapsed, no real sleep
    result = seq._should_wind_down(False, charge_authorized=False)
    check('after CHG_END_STOP_S of "Leaf wants to charge but RZ450e has not authorized it," '
          'the sequencer decides to wind down', result is True)


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
