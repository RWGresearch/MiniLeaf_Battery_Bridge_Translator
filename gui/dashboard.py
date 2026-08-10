"""HV battery dashboard - a separate, large window with a bar gauge per
signal, grouped by CAN message, showing side by side: the RZ450e input (if
any mapping ties to it), the conversion applied, and the resulting Leaf
output. Bar-gauge mechanics (Canvas + scaled rectangle) and the plain-
tkinter/ttk approach are both ported directly from
Refrance/Leaf_BMS_Emulator/leaf_hvbat_emulator.py's DashWindow.
"""
import math
import time
import tkinter as tk
from tkinter import scrolledtext, ttk

from bridge import leaf_signals, rz450e_signals
from gui.fault_history_window import FaultHistoryWindow
from gui.info_popup import help_btn
from gui.theme import (apply_style, VScrollFrame, configure_field_tags, write_field, write_section, no_wheel,
                       BG, FIELD, FG, FG_DIM, ACC, ERR, OK, MONO_FONT)

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
    "divider for clarity, and is a single read-only auto-refreshing "
    "monospace text block (changed 2026-08-06) - it scrolls independently "
    "with the mouse wheel, so the Battery Management status block (which "
    "grows with however many features are active) is always reachable "
    "even past the bottom of the window. Each field is a label, then its "
    "value flush against the box's own right edge (a real Tk tab stop, "
    "measured against the box's actual width) - only a genuinely too-long "
    "label wraps to a second line.\n\n"
    "At the bottom of the main per-signal list on the LEFT (below all the "
    "SLIDERS groups, added 2026-08-06) are three vertical bar charts side "
    "by side: all 96 individual cell voltages, all 16 temperature probes, "
    "and the 2 pack temperature extremes (Max/Min pack temperature from "
    "0x4A7 - distinct from the 16 individual probe readings) - given that "
    "list's full width to split between them rather than being squeezed "
    "into the narrow right column. Each chart auto-scales to its own live "
    "min/max plus a fixed margin, chosen from the dropdown next to that "
    "chart's title - ±0.01V to ±1V for cell voltages, ±0.1C to ±5C for "
    "both temp charts (the 'Temp probes' dropdown drives the pack-"
    "extremes chart too), or 'Full scale' on either to switch to a hard "
    "range instead (2.5-5.0V / -40 to 71C, this project's own documented "
    "cell/probe display range). Whichever margin is selected, the "
    "displayed range only ever GROWS, never shrinks back down once real "
    "data is flowing, so it doesn't visibly wobble on every small "
    "tick-to-tick fluctuation; it resets fresh the next time there's no "
    "live data at all (e.g. a disconnect) or the dropdown selection "
    "changes. A value outside the current displayed range still draws "
    "(clamped) but in red, same convention as every other bar in this "
    "window. The min/max/spread line above each chart is always the REAL "
    "live numbers, independent of whatever display range is currently "
    "selected - read that line, not the bar heights, for anything "
    "safety-related. Bars grey out the same as everywhere else in this "
    "window when their source has gone stale.\n\n"
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


class _MultiVBar:
    """A row of `count` independently-drawn VERTICAL bars in one Canvas -
    used for the 96 per-cell voltages, 16 per-probe temperatures, and the
    2 pack temperature extremes (added 2026-08-06). `auto_pad` is a fixed
    MARGIN (e.g. 0.5V, 5F) added below/above the live min/max to get the
    displayed scale, rather than a hard (lo, hi) - a fully-fixed range
    (the previous version) gave up any sense of "how much headroom is
    left," and pure live-min/max auto-scale (the version before THAT)
    made the tallest/shortest bar always touch the chart's edges no
    matter how tight the real spread was. User: "lets auto scale to .5V
    below and .5V above the min max. but dont jumparound to scale."

    The displayed scale only ever EXPANDS (never shrinks) once real data
    is flowing, and each expansion snaps its new edge OUT to the next grid
    line - a QUARTER of `auto_pad` (see set_values()) beyond what's
    actually needed, rather than the exact live-value-plus-pad figure - a
    first cut expanded by the exact amount needed every time, which meant
    ordinary CAN-noise-level jitter in the live readings (a value drifting
    by a fraction of a millivolt tick to tick) kept nudging the displayed
    edge out by that same tiny amount forever, i.e. it never actually
    stopped moving. Snapping to a grid gives each expansion real slack, so
    small readings within that slack don't trigger another one - confirmed
    via a smoke test that fed +/-0.2mV noise for 10 ticks after the scale
    settled and found it genuinely didn't move. The grid was tightened
    from a full `auto_pad` down to `auto_pad`/4 (user: "add slight less
    buffer to the top and bottom") - a full-pad grid could add up to
    another whole pad of extra headroom on top of what was requested;
    quartering it caps the excess much closer to the literal requested
    pad while remaining well above realistic noise levels. Never
    shrinks back down if the spread later narrows (an accepted trade-off
    per the same request). Resets back to unset the next time there's no
    live data at all (e.g. a disconnect), so a fresh session doesn't
    inherit a stale expanded range.

    `fixed_range=(lo, hi)` (added 2026-08-06, user request for a "Full
    scale" dropdown option) overrides auto_pad entirely with a hard range,
    same as _BarGauge's - use set_scale() to switch between the two modes
    at runtime, not the constructor args directly (it also clears the
    sticky auto_pad window so a later switch back to auto-pad doesn't
    inherit whatever range Full Scale mode was showing)."""

    def __init__(self, parent, count, width, height, gap=1, auto_pad=None, fixed_range=None):
        self.height = height
        self.auto_pad = auto_pad
        self.fixed_range = fixed_range
        self._disp_lo = None
        self._disp_hi = None
        self.canvas = tk.Canvas(parent, width=width, height=height, bg=FIELD, highlightthickness=0)
        total_gap = gap * max(0, count - 1)
        bar_w = max(1.0, (width - total_gap) / count) if count else width
        self.bars = []
        x = 0.0
        for _ in range(count):
            rect = self.canvas.create_rectangle(x, height, x + bar_w, height, fill=ACC, width=0)
            self.bars.append((rect, x, x + bar_w))
            x += bar_w + gap

    def pack(self, **kw):
        self.canvas.pack(**kw)

    def set_scale(self, auto_pad=None, fixed_range=None):
        """Switches scaling mode - called when the user picks a different
        dropdown option (gui/dashboard.py's cell/temp scale Comboboxes).
        Resets the sticky auto-pad window so the new mode starts clean on
        the very next set_values() call instead of inheriting whatever
        range the OLD mode had settled on."""
        self.auto_pad = auto_pad
        self.fixed_range = fixed_range
        self._disp_lo = None
        self._disp_hi = None

    def set_values(self, values):
        """`values`: list of (value_or_None, fresh) pairs, one per bar (in
        the same order the bars were created). Always returns the LIVE
        (lo, hi) actually present this call (for the caller's own min/max
        readout label), regardless of what the bars were scaled against.
        Returns None if nothing was live (and resets the sticky displayed
        range - see class docstring). A value outside the displayed range
        still gets drawn (clamped to the full bar) but in ERR red, same
        convention as _BarGauge's out-of-range coloring."""
        present = [v for v, fresh in values if v is not None and fresh]
        if not present:
            self._disp_lo = None
            self._disp_hi = None
            for rect, x0, x1 in self.bars:
                self.canvas.coords(rect, x0, self.height, x1, self.height)
                self.canvas.itemconfig(rect, fill=FG_DIM)
            return None
        live_lo, live_hi = min(present), max(present)
        if self.fixed_range is not None:
            lo, hi = self.fixed_range
        elif self.auto_pad is not None:
            pad = self.auto_pad
            target_lo, target_hi = live_lo - pad, live_hi + pad
            # Snap OUT to the next grid line beyond what's needed (floor
            # for the low edge, ceil for the high edge) - see the class
            # docstring for why: it's what gives each expansion enough
            # slack that ordinary noise doesn't trigger another one right
            # away. Grid step is pad/4, not pad itself (tightened 2026-08-06,
            # user: "add slight less buffer to the top and bottom") - a
            # full-pad-sized grid could add up to another whole pad of
            # extra headroom beyond what was actually requested (worst
            # case ~2x pad total); pad/4 caps that excess at ~25% of pad
            # instead (~1.25x pad total) while still being comfortably
            # larger than realistic CAN-noise-level jitter for every pad
            # size this window offers (confirmed against the existing
            # +/-0.2mV noise smoke test, which is ~12x smaller than pad/4
            # even at the smallest 0.01V option).
            grid = pad / 4
            if self._disp_lo is None or target_lo < self._disp_lo:
                self._disp_lo = math.floor(target_lo / grid) * grid
            if self._disp_hi is None or target_hi > self._disp_hi:
                self._disp_hi = math.ceil(target_hi / grid) * grid
            lo, hi = self._disp_lo, self._disp_hi
        else:
            lo, hi = live_lo, live_hi
        span = hi - lo
        for (rect, x0, x1), (v, fresh) in zip(self.bars, values):
            if v is None or not fresh:
                self.canvas.coords(rect, x0, self.height, x1, self.height)
                self.canvas.itemconfig(rect, fill=FG_DIM)
                continue
            frac = 0.5 if span <= 0 else (v - lo) / span
            out_of_range = frac < 0 or frac > 1
            frac = max(0.0, min(1.0, frac))
            bar_h = frac * self.height
            self.canvas.coords(rect, x0, self.height - bar_h, x1, self.height)
            self.canvas.itemconfig(rect, fill=ERR if out_of_range else ACC)
        return (live_lo, live_hi)


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

        # Cell-voltage / temp-probe bar charts, SIDE BY SIDE (moved here
        # 2026-08-06, user correction over two follow-ups: "the temp bar's
        # are supose to be in the left under the history data" then "cell
        # voltages are also supose to be on the left. side by side." - both
        # charts started in the narrow right column, which only gave 96/16
        # bars a few px each; down here, under the main per-signal list,
        # they get that list's full width to split between them instead.
        # Live inside `scroll` (left_outer's VScrollFrame inner frame), so
        # they scroll with the rest of the left list - don't need their own
        # layout-height budget the way the right column's charts did.
        #
        # Scale (added 2026-08-06, revised twice same day) - three earlier
        # attempts: pure live-min/max auto-scale (bars always touched the
        # chart edges regardless of real spread), a hard FIXED range (no
        # sense of "how much headroom is left"), then a hardcoded
        # auto-pad. User's final call: "lets go ahead and make the scale
        # adjustable with a drop down. The voltage can be point one up to
        # one volt or full scale, and the temperature can be point one
        # degree up to five degree or full scale" (defaults set to 0.1V/
        # 1C per a follow-up correction; temp options converted from °F to
        # °C 2026-08-09, same relative granularity). Each chart gets a
        # Comboboxen picking either an auto_pad margin (see _MultiVBar's
        # docstring for the expand-only/no-jitter mechanics) or
        # CELL_FULL_RANGE/TEMP_FULL_RANGE - the latter reuses
        # rz450e_signals' own existing per-cell/per-probe registry ranges
        # (2.5-5.0V, -40 to 71C) rather than inventing new numbers. The
        # temp dropdown drives BOTH
        # temp charts (probes and pack extremes) at once, since the user
        # referred to "the temperature" as one category. A value outside
        # the CURRENT displayed range still draws (clamped) but in ERR
        # red, matching _BarGauge's existing out-of-range convention. The
        # min/max/spread readout label above each chart is always the
        # REAL live numbers, independent of the selected display range.
        # no_wheel() (gui/theme.py) on each Combobox - they live inside
        # this same VScrollFrame-scrolled list, so without it a mouse-
        # wheel scroll over one would silently change its selection
        # instead of scrolling the page (the exact bug no_wheel was
        # written to prevent elsewhere in this app).
        VOLT_PAD_OPTIONS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0]
        TEMP_PAD_OPTIONS = [0.1, 0.5, 1.0, 2.0, 5.0]
        FULL_SCALE_LABEL = 'Full scale'
        CELL_FULL_RANGE = (2.5, 5.0)
        TEMP_FULL_RANGE = (-40.0, 71.1)
        DEFAULT_CELL_PAD = 0.05
        DEFAULT_TEMP_PAD = 1.0

        def pad_choices(options, unit):
            labels = [f'±{v:g}{unit}' for v in options] + [FULL_SCALE_LABEL]
            lookup = {f'±{v:g}{unit}': v for v in options}
            return labels, lookup

        volt_labels, self._volt_pad_lookup = pad_choices(VOLT_PAD_OPTIONS, 'V')
        temp_labels, self._temp_pad_lookup = pad_choices(TEMP_PAD_OPTIONS, 'C')
        volt_default_label = f'±{DEFAULT_CELL_PAD:g}V'
        temp_default_label = f'±{DEFAULT_TEMP_PAD:g}C'

        # Shorter charts + tighter vertical whitespace (2026-08-06, user:
        # "lets make each bar graph less tall. then give even less
        # padding") - height 150 -> 100px, charts_row's own top/bottom
        # margin trimmed, and the per-canvas pady after each chart's
        # min/max label dropped entirely (each bar canvas now packs
        # directly under its label with no extra gap).
        CHART_HEIGHT = 100
        charts_row = ttk.Frame(scroll)
        charts_row.pack(fill='x', pady=(10, 2))
        total_chart_w = SIGNAL_COL_W + IN_COL_W + CONV_COL_W + OUT_COL_W + RANGE_COL_W + OUT_COL_W
        chart_gap = 24
        temp_chart_w = 220
        other_temp_chart_w = 90
        cell_chart_w = total_chart_w - temp_chart_w - other_temp_chart_w - chart_gap * 2

        cell_col = ttk.Frame(charts_row)
        cell_col.pack(side='left')
        cell_head = ttk.Frame(cell_col)
        cell_head.pack(fill='x')
        ttk.Label(cell_head, text='Cell voltages (96)', style='Accent.TLabel',
                  font=DASH_ACCENT_FONT).pack(side='left')
        self.cell_scale_var = tk.StringVar(value=volt_default_label)
        cell_scale_combo = ttk.Combobox(cell_head, textvariable=self.cell_scale_var, state='readonly',
                                         width=10, values=volt_labels)
        cell_scale_combo.pack(side='left', padx=(6, 0))
        no_wheel(cell_scale_combo)
        cell_scale_combo.bind('<<ComboboxSelected>>',
                               lambda e: self._on_cell_scale_change(self.cell_scale_var.get(), CELL_FULL_RANGE))
        self.cell_keys = rz450e_signals.cell_voltage_keys()
        self.cell_minmax_lbl = ttk.Label(cell_col, text='no data', font=DASH_FONT, foreground=FG_DIM)
        self.cell_minmax_lbl.pack(anchor='w')
        self.cell_bars = _MultiVBar(cell_col, count=len(self.cell_keys), width=cell_chart_w, height=CHART_HEIGHT,
                                     auto_pad=DEFAULT_CELL_PAD)
        self.cell_bars.pack(anchor='w')

        temp_col = ttk.Frame(charts_row)
        temp_col.pack(side='left', padx=(chart_gap, 0))
        temp_head = ttk.Frame(temp_col)
        temp_head.pack(fill='x')
        ttk.Label(temp_head, text='Temp probes (16)', style='Accent.TLabel',
                  font=DASH_ACCENT_FONT).pack(side='left')
        self.temp_scale_var = tk.StringVar(value=temp_default_label)
        temp_scale_combo = ttk.Combobox(temp_head, textvariable=self.temp_scale_var, state='readonly',
                                         width=10, values=temp_labels)
        temp_scale_combo.pack(side='left', padx=(6, 0))
        no_wheel(temp_scale_combo)
        temp_scale_combo.bind('<<ComboboxSelected>>',
                               lambda e: self._on_temp_scale_change(self.temp_scale_var.get(), TEMP_FULL_RANGE))
        self.temp_keys = rz450e_signals.temp_probe_keys()
        self.temp_minmax_lbl = ttk.Label(temp_col, text='no data', font=DASH_FONT, foreground=FG_DIM)
        self.temp_minmax_lbl.pack(anchor='w')
        self.temp_bars = _MultiVBar(temp_col, count=len(self.temp_keys), width=temp_chart_w, height=CHART_HEIGHT,
                                     gap=3, auto_pad=DEFAULT_TEMP_PAD)
        self.temp_bars.pack(anchor='w')

        # Third chart, same row (added 2026-08-06, user request: "lets add
        # a third data graph that shows the other temps we arr reciving
        # from the battery in the same wrow") - the 0x4A7 'Temp extremes'
        # message's pack-level temp_max/temp_min (Max/Min pack
        # temperature), distinct from the 16 individual 0x4AA probe
        # readings the middle chart shows. Shares the "Temp probes"
        # dropdown above rather than getting its own - same auto-scale/pad
        # treatment, since these are temps too.
        other_temp_col = ttk.Frame(charts_row)
        other_temp_col.pack(side='left', padx=(chart_gap, 0))
        ttk.Label(other_temp_col, text='Pack temp extremes (2)', style='Accent.TLabel',
                  font=DASH_ACCENT_FONT).pack(anchor='w')
        self.other_temp_keys = ['temp_max', 'temp_min']
        self.other_temp_minmax_lbl = ttk.Label(other_temp_col, text='no data', font=DASH_FONT, foreground=FG_DIM)
        self.other_temp_minmax_lbl.pack(anchor='w')
        self.other_temp_bars = _MultiVBar(other_temp_col, count=len(self.other_temp_keys), width=other_temp_chart_w,
                                           height=CHART_HEIGHT, gap=6, auto_pad=DEFAULT_TEMP_PAD)
        self.other_temp_bars.pack(anchor='w')

        # Divider, then a fixed-width right column for the supplementary
        # (non-per-signal) info - moved out of the bottom of the long list
        # per user request, so it's visible without scrolling all the way
        # down and clearly separated from the main data.
        ttk.Separator(body, orient='vertical').pack(side='left', fill='y', padx=8)

        # Right column (rebuilt 2026-08-06, user request: "the data in the
        # main screen for live data works well... lets use that for the
        # data in the right side of the dashboard... i like the way that
        # works. its cleener") - was a per-field grid of individually-
        # updated ttk.Label widgets; now a read-only auto-refreshing
        # tk.Text, same mechanism as gui/panels.py's LiveMonitorPanel (the
        # main window's own 'Live transmitted values' panel) - both now
        # share write_section()/write_field()/configure_field_tags()
        # (gui/theme.py). Each field is a label, a tab, then its value -
        # configure_field_tags() sets a right-aligned tab stop against this
        # box's own real width, so the value lands flush against the right
        # edge regardless of label length (only a genuinely too-long label
        # wraps). Both bar charts that used to live below this box moved to
        # the left list, side by side, per later user correction - see the
        # comment above them, right after the SLIDERS loop.
        right = ttk.Frame(body, width=RIGHT_COL_W)
        right.pack(side='left', fill='y')
        right.pack_propagate(False)
        ttk.Label(right, text='Flags / generated / charge / management', style='Header.TLabel'
                  ).pack(anchor='w', padx=4, pady=(2, 4))
        self.right_box = tk.Text(right, wrap='word', bg=FIELD, fg=FG, insertbackground=FG,
                                  relief='flat', state='disabled', font=MONO_FONT)
        self.right_box.pack(fill='both', expand=True, padx=4, pady=(0, 4))
        configure_field_tags(self.right_box)

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

    def _write_right_column(self):
        """Refreshes self.right_box (Flags/Generated-signals/Charge-
        emulation/Battery-management) - called from _tick() below, using
        the same write_section()/write_field() (gui/theme.py) the main
        window's LiveMonitorPanel-based live panels use."""
        box = self.right_box
        # Scroll position preserved by INDEX, not yview() fraction - see
        # gui/panels.py's LiveMonitorPanel._refresh() for why (same fix,
        # same user report: "the live text slowely scrowl back to the top
        # on its own").
        top_index = box.index('@0,0')
        box.configure(state='normal')
        box.delete('1.0', 'end')

        def section(title):
            write_section(box, title)

        def field(label, value):
            write_field(box, label, value)

        tx = self.state_model.snapshot_leaf_tx()
        engine = getattr(self.master, 'engine', None)

        section('Flags (soft/hard cut + permissions)')
        # relay_cut_request (docs/13 item 16.3) - the actual HARD CUT signal
        # (0x1DB); listed first, ahead of leaf_signals.CHECKS, since it's
        # the most severe of the group (it lives in SLIDERS, not CHECKS, so
        # it's not already covered by the loop below).
        flag_fields = [('relay_cut_request', 'Relay cut request (0x1DB) - HARD CUT')]
        flag_fields += [(key, label) for key, label, _default in leaf_signals.CHECKS]
        for key, label in flag_fields:
            v = tx.get(key)
            field(label, 'no data' if v is None else ('ON' if v else 'OFF'))

        section('Generated signals (opaque, not mapped)')
        for key, label, _default in leaf_signals.GENERATED_SIGNALS:
            enabled = self.state_model.generated_enabled.get(key, True)
            field(label, 'sending' if enabled else 'OFF')

        section('Charge emulation (ramp)')
        cfg = self.state_model.charge_emulation
        emulate_on = bool(cfg.get('charge_emulate'))
        field('Emulation', 'ON' if emulate_on else 'OFF')
        field('Target / rate',
              f'{cfg.get("charge_target_kw", 0.0):.1f} kW, level {int(cfg.get("chg_uprate_level", 0))}')
        leaf_wants = bool(engine is not None and engine.sequencer.charge_active(time.monotonic()))
        rz_auth = bool(self.state_model.get_input('charge_permission_input'))
        field('Leaf 0x1F2 request', 'ACTIVE' if leaf_wants else 'idle')
        field('RZ450e permission', 'GRANTED' if rz_auth else 'not granted')
        charger_kw = tx.get('charger_limit_kw')
        field('charger_limit_kw', 'no data' if charger_kw is None else f'{charger_kw:.1f} kW')
        # Centrally-resolved status (docs/13 item 14.4) - see
        # RealtimeEngine.charge_status_summary()'s own docstring for why
        # this can't just check full_charge_flag directly. `engine` may be
        # None only in a test harness that builds this window without a
        # real App/engine.
        if engine is not None:
            status_text = engine.charge_status_summary()
        elif not emulate_on:
            status_text = 'disabled - "Max power for charger" uses the Signal Mapping value'
        elif leaf_wants and rz_auth:
            status_text = 'ramping/active - both triggers present'
        else:
            status_text = 'idle - no active, authorized charge request'
        field('status', status_text)

        section('Battery management status')
        for feature, text in self.state_model.snapshot_management_status().items():
            field(feature, text)

        box.configure(state='disabled')
        try:
            box.yview(top_index)
        except Exception:
            pass

    def _on_cell_scale_change(self, choice, full_range):
        """Bound to the 'Cell voltages' scale Combobox's <<ComboboxSelected>>
        (see _build()). `choice` is the selected label text; a lookup miss
        means 'Full scale' was picked."""
        pad = self._volt_pad_lookup.get(choice)
        if pad is None:
            self.cell_bars.set_scale(fixed_range=full_range)
        else:
            self.cell_bars.set_scale(auto_pad=pad)
        self._write_cell_temp_bars()

    def _on_temp_scale_change(self, choice, full_range):
        """Bound to the 'Temp probes' scale Combobox's <<ComboboxSelected>>
        (see _build()) - drives BOTH temp charts (probes and pack
        extremes) at once, since the user referred to "the temperature"
        as one category rather than asking for two independent controls."""
        pad = self._temp_pad_lookup.get(choice)
        if pad is None:
            self.temp_bars.set_scale(fixed_range=full_range)
            self.other_temp_bars.set_scale(fixed_range=full_range)
        else:
            self.temp_bars.set_scale(auto_pad=pad)
            self.other_temp_bars.set_scale(auto_pad=pad)
        self._write_cell_temp_bars()

    def _refresh_bar_chart(self, keys, bars, lbl, unit, precision):
        """Shared by cell/temp-probe/pack-temp-extremes charts below -
        pulls each key's current (value, freshness) from state_model,
        updates `bars`, and formats its min/max/spread readout label."""
        vals = []
        for key in keys:
            v = self.state_model.get_input(key)
            age = self.state_model.age_of(key)
            vals.append((v, age is not None and age < STALE_S))
        rng = bars.set_values(vals)
        if rng is None:
            lbl.configure(text='no data')
        else:
            lo, hi = rng
            lbl.configure(text=f'min {lo:.{precision}f}{unit}   max {hi:.{precision}f}{unit}   '
                                f'spread {hi - lo:.{precision}f}{unit}')

    def _write_cell_temp_bars(self):
        """Refreshes the cell-voltage/temp-probe/pack-temp-extremes bar
        charts and their min/max readout labels - called from _tick()
        below. Bar HEIGHT is scaled against each chart's sticky
        auto-padded range (see _MultiVBar.set_values()); the readout label
        always reports the REAL live min/max regardless of that."""
        self._refresh_bar_chart(self.cell_keys, self.cell_bars, self.cell_minmax_lbl, 'V', 3)
        self._refresh_bar_chart(self.temp_keys, self.temp_bars, self.temp_minmax_lbl, '°C', 1)
        self._refresh_bar_chart(self.other_temp_keys, self.other_temp_bars, self.other_temp_minmax_lbl, '°C', 1)

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

        self._write_right_column()
        self._write_cell_temp_bars()

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
