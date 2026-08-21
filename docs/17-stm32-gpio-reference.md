# STM32 Bridge Board — Spare GPIO Reference

Working reference doc, added 2026-08-20, updated 2026-08-21 (FIRMWARE_REVISION 3). Purpose: document
the 4 spare I/O pins on the CANBRIDGE-2port board (physical connector labels, current firmware config,
and how to add another one) so a future session doesn't have to re-derive this from scratch.

## Pin inventory (current, as of Rev 3)

`STM32_MiniLeaf_Bridge_Translator_uVision/Software/CANBRIDGE-2port/source/Src/gpio.c`'s `MX_GPIO_Init()`
configures all 4 spare pins. Only `Inp1` remains a plain unused input — the other 3 are now live:

| Firmware name | Port/Pin | Connector marking | Mode | Use |
|---|---|---|---|---|
| `Inp1` | GPIOC pin 12 | *(unmarked)* | Input, pull-up | unused — free for a future feature |
| `Inp2` | GPIOC pin 11 | `W222` | Input, pull-up | **fault-reset trigger** — ground it to clear latched faults + their escalation timers |
| `Inp3` | GPIOC pin 10 | `W116` | Output, push-pull | **fault-active light** — blinks at 0.5s while any fault latch is set, off otherwise |
| `Inp4` | GPIOA pin 15 | `BMW` | Output, push-pull | **awake light** — blinks at 0.1s while CAN data is arriving, steady on while idle, off while asleep |

The connector markings were read directly off the board by the user on 2026-08-20 — they don't mean
anything about the board's actual origin (it's the Nissan Leaf 2-port CAN bridge board); they're just
whatever silkscreen/label text happens to be printed next to each pin.

`main.c` reads all 4 once at boot into `static uint8_t config_Bits[2]`, which is never read again
anywhere in the firmware — dead/vestigial, inherited from the Mini-Cooper reference project this board's
firmware was originally based on. Harmless to leave in place; doesn't interact with anything below.

This is a real CubeMX project (`jcan.ioc`), but nothing in this port has ever regenerated code from the
`.ioc` — every hardware-init file has been hand-edited directly, never CubeMX-regenerated. See
`## Gotcha` below — the `.ioc` still says all-4-input and is now out of sync with `gpio.c`.

## Rev 1-3 feature: awake light, fault light, fault-reset button

Rev 1 implemented 2026-08-21 (input→output conversion pattern first proven 2026-08-20 on `Inp1`,
before this permanent feature moved it to `Inp3`/`Inp4`); Rev 2 the same day added blink behavior to
both lights on one shared timer; Rev 3 split them onto independent timers with different rates so
they're visually distinguishable. See the changelog comment at the top of `main.c` for the exact
per-revision diff. All 3 pieces live in `bridge_sequencer.c`'s `bridge_sequencer_tick()`, except the
sleep-off override:

- **Two independent blink timers** (top of `bridge_sequencer_tick()`): `fault_blink_next`/
  `fault_blink_on` (500ms half-period) for `Inp3`, `data_blink_next`/`data_blink_on` (100ms
  half-period) for `Inp4` - each a `HAL_GetTick()`-interval toggle (see "How to add another output"
  below for the general pattern). Deliberately different rates, not a shared timer (that was Rev 2),
  so the fault light and the CAN-activity light don't look identical at a glance.
- **`Inp3` fault light**: `(fault_active && fault_blink_on) ? SET : RESET` - blinks at 0.5s while any
  of `g_mgmt_state.hard_latched` / `.ac_charge_stop_latched` / `.ac_charge_temp_stop_latched` is set,
  fully off otherwise. Runs unconditionally every tick (moved up from after `compose_leaf_state()` in
  Rev 1) so the blink keeps going even if a fault stays latched into an idle/waiting-for-wake period.
- **`Inp4` awake light**: `(has_data ? data_blink_on : 1) ? SET : RESET`, where `has_data` is `(now -
  last_activity_tick) < 1000ms` - the same `last_activity_tick` the sleep timer uses, bumped on any
  CAN frame on either bus. Blinks at **0.1s** (faster than `Inp3`'s 0.5s, on purpose) while data is
  actively arriving, steady on while awake but idle. `main.c`'s Phase 5 sleep block still separately
  forces it fully off right before `HAL_PWR_EnterSLEEPMode()` and back on right after
  `HAL_ResumeTick()` - that override only matters for the instant of the WFI halt itself (nothing in
  `bridge_sequencer_tick()` runs while genuinely asleep to correct it), and gets superseded by the
  real blink/steady logic on the very next tick. Defaults to `SET` at boot (see `gpio.c`'s
  `HAL_GPIO_WritePin()` call ahead of `HAL_GPIO_Init()`, which avoids a boot-time glitch on push-pull
  outputs).
- **`Inp2` fault-reset input** (also top of `bridge_sequencer_tick()`, runs unconditionally every
  tick regardless of phase): edge-triggered — calls `management_engine_reset_all_conditions()` once
  on the high→low transition (pin grounded), not continuously while held. Edge-triggering matters
  because the underlying reset also re-arms the staleness watchdog's reference tick
  (`first_apply_tick`); calling it on every tick while the pin is held low would keep suppressing
  staleness detection for as long as someone holds the button down. Edge-triggering avoids that with
  one extra `static uint8_t` compared to a naive level check.

Measured cost via real UV4 rebuilds: Rev 2's blink behavior (both lights, shared timer) was **+60
bytes Code, +8 bytes RAM** over Rev 1's steady-on/off version; Rev 3's split to two independent
timers added another **+32 bytes Code, +8 bytes RAM** on top of that.

## `Inp2`'s reset depth: "Option A" (soft reset), chosen over "Option B" (hard reboot)

Two ways to implement the `Inp2` fault-reset were discussed and compared 2026-08-21, and **Option A is
what's implemented** — the user explicitly chose it ("i would rather 'softreset' like this") over Option
B after seeing the real measured cost of each:

- **Option A (implemented)**: `management_engine_reset_all_conditions()` (`management_engine.c`/`.h`) -
  calls `management_engine_notify_session_start()` (clears the 3 latches + re-arms
  `first_apply_tick`) AND additionally zeroes every escalation/pending timer that feeds those latches:
  `low_v_condition_pending`, `cell_cross_check_pending`, `temp_cross_check_pending`,
  `temp_probe_cross_check_pending`, `stale_pending`, `staleness_hard_cut`. This matters because those
  latches are recomputed fresh every tick from live conditions (`management_engine.c` around line 722:
  `if (hard_cut) { st->hard_latched = 1; }`) - clearing only the latch without also clearing its feeding
  timer means a still-actively-degrading condition can just re-latch on the very next tick. Option A
  forces the condition to re-accumulate through its **full timeout** before it can re-latch. Real
  measured cost (two back-to-back UV4 builds, only this call-site swapped): **+48 bytes Code, +0 bytes
  RAM** vs. calling `management_engine_notify_session_start()` alone.
- **Option B (not implemented, considered)**: swap the `Inp2` handler's call to `NVIC_SystemReset()`
  instead - a full MCU reboot. Would have been *cheaper* code (a one-line swap, no new function needed)
  and clears literally everything (BSS re-zeros on boot), but causes a real Leaf-bus TX gap for the
  reboot/re-init duration. Rejected in favor of the non-rebooting Option A.
- Worth knowing if `Inp1` is used for a reboot trigger later (see "Remaining future use" below) -
  that would be Option B's behavior, just on a different pin instead of replacing `Inp2`.

## How to add another output (e.g. using the last free pin, `Inp1`)

**1. In `gpio.c`, pull the pin out of its input group and give it its own output block** (mirrors what
Rev 1 already did for `Inp3`/`Inp4` — see the file directly for the exact current pattern):

```c
/* set the desired boot-time level BEFORE Init, to avoid a glitch */
HAL_GPIO_WritePin(Inp1_GPIO_Port, Inp1_Pin, GPIO_PIN_RESET);
GPIO_InitStruct.Pin = Inp1_Pin;
GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
GPIO_InitStruct.Pull = GPIO_NOPULL;
GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
HAL_GPIO_Init(GPIOC, &GPIO_InitStruct);
```

(Remove the pin from whichever `Inp*_Pin|Inp*_Pin` input group it's currently bundled into, same as
`Inp1`/`Inp2`'s remaining group in `gpio.c` today.)

**2. Drive the pin from application code** — `main.c`'s `while(1)` loop or `bridge_sequencer_tick()`,
whichever fires at the right time for what's being signaled:

```c
HAL_GPIO_WritePin(port, pin, GPIO_PIN_SET);    /* on */
HAL_GPIO_WritePin(port, pin, GPIO_PIN_RESET);  /* off */
HAL_GPIO_TogglePin(port, pin);                 /* flip */
```

For a periodic toggle, use a `HAL_GetTick()`-based interval check, never a blocking `HAL_Delay()` — the
main loop must keep servicing CAN RX/TX every iteration. `HAL_GetTick()` pauses during `HAL_SuspendTick()`
(main.c's sleep block) and resumes correctly on wake, so a tick-diff toggle needs no special handling
around sleep.

**3. Reading an input edge-triggered** (needed for a momentary-button-style trigger, not a level mirror):
see `Inp2`'s fault-reset code in `bridge_sequencer.c` above for the exact pattern — one `static uint8_t`
tracking the previous state, act only on the transition.

## Gotcha: don't reopen `jcan.ioc` in CubeMX

If `STM32_MiniLeaf_Bridge_Translator_uVision/Software/CANBRIDGE-2port/source/jcan.ioc` is ever reopened
in the actual CubeMX GUI and "Generate Code" is clicked, it will silently overwrite `gpio.c`'s hand-edited
pin config back to whatever's stored in the `.ioc` (still all-4-input, out of sync since Rev 1). Either
avoid CubeMX on this project entirely, or manually edit the `.ioc`'s matching `.Signal=` lines too (it's
a plain text file) to keep them in sync.

## Remaining future use (not yet implemented)

- **Reboot via `Inp1`**: call `NVIC_SystemReset()` when asserted — full MCU reset, more forceful than the
  `Inp2` fault-reset path (zeroes everything, not just the 3 latches).
- **Existing natural hook**: `bridge_sequencer.c`'s `manual_shutdown_requested` flag is checked in
  `bridge_sequencer_tick()` but never set anywhere — its own comment says "reserved for a future physical
  button." Wiring `Inp1` into this flag is the most natural fit for a future session if a manual
  wind-down/shutdown trigger (as opposed to reboot) is wanted instead.
