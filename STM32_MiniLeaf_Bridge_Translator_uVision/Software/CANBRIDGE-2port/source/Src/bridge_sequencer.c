#include "bridge_sequencer.h"    // This module's own prototypes
#include "bridge_config_gen.h"   // BRIDGE_CFG_*/BRIDGE_PROTO_* (generated, see tools/export_stm32_config.py)
#include "main.h"                 // HAL_GetTick()
#include "rz450e_ingest.h"        // g_rz_state, rz450e_ingest_frame() (Phase 3)
#include "management_engine.h"    // g_mgmt_state, management_engine_* (Phase 4)
#include "mapping_engine.h"       // mapping_engine_apply()/mapping_engine_derive_capacity() (Phase 5)
#include "leaf_output.h"          // LeafState, leaf_state_defaults/clamp, leaf_build_* (Phase 5)
#include "rz450e_uds.h"            // rz450e_uds_init/on_frame/tick() - DID/UDS polling (Phase 6)
#include <stddef.h>                 // NULL - used directly below (no longer pulled in transitively; bridge_config_gen.h stopped including it once its own NULL usage moved to bridge_config_gen.c)

// ── Phase state machine (bridge/realtime_engine.py's ShutdownSequencer) ────
typedef enum {
    BRIDGE_PHASE_IDLE,             // never used in firmware - see bridge_sequencer_init()'s comment
    BRIDGE_PHASE_WAITING_FOR_WAKE, // armed, waiting for real Leaf-bus traffic
    BRIDGE_PHASE_STARTUP,          // staged bring-up in progress (docs/07 timeline)
    BRIDGE_PHASE_RUNNING,          // fully up, normal operation
    BRIDGE_PHASE_WINDING_DOWN,     // staged wind-down in progress (docs/07 staging)
    BRIDGE_PHASE_STOPPED,          // silent, waiting for the bus to go genuinely quiet before re-arming
} BridgePhase;

static BridgePhase phase = BRIDGE_PHASE_WAITING_FOR_WAKE;
static uint32_t session_start_tick = 0;
static uint32_t shutdown_t0_tick = 0;
static uint32_t stopped_since_tick = 0;
static uint8_t last_leaf_rx_valid = 0;
static uint32_t last_leaf_rx_tick = 0;
static uint8_t rearmed_naturally = 0;   // see __init__'s comment in the Python source - only a REAL wind-down/re-wake sets this
static uint8_t manual_shutdown_requested = 0;  // no trigger source yet in firmware - reserved for a future physical button

// Ignition IDs (0x108, 0x1CB, 0x284) - presence/timestamp only, no payload decode
static uint8_t ignition_seen[3] = {0, 0, 0};
static uint32_t ignition_last_seen_tick[3] = {0, 0, 0};

// 0x1F2 charge-request decode state (CommandedChargePower / Charge_StatusTransitionReqest)
static uint8_t chg_last_frame_valid = 0;
static uint32_t chg_last_frame_tick = 0;
static int8_t chg_trans = -1;              // -1 = never seen (mirrors Python's None)
static uint16_t chg_cmd = 0;
static uint8_t chg_trans_last_change_valid = 0;
static uint32_t chg_trans_last_change_tick = 0;
static uint8_t chg_seen_active = 0;

// Wind-down trigger persistence timers
static uint8_t ignition_off_pending = 0;
static uint32_t ignition_off_since_tick = 0;
static uint8_t chg_end_pending = 0;
static uint32_t chg_end_since_tick = 0;
static uint8_t chg_stall_pending = 0;
static uint32_t chg_stall_since_tick = 0;

// Idle-sleep activity timer (Phase 2) - any CAN traffic on either bus resets it
static volatile uint32_t last_activity_tick = 0;

// Free-running 10ms counters (bridge/realtime_engine.py's _prun_tick_loop)
static uint8_t prun = 0;
static uint8_t tick10 = 0;
static uint8_t latch = 0;
static uint32_t prun_next_due_tick = 0;

// ── Per-ID TX period/timing table (bridge/leaf_signals.py's TX_PERIOD_MS) ──
typedef struct {
    uint32_t arb_id;
    uint16_t period_ms;
} TxIdInfo;

typedef enum {
    TXID_1DB = 0, TXID_1DC, TXID_1C2, TXID_1ED,
    TXID_55B, TXID_5BC, TXID_59E, TXID_5C0, TXID_5EB,
    TXID_COUNT
} TxIdIndex;

static const TxIdInfo TX_IDS[TXID_COUNT] = {
    { 0x1DBu, BRIDGE_PROTO_TX_PERIOD_MS_1DB },
    { 0x1DCu, BRIDGE_PROTO_TX_PERIOD_MS_1DC },
    { 0x1C2u, BRIDGE_PROTO_TX_PERIOD_MS_1C2 },
    { 0x1EDu, BRIDGE_PROTO_TX_PERIOD_MS_1ED },
    { 0x55Bu, BRIDGE_PROTO_TX_PERIOD_MS_55B },
    { 0x5BCu, BRIDGE_PROTO_TX_PERIOD_MS_5BC },
    { 0x59Eu, BRIDGE_PROTO_TX_PERIOD_MS_59E },
    { 0x5C0u, BRIDGE_PROTO_TX_PERIOD_MS_5C0 },
    { 0x5EBu, BRIDGE_PROTO_TX_PERIOD_MS_5EB },
};
static uint32_t next_due_tick[TXID_COUNT] = {0};

// ── Wind-down trigger helpers (bridge/realtime_engine.py's ShutdownSequencer) ──

// "Is at least one ignition ID's last sighting still within the quiet
// window" - bridge/realtime_engine.py's _run_state_fresh().
static uint8_t run_state_fresh(uint32_t now)
{
    uint8_t any_seen = 0;
    uint32_t newest = 0;
    for (uint8_t i = 0; i < 3u; i++)
    {
        if (ignition_seen[i])
        {
            if (!any_seen || (int32_t)(ignition_last_seen_tick[i] - newest) > 0)
            {
                newest = ignition_last_seen_tick[i];
            }
            any_seen = 1;
        }
    }
    if (!any_seen) { return 0u; }
    float elapsed_s = (float)(now - newest) / 1000.0f;
    return (elapsed_s <= BRIDGE_CFG_ET_IGNITION_QUIET_S) ? 1u : 0u;
}

// LB_RefusetoSleep (0x55B byte 6 bits 5-6) - bridge/realtime_engine.py's
// ShutdownSequencer.refuse_sleep_value(): 0 while ignition IDs are fresh
// (car is on), forced 1 during winding_down (real-capture-confirmed
// behavior, see leaf_output.c's caller).
static uint8_t refuse_sleep_value(uint32_t now)
{
    if (phase == BRIDGE_PHASE_WINDING_DOWN) { return 1u; }
    return run_state_fresh(now) ? 0u : 1u;
}

// bridge/realtime_engine.py's ShutdownSequencer._ignition_off_detected().
static uint8_t ignition_off_detected(uint32_t now)
{
    float since_session_s = (float)(now - session_start_tick) / 1000.0f;
    if (since_session_s < BRIDGE_CFG_ET_IGNITION_GRACE_S) { return 0u; }
    if (!(ignition_seen[0] && ignition_seen[1] && ignition_seen[2])) { return 0u; }  // need ALL 3 IDs seen at least once
    uint32_t newest = 0;
    for (uint8_t i = 0; i < 3u; i++)
    {
        if (i == 0u || (int32_t)(ignition_last_seen_tick[i] - newest) > 0) { newest = ignition_last_seen_tick[i]; }
    }
    float elapsed_s = (float)(now - newest) / 1000.0f;
    return (elapsed_s > BRIDGE_CFG_ET_IGNITION_QUIET_S) ? 1u : 0u;
}

// bridge/realtime_engine.py's ShutdownSequencer._chg_fresh()/charge_active().
static uint8_t chg_fresh(uint32_t now)
{
    if (!chg_last_frame_valid) { return 0u; }
    float age_s = (float)(now - chg_last_frame_tick) / 1000.0f;
    return (age_s <= BRIDGE_CFG_ET_CHG_CMD_FRESH_S) ? 1u : 0u;
}

static uint8_t charge_active(uint32_t now)
{
    if (!chg_fresh(now)) { return 0u; }
    return (chg_trans == 1 || chg_cmd > (uint16_t)BRIDGE_PROTO_CHG_CMD_IDLE) ? 1u : 0u;
}

// bridge/realtime_engine.py's ShutdownSequencer._should_wind_down() - all 6
// triggers (5th/6th are this bridge's own additions, docs/07).
static uint8_t should_wind_down(uint32_t now, uint8_t staleness_hard_cut, uint8_t charge_authorized)
{
    if (manual_shutdown_requested) { manual_shutdown_requested = 0u; return 1u; }
    if (staleness_hard_cut) { return 1u; }  // 5th trigger - staleness ONLY, see management_engine.h's own note

    uint8_t chg_is_active = charge_active(now);
    uint8_t chg_effective = (chg_is_active && charge_authorized) ? 1u : 0u;
    if (chg_effective)
    {
        ignition_off_pending = 0u;
        chg_end_pending = 0u;
        chg_stall_pending = 0u;
        return 0u;
    }

    // Trigger 1: ignition-off, with a grace period + hold-time delay
    if (ignition_off_detected(now))
    {
        if (!ignition_off_pending) { ignition_off_pending = 1u; ignition_off_since_tick = now; }
        float held_s = (float)(now - ignition_off_since_tick) / 1000.0f;
        if (held_s >= BRIDGE_CFG_ET_IGNITION_OFF_DELAY_S) { return 1u; }
    }
    else
    {
        ignition_off_pending = 0u;
    }

    // Trigger 3: charge-session-end stop
    if (chg_seen_active && !chg_effective && !run_state_fresh(now))
    {
        if (!chg_end_pending) { chg_end_pending = 1u; chg_end_since_tick = now; }
        float held_s = (float)(now - chg_end_since_tick) / 1000.0f;
        if (held_s >= BRIDGE_CFG_ET_CHG_END_STOP_S) { return 1u; }
    }
    else
    {
        chg_end_pending = 0u;
    }

    // Trigger 4: charge-negotiation-stall timeout
    if (chg_fresh(now) && !chg_seen_active && !run_state_fresh(now))
    {
        uint32_t anchor = chg_trans_last_change_valid ? chg_trans_last_change_tick : now;
        if (!chg_stall_pending || (int32_t)(anchor - chg_stall_since_tick) > 0)
        {
            chg_stall_since_tick = anchor;
            chg_stall_pending = 1u;
        }
        float held_s = (float)(now - chg_stall_since_tick) / 1000.0f;
        if (held_s >= BRIDGE_CFG_ET_CHG_STALL_TIMEOUT_S) { return 1u; }
    }
    else
    {
        chg_stall_pending = 0u;
    }

    // Trigger 6: total Leaf-bus silence (defensive fallback, docs/07)
    if (last_leaf_rx_valid)
    {
        float silent_s = (float)(now - last_leaf_rx_tick) / 1000.0f;
        if (silent_s >= BRIDGE_CFG_ET_BUS_SILENCE_TIMEOUT_S) { return 1u; }
    }

    return 0u;
}

// bridge/leaf_signals.py's ShutdownSequencer.id_active_during_winddown() -
// which HVBAT IDs keep transmitting (with fully normal content) at each
// wind-down stage, per docs/07's shutdown staging table.
static uint8_t id_active_during_winddown(uint32_t arb_id, uint32_t elapsed_ms)
{
    if (arb_id == 0x59Eu || arb_id == 0x5C0u || arb_id == 0x5EBu) { return 0u; }
    if (arb_id == 0x55Bu || arb_id == 0x5BCu)
    {
        return (elapsed_ms < (uint32_t)BRIDGE_PROTO_PWRDOWN_STAGE2_MS) ? 1u : 0u;
    }
    if (arb_id == 0x1DBu || arb_id == 0x1DCu)
    {
        return (elapsed_ms < (uint32_t)BRIDGE_PROTO_PWRDOWN_STAGE3_MS) ? 1u : 0u;
    }
    return (elapsed_ms < (uint32_t)BRIDGE_PROTO_PWRDOWN_STAGE4_MS) ? 1u : 0u;   // 0x1C2 / 0x1ED
}

// bridge/realtime_engine.py's ShutdownSequencer.tick() - advances the phase
// state machine, returns the "timing" value _build_frame()/the TX gating
// below needs (ms since session_start while startup/running, ms since
// shutdown_t0 while winding_down, 0 otherwise).
static uint32_t advance_phase(uint32_t now, uint8_t staleness_hard_cut, uint8_t charge_authorized)
{
    if (phase == BRIDGE_PHASE_STARTUP || phase == BRIDGE_PHASE_RUNNING)
    {
        uint32_t t_ms = now - session_start_tick;
        if (t_ms >= (uint32_t)BRIDGE_PROTO_T_RUNNING) { phase = BRIDGE_PHASE_RUNNING; }
        if (should_wind_down(now, staleness_hard_cut, charge_authorized))
        {
            phase = BRIDGE_PHASE_WINDING_DOWN;
            shutdown_t0_tick = now;
        }
        return t_ms;
    }
    if (phase == BRIDGE_PHASE_WINDING_DOWN)
    {
        uint32_t elapsed_ms = now - shutdown_t0_tick;
        if (elapsed_ms >= (uint32_t)BRIDGE_PROTO_PWRDOWN_STAGE4_MS)
        {
            phase = BRIDGE_PHASE_STOPPED;
            stopped_since_tick = now;
        }
        return elapsed_ms;
    }
    if (phase == BRIDGE_PHASE_STOPPED)
    {
        // BUG-FIX PARITY (see the Python source's own comment on this exact
        // point): re-arm only once the bus has gone GENUINELY quiet for the
        // cooldown period, re-checked continuously - never on a flat timer
        // regardless of real traffic.
        float quiet_for_s = last_leaf_rx_valid ? (float)(now - last_leaf_rx_tick) / 1000.0f : 0.0f;
        if (quiet_for_s >= BRIDGE_PROTO_PWRDOWN_DEFAULT_COOLDOWN_S)
        {
            phase = BRIDGE_PHASE_WAITING_FOR_WAKE;
            rearmed_naturally = 1u;   // a REAL wind-down/re-wake, not just a boot - see this file's own note
            ignition_seen[0] = 0u; ignition_seen[1] = 0u; ignition_seen[2] = 0u;
            chg_seen_active = 0u;
        }
        return 0u;
    }
    return 0u;   // idle / waiting_for_wake - nothing to advance here (see note_leaf_rx() for that transition)
}

// bridge/realtime_engine.py's ShutdownSequencer.note_leaf_rx() - called for
// every frame received on the Leaf bus (CAN2), regardless of ID.
static void note_leaf_rx(uint32_t arb_id, const uint8_t *data, uint8_t dlc, uint32_t now)
{
    last_leaf_rx_tick = now;
    last_leaf_rx_valid = 1u;

    if (phase == BRIDGE_PHASE_WAITING_FOR_WAKE)
    {
        phase = BRIDGE_PHASE_STARTUP;
        session_start_tick = now;
        if (rearmed_naturally)
        {
            // Real bus wake following a genuine prior wind-down (docs/12
            // finding F8) - the closest analog this bridge has to "the car
            // being powered down and back on" - clears a latched hard cut.
            // NOT called on the very first-ever boot (rearmed_naturally
            // starts at 0), matching the Python source exactly.
            management_engine_notify_session_start();
        }
    }

    if (arb_id == 0x108u) { ignition_seen[0] = 1u; ignition_last_seen_tick[0] = now; }
    else if (arb_id == 0x1CBu) { ignition_seen[1] = 1u; ignition_last_seen_tick[1] = now; }
    else if (arb_id == 0x284u) { ignition_seen[2] = 1u; ignition_last_seen_tick[2] = now; }
    else if (arb_id == 0x1F2u && dlc >= 3u)
    {
        uint16_t cmd = (uint16_t)(((uint16_t)(data[0] & 3u) << 8) | data[1]);
        int8_t trans = (int8_t)((data[2] >> 5) & 3u);
        if (trans != chg_trans)
        {
            chg_trans_last_change_tick = now;
            chg_trans_last_change_valid = 1u;
        }
        chg_trans = trans;
        chg_cmd = cmd;
        chg_last_frame_tick = now;
        chg_last_frame_valid = 1u;
        if (trans == 1 || cmd > (uint16_t)BRIDGE_PROTO_CHG_CMD_IDLE) { chg_seen_active = 1u; }
    }
}

// ── Composition + frame building (bridge/realtime_engine.py's
// _compose_leaf_state()/_build_frame()) ─────────────────────────────────────

static void compose_leaf_state(LeafState *out)
{
    leaf_state_defaults(out);
    mapping_engine_apply(out);
    mapping_engine_derive_capacity(out);
    // PORT PHASE 5b TODO (deliberately deferred, see this file's header
    // comment): _apply_charge_ramp() not ported - charger_limit_kw/
    // charge_limit_kw jump directly to the mapped/management value during
    // an active charge session instead of ramping smoothly.
    management_engine_apply(out);
    (void)leaf_state_clamp(out);   // MUST run before any frame is built - see leaf_output.h
}

static uint8_t build_frame_for_id(uint32_t arb_id, const LeafState *s, uint32_t timing_ms,
                                   uint8_t in_startup, uint8_t out[8])
{
    static const uint8_t zero_override[3] = { 0x00u, 0x00u, 0x00u };
    uint8_t p = BRIDGE_CFG_SEND_PRUN ? prun : 0u;

    if (arb_id == 0x1DBu)
    {
        if (in_startup && timing_ms < (uint32_t)BRIDGE_PROTO_T_VALID)
        {
            return leaf_build_1db_startup(s, p, timing_ms, out);
        }
        LeafState s2 = *s;
        if (in_startup && timing_ms < (uint32_t)BRIDGE_PROTO_T_RUNNING)
        {
            s2.failsafe_status = 0.0f;
        }
        uint8_t use_latch = BRIDGE_CFG_SEND_VOLTAGE_LATCH_TOGGLE ? latch : 0u;
        return leaf_build_1db(&s2, p, use_latch, out);
    }

    if (arb_id == 0x1DCu)
    {
        if (in_startup && timing_ms < (uint32_t)BRIDGE_PROTO_T_VALID)
        {
            return leaf_build_1dc_startup(p, (tick10 <= 1u) ? 1u : 0u, out);
        }
        const uint8_t *code_override = BRIDGE_CFG_SEND_CODE_1DC ? NULL : zero_override;
        // AC taper's currently-in-use uprate level (management_engine.h) -
        // -1 = not converging -> 0, matching the idle/no-charge-ramp value
        // this port's deferred _apply_charge_ramp() would otherwise supply.
        uint8_t uprate = (g_mgmt_state.ac_uprate_level >= 0) ? (uint8_t)g_mgmt_state.ac_uprate_level : 0u;
        return leaf_build_1dc(s, p, uprate, code_override, out);
    }

    if (arb_id == 0x1C2u)
    {
        if (!BRIDGE_CFG_SEND_HEARTBEAT_1C2) { return 0u; }
        return leaf_build_1c2(tick10, out);
    }

    if (arb_id == 0x1EDu)
    {
        return leaf_build_1ed(s, p, out);
    }

    if (arb_id == 0x55Bu)
    {
        uint16_t ir_raw = (in_startup && timing_ms < (uint32_t)BRIDGE_PROTO_T_VALID) ? 0x3FFu : 904u;
        uint8_t rs = refuse_sleep_value(HAL_GetTick());
        return leaf_build_55b(s, p, ir_raw, rs, out);
    }

    if (arb_id == 0x5BCu)
    {
        if (in_startup && tick10 <= 1u)
        {
            return leaf_build_5bc_first(s, tick10, out);
        }
        int16_t gids_override = (in_startup && timing_ms < (uint32_t)(BRIDGE_PROTO_T_VALID + 50)) ? 0x3FF : -1;
        const uint8_t *chg_time_override = BRIDGE_CFG_SEND_CHG_TIME_5BC ? NULL : zero_override;
        return leaf_build_5bc(s, tick10, gids_override, chg_time_override, out);
    }

    if (arb_id == 0x59Eu)
    {
        return leaf_build_59e(s, out);
    }

    if (arb_id == 0x5C0u)
    {
        const uint8_t *hist_override = BRIDGE_CFG_SEND_HIST_5C0 ? NULL : zero_override;
        return leaf_build_5c0(s, tick10, hist_override, out);
    }

    if (arb_id == 0x5EBu)
    {
        if (!BRIDGE_CFG_SEND_SEQ_5EB) { return 0u; }
        return leaf_build_5eb(tick10, out);
    }

    return 0u;
}

// ── Public API ───────────────────────────────────────────────────────────

void bridge_sequencer_init(void)
{
    last_activity_tick = HAL_GetTick();
    rz450e_ingest_init();
    rz450e_uds_init();
    management_engine_init();

    // No GUI/operator "Start Bridge" button exists on this hardware - the
    // bridge is always live and ready (see this file's header comment) -
    // skip the Python app's manual 'idle' phase entirely and arm
    // immediately, equivalent to arm() running right after boot.
    phase = BRIDGE_PHASE_WAITING_FOR_WAKE;
    prun_next_due_tick = HAL_GetTick() + 10u;
}

void bridge_sequencer_on_frame(uint8_t can_bus, CAN_FRAME *frame)
{
    uint32_t now = HAL_GetTick();
    last_activity_tick = now;   // any frame on either bus resets the idle/sleep clock

    if (can_bus == MYCAN1)
    {
        rz450e_ingest_frame(frame);
        // Unconditional, not an else-branch of the above: broadcast IDs and
        // the Toyota UDS response ID (0x74F) never overlap, so trying both
        // is always safe and avoids needing an ID-set lookup here.
        rz450e_uds_on_frame(frame);
    }
    else
    {
        note_leaf_rx(frame->ID, frame->data, frame->dlc, now);
    }
}

void bridge_sequencer_tick(void)
{
    uint32_t now = HAL_GetTick();

    // Free-running 10ms PRUN/latch/tick10 (bridge/realtime_engine.py's
    // _prun_tick_loop, its own dedicated thread) - always ticking,
    // independent of sequencer phase, same "just a free-running counter"
    // requirement as the Python source (docs/03).
    if ((int32_t)(now - prun_next_due_tick) >= 0)
    {
        prun = (uint8_t)((prun + 1u) % 4u);
        latch ^= 1u;
        tick10++;
        prun_next_due_tick += 10u;
        if ((int32_t)(prun_next_due_tick - now) < 0) { prun_next_due_tick = now + 10u; }
    }

    // RZ450e UDS/DID polling (Phase 6) - runs independent of the Leaf-side
    // sequencer phase below, matching bridge/realtime_engine.py's
    // _did_poll_loop being its own always-on thread, unrelated to whether
    // the bridge is currently transmitting to the Leaf.
    rz450e_uds_tick();

    if (phase == BRIDGE_PHASE_IDLE || phase == BRIDGE_PHASE_WAITING_FOR_WAKE)
    {
        return;   // nothing composed, nothing transmitted, nothing to advance yet
    }

    // Compose THIS tick's full Leaf output BEFORE advancing the phase -
    // should_wind_down()'s staleness trigger needs g_mgmt_state.
    // staleness_hard_cut freshly computed by management_engine_apply()
    // inside compose_leaf_state(), matching bridge/realtime_engine.py's own
    // _tx_loop() order exactly (compose first, then sequencer.tick()).
    LeafState leaf_state;
    compose_leaf_state(&leaf_state);

    uint8_t charge_authorized = (g_rz_state.charge_permission_input.last_update_tick != 0)
                                 && (g_rz_state.charge_permission_input.value != 0.0f);
    uint32_t timing_ms = advance_phase(now, g_mgmt_state.staleness_hard_cut, charge_authorized);

    if (phase == BRIDGE_PHASE_STOPPED)
    {
        return;   // just transitioned to stopped (or already was) - nothing to send this tick
    }

    uint8_t in_startup = (phase == BRIDGE_PHASE_STARTUP) ? 1u : 0u;

    for (uint8_t idx = 0; idx < (uint8_t)TXID_COUNT; idx++)
    {
        uint32_t arb_id = TX_IDS[idx].arb_id;

        // Which IDs exist at all for this vehicle config (compile-time
        // fixed - bridge/leaf_signals.py's hvbat_ids_for()).
        if (arb_id == 0x1C2u && !BRIDGE_CFG_BATTERY_GEN_IS_ZE1) { continue; }
        if (arb_id == 0x5EBu && !BRIDGE_CFG_BATTERY_GEN_IS_ZE1) { continue; }
        if (arb_id == 0x1EDu && !(BRIDGE_CFG_BATTERY_GEN_IS_ZE1 && BRIDGE_CFG_BATTERY_IS_62KWH)) { continue; }

        if (phase == BRIDGE_PHASE_WINDING_DOWN && !id_active_during_winddown(arb_id, timing_ms))
        {
            continue;
        }

        if (arb_id == 0x1C2u)
        {
            // heartbeat is immediate from bus-wake, no start-offset gate
        }
        else if ((arb_id == 0x1DBu || arb_id == 0x1DCu) && phase == BRIDGE_PHASE_STARTUP
                 && timing_ms < (uint32_t)BRIDGE_PROTO_T_1DB_START)
        {
            continue;
        }
        else if ((arb_id == 0x55Bu || arb_id == 0x5BCu) && phase == BRIDGE_PHASE_STARTUP
                 && timing_ms < (uint32_t)BRIDGE_PROTO_T_55B_START)
        {
            continue;
        }
        else if ((arb_id == 0x59Eu || arb_id == 0x5C0u || arb_id == 0x5EBu) && phase == BRIDGE_PHASE_STARTUP
                 && timing_ms < (uint32_t)BRIDGE_PROTO_T_59E_START)
        {
            continue;
        }

        if ((int32_t)(now - next_due_tick[idx]) < 0)
        {
            continue;   // not due yet
        }
        next_due_tick[idx] = now + TX_IDS[idx].period_ms;

        uint8_t frame_data[8];
        uint8_t dlc = build_frame_for_id(arb_id, &leaf_state, timing_ms, in_startup, frame_data);
        if (dlc > 0u)
        {
            CAN_FRAME frame;
            frame.ID = arb_id;
            frame.dlc = dlc;
            frame.ide = 0u;
            frame.rtr = 0u;
            for (uint8_t i = 0; i < dlc; i++) { frame.data[i] = frame_data[i]; }
            PushCan(MYCAN2, CAN_TX, &frame);
        }
    }
}

uint8_t bridge_sequencer_should_sleep(void)
{
    // Never sleep mid-sequence - only idle/waiting_for_wake (nothing being
    // actively timed or transmitted) may enter WFI. Wakes on any CAN RX
    // interrupt (main.c), matching the "any CAN activity" policy from the
    // STM32 port plan - not one fixed keepalive ID like the old Mini-Cooper
    // reference project used.
    if (phase != BRIDGE_PHASE_IDLE && phase != BRIDGE_PHASE_WAITING_FOR_WAKE)
    {
        return 0u;
    }
    uint32_t idle_ms = HAL_GetTick() - last_activity_tick;
    uint32_t timeout_ms = (uint32_t)(BRIDGE_CFG_ET_SLEEP_IDLE_TIMEOUT_S * 1000.0f);
    return (idle_ms > timeout_ms) ? 1u : 0u;
}
