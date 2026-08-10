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


def no_wheel(combo):
    """Prevent mouse-wheel scrolling from silently changing a readonly
    Combobox's selection (added 2026-08-01, user report: "when I'm
    scrolling with the wheel I have accidentally changed the inputs/
    outputs... this data should only be valid if selected with a mouse
    pointer and click"; moved here from gui/panels.py 2026-08-06 so
    gui/dashboard.py's scale dropdowns - which sit inside the same kind of
    scrollable VScrollFrame - can share it instead of duplicating it). An
    instance-level binding fires before both the Combobox's own class
    binding (which changes the value on wheel scroll) and VScrollFrame's
    page-scroll bind_all handler below - so returning "break" here stops
    both; only an explicit click can change the selection now."""
    for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
        combo.bind(seq, lambda e: 'break')

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

# Monospace variant (added 2026-08-06) - used ONLY by the label:value
# 'live data' Text widgets (gui/panels.py's LiveMonitorPanel,
# gui/dashboard.py's right column). Alignment itself now comes from a
# right-aligned Tk tab stop (see write_field()/configure_field_tags()
# below), which works in any font - this is kept as a deliberate look for
# these specific data-table-style boxes, distinct from BASE_FONT's use for
# prose (Log/help text) elsewhere.
MONO_FONT = ('Consolas', 8)


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
    # Warn.TCombobox (added 2026-08-03, docs/13 item 4.3): flags a mapping
    # tie that references a key no longer present in the current signal
    # registry (orphaned/renamed) - visually distinct from the ordinary
    # "(unused)" blank state, which uses the plain TCombobox style above.
    st.configure('Warn.TCombobox', fieldbackground=FIELD, background=PANEL,
                 foreground=ERR, arrowcolor=ERR)
    st.map('Warn.TCombobox', fieldbackground=[('readonly', FIELD)],
           foreground=[('readonly', ERR)])
    st.configure('Vertical.TScrollbar', background=PANEL, troughcolor=BG, arrowcolor=FG)
    st.configure('Horizontal.TScrollbar', background=PANEL, troughcolor=BG, arrowcolor=FG)
    st.configure('TPanedwindow', background=BG)
    for pat, val in (('*TCombobox*Listbox*Background', PANEL),
                     ('*TCombobox*Listbox*Foreground', FG),
                     ('*TCombobox*Listbox*selectBackground', ACC),
                     ('*TCombobox*Listbox*selectForeground', BG)):
        root.option_add(pat, val)


def configure_field_tags(box):
    """Configures the tag write_section() below relies on, AND a single
    right-aligned tab stop write_field() below relies on to push every
    value flush against the box's actual right edge - shared by every
    'live data as tagged Text' panel (gui/panels.py's LiveMonitorPanel and
    gui/dashboard.py's right column). Call once per Text widget, right
    after creating it.

    Fixed-character-count padding (label:<40 value:>12, the previous
    approach) assumed a specific box width in characters; whenever the
    real box was narrower than that guess (a resized pane, the Dashboard's
    RIGHT_COL_W column), lines that should have fit were wrapping anyway,
    and when it was wider, label and value sat closer together than
    intended (user reports: "the data boxes look close... its still
    wraping incorectly. some data output is wraping"). A tab stop is
    measured in actual pixels against the box's OWN winfo_width(), so it
    tracks whatever the box's real size is - a label only pushes its value
    onto a second line when the label itself is genuinely too long to fit
    the box, never as an artifact of a wrong guessed character count.
    Re-measured on every <Configure> (resize) so a resizable pane (the
    main window's PanedWindow-based live panels) keeps the value flush
    right after the user drags a sash."""
    box.tag_configure('hdr', foreground=ACC)

    def _retab(event=None):
        w = box.winfo_width()
        if w > 20:
            box.configure(tabs=f'{w - 10} right')

    box.bind('<Configure>', _retab)
    _retab()


def write_section(box, title):
    box.insert('end', f'{title}\n', 'hdr')


def write_field(box, label, value):
    """One field as a single label:value line - the label starts flush
    left, a tab jumps to configure_field_tags()'s right-aligned stop, and
    the value lands flush against the box's actual right edge. Requires
    configure_field_tags(box) to have been called on this widget first."""
    box.insert('end', f'{label}\t{value}\n')


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
