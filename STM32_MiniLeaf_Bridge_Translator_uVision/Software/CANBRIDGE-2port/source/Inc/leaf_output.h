/* Nissan Leaf HVBAT frame builders + output state (STM32 port Phase 5).
 *
 * Ports bridge/leaf_signals.py's DEFAULTS/RANGES/clamp_state() and its
 * frame-builder functions (build_1db, build_1dc, etc.), which are
 * themselves ported VERBATIM (byte-for-byte) from
 * Refrance/Leaf_BMS_Emulator/leaf_hvbat_emulator.py - do not "simplify" or
 * re-derive any bit-packing formula here; every shift/mask below must match
 * the Python source exactly. See docs/03-target-signals-leaf.md.
 *
 * This struct is also what bridge/management_engine.py's C port
 * (management_engine.c) reads a baseline from and writes its safety
 * overrides into - LeafState IS the C port's equivalent of the Python
 * app's `leaf_state` dict passed through DEFAULTS -> mapping_engine.apply()
 * -> management_engine.apply() -> clamp_state().
 */
#ifndef LEAF_OUTPUT_H
#define LEAF_OUTPUT_H

#include <stdint.h>

/* Every Leaf output field this bridge produces, one struct field per
 * SLIDERS/CHECKS/ZE1_62_SLIDERS entry in leaf_signals.py. All floats,
 * even the field's conceptually integer ones (relay_cut_request, gids,
 * dtc, ...) - matches Python treating every DEFAULTS/RANGES value
 * uniformly as a number; each frame builder does its own (int)/round()
 * cast and bitmask, exactly like the Python source. */
typedef struct {
    /* 0x1DB - Battery status */
    float pack_voltage_v;
    float pack_current_a;
    float usable_soc;
    float failsafe_status;
    float relay_cut_request;
    float discharge_pwr_sts;

    /* 0x1DC - Power limits */
    float discharge_limit_kw;
    float charge_limit_kw;
    float charger_limit_kw;
    float charge_pwr_sts;

    /* 0x55B - Fine SOC */
    float fine_soc_pct;

    /* 0x5BC - Display / SOH */
    float soh_pct;
    float gids;
    float capacity_bars_raw;
    float pwr_limit_reason;
    float temp_segment_pct;

    /* 0x59E - Quick-charge capacity */
    float qc_full_wh;
    float qc_remain_wh;
    float soc_correction;

    /* 0x5C0 - History data */
    float batt_temp_c;
    float dtc;

    /* Flags (0x1DB/0x55B) */
    float main_relay_on;
    float interlock;
    float full_charge_flag;
    float ir_malfunction;
    float capacity_empty;

    /* 0x1ED - ZE1 62kWh charger-limit field only */
    float chg2_limit_kw;
} LeafState;

/* Fills every field with its DEFAULTS value (bridge/leaf_signals.py's
 * DEFAULTS dict) - the starting point every tick's composition begins from,
 * same as the Python app's `dict(leaf_signals.DEFAULTS)`. */
void leaf_state_defaults(LeafState *s);

/* Clamps every field to its documented encodable (lo, hi) range in place -
 * MUST run immediately before frame-building, every tick, no exceptions.
 * The frame builders below bitmask-pack values into fixed-width CAN fields,
 * which WRAPS an out-of-range value instead of saturating it (confirmed
 * real failure mode: a negative discharge_limit_kw wrapped to a raw value
 * that decoded back to 251.0kW - full power, the opposite of the safety
 * intent). Returns nonzero if anything was actually out of range and got
 * clamped (informational only - firmware has no log to write this to, but a
 * future debug build can check this). */
uint8_t leaf_state_clamp(LeafState *s);

/* ── Nissan CRC-8 (poly 0x85, init 0, over bytes 0-6) ─────────────────── */
uint8_t leaf_crc8(const uint8_t *data7, uint8_t len);

/* ── Frame builders - every one returns the number of bytes written into
 * `out` (the frame's DLC). `prun` is the free-running 2-bit counter,
 * `latch` the free-running voltage-latch toggle bit, `tick10` the free-
 * running 10ms counter used to index the opaque replay cycles - all three
 * owned/incremented by bridge_sequencer.c, passed in here rather than
 * managed internally, matching the Python builders' own pure-function
 * signature (they take prun/latch/n as parameters, never read global
 * counters themselves). ────────────────────────────────────────────────── */
uint8_t leaf_build_1db(const LeafState *s, uint8_t prun, uint8_t latch, uint8_t out[8]);
uint8_t leaf_build_1db_startup(const LeafState *s, uint8_t prun, uint32_t t_ms, uint8_t out[8]);
/* code_1dc_override: NULL = use BRIDGE_PROTO_CODE_1DC[prun] (default,
 * verbatim behavior); else a caller-owned 3-byte array substituted in its
 * place (used when BRIDGE_CFG_SEND_CODE_1DC is 0 - a neutral {0,0,0}). */
uint8_t leaf_build_1dc(const LeafState *s, uint8_t prun, uint8_t uprate,
                       const uint8_t *code_1dc_override, uint8_t out[8]);
uint8_t leaf_build_1dc_startup(uint8_t prun, uint8_t first_frame, uint8_t out[8]);
uint8_t leaf_build_1c2(uint8_t tick10, uint8_t out[8]);
uint8_t leaf_build_1ed(const LeafState *s, uint8_t prun, uint8_t out[8]);
uint8_t leaf_build_55b(const LeafState *s, uint8_t prun, uint16_t ir_raw, uint8_t refuse_sleep, uint8_t out[8]);
/* gids_raw_override: -1 = compute from s->gids (default); else a raw 10-bit
 * value substituted directly (used for the startup placeholder). */
uint8_t leaf_build_5bc(const LeafState *s, uint8_t tick10, int16_t gids_raw_override,
                       const uint8_t *chg_time_override, uint8_t out[8]);
uint8_t leaf_build_5bc_first(const LeafState *s, uint8_t tick10, uint8_t out[8]);
uint8_t leaf_build_59e(const LeafState *s, uint8_t out[8]);
uint8_t leaf_build_5c0(const LeafState *s, uint8_t tick10, const uint8_t *hist_override, uint8_t out[8]);
uint8_t leaf_build_5eb(uint8_t tick10, uint8_t out[8]);

#endif /* LEAF_OUTPUT_H */
