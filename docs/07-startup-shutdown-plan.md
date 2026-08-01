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

## Startup timeline (t=0 = first VCM traffic detected on the Leaf bus)

| t (ms) | Event |
|---|---|
| 60 | `0x1C2` starts (trigger: VCM traffic appearing) |
| 65 | `0x1DB` + `0x1DC` start, phase A (invalid placeholders) |
| 155 | `0x55B` + `0x5BC` start — SOC/SOH valid immediately, other fields invalid |
| 565 | `0x59E` + `0x5C0` start, **fully normal from their first frame** — no invalid phase |
| 865 | `0x1DB`/`0x1DC` limits/voltage become fully valid |
| 915 | `0x5BC` GIDS becomes valid (last field to clear invalid) |
| 2105 | `0x1DB` failsafe status switches from forced startup value 0 to normal running value |

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
didn't cover:

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

### Bridge/mirror auto-stop

Not directly applicable in the same form as the original Leaf project (which bridged a real Leaf
battery to a real Leaf car) — this project's "source" is the RZ450e pack, not a second Leaf
battery. The staleness watchdog above is this project's equivalent concern.

## Source of truth

Snapshot of the Leaf project's confirmed, real-hardware-tested startup/shutdown behavior. If that
project's own testing later refines these numbers, update this file to match.
