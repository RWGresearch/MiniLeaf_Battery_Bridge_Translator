# CAN Bridge Firmware

CAN bridge system that translates messages between Nissan LEAF and Mini Cooper vehicles, enabling LEAF electric drivetrain integration with Mini Cooper instrument cluster.

## Core Functionality

The firmware operates as a dual CAN bus bridge that listens to LEAF CAN messages and generates corresponding Mini Cooper CAN messages for dashboard compatibility.

### Key Features

- **Dual CAN Interface**: Simultaneous operation on two CAN buses (500k and 100k baud)
- **Real-time Translation**: Converts LEAF vehicle data to Mini Cooper format:
  - Battery SOC → Fuel level display
  - Motor power → RPM gauge simulation  
  - Vehicle speed → Speedometer data
  - Gear selection → Transmission status
  - Cruise control → ASCD system data
  - Steering angle → Converted steering data for RR CAN
- **Message Replay System**: Generates missing Mini Cooper CAN messages using pre-recorded patterns
- **Power Management**: Sleep mode activation during CAN bus idle periods
- **Error Monitoring**: Real-time CAN bus error detection and diagnostics

### Bridge Operation Modes

- `my_bridge_car_can_listen`: Processes LEAF car CAN messages (gear, speed, cruise control)
- `my_bridge_RR_Convert`: Processes steering angle data for RR CAN conversion
- `my_bridge_ev_can_listen`: Processes LEAF battery CAN messages (SOC, power output)
- `my_bridge_mini_cluster`: Filters Mini Cooper K-CAN messages for cluster isolation

## Source Code Structure

### Core Files (`Src/`)

- **`main.c`**: System initialization, CAN setup, main processing loop
- **`can-bridge-firmware.c`**: Message translation logic and bridge functionality
- **`can.c`**: CAN bus management, message queuing, interrupt handlers
- **`gpio.c`**: GPIO configuration for input pins
- **`iwdg.c`**: Watchdog timer implementation

### Headers (`Inc/`)

- **`can-bridge-firmware.h`**: Bridge function prototypes and defines
- **`can.h`**: CAN data structures and function declarations
- **`leaf_can_structs.h`**: LEAF-specific CAN message structures
- **`REPLAY_PATTERNS.h`**: Pre-recorded Mini Cooper CAN message patterns
- **`main.h`**: System-wide definitions and GPIO pin assignments

## Message Translation Examples

| LEAF Message | Function | Output |
|-------------|----------|--------|
| 0x55B (SOC) | Battery level | 0x349 (Fuel sensors) |
| 0x1DB (Power) | Motor output | 0x0AA (RPM display) |
| 0x280 (Speed) | Vehicle speed | 0x1A6 (Speed counter) |
| 0x421 (Gear) | Gear position | 0x1D2 (Transmission) |
| 0x551 (ASCD) | Cruise control | 0x200 (ASCD status) |
| 0x002 (Steering) | Steering angle | 0x10A (Converted steering data) |

## Build Environment

- **Target**: STM32F105RC microcontroller
- **IDE**: Keil MDK-ARM (project in `MDK-ARM/jcan.uvprojx`)
- **Compiler**: ARM Compiler v6.24

## Configuration

Bridge type selection via boolean flags in `can-bridge-firmware.c`:
- Set appropriate bridge mode flags based on CAN bus connection
- Configure CAN baud rates in `can.c` (Prescaler values: 4 for 500k, 20 for 100k)
- Adjust GPIO pin assignments in `main.h` for hardware configuration