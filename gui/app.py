"""Main application window - three resizable panes (RZ450e battery / mapping
& management / Leaf emulator) plus a bottom log panel, per docs/08-gui-
design.md. Plain tkinter/ttk (no customtkinter - reverted 2026-07-31 per
user request: customtkinter's per-window DPI-scaling tracker and custom
canvas-based widgets made the app noticeably slower to load than the
reference apps' plain-tkinter style, which is what the user actually wants
back - see gui/theme.py)."""
import collections
import queue
import time
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

from bridge import config_profile
from bridge.can_backend import BusConnection
from bridge.mapping_engine import MappingEngine
from bridge.management_engine import ManagementEngine
from bridge.realtime_engine import RealtimeEngine
from bridge.state import SharedState
from gui.dashboard import DashboardWindow
from gui.fault_history_window import FaultHistoryWindow
from gui.info_popup import help_btn
from gui.theme import apply_style, ACC, BASE_FONT, BG, ERR, FG_DIM, FIELD, FG, OK
from gui.panels import (ConnectionsPanel, LiveMonitorPanel, MappingPanel, ManagementPanel,
                         GeneratedSignalsPanel, ChargeEmulationPanel, FuturePlaceholderPanel, VehiclePanel,
                         input_monitor_text, leaf_tx_monitor_text,
                         RZ450E_CONN_HELP, LEAF_CONN_HELP, BRIDGE_CONTROL_HELP)

BRIDGE_STATUS_STYLE = {
    'idle': ('Bridge: idle (not started)', FG_DIM),
    'waiting_for_wake': ('Bridge: armed - waiting for Leaf bus traffic', ACC),
    'startup': ('Bridge: running (staged startup)', OK),
    'running': ('Bridge: running', OK),
    'winding_down': ('Bridge: winding down', ACC),
    'stopped': ('Bridge: stopped (re-arming)', ERR),
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        apply_style(self)
        self.title('MiniLeaf Battery Bridge Translator')
        # Fixed position AND size (added 2026-07-31, user request - the
        # window previously had no explicit x/y, so the OS placed it
        # wherever, which could land it overlapping other windows).
        # Size scaled ~15% smaller (same pass, user request: "scale down the
        # main app as well to fit the same sizing... as the Dashboard") -
        # was 1680x980; position offset (+40+30) is a screen-placement
        # preference, not a size, so left unscaled.
        self.geometry('1430x835+40+30')

        self.log_queue = queue.Queue()
        # Formatted log lines, kept so the Dashboard window can mirror the
        # same log content (added 2026-07-31, user request) without draining
        # the same queue twice (which would just split messages between the
        # two windows instead of showing both the full history). Capped so a
        # very long session doesn't grow this unboundedly.
        self.log_lines = collections.deque(maxlen=2000)
        self.dashboard = None
        self.fault_window = None

        self.state_model = SharedState()
        profile = config_profile.load_profile()
        if profile:
            self.mapping, self.management = config_profile.apply_profile(profile, self.state_model)
        else:
            self.mapping, self.management = MappingEngine(), ManagementEngine()
        self.state_model.seed_last_known_good(config_profile.load_last_known_good())
        config_profile.load_fault_log(self.management)

        # RZ450e is one combined connection - one physical adapter carries
        # every ID (diagnostic/DID + the fast internal-bus broadcasts
        # together), split by CAN ID internally, matching this project's
        # actual hardware wiring (not two separate buses/adapters).
        self.rz_bus = BusConnection('rz450e')
        self.leaf_bus = BusConnection('leaf')
        self.rz_bus.log_fn = self.log
        self.leaf_bus.log_fn = self.log
        self.engine = RealtimeEngine(self.state_model, self.mapping, self.management,
                                      self.rz_bus, self.leaf_bus)
        self.engine.log_fn = self.log

        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)
        self.grid_columnconfigure(0, weight=1)

        # Classic tk.PanedWindow, not ttk.PanedWindow: it supports a per-pane
        # `minsize` (ttk's paned window does not), so no pane can ever be
        # squeezed to invisibility by a bad initial sash guess - a real bug
        # hit earlier with ttk.PanedWindow + a timing-dependent sashpos()
        # call (winfo_width() wasn't reliably valid yet when that fired,
        # collapsing the left/middle panes to ~0 width). Explicit width +
        # minsize at add-time removes the timing dependency entirely.
        self.paned = tk.PanedWindow(self, orient='horizontal', sashrelief='raised',
                                     sashwidth=6, bg=BG, bd=0)
        self.paned.grid(row=0, column=0, sticky='nsew', padx=2, pady=2)

        self._build_left()
        self._build_middle()
        self._build_right()
        self._build_log_panel()

        self.engine.start()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self._autosave_loop()
        self._drain_log()
        self.log('MiniLeaf Battery Bridge Translator started.')

    def log(self, msg):
        """Thread-safe - safe to call from any background thread (bus
        connections, the real-time engine). Ported pattern from the Leaf
        project's logq/_poll (queue.Queue drained on the Tk main thread)."""
        self.log_queue.put(str(msg))

    def _drain_log(self):
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            formatted = time.strftime('%H:%M:%S ') + line + '\n'
            self.log_lines.append(formatted)
            self.logbox.configure(state='normal')
            self.logbox.insert('end', formatted)
            self.logbox.see('end')
            self.logbox.configure(state='disabled')
        self.after(200, self._drain_log)

    def _build_log_panel(self):
        # Fault History moved into the Dashboard window's right column
        # (user request 2026-07-31, second pass) - back to a plain
        # full-width Log panel here.
        logf = ttk.LabelFrame(self, text='Log')
        logf.grid(row=1, column=0, sticky='ew', padx=2, pady=(0, 2))
        self.logbox = scrolledtext.ScrolledText(
            logf, height=8, state='disabled', wrap='word', font=BASE_FONT,
            bg=FIELD, fg=OK, insertbackground=FG, relief='flat')
        self.logbox.pack(fill='x', expand=True, padx=4, pady=4)

    # ── Left: RZ450e battery (inputs) ───────────────────────────────────
    def _build_left(self):
        left = ttk.Frame(self.paned)
        # stretch='never' (changed 2026-07-31, user request: "fix internal
        # window placement") - with all three panes set to 'always', Tk's
        # PanedWindow distributed extra window width unevenly between them
        # (observed: left pane rendering noticeably wider than its
        # configured width while the right pane stayed squeezed near its
        # minimum) - not a bug exactly, just not the fixed/predictable
        # layout wanted. Left and right now hold a constant width every
        # launch; only the middle (Configurator) pane absorbs extra window
        # width, since it's the one that benefits most from it (holds the
        # tabs). Still user-draggable via the sash - this only fixes the
        # STARTING layout, not the ability to resize.
        # Widths scaled ~15% smaller alongside the rest of the app (was
        # minsize=280, width=440).
        self.paned.add(left, minsize=238, width=374, stretch='never')
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(2, weight=1)
        ttk.Label(left, text='RZ450e BATTERY (inputs)', font=('Segoe UI', 14, 'bold')
                  ).grid(row=0, column=0, sticky='w', padx=8, pady=(8, 0))

        ConnectionsPanel(left, 'RZ450e connection (combined - one adapter, split by CAN ID)',
                          self.rz_bus, allow_listen_only=True, default_listen_only=False,
                          help_text=RZ450E_CONN_HELP
                          ).grid(row=1, column=0, sticky='ew', padx=4, pady=2)

        LiveMonitorPanel(left, 'Live decoded values', lambda: input_monitor_text(self.state_model)
                          ).grid(row=2, column=0, sticky='nsew', padx=4, pady=4)

    # ── Middle: mapping & management (the configurator) ────────────────
    def _build_middle(self):
        self.middle_frame = ttk.Frame(self.paned)
        # The one pane that still stretches (see _build_left's comment) -
        # sized so left(374) + middle(680) + right(374) = 1428, matching the
        # window's own fixed width (1430) almost exactly, so nothing is
        # squeezed on first render. Scaled ~15% smaller alongside the rest
        # of the app (was minsize=400, width=800).
        self.paned.add(self.middle_frame, minsize=340, width=680, stretch='always')
        self.middle_frame.grid_columnconfigure(0, weight=1)
        self.middle_frame.grid_rowconfigure(2, weight=1)

        top = ttk.Frame(self.middle_frame)
        top.grid(row=0, column=0, sticky='ew', padx=8, pady=(8, 0))
        ttk.Label(top, text='CONFIGURATOR', font=('Segoe UI', 14, 'bold')).pack(side='left')
        ttk.Button(top, text='Simulate power-down', command=self._simulate_power_down).pack(side='right', padx=8)
        ttk.Button(top, text='Load profile', command=self._load_profile).pack(side='right', padx=2)
        ttk.Button(top, text='Save profile', command=self._save_profile).pack(side='right', padx=2)
        ttk.Button(top, text='Fault History', command=self._open_fault_history).pack(side='right', padx=2)
        ttk.Button(top, text='Dashboard', command=self._open_dashboard).pack(side='right', padx=2)

        bridge_row = ttk.Frame(self.middle_frame)
        bridge_row.grid(row=1, column=0, sticky='ew', padx=8, pady=(4, 8))
        self.start_bridge_btn = ttk.Button(bridge_row, text='Start Bridge', command=self._start_bridge)
        self.start_bridge_btn.pack(side='left')
        self.stop_bridge_btn = ttk.Button(bridge_row, text='Stop Bridge', command=self._stop_bridge, state='disabled')
        self.stop_bridge_btn.pack(side='left', padx=(4, 0))
        help_btn(bridge_row, 'Start / Stop Bridge', BRIDGE_CONTROL_HELP).pack(side='left', padx=(6, 0))
        self.bridge_status_lbl = ttk.Label(bridge_row, text='Bridge: idle (not started)', foreground=FG_DIM)
        self.bridge_status_lbl.pack(side='left', padx=(12, 0))
        self._refresh_bridge_status()

        self.tabs_holder = ttk.Frame(self.middle_frame)
        self.tabs_holder.grid(row=2, column=0, sticky='nsew', padx=4, pady=(0, 4))
        self.tabs_holder.grid_columnconfigure(0, weight=1)
        self.tabs_holder.grid_rowconfigure(0, weight=1)
        self._populate_middle_tabs()

    def _populate_middle_tabs(self):
        for w in self.tabs_holder.winfo_children():
            w.destroy()
        tabs = ttk.Notebook(self.tabs_holder)
        tabs.grid(row=0, column=0, sticky='nsew')
        t_map = ttk.Frame(tabs)
        t_mgmt = ttk.Frame(tabs)
        t_gen = ttk.Frame(tabs)
        t_charge = ttk.Frame(tabs)
        t_future = ttk.Frame(tabs)
        tabs.add(t_map, text='Signal Mapping')
        tabs.add(t_mgmt, text='Battery Management')
        tabs.add(t_gen, text='Generated Signals')
        tabs.add(t_charge, text='Charge Emulation')
        tabs.add(t_future, text='Future: Battery Requests')

        MappingPanel(t_map, self.state_model, self.mapping).pack(fill='both', expand=True)
        ManagementPanel(t_mgmt, self.state_model, self.management).pack(fill='both', expand=True)
        GeneratedSignalsPanel(t_gen, self.state_model).pack(fill='both', expand=True)
        ChargeEmulationPanel(t_charge, self.state_model).pack(fill='both', expand=True)
        FuturePlaceholderPanel(t_future).pack(fill='both', expand=True)

    # ── Right: Leaf emulator (outputs) ──────────────────────────────────
    def _build_right(self):
        right = ttk.Frame(self.paned)
        self.paned.add(right, minsize=238, width=374, stretch='never')
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)
        ttk.Label(right, text='LEAF EMULATOR (outputs)', font=('Segoe UI', 14, 'bold')
                  ).grid(row=0, column=0, sticky='w', padx=8, pady=(8, 0))

        ConnectionsPanel(right, 'Leaf-facing CAN bus', self.leaf_bus,
                          allow_listen_only=False, help_text=LEAF_CONN_HELP
                          ).grid(row=1, column=0, sticky='ew', padx=4, pady=2)
        VehiclePanel(right, self.state_model).grid(row=2, column=0, sticky='ew', padx=4, pady=2)
        LiveMonitorPanel(right, 'Live transmitted values', lambda: leaf_tx_monitor_text(self.state_model)
                          ).grid(row=3, column=0, sticky='nsew', padx=4, pady=4)

    # ── Dashboard ────────────────────────────────────────────────────────
    def _open_dashboard(self):
        if self.dashboard is not None and self.dashboard.winfo_exists():
            self.dashboard.focus()
            return
        self.dashboard = DashboardWindow(self, self.state_model, self.mapping, self.management)

    def _open_fault_history(self):
        if self.fault_window is not None and self.fault_window.winfo_exists():
            self.fault_window.focus()
            return
        self.fault_window = FaultHistoryWindow(self, self.management)

    def _simulate_power_down(self):
        self.engine.request_shutdown()
        self.log('Manual power-down requested.')

    # ── Bridge start/stop ────────────────────────────────────────────────
    def _start_bridge(self):
        self.engine.start_bridge()
        self.start_bridge_btn.configure(state='disabled')
        self.stop_bridge_btn.configure(state='normal')

    def _stop_bridge(self):
        self.engine.stop_bridge()
        self.start_bridge_btn.configure(state='normal')
        self.stop_bridge_btn.configure(state='disabled')

    def _refresh_bridge_status(self):
        if not self.winfo_exists():
            return
        phase = self.engine.sequencer.phase
        text, color = BRIDGE_STATUS_STYLE.get(phase, (f'Bridge: {phase}', FG_DIM))
        self.bridge_status_lbl.configure(text=text, foreground=color)
        self.after(400, self._refresh_bridge_status)

    # ── Persistence ──────────────────────────────────────────────────────
    def _save_profile(self):
        path = filedialog.asksaveasfilename(defaultextension='.json',
                                             initialdir=config_profile.CONFIG_DIR,
                                             initialfile='profile.json')
        if path:
            config_profile.save_profile(self.state_model, self.mapping, self.management, path)
            self.log(f'Profile saved: {path}')

    def _load_profile(self):
        path = filedialog.askopenfilename(initialdir=config_profile.CONFIG_DIR)
        if not path:
            return
        profile = config_profile.load_profile(path)
        new_mapping, new_management = config_profile.apply_profile(profile, self.state_model)
        self.mapping.ties[:] = new_mapping.ties
        self.management.config = new_management.config
        self._populate_middle_tabs()
        self.log(f'Profile loaded: {path}')

    def _autosave_loop(self):
        config_profile.save_last_known_good(self.state_model)
        config_profile.save_fault_log(self.management)
        self.after(5000, self._autosave_loop)

    def _on_close(self):
        self.engine.stop()
        config_profile.save_last_known_good(self.state_model)
        config_profile.save_fault_log(self.management)
        config_profile.save_profile(self.state_model, self.mapping, self.management)
        self.destroy()
