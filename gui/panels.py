"""Reusable GUI panels (docs/08-gui-design.md): adapter connections, live
value monitors, signal mapping, battery management, and generated-signal
checkboxes. Plain tkinter/ttk (no customtkinter - reverted 2026-07-31 per
user request, see gui/theme.py)."""
import tkinter as tk
from tkinter import ttk

from bridge import can_backend, leaf_signals, management_engine, rz450e_signals
from bridge.mapping_engine import MappingTie, COMBINE_TYPES, explain_tie, VEHICLE_FIELD_BOUNDS
from gui.info_popup import show_info_popup, help_btn
from gui.theme import (VScrollFrame, MONO_FONT, FG, FIELD, FG_DIM, ERR, OK,
                       configure_field_tags, write_field, write_section, no_wheel)

REFRESH_MS = 400

# ── Contextual help text, ported style from leaf_hvbat_emulator.py's HELP
# dict / _help_btn pattern - every panel gets a '?' explaining what it does
# and citing the doc it's grounded in, not just the mapping tab's live
# conversion-math popup. ────────────────────────────────────────────────────
RZ450E_CONN_HELP = (
    "RZ450e connection (docs/02, docs/08)\n\n"
    "This is a single combined connection - one PEAK PCAN-USB adapter carries every "
    "RZ450e CAN ID (both the main/diagnostic bus and the internal cell-monitoring "
    "bus), split internally by CAN ID rather than by physical bus, matching this "
    "project's real hardware wiring.\n\n"
    "'Listen only' puts the adapter in PCAN's Listen Only mode (no ACK, no "
    "transmit) - useful for a pure passive sniff, but the DID/PID polling (SoC, "
    "capacity, primary V/I) needs to actually transmit requests, so listen-only "
    "will stop those specific values from updating.\n\n"
    "Auto-reconnect: if the connection drops, this project retries every few "
    "seconds automatically - watch the Log panel for connect/disconnect/"
    "reconnect events."
)
LEAF_CONN_HELP = (
    "Leaf-facing CAN bus (docs/03, docs/07)\n\n"
    "The connection the bridge transmits real Leaf HVBAT frames on, at the exact "
    "periods the Leaf's VCM/BCM expects (docs/06's real-time engine) - never put "
    "in listen-only mode since it must transmit.\n\n"
    "Auto-reconnect: if the connection drops, this project retries every few "
    "seconds automatically - watch the Log panel for connect/disconnect/"
    "reconnect events."
)
BRIDGE_CONTROL_HELP = (
    "Start / Stop Bridge (docs/06, docs/07)\n\n"
    "RZ450e monitoring and mapping/threshold edits work regardless of this - "
    "connect the RZ450e adapter and build your mapping first, then press "
    "'Start Bridge' once everything's connected and ready.\n\n"
    "'Start Bridge' arms the sequencer to WAIT for real traffic on the Leaf-"
    "facing connection before anything is transmitted - matching how a real "
    "Leaf HVBAT module wakes up (docs/07). Nothing is sent until the Leaf "
    "bus actually shows activity.\n\n"
    "'Stop Bridge' halts transmission immediately (not the graceful staged "
    "wind-down - use 'Simulate power-down' for that while running). Use Stop "
    "when you want to edit mappings/thresholds and restart clean.\n\n"
    "Changes made in the Signal Mapping / Battery Management tabs take effect "
    "live, immediately, whether the bridge is running or not - there's no "
    "separate 'apply' step."
)
VEHICLE_HELP = (
    "Vehicle / battery generation (docs/03)\n\n"
    "Selects which Leaf HVBAT CAN IDs actually transmit:\n"
    "- Base 6 IDs (0x1DB/0x1DC/0x55B/0x5BC/0x59E/0x5C0) are always active.\n"
    "- 0x1C2 (LBC second-processor heartbeat) only if Battery = ZE1.\n"
    "- 0x5EB only if Battery = ZE1.\n"
    "- 0x1ED (62kWh charger-limit message) only if Battery = ZE1 AND kWh = 62 - "
    "and that field is UNVERIFIED upstream (no real 62kWh capture exists yet), "
    "see docs/10 open question #11.\n\n"
    "'Usable capacity (kWh)' / 'Nameplate capacity (Ah)' (added 2026-08-08, "
    "docs/04/docs/16) are the RZ450e SOURCE-pack capacity spec feeding the "
    "GIDS/QC capacity formula (0x5BC remaining capacity, 0x59E QC full/"
    "remaining capacity) - a genuine pack spec, not Leaf-side generation "
    "selection like the three fields above, but placed here since it's the "
    "closest existing 'pack spec' surface. Formula: soh_fraction = measured "
    "capacity_ah / nameplate_capacity_ah; usable energy at current SOH = "
    "usable_capacity_kwh x soh_fraction; gids = (SoC% x usable energy) / "
    "80Wh. Defaults (64.0kWh / 201.00Ah) are spec-sourced, not independently "
    "bench-confirmed for this exact pack's buffer. The QC ceiling (how much "
    "of that usable energy the display counts as 'full') is a separate "
    "field, 'QC max SOC %' on the Charge Emulation tab, not here."
)
MAPPING_HELP = (
    "Signal Mapping (docs/04)\n\n"
    "Each card ties one or more RZ450e input signals to one Leaf output signal "
    "through a fixed preset combine function - linear (scale+offset), sum, "
    "average, min, max, lookup (a step table), or the soh_percent formula. No generic scripting is "
    "supported by design (docs/05's philosophy), so every tie stays portable to "
    "a future STM32 firmware port.\n\n"
    "Every dropdown entry is prefixed with its source CAN ID or DID (e.g. "
    "[0x020] Pack voltage, [DID 0x1F5B] State of charge) so it's clear which "
    "message a value comes from.\n\n"
    "Click the '?' on any individual tie to see the live input value(s), the "
    "conversion applied, and the resulting output value."
)
GENERATED_HELP = (
    "Generated Signals (docs/03)\n\n"
    "These are opaque replay tables/counters the real Leaf battery/BCM sends that "
    "have no confirmed real-world meaning to map from RZ450e data (PRUN counter, "
    "voltage-latch toggle, the 0x1C2 heartbeat, and the 0x1DC/0x5BC/0x5C0/0x5EB "
    "mux/cycle tables) - generated internally exactly as the Leaf project's own "
    "emulator does, never a mapping target.\n\n"
    "Each checkbox controls whether that signal is actually sent (default: "
    "checked/sent) - kept visible here, rather than hidden, so nothing is missed "
    "or forgotten when this config is later ported to STM32 firmware (docs/09)."
)
CHARGE_EMULATION_HELP = (
    "Charge-request ramp emulation (docs/06, added 2026-07-31)\n\n"
    "Ported from Leaf_BMS_Emulator, confirmed there against real hardware (bit-"
    "level diff of every HVBAT ID, idle vs. real charge-session captures): "
    "requires BOTH triggers at once (user directive, 2026-07-31) - a real 0x1F2 "
    "charge request from the Leaf (plugged in, asking to charge) AND RZ450e's own "
    "'Charge permission' interlock (0x358) actually granting it. With both present "
    "and this enabled, 'Max power for charger' (0x1DC) stops being a static number "
    "and instead SNAPS from its 1023 idle placeholder to 0.0 kW, then ramps up to "
    "'Charger ramp target (kW)' at a rate set by 'Uprate level' - level 7 = 2.0 "
    "kW/s, each level down halves the rate. The uprate bits (0x1DC byte 4 bits "
    "5-7) ride along with it, 0 -> level. The target itself is clamped into "
    "'AC charge minimum/maximum power request (kW)' below (default 0.5-6.6kW, "
    "6.6kW being the Leaf's real onboard AC charger ceiling) before ramping - "
    "and, as of 2026-08-06, the ramp rate-limits a LOWERED target exactly the "
    "same way it always has a raised one, for real fine (100W-scale) control "
    "in either direction instead of an instant jump.\n\n"
    "If the Leaf is asking to charge but RZ450e has NOT granted permission, this "
    "forces an explicit charger STOP instead of falling back to a static value: "
    "full_charge_flag is set (the confirmed real-hardware 'instant charge stop + "
    "contactor drop, needs a physical replug' bit), charge_limit_kw goes to 0.0, "
    "and charger_limit_kw goes to its -10.0 raw-idle value. The same mismatch also "
    "lets the bridge wind down and go back to sleep rather than staying awake "
    "indefinitely just because the Leaf keeps asking - a genuine replug is what "
    "produces a fresh 0x1F2 request.\n\n"
    "Once the charge request genuinely ends (both triggers drop), the ramp and "
    "uprate snap back to idle instantly, matching a real onboard charger's own "
    "power-negotiation curve on the bus rather than advertising a fixed ceiling "
    "the whole time.\n\n"
    "The per-cell overvoltage taper (Battery Management -> 'Per-cell charge/"
    "regen power limit') still gets the final say over the ramped value every "
    "tick - it can reduce it further, it is never bypassed by this feature.\n\n"
    "When this is OFF (default), 'Max power for charger' just uses whatever the "
    "Signal Mapping tab (or the DEFAULTS placeholder) already produces, as before "
    "this feature existed."
)
AC_CHARGE_TAPER_HELP = (
    "AC charger overvoltage taper (docs/05, added 2026-08-01, reworked 2026-08-06)\n\n"
    "Split out of Battery Management's per-cell charge/regen taper (user "
    "directive: AC charging via the Leaf's onboard charger, ~19A/0.09C, is "
    "physically very different from regenerative braking, up to ~0.5C into "
    "the pack - so each gets its own independently-tunable curve instead of "
    "sharing one). Drives ONLY charger_limit_kw ('Max power for charger,' "
    "docs/03) through FOUR tiers, in order: full power at/below 'Full power "
    "below'; ramping down to, then HOLDING at, the 'AC charge minimum power "
    "request (kW)' floor once voltage reaches 'Minimum power at/above' "
    "(renamed from a true zero-power point 2026-08-06 - the vehicle no "
    "longer has to react to a near-zero request to actually stop); crossing "
    "'Stop-charging cutoff V' deliberately ENDS the session (full_charge_flag "
    "+ charge/charger_limit_kw forced to their stop values), same as the "
    "target-SoC-reached stop below; 'Emergency high V' is a second, more "
    "extreme per-cell threshold above all of that, which escalates to a HARD "
    "CUT instead.\n\n"
    "Convergence toward that target is GENTLE, not instant (reworked "
    "2026-08-06 after a real bench test showed this taper hunting - a "
    "repeating full-cycle oscillation, not just a rough step - because "
    "instant response in either direction is the wrong model for a CC-CV "
    "charging control loop, unlike the discharge/regen tapers which "
    "genuinely do need to react instantly to real cell sag under load): it "
    "dynamically self-selects one of the existing 0-7 uprate levels based "
    "on how far it still has to move - always starts at level 7 (fastest) "
    "the moment it needs to converge, then downshifts (and upshifts back, "
    "symmetrically) as the remaining distance narrows or grows, gentler "
    "the closer it gets to the target. The level actually chosen is what's "
    "transmitted in 0x1DC's own uprate bits while this is actively "
    "converging (visible in this feature's live status text below, e.g. "
    "'target=X.XXkW, applied=X.XXkW (level N)') - overriding the manual "
    "'Uprate level' setting above only during that window, never outside "
    "it.\n\n"
    "'Daily target %' / 'Extended target %' and full_charge_flag are a "
    "separate concern, gated on RZ450e's charge_permission_input (charging "
    "interlock) being active - only fires during an actual plugged-in charge "
    "session, never from simply driving above the target SoC. Toggle "
    "'Extended mode' for a road-trip charge to the extended target instead "
    "of the daily one."
)
REQUIRE_LIVE_DATA_HELP = (
    "Require live data to charge (docs/13 item 13.1b, added 2026-08-03)\n\n"
    "Driving is allowed to run on cached/last-known-good values until the "
    "general staleness watchdog (Battery Management tab, 60s soft cut / +5s "
    "hard escalation) would object. Charging must NOT start on cached or "
    "default values at all - when enabled (recommended), the ramp will not "
    "start unless EVERY ONE of the 96 per-cell voltages plus the pack's max/"
    "min temperature has been seen LIVE at least once this session - not a "
    "pack-level summary, all 96 individually, and not from the startup cache. "
    "Blocked the same way as an RZ450e permission mismatch: full_charge_flag "
    "set, charger ramp held at 0, needs a fresh charge request to try again "
    "once live data has actually arrived.\n\n"
    "This is a ONE-TIME startup gate, not an ongoing freshness timer - once "
    "live data has arrived and the ramp is running, protection against data "
    "going stale later is the SAME 60s/+5s watchdog driving gets (which also "
    "sets full_charge_flag when it fires), not a separate/stricter timer. "
    "Per user directive: charging gets a different STARTUP requirement than "
    "driving, not different ongoing protection."
)
DC_QC_HELP = (
    "DC fast-charge / QC capacity (added 2026-08-06, moved here + 'QC max SOC "
    "%' added 2026-08-08)\n\n"
    "'DC minimum/maximum power request (kW)' are a PLACEHOLDER ONLY - not "
    "read by any active ramp/taper logic today (docs/10 open question #9: "
    "the RZ450e pack is rated for real 150kW/~430A DC fast charging, "
    "separate from the Leaf's 6.6kW onboard AC path this bridge actually "
    "emulates - that support isn't built yet). These fields exist purely so "
    "the config schema/GUI has a home for them ahead of DC charging support "
    "actually being built.\n\n"
    "'QC max SOC %' IS live, unlike the two placeholders above it - it caps "
    "the GIDS/QC capacity formula (Vehicle panel, see its own '?') at this "
    "percentage: qc_full_wh = usable energy at this SOC%, qc_remain_wh = "
    "however much of that is still ahead of current SoC (0 once already "
    "past it). Default 80% reflects that real DC fast charging only "
    "usefully charges to roughly that point before CC-CV tapering makes the "
    "rest pointless - PROVISIONAL, not yet tested against a real DC fast-"
    "charge session (no DC testing has been done on this project at all "
    "yet), same 'documented, not confirmed' status as the placeholder "
    "fields above it and temp_segment_pct's own mapping tie."
)
MANAGEMENT_FEATURE_HELP = {
    'low_voltage_cutoff': (
        "Low-voltage cutoff, cell-voltage authoritative (docs/05, corrected 2026-07-31)\n\n"
        "SOFT CUT - sets capacity_empty (0x55B) when the worst individual RZ450e "
        "cell voltage drops to/below 'Min cell V'. This is the gentle depleted-"
        "battery cutoff on the real Leaf: no RED dash error, and the contactor "
        "re-closes once the brake pedal and start button are pressed again with "
        "the flag cleared.\n\n"
        "SAFETY FIX (2026-07-31): 'Min SoC %' used to be OR'd in as an equal, "
        "independent trigger - a low SoC reading alone could cut off the pack "
        "even with every cell perfectly healthy. Per user directive, real-time "
        "per-cell voltage is now the SOLE authoritative trigger for every safety "
        "cutoff. SoC is a BACKUP CHECK ONLY: it's still evaluated every tick and "
        "shown in the status text (confirms when it agrees with the voltage-"
        "based decision, warns when it doesn't - e.g. a possible SoC calibration "
        "issue) but it never fires the cutoff by itself.\n\n"
        "'Emergency low V' is a second, more extreme per-cell threshold that "
        "escalates to a HARD CUT (relay_cut_request + interlock drop, RED "
        "'service EV system' dash error) - reserved for a genuine emergency, not "
        "routine low-battery behavior.\n\n"
        "'Soft cut persistence' (added 2026-07-31, docs/12 research pass): the "
        "worst-cell condition must hold continuously for this many seconds "
        "before capacity_empty actually latches - a guard against a single-"
        "tick voltage sag under a spike load (internal resistance roughly "
        "doubles on a cold pack) tripping the cutoff when the cell would have "
        "recovered the instant load dropped. The discharge power taper is "
        "already collapsing current/sag on its own faster ramp by the time "
        "this window would matter in the normal case - this is a backstop, "
        "not the primary defense. The emergency tier above stays instantaneous."
    ),
    'discharge_power_taper': (
        "Discharge power taper (docs/05, corrected 2026-07-31)\n\n"
        "Driven by individual cell voltage, not SoC - a single weak or "
        "imbalanced cell can sag well under heavy discharge load before "
        "pack-average SoC would suggest a problem, same reasoning as the "
        "charge/regen taper. Full discharge power limit at/above 'Taper "
        "start V', ramping linearly down to zero at/below 'Taper zero V' - "
        "by default the zero point matches the low-voltage cutoff's soft-cut "
        "floor, so power reaches zero right as capacity_empty engages, a "
        "smooth transition instead of full-power-then-sudden-stop.\n\n"
        "HYSTERESIS (user-specified): fast to respond to a dip - the applied "
        "power limit snaps down immediately, cell protection can't wait for "
        "a slow ramp - but slow to recover back to full power once voltage "
        "comes back up ('Recovery ramp (s)' = seconds to go from 0% back to "
        "100%). This avoids the power limit hunting/oscillating if cell "
        "voltage bounces near the threshold under intermittent acceleration.\n\n"
        "'Min discharge power (kW)' / 'Max discharge power (kW)' (added "
        "2026-08-08) bound where the taper actually ramps between - default "
        "0.0/110.0kW is an exact no-op (0.0 = the taper's pre-existing true-"
        "zero floor, 110.0 matches the researched Leaf drive-power peak, "
        "docs/12 section 6), same floor/ceiling pattern as the AC charger's "
        "min/max kW request bounds on the Charge Emulation tab.\n\n"
        "REAL-HARDWARE FINDING (2026-08-09, real ZE1 40kWh Leaf): the "
        "vehicle itself visibly reacts to 'Max discharge power (kW)' - "
        "around a 40kW setting the turtle (reduced-power) icon comes on the "
        "dash, and below roughly 3kW the car starts turning off power "
        "systems entirely. Both are the Leaf's OWN reaction to a low "
        "discharge_limit_kw request, not a bug in this taper - treat ~40kW "
        "as a practical ceiling before the dash starts warning the driver, "
        "and keep 'Min discharge power (kW)' comfortably above ~3kW so the "
        "taper's own floor never asks for a value the car reacts to that "
        "badly. Not yet swept for the exact real thresholds - documented "
        "starting point, not a confirmed number (docs/11)."
    ),
    'charge_target_taper': (
        "Per-cell REGEN power limit (docs/05, split 2026-08-01)\n\n"
        "charge_limit_kw is the Leaf's shared 'Charge/regen power limit' - the "
        "VCM uses it to cap BOTH regenerative braking while driving AND general "
        "charge acceptance, not just AC charging. This taper is driven ONLY by "
        "individual cell voltage, continuously, at ANY SoC and regardless of "
        "whether you're actually plugged in - it's what protects the pack "
        "during regen specifically, which can push up to ~0.5C into the pack, "
        "far more than the Leaf's ~0.09C AC charger ever could.\n\n"
        "PROACTIVE by design: the VCM is slow to respond to a charge_limit_kw "
        "change, so this can't wait until a cell is nearly at its limit - it "
        "has to start backing off well ahead of time. Full power at/below "
        "'Full regen below', ramping linearly down to zero at/above 'Zero regen "
        "at/above'.\n\n"
        "HYSTERESIS (added 2026-08-01, same pattern as the discharge power "
        "taper): fast to respond to a rise toward the ceiling, slow to relax "
        "back to full power once voltage recovers ('Recovery ramp (s)') - "
        "avoids power hunting if voltage bounces near the threshold.\n\n"
        "'Emergency high V' is a second, more extreme per-cell threshold "
        "(above the zero-regen point) that escalates to a HARD CUT - if a cell "
        "keeps climbing after regen is already at zero, something else is "
        "charging it.\n\n"
        "The AC-charger-specific taper (charger_limit_kw), plus the daily/"
        "extended SoC target and full_charge_flag, are now on the 'Charge "
        "Emulation' tab instead - AC charging (~19A/0.09C) is physically very "
        "different from regen, so it gets its own independently-tunable curve "
        "rather than sharing this one.\n\n"
        "'Min regen power (kW)' / 'Max regen power (kW)' (added 2026-08-08) "
        "bound where the taper actually ramps between - default 0.0/70.0kW "
        "is an exact no-op on rollout (0.0 = the taper's pre-existing true-"
        "zero floor; 70.0 matches the pre-existing static default, NOT the "
        "docs/12 section 6 researched ~0.5C/~36kW regen figure - kept "
        "unchanged deliberately so this ships with no behavior change, tune "
        "toward that research once ready). SAFETY: the emergency hard-cut "
        "branch above always outputs literal 0.0, ignoring this floor "
        "entirely - a floor must never keep feeding power into an "
        "overvoltage emergency.\n\n"
        "REAL-HARDWARE FINDING (2026-08-09): 'Min regen power (kW)' can be "
        "set all the way down to 0kW without issue - regen genuinely goes "
        "to zero cleanly. But 'Max regen power (kW)' only does something "
        "once it's set BELOW whatever the real Leaf pack/VCM can actually "
        "accept - on the project's real ZE1 40kWh test vehicle, changing "
        "this setting anywhere between 70kW and 40kW makes no observable "
        "difference at all, because the car's own regen ceiling sits around "
        "40kW - that's a LEAF LIMIT, not a setting this bridge controls. "
        "Peak regen capability varies a lot by Leaf generation/pack, so the "
        "useful range of this setting depends on which car it's driving:\n"
        "  - 1st-gen LEAF (24/30kWh): caps out around 20-30kW peak regen\n"
        "  - 2nd-gen LEAF (40kWh, this project's real test vehicle): just "
        "over 40kW peak regen\n"
        "  - LEAF PLUS (62kWh): can hit peak bursts of roughly 60-80kW under "
        "optimal conditions\n"
        "Set 'Max regen power (kW)' at or below the actual car's real "
        "ceiling for it to have any effect - anything above that ceiling is "
        "a no-op, the vehicle simply never asks for more than it can take. "
        "Not yet swept for the exact real ceiling on any specific car - "
        "documented starting point from real-hardware observation, not a "
        "confirmed number (docs/11)."
    ),
    'over_temperature_derate': (
        "Over-temperature derate (docs/05, restructured 2026-07-31 per docs/12 "
        "research pass - findings F1, F3, F6)\n\n"
        "HOT SIDE (both charge and discharge) is evaluated against the "
        "HOTTEST probe. Discharge power limit ramps from 'Discharge derate "
        "start' down to zero at 'Discharge hard stop' (a soft ramp only - see "
        "'Emergency temp' below for the actual hard-cut tier). Charge/regen "
        "acceptance ramps the same way between 'Charge derate start' and "
        "'Charge hard stop'.\n\n"
        "COLD SIDE (charge/regen acceptance only - discharge has no cold "
        "cutoff, since discharging doesn't plate lithium and tolerates a much "
        "wider range) is evaluated against the COLDEST probe, not the "
        "hottest. BUG FIX (2026-07-31): this used to test the hottest probe "
        "even for the cold-side block, which meant charging into a partly-"
        "frozen pack stayed allowed as long as the warmest corner read above "
        "freezing - lithium plating happens in the COLDEST cells. Charging is "
        "blocked entirely at/below 'Charge low block' °C and ramps up to full "
        "acceptance by 'Charge cold-derate start' °C - added because plating "
        "risk rises well above the freezing line at meaningful charge "
        "current; our real exposure here is regen into a cold-soaked pack "
        "(~0.5C), not the Leaf's 0.09C AC charger.\n\n"
        "'Emergency temp' (added 2026-07-31) is a genuine second, more "
        "extreme tier above 'Discharge hard stop' that escalates to a HARD "
        "CUT (relay_cut_request + interlock drop) - mirrors the two-tier "
        "soft/emergency structure the voltage features already have. "
        "Deliberately close above the soft stop: a cell with any plated "
        "lithium can begin self-heating as low as ~60°C, so there's "
        "very little real margin to work with.\n\n"
        "Defaults are researched general NMC/lithium-ion safety ranges "
        "cross-checked against this pack's own confirmed 3.6-4.2V/3.7V-nominal "
        "real-world range - not yet confirmed against a real thermal test on this "
        "specific pack (see docs/11's verification checklist). All temperature "
        "settings/readouts in this app are °C (converted from °F-primary "
        "storage 2026-08-09 - same physical thresholds, no behavior change)."
    ),
    'cell_imbalance_monitor': (
        "Cell imbalance monitor (added 2026-07-31, docs/12 research pass "
        "finding F4)\n\n"
        "MONITOR ONLY - never cuts or derates anything, just surfaces a "
        "warning in the status text. Watches the spread between the worst "
        "(highest) and worst (lowest) of all 96 individually read cell "
        "voltages and flags it once it reaches 'Warn spread'.\n\n"
        "This bridge can't balance cells - that's the RZ450e pack's own "
        "internal cell-supervision hardware, if it still operates once the "
        "pack is running in this configuration (open question, see docs/10). "
        "But we uniquely see all 96 cells at high rate, and a growing spread "
        "is the cheapest early-warning signal available: a cell resting "
        "30-50mV below its neighbors is a well-documented early signature of "
        "elevated self-discharge - i.e. a developing internal defect - before "
        "it becomes a bigger problem. A growing spread also silently shrinks "
        "the usable pack window over time (the highest cell hits the charge "
        "taper early, the lowest cell hits the discharge taper early)."
    ),
    'overcurrent_monitor': (
        "Overcurrent monitor (added 2026-07-31, docs/12 research pass "
        "finding F2)\n\n"
        "MONITOR ONLY - never cuts or derates anything, just surfaces a "
        "warning in the status text after sustained (not momentary) elevated "
        "current in either direction.\n\n"
        "Deliberately NOT wired to an active cutoff: there's no cell "
        "datasheet to source a real continuous/peak current limit from (Toyota "
        "doesn't publish one), and this project's own fast current sensor "
        "(0x023) saturates at +/-204.7A - below the Leaf's plausible ~230-"
        "315A peak drive current - so we can't even measure how far beyond "
        "that a real fault current would go. 'Discharge warn' and 'Charge/"
        "regen warn' are set from this project's own confirmed specs rather "
        "than an invented number: the discharge default sits comfortably "
        "below the sensor's saturation ceiling (so a warning still means "
        "something), and the charge/regen default sits above the Leaf's "
        "onboard AC charger's ~19A max so ordinary charging never trips it. "
        "'Persistence' avoids flagging a brief acceleration spike as if it "
        "were sustained overcurrent.\n\n"
        "Treat these as provisional/unconfirmed (see docs/11) until real "
        "drive-cycle current logging exists to set a value with actual "
        "confidence."
    ),
    'staleness_watchdog': (
        "Staleness watchdog (docs/06, expanded 2026-08-01)\n\n"
        "Tracks freshness of EVERY registered RZ450e input signal - all 96 "
        "per-cell voltages, all 16 temp probes, and every other fast/slow "
        "scalar - plus the RZ450e's own keep-alive/rolling counters (0x358/"
        "0x3F1 alive counters, 0x424 5s tick). If any signal that was live "
        "stops updating for 'Soft cut after' seconds, triggers a SOFT CUT "
        "(capacity_empty) AND explicitly stops charging (charge_limit_kw and "
        "charger_limit_kw forced to their stop values) - we can no longer "
        "verify it's safe to keep accepting charge/regen if the data behind "
        "that decision has gone stale. If still stale 'Hard cut escalation' "
        "seconds after that, escalates to a HARD CUT (relay_cut_request) - "
        "giving a transient CAN hiccup a brief window to self-clear before "
        "committing to the RED-error hard cut. A signal that has NEVER been "
        "seen this session doesn't count as stale (nothing to check yet) - "
        "only one that WAS live and then stopped updating does."
    ),
    'cell_data_cross_check': (
        "Cell data cross-check (docs/02, docs/04, added 2026-08-01)\n\n"
        "Live redundancy check between the 96 individually-read cell "
        "voltages (authoritative) and the 0x020 pack-level cell_min/"
        "cell_max summary - docs/02 and docs/04 both describe the pack "
        "summary as a 'sanity cross-check' against the per-cell messages, "
        "and this feature is what actually performs it.\n\n"
        "If the worst individual cell reading disagrees with the pack "
        "summary by 'Max delta' or more, continuously for 'Soft cut after' "
        "seconds, triggers a SOFT CUT - same soft-then-hard escalation "
        "STRUCTURE as the staleness watchdog, independently tunable from "
        "it. A mismatch this large usually means a decode problem or a "
        "genuinely unreliable reading, not a real physical condition, so "
        "this is a data-integrity check, not a voltage-level protection "
        "feature (that's low_voltage_cutoff/charge_target_taper's job)."
    ),
    'temp_data_cross_check': (
        "Temperature data cross-check (docs/02, added 2026-08-04, docs/13 item 16.2)\n\n"
        "Live redundancy check between the pack-level temperature extremes "
        "(0x4A7's temp_max/temp_min) and the actual min/max of all 16 "
        "individually-read temp probes (0x4AA) - the same pattern as the "
        "cell data cross-check above, applied to temperature.\n\n"
        "This matters because temp_min directly drives the cold-side charge-"
        "block/derate logic that protects against lithium plating - a "
        "decode or multiplexing fault could produce a temp_min/temp_max "
        "reading that's individually plausible but physically inconsistent "
        "with the 16 probes it's presumably derived from, with nothing else "
        "in this project able to catch it.\n\n"
        "If the pack-extremes summary disagrees with the individual probes "
        "by 'Max delta' or more, continuously for 'Soft cut after' seconds, "
        "triggers a SOFT CUT - same soft-then-hard escalation structure as "
        "the cell data cross-check and staleness watchdog, independently "
        "tunable from both."
    ),
    'input_validation': (
        "Input plausibility validation (docs/13 item 15.14, added 2026-08-03)\n\n"
        "Every RZ450e-decoded value is checked against a generous physical-"
        "plausibility range (rz450e_signals.PLAUSIBLE_RANGES - much wider "
        "than any actual safety threshold, meant only to catch obvious bus/"
        "decode garbage) before it's ever written into live state. A "
        "rejected value is simply dropped, not written - the field keeps "
        "aging under its last-good value instead, eventually caught by the "
        "staleness watchdog if the rejections keep happening.\n\n"
        "Default ON - this is a genuine safety net. Turning it off lets "
        "every decoded value through unfiltered regardless of how "
        "physically implausible it is, which is useful for deliberately "
        "testing how the rest of the pipeline handles a bad reading, but "
        "should not be left off during real operation."
    ),
    'checksum_validation': (
        "Toyota checksum validation (docs/02, docs/13 item 15.15, added 2026-08-03)\n\n"
        "The 5 RZ450e messages confirmed to carry Toyota's additive "
        "checksum (0x020/0x023/0x358/0x3F1/0x424) are checksum-verified "
        "before they're decoded at all - a mismatch (or a too-short frame) "
        "means the frame is corrupt and it's rejected outright, never "
        "handed to a decoder.\n\n"
        "Default ON - this is a genuine safety net. Turning it off lets "
        "every frame on these 5 IDs through for decoding regardless of "
        "whether its checksum is valid, which is useful for deliberately "
        "testing how the rest of the pipeline handles a corrupt frame, but "
        "should not be left off during real operation."
    ),
}

# Note: every class below stores the shared live-state model as
# `self.state_model`, never `self.state` - ttk.Widget already defines a real
# `.state()` method (widget state flags), and shadowing it with a plain
# attribute caused a real crash in the Dashboard window earlier - see
# main.py's rev 2 changelog.


class ConnectionsPanel(ttk.Frame):
    """Adapter selection (up to 8 PCAN channels), connect/disconnect, and
    auto-reconnect status for one logical bus role."""

    def __init__(self, master, title, bus_connection, allow_listen_only=True, default_listen_only=False,
                 help_text=None):
        super().__init__(master)
        self.bus = bus_connection
        head = ttk.Frame(self)
        head.pack(fill='x', padx=8, pady=(8, 2))
        ttk.Label(head, text=title, style='Header.TLabel').pack(side='left')
        if help_text:
            help_btn(head, title, help_text).pack(side='left', padx=(6, 0))

        row = ttk.Frame(self)
        row.pack(fill='x', padx=8, pady=2)
        self.channels = can_backend.detect_pcan_channels()
        self.channel_var = tk.StringVar(value=self.channels[0] if self.channels else '')
        self.channel_menu = ttk.Combobox(row, values=self.channels, textvariable=self.channel_var,
                                          width=20, state='readonly')
        no_wheel(self.channel_menu)
        self.channel_menu.pack(side='left', padx=(0, 4))
        ttk.Button(row, text='Rescan', width=8, style='Small.TButton', command=self._rescan).pack(side='left', padx=2)

        row2 = ttk.Frame(self)
        row2.pack(fill='x', padx=8, pady=2)
        self.listen_only_var = tk.BooleanVar(value=default_listen_only)
        if allow_listen_only:
            ttk.Checkbutton(row2, text='Listen only', variable=self.listen_only_var).pack(side='left', padx=(0, 8))
        self.connect_btn = ttk.Button(row2, text='Connect', width=11, command=self._toggle)
        self.connect_btn.pack(side='left')

        self.status_label = ttk.Label(self, text='Disconnected', foreground=ERR)
        self.status_label.pack(anchor='w', padx=8, pady=(2, 4))

        # Connection-health lights (added 2026-08-01, user request: "add a
        # dedicated CAN monitor... so we know the real state... add a
        # counter so we can see how many resets we have had"). TX OK is
        # tracked separately from Connected/RX, since a TX-only failure
        # (adapter won't send, bus-off) doesn't always flip `connected`
        # false too - see bridge/can_backend.py.
        health = ttk.Frame(self)
        health.pack(fill='x', padx=8, pady=(0, 8))
        self.tx_light = tk.Canvas(health, width=11, height=11, bg=FIELD, highlightthickness=0)
        self.tx_light_oval = self.tx_light.create_oval(1, 1, 10, 10, fill=FG_DIM, outline=FG_DIM)
        self.tx_light.pack(side='left', padx=(0, 4))
        self.health_label = ttk.Label(health, text='TX: -- | reconnects: 0 | TX errors: 0', foreground=FG_DIM)
        self.health_label.pack(side='left')

        self._refresh()

    def _rescan(self):
        self.channels = can_backend.detect_pcan_channels()
        self.channel_menu.configure(values=self.channels)

    def _toggle(self):
        if self.bus.connected:
            self.bus.disconnect()
        else:
            self.bus.connect(self.channel_var.get(), listen_only=self.listen_only_var.get())

    def _refresh(self):
        if not self.winfo_exists():
            return
        connected = self.bus.connected
        if connected:
            self.status_label.configure(text=f'Connected: {self.bus.channel}', foreground=OK)
            self.connect_btn.configure(text='Disconnect')
        else:
            err = f' ({self.bus.error})' if self.bus.error else ''
            self.status_label.configure(text=f'Disconnected{err}', foreground=ERR)
            self.connect_btn.configure(text='Connect')

        if connected:
            tx_ok = self.bus.tx_ok
            color = OK if tx_ok else ERR
            self.tx_light.itemconfig(self.tx_light_oval, fill=color, outline=color)
            tx_text = 'OK' if tx_ok else 'FAILING'
        else:
            self.tx_light.itemconfig(self.tx_light_oval, fill=FG_DIM, outline=FG_DIM)
            tx_text = '--'
        self.health_label.configure(
            text=f'TX: {tx_text} | reconnects: {self.bus.reconnect_count} | TX errors: {self.bus.send_errors}',
            foreground=ERR if connected and not self.bus.tx_ok else FG_DIM)

        self.after(REFRESH_MS, self._refresh)


class LiveMonitorPanel(ttk.Frame):
    """Read-only, periodically-refreshed grouped value dump. Text-block
    style (matches the Leaf project's DashWindow 'informative' feel) rather
    than one live-bound widget per signal, since there are 100+ input
    signals to display.

    `write_fn(box)` populates the (already-cleared) Text widget directly,
    using write_section()/write_field() (gui/theme.py) - each field is a
    label, then its value flush against the box's own right edge (a real
    Tk tab stop, re-measured on resize; see configure_field_tags())."""

    def __init__(self, master, title, write_fn):
        super().__init__(master)
        self.write_fn = write_fn
        ttk.Label(self, text=title, style='Header.TLabel').pack(anchor='w', padx=8, pady=(8, 2))
        self.box = tk.Text(self, wrap='word', bg=FIELD, fg=FG, insertbackground=FG,
                            relief='flat', state='disabled', font=MONO_FONT)
        self.box.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        configure_field_tags(self.box)
        self._refresh()

    def _refresh(self):
        if not self.winfo_exists():
            return
        # Scroll position preserved by INDEX, not yview() fraction (fixed
        # 2026-08-06, user report: "the live text slowely scrowl back to
        # the top on its own") - yview_moveto(fraction) rounds to the
        # nearest line internally, and re-deriving/reapplying that fraction
        # every ~400ms is a lossy round-trip that drifted the visible
        # position toward the top over many refreshes even though the
        # field count never changes. '@0,0' is the exact character index
        # under the widget's top-left corner; re-scrolling to that same
        # index after the rewrite is exact, not a rounded approximation.
        top_index = self.box.index('@0,0')
        self.box.configure(state='normal')
        self.box.delete('1.0', 'end')
        self.write_fn(self.box)
        self.box.configure(state='disabled')
        try:
            self.box.yview(top_index)
        except Exception:
            pass
        self.after(REFRESH_MS, self._refresh)


def write_input_monitor(box, state_model):
    groups = {}
    for sig in rz450e_signals.INPUT_SIGNALS:
        groups.setdefault(sig['group'], []).append(sig)
    for group, sigs in groups.items():
        write_section(box, group)
        for sig in sigs:
            key = sig['key']
            val = state_model.get_input(key)
            if val is None:
                write_field(box, sig['label'], '--')
                continue
            age = state_model.age_of(key)
            age_s = f'{age:.1f}s' if age is not None else '--'
            write_field(box, sig['label'], f'{val:.3f} {sig["unit"]} (age {age_s})')


def write_leaf_tx_monitor(box, state_model):
    tx = state_model.snapshot_leaf_tx()
    for group, sigs in leaf_signals.SLIDERS.items():
        write_section(box, group)
        for key, label, *_rest in sigs:
            write_field(box, label, repr(tx.get(key)))
    write_section(box, 'Flags')
    for key, label, _default in leaf_signals.CHECKS:
        write_field(box, label, repr(tx.get(key)))
    write_section(box, 'Management status')
    for feature, status in state_model.snapshot_management_status().items():
        write_field(box, feature, status)


class VehiclePanel(ttk.Frame):
    def __init__(self, master, state_model):
        super().__init__(master)
        self.state_model = state_model
        head = ttk.Frame(self)
        head.pack(fill='x', padx=8, pady=(8, 2))
        ttk.Label(head, text='Vehicle / battery generation', style='Header.TLabel').pack(side='left')
        help_btn(head, 'Vehicle / battery generation', VEHICLE_HELP).pack(side='left', padx=(6, 0))
        row = ttk.Frame(self)
        row.pack(fill='x', padx=8, pady=(0, 8))
        vehicle = state_model.snapshot_vehicle()
        self.car_gen = tk.StringVar(value=vehicle['car_gen'])
        self.batt_gen = tk.StringVar(value=vehicle['battery_gen'])
        self.batt_kwh = tk.StringVar(value=str(vehicle['battery_kwh']))
        ttk.Label(row, text='Car:').pack(side='left')
        car_combo = ttk.Combobox(row, values=['AZE0', 'ZE1'], textvariable=self.car_gen, width=6, state='readonly')
        car_combo.pack(side='left', padx=4)
        car_combo.bind('<<ComboboxSelected>>', lambda e: state_model.set_vehicle_item('car_gen', self.car_gen.get()))
        no_wheel(car_combo)
        ttk.Label(row, text='Battery:').pack(side='left')
        batt_combo = ttk.Combobox(row, values=['AZE0', 'ZE1'], textvariable=self.batt_gen, width=6, state='readonly')
        batt_combo.pack(side='left', padx=4)
        batt_combo.bind('<<ComboboxSelected>>', lambda e: state_model.set_vehicle_item('battery_gen', self.batt_gen.get()))
        no_wheel(batt_combo)
        ttk.Label(row, text='kWh:').pack(side='left')
        kwh_combo = ttk.Combobox(row, values=['30', '40', '62'], textvariable=self.batt_kwh, width=4, state='readonly')
        kwh_combo.pack(side='left', padx=4)
        kwh_combo.bind('<<ComboboxSelected>>', lambda e: state_model.set_vehicle_item('battery_kwh', int(self.batt_kwh.get())))
        no_wheel(kwh_combo)

        # RZ450e SOURCE-pack capacity spec (added 2026-08-08, docs/16 audit,
        # nameplate_capacity_ah split out same day per user follow-up) -
        # feeds mapping_engine.derive_capacity_outputs()'s GIDS formula. A
        # scope stretch for this panel (it's otherwise Leaf-side generation
        # selection), but the closest existing "vehicle/pack spec" surface -
        # see bridge/state.py's own comment on these vehicle dict keys.
        # qc_max_soc_pct deliberately does NOT live here - moved to
        # ChargeEmulationPanel (charging behavior, not a pack spec).
        # Own row per field (2026-08-08, user report: "nameplate capacity
        # words are off the screen" - this panel sits in the narrow right
        # sidebar column, ~374px wide; a single row with both label+Entry
        # pairs packed side-by-side overflowed it) - matches ManagementPanel's
        # one-field-per-row layout instead.
        cap_row = ttk.Frame(self)
        cap_row.pack(fill='x', padx=8, pady=(0, 2))
        ttk.Label(cap_row, text='Usable capacity (kWh):').pack(side='left')
        self.usable_kwh_var = tk.StringVar(value=str(vehicle['usable_capacity_kwh']))
        ttk.Entry(cap_row, textvariable=self.usable_kwh_var, width=8).pack(side='left', padx=4)
        cap_flag = ttk.Label(cap_row, text='', foreground=ERR, width=9)
        cap_flag.pack(side='left')
        self.usable_kwh_var.trace_add(
            'write', lambda *_a: self._set_vehicle_float('usable_capacity_kwh', self.usable_kwh_var, cap_flag))

        nameplate_row = ttk.Frame(self)
        nameplate_row.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(nameplate_row, text='Nameplate capacity (Ah):').pack(side='left')
        self.nameplate_ah_var = tk.StringVar(value=str(vehicle['nameplate_capacity_ah']))
        ttk.Entry(nameplate_row, textvariable=self.nameplate_ah_var, width=8).pack(side='left', padx=4)
        nameplate_flag = ttk.Label(nameplate_row, text='', foreground=ERR, width=9)
        nameplate_flag.pack(side='left')
        self.nameplate_ah_var.trace_add(
            'write', lambda *_a: self._set_vehicle_float(
                'nameplate_capacity_ah', self.nameplate_ah_var, nameplate_flag))

    def _set_vehicle_float(self, key, var, flag_lbl):
        """Same clamp-on-edit pattern as ManagementPanel._set_float, adapted
        to call state_model.set_vehicle_item() (the locked setter every other
        vehicle-dict edit in this panel already uses) instead of mutating a
        plain cfg dict directly."""
        try:
            raw = float(var.get())
        except ValueError:
            flag_lbl.configure(text='invalid')
            return
        lo, hi = VEHICLE_FIELD_BOUNDS[key]
        value = raw
        clamped = False
        if value < lo:
            value, clamped = lo, True
        elif value > hi:
            value, clamped = hi, True
        self.state_model.set_vehicle_item(key, value)
        flag_lbl.configure(text='clamped' if clamped else '')


class MappingPanel(ttk.Frame):
    """Signal Mapping tab: one row per tie, up to 3 input dropdowns (blank =
    unused) + combine selector + output dropdown + '?' popup + remove."""

    BLANK = '(unused)'
    # Orphan/rename warning (docs/13 item 4.3, added 2026-08-03): a tie can
    # reference an input/output key that no longer exists in the current
    # signal registry (a field got renamed, or a saved profile predates a
    # registry change) - previously that silently rendered exactly like a
    # genuinely-blank slot ("(unused)"), with no way to tell "nothing here"
    # apart from "something here that's now broken." This prefix marks that
    # case distinctly and keeps the actual stale key visible.
    ORPHAN_PREFIX = '(!) UNKNOWN KEY: '

    def __init__(self, master, state_model, mapping_engine):
        super().__init__(master)
        self.state_model = state_model
        self.mapping = mapping_engine

        # Display strings are prefixed with the source/target CAN ID or DID
        # (docs/08, item 7) so it's obvious which message a signal is from -
        # separate from the actual key used internally.
        self.input_display_to_key = {self.BLANK: ''}
        self.input_key_to_display = {'': self.BLANK}
        for sig in rz450e_signals.INPUT_SIGNALS:
            disp = f"[{sig['source']}] {sig['label']}"
            self.input_display_to_key[disp] = sig['key']
            self.input_key_to_display[sig['key']] = disp
        self.input_options = list(self.input_display_to_key.keys())

        self.output_display_to_key = {self.BLANK: ''}
        self.output_key_to_display = {'': self.BLANK}
        for sig in leaf_signals.OUTPUT_SIGNALS:
            disp = f"[{sig['source']}] {sig['label']}"
            self.output_display_to_key[disp] = sig['key']
            self.output_key_to_display[sig['key']] = disp
        self.output_options = list(self.output_display_to_key.keys())

        head = ttk.Frame(self)
        head.pack(fill='x', padx=4, pady=(4, 0))
        ttk.Label(head, text='Signal Mapping', style='Accent.TLabel').pack(side='left')
        help_btn(head, 'Signal Mapping', MAPPING_HELP).pack(side='left', padx=(6, 0))

        self.scroll = VScrollFrame(self)
        self.scroll.pack(fill='both', expand=True)
        self.rows = []
        self._rebuild()
        ttk.Button(self, text='+ Add mapping', command=self._add_row).pack(pady=8)

    def _rebuild(self):
        for w in self.rows:
            w.destroy()
        self.rows = []
        for idx, tie in enumerate(self.mapping.ties):
            self.rows.append(self._make_row(idx, tie))

    def _display_for_input(self, key):
        """Resolves an input key to its dropdown display string. An empty
        key is a genuine blank slot; a non-empty key not found in the
        registry is orphaned (item 4.3) - flagged distinctly and registered
        back into input_display_to_key so _update_tie() round-trips it to
        this exact key instead of silently dropping it to blank the next
        time any other field on the same row changes."""
        if not key:
            return self.BLANK
        disp = self.input_key_to_display.get(key)
        if disp is not None:
            return disp
        disp = f'{self.ORPHAN_PREFIX}{key}'
        self.input_display_to_key[disp] = key
        self.input_key_to_display[key] = disp
        return disp

    def _display_for_output(self, key):
        """Output-side counterpart of _display_for_input - see there."""
        if not key:
            return self.BLANK
        disp = self.output_key_to_display.get(key)
        if disp is not None:
            return disp
        disp = f'{self.ORPHAN_PREFIX}{key}'
        self.output_display_to_key[disp] = key
        self.output_key_to_display[key] = disp
        return disp

    @staticmethod
    def _is_orphan(display_text):
        return display_text.startswith(MappingPanel.ORPHAN_PREFIX)

    def _make_row(self, idx, tie):
        card = ttk.Frame(self.scroll.inner, relief='groove', borderwidth=1)
        card.pack(fill='x', pady=4, padx=2)

        line1 = ttk.Frame(card)
        line1.pack(fill='x', padx=6, pady=(6, 2))
        ttk.Label(line1, text='IN:', width=4).pack(side='left')
        vars_in = []
        combos_in = []
        for i in range(3):
            key = tie.inputs[i] if i < len(tie.inputs) else ''
            disp = self._display_for_input(key)
            v = tk.StringVar(value=disp)
            style = 'Warn.TCombobox' if self._is_orphan(disp) else 'TCombobox'
            combo = ttk.Combobox(line1, values=self.input_options, textvariable=v, width=32,
                                  state='readonly', style=style)
            combo.pack(side='left', padx=2)
            combo.bind('<<ComboboxSelected>>', lambda e, i=idx: self._update_tie(i))
            no_wheel(combo)
            vars_in.append(v)
            combos_in.append(combo)
        card._invars = vars_in
        card._in_combos = combos_in

        line2 = ttk.Frame(card)
        line2.pack(fill='x', padx=6, pady=(2, 6))
        ttk.Label(line2, text='->', width=3).pack(side='left')
        combine_var = tk.StringVar(value=tie.combine)
        combine_combo = ttk.Combobox(line2, values=list(COMBINE_TYPES), textvariable=combine_var, width=12, state='readonly')
        combine_combo.pack(side='left', padx=2)
        combine_combo.bind('<<ComboboxSelected>>', lambda e, i=idx: self._update_tie(i))
        no_wheel(combine_combo)
        card._combine_var = combine_var

        scale_var = tk.StringVar(value=str(tie.params.get('scale', 1.0)))
        offset_var = tk.StringVar(value=str(tie.params.get('offset', 0.0)))
        ttk.Entry(line2, textvariable=scale_var, width=7).pack(side='left', padx=1)
        ttk.Entry(line2, textvariable=offset_var, width=7).pack(side='left', padx=1)
        card._scale_var, card._offset_var = scale_var, offset_var
        for v in (scale_var, offset_var):
            v.trace_add('write', lambda *_a, i=idx: self._update_tie(i))

        ttk.Label(line2, text='OUT:', width=5).pack(side='left', padx=(6, 0))
        out_disp = self._display_for_output(tie.output)
        out_var = tk.StringVar(value=out_disp)
        out_style = 'Warn.TCombobox' if self._is_orphan(out_disp) else 'TCombobox'
        out_combo = ttk.Combobox(line2, values=self.output_options, textvariable=out_var, width=32,
                                  state='readonly', style=out_style)
        out_combo.pack(side='left', padx=2)
        out_combo.bind('<<ComboboxSelected>>', lambda e, i=idx: self._update_tie(i))
        no_wheel(out_combo)
        card._out_var = out_var
        card._out_combo = out_combo

        ttk.Button(line2, text='?', width=3, style='Small.TButton',
                   command=lambda i=idx: self._show_info(i)).pack(side='left', padx=(6, 2))
        ttk.Button(line2, text='X', width=3, style='Small.TButton',
                   command=lambda i=idx: self._remove(i)).pack(side='left', padx=2)
        return card

    def _update_tie(self, idx):
        if idx >= len(self.mapping.ties):
            return
        row = self.rows[idx]
        inputs = [self.input_display_to_key.get(v.get(), '') for v in row._invars]
        inputs = [k for k in inputs if k]
        tie = self.mapping.ties[idx]
        tie.inputs = inputs
        tie.combine = row._combine_var.get()
        try:
            tie.params = {'scale': float(row._scale_var.get()), 'offset': float(row._offset_var.get())}
        except ValueError:
            pass
        tie.output = self.output_display_to_key.get(row._out_var.get(), '')

        # Re-evaluate the orphan-warning style on every combo (item 4.3) -
        # a user picking a real replacement for a previously-orphaned slot
        # should see the red styling clear immediately, without needing a
        # full row rebuild.
        for v, combo in zip(row._invars, row._in_combos):
            combo.configure(style='Warn.TCombobox' if self._is_orphan(v.get()) else 'TCombobox')
        row._out_combo.configure(style='Warn.TCombobox' if self._is_orphan(row._out_var.get()) else 'TCombobox')

    def _add_row(self):
        tie = MappingTie([], 'linear', '', {'scale': 1.0, 'offset': 0.0})
        self.mapping.add(tie)
        self.rows.append(self._make_row(len(self.mapping.ties) - 1, tie))

    def _remove(self, idx):
        self.mapping.remove(idx)
        self._rebuild()

    def _show_info(self, idx):
        tie = self.mapping.ties[idx]
        text, _result = explain_tie(tie, self.state_model)
        show_info_popup(self, f'Mapping: {tie.name}', text)


class ManagementPanel(ttk.Frame):
    """Battery Management tab: one block per curated protection feature,
    pre-filled with the researched defaults (docs/05), fully editable."""

    FEATURE_FIELDS = {
        'low_voltage_cutoff': [('min_cell_v', 'Min cell V (soft, authoritative)'),
                                ('emergency_low_v', 'Emergency low V (hard, authoritative)'),
                                ('soft_cut_persistence_s', 'Soft cut persistence (s, transient-sag guard)'),
                                ('min_soc_pct', 'Min SoC % (backup check only, never acts alone)')],
        'discharge_power_taper': [('taper_start_v', 'Full power at/above (V/cell)'),
                                   ('taper_zero_v', 'Zero power at/below (V/cell)'),
                                   ('recovery_ramp_s', 'Recovery ramp (s, slow release)'),
                                   ('discharge_min_kw', 'Min discharge power (kW)'),
                                   ('discharge_max_kw', 'Max discharge power (kW)')],
        # REGEN ONLY as of 2026-08-01 (split from the old combined regen+AC
        # taper - user directive: regen and AC charging are physically very
        # different, ~0.5C vs ~0.09C). The AC-charger-specific taper and the
        # daily/extended AC target moved to the Charge Emulation tab
        # (ChargeEmulationPanel below), since they only ever mattered while
        # actually plugged in.
        'charge_target_taper': [('regen_full_v', 'Full regen below (V/cell)'),
                                 ('regen_zero_v', 'Zero regen at/above (V/cell)'),
                                 ('emergency_high_v', 'Emergency high V (hard, per-cell)'),
                                 ('recovery_ramp_s', 'Recovery ramp (s, slow release)'),
                                 ('regen_min_kw', 'Min regen power (kW)'),
                                 ('regen_max_kw', 'Max regen power (kW)')],
        'over_temperature_derate': [('charge_derate_low_start_c', 'Charge cold-derate start °C (coldest probe)'),
                                     ('charge_low_block_c', 'Charge low block °C (coldest probe)'),
                                     ('charge_derate_start_c', 'Charge derate start °C (hottest probe)'),
                                     ('charge_hard_stop_c', 'Charge hard stop °C (hottest probe, soft ramp)'),
                                     ('discharge_derate_start_c', 'Discharge derate start °C (hottest probe)'),
                                     ('discharge_hard_stop_c', 'Discharge hard stop °C (hottest probe, soft ramp)'),
                                     ('emergency_temp_c', 'Emergency temp °C (hard, hottest probe)')],
        'cell_imbalance_monitor': [('warn_delta_v', 'Warn spread (V, worst-high minus worst-low)')],
        'overcurrent_monitor': [('continuous_discharge_warn_a', 'Discharge warn (A)'),
                                 ('continuous_charge_warn_a', 'Charge/regen warn (A)'),
                                 ('persistence_s', 'Persistence before warning (s)')],
        'staleness_watchdog': [('soft_cut_s', 'Soft cut after (s)'), ('hard_escalation_s', 'Hard cut escalation (+s)')],
        'cell_data_cross_check': [('max_delta_v', 'Max delta vs 0x020 pack summary (V)'),
                                   ('soft_cut_s', 'Soft cut after (s)'), ('hard_escalation_s', 'Hard cut escalation (+s)')],
        'temp_data_cross_check': [('max_delta_c', 'Max delta vs 0x4A7 pack extremes (°C)'),
                                   ('soft_cut_s', 'Soft cut after (s)'), ('hard_escalation_s', 'Hard cut escalation (+s)')],
        # Both below: no threshold fields, just an enable checkbox - added
        # 2026-08-03 (docs/13 items 15.14/15.15, user directive: "we should
        # add enable disable for our app for testing... default to on").
        # Previously always-on with no config at all; RealtimeEngine reads
        # the same cfg[...]['enabled'] flag to decide whether to actually
        # run the check, not just whether to report on it.
        'input_validation': [],
        'checksum_validation': [],
    }
    LABELS = {
        'low_voltage_cutoff': 'Low-voltage cutoff, cell-voltage authoritative (soft -> capacity_empty)',
        'discharge_power_taper': 'Discharge power taper, per-cell + hysteresis (soft ramp)',
        'charge_target_taper': 'Per-cell REGEN power limit, hysteresis (soft ramp) - AC charger taper is on the Charge Emulation tab',
        'over_temperature_derate': 'Over-temperature derate (soft ramp, both cold and hot side)',
        'cell_imbalance_monitor': 'Cell imbalance monitor (warn only, no cutoff action)',
        'overcurrent_monitor': 'Overcurrent monitor (warn only, no cutoff action)',
        'staleness_watchdog': 'Staleness watchdog (soft -> hard escalation)',
        'cell_data_cross_check': 'Cell data cross-check, per-cell vs 0x020 pack summary (soft -> hard escalation)',
        'temp_data_cross_check': 'Temp data cross-check, 0x4A7 extremes vs 0x4AA per-probe (soft -> hard escalation)',
        'input_validation': 'Input plausibility validation (rejects implausible decoded values)',
        'checksum_validation': 'Toyota checksum validation (rejects corrupt frames)',
    }

    # (lo, hi) numeric bounds per (feature, field) - moved into
    # bridge/management_engine.py (2026-08-03, docs/13 items 13.3/13.9) so
    # ManagementEngine.from_dict() (profile/file loading) can enforce the
    # exact same bounds this panel enforces on typing - previously this
    # table only existed here, so a hand-edited or corrupted profile.json
    # could set any threshold to an arbitrary value with nothing standing
    # in the way. Imported, not redefined, so the two paths can never
    # silently diverge.
    FEATURE_FIELD_BOUNDS = management_engine.FEATURE_FIELD_BOUNDS

    def __init__(self, master, state_model, management_engine, log_fn=None):
        super().__init__(master)
        self.state_model = state_model
        self.management = management_engine
        self.log_fn = log_fn or (lambda msg: None)
        self.status_labels = {}
        self.scroll = VScrollFrame(self, label_text='Battery Management (see the "?" on each feature for details)')
        self.scroll.pack(fill='both', expand=True)
        for feature, fields in self.FEATURE_FIELDS.items():
            self._build_feature(feature, fields)
        self._schedule_status_refresh()

    def _build_feature(self, feature, fields):
        cfg = self.management.config[feature]
        box = ttk.Frame(self.scroll.inner, relief='groove', borderwidth=1)
        box.pack(fill='x', pady=6, padx=2)
        top = ttk.Frame(box)
        top.pack(fill='x', padx=6, pady=(6, 2))
        enabled_var = tk.BooleanVar(value=cfg['enabled'])

        def _on_toggle(feature=feature, cfg=cfg, enabled_var=enabled_var):
            # Log every feature enable/disable (added 2026-08-01, user
            # request: "add this to the log, just so its notated") - a
            # single checkbox fully disables that feature's protection with
            # nothing else standing behind it (by design, see docs/13), so
            # it's worth a permanent record of when/that it happened.
            cfg['enabled'] = enabled_var.get()
            self.log_fn(f'Battery Management: "{self.LABELS.get(feature, feature)}" '
                        f'{"ENABLED" if cfg["enabled"] else "DISABLED"}')

        ttk.Checkbutton(top, text=self.LABELS[feature], variable=enabled_var, command=_on_toggle).pack(side='left')
        help_btn(top, self.LABELS[feature], MANAGEMENT_FEATURE_HELP[feature]).pack(side='left', padx=(6, 0))
        status = ttk.Label(box, text='status: --', foreground=FG_DIM)
        status.pack(anchor='w', padx=28)
        self.status_labels[feature] = status

        grid = ttk.Frame(box)
        grid.pack(fill='x', padx=28, pady=(2, 8))
        for key, label in fields:
            row = ttk.Frame(grid)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=label, width=32, anchor='w').pack(side='left')
            var = tk.StringVar(value=str(cfg[key]))
            ttk.Entry(row, textvariable=var, width=12).pack(side='left')
            flag_lbl = ttk.Label(row, text='', foreground=ERR, width=9)
            flag_lbl.pack(side='left', padx=(4, 0))
            bounds = self.FEATURE_FIELD_BOUNDS.get((feature, key))
            var.trace_add('write', lambda *_a, c=cfg, k=key, v=var, b=bounds, fl=flag_lbl:
                           self._set_float(c, k, v, b, fl))

    @staticmethod
    def _set_float(cfg, key, var, bounds, flag_lbl):
        """Clamps to `bounds` if registered (added 2026-08-01, user
        directive - mirrors ChargeEmulationPanel's existing clamp pattern,
        now applied consistently across every threshold field instead of
        only the charge tab). Unlike the old silent-swallow-on-ValueError
        behavior, an unparseable or out-of-bounds edit is now visually
        flagged (`flag_lbl`) rather than failing invisibly - the user asked
        for exactly this ("if its wrongly computed we should set an error
        state")."""
        try:
            raw = float(var.get())
        except ValueError:
            flag_lbl.configure(text='invalid')
            return
        value = raw
        clamped = False
        if bounds is not None:
            lo, hi = bounds
            if value < lo:
                value, clamped = lo, True
            elif value > hi:
                value, clamped = hi, True
        cfg[key] = value
        flag_lbl.configure(text='clamped' if clamped else '')

    def _schedule_status_refresh(self):
        if not self.winfo_exists():
            return
        for feature, label in self.status_labels.items():
            status = self.management.status.get(feature, '--')
            label.configure(text=f'status: {status}')
        self.after(REFRESH_MS, self._schedule_status_refresh)


class GeneratedSignalsPanel(ttk.Frame):
    """Send/don't-send checkboxes for internally-generated opaque signals
    (docs/03) - never mapping targets, default checked, kept visible so
    nothing is missed or forgotten when porting to STM32 firmware."""

    def __init__(self, master, state_model):
        super().__init__(master)
        head = ttk.Frame(self)
        head.pack(fill='x', padx=4, pady=(4, 0))
        ttk.Label(head, text='Generated Signals (internal, not mapped)', style='Accent.TLabel').pack(side='left')
        help_btn(head, 'Generated Signals', GENERATED_HELP).pack(side='left', padx=(6, 0))

        self.scroll = VScrollFrame(self)
        self.scroll.pack(fill='both', expand=True)
        for key, label, default in leaf_signals.GENERATED_SIGNALS:
            var = tk.BooleanVar(value=state_model.generated_enabled.get(key, default))
            ttk.Checkbutton(self.scroll.inner, text=label, variable=var,
                            command=lambda k=key, v=var: state_model.generated_enabled.__setitem__(k, v.get())
                            ).pack(anchor='w', pady=3, padx=6)


class ChargeEmulationPanel(ttk.Frame):
    """Charge Emulation tab (docs/06, added 2026-07-31): the charger-request
    ramp feature ported from Leaf_BMS_Emulator - checkbox + two numeric
    fields, following the same Entry/StringVar-trace pattern ManagementPanel
    uses for its per-feature config, plus a live status line since this
    feature's state (ramping / stopped / idle) isn't otherwise visible
    anywhere else in the GUI."""

    def __init__(self, master, state_model, log_fn=None, engine=None):
        super().__init__(master)
        self.state_model = state_model
        self.log_fn = log_fn or (lambda msg: None)
        # engine (added 2026-08-03, docs/13 item 14.4): RealtimeEngine.
        # charge_status_summary() is the single accurate source for "why is
        # charging stopped/active right now," resolved from the SAME live
        # status ManagementEngine/the ramp gate themselves produce - avoids
        # this panel re-deriving (and getting wrong) its own guess at the
        # reason. See that method's own docstring for the bug this replaces.
        self.engine = engine
        cfg = state_model.charge_emulation

        # Scrollable (added 2026-08-08, user report: "charge emulation needs
        # a scroll" - the new DC fast-charge/QC section below pushed this
        # tab's content past the visible tab area with no way to reach it).
        # Same VScrollFrame pattern ManagementPanel already uses - put every
        # child in self.scroll.inner, not self, from here on.
        self.scroll = VScrollFrame(self)
        self.scroll.pack(fill='both', expand=True)
        inner = self.scroll.inner

        head = ttk.Frame(inner)
        head.pack(fill='x', padx=4, pady=(4, 0))
        ttk.Label(head, text='Charge Emulation', style='Accent.TLabel').pack(side='left')
        help_btn(head, 'Charge Emulation', CHARGE_EMULATION_HELP).pack(side='left', padx=(6, 0))

        box = ttk.Frame(inner, relief='groove', borderwidth=1)
        box.pack(fill='x', pady=6, padx=6)

        enabled_var = tk.BooleanVar(value=cfg.get('charge_emulate', False))
        ttk.Checkbutton(box, text='Emulate charger request (0x1DC ramp) - requires both a real Leaf '
                                   'charge request AND RZ450e charge permission',
                         variable=enabled_var,
                         command=lambda: cfg.__setitem__('charge_emulate', enabled_var.get())
                         ).pack(anchor='w', padx=6, pady=(6, 2))

        grid = ttk.Frame(box)
        grid.pack(fill='x', padx=28, pady=(2, 6))

        row1 = ttk.Frame(grid)
        row1.pack(fill='x', pady=1)
        ttk.Label(row1, text='Charger ramp target (kW)', width=32, anchor='w').pack(side='left')
        target_var = tk.StringVar(value=str(cfg.get('charge_target_kw', 0.0)))
        ttk.Entry(row1, textvariable=target_var, width=12).pack(side='left')
        _tgt_flag = ttk.Label(row1, text='', foreground=ERR, width=9)
        _tgt_flag.pack(side='left', padx=(4, 0))
        _tgt_lo, _tgt_hi = leaf_signals.CHARGE_EMULATION_BOUNDS['charge_target_kw']
        target_var.trace_add('write', lambda *_a: self._set_float(
            cfg, 'charge_target_kw', target_var, _tgt_lo, _tgt_hi, _tgt_flag))

        row2 = ttk.Frame(grid)
        row2.pack(fill='x', pady=1)
        ttk.Label(row2, text='Uprate level / ramp rate (0-7)', width=32, anchor='w').pack(side='left')
        level_var = tk.StringVar(value=str(cfg.get('chg_uprate_level', 0)))
        ttk.Entry(row2, textvariable=level_var, width=12).pack(side='left')
        _lvl_flag = ttk.Label(row2, text='', foreground=ERR, width=9)
        _lvl_flag.pack(side='left', padx=(4, 0))
        _lvl_lo, _lvl_hi = leaf_signals.CHARGE_EMULATION_BOUNDS['chg_uprate_level']
        level_var.trace_add('write', lambda *_a: self._set_int(
            cfg, 'chg_uprate_level', level_var, _lvl_lo, _lvl_hi, _lvl_flag))

        # AC charge power request bounds (added 2026-08-06, user directive:
        # "the maximum AC charging is only 6.6 kilowatt for the leaf...
        # make this configurable, min and max kW request for AC") - clamps
        # both the ramp target above AND the AC taper's minimum-power floor
        # (management_engine.py's ac_charge_taper) into this range.
        for key, label in (('ac_min_kw', 'AC charge minimum power request (kW)'),
                            ('ac_max_kw', 'AC charge maximum power request (kW)')):
            row = ttk.Frame(grid)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=label, width=32, anchor='w').pack(side='left')
            var = tk.StringVar(value=str(cfg.get(key, 0.0)))
            ttk.Entry(row, textvariable=var, width=12).pack(side='left')
            flag_lbl = ttk.Label(row, text='', foreground=ERR, width=9)
            flag_lbl.pack(side='left', padx=(4, 0))
            lo, hi = leaf_signals.CHARGE_EMULATION_BOUNDS[key]
            var.trace_add('write', lambda *_a, k=key, v=var, lo=lo, hi=hi, fl=flag_lbl:
                           self._set_float(cfg, k, v, lo, hi, fl))

        self.status_label = ttk.Label(box, text='status: --', foreground=FG_DIM)
        self.status_label.pack(anchor='w', padx=6, pady=(0, 6))

        # AC-charger overvoltage taper + AC SoC target (moved here 2026-08-01
        # from Battery Management's charge_target_taper - user directive:
        # "anything charger related should be there... regen and AC charging
        # is not the same"). Independently-tunable curve from the regen
        # taper still on the Battery Management tab (charge_limit_kw).
        ttk.Separator(inner, orient='horizontal').pack(fill='x', padx=6, pady=(4, 4))
        ac_box = ttk.Frame(inner, relief='groove', borderwidth=1)
        ac_box.pack(fill='x', pady=(0, 6), padx=6)

        ac_top = ttk.Frame(ac_box)
        ac_top.pack(fill='x', padx=6, pady=(6, 2))
        ac_enabled_var = tk.BooleanVar(value=cfg.get('ac_taper_enabled', True))

        def _on_ac_toggle(cfg=cfg, ac_enabled_var=ac_enabled_var):
            cfg['ac_taper_enabled'] = ac_enabled_var.get()
            self.log_fn(f'Charge Emulation: "AC charger overvoltage taper" '
                        f'{"ENABLED" if cfg["ac_taper_enabled"] else "DISABLED"}')

        ttk.Checkbutton(ac_top, text='AC charger overvoltage taper (charger_limit_kw, per-cell)',
                        variable=ac_enabled_var, command=_on_ac_toggle).pack(side='left')
        help_btn(ac_top, 'AC charger overvoltage taper', AC_CHARGE_TAPER_HELP).pack(side='left', padx=(6, 0))
        self.ac_status_label = ttk.Label(ac_box, text='status: --', foreground=FG_DIM)
        self.ac_status_label.pack(anchor='w', padx=28)

        ac_grid = ttk.Frame(ac_box)
        ac_grid.pack(fill='x', padx=28, pady=(2, 6))
        # Bounds pulled from leaf_signals.CHARGE_EMULATION_BOUNDS (2026-08-03,
        # docs/13 items 13.3/13.9) instead of hardcoded here, so this panel
        # and the profile-loading clamp in config_profile.py can never
        # silently diverge on what's an acceptable value for these fields.
        # ac_zero_v renamed to ac_min_v, ac_cutoff_v added (2026-08-06) - see
        # leaf_signals.py's CHARGE_SLIDERS comment for the full rationale
        # (the taper now holds at a configurable minimum kW instead of
        # driving to true zero, ac_cutoff_v is the new deliberate
        # stop-charging voltage). The taper's convergence rate is no longer
        # a GUI-configurable field at all - it dynamically self-selects one
        # of the existing 0-7 uprate levels based on how close it is to the
        # target (management_engine.py's _select_ac_uprate_level()),
        # visible in this feature's own live status text below and in the
        # real transmitted 0x1DC uprate bits, not a separate slider here.
        for key, label in (
                ('ac_full_v', 'Full power below (V/cell)'),
                ('ac_min_v', 'Minimum power at/above (V/cell) - holds, does not stop charging'),
                ('ac_cutoff_v', 'Stop-charging cutoff V (per-cell, ends session)'),
                ('ac_emergency_v', 'Emergency high V (hard, per-cell)'),
                ('daily_target_pct', 'Daily target % (SoC stop point)'),
                ('extended_target_pct', 'Extended target % (SoC stop point)')):
            row = ttk.Frame(ac_grid)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=label, width=32, anchor='w').pack(side='left')
            var = tk.StringVar(value=str(cfg.get(key, 0.0)))
            ttk.Entry(row, textvariable=var, width=12).pack(side='left')
            flag_lbl = ttk.Label(row, text='', foreground=ERR, width=9)
            flag_lbl.pack(side='left', padx=(4, 0))
            lo, hi = leaf_signals.CHARGE_EMULATION_BOUNDS[key]
            var.trace_add('write', lambda *_a, k=key, v=var, lo=lo, hi=hi, fl=flag_lbl:
                           self._set_float(cfg, k, v, lo, hi, fl))

        ext_var = tk.BooleanVar(value=cfg.get('extended_mode', False))

        def _on_ext_toggle(cfg=cfg, ext_var=ext_var):
            cfg['extended_mode'] = ext_var.get()
            self.log_fn(f'Charge Emulation: "Extended mode" {"ENABLED" if cfg["extended_mode"] else "DISABLED"}')

        ttk.Checkbutton(ac_grid, text='Extended mode active (road trip - charge to extended target)',
                        variable=ext_var, command=_on_ext_toggle).pack(anchor='w', pady=(4, 0))

        # Data-presence gate (added 2026-08-03, docs/13 item 13.1b; REWORKED
        # same day per user clarification: no separate custom timer - just
        # "has genuinely live data arrived at all," never start the ramp on
        # cached/default values. Ongoing staleness protection once running
        # is the same general watchdog driving gets, on the Battery
        # Management tab, not a second timer here). Lives in the main
        # charge box, not the AC sub-box, since it gates the ramp generally.
        ttk.Separator(inner, orient='horizontal').pack(fill='x', padx=6, pady=(4, 4))
        data_box = ttk.Frame(inner, relief='groove', borderwidth=1)
        data_box.pack(fill='x', pady=(0, 6), padx=6)
        req_var = tk.BooleanVar(value=cfg.get('require_live_data_to_charge', True))

        def _on_req_toggle(cfg=cfg, req_var=req_var):
            cfg['require_live_data_to_charge'] = req_var.get()
            self.log_fn(f'Charge Emulation: "Require live data to charge" '
                        f'{"ENABLED" if cfg["require_live_data_to_charge"] else "DISABLED"}')

        req_top = ttk.Frame(data_box)
        req_top.pack(fill='x', padx=6, pady=(6, 2))
        ttk.Checkbutton(req_top, text='Require genuinely live battery data (not cached/default) before '
                                       'charge ramp can start',
                        variable=req_var, command=_on_req_toggle).pack(side='left')
        help_btn(req_top, 'Require live data to charge', REQUIRE_LIVE_DATA_HELP).pack(side='left', padx=(6, 0))

        # DC fast-charge / QC capacity fields (moved here 2026-08-08 from the
        # "Future Placeholder" tab, user directive: "move all three [qc_max_
        # soc_pct + dc_min_kw + dc_max_kw] to the main Charge Emulation tab" -
        # same theme as the AC fields above, not a separate future-work
        # concern). dc_min_kw/dc_max_kw are still a PLACEHOLDER - not read by
        # any active ramp/taper logic (docs/10-open-questions.md #9, DC fast
        # charging support isn't built yet). qc_max_soc_pct IS live - it
        # caps mapping_engine.derive_capacity_outputs()'s QC capacity display
        # fields (0x59E) - PROVISIONAL, no DC fast-charge testing done yet.
        ttk.Separator(inner, orient='horizontal').pack(fill='x', padx=6, pady=(4, 4))
        dc_box = ttk.Frame(inner, relief='groove', borderwidth=1)
        dc_box.pack(fill='x', pady=(0, 6), padx=6)
        dc_top = ttk.Frame(dc_box)
        dc_top.pack(fill='x', padx=6, pady=(6, 2))
        ttk.Label(dc_top, text='DC fast-charge / QC capacity', style='Header.TLabel').pack(side='left')
        help_btn(dc_top, 'DC fast-charge / QC capacity', DC_QC_HELP).pack(side='left', padx=(6, 0))
        dc_grid = ttk.Frame(dc_box)
        dc_grid.pack(fill='x', padx=28, pady=(2, 6))
        for key, label in (
                ('dc_min_kw', 'DC minimum power request (kW) - PLACEHOLDER, not yet wired to any logic'),
                ('dc_max_kw', 'DC maximum power request (kW) - PLACEHOLDER, not yet wired to any logic'),
                ('qc_max_soc_pct', 'QC max SOC % - GIDS/QC capacity ceiling (PROVISIONAL, untested)')):
            row = ttk.Frame(dc_grid)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=label, width=60, anchor='w').pack(side='left')
            var = tk.StringVar(value=str(cfg.get(key, 0.0)))
            ttk.Entry(row, textvariable=var, width=12).pack(side='left')
            flag_lbl = ttk.Label(row, text='', foreground=ERR, width=9)
            flag_lbl.pack(side='left', padx=(4, 0))
            lo, hi = leaf_signals.CHARGE_EMULATION_BOUNDS[key]
            var.trace_add('write', lambda *_a, k=key, v=var, lo=lo, hi=hi, fl=flag_lbl:
                           self._set_float(cfg, k, v, lo, hi, fl))

        self._schedule_status_refresh()

    @staticmethod
    def _set_float(cfg, key, var, lo, hi, flag_lbl=None):
        """Visual invalid/clamped feedback (added 2026-08-06, user report:
        "check that we can't enter a non valid value in the charger inputs
        - I was able to enter nothing and also outside safe limits" with no
        feedback either way) - ports ManagementPanel._set_float's exact
        pattern into this panel instead of the previous silent
        swallow-on-ValueError / silent-clamp-with-no-indication behavior.
        `flag_lbl` optional so any caller not yet updated keeps working."""
        try:
            raw = float(var.get())
        except ValueError:
            if flag_lbl is not None:
                flag_lbl.configure(text='invalid')
            return
        value = raw
        clamped = False
        if value < lo:
            value, clamped = lo, True
        elif value > hi:
            value, clamped = hi, True
        cfg[key] = value
        if flag_lbl is not None:
            flag_lbl.configure(text='clamped' if clamped else '')

    @staticmethod
    def _set_int(cfg, key, var, lo, hi, flag_lbl=None):
        """See _set_float's docstring - same invalid/clamped feedback,
        int-typed fields (chg_uprate_level)."""
        try:
            raw = int(float(var.get()))
        except ValueError:
            if flag_lbl is not None:
                flag_lbl.configure(text='invalid')
            return
        value = raw
        clamped = False
        if value < lo:
            value, clamped = lo, True
        elif value > hi:
            value, clamped = hi, True
        cfg[key] = value
        if flag_lbl is not None:
            flag_lbl.configure(text='clamped' if clamped else '')

    def _schedule_status_refresh(self):
        if not self.winfo_exists():
            return
        ac_status = self.state_model.snapshot_management_status().get('ac_charge_taper', '--')
        self.ac_status_label.configure(text=f'status: {ac_status}')
        cfg = self.state_model.charge_emulation
        if self.engine is not None:
            # Accurate, centrally-resolved reason (docs/13 item 14.4) -
            # replaces the old hardcoded "RZ450e permission not granted"
            # guess, which was wrong whenever the real cause was the
            # live-data gate, the staleness watchdog, or (not a fault at
            # all) the AC target SoC being reached.
            text = f'status: {self.engine.charge_status_summary()}'
        elif not cfg.get('charge_emulate'):
            text = 'status: disabled - "Max power for charger" uses the Signal Mapping value'
        else:
            tx = self.state_model.snapshot_leaf_tx()
            charger_kw = tx.get('charger_limit_kw')
            text = 'status: no data yet' if charger_kw is None else f'status: charger_limit_kw = {charger_kw:.1f} kW'
        self.status_label.configure(text=text)
        self.after(REFRESH_MS, self._schedule_status_refresh)


class FuturePlaceholderPanel(ttk.Frame):
    """Disabled placeholder for future Leaf-to-battery PID/DID requests
    (VCM querying the battery) - built per docs/08, explicitly NOT
    implemented in this milestone.

    Previously also hosted the DC fast-charge min/max kW request bounds -
    MOVED to ChargeEmulationPanel 2026-08-08 (user directive: "move all
    three [qc_max_soc_pct + dc_min_kw + dc_max_kw] to the main Charge
    Emulation tab") - same theme as the AC fields there, not a future-work
    concern. `state_model` param kept (unused) for call-site compatibility
    with gui/app.py."""

    def __init__(self, master, state_model=None):
        super().__init__(master)
        ttk.Label(self, text='Leaf -> Battery requests (future work)',
                  style='Header.TLabel', foreground='#6a6a70').pack(anchor='w', padx=8, pady=(8, 2))
        ttk.Label(self, text='Not implemented yet. Placeholder for future PID/DID\n'
                              'requests the Leaf VCM may issue to the battery.',
                  foreground='#6a6a70', justify='left').pack(anchor='w', padx=8, pady=4)
        ttk.Combobox(self, values=['(none)'], state='disabled', width=20).pack(anchor='w', padx=8, pady=4)
