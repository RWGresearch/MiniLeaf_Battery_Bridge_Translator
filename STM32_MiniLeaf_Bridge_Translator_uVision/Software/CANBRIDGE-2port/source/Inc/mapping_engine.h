/* Signal mapping (STM32 port Phase 5).
 *
 * Ports bridge/mapping_engine.py's MappingEngine.apply()/evaluate_combine()
 * and derive_capacity_outputs(): RZ450e input signal(s) -> a fixed preset
 * combine function -> a Leaf output field. The actual per-tie logic is
 * generated straight-line C in Inc/bridge_config_gen.h (from config/
 * profile.json's saved mapping ties, via tools/export_stm32_config.py) -
 * not a runtime-interpreted table, same "curated fixed structure" rule as
 * everywhere else in this port, just resolved at codegen time instead of
 * on-MCU (config is compile-time-only in this firmware either way).
 */
#ifndef MAPPING_ENGINE_H
#define MAPPING_ENGINE_H

#include "leaf_output.h"

/* Applies every configured mapping tie against g_rz_state (rz450e_ingest.h)
 * into `out`. A tie whose input(s) have no live data yet leaves its output
 * field untouched (keeps whatever DEFAULTS/an earlier tie already put
 * there), matching bridge/mapping_engine.py's evaluate_combine() returning
 * None for "nothing to compute yet" rather than snapping to zero. */
void mapping_engine_apply(LeafState *out);

/* GIDS/QC capacity derived formula (bridge/mapping_engine.py's
 * derive_capacity_outputs()) - not a mapping tie, a separate multi-signal
 * formula using state.vehicle/charge_emulation config
 * (BRIDGE_CFG_USABLE_CAPACITY_KWH etc.). Leaves gids/qc_full_wh/
 * qc_remain_wh untouched if soc_pct or every capacity_packN_ah reading is
 * still missing (both are DID-sourced, unpopulated until Phase 6 -
 * matches Python's own "no data yet" early return). */
void mapping_engine_derive_capacity(LeafState *out);

#endif /* MAPPING_ENGINE_H */
