# REVISION: 63
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

REVISION = 64
REV_DATE = '2026-08-09'

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
#
# Rev 31: implemented the user's full item-by-item response pass over
#   docs/13-review-checklist-2026-08-01.md (the safety/redundancy review from
#   the prior two sessions) - every "Your notes:" line in that doc turned
#   into a concrete change below. Two numeric ambiguities were resolved via
#   direct question before starting: over-temp emergency hard-cut -> 61C
#   exactly (141.8F, was 149F/65C); low-voltage SoC backup -> single
#   threshold lowered to 8% (was 10%), no second tier.
#   (A) Staleness watchdog (bridge/management_engine.py, bridge/state.py) now
#       covers EVERY registered input signal (all 96 cells, all 16 temp
#       probes, every scalar - rz450e_signals.INPUT_SIGNAL_KEYS), not a
#       hand-picked subset of 5 - closes docs/13 item 1.1 and docs/10 open
#       question #2. New SharedState.ages_of() batches the freshness check
#       into one lock acquisition instead of 100+ separate age_of() calls
#       per tick. On soft cut, staleness now ALSO forces an explicit charge-
#       stop (charge_limit_kw=0.0, charger_limit_kw=-10.0), scoped to this
#       feature only - stale safety data must not just soft-cut, it must
#       stop charging outright (user directive).
#   (B) New rz450e_signals.PLAUSIBLE_RANGES/validate_inputs() (docs/13 item
#       1.3): every decoded input is checked against a generous physical-
#       plausibility range before reaching SharedState - a rejected value is
#       simply not written (so it ages under its last-good value and the now
#       comprehensive watchdog (A) catches sustained-invalid data after 60s,
#       exactly the mechanism requested). New SharedState.note_rejected_
#       input()/recent_rejections() and a fault_log entry (input_validation_
#       reject) surface it. Wired into both the raw-CAN ingest loop and the
#       DID poll loop via a new shared RealtimeEngine._ingest_validated()
#       helper.
#   (C) New cell_data_cross_check feature (management_engine.py) - the "0x020
#       pack summary is a sanity cross-check against the per-cell array"
#       behavior docs/02 and docs/04 already documented but that was never
#       actually implemented (docs/13 item 1.2): compares worst_low/
#       worst_high against cell_min/cell_max, same soft->hard escalation
#       structure as the staleness watchdog (60s/+5s, independently
#       tunable), logs a new fault_log entry (cell_data_mismatch) describing
#       the actual delta.
#   (D) bridge/can_backend.py: BusConnection gained a real lock protecting
#       _worker/_want_connected/_monitor_thread across connect()/
#       disconnect()/_auto_reconnect_loop() - fixes two real races found in
#       the review (docs/13 items 3.1/3.2): a fast disconnect-then-reconnect
#       could silently kill the auto-reconnect monitor for the rest of the
#       session, and a concurrent reconnect could silently undo an explicit
#       Disconnect click. _auto_reconnect_loop's flat 3s sleep replaced with
#       an interruptible Event.wait() so disconnect() takes effect
#       immediately instead of up to 3s late. New tx_ok/send_errors/
#       reconnect_count health counters, surfaced as a TX-OK status light +
#       reconnect/error counts on ConnectionsPanel (docs/13 item 2.1).
#       gui/app.py's _on_close() now cleanly disconnects both buses instead
#       of relying on daemon-thread teardown (docs/13 item 3.3).
#   (E) RealtimeEngine.last_tick_monotonic heartbeat (docs/13 item 2.2) - a
#       dead _tx_loop thread previously froze the phase label with no
#       warning; gui/app.py now shows "Bridge: NOT RESPONDING" if the
#       heartbeat goes stale, and gui/dashboard.py's output bars use real
#       freshness instead of a hardcoded fresh=True.
#   (F) DID poll cadence reworked (realtime_engine.py's _did_poll_loop,
#       docs/13 item 2.3): waits up to 5.0s for each DID's response then
#       moves on immediately (small 0.3s pacing gap) instead of always
#       sleeping a flat 5.0s regardless of response time - the old behavior
#       meant any one specific DID was really only re-polled every ~15s.
#   (G) ManagementPanel threshold fields now clamp to registered (lo, hi)
#       bounds with visual "invalid"/"clamped" feedback instead of silently
#       swallowing bad input (docs/13 item 4.1), mirroring
#       ChargeEmulationPanel's existing pattern. New management_engine.py
#       _check_config_sanity() cross-field ordering check (e.g. an emergency
#       tier typed less extreme than its own soft tier) protects a hand-
#       edited profile.json too, logs a config_sanity fault_log warning.
#   (H) LARGEST change: split the old combined charge_target_taper (which
#       scaled both charge_limit_kw AND charger_limit_kw off one curve) into
#       two independently-tunable features (docs/13 item 4.2, user
#       directive: regen can push ~0.5C into the pack, AC charging only
#       ~0.09C - physically too different to share one curve). charge_
#       target_taper (Battery Management tab) now drives ONLY charge_
#       limit_kw, values updated to 4.00/4.15/4.30V, and gained the same
#       fast-attack/slow-release hysteresis discharge_power_taper already
#       had (new recovery_ramp_s field). New ac_charge_taper config lives in
#       state.charge_emulation (leaf_signals.py's CHARGE_SLIDERS/
#       CHARGE_CHECKS gained ac_full_v/ac_zero_v/ac_emergency_v/ac_taper_
#       enabled/daily_target_pct/extended_target_pct/extended_mode) and
#       drives ONLY charger_limit_kw + full_charge_flag/AC-target-reached,
#       exposed on a new "AC charger overvoltage taper" section of the
#       Charge Emulation tab. charge_emulate now defaults ON (was off, user
#       directive). Every feature checkbox toggle (Battery Management AND
#       Charge Emulation) now logs to the Log panel (docs/13 item 4.2's
#       other ask).
#   (I) Hard-cut latching (docs/12 finding F8, docs/13 item 5.1, user
#       directive: "it should only reset after the car has been powered down
#       and back on OR if the charger is unplugged and replugged"). New
#       ManagementEngine._hard_latched: once a hard cut fires, relay_cut_
#       request/interlock stay asserted every tick even after the triggering
#       reading recovers, until notify_session_start() (RealtimeEngine calls
#       it on a fresh waiting_for_wake->startup transition, the closest
#       analog this bridge has to a power cycle) or notify_charge_replug()
#       (called on a fresh 0x1F2 request following none) clears it. Scoped
#       to hard cuts only - soft cuts keep auto-clearing, matching docs/12
#       §8's own researched soft/hard distinction. No manual GUI unlatch.
#   (J) gui/panels.py: mouse-wheel scrolling no longer silently changes a
#       readonly Combobox's selection (new _no_wheel() helper, applied to
#       every mapping/vehicle/channel dropdown - docs/13 item 4.3, user
#       report of accidental changes while scrolling). New provisional
#       (explicitly NOT hardware-confirmed, unlike soc_correction/
#       capacity_bars_raw) default tie for temp_segment_pct, which
#       previously had no live driver at all - new docs/10 open question
#       #13. Full output-signal coverage audit done (every leaf_signals.
#       OUTPUT_SIGNALS key checked for a live driver) - findings appended to
#       the bottom of docs/13 rather than silently fixed, per the user's
#       request to review them one at a time.
#   (K) docs/09's STM32 export example JSON regenerated directly from
#       default_config()/charge_emulation (was stale, missing several
#       fields) plus a new section on the charge_target_taper/ac_charge_
#       taper split. docs/05 and docs/10 now state the "missing interlock
#       signal fails safe to not-permitted" behavior as an explicit,
#       deliberate policy (was previously correct in code but only as an
#       emergent side effect) and clarify it's a different concern from the
#       already-completed charge-ramp dual-trigger requirement (docs/13 item
#       Part 10 #4 - the user's notes suggested these were being conflated).
#   (L) Partial shared-state locking cleanup (docs/13 item 5.4) - new locked
#       SharedState.snapshot_management_status()/set_management_status()/
#       snapshot_vehicle()/set_vehicle_item(), applied to every touch point.
#       generated_enabled/charge_emulation and ManagementEngine.config/
#       status deliberately NOT retrofitted this pass - the user's own note
#       flagged that how this should work on the future STM32 port (a
#       standalone system, not a live Python object graph) is still an open
#       architecture question; documented as a scope decision, not silently
#       dropped.
#   (M) New tests: AC charge-target-reached contactor-drop path (previously
#       zero coverage), both overvoltage-emergency fault_log entries (regen
#       and the new AC tier), manual_reset against an already-auto-cleared
#       soft entry (tests/test_management_engine.py, tests/test_fault_log.
#       py). New docs/14-validation-test-plan.md gathers every remaining
#       "needs a test"/"needs real hardware" item from docs/13 and this
#       session (boundary-value sweeps, every newly-changed threshold
#       pending real-hardware confirmation, the new features' test gaps)
#       into one working checklist instead of scattered across review notes.
#   (N) New bridge/trc_log.py - PCAN-Explorer-compatible .trc capture, ported
#       (not re-derived) from Refrance/RZ450e_battery_can_decode_Project's
#       own confirmed trc_write_header/trc_format_row. New Start Log/Stop
#       Log button on the main window (user request: "can we add a data
#       logger. must keep the .trc format.") captures every RX/TX frame on
#       both buses (rz450e -> bus 1, leaf -> bus 2) into one file - RX logged
#       from the ingest loops, TX logged from BusConnection.send()'s single
#       choke point (covers the Leaf TX loop and RZ450e DID requests alike).
#       Verified directly: a captured session's rows round-trip through
#       read_trc_rows() correctly.
#   All 6 test files (119 checks, up from 107) pass; full App() launch
#   confirmed clean after every package.
#
#   POST-IMPLEMENTATION REVIEW (same session): ran 4 parallel fresh review
#   passes specifically hunting for anything missed or newly introduced by
#   (A)-(N) above - the regen/AC-charger split, interactions between the new
#   safety checks, the new locking code, and a fresh-eyes sweep of
#   everything else. Found and fixed one real, high-severity bug plus
#   several smaller correctness/documentation gaps:
#   - **SAFETY BUG FOUND AND FIXED**: the new hard-cut latch (I) could be
#     spuriously cleared just by pressing Stop Bridge then Start Bridge,
#     with no relation to an actual car power-cycle - `notify_session_start()`
#     originally fired on EVERY `waiting_for_wake -> startup` transition,
#     but a manual Stop/Start Bridge toggle produces that exact same
#     transition without the car's VCM ever having lost power (very likely,
#     since the user never touched the ignition). This directly defeated
#     the latch's whole purpose. Fixed: new `ShutdownSequencer.
#     rearmed_naturally` flag distinguishes a NATURAL re-arm (the sequencer
#     itself completed a real wind-down - `'stopped' -> 'waiting_for_wake'`)
#     from a MANUAL one (`arm()`, i.e. Start Bridge) - only a natural re-arm
#     is close enough to "the car being powered down and back on" to clear
#     the latch. 4 new tests confirm this directly (tests/
#     test_shutdown_sequencer.py, tests/test_management_engine.py).
#   - **Fault History window would have shown "all clear" during an active
#     latched cut** - every hard-tier fault_log entry (low_voltage_
#     emergency, overvoltage_emergency, etc.) correctly still reflects its
#     OWN instantaneous trigger (by design - useful "did this specific
#     thing recur" info), but none of them reflected that the CUT ITSELF
#     stays latched after its trigger recovers. Fixed: new dedicated
#     `hard_cut_latch` fault_log entry, always live, showing whether the
#     vehicle is actually still cut off right now. `gui/fault_history_
#     window.py`'s help text corrected (previously claimed all cuts still
#     auto-clear, no longer true for hard cuts).
#   - Stale comments/docstrings pointing at the OLD combined charge_target_
#     taper for behavior that now lives in the new ac_charge_taper, found
#     across bridge/management_engine.py, bridge/realtime_engine.py,
#     docs/03, and docs/08 (which also still described "Emulate charger
#     request" as off-by-default and never mentioned the new AC-charger
#     taper section at all) - all corrected.
#   - Two lower-severity items deliberately NOT fixed this pass, documented
#     instead (appended to docs/13-review-checklist-2026-08-01.md for the
#     next iteration): `BusConnection._start_worker_locked()` holds its lock
#     across a 150ms sleep + log call (latency, not corruption - could
#     stall the GUI thread or delay a TX send by up to 150ms during a
#     reconnect); and the reconnect-race fix from this session narrowed its
#     TOCTOU window from ~3s to microseconds but didn't perfectly eliminate
#     it.
#   All 6 test files (129 checks, up from 107 pre-session / 119 before this
#   fix) pass; full App() launch confirmed clean. Findings appended to the
#   bottom of docs/13-review-checklist-2026-08-01.md per the user's explicit
#   iterate-until-nothing-is-missed loop - round 2 starts whenever the user
#   works through those.
#
# Rev 32: Fault History window layout follow-up (gui/fault_history_window.py,
#   user request: "make it ever so slightly wider so everything fits on the
#   screen cleanly by moving the counter in between the light icon and the
#   text. then the reset can be on the same row also. all one line"). Each
#   row was light+description stacked above count+Reset - now light, count,
#   description, and Reset are all packed into ONE row (`_build_fault_row`).
#   Window widened 420->460 to fit the combined row; DESC_WRAP narrowed
#   330->300 to give the count/Reset their own space in the now-shared row.
#   Active-state count text shortened from "Nx - ACTIVE" to just "Nx" (the
#   narrow inline count column, width=4 chars, has no room for the suffix -
#   the lit light + colored count together already convey "active," matching
#   the legend above). FAULT_HISTORY_HELP's "count below each description"
#   line corrected to "count next to each light." Verified via a live
#   App() smoke test: every row measures 433px of content within the 460px
#   window (light at x=5, count at x=23, Reset at x=380-428, description
#   filling x=57-374) with no overflow, and row height (29px) comfortably
#   exceeds the label's actual text height (17px) - no clipping.
#
# Rev 33: worked through docs/13-review-checklist-2026-08-01.md's Part 12
#   (round-2 findings from implementing Part 1-11) and Part 13 (a fresh
#   line-by-line pass hunting specifically for what Parts 1-12 missed),
#   item by item, per the user's iterate-until-nothing-is-missed loop:
#   (12.3) bridge/can_backend.py: split _start_worker_locked() (mutates
#       self._worker only) from a new _finish_worker_start() (the 150ms
#       connection-attempt wait + log call) so connect()/_auto_reconnect_
#       loop() no longer hold the connection lock across a sleep - was
#       stalling the GUI thread or delaying the TX loop's next send() by up
#       to 150ms during any reconnect attempt.
#   (12.5) bridge/leaf_signals.py: removed 'voltage_latch' as a mapping
#       target (build_1db() never read it - mapping anything to it had zero
#       effect on the wire, user decision: remove rather than wire in dead
#       weight). Fixed the 4 GENERATED_SIGNALS checkboxes (prun, code_1dc,
#       chg_time_5bc, hist_5c0) that were packed into their frames
#       unconditionally regardless of checkbox state - new code_1dc_
#       override/chg_time_override/hist_override params on build_1dc/
#       build_5bc/build_5c0 let bridge/realtime_engine.py's _build_frame()
#       substitute a neutral placeholder when unchecked, matching how
#       voltage_latch_toggle/heartbeat_1c2/seq_5eb already behaved.
#       main_relay_on: user confirmed (docs/13 item 12.5's notes) it only
#       has any effect during startup, so driving it static 1 is fine -
#       no code change, decision documented in docs/03.
#   (13.1/13.1a) bridge/management_engine.py, bridge/state.py: the staleness
#       watchdog previously EXCLUDED a signal that had never arrived at all
#       (by design, to avoid false-tripping with no hardware connected) -
#       meaning indefinite full power with no failsafe ever firing for that
#       specific gap, not the intended 60s grace period. Fixed: Management
#       Engine now tracks its own first-apply() timestamp; a never-arrived
#       signal now ages from that moment exactly like a signal that went
#       stale, hitting the same 60s soft / +5s hard schedule. Soft stage now
#       also forces full_charge_flag=1 (in addition to the existing
#       capacity_empty + zeroed charge_limit_kw/charger_limit_kw) - stale
#       safety data must stop an active charge outright, not just soft-cut.
#   (13.1b) bridge/leaf_signals.py, bridge/realtime_engine.py, bridge/
#       state.py: new require_live_data_to_charge (default ON) - the charge
#       ramp must not start on cached/default battery data at all, unlike
#       driving (which is allowed to run on cached values until the
#       watchdog's own schedule would object). REWORKED after user
#       clarification that this needed to be the SAME watchdog/safety model
#       as driving, just with a different startup gate, not a second custom
#       freshness timer: removed the first version's standalone timer,
#       replaced with a one-time per-session "has every per-cell voltage +
#       temp_max/temp_min gone genuinely live" check
#       (RealtimeEngine._charge_data_ready()). SESSION-BOUNDARY BUG found via
#       the user's own follow-up question ("so the 'once this session' is a
#       session from sleep to running state?") and fixed same-day: the first
#       version checked "has this signal EVER updated since the app
#       process launched" (age_of()/SharedState are deliberately app-
#       lifetime-persistent, for the general watchdog's benefit), which
#       meant one live contact early in a long-running session satisfied the
#       gate FOREVER, including across a full sleep-wake cycle with the
#       RZ450e now disconnected. Fixed with new SharedState.timestamps_of()
#       (session-scoped) compared against new ShutdownSequencer.
#       get_session_start(), instead of just checking non-None.
#   (13.2) bridge/config_profile.py: load_last_known_good() now runs the
#       cached data through the same rz450e_signals.validate_inputs()
#       plausibility check live data already gets, before it's allowed to
#       seed SharedState - an implausible or corrupted cached value is
#       dropped and logged (gui/app.py reports the count/keys at startup)
#       rather than silently feeding a safety decision.
#   (13.3/13.9) bridge/management_engine.py, bridge/leaf_signals.py: a hand-
#       edited or corrupted profile.json could silently defeat any safety
#       threshold, since ManagementEngine.from_dict() never clamped loaded
#       values the way the GUI's own typing already did. Fixed: moved
#       FEATURE_FIELD_BOUNDS into management_engine.py itself so both
#       gui/panels.py (typing) and from_dict() (loading) share one bounds
#       table; same fix for charge_emulation via new leaf_signals.
#       CHARGE_EMULATION_BOUNDS. A value that can't even coerce to a number
#       is dropped, keeping the existing safe default.
#   (13.4) bridge/realtime_engine.py: the hard-cut latch (Rev 31 item I)
#       could be cleared by a phantom "replug" that wasn't one - any rising
#       edge of charge_active() counted, including a single dropped/delayed
#       0x1F2 frame or a brief VCM retry. Fixed: a resumption only counts as
#       a genuine replug if the request was genuinely ABSENT for at least
#       leaf_signals.CHG_END_STOP_S (3.0s, reused - the same threshold the
#       shutdown sequencer uses to decide a charge session really ended, not
#       a new invented number) beforehand. A too-brief resumption still
#       resumes the ramp normally, it just doesn't clear the latch.
#   (13.5) bridge/rz450e_signals.py, bridge/realtime_engine.py, bridge/
#       state.py: this project's own docs said Toyota's additive checksum
#       "should" be used for corruption detection, but it was never actually
#       wired in. New CHECKSUM_IDS/frame_checksum_ok() validates the 5 IDs
#       confirmed to carry the checksum (0x020/0x023/0x358/0x3F1/0x424) -
#       confirmed the other 4 decoded IDs (0x4A7/0x4A9/0x4C0/0x4AA) do NOT
#       carry one (0% match) and keep relying on PLAUSIBLE_RANGES alone.
#       Wired into _ingest_rz_bus() before decode; a mismatch is rejected
#       and logged (SharedState.note_checksum_failure()), never decoded.
#   (13.7) bridge/management_engine.py: over_temperature_derate going dark
#       on missing temp data was invisible (no status text, no fault_log
#       entries), unlike every voltage-based feature which already reports
#       "no data yet" explicitly. Fixed to match - observability only, the
#       actual applied behavior (full power/no derate with no temp data) is
#       unchanged, that's the separate 13.1 question.
#   (13.6, 13.8) Reviewed, no action taken per user call - hysteresis
#       "slow release" jump ceiling and capacity_bars_raw's implausible-
#       reading sentinel are both left as-is (low risk).
#   New tests/test_config_profile.py and tests/test_rz450e_signals.py (first
#   tests for those modules); tests/test_charge_ramp.py and tests/
#   test_management_engine.py gained extensive new coverage for the session-
#   boundary/data-gate/staleness/latch-replug behavior above. All 8 test
#   files pass; full App() launch confirmed clean.
#   DOCS: extensive line-by-line audit of every doc against current code,
#   requested separately after this implementation pass ("go line for line
#   through the docs and verify we don't have old or incorrect notes") -
#   corrected stale F8/checksum/threshold/default claims across README.md
#   and docs/01-12 (docs-only pass, no further code changes); this
#   changelog entry itself was written after that audit flagged that
#   REVISION had not been bumped to cover this round's work.
#
# Rev 34: worked through docs/13-review-checklist-2026-08-01.md's Part 14
#   (a fresh line-by-line pass reading every bridge/*.py and gui/*.py file in
#   full alongside every docs/*.md file, hunting for code/docs drift), item
#   by item:
#   (14.1) docs/08-gui-design.md: "12 total" tracked fault conditions was
#       stale (bridge/fault_log.py's FAULT_DEFINITIONS has grown to 19) -
#       corrected, plus a stale two-line-per-entry layout description fixed
#       to match the current single-line row. Docs-only.
#   (14.2) docs/07-startup-shutdown-plan.md's startup timeline said 0x1C2
#       starts at t=60ms; traced against the primary source (Leaf_BMS_
#       Emulator's 04-startup-sequence.md and HVBAT_PowerUp_Handshake_
#       Report.md) - both say to immediately start 0x1C2 with no gate, and
#       neither's own named-constants list includes a T_1C2_START; the
#       "+60ms" in the handshake report's raw timeline is an artifact of
#       when VCM traffic first appeared in that one .trc recording (measured
#       from the very first frame, including OTHER ECUs' one-shot alive
#       frames), not a deliberate code-level delay. The code
#       (bridge/realtime_engine.py's _tx_loop, sends 0x1C2 immediately) was
#       already correct - fixed the doc's table instead, per user directive
#       ("fix the docs if the code is correct... we want to keep the byte/
#       timing-exact we saw in the log"). Docs-only.
#   (14.3) SAFETY-RELEVANT BEHAVIOR CHANGE, user directive: "the bridge
#       should not wind down unless there is a real trigger to do so... a
#       hard cut should not stop the bridge... all fail safes are triggered
#       and the bridge still operates." The 5th wind-down trigger
#       (ShutdownSequencer._should_wind_down) was firing on ANY hard cut
#       (voltage/temp/cross-check emergencies too, not just the staleness
#       watchdog docs/07 always described) - meaning a non-staleness
#       emergency could wind the bridge down and go briefly SILENT on the
#       Leaf bus during the 'stopped' phase, instead of just latching
#       relay_cut_request/interlock and continuing to run/transmit. Fixed:
#       new ManagementEngine.staleness_hard_cut (reset False at the top of
#       every apply(), set True ONLY in the staleness_watchdog block's own
#       hard-escalation branch) is what RealtimeEngine._tx_loop now passes
#       into the sequencer instead of the old any-hard-cut variable
#       (_should_wind_down's parameter renamed hard_cut_this_tick ->
#       staleness_hard_cut for clarity) - bridge/management_engine.py,
#       bridge/realtime_engine.py. The general hard_cut variable is
#       unchanged for Log-panel/UI purposes; only the wind-down decision was
#       narrowed. New tests confirm a non-staleness emergency (low-voltage)
#       leaves staleness_hard_cut False while still asserting
#       relay_cut_request, and the staleness escalation sets both.
#   (14.4) GUI charge-stop status text always blamed "RZ450e permission not
#       granted" whenever full_charge_flag was set, even when the real cause
#       was the live-data gate (13.1b), the staleness watchdog (13.1), or -
#       not a fault at all - the AC target SoC being reached. New
#       RealtimeEngine.charge_status_summary() resolves the actual reason in
#       priority order from the same live status text each subsystem already
#       produces (staleness watchdog text, then ac_charge_taper's "target
#       reached" text reported as a CHARGE COMPLETE success rather than a
#       red STOPPED, then a new _last_charge_gate_reason set inside
#       _apply_charge_ramp itself) instead of guessing - both gui/panels.py's
#       ChargeEmulationPanel and gui/dashboard.py's Charge emulation section
#       now call this one method instead of duplicating their own wrong
#       logic. Confirmed the start-of-ramp log line already named all three
#       gates together (no change needed there) - the gap was entirely on
#       the stop/status side.
#   (14.5) User directive, confirmed: fault_log entries should behave live
#       like everything else in this real-time engine, not freeze when a
#       feature is disabled mid-active-fault. Every feature block in
#       ManagementEngine.apply() gained an else-branch for its own `not
#       f['enabled']` case, via a new _clear_disabled_feature() helper that
#       immediately marks that feature's tracked fault_log entries inactive
#       ('feature disabled') and sets status[feature]='disabled', instead of
#       just letting the update() calls silently stop happening. Also resets
#       each feature's own hysteresis/persistence state on disable
#       (_discharge_factor_applied, _regen_factor_applied, _overcurrent_
#       since, _cross_check_since, _stale_since) so re-enabling later starts
#       clean rather than resuming a stale ramped-down factor.
#   All 8 test files pass (2 new checks for 14.3, 1 new check for 14.5);
#   full App() launch confirmed clean, including exercising
#   charge_status_summary() directly.
#
# Rev 35: added docs/13-review-checklist-2026-08-01.md's Part 15 (a full
#   fault-trigger catalog - every FAULT_DEFINITIONS entry with its trigger
#   condition, reset condition, and effect, one checklist item per fault,
#   "so I can check each one") and worked through the user's item-by-item
#   response:
#   (15.1-15.4/15.9 wording) User caught real ambiguity: "worst individual
#       cell voltage" doesn't say whether that means highest or lowest
#       without already knowing the context - it's the code's own variable
#       naming (worst_high/worst_low = "closest to a problem," which flips
#       direction between an overvoltage and undervoltage check). Docs-only
#       fix: every entry in the Part 15 catalog now says "highest"/"lowest"
#       explicitly in prose.
#   (15.3/15.4) User directive: "lets set this to 4.2V as our norm base
#       config." charge_target_taper.emergency_high_v and charge_emulation's
#       ac_emergency_v both changed 4.30V -> 4.20V (bridge/management_
#       engine.py, bridge/leaf_signals.py) - the standard NMC charge ceiling
#       exactly, tightening the margin above the 4.15V zero-power point to
#       0.05V (still passes the zero_v < emergency_v config-sanity check).
#       NOTE: a config/profile.json already saved with the old 4.30V value
#       keeps using 4.30V until re-saved or edited - this is a
#       default_config() change only, same as any other default-vs-saved
#       case.
#   (15.14/15.15) User directive: "we should add enable disable for our app
#       for testing and so we know its something thats happning and can be
#       tested. default to on." Both input_validation_reject and
#       checksum_reject were always-on with zero config - given real
#       toggleable features (input_validation, checksum_validation, both
#       {'enabled': True}, no threshold fields) with their own Battery
#       Management tab checkboxes (gui/panels.py, empty FEATURE_FIELDS
#       list). RealtimeEngine._ingest_validated() and _ingest_rz_bus()'s
#       checksum check both now read these flags LIVE (not cached) before
#       actually running rz450e_signals.validate_inputs()/
#       frame_checksum_ok() - disabling doesn't just hide the fault light,
#       it lets the (deliberately bad, for testing) data or frame through
#       unfiltered. Disabling either clears its fault_log entry live, same
#       pattern as item 14.5. New tests/test_realtime_engine.py (new file,
#       first test coverage for this module): confirms both toggles
#       actually gate the underlying check (not just the report), including
#       a real background-thread test of _ingest_rz_bus() against a queued
#       frame with a deliberately-wrong checksum byte.
#   DOCS: propagated the 4.20V threshold and the two new toggleable features
#   through every doc that referenced the old always-on/4.30V state -
#   docs/02 (checksum section), docs/05 (threshold table + new feature-table
#   rows for input_validation/checksum_validation), docs/06 (staleness
#   watchdog section), docs/08 (GUI design, new checkbox mentions), docs/09
#   (STM32 export example JSON + the "fixed-logic safety nets" section
#   reworked - whether these checks RUN is now profile data, the formulas/
#   ID-sets/ranges themselves stay fixed), docs/11 (verification checklist:
#   updated 4.30V rows, added a missing input-plausibility-validation row
#   that never existed, updated the checksum row for the new toggle),
#   docs/12 (historical audit table annotated with the 2026-08-03 retune,
#   same pattern as prior threshold changes). All 9 test files pass; full
#   App() launch confirmed clean, including the two new Battery Management
#   checkboxes actually present in ManagementEngine.config.
#
# Rev 36: two workflow/tooling changes from the same conversation, both user
#   directives, not app behavior changes:
#   (1) `config/` is now git-tracked - `.gitignore`'s `config/*.json` line
#       removed. Previously the user's saved profile.json/last_known_good.
#       json/fault_log.json and any named snapshot profiles were all
#       gitignored; the user wants config/ treated as a running backup
#       instead, tracking everything in it (including the two continuously-
#       autosaved data files, an explicit choice over the lower-noise
#       "settings files only" alternative).
#   (2) New tests/check_profile_drift.py - found live that config/
#       profile.json was silently carrying forward several pre-2026-08-01
#       researched defaults (min_soc_pct=10.0, emergency_low_v=2.8,
#       discharge taper 3.5/3.0, regen taper 3.9/4.1, emergency_temp_
#       f=149.0, warn_delta_v=0.05) through session after session of
#       reload-then-resave, since none of this session's many threshold
#       retunings had ever been reflected in a GUI edit+save. User directive:
#       "a method to be sure that in the future we don't miss something like
#       that by checking against the profile.json and see what is changed
#       and why." Deliberately NOT a pass/fail test (named check_* instead of
#       test_*, not swept into the "run every test_*.py" loop) - a saved
#       profile SHOULD differ from code defaults when deliberately tuned, so
#       "differs" isn't itself a failure. Prints a field-by-field report in
#       three categories (CHANGED / MISSING FROM PROFILE / ORPHANED IN
#       PROFILE) comparing a saved profile.json against
#       bridge/management_engine.py's default_config() and bridge/
#       leaf_signals.py's slider/check-derived defaults - run any time after
#       changing a default to see its real effect on the actual saved file,
#       not just trust nothing crashed. Confirmed working against the real
#       config/profile.json: found exactly the 8 stale fields above.
#   All 9 test files still pass; docs/14-validation-test-plan.md documents
#   both changes.
#
# Rev 37: user asked to re-save config/profile.json from current code
#   defaults and do a full project sweep to confirm nothing else was missed,
#   after the profile-drift discovery. No bridge/gui behavior changed - this
#   is entirely a config-data resave + test-coverage-closing pass:
#   - config/profile.json resaved via MappingEngine()/ManagementEngine()/
#     SharedState() fresh construction + config_profile.save_profile() -
#     confirmed 0 drift against tests/check_profile_drift.py afterward.
#     Confirmed the two named 2026-07-31 snapshot profiles (7-31-2026-*.json)
#     are DELIBERATELY historical (real-hardware-confirmation reference
#     points cited by name in docs/11) and should NOT be resaved - their
#     drift is expected/correct, not a miss.
#   - Cross-checked, programmatically, that every FAULT_DEFINITIONS key has
#     a matching fault_log.update() call site in management_engine.py and
#     vice versa (19/19 both directions), that every default_config()
#     feature/field has full GUI coverage (ManagementPanel's FEATURE_FIELDS/
#     LABELS/MANAGEMENT_FEATURE_HELP, and FEATURE_FIELD_BOUNDS/
#     CHARGE_EMULATION_BOUNDS), and that every GENERATED_SIGNALS checkbox
#     actually gates its frame builder - all clean, zero gaps found in any
#     of these.
#   - Found 2 more real stale docs/05 claims the earlier threshold-update
#     pass missed: the over-temperature section's prose still said the
#     emergency hard-cut trigger was 65C/149F (actual: 61C/141.8F, tightened
#     2026-08-01), and the "Operating SoC window" line still said "10% min"
#     with no mention min_soc_pct was later retuned to 8%. Both fixed.
#   - Found docs/14 claimed discharge_power_taper's hysteresis "has a direct
#     test" - it did NOT (confirmed by search - zero hysteresis assertions
#     existed for either taper). Closed for real: new tests/
#     test_management_engine.py::test_discharge_power_taper_hysteresis_
#     fast_attack_slow_release and ::test_charge_target_taper_regen_
#     hysteresis_fast_attack_slow_release (both confirm instant snap-down on
#     a dip/rise, partial-not-full recovery mid-ramp, full recovery once the
#     ramp completes).
#   - Closed 4 more docs/14 Part 1 TODOs that were tracked but never
#     actually implemented: cell_data_cross_check had ZERO test coverage at
#     all despite being a real soft->hard escalating cutoff (new
#     test_cell_data_cross_check_soft_and_hard_escalation);
#     _check_config_sanity() likewise had zero coverage (new
#     test_config_sanity_detects_inverted_threshold); the staleness
#     watchdog's "never arrived" case was tested but a signal going
#     fresh->stale mid-session never was (new
#     test_staleness_watchdog_flags_a_signal_that_went_stale_mid_session,
#     backdates one live signal's timestamp directly); cell_imbalance_warn/
#     overcurrent_discharge_warn/overcurrent_charge_warn fault_log entries
#     were never directly asserted (only their status text was) - added to
#     the existing F2/F4 tests. Verified hard-cut latching's 4 sub-items
#     were in fact already fully covered by existing tests and checked that
#     TODO off too. Only docs/14's BusConnection reconnect-race item remains
#     genuinely open (needs a threading-level test against can_backend.py,
#     more involved - left as the one item not closed this pass).
#   - Updated CLAUDE.md: fixed lingering pre-code-era phrasing ("once app
#     code exists", "once one exists"), added a `config/`-is-git-tracked
#     convention note and a reminder to run check_profile_drift.py after
#     changing a default. Cross-referenced the same in docs/06's
#     "Interaction with config save/load" section.
#   All 9 test files pass (7 new test functions added, closing 6 real
#   coverage gaps); profile drift confirmed 0; full App() launch confirmed
#   clean.
#
# Rev 38: closed the one remaining docs/14 gap from Rev 37's sweep, per user
#   request ("should we add the BusConnection test? yes, add those two").
#   New tests/test_can_backend.py (first coverage for this module), two
#   tests for the docs/13 items 3.1/3.2 reconnect-race fix:
#   test_disconnect_interrupts_the_monitor_promptly_not_after_the_full_
#   interval (deterministic - confirms the monitor thread exits within ~1s
#   of disconnect(), not the full 3.0s RECONNECT_INTERVAL_S a reverted-to-
#   time.sleep() implementation would take) and
#   test_rapid_disconnect_reconnect_cycling_never_leaves_the_connection_
#   without_a_monitor (10x connect/disconnect stress cycle, confirms exactly
#   one live monitor thread and a working connection at the end, no leaked-
#   away monitor). Confirmed stable across 5 repeated runs, no flakiness.
#   Deliberately does NOT attempt the exact microsecond-scale race in item
#   3.2 (a reconnect completing right as disconnect() fires) - not
#   practically testable deterministically without adding test-only
#   synchronization hooks to bridge/can_backend.py itself; real-hardware/
#   manual verification remains the only way to close that specific
#   sub-case. All 10 test files now pass.
#
# Rev 39: closed out every remaining open item in
#   docs/13-review-checklist-2026-08-01.md (user request: "should we fix the
#   open items? ... lets make sure the checklist is fully 'done'"). Two items
#   resolved as deliberate documentation-only decisions, four implemented in
#   full per the user's explicit "i want to do it all if it was supose to be
#   in place and it was missed":
#   (1) AC-charger taper hysteresis asymmetry (docs/13 Part 9) - user
#       decision: "let's leave it and mark it as such so it's not confusing
#       in the future." ac_charge_taper deliberately does NOT get the same
#       fast-attack/slow-release hysteresis charge_target_taper (regen) has -
#       documented as intentional, not an oversight, in a code comment
#       (bridge/management_engine.py, the ac_charge_taper block) and in
#       docs/05-battery-management-safety.md's CC->CV section.
#   (2) Cell-coverage indicator (docs/13 item 1.3) - user decision: "let's
#       mark as the staleness watchdog covers this as you show." No separate
#       "N/96 cells reporting" UI was built; resolved as already covered by
#       the existing staleness watchdog + input/checksum validation.
#   (3) Boundary-value sweep tests (docs/13 item 6.3) - 6 new test functions
#       in tests/test_management_engine.py, each checking just-before AND
#       just-after the exact configured threshold instead of only "clearly
#       inside/outside": low_voltage_cutoff's 2.0s soft_cut_persistence_s,
#       overcurrent_monitor's 5.0s persistence_s, over_temperature_derate's
#       141.8F emergency_temp_f (plus confirming the >= comparison fires
#       exactly at the boundary), cell_imbalance_monitor's 100mV
#       warn_delta_v, cell_data_cross_check's 150mV max_delta_v plus its own
#       soft/hard escalation timing, and staleness_watchdog's own soft/hard
#       escalation timing (the latter two use temporarily-shrunk 0.15s
#       windows so the tests don't need to wait the real 60s/+5s). 22 new
#       checks, all passing.
#   (4) Tightened two loose test assertions (docs/13 item 6.4) -
#       test_f1_cold_block_uses_coldest_probe's second check now asserts the
#       exact expected charge_limit_kw instead of a bare > 0.0;
#       test_f3_cold_derate_ramp's midpoint check now asserts the exact
#       computed 0.5 factor (within 1e-9) instead of a loose
#       0.35 < factor < 0.65 range.
#   (5) Fault History window grouped by tier (docs/13 Part 15 closing note) -
#       gui/fault_history_window.py now sections its rows into "Monitor Only
#       - no cut" / "Soft Cut" / "Hard Cut", low-to-high severity, using the
#       level already carried by every FAULT_DEFINITIONS entry (and every
#       dynamic clamp_<key> entry, always 'warn') as the grouping key - no
#       separate mapping table needed. New Warn.TCombobox-adjacent tier
#       heading colors reuse the same _LEVEL_COLOR already used for the
#       per-row status lights.
#   (6) Mapping-tie orphan/rename warning (docs/13 item 4.3) - MappingPanel
#       (gui/panels.py) now distinguishes a genuinely-blank input/output slot
#       from one referencing a key no longer in the current signal registry
#       (a future field rename, or a profile predating a registry change).
#       An orphaned slot shows "(!) UNKNOWN KEY: <raw key>" and is styled
#       with a new dedicated Warn.TCombobox ttk style (gui/theme.py, red
#       field/foreground) instead of silently rendering identically to
#       "(unused)". Fixed a real latent data-loss bug found while building
#       this: the orphaned display string is now registered back into
#       input_display_to_key/output_display_to_key so _update_tie() resolves
#       it back to the original key on every edit, instead of resolving an
#       unrecognized string to '' and silently deleting the stale reference
#       the next time any other field on that row changed. Verified with a
#       GUI smoke test asserting an orphaned tie's inputs/output survive
#       _update_tie() unchanged, and that the warning style clears once a
#       real replacement is picked.
#   All 10 test files pass (22 new boundary-sweep checks + 2 tightened
#   assertions); GUI smoke test confirms MappingPanel (including an orphaned
#   tie) and FaultHistoryWindow (tier-grouped) both build with no exceptions.
#   docs/13 and docs/14 updated to reflect every item above as resolved -
#   docs/13's checklist has no remaining open items from this review pass
#   except the two already-acknowledged non-actionable ones (3.2/12.4's
#   exact reconnect-race sub-case, and 5.4's shared-state locking, both
#   pending real-hardware/architecture decisions not yet made).

# Rev 40: implemented the user's item-by-item response pass over
#   docs/13-review-checklist-2026-08-01.md's new Part 16 (a fresh full
#   safety re-audit against docs/memory/checklist + NMC-BMS industry
#   research, 4 new findings, 0 code regressions found in that pass itself).
#   All four items resolved per explicit user direction:
#   (1) Item 16.1 (background-thread fault containment) - user asked
#       whether a Python try/except fix would carry over to the future
#       STM32 port; answered directly (the mechanism doesn't, the safety
#       principle does, and is actually stronger in firmware via a hardware
#       watchdog - see docs/09's new note) then implemented since the
#       answer was yes. `RealtimeEngine._tx_loop()`/`_ingest_rz_bus()`/
#       `_did_poll_loop()` now catch an unexpected exception, log it
#       (rate-limited to 1/s), and continue instead of the thread dying
#       permanently and silently. `_tx_loop`'s heartbeat stamp
#       (`last_tick_monotonic`) was deliberately moved to only update on a
#       SUCCESSFUL tick, not unconditionally - a one-off failure now
#       self-heals invisibly, but a PERSISTENT failure still correctly
#       trips "Bridge: NOT RESPONDING" exactly as before; catch-and-continue
#       must never mask an ongoing problem as "fine" - bridge/
#       realtime_engine.py.
#   (2) Item 16.2 (temp_min/temp_max plausibility gap) - user directive:
#       cross-check the 0x4A7 pack-extremes summary against the actual
#       min/max of the 16 individual 0x4AA probes, not just a fixed
#       temp_min<=temp_max check. New `temp_data_cross_check` feature,
#       mirroring `cell_data_cross_check`'s soft->hard escalation structure
#       exactly (10°F max delta / 60s soft / +5s hard, documented starting
#       point) - protects the cold-side lithium-plating-prevention logic
#       (keyed on temp_min) from a decode/mux fault that individually
#       passes PLAUSIBLE_RANGES but is physically inconsistent with the 16
#       probes it's presumably derived from - bridge/management_engine.py
#       (config, apply() logic, FEATURE_FIELD_BOUNDS), bridge/fault_log.py
#       (2 new FAULT_DEFINITIONS entries), gui/panels.py (fields/label/help
#       text), 3 new tests in tests/test_management_engine.py, docs/05,
#       docs/09, docs/11 updated.
#   (3) Item 16.3 (capacity_empty/full_charge_flag/interlock/
#       relay_cut_request exposed as ordinary mapping targets) - user
#       directive: fix it, and make sure all four stay visible on the
#       Dashboard. Found a real bug while tracing the fix: a code comment
#       claiming relay_cut_request was already excluded from mapping
#       targets was WRONG - it had been sitting in SLIDERS, fully
#       user-mappable, the whole time. Fixed properly: new
#       leaf_signals.MANAGEMENT_EXCLUSIVE_KEYS excludes all four from
#       OUTPUT_SIGNALS (can't be picked as a Signal Mapping tie's output
#       anymore) while they stay in SLIDERS/CHECKS/DEFAULTS/RANGES for
#       everything else; ManagementEngine.apply() now also explicitly
#       clears capacity_empty/relay_cut_request/interlock back to their
#       safe value every tick when no condition holds (not just
#       conditionally forcing the cut value) - full_charge_flag deliberately
#       excluded from that second half since RealtimeEngine._apply_
#       charge_ramp() legitimately also sets it from a different module, and
#       a centralized clear would race against and undo that. Added
#       relay_cut_request to the Dashboard's Flags section (previously only
#       in the main per-signal bar list) so all four safety flags are
#       grouped together - bridge/leaf_signals.py, bridge/
#       management_engine.py, gui/dashboard.py, 2 new tests
#       (tests/test_mapping_engine.py, tests/test_management_engine.py),
#       docs/03 corrected.
#   (4) Item 16.4 (isolation monitoring / weld detection, undocumented NMC-
#       BMS industry practices) - user asked to research online whether the
#       RZ450e has DTCs for either. Researched: isolation monitoring is
#       real and plausible (generic OBD-II P0AA6, confirmed applicable to
#       both Toyota/Lexus and Nissan EVs) but not currently reachable - this
#       project only implements UDS ReadDataByIdentifier (service 0x22);
#       DTC-style isolation faults need ReadDTCInformation (service 0x19),
#       never attempted against the RZ450e by this project or the reference
#       project. Weld detection confirmed to be a VCM/inverter-side
#       electromechanical check with no representation on either CAN bus
#       this project taps into - structurally unreachable, not just
#       unimplemented, matching the user's own guess that it comes from the
#       VCM/Leaf side, not the battery. Both added as new, fully-reasoned
#       docs/10 open questions (#14/#15) with concrete next steps (or the
#       lack thereof) spelled out - no code change, doc-only per the actual
#       nature of the finding.
#   config/profile.json resaved to pick up temp_data_cross_check's new
#   defaults - confirmed 0 drift via tests/check_profile_drift.py
#   afterward. All 10 test files pass (6 new test functions added across
#   this session). docs/13's Part 16 items all have real outcome notes now,
#   same discipline as every prior round.
#
# Rev 41: response pass over two real bench-hardware test logs (logs/
#   minileaf_20260805_200831..., logs/minileaf_20260805_202221...) - wrote
#   two new diagnostic scripts (tests/check_charge_ramp_log.py, tests/
#   check_shutdown_sequencer_replay.py) to decode the actual captured CAN
#   traffic and replay it through the real ShutdownSequencer class rather
#   than guessing at root causes. 4 items:
#   (1) Log save location - Start Log's save dialog now defaults into a new
#       logs/ folder (sibling of config/) instead of config/ itself, per
#       user request. New config_profile.LOGS_DIR/_ensure_logs_dir() -
#       bridge/config_profile.py, gui/app.py. logs/ added to .gitignore
#       (per-session capture artifacts, not a deliberate tracked backup
#       like config/*.json).
#   (2) Bridge staying awake indefinitely after a charger unplug - decoded
#       the "restart" log: 2 of 3 charge cycles wound down/slept correctly
#       (confirmed by replaying the real captured Leaf-bus traffic through
#       the actual ShutdownSequencer class - reproduces both real
#       wind-downs almost exactly), but the 3rd cycle transmitted
#       continuously for ~110s past where every trigger's own timer should
#       have fired. Root cause NOT fully identified from the capture alone -
#       documented honestly as an open question (docs/10 #16), not swept
#       under the rug. Added a 6th, bridge-specific defensive wind-down
#       trigger regardless: if the Leaf bus goes completely silent (any ID)
#       for leaf_signals.BUS_SILENCE_TIMEOUT_S (30.0s), wind down - a bench
#       rig with no ignition wiring can only ever rely on the
#       charge-session triggers, and this closes the "none of them ever
#       resolve, for any reason" gap - bridge/leaf_signals.py, bridge/
#       realtime_engine.py (ShutdownSequencer._should_wind_down()), docs/07,
#       docs/10, docs/11.
#   (3) AC charger taper "jumps around" during a ramp-down test - decoded
#       the cutoff-test log: charger_limit_kw genuinely oscillating
#       (5.20->5.00->5.10->...kW) while cell voltage sat inside a tight
#       3.62/3.64V test band the user had configured. Root cause: this
#       taper was deliberately built with ZERO hysteresis (a 2026-08-03
#       decision, docs/13) - a pure function of noisy instantaneous
#       per-cell voltage. REVERSED that decision based on this new
#       real-hardware evidence: added the same fast-attack/slow-release
#       hysteresis the sibling regen/discharge tapers already have, via new
#       configurable ac_recovery_ramp_s. Also, per user directive:
#       renamed ac_zero_v -> ac_min_v and changed the taper's floor from a
#       literal 0kW to a configurable ac_min_kw (holds there instead of
#       driving to true zero); added a new, separate ac_cutoff_v
#       ("minimum value" vs. an explicit "stop charging" voltage, working
#       together) that deliberately ends the session (full_charge_flag) once
#       crossed, instead of relying on the vehicle reacting to a near-zero
#       power request; added configurable ac_min_kw/ac_max_kw AC power
#       request bounds (6.6kW default ceiling = the Leaf's real onboard AC
#       charger max) that clamp both this taper's floor and the manual
#       charge_target_kw ramp target; added dc_min_kw/dc_max_kw as a
#       PLACEHOLDER ONLY (confirmed scope - no active DC logic yet),
#       surfaced on the Future tab. Also fixed, found while reading the
#       ramp code (not from this specific log): RealtimeEngine.
#       _apply_charge_ramp()'s target tracking only rate-limited an
#       INCREASING target (min(current+rate*dt, target)) - a DECREASING
#       target (e.g. live user edit, or the new min/max kW clamp) snapped
#       instantly with no rate limit at all, the actual mechanism behind
#       the observed oscillation; now rate-limited symmetrically in both
#       directions for real fine (100W-scale) precision either way -
#       bridge/leaf_signals.py (CHARGE_SLIDERS), bridge/management_engine.py
#       (ac_charge_taper rewrite, updated sanity checks), bridge/
#       realtime_engine.py (_apply_charge_ramp), bridge/config_profile.py
#       (one-time ac_zero_v -> ac_min_v migration for older saved
#       profiles), gui/panels.py, docs/05, docs/09, docs/11.
#   (4) Charge Emulation tab number entries silently swallowed invalid
#       input (empty string, out-of-range) with zero visual feedback,
#       unlike the Battery Management tab - user confirmed being able to
#       enter nothing or an out-of-range value with no indication either
#       way. Ported ManagementPanel's exact invalid/clamped flag_lbl
#       pattern into every Entry on the Charge Emulation tab (and the new
#       DC placeholder fields) - gui/panels.py.
#   config/profile.json NOT resaved this pass (leaf_signals.py/
#   management_engine.py default changes only affect NEW/never-set fields
#   plus the ac_zero_v->ac_min_v migration path) - run tests/
#   check_profile_drift.py before relying on that assumption.
#
# Rev 42: same-day follow-up to Rev 41 item 3, after the user pushed back on
#   the fixed-time-constant hysteresis fix ("it was jumping from 4.4kw to 0
#   ... it should ramp down slowly... the closer it gets to its set point,
#   the gentler it needs to adjust") and proposed reusing the existing 0-7
#   ramp-rate levels to self-adjust instead of a fixed ramp time. Diagnosed
#   correctly, with a real physics explanation: this is a CC-CV charging
#   control loop, not a danger-response cutoff - an instant downward step
#   (even with Rev 41's slow release afterward) lets voltage sag more than
#   necessary, the taper reads that as safe and lets power back up,
#   overshoots, and hunts. Fast-attack-on-a-dip is right for the discharge/
#   regen tapers (arresting real cell sag under load, unchanged) but wrong
#   for a charger converging to a steady-state setpoint.
#   Implemented per the user's own architecture, confirmed via discussion:
#   ac_charge_taper now dynamically self-selects one of the existing 0-7
#   chg_uprate_level rates (2.0kW/s at level 7, halving per level down -
#   same real-hardware-confirmed table _apply_charge_ramp already uses)
#   based on remaining distance to target, always starting at level 7 the
#   moment convergence begins, downshifting/upshifting symmetrically (with
#   hysteresis on the LEVEL SWITCH itself via new _AC_LEVEL_DOWNSHIFT_KW/
#   _AC_LEVEL_HYSTERESIS_MULT, so the selected level doesn't flap right at a
#   boundary - verified directly with a scripted oscillation staying locked
#   at one level instead of flapping) as the gap narrows or grows. Each step
#   is clamped to land exactly on target, never overshoots. Per explicit
#   user clarification ("the 0-7# is a driven number that gets sent out on
#   the bus... needs to kinda represent what's going on with the request"),
#   the dynamically-selected level is now what's actually TRANSMITTED in
#   0x1DC's own uprate bits while genuinely converging (new
#   ManagementEngine.ac_uprate_level, read by RealtimeEngine.
#   _compose_leaf_state()) - overriding the manually-configured
#   chg_uprate_level only during that window. ac_recovery_ramp_s (Rev 41's
#   fixed-time-constant field) removed cleanly - never reached a saved
#   profile, no migration needed - bridge/management_engine.py
#   (_select_ac_uprate_level(), ac_charge_taper rewrite), bridge/
#   realtime_engine.py (_compose_leaf_state() override), bridge/
#   leaf_signals.py (field removal), gui/panels.py (field removal,
#   AC_CHARGE_TAPER_HELP/CHARGE_EMULATION_HELP rewritten).
#   IMPORTANT CORRECTION found while writing this fix's test coverage: the
#   2026-08-05 log originally cited as evidence for Rev 41 item 3's AC-taper
#   diagnosis was re-checked and the taper was NEVER actually engaged during
#   that capture - cell voltage stayed 3.616-3.640V the entire session,
#   never reaching the 4.00V ac_full_v floor (_ramp_factor() evaluates to
#   exactly 0.0 there), so instant_factor was a constant 1.0 (untapered) in
#   both the old and new code. That log's real oscillation is far more
#   likely explained by _apply_charge_ramp()'s own asymmetric rate-limiting
#   bug (also fixed in Rev 41, independently) reacting to a live-adjusted
#   ramp target - not by this taper's hysteresis at all. This rework is
#   still a sound, real fix for when the taper genuinely IS engaged (the
#   user's CC-CV physics reasoning is correct and general) but is NOT
#   confirmed to be what fixed that specific log's evidence - docs/13 items
#   17.3/17.5, docs/11, docs/15 all corrected to reflect this distinction
#   rather than silently deleting the earlier (wrong) claim.
#   8 new/replaced tests in tests/test_management_engine.py (level-7 start,
#   progressive downshift, no-overshoot-under-huge-dt, level-switch
#   hysteresis via direct unit test, None outside the convergence window,
#   and a synthetic noisy-voltage-inside-the-window regression scenario -
#   explicitly NOT a replay of the 2026-08-05 log, which never entered the
#   taper's window at all); 1 pre-existing test in tests/test_charge_ramp.py
#   fixed for the new gentle-convergence behavior (was asserting an instant
#   halving). All test files pass. docs/05, docs/08, docs/09, docs/11,
#   docs/13, docs/15 updated.
#
# Rev 43: Rev 42's "correction" (the claim that the 2026-08-05 log never
#   entered ac_charge_taper's window at all) was ITSELF WRONG - user
#   directly corrected it: "you were correct. i set the values to 3.62 and
#   3.64 for testing." The correction had checked cell voltage against the
#   CODE DEFAULT ac_full_v/ac_min_v (4.00V/4.15V) instead of the actual
#   bracketed test values the user had configured (3.62V/3.64V, this
#   project's own "bracket the threshold, not the battery" technique,
#   docs/15) - the log's real voltage (3.616-3.640V) sits squarely inside
#   THAT window, just not the default one.
#   Re-verified numerically: the OLD zero-hysteresis formula
#   (ramped_kw * ac_factor, ramped_kw=92.3) reproduces the log's real
#   observed charger_limit_kw values to within encoding rounding at every
#   sample checked (50.07~50.00, 44.44~44.40, 27.54~27.50, 21.90 exact,
#   16.27~16.30, 5.00 exact, 0.00 exact) - airtight confirmation of the
#   ORIGINAL diagnosis from Rev 41.
#   Retested the NEW code per the user's explicit request ("you can retest
#   with those values if you want"): new tests/check_ac_taper_log_replay.py
#   replays the REAL captured cell-voltage trace from the log through the
#   actual current ManagementEngine.apply() (real elapsed time between
#   calls) at the real ac_full_v=3.62/ac_min_v=3.64 configuration - worst
#   single-call jump across the entire ~1780-second real replay is
#   0.819kW, vs. the original log's real multi-kW same-tick jumps. Strong
#   real-data confirmation the Rev 42 fix genuinely resolves the exact
#   scenario captured in this log - still a software replay of captured
#   data, not a live bench retest, so docs/11 stays at "Documented," not
#   promoted to "Confirmed" (that's the user's manual sign-off step).
#   docs/13 (new item 17.6, plus a retraction note appended directly to the
#   wrong Rev 42 correction rather than deleting it - keeps the mistake-and-
#   correction trail visible), docs/05, docs/11, docs/15 all corrected back.
#   Also added project memory capturing the methodology lesson: when
#   diagnosing a bug from a real .trc capture, check the ACTUAL session-
#   configured threshold values, not code defaults - this project's
#   bracketing-test technique means a real test often deliberately runs
#   with non-default thresholds, so assuming defaults were in effect is not
#   a safe shortcut. All test files still pass (unchanged from Rev 42 - no
#   code logic changed this revision, only a new diagnostic script and doc/
#   memory corrections).
#
# Rev 44: user report on the Dashboard window: "there is no scrowl so i cant
#   see whats on the botom" - the right-hand column (Flags, Generated
#   Signals, Charge Emulation, Battery Management status) was a fixed-height
#   ttk.Frame with pack_propagate(False); Battery Management status grows
#   with however many features are active and had nothing to scroll to once
#   it ran past the bottom of the window. gui/dashboard.py: right column now
#   built inside the same VScrollFrame already used for the left per-signal
#   list, with its own independent mouse-wheel scroll; updated the in-app
#   help text that previously claimed the column was "sized so everything
#   fits without scrolling."
#
# Rev 45: same-day follow-up - user preferred the main window's existing
#   'Live transmitted values' style over Rev 44's VScrollFrame-of-widgets
#   fix: "the data in the main screen for live data works well... lets use
#   that for the data in the right side of the dashboard... i like the way
#   that works. its cleener." gui/dashboard.py: right column rebuilt to
#   reuse gui/panels.py's LiveMonitorPanel (the same read-only, auto-
#   refreshing tk.Text widget gui/app.py's own 'Live transmitted values'
#   panel uses) instead of one ttk.Label per field - a plain tk.Text widget
#   scrolls natively with the mouse wheel, so this also satisfies Rev 44's
#   original scroll request more simply, without the VScrollFrame/Canvas
#   wrapper. New DashboardWindow._right_column_text() formats the Flags/
#   Generated-signals/Charge-emulation/Battery-management data as one text
#   block in the same "-- Group --" style as panels.leaf_tx_monitor_text
#   (kept separate from that function rather than sharing it, since this
#   version needs the relay_cut_request row, the two Dashboard-only
#   sections, and `engine.charge_status_summary()`). Trade-off: per-field
#   color-coding (OK/ERR/dim for stale, granted/not-granted, etc.) is gone -
#   matches the main screen's own monochrome live-text convention, which
#   already has no per-field color tags anywhere in this codebase.
#
# Rev 46: same-day follow-up - user looked at Rev 45's result: "the data is
#   not as cleen... can we get the data alighned. if we need to use 2 lines
#   to get the dat ato be up agnest the right side of the frame." Root
#   cause: leaf_tx_monitor_text's/LiveMonitorPanel's usual label:value
#   space-padding trick (f'{label:<40} {value}') only lines up in a
#   monospace font - the Dashboard's Text widget uses DASH_FONT (Segoe UI,
#   proportional), so padded columns never actually aligned on screen.
#   gui/dashboard.py: dropped the plain-string _right_column_text() from
#   Rev 45 in favor of _write_right_column(), which writes directly into
#   self.right_box using a Text tag ('val', justify='right') - each field
#   is now its own label line followed by its value on the next line,
#   right-justified flush against the column's edge regardless of label
#   length or font metrics, exactly the two-line layout the user asked
#   for. Still refreshed from the existing _tick() loop; still a plain
#   read-only tk.Text (scrolls natively), so Rev 44/45's scroll fix is
#   unaffected. Confirmed via a smoke script driving a real App/
#   DashboardWindow through several ticks - 19 correctly right-justified
#   value lines, no exceptions.
#
# Rev 47: user request: "lets do the same formating to the live data in the
#   main window" - the Rev 46 label/right-justified-value two-line layout
#   only reached the Dashboard's right column; the main window's own two
#   LiveMonitorPanel instances ('Live decoded values' on the left,
#   'Live transmitted values' on the right - gui/app.py) still used the
#   original label:value single-line, space-padded format, which has the
#   exact same proportional-font misalignment Rev 46 fixed.
#   Extracted the write pattern into shared helpers instead of copying it a
#   third time: gui/theme.py gained configure_field_tags(box)/
#   write_section(box, title)/write_field(box, label, value) (the 'hdr'/
#   'val' tag setup and two-line-per-field write, factored out of Rev 46's
#   dashboard-only version). gui/panels.py: LiveMonitorPanel now takes a
#   write_fn(box) that writes directly into the (pre-cleared) Text widget
#   via those helpers, instead of a text_fn() returning a plain string;
#   input_monitor_text()/leaf_tx_monitor_text() became
#   write_input_monitor(box, state_model)/write_leaf_tx_monitor(box,
#   state_model); box wrap changed 'none' -> 'word' to match the
#   Dashboard's. gui/app.py's two LiveMonitorPanel(...) call sites updated
#   to the new write_fn signature. gui/dashboard.py's right column also
#   switched from its own local section()/field() closures to the same
#   shared helpers (no behavior change there, just de-duplication).
#   Verified with a smoke script driving a real App: both main-window
#   Text widgets and the Dashboard's right column all populate correctly
#   (127 and 26 right-justified value lines respectively) across several
#   refresh ticks with no exceptions; all 9 tests/test_*.py scripts still
#   pass (none touch the GUI layer, but confirm the underlying data these
#   panels read was untouched).
#
# Rev 48: two more user requests in one pass. (1) "lets put all those items
#   on the same line. only a few of them need a roll over to a new line" -
#   reverted Rev 46/47's two-line label/right-justified-value layout back
#   to one line per field, but fixed the ROOT CAUSE this time instead of
#   working around it: the space-padding alignment trick never worked
#   because these Text widgets used BASE_FONT/DASH_FONT (Segoe UI,
#   proportional). gui/theme.py: new MONO_FONT = ('Consolas', 8), used only
#   by gui/panels.py's LiveMonitorPanel box and gui/dashboard.py's
#   right_box; write_field(box, label, value, label_w=40, value_w=12) now
#   writes one space-padded line (real alignment, since the font is
#   actually monospace now) instead of two tagged lines - nothing is
#   truncated, so the handful of genuinely long labels (the two ~60-char
#   SOFT CUT flag descriptions) just word-wrap, exactly the "few" the user
#   expected. configure_field_tags() dropped the now-unused 'val' tag.
#   (2) "i want all 96 cells to be shown on a bar type graph right below
#   the live data on the dashboard. 96 vertical bar's. i also want all 16
#   temp probes on a different spot next to that. lets scale from min to
#   max ranges for those items. dont resize the window, it will fit down
#   there below the history data." gui/dashboard.py: new _MultiVBar class
#   (one Canvas, N independently-scaled vertical bars) - unlike every
#   other bar in this window, set_values() scales to the LIVE min/max of
#   just that call's data instead of rz450e_signals' fixed (lo, hi); real
#   cell-to-cell spread is only tens of millivolts, invisible against the
#   registry's full 2.5-5.0V range, so live min/max scaling is what
#   actually makes imbalance visible. Two instances added directly below
#   right_box in the same `right` column, using the already-existing
#   rz450e_signals.cell_voltage_keys()/temp_probe_keys() (96 and 16 keys) -
#   packed side='top' after right_box (which keeps expand=True/fill='both'
#   above them), so Tk reserves their fixed height first and right_box
#   absorbs whatever's left; DashboardWindow's own geometry is untouched
#   (still 1430x950). Each chart has its own live min/max/spread readout
#   label above it, since the auto-scaled bar heights alone can't be read
#   as absolute values. Verified with two smoke scripts against a real
#   App/DashboardWindow: fields render single-line and correctly (a long-
#   label line spot-checked directly), fake cell/temp data produces
#   correct live min/max/spread text, and a winfo_y()/winfo_height() probe
#   confirmed the temp-probe chart's bottom edge (774px) lands inside the
#   right column's allocated height (778px) with the window still exactly
#   950px tall - nothing clipped, nothing resized. All 10 tests/test_*.py
#   scripts still pass.
#
# Rev 49: two user corrections to Rev 48. (1) "the temp bar's are supose to
#   be in the left under the history data" - the 16-probe temp chart was
#   originally placed in the narrow right column alongside the 96-cell
#   chart, giving each bar only ~18px. gui/dashboard.py: moved it into
#   `scroll` (left_outer's VScrollFrame inner frame), appended right after
#   the SLIDERS loop that builds the main per-signal list - now spans that
#   list's full width (1001px, computed from the same SIGNAL_COL_W/
#   IN_COL_W/CONV_COL_W/OUT_COL_W/RANGE_COL_W column-width constants the
#   header row uses) instead of RIGHT_COL_W-16. The 96-cell chart stays in
#   the right column per the original request ("right below the live
#   data"), unchanged. (2) "the live text slowely scrowl back to the top
#   on its own" - both live-data Text widgets (gui/panels.py's
#   LiveMonitorPanel._refresh(), gui/dashboard.py's _write_right_column())
#   preserved scroll position across each delete+rewrite via
#   yview_moveto(fraction_before). Root cause: yview_moveto() rounds to
#   the nearest line, and with wrap='word' boxes whose field VALUES change
#   length tick to tick (a status string, "None" vs a number, etc.), the
#   number of wrapped DISPLAY lines shifts between refreshes even though
#   the logical field count never does - repeatedly round-tripping through
#   a fraction is lossy and drifted the visible position toward the top
#   over many refresh cycles. Fixed by anchoring to box.index('@0,0') (the
#   exact character index at the top-left corner) before the rewrite and
#   restoring via box.yview(that_index) after, instead of a fraction -
#   exact, not a rounded approximation. Isolated before/after test (a
#   throwaway Text widget with wrap='word' and per-cycle varying-length
#   content, mimicking the real panels): the OLD fraction approach drifted
#   -12 lines over 39 refresh cycles (i.e. crept back toward the top,
#   exactly the reported symptom); the NEW index approach held at 0 drift
#   every cycle. Re-verified the full app + Dashboard lifecycle and all 10
#   tests/test_*.py scripts still pass.
#
# Rev 50: two more corrections to Rev 49. (1) "cell voltages are also
#   supose to be on the left. side by side" - the 96-cell chart was still
#   in the right column; moved it into `scroll` (the left list) next to
#   the 16-probe chart, both inside a new `charts_row` Frame so they pack
#   side='left' next to each other instead of stacked - cell chart gets
#   717px, temp chart 260px (computed from the same left-list-width
#   constants as before, split so 96 bars still get meaningfully more room
#   than 16). The right column now has no bar charts left in it at all.
#   (2) "the data boxes look close. can we add all output data to the
#   rigth and all descriptions to the left of each window? its still
#   wraping incorectly. some data output is wraping" - root-caused this
#   time instead of guessing at wider constants again: Rev 48's
#   write_field(label_w=40, value_w=12) space-padding assumed a FIXED
#   character-count box width; whenever the real box (in actual pixels,
#   via MONO_FONT's real glyph width) was narrower than that guess, lines
#   that should fit were wrapping anyway, and when wider, label/value sat
#   closer together than the padding implied. gui/theme.py:
#   configure_field_tags(box) now also binds <Configure> to set a REAL Tk
#   right-aligned tab stop (`box.configure(tabs=f'{box.winfo_width()-10}
#   right')`), re-measured on every resize; write_field(box, label, value)
#   dropped label_w/value_w and just writes 'label\tvalue\n' - the value
#   is pixel-flush against the box's actual current right edge, and a line
#   only wraps when the label itself is genuinely too long for the real
#   box width, never as an artifact of a wrong guessed character count.
#   Confirmed a right-tab-stop actually behaves this way in this Tk build
#   via an isolated dlineinfo() probe (short/medium labels stayed on one
#   display row with the value flush right; only a deliberately very long
#   label wrapped) before wiring it in, then verified against the real
#   Dashboard: right_box's `tabs` option came back correctly measured
#   ((288, 'right') at its actual ~298px width), the main window's two
#   LiveMonitorPanel boxes each got their OWN independently-measured tab
#   stop matching their own pane width, and a dlineinfo() sweep of all 24
#   right-column fields found exactly 5 that wrap to a second display
#   row - all 5 are the genuinely long ones (the two ~60-char SOFT CUT
#   labels, two long Generated-signal labels, and the long charge-status
#   sentence), none of the normal ~19 short/medium fields. All 10
#   tests/test_*.py scripts still pass.
#
# Rev 51: user follow-up on the cell/temp bar charts: "lets scale the
#   battery and temp to be slightly more 'bar' in the scale so its bettery
#   bar graph" - both charts' height bumped 90 -> 150px in
#   DashboardWindow._build() (gui/dashboard.py) for a more pronounced
#   bar-graph look; temp probe chart's gap between bars also widened
#   1 -> 3px (16 bars in 260px has plenty of room to spare, unlike the 96
#   cell bars in 717px where widening the gap would eat noticeably into
#   each bar's width). Confirmed via smoke script against a real
#   App/DashboardWindow that both _MultiVBar instances report height=150
#   and render/scale correctly with fake live data; all 10
#   tests/test_*.py scripts still pass.
#
# Rev 52: user follow-up: "the hight was fine, but the min max cant be the
#   full scale, we need more below and above. lets say scale the battery
#   from 2.5-4.3 and scale the temp from 0 deg - max." Rev 48/51's
#   auto-scale-to-live-min/max design meant the tallest and shortest bar
#   ALWAYS touched the top/bottom of the chart, no matter how tight the
#   real spread was - no visual headroom to judge against, which is what
#   the user was pointing at. gui/dashboard.py: _MultiVBar gained a
#   scale=(lo, hi) param - a FIXED display range (same idea as _BarGauge's
#   own (lo, hi)) used for bar HEIGHT, while set_values() still returns
#   the actual LIVE (lo, hi) present that call so the readout label above
#   each chart keeps reporting real numbers, not the fixed scale. Wired up
#   as CELL_SCALE=(2.5, 4.3) (the user's own explicit numbers) and
#   TEMP_SCALE=(0.0, 160.0) (0F per the user's explicit floor; 160F reuses
#   rz450e_signals' OWN existing temp_01..temp_16 registry range rather
#   than inventing a new ceiling). A value outside its chart's fixed range
#   now clamps to full bar height in ERR red instead of silently rescaling
#   past it - same out-of-range convention _BarGauge already uses
#   elsewhere in this window. Both chart header labels now show the
#   configured scale text (e.g. "2.5-4.3V") instead of "live min/max
#   scaled". Verified with a smoke script against a real
#   App/DashboardWindow: fed a tight real-world cell-voltage spread
#   (3.600-3.618V) plus one deliberately out-of-range cell (4.5V) -
#   confirmed the readout label still shows the correct live max (4.500V),
#   a normal cell's bar height matches the fixed-scale math exactly
#   (91.9px for 3.603V against the 2.5-4.3V/150px scale, computed
#   independently and matched to within float rounding), and the
#   out-of-range cell draws at full height in ERR red (#ff6b6b). All 10
#   tests/test_*.py scripts still pass.
#
# Rev 53: user walked back Rev 52's hard-fixed cell/temp chart scale:
#   "lets auto scale to .5V below and .5V above the min max. but dont
#   jumparound to scale... same for the temps lets do 5 above and 5
#   below. then lets add a third data graph that shows the other temps we
#   arr reciving from the battery in the same wrow."
#   gui/dashboard.py: _MultiVBar's fixed `scale=` param replaced with
#   `auto_pad=` - the displayed range is now live min/max +/- a fixed
#   margin (CELL_PAD=0.5V, TEMP_PAD=5F, the user's own numbers), but
#   STICKY: it only ever expands, never shrinks, once real data is
#   flowing, and (after a first attempt still jittered under ordinary
#   CAN-noise-level fluctuation - caught by a smoke test that fed +/-0.2mV
#   noise and found the displayed edge still crept) each expansion snaps
#   OUT to the next multiple of auto_pad beyond what's needed rather than
#   the exact figure, which is what actually stops small readings from
#   re-triggering another expansion. Resets to unset the next time a
#   chart has no live data at all (a disconnect), so a fresh session
#   doesn't inherit a stale expanded range. Added a third chart in the
#   same charts_row: self.other_temp_bars, showing the 0x4A7 'Temp
#   extremes' message's pack-level temp_max/temp_min (Max/Min pack
#   temperature) - distinct from the 16 individual 0x4AA probe readings
#   the middle chart already shows, and the most literal read of "the
#   other temps we are receiving from the battery." Refactored the
#   per-tick refresh into one shared _refresh_bar_chart() helper (cell/
#   temp-probe/pack-temp-extremes all called it) instead of copying the
#   same read-state/format-label logic a third time. Verified via smoke
#   script against a real App/DashboardWindow: the display scale held
#   exactly steady (3.0-4.5V) across 10 same-data ticks AND 10 ticks of
#   +/-0.2mV noise, correctly expanded (to 3.0-6.0V, grid-snapped) only
#   when a value (5.2V) actually landed outside the current range, and
#   correctly reset to unset when fed all-stale data. All 10
#   tests/test_*.py scripts still pass.
#
# Rev 54: user request: "lets go ahead and make the scale adjustable with
#   a drop down. The voltage can be point one up to one volt or full
#   scale, and the temperature can be point one degree up to five degree
#   or full scale" (defaults corrected mid-request to 0.1V/1F, down from
#   the numbers used to size Rev 53's hardcoded CELL_PAD/TEMP_PAD).
#   gui/dashboard.py: _MultiVBar gained fixed_range= (a hard (lo, hi)
#   override, same mechanic as Rev 52's since-removed scale= param) and a
#   set_scale(auto_pad=, fixed_range=) method that also clears the sticky
#   auto-pad window so switching modes doesn't inherit a stale range from
#   whichever mode was active before. Each of the three charts got a
#   readonly ttk.Combobox next to its title: "Cell voltages" offers
#   ±0.1/±0.2/±0.5/±1.0V + Full Scale; "Temp probes" offers ±0.1/±0.5/±1/
#   ±2/±5F + Full Scale and drives BOTH temp charts (probes and pack
#   extremes) at once, since the user referred to "the temperature" as
#   one category. "Full scale" reuses rz450e_signals' own existing
#   per-cell/per-probe registry ranges (2.5-5.0V, -40 to 160F) rather than
#   inventing new numbers. Every Combobox got no_wheel() (gui/theme.py) -
#   they live inside the same VScrollFrame-scrolled left list as
#   everything else, so without it a mouse-wheel scroll over one would
#   silently change its selection instead of scrolling the page.
#   Also moved gui/panels.py's private _no_wheel() helper to gui/theme.py
#   as public no_wheel() (renamed all 7 existing call sites in panels.py)
#   so dashboard.py's new Comboboxes could reuse it instead of duplicating
#   the same handful of lines a second time.
#   Verified via smoke script against a real App/DashboardWindow: default
#   selections/pads matched (±0.1V, ±1F), switching the cell dropdown to
#   "Full scale" produced the exact expected bar height against the
#   2.5-5.0V range, switching it back to a pad value correctly cleared
#   fixed_range, and switching the temp dropdown to "Full scale" applied
#   to BOTH temp_bars and other_temp_bars in one action. All 10
#   tests/test_*.py scripts still pass.
#
# Rev 55: user request: "lets make the voltage scalibal down to .01" -
#   gui/dashboard.py's VOLT_PAD_OPTIONS extended from [0.1, 0.2, 0.5, 1.0]
#   to [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0] (added the finer steps
#   between 0.01 and 0.1 too, not just the single new endpoint, so the
#   dropdown has a sensible progression rather than a big jump from 0.01
#   straight to 0.1); default selection unchanged (±0.1V). Temp dropdown
#   untouched - only "the voltage" was asked for. Verified via smoke
#   script: selecting ±0.01V against a deliberately tight fake cell
#   spread (3.600-3.6035V) produced a correspondingly tight ~0.03V
#   displayed scale (3.59-3.62V), vs. the much wider range the default
#   ±0.1V pad would give the same data. All 10 tests/test_*.py scripts
#   still pass.
#
# Rev 56: user follow-up: "lets defualt to .01 and add slight less buffer
#   to the top and bottom." gui/dashboard.py: DEFAULT_CELL_PAD changed
#   0.1 -> 0.01 (default Combobox selection is now '±0.01V'; temp default
#   untouched at ±1F, only "the voltage" was mentioned again). Separately,
#   _MultiVBar.set_values()'s grid-snap expansion (Rev 53's fix for
#   scale-jitter-under-noise) was snapping to a grid the size of the FULL
#   auto_pad, which could add up to another whole pad of extra headroom on
#   top of what was actually requested (worst case ~2x pad total) - that
#   excess is exactly what "add slight less buffer" was pointing at.
#   Tightened the grid to auto_pad/4, capping the excess at ~25% of pad
#   (~1.25x pad total) instead, while keeping it comfortably above
#   realistic CAN-noise-level jitter for every pad size offered (smallest
#   is 0.01V -> grid=0.0025V, still ~12x the +/-0.2mV noise level the
#   original stability smoke test used). Verified via smoke script: fed a
#   tight fake cell spread (3.600-3.603V), confirmed the default selection
#   is now '±0.01V', and measured the actual buffer on both edges landing
#   between 1.0x and 1.25x the requested 0.01V pad (was up to 2x before) -
#   then re-ran the +/-0.2mV noise-stability check against the new
#   tighter grid and confirmed the displayed range still didn't move.
#   All 10 tests/test_*.py scripts still pass.
#
# Rev 57: user follow-up: "lets make each bar graph less tall. then give
#   even less padding. lets give it .05V to th etop and botom."
#   gui/dashboard.py: all three chart heights 150 -> 100px (new
#   CHART_HEIGHT constant, one place instead of three separate literals);
#   charts_row's own top/bottom margin trimmed (14,4) -> (10,2), and the
#   pady after each chart's min/max label dropped entirely so the bar
#   canvas packs directly under it. DEFAULT_CELL_PAD changed 0.01 -> 0.05
#   (temp default untouched at ±1F - only voltage numbers were given
#   again). Verified via smoke script: all three _MultiVBar instances
#   report height=100, the default cell-scale selection is now '±0.05V'
#   with auto_pad=0.05, the measured buffer on both edges of a fake tight
#   cell spread landed within the expected 1.0x-1.25x*0.05V range, and the
#   Dashboard window's actual on-screen height is still exactly 950px
#   (nothing forced a resize). All 10 tests/test_*.py scripts still pass.
#
# Rev 58: real bench log finding (minileaf_20260806_182106-charge-test-with-
#   low-set-points-on-v.trc, ac_cutoff_v bracketed to 3.64V per docs/15 B20):
#   "when the stop charging cutoff was triggered, it actually didn't stop...
#   it just barely triggered it then reset for charging again." Decoding
#   every real 0x1DB frame's full_charge_flag bit confirmed the bug: the
#   flag pulsed on/off 29 times over the session, ~0.6s per pulse, instead of
#   ever actually stopping the session. Root cause - bridge/
#   management_engine.py's AC stop-charging cutoff (both the ac_cutoff_v
#   per-cell trigger and the SoC-target-reached stop) recomputed full_charge_
#   flag fresh every tick with no memory: the instant charger_limit_kw hit 0,
#   the triggering cell's voltage sagged back under ac_cutoff_v within a tick
#   or two, and the flag fell back to 0, re-offering power - a repeating hunt
#   that never gave the real Leaf VCM time to actually react and end the
#   session. Fixed by latching the stop once triggered (new
#   ManagementEngine._ac_charge_stop_latched, same mechanism as the existing
#   _hard_latched): once either trigger fires, full_charge_flag/charge_limit_
#   kw=0/charger_limit_kw=-10 stay asserted on every subsequent tick
#   regardless of the triggering reading recovering, cleared only via
#   notify_session_start()/notify_charge_replug() - i.e. the car must
#   genuinely shut down and the plug be reinserted before charging can
#   resume, per user directive. New regression test (tests/
#   test_management_engine.py: test_ac_stop_charging_cutoff_latches_once_
#   triggered) replays the exact recover-then-hunt sequence and confirms the
#   latch holds across 20+ subsequent ticks at a fully-recovered voltage,
#   clearing only on notify_charge_replug(). All tests/test_*.py scripts
#   still pass.
#
# Rev 59: user question following up on Rev 58 - "we split the charger and
#   drive mode for what controls the max power for charger. i do beleave in
#   the refrance project, that pramiter was controlled diffrently when in
#   drive mode... 2 different ways of controlling the output. one under
#   driving and one under charging, those are handeld diffrently by
#   diffrent algarithoms... Per-cell regen power liment for driving and AC
#   charger overvoltage taper for ac charging. thats a difrent control
#   means all togther." Confirmed against Refrance/Leaf_BMS_Emulator's
#   bit-level-diff-confirmed real-hardware notes: LB_MAX_POWER_FOR_CHARGER
#   sits fixed at the 1023/92.3kW idle placeholder whenever not actually
#   charging - it never becomes a live, per-cell-voltage-managed value
#   outside a real charge session. bridge/management_engine.py's
#   ac_charge_taper block (both the ac_full_v/ac_min_v/ac_min_kw taper AND
#   its own ac_emergency_v hard cut) previously ran every tick regardless of
#   charging_active (RZ450e's charge_permission_input) - only the
#   ac_cutoff_v stop-charging/full_charge_flag portion was gated. Fixed:
#   the whole block is now gated on charging_active - while not charging,
#   charger_limit_kw is left completely untouched (whatever mapping/idle
#   value was already there), and driving-mode overvoltage protection is
#   entirely charge_target_taper's job (the regen taper, unconditional by
#   design - drives the shared charge_limit_kw field for both regen-while-
#   driving and charge-while-plugged-in, per docs/03). Also relaxed
#   _CHARGE_EMULATION_SANITY_CHECKS' ac_min_v/ac_cutoff_v pair from strict
#   `<` to `<=` (user: "i wanted to trigger full charge cutoff at the same
#   time we reached min charger taper. thats why i had them set the same
#   value" - a deliberate valid config, not an inverted ordering; the
#   `config_sanity` warning was a false positive for this setup). New
#   regression tests: tests/test_management_engine.py
#   test_ac_taper_leaves_charger_limit_kw_untouched_while_driving (new);
#   tests/test_charge_ramp.py's two "applies even without the interlock"
#   tests renamed/reversed to test_charger_limit_kw_untouched_by_ac_taper_*
#   (they asserted the OLD, now-removed behavior). Six existing AC-taper
#   convergence tests in test_management_engine.py updated to explicitly set
#   charge_permission_input=1 (now required precondition), with three of
#   them moved off cell_v=4.18 (collides with the ac_cutoff_v=4.18 default,
#   which now correctly fires once charging_active is set) to 4.16. All
#   tests/test_*.py scripts still pass.
#
# Rev 60: docs/16 parameter-clamping audit follow-up - two fixes.
#   (1) discharge_min_kw/discharge_max_kw (discharge_power_taper) and
#   regen_min_kw/regen_max_kw (charge_target_taper) - user directive: "both
#   need min and max settings by the user", same pattern as charger_limit_kw's
#   existing ac_min_kw/ac_max_kw. Formula changed from `baseline * factor`
#   (true zero floor, unbounded ceiling) to `floor + (ceiling - floor) *
#   factor` where `ceiling = max(floor, min(baseline, max_kw))` (the max()
#   guard protects against a misconfigured max_kw < min_kw or a baseline
#   naturally below the floor). Defaults (0.0/110.0, 0.0/70.0) are exact
#   backward-compat no-ops. discharge_max_kw=110.0 is grounded in
#   docs/12-nmc-bms-design-research.md §6's researched "Leaf drive power
#   80-110 kW peak" figure; regen_max_kw=70.0 is explicitly NOT grounded in
#   that same section's "~0.5C/a few tens of kW" regen figure (~36kW) - kept
#   at the pre-existing static default deliberately (user directive: ship
#   with no behavior change first, tune down later). SAFETY-CRITICAL DETAIL:
#   charge_target_taper's emergency branch (worst cell >= emergency_high_v)
#   still outputs literal 0.0, bypassing regen_min_kw entirely - a floor must
#   never keep feeding power into an overvoltage emergency, matching
#   ac_charge_taper's own emergency branch (which likewise bypasses
#   ac_min_kw). New tests: test_discharge_power_taper_respects_configured_
#   floor_and_ceiling, test_charge_target_taper_regen_respects_configured_
#   floor_and_ceiling, test_charge_target_taper_emergency_bypasses_regen_min_
#   kw_floor, test_discharge_regen_min_max_sanity_checks (all in
#   tests/test_management_engine.py).
#   (2) GIDS/QC capacity formula fix (mapping_engine.derive_capacity_
#   outputs()) - the OLD formula computed gids/qc_full_wh/qc_remain_wh from
#   GROSS pack capacity (capacity_ah x live pack_v), conflating it with
#   USABLE capacity - overstated GIDs ~12.5% for a real pack with a top/
#   bottom reserve buffer (this project's bench pack: ~72kWh gross/~64kWh
#   usable). New state.vehicle fields usable_capacity_kwh (default 64.0,
#   spec-sourced, matches docs/12 §6's "documented pack figure ~71.4 kWh")
#   and qc_max_soc_pct (default 80.0, PROVISIONAL - no DC fast-charge
#   testing done at all yet) now drive the formula: gids = usable_wh/80
#   where usable_wh = soc_pct% x (usable_capacity_kwh x soh_fraction);
#   qc_full_wh capped at qc_max_soc_pct% of that usable capacity (real DC
#   fast charging only usefully charges to ~80% before CC-CV tapering makes
#   the rest pointless); qc_remain_wh = max(0, qc_full_wh - usable_wh).
#   pack_v dropped from the formula entirely (no longer used). Reproduces
#   the user's own by-hand worked example EXACTLY: 64kWh usable x 94% SOH =
#   60.16kWh = 752 GIDs at 100% SOC (tests/test_mapping_engine.py:
#   test_gids_qc_capacity_uses_usable_capacity_not_gross). Both new fields
#   validated on profile load via new mapping_engine.VEHICLE_FIELD_BOUNDS +
#   config_profile._apply_vehicle() (closes a real pre-existing gap -
#   state.vehicle previously had ZERO profile-load validation at all) and
#   editable in gui/panels.py's VehiclePanel (new Entry+bounds-clamp rows,
#   same pattern ManagementPanel already uses). New tests:
#   test_gids_qc_capacity_uses_usable_capacity_not_gross,
#   test_gids_qc_capacity_defaults_without_explicit_vehicle_config,
#   test_gids_qc_capacity_missing_inputs_returns_empty
#   (tests/test_mapping_engine.py); test_vehicle_profile_load_* (4 tests,
#   tests/test_config_profile.py). docs/10-open-questions.md #3 (GIDS
#   VCM-threshold interaction) re-checked against the new formula
#   (back-of-envelope: GIDS~=49/5 now fall at ~6.5%/~0.66% SoC, close to the
#   old formula's ~6.0%/~0.6% - not meaningfully changed) - remains open,
#   still needs a real voltage-based check, not just this SoC%-based sanity
#   pass. All tests/test_*.py scripts still pass; GUI smoke-tested (App()
#   builds with no exceptions, new fields render).
#
# Rev 61: same-day follow-up to Rev 60 - user asked "where is the GIDs, is
#   that a mapping or a hardcoded value... we should have that as a map or
#   configurable setting," then "soh_fraction could be configured" and "the
#   80% QC needs to be on the charge emulation and so does the DC fast
#   charge power request placeholder." Two changes:
#   (1) `mapping_engine.NAMEPLATE_CAPACITY_AH` (201.00 Ah, the divisor behind
#   the GIDS formula's SOH fraction) was a hardcoded module constant - now
#   also a live, user-configurable `state.vehicle['nameplate_capacity_ah']`
#   (new field, same default, editable on the Vehicle panel next to
#   usable_capacity_kwh). The constant remains as the fallback default and is
#   still what the separate `soh_pct` mapping tie's own `nameplate_ah`
#   tie-param bakes in at tie-creation time - deliberately NOT unified with
#   this new live value (two independently-tunable "nameplate capacity"
#   numbers for now; unifying them would mean plumbing live state access into
#   evaluate_combine()'s pure-function signature, out of scope). GIDS itself
#   stays a fixed named derivation (mapping_engine.derive_capacity_outputs()),
#   not a generic Signal Mapping tie - matches this project's "curated
#   features, no generic scripting" rule; every real number feeding the
#   formula is configurable even though the formula shape itself isn't.
#   (2) `qc_max_soc_pct` moved OUT of `state.vehicle` (where it briefly lived
#   for about a day, Rev 60) INTO `state.charge_emulation`
#   (leaf_signals.CHARGE_SLIDERS) - it's charging behavior, not a pack spec.
#   GUI-wise, also moved further: `dc_min_kw`/`dc_max_kw` previously rendered
#   on the separate "Future Placeholder" tab (not the actual Charge Emulation
#   tab) even though their data already lived in charge_emulation - user
#   directive: move all three (qc_max_soc_pct + both DC fields) onto the real
#   Charge Emulation tab, in a new "DC fast-charge / QC capacity" section.
#   FuturePlaceholderPanel simplified back to just the disabled PID/DID
#   placeholder it originally was. New one-time migration in
#   config_profile.apply_profile() (same pattern as the pre-existing
#   ac_zero_v -> ac_min_v migration) carries a legacy vehicle.qc_max_soc_pct
#   value across into charge_emulation.qc_max_soc_pct on load, so a profile
#   saved during the brief Rev-60 window doesn't silently lose a real tuned
#   value. New/updated tests: test_nameplate_capacity_ah_is_configurable,
#   test_qc_max_soc_pct_lives_in_charge_emulation_and_is_configurable
#   (tests/test_mapping_engine.py); test_qc_max_soc_pct_profile_load_clamps_
#   out_of_bounds, test_qc_max_soc_pct_migrates_from_old_vehicle_section,
#   test_qc_max_soc_pct_migration_does_not_override_an_explicit_new_value,
#   plus the existing vehicle round-trip/clamp tests updated for the field
#   rename (tests/test_config_profile.py). All tests/test_*.py scripts still
#   pass; GUI smoke-tested (App() builds with no exceptions).
#
# Rev 62: two GUI polish fixes from real usage, same day as Rev 61's new
#   fields. (1) User report: "charge emulation needs a scroll" - the new
#   'DC fast-charge / QC capacity' section (Rev 61) pushed the Charge
#   Emulation tab's content past the visible tab area with no way to reach
#   it (confirmed: 591px of content in a 265px-tall visible area in a smoke
#   test). ChargeEmulationPanel now wraps its content in the same
#   VScrollFrame ManagementPanel already uses (gui/theme.py) - every child
#   widget reparented from `self` to `self.scroll.inner`. (2) User report:
#   "name plate capacity words are off the screen" - VehiclePanel sits in
#   the ~374px-wide right sidebar; the new 'Usable capacity'/'Nameplate
#   capacity' fields (Rev 61) were packed into ONE horizontal row (label +
#   Entry + flag x2), overflowing well past that width (confirmed
#   reqwidth dropped from overflowing to 296px after the fix). Split into
#   two separate rows, matching ManagementPanel's one-field-per-row layout.
#   Also, per user request ("update the ? for the new added items"): the
#   '?' help popups for discharge_power_taper/charge_target_taper
#   (Battery Management tab) now document their new min/max kW fields
#   (citing docs/12 section 6's researched discharge/regen power figures
#   and the regen emergency-bypasses-the-floor safety detail); VEHICLE_HELP
#   documents usable_capacity_kwh/nameplate_capacity_ah and the GIDS
#   formula; a new DC_QC_HELP + '?' button was added to the (previously
#   unexplained) 'DC fast-charge / QC capacity' section itself. All
#   tests/test_*.py scripts still pass; GUI smoke-tested (App() builds with
#   no exceptions, VehiclePanel/ChargeEmulationPanel widget dimensions
#   verified directly via winfo_reqwidth/reqheight).

# Rev 63: two fixes from a full audit of every '?' help popup and every
#   docs/10-open-questions.md item against the current code, plus one new
#   feature. (1) docs/10 item 9's parenthetical still said "vehicle.
#   qc_max_soc_pct" - that field moved to state.charge_emulation the same
#   day (docs/04's own note, mapping_engine.py:217) so the reference was
#   stale; corrected. (2) gui/panels.py's MAPPING_HELP enumerated the
#   Signal Mapping tab's combine-function dropdown as "linear, sum, average,
#   min, max, or soh_percent" but mapping_engine.COMBINE_TYPES has always
#   included a 7th, real, selectable option, 'lookup' (a step table,
#   evaluate_combine()) - the help text just never mentioned it; added.
#   (3) New feature (user request): Start Log now also opens a
#   '<name>_log_output.txt' companion text file next to the .trc capture
#   (gui/app.py's _start_log_output/_stop_log_output, wired into
#   _toggle_trc_log/_on_close) - never modifies the .trc file/format itself.
#   Written once at start: a full settings snapshot, same shape as a saved
#   profile (bridge/config_profile.py's new build_profile_dict(), factored
#   out of save_profile() so this doesn't also overwrite config/profile.json
#   on disk). From then on, _drain_log mirrors every Log-panel line into it
#   live (line-buffered) until Stop Log/app close - on-the-fly setting
#   changes already produce a log_fn line (ManagementPanel/
#   ChargeEmulationPanel), so those show up for free, per user directive,
#   with no separate change-tracking needed.
#
# Rev 64: two unrelated changes, same session (user request).
#   (1) Documentation-only: logged two real-hardware findings from the real
#   ZE1 40kWh Leaf test vehicle, no code/default changes. Discharge
#   (discharge_max_kw/discharge_min_kw): the real dash turtle (reduced-power)
#   icon comes on around a 40kW discharge_max_kw setting, and the car starts
#   turning off power systems below roughly 3kW discharge_min_kw - matches
#   why the user's own saved config/profile.json already runs 40.0/5.0kW
#   instead of the 110.0/0.0kW code default. Regen (regen_max_kw): the real
#   car's own regen ceiling sits around 40kW on this test vehicle, so any
#   regen_max_kw setting between the 70.0kW default and ~40kW is a no-op -
#   a Leaf limit, not a bridge setting; peak regen varies by Leaf generation
#   (1st-gen 24/30kWh ~20-30kW, 2nd-gen 40kWh just over 40kW, LEAF PLUS
#   62kWh ~60-80kW). Logged in docs/05's default-value table, docs/15 B7/B8,
#   docs/16 A2/A3, and both features' help text (gui/panels.py).
#   (2) Celsius conversion (user request: "make sure our system uses celcius
#   for all settings and readouts"). This project stored/displayed every
#   temperature in °F internally (bridge/rz450e_signals.py's decode_temp_
#   msg()/decode_temp_minmax() converted raw°C to °F on ingest; every
#   over_temperature_derate/temp_data_cross_check config field had an _f
#   suffix and an °F default value) despite every other doc already
#   presenting °C as the primary unit. Same physical thresholds throughout -
#   no safety-relevant number actually changed, only the unit/storage
#   representation:
#     - bridge/rz450e_signals.py: decode functions now emit raw°C directly
#       (no ×9/5+32 conversion); registry 'unit'/'range' and
#       PLAUSIBLE_RANGES converted to °C (-51.1..121.1°C, was -60..250°F).
#     - bridge/management_engine.py: every over_temperature_derate field
#       renamed _f->_c with its default re-expressed in °C (e.g.
#       emergency_temp_f=141.8 -> emergency_temp_c=61.0, exact - this is why
#       141.8°F was originally chosen, it's precisely 61°C); temp_data_
#       cross_check's max_delta_f=10.0 -> max_delta_c=5.6 (a DELTA converts
#       via ×5/9, no +/-32 offset, unlike an absolute reading).
#       FEATURE_FIELD_BOUNDS/_CONFIG_SANITY_CHECKS updated to match.
#       ManagementEngine.from_dict() migrates an old _f-keyed saved profile
#       to its new _c key with a real unit conversion (not just a rename),
#       same "translate, don't drop" discipline as config_profile.py's
#       existing ac_zero_v->ac_min_v migration - verified directly against
#       the real saved config/profile.json.
#     - bridge/mapping_engine.py: the temp_max(°F)->batt_temp_c(°C) default
#       tie is now an identity (temp_max already arrives in °C); temp_
#       segment_pct's window changed from 32-140°F to the equivalent 0-60°C.
#       config_profile.py migrates a saved profile's mapping-tie params too
#       (matched by fingerprint against the old formula) - this one was a
#       genuine correctness risk, not just cosmetic: an unmigrated old tie
#       would have applied an °F->°C-shaped formula a SECOND time to a value
#       already in °C, silently producing garbage.
#     - gui/panels.py: over_temperature_derate/temp_data_cross_check field
#       labels and help text now say °C throughout.
#     - gui/dashboard.py: temp bar-chart units/full-range/pad-option labels
#       now °C (TEMP_FULL_RANGE -40..71.1°C, was -40..160°F).
#     - tests/test_management_engine.py, tests/test_charge_ramp.py, tests/
#       check_ac_taper_log_replay.py: every hardcoded °F temp literal
#       converted to its °C equivalent (full suite re-verified passing,
#       except two pre-existing test_charge_ramp.py failures unrelated to
#       this change - see docs/05's 2026-08-07 ac_charge_taper driving-vs-
#       charging gating note, those two tests assert the OLD pre-2026-08-07
#       behavior and were already stale before this session).
#     - docs/05, 09, 10, 12 updated (field names/values); docs/13/14 left
#       as accurate historical record of what was true when written, same
#       discipline as every other past field rename in this project.

if __name__ == '__main__':
    app = App()
    app.mainloop()
