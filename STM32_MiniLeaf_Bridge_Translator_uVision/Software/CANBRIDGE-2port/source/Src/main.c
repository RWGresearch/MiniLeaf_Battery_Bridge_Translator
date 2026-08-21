/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2022 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
#include "main.h"
#include "can.h"
#include "iwdg.h"
#include "gpio.h"

#include "stdio.h"
#include <string.h>
#include "bridge_sequencer.h" /* MiniLeaf bridge logic (replaces the old Mini-Cooper can-bridge-firmware.h) */

/* FIRMWARE_REVISION changelog (this port's equivalent of main.py's REVISION):
 * Rev 1 (2026-08-21): Inp4 awake-indicator output (on while running, off
 *   during WFI sleep, see Phase 5 below); Inp3 fault-active-indicator output
 *   (mirrors g_mgmt_state's 3 latches, driven in bridge_sequencer.c); Inp2
 *   active-low manual fault-latch-reset input (edge-triggered, calls
 *   management_engine_notify_session_start(), also in bridge_sequencer.c).
 *   See docs/17-stm32-gpio-reference.md for the pin table and rationale.
 * Rev 2 (2026-08-21): Inp2's reset call swapped to the new
 *   management_engine_reset_all_conditions() (management_engine.c) - "Option
 *   A" soft reset, also clears the escalation/pending timers feeding the 3
 *   latches, not just the latches themselves (measured +48B Code/+0B RAM vs.
 *   Rev 1's plain notify_session_start()). Inp3 now blinks at 0.5s while a
 *   fault is active (was steady-on); Inp4 now blinks at 0.5s while CAN data
 *   is actively arriving on either bus, steady-on while awake-but-idle, off
 *   only when genuinely asleep (measured +60B Code/+8B RAM over Rev 1).
 *   Both driven from one shared blink-phase timer in bridge_sequencer_tick().
 * Rev 3 (2026-08-21): Inp4's blink rate split off from Inp3's - Inp4 (CAN
 *   data) now blinks at 0.1s instead of sharing Inp3's 0.5s, so the two read
 *   as visually distinct rates instead of looking identical at a glance.
 *   Inp3 (fault) stays at 0.5s. Two independent blink-phase timers now
 *   (measured +32B Code/+8B RAM over Rev 2's single shared timer).
 */
#define FIRMWARE_REVISION 3

static MYCAN_Errors mErrors[2] = {0};
// last_tick removed -- sleep/wake now handled by bridge_sequencer_should_sleep()
uint32_t au32_UID[3] = {0}; 
static const uint8_t au8_lock[12] = {0x33,0x44,0x55,0x66,0x11,0x22,0x33,0x44,0x77,0x66,0x55,0x44};
static uint8_t config_Bits[2] = {0};
static uint32_t canErrors = 0;
// idleTick removed -- replaced by configurable keepalive in bridge_sequencer_should_sleep()

void SystemClock_Config(void);

int main(void)
{
  /* USER CODE BEGIN 1 */
   CAN_FRAME frame;
  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/
  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */

	HAL_Init();

  /* Configure the system clock */
  SystemClock_Config();

    if( strncmp(STM32_UUID, (const char*)au8_lock, 12 ) != 0 )
    {
        NVIC_SystemReset();
    }

		MX_GPIO_Init();
		MX_CAN1_Init();
		MX_CAN2_Init();
    
    //MX_IWDG_Init(); //Init watchdog

    config_Bits[0] = (HAL_GPIO_ReadPin(Inp1_GPIO_Port, Inp1_Pin) == GPIO_PIN_SET ? 1:0) | (HAL_GPIO_ReadPin(Inp2_GPIO_Port, Inp2_Pin) == GPIO_PIN_SET ? 2:0); 
    config_Bits[1] = (HAL_GPIO_ReadPin(Inp3_GPIO_Port, Inp3_Pin) == GPIO_PIN_SET ? 1:0) | (HAL_GPIO_ReadPin(Inp4_GPIO_Port, Inp4_Pin) == GPIO_PIN_SET ? 2:0);  
    
    HAL_CAN_RegisterCallback(&hcan1, HAL_CAN_RX_FIFO0_MSG_PENDING_CB_ID, HAL_CAN_RxFIFO0MsgPendingCallback1 );
    HAL_CAN_RegisterCallback(&hcan1, HAL_CAN_RX_FIFO1_MSG_PENDING_CB_ID, HAL_CAN_RxFIFO1MsgPendingCallback1 );    
    HAL_CAN_RegisterCallback(&hcan2, HAL_CAN_RX_FIFO0_MSG_PENDING_CB_ID, HAL_CAN_RxFIFO0MsgPendingCallback2 );
    HAL_CAN_RegisterCallback(&hcan2, HAL_CAN_RX_FIFO1_MSG_PENDING_CB_ID, HAL_CAN_RxFIFO1MsgPendingCallback2 ); 
		
		HAL_CAN_ActivateNotification(&hcan1, CAN_IT_RX_FIFO0_MSG_PENDING);
		HAL_CAN_ActivateNotification(&hcan2, CAN_IT_RX_FIFO1_MSG_PENDING);
    
    AddCANFilters( &hcan1 );
    AddCANFilters( &hcan2 );

    // Initialize the MiniLeaf bridge sequencer (STM32 port Phase 2 skeleton -
    // see STM32_MiniLeaf_Bridge_Translator_uVision/.../Inc/bridge_sequencer.h)
    bridge_sequencer_init();

    while (1)
    {
        //HAL_IWDG_Refresh(&hiwdg);

        // === Phase 1: Drain ALL pending RX messages (update globals) ===
        while (LenCan(MYCAN1, CAN_RX) > 0)
        {
            PopCan(MYCAN1, CAN_RX, &frame);
            bridge_sequencer_on_frame(MYCAN1, &frame);
        }
        while (LenCan(MYCAN2, CAN_RX) > 0)
        {
            PopCan(MYCAN2, CAN_RX, &frame);
            bridge_sequencer_on_frame(MYCAN2, &frame);
        }

        // === Phase 2: Generate periodic output (independent timing) ===
        bridge_sequencer_tick();

        // === Phase 3: Transmit pending TX frames ===
        sendCan(MYCAN1);
        sendCan(MYCAN2);

        // === Phase 4: Error monitoring + bus-off recovery ===
        canErrors = hcan1.Instance->ESR;
        mErrors[0].rec = canErrors >> 24;
        mErrors[0].trans = ( canErrors >> 16 ) &0xff;
        mErrors[0].lastErrorCode = ( canErrors & 0x70 ) >> 4;
        mErrors[0].boff = ( canErrors & 0x04 ) >> 2;
        mErrors[0].passive = ( canErrors & 0x02 ) >> 1;
        mErrors[0].errorFlag = canErrors & 1;
        // mErrors was already being filled in every loop but never acted on -
        // ABOM (AutoBusOff=ENABLE in can.c) lets the peripheral itself clear
        // bus-off once the bus goes idle, but if that never happens (or takes
        // too long), nothing here ever forced it. Stop+Start is the documented
        // bxCAN software bus-off recovery: entering/leaving INIT mode clears
        // the error counters and bus-off state. Filters survive this (they
        // live in a separate FINIT-gated register block, untouched by INRQ).
        if( mErrors[0].boff )
        {
            HAL_CAN_Stop( &hcan1 );
            HAL_CAN_Start( &hcan1 );
        }

        canErrors = hcan2.Instance->ESR;
        mErrors[1].rec = canErrors >> 24;
        mErrors[1].trans = ( canErrors >> 16 ) &0xff;
        mErrors[1].lastErrorCode = ( canErrors & 0x70 ) >> 4;
        mErrors[1].boff = ( canErrors & 0x04 ) >> 2;
        mErrors[1].passive = ( canErrors & 0x02 ) >> 1;
        mErrors[1].errorFlag = canErrors & 1;
        if( mErrors[1].boff )
        {
            HAL_CAN_Stop( &hcan2 );
            HAL_CAN_Start( &hcan2 );
        }

        // === Phase 5: Sleep check ===
        if (bridge_sequencer_should_sleep())
        {
            HAL_GPIO_WritePin(Inp4_GPIO_Port, Inp4_Pin, GPIO_PIN_RESET);  // awake light off
            HAL_SuspendTick();
            HAL_PWR_EnterSLEEPMode(PWR_MAINREGULATOR_ON, PWR_SLEEPENTRY_WFI);
            HAL_ResumeTick();
            HAL_GPIO_WritePin(Inp4_GPIO_Port, Inp4_Pin, GPIO_PIN_SET);    // awake light on
        }
    }
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_LSI|RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.HSEPredivValue = RCC_HSE_PREDIV_DIV5;
  RCC_OscInitStruct.HSIState = RCC_HSI_ON;
  RCC_OscInitStruct.LSIState = RCC_LSI_ON;
  RCC_OscInitStruct.Prediv1Source = RCC_PREDIV1_SOURCE_PLL2;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLMUL = RCC_PLL_MUL9;
  RCC_OscInitStruct.PLL2.PLL2State = RCC_PLL2_ON;
  RCC_OscInitStruct.PLL2.PLL2MUL = RCC_PLL2_MUL8;
  RCC_OscInitStruct.PLL2.HSEPrediv2Value = RCC_HSE_PREDIV2_DIV5;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV2; //DIV1 is original for 72Mhz
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1; //DIV2 is original for 72Mhz
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2) != HAL_OK)
  {
    Error_Handler();
  }

  /** Configure the Systick interrupt time
  */
  __HAL_RCC_PLLI2S_ENABLE();
}


/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
    /* User can add his own implementation to report the HAL error return state */
    __disable_irq();
		HAL_NVIC_SystemReset ( );
}

