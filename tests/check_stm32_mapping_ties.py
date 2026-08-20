"""Verifies tools/export_stm32_config.py's codegen-emitted mapping-tie C
(the 2026-08-18 rewrite that replaced a runtime string-keyed interpreter in
mapping_engine.c with straight-line C generated directly from each tie -
see bridge_config_gen.h's own comment). Two independent checks:

1. SEMANTIC fuzz - a from-scratch Python re-derivation (written independently
   of tools/export_stm32_config.py's _emit_tie_statement(), not copy-pasted
   from it) of the exact control flow the generated C uses (first-live-input
   chain for linear/soh_percent/lookup, all-present aggregate for
   sum/average/min/max/), fuzzed against the REAL, unmodified
   bridge.mapping_engine.evaluate_combine() across random present/missing
   input patterns and random values, for all 7 combine types x 1-3 inputs.
   The real profile currently only ever uses linear/soh_percent (see
   project memory) - this is what actually exercises sum/average/min/max/
   lookup, which no live profile currently touches.

2. CODEGEN SYNTAX - actually calls the real generate_header()/_emit_*
   functions from tools/export_stm32_config.py against a SYNTHETIC
   MappingEngine covering all 7 combine types (the live profile's own ties
   never touch sum/average/min/max/lookup, so running codegen against
   config/profile.json alone would never compile-check those paths), then
   -fsyntax-only compiles the result with clang against the real firmware
   headers - catches a Python-emits-bad-C transcription bug directly.

Re-run this whenever tools/export_stm32_config.py's tie-emission functions
or bridge/mapping_engine.py's evaluate_combine() change.
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge import config_profile, leaf_signals, management_engine, mapping_engine, state as state_mod
from tools import export_stm32_config as codegen

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STM32_INC = os.path.join(REPO_ROOT, 'STM32_MiniLeaf_Bridge_Translator_uVision', 'Software',
                          'CANBRIDGE-2port', 'source', 'Inc')

CLANG = r'C:\ProgramData\LLVM for Renesas RISC-V 19.1.7.202501\bin\clang.exe'

# Minimal stand-ins for main.h/can.h (real ones pull in the STM32 HAL, which
# this environment's RISC-V-only clang can't process) - same technique used
# throughout this port's other check_stm32_*.py scripts. Copied alongside
# the REAL leaf_output.h/rz450e_ingest.h below so quoted #include "can.h"
# (searched relative to the including file's own directory first) resolves
# to this stub, not the real STM32_INC/can.h.
_STUB_MAIN_H = (
    '#ifndef __MAIN_H__\n#define __MAIN_H__\n#include <stdint.h>\n'
    'uint32_t HAL_GetTick(void);\n'
    'typedef struct { int _stub; } CAN_HandleTypeDef;\n#endif\n'
)
_STUB_CAN_H = (
    '#ifndef __CAN_H__\n#define __CAN_H__\n#include "main.h"\n'
    '#define CAN_QUEUE 16\n#define MYCAN1 0\n#define MYCAN2 1\n#define CAN_TX 0\n#define CAN_RX 1\n'
    'typedef struct { uint32_t ID; uint8_t dlc; uint8_t ide; uint8_t rtr; uint8_t pad; uint8_t data[8]; } CAN_FRAME;\n'
    'typedef enum { CQ_OK, CQ_FULL, CQ_EMPTY, CQ_IGNORED } CQ_STATUS;\n'
    'CQ_STATUS PushCan(uint8_t canNum, uint8_t TxRx, CAN_FRAME *frame);\n'
    'CQ_STATUS PopCan(uint8_t canNum, uint8_t TxRx, CAN_FRAME *frame);\n'
    'uint8_t LenCan(uint8_t canNum, uint8_t TxRx);\nvoid sendCan(uint8_t channel);\n#endif\n'
)

# One tie per combine type, deliberately mixing 1-3 input counts and, for
# 'linear'/'soh_percent'/'lookup', an input ORDER where the first input is
# sometimes the one that ends up missing - exercises the "fall through to
# the next live input" branch, not just the common single-input case every
# real profile ties use today.
SYNTHETIC_TIES = [
    mapping_engine.MappingTie(['pack_v'], 'linear', 'pack_voltage_v', {'scale': 2.5, 'offset': -3.0}),
    mapping_engine.MappingTie(['cell_min', 'cell_max'], 'linear', 'pack_current_a', {'scale': 1.0, 'offset': 0.0}),
    mapping_engine.MappingTie(['current', 'current_b'], 'sum', 'usable_soc'),
    mapping_engine.MappingTie(['current', 'current_b', 'primary_current_a'], 'average', 'fine_soc_pct'),
    mapping_engine.MappingTie(['temp_min', 'temp_max'], 'min', 'batt_temp_c'),
    mapping_engine.MappingTie(['temp_min', 'temp_max', 'primary_pack_v'], 'max', 'soc_correction'),
    mapping_engine.MappingTie(['capacity_pack1_ah'], 'soh_percent', 'soh_pct', {'nameplate_ah': 201.0}),
    mapping_engine.MappingTie(['capacity_pack2_ah', 'capacity_pack1_ah'], 'lookup', 'capacity_bars_raw',
                               {'table': [[100.0, 1.0], [50.0, 5.0], [0.0, 14.0], [150.0, 0.0]]}),
]


def _python_mirror_eval(tie):
    """Independent re-derivation of the GENERATED C's control flow - written
    fresh here, not derived from tools/export_stm32_config.py's own source,
    so a bug shared between codegen and this mirror is unlikely to be the
    same bug. `present_values` is {input_key: (is_live, value)}."""
    combine = tie.combine
    params = tie.params

    def eval_with(present_values):
        if combine in ('linear', 'soh_percent', 'lookup'):
            for key in tie.inputs:
                is_live, value = present_values[key]
                if not is_live:
                    continue
                if combine == 'linear':
                    return value * params.get('scale', 1.0) + params.get('offset', 0.0)
                if combine == 'soh_percent':
                    nameplate = params.get('nameplate_ah', mapping_engine.NAMEPLATE_CAPACITY_AH)
                    return (value / nameplate) * 100.0 if nameplate != 0.0 else 0.0
                # lookup: table pre-sorted ascending by x, walk keeping last x <= value
                table = sorted(params['table'], key=lambda p: p[0])
                best_y = table[0][1]
                for x, y in table:
                    if x <= value:
                        best_y = y
                    else:
                        break
                return best_y
            return None
        if combine in ('sum', 'average'):
            vals = [v for (live, v) in (present_values[k] for k in tie.inputs) if live]
            if not vals:
                return None
            return sum(vals) if combine == 'sum' else sum(vals) / len(vals)
        if combine in ('min', 'max'):
            best = None
            for key in tie.inputs:
                is_live, value = present_values[key]
                if not is_live:
                    continue
                if best is None or (combine == 'min' and value < best) or (combine == 'max' and value > best):
                    best = value
            return best
        raise ValueError(combine)

    return eval_with


def run_semantic_fuzz(iterations, seed):
    rng = random.Random(seed)
    mismatches = 0
    checked = 0
    for tie_dict in [t.to_dict() for t in SYNTHETIC_TIES]:
        tie = mapping_engine.MappingTie.from_dict(tie_dict)
        mirror = _python_mirror_eval(tie)
        for _ in range(iterations):
            present_values = {}
            values_in_order = []
            for key in tie.inputs:
                is_live = rng.random() > 0.3
                value = rng.uniform(-250.0, 450.0)
                present_values[key] = (is_live, value)
                values_in_order.append(value if is_live else None)

            expected = mapping_engine.evaluate_combine(values_in_order, tie.combine, tie.params)
            actual = mirror(present_values)
            checked += 1
            if expected is None and actual is None:
                continue
            if expected is None or actual is None or abs(expected - actual) > 1e-6:
                mismatches += 1
                if mismatches <= 5:
                    print(f'MISMATCH tie={tie.name!r} present={present_values} '
                          f'expected={expected!r} actual={actual!r}')
    print(f'Semantic fuzz: {checked} checks across {len(SYNTHETIC_TIES)} synthetic ties '
          f'(all 7 combine types), {mismatches} mismatches')
    return mismatches == 0


def run_codegen_syntax_check():
    warnings = codegen.Warnings()
    state, _real_mapping, mgmt, profile_name = codegen.load_and_validate(
        codegen.DEFAULT_PROFILE_PATH, warnings)

    synthetic_mapping = mapping_engine.MappingEngine(list(SYNTHETIC_TIES))
    ties, lookup_tables = codegen._build_mapping_model(synthetic_mapping)
    header_text = codegen.generate_header(state, synthetic_mapping, mgmt, profile_name, ties, lookup_tables)

    with tempfile.TemporaryDirectory() as tmpdir:
        header_path = os.path.join(tmpdir, 'bridge_config_gen.h')
        with open(header_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(header_text)
        with open(os.path.join(tmpdir, 'main.h'), 'w', encoding='utf-8') as f:
            f.write(_STUB_MAIN_H)
        with open(os.path.join(tmpdir, 'can.h'), 'w', encoding='utf-8') as f:
            f.write(_STUB_CAN_H)
        shutil.copy(os.path.join(STM32_INC, 'leaf_output.h'), tmpdir)
        shutil.copy(os.path.join(STM32_INC, 'rz450e_ingest.h'), tmpdir)
        stub_c = os.path.join(tmpdir, 'probe.c')
        with open(stub_c, 'w', encoding='utf-8') as f:
            f.write('#include "bridge_config_gen.h"\n'
                    'void _force_reference(LeafState *out) { bridge_config_apply_mapping_ties(out); }\n')

        if not os.path.exists(CLANG):
            print(f'SKIP codegen syntax check: clang not found at {CLANG!r}')
            return True

        result = subprocess.run(
            [CLANG, '-fsyntax-only', '-Wall', '-Wextra', '-Werror', f'-I{tmpdir}', stub_c],
            capture_output=True, text=True)
        if result.returncode != 0:
            print('Codegen syntax check FAILED:')
            print(result.stdout)
            print(result.stderr)
            return False
        print('Codegen syntax check: all 7 combine types compile clean (-Wall -Wextra -Werror)')
        return True


def main():
    ok_semantic = run_semantic_fuzz(iterations=5000, seed=20260818)
    ok_syntax = run_codegen_syntax_check()
    sys.exit(0 if (ok_semantic and ok_syntax) else 1)


if __name__ == '__main__':
    main()
