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


# ── ShutdownSequencer.charge_active() - the shared 0x1F2 detection ─────────
def test_charge_active_public_method():
    seq = ShutdownSequencer()
    check('charge_active is False with no 0x1F2 traffic ever seen',
          seq.charge_active(time.monotonic()) is False)
    seq.note_leaf_rx(CHG_ID, _chg_request_frame())
    check('charge_active is True right after a trans=1 frame',
          seq.charge_active(time.monotonic()) is True)
    time.sleep(leaf_signals.CHG_CMD_FRESH_S + 0.15)
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
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp starts at exactly 0.0kW (CHG_RAMP_START_RAW)', leaf_state['charger_limit_kw'] == 0.0)
    check('transmitted uprate matches the configured level', engine._chg_uprate_current == 7)


def test_ramp_rate_at_level_7_is_2kw_per_second():
    engine, state = _fresh_engine()
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
    state.charge_emulation.update({'charge_emulate': True, 'charge_target_kw': 50.0, 'chg_uprate_level': 7})
    state.update_input('charge_permission_input', 1)
    engine.sequencer.note_leaf_rx(CHG_ID, _chg_request_frame())
    engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp is active right after a charge request', engine._chg_ramp_raw is not None)

    time.sleep(leaf_signals.CHG_CMD_FRESH_S + 0.15)
    default_charger_kw = leaf_signals.DEFAULTS['charger_limit_kw']
    leaf_state = engine._apply_charge_ramp(dict(leaf_signals.DEFAULTS))
    check('ramp resets to None once the 0x1F2 request goes stale', engine._chg_ramp_raw is None)
    check('transmitted uprate drops back to 0 (matches "idle frames always carry uprate 0")',
          engine._chg_uprate_current == 0)
    check('charger_limit_kw is left untouched (not overridden) once the ramp is inactive',
          leaf_state['charger_limit_kw'] == default_charger_kw)


# ── Safety: the per-cell taper must stay authoritative over charger_limit_kw ─
def test_charger_limit_kw_safety_taper_applies_even_without_rz450e_interlock():
    mgmt = ManagementEngine()
    rz = SharedState()
    for i in range(1, 97):
        rz.update_input(f'cell_{i:02d}', 4.35)   # above the 4.30V emergency-high default
    rz.update_input('temp_max', 77.0)
    rz.update_input('current', 0.0)
    rz.update_input('charge_permission_input', 0.0)   # RZ450e interlock NOT active

    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 80.0   # simulate the charge-ramp having raised it
    out = mgmt.apply(leaf_state, rz)
    check('charger_limit_kw is zeroed by the per-cell emergency taper even when the RZ450e '
          'interlock is not active (the charge-ramp is a different, Leaf-side signal that can '
          'be out of sync with it)', out['charger_limit_kw'] == 0.0, f"got {out['charger_limit_kw']}")
    check('the hard cut still fires too (worst_high above emergency_high_v)',
          out.get('relay_cut_request', 0) == 3)


def test_charger_limit_kw_proactive_taper_applies_without_interlock_too():
    mgmt = ManagementEngine()
    rz = SharedState()
    for i in range(1, 97):
        rz.update_input(f'cell_{i:02d}', 4.00)   # inside the 3.90-4.10V proactive taper window (~50%)
    rz.update_input('temp_max', 77.0)
    rz.update_input('current', 0.0)
    rz.update_input('charge_permission_input', 0.0)

    leaf_state = dict(leaf_signals.DEFAULTS)
    leaf_state['charger_limit_kw'] = 80.0
    out = mgmt.apply(leaf_state, rz)
    check('charger_limit_kw is proactively tapered (roughly halved) even without the interlock active',
          0 < out['charger_limit_kw'] < 80.0, f"got {out['charger_limit_kw']}")


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
