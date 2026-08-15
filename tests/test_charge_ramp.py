"""Verification script for the charger-request ramp feature (docs/06),
implemented 2026-07-31 - run directly (`py tests/test_charge_ramp.py`).
Ported from Leaf_BMS_Emulator, confirmed there against real hardware (bit-
level diff of every HVBAT ID, idle vs. real charge-session captures): while
a real 0x1F2 charge request is active and "Emulate charger request" is on,
charger_limit_kw ramps from 0.0 kW to a configured target at a rate set by
an uprate level, instead of just sending a static number.

Also covers a related safety fix made while adding this: the per-cell
overvoltage taper on charger_limit_kw used to only apply while the RZ450e-
side charge_permission_input interlock was active - fine before this ramp
existed, but the ramp can now raise charger_limit_kw whenever the LEAF-side
0x1F2 request is active, a different signal that can be out of sync with
the interlock. The taper must be authoritative over charger_limit_kw
unconditionally, same as charge_limit_kw already is.

Also covers the `charge_authorized`/dual-trigger requirement added
2026-07-31 (user directive): the ramp only ever runs while BOTH the Leaf-
side 0x1F2 request AND the RZ450e-side charge_permission_input interlock are
active - if the Leaf wants to charge but RZ450e hasn't authorized it, the
ramp forces an explicit stop (full_charge_flag=1, charge/charger_limit_kw
zeroed) rather than silently falling back to a static value.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import leaf_signals
from bridge.management_engine import ManagementEngine
from bridge.mapping_engine import MappingEngine
from bridge.realtime_engine import CHG_ID, RealtimeEngine, ShutdownSequencer
from bridge.state import SharedState

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


def _fresh_engine():
    state = SharedState()
    engine = RealtimeEngine(state, MappingEngine(), ManagementEngine(), None, None)
    return engine, state


def _chg_request_frame():
    # trans = (data[2] >> 5) & 3 -> 0x20 gives trans=1 (Charge_StatusTransitionReqest active)
    return bytes([0x00, 0x00, 0x20])


def _seed_fresh_battery_data(state):
    """docs/13 item 13.1b (added 2026-08-03): the charge ramp now also
    requires all 96 per-cell voltages + pack temp extremes to be fresh
    before it will run - most of the pre-existing tests below only cared
    about the emulate/leaf-request/rz-permission triggers, so they need this
    helper to keep passing under the new, stricter default."""
    for i in range(1, 97):
        state.update_input(f'cell_{i:02d}', 3.80)
    state.update_input('temp_max', 25.0)
    state.update_input('temp_min', 23.9)


# ── ShutdownSequencer.charge_active() - the shared 0x1F2 detection ─────────
def test_charge_active_public_method():
    seq = ShutdownSequencer()
    check('charge_active is False with no 0x1F2 traffic ever seen',
          seq.charge_active(time.monotonic()) is False)
    seq.note_leaf_rx(CHG_ID, _chg_request_frame())
    check('charge_active is True right after a trans=1 frame',
          seq.charge_active(time.monotonic()) is True)
    time.sleep(seq.config['chg_cmd_fresh_s'] + 0.15)
    check('charge_active goes False once the 0x1F2 frame goes stale',
          seq.charge_active(time.monotonic()) is False)


# ── Ramp gating ─────────────────────────────────────────────────────────────
def test_ramp_inactive_without_charge_emulate_enabled():
    engine, state = _fresh_engine()
    state.charge_emulation['charge_emulate'] = False
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())  # a real, authorized charge request IS active...
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp stays inactive if "Emulate charger request" is off, even with a real, '
          'authorized charge request active',
          engine._chg_ramp_raw is None and engine._chg_uprate_current == 0)


def test_ramp_inactive_without_a_real_charge_request():
    engine, state = _fresh_engine()
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    # no note_leaf_rx() call at all - no charge request has ever been seen
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp stays inactive with the checkbox on and RZ450e permission granted, but no '
          'real 0x1F2 charge request', engine._chg_ramp_raw is None and engine._chg_uprate_current == 0)


def test_ramp_inactive_without_rz450e_permission():
    engine, state = _fresh_engine()
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())   # Leaf wants to charge...
    # ...but charge_permission_input is never set (RZ450e has not authorized it)
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp stays inactive with a real Leaf charge request but no RZ450e permission '
          '(both triggers are required)',
          engine._chg_ramp_raw is None and engine._chg_uprate_current == 0)


def test_stop_flag_asserted_when_leaf_wants_charge_but_not_authorized():
    engine, state = _fresh_engine()
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('full_charge_flag is forced to 1 (instant stop, needs a physical replug) when the '
          'Leaf wants to charge but RZ450e has not granted permission',
          leaf_state['full_charge_flag'] == 1, f"got {leaf_state['full_charge_flag']}")
    check('charge_limit_kw is forced to 0.0 in the same mismatch', leaf_state['charge_limit_kw'] == 0.0)
    check('charger_limit_kw is forced to -10.0 (raw idle-stop value) in the same mismatch',
          leaf_state['charger_limit_kw'] == -10.0)


def test_stop_flag_not_forced_when_charge_emulate_disabled():
    engine, state = _fresh_engine()
    state.charge_emulation['charge_emulate'] = False   # feature is opt-in
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())   # same mismatch as above
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('with "Emulate charger request" off, the mismatch does not force full_charge_flag '
          '(this feature is opt-in - static/mapped behavior is left alone)',
          leaf_state.get('full_charge_flag') == leaf_signals.DEFAULTS.get('full_charge_flag'))


def test_stop_flag_not_forced_when_nothing_is_active():
    engine, state = _fresh_engine()
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    # neither a Leaf request nor RZ450e permission - ordinary non-charging operation
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('with no Leaf charge request at all, nothing is forced (there is no session to stop)',
          leaf_state.get('full_charge_flag') == leaf_signals.DEFAULTS.get('full_charge_flag'))


# ── Ramp math ────────────────────────────────────────────────────────────────
def test_ramp_starts_at_zero_kw_and_matches_configured_level():
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp starts at exactly 0.0kW (CHG_RAMP_START_RAW)', leaf_state['charger_limit_kw'] == 0.0)
    check('transmitted uprate matches the configured level', engine._chg_uprate_current == 7)


def test_ramp_rate_at_level_7_is_2kw_per_second():
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))  # initializes the ramp at 0.0kW
    engine._chg_ramp_last_t = time.monotonic() - 1.0   # force a deterministic 1.0s dt, no real sleep needed
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('after 1.0s at level 7 (confirmed 2.0 kW/s real-hardware rate), charger_limit_kw is ~2.0kW',
          abs(leaf_state['charger_limit_kw'] - 2.0) < 0.15, f"got {leaf_state['charger_limit_kw']}")


def test_ramp_rate_halves_per_level_down():
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 6})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    engine._chg_ramp_last_t = time.monotonic() - 1.0
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('level 6 ramps at half level 7\'s rate (~1.0 kW/s)',
          abs(leaf_state['charger_limit_kw'] - 1.0) < 0.1, f"got {leaf_state['charger_limit_kw']}")


def test_ramp_caps_at_configured_target():
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 1.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    engine._chg_ramp_last_t = time.monotonic() - 5.0   # would reach ~10kW uncapped at 2kW/s
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp never exceeds the configured target (1.0kW), even after enough time to overshoot it',
          leaf_state['charger_limit_kw'] == 1.0, f"got {leaf_state['charger_limit_kw']}")


def test_ramp_resets_when_charge_request_goes_stale():
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp is active right after a charge request', engine._chg_ramp_raw is not None)

    time.sleep(state.engine_timing['chg_cmd_fresh_s'] + 0.15)
    default_charger_kw = leaf_signals.DEFAULTS['charger_limit_kw']
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp resets to None once the 0x1F2 request goes stale', engine._chg_ramp_raw is None)
    check('transmitted uprate drops back to 0 (matches "idle frames always carry uprate 0")',
          engine._chg_uprate_current == 0)
    check('charger_limit_kw is left untouched (not overridden) once the ramp is inactive',
          leaf_state['charger_limit_kw'] == default_charger_kw)


# ── docs/13 item 13.1b: charging can't start on cached/default data ─────────
# Reworked 2026-08-03 after user clarification: no separate custom freshness
# timer - just "has genuinely live data arrived at all" as a one-time startup
# gate. Ongoing staleness protection is entirely the general watchdog's job
# (tested in test_management_engine.py), not duplicated here.
def test_ramp_blocked_when_cell_data_has_never_arrived():
    engine, state = _fresh_engine()
    # Deliberately do NOT seed any cell/temp data - both triggers otherwise present.
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp does NOT start with no per-cell/temp data at all, even with both other triggers present',
          engine._chg_ramp_raw is None)
    check('full_charge_flag is forced to 1 when blocked by missing battery data',
          leaf_state['full_charge_flag'] == 1)
    check('charger_limit_kw is forced to -10.0 (idle) when blocked by missing battery data',
          leaf_state['charger_limit_kw'] == -10.0)


def test_ramp_blocked_when_cell_data_is_only_from_a_previous_session():
    # Bug found 2026-08-04 (user question: what does "session" mean here?):
    # data live before the CURRENT bridge session began must not satisfy
    # the gate, even though it's live somewhere in SharedState - otherwise a
    # contact from hours ago, before a full sleep -> wake cycle, would let
    # charging start on stale-from-a-prior-session data.
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)   # live now, i.e. "previous session"
    engine.sequencer.session_start = time.monotonic() + 0.05   # simulate a NEW session starting later
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('data live only during a PREVIOUS session does not satisfy the charge-start gate',
          engine._chg_ramp_raw is None)
    check('full_charge_flag is forced to 1 when blocked by previous-session-only battery data',
          leaf_state['full_charge_flag'] == 1)


def test_ramp_proceeds_once_data_goes_live_within_the_current_session():
    engine, state = _fresh_engine()
    engine.sequencer.session_start = time.monotonic()
    time.sleep(0.02)
    _seed_fresh_battery_data(state)   # live AFTER the current session began
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('data that went live AFTER the current session started satisfies the gate',
          engine._chg_ramp_raw is not None)


def test_ramp_stays_ready_once_data_has_gone_live_even_if_it_later_ages():
    # This is the key behavioral difference from the rejected first version
    # of this fix: once a signal has been live at least once THIS SESSION,
    # THIS gate never blocks it again just because it's gotten old - that's
    # the general staleness watchdog's job (60s/+5s), not a duplicate timer.
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    time.sleep(0.1)   # data is now "old" but was genuinely live once, this session
    ready, missing_key = engine._charge_data_ready()
    check('the charge-start gate stays satisfied once data has gone live this session, '
          'regardless of age - '
          'it is a one-time "ever live" check, not an ongoing freshness timer',
          ready is True, missing_key)


def test_ramp_proceeds_when_data_gate_disabled_despite_no_data():
    engine, state = _fresh_engine()
    # No cell/temp data seeded, but the gate is explicitly turned off.
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7,
                                    'require_live_data_to_charge': False})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp starts with no battery data at all when require_live_data_to_charge is disabled',
          engine._chg_ramp_raw is not None)


def test_ramp_proceeds_with_only_partial_cell_coverage_blocked():
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)
    state.rz450e.pop('cell_50', None)   # one cell's live value never arrived
    state.rz450e_ts.pop('cell_50', None)
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp is blocked if even ONE of the 96 cells is missing (strict "all 96," not a subset)',
          engine._chg_ramp_raw is None)
    check('full_charge_flag reflects the missing-cell block',
          leaf_state['full_charge_flag'] == 1)


# ── docs/13 item 13.4: a real minimum gap is required to count as a replug ──
def test_brief_charge_dropout_does_not_clear_latch():
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    engine.management._hard_latched = True   # simulate an existing latched hard cut

    # Simulate a single dropped/delayed 0x1F2 frame: charge_active briefly
    # goes False (older than CHG_CMD_FRESH_S) then True again, well under
    # CHG_END_STOP_S later - must NOT count as a real replug.
    engine._chg_inactive_since = time.monotonic() - 0.6
    engine.sequencer.chg_last_frame_t = None
    engine.sequencer.chg_trans = None
    engine._prev_charge_active = False
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('a brief charge dropout (< CHG_END_STOP_S) resuming does NOT clear a latched hard cut',
          engine.management._hard_latched is True)


def test_genuine_gap_does_clear_latch():
    engine, state = _fresh_engine()
    _seed_fresh_battery_data(state)
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    engine.management._hard_latched = True   # simulate an existing latched hard cut

    # Simulate a real unplug/replug: charge_active has been false for
    # longer than CHG_END_STOP_S before resuming.
    engine._chg_inactive_since = time.monotonic() - (state.engine_timing['chg_end_stop_s'] + 0.5)
    engine.sequencer.chg_last_frame_t = None
    engine.sequencer.chg_trans = None
    engine._prev_charge_active = False
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('a genuine gap (>= CHG_END_STOP_S) before resuming DOES clear a latched hard cut',
          engine.management._hard_latched is False)


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
