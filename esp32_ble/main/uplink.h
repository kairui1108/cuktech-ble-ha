#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "config_store.h"

/* ============================================================
 * uplink — the only place that publishes telemetry.
 *
 * All publishes are rate-limited and serialized in uplink_loop()
 * (called from app_task), so BLE/HTTP tasks never block on the MQTT
 * outbox.  Telemetry uses QoS 0 (matching the Python reference
 * implementation); inbound command subscriptions stay QoS 1.
 * ============================================================ */

typedef void (*uplink_set_cb)(int piid, uint32_t value);
typedef void (*uplink_port_cb)(const char *port, const char *action);
typedef void (*uplink_ble_cb)(bool enabled);

void uplink_init(const DeviceConfig *cfg);
void uplink_set_command_callbacks(uplink_set_cb set_cb,
                                  uplink_port_cb port_cb,
                                  uplink_ble_cb ble_cb);

/* Mark state dirty; the actual publish happens in uplink_loop(). */
void uplink_publish_port(int idx);
void uplink_publish_settings(void);
void uplink_publish_status(void);

/* Called from app_task on a fixed cadence. */
void uplink_loop(void);

bool uplink_connected(void);
