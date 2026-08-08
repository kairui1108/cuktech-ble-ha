#include "config_store.h"
#include "config.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"
#include <string.h>

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

/* 与 _str 相同，但当 NVS 中无值时用 default_value 代替空串 */
static void _str_def(nvs_handle_t h, const char *k, char *buf, size_t max, const char *def) {
    size_t len = max;
    if (nvs_get_str(h, k, buf, &len) != ESP_OK) {
        strncpy(buf, def, max - 1);
        buf[max - 1] = '\0';
    }
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
        .bemfa_enable = false,
        .bemfa_name_c1 = "C口1开关",
        .bemfa_name_c2 = "C口2开关",
        .bemfa_name_c3 = "C口3开关",
        .bemfa_name_a = "USB-A开关",
        .bemfa_name_ble = "蓝牙开关",
        .bemfa_modified = false,
        .reboot_interval_sec = 0,  
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

    /* 读取时用 DEFAULT_* 作为回退，避免空密码导致 4-way 握手超时 */
    _str_def(h, "wifi_ssid", cfg->wifi_ssid, sizeof(cfg->wifi_ssid), DEFAULT_WIFI_SSID);
    _str_def(h, "wifi_pass", cfg->wifi_pass, sizeof(cfg->wifi_pass), DEFAULT_WIFI_PASS);
    _str_def(h, "dev_mac",   cfg->device_mac,   sizeof(cfg->device_mac),   DEFAULT_DEVICE_MAC);
    _str_def(h, "dev_token", cfg->device_token, sizeof(cfg->device_token), DEFAULT_DEVICE_TOKEN);
    _str_def(h, "dev_key",   cfg->device_ble_key, sizeof(cfg->device_ble_key), DEFAULT_DEVICE_BLE_KEY);
    _str_def(h, "mqtt_broker", cfg->mqtt_broker, sizeof(cfg->mqtt_broker), DEFAULT_MQTT_BROKER);
    _u16(h, "mqtt_port", &cfg->mqtt_port);
    _str_def(h, "mqtt_user", cfg->mqtt_user, sizeof(cfg->mqtt_user), DEFAULT_MQTT_USER);
    _str_def(h, "mqtt_pass", cfg->mqtt_pass, sizeof(cfg->mqtt_pass), DEFAULT_MQTT_PASS);
    _str_def(h, "mqtt_topic", cfg->mqtt_topic_prefix, sizeof(cfg->mqtt_topic_prefix), DEFAULT_MQTT_TOPIC_PREFIX);
    uint8_t en = 1;
    if (nvs_get_u8(h, "mqtt_en", &en) == ESP_OK) cfg->mqtt_enable = (en != 0);
    else cfg->mqtt_enable = DEFAULT_MQTT_ENABLE;

    uint8_t bemfa_en = 0;
    if (nvs_get_u8(h, "bemfa_en", &bemfa_en) == ESP_OK) cfg->bemfa_enable = (bemfa_en != 0);
    _str_def(h, "bemfa_uid", cfg->bemfa_uid, sizeof(cfg->bemfa_uid), "");
    _str_def(h, "bemfa_n_c1", cfg->bemfa_name_c1, sizeof(cfg->bemfa_name_c1), "C口1开关");
    _str_def(h, "bemfa_n_c2", cfg->bemfa_name_c2, sizeof(cfg->bemfa_name_c2), "C口2开关");
    _str_def(h, "bemfa_n_c3", cfg->bemfa_name_c3, sizeof(cfg->bemfa_name_c3), "C口3开关");
    _str_def(h, "bemfa_n_a",  cfg->bemfa_name_a,  sizeof(cfg->bemfa_name_a),  "USB-A开关");
    _str_def(h, "bemfa_n_bl", cfg->bemfa_name_ble, sizeof(cfg->bemfa_name_ble), "蓝牙开关");
    uint8_t mod = 0;
    if (nvs_get_u8(h, "bemfa_mod", &mod) == ESP_OK) cfg->bemfa_modified = (mod != 0);

    uint32_t rb_int = 0;
    if (nvs_get_u32(h, "reboot_int", &rb_int) == ESP_OK) cfg->reboot_interval_sec = rb_int;
    else cfg->reboot_interval_sec = 0;

    cfg->valid = (cfg->wifi_ssid[0] != '\0' && cfg->wifi_pass[0] != '\0');
    nvs_close(h);
    ESP_LOGI(TAG, "Config loaded: wifi=%s mqtt=%s:%d device=%s bemfa=%s",
             cfg->wifi_ssid, cfg->mqtt_broker, cfg->mqtt_port, cfg->device_mac,
             cfg->bemfa_enable ? "on" : "off");
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
    nvs_set_u8(h, "bemfa_en", cfg->bemfa_enable ? 1 : 0);
    nvs_set_str(h, "bemfa_uid", cfg->bemfa_uid);
    nvs_set_str(h, "bemfa_n_c1", cfg->bemfa_name_c1);
    nvs_set_str(h, "bemfa_n_c2", cfg->bemfa_name_c2);
    nvs_set_str(h, "bemfa_n_c3", cfg->bemfa_name_c3);
    nvs_set_str(h, "bemfa_n_a",  cfg->bemfa_name_a);
    nvs_set_str(h, "bemfa_n_bl", cfg->bemfa_name_ble);
    nvs_set_u8(h, "bemfa_mod", cfg->bemfa_modified ? 1 : 0);
    nvs_set_u32(h, "reboot_int", cfg->reboot_interval_sec);
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
