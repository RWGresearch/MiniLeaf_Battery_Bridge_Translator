# Parameter Clamping Audit

**Working document, added 2026-08-07.** Triggered by the Rev 59 fix (`ac_min_kw`/`ac_max_kw` -
the AC charger's configurable power-request floor/ceiling) and the user's follow-up question:
*"it seems we need to add min and max limit inputs for each item we control, just like we did for
the charger kW min and max... go through every parameter we generate an output for and see what
clamps it, what it's set to, and write in the checklist each item."*

**This doc does not change any code.** It's a review pass: for every value this bridge transmits
to the Leaf, what actually bounds it today, and — for anything that's currently a **fixed constant
with no config/GUI input at all** — whether it should get one. Check items off as you review them,
same discipline as `docs/15`. Each item has two checkboxes for your verdict:

- **`[x] Set once`** — this should stay a fixed value (protocol-confirmed, hardware-encodable
  range, or a real constant that never needs per-vehicle tuning). No GUI input needed beyond what
  exists.
- **`[x] Configurable`** — this needs a new user-editable min/max (or single value) input, the
  same pattern as `ac_min_kw`/`ac_max_kw` (a plain `Entry` field in the relevant GUI panel, backed
  by a `(lo, hi)` bounds entry so it can't be typed/loaded out of range).

I've marked my own recommendation on each item (`Recommend: ...`) but these are judgment calls,
not researched safety numbers — you have the final say, per this project's own "confirmed vs.
unverified" discipline (`CLAUDE.md`).

---

## How the clamping actually works today (context)

Two independent layers, easy to conflate:

1. **`leaf_signals.RANGES`** (`bridge/leaf_signals.py`) — the **hardware-encodable range** for
   every output field, applied by `clamp_state()` right before a frame is built. This exists
   purely so an out-of-range value (from a bad mapping tie, a derived-signal formula, or a taper
   bug) can't silently **wrap** into a nonsense raw CAN value instead of saturating — it is *not*
   a safety/vehicle-capability limit, just "what the CAN field can physically hold." Applies to
   every field in `SLIDERS`/`CHARGE_SLIDERS`/`ZE1_62_SLIDERS`/`CHECKS` universally, no per-feature
   opt-out.
2. **`ac_min_kw`/`ac_max_kw`** (and the placeholder `dc_min_kw`/`dc_max_kw`) — a **different kind
   of bound**: a real vehicle-capability ceiling/floor (e.g. "the Leaf's onboard AC charger tops
   out at 6.6kW") that the *management logic itself* clamps its own target/output against, before
   `RANGES` ever gets involved. This is the pattern being asked about here — Part A below is the
   direct audit of where else this pattern applies vs. where it's missing.

---

## Part A — Power-limit outputs (the direct `ac_min_kw`/`ac_max_kw` parallel)

These are the fields `ManagementEngine` actively computes every tick (not passive mapping
passthroughs) — the ones where "what's the real floor/ceiling this should ever request" is a
meaningful question.

### A1. `charger_limit_kw` ("Max power for charger") — HAS min/max already (reference pattern)
- Config: `ac_min_kw` (default 0.5kW) / `ac_max_kw` (default 6.6kW, "the Leaf's real onboard AC
  charger ceiling"), `charge_emulation` tab, `gui/panels.py` `ChargeEmulationPanel`.
- Clamps: the charge-ramp target (`RealtimeEngine._apply_charge_ramp()`) AND the AC taper's floor
  (`ac_charge_taper` in `management_engine.py`).
- [x] **Set once** — no, this one's already configurable, listed here only as the reference
      pattern the items below are compared against.
- Notes: _____________________________________________________

### A2. `discharge_limit_kw` ("Discharge power limit") — NO min/max config exists
- Driven by: `discharge_power_taper` (per-cell voltage ramp, `taper_start_v`/`taper_zero_v`/
  `recovery_ramp_s`) — multiplies whatever value was already there by a 0.0-1.0 factor.
- "Whatever value was already there" = the Signal Mapping tie's output if one exists, otherwise
  the static `SLIDERS` default (**110.0 kW**) — there is currently **no dedicated ceiling** the
  taper caps its own input against (unlike `ac_max_kw` capping the AC charger's request to a real
  6.6kW ceiling), and **no floor** — the taper genuinely reaches true 0.0 kW, not a held minimum
  (unlike `ac_min_kw`'s "hold here, don't make the VCM react to a literal 0kW request" design).
- Only bound today: `leaf_signals.RANGES['discharge_limit_kw'] = (0, 255.75)` — the raw CAN field's
  hardware-encodable range, not a real vehicle limit.
- Recommend: **Configurable** — add `discharge_max_kw` (the real vehicle/motor-inverter draw
  ceiling this pack should ever be asked to supply) to `discharge_power_taper`'s own config, same
  GUI pattern as `ac_max_kw` living alongside the AC taper's own thresholds. Whether a nonzero
  `discharge_min_kw` floor is also warranted (same "don't hand the VCM a literal zero" reasoning
  `ac_min_kw` was added for) is a real open question — the AC case needed it because a real bench
  test showed hunting; there's no equivalent real-hardware finding for discharge yet, so this may
  be an unnecessary floor rather than a needed one. Your call.
- [ ] Set once
- [x] Configurable
- Notes: _____________________________________________________
- **RESOLVED 2026-08-08**: `discharge_min_kw`/`discharge_max_kw` added (default 0.0/110.0kW,
  `discharge_max_kw` grounded in `docs/12` §6's researched 80-110kW Leaf drive-power figure).
  Wired into `discharge_power_taper`'s `apply()` block, GUI (`ManagementPanel.FEATURE_FIELDS`),
  `FEATURE_FIELD_BOUNDS`, and `_CONFIG_SANITY_CHECKS`. See `main.py`'s changelog for the full
  implementation record.
- **REAL-HARDWARE FINDING, 2026-08-09**: on the real ZE1 40kWh test vehicle, `discharge_max_kw`
  around 40kW brings the turtle (reduced-power) dash icon on, and `discharge_min_kw` below roughly
  3kW makes the car start turning off power systems entirely — both are the Leaf's own reaction to
  the transmitted value, not a bridge bug. Explains why the user's real saved `config/profile.json`
  already runs `discharge_min_kw=5.0`/`discharge_max_kw=40.0` (margin above/below both observed
  thresholds) rather than the 0.0/110.0 code default. See `docs/05`'s default table and `docs/15` B8.

### A3. `charge_limit_kw` ("Charge/regen power limit") — NO min/max config exists
- Driven by: `charge_target_taper` (REGEN ONLY, per-cell voltage ramp, `regen_full_v`/
  `regen_zero_v`/`recovery_ramp_s`) — same 0.0-1.0 multiplicative pattern as discharge above, no
  separate ceiling/floor config. **Also** forced to `0.0` by `ac_charge_taper`'s stop-charging
  cutoff/target-reached logic while actually charging (that part's fine — it's an explicit stop,
  not a ceiling question).
- "Whatever value was already there" = mapping tie output or the static `SLIDERS` default
  (**70.0 kW**).
- Only bound today: `leaf_signals.RANGES['charge_limit_kw'] = (0, 255.75)`, same hardware-encodable
  range, not a real limit.
- Recommend: **Configurable** — add a `regen_max_kw` (the Leaf's real max regen-acceptance
  ceiling — you mentioned "I can regen WAY more power than I can AC charge" when this feature was
  originally split from AC charging, so this ceiling is a real, distinct number worth capturing
  explicitly rather than leaving implicit in whatever the mapping produces). Same open question as
  A2 on whether a nonzero `regen_min_kw` floor is warranted.
- [ ] Set once
- [x] Configurable
- Notes: in the past, i did not realise that the regen Charge/regen power limit is the thing that control's 
the regen when driving, so we need to map that output in drive mode so that it adhears to what we have set up in the 
 per cell regen, we need to confirm this is maped corectly. 
The added config cap's will be helpfull. 
- **RESOLVED (config) 2026-08-08**: `regen_min_kw`/`regen_max_kw` added (default 0.0/70.0kW -
  70.0kW deliberately NOT changed to `docs/12` §6's researched ~36kW/0.5C figure yet, per your own
  directive to ship with no behavior change first). Wired into `charge_target_taper`'s `apply()`
  block (with the emergency tier explicitly bypassing the floor - see `main.py`'s changelog),
  GUI, `FEATURE_FIELD_BOUNDS`, `_CONFIG_SANITY_CHECKS`.
  **On your "is this actually mapped correctly in drive mode" question**: confirmed in code -
  `charge_target_taper` computes and writes `charge_limit_kw` on every single tick unconditionally,
  regardless of whether `charge_permission_input` (charging) is active - it is NOT gated to
  charging-only the way `charger_limit_kw`'s AC taper is (that split was the whole point of the
  2026-08-07 driving-vs-charging separation fix). So the wiring itself is correct by inspection.
  What's still open is the REAL-HARDWARE half of your question - does the real Leaf dash/VCM
  actually react to this transmitted value the way you'd expect while driving - which is
  `docs/15-real-hardware-test-checklist.md` B7's job, still unchecked there. Worth a live test.
- **REAL-HARDWARE FINDING, 2026-08-09**: on the real ZE1 40kWh test vehicle, `regen_min_kw` reaches
  true zero cleanly, but `regen_max_kw` only has any observable effect once set below what the car
  can actually accept — anything from 70kW (the current default) down to ~40kW is a no-op, since
  the car's own regen ceiling sits around 40kW. That's a Leaf limit, not a bridge setting. Peak
  regen varies a lot by Leaf generation/pack: 1st-gen LEAF (24/30kWh) ~20-30kW, 2nd-gen LEAF
  (40kWh, this test vehicle) just over 40kW, LEAF PLUS (62kWh) ~60-80kW peak bursts — so
  `regen_max_kw` needs to sit at/below whichever ceiling applies to the actual car for the setting
  to matter at all. See `docs/05`'s default table and `docs/15` B7.

### A4. `chg2_limit_kw` (0x1ED, ZE1 62kWh charger-limit field) — not wired to any logic at all
- Static/mapped only — `ZE1_62_SLIDERS` default 10.0kW, range (10, 204). Explicitly flagged
  **"[UNVERIFIED upstream]"** in its own label (no real 62kWh capture exists yet in the source
  project) and never read by any taper/ramp logic (unlike `charger_limit_kw`, this field has no
  active management at all right now).
- Recommend: **Set once** for now — there's no active logic to give a floor/ceiling to yet. Revisit
  once/if this field gets wired to real behavior (same category as the `dc_min_kw`/`dc_max_kw`
  DC-fast-charge placeholders below, which are explicitly "no active logic yet" by design).
- [ ] Set once
- [x] Configurable
- Notes: we dont have any 62 kw pramiters yet. more testing needed for this in the future.

---

## Part B — Every other mapped/generated output signal

None of these are computed by `ManagementEngine` — they're either a direct Signal Mapping tie, a
derived formula (`mapping_engine.derive_capacity_outputs()`/`soh_percent`), or a static default.
For all of them, the only clamp is `leaf_signals.RANGES` (the hardware-encodable range,
`clamp_state()`) — there's no "real vehicle capability" ceiling/floor question the way there is for
a power limit, since these are telemetry/status passthroughs, not decisions this bridge is making.
Grouped by CAN ID; check off as reviewed.

### 0x1DB — Battery status
- [x] `pack_voltage_v` (0-450V) — passive telemetry (mapped from RZ450e pack voltage). Recommend:
      **Set once** (RANGES is a hardware bound, not a safety decision).
- [x] `pack_current_a` (-400…+200A) — passive telemetry. Recommend: **Set once**.
What is this min and max on the leaf side? I feel it should go outside this if the data shows it should? 
basicaly, dose this have cap's at -400 and + 200?  let me know and we can look from there. 
I may need min and max settings due to the liminets on the leaf. 
  **Answered 2026-08-08**: yes, and it's a real Nissan DBC-sourced value, not arbitrary -
  `Refrance/Leaf_BMS_Emulator`'s own DBC comment confirms "DBC range -400..+200 A (max discharge /
  max charge)" for this exact 11-bit signed field. It's the documented real-world Leaf-side limit
  already, no separate min/max needed - closed, no action taken.

- [x] `usable_soc` (0-100%) — passive telemetry. Recommend: **Set once**.
- [x] `failsafe_status` (0-7 raw) — passive/static enum. Recommend: **Set once**.
- [x] `relay_cut_request` (0-3) — **management-driven** (hard-cut latch), excluded from Signal
      Mapping targets (`MANAGEMENT_EXCLUSIVE_KEYS`) — `ManagementEngine` is the sole authority,
      no separate min/max applies. Recommend: **Set once** (not a power value).
- [x] `discharge_pwr_sts` (0-3) — passive/static status enum, never touched by management or
      realtime engine logic. Recommend: **Set once**.

### 0x1DC — Power limits
- Covered in Part A (`discharge_limit_kw`, `charge_limit_kw`, `charger_limit_kw`).
- [x] `charge_pwr_sts` (0-3) — passive/static status enum, same as `discharge_pwr_sts`. Recommend:
      **Set once**.
	  i think this should be configuribal for the cutoff points. but i guess thats what part A covers? 
		also note that the discharge liment need a configurabal hestritis, the last test showed that it was quite humpy.
		the fast cut back is good. but we need to make it slower to reset back up.  
### 0x55B — Fine SOC
- [x] `fine_soc_pct` (0-102.3%) — passive telemetry. Recommend: **Set once**.

### 0x5BC — Display / SOH
- [x] `soh_pct` (0-127%) — **derived** (`soh_percent` combine formula from RZ450e capacity_ah).
      Bounded by the universal `RANGES` clamp same as every other field; unlike `gids`/`qc_full_wh`
      below, no confirmed real-pack scenario has actually pushed this one out of range, so this is
      a backstop, not a known-active clamp. Recommend: **Set once** — a hardware-encoding backstop,
      not a tunable safety number.
- [x] `gids` (0-1023) — derived (`derive_capacity_outputs()`). Same as above. Recommend: **Set
      once**.
	  we need to confirm we are calulating this corectly: 
	  One GID equals roughly 80 watt-hours (Wh) of electrical energy. 
	  A brand-new 24 kWh Leaf pack holds about 300 GIDs, 
	  while larger packs (like 40 kWh) scale up to around 500 GIDs.
	  with this data we have a 72KWH pack, we have about 900 gid's. 
	  however, Absolute Energy vs. Percentage: Unlike State of Charge 
	  (SOC %), which shows how "full" a battery is relative to its current 
	  degraded health, GIDs give an absolute measure of the actual energy left in the pack
	  we have about 64 kw of real usibal energy at 100% SOH. 
	  we need to calulate the gid's thats 800 for the usibal energy. and its at 94% SOH.
	  thats about 752 gid's at 100% SOC. there for this needs maped so that the math works out
	  using that calulation. dose not need min / max. 
- **RESOLVED 2026-08-08**: `mapping_engine.derive_capacity_outputs()` rewritten exactly per your
  math above - `gids = usable_wh/80` where `usable_wh` now derives from `vehicle.usable_capacity_kwh`
  (default 64.0kWh) x measured SOH, not gross `capacity_ah x pack_v`. Confirmed via
  `tests/test_mapping_engine.py` with your exact inputs (94% SOH, 100% SOC): `gids == 752.0` exactly.
  Correctly no min/max fields added, as you noted.
- **Follow-up, same day**: you asked "so where is the GIDs, is that a mapping or a hardcoded value"
  and "soh_fraction could be configured" - `soh_fraction`'s divisor (previously the hardcoded
  `NAMEPLATE_CAPACITY_AH` module constant, 201.00 Ah) is now also a live `vehicle.
  nameplate_capacity_ah` field, editable right next to `usable_capacity_kwh` on the Vehicle panel.
  GIDS itself deliberately stays a fixed named derivation, not a generic Signal Mapping tie (would
  need a new combine type / go against this project's "curated features, no generic scripting"
  rule) - but every real number feeding it is now yours to set.
- [x] `capacity_bars_raw` (0-15) — passive/static (with a confirmed real mapping tie from
      `capacity_pack1_ah`). Recommend: **Set once**.
	  i think this one already clamps? but those are not settings for the user to set? 
  **Answered 2026-08-08**: correct on both counts - clamped via `RANGES` (0-15), and driven by a
  confirmed real mapping tie (`capacity_pack1_ah -> capacity_bars_raw`, user-confirmed on real
  hardware 2026-07-31), so the tie itself IS the setting (remappable in Signal Mapping if ever
  needed, but the shipped default is already correct). No action taken.
- [x] `pwr_limit_reason` (0-7) — passive/static enum. Recommend: **Set once**.
what currently drives this? its jsut set one right? 
  **Answered 2026-08-08**: confirmed - purely static (default 0), nothing in `management_engine.py`
  or `realtime_engine.py` touches it. Could be wired to reflect which feature is currently limiting
  power as a future feature, but that's a real addition, not a clamping fix - not done here.
- [x] `temp_segment_pct` (0-100%) — passive telemetry. Recommend: **Set once**. 
     i think this needs liments. 
  **Answered 2026-08-08**: you confirmed in the same-day follow-up ("its driven by the input and
  matches the output so i think its fine as is") - closed, no action.
### 0x59E — Quick-charge capacity
- [x] `qc_full_wh` (0-51100Wh) — derived (`derive_capacity_outputs()`), can legitimately exceed
      the 9-bit range for a larger pack — same RANGES-clamp backstop as `gids`/`soh_pct`.
      Recommend: **Set once**.
		this needs to be driven by the known KW usibal. drived from gid's as we calulated. 
		however we need to add a cut off for this. basicaly we cant QC at max wh. its about 80%
so we need max SOC %, and we need to drive using GID's 		
- [x] `qc_remain_wh` (0-51100Wh) — same as above. Recommend: **Set once**.
	same as above. but calulated for remaining. not full.  
- **RESOLVED 2026-08-08**: `qc_full_wh`/`qc_remain_wh` now both derive from the same usable-capacity
  GIDS fix above, capped at `qc_max_soc_pct` (new field, default 80.0%, matches your "cant QC
  at max wh, its about 80%" note exactly) - `qc_full_wh = usable_kwh_at_soh * qc_max_soc_pct%`,
  `qc_remain_wh = max(0, qc_full_wh - usable_wh)` (capped at 0 once already past the ceiling, per
  your explicit confirmation when asked). PROVISIONAL - no DC fast-charge testing done at all yet,
  marked as such in code/docs (same status as `temp_segment_pct`'s own formula). **Follow-up, same
  day**: `qc_max_soc_pct` moved out of `vehicle` into `charge_emulation` (per your directive "the 80%
  QC needs to be on the charge emulation") - GUI field now sits on the Charge Emulation tab next to
  the DC placeholder fields, not the Vehicle panel; a one-time migration handles a profile saved
  during the brief window it lived in `vehicle`. Correctly no min/max
  fields added.
- [x] `soc_correction` (0-200 raw) — derived, real-hardware-confirmed formula (2 counts/%).
      Recommend: **Set once**.

### 0x5C0 — History data
- [x] `batt_temp_c` (-40…86°C) — passive telemetry. Recommend: **Set once**.
whats the min and max that this can drive to and are the clamped? 
  **Answered 2026-08-08**: yes, clamped via `RANGES` (-40 to 86°C), and matches the real encoding -
  `build_5c0()` packs it as `(temp_c + 40) & 0x7F` (7-bit field), true encodable range -40 to +87°C,
  so the documented -40...86 range is correct.
- [x] `dtc` (0-255 raw) — passive/static. Recommend: **Set once**.

### Flags (not power values — 0/1 clamp is inherent, listed for completeness only)
- [x] `main_relay_on` — passive/static (default 1), never touched by `ManagementEngine` today
      (worth separately confirming this is intentional — see Part D below, this is a behavior
      question, not a clamping one).
- [x] `interlock` / `full_charge_flag` / `capacity_empty` — all three **management-driven**,
      excluded from Signal Mapping targets. Recommend: **Set once** (booleans, not power values).
- [x] `ir_malfunction` — passive/static. Recommend: **Set once**.

---

## Part C — Fixed timing/protocol constants with zero GUI/config exposure

These are plain Python module-level constants today — no Entry field, no profile.json key, nothing
editable short of changing code. Some are byte-verified real-hardware protocol facts (should almost
certainly stay fixed); a few are this bridge's *own* invented values, not ported from either
reference project, and are explicitly flagged in their own comments as unconfirmed/tunable-later —
those are the better candidates for an actual input.

### Leaf-protocol timing (`bridge/leaf_signals.py`) — real-hardware-confirmed staging
- [x] `T_1DB_START`/`T_55B_START`/`T_59E_START`/`T_PH_B`/`T_PH_C`/`T_VALID`/`T_RUNNING` (startup
      staging, ms) — ported/confirmed real Leaf startup sequence (`docs/07`). Recommend: **Set
      once** — changing these would desync from the real VCM's expected handshake timing.
- [x] `PWRDOWN_STAGE2_MS`/`PWRDOWN_STAGE3_MS`/`PWRDOWN_STAGE4_MS` (shutdown staging) — same
      category, ported/confirmed. Recommend: **Set once**.
- [x] `PWRDOWN_DEFAULT_COOLDOWN_S` (1.0s genuine-bus-quiet re-arm wait) — ported from
      Leaf_BMS_Emulator's confirmed `PWRDOWN_DEFAULT_COOLDOWN_S`. Recommend: **Set once**.
- [x] `CHG_IDLE_RAW` / `CHG_RAMP_START_RAW` / `CHG_RAMP_RAW_PER_S` (1023 / 100 / 20.0 raw-per-sec) —
      real-hardware-confirmed encoding constants for the charger-ramp field itself (bit-diff
      confirmed against Leaf_BMS_Emulator). The RATE these produce is already exposed as the
      `chg_uprate_level` (0-7) GUI slider - these three are the encoding math underneath it, not
      independently meaningful numbers. Recommend: **Set once**.

### Wind-down/detection timing (`bridge/leaf_signals.py`) — mixed provenance
- [x] `IGNITION_IDS` (which CAN IDs count as "ignition run-state") — protocol fact (which real IDs
      the Leaf sends), not a tunable number. Recommend: **Set once**.
- [x] `IGNITION_QUIET_S` (0.5s) / `IGNITION_OFF_DELAY_S` (10.0s) / `IGNITION_GRACE_S` (10.0s) —
      ported wind-down timing, real-hardware-confirmed per `docs/07`. Recommend: **Set once**.
- [x] `CHG_END_STOP_S` (3.0s, the "genuine unplug/replug" gap threshold — also gates clearing a
      latched hard cut / the new AC charge-stop latch) — ported/confirmed. Recommend: **Set once**
      — this one's also load-bearing for the Rev 58/59 latch-clearing logic, so a bad edit here has
      real safety consequences (a too-short value could let a brief comms blip look like a replug
      and clear a latched stop early).
- [x] `CHG_STALL_TIMEOUT_S` (15.0s) / `CHG_CMD_IDLE` (100 raw) / `CHG_CMD_FRESH_S` (0.5s) — ported/
      confirmed charge-request detection timing. Recommend: **Set once**.
- [x] `BUS_SILENCE_TIMEOUT_S` (30.0s, the bridge's OWN 6th wind-down trigger, added 2026-08-06) —
      **NOT** a ported/confirmed value, explicitly documented as a defensive addition "the original
      real-bench discrepancy this trigger was added to guard against... has not been reproduced or
      explained" (`docs/15` B19). Recommend: **Configurable** — this is exactly the kind of
      bridge-invented, not-yet-tuned number that's worth exposing so it can be adjusted without a
      code change while B19 is still open.
- [x] Charge Emulation panel's `require_live_data_to_charge` startup gate has no explicit timeout
      of its own (by design - a one-time "ever live" check, not a timer) - N/A, not a candidate.

### AC taper convergence rate (`bridge/management_engine.py`) — bridge-invented, self-flagged as unconfirmed
- [x] `_AC_LEVEL_DOWNSHIFT_KW = (3.0, 1.5, 0.75, 0.4, 0.2, 0.1, 0.05)` (the 7 level-selection
      thresholds) — own comment states these are **"NEW, TUNED STARTING VALUES... not researched
      or real-hardware-confirmed"** and flags them as "the retest most likely to need a follow-up
      tuning pass" (`docs/15` B20). Recommend: **Configurable** — strong candidate given the code
      comments already anticipate needing to retune these after a live retest.
- [x] `_AC_LEVEL_HYSTERESIS_MULT = 1.5` (upshift deadband multiplier) — same category, untested
      constant. Recommend: **Configurable**, or at minimum bundle with the thresholds above if you
      decide those need exposing.

### RZ450e-side input polling (`bridge/rz450e_signals.py`) — not an output, listed since you asked about "every fixed item"
- [x] `DID_RESPONSE_TIMEOUT_S` (5.0s) / `DID_INTER_REQUEST_GAP_S` (0.3s) — UDS/DID polling timing
      for slow signals (SoC, capacity, etc.) from the RZ450e side. Not Leaf-output-facing, but
      genuinely fixed/unexposed. Recommend: **Set once** unless real hardware shows the RZ450e's
      DID response is slower/faster than this in practice.

---

## Part D — Behavior questions surfaced during this audit (not clamping, flag for separate follow-up)

- `main_relay_on` is never touched by `ManagementEngine` (stays whatever the static default/mapping
  produces, independent of `relay_cut_request`/hard-cut state) — worth confirming this is
  intentional (a real Leaf might expect `main_relay_on` to drop alongside `relay_cut_request`
  during a hard cut) rather than an oversight. Separate from this audit's clamping scope; flagging
  so it doesn't get lost.
no this is fine. leave it as is. its corect. 
---

## After reviewing

For every item marked **Configurable**, fold it into a real implementation task (new
`(lo, hi)` bounds entry + GUI `Entry` field + `default_config()`/`CHARGE_SLIDERS` default, same
pattern as `ac_min_kw`/`ac_max_kw`) — this doc is the decision record, not the implementation.
