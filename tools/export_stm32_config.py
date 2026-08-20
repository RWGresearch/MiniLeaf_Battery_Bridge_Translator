"""Offline STM32 config codegen (docs/09-stm32-export-format.md, phase 2 milestone 1).

Loads a saved config/profile.json through the SAME validated path the GUI uses
(bridge/config_profile.py's apply_profile() -> bounds-clamping via
FEATURE_FIELD_BOUNDS/CHARGE_EMULATION_BOUNDS/VEHICLE_FIELD_BOUNDS/
ENGINE_TIMING_BOUNDS, plus the existing key-rename/unit migrations), then
emits a generated C header of compile-time constants for the STM32 firmware
in STM32_MiniLeaf_Bridge_Translator_uVision/.

This is a ONE-TIME, COMPILE-TIME step. There is no on-device JSON parsing and
no runtime config storage in the firmware - whatever this script bakes into
the generated header is what ships until the next recompile+reflash. Never
edit the generated header by hand; edit config/profile.json (or its defaults
in bridge/) and re-run this script instead.

Usage:
    py tools/export_stm32_config.py
    py tools/export_stm32_config.py --profile config/some-other-profile.json
    py tools/export_stm32_config.py --check      (dry run - print what would
                                                    change, write nothing)

"--check" is the "configurator that can pull from the .json and populate the
settings" the user asked for (2026-08-18) - the same load/apply/emit path,
just without writing the output file, so it doubles as a diff tool for
reviewing what a profile change would actually do to the firmware config
before committing to a reflash.
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge import config_profile, leaf_signals, management_engine, mapping_engine, rz450e_signals, state as state_mod

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PROFILE_PATH = config_profile.DEFAULT_PROFILE_PATH
_STM32_SRC = os.path.join(REPO_ROOT, 'STM32_MiniLeaf_Bridge_Translator_uVision', 'Software',
                           'CANBRIDGE-2port', 'source')
DEFAULT_OUTPUT_PATH = os.path.join(_STM32_SRC, 'Inc', 'bridge_config_gen.h')

# Firmware-only defaults with no config/profile.json / GUI equivalent - the
# Python app never needs these (it always runs on a PC, no sleep concept).
# Deliberately NOT added to leaf_signals.ENGINE_TIMING_FIELDS: that list is
# consumed generically by gui/panels.py's EngineTimingPanel, which hardcodes
# ENGINE_TIMING_FIELDS[:4]/[4:] into two labeled boxes ("DID polling" /
# "Wind-down / charge-session detection") - appending a field here would
# either land it in a group it doesn't semantically belong to, or require
# touching the live GUI for a value that has zero effect in the Python app.
# See docs/09-stm32-export-format.md's "Phase 2 status" section.
STM32_ONLY_ENGINE_TIMING_DEFAULTS = {
    'sleep_idle_timeout_s': (
        60.0, 'Idle timeout before entering CAN-bus-silence sleep (s) - STM32 firmware only'),
}

# Fixed-name RZ450e input signals with a direct RzState field of the same
# name (rz450e_ingest.h) - everything else is the cell_NN/temp_NN array
# convention handled by _rz_field_expr() below. Kept as an explicit set (not
# derived from RzState's own layout, which this script has no access to) so
# an unrecognized key fails codegen loudly instead of guessing.
_RZ_FIXED_FIELDS = {
    'pack_v', 'cell_min', 'cell_max', 'current', 'current_b', 'temp_max', 'temp_min',
    'charge_permission_input', 'soc_pct',
    'capacity_pack1_ah', 'capacity_pack2_ah', 'capacity_pack3_ah', 'capacity_pack4_ah',
    'primary_pack_v', 'primary_current_a',
}
_LEAF_OUTPUT_KEYS = {s['key'] for s in leaf_signals.OUTPUT_SIGNALS}


def _c_ident(key):
    """profile.json keys are already snake_case Python identifiers, which are
    also valid C identifiers - this just upper-cases for the #define
    convention and is a single choke point if that ever stops being true."""
    return key.upper()


def _c_float(value):
    return f"{float(value)}f"


def _c_bool(value):
    return '1' if value else '0'


def _c_string(value):
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


class Warnings:
    """Collects every place the loaded profile disagreed with what actually
    got baked into the header (out-of-bounds clamp, missing field falling
    back to a compiled-in default, unrecognized key dropped) - the load path
    itself (apply_profile/ManagementEngine.from_dict) silently clamps/drops,
    matching this project's established convention, but silently is not
    good enough for something that gets flashed once and trusted afterward.
    Every entry here should be reviewed before flashing."""

    def __init__(self):
        self.items = []

    def add(self, msg):
        self.items.append(msg)

    def report(self, out=sys.stderr):
        if not self.items:
            print("No clamping/defaulting/migration differences found.", file=out)
            return
        print(f"{len(self.items)} value(s) differ from the raw profile.json - review before flashing:",
              file=out)
        for msg in self.items:
            print(f"  - {msg}", file=out)


def _diff_numeric_section(warnings, section_name, raw, effective, bounds):
    """Compares a raw profile.json section dict against the same section
    after apply_profile()'s clamp, for every key clamp bounds are known for.
    Only reports genuinely different values (clamped, or a NaN/garbage input
    that got dropped in favor of the existing default) - a missing key that
    was never in the raw file to begin with is expected/normal, not warned
    about (every field always has a compiled-in default from state.py)."""
    for key, value in (raw or {}).items():
        if key not in bounds:
            continue
        eff = effective.get(key)
        try:
            if eff is not None and abs(float(value) - float(eff)) > 1e-9:
                warnings.add(f"{section_name}.{key}: profile.json had {value!r}, "
                             f"using clamped/validated {eff!r}")
        except (TypeError, ValueError):
            warnings.add(f"{section_name}.{key}: profile.json had unparseable {value!r}, "
                          f"using existing default {eff!r}")


def load_and_validate(profile_path, warnings):
    """Runs the exact same load path main.py/gui/app.py uses at startup, so
    the generated header can never drift from what the live GUI app would
    actually run with the same profile.json."""
    raw_profile = config_profile.load_profile(profile_path) or {}
    state = state_mod.SharedState()
    mapping = None
    mgmt = None
    try:
        mapping, mgmt = config_profile.apply_profile(raw_profile, state)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
        print(f"ERROR: failed to load/apply {profile_path}: {exc}", file=sys.stderr)
        raise

    _diff_numeric_section(warnings, 'vehicle', raw_profile.get('vehicle', {}),
                           state.vehicle, mapping_engine.VEHICLE_FIELD_BOUNDS)
    _diff_numeric_section(warnings, 'charge_emulation', raw_profile.get('charge_emulation', {}),
                           state.charge_emulation, leaf_signals.CHARGE_EMULATION_BOUNDS)
    _diff_numeric_section(warnings, 'engine_timing', raw_profile.get('engine_timing', {}),
                           state.engine_timing, leaf_signals.ENGINE_TIMING_BOUNDS)
    raw_features = raw_profile.get('management_features', {})
    eff_features = mgmt.to_dict()
    for feature, raw_values in raw_features.items():
        if feature not in eff_features:
            warnings.add(f"management_features.{feature}: not a recognized feature, ignored")
            continue
        feature_bounds = {k: v for (f, k), v in management_engine.FEATURE_FIELD_BOUNDS.items() if f == feature}
        _diff_numeric_section(warnings, f'management_features.{feature}', raw_values,
                               eff_features[feature], feature_bounds)

    # Cross-field ordering sanity (e.g. an emergency threshold typed less
    # extreme than its own soft tier) - bridge/management_engine.py's
    # ManagementEngine.apply() re-checks this every tick at RUNTIME and
    # surfaces it as a live fault_log warning, since the Python app's config
    # can be edited live. Firmware config is compile-time-only (baked into
    # bridge_config_gen.h once, never re-checked on the MCU), so this is the
    # one point where a violation can still be caught - before flashing.
    sanity_violations = management_engine._check_config_sanity(mgmt.to_dict(), state.charge_emulation)
    for violation in sanity_violations:
        warnings.add(f"config sanity: {violation}")

    return state, mapping, mgmt, raw_profile.get('profile_name', 'profile')


def _emit_section(lines, title):
    lines.append('')
    lines.append(f'/* ---- {title} ---- */')


def _build_mapping_model(mapping):
    """Returns (ties, lookup_tables). lookup_tables is [(tie_index, c_var_name,
    sorted_table_rows), ...] for every 'lookup'-combine tie."""
    ties = mapping.to_list()
    lookup_tables = []
    for i, tie in enumerate(ties):
        if tie['combine'] == 'lookup':
            # Sorted by x ascending, matching mapping_engine.py's
            # evaluate_combine() which re-sorts on every call - the C port's
            # lookup walk assumes ascending order and does NOT re-sort at
            # runtime, so this must be pre-sorted here instead.
            table = sorted(tie['params'].get('table', []), key=lambda p: p[0])
            lookup_tables.append((i, f'BRIDGE_CFG_LOOKUP_TABLE_{i}', table))
    return ties, lookup_tables


def _rz_field_expr(key):
    """Resolves an RZ450e INPUT_SIGNALS key (bridge/rz450e_signals.py) to the
    C expression reaching its RzSignal in g_rz_state (rz450e_ingest.h)."""
    if key not in rz450e_signals.INPUT_SIGNAL_KEYS:
        raise ValueError(f'unrecognized RZ450e input signal key in a mapping tie: {key!r}')
    if key in _RZ_FIXED_FIELDS:
        return f'g_rz_state.{key}'
    if key.startswith('cell_'):
        n = int(key[5:])
        if 1 <= n <= 96:
            return f'g_rz_state.cell[{n - 1}]'
    if key.startswith('temp_'):
        n = int(key[5:])
        if 1 <= n <= 16:
            return f'g_rz_state.temp[{n - 1}]'
    raise ValueError(f'unrecognized RZ450e input signal key in a mapping tie: {key!r}')


def _leaf_output_field(key):
    """Resolves a Leaf mapping-target key (bridge/leaf_signals.py's
    OUTPUT_SIGNALS) to the LeafState field it writes (leaf_output.h) -
    matches MANAGEMENT_EXCLUSIVE_KEYS exclusion (gids/qc_*/etc. can never be
    a mapping tie's output, same rule the GUI's own dropdown enforces)."""
    if key not in _LEAF_OUTPUT_KEYS:
        raise ValueError(f'unrecognized/non-mappable Leaf output key in a mapping tie: {key!r}')
    return f'out->{key}'


def _emit_lookup_tables(lines, lookup_tables):
    """Piecewise-linear tables + the shared eval helper for any 'lookup'-
    combine tie - genuine runtime-indexed data (the input value picks a row
    at runtime), unlike every other combine type, so this stays a real array
    + loop rather than being unrolled into per-tie code. `static`, only
    emitted/linked if at least one tie actually uses 'lookup' - the current
    default profile has none, so this whole block currently compiles to
    nothing (see bridge_config_apply_mapping_ties()'s own comment for the
    general "unused static costs nothing per translation unit" pattern this
    whole file leans on)."""
    if not lookup_tables:
        return
    lines.append('/* Pre-sorted ascending by x at codegen time - mapping_engine.py\'s')
    lines.append(' * evaluate_combine() re-sorts on every call; this port does not re-sort at')
    lines.append(' * runtime, so codegen must pre-sort instead. */')
    for i, var, table in lookup_tables:
        rows = ', '.join(f'{{{_c_float(x)}, {_c_float(y)}}}' for x, y in table)
        lines.append(f'static const float {var}[{len(table)}][2] = {{ {rows} }};')
    lines.append('')
    lines.append('static float bridge_mapping_lookup_eval(const float table[][2], uint8_t count, float x)')
    lines.append('{')
    lines.append('    float best_y = table[0][1];')
    lines.append('    for (uint8_t k = 0; k < count; k++)')
    lines.append('    {')
    lines.append('        if (table[k][0] <= x) { best_y = table[k][1]; }')
    lines.append('        else { break; }')
    lines.append('    }')
    lines.append('    return best_y;')
    lines.append('}')
    lines.append('')


def _emit_tie_statement(lines, tie, idx, lookup_by_idx):
    """Emits the C statement(s) for ONE mapping tie directly - no runtime
    struct table, no string-keyed field lookup, no combine-type dispatch.
    Every tie's input(s)/output/combine-function/constants are already fully
    known at codegen time (config is compile-time-only in this firmware), so
    there is no reason to pay for a generic interpreter to re-derive that at
    runtime on every tick. Replaces the `bridge_mapping_tie_t` struct-array +
    mapping_engine.c's old rz_lookup()/leaf_output_lookup() string-matching
    (removed 2026-08-18, chasing a Keil link-size overage - the old
    interpreter cost ~35 strcmp/strncmp call sites plus a 7-case switch to
    run what a real profile is usually just a handful of single-input
    `linear`/`soh_percent` formulas)."""
    safe_name = str(tie.get('name', '')).replace('*/', '* /')
    lines.append(f'    /* {safe_name} */')
    output_expr = _leaf_output_field(tie['output'])
    input_exprs = [_rz_field_expr(k) for k in tie['inputs']]
    combine = tie['combine']
    params = tie.get('params', {})

    if combine in ('linear', 'soh_percent', 'lookup'):
        # First-LIVE-input-in-declared-order semantics, matching
        # evaluate_combine()'s `values = [v for v in values if v is not
        # None]; ...; values[0]` - almost always a single-input tie in
        # practice, but a multi-input 'linear'/'soh_percent'/'lookup' tie is
        # legal config and must fall through to the next input the same way.
        if combine == 'linear':
            scale = _c_float(params.get('scale', 1.0))
            offset = _c_float(params.get('offset', 0.0))

            def formula(expr):
                return f'{expr}.value * {scale} + {offset}'
        elif combine == 'soh_percent':
            nameplate_ah = params.get('nameplate_ah', mapping_engine.NAMEPLATE_CAPACITY_AH)
            nameplate_c = _c_float(nameplate_ah)
            if float(nameplate_ah) == 0.0:
                def formula(expr):
                    return '0.0f'
            else:
                def formula(expr):
                    return f'({expr}.value / {nameplate_c}) * 100.0f'
        else:  # lookup
            var, table = lookup_by_idx[idx]

            def formula(expr, _var=var, _count=len(table)):
                return f'bridge_mapping_lookup_eval({_var}, {_count}, {expr}.value)'

        for i, expr in enumerate(input_exprs):
            kw = 'if' if i == 0 else 'else if'
            lines.append(f'    {kw} ({expr}.last_update_tick != 0) {{ {output_expr} = {formula(expr)}; }}')
    elif combine in ('sum', 'average'):
        lines.append('    {')
        lines.append('        float acc = 0.0f; uint8_t n = 0;')
        for expr in input_exprs:
            lines.append(f'        if ({expr}.last_update_tick != 0) {{ acc += {expr}.value; n++; }}')
        if combine == 'sum':
            lines.append(f'        if (n > 0) {{ {output_expr} = acc; }}')
        else:
            lines.append(f'        if (n > 0) {{ {output_expr} = acc / (float)n; }}')
        lines.append('    }')
    elif combine in ('min', 'max'):
        cmp_op = '<' if combine == 'min' else '>'
        lines.append('    {')
        lines.append('        float best = 0.0f; uint8_t have = 0;')
        for expr in input_exprs:
            lines.append(f'        if ({expr}.last_update_tick != 0) {{ float v = {expr}.value; '
                         f'if (!have || v {cmp_op} best) {{ best = v; }} have = 1; }}')
        lines.append(f'        if (have) {{ {output_expr} = best; }}')
        lines.append('    }')
    else:
        raise ValueError(f'unrecognized combine type in a mapping tie: {combine!r}')


def _emit_mapping_apply_function(lines, ties, lookup_tables):
    lookup_by_idx = {i: (var, table) for i, var, table in lookup_tables}
    lines.append('/* Applies every configured mapping tie - see _emit_tie_statement() in')
    lines.append(' * tools/export_stm32_config.py for why this is generated straight-line C')
    lines.append(' * instead of a runtime-interpreted table. `static` and only ever called')
    lines.append(' * from mapping_engine.c - every other firmware file includes this header')
    lines.append(' * too but never calls this function, so it costs them nothing (an')
    lines.append(' * unreferenced `static` function is eliminated per translation unit). `inline`')
    lines.append(' * is what it is here for, not for the optimizer: Clang/GCC do not emit')
    lines.append(' * -Wunused-function for an unused `static inline` (unlike a plain `static`,')
    lines.append(' * which warns in every one of the 5 TUs that never call it) - silences the')
    lines.append(' * warning with zero behavior/size change, since a header-defined helper not')
    lines.append(' * called by every including file is exactly what `inline` signals. */')
    lines.append('static inline void bridge_config_apply_mapping_ties(LeafState *out)')
    lines.append('{')
    for i, tie in enumerate(ties):
        _emit_tie_statement(lines, tie, i, lookup_by_idx)
    lines.append('}')


def generate_header(state, mapping, mgmt, profile_name, ties, lookup_tables):
    lines = []
    lines.append('/* AUTO-GENERATED by tools/export_stm32_config.py - DO NOT EDIT BY HAND.')
    lines.append(f' * Source profile: {profile_name} (config/profile.json)')
    lines.append(f' * Generated: {datetime.datetime.now().isoformat(timespec="seconds")}')
    lines.append(' * Re-run the script after editing config/profile.json; this file is a')
    lines.append(' * compile-time snapshot only - the firmware never reads profile.json or')
    lines.append(' * any other runtime config source. See docs/09-stm32-export-format.md.')
    lines.append(' */')
    lines.append('#ifndef BRIDGE_CONFIG_GEN_H')
    lines.append('#define BRIDGE_CONFIG_GEN_H')
    lines.append('')
    lines.append('#include <stdint.h>')
    lines.append('#include "leaf_output.h"     /* LeafState - bridge_config_apply_mapping_ties() below */')
    lines.append('#include "rz450e_ingest.h"   /* RzState/g_rz_state - bridge_config_apply_mapping_ties() below */')

    _emit_section(lines, 'Vehicle / pack spec')
    lines.append(f'#define BRIDGE_CFG_PROFILE_NAME {_c_string(profile_name)}')
    lines.append(f'#define BRIDGE_CFG_CAR_GEN {_c_string(state.vehicle["car_gen"])}')
    lines.append(f'#define BRIDGE_CFG_CAR_GEN_IS_ZE1 {_c_bool(state.vehicle["car_gen"] == "ZE1")}')
    lines.append(f'#define BRIDGE_CFG_BATTERY_GEN {_c_string(state.vehicle["battery_gen"])}')
    lines.append(f'#define BRIDGE_CFG_BATTERY_GEN_IS_ZE1 {_c_bool(state.vehicle["battery_gen"] == "ZE1")}')
    lines.append(f'#define BRIDGE_CFG_BATTERY_KWH {int(state.vehicle["battery_kwh"])}')
    lines.append(f'#define BRIDGE_CFG_BATTERY_IS_62KWH {_c_bool(int(state.vehicle["battery_kwh"]) == 62)}')
    lines.append(f'#define BRIDGE_CFG_USABLE_CAPACITY_KWH {_c_float(state.vehicle["usable_capacity_kwh"])}')
    lines.append(f'#define BRIDGE_CFG_NAMEPLATE_CAPACITY_AH {_c_float(state.vehicle["nameplate_capacity_ah"])}')

    _emit_section(lines, 'Generated/opaque Leaf-signal send flags (docs/03)')
    for key, label, _default in leaf_signals.GENERATED_SIGNALS:
        lines.append(f'#define BRIDGE_CFG_SEND_{_c_ident(key)} {_c_bool(state.generated_enabled[key])} '
                      f'/* {label} */')

    _emit_section(lines, 'Charge emulation (charger-ramp + AC taper + AC temp derate)')
    for key, label, _default in leaf_signals.CHARGE_CHECKS:
        lines.append(f'#define BRIDGE_CFG_CE_{_c_ident(key)} {_c_bool(state.charge_emulation[key])} '
                      f'/* {label} */')
    for key, label, _lo, _hi, _step, _default in leaf_signals.CHARGE_SLIDERS:
        lines.append(f'#define BRIDGE_CFG_CE_{_c_ident(key)} {_c_float(state.charge_emulation[key])} '
                      f'/* {label} */')

    _emit_section(lines, 'Engine timing (DID polling cadence, wind-down/charge heuristics)')
    for key, label, _lo, _hi, _step, _default in leaf_signals.ENGINE_TIMING_FIELDS:
        lines.append(f'#define BRIDGE_CFG_ET_{_c_ident(key)} {_c_float(state.engine_timing[key])} '
                      f'/* {label} */')
    lines.append('/* Firmware-only, not part of config/profile.json - see STM32_ONLY_ENGINE_TIMING_DEFAULTS')
    lines.append(' * in tools/export_stm32_config.py for why. */')
    for key, (default, label) in STM32_ONLY_ENGINE_TIMING_DEFAULTS.items():
        lines.append(f'#define BRIDGE_CFG_ET_{_c_ident(key)} {_c_float(default)} /* {label} */')

    _emit_section(lines, 'Management/safety features (docs/05) - thresholds only, algorithm shapes hardcoded')
    features = mgmt.to_dict()
    for feature in management_engine.default_config().keys():  # stable, documented order
        values = features.get(feature, {})
        lines.append(f'/* -- {feature} -- */')
        lines.append(f'#define BRIDGE_CFG_MF_{_c_ident(feature)}_ENABLED {_c_bool(values.get("enabled", False))}')
        for key, value in values.items():
            if key == 'enabled':
                continue
            if isinstance(value, bool):
                lines.append(f'#define BRIDGE_CFG_MF_{_c_ident(feature)}_{_c_ident(key)} {_c_bool(value)}')
            else:
                lines.append(f'#define BRIDGE_CFG_MF_{_c_ident(feature)}_{_c_ident(key)} {_c_float(value)}')

    _emit_section(lines, 'Signal mapping ties (bridge/mapping_engine.py) - codegen-emitted directly, no runtime interpreter')
    _emit_lookup_tables(lines, lookup_tables)
    _emit_mapping_apply_function(lines, ties, lookup_tables)

    _emit_protocol_constants(lines)

    lines.append('')
    lines.append('#endif /* BRIDGE_CONFIG_GEN_H */')
    lines.append('')
    return '\n'.join(lines)


def _emit_protocol_constants(lines):
    """Real-Leaf protocol constants and the opaque generated-signal replay
    tables (bridge/leaf_signals.py's TX_PERIOD_MS, T_*_START/PWRDOWN_*_MS
    timing, DEFAULTS/RANGES, CODE_1DC/CHG_TIME_5BC/HIST5C0/SEQ_5EB) - these
    are bit-verified against real Leaf captures, NOT user-configurable
    (unlike everything above), but still generated here rather than hand-
    transcribed into leaf_output.c: SEQ_5EB alone is 45 rows x 8 bytes, and
    "byte-verified against real captures, ported verbatim" (leaf_signals.
    py's own module docstring) is exactly the kind of correctness
    requirement a manual transcription risks violating. Reading these
    directly from the same Python source main.py itself uses guarantees
    byte-for-byte fidelity with zero transcription risk."""
    _emit_section(lines, 'Real-Leaf protocol timing (bit-verified, not user-configurable)')
    for arb_id, period_ms in leaf_signals.TX_PERIOD_MS.items():
        lines.append(f'#define BRIDGE_PROTO_TX_PERIOD_MS_{arb_id:X} {period_ms}')
    for name in ('T_1DB_START', 'T_55B_START', 'T_59E_START', 'T_PH_B', 'T_PH_C', 'T_VALID', 'T_RUNNING'):
        lines.append(f'#define BRIDGE_PROTO_{name} {getattr(leaf_signals, name)}')
    for name in ('PWRDOWN_STAGE2_MS', 'PWRDOWN_STAGE3_MS', 'PWRDOWN_STAGE4_MS'):
        lines.append(f'#define BRIDGE_PROTO_{name} {getattr(leaf_signals, name)}')
    lines.append(f'#define BRIDGE_PROTO_PWRDOWN_DEFAULT_COOLDOWN_S {_c_float(leaf_signals.PWRDOWN_DEFAULT_COOLDOWN_S)}')
    lines.append(f'#define BRIDGE_PROTO_CHG_CMD_IDLE {leaf_signals.CHG_CMD_IDLE}')
    ign_ids = sorted(leaf_signals.IGNITION_IDS)
    lines.append(f'#define BRIDGE_PROTO_IGNITION_ID_COUNT {len(ign_ids)}')
    lines.append('static const uint16_t BRIDGE_PROTO_IGNITION_IDS[BRIDGE_PROTO_IGNITION_ID_COUNT] = '
                  f'{{ {", ".join(f"0x{i:X}u" for i in ign_ids)} }};')

    _emit_section(lines, 'Leaf output DEFAULTS/RANGES (bridge/leaf_signals.py)')
    all_fields = []  # (key, default, lo, hi)
    for group in leaf_signals.SLIDERS.values():
        for key, _label, lo, hi, _step, default in group:
            all_fields.append((key, default, lo, hi))
    for key, _label, default in leaf_signals.CHECKS:
        all_fields.append((key, default, 0, 1))
    for key, _label, _lo, _hi, _step, default in leaf_signals.ZE1_62_SLIDERS:
        lo, hi = leaf_signals.RANGES[key]
        all_fields.append((key, default, lo, hi))
    for key, default, lo, hi in all_fields:
        lines.append(f'#define BRIDGE_PROTO_DEFAULT_{_c_ident(key)} {_c_float(default)}')
        lines.append(f'#define BRIDGE_PROTO_RANGE_{_c_ident(key)}_LO {_c_float(lo)}')
        lines.append(f'#define BRIDGE_PROTO_RANGE_{_c_ident(key)}_HI {_c_float(hi)}')

    _emit_section(lines, 'Opaque generated-signal replay tables (bridge/leaf_signals.py)')
    lines.append(f'#define BRIDGE_PROTO_CODE_1DC_COUNT {len(leaf_signals.CODE_1DC)}')
    lines.append('static const uint8_t BRIDGE_PROTO_CODE_1DC[BRIDGE_PROTO_CODE_1DC_COUNT][3] = {')
    for row in leaf_signals.CODE_1DC:
        lines.append('    { ' + ', '.join(f'0x{b:02X}u' for b in row) + ' },')
    lines.append('};')

    lines.append(f'#define BRIDGE_PROTO_CHG_TIME_5BC_COUNT {len(leaf_signals.CHG_TIME_5BC)}')
    lines.append('static const uint8_t BRIDGE_PROTO_CHG_TIME_5BC[BRIDGE_PROTO_CHG_TIME_5BC_COUNT][3] = {')
    for row in leaf_signals.CHG_TIME_5BC:
        lines.append('    { ' + ', '.join(f'0x{b:02X}u' for b in row) + ' },')
    lines.append('};')

    hist_keys = sorted(leaf_signals.HIST5C0.keys())
    lines.append(f'#define BRIDGE_PROTO_HIST5C0_COUNT {len(hist_keys)}')
    lines.append('/* indexed [mux-1] - mux is 1..6, matching bridge/leaf_signals.py\'s HIST5C0 dict keys */')
    lines.append('static const uint8_t BRIDGE_PROTO_HIST5C0[BRIDGE_PROTO_HIST5C0_COUNT][3] = {')
    for k in hist_keys:
        row = leaf_signals.HIST5C0[k]
        lines.append('    { ' + ', '.join(f'0x{b:02X}u' for b in row) + ' },')
    lines.append('};')

    lines.append(f'#define BRIDGE_PROTO_SEQ_5EB_COUNT {len(leaf_signals.SEQ_5EB)}')
    lines.append('static const uint8_t BRIDGE_PROTO_SEQ_5EB[BRIDGE_PROTO_SEQ_5EB_COUNT][8] = {')
    for row in leaf_signals.SEQ_5EB:
        lines.append('    { ' + ', '.join(f'0x{b:02X}u' for b in row) + ' },')
    lines.append('};')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default=DEFAULT_PROFILE_PATH,
                         help='Path to the profile.json to export (default: config/profile.json)')
    parser.add_argument('--output', default=DEFAULT_OUTPUT_PATH,
                         help='Path to write the generated header (default: the STM32 project Inc/)')
    parser.add_argument('--check', action='store_true',
                         help='Dry run: load/validate and print warnings, write nothing')
    args = parser.parse_args()

    if not os.path.exists(args.profile):
        print(f"ERROR: profile not found: {args.profile}", file=sys.stderr)
        sys.exit(1)

    warnings = Warnings()
    state, mapping, mgmt, profile_name = load_and_validate(args.profile, warnings)
    warnings.report()

    ties, lookup_tables = _build_mapping_model(mapping)
    header = generate_header(state, mapping, mgmt, profile_name, ties, lookup_tables)

    if args.check:
        print(f"\n--check: would write {len(header.splitlines())} lines to {args.output}")
        return

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8', newline='\n') as f:
        f.write(header)
    print(f"Wrote {args.output}")


if __name__ == '__main__':
    main()
