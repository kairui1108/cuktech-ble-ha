#include "ble_manager.h"
#include "config.h"
#include "miot_protocol.h"
#include "miot_auth.h"
#include "queue_msg.h"

#include <string.h>
#include <stdio.h>
#include <math.h>
#include "esp_log.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs_id.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static const char *TAG = "BLE";

/* Forward declarations */
static void _set_state(BLEState s);
static void _scan(void);
static void _discover(void);
static void _handle_cmd_recv(void);
static void _handle_cmd_send(void);
static void _pending_check_timeouts(void);
static void _keepalive(void);
static void _reconnect(void);
static void _disable_all_notifications(void);
static int _gap_event(struct ble_gap_event *event, void *arg);
static int _disc_svc_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                         const struct ble_gatt_svc *service, void *arg);
static int _disc_chr_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                         const struct ble_gatt_chr *chr, void *arg);

#define NOTIF_ITEM_SIZE  256
#define NOTIF_QUEUE_LEN  8

typedef struct { uint8_t data[NOTIF_ITEM_SIZE]; size_t len; uint16_t conn_handle, attr_handle; } NotifItem;
typedef struct { float voltage, current, power; uint8_t protocol, status; bool active; } PortData;

static BLEState _state = BLE_IDLE;
static StateCallback _state_cb = NULL;
static PortDataCallback _port_data_cb = NULL;
static uint16_t _conn_handle = 0xFFFF;
static bool _connected = false;
static uint16_t _auth_ctrl_handle = 0, _auth_data_handle = 0, _cmd_send_handle = 0, _cmd_recv_handle = 0;
static QueueHandle_t _q_auth_ctrl = NULL, _q_auth_data = NULL, _q_cmd_send = NULL, _q_cmd_recv = NULL;
static SessionKeys _keys = {};
static uint32_t _send_it = 0;
static uint8_t _seq = 1;
static PortData _ports[4] = {};
static uint32_t _settings[32] = {};
static bool _settings_valid[32] = {};
static uint32_t _ra = 0;
static uint64_t _last_keepalive = 0;
static SemaphoreHandle_t _disc_sem = NULL, _connected_sem = NULL, _disconnect_sem = NULL;
static uint16_t _disc_service_start = 0, _disc_service_end = 0;
static uint8_t _target_addr[6] = {}, _token[12] = {};
static char _mac_str[18] = {};
static bool _nimble_ready = false;
static volatile bool _enabled = true;

typedef struct { uint16_t uuid; uint16_t *handle; const char *name; } CharCtx;
static CharCtx _char_ctx[4];
static int _char_ctx_n = 0;

/* ============================================================
 * Async pending command table
 * ============================================================ */
#define MAX_PENDING 8
#define CIPHER_BUF_SIZE 128

typedef enum { PENDING_GET, PENDING_SET } PendingType;
typedef enum { SEND_IDLE, SEND_AWAIT_RDY, SEND_AWAIT_OK, SEND_AWAIT_RESP } SendPhase;

typedef struct {
    bool      in_use;
    uint8_t   seq;
    uint8_t   piid;
    PendingType type;
    uint32_t  send_value;
    uint32_t  poll_seq;
    uint64_t  deadline;
    bool      acked;
    bool      no_result;
    SendPhase phase;
    uint8_t   ciphertext[CIPHER_BUF_SIZE];  // [1,0] + encrypted data
    size_t    ct_len;
} PendingEntry;

static PendingEntry _pending[MAX_PENDING] = {};

static void _pending_check_timeouts(void) {
    uint64_t now = esp_timer_get_time() / 1000;
    for (int i = 0; i < MAX_PENDING; i++) {
        if (!_pending[i].in_use || now < _pending[i].deadline) continue;
        ESP_LOGW(TAG, "Pending %s seq=%d piid=%d timed out",
                 _pending[i].type == PENDING_GET ? "GET" : "SET",
                 _pending[i].seq, _pending[i].piid);
        BleResult r;
        if (_pending[i].type == PENDING_GET) {
            r = (BleResult){RES_GET, false, _pending[i].piid, 0, _pending[i].poll_seq};
        } else {
            r = (BleResult){RES_SET, false, _pending[i].piid, _pending[i].send_value, 0};
        }
        xQueueSend(result_queue, &r, 0);
        _pending[i].in_use = false;
    }
}

static void _pending_match(const uint8_t *resp, size_t rl) {
    if (rl < 6) return;
    uint8_t seq = resp[2];
    uint8_t op  = resp[4];
    for (int i = 0; i < MAX_PENDING; i++) {
        if (!_pending[i].in_use || _pending[i].seq != seq) continue;
        if (_pending[i].type == PENDING_GET && op == 0x03) {
            uint32_t val = 0;
            uint8_t vlen = (rl >= 12) ? resp[11] : 0;
            if (vlen >= 4 && rl >= 17)
                val = resp[13] | (resp[14] << 8) | (resp[15] << 16) | (resp[16] << 24);
            else if (vlen >= 1 && rl >= 14)
                val = resp[13];
            BleResult r = {RES_GET, true, _pending[i].piid, val, _pending[i].poll_seq};
            xQueueSend(result_queue, &r, 0);
            _pending[i].in_use = false;
            return;
        }
        if (_pending[i].type == PENDING_SET && op == 0x01) {
            _pending[i].acked = true;
            return;
        }
        if (_pending[i].type == PENDING_SET && op == 0x04) {
            if (!_pending[i].no_result) {
                bool piid_ok = (rl >= 8 && resp[7] == _pending[i].piid);
                BleResult r = {RES_SET, piid_ok, _pending[i].piid, _pending[i].send_value, 0};
                xQueueSend(result_queue, &r, 0);
            }
            _pending[i].in_use = false;
            return;
        }
    }
}

/* ============================================================
 * NimBLE sync / state machine
 * ============================================================ */
static void _nimble_on_sync(void) {
    ESP_LOGI(TAG, "NimBLE host synced");
    ble_hs_util_ensure_addr(0);
    vTaskDelay(pdMS_TO_TICKS(500));
    _nimble_ready = true;
    ESP_LOGI(TAG, "NimBLE ready");
}

void ble_manager_loop(void) {
    if (!_nimble_ready) return;
    switch (_state) {
    case BLE_SCANNING:     _scan(); break;
    case BLE_CONNECTING:
        if (xSemaphoreTake(_connected_sem, pdMS_TO_TICKS(100)) == pdTRUE) _discover();
        break;
    case BLE_READY:
        _handle_cmd_recv();
        _handle_cmd_send();
        _pending_check_timeouts();
        if ((esp_timer_get_time() / 1000) - _last_keepalive >= KEEPALIVE_INTERVAL_MS) _keepalive();
        break;
    case BLE_RECONNECT: _reconnect(); break;
    default: break;
    }
}

static void _nimble_host_task(void *param) {
    ESP_LOGI(TAG, "NimBLE host task started");
    nimble_port_run();
    ESP_LOGI(TAG, "NimBLE host task done");
    nimble_port_freertos_deinit();
    vTaskDelete(NULL);
}

static void _set_state(BLEState s) {
    if (s == BLE_RECONNECT && !_enabled) s = BLE_IDLE;
    if (s == BLE_SCANNING && !_enabled) s = BLE_IDLE;
    BLEState old = _state;
    _state = s;
    ESP_LOGI(TAG, "State: %d -> %d", (int)old, (int)s);
    if (_state_cb) _state_cb(old, s);
}

static void _dispatch_notif(uint16_t attr_handle, const uint8_t *data, size_t len) {
    NotifItem item;
    item.conn_handle = _conn_handle;
    item.attr_handle = attr_handle;
    item.len = (len > NOTIF_ITEM_SIZE) ? NOTIF_ITEM_SIZE : len;
    memcpy(item.data, data, item.len);

    QueueHandle_t q = NULL;
    if (attr_handle == _auth_ctrl_handle)  q = _q_auth_ctrl;
    else if (attr_handle == _auth_data_handle) q = _q_auth_data;
    else if (attr_handle == _cmd_send_handle)  q = _q_cmd_send;
    else if (attr_handle == _cmd_recv_handle)  q = _q_cmd_recv;

    if (q) {
        if (xQueueSend(q, &item, 0) != pdTRUE) {
            ESP_LOGW(TAG, "Queue full for handle 0x%04X, dropping", attr_handle);
        }
    }
}

/* ============================================================
 * Queue helpers (non-blocking)
 * ============================================================ */
static bool _wait_queue(QueueHandle_t q, uint8_t *buf, size_t *len, uint32_t ms) {
    NotifItem item;
    if (xQueueReceive(q, &item, pdMS_TO_TICKS(ms)) != pdTRUE) return false;
    size_t n = (item.len > 256) ? 256 : item.len;
    memcpy(buf, item.data, n);
    *len = n;
    return true;
}

static bool _pop_cmd_recv(uint8_t *buf, size_t *len) {
    return _wait_queue(_q_cmd_recv, buf, len, 0);
}

static bool _pop_cmd_send(uint8_t *buf, size_t *len) {
    return _wait_queue(_q_cmd_send, buf, len, 0);
}

static void _drain_all_queues(void) {
    NotifItem item;
    QueueHandle_t qs[] = {_q_auth_ctrl, _q_auth_data, _q_cmd_send, _q_cmd_recv};
    for (int i = 0; i < 4; i++) while (xQueueReceive(qs[i], &item, 0) == pdTRUE) {}
}

/* ============================================================
 * BLE write helpers
 * ============================================================ */
static bool _wru_nr(uint16_t handle, const uint8_t *data, size_t len) {
    if (!_connected || handle == 0) return false;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(data, len);
    if (!om) return false;
    return ble_gattc_write_no_rsp(_conn_handle, handle, om) == 0;
}

static SemaphoreHandle_t _op_sem = NULL;
static int _op_rc = 0;
static uint32_t _op_seq = 0, _op_expected_seq = 0;

static int _gatt_write_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                           struct ble_gatt_attr *attr, void *arg) {
    uint32_t cb_seq = (uint32_t)(uintptr_t)arg;
    if (cb_seq != _op_expected_seq) return 0;
    _op_rc = (error != NULL) ? error->status : 0;
    if (_op_sem) xSemaphoreGive(_op_sem);
    return 0;
}

static bool _wru(uint16_t handle, const uint8_t *data, size_t len) {
    if (!_connected || handle == 0) return false;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(data, len);
    if (!om) return false;
    _op_rc = -1;
    _op_expected_seq = ++_op_seq;
    xSemaphoreTake(_op_sem, 0);
    int rc = ble_gattc_write(_conn_handle, handle, om, _gatt_write_cb,
                             (void*)(uintptr_t)_op_expected_seq);
    if (rc != 0) return false;
    if (xSemaphoreTake(_op_sem, pdMS_TO_TICKS(3000)) != pdTRUE) return false;
    return _op_rc == 0;
}

/* ============================================================
 * Port data parsing + protocol estimation
 * ============================================================ */
static const float _PD_FV[] = {5.0f, 9.0f, 12.0f, 15.0f, 20.0f};

static float _min_dist(float voltage) {
    float d = fabsf(voltage - _PD_FV[0]);
    for (int i = 1; i < 5; i++) { float nd = fabsf(voltage - _PD_FV[i]); if (nd < d) d = nd; }
    return d;
}

static uint8_t _estimate_pd_subtype(float voltage) {
    float md = _min_dist(voltage);
    if (voltage < 12.0f) { if (md <= 0.05f) return 7; return 8; }
    if (md <= 0.3f) return 7;
    if (voltage >= 3.0f && voltage <= 21.0f) return 8;
    return 7;
}

static uint8_t _estimate_proto(uint8_t piid, float voltage, uint8_t code) {
    if (piid == 1 || piid == 2) {
        // PD 关闭时端口只能输出 5V (对齐 Python state_protocol_v2.py)
        int pd_bit = (piid == 1) ? 0 : 8;
        bool pd_enabled = (_settings_valid[21]) ? ((_settings[21] >> pd_bit) & 1) : true;
        if (!pd_enabled && voltage > 0) return 1; // 5V
        if (code == 0x08) return 8;
        if (code == 0x70) { float md = _min_dist(voltage); return (md <= 0.3f) ? 7 : 3; }
        if (code == 0x01 || code == 0x03 || code == 0x04 || code == 0x05 ||
            code == 0x06 || code == 0x07 || code == 0x0A || code == 0x0B || code == 0x30)
            return _estimate_pd_subtype(voltage);
        float md = _min_dist(voltage);
        if (md <= 0.5f) return 7;
        if (voltage >= 3.0f && voltage <= 21.0f) return 8;
        return 0;
    }
    if (piid == 3) {
        if (code == 0x70) return 3;
        if (voltage >= 15.0f) return 7;
        if (voltage >= 8.5f) return 3;
        if (voltage <= 5.5f) return 1;
        return voltage > 6.0f ? 3 : 1;
    }
    if (piid == 4) {
        if (code == 0x70) return 3;
        if (voltage > 5.5f) return 3;
        if (voltage > 0) return 1;
    }
    return 0;
}

static void _parse_port(uint8_t piid, const uint8_t *pt, size_t pt_len) {
    if (piid < 1 || piid > 4 || pt_len < 12) return;
    uint32_t val = pt[pt_len-4] | (pt[pt_len-3] << 8) |
                   (pt[pt_len-2] << 16) | (pt[pt_len-1] << 24);
    uint8_t idx = piid - 1;
    _ports[idx].voltage = ((val >> 24) & 0xFF) / 10.0f;
    _ports[idx].current = ((val >> 16) & 0xFF) / 10.0f;
    _ports[idx].protocol = _estimate_proto(piid, _ports[idx].voltage, (val >> 8) & 0xFF);
    _ports[idx].status = val & 0xFF;
    _ports[idx].power = _ports[idx].voltage * _ports[idx].current;
    _ports[idx].active = (_ports[idx].status != 0) || (_ports[idx].voltage > 0.5f);
    ESP_LOGI(TAG, "Port%d: V=%.1f I=%.1f P=%.1f proto=%d st=0x%02X",
             piid, _ports[idx].voltage, _ports[idx].current,
             _ports[idx].power, _ports[idx].protocol, _ports[idx].status);
    if (result_queue) {
        BleResult r = {RES_PORT_PUSH, true, piid, 0, 0,
                       _ports[idx].voltage, _ports[idx].current, _ports[idx].power,
                       _ports[idx].protocol, _ports[idx].status, false};
        if (xQueueSend(result_queue, &r, 0) != pdTRUE)
            ESP_LOGW(TAG, "result_queue full, dropping push");
    }
}

/* ============================================================
 * Auth helpers (still synchronous — only used during auth flow)
 * ============================================================ */
static bool _recv_auth(uint8_t *out, size_t *out_len, uint32_t ms) {
    uint64_t start = esp_timer_get_time() / 1000;
    uint8_t buf[256]; size_t blen;
    while ((esp_timer_get_time() / 1000 - start) < ms) {
        uint32_t rem = ms - (uint32_t)(esp_timer_get_time() / 1000 - start);
        if (!_wait_queue(_q_auth_data, buf, &blen, (rem > 3000) ? 3000 : rem)) break;
        if (blen < 4) continue;
        uint8_t atype = buf[2];
        if (atype == 0x01) continue;
        if ((atype == 0x02 || atype == 0x04) && blen >= 4) {
            size_t pl = blen - 4;
            if (pl > 256) pl = 256;
            memcpy(out, buf + 4, pl); *out_len = pl;
            _wru_nr(_auth_data_handle, (uint8_t[]){0,0,3,0}, 4);
            return true;
        }
        if (atype == 0x00 && blen >= 6) {
            uint16_t cnt = buf[4] | (buf[5] << 8);
            if (cnt > 100) cnt = 100;
            _wru_nr(_auth_data_handle, (uint8_t[]){0,0,1,1}, 4);
            size_t total = 0;
            for (uint16_t i = 0; i < cnt && total < 508; i++) {
                NotifItem item;
                if (xQueueReceive(_q_auth_data, &item, pdMS_TO_TICKS(3000)) != pdTRUE) break;
                if (item.len > 2) {
                    size_t cp = (item.len - 2 < 508 - total) ? (item.len - 2) : (508 - total);
                    memcpy(out + total, item.data + 2, cp); total += cp;
                }
            }
            _wru_nr(_auth_data_handle, (uint8_t[]){0,0,1,0}, 4);
            *out_len = total; return total > 0;
        }
    }
    *out_len = 0; return false;
}

static bool _recv_auth_raw(uint8_t *out, size_t *out_len, uint32_t ms) {
    uint64_t start = esp_timer_get_time() / 1000;
    while ((esp_timer_get_time() / 1000 - start) < ms) {
        uint32_t rem = ms - (uint32_t)(esp_timer_get_time() / 1000 - start);
        if (!_wait_queue(_q_auth_data, out, out_len, (rem > 3000) ? 3000 : rem)) break;
        if (*out_len < 4) continue;
        if (out[2] == 0x01) continue;
        if (out[2] == 0x02 || out[2] == 0x04) return true;
    }
    *out_len = 0; return false;
}

static bool _wait_notif_auth(const uint8_t *expected, size_t elen, uint32_t ms) {
    uint8_t buf[256]; size_t blen;
    uint64_t deadline = esp_timer_get_time() / 1000 + ms;
    while ((esp_timer_get_time() / 1000) < deadline) {
        uint32_t rem = deadline - (esp_timer_get_time() / 1000);
        if (!_wait_queue(_q_auth_data, buf, &blen, (rem > 3000) ? 3000 : rem)) break;
        if (blen == elen && memcmp(buf, expected, elen) == 0) return true;
    }
    return false;
}

static void _drain_auth_queue(void) {
    NotifItem item;
    while (xQueueReceive(_q_auth_data, &item, 0) == pdTRUE) {}
}

/* ============================================================
 * Auth flow (synchronous — used only during initial auth)
 * ============================================================ */
static bool _auth_sync_send_ctrl(const uint8_t *data, size_t len) {
    return _wru_nr(_auth_ctrl_handle, data, len);
}

static bool _auth_sync_send_data(const uint8_t *data, size_t len) {
    return _wru_nr(_auth_data_handle, data, len);
}

static void _auth(void) {
    ESP_LOGI(TAG, "Auth start");
    uint8_t buf[512]; size_t blen;
    uint8_t rand_key[16], dev_random[16], dev_hmac[32], our_hmac[32];
    SessionKeys keys = {};

    ESP_LOGI(TAG, "Phase A: init (0xA4)");
    _drain_auth_queue();
    if (!_auth_sync_send_ctrl((uint8_t[]){0xA4}, 1)) goto fail;
    if (!_recv_auth_raw(buf, &blen, 3000) || blen < 4) { ESP_LOGE(TAG, "Phase A: no init response"); goto fail; }
    ESP_LOGI(TAG, "Phase A: type=0x%02X len=%d", buf[2], (int)blen);
    buf[2]++; _auth_sync_send_data(buf, blen);

    uint64_t pa_deadline = esp_timer_get_time() / 1000 + 8000;
    bool got_key_data = false;
    while ((esp_timer_get_time() / 1000) < pa_deadline) {
        uint32_t rem = pa_deadline - (esp_timer_get_time() / 1000);
        if (!_recv_auth_raw(buf, &blen, (rem > 3000) ? 3000 : rem)) break;
        if (blen < 4) continue;
        if (buf[2] == 0x04 && blen >= 20) { got_key_data = true; break; }
    }
    if (!got_key_data) { ESP_LOGE(TAG, "Phase A: no key exchange data"); goto fail; }

    size_t pad_len = (blen > 4) ? (blen - 4) : 0;
    uint8_t placeholder[512] = {0, 0, 5, 1};
    memset(placeholder + 4, 0xF2, pad_len);
    _auth_sync_send_data(placeholder, 4 + pad_len);
    vTaskDelay(pdMS_TO_TICKS(600)); _drain_auth_queue();

    ESP_LOGI(TAG, "Phase B: key exchange");
    vTaskDelay(pdMS_TO_TICKS(50));
    _auth_sync_send_ctrl((uint8_t[]){0x24, 0, 0, 0}, 4);

    esp_fill_random(rand_key, 16);
    _auth_sync_send_data((uint8_t[]){0, 0, 0, 0x0B, 1, 0}, 6);

    uint8_t rcv_rdy[] = {0, 0, 1, 1};
    bool got_rdy = false;
    for (int r = 0; r < 5; r++) { if (_wait_notif_auth(rcv_rdy, 4, 3000)) { got_rdy = true; break; } }
    if (!got_rdy) { ESP_LOGE(TAG, "Phase B: no RCV_RDY"); goto fail; }

    uint8_t key_frame[18] = {1, 0};
    memcpy(key_frame + 2, rand_key, 16);
    _auth_sync_send_data(key_frame, 18);

    uint8_t rcv_ok[] = {0, 0, 1, 0};
    if (!_wait_notif_auth(rcv_ok, 4, 3000)) { ESP_LOGE(TAG, "Phase B: no RCV_OK"); goto fail; }
    if (!_recv_auth(buf, &blen, 3000) || blen < 16) { ESP_LOGE(TAG, "Phase B: no dev key"); goto fail; }
    memcpy(dev_random, buf, 16);
    if (!_recv_auth(buf, &blen, 3000) || blen < 32) { ESP_LOGE(TAG, "Phase B: no dev HMAC"); goto fail; }
    memcpy(dev_hmac, buf, 32);

    uint8_t salt[32]; memcpy(salt, rand_key, 16); memcpy(salt + 16, dev_random, 16);
    uint8_t salt_inv[32]; memcpy(salt_inv, dev_random, 16); memcpy(salt_inv + 16, rand_key, 16);
    if (!derive_session_keys(_token, 12, rand_key, dev_random, &keys)) goto fail;

    uint8_t expected_hmac[32];
    if (!hmac_sha256(keys.dev_key, 16, salt_inv, 32, expected_hmac)) goto fail;
    if (memcmp(expected_hmac, dev_hmac, 32) != 0) { ESP_LOGE(TAG, "HMAC mismatch"); goto fail; }
    ESP_LOGI(TAG, "HMAC verified OK");

    ESP_LOGI(TAG, "Phase C: send HMAC");
    if (!hmac_sha256(keys.app_key, 16, salt, 32, our_hmac)) goto fail;

    _auth_sync_send_data((uint8_t[]){0, 0, 0, 0x0A, 1, 0}, 6);
    got_rdy = false;
    for (int r = 0; r < 5; r++) { if (_wait_notif_auth(rcv_rdy, 4, 3000)) { got_rdy = true; break; } }
    if (!got_rdy) { ESP_LOGE(TAG, "Phase C: no RCV_RDY"); goto fail; }

    uint8_t hmac_frame[34] = {1, 0};
    memcpy(hmac_frame + 2, our_hmac, 32);
    _auth_sync_send_data(hmac_frame, 34);
    _wait_notif_auth(rcv_ok, 4, 3000);

    ESP_LOGI(TAG, "Waiting auth result...");
    uint64_t deadline = esp_timer_get_time() / 1000 + 8000;
    while ((esp_timer_get_time() / 1000) < deadline) {
        uint32_t rem = deadline - (esp_timer_get_time() / 1000);
        if (!_wait_queue(_q_auth_ctrl, buf, &blen, (rem > 3000) ? 3000 : rem)) break;
        if (blen >= 1) {
            uint8_t code = buf[0];
            ESP_LOGI(TAG, "Auth result: 0x%02X", code);
            if (code == AUTH_SUCCESS || code == AUTH_ACTIVATE) {
                _keys = keys; _send_it = 0; _seq = 1; _ra = 0;
                _set_state(BLE_READY); _last_keepalive = esp_timer_get_time() / 1000;
                ESP_LOGI(TAG, "Auth OK!");
                return;
            }
            ESP_LOGW(TAG, "Auth failed: 0x%02X", code); goto fail;
        }
    }
    ESP_LOGE(TAG, "Auth timeout");
fail:
    ESP_LOGI(TAG, "Auth failed, disconnect + wait 3s");
    ble_manager_disconnect();
    vTaskDelay(pdMS_TO_TICKS(3000));
    _set_state(BLE_RECONNECT);
}

/* ============================================================
 * Handle CMD_RECV notifications (push + async response matching)
 * ============================================================ */
static void _process_decrypted(const uint8_t *pt, size_t pt_len) {
    if (pt_len >= 12 && pt[4] == 0x04 && pt[7] >= 1 && pt[7] <= 4)
        _parse_port(pt[7], pt, pt_len);   // port push
    else
        _pending_match(pt, pt_len);        // command response
}

static void _handle_cmd_recv(void) {
    uint8_t buf[256]; size_t blen;
    // Process ALL available items (not just one)
    while (_pop_cmd_recv(buf, &blen)) {
        if (!_connected) return;

        if (buf[2] == 0x00 && blen >= 6) {
            // Multi-frame
            uint16_t cnt = buf[4] + (buf[5] << 8);
            if (cnt > 100) cnt = 100;
            _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,1,1}, 4);
            uint8_t tmp[512]; size_t t = 0;
            for (uint16_t i = 0; i < cnt; i++) {
                NotifItem item;
                if (xQueueReceive(_q_cmd_recv, &item, pdMS_TO_TICKS(3000)) != pdTRUE) break;
                if (item.data[2] == 0x02 && item.len >= 3) {
                    size_t cp = (item.len - 2 < 508 - t) ? (item.len - 2) : (508 - t);
                    memcpy(tmp + t, item.data + 2, cp); t += cp;
                }
            }
            _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,1,0}, 4);
            if (t > 0) {
                size_t pt_len;
                if (decrypt_response(&_keys, tmp, t, tmp, &pt_len))
                    _process_decrypted(tmp, pt_len);
            }
        } else if (buf[2] == 0x02 && blen >= 4) {
            size_t pt_len;
            if (decrypt_response(&_keys, buf + 4, blen - 4, buf + 4, &pt_len)) {
                _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,3,0}, 4);
                _process_decrypted(buf + 4, pt_len);
            }
        }
    }
}

/* ============================================================
 * Handle CMD_SEND notifications (async send state machine)
 * ============================================================ */
static void _handle_cmd_send(void) {
    uint8_t buf[256]; size_t blen;
    while (_pop_cmd_send(buf, &blen)) {
        if (blen != 4) continue;

        // RCV_RDY [0,0,1,1] → write ciphertext from first pending SEND_AWAIT_RDY
        if (buf[2] == 0x01 && buf[3] == 0x01) {
            for (int i = 0; i < MAX_PENDING; i++) {
                if (_pending[i].in_use && _pending[i].phase == SEND_AWAIT_RDY) {
                    _wru_nr(_cmd_send_handle, _pending[i].ciphertext, _pending[i].ct_len);
                    _pending[i].phase = SEND_AWAIT_OK;
                    break;
                }
            }
        }
        // RCV_OK [0,0,1,0] → command fully sent
        else if (buf[2] == 0x01 && buf[3] == 0x00) {
            for (int i = 0; i < MAX_PENDING; i++) {
                if (_pending[i].in_use && _pending[i].phase == SEND_AWAIT_OK) {
                    _pending[i].phase = SEND_AWAIT_RESP;
                    break;
                }
            }
        }
    }
}

/* ============================================================
 * Async command send
 * ============================================================ */
static bool _send_enc_start(const uint8_t *pt, size_t pt_len, uint8_t *enc_out, size_t *enc_len) {
    if (!encrypt_command(&_keys, &_send_it, pt, pt_len, enc_out, enc_len)) return false;
    // Write CMD HEADER [0,0,0,0,1,0] — tells device to expect one encrypted frame
    return _wru_nr(_cmd_send_handle, (uint8_t[]){0, 0, 0, 0, 1, 0}, 6);
}

static bool _alloc_pending(uint8_t seq, uint8_t piid, PendingType type,
                            uint32_t send_value, uint32_t poll_seq, uint64_t timeout_ms,
                            bool no_result, const uint8_t *ct, size_t ct_len) {
    for (int i = 0; i < MAX_PENDING; i++) {
        if (_pending[i].in_use) continue;
        _pending[i] = (PendingEntry){
            .in_use = true, .seq = seq, .piid = piid, .type = type,
            .send_value = send_value, .poll_seq = poll_seq,
            .deadline = (esp_timer_get_time() / 1000) + timeout_ms,
            .no_result = no_result, .phase = SEND_AWAIT_RDY, .ct_len = ct_len,
        };
        memcpy(_pending[i].ciphertext, ct, ct_len);
        return true;
    }
    ESP_LOGW(TAG, "Pending table full");
    return false;
}

static bool _send_get_async(uint8_t piid, uint32_t poll_seq) {
    uint8_t buf[16] = {0};
    uint8_t req_seq = _seq;
    buf[0] = 12; buf[1] = 0x20; buf[2] = req_seq; buf[3] = 0;
    buf[4] = 0x02; buf[5] = 0x01; buf[6] = SIID_CHARGER;
    buf[7] = piid; buf[8] = 0; buf[9] = 0x01; buf[10] = 0x10; buf[11] = 0;
    _seq = (_seq + 1) & 0xFF;

    uint8_t enc[CIPHER_BUF_SIZE]; size_t el;
    if (!_send_enc_start(buf, 12, enc, &el)) return false;
    return _alloc_pending(req_seq, piid, PENDING_GET, 0, poll_seq, 2500, false, enc, el);
}

static bool _send_set_async(uint8_t piid, uint32_t value, uint32_t poll_seq, bool no_result) {
    uint8_t buf[16] = {0};
    uint8_t byte_len, tl_lo, tl_hi;
    if (value <= 0xFF) { byte_len = 1; tl_lo = 0x01; tl_hi = 0x10; }
    else { byte_len = 4; tl_lo = 0x04; tl_hi = 0x50; }
    int total_len = 11 + byte_len;
    uint8_t req_seq = _seq;
    buf[0] = total_len; buf[1] = 0x20; buf[2] = req_seq; buf[3] = 0;
    buf[4] = 0x00; buf[5] = 0x01; buf[6] = SIID_CHARGER;
    buf[7] = piid; buf[8] = 0; buf[9] = tl_lo; buf[10] = tl_hi;
    buf[11] = value & 0xFF;
    if (byte_len >= 4) { buf[12] = (value >> 8) & 0xFF; buf[13] = (value >> 16) & 0xFF; buf[14] = (value >> 24) & 0xFF; }
    _seq = (_seq + 1) & 0xFF;

    uint8_t enc[CIPHER_BUF_SIZE]; size_t el;
    if (!_send_enc_start(buf, total_len, enc, &el)) return false;
    return _alloc_pending(req_seq, piid, PENDING_SET, value, poll_seq, 4000, no_result, enc, el);
}

/* ============================================================
 * Sync wrappers (for CMD_PORT and auth flow)
 * ============================================================ */
static void _drain_cmd_recv_pushes(void) {
    NotifItem item;
    while (xQueueReceive(_q_cmd_recv, &item, 0) == pdTRUE) {
        if (item.data[2] == 0x02 && item.len >= 4) {
            size_t tlen = 0; uint8_t pt[256];
            if (decrypt_response(&_keys, item.data + 4, item.len - 4, pt, &tlen)) {
                if (tlen >= 12 && pt[4] == 0x04 && pt[7] >= 1 && pt[7] <= 4) {
                    _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,3,0}, 4);
                    _parse_port(pt[7], pt, tlen);
                } else {
                    _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,3,0}, 4);
                }
            }
        }
    }
}

static bool _wait_cmd_send(uint8_t *buf, size_t *blen, uint32_t ms) {
    uint64_t deadline = esp_timer_get_time() / 1000 + ms;
    while ((esp_timer_get_time() / 1000) < deadline) {
        if (_pop_cmd_send(buf, blen)) return true;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return false;
}

static bool _send_enc_sync(const uint8_t *pt, size_t pt_len) {
    uint8_t enc[512]; size_t el;
    if (!encrypt_command(&_keys, &_send_it, pt, pt_len, enc, &el)) return false;
    if (!_wru_nr(_cmd_send_handle, (uint8_t[]){0,0,0,0,1,0}, 6)) return false;
    uint8_t buf[256]; size_t blen;
    if (!_wait_cmd_send(buf, &blen, 3000)) { ESP_LOGW(TAG, "CMD_SEND: no RCV_RDY"); return false; }
    if (blen != 4 || buf[2] != 1 || buf[3] != 1) { ESP_LOGW(TAG, "RCV_RDY mismatch"); return false; }
    uint8_t f[2+512] = {1,0}; memcpy(f + 2, enc, el);
    if (!_wru_nr(_cmd_send_handle, f, 2 + el)) return false;
    if (!_wait_cmd_send(buf, &blen, 3000)) { ESP_LOGW(TAG, "CMD_SEND: no RCV_OK"); return false; }
    if (blen != 4 || buf[2] != 1 || buf[3] != 0) { ESP_LOGW(TAG, "RCV_OK mismatch"); return false; }
    return true;
}

static bool _recv_cmd(uint8_t *out, size_t *ol, uint32_t ms) {
    uint64_t start = esp_timer_get_time() / 1000;
    uint8_t buf[256]; size_t blen;
    while ((esp_timer_get_time() / 1000 - start) < ms) {
        uint32_t rem = ms - (uint32_t)(esp_timer_get_time() / 1000 - start);
        if (!_wait_queue(_q_cmd_recv, buf, &blen, (rem > 2000) ? 2000 : rem)) break;
        if (blen >= 4 && buf[2] == 0x01) continue;
        if (buf[2] == 0x02 && blen >= 4) {
            uint8_t tmp[256]; size_t tlen = 0;
            if (!decrypt_response(&_keys, buf + 4, blen - 4, tmp, &tlen)) continue;
            if (tlen >= 12 && tmp[4] == 0x04 && tmp[7] >= 1 && tmp[7] <= 4) {
                _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,3,0}, 4);
                _parse_port(tmp[7], tmp, tlen); continue;
            }
            _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,3,0}, 4);
            *ol = (tlen > 256) ? 256 : tlen; memcpy(out, tmp, *ol); return true;
        }
        if (buf[2] == 0x00 && blen >= 6) {
            uint16_t cnt = buf[4] + (buf[5] << 8);
            _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,1,1}, 4);
            uint8_t tmp[512]; size_t t = 0;
            for (uint16_t i = 0; i < cnt && t < 508; i++) {
                if (!_wait_queue(_q_cmd_recv, buf, &blen, 3000)) break;
                if (buf[2] == 0x02) { size_t cp = (blen - 2 < 508 - t) ? (blen - 2) : (508 - t); memcpy(tmp + t, buf + 2, cp); t += cp; }
            }
            _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,1,0}, 4);
            if (t > 0) { size_t pl; if (decrypt_response(&_keys, tmp, t, tmp, &pl)) { *ol = (pl > 256) ? 256 : pl; memcpy(out, tmp, *ol); return true; } }
        }
    }
    return false;
}

bool ble_manager_miot_get(uint8_t piid, uint32_t* value) {
    _drain_cmd_recv_pushes();
    uint8_t buf[16] = {0};
    buf[0] = 12; buf[1] = 0x20; buf[2] = _seq; buf[3] = 0;
    buf[4] = 0x02; buf[5] = 0x01; buf[6] = SIID_CHARGER;
    buf[7] = piid; buf[8] = 0; buf[9] = 0x01; buf[10] = 0x10; buf[11] = 0;
    _seq = (_seq + 1) & 0xFF;
    if (!_send_enc_sync(buf, 12)) return false;
    uint8_t resp[256]; size_t rl = 0;
    if (!_recv_cmd(resp, &rl, 8000)) return false;
    if (rl < 14 || resp[4] != 0x03 || resp[7] != piid) return false;
    uint8_t vlen = resp[11];
    if (vlen >= 4 && rl >= 17) *value = resp[13] | (resp[14] << 8) | (resp[15] << 16) | (resp[16] << 24);
    else if (vlen >= 1 && rl >= 14) *value = resp[13];
    else *value = 0;
    return true;
}

bool ble_manager_miot_set(uint8_t piid, uint32_t value) {
    _drain_cmd_recv_pushes();
    uint8_t buf[16] = {0};
    uint8_t byte_len, tl_lo, tl_hi;
    if (value <= 0xFF) { byte_len = 1; tl_lo = 0x01; tl_hi = 0x10; }
    else { byte_len = 4; tl_lo = 0x04; tl_hi = 0x50; }
    int total_len = 11 + byte_len;
    buf[0] = total_len; buf[1] = 0x20; buf[2] = _seq; buf[3] = 0;
    buf[4] = 0x00; buf[5] = 0x01; buf[6] = SIID_CHARGER;
    buf[7] = piid; buf[8] = 0; buf[9] = tl_lo; buf[10] = tl_hi;
    buf[11] = value & 0xFF;
    if (byte_len >= 4) { buf[12] = (value >> 8) & 0xFF; buf[13] = (value >> 16) & 0xFF; buf[14] = (value >> 24) & 0xFF; }
    _seq = (_seq + 1) & 0xFF;
    if (!_send_enc_sync(buf, total_len)) return false;
    uint8_t resp[256]; size_t rl = 0;
    if (!_recv_cmd(resp, &rl, 3000)) return false;
    bool ok = false;
    if (rl >= 6 && resp[4] == 0x01) {
        ok = true;
        if (_recv_cmd(resp, &rl, 2000)) { if (rl >= 8 && resp[4] == 0x04) ok = (resp[7] == piid); }
    } else if (rl >= 8 && resp[4] == 0x04) { ok = (resp[7] == piid); }
    NotifItem item;
    QueueHandle_t qs[] = {_q_auth_ctrl, _q_auth_data, _q_cmd_send, _q_cmd_recv};
    for (int i = 0; i < 4; i++) while (xQueueReceive(qs[i], &item, 0) == pdTRUE) {}
    return ok;
}


/* ============================================================
 * BLE lifecycle: scan, discover, disconnect, reconnect, keepalive
 * ============================================================ */
static void _scan(void) {
    xSemaphoreTake(_connected_sem, 0);
    struct ble_gap_disc_params dp = {}; dp.itvl = 0x60; dp.window = 0x60;
    ESP_LOGI(TAG, "Scanning for %s...", _mac_str);
    int rc = ble_gap_disc(BLE_OWN_ADDR_PUBLIC, 10 * 100, &dp, _gap_event, NULL);
    if (rc != 0) { ESP_LOGE(TAG, "ble_gap_disc failed: %d", rc); vTaskDelay(pdMS_TO_TICKS(1000)); }
    else _set_state(BLE_CONNECTING);
}

static void _discover(void) {
    ESP_LOGI(TAG, "Discovering...");
    _disc_service_start = 0; _disc_service_end = 0;
    xSemaphoreTake(_disc_sem, 0);
    ble_gattc_disc_all_svcs(_conn_handle, _disc_svc_cb, NULL);
    if (xSemaphoreTake(_disc_sem, pdMS_TO_TICKS(5000)) != pdTRUE || _disc_service_start == 0) {
        ESP_LOGE(TAG, "Service discovery failed"); _set_state(BLE_RECONNECT); return;
    }
    _char_ctx[0] = (CharCtx){CHAR_UUID_AUTH_CTRL, &_auth_ctrl_handle, "auth_ctrl"};
    _char_ctx[1] = (CharCtx){CHAR_UUID_AUTH_DATA, &_auth_data_handle, "auth_data"};
    _char_ctx[2] = (CharCtx){CHAR_UUID_CMD_SEND,  &_cmd_send_handle,  "cmd_send"};
    _char_ctx[3] = (CharCtx){CHAR_UUID_CMD_RECV,  &_cmd_recv_handle,  "cmd_recv"};
    _char_ctx_n = 4;
    xSemaphoreTake(_disc_sem, 0);
    ble_gattc_disc_all_chrs(_conn_handle, _disc_service_start, _disc_service_end, _disc_chr_cb, NULL);
    if (xSemaphoreTake(_disc_sem, pdMS_TO_TICKS(5000)) != pdTRUE) ESP_LOGW(TAG, "Char disc timeout");
    ESP_LOGI(TAG, "handles: ctrl=0x%04X data=0x%04X send=0x%04X recv=0x%04X",
             _auth_ctrl_handle, _auth_data_handle, _cmd_send_handle, _cmd_recv_handle);
    if (_auth_ctrl_handle) { uint8_t v[] = {0x01,0x00}; _wru(_auth_ctrl_handle+1, v, 2); vTaskDelay(pdMS_TO_TICKS(100)); }
    if (_auth_data_handle) { uint8_t v[] = {0x01,0x00}; _wru(_auth_data_handle+1, v, 2); vTaskDelay(pdMS_TO_TICKS(100)); }
    if (_cmd_send_handle)  { uint8_t v[] = {0x01,0x00}; _wru(_cmd_send_handle+1,  v, 2); vTaskDelay(pdMS_TO_TICKS(100)); }
    if (_cmd_recv_handle)  { uint8_t v[] = {0x01,0x00}; _wru(_cmd_recv_handle+1,  v, 2); vTaskDelay(pdMS_TO_TICKS(100)); }
    vTaskDelay(pdMS_TO_TICKS(500));
    if (_auth_ctrl_handle && _auth_data_handle) { _set_state(BLE_AUTHENTICATING); _auth(); }
    else { ESP_LOGE(TAG, "Missing auth handles"); _set_state(BLE_RECONNECT); }
}

static void _reconnect(void) {
    uint32_t base = (_ra == 0) ? 3000 : 5000;
    uint32_t d = base * (1 << (_ra < 4 ? _ra : 3));
    if (d > 40000) d = 40000;
    ESP_LOGI(TAG, "Reconnect in %ums (attempt %d)", (unsigned)d, (int)(_ra + 1));
    vTaskDelay(pdMS_TO_TICKS(d));
    _ra++;
    _set_state(BLE_SCANNING);
}

static void _keepalive(void) {
    // Write Command (no response) to cmd_recv — most reliable keepalive
    // No device response needed, won't block, won't trigger disconnect
    if (_connected) {
        _wru_nr(_cmd_recv_handle, (uint8_t[]){0,0,0,0}, 4);
    }
    _last_keepalive = esp_timer_get_time() / 1000;
}

void ble_manager_disconnect(void) {
    if (!_connected) { _drain_all_queues(); return; }
    _disable_all_notifications();
    xSemaphoreTake(_disconnect_sem, 0);
    ble_gap_terminate(_conn_handle, 0x13);
    if (xSemaphoreTake(_disconnect_sem, pdMS_TO_TICKS(3000)) != pdTRUE) ESP_LOGW(TAG, "Disconnect timeout");
    _connected = false; _conn_handle = 0xFFFF; _drain_all_queues();
}

/* ============================================================
 * GAP + GATT callbacks (NimBLE host task context)
 * ============================================================ */
static int _gap_event(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
    case BLE_GAP_EVENT_DISC:
        if (memcmp(event->disc.addr.val, _target_addr, 6) == 0) {
            ESP_LOGI(TAG, "Found target, RSSI=%d", event->disc.rssi);
            ble_gap_disc_cancel();
            _set_state(BLE_CONNECTING);
            if (ble_gap_connect(BLE_OWN_ADDR_PUBLIC, &event->disc.addr, 30000, NULL, _gap_event, NULL) != 0)
                _set_state(BLE_RECONNECT);
        }
        break;
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status != 0) { ESP_LOGE(TAG, "Connect fail: %d", event->connect.status); _connected = false; _set_state(BLE_RECONNECT); break; }
        _conn_handle = event->connect.conn_handle; _connected = true;
        ESP_LOGI(TAG, "Connected, handle=%d", _conn_handle);
        ble_gattc_exchange_mtu(_conn_handle, NULL, NULL);
        if (_connected_sem) xSemaphoreGive(_connected_sem);
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "Disconnected, reason=%d", event->disconnect.reason);
        _connected = false; _conn_handle = 0xFFFF; _drain_all_queues();
        if (_disconnect_sem) xSemaphoreGive(_disconnect_sem);
        _set_state(BLE_RECONNECT); break;
    case BLE_GAP_EVENT_MTU: ESP_LOGI(TAG, "MTU: %d", event->mtu.value); break;
    case BLE_GAP_EVENT_NOTIFY_RX:
        _dispatch_notif(event->notify_rx.attr_handle,
                        OS_MBUF_PKTLEN(event->notify_rx.om) > 0 ? event->notify_rx.om->om_data : NULL,
                        OS_MBUF_PKTLEN(event->notify_rx.om));
        break;
    case BLE_GAP_EVENT_DISC_COMPLETE:
        ESP_LOGI(TAG, "Scan complete, reason=%d", event->disc_complete.reason);
        _set_state(BLE_SCANNING); break;
    default: break;
    }
    return 0;
}

static int _disc_svc_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                         const struct ble_gatt_svc *service, void *arg) {
    if (error && error->status != 0) { if (_disc_sem) xSemaphoreGive(_disc_sem); return 0; }
    if (service == NULL) { ESP_LOGI(TAG, "Svc discovery complete"); if (_disc_sem) xSemaphoreGive(_disc_sem); return 0; }
    if (service->uuid.u16.value == 0xFE95) { _disc_service_start = service->start_handle; _disc_service_end = service->end_handle; ESP_LOGI(TAG, "MiOT service: 0x%04X-0x%04X", service->start_handle, service->end_handle); }
    return 0;
}

static int _disc_chr_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                         const struct ble_gatt_chr *chr, void *arg) {
    if (error && error->status != 0) { if (_disc_sem) xSemaphoreGive(_disc_sem); return 0; }
    if (chr == NULL) { if (_disc_sem) xSemaphoreGive(_disc_sem); return 0; }
    for (int i = 0; i < _char_ctx_n; i++) {
        if (chr->uuid.u16.value == _char_ctx[i].uuid) { *_char_ctx[i].handle = chr->val_handle; ESP_LOGI(TAG, "%s handle=0x%04X", _char_ctx[i].name, chr->val_handle); break; }
    }
    return 0;
}

static void _disable_all_notifications(void) {
    if (!_connected) return;
    uint8_t val[] = {0x00, 0x00};
    uint16_t handles[] = {_auth_ctrl_handle, _auth_data_handle, _cmd_send_handle, _cmd_recv_handle};
    for (int i = 0; i < 4; i++) { if (handles[i]) { _wru_nr(handles[i] + 1, val, 2); vTaskDelay(pdMS_TO_TICKS(30)); } }
    vTaskDelay(pdMS_TO_TICKS(100));
}

/* ============================================================
 * Public API
 * ============================================================ */
void ble_manager_init(const char *device_mac, const char *device_token, const char *device_ble_key) {
    _q_auth_ctrl = xQueueCreate(NOTIF_QUEUE_LEN, sizeof(NotifItem));
    _q_auth_data = xQueueCreate(NOTIF_QUEUE_LEN, sizeof(NotifItem));
    _q_cmd_send  = xQueueCreate(NOTIF_QUEUE_LEN, sizeof(NotifItem));
    _q_cmd_recv  = xQueueCreate(NOTIF_QUEUE_LEN, sizeof(NotifItem));
    _op_sem = xSemaphoreCreateBinary();
    _connected_sem = xSemaphoreCreateBinary();
    _disc_sem = xSemaphoreCreateBinary();
    _disconnect_sem = xSemaphoreCreateBinary();

    if (sscanf(device_mac, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx",
           &_target_addr[5], &_target_addr[4], &_target_addr[3],
           &_target_addr[2], &_target_addr[1], &_target_addr[0]) != 6) {
        ESP_LOGE(TAG, "Invalid MAC: %s", device_mac);
    }
    strncpy(_mac_str, device_mac, sizeof(_mac_str) - 1);
    for (int i = 0; i < 12; i++) { char hex[3] = {device_token[i*2], device_token[i*2+1], 0}; _token[i] = (uint8_t)strtol(hex, NULL, 16); }

    ESP_LOGI(TAG, "Init NimBLE...");
    nimble_port_init();
    ble_hs_cfg.gatts_register_cb = NULL;
    ble_hs_cfg.sync_cb = _nimble_on_sync;
    nimble_port_freertos_init(_nimble_host_task);
    _set_state(BLE_SCANNING);
}

BLEState ble_manager_state(void) { return _state; }
bool ble_manager_is_ready(void) { return _state == BLE_READY; }
void ble_manager_set_state_callback(StateCallback cb) { _state_cb = cb; }
void ble_manager_set_port_data_callback(PortDataCallback cb) { _port_data_cb = cb; }
const PortInfo* ble_manager_get_ports(void) { return (const PortInfo*)_ports; }
uint32_t ble_manager_get_setting(uint8_t piid) { return (piid < 32) ? _settings[piid] : 0; }
bool ble_manager_has_setting(uint8_t piid) { return piid < 32 && _settings_valid[piid]; }
void ble_manager_store_setting(uint8_t piid, uint32_t val) { if (piid < 32) { _settings[piid] = val; _settings_valid[piid] = true; } }

int ble_manager_pending_count(void) {
    int count = 0;
    for (int i = 0; i < MAX_PENDING; i++) {
        if (_pending[i].in_use) count++;
    }
    return count;
}

bool ble_manager_send_get_async(uint8_t piid, uint32_t poll_seq) { return _send_get_async(piid, poll_seq); }
bool ble_manager_send_set_async(uint8_t piid, uint32_t value, uint32_t poll_seq) { return _send_set_async(piid, value, poll_seq, false); }
bool ble_manager_send_set_nr_async(uint8_t piid, uint32_t value) { return _send_set_async(piid, value, 0, true); }

void ble_manager_set_enabled(bool enabled) {
    _enabled = enabled;
    if (enabled) {
        if (_state == BLE_IDLE) _set_state(BLE_SCANNING);
    } else {
        if (_connected) ble_manager_disconnect();
        if (_state != BLE_IDLE) _set_state(BLE_IDLE);
    }
}

bool ble_manager_is_enabled(void) { return _enabled; }
