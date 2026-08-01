"""Lexus RZ450e source signals: raw-CAN decoders (fast, primary), a compact
UDS/ISO-TP DID client (slow, secondary), and the input-signal registry used
by the mapping GUI. Decode formulas are ported from
Refrance/RZ450e_battery_can_decode_Project/rx450e_can_analyzer.py's own
confirmed decoder functions - see docs/02-source-signals-rz450e.md for the
citation and confirmed/unverified status of each signal.

Only CONFIRMED signals are implemented here, per this project's inherited
confirmed-vs-unverified discipline (docs/02).
"""
import queue
import time

# ── Addressing ───────────────────────────────────────────────────────────
OBD_BROADCAST = 0x7DF
TOYOTA_REQ_ID = 0x747
TOYOTA_RESP_ID = 0x74F

# Raw CAN IDs used (docs/02)
ID_PACK_V = 0x020
ID_CURRENT = 0x023
ID_TEMP_MINMAX = 0x4A7
ID_CELLS_A = 0x4A9
ID_CELLS_B = 0x4C0
ID_TEMPS = 0x4AA
ID_CHARGE_PERM = 0x358
ID_ALIVE_3F1 = 0x3F1
ID_TICK_424 = 0x424   # bus1

FAST_RAW_IDS = {ID_PACK_V, ID_CURRENT, ID_TEMP_MINMAX, ID_CELLS_A, ID_CELLS_B,
                ID_TEMPS, ID_CHARGE_PERM, ID_ALIVE_3F1, ID_TICK_424}

# DIDs (slow, UDS ReadDataByIdentifier over 0x747->0x74F)
DID_SOC = (0x1F, 0x5B)
DID_CAPACITY = (0x1D, 0x3E)
DID_PRIMARY_V_I = (0x1F, 0x9A)
DID_POLL_INTERVAL_S = 5.0   # cycle through the 3 DIDs roughly every 5s each


def toyota_sum_checksum(arb_id, data):
    """Confirmed 100%-match additive checksum, last byte of the frame."""
    n = len(data) - 1
    return ((arb_id >> 8) + (arb_id & 0xFF) + len(data)
             + sum(b for j, b in enumerate(data) if j != n)) & 0xFF


def _u12_quad(d):
    return [(d[1] << 4) | (d[2] >> 4),
            ((d[2] & 0x0F) << 8) | d[3],
            (d[4] << 4) | (d[5] >> 4),
            ((d[5] & 0x0F) << 8) | d[6]]


def decode_020(d):
    if len(d) < 6:
        return {}
    return {
        'pack_v': float((d[0] << 4) | (d[1] >> 4)),
        'cell_min': (((d[1] & 0x0F) << 8) | d[2]) * 5.0 / 4096.0,
        'cell_max': ((d[3] << 4) | (d[4] >> 4)) * 5.0 / 4096.0,
    }


def decode_023(d):
    if len(d) < 7:
        return {}
    return {
        'current': (((d[0] << 4) | (d[1] >> 4)) - 0x800) * 0.1,
        'current_b': (((d[4] << 4) | (d[5] >> 4)) - 0x800) * 0.1,
    }


def decode_358(d):
    if len(d) < 2:
        return {}
    return {'charge_permission_input': float(d[0] & 1), 'alive_358': float(d[1])}


def decode_3f1(d):
    if len(d) < 1:
        return {}
    return {'alive_3f1': float(d[0] & 0x0F)}


def decode_424(d):
    if len(d) < 1:
        return {}
    return {'counter_5s': float(d[0])}


def decode_cell_msg(d):
    if len(d) < 8:
        return {}
    base = d[0]
    if base > 0x5C or base % 4:
        return {}
    out = {}
    for k, raw in enumerate(_u12_quad(d)):
        cell = base + k + 1
        if cell <= 96:
            out[f'cell_{cell:02d}'] = raw * 5.0 / 4096.0
    return out


def decode_temp_msg(d):
    if len(d) < 8:
        return {}
    mux = d[0]
    if mux not in (0x00, 0x07, 0x0E):
        return {}
    out = {}
    for j in range(1, 8):
        probe = mux + j
        if probe <= 16:
            out[f'temp_{probe:02d}'] = d[j] * 9 / 5 + 32.0
    return out


def decode_temp_minmax(d):
    if len(d) < 4:
        return {}
    return {
        'temp_max': d[0] * 9 / 5 + 32.0,
        'temp_min': d[1] * 9 / 5 + 32.0,
        'temp_max_probe': float(d[2]),
        'temp_min_probe': float(d[3]),
    }


_RAW_DECODERS = {
    ID_PACK_V: decode_020,
    ID_CURRENT: decode_023,
    ID_CHARGE_PERM: decode_358,
    ID_ALIVE_3F1: decode_3f1,
    ID_TICK_424: decode_424,
    ID_CELLS_A: decode_cell_msg,
    ID_CELLS_B: decode_cell_msg,
    ID_TEMPS: decode_temp_msg,
    ID_TEMP_MINMAX: decode_temp_minmax,
}


def decode_frame(arb_id, data):
    """Dispatch a raw CAN frame to its decoder. Returns {} for unrecognized
    or not-yet-confirmed IDs (nothing outside FAST_RAW_IDS is decoded)."""
    fn = _RAW_DECODERS.get(arb_id)
    if not fn:
        return {}
    try:
        return fn(bytes(data))
    except (IndexError, ValueError):
        return {}


def decode_soc(d):
    if len(d) < 4:
        return {}
    return {'soc_pct': d[3] * 100.0 / 255.0}


def _u16(d, n):
    return d[n] * 256 + d[n + 1]


def _s16(d, n):
    v = _u16(d, n)
    return v - 65536 if v >= 32768 else v


def decode_capacity(d):
    if len(d) < 11:
        return {}
    return {
        'capacity_pack1_ah': _u16(d, 3) / 100.0,
        'capacity_pack2_ah': _u16(d, 5) / 100.0,
        'capacity_pack3_ah': _u16(d, 7) / 100.0,
        'capacity_pack4_ah': _u16(d, 9) / 100.0,
    }


def decode_primary_v_i(d):
    if len(d) < 9:
        return {}
    return {
        'primary_pack_v': _u16(d, 5) / 64.0,
        'primary_current_a': _s16(d, 7) * 0.1,
    }


class DidClient:
    """Minimal blocking ISO-TP (ISO 15765-2) client for UDS ReadDataByIdentifier
    (service 0x22) over the Toyota extended addressing (0x747 -> 0x74F). Call
    `feed()` for every frame received on the same bus as the response ID -
    it swallows frames belonging to an in-flight transaction and returns True,
    so the caller's generic raw-CAN decode loop can skip those frames."""

    def __init__(self, bus_connection, request_id=TOYOTA_REQ_ID, response_id=TOYOTA_RESP_ID):
        self.bus = bus_connection
        self.request_id = request_id
        self.response_id = response_id
        self._resp_queue = queue.Queue()
        self._active = False

    def feed(self, arb_id, data):
        if arb_id == self.response_id and self._active:
            self._resp_queue.put(bytes(data))
            return True
        return False

    def request(self, did, timeout=2.0):
        """did: (hi_byte, lo_byte). Returns the decoded UDS payload list `d`
        (d[0]=0x62 echo, d[1:3]=DID, data from d[3] on) or None on timeout."""
        if not self.bus or not self.bus.connected:
            return None
        while not self._resp_queue.empty():
            try:
                self._resp_queue.get_nowait()
            except queue.Empty:
                break
        self._active = True
        try:
            req = bytes([0x03, 0x22, did[0], did[1], 0, 0, 0, 0])
            if not self.bus.send(self.request_id, req):
                return None
            deadline = time.monotonic() + timeout
            payload = None
            expected_len = None
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    data = self._resp_queue.get(timeout=max(0.01, remaining))
                except queue.Empty:
                    break
                if not data:
                    continue
                pci = data[0] >> 4
                if pci == 0x0:
                    length = data[0] & 0x0F
                    return list(data[1:1 + length])
                elif pci == 0x1:
                    expected_len = ((data[0] & 0x0F) << 8) | data[1]
                    payload = list(data[2:8])
                    fc = bytes([0x30, 0x00, 0x0A, 0, 0, 0, 0, 0])
                    self.bus.send(self.request_id, fc)
                elif pci == 0x2 and payload is not None:
                    payload += list(data[1:8])
                    if len(payload) >= expected_len:
                        return payload[:expected_len]
            return None
        finally:
            self._active = False


# ── Input signal registry (for the mapping/management GUI dropdowns) ──────
# 'source' is the CAN ID / DID this signal actually comes from, shown in the
# mapping dropdowns (docs/08) so it's obvious which message a decoded value
# stands for. 'range' is a display-only (lo, hi) used to scale the
# dashboard's bar gauges - not a hard/confirmed limit.
def _build_input_registry():
    reg = [
        {'key': 'pack_v', 'label': 'Pack voltage (whole V)', 'unit': 'V', 'fast': True, 'source': '0x020', 'group': '0x020 Pack V / cell min-max', 'range': (300, 420)},
        {'key': 'cell_min', 'label': 'Min cell voltage (pack summary)', 'unit': 'V', 'fast': True, 'source': '0x020', 'group': '0x020 Pack V / cell min-max', 'range': (2.5, 5.0)},
        {'key': 'cell_max', 'label': 'Max cell voltage (pack summary)', 'unit': 'V', 'fast': True, 'source': '0x020', 'group': '0x020 Pack V / cell min-max', 'range': (2.5, 5.0)},
        {'key': 'current', 'label': 'Pack current (+discharge/-charge)', 'unit': 'A', 'fast': True, 'source': '0x023', 'group': '0x023 Pack current', 'range': (-220, 220)},
        {'key': 'current_b', 'label': 'Pack current, 2nd sensor tap', 'unit': 'A', 'fast': True, 'source': '0x023', 'group': '0x023 Pack current', 'range': (-220, 220)},
        {'key': 'temp_max', 'label': 'Max pack temperature', 'unit': '°F', 'fast': True, 'source': '0x4A7', 'group': '0x4A7 Temp extremes', 'range': (-40, 160)},
        {'key': 'temp_min', 'label': 'Min pack temperature', 'unit': '°F', 'fast': True, 'source': '0x4A7', 'group': '0x4A7 Temp extremes', 'range': (-40, 160)},
        {'key': 'charge_permission_input', 'label': 'Charge permission input (interlock)', 'unit': '', 'fast': True, 'source': '0x358', 'group': '0x358 Charge interlock', 'range': (0, 1)},
        {'key': 'soc_pct', 'label': 'State of charge', 'unit': '%', 'fast': False, 'source': 'DID 0x1F5B', 'group': 'DID 0x1F5B SoC (slow)', 'range': (0, 100)},
        {'key': 'capacity_pack1_ah', 'label': 'Pack 1 capacity/SOH', 'unit': 'Ah', 'fast': False, 'source': 'DID 0x1D3E', 'group': 'DID 0x1D3E capacity (very slow)', 'range': (0, 210)},
        {'key': 'capacity_pack2_ah', 'label': 'Pack 2 capacity/SOH', 'unit': 'Ah', 'fast': False, 'source': 'DID 0x1D3E', 'group': 'DID 0x1D3E capacity (very slow)', 'range': (0, 210)},
        {'key': 'capacity_pack3_ah', 'label': 'Pack 3 capacity/SOH', 'unit': 'Ah', 'fast': False, 'source': 'DID 0x1D3E', 'group': 'DID 0x1D3E capacity (very slow)', 'range': (0, 210)},
        {'key': 'capacity_pack4_ah', 'label': 'Pack 4 capacity/SOH', 'unit': 'Ah', 'fast': False, 'source': 'DID 0x1D3E', 'group': 'DID 0x1D3E capacity (very slow)', 'range': (0, 210)},
        {'key': 'primary_pack_v', 'label': 'Primary voltage reference (cross-check)', 'unit': 'V', 'fast': False, 'source': 'DID 0x1F9A', 'group': 'DID 0x1F9A primary V/I (slow)', 'range': (300, 420)},
        {'key': 'primary_current_a', 'label': 'Primary current reference (cross-check)', 'unit': 'A', 'fast': False, 'source': 'DID 0x1F9A', 'group': 'DID 0x1F9A primary V/I (slow)', 'range': (-220, 220)},
    ]
    for cell in range(1, 97):
        reg.append({'key': f'cell_{cell:02d}', 'label': f'Cell {cell} voltage',
                    'unit': 'V', 'fast': True, 'source': '0x4A9/0x4C0', 'group': '0x4A9/0x4C0 per-cell voltages (96)',
                    'range': (2.5, 5.0)})
    for probe in range(1, 17):
        reg.append({'key': f'temp_{probe:02d}', 'label': f'Temp probe {probe}',
                    'unit': '°F', 'fast': True, 'source': '0x4AA', 'group': '0x4AA per-probe temps (16)',
                    'range': (-40, 160)})
    return reg


INPUT_SIGNALS = _build_input_registry()
INPUT_SIGNAL_KEYS = {s['key'] for s in INPUT_SIGNALS}


def cell_voltage_keys():
    return [f'cell_{i:02d}' for i in range(1, 97)]


def temp_probe_keys():
    return [f'temp_{i:02d}' for i in range(1, 17)]
