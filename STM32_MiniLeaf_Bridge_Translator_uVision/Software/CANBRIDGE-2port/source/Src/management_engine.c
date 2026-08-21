#include "management_engine.h"   // This module's own prototypes/structs
#include "bridge_config_gen.h"   // BRIDGE_CFG_MF_*/BRIDGE_CFG_CE_* compile-time thresholds (generated, see tools/export_stm32_config.py)
#include "rz450e_ingest.h"        // g_rz_state (Phase 3) - the sole input this engine reads
#include "main.h"                 // HAL_GetTick()

// The one shared instance every other module reads (declared extern in the header).
ManagementState g_mgmt_state;

// ── Small float helpers (kept local/hand-written rather than pulling in
// <math.h> - every operation here is trivial enough not to need it, matching
// this project's "keep the program as small as reasonable" goal) ──────────
static float minf2(float a, float b) { return (a < b) ? a : b; }
static float maxf2(float a, float b) { return (a > b) ? a : b; }
static float absf(float v) { return (v < 0.0f) ? -v : v; }

// Same contract as bridge/management_engine.py's _clamp(): pins `v` to [lo, hi].
static float clampf(float v, float lo, float hi)
{
    return maxf2(lo, minf2(hi, v));
}

// Same contract as bridge/management_engine.py's _ramp_factor(): 1.0 at/above
// `ceiling`, 0.0 at/below `floor`, linear in between. Every taper/derate
// curve in this file is built from this one shape.
static float ramp_factor(float value, float floor, float ceiling)
{
    if (ceiling <= floor)
    {
        return (value >= ceiling) ? 1.0f : 0.0f;
    }
    return clampf((value - floor) / (ceiling - floor), 0.0f, 1.0f);
}

// ── AC charger taper convergence rate table (docs/05's "AC charger taper
// rework" section) - dynamically self-selects one of the existing 0-7
// uprate rates (2.0kW/s at level 7, halving per level down) based on
// remaining distance to target, instead of a fixed time constant. Ported
// unchanged from bridge/management_engine.py's _AC_LEVEL_DOWNSHIFT_KW /
// _select_ac_uprate_level() - these 7 thresholds are new, tuned starting
// values (not real-hardware-confirmed), same status as in the Python source.
static const float AC_LEVEL_DOWNSHIFT_KW[7] = {3.0f, 1.5f, 0.75f, 0.4f, 0.2f, 0.1f, 0.05f};  // levels 7..1
#define AC_LEVEL_HYSTERESIS_MULT 1.5f

static float ac_threshold_for(int8_t level)
{
    if (level <= 0)
    {
        return 0.0f;  // level 0 is the floor - no threshold of its own
    }
    return AC_LEVEL_DOWNSHIFT_KW[7 - level];
}

// Picks the 0-7 uprate level to converge `remaining_kw_abs` toward zero, with
// hysteresis on the switch thresholds (downshift immediately, upshift only
// once comfortably past the next level's own threshold) so the SELECTED
// LEVEL doesn't flap right at a boundary.
static int8_t select_ac_uprate_level(float remaining_kw_abs, int8_t current_level)
{
    int8_t level = current_level;
    while (level > 0 && remaining_kw_abs < ac_threshold_for(level))
    {
        level--;  // downshift: current level's own threshold isn't met
    }
    while (level < 7 && remaining_kw_abs >= ac_threshold_for((int8_t)(level + 1)) * AC_LEVEL_HYSTERESIS_MULT)
    {
        level++;  // upshift: comfortably past the NEXT level's threshold
    }
    return level;
}

// Age (seconds) of one signal - "never seen this session" ages from
// first_apply_tick (docs/13 item 13.1: a signal that's never arrived is a
// STRICTLY WORSE case than one that went stale after being live, so it must
// hit the same soft/hard-cut schedule, not sit outside the watchdog forever).
static float signal_age_s(const RzSignal *sig, uint32_t now, uint32_t first_apply_tick)
{
    uint32_t ref_tick = (sig->last_update_tick != 0) ? sig->last_update_tick : first_apply_tick;
    // Never measure age from before the current session started - first_apply_tick
    // gets reset on every real re-arm (management_engine_notify_session_start()),
    // not just true boot, so a signal that was fresh before an idle period but
    // hasn't updated YET this session reads as "just started", not "ancient".
    if ((int32_t)(first_apply_tick - ref_tick) > 0) { ref_tick = first_apply_tick; }
    return (float)(now - ref_tick) / 1000.0f;
}

// "Is this free-running counter actually still CHANGING value" (bridge/
// state.py's counter_stale_age()) - a different, additional failure mode
// from "no frames arriving at all" (already covered by signal_age_s() on
// every other signal): a bus that's still delivering frames, but with a
// frozen/stuck counter byte, would otherwise look perfectly fresh.
static float counter_freshness_s(uint8_t *seen, float *last_value, uint32_t *last_change_tick,
                                  const RzSignal *sig, uint32_t now, uint32_t first_apply_tick)
{
    if (sig->last_update_tick == 0)
    {
        return (float)(now - first_apply_tick) / 1000.0f;  // never arrived at all
    }
    if (!*seen || sig->value != *last_value)
    {
        *seen = 1;
        *last_value = sig->value;
        *last_change_tick = now;  // first sighting, or the value actually moved - reset the "stuck" clock
    }
    uint32_t ref_tick = *last_change_tick;
    if ((int32_t)(first_apply_tick - ref_tick) > 0) { ref_tick = first_apply_tick; }  // see signal_age_s()'s own note
    return (float)(now - ref_tick) / 1000.0f;
}

void management_engine_init(void)
{
    // g_mgmt_state is zero-initialized by the C runtime (static storage /
    // BSS) before main() ever runs - only the fields that must start
    // somewhere OTHER than zero need setting explicitly here.
    g_mgmt_state.discharge_factor_applied = 1.0f;   // full power, not zero, until a real reading says otherwise
    g_mgmt_state.regen_factor_applied = 1.0f;
    g_mgmt_state.ac_current_level = 7;              // "always start at level 7" per the AC taper's own design
    g_mgmt_state.ac_uprate_level = -1;              // -1 = not actively converging (mirrors Python's None)
}

void management_engine_notify_session_start(void)
{
    // A real bus wake (the closest analog this bridge has to "the car was
    // actually power-cycled") - clears every latch below.
    g_mgmt_state.hard_latched = 0;
    g_mgmt_state.ac_charge_stop_latched = 0;
    g_mgmt_state.ac_charge_temp_stop_latched = 0;
    // Re-arm the staleness watchdog's reference point too (2026-08-19 fix) -
    // without this, a signal that went stale during an idle WAITING_FOR_WAKE
    // period (where management_engine_apply() never runs) reads as ancient
    // the instant the watchdog resumes, staleness_hard_cut fires on the very
    // first tick, and the fresh session winds back down before real data can
    // catch up. signal_age_s()/counter_freshness_s() clamp against this.
    g_mgmt_state.first_apply_tick = HAL_GetTick();
}

void management_engine_reset_all_conditions(void)
{
    // Unlike notify_session_start() above (latches only, deliberately
    // conservative), this also clears every escalation/pending timer that
    // FEEDS the latches - so a still-active condition must re-accumulate
    // through its full timeout before it can re-latch, instead of
    // re-tripping on the very next tick. OPTION A (2026-08-21): called from
    // the Inp2 fault-reset button (bridge_sequencer.c) only - NOT the normal
    // bus-rewake path above, which stays as-is.
    management_engine_notify_session_start();
    g_mgmt_state.low_v_condition_pending = 0;
    g_mgmt_state.cell_cross_check_pending = 0;
    g_mgmt_state.temp_cross_check_pending = 0;
    g_mgmt_state.temp_probe_cross_check_pending = 0;
    g_mgmt_state.stale_pending = 0;
    g_mgmt_state.staleness_hard_cut = 0;
}

void management_engine_notify_charge_replug(void)
{
    // A genuine unplug/replug - corrected 2026-08-19, this comment
    // previously said "charge_permission_input absent" which is WRONG:
    // bridge/realtime_engine.py's real replug edge-detector (inside
    // _apply_charge_ramp(), not yet ported - Phase 5b) watches the LEAF's
    // OWN 0x1F2 charge-request signal going absent for at least
    // BRIDGE_CFG_ET_CHG_END_STOP_S before a fresh request, NOT RZ450e's
    // charge_permission_input (0x358) - that's a separate, additionally-
    // required condition for the charge ramp itself, unrelated to this
    // clear. NOT YET CALLED from anywhere in this port (no caller wires up
    // the trigger condition, since it needs Leaf-side 0x1F2 tracking this
    // firmware doesn't have yet) - the other real-world event allowed to
    // clear these same latches (docs/05's "full_charge_flag re-arm without
    // a physical replug" section), once wired up.
    g_mgmt_state.hard_latched = 0;
    g_mgmt_state.ac_charge_stop_latched = 0;
    g_mgmt_state.ac_charge_temp_stop_latched = 0;
    g_mgmt_state.first_apply_tick = HAL_GetTick();   // see notify_session_start()'s own note
}

void management_engine_apply(LeafState *out)
{
    ManagementState *st = &g_mgmt_state;

    uint32_t now = HAL_GetTick();
    // dt in seconds, matching bridge/management_engine.py's time.monotonic()
    // delta - 0.0 on the very first call (st->last_apply_tick == 0), same as
    // Python's `self._last_apply_time is None` case.
    float dt = (st->last_apply_tick != 0) ? (float)(now - st->last_apply_tick) / 1000.0f : 0.0f;
    st->last_apply_tick = now;
    if (st->first_apply_tick == 0)
    {
        st->first_apply_tick = now;
    }

    // ── Gather the shared inputs every feature below needs (mirrors
    // apply()'s own setup block) ──────────────────────────────────────────
    uint8_t have_cell_min_raw = (g_rz_state.cell_min.last_update_tick != 0);
    float cell_min_raw = g_rz_state.cell_min.value;
    uint8_t have_cell_max_raw = (g_rz_state.cell_max.last_update_tick != 0);
    float cell_max_raw = g_rz_state.cell_max.value;

    // worst_low/worst_high: min/max across all 96 LIVE per-cell readings -
    // the sole authoritative voltage source (docs/05). have_cells tracks
    // whether the per-cell array itself has any live data, separately from
    // worst_low/worst_high (which may fall back to the 0x020 pack-level
    // summary below) - cell_data_cross_check needs that distinction.
    uint8_t have_cells = 0;
    float cell_lo = 0.0f, cell_hi = 0.0f;
    for (int i = 0; i < 96; i++)
    {
        if (g_rz_state.cell[i].last_update_tick != 0)
        {
            float v = g_rz_state.cell[i].value;
            if (!have_cells) { cell_lo = v; cell_hi = v; have_cells = 1; }
            else { if (v < cell_lo) cell_lo = v; if (v > cell_hi) cell_hi = v; }
        }
    }
    uint8_t have_worst_low = have_cells || have_cell_min_raw;
    float worst_low = have_cells ? cell_lo : cell_min_raw;
    uint8_t have_worst_high = have_cells || have_cell_max_raw;
    float worst_high = have_cells ? cell_hi : cell_max_raw;

    uint8_t have_soc = (g_rz_state.soc_pct.last_update_tick != 0);   // stays "no data" until Phase 6 - see rz450e_ingest.h
    float soc = g_rz_state.soc_pct.value;

    uint8_t have_temp_max = (g_rz_state.temp_max.last_update_tick != 0);
    float temp_max = g_rz_state.temp_max.value;
    uint8_t have_temp_min = (g_rz_state.temp_min.last_update_tick != 0);
    float temp_min = g_rz_state.temp_min.value;

    uint8_t soft_cut = 0;
    uint8_t hard_cut = 0;
    st->staleness_hard_cut = 0;   // recomputed below, THIS tick only (docs/13 item 14.3)

    // ── low_voltage_cutoff ────────────────────────────────────────────────
    // Cell voltage is the SOLE authoritative trigger - SoC is a backup check
    // only in the Python source and never touches an output there either, so
    // it's not ported here at all (see this file's header comment on what's
    // deliberately dropped: status-string-only branches with zero output
    // effect).
    if (BRIDGE_CFG_MF_LOW_VOLTAGE_CUTOFF_ENABLED)
    {
        if (have_worst_low && worst_low <= BRIDGE_CFG_MF_LOW_VOLTAGE_CUTOFF_EMERGENCY_LOW_V)
        {
            hard_cut = 1;
            st->low_v_condition_pending = 0;
        }
        else if (have_worst_low && worst_low <= BRIDGE_CFG_MF_LOW_VOLTAGE_CUTOFF_MIN_CELL_V)
        {
            // Persistence window (docs/12 finding F5) - guards against a
            // single-tick sag transient tripping the soft cut.
            if (!st->low_v_condition_pending)
            {
                st->low_v_condition_pending = 1;
                st->low_v_condition_since_tick = now;
            }
            float held = (float)(now - st->low_v_condition_since_tick) / 1000.0f;
            if (held >= BRIDGE_CFG_MF_LOW_VOLTAGE_CUTOFF_SOFT_CUT_PERSISTENCE_S)
            {
                soft_cut = 1;
            }
        }
        else
        {
            st->low_v_condition_pending = 0;
        }
    }

    // ── discharge_power_taper ────────────────────────────────────────────
    if (BRIDGE_CFG_MF_DISCHARGE_POWER_TAPER_ENABLED)
    {
        float v_factor = have_worst_low
            ? ramp_factor(worst_low, BRIDGE_CFG_MF_DISCHARGE_POWER_TAPER_TAPER_MIN_V,
                                      BRIDGE_CFG_MF_DISCHARGE_POWER_TAPER_TAPER_START_V)
            : 1.0f;
        // SoC-based factor (docs/05's "SoC + voltage combined taper") -
        // primary/smoothing input, combined via min() with voltage, never a
        // replacement for it. No data -> 1.0 (does not restrict), which
        // degrades exactly to voltage-only behavior - true through Phase 5
        // since soc_pct has no live source until Phase 6.
        float soc_factor = have_soc
            ? ramp_factor(soc, BRIDGE_CFG_MF_DISCHARGE_POWER_TAPER_TAPER_MIN_SOC_PCT,
                               BRIDGE_CFG_MF_DISCHARGE_POWER_TAPER_TAPER_START_SOC_PCT)
            : 1.0f;
        float instant_factor = minf2(v_factor, soc_factor);

        // Hysteresis: fast attack (snap down immediately on a dip), slow
        // release (rate-limited recovery, avoids power hunting near the
        // threshold under intermittent load).
        if (instant_factor < st->discharge_factor_applied)
        {
            st->discharge_factor_applied = instant_factor;
        }
        else if (instant_factor > st->discharge_factor_applied)
        {
            float max_step = dt / maxf2(BRIDGE_CFG_MF_DISCHARGE_POWER_TAPER_RECOVERY_RAMP_S, 1e-6f);
            st->discharge_factor_applied = minf2(instant_factor, st->discharge_factor_applied + max_step);
        }

        // Floor/ceiling (docs/16 audit): `max(floor, ...)` guards against a
        // misconfigured max < floor, or a mapping-tie baseline already below
        // the floor, either of which would otherwise push output BELOW the
        // floor at a high factor.
        float floor_kw = BRIDGE_CFG_MF_DISCHARGE_POWER_TAPER_DISCHARGE_MIN_KW;
        float max_kw = BRIDGE_CFG_MF_DISCHARGE_POWER_TAPER_DISCHARGE_MAX_KW;
        float ceiling_kw = maxf2(floor_kw, minf2(out->discharge_limit_kw, max_kw));
        out->discharge_limit_kw = floor_kw + (ceiling_kw - floor_kw) * st->discharge_factor_applied;
    }
    else
    {
        st->discharge_factor_applied = 1.0f;  // don't resume a stale ramped-down factor if re-enabled later
    }

    // ── charge_target_taper (REGEN ONLY - drives charge_limit_kw, active
    // regardless of charging context) ────────────────────────────────────
    if (BRIDGE_CFG_MF_CHARGE_TARGET_TAPER_ENABLED)
    {
        float floor_kw = BRIDGE_CFG_MF_CHARGE_TARGET_TAPER_REGEN_MIN_KW;
        float max_kw = BRIDGE_CFG_MF_CHARGE_TARGET_TAPER_REGEN_MAX_KW;
        if (have_worst_high && worst_high >= BRIDGE_CFG_MF_CHARGE_TARGET_TAPER_EMERGENCY_HIGH_V)
        {
            hard_cut = 1;
            st->regen_factor_applied = 0.0f;
            // Literal zero - a floor must NOT keep feeding power into an
            // overvoltage emergency (bypasses floor_kw entirely).
            out->charge_limit_kw = 0.0f;
        }
        else
        {
            float v_factor = have_worst_high
                ? 1.0f - ramp_factor(worst_high, BRIDGE_CFG_MF_CHARGE_TARGET_TAPER_REGEN_FULL_V,
                                                  BRIDGE_CFG_MF_CHARGE_TARGET_TAPER_REGEN_MIN_V)
                : 1.0f;
            float soc_factor = have_soc
                ? 1.0f - ramp_factor(soc, BRIDGE_CFG_MF_CHARGE_TARGET_TAPER_REGEN_FULL_SOC_PCT,
                                          BRIDGE_CFG_MF_CHARGE_TARGET_TAPER_REGEN_MIN_SOC_PCT)
                : 1.0f;
            float instant_factor = minf2(v_factor, soc_factor);

            if (instant_factor < st->regen_factor_applied)
            {
                st->regen_factor_applied = instant_factor;
            }
            else if (instant_factor > st->regen_factor_applied)
            {
                float max_step = dt / maxf2(BRIDGE_CFG_MF_CHARGE_TARGET_TAPER_RECOVERY_RAMP_S, 1e-6f);
                st->regen_factor_applied = minf2(instant_factor, st->regen_factor_applied + max_step);
            }
            float ceiling_kw = maxf2(floor_kw, minf2(out->charge_limit_kw, max_kw));
            out->charge_limit_kw = floor_kw + (ceiling_kw - floor_kw) * st->regen_factor_applied;
        }
    }
    else
    {
        st->regen_factor_applied = 1.0f;
    }

    // ── ac_charge_taper (AC CHARGER ONLY - drives charger_limit_kw, owns the
    // daily/extended SoC target + full_charge_flag, entirely gated on the
    // charging interlock) + ac_charge_temp_derate (its own independently-
    // tunable temperature curve). Both fully separate control paths from
    // driving (docs/05, 2026-08-07 gating tightened) - neither may touch
    // charger_limit_kw while charging_active is false. ────────────────────
    uint8_t charging_active = (g_rz_state.charge_permission_input.last_update_tick != 0)
                               && (g_rz_state.charge_permission_input.value != 0.0f);

    {
        float target = BRIDGE_CFG_CE_EXTENDED_MODE ? BRIDGE_CFG_CE_EXTENDED_TARGET_PCT : BRIDGE_CFG_CE_DAILY_TARGET_PCT;
        float ac_min_kw = BRIDGE_CFG_CE_AC_MIN_KW;

        if (!BRIDGE_CFG_CE_AC_TAPER_ENABLED)
        {
            st->ac_uprate_level = -1;
        }
        else if (!charging_active)
        {
            // charger_limit_kw left completely untouched - driving-mode
            // overvoltage protection is charge_target_taper's job, above.
            st->ac_uprate_level = -1;
        }
        else
        {
            float tapered_kw;
            if (have_worst_high && worst_high >= BRIDGE_CFG_CE_AC_EMERGENCY_V)
            {
                hard_cut = 1;
                st->ac_applied_kw = 0.0f;
                st->ac_uprate_level = -1;
                tapered_kw = 0.0f;
            }
            else
            {
                float ramped_kw = out->charger_limit_kw;
                float instant_factor = have_worst_high
                    ? 1.0f - ramp_factor(worst_high, BRIDGE_CFG_CE_AC_FULL_V, BRIDGE_CFG_CE_AC_MIN_V)
                    : 1.0f;

                if (instant_factor >= 1.0f || ramped_kw <= ac_min_kw)
                {
                    // Full power (nothing to converge to), or the pre-taper
                    // ramp hasn't reached the floor yet - pass through
                    // untouched, keep ac_applied_kw tracking it 1:1 so there
                    // is no stale lag the instant the taper DOES engage.
                    st->ac_applied_kw = ramped_kw;
                    st->ac_uprate_level = -1;
                    tapered_kw = ramped_kw;
                }
                else
                {
                    // Dynamically-selected convergence rate (docs/05's "AC
                    // charger taper rework") - see select_ac_uprate_level()
                    // above for the full rationale.
                    float instant_target_kw = ac_min_kw + (ramped_kw - ac_min_kw) * instant_factor;
                    float remaining = instant_target_kw - st->ac_applied_kw;
                    if (st->ac_uprate_level < 0)
                    {
                        st->ac_current_level = 7;  // fresh entry into the convergence window - always start at 7
                    }
                    int8_t level = select_ac_uprate_level(absf(remaining), st->ac_current_level);
                    st->ac_current_level = level;
                    st->ac_uprate_level = level;
                    float rate_kw_s = 2.0f / (float)(1 << (7 - level));  // same doubling formula as CHG_RAMP_RAW_PER_S
                    float max_step = rate_kw_s * dt;
                    float step = clampf(remaining, -max_step, max_step);  // clamped to land exactly on target, never overshoot
                    st->ac_applied_kw += step;
                    tapered_kw = st->ac_applied_kw;
                }
            }

            // Only reaches here while charging_active - a real, RZ450e-
            // authorized charge session, so this is this feature's own field
            // to control, not a conflict with driving.
            out->charger_limit_kw = tapered_kw;

            // Stop-charging cutoff + SoC-target-reached stop, both latched
            // (docs/05, real bench log 2026-08-06) - once either fires THIS
            // tick, keep asserting the stop on every subsequent tick even if
            // the triggering reading itself recovers, until a genuine
            // session-start/replug clears it.
            uint8_t cutoff_now = have_worst_high && (worst_high >= BRIDGE_CFG_CE_AC_CUTOFF_V);
            uint8_t target_now = have_soc && (soc >= target);
            if (cutoff_now || target_now)
            {
                st->ac_charge_stop_latched = 1;
            }
            if (st->ac_charge_stop_latched)
            {
                out->full_charge_flag = 1;
                out->charge_limit_kw = 0.0f;
                out->charger_limit_kw = -10.0f;
            }
        }
    }

    if (!BRIDGE_CFG_CE_AC_TEMP_DERATE_ENABLED)
    {
        // disabled - latches left untouched (only a real session-start/replug may clear them)
    }
    else if (!charging_active)
    {
        // Do NOT clear ac_charge_temp_stop_latched here - charging_active is
        // a bare, undebounced interlock read; a momentary glitch (not a real
        // unplug) must not silently resume a charger that just hard-stopped
        // on heat.
    }
    else if (!have_temp_max)
    {
        // no temperature data yet - full power, nothing to derate
    }
    else
    {
        float ac_cold_ref = have_temp_min ? temp_min : temp_max;
        float cold_factor = ramp_factor(ac_cold_ref, BRIDGE_CFG_CE_AC_LOW_BLOCK_C, BRIDGE_CFG_CE_AC_DERATE_LOW_START_C);
        float hot_factor = 1.0f - ramp_factor(temp_max, BRIDGE_CFG_CE_AC_DERATE_START_C, BRIDGE_CFG_CE_AC_HARD_STOP_C);
        float ac_temp_factor = minf2(cold_factor, hot_factor);
        out->charger_limit_kw = out->charger_limit_kw * ac_temp_factor;

        // Hot-side latches (ends the session outright, "unplug/replug to
        // resume") - cold-side deliberately does NOT latch, auto-resumes as
        // the pack warms (a cold soak is expected to resolve on its own).
        if (temp_max >= BRIDGE_CFG_CE_AC_HARD_STOP_C)
        {
            st->ac_charge_temp_stop_latched = 1;
        }
        if (st->ac_charge_temp_stop_latched)
        {
            out->full_charge_flag = 1;
            out->charge_limit_kw = 0.0f;
            out->charger_limit_kw = -10.0f;
        }
    }

    // ── over_temperature_derate (driving-mode: discharge + regen, hot AND
    // cold side) ──────────────────────────────────────────────────────────
    if (BRIDGE_CFG_MF_OVER_TEMPERATURE_DERATE_ENABLED && have_temp_max)
    {
        // Cold-side decisions use the COLDEST probe (docs/12 finding F1) -
        // lithium plating happens in the coldest cells. Falls back to
        // temp_max only if temp_min truly isn't available (same 0x4A7 frame).
        float cold_ref = have_temp_min ? temp_min : temp_max;
        uint8_t emergency = (temp_max >= BRIDGE_CFG_MF_OVER_TEMPERATURE_DERATE_EMERGENCY_TEMP_C);
        float d_factor, c_factor;
        if (emergency)
        {
            hard_cut = 1;
            d_factor = 0.0f;
            c_factor = 0.0f;
        }
        else
        {
            d_factor = 1.0f - ramp_factor(temp_max, BRIDGE_CFG_MF_OVER_TEMPERATURE_DERATE_DISCHARGE_DERATE_START_C,
                                                     BRIDGE_CFG_MF_OVER_TEMPERATURE_DERATE_DISCHARGE_HARD_STOP_C);
            // Cold-side derate ramp (docs/12 finding F3) - our real exposure
            // is regen into a cold-soaked pack, not the 0.09C AC charger
            // (which has its own independently-tunable thresholds above).
            float cold_factor = ramp_factor(cold_ref, BRIDGE_CFG_MF_OVER_TEMPERATURE_DERATE_CHARGE_LOW_BLOCK_C,
                                                       BRIDGE_CFG_MF_OVER_TEMPERATURE_DERATE_CHARGE_DERATE_LOW_START_C);
            float hot_factor = 1.0f - ramp_factor(temp_max, BRIDGE_CFG_MF_OVER_TEMPERATURE_DERATE_CHARGE_DERATE_START_C,
                                                            BRIDGE_CFG_MF_OVER_TEMPERATURE_DERATE_CHARGE_HARD_STOP_C);
            c_factor = minf2(cold_factor, hot_factor);
        }
        out->discharge_limit_kw = out->discharge_limit_kw * d_factor;
        out->charge_limit_kw = out->charge_limit_kw * c_factor;
        // charger_limit_kw (AC charging) is deliberately NOT multiplied by
        // c_factor - that's ac_charge_temp_derate's own job, above, with its
        // own independently-tunable thresholds. The TRUE pack-wide emergency
        // is the one exception: a genuine over-temp emergency is a real
        // hazard regardless of what's drawing/accepting current.
        if (emergency)
        {
            out->charger_limit_kw = 0.0f;
        }
    }

    // ── cell_data_cross_check (per-cell array vs. 0x020 pack summary) ────
    if (BRIDGE_CFG_MF_CELL_DATA_CROSS_CHECK_ENABLED)
    {
        if (have_cells && have_cell_min_raw && have_cell_max_raw)
        {
            float delta = maxf2(absf(cell_lo - cell_min_raw), absf(cell_hi - cell_max_raw));
            if (delta >= BRIDGE_CFG_MF_CELL_DATA_CROSS_CHECK_MAX_DELTA_V)
            {
                if (!st->cell_cross_check_pending)
                {
                    st->cell_cross_check_pending = 1;
                    st->cell_cross_check_since_tick = now;
                }
                float held = (float)(now - st->cell_cross_check_since_tick) / 1000.0f;
                if (held >= BRIDGE_CFG_MF_CELL_DATA_CROSS_CHECK_SOFT_CUT_S)
                {
                    soft_cut = 1;
                    if (held - BRIDGE_CFG_MF_CELL_DATA_CROSS_CHECK_SOFT_CUT_S >= BRIDGE_CFG_MF_CELL_DATA_CROSS_CHECK_HARD_ESCALATION_S)
                    {
                        hard_cut = 1;
                    }
                }
            }
            else
            {
                st->cell_cross_check_pending = 0;
            }
        }
        else
        {
            st->cell_cross_check_pending = 0;
        }
    }

    // ── temp_data_cross_check (0x4A7 pack extremes vs. the 16-probe array) ─
    if (BRIDGE_CFG_MF_TEMP_DATA_CROSS_CHECK_ENABLED)
    {
        uint8_t have_probes = 0;
        float probe_lo = 0.0f, probe_hi = 0.0f;
        for (int i = 0; i < 16; i++)
        {
            if (g_rz_state.temp[i].last_update_tick != 0)
            {
                float v = g_rz_state.temp[i].value;
                if (!have_probes) { probe_lo = v; probe_hi = v; have_probes = 1; }
                else { if (v < probe_lo) probe_lo = v; if (v > probe_hi) probe_hi = v; }
            }
        }
        if (have_probes && have_temp_max)
        {
            float delta = absf(temp_max - probe_hi);
            if (have_temp_min)
            {
                delta = maxf2(delta, absf(temp_min - probe_lo));
            }
            if (delta >= BRIDGE_CFG_MF_TEMP_DATA_CROSS_CHECK_MAX_DELTA_C)
            {
                if (!st->temp_cross_check_pending)
                {
                    st->temp_cross_check_pending = 1;
                    st->temp_cross_check_since_tick = now;
                }
                float held = (float)(now - st->temp_cross_check_since_tick) / 1000.0f;
                if (held >= BRIDGE_CFG_MF_TEMP_DATA_CROSS_CHECK_SOFT_CUT_S)
                {
                    soft_cut = 1;
                    if (held - BRIDGE_CFG_MF_TEMP_DATA_CROSS_CHECK_SOFT_CUT_S >= BRIDGE_CFG_MF_TEMP_DATA_CROSS_CHECK_HARD_ESCALATION_S)
                    {
                        hard_cut = 1;
                    }
                }
            }
            else
            {
                st->temp_cross_check_pending = 0;
            }
        }
        else
        {
            st->temp_cross_check_pending = 0;
        }
    }

    // ── temp_probe_cross_check (DID 0x1814 vs 0x4AA, per probe) - compares
    // each of the 16 probes' DID reading against its CAN reading for the
    // SAME physical probe, flagging the worst (largest) single-probe
    // disagreement. Distinct from temp_data_cross_check above (which only
    // compares the 0x4A7 pack-level EXTREMES summary against the probe
    // array as a whole). ────────────────────────────────────────────────
    if (BRIDGE_CFG_MF_TEMP_PROBE_CROSS_CHECK_ENABLED)
    {
        uint8_t have_pair = 0;
        float worst_delta = 0.0f;
        for (int i = 0; i < 16; i++)
        {
            if (g_rz_state.temp_did[i].last_update_tick != 0 && g_rz_state.temp_can[i].last_update_tick != 0)
            {
                float delta = absf(g_rz_state.temp_did[i].value - g_rz_state.temp_can[i].value);
                if (!have_pair || delta > worst_delta)
                {
                    worst_delta = delta;
                    have_pair = 1;
                }
            }
        }
        if (have_pair)
        {
            if (worst_delta >= BRIDGE_CFG_MF_TEMP_PROBE_CROSS_CHECK_MAX_DELTA_C)
            {
                if (!st->temp_probe_cross_check_pending)
                {
                    st->temp_probe_cross_check_pending = 1;
                    st->temp_probe_cross_check_since_tick = now;
                }
                float held = (float)(now - st->temp_probe_cross_check_since_tick) / 1000.0f;
                if (held >= BRIDGE_CFG_MF_TEMP_PROBE_CROSS_CHECK_SOFT_CUT_S)
                {
                    soft_cut = 1;
                    if (held - BRIDGE_CFG_MF_TEMP_PROBE_CROSS_CHECK_SOFT_CUT_S
                        >= BRIDGE_CFG_MF_TEMP_PROBE_CROSS_CHECK_HARD_ESCALATION_S)
                    {
                        hard_cut = 1;
                    }
                }
            }
            else
            {
                st->temp_probe_cross_check_pending = 0;
            }
        }
        else
        {
            st->temp_probe_cross_check_pending = 0;
        }
    }

    // ── staleness_watchdog (worst age across every currently-tracked input,
    // docs/06 section 3) ──────────────────────────────────────────────────
    if (BRIDGE_CFG_MF_STALENESS_WATCHDOG_ENABLED)
    {
        float worst_age = 0.0f;

        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.pack_v, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.cell_min, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.cell_max, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.current, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.current_b, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.temp_max, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.temp_min, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.charge_permission_input, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.soc_pct, now, st->first_apply_tick));
        for (int i = 0; i < 96; i++)
        {
            worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.cell[i], now, st->first_apply_tick));
        }
        for (int i = 0; i < 16; i++)
        {
            worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.temp[i], now, st->first_apply_tick));
            worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.temp_can[i], now, st->first_apply_tick));
            worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.temp_did[i], now, st->first_apply_tick));
        }
        // Phase 6 DID-sourced scalars - completes the match against
        // bridge/rz450e_signals.py's full INPUT_SIGNAL_KEYS coverage.
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.capacity_pack1_ah, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.capacity_pack2_ah, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.capacity_pack3_ah, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.capacity_pack4_ah, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.primary_pack_v, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, signal_age_s(&g_rz_state.primary_current_a, now, st->first_apply_tick));

        // The 3 watchdog-usable counters use "still changing," not "still
        // arriving" (see counter_freshness_s()'s own comment) - a separate
        // check from every signal above, not a duplicate of it.
        worst_age = maxf2(worst_age, counter_freshness_s(&st->alive_3f1_seen, &st->alive_3f1_last_value,
                          &st->alive_3f1_last_change_tick, &g_rz_state.alive_3f1, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, counter_freshness_s(&st->alive_358_seen, &st->alive_358_last_value,
                          &st->alive_358_last_change_tick, &g_rz_state.alive_358, now, st->first_apply_tick));
        worst_age = maxf2(worst_age, counter_freshness_s(&st->counter_5s_seen, &st->counter_5s_last_value,
                          &st->counter_5s_last_change_tick, &g_rz_state.counter_5s, now, st->first_apply_tick));

        if (worst_age >= BRIDGE_CFG_MF_STALENESS_WATCHDOG_SOFT_CUT_S)
        {
            if (!st->stale_pending)
            {
                st->stale_pending = 1;
                st->stale_since_tick = now;
            }
            soft_cut = 1;
            // Stale safety-relevant data must also stop charging outright,
            // not just soft-cut (docs/05, user directive) - we can no longer
            // verify it's safe to keep accepting charge/regen.
            out->charge_limit_kw = 0.0f;
            out->charger_limit_kw = -10.0f;
            out->full_charge_flag = 1;
            if ((float)(now - st->stale_since_tick) / 1000.0f >= BRIDGE_CFG_MF_STALENESS_WATCHDOG_HARD_ESCALATION_S)
            {
                hard_cut = 1;
                st->staleness_hard_cut = 1;
            }
        }
        else
        {
            st->stale_pending = 0;
        }
    }

    // ── Final assembly - capacity_empty/relay_cut_request/interlock each
    // have exactly ONE authority within this function (docs/13 item 16.3):
    // soft_cut and the hard-cut latch, respectively. ──────────────────────
    out->capacity_empty = soft_cut ? 1u : 0u;
    if (hard_cut)
    {
        st->hard_latched = 1;   // latches - keeps asserting even after the trigger recovers, until a real replug/session-start
    }
    if (st->hard_latched)
    {
        out->relay_cut_request = 3;
        out->interlock = 0;
    }
    else
    {
        out->relay_cut_request = 0;
        out->interlock = 1;
    }
}
