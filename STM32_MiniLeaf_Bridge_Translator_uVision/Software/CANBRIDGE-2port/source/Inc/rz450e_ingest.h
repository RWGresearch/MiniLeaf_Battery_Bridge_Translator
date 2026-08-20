/* RZ450e broadcast CAN ingest (STM32 port Phase 3).
 *
 * Ports the broadcast half of bridge/rz450e_signals.py - the 10 raw CAN IDs
 * this project reads directly off the RZ450e's own CAN bus (fast, primary
 * source for everything safety-relevant, per docs/05-battery-management-
 * safety.md's "real-time per-cell voltage is the sole authoritative signal"
 * rule). The slow UDS/DID-polled half (SoC, capacity, primary V/I
 * cross-check, DID 0x1814 temp probes) is a separate module, rz450e_uds.c/.h,
 * built in Phase 6 - see the STM32 port plan for why that's deliberately
 * later (everything here already degrades gracefully without it).
 */
#ifndef RZ450E_INGEST_H
#define RZ450E_INGEST_H

#include <stdint.h>
#include "can.h"

/* One decoded input signal: its latest accepted value, and the HAL_GetTick()
 * millisecond timestamp it was last accepted at. last_update_tick == 0 means
 * "never received a valid reading this session" - a signal ages from the
 * bridge's own boot, exactly like the Python app's SharedState (docs/06),
 * not from some earlier session or a fabricated zero value. A rejected
 * (implausible, or checksum-failed) reading is simply never written here, so
 * the existing value keeps aging - the staleness watchdog (Phase 4,
 * management_engine.c) is what actually catches sustained rejection, same
 * design as bridge/rz450e_signals.py's validate_inputs(). */
typedef struct {
    float value;
    uint32_t last_update_tick;
} RzSignal;

/* All RZ450e input signals this bridge reads from broadcast CAN. Field names
 * match bridge/rz450e_signals.py's dict keys (minus the cell_NN/temp_NN
 * numeric suffixes, which become array indices instead - cell[0] is cell_01,
 * temp[0] is temp_01, etc.) so this struct can be read side-by-side with the
 * Python source it was ported from. */
typedef struct {
    /* 0x020 - whole-pack voltage + pack-level cell min/max. The min/max pair
     * here is a SANITY CHECK ONLY (docs/05) - the per-cell array below,
     * decoded straight off 0x4A9/0x4C0, is the sole authoritative source for
     * every safety cutoff/taper. */
    RzSignal pack_v;
    RzSignal cell_min;
    RzSignal cell_max;

    /* 0x023 - pack current, two independently-sampled sensor taps. */
    RzSignal current;
    RzSignal current_b;

    /* 0x4A7 - pack-level temperature extremes (hottest/coldest probe value
     * AND which probe number reported it). Also sanity-checked against the
     * 16-probe array below (docs/05's temp_data_cross_check). */
    RzSignal temp_max;
    RzSignal temp_min;
    RzSignal temp_max_probe;
    RzSignal temp_min_probe;

    /* 0x358 - the RZ450e's own charging-interlock signal. A missing/never-
     * seen reading here (last_update_tick == 0) must always be treated as
     * "charging not permitted" by every consumer - see docs/05's design
     * philosophy note; this module only decodes the raw bit, it does not
     * enforce that fail-safe rule itself (that's Phase 4's job). */
    RzSignal charge_permission_input;
    RzSignal alive_358;   /* 4-bit alive counter riding alongside 0x358 */

    /* DID 0x1F5B (UDS, slow ~4-9s poll) - state of charge. NOT populated by
     * this module (rz450e_ingest.c only decodes broadcast CAN) - stays at
     * last_update_tick == 0 ("no data") until Phase 6 (rz450e_uds.c) exists
     * and starts polling it. Every Phase 4 safety feature that reads this
     * already degrades gracefully with no SoC data (matches
     * bridge/management_engine.py exactly: SoC is a backup/smoothing input
     * everywhere, never the sole trigger for anything - see docs/05), so
     * leaving it permanently "no data" through Phases 4-5 is a safe,
     * intentional temporary state, not a bug. */
    RzSignal soc_pct;

    /* DID 0x1D3E (capacity/SOH per sub-pack) and DID 0x1F9A (primary V/I
     * cross-check reference) - same "not populated until Phase 6" status as
     * soc_pct above. capacity_pack1_ah is what mapping_engine.c's GIDS/QC
     * capacity derivation actually needs (falls back through pack2-4 if
     * pack1 is unavailable, matching bridge/mapping_engine.py's
     * derive_capacity_outputs()); primary_pack_v/primary_current_a have no
     * active consumer yet (cross-check/display only per docs/02), included
     * for completeness. */
    RzSignal capacity_pack1_ah;
    RzSignal capacity_pack2_ah;
    RzSignal capacity_pack3_ah;
    RzSignal capacity_pack4_ah;
    RzSignal primary_pack_v;
    RzSignal primary_current_a;

    /* 0x3F1 / 0x424 - free-running alive counters, useful only as a
     * watchdog "is this bus still moving" signal (docs/06), not as physical
     * data - no plausibility range applies to either. */
    RzSignal alive_3f1;
    RzSignal counter_5s;

    /* 0x4A9 / 0x4C0 - all 96 individual cell voltages, 4 cells per frame,
     * round-robin muxed by the battery itself (this bridge just accepts
     * whichever base index arrives - no receive-side sequencing needed).
     * Index 0 = cell 1 ... index 95 = cell 96. THE authoritative voltage
     * source for every safety feature in Phase 4. */
    RzSignal cell[96];

    /* Temp probes 1-16, three parallel views (Phase 6 - DID 0x1814 primary /
     * 0x4AA CAN backup, docs/02):
     *   temp_can[16]  - always written on every 0x4AA CAN frame (backup source)
     *   temp_did[16]  - always written on every DID 0x1814 response (primary source)
     *   temp[16]      - the EFFECTIVE/front-door value every consumer (mapping_engine.c,
     *                   management_engine.c, over_temperature_derate etc.) actually reads:
     *                   DID always wins the instant a response arrives (rz450e_uds.c writes
     *                   it directly); rz450e_ingest.c's CAN decode only promotes temp_can into
     *                   this array when the DID side hasn't produced a fresh-enough reading
     *                   within BRIDGE_CFG_ET_DID_TEMP_FRESH_WINDOW_S (checked via temp_did[0]'s
     *                   age as a proxy for the whole last DID batch, matching bridge/
     *                   realtime_engine.py's _ingest_rz_bus ID_TEMPS branch exactly - all 16
     *                   probes arrive together in one 0x1814 response, so one shared freshness
     *                   check is correct, not an approximation). Index 0 = probe 1 ... index 15
     *                   = probe 16 in all three arrays. */
    RzSignal temp[16];
    RzSignal temp_can[16];
    RzSignal temp_did[16];

    /* Running count of frames rejected for failing their Toyota additive
     * checksum (docs/02's 5 checksum-bearing IDs) - data-integrity
     * bookkeeping, not itself a decoded physical signal. */
    uint32_t checksum_failures;
} RzState;

/* The single shared instance - every other module reads this directly
 * (matches bridge/state.py's SharedState being the one place RX and
 * everything downstream meet). No lock is needed here the way the Python
 * app needs one: everything on this MCU runs on one core, one thread of
 * execution (main loop + ISRs, no RTOS), and CAN_FRAME handoff out of the
 * ISR already goes through can.c's own IRQ-safe ring queue. */
extern RzState g_rz_state;

/* Call once at startup, before the main loop begins draining CAN1. */
void rz450e_ingest_init(void);

/* Call for every RX frame received on the RZ450e bus (CAN1 / MYCAN1). Not
 * safe to call from an ISR - call it from the main loop, same as every
 * other bridge_sequencer_on_frame() consumer (can.c's ISRs only ever push
 * raw frames onto the queue, they never decode). */
void rz450e_ingest_frame(CAN_FRAME *frame);

#endif /* RZ450E_INGEST_H */
