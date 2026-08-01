"""Fault History - its own window (moved out of the Dashboard 2026-07-31,
user request: the Dashboard didn't have enough real estate for it on top of
everything else once both were made more compact - "make it tall and
narrow"). Same underlying data and behavior as when this lived inside the
Dashboard (bridge/fault_log.py's FaultLog/FAULT_DEFINITIONS, the light/
count/Reset semantics) - just single-column top-to-bottom now, since a
narrow window doesn't have room for the Dashboard's old two-column grid.
"""
import tkinter as tk
from tkinter import ttk

from bridge.fault_log import FAULT_DEFINITIONS
from gui.info_popup import help_btn
from gui.theme import apply_style, VScrollFrame, BG, FIELD, FG, FG_DIM, ERR

LIGHT_D = 12
DESC_WRAP = 330

_LEVEL_COLOR = {'hard': ERR, 'soft': '#ffa94d', 'warn': '#e0c341'}

FAULT_HISTORY_HELP = (
    "Fault History (moved to its own window 2026-07-31)\n\n"
    "Records every time a soft cut, hard cut, or monitor warning has actually "
    "triggered - independent of whether it has since auto-cleared. Cuts still "
    "auto-clear the instant their triggering reading recovers (kept as the "
    "default behavior - see docs/06 section 5); this window just makes sure a "
    "brief fault that trips and self-clears between checks doesn't go unnoticed.\n\n"
    "The light on the left of each row shows its CURRENT state at a glance - lit "
    "solid in the fault's tier color (red = hard, orange = soft, yellow = warn) = "
    "active right now; a hollow ring (dark center, colored outline) = has happened "
    "before but is currently clear; plain grey = never happened this session.\n\n"
    "The count below each description shows how many times it's triggered this "
    "session. 'Reset' clears that one entry's count/history - like clearing a "
    "stored code on a real BMS scan tool, it does NOT force a still-active "
    "condition to go away (that's driven by live sensor data every tick, "
    "unchanged) - a genuinely still-active fault immediately starts counting "
    "again from 1 on the very next check. 'Reset all' clears every entry at once.\n\n"
    "History is saved to its own file (config/fault_log.json) and survives app "
    "restarts, not just a bridge power-cycle within one session."
)


class FaultHistoryWindow(tk.Toplevel):
    def __init__(self, master, management_engine):
        super().__init__(master)
        apply_style(self)
        self.title('Fault History')
        # Tall and narrow (user request) - positioned just to the right of
        # whichever window opened it, rather than a hardcoded screen
        # position that might not suit every monitor layout.
        x = master.winfo_x() + master.winfo_width() + 10
        y = master.winfo_y()
        self.geometry(f'420x900+{x}+{y}')
        self.management = management_engine
        self.fault_labels = {}   # fault key -> (light, circle, name_lbl, count_lbl, level)
        self._build()
        self._tick()

    def _build(self):
        head = ttk.Frame(self)
        head.pack(fill='x', padx=8, pady=(8, 4))
        ttk.Label(head, text='Fault History', style='Header.TLabel').pack(side='left')
        help_btn(head, 'Fault History', FAULT_HISTORY_HELP).pack(side='left', padx=(6, 0))

        reset_row = ttk.Frame(self)
        reset_row.pack(fill='x', padx=8, pady=(0, 4))
        ttk.Button(reset_row, text='Reset all', style='Small.TButton',
                   command=self._reset_all).pack(side='right')

        legend = ttk.Frame(self)
        legend.pack(fill='x', padx=8, pady=(0, 8))
        self._legend_light(legend, ERR, ERR)
        ttk.Label(legend, text='active', foreground=FG_DIM).pack(side='left', padx=(0, 8))
        self._legend_light(legend, FIELD, ERR)
        ttk.Label(legend, text='cleared', foreground=FG_DIM).pack(side='left', padx=(0, 8))
        self._legend_light(legend, FG_DIM, FG_DIM)
        ttk.Label(legend, text='never', foreground=FG_DIM).pack(side='left')

        scroll = VScrollFrame(self)
        scroll.pack(fill='both', expand=True, padx=4, pady=(0, 8))
        self.fault_frame = scroll.inner
        for key, label, level in FAULT_DEFINITIONS:
            self._build_fault_row(key, label, level)

    @staticmethod
    def _legend_light(parent, fill, outline):
        c = tk.Canvas(parent, width=LIGHT_D, height=LIGHT_D, bg=BG, highlightthickness=0)
        c.create_oval(1, 1, LIGHT_D - 1, LIGHT_D - 1, fill=fill, outline=outline)
        c.pack(side='left', padx=(0, 3))
        return c

    def _build_fault_row(self, key, label, level):
        # Stacked two-line layout (light + description on top, count +
        # Reset below) instead of the Dashboard's old single-line-per-row -
        # gives the description room to wrap in a narrow window without
        # forcing the count/button off to a cramped corner.
        row = ttk.Frame(self.fault_frame, relief='groove', borderwidth=1)
        row.pack(fill='x', pady=3, padx=2)

        top = ttk.Frame(row)
        top.pack(fill='x', padx=4, pady=(4, 2))
        light = tk.Canvas(top, width=LIGHT_D, height=LIGHT_D, bg=BG, highlightthickness=0)
        circle = light.create_oval(1, 1, LIGHT_D - 1, LIGHT_D - 1, fill=FG_DIM, outline=FG_DIM)
        light.pack(side='left', padx=(0, 6))
        name_lbl = ttk.Label(top, text=label, wraplength=DESC_WRAP, anchor='w',
                              justify='left', foreground=FG)
        name_lbl.pack(side='left', fill='x', expand=True)

        bottom = ttk.Frame(row)
        bottom.pack(fill='x', padx=(4 + LIGHT_D + 6, 4), pady=(0, 4))
        count_lbl = ttk.Label(bottom, text='--', anchor='w', foreground=FG_DIM)
        count_lbl.pack(side='left')
        ttk.Button(bottom, text='Reset', style='Small.TButton', width=6,
                   command=lambda k=key: self._reset(k)).pack(side='right')

        self.fault_labels[key] = (light, circle, name_lbl, count_lbl, level)

    def _reset(self, key):
        self.management.fault_log.manual_reset(key)

    def _reset_all(self):
        self.management.fault_log.reset_all()

    def _tick(self):
        if not self.winfo_exists():
            return
        fault_snap = self.management.fault_log.snapshot()
        for key, entry in fault_snap.items():
            if key not in self.fault_labels:
                if not key.startswith('clamp_'):
                    continue   # shouldn't happen - FAULT_DEFINITIONS covers every non-clamp key
                self._build_fault_row(key, entry.get('label', key), entry.get('level', 'warn'))
        for key, (light, circle, name_lbl, count_lbl, level) in self.fault_labels.items():
            entry = fault_snap.get(key, {'count': 0, 'active': False})
            count = entry.get('count', 0)
            color = _LEVEL_COLOR.get(level, FG_DIM)
            if entry.get('active'):
                light.itemconfig(circle, fill=color, outline=color)
                count_lbl.configure(text=f'{count}x - ACTIVE', foreground=color)
            elif count > 0:
                light.itemconfig(circle, fill=FIELD, outline=color)
                count_lbl.configure(text=f'{count}x', foreground=FG_DIM)
            else:
                light.itemconfig(circle, fill=FG_DIM, outline=FG_DIM)
                count_lbl.configure(text='--', foreground=FG_DIM)
        self.after(300, self._tick)
