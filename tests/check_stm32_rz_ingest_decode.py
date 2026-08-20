"""Cross-checks the bit arithmetic in STM32_MiniLeaf_Bridge_Translator_uVision's
rz450e_ingest.c against the real Python decoders in bridge/rz450e_signals.py.

No ARM/x86 C toolchain is available in this dev environment (only a
bare-metal RISC-V-only clang, per the STM32 port session notes) so this does
NOT compile or execute rz450e_ingest.c itself. Instead it re-derives the same
bit-shift/mask arithmetic independently in Python (fixed-width uint8/uint16
semantics emulated with explicit masks) and fuzzes it against the real
decoders across many random byte payloads - this catches a transcription
error in the C port (wrong shift amount, wrong nibble, an off-by-one on a
cell/probe number) that a small hand-picked test vector could miss, but it
does NOT verify the C actually compiles/links/runs correctly on real
hardware. Re-run this any time rz450e_ingest.c's decode functions change, and
keep the Python re-derivation below in sync by hand - it is a deliberate
second, independent implementation, not a copy-paste of the C.

Separately confirmed once (2026-08-18, not repeated by this script):
`clang -fsyntax-only -Wall -Wextra -Wconversion -pedantic` against the real
rz450e_ingest.c compiled clean (only benign -Wconversion noise from C's
usual integer promotion on shift/OR expressions, not a real bug).

Run: py tests/check_stm32_rz_ingest_decode.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import rz450e_signals as rz

N = 20000
U16 = 0xFFFF


# ── Independent re-derivation of rz450e_ingest.c's decode functions ────────
# (re-typed from the C source by hand, NOT copy-pasted, and NOT importing the
# C file - the whole point is two independently-written implementations
# agreeing, not one file checking itself.)

def c_decode_020(d):
    pack_v = float(((d[0] << 4) | (d[1] >> 4)) & U16)
    cell_min = ((((d[1] & 0x0F) << 8) | d[2]) & U16) * (5.0 / 4096.0)
    cell_max = (((d[3] << 4) | (d[4] >> 4)) & U16) * (5.0 / 4096.0)
    return pack_v, cell_min, cell_max


def c_decode_023(d):
    current = (float((((d[0] << 4) | (d[1] >> 4)) & U16)) - 2048.0) * 0.1
    current_b = (float((((d[4] << 4) | (d[5] >> 4)) & U16)) - 2048.0) * 0.1
    return current, current_b


def c_decode_cell_msg(d):
    base = d[0]
    if base > 0x5C or (base % 4) != 0:
        return None
    raw = [
        ((d[1] << 4) | (d[2] >> 4)) & U16,
        (((d[2] & 0x0F) << 8) | d[3]) & U16,
        ((d[4] << 4) | (d[5] >> 4)) & U16,
        (((d[5] & 0x0F) << 8) | d[6]) & U16,
    ]
    out = {}
    for k in range(4):
        cell_number = base + k + 1
        if cell_number <= 96:
            out[cell_number] = raw[k] * (5.0 / 4096.0)
    return out


def c_decode_temp_msg(d):
    mux = d[0]
    if mux not in (0x00, 0x07, 0x0E):
        return None
    out = {}
    for j in range(1, 8):
        probe_number = mux + j
        if probe_number <= 16:
            out[probe_number] = float(d[j])
    return out


def c_toyota_checksum(arb_id, d, dlc):
    s = (arb_id >> 8) + (arb_id & 0xFF) + dlc
    for i in range(dlc - 1):
        s += d[i]
    return s & 0xFF


def run():
    random.seed(1234)
    fails = []

    for _ in range(N):
        d = [random.randint(0, 255) for _ in range(8)]

        py = rz.decode_020(bytes(d))
        cpv, cmn, cmx = c_decode_020(d)
        if py:
            if abs(py['pack_v'] - cpv) > 1e-9:
                fails.append(('020 pack_v', d, py['pack_v'], cpv))
            if abs(py['cell_min'] - cmn) > 1e-9:
                fails.append(('020 cell_min', d, py['cell_min'], cmn))
            if abs(py['cell_max'] - cmx) > 1e-9:
                fails.append(('020 cell_max', d, py['cell_max'], cmx))

        py = rz.decode_023(bytes(d))
        cur, curb = c_decode_023(d)
        if py:
            if abs(py['current'] - cur) > 1e-9:
                fails.append(('023 current', d, py['current'], cur))
            if abs(py['current_b'] - curb) > 1e-9:
                fails.append(('023 current_b', d, py['current_b'], curb))

        py = rz.decode_cell_msg(bytes(d))
        c_out = c_decode_cell_msg(d)
        py_by_num = {int(k.split('_')[1]): v for k, v in py.items()}
        if c_out is None:
            if py_by_num:
                fails.append(('cell base-reject mismatch', d, py_by_num, c_out))
        else:
            if set(py_by_num.keys()) != set(c_out.keys()):
                fails.append(('cell keys', d, sorted(py_by_num.keys()), sorted(c_out.keys())))
            elif any(abs(py_by_num[k] - c_out[k]) > 1e-9 for k in c_out):
                fails.append(('cell values', d, py_by_num, c_out))

        py = rz.decode_temp_msg(bytes(d))
        c_out = c_decode_temp_msg(d)
        py_by_num = {int(k.split('_')[1]): v for k, v in py.items()}
        if c_out is None:
            if py_by_num:
                fails.append(('temp mux-reject mismatch', d, py_by_num, c_out))
        else:
            if set(py_by_num.keys()) != set(c_out.keys()):
                fails.append(('temp keys', d, sorted(py_by_num.keys()), sorted(c_out.keys())))
            elif any(abs(py_by_num[k] - c_out[k]) > 1e-9 for k in c_out):
                fails.append(('temp values', d, py_by_num, c_out))

        for arb_id in rz.CHECKSUM_IDS:
            py_cs = rz.toyota_sum_checksum(arb_id, bytes(d))
            c_cs = c_toyota_checksum(arb_id, d, 8)
            if py_cs != c_cs:
                fails.append((f'checksum {arb_id:#x}', d, py_cs, c_cs))

    print(f"Ran {N} random frames x (020, 023, cell-mux, temp-mux, "
          f"checksum x{len(rz.CHECKSUM_IDS)}) checks")
    print(f"Mismatches: {len(fails)}")
    for f in fails[:20]:
        print(" ", f)
    return len(fails) == 0


if __name__ == '__main__':
    ok = run()
    sys.exit(0 if ok else 1)
