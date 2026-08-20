/* Top-level bridge orchestration - the only module main.c talks to directly
 * (STM32 port Phase 5).
 *
 * Ports bridge/realtime_engine.py's ShutdownSequencer (the staged startup/
 * shutdown state machine, all 6 wind-down triggers) and the TX loop's own
 * per-ID period/start-offset/wind-down gating - see docs/07-startup-
 * shutdown-plan.md. Composes each tick's Leaf output via mapping_engine.c ->
 * management_engine.c -> leaf_output.c, exactly mirroring bridge/
 * realtime_engine.py's _compose_leaf_state() pipeline.
 *
 * One deliberate scope gap, flagged clearly rather than silently dropped:
 * bridge/realtime_engine.py's _apply_charge_ramp() (the charger-request
 * ramp emulation + its notify_charge_replug() trigger) is NOT ported here.
 * charger_limit_kw/charge_limit_kw jump directly to the mapped/management
 * value during an active charge session instead of ramping smoothly, and a
 * latched hard-cut can currently only clear via a genuine session
 * restart (bus re-wake), not a simple unplug/replug. This errs safe (stays
 * cut off longer, never resumes power it shouldn't) - see the STM32 port
 * session notes for the full rationale and what a follow-up port would need.
 *
 * No GUI/operator "Start Bridge" button exists on this hardware (always
 * live and ready, per this port's own design intent) - bridge_sequencer_
 * init() arms immediately at boot, skipping the Python app's manual 'idle'
 * phase entirely.
 */
#ifndef BRIDGE_SEQUENCER_H
#define BRIDGE_SEQUENCER_H

#include "can.h"

/* Call once at startup, after CAN init/filters are armed. */
void bridge_sequencer_init(void);

/* Call for every RX frame popped off either CAN queue (can_bus is MYCAN1/
 * MYCAN2 from can.h). Phase 2: only bumps the idle-activity timestamp used
 * by bridge_sequencer_should_sleep() below - no decode happens yet. */
void bridge_sequencer_on_frame(uint8_t can_bus, CAN_FRAME *frame);

/* Call once per main-loop iteration, after RX draining and before TX. */
void bridge_sequencer_tick(void);

/* Returns nonzero if the main loop should enter WFI sleep this iteration. */
uint8_t bridge_sequencer_should_sleep(void);

#endif /* BRIDGE_SEQUENCER_H */
