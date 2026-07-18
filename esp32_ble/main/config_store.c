#include "config_store.h"
#include "config.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"

static const char *TAG = "CONFIG_STORE";
static const char *NVS_NAMESPACE = "device_cfg";

void config_store_init(void) {
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
}

static void _str(nvs_handle_t h, const char *k, char *buf, size_t max) {
    size_t len = max;
    if (nvs_get_str(h, k, buf, &len) != ESP_OK) buf[0] = '\0';
}

static void _u16(nvs_handle_t h, const char *k, uint16_t *val) {
    if (nvs_get_u16(h, k, val) != ESP_OK) *val = 0;
}

void config_store_apply_defaults(DeviceConfig *cfg) {
    *cfg = (DeviceConfig){
        .wifi_ssid = DEFAULT_WIFI_SSID,
        .wifi_pass = DEFAULT_WIFI_PASS,
        .device_mac = DEFAULT_DEVICE_MAC,
        .device_token = DEFAULT_DEVICE_TOKEN,
        .device_ble_key = DEFAULT_DEVICE_BLE_KEY,
        .mqtt_broker = DEFAULT_MQTT_BROKER,
        .mqtt_port = DEFAULT_MQTT_PORT,
        .mqtt_user = DEFAULT_MQTT_USER,
        .mqtt_pass = DEFAULT_MQTT_PASS,
        .mqtt_topic_prefix = DEFAULT_MQTT_TOPIC_PREFIX,
        .mqtt_enable = DEFAULT_MQTT_ENABLE,
        .valid = false,
    };
}

bool config_store_load(DeviceConfig *cfg) {
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGI(TAG, "No saved config, using defaults");
        config_store_apply_defaults(cfg);
        return false;
    }

    _str(h, "wifi_ssid", cfg->wifi_ssid, sizeof(cfg->wifi_ssid));
    _str(h, "wifi_pass", cfg->wifi_pass, sizeof(cfg->wifi_pass));
    _str(h, "dev_mac", cfg->device_mac, sizeof(cfg->device_mac));
    _str(h, "dev_token", cfg->device_token, sizeof(cfg->device_token));
    _str(h, "dev_key", cfg->device_ble_key, sizeof(cfg->device_ble_key));
    _str(h, "mqtt_broker", cfg->mqtt_broker, sizeof(cfg->mqtt_broker));
    _u16(h, "mqtt_port", &cfg->mqtt_port);
    _str(h, "mqtt_user", cfg->mqtt_user, sizeof(cfg->mqtt_user));
    _str(h, "mqtt_pass", cfg->mqtt_pass, sizeof(cfg->mqtt_pass));
    _str(h, "mqtt_topic", cfg->mqtt_topic_prefix, sizeof(cfg->mqtt_topic_prefix));
    uint8_t en = 1;
    if (nvs_get_u8(h, "mqtt_en", &en) == ESP_OK) cfg->mqtt_enable = (en != 0);
    else cfg->mqtt_enable = DEFAULT_MQTT_ENABLE;

    cfg->valid = (cfg->wifi_ssid[0] != '\0' && cfg->device_mac[0] != '\0');
    nvs_close(h);
    ESP_LOGI(TAG, "Config loaded: wifi=%s mqtt=%s:%d device=%s",
             cfg->wifi_ssid, cfg->mqtt_broker, cfg->mqtt_port, cfg->device_mac);
    return cfg->valid;
}

bool config_store_save(const DeviceConfig *cfg) {
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS");
        return false;
    }
    nvs_set_str(h, "wifi_ssid", cfg->wifi_ssid);
    nvs_set_str(h, "wifi_pass", cfg->wifi_pass);
    nvs_set_str(h, "dev_mac", cfg->device_mac);
    nvs_set_str(h, "dev_token", cfg->device_token);
    nvs_set_str(h, "dev_key", cfg->device_ble_key);
    nvs_set_str(h, "mqtt_broker", cfg->mqtt_broker);
    nvs_set_u16(h, "mqtt_port", cfg->mqtt_port);
    nvs_set_str(h, "mqtt_user", cfg->mqtt_user);
    nvs_set_str(h, "mqtt_pass", cfg->mqtt_pass);
    nvs_set_str(h, "mqtt_topic", cfg->mqtt_topic_prefix);
    nvs_set_u8(h, "mqtt_en", cfg->mqtt_enable ? 1 : 0);
    esp_err_t err = nvs_commit(h);
    nvs_close(h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "NVS commit failed: %s", esp_err_to_name(err));
        return false;
    }
    ESP_LOGI(TAG, "Config saved");
    return true;
}

bool config_store_is_configured(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return false;
    char buf[33] = {0};
    size_t len = sizeof(buf);
    bool ok = (nvs_get_str(h, "wifi_ssid", buf, &len) == ESP_OK && buf[0] != '\0');
    nvs_close(h);
    return ok;
}

void config_store_erase(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
        nvs_erase_all(h);
        nvs_commit(h);
        nvs_close(h);
    }
    ESP_LOGW(TAG, "Config erased");
}
