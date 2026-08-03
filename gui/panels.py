"""Reusable GUI panels (docs/08-gui-design.md): adapter connections, live
value monitors, signal mapping, battery management, and generated-signal
checkboxes. Plain tkinter/ttk (no customtkinter - reverted 2026-07-31 per
user request, see gui/theme.py)."""
import tkinter as tk
from tkinter import ttk

from bridge import can_backend, leaf_signals, rz450e_signals
from bridge.mapping_engine import MappingTie, COMBINE_TYPES, explain_tie
from gui.info_popup import show_info_popup, help_btn
from gui.theme import VScrollFrame, BASE_FONT, FG, FIELD, FG_DIM, ERR, OK

REFRESH_MS = 400


def _no_wheel(combo):
    """Prevent mouse-wheel scrolling from silently changing a readonly
    Combobox's selection (added 2026-08-01, user report: "when I'm
    scrolling with the wheel I have accidentally changed the inputs/
    outputs... this data should only be valid if selected with a mouse
    pointer and click"). An instance-level binding fires before both the
    Combobox's own class binding (which changes the value on wheel scroll)
    and VScrollFrame's page-scroll bind_all handler (gui/theme.py) - so
    returning "break" here stops both; only an explicit click can change
    the selection now."""
    for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
        combo.bind(seq, lambda e: 'break')

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
    "see docs/10 open question #11."
)
MAPPING_HELP = (
    "Signal Mapping (docs/04)\n\n"
    "Each card ties one or more RZ450e input signals to one Leaf output signal "
    "through a fixed preset combine function - linear (scale+offset), sum, "
    "average, min, max, or the soh_percent formula. No generic scripting is "
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
    "5-7) ride along with it, 0 -> level.\n\n"
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
    "AC charger overvoltage taper (docs/05, added 2026-08-01)\n\n"
    "Split out of Battery Management's per-cell charge/regen taper (user "
    "directive: AC charging via the Leaf's onboard charger, ~19A/0.09C, is "
    "physically very different from regenerative braking, up to ~0.5C into "
    "the pack - so each gets its own independently-tunable curve instead of "
    "sharing one). Drives ONLY charger_limit_kw ('Max power for charger,' "
    "docs/03) - full power at/below 'Full power below', ramping to zero at/"
    "above 'Zero power at/above', with 'Emergency high V' as a second, more "
    "extreme per-cell threshold that escalates to a HARD CUT.\n\n"
    "'Daily target %' / 'Extended target %' and full_charge_flag are a "
    "separate concern, gated on RZ450e's charge_permission_input (charging "
    "interlock) being active - only fires during an actual plugged-in charge "
    "session, never from simply driving above the target SoC. Toggle "
    "'Extended mode' for a road-trip charge to the extended target instead "
    "of the daily one."
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
        "voltage bounces near the threshold under intermittent acceleration."
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
        "rather than sharing this one."
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
        "blocked entirely at/below 'Charge low block' °F and ramps up to full "
        "acceptance by 'Charge cold-derate start' °F - added because plating "
        "risk rises well above the freezing line at meaningful charge "
        "current; our real exposure here is regen into a cold-soaked pack "
        "(~0.5C), not the Leaf's 0.09C AC charger.\n\n"
        "'Emergency temp' (added 2026-07-31) is a genuine second, more "
        "extreme tier above 'Discharge hard stop' that escalates to a HARD "
        "CUT (relay_cut_request + interlock drop) - mirrors the two-tier "
        "soft/emergency structure the voltage features already have. "
        "Deliberately close above the soft stop: a cell with any plated "
        "lithium can begin self-heating as low as ~60°C (140°F), so there's "
        "very little real margin to work with.\n\n"
        "Defaults are researched general NMC/lithium-ion safety ranges "
        "cross-checked against this pack's own confirmed 3.6-4.2V/3.7V-nominal "
        "real-world range - not yet confirmed against a real thermal test on this "
        "specific pack (see docs/11's verification checklist)."
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
        _no_wheel(self.channel_menu)
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
    signals to display."""

    def __init__(self, master, title, text_fn):
        super().__init__(master)
        self.text_fn = text_fn
        ttk.Label(self, text=title, style='Header.TLabel').pack(anchor='w', padx=8, pady=(8, 2))
        self.box = tk.Text(self, wrap='none', bg=FIELD, fg=FG, insertbackground=FG,
                            relief='flat', state='disabled', font=BASE_FONT)
        self.box.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self._refresh()

    def _refresh(self):
        if not self.winfo_exists():
            return
        text = self.text_fn()
        yview = self.box.yview()
        self.box.configure(state='normal')
        self.box.delete('1.0', 'end')
        self.box.insert('1.0', text)
        self.box.configure(state='disabled')
        try:
            self.box.yview_moveto(yview[0])
        except Exception:
            pass
        self.after(REFRESH_MS, self._refresh)


def input_monitor_text(state_model):
    lines = []
    groups = {}
    for sig in rz450e_signals.INPUT_SIGNALS:
        groups.setdefault(sig['group'], []).append(sig)
    for group, sigs in groups.items():
        lines.append(f'-- {group} --')
        for sig in sigs:
            key = sig['key']
            val = state_model.get_input(key)
            age = state_model.age_of(key)
            age_s = f'{age:4.1f}s' if age is not None else ' --  '
            if val is None:
                lines.append(f'  {sig["label"]:<40} --')
            else:
                lines.append(f'  {sig["label"]:<40} {val:>10.3f} {sig["unit"]:<4} (age {age_s})')
    return '\n'.join(lines)


def leaf_tx_monitor_text(state_model):
    lines = []
    tx = state_model.snapshot_leaf_tx()
    for group, sigs in leaf_signals.SLIDERS.items():
        lines.append(f'-- {group} --')
        for key, label, *_rest in sigs:
            val = tx.get(key)
            lines.append(f'  {label:<40} {val!r}')
    lines.append('-- Flags --')
    for key, label, _default in leaf_signals.CHECKS:
        lines.append(f'  {label:<40} {tx.get(key)!r}')
    lines.append('-- Management status --')
    for feature, status in state_model.snapshot_management_status().items():
        lines.append(f'  {feature:<30} {status}')
    return '\n'.join(lines)


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
        _no_wheel(car_combo)
        ttk.Label(row, text='Battery:').pack(side='left')
        batt_combo = ttk.Combobox(row, values=['AZE0', 'ZE1'], textvariable=self.batt_gen, width=6, state='readonly')
        batt_combo.pack(side='left', padx=4)
        batt_combo.bind('<<ComboboxSelected>>', lambda e: state_model.set_vehicle_item('battery_gen', self.batt_gen.get()))
        _no_wheel(batt_combo)
        ttk.Label(row, text='kWh:').pack(side='left')
        kwh_combo = ttk.Combobox(row, values=['30', '40', '62'], textvariable=self.batt_kwh, width=4, state='readonly')
        kwh_combo.pack(side='left', padx=4)
        kwh_combo.bind('<<ComboboxSelected>>', lambda e: state_model.set_vehicle_item('battery_kwh', int(self.batt_kwh.get())))
        _no_wheel(kwh_combo)


class MappingPanel(ttk.Frame):
    """Signal Mapping tab: one row per tie, up to 3 input dropdowns (blank =
    unused) + combine selector + output dropdown + '?' popup + remove."""

    BLANK = '(unused)'

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

    def _make_row(self, idx, tie):
        card = ttk.Frame(self.scroll.inner, relief='groove', borderwidth=1)
        card.pack(fill='x', pady=4, padx=2)

        line1 = ttk.Frame(card)
        line1.pack(fill='x', padx=6, pady=(6, 2))
        ttk.Label(line1, text='IN:', width=4).pack(side='left')
        vars_in = []
        for i in range(3):
            key = tie.inputs[i] if i < len(tie.inputs) else ''
            v = tk.StringVar(value=self.input_key_to_display.get(key, self.BLANK))
            combo = ttk.Combobox(line1, values=self.input_options, textvariable=v, width=32, state='readonly')
            combo.pack(side='left', padx=2)
            combo.bind('<<ComboboxSelected>>', lambda e, i=idx: self._update_tie(i))
            _no_wheel(combo)
            vars_in.append(v)
        card._invars = vars_in

        line2 = ttk.Frame(card)
        line2.pack(fill='x', padx=6, pady=(2, 6))
        ttk.Label(line2, text='->', width=3).pack(side='left')
        combine_var = tk.StringVar(value=tie.combine)
        combine_combo = ttk.Combobox(line2, values=list(COMBINE_TYPES), textvariable=combine_var, width=12, state='readonly')
        combine_combo.pack(side='left', padx=2)
        combine_combo.bind('<<ComboboxSelected>>', lambda e, i=idx: self._update_tie(i))
        _no_wheel(combine_combo)
        card._combine_var = combine_var

        scale_var = tk.StringVar(value=str(tie.params.get('scale', 1.0)))
        offset_var = tk.StringVar(value=str(tie.params.get('offset', 0.0)))
        ttk.Entry(line2, textvariable=scale_var, width=7).pack(side='left', padx=1)
        ttk.Entry(line2, textvariable=offset_var, width=7).pack(side='left', padx=1)
        card._scale_var, card._offset_var = scale_var, offset_var
        for v in (scale_var, offset_var):
            v.trace_add('write', lambda *_a, i=idx: self._update_tie(i))

        ttk.Label(line2, text='OUT:', width=5).pack(side='left', padx=(6, 0))
        out_var = tk.StringVar(value=self.output_key_to_display.get(tie.output, self.BLANK))
        out_combo = ttk.Combobox(line2, values=self.output_options, textvariable=out_var, width=32, state='readonly')
        out_combo.pack(side='left', padx=2)
        out_combo.bind('<<ComboboxSelected>>', lambda e, i=idx: self._update_tie(i))
        _no_wheel(out_combo)
        card._out_var = out_var

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
                                   ('recovery_ramp_s', 'Recovery ramp (s, slow release)')],
        # REGEN ONLY as of 2026-08-01 (split from the old combined regen+AC
        # taper - user directive: regen and AC charging are physically very
        # different, ~0.5C vs ~0.09C). The AC-charger-specific taper and the
        # daily/extended AC target moved to the Charge Emulation tab
        # (ChargeEmulationPanel below), since they only ever mattered while
        # actually plugged in.
        'charge_target_taper': [('regen_full_v', 'Full regen below (V/cell)'),
                                 ('regen_zero_v', 'Zero regen at/above (V/cell)'),
                                 ('emergency_high_v', 'Emergency high V (hard, per-cell)'),
                                 ('recovery_ramp_s', 'Recovery ramp (s, slow release)')],
        'over_temperature_derate': [('charge_derate_low_start_f', 'Charge cold-derate start °F (coldest probe)'),
                                     ('charge_low_block_f', 'Charge low block °F (coldest probe)'),
                                     ('charge_derate_start_f', 'Charge derate start °F (hottest probe)'),
                                     ('charge_hard_stop_f', 'Charge hard stop °F (hottest probe, soft ramp)'),
                                     ('discharge_derate_start_f', 'Discharge derate start °F (hottest probe)'),
                                     ('discharge_hard_stop_f', 'Discharge hard stop °F (hottest probe, soft ramp)'),
                                     ('emergency_temp_f', 'Emergency temp °F (hard, hottest probe)')],
        'cell_imbalance_monitor': [('warn_delta_v', 'Warn spread (V, worst-high minus worst-low)')],
        'overcurrent_monitor': [('continuous_discharge_warn_a', 'Discharge warn (A)'),
                                 ('continuous_charge_warn_a', 'Charge/regen warn (A)'),
                                 ('persistence_s', 'Persistence before warning (s)')],
        'staleness_watchdog': [('soft_cut_s', 'Soft cut after (s)'), ('hard_escalation_s', 'Hard cut escalation (+s)')],
        'cell_data_cross_check': [('max_delta_v', 'Max delta vs 0x020 pack summary (V)'),
                                   ('soft_cut_s', 'Soft cut after (s)'), ('hard_escalation_s', 'Hard cut escalation (+s)')],
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
    }

    # Registered (lo, hi) numeric bounds per (feature, field) - added
    # 2026-08-01, user directive ("we need clamps on all data thats input
    # by the user"). Deliberately generous (same "sanity range, not an
    # operating threshold" philosophy as rz450e_signals.PLAUSIBLE_RANGES) -
    # this exists to catch a mistyped value (an extra digit, a misplaced
    # decimal), not to second-guess a deliberately extreme but valid
    # threshold choice. A field with no entry here is left unclamped
    # (numeric parse failure is still caught, just no range enforced).
    FEATURE_FIELD_BOUNDS = {
        ('low_voltage_cutoff', 'min_cell_v'): (0.0, 5.0),
        ('low_voltage_cutoff', 'emergency_low_v'): (0.0, 5.0),
        ('low_voltage_cutoff', 'soft_cut_persistence_s'): (0.0, 60.0),
        ('low_voltage_cutoff', 'min_soc_pct'): (0.0, 100.0),
        ('discharge_power_taper', 'taper_start_v'): (0.0, 5.0),
        ('discharge_power_taper', 'taper_zero_v'): (0.0, 5.0),
        ('discharge_power_taper', 'recovery_ramp_s'): (0.01, 60.0),
        ('charge_target_taper', 'regen_full_v'): (0.0, 5.0),
        ('charge_target_taper', 'regen_zero_v'): (0.0, 5.0),
        ('charge_target_taper', 'emergency_high_v'): (0.0, 5.0),
        ('charge_target_taper', 'recovery_ramp_s'): (0.01, 60.0),
        ('over_temperature_derate', 'charge_derate_low_start_f'): (-60.0, 250.0),
        ('over_temperature_derate', 'charge_low_block_f'): (-60.0, 250.0),
        ('over_temperature_derate', 'charge_derate_start_f'): (-60.0, 250.0),
        ('over_temperature_derate', 'charge_hard_stop_f'): (-60.0, 250.0),
        ('over_temperature_derate', 'discharge_derate_start_f'): (-60.0, 250.0),
        ('over_temperature_derate', 'discharge_hard_stop_f'): (-60.0, 250.0),
        ('over_temperature_derate', 'emergency_temp_f'): (-60.0, 250.0),
        ('cell_imbalance_monitor', 'warn_delta_v'): (0.0, 2.0),
        ('overcurrent_monitor', 'continuous_discharge_warn_a'): (0.0, 210.0),
        ('overcurrent_monitor', 'continuous_charge_warn_a'): (0.0, 210.0),
        ('overcurrent_monitor', 'persistence_s'): (0.0, 120.0),
        ('staleness_watchdog', 'soft_cut_s'): (1.0, 600.0),
        ('staleness_watchdog', 'hard_escalation_s'): (0.0, 600.0),
        ('cell_data_cross_check', 'max_delta_v'): (0.0, 2.0),
        ('cell_data_cross_check', 'soft_cut_s'): (1.0, 600.0),
        ('cell_data_cross_check', 'hard_escalation_s'): (0.0, 600.0),
    }

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

    def __init__(self, master, state_model, log_fn=None):
        super().__init__(master)
        self.state_model = state_model
        self.log_fn = log_fn or (lambda msg: None)
        cfg = state_model.charge_emulation

        head = ttk.Frame(self)
        head.pack(fill='x', padx=4, pady=(4, 0))
        ttk.Label(head, text='Charge Emulation', style='Accent.TLabel').pack(side='left')
        help_btn(head, 'Charge Emulation', CHARGE_EMULATION_HELP).pack(side='left', padx=(6, 0))

        box = ttk.Frame(self, relief='groove', borderwidth=1)
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
        target_var.trace_add('write', lambda *_a: self._set_float(cfg, 'charge_target_kw', target_var, 0.0, 92.2))

        row2 = ttk.Frame(grid)
        row2.pack(fill='x', pady=1)
        ttk.Label(row2, text='Uprate level / ramp rate (0-7)', width=32, anchor='w').pack(side='left')
        level_var = tk.StringVar(value=str(cfg.get('chg_uprate_level', 0)))
        ttk.Entry(row2, textvariable=level_var, width=12).pack(side='left')
        level_var.trace_add('write', lambda *_a: self._set_int(cfg, 'chg_uprate_level', level_var, 0, 7))

        self.status_label = ttk.Label(box, text='status: --', foreground=FG_DIM)
        self.status_label.pack(anchor='w', padx=6, pady=(0, 6))

        # AC-charger overvoltage taper + AC SoC target (moved here 2026-08-01
        # from Battery Management's charge_target_taper - user directive:
        # "anything charger related should be there... regen and AC charging
        # is not the same"). Independently-tunable curve from the regen
        # taper still on the Battery Management tab (charge_limit_kw).
        ttk.Separator(self, orient='horizontal').pack(fill='x', padx=6, pady=(4, 4))
        ac_box = ttk.Frame(self, relief='groove', borderwidth=1)
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
        for key, label, lo, hi in (
                ('ac_full_v', 'Full power below (V/cell)', 0.0, 5.0),
                ('ac_zero_v', 'Zero power at/above (V/cell)', 0.0, 5.0),
                ('ac_emergency_v', 'Emergency high V (hard, per-cell)', 0.0, 5.0),
                ('daily_target_pct', 'Daily target % (SoC stop point)', 0.0, 100.0),
                ('extended_target_pct', 'Extended target % (SoC stop point)', 0.0, 100.0)):
            row = ttk.Frame(ac_grid)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=label, width=32, anchor='w').pack(side='left')
            var = tk.StringVar(value=str(cfg.get(key, 0.0)))
            ttk.Entry(row, textvariable=var, width=12).pack(side='left')
            var.trace_add('write', lambda *_a, k=key, v=var, lo=lo, hi=hi: self._set_float(cfg, k, v, lo, hi))

        ext_var = tk.BooleanVar(value=cfg.get('extended_mode', False))

        def _on_ext_toggle(cfg=cfg, ext_var=ext_var):
            cfg['extended_mode'] = ext_var.get()
            self.log_fn(f'Charge Emulation: "Extended mode" {"ENABLED" if cfg["extended_mode"] else "DISABLED"}')

        ttk.Checkbutton(ac_grid, text='Extended mode active (road trip - charge to extended target)',
                        variable=ext_var, command=_on_ext_toggle).pack(anchor='w', pady=(4, 0))

        self._schedule_status_refresh()

    @staticmethod
    def _set_float(cfg, key, var, lo, hi):
        try:
            cfg[key] = max(lo, min(hi, float(var.get())))
        except ValueError:
            pass

    @staticmethod
    def _set_int(cfg, key, var, lo, hi):
        try:
            cfg[key] = max(lo, min(hi, int(float(var.get()))))
        except ValueError:
            pass

    def _schedule_status_refresh(self):
        if not self.winfo_exists():
            return
        ac_status = self.state_model.snapshot_management_status().get('ac_charge_taper', '--')
        self.ac_status_label.configure(text=f'status: {ac_status}')
        tx = self.state_model.snapshot_leaf_tx()
        cfg = self.state_model.charge_emulation
        if not cfg.get('charge_emulate'):
            text = 'status: disabled - "Max power for charger" uses the Signal Mapping value'
        else:
            full_stop = bool(tx.get('full_charge_flag'))
            charger_kw = tx.get('charger_limit_kw')
            if full_stop:
                text = 'status: STOPPED - charge requested but RZ450e permission not granted (full_charge_flag set)'
            elif charger_kw is None:
                text = 'status: no data yet'
            else:
                # 92.3kW is the idle "no limit" placeholder (leaf_signals.DEFAULTS) -
                # anything below it means an authorized charge request is ramping
                # (or the management engine's own safety taper has reduced it).
                text = f'status: charger_limit_kw = {charger_kw:.1f} kW' + \
                    (' (idle - no active/authorized request)' if charger_kw >= 92.2 else ' (ramping/active)')
        self.status_label.configure(text=text)
        self.after(REFRESH_MS, self._schedule_status_refresh)


class FuturePlaceholderPanel(ttk.Frame):
    """Disabled placeholder for future Leaf-to-battery PID/DID requests
    (VCM querying the battery) - built per docs/08, explicitly NOT
    implemented in this milestone."""

    def __init__(self, master):
        super().__init__(master)
        ttk.Label(self, text='Leaf -> Battery requests (future work)',
                  style='Header.TLabel', foreground='#6a6a70').pack(anchor='w', padx=8, pady=(8, 2))
        ttk.Label(self, text='Not implemented yet. Placeholder for future PID/DID\n'
                              'requests the Leaf VCM may issue to the battery.',
                  foreground='#6a6a70', justify='left').pack(anchor='w', padx=8, pady=4)
        ttk.Combobox(self, values=['(none)'], state='disabled', width=20).pack(anchor='w', padx=8, pady=4)
