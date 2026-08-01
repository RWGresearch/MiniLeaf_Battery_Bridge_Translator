# GUI Design

Single main window, plain tkinter/ttk (`01-project-goals.md` — reverted from customtkinter
2026-07-31, see that doc's GUI toolkit section). Visually inspired by
`Refrance/Leaf_BMS_Emulator/leaf_hvbat_emulator.py`'s existing `DashWindow`/live-monitor/log style
— the user specifically wants that same "informative" feel kept, and asked (2026-07-31 review) for
the dashboard's bar-gauge visuals specifically, not just its text monitor style.

## Overall layout: three resizable panes + a bottom log

```
+-------------------------+-------------------------------+-------------------------------+
|   RZ450e BATTERY (left) |     MAPPING / MANAGEMENT       |   LEAF EMULATOR (right)        |
|   = all inputs          |         (middle)               |   = all outputs                |
|  <- drag sash to resize ->                          <- drag sash to resize ->             |
+-------------------------+-------------------------------+-------------------------------+
| Connections panel         | [Fault History] [Dashboard]    | Connections panel              |
|  - ONE combined RZ450e    | [Save] [Load] [Simulate...]     | Leaf-facing CAN bus             |
|    adapter (see below)    | Signal Mapping tab             | Vehicle/battery generation      |
|  - auto-reconnect         |  - dropdowns show source ID    | selector (AZE0/ZE1, capacity)   |
| Live decoded values,      |  - "?" popups                  | Live TX values (what's          |
| grouped by CAN message    | Battery Management tab         | actually being sent),           |
| (mirrors DashWindow)      |  - protection features          | grouped by CAN message          |
|                           |  - thresholds, soft/hard tier   | (mirrors DashWindow)             |
|                           | Generated Signals tab           |                                  |
|                           |  - send/don't-send checkboxes   |                                  |
|                           | Charge Emulation tab            |                                  |
|                           |  - charger-request ramp (0x1DC) |                                  |
+-------------------------+-------------------------------+-------------------------------+
| Log (full width, timestamped, thread-safe queue - ported pattern from the Leaf app's logq)   |
+-----------------------------------------------------------------------------------------------+
```

**Fault History lives in its own window now, not this bottom row** — see "Fault History window
(separate, tall and narrow)" below for its full history (it moved twice: main window → Dashboard →
its own window). Reachable via a **"Fault History"** button here, and via a matching button in the
Dashboard's own header — both share the same single window (opening from either one focuses the
same instance rather than creating a second).

The three panes are a `tk.PanedWindow` with draggable sashes, still user-resizable at any time, but
**left and right hold a fixed starting width while only the middle (Configurator) pane stretches**
(changed 2026-07-31, fourth review pass, user request to "fix internal window placement").
Previously all three panes were `stretch='always'`, and Tk's `PanedWindow` distributed the window's
extra width unevenly between them — observed rendering the left pane noticeably wider than its
configured width while the right pane stayed squeezed near its minimum, not the fixed/predictable
layout wanted. This doesn't undo the original 2026-07-31 feedback that a fixed equal-thirds split
cut content off — the middle pane still gets first claim on any extra space, same as before, just
via `stretch` on one pane instead of all three.

**The main window itself now opens at a fixed position, not just a fixed size** (same pass) —
`geometry()` now includes an explicit `+x+y` instead of leaving window placement to whatever the OS
decided, which previously sometimes landed it overlapping other windows.

**Scaled ~15% smaller, matching the Dashboard (2026-07-31, eighth review pass, user request:
"scale down the main app as well to fit the same sizing for font and everything")**: window
1680×980 → **1430×835** (position offset unchanged, that's a screen-placement preference, not a
size); pane widths 440/800/440 → **374/680/374** (left+middle+right = 1428, matching the window's
own width almost exactly); the "RZ450e BATTERY (inputs)" / "CONFIGURATOR" / "LEAF EMULATOR
(outputs)" section headers 16pt → 14pt. Unlike the Dashboard (which had almost no explicit fonts to
begin with), this pass shrunk the **shared ttk theme itself** (`gui/theme.py`'s `apply_style()`):
base font `.` → 8pt (previously unset, inheriting Tk's system default), `Header.TLabel` 14pt→12pt,
`Accent.TLabel` 10pt→9pt, plus proportionally smaller button/tab padding — these three sizes
already matched what the Dashboard was independently using as local per-widget overrides
(`DASH_FONT`/`DASH_HEADER_FONT`/`DASH_ACCENT_FONT` in `gui/dashboard.py`), so this doesn't
double-shrink the Dashboard (an explicit widget-level `font=` always wins over a style default) —
it just gives the main app (which had almost no explicit fonts) the same baseline. Every
character-width `width=N` field elsewhere in `gui/panels.py` shrinks for free alongside the smaller
base font, without needing individual edits.

**Bug found and fixed the same day: `tk.Text`/`scrolledtext.ScrolledText` widgets don't participate
in ttk styling at all** — user report: "did we change the font size as well? It does not fit in the
smaller window as it did." The ttk theme change above only affects *ttk* widgets; `LiveMonitorPanel`
(`gui/panels.py`, the "Live decoded values"/"Live transmitted values" boxes — the densest content in
the whole app) and the main window's own Log panel (`gui/app.py`) both use a plain `tk.Text` /
`scrolledtext.ScrolledText` with no font ever set, so they silently kept rendering at Tk's system
default (~9pt) while the panes around them got narrower — exactly why less of that dense text fit
than before. Confirmed directly (`winfo_reqheight()`/`cget('font')`): these widgets were still
9pt-equivalent after the "15% smaller" pass despite everything ttk-based around them shrinking.
Fixed by adding a shared `BASE_FONT = ('Segoe UI', 8)` constant to `gui/theme.py` (also used inside
`apply_style()`'s own `'.'` style now, replacing what had been a locally-hardcoded tuple) and
passing `font=BASE_FONT` explicitly to every plain-`tk.Text`-family widget: `LiveMonitorPanel`'s
box, the main window's log box, and `gui/info_popup.py`'s "?" help popup text (same gap, lower
stakes, fixed for consistency while touching this). The Dashboard's own log box was already correct
— it got an explicit font in the pass that added it. Verified directly: all 3 `tk.Text` widgets in
the main app now report `{Segoe UI} 8`, matching every ttk widget around them.

All RZ450e data is an input (left). All Leaf data is an output (right). This matches the user's
framing directly: "all the data from the battery currently is going to be inputs, and all of the
data going out to the car will be outputs."

## RZ450e is ONE combined connection, not two

**Correction (2026-07-31 user review)**: the RZ450e side is a single physical adapter connection,
not separate bus1/bus2 connections. One PCAN adapter carries every RZ450e CAN ID (diagnostic/DID
traffic and the fast internal-bus broadcasts together), split internally by CAN ID rather than by
physical bus — matching the "combined bus (1 adapter, split by CAN id)" mode the RZ450e decode
project's own analyzer app already supports. See `docs/02-source-signals-rz450e.md`.

## Left panel — RZ450e battery (inputs)

- **Connections**: one adapter dropdown (populated via a `detect_pcan_channels()`-style scan,
  ported from `rx450e_can_analyzer.py`), rescan button, auto-reconnect toggle, connection status.
- **Live data monitor**: grouped by CAN message (`0x020`, `0x023`, `0x4A9`/`0x4C0` cell voltages,
  `0x4AA` temps, DID/PID values with their own slow-poll cadence indicator), latest decoded value
  per signal, with signal age shown — same informative-text-block pattern as the Leaf app's
  `DashWindow`.

## Middle panel — mapping & management (the configurator)

### Signal Mapping tab

One card per active tie, laid out on two lines so nothing gets cut off horizontally:
- **Line 1 — inputs**: up to 3 input dropdowns (blank = unused slot), each populated from the
  RZ450e signal registry. **Every dropdown entry is prefixed with its source CAN ID or DID** (e.g.
  `[0x020] Pack voltage (whole V)`, `[DID 0x1F5B] State of charge`) — per the user's 2026-07-31
  request, so it's obvious which message a decoded value stands for without cross-referencing docs.
- **Line 2 — conversion + output**: combine-function selector (fixed preset list from
  `04-signal-mapping.md`), scale/offset fields (for `linear`), an output dropdown (same
  source-ID-prefixed style, e.g. `[0x1DB] Pack voltage (V)`, `[0x55B] Capacity empty flag...`), a
  "?" popup, and a remove button.
- **"?" popup**: shows, for the current live values, the input value(s), the conversion applied,
  and the resulting output value, so the user can visually confirm exactly what math is happening.
- "Add mapping" control at the bottom.

### Battery Management tab

One block per protection feature from `05-battery-management-safety.md`:
- Enable checkbox (all on by default).
- Threshold field(s), pre-filled with the researched defaults.
- A live status label (soft/hard/ramp-factor text), refreshed continuously — same
  visual-confirmation principle as the mapping tab.
- The charge-target-taper feature also exposes an "extended mode" checkbox (daily vs. road-trip
  SoC target) — see `05`'s corrected per-cell-driven taper design.
- The discharge-power-taper feature (added 2026-07-31) exposes its own full-power/zero-power
  cell-voltage fields plus a "Recovery ramp (s)" field for its fast-attack/slow-release hysteresis
  — the status text shows both the instantaneous and the currently-applied (rate-limited) factor
  so the hysteresis itself is visually confirmable, not just the underlying curve.
- **Cell voltage is the sole authoritative trigger for every cutoff (2026-07-31 fix)** —
  `low_voltage_cutoff`'s "Min SoC %" field is now explicitly labeled "backup check only, never acts
  alone" in the GUI, and its status text shows whether SoC agrees or disagrees with the
  voltage-based decision rather than ever triggering independently. See `05`'s Design philosophy
  section.
- **Every field in every feature's config is exposed and editable** — confirmed 2026-07-31 by
  auditing `default_config()` against the GUI's `FEATURE_FIELDS` table (no gaps found); anything
  that's a tunable safety threshold shows up here, not hidden in code. Protocol/units math (CAN
  scale factors, bit-encoding formulas) is a separate category, deliberately NOT editable here since
  those are confirmed facts about the wire protocol, not safety policy — see `05`'s design
  philosophy and `12-nmc-bms-design-research.md`.
- **`over_temperature_derate` (restructured 2026-07-31, docs/12 findings F1/F3/F6)** now shows
  separate cold-side fields (evaluated against the coldest probe: cold-derate start, low block) and
  hot-side fields (hottest probe: charge/discharge derate-start/hard-stop, plus a genuine
  "Emergency temp" hard-cut tier separate from the soft ramp ceilings).
- **Two new monitor-only features (added 2026-07-31, docs/12 findings F2, F4)**: `cell_imbalance_
  monitor` (warns on cell-voltage spread) and `overcurrent_monitor` (warns on sustained elevated
  current). Both are explicitly labeled "warn only, no cutoff action" — they surface information the
  bridge doesn't act on, by deliberate design (see `05`).

### Generated Signals tab

One row per internally-generated field (PRUN, toggle bit, `0x1C2` heartbeat, the `0x1DC`/`0x5BC`/
`0x5C0`/`0x5EB` opaque replay tables) — a send/don't-send checkbox, **default checked**.

### Charge Emulation tab (added 2026-07-31)

The charger-request ramp feature (`06-realtime-engine-and-watchdog.md` section 6, `03-target-
signals-leaf.md`), ported from `Refrance/Leaf_BMS_Emulator` after the user asked whether it had
been — it hadn't yet at the time. One block, same visual pattern as a `ManagementPanel` feature:

- **"Emulate charger request" checkbox** (default off, opt-in) — its label states the dual-trigger
  requirement directly ("requires both a real Leaf charge request AND RZ450e charge permission"),
  since that's the single most important thing to understand about this feature.
- **"Charger ramp target (kW)"** (0-92.2) and **"Uprate level / ramp rate (0-7)"** — plain `Entry`
  fields, same `StringVar`-trace pattern `ManagementPanel` uses, clamped to their valid range on
  write.
- **A live status line** ("disabled" / "idle - no active, authorized request" / "STOPPED - charge
  requested but RZ450e permission not granted" / "charger_limit_kw = N kW (ramping/active)") —
  needed here specifically because this feature's state isn't visible anywhere else in the
  Configurator (unlike `ManagementPanel`'s features, which show their status right next to the
  bar/taper they're driving).
- **"?" help** — explains the dual-trigger requirement, the ramp math (0.0kW start, rate per uprate
  level), the stop-flag behavior on a permission mismatch, and that the per-cell overvoltage taper
  still gets the final say over the ramped value regardless.

See the Dashboard section below for the *other* place this feature's live state is surfaced — a
dedicated right-column section, since the two triggers driving it (the Leaf's own `0x1F2` request
and RZ450e's permission interlock) weren't shown together anywhere.

### Future placeholder — Leaf→battery requests (disabled, not implemented)

A greyed-out/disabled tab, visually present but non-functional, for the future case where the Leaf
VCM issues PID/DID requests that need to be answered by the battery side.

### Save / Load / Simulate power-down / Dashboard

Toolbar buttons above the tabs: save/load a named config profile (see `09-stm32-export-format.md`
for the schema), a manual "Simulate power-down" trigger for the ported shutdown sequence
(`07-startup-shutdown-plan.md`), and an **Dashboard** button opening the bar-gauge dashboard window
(see below).

### Start Bridge / Stop Bridge (added 2026-07-31)

A second toolbar row, with a live status label (`Bridge: idle` / `armed - waiting for Leaf bus
traffic` / `running` / `winding down` / `stopped`) and a "?" explaining the behavior:
- **Start Bridge** arms the engine to wait for real Leaf-bus traffic before transmitting anything
  (docs/06 section 0, docs/07) — connect adapters and build the mapping first, then press this when
  ready. RZ450e monitoring and mapping/threshold edits work in the `idle` state too, before Start is
  ever pressed.
- **Stop Bridge** halts transmission immediately — a manual control for reconfiguring, distinct
  from "Simulate power-down" (the graceful staged wind-down while actually running).
- Mapping and battery-management edits are live in either state, applied on the very next tick —
  there is no separate "apply changes" step.

## Right panel — Leaf emulator (outputs)

- **Connections**: same adapter-selection pattern as the left panel, for the Leaf-facing bus.
- **Vehicle/battery generation selector**: AZE0/ZE1 car generation × battery generation/capacity
  (30/40/62kWh), mirroring `Core.set_vehicle()` from the Leaf project.
- **Live TX monitor**: grouped by CAN message, showing what's actually being transmitted right now
  (post-mapping, post-management-layer).

## Log panel (bottom, full width)

**Added 2026-07-31 per user request** — ported pattern from the Leaf project's own log (a
`queue.Queue` fed from any thread via a simple `log(msg)` call, drained on the Tk main thread with a
`HH:MM:SS` timestamp prefix, auto-scrolling). Connection events (connect/disconnect/reconnect
attempts), sequencer phase transitions (startup → running → winding_down → stopped), soft/hard-cut
assertions, output-clamp events, and profile save/load all log here — this is the primary place to
see *why* the bridge is behaving a certain way without digging through the live-value panels.

**Briefly narrowed, then reverted (2026-07-31, same day)**: Fault History initially lived beside the
Log in a horizontal `tk.PanedWindow` here, but the user asked for it to move into the Dashboard
window instead (see below) — the Log panel is back to its original full-width layout.

## Dashboard window (separate, large, resizable)

**Added 2026-07-31 per user request** — a `tk.Toplevel`, opened via the middle panel's "Dashboard"
button, large enough to show every Leaf output signal as a **bar gauge** (ported bar-gauge
mechanics from the Leaf project's own `DashWindow`: a `Canvas` + a rectangle scaled to the value's
fraction of its display range, greying out when stale). Per signal, laid out **side by side**:

```
[Signal] [Input bar + value] [conversion text] [Output bar + value] [Output Range] [Startup Default bar + value]
```

**"Startup Default" column, added 2026-07-31 (user request, third review pass)** — same bar-gauge
style and the same `(lo, hi)` display range as the Output column, so the two bars are directly
visually comparable, not just the numbers. Shows the static `leaf_signals.DEFAULTS` value for that
field — set once at window build time and never updated, since it's a fixed reference point, not
live data: exactly what that field would be sending if no RZ450e data (and no mapping override) had
ever arrived, per `06-realtime-engine-and-watchdog.md`'s "known-good startup memory." Lets the user
directly compare "what's being sent now" against "what it starts at with zero battery data" at a
glance.

**"Output Range" column, added 2026-07-31 (user request, fifth review pass)** — plain text (e.g.
`0 to 255.75`), the same documented `(lo, hi)` the Output and Startup Default bars are both scaled
against. Static, same reason as Startup Default. Placed directly after the Output column since it's
explaining that column specifically; the Startup Default comparison column comes after it.

**Header/column pixel alignment, fixed 2026-07-31 (same review pass)** — the header row previously
used `ttk.Label(width=N)` in *character* units, while the data rows mixed pixel-exact bar canvases
with character-width value labels; those two unit systems don't map to the same pixel offset, so
header text visibly drifted out of alignment with its actual column (user-reported). Fixed by
introducing `_fixed_cell()` — a `Frame` pinned to an exact **pixel** width (`pack_propagate(False)`)
with one label inside — used for *every* text cell in both the header and the data rows, built from
the same shared width constants (`SIGNAL_COL_W`, `VAL_COL_W`, `CONV_COL_W`, `RANGE_COL_W`,
`IN_COL_W`, `OUT_COL_W`). Alignment is now guaranteed by construction rather than by guessing font
metrics — confirmed directly by reading back live widget x-offsets: header cells land at pixel
0/244/462/576/824/948, and the Signal/Conversion/Output-Range data cells land at the identical
0/462/824.

**Text-clipping bug, found and fixed 2026-07-31 (same day, next review pass)** — `_fixed_cell()`'s
`pack_propagate(False)` locks BOTH width and height to whatever's given, and its height default was
`BAR_H` (14px — sized for the bar-gauge canvases only). A normal `ttk.Label` needs ~19px (regular)
to ~21px (bold) to render one line without clipping (confirmed via `winfo_reqheight()`), so *every*
text cell — Signal, Input/Output/Default values, Conversion, Output Range, and every bold header —
was rendering inside a container shorter than the text itself (user-reported: "everything on the
left side... is cut off"). Fixed with a new `CELL_H = 22` constant used as `_fixed_cell`'s height
default instead of `BAR_H`. Bar canvases (`_BarGauge`) never went through `_fixed_cell` and were
unaffected — still sized at `BAR_H` as intended.

**Precision, fixed 2026-07-31 (next review pass)** — the Input/Output/Startup-Default value labels
were formatted `:.2f` (2 decimals), which under-represents the RZ450e per-cell voltage signals'
actual resolution: `raw × 5/4096` steps by ~0.00122V, so 2 decimals can't distinguish adjacent raw
readings (e.g. 3.501V and 3.502V would both round to "3.50"). Changed to `:.3f` (3 decimals) to
match the precision already used by the main window's "Live decoded values" monitor
(`input_monitor_text()`, `gui/panels.py`) and by `management_engine.py`'s own status text (e.g.
`f'{worst_low:.3f}V'`) — the Dashboard was the one inconsistently-truncated spot. Confirmed the
70px value-cell width still comfortably fits the longer 3-decimal strings (measured up to 66px for
the widest case, `"3.700 (stale)"`) with no new clipping.

**Scaled ~15% smaller overall, and a Log panel added at the bottom (2026-07-31, eighth review
pass, user request: "make it 15% smaller in total... so it all fits on the screen without
maximizing")**:
- Every pixel dimension (bar widths, column widths, row/cell heights, the fault-light diameter,
  the right column width) scaled down ~15% from its previous value — see `gui/dashboard.py`'s
  module-level constants, each commented with its prior value so the scale factor stays traceable.
  Window itself: 1680×1060 → 1430×950.
- Most labels previously had **no explicit font at all** (inheriting the shared ttk theme's
  default, ~9pt) — there was no codified baseline to scale down, so this pass introduces one:
  `DASH_FONT` (8pt, was the implicit ~9pt default), `DASH_FONT_BOLD` (8pt bold, column headers),
  `DASH_ACCENT_FONT` (9pt bold, overrides just the size of the shared `Accent.TLabel` style for
  this window — the style's accent color is unaffected, and the global theme/main app window are
  untouched), `DASH_HEADER_FONT` (12pt bold, was `Header.TLabel`'s 14pt). Confirmed via
  `winfo_reqheight()` that `CELL_H` (22px → 19px) still comfortably fits the smaller font (17px
  actual) before finalizing, avoiding a repeat of the earlier text-clipping bug.
- **New Log panel** at the bottom of the window (`ttk.LabelFrame`, a read-only `ScrolledText`,
  packed `side='bottom'` so it claims a fixed strip and the rest of the window fills above it) —
  mirrors the main window's own log rather than duplicating the connection, so both windows show
  the exact same history. `App` (`gui/app.py`) now keeps a capped `collections.deque(maxlen=2000)`
  of every formatted log line alongside its existing `queue.Queue`-fed log box; the Dashboard reads
  from that deque (not the queue, which is drained once by the main window and would otherwise
  split messages between the two boxes) and syncs only new lines each tick, but does show full
  history immediately on open since the deque already holds everything logged so far. Confirmed
  directly: opening the Dashboard mid-session shows messages logged both before and after it was
  opened.

- If a mapping tie targets that signal: the input bar shows the live RZ450e source value (scaled to
  that signal's own display range), the conversion column shows the applied formula (e.g. `x-1.0`
  for the sign-inverted current tie), and the output bar shows the resulting transmitted value.
- If no tie targets that signal (opaque replay tables, or a value left at its default): the input
  side reads "(generated/default)" and only the output bar is shown — this is the "some data not
  driving anything, some data not driven but instead generated" case the user described.

**Two-column layout, added 2026-07-31 (second review pass)** — the window is split left/right by a
`ttk.Separator`:
- **Left** (scrollable, fills remaining width): the main per-signal bar-gauge list, grouped by CAN
  message.
- **Right** (fixed-width): Flags (soft/hard cut, permissions, as ON/OFF rows), Generated Signals
  (send/off status), **Charge emulation (ramp)** (added 2026-07-31, see below), and Battery
  Management status — moved here from the *bottom of the same long scrolling list* per user
  feedback, so they're visible at a glance without scrolling past 20+ signal rows, and clearly set
  apart from the main data by the divider.

**"Charge emulation (ramp)" section, added 2026-07-31** — user follow-up after the charger-request
ramp feature shipped ("we need some data on charging and ramp setting etc on the dashboard"). Shows,
live:
- **Emulation** ON/OFF (the Configurator checkbox's current state).
- **Target / rate** — the configured `charge_target_kw` / `chg_uprate_level`.
- **Leaf 0x1F2 request** — ACTIVE/idle, read directly from `RealtimeEngine.sequencer.charge_active()`
  via `self.master.engine` (`gui/app.py`'s `App.engine`) — this state lives inside the sequencer, not
  `SharedState`, and wasn't shown anywhere in the GUI before this.
- **RZ450e permission** — GRANTED/not granted, from `charge_permission_input` (`0x358`) — previously
  only visible buried in the left input list, not next to the thing it gates.
- **`charger_limit_kw`** — the live transmitted value (already visible as a bar in the main list
  above, repeated here so it sits next to the "why").
- **A plain-English status line** — "disabled" / "idle - no active, authorized charge request" /
  "STOPPED - Leaf wants to charge but RZ450e permission not granted (full_charge_flag set)" /
  "ramping/active - both triggers present" — the same four states `ChargeEmulationPanel`'s own
  status line reports, so either window tells a consistent story.

This keeps the whole picture (inputs, conversions, outputs, generated signals, and management
status) visible in one large window without needing horizontal scrolling. A **"Fault History"**
button next to the "?" help button opens that window (see below) — it briefly lived at the bottom
of the left list here, then moved out to its own window entirely once there wasn't room for it on
top of everything else.

## Fault History window (separate, tall and narrow)

**Moved to its own window 2026-07-31, ninth review pass** — `gui/fault_history_window.py`'s
`FaultHistoryWindow`, a `tk.Toplevel` opened via a **"Fault History"** button in either the main
window's Configurator toolbar or the Dashboard's header (both share one window instance — opening
from either focuses the same one rather than creating a second, matching how "Dashboard" itself
works). Has been through three locations: originally its own panel beside the Log in the main
window, then the Dashboard's right column ("we have all the real state there... no scrolling
required"), then the bottom of the Dashboard's left list ("align under the left data"), and finally
pulled out to its own window entirely once the user reported not having enough real estate left on
the Dashboard for it on top of everything else — **"make it tall and narrow."**

- **Geometry `420×900`, positioned just to the right of whichever window opened it** (computed from
  that window's live `winfo_x()`/`winfo_width()` at open time, not a hardcoded screen coordinate
  that might not suit every monitor layout).
- **Single-column, top to bottom** (not the Dashboard's old two-column grid — a narrow window
  doesn't have room for two side by side), inside a `VScrollFrame` so it scrolls if content ever
  exceeds the window instead of clipping.
- **Stacked two-line-per-entry layout**: a light + the fault's full description on the top line,
  the trigger count + a **Reset** button on the line below — gives the description room to wrap in
  a narrow window without cramming the count/button into a tight corner (the Dashboard's old
  version packed all of this onto one line, workable there since it had much more horizontal room).
- One row per entry in `bridge/fault_log.py`'s `FAULT_DEFINITIONS` (every soft/hard cut and monitor
  warning `management_engine.py` tracks — 12 total), plus a dynamically-added row for any
  output-clamp event (`06-realtime-engine-and-watchdog.md` section 4) that has actually occurred.
- **A small circular status "light"** on each row instead of relying on text color alone: **lit
  solid** in the fault's tier color (red = hard, orange = soft, yellow = warn) = active right now; a
  **hollow ring** (dark center, colored outline) = has happened before but is currently clear;
  **plain grey** = never happened this session. A **brief color-key legend** sits above the list
  with three example lights and short captions, so the meaning is visible without opening "?".
- **Reset** is an acknowledgment action only — it cannot force a still-true condition to false (live
  sensor data re-derives that every tick, unchanged); a genuinely still-active fault immediately
  starts counting again from 1 on the very next check, the same behavior a real BMS scan tool has
  when clearing a stored code for a fault that's still physically present. A top-of-window
  **"Reset all"** clears every entry at once.

Fault history itself is saved to its own `config/fault_log.json` (autosaved every 5s alongside the
last-known-good cache, loaded at startup and after a manual profile load), so it survives an app
restart, not just a bridge power-cycle within one running session — see
`06-realtime-engine-and-watchdog.md` section 5.

## Contextual "?" help, everywhere — not just the mapping tab

**Added 2026-07-31 (second review pass)** — the mapping tab's per-tie conversion-math popup was
only ever one piece of this; the reference Leaf app has a "?" next to nearly every
panel/group (`_help_btn`/`_show_help`), and that pattern was under-built here initially. Every
panel now has one, using a shared `help_btn()`/`show_info_popup()` helper
(`gui/info_popup.py`) that opens the same style of plain-text popup:
- Both **Connections panels** — what the combined RZ450e connection means, what Listen Only does.
- The **Vehicle panel** — how car/battery generation gates which Leaf CAN IDs transmit.
- The **Signal Mapping tab** (top-level) — combine functions, source-ID-prefixed dropdowns.
- **Every individual Battery Management feature** — what it does, what its thresholds mean, and
  its soft/hard cut tier, sourced directly from `05-battery-management-safety.md`.
- The **Generated Signals tab** — what these opaque signals are and why they're checkboxes.
- The **Charge Emulation tab** (added 2026-07-31) — the dual-trigger requirement, the ramp math,
  and the stop-flag behavior on a permission mismatch.
- The **Dashboard window** — how to read the bar gauges, the two-column layout, and (added
  2026-07-31) the Charge emulation section's live status line.

## Design principle carried through every panel

Per the user: *"I want to be able to see a visual indicator of what the [CAN data] represents
going in and what the [CAN data] represents going out... I want a graphical visual to confirm that
what I think is happening is actually what's happening between the input data and the output
data."* Every mapping tie gets a "?" popup with live numbers, the dashboard gives the same
input→conversion→output story as bar graphs at a glance for every signal at once, and every panel
now has its own contextual "?" explaining what it does and why.
