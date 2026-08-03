# Signal Mapping — RZ450e Inputs → Leaf Outputs

The mapping engine ties confirmed RZ450e signals (`02-source-signals-rz450e.md`) to Leaf mapping
targets (`03-target-signals-leaf.md`). This doc covers the structural mismatches that make this
not a 1:1 relabeling job, and the mapping UI/engine design.

## The 5 structural mismatches

### 1. Current sign is inverted

**RZ450e: positive = discharge. Leaf: positive = charge.** Both conventions are independently
100%-validated in their own source projects, so this is a real, confirmed inversion, not a bug in
either project. **Every current mapping must apply `leaf_current = -1 × rz450e_current`** (plus
whatever scale conversion). Because both projects flag this exact convention as "previously
gotten wrong once already," this conversion needs its own unit test in the bridge app, not just a
visual spot-check.

### 2. GIDS / QC capacity have no RZ450e equivalent — must be derived

Leaf's GIDS (remaining capacity, ~80Wh/gid) and QC full/remaining capacity (Wh) aren't things the
RZ450e reports directly. Derive:

```
usable_wh   = rz450e_soc_pct / 100.0 × rz450e_capacity_ah × rz450e_pack_voltage
gids        = usable_wh / 80.0                      → 0x5BC remaining capacity
qc_full_wh  = rz450e_capacity_ah × rz450e_pack_voltage   → 0x59E QC full capacity
qc_remain_wh = usable_wh                             → 0x59E QC remaining capacity
```

Uses RZ450e SoC (DID `0x1F5B`, slow) × capacity (DID `0x1D3E`, very slow, effectively static) ×
voltage (raw CAN `0x020` `pack_v`, fast). Known threshold behaviors to preserve on the Leaf side
(from the Leaf project's real-hardware testing): **GIDS ≈ 49 triggers the low-battery warning**,
**GIDS ≈ 5 triggers turtle mode** (VCM limits to dash "6 bars"). These are real VCM-side
thresholds, not tunable on this project's end — just something to be aware of when picking the
**cell-voltage** thresholds in `05-battery-management-safety.md`'s `discharge_power_taper` (full
power ≥3.00V, zero ≤2.60V/cell as of the 2026-08-01 re-anchoring) and `low_voltage_cutoff` (soft
cut 3.00V/cell, emergency hard cut 2.60V/cell — SoC is a backup check only there as of the
2026-07-31 correction, not an independent floor). A 2.60-3.00V/cell window on a ~194.97Ah/348V pack
is nowhere near GIDS≈5, so no conflict expected, but worth re-checking once real GIDS values are
flowing — see `10-open-questions.md`, item 3.

**Dash SOC% (`soc_correction`, `0x59E` byte 7) — confirmed 2026-07-31.** A separate field from
GIDS/QC above, and from `usable_soc`/`fine_soc_pct` — per the Leaf project's own findings, the
physical dash SOC% readout comes from `soc_correction` specifically, not from those other fields.
Inherited as an open question (the reference project confirmed this was the dash-% source but
never derived the actual raw-to-percent formula). **User-confirmed on real hardware** (their own
Leaf + this project's bench RZ450e pack): **`soc_correction_raw = rz450e_soc_pct × 2.0`** — raw
0-200 maps to 0-100%, i.e. exactly 2 raw counts per percent, a plain linear tie (no offset). Shipped
as a default mapping (`bridge/mapping_engine.py`'s `default_ties()`); `leaf_signals.py`'s slider
range/default updated from an unconfirmed 0-255/241 placeholder to the confirmed 0-200/90. See
`10-open-questions.md` item 10 and `11-manual-verification-checklist.md` for the full confirmation
record.

### 3. Cell architecture differs — per-cell data can't reach the periodic bus

RZ450e broadcasts all 96 cell voltages constantly (`0x4A9`/`0x4C0`). The Leaf's periodic bus has no
per-cell field at all — the only place cell data can appear on the Leaf side is the UDS diagnostic
responder (`0x79B`/`0x7BB`), deferred to milestone 2 per `01-project-goals.md`. Until then, per-cell
data is used **internally** by the battery-management layer (`05-battery-management-safety.md`)
for protection decisions, but has no periodic-frame destination.

### 4. Update cadence differs by 3+ orders of magnitude

RZ450e fast raw-CAN signals (voltage, current, per-cell, per-probe temps) update in single-digit
milliseconds. RZ450e DID/PID values (SoC, SOH/capacity) update every 4-9 seconds. The Leaf bus
needs every field refreshed at its own fixed 10-100ms period, unconditionally (see
`06-realtime-engine-and-watchdog.md`).

**Rule: prefer the fast raw-CAN source whenever one exists for a needed quantity.** Use DID/PID
values only for quantities with no raw-CAN equivalent (SoC, SOH) — hold the last polled value
between updates and feed it into the same fixed-period Leaf TX loop as everything else. Never make
the Leaf TX loop wait on a slow DID poll.

| Quantity | Primary (fast) source | Fallback / supplement (slow) source |
|---|---|---|
| Pack voltage | `0x020 pack_v` | DID `0x1F9A` voltage (cross-check only) |
| Pack current | `0x023` | DID `0x1F9A` / PID `0x9A` current (cross-check only) |
| Cell voltages (min/max/individual) | `0x4A9`/`0x4C0` (primary), `0x020 cell_min/max` (sanity check) | DID `0x182E` (milestone 2 UDS responder only) |
| Temperature | `0x4AA` (per-probe), `0x4A7` (extremes) | DID `0x1814`/`0x1832` (milestone 2 only) |
| SoC | — (no fast equivalent) | DID `0x1F5B` / PID `0x5B` |
| SOH / capacity | — (no fast equivalent, and near-static anyway) | DID `0x1D3E` |

### 5. Several Leaf fields are inherently unmappable

Opaque replay tables, PRUN, toggle bits — see `03-target-signals-leaf.md`'s "internally generated"
section. These are never mapping *targets*; they're generated exactly as the Leaf project already
does, surfaced in the GUI as send/don't-send checkboxes (default checked).

## Mapping engine design

**Fixed preset list of combine/conversion functions** (per user decision — no generic
rule-scripting engine, so every mapping stays portable to a C function on the eventual STM32
firmware):

- **Passthrough + linear scale/offset** — `output = input × scale + offset` (covers the vast
  majority of direct ties, including the sign-inversion case above as `scale = -1 × ...`).
- **Sum** — combine multiple inputs (e.g., not currently needed for a many-to-one tie, but
  available).
- **Average** — e.g., averaging multiple temperature probes if a Leaf field needs a single
  representative value.
- **Min / Max** — e.g., deriving a single "worst cell" value from the 96-cell array for a
  protection feature.
- **Static lookup table** — for any confirmed-but-nonlinear relationship (none currently shipped as
  a default tie — see the plain linear example below, which turned out not to need one).

**ChargeBars/CapacityBars raw — confirmed live mapping, added 2026-07-31.** Previously just a fixed
default (docs/03), on the assumption a direct RZ450e-driven mapping to this 0-14/15 field might need
the static-lookup-table combine above to handle the "15 = all segments off" special case. **User-
confirmed on real hardware** (own Leaf + this project's bench RZ450e pack) that the working 0-14
range is simply linear against `capacity_pack1_ah`'s 0-200 Ah range — `scale = 14/200 = 0.07`, no
offset, no lookup table needed, since a plain 0-200 Ah input can never produce the out-of-range 15
sentinel anyway. Shipped as a default tie in `bridge/mapping_engine.py`'s `default_ties()`.

Each of the derived-value formulas above (GIDS, QC capacity) is implemented as its own named
derivation, not expressed through the generic preset list — they're multi-signal formulas, not a
single tie.

## GUI representation

See `08-gui-design.md` for the actual screen layout. In short: each mapping is a two-line card with
up to 3 input signal dropdowns (sourced from `02-source-signals-rz450e.md`'s registry), an output
signal dropdown (sourced from `03-target-signals-leaf.md`'s registry), a combine-function selector,
and a "?" button showing the live conversion math (input value(s) → conversion applied → output
value) so the user can visually confirm what's actually happening. **Every dropdown entry is
prefixed with its source CAN ID or DID** (e.g. `[0x020] Pack voltage`, `[DID 0x1F5B] State of
charge`, `[0x1DB] Pack voltage (V)`) — added after the user's 2026-07-31 review, so it's obvious
which message a decoded value stands for without cross-referencing this doc. There's also a
separate large **Dashboard** window (bar gauges, input→conversion→output side by side for every
signal at once) for a wider view than the configurator tabs allow — see `08-gui-design.md`.
