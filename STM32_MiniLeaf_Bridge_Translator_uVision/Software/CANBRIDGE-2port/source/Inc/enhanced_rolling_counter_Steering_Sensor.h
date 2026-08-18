#ifndef ENHANCED_ROLLING_COUNTER_STEERING_SENSOR_H
#define ENHANCED_ROLLING_COUNTER_STEERING_SENSOR_H

#include <stdint.h>
#include <stdbool.h>
#include "can.h"

// Main rolling counter calculation function
uint8_t calculate_rolling_counter(const uint8_t *data, uint16_t sequence_number);

// Enhanced version with more patterns
uint8_t calculate_enhanced_rolling_counter(const uint8_t *data, uint16_t sequence_number);

// Steering rate calculation function
uint8_t calculate_steering_rate(uint16_t prev_angle, uint16_t curr_angle);

// Helper function to update CAN frame with rolling counter
void update_can_frame_counter(CAN_FRAME *frame, uint16_t sequence_number);

// Helper function to update CAN frame with both rate and rolling counter
void update_can_frame_with_rate(CAN_FRAME *frame, const CAN_FRAME *prev_frame, uint16_t sequence_number);

// 0x0C4 to 0x10A processing functions
bool handle_0C4_steering_frame(const CAN_FRAME *frame_0C4, CAN_FRAME *output_frame_10a);
void init_steering_processor(void);

// Validation function (optional, for testing)
uint8_t validate_enhanced_algorithm(void);

#endif // ENHANCED_ROLLING_COUNTER_STEERING_SENSOR_H