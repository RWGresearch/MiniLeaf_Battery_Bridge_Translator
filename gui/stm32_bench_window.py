"""STM32 Bench Replay - GUI wrapper around tools/stm32_bench_replay.py
(Phase 7's bench validation test), so it doesn't require a terminal. Opens
as its own popup window (same tk.Toplevel pattern as gui/dashboard.py/
gui/fault_history_window.py), added 2026-08-18 alongside the "Export STM32
Config" button per user request; capture support added the same day once
the user had real bench hardware ready (two PCAN adapters: one wired to the
board's CAN1 for injection, one to CAN2 to record its real output).

Deliberately SEPARATE CAN connections from the main app's own rz_bus/
leaf_bus (bridge/can_backend.py's BusConnection, same class, new instances) -
these drive different physical adapters wired to the STM32 board's bench
headers, not the live RZ450e/Leaf buses this app bridges day to day.
Progress/status messages are routed into the main app's own Log panel (the
`log_fn` passed in), not a separate log box here, so there's one place to
watch regardless of which window has focus.
"""
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from bridge import can_backend
from gui.theme import apply_style, no_wheel, BASE_FONT, ACC, ERR, FG_DIM, OK
from tools import stm32_bench_replay


class Stm32BenchReplayWindow(tk.Toplevel):
    def __init__(self, master, log_fn):
        super().__init__(master)
        apply_style(self)
        self.title('STM32 Bench Replay')
        x = master.winfo_x() + master.winfo_width() + 10
        y = master.winfo_y()
        self.geometry(f'560x520+{x}+{y}')
        self.log_fn = log_fn

        self._loaded = None          # (broadcast_frames, did_responses, leaf_track) once a .trc is parsed
        self._trc_path = None
        self._stop_event = None
        self._replay_thread = None
        self._load_thread = None

        self.trc_path_var = tk.StringVar(value='(no capture selected)')
        self.channel_var = tk.StringVar(value='')
        self.speed_var = tk.StringVar(value='1.0')
        self.responder_var = tk.BooleanVar(value=True)
        self.capture_var = tk.BooleanVar(value=True)
        self.capture_channel_var = tk.StringVar(value='')
        self.capture_output_var = tk.StringVar(value='')
        self.wake_frame_var = tk.BooleanVar(value=True)
        self.leaf_track_var = tk.BooleanVar(value=True)
        self.test_wind_down_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value='Select a .trc capture to begin.')

        self._build()
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    def _build(self):
        head = ttk.Frame(self)
        head.pack(fill='x', padx=8, pady=(8, 4))
        ttk.Label(head, text='STM32 Bench Replay', style='Header.TLabel').pack(side='left')

        trc_row = ttk.Frame(self)
        trc_row.pack(fill='x', padx=8, pady=2)
        ttk.Button(trc_row, text='Browse .trc...', command=self._browse_trc).pack(side='left')
        ttk.Label(trc_row, textvariable=self.trc_path_var, foreground=FG_DIM, wraplength=360
                  ).pack(side='left', padx=(8, 0))

        # ── Inject (CAN1) ──
        inj_head = ttk.Frame(self)
        inj_head.pack(fill='x', padx=8, pady=(10, 0))
        ttk.Label(inj_head, text='INJECT - real RZ450e traffic onto CAN1', foreground=ACC).pack(anchor='w')

        chan_row = ttk.Frame(self)
        chan_row.pack(fill='x', padx=8, pady=2)
        ttk.Label(chan_row, text='CAN1 channel:', width=14).pack(side='left')
        self.channels = can_backend.detect_pcan_channels()
        if self.channels:
            self.channel_var.set(self.channels[0])
        self.channel_menu = ttk.Combobox(chan_row, values=self.channels, textvariable=self.channel_var,
                                          width=16, state='readonly')
        no_wheel(self.channel_menu)
        self.channel_menu.pack(side='left', padx=(0, 4))
        ttk.Button(chan_row, text='Rescan', style='Small.TButton', command=self._rescan).pack(side='left', padx=(0, 4))
        self.inject_identify_btn = ttk.Button(chan_row, text='Identify', style='Small.TButton',
                                               command=lambda: self._identify('inject'))
        self.inject_identify_btn.pack(side='left')

        opt_row = ttk.Frame(self)
        opt_row.pack(fill='x', padx=8, pady=2)
        ttk.Label(opt_row, text='Speed:').pack(side='left')
        ttk.Entry(opt_row, textvariable=self.speed_var, width=6).pack(side='left', padx=(4, 12))
        ttk.Checkbutton(opt_row, text='UDS responder (answer live DID requests with cached responses)',
                         variable=self.responder_var).pack(side='left')

        # ── Capture (CAN2) ──
        cap_head = ttk.Frame(self)
        cap_head.pack(fill='x', padx=8, pady=(10, 0))
        ttk.Checkbutton(cap_head, text='CAPTURE - record the board\'s real output on CAN2 (needs a 2nd adapter)',
                         variable=self.capture_var, command=self._on_capture_toggle).pack(anchor='w')

        cap_chan_row = ttk.Frame(self)
        cap_chan_row.pack(fill='x', padx=8, pady=2)
        ttk.Label(cap_chan_row, text='CAN2 channel:', width=14).pack(side='left')
        self.capture_channels = can_backend.detect_pcan_channels()
        default_capture = next((c for c in self.capture_channels if c != self.channel_var.get()),
                                self.capture_channels[0] if self.capture_channels else '')
        self.capture_channel_var.set(default_capture)
        self.capture_channel_menu = ttk.Combobox(cap_chan_row, values=self.capture_channels,
                                                  textvariable=self.capture_channel_var, width=16, state='readonly')
        no_wheel(self.capture_channel_menu)
        self.capture_channel_menu.pack(side='left', padx=(0, 4))
        ttk.Button(cap_chan_row, text='Rescan', style='Small.TButton', command=self._rescan).pack(side='left', padx=(0, 4))
        self.capture_identify_btn = ttk.Button(cap_chan_row, text='Identify', style='Small.TButton',
                                                command=lambda: self._identify('capture'))
        self.capture_identify_btn.pack(side='left')

        cap_out_row = ttk.Frame(self)
        cap_out_row.pack(fill='x', padx=8, pady=2)
        ttk.Label(cap_out_row, text='Output .trc:', width=14).pack(side='left')
        self.capture_output_entry = ttk.Entry(cap_out_row, textvariable=self.capture_output_var, width=28)
        self.capture_output_entry.pack(side='left', padx=(0, 4), fill='x', expand=True)
        ttk.Button(cap_out_row, text='Browse...', style='Small.TButton',
                   command=self._browse_capture_output).pack(side='left')

        cap_leaf_track_row = ttk.Frame(self)
        cap_leaf_track_row.pack(fill='x', padx=8, pady=2)
        self.leaf_track_check = ttk.Checkbutton(
            cap_leaf_track_row,
            text='Replay this capture\'s own real Leaf-bus traffic on CAN2, if it has any (genuine '
                 'startup/keep-alive/wind-down, better than a synthetic wake frame)',
            variable=self.leaf_track_var)
        self.leaf_track_check.pack(side='left')

        cap_wake_row = ttk.Frame(self)
        cap_wake_row.pack(fill='x', padx=8, pady=2)
        self.wake_frame_check = ttk.Checkbutton(
            cap_wake_row,
            text='Send a synthetic CAN2 wake frame instead (fallback - used only if this capture has '
                 'no real Leaf-bus traffic, or the option above is unchecked)',
            variable=self.wake_frame_var)
        self.wake_frame_check.pack(side='left')

        cap_wind_down_row = ttk.Frame(self)
        cap_wind_down_row.pack(fill='x', padx=8, pady=2)
        wait_s = stm32_bench_replay.default_wind_down_wait_s(log_fn=lambda _m: None)
        self.wind_down_check = ttk.Checkbutton(
            cap_wind_down_row,
            text=(f'Test natural wind-down/re-arm after replay (adds ~{wait_s:.0f}s: goes silent on CAN2, '
                  f'then sends a fresh wake to check the board clears its own latches)'),
            variable=self.test_wind_down_var)
        self.wind_down_check.pack(side='left')

        status_row = ttk.Frame(self)
        status_row.pack(fill='x', padx=8, pady=(10, 2))
        ttk.Label(status_row, textvariable=self.status_var, foreground=ACC, wraplength=460, justify='left'
                  ).pack(anchor='w')

        btn_row = ttk.Frame(self)
        btn_row.pack(fill='x', padx=8, pady=8)
        self.start_btn = ttk.Button(btn_row, text='Start Replay', command=self._start_replay, state='disabled')
        self.start_btn.pack(side='left')
        self.stop_btn = ttk.Button(btn_row, text='Stop', command=self._stop_replay, state='disabled')
        self.stop_btn.pack(side='left', padx=(4, 0))

        note = ttk.Label(
            self,
            text=('Replays a captured .trc\'s real RZ450e broadcast traffic with real inter-frame '
                  'timing and answers the board\'s live UDS/DID requests with that session\'s cached '
                  'responses. With CAPTURE on, simultaneously records everything the board transmits '
                  'on CAN2 to a new .trc - this is the "board\'s real output" you diff against what '
                  'bridge/ itself computes for the same input. Progress appears in the main Log panel.'),
            foreground=FG_DIM, wraplength=460, justify='left')
        note.pack(fill='x', padx=8, pady=(4, 8))

        self._on_capture_toggle()

    def _on_capture_toggle(self):
        state = 'readonly' if self.capture_var.get() else 'disabled'
        entry_state = 'normal' if self.capture_var.get() else 'disabled'
        self.capture_channel_menu.configure(state=state)
        self.capture_output_entry.configure(state=entry_state)
        self.leaf_track_check.configure(state=entry_state)
        self.wake_frame_check.configure(state=entry_state)
        self.wind_down_check.configure(state=entry_state)

    # ── .trc loading (parsing a large capture can take a while - background thread) ──
    def _browse_trc(self):
        path = filedialog.askopenfilename(
            filetypes=[('PCAN Trace', '*.trc'), ('All', '*.*')], initialdir='logs')
        if not path:
            return
        self._trc_path = path
        self.trc_path_var.set(os.path.basename(path))
        self.capture_output_var.set(stm32_bench_replay.default_capture_output_path(path))
        self._loaded = None
        self.start_btn.configure(state='disabled')
        self.status_var.set(f'Loading {os.path.basename(path)}... (large captures can take a while)')
        self._load_thread = threading.Thread(target=self._load_worker, args=(path,), daemon=True)
        self._load_thread.start()

    def _browse_capture_output(self):
        initial = self.capture_output_var.get() or 'logs'
        path = filedialog.asksaveasfilename(
            defaultextension='.trc', initialdir=os.path.dirname(initial) or 'logs',
            initialfile=os.path.basename(initial) or 'board_output.trc',
            filetypes=[('PCAN Trace', '*.trc'), ('All', '*.*')])
        if path:
            self.capture_output_var.set(path)

    def _load_worker(self, path):
        try:
            broadcast_frames, did_responses, leaf_track = stm32_bench_replay.load_capture(path, log_fn=self.log_fn)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            self.after(0, self._on_load_error, str(exc))
            return
        self.after(0, self._on_load_done, broadcast_frames, did_responses, leaf_track)

    def _on_load_done(self, broadcast_frames, did_responses, leaf_track):
        self._loaded = (broadcast_frames, did_responses, leaf_track)
        duration_s = broadcast_frames[-1][0] if broadcast_frames else 0.0
        leaf_note = (f' Real Leaf-bus traffic available: {len(leaf_track)} frame(s), '
                     f'{leaf_track[0][0]:.1f}s-{leaf_track[-1][0]:.1f}s.' if leaf_track
                     else ' No real Leaf-bus traffic in this capture - will use the synthetic wake frame.')
        self.status_var.set(
            f'Loaded: {len(broadcast_frames)} broadcast frames spanning {duration_s:.1f}s, '
            f'{len(did_responses)} DID(s) with a cached response.{leaf_note}')
        self.start_btn.configure(state='normal')
        self.log_fn(f'STM32 Bench Replay: loaded {self.trc_path_var.get()} '
                    f'({len(broadcast_frames)} frames, {len(did_responses)} DIDs, '
                    f'{len(leaf_track)} real Leaf-bus frame(s))')

    def _on_load_error(self, message):
        self.status_var.set(f'Failed to load capture: {message}')
        self.log_fn(f'STM32 Bench Replay: failed to load capture - {message}')

    def _rescan(self):
        channels = can_backend.detect_pcan_channels()
        self.channels = channels
        self.capture_channels = channels
        self.channel_menu.configure(values=channels)
        self.capture_channel_menu.configure(values=channels)

    # ── Identify (briefly listens on a channel to confirm what's wired there) ──
    def _identify(self, which):
        """Runs stm32_bench_replay.identify_channel() against the CAN1 or
        CAN2 channel currently selected - lets a wiring mixup be caught
        BEFORE starting a real replay, instead of only being visible
        afterward in a captured file (see the 2026-08-18 board-output
        capture that turned out to be all UDS 0x747 REQUESTs - the adapter
        the user had wired as "CAN2 capture" was actually on the battery/
        CAN1 side)."""
        channel = self.channel_var.get() if which == 'inject' else self.capture_channel_var.get()
        if not channel:
            messagebox.showerror('STM32 Bench Replay', 'No channel selected to identify.', parent=self)
            return
        btn = self.inject_identify_btn if which == 'inject' else self.capture_identify_btn
        btn.configure(state='disabled', text='Listening...')
        self.log_fn(f'STM32 Bench Replay: identifying {channel} '
                    f'({stm32_bench_replay.IDENTIFY_LISTEN_S:.0f}s)...')
        threading.Thread(target=self._identify_worker, args=(which, channel, btn), daemon=True).start()

    def _identify_worker(self, which, channel, btn):
        result = stm32_bench_replay.identify_channel(channel, self.log_fn)
        self.after(0, self._on_identify_done, which, channel, btn, result)

    def _on_identify_done(self, which, channel, btn, result):
        btn.configure(state='normal', text='Identify')
        if result is None:
            messagebox.showerror('STM32 Bench Replay', f'Could not connect to {channel}.', parent=self)
            return
        counts, verdict = result
        expected = 'CAN1 (battery/RZ450e)' if which == 'inject' else 'CAN2 (vehicle/Leaf)'
        detail = ', '.join(f'{k}={v}' for k, v in counts.items() if v)
        messagebox.showinfo(
            'STM32 Bench Replay - Identify',
            f'{channel} (selected as {expected}):\n\n{verdict}\n\n' + (detail if detail else 'No frames seen.'),
            parent=self)

    # ── Replay (real-time, background thread - can run for minutes) ──
    def _start_replay(self):
        if not self._loaded:
            return
        channel = self.channel_var.get()
        if not channel:
            messagebox.showerror('STM32 Bench Replay', 'No CAN1 channel selected.', parent=self)
            return
        try:
            speed = float(self.speed_var.get())
            if speed <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror('STM32 Bench Replay', 'Speed must be a positive number.', parent=self)
            return

        use_capture = self.capture_var.get()
        capture_channel = self.capture_channel_var.get()
        capture_output = self.capture_output_var.get()
        if use_capture:
            if not capture_channel:
                messagebox.showerror('STM32 Bench Replay', 'No CAN2 capture channel selected.', parent=self)
                return
            if capture_channel == channel:
                messagebox.showerror('STM32 Bench Replay',
                                      'CAN1 and CAN2 channels must be different adapters.', parent=self)
                return
            if not capture_output:
                messagebox.showerror('STM32 Bench Replay', 'No capture output path set.', parent=self)
                return

        broadcast_frames, did_responses, leaf_track = self._loaded
        use_leaf_track = bool(leaf_track) and self.leaf_track_var.get()
        self._stop_event = threading.Event()
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        if use_capture:
            self.status_var.set(f'Replaying on {channel} at {speed}x, capturing on {capture_channel}...')
            self.log_fn(f'STM32 Bench Replay: starting on {channel} at {speed}x '
                        f'({"with" if self.responder_var.get() else "without"} UDS responder), '
                        f'capturing board output on {capture_channel} -> {capture_output}')
        else:
            self.status_var.set(f'Replaying on {channel} at {speed}x...')
            self.log_fn(f'STM32 Bench Replay: starting on {channel} at {speed}x '
                        f'({"with" if self.responder_var.get() else "without"} UDS responder)')
        self._replay_thread = threading.Thread(
            target=self._replay_worker,
            args=(broadcast_frames, did_responses, channel, speed, self.responder_var.get(),
                  use_capture, capture_channel, capture_output, self.wake_frame_var.get(),
                  leaf_track if use_leaf_track else None, self.test_wind_down_var.get(), self._stop_event),
            daemon=True)
        self._replay_thread.start()
        self._poll_replay_done()

    def _replay_worker(self, broadcast_frames, did_responses, channel, speed, use_responder,
                        use_capture, capture_channel, capture_output, send_wake, leaf_track, test_wind_down,
                        stop_event):
        if use_capture:
            stm32_bench_replay.replay_and_capture(
                broadcast_frames, did_responses, channel, capture_channel, capture_output,
                speed, use_responder, self.log_fn, stop_event=stop_event, send_wake=send_wake,
                leaf_track=leaf_track, test_wind_down=test_wind_down)
        else:
            stm32_bench_replay.run_replay(broadcast_frames, did_responses, channel, speed,
                                           use_responder, self.log_fn, stop_event=stop_event)

    def _poll_replay_done(self):
        if not self.winfo_exists():
            return
        if self._replay_thread is not None and self._replay_thread.is_alive():
            self.after(300, self._poll_replay_done)
            return
        self._replay_thread = None
        self._stop_event = None
        self.start_btn.configure(state='normal')
        self.stop_btn.configure(state='disabled')
        self.status_var.set('Replay finished - see the main Log panel for the summary.')

    def _stop_replay(self):
        if self._stop_event is not None:
            self._stop_event.set()
            self.status_var.set('Stopping...')
            self.stop_btn.configure(state='disabled')

    def _on_close(self):
        if self._replay_thread is not None and self._replay_thread.is_alive():
            if not messagebox.askyesno('STM32 Bench Replay',
                                        'A replay is still running - stop it and close this window?',
                                        parent=self):
                return
            self._stop_replay()
        self.destroy()
