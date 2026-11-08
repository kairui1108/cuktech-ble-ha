#pragma once

#include <stddef.h>
#include "esp_http_server.h"

/* ============================================================
 * OTA module.
 *
 * Firmware upload is handled asynchronously: the httpd task only
 * takes ownership of the request and queues it; a dedicated task
 * streams the image to the inactive OTA slot.  This keeps the
 * dashboard responsive if someone uploads while the page is open.
 *
 * POST /api/ota with Content-Length and a raw .bin body.
 * GET  /api/ota returns progress/state.
 *
 * The partition table must contain ota_0/ota_1; on devices shipped
 * with the old single-factory table a one-time USB reflash is needed.
 * ============================================================ */

void ota_init(void);
int  ota_status_json(char *buf, size_t cap);
int  ota_http_post_handler(httpd_req_t *req);
