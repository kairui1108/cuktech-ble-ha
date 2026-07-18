#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

// ============================================================
// MiOT BLE Protocol Constants & Utilities
// ============================================================

// --- GATT Service ---
#define MIOT_SERVICE_UUID    "0000fe95-0000-1000-8000-00805f9b34fb"

// --- Characteristics UUID ---
#define CHAR_UUID_AUTH_CTRL  0x0010
#define CHAR_UUID_AUTH_DATA  0x0019
#define CHAR_UUID_CMD_SEND   0x001A
#define CHAR_UUID_CMD_RECV   0x001B

// --- Auth Command ---
#define CMD_LOGIN            0x24
#define CMD_SEND_KEY         0x0B
#define CMD_SEND_INFO        0x0A
#define AUTH_SUCCESS         0x21
#define AUTH_ACTIVATE        0x11

// --- SIID / PIID ---
#define SIID_CHARGER         2

// --- TLV Type IDs ---
#define TID_UINT8            1
#define TID_UINT32           5
