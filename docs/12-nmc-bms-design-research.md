# NMC BMS Design Research — What We Need to Know, and How Our Design Compares

**Research report, 2026-07-31.** This doc collects the well-documented industry/academic
understanding of NMC lithium-ion behavior — voltage, temperature, current/C-rate, imbalance, and
standard BMS protection architecture — then audits our own battery-management design
(`05-battery-management-safety.md`, `bridge/management_engine.py`) against it.

The research pass itself changed no code (user-directed: research first, discuss findings, decide
what to implement). **Findings F1, F3, F4, F5, and F6 were subsequently implemented the same day**,
after user review — see §9's per-finding status. F2 was implemented as a monitor-only feature
rather than an active cutoff, a deliberate scope choice explained in its entry. F7 was an
alignment note, no action needed. F8 (fault-latching, §9) was discovered while implementing the
others and was implemented on 2026-08-01, after explicit discussion, as `ManagementEngine.
_hard_latched` — see §9's per-finding status for the full mechanism.

All numbers below are **researched general-industry/academic values**, not RZ450e-specific
datasheet values (Toyota publishes no per-cell limits). Same confirmed-vs-documented discipline as
the rest of this doc set applies: these are documented starting points, promoted only via
`11-manual-verification-checklist.md`.

---

## 1. Voltage — the high end (overcharge)

- **4.20 V/cell is the standard NMC charge ceiling.** Some high-voltage NMC variants are rated
  4.35–4.40 V, but nothing suggests this pack is one (its confirmed real-world range topped out at
  4.2 V-class behavior; Toyota's published bZ4X material matches standard NMC).
- **Degradation rises steeply with ceiling voltage, even below the "safe" limit.** Peer-reviewed
  cycling studies show that lowering the upper cutoff from 4.20 V to 4.10 V significantly reduces
  cathode active-material loss and extends cycle life; higher voltage accelerates electrolyte
  oxidation and cathode degradation. The last ~0.1 V buys little capacity and costs
  disproportionate life.
- **Calendar aging (time spent sitting, no cycling) is driven by SoC × temperature.** Storage at
  ~100% SoC produces substantially accelerated capacity fade versus mid-SoC storage — high anode
  potential destabilizes the electrolyte and grows SEI even at rest. This is the researched basis
  for "charge to 80% daily, 100% only when needed" — it's not just a cycling rule, it's a
  *parked-car* rule: minimize time parked at full.
- **True overcharge (above ~4.3–4.4 V) is a safety event, not just an aging event**: metallic
  lithium plates onto the anode, the cathode over-delithiates and destabilizes, and continued
  forcing leads to gas generation, venting, and thermal runaway. This is why a BMS has a separate
  emergency tier above the normal ceiling, and why that tier must act on the *worst single cell* —
  a pack-average voltage can look normal while one cell is over the line.

## 2. Voltage — the low end (over-discharge)

The damage sequence during over-discharge is well characterized in the literature (in-situ
studies, post-mortem XPS/SEM):

1. **Below ~3.0 V** (NMC): little immediate harm, but usable energy is nearly exhausted and
   voltage falls fast under any load — this is where a BMS ends discharge in normal operation
   (typical BMS floors: 2.75–3.0 V; many datasheets allow 2.5 V absolute minimum).
2. **Below ~2.5 V**: SEI decomposition begins on the anode; capacity loss becomes measurable and
   partially irreversible.
3. **Deep over-discharge (approaching 0 V / driven into reversal)**: the anode potential rises
   above ~3.4–3.5 V vs Li/Li+, which **dissolves the copper current collector**. On the next
   recharge, dissolved copper redeposits as metallic copper on the electrodes/separator — a
   latent **internal short circuit**. Studies detect measurable internal shorts after
   over-discharge past roughly −12% SoC. A copper-contaminated cell is a delayed fire risk: the
   danger appears at *recharge*, possibly much later, not at the moment of over-discharge.

**BMS design consequences (standard practice):**
- End normal discharge at ~2.75–3.0 V/cell on the **lowest** cell (not pack average).
- A second, lower emergency threshold (still above the ~2.5 V damage line) forces disconnect.
- A pack that has sat deeply over-discharged should not simply be recharged at full current — real
  BMSs either refuse or require a supervised low-current recovery. (For us: mostly a
  bench-handling rule, since the bridge can't control the RZ450e's own contactors on a dead pack.)
- **Sag vs. rest voltage matters at the low end.** Cell voltage under heavy load sags well below
  its open-circuit value (internal resistance × current — and IR roughly doubles from 25°C to
  ~0°C). Industry practice therefore distinguishes reaction speeds by tier: **power derating
  reacts instantly to instantaneous voltage** (protect the cell now), but a **latching cutoff is
  qualified over a short persistence window** so a momentary sag transient under a spike load
  doesn't slam the system off when the cell would rebound to a healthy rest voltage the moment
  load drops.

## 3. Temperature — charging (the strict direction)

- **Safe charge window: 0–45°C** — the near-universal lithium-ion datasheet figure.
- **Below 0°C, charging metal-plates the anode.** At low temperature, ion transport into graphite
  slows so much that incoming lithium deposits as metal on the anode surface instead of
  intercalating. Plated lithium is (a) permanent capacity loss, (b) a dendrite/internal-short
  precursor, and (c) — critically — it **lowers the cell's thermal-runaway onset temperature**
  (see §5). Charging below freezing is the single most damaging "normal-looking" operation an EV
  battery can experience: nothing appears wrong at the time.
- **Plating is not a cliff at 0°C — it's a current × temperature × SoC surface.** The literature
  is consistent that plating risk rises with charge C-rate, falls with temperature, and rises with
  SoC (a full anode has fewer vacant sites). Documented practice: full fast-charge current is fine
  from roughly 10–15°C up; **below ~10°C charge current should be actively derated**, reaching
  ~0.1C near 0°C and as little as ~0.02C at −30°C for cells rated to charge there at all. Real EV
  BMSs implement this as a continuous cold-side derate curve, not a single on/off block — and they
  apply it to **regen braking too**, since regen is charging (a cold-soaked EV limits or disables
  regen until the pack warms; this is standard production-EV behavior).
- **The cold-side decision must use the pack's COLDEST sensor.** Plating happens in the coldest
  region of the pack; a warm-side or average temperature can be above 0°C while corner cells are
  below it. (Mirror rule of the hot side, which must use the hottest sensor.)
- **A cycle-life data point worth remembering**: a 53 Ah graphite/NMC cell charged at 0.85C
  lasted ~4000 cycles at 20°C but only ~40 cycles at 10°C — a 100× life collapse from a
  10-degree difference at a C-rate that "should" be fine. Cold charging at moderate current is
  quietly destructive long before it's a fire risk.
- **High-temperature charging**: derate beginning around 30–35°C, stop by 45°C. Sustained
  operation above 40–45°C accelerates every aging mechanism (SEI growth, electrolyte oxidation,
  transition-metal dissolution) even when nothing "fails."

## 4. Temperature — discharging (the tolerant direction)

- **Safe discharge window: roughly −20°C to 60°C** (some cells rated to −30°C). Discharging
  doesn't plate lithium, so the cold side is an efficiency/power problem (high IR, deep sag, weak
  power) rather than a damage mechanism — no low-temperature discharge cutoff is standard, though
  expect large voltage sag that low-voltage logic must tolerate.
- **The hot side is the real limit**, because of what sits just above it (§5). Derate from
  ~50–55°C, stop at 60°C.

## 5. Thermal runaway — the failure chain and its onset temperatures

The sequence is consistent across the review literature (each stage's heat feeds the next):

| Stage | Onset temp | What happens |
|---|---|---|
| SEI decomposition | **~70–90°C** (as low as **~60°C** in cells with plated lithium) | Protective anode film breaks down, exothermic; self-heating begins |
| Anode–electrolyte reaction | ~120°C | Exposed lithiated graphite reacts directly with electrolyte |
| Separator collapse | ~130–160°C (PE melts ~130°C, PP ~165°C) | Internal short circuits across the pack |
| Cathode oxygen release | ~180–250°C (lower for higher-nickel NMC) | Cell supplies its own oxidizer; full runaway, venting/fire |

**Design consequences:**
- The 60°C operational ceiling isn't arbitrary: it's the **edge of the self-heating region** for
  an aged or plated cell. There is very little margin above it — an emergency response above the
  60°C soft-stop should come within a few degrees, not tens of degrees.
- **Rate-of-rise (dT/dt) is a recognized early-warning signal** independent of any absolute
  threshold: a healthy pack's temperature moves slowly (thermal mass); a cell heading into stage 1
  climbs abnormally fast, often paired with a simultaneous unexplained cell-voltage drop
  (soft internal short). Runaway detection systems key on exactly this pair.
- Past runaway onset, a BMS can't stop the chemistry — everything above is about acting *before*
  60–90°C, which is why the boring derate curves in §3–4 are the actual thermal-runaway
  protection.

## 6. Current / C-rate

- Cell manufacturers specify **separate continuous and short-peak current limits**, for both
  charge and discharge, both temperature-dependent. Standard BMS practice enforces both:
  instantaneous overcurrent → immediate action; sustained above-continuous current → derate/alarm.
- **Charge C-rate is the strict one** (plating, §3). Discharge C-rate for a large EV NMC cell is
  typically comfortable to 2–3C continuous — heat is the binding constraint, and cycle-life
  studies show high discharge rates mainly matter through the temperature they generate.
- **Our actual operating envelope is gentle by EV standards** (documented pack figure ~71.4 kWh /
  ~200 Ah nominal — unverified against this bench pack):
  - Leaf onboard AC charge, 6.6 kW ≈ 19 A ≈ **0.09C** — negligible plating pressure at any
    temperature above freezing.
  - Leaf regen, up to a few tens of kW ≈ up to ~0.5C *into* the pack — **the only meaningful
    charge-C-rate vector we have, and it's the one that happens on cold packs**.
  - Leaf drive power 80–110 kW peak ≈ 230–315 A ≈ **1.1–1.6C** discharge peak — well within NMC
    capability.
  - **Sensor limitation**: our only fast current signal (`0x023`, ~8 ms) **saturates at
    ±204.7 A** (`02-source-signals-rz450e.md`), *below* the Leaf's plausible peak drive current.
    Any overcurrent feature must treat a saturated reading as "at/above limit — duration unknown,"
    and can never measure how far beyond ~205 A the pack is actually being pulled.
  - **Correction (user, 2026-07-31): the ±204.7A ceiling is a limit of the CAN signal's 12-bit
    encoding, not the battery.** The real pack's own rated capability is considerably higher than
    the "gentle" Leaf-side estimate above: a 500A in-line slow-blow discharge fuse, 150kW DC fast
    charging (~430A, separate from the Leaf's 6.6kW AC path and not currently in this project's
    scope — see `10-open-questions.md` #9), and the factory RZ450E's 230kW peak output (~660A for
    short bursts). This bench setup has only ever tested up to ~200A (a bench limitation, not a
    pack limitation). Net effect: this project's only fast current signal cannot see, and cannot
    protect against, anywhere near the pack's real current range — a materially bigger gap than
    "our envelope is gentle" originally suggested.

**Addendum (2026-08-08, docs/16 parameter-clamping audit):** `discharge_power_taper`/
`charge_target_taper` gained real configurable ceiling/floor fields (`discharge_min_kw`/
`discharge_max_kw`, `regen_min_kw`/`regen_max_kw`) using the figures researched above.
`discharge_max_kw` defaults to 110.0kW, matching the "80-110 kW peak" Leaf drive-power figure
directly. `regen_max_kw` does **not** yet match the "~0.5C ≈ a few tens of kW" regen figure above —
it ships at 70.0kW (the pre-existing static default) deliberately, to avoid a silent behavior change
on rollout (user directive) — tune it down toward this section's own ~36kW estimate once ready.

## 7. Cell imbalance and balancing

- Series cells drift apart (manufacturing spread, temperature gradients, self-discharge
  differences). In a 96s pack the **worst cell sets every limit** — which our per-cell-authoritative
  design already embraces.
- Typical production thresholds: balancing engages when cell ΔV exceeds **~10–30 mV** (usually
  evaluated near top of charge, where the OCV curve is steep enough to mean something).
- **A cell that sits 30–50 mV below its neighbors at rest** (not under load) is the classic
  early signature of elevated self-discharge — i.e. a developing internal defect — and is exactly
  the kind of thing a soft internal short looks like before it becomes a thermal event. Rising
  ΔV *trend* over days/weeks is as informative as the absolute number.
- This bridge **cannot balance** (balancing hardware, if any is active, lives inside the RZ450e
  pack's own cell-supervision electronics — whether it still operates in our configuration is
  unknown). But we uniquely *see* all 96 cells at high rate, so monitoring/alerting on spread is
  cheap and valuable: a growing spread also silently shrinks the usable pack window (highest cell
  hits the charge taper early, lowest cell hits the discharge taper early — the pack "feels
  smaller" as a symptom).

## 8. Standard BMS protection architecture (how the industry structures all of the above)

- **Layered, graduated response** — every parameter (V, T, I) gets escalating tiers:
  **monitor → warn → derate → controlled stop (soft) → disconnect (hard)**, with hard disconnect
  reserved for genuine emergencies. This is exactly the philosophy our soft/hard split already
  implements.
- **Fault latching**: emergency-tier (hard) faults **latch** — they don't self-clear when the
  reading drifts back to normal, because the hazardous condition they imply (e.g. a cell that hit
  4.3 V has possibly plated lithium) doesn't un-happen. Clearing a hard fault is a deliberate
  human action. Derate-tier responses, by contrast, release automatically (with hysteresis).
- **Asymmetric response speed**: derates attack fast, release slow (anti-hunting hysteresis —
  our discharge taper already does this); cutoffs are qualified over a persistence window
  (anti-transient — see §2); emergency tiers act immediately.
- **Plausibility/cross-checks on the sensors themselves**: a BMS that acts on garbage input is
  itself a hazard. Standard practice: range checks (a physically impossible reading = sensor
  fault, not battery fault), rate-of-change checks, cross-source agreement checks (our
  fast-broadcast vs. DID cross-checks, and the SoC-as-backup-witness rule, are this pattern), and
  a defined degraded/safe state when a sensor is lost (our staleness watchdog).
- **Relevant standards landscape** (for orientation, not compliance claims): **ISO 26262**
  (automotive functional safety — hazard analysis, ASIL levels, mandated overcharge/overdischarge/
  overtemp/overcurrent/short-circuit protection with fault-tolerant design), **IEC 62660**
  (EV lithium cell performance/reliability/abuse testing), **UL 2580** (EV battery pack safety),
  **SAE J2464/J2929** (pack abuse testing). The layered-protection + plausibility + safe-state
  pattern above is what these standards institutionalize.

---

## 9. Audit: our design vs. this research

**Note (added 2026-08-04): the specific numbers in the table below are a snapshot from this audit's
original 2026-07-31 pass.** Several were subsequently retuned by later user edits (2026-08-01) or
split into separate features (regen vs. AC charging) — `05-battery-management-safety.md` is the
current source of truth for actual defaults; this table's verdicts (✅ matches researched practice)
still hold, just against the retuned numbers, called out inline below.

### What aligns (no change indicated)

| Our default (`05-battery-management-safety.md`) | Researched range | Verdict |
|---|---|---|
| Per-cell voltage is sole cutoff authority; SoC backup-witness only | Worst-cell-governs is universal practice | ✅ matches |
| Soft cut preferred, hard cut reserved for emergencies | Graduated monitor→derate→stop→disconnect | ✅ matches |
| Min-cell soft cut 3.00 V / emergency 2.80 V (**retuned 2026-08-01 to 2.60 V emergency**) | BMS floors 2.75–3.0 V; damage line ~2.5 V | ✅ in range, sensible margins |
| Charge taper 3.90→4.10 V, emergency 4.30 V (**retuned/split 2026-08-01: regen-only taper now 4.00→4.15 V, emergency 4.30 V; emergency tier further tightened 2026-08-03 to 4.20 V exactly, matching the ceiling below**) | Ceiling 4.20 V; staying below it extends life; emergency tier above | ✅ conservative in the right direction (slow-VCM lead time) |
| Discharge taper 3.50→3.00 V, fast-attack/slow-release (**re-anchored 2026-08-01 to 3.00→2.60 V**) | Derate-before-cutoff; anti-hunting hysteresis | ✅ matches practice |
| Charge temp: derate 32°C, stop 45°C, block <0°C | Derate ~30–35°C, stop 45°C, no sub-freezing charge | ✅ in range (F1, F3 fixed/added below) |
| Discharge temp: derate 55°C, stop 60°C, no cold cutoff | Derate ~50–55°C, stop 60°C; cold side is power-only | ✅ in range (F6 added below) |
| 80% daily / 100% extended charge target | High-SoC calendar aging is real and large | ✅ well-supported |
| Staleness watchdog, soft→hard escalation | Defined safe state on data loss | ✅ matches |
| Regen and AC charge shared one charge-power ceiling | Regen *is* charging; same protections apply | ✅ matched at the time — **superseded 2026-08-01**: split into two independently-tunable features (`charge_target_taper` for regen, `ac_charge_taper` for AC charging), defaulted to the same starting curve, since the two use cases warrant independent tuning (regen up to ~0.5C vs. AC charging ~0.09C) even though the underlying protection principle is unchanged |

### Findings

**F1 — Cold-charge block tested the WRONG temperature probe (bug-level). ✅ FIXED 2026-07-31.**
`bridge/management_engine.py` used to block charging when `temp_max <= charge_low_block_c` — i.e.
charging was only blocked once the *hottest* probe was at/below 0°C. Research (§3) is unambiguous:
cold-side decisions must use the **coldest** probe, because plating happens in the coldest cells.
As written, a pack whose coldest corner was below freezing kept accepting charge/regen as long as
its warmest probe read above 0°C. Fixed: cold-side decisions (block + F3's derate ramp) now key on
`temp_min`, which was already decoded on the same fast `0x4A7` frame (`bridge/rz450e_signals.py`);
hot-side logic is unchanged, still on `temp_max`. Unit-tested in `tests/test_management_engine.py`
(`test_f1_cold_block_uses_coldest_probe`) — confirmed a pack with a warm hottest probe but a frozen
coldest probe now correctly blocks charging, and the reverse scenario doesn't.

**F2 — No overcurrent feature at all. ✅ IMPLEMENTED 2026-07-31 as MONITOR-ONLY (deliberately not
an active cutoff).** Added `overcurrent_monitor`: warns (status text only, no derate/cut) after
sustained elevated current in either direction, using the fast `0x023` current signal. Deliberately
scoped down from a full cutoff feature: no cell datasheet exists to source a real continuous/peak
limit from, and `0x023` saturates at ±204.7 A — below the Leaf's plausible ~230–315 A peak drive
current — so an active cutoff couldn't even measure how far beyond that a real fault current would
go, and a guessed threshold risked nuisance-tripping during ordinary hard acceleration. The warn
thresholds (150 A discharge / 30 A charge-regen, 5 s persistence) are derived from this project's
own already-confirmed specs (the sensor's saturation ceiling; the Leaf AC charger's ~19 A max), not
an invented number — but they're explicitly provisional pending real drive-cycle current logging
(new open question, `10-open-questions.md` #8). Unit-tested for correct warn/no-warn/persistence/
saturation-note behavior; the threshold *values* still need real-world confirmation.
**Correction, same day**: the ±204.7A sensor ceiling is a CAN-signal encoding limit, not a battery
limit — the real pack is rated to a 500A discharge fuse and ~660A short-burst peak (§6, user-
confirmed). This monitor can only ever see a small fraction of the pack's real operating range;
that's a hardware gap (a future wider-range current sensor), not something this feature can fix in
software.

**F3 — No cold-side charge derate curve, only the 0°C hard block. ✅ IMPLEMENTED 2026-07-31.**
Industry practice (§3) derates charge current continuously below ~10–15°C, not just at the
freezing line — and the 100×-cycle-life-collapse data point shows cold charging is destructive
well above 0°C at rates far below "fast charge." Our mitigating factor: 0.09C AC charging is
negligible-risk at any above-freezing temperature. Our exposure: **regen into a cold-soaked pack**
(up to ~0.5C, §6), which is exactly the scenario production EVs limit regen for. Added
`charge_derate_low_start_c` (10°C default) — charge/regen acceptance now ramps from 0% at the
existing 0°C block up to 100% at this new point, reusing the same `_ramp_factor` mechanism the
hot-side derate already uses, keyed on `temp_min` per F1. Unit-tested at the midpoint (5°C →
~50% factor) and above the window (15.6°C → full power). (Field renamed from `charge_derate_low_
start_f` and re-stored in °C, 2026-08-09 - same physical thresholds, no behavior change.)

**F4 — No cell-spread (ΔV) monitoring/alert. ✅ IMPLEMENTED 2026-07-31, monitor-only by design.**
We watch worst-cell extremes but never watched the *spread* itself (§7). We can't balance, but a
growing rest-time spread (≥~30–50 mV) is the cheapest early-warning signal for a developing bad
cell that we have — and we're the only thing watching. Added `cell_imbalance_monitor`: warns once
the spread between the worst-high and worst-low of all 96 cells reaches 50 mV (default), never
cuts or derates. Also added the open question this raised to `10-open-questions.md` #7: **does the
RZ450e pack's internal cell-supervision hardware still balance cells in our configuration**
(powered, but no Toyota vehicle attached)? If not, spread will grow over months and this monitor
becomes more important. Unit-tested: a 100 mV artificial spread correctly warns, a balanced pack
correctly reports "ok," and the monitor never asserts `capacity_empty`/`relay_cut_request` under
any spread.

**F5 — Low-voltage soft cut had no persistence qualification against sag transients.
✅ FIXED 2026-07-31.** The 3.00 V soft cut evaluated instantaneous per-cell voltage. Under a spike
load (cold pack = double IR, §2), a healthy-at-rest cell can momentarily sag through 3.00 V;
standard practice derates instantly but *qualifies* the cutoff tier over a short persistence
window. Added `soft_cut_persistence_s` (2.0 s default): the min-cell condition must now hold
continuously for this long before `capacity_empty` latches. The discharge taper reaching zero
power at the same 3.00 V was already collapsing current/sag before this window would normally
matter — this is a backstop, not the primary defense. Emergency tier (2.80 V) stays instantaneous,
by design. Unit-tested: a cell held at 2.90 V does not cut on the first tick but does after 2.0 s;
a cell at 2.70 V (emergency) cuts instantly with no persistence delay.

**F6 — Emergency over-temperature hard-cut had no documented default value, and the soft-ramp
ceiling was doing double duty as the hard-cut trigger. ✅ FIXED 2026-07-31.** The feature row
existed in `05`'s table ("Emergency over/under-voltage **or over-temp** → hard cut") but the
researched-defaults table only ever defined emergency defaults for voltage (2.80 V / 4.30 V) — no
emergency temperature number existed, and the code asserted a hard cut directly at
`discharge_hard_stop_c`/`charge_hard_stop_c` (the soft ramp's own ceiling), contradicting this
feature's "Soft (ramp)" tier label. Fixed: both hard-stop values are now pure soft ramp-to-zero
points; a new `emergency_temp_c` (65°C default, **tightened 2026-08-01 to 61°C**) is
the actual hard-cut trigger, mirroring
the two-tier soft/emergency structure the voltage features already have. Deliberately close above
the 60°C soft stop — self-heating onset for a cell with plated lithium can begin as low as ~60°C
(§5), leaving very little real margin. Unit-tested: at exactly 60°C (the old combined threshold),
discharge power now reaches zero WITHOUT a hard cut; at 65.6°C, a hard cut correctly fires. (Fields
renamed from `_f` to `_c`/re-stored in °C, 2026-08-09 - same physical thresholds, no behavior
change.) **Not
implemented**: dT/dt (rate-of-rise) + unexplained-voltage-drop as a recognized early runaway
signature (§5) — still a candidate monitor/warn-tier feature, not built this pass.

**F7 — (Alignment note, no action needed.)** The 80%-daily target is strongly supported by
calendar-aging research, with one refinement the research suggests: the harm is *time parked
full*, not the brief visit to 100% — so the existing "extended/road-trip" toggle is exactly the
right shape (charge to 100% right before departure, don't live there). Worth a line in the GUI
help text someday; no design change needed.

**F8 — Hard cuts don't latch; they self-clear if the triggering reading recovers. Discovered
2026-07-31 while implementing F1–F6. ✅ IMPLEMENTED 2026-08-01.** §8 of this same report states
standard practice: emergency-tier faults **latch** and require a deliberate human action to clear,
because the hazardous condition they imply (e.g. a cell that hit 4.30 V may have plated lithium)
doesn't un-happen just because the reading drifted back to normal. Our
`hard_cut`/`relay_cut_request` used to be recomputed fresh from live readings on every `apply()`
tick with no latch: a brief emergency-level spike (voltage or temperature) subsiding on its own
would clear the hard cut the very next tick, re-closing `interlock`. Fixed: `ManagementEngine.
_hard_latched` now stays asserted once any hard-cut condition fires, regardless of subsequent
readings, cleared only by `notify_session_start()` (a genuine bus-wake power cycle) or
`notify_charge_replug()` (a genuine ≥3.0s charge-permission gap, refined `docs/13` item 13.4 after
an independent review found the first version could be cleared by simply toggling the bridge's own
Stop/Start button). See `05-battery-management-safety.md`'s "`full_charge_flag` re-arm" section and
`06-realtime-engine-and-watchdog.md` section 5 for the full current behavior.

### Implementation summary

| Finding | Status | Where |
|---|---|---|
| F1 (cold-block wrong probe) | ✅ Fixed | `bridge/management_engine.py`, `over_temperature_derate` |
| F2 (overcurrent) | ✅ Implemented (monitor-only) | `bridge/management_engine.py`, `overcurrent_monitor` |
| F3 (cold derate ramp) | ✅ Implemented | `bridge/management_engine.py`, `over_temperature_derate` |
| F4 (cell-spread monitor) | ✅ Implemented (monitor-only) | `bridge/management_engine.py`, `cell_imbalance_monitor` |
| F5 (soft-cut persistence) | ✅ Implemented | `bridge/management_engine.py`, `low_voltage_cutoff` |
| F6 (emergency temp tier) | ✅ Implemented | `bridge/management_engine.py`, `over_temperature_derate` |
| F7 (80% target alignment) | No action needed | — |
| F8 (fault latching) | ✅ Implemented (2026-08-01) | `bridge/management_engine.py`, `_hard_latched` |

All seven implemented findings are unit-tested in `tests/test_management_engine.py` (run with
`py tests/test_management_engine.py`) and cross-referenced in
`11-manual-verification-checklist.md`. GUI fields/help text updated in `gui/panels.py`.

---

## Sources

General/industry references (same caveat as `05`: none are RZ450e-specific):

- [Battery University BU-410 — Charging at High and Low Temperatures](https://www.batteryuniversity.com/article/bu-410-charging-at-high-and-low-temperatures/) — charge temp windows, ~0.02C at −30°C, sub-freezing derate practice
- [Lithium Plating Mechanism, Detection, and Mitigation in Lithium-Ion Batteries (Prog. Energy Combust. Sci., review)](https://www.sciencedirect.com/science/article/abs/pii/S0360128521000514)
- [Optimal charging strategies to prevent lithium plating at low ambient temperatures (J. Energy Storage)](https://www.sciencedirect.com/science/article/abs/pii/S2352152X19300891)
- [Degradation from combined current rate + operating temperature during fast charging (J. Energy Storage)](https://www.sciencedirect.com/science/article/abs/pii/S2352152X22008209) — incl. the 4000→40 cycles at 20°C→10°C data point
- [The Dilemma of C-Rate and Cycle Life for Lithium-Ion Batteries (review PDF)](https://pdfs.semanticscholar.org/e97b/03c99ff21fe4f13d304c11581f75b499e90c.pdf)
- [Thermal Runaway in Lithium-Ion Batteries: Mechanisms, Prediction, Mitigation (MDPI Batteries review)](https://www.mdpi.com/2313-0105/12/3/88) — stage onset temperatures
- [Preventative Strategies to Mitigate Thermal Runaway in NMC532-Graphite Cells (MDPI Batteries)](https://doi.org/10.3390/batteries10030104)
- [Detecting Lithium-Ion Battery Thermal Runaway (MoviTHERM)](https://movitherm.com/feeds/blog/thermal-runaway-detection) — dT/dt detection practice
- [EV Lithium Battery Thermal Runaway: Triggers & Global Standards (Bonnen)](https://www.bonnenbatteries.com/ev-lithium-battery-thermal-runaway-trigger-mechanisms-global-standards-compared/)
- [Mechanism of the entire overdischarge process and overdischarge-induced internal short circuit (Nature Sci. Reports)](https://www.nature.com/articles/srep30248) — copper dissolution, ISC at ~−12% SoC
- [Copper Dissolution in Overdischarged Lithium-ion Cells: XPS/XAFS analysis (J. Electrochem. Soc.)](https://iopscience.iop.org/article/10.1149/1945-7111/ab697a) — anode potential ~3.4–3.5 V trigger
- [Deposition of copper during deep discharge (Nature Sci. Reports)](https://www.nature.com/articles/s41598-021-85575-x)
- [Calendar Aging of Lithium-Ion Batteries: Impact of the Graphite Anode (J. Electrochem. Soc.)](https://iopscience.iop.org/article/10.1149/2.0411609jes)
- [Temperature and SoC impact on long-term storage degradation (P2D analysis, PMC)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12219620/) — accelerated fade at 100% SoC storage
- [Strategies for Enhancing Battery Life Under Fast Charging: NMC cycling (MDPI Batteries)](https://www.mdpi.com/2313-0105/12/2/73) — 4.1 V vs 4.2 V ceiling effect
- [Review of Cell-Level Battery Aging Models for EVs (MDPI Batteries)](https://www.mdpi.com/2313-0105/10/11/374)
- [Derating of Lithium-ion Cells: SoC, C-rate, temperature relationship (EVreporter)](https://evreporter.com/derating-of-lithium-ion-cells-relationship-between-soc-c-rate-and-temperature/)
- [Factors that impact lithium-ion cycle life (EVreporter)](https://evreporter.com/understanding-factors-that-impact-the-cycle-life-of-a-lithium-ion-cell/)
- [How do BMS strategies balance performance and battery lifespan? (EV Engineering Online)](https://www.evengineeringonline.com/how-do-bms-strategies-balance-performance-and-battery-lifespan/) — production cold-derate behavior
- [Understanding functional safety: ISO 26262 guide (EV Engineering Online)](https://www.evengineeringonline.com/how-does-iso-26262-road-vehicles-functional-safety-standards-apply-to-evs/)
- [UL 2580 and ISO 26262 EV battery certifications (LEAP)](https://leap.hiitio.com/ul-2580-and-iso-26262-ev-battery-certifications/) / [UN38.3, IEC 62660, ISO 26262 explained (LEAP)](https://leap.hiitio.com/ev-battery-pack-certification-guide-un38-3-iec-62660-iso-26262-explained/)
- [Orion BMS — How Cell Balancing Works](https://www.orionbms.com/manuals/utility_o2/param_balancing_description.html) — ΔV balancing thresholds
- [Early Signs of Cell Imbalance in High-Voltage EV Batteries (Midtronics)](https://www.midtronics.com/blog/early-signs-of-cell-imbalance-in-high-voltage-ev-batteries/) — 30–50 mV rest-divergence weak-cell signature
- [How do you balance cells in a lithium-ion battery pack? (EL-CELL)](https://www.el-cell.com/how-do-you-balance-cells-in-a-lithium-ion-battery-pack/)
