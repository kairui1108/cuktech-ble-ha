#include "uplink.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#include "config.h"
#include "device_state.h"
#include "bemfa.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "mqtt_client.h"

static const char *TAG = "UPLINK";

#define TOPIC_SUFFIX_STATUS     "/status"
#define TOPIC_SUFFIX_SETTINGS   "/settings"
#define TOPIC_SUFFIX_PORT_C1    "/port/c1"
#define TOPIC_SUFFIX_PORT_C2    "/port/c2"
#define TOPIC_SUFFIX_PORT_C3    "/port/c3"
#define TOPIC_SUFFIX_PORT_A     "/port/a"
#define TOPIC_SUFFIX_SET        "/set"
#define TOPIC_SUFFIX_PORT_CMD   "/port"
#define TOPIC_SUFFIX_BLE        "/ble"

#define MAX_TOPIC_LEN 128
#define PORT_MIN_INTERVAL_MS     500   /* coalesce bursts; >= device push rate */
#define CONTROL_MIN_INTERVAL_MS  200

static char s_topic_status[MAX_TOPIC_LEN];
static char s_topic_settings[MAX_TOPIC_LEN];
static char s_topic_port_c1[MAX_TOPIC_LEN], s_topic_port_c2[MAX_TOPIC_LEN];
static char s_topic_port_c3[MAX_TOPIC_LEN], s_topic_port_a[MAX_TOPIC_LEN];
static char s_topic_set[MAX_TOPIC_LEN], s_topic_port_cmd[MAX_TOPIC_LEN], s_topic_ble[MAX_TOPIC_LEN];

static esp_mqtt_client_handle_t s_client = NULL;
static volatile bool s_connected = false;
static uplink_set_cb  s_set_cb  = NULL;
static uplink_port_cb s_port_cb = NULL;
static uplink_ble_cb  s_ble_cb  = NULL;

/* Pending work, protected because any task may mark state dirty. */
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t s_pending_ports = 0;
static bool s_pending_settings = false;
static bool s_pending_status = false;
/* Which pending events Bemfa has already consumed.  Without this a
   pending flag kept alive by an MQTT outage would republish to Bemfa
   every loop. */
static uint32_t s_bemfa_ports_sent = 0;
static bool s_bemfa_status_sent = false;

static uint64_t s_last_port_ms[4] = {0, 0, 0, 0};
static uint64_t s_last_settings_ms = 0;
static uint64_t s_last_status_ms = 0;
/* Bemfa is an independent sink: its rate windows must not consume the
   MQTT pending flags, otherwise a Bemfa-only install never gets data
   and an MQTT-only install would be delayed by the Bemfa cadence. */
static uint64_t s_last_bemfa_port_ms[4] = {0, 0, 0, 0};
static uint64_t s_last_bemfa_status_ms = 0;

static const char *PROTO_NAMES[] = {"idle","5V","5V","QC","AFC","FCP","SCP","PD","PPS","PPS","UFCS"};
static const int PROTO_NAMES_LEN = sizeof(PROTO_NAMES)/sizeof(PROTO_NAMES[0]);

static const char *get_proto_name(uint8_t code) {
    return (code < PROTO_NAMES_LEN) ? PROTO_NAMES[code] : "?";
}

static void _mark_port(int idx) {
    if (idx < 0 || idx >= 4) return;
    portENTER_CRITICAL(&s_lock);
    s_pending_ports |= (1u << idx);
    s_bemfa_ports_sent &= ~(1u << idx);
    portEXIT_CRITICAL(&s_lock);
}

void uplink_publish_port(int idx)     { _mark_port(idx); }

void uplink_publish_settings(void) {
    portENTER_CRITICAL(&s_lock);
    s_pending_settings = true;
    portEXIT_CRITICAL(&s_lock);
}

void uplink_publish_status(void) {
    portENTER_CRITICAL(&s_lock);
    s_pending_status = true;
    s_bemfa_status_sent = false;
    portEXIT_CRITICAL(&s_lock);
}

bool uplink_connected(void) { return s_connected; }

/* ============================================================
 * Minimal JSON field reader for the tiny inbound command payloads.
 * We deliberately avoid cJSON here: it is not needed for three flat
 * objects and the MQTT task must stay allocation-free on this path.
 * ============================================================ */
static const char *skip_ws(const char *p) {
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    return p;
}

static const char *find_key(const char *s, const char *key) {
    char pat[48];
    int n = snprintf(pat, sizeof(pat), "\"%s\"", key);
    if (n <= 0 || n >= (int)sizeof(pat)) return NULL;
    const char *p = s;
    while ((p = strstr(p, pat)) != NULL) {
        const char *q = skip_ws(p + n);
        if (*q == ':') return skip_ws(q + 1);
        p += n;
    }
    return NULL;
}

static bool json_get_num(const char *s, const char *key, double *out) {
    const char *p = find_key(s, key);
    if (!p) return false;
    char *end = NULL;
    double v = strtod(p, &end);
    if (end == p) return false;
    *out = v;
    return true;
}

static bool json_get_str(const char *s, const char *key, char *out, size_t cap) {
    const char *p = find_key(s, key);
    if (!p || *p != '"') return false;
    p++;
    size_t i = 0;
    while (*p && *p != '"') {
        if (*p == '\\' && p[1]) p++;
        if (i + 1 < cap) out[i++] = *p;
        p++;
    }
    if (*p != '"') return false;
    out[i] = '\0';
    return true;
}

static bool json_get_bool(const char *s, const char *key, bool *out) {
    const char *p = find_key(s, key);
    if (!p) return false;
    if (strncmp(p, "true", 4) == 0)  { *out = true;  return true; }
    if (strncmp(p, "false", 5) == 0) { *out = false; return true; }
    return false;
}

/* ============================================================
 * Publish helpers (called from app_task only)
 * ============================================================ */
static bool _publish_port_now(int idx, const PortState *p) {
    const char *proto = p->active ? get_proto_name(p->protocol) : "idle";
    char payload[256];
    int n = snprintf(payload, sizeof(payload),
                     "{\"voltage\":%.1f,\"current\":%.1f,\"power\":%.1f,\"active\":%s,\"protocol\":\"%s\"}",
                     p->voltage, p->current, p->power,
                     p->active ? "true" : "false", proto);
    if (n <= 0 || n >= (int)sizeof(payload)) return false;

    const char *topics[] = {s_topic_port_c1, s_topic_port_c2, s_topic_port_c3, s_topic_port_a};
    int rc = esp_mqtt_client_publish(s_client, topics[idx], payload, 0, 0, 1);
    if (rc < 0) {
        ESP_LOGW(TAG, "port %d publish failed (rc=%d)", idx, rc);
        return false;
    }
    ESP_LOGD(TAG, "port %d: v=%.1f c=%.1f p=%.1f active=%d",
             idx, p->voltage, p->current, p->power, (int)p->active);
    return true;
}

static bool _publish_settings_now(void) {
    uint32_t values[DEVICE_PIID_COUNT];
    bool valid[DEVICE_PIID_COUNT];
    device_state_get_settings(values, valid);

    char payload[512];
    int pos = snprintf(payload, sizeof(payload), "{");
    if (pos < 0 || pos >= (int)sizeof(payload)) return false;

    const uint8_t known[] = {5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21};
    bool first = true;
    for (int i = 0; i < (int)sizeof(known); i++) {
        uint8_t piid = known[i];
        bool publish_this = (piid == 16) || (piid == 21) || valid[piid];
        if (!publish_this) continue;

        int room = (int)sizeof(payload) - pos;
        if (room < 24) return false;
        int w = snprintf(payload + pos, room, "%s\"%u\":%lu",
                         first ? "" : ",", piid, (unsigned long)values[piid]);
        if (w < 0 || w >= room) return false;
        pos += w;
        first = false;
    }
    if (pos + 2 > (int)sizeof(payload)) return false;
    payload[pos++] = '}';
    payload[pos] = '\0';

    int rc = esp_mqtt_client_publish(s_client, s_topic_settings, payload, 0, 0, 1);
    if (rc < 0) {
        ESP_LOGW(TAG, "settings publish failed (rc=%d)", rc);
        return false;
    }
    return true;
}

static bool _publish_status_now(void) {
    bool ready = device_state_ble_ready();
    bool enabled = device_state_ble_enabled();

    char payload[256];
    int n = snprintf(payload, sizeof(payload),
                     "{\"connected\":%s,\"authenticated\":%s,\"ble_enabled\":%s,"
                     "\"device_model\":\"njcuk.fitting.ad1204\",\"firmware_version\":\"" FW_VERSION "\"}",
                     ready ? "true" : "false", ready ? "true" : "false",
                     enabled ? "true" : "false");
    if (n <= 0 || n >= (int)sizeof(payload)) return false;

    int rc = esp_mqtt_client_publish(s_client, s_topic_status, payload, 0, 0, 1);
    if (rc < 0) {
        ESP_LOGW(TAG, "status publish failed (rc=%d)", rc);
        return false;
    }
    return true;
}

/* ============================================================
 * Rate-limited fan-out, called from app_task.
 *
 * MQTT is the primary sink; Bemfa receives the same port/status
 * events even when MQTT is disabled or down.  Pending flags are
 * only cleared once the MQTT publish succeeds (or when MQTT is not
 * configured at all), so an MQTT outage cannot lose the latest
 * value while Bemfa keeps updating.
 * ============================================================ */
void uplink_loop(void) {
    uint64_t now = esp_timer_get_time() / 1000;

    uint32_t ports;
    bool settings, status;
    portENTER_CRITICAL(&s_lock);
    ports = s_pending_ports;
    settings = s_pending_settings;
    status = s_pending_status;
    portEXIT_CRITICAL(&s_lock);

    if (!ports && !settings && !status) return;

    const bool mqtt_up = (s_client != NULL && s_connected);
    const bool bemfa_up = bemfa_enabled();

    for (int idx = 0; idx < 4; idx++) {
        if (!(ports & (1u << idx))) continue;

        PortState p;
        device_state_get_port(idx, &p);

        /* Bemfa: update its cached state even while offline, so the
           state published on reconnect is current. */
        if (bemfa_up && p.valid &&
            now - s_last_bemfa_port_ms[idx] >= PORT_MIN_INTERVAL_MS) {
            bool need;
            portENTER_CRITICAL(&s_lock);
            need = (s_pending_ports & (1u << idx)) &&
                   !(s_bemfa_ports_sent & (1u << idx));
            if (need) s_bemfa_ports_sent |= (1u << idx);
            portEXIT_CRITICAL(&s_lock);
            if (need) {
                bemfa_publish_port(idx, p.voltage, p.current, p.power, p.active);
                s_last_bemfa_port_ms[idx] = now;
            }
        }

        bool done = false;
        if (mqtt_up) {
            if (now - s_last_port_ms[idx] >= PORT_MIN_INTERVAL_MS) {
                if (!p.valid) {
                    done = true;   /* nothing meaningful to publish yet */
                } else if (_publish_port_now(idx, &p)) {
                    s_last_port_ms[idx] = now;
                    done = true;
                }
            }
        } else if (s_client == NULL) {
            done = true;           /* MQTT not configured; Bemfa was served */
        }

        if (done) {
            portENTER_CRITICAL(&s_lock);
            s_pending_ports &= ~(1u << idx);
            s_bemfa_ports_sent &= ~(1u << idx);
            portEXIT_CRITICAL(&s_lock);
        }
    }

    if (settings && now - s_last_settings_ms >= CONTROL_MIN_INTERVAL_MS) {
        bool done = false;
        if (mqtt_up) {
            if (_publish_settings_now()) {
                s_last_settings_ms = now;
                done = true;
            }
        } else if (s_client == NULL) {
            done = true;           /* no settings sink without MQTT */
        }
        if (done) {
            portENTER_CRITICAL(&s_lock);
            s_pending_settings = false;
            portEXIT_CRITICAL(&s_lock);
        }
    }

    if (status) {
        if (bemfa_up && now - s_last_bemfa_status_ms >= CONTROL_MIN_INTERVAL_MS) {
            bool need;
            portENTER_CRITICAL(&s_lock);
            need = s_pending_status && !s_bemfa_status_sent;
            if (need) s_bemfa_status_sent = true;
            portEXIT_CRITICAL(&s_lock);
            if (need) {
                bemfa_publish_status(device_state_ble_ready());
                s_last_bemfa_status_ms = now;
            }
        }
        if (now - s_last_status_ms >= CONTROL_MIN_INTERVAL_MS) {
            bool done = false;
            if (mqtt_up) {
                if (_publish_status_now()) {
                    s_last_status_ms = now;
                    done = true;
                }
            } else if (s_client == NULL) {
                done = true;
            }
            if (done) {
                portENTER_CRITICAL(&s_lock);
                s_pending_status = false;
                s_bemfa_status_sent = false;
                portEXIT_CRITICAL(&s_lock);
            }
        }
    }
}

/* ============================================================
 * MQTT events
 * ============================================================ */
static void _handle_data(const char *topic, const char *data) {
    if (strstr(topic, "/set")) {
        double piid = 0, value = 0;
        if (json_get_num(data, "piid", &piid) &&
            json_get_num(data, "value", &value) &&
            piid > 0 && piid < 32) {
            if (s_set_cb) s_set_cb((int)piid, (uint32_t)value);
        }
    } else if (strstr(topic, "/port")) {
        char port[8], action[8];
        if (json_get_str(data, "port", port, sizeof(port)) &&
            json_get_str(data, "action", action, sizeof(action))) {
            if (s_port_cb) s_port_cb(port, action);
        }
    } else if (strstr(topic, "/ble")) {
        bool enabled = false;
        if (json_get_bool(data, "enabled", &enabled)) {
            if (s_ble_cb) s_ble_cb(enabled);
        }
    }
}

static void mqtt_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
    esp_mqtt_event_handle_t event = data;
    switch (event->event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT connected");
        s_connected = true;
        esp_mqtt_client_subscribe(s_client, s_topic_set, 1);
        esp_mqtt_client_subscribe(s_client, s_topic_port_cmd, 1);
        esp_mqtt_client_subscribe(s_client, s_topic_ble, 1);
        /* Flush current state after (re)connect. */
        portENTER_CRITICAL(&s_lock);
        s_pending_ports = 0x0F;
        s_pending_settings = true;
        s_pending_status = true;
        portEXIT_CRITICAL(&s_lock);
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT disconnected");
        s_connected = false;
        break;
    case MQTT_EVENT_DATA: {
        char topic[128];
        char payload[256];
        int tlen = event->topic_len < (int)sizeof(topic) - 1 ? event->topic_len : (int)sizeof(topic) - 1;
        int dlen = event->data_len  < (int)sizeof(payload) - 1 ? event->data_len  : (int)sizeof(payload) - 1;
        memcpy(topic, event->topic, tlen); topic[tlen] = '\0';
        memcpy(payload, event->data, dlen); payload[dlen] = '\0';
        _handle_data(topic, payload);
        break;
    }
    case MQTT_EVENT_ERROR:
        ESP_LOGW(TAG, "MQTT error");
        break;
    default:
        break;
    }
}

/* ============================================================
 * Init
 * ============================================================ */
void uplink_set_command_callbacks(uplink_set_cb set_cb,
                                  uplink_port_cb port_cb,
                                  uplink_ble_cb ble_cb) {
    s_set_cb = set_cb;
    s_port_cb = port_cb;
    s_ble_cb = ble_cb;
}

void uplink_init(const DeviceConfig *cfg) {
    if (!cfg || !cfg->mqtt_enable || cfg->mqtt_broker[0] == '\0') {
        ESP_LOGI(TAG, "MQTT disabled or broker not configured");
        return;
    }

    const char *p = cfg->mqtt_topic_prefix;
    snprintf(s_topic_status,   MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_STATUS);
    snprintf(s_topic_settings, MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_SETTINGS);
    snprintf(s_topic_port_c1,  MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_C1);
    snprintf(s_topic_port_c2,  MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_C2);
    snprintf(s_topic_port_c3,  MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_C3);
    snprintf(s_topic_port_a,   MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_A);
    snprintf(s_topic_set,      MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_SET);
    snprintf(s_topic_port_cmd, MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_CMD);
    snprintf(s_topic_ble,      MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_BLE);

    uint8_t mac[6];
    esp_efuse_mac_get_default(mac);
    char client_id[32];
    snprintf(client_id, sizeof(client_id), "cuktech-ble-%02x%02x%02x%02x",
             mac[2], mac[3], mac[4], mac[5]);

    char uri[128];
    snprintf(uri, sizeof(uri), "mqtt://%s", cfg->mqtt_broker);

    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = uri,
        .broker.address.port = cfg->mqtt_port,
        .credentials.username = cfg->mqtt_user,
        .credentials.authentication.password = cfg->mqtt_pass,
        .credentials.client_id = client_id,
        .buffer.size = 512,          /* inbound commands are tiny */
        .buffer.out_size = 512,      /* largest payload is settings */
        .task.stack_size = 4096,
        .network.reconnect_timeout_ms = 5000,
        .network.timeout_ms = 10000,
    };
    s_client = esp_mqtt_client_init(&mqtt_cfg);
    if (!s_client) {
        ESP_LOGE(TAG, "esp_mqtt_client_init failed");
        return;
    }
    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_client);
    ESP_LOGI(TAG, "MQTT client_id=%s qos=0", client_id);
}
