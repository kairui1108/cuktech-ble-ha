#pragma once

#include <stdint.h>
#include <stdbool.h>

/* ============================================================
 * device_state — single source of truth for charger state.
 *
 * Before this module, main.c and ble_manager.c each kept their own
 * settings[]/settings_valid[] arrays with different locks.  That
 * duplicated RAM and, more importantly, let the two copies drift
 * (e.g. PIID 17/18 were only written to main's copy, so BLE-side
 * protocol estimation never saw them).  All runtime state now lives
 * here behind one short critical section.
 * ============================================================ */

#define DEVICE_PORT_COUNT 4
#define DEVICE_PIID_COUNT 32

typedef struct {
    float   voltage;
    float   current;
    float   power;
    uint8_t protocol;
    uint8_t status;
    bool    active;
    bool    valid;
} PortState;

void device_state_init(void);

/* Ports */
void device_state_get_ports(PortState out[DEVICE_PORT_COUNT]);
void device_state_get_port(int idx, PortState *out);
void device_state_set_port(int idx, const PortState *p);

/* Settings (PIID 0..31) */
bool device_state_get_setting(uint8_t piid, uint32_t *value);
bool device_state_has_setting(uint8_t piid);
void device_state_set_setting(uint8_t piid, uint32_t value);
void device_state_get_settings(uint32_t out[DEVICE_PIID_COUNT],
                               bool valid[DEVICE_PIID_COUNT]);

/* PIID 16 / 21 have dedicated meanings in the frontend API. */
uint32_t device_state_port_ctrl(void);
void     device_state_set_port_ctrl(uint32_t value);
uint32_t device_state_protocol_ext(void);
void     device_state_set_protocol_ext(uint32_t value);

/* Desired BLE switch (what the user asked for) and actual readiness. */
bool device_state_ble_enabled(void);
void device_state_set_ble_enabled(bool enabled);
bool device_state_ble_ready(void);
void device_state_set_ble_ready(bool ready);
