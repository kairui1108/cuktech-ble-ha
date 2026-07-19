#pragma once

#include <stdint.h>
#include <stdbool.h>

typedef struct {
    char wifi_ssid[33];
    char wifi_pass[65];
    char device_mac[18];
    char device_token[25];
    char device_ble_key[33];
    char mqtt_broker[64];
    uint16_t mqtt_port;
    char mqtt_user[33];
    char mqtt_pass[65];
    char mqtt_topic_prefix[64];
    bool mqtt_enable;
    bool bemfa_enable;
    char bemfa_uid[33];
    bool valid;
} DeviceConfig;

void config_store_init(void);
bool config_store_load(DeviceConfig *cfg);
bool config_store_save(const DeviceConfig *cfg);
void config_store_apply_defaults(DeviceConfig *cfg);
bool config_store_is_configured(void);
void config_store_erase(void);
