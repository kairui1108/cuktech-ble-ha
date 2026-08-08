#include <string.h>
#include <stdio.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#include "esp_system.h"
#include "esp_log.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "esp_wifi.h"
#include "esp_coexist.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_mac.h"

#include "config.h"
#include "config_store.h"
#include "http_server.h"
#include "bemfa.h"

#if ENABLE_MQTT
#include "mqtt_client.h"
#include "cJSON.h"
#endif

#include "queue_msg.h"
#include "ble_manager.h"
#include "miot_protocol_shared.h"

static const char* TAG = "MAIN";
static DeviceConfig g_cfg;

// ============================================================
// Queues
// ============================================================
QueueHandle_t cmd_queue = NULL;
QueueHandle_t urgent_queue = NULL;  // higher priority: port commands, user SETs
QueueHandle_t result_queue = NULL;

// Semaphore for delayed BLE connection.
// Created in system_manager, taken by ble_task to wait (timeout = BLE_CONNECT_DELAY_S s).
// Given by handle_ble_control() when frontend user clicks "连接设备".
static SemaphoreHandle_t ble_connect_sem = NULL;
#define BLE_CONNECT_DELAY_S 60

// ============================================================
// Shared state
// ============================================================
static SemaphoreHandle_t state_mutex = NULL;

typedef struct {
    float voltage, current, power;
    uint8_t protocol, status;
    bool active, valid;
} PortData;

static PortData port_data[4] = {};
static uint32_t settings[32] = {};
static bool settings_valid[32] = {};
// Known device defaults (can't GET to read actual values)
static void _init_settings_defaults(void) {
    settings[5] = 1; settings_valid[5] = true;   // 场景模式: 1=AI模式
    settings[6] = 0; settings_valid[6] = true;   // 息屏时间: 0=5分钟
    settings[13] = 1; settings_valid[13] = true;  // 语言: 1=中文
    settings[15] = 0; settings_valid[15] = true;  // USB-A常通电: 0=关闭
    settings[19] = 1; settings_valid[19] = true;  // 空闲息屏: 1=开启
    settings[20] = 1; settings_valid[20] = true;  // 屏幕方向锁: 1=开启
    // PIID 9-12 倒计时默认 0 (无倒计时) — 已经是 0
    for (int i = 9; i <= 12; i++) settings_valid[i] = true;
}
// PIID 21 "all protocols on" default:
// C1(bits 0-3): PD(0)+PPS(1)+UFCS(2)+reserved(3) = 0x0F
// C2(bits 8-11): PD(8)+PPS(9)+UFCS(10)+reserved(11) = 0x0F<<8
// C3(bits 16-17): UFCS(16)+SCP(17) = 0x03<<16
// A (bits 24-25): UFCS(24)+SCP(25) = 0x03<<24
#define PIID21_ALL_ON  0x03030F0F
static uint32_t protocol_extend_val = PIID21_ALL_ON;
static bool protocol_extend_valid = true;
static uint32_t port_ctrl_val = 0xFF;  // assume all ports on (can't GET to read actual)
static bool port_ctrl_valid = true;    // track SET updates
static bool ble_enabled = true;        // BLE connection enable/disable switch
static bool ble_ready_flag = false;
static int mqtt_restart_count = 0;     // MQTT reconnect failure counter (shared with event handler)
static uint64_t last_set16_time = 0;
static uint64_t last_set_time = 0;   // any SET command (used for push/GET debounce during transitions)
static uint8_t last_set_piid = 0;    // piid of last SET — distinguish port control from protocol change
static bool mqtt_connected = false;  // MQTT connection state (check before publish to avoid outbox overflow)

#define LOCK_STATE()   do { if (state_mutex) xSemaphoreTake(state_mutex, portMAX_DELAY); } while(0)
#define UNLOCK_STATE() do { if (state_mutex) xSemaphoreGive(state_mutex); } while(0)

// ============================================================
// Protocol name mapping
// ============================================================
static void publish_port(int idx);
static void publish_settings(void);
static void publish_status(void);
static const char* PROTO_NAMES[] = {"idle","5V","5V","QC","AFC","FCP","SCP","PD","PPS","PPS","UFCS"};
static const int PROTO_NAMES_LEN = sizeof(PROTO_NAMES)/sizeof(PROTO_NAMES[0]);
static const char* get_proto_name(uint8_t code) {
    return (code < PROTO_NAMES_LEN) ? PROTO_NAMES[code] : "?";
}

static uint8_t estimate_protocol(uint8_t piid, float voltage, uint8_t code,
                                  uint32_t caps, bool pd_enabled, uint8_t hw_protocol) {
    /* Hardware protocol code (from PIID 17/18) takes priority. */
    if (hw_protocol > 0) return hw_protocol;

    int idx = piid - 1;
    bool pps_enabled = false;
    uint8_t pdo_kind = 0;
    if (idx == 0 || idx == 1) {
        int pps_bit = (idx == 0) ? 1 : 9;
        pps_enabled = (protocol_extend_val >> pps_bit) & 1;
        if (settings_valid[17]) {
            uint16_t port_word = (idx == 0) ? (settings[17] & 0xFFFF) : ((settings[17] >> 16) & 0xFFFF);
            pdo_kind = (port_word >> 8) & 0xFF;
        }
    } else if (piid == 3) {
        if (settings_valid[18]) {
            pdo_kind = ((settings[18] & 0xFFFF) >> 8) & 0xFF;
        }
    }

    return estimate_protocol_shared(piid, voltage, code, pd_enabled, pps_enabled, pdo_kind, hw_protocol);
}

// ============================================================
// MQTT
// ============================================================
#if ENABLE_MQTT

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
static char _topic_status[MAX_TOPIC_LEN];
static char _topic_settings[MAX_TOPIC_LEN];
static char _topic_port_c1[MAX_TOPIC_LEN], _topic_port_c2[MAX_TOPIC_LEN];
static char _topic_port_c3[MAX_TOPIC_LEN], _topic_port_a[MAX_TOPIC_LEN];
static char _topic_set[MAX_TOPIC_LEN], _topic_port_cmd[MAX_TOPIC_LEN], _topic_ble[MAX_TOPIC_LEN];

static void _init_topics(void) {
    const char *p = g_cfg.mqtt_topic_prefix;
    snprintf(_topic_status,   MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_STATUS);
    snprintf(_topic_settings, MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_SETTINGS);
    snprintf(_topic_port_c1,  MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_C1);
    snprintf(_topic_port_c2,  MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_C2);
    snprintf(_topic_port_c3,  MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_C3);
    snprintf(_topic_port_a,   MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_A);
    snprintf(_topic_set,      MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_SET);
    snprintf(_topic_port_cmd, MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_PORT_CMD);
    snprintf(_topic_ble,      MAX_TOPIC_LEN, "%s%s", p, TOPIC_SUFFIX_BLE);
}

static esp_mqtt_client_handle_t mqtt_client = NULL;
static uint64_t last_mqtt_ok = 0;

static cJSON* get_port_data_json(void) {
    cJSON *arr = cJSON_CreateArray();
    const char *names[] = {"C1", "C2", "C3", "USB-A"};
    LOCK_STATE();
    for (int i = 0; i < 4; i++) {
        cJSON *obj = cJSON_CreateObject();
        cJSON_AddStringToObject(obj, "port", names[i]);
        cJSON_AddNumberToObject(obj, "voltage", port_data[i].voltage);
        cJSON_AddNumberToObject(obj, "current", port_data[i].current);
        cJSON_AddNumberToObject(obj, "power", port_data[i].power);
        const char *proto = port_data[i].active ? get_proto_name(port_data[i].protocol) : "idle";
        cJSON_AddStringToObject(obj, "protocol", proto);
        cJSON_AddBoolToObject(obj, "active", port_data[i].active);
        cJSON_AddNumberToObject(obj, "status", port_data[i].status);
        cJSON_AddItemToArray(arr, obj);
    }
    UNLOCK_STATE();
    return arr;
}

static cJSON* get_settings_json(void) {
    cJSON *root = cJSON_CreateObject();
    LOCK_STATE();
    for (int i = 0; i < 32; i++) {
        if (settings_valid[i]) {
            char key[8]; snprintf(key, sizeof(key), "%d", i);
            cJSON_AddNumberToObject(root, key, settings[i]);
        }
    }
    if (port_ctrl_valid) {
        char key[8]; snprintf(key, sizeof(key), "%d", 16);
        cJSON_AddNumberToObject(root, key, port_ctrl_val);
    }
    if (protocol_extend_valid) {
        char key[8]; snprintf(key, sizeof(key), "%d", 21);
        cJSON_AddNumberToObject(root, key, protocol_extend_val);
    }
    UNLOCK_STATE();
    cJSON_AddBoolToObject(root, "ble_enabled", ble_manager_is_enabled());
    return root;
}

static bool handle_port_control(const char *port, const char *action) {
    if (!port || !action) return false;
    int bit = -1;
    if (strcmp(port, "c1") == 0) bit = 0;
    else if (strcmp(port, "c2") == 0) bit = 1;
    else if (strcmp(port, "c3") == 0) bit = 2;
    else if (strcmp(port, "a") == 0) bit = 3;
    if (bit < 0) return false;
    bool on = (strcmp(action, "on") == 0);
    BleCommand cmd = {CMD_PORT, (uint8_t)bit, on ? 1 : 0, 0};
    if (xQueueSend(urgent_queue, &cmd, pdMS_TO_TICKS(2000)) != pdTRUE) {
        ESP_LOGW(TAG, "HTTP PORT %s %s dropped (queue full)", port, action);
        return false;
    }
    ESP_LOGI(TAG, "HTTP PORT %s %s (bit=%d)", port, action, bit);
    return true;
}

static bool handle_setting_set(int piid, int value) {
    if (piid <= 0 || piid >= 32) return false;
    BleCommand cmd = {CMD_SET, (uint8_t)piid, (uint32_t)value, 0};
    if (xQueueSend(urgent_queue, &cmd, pdMS_TO_TICKS(2000)) != pdTRUE) {
        ESP_LOGW(TAG, "HTTP SET piid=%d dropped (queue full)", piid);
        return false;
    }
    ESP_LOGI(TAG, "HTTP SET piid=%d value=%d", piid, value);
    return true;
}

static bool handle_protocol_toggle(const char *port, const char *protocol, bool on) {
    if (!port || !protocol) return false;
    static const struct { const char *port; const char *proto; int bit; } map[] = {
        {"c1","pd",0},{"c1","pps",1},{"c1","ufcs",2},
        {"c2","pd",8},{"c2","pps",9},{"c2","ufcs",10},
        {"c3","ufcs",16},{"c3","scp",17},
        {"a","ufcs",24},{"a","scp",25},
    };
    for (int i = 0; i < sizeof(map)/sizeof(map[0]); i++) {
        if (strcmp(port, map[i].port) == 0 && strcasecmp(protocol, map[i].proto) == 0) {
            uint32_t val = protocol_extend_val;
            if (on) val |= (1 << map[i].bit);
            else val &= ~(1 << map[i].bit);
            BleCommand cmd = {CMD_SET, 21, val, 0};
            if (xQueueSend(urgent_queue, &cmd, pdMS_TO_TICKS(2000)) != pdTRUE) {
                ESP_LOGW(TAG, "HTTP PROTO %s %s dropped (queue full)", port, protocol);
                return false;
            }
            ESP_LOGI(TAG, "HTTP PROTO %s %s %s (PIID21=0x%lX)", port, protocol, on?"ON":"OFF", (unsigned long)val);
            return true;
        }
    }
    return false;
}

static bool handle_ble_control(bool enable) {
    ble_enabled = enable;
    ble_manager_set_enabled(enable);
    if (enable && ble_connect_sem) {
        /* Give the semaphore to wake up ble_task early (if it's still
           in the delayed-start window). Safe to call multiple times. */
        xSemaphoreGive(ble_connect_sem);
    }
    publish_status();
    ESP_LOGI(TAG, "HTTP BLE %s", enable ? "enable" : "disable");
    return true;
}

static void publish_port(int idx) {
    if (!mqtt_client || !mqtt_connected) return;
    LOCK_STATE();
    if (!port_data[idx].valid) { UNLOCK_STATE(); return; }
    float v = port_data[idx].voltage;
    float c = port_data[idx].current;
    float p = port_data[idx].power;
    bool a = port_data[idx].active;
    const char* proto = a ? get_proto_name(port_data[idx].protocol) : "idle";
    const char* names[] = {"C1","C2","C3","A"};
    ESP_LOGI(TAG, "publish %s: v=%.1f c=%.1f p=%.1f active=%s",
             names[idx], v, c, p, a ? "Y" : "N");
    UNLOCK_STATE();

    char payload[256];
    snprintf(payload, sizeof(payload),
             "{\"voltage\":%.1f,\"current\":%.1f,\"power\":%.1f,\"active\":%s,\"protocol\":\"%s\"}",
             v, c, p, a ? "true" : "false", proto);
    const char* topics[] = {_topic_port_c1, _topic_port_c2, _topic_port_c3, _topic_port_a};
    int pub_rc = esp_mqtt_client_publish(mqtt_client, topics[idx], payload, 0, 1, 1);
    if (pub_rc >= 0) last_mqtt_ok = esp_timer_get_time() / 1000;
}

static void publish_settings(void) {
    if (!mqtt_client || !mqtt_connected) return;
    LOCK_STATE();
    char payload[512];
    int pos = snprintf(payload, sizeof(payload), "{");
    // Publish all known settings (aligned with Python ble_manager.py _fetch_settings)
    const uint8_t known[] = {5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21};
    bool first = true;
    for (int i = 0; i < sizeof(known); i++) {
        uint8_t piid = known[i];
        // Always publish PIID 16 (port control) and PIID 21 (protocol extend) —
        // use tracked values even before first GET/SET
        bool publish_this = (piid == 16) || (piid == 21) || (piid < 32 && settings_valid[piid]);
        if (publish_this) {
            int room = sizeof(payload) - pos;
            if (room < 20) break;
            uint32_t val;
            if (piid == 16) val = port_ctrl_val;
            else if (piid == 21) val = protocol_extend_val;
            else val = settings[piid];
            pos += snprintf(payload + pos, room,
                            "%s\"%u\":%lu", first ? "" : ",", piid,
                            (unsigned long)val);
            first = false;
        }
    }
    snprintf(payload + pos, sizeof(payload) - pos, "}");
    UNLOCK_STATE();
    esp_mqtt_client_publish(mqtt_client, _topic_settings, payload, 0, 1, 1);
    last_mqtt_ok = esp_timer_get_time() / 1000;
}

static void publish_status(void) {
    if (!mqtt_client || !mqtt_connected) return;
    LOCK_STATE();
    bool ready = ble_ready_flag;
    UNLOCK_STATE();
    char payload[256];
    snprintf(payload, sizeof(payload),
             "{\"connected\":%s,\"authenticated\":%s,\"ble_enabled\":%s,\"device_model\":\"njcuk.fitting.ad1204\",\"firmware_version\":\"esp32-v1.0\"}",
             ready ? "true" : "false", ready ? "true" : "false", ble_enabled ? "true" : "false");
    esp_mqtt_client_publish(mqtt_client, _topic_status, payload, 0, 1, 1);
    last_mqtt_ok = esp_timer_get_time() / 1000;
}

static void mqtt_event_handler(void* arg, esp_event_base_t base, int32_t id, void* data) {
    esp_mqtt_event_handle_t event = data;
    switch (event->event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT connected");
        mqtt_connected = true;
        last_mqtt_ok = esp_timer_get_time() / 1000;
        mqtt_restart_count = 0;  /* reset reconnection watchdog */
        esp_mqtt_client_subscribe(mqtt_client, _topic_set, 1);
        esp_mqtt_client_subscribe(mqtt_client, _topic_port_cmd, 1);
        esp_mqtt_client_subscribe(mqtt_client, _topic_ble, 1);
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT disconnected");
        mqtt_connected = false;
        break;
    case MQTT_EVENT_DATA: {
        // Parse topic to determine command type
        char topic_buf[128] = {0};
        int tlen = event->topic_len < sizeof(topic_buf) - 1 ? event->topic_len : sizeof(topic_buf) - 1;
        memcpy(topic_buf, event->topic, tlen);

        if (strstr(topic_buf, "/set")) {
            // PIID value SET command: {"piid":N, "value":V}
            // Parse from event->data (JSON)
            cJSON *json = cJSON_ParseWithLength(event->data, event->data_len);
            if (json) {
                cJSON *piid_item = cJSON_GetObjectItem(json, "piid");
                cJSON *val_item = cJSON_GetObjectItem(json, "value");
                if (piid_item && val_item) {
                    uint8_t piid = (uint8_t)cJSON_GetNumberValue(piid_item);
                    uint32_t val = (uint32_t)cJSON_GetNumberValue(val_item);
                    if (piid > 0) {
                        BleCommand cmd = {CMD_SET, piid, val, 0};
                        if (xQueueSend(urgent_queue, &cmd, pdMS_TO_TICKS(2000)) != pdTRUE) {
                            ESP_LOGW(TAG, "MQTT SET piid=%d dropped (queue full)", piid);
                        }
                        ESP_LOGI(TAG, "MQTT SET piid=%d val=%lu", piid, (unsigned long)val);
                    }
                }
                cJSON_Delete(json);
            }
        } else if (strstr(topic_buf, "/port")) {
            // Port command: {"port":"c1", "action":"on"/"off"}
            cJSON *json = cJSON_ParseWithLength(event->data, event->data_len);
            if (json) {
                cJSON *port_item = cJSON_GetObjectItem(json, "port");
                cJSON *action_item = cJSON_GetObjectItem(json, "action");
                if (port_item && action_item && cJSON_IsString(port_item) && cJSON_IsString(action_item)) {
                    const char *pname = cJSON_GetStringValue(port_item);
                    const char *action = cJSON_GetStringValue(action_item);
                    int bit = -1;
                    if      (strcmp(pname, "c1") == 0) bit = 0;
                    else if (strcmp(pname, "c2") == 0) bit = 1;
                    else if (strcmp(pname, "c3") == 0) bit = 2;
                    else if (strcmp(pname, "a")  == 0) bit = 3;
                    if (bit >= 0) {
                        bool on = (strcmp(action, "on") == 0);
                        BleCommand cmd = {CMD_PORT, (uint8_t)bit, on ? 1 : 0, 0};
                        if (xQueueSend(urgent_queue, &cmd, pdMS_TO_TICKS(2000)) != pdTRUE) {
                            ESP_LOGW(TAG, "MQTT PORT %s %s dropped (queue full)", pname, action);
                        }
                    }
                }
                cJSON_Delete(json);
            }
        } else if (strstr(topic_buf, "/ble")) {
            cJSON *json = cJSON_ParseWithLength(event->data, event->data_len);
            if (json) {
                cJSON *en_item = cJSON_GetObjectItem(json, "enabled");
                if (en_item && cJSON_IsBool(en_item)) {
                    bool want = cJSON_IsTrue(en_item);
                    if (want != ble_enabled) {
                        ble_enabled = want;
                        ble_manager_set_enabled(want);
                        ESP_LOGI(TAG, "BLE %s by MQTT", want ? "enabled" : "disabled");
                        publish_status();
                    }
                }
                cJSON_Delete(json);
            }
        }
        break;
    }
    default: break;
    }
}

static void mqtt_init(void) {
    if (!g_cfg.mqtt_enable || g_cfg.mqtt_broker[0] == '\0') {
        ESP_LOGI(TAG, "MQTT disabled or broker not configured");
        return;
    }

    // Use chip MAC as unique client ID suffix to avoid conflicts with Python server
    uint8_t mac[6];
    esp_efuse_mac_get_default(mac);
    char client_id[32];
    snprintf(client_id, sizeof(client_id), "cuktech-ble-%02x%02x%02x%02x",
             mac[2], mac[3], mac[4], mac[5]);

    char uri[128];
    snprintf(uri, sizeof(uri), "mqtt://%s", g_cfg.mqtt_broker);

    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = uri,
        .broker.address.port = g_cfg.mqtt_port,
        .credentials.username = g_cfg.mqtt_user,
        .credentials.authentication.password = g_cfg.mqtt_pass,
        .credentials.client_id = client_id,
        .buffer.out_size = 512,
        .task.stack_size = 3072,
    };
    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(mqtt_client);
    ESP_LOGI(TAG, "MQTT client_id=%s", client_id);
}

#else

static void publish_port(int idx) { (void)idx; }
static void publish_settings(void) {}
static void publish_status(void) {}
static void mqtt_init(void) {}

#endif

// ============================================================
// ============================================================
// System Manager — event-driven startup state machine
// ============================================================

static int wifi_retry_count = 0;
#define SYS_EVT_WIFI_READY   BIT0
#define SYS_EVT_BLE_READY    BIT1

static EventGroupHandle_t sys_events;  // system-level event flags

/* Stages of the system startup lifecycle */
enum sys_stage {
    STAGE_IDLE,
    STAGE_WIFI,
    STAGE_HTTP,
    STAGE_BLE,
    STAGE_MQTT,
    STAGE_RUNNING,
};
static enum sys_stage _stage = STAGE_IDLE;

static const char *stage_name(enum sys_stage s) {
    switch (s) {
        case STAGE_IDLE:    return "IDLE";
        case STAGE_WIFI:    return "WIFI";
        case STAGE_HTTP:    return "HTTP";
        case STAGE_BLE:     return "BLE";
        case STAGE_MQTT:    return "MQTT";
        case STAGE_RUNNING: return "RUNNING";
        default:            return "?";
    }
}

static void advance_stage(void) {
    ESP_LOGI(TAG, "=== Stage: %s → %s ===",
             stage_name(_stage), stage_name(_stage + 1));
    _stage++;
}

/* WiFi Manager — pure event passing, no blocking in callbacks */
static void wifi_event_handler(void* arg, esp_event_base_t base, int32_t id, void* data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_retry_count++;
        ESP_LOGW(TAG, "WiFi disconnected (reason=%d), retry (attempt %d)",
                 ((wifi_event_sta_disconnected_t*)data)->reason, wifi_retry_count);
        xEventGroupClearBits(sys_events, SYS_EVT_WIFI_READY);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        wifi_retry_count = 0;
        ip_event_got_ip_t* event = (ip_event_got_ip_t*)data;
        ESP_LOGI(TAG, "WiFi got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(sys_events, SYS_EVT_WIFI_READY);
    }
}

/* Dedicated reconnect task — never blocks the Event Loop */
static void wifi_reconnect_task(void *arg) {
    while (1) {
        /* Wait for disconnect with both bits cleared */
        xEventGroupWaitBits(sys_events, SYS_EVT_WIFI_READY,
                            pdFALSE, pdFALSE, portMAX_DELAY);
        vTaskDelay(pdMS_TO_TICKS(100));  /* debounce */
        /* WIFI_READY is clear → we are disconnected → reconnect */
        if (!(xEventGroupGetBits(sys_events) & SYS_EVT_WIFI_READY)) {
            int d = (wifi_retry_count < 5) ? (1000 * (1 << wifi_retry_count)) : 30000;
            if (d > 30000) d = 30000;
            vTaskDelay(pdMS_TO_TICKS(d));
            esp_wifi_connect();
        }
    }
}

// ============================================================
// BLE callbacks
// ============================================================

static void on_ble_state_change(BLEState old_state, BLEState new_state) {
    // Notify app_task immediately on state changes (especially disconnect).
    // Use a short timeout to avoid deadlocking ble_task if app_task is stuck.
    BleResult res = {RES_BLE_STATUS, true, 0, (uint32_t)(new_state == BLE_READY), 0};
    if (xQueueSend(result_queue, &res, pdMS_TO_TICKS(500)) != pdTRUE) {
        ESP_LOGW(TAG, "BLE state change dropped (queue full)");
    }
}

// Port data callback unused — _parse_port sends RES_PORT_PUSH directly
// Keep registration with NULL in ble_task
// static void on_port_data(int piid) { }

// ============================================================
// BLE task — runs NimBLE event processing + BLE state machine
// ============================================================

static void ble_task(void *pvParameters) {
    ESP_LOGI(TAG, "BLE task started");

    /* Delayed BLE connection: wait up to BLE_CONNECT_DELAY_S for the
       semaphore, or until the frontend user clicks "连接设备" to give
       the semaphore early. This lets WiFi settle and avoids BLE radio
       contention during initial page loads / image transfers. */
    if (ble_connect_sem) {
        TickType_t timeout = pdMS_TO_TICKS(BLE_CONNECT_DELAY_S * 1000);
        if (xSemaphoreTake(ble_connect_sem, timeout) == pdTRUE) {
            ESP_LOGI(TAG, "BLE connect triggered early by frontend");
        } else {
            ESP_LOGI(TAG, "BLE connect delay expired (%ds), starting now", BLE_CONNECT_DELAY_S);
        }
    }

    ble_manager_set_state_callback(on_ble_state_change);
    ble_manager_set_port_data_callback(NULL);
    ble_manager_init(g_cfg.device_mac, g_cfg.device_token, g_cfg.device_ble_key);

    BleCommand cmd;
    BleResult res;
    bool was_ready = false;
    // NOTE: Device does NOT support MiOT GET commands.
    // Port data comes via push notifications only.
    // Settings can only be changed via SET, not read via GET.
    // Internal poll is disabled to prevent pending table overflow.

    while (1) {
        // Drain urgent queue (CMD_PORT from MQTT) and cmd_queue (rare external GETs).
        // Limit to 8 commands per iteration to bound time between ble_manager_loop()
        // (needed for keepalive and notification processing).
        bool did_work = false;
        int drain_cnt = 0;
        do {
            cmd.type = CMD_NOP;
            if (urgent_queue && xQueueReceive(urgent_queue, &cmd, 0) == pdTRUE) {
                did_work = true;
            } else if (xQueueReceive(cmd_queue, &cmd, 0) == pdTRUE) {
                did_work = true;
            }
            if (cmd.type != CMD_NOP) {
                drain_cnt++;
                switch (cmd.type) {
                case CMD_GET: {
                    uint32_t val = 0;
                    bool ok = ble_manager_miot_get(cmd.piid, &val);
                    if (ok) {
                        LOCK_STATE();
                        if (cmd.piid < 32) { settings[cmd.piid] = val; settings_valid[cmd.piid] = true; }
                        if (cmd.piid == 16) { port_ctrl_val = val; port_ctrl_valid = true; }
                        if (cmd.piid == 21) { protocol_extend_val = val; ble_manager_store_setting(21, val); }
                        UNLOCK_STATE();
                        publish_settings();
                        ESP_LOGI(TAG, "GET piid=%d = %lu", cmd.piid, (unsigned long)val);
                    } else {
                        ESP_LOGW(TAG, "GET piid=%d FAILED", cmd.piid);
                    }
                    ble_manager_loop();
                    break;
                }
                case CMD_SET: {
                    ESP_LOGI(TAG, "SET piid=%d val=%lu", cmd.piid, (unsigned long)cmd.value);
                    bool ok = ble_manager_miot_set(cmd.piid, cmd.value);
                    if (ok) {
                        LOCK_STATE();
                        if (cmd.piid == 16) { port_ctrl_val = cmd.value; port_ctrl_valid = true; }
                        else if (cmd.piid < 32) { settings[cmd.piid] = cmd.value; settings_valid[cmd.piid] = true; }
                        if (cmd.piid == 21) { protocol_extend_val = cmd.value; ble_manager_store_setting(21, cmd.value); }
                        UNLOCK_STATE();
                        publish_settings();
                        ESP_LOGI(TAG, "SET piid=%d val=%lu OK", cmd.piid, (unsigned long)cmd.value);
                    } else {
                        ESP_LOGW(TAG, "SET piid=%d val=%lu FAILED", cmd.piid, (unsigned long)cmd.value);
                    }
                    res = (BleResult){RES_SET, ok, cmd.piid, cmd.value, 0};
                    xQueueSend(result_queue, &res, 0);
                    break;
                }
                case CMD_PORT: {
                    LOCK_STATE();
                    uint32_t current = port_ctrl_val;
                    ESP_LOGI(TAG, "CMD_PORT bit=%d %s: current=0x%02lX", cmd.piid, cmd.value ? "ON" : "OFF", (unsigned long)current);
                    if (cmd.value) current |= (1 << cmd.piid);
                    else current &= ~(1 << cmd.piid);
                    port_ctrl_val = current;
                    port_ctrl_valid = true;
                    UNLOCK_STATE();
                    if (ble_manager_miot_set(16, current)) {
                        ESP_LOGI(TAG, "CMD_PORT: SET16=0x%02lX sent", (unsigned long)current);
                    } else {
                        ESP_LOGW(TAG, "CMD_PORT: SET16=0x%02lX FAILED", (unsigned long)current);
                        if (!ble_manager_is_ready()) {
                            ESP_LOGW(TAG, "BLE not connected, aborting port control");
                            cmd.type = CMD_NOP;  // exit command drain loop
                            break;
                        }
                    }
                    res = (BleResult){RES_SET, true, 16, current, 0};
                    xQueueSend(result_queue, &res, 0);
                    last_set16_time = esp_timer_get_time() / 1000;
                    break;
                }
                case CMD_RECONNECT:
                case CMD_DISCONNECT:
                    ble_manager_disconnect();
                    break;
                default: break;
                }
            }
        } while (cmd.type != CMD_NOP && drain_cnt < 8);

        // BLE state machine & response processing
        ble_manager_loop();

        // State change notification
        {
            bool ready = ble_manager_is_ready();
            if (ready != was_ready) {
                was_ready = ready;
                res = (BleResult){RES_BLE_STATUS, true, 0, (uint32_t)ready, 0};
                xQueueSend(result_queue, &res, portMAX_DELAY);
            }
        }

        // Auto BLE reconnect with exponential backoff:
        //  1st retry after 10s, then 20s, 40s, 80s, ... capped at 300s.
        //  Resets to 10s when BLE reconnects successfully.
        {
            static uint64_t last_ble_connected = 0;
            static unsigned backoff_sec = 10;
            uint64_t now = esp_timer_get_time() / 1000;
            bool ready = ble_manager_is_ready();
            if (ready) {
                last_ble_connected = now;
                backoff_sec = 10;  // reset on success
            } else if (ble_enabled && last_ble_connected > 0 &&
                       (now - last_ble_connected >= (uint64_t)backoff_sec * 1000)) {
                ESP_LOGI(TAG, "BLE auto-reconnect: restarting scanner (backoff=%us)...", backoff_sec);
                last_ble_connected = now;
                backoff_sec = (backoff_sec >= 150) ? 300 : (backoff_sec * 2);
                ble_manager_set_enabled(false);
                /* Wait for disconnect to fully settle before re-enabling
                   (avoid racing with in-flight GAP disconnect event). */
                uint32_t settle_ms = 500;
                do {
                    vTaskDelay(pdMS_TO_TICKS(50));
                    settle_ms -= 50;
                } while (!ble_manager_is_idle() && settle_ms > 0);
                ble_manager_set_enabled(true);
            }
        }

        // Only yield if truly idle
        if (!did_work) vTaskDelay(pdMS_TO_TICKS(20));
    }
}

// ============================================================
// App task
// ============================================================

static void app_task(void* pvParameters) {
    vTaskDelay(pdMS_TO_TICKS(100));
    uint64_t last_mem_print = 0;
    uint64_t last_status_print = 0;
    uint64_t last_cd_fetch_slow = 0;
    uint64_t last_cd_fetch_fast = 0;
    uint64_t last_mqtt_restart = 0;
    uint64_t boot_time = esp_timer_get_time() / 1000;  /* for periodic auto-reboot */

    while (1) {
        uint64_t now = esp_timer_get_time() / 1000;

        // Periodic auto-reboot (if configured)
        uint32_t rbi = g_cfg.reboot_interval_sec;
        if (rbi > 0 && (now - boot_time) >= (uint64_t)rbi * 1000) {
            ESP_LOGW(TAG, "Periodic reboot: interval=%us reached (uptime=%us)",
                     (unsigned)rbi, (unsigned)((now - boot_time) / 1000));
            vTaskDelay(pdMS_TO_TICKS(100));
            esp_restart();
        }

        // Memory monitor + GC every 10 seconds
        if (now - last_mem_print >= 10000) {
            static uint8_t _frag_warn_count = 0;
            last_mem_print = now;
            size_t _free = esp_get_free_heap_size();
            size_t _largest = heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
            /* Stack watermarks: < 512 bytes remaining → risk of overflow */
            UBaseType_t app_hwm  = uxTaskGetStackHighWaterMark(NULL); /* own task */
            unsigned app_pct = (unsigned)((app_hwm * 100) / 7168);
            ESP_LOGI(TAG, "MEM: free=%u min=%u max_block=%u | stack: app=%u/%u (%u%%)",
                     (unsigned)_free,
                     (unsigned)esp_get_minimum_free_heap_size(),
                     (unsigned)_largest,
                     (unsigned)app_hwm, 7168, app_pct);
            if (app_hwm < 1024) {
                ESP_LOGW(TAG, "APP task stack low: %u bytes remaining!", (unsigned)app_hwm);
            }
            if (_largest < _free / 2) {
                _frag_warn_count++;
                ESP_LOGW(TAG, "Fragmentation [%d]: max_block=%u < free/2 (%u) — triggering GC",
                         _frag_warn_count, (unsigned)_largest, (unsigned)_free / 2);
                /* Active GC: reset cJSON pool, log detailed caps breakdown */
                size_t pool_peak = http_server_pool_gc();
                multi_heap_info_t info;
                heap_caps_get_info(&info, MALLOC_CAP_8BIT);
                ESP_LOGI(TAG, "GC: pool_peak=%u caps_8bit: free=%u min_free=%u largest=%u total_alloc=%u",
                         (unsigned)pool_peak,
                         (unsigned)info.total_free_bytes,
                         (unsigned)info.minimum_free_bytes,
                         (unsigned)info.largest_free_block,
                         (unsigned)info.total_allocated_bytes);
                /* If fragmentation persists for >5 min (30×10s intervals),
                   reset the chip to recover a clean heap. */
                if (_frag_warn_count >= 30) {
                    ESP_LOGE(TAG, "Fragmentation persisted 5+ min — rebooting to recover heap");
                    vTaskDelay(pdMS_TO_TICKS(100));
                    esp_restart();
                }
            } else {
                _frag_warn_count = 0;  /* fragmentation resolved */
            }
        }

#if ENABLE_MQTT
        // MQTT reconnection watchdog: if disconnected >2min since last
        // successful publish, try reconnecting. After 3 failed attempts,
        // force WiFi reset to purge corrupted LWIP state (the "Host is
        // unreachable" case where WiFi is associated but TCP stack is dead).
        if (mqtt_client && !mqtt_connected &&
            last_mqtt_ok > 0 &&
            (now - last_mqtt_ok > 120000) &&
            (now - last_mqtt_restart >= 30000)) {
            last_mqtt_restart = now;
            mqtt_restart_count++;
            if (mqtt_restart_count > 3) {
                ESP_LOGW(TAG, "MQTT reconnect failed 3x, forcing WiFi reset");
                mqtt_restart_count = 0;
                esp_wifi_disconnect();
                vTaskDelay(pdMS_TO_TICKS(2000));
                esp_wifi_connect();
            } else {
                ESP_LOGI(TAG, "MQTT disconnected %ds, reconnecting (%d/3)",
                         (int)((now - last_mqtt_ok) / 1000), mqtt_restart_count);
                esp_mqtt_client_reconnect(mqtt_client);
            }
        }
#endif

        // Process BLE results
        BleResult res;
        while (xQueueReceive(result_queue, &res, 0) == pdTRUE) {
            switch (res.type) {
            case RES_PORT_PUSH: {
                int idx = res.piid - 1;
                if (idx >= 0 && idx < 4) {
                    LOCK_STATE();
                    port_data[idx] = (PortData){
                        res.voltage, res.current, res.power,
                        res.protocol, res.status,
                        res.status != 0 || res.voltage > 0.5f, true
                    };
                    bool a = port_data[idx].active;
                    UNLOCK_STATE();
                    publish_port(idx);
                    // Always update Bemfa state regardless of main MQTT
                    bemfa_publish_port(idx, res.voltage, res.current, res.power, a);
                }
                break;
            }
            case RES_GET: {
                LOCK_STATE();
                if (res.piid >= 1 && res.piid <= 4) {
                    int idx = res.piid - 1;
                    float v = ((res.value >> 24) & 0xFF) / 10.0f;
                    float c = ((res.value >> 16) & 0xFF) / 10.0f;
                    uint8_t code = (res.value >> 8) & 0xFF;
                    uint8_t st = res.value & 0xFF;
                    bool pd_on = true;
                    if (idx < 2) {
                        int bit = (idx == 0) ? 0 : 8;
                        pd_on = (protocol_extend_val >> bit) & 1;
                    }
                    // Extract hardware protocol code from PIID 17/18 if available
                    uint8_t hw_proto = 0;
                    if (settings_valid[17] || settings_valid[18]) {
                        switch (res.piid) {
                            case 1: if (settings_valid[17]) hw_proto = (settings[17] >> 24) & 0xFF; break;
                            case 2: if (settings_valid[17]) hw_proto = (settings[17] >> 8) & 0xFF; break;
                            case 3: if (settings_valid[18]) hw_proto = (settings[18] >> 24) & 0xFF; break;
                            case 4: if (settings_valid[18]) hw_proto = (settings[18] >> 8) & 0xFF; break;
                        }
                    }
                    uint8_t proto = estimate_protocol(res.piid, v, code, 0, pd_on, hw_proto);
                    bool was_active = port_data[idx].active;
                    // Debounce: skip zero-value GET if port was previously active
                    // and we're within 500ms of a non-port-control SET (protocol transitions).
                    uint64_t now = esp_timer_get_time() / 1000;
                    bool nearly_zero = (v < 0.1f && c < 0.1f && st == 0);
                    if (nearly_zero && port_data[idx].valid && port_data[idx].active && last_set_piid != 16) {
                        if (now - last_set_time < 500) {
                            UNLOCK_STATE();
                            break;
                        }
                    }
                    port_data[idx] = (PortData){v, c, v*c, proto, st, st!=0||v>0.5f, true};
                    bool publish = false;
                    if (was_active != port_data[idx].active) publish = true;
                    if (!port_data[idx].valid) publish = true;
                    if (now - last_set_time < 500 && (last_set_piid == 16 || last_set_piid == 21)) {
                        publish = true;
                    }
                    UNLOCK_STATE();
                    if (publish) publish_port(idx);
                } else {
                    // Settings: update and publish (only on success)
                    if (res.success) {
                        if (res.piid == 16) {
                            // Ignore stale GET(16)=0 for 3s after a SET
                            if (res.value == 0 && port_ctrl_val != 0 &&
                                (esp_timer_get_time() / 1000 - last_set16_time) < 3000) {
                                // stale — keep previous value
                            } else {
                                settings[16] = res.value;
                                port_ctrl_val = res.value;
                                port_ctrl_valid = true;
                            }
                            if (!settings_valid[16]) settings_valid[16] = true;
                        } else if (res.piid < 32) {
                            settings[res.piid] = res.value;
                            settings_valid[res.piid] = true;
                        }
                        if (res.piid == 21) { protocol_extend_val = res.value; ble_manager_store_setting(21, res.value); }
                    }
                    UNLOCK_STATE();
                    if (res.success) publish_settings();
                }
                break;
            }
            case RES_SET: {
                LOCK_STATE();
                if (res.success) {
                    if (res.piid == 16) { port_ctrl_val = res.value; port_ctrl_valid = true; }
                    else if (res.piid < 32) {
                        settings[res.piid] = res.value;
                        settings_valid[res.piid] = true;
                    }
                    if (res.piid == 21) protocol_extend_val = res.value;
                }
                UNLOCK_STATE();
                if (res.success) {
                    last_set_time = esp_timer_get_time() / 1000;
                    last_set_piid = res.piid;
                    publish_settings();
                    // After port control (16) or protocol change (21), queue port GETs
                    // to cmd_queue (not urgent_queue) — they'll be interleaved with polling.
                    if (res.piid == 16 || res.piid == 21) {
                        // Port data arrives via push notification, no need to GET
                    }
                }
                break;
            }
            case RES_BLE_STATUS: {
                LOCK_STATE();
                ble_ready_flag = res.value != 0;
                UNLOCK_STATE();
                publish_status();
                // Always update Bemfa BLE state regardless of main MQTT
                bemfa_publish_status(res.value != 0);
                if (res.value) {
                    // Fetch settings on initial BLE connect so frontend shows
                    // actual values instead of hardcoded defaults, and protocol
                    // detection uses real hardware protocol codes (PIID 17/18).
                    static const uint8_t INIT_PIIDS[] = {5, 6, 15, 21, 17, 18};
                    for (int i = 0; i < sizeof(INIT_PIIDS); i++) {
                        BleCommand c = {CMD_GET, INIT_PIIDS[i], 0, 0};
                        xQueueSend(cmd_queue, &c, 0);
                    }
                }
                break;
            }
            default: break;
            }
        }

        // Slow poll (90s): scene mode, screen time, countdown, trickle
        if (ble_ready_flag && (now - last_cd_fetch_slow >= 90000)) {
            last_cd_fetch_slow = now;
            static const uint8_t SLOW_PIIDS[] = {5, 6, 9, 10, 11, 12, 15};
            for (int i = 0; i < sizeof(SLOW_PIIDS); i++) {
                BleCommand c = {CMD_GET, SLOW_PIIDS[i], 0, 0};
                xQueueSend(cmd_queue, &c, 0);
            }
        }
        // Fast poll (20s): port control (16) and protocol extends (21) change via SET
        if (ble_ready_flag && (now - last_cd_fetch_fast >= 20000)) {
            last_cd_fetch_fast = now;
            static const uint8_t FAST_PIIDS[] = {16, 21};
            for (int i = 0; i < sizeof(FAST_PIIDS); i++) {
                BleCommand c = {CMD_GET, FAST_PIIDS[i], 0, 0};
                xQueueSend(cmd_queue, &c, 0);
            }
        }

        // Status print every 10s
        if (now - last_status_print >= 10000) {
            int pending = ble_manager_pending_count();
            wifi_ap_record_t ap_info;
            int rssi = -100;
            if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) rssi = ap_info.rssi;
            ESP_LOGI(TAG, "Status: BLE=%s pend=%d WiFi=%ddBm Uptime=%ds",
                     ble_ready_flag ? "CONNECTED" : "DISCONNECTED",
                     pending, rssi, (int)(now / 1000));
            last_status_print = now;
        }

        // Bemfa keepalive
        bemfa_loop();

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

// ============================================================
// AP Mode (first-time config)
// ============================================================

static void _reboot_task(void *arg) {
    vTaskDelay(pdMS_TO_TICKS(3000));
    ESP_LOGW(TAG, "Rebooting now...");
    esp_restart();
}

static void ap_reboot_callback(void) {
    ESP_LOGI(TAG, "Config saved, scheduling reboot in 3s...");
    xTaskCreate(_reboot_task, "reboot", 2048, NULL, 1, NULL);
}

static void enter_ap_mode(bool net_init_done) {
    ESP_LOGW(TAG, "No config found — entering AP mode for setup");
    ESP_LOGW(TAG, "Connect to WiFi: %s  Password: %s", AP_SSID, AP_PASSWORD);
    ESP_LOGW(TAG, "Then open: http://192.168.4.1/");

    if (!net_init_done) {
        ESP_ERROR_CHECK(esp_netif_init());
        ESP_ERROR_CHECK(esp_event_loop_create_default());
        esp_netif_create_default_wifi_ap();
        wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
        ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    } else {
        // WiFi already initialized by system_manager — just create AP netif
        esp_netif_create_default_wifi_ap();
    }

    wifi_config_t ap_config = {
        .ap = {
            .ssid = AP_SSID,
            .ssid_len = sizeof(AP_SSID) - 1,
            .password = AP_PASSWORD,
            .max_connection = 4,
            .authmode = WIFI_AUTH_WPA2_PSK,
            .channel = 6,  // 避开最拥挤的 channel 1
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    /* Must be called after wifi_start */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_inactive_time(WIFI_IF_AP, 600));

    ESP_LOGI(TAG, "AP started");

    config_store_apply_defaults(&g_cfg);
    http_server_start(&g_cfg, ap_reboot_callback);

    // Block forever — HTTP callback will reboot
    while (1) vTaskDelay(pdMS_TO_TICKS(1000));
}

// ============================================================
// System Manager — startup coordinator
// ============================================================

static void system_manager(void) {
    sys_events = xEventGroupCreate();

    /* ---- Stage: WiFi ---- */
    advance_stage(); // STAGE_WIFI

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wcfg));

    esp_event_handler_instance_t e_any, e_gotip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                    &wifi_event_handler, NULL, &e_any));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                    &wifi_event_handler, NULL, &e_gotip));

    wifi_config_t wifi_sta = {
        .sta = {.ssid = "", .password = ""},
    };
    strncpy((char*)wifi_sta.sta.ssid, g_cfg.wifi_ssid, sizeof(wifi_sta.sta.ssid)-1);
    strncpy((char*)wifi_sta.sta.password, g_cfg.wifi_pass, sizeof(wifi_sta.sta.password)-1);
    ESP_LOGI(TAG, "WiFi SSID='%s' (len=%d) PASS len=%d",
             g_cfg.wifi_ssid, strlen(g_cfg.wifi_ssid), strlen(g_cfg.wifi_pass));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_sta));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    /* Coexistence: balance BLE and WiFi */
    esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "WiFi connecting to %s...", g_cfg.wifi_ssid);
    EventBits_t bits = xEventGroupWaitBits(sys_events, SYS_EVT_WIFI_READY,
                                            pdFALSE, pdTRUE, pdMS_TO_TICKS(30000));
    if (!(bits & SYS_EVT_WIFI_READY)) {
        ESP_LOGW(TAG, "WiFi failed to connect within 30s — proceeding in AP mode");
        enter_ap_mode(true);  // netif/event_loop already initialized
        return;
    }
    /* Start reconnect task only AFTER first successful connection */
    xTaskCreatePinnedToCore(wifi_reconnect_task, "wifi_reconn", 2048, NULL, 3, NULL, 0);

    /* ---- Stage: HTTP ---- */
    advance_stage(); // STAGE_HTTP
    _init_topics();
    _init_settings_defaults();
    ble_manager_store_setting(16, port_ctrl_val);
    ble_manager_store_setting(21, protocol_extend_val);

    cmd_queue = xQueueCreate(20, sizeof(BleCommand));
    urgent_queue = xQueueCreate(4, sizeof(BleCommand));
    result_queue = xQueueCreate(48, sizeof(BleResult));
    state_mutex = xSemaphoreCreateMutex();

    http_server_set_callbacks(get_port_data_json, get_settings_json,
                              handle_port_control, handle_setting_set,
                              handle_protocol_toggle,
                              handle_ble_control);
    http_server_start(&g_cfg, ap_reboot_callback);
    ESP_LOGI(TAG, "HTTP server ready");

    /* Create semaphore for delayed BLE connection. ble_task will wait
       on this semaphore; handle_ble_control can give it to wake up early. */
    ble_connect_sem = xSemaphoreCreateBinary();

    /* ---- Stage: BLE ---- */
    advance_stage(); // STAGE_BLE
#if CONFIG_FREERTOS_UNICORE
    /* Single-core (ESP32-C3/H2): no core-1 to pin to */
    xTaskCreate(ble_task, "ble", 16384, NULL, 2, NULL);
#else
    /* Dual-core (ESP32/S3): pin BLE to core 1 (NimBLE controller) */
    xTaskCreatePinnedToCore(ble_task, "ble", 16384, NULL, 2, NULL, 1);
#endif
    xTaskCreatePinnedToCore(app_task,  "app", 7168,  NULL, 1, NULL, 0);

    /* ---- Stage: MQTT + Bemfa ---- */
    advance_stage(); // STAGE_MQTT
    vTaskDelay(pdMS_TO_TICKS(2000));  // brief settle before MQTT dials out
    mqtt_init();
    bemfa_init(&g_cfg, handle_port_control, handle_ble_control);

    advance_stage(); // STAGE_RUNNING
    ESP_LOGI(TAG, "System startup complete — all services running");
}

// ============================================================
// app_main
// ============================================================

void app_main(void) {
    ESP_LOGI(TAG, "================================");
    ESP_LOGI(TAG, " CUKTECH BLE Bridge - ESP-IDF");
    ESP_LOGI(TAG, "================================");

    // NVS init
    config_store_init();
    config_store_load(&g_cfg);

    if (!g_cfg.valid) {
        enter_ap_mode(false);  // first boot — init netif/event_loop
        return;
    }

    system_manager();
}
