#include "device_state.h"

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* Short critical sections only: every accessor copies a handful of
   words.  Never call anything blocking or allocate while holding it. */
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

static PortState s_ports[DEVICE_PORT_COUNT];
static uint32_t  s_settings[DEVICE_PIID_COUNT];
static bool      s_settings_valid[DEVICE_PIID_COUNT];
static bool      s_ble_enabled = true;
static bool      s_ble_ready   = false;

#define LOCK()   portENTER_CRITICAL(&s_lock)
#define UNLOCK() portEXIT_CRITICAL(&s_lock)

/* Defaults for values the device cannot be asked for (no MiOT GET for
   many PIIDs).  Mirrors the old _init_settings_defaults(). */
void device_state_init(void)
{
    LOCK();
    memset(s_ports, 0, sizeof(s_ports));
    memset(s_settings, 0, sizeof(s_settings));
    memset(s_settings_valid, 0, sizeof(s_settings_valid));

    s_settings[5]  = 1;  s_settings_valid[5]  = true;   /* scene mode: AI */
    s_settings[6]  = 1;  s_settings_valid[6]  = true;   /* screen time: 5 min */
    s_settings[13] = 1;  s_settings_valid[13] = true;   /* language: zh */
    s_settings[15] = 0;  s_settings_valid[15] = true;   /* USB-A always-on */
    s_settings[19] = 1;  s_settings_valid[19] = true;   /* idle screen */
    s_settings[20] = 1;  s_settings_valid[20] = true;   /* orientation lock */
    for (int i = 9; i <= 12; i++) s_settings_valid[i] = true; /* countdown = 0 */

    /* PIID 21: all protocols on. C1/C2 PD+PPS+reserved, C3/A UFCS+SCP. */
    s_settings[16] = 0xFF;             /* assume all ports on */
    s_settings_valid[16] = true;
    s_settings[21] = 0x03030F0F;
    s_settings_valid[21] = true;

    s_ble_enabled = true;
    s_ble_ready   = false;
    UNLOCK();
}

void device_state_get_ports(PortState out[DEVICE_PORT_COUNT])
{
    if (!out) return;
    LOCK();
    memcpy(out, s_ports, sizeof(s_ports));
    UNLOCK();
}

void device_state_get_port(int idx, PortState *out)
{
    if (!out || idx < 0 || idx >= DEVICE_PORT_COUNT) return;
    LOCK();
    *out = s_ports[idx];
    UNLOCK();
}

void device_state_set_port(int idx, const PortState *p)
{
    if (!p || idx < 0 || idx >= DEVICE_PORT_COUNT) return;
    LOCK();
    s_ports[idx] = *p;
    UNLOCK();
}

bool device_state_get_setting(uint8_t piid, uint32_t *value)
{
    if (piid >= DEVICE_PIID_COUNT) return false;
    LOCK();
    if (value) *value = s_settings[piid];
    bool valid = s_settings_valid[piid];
    UNLOCK();
    return valid;
}

bool device_state_has_setting(uint8_t piid)
{
    if (piid >= DEVICE_PIID_COUNT) return false;
    LOCK();
    bool valid = s_settings_valid[piid];
    UNLOCK();
    return valid;
}

void device_state_set_setting(uint8_t piid, uint32_t value)
{
    if (piid >= DEVICE_PIID_COUNT) return;
    LOCK();
    s_settings[piid] = value;
    s_settings_valid[piid] = true;
    UNLOCK();
}

void device_state_get_settings(uint32_t out[DEVICE_PIID_COUNT],
                               bool valid[DEVICE_PIID_COUNT])
{
    LOCK();
    if (out)   memcpy(out, s_settings, sizeof(s_settings));
    if (valid) memcpy(valid, s_settings_valid, sizeof(s_settings_valid));
    UNLOCK();
}

uint32_t device_state_port_ctrl(void)
{
    LOCK();
    uint32_t v = s_settings[16];
    UNLOCK();
    return v;
}

void device_state_set_port_ctrl(uint32_t value)
{
    LOCK();
    s_settings[16] = value;
    s_settings_valid[16] = true;
    UNLOCK();
}

uint32_t device_state_protocol_ext(void)
{
    LOCK();
    uint32_t v = s_settings[21];
    UNLOCK();
    return v;
}

void device_state_set_protocol_ext(uint32_t value)
{
    LOCK();
    s_settings[21] = value;
    s_settings_valid[21] = true;
    UNLOCK();
}

bool device_state_ble_enabled(void)
{
    LOCK();
    bool v = s_ble_enabled;
    UNLOCK();
    return v;
}

void device_state_set_ble_enabled(bool enabled)
{
    LOCK();
    s_ble_enabled = enabled;
    UNLOCK();
}

bool device_state_ble_ready(void)
{
    LOCK();
    bool v = s_ble_ready;
    UNLOCK();
    return v;
}

void device_state_set_ble_ready(bool ready)
{
    LOCK();
    s_ble_ready = ready;
    UNLOCK();
}
