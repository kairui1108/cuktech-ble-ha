#include "ota.h"

#include "config.h"
#include "device_state.h"
#include "uplink.h"
#include "queue_msg.h"

#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include <string.h>
#include <stdio.h>
#include <stdbool.h>

static const char *TAG = "OTA";

/* Single upload at a time.  The worker is the only writer of the
   state strings; HTTP readers only ever see a complete pointer. */
static volatile bool s_busy = false;
static volatile int  s_progress = -1;
static volatile const char *s_state = "idle";

static QueueHandle_t s_queue = NULL;
static uint8_t s_buf[4096];

static bool _run_ota_inner(httpd_req_t *req) {
    const esp_partition_t *update = esp_ota_get_next_update_partition(NULL);
    if (!update) {
        ESP_LOGE(TAG, "No inactive OTA partition (partition table mismatch)");
        s_state = "no_partition";
        return false;
    }

    size_t total = (size_t)req->content_len;
    if (total == 0 || total > update->size) {
        ESP_LOGW(TAG, "Bad image size: %u (slot=%u)",
                 (unsigned)total, (unsigned)update->size);
        s_state = "bad_size";
        return false;
    }
    ESP_LOGI(TAG, "OTA start: %u bytes -> %s @0x%lx",
             (unsigned)total, update->label, (unsigned long)update->address);

    /* Free the 2.4 GHz radio as much as possible: stop BLE reconnects
       and disconnect.  The BLE task performs the actual NimBLE call. */
    device_state_set_ble_enabled(false);
    if (urgent_queue) {
        BleCommand cmd = {CMD_BLE, 0, 0, 0};
        xQueueSend(urgent_queue, &cmd, 0);
    }
    uplink_publish_status();
    vTaskDelay(pdMS_TO_TICKS(500));

    s_state = "starting";
    s_progress = 0;

    esp_ota_handle_t handle = 0;
    esp_err_t err = esp_ota_begin(update, OTA_WITH_SEQUENTIAL_WRITES, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin: %s", esp_err_to_name(err));
        s_state = "begin_failed";
        return false;
    }

    size_t written = 0;
    int timeouts = 0;
    s_state = "receiving";

    while (written < total) {
        size_t want = total - written;
        if (want > sizeof(s_buf)) want = sizeof(s_buf);

        int n = httpd_req_recv(req, (char *)s_buf, want);
        if (n == HTTPD_SOCK_ERR_TIMEOUT) {
            if (++timeouts > 10) {
                ESP_LOGE(TAG, "OTA receive timed out at %u/%u",
                         (unsigned)written, (unsigned)total);
                s_state = "recv_timeout";
                err = ESP_FAIL;
                break;
            }
            continue;
        }
        if (n <= 0) {
            ESP_LOGE(TAG, "OTA receive failed at %u/%u (n=%d)",
                     (unsigned)written, (unsigned)total, n);
            s_state = "recv_failed";
            err = ESP_FAIL;
            break;
        }
        timeouts = 0;

        err = esp_ota_write(handle, s_buf, (size_t)n);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_write: %s", esp_err_to_name(err));
            s_state = "write_failed";
            break;
        }
        written += (size_t)n;
        s_progress = (int)((written * 100) / total);
    }

    if (err != ESP_OK || written != total) {
        if (written != total && err == ESP_OK) s_state = "incomplete";
        esp_ota_abort(handle);
        return false;
    }

    s_state = "verifying";
    err = esp_ota_end(handle);          /* frees the handle on both paths */
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_end: %s", esp_err_to_name(err));
        s_state = "validate_failed";
        return false;
    }

    err = esp_ota_set_boot_partition(update);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_set_boot_partition: %s", esp_err_to_name(err));
        s_state = "boot_set_failed";
        return false;
    }

    ESP_LOGI(TAG, "OTA image valid, boot partition set to %s", update->label);
    s_progress = 100;
    s_state = "done";
    return true;
}

static bool _run_ota(httpd_req_t *req) {
    bool ble_was_enabled = device_state_ble_enabled();
    bool ok = _run_ota_inner(req);

    /* A failed upload must not leave BLE switched off.  A successful
       one reboots immediately, so only the failure path restores it. */
    if (!ok && ble_was_enabled) {
        device_state_set_ble_enabled(true);
        if (urgent_queue) {
            BleCommand cmd = {CMD_BLE, 0, 1, 0};
            xQueueSend(urgent_queue, &cmd, 0);
        }
        uplink_publish_status();
    }
    return ok;
}

static void _ota_task(void *arg) {
    (void)arg;
    httpd_req_t *req = NULL;
    for (;;) {
        if (xQueueReceive(s_queue, &req, portMAX_DELAY) != pdTRUE || !req) continue;

        bool ok = _run_ota(req);

        char msg[192];
        if (ok) {
            snprintf(msg, sizeof(msg),
                     "{\"ok\":true,\"message\":\"Update complete. Rebooting...\"}");
        } else {
            snprintf(msg, sizeof(msg),
                     "{\"ok\":false,\"error\":\"%s\"}", (const char *)s_state);
        }
        httpd_resp_set_type(req, "application/json");
        httpd_resp_sendstr(req, msg);
        httpd_req_async_handler_complete(req);
        req = NULL;

        if (ok) {
            ESP_LOGI(TAG, "Rebooting into the new image in 1.5s");
            vTaskDelay(pdMS_TO_TICKS(1500));
            esp_restart();
            /* not reached */
        }

        s_progress = -1;
        s_state = "idle";
        s_busy = false;
    }
}

void ota_init(void) {
    if (!s_queue) {
        s_queue = xQueueCreate(1, sizeof(httpd_req_t *));
        if (s_queue) {
            xTaskCreate(_ota_task, "ota", 6144, NULL, 3, NULL);
        } else {
            ESP_LOGE(TAG, "Failed to create OTA queue");
            return;
        }
    }

    const esp_partition_t *run = esp_ota_get_running_partition();
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    ESP_LOGI(TAG, "Running=%s @0x%lx, next=%s",
             run ? run->label : "?", run ? (unsigned long)run->address : 0,
             next ? next->label : "-");

#if CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
    if (run) {
        esp_ota_img_states_t st;
        if (esp_ota_get_state_partition(run, &st) == ESP_OK) {
            ESP_LOGI(TAG, "Rollback state: %d (mark-valid after 60s uptime)", (int)st);
        }
    }
#endif
}

int ota_status_json(char *buf, size_t cap) {
    const esp_partition_t *run = esp_ota_get_running_partition();
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    return snprintf(buf, cap,
        "{\"ok\":true,\"available\":%s,\"state\":\"%s\",\"in_progress\":%s,\"progress\":%d,"
        "\"running\":\"%s\",\"next\":\"%s\",\"version\":\"%s\"}",
        s_queue ? "true" : "false",
        (const char *)s_state, s_busy ? "true" : "false", s_progress,
        run ? run->label : "?", next ? next->label : "-", FW_VERSION);
}

int ota_http_post_handler(httpd_req_t *req) {
    if (!s_queue || s_busy) {
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_set_type(req, "application/json");
        httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"ota_busy\"}");
        return 0;
    }

    s_busy = true;
    s_progress = 0;
    s_state = "waiting";

    httpd_req_t *copy = NULL;
    esp_err_t err = httpd_req_async_handler_begin(req, &copy);
    if (err != ESP_OK) {
        s_busy = false;
        s_progress = -1;
        s_state = "idle";
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "async handler unavailable");
        return 0;
    }

    if (xQueueSend(s_queue, &copy, 0) != pdTRUE) {
        /* Ownership of the request is with the copy once begin() succeeds,
           so the response must be sent on the copy before completing it. */
        httpd_resp_set_status(copy, "503 Service Unavailable");
        httpd_resp_set_type(copy, "application/json");
        httpd_resp_sendstr(copy, "{\"ok\":false,\"error\":\"ota_busy\"}");
        httpd_req_async_handler_complete(copy);
        s_busy = false;
        s_progress = -1;
        s_state = "idle";
        return 0;
    }
    return 0;
}
