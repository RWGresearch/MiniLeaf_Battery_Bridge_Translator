# REVISION: 30
"""MiniLeaf Battery Bridge Translator - entry point.

Bridges a Lexus RZ450e HV battery to a Nissan Leaf's CAN bus: a configurable
signal-mapping + battery-management layer, live CAN in / live CAN out. See
docs/01-project-goals.md for the full picture.

Run: py main.py
Dependencies: pip install -r requirements.txt
  - python-can (real PCAN hardware; the app degrades to a hardware-free DEMO
    mode automatically if this isn't installed - see bridge/can_backend.py)
GUI is plain tkinter/ttk (stdlib, no extra GUI dependency) - see gui/theme.py.
"""
from gui.app import App

REVISION = 30
REV_DATE = '2026-07-31'

# Rev 1: initial milestone-1 app - adapters, signal registries, mapping
# engine, battery-management engine with researched defaults, real-time
# fixed-period TX engine with staleness watchdog, ported startup/shutdown
# sequencer, config profile save/load. UDS/LeafSpy responder (milestone 2)
# and Leaf->battery PID/DID requests (future) are explicitly not implemented.
#
# Rev 2: first user review pass, 7 fixes -
#   (1) RZ450e is one combined adapter connection, not two (bus1/bus2 split
#       by CAN ID internally, matching the real hardware wiring) -
#       bridge/realtime_engine.py, bridge/can_backend.py, gui/app.py.
#   (2) Added a bottom log panel (ported queue.Queue + timestamped-drain
#       pattern from the Leaf project's own log) - gui/app.py. Wired into
#       connection events, sequencer phase transitions, and soft/hard-cut
#       assertions.
#   (3+5) Replaced the fixed equal-thirds column layout with a resizable
#       ttk.PanedWindow (draggable sashes) - the middle configurator pane was
#       too narrow and cutting off mapping-row content, specifically hiding
#       the "?" buttons - gui/app.py. Also redesigned each mapping tie as a
#       compact 2-line card instead of one long row - gui/panels.py.
#   (4) New Dashboard window (gui/dashboard.py) with bar gauges ported from
#       the Leaf project's own DashWindow, showing input -> conversion ->
#       output side by side per signal, plus flags/generated-signals/
#       management-status sections.
#   (6) Fixed the charge-target-taper: it was gated by an SoC threshold
#       before tapering would engage. Corrected per user: the CC->CV taper
#       must be driven ONLY by individual cell voltage, continuously, at any
#       SoC (a single imbalanced cell can approach the ceiling early) - SoC
#       is now only used for the separate daily/extended target stop-point,
#       not the taper ramp itself - bridge/management_engine.py.
#   (7) Signal-mapping dropdowns now prefix every entry with its source CAN
#       ID/DID (e.g. "[0x020] Pack voltage") - bridge/rz450e_signals.py,
#       bridge/leaf_signals.py, gui/panels.py.
#
#   Also fixed along the way: mapping_engine.evaluate_combine() was
#   returning 0.0 (not None) when no input had live data yet, snapping
#   mapped outputs to zero instead of preserving DEFAULTS/last-known-good at
#   startup; and DashboardWindow originally stored the shared state model as
#   `self.state`, which shadowed CTkToplevel's inherited `.state()` window
#   method and crashed customtkinter's DPI-scaling tracker - renamed to
#   `self.state_model`.
#
# Rev 3: reverted the GUI from customtkinter back to plain tkinter/ttk, per
#   user request (2026-07-31 second review) - customtkinter's per-window DPI-
#   scaling tracker and custom canvas-based widget rendering made the app
#   noticeably slower to load than the reference apps' plain-tkinter style,
#   which is what the user actually wants back (this was the originally-
#   recommended option of the 3 offered at project start). Added gui/theme.py
#   (ported dark ttk theme + a VScrollFrame helper, since ttk has no native
#   scrollable-frame widget) and rewrote gui/app.py, gui/panels.py,
#   gui/dashboard.py, gui/info_popup.py on top of it. Dropped the
#   customtkinter dependency entirely (requirements.txt, this file). Also
#   renamed `self.state`/`self.state_model` consistently across all new
#   ttk.Frame/ttk widget subclasses (not just Toplevels) - ttk.Widget itself
#   defines a real `.state()` method (widget state flags), the same class of
#   name collision fixed in rev 2 for CTkToplevel, so this was made a hard
#   rule going forward rather than a one-off fix.
#
# Rev 4: fixed a real bug from rev 3's conversion - only the right (Leaf)
#   panel and the log were visible after launch; the left and middle panes
#   were collapsed to ~0 width. Root cause: gui/app.py used ttk.PanedWindow
#   with sash positions set via a self.after(150, ...)-delayed sashpos()
#   call computed from self.paned.winfo_width() - ttk.PanedWindow has no
#   per-pane minsize, so when that width read too small/stale, both sashes
#   landed near 0 and squeezed the first two panes away. Replaced with a
#   classic tk.PanedWindow (which supports minsize/width/stretch per pane
#   directly at .add() time), so every pane gets a real minimum width with
#   no timing dependency at all - gui/app.py. Verified programmatically:
#   all 3 panes now measure >100px wide immediately after construction.
#
# Rev 5: two more user-review fixes -
#   (1) Dashboard now uses a two-column layout: the long per-signal bar-
#       gauge list stays in a scrollable left column; Flags/Generated
#       Signals/Battery Management status moved out of the bottom of that
#       same list into a fixed-width (340px) right column, separated by a
#       ttk.Separator - previously all four sections were stacked in one
#       long scrolling column. Verified: left column now measures ~1126px,
#       right column exactly 340px, both fit inside the 1500px window with
#       no horizontal overflow - gui/dashboard.py.
#   (2) Added contextual '?' help buttons throughout the app, matching
#       leaf_hvbat_emulator.py's pervasive _help_btn/_show_help pattern -
#       previously only the Signal Mapping tab's per-tie conversion-math
#       popup existed. Added to: both Connections panels (RZ450e combined-
#       bus explanation, Leaf bus explanation), the Vehicle panel (car/
#       battery generation -> active CAN ID gating), the Signal Mapping tab
#       (combine functions, source-ID-prefixed dropdowns), every individual
#       Battery Management feature (what it does, its thresholds, soft vs.
#       hard cut tier - one help text per feature, sourced from docs/05),
#       the Generated Signals tab, and the Dashboard window itself -
#       gui/panels.py (help text constants + wiring), gui/dashboard.py,
#       gui/info_popup.py (new help_btn() helper), gui/app.py.
#
# Rev 6: fixed a real gap found by the user asking "does it wait for
#   connections/data to start?" - it didn't. RealtimeEngine.start() (called
#   at app launch) began the staged startup sequence immediately, timed from
#   process-start rather than real Leaf bus activity, and would happily
#   "transmit" (harmlessly, since send() just fails if disconnected)
#   regardless of connection state. Root-caused a second bug in the same
#   investigation: SharedState.counter_stale_age() fell back to session-start
#   for a never-seen counter, so the staleness watchdog would falsely soft-
#   then-hard-cut after 60-65 real seconds even with ZERO hardware ever
#   connected - verified directly (rewound timestamps 70s with nothing
#   connected -> watchdog fired a false soft cut before the fix, stayed 'ok'
#   after).
#
#   Fix: added a manual Start Bridge / Stop Bridge control (per user
#   request) so RZ450e monitoring and mapping/threshold edits work
#   immediately at launch (both are already real-time - no "apply" step),
#   while actual Leaf-bus transmission stays gated until the user presses
#   Start. ShutdownSequencer now has two new phases: 'idle' (default at
#   launch/after Stop - nothing evaluated or sent) and 'waiting_for_wake'
#   (armed by Start Bridge, still sends nothing until the Leaf connection
#   sees real incoming traffic - matching docs/07's actual real-hardware
#   trigger condition, "wait for CAN traffic on the bus," rather than "the
#   app process started"). The post-wind-down re-arm now returns to
#   'waiting_for_wake' too, instead of blindly restarting the staged
#   sequence on a timer - bridge/realtime_engine.py. counter_stale_age() now
#   returns None (not a session-start fallback) for a never-seen counter,
#   and the watchdog excludes None ages, consistent with how the fast
#   raw-CAN signal ages were already handled - bridge/state.py,
#   bridge/management_engine.py. Added the Start Bridge/Stop Bridge buttons
#   + a live phase-status label + a "?" explaining the behavior -
#   gui/app.py, gui/panels.py. Verified the full idle -> waiting_for_wake ->
#   startup -> idle lifecycle end-to-end, including that leaf_tx correctly
#   shows zero keys until real wake and clears again on Stop.
#
# Rev 7: user asked whether regen braking power was protected against
#   overcharging the pack while driving. Investigation found charge_limit_kw
#   (0x1DC) is documented as the Leaf's shared "Charge/regen power limit" -
#   the per-cell voltage taper already covered regen, since it always applies
#   to charge_limit_kw regardless of context. But the same investigation
#   surfaced a real SAFETY BUG in the same feature: the SoC-target-reached
#   logic set full_charge_flag (instant HV contactor drop per docs/03) purely
#   from SoC >= target, with no check on whether the car was actually
#   plugged in - so simply driving above the target SoC (e.g. having charged
#   to 85% overnight, then driving) would have dropped main contactors
#   mid-drive. Fixed: charger_limit_kw and full_charge_flag now only fire
#   while RZ450e's charge_permission_input (the confirmed charging interlock)
#   is active; charge_limit_kw's per-cell taper stays always-active
#   regardless, since that's the regen protection. Also cleaned up a stale
#   duplicate code block left over from the rev-5 taper edit that was
#   silently re-asserting full_charge_flag unconditionally after the new
#   gated logic - bridge/management_engine.py. Verified both scenarios
#   directly: driving at 85% SoC with an imbalanced cell taper correctly
#   engages with full_charge_flag staying 0; actively charging at 85% (above
#   an 80% target) correctly still sets full_charge_flag=1.

# Rev 8: user flagged that the real Leaf VCM is slow to respond to a
#   charge_limit_kw change, so the regen/charge-acceptance taper needs to be
#   proactive - starting well before the danger zone, not at a narrow margin
#   right on the ceiling. Replaced the previous max_cell_v (4.20V) +
#   taper_voltage_margin (0.05V) formulation with the user's exact specified
#   curve: full regen/charge at/below 3.90V/cell, linearly down to zero at/
#   above 4.10V/cell - bridge/management_engine.py (new regen_full_v/
#   regen_zero_v config fields, replacing max_cell_v/taper_voltage_margin).
#   Emergency hard-cut tier (4.30V) unchanged, now understood as "above the
#   zero-point" rather than "above the ceiling." Wired into the Battery
#   Management tab (gui/panels.py) with updated help text explaining the
#   proactive rationale. Verified the exact curve numerically: 100/75/50/25/0%
#   of max charge_limit_kw at 3.90/3.95/4.00/4.05/4.10V respectively.

# Rev 9: user asked whether discharge power (driving) got the same
#   proactive treatment as the charge/regen taper. It didn't - the discharge
#   side ("low_soc_discharge_derate") was still SoC-driven (ramp 15%->10%),
#   the same imprecise-gating problem already rejected for charge/regen.
#   Renamed to discharge_power_taper and reworked to be per-cell-voltage-
#   driven: full discharge power at/above 3.50V/cell, zero at/below 3.00V/
#   cell (matching low_voltage_cutoff's soft-cut floor exactly, so power
#   reaches zero right as capacity_empty engages). Also added, per user
#   request, fast-attack/slow-release hysteresis: the applied power limit
#   snaps down immediately on a voltage dip but only climbs back up at a
#   rate-limited pace (default 3.0s to fully recover) once voltage
#   recovers - avoids power hunting if voltage bounces near the threshold
#   under intermittent acceleration. This is the first management feature
#   that carries real state between ticks (ManagementEngine
#   ._discharge_factor_applied / ._last_apply_time), not just a stateless
#   voltage->factor formula - bridge/management_engine.py. Wired into the
#   Battery Management tab with updated help text - gui/panels.py. Verified
#   both the curve (100/80/50/20/0% at 3.60/3.40/3.25/3.10/3.00V) and the
#   hysteresis directly (instant snap-down on a dip to 3.00V, gradual
#   recovery over the configured ramp time after recovering to 3.60V, not
#   instant).

# Rev 10: user asked whether every critical cutoff was using live per-cell
#   voltage, not SoC, with SoC only as a backup check. Audited every
#   safety-relevant feature: discharge_power_taper and charge_target_taper's
#   overvoltage/regen protection were already 100% per-cell-voltage-driven,
#   but low_voltage_cutoff had a real bug - its soft-cut condition OR'd SoC
#   in as an equal, independent trigger, meaning a low SoC reading ALONE
#   could assert capacity_empty even with every cell perfectly healthy.
#   Fixed: per-cell voltage (min_cell_v / emergency_low_v) is now the sole
#   authoritative trigger; min_soc_pct is evaluated every tick and surfaced
#   in the status text (confirms agreement, warns on disagreement - e.g. a
#   possible SoC calibration issue) but can never fire the cutoff by itself
#   - bridge/management_engine.py. Verified all 4 combinations directly:
#   healthy cells + low SoC -> no cutoff (bug reproduced then fixed); low
#   cells + healthy SoC -> cutoff fires; both low -> cutoff fires with
#   "agrees" note; both healthy -> ok. Documented this as a general
#   cross-cutting design rule (not just this one feature) in docs/05's
#   Design philosophy section, and noted the one legitimate exception: the
#   AC-charge daily/extended target is a SoC-based user preference ("charge
#   to 80%"), not an overvoltage safety cutoff, so SoC is the correct metric
#   there by definition. gui/panels.py help text and field labels updated to
#   match.
#
# Rev 11: DOCS-ONLY research pass, no code behavior changed (user-directed:
#   research NMC/BMS design fundamentals first, report before touching code).
#   New docs/12-nmc-bms-design-research.md: researched + cited NMC voltage
#   windows (overcharge aging, over-discharge copper-dissolution/ISC chain),
#   temperature behavior (lithium plating below ~10C, thermal-runaway stage
#   onset temps, dT/dt early warning), charge/discharge C-rate limits, cell
#   imbalance thresholds, and standard layered BMS protection architecture -
#   then audited our design against it. Alignments confirmed (per-cell
#   authority, soft/hard tiers, taper windows, temp defaults, 80% daily
#   target all within researched ranges). Findings logged for user decision,
#   NOT yet fixed: F1 cold-charge block tests temp_max instead of temp_min
#   (management_engine.py:239 - charging into a partly-frozen pack allowed
#   until the HOTTEST probe hits 0C; bug-level, one-line fix when approved);
#   F2 no overcurrent feature (0x023 available but saturates +/-204.7A);
#   F3 no cold-side charge/regen derate curve below ~10C (plating vector is
#   regen, not the 0.09C AC charger); F4 no cell-spread (delta-V) monitor;
#   F5 low-voltage soft cut has no persistence qualification vs sag
#   transients; F6 emergency over-temp hard-cut has no documented default
#   (research suggests ~65C); F7 alignment note on 80% target. Also added
#   doc 12 to the reading-order index in docs/01-project-goals.md.
#
# Rev 12: implemented docs/12's findings F1, F3, F4, F5, F6 (user asked to
#   implement everything the research pass found, following review of the
#   Rev 11 report) - bridge/management_engine.py, gui/panels.py:
#   (F1) BUG FIX - over_temperature_derate's cold-side charge block/derate
#       now key on temp_min (coldest probe), not temp_max. Previously a pack
#       with a frozen coldest cell but a warm hottest cell would still accept
#       charge/regen - backwards, since plating happens in the coldest
#       cells. Hot-side logic unchanged (temp_max).
#   (F3) Added charge_derate_low_start_f (10C/50F default): charge/regen
#       acceptance now ramps from 0% at the existing 0C block up to 100% by
#       10C, instead of a single on/off snap at freezing - our real exposure
#       is regen into a cold-soaked pack (~0.5C), not the 0.09C AC charger.
#   (F4) New cell_imbalance_monitor feature (monitor-only, never cuts/
#       derates): warns once the spread between the worst-high and worst-low
#       of all 96 cells reaches warn_delta_v (50mV default) - a cell resting
#       30-50mV below its neighbors is a documented early signature of a
#       developing internal defect. Raised an open question (docs/10 #7):
#       does the RZ450e's own internal cell-supervision hardware still
#       balance cells in our configuration.
#   (F5) Added soft_cut_persistence_s (2.0s default) to low_voltage_cutoff:
#       the min-cell soft-cut condition must now hold continuously for this
#       long before capacity_empty latches, guarding against a single-tick
#       sag transient under a spike load. Emergency tier (2.80V) stays
#       instantaneous, unchanged.
#   (F6) Split over_temperature_derate's hard-stop values from the actual
#       hard-cut trigger - discharge_hard_stop_f/charge_hard_stop_f used to
#       BOTH cap the soft ramp AND assert relay_cut_request directly, which
#       contradicted this feature's own "Soft (ramp)" documentation. Both are
#       now pure soft ramp-to-zero points; new emergency_temp_f (65C/149F
#       default) is the real hard-cut tier, deliberately close above the
#       140F soft stop since self-heating onset for a cell with plated
#       lithium can begin as low as ~60C.
#   (F2) Implemented overcurrent_monitor as MONITOR-ONLY (deliberate scope
#       choice, not a partial implementation) - no cell datasheet exists for
#       a real continuous/peak current limit, and 0x023 saturates at
#       +/-204.7A (below the Leaf's plausible ~230-315A peak drive current),
#       so an active cutoff couldn't measure a real fault current's true
#       magnitude and a guessed threshold risked nuisance-tripping ordinary
#       acceleration. Warns (status text only) after sustained current above
#       150A discharge / 30A charge-regen (derived from this project's own
#       confirmed sensor/charger specs, not invented) - flagged as
#       provisional pending real drive-cycle logging (docs/10 #8).
#   New tests/test_management_engine.py (16 checks, all passing) verifies
#   every fix directly - first tests/ folder in this project, matching both
#   reference projects' pattern of committing verification scripts. Updated
#   docs/05, docs/10 (#7, #8), docs/11 (per-feature verification rows), and
#   docs/12 (marked F1/F3/F4/F5/F6 implemented, F2 implemented-as-monitor).
#   NOT implemented - flagged as new finding F8 in docs/12, needs a decision
#   before building: hard cuts (relay_cut_request) don't latch - they
#   self-clear the instant the triggering reading recovers, which is a real
#   gap against this project's own researched "faults should latch until
#   manually cleared" standard-practice note. Bigger change than the others
#   (needs a fault-acknowledge GUI action and a decision on which tiers
#   latch), deliberately not bundled into this pass.
#
# Rev 13: user follow-up on Rev 12/docs/12 - "add a fault history/reset
#   section", "are all BMS functions configurable", "verify data conversion
#   stays in bounds", plus a real-hardware correction on current-sensor vs.
#   real battery capability. Four separate pieces of work:
#   (1) NEW bridge/fault_log.py (FaultLog class + FAULT_DEFINITIONS catalog):
#       records every soft/hard cut and monitor-warning trigger/clear
#       transition (count, first/last triggered, last cleared, live active
#       state) - kept SEPARATE from the underlying cut's own auto-clear
#       behavior (unchanged, per user instruction: real Leaf's own reset is
#       a physical power-cycle, and that stays the default here too; fault
#       LATCHING, docs/12's F8, remains a deliberate non-change). Wired into
#       every trigger point in bridge/management_engine.py's apply()
#       (low_voltage_cutoff x2, charge_target_taper's overvoltage,
#       over_temperature_derate x4, cell_imbalance_monitor,
#       overcurrent_monitor x2, staleness_watchdog x2 = 12 tracked
#       conditions) plus output-clamp events (below). New GUI
#       FaultHistoryPanel (gui/panels.py) - one row per condition, live
#       active/cleared status, count, timestamps, per-row "Reset" button
#       (acknowledges/clears the record only - does NOT and cannot force a
#       still-true condition false, verified directly in
#       tests/test_fault_log.py and tests/test_management_engine.py) plus a
#       "Reset all". Persisted to its own config/fault_log.json (autosaved
#       every 5s, loaded at startup) so history survives app restarts, not
#       just a bridge power-cycle within one session - config_profile.py,
#       gui/app.py.
#   (2) Layout: log panel is no longer full width (user request) - gui/
#       app.py's _build_log_panel now uses a horizontal tk.PanedWindow with
#       the log on the left and the new Fault History panel on the right,
#       same draggable-sash pattern as the three main columns.
#   (3) BUG FIX - output range clamping. Confirmed a real bug (tests/
#       test_output_clamping.py): every Leaf frame builder bitmask-packs
#       values (e.g. `& 0x3FF`), which WRAPS an out-of-range value instead
#       of saturating it - a negative discharge_limit_kw (e.g. from an
#       arithmetic edge case) wrapped to a raw value that decoded back to
#       251.0kW, essentially FULL POWER, the opposite of the safety intent.
#       Also confirmed realistic (non-buggy) values can legitimately exceed
#       a documented range - a 200Ah/400V pack's derived qc_full_wh
#       computes 80000 against a documented max of 51100. Fixed: new
#       leaf_signals.RANGES + clamp_state() clamps every field to its
#       documented (lo,hi) before any frame is built - the single choke
#       point, wired into realtime_engine.py's _compose_leaf_state(). Any
#       actual clamp event is logged (fault_log key prefix `clamp_`, plus a
#       one-time Log-panel message) rather than silently absorbed, since
#       needing to clamp means something upstream is producing an
#       out-of-spec number. Confirms the answer to "do we stay within our
#       documented output limits" is now yes, guaranteed - it previously
#       was not.
#   (4) Confirmed EVERY battery-management config field is already exposed/
#       editable in the GUI (audited default_config() against gui/panels.py's
#       FEATURE_FIELDS - zero gaps) - documented this audit in docs/08 rather
#       than needing new code, since nothing was actually hidden.
#   (5) REAL-HARDWARE CORRECTION (user, 2026-07-31): the 0x023 current
#       sensor's +/-204.7A saturation ceiling is a CAN-signal encoding
#       limit, NOT a battery limit, and this bench setup's own ~200A test
#       history is a bench-setup limitation, not a pack limitation. The
#       real pack is rated for a 500A in-line discharge fuse, 150kW DC fast
#       charging (~430A, NOT currently in this bridge's scope - new open
#       question docs/10 #9), and the factory RZ450E's 230kW peak output
#       (~660A short bursts) - all far beyond what this project's only fast
#       current sensor can even represent. Corrected throughout docs/02,
#       docs/05, docs/10, docs/12 and bridge/management_engine.py's
#       overcurrent_monitor comments/status text - this monitor was already
#       correctly scoped as provisional/monitor-only (Rev 12), but the
#       reasoning why is now accurate rather than understated.
#   New tests/test_fault_log.py (14 checks) and tests/test_output_clamping.py
#   (11 checks) - all 3 test files, 51 checks total, passing. Full App()
#   smoke-tested end to end (hardware-free demo mode) with the new layout.
#
# Rev 14: user follow-up - "move the fault data to the dashboard window, on
#   the bottom right... no scrolling required", plus "do we need to update
#   any docs with this last round of changes". Relocated Fault History:
#   - REMOVED the standalone FaultHistoryPanel from gui/panels.py and the
#     bottom-of-main-window paned row added in Rev 13 (gui/app.py) - Log
#     panel is back to full width, matching how it looked before Rev 13.
#   - ADDED a compact Fault History section directly into gui/dashboard.py's
#     right column, below Battery Management status - one line per tracked
#     condition (12 fixed entries from bridge/fault_log.py's
#     FAULT_DEFINITIONS, plus dynamic rows for any output-clamp event that's
#     actually occurred): a color-coded description, a compact count ('--'
#     never happened, dim 'Nx' cleared, bright colored 'Nx' active), and a
#     per-row Reset button, plus a section-level "Reset all". Kept
#     deliberately terse (no per-row timestamps/detail text, unlike the
#     removed standalone panel) specifically so the whole section fits
#     without its own scrollbar - the live detail text for *why* something
#     tripped is already one section up, in Battery Management status.
#   - DashboardWindow now takes a management_engine argument (gui/app.py's
#     _open_dashboard call site updated); window resized 1500x900 ->
#     1560x1300 and RIGHT_COL_W 340 -> 360 to fit the new section without
#     scrolling.
#   - Confirmed via a full App() smoke test: Dashboard opens with the fault
#     section wired to live management.fault_log data, a simulated trigger
#     updates the row's count/color, a simulated clamp event grows a new
#     dynamic row, and Reset/Reset-all still only clear the record (verified
#     against the same tests/test_fault_log.py and
#     tests/test_management_engine.py checks from Rev 13 - fault_log's own
#     behavior didn't change, only where it's displayed).
#   Docs updated to match: docs/08 (layout diagram, Log panel section, new
#   Dashboard "Fault History section" writeup), docs/06 (corrected the GUI
#   panel reference), docs/11 (new verification-checklist rows for output
#   clamping, fault-history logging, the Dashboard relocation, and the
#   config-field-audit finding - none of this had checklist rows yet).
#
# Rev 15: two more user follow-ups on the Dashboard (gui/dashboard.py):
#   (1) Fault History "lights" - replaced/augmented the color-only count
#       text with an actual small circular status-light indicator per row
#       (a tk.Canvas oval): lit solid in the fault's tier color = active
#       right now, a hollow colored ring (dark fill, colored outline) = has
#       happened before but is currently clear, plain grey = never
#       happened. The description text and trigger count stay next to it.
#       Window height reduced 1560x1300 -> 1560x1180 per user request
#       ("slightly shrink the height, width is fine"). Verified all three
#       light states directly (fill/outline colors read back from the
#       canvas) via a live App() smoke test.
#   (2) New "Startup Default" 5th column in the main per-signal list
#       (Signal | Input | Conversion | Output | Startup Default) - shows
#       leaf_signals.DEFAULTS's static startup value for that field, so the
#       user can see exactly what gets sent before any real RZ450e data
#       arrives, compared directly against the live Output column. First
#       built as a plain text value, then upgraded (same-turn user follow-
#       up: "i want a bar showing there state as well") to a full bar gauge
#       on the SAME (lo, hi) range as the Output bar - set once at build
#       time and never updated (it's a fixed reference, not live data) - so
#       the two bars are directly visually comparable, not just the
#       numbers. Verified the bar's fill fraction matches the expected
#       default/range ratio exactly (110.0/255.75 -> 73.1px/170px, both
#       ~43%) via a live App() smoke test. Updated docs/08 (5-column
#       diagram + a new explanatory paragraph) and the Dashboard's own "?"
#       help text.
#
# Rev 16: two more Dashboard follow-ups (gui/dashboard.py):
#   (1) Brief color-key legend for the fault-history lights, added directly
#       above the fault rows (right below the "Fault history" header/Reset-
#       all row) - three example lights (solid/hollow-ring/grey) with short
#       captions ("active"/"cleared"/"never") plus a one-line note that the
#       lit color itself indicates the tier (hard/soft/warn), so the light
#       meaning is visible at a glance without opening the "?" help popup.
#       New _legend_light() helper, reused by both the legend and the
#       per-row lights.
#   (2) Window height reduced again, 1560x1180 -> 1560x1060, per user
#       feedback that it was still running off the bottom of the screen by
#       roughly half an inch. Width unchanged (already confirmed fine).
#   Updated docs/08's Fault History section writeup to describe the lights/
#   legend and the full window-height history across review passes.
#
# Rev 17: two more Dashboard follow-ups (gui/dashboard.py):
#   (1) New "Output Range" column - plain static text (e.g. "0 to 255.75"),
#       the same documented (lo, hi) the Output and Startup Default bars are
#       both scaled against, placed right after the Output column since
#       it's explaining that column specifically.
#   (2) BUG FIX - header/column pixel alignment. User reported the header
#       row's labels didn't line up with their actual columns. Root cause:
#       the header used ttk.Label(width=N) in CHARACTER units while the
#       data rows mixed pixel-exact bar canvases (IN_BAR_W/OUT_BAR_W) with
#       character-width value labels - those two unit systems don't
#       correspond to the same pixel offset, so header text drifted out of
#       alignment as soon as any label rendered wider than its guessed
#       character width. Fixed with a new _fixed_cell() helper - a Frame
#       pinned to an exact PIXEL width (pack_propagate(False)) with one
#       label inside - used for every text cell in BOTH the header and the
#       data rows, built from shared width constants (SIGNAL_COL_W,
#       VAL_COL_W, CONV_COL_W, RANGE_COL_W, IN_COL_W, OUT_COL_W). Alignment
#       is now guaranteed by construction, not by guessing font metrics.
#       Verified directly by reading back live widget x-offsets: header
#       cells land at pixel 0/244/462/576/824/948, and the data rows'
#       Signal/Conversion/Output-Range cells land at the identical
#       0/462/824.
#   Window widened 1560x1060 -> 1680x1060 to fit the new column (height
#   unchanged, already right-sized per Rev 16). Updated docs/08 (column
#   diagram, both new writeups, the alignment-fix explanation, and the
#   window-size history line).
#
# Rev 18: BUG FIX - Rev 17's new _fixed_cell() helper cut off every text
#   cell on the left side of the Dashboard (user report: "everything on the
#   left side in the columns is cut off"). Root cause: _fixed_cell()
#   defaulted its Frame's height to BAR_H (14px, sized for the bar gauge
#   CANVASES only) and called pack_propagate(False), which locks BOTH width
#   AND height to the given values - a normal ttk.Label needs ~19px
#   (regular) to ~21px (bold) to render one line without clipping,
#   confirmed directly (`lbl.winfo_reqheight()`), so every text cell
#   (Signal, Input value, Conversion, Output value, Output Range, Startup
#   Default value, and every bold header label) was rendering inside a
#   container shorter than the text itself. Fixed: new CELL_H=22 constant,
#   used as _fixed_cell's height default instead of BAR_H - comfortably
#   fits both regular and bold text with margin. Bar canvases (_BarGauge)
#   are unaffected - they never went through _fixed_cell, still sized at
#   BAR_H=14 as intended.
#
# Rev 19: BUG FIX - also caught a second, unrelated bug while making Rev 18's
#   fix: an earlier edit had left a literal duplicated
#   "if __name__ == '__main__':" line with nothing between the two, which is
#   a real IndentationError - main.py would not launch at all (user report:
#   "does not seem to launch the .py"). Removed the stray duplicate.
#   Separately (user follow-up, same session): the Dashboard's Input/Output/
#   Startup-Default value labels were formatted to 2 decimal places
#   (":.2f"), which under-represents the RZ450e per-cell voltage signals'
#   real resolution (raw x 5/4096 steps by ~0.00122V - 2 decimals can't
#   distinguish adjacent raw readings). Changed all three to ":.3f" in
#   gui/dashboard.py, matching the 3-decimal precision already used by the
#   main window's "Live decoded values" monitor (input_monitor_text(),
#   gui/panels.py) and by management_engine.py's own status text - the
#   Dashboard was the one inconsistently-truncated display. Confirmed the
#   70px value-cell width still comfortably fits the longer 3-decimal
#   strings (measured up to 66px worst case) with no new clipping, and that
#   main.py now parses and launches cleanly.
#
# Rev 20: Dashboard layout follow-up (gui/dashboard.py, user report: didn't
#   realize the right column already had live status data under "Battery
#   management status" until seeing it; asked for the fault states to align
#   under the left data instead of being squeezed alongside it). Moved the
#   entire Fault History section (header/Reset-all row, color-key legend,
#   the 12 fault rows) from the right column to the BOTTOM OF THE LEFT MAIN
#   LIST, below a new horizontal ttk.Separator - same section, same widgets,
#   just re-parented from `right` to `scroll` (the left VScrollFrame's
#   inner frame). Widened each fault row's description wraplength from
#   RIGHT_COL_W-130 (~230px) to 900px now that it has the full left-column
#   width instead of the narrow 360px right column - most descriptions now
#   fit on one line. Right column is back to just Flags/Generated Signals/
#   Battery Management status, matching its original Rev-11-era layout.
#   Updated docs/08 (two-column layout description, Fault History section
#   rewrite covering all three locations it's lived in). Verified via a
#   live App() smoke test: exactly 12 fault rows build, parented under the
#   left scroll frame (not the right column), and the right column's child
#   count matches Flags+Generated+Battery-status with nothing fault-related
#   left behind.
#
# Rev 21: Fault History follow-up (gui/dashboard.py, user request: "split
#   them so some are next to the others so they fit on the single page").
#   Laid the 12 fault rows out in FAULT_COLS=2 side-by-side columns (each
#   its own Frame, gridded into self.fault_frame) instead of one long
#   single-column stack - roughly halves the vertical space the section
#   needs. Rows are now handed out round-robin across columns
#   (self._fault_col_i, incremented in _build_fault_row) so any later
#   dynamically-added output-clamp row keeps the columns balanced too, not
#   just the initial 12. Each column's description wraplength narrowed from
#   the old single-column 900px to FAULT_COL_WRAP=440px, since it now only
#   gets a fraction of the left area's width. Verified via a live App()
#   smoke test: 12 rows split evenly 6/6 across the two columns, with the
#   second column's frame sitting at x=635 (roughly half the section's
#   width, confirming they're genuinely side by side, not stacked).
#   Updated docs/08 to describe the column layout and round-robin behavior.
#
# Rev 22: implemented the two auto-sleep/shutdown gaps found auditing this
#   bridge against Leaf_BMS_Emulator's confirmed real-hardware findings
#   (user request: "check the reference code and current project and see if
#   we're missing anything for auto sleep/shutdown") - bridge/
#   realtime_engine.py, bridge/leaf_signals.py:
#   (1) BUG FIX - LB_RefusetoSleep (0x55B byte6 bits5-6) was hardcoded to
#       refuse_sleep=0 in every _build_frame() call, meaning this bridge
#       ALWAYS told the Leaf's other ECUs "ignition on," even after fully
#       winding down. Leaf_BMS_Emulator confirmed via a real capture that
#       this bit tracks ignition state directly (0 while on, flips to 1
#       within ~150ms of key-off) and forces it to 1 during their staged
#       power-down. Fixed with new ShutdownSequencer.refuse_sleep_value(now):
#       derives 0/1 from the same run-state-freshness check the ignition-off
#       detector already uses, forces 1 during winding_down. Wired into
#       _build_frame's 0x55B branch (was a hardcoded literal).
#   (2) BUG FIX - post-shutdown re-arm used a flat PWRDOWN_DEFAULT_COOLDOWN_S
#       timer since entering 'stopped', not an actual bus-quiet check -
#       exactly the bug Leaf_BMS_Emulator hit in their own rev 20 and fixed
#       in rev 21 after a real capture showed a still-transmitting VCM
#       instantly re-triggering the wake detector the moment their emulator
#       re-armed on a fixed timer (zero RX gaps >100ms across two power-down
#       button presses). Fixed: ShutdownSequencer now tracks last_leaf_rx_t
#       (updated on EVERY Leaf-bus frame, not just ignition/charge IDs) and
#       tick()'s 'stopped' branch requires now - last_leaf_rx_t >=
#       PWRDOWN_DEFAULT_COOLDOWN_S before re-arming, re-checked every tick -
#       correctly waits indefinitely if the bus never truly quiets. Removed
#       leaf_signals.py's BUS_QUIET_TIMEOUT_S (was defined, never used) -
#       traced it to a SEPARATE reference-project concept (detecting when
#       their SOURCE battery goes quiet mid-session) that this project
#       already covers differently (the staleness watchdog), not something
#       that needed wiring in as originally suspected.
#   New tests/test_shutdown_sequencer.py (9 checks) - directly reproduces
#   the "still-transmitting VCM" scenario the re-arm fix protects against
#   (confirms no premature re-arm while simulated traffic continues, correct
#   re-arm once genuinely quiet) and all four refuse_sleep_value() states.
#   Updated docs/07 (both fixes, with citations), docs/03 (LB_RefusetoSleep
#   note), docs/11 (two new verification-checklist rows - software-confirmed,
#   both still need a real-VCM hardware test to confirm the actual sleep/
#   re-arm behavior on a real car). All 4 test files (58 checks) pass; full
#   App() launch confirmed clean via `py main.py`.
#
# Rev 23: main window layout fix (gui/app.py, user request - shared a
#   screenshot showing the app window and asked to fix the window placement,
#   both the on-screen position and the internal pane proportions):
#   (1) Main window now opens at a FIXED position, not just a fixed size -
#       geometry('1680x980+40+30') instead of '1680x980' with no x/y, which
#       previously left placement entirely to the OS.
#   (2) The three-pane layout's left and right panes (RZ450e battery / Leaf
#       emulator) now hold a fixed 440px starting width (stretch='never')
#       instead of all three panes being stretch='always' - the old all-
#       stretch setup let Tk's PanedWindow distribute extra window width
#       unevenly (observed: left pane rendering noticeably wider than
#       configured while the right pane stayed squeezed near its minimum).
#       Only the middle (Configurator) pane still stretches, sized so
#       440+800+440=1680 matches the window's own fixed width exactly -
#       nothing squeezed on first render. Still fully user-draggable via the
#       sashes afterward; this only fixes the STARTING layout.
#   Verified via a live App() smoke test: geometry reports exactly
#   "1680x980+40+30", and the paned window's sash coordinates confirm the
#   panes render at left=440px, middle=790px, right=446px (vs. the uneven
#   split visible in the user's screenshot beforehand). Updated docs/08.
#
# Rev 24: two more Dashboard follow-ups in the same turn (gui/dashboard.py,
#   gui/app.py):
#   (1) Scaled every pixel dimension in the Dashboard ~15% smaller (user
#       request: "make it 15% smaller in total... so it all fits on the
#       screen without maximizing") - bar widths, column widths, cell
#       heights, fault-light diameter, right-column width, all derived from
#       their previous values with the prior value noted in a comment.
#       Window 1680x1060 -> 1430x950. Introduced explicit fonts for the
#       first time (DASH_FONT 8pt, DASH_FONT_BOLD 8pt bold, DASH_ACCENT_FONT
#       9pt bold, DASH_HEADER_FONT 12pt bold) since most labels previously
#       had no font set at all (inheriting the shared ~9pt theme default) -
#       applied via `_fixed_cell`'s new font= default plus explicit font=
#       on every other label; DASH_ACCENT_FONT/DASH_HEADER_FONT override
#       just the SIZE of the shared Accent.TLabel/Header.TLabel styles for
#       this window only (color unaffected, global theme/main app
#       untouched). Re-verified CELL_H (22->19px) against the new font's
#       actual reqheight (17px) via winfo_reqheight() before finalizing, to
#       avoid reintroducing the Rev 18 clipping bug at the new smaller size.
#   (2) New Log panel at the bottom of the Dashboard (user request: "a
#       redundant screen that the log has in the main screen") - mirrors the
#       main window's log rather than duplicating the connection. App
#       (gui/app.py) now keeps a capped collections.deque(maxlen=2000) of
#       every formatted log line alongside its existing queue.Queue-fed log
#       box; DashboardWindow reads from that deque (self.master.log_lines)
#       and syncs only new lines per tick, showing full existing history
#       immediately on open (not just future lines).
#   Verified via a live App() smoke test: geometry reports "1430x950",
#   opening the Dashboard mid-session shows both a message logged BEFORE it
#   opened and one logged AFTER, and a sampled value-cell's actual rendered
#   height (19px) comfortably exceeds its label's reqheight (17px) - no
#   clipping. All 4 test files (58 checks) still pass; full App() launch
#   confirmed clean. Updated docs/08.
#
# Rev 25: three more layout follow-ups in the same turn, user request ("I
#   like the size we made the dashboard" + "scale down the main app as well
#   to fit the same sizing for font and everything" + "let's move the fault
#   history to its own window... make it tall and narrow"):
#   (1) Fault History moved OUT of the Dashboard into its own window - new
#       gui/fault_history_window.py (FaultHistoryWindow), a tk.Toplevel
#       sized 420x900 (tall/narrow), positioned just to the right of
#       whichever window opened it (computed from that window's live
#       winfo_x()/winfo_width(), not a hardcoded screen position). Single
#       column top-to-bottom (not the Dashboard's old 2-column grid - no
#       room for that in a narrow window), stacked two-line-per-entry rows
#       (light+description on top, count+Reset below, vs. the Dashboard's
#       old single-line-per-row - gives descriptions room to wrap). Same
#       underlying data/behavior as before (FAULT_DEFINITIONS, light
#       states, Reset semantics) - just relocated and re-laid-out.
#       gui/dashboard.py: removed the entire Fault History section (~90
#       lines - _build_fault_row, _legend_light, _reset_fault,
#       _reset_all_faults, FAULT_COLS/FAULT_COL_WRAP, the fault_labels
#       tracking in _tick()) and added a "Fault History" button that opens
#       the new window. gui/app.py: added a matching "Fault History" button
#       in the Configurator toolbar; both buttons delegate to ONE shared
#       window reference (DashboardWindow's button calls
#       self.master._open_fault_history()) so opening from either place
#       focuses the same instance instead of creating two independent ones
#       - confirmed directly via a smoke test that this was actually
#       happening before the fix, and that it wasn't after.
#   (2) Main app scaled ~15% smaller to match the Dashboard - gui/app.py:
#       window 1680x980 -> 1430x835 (position offset unchanged), pane
#       widths 440/800/440 -> 374/680/374, section headers 16pt -> 14pt.
#       Unlike the Dashboard (which had almost no explicit fonts to begin
#       with, so it introduced local DASH_FONT-style overrides), this pass
#       shrinks the SHARED ttk theme itself (gui/theme.py's apply_style()):
#       base '.' font -> 8pt (was unset, inheriting Tk's system default),
#       Header.TLabel 14pt->12pt, Accent.TLabel 10pt->9pt, plus
#       proportionally smaller button/tab padding. These three sizes
#       already matched what the Dashboard was independently using as
#       local overrides, so this doesn't double-shrink the Dashboard (an
#       explicit widget-level font= always wins over a style default) -
#       it just gives the main app (which had almost no explicit fonts)
#       the same baseline for free, including every character-width
#       width=N field in gui/panels.py, without touching that file at all.
#   Verified via a live App() smoke test: main window geometry "1430x835",
#   pane sash positions land at ~374/672/380 (vs. the old uneven-but-fixed
#   374/680/374 target - Combobox/font rendering rounds slightly, still
#   comfortably matching), Dashboard geometry unchanged at "1430x950" (not
#   touched, per "I like the size we made the dashboard"), Fault History
#   opens with 12 rows and the same window whether opened from the main app
#   or the Dashboard. All 4 test files (58 checks) pass; full App() launch
#   confirmed clean. Updated docs/08 extensively (layout diagram, pane
#   sizing, main-app scaling writeup, and a full rewrite of the Fault
#   History section describing its new standalone window and full move
#   history across all three locations it's lived in).
#
# Rev 26: BUG FIX - user report: "did we change the font size as well? it
#   dose not fit in the smaller window as it did." Root cause: Rev 25's ttk
#   theme shrink (gui/theme.py's apply_style()) only affects ttk widgets -
#   plain tk.Text and scrolledtext.ScrolledText widgets do NOT participate
#   in ttk styling at all and were never given an explicit font, so they
#   silently kept rendering at Tk's system default (~9pt) while the panes
#   around them got narrower. This hit exactly the densest content in the
#   app: LiveMonitorPanel's "Live decoded values"/"Live transmitted values"
#   boxes (gui/panels.py) and the main window's own Log panel (gui/app.py) -
#   confirmed directly via winfo_reqheight()/cget('font') that these were
#   still 9pt-equivalent after the "15% smaller" pass. Fixed: new shared
#   BASE_FONT = ('Segoe UI', 8) constant in gui/theme.py (apply_style()'s
#   own '.' style now references it too, replacing a locally-hardcoded
#   tuple), passed explicitly as font=BASE_FONT to LiveMonitorPanel's box,
#   the main window's log box, and gui/info_popup.py's "?" help popup text
#   (same gap, fixed for consistency). The Dashboard's own log box was
#   already correct (got an explicit font when it was added in Rev 24).
#   Verified directly: all 3 tk.Text widgets in the main app now report
#   font "{Segoe UI} 8", matching every ttk widget around them. All 4 test
#   files (58 checks) still pass; full App() launch confirmed clean.
#   Updated docs/08.
#
# Rev 27: RESOLVED docs/10-open-questions.md item 10 - user confirmed on
#   real hardware (their own Leaf + this project's own bench RZ450e pack,
#   NOT a value carried over from the Leaf project, which left this exact
#   question open): the dash SOC% source field soc_correction (0x59E byte
#   7) is a plain linear formula, raw = soc_pct x 2.0 (raw 0-200 = 0-100%).
#   - bridge/mapping_engine.py: added a shipped default tie (soc_pct ->
#     soc_correction, scale=2.0, offset=0.0) to default_ties().
#   - bridge/leaf_signals.py: corrected soc_correction's slider range/
#     default from an unconfirmed 0-255/241 placeholder (241 raw would be
#     an impossible 120.5% under the now-confirmed formula) to the
#     confirmed 0-200/90 - 90 matches usable_soc/fine_soc_pct's own ~45%
#     default for consistency across the SoC-family fields.
#   - New tests/test_mapping_engine.py (12 checks, first test for this
#     module) - confirms the default tie's presence/parameters, the
#     end-to-end computed value at several SoC points (0/45/82/100% ->
#     0/90/164/200 raw), and that leaf_signals.py's range/default actually
#     changed.
#   Updated docs/10 (resolved item 10 in place, not renumbered - other docs
#   reference it by number), docs/04 (mismatch #2, the confirmed formula),
#   docs/03 (added a previously-missing soc_correction row to the mapping-
#   targets table), docs/11 (new Confirmed-tier row - the first fully
#   real-hardware-confirmed *mapping* in this project, as opposed to a
#   management-feature threshold). User's own reference profile:
#   config/7-31-2026-new-SOC-dash-fix.json (gitignored, not modified).
#   All 5 test files (70 checks) pass; full App() launch confirmed clean.
#
# Rev 28: user confirmed a second mapping on real hardware in the same
#   session (own Leaf + own bench RZ450e pack): ChargeBars/CapacityBars raw
#   (0x5BC) against their real dash SOH/capacity bar display, working out
#   the scaling themselves for a 0-200Ah input -> 0-14 raw output range
#   (raw = capacity_pack1_ah x 0.07) - asked to have it added as a default
#   mapping same as the SOC fix.
#   - bridge/mapping_engine.py: added the shipped default tie
#     (capacity_pack1_ah -> capacity_bars_raw, scale=0.07, offset=0.0) to
#     default_ties(). leaf_signals.py's existing 0-15 range/6 default were
#     already consistent with this formula - no correction needed there,
#     unlike soc_correction's previous invalid 241 default.
#   - tests/test_mapping_engine.py: added a second pair of checks (tie
#     presence/parameters + end-to-end computed values at 0/100/200Ah ->
#     0/7/14 raw), same pattern as the soc_correction tests.
#   Updated docs/03 (the ChargeBars/CapacityBars row), docs/04 (replaced the
#   old "might need a lookup table" speculation with the confirmed plain-
#   linear formula), docs/11 (new Confirmed-tier row). User's own reference
#   profile: config/7-31-2026-new-SOC-dash-fix-added-SOH-for_helth-bars.json
#   (gitignored, not modified). All 5 test files (78 checks) pass; full
#   App() launch confirmed clean.
#
# Rev 29: implemented the charger-request ramp feature, user-requested after
#   asking whether it was ported yet ("are we implementing the charge
#   detector? the ramp and setting the KW correctly when the charger is
#   plugged in? that worked on the battery emulator app") - it wasn't; ported
#   from Refrance/Leaf_BMS_Emulator (real-hardware-confirmed via bit-level
#   diff of every HVBAT ID, idle vs. real charge-session captures).
#   - bridge/leaf_signals.py: added CHG_IDLE_RAW/CHG_RAMP_START_RAW/
#     CHG_RAMP_RAW_PER_S constants (the confirmed 0.0kW start, 2.0kW/s-at-
#     uprate-7 rate).
#   - bridge/state.py: new SharedState.charge_emulation dict (charge_emulate
#     checkbox, charge_target_kw, chg_uprate_level) - user-adjustable
#     emulation controls, seeded from leaf_signals.py, same pattern as the
#     existing `vehicle` dict.
#   - bridge/realtime_engine.py: ShutdownSequencer.charge_active() made public
#     (was an inline local) so the new RealtimeEngine._apply_charge_ramp()
#     can reuse the same 0x1F2 decode instead of duplicating it. The ramp
#     runs once per tick in _compose_leaf_state(), BEFORE the management
#     engine's per-cell overvoltage taper, so that safety feature always gets
#     the final say and is never bypassed. _build_frame's 0x1DC branch now
#     sources its uprate bits from the ramp's own computed state instead of
#     reading the static slider directly whenever the checkbox was on
#     (confirmed bug: idle frames must always carry uprate 0, not the
#     configured level, regardless of checkbox state).
#   - DUAL-TRIGGER REQUIREMENT (user directive, mid-implementation: "i only
#     want to trigger the charge ramp... if both triggers are set. else the
#     charger should stop"): the ramp requires BOTH a real Leaf 0x1F2 charge
#     request AND RZ450e's own charge_permission_input interlock (0x358) -
#     the first implementation only checked the Leaf-side signal, a real gap
#     once called out. If the Leaf wants to charge but RZ450e hasn't
#     authorized it, forces full_charge_flag=1 (the confirmed "instant stop,
#     needs a physical replug" bit) + charge_limit_kw=0.0 +
#     charger_limit_kw=-10.0, instead of silently falling back to a static
#     value.
#   - ShutdownSequencer._should_wind_down/tick() gained a new
#     `charge_authorized` parameter (default True, so any direct unit test of
#     the class in isolation is unaffected) so that same Leaf-wants-but-not-
#     authorized mismatch is treated as "not really charging" for sleep
#     purposes too - the bridge can wind down/sleep instead of staying awake
#     indefinitely just because the Leaf keeps asking with no pack permission
#     behind it ("can sleep till replugged" - a genuine replug is what
#     produces a fresh 0x1F2 request).
#   - SAFETY FIX found while implementing (self-identified, not user-
#     reported): bridge/management_engine.py's charge_target_taper used to
#     only scale charger_limit_kw's per-cell overvoltage taper while
#     charging_active (the RZ450e 0x358 interlock) was true - fine before
#     this ramp existed, but the ramp could now raise charger_limit_kw
#     whenever the Leaf-side 0x1F2 request was active, a different signal
#     that can be out of sync with the interlock. Made the taper
#     unconditional, matching charge_limit_kw's own already-unconditional
#     treatment - never conditionally skippable regardless of which upstream
#     logic set the value.
#   - gui/panels.py: new ChargeEmulationPanel (checkbox + 2 numeric fields +
#     live status line + "?" help text covering the dual-trigger/stop-flag
#     behavior); gui/app.py: wired in as a new "Charge Emulation" tab in the
#     Configurator's Notebook.
#   - bridge/config_profile.py: charge_emulation now saves/loads with the
#     rest of the profile, same pattern as vehicle/generated_signals.
#   - tests/test_charge_ramp.py (new, 23 checks): dual-trigger gating, ramp
#     start/rate/target-cap/reset, idle-frames-carry-uprate-0, the stop-flag
#     mismatch behavior (asserted/not-asserted in each case), and the
#     charge_target_taper safety fix (both the emergency and proactive
#     tapers still apply to charger_limit_kw without the RZ450e interlock).
#     tests/test_shutdown_sequencer.py: 6 new checks for `charge_authorized`
#     (authorized keeps the bridge awake unchanged; unauthorized starts the
#     wind-down timer and eventually winds down).
#   Updated docs/03 (new field descriptions + full_charge_flag cross-ref),
#   docs/06 (new section 6: architecture, dual-trigger, mismatch, sleep
#   interaction), docs/07 (trigger 2/3 cross-reference), docs/11 (3 new
#   Documented rows, pending real-hardware confirmation - this feature is not
#   yet tested against an actual charge session on real hardware). All 6 test
#   files (107 checks) pass; full App() launch confirmed clean.
#
# Rev 30: two user follow-ups on Rev 29's charger-request ramp feature -
#   "did we update all the docs with everything we did this session?" and "we
#   need some data on charging and ramp setting etc on the dashboard."
#   (1) DOCS AUDIT found one real gap: docs/08-gui-design.md's Configurator
#       tab list/layout diagram never got the new "Charge Emulation" tab
#       added when it was built in Rev 29 - gui/panels.py and gui/app.py were
#       correct, the doc just hadn't caught up. Fixed: new "### Charge
#       Emulation tab" section, updated ASCII layout diagram, and added it to
#       the "Contextual help" list. Also cross-referenced the new feature
#       into docs/10-open-questions.md's two directly-relevant open items -
#       #1 (full_charge_flag re-arm) now notes the ramp's mismatch trigger is
#       a second place that flag gets set, same non-latching approach as the
#       existing case; #4 (charge_permission_input "not present" default)
#       now notes every code path touched by Rev 29 already fails safe to
#       "not permitted" (SharedState.get_input() returns None for a signal
#       that's never arrived), though this still isn't a written project-wide
#       policy. All other docs touched in Rev 29 (03, 06, 07, 11) were
#       already accurate on review - no further gaps found there.
#   (2) NEW Dashboard section (gui/dashboard.py) - "Charge emulation (ramp)"
#       in the right column, between Generated Signals and Battery Management
#       status: emulation ON/OFF, configured target/rate, whether the Leaf's
#       0x1F2 request is currently ACTIVE (read from RealtimeEngine.sequencer
#       via self.master.engine - this state lived only inside the sequencer,
#       never surfaced in the GUI before), whether RZ450e's own
#       charge_permission_input is GRANTED (previously only visible buried in
#       the left input list), the live charger_limit_kw value, and a plain-
#       English status line matching ChargeEmulationPanel's own wording
#       (disabled / idle / ramping-active / STOPPED-on-mismatch) so both
#       windows tell a consistent story. DASHBOARD_HELP text updated to
#       match. Verified via a live App() smoke test exercising all four
#       states directly (toggling charge_emulate, injecting a simulated
#       0x1F2 frame, and flipping charge_permission_input) - each status
#       line and field updated correctly with no exceptions.
#   Updated docs/08 (Charge Emulation tab section, Dashboard section
#   rewrite, contextual-help list), docs/10 (two open-question cross-
#   references), docs/11 (new verification row for the GUI additions). All 6
#   test files (107 checks, unchanged - this was a GUI/docs-only follow-up,
#   no bridge/ behavior changed) pass; full App() launch confirmed clean.

if __name__ == '__main__':
    app = App()
    app.mainloop()
