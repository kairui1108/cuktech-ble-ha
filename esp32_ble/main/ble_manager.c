#include "ble_manager.h"
#include "config.h"
#include "device_state.h"
#include "miot_protocol.h"
#include "miot_auth.h"
#include "queue_msg.h"
#include "miot_protocol_shared.h"

#include <string.h>
#include <stdio.h>
#include <math.h>
#include "esp_log.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "esp_timer.h"

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
static void _pending_start_next(void);
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
#define NOTIF_QUEUE_LEN_AUTH  2  // auth queues only used briefly during startup

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
/* Settings now live in device_state (single source of truth). */
static uint32_t _ra = 0;
static uint64_t _last_keepalive = 0;
static SemaphoreHandle_t _disc_sem = NULL, _connected_sem = NULL, _disconnect_sem = NULL;
static uint16_t _disc_service_start = 0, _disc_service_end = 0;
static uint8_t _target_addr[6] = {}, _token[12] = {};
/* Peer captured by the scan callback; the connect itself is issued from
   the manager loop (task context) after the discovery procedure has been
   cancelled — see ble_manager_loop(). */
static ble_addr_t _found_peer;
static volatile bool _peer_found = false;
/* A connect procedure has been started and its result is pending.  Used to
   keep DISC_COMPLETE from rescanning while the link is being established. */
static volatile bool _connect_pending = false;
static char _mac_str[18] = {};
static volatile bool _nimble_ready = false;  /* written by NimBLE host task, read by ble_task */
static volatile bool _enabled = true;

typedef struct { uint16_t uuid; uint16_t *handle; const char *name; } CharCtx;
static CharCtx _char_ctx[4];
static int _char_ctx_n = 0;

/* ============================================================
 * Async pending command table
 * ============================================================ */
#define MAX_PENDING 12
#define CIPHER_BUF_SIZE 128

typedef enum { PENDING_GET, PENDING_SET } PendingType;
/* Commands must be sent strictly one at a time: the device performs a
   header/RCV_RDY/ciphertext/RCV_OK handshake per command.  SEND_QUEUED
   entries are encrypted and started only when the wire is idle. */
typedef enum { SEND_IDLE, SEND_QUEUED, SEND_AWAIT_RDY, SEND_AWAIT_OK, SEND_AWAIT_RESP } SendPhase;

typedef struct {
    bool      in_use;
    uint8_t   seq;
    uint8_t   piid;
    PendingType type;
    uint32_t  send_value;
    uint32_t  poll_seq;
    uint64_t  deadline;         /* 0 while still queued */
    uint32_t  timeout_ms;       /* starts when the command is sent */
    bool      acked;
    bool      no_result;
    SendPhase phase;
    uint8_t   plaintext[16];
    uint8_t   pt_len;
    uint8_t   ciphertext[CIPHER_BUF_SIZE];  // [1,0] + encrypted data
    size_t    ct_len;
} PendingEntry;

static PendingEntry _pending[MAX_PENDING] = {};

static void _pending_check_timeouts(void) {
    uint64_t now = esp_timer_get_time() / 1000;
    for (int i = 0; i < MAX_PENDING; i++) {
        if (!_pending[i].in_use) continue;
        if (_pending[i].phase == SEND_QUEUED) continue;   /* not sent yet */
        if (now < _pending[i].deadline) continue;
        ESP_LOGW(TAG, "Pending %s seq=%d piid=%d timed out",
                 _pending[i].type == PENDING_GET ? "GET" : "SET",
                 _pending[i].seq, _pending[i].piid);
        BleResult r;
        if (_pending[i].type == PENDING_GET) {
            r = (BleResult){RES_GET, false, _pending[i].piid, 0, _pending[i].poll_seq};
        } else {
            r = (BleResult){RES_SET, false, _pending[i].piid, _pending[i].send_value, 0};
        }
        xQueueSend(result_queue, &r, pdMS_TO_TICKS(50));
        _pending[i].in_use = false;
    }
}

static void _pending_abort_all(void) {
    for (int i = 0; i < MAX_PENDING; i++) {
        if (!_pending[i].in_use) continue;
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
            xQueueSend(result_queue, &r, pdMS_TO_TICKS(50));
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
                xQueueSend(result_queue, &r, pdMS_TO_TICKS(50));
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
    _nimble_ready = true;
    ESP_LOGI(TAG, "NimBLE ready");
}

static volatile bool _params_requested = false;
static uint32_t _params_next_try_ms = 0;

/* The charger asks for a 30-50 ms connection interval with latency 0,
   which keeps the radio in a connection event almost every other WiFi
   frame.  Measured on this C3: TCP round trips become bimodal (3 ms or
   ~100 ms) and page loads drop from 35-48 KB/s to 10-22 KB/s.  Status
   updates arrive about once a second and commands tolerate a few hundred
   ms, so ask for 200 ms with a latency of 2 (commands may therefore wait
   up to ~600 ms), which cuts the number of connection events — and the
   airtime WiFi loses to them — by four.  Issued from the manager loop,
   not from the connect callback, because an update cannot start while
   the MTU exchange or discovery procedure is running (it returns
   BLE_HS_EBUSY, retried a little later). */
static void _maybe_update_params(void) {
    /* Only after authentication: the auth handshake exchanges several
       notifications, and a 200 ms interval with latency 2 made the first
       attempt time out ("Phase A: no key exchange data"), costing a full
       disconnect/reconnect before it succeeded. */
    if (_state != BLE_READY) return;
    if (!_connected || _params_requested || _conn_handle == 0xFFFF) return;
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    if ((int32_t)(now - _params_next_try_ms) < 0) return;
    struct ble_gap_upd_params up = {
        .itvl_min = 160,            /* 200 ms */
        .itvl_max = 160,
        .latency = 2,
        .supervision_timeout = 600, /* 6 s */
    };
    int rc = ble_gap_update_params(_conn_handle, &up);
    if (rc == BLE_HS_EBUSY) {
        _params_next_try_ms = now + 200;
    } else {
        _params_requested = true;
        if (rc != 0) ESP_LOGW(TAG, "ble_gap_update_params: %d", rc);
    }
}

void ble_manager_loop(void) {
    if (!_nimble_ready) return;
    /* Anything queued belongs to the old connection; fail it cleanly
       instead of replaying stale commands after a reconnect. */
    if (_state != BLE_READY) _pending_abort_all();
    _maybe_update_params();
    switch (_state) {
    case BLE_SCANNING:     _scan(); break;
    case BLE_CONNECTING:
        if (_peer_found) {
            _peer_found = false;
            /* Issuing ble_gap_connect() from inside the discovery event
               races with the cancel of the discovery procedure; the
               controller then aborts the first attempt with HCI 0x3E
               ("connection failed to be established").  Doing it here,
               after the callback returned, connects on the first try. */
            if (ble_gap_connect(BLE_OWN_ADDR_PUBLIC, &_found_peer, 30000,
                                NULL, _gap_event, NULL) != 0) {
                ESP_LOGE(TAG, "ble_gap_connect failed");
                _connect_pending = false;
                _set_state(BLE_RECONNECT);
            } else {
                _connect_pending = true;
            }
        } else if (xSemaphoreTake(_connected_sem, pdMS_TO_TICKS(100)) == pdTRUE) {
            _discover();
        }
        break;
    case BLE_READY:
        _handle_cmd_recv();
        _handle_cmd_send();
        _pending_start_next();
        _pending_check_timeouts();
        _pending_start_next();
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
// Hardware protocol code from PIID 17/18 (byte positions aligned with Python ble_manager.py):
// PIID 17: byte[3] = C1, byte[1] = C2; PIID 18: byte[3] = C3, byte[1] = A
static uint8_t _get_hw_proto(uint8_t piid) {
    uint32_t v17 = 0, v18 = 0;
    bool ok17 = device_state_get_setting(17, &v17);
    bool ok18 = device_state_get_setting(18, &v18);
    uint8_t result = 0;
    switch (piid) {
        case 1: result = (ok17 && ((v17 >> 24) & 0xFF) > 0) ? (v17 >> 24) & 0xFF : 0; break;
        case 2: result = (ok17 && ((v17 >> 8) & 0xFF) > 0) ? (v17 >> 8) & 0xFF : 0; break;
        case 3: result = (ok18 && ((v18 >> 24) & 0xFF) > 0) ? (v18 >> 24) & 0xFF : 0; break;
        case 4: result = (ok18 && ((v18 >> 8) & 0xFF) > 0) ? (v18 >> 8) & 0xFF : 0; break;
    }
    return result;
}

static uint8_t _estimate_proto(uint8_t piid, float voltage, uint8_t code, uint8_t hw_protocol) {
    if (hw_protocol > 0) return hw_protocol;

    uint32_t ext = 0, s17 = 0, s18 = 0;
    bool ext_ok = device_state_get_setting(21, &ext);
    bool s17_ok = device_state_get_setting(17, &s17);
    bool s18_ok = device_state_get_setting(18, &s18);

    int pd_bit = (piid == 1) ? 0 : 8;
    int pps_bit = (piid == 1) ? 1 : 9;
    bool pd_enabled = ext_ok ? ((ext >> pd_bit) & 1) : true;
    bool pps_enabled = ext_ok ? ((ext >> pps_bit) & 1) : true;
    uint8_t pdo_kind = 0;
    if ((piid == 1 || piid == 2) && s17_ok) {
        uint16_t port_word = (piid == 1) ? (s17 & 0xFFFF) : ((s17 >> 16) & 0xFFFF);
        pdo_kind = (port_word >> 8) & 0xFF;
    } else if ((piid == 3 || piid == 4) && s18_ok) {
        /* C3 (piid=3) = lower 16 bits; A (piid=4) = upper 16 bits (same layout as PIID 17) */
        uint16_t port_word = (piid == 3) ? (s18 & 0xFFFF) : ((s18 >> 16) & 0xFFFF);
        pdo_kind = (port_word >> 8) & 0xFF;
    }
    return estimate_protocol_shared(piid, voltage, code, pd_enabled, pps_enabled, pdo_kind, hw_protocol);
}

static void _parse_port(uint8_t piid, const uint8_t *pt, size_t pt_len) {
    if (piid < 1 || piid > 4 || pt_len < 12) return;
    uint32_t val = pt[pt_len-4] | (pt[pt_len-3] << 8) |
                   (pt[pt_len-2] << 16) | (pt[pt_len-1] << 24);
    uint8_t idx = piid - 1;
    _ports[idx].voltage = ((val >> 24) & 0xFF) / 10.0f;
    _ports[idx].current = ((val >> 16) & 0xFF) / 10.0f;
    _ports[idx].protocol = _estimate_proto(piid, _ports[idx].voltage, (val >> 8) & 0xFF, _get_hw_proto(piid));
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
static bool _alloc_pending(uint8_t seq, uint8_t piid, PendingType type,
                            uint32_t send_value, uint32_t poll_seq, uint32_t timeout_ms,
                            bool no_result, const uint8_t *pt, size_t pt_len) {
    if (pt_len > sizeof(_pending[0].plaintext)) return false;

    /* Keep slots free for user commands: background GET polling must
       never crowd out a SET/port toggle. */
    if (type == PENDING_GET) {
        int used = 0;
        for (int i = 0; i < MAX_PENDING; i++) if (_pending[i].in_use) used++;
        if (used >= MAX_PENDING - 4) {
            ESP_LOGD(TAG, "Pending table reserved for SETs, skipping GET piid=%d", piid);
            return false;
        }
    }

    for (int i = 0; i < MAX_PENDING; i++) {
        if (_pending[i].in_use) continue;
        _pending[i] = (PendingEntry){
            .in_use = true, .seq = seq, .piid = piid, .type = type,
            .send_value = send_value, .poll_seq = poll_seq,
            .deadline = 0,                 /* set when actually sent */
            .timeout_ms = timeout_ms,
            .no_result = no_result, .phase = SEND_QUEUED,
            .pt_len = (uint8_t)pt_len,
        };
        memcpy(_pending[i].plaintext, pt, pt_len);
        return true;
    }
    ESP_LOGW(TAG, "Pending table full");
    return false;
}

/* Start the oldest queued command, but only when no command is in flight.
   The MiOT command channel is a strict request/response handshake, so
   overlapping headers would desynchronise the device. */
static void _pending_start_next(void) {
    for (int i = 0; i < MAX_PENDING; i++) {
        if (!_pending[i].in_use) continue;
        if (_pending[i].phase != SEND_QUEUED) return;  /* wire busy */
    }

    /* User SETs first, then background GET polling.  Two passes over a
       12-entry table are cheaper than any sort. */
    for (int pass = 0; pass < 2; pass++) {
        PendingType want = pass == 0 ? PENDING_SET : PENDING_GET;
        for (int i = 0; i < MAX_PENDING; i++) {
            if (!_pending[i].in_use || _pending[i].phase != SEND_QUEUED) continue;
            if (_pending[i].type != want) continue;

            uint8_t enc[CIPHER_BUF_SIZE];
            size_t el = 0;
            if (!encrypt_command(&_keys, &_send_it, _pending[i].plaintext,
                                 _pending[i].pt_len, enc, &el) ||
                el + 2 > sizeof(_pending[i].ciphertext)) {
                ESP_LOGW(TAG, "Encrypt failed for piid=%d, dropping", _pending[i].piid);
                BleResult r = {_pending[i].type == PENDING_GET ? RES_GET : RES_SET,
                               false, _pending[i].piid, _pending[i].send_value, 0};
                xQueueSend(result_queue, &r, 0);
                _pending[i].in_use = false;
                continue;
            }

            /* CMD HEADER tells the device to expect one encrypted frame. */
            if (!_wru_nr(_cmd_send_handle, (uint8_t[]){0, 0, 0, 0, 1, 0}, 6)) {
                ESP_LOGW(TAG, "CMD header write failed, will retry");
                return;   /* leave queued */
            }

            _pending[i].ciphertext[0] = 1;
            _pending[i].ciphertext[1] = 0;
            memcpy(_pending[i].ciphertext + 2, enc, el);
            _pending[i].ct_len = 2 + el;
            _pending[i].deadline = esp_timer_get_time() / 1000 + _pending[i].timeout_ms;
            _pending[i].phase = SEND_AWAIT_RDY;
            return;
        }
    }
}

static bool _send_get_async(uint8_t piid, uint32_t poll_seq) {
    uint8_t buf[16] = {0};
    uint8_t req_seq = _seq;
    buf[0] = 12; buf[1] = 0x20; buf[2] = req_seq; buf[3] = 0;
    buf[4] = 0x02; buf[5] = 0x01; buf[6] = SIID_CHARGER;
    buf[7] = piid; buf[8] = 0; buf[9] = 0x01; buf[10] = 0x10; buf[11] = 0;
    _seq = (_seq + 1) & 0xFF;

    /* 2 s, not 2.5 s: the command channel is strictly serialized, so a hung
       GET holds it and a SET queued behind it waits the whole timeout. On
       hardware a 1.5 s budget still produced the occasional timeout (two in
       75 s while the charger was pushing), so 2 s keeps the margin without
       going back to 2.5 s. Dropping a GET frame costs nothing —
       process_result() ignores !success results and the poll retries the
       port on the next interval. SETs keep their 4 s budget. */
    return _alloc_pending(req_seq, piid, PENDING_GET, 0, poll_seq, 2000, false, buf, 12);
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

    return _alloc_pending(req_seq, piid, PENDING_SET, value, poll_seq, 4000, no_result,
                          buf, (size_t)total_len);
}

/* All command traffic goes through the async pending table above.
   The old blocking miot_get/miot_set wrappers were removed: they could
   stall the BLE state machine for up to 8 seconds per command. */


/* ============================================================
 * BLE lifecycle: scan, discover, disconnect, reconnect, keepalive
 * ============================================================ */
static void _scan(void) {
    xSemaphoreTake(_connected_sem, 0);
    _peer_found = false;
    ble_gap_disc_cancel();
    vTaskDelay(pdMS_TO_TICKS(100));
    /* 60 ms scan interval with a 30 ms window (50% duty) instead of
       60 ms / 60 ms.  A 100%-duty scan leaves the single C3 radio no idle
       airtime, which stalled every WiFi round trip whenever the charger
       was out of range; a 30 ms window still catches its ~30 ms
       advertisements within a few intervals. */
    struct ble_gap_disc_params dp = {}; dp.itvl = 0x60; dp.window = 0x30;
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
    /* Non-reentrant guard: prevent overlapping disconnect sequences */
    static bool _disconnecting = false;
    if (_disconnecting) { _drain_all_queues(); return; }
    if (!_connected) { _drain_all_queues(); return; }
    _disconnecting = true;
    _disable_all_notifications();
    xSemaphoreTake(_disconnect_sem, 0);
    ble_gap_terminate(_conn_handle, 0x13);
    if (xSemaphoreTake(_disconnect_sem, pdMS_TO_TICKS(3000)) != pdTRUE) ESP_LOGW(TAG, "Disconnect timeout");
    _connected = false; _conn_handle = 0xFFFF; _drain_all_queues();
    _disconnecting = false;
}

/* ============================================================
 * GAP + GATT callbacks (NimBLE host task context)
 * ============================================================ */
static int _gap_event(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
    case BLE_GAP_EVENT_DISC:
        if (!_peer_found && memcmp(event->disc.addr.val, _target_addr, 6) == 0) {
            ESP_LOGI(TAG, "Found target, RSSI=%d", event->disc.rssi);
            _found_peer = event->disc.addr;
            _peer_found = true;
            ble_gap_disc_cancel();
        }
        break;
    case BLE_GAP_EVENT_CONNECT:
        _connect_pending = false;
        if (event->connect.status != 0) { ESP_LOGE(TAG, "Connect fail: %d", event->connect.status); _connected = false; _set_state(BLE_RECONNECT); break; }
        _conn_handle = event->connect.conn_handle; _connected = true;
        _params_requested = false;
        ESP_LOGI(TAG, "Connected, handle=%d", _conn_handle);
        {
            struct ble_gap_conn_desc desc;
            if (ble_gap_conn_find(_conn_handle, &desc) == 0) {
                ESP_LOGI(TAG, "params: itvl=%.2fms latency=%u timeout=%ums",
                         desc.conn_itvl * 1.25, desc.conn_latency,
                         desc.supervision_timeout * 10);
            }
        }
        ble_gattc_exchange_mtu(_conn_handle, NULL, NULL);
        if (_connected_sem) xSemaphoreGive(_connected_sem);
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "Disconnected, reason=%d", event->disconnect.reason);
        _connected = false; _connect_pending = false; _conn_handle = 0xFFFF;
        _params_requested = false; _drain_all_queues();
        if (_disconnect_sem) xSemaphoreGive(_disconnect_sem);
        _set_state(BLE_RECONNECT); break;
    case BLE_GAP_EVENT_MTU: ESP_LOGI(TAG, "MTU: %d", event->mtu.value); break;
    case BLE_GAP_EVENT_CONN_UPDATE: {
        struct ble_gap_conn_desc cd;
        int crc = ble_gap_conn_find(event->conn_update.conn_handle, &cd);
        ESP_LOGI(TAG, "Conn update: status=%d itvl=%.2fms latency=%u timeout=%ums",
                 event->conn_update.status,
                 crc == 0 ? cd.conn_itvl * 1.25 : -1.0,
                 crc == 0 ? cd.conn_latency : 0,
                 crc == 0 ? cd.supervision_timeout * 10 : 0);
        break;
    }
    case BLE_GAP_EVENT_NOTIFY_RX:
        _dispatch_notif(event->notify_rx.attr_handle,
                        OS_MBUF_PKTLEN(event->notify_rx.om) > 0 ? event->notify_rx.om->om_data : NULL,
                        OS_MBUF_PKTLEN(event->notify_rx.om));
        break;
    case BLE_GAP_EVENT_DISC_COMPLETE:
        ESP_LOGI(TAG, "Scan complete, reason=%d", event->disc_complete.reason);
        /* A cancel issued from the DISC callback also completes the scan;
           keep waiting for the connect instead of rescanning. */
        if (!_peer_found && !_connect_pending) _set_state(BLE_SCANNING);
        break;
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
    /* Validate BLE config before initializing NimBLE. If MAC/token/key
       are missing or ill-formed, skip BLE entirely — no scanning, no
       repeated connection attempts. */
    uint8_t mac[6];
    bool mac_ok = (device_mac && strlen(device_mac) == 17 &&
                   sscanf(device_mac, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx",
                          &mac[5], &mac[4], &mac[3], &mac[2], &mac[1], &mac[0]) == 6);
    /* Token must be exactly 24 hex chars → 12 bytes.  We also accept
       bare 12-byte raw token strings (rare config edge-case). */
    size_t tlen = device_token ? strlen(device_token) : 0;
    bool token_ok = (tlen >= 24);

    if (!mac_ok || !token_ok) {
        ESP_LOGW(TAG, "BLE config incomplete (mac=%s token=%s len=%u) — BLE disabled",
                 device_mac ? "set" : "null",
                 device_token ? (tlen > 8 ? "set" : "short") : "null",
                 (unsigned)tlen);
        _enabled = false;
        _state = BLE_IDLE;
        snprintf(_mac_str, sizeof(_mac_str), "%s", device_mac ? device_mac : "");
        return;
    }

    memcpy(_target_addr, mac, 6);
    _q_auth_ctrl = xQueueCreate(NOTIF_QUEUE_LEN_AUTH, sizeof(NotifItem));
    _q_auth_data = xQueueCreate(NOTIF_QUEUE_LEN_AUTH, sizeof(NotifItem));
    _q_cmd_send  = xQueueCreate(NOTIF_QUEUE_LEN, sizeof(NotifItem));
    _q_cmd_recv  = xQueueCreate(NOTIF_QUEUE_LEN, sizeof(NotifItem));
    _op_sem = xSemaphoreCreateBinary();
    _connected_sem = xSemaphoreCreateBinary();
    _disc_sem = xSemaphoreCreateBinary();
    _disconnect_sem = xSemaphoreCreateBinary();

    snprintf(_mac_str, sizeof(_mac_str), "%s", device_mac);
    /* Parse 12-byte token from hex string. Loop guard ensures we never
       read past the NUL terminator (already validated >= 24 chars). */
    for (int i = 0; i < 12; i++) {
        char hex[3] = {device_token[i*2], device_token[i*2+1], 0};
        _token[i] = (uint8_t)strtol(hex, NULL, 16);
    }

    ESP_LOGI(TAG, "Init NimBLE...");
    vTaskDelay(pdMS_TO_TICKS(500));
    nimble_port_init();
    ble_hs_cfg.gatts_register_cb = NULL;
    ble_hs_cfg.sync_cb = _nimble_on_sync;
    nimble_port_freertos_init(_nimble_host_task);
    _set_state(BLE_SCANNING);
}

BLEState ble_manager_state(void) { return _state; }
bool ble_manager_is_ready(void) { return _state == BLE_READY; }
bool ble_manager_is_idle(void) { return _state == BLE_IDLE; }
void ble_manager_set_state_callback(StateCallback cb) { _state_cb = cb; }
void ble_manager_set_port_data_callback(PortDataCallback cb) { _port_data_cb = cb; }
const PortInfo* ble_manager_get_ports(void) { return (const PortInfo*)_ports; }
uint32_t ble_manager_get_setting(uint8_t piid) {
    uint32_t v = 0;
    device_state_get_setting(piid, &v);
    return v;
}
bool ble_manager_has_setting(uint8_t piid) {
    return device_state_has_setting(piid);
}
void ble_manager_store_setting(uint8_t piid, uint32_t val) {
    device_state_set_setting(piid, val);
}

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
