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


def default_config():
    """Researched NMC/lithium-ion defaults, cross-checked against this
    pack's confirmed real-world range (docs/05). Shipped ENABLED, not
    blank - per explicit user confirmation, since everything here is
    editable and gets refined against real hardware over time."""
    return {
        'low_voltage_cutoff': {
            'enabled': True, 'min_cell_v': 3.00, 'min_soc_pct': 10.0,
            'emergency_low_v': 2.80,
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
            'taper_start_v': 3.50, 'taper_zero_v': 3.00,
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
            # Proactive regen/charge-acceptance curve (user-specified
            # 2026-07-31): the VCM is slow to respond to a charge_limit_kw
            # change, so the taper must start well before the danger zone,
            # not right at its edge. Full power at/below regen_full_v, zero
            # at/above regen_zero_v, linear between.
            'regen_full_v': 3.90, 'regen_zero_v': 4.10,
            'emergency_high_v': 4.30,
            'daily_target_pct': 80.0, 'extended_target_pct': 100.0,
            'extended_mode': False,
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
            # as low as ~60C (140F); 149F/65C leaves only a few degrees of
            # margin above the 140F soft stop, deliberately thin because
            # there's very little real margin above it in the chemistry.
            'emergency_temp_f': 149.0,
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
            'enabled': True, 'warn_delta_v': 0.05,
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
    }


class ManagementEngine:
    def __init__(self, config=None):
        self.config = config or default_config()
        self.status = {}
        self._stale_since = None
        self._discharge_factor_applied = 1.0
        self._last_apply_time = None
        self._low_v_condition_since = None
        self._overcurrent_since = None
        self._overcurrent_direction = None
        self.fault_log = FaultLog()

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

        cells = [rz_state.get_input(k) for k in rz450e_signals.cell_voltage_keys()]
        cells = [c for c in cells if c is not None]
        worst_low = min(cells) if cells else rz_state.get_input('cell_min')
        worst_high = max(cells) if cells else rz_state.get_input('cell_max')
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
            target = f['extended_target_pct'] if f.get('extended_mode') else f['daily_target_pct']
            # RZ450e's confirmed charging interlock (0x358) - the user's own
            # design intent for this signal ("if not active, a charge request
            # must not proceed", docs/02). None/missing is treated as NOT
            # charging (safe default - never assert a charge-stop we can't
            # actually confirm the context for).
            charging_active = bool(rz_state.get_input('charge_permission_input'))

            # Per-cell taper on charge_limit_kw ("Charge/regen power limit",
            # docs/03) is driven ONLY by individual cell voltage, continuously,
            # regardless of SoC or charging context - charge_limit_kw is the
            # Leaf's shared charge-acceptance ceiling, used by the VCM for
            # regenerative braking while DRIVING just as much as for AC
            # charging, so this always applies.
            #
            # PROACTIVE curve (user-specified 2026-07-31): the VCM is slow to
            # respond to a charge_limit_kw change, so regen must start being
            # backed off well before any cell is actually in danger, not at a
            # narrow margin right on the ceiling - full power at/below
            # regen_full_v (default 3.90V), zero at/above regen_zero_v
            # (default 4.10V), linear between. worst_high is the max of all
            # 96 individually read cell voltages, not a pack-level summary.
            if worst_high is not None and worst_high >= f['emergency_high_v']:
                hard_cut = True
                cell_factor = 0.0
                cell_status = f'EMERGENCY hard cut ({worst_high:.3f}V >= {f["emergency_high_v"]}V)'
            elif worst_high is not None:
                cell_factor = 1.0 - _ramp_factor(worst_high, f['regen_full_v'], f['regen_zero_v'])
                cell_status = (
                    f'proactive regen/charge taper factor={cell_factor:.2f} (worst cell '
                    f'{worst_high:.3f}V - full <= {f["regen_full_v"]}V, zero >= {f["regen_zero_v"]}V)')
            else:
                cell_factor = 1.0
                cell_status = 'no per-cell voltage data yet - full power'
            out['charge_limit_kw'] = out.get('charge_limit_kw', 0.0) * cell_factor

            self.fault_log.update('overvoltage_emergency', 'Cell overvoltage EMERGENCY hard cut (per-cell)',
                                  'hard', worst_high is not None and worst_high >= f['emergency_high_v'], cell_status)

            # charger_limit_kw's per-cell overvoltage taper applies
            # UNCONDITIONALLY, same as charge_limit_kw above (fixed
            # 2026-07-31 alongside docs/06's charger-request ramp feature):
            # this used to only scale charger_limit_kw while `charging_active`
            # (the RZ450e-side 0x358 interlock) was true, which was fine as
            # long as nothing else ever raised charger_limit_kw outside that
            # context - but the new charge-ramp emulation
            # (RealtimeEngine._apply_charge_ramp) can now set it to a high
            # value whenever the LEAF-side 0x1F2 charge request is active,
            # which is a DIFFERENT signal from this RZ450e-side interlock and
            # can plausibly be out of sync with it (e.g. on a bench setup).
            # The per-cell safety taper must be authoritative over
            # charger_limit_kw regardless of which upstream logic set it,
            # exactly like charge_limit_kw already is - never conditionally
            # skippable.
            out['charger_limit_kw'] = out.get('charger_limit_kw', 0.0) * cell_factor

            # full_charge_flag (instant charge-stop + HARD CONTACTOR DROP per
            # docs/03) still only makes sense while actually plugged in and
            # charging - SAFETY FIX (2026-07-31): this used to fire purely
            # from SoC >= target with no charging-context check at all, which
            # meant simply DRIVING above the target SoC (e.g. having charged
            # to 85% overnight, then driving) would assert full_charge_flag
            # and drop the real Leaf's main HV contactors mid-drive. Gated on
            # the charging interlock so it can only fire during an actual
            # charge session.
            if charging_active:
                if soc is not None and soc >= target:
                    out['full_charge_flag'] = 1
                    out['charge_limit_kw'] = 0.0
                    out['charger_limit_kw'] = -10.0
                    cell_status += f' | AC charge target {target:.0f}% reached - full_charge_flag set'
                status['charge_target_taper'] = cell_status
            else:
                status['charge_target_taper'] = (
                    cell_status + ' | not actively charging per RZ450e interlock (regen/charger-ramp '
                    'ceiling still active; AC target/full_charge_flag gated off)')

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

        f = cfg['staleness_watchdog']
        if f['enabled']:
            ages = []
            for key in ('pack_v', 'current', 'cell_min', 'cell_max', 'soc_pct'):
                a = rz_state.age_of(key)
                if a is not None:
                    ages.append(a)
            for ck in ('alive_3f1', 'alive_358', 'counter_5s'):
                a = rz_state.counter_stale_age(ck)
                if a is not None:
                    ages.append(a)
            worst_age = max(ages) if ages else 0.0
            stale_soft_active = False
            stale_hard_active = False
            if worst_age >= f['soft_cut_s']:
                if self._stale_since is None:
                    self._stale_since = time.monotonic()
                soft_cut = True
                stale_soft_active = True
                if time.monotonic() - self._stale_since >= f['hard_escalation_s']:
                    hard_cut = True
                    stale_hard_active = True
                    status['staleness_watchdog'] = f'STALE {worst_age:.0f}s - escalated to HARD cut'
                else:
                    status['staleness_watchdog'] = f'STALE {worst_age:.0f}s - soft cut'
            else:
                self._stale_since = None
                status['staleness_watchdog'] = f'ok (worst age {worst_age:.1f}s)'

            self.fault_log.update('staleness_soft', 'Staleness watchdog - soft cut (stale data)', 'soft',
                                  stale_soft_active, status['staleness_watchdog'])
            self.fault_log.update('staleness_hard', 'Staleness watchdog - hard cut escalation', 'hard',
                                  stale_hard_active, status['staleness_watchdog'])

        if soft_cut:
            out['capacity_empty'] = 1
        if hard_cut:
            out['relay_cut_request'] = 3
            out['interlock'] = 0

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
