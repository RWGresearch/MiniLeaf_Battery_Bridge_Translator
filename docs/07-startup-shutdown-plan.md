# Startup / Shutdown Plan

Ported from `Refrance/Leaf_BMS_Emulator/battery emulator overview/04-startup-sequence.md` and
`06-shutdown-and-stop-conditions.md` — confirmed on real ZE1 (40kWh) hardware 2026-07-30, and
explicitly called out by the user as a **hard requirement for this project**, not just a nice-to-
have replication. Ported essentially as-is; the only new consideration is how it interacts with
this project's known-good startup cache (`06-realtime-engine-and-watchdog.md`).

## Headline: there is no request/response handshake

The Leaf's HVBAT node never gets queried to start up — it starts transmitting on its own timer the
moment it sees the bus wake, sending placeholder "signal invalid" values while internally
completing precharge, then switching to normal content. **The staged bring-up timing itself *is*
the handshake.**

## Startup timeline (t=0 = first VCM traffic detected on the Leaf bus, i.e. `0x1C2`'s own start)

| t (ms) | Event |
|---|---|
| 0 | `0x1C2` starts, immediately, no delay — trigger: VCM traffic appearing on the bus. Every other offset below is measured from this same moment, not from a separate "bus wake" instant. |
| 65 | `0x1DB` + `0x1DC` start, phase A (invalid placeholders) |
| 155 | `0x55B` + `0x5BC` start — SOC/SOH valid immediately, other fields invalid |
| 565 | `0x59E` + `0x5C0` start, **fully normal from their first frame** — no invalid phase |
| 865 | `0x1DB`/`0x1DC` limits/voltage become fully valid |
| 915 | `0x5BC` GIDS becomes valid (last field to clear invalid) |
| 2105 | `0x1DB` failsafe status switches from forced startup value 0 to normal running value |

**Corrected 2026-08-03 (`docs/13` item 14.2)** — this table previously listed `0x1C2` at t=60ms.
Traced to the source: the Leaf project's own `HVBAT_PowerUp_Handshake_Report.md` §2 measures from
the very first frame in that specific `.trc` recording (including *other* ECUs' one-shot alive
frames, which arrive before the VCM's real 10ms stream) and observes `0x1C2` at "+60ms" in THAT
recording's own absolute timeline — but its §4 summary, and `04-startup-sequence.md`'s own summary,
both say to **"immediately start 0x1C2"** the instant real bus traffic is detected, with no
separate gate/delay constant, and `04-startup-sequence.md`'s own named-constants list
(`T_1DB_START=65`/`T_55B_START=155`/etc., "all milliseconds from bus wake / `0x1C2` start" — i.e.
those two are meant to be the same instant) never includes a `T_1C2_START` either. The "+60ms"
figure is an artifact of when VCM traffic happened to first appear in that one recording relative
to an earlier absolute start point, not a deliberate timing gate the real battery or the emulator
enforces. `bridge/realtime_engine.py`'s `_tx_loop()` already sends `0x1C2` immediately (no
start-offset gate) — the code was correct; this table was the stale side.

`0x1DB` walks phases A→B→C→D→E automatically on this wall-clock schedule — no external condition
needed to advance. Full byte-level phase content is in the Leaf project's own
`04-startup-sequence.md`; port the Leaf project's own builder functions rather than
re-deriving this by hand (see `03-target-signals-leaf.md`'s note on frame builders).

**Why this matters for this bridge specifically**: with the known-good startup cache
(`06-realtime-engine-and-watchdog.md`), the SOC/SOH fields that go valid early (155ms) can use a
real cached value from the moment they go valid, instead of an arbitrary placeholder — this is a
meaningful fidelity improvement over the original Leaf project (which only ever had GUI-slider
defaults at cold start, not real last-session battery data).

**Bench-test shortcut** (confirmed safe in the Leaf project's "Autostart OFF" mode): if strict
fidelity isn't needed, it's fine to skip straight to normal/valid content the instant the bus is
detected alive. Keep this as a configurable mode, but the staged sequence is the default and the
one the user confirmed as required.

**Implementation note (2026-07-31 correction)**: this "wait for bus wake" trigger is now the
actual gate, not just documentation intent — an earlier pass had the staged sequence begin timing
from *app launch* instead, which meant it ran through startup regardless of whether the Leaf side
was even connected. It's now driven by the GUI's **Start Bridge** control
(`06-realtime-engine-and-watchdog.md`, section 0): pressing Start arms the engine into a
`waiting_for_wake` state that sends nothing at all until the Leaf-facing connection actually sees
incoming traffic, at which point `t=0` above is set to that real moment, not to when the button
was pressed.

## Shutdown

A real HVBAT node never announces shutdown — every ID's last frame is fully normal content right
up until frames stop.

### Staged power-down sequence (content while winding down)

| t (ms from shutdown start) | What happens |
|---|---|
| 0 | `0x59E`, `0x5C0` (and `0x5EB` if ZE1) stop immediately |
| +250 | `0x55B`, `0x5BC` stop |
| +300 | `0x1DB`, `0x1DC` stop |
| +1200 | `0x1C2` goes silent — bus fully quiet |

Every message still transmitting during wind-down uses fully normal, currently-valid content —
no "shutting down" flag exists.

### Four independent triggers for starting wind-down

Implement all four — each was added upstream after a specific real-world failure mode the others
didn't cover. Every timing value below (0.5s/10s quiet+delay, 10s grace, 3.0s charge-end, 15s stall)
is GUI-editable as of 2026-08-14 (`state.engine_timing`, "Timing" tab) - was a bare
`leaf_signals` module constant before that (`ignition_quiet_s`/`ignition_off_delay_s`/
`ignition_grace_s`/`chg_end_stop_s`/`chg_stall_timeout_s` respectively) - see
`06-realtime-engine-and-watchdog.md` section 1c. None of these are ported/confirmed real-Leaf
protocol values (unlike the startup-timeline/shutdown-staging tables above/below, which stay fixed
in code), they're this bridge's own detection heuristics, hence editable:

1. **Ignition-off detector** — watch `0x108`/`0x1CB`/`0x284`; once all three have been seen at
   least once and are quiet for 0.5s, wait 10s, then wind down (cancel if any reappears). 10s grace
   period from session start before this detector may fire at all.
2. **Charge-active hold** — suppress the ignition-off detector while `0x1F2` shows an active charge
   request. **Extended 2026-07-31** (user directive, alongside `06`'s charger-request ramp feature):
   this only holds off wind-down while RZ450e's own `charge_permission_input` interlock (`0x358`)
   also authorizes the request (`ShutdownSequencer.tick()`'s new `charge_authorized` parameter,
   default `True` so direct unit tests of the class are unaffected). An active-but-unauthorized
   request falls straight into trigger 3 below instead of holding wind-down off indefinitely — the
   Leaf is free to keep asking, but that alone is no longer a reason for the bridge to stay awake.
3. **Charge-session-end stop** — a pure charge-plug wake carries no run-state IDs at all, so
   ignition-off can never fire for it. Once a charge context has been active and then ends (or, as
   of 2026-07-31, is active but unauthorized per trigger 2's extension) with no fresh run-state IDs,
   wait 3.0s, then wind down.
4. **Charge-negotiation-stall timeout** — if `0x1F2` is present but charge never activates and its
   `trans` value settles for 15s with no fresh run-state IDs, wind down anyway.

### Re-arm / re-wake

After any wind-down, wait for the bus to be genuinely quiet for 1.0s before arming to detect the
next wake — otherwise an already-in-flight VCM frame can instantly re-trigger the wake detector.

**Bug found and fixed 2026-07-31**: this bridge's `ShutdownSequencer.tick()` originally re-armed
after a flat `PWRDOWN_DEFAULT_COOLDOWN_S` (1.0s) had elapsed since *entering* `'stopped'`, not
after the bus had actually gone quiet — meaning a VCM that was still transmitting right through
the cooldown window would get its very next frame instantly re-trigger the wake detector, undoing
the shutdown. This is precisely the bug Leaf_BMS_Emulator hit in their own rev 20 and fixed in rev
21, confirmed against a real capture (`Trace_..._emulation_poweroff.trc`) showing zero RX gaps
>100ms across two power-down button presses. Fixed by tracking `last_leaf_rx_t` (updated on every
Leaf-bus frame, not just ignition/charge IDs) and requiring `now - last_leaf_rx_t >=
PWRDOWN_DEFAULT_COOLDOWN_S` before re-arming — re-checked every tick, so it correctly waits
indefinitely if the bus never truly quiets, rather than re-arming on a timer regardless. Verified
directly (`tests/test_shutdown_sequencer.py`): simulating a still-chattering bus does *not* re-arm
even past the old flat-timer window; a genuinely quiet bus does.

### LB_RefusetoSleep (0x55B byte 6, bits 5–6) — bug found and fixed 2026-07-31

Was hardcoded to `refuse_sleep=0` (always "ignition on") in every `build_55b()` call — see
`03-target-signals-leaf.md`. Leaf_BMS_Emulator confirmed via a real capture that this bit tracks
ignition state directly (0 while the car is on, flips to 1 within ~150ms of key-off) and forces it
to 1 unconditionally during their staged power-down (which only ever runs on/after a key-off).
Fixed with `ShutdownSequencer.refuse_sleep_value(now)`: derives `0`/`1` from the same run-state-
freshness check the ignition-off detector already uses during normal operation, forces `1` during
`winding_down`. This bridge was previously telling the Leaf's other ECUs "ignition is on" forever,
even after fully winding down — a plausible contributor to the vehicle not sleeping/idling down
correctly. Verified directly (`tests/test_shutdown_sequencer.py`): 0 while ignition IDs are fresh,
1 once they go stale or are never seen, 1 forced during `winding_down` regardless of freshness.

### This bridge's specific staleness/shutdown interaction

This project adds a **fifth** trigger not present in the original Leaf project: the staleness
watchdog's hard-cut escalation (`06-realtime-engine-and-watchdog.md`) — if RZ450e data has been
stale for 65+ seconds, wind down via `relay_cut_request` regardless of what the four Leaf-side
triggers are doing. This is specific to the bridge context (the original Leaf emulator never had
an upstream "battery" data source that could itself go stale).

### Sixth trigger (bridge-specific, defensive)

Added 2026-08-06, after a real bench test (`logs/minileaf_20260805_200831_Charging to xx% the
restart.trc`) showed the bridge transmitting continuously for 100+ seconds with no wind-down, in a
case that couldn't be fully root-caused from the captured traffic alone (see `docs/10-open-
questions.md`). **Not** a ported/confirmed real-Leaf behavior like triggers 1-4 above, and not
positioned as a fix for that specific unexplained case — it's a defensive fallback for the general
structural gap: a bench rig with no ignition wiring can only ever wind down via triggers 2/3/4
(all charge-session-based); if a real run ever lands in a state where none of those resolve, for
any reason, there was previously no way out short of Stop Bridge.

**Bus silence timeout** — if the Leaf bus goes completely silent (no frame of any ID, not just
ignition/charge IDs) for `bus_silence_timeout_s` (30.0s default, deliberately well above every
other trigger's own timeout so it never preempts a legitimate slower condition) while in `startup`
or `running`, wind down. GUI-editable as of 2026-08-14 (`state.engine_timing`, "Timing" tab -
see `06-realtime-engine-and-watchdog.md` section 1c) - was a bare `leaf_signals.BUS_SILENCE_
TIMEOUT_S` module constant before that. Implemented in `ShutdownSequencer._should_wind_down()`, reusing the
already-tracked `last_leaf_rx_t` field (no new state). **Documented, not yet confirmed** against a
re-test — see `docs/11-manual-verification-checklist.md`.

### Bridge/mirror auto-stop

Not directly applicable in the same form as the original Leaf project (which bridged a real Leaf
battery to a real Leaf car) — this project's "source" is the RZ450e pack, not a second Leaf
battery. The staleness watchdog above is this project's equivalent concern.

## Source of truth

Snapshot of the Leaf project's confirmed, real-hardware-tested startup/shutdown behavior. If that
project's own testing later refines these numbers, update this file to match.
