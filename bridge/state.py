"""Shared live-state model: RZ450e input values (RX-side writes), the
generated/mapping-target values actually transmitted to the Leaf (TX-side
writes, for the GUI live monitor), and the known-good persisted cache that
survives restarts (docs/06-realtime-engine-and-watchdog.md).

This is intentionally the ONLY place both the ingest side and the TX side
touch shared data - everything is behind one lock so the real-time engine's
"RX updates state, TX reads state on its own schedule" split
(docs/06, section 1) has a single, simple point of synchronization.
"""
import threading
import time

from bridge import leaf_signals


class SharedState:
    def __init__(self):
        self.lock = threading.RLock()

        # RZ450e inputs: key -> latest physical value
        self.rz450e = {}
        self.rz450e_ts = {}              # key -> monotonic time of last update
        self._counter_last_value = {}    # counter key -> last raw value seen
        self._counter_last_change_ts = {}  # counter key -> when it last actually changed
        self._session_start = time.monotonic()

        # Leaf outputs actually transmitted (post mapping + management), for
        # the GUI's live TX monitor. Not authoritative - the frame builders
        # in leaf_signals.py are what's actually sent.
        self.leaf_tx = {}
        self.leaf_tx_ts = {}

        # Known-good cache: last real value seen for every input signal,
        # loaded at startup and used until live data arrives for that key.
        self.last_known_good = {}

        # Generated/opaque-table send flags (default all on).
        self.generated_enabled = {key: default for key, _, default in leaf_signals.GENERATED_SIGNALS}

        # Vehicle/battery generation selection.
        self.vehicle = {'car_gen': 'ZE1', 'battery_gen': 'ZE1', 'battery_kwh': 40}

        # Charger-request ramp emulation controls (docs/06), added
        # 2026-07-31 - user-adjustable, not RZ450e-driven, so (like
        # `vehicle` above) these live here rather than going through the
        # mapping engine. Seeded from leaf_signals.py's own confirmed
        # defaults so there's one source of truth for the starting values.
        self.charge_emulation = {k: d for (k, _, d) in leaf_signals.CHARGE_CHECKS}
        self.charge_emulation.update({k: d for (k, _, _, _, _, d) in leaf_signals.CHARGE_SLIDERS})

        # Management-layer runtime flags (soft/hard cut currently asserted),
        # surfaced for the GUI's "?" popups.
        self.management_status = {}

    # ── RZ450e input side (RX thread writes) ───────────────────────────────
    def update_input(self, key, value):
        with self.lock:
            self.rz450e[key] = value
            self.rz450e_ts[key] = time.monotonic()
            self.last_known_good[key] = value

    def update_inputs(self, mapping):
        with self.lock:
            now = time.monotonic()
            for key, value in mapping.items():
                self.rz450e[key] = value
                self.rz450e_ts[key] = now
                self.last_known_good[key] = value

    def note_counter(self, key, raw_value):
        """Track whether a keep-alive counter is actually incrementing, not
        just whether frames are arriving (docs/06, staleness watchdog)."""
        with self.lock:
            last = self._counter_last_value.get(key)
            now = time.monotonic()
            if last is None or last != raw_value:
                self._counter_last_value[key] = raw_value
                self._counter_last_change_ts[key] = now
            self.rz450e[key] = raw_value
            self.rz450e_ts[key] = now

    def counter_stale_age(self, key):
        """Seconds since counter `key` last actually changed value, or None
        if it has never been seen at all this session. Returning None (not
        a fallback to session-start) matters: a counter that was NEVER
        connected must not be treated as 'gone stale' by the watchdog - it
        was never there to begin with. Confirmed bug, see main.py rev 6
        changelog: with the old session-start fallback, a fresh app launch
        with zero hardware ever connected would falsely trip the staleness
        watchdog's soft/hard cut purely from wall-clock time elapsing."""
        with self.lock:
            ts = self._counter_last_change_ts.get(key)
            return None if ts is None else time.monotonic() - ts

    def get_input(self, key, default=None):
        with self.lock:
            if key in self.rz450e:
                return self.rz450e[key]
            if key in self.last_known_good:
                return self.last_known_good[key]
            return default

    def age_of(self, key):
        """Seconds since `key` last updated, or None if never seen this
        session (a value may still exist via last_known_good)."""
        with self.lock:
            ts = self.rz450e_ts.get(key)
            return None if ts is None else time.monotonic() - ts

    def seed_last_known_good(self, cached):
        """Load a persisted last-known-good cache at startup, before any
        live data has arrived (docs/06, section 2)."""
        with self.lock:
            self.last_known_good.update(cached)

    # ── Leaf output side (TX thread writes, GUI reads) ─────────────────────
    def set_leaf_tx(self, key, value):
        with self.lock:
            self.leaf_tx[key] = value
            self.leaf_tx_ts[key] = time.monotonic()

    def set_leaf_tx_many(self, mapping):
        with self.lock:
            now = time.monotonic()
            for key, value in mapping.items():
                self.leaf_tx[key] = value
                self.leaf_tx_ts[key] = now

    def clear_leaf_tx(self):
        """Called when the bridge is manually stopped, so the GUI's live
        monitor/dashboard immediately reflect 'not transmitting' rather than
        showing stale pre-stop values sitting there misleadingly."""
        with self.lock:
            self.leaf_tx.clear()
            self.leaf_tx_ts.clear()
            self.management_status = {}

    def snapshot_inputs(self):
        with self.lock:
            return dict(self.rz450e)

    def snapshot_leaf_tx(self):
        with self.lock:
            return dict(self.leaf_tx)
