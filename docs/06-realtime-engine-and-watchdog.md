# Real-Time Engine, Startup Cache, and Staleness Watchdog

Three related architectural requirements, called out explicitly by the user, that make this
bridge behave like a real BMS/ECU rather than a simple pass-through relay.

## 0. Manual Start/Stop gate + real bus-wake trigger (added 2026-07-31)

**The engine does not transmit anything the instant the app launches.** Two independent phases
exist before the staged startup sequence (section below / `07-startup-shutdown-plan.md`) even
begins:

- **`idle`** — the default state from app launch. RZ450e monitoring (ingest, DID polling) and
  mapping/threshold edits all work normally in this state — the GUI's **Start Bridge** button is
  exactly for letting the user connect adapters and build/verify their mapping before anything
  gets sent to the Leaf bus. Nothing is composed, nothing is sent.
- **`waiting_for_wake`** — entered when **Start Bridge** is pressed. The engine is armed but still
  sends nothing; it's waiting for the Leaf-facing connection to see **any real incoming CAN
  traffic** (a proxy for "the VCM just woke up"), matching the real hardware's own trigger
  condition documented in `07-startup-shutdown-plan.md` — not a wall-clock timer from when the
  button was pressed. The staged startup sequence's `session_start` is only set at the moment real
  traffic is actually seen.
- **Stop Bridge** halts transmission immediately (back to `idle`) — a manual control for
  reconfiguring, distinct from **Simulate power-down** (the graceful staged wind-down while
  running).

This closes a real gap from the initial implementation: the engine originally started transmitting
immediately at app launch, timed from process-start rather than from real bus activity, and — as a
direct consequence — the staleness watchdog (section 3 below) couldn't distinguish "RZ450e was
never connected" from "RZ450e was connected, then went stale," so it would falsely trip a soft cut
after 60s and a hard cut after 65s even with zero hardware ever attached. Both are fixed together:
`SharedState.counter_stale_age()` now returns `None` (not a session-start fallback) for a counter
that's never been seen, and the watchdog excludes `None` ages from its "worst age" calculation —
consistent with the same "never seen = nothing to check" principle already applied to the fast
raw-CAN signals.

Mapping and battery-management threshold edits are **live in either state** — `RealtimeEngine`
reads the same `MappingEngine`/`ManagementEngine` objects the GUI edits directly, re-evaluated on
every tick, so there is no separate "apply changes" step to remember.

## 1. Fixed-period TX, decoupled from RX arrival

**The Leaf-facing transmit loop must send every HVBAT CAN ID at its exact real-world period, on a
fixed wall clock, always — never "only when new RZ450e data arrived."** This is a hard
requirement: the Leaf's VCM expects `0x1DB` etc. every 10-100ms unconditionally; if the bridge only
forwarded data reactively, any RZ450e hiccup or slow DID poll would directly stall the Leaf bus and
likely trip the VCM's own timeout logic.

**Architecture** (directly mirrors `leaf_hvbat_emulator.py`'s own `_tx_loop`):
- An **RX/ingest side** continuously updates a single shared current-state model as RZ450e frames
  and DID/PID responses arrive — this side runs at whatever cadence the data actually shows up.
- A **separate, precisely-timed TX loop** reads the latest state model and builds/sends each Leaf
  frame on its own fixed period. Zero coupling between the two cadences — the TX loop never blocks
  on or waits for RX.
- Internally-generated fields (PRUN, toggle bits, mux cycles, heartbeats — see
  `03-target-signals-leaf.md`) are driven by the TX loop's own counters, same as the Leaf project
  already does, unaffected by RX timing entirely.

## 2. Known-good startup memory

On app startup, before any live RZ450e data has arrived, the TX loop must already have something
reasonable to send — not zeros or blanks, which could look like a real fault condition to the
Leaf's VCM.

- **Persist last-known-good values to disk** between sessions (separate from the mapping/threshold
  config file — this is *the data itself*, not *what to do with the data*). Load them at startup as
  the initial state.
- The moment a field's live RZ450e source starts arriving, switch that field over to live data
  immediately — the cached value is a startup bridge, not a fallback to keep using once live data
  exists.
- This directly enables the staged Leaf startup sequence (`07-startup-shutdown-plan.md`) to have
  a real SoC/voltage/etc. to report from its very first valid frame, rather than an arbitrary
  placeholder.

## 3. Staleness watchdog

Tracks freshness of **every registered input signal** (docs/13 item 1.1, fixed 2026-08-01) — all 96
per-cell voltages, all 16 temp probes, and every fast/slow scalar, not a hand-picked subset — plus
the keep-alive counters the RZ450e project confirmed are usable for exactly this purpose
(`02-source-signals-rz450e.md`):
- `0x358` and `0x3F1` rolling `alive_counter`s (the latter 4-bit, wraps every 16 frames)
- `0x424` 5-second tick counter
- Toyota's additive checksum on the 5 confirmed checksum-bearing messages (`0x020`/`0x023`/`0x358`/
  `0x3F1`/`0x424`) — wired in as a frame-level rejection at ingest (docs/13 item 13.5, fixed
  2026-08-03), not just a freshness signal: a mismatch means the frame is corrupt and it's dropped
  before decoding, the same as a plausibility-check rejection. Both this checksum check and the
  input-plausibility check (`rz450e_signals.validate_inputs()`, `05-battery-management-safety.md`)
  are toggleable as of the same day (`checksum_validation`/`input_validation`, docs/13 items
  15.14/15.15, default ON, Battery Management tab) — for deliberately testing what happens
  downstream with bad data, not something to leave off during real operation.

**"Never seen at all" ages from the bridge's own first tick, not excluded forever** (docs/13 item
13.1a, fixed 2026-08-03): a signal that has never arrived this session starts aging from the moment
`ManagementEngine.apply()` is first called (i.e. the instant the sequencer reaches `startup`, real
bus wake) — a strictly worse case than "went stale after being live" must not get a strictly
*weaker* response (none at all). Only a signal that's never been seen even once at app launch, with
zero prior session data cached, sits at "age 0" the very first tick — from then on it ages exactly
like any other signal would.

**Two-stage escalation** (per user instruction):
1. At **60 seconds** stale for any required input → **soft cut** (`capacity_empty`), and — per a
   2026-08-03 clarification — **also forces `full_charge_flag = 1`, `charge_limit_kw = 0`,
   `charger_limit_kw = -10`** (idle-stop): a soft cut elsewhere in the management layer (e.g. a
   depleted-battery low-voltage cutoff) deliberately does NOT block charging, since that's exactly
   what a depleted pack needs — but stale *data itself* means we can no longer verify it's safe to
   accept charge/regen at all, so this specific trigger stops charging outright using the same
   confirmed real-hardware "instant stop, needs a fresh charge request to retry" bit every other
   charge-block path already uses. Most transient CAN hiccups should resolve well before 60s.
2. If still stale **5 seconds later** (65s total) → escalate to **hard cut**
   (`relay_cut_request`). This is a genuine "we can no longer safely read the battery" condition —
   distinct from the routine threshold-based protective features in
   `05-battery-management-safety.md`, which is why it gets the hard-cut/RED-error treatment on
   escalation rather than staying soft indefinitely.

### Charging's stricter requirement is a STARTUP gate, not a second watchdog (added 2026-08-03)

Driving is allowed to run on cached/last-known-good values until this watchdog's 60s/+5s schedule
would object. Charging must not start on cached/default values at all — `require_live_data_to_charge`
(default ON, Charge Emulation tab) blocks the charge ramp from starting until every one of the 96
per-cell voltages plus the pack's temp extremes has been seen **live during the current bridge
session** (`RealtimeEngine._charge_data_ready()`). This is a one-time "has real data arrived this
session" check, not a second freshness timer running in parallel — a separate, shorter, ongoing
custom timer for charging was the first version of this fix and was explicitly rejected in favor of
reusing this exact watchdog for everything that happens *after* the gate passes. Once live data has
arrived once this session, this gate stays satisfied regardless of the data's age from then on;
ongoing protection for a charge session that later goes stale is entirely this watchdog's job
(including the `full_charge_flag` behavior in stage 1 above), same as driving.

**"This session" precisely means since the current `waiting_for_wake → startup` transition**
(`ShutdownSequencer.get_session_start()`), not since the app process launched — bug found
2026-08-04. `SharedState` lives for the whole app run and its per-signal timestamps are never
cleared (deliberately — see the note on `age_of()`/`ages_of()` below), so the first version of this
gate checked "has this signal EVER updated since the app started," which meant a single live
contact early in a long-running session would satisfy it **forever**, including after a full
sleep→wake cycle where RZ450e might now be completely disconnected. Fixed by comparing each
signal's last-update timestamp against the session's actual start time
(`SharedState.timestamps_of()`) instead of just checking it's non-`None`. This distinction matters
specifically because `age_of()`/`ages_of()` (the general watchdog above) are *deliberately*
app-lifetime-persistent, not session-scoped — RZ450e keeps transmitting independent of whatever the
Leaf-side sequencer is doing, so the general staleness clock correctly should **not** reset just
because the Leaf side slept and woke up. The charge-start gate is the one place that specifically
needs actual bridge-session boundaries instead.

## 4. Output clamping — the final safety net before a frame is built (added 2026-07-31)

`RealtimeEngine._compose_leaf_state()` is the single point where `DEFAULTS` + the mapping engine's
output + the management engine's overrides are combined into the value set every Leaf frame gets
built from. After the management layer runs, `leaf_signals.clamp_state()` clamps every field to its
documented `(lo, hi)` encodable range before anything reaches `_build_frame()`.

This exists because the frame builders (`bridge/leaf_signals.py`'s `build_1dc` etc.) pack values
into fixed-width CAN fields using a bitmask (e.g. `raw & 0x3FF`), which **wraps** an out-of-range
value instead of saturating it. Confirmed directly (`tests/test_output_clamping.py`): a
`discharge_limit_kw` of `-5.0` (e.g. from an arithmetic edge case) bitmask-wraps to a raw value that
decodes back to **251.0 kW — essentially full power, the exact opposite of the safety intent.**
Realistic values can also legitimately exceed a documented range without any bug at all — a 200Ah/
400V pack's derived `qc_full_wh` (`mapping_engine.derive_capacity_outputs`) computes 80,000 against
a documented max of 51,100.

`clamp_state()` returns `(clamped_state, clamped_keys)` — any field that actually needed clamping is
logged into the fault history (`bridge/fault_log.py`, key prefix `clamp_`) and printed to the Log
panel the first time it happens, rather than silently absorbed. Needing to clamp a value is itself
diagnostic information: it means something upstream (a user's mapping tie, a derived-signal
formula) is producing an out-of-spec number.

## 5. Fault history — memory across auto-clearing cuts (added 2026-07-31)

Every soft cut and monitor warning in `bridge/management_engine.py`'s curated features
**auto-clears** the moment its triggering reading recovers — matching the real Leaf's own "the
condition resolves and the dash error goes away" pattern for `capacity_empty`/`full_charge_flag`.

**Hard cuts latch, as of 2026-08-01 (`12-nmc-bms-design-research.md` finding F8, `docs/13` items
13.1/13.4).** Once any hard-cut condition fires, `ManagementEngine._hard_latched` stays asserted
even after the triggering reading recovers — a real emergency-tier condition shouldn't silently
clear itself just because a reading blipped back into range for one tick. It's cleared only by a
genuine re-arm: `notify_session_start()` (a real bus wake, `waiting_for_wake -> startup`, i.e. the
car was actually power-cycled — a `rearmed_naturally` transition specifically, NOT the bridge's own
Stop/Start Bridge button being toggled while the car's VCM never lost power) or
`notify_charge_replug()` (`charge_permission_input` genuinely absent for at least `CHG_END_STOP_S`
= 3.0s before a new request, see `05-battery-management-safety.md`'s "`full_charge_flag` re-arm"
section).

The problem auto-clearing alone creates: a brief fault could trip and self-clear entirely between
GUI status polls, with no record it ever happened. `bridge/fault_log.py`'s `FaultLog` sits inside
`ManagementEngine` and records every trigger/clear transition for every tracked condition
(`FAULT_DEFINITIONS`) plus every output-clamp event (section 4 above) — a running count, first/last
triggered, last cleared, and the live active/cleared state — surfaced in its own **Fault History
window** (`gui/fault_history_window.py`, see `08-gui-design.md`'s "Fault History window" section —
it moved through the Dashboard's right column and left list before ending up in its own
`tk.Toplevel` once there wasn't room for it alongside everything else). A per-entry **Reset** button
lets the user acknowledge/clear the recorded history for one entry — this cannot and does not force
a still-true condition to false (that value is re-derived from live sensor data every tick,
unchanged); if the condition is genuinely still active, the very next tick sees a fresh trigger and
the count starts again from 1, the same behavior a real BMS scan tool has when clearing a stored
code for a fault that's still physically present.

## 6. Charger-request ramp emulation (added 2026-07-31)

Ported from `Refrance/Leaf_BMS_Emulator`, confirmed there against real hardware (bit-level diff of
every HVBAT ID, idle vs. real charge-session captures) and gated behind a GUI checkbox
(`gui/panels.py`'s `ChargeEmulationPanel`, the "Charge Emulation" tab, `charge_emulate` — default
**on** as of 2026-08-01) — see `03-target-signals-leaf.md` for the field-level description.

`RealtimeEngine._apply_charge_ramp()` runs once per tick, inside `_compose_leaf_state()`, **before**
the management engine's per-cell overvoltage taper — so that safety feature always gets the final
say over the ramped `charger_limit_kw` value, never bypassed. It requires **all** of:

- the `charge_emulate` checkbox being on;
- a fresh, real `0x1F2` charge request from the Leaf — reuses `ShutdownSequencer.charge_active()`,
  the exact same 0x1F2 decode the shutdown triggers (section 0 above) already maintain, rather than
  duplicating it;
- RZ450e's own `charge_permission_input` interlock (`0x358`) actually granting it (user directive,
  2026-07-31) — a signal this project's own real hardware sources, independent of the Leaf-side
  request; and
- the charge-start data gate (`require_live_data_to_charge`, section 3 above) being satisfied —
  added 2026-08-03, the third/newest of the four conditions.

With both present, `charger_limit_kw` snaps from its 92.3kW idle placeholder to 0.0 kW and ramps to
the configured target at a rate set by an uprate level (0-7; level 7 = 2.0 kW/s, each level down
halves it), using a real measured `dt` — not a fixed-tick assumption like the reference project's
own steady 10ms loop, since this bridge's TX loop doesn't run on a fixed period.

**Mismatch handling**: if the Leaf wants to charge but RZ450e hasn't authorized it, this is not
"wait and see" — it forces `full_charge_flag = 1` (the confirmed "instant stop, needs a physical
replug" bit), plus `charge_limit_kw = 0.0` and `charger_limit_kw = -10.0`, instead of silently
falling back to whatever static/mapped value would otherwise apply.

**Sleep interaction**: the same mismatch feeds into `ShutdownSequencer.tick()`/`_should_wind_down()`
via a new `charge_authorized` parameter (default `True`, so any direct unit test of the class in
isolation keeps its original always-authorized behavior). Previously, `_should_wind_down` treated
*any* fresh 0x1F2 request as a reason to stay awake indefinitely (`chg_active` alone reset every
wind-down timer). Now it computes `chg_effective = chg_active and charge_authorized`: an active-but-
unauthorized request is treated the same as an ordinary charge-session end — it falls into the
existing `chg_seen_active and not chg_effective` wind-down trigger (section 0) — rather than
refusing to sleep forever just because the Leaf keeps asking with no pack permission behind it. A
genuine replug is what produces a fresh `0x1F2` request in the first place, so this doesn't lose the
ability to wake back up once actually reconnected.

## Interaction with config save/load

Four distinct persisted things, not to be conflated:
1. **Mapping/threshold config** (`profile.json`) — what signal ties to what, and every protection
   feature's thresholds. Edited deliberately by the user, saved explicitly.
2. **Last-known-good data cache** (`last_known_good.json`, this doc's section 2) — the actual last
   values seen, used purely to bridge the startup gap before live data arrives.
3. **Fault history** (`fault_log.json`, section 5 above) — a record of what already happened
   (trigger counts/timestamps), autosaved every 5s like the data cache, loaded at startup and after
   a manual profile load so it survives both app restarts and mid-session profile switches. Not a
   setting either — clearing/adjusting it (the GUI's per-entry Reset button) never changes what the
   engine actually does, only what's remembered about what it already did.
4. **STM32 export** (`09-stm32-export-format.md`) — a derived, read-only snapshot of (1), for
   porting to firmware. Never a live/runtime file.

All of `config/*.json` — (1), (2), (3), and any named snapshot profile — is git-tracked (changed
2026-08-03; previously gitignored), the user treating it as a running backup rather than purely
local/ephemeral state. Since (1) is loaded automatically at startup whenever it exists (instead of
fresh code defaults), a default changed in `bridge/management_engine.py`/`bridge/leaf_signals.py`
does **not** retroactively apply to an already-saved `profile.json` — run
`tests/check_profile_drift.py` after changing a default to see exactly what it does (or doesn't do)
to the real saved file.
