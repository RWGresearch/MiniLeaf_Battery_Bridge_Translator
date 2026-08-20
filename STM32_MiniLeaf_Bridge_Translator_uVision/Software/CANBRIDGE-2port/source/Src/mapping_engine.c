#include "mapping_engine.h"      // This module's own prototypes
#include "rz450e_ingest.h"        // g_rz_state - every tie's input signal(s) come from here
#include "bridge_config_gen.h"   // bridge_config_apply_mapping_ties(), BRIDGE_CFG_USABLE_CAPACITY_KWH etc.

// The actual per-tie logic (RZ450e input(s) -> combine function -> Leaf
// output field) is generated straight-line C, not a runtime interpreter -
// see bridge_config_gen.h's own comment and tools/export_stm32_config.py's
// _emit_tie_statement() for why (removed 2026-08-18, chasing a Keil
// link-size overage: config is compile-time-only here, so there's no
// runtime benefit to re-deriving each tie's input/output/combine-function
// from a string-keyed table on every tick instead of just emitting the
// equivalent code once at codegen time).
void mapping_engine_apply(LeafState *out)
{
    bridge_config_apply_mapping_ties(out);
}

void mapping_engine_derive_capacity(LeafState *out)
{
    if (g_rz_state.soc_pct.last_update_tick == 0)
    {
        return;  // no SoC data yet - leave gids/qc_full_wh/qc_remain_wh at their existing value
    }

    // Falls back through pack1 -> pack2 -> pack3 -> pack4, using whichever
    // is the first with a genuinely nonzero live reading - matches Python's
    // `if capacity_ah: break` truthiness check (0.0 counts as "no data").
    const RzSignal *capacity_candidates[4] = {
        &g_rz_state.capacity_pack1_ah, &g_rz_state.capacity_pack2_ah,
        &g_rz_state.capacity_pack3_ah, &g_rz_state.capacity_pack4_ah,
    };
    float capacity_ah = 0.0f;
    uint8_t have_capacity = 0;
    for (uint8_t i = 0; i < 4u; i++)
    {
        if (capacity_candidates[i]->last_update_tick != 0 && capacity_candidates[i]->value != 0.0f)
        {
            capacity_ah = capacity_candidates[i]->value;
            have_capacity = 1;
            break;
        }
    }
    if (!have_capacity)
    {
        return;  // no capacity data yet - leave gids/qc_full_wh/qc_remain_wh untouched
    }

    float soc_pct = g_rz_state.soc_pct.value;
    float nameplate_ah = BRIDGE_CFG_NAMEPLATE_CAPACITY_AH;
    float soh_fraction = (nameplate_ah != 0.0f) ? (capacity_ah / nameplate_ah) : 0.0f;
    float usable_kwh_at_soh = BRIDGE_CFG_USABLE_CAPACITY_KWH * soh_fraction;
    float usable_wh = (soc_pct / 100.0f) * usable_kwh_at_soh * 1000.0f;
    float qc_ceiling_wh = usable_kwh_at_soh * 1000.0f * (BRIDGE_CFG_CE_QC_MAX_SOC_PCT / 100.0f);
    float remain_wh = qc_ceiling_wh - usable_wh;

    out->gids = usable_wh / 80.0f;
    out->qc_full_wh = qc_ceiling_wh;
    out->qc_remain_wh = (remain_wh > 0.0f) ? remain_wh : 0.0f;
}
