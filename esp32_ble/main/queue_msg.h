#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

enum CmdType {
    CMD_NOP = 0,
    CMD_GET,
    CMD_SET,
    CMD_PORT,       // piid=bit_index(0-3), value=1(on)/0(off) — does GET+modify+SET atomically
    CMD_POLL_STEP,
    CMD_DISCONNECT,
    CMD_RECONNECT,
};

typedef struct {
    enum CmdType type;
    uint8_t piid;
    uint32_t value;
    uint32_t poll_seq;
} BleCommand;

enum ResultType {
    RES_NOP = 0,
    RES_GET,
    RES_SET,
    RES_PORT_PUSH,
    RES_POLL_STEP,
    RES_BLE_STATUS,
    RES_DRAIN_DONE,
    RES_KEEPALIVE,
};

typedef struct {
    enum ResultType type;
    bool success;
    uint8_t piid;
    uint32_t value;
    uint32_t poll_seq;
    float voltage, current, power;
    uint8_t protocol;
    uint8_t status;
    bool ble_ready;
} BleResult;

extern QueueHandle_t cmd_queue;
extern QueueHandle_t urgent_queue;
extern QueueHandle_t result_queue;
