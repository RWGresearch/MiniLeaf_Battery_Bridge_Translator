"""Verification script for rz450e_signals.py's frame-level validation
(docs/13 item 13.5) - run directly (`py tests/test_rz450e_signals.py`).

This project's own docs (docs/02) said the Toyota additive checksum "should"
be used as a corruption/staleness check - it was defined but never actually
called anywhere until 2026-08-03. Covers frame_checksum_ok() directly and
validate_inputs()'s defensive handling of non-numeric (corrupted) values.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import rz450e_signals

FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


def _valid_frame(arb_id, payload7):
    """Builds an 8-byte frame with a correct Toyota checksum in byte 7."""
    b = list(payload7) + [0] * (7 - len(payload7))
    b = b[:7]
    return bytes(b + [rz450e_signals.toyota_sum_checksum(arb_id, bytes(b + [0]))])


def test_checksum_ok_on_a_genuinely_valid_frame():
    frame = _valid_frame(rz450e_signals.ID_PACK_V, [0x15, 0xC0, 0xB9, 0xB0, 0x00])
    check('a correctly-checksummed frame on a checksum-bearing ID passes',
          rz450e_signals.frame_checksum_ok(rz450e_signals.ID_PACK_V, frame))


def test_checksum_rejects_a_corrupted_byte():
    frame = bytearray(_valid_frame(rz450e_signals.ID_PACK_V, [0x15, 0xC0, 0xB9, 0xB0, 0x00]))
    frame[2] ^= 0xFF   # flip a data byte without updating the checksum
    check('a frame with a corrupted data byte (checksum now mismatched) is rejected',
          not rz450e_signals.frame_checksum_ok(rz450e_signals.ID_PACK_V, bytes(frame)))


def test_checksum_rejects_a_too_short_frame_on_a_checksum_bearing_id():
    check('a too-short frame on a checksum-bearing ID is rejected outright (nothing to verify)',
          not rz450e_signals.frame_checksum_ok(rz450e_signals.ID_PACK_V, bytes([0x15, 0xC0])))


def test_checksum_not_checked_on_ids_that_dont_carry_one():
    # 0x4A9 (per-cell voltages) is confirmed to NOT carry a checksum in byte 7
    # (docs/02: "byte7 is real cell-voltage data, not a checksum").
    garbage = bytes([0x00, 0x0F, 0x4D, 0x0F, 0x4B, 0x0F, 0x54, 0xFF])
    check('an ID that does not carry a Toyota checksum always passes (nothing to check)',
          rz450e_signals.frame_checksum_ok(rz450e_signals.ID_CELLS_A, garbage))


def test_all_five_checksum_ids_are_registered():
    expected = {rz450e_signals.ID_PACK_V, rz450e_signals.ID_CURRENT, rz450e_signals.ID_CHARGE_PERM,
                rz450e_signals.ID_ALIVE_3F1, rz450e_signals.ID_TICK_424}
    check('exactly the 5 confirmed checksum-bearing IDs are registered (docs/02)',
          rz450e_signals.CHECKSUM_IDS == expected, rz450e_signals.CHECKSUM_IDS)


# ── validate_inputs() defensive handling (supports 13.2's disk-data reuse) ──
def test_validate_inputs_rejects_non_numeric_without_crashing():
    valid, rejected = rz450e_signals.validate_inputs({'cell_01': 'not-a-number', 'pack_v': 355.0})
    check('a non-numeric value does not crash validate_inputs()', True)
    check('the non-numeric value is rejected', 'cell_01' in rejected and 'cell_01' not in valid)
    check('a normal numeric value alongside it still validates fine', valid.get('pack_v') == 355.0)


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        sys.exit(1)
    print('All checks passed.')
