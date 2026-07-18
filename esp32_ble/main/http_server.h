#pragma once

#include "config_store.h"
#include "cJSON.h"

typedef void (*http_config_cb)(void);

// Callbacks for main.c to provide data / handle commands
typedef cJSON *(*port_data_cb)(void);
typedef cJSON *(*settings_cb)(void);
typedef bool (*port_control_cb)(const char *port, const char *action);
typedef bool (*setting_set_cb)(int piid, int value);
typedef bool (*protocol_toggle_cb)(const char *port, const char *protocol, bool on);
typedef bool (*ble_control_cb)(bool enable);

void http_server_start(DeviceConfig *cfg, http_config_cb on_save);
void http_server_stop(void);
void http_server_set_callbacks(port_data_cb ports, settings_cb settings,
                               port_control_cb port_ctl, setting_set_cb setting_set,
                               protocol_toggle_cb proto_toggle,
                               ble_control_cb ble_ctl);
