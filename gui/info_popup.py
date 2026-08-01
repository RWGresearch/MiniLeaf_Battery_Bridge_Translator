"""The '?' conversion-math popup (docs/08-gui-design.md) - shows live input
value(s), the applied conversion, and the resulting output value, so the
user can visually confirm the math rather than trust a static description.
Ported style from leaf_hvbat_emulator.py's App._show_help (plain
tkinter.Toplevel, not customtkinter)."""
import tkinter as tk
from tkinter import ttk

from gui.theme import BASE_FONT, BG, FG, PANEL, FIELD


def show_info_popup(parent, title, text):
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=PANEL)
    win.geometry('480x320')
    win.transient(parent.winfo_toplevel())
    box = tk.Text(win, wrap='word', bg=FIELD, fg=FG, insertbackground=FG, relief='flat', font=BASE_FONT)
    box.pack(fill='both', expand=True, padx=10, pady=10)
    box.insert('1.0', text)
    box.configure(state='disabled')
    ttk.Button(win, text='Close', command=win.destroy).pack(pady=(0, 10))


def help_btn(parent, title, text):
    """A small '?' button that opens a contextual help popup - ported
    pattern from leaf_hvbat_emulator.py's App._help_btn/_show_help, used
    throughout that app next to nearly every panel/group. Returns the
    button so the caller can .pack()/.grid() it wherever fits."""
    return ttk.Button(parent, text='?', width=2, style='Small.TButton',
                       command=lambda: show_info_popup(parent, title, text))
