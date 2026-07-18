#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

// ============================================================
// CUKTECH Charger BLE Bridge - Defaults & Feature Flags
// ============================================================

// --- 首次配置默认值（用户通过 AP 配置页面可修改）---
#define DEFAULT_WIFI_SSID        ""
#define DEFAULT_WIFI_PASS        ""
#define DEFAULT_DEVICE_MAC       ""
#define DEFAULT_DEVICE_TOKEN     ""
#define DEFAULT_DEVICE_BLE_KEY   ""
#define DEFAULT_MQTT_ENABLE      true
#define DEFAULT_MQTT_BROKER      ""
#define DEFAULT_MQTT_PORT        1883
#define DEFAULT_MQTT_USER        ""
#define DEFAULT_MQTT_PASS        ""
#define DEFAULT_MQTT_TOPIC_PREFIX "cuktech/charger"

// --- 功能开关 ---
#define ENABLE_MQTT      true

// --- Timing ---
#define KEEPALIVE_INTERVAL_MS    10000

// --- AP 配置模式 ---
#define AP_SSID         "CUKTECH-Setup"
#define AP_PASSWORD     "12345678"
