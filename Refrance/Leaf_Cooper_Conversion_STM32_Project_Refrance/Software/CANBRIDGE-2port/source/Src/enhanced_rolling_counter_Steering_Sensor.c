/*
 * This converts the 2013 mini cooper SZL angle sensor and rate of change to the needed
 * 2025 Nissan leaf angle data so that the Mini cooper angle sensor can be use on the Leaf Steering Columb.
 * RWG 9-25-2025.
 * SIMPLIFIED Rolling Counter Implementation for CAN Messages ID 0x002
 * REVISION: 0008 - Correct FOLDED XOR Checksum + Rolling Counter Implementation
 *
 * FINAL DISCOVERY:
 * Byte 4 structure is simple and elegant:
 *   - High nibble (bits 7-4): Folded XOR checksum
 *   - Low nibble (bits 3-0): Simple rolling counter (0-F)
 *
 * Checksum algorithm (FOLDED XOR):
 *   1. XOR all data bytes and counter: xor_result = byte0 ^ byte1 ^ byte2 ^ byte3 ^ counter
 *   2. Fold by XORing nibbles: checksum = (xor_result & 0x0F) ^ ((xor_result >> 4) & 0x0F)
 *   3. Combine: byte4 = (checksum << 4) | counter
 *
 * This completely replaces the overly complex reference pattern approach.
 *
 * REVISION HISTORY:
 * 0008 (Oct 26, 2025) - Discovered folded XOR checksum, validated with moving sensor data
 * 0007 (Oct 26, 2025) - Simplified to basic XOR checksum, validated with static sensor data
 * 0006 (Sep 25, 2025) - Complex reference pattern approach (incorrect)
 *
 * - Tuned scaling parameters: Rate=2.9x, Angle=0.45x (from visual analysis)
 */

#include <stdint.h>
#include <stdbool.h>
#include "can.h"  // For CAN_FRAME type definition

// FINAL TUNED scaling constants for 0x0C4 to 0x10A transformation
// Values determined through visual analysis and parameter tuning
#define SOURCE_0C4_RANGE_RAW        24000    // ±1200° in decidegrees (full range)
#define TARGET_0X10A_RANGE_RAW      12000    // ±600° in decidegrees (full range)
#define SCALE_FACTOR_FIXED          0.45f    // TUNED: 0.45x for optimal visual match
#define RATE_SCALE_FACTOR           2.9f     // TUNED: 2.9x for byte 4 rate scaling

// No reference pattern needed - checksum is calculated from data!

uint8_t calculate_steering_rate(uint16_t prev_angle, uint16_t curr_angle)
{
    // Calculate absolute angle change
    int32_t angle_change = (int32_t)curr_angle - (int32_t)prev_angle;
    uint16_t abs_change = (angle_change < 0) ? -angle_change : angle_change;

    // Apply threshold-based rate calculation based on real data analysis
    // Refined thresholds based on actual steering sensor data patterns
    if (abs_change == 0) {
        return 0;           // No change at all
    } else if (abs_change < 300) {
        return 1;           // Small to medium change (covers 252-262 range seen in data)
    } else if (abs_change < 500) {
        return 2;           // Medium change
    } else if (abs_change < 700) {
        return 3;           // Large change
    } else if (abs_change < 900) {
        return 4;           // Very large change
    } else {
        return 5;           // Maximum rate for huge changes
    }
}

/**
 * Calculate folded XOR checksum for the data bytes and rolling counter
 *
 * The checksum is calculated by XORing all data bytes with the counter,
 * then "folding" by XORing the low and high nibbles together.
 * This creates a 4-bit checksum that fits in the high nibble of byte 4.
 *
 * @param byte0 First data byte
 * @param byte1 Second data byte
 * @param byte2 Third data byte
 * @param byte3 Fourth data byte
 * @param counter Rolling counter value (0-F)
 * @return 4-bit checksum value
 */
static uint8_t calculate_xor_checksum(uint8_t byte0, uint8_t byte1, uint8_t byte2, uint8_t byte3, uint8_t counter)
{
    // XOR all bytes and counter together
    uint8_t xor_result = byte0 ^ byte1 ^ byte2 ^ byte3 ^ counter;

    // Fold: XOR the low and high nibbles together to create 4-bit checksum
    uint8_t checksum = (xor_result & 0x0F) ^ ((xor_result >> 4) & 0x0F);

    return checksum;
}

/**
 * Calculate complete byte 4 with checksum and rolling counter
 *
 * @param data Pointer to first 4 bytes of CAN data payload
 * @param sequence_number Sequential message number (0, 1, 2, ...)
 * @return Complete byte 4 value (checksum in high nibble, counter in low nibble)
 */
uint8_t calculate_enhanced_rolling_counter(const uint8_t *data, uint16_t sequence_number)
{
    // Simple rolling counter: just use low 4 bits of sequence number (0-F with wrap)
    uint8_t counter = sequence_number & 0x0F;

    // Calculate XOR checksum
    uint8_t checksum = calculate_xor_checksum(data[0], data[1], data[2], data[3], counter);

    // Combine: checksum in high nibble, counter in low nibble
    return (checksum << 4) | counter;
}

/**
 * Main rolling counter calculation function
 *
 * @param data Pointer to 4-byte CAN data payload
 * @param sequence_number Sequential message number (0, 1, 2, ...)
 * @return 8-bit rolling counter value
 */
uint8_t calculate_rolling_counter(const uint8_t *data, uint16_t sequence_number)
{
    // Try enhanced algorithm first (handles steering angle data)
    return calculate_enhanced_rolling_counter(data, sequence_number);
}

/**
 * Validation function for checksum algorithm
 * Tests against real sensor data from trace file
 *
 * @return Number of failed tests (0 = all pass)
 */
uint8_t validate_enhanced_algorithm(void)
{
    uint8_t errors = 0;

    // Test data from actual sensor at zero position (2025-10-26_09-41-23_711_fake_sensor_at_zero.trc)
    // Format: {byte0, byte1, byte2, byte3, expected_byte4_with_checksum}
    uint8_t test_cases[][5] = {
        {0x00, 0x00, 0x00, 0x07, 0xCB},  // Counter B, Checksum C
        {0x00, 0x00, 0x00, 0x07, 0xBC},  // Counter C, Checksum B
        {0x00, 0x00, 0x00, 0x07, 0xAD},  // Counter D, Checksum A
        {0x00, 0x00, 0x00, 0x07, 0x9E},  // Counter E, Checksum 9
        {0x00, 0x00, 0x00, 0x07, 0x8F},  // Counter F, Checksum 8
        {0x00, 0x00, 0x00, 0x07, 0x70},  // Counter 0, Checksum 7
        {0x00, 0x00, 0x00, 0x07, 0x61},  // Counter 1, Checksum 6
        {0x00, 0x00, 0x00, 0x07, 0x52},  // Counter 2, Checksum 5
        {0x00, 0x00, 0x00, 0x07, 0x43},  // Counter 3, Checksum 4
        {0x00, 0x00, 0x00, 0x07, 0x34},  // Counter 4, Checksum 3
        {0x00, 0x00, 0x00, 0x07, 0x25},  // Counter 5, Checksum 2
        {0x00, 0x00, 0x00, 0x07, 0x16},  // Counter 6, Checksum 1
        {0x00, 0x00, 0x00, 0x07, 0x07},  // Counter 7, Checksum 0
        {0x00, 0x00, 0x00, 0x07, 0xF8},  // Counter 8, Checksum F
        {0x00, 0x00, 0x00, 0x07, 0xE9},  // Counter 9, Checksum E
        {0x00, 0x00, 0x00, 0x07, 0xDA},  // Counter A, Checksum D
    };

    for (int i = 0; i < 16; i++) {
        // Extract expected counter from the test case
        uint8_t expected_counter = test_cases[i][4] & 0x0F;

        // Calculate the sequence number that would produce this counter
        uint16_t sequence_num = (expected_counter + 16 - 0xB) & 0x0F;
        if (i < 16) {
            sequence_num = 0 + i;  // 0, 1, 2 ,3 ,4 ,5, 6, 7, 8, 9, A, B, C, D, E, F
        }

        uint8_t result = calculate_rolling_counter(test_cases[i], sequence_num);
        if (result != test_cases[i][4]) {
            errors++;
        }
    }

    return errors;
}

/**
 * State management structure for 0x0C4 to 0x10A processing
 */
typedef struct {
    uint16_t prev_scaled_angle;    // Previous scaled angle for rate calculation
    uint16_t sequence_number;      // Current sequence number for rolling counter
    bool has_previous_frame;       // Flag to indicate if we have a previous frame
} steering_processor_state_t;

/**
 * Convert 0x0C4 rate to 0x002/0x10A rate format
 * Maps 16-bit 0x0C4 rate values to 8-bit range matching real 002 data
 * Based on trace analysis: 002 range 0-198, 0C4 effective range 0-65280
 *
 * @param raw_0C4_rate Raw rate value from 0x0C4 message (BYTE 4 ONLY!)
 * @return Converted rate for 0x002/0x10A message (0-255)
 */
uint8_t convert_0C4_rate_to_002_format(uint16_t raw_0C4_rate)
{
    // Handle static center position (no movement)
    if (raw_0C4_rate == 0) {  // Byte 4 = 0 means no rate
        return 0;
    }

    // Since raw_0C4_rate is now just byte 4 (0-255), handle signed conversion
    uint8_t byte4_rate = (uint8_t)raw_0C4_rate;
    uint16_t abs_rate;

    // Convert to signed 8-bit if needed (for negative rates)
    if (byte4_rate > 127) {
        int8_t signed_rate = (int8_t)(byte4_rate - 256);  // Convert to signed (-128 to +127)
        abs_rate = (signed_rate < 0) ? -signed_rate : signed_rate;
    } else {
        abs_rate = byte4_rate;
    }

    // Apply TUNED scaling factor (2.9x) determined through visual analysis
    // Use integer arithmetic: 2.9 ≈ 190/65 for good precision
    uint32_t scaled = (abs_rate * 190) / 65;  // 190/65 ≈ 2.923

    // Clamp to prevent overflow (002 max observed ~198, but allow some headroom)
    if (scaled > 255) {
        scaled = 255;
    }

    return (uint8_t)scaled;
}

/**
 * Scale 0x0C4 raw angle to 0x10A target range with inversion and clamping
 * Converts ±1200° range to ±600° range (0.5x scale factor) with inversion as the leaf and mini are opiset directions
 *
 * @param raw_0C4_angle Raw angle value from 0x0C4 message (bytes 0-1)
 * @return Scaled and inverted raw angle for 0x10A message
 */
uint16_t scale_0C4_to_10a_fixed(uint16_t raw_0C4_angle)
{
    // Convert to signed 32-bit for calculation to handle overflow
    int32_t signed_0C4 = (raw_0C4_angle > 32767) ?
                         (int32_t)raw_0C4_angle - 65536 :
                         (int32_t)raw_0C4_angle;

    // Apply TUNED scaling with inversion: new_value = -(old_value * scale_factor)
    // SCALE_FACTOR_FIXED = 0.45f determined through visual parameter tuning
    int32_t scaled_signed = -(int32_t)(signed_0C4 * SCALE_FACTOR_FIXED);

    // Clamp to prevent overflow beyond reasonable sensor limits
    // Allow some range beyond mechanical limits as specified
    const int32_t MAX_SENSOR_RANGE = 7200;  // ±720° in decidegrees
    if (scaled_signed > MAX_SENSOR_RANGE) {
        scaled_signed = MAX_SENSOR_RANGE;
    } else if (scaled_signed < -MAX_SENSOR_RANGE) {
        scaled_signed = -MAX_SENSOR_RANGE;
    }

    // Convert back to unsigned 16-bit
    uint16_t scaled_raw = (scaled_signed < 0) ?
                         (uint16_t)(scaled_signed + 65536) :
                         (uint16_t)scaled_signed;

    return scaled_raw;
}

/**
 * Extract angle and rate from 0x0C4 CAN frame
 *
 * @param frame_0C4 Pointer to 0x0C4 CAN frame
 * @param angle_out Pointer to store extracted angle (can be NULL)
 * @param rate_out Pointer to store extracted rate (can be NULL)
 * @return true if extraction successful, false on error
 */
bool extract_angle_and_rate_from_0C4(const CAN_FRAME *frame_0C4, uint16_t *angle_out, uint16_t *rate_out)
{
    if (frame_0C4 == NULL || frame_0C4->dlc < 5) {  // Need at least 5 bytes for byte 4
        return false;
    }

    // Extract 16-bit little endian angle from bytes 0-1
    if (angle_out != NULL) {
        *angle_out = (frame_0C4->data[1] << 8) | frame_0C4->data[0];
    }

    // Extract rate from BYTE 4 ONLY (BREAKTHROUGH DISCOVERY!)
    // Byte 2 = constant 252, Byte 3 = variable but noisy, Byte 4 = smooth rate data
    if (rate_out != NULL) {
        *rate_out = frame_0C4->data[4];  // 8-bit rate data, perfect for conversion
    }

    return true;
}

/**
 * Legacy function for backward compatibility
 */
uint16_t extract_angle_from_0C4(const CAN_FRAME *frame_0C4)
{
    uint16_t angle = 0;
    extract_angle_and_rate_from_0C4(frame_0C4, &angle, NULL);
    return angle;
}

/**
 * Process 0x0C4 CAN frame and generate complete 0x10A CAN frame
 *
 * @param state Processor state (maintains sequence and previous angle)
 * @param frame_0C4 Input 0x0C4 CAN frame
 * @param output_frame_10a Output 0x10A CAN frame (will be populated)
 * @return true if successful, false on error
 */
bool process_0C4_to_10a_frame(steering_processor_state_t *state,
                              const CAN_FRAME *frame_0C4,
                              CAN_FRAME *output_frame_10a)
{
    if (state == NULL || frame_0C4 == NULL || output_frame_10a == NULL) {
        return false;
    }

    // Validate input frame
    if (frame_0C4->ID != 0x0C4 || frame_0C4->dlc < 2) {
        return false;
    }

    // Extract angle and rate from 0x0C4 frame
    uint16_t raw_0C4_angle, raw_0C4_rate;
    if (!extract_angle_and_rate_from_0C4(frame_0C4, &raw_0C4_angle, &raw_0C4_rate)) {
        return false;
    }

    // Scale angle to 0x10A range (±1200° → ±600°) with inversion
    uint16_t scaled_angle = scale_0C4_to_10a_fixed(raw_0C4_angle);

    // Convert rate from 0C4 format to 002/10A format
    uint8_t converted_rate = convert_0C4_rate_to_002_format(raw_0C4_rate);

    // Setup output frame header
    output_frame_10a->ID = 0x10A;
    output_frame_10a->dlc = 5;
    output_frame_10a->ide = 0;
    output_frame_10a->rtr = 0;

    // Set scaled angle data (bytes 0-1, little endian)
    output_frame_10a->data[0] = scaled_angle & 0xFF;
    output_frame_10a->data[1] = (scaled_angle >> 8) & 0xFF;

    // Use extracted and converted rate instead of calculating (byte 2)
    // This is the key fix - use rate from 0C4 bytes 2-3 instead of calculating
    output_frame_10a->data[2] = converted_rate;

    // Set constant value (byte 3)
    output_frame_10a->data[3] = 0x07;

    // Calculate and set rolling counter (byte 4)
    output_frame_10a->data[4] = calculate_rolling_counter(output_frame_10a->data, state->sequence_number);

    // Update state for next call
    state->prev_scaled_angle = scaled_angle;
    state->sequence_number++;
    state->has_previous_frame = true;

    return true;
}

// Global state instance for simple application interface
static steering_processor_state_t g_steering_state = {0, 0, false};

/**
 * Initialize the steering processor state
 * Call this once at system startup
 */
void init_steering_processor(void)
{
    g_steering_state.prev_scaled_angle = 0;
    g_steering_state.sequence_number = 0;
    g_steering_state.has_previous_frame = false;
}

/**
 * Handle incoming 0x0C4 frame and generate complete 0x10A frame
 * Simple application interface using global state
 *
 * @param frame_0C4 Input 0x0C4 CAN frame
 * @param output_frame_10a Output buffer for 0x10A frame
 * @return true if frame generated successfully, false on error
 */
bool handle_0C4_steering_frame(const CAN_FRAME *frame_0C4,
                               CAN_FRAME *output_frame_10a)
{
    return process_0C4_to_10a_frame(&g_steering_state, frame_0C4, output_frame_10a);
}

/**
 * Debug function to verify checksum calculation for a given payload
 * Can be used to validate incoming CAN messages
 *
 * @param data Pointer to 5-byte CAN data (including byte 4)
 * @param sequence_number Expected sequence number
 * @return true if checksum is valid, false otherwise
 */
bool verify_checksum(const uint8_t *data, uint16_t sequence_number)
{
    // Calculate what byte 4 should be
    uint8_t calculated = calculate_rolling_counter(data, sequence_number);

    // Compare with actual byte 4
    return (calculated == data[4]);
}

/**
 * Update CAN frame with calculated rolling counter
 *
 * @param frame Pointer to CAN frame structure
 * @param sequence_number Current sequence number for this message type
 */
void update_can_frame_counter(CAN_FRAME *frame, uint16_t sequence_number)
{
    if (frame->ID == 0x002 && frame->dlc >= 5) {
        frame->data[4] = calculate_rolling_counter(frame->data, sequence_number);
    }
}

/**
 * Update CAN frame with calculated rate and rolling counter
 *
 * @param frame Pointer to CAN frame structure
 * @param prev_frame Pointer to previous frame for rate calculation
 * @param sequence_number Current sequence number for this message type
 */
void update_can_frame_with_rate(CAN_FRAME *frame, const CAN_FRAME *prev_frame, uint16_t sequence_number)
{
    if (frame->ID == 0x002 && frame->dlc >= 5) {
        // Calculate rate from angle change if previous frame available
        if (prev_frame != NULL && prev_frame->ID == 0x002 && prev_frame->dlc >= 5) {
            uint16_t prev_angle = prev_frame->data[0] | (prev_frame->data[1] << 8);
            uint16_t curr_angle = frame->data[0] | (frame->data[1] << 8);
            frame->data[2] = calculate_steering_rate(prev_angle, curr_angle);
        }

        // Calculate rolling counter
        frame->data[4] = calculate_rolling_counter(frame->data, sequence_number);
    }
}

