
//----------------------------------------------
// this code is to make a minni a leaf.
// AKA translate the mini cooper and nissan leaf AZE1 can messages
// Right now we only listion to the nissan leaf
// we then conver those messages to the mini cooper for the dcluster only.
// currently 1. SOC(fuel), 2. power(rpm) 3. speed, 4. gear shift pos 5. other indacators like cruse control, ETC 6. Steering Angle 7...
// this will fake the messages from the motor ECU, transmition ECU, and HVAC, ETC so taht the cooper guages do not through fualts.
// base code structure based on https://github.com/dalathegreat/Nissan-LEAF-Battery-Upgrade/
// all Mini code compleatly custom reversed engineered by RWGresearch (~Russ Gries) with the help of AI as i suck at programing.
// but i did do alot of hand pecking and AI could not figgure out alot of stuff strangly enugh...
// its a work progross still 8-20-2025
// note that it takes 5 can bridges to make this work, between the custom PCB's and its internal RR can, EV Can, Car can, K Can, P Can
// 1. can bridge between the leaf EV can > mini cluster can
// 2. can bridge between the leaf car can > mini cluster can
// 3. can bridge between the mini k-can > isolating mini cluster on its own can.
// 4. can bridge between the leaf car can > RR can for rc control and other control means like cruse or feed back and Steering Angle
// 5. can bridge between the leaf car can > leaf EV can to fake the 2015 BMS in the 2025 Leaf

//----------------


#include "can.h" // Include CAN bus library definitions

#include "can-bridge-firmware.h"  // Include project-specific firmware header
#include "nissan_can_structs.h"   // Include Nissan CAN structure definitions
#include "REPLAY_PATTERNS.h"  		// Include Nissan CAN structure definitions
#include "enhanced_rolling_counter_Steering_Sensor.h"  // Include enhanced rolling counter functions

#include <stdint.h>               // Fixed-width integer types
#include <stddef.h>               // size_t type
#include <stdio.h>  						  // Standard C library for I/O (used for debugging if needed)
#include <string.h> 						  // Standard C library for memory and string operations // memcpy for copying payload bytes
#include <stdbool.h> 							// Standard C library for bool's
#include <math.h>							  	// Standard C library for math functions (floorf, roundf)

// =====================
// Option for setting the CAN type
// =====================
// options for bridge type (dont forget to set buad in can.h for the type of bridge your using) mini K-can is 100k buad and leaf is 500K buad. and mini Pcan is 500K
static bool my_bridge_car_can_listen = 0;         // if using for listing to leaf car can and converting that data for the mini cluster set to 1
static bool my_bridge_Leaf_Car_to_RR_Convert = 0; // if using for messages to convert from Leaf to RR can, data set to 1
static bool my_bridge_RR_to_Leaf_Car_Convert = 1; // if using for messages to convert from RR to Leaf Car can, data set to 1 (Angle Sensor)
static bool my_bridge_Mini_to_RR_Convert = 0;     // if using for messages to convert from Mini to RR can, data set to 1
static bool my_bridge_ev_can_listen = 0;          // if using for listing to leaf battery can and converting that data for the mini cluster set to 1
static bool my_bridge_mini_cluster = 0;           // if using for mini cooper between the car and the cluster to block error messages from going to the mini cluster set to 1
static bool Bentch_Testing = 0;                   // use for bentch testing so the messages get played back for mini cluster... do not use if in mini...

// =====================
// Sleep/Wake Configuration
// =====================
// Set the CAN message ID that keeps this bridge awake.
// If this message is not seen for sleep_timeout_seconds, the bridge sleeps.
// The bridge wakes on any CAN interrupt (WFI).
static uint32_t sleep_keepalive_id = 0x10A;    // Message ID to watch (0x10A for RR steering, 0x1F2 for EV can, 0x180 for car can)
static uint8_t  sleep_keepalive_bus = 1;        // Which CAN bus to watch (0=CAN1, 1=CAN2)
static uint8_t  sleep_timeout_seconds = 60;     // Seconds without keepalive before sleep
static bool     sleep_enabled = 0;              // Enable/disable sleep feature
static volatile uint32_t last_keepalive_tick = 0; // Last time keepalive message was seen

// Forward declarations for replay system ( fake the mini missing messages for mini cluster)
void replay_can_replay_init(void);
void replay_can_replay_tick_10ms(void);

// =====================
// Global variables for periodic CAN messages
// =====================

// Snapshot caches for periodic retransmission of reactive messages
static uint8_t last_194_data[8] = {0};
static uint8_t last_1D6_data[8] = {0};
static uint8_t last_194_dlc = 0;
static uint8_t last_1D6_dlc = 0;
static bool    cached_194_valid = false;
static bool    cached_1D6_valid = false;

static CAN_FRAME cached_0C4_frame;           // Last 0x0C4 for periodic 0x10A generation
static bool      cached_0C4_valid = false;

// Cached 0x10A steering data for periodic 0x002 generation (RR->Leaf Car path)
static uint8_t  cached_10A_data[4] = {0x00, 0x00, 0x00, 0x07};  // Bytes 0-3: angle(2) + rate(1) + constant(1)
static bool     cached_10A_valid = false;
static uint16_t seq_002 = 0;               // Rolling counter sequence for outgoing 0x002



// Stores the latest calculated RPM for the 0x0AA message
static uint16_t current_rpm = 2000;      // Initialized to 2000 RPM (zero power idle equivalent)

// Stores the latest vehicle speed in miles per hour for the 0x1A6 message
static float current_speed_mph = 0.0f;   // Initialized to 0 mph

// Stores the latest battery state-of-charge percentage for the 0x349 message
static float current_soc = 0.0f;         // Initialized to 0%

// Global variable to store current gear selection from 0x421 message
static uint8_t current_gear_byte = 0x08; // Initialize to Park position

// Global variable to store the ASCD speed requested_kph "cruise control speed set point" for 0x200
static float ASCD_speed_request_kph = 25.6f; // Initialize to 16 kph

// Global variable to store current e-pedal state
//static uint8_t e_pedal_state = 0; // Initialize to off

// Global variable to store current ready state details for ASCD (cruise control on / off ready / not ready / set / not set) for 0x200 message
static uint8_t ASCD_ready_state = 0; // 0=not active, 1=ready, 2=ready&set, 3=unknown

// Global variable to store eco state
static uint8_t eco_state = 0x00; // 0=off, 0x40=on


// Keep the original one_second_ping function even though it's not used in this code
void one_second_ping(void)
{
    // Placeholder function - could be used for diagnostics or status messages every second
}

// =====================
// Combined 100 ms periodic message sender
// =====================
void periodic_send_all_100ms(void)
{
    // add 100ms second tasks here
}


// =====================
// Sleep/Wake helper functions
// =====================
void bridge_check_keepalive(CAN_FRAME *frame, uint8_t can_bus)
{
    if (frame->ID == sleep_keepalive_id && can_bus == sleep_keepalive_bus)
    {
        last_keepalive_tick = HAL_GetTick();
    }
}

uint8_t bridge_should_sleep(void)
{
    if (!sleep_enabled)
    {
        return 0;
    }
    uint32_t now = HAL_GetTick();
    if ((now - last_keepalive_tick) > ((uint32_t)sleep_timeout_seconds * 1000u))
    {
        return 1;
    }
    return 0;
}


// =====================
// Periodic output function - decoupled from input message timing
// Called from main loop, uses HAL_GetTick() for all timing gates
// =====================
void bridge_periodic_output(void)
{
    // Timing variables for each periodic rate
    static uint32_t last_send_10ms = 0;
    static uint32_t last_send = 0;       // 100ms timer
    static uint32_t last_send500 = 0;
    static uint32_t last_send200 = 0;

    // Persistent counters for speed/time tracking in the 0x1A6 message
    static uint32_t speed_counter = 0; // Accumulates distance-like units
    static uint16_t time_counter = 0;
    static uint32_t counters_last_reset_time = 0; // When we last reset the counters
    static bool counters_reset_on_startup = false; // Flag to reset once on startup

    // Get current system time in milliseconds from HAL (wraps after ~49 days)
    uint32_t now = HAL_GetTick();

    // === 10ms tasks (replay + speed counter + 0x1A6) ===
    if (now - last_send_10ms >= 10u)
    {
        last_send_10ms = now;

        replay_can_replay_tick_10ms(); // Run replay scheduler every 10 ms (for mini cluster replay system)

        // Reset counters once on first startup or after long inactivity
        if (!counters_reset_on_startup)
        {
            // First time running - reset counters and mark startup
            speed_counter = 0;
            time_counter = 0;
            counters_last_reset_time = now;
            counters_reset_on_startup = true;
        }
        else if (counters_last_reset_time != 0 && (now - counters_last_reset_time > 1800000)) // 30 minutes since last reset ( TEST*** this might reset the mph and avrage speed etc on the cluster)
        {
            // Been a long time since we reset - likely a new drive cycle
            speed_counter = 0;
            time_counter = 0;
            counters_last_reset_time = now;
        }

        // Update reset time periodically while running (every 60 seconds)
        static uint32_t last_reset_time_update = 0;
        if (now - last_reset_time_update > 60000) // Update every minute while running
        {
            counters_last_reset_time = now; // Keep the reset timer current while active
            last_reset_time_update = now;
        }

        // Calculate how much time has passed since last send
        uint32_t delta_ms = now - last_send200;

        // Increase speed counter based on elapsed time and current speed
        // Formula: increment = (delta_ms / 50 ms) * speed (mph)
        double inc_speed = (delta_ms / 50.0) * (double)current_speed_mph;
        speed_counter += (uint32_t)inc_speed; // Accumulate into counter

        // Increment rolling time counter based on elapsed milliseconds since last update
        // Formula: increment = (delta_ms / 50 ms) * 100
        double inc_time = (delta_ms / 50.0) * 100.0;
        time_counter += (uint16_t)inc_time; // Accumulate into time counter
        time_counter %= 4096;               // Wrap at 12 bits (0-4095)

        if (my_bridge_car_can_listen == 1) // only if we are reprducing speed for mini cluster and on the car can.
        {
            // Build and send 0x1A6 message (vehicle speed and time counter)
            // ============================

            CAN_FRAME frame_1A6; // Structure for CAN message

            frame_1A6.ID = 0x1A6; // CAN ID for Mini Cooper speed message
            frame_1A6.dlc = 8;    // Data length: 8 bytes
            frame_1A6.ide = 0;    // Standard ID format
            frame_1A6.rtr = 0;    // Data frame

            // Truncate to 16-bit for CAN message
            uint16_t speed_for_can = speed_counter & 0xFFFF;

            // Populate three 2-byte speed counters (same value in all three)
            for (int i = 0; i < 3; i++)
            {
                int off = i * 2; // Offset in data array
                frame_1A6.data[off]     = speed_for_can & 0xFF;        // Use speed_for_can (16-bit)
                frame_1A6.data[off + 1] = (speed_for_can >> 8) & 0xFF; // Use speed_for_can (16-bit)
            }

            // Populate time counter in BIG ENDIAN
            // Upper 4 bits of high byte fixed to 0xF, lower 4 bits = upper time bits
            // -----------------------------
            // Update Bytes 6-7: Rolling 12-bit counter
            // Increments at a rate of +100 counts every 50 ms, independent of vehicle speed.
            // Byte 6 contains the lower 8 bits.
            // Byte 7 contains the high 4 bits in the lower nibble, with the upper nibble fixed to 0xF.
            // -----------------------------
            frame_1A6.data[6] = time_counter & 0xFF;
            frame_1A6.data[7] = 0xF0 | ((time_counter >> 8) & 0x0F);

            // Send frame on CAN bus #1 ------------------------------
            PushCan(1, CAN_TX, &frame_1A6); //(vehicle speed and time counter)
        }

        // --- Periodic steering: Mini_to_RR_Convert (cached 0x0C4 -> 0x10A) ---
        if (my_bridge_Mini_to_RR_Convert == 1 && cached_0C4_valid)
        {
            CAN_FRAME frame_10A;
            if (handle_0C4_steering_frame(&cached_0C4_frame, &frame_10A))
            {
                PushCan(1, CAN_TX, &frame_10A);
            }
        }

        // --- Periodic steering: RR_to_Leaf_Car_Convert (cached 0x10A -> 0x002) ---
        if (my_bridge_RR_to_Leaf_Car_Convert == 1)
        {
            CAN_FRAME frame_002;
            frame_002.ID  = 0x002;
            frame_002.dlc = 5;
            frame_002.ide = 0;
            frame_002.rtr = 0;
            memcpy(frame_002.data, cached_10A_data, 4);  // Bytes 0-3 from cached 0x10A
            frame_002.data[4] = calculate_enhanced_rolling_counter(frame_002.data, seq_002); // Byte 4: checksum + counter
            seq_002++;
            PushCan(0, CAN_TX, &frame_002); // Send to Leaf Car CAN
        }

        // --- Periodic button forwarding: Mini_to_RR_Convert ---
        if (my_bridge_Mini_to_RR_Convert == 1)
        {
            if (cached_194_valid)
            {
                CAN_FRAME frame_194;
                frame_194.ID = 0x194;
                frame_194.dlc = last_194_dlc;
                frame_194.ide = 0;
                frame_194.rtr = 0;
                memcpy(frame_194.data, last_194_data, last_194_dlc);
                PushCan(1, CAN_TX, &frame_194);
            }
            if (cached_1D6_valid)
            {
                CAN_FRAME frame_1D6;
                frame_1D6.ID = 0x1D6;
                frame_1D6.dlc = last_1D6_dlc;
                frame_1D6.ide = 0;
                frame_1D6.rtr = 0;
                memcpy(frame_1D6.data, last_1D6_data, last_1D6_dlc);
                PushCan(1, CAN_TX, &frame_1D6);
            }
        }
    }

    // === 100ms tasks (0x0AA RPM) ===
    if (now - last_send >= 100u)
    {
        // Update last send time to now
        last_send = now;

        if (my_bridge_ev_can_listen == 1) // only send if we are reprducing RPM mini cluster and on the EV can.
        {
            // ============================
            // Build and send 0x0AA message (power output > RPM for scale for Mini Cooper cluster)
            // ============================

            CAN_FRAME frame_0AA;  // Structure to hold CAN message  for leaf power output/RPM guage

            frame_0AA.ID = 0x0AA; // Set CAN ID to 0x0AA (Mini Cooper RPM message)
            frame_0AA.dlc = 8;    // Data length: 8 bytes
            frame_0AA.ide = 0;    // Standard ID format
            frame_0AA.rtr = 0;    // Data frame, not a remote request

            frame_0AA.data[0] = 0xF1; // Fixed byte pattern
            frame_0AA.data[1] = 0x40; // Fixed byte pattern
            frame_0AA.data[2] = 0xFF; // Throttle position low byte (example: foot off)
            frame_0AA.data[3] = 0x00; // Throttle position high byte

            // Convert RPM to raw value (Mini Cooper expects RPM * 4)
            uint16_t rpm_raw = current_rpm * 4;
            frame_0AA.data[4] = rpm_raw & 0xFF;        // Low byte of RPM
            frame_0AA.data[5] = (rpm_raw >> 8) & 0xFF; // High byte of RPM

            frame_0AA.data[6] = 0x80; // Fixed byte pattern
            frame_0AA.data[7] = 0x99; // Fixed byte pattern

            // Send frame on CAN bus #1
            PushCan(1, CAN_TX, &frame_0AA); // (power output > RPM for scale for Mini Cooper cluster)
        }
    }

    // === 200ms tasks (0x349 SOC, 0x200 cruise, 0x1D2 gear) ===
    if (now - last_send200 >= 200u)
    {
        // Update last send time to now
        last_send200 = now;

        if (my_bridge_ev_can_listen == 1) // only send if we are reprducing SOC mini cluster and on the EV can.
        {
            // ============================
            // Build and send 0x349 message (fuel level based on SOC)
            // ============================

            // Map SOC (0-100%) to raw_fuel (0 = 0% fuel, 3200 = 100% fuel) NOTE, fuel guage is inverce, so 100 - current_soc dose this.. must use off set or sensor is in error state...
            uint16_t raw_fuel = (uint16_t)(((100 - current_soc) / 100.0f) * 2950.0f) +150; // @ 5% SOC battery the --- mls will be 0 miles left
            if (raw_fuel < 0) raw_fuel = 150; // Clamp to min (0% fuel)
            if (raw_fuel > 3200) raw_fuel = 3200; // Clamp to max (100% fuel)

            CAN_FRAME frame_349; // Structure for CAN message
            frame_349.ID = 0x349; // CAN ID for Mini Cooper fuel message
            frame_349.dlc = 5;    // Data length: 5 bytes
            frame_349.ide = 0;    // Standard ID format
            frame_349.rtr = 0;    // Data frame

            // Fill both fuel sensors with raw_fuel value (little endian)
            frame_349.data[0] = raw_fuel & 0xFF;         // Sensor 1 low byte
            frame_349.data[1] = (raw_fuel >> 8) & 0xFF;  // Sensor 1 high byte
            frame_349.data[2] = raw_fuel & 0xFF;         // Sensor 2 low byte
            frame_349.data[3] = (raw_fuel >> 8) & 0xFF;  // Sensor 2 high byte
            frame_349.data[4] = 0x00;                    // Reserved / unused byte

            // Optional: Mimic trace discrepancy by offsetting Sensor 2 (~1.2% fuel, 37 units) the mini cooper has 2 sensors and there not the same they change, we need to fake this.
            uint16_t raw_fuel_sensor2 = raw_fuel > 37 ? raw_fuel - 37 : 0; // Offset based on trace
            frame_349.data[2] = raw_fuel_sensor2 & 0xFF;         // Sensor 2 low byte
            frame_349.data[3] = (raw_fuel_sensor2 >> 8) & 0xFF;  // Sensor 2 high byte
            //Send frame on CAN bus #1

            PushCan(1, CAN_TX, &frame_349); // fuel level based on SOC
        }

        if (my_bridge_car_can_listen == 1) // only send if we are reprducing data for mini cluster and on the car can.
        {
            // ============================
            // Build and send 0x200 message (generate ASCD (cruise) frame here) send every 200ms.
            // ============================
            bool ASCD_ready = 0; // set up var used in this block

            CAN_FRAME frame_200; // Structure for CAN message
            frame_200.ID = 0x200; // CAN ID for cruise control message
            frame_200.dlc = 8;    // Data length: 8 bytes
            frame_200.ide = 0;    // Standard ID format
            frame_200.rtr = 0;    // Data frame

            // Clamp KPH to valid range 16-160 ish...
            uint8_t kph = ASCD_speed_request_kph;
            if (kph < 20) kph = 20;
            if (kph > 255) kph = 255;

            // Calculate contributions based on the decoding formula
            float base_km_h = 25.6f;
            float delta_km_h = (float)kph - base_km_h;
            int delta1 = (int)floorf(delta_km_h / 25.6f);  // Coarse adjustment from byte1
            float remainder = delta_km_h - (delta1 * 25.6f);
            int delta0 = (int)roundf(remainder / 0.1024f);  // Fine adjustment from byte0

            // Compute byte0 and byte1 starting at this value and add teh delta.
            uint8_t byte0 = 0x04 + delta0;
            uint8_t byte1 = 0xA1 + delta1;

            // Clamp byte's to maximum's
            if (byte0 > 255) byte0 = 255;  // Ensure byte1 stays within 0-255
            if (byte1 > 255) byte1 = 255;  // Ensure byte1 stays within 0-255

            if (ASCD_ready_state != 0) // exreact the state of the ASCD, there could be more than on / off so we need to extract that.
            {
                ASCD_ready = 1;
            }
            else
            {
                ASCD_ready = 0;
            }

            // Set the CAN data array
            frame_200.data[0] = byte0;          // byte0
            frame_200.data[1] = byte1;          // byte1
            frame_200.data[2] = ASCD_ready ? 0xFD : 0xFC;  // Cruise status : STATUS_READY 0xFD, STATUS_NOT_READY 0xFC
            frame_200.data[3] = 0xFF;           // Unused
            frame_200.data[4] = 0xFF;           // Unused
            frame_200.data[5] = 0xFF;           // Unused
            frame_200.data[6] = 0xFF;           // Unused
            frame_200.data[7] = 0xFF;           // Unused

            // Send frame on CAN bus #1
            PushCan(1, CAN_TX, &frame_200); // cruise control (ASCD) message


            // Function to send gear position message 0x1D2 based on current gear selection
            // =====================
            // Rolling counter for 0x1D2 message (0-14, wraps to 0)
            static uint8_t counter_1D2 = 0;

            CAN_FRAME frame_1D2; // Structure for gear position CAN message

            frame_1D2.ID = 0x1D2; // Set CAN ID to 0x1D2 for gear position
            frame_1D2.dlc = 6;    // Data length: 6 bytes
            frame_1D2.ide = 0;    // Standard ID format
            frame_1D2.rtr = 0;    // Data frame, not a remote request

            // Calculate rolling counter byte value
            uint8_t counter_byte = (counter_1D2 << 4) | 0x0C;

            // Determine gear position data based on current gear byte value
            switch(current_gear_byte)
            {
                case 0x08: // Park position
                    frame_1D2.data[0] = 0xE1; // Park signature byte 0
                    frame_1D2.data[1] = 0x0F; // Park signature byte 1
                    frame_1D2.data[2] = 0xFF; // Park signature byte 2
                    frame_1D2.data[3] = counter_byte; // Park signature byte 3
                    frame_1D2.data[4] = 0xF0; // Park signature byte 4
                    frame_1D2.data[5] = 0xFF; // Park signature byte 5
                    break;

                case 0x20: // Drive position

                    if (eco_state == 0x40) //if ecco is on show D only
                    {
                        frame_1D2.data[0] = 0x78; // Drive signature byte 0
                        frame_1D2.data[1] = 0x0F; // Drive signature byte 1
                        frame_1D2.data[2] = 0xFF; // Drive signature byte 2
                        frame_1D2.data[3] = counter_byte; // Drive signature byte 3
                        frame_1D2.data[4] = 0xF0; // Drive signature byte 4
                        frame_1D2.data[5] = 0xFF; // Drive signature byte 5
                        break;
                    }
                    else // if eco is off show DS for sport...
                    {
                        frame_1D2.data[0] = 0x78; // Drive signature byte 0
                        frame_1D2.data[1] = 0x0F; // Drive signature byte 1
                        frame_1D2.data[2] = 0xFF; // Drive signature byte 2
                        frame_1D2.data[3] = counter_byte; // Drive signature byte 3
                        frame_1D2.data[4] = 0xF1; // Drive signature byte 4
                        frame_1D2.data[5] = 0xFF; // Drive signature byte 5
                        break;
                    }


                case 0x38: // B position (engine braking/low gear)

                    if (eco_state == 0x40) //if ecco is on show M2 only
                    {
                        frame_1D2.data[0] = 0x78; // B position signature byte 0
                        frame_1D2.data[1] = 0x6F; // B position signature byte 1
                        frame_1D2.data[2] = 0xFF; // B position signature byte 2
                        frame_1D2.data[3] = counter_byte; // B position signature byte 3
                        frame_1D2.data[4] = 0xF2; // B position signature byte 4
                        frame_1D2.data[5] = 0xFF; // B position signature byte 5
                        break;
                    }
                    else // if eco is off show M1 for sport...
                    {
                        frame_1D2.data[0] = 0x78; // B position signature byte 0
                        frame_1D2.data[1] = 0x5F; // B position signature byte 1
                        frame_1D2.data[2] = 0xFF; // B position signature byte 2
                        frame_1D2.data[3] = counter_byte; // B position signature byte 3
                        frame_1D2.data[4] = 0xF2; // B position signature byte 4
                        frame_1D2.data[5] = 0xFF; // B position signature byte 5
                        break;
                    }

                case 0x18: // Neutral position
                    frame_1D2.data[0] = 0xB4; // Neutral signature byte 0
                    frame_1D2.data[1] = 0x0F; // Neutral signature byte 1
                    frame_1D2.data[2] = 0xFF; // Neutral signature byte 2
                    frame_1D2.data[3] = counter_byte; // Neutral signature byte 3
                    frame_1D2.data[4] = 0xF0; // Neutral signature byte 4
                    frame_1D2.data[5] = 0xFF; // Neutral signature byte 5
                    break;

                case 0x10: // Reverse position
                    frame_1D2.data[0] = 0xD2; // Reverse signature byte 0
                    frame_1D2.data[1] = 0x0F; // Reverse signature byte 1
                    frame_1D2.data[2] = 0xFF; // Reverse signature byte 2
                    frame_1D2.data[3] = counter_byte; // Reverse signature byte 3
                    frame_1D2.data[4] = 0xF0; // Reverse signature byte 4
                    frame_1D2.data[5] = 0xFF; // Reverse signature byte 5
                    break;

                default: // Default to Park if unknown gear position detected
                    frame_1D2.data[0] = 0xE1; // Default Park signature byte 0
                    frame_1D2.data[1] = 0x0F; // Default Park signature byte 1
                    frame_1D2.data[2] = 0xFF; // Default Park signature byte 2
                    frame_1D2.data[3] = counter_byte; // Default Park signature byte 3
                    frame_1D2.data[4] = 0xF0; // Default Park signature byte 4
                    frame_1D2.data[5] = 0xFF; // Default Park signature byte 5
                    break;
            }

            // Send gear position frame on CAN bus #1
            PushCan(1, CAN_TX, &frame_1D2); // send gear position message 0x1D2 based on current gear selection

            // Increment rolling counter and reset at 15
            counter_1D2++;
            if (counter_1D2 >= 15)
            {
                counter_1D2 = 0;
            }
        }
    }

    // === 500ms tasks ===
    if (now - last_send500 >= 500u)
    {
        // Update last send time to now
        last_send500 = now;
        // ============================
    }
}


// =====================
// CAN handler function
// Processes incoming CAN frames, updates globals, and forwards/creates messages
// This code is broke up in  awy where we look at what type of bridge we are and do / and do things based on what bridge we are using function.
// =====================
void can_handler(uint8_t can_bus, CAN_FRAME *frame)
{

		// Check keepalive for sleep/wake system on every incoming frame
		bridge_check_keepalive(frame, can_bus);

		uint8_t blocked = 0;  // used for pass through blocking

    // Only process messages from CAN bus #0 (MYCAN1 == 0)(can 1 on the PCB) this is the listen can.
    if (can_bus == 0)
    {

			  // ----------------
        // messages for leaf car_can only
        // ----------------
			if (my_bridge_car_can_listen == 1)
			{
					// ----------------
					// Handle CAN frame with specific ID 0x421: gear position selector
					// ----------------
					if (frame->ID == 0x421 && frame->dlc >= 1) // (frame->ID == 0x421)
					{
							// Read gear position from byte 0 of incoming message
							current_gear_byte = frame->data[0];

										// =====================
					}

					// ----------------
					// Handle CAN frame with specific ID 0x280: vehicle speed
					// ----------------
					if (frame->ID == 0x280 && frame->dlc >= 6) // (frame->ID == 0x280)
					{
							// Extract 16-bit unsigned speed from bytes 4-5 (big-endian)
							uint16_t raw_speed = (frame->data[4] << 8) | frame->data[5];

							// Convert raw value to km/h
							float speed_kmh = raw_speed * 0.01f;

							// Convert to mph
							current_speed_mph = (speed_kmh * 0.621371f) / 1.85f; // *2; //had to / 2 or 1.xx as the speed was 2x the speed ? to fast...  if i want 2x the add 2 * try "/0.925" in replacment of "1.85f *2"

							if (current_speed_mph < 0.0f)
							{
								  current_speed_mph = 0.0f; //clamp
							}
 				  		if (current_speed_mph > 150.0f)
							{
								  current_speed_mph = 0.0f; // fail safe so that when there is no data we dont go crazy on the speed... also helped with " bad data" glitch...
							}
					}


					// ----------------
					// Handle CAN frame with specific ID 0x551: extracts the ACSD data for criuse control from the car can, also contains eco on / off or e-peddal on / off check this
					// ----------------
					if (frame->ID == 0x551 && frame->dlc >= 7)  // Fixed CAN ID
					{
							// Read ASCD speed request in MPH from byte 4 of incoming message
							uint8_t ASCD_speed_request_mph = frame->data[4]; // Already in decimal, no conversion needed

							// Convert MPH to KPH and store in global variable
						  ASCD_speed_request_kph = ASCD_speed_request_mph * 1.60934f; // Keep as float

							// clamp if outside range or not actave:
							if (ASCD_speed_request_kph > 256)
							{
									ASCD_speed_request_kph = 0;
							}

							// Read ASCD state from byte 5
							uint8_t ASCD_state = frame->data[5];

							switch(ASCD_state)
							{
									case 0x00: // Not active
											ASCD_ready_state = 0;
											break;
									case 0x50: // Ready active
											ASCD_ready_state = 1;
											break;
									case 0x40: // Ready and set active
											ASCD_ready_state = 2;
											break;
									default: // Unknown state
											ASCD_ready_state = 3;
											break;
							}

							// Get eco state from byte 6 (00 = off, 40 = on)
							eco_state = frame->data[6]; // if this is eco add this to the gear selection to show sport mode or not...
					}

			}

        // ----------------
        // messages for EV can only
        // ----------------
			if (my_bridge_ev_can_listen == 1)
			{

					// Handle CAN frame with specific ID 0x1DB: battery voltage and current
					// Expect DLC >= 3 for these fields
					if (frame->ID == 0x1DB && frame->dlc >= 3)
					{
							const uint8_t *b = frame->data;

							// Voltage: 23|10@0+ (0.5, 0)
							// raw bits: b2[7:0], b1[7:0]; 10-bit field across them, Motorola
							uint16_t raw_voltage = (uint16_t)(((uint16_t)b[2] << 2) | (b[1] >> 6)) & 0x03FF;
							float voltage = (float)raw_voltage * 0.5f;  // volts, NO +200 offset

							// Current: 7|11@0- (0.5, 0), signed 11-bit spanning b0/b1, Motorola
							uint16_t cur11 = (uint16_t)(((uint16_t)b[0] << 3) | (b[1] >> 5)) & 0x07FF;
							// Sign-extend to match Python (subtract 0x0800 equivalent)
					if (cur11 & 0x0400)
					{
						cur11 -= 0x0800;  // Matches Python's sign-extension
					}
					int16_t raw_current = (int16_t)cur11;
					float current = (float)raw_current * 0.5f;  // amps

					// If you want positive power when discharging the pack
					float current_eff = -current;

					// Power in kW
					float power_kw = (voltage * current_eff) / 1000.0f;

					// Define max power and max regen (based on research: max power 110 kW, max regen -43 kW)
					const float P_max = 110.0f;  // Max discharge power in kW
					const float P_min = -40.0f;  // Max regen power in kW (negative)

					// Calculate RPM based on power (unclamped to match Python)
					float rpm_unclamped;
					if (power_kw <= 0.0f)
							{
								// Regen range: P_min to 0 -> 0 to 2000 RPM
								rpm_unclamped = 2000.0f * (power_kw - P_min) / (0.0f - P_min);
							} else
							{
								// Power range: 0 to P_max -> 2000 to 8000 RPM
								rpm_unclamped = 2000.0f + 6000.0f * (power_kw / P_max);
							}

							// Clamp RPM for actual use (optional, to match existing behavior)
							//uint16_t rpm = (uint16_t)rpm_unclamped;
							//if (rpm > 8000) rpm = 8000;
							//if (rpm < 0) rpm = 0;
							// below is the corect way to do this so its clamped corectly...
							if (rpm_unclamped > 8000.0f) rpm_unclamped = 8000.0f;
							if (rpm_unclamped < 0.0f) rpm_unclamped = 0.0f;
							uint16_t rpm = (uint16_t)rpm_unclamped;

							// Update the global current_rpm for periodic sending
							current_rpm = rpm;
					}

				// ----------------
        // Handle CAN frame with specific ID 0x55B: battery SOC
        // ----------------
        if (frame->ID == 0x55B && frame->dlc >= 2) // (frame->ID == 0x55B)
        {
            // Extract raw SOC (10 bits big endian, factor 0.1)
            uint16_t raw_soc = ((frame->data[0] << 2) |
                                ((frame->data[1] & 0xC0) >> 6));

				  	// Convert to inverted % SOC (100% - calculated SOC)
            //float soc = 100.0f - (raw_soc * 0.1f); // Invert: e.g., 75% becomes 25%

            // Convert to % SOC
            float soc = raw_soc * 0.1f;

            // Clamp to 0-100%
            if (soc > 100.0f) soc = 100.0f;
            if (soc < 0.0f) soc = 0.0f;

            // Update global SOC value
            current_soc = soc;
        }

			}

			if (my_bridge_Leaf_Car_to_RR_Convert == 1) // if we are sending messages out to RR can coming from Leaf Car can:
			{
						// do task here for any leaf car can to RR can. place holder ATM
			}

			if (my_bridge_Mini_to_RR_Convert == 1) // if we are sending messages out to RR can coming from Mini P-Can:
			{
							// Cache raw 0x0C4 steering sensor data for periodic retransmission as 0x10A
							if (frame->ID == 0x0C4 && frame->dlc >= 7)  // Fixed CAN ID
							{
									memcpy(&cached_0C4_frame, frame, sizeof(CAN_FRAME));
									cached_0C4_valid = true;
							}
			}
		}
		// end of read only can 0 only.

		// Only process messages from CAN bus #1 in this case its the "out going" can but we are trying to listion to the RR can in this case. (can 2 on the PCB)
    if (can_bus == 1)
		{

				// if we want to send out anything from RR can to Leaf Car can.
				if (my_bridge_RR_to_Leaf_Car_Convert == 1) // if we are sending messages out to Leaf Car can from RR can:
				{
						// Cache incoming 0x10A steering data for periodic 0x002 generation (timing-independent)
						if (frame->ID == 0x10A && frame->dlc >= 5)  // Fixed CAN ID
						{
							memcpy(cached_10A_data, frame->data, 4); // Cache bytes 0-3 (angle + rate + constant)
							cached_10A_valid = true;                 // Mark cache as valid, periodic sender will handle 0x002
						}

				}

		}


		// block unwanted messages for this convert and pass or dont pass the rest.
		if (my_bridge_Mini_to_RR_Convert == 1) // block some messages pass the rest on so the k - can work corectly for other functions like body control stuff.
		{
			switch (frame->ID)
        {

				//--------------------
				// The below are from the genarated items, 1. SOC(fuel), 2. power(rpm) 3. speed, 4. gear shift pos
				//--------------------

				case 0x194: // pass MF Steering wheele cruse control buttions data and send it out to the RR can.
					  // Cache the data for periodic sending
					  memcpy(last_194_data, frame->data, frame->dlc);
					  last_194_dlc = frame->dlc;
					  cached_194_valid = true;
					  blocked = 1;
            break;

				case 0x1D6: // pass steering MF steering buttions for Auido data and send it out to the RR can.
					  // Cache the data for periodic sending
					  memcpy(last_1D6_data, frame->data, frame->dlc);
					  last_1D6_dlc = frame->dlc;
					  cached_1D6_valid = true;
					  blocked = 1;
            break;

        default:

            blocked = 1;
            break;
        }

        if (!blocked) // This passes messages from one bridge to the other if not blocked.
				{
						if (can_bus == 0)
						{
								PushCan(1, CAN_TX, frame);
						}
						else
						{
								PushCan(0, CAN_TX, frame);
						}
        }
		}

	      // block unwanted messages coming from the mini k-can buss going ot the cluster.
        // we are replacing some of thses messages with our own, on the replay_ReplayItem replay_items list.
	      // other items are the ones we are genarating above.
		if (my_bridge_mini_cluster == 1) // block some messages pass the rest on so teh k - can work corectly for other functions like body control stuff.
		{
			switch (frame->ID)
        {

				//--------------------
				// The below are from the genarated items, 1. SOC(fuel), 2. power(rpm) 3. speed, 4. gear shift pos
				//--------------------
        case 0x0A8: // Torque, Clutch and Brake status

            blocked = 1;
            break;

        case 0x0C0: // ABS / Brake counter

            blocked = 0;
            break;

        case 0x0D7: // Counter (Airbag / Seatbelt Related) on the bentch the air bag comes on when not faking this.

            blocked = 0;
            break;

        case 0x130: // Ignition and Key status (Term 15 / R ON?) possibal to tie this in to the leaaf?

            blocked = 0;
            break;

        case 0x19E: // If this ID is not present or stops then the Instrument Cluster will report an ABS error along with, tyre pressure, and a red parking brake error.

            blocked = 0;
            break;

        case 0x1D0: // Engine temp, Pressure sensor & Handbrake

            blocked = 1;
            break;

        case 0x2D6: // Air Conditioning Status.

            blocked = 1;
            break;

        case 0x2E6: // Climate control status (Fan and Temp speed)

            blocked = 1;
            break;
        case 0x2EA: // This register reflects the status of the Climate control unit for the Passenger side.

            blocked = 1;
            break;

        case 0x34F: // Handbrake status turns on / off light on dash

            blocked = 0;
            break;


				//--------------------
				// The below are from the genarated items, 1. SOC(fuel), 2. power(rpm) 3. speed, 4. gear shift pos
				//--------------------
				case 0x1A6: // (vehicle speed and time counter)

            blocked = 1;
            break;

				case 0x0AA: // (power output > RPM for scale for Mini Cooper cluster

            blocked = 1;
            break;

				case 0x349: // fuel level based on SOC

            blocked = 1;
            break;

				case 0x1D2: // send gear position message 0x1D2 based on current gear selection

            blocked = 1;
            break;

        default:

            blocked = 0;
            break;
        }

        if (!blocked) // This passes messages from one bridge to the other if not blocked.
				{
						if (can_bus == 0)
						{
								PushCan(1, CAN_TX, frame);
						}
						else
						{
								PushCan(0, CAN_TX, frame);
						}
        }
		}
}




// ===== Begin auto-generated replay system (prefixed with replay_) =====

static int replay__initialized = 0; // lazy-init flag for replay system

// can_replay_generated.c -- Auto-generated from PCAN .trc

// Replay rules:
// * All frames transmit on bus #1 using PushCan(1, CAN_TX, &tx)
// * Period per ID = avg inter-arrival, rounded to nearest 10 ms
// * If data unchanged, pattern has 1 step; if changed, we loop the observed sequence

// thses are teh messages that we are genarating to fake the missing parts of the mini, Motor ECU, Transmission ECU, Air Con onfo.
static replay_ReplayItem replay_items[] = { // Table linking IDs to patterns and timing, lat digit 1 = enable 0 = diable.
		{ 0x0A8 , 8, 100, (uint16_t)(sizeof(replay_PATTERN_00A8)/sizeof(replay_PATTERN_00A8[0])), replay_PATTERN_00A8, 0u, 0u, 1u }, // 0x0A8 Torque, Clutch and Brake status
		{ 0x0C0 , 2, 200, (uint16_t)(sizeof(replay_PATTERN_00C0)/sizeof(replay_PATTERN_00C0[0])), replay_PATTERN_00C0, 0u, 0u, 1u }, // 0x0C0 ABS / Brake counter
		{ 0x0D7 , 2, 200, (uint16_t)(sizeof(replay_PATTERN_00D7)/sizeof(replay_PATTERN_00D7[0])), replay_PATTERN_00D7, 0u, 0u, 1u }, // 0x0D7 Counter (Airbag / Seatbelt Related) on the bentch the air bag comes on when not faking this.
		{ 0x130 , 5, 100, (uint16_t)(sizeof(replay_PATTERN_0130)/sizeof(replay_PATTERN_0130[0])), replay_PATTERN_0130, 0u, 0u, 1u }, // 0x130 Ignition and Key status (Term 15 / R ON?) possibal to tie this in to the leaaf?
		{ 0x19E , 8, 200, (uint16_t)(sizeof(replay_PATTERN_019E)/sizeof(replay_PATTERN_019E[0])), replay_PATTERN_019E, 0u, 0u, 1u }, // 0x19E If this ID is not present or stops then the Instrument Cluster will report an ABS error along with, tyre pressure, and a red parking brake error.
		{ 0x1D0 , 8, 100, (uint16_t)(sizeof(replay_PATTERN_01D0)/sizeof(replay_PATTERN_01D0[0])), replay_PATTERN_01D0, 0u, 0u, 1u }, // 0x1D0 Engine temp, Pressure sensor & Handbrake
		{ 0x2D6 , 3, 100, (uint16_t)(sizeof(replay_PATTERN_02D6)/sizeof(replay_PATTERN_02D6[0])), replay_PATTERN_02D6, 0u, 0u, 1u }, // 0x2D6 Air Conditioning Status.
		{ 0x2E6 , 8, 100, (uint16_t)(sizeof(replay_PATTERN_02E6)/sizeof(replay_PATTERN_02E6[0])), replay_PATTERN_02E6, 0u, 0u, 1u }, // 0x2E6 Climate control status (Fan and Temp speed)
		{ 0x2EA , 8, 100, (uint16_t)(sizeof(replay_PATTERN_02EA)/sizeof(replay_PATTERN_02EA[0])), replay_PATTERN_02EA, 0u, 0u, 1u }, // 0x2EA This register reflects the status of the Climate control unit for the Passenger side.
		{ 0x34F , 2, 5000, (uint16_t)(sizeof(replay_PATTERN_034F)/sizeof(replay_PATTERN_034F[0])), replay_PATTERN_034F, 0u, 0u, 1u }, // 0x34F Handbrake status turns on / off light on dash
}; // end replay_items
static const size_t replay_count = sizeof(replay_items)/sizeof(replay_items[0]); // number of IDs

static void replay_replay_init_schedule(void) { // Initialize schedule for all IDs
		uint32_t replay_now_ms = HAL_GetTick();        // Get current ms tick
		for (size_t i = 0; i < replay_count; ++i) { // Walk all replay items
				replay_items[i].next_due_ms = replay_now_ms + replay_items[i].period_ms; // First send after one period
				replay_items[i].step_idx    = 0;   // Start from first step
		}                                      // End loop
} // end replay_replay_init_schedule

static void replay_replay_poll_and_send(void) { // Poll due frames and transmit on bus #1

	  //------------------------------//
		if (my_bridge_ev_can_listen == 0 && Bentch_Testing == 0 ) // only send playback if we are playing back missing messages for mini cluster OR if we are testing...
		{
				return; // Exit early if replay system is disabled, only enable if we are EV can... or testing...
		}

		uint32_t replay_now_ms = HAL_GetTick();        // Snapshot current time
		for (size_t i = 0; i < replay_count; ++i) { // Iterate all items
				replay_ReplayItem *it = &replay_items[i];     // Alias to current item
				if ((int32_t)(replay_now_ms - it->next_due_ms) >= 0) { // Due replay_now_ms?
					if (!it->enabled) continue; // Skip disabled messages
						CAN_FRAME tx;                      // Prepare transmit frame
						tx.ID  = (uint32_t)(it->id & 0x7FFu); // Ensure standard 11-bit ID
						tx.dlc = it->dlc;                  // Set DLC
						tx.ide = 0;                        // Standard frame
						tx.rtr = 0;                        // Data frame
						memcpy(tx.data, it->steps[it->step_idx].bytes, tx.dlc);  //memcpy(tx.data, it->steps[it->step_idx].bytes, 8u); // Copy payload
						PushCan(1, CAN_TX, &tx);           // Send via existing CAN handler on bus #1
					  //PushCan(0, CAN_TX, &tx);         // Send via existing CAN handler on bus #0 if you need to send messages back to the k-can on the mini do that here
						it->step_idx = (uint16_t)((it->step_idx + 1u) % it->step_count); // Next step (wrap)
						it->next_due_ms = replay_now_ms + it->period_ms; // Schedule next send
				}                                      // End if due
		}                                          // Next item
} // end replay_replay_poll_and_send

void replay_can_replay_init(void) {  // Call once after CAN init
		replay_replay_init_schedule();   // Prime schedules
} // end replay_can_replay_init


void replay_can_replay_tick_10ms(void) { if (!replay__initialized) { replay_can_replay_init(); replay__initialized = 1; } // ensure initialized
		// Call from your existing 10 ms tick (ID 0x1F2 path or semilar 10 ms message)
		replay_replay_poll_and_send();       // Transmit any due frames
} // end replay_can_replay_tick_10ms


// ===== End auto-generated replay system =====
