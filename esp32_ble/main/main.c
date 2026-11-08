#include <string.h>
#include <strings.h>
#include <stdio.h>
#include <stdarg.h>
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
#include "esp_task_wdt.h"
#include "esp_ota_ops.h"

#include "config.h"
#include "config_store.h"
#include "device_state.h"
#include "http_server.h"
#include "uplink.h"
#include "bemfa.h"
#include "ota.h"

#include "queue_msg.h"
#include "ble_manager.h"
#include "miot_protocol_shared.h"

static const char *TAG = "MAIN";
static DeviceConfig g_cfg;

/* ============================================================
 * Queues
 * ============================================================ */
QueueHandle_t cmd_queue = NULL;
QueueHandle_t urgent_queue = NULL;
QueueHandle_t result_queue = NULL;

/* Wake-up for the delayed BLE connection (frontend "connect" button).
   The delay only needs to cover WiFi + MQTT startup (a few seconds) so the
   radio is not shared with coex while the link comes up; 60 s kept the
   dashboard empty for a minute after every boot/OTA. */
static SemaphoreHandle_t ble_connect_sem = NULL;
#define BLE_CONNECT_DELAY_S 15

/* ============================================================
 * Timers shared by app_task (all written/read in app_task except
 * last_set16_time which is also set in ble_task — use the tick count
 * through a volatile uint64_t; reads are only for debounce windows).
 * ============================================================ */
static volatile uint64_t last_set16_time = 0;
static volatile uint64_t last_set_time = 0;
static volatile uint8_t  last_set_piid = 0;

/* ============================================================
 * Port polling / heartbeat (app_task only)
 *
 * Why polling at all: the charger only sends a BLE notification when a
 * reading *changes*. While it holds a constant output (CV/CC at fixed
 * power) nothing at all arrives, so MQTT port topics go silent. HA's
 * energy engine counts frames (300 frames ≈ 5 min at 1 Hz) and discards a
 * >30 s gap entirely, so a silent phase metered as 0 Wh and the session
 * state machine never advanced. The Python server has the same protection
 * (1 s timer + active GET); the firmware had no port GET at all, which is
 * why a *steady* C3/USB-A charge never showed up on the ESP32 backend.
 *
 * What the device actually pushes (verified against the real HCI captures
 * in ble_server/tests/fixtures/notify_frames.json): per-port
 * `properties_changed` frames, including piid 3 and 4 — a plug-in or a
 * protocol transition produces a full 1→2→3→4 snapshot ~90 ms apart, and a
 * port's own changes push that port. So discovery is normally push-driven;
 * what pushes cannot cover is (a) the steady phase and (b) a *late*
 * connection: if we connect while a port is already charging steadily,
 * nothing changes, so nothing is ever pushed and the port sits at the idle
 * default (this is exactly the case the Python server's 3 s poll fixed).
 *
 * Cadence therefore follows whether power is actually flowing
 * (`active && current > 0.1`, the same threshold HA uses to open a
 * session), but a port only gets a GET when it has gone quiet — a push
 * (or a previous GET) newer than PORT_POLL_FRESH_MS makes the read
 * redundant:
 *
 *   charging   one GET per second, round-robin over the ports that have not
 *              been heard from in 3 s — in steady CV/CC silence that is every
 *              port every ~4 s, well inside the 30 s gap window; while the
 *              charger keeps pushing, nothing is sent at all. The 4 s
 *              heartbeat republishes unchanged readings.
 *   idle       same rule, one probe every 5 s (0.2 cmd/s), so a port that
 *              stops being heard from is re-read within 20 s. This is the
 *              *safety net* for the missed-push and dropped-resync cases.
 *   on connect the cadence is raised to 1 Hz for 4 s, so every port that has
 *              no fresh sample (a charger that was already delivering power
 *              when we connected pushes nothing) is read within seconds.
 *
 * A stale "drawing" reading after a missed unplug push self-heals: it keeps
 * the fast cadence, which corrects the state within ~4 s.
 *
 * NOTE: the 0.1 A threshold below is an implicit contract with
 * ha_integration/custom_components/cuktech_charger/energy_engine.py
 * (START_THRESHOLD / the `current <= 0.1` end rule). If HA's threshold
 * changes, this one must follow — otherwise one side believes a session is
 * running while the other has already dropped to the idle tier and stopped
 * sampling that port.
 * ============================================================ */
#define PORT_POLL_ACTIVE_MS   1000   /* a port is delivering power */
#define PORT_POLL_IDLE_MS     5000   /* idle: safety probe over all 4 ports */
#define PORT_HEARTBEAT_MS     4000   /* active port re-publish cadence */
/* A port sampled this recently needs no GET: the charger is already pushing.
   Measured on hardware: while the load fluctuates it pushes ~0.75 frames/s,
   and polling on top of that filled the serialized command channel (pend=5-7
   of 12, GETs timing out) for data we already had. Push frames and GET
   results both refresh this stamp. */
#define PORT_POLL_FRESH_MS    3000   /* a drawing port goes this long before a GET */
#define PORT_POLL_IDLE_FRESH_MS 15000  /* idle ports only need the safety probe */
static uint64_t last_port_poll = 0;
static bool     port_poll_fast = false;          /* true = 有口在输出，1Hz 档 */
static uint8_t  port_poll_piid = 1;              /* next port to consider, 1..DEVICE_PORT_COUNT */
static uint8_t  port_resync_left = 0;            /* 连接后 4 s 内按 1 Hz 找一遍缺口 */
static uint64_t last_port_pub_ms[DEVICE_PORT_COUNT] = {0};
/* 每个口最近一次拿到样本（推送或 GET 成功）的时间，用于跳过重复轮询。 */
static uint64_t last_port_rx_ms[DEVICE_PORT_COUNT] = {0};
/* 连续零读数确认（见 RES_GET 分支）：避免单帧 0V/0A 被当成"端口空了"。 */
static uint8_t  zero_confirm[DEVICE_PORT_COUNT] = {0};

/* 真正在对外输出功率的端口。active 只代表"端口开着/有电压"——充满后的
   5V/0A 也是 active，但那种状态不需要 1 Hz 采样（功率为 0，变化会推送）。 */
static bool _any_port_drawing(const PortState *ports) {
    for (int i = 0; i < DEVICE_PORT_COUNT; i++) {
        if (ports[i].active && ports[i].current > 0.1f) return true;
    }
    return false;
}

/* ============================================================
 * Protocol name mapping
 * ============================================================ */
static const char *PROTO_NAMES[] = {"idle","5V","5V","QC","AFC","FCP","SCP","PD","PPS","PPS","UFCS"};
static const int PROTO_NAMES_LEN = sizeof(PROTO_NAMES)/sizeof(PROTO_NAMES[0]);

static const char *get_proto_name(uint8_t code) {
    return (code < PROTO_NAMES_LEN) ? PROTO_NAMES[code] : "?";
}

/* ============================================================
 * HTTP -> command queue (never blocks the httpd task)
 * ============================================================ */
static bool send_urgent(const BleCommand *cmd, const char *what) {
    if (!urgent_queue) return false;
    if (xQueueSend(urgent_queue, cmd, pdMS_TO_TICKS(50)) != pdTRUE) {
        ESP_LOGW(TAG, "%s dropped (command queue full)", what);
        return false;
    }
    return true;
}

static bool handle_port_control(const char *port, const char *action) {
    if (!port || !action) return false;
    int bit = -1;
    if      (strcmp(port, "c1") == 0) bit = 0;
    else if (strcmp(port, "c2") == 0) bit = 1;
    else if (strcmp(port, "c3") == 0) bit = 2;
    else if (strcmp(port, "a")  == 0) bit = 3;
    if (bit < 0) return false;

    bool on = (strcmp(action, "on") == 0);
    BleCommand cmd = {CMD_PORT, (uint8_t)bit, on ? 1 : 0, 0};
    if (!send_urgent(&cmd, "PORT")) return false;
    ESP_LOGI(TAG, "PORT %s %s", port, action);
    return true;
}

static bool handle_setting_set(int piid, int value) {
    if (piid <= 0 || piid >= 32) return false;
    BleCommand cmd = {CMD_SET, (uint8_t)piid, (uint32_t)value, 0};
    if (!send_urgent(&cmd, "SET")) return false;
    ESP_LOGI(TAG, "SET piid=%d value=%d", piid, value);
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
    for (int i = 0; i < (int)(sizeof(map)/sizeof(map[0])); i++) {
        if (strcmp(port, map[i].port) == 0 && strcasecmp(protocol, map[i].proto) == 0) {
            BleCommand cmd = {CMD_PROTO, (uint8_t)map[i].bit, on ? 1 : 0, 0};
            if (!send_urgent(&cmd, "PROTO")) return false;
            ESP_LOGI(TAG, "PROTO %s %s %s", port, protocol, on ? "ON" : "OFF");
            return true;
        }
    }
    return false;
}

static bool handle_ble_control(bool enable) {
    /* Only record intent and queue the action; the actual NimBLE calls
       must happen in ble_task (NimBLE APIs are not safe from httpd/MQTT
       tasks, and disconnect() can block for seconds). */
    device_state_set_ble_enabled(enable);
    if (enable && ble_connect_sem) xSemaphoreGive(ble_connect_sem);

    BleCommand cmd = {CMD_BLE, 0, enable ? 1 : 0, 0};
    bool queued = send_urgent(&cmd, "BLE");
    uplink_publish_status();
    ESP_LOGI(TAG, "BLE %s%s", enable ? "enable" : "disable",
             queued ? "" : " (command queue full)");
    return queued;
}

/* ============================================================
 * GET response writers (no cJSON, no heap on the hot path)
 * ============================================================ */
static int append(char *buf, size_t cap, int pos, const char *fmt, ...) {
    if (pos < 0 || (size_t)pos >= cap) return -1;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf + pos, cap - (size_t)pos, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= cap - (size_t)pos) return -1;
    return pos + n;
}

static int write_ports_json(char *buf, size_t cap) {
    if (!buf || cap == 0) return -1;
    PortState ports[DEVICE_PORT_COUNT];
    device_state_get_ports(ports);
    static const char *names[] = {"C1", "C2", "C3", "USB-A"};

    int pos = append(buf, cap, 0, "[");
    for (int i = 0; i < DEVICE_PORT_COUNT; i++) {
        const PortState *p = &ports[i];
        const char *proto = p->active ? get_proto_name(p->protocol) : "idle";
        pos = append(buf, cap, pos,
                     "%s{\"port\":\"%s\",\"voltage\":%.1f,\"current\":%.1f,"
                     "\"power\":%.1f,\"protocol\":\"%s\",\"active\":%s,\"status\":%u}",
                     i ? "," : "", names[i],
                     p->voltage, p->current, p->power, proto,
                     p->active ? "true" : "false", (unsigned)p->status);
        if (pos < 0) return -1;
    }
    return append(buf, cap, pos, "]");
}

static int write_settings_json(char *buf, size_t cap) {
    if (!buf || cap == 0) return -1;
    uint32_t values[DEVICE_PIID_COUNT];
    bool valid[DEVICE_PIID_COUNT];
    device_state_get_settings(values, valid);

    int pos = append(buf, cap, 0, "{");
    bool first = true;
    for (int i = 0; i < DEVICE_PIID_COUNT; i++) {
        if (!valid[i]) continue;
        pos = append(buf, cap, pos, "%s\"%d\":%lu",
                     first ? "" : ",", i, (unsigned long)values[i]);
        if (pos < 0) return -1;
        first = false;
    }
    pos = append(buf, cap, pos, "%s\"ble_enabled\":%s}",
                 first ? "" : ",", device_state_ble_enabled() ? "true" : "false");
    return pos;
}

static bool get_setting_value(int piid, uint32_t *value) {
    if (piid < 0 || piid >= DEVICE_PIID_COUNT) return false;
    return device_state_get_setting((uint8_t)piid, value);
}

/* ============================================================
 * MQTT inbound -> command queue
 * ============================================================ */
static void mqtt_set_cb(int piid, uint32_t value) {
    BleCommand cmd = {CMD_SET, (uint8_t)piid, value, 0};
    if (!send_urgent(&cmd, "MQTT SET")) return;
    ESP_LOGI(TAG, "MQTT SET piid=%d val=%lu", piid, (unsigned long)value);
}

static void mqtt_port_cb(const char *port, const char *action) {
    handle_port_control(port, action);
}

static void mqtt_ble_cb(bool enabled) {
    handle_ble_control(enabled);
}

/* ============================================================
 * System manager / WiFi
 * ============================================================ */
static int wifi_retry_count = 0;
#define SYS_EVT_WIFI_READY   BIT0
#define SYS_EVT_BLE_READY    BIT1

static EventGroupHandle_t sys_events = NULL;
static TaskHandle_t s_wifi_reconn_task = NULL;

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_retry_count++;
        ESP_LOGW(TAG, "WiFi disconnected (reason=%d), retry (attempt %d)",
                 ((wifi_event_sta_disconnected_t *)data)->reason, wifi_retry_count);
        xEventGroupClearBits(sys_events, SYS_EVT_WIFI_READY);
        if (s_wifi_reconn_task) xTaskNotifyGive(s_wifi_reconn_task);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        wifi_retry_count = 0;
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "WiFi got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        /* Bring IPv6 up on the station: a link-local address is formed
           immediately, and with LWIP_IPV6_AUTOCONFIG a global address is
           added from the router advertisement when the AP offers a
           prefix.  The config page shows whichever one we end up with. */
        esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        if (sta) esp_netif_create_ip6_linklocal(sta);
        xEventGroupSetBits(sys_events, SYS_EVT_WIFI_READY);
    }
}

/* Event-driven reconnect: no polling, no busy loop. */
static void wifi_reconnect_task(void *arg) {
    (void)arg;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        if (xEventGroupGetBits(sys_events) & SYS_EVT_WIFI_READY) continue;

        int d = (wifi_retry_count < 5) ? (1000 * (1 << wifi_retry_count)) : 30000;
        if (d > 30000) d = 30000;
        vTaskDelay(pdMS_TO_TICKS(d));

        if (!(xEventGroupGetBits(sys_events) & SYS_EVT_WIFI_READY)) {
            ESP_LOGI(TAG, "WiFi reconnect (backoff %dms)", d);
            /* Drop notifications raised while we were backing off: the
               connect below supersedes them.  A disconnect that happens
               after this point notifies again normally. */
            ulTaskNotifyTake(pdTRUE, 0);
            esp_wifi_connect();
        }
    }
}

/* ============================================================
 * BLE callback
 * ============================================================ */
static void on_ble_state_change(BLEState old_state, BLEState new_state) {
    (void)old_state;
    BleResult res = {RES_BLE_STATUS, true, 0, (uint32_t)(new_state == BLE_READY), 0};
    if (xQueueSend(result_queue, &res, 0) != pdTRUE) {
        ESP_LOGW(TAG, "BLE state change dropped (result queue full)");
    }
}

/* ============================================================
 * BLE task — NimBLE state machine + command execution.
 * All NimBLE calls happen here.  No blocking command wrappers.
 * ============================================================ */
static void ble_task(void *pvParameters) {
    (void)pvParameters;
    ESP_LOGI(TAG, "BLE task started");

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

    bool was_ready = false;
    uint64_t last_stack_log = 0;

    while (1) {
        bool did_work = false;
        int drain_cnt = 0;
        BleCommand cmd;

        do {
            cmd.type = CMD_NOP;
            if (urgent_queue && xQueueReceive(urgent_queue, &cmd, 0) == pdTRUE) {
                did_work = true;
            } else if (xQueueReceive(cmd_queue, &cmd, 0) == pdTRUE) {
                did_work = true;
            }
            if (cmd.type == CMD_NOP) break;
            drain_cnt++;

            switch (cmd.type) {
            case CMD_GET: {
                if (!ble_manager_send_get_async(cmd.piid, 0)) {
                    ESP_LOGW(TAG, "GET piid=%d could not be queued", cmd.piid);
                    BleResult r = {RES_GET, false, cmd.piid, 0, 0};
                    xQueueSend(result_queue, &r, 0);
                } else {
                    ESP_LOGD(TAG, "GET piid=%d queued", cmd.piid);
                }
                break;
            }
            case CMD_SET: {
                if (!ble_manager_send_set_async(cmd.piid, cmd.value, 0)) {
                    ESP_LOGW(TAG, "SET piid=%d could not be queued", cmd.piid);
                    BleResult r = {RES_SET, false, cmd.piid, cmd.value, 0};
                    xQueueSend(result_queue, &r, 0);
                } else {
                    ESP_LOGD(TAG, "SET piid=%d val=%lu queued", cmd.piid, (unsigned long)cmd.value);
                }
                break;
            }
            case CMD_PORT: {
                uint32_t current = device_state_port_ctrl();
                if (cmd.value) current |= (1u << cmd.piid);
                else           current &= ~(1u << cmd.piid);
                device_state_set_port_ctrl(current);
                last_set16_time = esp_timer_get_time() / 1000;
                last_set_time = last_set16_time;
                last_set_piid = 16;
                uplink_publish_settings();
                if (!ble_manager_send_set_async(16, current, 0)) {
                    ESP_LOGW(TAG, "PORT SET16=0x%02lX could not be queued", (unsigned long)current);
                } else {
                    ESP_LOGI(TAG, "PORT bit=%d -> SET16=0x%02lX", cmd.piid, (unsigned long)current);
                }
                break;
            }
            case CMD_PROTO: {
                uint32_t current = device_state_protocol_ext();
                if (cmd.value) current |= (1u << cmd.piid);
                else           current &= ~(1u << cmd.piid);
                device_state_set_protocol_ext(current);
                last_set_time = esp_timer_get_time() / 1000;
                last_set_piid = 21;
                uplink_publish_settings();
                if (!ble_manager_send_set_async(21, current, 0)) {
                    ESP_LOGW(TAG, "PROTO SET21=0x%lX could not be queued", (unsigned long)current);
                } else {
                    ESP_LOGI(TAG, "PROTO bit=%d -> SET21=0x%lX", cmd.piid, (unsigned long)current);
                }
                break;
            }
            case CMD_BLE: {
                bool want = (cmd.value != 0);
                ESP_LOGI(TAG, "BLE requested %s", want ? "enable" : "disable");
                ble_manager_set_enabled(want);
                uplink_publish_status();
                break;
            }
            case CMD_RECONNECT:
            case CMD_DISCONNECT:
                ble_manager_disconnect();
                break;
            default:
                break;
            }
        } while (drain_cnt < 8);

        ble_manager_loop();

        bool ready = ble_manager_is_ready();
        if (ready != was_ready) {
            was_ready = ready;
            BleResult res = {RES_BLE_STATUS, true, 0, (uint32_t)ready, 0};
            xQueueSend(result_queue, &res, pdMS_TO_TICKS(50));
        }

        /* Auto reconnect with exponential backoff (10s .. 300s). */
        {
            static uint64_t last_ble_connected = 0;
            static unsigned backoff_sec = 10;
            uint64_t now = esp_timer_get_time() / 1000;
            if (ready) {
                last_ble_connected = now;
                backoff_sec = 10;
            } else if (device_state_ble_enabled() && last_ble_connected > 0 &&
                       (now - last_ble_connected >= (uint64_t)backoff_sec * 1000)) {
                ESP_LOGI(TAG, "BLE auto-reconnect: restarting scanner (backoff=%us)", backoff_sec);
                last_ble_connected = now;
                backoff_sec = (backoff_sec >= 150) ? 300 : (backoff_sec * 2);
                ble_manager_set_enabled(false);
                uint32_t settle_ms = 500;
                do {
                    vTaskDelay(pdMS_TO_TICKS(50));
                    settle_ms -= 50;
                } while (!ble_manager_is_idle() && settle_ms > 0);
                ble_manager_set_enabled(true);
            }
        }

        uint64_t now = esp_timer_get_time() / 1000;
        if (now - last_stack_log >= 60000) {
            last_stack_log = now;
            ESP_LOGD(TAG, "ble stack hwm=%u bytes",
                     (unsigned)uxTaskGetStackHighWaterMark(NULL));
        }

        if (!did_work) vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/* ============================================================
 * app task — result processing + periodic timers + publish flush
 * ============================================================ */
static void process_result(const BleResult *res) {
    switch (res->type) {
    case RES_PORT_PUSH: {
        int idx = res->piid - 1;
        if (idx < 0 || idx >= DEVICE_PORT_COUNT) break;
        PortState p = {
            .voltage = res->voltage,
            .current = res->current,
            .power = res->power,
            .protocol = res->protocol,
            .status = res->status,
            .active = (res->status != 0) || (res->voltage > 0.5f),
            .valid = true,
        };
        device_state_set_port(idx, &p);
        uplink_publish_port(idx);
        last_port_pub_ms[idx] = esp_timer_get_time() / 1000;
        last_port_rx_ms[idx] = last_port_pub_ms[idx];
        break;
    }
    case RES_GET: {
        if (res->piid >= 1 && res->piid <= 4) {
            /* A failed GET (timeout / aborted on disconnect) carries value=0.
               Treating that as a reading would publish "0 V 0 A / inactive"
               and make the HA integration end the session and consume a
               `once` limit on the very next poll. */
            if (!res->success) break;
            int idx = res->piid - 1;
            float v = ((res->value >> 24) & 0xFF) / 10.0f;
            float c = ((res->value >> 16) & 0xFF) / 10.0f;
            uint8_t code = (res->value >> 8) & 0xFF;
            uint8_t st = res->value & 0xFF;

            uint32_t ext = 0, s17 = 0, s18 = 0;
            bool ext_ok = device_state_get_setting(21, &ext);
            bool s17_ok = device_state_get_setting(17, &s17);
            bool s18_ok = device_state_get_setting(18, &s18);

            bool pd_on = true, pps_on = true;
            if (idx < 2) {
                if (ext_ok) {
                    pd_on  = (ext >> (idx == 0 ? 0 : 8)) & 1;
                    pps_on = (ext >> (idx == 0 ? 1 : 9)) & 1;
                }
            } else if (ext_ok) {
                /* C3/A are not governed by PD/PPS bits in the same way. */
                pd_on = true;
                pps_on = true;
            }

            uint8_t hw_proto = 0;
            uint8_t pdo_kind = 0;
            if (idx < 2) {
                if (s17_ok) {
                    hw_proto = (idx == 0) ? ((s17 >> 24) & 0xFF) : ((s17 >> 8) & 0xFF);
                    uint16_t word = (idx == 0) ? (s17 & 0xFFFF) : ((s17 >> 16) & 0xFFFF);
                    pdo_kind = (word >> 8) & 0xFF;
                }
            } else {
                if (s18_ok) {
                    hw_proto = (idx == 2) ? ((s18 >> 24) & 0xFF) : ((s18 >> 8) & 0xFF);
                    uint16_t word = (idx == 2) ? (s18 & 0xFFFF) : ((s18 >> 16) & 0xFFFF);
                    pdo_kind = (word >> 8) & 0xFF;
                }
            }

            uint8_t proto = estimate_protocol_shared(res->piid, v, code,
                                                     pd_on, pps_on, pdo_kind, hw_proto);

            PortState old;
            device_state_get_port(idx, &old);
            bool was_active = old.active;
            bool was_valid = old.valid;

            uint64_t now = esp_timer_get_time() / 1000;
            bool nearly_zero = (v < 0.1f && c < 0.1f && st == 0);
            if (nearly_zero && was_valid && was_active && last_set_piid != 16 &&
                (now - last_set_time < 500)) {
                break;  /* ignore transient zero during protocol transitions */
            }
            /* 单帧全零不足以认定"端口空了"：协议切换/刚上电时设备会瞬时报 0，
               而轮询是 4s 一次，撞上这个窗口会让 HA 立刻结束会话（并消费
               once 限额）。要求连续两次采样都为零才接受——与 Python 服务端
               "单帧 V/I=0 绝不结束会话" 的去抖同义，只是这里用采样数代替秒数。 */
            if (nearly_zero && was_valid && was_active) {
                if (zero_confirm[idx] < 1) {
                    zero_confirm[idx]++;
                    ESP_LOGD(TAG, "Port%d reads zero, confirming before publish", idx + 1);
                    break;
                }
            } else {
                zero_confirm[idx] = 0;
            }

            PortState p = {
                .voltage = v,
                .current = c,
                .power = v * c,
                .protocol = proto,
                .status = st,
                .active = (st != 0) || (v > 0.5f),
                .valid = true,
            };
            device_state_set_port(idx, &p);
            last_port_rx_ms[idx] = now;

            bool publish = (was_active != p.active) || !was_valid;
            /* 读数变化也要发：GET 轮询是静默期唯一的样本来源，只在
               active 变化时发布会把"恒功率一直 60W"整段吞掉。 */
            if (fabsf(p.voltage - old.voltage) > 0.05f ||
                fabsf(p.current - old.current) > 0.05f ||
                p.status != old.status)
                publish = true;
            if (now - last_set_time < 500 && (last_set_piid == 16 || last_set_piid == 21))
                publish = true;
            /* 心跳：设备在恒压恒流固定功率下完全不推 notify，HA 的积分器
               对 >30s 的间隔整段丢弃，所以活跃口必须周期性给出样本。 */
            if (p.active && (now - last_port_pub_ms[idx]) >= PORT_HEARTBEAT_MS)
                publish = true;
            if (publish) {
                uplink_publish_port(idx);
                last_port_pub_ms[idx] = now;
            }
        } else {
            if (res->success) {
                uint64_t now = esp_timer_get_time() / 1000;
                if (res->piid == 16) {
                    /* Ignore a stale GET(16)=0 for 3s after a port SET. */
                    if (!(res->value == 0 && device_state_port_ctrl() != 0 &&
                          (now - last_set16_time) < 3000)) {
                        device_state_set_port_ctrl(res->value);
                    }
                } else if (res->piid < DEVICE_PIID_COUNT) {
                    device_state_set_setting(res->piid, res->value);
                }
                uplink_publish_settings();
            }
        }
        break;
    }
    case RES_SET: {
        if (res->success) {
            if (res->piid == 16) device_state_set_port_ctrl(res->value);
            else if (res->piid < DEVICE_PIID_COUNT) device_state_set_setting(res->piid, res->value);
            last_set_time = esp_timer_get_time() / 1000;
            last_set_piid = res->piid;
            uplink_publish_settings();
        }
        break;
    }
    case RES_BLE_STATUS: {
        device_state_set_ble_ready(res->value != 0);
        uplink_publish_status();
        if (res->value) {
            static const uint8_t INIT_PIIDS[] = {5, 6, 15, 21, 17, 18};
            for (int i = 0; i < (int)sizeof(INIT_PIIDS); i++) {
                BleCommand c = {CMD_GET, INIT_PIIDS[i], 0, 0};
                xQueueSend(cmd_queue, &c, 0);
            }
            /* 连接（含重连）后把端口轮询提到 1 Hz 持续 4 s：如果连上时已经有
               设备在恒功率充电，设备不会因为"没变化"而推送，端口会一直停在
               默认值——这正是 Python 服务端加 3s 轮询要解决的场景。这 4 个
               时隙由 app_task 按新鲜度挑选没有样本的口，已有推送的口直接跳过。 */
            port_resync_left = DEVICE_PORT_COUNT;
        }
        break;
    }
    default:
        break;
    }
}

static void app_task(void *pvParameters) {
    (void)pvParameters;
    vTaskDelay(pdMS_TO_TICKS(100));

    uint64_t last_mem_print = 0;
    uint64_t last_status_print = 0;
    /* First poll runs one full interval after boot; the initial
       INIT_PIIDS GETs already refresh the essentials. */
    uint64_t last_cd_fetch_slow = esp_timer_get_time() / 1000;
    uint64_t last_cd_fetch_fast = last_cd_fetch_slow;
    uint64_t boot_time = esp_timer_get_time() / 1000;
    bool ota_marked_valid = false;

    while (1) {
        uint64_t now = esp_timer_get_time() / 1000;

        /* Periodic auto-reboot (explicit user configuration only). */
        uint32_t rbi = g_cfg.reboot_interval_sec;
        if (rbi > 0 && (now - boot_time) >= (uint64_t)rbi * 1000) {
            ESP_LOGW(TAG, "Periodic reboot: interval=%us reached", (unsigned)rbi);
            vTaskDelay(pdMS_TO_TICKS(100));
#if CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
            /* A deliberate reboot means the image is healthy: never let
               a short reboot interval trip the rollback counter. */
            if (!ota_marked_valid) {
                esp_ota_mark_app_valid_cancel_rollback();
                ota_marked_valid = true;
            }
#endif
            esp_restart();
        }

#if CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
        /* Mark the image valid after 60s of healthy running.  If the
           device crashes before this, the bootloader rolls back. */
        if (!ota_marked_valid && (now - boot_time) >= 60000) {
            esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
            ESP_LOGI(TAG, "OTA rollback: mark valid -> %s", esp_err_to_name(err));
            ota_marked_valid = true;
        }
#endif

        /* Memory monitor (diagnostics only — no GC, no reboot). */
        if (now - last_mem_print >= 30000) {
            last_mem_print = now;
            size_t free_heap = esp_get_free_heap_size();
            size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
            UBaseType_t hwm = uxTaskGetStackHighWaterMark(NULL);
            multi_heap_info_t hi;
            heap_caps_get_info(&hi, MALLOC_CAP_8BIT);
            ESP_LOGI(TAG, "MEM: free=%u min=%u max_block=%u alloc=%u blocks=%u/%u | app stack hwm=%u",
                     (unsigned)free_heap,
                     (unsigned)esp_get_minimum_free_heap_size(),
                     (unsigned)largest,
                     (unsigned)hi.total_allocated_bytes,
                     (unsigned)hi.allocated_blocks, (unsigned)hi.total_blocks,
                     (unsigned)hwm);
            if (hwm < 1024) ESP_LOGW(TAG, "app task stack low: %u bytes", (unsigned)hwm);
            if (largest < free_heap / 3) {
                ESP_LOGW(TAG, "heap fragmentation: max_block=%u < free/3 (%u)",
                         (unsigned)largest, (unsigned)(free_heap / 3));
            }
        }

        /* Block until a BLE result arrives or 50ms elapses. */
        BleResult res;
        if (xQueueReceive(result_queue, &res, pdMS_TO_TICKS(50)) == pdTRUE) {
            do {
                process_result(&res);
            } while (xQueueReceive(result_queue, &res, 0) == pdTRUE);
            now = esp_timer_get_time() / 1000;
        }

        bool ble_ready = device_state_ble_ready();

        /* Slow poll: scene/screen/countdown/trickle. */
        if (ble_ready && (now - last_cd_fetch_slow >= 90000)) {
            last_cd_fetch_slow = now;
            static const uint8_t SLOW_PIIDS[] = {5, 6, 9, 10, 11, 12, 15};
            for (int i = 0; i < (int)sizeof(SLOW_PIIDS); i++) {
                BleCommand c = {CMD_GET, SLOW_PIIDS[i], 0, 0};
                xQueueSend(cmd_queue, &c, 0);
            }
        }
        /* Fast poll: port control / protocol extend. */
        if (ble_ready && (now - last_cd_fetch_fast >= 20000)) {
            last_cd_fetch_fast = now;
            static const uint8_t FAST_PIIDS[] = {16, 21};
            for (int i = 0; i < (int)sizeof(FAST_PIIDS); i++) {
                BleCommand c = {CMD_GET, FAST_PIIDS[i], 0, 0};
                xQueueSend(cmd_queue, &c, 0);
            }
        }
        /* Port poll: cadence follows whether power is actually flowing, and
           a port is only read when it has gone quiet — see the block at the
           top of the file.
             resync:   after (re)connect, 1 Hz for 4 s to fill whatever port
                       produced no fresh sample;
             charging: one GET/s, round-robin over the ports not heard from
                       in 3 s (each port every ~4 s during steady silence);
             idle:     same rule, one probe every 5 s.
           While the charger keeps pushing, every port stays fresh and no
           GET is sent at all. The tier is re-evaluated once per poll (not
           per loop iteration), so a state change costs at most one interval
           of extra/missing polling. */
        {
            /* 1 Hz both while charging and during the post-connect resync;
               only the fully idle tier drops to one probe per 5 s. */
            uint32_t interval = (port_poll_fast || port_resync_left > 0)
                                    ? PORT_POLL_ACTIVE_MS : PORT_POLL_IDLE_MS;
            if (ble_ready && (now - last_port_poll >= interval)) {
                last_port_poll = now;

                PortState ps[DEVICE_PORT_COUNT];
                device_state_get_ports(ps);
                port_poll_fast = _any_port_drawing(ps);

                /* First port at or after the cursor whose newest sample (push
                   or GET) is older than its threshold: a port that is actually
                   delivering power must be re-read within seconds while the
                   charger sits in silent CV/CC, whereas a port that is not
                   drawing only needs the occasional safety probe. */
                uint8_t piid = 0;
                for (int k = 0; k < DEVICE_PORT_COUNT; k++) {
                    uint8_t cand = (uint8_t)((port_poll_piid - 1 + k) %
                                             DEVICE_PORT_COUNT + 1);
                    const PortState *pp = &ps[cand - 1];
                    uint32_t need_ms = (pp->active && pp->current > 0.1f)
                                           ? PORT_POLL_FRESH_MS
                                           : PORT_POLL_IDLE_FRESH_MS;
                    if (now - last_port_rx_ms[cand - 1] >= need_ms) {
                        piid = cand;
                        break;
                    }
                }

                /* The resync slot is spent either way: if the charger pushed
                   everything we needed, there is nothing left to resync. */
                if (port_resync_left > 0) port_resync_left--;

                if (piid == 0) {
                    ESP_LOGD(TAG, "port poll: every port fresh, skipping");
                } else {
                    BleCommand c = {CMD_GET, piid, 0, 0};
                    if (xQueueSend(cmd_queue, &c, 0) != pdTRUE) {
                        /* Queue full: leave the cursor where it is, so the
                           next interval retries the same port. */
                        ESP_LOGW(TAG, "port poll piid=%d dropped (queue full)", piid);
                    } else {
                        port_poll_piid = (piid >= DEVICE_PORT_COUNT)
                                             ? 1 : (uint8_t)(piid + 1);
                    }
                }
            }
        }

        if (now - last_status_print >= 10000) {
            int pending = ble_manager_pending_count();
            wifi_ap_record_t ap_info;
            int rssi = -100;
            if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) rssi = ap_info.rssi;
            ESP_LOGI(TAG, "Status: BLE=%s pend=%d WiFi=%ddBm Uptime=%ds",
                     ble_ready ? "CONNECTED" : "DISCONNECTED",
                     pending, rssi, (int)(now / 1000));
            last_status_print = now;
        }

        bemfa_loop();
        uplink_loop();
    }
}

/* ============================================================
 * AP mode (first-time configuration)
 * ============================================================ */
static void _reboot_task(void *arg) {
    (void)arg;
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
        esp_netif_create_default_wifi_ap();
    }

    wifi_config_t ap_config = {
        .ap = {
            .ssid = AP_SSID,
            .ssid_len = sizeof(AP_SSID) - 1,
            .password = AP_PASSWORD,
            .max_connection = 4,
            .authmode = WIFI_AUTH_WPA2_PSK,
            .channel = 6,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_inactive_time(WIFI_IF_AP, 600));

    ESP_LOGI(TAG, "AP started");

    config_store_apply_defaults(&g_cfg);

#if CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
    /* AP fallback is a working state (missing/bad WiFi credentials), not a
       failed boot.  Confirm the running image here as well; otherwise a
       fresh OTA image that lands in AP mode would be rolled back on the
       next reboot (e.g. right after the user saves the WiFi settings). */
    esp_err_t mark_err = esp_ota_mark_app_valid_cancel_rollback();
    ESP_LOGI(TAG, "OTA rollback: mark valid (AP mode) -> %s",
             esp_err_to_name(mark_err));
#endif

    http_server_start(&g_cfg, ap_reboot_callback);

    while (1) vTaskDelay(pdMS_TO_TICKS(1000));
}

/* ============================================================
 * System manager
 * ============================================================ */
static void system_manager(void) {
    sys_events = xEventGroupCreate();

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

    wifi_config_t wifi_sta = { .sta = {.ssid = "", .password = ""} };
    strncpy((char *)wifi_sta.sta.ssid, g_cfg.wifi_ssid, sizeof(wifi_sta.sta.ssid) - 1);
    strncpy((char *)wifi_sta.sta.password, g_cfg.wifi_pass, sizeof(wifi_sta.sta.password) - 1);
    ESP_LOGI(TAG, "WiFi SSID='%s' (len=%d) PASS len=%d",
             g_cfg.wifi_ssid, (int)strlen(g_cfg.wifi_ssid), (int)strlen(g_cfg.wifi_pass));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_sta));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    esp_coex_preference_set(ESP_COEX_PREFER_WIFI);

    /* Create the reconnect worker before starting WiFi so the first
       disconnect notification cannot be missed. */
    xTaskCreatePinnedToCore(wifi_reconnect_task, "wifi_reconn", 2560, NULL, 3,
                            &s_wifi_reconn_task, 0);

    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "WiFi connecting to %s...", g_cfg.wifi_ssid);

    EventBits_t bits = xEventGroupWaitBits(sys_events, SYS_EVT_WIFI_READY,
                                           pdFALSE, pdTRUE, pdMS_TO_TICKS(30000));
    if (!(bits & SYS_EVT_WIFI_READY)) {
        ESP_LOGW(TAG, "WiFi failed to connect within 30s — proceeding in AP mode");
        enter_ap_mode(true);
        return;
    }

    /* ---- HTTP ---- */
    cmd_queue = xQueueCreate(20, sizeof(BleCommand));
    urgent_queue = xQueueCreate(8, sizeof(BleCommand));
    result_queue = xQueueCreate(48, sizeof(BleResult));

    http_server_set_callbacks(write_ports_json, write_settings_json,
                              get_setting_value,
                              handle_port_control, handle_setting_set,
                              handle_protocol_toggle, handle_ble_control);
    ota_init();                 /* create the OTA worker before requests arrive */
    http_server_start(&g_cfg, ap_reboot_callback);
    ESP_LOGI(TAG, "HTTP server ready");

    ble_connect_sem = xSemaphoreCreateBinary();

    /* ---- BLE ---- */
#if CONFIG_FREERTOS_UNICORE
    xTaskCreate(ble_task, "ble", 10240, NULL, 2, NULL);
#else
    xTaskCreatePinnedToCore(ble_task, "ble", 10240, NULL, 2, NULL, 1);
#endif
    xTaskCreatePinnedToCore(app_task, "app", 6144, NULL, 1, NULL, 0);

    /* ---- MQTT + Bemfa ---- */
    vTaskDelay(pdMS_TO_TICKS(2000));
#if ENABLE_MQTT
    uplink_set_command_callbacks(mqtt_set_cb, mqtt_port_cb, mqtt_ble_cb);
    uplink_init(&g_cfg);
#endif
    bemfa_init(&g_cfg, handle_port_control, handle_ble_control);

    ESP_LOGI(TAG, "System startup complete — all services running");
}

void app_main(void) {
    ESP_LOGI(TAG, "================================");
    ESP_LOGI(TAG, " CUKTECH BLE Bridge - ESP-IDF");
    ESP_LOGI(TAG, "================================");

    config_store_init();
    config_store_load(&g_cfg);
    device_state_init();

    if (!g_cfg.valid) {
        enter_ap_mode(false);
        return;
    }

    system_manager();
}
