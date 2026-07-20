#include "bemfa.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_http_client.h"
#include "mqtt_client.h"
#include <string.h>
#include <sys/socket.h>
#include <netdb.h>

static const char *TAG = "BEMFA";

static esp_mqtt_client_handle_t _client = NULL;
static bemfa_cmd_cb _on_cmd = NULL;
static bemfa_ble_cb _on_ble = NULL;
static bool _enabled = false;
static uint64_t _connect_time = 0;
static portMUX_TYPE _state_mux = portMUX_INITIALIZER_UNLOCKED;
static bool _port_state[4] = {false, false, false, false};
static bool _ble_state = false;
static const DeviceConfig *_cfg = NULL;

// Ping/pong keepalive (aligned with HA integration)
#define TOPIC_PING          "hassping"
#define INTERVAL_PING_SEND  30   // send ping every 30s
#define INTERVAL_PING_RECV  20   // detect lost after 20s
#define MAX_PING_LOST       3    // reconnect after 3 consecutive lost
#define GRACE_PERIOD_SEC    10   // ignore echo for 10s after each connect
static uint64_t _last_ping_time = 0;
static uint64_t _ping_send_time = 0;  // when last ping was sent
static int _ping_lost = 0;
static bool _ping_waiting = false;    // waiting for pong
static bool _grace_active = false;    // suppress commands during grace

// 5 devices: c1, c2, c3, usb-a, ble
#define NUM_DEVICES 5
#define TOPIC_BUF_LEN 128

static char _topics[NUM_DEVICES][TOPIC_BUF_LEN];
static char _pub_topics[NUM_DEVICES][TOPIC_BUF_LEN];

static const char *MD5_HEX[NUM_DEVICES] = {
    "d0dbbf2e94cfbbb94890171831115c58",  // md5("cuktech_c1")
    "58ba80e67dd21f0f59b44d78d5567297",  // md5("cuktech_c2")
    "cf5fe9b80b6039d0bd702d9d8439bc3c",  // md5("cuktech_c3")
    "67e92b6bb3e2057d1cdc1829d1e6a7c4",  // md5("cuktech_usb_a")
    "2fc0d9b7387e677f287fbdba07f2877a",  // md5("cuktech_ble")
};

static const char *DEVICE_NAMES[NUM_DEVICES] = {
    "C口1开关", "C口2开关", "C口3开关", "USB-A开关", "蓝牙开关",
};
static const char *PORT_NAMES[NUM_DEVICES] = {"c1", "c2", "c3", "a", "ble"};

// ---- HTTP API ----

static bool _register_topic(const char *uid, const char *topic, const char *name) {
    char post_data[256];
    snprintf(post_data, sizeof(post_data),
             "uid=%s&topic=%s&type=1&name=%s", uid, topic, name);
    esp_http_client_config_t http_cfg = {
        .url = "http://api.bemfa.com/api/user/addtopic/",
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    esp_http_client_set_header(client, "Content-Type", "application/x-www-form-urlencoded");
    esp_err_t err = esp_http_client_open(client, strlen(post_data));
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "HTTP open failed for %s: %s", topic, esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return false;
    }
    esp_http_client_write(client, post_data, strlen(post_data));
    int status = esp_http_client_fetch_headers(client);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    ESP_LOGI(TAG, "Register topic %s (%s): HTTP %d", topic, name, status);
    if (status != 200) {
        ESP_LOGW(TAG, "Failed to register topic %s (HTTP %d)", topic, status);
    }
    return status == 200;
}

// Pre-resolve DNS for Bemfa API to ensure network is ready
static bool _dns_resolve(const char *host) {
    struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM };
    struct addrinfo *result = NULL;
    int err = getaddrinfo(host, NULL, &hints, &result);
    if (err != 0 || !result) {
        ESP_LOGW(TAG, "DNS resolve failed for %s: %d", host, err);
        return false;
    }
    freeaddrinfo(result);
    ESP_LOGI(TAG, "DNS resolved: %s", host);
    return true;
}

static void _register_task(void *arg) {
    // Wait for network stack to be fully ready
    vTaskDelay(pdMS_TO_TICKS(3000));

    // Pre-resolve DNS
    if (!_dns_resolve("api.bemfa.com")) {
        ESP_LOGW(TAG, "DNS not ready, retrying in 5s...");
        vTaskDelay(pdMS_TO_TICKS(5000));
        if (!_dns_resolve("api.bemfa.com")) {
            ESP_LOGW(TAG, "DNS still not ready, skipping registration");
            vTaskDelete(NULL);
            return;
        }
    }

    ESP_LOGI(TAG, "Registering Bemfa topics...");
    for (int i = 0; i < NUM_DEVICES; i++) {
        _register_topic(_cfg->bemfa_uid, _topics[i], DEVICE_NAMES[i]);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    ESP_LOGI(TAG, "Bemfa topic registration done");
    vTaskDelete(NULL);
}

// ---- Ping/Pong (aligned with HA integration) ----

static void _send_ping(void) {
    if (!_client) return;
    esp_mqtt_client_publish(_client, TOPIC_PING, "ping", 0, 0, 1);
    _ping_send_time = esp_timer_get_time() / 1000000;
    _ping_waiting = true;
    ESP_LOGD(TAG, "Ping sent");
}

static void _handle_pong(void) {
    _ping_lost = 0;
    _ping_waiting = false;
    ESP_LOGD(TAG, "Pong received");
}

// ---- MQTT handler ----

static void _mqtt_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
    esp_mqtt_event_handle_t event = data;
    switch (event->event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "Bemfa MQTT connected");
        _connect_time = esp_timer_get_time() / 1000000;
        _ping_lost = 0;
        _ping_waiting = false;
        _last_ping_time = _connect_time;
        _grace_active = true;
        // Subscribe to device topics
        for (int i = 0; i < NUM_DEVICES; i++) {
            if (_topics[i][0]) {
                esp_mqtt_client_subscribe(_client, _topics[i], 1);
                ESP_LOGI(TAG, "Subscribed: %s", _topics[i]);
            }
        }
        // Subscribe to ping topic (for pong)
        esp_mqtt_client_subscribe(_client, TOPIC_PING, 1);
        // Publish initial states
        for (int i = 0; i < 4; i++) {
            const char *state;
            portENTER_CRITICAL(&_state_mux);
            state = _port_state[i] ? "on" : "off";
            portEXIT_CRITICAL(&_state_mux);
            esp_mqtt_client_publish(_client, _pub_topics[i], state, 0, 1, 1);
        }
        {
            const char *state;
            portENTER_CRITICAL(&_state_mux);
            state = _ble_state ? "on" : "off";
            portEXIT_CRITICAL(&_state_mux);
            esp_mqtt_client_publish(_client, _pub_topics[4], state, 0, 1, 1);
        }
        break;

    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "Bemfa MQTT disconnected");
        _ping_waiting = false;
        _grace_active = true;  // re-enable grace on next connect
        break;

    case MQTT_EVENT_DATA: {
        char topic_buf[128] = {0};
        int tlen = event->topic_len < sizeof(topic_buf) - 1 ? event->topic_len : sizeof(topic_buf) - 1;
        memcpy(topic_buf, event->topic, tlen);
        topic_buf[tlen] = '\0';

        char data_buf[64] = {0};
        int dlen = event->data_len < sizeof(data_buf) - 1 ? event->data_len : sizeof(data_buf) - 1;
        memcpy(data_buf, event->data, dlen);
        data_buf[dlen] = '\0';

        // Handle pong
        if (strcmp(topic_buf, TOPIC_PING) == 0) {
            _handle_pong();
            break;
        }

        ESP_LOGI(TAG, "MQTT recv: topic=%s data=%s", topic_buf, data_buf);

        // Ignore echo during grace period (10s after each connect)
        if (_grace_active) {
            uint64_t now = esp_timer_get_time() / 1000000;
            if (now - _connect_time < GRACE_PERIOD_SEC) {
                ESP_LOGI(TAG, "Ignoring echo during grace period");
                break;
            }
            _grace_active = false;
        }

        // Match command
        bool on = (strncasecmp(data_buf, "on", 2) == 0);
        for (int i = 0; i < NUM_DEVICES; i++) {
            if (_topics[i][0] && strcmp(topic_buf, _topics[i]) == 0) {
                bool ok = false;
                if (i == 4 && _on_ble) {
                    ok = _on_ble(on);
                } else if (_on_cmd) {
                    ok = _on_cmd(PORT_NAMES[i], on ? "on" : "off");
                }
                if (ok) {
                    portENTER_CRITICAL(&_state_mux);
                    if (i < 4) _port_state[i] = on;
                    else _ble_state = on;
                    portEXIT_CRITICAL(&_state_mux);
                }
                break;
            }
        }
        break;
    }
    default: break;
    }
}

// ---- Init / Deinit ----

void bemfa_init(const DeviceConfig *cfg, bemfa_cmd_cb on_cmd, bemfa_ble_cb on_ble) {
    if (!cfg->bemfa_enable || cfg->bemfa_uid[0] == '\0') {
        ESP_LOGI(TAG, "Bemfa disabled");
        return;
    }

    _on_cmd = on_cmd;
    _on_ble = on_ble;
    _enabled = true;
    _cfg = cfg;

    for (int i = 0; i < NUM_DEVICES; i++) {
        snprintf(_topics[i], sizeof(_topics[i]), "hass%s006", MD5_HEX[i]);
        snprintf(_pub_topics[i], sizeof(_pub_topics[i]), "hass%s006/set", MD5_HEX[i]);
    }

    xTaskCreate(_register_task, "bemfa_reg", 4096, NULL, 1, NULL);

    char uri[128];
    snprintf(uri, sizeof(uri), "mqtt://bemfa.com:9501");
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = uri,
        .session.keepalive = 600,
        .credentials.client_id = cfg->bemfa_uid,
    };
    _client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(_client, ESP_EVENT_ANY_ID, _mqtt_event_handler, NULL);
    esp_mqtt_client_start(_client);
    ESP_LOGI(TAG, "Bemfa MQTT started (uid=%.4s****)", cfg->bemfa_uid);
}

void bemfa_disconnect(void) {
    if (_client) {
        esp_mqtt_client_stop(_client);
        esp_mqtt_client_destroy(_client);
        _client = NULL;
    }
    _on_cmd = NULL;
    _on_ble = NULL;
    _enabled = false;
}

// ---- Publish state ----

void bemfa_publish_port(int idx, float voltage, float current, float power, bool active) {
    if (!_enabled || !_client || idx < 0 || idx >= 4) return;
    portENTER_CRITICAL(&_state_mux);
    _port_state[idx] = active;
    portEXIT_CRITICAL(&_state_mux);
    const char *state = active ? "on" : "off";
    esp_mqtt_client_publish(_client, _pub_topics[idx], state, 0, 1, 1);
}

void bemfa_publish_status(bool connected) {
    if (!_enabled || !_client) return;
    portENTER_CRITICAL(&_state_mux);
    _ble_state = connected;
    portEXIT_CRITICAL(&_state_mux);
    const char *state = connected ? "on" : "off";
    esp_mqtt_client_publish(_client, _pub_topics[4], state, 0, 1, 1);
}

// ---- Ping/pong loop (called from app_task) ----

void bemfa_loop(void) {
    if (!_enabled || !_client) return;

    uint64_t now = esp_timer_get_time() / 1000000;

    // Send ping every 30s
    if (now - _last_ping_time >= INTERVAL_PING_SEND) {
        _last_ping_time = now;
        _send_ping();
    }

    // Check pong timeout (20s after ping sent)
    if (_ping_waiting && (now - _ping_send_time >= INTERVAL_PING_RECV)) {
        _ping_lost++;
        _ping_waiting = false;
        ESP_LOGW(TAG, "Ping lost (%d/%d)", _ping_lost, MAX_PING_LOST);

        if (_ping_lost == MAX_PING_LOST) {
            ESP_LOGW(TAG, "Max ping lost, reconnecting...");
            _ping_lost = 0;
            // Disconnect and let esp_mqtt_client auto-reconnect
            esp_mqtt_client_stop(_client);
            vTaskDelay(pdMS_TO_TICKS(1000));
            esp_mqtt_client_start(_client);
        }
    }
}

    }

    // Send ping every 30s
    if (now - _last_ping_time >= INTERVAL_PING_SEND) {
        _last_ping_time = now;
        _send_ping();
    }

    // Check pong timeout (20s after ping sent)
    if (_ping_waiting && (now - _ping_send_time >= INTERVAL_PING_RECV)) {
        _ping_lost++;
        _ping_waiting = false;
        ESP_LOGW(TAG, "Ping lost (%d/%d)", _ping_lost, MAX_PING_LOST);

        if (_ping_lost == MAX_PING_LOST) {
            ESP_LOGW(TAG, "Max ping lost, restarting Bemfa MQTT...");
            _ping_lost = 0;
            esp_mqtt_client_stop(_client);
            vTaskDelay(pdMS_TO_TICKS(1000));
            if (!_circuit_open) esp_mqtt_client_start(_client);
        }
    }
}
