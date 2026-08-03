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

### 1.2 — The "sanity cross-check" between per-cell data and pack summary is documented but not built
- [x] Reviewed
docs/02:34 and docs/04:77 both describe `0x020`'s `cell_min`/`cell_max` as a "sanity cross-check"
against the 96 individual cell messages. In code (`management_engine.py:172-175`), they're used
**only** as a fallback when the per-cell list is totally empty — no comparison logic exists for the
normal case where both have data.
**Your notes:**
fallback is OK, but we should do the live cross check and if thing start to get outside a delta that is safe we need to 
use trigger a fail safe just like the watch dog. of corse we need to add those reasions for fail safe to the fualt page.

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


---

## Part 4 — Configuration & input-safety findings

### 4.1 — No sanity bounds or feedback on manually-typed safety thresholds
- [ ] Reviewed
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

### 5.3 — `docs/09`'s STM32 export example is stale relative to the actual config schema
- [x] Reviewed
The illustrative JSON in `docs/09-stm32-export-format.md` omits `emergency_temp_f`,
`cell_imbalance_monitor`, `overcurrent_monitor`, and `soft_cut_persistence_s` — all real fields in
`default_config()`. Not a functional bug (the real export via `to_dict()` includes everything) —
just a stale doc example that could mislead a future firmware-porter.
**Your notes:**
yeah fix this so we dont miss it. 

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
 
### 6.2 — Overvoltage emergency hard-cut has thin, indirect test coverage **(NEW)**
- [x] Reviewed
Unlike low-voltage emergency and over-temp emergency (each has a dedicated test checking both the
cut and its `fault_log` entry), the mirror-image overvoltage tier (`management_engine.py:289-304`,
fault key `overvoltage_emergency`) is only incidentally exercised inside
`test_charge_ramp.py:201-217`, which checks `relay_cut_request == 3` but never asserts the
`fault_log` entry at all — a coverage gap on one of only 3 hard-cut fault types.
**Your notes:**
add it.

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

### 6.4 — A couple of assertions are looser than the underlying math requires **(NEW)**
- [x] Reviewed
`test_f1_cold_block_uses_coldest_probe`'s second check only asserts `charge_limit_kw > 0.0` where an
exact value was computable and available. `test_f3_cold_derate_ramp`'s midpoint check accepts a
`0.35 < factor < 0.65` range for a case where the linear ramp formula gives an exact expected factor
of 0.5 — loose enough that it would still pass if the ramp curve were subtly non-linear or otherwise
wrong. Not urgent, but worth tightening opportunistically.
**Your notes:**
yeah check coldest makes sence if i understand this comment corectly. do we need to corect somthing here? 

### 6.5 — `manual_reset` is only tested against an instantaneous emergency condition
- [x] Reviewed
`test_fault_log_manual_reset_does_not_change_live_cut_decision` only exercises reset against an
always-true emergency condition. It never tests the more realistic, common case `fault_log.py`'s own
docstring emphasizes as its primary motivation — resetting a **soft** or **warn**-tier entry whose
condition has since auto-cleared.
**Your notes:**
ok, so we need to fix this? 

### 6.6 — Confirmed clean on this pass (for context, not action items)
- [x] Reviewed
`FaultLog`'s own unit tests (`test_fault_log.py`) are tight and non-tautological — rising-edge
counting, persistence round-trip of `active`, and re-trigger-after-reset are all checked against
exact values. `test_output_clamping.py` and `test_mapping_engine.py` use exact-value assertions
throughout. `FAULT_DEFINITIONS`'s count matches docs/08's "12 total" claim exactly. Every window
geometry/sizing claim checked in docs/08 (1430×835 main window, 374/680/374 pane widths, 420×900
Fault History window) matches the code exactly.
**Your notes:**


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

### Discharge power taper
| Field | Default | Verified | Notes |
|---|---|---|---|
| Taper start V (full power) | 3.0 V | Documented | |
| Taper zero V (zero power) | 2.6 V | Documented | Matches soft-cut floor by design |
| Recovery ramp | 3.0 s | Documented | Fast-attack/slow-release hysteresis; see 4.1 re: unvalidated-value consequence |
- [x] Reviewed — **Your notes:**
changed zero power to 3.0 changed zero power to 2.6 so it matches the cut off.
we should have a | Min SoC % (backup check, never acts alone) | 10.0 % - 2% this way we have redundency. 

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

### Cell imbalance monitor (warn only)
| Field | Default | Verified | Notes |
|---|---|---|---|
| Warn spread | 100 mV | Confirmed (software, logic only) | Never cuts/derates; see 6.3 re: threshold itself not directly tested |
- [x] Reviewed — **Your notes:**
changed to 100mv 

### Overcurrent monitor (warn only)
| Field | Default | Verified | Notes |
|---|---|---|---|
| Discharge warn | 150 A | Documented | Sensor saturates at ±204.7A — cannot see the pack's real ~500A/660A range at all |
| Charge/regen warn | 30 A | Documented | Above Leaf AC charger's ~19A max |
| Persistence | 5.0 s | Documented | See 6.3 re: boundary itself not directly tested |
- [x] Reviewed — **Your notes:**


### Staleness watchdog
| Field | Default | Verified | Notes |
|---|---|---|---|
| Soft cut after | 60 s | Documented | See 1.1 — doesn't cover per-cell/temp signals |
| Hard cut escalation | +5 s | Documented | |
- [x] Reviewed — **Your notes:**
now added data validation scheem. needs its own implmentation in to the watch dog. 

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

## Part 9 — Control-behavior review (the way things are controlled, not just the numbers)

- [x] **Soft-cut vs. hard-cut split** — matches intended design, reserved for genuine emergencies +
  staleness escalation. **Your notes:**

- [x] **Discharge-taper hysteresis** (fast-attack / slow-release, default 3.0s) — only feature
  carrying state between ticks; confirms the intended anti-hunting behavior when valid input is
  given (see 4.1 for what an invalid input does to it). **Your notes:**

- [x] **Charge/regen taper is a pure function of instantaneous voltage** (no hysteresis, unlike
  discharge) — intentional per docs/05. Worth confirming you still want that asymmetry. **Your
  notes:** 
  regen we should add some hysteresis? same as discharge? also split regen from charger as descussed.
  charger dose not have the hysteresis? 

- [x] **`full_charge_flag` re-arm has no physical-replug equivalent** (docs/10 #1, still open).
  **Your notes:** humm. thsi was from my memory, an unplug and replug reset. i think i mentioned this already in this doc.

- [x] **Charge-ramp dual-trigger requirement** — mismatch forces an explicit stop rather than
  falling back to a static value; see 5.2 re: the two status displays for this feature disagreeing.
  **Your notes:** yeah see notes on 5.2. i think that covers this one in detail. 

- [x] **4 ported shutdown triggers + 1 bridge-specific staleness trigger** — all five converge
  through one `_should_wind_down()` check each tick. **Your notes:**

- [x] **Output clamping** — guarantees nothing out-of-range reaches the CAN bus regardless of what
  upstream logic produces. **Your notes:** yeah and now added user input clamping.

- [x] **DID/PID polling cadence** — see 2.3; effectively ~15s per specific DID, not ~5s.
  **Your notes:** yeah notated how to change this in 2.3

- [x] **Auto-reconnect on connection drop** — see 3.1/3.2; can silently stop working, or silently
  override a manual disconnect, under a specific race. **Your notes:** see notes in those 3.1/3.2

- [x] **Fault auto-clear vs. latching** — see 5.1; the single biggest open behavioral decision left
  in the whole management layer. **Your notes:**
yeah we need to fix this, as notated in 5.1. unless im mestaken and the option to clear automaticaly with "power cycle"
  VS manuialy is already in place? let me know. 
---

## Part 10 — Safety-relevant open questions already tracked in `docs/10`

- [x] **#2 — exact staleness-watchdog behavior when only some source groups go stale.** Item 1.1
  above is a concrete, worse-than-assumed answer — the doc's own wording assumed "raw-CAN covers
  voltage/current/temp" as one group; in code it doesn't. **Your notes:** yeah we need all can added to watchdog as descussed. VALIDATE data. 

- [x] **#4 — `charge_permission_input` "no interlock present" default.** Currently fails safe only
  as a side effect of `get_input()` returning `None`, not a written, deliberate policy. **Your
  notes:**
umm explin this more? if i understand corectly. we need both interlock's? thought we changed that
 yesterday as it was implmented incorectly. and the doc's should have been updated? 

- [x] **#7 — does the RZ450e pack's own internal cell-balancing hardware still run** in this
  configuration? Directly affects how much weight to put on the cell-imbalance monitor over time.
  **Your notes:**
yeah need to add to the test doc jsut so it dose not get forgotten. 

- [x] **#8/#9 — overcurrent monitor and DC fast-charging are both outside what the current sensor
  can see** (±204.7A signal ceiling vs. a 500A fuse / ~660A peak / ~430A DC-fast-charge pack).
  **Your notes:**
yeah add to test doc, as we cant validate this as of yet. 
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
- [ ] Reviewed
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
- [ ] Reviewed
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
- [ ] Reviewed
`_start_worker_locked()` (`bridge/can_backend.py`) runs its `time.sleep(0.15)` connection-attempt
wait and its `log_fn(...)` call while still holding the same lock that `connect()`/`disconnect()`/
`send()`/the `connected`/`error`/`tx_ok` properties all need. Not a corruption risk (confirmed no
deadlock — nothing else is acquired while this lock is held), but during any reconnect attempt this
can stall the GUI thread (if `connect()`/`disconnect()` are called directly from a button handler)
or delay the TX loop's next `leaf_bus.send()` call by up to 150ms. Worth narrowing the locked region
to just the `_worker` mutation, doing the sleep/log outside it — deliberately not changed this pass
to avoid touching the just-fixed lock logic twice in one session without a chance to test between.
**Your notes:**


### 12.4 — The reconnect-race fix narrowed the bad window, didn't perfectly close it
- [ ] Reviewed
Item 3.1's fix (a real lock + interruptible wait) took the "disconnect then fast-reconnect loses the
auto-reconnect monitor" window from up to 3 seconds down to microseconds — `connect()`'s
`is_alive()` check on the old monitor thread and that thread's own `_stop_monitor.wait()` returning
aren't synchronized with each other, so a vanishingly narrow race technically still exists. Given how
much smaller the window now is (thread-teardown speed vs. a 3-second sleep), this is very unlikely
to matter in practice, but flagging it precisely rather than claiming the fix is airtight. A cleaner
close would use a monotonic "generation counter" on the monitor thread instead of `is_alive()`.
**Your notes:**


### 12.5 — Output-signal coverage audit: three more things found (from item 4.3's "check everything is mapped" request)
- [ ] Reviewed
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


### 12.6 — New `docs/14-validation-test-plan.md`
- [ ] Reviewed
Gathers every "needs a test," "needs real hardware," or "can't validate yet" item from this pass
(and the ones already fixed: 6.1/6.2/6.5) into one working checklist — including every threshold
changed this session (none of which are hardware-confirmed yet, they're just edited numbers) and
the new features that have no test coverage at all yet (staleness watchdog on an individual signal,
input-plausibility rejection, the cell-data cross-check, config sanity, and — now — the hard-cut
latch, though that one now has direct unit tests as of 12.1's fix).
**Your notes:**

