/* Battery management / safety layer (STM32 port Phase 4).
 *
 * Ports bridge/management_engine.py's ManagementEngine.apply() - the single
 * most safety-critical module in this whole project (docs/05-battery-
 * management-safety.md). This is NOT a passive translator: it reads real
 * RZ450e data (via g_rz_state, rz450e_ingest.h) and decides what's safe,
 * exactly like the Python original.
 *
 * Every threshold used here comes from Inc/bridge_config_gen.h (generated
 * by tools/export_stm32_config.py from config/profile.json) as compile-time
 * constants - there is no runtime config struct, matching this port's
 * "config is a one-time, compile-time thing" design (see the STM32 port
 * plan). Only the ALGORITHM SHAPES (ramp curves, hysteresis, latch rules,
 * the AC uprate level-selection table) are hardcoded C logic here, per
 * docs/09-stm32-export-format.md's "fixed logic, not exported" list.
 *
 * Deliberately NOT ported to firmware (see this file's own top-of-function
 * comments for exactly where each is skipped): human-readable status
 * strings and bridge/fault_log.py's persistent fault-history log - both are
 * PC-GUI-only conveniences with no consumer on this MCU (docs/09's "What
 * this is NOT" section already excludes the log panel/Dashboard). The
 * underlying FACTS those features report (which fault is active, which
 * factor is being applied) are still tracked in ManagementState below -
 * only the string formatting and persistent counters are dropped.
 */
#ifndef MANAGEMENT_ENGINE_H
#define MANAGEMENT_ENGINE_H

#include <stdint.h>
#include "leaf_output.h"   /* LeafState - the full Leaf-bound output struct this
                             * engine reads a baseline from and writes its safety
                             * overrides into (discharge_limit_kw, charge_limit_kw,
                             * charger_limit_kw, capacity_empty, relay_cut_request,
                             * interlock, full_charge_flag) - matches bridge/
                             * management_engine.py's apply() operating on the
                             * SAME `leaf_state` dict DEFAULTS/mapping_engine.c
                             * already populated, not a separate narrower struct.
                             * The AC taper's currently-in-use uprate level is
                             * NOT a LeafState field, same as Python: it's
                             * ManagementState.ac_uprate_level below (mirrors
                             * ManagementEngine.ac_uprate_level, a public engine
                             * attribute, not a leaf_state dict key) - Phase 5's
                             * bridge_sequencer.c reads it from there directly
                             * when building 0x1DC. */

/* Persistent state carried between apply() calls - every hysteresis "applied
 * factor", latch, and "condition has been present since when" timer this
 * engine needs. Mirrors ManagementEngine.__init__()'s instance fields
 * one-for-one. Zero-initialized at startup (matches every Python field
 * starting at None/1.0/False as documented per-field below). */
typedef struct {
    uint32_t last_apply_tick;      /* 0 = apply() never called yet */
    uint32_t first_apply_tick;     /* 0 = apply() never called yet - anchors "never seen" signal aging, docs/13 item 13.1 */

    float discharge_factor_applied;   /* starts at 1.0 (full power) - set in management_engine_init() */
    float regen_factor_applied;       /* starts at 1.0 */

    /* AC charger taper convergence state (docs/05's "AC charger taper rework" section) */
    float ac_applied_kw;
    int8_t ac_current_level;       /* persisted 0-7 rate level, starts at 7 */
    int8_t ac_uprate_level;        /* -1 = not converging right now (mirrors Python None) */

    /* low_voltage_cutoff's soft-cut persistence window */
    uint8_t low_v_condition_pending;
    uint32_t low_v_condition_since_tick;

    /* The 3 soft->hard cross-check escalation timers */
    uint8_t cell_cross_check_pending;
    uint32_t cell_cross_check_since_tick;
    uint8_t temp_cross_check_pending;
    uint32_t temp_cross_check_since_tick;
    uint8_t temp_probe_cross_check_pending;
    uint32_t temp_probe_cross_check_since_tick;

    /* Staleness watchdog */
    uint8_t stale_pending;
    uint32_t stale_since_tick;
    uint8_t staleness_hard_cut;    /* THIS tick only - Phase 5's sequencer needs to know the hard cut came specifically from staleness (docs/13 item 14.3) */

    /* "Is this free-running counter actually still changing" tracking for
     * the 3 watchdog-usable counters (bridge/state.py's _counter_last_value/
     * _counter_last_change_ts) - a stuck counter is a different failure mode
     * from "no frames arriving at all" (which every other signal's own
     * last_update_tick already catches). */
    float alive_3f1_last_value;
    uint32_t alive_3f1_last_change_tick;
    uint8_t alive_3f1_seen;
    float alive_358_last_value;
    uint32_t alive_358_last_change_tick;
    uint8_t alive_358_seen;
    float counter_5s_last_value;
    uint32_t counter_5s_last_change_tick;
    uint8_t counter_5s_seen;

    /* Latches - cleared only by management_engine_notify_session_start()/
     * management_engine_notify_charge_replug(), never by a triggering
     * condition simply recovering (docs/05's "full_charge_flag re-arm"
     * section / docs/12 finding F8). */
    uint8_t hard_latched;
    uint8_t ac_charge_stop_latched;
    uint8_t ac_charge_temp_stop_latched;
} ManagementState;

extern ManagementState g_mgmt_state;

/* Call once at startup. */
void management_engine_init(void);

/* Call once per main-loop control-loop tick (Phase 5's bridge_sequencer_tick(),
 * once that exists - NOT wired in yet as of Phase 4, since there is no real
 * mapping-engine/DEFAULTS output yet to seed `out` with). `out` must already
 * hold the DEFAULTS+mapping-engine baseline values on entry (matching
 * bridge/management_engine.py's `out = dict(leaf_state)`); this function
 * mutates it in place with every safety-layer override. */
void management_engine_apply(LeafState *out);

/* Call when the bridge begins a fresh session (sequencer waiting_for_wake ->
 * startup, i.e. a real bus wake) - clears the hard-cut and AC-stop latches. */
void management_engine_notify_session_start(void);

/* OPTION A (2026-08-21): fuller manual reset than notify_session_start() -
 * clears the 3 latches AND every escalation/pending timer that feeds them,
 * so a still-active condition must re-accumulate through its full timeout
 * before it can re-latch. Called from the Inp2 fault-reset button
 * (bridge_sequencer.c) only. See docs/17-stm32-gpio-reference.md. */
void management_engine_reset_all_conditions(void);

/* Call when charge_permission_input has been continuously absent for at
 * least BRIDGE_CFG_ET_CHG_END_STOP_S before a new charge request arrives (a
 * genuine unplug/replug, not a brief interlock glitch) - clears the same
 * latches as management_engine_notify_session_start(). */
void management_engine_notify_charge_replug(void);

#endif /* MANAGEMENT_ENGINE_H */
