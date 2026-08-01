"""CAN adapter layer: PCAN-USB channel detection, a per-adapter RX/TX worker
thread, and an auto-reconnecting BusConnection wrapper.

Ported pattern (not code) from Refrance/RZ450e_battery_can_decode_Project/
rx450e_can_analyzer.py's detect_pcan_channels()/CANWorker, and the DEMO-mode
philosophy from Refrance/Leaf_BMS_Emulator/leaf_hvbat_emulator.py (CAN_AVAILABLE
gate, degrade gracefully with no hardware/no python-can installed).
"""
import queue
import threading
import time

try:
    import can
    CAN_AVAILABLE = True
except ImportError:
    CAN_AVAILABLE = False

CAN_INTERFACE = 'pcan'
BITRATE = 500_000
RECONNECT_INTERVAL_S = 3.0


def detect_pcan_channels():
    """PCAN_USBBUSx channel names currently visible to the PCAN-Basic driver,
    so up to 8 adapters are selectable. Falls back to the first 8 slots by
    name if nothing is detected yet (adapter plugged in after launch, or
    python-can/PCANBasic unavailable) so the dropdown is never empty."""
    fallback = [f'PCAN_USBBUS{i}' for i in range(1, 9)]
    if not CAN_AVAILABLE:
        return fallback
    try:
        cfgs = can.detect_available_configs(interfaces=['pcan'])
        chans = sorted({c['channel'] for c in cfgs if c.get('channel')})
        return chans or fallback
    except Exception:
        return fallback


class CanWorker(threading.Thread):
    """Owns one PCAN-USB adapter. Puts ('rx', label, msg) / ('err', label, msg)
    tuples on the shared rx_queue. listen_only prevents transmit at the
    adapter level (no ACK) - useful for a pure sniff connection."""

    def __init__(self, channel, rx_queue, label, listen_only=False):
        super().__init__(daemon=True, name=f'CAN-{label}')
        self.channel = channel
        self.label = label
        self._q = rx_queue
        self._bus = None
        self._stop_evt = threading.Event()
        self.connected = False
        self.error = None
        self.listen_only = listen_only

    def run(self):
        if not CAN_AVAILABLE:
            self.error = 'python-can not installed'
            return
        try:
            kwargs = {'auto_reset': True}
            if self.listen_only:
                kwargs['state'] = can.BusState.PASSIVE
            self._bus = can.Bus(interface=CAN_INTERFACE, channel=self.channel,
                                 bitrate=BITRATE, **kwargs)
            self.connected = True
            self.error = None
        except Exception as exc:
            self.error = str(exc)
            return

        while not self._stop_evt.is_set():
            try:
                msg = self._bus.recv(timeout=0.1)
                if msg:
                    self._q.put(('rx', self.label, msg))
            except Exception as exc:
                self.error = str(exc)
                self._q.put(('err', self.label, str(exc)))
                break

        if self._bus:
            try:
                self._bus.shutdown()
            except Exception:
                pass
        self.connected = False

    def stop(self):
        self._stop_evt.set()

    def send(self, arb_id, data, extended=False):
        if not self.connected or not self._bus or self.listen_only:
            return False
        try:
            msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=extended)
            self._bus.send(msg)
            return True
        except Exception as exc:
            self.error = str(exc)
            self._q.put(('err', self.label, f'TX: {exc}'))
            return False


class DemoWorker:
    """Hardware-free stand-in used when python-can/adapters aren't available,
    or the user explicitly wants DEMO mode. Mirrors CanWorker's public surface
    (connected/error/send/stop) but never actually sends or receives anything
    real - the mapping/management/GUI layers still exercise fully against
    whatever last-known-good/generated values are in the state model."""

    def __init__(self, channel, rx_queue, label, listen_only=False):
        self.channel = channel
        self.label = label
        self.connected = True
        self.error = None
        self.listen_only = listen_only
        self._q = rx_queue

    def start(self):
        pass

    def stop(self):
        self.connected = False

    def is_alive(self):
        return self.connected

    def send(self, arb_id, data, extended=False):
        return not self.listen_only


class BusConnection:
    """One logical bus slot (e.g. 'rz450e_bus1', 'leaf_bus') with a selectable
    PCAN channel and auto-reconnect. Owns a rx_queue that callers drain for
    ('rx'|'err', label, payload) events."""

    def __init__(self, label, demo=False):
        self.label = label
        self.demo = demo
        self.rx_queue = queue.Queue()
        self.channel = None
        self.listen_only = False
        self._worker = None
        self._want_connected = False
        self._monitor_thread = None
        self._stop_monitor = threading.Event()
        self.log_fn = lambda msg: None   # replaced by the GUI with the log panel's put()

    @property
    def connected(self):
        return bool(self._worker and self._worker.connected)

    @property
    def error(self):
        return self._worker.error if self._worker else None

    def connect(self, channel, listen_only=False):
        self.channel = channel
        self.listen_only = listen_only
        self._want_connected = True
        self._start_worker()
        if not self._monitor_thread or not self._monitor_thread.is_alive():
            self._stop_monitor.clear()
            self._monitor_thread = threading.Thread(
                target=self._auto_reconnect_loop, daemon=True,
                name=f'Reconnect-{self.label}')
            self._monitor_thread.start()

    def disconnect(self):
        self._want_connected = False
        self._stop_monitor.set()
        if self._worker:
            self._worker.stop()
            self._worker = None
        self.log_fn(f'{self.label}: disconnected')

    def _start_worker(self):
        cls = DemoWorker if (self.demo or not CAN_AVAILABLE) else CanWorker
        self._worker = cls(self.channel, self.rx_queue, self.label,
                            listen_only=self.listen_only)
        self._worker.start()
        # give the worker thread a moment to attempt the real connection
        # before logging the outcome (CanWorker.run() connects synchronously
        # at thread start, so a short wait is enough for the common case).
        time.sleep(0.15)
        if self._worker.connected:
            self.log_fn(f'{self.label}: connected on {self.channel}'
                        + (' (listen only)' if self.listen_only else ''))
        elif self._worker.error:
            self.log_fn(f'{self.label}: connect failed on {self.channel} - {self._worker.error}')

    def _auto_reconnect_loop(self):
        while not self._stop_monitor.is_set():
            time.sleep(RECONNECT_INTERVAL_S)
            if not self._want_connected:
                continue
            if self._worker and not self._worker.connected and self._worker.error:
                self.log_fn(f'{self.label}: attempting reconnect on {self.channel}')
                self._start_worker()

    def send(self, arb_id, data, extended=False):
        if self._worker:
            return self._worker.send(arb_id, data, extended=extended)
        return False
