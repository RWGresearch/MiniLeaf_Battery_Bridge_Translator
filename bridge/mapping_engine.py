"""Signal mapping: RZ450e input signal(s) -> a fixed preset combine function
-> a Leaf mapping-target signal. Fixed preset list only, per docs/04-signal-
mapping.md and docs/05's design-philosophy note - no generic expression
engine, so every tie stays portable to a future STM32 C function.
"""
from bridge import leaf_signals, rz450e_signals

COMBINE_TYPES = ('linear', 'sum', 'average', 'min', 'max', 'lookup', 'soh_percent')

NAMEPLATE_CAPACITY_AH = 201.00   # confirmed 100%-SOH baseline, docs/02 - fallback
# default only as of 2026-08-08 (see VEHICLE_FIELD_BOUNDS/derive_capacity_
# outputs() below): the GIDS formula now reads a live, user-configurable
# state.vehicle['nameplate_capacity_ah'] instead of this constant directly.
# Still the module-level default/fallback, and still what the default
# 'soh_percent' mapping tie (see default_ties() below) bakes into its own
# static `nameplate_ah` param at tie-creation time - that tie's value is
# independently editable via its own Signal Mapping params (pre-existing
# capability, unrelated to this file's own live state.vehicle read), not
# wired to track state.vehicle live. Two independently-tunable "nameplate
# capacity" numbers by design for now (SOH% dash mapping vs. GIDS formula) -
# not unified, since doing so would mean plumbing live state access into
# evaluate_combine()'s pure-function signature, a bigger structural change
# nobody has asked for yet.

# (lo, hi) numeric bounds for state.vehicle's capacity-formula fields (added
# 2026-08-08, docs/16 audit) - same "generous sanity range, not an operating
# threshold" philosophy as management_engine.FEATURE_FIELD_BOUNDS, used by
# both gui/panels.py's VehiclePanel (clamps on every keystroke) and
# config_profile.py's profile-loading path (clamps on every load), so the two
# paths can never silently diverge - same pattern FEATURE_FIELD_BOUNDS itself
# already established for management-feature fields. qc_max_soc_pct moved OUT
# of here (2026-08-08 follow-up, user directive: "the 80% QC needs to be on
# the charge emulation") into leaf_signals.CHARGE_SLIDERS/CHARGE_EMULATION_
# BOUNDS instead - it's charging behavior, not a pack spec.
VEHICLE_FIELD_BOUNDS = {
    'usable_capacity_kwh': (0.0, 200.0),   # generous sanity bound, not a real pack-size limit
    'nameplate_capacity_ah': (0.0, 500.0),   # generous sanity bound, not a real pack-size limit
}


def evaluate_combine(values, combine, params):
    """Returns None (not 0.0) when no input has live/cached data yet, so the
    caller can leave the target at its DEFAULTS/last-known-good value
    instead of snapping to zero (docs/06's known-good-startup requirement)."""
    values = [v for v in values if v is not None]
    if not values:
        return None
    if combine == 'linear':
        return values[0] * params.get('scale', 1.0) + params.get('offset', 0.0)
    if combine == 'sum':
        return sum(values)
    if combine == 'average':
        return sum(values) / len(values)
    if combine == 'min':
        return min(values)
    if combine == 'max':
        return max(values)
    if combine == 'soh_percent':
        nameplate = params.get('nameplate_ah', NAMEPLATE_CAPACITY_AH)
        return (values[0] / nameplate) * 100.0 if nameplate else 0.0
    if combine == 'lookup':
        table = sorted(params.get('table', []), key=lambda p: p[0])
        if not table:
            return 0.0
        v = values[0]
        best = table[0]
        for point in table:
            if point[0] <= v:
                best = point
            else:
                break
        return best[1]
    return 0.0


class MappingTie:
    def __init__(self, inputs, combine, output, params=None, name=None):
        self.inputs = list(inputs)
        self.combine = combine
        self.output = output
        self.params = params or {}
        self.name = name or f"{'+'.join(inputs)} -> {output}"

    def evaluate(self, state):
        values = [state.get_input(k) for k in self.inputs]
        return evaluate_combine(values, self.combine, self.params)

    def to_dict(self):
        return {'inputs': self.inputs, 'combine': self.combine,
                'output': self.output, 'params': self.params, 'name': self.name}

    @classmethod
    def from_dict(cls, d):
        # scale/offset validation (added 2026-08-13, blind-review finding):
        # a hand-edited/corrupted profile.json (or a NaN that slipped past
        # the GUI's own entry validation before this fix) must not be able
        # to plant a non-finite scale/offset - same "drop the bad value,
        # keep the safe default" convention as every other bounds-clamped
        # config category in this project (FEATURE_FIELD_BOUNDS,
        # VEHICLE_FIELD_BOUNDS, CHARGE_EMULATION_BOUNDS). scale/offset are
        # legitimately unbounded (a signal conversion can need any real
        # slope/intercept), so this only rejects NON-FINITE/unparseable
        # values, not out-of-range ones. Other params keys (`table`,
        # `nameplate_ah`, used by the 'lookup'/'soh_percent' combine types)
        # are untouched - no known corruption vector for those yet.
        params = dict(d.get('params', {}))
        for key, default in (('scale', 1.0), ('offset', 0.0)):
            if key in params:
                try:
                    params[key] = leaf_signals.parse_finite_float(params[key])
                except (TypeError, ValueError):
                    params[key] = default
        return cls(d['inputs'], d['combine'], d['output'],
                    params=params, name=d.get('name'))


# ── Sensible starting ties (not safety-relevant, just baseline wiring so the
# app is usable out of the box - editable/removable like any other tie) ────
def default_ties():
    return [
        MappingTie(['pack_v'], 'linear', 'pack_voltage_v', {'scale': 1.0, 'offset': 0.0}),
        MappingTie(['current'], 'linear', 'pack_current_a', {'scale': -1.0, 'offset': 0.0},
                   name='current -> pack_current_a (SIGN INVERTED: RZ450e +discharge -> Leaf +charge)'),
        MappingTie(['soc_pct'], 'linear', 'usable_soc', {'scale': 1.0, 'offset': 0.0}),
        MappingTie(['soc_pct'], 'linear', 'fine_soc_pct', {'scale': 1.0, 'offset': 0.0}),
        # soc_correction (0x59E byte 7) drives the real dash SOC% display -
        # user-confirmed on real hardware 2026-07-31 (own Leaf, own bench
        # RZ450e pack): raw 0-200 = 0-100%, i.e. 2 raw counts per percent.
        # Resolves docs/10-open-questions.md item 10, inherited from
        # Leaf_BMS_Emulator as unsolved there - this project independently
        # derived it via direct real-vehicle testing. See docs/04's
        # mismatch #2 and docs/11's verification checklist.
        MappingTie(['soc_pct'], 'linear', 'soc_correction', {'scale': 2.0, 'offset': 0.0},
                   name='soc_pct -> soc_correction (dash SOC% display, confirmed 2 raw counts/%)'),
        # ChargeBars/CapacityBars raw (0x5BC) - user-confirmed on real
        # hardware 2026-07-31 (own Leaf, own bench RZ450e pack): a plain
        # linear map from capacity_pack1_ah's 0-200 Ah working range to the
        # display's confirmed 0-14 "full bar display" range (docs/03 -
        # 0-14 is the linear scale, 15 is a separate "all segments off"
        # sentinel, not part of it, so 0-200 Ah can never accidentally hit
        # 15). scale = 14/200 = 0.07.
        MappingTie(['capacity_pack1_ah'], 'linear', 'capacity_bars_raw', {'scale': 0.07, 'offset': 0.0},
                   name='capacity_pack1_ah -> capacity_bars_raw (confirmed 0-200Ah = 0-14 bars)'),
        MappingTie(['temp_max'], 'linear', 'batt_temp_c', {'scale': 1.0, 'offset': 0.0},
                   name='temp_max (°C) -> batt_temp_c (°C) (identity - both already °C as of 2026-08-09)'),
        MappingTie(['capacity_pack1_ah'], 'soh_percent', 'soh_pct',
                   {'nameplate_ah': NAMEPLATE_CAPACITY_AH}),
        # temp_segment_pct (0x5BC "Dash temperature segment (%)," docs/03) -
        # added 2026-08-01. UNLIKE soc_correction/capacity_bars_raw above,
        # this is NOT a real-hardware-confirmed formula - no capture exists
        # yet correlating this field against the real dash display. Shipped
        # as a provisional starting point only (so the field has SOME live
        # driver instead of sitting on its static DEFAULTS value forever):
        # linear map of temp_max over a 0-60C window (the pack's cold-block
        # to discharge-hard-stop range, docs/05 - was expressed as 32-140F
        # before the 2026-08-09 Celsius conversion, same physical window) to
        # 0-100%. Treat as unconfirmed - see docs/10-open-questions.md, this
        # project's confirmed-vs-unverified discipline applies here same as
        # anywhere else. Remove/edit/replace freely once a real capture exists.
        MappingTie(['temp_max'], 'linear', 'temp_segment_pct',
                   {'scale': 100.0 / 60.0, 'offset': 0.0},
                   name='temp_max -> temp_segment_pct (PROVISIONAL, not hardware-confirmed - see docs/10)'),
    ]


class MappingEngine:
    def __init__(self, ties=None):
        self.ties = ties if ties is not None else default_ties()

    def apply(self, state):
        out = {}
        for tie in self.ties:
            if not tie.output:
                continue
            value = tie.evaluate(state)
            if value is not None:
                out[tie.output] = value
        return out

    def add(self, tie):
        self.ties.append(tie)

    def remove(self, index):
        if 0 <= index < len(self.ties):
            self.ties.pop(index)

    def to_list(self):
        return [t.to_dict() for t in self.ties]

    @classmethod
    def from_list(cls, items):
        return cls([MappingTie.from_dict(d) for d in items])


# ── Derived multi-signal formulas (docs/04-signal-mapping.md) ─────────────
def derive_capacity_outputs(state):
    """GIDS and QC capacity have no direct RZ450e equivalent - derived from
    USABLE pack capacity (state.vehicle['usable_capacity_kwh'], a real-spec
    default not yet bench-confirmed for this exact pack's buffer - see
    docs/10) scaled by measured SOH, NOT gross nameplate capacity x live pack
    voltage (the old formula, fixed 2026-08-08 per docs/16's audit finding):
    the old approach conflated gross and usable capacity, overstating GIDs by
    ~12.5% for a real pack with a top/bottom reserve buffer (this project's
    bench pack: ~72kWh gross / ~64kWh usable). SOH itself is measured
    capacity_ah / state.vehicle['nameplate_capacity_ah'] (also user-
    configurable, split out of the formula 2026-08-08 same day as a follow-up
    - was the hardcoded NAMEPLATE_CAPACITY_AH module constant, now a live
    per-pack value; see that constant's own comment for how this relates to
    the separate soh_pct mapping tie's own independently-editable nameplate
    figure). Uses whichever pack capacity reading is available (pack1,
    falling back through pack2-4) since all 4 read the same value on a
    healthy pack (docs/02).

    QC ceiling (qc_full_wh/qc_remain_wh) is capped at
    state.charge_emulation['qc_max_soc_pct'] (default 80%, moved here from
    state.vehicle 2026-08-08 - it's charging behavior, not a pack spec) since
    real DC fast charging only usefully charges to roughly that point before
    CC-CV tapering makes the rest pointless - PROVISIONAL, not yet tested
    against a real DC fast-charge session (no DC testing done on this project
    at all yet), same "documented, not confirmed" status as
    temp_segment_pct's own mapping tie (see default_ties() above).
    qc_remain_wh caps at 0 once already past the SOC ceiling (user directive
    2026-08-08) - reads as "how much more DC fast charging usefully gets
    you," not the pack's literal total energy."""
    soc_pct = state.get_input('soc_pct')
    capacity_ah = None
    for key in ('capacity_pack1_ah', 'capacity_pack2_ah', 'capacity_pack3_ah', 'capacity_pack4_ah'):
        capacity_ah = state.get_input(key)
        if capacity_ah:
            break
    if soc_pct is None or not capacity_ah:
        return {}
    nameplate_ah = state.vehicle.get('nameplate_capacity_ah', NAMEPLATE_CAPACITY_AH)
    soh_fraction = capacity_ah / nameplate_ah
    usable_kwh_at_soh = state.vehicle.get('usable_capacity_kwh', 64.0) * soh_fraction
    usable_wh = (soc_pct / 100.0) * usable_kwh_at_soh * 1000.0
    qc_ceiling_wh = usable_kwh_at_soh * 1000.0 * (state.charge_emulation.get('qc_max_soc_pct', 80.0) / 100.0)
    return {
        'gids': usable_wh / 80.0,
        'qc_full_wh': qc_ceiling_wh,
        'qc_remain_wh': max(0.0, qc_ceiling_wh - usable_wh),
    }


def explain_tie(tie, state):
    """For the GUI '?' popup: shows the live input value(s), the conversion
    applied, and the resulting output value. Inputs are already decoded
    physical values (this app doesn't retain raw CAN bytes past decode), so
    this shows physical-in -> conversion -> physical-out, which is what
    confirms the math is doing what the user expects."""
    input_meta = {s['key']: s for s in rz450e_signals.INPUT_SIGNALS}
    lines = []
    values = []
    for key in tie.inputs:
        v = state.get_input(key)
        values.append(v)
        meta = input_meta.get(key, {})
        unit = meta.get('unit', '')
        lines.append(f"{key} = {v!r} {unit}".strip())
    result = evaluate_combine(values, tie.combine, tie.params)
    lines.append(f"combine={tie.combine} params={tie.params}")
    lines.append(f"-> {tie.output} = {result!r}")
    return '\n'.join(lines), result
