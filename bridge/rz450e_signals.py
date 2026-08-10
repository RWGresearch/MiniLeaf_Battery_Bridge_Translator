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
# Reworked 2026-08-01 (user directive): the old DID_POLL_INTERVAL_S slept a
# flat 5.0s after EVERY request regardless of how fast the response actually
# came back, so any one specific DID was really only re-polled every ~15s
# (3 DIDs x 5s), not "roughly every 5s each" as the old name implied. Now:
# wait up to DID_RESPONSE_TIMEOUT_S for a response, then move to the next
# DID immediately - only a small DID_INTER_REQUEST_GAP_S pacing delay
# between requests, so a fast response doesn't also cost a needless extra
# wait, but the bus still isn't flooded with back-to-back requests.
DID_RESPONSE_TIMEOUT_S = 5.0
DID_INTER_REQUEST_GAP_S = 0.3


def toyota_sum_checksum(arb_id, data):
    """Confirmed 100%-match additive checksum, last byte of the frame."""
    n = len(data) - 1
    return ((arb_id >> 8) + (arb_id & 0xFF) + len(data)
             + sum(b for j, b in enumerate(data) if j != n)) & 0xFF


# IDs confirmed to carry the Toyota additive checksum in byte 7 (docs/02) -
# ID_TEMP_MINMAX/ID_CELLS_A/ID_CELLS_B/ID_TEMPS do NOT (confirmed 0% match
# upstream - byte 7 there is real payload data, not a checksum). Checked
# 2026-08-03 (docs/13 item 13.5, user directive): this project's own docs
# said this checksum "should" be used as an additional staleness/corruption
# check but it was never actually wired into the ingest path until now.
CHECKSUM_IDS = {ID_PACK_V, ID_CURRENT, ID_CHARGE_PERM, ID_ALIVE_3F1, ID_TICK_424}


def frame_checksum_ok(arb_id, data):
    """True if `arb_id` doesn't carry a Toyota additive checksum (nothing to
    check - most raw-CAN IDs this project decodes don't), or if it does and
    the checksum matches. False means either a checksum mismatch or a
    too-short frame on an ID that's supposed to have one - both are treated
    as corruption, not decoded at all, rather than handed to a decoder that
    would otherwise accept whatever bytes happen to fall in range."""
    if arb_id not in CHECKSUM_IDS:
        return True
    if len(data) < 8:
        return False
    return data[7] == toyota_sum_checksum(arb_id, data)


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
            # Raw byte is already 1°C/bit, 0 offset (2026-08-09: this project
            # now stores/displays every temperature in °C - previously
            # converted to °F here, the only place that conversion happened).
            out[f'temp_{probe:02d}'] = float(d[j])
    return out


def decode_temp_minmax(d):
    if len(d) < 4:
        return {}
    return {
        'temp_max': float(d[0]),
        'temp_min': float(d[1]),
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
        {'key': 'temp_max', 'label': 'Max pack temperature', 'unit': '°C', 'fast': True, 'source': '0x4A7', 'group': '0x4A7 Temp extremes', 'range': (-40, 71)},
        {'key': 'temp_min', 'label': 'Min pack temperature', 'unit': '°C', 'fast': True, 'source': '0x4A7', 'group': '0x4A7 Temp extremes', 'range': (-40, 71)},
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
                    'unit': '°C', 'fast': True, 'source': '0x4AA', 'group': '0x4AA per-probe temps (16)',
                    'range': (-40, 71)})
    return reg


INPUT_SIGNALS = _build_input_registry()
INPUT_SIGNAL_KEYS = {s['key'] for s in INPUT_SIGNALS}


def cell_voltage_keys():
    return [f'cell_{i:02d}' for i in range(1, 97)]


def temp_probe_keys():
    return [f'temp_{i:02d}' for i in range(1, 17)]


# ── Input plausibility validation (added 2026-08-01, user directive) ──────
# Deliberately generous physical-plausibility bounds - much wider than any
# operating/safety threshold in bridge/management_engine.py - meant only to
# reject obvious decode/bus garbage (a corrupted frame, a dropped byte) that
# would otherwise silently become the BMS's live input. NOT a substitute for
# the actual safety thresholds, and not meant to ever reject a real, if
# extreme, physical reading a healthy sensor could produce.
PLAUSIBLE_RANGES = {
    'pack_v': (0.0, 500.0),
    'cell_min': (0.50, 5.00),
    'cell_max': (0.50, 5.00),
    'current': (-210.0, 210.0),
    'current_b': (-210.0, 210.0),
    'temp_max': (-51.1, 121.1),
    'temp_min': (-51.1, 121.1),
    'soc_pct': (0.0, 100.0),
    'capacity_pack1_ah': (0.0, 300.0),
    'capacity_pack2_ah': (0.0, 300.0),
    'capacity_pack3_ah': (0.0, 300.0),
    'capacity_pack4_ah': (0.0, 300.0),
    'primary_pack_v': (0.0, 500.0),
    'primary_current_a': (-700.0, 700.0),
}
for _c in range(1, 97):
    PLAUSIBLE_RANGES[f'cell_{_c:02d}'] = (0.50, 5.00)
for _p in range(1, 17):
    PLAUSIBLE_RANGES[f'temp_{_p:02d}'] = (-51.1, 121.1)
del _c, _p


def validate_inputs(mapping):
    """Split a freshly-decoded {key: value} dict into (valid, rejected)
    against PLAUSIBLE_RANGES above. A key with no registered range (e.g.
    charge_permission_input, a 1-bit flag that can't be out of range by
    construction) always passes through unchanged. `valid` is safe to hand
    straight to SharedState.update_inputs(); `rejected` is for the caller to
    log - a rejected key is simply never written, so it keeps aging under
    whatever value (or None) it last had, and a signal that stays invalid
    long enough gets caught by the now-comprehensive staleness watchdog
    (bridge/management_engine.py) exactly like one that stopped arriving
    altogether - this is the intended mechanism, not a gap: a single
    rejected sample is quietly ignored, but sustained invalid data still
    surfaces as a real fault after the watchdog's window elapses."""
    valid, rejected = {}, {}
    for key, value in mapping.items():
        bounds = PLAUSIBLE_RANGES.get(key)
        if bounds is None or value is None:
            valid[key] = value
            continue
        lo, hi = bounds
        # try/except (added 2026-08-03, docs/13 item 13.2): this function now
        # also validates disk-persisted cache data (config_profile.load_
        # last_known_good()), which - unlike a live CAN decode, which always
        # produces a float - could contain a corrupted/hand-edited non-
        # numeric value (a string, a list, etc.). A type that can't be
        # compared against the bounds is exactly as untrustworthy as one
        # that's out of range - reject it the same way instead of crashing.
        try:
            in_range = lo <= value <= hi
        except TypeError:
            rejected[key] = value
            continue
        if in_range:
            valid[key] = value
        else:
            rejected[key] = value
    return valid, rejected
