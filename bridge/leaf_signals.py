"""Nissan Leaf HVBAT target signals: CAN IDs, frame builders, opaque replay
tables, and mapping-target metadata.

Frame builders and constant tables below are ported VERBATIM (byte-for-byte)
from Refrance/Leaf_BMS_Emulator/leaf_hvbat_emulator.py (rev 43), per this
project's own CLAUDE.md instruction not to re-derive real-hardware-confirmed
byte formulas by hand. See docs/03-target-signals-leaf.md for the source
citation and docs/07-startup-shutdown-plan.md for the timing this feeds.
"""
import re

# ── HVBAT CAN IDs, gated by car/battery generation ──────────────────────────
HVBAT_IDS_BASE = {0x1DB, 0x1DC, 0x55B, 0x5BC, 0x59E, 0x5C0}
HVBAT_ID_1C2 = 0x1C2   # ZE1 only (any capacity)
HVBAT_ID_1ED = 0x1ED   # ZE1 62 kWh only, UNVERIFIED upstream
HVBAT_ID_5EB = 0x5EB   # ZE1 only (any capacity)

# TX period per ID, milliseconds - the real-time engine sends every one of
# these on a fixed wall-clock schedule (docs/06-realtime-engine-and-watchdog.md).
TX_PERIOD_MS = {
    0x1DB: 10, 0x1DC: 10, 0x1C2: 10, 0x1ED: 10,
    0x55B: 100, 0x5BC: 100,
    0x59E: 500, 0x5C0: 500, 0x5EB: 500,
}


def hvbat_ids_for(battery_gen, battery_kwh):
    ids = set(HVBAT_IDS_BASE) | {HVBAT_ID_1C2 if battery_gen == 'ZE1' else None}
    ids.discard(None)
    if battery_gen == 'ZE1':
        ids.add(HVBAT_ID_5EB)
        if battery_kwh == 62:
            ids.add(HVBAT_ID_1ED)
    return ids


# ── Nissan CRC-8 (poly 0x85, init 0, over bytes 0-6) ────────────────────────
_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = ((_c << 1) ^ 0x85) & 0xFF if _c & 0x80 else (_c << 1) & 0xFF
    _CRC_TABLE.append(_c)


def crc8(data7):
    crc = 0
    for b in data7:
        crc = _CRC_TABLE[crc ^ b]
    return crc


# ── Mapping-target signal metadata: (key, label, min, max, step, default) ──
# Grouped by CAN message purely for GUI display; the mapping engine flattens
# this into one registry. See docs/03-target-signals-leaf.md.
SLIDERS = {
    '0x1DB - Battery status (10 ms)': [
        ('pack_voltage_v',   'Pack voltage (V)',            0,   450,   0.5, 378.5),
        ('pack_current_a',   'Pack current (A)',           -400, 200,   0.5, 0.0),
        ('usable_soc',       'Usable SOC (%)',              0,   100,   1,   45),
        ('failsafe_status',  'Failsafe status (raw, 0=normal)', 0, 7,   1,   0),
        ('relay_cut_request','Relay cut request (0=none)',  0,   3,     1,   0),
        ('discharge_pwr_sts','Discharge power status',      0,   3,     1,   1),
    ],
    '0x1DC - Power limits (10 ms)': [
        ('discharge_limit_kw', 'Discharge power limit (kW)', 0, 255.75, 0.25, 110.0),
        ('charge_limit_kw',    'Charge/regen power limit (kW)', 0, 255.75, 0.25, 70.0),
        ('charger_limit_kw',   'Max power for charger (kW)', -10, 92.3, 0.1, 92.3),
        ('charge_pwr_sts',     'Charge power status',        0,  3,      1,   1),
    ],
    '0x55B - Fine SOC (100 ms)': [
        ('fine_soc_pct', 'Fine SOC (%)', 0, 102.3, 0.1, 46.3),
    ],
    '0x5BC - Display / SOH (100 ms)': [
        ('soh_pct',        'SOH / capacity deterioration (%)', 0, 127, 1, 100),
        ('gids',           'Remaining capacity (GIDS)',        0, 1023, 1, 120),
        ('capacity_bars_raw', 'ChargeBars/CapacityBars raw (0-15)', 0, 15, 1, 6),
        ('pwr_limit_reason','Output power limit reason (0=none)', 0, 7, 1, 0),
        ('temp_segment_pct','Dash temperature segment (%)',    0, 100, 0.1, 58.3),
    ],
    '0x59E - Quick-charge capacity (500 ms)': [
        ('qc_full_wh',    'QC full capacity (Wh)',      0, 51100, 100, 23000),
        ('qc_remain_wh',  'QC remaining capacity (Wh)', 0, 51100, 100, 10500),
        # Range/default confirmed on real hardware 2026-07-31 (user's own
        # Leaf + RZ450e bench pack): raw 0-200 = 0-100% dash display, 2 raw
        # counts per percent - was previously an unconfirmed 0-255/241
        # placeholder (241 raw would be an impossible 120.5% under the now-
        # confirmed formula). Default 90 matches usable_soc/fine_soc_pct's
        # own ~45% default for consistency across the SoC-family fields.
        ('soc_correction','Dash SOC % display (raw byte, 2 counts/%, confirmed)', 0, 200, 1, 90),
    ],
    '0x5C0 - History data (500 ms)': [
        ('batt_temp_c', 'Battery temperature (degC)', -40, 86, 1, 37),
        ('dtc',         'Diagnosis trouble code (raw byte)', 0, 255, 1, 0),
    ],
}

CHECKS = [  # (key, label, default) - soft/hard cut and permission flags
    ('main_relay_on',   'Main relay ON permission (0x1DB)', 1),
    ('interlock',       'Inter-lock connected (0x1DB)',     1),
    ('full_charge_flag','Full charge flag (0x1DB) - SOFT CUT: instant charge stop', 0),
    ('ir_malfunction',  'IR sensor malfunction (0x55B)',    0),
    ('capacity_empty',  'Capacity empty flag (0x55B) - SOFT CUT: instant contactor cutoff', 0),
]
# 'voltage_latch' removed as a mapping target (2026-08-01, item 12.5, user
# decision): build_1db() never read s['voltage_latch'] at all, so mapping
# anything to it in the GUI had zero effect - the real toggle-bit gating it
# was meant to provide already lives in GENERATED_SIGNALS' own
# 'voltage_latch_toggle' checkbox (see realtime_engine.py's _build_frame),
# which is the actual live equivalent of the original Leaf_BMS_Emulator's
# `latch if s['voltage_latch'] else 0` gate - no functionality lost.

ZE1_62_SLIDERS = [
    ('chg2_limit_kw', '0x1ED charger-limit field (kW) [UNVERIFIED upstream]',
     10, 204, 0.1, 10.0),
]

CHARGE_CHECKS = [
    # Default flipped 0->1 (user edit, 2026-08-01): "the charge option
    # should be set to on as default."
    ('charge_emulate', 'Emulate charger request (0x1DC ramp)', 1),
    # AC-charger overvoltage taper enable (added 2026-08-01, split out of
    # bridge/management_engine.py's charge_target_taper - see its
    # default_config() comment). Default ON, matching this project's "ship
    # enabled, not blank" philosophy (docs/05) for anything safety-relevant.
    ('ac_taper_enabled', 'AC charger overvoltage taper enabled', 1),
    # Moved here from management_engine.py's charge_target_taper
    # (2026-08-01 split) - only ever mattered while actually plugged in, so
    # it belongs with the rest of the charger-specific controls.
    ('extended_mode', 'Extended mode active (road trip - charge to extended target)', 0),
    # Added 2026-08-03 (docs/13 item 13.1, user directive), REWORKED
    # 2026-08-03 same day after user clarification: driving is allowed to
    # run on cached/last-known-good values until the general 60s/+5s
    # staleness watchdog would object (docs/06 section 3) - charging must
    # NOT start on cached/default values at all, full stop. The only
    # difference from driving is this startup gate; once genuinely live
    # data has arrived and the ramp is running, ongoing protection is the
    # SAME staleness watchdog driving gets, not a separate/stricter timer
    # (a separate custom threshold was the first version of this fix and
    # was explicitly rejected - "it's the same safety and data validation
    # as driving, just with a different startup"). Default ON, matching
    # this project's "ship enabled" philosophy for anything safety-relevant.
    ('require_live_data_to_charge',
     'Require genuinely live (not cached/default) battery data before the charge ramp can start', 1),
]
CHARGE_SLIDERS = [
    ('charge_target_kw', 'Charger ramp target (kW)', 0, 92.2, 0.1, 92.2),
    ('chg_uprate_level', 'Uprate level / ramp rate (0-7)', 0, 7, 1, 7),
    # AC-charger-specific per-cell overvoltage taper (added 2026-08-01,
    # split from management_engine.py's charge_target_taper - user
    # directive: AC charging (~19A/0.09C) and regen (up to ~0.5C) are
    # physically very different, so they get independently-tunable curves).
    # Defaulted to the same starting values as the regen taper - not yet
    # independently tuned, just no longer forced to share one curve.
    ('ac_full_v', 'AC charge full power below (V/cell)', 0, 5.0, 0.01, 4.00),
    ('ac_zero_v', 'AC charge zero power at/above (V/cell)', 0, 5.0, 0.01, 4.15),
    # ac_emergency_v 4.30->4.20V (user edit, 2026-08-03, docs/13 item 15.4) -
    # matches charge_target_taper's regen-side emergency_high_v, set to the
    # standard NMC charge ceiling exactly (docs/05's researched 4.20V).
    ('ac_emergency_v', 'AC charge emergency high V (hard cut)', 0, 5.0, 0.01, 4.20),
    # Moved here from management_engine.py's charge_target_taper
    # (2026-08-01 split) - the AC daily/extended SoC target only ever
    # mattered while actually plugged in (gated on charge_permission_input).
    ('daily_target_pct', 'Daily target % (SoC stop point)', 0, 100, 1, 80.0),
    ('extended_target_pct', 'Extended target % (SoC stop point)', 0, 100, 1, 100.0),
]
# (lo, hi) numeric bounds per charge_emulation field, derived straight from
# CHARGE_SLIDERS' own (lo, hi) columns (added 2026-08-03, docs/13 items
# 13.3/13.9) - shared by BOTH gui/panels.py's ChargeEmulationPanel (clamps
# on every keystroke) and config_profile.py's profile-loading path (clamps
# on every load), so a hand-edited or corrupted profile.json can't set an
# AC-charger/regen threshold outside what the GUI itself would ever allow.
CHARGE_EMULATION_BOUNDS = {k: (lo, hi) for k, _, lo, hi, _, _ in CHARGE_SLIDERS}

# Live values for the three fields above are read from
# SharedState.charge_emulation (seeded from these same defaults), not from
# `leaf_state`/DEFAULTS - see bridge/state.py and RealtimeEngine's
# _apply_charge_ramp() (docs/06). They're user-adjustable EMULATION CONTROL
# knobs, not signals that go on the wire themselves - only their effect
# (the ramped charger_limit_kw value and the LB_BPCMAX_UPRATE bits) does.

# Charger-request ramp constants (ported 2026-07-31, implemented alongside
# docs/06's charge-ramp feature) - confirmed against real hardware by
# Leaf_BMS_Emulator (bit-level diff of every HVBAT ID across idle vs. real
# charge-session captures): while a charge request is active,
# LB_MAX_POWER_FOR_CHARGER snaps from the 1023 idle placeholder to 0.0 kW
# and ramps up at 2 kW/s (at uprate level 7, halving per level down) until
# it reaches the configured ramp target, overriding the static "Max power
# for charger" value for the duration; at charge end both snap back to idle
# instantly.
CHG_IDLE_RAW = 1023        # LB_MAX_POWER_FOR_CHARGER idle/not-limiting value
CHG_RAMP_START_RAW = 100   # = 0.0 kW, the observed real-battery ramp start
CHG_RAMP_RAW_PER_S = 20.0  # raw counts/sec at uprate level 7 (= 2.0 kW/s); each level down halves the rate

DEFAULTS = {k: d for grp in SLIDERS.values() for (k, _, _, _, _, d) in grp}
DEFAULTS.update({k: d for (k, _, d) in CHECKS})
DEFAULTS.update({k: d for (k, _, _, _, _, d) in CHARGE_SLIDERS})
DEFAULTS.update({k: d for (k, _, d) in CHARGE_CHECKS})
DEFAULTS.update({k: d for (k, _, _, _, _, d) in ZE1_62_SLIDERS})

# Documented encodable (lo, hi) range per field - the frame builders below
# pack values into fixed-width CAN fields with a bitmask (e.g. `& 0x3FF`),
# which WRAPS a value outside range instead of saturating it (confirmed
# 2026-07-31: discharge_limit_kw=-5.0, which should mean "no discharge
# power," wraps to a raw value that decodes back to 251.0 kW - full power,
# the opposite of the safety intent; a mapping-derived qc_full_wh/gids can
# also legitimately exceed its 10-bit range under realistic pack values,
# e.g. a 200Ah/400V pack computes qc_full_wh=80000 against a documented max
# of 51100). `clamp_state()` is the single choke point that guarantees
# nothing outside this range ever reaches the CAN bus - see docs/06's
# real-time engine section and docs/11's verification checklist.
RANGES = {k: (lo, hi) for grp in SLIDERS.values() for (k, _, lo, hi, _, _) in grp}
RANGES.update({k: (0, 1) for (k, _, _) in CHECKS})
RANGES.update({k: (lo, hi) for (k, _, lo, hi, _, _) in CHARGE_SLIDERS})
RANGES.update({k: (0, 1) for (k, _, _) in CHARGE_CHECKS})
RANGES.update({k: (lo, hi) for (k, _, lo, hi, _, _) in ZE1_62_SLIDERS})
# nameplate_ah/ZE1_62_SLIDERS/CHARGE_SLIDERS keys aren't reachable from the
# mapping/management layers today (no tie or feature currently targets
# them), but are included for defense in depth if that ever changes.


def clamp_state(s):
    """Clamp every field to its documented encodable range before frame-
    building. Returns (clamped_state, clamped_keys) - clamped_keys is a list
    of (key, original_value, clamped_value) for anything actually out of
    range, so the caller can log it: a value needing to be clamped means
    something upstream (a user's mapping tie, a derived-signal formula) is
    producing an out-of-spec number, which should be visible, not silently
    absorbed."""
    out = dict(s)
    clamped = []
    for key, (lo, hi) in RANGES.items():
        v = out.get(key)
        if v is None:
            continue
        if v < lo:
            clamped.append((key, v, lo))
            out[key] = lo
        elif v > hi:
            clamped.append((key, v, hi))
            out[key] = hi
    return out, clamped


# ── Flattened output registry (for the mapping GUI's output dropdown and the
# dashboard) - 'source' is the CAN ID this signal is carried on, so it's easy
# to tell which message a given output stands for (docs/08). ───────────────
def _build_output_registry():
    reg = []
    for group, sigs in SLIDERS.items():
        source = group.split(' ')[0]
        for key, label, lo, hi, step, default in sigs:
            reg.append({'key': key, 'label': label, 'lo': lo, 'hi': hi, 'step': step,
                        'default': default, 'source': source, 'group': group})
    for key, label, default in CHECKS:
        m = re.search(r'\((0x[0-9A-Fa-f]+)', label)
        source = m.group(1) if m else '?'
        reg.append({'key': key, 'label': label, 'lo': 0, 'hi': 1, 'step': 1,
                    'default': default, 'source': source, 'group': 'Flags'})
    return reg


OUTPUT_SIGNALS = _build_output_registry()

# Two-tier hard-cut relay signal, not itself in SLIDERS/CHECKS above since it's
# driven exclusively by the battery-management layer, never a direct mapping
# target: relay_cut_request (0x1DB) doubles as the HARD CUT signal when set
# non-zero by an emergency-tier protection feature (docs/05).

# ── Opaque generated tables/counters - never mapping targets, GUI checkbox
# controls whether each is actually sent (default checked). ────────────────
GENERATED_SIGNALS = [
    ('prun', 'PRUN counter (2-bit, packed into 0x1DB/0x1DC/0x55B/0x1ED)', True),
    ('voltage_latch_toggle', 'Voltage latch toggle bit (0x1DB byte 3 bit 0)', True),
    ('heartbeat_1c2', '0x1C2 heartbeat byte', True),
    ('code_1dc', '0x1DC bytes 4-6 opaque cycle', True),
    ('chg_time_5bc', '0x5BC bytes 5-7 charge-time cycle', True),
    ('hist_5c0', '0x5C0 mux + bytes 3-5 history cycle', True),
    ('seq_5eb', '0x5EB full-frame 45-step cycle (ZE1 only)', True),
]

CODE_1DC = [(0x0C, 0xD8, 0xE4), (0x01, 0x14, 0xD4), (0x04, 0xE0, 0xC8), (0x08, 0xC0, 0xC0)]

CHG_TIME_5BC = [(0x00, 0x1F, 0xFF), (0x00, 0xA0, 0x67), (0x01, 0x00, 0xE9),
                (0x01, 0x61, 0x7A), (0x02, 0x40, 0x33), (0x02, 0xA0, 0x74),
                (0x03, 0x00, 0xBF)]

HIST5C0 = {
    1: (0x00, 0x20, 0xC8),
    2: (0x00, 0x00, 0xC4),
    3: (0x00, 0x00, 0x00),
    4: (0x00, 0x00, 0x94),
    5: (0x00, 0x00, 0x7C),
    6: (0x01, 0xC8, 0x6C),
}

SEQ_5EB = [
    (0x26, 0x01, 0x00, 0x01, 0x07, 0x0E, 0xC0, 0x33),
    (0x26, 0x11, 0x00, 0x01, 0x07, 0x0E, 0xC0, 0x3B),
    (0x26, 0x20, 0x3D, 0x3E, 0x3D, 0x21, 0x40, 0x00),
    (0x26, 0x30, 0x3D, 0x3E, 0x3D, 0x21, 0x40, 0x4D),
    (0x26, 0x42, 0x7D, 0x7E, 0x7D, 0x3B, 0x80, 0x00),
    (0x26, 0x50, 0x00, 0x12, 0x48, 0x00, 0x00, 0x00),
    (0x26, 0x60, 0x7D, 0x7E, 0x7D, 0x00, 0x00, 0x00),
    (0x26, 0x70, 0x0F, 0x31, 0x23, 0x08, 0xC0, 0x23),
    (0x26, 0x80, 0x3E, 0x3E, 0x3D, 0x00, 0x00, 0x00),
    (0x46, 0x01, 0x00, 0x01, 0x07, 0x0E, 0xC0, 0x33),
    (0x46, 0x11, 0x00, 0x01, 0x08, 0x00, 0x00, 0x0A),
    (0x46, 0x20, 0x3D, 0x3E, 0x3D, 0x21, 0x40, 0x00),
    (0x46, 0x30, 0x3D, 0x3E, 0x3D, 0x21, 0x40, 0x3B),
    (0x46, 0x42, 0x7D, 0x7E, 0x7D, 0x3B, 0x80, 0x00),
    (0x46, 0x50, 0x00, 0x12, 0x48, 0x00, 0x00, 0x00),
    (0x46, 0x60, 0x7D, 0x7F, 0x7C, 0x00, 0x00, 0x00),
    (0x46, 0x70, 0x4A, 0x4F, 0x23, 0x08, 0xC0, 0x23),
    (0x46, 0x80, 0x3E, 0x3E, 0x3D, 0x00, 0x00, 0x00),
    (0x66, 0x02, 0x00, 0x0D, 0x13, 0x00, 0x80, 0x16),
    (0x66, 0x12, 0x00, 0x0D, 0x13, 0x0E, 0x40, 0x30),
    (0x66, 0x20, 0x3F, 0x40, 0x3F, 0x14, 0x40, 0x00),
    (0x66, 0x30, 0x55, 0x57, 0x54, 0x76, 0x01, 0x49),
    (0x66, 0x42, 0x75, 0x76, 0x75, 0x7C, 0x00, 0x00),
    (0x66, 0x50, 0x50, 0x01, 0x13, 0x00, 0x00, 0x00),
    (0x66, 0x60, 0xEA, 0xEC, 0xE9, 0x00, 0x00, 0x00),
    (0x66, 0x70, 0x01, 0x4F, 0x5E, 0x10, 0x80, 0x18),
    (0x66, 0x80, 0x50, 0x57, 0x3F, 0x00, 0x00, 0x00),
    (0x86, 0x02, 0x00, 0x11, 0x09, 0x04, 0x40, 0x37),
    (0x86, 0x12, 0x00, 0x11, 0x0C, 0x0D, 0x80, 0x32),
    (0x86, 0x20, 0x44, 0x46, 0x43, 0x37, 0x00, 0x00),
    (0x86, 0x30, 0x46, 0x47, 0x45, 0x44, 0xC0, 0x0B),
    (0x86, 0x40, 0x88, 0x89, 0x88, 0x03, 0x40, 0x00),
    (0x86, 0x50, 0x0B, 0x10, 0x11, 0x00, 0x00, 0x00),
    (0x86, 0x60, 0x9D, 0x9E, 0x9B, 0x00, 0x00, 0x00),
    (0x86, 0x70, 0x19, 0x06, 0x3B, 0x0D, 0x80, 0x31),
    (0x86, 0x80, 0x46, 0x47, 0x43, 0x00, 0x00, 0x00),
    (0xA6, 0x02, 0x00, 0x11, 0x0F, 0x06, 0x80, 0x0C),
    (0xA6, 0x12, 0x00, 0x12, 0x07, 0x0D, 0xC0, 0x11),
    (0xA6, 0x20, 0x4A, 0x4A, 0x4A, 0x32, 0x00, 0x00),
    (0xA6, 0x30, 0x48, 0x49, 0x47, 0x6F, 0x00, 0x0B),
    (0xA6, 0x40, 0x85, 0x86, 0x84, 0x03, 0x40, 0x00),
    (0xA6, 0x50, 0x31, 0x02, 0x03, 0x00, 0x00, 0x00),
    (0xA6, 0x60, 0xDD, 0xDF, 0xDB, 0x00, 0x00, 0x00),
    (0xA6, 0x70, 0x3E, 0x47, 0x59, 0x11, 0x00, 0x2E),
    (0xA6, 0x80, 0x4A, 0x4C, 0x47, 0x00, 0x00, 0x00),
]

# Startup phase boundaries, ms from HVBAT stream start (docs/07).
T_1DB_START, T_55B_START, T_59E_START = 65, 155, 565
T_PH_B, T_PH_C, T_VALID, T_RUNNING = 195, 325, 865, 2105

# Shutdown staging, ms from wind-down trigger (docs/07).
PWRDOWN_STAGE2_MS = 250
PWRDOWN_STAGE3_MS = 300
PWRDOWN_STAGE4_MS = 1200
# Genuine bus-quiet duration required before re-arming to wait for the next
# wake (bridge/realtime_engine.py's ShutdownSequencer.tick(), 'stopped'
# phase) - matches Leaf_BMS_Emulator's own PWRDOWN_DEFAULT_COOLDOWN_S /
# `_wait_for_genuine_quiet`, confirmed against a real capture where a fixed
# post-stop timer (not an actual quiet check) let an already-in-flight VCM
# frame instantly re-trigger the wake detector. (The reference project also
# has a separate BUS_QUIET_TIMEOUT_S for detecting when its SOURCE battery
# goes quiet mid-session - this project's equivalent of that concern is the
# staleness watchdog, docs/06 section 3, not a raw last-RX-time check, so no
# second constant is needed here.)
PWRDOWN_DEFAULT_COOLDOWN_S = 1.0

IGNITION_IDS = {0x108, 0x1CB, 0x284}
IGNITION_QUIET_S = 0.5
IGNITION_OFF_DELAY_S = 10.0
IGNITION_GRACE_S = 10.0
CHG_END_STOP_S = 3.0
CHG_STALL_TIMEOUT_S = 15.0
CHG_CMD_IDLE = 100
CHG_CMD_FRESH_S = 0.5


# ── Frame builders (byte-verified against real captures, ported verbatim) ──
def build_1db(s, prun, latch):
    raw_i = int(round(s['pack_current_a'] * 2)) & 0x7FF
    raw_v = int(round(s['pack_voltage_v'] * 2)) & 0x3FF
    b = [
        (raw_i >> 3) & 0xFF,
        ((raw_i & 7) << 5) | ((int(s['relay_cut_request']) & 3) << 3)
            | (int(s['failsafe_status']) & 7),
        raw_v >> 2,
        ((raw_v & 3) << 6) | (int(s['main_relay_on']) << 5)
            | (int(s['full_charge_flag']) << 4) | (int(s['interlock']) << 3)
            | ((int(s['discharge_pwr_sts']) & 3) << 1) | latch,
        int(s['usable_soc']) & 0x7F,
        0x00,
        prun,
    ]
    return bytes(b + [crc8(b)])


def build_1db_startup(s, prun, t_ms):
    soc = int(s['usable_soc']) & 0x7F
    if t_ms < T_PH_B:
        b = [0x7F, 0xE0, 0xFF, 0xC6, soc, 0x00, prun]
    elif t_ms < T_PH_C:
        b = [0xFF, 0xE0, 0xFF, 0xE7, soc, 0x00, prun]
    else:
        b = [0x00, 0x00, 0xFF, 0xEF, 0x64, 0x00, prun]
    return bytes(b + [crc8(b)])


def build_1dc(s, prun, uprate=0, code_1dc_override=None):
    """code_1dc_override (added 2026-08-01, item 12.5): lets the caller
    substitute a neutral (0,0,0) placeholder for the real CODE_1DC opaque
    replay table when the GUI's 'code_1dc' Generated Signals checkbox is
    unchecked - default None preserves the exact original verbatim
    behavior, so nothing changes for the common case."""
    raw_d = int(round(s['discharge_limit_kw'] / 0.25)) & 0x3FF
    raw_c = int(round(s['charge_limit_kw'] / 0.25)) & 0x3FF
    raw_g = int(round((s['charger_limit_kw'] + 10) / 0.1)) & 0x3FF
    c4, c5, c6 = code_1dc_override if code_1dc_override is not None else CODE_1DC[prun]
    b = [
        raw_d >> 2,
        ((raw_d & 3) << 6) | ((raw_c >> 4) & 0x3F),
        ((raw_c & 0xF) << 4) | ((raw_g >> 6) & 0xF),
        ((raw_g & 0x3F) << 2) | (int(s['charge_pwr_sts']) & 3),
        c4 | ((uprate & 7) << 5), c5, c6 | prun,
    ]
    return bytes(b + [crc8(b)])


def build_1dc_startup(prun, first_frame=False):
    if first_frame:
        b = [0xFF, 0xFF, 0xFF, 0xFF, 0x1F, 0xFF, 0xFC | prun]
    else:
        c4, c5, c6 = CODE_1DC[prun]
        b = [0xFF, 0xFF, 0xFF, 0xFF, c4, c5, c6 | prun]
    return bytes(b + [crc8(b)])


def build_1c2(n):
    return bytes([0x50 | (n & 0x0F)])


def build_1ed(s, prun):
    raw = int(round((s['chg2_limit_kw'] - 10) / 0.1)) & 0x7FF
    b = [(raw >> 3) & 0xFF, ((raw & 7) << 5) | (prun & 3)]
    return bytes(b + [crc8(b)])


def build_55b(s, prun, ir_raw=904, alu=0x55, refuse_sleep=0):
    raw_soc = int(round(s['fine_soc_pct'] * 10)) & 0x3FF
    b = [
        raw_soc >> 2,
        (raw_soc & 3) << 6,
        alu,
        0x00,
        ir_raw >> 2,
        ((ir_raw & 3) << 6) | (int(s['ir_malfunction']) & 1),
        (int(s['capacity_empty']) << 7) | ((refuse_sleep & 3) << 5) | 0x10 | prun,
    ]
    return bytes(b + [crc8(b)])


def build_5bc(s, n, gids_raw=None, chg_time_override=None):
    """chg_time_override (added 2026-08-01, item 12.5): same pattern as
    build_1dc's code_1dc_override - a neutral (0,0,0) placeholder for the
    'chg_time_5bc' checkbox, default None = original verbatim behavior."""
    gids = int(s['gids']) & 0x3FF if gids_raw is None else gids_raw
    toggle = 1 - ((n // 5) & 1)
    bars_raw = int(s['capacity_bars_raw']) & 0xF
    b5, b6, b7 = chg_time_override if chg_time_override is not None else CHG_TIME_5BC[n % 7]
    return bytes([
        gids >> 2,
        (gids & 3) << 6,
        (bars_raw << 4) | (0x0E if toggle else 0x04),
        int(round(s['temp_segment_pct'] / 0.4166666)) & 0xFF,
        ((int(s['soh_pct']) & 0x7F) << 1) | toggle,
        b5 | ((int(s['pwr_limit_reason']) & 7) << 5),
        b6, b7,
    ])


def build_5bc_first(s, n):
    toggle = 1 - ((n // 5) & 1)
    return bytes([0xFF, 0xC0, 0xFF, 0xFF,
                  ((int(s['soh_pct']) & 0x7F) << 1) | toggle, 0x03, 0xFF, 0xFF])


def build_59e(s):
    full = int(round(s['qc_full_wh'] / 100)) & 0x1FF
    rem = int(round(s['qc_remain_wh'] / 100)) & 0x1FF
    return bytes([
        0x00, 0x00,
        (full >> 4) & 0x1F,
        ((full & 0xF) << 4) | ((rem >> 5) & 0xF),
        (rem & 0x1F) << 3,
        0x00, 0x00,
        int(s['soc_correction']) & 0xFF,
    ])


def build_5c0(s, n, hist_override=None):
    """hist_override (added 2026-08-01, item 12.5): same pattern as
    build_1dc's code_1dc_override - a neutral (0,0,0) placeholder for the
    'hist_5c0' checkbox, default None = original verbatim behavior."""
    mux = (n % 6) + 1
    t = (int(s['batt_temp_c']) + 40) & 0x7F
    b3, b4, b5 = hist_override if hist_override is not None else HIST5C0[mux]
    return bytes([
        mux,
        t << 1,
        t << 1,
        b3,
        b4,
        b5,
        0x1F,
        int(s['dtc']) & 0xFF,
    ])


def build_5eb(n):
    return bytes(SEQ_5EB[n % len(SEQ_5EB)])
