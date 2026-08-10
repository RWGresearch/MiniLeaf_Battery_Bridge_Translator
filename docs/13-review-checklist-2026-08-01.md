# Full Top-to-Bottom Review Checklist — 2026-08-01 (rev 2)

Requested review: go through the entire app for correctness and safety, with a focus on redundant/
cross-checked inputs for anything relevant to a safe shutdown. **Revision 2**: the first pass
covered the core management/GUI/docs logic directly. This pass was specifically asked to go
further — verify things not already flagged, from different angles — so it adds three more
independent review passes on top of the first: (1) a full read of every test file's actual
assertions, not just which functions they touch, (2) a line-by-line cross-check of `docs/08` and
`docs/09` against current code, (3) a dedicated concurrency/thread-lifecycle audit of the CAN
connection layer and window lifecycle, plus additional direct tracing of a few numeric edge cases.
**No code was changed — still a read-only review.**

## How to use this doc

Every item is something to **discuss, change, or verify**, not a pre-decided action list. Each has
a checkbox and a blank `Your notes:` line. This revision keeps every item from the first pass (none
were withdrawn) and adds a new concurrency/lifecycle section (Part 3) and a new test-coverage-
quality section (Part 6), plus a few items folded into existing sections. Items already reviewed in
the first pass keep their original numbering where possible so your earlier progress isn't
scrambled; new items are marked **NEW**.

---

## Part 1 — Data redundancy / cross-check findings

### 1.1 — Staleness watchdog doesn't watch the signals the cutoffs actually use
- [x] Reviewed
`staleness_watchdog` (`bridge/management_engine.py:462-494`) only tracks freshness of `pack_v,
current, cell_min, cell_max, soc_pct` plus 3 keep-alive counters. It never checks the age of the
individual `cell_01`..`cell_96` signals or any `temp_01`..`temp_16`/`temp_max`/`temp_min` signal —
the exact signals docs/05 calls "the SOLE authoritative signal for every safety cutoff." Those are
read via `get_input()` (`management_engine.py:172-177, 354`), which falls back to
`last_known_good` forever with no age check of its own. If just the CAN messages carrying per-cell
voltage/temp stopped (while `0x020`/`0x023`/`0x358`/`0x3F1`/`0x424` kept flowing — plausible here
specifically, since docs/02 describes the RZ450e as two separate logical buses merged onto one
adapter), every voltage/temp cutoff would keep computing off a frozen cached reading with the
watchdog reporting "ok." No test anywhere exercises staleness of an individual cell/temp signal.
**Your notes:**
i thought there was a 60 second stale soft cut for all cases, and a hard cut + 5 seconds after that. 
however, we do need the watchdog to check ALL incoming messages for stailness. 
and cut off soft, then hard, AND stop charging ( charge cut) for any and all stail data that we know we are 
using in for th input to the the BMS. espoeshialy any safty related data. 

**Outcome (2026-08-01, refined 2026-08-03): FIXED.** The watchdog now tracks freshness of every
registered input signal (all 96 cells, all 16 temp probes, every scalar and keep-alive counter),
not just the original 5-signal subset - docs/13 item 13.1a, `docs/06` section 3. Soft-then-hard
(60s/+5s) escalation confirmed already correct; the soft stage was later extended (item 13.1) to
also force an explicit charge-stop (`full_charge_flag`/`charge_limit_kw`/`charger_limit_kw`), not
just `capacity_empty` - directly matching "cut off soft, then hard, AND stop charging" above.

### 1.2 — The "sanity cross-check" between per-cell data and pack summary is documented but not built
- [x] Reviewed
docs/02:34 and docs/04:77 both describe `0x020`'s `cell_min`/`cell_max` as a "sanity cross-check"
against the 96 individual cell messages. In code (`management_engine.py:172-175`), they're used
**only** as a fallback when the per-cell list is totally empty — no comparison logic exists for the
normal case where both have data.
**Your notes:**
fallback is OK, but we should do the live cross check and if thing start to get outside a delta that is safe we need to 
use trigger a fail safe just like the watch dog. of corse we need to add those reasions for fail safe to the fualt page.

**Outcome (2026-08-01, tested 2026-08-03): FIXED.** New `cell_data_cross_check` feature -
continuously compares the per-cell array against the `0x020` pack summary, same soft->hard
escalation structure as the staleness watchdog (60s/+5s, independently tunable), with two dedicated
fault_log entries (`cell_data_mismatch`/`cell_data_mismatch_hard`, see checklist items 15.16/15.17)
so the reason is visible on the Fault History page exactly as asked. Had zero test coverage until a
later sweep found the gap - now covered by `test_cell_data_cross_check_soft_and_hard_escalation`.

### 1.3 — Worst-cell computation has no visibility into partial cell coverage
- [x] Reviewed
`cells = [rz_state.get_input(k) for k in cell_voltage_keys()]` filters out `None`s
(`management_engine.py:172-173`) — `worst_low`/`worst_high` are computed from whatever subset has
ever reported a value, with no minimum-coverage requirement and no status-text indication of "only
N/96 cells have live data right now." The 96 cells arrive over 24 separate mux'd CAN messages; if a
subset never arrives, the missing cells' stale/`None` values are used indefinitely with no signal
that coverage is incomplete. Confirmed untested: every existing test populates all 96 cells before
asserting anything (`tests/test_management_engine.py`'s `set_all_cells()`/`base_inputs()`).
**Your notes:**
indeed we must check that any muxed or and data is VALIDATED for every incoming can message. 
we should most likely do some kind of CRC or cross check that the data is in a valid range,
amyting outside the valid data should be rejected. and checked on the next input of that data. 
if after 60 seconds the data is still invalid. we should trigger the watchdog. as thats what it is for.  

**Outcome (2026-08-01/03): FIXED - resolved as "the other mechanisms already cover this."** User
decision, 2026-08-03: "let's mark as the staleness watchdog covers this as you show." The broader
ask (validate every incoming message, reject anything outside a plausible range, let sustained
invalid data escalate through the watchdog) is fully done: `rz450e_signals.PLAUSIBLE_RANGES`/
`validate_inputs()` (per-signal plausibility check, a rejected value is never written and ages
under its last-good value until the now-comprehensive watchdog, item 1.1, catches it after 60s)
plus Toyota checksum validation on the 5 confirmed checksum-bearing IDs (item 13.5) - both
toggleable, checklist items 15.14/15.15. A cell that's never arrived at all also now ages from this
engine's first `apply()` call (item 13.1a) rather than being invisible forever. Between these three
mechanisms, a cell missing from the array for any reason (never arrived, went stale, or was
rejected as implausible) is always caught within the watchdog's normal 60s/+5s schedule - **no
separate "N/96 coverage" indicator was built, and per this decision, none is needed**: it would be
reporting the same underlying condition these three mechanisms already surface, just phrased
differently.

---

## Part 2 — Silent-failure / observability findings

### 2.1 — A CAN transmit failure to the Leaf bus is completely silent
- [x] Reviewed
`CanWorker.send()` (`bridge/can_backend.py:92-102`) pushes an `('err', ...)` tuple on TX failure but
never sets `self.connected = False`. Both ingest loops (`realtime_engine.py:337, 373`) discard
`'err'` tuples outright. `ConnectionsPanel._refresh()` (`gui/panels.py:339-349`) only shows
`bus.error` while `connected` is `False` — which TX failures never trigger. Net effect: the panel
can show **"Connected" in green** while every Leaf-bound frame silently fails to send.
**Your notes:**
yeah, add a decacated can moniter lights next to the conection section. so we know the real state. 
including bus heavy. and any resets, add a counter so we can see how many resets we have had.

**Outcome (2026-08-01): FIXED.** New connection-health lights on both `ConnectionsPanel`s (RZ450e
and Leaf) - a TX-OK light (green/red) tracked separately from RX/`connected`, plus a
`reconnects: N | TX errors: N` counter line. `bridge/can_backend.py`'s `BusConnection` gained
`tx_ok`/`reconnect_count`/`send_errors` to back this. "Bus heavy" specifically (a bus-off/error-
frame-rate condition) isn't separately surfaced - `tx_ok` covers "the most recent send failed,"
which is the practical symptom, but there's no dedicated bus-load/error-rate metric if that's still
wanted as its own thing.

### 2.2 — A dead TX-loop thread produces no warning either
- [x] Reviewed
`bridge_status_lbl` (`gui/app.py:272-278`) reflects `engine.sequencer.phase`, only ever advanced
from inside `_tx_loop` itself. If that thread dies from an uncaught exception, `phase` freezes at
its last value — nothing checks `thread.is_alive()` anywhere. Independently reconfirmed by reading
`gui/dashboard.py:349-355` directly: `_tick()` calls `r['out_bar'].set(out_v, fresh=True)`
unconditionally — the output bars are hardcoded "fresh" regardless of actual age, unlike the input
bars just below them, which do check `age_of()` (`gui/dashboard.py:358-362`).
**Your notes:**
same as 2.1, add those things in the app so we can see whats going on. if the age reaches the triggerd 60 sec.
the watch dog takes over. 

**Outcome (2026-08-01): FIXED.** New `RealtimeEngine.last_tick_monotonic` heartbeat, checked by
`gui/app.py`'s bridge-status label every 400ms - shows "Bridge: NOT RESPONDING (TX thread
stalled)" in red if the TX loop hasn't ticked in over `HEARTBEAT_STALE_S` (2.0s), instead of the
phase label silently freezing forever. `gui/dashboard.py`'s output bars also now use this same
heartbeat for their `fresh` flag instead of the old hardcoded `fresh=True` - confirmed directly,
this was the second half of the original finding. The watchdog's own 60s/+5s escalation (item 1.1)
is a separate, already-correct mechanism for RZ450e *input* staleness - this fix is specifically
about the bridge's own TX thread dying, a different failure mode.

### 2.3 — DID poll cadence is slower than its own naming/docs imply **(NEW)**
- [x] Reviewed
`_did_poll_loop` (`realtime_engine.py:377-392`) sleeps the full `DID_POLL_INTERVAL_S` (5.0s) after
**every single** DID request, cycling round-robin through 3 DIDs (SoC, capacity, primary V/I) —
so any one specific DID (e.g. SoC) is actually re-polled only about once every **15s**, not "roughly
every 5s each" as the constant's own comment suggests. Lower severity since SoC is only ever a
backup check that can't fire a cutoff alone — but it further widens the real-world lag on the one
cross-check signal beyond what docs/02 describes (~4-9s), and is a plain doc/code mismatch worth a
quick decision either way (spread the sleep so each DID really is ~5s apart, or fix the comment).
**Your notes:**
can we not just confirm the responce was recived then go to the next one? we can wait up to 5 seconds. 
but if nothing for 5 seconds, go to the next and try that one again when it comes back around round robbon.
of corse, confirm that some did's dont need more than 5 seconds. but, once the aproperate full responce is recived, 
then yes, move on to the next one. i did nt want to flood the can network with did data. thats why i have the wait. 
so we still may need some extra wait time to keep the network from overloading.

**Outcome (2026-08-01): FIXED, exactly as described.** `_did_poll_loop` reworked: waits up to
`DID_RESPONSE_TIMEOUT_S` (5.0s) for each DID's response, then moves to the next one immediately
once it actually arrives - only a small `DID_INTER_REQUEST_GAP_S` (0.3s) pacing delay between
requests, so a fast response doesn't cost a needless extra wait, but the bus still isn't hit with
back-to-back requests. Each of the 3 DIDs is now really re-polled roughly every 5s (plus whatever
the other two took), not ~15s as before.

---

## Part 3 — Connection lifecycle & concurrency findings **(NEW section)**

### 3.1 — Rapid disconnect→connect can silently kill auto-reconnect for the rest of the session **(NEW)**
- [x] Reviewed
Traced directly and independently confirmed by a dedicated concurrency pass. In
`bridge/can_backend.py`: `disconnect()` (`:170-176`) sets `_stop_monitor.set()` but never touches
`_monitor_thread`; `_auto_reconnect_loop()` (`:193-200`) only checks that flag at the *top* of its
loop, after an already-in-progress `time.sleep(RECONNECT_INTERVAL_S)` (3.0s); `connect()`
(`:158-168`) only spawns a new monitor thread (and only there calls `_stop_monitor.clear()`) if the
existing one isn't `is_alive()`. **Repro**: disconnect while the monitor thread is mid-sleep, then
reconnect within that ~3s window — the old thread still reads as alive, so `connect()` skips
spawning a replacement and skips clearing the stop flag; when the old thread wakes, it sees the
(never-cleared) stop flag and exits. Result: a live, apparently-normal connection with **zero**
auto-reconnect monitoring for the rest of the session — no log, no error, nothing in the GUI
indicates it happened. Directly undercuts the "auto-reconnect every few seconds" behavior described
to the user in `RZ450E_CONN_HELP`/`LEAF_CONN_HELP` (`gui/panels.py:19-41`).
**Your notes:**
yeah, can we fix this? if so lets do.

**Outcome (2026-08-01): FIXED, tested 2026-08-03.** `BusConnection` gained a real lock protecting
`_worker`/`_want_connected`/`_monitor_thread` across `connect()`/`disconnect()`/
`_auto_reconnect_loop()`, and the monitor's flat `time.sleep(RECONNECT_INTERVAL_S)` was replaced
with an interruptible `Event.wait()` so `disconnect()` takes effect immediately instead of up to
3s late - closing the specific repro in this item (disconnect mid-sleep, reconnect within the
window). Verified with a new `tests/test_can_backend.py` (first coverage for this module):
`test_disconnect_interrupts_the_monitor_promptly_not_after_the_full_interval` (monitor exits within
~1s, not the full 3.0s) and `test_rapid_disconnect_reconnect_cycling_never_leaves_the_connection_
without_a_monitor` (10x stress cycle, always ends up connected with exactly one live monitor - no
leaked-away monitor). Stable across 5 repeated runs.

### 3.2 — A concurrent auto-reconnect can silently undo an explicit Disconnect click **(NEW)**
- [x] Reviewed
Same root cause as 3.1 (no lock protects `_worker`/`_want_connected` across `connect()`/
`disconnect()`/`_auto_reconnect_loop()`), different manifestation: if the monitor thread wakes and
is mid-way through evaluating "should I reconnect" (`self._worker and not connected and error`) at
the exact moment the user clicks Disconnect, it can still call `_start_worker()` immediately after
`disconnect()` set `_worker = None` — silently reconnecting the bus moments after the user
explicitly disconnected it, with no indication anything overrode their action. The window is narrow,
but the trigger condition (`_worker.error` set) is exactly the state a user is likely reacting to
when they click Disconnect in the first place, so it's not purely theoretical.
**Your notes:**
so we need to fix this? 

**Outcome (2026-08-01): PARTIALLY FIXED, same lock as 3.1.** The same lock+interruptible-wait fix
narrowed this race's window too - from up to ~3s down to microseconds (`connect()`'s check-and-
mutate and `disconnect()`'s own mutation now both happen under the lock). **Not perfectly closed**:
a vanishingly narrow window can still technically exist between `_worker.is_alive()`-style checks
and the lock acquisition around them (see item 12.4's own follow-up note on this same gap - a
cleaner close would use a monotonic "generation counter" on the monitor thread instead of relying
on liveness checks, not done). Given how much smaller the window now is, this is very unlikely to
matter in practice, but it isn't a mathematically complete fix - see `tests/test_can_backend.py`'s
own docstring for why the *exact* race isn't practically unit-testable without adding test-only
synchronization hooks to `can_backend.py` itself.

### 3.3 — App close doesn't cleanly shut down the CAN adapters **(NEW)**
- [x] Reviewed
`App._on_close()` (`gui/app.py:305-310`) calls `engine.stop()` (just flips `_running = False`, no
thread join) then `self.destroy()`; it never calls `rz_bus.disconnect()` / `leaf_bus.disconnect()`.
The `CanWorker` RX threads are daemon threads, so process exit kills them directly rather than
letting them exit their loop and run `self._bus.shutdown()` (`can_backend.py:82-86`) — the PCAN
adapter's own clean-shutdown path is skipped every time the app is closed. Unlikely to corrupt any
in-app data, but can plausibly leave the driver handle in a busy/locked state until replug or a
delay before the adapter is usable again on next launch.
**Your notes:**
most effently needs to cleenly dissconnect if app is closed. 

**Outcome (2026-08-01): FIXED.** `App._on_close()` now calls `rz_bus.disconnect()`/
`leaf_bus.disconnect()` before `self.destroy()`, so both adapters run their real PCAN-shutdown path
(`CanWorker.run()`'s `self._bus.shutdown()`) instead of just being killed by daemon-thread process
exit.

### 3.4 — Confirmed clean on this pass (for context, not action items)
- [x] Reviewed
- `RealtimeEngine.start()` cannot be double-invoked in the current code (single call site in
  `App.__init__`, and `start()` itself is idempotent via an `if self._running: return` guard).
- `DashboardWindow`/`FaultHistoryWindow`'s periodic `.after()` callbacks correctly self-terminate
  (`if not self.winfo_exists(): return`) rather than risk touching destroyed widgets or pyramiding
  across repeated open/close cycles.
- `ManagementEngine.from_dict()`/`MappingEngine.from_list()` handle a profile saved by an older
  code revision safely — a feature added after the profile was saved just keeps its fresh, safe
  default rather than erroring or silently staying blank.
**Your notes:**

**Outcome:** No action needed - confirmed clean on this pass, not a defect. Still true as of
2026-08-03 (no regressions found in the later full code sweep, item Part 14).

---

## Part 4 — Configuration & input-safety findings

### 4.1 — No sanity bounds or feedback on manually-typed safety thresholds
- [x] Reviewed
`ManagementPanel`'s threshold fields (`gui/panels.py:641-644, 652-657`) write straight into the
live config on every keystroke with no min/max clamp and no cross-field validation (nothing stops
`emergency_low_v` being typed higher than `min_cell_v`, inverting the two tiers). A bad parse is
silently swallowed with no visual indication the edit was rejected. Contrast `ChargeEmulationPanel`,
which does clamp (`gui/panels.py:738-749`).
**Concrete consequences of this** (traced directly, illustrating why the gap matters in practice,
not just in the abstract): a `recovery_ramp_s` of 0 or negative collapses to a `1e-6` floor
(`management_engine.py:253`), making the discharge taper's "slow release" hysteresis effectively
instantaneous — silently defeating the anti-power-hunting protection it exists for. A negative
`soft_cut_persistence_s` makes the persistence guard's `held >= f['soft_cut_persistence_s']` check
(`management_engine.py:211`) true on the very first tick (`held` starts at `0.0`), effectively
disabling the transient-sag guard entirely — both from a single mistyped character, live, with no
confirmation step.
**Your notes:**
yeah we need clamps on all data thats input by the user. and we need to clamp the can output data
( I think the can output clamp is inplace? but we need to validata the data to be with in range though. 
if its wrongly computed we should set an error state? ) on the same note. this gose along with 1.3 for ths input
can data.

**Outcome (2026-08-01, extended 2026-08-03): FIXED.** Every `ManagementPanel` threshold field now
clamps to a registered `(lo, hi)` bound on every keystroke via `FEATURE_FIELD_BOUNDS`, with a visual
"invalid"/"clamped" flag next to the field instead of silently swallowing a bad edit - exactly the
"set an error state" ask. Extended 2026-08-03 (items 13.3/13.9) to the profile-*loading* path too
(`ManagementEngine.from_dict()`, `config_profile.py`'s charge_emulation loader) using the identical
bounds tables, so a hand-edited/corrupted `profile.json` can't set a threshold the GUI itself would
never allow. **Output clamping** (the other half of this note) was already in place before this
item was written (`leaf_signals.clamp_state()`, docs/06 section 4) - confirmed still correct, not a
gap.

### 4.2 — Every protection feature is a single checkbox away from being fully off, with no backstop
- [x] Reviewed
Each of the 7 curated features has one `enabled` checkbox (`gui/panels.py:628-630`); unchecking it
skips that entire block in `apply()` with nothing else standing behind it. Deliberate design choice
per docs/05's philosophy, not a bug — flagging because it's in tension with "redundant,
double-checked," worth an explicit decision either way.
**Your notes:**
it would be good to add this to the log, jsut so its notated. but yes. currently by desighn. 
on a side note: this remineds me the charge option should be set to on as defualt. 
second side note: this also makes me think. we have regen power AND charge power. we should seprate those. 
move the AC charge liments in to the charge tab? anything charger related should be there? 
i know that there shared values. but they get used in diffrent ways depending on if th car is plgged in. 

**Outcome (2026-08-01): FIXED, all three side-notes included.** (1) Every feature checkbox toggle
(Battery Management AND Charge Emulation) now logs an ENABLED/DISABLED line. (2) `charge_emulate`
default flipped 0->1. (3) The big one: `charge_target_taper` split into regen-only (drives
`charge_limit_kw`, stays on the Battery Management tab) and a new `ac_charge_taper` (drives
`charger_limit_kw`, moved to the Charge Emulation tab alongside the rest of the charger-specific
controls) - see `05-battery-management-safety.md`'s CC->CV section for the full design. The
original finding itself (single checkbox = fully off, no backstop) remains true by design, unchanged
- confirmed still the intended behavior, not something to add a backstop to.

### 4.3 — A mapping tie can silently go dead after a future field rename, with a misleading GUI state **(NEW)**
- [x] Reviewed
`MappingPanel` builds its output dropdown strictly from the current `leaf_signals.OUTPUT_SIGNALS`
registry, so it can't produce an invalid key today. But `MappingTie.output` is just a stored string
— if a saved `profile.json` (hand-edited, or from a future code revision that renamed/removed a
field) has a tie targeting a key no longer in the registry, `output_key_to_display.get(tie.output,
self.BLANK)` (`gui/panels.py:529, 554`) falls back to showing **"(unused)"** in the OUT dropdown —
even though `tie.output` internally still holds the old string, and `MappingEngine.apply()` still
evaluates the tie every tick and writes into that now-meaningless key. There's no way to
distinguish, from the GUI, "this tie is intentionally unused" from "this tie used to matter and
silently stopped working after an update." Low probability today (no such rename has happened yet),
but worth a decision on whether a mismatch like this should surface a warning instead of quietly
looking identical to "unused."
**Your notes:**
yeah we need to fix this and make sure that any data is set and saved corectly. and desplayed corectlyon the dashboard page. 

**Outcome (2026-08-03): FIXED.** User decision: "i want to do it all if it was supose to be in
place and it was missed." `MappingPanel` (`gui/panels.py`) now distinguishes a genuinely-blank
slot from an orphaned/renamed key on BOTH input and output dropdowns: `_display_for_input`/
`_display_for_output` check the current registry first, and for a non-empty key that's no longer
found there, build a `(!) UNKNOWN KEY: <raw key>` display string instead of silently falling back
to `(unused)`. That combo is also styled with a new dedicated `Warn.TCombobox` ttk style
(`gui/theme.py`, red field/foreground) so it's visually distinct at a glance, not just
text-distinguishable. The orphan display string is registered back into
`input_display_to_key`/`output_display_to_key` so `_update_tie()` round-trips it to the exact
original key on every edit instead of resolving an unrecognized display string to `''` and
silently deleting the stale reference the next time any other field on that row changes (this
would have been a real data-loss bug if shipped as originally sketched) - verified with a smoke
test asserting `tie.inputs`/`tie.output` survive `_update_tie()` unchanged while orphaned. Once
the user picks a real replacement from the dropdown, `_update_tie()` re-evaluates and clears the
warning style immediately, no row rebuild needed. Dashboard's own `_tie_for_output()` only matches
ties against currently-valid keys, so an orphaned tie naturally shows as unmapped there already -
no separate dashboard-side fix was needed for that part of the original note.

---

## Part 5 — Behavioral / decision items

### 5.1 — Hard cuts (and soft cuts) don't latch — they self-clear the instant the reading recovers
- [x] Reviewed
Already-documented open item (`docs/12` finding **F8**) — flagged here because it's directly
relevant to "how do we handle a safe power-down" and deserves an explicit decision. Every cut in
`apply()` is recomputed fresh every tick with no memory: a cell that spikes to 4.35V for one tick and
immediately drops back asserts the hard cut and clears it again on the very next tick, silently.
Standard BMS practice (researched in docs/12 §8) is that emergency-tier faults **latch** and require
a deliberate human clear, since the underlying hazard doesn't un-happen just because the reading
came back down. `FaultLog` records that it happened but doesn't change the pack's electrical state.
**Your notes:**
when we implmented this is stated that it should only reset AFTER the car has been powered down 
and back on OR if the charger is unplugged and repluged for instance. 
not sure why it did not getr implmented that way. it should be.

**Outcome (2026-08-01, refined 2026-08-03): FIXED, exactly as specified.** New
`ManagementEngine._hard_latched`: any hard-tier condition latches `relay_cut_request`/`interlock`
on every subsequent tick regardless of the reading recovering, cleared ONLY by
`notify_session_start()` (a real bus wake, i.e. the car was actually powered down and back on - not
a bare Stop/Start Bridge toggle, a bug an independent review pass caught and fixed the same day) or
`notify_charge_replug()` (the charger genuinely unplugged and replugged - refined 2026-08-03, item
13.4, after a second review found the first version could be fooled by a brief dropout that wasn't
a real replug). Soft cuts keep auto-clearing, unchanged, matching docs/12 §8's own researched
soft/hard distinction.

### 5.2 — The two "charge status" displays (Configurator tab vs. Dashboard) can disagree during ordinary operation **(NEW)**
- [x] Reviewed
docs/08 (lines 334-349) states both windows "tell a consistent story" about charge-ramp state. In
code, the Dashboard correctly derives active/idle from the real trigger signals (`leaf_wants =
sequencer.charge_active(...)`, `rz_auth = charge_permission_input`, `gui/dashboard.py:395-401`).
`ChargeEmulationPanel._schedule_status_refresh()` instead infers idle purely by thresholding the
transmitted number itself (`charger_kw >= 92.2`, `gui/panels.py:762-771`) — never reading either
trigger signal. Since `charge_target_taper`'s per-cell overvoltage factor unconditionally scales
`charger_limit_kw` every tick regardless of whether a charge request is even active
(`management_engine.py:321`), a resting pack sitting in the 3.9-4.1V taper window can produce a
`charger_limit_kw` below 92.2kW with **no** active charge request at all — at which point the
Configurator tab would show "(ramping/active)" while the Dashboard correctly shows "idle." This is a
plausible normal-operation scenario, not a contrived edge case.
**Your notes:**
yeah if the charge is not active. we should not asume anything. those need otbe isolated behavor. 
already notated this in 4.2

**Outcome (2026-08-03): FIXED**, via item 14.4, well after the regen/AC split (4.2) this note
points back to. New `RealtimeEngine.charge_status_summary()` is the single source both the
Configurator tab and the Dashboard now read, resolved from the same live trigger signals
(`sequencer.charge_active()`, `charge_permission_input`, the actual gate/staleness/target-reached
status) instead of the old guess-from-the-transmitted-number approach that could disagree with the
Dashboard during ordinary resting-voltage-taper operation.

### 5.3 — `docs/09`'s STM32 export example is stale relative to the actual config schema
- [x] Reviewed
The illustrative JSON in `docs/09-stm32-export-format.md` omits `emergency_temp_f`,
`cell_imbalance_monitor`, `overcurrent_monitor`, and `soft_cut_persistence_s` — all real fields in
`default_config()`. Not a functional bug (the real export via `to_dict()` includes everything) —
just a stale doc example that could mislead a future firmware-porter.
**Your notes:**
yeah fix this so we dont miss it. 

**Outcome (2026-08-01): FIXED, and kept current throughout this session.** `docs/09`'s example JSON
is now regenerated directly from `default_config()`/`charge_emulation` rather than hand-transcribed,
and has been re-verified/updated every time a field was added or a default changed since (most
recently the 4.20V threshold change and the `input_validation`/`checksum_validation` toggles,
2026-08-03).

### 5.4 — Shared state is mutated/read without its own lock in several places
- [x] Reviewed
`bridge/state.py`'s own header states the design intent: *"everything is behind one lock."* In
practice, `management_status`, `vehicle`, `generated_enabled`, `charge_emulation`,
`ManagementEngine.config`/`.status`, and `MappingEngine` ties are all mutated/read without it, from
the GUI thread and the TX thread concurrently (`realtime_engine.py:597, 442-450, 520-569, 619`;
`gui/panels.py:435-443, 644, 685, 714-732, 541-563`). Under CPython's GIL this is unlikely to
crash outright, but it's inconsistent with the module's stated design, and worth knowing about
given the explicit plan to port this logic to STM32 firmware, where there's no GIL to paper over it.
**Your notes:**
yeah, we need to understand how the STM32 version will work. currentkly a snapshot of our settings will be used. 
but this will work as a standalone system. this will not have some items included. we havent goten to that yet. 

**Outcome (2026-08-01): PARTIALLY ADDRESSED, deliberately scoped.** New locked accessors added for
`management_status`/`vehicle` (`SharedState.snapshot_management_status()`/`set_management_status()`/
`snapshot_vehicle()`/`set_vehicle_item()`), applied at every touch point. `generated_enabled`/
`charge_emulation`/`ManagementEngine.config`/`.status` were deliberately NOT retrofitted this pass -
per this note's own point, how shared state should work on the future standalone STM32 port (not a
live Python object graph) is still an open architecture question, so locking the Python-side access
pattern further didn't seem worth doing until that's decided. Documented as a scope decision, not
silently dropped - individual dict item reads/writes remain safe under CPython's GIL either way.

---

## Part 6 — Test-coverage quality findings **(NEW section)**

A full read of every assertion in all 6 test files (not just which functions each touches), plus a
cross-check against `docs/08`/`docs/09`'s specific behavioral claims.

### 6.1 — The AC "charge target reached" contactor-drop path has zero test coverage **(NEW)**
- [x] Reviewed
When `charging_active` and `soc >= target`, `charge_target_taper` force-sets `full_charge_flag = 1`
(instant contactor drop per docs/03), zeroes `charge_limit_kw`, and sets `charger_limit_kw = -10.0`
(`management_engine.py:332-337`) — a genuine hard safety action. No test in any of the 6 files ever
sets `soc_pct` above the daily/extended target while `charge_permission_input` is active. This is
distinct from the dual-trigger mismatch path `test_charge_ramp.py` does cover.
**Your notes:**
indeed i do need to test this. add it to a test plan doc. we havent made on yet but there are
 other notes else where about tests that need to be done. gather those up and make a doc / checklist about it. 
 
**Outcome (2026-08-01): FIXED, both parts.** New
`tests/test_management_engine.py::test_ac_charge_target_reached_sets_full_charge_flag` covers the
contactor-drop path directly. Separately, new `docs/14-validation-test-plan.md` gathers every
scattered "needs a test"/"needs real hardware" note from this checklist (and this session's
implementation pass) into one working document, exactly as asked.

### 6.2 — Overvoltage emergency hard-cut has thin, indirect test coverage **(NEW)**
- [x] Reviewed
Unlike low-voltage emergency and over-temp emergency (each has a dedicated test checking both the
cut and its `fault_log` entry), the mirror-image overvoltage tier (`management_engine.py:289-304`,
fault key `overvoltage_emergency`) is only incidentally exercised inside
`test_charge_ramp.py:201-217`, which checks `relay_cut_request == 3` but never asserts the
`fault_log` entry at all — a coverage gap on one of only 3 hard-cut fault types.
**Your notes:**
add it.

**Outcome (2026-08-01): FIXED.** New
`tests/test_management_engine.py::test_overvoltage_emergency_fault_log_entries` directly asserts
both `overvoltage_emergency` (regen) and `ac_overvoltage_emergency` (AC charger) fault_log entries,
not just `relay_cut_request`.

### 6.3 — Several tests skip the exact boundary they're built around **(NEW)**
- [x] Reviewed
A recurring pattern, distinct from the staleness/cross-check gaps above: `test_f5_soft_cut_
persistence` jumps straight from tick-0 to 2.1s without checking just-before the 2.0s window (e.g.
~1.9s) to confirm it genuinely hasn't latched yet; `test_f2_overcurrent_monitor` has the same gap
for its 5.0s persistence window; `test_f6_emergency_temp_tier` tests 140°F and 150°F but never near
the actual 149°F threshold; `test_f4_cell_imbalance_monitor` tests 0mV and 100mV but never near the
50mV warn threshold. Worth deciding whether these are worth tightening before relying on the
persistence/threshold logic as-is.
**Your notes:**
add to look over every min max and all and any pramiters one at a time to the test plan as a check prams section. 

**Outcome (2026-08-03): FIXED.** User decision: "i want to do it all if it was supose to be in
place and it was missed." All 6 items from `docs/14-validation-test-plan.md`'s "Boundary-value
sweeps" section are now real tests in `tests/test_management_engine.py`, each checking just-before
AND just-after the exact configured value (not just "clearly inside/outside"):
`test_boundary_low_voltage_soft_cut_persistence` (1.9s / 2.1s around the 2.0s window),
`test_boundary_overcurrent_persistence` (4.9s / 5.1s around 5.0s),
`test_boundary_emergency_temp` (141.7°F / exactly 141.8°F / 141.9°F, also confirming the `>=`
comparison fires right at the boundary), `test_boundary_cell_imbalance_warn_delta` (99mV / 101mV
around the 100mV threshold), `test_boundary_cell_data_cross_check_delta_and_escalation_timing`
(149mV/151mV around the 150mV `max_delta_v` threshold, PLUS its own soft/hard escalation timing
boundary with the persistence windows shrunk to 0.15s each so the test doesn't need to wait the
real 60s/+5s), and `test_boundary_staleness_watchdog_soft_and_hard_escalation` (same shrunk-window
pattern for the watchdog's own 60s soft / +5s hard escalation). All 22 new checks pass. `docs/14`'s
checklist items are checked off to match.

### 6.4 — A couple of assertions are looser than the underlying math requires **(NEW)**
- [x] Reviewed
`test_f1_cold_block_uses_coldest_probe`'s second check only asserts `charge_limit_kw > 0.0` where an
exact value was computable and available. `test_f3_cold_derate_ramp`'s midpoint check accepts a
`0.35 < factor < 0.65` range for a case where the linear ramp formula gives an exact expected factor
of 0.5 — loose enough that it would still pass if the ramp curve were subtly non-linear or otherwise
wrong. Not urgent, but worth tightening opportunistically.
**Your notes:**
yeah check coldest makes sence if i understand this comment corectly. do we need to corect somthing here? 

**Outcome (2026-08-03): FIXED.** User decision: "i want to do it all if it was supose to be in
place and it was missed." `test_f1_cold_block_uses_coldest_probe`'s second check now asserts the
exact expected `charge_limit_kw` (the unclamped `DEFAULTS` value - hand-traced: cold_factor=1.0
since the 70°F coldest probe is above `charge_derate_low_start_f`, hot_factor=1.0 since the 20°F
hottest probe is nowhere near `charge_derate_start_f`, and the default 3.70V cell voltage doesn't
trigger the regen taper either) instead of a bare `> 0.0`. `test_f3_cold_derate_ramp`'s midpoint
check now asserts `abs(factor - 0.5) < 1e-9` (the linear ramp formula gives an exact 0.5 at
`(41-32)/(50-32)`) instead of the loose `0.35 < factor < 0.65` range. Both pass. To answer the
direct question that was asked: no correction was needed to the *feature logic* itself (the
coldest-probe behavior was already correct, per item F1's own bug-fix confirmation) - this was
purely about the *test assertions* being looser than the math required.

### 6.5 — `manual_reset` is only tested against an instantaneous emergency condition
- [x] Reviewed
`test_fault_log_manual_reset_does_not_change_live_cut_decision` only exercises reset against an
always-true emergency condition. It never tests the more realistic, common case `fault_log.py`'s own
docstring emphasizes as its primary motivation — resetting a **soft** or **warn**-tier entry whose
condition has since auto-cleared.
**Your notes:**
ok, so we need to fix this? 

**Outcome (2026-08-01): FIXED.** New
`tests/test_fault_log.py::test_manual_reset_on_already_auto_cleared_soft_entry` covers exactly the
scenario this item's own text calls out as the realistic, common case.

### 6.6 — Confirmed clean on this pass (for context, not action items)
- [x] Reviewed
`FaultLog`'s own unit tests (`test_fault_log.py`) are tight and non-tautological — rising-edge
counting, persistence round-trip of `active`, and re-trigger-after-reset are all checked against
exact values. `test_output_clamping.py` and `test_mapping_engine.py` use exact-value assertions
throughout. `FAULT_DEFINITIONS`'s count matches docs/08's "12 total" claim exactly. Every window
geometry/sizing claim checked in docs/08 (1430×835 main window, 374/680/374 pane widths, 420×900
Fault History window) matches the code exactly.
**Your notes:**

**Outcome:** No action needed - confirmed clean on this pass. Note: this item's own "`FAULT_
DEFINITIONS`'s count matches docs/08's '12 total' claim" line was accurate on 2026-08-01, but the
catalog has since grown to 19 entries (item 14.1) - that's a later drift, not something wrong with
this confirmation at the time it was made.

---

## Part 7 — Battery management thresholds (every parameter, one at a time)

Basis column cites `docs/05`/`docs/12`; Verified column reflects `docs/11`'s current status
(**Documented** = researched default, not yet checked against this real pack; **Confirmed
(software)** = the *logic* is unit-tested, not the *value*; none of the safety thresholds below are
"Confirmed (real hardware)" yet).

### Low-voltage cutoff
| Field | Default | Verified | Notes |
|---|---|---|---|
| Min cell V (soft → `capacity_empty`) | 3.00 V | Confirmed (software, logic only) | Standard NMC floor, margin above ~2.5V damage line |
| Emergency low V (hard cut) | 2.60 V | Documented | Not yet tested |
| Soft cut persistence | 2.0 s | Confirmed (software) | Guards against sag transient under spike load; see 6.3 re: boundary not directly tested |
| Min SoC % (backup check, never acts alone) | 10.0 % | — | By design cannot trigger a cutoff alone |
- [x] Reviewed — **Your notes:**
i change to 2.6 for hard cutoff. less likely to trigger untill it truly reaches the value. 

**Outcome (2026-08-01): APPLIED.** `emergency_low_v` default is now 2.60V. `min_soc_pct` also
lowered to 8.0% the same session, for consistency alongside this change (see the Discharge power
taper section below, where the arithmetic for that number actually came from).

### Discharge power taper
| Field | Default | Verified | Notes |
|---|---|---|---|
| Taper start V (full power) | 3.0 V | Documented | |
| Taper zero V (zero power) | 2.6 V | Documented | Matches soft-cut floor by design |
| Recovery ramp | 3.0 s | Documented | Fast-attack/slow-release hysteresis; see 4.1 re: unvalidated-value consequence |
- [x] Reviewed — **Your notes:**
changed zero power to 3.0 changed zero power to 2.6 so it matches the cut off.
we should have a | Min SoC % (backup check, never acts alone) | 10.0 % - 2% this way we have redundency. 

**Outcome (2026-08-01): APPLIED, both parts.** `taper_start_v`/`taper_zero_v` are now 3.00V/2.60V.
`min_soc_pct` (`low_voltage_cutoff`, the field this note's "10.0% - 2%" arithmetic refers to) is now
8.0%.

### Charge/regen power limit + AC target
| Field | Default | Verified | Notes |
|---|---|---|---|
| Regen full V (full power at/below) | 4.0 V | Documented | Proactive — VCM is slow to react |
| Regen zero V (zero at/above) | 4.15 V | Documented | Still under 4.20V standard ceiling |
| Emergency high V (hard cut) | 4.3 V | Documented | See 6.2 re: thin test coverage on this tier |
| Daily target % | 80.0 % | — | See 6.1 re: untested contactor-drop path |
| Extended target % | 100.0 % | — | User preference |
- [x] Reviewed — **Your notes:**
change to 4.0 for full regen and changed zero to 4.15.
this now needs to be split in the same way we did charging. regen and AC charging is not the same. 
i can regen WAY more power then i can AC charge. so the pramiters need to be split. and put on the charging tab.

**Outcome (2026-08-01): APPLIED, all three parts.** `regen_full_v`/`regen_zero_v` are now 4.00V/
4.15V. The regen/AC split happened the same session (`charge_target_taper` now regen-only, new
`ac_charge_taper` on the Charge Emulation tab) - see item 4.2's outcome for the full design.
`emergency_high_v` was further tightened 2026-08-03 to 4.20V (item 15.3).

### Over-temperature derate
| Field | Default | Verified | Notes |
|---|---|---|---|
| Charge cold-derate start (coldest probe) | 50°F / 10°C | Confirmed (software, logic only) | |
| Charge low block (coldest probe) | 32°F / 0°C | Confirmed (software, logic only) | Bug-fixed 2026-07-31 (was keyed on hottest probe) |
| Charge derate start (hottest probe) | 90°F / 32°C | Documented | |
| Charge hard stop (hottest probe, soft ramp only) | 113°F / 45°C | Documented | |
| Discharge derate start (hottest probe) | 131°F / 55°C | Documented | |
| Discharge hard stop (hottest probe, soft ramp only) | 140°F / 60°C | Documented | |
| Emergency temp (hard cut, hottest probe) | 149°F / 61°C | Confirmed (software, logic only) | Deliberately thin margin; see 6.3 re: threshold itself not directly tested |
- [x] Reviewed — **Your notes:**
change hard cut to 61 

**Outcome (2026-08-01): APPLIED.** `emergency_temp_f` is now 141.8°F (61°C exactly).

### Cell imbalance monitor (warn only)
| Field | Default | Verified | Notes |
|---|---|---|---|
| Warn spread | 100 mV | Confirmed (software, logic only) | Never cuts/derates; see 6.3 re: threshold itself not directly tested |
- [x] Reviewed — **Your notes:**
changed to 100mv 

**Outcome (2026-08-01): APPLIED.** `warn_delta_v` is now 0.10V (100mV).

### Overcurrent monitor (warn only)
| Field | Default | Verified | Notes |
|---|---|---|---|
| Discharge warn | 150 A | Documented | Sensor saturates at ±204.7A — cannot see the pack's real ~500A/660A range at all |
| Charge/regen warn | 30 A | Documented | Above Leaf AC charger's ~19A max |
| Persistence | 5.0 s | Documented | See 6.3 re: boundary itself not directly tested |
- [x] Reviewed — **Your notes:**

**Outcome:** No changes requested - 150A/30A/5.0s confirmed as-is.

### Staleness watchdog
| Field | Default | Verified | Notes |
|---|---|---|---|
| Soft cut after | 60 s | Documented | See 1.1 — doesn't cover per-cell/temp signals |
| Hard cut escalation | +5 s | Documented | |
- [x] Reviewed — **Your notes:**
now added data validation scheem. needs its own implmentation in to the watch dog. 

**Outcome (2026-08-01/03): FIXED, thresholds unchanged (60s/+5s confirmed as-is).** The data-
validation scheme this note anticipates was built and wired directly into the watchdog's own input
pipeline, not as a separate parallel system: `input_validation`/`checksum_validation` (items
15.14/15.15) reject bad data before it ever reaches `SharedState`, and the watchdog itself (item
1.1) now covers every signal those checks protect, so a signal that stays rejected/stale long enough
is caught by this exact watchdog - the two systems are integrated, not separate implementations.

---

## Part 8 — Signal mapping / conversions (every default tie)

| # | Input(s) → Output | Formula | Status |
|---|---|---|---|
| 1 | `pack_v` → `pack_voltage_v` | linear ×1.0 +0.0 | Documented |
| 2 | `current` → `pack_current_a` | linear **×−1.0** +0.0 (sign inverted) | Documented — `docs/04` flags this exact convention as "previously gotten wrong once already," worth a dedicated bench check |
| 3 | `soc_pct` → `usable_soc` | linear ×1.0 +0.0 | Documented |
| 4 | `soc_pct` → `fine_soc_pct` | linear ×1.0 +0.0 | Documented |
| 5 | `soc_pct` → `soc_correction` (physical dash %) | linear ×2.0 +0.0 | **Confirmed real hardware** (2026-07-31) |
| 6 | `capacity_pack1_ah` → `capacity_bars_raw` | linear ×0.07 +0.0 | **Confirmed real hardware** (2026-07-31) |
| 7 | `temp_max` (°F) → `batt_temp_c` | linear ×5/9 −17.78 | Documented |
| 8 | `capacity_pack1_ah` → `soh_pct` | `ah / 201.00 × 100` | Documented |
| 9 | (derived, not a tie) → `gids`, `qc_full_wh`, `qc_remain_wh` | `soc% × capacity_ah × pack_v` family | Documented — needs a real SoC sweep cross-checked against LeafSpy's GIDS display (docs/10 #3) |
- [x] Reviewed — **Your notes:** (see also 4.3 re: what happens if a tie's output field is ever renamed)
this remineds me, when im scrowling with the wheel. i have acidentlky changed the inputs / outputs. this data should onlky be valid if selected with a mouse pointer and click. 
im thinking there is more than one temp output that needs to be set. the dash segemnt needs sto be added to this and set as a defuaklt maping. 

then confirm we are not missing any other map's or output CAN data, EVERY output can message should be driven by some kind input or active logic?  
if there are some or you think there are more missing, lets add those to the botom of this list and i will go through them one at a time as well. 

**Outcome (2026-08-01): FIXED, all three parts.** (1) Mouse-wheel scrolling no longer silently
changes a readonly Combobox's selection anywhere in the app (new `_no_wheel()` helper) - only an
explicit click can change a mapping/vehicle/channel dropdown now. (2) New provisional default tie
for `temp_segment_pct` (the dash temperature segment) - explicitly marked NOT hardware-confirmed
(unlike `soc_correction`/`capacity_bars_raw`), tracked as `docs/10` open question #13. (3) A full
output-signal coverage audit found and fixed 3 more gaps, appended to the bottom of this checklist
as items 12.5's sub-findings - `voltage_latch` (dead mapping target, removed), `main_relay_on`
(reviewed, decided static-1 is fine - see item 12.5's own notes), and 4 `GENERATED_SIGNALS`
checkboxes that weren't actually gating their frames yet (fixed the same pass).

## Part 9 — Control-behavior review (the way things are controlled, not just the numbers)

- [x] **Soft-cut vs. hard-cut split** — matches intended design, reserved for genuine emergencies +
  staleness escalation. **Your notes:**
  **Outcome:** Confirmed, no changes needed. Hard cuts gained latching since this note (item 5.1) -
  the soft/hard split itself is unchanged.

- [x] **Discharge-taper hysteresis** (fast-attack / slow-release, default 3.0s) — only feature
  carrying state between ticks; confirms the intended anti-hunting behavior when valid input is
  given (see 4.1 for what an invalid input does to it). **Your notes:**
  **Outcome:** Confirmed, no changes needed at the time. No longer the *only* feature carrying
  state between ticks - `charge_target_taper` (regen) gained the same hysteresis pattern the next
  bullet's note requested. Both tapers' hysteresis got direct test coverage 2026-08-03 (previously
  neither did, despite this confirmation - see item 6.4's sibling gap).

- [x] **Charge/regen taper is a pure function of instantaneous voltage** (no hysteresis, unlike
  discharge) — intentional per docs/05. Worth confirming you still want that asymmetry. **Your
  notes:** 
  regen we should add some hysteresis? same as discharge? also split regen from charger as descussed.
  charger dose not have the hysteresis? 

  **Outcome (2026-08-01, decided 2026-08-03): DONE, resolved as a deliberate asymmetry.** Regen
  (`charge_target_taper`) got hysteresis (`_regen_factor_applied`, same fast-attack/slow-release
  pattern as discharge, now with a direct test). **`ac_charge_taper` deliberately does NOT get
  hysteresis** - explicit user decision, 2026-08-03: "let's leave it and mark it as such so it's not
  confusing in the future." `ac_factor` stays a pure function of the current instantaneous voltage,
  computed fresh every tick with no smoothing - unlike its regen sibling. This is now a **documented,
  intentional difference**, not an oversight: if you're reading `ac_charge_taper`'s code later and
  wondering why it lacks the `_regen_factor_applied`-style state the regen taper has, this is why.

- [x] **`full_charge_flag` re-arm has no physical-replug equivalent** (docs/10 #1, still open).
  **Your notes:** humm. thsi was from my memory, an unplug and replug reset. i think i mentioned this already in this doc.
  **Outcome (2026-08-01, refined 2026-08-03): FIXED.** Same mechanism as item 5.1 -
  `notify_charge_replug()` (a genuine `charge_permission_input` absence for `CHG_END_STOP_S`=3.0s,
  then a fresh request) is exactly the "unplug and replug" re-arm this note remembered wanting.
  `docs/10` item #1 itself updated to RESOLVED - see `05-battery-management-safety.md`'s
  "`full_charge_flag` re-arm" section for the full current behavior.

- [x] **Charge-ramp dual-trigger requirement** — mismatch forces an explicit stop rather than
  falling back to a static value; see 5.2 re: the two status displays for this feature disagreeing.
  **Your notes:** yeah see notes on 5.2. i think that covers this one in detail. 
  **Outcome:** The dual-trigger requirement itself was already correct, unchanged. The status-
  display disagreement is fixed - see item 5.2's outcome.

- [x] **4 ported shutdown triggers + 1 bridge-specific staleness trigger** — all five converge
  through one `_should_wind_down()` check each tick. **Your notes:**
  **Outcome (2026-08-03): NARROWED, per a later explicit user directive (item 14.3).** The 5th
  trigger used to fire on ANY hard cut (not just staleness); now it's staleness-only
  (`ManagementEngine.staleness_hard_cut`) - a non-staleness hard cut (voltage/temp/cross-check
  emergency) latches and keeps the bridge running/transmitting instead of winding down. Still all
  converge through one `_should_wind_down()` check, just with a narrower 5th-trigger condition than
  described when this confirmation was originally written.

- [x] **Output clamping** — guarantees nothing out-of-range reaches the CAN bus regardless of what
  upstream logic produces. **Your notes:** yeah and now added user input clamping.
  **Outcome:** Confirmed unchanged/correct. Input-side clamping is item 4.1's outcome.

- [x] **DID/PID polling cadence** — see 2.3; effectively ~15s per specific DID, not ~5s.
  **Your notes:** yeah notated how to change this in 2.3
  **Outcome:** Fixed - see item 2.3's outcome.

- [x] **Auto-reconnect on connection drop** — see 3.1/3.2; can silently stop working, or silently
  override a manual disconnect, under a specific race. **Your notes:** see notes in those 3.1/3.2
  **Outcome:** Fixed (3.1) / narrowed but not perfectly closed (3.2) - see those items' own outcomes,
  now also directly tested (`tests/test_can_backend.py`, 2026-08-03).

- [x] **Fault auto-clear vs. latching** — see 5.1; the single biggest open behavioral decision left
  in the whole management layer. **Your notes:**
yeah we need to fix this, as notated in 5.1. unless im mestaken and the option to clear automaticaly with "power cycle"
  VS manuialy is already in place? let me know. 

**Outcome (2026-08-01): FIXED - direct answer to the question asked.** Yes, exactly that option is
now in place: a hard cut latches and clears ONLY via a genuine power-cycle-equivalent
(`notify_session_start()`, a real bus wake) or a genuine charger replug
(`notify_charge_replug()`) - there is deliberately NO separate manual "unlatch" button in the GUI.
See item 5.1's outcome for the full mechanism.
---

## Part 10 — Safety-relevant open questions already tracked in `docs/10`

- [x] **#2 — exact staleness-watchdog behavior when only some source groups go stale.** Item 1.1
  above is a concrete, worse-than-assumed answer — the doc's own wording assumed "raw-CAN covers
  voltage/current/temp" as one group; in code it doesn't. **Your notes:** yeah we need all can added to watchdog as descussed. VALIDATE data. 
  **Outcome:** Fixed - see item 1.1's outcome (full signal coverage) and item 1.3's outcome (input
  validation).

- [x] **#4 — `charge_permission_input` "no interlock present" default.** Currently fails safe only
  as a side effect of `get_input()` returning `None`, not a written, deliberate policy. **Your
  notes:**
umm explin this more? if i understand corectly. we need both interlock's? thought we changed that
 yesterday as it was implmented incorectly. and the doc's should have been updated? 

**Outcome (2026-08-01): FIXED, the policy part.** `charge_permission_input` (`0x358`) missing/unwired
now fails safe to "not permitted" as an explicit, deliberate, documented policy (`05-battery-
management-safety.md`'s Design philosophy section, `10-open-questions.md` #4 marked RESOLVED) -
previously true in code but only as an emergent side effect of `get_input()` returning `None`. **On
the direct question**: this project has only the one interlock signal, `charge_permission_input` -
there isn't a second one in this codebase to reconcile against. If you're thinking of a different
signal from a past session, flag it and I'll trace it specifically; nothing in the current code
suggests a second interlock was ever implemented or removed.

- [x] **#7 — does the RZ450e pack's own internal cell-balancing hardware still run** in this
  configuration? Directly affects how much weight to put on the cell-imbalance monitor over time.
  **Your notes:**
yeah need to add to the test doc jsut so it dose not get forgotten. 

**Outcome (2026-08-01): TRACKED, not fixable in software.** Added to `docs/14-validation-test-plan.md`
Part 2 (real-hardware-only) as its own line item - genuinely can't be answered without extended
real-hardware observation (cell spread over multiple sessions), so this stays open until that
testing happens.

- [x] **#8/#9 — overcurrent monitor and DC fast-charging are both outside what the current sensor
  can see** (±204.7A signal ceiling vs. a 500A fuse / ~660A peak / ~430A DC-fast-charge pack).
  **Your notes:**
yeah add to test doc, as we cant validate this as of yet. 

**Outcome (2026-08-01): TRACKED, not fixable in software.** Both added to `docs/14-validation-test-
plan.md` Part 2 as their own line items - the `0x023` sensor is structurally unable to see the
pack's real ~500A/660A range at all (a hardware ceiling, not a software gap), and DC fast-charging
is entirely outside this project's current scope. Both stay open pending a future wider-range
current sensor / explicit scope decision, not something more code can close.
---

## Part 11 — Overall verification-status rollup

Per `docs/11`, of the 12 tracked battery-management features, **3 have a bug-fix reproduced and
confirmed in software** and a few more have their *logic* unit-tested — but **zero are confirmed
against real hardware on the actual safety envelope** (voltage/temp/current threshold *values*).
The only two fully real-hardware-confirmed items in the whole project are mapping formulas (dash
SOC%, capacity bars), not protection thresholds. This pass's test-coverage-quality findings (Part 6)
add a further nuance: even the "software-confirmed" logic has a few untested paths (6.1, 6.2) and a
pattern of not testing right at the actual threshold boundary (6.3) — worth keeping in mind when
weighing how much confidence "unit-tested" should actually buy for a given feature. Practically:
treat every number in Part 7 as a reasonable, researched starting point, not yet a validated one.
- [x] **Your notes:**
yeah any thing like this needs to be added to the test doc. realy that should be called a validation doc... 
however that will need to be confirmed in the final hardwere as well.

new: 
can we add a data logger. must keep the .trc format. i want to log so that we can check and confirm things as we test. 

**Outcome (2026-08-01): FIXED, both parts.** "Validation doc" -> `docs/14-validation-test-plan.md`
(created this session, name changed as requested). Data logger -> new `bridge/trc_log.py`, ported
byte-for-byte from the RZ450e reference project's own confirmed `trc_write_header`/`trc_format_row`
- new Start Log/Stop Log button in the main window captures every RX/TX frame on both buses into
one PCAN-Explorer-compatible `.trc` file.

---

## Part 12 — Round 2: findings from implementing every item above (2026-08-01)

Everything above this line is what you already reviewed and annotated. This section is new:
findings from actually implementing your responses, plus a fresh 4-way parallel review pass
specifically hunting for anything the implementation itself missed or introduced (regen/AC-charger
split correctness, interactions between the new safety checks, the new locking/concurrency code,
and a fresh-eyes sweep of everything else). Two things were already fixed in code during this same
pass (marked **FIXED** below, with the test added); everything else is left for you to work through
like the sections above.

### 12.1 — SAFETY BUG (found and fixed): the new hard-cut latch could be cleared by Stop/Start Bridge alone
- [x] Reviewed
**FIXED.** Item 5.1 above asked for hard cuts to latch, clearing only on a real power-cycle or
charger replug — implemented, but the first version had a real gap: `notify_session_start()` fired
on *every* `waiting_for_wake → startup` transition, and pressing **Stop Bridge then Start Bridge**
produces that exact same transition without the car's VCM ever having lost power (very likely,
since nothing about that button touches the ignition). So a user (or anyone scripting a reconnect)
could silently clear an emergency-tier latch — over-temp, overvoltage, a stale-data hard cut — with
no relation to the car actually being power-cycled. **Fix**: new `ShutdownSequencer.
rearmed_naturally` flag distinguishes a *natural* re-arm (the sequencer itself completed a real
wind-down: `'stopped' → 'waiting_for_wake'`) from a *manual* one (`arm()`, i.e. the Start Bridge
button) — only a natural re-arm now clears the latch. 4 new tests
(`tests/test_shutdown_sequencer.py`, `tests/test_management_engine.py`) confirm this directly,
including that a still-bad condition re-latches immediately even after a legitimate session start.
**Your notes:**


### 12.2 — Fault History window would have shown "all clear" during an active latched cut
- [x] Reviewed
**FIXED.** Each individual hard-tier fault entry (`low_voltage_emergency`, `overvoltage_emergency`,
`ac_overvoltage_emergency`, `over_temp_emergency`, `cell_data_mismatch_hard`, `staleness_hard`)
correctly keeps reflecting its own *instantaneous* trigger — useful, "did this specific thing
happen again" information — but none of them reflected that the *cut itself* stays latched after
its own trigger recovers. A technician opening the dedicated Fault History window during an active
latched cut would have seen every row showing "cleared." Fixed: new dedicated `hard_cut_latch`
fault entry, always live, showing whether the vehicle is actually still cut off right now — watch
this one specifically, not the individual trigger rows, to know if the vehicle is really clear.
`gui/fault_history_window.py`'s help text corrected (previously said all cuts still auto-clear).
**Your notes:**


### 12.3 — `BusConnection` holds its lock across a 150ms sleep + log call (not fixed this pass)
- [x] Reviewed
`_start_worker_locked()` (`bridge/can_backend.py`) runs its `time.sleep(0.15)` connection-attempt
wait and its `log_fn(...)` call while still holding the same lock that `connect()`/`disconnect()`/
`send()`/the `connected`/`error`/`tx_ok` properties all need. Not a corruption risk (confirmed no
deadlock — nothing else is acquired while this lock is held), but during any reconnect attempt this
can stall the GUI thread (if `connect()`/`disconnect()` are called directly from a button handler)
or delay the TX loop's next `leaf_bus.send()` call by up to 150ms. Worth narrowing the locked region
to just the `_worker` mutation, doing the sleep/log outside it — deliberately not changed this pass
to avoid touching the just-fixed lock logic twice in one session without a chance to test between.
**Your notes:**
it weems to work ok ATM. so i guess we can fix this?  

**Outcome (2026-08-01): FIXED.** Split into `_start_worker_locked()` (mutates `self._worker` only,
still under the lock) and a new `_finish_worker_start()` (the 150ms connect-wait sleep + log call,
now runs AFTER releasing the lock) - `connect()`/`_auto_reconnect_loop()` no longer hold the
connection lock across a sleep. Verified no regression via `tests/test_can_backend.py` (2026-08-03).

### 12.4 — The reconnect-race fix narrowed the bad window, didn't perfectly close it
- [x] Reviewed
Item 3.1's fix (a real lock + interruptible wait) took the "disconnect then fast-reconnect loses the
auto-reconnect monitor" window from up to 3 seconds down to microseconds — `connect()`'s
`is_alive()` check on the old monitor thread and that thread's own `_stop_monitor.wait()` returning
aren't synchronized with each other, so a vanishingly narrow race technically still exists. Given how
much smaller the window now is (thread-teardown speed vs. a 3-second sleep), this is very unlikely
to matter in practice, but flagging it precisely rather than claiming the fix is airtight. A cleaner
close would use a monotonic "generation counter" on the monitor thread instead of `is_alive()`.
**Your notes:**

**Outcome: STILL AN ACKNOWLEDGED LIMITATION, unchanged.** No generation-counter rework was done -
this remains a real but vanishingly narrow window, not practically closeable without adding
test-only synchronization instrumentation to `can_backend.py` (see `tests/test_can_backend.py`'s
own docstring, added 2026-08-03, for why the exact race isn't unit-testable either). Same
conclusion as item 3.2.

### 12.5 — Output-signal coverage audit: three more things found (from item 4.3's "check everything is mapped" request)
- [x] Reviewed
Auditing every `leaf_signals.OUTPUT_SIGNALS` key for whether anything actually drives it, beyond
`temp_segment_pct` (already fixed):
- **`voltage_latch`** (a `CHECKS` field, shown as a mapping target in the Signal Mapping tab) is
  completely dead — `build_1db()` never reads `s['voltage_latch']` at all. The bit that actually
  goes on the wire is driven entirely by the separate `GENERATED_SIGNALS` checkbox
  `voltage_latch_toggle` + an internal counter. Mapping anything to `voltage_latch` in the GUI has
  **zero effect** on the transmitted frame. Candidates: remove it from the mapping-target list
  entirely (since it can never do anything), or wire it in for real if it's actually meant to do
  something.
- **`main_relay_on`** *is* read into the `0x1DB` frame, but nothing (no mapping tie, no management
  feature) ever sets it to anything other than its static default of `1`. `docs/03` describes it as
  a redundant hard-cut-adjacent signal ("clearing it also prevents contactor closure... different
  timing from interlock, otherwise similar effect") — right now this bridge only ever drives
  `relay_cut_request`/`interlock` for a hard cut, never this third channel. Given the redundancy
  theme of this whole review: should the hard-cut path also clear `main_relay_on` for a third layer?
- **4 of the 7 `GENERATED_SIGNALS` checkboxes don't actually gate anything**: `prun`, `code_1dc`,
  `chg_time_5bc`, and `hist_5c0` are unconditionally packed into their frames regardless of checkbox
  state — only `voltage_latch_toggle`, `heartbeat_1c2`, and `seq_5eb` are actually checked in
  `_build_frame()` (`bridge/realtime_engine.py`). Unchecking one of the other 4 in the Generated
  Signals tab currently does nothing at all, silently.
(Everything else — `failsafe_status`, `discharge_pwr_sts`, `charge_pwr_sts`, `pwr_limit_reason`,
`dtc` — stays intentionally static with no live driver; already tracked as known/expected in
`docs/10` item #12, not a new gap.)
**Your notes:**
main_relay_on only works during startup. after start up there is no effect. so its fine to just be driven 1 for now
checkboxes don't actually gate anything... i mean the check box should stop sending that message... needs fixed? 

**Outcome (2026-08-01): FIXED, all three.** `voltage_latch` removed as a mapping target entirely
(user decision - it could never do anything, so removing was cleaner than wiring in dead weight);
`main_relay_on` left static per the direct answer above (documented in `03-target-signals-leaf.md`
as a deliberate decision, not an oversight); the 4 non-gating `GENERATED_SIGNALS` checkboxes
(`prun`, `code_1dc`, `chg_time_5bc`, `hist_5c0`) all now actually gate their frame content -
confirmed programmatically (2026-08-03 sweep): all 7 `GENERATED_SIGNALS` keys are gated in
`_build_frame()`, zero gaps.

### 12.6 — New `docs/14-validation-test-plan.md`
- [x] Reviewed
Gathers every "needs a test," "needs real hardware," or "can't validate yet" item from this pass
(and the ones already fixed: 6.1/6.2/6.5) into one working checklist — including every threshold
changed this session (none of which are hardware-confirmed yet, they're just edited numbers) and
the new features that have no test coverage at all yet (staleness watchdog on an individual signal,
input-plausibility rejection, the cell-data cross-check, config sanity, and — now — the hard-cut
latch, though that one now has direct unit tests as of 12.1's fix).
**Your notes:**

**Outcome (2026-08-03): DONE, and kept current.** `docs/14-validation-test-plan.md` was created as
described and has been updated throughout this review pass as items got closed - most recently
Part 1's "Boundary-value sweeps" and "Tighten loose assertions" sections, both now fully checked
off (items 6.3/6.4). It remains the live working checklist for anything still needing a real test
or real-hardware confirmation.

---

## Part 13 — Round 3: fresh full pass, hunting specifically for what Parts 1-12 missed (2026-08-01)

Requested: go through everything again from scratch looking for new failsafe bugs/concerns, with
particular attention to CAN data handling, and cover every corner. Read `management_engine.py`,
`realtime_engine.py`, `state.py`, `rz450e_signals.py`, `mapping_engine.py`, `leaf_signals.py`,
`config_profile.py`, and `fault_log.py` in full again, line by line, specifically looking for gaps
Parts 1-12 didn't already cover — not re-litigating anything already fixed/decided there. Nothing
was changed this pass — read-only, same as the original Part 1-12 review.

### 13.1 — The bridge can transmit full power with ZERO RZ450e safety data ever received
- [x] Reviewed
Every per-cell-driven protection feature has an explicit "no data yet" branch, and they don't agree
on what "no data" means to do:
- `discharge_power_taper`, `charge_target_taper` (regen), and `ac_charge_taper` all explicitly set
  `instant_factor = 1.0` ("full power") when `worst_low`/`worst_high` is `None`
  (`management_engine.py:386, 427, 475` — the literal string `'no per-cell voltage data yet - full
  power'` appears at all three sites).
- `low_voltage_cutoff` falls through to `status = 'ok'` when `worst_low` is `None` and SoC isn't low
  either (`:369-371`) — no cutoff, no warning.
- `over_temperature_derate`'s entire block is skipped (`if f['enabled'] and temp_max is not None:`,
  `:512`) when `temp_max` is `None` — not even a "no data" status or fault_log entry gets written,
  unlike the voltage features.
- The staleness watchdog (`:691-747`) cannot catch this **by design** — it explicitly excludes a
  key with `age is None` ("never seen this session") from its "worst age" calculation, because for
  *its own* purpose (catching a signal that WAS live and then stopped) that's correct. But that
  leaves the "never arrived at all" case with no other net underneath it.
- `RealtimeEngine._tx_loop` begins transmitting the instant `sequencer.phase` reaches `startup`
  (i.e. the moment real Leaf-bus traffic is seen) — there is no check anywhere that RZ450e data has
  ever been received before that happens, and `gui/app.py`'s `_start_bridge()` (`:308-311`) is an
  unconditional `engine.start_bridge()` with no such guard either.
- The only mitigation is the `last_known_good` cache (`SharedState.get_input()` falls back to it) —
  which is empty on a first-ever launch, and (see 13.2) isn't validated even when present.
**Concrete scenario**: fresh install, or `config/last_known_good.json` deleted/missing, RZ450e
adapter plugged in but not yet sending (still booting, wrong channel selected, wiring fault) —
Leaf VCM wakes and the bridge starts transmitting full discharge power, full regen/AC charge power,
and zero over-temperature protection, indefinitely, until RZ450e data eventually arrives (if it ever
does) or an unrelated fault happens to fire. This directly contradicts the design principle stated
throughout `docs/05`/`docs/12` and this file's own Part 1 ("cell voltage is the SOLE authoritative
trigger" / "we can no longer verify it's safe to keep accepting charge/regen if the data behind
that decision is stale") — "never arrived" is a strictly worse case than "went stale" and currently
gets a strictly weaker response (none at all, vs. a 60s/65s watchdog).
**Your notes:**
so, if i understand this corectly. for at least the first 60 seconds if the data is not present from the battery
then the system will use the "good known defualts" and after 60 seconds the failsafes will trigger from stail data? 
if this is the caes. we could add some safty options that enable "good battery data must be present before charging ramp can start" 
this way at least in charging, we must varify good data is coming in and with in safe ranges. 
the "driving" is less critical as 60 seconds is OK for driving. 
we need to split thses in to the 2 tabs and they must be controled by 2 difrent senarioes. 
one for driving, and one for charging. ( thses tabs are already in place) 
 the driving one is less strict. ( 60 seconds, then change hard cut to + 60) 
 the charging one is verry strict. ( good data ONLY for ramp to start charging. the 60 soft cut + 5 sec hard cut) 
 am i missing anything else here? 

**Outcome (2026-08-03):** Correction on the premise first - it was NOT "good known defaults for
60s then failsafe." Data that had **never arrived at all** was previously excluded from the
watchdog entirely (by design, to avoid false-tripping with no hardware connected) - meaning
**indefinite** full power with **no** failsafe ever firing for that specific gap, not a 60s grace
period. **FIXED for driving/general case**: `ManagementEngine` now tracks its own first-`apply()`
timestamp; a signal that's never arrived ages from that moment exactly like one that went stale,
hitting the same 60s soft / +5s hard schedule (`tests/test_management_engine.py`).

**FIXED for charging, REWORKED after follow-up clarification** (first version used a separate
custom 2.0s freshness timer - explicitly rejected in favor of reusing the same watchdog driving
gets): `require_live_data_to_charge` (default ON, Charge Emulation tab) is now a **one-time startup
gate**, not an ongoing timer - it checks that all 96 per-cell voltages plus pack temp extremes have
been seen **live at least once this session** (not from the startup cache/defaults) before the ramp
is allowed to start at all. Once that's true, ongoing protection during an active charge session is
the exact same 60s soft / +5s hard watchdog driving gets - no second/duplicate timer. Per your
"trigger the stop charging flag" answer, the watchdog's soft-cut stage now also sets
`full_charge_flag = 1` (previously only zeroed `charge_limit_kw`/`charger_limit_kw`), so it stops an
active charge session the same confirmed real-hardware way every other charge-block path already
does. See `tests/test_charge_ramp.py`'s data-gate tests and `tests/test_management_engine.py`'s
`test_staleness_soft_cut_also_sets_full_charge_flag`. The two tabs you referenced (Battery
Management for driving/discharge's ongoing protection, Charge Emulation for charging's startup
gate) now carry exactly the asymmetric-at-startup, identical-thereafter behavior you described.


### 13.2 — `last_known_good.json` is loaded straight into live safety decisions with zero validation
- [x] Reviewed
Live CAN/DID data goes through `rz450e_signals.validate_inputs()`'s `PLAUSIBLE_RANGES` check before
it's ever written to `SharedState` (`realtime_engine.py`'s `_ingest_validated`). The disk-persisted
last-known-good cache does not: `config_profile.load_last_known_good()` (`:71-78`) parses the JSON
file (catching only `JSONDecodeError`/`OSError`) and hands the raw dict straight to
`SharedState.seed_last_known_good()` (`state.py:174-178`), which just does
`self.last_known_good.update(cached)` — no range check, no type check. `get_input()` (`:104-110`)
then returns straight from this dict whenever the live `rz450e` dict doesn't have a fresher value —
which, combined with 13.1, is exactly the situation this cache exists to cover. A corrupted file, a
hand edit, a copy-pasted cache from a different/older pack, or a future schema change that shifts
units would inject an unvalidated number directly into every safety cutoff/taper calculation, with
no plausibility check standing between the file on disk and the BMS decision logic.
**Your notes:**
same asnswer as 13.2

**Outcome (2026-08-03): FIXED.** `config_profile.load_last_known_good()` now runs the cache
through the exact same `rz450e_signals.validate_inputs()` plausibility check live data gets before
seeding `SharedState` - an implausible or non-numeric (corrupted) cached value is dropped and
logged (`gui/app.py` reports the count/keys at startup), never handed to a safety decision. See
`tests/test_config_profile.py`.

### 13.3 — A hand-edited or corrupted `profile.json` can silently defeat any single safety threshold
- [x] Reviewed
`ManagementEngine.from_dict()` (`management_engine.py:780-793`) — used both by the explicit "Load
profile" button and by the automatic profile load at every app startup — copies every numeric field
present in the saved config straight into the live threshold dict with **no bounds check at all**.
Contrast `gui/panels.py`'s `ManagementPanel`, which clamps every field to a documented `(lo, hi)`
range on every keystroke (item 4.1, already fixed) — that protection only applies to *typing in the
GUI*, not to *loading a file*. `_check_config_sanity()` (`:60-83`) only checks relative ORDERING
between specific field pairs (e.g. `emergency_low_v < min_cell_v`) — it has no concept of an
individually-absurd value that still happens to be self-consistent (e.g. `emergency_low_v: -50.0`
paired with `min_cell_v: -10.0` passes the ordering check while being physically meaningless and
functionally disabling that entire protection tier). **Concrete scenario**: a `profile.json` edited
by hand, corrupted by a partial disk write, or saved by a future code revision with different units/
scale for a field that keeps the same key name — loads silently, no error, no fault_log entry, no
GUI warning distinguishing it from a normal load. The app just runs from that point on with
whichever threshold got corrupted permanently defeated.
**Your notes:**
we can fix this honistly clamping input data from this makes sence. else we may have some kind of cruption? 

**Outcome (2026-08-03): FIXED**, and folded together with 13.9 below since they're the same root
cause. `FEATURE_FIELD_BOUNDS` (the bounds table the GUI already used) moved into
`bridge/management_engine.py` itself and is now used by BOTH `gui/panels.py` (typing) and
`ManagementEngine.from_dict()` (loading a profile) - the two paths can't diverge anymore. Same fix
applied to the Charge Emulation fields via a new `leaf_signals.CHARGE_EMULATION_BOUNDS`. A value
that can't even be coerced to a number (real corruption) is dropped, keeping the existing safe
default rather than writing garbage through. See `tests/test_config_profile.py`.

### 13.4 — The hard-cut latch can be cleared by a phantom "replug" that isn't one
- [x] Reviewed
Item 5.1/12.1 fixed hard cuts to latch until "the car has been powered down and back on OR the
charger is unplugged and replugged" — implemented as `ManagementEngine.notify_charge_replug()`,
called from `RealtimeEngine._apply_charge_ramp()` (`:523-525`):
```
if leaf_wants_charge and not self._prev_charge_active:
    self.management.notify_charge_replug()
```
`leaf_wants_charge = self.sequencer.charge_active(now)` is derived **purely from the Leaf-side
0x1F2 message** (`Charge_StatusTransitionReqest == 1` or `CommandedChargePower` above idle, "fresh"
within `CHG_CMD_FRESH_S` = 0.5s) — this call happens *before* `rz_authorized` (RZ450e's own
`charge_permission_input` interlock) is even read on the next line, and is not gated on "Emulate
charger request" being enabled either. Two concrete, non-contrived ways this fires with no physical
unplug ever happening: (1) a single dropped/delayed `0x1F2` frame on the Leaf bus makes it go stale
for >0.5s and then resume — `charge_active()` flips False->True on the very next fresh frame; (2) a
real VCM's own charge-negotiation retry behavior (the exact "`trans` flapping" pattern already
documented elsewhere in this file) toggles it the same way. Either one clears an emergency-tier
latch — over-temp, overvoltage, a stale-data hard cut — with no relation whatsoever to the car
actually being power-cycled or the charger actually being unplugged, undermining the entire point
of the fix in 5.1/12.1.
**Your notes:**
yea, we need to fix this so it works as intended.

**Outcome (2026-08-03): FIXED.** A rising edge of `charge_active()` now only counts as a genuine
replug if the request was genuinely ABSENT for at least `leaf_signals.CHG_END_STOP_S` (3.0s - reused,
not a new invented number) beforehand - a single dropped/delayed 0x1F2 frame or brief VCM retry no
longer clears the latch; only a gap long enough to represent a real unplug does. A too-brief
resumption still resumes the ramp normally, it just doesn't touch the latch. See
`tests/test_charge_ramp.py`'s `test_brief_charge_dropout_does_not_clear_latch` /
`test_genuine_gap_does_clear_latch`.

### 13.5 — The Toyota checksum this project's own docs said should be used for corruption detection was never wired in
- [x] Reviewed
`docs/02-source-signals-rz450e.md` states: *"The RZ450e project chose not to wire this into its own
downstream logic, but this project should, as an additional staleness/corruption check."*
`rz450e_signals.toyota_sum_checksum()` (`:49-53`) is defined but **never called anywhere** in the
ingest path — confirmed by a full-codebase search; the only other references are in `Refrance/`'s
two read-only reference projects. Every raw-CAN decoder (`decode_020`, `decode_023`, `decode_cell_
msg`, etc.) accepts frame contents based only on a length floor and, for the two muxed messages, a
structural base/mux sanity check — nothing validates the byte-7 checksum that 10 of the 12 confirmed
messages carry. A single bit-flipped byte in a frame that still happens to land inside
`PLAUSIBLE_RANGES` (deliberately wide — e.g. any cell voltage 0.50-5.00V passes) is accepted as a
genuine reading with no way to detect the corruption, even though the data needed to catch it is
sitting right there in byte 7 of the same frame.
**Your notes:**
yeah, we must validate the data, especialy if a checksum exzists. and if it dose not, 
we are supose to be validataing the data is with in a range that makes sence.  

**Outcome (2026-08-03): FIXED.** `rz450e_signals.frame_checksum_ok()` now validates the exact 5 IDs
confirmed to carry the Toyota additive checksum (`0x020`, `0x023`, `0x358`, `0x3F1`, `0x424`) before
any of them are decoded - a mismatch (or a too-short frame on one of these IDs) is rejected and
logged, never handed to a decoder. The other 4 decoded IDs (`0x4A7`, `0x4A9`, `0x4C0`, `0x4AA`) are
confirmed to NOT carry a checksum (docs/02), so they keep relying on `PLAUSIBLE_RANGES` alone, per
your second point - that range check was already in place. See `tests/test_rz450e_signals.py`.

### 13.6 — Hysteresis "slow release" has no ceiling on how far it can jump in one delayed tick
- [x] Reviewed
`discharge_power_taper` and `charge_target_taper`'s (regen) recovery ramps compute
`max_step = dt / recovery_ramp_s` using a real measured `dt` with no cap
(`management_engine.py:395, 435`). If the TX loop's tick is delayed for any reason (GC pause, OS
scheduling hiccup, lock contention from a GUI keystroke touching the same unlocked config dict per
item 5.4/Part 12's own noted gap, a debugger breakpoint during development), the next `dt` could be
large enough that `max_step` alone lets the applied factor jump straight to the current
`instant_factor` in a single step — silently skipping the gradual recovery the hysteresis exists to
provide (anti-oscillation / anti-power-hunting, per its own design comment). Bounded by
`instant_factor` so this can never exceed what live data currently supports (not a raw safety
violation), but it is a silent defeat of the stated anti-hunting design intent under a delayed tick —
worth at least a `dt` ceiling (e.g. clamp `dt` to some max like 0.5s before using it) so a long gap
degrades to "snap to instant value" in a bounded, understood way rather than an unbounded one.
**Your notes:**
i would not this as reviewed, i thin this is ok becuse the VCM will fliter out the change. lets leave it for now

**Outcome (2026-08-03):** No action taken, per your call - left as-is.

### 13.7 — `over_temperature_derate` going dark on missing data is invisible, unlike every other feature
- [x] Reviewed
Smaller/observability-only companion to 13.1: when `temp_max` is `None`, `over_temperature_derate`'s
whole block is skipped (`management_engine.py:512`) — no `status['over_temperature_derate']` entry
is ever set, and none of that feature's four `fault_log.update()` calls run. Every voltage-based
feature, by contrast, explicitly sets a "no per-cell voltage data yet" status even with zero data
(13.1). Net effect: a missing/dead temperature sensor is silently invisible in both the Dashboard
status text and the Fault History window, while the equivalent voltage-side gap at least shows up as
text (even though 13.1 shows that text is currently paired with the wrong — "full power" —
behavior too).
**Your notes:**
yeah lets fix this.

**Outcome (2026-08-03): FIXED.** When `temp_max` is `None`, `over_temperature_derate` now sets an
explicit `'no temperature data yet - full power'` status and writes an (inactive) fault_log entry
for all four of its tracked conditions, matching how the voltage-based features already report "no
data yet." See `tests/test_management_engine.py`'s `test_missing_temp_data_reports_status_and_
fault_log_entries`. Note: this is observability-only, matching your review-scope note above - what
actually gets APPLIED with no temp data (still full power/no derate) is the separate, already-fixed
13.1 question.

### 13.8 — `capacity_bars_raw`'s default mapping can hit the "all segments off" sentinel from an implausible-but-unvalidated reading
- [x] Reviewed
Low severity, noted for completeness. The confirmed real-hardware mapping (`mapping_engine.py:95-
96`) is `capacity_bars_raw = capacity_pack1_ah * 0.07`, and docs/03 documents raw 0-14 as "full bar
display" with 15 as a separate "all segments off" sentinel, not a continuation of the scale.
`capacity_pack1_ah` above ~214.3 Ah (>100% SOH for this pack, physically implausible under normal
operation) would compute to 15+ and land on the "all off" sentinel instead of a plausible full-scale
reading. Not reachable from a healthy live sensor, but combined with 13.2/13.3 (an unvalidated
cache/profile value) it's a real, if minor, path to a misleading dash display.
**Your notes:**
this is find, the 201ah number can only go down.

**Outcome (2026-08-03):** No action taken, per your call - agreed low-risk given SOH is monotonically
non-increasing in practice.


### 13.9 NEW from user - out of bounds input data. 

we need to inforce all data input by the user is with in safe values. such as alowing the charger
 input or voltage to be set above safe values. currently this is not working in ALL cases where there is an input,
 altho i do beleave it was supose to be implmented, we need to check all cases and places. 

**Outcome (2026-08-03): FIXED, folded into 13.3.** Audited every numeric input surface: GUI typing
(`ManagementPanel`, `ChargeEmulationPanel`) was already clamped end-to-end (item 4.1, done
2026-08-01) - the gap was specifically the file-loading paths (`profile.json`'s management
thresholds and charge_emulation fields), which bypassed the GUI's clamp entirely. Both now share
the exact same bounds tables the GUI uses (`bridge/management_engine.FEATURE_FIELD_BOUNDS`,
`bridge/leaf_signals.CHARGE_EMULATION_BOUNDS`), so typing and loading can never enforce different
limits. See `tests/test_config_profile.py`.

---

## Part 14 — Round 4: fresh line-by-line pass, code AND docs together (2026-08-03)

Requested: re-read every `bridge/*.py` and `gui/*.py` file in full, alongside every `docs/*.md`
file, specifically hunting for places where code and docs have drifted apart - not re-litigating
anything already fixed/decided in Parts 1-13. Nothing changed this pass - read-only, same discipline
as every prior review round.

### 14.1 — Fault History count is stale in docs/08 (cosmetic, but worth fixing)
- [x] Reviewed

`docs/08-gui-design.md`'s Fault History section says "12 total" tracked conditions. That was
accurate as of the original Rev 12/13 implementation, but `bridge/fault_log.py`'s
`FAULT_DEFINITIONS` has grown to **19** entries since (added: `ac_overvoltage_emergency`,
`input_validation_reject`, `checksum_reject`, `cell_data_mismatch`, `cell_data_mismatch_hard`,
`config_sanity`, `hard_cut_latch`) - confirmed by counting every `self.fault_log.update(...)` call
site in `bridge/management_engine.py`, which matches the 19 catalog entries exactly (no orphans
either direction). `docs/13`'s own line 347 (Part 6) also asserts the count "matches docs/08's '12
total' claim exactly," which is now a stale claim about a stale claim. Purely a documentation
fix - the code itself (both files) is internally consistent and the `FaultHistoryWindow` already
scrolls, so no UI issue.
**Your notes:**
yeah update the doc's 

**Outcome (2026-08-03): FIXED.** `docs/08-gui-design.md`'s Fault History section now says "19
total... see that file for the current catalog" instead of a hardcoded "12," and its adjacent
layout description was also corrected to the current single-line-per-entry row (it still said the
old two-line stacked layout from before the 2026-08-01 widening). Docs-only, no code change.

### 14.2 — 0x1C2 heartbeat's documented 60ms startup delay doesn't match the code (immediate, no gate)
- [x] Reviewed

`docs/07-startup-shutdown-plan.md`'s startup timeline table says `0x1C2` starts at **t=60ms**
("trigger: VCM traffic appearing"). But `RealtimeEngine._tx_loop()` has this explicit branch for
`HVBAT_ID_1C2`:
```python
if arb_id in (leaf_signals.HVBAT_ID_1C2,):
    pass  # heartbeat is immediate from bus-wake, no start-offset gate
```
— i.e. it actually starts at **t=0**, the instant the sequencer enters `startup`, with no offset
gate at all (unlike `0x1DB`/`0x1DC` at t=65ms and `0x55B`/`0x5BC` at t=155ms, which both DO have an
explicit `timing < T_..._START` gate in the same function). A small (60ms) discrepancy, but every
other number in this timeline has been kept byte/timing-exact to the ported reference behavior, so
this is either a genuine porting gap (the 60ms gate was never actually implemented for 0x1C2) or the
doc's 60ms figure was never quite right to begin with - worth a real decision either way rather than
leaving the two disagreeing silently.
**Your notes:**
yeah, we need to keep what the orgnial log shows for start up timing. 
so just fix the docs if the code is corect. as we do want to keep the byte/timing-exact we saw 
in the log'safefrom the past ref projects. 

**Outcome (2026-08-03): the code was correct, docs/07 was FIXED.** Traced against the primary
source: `Refrance/Leaf_BMS_Emulator/battery emulator overview/04-startup-sequence.md` and
`Reports/HVBAT_PowerUp_Handshake_Report.md` both explicitly say to **"immediately start 0x1C2"**
the instant real bus traffic is detected, with no separate gate/delay constant, and neither
report's own named-constants list (`T_1DB_START=65` etc.) includes a `T_1C2_START`. The "+60ms"
figure in the handshake report's §2 timeline is measured from that specific `.trc` recording's very
first frame (including OTHER ECUs' one-shot alive frames, which arrive before the VCM's real 10ms
stream) - an artifact of when VCM traffic happened to first appear in that one capture, not a
deliberate timing gate the real battery or the emulator enforces. `bridge/realtime_engine.py`'s
`_tx_loop()` already sends `0x1C2` immediately with no start-offset gate, matching the confirmed
source behavior - `docs/07`'s table was corrected to show `0x1C2` at t=0 (with every other offset
still measured from that same instant, unchanged) rather than the previous internally-contradictory
"t=60ms" entry. No code change.

### 14.3 — The "5th wind-down trigger" fires on ANY hard cut, not just the staleness watchdog as documented
- [x] Reviewed

`docs/07-startup-shutdown-plan.md`'s "This bridge's specific staleness/shutdown interaction" section
describes a fifth wind-down trigger, specific to this bridge, as: *"the staleness watchdog's
hard-cut escalation - if RZ450e data has been stale for 65+ seconds, wind down via
relay_cut_request regardless of what the four Leaf-side triggers are doing."* The actual code
(`ShutdownSequencer._should_wind_down`) is broader than that:
```python
if hard_cut_this_tick:
    return True   # 5th trigger, specific to this bridge (docs/06/07)
```
`hard_cut_this_tick` is `RealtimeEngine._tx_loop`'s `hard_cut` variable, computed from
`leaf_state.get('relay_cut_request', 0) not in (0, None)` - true for **any** hard-cut source
(staleness escalation, but also cell overvoltage/regen/AC-charger emergency, over-temperature
emergency, and cell-data cross-check hard escalation), not staleness specifically.

This has a real behavioral consequence worth a deliberate decision, not just a doc correction: when
any hard cut fires, the bridge runs the full staged wind-down (`docs/07`'s power-down timeline,
~1.2s) and then goes **completely silent** on the Leaf bus in the `stopped` phase (`_tx_loop`
sends nothing at all while `phase in ('idle', 'waiting_for_wake', 'stopped')`) until the bus goes
genuinely quiet and it re-arms to `waiting_for_wake`. So an emergency-tier condition (e.g. a cell
hitting the overvoltage emergency threshold) doesn't just latch `relay_cut_request`/`interlock` and
hold there indefinitely broadcasting the cut - it makes the bridge stop transmitting *anything* to
the Leaf bus shortly after, then potentially re-arms and resumes normal transmission (with the hard
cut still latched, so `relay_cut_request` should reassert on the very next running tick - but there
is a window where the Leaf bus sees nothing at all rather than a continuously-asserted hard cut).

Two real questions for a decision, not just which one docs/07 should describe:
1. Is "any hard cut winds the bridge down" the intended behavior, or should this 5th trigger be
   narrowed back to staleness only (matching what `docs/07` currently says), with other hard cuts
   just holding `relay_cut_request`/`interlock` asserted indefinitely without ever entering
   `winding_down`/`stopped`?
2. If "any hard cut winds down" IS intended, should the wind-down sequence for a hard-cut-triggered
   shutdown differ from a normal ignition-off wind-down - e.g. skip straight to holding the hard-cut
   state indefinitely instead of the staged power-down timeline built for a graceful shutdown, so
   the Leaf bus is never silent during an active emergency?
**Your notes:**
no, the bridge should not wind down unless there is a real trigger to do so.
 a hard cut should not stop the bridge. all fail safe's are triggerd and the bridge still operates
 IF not comaneded to stop unless one of the known triggers to wind down happens. 
 if there is more Qustions on this let me know. else fix the code and the doc's to match what we want. 

**Outcome (2026-08-03): FIXED.** Narrowed the 5th wind-down trigger back to the staleness
watchdog's own hard-cut escalation ONLY, matching `docs/07`'s original (correct) description - a
voltage/temperature/cross-check emergency now just latches `relay_cut_request`/`interlock` via the
existing `ManagementEngine._hard_latched` mechanism and the bridge keeps running/transmitting
indefinitely, exactly as directed ("a hard cut should not stop the bridge... all fail safes are
triggered and the bridge still operates"). New `ManagementEngine.staleness_hard_cut` (reset False
at the top of every `apply()`, set True only in the staleness-watchdog block's own hard-escalation
branch) is what `RealtimeEngine._tx_loop` now passes into `ShutdownSequencer.tick()`/
`_should_wind_down()` (renamed parameter `hard_cut_this_tick` -> `staleness_hard_cut` for clarity)
instead of the old any-hard-cut `hard_cut` variable - that variable still exists and is used
unchanged for the Log panel's "HARD CUT asserted" message and `last_hard_cut` UI tracking, which
should still reflect ANY hard cut, just no longer for the wind-down decision. New tests
(`tests/test_management_engine.py`): `test_staleness_hard_cut_flag_not_set_by_a_non_staleness_
hard_cut` (an emergency low-voltage cut asserts `relay_cut_request` but leaves `staleness_hard_cut`
False) and `test_staleness_hard_cut_flag_set_by_the_staleness_watchdog_escalation` (confirms the
reverse). No doc change needed - `docs/07`'s "staleness watchdog's hard-cut escalation... regardless
of what the four Leaf-side triggers are doing" description was already accurate; only the code
needed to match it.

### 14.4 — GUI charge-stop status text always blames "RZ450e permission not granted," even when the real cause is the data gate or the staleness watchdog
- [x] Reviewed

`RealtimeEngine._apply_charge_ramp()`'s own log message already distinguishes all three reasons
`full_charge_flag` can now be forced during a blocked charge request:
```python
if not rz_authorized:
    reason = 'RZ450e has not granted charge_permission_input'
elif not data_ready:
    reason = f'no genuinely live battery data yet, cannot start on cached/default values (missing: {missing_key})'
else:
    reason = 'blocked'
```
But both live GUI status displays hardcode only the first reason, regardless of which one actually
applies:
- `gui/panels.py`'s `ChargeEmulationPanel._schedule_status_refresh()`: `'status: STOPPED - charge
  requested but RZ450e permission not granted (full_charge_flag set)'`
- `gui/dashboard.py`'s `DashboardWindow._tick()`: `'STOPPED - Leaf wants to charge but RZ450e
  permission not granted (full_charge_flag set)'`

Both simply check `bool(tx.get('full_charge_flag'))` and show this same wording whenever it's set
while `charge_emulate` is on - they have no way to tell whether the real cause was RZ450e's
interlock, `require_live_data_to_charge` (item 13.1b) not yet satisfied, or the general staleness
watchdog (item 13.1) escalating mid-charge. A user troubleshooting "why won't it charge" with
RZ450e's permission actually granted, but the per-cell data not yet live, would see a message
telling them the interlock is the problem when it isn't - actively misleading, not just incomplete.
**Your notes:**
yeah confirm both gates for starting the charger are notated in teh log as well as any other reasion like 
temp or voltage. and include the stop reasion(s) via voltage / temps / full / error/ ETC. 

**Outcome (2026-08-03): FIXED, plus confirming the start-side log was already correct.** The
start-of-ramp log line already names all three gates together (`_apply_charge_ramp`'s "0x1F2
charge request active + RZ450e permission granted + battery data genuinely live - starting..."), so
no change was needed there. The gap was entirely on the STOP/status side. New `RealtimeEngine.
charge_status_summary()` is now the single source both GUI surfaces read (`gui/panels.py`'s
`ChargeEmulationPanel`, `gui/dashboard.py`'s Charge emulation section - both previously duplicated
their own hardcoded, wrong guess), resolved in priority order from the SAME live data each
subsystem already produces instead of re-guessing:
1. `ManagementEngine`'s `staleness_watchdog` status text, if it says "charging stopped" (covers
   item 13.1's watchdog-forced stop).
2. `ManagementEngine`'s `ac_charge_taper` status text, if it says the target SoC was reached
   (covers the AC daily/extended target - a SUCCESS, not a fault; now reported as "CHARGE COMPLETE"
   instead of the old always-red "STOPPED... permission not granted" wording).
3. A new `RealtimeEngine._last_charge_gate_reason` (set inside `_apply_charge_ramp` itself, cleared
   the instant the ramp is active or genuinely idle) covering the ramp's own permission/live-data
   gate, exactly matching what's logged.

The voltage/temperature part of the request is already covered structurally, not folded into this
one line: `ac_charge_taper`'s own status label (visible right next to the main status on the Charge
Emulation tab) already reports the live per-cell voltage taper factor, and `over_temperature_
derate`'s status (Battery Management tab + Dashboard's "Battery management status" section) already
reports temperature-driven derating - both were already accurate per-feature status text before
this fix; the fix specifically targeted the ONE line that was actively wrong (the full_charge_flag
STOPPED message). Dashboard status coloring updated to match: "CHARGE COMPLETE" now renders green
(success), not the same red as a genuine STOPPED-on-a-problem message.

### 14.5 — Disabling a battery-management feature mid-cut freezes its Fault History entry as permanently "active"
- [x] Reviewed

Every feature block in `ManagementEngine.apply()` is wrapped in `if f['enabled']:` before it calls
`self.fault_log.update(...)` for its own tracked condition(s) - e.g. `over_temperature_derate`'s
four fault_log entries only get updated while that feature's checkbox is on. If a feature's fault is
currently active (e.g. `over_temp_emergency` is lit) and the user unchecks that feature's "Enabled"
box in the Battery Management tab, `fault_log.update()` simply stops being called for those keys
entirely - the entry freezes at whatever active/inactive state it last had, rather than being told
the condition is no longer being evaluated. The single master `hard_cut_latch` entry is unaffected
(it's updated unconditionally every tick from `self._hard_latched`, which itself would correctly
stop being set once the disabled feature stops contributing to `hard_cut`), so the *overall* "is the
vehicle actually cut off" signal stays accurate - only the **per-feature** breakdown entries can go
stale. Minor (observability only, no incorrect safety-relevant output), but worth a decision: should
disabling a feature explicitly clear/mark its fault_log entries as inactive, or is a frozen
per-feature entry acceptable since the master latch entry is what actually matters?
**Your notes:**
so any new data change or entry should ither A. work in real time. B. the bridge must be stoped and sterted. 
i know we have a real time enegen going to changes. it makes sence to have all those entrys as live inputs? 
if no let me know. 

**Outcome (2026-08-03): agreed, option A (live) - FIXED.** This matches the rest of the system's
own design already (mapping/threshold edits apply on the very next tick, no restart needed, per
`docs/06` section 0/`docs/08`'s Start/Stop Bridge section) - there was no reason for fault_log
entries specifically to be the one frozen exception. Every feature block in `ManagementEngine.
apply()` now has a matching `else:` branch for `not f['enabled']` that immediately calls a new
`_clear_disabled_feature()` helper, explicitly setting that feature's own tracked fault_log
entries to inactive with a `'feature disabled'` detail (instead of just letting the `update()` call
stop happening and the entry go stale) plus a `status[feature] = 'disabled'` entry so the GUI shows
that plainly instead of the last live reading. Applied to every feature with its own fault_log
keys: `low_voltage_cutoff`, `charge_target_taper` (regen), `ac_charge_taper`, `over_temperature_
derate`, `cell_imbalance_monitor`, `overcurrent_monitor`, `cell_data_cross_check`,
`staleness_watchdog` (`discharge_power_taper` has no fault_log entries of its own, so it only
gained a `status['discharge_power_taper'] = 'disabled'` line). Also reset each feature's own
persistence/hysteresis state (`_discharge_factor_applied`, `_regen_factor_applied`,
`_overcurrent_since`, `_cross_check_since`, `_stale_since`) back to a neutral value on disable, so
re-enabling later doesn't resume from a stale ramped-down factor or an old persistence-window
clock. New test (`tests/test_management_engine.py`):
`test_disabling_a_feature_mid_fault_clears_its_fault_log_entries_live` - drives
`over_temperature_derate` into an active emergency hard cut, disables the feature, and confirms all
four of its fault_log entries immediately show inactive on the very next tick, not frozen "active."

---

## Part 15 — Full fault-trigger catalog, for confirming every condition is set up correctly (2026-08-03)

Requested: every fault trigger this bridge has, with its input condition, what resets it, and what
actually happens when it fires - one row per `bridge/fault_log.py` `FAULT_DEFINITIONS` entry (19
total, see item 14.1), plus the dynamic output-clamp category. Sourced directly from
`bridge/management_engine.py`'s `apply()` (current as of Rev 34) - not re-derived from docs, so this
is a description of what the code actually does right now, for you to check against what you
*want* it to do. All thresholds shown are the shipped defaults (`default_config()`) - editable in
the GUI; a hand-edited/loaded value changes the number, not the logic described here.

**Two clearing mechanisms, used consistently throughout:**
- **Auto-clear (soft/warn tier)**: the fault_log entry's own `active` state is recomputed fresh
  every tick straight from the live reading - it clears itself the instant the condition is no
  longer true, no persistence required to clear (only some have a persistence *window before
  triggering*, noted per-row below).
- **Latch (hard tier only)**: any hard-tier trigger sets `ManagementEngine._hard_latched = True`,
  which then holds `relay_cut_request = 3` / `interlock = 0` on **every** subsequent tick regardless
  of whether that specific reading has recovered, until `notify_session_start()` (a real bus wake -
  `waiting_for_wake -> startup` following a **natural** re-arm, never a bare Stop/Start Bridge
  toggle) or `notify_charge_replug()` (`charge_permission_input` genuinely absent for
  `CHG_END_STOP_S`=3.0s, then a fresh request) is called. Clearing the latch does NOT bypass a
  still-true condition - it re-asserts on the very next tick if the underlying reading hasn't
  actually recovered too. **Only the `staleness_hard` trigger is additionally allowed to wind the
  bridge down** (item 14.3) - every other hard cut just latches and the bridge keeps running/
  transmitting.
- **Disabling the owning feature** (item 14.5) clears that feature's own entries to inactive
  immediately, live, the same tick - not a third mechanism exactly, but worth remembering per row.

**Two general notes that apply across several rows below, so they're not repeated 19 times:**
- **Escalation pairs behave differently from each other.** For the two soft→hard escalation pairs
  (staleness, cell-data cross-check), **both** the soft and hard entries stay active simultaneously
  once escalated - the hard tier doesn't replace the soft one. The two low-voltage tiers, by
  contrast, are mutually exclusive (emergency is checked first and short-circuits the soft-tier
  check that same tick).
- **Three "warn"-tier entries aren't monitor-only** (`charge_cold_block`, `discharge_temp_zero`,
  `charge_temp_zero`) - they directly zero a power-limit output despite the "warn" label, which
  reflects dash-error severity (no error / soft cut / RED hard cut), not whether the condition
  changes what's transmitted. Flagged again on each of those three rows below - worth confirming
  that's the fault-log *level* you actually want for something with a real power-cutting effect.

### 15.1 — `low_voltage_emergency` (hard)
- [x] Reviewed

**Triggers when:** **lowest** individual cell voltage (or pack `cell_min` if no per-cell data)
`<= emergency_low_v` (2.60V). Instant - no persistence window.
**Resets when:** the reading rises back above 2.60V (entry auto-clears instantly) - but see the
hard-cut **latch** mechanism above (this condition clearing does not clear `relay_cut_request`
itself). `low_voltage_cutoff` disabled also clears this entry live.
**Effect while active:** `relay_cut_request=3`, `interlock=0` (latched). Mutually exclusive with
`low_voltage_soft` this tick - emergency is checked first.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.2 — `low_voltage_soft` (soft)
- [x] Reviewed

**Triggers when:** **lowest** individual cell `<= min_cell_v` (3.00V, and NOT already at/below the
emergency tier), held **continuously** for `soft_cut_persistence_s` (2.0s) - guards against a
single-tick sag transient.
**Resets when:** the reading rises back above 3.00V at any point (auto-clears instantly, no
persistence on clearing) - or the persistence timer resets to zero if the dip was momentary and
never reached 2.0s continuous. `low_voltage_cutoff` disabled clears live.
**Effect while active:** `capacity_empty=1` (soft cut - no dash error, auto re-closes once the
condition and flag both clear). Min SoC % is evaluated alongside and shown in the status text but
**never** triggers this by itself (backup/agreement check only).
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.3 — `overvoltage_emergency` (hard, regen)
- [x] Reviewed

**Triggers when:** **highest** individual cell voltage `>= emergency_high_v` (4.20V) -
`charge_target_taper`, **regen only**, active regardless of charging context (driving or plugged
in). Instant.
**Resets when:** the reading drops back below 4.20V (auto-clears instantly) - subject to the
hard-cut latch. `charge_target_taper` disabled clears live.
**Effect while active:** `relay_cut_request=3`/`interlock=0` (latched); `charge_limit_kw` forced to
0 this tick (taper factor snaps to 0.0).
**Your notes:**
"worst individual cell voltage" should not this be the highest cell? most charged? 
or is this jsut a wording thing? describe "worst individual cell voltage `>= emergency_high_v` (4.30V)"
should be "higest individual cell voltage `>= emergency_high_v` (4.30V)" ???
also lets set this to 4.2V as our norm base config

**Outcome (2026-08-03): both FIXED.** Wording: correct catch - "worst" was ambiguous (it's the
code's own variable name, `worst_high = max(cells)`, chosen because "worst" means "closest to a
problem," which is the *highest* reading for an overvoltage check and the *lowest* reading for an
undervoltage check - not obvious out of context). Every entry in this catalog (15.1-15.4, 15.9) now
says "highest"/"lowest" explicitly in prose instead of "worst," while code snippets/variable names
elsewhere still legitimately say `worst_high`/`worst_low` (that's the actual Python identifier).
Threshold: `charge_target_taper.emergency_high_v` default changed 4.30V -> 4.20V (`bridge/
management_engine.py`) - the standard NMC charge ceiling exactly (docs/05's own researched value),
tightening the margin above the 4.15V zero-power point to 0.05V; still passes the `regen_zero_v <
emergency_high_v` config-sanity check (15.18). **Note for existing saved profiles**: this is a
`default_config()` change only - a `config/profile.json` already saved with the old 4.30V value
will keep using 4.30V until re-saved or edited, same as any other default-vs-saved-value case.

### 15.4 — `ac_overvoltage_emergency` (hard, AC charger)
- [x] Reviewed

**Triggers when:** **highest** individual cell voltage `>= ac_emergency_v` (4.20V) -
`ac_charge_taper`, **AC charger only**, config lives in the Charge Emulation tab, gated on its own
`ac_taper_enabled` checkbox (not `cfg[...]['enabled']`). Instant.
**Resets when:** the reading drops back below 4.20V (auto-clears) - subject to the latch.
Unchecking "AC charger overvoltage taper" clears live.
**Effect while active:** `relay_cut_request=3`/`interlock=0` (latched); `charger_limit_kw` forced
to 0 this tick.
**Your notes:**
same as 15.3 "higest individual cell voltage `>= ac_emergency_v` (4.30V) ??
also lets set this to 4.2V as our norm base config

**Outcome (2026-08-03): both FIXED, same as 15.3.** `ac_emergency_v` default changed 4.30V ->
4.20V (`bridge/leaf_signals.py`'s `CHARGE_SLIDERS`), matching `charge_target_taper`'s regen-side
threshold exactly. Wording corrected the same way across the whole catalog.

### 15.5 — `over_temp_emergency` (hard)
- [x] Reviewed

**Triggers when:** hottest probe (`temp_max`) `>= emergency_temp_f` (141.8°F/61°C). Instant.
**Resets when:** `temp_max` drops back below 141.8°F (auto-clears) - subject to the latch.
`over_temperature_derate` disabled clears live.
**Effect while active:** `relay_cut_request=3`/`interlock=0` (latched); discharge, charge, AND
charger limits are ALL forced to 0 this tick (both `d_factor`/`c_factor` snap to 0.0).
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.6 — `charge_cold_block` (warn — but not monitor-only, see below)
- [x] Reviewed

**Triggers when:** coldest probe (`temp_min`, falls back to `temp_max` if `temp_min` is
unavailable) `<= charge_low_block_f` (32°F/0°C).
**Resets when:** the coldest probe rises back above 32°F (auto-clears instantly).
`over_temperature_derate` disabled clears live.
**Effect while active:** **not just a label** - `cold_factor` is 0.0 at/below this point, which
zeroes `charge_limit_kw`/`charger_limit_kw` via `c_factor = min(cold_factor, hot_factor)`, ramping
back to full by `charge_derate_low_start_f` (50°F/10°C). Tagged "warn" in the catalog but has a
real output effect, unlike the true monitor-only entries below (15.9-15.11).
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.7 — `discharge_temp_zero` (warn — but not monitor-only, see below)
- [x] Reviewed

**Triggers when:** `d_factor <= 0.0`, i.e. `temp_max >= discharge_hard_stop_f` (140°F/60°C).
**Resets when:** `temp_max` drops back below 140°F (auto-clears). `over_temperature_derate`
disabled clears live.
**Effect while active:** `discharge_limit_kw` forced to 0 this tick. Also a real output effect
despite the "warn" tier - the actual hard-cut tier is the separate `over_temp_emergency` (15.5) at
141.8°F.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.8 — `charge_temp_zero` (warn — but not monitor-only, see below)
- [x] Reviewed

**Triggers when:** `c_factor <= 0.0` - either the cold-block condition (15.6), or `temp_max >=
charge_hard_stop_f` (113°F/45°C) on the hot side, whichever binds.
**Resets when:** `c_factor` rises back above 0 (auto-clears). `over_temperature_derate` disabled
clears live.
**Effect while active:** `charge_limit_kw` AND `charger_limit_kw` both forced to 0 this tick.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.9 — `cell_imbalance_warn` (warn, true monitor-only)
- [x] Reviewed

**Triggers when:** spread between the highest and lowest of all 96 individually-read cells
`>= warn_delta_v` (100mV). Requires both extremes to have live data.
**Resets when:** the spread drops back below 100mV (auto-clears). `cell_imbalance_monitor`
disabled clears live.
**Effect while active:** monitor only - status text warning, zero output effect. This bridge
cannot rebalance cells.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.10 — `overcurrent_discharge_warn` (warn, true monitor-only)
- [x] Reviewed

**Triggers when:** `current > 0` (discharging) and magnitude `>= continuous_discharge_warn_a`
(150A), held continuously for `persistence_s` (5.0s).
**Resets when:** magnitude drops back below 150A (auto-clears instantly - the persistence window
only gates triggering, not clearing). `overcurrent_monitor` disabled clears live.
**Effect while active:** monitor only - zero output effect, ever.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.11 — `overcurrent_charge_warn` (warn, true monitor-only)
- [x] Reviewed

**Triggers when:** `current <= 0` (charging/regen) and magnitude `>= continuous_charge_warn_a`
(30A), held continuously for `persistence_s` (5.0s).
**Resets when:** same pattern as 15.10, opposite current direction.
**Effect while active:** monitor only - zero output effect.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.12 — `staleness_soft` (soft)
- [x] Reviewed

**Triggers when:** the single worst-aged registered input signal (all 96 cells, 16 temp probes,
every scalar, plus the `alive_3f1`/`alive_358`/`counter_5s` keep-alive counters) reaches
`soft_cut_s` (60s) - a signal that has **never** arrived at all ages from this engine's own first
`apply()` call, not excluded forever.
**Resets when:** the worst-aged signal receives fresh data and drops back under 60s (auto-clears
instantly). `staleness_watchdog` disabled clears live.
**Effect while active:** `capacity_empty=1` **AND** explicitly `charge_limit_kw=0.0`,
`charger_limit_kw=-10.0`, `full_charge_flag=1` - the only soft cut that also force-stops charging
outright (a depleted-battery soft cut deliberately does NOT block charging; stale *data* itself
must, since safety can no longer be verified).
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.13 — `staleness_hard` (hard)
- [x] Reviewed

**Triggers when:** the same worst-age condition as 15.12, sustained an additional
`hard_escalation_s` (+5s, ~65s total) past the soft-cut point.
**Resets when:** worst age drops back under 60s (auto-clears) - subject to the hard-cut latch.
`staleness_watchdog` disabled clears live.
**Effect while active:** `relay_cut_request=3`/`interlock=0` (latched) plus the same charge-stop as
the soft tier. **This is the ONLY trigger that also sets `ManagementEngine.staleness_hard_cut`**,
the sole condition allowed to wind the bridge down (item 14.3) - every other hard cut in this
catalog just latches and keeps the bridge running.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.14 — `input_validation_reject` (warn, toggleable)
- [x] Reviewed

**Triggers when:** any RZ450e-decoded value fails `rz450e_signals.validate_inputs()`'s
plausibility range within the trailing 5.0s window (`recent_rejections()`), AND
`input_validation.enabled` is on (default on).
**Resets when:** no rejection occurs in the trailing 5s (a rolling window, not a latch - ages out
on its own). Disabling `input_validation` clears live.
**Effect while active:** the rejected value is simply never written to `SharedState` at all - it's
dropped, and the field keeps aging under its last-good value (or `None`), eventually caught by the
staleness watchdog (15.12/15.13) if sustained.
**Your notes:**
we should add enable disable for our app for testing and so we know its somthing thats happning 
and can be tested. defualt to on.

**Outcome (2026-08-03): FIXED.** No longer "always-on" - new `input_validation` feature
(`bridge/management_engine.py`'s `default_config()`, `{'enabled': True}`, no threshold fields) with
its own checkbox on the Battery Management tab (`gui/panels.py`, empty `FEATURE_FIELDS` list since
there's nothing to tune, just enable/disable). `RealtimeEngine._ingest_validated()` reads this same
flag live, every call - when off, every decoded value passes straight through unfiltered instead of
being plausibility-checked, so a deliberately-bad synthetic value can be pushed through to test
downstream handling. Disabling clears `input_validation_reject`'s fault_log entry live, same pattern
as every other feature (item 14.5). New `tests/test_realtime_engine.py` (new file):
`test_input_validation_toggle_lets_implausible_values_through_when_disabled` confirms an implausible
value is rejected when enabled and passes through when disabled;
`test_disabling_input_validation_clears_its_fault_log_entry_live` confirms the fault_log entry.

### 15.15 — `checksum_reject` (warn, toggleable)
- [x] Reviewed

**Triggers when:** any frame on the 5 confirmed checksum-bearing IDs (`0x020`/`0x023`/`0x358`/
`0x3F1`/`0x424`) fails its Toyota additive checksum within the trailing 5.0s window, AND
`checksum_validation.enabled` is on (default on).
**Resets when:** no failure in the trailing 5s (rolling window). Disabling `checksum_validation`
clears live.
**Effect while active:** the frame is rejected before decode entirely - never reaches
`SharedState`, same downstream effect as a plausibility rejection (15.14).
**Your notes:**
we should add enable disable for our app for testing and so we know its somthing thats happning 
and can be tested. defualt to on.

**Outcome (2026-08-03): FIXED, same pattern as 15.14.** New `checksum_validation` feature
(`{'enabled': True}`, no threshold fields, own Battery Management checkbox). `RealtimeEngine.
_ingest_rz_bus()` reads this flag live before calling `rz450e_signals.frame_checksum_ok()` - when
off, every frame on the 5 checksum-bearing IDs is decoded regardless of checksum validity, so a
deliberately-corrupted synthetic frame can be pushed through to test downstream handling. Disabling
clears `checksum_reject`'s fault_log entry live. New tests in `tests/test_realtime_engine.py`:
`test_checksum_validation_toggle_lets_corrupt_frames_through_when_disabled` (runs
`_ingest_rz_bus()` in a background thread against a real queued frame with a deliberately-wrong
checksum byte, confirms it's rejected when enabled and decoded when disabled) and
`test_disabling_checksum_validation_clears_its_fault_log_entry_live`.

### 15.16 — `cell_data_mismatch` (soft)
- [x] Reviewed

**Triggers when:** `|worst_low - cell_min_raw|` or `|worst_high - cell_max_raw|` (per-cell array
vs. the `0x020` pack-summary) `>= max_delta_v` (150mV), held continuously for `soft_cut_s` (60s).
Requires both the per-cell array AND the pack summary to have data.
**Resets when:** the delta drops back below 150mV (auto-clears instantly). `cell_data_cross_check`
disabled clears live.
**Effect while active:** `capacity_empty=1`. A mismatch this large usually means a decode problem,
not a real physical condition.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.17 — `cell_data_mismatch_hard` (hard)
- [x] Reviewed

**Triggers when:** the same mismatch as 15.16, sustained an additional `hard_escalation_s` (+5s,
~65s total) past the soft-cut point.
**Resets when:** the delta drops back below threshold (auto-clears) - subject to the hard-cut
latch. `cell_data_cross_check` disabled clears live.
**Effect while active:** `relay_cut_request=3`/`interlock=0` (latched). Does **NOT** set
`staleness_hard_cut` - per item 14.3 this does not wind the bridge down, only latches.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.18 — `config_sanity` (warn, always-on)
- [x] Reviewed

**Triggers when:** any of 10 cross-field threshold-ordering rules is violated (e.g.
`emergency_low_v` not `<` `min_cell_v`, `taper_zero_v` not `<` `taper_start_v`, `ac_full_v` not `<`
`ac_zero_v`) - checked every tick against the live config, regardless of any feature's own enable
flag.
**Resets when:** the offending fields are corrected (auto-clears instantly - re-evaluated fresh
every tick).
**Effect while active:** warning only - does NOT block, correct, or override the nonsensical
values; the engine keeps running with whatever's actually configured (`clamp_state()` is the
separate safety net that keeps CAN output in range regardless). Protects against a hand-edited/
corrupted `profile.json`, not just GUI typos (which are already bounds-clamped separately).
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.19 — `hard_cut_latch` (hard, derived/meta)
- [x] Reviewed

**Triggers when:** `ManagementEngine._hard_latched` is `True` - i.e. **any** hard-tier condition
above (15.1, 15.3, 15.4, 15.5, 15.13, 15.17) fired this tick or a previous tick and hasn't been
cleared yet. A derived entry, not its own independent sensor check.
**Resets when:** `notify_session_start()` or `notify_charge_replug()` (see the latch mechanism
above) - **and** the underlying condition must have also actually recovered, or it re-latches the
very next tick.
**Effect while active:** mirrors `relay_cut_request=3`/`interlock=0` while latched. **This is the
authoritative "is the vehicle actually cut off right now" indicator** - unlike the individual
per-condition entries above (which each reflect only their own instantaneous trigger, and can show
"cleared" while the vehicle is still latched off), this one is always accurate. Unaffected by any
single feature being disabled, since it reflects the aggregate outcome of whichever features
remain enabled.
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

### 15.20 — `clamp_<key>` (dynamic, one per field, warn)
- [x] Reviewed

**Triggers when:** any composed Leaf output field falls outside its documented encodable `(lo,
hi)` range in `leaf_signals.clamp_state()` (`RealtimeEngine._compose_leaf_state()`, docs/06 section
4) - e.g. a mapping tie or derived formula produces a value the CAN frame's bitmask can't
represent without wrapping. Not listed in `FAULT_DEFINITIONS` (it's per-field, dynamic) - a row
only appears once it's actually happened at least once.
**Resets when:** the field's value returns to within its documented range on a later tick
(auto-clears). Not tied to any feature's enable flag - this is a separate, always-on final safety
net.
**Effect while active:** the value is clamped to the nearest bound (`lo` or `hi`) before it's ever
packed into a frame - this already happens regardless of whether the fault_log entry exists; the
entry is purely observability (a value needing to be clamped means something upstream is
misconfigured, not a hardware fault).
**Your notes:**

**Outcome:** Confirmed correct as described, no changes requested.

**Closing note on the whole catalog - Your notes:**
i htink that in the fualt window we should move "monitor only" in to a group, and "soft cut" in it another,
 and "Hard cut" in to another so its eazy to tell what fualts are going to do what to
 the system if anything. 

**Outcome (2026-08-03): FIXED.** User decision: "i want to do it all if it was supose to be in
place and it was missed." `gui/fault_history_window.py` now builds three sections inside the
scrollable area - "Monitor Only - no cut", "Soft Cut", "Hard Cut" (`_TIER_ORDER`/`_TIER_HEADING`),
each with a heading in that tier's color, low-to-high severity top-to-bottom matching the
escalation order used throughout docs/05. `_build_fault_row()` now routes each row into the
correct tier's frame (`self.tier_frames[level]`) using exactly the `level` value already carried
by every `FAULT_DEFINITIONS` entry and every dynamically-discovered `clamp_<key>` entry (always
`'warn'`, per `RealtimeEngine._compose_leaf_state()`) - so this catalog's own per-item tier
(15.1-15.20) is literally what drives the grouping, no separate mapping table needed. Verified via
a GUI smoke test (window builds with no exceptions, all rows present).

---

## Part 16 — Round 5: full safety re-audit against docs/memory/checklist + NMC-BMS industry research (2026-08-04)

Requested: a complete code review compared line-for-line against the docs, the review checklist,
and this project's own memory, plus a check against undocumented/industry-standard NMC BMS safety
practices not already covered by `docs/12`'s research pass - specifically to confirm nothing was
missed and that every cutoff and error-handling path is genuinely correct. Method: re-read every
doc in `docs/`, all 63 items and their outcomes in Parts 1-15 above, every line of
`bridge/management_engine.py`, `bridge/realtime_engine.py`, `bridge/rz450e_signals.py`,
`bridge/state.py`, `bridge/leaf_signals.py`, `bridge/fault_log.py`, `bridge/mapping_engine.py`,
`bridge/config_profile.py`, `bridge/can_backend.py`, and the relevant GUI wiring; ran all 10
`tests/test_*.py` files (all pass) and `tests/check_profile_drift.py` (0 drift against the saved
`config/profile.json`).

**Headline result: every documented cutoff/tier (Parts 7, 9, 15 above) is implemented correctly as
designed** - low/emergency voltage, both charge/regen tapers, over-temperature (hot and cold side),
staleness watchdog, cell-data cross-check, hard-cut latching and its two genuine re-arm conditions,
input/checksum validation, output clamping, and the narrowed 5th wind-down trigger all trace
through the code exactly as `docs/05`/`docs/12`/`docs/13` describe, with no logic drift from what
Parts 1-15 already closed. No previously-fixed item has regressed. The findings below are new gaps
this pass found, not re-litigation of anything above.

### 16.1 — Two critical background threads have no top-level exception handling - an uncaught error permanently and silently kills bridge transmission
- [x] Reviewed

`RealtimeEngine._tx_loop()` (`bridge/realtime_engine.py:878-969`) has ZERO try/except anywhere in
its body - if `_compose_leaf_state()` throws for any reason (a KeyError from an unexpected config
shape, a ZeroDivisionError, any bug), the entire thread dies permanently. This IS eventually caught
by the heartbeat check (`gui/app.py`'s `HEARTBEAT_STALE_S`=2.0s, item 2.2's fix) showing "Bridge:
NOT RESPONDING" - but the bridge then goes completely silent on the Leaf bus for the rest of the
session with no auto-recovery; only a full app restart brings it back.

Worse, `_ingest_rz_bus()` (`:406-458`) and `_did_poll_loop()` (`:505-528`) have NO equivalent
heartbeat at all. Their try/except only wraps the `queue.get(timeout=0.2)` call
(`:412-415`, `:494-497`) - everything downstream of that (checksum validation, frame decoding,
`_ingest_validated()`, DID request/response handling) is unprotected. If either thread dies from an
uncaught exception, the only symptom is that RZ450e input data stops updating, indistinguishable
from "the adapter genuinely stopped sending" - eventually caught by the staleness watchdog's normal
60s/+5s schedule, but with no distinct "ingest thread crashed" indicator the way the TX loop now has
one. A real BMS's response to an internal software fault should be a defined, immediately-visible
safe state (ISO 26262's fail-safe principle, `docs/12` §8) - not "wait up to 65 seconds and let it
look like a data-staleness cutoff."
**Your notes:**
so Qustion, if we fix this here, will it carry over when we port to the STM32? if not then we can leave it. if yes then we need to fix it. 

**Outcome (2026-08-04): answered, then FIXED.** Direct answer: the literal Python `try`/`except`
mechanism does NOT port to C - but the safety principle it satisfies (an internal software fault
must not silently and permanently halt safety-relevant transmission) absolutely does need a
firmware equivalent, and is actually a HARDER requirement in firmware (a hardware/software watchdog
timer forcing a defined safe state or MCU reset is the standard automotive answer, stronger than
this app's own best-effort catch-and-continue) - see `docs/09`'s new note for the full explanation
and the two concrete firmware requirements this implies. Since the answer was yes, fixed: all three
loops (`_tx_loop`, `_ingest_rz_bus`, `_did_poll_loop`) now catch an unexpected exception, log it
(rate-limited to once/second so a fast-repeating failure can't flood the Log panel), and continue on
the next iteration/frame/DID instead of dying permanently. Critically, `_tx_loop`'s
`last_tick_monotonic` heartbeat stamp was moved to only update on a SUCCESSFUL tick (at each real
exit point, inside the `try`) rather than unconditionally at the top of the loop - a one-off failure
now self-heals invisibly (next good tick refreshes the heartbeat well inside the existing 2.0s
window), but a PERSISTENT failure still correctly trips "Bridge: NOT RESPONDING" exactly as before;
catch-and-continue must never silently mask an ongoing problem as "the loop is fine." All 10 test
files still pass.

### 16.2 — No plausibility cross-check between `temp_min` and `temp_max` (unlike the equivalent per-cell-vs-pack-summary check that already exists)
- [x] Reviewed

`rz450e_signals.PLAUSIBLE_RANGES` bounds `temp_max`/`temp_min` independently (each -60..250°F) but
never checks them against each other. A decode fault or byte-swap on `0x4A7` (`decode_temp_minmax`,
`rz450e_signals.py:151-159`) could produce a `temp_min` reading numerically higher than `temp_max`
while both individually pass their own plausibility bounds - and `over_temperature_derate`
(`management_engine.py:663-664`) feeds `temp_min` directly into the cold-side derate/block logic
(`cold_ref = temp_min if temp_min is not None else temp_max`), the exact path that exists
specifically to prevent charging into a partly-frozen pack (docs/12 finding F1). This project
already has the right pattern for exactly this class of problem - `cell_data_cross_check`
(added 2026-08-01) compares two independently-sourced readings of the same underlying quantity and
escalates on disagreement - but no equivalent exists for temp_min vs. temp_max, which are two
fields on the very same frame (`0x4A7`) and could be validated with a single `temp_min <= temp_max
+ margin` check at decode or ingest time.
**Your notes:**
we do have a min and max on the can buss live data. but no DID PID. sp we will need to compar that to the 16 temps and use the min max there to confirm. 

**Outcome (2026-08-04): FIXED, exactly as directed.** New `temp_data_cross_check` feature
(`bridge/management_engine.py`), same soft→hard escalation structure as `cell_data_cross_check`:
compares `0x4A7`'s pack-level `temp_max`/`temp_min` against the ACTUAL min/max computed live from all
16 individual `0x4AA` probes (`temp_01`..`temp_16`) - not a fixed on/off check, the real per-probe
data as directed. Default 10°F max delta / 60s soft / +5s hard (documented starting point, not yet
real-hardware-confirmed - see `docs/11`). Reports "no data to cross-check yet" (no false trigger)
when the 16 individual probes haven't arrived, which is the state every pre-existing test in this
project's suite uses (`base_inputs()` only sets `temp_max`/`temp_min`), confirmed by a dedicated test
so this new feature can't retroactively break anything else. Fully wired: `FEATURE_FIELD_BOUNDS`,
`ManagementPanel` fields/label/help text, `FAULT_DEFINITIONS` (`temp_data_mismatch`/
`temp_data_mismatch_hard`), 3 new tests in `tests/test_management_engine.py`, `docs/05`/`docs/09`/
`docs/11` updated. All 10 test files pass; `check_profile_drift.py` correctly reports the 4 new
fields as "missing from profile" (expected for any new feature, safely defaulted at load per
`ManagementEngine.from_dict()`'s known-fields pattern) until the profile is next re-saved.

### 16.3 — Two safety-relevant soft-cut flags (`capacity_empty`, `full_charge_flag`) and the hard-cut's `interlock` companion are exposed as ordinary user-mappable Signal Mapping targets, unlike `relay_cut_request`
- [x] Reviewed

`leaf_signals.py`'s own comment (`:265-268`) explains that `relay_cut_request` is deliberately kept
out of `SLIDERS`/`CHECKS` specifically because it's "driven exclusively by the battery-management
layer, never a direct mapping target." `capacity_empty`, `full_charge_flag`, and `interlock` are
all in `CHECKS` (`:98-104`) instead, which means all three flow into `OUTPUT_SIGNALS`
(`_build_output_registry()`, `:248-260`) and are selectable today in the Signal Mapping tab's output
dropdown (confirmed: `gui/panels.py:610` builds `MappingPanel`'s output list unfiltered from
`OUTPUT_SIGNALS`) - with no warning that these three are meant to be management-only.

This matters because of how `ManagementEngine.apply()` actually writes these fields: it only ever
forces `capacity_empty`/`full_charge_flag` to **1** and `interlock` to **0** when its own condition
is true (`management_engine.py:975-986`) - there is no corresponding `else: out['capacity_empty'] =
0` etc. If a user creates a mapping tie targeting one of these three (deliberately, or by picking
the wrong item from a long dropdown), whatever the mapping engine computes for it every tick
(`_compose_leaf_state()` applies mapping BEFORE management, `realtime_engine.py:781-785`) becomes
the value on any tick where management's own condition is false - management can add its own
force-on, but can never force the field back to a "clear" state the mapping tie is holding it away
from. Concretely: a tie that evaluates non-zero could keep `capacity_empty` (or `full_charge_flag`,
or a cleared `interlock`) permanently stuck asserted regardless of real battery condition, and
because this happens entirely inside the mapping layer, none of it shows up in the Fault History
window (which only reflects `ManagementEngine`'s own fault_log) - a technician would see the
dashboard say "cut off" with every management status line reading "ok."
**Your notes:**
yeah, we need to fix this. we need to be sure those are somewhere in the dashboard so we can see there output sendt.

**Outcome (2026-08-04): FIXED, both parts, plus a real bug found and corrected along the way.**
While tracing the fix, found the write-up above had actually UNDERSTATED the problem: the code
comment claiming `relay_cut_request` was "not itself in SLIDERS/CHECKS... never a direct mapping
target" was simply WRONG - it had been sitting in `SLIDERS`, fully user-mappable, the entire time,
alongside `capacity_empty`/`full_charge_flag`/`interlock`, none of which were ever actually
protected. Fixed properly rather than just updating the stale comment: new
`leaf_signals.MANAGEMENT_EXCLUSIVE_KEYS` (`relay_cut_request`, `capacity_empty`, `full_charge_flag`,
`interlock`) excludes all four from `OUTPUT_SIGNALS` (`_build_output_registry()`) so none can be
picked as a Signal Mapping tie's output, while staying in `SLIDERS`/`CHECKS`/`DEFAULTS`/`RANGES` for
everything else (dashboard display, output clamping). Second half: `ManagementEngine.apply()` now
explicitly clears `capacity_empty`/`relay_cut_request`/`interlock` back to their safe value every
tick when no condition holds, not just conditionally forcing the cut value - closing the gap even if
some other future bug ever re-exposed these fields. `full_charge_flag` deliberately did NOT get the
same explicit-clear treatment - it's legitimately also set by `RealtimeEngine._apply_charge_ramp()`
in a different module/moment, and a centralized unconditional clear inside `ManagementEngine.apply()`
would race against and undo that; removing it as a mapping target alone closes the actual
vulnerability without that risk. **Dashboard visibility**: `capacity_empty`/`full_charge_flag`/
`interlock` were already shown in the Flags section (reads `CHECKS` directly, unaffected by the
`OUTPUT_SIGNALS` change) and `relay_cut_request` was already shown in the main per-signal bar list
(reads `SLIDERS` directly) - both confirmed unaffected by this fix. Per your direct ask, also added
`relay_cut_request` to the Flags section too (previously only in the main list), so all four
safety-relevant flags are now visually grouped together where a technician would look first for "is
something cut off right now." 2 new tests added (`tests/test_mapping_engine.py`,
`tests/test_management_engine.py`); `docs/03` corrected to remove the stale/wrong claim and document
the real, now-fixed behavior. All 10 test files pass.

### 16.4 — Two standard EV-pack safety practices from `docs/12`'s own research area are not addressed anywhere in this project's docs, and were never explicitly scoped in or out
- [x] Reviewed

Checked against the same ISO 26262/UL 2580/IEC 62660 literature `docs/12` already cites, plus
general NMC/EV-pack BMS design practice, for anything `docs/12`'s original research pass didn't
cover:

- **Insulation/isolation-resistance monitoring** (HV+/HV- to chassis ground leakage) is a standard,
  often regulatorily-required EV HV-pack safety function (ISO 6469-1, UL 2580) - a degrading
  insulation fault is a shock/fire hazard independent of any voltage/temperature/current condition
  this bridge already watches. Nothing in `docs/02`'s confirmed-signal list or `docs/10`'s open
  questions mentions whether the RZ450e pack exposes an isolation-monitor signal at all, or whether
  its own internal isolation monitoring (if any) still functions once bridged into this
  configuration (same open question already raised for cell balancing, `docs/10` #7). Unlike
  overcurrent/DC-fast-charging (`docs/10` #8/#9), which got an explicit "here's why this is out of
  scope" writeup, isolation monitoring was never discussed at all.
- **Contactor/relay commanded-vs-actual-state readback** (weld detection) - this bridge asserts
  `relay_cut_request`/`interlock` as open-loop *requests* to the Leaf's own VCM; it has no signal
  path to confirm the real contactors actually opened (or that the RZ450e-side contactors, if any
  exist independent of the Leaf's own pack contactors, responded either). This is architecturally
  inherent (the bridge doesn't own or directly drive contactor hardware on either side) and may be
  a legitimate, permanent "not this project's job" - but per this project's own confirmed-vs-
  documented discipline, that should be a written, deliberate scope statement in `docs/10`, not
  silence.

Neither is a code change - both are candidates for a new `docs/10` open-question entry (matching
the treatment `docs/10` #7/#8/#9 already got) so a future session doesn't have to rediscover that
these were considered and knowingly left out, versus simply never having come up.
**Your notes:**
is there DTC's on the RZ450e that detect the insulation/isolation-resistance monitoring? we could add those 
and if true we can check that and create a result if we need to. or jsut set a flag for monitering.

lets look online, there is TDC's for weld detection for this but there no from the battery ( i dont think ) 
I think there from the VCM or other on the leaf. so we should look for where that comes from. and possinbaly 
we could detect that there? 

**Outcome (2026-08-04): researched via web search, both questions answered directly, added as
`docs/10` open questions #14/#15 (no code change - neither is implementable from what this project
currently has).**

**Isolation/insulation monitoring - yes, real, and there's a generic DTC for it.** Confirmed via
web research: **P0AA6** ("HV Isolation Fault") is a standardized, generic OBD-II code used across
the EV industry for exactly this - explicitly confirmed applicable to both Toyota/Lexus and Nissan
EVs, not a hypothetical. It's plausible the RZ450e's own ECU maintains this internally. **But it's
not currently reachable, for a specific reason**: every diagnostic signal this project reads
(`docs/02`) uses UDS **ReadDataByIdentifier (service `0x22`)** - DTC-style isolation faults are read
via a completely different UDS service, **ReadDTCInformation (service `0x19`)**, which this project
(and the RZ450e reference project) has never attempted. Concrete next step, not vague: try a UDS
`0x19` request against the same confirmed `0x747`→`0x74F` diagnostic addressing already used for
service `0x22`, and see whether the pack responds at all - genuinely unknown until that capture
happens. See `docs/10` item 14.

**Weld detection - correctly guessed, and it's structurally unreachable, not just unimplemented.**
Confirmed: this is a VCM/inverter-side electromechanical check (did a commanded contactor-open
actually produce a real disconnect), verified via vehicle-side contactor-drive/F/S-relay feedback
lines that have no representation on either CAN bus this project taps into. It isn't something the
*battery* reports in any EV architecture - your instinct that it comes from the VCM/Leaf side, not
the battery, was correct. The Leaf's own unmodified VCM (which this bridge feeds HVBAT frames, per
`docs/07`) presumably runs its own weld-detection logic entirely independently of what this bridge
sends. Unlike isolation monitoring, there's no plausible signal path to go looking for here - this
stays permanently out of scope. See `docs/10` item 15.

---

## Part 17 — Round 6: first real-bench-hardware test findings (2026-08-06)

Different in kind from Parts 1-16: those were code-review passes with no real hardware involved.
This is the first round driven by actual `.trc` captures off the real bench pack (the data logger
Part 11 asked for) - two sessions, `logs/minileaf_20260805_200831_Charging to xx% the restart.trc`
and `logs/minileaf_20260805_202221 testing cell cutoff and ramp down.trc`. Method: wrote two new
diagnostic scripts (`tests/check_charge_ramp_log.py`, `tests/check_shutdown_sequencer_replay.py`)
to decode the actual captured CAN traffic and, for the shutdown question, replay it through the
REAL `ShutdownSequencer` class rather than reasoning about the code in the abstract - root causes
below are traced from real decoded data, not inferred from reading the source alone.

### 17.1 — Log save location defaults into `config/`, not a dedicated logs folder
- [x] Reviewed

Start Log's save dialog (`gui/app.py`'s `_toggle_trc_log()`) defaulted `initialdir` to
`config_profile.CONFIG_DIR` - functional, but mixes one-off `.trc` captures in with the
deliberately-tracked `config/*.json` backup files.
**Your notes:**
I would like to change the log location to the log folder, which I've created. So the default
should be there when you hit save log.

**Outcome (2026-08-06): FIXED.** New `config_profile.LOGS_DIR` (sibling of `config/`, matching the
`logs/` folder already created) + `_ensure_logs_dir()` helper; `_toggle_trc_log()` now defaults
there instead. `logs/` added to `.gitignore` (per-session capture artifacts, not a deliberate
tracked backup like `config/*.json` - flagged to the user in case captures should actually stay
tracked). Software-verified only (`config_profile.LOGS_DIR` resolves to the real existing `logs/`
folder) - not yet re-confirmed by actually clicking Start Log in the running app.

### 17.2 — Bridge stayed awake and transmitting for ~110s past where every wind-down trigger's own timer should have fired, one real charge cycle out of three
- [x] Reviewed

Decoded the "restart" log: 2 of 3 charge cycles wound down and slept correctly - confirmed by
replaying the REAL captured Leaf-bus traffic through the actual `ShutdownSequencer` class
(`tests/check_shutdown_sequencer_replay.py`), which reproduces both real wind-downs almost exactly
(within ~1.2s of the real observed timing, matching the staged power-down timing table itself). The
3rd cycle did not: continuous Leaf-bus TX for another ~110s with zero gap, even though replaying
that exact same captured traffic predicts wind-down ~3s after the last `0x1F2` frame, regardless of
what `charge_permission_input` was doing at the time (checked both forced-True and forced-False).
**Your notes:**
there are two logs i created while testing. both of them have something slightly different in them.
however, both of them show when the charger is disconnected, it starts up the bridge and never
shuts off. i don't really know the condition to shut off in that scenario. this is after charge is
full is triggered. everything comes back online ok but it should sleep at some point? it powers
down correctly when it's supposed to, but then when i go to disconnect the charger plug, it fires
back up. i didn't leave it on for a super long time, but i don't think it would have shut down.

**Outcome (2026-08-06): PARTIALLY RESOLVED - root cause of the specific 3rd-cycle discrepancy NOT
identified, honestly documented as still open (`docs/10` #16), NOT swept under the rug.** Every
trigger's own inputs, replayed offline, look like they should have resolved normally for that cycle
too - the replay is a simplified event-driven reconstruction of `charge_permission_input` from the
logged `0x358` frames, and may not perfectly capture some live-app-only timing/threading
interaction the static replay can't reproduce. What IS fixed, unconditionally: added a 6th,
bridge-specific defensive wind-down trigger (`leaf_signals.BUS_SILENCE_TIMEOUT_S` = 30.0s,
`ShutdownSequencer._should_wind_down()`) - if the Leaf bus goes completely silent for 30s regardless
of any other trigger's state, wind down anyway. This bench rig has no ignition wiring at all, so
only the charge-session triggers can ever fire; this closes the general "none of them ever resolve,
for whatever reason" gap even though it doesn't explain this specific case. Re-ran the replay
script against both logs with the new trigger in place: timings for all 5 genuine wind-downs across
both logs are UNCHANGED (the 6th trigger never preempts a legitimate faster trigger), confirming it
doesn't introduce a regression. Documented in `docs/07` ("Sixth trigger" section), `docs/10` (#16,
the open discrepancy itself), `docs/11` (new row, marked Documented/unverified). Needs a re-test
with the Log panel's timestamps cross-referenced against a fresh `.trc` capture to actually catch
the original discrepancy in the act, if it recurs.

### 17.3 — AC charger ramp "jumps around" during a ramp-down test; related AC-charger config requests
- [x] Reviewed

Decoded the cutoff-test log: `charger_limit_kw` genuinely oscillating
(`5.20->5.00->5.10->5.20->...->5.70->5.40->...` kW) while cell voltage sat at 3.615-3.627V - squarely
inside a deliberately tight 3.62V/3.64V test band the user had configured for a separate cell-cutoff
test. Root cause: `ac_charge_taper` was built with a deliberate 2026-08-03 "no hysteresis" decision
(`docs/13` Part 9) - a pure function of instantaneous per-cell voltage, recomputed fresh every tick
from noisy individual-cell readings; inside a 20mV test band that's a large multiplier swing per mV
of noise. Separately, found while reading the ramp code (not from this log): `RealtimeEngine.
_apply_charge_ramp()`'s target tracking only rate-limited an INCREASING target
(`min(current+rate*dt, target)`) - a DECREASING target snapped instantly with zero rate limit, the
actual mechanism behind the oscillation once it's traced through: a live target/factor change could
produce an instant multi-hundred-watt jump in a single 10ms tick.
**Your notes:**
the test where i test the stop at charge % seems to work fine see log for reference. for the test
where i try to ramp down to allow the voltage to come to a steady state didn't work very well
because the ramp down wasn't high precision, we need fine 100W precision on the ramp. it seemed to
jump around quite a bit. [...] the maximum AC charging is only 6.6 kilowatt for the leaf. therefore,
it needs to ramp from let's say 7 kilowatt down to 500 watts for example. let's make this
configurable, min and max kW request for AC, and min and max kW for DC charging [...] right now it
should when it reaches the set value for zero power trigger the full charge bit and stop. we need
to add one more value for this to work, this value will be the minimum charge voltage [...] which
the ramp down will go to until it reaches the voltage value. it will hold the minimum kw charge
request from the ramp and then shut off once it reaches the zero value, so really rename the zero
power to minimum value and make a new value called cutoff or stop charging for a set voltage [...]
we also need a bit more hysteresis time for the charger, let's make that configurable as well.

**Outcome (2026-08-06): FIXED, all parts.** Reversed the 2026-08-03 no-hysteresis decision based on
this new real-hardware evidence (`docs/05`'s "AC charger taper rework" section documents the
reversal explicitly, replacing the old note rather than silently deleting it) - `ac_charge_taper`
now carries the same fast-attack/slow-release hysteresis the sibling regen/discharge tapers already
had, via new configurable `ac_recovery_ramp_s`. `ac_zero_v` renamed to `ac_min_v`; the taper's floor
changed from a literal 0kW to configurable `ac_min_kw` (holds there instead of driving to true
zero, and does not force a ramp value already below the floor UP to it - preserves the ramp's own
startup precision). New `ac_cutoff_v` (interior point of the already-researched safe envelope,
between `ac_min_v` and the existing `ac_emergency_v` NMC ceiling) deliberately ends the session
(`full_charge_flag`) once crossed, gated on `charge_permission_input` the same as the existing
SoC-target-reached stop (must not fire while simply driving). New configurable `ac_min_kw`/
`ac_max_kw` (6.6kW default ceiling = the Leaf's real onboard AC charger max, per user spec) clamp
both this taper's floor and the manual `charge_target_kw` ramp target. `dc_min_kw`/`dc_max_kw` added
as a PLACEHOLDER ONLY (confirmed scope with the user - no active DC charging logic exists yet),
surfaced on the Future tab. The ramp's symmetric-rate-limiting bug fixed separately: decreases now
rate-limit exactly like increases always did, giving real fine (100W-scale, matching the user's
explicit ask) control over a falling target either from a live edit or the new min/max clamp.
7 new tests added (`tests/test_management_engine.py`, `tests/test_charge_ramp.py`) covering the
floor behavior, the cutoff, the hysteresis, and the symmetric ramp - all pass. An older saved
`profile.json`'s `ac_zero_v` value migrates automatically to `ac_min_v` on load
(`bridge/config_profile.py`), confirmed against the real saved profile. `docs/05`, `docs/09`,
`docs/11` all updated. Software-verified only (unit tests + a replay of the OLD oscillation bug
against the real log, confirming the diagnosis) - the NEW behavior (min-kW floor, cutoff voltage,
hysteresis) has not yet been re-tested against real charging hardware; see `docs/15` for the
specific real-hardware follow-up items.

**Correction (2026-08-06, same day, found while writing 17.5's regression test): the root-cause
attribution above was WRONG - `ac_charge_taper` was never actually engaged during this log at
all.** Checked directly: `_ramp_factor(3.62, floor=ac_full_v=4.00, ceiling=ac_min_v=4.15)` and the
same at 3.64V both evaluate to exactly `0.0` - the cell voltage throughout the ENTIRE log
(confirmed by scanning every `0x020` frame in the capture: 3.616-3.640V range, never higher) never
once entered the taper's 4.00-4.15V window. `instant_factor` was a constant `1.0` (full power,
completely untapered) the whole session in BOTH the old and new code - the taper's own hysteresis
(or lack of it) cannot be what produced the observed oscillation, in either this log's small early
swings or the later `33.1→...→0.0→33.1` repeating cycle (also re-checked: same 3.6-3.7V range
throughout). The real mechanism is almost certainly the SECOND thing found in this item's own
original writeup above - `_apply_charge_ramp()`'s asymmetric rate-limiting bug (decreasing target
snapped instantly) - most likely triggered by the user live-adjusting the "Charger ramp target
(kW)" GUI field during the test (each keystroke fires an immediate config write via
`ChargeEmulationPanel`'s `trace_add`, consistent with the small-scale swings) or by real
Leaf-commanded `0x1F2` power values changing (confirmed varying between 100/150/160 during the
later repeating-cycle window, though `trans` stayed continuously active so a ramp reset from a
literal request drop looks unlikely on its own). That specific bug was already fixed earlier the
same session (this item's own outcome above, "the ramp's symmetric-rate-limiting bug fixed
separately") - independent of and prior to 17.5's AC-taper convergence-rate rework below. 17.5's
fix is a real, sound improvement for when the taper genuinely IS engaged (a pack actually
approaching 4.00V+) - the user's CC-CV physics reasoning is correct and general - but it is **not
confirmed to be what fixed the specific evidence cited in this log**, and the `docs/11`/`docs/15`
entries referencing "confirmed against this log" have been corrected to reflect that distinction.

**THIS CORRECTION WAS ITSELF WRONG - see item 17.6 below.** It checked cell voltage against the
CODE DEFAULT `ac_full_v`/`ac_min_v` (4.00V/4.15V) - but the user had NOT been running with the
defaults for this test; they had deliberately bracketed those thresholds down to 3.62V/3.64V
(this project's own "bracket the threshold" technique, `docs/15`), which the log's voltage sits
squarely inside. 17.6 re-verifies numerically against the actual configuration used and confirms
the ORIGINAL diagnosis (this item's own outcome above, before this now-retracted correction) was
right all along.

### 17.4 — Charge Emulation tab number fields accept empty/out-of-range input with no visual feedback
- [x] Reviewed

Unlike the Battery Management tab's `ManagementPanel` (which flags "invalid"/"clamped" next to
every field, added `docs/13` Part 4), `ChargeEmulationPanel._set_float`/`_set_int` silently `pass`
on a `ValueError` (empty/non-numeric input just leaves the last-good config value with zero
indication anything was wrong) and silently clamp out-of-range input with no visual flag either.
**Your notes:**
also check that we can't enter a non valid value in the charger inputs. it seems i was able to
enter nothing and also outside safe limits.

**Outcome (2026-08-06): FIXED.** Ported `ManagementPanel`'s exact invalid/clamped `flag_lbl`
pattern into every numeric Entry on the Charge Emulation tab (including the new fields from 17.3
and the new DC placeholder fields on the Future tab). Directly verified (not just by inspection):
scripted an empty-string entry -> flag reads "invalid", config value unchanged; scripted an
out-of-range entry (999 against a 0-5.0 bound) -> flag reads "clamped", config value clamped to 5.0.

### 17.5 — 17.3's hysteresis fix was itself the wrong model: a closer look at the same log showed a repeating hunt, not just a rough jump
- [x] Reviewed

Continuing 17.3's investigation the same day: re-decoded the FULL `.trc` log (not just the tail
originally inspected) and found a much clearer picture of the failure than "jumps around" - a
repeating full-cycle hunt late in the log (`33.1→27.5→21.9→16.3→10.6→5.0→0.0→5.0→33.1→...` kW,
cycling every ~3s for over 20 seconds straight), including a `5.00→33.10` kW jump in a single 10ms
tick - well beyond what 17.3's fixed-time-constant hysteresis fix (instant fast-attack down, slow
release up) would have prevented, since that fix kept the DOWN direction instant, copying the
discharge/regen tapers' own design.
**Your notes:**
so for the ramp, what the system must do is ramp slowly the closer it gets to minimum kW request.
in yesterday's test it was jumping from 4.4kw to 0, never anything in between [...] it should ramp
down slowly. of course the closer it gets to its set value the slower it will ramp. here is what
we are solving for, under charge load, the voltage will jump up. so lowering the input current
will reduce this. we are using the CC-CV charge characteristics, therefore the ramp must be gentle
enough to change the input in a way where the closer it gets to the minimum power set point, the
gentler it needs to adjust. 500W of input power jump could bump the current up a lot and then it
will hunt for a steady state. that's what it's doing now.

[Follow-up, after a proposed exponential/proportional-filter design]: i think this makes sense. we
already have a 0-7 ramp rate. i suggest we use that to control how fast we ramp. the closer we get
to the required minimum power the slower we change the ramp. [...] especially when trying to ramp
down. then use a slower ramp up and it will help not hunt but instead self-adjust because the
closer we get to the min set point the slower we need to respond. [Clarified when asked whether the
transmitted uprate bits should reflect this too:] the 0-7# is a driven number that gets sent out on
the bus. so it needs to kinda represent what's going on with the request in case it's used
somewhere else in the system [...] stick with always starting at #7. then change as required if
things are in the window that is supposed to ramp.

**Outcome (2026-08-06): FIXED - the fast-attack/slow-release hysteresis from 17.3 removed and
replaced the same day with a dynamically-selected 0-7 convergence rate, per the user's own
architecture.** Correct root-cause diagnosis: this is a CC-CV charging control loop, not a danger-
response cutoff - an instant downward step (even with a slow release afterward) lets voltage sag
more than necessary, the taper reads that as safe and lets power back up, overshoots, and the loop
hunts. Fast-attack-on-a-dip is the right model for the discharge/regen tapers (arresting real cell
sag under load, unchanged) but the wrong one here. New design (`bridge/management_engine.py`'s
`ac_charge_taper` block + new `_select_ac_uprate_level()`): the taper dynamically self-selects one
of the existing 0-7 `chg_uprate_level` rates (`leaf_signals.CHG_RAMP_RAW_PER_S`, real-hardware-
confirmed - 2.0kW/s at level 7, halving per level down) based on remaining distance to target,
always starting at level 7 the moment convergence begins, downshifting/upshifting symmetrically
(with hysteresis on the level switch itself, via new `_AC_LEVEL_DOWNSHIFT_KW`/
`_AC_LEVEL_HYSTERESIS_MULT`, so the SELECTED LEVEL doesn't flap right at a boundary - verified
directly with a scripted oscillation right at a threshold: stayed locked at one level instead of
flapping). Each step is clamped to land exactly on target, never overshoots. Per the user's explicit
clarification, the dynamically-selected level is now what's actually TRANSMITTED in `0x1DC`'s uprate
bits while the taper is genuinely converging (`ManagementEngine.ac_uprate_level`, read by
`RealtimeEngine._compose_leaf_state()`) - overriding the manually-configured `chg_uprate_level` only
during that window, "always starting at #7" then changing "as required" exactly as directed.
`ac_recovery_ramp_s` (17.3's fixed-time-constant field) removed cleanly - never reached a saved
profile, no migration needed. New tests in `tests/test_management_engine.py` include a directed
regression test that puts cell voltage genuinely INSIDE the taper's `ac_full_v`-`ac_min_v` window
(the DEFAULT 4.00-4.15V window specifically - a synthetic scenario, not a replay of the real log's
own sequence) and confirms the new algorithm never produces a same-tick multi-kW jump there,
converges without overshoot, and correctly starts at level 7. `docs/05`, `docs/08`, `docs/09`,
`docs/11`, `docs/15` all updated. Software-verified only at the time this item was written (unit
tests + level-selection logic verified standalone) - **see item 17.6 below**, written shortly
after this one: the "2026-08-05 log never reached the taper's window" claim above turned out to be
checked against the wrong (default) configuration, and a REAL replay of the actual log against
this exact new algorithm is now also available, with real-data results. `docs/15` B20 tracks the
remaining live-hardware retest, explicitly flagged as the item most likely to need a follow-up
tuning pass (the 7 threshold constants are new, not real-hardware-confirmed).

### 17.6 — 17.3's original diagnosis was RIGHT after all - my own "correction" (above, in 17.3) was wrong, caused by checking the wrong config values
- [x] Reviewed

17.3's own correction above concluded the AC taper was never engaged during the 2026-08-05 log,
based on checking cell voltage (3.616-3.640V) against the CODE DEFAULT `ac_full_v`/`ac_min_v`
(4.00V/4.15V). That check itself was correct - but it checked the wrong configuration. The user
had NOT been running with the defaults for this specific test; they had deliberately bracketed
`ac_full_v`/`ac_min_v` down to 3.62V/3.64V (`docs/15`'s own "bracket the threshold, not the
battery" technique - moving a feature's threshold to sit just past the pack's real, current,
everyday voltage to test the logic without needing genuine extremes), exactly matching the log's
filename ("testing cell cutoff and ramp down").
**Your notes:**
you were correct. i set the values to 3.62 and 3.64 for testing. so that's correct. you can retest
with those values if you want. [...] of course that's not the typical, but that's what i tested
at. [clarifying, when asked:] typical is what you already tested with.

**Outcome (2026-08-06): Re-verified numerically against the real log with the correct (actually-
tested) values - 17.3's ORIGINAL diagnosis is confirmed, not the "correction."** Checked
`_ramp_factor(3.62..3.64, floor=3.62, ceiling=3.64)` - the log's actual cell voltage range sits
squarely inside that window (unlike the 4.00-4.15V default window, which it never reached).
Cross-referenced the OLD (pre-fix) zero-hysteresis formula directly against the real log's
`0x020` cell_max readings and observed `charger_limit_kw` values, with `ramped_kw=92.3` (the idle
DEFAULTS ceiling): predicted `50.07/44.44/27.54/21.90/16.27/5.00/0.00` kW against OBSERVED
`50.00/44.40/27.50/21.90/16.30/5.00/0.00` kW - an exact match to within encoding rounding. This is
airtight: the taper's own zero-hysteresis, per-cell-voltage-driven formula, evaluated against the
user's ACTUAL bracketed test window, fully explains the log's observed oscillation (both the small
early swings from single-ADC-count voltage noise, ~1.2mV, producing large relative swings inside
a tight 20mV window, and the later repeating cycle, which lines up with real, continuous, slowly-
drifting voltage climbing steadily through the window and dropping back - itself plausibly the
real CC-CV hunting interaction the user described: cut power hard, voltage relaxes, release power,
voltage climbs, repeat).

**Retest performed as requested**, using a new diagnostic script
(`tests/check_ac_taper_log_replay.py`, added this session) that replays the real captured cell-
voltage trace from the log through the ACTUAL current `ManagementEngine.apply()` (real elapsed
time between calls) with `ac_full_v=3.62`/`ac_min_v=3.64` - the same bracketed values actually
used. Result: worst single-call jump across the entire ~1780-second real replay is **0.819kW**
(vs. the original log's real multi-kW same-tick jumps) - the new dynamically-selected 0-7
convergence rate genuinely fixes the exact real scenario captured in this log, confirmed against
real data, not just a synthetic scenario. The 3.62V/3.64V window is explicitly a deliberate
bracketed TEST configuration, not the "typical"/default operating thresholds (4.00V/4.15V, per
user clarification "typical is what you already tested with") - 17.5's synthetic regression test
(midpoint 4.075V, the default window) remains valid coverage for ordinary/typical operation
alongside this real-data confirmation for the bracketed-test scenario.

**Docs corrected back** (17.3's "Correction" paragraph above is left in place, not deleted, per
this project's own convention of keeping the mistake-and-correction trail visible rather than
silently rewriting history - it explains WHY the wrong conclusion was reached, which remains
useful): `docs/05`'s "REVERSED... then REWORKED" note, `docs/11`'s AC-taper row, and `docs/15`'s
B20 intro all updated to reinstate the original diagnosis with this stronger, now-real-data-backed
evidence, replacing the incorrect walk-back. **Lesson for future sessions, worth remembering
generally for this project**: when diagnosing a bug from a real `.trc` capture, always check the
ACTUAL live/session-configured threshold values that were in effect during that specific test, not
the code defaults - `docs/15`'s own "bracket the threshold" technique means a real test session
frequently runs with thresholds deliberately moved away from their defaults, so assuming defaults
were in effect during a specific historical capture is not a safe shortcut.

---
