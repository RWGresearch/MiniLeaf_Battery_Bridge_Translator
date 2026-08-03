# Open Questions

## New to this project

1. **RESOLVED 2026-08-01/03 — `full_charge_flag` re-arm without a physical replug.** The real Leaf
   requires an unplug/replug cycle to resume charging after this flag fires; the RZ450e side has no
   direct equivalent signal. Resolved via the general hard-cut latching mechanism (`12-nmc-bms-
   design-research.md` finding F8): any hard-cut condition (including a charging-permission
   mismatch) now latches via `ManagementEngine._hard_latched`, cleared only by
   `notify_session_start()` (genuine bus-wake power cycle) or `notify_charge_replug()`
   (`charge_permission_input` genuinely absent for at least `CHG_END_STOP_S` = 3.0s before a new
   request — refined `docs/13` item 13.4 after an independent review found the first version could
   be cleared by simply toggling the bridge's own Stop/Start button with the car never actually
   losing power). See `05-battery-management-safety.md`'s "`full_charge_flag` re-arm" section for
   full current behavior.
2. **Exact staleness-watchdog behavior when only some source groups go stale.** The watchdog
   (`06-realtime-engine-and-watchdog.md`) is specified per source-group (fast raw-CAN vs. DID/PID),
   but the interaction when, say, only the DID/PID group goes stale while raw-CAN stays fresh
   hasn't been fully specified — likely fine (raw-CAN covers voltage/current/temp, the safety-
   critical fast-response quantities) but worth confirming once implemented.
3. **GIDS threshold interaction.** The Leaf project found real thresholds (GIDS≈49 = low-battery
   warning, GIDS≈5 = turtle mode) baked into the VCM itself. This project's derived GIDS formula
   (`04-signal-mapping.md`) needs to be checked against these once real numbers are flowing, to
   make sure the cell-voltage-driven `discharge_power_taper` (full power ≥3.00V, zero ≤2.60V/cell
   as of the 2026-08-01 re-anchoring) and `low_voltage_cutoff` (soft cut at 3.00V/cell, emergency
   hard cut at 2.60V/cell, `min_soc_pct` a backup check only — see `05-battery-management-
   safety.md`'s 2026-07-31 correction) don't put the bridge in an unexpected VCM-side low-battery
   state before its own protection features would have acted. Since both are now voltage-driven
   rather than SoC-driven, this is really a question of whether the *voltage* defaults
   (3.00V/2.60V) map to a GIDS range comfortably above 49/5, not a SoC-floor question anymore.
4. **RESOLVED 2026-08-01 — Whether the `charge_permission_input` interlock (`0x358`) needs a "no
   interlock present" default behavior.** This question is specifically about ONE signal
   (`charge_permission_input`) being physically absent/unwired entirely (e.g. an earlier hardware
   revision with no wire run to that pin) - **not** the same thing as the charger-ramp emulation's
   "dual-trigger" requirement (`06-realtime-engine-and-watchdog.md` section 6), which is a different,
   already-completed feature about needing BOTH a real Leaf `0x1F2` request AND this signal being
   actively granted before the ramp runs. Dual-trigger assumes the signal is present and asks
   "is it granted right now?"; this question asks "what if the signal was never wired up at all?" -
   two separate concerns, easy to conflate since they involve the same signal.
   **Now a deliberate, written policy** (previously only an emergent side effect, per the note
   below): if `charge_permission_input` has never arrived this session,
   `SharedState.get_input('charge_permission_input')` returns `None`, and every place that reads it
   (`bool(rz_state.get_input(...))` in `charge_target_taper`, `ac_charge_taper`, and the charger-
   ramp's `rz_authorized`/`charge_authorized`) treats that as `False` - **fails safe to "not
   permitted,"** never "permitted." This was already true in code as of 2026-07-31 (an emergent
   consequence of `get_input()`'s behavior, not a deliberate check anyone wrote), and remains true
   after the 2026-08-01 regen/AC-charger taper split - every new code path introduced in that split
   (`ac_charge_taper`'s `charging_active`) uses the exact same pattern. Confirmed correct and
   promoted to an explicit project policy rather than an incidental side effect: **a missing/unwired
   `charge_permission_input` signal must always be treated as "charging not authorized," by design,
   not by accident.**
5. **Single combined RZ450e adapter — needs hardware confirmation, not just a software assumption.**
   Collapsing bus1/bus2 onto one PCAN connection (per the user's 2026-07-31 correction) assumes
   both logical buses are visible on the same physical CAN wire pair from the adapter's vantage
   point. If the battery's two internal buses are actually electrically isolated networks (not
   just an address-space split on a shared line), one adapter genuinely can't see both and this
   would need to revert to two connections. Confirm against the real bench pack before relying on
   this for anything beyond the current software's assumption.
6. **Dashboard bar-gauge display ranges are estimates, not confirmed safety limits.** The `range`
   metadata added to `02-source-signals-rz450e.md`'s signal registry (e.g. cell voltage 2.5-5.0V,
   temp -40..160°F) is only there to scale the dashboard's bar gauges sensibly — it is a separate,
   looser number from the actual researched/confirmed thresholds in `05-battery-management-
   safety.md`. Don't confuse "the bar looks full" with "this is at a safety-relevant limit" — the
   management panel's status text is the authoritative source for that, not the dashboard bar
   position.
7. **Does the RZ450e pack's internal cell-supervision hardware still balance cells in this
   configuration?** Added 2026-07-31 alongside the new `cell_imbalance_monitor` feature
   (`05-battery-management-safety.md`) — this bridge only monitors/warns on cell spread, it cannot
   balance. If the pack's own internal BMS electronics are still powered and performing passive
   balancing while connected to this bridge (rather than a real Toyota vehicle), spread should stay
   roughly stable over time; if not, spread will likely grow over weeks/months of use, making the
   monitor more important than it might first appear. No way to answer this without extended
   real-hardware observation — track cell spread over several sessions once running against the
   real bench pack.
8. **Overcurrent monitor thresholds (150A discharge / 30A charge-regen) are provisional, not tuned
   to a real drive cycle — and the monitor as a whole cannot see this pack's real operating
   range.** Added 2026-07-31 alongside `05-battery-management-safety.md`'s new `overcurrent_monitor`
   feature — derived from this project's own confirmed specs (comfortably below the `0x023`
   sensor's ±204.7A saturation ceiling; above the Leaf AC charger's ~19A max) as a defensible
   starting point, not from any cell datasheet (none exists for this pack) or observed real driving
   current. **Corrected same day**: the ±204.7A ceiling is a limit of this specific CAN signal's
   12-bit encoding, not the battery — the real pack is rated to a 500A discharge fuse and the
   factory RZ450E's 230kW peak output is ~660A for short bursts (user-confirmed, `02-source-
   signals-rz450e.md`). This monitor is therefore structurally unable to see anywhere near the
   pack's real high-current range; a future hardware revision (a wider-range current sensor/shunt)
   would be needed before real fuse-relevant or peak-relevant current could ever be monitored.
   Real drive-cycle current logging is still needed to confirm the *sub-205A* thresholds don't
   nuisance-warn during ordinary sustained acceleration/hill-climbing, but that's a separate,
   narrower question from the sensor-range gap.
9. **DC fast charging (150kW, ~430A into the pack) is a real pack capability not currently
   addressed anywhere in this project.** User-confirmed 2026-07-31 (`02-source-signals-
   rz450e.md`) that the RZ450e pack is rated for 150kW DC fast charging, separate from the Leaf's
   6.6kW onboard AC charger this bridge's charge-taper/target features are built around
   (`05-battery-management-safety.md`). `03-target-signals-leaf.md` has no DC fast-charge (CHAdeMO)
   signal path documented today, and every current-based safety consideration in this project
   (the charge/regen voltage taper's proactive margins, the overcurrent monitor's thresholds) was
   sized against AC-charger-scale current (~19A), not DC-fast-charge-scale current (~430A). If DC
   fast charging is ever brought into this bridge's scope, every charge-side feature needs
   re-evaluating against that much higher current, not assumed to already cover it.
13. **`temp_segment_pct` (0x5BC "Dash temperature segment (%)") has no real-hardware-confirmed
    formula.** Added 2026-08-01 as part of a full audit of every Leaf output signal for whether it
    has a live driver at all (`docs/13-review-checklist-2026-08-01.md`) - this field previously
    sat on its static `DEFAULTS` value forever, with no mapping tie targeting it. Shipped a
    provisional linear tie (`bridge/mapping_engine.py`'s `default_ties()`: `temp_max` scaled over a
    32-140°F window to 0-100%) so it has *some* live driver, but unlike `soc_correction`/
    `capacity_bars_raw` this has not been checked against a real dash display. Needs the same
    real-hardware confirmation pass those two got before being trusted.

## Inherited from `Refrance/RZ450e_battery_can_decode_Project/`

7. **Amps vs. kilowatts for the wide-range current/power tap** (raw CAN `0x371`/`0x021`) — still
   genuinely unresolved upstream; excluded from `02-source-signals-rz450e.md` entirely until
   settled. Needs a much bigger pack-voltage swing test than any capture so far.
8. **`0x4AF` usage-history table units** — behaves like a charge-throughput accumulator but real
   units (coulombs? Wh? raw ADC-seconds?) never confirmed. Not used by this project.
9. **DID `0x1F9A` vs. PID `0x9A` voltage resolution** — user suspects PID `0x9A`'s voltage copy may
   have finer resolution than the currently-labeled-primary `0x1F9A`. Doesn't block this project
   (both are slow DID/PID sources, secondary to raw-CAN anyway) but worth knowing about if a future
   sanity-check feature is added.

## Inherited from `Refrance/Leaf_BMS_Emulator/`

10. **RESOLVED 2026-07-31 — Dash SOC% formula (`soc_correction`, `0x59E`).** Inherited from
    Leaf_BMS_Emulator as unsolved there ("confirmed to be the dash-% source but confirmed not a 1:1
    raw-to-percent mapping; the real formula was never derived... not something to try to solve
    independently here"). **User confirmed on real hardware** (their own Leaf + this project's own
    bench RZ450e pack, not a Leaf-project capture): **raw 0-200 = 0-100%** (2 raw counts per
    percent) — a plain linear `soc_pct × 2.0` tie, added as a shipped default
    (`bridge/mapping_engine.py`'s `default_ties()`). This project solved independently what the
    reference project's own open question said not to attempt — worth remembering this is *our*
    finding, not sourced from or yet fed back to `Refrance/Leaf_BMS_Emulator` (which stays
    read-only regardless — see `01-project-goals.md`). See `04-signal-mapping.md`'s mismatch #2 and
    `11-manual-verification-checklist.md` for the confirmation record. Reference profile saved by
    the user: `config/7-31-2026-new-SOC-dash-fix.json`.
11. **0x1ED (62kWh charger-limit) field** — unverified upstream, no real 62kWh capture exists yet.
    If this project is ever run against a 62kWh ZE1 pack/car, treat this field's values as unproven
    until confirmed.
12. **Several Leaf dash-behavior signals with "no observed effect, more testing required"**
    (discharge/charge power status, IR sensor malfunction, output power limit reason, voltage
    latch) — these have confirmed defaults but unconfirmed real dash/car effects. Not expected to
    matter for this project's mapping (none of them are currently planned mapping targets from
    RZ450e data), but flagged here in case that changes.

## Process note

New open items discovered while building milestone 1 should be added here, following the same
format: what's unresolved, why it doesn't block current work, and what would need to happen to
resolve it.
