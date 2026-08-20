#include "rz450e_ingest.h"       // This module's own prototypes/RzState
#include "main.h"                 // HAL_GetTick() (via stm32f1xx_hal.h)
#include "bridge_config_gen.h"    // BRIDGE_CFG_ET_DID_TEMP_FRESH_WINDOW_S (Phase 6 temp DID/CAN blend)

// The one shared instance every other module reads (declared extern in the header).
RzState g_rz_state;

// ── RZ450e raw CAN IDs (docs/02-source-signals-rz450e.md) ──────────────────
// Named the same as bridge/rz450e_signals.py's module-level constants so the
// two files can be read side-by-side.
#define ID_PACK_V        0x020u
#define ID_CURRENT       0x023u
#define ID_TEMP_MINMAX   0x4A7u
#define ID_CELLS_A       0x4A9u
#define ID_CELLS_B       0x4C0u
#define ID_TEMPS         0x4AAu
#define ID_CHARGE_PERM   0x358u
#define ID_ALIVE_3F1     0x3F1u
#define ID_TICK_424      0x424u

// ── Toyota additive checksum (docs/02) ──────────────────────────────────────
// Confirmed 100%-match formula, last byte of the frame, but ONLY on these 5
// IDs - the mux'd cell/temp IDs and 0x4A7 do NOT carry this checksum (byte 7
// there is real payload, confirmed 0% match upstream if treated as one).
static uint8_t rz_frame_has_checksum(uint32_t arb_id)
{
    return (arb_id == ID_PACK_V || arb_id == ID_CURRENT || arb_id == ID_CHARGE_PERM
            || arb_id == ID_ALIVE_3F1 || arb_id == ID_TICK_424) ? 1u : 0u;
}

static uint8_t rz_toyota_sum_checksum(uint32_t arb_id, const uint8_t *data, uint8_t dlc)
{
    // sum of: high byte of the 11-bit ID, low byte of the ID, the DLC itself,
    // and every data byte EXCEPT the checksum byte (the last one) - all mod 256.
    uint32_t sum = (arb_id >> 8) + (arb_id & 0xFFu) + dlc;
    for (uint8_t i = 0; i + 1u < dlc; i++)  // every byte except the last (dlc-1 is the checksum itself)
    {
        sum += data[i];
    }
    return (uint8_t)(sum & 0xFFu);
}

// True if this ID doesn't carry a checksum (nothing to check), or if it does
// and the checksum in byte 7 matches. False means corruption - the caller
// must not decode this frame at all, same as bridge/rz450e_signals.py's
// frame_checksum_ok().
static uint8_t rz_checksum_ok(uint32_t arb_id, const uint8_t *data, uint8_t dlc)
{
    if (!rz_frame_has_checksum(arb_id))
    {
        return 1u;  // this ID never carries a checksum - nothing to reject on
    }
    if (dlc < 8u)
    {
        return 0u;  // too short to even hold a checksum byte - treat as corrupt
    }
    return (data[7] == rz_toyota_sum_checksum(arb_id, data, dlc)) ? 1u : 0u;
}

// ── Plausibility-checked signal write (docs/05, bridge/rz450e_signals.py's
// PLAUSIBLE_RANGES + validate_inputs()) ─────────────────────────────────────
// Deliberately generous bounds - much wider than any real safety threshold -
// meant only to reject obvious decode/bus garbage (a corrupted frame, a
// dropped byte), never a real if extreme physical reading. A rejected value
// is simply not written - the existing value keeps aging, caught later by
// the staleness watchdog (Phase 4) if the rejection is sustained, not by
// this function.
static void rz_update_bounded(RzSignal *sig, float value, float lo, float hi)
{
    if (value < lo || value > hi)
    {
        return;  // implausible - reject, leave the existing value (and its age) untouched
    }
    sig->value = value;
    sig->last_update_tick = HAL_GetTick();
}

// For fields with no registered plausibility range in the Python source
// (alive counters, flags, "which probe reported this" indices) - always
// accepted, same as validate_inputs()'s "no registered range -> pass
// through unchanged" behavior.
static void rz_update_unbounded(RzSignal *sig, float value)
{
    sig->value = value;
    sig->last_update_tick = HAL_GetTick();
}

// ── Per-ID decoders (bridge/rz450e_signals.py's decode_* functions) ─────────

// 0x020: pack voltage (whole V) + pack-level cell min/max (sanity check only).
static void rz_decode_020(const uint8_t *d, uint8_t dlc)
{
    if (dlc < 6u) return;  // matches Python's `if len(d) < 6: return {}`

    // pack_v: top 12 bits of d[0..1], raw value IS whole volts (no scale factor)
    float pack_v = (float)(((uint16_t)d[0] << 4) | (d[1] >> 4));
    rz_update_bounded(&g_rz_state.pack_v, pack_v, 0.0f, 500.0f);

    // cell_min: 12-bit big-endian value spanning the low nibble of d[1] + all of d[2],
    // scaled by 5.0/4096.0 (12-bit ADC over a 0-5V range)
    float cell_min = (float)(((uint16_t)(d[1] & 0x0Fu) << 8) | d[2]) * (5.0f / 4096.0f);
    rz_update_bounded(&g_rz_state.cell_min, cell_min, 0.50f, 5.00f);

    // cell_max: same 12-bit pattern as pack_v, but shifted to d[3]/d[4] and scaled like a cell voltage
    float cell_max = (float)(((uint16_t)d[3] << 4) | (d[4] >> 4)) * (5.0f / 4096.0f);
    rz_update_bounded(&g_rz_state.cell_max, cell_max, 0.50f, 5.00f);
}

// 0x023: pack current, two independent sensor taps.
static void rz_decode_023(const uint8_t *d, uint8_t dlc)
{
    if (dlc < 7u) return;

    // 12-bit signed value: raw 12-bit unsigned minus 0x800 (2048) recenters it
    // around zero, then *0.1 A/count.
    float current = ((float)(((uint16_t)d[0] << 4) | (d[1] >> 4)) - 2048.0f) * 0.1f;
    rz_update_bounded(&g_rz_state.current, current, -210.0f, 210.0f);

    float current_b = ((float)(((uint16_t)d[4] << 4) | (d[5] >> 4)) - 2048.0f) * 0.1f;
    rz_update_bounded(&g_rz_state.current_b, current_b, -210.0f, 210.0f);
}

// 0x4A7: pack-level temperature extremes + which probe reported each one.
static void rz_decode_4a7(const uint8_t *d, uint8_t dlc)
{
    if (dlc < 4u) return;

    rz_update_bounded(&g_rz_state.temp_max, (float)d[0], -51.1f, 121.1f);
    rz_update_bounded(&g_rz_state.temp_min, (float)d[1], -51.1f, 121.1f);
    // "which probe number" fields have no plausibility range in the Python
    // source (not a physical measurement) - always accepted.
    rz_update_unbounded(&g_rz_state.temp_max_probe, (float)d[2]);
    rz_update_unbounded(&g_rz_state.temp_min_probe, (float)d[3]);
}

// 0x358: charging interlock bit + its own 4-bit alive counter.
static void rz_decode_358(const uint8_t *d, uint8_t dlc)
{
    if (dlc < 2u) return;

    // A 1-bit flag can't be "out of range" by construction - no bounds check,
    // matches PLAUSIBLE_RANGES not registering this key at all.
    rz_update_unbounded(&g_rz_state.charge_permission_input, (float)(d[0] & 0x01u));
    rz_update_unbounded(&g_rz_state.alive_358, (float)d[1]);
}

// 0x3F1: 4-bit free-running alive counter (watchdog-usable, not physical data).
static void rz_decode_3f1(const uint8_t *d, uint8_t dlc)
{
    if (dlc < 1u) return;
    rz_update_unbounded(&g_rz_state.alive_3f1, (float)(d[0] & 0x0Fu));
}

// 0x424: +1 every 5.000s free-running counter (watchdog-usable).
static void rz_decode_424(const uint8_t *d, uint8_t dlc)
{
    if (dlc < 1u) return;
    rz_update_unbounded(&g_rz_state.counter_5s, (float)d[0]);
}

// 0x4A9 / 0x4C0: 4 individual cell voltages per frame, muxed by a running
// "base index" in d[0] (0, 4, 8, ... 92 - the battery round-robins through
// all 96 cells on its own schedule; this bridge just accepts whichever base
// arrives next, no receive-side sequencing required). Both CAN IDs share
// this exact decoder - the battery splits the 96-cell sweep across the two
// IDs, but the payload layout is identical either way.
static void rz_decode_cell_msg(const uint8_t *d, uint8_t dlc)
{
    if (dlc < 8u) return;

    uint8_t base = d[0];
    if (base > 0x5Cu || (base % 4u) != 0u)
    {
        return;  // not a valid mux base (must be 0,4,8,...,92) - drop the frame, matches Python
    }

    // Same 12-bit-over-a-nibble-boundary unpack repeated 4 times across d[1..6],
    // each value scaled the same way as every other cell voltage (5V / 4096 counts).
    uint16_t raw[4];
    raw[0] = ((uint16_t)d[1] << 4) | (d[2] >> 4);
    raw[1] = ((uint16_t)(d[2] & 0x0Fu) << 8) | d[3];
    raw[2] = ((uint16_t)d[4] << 4) | (d[5] >> 4);
    raw[3] = ((uint16_t)(d[5] & 0x0Fu) << 8) | d[6];

    for (uint8_t k = 0; k < 4u; k++)
    {
        uint8_t cell_number = (uint8_t)(base + k + 1u);  // 1-based cell number, matches Python
        if (cell_number <= 96u)
        {
            float voltage = (float)raw[k] * (5.0f / 4096.0f);
            rz_update_bounded(&g_rz_state.cell[cell_number - 1u], voltage, 0.50f, 5.00f);
        }
    }
}

// 0x4AA: 7 individual temp probes per frame, muxed by a running base in d[0]
// (0, 7, or 14 - 16 probes split 7+7+2 across three mux values).
static void rz_decode_temp_msg(const uint8_t *d, uint8_t dlc)
{
    if (dlc < 8u) return;

    uint8_t mux = d[0];
    if (mux != 0x00u && mux != 0x07u && mux != 0x0Eu)
    {
        return;  // not one of the 3 valid mux bases - drop the frame, matches Python
    }

    // DID 0x1814 freshness check (Phase 6, docs/02) - checked ONCE per frame,
    // via probe 1's DID age as a proxy for the whole last DID 0x1814 batch
    // (all 16 probes arrive together in one response, so one shared check is
    // correct here, not an approximation - matches bridge/realtime_engine.py's
    // _ingest_rz_bus ID_TEMPS branch exactly).
    uint8_t did_fresh = 0u;
    if (g_rz_state.temp_did[0].last_update_tick != 0u)
    {
        float did_age_s = (float)(HAL_GetTick() - g_rz_state.temp_did[0].last_update_tick) / 1000.0f;
        did_fresh = (did_age_s <= BRIDGE_CFG_ET_DID_TEMP_FRESH_WINDOW_S) ? 1u : 0u;
    }

    for (uint8_t j = 1; j <= 7u; j++)
    {
        uint8_t probe_number = (uint8_t)(mux + j);  // 1-based probe number
        if (probe_number <= 16u)
        {
            // Raw byte is already whole-degree °C, no scale/offset (this
            // project stores every temperature in °C - see rz450e_signals.py's
            // decode_temp_msg() comment on the 2026-08-09 °F->°C switch).
            // Always updates the CAN-backup copy; only promotes to the
            // effective/front-door temp[] array when DID isn't fresh enough -
            // see rz450e_ingest.h's own comment on the three parallel arrays.
            rz_update_bounded(&g_rz_state.temp_can[probe_number - 1u], (float)d[j], -51.1f, 121.1f);
            if (!did_fresh)
            {
                rz_update_bounded(&g_rz_state.temp[probe_number - 1u], (float)d[j], -51.1f, 121.1f);
            }
        }
    }
}

// ── Dispatch ─────────────────────────────────────────────────────────────

void rz450e_ingest_init(void)
{
    // Every field starts at last_update_tick == 0 ("never seen this session")
    // via static zero-initialization of g_rz_state - nothing else to do here.
    // Explicit call kept (rather than relying on zero-init alone) so a future
    // phase has an obvious place to add real startup work.
}

void rz450e_ingest_frame(CAN_FRAME *frame)
{
    if (!rz_checksum_ok(frame->ID, frame->data, frame->dlc))
    {
        g_rz_state.checksum_failures++;
        return;  // corrupted frame - never decoded, matches bridge/rz450e_signals.py
    }

    switch (frame->ID)
    {
        case ID_PACK_V:      rz_decode_020(frame->data, frame->dlc);       break;
        case ID_CURRENT:     rz_decode_023(frame->data, frame->dlc);       break;
        case ID_TEMP_MINMAX: rz_decode_4a7(frame->data, frame->dlc);       break;
        case ID_CELLS_A:     rz_decode_cell_msg(frame->data, frame->dlc);  break;
        case ID_CELLS_B:     rz_decode_cell_msg(frame->data, frame->dlc);  break;
        case ID_TEMPS:       rz_decode_temp_msg(frame->data, frame->dlc);  break;
        case ID_CHARGE_PERM: rz_decode_358(frame->data, frame->dlc);       break;
        case ID_ALIVE_3F1:   rz_decode_3f1(frame->data, frame->dlc);       break;
        case ID_TICK_424:    rz_decode_424(frame->data, frame->dlc);       break;
        default:
            break;  // not a recognized/confirmed RZ450e broadcast ID - ignored, matches decode_frame()
    }
}
