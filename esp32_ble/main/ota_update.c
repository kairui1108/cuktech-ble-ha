#include "ota_update.h"
#include "esp_ota_ops.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_http_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "OTA";

static void _ota_task(void *arg) {
    char *url = (char *)arg;
    ESP_LOGI(TAG, "Starting OTA from: %s", url);

    esp_http_client_config_t http_cfg = {
        .url = url,
        .timeout_ms = 30000,
    };

    esp_err_t err = esp_https_ota(&http_cfg);
    free(url);

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "OTA succeeded, rebooting...");
        esp_ota_mark_app_valid_cancel_rollback();
        esp_restart();
    } else {
        ESP_LOGE(TAG, "OTA failed: %s", esp_err_to_name(err));
    }
    vTaskDelete(NULL);
}

void ota_update_init(void) {
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(NULL, &state) == ESP_OK) {
        if (state == ESP_OTA_IMG_PENDING_VERIFY) {
            ESP_LOGI(TAG, "OTA image verified, marking valid");
            esp_ota_mark_app_valid_cancel_rollback();
        }
    }
}

bool ota_update_start(const char *url) {
    if (!url || url[0] == '\0') return false;
    char *url_copy = strdup(url);
    if (!url_copy) return false;
    BaseType_t ret = xTaskCreate(_ota_task, "ota", 8192, url_copy, 1, NULL);
    if (ret != pdPASS) {
        free(url_copy);
        return false;
    }
    return true;
}
