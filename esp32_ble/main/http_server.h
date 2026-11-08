#pragma once

#include <stddef.h>
#include <stdbool.h>
#include "config_store.h"

typedef void (*http_config_cb)(void);

/* ============================================================
 * Callbacks provided by main.c.
 *
 * The writers serialize the corresponding JSON object/array
 * directly into the provided buffer and return the number of
 * characters written (excluding the NUL terminator), or -1 if
 * the buffer is too small.  No heap allocation on GET paths.
 * ============================================================ */
typedef int  (*ports_writer_cb)(char *buf, size_t cap);
typedef int  (*settings_writer_cb)(char *buf, size_t cap);
typedef bool (*get_setting_cb)(int piid, uint32_t *value);

typedef bool (*port_control_cb)(const char *port, const char *action);
typedef bool (*setting_set_cb)(int piid, int value);
typedef bool (*protocol_toggle_cb)(const char *port, const char *protocol, bool on);
typedef bool (*ble_control_cb)(bool enable);

void http_server_start(DeviceConfig *cfg, http_config_cb on_save);
void http_server_stop(void);

void http_server_set_callbacks(ports_writer_cb ports, settings_writer_cb settings,
                               get_setting_cb get_setting,
                               port_control_cb port_ctl, setting_set_cb setting_set,
                               protocol_toggle_cb proto_toggle,
                               ble_control_cb ble_ctl);
