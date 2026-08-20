"""Cross-checks rz450e_uds.c's ISO-TP reassembly and DID decode formulas.

No ARM/x86 C toolchain is available in this dev environment - see
check_stm32_rz_ingest_decode.py's own docstring for the full explanation.

Two independent checks:
1. ISO-TP (ISO 15765-2) reassembly: a from-scratch Python re-derivation of
   rz450e_uds.c's on_frame() state machine (hand-typed from the C source,
   not imported) is fed a PROGRAMMATICALLY GENERATED single-frame or
   first-frame+consecutive-frame sequence (built the way a real UDS
   responder would send one, for random payload lengths/content) and must
   reconstruct the exact original payload bytes, for both the C-mirror and
   an independent re-derivation of bridge/rz450e_signals.py's DidClient.
   request() reassembly logic (also hand-typed, not imported - that method
   is normally only reachable via a blocking call interleaved with a live
   queue, not easily unit-testable in isolation).
2. DID decode formulas (decode_soc/decode_capacity/decode_primary_v_i/
   decode_temp_probes_did): fuzzed directly against the REAL, imported
   bridge/rz450e_signals.py functions across random payloads.

Run: py tests/check_stm32_rz450e_uds.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import rz450e_signals as rz

N = 20000


# ── ISO-TP frame sequence generator (builds a real responder's frame
# sequence for a given payload, single-frame or first-frame+consecutive) ──
def build_iso_tp_frames(payload):
    n = len(payload)
    if n <= 7:
        pci = n & 0x0F
        data = [pci] + list(payload) + [0] * (7 - n)
        return [bytes(data[:8])]

    frames = []
    ff = [0x10 | ((n >> 8) & 0x0F), n & 0xFF] + list(payload[0:6])
    frames.append(bytes(ff))
    rest = payload[6:]
    seq = 1
    for i in range(0, len(rest), 7):
        chunk = rest[i:i + 7]
        cf = [0x20 | (seq & 0x0F)] + list(chunk) + [0] * (7 - len(chunk))
        frames.append(bytes(cf[:8]))
        seq += 1
    return frames


# ── Independent re-derivation of rz450e_uds.c's on_frame() reassembly ─────
class CMirrorReassembly:
    def __init__(self):
        self.resp_buf = bytearray(40)
        self.resp_expected_len = 0
        self.resp_received_len = 0
        self.resp_in_progress = False
        self.resp_complete = False
        self.fc_sent = 0

    def on_frame(self, data):
        if len(data) < 2:
            return
        pci = (data[0] >> 4) & 0x0F
        if pci == 0x0:
            length = data[0] & 0x0F
            if length > 7:
                length = 7
            self.resp_buf[0:length] = data[1:1 + length]
            self.resp_received_len = length
            self.resp_expected_len = length
            self.resp_complete = True
        elif pci == 0x1:
            expected = ((data[0] & 0x0F) << 8) | data[1]
            if expected > 40:
                expected = 40
            self.resp_expected_len = expected
            self.resp_buf[0:6] = data[2:8]
            self.resp_received_len = 6
            self.resp_in_progress = True
            self.fc_sent += 1
        elif pci == 0x2 and self.resp_in_progress:
            for i in range(7):
                if self.resp_received_len < 40 and self.resp_received_len < self.resp_expected_len:
                    self.resp_buf[self.resp_received_len] = data[1 + i]
                    self.resp_received_len += 1
            if self.resp_received_len >= self.resp_expected_len:
                self.resp_complete = True
                self.resp_in_progress = False

    def result(self):
        return bytes(self.resp_buf[:self.resp_received_len]) if self.resp_complete else None


# ── Independent re-derivation of bridge/rz450e_signals.py's DidClient.
# request() reassembly logic (that method is a blocking call over a live
# queue - not directly unit-testable, so re-derived here as a plain
# feed-frames-in-order function instead) ──────────────────────────────────
def py_mirror_reassemble(frames):
    payload = None
    expected_len = None
    for data in frames:
        pci = data[0] >> 4
        if pci == 0x0:
            length = data[0] & 0x0F
            return bytes(data[1:1 + length])
        elif pci == 0x1:
            expected_len = ((data[0] & 0x0F) << 8) | data[1]
            payload = list(data[2:8])
        elif pci == 0x2 and payload is not None:
            payload += list(data[1:8])
            if len(payload) >= expected_len:
                return bytes(payload[:expected_len])
    return None


def run_isotp_checks(rnd):
    fails = []
    for _ in range(2000):
        n = rnd.choice([rnd.randint(1, 7), rnd.randint(8, 35)])
        payload = bytes(rnd.randint(0, 255) for _ in range(n))
        frames = build_iso_tp_frames(payload)

        c_mirror = CMirrorReassembly()
        for f in frames:
            c_mirror.on_frame(f)
        c_result = c_mirror.result()

        py_result = py_mirror_reassemble(frames)

        if c_result != payload:
            fails.append(('c_mirror', n, payload, c_result))
        if py_result != payload:
            fails.append(('py_mirror', n, payload, py_result))
        if c_result != py_result:
            fails.append(('c_vs_py', n, c_result, py_result))
    return fails


def run_did_decode_checks(rnd):
    fails = []
    for _ in range(N):
        # decode_soc
        d = bytes(rnd.randint(0, 255) for _ in range(rnd.choice([4, 8])))
        py = rz.decode_soc(d)
        if len(d) >= 4:
            c_soc = d[3] * 100.0 / 255.0
            if abs(py.get('soc_pct', -1) - c_soc) > 1e-6:
                fails.append(('decode_soc', d, py, c_soc))

        # decode_capacity
        d = bytes(rnd.randint(0, 255) for _ in range(rnd.choice([11, 12, 15])))
        py = rz.decode_capacity(d)
        if len(d) >= 11:
            def u16(dd, n):
                return dd[n] * 256 + dd[n + 1]
            expected = {
                'capacity_pack1_ah': u16(d, 3) / 100.0,
                'capacity_pack2_ah': u16(d, 5) / 100.0,
                'capacity_pack3_ah': u16(d, 7) / 100.0,
                'capacity_pack4_ah': u16(d, 9) / 100.0,
            }
            for k, v in expected.items():
                if abs(py.get(k, -1) - v) > 1e-6:
                    fails.append(('decode_capacity', k, d, py, expected))

        # decode_primary_v_i
        d = bytes(rnd.randint(0, 255) for _ in range(rnd.choice([9, 10, 12])))
        py = rz.decode_primary_v_i(d)
        if len(d) >= 9:
            def u16(dd, n):
                return dd[n] * 256 + dd[n + 1]
            def s16(dd, n):
                v = u16(dd, n)
                return v - 65536 if v >= 32768 else v
            expected_v = u16(d, 5) / 64.0
            expected_i = s16(d, 7) * 0.1
            if abs(py.get('primary_pack_v', -1) - expected_v) > 1e-6:
                fails.append(('decode_primary_v_i v', d, py, expected_v))
            if abs(py.get('primary_current_a', -999) - expected_i) > 1e-6:
                fails.append(('decode_primary_v_i i', d, py, expected_i))

        # decode_temp_probes_did
        d = bytes(rnd.randint(0, 255) for _ in range(rnd.choice([35, 36, 40])))
        py = rz.decode_temp_probes_did(d)
        if len(d) >= 35:
            def u16(dd, n):
                return dd[n] * 256 + dd[n + 1]
            for n in range(1, 17):
                expected = u16(d, 3 + (n - 1) * 2) / 256.0 - 50.0
                key = f'temp_{n:02d}_did'
                if abs(py.get(key, -999) - expected) > 1e-6:
                    fails.append(('decode_temp_probes_did', n, d, py.get(key), expected))
    return fails


def run():
    rnd = random.Random(2026)
    isotp_fails = run_isotp_checks(rnd)
    did_fails = run_did_decode_checks(rnd)

    print(f"ISO-TP reassembly: {len(isotp_fails)} mismatches (2000 random frame sequences)")
    for f in isotp_fails[:20]:
        print(" ", f)
    print(f"DID decode formulas: {len(did_fails)} mismatches ({N} iterations x 4 DIDs)")
    for f in did_fails[:20]:
        print(" ", f)

    return len(isotp_fails) == 0 and len(did_fails) == 0


if __name__ == '__main__':
    ok = run()
    sys.exit(0 if ok else 1)
