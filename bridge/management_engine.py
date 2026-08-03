"""Battery management / safety layer: curated, named protection features with
researched defaults (docs/05-battery-management-safety.md). Not a generic
rule engine - each feature below is a fixed function with typed config, so
every feature stays portable to a future STM32 C function.

Soft cut = capacity_empty / full_charge_flag (no dash error).
Hard cut = relay_cut_request + interlock drop (RED dash error) - reserved
for emergency-tier thresholds and the staleness watchdog's escalation.
"""
import time

from bridge import rz450e_signals
from bridge.fault_log import FaultLog


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _ramp_factor(value, floor, ceiling):
    """1.0 at/above `ceiling`, 0.0 at/below `floor`, linear between."""
    if ceiling <= floor:
        return 1.0 if value >= ceiling else 0.0
    return _clamp((value - floor) / (ceiling - floor), 0.0, 1.0)


# Cross-field ordering sanity (docs/05, added 2026-08-01, user directive):
# protects a hand-edited profile.json too, not just live GUI typing -
# gui/panels.py's ManagementPanel separately clamps each field to its own
# (lo, hi) numeric bounds, but can't catch a relationship BETWEEN two
# fields (e.g. an emergency threshold typed less extreme than its own soft
# tier). (feature, field_a, field_b, description) - checked as
# cfg[feature][field_a] < cfg[feature][field_b].
_CONFIG_SANITY_CHECKS = [
    ('low_voltage_cutoff', 'emergency_low_v', 'min_cell_v',
     'the emergency tier should be more extreme (lower) than the soft-cut tier'),
    ('discharge_power_taper', 'taper_zero_v', 'taper_start_v',
     'zero-power point should be below the full-power point'),
    ('charge_target_taper', 'regen_full_v', 'regen_zero_v',
     'full-power point should be below the zero-power point'),
    ('charge_target_taper', 'regen_zero_v', 'emergency_high_v',
     'the emergency tier should be more extreme (higher) than the zero-power point'),
    ('over_temperature_derate', 'charge_low_block_f', 'charge_derate_low_start_f',
     'the cold block point should be below the cold-derate-start point'),
    ('over_temperature_derate', 'charge_derate_start_f', 'charge_hard_stop_f',
     'the derate-start point should be below the hard-stop point'),
    ('over_temperature_derate', 'discharge_derate_start_f', 'discharge_hard_stop_f',
     'the derate-start point should be below the hard-stop point'),
    ('over_temperature_derate', 'discharge_hard_stop_f', 'emergency_temp_f',
     'the emergency tier should be more extreme (hotter) than the soft hard-stop point'),
]


_CHARGE_EMULATION_SANITY_CHECKS = [
    ('ac_full_v', 'ac_zero_v', 'full-power point should be below the zero-power point'),
    ('ac_zero_v', 'ac_emergency_v', 'the emergency tier should be more extreme (higher) than the zero-power point'),
]


def _check_config_sanity(cfg, charge_emulation=None):
    """Returns a list of human-readable violation descriptions - does not
    change or block anything itself; the engine keeps running with whatever
    values are actually present (clamp_state() still protects the CAN bus
    output either way). Surfaced as a fault_log warning, not a crash or a
    silent misbehavior. `charge_emulation` (state.charge_emulation, added
    2026-08-01) is checked too since the AC-charger taper's thresholds live
    there, not in `cfg`, after the regen/AC-charger split."""
    violations = []
    for feature, field_a, field_b, desc in _CONFIG_SANITY_CHECKS:
        fcfg = cfg.get(feature)
        if not fcfg or field_a not in fcfg or field_b not in fcfg:
            continue
        a, b = fcfg[field_a], fcfg[field_b]
        if not (a < b):
            violations.append(f'{feature}.{field_a}={a:g} should be < {feature}.{field_b}={b:g} ({desc})')
    if charge_emulation:
        for field_a, field_b, desc in _CHARGE_EMULATION_SANITY_CHECKS:
            if field_a in charge_emulation and field_b in charge_emulation:
                a, b = charge_emulation[field_a], charge_emulation[field_b]
                if not (a < b):
                    violations.append(
                        f'charge_emulation.{field_a}={a:g} should be < charge_emulation.{field_b}={b:g} ({desc})')
    return violations


def default_config():
    """Researched NMC/lithium-ion defaults, cross-checked against this
    pack's confirmed real-world range (docs/05). Shipped ENABLED, not
    blank - per explicit user confirmation, since everything here is
    editable and gets refined against real hardware over time."""
    return {
        'low_voltage_cutoff': {
            # emergency_low_v 2.80->2.60V and min_soc_pct 10.0->8.0% (user
            # edit, 2026-08-01, docs/13-review-checklist item Part 7): lower
            # emergency threshold so it's less likely to trigger until the
            # cell has truly reached a genuinely extreme low, and a lower
            # SoC backup floor for consistency/redundancy alongside it (SoC
            # remains a backup check only - never an independent trigger).
            'enabled': True, 'min_cell_v': 3.00, 'min_soc_pct': 8.0,
            'emergency_low_v': 2.60,
            # Persistence window (docs/12 finding F5, 2026-07-31): the soft
            # cut only latches once the min-cell condition has held
            # continuously for this many seconds - guards against a single-
            # tick sag transient under a spike load (cold-pack IR roughly
            # doubles vs. 25C) tripping capacity_empty when the cell would
            # have rebounded the instant load dropped. The discharge power
            # taper reaching zero at the same voltage already collapses
            # current/sag well before this window elapses in the normal
            # case; this is the backstop for a fast, brief spike. Emergency
            # tier stays instantaneous - no persistence there.
            'soft_cut_persistence_s': 2.0,
        },
        'discharge_power_taper': {
            'enabled': True,
            # Proactive discharge-power curve (user-specified 2026-07-31,
            # mirroring the regen/charge taper's design): driven by
            # individual cell voltage, not SoC - a single weak/imbalanced
            # cell sags under heavy discharge load well before pack-average
            # SoC would suggest. Full power at/above taper_start_v, ramping
            # to zero at/below taper_zero_v (matches low_voltage_cutoff's
            # min_cell_v soft-cut floor by default, so the ramp reaches zero
            # right as the soft cut engages - a smooth transition instead of
            # full-power-then-sudden-stop).
            # taper_start_v 3.50->3.00V and taper_zero_v 3.00->2.60V (user
            # edit, 2026-08-01): re-anchored to the same 2.60V now used by
            # low_voltage_cutoff's emergency tier, so the taper still
            # reaches zero right around where the cutoff tiers sit.
            'taper_start_v': 3.00, 'taper_zero_v': 2.60,
            # Hysteresis (user-specified): fast to respond to a dip (snap
            # down immediately - cell protection can't wait), slow to
            # recover back to full power once voltage comes back up (avoids
            # power hunting/oscillation if voltage bounces near the
            # threshold under intermittent acceleration). Time in seconds to
            # go from 0% back to 100% power once voltage has recovered.
            'recovery_ramp_s': 3.0,
        },
        'charge_target_taper': {
            'enabled': True,
            # REGEN ONLY as of 2026-08-01 (user directive: "regen and AC
            # charging is not the same... I can regen WAY more power than I
            # can AC charge, so the parameters need to be split"). Drives
            # ONLY charge_limit_kw ("Charge/regen power limit," docs/03) -
            # the Leaf's shared ceiling for regenerative braking while
            # driving AND general charge acceptance. The AC-charger-specific
            # taper (charger_limit_kw) and the daily/extended AC SoC target
            # both moved to state.charge_emulation / the Charge Emulation
            # GUI tab - see this file's own `ac_charge_taper` block further
            # down (NOT a RealtimeEngine method - it lives directly in
            # ManagementEngine.apply(), same as this block) and
            # leaf_signals.py's CHARGE_SLIDERS/CHARGE_CHECKS - since they
            # only ever mattered while actually plugged in anyway.
            #
            # Proactive curve (user-specified, values updated 2026-08-01):
            # the VCM is slow to respond to a charge_limit_kw change, so the
            # taper must start well before the danger zone, not right at its
            # edge. Full power at/below regen_full_v, zero at/above
            # regen_zero_v, linear between.
            'regen_full_v': 4.00, 'regen_zero_v': 4.15,
            'emergency_high_v': 4.30,
            # Hysteresis (added 2026-08-01, user request: "regen we should
            # add some hysteresis? same as discharge?") - same fast-attack/
            # slow-release pattern as discharge_power_taper's recovery_ramp_s.
            'recovery_ramp_s': 3.0,
        },
        'over_temperature_derate': {
            'enabled': True,
            # Cold-side (docs/12 findings F1 + F3, 2026-07-31 research pass):
            # charge_low_block_f and charge_derate_low_start_f are evaluated
            # against the COLDEST probe (temp_min), never the hottest -
            # lithium plating happens in the coldest cells, so a pack whose
            # coldest corner is below freezing must be blocked even if its
            # warmest probe reads fine. Ramps from 0 (charge_low_block_f,
            # 32F/0C) to full (charge_derate_low_start_f, 50F/~10C) - research
            # shows plating risk rises well above 0C at meaningful charge
            # current; our real exposure is regen into a cold-soaked pack
            # (~0.5C), not the 0.09C AC charger.
            'charge_derate_low_start_f': 50.0, 'charge_low_block_f': 32.0,
            # Hot-side charge ramp, evaluated against the hottest probe
            # (temp_max) - unchanged range, but charge_hard_stop_f is now a
            # pure soft ramp-to-zero point, not also a hard-cut trigger (see
            # emergency_temp_f below).
            'charge_derate_start_f': 90.0, 'charge_hard_stop_f': 113.0,
            # Discharge ramp, hottest probe - discharge_hard_stop_f is also
            # now a pure soft ramp-to-zero point, matching the "Soft (ramp)"
            # tier this feature is documented as in docs/05's feature table.
            'discharge_derate_start_f': 131.0, 'discharge_hard_stop_f': 140.0,
            # Emergency hard-cut tier (docs/12 finding F6, 2026-07-31): a
            # second, more extreme threshold above discharge_hard_stop_f,
            # evaluated against the hottest probe - mirrors the two-tier
            # soft/emergency structure the voltage features already have.
            # Self-heating onset for a cell with any plated lithium can begin
            # as low as ~60C (140F). Tightened 2026-08-01 (user edit) from
            # 149F/65C to 141.8F/61C exactly - only ~1.8F/1C of margin above
            # the 140F/60C soft stop, deliberately thinner than the original
            # researched default because there's very little real margin
            # above it in the chemistry to begin with.
            'emergency_temp_f': 141.8,
        },
        'cell_imbalance_monitor': {
            # Monitor/warn-tier only (docs/12 finding F4, 2026-07-31) - never
            # cuts or derates anything. We can't balance cells (that would be
            # the RZ450e pack's own internal cell-supervision hardware, if it
            # still operates in this configuration - see docs/10 open
            # question), but we uniquely see all 96 cells at high rate, so
            # flagging a growing spread is cheap and catches a developing bad
            # cell early: a cell resting 30-50mV below its neighbors is the
            # classic early signature of elevated self-discharge / an
            # internal defect.
            # warn_delta_v 0.05->0.10V (user edit, 2026-08-01) - widened
            # from the original researched 50mV starting point to 100mV.
            'enabled': True, 'warn_delta_v': 0.10,
        },
        'overcurrent_monitor': {
            # Monitor/warn-tier only (docs/12 finding F2, 2026-07-31) - never
            # cuts or derates. Our envelope is gentle by EV standards (Leaf AC
            # charge ~19A/0.09C, drive peak ~230-315A/1.1-1.6C) and no cell
            # datasheet exists to source a real continuous/peak limit from,
            # so this is deliberately NOT wired to an active cutoff yet.
            # Defaults are derived from THIS project's own confirmed specs,
            # not an invented number: continuous_discharge_warn_a sits well
            # below the 0x023 sensor's +/-204.7A saturation ceiling (so a
            # warning still means something - above ~205A the true magnitude
            # is unknown to us regardless), and continuous_charge_warn_a
            # sits above the Leaf onboard AC charger's ~19A max so normal
            # charging never trips it, catching only abnormal charge/regen
            # current. persistence_s avoids flagging a brief acceleration
            # spike as sustained overcurrent.
            'enabled': True, 'continuous_discharge_warn_a': 150.0,
            'continuous_charge_warn_a': 30.0, 'persistence_s': 5.0,
        },
        'staleness_watchdog': {
            'enabled': True, 'soft_cut_s': 60.0, 'hard_escalation_s': 5.0,
        },
        'cell_data_cross_check': {
            # Live redundancy check between the per-cell array
            # (authoritative) and the 0x020 pack-level cell_min/cell_max
            # summary (added 2026-08-01, user directive). docs/02 and
            # docs/04 both describe the pack summary as a "sanity cross-
            # check" against the per-cell messages, but no comparison
            # actually existed in code before this - cell_min/cell_max were
            # only ever used as a complete fallback when the per-cell array
            # was empty. Same soft-then-hard escalation STRUCTURE as the
            # staleness watchdog ("just like the watchdog," per the user),
            # independently tunable from it - same 60s/5s starting point.
            'enabled': True, 'max_delta_v': 0.15, 'soft_cut_s': 60.0, 'hard_escalation_s': 5.0,
        },
    }


class ManagementEngine:
    # NOTE (added 2026-08-01): `.config`/`.status` are read/written from both
    # the GUI thread (gui/panels.py's ManagementPanel/ChargeEmulationPanel,
    # on every keystroke) and the TX thread (apply(), every tick) with no
    # lock of its own - same class of gap as bridge/state.py's
    # `generated_enabled`/`charge_emulation` (see that file's comment).
    # Individual dict item reads/writes remain safe under CPython's GIL;
    # deliberately not retrofitted with a lock this pass, pending the same
    # STM32-architecture discussion - see docs/13-review-checklist-2026-08-01.md.
    def __init__(self, config=None):
        self.config = config or default_config()
        self.status = {}
        self._stale_since = None
        self._discharge_factor_applied = 1.0
        self._regen_factor_applied = 1.0
        self._last_apply_time = None
        self._low_v_condition_since = None
        self._overcurrent_since = None
        self._overcurrent_direction = None
        self._cross_check_since = None
        # Hard-cut latch (docs/12 finding F8, fixed 2026-08-01 per user
        # directive: "it should only reset AFTER the car has been powered
        # down and back on OR if the charger is unplugged and replugged").
        # Previously every hard cut self-cleared the instant the triggering
        # reading recovered - a brief emergency-level spike (voltage or
        # temperature) would assert relay_cut_request/interlock for exactly
        # one tick and clear itself the next, silently. Scoped to HARD cuts
        # only, matching docs/12 §8's own researched distinction (emergency-
        # tier faults latch; derate-tier/soft responses release
        # automatically) - soft cuts (capacity_empty) keep auto-clearing.
        # Cleared only via notify_session_start()/notify_charge_replug(),
        # called by RealtimeEngine on the two real-world conditions above -
        # no manual GUI "unlatch" control.
        self._hard_latched = False
        self.fault_log = FaultLog()

    def notify_session_start(self):
        """Called by RealtimeEngine when the bridge begins a fresh session
        (ShutdownSequencer transitions waiting_for_wake -> startup) - the
        closest analog this bridge has to "the car being powered down and
        back on." Clears a latched hard cut."""
        self._hard_latched = False

    def notify_charge_replug(self):
        """Called by RealtimeEngine when a fresh charge request follows a
        period with none active - a genuine unplug/replug. Clears a latched
        hard cut (the other real-world condition, alongside a fresh session
        start, that's allowed to)."""
        self._hard_latched = False

    def apply(self, leaf_state, rz_state):
        """leaf_state: dict of Leaf output values already produced by
        DEFAULTS + the mapping engine. Returns a new dict with this layer's
        overrides applied (power-limit derating, soft/hard cut flags)."""
        out = dict(leaf_state)
        status = {}
        cfg = self.config

        now = time.monotonic()
        dt = (now - self._last_apply_time) if self._last_apply_time is not None else 0.0
        self._last_apply_time = now

        cell_min_raw = rz_state.get_input('cell_min')
        cell_max_raw = rz_state.get_input('cell_max')
        cells = [rz_state.get_input(k) for k in rz450e_signals.cell_voltage_keys()]
        cells = [c for c in cells if c is not None]
        worst_low = min(cells) if cells else cell_min_raw
        worst_high = max(cells) if cells else cell_max_raw
        soc = rz_state.get_input('soc_pct')
        temp_max = rz_state.get_input('temp_max')

        soft_cut = False
        hard_cut = False

        f = cfg['low_voltage_cutoff']
        if f['enabled']:
            # Cell voltage is the SOLE authoritative trigger (user directive,
            # 2026-07-31): real-time per-cell data determines every safety
            # cutoff. SoC is a BACKUP CHECK ONLY - it never independently
            # fires a cutoff on its own (an earlier version OR'd the two
            # together, which meant a low SoC reading alone could trigger
            # capacity_empty even with every cell perfectly healthy - fixed).
            # SoC is still evaluated every tick and surfaced in status: it
            # confirms the voltage-based decision when they agree, and
            # raises a visible (non-acting) warning when they disagree,
            # which is itself useful information (e.g. a SoC calibration
            # problem) without ever being allowed to act on its own.
            soc_low = soc is not None and soc <= f['min_soc_pct']
            if worst_low is not None and worst_low <= f['emergency_low_v']:
                hard_cut = True
                self._low_v_condition_since = None
                status['low_voltage_cutoff'] = f'EMERGENCY hard cut ({worst_low:.3f}V <= {f["emergency_low_v"]}V)'
            elif worst_low is not None and worst_low <= f['min_cell_v']:
                # Persistence window (docs/12 finding F5): require the
                # condition to hold continuously for soft_cut_persistence_s
                # before latching capacity_empty - guards against a single-
                # tick sag transient under a spike load. The discharge power
                # taper (same zero-point by default) is already collapsing
                # current/sag on its own faster ramp, so this window is a
                # backstop, not the primary defense.
                if self._low_v_condition_since is None:
                    self._low_v_condition_since = now
                held = now - self._low_v_condition_since
                if held >= f['soft_cut_persistence_s']:
                    soft_cut = True
                    agree = ' (SoC backup check agrees)' if soc_low else ' (SoC backup check does not confirm)'
                    status['low_voltage_cutoff'] = (
                        f'soft cut active - cell {worst_low:.3f}V <= {f["min_cell_v"]}V{agree if soc is not None else ""}')
                else:
                    status['low_voltage_cutoff'] = (
                        f'condition present {held:.1f}s/{f["soft_cut_persistence_s"]:.1f}s before soft cut '
                        f'latches (transient-sag guard) - cell {worst_low:.3f}V <= {f["min_cell_v"]}V')
            elif soc_low:
                self._low_v_condition_since = None
                cell_display = f'{worst_low:.3f}V' if worst_low is not None else 'no data'
                status['low_voltage_cutoff'] = (
                    f'SoC backup check reads {soc:.1f}% <= {f["min_soc_pct"]}% floor, but per-cell voltage '
                    f'({cell_display}) does not confirm - cell voltage is authoritative, NOT cutting off; '
                    f'check SoC calibration')
            else:
                self._low_v_condition_since = None
                status['low_voltage_cutoff'] = 'ok'

            # Fault log (docs/14): records every trigger/clear, independent
            # of the cut's own auto-clear behavior - see bridge/fault_log.py.
            self.fault_log.update('low_voltage_emergency', 'Low-voltage EMERGENCY hard cut (per-cell)',
                                  'hard', worst_low is not None and worst_low <= f['emergency_low_v'],
                                  status['low_voltage_cutoff'])
            self.fault_log.update('low_voltage_soft', 'Low-voltage soft cut (capacity_empty)',
                                  'soft', soft_cut, status['low_voltage_cutoff'])

        f = cfg['discharge_power_taper']
        if f['enabled']:
            if worst_low is not None:
                instant_factor = _ramp_factor(worst_low, f['taper_zero_v'], f['taper_start_v'])
            else:
                instant_factor = 1.0

            # Hysteresis: fast attack (snap down immediately on a dip - cell
            # protection can't wait for a slow ramp), slow release (rate-
            # limited recovery back to full power, avoids power hunting if
            # voltage bounces near the threshold under intermittent load).
            if instant_factor < self._discharge_factor_applied:
                self._discharge_factor_applied = instant_factor
            elif instant_factor > self._discharge_factor_applied:
                max_step = dt / max(f['recovery_ramp_s'], 1e-6)
                self._discharge_factor_applied = min(instant_factor, self._discharge_factor_applied + max_step)

            out['discharge_limit_kw'] = out.get('discharge_limit_kw', 0.0) * self._discharge_factor_applied
            if worst_low is not None:
                status['discharge_power_taper'] = (
                    f'applied factor={self._discharge_factor_applied:.2f} (instant={instant_factor:.2f}, '
                    f'worst cell {worst_low:.3f}V - full >= {f["taper_start_v"]}V, zero <= {f["taper_zero_v"]}V)')
            else:
                status['discharge_power_taper'] = (
                    f'applied factor={self._discharge_factor_applied:.2f} (no per-cell voltage data yet)')

        f = cfg['charge_target_taper']
        if f['enabled']:
            # REGEN ONLY (split 2026-08-01, see default_config()'s comment
            # for the full rationale) - drives ONLY charge_limit_kw, the
            # Leaf's shared "Charge/regen power limit" (docs/03), active
            # regardless of SoC or charging context (regen happens while
            # driving, not just plugged in). worst_high is the max of all 96
            # individually read cell voltages, not a pack-level summary.
            if worst_high is not None and worst_high >= f['emergency_high_v']:
                hard_cut = True
                self._regen_factor_applied = 0.0
                cell_factor = 0.0
                cell_status = f'EMERGENCY hard cut ({worst_high:.3f}V >= {f["emergency_high_v"]}V)'
            else:
                if worst_high is not None:
                    instant_factor = 1.0 - _ramp_factor(worst_high, f['regen_full_v'], f['regen_zero_v'])
                    cell_status = (
                        f'proactive regen taper factor={instant_factor:.2f} (worst cell '
                        f'{worst_high:.3f}V - full <= {f["regen_full_v"]}V, zero >= {f["regen_zero_v"]}V)')
                else:
                    instant_factor = 1.0
                    cell_status = 'no per-cell voltage data yet - full power'
                # Hysteresis (added 2026-08-01, user request: "regen we
                # should add some hysteresis? same as discharge?") - same
                # fast-attack/slow-release pattern as discharge_power_taper.
                if instant_factor < self._regen_factor_applied:
                    self._regen_factor_applied = instant_factor
                elif instant_factor > self._regen_factor_applied:
                    max_step = dt / max(f['recovery_ramp_s'], 1e-6)
                    self._regen_factor_applied = min(instant_factor, self._regen_factor_applied + max_step)
                cell_factor = self._regen_factor_applied

            out['charge_limit_kw'] = out.get('charge_limit_kw', 0.0) * cell_factor
            status['charge_target_taper'] = cell_status + f' | applied factor={cell_factor:.2f}'

            self.fault_log.update('overvoltage_emergency', 'Cell overvoltage EMERGENCY hard cut - regen (per-cell)',
                                  'hard', worst_high is not None and worst_high >= f['emergency_high_v'], cell_status)

        # AC-charger taper (split out 2026-08-01 from the old combined
        # charge_target_taper - see default_config()'s comment). Config
        # lives in state.charge_emulation / the Charge Emulation GUI tab,
        # not `cfg`, since it's specifically about the charger, not a
        # general battery-management threshold - `rz_state` here IS the
        # SharedState instance (RealtimeEngine passes self.state), so
        # .charge_emulation is directly available. Drives ONLY
        # charger_limit_kw ("Max power for charger," docs/03) and owns the
        # daily/extended AC SoC target + full_charge_flag, both still gated
        # on charge_permission_input (only ever mattered while plugged in).
        ac_cfg = rz_state.charge_emulation
        if ac_cfg.get('ac_taper_enabled', True):
            target = ac_cfg['extended_target_pct'] if ac_cfg.get('extended_mode') else ac_cfg['daily_target_pct']
            # RZ450e's confirmed charging interlock (0x358) - the user's own
            # design intent for this signal ("if not active, a charge request
            # must not proceed", docs/02). None/missing is treated as NOT
            # charging (safe default - never assert a charge-stop we can't
            # actually confirm the context for).
            charging_active = bool(rz_state.get_input('charge_permission_input'))

            if worst_high is not None and worst_high >= ac_cfg['ac_emergency_v']:
                hard_cut = True
                ac_factor = 0.0
                ac_status = f'EMERGENCY hard cut ({worst_high:.3f}V >= {ac_cfg["ac_emergency_v"]}V)'
            elif worst_high is not None:
                ac_factor = 1.0 - _ramp_factor(worst_high, ac_cfg['ac_full_v'], ac_cfg['ac_zero_v'])
                ac_status = (
                    f'AC charger taper factor={ac_factor:.2f} (worst cell {worst_high:.3f}V - '
                    f'full <= {ac_cfg["ac_full_v"]}V, zero >= {ac_cfg["ac_zero_v"]}V)')
            else:
                ac_factor = 1.0
                ac_status = 'no per-cell voltage data yet - full power'

            # Applies UNCONDITIONALLY, same reasoning as the regen taper
            # above (fixed 2026-07-31 alongside docs/06's charger-request
            # ramp feature, preserved through the 2026-08-01 split): the
            # charge-ramp emulation (RealtimeEngine._apply_charge_ramp) can
            # raise charger_limit_kw whenever the Leaf-side 0x1F2 request is
            # active, a different signal from the RZ450e-side interlock that
            # can plausibly be out of sync with it - this per-cell safety
            # taper must be authoritative regardless of which upstream logic
            # set the value.
            out['charger_limit_kw'] = out.get('charger_limit_kw', 0.0) * ac_factor

            self.fault_log.update(
                'ac_overvoltage_emergency', 'Cell overvoltage EMERGENCY hard cut - AC charger (per-cell)',
                'hard', worst_high is not None and worst_high >= ac_cfg['ac_emergency_v'], ac_status)

            # full_charge_flag (instant charge-stop + HARD CONTACTOR DROP per
            # docs/03) only makes sense while actually plugged in and
            # charging - SAFETY FIX (2026-07-31, preserved through the
            # 2026-08-01 split): gated on the charging interlock so it can
            # only fire during an actual charge session, never from simply
            # driving above the target SoC.
            if charging_active:
                if soc is not None and soc >= target:
                    out['full_charge_flag'] = 1
                    out['charge_limit_kw'] = 0.0
                    out['charger_limit_kw'] = -10.0
                    ac_status += f' | AC charge target {target:.0f}% reached - full_charge_flag set'
                status['ac_charge_taper'] = ac_status
            else:
                status['ac_charge_taper'] = (
                    ac_status + ' | not actively charging per RZ450e interlock (charger ceiling still '
                    'active; AC target/full_charge_flag gated off)')

        f = cfg['over_temperature_derate']
        if f['enabled'] and temp_max is not None:
            # Cold-side decisions use the COLDEST probe (docs/12 finding F1,
            # fixed 2026-07-31) - lithium plating happens in the coldest
            # cells, so testing temp_max here would let charging continue
            # into a partly-frozen pack as long as the warmest corner reads
            # above the block temp. Falls back to temp_max only if temp_min
            # truly isn't available - both decode from the same 0x4A7 frame,
            # so this is a same-frame fallback, not an independent
            # assume-it's-fine default.
            temp_min = rz_state.get_input('temp_min')
            cold_ref = temp_min if temp_min is not None else temp_max

            if temp_max >= f['emergency_temp_f']:
                hard_cut = True
                d_factor = 0.0
                c_factor = 0.0
                status['over_temperature_derate'] = (
                    f'EMERGENCY hard cut - hottest probe {temp_max:.1f}F >= {f["emergency_temp_f"]}F')
            else:
                d_factor = 1.0 - _ramp_factor(temp_max, f['discharge_derate_start_f'], f['discharge_hard_stop_f'])

                # Cold-side derate (docs/12 finding F3): ramps charge/regen
                # acceptance down approaching 0C instead of a single on/off
                # block at the freezing line - our real exposure here is
                # regen into a cold-soaked pack, not the 0.09C AC charger.
                cold_factor = _ramp_factor(cold_ref, f['charge_low_block_f'], f['charge_derate_low_start_f'])
                hot_factor = 1.0 - _ramp_factor(temp_max, f['charge_derate_start_f'], f['charge_hard_stop_f'])
                c_factor = min(cold_factor, hot_factor)

                status['over_temperature_derate'] = (
                    f'discharge_factor={d_factor:.2f}, charge_factor={c_factor:.2f} '
                    f'(coldest probe {cold_ref:.1f}F, hottest probe {temp_max:.1f}F)')

            out['discharge_limit_kw'] = out.get('discharge_limit_kw', 0.0) * d_factor
            out['charge_limit_kw'] = out.get('charge_limit_kw', 0.0) * c_factor
            out['charger_limit_kw'] = out.get('charger_limit_kw', 0.0) * c_factor

            self.fault_log.update('over_temp_emergency', 'Over-temperature EMERGENCY hard cut (hottest probe)',
                                  'hard', temp_max >= f['emergency_temp_f'], status['over_temperature_derate'])
            self.fault_log.update('charge_cold_block', 'Charge/regen blocked - coldest probe at/below freezing',
                                  'warn', cold_ref <= f['charge_low_block_f'], status['over_temperature_derate'])
            self.fault_log.update('discharge_temp_zero', 'Discharge power at zero - over-temperature',
                                  'warn', d_factor <= 0.0, status['over_temperature_derate'])
            self.fault_log.update('charge_temp_zero', 'Charge/regen power at zero - over-temperature',
                                  'warn', c_factor <= 0.0, status['over_temperature_derate'])

        f = cfg['cell_imbalance_monitor']
        if f['enabled']:
            if worst_high is not None and worst_low is not None:
                delta = worst_high - worst_low
                if delta >= f['warn_delta_v']:
                    status['cell_imbalance_monitor'] = (
                        f'WARNING - spread {delta * 1000:.0f}mV >= {f["warn_delta_v"] * 1000:.0f}mV '
                        f'(worst high {worst_high:.3f}V, worst low {worst_low:.3f}V) - monitor only, '
                        f'no cutoff action')
                else:
                    status['cell_imbalance_monitor'] = f'ok (spread {delta * 1000:.0f}mV)'
            else:
                status['cell_imbalance_monitor'] = 'no per-cell voltage data yet'

            self.fault_log.update('cell_imbalance_warn', 'Cell imbalance warning (spread)', 'warn',
                                  worst_high is not None and worst_low is not None
                                  and (worst_high - worst_low) >= f['warn_delta_v'],
                                  status['cell_imbalance_monitor'])

        f = cfg['overcurrent_monitor']
        if f['enabled']:
            current = rz_state.get_input('current')
            discharge_warn_active = False
            charge_warn_active = False
            if current is None:
                status['overcurrent_monitor'] = 'no current data yet'
                self._overcurrent_since = None
                self._overcurrent_direction = None
            else:
                discharging = current > 0
                magnitude = abs(current)
                direction = 'discharge' if discharging else 'charge'
                warn_a = f['continuous_discharge_warn_a'] if discharging else f['continuous_charge_warn_a']
                # 0x023 saturates at +/-204.7A (docs/02) - well below this
                # pack's real physical capability (bench setup has never
                # discharged past 200A; the real pack is fuse-rated to 500A
                # and the factory RZ450E outputs up to 230kW/~660A in short
                # bursts, per user 2026-07-31) - so a reading at/near
                # saturation means "at/above this value, true magnitude
                # unknown," not a precise number, and this monitor can never
                # see the pack's real high-current range at all.
                sat_note = (' - NOTE: at/near 0x023 sensor saturation (+/-204.7A), true magnitude above '
                            'this is unknown (this pack is rated well past this - see docs/02)') if magnitude >= 204.0 else ''

                if magnitude >= warn_a:
                    if self._overcurrent_since is None or self._overcurrent_direction != direction:
                        self._overcurrent_since = now
                        self._overcurrent_direction = direction
                    held = now - self._overcurrent_since
                    if held >= f['persistence_s']:
                        if discharging:
                            discharge_warn_active = True
                        else:
                            charge_warn_active = True
                        status['overcurrent_monitor'] = (
                            f'WARNING - sustained {direction} current {magnitude:.1f}A >= {warn_a}A for '
                            f'{held:.1f}s (monitor only, no cutoff action){sat_note}')
                    else:
                        status['overcurrent_monitor'] = (
                            f'{direction} current {magnitude:.1f}A elevated, {held:.1f}s/'
                            f'{f["persistence_s"]:.1f}s before warning{sat_note}')
                else:
                    self._overcurrent_since = None
                    self._overcurrent_direction = None
                    status['overcurrent_monitor'] = f'ok ({direction} {magnitude:.1f}A){sat_note}'

            self.fault_log.update('overcurrent_discharge_warn', 'Overcurrent warning - discharge', 'warn',
                                  discharge_warn_active, status['overcurrent_monitor'])
            self.fault_log.update('overcurrent_charge_warn', 'Overcurrent warning - charge/regen', 'warn',
                                  charge_warn_active, status['overcurrent_monitor'])

        sanity_violations = _check_config_sanity(cfg, rz_state.charge_emulation)
        status['config_sanity'] = 'ok' if not sanity_violations else '; '.join(sanity_violations)
        self.fault_log.update(
            'config_sanity', 'Battery-management config has an inverted/nonsensical threshold ordering',
            'warn', bool(sanity_violations), status['config_sanity'])

        # Input plausibility rejections (docs/05, added 2026-08-01) - always
        # on, no enable flag, same "always-on safety net" philosophy as
        # output clamping (docs/06 section 4). rz450e_signals.validate_
        # inputs() already keeps a rejected value out of SharedState
        # entirely (bridge/realtime_engine.py's _ingest_validated); this
        # just surfaces it as a live fault_log entry rather than a silent
        # drop, per the user's request to log the reason to the fault page.
        recent_rejections = rz_state.recent_rejections()
        if recent_rejections:
            status['input_validation'] = 'REJECTED (implausible): ' + '; '.join(
                f'{k}={v!r}' for k, v in recent_rejections.items())
        else:
            status['input_validation'] = 'ok'
        self.fault_log.update('input_validation_reject', 'Input plausibility check rejected a value', 'warn',
                              bool(recent_rejections), status['input_validation'])

        f = cfg['cell_data_cross_check']
        if f['enabled']:
            cross_soft_active = False
            cross_hard_active = False
            if cells and cell_min_raw is not None and cell_max_raw is not None:
                delta = max(abs(worst_low - cell_min_raw), abs(worst_high - cell_max_raw))
                if delta >= f['max_delta_v']:
                    if self._cross_check_since is None:
                        self._cross_check_since = now
                    held = now - self._cross_check_since
                    if held >= f['soft_cut_s']:
                        soft_cut = True
                        cross_soft_active = True
                        if held - f['soft_cut_s'] >= f['hard_escalation_s']:
                            hard_cut = True
                            cross_hard_active = True
                            status['cell_data_cross_check'] = (
                                f'MISMATCH {delta * 1000:.0f}mV vs 0x020 pack summary for {held:.0f}s - '
                                f'escalated to HARD cut')
                        else:
                            status['cell_data_cross_check'] = (
                                f'MISMATCH {delta * 1000:.0f}mV vs 0x020 pack summary for {held:.0f}s - soft cut')
                    else:
                        status['cell_data_cross_check'] = (
                            f'mismatch {delta * 1000:.0f}mV present {held:.1f}s/{f["soft_cut_s"]:.0f}s before '
                            f'soft cut latches')
                else:
                    self._cross_check_since = None
                    status['cell_data_cross_check'] = f'ok (delta {delta * 1000:.0f}mV)'
            else:
                self._cross_check_since = None
                status['cell_data_cross_check'] = 'no data to cross-check yet'

            self.fault_log.update(
                'cell_data_mismatch', 'Cell data cross-check mismatch (per-cell vs 0x020 pack summary)',
                'soft', cross_soft_active, status['cell_data_cross_check'])
            self.fault_log.update(
                'cell_data_mismatch_hard', 'Cell data cross-check mismatch - hard cut escalation',
                'hard', cross_hard_active, status['cell_data_cross_check'])

        f = cfg['staleness_watchdog']
        if f['enabled']:
            # Covers EVERY registered input signal (docs/06 section 3, user
            # directive 2026-08-01) - all 96 per-cell voltages, all 16 temp
            # probes, and every fast/slow scalar - not just a hand-picked
            # subset. A key that has never been seen this session (age None)
            # is excluded, same "never seen != stale" principle as before
            # (docs/06 section 0) - only a key that WAS live and then stopped
            # updating counts as stale. Batched into one lock acquisition
            # (SharedState.ages_of) rather than 100+ separate age_of() calls
            # per tick.
            ages_by_key = rz_state.ages_of(rz450e_signals.INPUT_SIGNAL_KEYS)
            worst_age = 0.0
            worst_key = None
            for key, a in ages_by_key.items():
                if a is not None and a > worst_age:
                    worst_age = a
                    worst_key = key
            for ck in ('alive_3f1', 'alive_358', 'counter_5s'):
                a = rz_state.counter_stale_age(ck)
                if a is not None and a > worst_age:
                    worst_age = a
                    worst_key = f'counter:{ck}'
            stale_soft_active = False
            stale_hard_active = False
            if worst_age >= f['soft_cut_s']:
                if self._stale_since is None:
                    self._stale_since = time.monotonic()
                soft_cut = True
                stale_soft_active = True
                # Stale safety-relevant input data must not just soft-cut -
                # it must also stop charging outright (user directive
                # 2026-08-01): we can no longer verify it's safe to keep
                # accepting charge/regen if the data behind that decision is
                # stale, so force an explicit charge-stop here rather than
                # relying on capacity_empty alone. Scoped to THIS feature
                # only (not every soft cut - e.g. a low-voltage soft cut
                # should NOT block charging, which is exactly what a
                # depleted pack needs).
                out['charge_limit_kw'] = 0.0
                out['charger_limit_kw'] = -10.0
                if time.monotonic() - self._stale_since >= f['hard_escalation_s']:
                    hard_cut = True
                    stale_hard_active = True
                    status['staleness_watchdog'] = (
                        f'STALE {worst_age:.0f}s ({worst_key}) - escalated to HARD cut, charging stopped')
                else:
                    status['staleness_watchdog'] = (
                        f'STALE {worst_age:.0f}s ({worst_key}) - soft cut, charging stopped')
            else:
                self._stale_since = None
                status['staleness_watchdog'] = f'ok (worst age {worst_age:.1f}s, {worst_key or "n/a"})'

            self.fault_log.update('staleness_soft', 'Staleness watchdog - soft cut (stale data)', 'soft',
                                  stale_soft_active, status['staleness_watchdog'])
            self.fault_log.update('staleness_hard', 'Staleness watchdog - hard cut escalation', 'hard',
                                  stale_hard_active, status['staleness_watchdog'])

        if soft_cut:
            out['capacity_empty'] = 1
        # Latching (added 2026-08-01, see __init__'s comment): a hard cut
        # THIS tick sets the latch; the latch, once set, keeps asserting the
        # cut on every subsequent tick regardless of whether `hard_cut`
        # itself goes back to False, until notify_session_start()/
        # notify_charge_replug() clears it.
        if hard_cut:
            self._hard_latched = True
        if self._hard_latched:
            out['relay_cut_request'] = 3
            out['interlock'] = 0
            status['hard_cut_latch'] = 'LATCHED - clears on a fresh session start or charger replug'
        # Fault log entry for the LATCH itself (bug fix, 2026-08-01, found by
        # an independent review pass): every individual hard-tier fault_log
        # entry above (low_voltage_emergency, overvoltage_emergency, etc.)
        # is intentionally still keyed on its own INSTANTANEOUS condition -
        # that's genuinely useful ("did this specific trigger recur?") - but
        # none of them reflect that the CUT ITSELF is still latched once its
        # own trigger has recovered. Without this entry, the Fault History
        # window would show every entry "cleared" while the vehicle is
        # still hard-cut - actively misleading, not just incomplete.
        self.fault_log.update('hard_cut_latch', 'Hard cut LATCHED (relay_cut_request/interlock still asserted)',
                              'hard', self._hard_latched, status.get('hard_cut_latch', 'not latched'))

        self.status = status
        return out

    def to_dict(self):
        return self.config

    @classmethod
    def from_dict(cls, d):
        cfg = default_config()
        for feature, values in (d or {}).items():
            if feature in cfg:
                # Only pull in keys the current schema still defines - a saved
                # profile from an older revision may carry fields that have
                # since been removed (e.g. taper_start_pct, removed rev 2)
                # and blindly merging them back in would silently resurrect
                # dead config forever.
                for key, value in values.items():
                    if key in cfg[feature]:
                        cfg[feature][key] = value
        return cls(cfg)
