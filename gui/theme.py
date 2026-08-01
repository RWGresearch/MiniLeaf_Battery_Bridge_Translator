"""Dark ttk theme + a reusable scrollable frame, plain tkinter/ttk only (no
customtkinter). Ported color palette and `_style()` approach from
Refrance/Leaf_BMS_Emulator/leaf_hvbat_emulator.py - reverted to this per user
request (2026-07-31 second review): customtkinter's per-window DPI-scaling
tracker and custom canvas-based widget rendering made the app noticeably
slower to load than the reference apps' plain-tkinter style, which is what
the user actually wants back.
"""
import tkinter as tk
from tkinter import ttk

BG, PANEL, FIELD = '#1e1e22', '#2a2a30', '#35353c'
FG, FG_DIM, ACC, ERR, OK = '#dcdcdc', '#9a9aa0', '#4da3ff', '#ff6b6b', '#5fd38d'

BAR_W, BAR_H = 170, 12

# Shared base font (added 2026-07-31, user request: main app fonts weren't
# actually shrinking in a few spots even after apply_style()'s ~15% scale-
# down). ttk widgets pick up apply_style()'s '.' style font automatically,
# but plain tk.Text/scrolledtext.ScrolledText widgets (LiveMonitorPanel's
# live-data boxes, the main window's and popups' Log/help text) are NOT ttk
# widgets and do NOT participate in ttk styling at all - they silently kept
# rendering at Tk's system default font (~9pt) regardless of this theme,
# which is exactly why content stopped fitting in the now-narrower panes
# after the main app was scaled down (the surrounding window/pane shrank,
# but that text never did). Import this and pass font=BASE_FONT explicitly
# to every such widget - confirmed via winfo_reqheight()/winfo_reqwidth()
# that this is the only way to actually shrink a plain tk.Text widget.
BASE_FONT = ('Segoe UI', 8)


def apply_style(root):
    """Ported from leaf_hvbat_emulator.py's App._style()."""
    root.configure(bg=BG)
    st = ttk.Style(root)
    try:
        st.theme_use('clam')
    except tk.TclError:
        pass
    # Base/Header/Accent fonts shrunk ~15% (added 2026-07-31, user request:
    # "scale down the main app as well to fit the same sizing for font and
    # everything [as the Dashboard]") - these three now match the sizes
    # gui/dashboard.py's DASH_FONT/DASH_HEADER_FONT/DASH_ACCENT_FONT already
    # used locally (that window couldn't touch the shared theme without also
    # affecting the main app, which is exactly what's being done here now).
    # Explicitly setting a base font on '.' also gives every ttk widget that
    # doesn't set its own font (buttons, checkboxes, entries, comboboxes,
    # tabs) a real, scaled-down baseline instead of silently inheriting
    # whatever Tk's system default happens to be.
    st.configure('.', background=BG, foreground=FG, fieldbackground=FIELD,
                 bordercolor='#444', lightcolor=PANEL, darkcolor=BG,
                 font=BASE_FONT)
    st.configure('TFrame', background=BG)
    st.configure('TLabel', background=BG, foreground=FG)
    st.configure('Header.TLabel', background=BG, foreground=FG, font=('Segoe UI', 12, 'bold'))
    st.configure('Dim.TLabel', background=BG, foreground=FG_DIM)
    st.configure('Accent.TLabel', background=BG, foreground=ACC, font=('Segoe UI', 9, 'bold'))
    st.configure('TLabelframe', background=BG, bordercolor='#49494f')
    st.configure('TLabelframe.Label', background=BG, foreground=ACC)
    st.configure('TButton', background=PANEL, foreground=FG, padding=3)
    st.map('TButton', background=[('active', '#3a3a42')])
    st.configure('Small.TButton', background=PANEL, foreground=FG, padding=(3, 0))
    st.map('Small.TButton', background=[('active', '#3a3a42')])
    st.configure('TCheckbutton', background=BG, foreground=FG)
    st.map('TCheckbutton', background=[('active', BG)])
    st.configure('TEntry', fieldbackground=FIELD, foreground=FG)
    st.configure('TNotebook', background=BG, bordercolor='#444')
    st.configure('TNotebook.Tab', background=PANEL, foreground=FG, padding=(7, 3))
    st.map('TNotebook.Tab', background=[('selected', BG)], foreground=[('selected', ACC)])
    st.configure('TCombobox', fieldbackground=FIELD, background=PANEL,
                 foreground=FG, arrowcolor=FG)
    st.map('TCombobox', fieldbackground=[('readonly', FIELD)],
           foreground=[('readonly', FG)])
    st.configure('Vertical.TScrollbar', background=PANEL, troughcolor=BG, arrowcolor=FG)
    st.configure('Horizontal.TScrollbar', background=PANEL, troughcolor=BG, arrowcolor=FG)
    st.configure('TPanedwindow', background=BG)
    for pat, val in (('*TCombobox*Listbox*Background', PANEL),
                     ('*TCombobox*Listbox*Foreground', FG),
                     ('*TCombobox*Listbox*selectBackground', ACC),
                     ('*TCombobox*Listbox*selectForeground', BG)):
        root.option_add(pat, val)


class VScrollFrame(ttk.Frame):
    """A vertically-scrollable frame (ttk has no native equivalent to
    customtkinter's CTkScrollableFrame). Put child widgets in `.inner`, not
    in the VScrollFrame itself."""

    def __init__(self, master, label_text=None, **kw):
        super().__init__(master, **kw)
        if label_text:
            ttk.Label(self, text=label_text, style='Accent.TLabel').pack(anchor='w', padx=4, pady=(2, 4))
        canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient='vertical', command=canvas.yview)
        self.inner = ttk.Frame(canvas)
        self.inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        self._window = canvas.create_window((0, 0), window=self.inner, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(self._window, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.canvas = canvas

        def _wheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), 'units')

        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', _wheel))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))
