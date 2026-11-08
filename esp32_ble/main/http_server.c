#include "http_server.h"
#include "config.h"
#include "device_state.h"
#include "ota.h"
#include "embedded_files.h"

#include "esp_http_server.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_coexist.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "lwip/sockets.h"
#include "cJSON.h"

#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "HTTP_SERVER";
static httpd_handle_t _server = NULL;
static DeviceConfig *_cfg = NULL;
static http_config_cb _on_save = NULL;

/* Browser opening a page keeps up to 6 connections per host; add the
   dashboard's polling connection and a spare for a second client. */
#define HTTPD_MAX_SOCKETS 8

/* Never let every session be owned by the async static worker.
   esp_http_server 5.3's LRU purge skips sessions with for_async_req set;
   if all sockets were async and another connection waited in the listen
   backlog, httpd_accept_conn() would return without accepting while the
   listen socket stays readable -> a spin at httpd priority that starves
   IDLE and trips the task watchdog.  Keeping two slots outside the async
   pool guarantees the LRU purge always has a session it may close. */
#define HTTPD_MAX_ASYNC_STATIC (HTTPD_MAX_SOCKETS - 2)
/* Two concurrent transfers, not six.  Every streaming socket needs a
   TCP send buffer (~3 KB) plus 1.6 KB WiFi/pbuf segments, so six flows at
   once hollowed out the heap (free heap 54 KB -> 8 KB, largest free block
   1.5 KB) while BLE held its own buffers; pbuf allocation then failed and
   every send blocked until its send timeout.  A third flow was measured
   as well: it took the cold page load from 16 s to 48 s because the extra
   pbufs no longer fit.  The remaining requests wait in the queue (they
   cost nothing while they wait) instead of timing out mid-transfer. */
#define HTTPD_STATIC_WORKERS 2
#define HTTPD_STATIC_STACK 4096
/* Bounds concurrent streaming sends (see HTTPD_STATIC_WORKERS). */
static SemaphoreHandle_t _send_gate = NULL;

static ports_writer_cb    _ports_writer = NULL;
static settings_writer_cb _settings_writer = NULL;
static get_setting_cb     _get_setting_cb = NULL;
static port_control_cb    _port_ctl_cb = NULL;
static setting_set_cb     _setting_set_cb = NULL;
static protocol_toggle_cb _proto_toggle_cb = NULL;
static ble_control_cb     _ble_ctl_cb = NULL;

void http_server_set_callbacks(ports_writer_cb ports, settings_writer_cb settings,
                               get_setting_cb get_setting,
                               port_control_cb port_ctl, setting_set_cb setting_set,
                               protocol_toggle_cb proto_toggle,
                               ble_control_cb ble_ctl) {
    _ports_writer = ports;
    _settings_writer = settings;
    _get_setting_cb = get_setting;
    _port_ctl_cb = port_ctl;
    _setting_set_cb = setting_set;
    _proto_toggle_cb = proto_toggle;
    _ble_ctl_cb = ble_ctl;
}

/* ============================================================
 * Response helpers.
 *
 * The httpd server runs a single task, so these scratch buffers are
 * not shared between handlers.  Static file bodies use the async
 * worker and never touch them.
 * ============================================================ */
static char _json_buf[3072];

static void _send_json(httpd_req_t *req, const char *buf, int len) {
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    if (len > 0 && len < (int)sizeof(_json_buf)) httpd_resp_send(req, buf, len);
    else                                          httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"buffer_full\"}");
}

static void _json_error(httpd_req_t *req, const char *msg) {
    httpd_resp_set_type(req, "application/json");
    char buf[128];
    snprintf(buf, sizeof(buf), "{\"ok\":false,\"error\":\"%s\"}", msg);
    httpd_resp_sendstr(req, buf);
}

static int _append(char *buf, size_t cap, int pos, const char *fmt, ...) {
    if (pos < 0 || (size_t)pos >= cap) return -1;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf + pos, cap - (size_t)pos, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= cap - (size_t)pos) return -1;
    return pos + n;
}

/* ==================== Ping / Health ==================== */

static int _get_ping_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_sendstr(req, "{\"ok\":true,\"service\":\"cuktech_charger_esp32\"}");
    return 0;
}

static int _get_status_handler(httpd_req_t *req) {
    bool ready = device_state_ble_ready();
    int n = snprintf(_json_buf, sizeof(_json_buf),
        "{\"ok\":true,\"connected\":%s,\"authenticated\":%s,"
        "\"ble_enabled\":%s,\"ble_ready\":%s,\"free_heap\":%u,"
        "\"min_free_heap\":%u,\"max_block\":%u,"
        "\"device_model\":\"%s\",\"firmware_version\":\"%s\"}",
        ready ? "true" : "false", ready ? "true" : "false",
        device_state_ble_enabled() ? "true" : "false",
        ready ? "true" : "false", (unsigned)esp_get_free_heap_size(),
        (unsigned)esp_get_minimum_free_heap_size(),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
        DEVICE_MODEL, FW_VERSION);
    _send_json(req, _json_buf, n);
    return 0;
}

/* ==================== Config API ==================== */

static void _mask_str(const char *in, char *out, size_t out_size) {
    size_t len = strlen(in);
    if (len <= 8 || out_size < 10) {
        strncpy(out, "****", out_size - 1);
        out[out_size - 1] = '\0';
        return;
    }
    size_t copy_front = 4;
    size_t copy_back  = 4;
    if (copy_front + 4 + copy_back >= out_size) {
        copy_front = (out_size - 5) / 2;
        copy_back  = copy_front;
    }
    memcpy(out, in, copy_front);
    memcpy(out + copy_front, "****", 4);
    memcpy(out + copy_front + 4, in + len - copy_back, copy_back);
    out[copy_front + 4 + copy_back] = '\0';
}

/* RFC 5952 text form: lowercase, no leading zeros, zero runs collapsed.
   The macro form (fe80:0000:0000:0000:...) is 39 characters and does not
   fit the status card on a phone. */
static void _ip6_str(const esp_ip6_addr_t *a, char *out, size_t n) {
    uint16_t w[8] = {
        ESP_IP6_ADDR_BLOCK1(a), ESP_IP6_ADDR_BLOCK2(a),
        ESP_IP6_ADDR_BLOCK3(a), ESP_IP6_ADDR_BLOCK4(a),
        ESP_IP6_ADDR_BLOCK5(a), ESP_IP6_ADDR_BLOCK6(a),
        ESP_IP6_ADDR_BLOCK7(a), ESP_IP6_ADDR_BLOCK8(a),
    };
    int best = -1, best_len = 1;
    for (int i = 0; i < 8; ) {
        if (w[i] != 0) { i++; continue; }
        int j = i;
        while (j < 8 && w[j] == 0) j++;
        if (j - i > best_len) { best = i; best_len = j - i; }
        i = j;
    }
    size_t p = 0;
    if (n < 8) { if (n) out[0] = '\0'; return; }
    for (int i = 0; i < 8 && p < n - 8; i++) {
        if (i == best) {
            out[p++] = ':'; out[p++] = ':';
            i += best_len - 1;
            continue;
        }
        if (p > 0 && out[p - 1] != ':') out[p++] = ':';
        p += (size_t)snprintf(out + p, n - p, "%x", (unsigned)w[i]);
    }
    out[p < n ? p : n - 1] = '\0';
}

static int _get_config_handler(httpd_req_t *req) {
    char mask_ws[16], mask_tk[16], mask_bk[16], mask_mp[16], mask_uid[16];
    _mask_str(_cfg->wifi_pass, mask_ws, sizeof(mask_ws));
    _mask_str(_cfg->device_token, mask_tk, sizeof(mask_tk));
    _mask_str(_cfg->device_ble_key, mask_bk, sizeof(mask_bk));
    _mask_str(_cfg->mqtt_pass, mask_mp, sizeof(mask_mp));
    _mask_str(_cfg->bemfa_uid, mask_uid, sizeof(mask_uid));

    /* Addresses this device currently holds, shown on the status card.
       The AP interface is reported while the device is in provisioning
       mode, and an IPv6 link-local address is shown when no global one
       has been configured yet, so the rows are never blank. */
    char ip4[16] = "", ip6[48] = "";
    esp_netif_t *nif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t ipi;
    if (nif && esp_netif_get_ip_info(nif, &ipi) == ESP_OK && ipi.ip.addr) {
        snprintf(ip4, sizeof(ip4), IPSTR, IP2STR(&ipi.ip));
    } else {
        nif = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
        if (nif && esp_netif_get_ip_info(nif, &ipi) == ESP_OK && ipi.ip.addr)
            snprintf(ip4, sizeof(ip4), IPSTR, IP2STR(&ipi.ip));
    }
    if (nif) {
        esp_ip6_addr_t a6;
        if (esp_netif_get_ip6_global(nif, &a6) == ESP_OK)
            _ip6_str(&a6, ip6, sizeof(ip6));
        else if (esp_netif_get_ip6_linklocal(nif, &a6) == ESP_OK)
            _ip6_str(&a6, ip6, sizeof(ip6));
    }

    int n = snprintf(_json_buf, sizeof(_json_buf),
        "{"
        "\"wifi_ssid\":\"%s\","
        "\"wifi_pass\":\"%s\","
        "\"device_mac\":\"%s\","
        "\"device_token\":\"%s\","
        "\"device_ble_key\":\"%s\","
        "\"mqtt_broker\":\"%s\","
        "\"mqtt_port\":%u,"
        "\"mqtt_user\":\"%s\","
        "\"mqtt_pass\":\"%s\","
        "\"mqtt_topic_prefix\":\"%s\","
        "\"mqtt_enable\":%s,"
        "\"bemfa_enable\":%s,"
        "\"bemfa_uid\":\"%s\","
        "\"bemfa_name_c1\":\"%s\","
        "\"bemfa_name_c2\":\"%s\","
        "\"bemfa_name_c3\":\"%s\","
        "\"bemfa_name_a\":\"%s\","
        "\"bemfa_name_ble\":\"%s\","
        "\"reboot_interval_sec\":%u,"
        "\"ip\":\"%s\","
        "\"ip6\":\"%s\""
        "}",
        _cfg->wifi_ssid, mask_ws,
        _cfg->device_mac, mask_tk, mask_bk,
        _cfg->mqtt_broker, (unsigned)_cfg->mqtt_port,
        _cfg->mqtt_user, mask_mp,
        _cfg->mqtt_topic_prefix,
        _cfg->mqtt_enable ? "true" : "false",
        _cfg->bemfa_enable ? "true" : "false",
        mask_uid,
        _cfg->bemfa_name_c1, _cfg->bemfa_name_c2, _cfg->bemfa_name_c3,
        _cfg->bemfa_name_a, _cfg->bemfa_name_ble,
        (unsigned)_cfg->reboot_interval_sec, ip4, ip6);
    _send_json(req, _json_buf, n);
    return 0;
}

static int _post_config_handler(httpd_req_t *req) {
    char buf[1024];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { _json_error(req, "empty"); return 0; }
    buf[len] = '\0';

    /* Rare path: parse on the heap.  The pool hooks are gone, so this
       cannot race with any other task and leaves no permanent state. */
    cJSON *root = cJSON_Parse(buf);
    if (!root) { _json_error(req, "bad_json"); return 0; }

    #define SET_STR(f, k) do { cJSON *j = cJSON_GetObjectItem(root, k); \
        if (j && cJSON_IsString(j)) { strncpy(_cfg->f, cJSON_GetStringValue(j), sizeof(_cfg->f)-1); \
            _cfg->f[sizeof(_cfg->f)-1] = '\0'; } } while(0)
    #define SET_STR_MASKED(f, k) do { cJSON *j = cJSON_GetObjectItem(root, k); \
        if (j && cJSON_IsString(j)) { const char *_v = cJSON_GetStringValue(j); \
            if (!strstr(_v, "****")) { strncpy(_cfg->f, _v, sizeof(_cfg->f)-1); \
                _cfg->f[sizeof(_cfg->f)-1] = '\0'; } \
        } \
    } while(0)
    SET_STR(wifi_ssid, "wifi_ssid"); SET_STR_MASKED(wifi_pass, "wifi_pass");
    SET_STR(device_mac, "device_mac"); SET_STR_MASKED(device_token, "device_token");
    SET_STR_MASKED(device_ble_key, "device_ble_key"); SET_STR(mqtt_broker, "mqtt_broker");
    SET_STR_MASKED(mqtt_user, "mqtt_user"); SET_STR_MASKED(mqtt_pass, "mqtt_pass");
    SET_STR(mqtt_topic_prefix, "mqtt_topic_prefix");

    char old_names[5][32];
    strncpy(old_names[0], _cfg->bemfa_name_c1, sizeof(old_names[0]) - 1);
    strncpy(old_names[1], _cfg->bemfa_name_c2, sizeof(old_names[1]) - 1);
    strncpy(old_names[2], _cfg->bemfa_name_c3, sizeof(old_names[2]) - 1);
    strncpy(old_names[3], _cfg->bemfa_name_a,  sizeof(old_names[3]) - 1);
    strncpy(old_names[4], _cfg->bemfa_name_ble, sizeof(old_names[4]) - 1);
    for (int i = 0; i < 5; i++) old_names[i][31] = '\0';

    SET_STR_MASKED(bemfa_uid, "bemfa_uid");
    SET_STR(bemfa_name_c1, "bemfa_name_c1");
    SET_STR(bemfa_name_c2, "bemfa_name_c2");
    SET_STR(bemfa_name_c3, "bemfa_name_c3");
    SET_STR(bemfa_name_a,  "bemfa_name_a");
    SET_STR(bemfa_name_ble, "bemfa_name_ble");

    {
        const char *n[5] = {_cfg->bemfa_name_c1, _cfg->bemfa_name_c2,
                            _cfg->bemfa_name_c3, _cfg->bemfa_name_a,
                            _cfg->bemfa_name_ble};
        for (int i = 0; i < 5; i++) {
            if (strcmp(old_names[i], n[i]) != 0) {
                _cfg->bemfa_modified = true;
                ESP_LOGI(TAG, "Bemfa name %d changed: \"%s\" -> \"%s\"",
                         i, old_names[i], n[i]);
                break;
            }
        }
    }

    cJSON *je = cJSON_GetObjectItem(root, "mqtt_enable");
    if (je && cJSON_IsBool(je)) _cfg->mqtt_enable = cJSON_IsTrue(je);
    cJSON *jb = cJSON_GetObjectItem(root, "bemfa_enable");
    if (jb && cJSON_IsBool(jb)) _cfg->bemfa_enable = cJSON_IsTrue(jb);
    cJSON *jp = cJSON_GetObjectItem(root, "mqtt_port");
    if (jp && cJSON_IsNumber(jp)) _cfg->mqtt_port = (uint16_t)cJSON_GetNumberValue(jp);
    cJSON *jrb = cJSON_GetObjectItem(root, "reboot_interval_sec");
    if (jrb && cJSON_IsNumber(jrb)) {
        double rb = cJSON_GetNumberValue(jrb);
        _cfg->reboot_interval_sec = (rb > 0) ? (uint32_t)rb : 0;
    }
    _cfg->valid = (_cfg->wifi_ssid[0] != '\0' && _cfg->wifi_pass[0] != '\0');
    cJSON_Delete(root);

    ESP_LOGI(TAG, "Config saved: wifi=%s mqtt=%s:%d", _cfg->wifi_ssid, _cfg->mqtt_broker, _cfg->mqtt_port);
    config_store_save(_cfg);

    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, "{\"ok\":true,\"message\":\"Config saved. Rebooting...\"}");
    if (_on_save) _on_save();
    return 0;
}

/* ==================== Dashboard API ==================== */

static int _get_ports_handler(httpd_req_t *req) {
    if (!_ports_writer) { _json_error(req, "error"); return 0; }
    int n = _ports_writer(_json_buf, sizeof(_json_buf));
    _send_json(req, _json_buf, n);
    return 0;
}

static int _get_settings_handler(httpd_req_t *req) {
    if (!_settings_writer) { _json_error(req, "error"); return 0; }
    int n = _settings_writer(_json_buf, sizeof(_json_buf));
    _send_json(req, _json_buf, n);
    return 0;
}

static int _get_all_handler(httpd_req_t *req) {
    bool ready = device_state_ble_ready();
    int pos = snprintf(_json_buf, sizeof(_json_buf),
        "{\"ok\":true,\"connected\":%s,\"authenticated\":%s,"
        "\"ble_enabled\":%s,\"ble_ready\":%s,"
        "\"free_heap\":%u,\"min_free_heap\":%u,\"max_block\":%u,"
        "\"device_model\":\"%s\",\"firmware_version\":\"%s\"",
        ready ? "true" : "false", ready ? "true" : "false",
        device_state_ble_enabled() ? "true" : "false",
        ready ? "true" : "false",
        (unsigned)esp_get_free_heap_size(),
        (unsigned)esp_get_minimum_free_heap_size(),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
        DEVICE_MODEL, FW_VERSION);
    if (pos < 0 || (size_t)pos >= sizeof(_json_buf)) {
        _json_error(req, "buffer_full");
        return 0;
    }

    if (_ports_writer) {
        if ((size_t)pos + 64 >= sizeof(_json_buf)) { _json_error(req, "buffer_full"); return 0; }
        pos = _append(_json_buf, sizeof(_json_buf), pos, ",\"ports\":");
        if (pos < 0) { _json_error(req, "buffer_full"); return 0; }
        int n = _ports_writer(_json_buf + pos, sizeof(_json_buf) - (size_t)pos - 32);
        if (n < 0) { _json_error(req, "buffer_full"); return 0; }
        pos += n;
    }
    if (_settings_writer) {
        if ((size_t)pos + 64 >= sizeof(_json_buf)) { _json_error(req, "buffer_full"); return 0; }
        pos = _append(_json_buf, sizeof(_json_buf), pos, ",\"settings\":");
        if (pos < 0) { _json_error(req, "buffer_full"); return 0; }
        int n = _settings_writer(_json_buf + pos, sizeof(_json_buf) - (size_t)pos - 2);
        if (n < 0) { _json_error(req, "buffer_full"); return 0; }
        pos += n;
    }
    pos = _append(_json_buf, sizeof(_json_buf), pos, "}");
    _send_json(req, _json_buf, pos);
    return 0;
}

/* ---- POST bodies (small, rare: cJSON on the heap is fine) ---- */

static int _post_port_handler(httpd_req_t *req) {
    char buf[256];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { _json_error(req, "error"); return 0; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { _json_error(req, "bad_json"); return 0; }
    cJSON *jp = cJSON_GetObjectItem(root, "port");
    cJSON *ja = cJSON_GetObjectItem(root, "action");
    bool ok = false;
    if (jp && ja && cJSON_IsString(jp) && cJSON_IsString(ja) && _port_ctl_cb)
        ok = _port_ctl_cb(cJSON_GetStringValue(jp), cJSON_GetStringValue(ja));
    cJSON_Delete(root);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, ok ? "{\"ok\":true}" : "{\"ok\":false}");
    return 0;
}

static int _post_setting_handler(httpd_req_t *req) {
    char buf[256];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { _json_error(req, "error"); return 0; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { _json_error(req, "bad_json"); return 0; }
    cJSON *jp = cJSON_GetObjectItem(root, "piid");
    cJSON *jv = cJSON_GetObjectItem(root, "value");
    bool ok = false;
    if (jp && jv && cJSON_IsNumber(jp) && cJSON_IsNumber(jv) && _setting_set_cb)
        ok = _setting_set_cb((int)cJSON_GetNumberValue(jp), (int)cJSON_GetNumberValue(jv));
    cJSON_Delete(root);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, ok ? "{\"ok\":true}" : "{\"ok\":false}");
    return 0;
}

static int _post_ble_handler(httpd_req_t *req) {
    char buf[128];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { _json_error(req, "error"); return 0; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { _json_error(req, "bad_json"); return 0; }
    cJSON *je = cJSON_GetObjectItem(root, "enabled");
    bool ok = false;
    if (je && cJSON_IsBool(je) && _ble_ctl_cb)
        ok = _ble_ctl_cb(cJSON_IsTrue(je));
    cJSON_Delete(root);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, ok ? "{\"ok\":true}" : "{\"ok\":false}");
    return 0;
}

static int _post_protocol_handler(httpd_req_t *req) {
    char buf[256];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { _json_error(req, "error"); return 0; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { _json_error(req, "bad_json"); return 0; }
    cJSON *jp = cJSON_GetObjectItem(root, "port");
    cJSON *jproto = cJSON_GetObjectItem(root, "protocol");
    cJSON *ja = cJSON_GetObjectItem(root, "action");
    bool ok = false;
    if (jp && jproto && ja && cJSON_IsString(jp) && cJSON_IsString(jproto) &&
        cJSON_IsString(ja) && _proto_toggle_cb) {
        ok = _proto_toggle_cb(cJSON_GetStringValue(jp), cJSON_GetStringValue(jproto),
                              strcmp(cJSON_GetStringValue(ja), "on") == 0);
    }
    cJSON_Delete(root);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, ok ? "{\"ok\":true}" : "{\"ok\":false}");
    return 0;
}

/* ==================== Sleep API ==================== */

static const char *SCR_LABELS[] = {"", "5分钟", "10分钟", "30分钟", "常亮", "1分钟"};
#define SCR_COUNT 6
#define SCR_VALID(v) ((v) >= 1 && (v) < SCR_COUNT)

static int _get_sleep_handler(httpd_req_t *req) {
    uint32_t val = 0;
    if (!_get_setting_cb || !_get_setting_cb(6, &val)) val = 0;
    int n = snprintf(_json_buf, sizeof(_json_buf),
                     "{\"value\":%lu,\"label\":\"%s\"}",
                     (unsigned long)val,
                     SCR_VALID((int)val) ? SCR_LABELS[val] : "?");
    _send_json(req, _json_buf, n);
    return 0;
}

static int _post_sleep_handler(httpd_req_t *req) {
    char buf[128];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { _json_error(req, "error"); return 0; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { _json_error(req, "bad_json"); return 0; }
    cJSON *jv = cJSON_GetObjectItem(root, "value");
    bool ok = false;
    if (jv && cJSON_IsNumber(jv) && _setting_set_cb) {
        int val = (int)cJSON_GetNumberValue(jv);
        if (SCR_VALID(val)) ok = _setting_set_cb(6, val);
    }
    cJSON_Delete(root);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, ok ? "{\"ok\":true}" : "{\"ok\":false}");
    return 0;
}

/* ==================== Embedded static files ==================== */

static const EmbeddedFile *_find_embedded(const char *path) {
    for (const EmbeddedFile *f = embedded_files; f->path; f++) {
        if (strcmp(f->path, path) == 0) return f;
    }
    return NULL;
}

/* Send an embedded file.  Runs either in the async worker (preferred)
   or inline if the worker queue is full.  No artificial inter-chunk
   delay: httpd_resp_send_chunk() already blocks on the socket and the
   send timeout bounds the wait. */
/* The peer stopped reading (closed tab, dead WiFi link).  Left alone, the
   half-written response keeps its pcb retransmitting for minutes; each such
   connection pins its send buffer, the heap falls to a few KB and no new
   connection can even be accepted (observed: whole server silent until
   reboot).  SO_LINGER(0) makes the queued close send an RST, so lwIP frees
   the pcb and its buffers at once. */
static void _abort_socket(int sock) {
    struct linger lg = { .l_onoff = 1, .l_linger = 0 };
    setsockopt(sock, SOL_SOCKET, SO_LINGER, &lg, sizeof(lg));
    if (_server) httpd_sess_trigger_close(_server, sock);
}

static void _serve_embedded(httpd_req_t *req, const EmbeddedFile *f) {
    esp_coex_preference_set(ESP_COEX_PREFER_WIFI);
    int sock = httpd_req_to_sockfd(req);

    /* Hard cap on streams even though only two workers exist: the inline
       fallback path below also reaches this function from the httpd task. */
    bool gated = (_send_gate && xSemaphoreTake(_send_gate, pdMS_TO_TICKS(2000)) == pdTRUE);

    httpd_resp_set_type(req, f->content_type);
    if (f->encoding) {
        httpd_resp_set_hdr(req, "Content-Encoding", f->encoding);
    }
    /* Images are content-addressed by their (new) filenames and can be
       cached for a week.  HTML/JS/CSS must revalidate: after an OTA the
       old cached page would still point at removed asset names. */
    bool is_image = (strncmp(f->content_type, "image/", 6) == 0);
    httpd_resp_set_hdr(req, "Cache-Control", is_image ? "max-age=604800" : "no-cache");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

    /* Content hash of the embedded bytes: unchanged assets can be
       answered with 304, so a revisit only costs a header round-trip
       instead of re-sending ~90 KB of JS/CSS over WiFi. */
    char etag[16];
    snprintf(etag, sizeof(etag), "\"%08" PRIx32 "\"", f->crc);
    httpd_resp_set_hdr(req, "ETag", etag);

    const uint8_t *p = f->data;
    size_t left = f->size;
    /* Exactly one MSS per send: lwIP then queues a single 1.6 KB pbuf per
       chunk instead of copying into a 4 KB send buffer, so a stalled peer
       parks ~1.5 KB rather than 4 KB of heap. */
    const size_t CHUNK = 1460;
    esp_err_t err = ESP_OK;
    while (left > 0) {
        size_t n = (left > CHUNK) ? CHUNK : left;
        err = httpd_resp_send_chunk(req, (const char *)p, n);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Send chunk failed for %s (%d bytes left): %s — dropping connection",
                     f->path, (int)left, esp_err_to_name(err));
            _abort_socket(sock);
            break;
        }
        p += n;
        left -= n;
    }
    if (err == ESP_OK) httpd_resp_send_chunk(req, NULL, 0);
    if (gated) xSemaphoreGive(_send_gate);
    esp_coex_preference_set(ESP_COEX_PREFER_WIFI);
}

/* Async static worker: frees the single httpd task as soon as the URI
   has been resolved, so dashboards polling /api/all are never queued
   behind a 500 KB script/image transfer. */
typedef struct {
    httpd_req_t *req;
    const EmbeddedFile *file;
} static_job_t;

static QueueHandle_t _static_queue = NULL;
/* Jobs handed to the async worker and not yet completed.  Only the httpd
   task increments, only the workers decrement, so a plain counter is
   enough on this single-core target. */
static volatile int _async_inflight = 0;

/* httpd_req_async_handler_complete() clears the session's async flag but
   does not wake the server's select() loop, which built its descriptor set
   while the socket was still owned by this worker.  Any keep-alive request
   that arrived in the meantime therefore sits unread until some unrelated
   event (a new connection, a control message) wakes the loop — measured as
   multi-second to minute-long stalls while the browser reuses its six
   connections.  Post a no-op work item so the loop runs select() again. */
static void _wake_httpd(void *arg) {
    (void)arg;
}

static void _static_worker(void *arg) {
    (void)arg;
    static_job_t job;
    for (;;) {
        if (xQueueReceive(_static_queue, &job, portMAX_DELAY) != pdTRUE) continue;
        if (job.req && job.file) _serve_embedded(job.req, job.file);
        if (job.req) {
            httpd_req_async_handler_complete(job.req);
            _async_inflight--;
            if (_server) httpd_queue_work(_server, _wake_httpd, NULL);
        }
    }
}

/* Resolve the file, then hand the request to the worker.  If async
   handling is unavailable (or the async pool is full), fall back to
   serving inline so the page still works. */
static void _dispatch_embedded(httpd_req_t *req, const EmbeddedFile *f) {
    if (!f) {
        httpd_resp_set_type(req, "application/json");
        httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"not_found\"}");
        return;
    }

    /* Conditional GET.  Answering 304 keeps a revisit cheap: text assets
       are revalidated on every load ("no-cache"), and images after their
       week of freshness runs out. */
    char inm[48];
    if (httpd_req_get_hdr_value_str(req, "If-None-Match", inm, sizeof(inm)) == ESP_OK) {
        char etag[16];
        snprintf(etag, sizeof(etag), "\"%08" PRIx32 "\"", f->crc);
        if (strcmp(inm, etag) == 0) {
            bool is_image = (strncmp(f->content_type, "image/", 6) == 0);
            httpd_resp_set_status(req, "304 Not Modified");
            httpd_resp_set_type(req, f->content_type);
            httpd_resp_set_hdr(req, "ETag", etag);
            httpd_resp_set_hdr(req, "Cache-Control", is_image ? "max-age=604800" : "no-cache");
            httpd_resp_send(req, NULL, 0);
            return;
        }
    }

    if (_static_queue && _async_inflight < HTTPD_MAX_ASYNC_STATIC) {
        httpd_req_t *copy = NULL;
        if (httpd_req_async_handler_begin(req, &copy) == ESP_OK) {
            static_job_t job = { copy, f };
            _async_inflight++;
            if (xQueueSend(_static_queue, &job, 0) == pdTRUE) return;
            _async_inflight--;
            httpd_req_async_handler_complete(copy);
        }
    }
    ESP_LOGD(TAG, "inline serve: %s", f->path);
    _serve_embedded(req, f);
}

/* ==================== Dashboard / config page ==================== */

static int _get_dash_handler(httpd_req_t *req) {
    bool is_config = true;
    if (_cfg && _cfg->valid) {
        esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        if (sta) {
            esp_netif_ip_info_t ip;
            if (esp_netif_get_ip_info(sta, &ip) == ESP_OK && ip.ip.addr != 0)
                is_config = false;
        }
    }
    _dispatch_embedded(req, _find_embedded(is_config ? "/config.html" : "/phone.html"));
    return 0;
}

static int _get_config_page_handler(httpd_req_t *req) {
    _dispatch_embedded(req, _find_embedded("/config.html"));
    return 0;
}

static int _get_static_handler(httpd_req_t *req) {
    /* req->uri may include a query string (e.g. ?v=2); match on the
       path portion only. */
    char path[192];
    const char *qmark = strchr(req->uri, '?');
    size_t plen = qmark ? (size_t)(qmark - req->uri) : strlen(req->uri);
    if (plen >= sizeof(path)) plen = sizeof(path) - 1;
    memcpy(path, req->uri, plen);
    path[plen] = '\0';

    if (strcmp(path, "/") == 0 || strcmp(path, "/dashboard") == 0)
        return _get_dash_handler(req);
    if (strcmp(path, "/config") == 0)
        return _get_config_page_handler(req);

    const EmbeddedFile *f = _find_embedded(path);
    if (!f) f = _find_embedded("/phone.html");   /* SPA fallback */
    _dispatch_embedded(req, f);
    return 0;
}

static int _get_ota_handler(httpd_req_t *req) {    int n = ota_status_json(_json_buf, sizeof(_json_buf));
    _send_json(req, _json_buf, n);
    return 0;
}

/* ==================== Server start ==================== */

/* Nagle is on by default in lwIP.  For the many small (20-70 KB) asset
   responses this device serves, Nagle waits for the peer's delayed ACK
   before releasing the last segment, which caps each transfer at roughly
   send-buffer/RTT.  Disabling it restores full throughput. */
static esp_err_t _httpd_open_fn(httpd_handle_t hd, int sockfd) {
    (void)hd;
    int nodelay = 1;
    setsockopt(sockfd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
    return ESP_OK;
}

void http_server_start(DeviceConfig *cfg, http_config_cb on_save) {
    _cfg = cfg;
    _on_save = on_save;

    if (!_send_gate) _send_gate = xSemaphoreCreateCounting(HTTPD_STATIC_WORKERS, HTTPD_STATIC_WORKERS);
    if (!_static_queue) {
        /* At most HTTPD_MAX_ASYNC_STATIC jobs can be in flight, so the
           queue can never overflow while a request is in flight. */
        _static_queue = xQueueCreate(HTTPD_MAX_ASYNC_STATIC, sizeof(static_job_t));
        if (_static_queue) {
            for (int i = 0; i < HTTPD_STATIC_WORKERS; i++) {
                if (xTaskCreate(_static_worker, "http_static", HTTPD_STATIC_STACK, NULL, 2, NULL) != pdPASS) {
                    ESP_LOGW(TAG, "Only %d static worker(s) started", i);
                    break;
                }
            }
        }
    }

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 24;
    config.max_open_sockets = HTTPD_MAX_SOCKETS;
    config.server_port = 80;
    /* max_resp_headers is a COUNT, not a byte budget: 10 slots cover
       Content-Type/CORS/Cache-Control/Content-Encoding with room to
       spare, and each socket allocates only ~80 bytes for them. */
    config.max_resp_headers = 10;
    config.stack_size = 6144;
    config.lru_purge_enable = true;
    config.keep_alive_enable = false;  /* TCP keepalive races with the browser reusing idle sockets */
    config.keep_alive_idle = 5;
    config.keep_alive_interval = 1;
    config.keep_alive_count = 3;
    /* A peer that stopped reading (tab closed, phone asleep) used to hold a
       static worker for the full 10 s before the socket was aborted, which
       with two workers stalled everyone else behind it.  Four seconds is
       far longer than any healthy ACK on this link and bounds the damage. */
    config.send_wait_timeout = 4;
    config.recv_wait_timeout = 5;
    config.uri_match_fn = httpd_uri_match_wildcard;
    config.open_fn = _httpd_open_fn;

    if (httpd_start(&_server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start HTTP server");
        return;
    }

    const httpd_uri_t uris[] = {
        { .uri = "/api/ping",     .method = HTTP_GET,  .handler = _get_ping_handler },
        { .uri = "/api/status",   .method = HTTP_GET,  .handler = _get_status_handler },
        { .uri = "/api/all",      .method = HTTP_GET,  .handler = _get_all_handler },
        { .uri = "/api/config",   .method = HTTP_GET,  .handler = _get_config_handler },
        { .uri = "/api/config",   .method = HTTP_POST, .handler = _post_config_handler },
        { .uri = "/api/ports",    .method = HTTP_GET,  .handler = _get_ports_handler },
        { .uri = "/api/settings", .method = HTTP_GET,  .handler = _get_settings_handler },
        { .uri = "/api/port",     .method = HTTP_POST, .handler = _post_port_handler },
        { .uri = "/api/setting",  .method = HTTP_POST, .handler = _post_setting_handler },
        { .uri = "/api/protocol", .method = HTTP_POST, .handler = _post_protocol_handler },
        { .uri = "/api/ble",      .method = HTTP_POST, .handler = _post_ble_handler },
        { .uri = "/api/sleep",    .method = HTTP_GET,  .handler = _get_sleep_handler },
        { .uri = "/api/sleep",    .method = HTTP_POST, .handler = _post_sleep_handler },
        { .uri = "/api/ota",      .method = HTTP_GET,  .handler = _get_ota_handler },
        { .uri = "/api/ota",      .method = HTTP_POST, .handler = ota_http_post_handler },
        /* Dashboard, config page and every embedded asset are served by
           one wildcard handler, registered last. */
        { .uri = "/*",            .method = HTTP_GET,  .handler = _get_static_handler },
    };
    for (int i = 0; i < (int)(sizeof(uris)/sizeof(uris[0])); i++) {
        esp_err_t err = httpd_register_uri_handler(_server, &uris[i]);
        if (err != ESP_OK) ESP_LOGW(TAG, "Failed to register %s: %s",
                                    uris[i].uri, esp_err_to_name(err));
    }
    ESP_LOGI(TAG, "HTTP server started on port 80 (keep-alive, %d assets)",
             (int)(sizeof(embedded_files)/sizeof(embedded_files[0]) - 1));
}

void http_server_stop(void) {
    if (_server) { httpd_stop(_server); _server = NULL; }
}
