/* RZ450e UDS/DID polling (STM32 port Phase 6).
 *
 * Ports the slow half of bridge/rz450e_signals.py (the DID/ISO-TP client)
 * and bridge/realtime_engine.py's _did_poll_loop(): a request/response
 * state machine over UDS ReadDataByIdentifier (service 0x22), Toyota
 * extended addressing (0x747 tester -> 0x74F BMS response), including a
 * real ISO-TP (ISO 15765-2) multi-frame reassembly for responses longer
 * than 7 bytes. New engineering with no reference-project precedent - the
 * Mini-Cooper reference project never did UDS.
 *
 * Deliberately built LAST among the core modules (see the STM32 port plan):
 * every consumer of this data already degrades gracefully without it -
 * SoC's taper-blend factor defaults to 1.0 (docs/05), temp probes fall back
 * to the 0x4AA CAN backup (rz450e_ingest.h), and the primary-V-I/capacity
 * readings have no safety-critical consumer at all (cross-check/GIDS-
 * display only). This module adds fidelity, it does not unblock core
 * safety behavior.
 *
 * Two logical polls, run SEQUENTIALLY through one shared request/response
 * channel (matching the real hardware - the RZ450e's diagnostic responder
 * can only field one outstanding request at a time, and Python's own
 * _did_poll_loop is a single thread doing one then the other, never
 * concurrently):
 *   1. A 3-item round robin: SoC (0x1F5B) -> capacity (0x1D3E) ->
 *      primary V/I (0x1F9A) -> repeat.
 *   2. A separate temp-probe poll (0x1814, all 16 probes at once), gated on
 *      its own BRIDGE_CFG_ET_DID_TEMP_POLL_INTERVAL_S interval, checked
 *      once per round-robin cycle rather than folded into it (a 4th
 *      round-robin slot would slow the other three for no benefit, per
 *      docs/02's own measured ~9s/poll figure for the 3-item cycle).
 */
#ifndef RZ450E_UDS_H
#define RZ450E_UDS_H

#include "can.h"

/* Call once at startup. */
void rz450e_uds_init(void);

/* Call for every RX frame received on the RZ450e bus (CAN1 / MYCAN1) -
 * ignores everything except the Toyota UDS response ID (0x74F); safe to
 * call unconditionally alongside rz450e_ingest_frame() for the same frame,
 * their ID sets never overlap. */
void rz450e_uds_on_frame(CAN_FRAME *frame);

/* Call once per main-loop iteration. Advances the request/response state
 * machine: sends the next request when idle, checks for a completed
 * response or a timeout when awaiting one, paces requests apart by
 * BRIDGE_CFG_ET_DID_INTER_REQUEST_GAP_S. */
void rz450e_uds_tick(void);

#endif /* RZ450E_UDS_H */
