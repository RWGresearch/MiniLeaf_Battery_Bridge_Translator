"""Cross-checks the leaf_output.c frame builders against the real
bridge/leaf_signals.py builders across random LeafState values.

No ARM/x86 C toolchain is available in this dev environment - see
check_stm32_rz_ingest_decode.py's own docstring for the full explanation.
This re-derives the exact same bit-packing arithmetic independently in
Python (hand-typed from the C source, not imported) and fuzzes it against
the real bridge/leaf_signals.py builder functions across thousands of
random field values, covering every frame builder including the startup
variants and the opaque-table override paths.

Run: py tests/check_stm32_leaf_output_builders.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import leaf_signals as ls

N = 20000


def iround(v):
    return int(v + (0.5 if v >= 0 else -0.5))


def crc8(data7):
    crc = 0
    for b in data7:
        c = crc ^ b
        for _ in range(8):
            c = ((c << 1) ^ 0x85) & 0xFF if (c & 0x80) else (c << 1) & 0xFF
        crc = c
    return crc


# ── Independent re-derivations of leaf_output.c's builders ─────────────────
def c_build_1db(s, prun, latch):
    raw_i = iround(s['pack_current_a'] * 2) & 0x7FF
    raw_v = iround(s['pack_voltage_v'] * 2) & 0x3FF
    b = [
        (raw_i >> 3) & 0xFF,
        ((raw_i & 7) << 5) | ((int(s['relay_cut_request']) & 3) << 3) | (int(s['failsafe_status']) & 7),
        raw_v >> 2,
        ((raw_v & 3) << 6) | (int(s['main_relay_on']) << 5) | (int(s['full_charge_flag']) << 4)
            | (int(s['interlock']) << 3) | ((int(s['discharge_pwr_sts']) & 3) << 1) | latch,
        int(s['usable_soc']) & 0x7F,
        0x00,
        prun,
    ]
    return bytes(b + [crc8(b)])


def c_build_1db_startup(s, prun, t_ms):
    soc = int(s['usable_soc']) & 0x7F
    if t_ms < ls.T_PH_B:
        b = [0x7F, 0xE0, 0xFF, 0xC6, soc, 0x00, prun]
    elif t_ms < ls.T_PH_C:
        b = [0xFF, 0xE0, 0xFF, 0xE7, soc, 0x00, prun]
    else:
        b = [0x00, 0x00, 0xFF, 0xEF, 0x64, 0x00, prun]
    return bytes(b + [crc8(b)])


def c_build_1dc(s, prun, uprate, code_override):
    raw_d = iround(s['discharge_limit_kw'] / 0.25) & 0x3FF
    raw_c = iround(s['charge_limit_kw'] / 0.25) & 0x3FF
    raw_g = iround((s['charger_limit_kw'] + 10) / 0.1) & 0x3FF
    c4, c5, c6 = code_override if code_override is not None else ls.CODE_1DC[prun]
    b = [
        raw_d >> 2,
        ((raw_d & 3) << 6) | ((raw_c >> 4) & 0x3F),
        ((raw_c & 0xF) << 4) | ((raw_g >> 6) & 0xF),
        ((raw_g & 0x3F) << 2) | (int(s['charge_pwr_sts']) & 3),
        c4 | ((uprate & 7) << 5), c5, c6 | prun,
    ]
    return bytes(b + [crc8(b)])


def c_build_1dc_startup(prun, first_frame):
    if first_frame:
        b = [0xFF, 0xFF, 0xFF, 0xFF, 0x1F, 0xFF, 0xFC | prun]
    else:
        c4, c5, c6 = ls.CODE_1DC[prun]
        b = [0xFF, 0xFF, 0xFF, 0xFF, c4, c5, c6 | prun]
    return bytes(b + [crc8(b)])


def c_build_1c2(tick10):
    return bytes([0x50 | (tick10 & 0x0F)])


def c_build_1ed(s, prun):
    raw = iround((s['chg2_limit_kw'] - 10) / 0.1) & 0x7FF
    b = [(raw >> 3) & 0xFF, ((raw & 7) << 5) | (prun & 3)]
    return bytes(b + [crc8(b)])


def c_build_55b(s, prun, ir_raw, refuse_sleep):
    raw_soc = iround(s['fine_soc_pct'] * 10) & 0x3FF
    b = [
        raw_soc >> 2,
        (raw_soc & 3) << 6,
        0x55,
        0x00,
        ir_raw >> 2,
        ((ir_raw & 3) << 6) | (int(s['ir_malfunction']) & 1),
        (int(s['capacity_empty']) << 7) | ((refuse_sleep & 3) << 5) | 0x10 | prun,
    ]
    return bytes(b + [crc8(b)])


def c_build_5bc(s, tick10, gids_override, chg_time_override):
    gids = gids_override if gids_override is not None and gids_override >= 0 else (int(s['gids']) & 0x3FF)
    toggle = 1 - ((tick10 // 5) & 1)
    bars_raw = int(s['capacity_bars_raw']) & 0xF
    b5, b6, b7 = chg_time_override if chg_time_override is not None else ls.CHG_TIME_5BC[tick10 % 7]
    return bytes([
        gids >> 2,
        (gids & 3) << 6,
        (bars_raw << 4) | (0x0E if toggle else 0x04),
        iround(s['temp_segment_pct'] / 0.4166666) & 0xFF,
        ((int(s['soh_pct']) & 0x7F) << 1) | toggle,
        b5 | ((int(s['pwr_limit_reason']) & 7) << 5),
        b6, b7,
    ])


def c_build_5bc_first(s, tick10):
    toggle = 1 - ((tick10 // 5) & 1)
    return bytes([0xFF, 0xC0, 0xFF, 0xFF, ((int(s['soh_pct']) & 0x7F) << 1) | toggle, 0x03, 0xFF, 0xFF])


def c_build_59e(s):
    full = iround(s['qc_full_wh'] / 100) & 0x1FF
    rem = iround(s['qc_remain_wh'] / 100) & 0x1FF
    return bytes([
        0x00, 0x00, (full >> 4) & 0x1F, ((full & 0xF) << 4) | ((rem >> 5) & 0xF),
        (rem & 0x1F) << 3, 0x00, 0x00, int(s['soc_correction']) & 0xFF,
    ])


def c_build_5c0(s, tick10, hist_override):
    mux = (tick10 % 6) + 1
    t = (int(s['batt_temp_c']) + 40) & 0x7F
    b3, b4, b5 = hist_override if hist_override is not None else ls.HIST5C0[mux]
    return bytes([mux, t << 1, t << 1, b3, b4, b5, 0x1F, int(s['dtc']) & 0xFF])


def c_build_5eb(tick10):
    return bytes(ls.SEQ_5EB[tick10 % len(ls.SEQ_5EB)])


def run():
    random.seed(555)
    fails = []

    for _ in range(N):
        s = {}
        for group in ls.SLIDERS.values():
            for key, _label, lo, hi, _step, _default in group:
                s[key] = random.uniform(lo, hi)
        for key, _label, _default in ls.CHECKS:
            s[key] = random.choice([0, 1])
        for key, _label, lo, hi, _step, _default in ls.ZE1_62_SLIDERS:
            s[key] = random.uniform(lo, hi)

        prun = random.randint(0, 3)
        latch = random.randint(0, 1)
        uprate = random.randint(0, 7)
        tick10 = random.randint(0, 255)
        t_ms = random.choice([0, 50, 65, 100, 155, 195, 200, 325, 400, 565, 865, 915, 1000, 2105, 3000])
        ir_raw = random.choice([904, 0x3FF])
        refuse_sleep = random.randint(0, 3)
        first_frame = random.choice([True, False])
        code_override = random.choice([None, (0, 0, 0)])
        chg_time_override = random.choice([None, (0, 0, 0)])
        hist_override = random.choice([None, (0, 0, 0)])
        gids_override = random.choice([None, 0x3FF])

        checks = [
            ('1db', ls.build_1db(s, prun, latch), c_build_1db(s, prun, latch)),
            ('1db_startup', ls.build_1db_startup(s, prun, t_ms), c_build_1db_startup(s, prun, t_ms)),
            ('1dc', ls.build_1dc(s, prun, uprate, code_override), c_build_1dc(s, prun, uprate, code_override)),
            ('1dc_startup', ls.build_1dc_startup(prun, first_frame), c_build_1dc_startup(prun, first_frame)),
            ('1c2', ls.build_1c2(tick10), c_build_1c2(tick10)),
            ('1ed', ls.build_1ed(s, prun), c_build_1ed(s, prun)),
            ('55b', ls.build_55b(s, prun, ir_raw=ir_raw, refuse_sleep=refuse_sleep),
                    c_build_55b(s, prun, ir_raw, refuse_sleep)),
            ('5bc', ls.build_5bc(s, tick10, gids_raw=gids_override, chg_time_override=chg_time_override),
                    c_build_5bc(s, tick10, gids_override, chg_time_override)),
            ('5bc_first', ls.build_5bc_first(s, tick10), c_build_5bc_first(s, tick10)),
            ('59e', ls.build_59e(s), c_build_59e(s)),
            ('5c0', ls.build_5c0(s, tick10, hist_override=hist_override), c_build_5c0(s, tick10, hist_override)),
            ('5eb', ls.build_5eb(tick10), c_build_5eb(tick10)),
        ]
        for name, py_bytes, c_bytes in checks:
            if py_bytes != c_bytes:
                fails.append((name, py_bytes, c_bytes))

        if len(fails) > 20:
            break

    print(f"Ran {N} random LeafState fuzz iterations x 12 builders")
    print(f"Mismatches: {len(fails)}")
    for f in fails[:20]:
        print(" ", f)
    return len(fails) == 0


if __name__ == '__main__':
    ok = run()
    sys.exit(0 if ok else 1)
