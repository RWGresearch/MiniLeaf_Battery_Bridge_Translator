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
2. **RESOLVED 2026-08-01 — Exact staleness-watchdog behavior when only some source groups go
   stale.** This question's own premise turned out to be wrong once checked against the
   implementation (`docs/13` Part 10, item #2): the watchdog does not track a per-source-group
   "worst group" at all. Per `06-realtime-engine-and-watchdog.md` section 3 (docs/13 item 1.1), it
   tracks freshness of **every registered input signal individually** — all 96 per-cell voltages,
   all 16 temp probes, every fast/slow scalar, plus the keep-alive counters — and takes the single
   worst age across all of them (`ManagementEngine.apply()`'s `staleness_watchdog` block,
   `rz_state.ages_of(rz450e_signals.INPUT_SIGNAL_KEYS)`). So there is no "only the DID/PID group
   goes stale while raw-CAN stays fresh" case to specify a special interaction for — any single
   signal going stale, regardless of which group it belongs to, drives `worst_age` past the same
   60s soft-cut/65s hard-cut schedule described in that section. This is a *stricter* answer than
   the original question assumed ("raw-CAN covers the safety-critical quantities" is no longer the
   reasoning — every signal is now watched, not just that subset).
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
   **Still open as of the 2026-08-08 GIDS formula fix** (`04-signal-mapping.md` - usable-capacity-
   based, not gross-capacity-based): re-checked with the new formula (back-of-envelope,
   `usable_capacity_kwh`=64.0, 94% SOH → 752 GIDs at 100% SOC, linear in SoC%) - GIDS≈49 now falls
   at ~6.5% SoC, GIDS≈5 at ~0.66% SoC, both close to where the OLD formula already put them
   (~6.0%/~0.6%), so the fix doesn't meaningfully change the picture. This is still an SoC%-based
   sanity check, not the voltage-based one this item actually wants - remains open until real GIDS
   values are flowing and can be cross-checked against actual per-cell voltage at that point.
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
5. **RESOLVED 2026-08-04 — Single combined RZ450e adapter.** Collapsing bus1/bus2 onto one PCAN
   connection (per the user's 2026-07-31 correction) assumed both logical buses are visible on the
   same physical CAN wire pair from the adapter's vantage point — unconfirmed until now because it
   depended on whether the battery's two internal buses are electrically isolated networks or just
   an address-space split on a shared line. **User-confirmed on the real bench pack: one CAN
   channel sees both buses.** No revert to two connections needed.
6. **Dashboard bar-gauge display ranges are estimates, not confirmed safety limits.** The `range`
   metadata added to `02-source-signals-rz450e.md`'s signal registry (e.g. cell voltage 2.5-5.0V,
   temp -40..71°C) is only there to scale the dashboard's bar gauges sensibly — it is a separate,
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
   **Not to be confused with the 2026-08-08 QC-capacity-display fix** (`04-signal-mapping.md`,
   `qc_full_wh`/`qc_remain_wh` now capped at `charge_emulation.qc_max_soc_pct`, default 80%) - that's a
   passive telemetry/dash-display formula fix, not active DC charging power-delivery logic; this
   item (active DC fast-charge support) remains completely unaddressed.
13. **`temp_segment_pct` (0x5BC "Dash temperature segment (%)") has no real-hardware-confirmed
    formula.** Added 2026-08-01 as part of a full audit of every Leaf output signal for whether it
    has a live driver at all (`docs/13-review-checklist-2026-08-01.md`) - this field previously
    sat on its static `DEFAULTS` value forever, with no mapping tie targeting it. Shipped a
    provisional linear tie (`bridge/mapping_engine.py`'s `default_ties()`: `temp_max` scaled over a
    0-60°C window to 0-100%, was 32-140°F before the 2026-08-09 Celsius conversion, same physical
    window) so it has *some* live driver, but unlike `soc_correction`/
    `capacity_bars_raw` this has not been checked against a real dash display. Needs the same
    real-hardware confirmation pass those two got before being trusted.
    **Note (2026-08-04): the input signal itself is already effectively "max of the 16 probes,"
    not a separate question.** `temp_max` comes from `0x4A7`'s pack-level extremes field, and as of
    docs/13 item 16.2 that value is now live-cross-checked every tick against the actual min/max of
    all 16 individually-read `0x4AA` probes (`temp_data_cross_check` in `management_engine.py`) - a
    mismatch soft/hard-cuts. So there's no open "should we derive max from the 16 probes instead"
    sub-question; that's already effectively what happens, with a safety net if the two disagree.
    (Update 2026-08-14: the 16-probe side of that comparison is now itself DID `0x1814`-primary,
    `0x4AA`-backup - see item 18 below - so `temp_data_cross_check` transparently benefits from the
    better resolution whenever DID is the active source, with no change needed to this reasoning.)
    What remains unconfirmed is narrower: whether the 0-60°C -> 0-100% *window/formula* matches
    what a real Leaf dash actually shows for this field. Tracked as a to-do in
    `docs/14-validation-test-plan.md`.
14. **Insulation/isolation-resistance monitoring (HV+/HV- to chassis ground) - researched
    2026-08-04, docs/13 item 16.4, not currently readable by this project, but plausibly real.**
    A degrading isolation fault (HV cabling insulation rubbed through, a leaking/ruptured cell) is a
    standard, often regulatorily-required EV HV-pack safety function (ISO 6469-1, UL 2580) -
    generic OBD-II DTC **P0AA6** ("HV Isolation Fault") is used across the industry for exactly this,
    confirmed via web research to apply to both Toyota/Lexus and Nissan EVs specifically, not just a
    hypothetical. It's plausible the RZ450e pack's own ECU maintains an isolation-fault DTC
    internally, independent of anything this bridge currently reads. **Concretely why this project
    can't see it yet, not just "hasn't looked":** every confirmed RZ450e diagnostic signal this
    project uses (`docs/02`) goes through UDS **ReadDataByIdentifier (service `0x22`)** - DTC-style
    isolation-fault codes are read via a *different* UDS service, **ReadDTCInformation (`0x19`)**,
    which neither this project nor `Refrance/RZ450e_battery_can_decode_Project` has ever attempted
    against the RZ450e (checked: not in that project's confirmed signal lists either). **Concrete next
    step if pursued**: attempt a UDS `0x19` request against the same confirmed diagnostic addressing
    already used for service `0x22` (`0x747` tester → `0x74F` BMS response, `docs/02`) and see whether
    it returns a DTC list at all - not yet attempted by either project, so genuinely unknown whether
    this pack even responds to that service. Out of scope for milestone 1 until that capture happens;
    this is a real, well-defined next step, not a vague "look into it someday."
15. **Contactor/relay commanded-vs-actual-state (weld) detection - researched 2026-08-04, docs/13
    item 16.4, architecturally out of scope, not a TODO.** This bridge asserts
    `relay_cut_request`/`interlock` as open-loop *requests* to the Leaf's own VCM; it has no way to
    confirm the real contactors actually opened. Research confirms why this can't be fixed by reading
    more RZ450e signals: weld detection is fundamentally a **VCM/inverter-side electromechanical
    check** (did a commanded contactor-open actually produce a real disconnect, verified via the
    vehicle's own contactor-drive/F/S-relay feedback lines) - it has no representation on either CAN
    bus this project taps into, battery or Leaf side, and it isn't something the *battery* reports at
    all in any EV architecture. The Leaf's own unmodified VCM (which this bridge feeds periodic HVBAT
    frames, per `docs/07`'s "no request/response handshake") presumably still runs its own
    weld-detection logic entirely independently, using vehicle-side signals this project never sees
    regardless of what data the "battery" sends. **Unlike item 14 above, there is no plausible signal
    path to go looking for here** - this stays permanently out of scope, included so a future session
    doesn't re-investigate something that's structurally unreachable from this bridge's position in
    the system.

16. **Unexplained ~110s of continuous transmission with no wind-down, third charge cycle of a real
    bench test (2026-08-05, `logs/minileaf_20260805_200831_Charging to xx% the restart.trc`).** The
    first two charge cycles in that log wound down and re-armed correctly - confirmed by replaying
    the actual captured Leaf-bus RX traffic through the real `ShutdownSequencer` class
    (`tests/check_shutdown_sequencer_replay.py`), which reproduces both real wind-down timings
    almost exactly. The third cycle did not: the real log shows continuous Leaf-bus TX for another
    ~110s past the point every wind-down trigger's own timer should have fired (the replay predicts
    wind-down ~3s after the last `0x1F2` frame, regardless of what `charge_permission_input` was
    doing at the time - checked both ways). Root cause not identified from the capture alone - every
    trigger's own inputs (Leaf-side `0x1F2`/ignition IDs, RZ450e-side `charge_permission_input`)
    look, in the replay, like they should have resolved normally. Possible explanations not yet
    ruled out: a real difference in RZ450e-side `0x358` behavior during that specific window not
    fully captured by the replay's simplified event-driven charge_permission_input tracking, a
    timing/threading interaction only reproducible in the live app (not in an offline replay driven
    by a static log), or something in the live GUI state not visible in the .trc capture at all
    (which only records CAN frames, not GUI/internal engine state). The 2026-08-06 sixth wind-down
    trigger (`docs/07-startup-shutdown-plan.md`'s "Sixth trigger" section, `leaf_signals.
    BUS_SILENCE_TIMEOUT_S`) is a defensive fallback for the general structural gap this exposed, not
    a fix for this specific case - it should prevent an indefinite stay-awake in the future, but does
    not explain what actually happened here. Needs a re-test with the Log panel's timestamps
    cross-referenced against a fresh .trc capture to actually catch this in the act.
    **Note (2026-08-08): the `<name>_log_output.txt` companion file (`docs/08-gui-design.md`'s Log
    panel section, `main.py` Rev 63) now produces exactly this Log-panel/.trc pairing automatically**
    - any future re-test attempt should start from that file (timestamped Log-panel lines plus the
    session's actual settings snapshot) alongside the .trc capture, rather than manually
    cross-referencing the two by hand.
17. **No active check that a reconnected CAN channel is actually carrying the traffic it's supposed
    to (found 2026-08-13, real bench test `minileaf_20260813_162758_discharge regen using SOC.trc`).**
    During that session the RZ450e connection dropped and was manually reconnected via the app
    (`ConnectionsPanel._toggle()`, `gui/panels.py`) several times in a row; one reconnect landed on
    `PCAN_USBBUS2` instead of the `PCAN_USBBUS1` every prior connect/reconnect in the session had
    used, even though the user never physically changed which adapter was plugged into which port.
    Root cause: `BusConnection._auto_reconnect_loop()` (`bridge/can_backend.py`) always retries the
    exact channel string it was last given - it never re-scans - so the channel can only change via
    an explicit `connect(channel, ...)` call (a Connect-button click with a specific channel selected
    in the dropdown). This IS a known PEAK/Windows driver quirk: a PCAN-USB adapter's assigned
    `PCAN_USBBUSx` slot isn't guaranteed stable across a USB-level disconnect/reconnect, especially
    with more than one adapter attached, so a manual reconnect during a rough patch can land on a
    different bus number than before with nothing wrong. In THIS specific session it's very unlikely
    the two logical connections (`rz450e`/`leaf`) actually got swapped onto each other's physical
    adapter - if they had, RZ450e-specific signals would never have decoded from whatever's really on
    the Leaf bus, and the staleness watchdog (60s soft/65s hard) would have fired again almost
    immediately, but the very next `running` window ran clean for 43+ minutes with no staleness event
    - but the app currently has no way to confirm that itself; it just opens whatever channel is
    selected and starts decoding. **Proposed fix, not yet built (2026-08-13 user directive: note it,
    don't build it yet)**: after a (re)connect, confirm real traffic matching that role's expected
    CAN IDs/checksums actually arrives within some short window, and warn distinctly (not just via
    the general staleness watchdog, which takes up to 65s) if it doesn't - this would catch a genuine
    mis-wire in seconds instead of relying on the staleness watchdog's much longer schedule.

18. **Temp probe DID `0x1814` primary-source timing values and `temp_probe_cross_check`'s
    `max_delta_c` are all provisional, user-directed 2026-08-14, not yet real-hardware-confirmed.**
    `did_temp_poll_interval_s` (10s default) and `did_temp_fresh_window_s` (20s default,
    `state.engine_timing`, GUI-editable via the "Timing" tab as of 2026-08-14) were chosen
    from the DID round-robin's real measured cadence (`02-source-signals-rz450e.md`'s ~9s/poll
    figure for the existing 3-item cycle) plus reasoning about headroom, not from an actual
    observed DID `0x1814` round-trip time on this pack -
    real-hardware logging could show the DID responds faster or slower than assumed, which would
    argue for a different freshness window. Separately, `temp_probe_cross_check`'s 2.0°C
    `max_delta_c` (`05-battery-management-safety.md`) assumes CAN quantization (~1.0°C) plus a small
    sampling-time-gap allowance is the only real source of DID-vs-CAN disagreement for the SAME
    probe - real bench data could reveal a larger systematic offset between the two decode paths
    that this starting point doesn't yet account for. Needs a real-hardware session with both
    sources live to confirm or retune all three numbers - see `docs/11-manual-verification-
    checklist.md` and `docs/15-real-hardware-test-checklist.md`.

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
