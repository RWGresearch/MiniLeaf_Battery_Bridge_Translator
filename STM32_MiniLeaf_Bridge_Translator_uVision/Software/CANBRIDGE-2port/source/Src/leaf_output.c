#include "leaf_output.h"        // This module's own prototypes/LeafState
#include "bridge_config_gen.h"  // BRIDGE_PROTO_DEFAULT_*/BRIDGE_PROTO_RANGE_*/opaque tables (generated, see tools/export_stm32_config.py)
#include <stddef.h>               // NULL - used directly below (no longer pulled in transitively; bridge_config_gen.h stopped including it once its own NULL usage moved to bridge_config_gen.c)

// Round-half-away-from-zero to the nearest int32 - matches Python's round()
// closely enough for real physical measurements (the two only differ at an
// exact .5 tie, which floating-point sensor data essentially never lands on
// exactly). Hand-written rather than pulling in <math.h>'s lroundf(), same
// "keep it small" reasoning as management_engine.c.
static int32_t iround(float v)
{
    return (int32_t)(v + (v >= 0.0f ? 0.5f : -0.5f));
}

void leaf_state_defaults(LeafState *s)
{
    s->pack_voltage_v    = BRIDGE_PROTO_DEFAULT_PACK_VOLTAGE_V;
    s->pack_current_a    = BRIDGE_PROTO_DEFAULT_PACK_CURRENT_A;
    s->usable_soc        = BRIDGE_PROTO_DEFAULT_USABLE_SOC;
    s->failsafe_status   = BRIDGE_PROTO_DEFAULT_FAILSAFE_STATUS;
    s->relay_cut_request = BRIDGE_PROTO_DEFAULT_RELAY_CUT_REQUEST;
    s->discharge_pwr_sts = BRIDGE_PROTO_DEFAULT_DISCHARGE_PWR_STS;

    s->discharge_limit_kw = BRIDGE_PROTO_DEFAULT_DISCHARGE_LIMIT_KW;
    s->charge_limit_kw    = BRIDGE_PROTO_DEFAULT_CHARGE_LIMIT_KW;
    s->charger_limit_kw   = BRIDGE_PROTO_DEFAULT_CHARGER_LIMIT_KW;
    s->charge_pwr_sts     = BRIDGE_PROTO_DEFAULT_CHARGE_PWR_STS;

    s->fine_soc_pct = BRIDGE_PROTO_DEFAULT_FINE_SOC_PCT;

    s->soh_pct           = BRIDGE_PROTO_DEFAULT_SOH_PCT;
    s->gids              = BRIDGE_PROTO_DEFAULT_GIDS;
    s->capacity_bars_raw = BRIDGE_PROTO_DEFAULT_CAPACITY_BARS_RAW;
    s->pwr_limit_reason  = BRIDGE_PROTO_DEFAULT_PWR_LIMIT_REASON;
    s->temp_segment_pct  = BRIDGE_PROTO_DEFAULT_TEMP_SEGMENT_PCT;

    s->qc_full_wh     = BRIDGE_PROTO_DEFAULT_QC_FULL_WH;
    s->qc_remain_wh   = BRIDGE_PROTO_DEFAULT_QC_REMAIN_WH;
    s->soc_correction = BRIDGE_PROTO_DEFAULT_SOC_CORRECTION;

    s->batt_temp_c = BRIDGE_PROTO_DEFAULT_BATT_TEMP_C;
    s->dtc         = BRIDGE_PROTO_DEFAULT_DTC;

    s->main_relay_on    = BRIDGE_PROTO_DEFAULT_MAIN_RELAY_ON;
    s->interlock        = BRIDGE_PROTO_DEFAULT_INTERLOCK;
    s->full_charge_flag = BRIDGE_PROTO_DEFAULT_FULL_CHARGE_FLAG;
    s->ir_malfunction   = BRIDGE_PROTO_DEFAULT_IR_MALFUNCTION;
    s->capacity_empty   = BRIDGE_PROTO_DEFAULT_CAPACITY_EMPTY;

    s->chg2_limit_kw = BRIDGE_PROTO_DEFAULT_CHG2_LIMIT_KW;
}

// One clamp per field, explicit rather than a generic table-driven loop
// (matches this project's "curated, not generic" convention) - every branch
// is identical in shape, just naming a different field/bound pair.
static uint8_t clampf_inplace(float *v, float lo, float hi)
{
    if (*v < lo) { *v = lo; return 1u; }
    if (*v > hi) { *v = hi; return 1u; }
    return 0u;
}

uint8_t leaf_state_clamp(LeafState *s)
{
    uint8_t any = 0u;
    any |= clampf_inplace(&s->pack_voltage_v, BRIDGE_PROTO_RANGE_PACK_VOLTAGE_V_LO, BRIDGE_PROTO_RANGE_PACK_VOLTAGE_V_HI);
    any |= clampf_inplace(&s->pack_current_a, BRIDGE_PROTO_RANGE_PACK_CURRENT_A_LO, BRIDGE_PROTO_RANGE_PACK_CURRENT_A_HI);
    any |= clampf_inplace(&s->usable_soc, BRIDGE_PROTO_RANGE_USABLE_SOC_LO, BRIDGE_PROTO_RANGE_USABLE_SOC_HI);
    any |= clampf_inplace(&s->failsafe_status, BRIDGE_PROTO_RANGE_FAILSAFE_STATUS_LO, BRIDGE_PROTO_RANGE_FAILSAFE_STATUS_HI);
    any |= clampf_inplace(&s->relay_cut_request, BRIDGE_PROTO_RANGE_RELAY_CUT_REQUEST_LO, BRIDGE_PROTO_RANGE_RELAY_CUT_REQUEST_HI);
    any |= clampf_inplace(&s->discharge_pwr_sts, BRIDGE_PROTO_RANGE_DISCHARGE_PWR_STS_LO, BRIDGE_PROTO_RANGE_DISCHARGE_PWR_STS_HI);

    any |= clampf_inplace(&s->discharge_limit_kw, BRIDGE_PROTO_RANGE_DISCHARGE_LIMIT_KW_LO, BRIDGE_PROTO_RANGE_DISCHARGE_LIMIT_KW_HI);
    any |= clampf_inplace(&s->charge_limit_kw, BRIDGE_PROTO_RANGE_CHARGE_LIMIT_KW_LO, BRIDGE_PROTO_RANGE_CHARGE_LIMIT_KW_HI);
    any |= clampf_inplace(&s->charger_limit_kw, BRIDGE_PROTO_RANGE_CHARGER_LIMIT_KW_LO, BRIDGE_PROTO_RANGE_CHARGER_LIMIT_KW_HI);
    any |= clampf_inplace(&s->charge_pwr_sts, BRIDGE_PROTO_RANGE_CHARGE_PWR_STS_LO, BRIDGE_PROTO_RANGE_CHARGE_PWR_STS_HI);

    any |= clampf_inplace(&s->fine_soc_pct, BRIDGE_PROTO_RANGE_FINE_SOC_PCT_LO, BRIDGE_PROTO_RANGE_FINE_SOC_PCT_HI);

    any |= clampf_inplace(&s->soh_pct, BRIDGE_PROTO_RANGE_SOH_PCT_LO, BRIDGE_PROTO_RANGE_SOH_PCT_HI);
    any |= clampf_inplace(&s->gids, BRIDGE_PROTO_RANGE_GIDS_LO, BRIDGE_PROTO_RANGE_GIDS_HI);
    any |= clampf_inplace(&s->capacity_bars_raw, BRIDGE_PROTO_RANGE_CAPACITY_BARS_RAW_LO, BRIDGE_PROTO_RANGE_CAPACITY_BARS_RAW_HI);
    any |= clampf_inplace(&s->pwr_limit_reason, BRIDGE_PROTO_RANGE_PWR_LIMIT_REASON_LO, BRIDGE_PROTO_RANGE_PWR_LIMIT_REASON_HI);
    any |= clampf_inplace(&s->temp_segment_pct, BRIDGE_PROTO_RANGE_TEMP_SEGMENT_PCT_LO, BRIDGE_PROTO_RANGE_TEMP_SEGMENT_PCT_HI);

    any |= clampf_inplace(&s->qc_full_wh, BRIDGE_PROTO_RANGE_QC_FULL_WH_LO, BRIDGE_PROTO_RANGE_QC_FULL_WH_HI);
    any |= clampf_inplace(&s->qc_remain_wh, BRIDGE_PROTO_RANGE_QC_REMAIN_WH_LO, BRIDGE_PROTO_RANGE_QC_REMAIN_WH_HI);
    any |= clampf_inplace(&s->soc_correction, BRIDGE_PROTO_RANGE_SOC_CORRECTION_LO, BRIDGE_PROTO_RANGE_SOC_CORRECTION_HI);

    any |= clampf_inplace(&s->batt_temp_c, BRIDGE_PROTO_RANGE_BATT_TEMP_C_LO, BRIDGE_PROTO_RANGE_BATT_TEMP_C_HI);
    any |= clampf_inplace(&s->dtc, BRIDGE_PROTO_RANGE_DTC_LO, BRIDGE_PROTO_RANGE_DTC_HI);

    any |= clampf_inplace(&s->main_relay_on, BRIDGE_PROTO_RANGE_MAIN_RELAY_ON_LO, BRIDGE_PROTO_RANGE_MAIN_RELAY_ON_HI);
    any |= clampf_inplace(&s->interlock, BRIDGE_PROTO_RANGE_INTERLOCK_LO, BRIDGE_PROTO_RANGE_INTERLOCK_HI);
    any |= clampf_inplace(&s->full_charge_flag, BRIDGE_PROTO_RANGE_FULL_CHARGE_FLAG_LO, BRIDGE_PROTO_RANGE_FULL_CHARGE_FLAG_HI);
    any |= clampf_inplace(&s->ir_malfunction, BRIDGE_PROTO_RANGE_IR_MALFUNCTION_LO, BRIDGE_PROTO_RANGE_IR_MALFUNCTION_HI);
    any |= clampf_inplace(&s->capacity_empty, BRIDGE_PROTO_RANGE_CAPACITY_EMPTY_LO, BRIDGE_PROTO_RANGE_CAPACITY_EMPTY_HI);

    any |= clampf_inplace(&s->chg2_limit_kw, BRIDGE_PROTO_RANGE_CHG2_LIMIT_KW_LO, BRIDGE_PROTO_RANGE_CHG2_LIMIT_KW_HI);
    return any;
}

// Nissan CRC-8 (poly 0x85, init 0) - computed directly per-byte rather than
// via a precomputed 256-entry table (matches bridge/leaf_signals.py's
// crc8()/_CRC_TABLE exactly in result, trades a few cycles for not needing
// a 256-byte flash table or risking a hand-transcription error in one).
uint8_t leaf_crc8(const uint8_t *data, uint8_t len)
{
    uint8_t crc = 0;
    for (uint8_t i = 0; i < len; i++)
    {
        uint8_t c = (uint8_t)(crc ^ data[i]);
        for (uint8_t bit = 0; bit < 8u; bit++)
        {
            c = (c & 0x80u) ? (uint8_t)((c << 1) ^ 0x85u) : (uint8_t)(c << 1);
        }
        crc = c;
    }
    return crc;
}

// ── Frame builders (byte-verified against real captures, ported verbatim
// from bridge/leaf_signals.py - do not "simplify" any shift/mask here) ────

uint8_t leaf_build_1db(const LeafState *s, uint8_t prun, uint8_t latch, uint8_t out[8])
{
    int32_t raw_i = iround(s->pack_current_a * 2.0f) & 0x7FF;
    int32_t raw_v = iround(s->pack_voltage_v * 2.0f) & 0x3FF;
    uint8_t b[7];
    b[0] = (uint8_t)((raw_i >> 3) & 0xFF);
    b[1] = (uint8_t)(((raw_i & 7) << 5) | (((int32_t)s->relay_cut_request & 3) << 3)
                      | ((int32_t)s->failsafe_status & 7));
    b[2] = (uint8_t)(raw_v >> 2);
    b[3] = (uint8_t)(((raw_v & 3) << 6) | ((int32_t)s->main_relay_on << 5)
                      | ((int32_t)s->full_charge_flag << 4) | ((int32_t)s->interlock << 3)
                      | (((int32_t)s->discharge_pwr_sts & 3) << 1) | latch);
    b[4] = (uint8_t)((int32_t)s->usable_soc & 0x7F);
    b[5] = 0x00u;
    b[6] = prun;
    for (uint8_t i = 0; i < 7u; i++) { out[i] = b[i]; }
    out[7] = leaf_crc8(b, 7u);
    return 8u;
}

uint8_t leaf_build_1db_startup(const LeafState *s, uint8_t prun, uint32_t t_ms, uint8_t out[8])
{
    uint8_t soc = (uint8_t)((int32_t)s->usable_soc & 0x7F);
    uint8_t b[7];
    if (t_ms < (uint32_t)BRIDGE_PROTO_T_PH_B)
    {
        uint8_t tmp[7] = { 0x7Fu, 0xE0u, 0xFFu, 0xC6u, soc, 0x00u, prun };
        for (uint8_t i = 0; i < 7u; i++) { b[i] = tmp[i]; }
    }
    else if (t_ms < (uint32_t)BRIDGE_PROTO_T_PH_C)
    {
        uint8_t tmp[7] = { 0xFFu, 0xE0u, 0xFFu, 0xE7u, soc, 0x00u, prun };
        for (uint8_t i = 0; i < 7u; i++) { b[i] = tmp[i]; }
    }
    else
    {
        // Third phase deliberately hardcodes 0x64 (100) for the SOC byte,
        // NOT the computed `soc` value above - matches the Python source
        // exactly, not a transcription slip.
        uint8_t tmp[7] = { 0x00u, 0x00u, 0xFFu, 0xEFu, 0x64u, 0x00u, prun };
        for (uint8_t i = 0; i < 7u; i++) { b[i] = tmp[i]; }
    }
    for (uint8_t i = 0; i < 7u; i++) { out[i] = b[i]; }
    out[7] = leaf_crc8(b, 7u);
    return 8u;
}

uint8_t leaf_build_1dc(const LeafState *s, uint8_t prun, uint8_t uprate,
                       const uint8_t *code_1dc_override, uint8_t out[8])
{
    int32_t raw_d = iround(s->discharge_limit_kw / 0.25f) & 0x3FF;
    int32_t raw_c = iround(s->charge_limit_kw / 0.25f) & 0x3FF;
    int32_t raw_g = iround((s->charger_limit_kw + 10.0f) / 0.1f) & 0x3FF;
    uint8_t c4, c5, c6;
    if (code_1dc_override != NULL)
    {
        c4 = code_1dc_override[0]; c5 = code_1dc_override[1]; c6 = code_1dc_override[2];
    }
    else
    {
        c4 = BRIDGE_PROTO_CODE_1DC[prun][0];
        c5 = BRIDGE_PROTO_CODE_1DC[prun][1];
        c6 = BRIDGE_PROTO_CODE_1DC[prun][2];
    }
    uint8_t b[7];
    b[0] = (uint8_t)(raw_d >> 2);
    b[1] = (uint8_t)(((raw_d & 3) << 6) | ((raw_c >> 4) & 0x3F));
    b[2] = (uint8_t)(((raw_c & 0xF) << 4) | ((raw_g >> 6) & 0xF));
    b[3] = (uint8_t)(((raw_g & 0x3F) << 2) | ((int32_t)s->charge_pwr_sts & 3));
    b[4] = (uint8_t)(c4 | ((uprate & 7u) << 5));
    b[5] = c5;
    b[6] = (uint8_t)(c6 | prun);
    for (uint8_t i = 0; i < 7u; i++) { out[i] = b[i]; }
    out[7] = leaf_crc8(b, 7u);
    return 8u;
}

uint8_t leaf_build_1dc_startup(uint8_t prun, uint8_t first_frame, uint8_t out[8])
{
    uint8_t b[7];
    if (first_frame)
    {
        uint8_t tmp[7] = { 0xFFu, 0xFFu, 0xFFu, 0xFFu, 0x1Fu, 0xFFu, (uint8_t)(0xFCu | prun) };
        for (uint8_t i = 0; i < 7u; i++) { b[i] = tmp[i]; }
    }
    else
    {
        uint8_t c4 = BRIDGE_PROTO_CODE_1DC[prun][0];
        uint8_t c5 = BRIDGE_PROTO_CODE_1DC[prun][1];
        uint8_t c6 = BRIDGE_PROTO_CODE_1DC[prun][2];
        uint8_t tmp[7] = { 0xFFu, 0xFFu, 0xFFu, 0xFFu, c4, c5, (uint8_t)(c6 | prun) };
        for (uint8_t i = 0; i < 7u; i++) { b[i] = tmp[i]; }
    }
    for (uint8_t i = 0; i < 7u; i++) { out[i] = b[i]; }
    out[7] = leaf_crc8(b, 7u);
    return 8u;
}

uint8_t leaf_build_1c2(uint8_t tick10, uint8_t out[8])
{
    out[0] = (uint8_t)(0x50u | (tick10 & 0x0Fu));
    return 1u;
}

uint8_t leaf_build_1ed(const LeafState *s, uint8_t prun, uint8_t out[8])
{
    int32_t raw = iround((s->chg2_limit_kw - 10.0f) / 0.1f) & 0x7FF;
    uint8_t b[2];
    b[0] = (uint8_t)((raw >> 3) & 0xFF);
    b[1] = (uint8_t)(((raw & 7) << 5) | (prun & 3u));
    out[0] = b[0]; out[1] = b[1];
    out[2] = leaf_crc8(b, 2u);
    return 3u;
}

uint8_t leaf_build_55b(const LeafState *s, uint8_t prun, uint16_t ir_raw, uint8_t refuse_sleep, uint8_t out[8])
{
    int32_t raw_soc = iround(s->fine_soc_pct * 10.0f) & 0x3FF;
    uint8_t b[7];
    b[0] = (uint8_t)(raw_soc >> 2);
    b[1] = (uint8_t)((raw_soc & 3) << 6);
    b[2] = 0x55u;  // `alu` - always 0x55 in this project, no caller ever overrides it (matches the Python default)
    b[3] = 0x00u;
    b[4] = (uint8_t)(ir_raw >> 2);
    b[5] = (uint8_t)(((ir_raw & 3u) << 6) | ((int32_t)s->ir_malfunction & 1));
    b[6] = (uint8_t)(((int32_t)s->capacity_empty << 7) | ((refuse_sleep & 3u) << 5) | 0x10u | prun);
    for (uint8_t i = 0; i < 7u; i++) { out[i] = b[i]; }
    out[7] = leaf_crc8(b, 7u);
    return 8u;
}

uint8_t leaf_build_5bc(const LeafState *s, uint8_t tick10, int16_t gids_raw_override,
                       const uint8_t *chg_time_override, uint8_t out[8])
{
    int32_t gids = (gids_raw_override >= 0) ? gids_raw_override : ((int32_t)s->gids & 0x3FF);
    uint8_t toggle = (uint8_t)(1 - ((tick10 / 5u) & 1u));
    int32_t bars_raw = (int32_t)s->capacity_bars_raw & 0xF;
    uint8_t b5, b6, b7;
    if (chg_time_override != NULL)
    {
        b5 = chg_time_override[0]; b6 = chg_time_override[1]; b7 = chg_time_override[2];
    }
    else
    {
        const uint8_t *row = BRIDGE_PROTO_CHG_TIME_5BC[tick10 % BRIDGE_PROTO_CHG_TIME_5BC_COUNT];
        b5 = row[0]; b6 = row[1]; b7 = row[2];
    }
    out[0] = (uint8_t)(gids >> 2);
    out[1] = (uint8_t)((gids & 3) << 6);
    out[2] = (uint8_t)((bars_raw << 4) | (toggle ? 0x0Eu : 0x04u));
    out[3] = (uint8_t)(iround(s->temp_segment_pct / 0.4166666f) & 0xFF);
    out[4] = (uint8_t)((((int32_t)s->soh_pct & 0x7F) << 1) | toggle);
    out[5] = (uint8_t)(b5 | (((int32_t)s->pwr_limit_reason & 7) << 5));
    out[6] = b6;
    out[7] = b7;
    return 8u;  // NOTE: no CRC byte on this frame - 8 raw payload bytes, matches the Python source exactly
}

uint8_t leaf_build_5bc_first(const LeafState *s, uint8_t tick10, uint8_t out[8])
{
    uint8_t toggle = (uint8_t)(1 - ((tick10 / 5u) & 1u));
    out[0] = 0xFFu; out[1] = 0xC0u; out[2] = 0xFFu; out[3] = 0xFFu;
    out[4] = (uint8_t)((((int32_t)s->soh_pct & 0x7F) << 1) | toggle);
    out[5] = 0x03u; out[6] = 0xFFu; out[7] = 0xFFu;
    return 8u;  // no CRC byte, matches the Python source
}

uint8_t leaf_build_59e(const LeafState *s, uint8_t out[8])
{
    int32_t full = iround(s->qc_full_wh / 100.0f) & 0x1FF;
    int32_t rem = iround(s->qc_remain_wh / 100.0f) & 0x1FF;
    out[0] = 0x00u; out[1] = 0x00u;
    out[2] = (uint8_t)((full >> 4) & 0x1F);
    out[3] = (uint8_t)(((full & 0xF) << 4) | ((rem >> 5) & 0xF));
    out[4] = (uint8_t)((rem & 0x1F) << 3);
    out[5] = 0x00u; out[6] = 0x00u;
    out[7] = (uint8_t)((int32_t)s->soc_correction & 0xFF);
    return 8u;  // no CRC byte, matches the Python source
}

uint8_t leaf_build_5c0(const LeafState *s, uint8_t tick10, const uint8_t *hist_override, uint8_t out[8])
{
    uint8_t mux = (uint8_t)((tick10 % 6u) + 1u);
    uint8_t t = (uint8_t)(((int32_t)s->batt_temp_c + 40) & 0x7F);
    uint8_t b3, b4, b5;
    if (hist_override != NULL)
    {
        b3 = hist_override[0]; b4 = hist_override[1]; b5 = hist_override[2];
    }
    else
    {
        const uint8_t *row = BRIDGE_PROTO_HIST5C0[mux - 1u];  // indexed [mux-1], mux is 1..6
        b3 = row[0]; b4 = row[1]; b5 = row[2];
    }
    out[0] = mux;
    out[1] = (uint8_t)(t << 1);
    out[2] = (uint8_t)(t << 1);
    out[3] = b3; out[4] = b4; out[5] = b5;
    out[6] = 0x1Fu;
    out[7] = (uint8_t)((int32_t)s->dtc & 0xFF);
    return 8u;  // no CRC byte, matches the Python source
}

uint8_t leaf_build_5eb(uint8_t tick10, uint8_t out[8])
{
    const uint8_t *row = BRIDGE_PROTO_SEQ_5EB[tick10 % BRIDGE_PROTO_SEQ_5EB_COUNT];
    for (uint8_t i = 0; i < 8u; i++) { out[i] = row[i]; }
    return 8u;  // no CRC byte, matches the Python source
}
