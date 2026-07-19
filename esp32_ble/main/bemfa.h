#pragma once

#include <stdbool.h>
#include "config_store.h"

typedef bool (*bemfa_cmd_cb)(const char *port, const char *action);
typedef bool (*bemfa_ble_cb)(bool enable);

void bemfa_init(const DeviceConfig *cfg, bemfa_cmd_cb on_cmd, bemfa_ble_cb on_ble);
void bemfa_disconnect(void);
void bemfa_loop(void);
void bemfa_publish_port(int idx, float voltage, float current, float power, bool active);
void bemfa_publish_status(bool connected);
