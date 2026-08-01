"""Fault/event history: records every time a soft cut, hard cut, over/under-
temp condition, monitor warning, or output-clamp event actually triggers -
independent of the underlying condition's own auto-clear behavior.

Design intent (user-specified 2026-07-31): the real Leaf's own recovery path
for a cut is a physical power-cycle (ignition off/on) - this bridge's cuts
already auto-clear the moment the triggering reading recovers (kept as the
default behavior, not changed here), but that auto-clear was previously
INVISIBLE - a brief fault could trip and self-clear between GUI status polls
with no record it ever happened. This module keeps a running count and
last-triggered/last-cleared history per fault type so that's no longer true,
plus a manual "acknowledge/reset" action per entry (see `manual_reset()`) -
matches how a real BMS scan tool clears stored DTCs: clearing the record
doesn't force the underlying condition to go away, so a still-active fault
immediately reappears (with a fresh trigger count) on the very next check.

Persisted separately from `config_profile.py`'s profile.json (deliberately-
edited settings) - this is DATA about what happened, like last_known_good,
not a setting - see `save_fault_log`/`load_fault_log` there.
"""
import threading
import time

LEVELS = ('warn', 'soft', 'hard')

# Canonical catalog of every fault/condition bridge/management_engine.py
# tracks (key, label, level) - the GUI's Fault History panel renders one row
# per entry here REGARDLESS of whether it has ever actually fired yet (an
# entry only exists in a live FaultLog once update() has been called for it
# at least once, which doesn't happen until the bridge has run at least one
# tick), so the full list of what CAN go wrong is always visible, not just
# what already has. Output-clamp events (bridge/realtime_engine.py,
# key prefix 'clamp_') are dynamic per-field and NOT listed here - they only
# appear once they've actually occurred, since there are ~20+ possible
# fields and clamping is a safety-net implementation detail, not a curated
# policy feature the user tunes.
FAULT_DEFINITIONS = [
    ('low_voltage_emergency', 'Low-voltage EMERGENCY hard cut (per-cell)', 'hard'),
    ('low_voltage_soft', 'Low-voltage soft cut (capacity_empty)', 'soft'),
    ('overvoltage_emergency', 'Cell overvoltage EMERGENCY hard cut (per-cell)', 'hard'),
    ('over_temp_emergency', 'Over-temperature EMERGENCY hard cut (hottest probe)', 'hard'),
    ('charge_cold_block', 'Charge/regen blocked - coldest probe at/below freezing', 'warn'),
    ('discharge_temp_zero', 'Discharge power at zero - over-temperature', 'warn'),
    ('charge_temp_zero', 'Charge/regen power at zero - over-temperature', 'warn'),
    ('cell_imbalance_warn', 'Cell imbalance warning (spread)', 'warn'),
    ('overcurrent_discharge_warn', 'Overcurrent warning - discharge', 'warn'),
    ('overcurrent_charge_warn', 'Overcurrent warning - charge/regen', 'warn'),
    ('staleness_soft', 'Staleness watchdog - soft cut (stale data)', 'soft'),
    ('staleness_hard', 'Staleness watchdog - hard cut escalation', 'hard'),
]


class FaultLog:
    def __init__(self):
        self.lock = threading.RLock()
        self.entries = {}

    def update(self, key, label, level, active, detail=''):
        """Call every tick for every tracked condition, with `active` being
        this tick's live true/false evaluation. Increments the trigger count
        only on a rising edge (inactive -> active), so a condition held
        continuously across many ticks counts as ONE event, not one per
        tick."""
        with self.lock:
            e = self.entries.get(key)
            if e is None:
                e = {
                    'label': label, 'level': level, 'count': 0, 'active': False,
                    'first_triggered': None, 'last_triggered': None,
                    'last_cleared': None, 'last_detail': '',
                }
                self.entries[key] = e
            e['label'] = label
            e['level'] = level
            e['last_detail'] = detail
            if active and not e['active']:
                now = time.time()
                e['count'] += 1
                e['last_triggered'] = now
                if e['first_triggered'] is None:
                    e['first_triggered'] = now
            elif not active and e['active']:
                e['last_cleared'] = time.time()
            e['active'] = active

    def manual_reset(self, key):
        """User-acknowledged reset (the 'Reset' button next to an entry in
        the GUI) - clears the count/history for this entry. Does NOT and
        cannot force a currently-true condition to false (that value comes
        straight from live sensor data on the next tick); if the condition
        is still actually active, `update()` sees a fresh inactive->active
        transition on the very next tick and the entry immediately starts
        counting again from 1 - the same behavior a real BMS scan tool has
        when you clear a code for a fault that's still physically present."""
        with self.lock:
            if key in self.entries:
                e = self.entries[key]
                e.update({
                    'count': 0, 'active': False, 'first_triggered': None,
                    'last_triggered': None, 'last_cleared': None,
                })

    def reset_all(self):
        with self.lock:
            for key in list(self.entries):
                self.manual_reset(key)

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.entries.items()}

    def to_dict(self):
        with self.lock:
            return {k: dict(v) for k, v in self.entries.items()}

    @classmethod
    def from_dict(cls, d):
        fl = cls()
        if d:
            for key, entry in d.items():
                fl.entries[key] = {
                    'label': entry.get('label', key), 'level': entry.get('level', 'warn'),
                    'count': entry.get('count', 0), 'active': False,   # never restore as "active" - re-evaluated live on the next tick
                    'first_triggered': entry.get('first_triggered'),
                    'last_triggered': entry.get('last_triggered'),
                    'last_cleared': entry.get('last_cleared'),
                    'last_detail': entry.get('last_detail', ''),
                }
        return fl
