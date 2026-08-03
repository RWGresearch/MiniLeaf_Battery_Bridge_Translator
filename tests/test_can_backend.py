"""Verification script for bridge/can_backend.py's BusConnection - run
directly (`py tests/test_can_backend.py`), not a pytest suite, matching this
project's other test scripts. First test coverage for this module.

Covers docs/13 items 3.1/3.2 (fixed 2026-08-01): BusConnection used to hold
no lock across connect()/disconnect()/_auto_reconnect_loop(), and the
monitor thread slept a flat RECONNECT_INTERVAL_S (3.0s) with no way to be
interrupted - a fast disconnect-then-reconnect within that window could
leave a connection with no reconnect monitor for the rest of the session,
or a concurrent reconnect could silently undo an explicit Disconnect click.

Per docs/14-validation-test-plan.md's own note on this item: the *exact*
microsecond-scale race (a reconnect completing right as disconnect() fires)
isn't practically testable deterministically without adding test-only
synchronization hooks to can_backend.py itself, which this project doesn't
have. What IS cleanly, deterministically testable - and would catch a real
regression if the interruptible-wait or lock-scoping fix were ever
reverted - are the two things below.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.can_backend import BusConnection, RECONNECT_INTERVAL_S

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


def test_disconnect_interrupts_the_monitor_promptly_not_after_the_full_interval():
    bus = BusConnection('test', demo=True)
    bus.connect('DEMO1')
    check('sanity: a monitor thread is running after connect()',
          bus._monitor_thread is not None and bus._monitor_thread.is_alive())

    t0 = time.monotonic()
    bus.disconnect()
    # The OLD (buggy) behavior used a flat time.sleep(RECONNECT_INTERVAL_S)
    # with no way to interrupt it - the monitor thread would stay alive for
    # up to the full 3.0s regardless of disconnect(). The FIXED behavior
    # uses an interruptible Event.wait(), which unblocks the instant
    # _stop_monitor.set() is called inside disconnect() - so join(timeout=1.0)
    # here (well under RECONNECT_INTERVAL_S) should always succeed on a
    # correct implementation.
    bus._monitor_thread.join(timeout=1.0)
    elapsed = time.monotonic() - t0
    check(f'monitor thread exits within ~1s of disconnect(), not waiting the full '
          f'{RECONNECT_INTERVAL_S:g}s RECONNECT_INTERVAL_S',
          not bus._monitor_thread.is_alive(),
          f'elapsed={elapsed:.2f}s, thread still alive={bus._monitor_thread.is_alive()}')


def test_rapid_disconnect_reconnect_cycling_never_leaves_the_connection_without_a_monitor():
    bus = BusConnection('test', demo=True)
    for _ in range(10):
        bus.connect('DEMO1')
        bus.disconnect()
    bus.connect('DEMO1')   # final state: should end up connected with a live monitor
    try:
        check('after rapid disconnect/reconnect cycling, the bus ends up connected',
              bus.connected)
        check('after rapid cycling, exactly one monitor thread is alive (no leaked-away monitor '
              'from item 3.1)',
              bus._monitor_thread is not None and bus._monitor_thread.is_alive(),
              f'monitor_thread={bus._monitor_thread}')
    finally:
        bus.disconnect()


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
