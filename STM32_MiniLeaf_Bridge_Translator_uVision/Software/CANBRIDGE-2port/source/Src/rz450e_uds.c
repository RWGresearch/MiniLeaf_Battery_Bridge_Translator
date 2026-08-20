#include "rz450e_uds.h"           // This module's own prototypes
#include "bridge_config_gen.h"    // BRIDGE_CFG_ET_DID_* (generated, see tools/export_stm32_config.py)
#include "rz450e_ingest.h"         // g_rz_state - every DID response gets written here
#include "main.h"                  // HAL_GetTick()

// ── Toyota UDS addressing (docs/02) ─────────────────────────────────────────
#define TOYOTA_REQ_ID  0x747u
#define TOYOTA_RESP_ID 0x74Fu

// ── DIDs (docs/02) - (hi, lo) byte pairs, matches the real UDS request layout ─
#define DID_SOC_HI          0x1Fu
#define DID_SOC_LO          0x5Bu
#define DID_CAPACITY_HI     0x1Du
#define DID_CAPACITY_LO     0x3Eu
#define DID_PRIMARY_VI_HI   0x1Fu
#define DID_PRIMARY_VI_LO   0x9Au
#define DID_TEMP_PROBES_HI  0x18u
#define DID_TEMP_PROBES_LO  0x14u

// 3-item round robin: SoC -> capacity -> primary V/I -> repeat (docs/02's
// own measured ~9s/poll for this cycle). Temp probes (0x1814) are
// deliberately NOT a 4th slot here - see this file's header comment.
static const uint8_t RR_DID_HI[3] = { DID_SOC_HI, DID_CAPACITY_HI, DID_PRIMARY_VI_HI };
static const uint8_t RR_DID_LO[3] = { DID_SOC_LO, DID_CAPACITY_LO, DID_PRIMARY_VI_LO };

// ── Request/response state machine ──────────────────────────────────────────
// One shared machine, not two independent ones - only one UDS transaction is
// ever in flight at a time (matches both the real hardware's single-
// responder limitation and Python's own single-threaded _did_poll_loop).
typedef enum {
    UDS_STATE_IDLE,             // ready to send the next round-robin request
    UDS_STATE_AWAITING_RR,      // round-robin request sent, waiting for response/timeout
    UDS_STATE_GAP_AFTER_RR,     // inter-request pacing delay after a round-robin attempt
    UDS_STATE_AWAITING_TEMP,    // temp-probe request sent, waiting for response/timeout
    UDS_STATE_GAP_AFTER_TEMP,   // inter-request pacing delay after a temp attempt
} UdsState;

static UdsState uds_state = UDS_STATE_IDLE;
static uint32_t request_sent_tick = 0;
static uint32_t gap_until_tick = 0;
static uint8_t rr_index = 0;              // 0=SoC, 1=capacity, 2=primary V/I
static uint32_t next_temp_due_tick = 0;

// ISO-TP (ISO 15765-2) response reassembly - one buffer, since only one
// transaction is ever in flight. Largest real response is DID 0x1814 (16
// probes x 2 bytes + 3-byte UDS header = 35 bytes); sized generously above
// that as a defensive margin against a malformed/corrupt length field.
#define RESP_BUF_SIZE 40u
static uint8_t resp_buf[RESP_BUF_SIZE];
static uint16_t resp_expected_len = 0;
static uint16_t resp_received_len = 0;
static uint8_t resp_in_progress = 0;   // mid multi-frame reassembly (First Frame seen, awaiting Consecutive Frames)
static uint8_t resp_complete = 0;      // a full response is ready for the current transaction

static uint16_t u16_be(const uint8_t *d, uint16_t n) { return (uint16_t)(((uint16_t)d[n] << 8) | d[n + 1]); }

static int16_t s16_be(const uint8_t *d, uint16_t n)
{
    uint16_t v = u16_be(d, n);
    return (v >= 32768u) ? (int16_t)(v - 65536) : (int16_t)v;
}

static void send_uds_request(uint8_t did_hi, uint8_t did_lo)
{
    CAN_FRAME frame;
    frame.ID = TOYOTA_REQ_ID;
    frame.dlc = 8u;
    frame.ide = 0u;
    frame.rtr = 0u;
    // "03 22 <DID hi> <DID lo> 00 00 00 00" - single-frame ISO-TP, 3 UDS
    // payload bytes (service 0x22 ReadDataByIdentifier + the 2-byte DID).
    frame.data[0] = 0x03u; frame.data[1] = 0x22u; frame.data[2] = did_hi; frame.data[3] = did_lo;
    frame.data[4] = 0x00u; frame.data[5] = 0x00u; frame.data[6] = 0x00u; frame.data[7] = 0x00u;
    PushCan(MYCAN1, CAN_TX, &frame);

    resp_expected_len = 0u;
    resp_received_len = 0u;
    resp_in_progress = 0u;
    resp_complete = 0u;
}

// ISO-TP Flow Control frame (continue, block size 0 = unlimited, separation
// time 0x0A = 10ms) - sent back to the tester ID the instant a First Frame
// arrives, same fixed bytes bridge/rz450e_signals.py's DidClient.request() sends.
static void send_flow_control(void)
{
    CAN_FRAME frame;
    frame.ID = TOYOTA_REQ_ID;
    frame.dlc = 8u;
    frame.ide = 0u;
    frame.rtr = 0u;
    frame.data[0] = 0x30u; frame.data[1] = 0x00u; frame.data[2] = 0x0Au;
    frame.data[3] = 0x00u; frame.data[4] = 0x00u; frame.data[5] = 0x00u;
    frame.data[6] = 0x00u; frame.data[7] = 0x00u;
    PushCan(MYCAN1, CAN_TX, &frame);
}

void rz450e_uds_on_frame(CAN_FRAME *frame)
{
    if (frame->ID != TOYOTA_RESP_ID) { return; }
    if (uds_state != UDS_STATE_AWAITING_RR && uds_state != UDS_STATE_AWAITING_TEMP) { return; }
    if (frame->dlc < 2u) { return; }  // too short to even hold a PCI+length - ignore, defensive

    uint8_t pci = (uint8_t)((frame->data[0] >> 4) & 0x0Fu);

    if (pci == 0x0u)
    {
        // Single frame: low nibble of byte 0 is the payload length (0-7).
        uint8_t length = (uint8_t)(frame->data[0] & 0x0Fu);
        if (length > 7u) { length = 7u; }   // defensive clamp - malformed frame can't misparse into adjacent memory
        for (uint8_t i = 0; i < length; i++) { resp_buf[i] = frame->data[1u + i]; }
        resp_received_len = length;
        resp_expected_len = length;
        resp_complete = 1u;
    }
    else if (pci == 0x1u)
    {
        // First Frame: 12-bit total length across both nibbles of bytes 0-1,
        // first 6 payload bytes in data[2..7]. Must send Flow Control back
        // immediately or the responder stalls waiting for it.
        uint16_t expected = (uint16_t)(((uint16_t)(frame->data[0] & 0x0Fu) << 8) | frame->data[1]);
        if (expected > RESP_BUF_SIZE) { expected = RESP_BUF_SIZE; }  // defensive clamp
        resp_expected_len = expected;
        for (uint8_t i = 0; i < 6u; i++) { resp_buf[i] = frame->data[2u + i]; }
        resp_received_len = 6u;
        resp_in_progress = 1u;
        send_flow_control();
    }
    else if (pci == 0x2u && resp_in_progress)
    {
        // Consecutive Frame: up to 7 more payload bytes in data[1..7]. The
        // LAST real CF frame is still padded to 8 bytes total on the wire,
        // so its trailing byte(s) beyond the true payload length are
        // padding, not data - stop at resp_expected_len exactly (matches
        // bridge/rz450e_signals.py's DidClient.request() truncating via
        // `payload[:expected_len]` on return; found via
        // tests/check_stm32_rz450e_uds.py fuzzing - an earlier version of
        // this loop copied all 7 bytes unconditionally, over-reading
        // padding into the reported length).
        for (uint8_t i = 0; i < 7u; i++)
        {
            if (resp_received_len < RESP_BUF_SIZE && resp_received_len < resp_expected_len)
            {
                resp_buf[resp_received_len] = frame->data[1u + i];
                resp_received_len++;
            }
        }
        if (resp_received_len >= resp_expected_len)
        {
            resp_complete = 1u;
            resp_in_progress = 0u;
        }
    }
}

// ── Response decoders (bridge/rz450e_signals.py's decode_soc/decode_capacity/
// decode_primary_v_i/decode_temp_probes_did) - same plausibility bounds as
// rz450e_ingest.c's broadcast decoders (rz450e_signals.PLAUSIBLE_RANGES). ──

static void decode_and_ingest_rr(uint8_t index, const uint8_t *d, uint16_t len)
{
    uint32_t now = HAL_GetTick();

    if (index == 0u)   // DID 0x1F5B - SoC
    {
        if (len < 4u) { return; }
        float soc = (float)d[3] * 100.0f / 255.0f;
        if (soc >= 0.0f && soc <= 100.0f)
        {
            g_rz_state.soc_pct.value = soc;
            g_rz_state.soc_pct.last_update_tick = now;
        }
    }
    else if (index == 1u)   // DID 0x1D3E - capacity/SOH, 4 sub-packs
    {
        if (len < 11u) { return; }
        float c1 = (float)u16_be(d, 3) / 100.0f;
        float c2 = (float)u16_be(d, 5) / 100.0f;
        float c3 = (float)u16_be(d, 7) / 100.0f;
        float c4 = (float)u16_be(d, 9) / 100.0f;
        if (c1 >= 0.0f && c1 <= 300.0f) { g_rz_state.capacity_pack1_ah.value = c1; g_rz_state.capacity_pack1_ah.last_update_tick = now; }
        if (c2 >= 0.0f && c2 <= 300.0f) { g_rz_state.capacity_pack2_ah.value = c2; g_rz_state.capacity_pack2_ah.last_update_tick = now; }
        if (c3 >= 0.0f && c3 <= 300.0f) { g_rz_state.capacity_pack3_ah.value = c3; g_rz_state.capacity_pack3_ah.last_update_tick = now; }
        if (c4 >= 0.0f && c4 <= 300.0f) { g_rz_state.capacity_pack4_ah.value = c4; g_rz_state.capacity_pack4_ah.last_update_tick = now; }
    }
    else   // DID 0x1F9A - primary V/I cross-check reference
    {
        if (len < 9u) { return; }
        float v = (float)u16_be(d, 5) / 64.0f;
        float i = (float)s16_be(d, 7) * 0.1f;
        if (v >= 0.0f && v <= 500.0f) { g_rz_state.primary_pack_v.value = v; g_rz_state.primary_pack_v.last_update_tick = now; }
        if (i >= -700.0f && i <= 700.0f) { g_rz_state.primary_current_a.value = i; g_rz_state.primary_current_a.last_update_tick = now; }
    }
}

static void decode_and_ingest_temp(const uint8_t *d, uint16_t len)
{
    if (len < 35u) { return; }
    uint32_t now = HAL_GetTick();
    for (uint8_t n = 1; n <= 16u; n++)
    {
        float value = (float)u16_be(d, (uint16_t)(3 + (n - 1) * 2)) / 256.0f - 50.0f;
        if (value >= -51.1f && value <= 121.1f)
        {
            // DID always wins the instant a response arrives (docs/02) -
            // write both the DID-specific copy AND the effective/front-door
            // array directly, unconditionally overriding whatever the CAN
            // backup last put there. rz450e_ingest.c's own CAN decode checks
            // temp_did[0]'s freshness before it's next allowed to overwrite
            // this same front-door array - see that file's rz_decode_temp_msg().
            g_rz_state.temp_did[n - 1u].value = value;
            g_rz_state.temp_did[n - 1u].last_update_tick = now;
            g_rz_state.temp[n - 1u].value = value;
            g_rz_state.temp[n - 1u].last_update_tick = now;
        }
    }
}

void rz450e_uds_init(void)
{
    uds_state = UDS_STATE_IDLE;
    rr_index = 0u;
    resp_complete = 0u;
    resp_in_progress = 0u;
    next_temp_due_tick = HAL_GetTick();   // due immediately - matches Python's `next_temp_due = time.monotonic()`
}

void rz450e_uds_tick(void)
{
    uint32_t now = HAL_GetTick();
    uint32_t response_timeout_ms = (uint32_t)(BRIDGE_CFG_ET_DID_RESPONSE_TIMEOUT_S * 1000.0f);
    uint32_t inter_request_gap_ms = (uint32_t)(BRIDGE_CFG_ET_DID_INTER_REQUEST_GAP_S * 1000.0f);

    switch (uds_state)
    {
        case UDS_STATE_IDLE:
        {
            send_uds_request(RR_DID_HI[rr_index], RR_DID_LO[rr_index]);
            request_sent_tick = now;
            uds_state = UDS_STATE_AWAITING_RR;
            break;
        }

        case UDS_STATE_AWAITING_RR:
        {
            if (resp_complete)
            {
                decode_and_ingest_rr(rr_index, resp_buf, resp_received_len);
                gap_until_tick = now + inter_request_gap_ms;
                uds_state = UDS_STATE_GAP_AFTER_RR;
            }
            else if ((now - request_sent_tick) >= response_timeout_ms)
            {
                // Timed out - give up on this DID for this cycle (matches
                // Python's request() returning None; the next round-robin
                // pass will try it again).
                gap_until_tick = now + inter_request_gap_ms;
                uds_state = UDS_STATE_GAP_AFTER_RR;
            }
            break;
        }

        case UDS_STATE_GAP_AFTER_RR:
        {
            if ((int32_t)(now - gap_until_tick) >= 0)
            {
                rr_index = (uint8_t)((rr_index + 1u) % 3u);
                if ((int32_t)(now - next_temp_due_tick) >= 0)
                {
                    // Rescheduled BEFORE the request, not after it completes
                    // - matches Python's own fault-containment ordering
                    // (bridge/realtime_engine.py's _did_poll_loop comment) so
                    // a slow/failed response doesn't compound delay onto the
                    // next cycle.
                    next_temp_due_tick = now + (uint32_t)(BRIDGE_CFG_ET_DID_TEMP_POLL_INTERVAL_S * 1000.0f);
                    send_uds_request(DID_TEMP_PROBES_HI, DID_TEMP_PROBES_LO);
                    request_sent_tick = now;
                    uds_state = UDS_STATE_AWAITING_TEMP;
                }
                else
                {
                    uds_state = UDS_STATE_IDLE;
                }
            }
            break;
        }

        case UDS_STATE_AWAITING_TEMP:
        {
            if (resp_complete)
            {
                decode_and_ingest_temp(resp_buf, resp_received_len);
                gap_until_tick = now + inter_request_gap_ms;
                uds_state = UDS_STATE_GAP_AFTER_TEMP;
            }
            else if ((now - request_sent_tick) >= response_timeout_ms)
            {
                gap_until_tick = now + inter_request_gap_ms;
                uds_state = UDS_STATE_GAP_AFTER_TEMP;
            }
            break;
        }

        case UDS_STATE_GAP_AFTER_TEMP:
        {
            if ((int32_t)(now - gap_until_tick) >= 0)
            {
                uds_state = UDS_STATE_IDLE;
            }
            break;
        }

        default:
            uds_state = UDS_STATE_IDLE;
            break;
    }
}
