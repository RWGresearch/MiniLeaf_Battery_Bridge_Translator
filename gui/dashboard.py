"""HV battery dashboard - a separate, large window with a bar gauge per
signal, grouped by CAN message, showing side by side: the RZ450e input (if
any mapping ties to it), the conversion applied, and the resulting Leaf
output. Bar-gauge mechanics (Canvas + scaled rectangle) and the plain-
tkinter/ttk approach are both ported directly from
Refrance/Leaf_BMS_Emulator/leaf_hvbat_emulator.py's DashWindow.
"""
import time
import tkinter as tk
from tkinter import scrolledtext, ttk

from bridge import leaf_signals, rz450e_signals
from gui.fault_history_window import FaultHistoryWindow
from gui.info_popup import help_btn
from gui.theme import apply_style, VScrollFrame, BG, FIELD, FG, FG_DIM, ACC, ERR, OK

# Everything below is scaled ~15% smaller than its original size (added
# 2026-07-31, user request: "make it 15% smaller in total... so it all fits
# on the screen without maximizing"). Original values are noted alongside
# each so the scale factor is traceable, not a mystery number.
IN_BAR_W, OUT_BAR_W, BAR_H = 119, 145, 12         # was 140, 170, 14
CELL_H = 19   # was 22 - text-label cell height; a ttk.Label at the new 8pt font needs ~17px (confirmed via winfo_reqheight()), NOT BAR_H (sized for the bar canvases only)
RIGHT_COL_W = 306        # was 360
STALE_S = 3.0
POLL_MS = 300

# Explicit fonts (added 2026-07-31, same pass) - most labels previously had
# no font set at all (inheriting the Tk/ttk default, ~9pt), which meant
# there was no baseline to scale down. DASH_FONT is the new ~15%-smaller
# baseline (9pt -> 8pt), applied explicitly everywhere; DASH_ACCENT_FONT and
# DASH_HEADER_FONT override just the SIZE of the shared 'Accent.TLabel'/
# 'Header.TLabel' styles for this window only (they keep those styles' color
# via `style=`, since font options don't affect color) - the global theme
# (gui/theme.py), and therefore the main app window, is untouched.
DASH_FONT = ('Segoe UI', 8)
DASH_FONT_BOLD = ('Segoe UI', 8, 'bold')
DASH_ACCENT_FONT = ('Segoe UI', 9, 'bold')     # was Accent.TLabel's 10pt
DASH_HEADER_FONT = ('Segoe UI', 12, 'bold')    # was Header.TLabel's 14pt

# Fixed PIXEL widths for every column (header cells and data-row cells both
# use these, via `_fixed_cell()` below) - fixed 2026-07-31 (user report: the
# header labels didn't line up with their columns). The old header used
# ttk.Label(width=N) in character units while the data rows mixed pixel-
# exact bar canvases with character-width value labels - those two unit
# systems don't correspond to the same pixel offset, so the header text
# drifted out of alignment with its actual column as soon as a row had a
# wider-than-guessed element. Building both header and data cells from the
# same pixel-width constants guarantees alignment by construction.
SIGNAL_COL_W = 204       # was 240
VAL_COL_W = 60           # was 70
CONV_COL_W = 94          # was 110
RANGE_COL_W = 102        # was 120
IN_COL_W = IN_BAR_W + 4 + VAL_COL_W
OUT_COL_W = OUT_BAR_W + 4 + VAL_COL_W

INPUT_META = {s['key']: s for s in rz450e_signals.INPUT_SIGNALS}

DASHBOARD_HELP = (
    "HV Battery Dashboard (docs/08)\n\n"
    "Every Leaf output signal is shown as a bar gauge, grouped by CAN message, in "
    "the main list on the left:\n"
    "  [Signal label] [Input bar+value (RZ450e)] [conversion] [Output bar+value "
    "(transmitted)] [Startup Default]\n\n"
    "If a mapping tie targets that signal, the input bar shows the live RZ450e "
    "source value and the conversion column shows the formula applied (e.g. "
    "x-1.0 for the sign-inverted current tie). If no tie targets it, the input "
    "side reads '(generated/default)' and only the output bar is shown.\n\n"
    "'Startup Default' (added 2026-07-31) is the static value this field starts "
    "at in leaf_signals.DEFAULTS, before any live mapping/management data has "
    "arrived (docs/06's 'known-good startup memory') - it never changes, so you "
    "can compare it directly against the live Output column to see what's "
    "actually different from what gets sent with no battery data at all.\n\n"
    "Bars grey out when their source has gone stale (no data for 3+ seconds). "
    "Bar ranges are DISPLAY ESTIMATES for scaling only, not confirmed safety "
    "limits - see docs/10's open question about this; the Battery Management "
    "tab's status text is the authoritative source for anything safety-related.\n\n"
    "The right-hand column (Flags, Generated Signals, Charge Emulation, "
    "Battery Management status) is separated from the main list by a "
    "divider for clarity - sized so everything fits without scrolling.\n\n"
    "'Charge emulation (ramp)' (added 2026-07-31) shows the charger-request "
    "ramp feature's live state: whether it's enabled, the configured target/"
    "rate, whether the Leaf currently has a real 0x1F2 charge request active, "
    "whether RZ450e's own charge permission interlock is granting it, the "
    "live charger_limit_kw value, and a plain-English status line - both "
    "triggers are required for the ramp to run; see the 'Charge Emulation' "
    "Configurator tab for the full explanation and controls.\n\n"
    "Fault History moved to its OWN WINDOW (2026-07-31) - it briefly lived at "
    "the bottom of the left list here, but between this window and the main "
    "app there wasn't enough room left for it too. Use the 'Fault History' "
    "button next to this help button to open it.\n\n"
    "The Log panel at the bottom (added 2026-07-31) mirrors the main window's "
    "own log - the same connection/sequencer/cut events, not a separate feed - "
    "so you don't need to switch back to the main window just to see what's "
    "happening."
)


def _conversion_text(tie):
    if tie is None:
        return '(generated/default)'
    if tie.combine == 'linear':
        scale = tie.params.get('scale', 1.0)
        offset = tie.params.get('offset', 0.0)
        return f'x{scale:g} +{offset:g}' if offset else f'x{scale:g}'
    return tie.combine


class _BarGauge:
    def __init__(self, parent, width, lo, hi):
        self.lo, self.hi = lo, hi
        self.canvas = tk.Canvas(parent, width=width, height=BAR_H, bg=FIELD, highlightthickness=0)
        self.width = width
        self.rect = self.canvas.create_rectangle(0, 0, 0, BAR_H, fill=ACC, width=0)

    def pack(self, **kw):
        self.canvas.pack(**kw)

    def set(self, value, fresh=True):
        if value is None or not fresh:
            self.canvas.coords(self.rect, 0, 0, 0, BAR_H)
            self.canvas.itemconfig(self.rect, fill=FG_DIM)
            return
        if self.hi <= self.lo:
            frac = 0.0
        else:
            frac = (value - self.lo) / (self.hi - self.lo)
        out_of_range = frac < 0 or frac > 1
        frac = max(0.0, min(1.0, frac))
        self.canvas.coords(self.rect, 0, 0, frac * self.width, BAR_H)
        self.canvas.itemconfig(self.rect, fill=ERR if out_of_range else ACC)


class DashboardWindow(tk.Toplevel):
    def __init__(self, master, state_model, mapping_engine, management_engine):
        super().__init__(master)
        apply_style(self)
        self.title('HV Battery Dashboard - input > conversion > output')
        # 1430x950 (was 1680x1060) - narrower/shorter to match the ~15%
        # smaller content, and to still comfortably fit the new Log section
        # added below (2026-07-31, user request).
        self.geometry('1430x950')
        self.state_model = state_model
        self.mapping = mapping_engine
        self.management = management_engine   # only used to open FaultHistoryWindow now - see _open_fault_history()
        self.fault_window = None
        self.rows = []          # list of dicts describing each drawn row
        self.flag_rows = []
        self._log_synced = 0    # how many of master.log_lines are already mirrored below
        self._build()
        self._tick()

    def _tie_for_output(self, key):
        for tie in self.mapping.ties:
            if tie.output == key:
                return tie
        return None

    def _open_fault_history(self):
        # Delegates to the main App's own _open_fault_history so this
        # button and the main window's button share ONE window (focus if
        # already open) instead of each independently opening its own -
        # `self.master` is the App instance (DashboardWindow is always
        # constructed with it as master).
        if hasattr(self.master, '_open_fault_history'):
            self.master._open_fault_history()
            return
        if self.fault_window is not None and self.fault_window.winfo_exists():
            self.fault_window.focus()
            return
        self.fault_window = FaultHistoryWindow(self, self.management)

    def _build(self):
        head = ttk.Frame(self)
        head.pack(fill='x', padx=8, pady=(8, 0))
        ttk.Label(head, text='HV Battery Dashboard', style='Header.TLabel',
                  font=DASH_HEADER_FONT).pack(side='left')
        help_btn(head, 'HV Battery Dashboard', DASHBOARD_HELP).pack(side='left', padx=(6, 0))
        ttk.Button(head, text='Fault History', command=self._open_fault_history).pack(side='left', padx=(10, 0))

        # Log (mirrors the main window's log, added 2026-07-31, user
        # request: "a redundant screen that the log has in the main
        # screen") - packed with side='bottom' BEFORE `body` below, so it
        # claims a fixed strip at the bottom of the window and `body` fills
        # whatever's left above it.
        log_frame = ttk.LabelFrame(self, text='Log (mirrors main window)')
        log_frame.pack(side='bottom', fill='x', padx=8, pady=(4, 8))
        self.logbox = scrolledtext.ScrolledText(
            log_frame, height=6, state='disabled', wrap='word', font=DASH_FONT,
            bg=FIELD, fg=OK, insertbackground=FG, relief='flat')
        self.logbox.pack(fill='both', expand=True, padx=4, pady=4)

        body = ttk.Frame(self)
        body.pack(fill='both', expand=True, padx=8, pady=8)

        # Left: the main per-signal list (the long one) - its own vertical
        # scroll area, fills all available width.
        left_outer = VScrollFrame(body, label_text='Every Leaf output signal: RZ450e input -> conversion -> transmitted value')
        left_outer.pack(side='left', fill='both', expand=True)
        scroll = left_outer.inner

        header = ttk.Frame(scroll)
        header.pack(fill='x', pady=(0, 4))
        for text, w in (('Signal', SIGNAL_COL_W), ('Input (RZ450e)', IN_COL_W),
                        ('Conversion', CONV_COL_W), ('Output (Leaf, transmitted)', OUT_COL_W),
                        ('Output Range', RANGE_COL_W), ('Startup Default', OUT_COL_W)):
            self._fixed_cell(header, text, w, font=DASH_FONT_BOLD)

        for group, sigs in leaf_signals.SLIDERS.items():
            ttk.Label(scroll, text=group, style='Accent.TLabel', font=DASH_ACCENT_FONT).pack(anchor='w', pady=(10, 2))
            for key, label, lo, hi, step, _default in sigs:
                self._build_value_row(scroll, key, label, lo, hi)

        # Divider, then a fixed-width right column for the supplementary
        # (non-per-signal) info - moved out of the bottom of the long list
        # per user request, so it's visible without scrolling all the way
        # down and clearly separated from the main data.
        ttk.Separator(body, orient='vertical').pack(side='left', fill='y', padx=8)

        right = ttk.Frame(body, width=RIGHT_COL_W)
        right.pack(side='left', fill='y')
        right.pack_propagate(False)

        ttk.Label(right, text='Flags (soft/hard cut + permissions)', style='Accent.TLabel',
                  font=DASH_ACCENT_FONT).pack(anchor='w', pady=(0, 2))
        for key, label, _default in leaf_signals.CHECKS:
            self._build_flag_row(right, key, label)

        ttk.Label(right, text='Generated signals (opaque, not mapped)',
                  style='Accent.TLabel', font=DASH_ACCENT_FONT).pack(anchor='w', pady=(12, 2))
        gen_frame = ttk.Frame(right)
        gen_frame.pack(fill='x')
        self.gen_labels = {}
        for key, label, _default in leaf_signals.GENERATED_SIGNALS:
            row = ttk.Frame(gen_frame)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=label, width=26, anchor='w', font=DASH_FONT).pack(side='left')
            lbl = ttk.Label(row, text='--', width=10, anchor='w', font=DASH_FONT)
            lbl.pack(side='left')
            self.gen_labels[key] = lbl

        # Charge emulation / ramp status (added 2026-07-31) - the two
        # triggers driving the charger-request ramp (docs/06) weren't
        # surfaced together anywhere: RZ450e's charge_permission_input was
        # only visible buried in the left input list, and whether the Leaf
        # currently has an active 0x1F2 request at all wasn't shown anywhere
        # in the GUI (it lives inside RealtimeEngine.sequencer, not
        # SharedState). charger_limit_kw itself is already visible as a bar
        # in the main per-signal list above, but not with the "why" (ramping
        # vs. idle vs. stopped-on-mismatch) attached to it.
        ttk.Label(right, text='Charge emulation (ramp)', style='Accent.TLabel',
                  font=DASH_ACCENT_FONT).pack(anchor='w', pady=(12, 2))
        charge_frame = ttk.Frame(right)
        charge_frame.pack(fill='x')
        self.charge_labels = {}
        for name, label in (('enabled', 'Emulation'), ('target', 'Target / rate'),
                             ('leaf_request', 'Leaf 0x1F2 request'),
                             ('rz_permission', 'RZ450e permission')):
            row = ttk.Frame(charge_frame)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=label, width=17, anchor='w', font=DASH_FONT).pack(side='left')
            lbl = ttk.Label(row, text='--', anchor='w', font=DASH_FONT)
            lbl.pack(side='left')
            self.charge_labels[name] = lbl
        kw_row = ttk.Frame(charge_frame)
        kw_row.pack(fill='x', pady=(4, 0))
        ttk.Label(kw_row, text='charger_limit_kw:', anchor='w', foreground=ACC, font=DASH_FONT).pack(anchor='w')
        self.charge_labels['charger_kw'] = ttk.Label(kw_row, text='--', anchor='w', font=DASH_FONT)
        self.charge_labels['charger_kw'].pack(anchor='w')
        status_row = ttk.Frame(charge_frame)
        status_row.pack(fill='x', pady=(4, 0))
        ttk.Label(status_row, text='status:', anchor='w', foreground=ACC, font=DASH_FONT).pack(anchor='w')
        self.charge_labels['status'] = ttk.Label(status_row, text='--', anchor='w', justify='left',
                                                  wraplength=RIGHT_COL_W - 20, font=DASH_FONT)
        self.charge_labels['status'].pack(anchor='w')

        ttk.Label(right, text='Battery management status', style='Accent.TLabel',
                  font=DASH_ACCENT_FONT).pack(anchor='w', pady=(12, 2))
        self.mgmt_frame = ttk.Frame(right)
        self.mgmt_frame.pack(fill='x')
        self.mgmt_labels = {}

    @staticmethod
    def _fixed_cell(parent, text, width_px, height_px=CELL_H, font=DASH_FONT, **label_kw):
        """A Frame pinned to an exact PIXEL width (pack_propagate off) with
        one left-anchored Label inside - used for every text cell in both
        the header row and the data rows, so a column's header and its data
        always occupy the identical pixel span regardless of font metrics
        (see the module-level comment on SIGNAL_COL_W etc.). Defaults to
        DASH_FONT; callers (e.g. the bold header row) pass their own font=
        to override."""
        cell = tk.Frame(parent, width=width_px, height=height_px)
        cell.pack_propagate(False)
        cell.pack(side='left', padx=(0, 4))
        lbl = ttk.Label(cell, text=text, anchor='w', font=font, **label_kw)
        lbl.pack(side='left', fill='both', expand=True)
        return lbl

    def _build_value_row(self, parent, key, label, lo, hi):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=1)
        self._fixed_cell(row, label, SIGNAL_COL_W)

        tie = self._tie_for_output(key)
        in_key = tie.inputs[0] if (tie and tie.inputs) else None
        in_meta = INPUT_META.get(in_key) if in_key else None
        in_lo, in_hi = in_meta['range'] if in_meta else (0, 1)
        in_bar = _BarGauge(row, IN_BAR_W, in_lo, in_hi)
        in_bar.pack(side='left', padx=(0, 4))
        in_val_lbl = self._fixed_cell(row, 'no data', VAL_COL_W, foreground=FG_DIM)

        conv_lbl = self._fixed_cell(row, _conversion_text(tie), CONV_COL_W, foreground=FG_DIM)

        out_bar = _BarGauge(row, OUT_BAR_W, lo, hi)
        out_bar.pack(side='left', padx=(0, 4))
        out_val_lbl = self._fixed_cell(row, 'no data', VAL_COL_W, foreground=FG_DIM)

        # Output Range (added 2026-07-31, user request) - the documented
        # (lo, hi) this signal's bars are scaled against, so the bars above
        # can be read as an actual physical range, not just a proportional
        # fill. Static - the range itself doesn't change at runtime.
        range_text = f'{lo:g} to {hi:g}'
        self._fixed_cell(row, range_text, RANGE_COL_W, foreground=FG_DIM)

        # Startup default (added 2026-07-31, user request) - the static
        # DEFAULTS value this field starts at before any live mapping/
        # management data arrives (docs/06 section 2, "known-good startup
        # memory"). Static, not live - set once and never updated in _tick() -
        # a bar on the SAME (lo, hi) range as the Output bar, so the two bars
        # are directly, visually comparable at a glance, not just the numbers.
        default_v = leaf_signals.DEFAULTS.get(key)
        default_bar = _BarGauge(row, OUT_BAR_W, lo, hi)
        default_bar.pack(side='left', padx=(0, 4))
        default_bar.set(default_v, fresh=True)
        default_text = 'n/a' if default_v is None else f'{default_v:.3f}'
        self._fixed_cell(row, default_text, VAL_COL_W, foreground=FG_DIM)

        self.rows.append({'key': key, 'in_key': in_key, 'in_bar': in_bar, 'in_lbl': in_val_lbl,
                          'out_bar': out_bar, 'out_lbl': out_val_lbl, 'tie': tie, 'conv_lbl': conv_lbl})

    def _build_flag_row(self, parent, key, label):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=1)
        ttk.Label(row, text=label, wraplength=RIGHT_COL_W - 90, anchor='w', justify='left',
                  font=DASH_FONT).pack(side='left', fill='x', expand=True)
        val = ttk.Label(row, text='no data', width=8, anchor='w', foreground=FG_DIM, font=DASH_FONT)
        val.pack(side='left')
        self.flag_rows.append({'key': key, 'lbl': val})

    def _tick(self):
        if not self.winfo_exists():
            return
        tx = self.state_model.snapshot_leaf_tx()
        # Output freshness (added 2026-08-01) - previously hardcoded
        # fresh=True unconditionally, so a dead TX thread would leave the
        # output bars looking perfectly normal forever. Mirrors gui/app.py's
        # own heartbeat check (RealtimeEngine.last_tick_monotonic).
        engine = getattr(self.master, 'engine', None)
        last_tick = getattr(engine, 'last_tick_monotonic', None) if engine else None
        tx_fresh = last_tick is not None and (time.monotonic() - last_tick) <= STALE_S
        for r in self.rows:
            out_v = tx.get(r['key'])
            r['out_bar'].set(out_v, fresh=tx_fresh)
            if out_v is None:
                r['out_lbl'].configure(text='no data', foreground=FG_DIM)
            else:
                r['out_lbl'].configure(text=f"{out_v:.3f}" + ('' if tx_fresh else ' (stale)'),
                                        foreground=FG if tx_fresh else ERR)
            if r['in_key']:
                in_v = self.state_model.get_input(r['in_key'])
                age = self.state_model.age_of(r['in_key'])
                fresh = age is not None and age < STALE_S
                r['in_bar'].set(in_v, fresh=fresh)
                if in_v is None:
                    r['in_lbl'].configure(text='no data', foreground=FG_DIM)
                else:
                    r['in_lbl'].configure(text=f"{in_v:.3f}" + ('' if fresh else ' (stale)'),
                                          foreground=FG if fresh else ERR)
            # keep conversion text in sync in case the tie changed since build
            current_tie = self._tie_for_output(r['key'])
            r['conv_lbl'].configure(text=_conversion_text(current_tie))

        for r in self.flag_rows:
            v = tx.get(r['key'])
            if v is None:
                r['lbl'].configure(text='no data', foreground=FG_DIM)
            else:
                r['lbl'].configure(text='ON' if v else 'OFF', foreground=OK if v else FG_DIM)

        for key, lbl in self.gen_labels.items():
            enabled = self.state_model.generated_enabled.get(key, True)
            lbl.configure(text='sending' if enabled else 'OFF', foreground=OK if enabled else FG_DIM)

        # Charge emulation / ramp status (added 2026-07-31) - see _build()'s
        # comment above for why this needed its own section. `engine` is
        # RealtimeEngine (gui/app.py's App.engine); its sequencer already
        # tracks the live 0x1F2 request state, so this reads it directly
        # rather than duplicating the 0x1F2 decode in SharedState.
        cfg = self.state_model.charge_emulation
        emulate_on = bool(cfg.get('charge_emulate'))
        self.charge_labels['enabled'].configure(text='ON' if emulate_on else 'OFF',
                                                 foreground=OK if emulate_on else FG_DIM)
        self.charge_labels['target'].configure(
            text=f"{cfg.get('charge_target_kw', 0.0):.1f} kW, level {int(cfg.get('chg_uprate_level', 0))}")

        engine = getattr(self.master, 'engine', None)
        leaf_wants = bool(engine is not None and engine.sequencer.charge_active(time.monotonic()))
        rz_auth = bool(self.state_model.get_input('charge_permission_input'))
        self.charge_labels['leaf_request'].configure(text='ACTIVE' if leaf_wants else 'idle',
                                                      foreground=OK if leaf_wants else FG_DIM)
        self.charge_labels['rz_permission'].configure(text='GRANTED' if rz_auth else 'not granted',
                                                       foreground=OK if rz_auth else FG_DIM)

        charger_kw = tx.get('charger_limit_kw')
        full_stop = bool(tx.get('full_charge_flag'))
        kw_text = 'no data' if charger_kw is None else f'{charger_kw:.1f} kW'
        self.charge_labels['charger_kw'].configure(text=kw_text, foreground=FG_DIM if charger_kw is None else FG)

        # Centrally-resolved status (docs/13 item 14.4, fixed 2026-08-03) -
        # RealtimeEngine.charge_status_summary() replaces the old hardcoded
        # "RZ450e permission not granted" guess here, which was wrong
        # whenever the real cause was the live-data gate, the staleness
        # watchdog, or (not a fault at all) the AC target SoC being reached -
        # see that method's own docstring. `engine` may be None only in a
        # test harness that builds this window without a real App/engine.
        if engine is not None:
            status_text = engine.charge_status_summary()
        elif not emulate_on:
            status_text = 'disabled - "Max power for charger" uses the Signal Mapping value'
        elif leaf_wants and rz_auth:
            status_text = 'ramping/active - both triggers present'
        else:
            status_text = 'idle - no active, authorized charge request'
        # Color by message content, not just full_stop - "CHARGE COMPLETE"
        # (target SoC reached) also sets full_charge_flag but is a SUCCESS,
        # not a fault, and must not render in the same red as a genuine
        # STOPPED-on-a-problem message (docs/13 item 14.4).
        if status_text.startswith('STOPPED'):
            status_fg = ERR
        elif status_text.startswith('CHARGE COMPLETE') or status_text.startswith('ramping'):
            status_fg = OK
        else:
            status_fg = FG_DIM
        self.charge_labels['status'].configure(text=status_text, foreground=status_fg)

        status = self.state_model.snapshot_management_status()
        for feature, text in status.items():
            if feature not in self.mgmt_labels:
                row = ttk.Frame(self.mgmt_frame)
                row.pack(fill='x', pady=(4, 0))
                ttk.Label(row, text=feature, anchor='w', foreground=ACC, font=DASH_FONT).pack(anchor='w')
                lbl = ttk.Label(row, text='', anchor='w', wraplength=RIGHT_COL_W - 20, justify='left',
                                 font=DASH_FONT)
                lbl.pack(anchor='w')
                self.mgmt_labels[feature] = lbl
            self.mgmt_labels[feature].configure(text=text)

        # Mirror the main window's log (added 2026-07-31, user request) -
        # `self.master` is the App instance; `log_lines` is the same
        # formatted-line history the main window's own log box is built
        # from (bridge/app.py's _drain_log()), so this reads it rather than
        # draining the same queue.Queue a second time (which would just
        # split messages between the two boxes instead of mirroring). Only
        # appends what's new since the last tick, not the whole history
        # every time. Note: if the deque's maxlen (2000) is ever exceeded
        # mid-session, older evicted lines simply won't be (re)synced here -
        # an acceptable limitation for a secondary/redundant view.
        log_lines = getattr(self.master, 'log_lines', None)
        if log_lines is not None and len(log_lines) > self._log_synced:
            new_lines = list(log_lines)[self._log_synced:]
            self._log_synced = len(log_lines)
            self.logbox.configure(state='normal')
            for line in new_lines:
                self.logbox.insert('end', line)
            self.logbox.see('end')
            self.logbox.configure(state='disabled')

        self.after(POLL_MS, self._tick)
