#include "http_server.h"
#include "config.h"
#include "esp_http_server.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "cJSON.h"
#include <string.h>

static const char *TAG = "HTTP_SERVER";
static httpd_handle_t _server = NULL;
static DeviceConfig *_cfg = NULL;
static http_config_cb _on_save = NULL;
static port_data_cb _port_data_cb = NULL;
static settings_cb _settings_cb = NULL;
static port_control_cb _port_ctl_cb = NULL;
static setting_set_cb _setting_set_cb = NULL;
static protocol_toggle_cb _proto_toggle_cb = NULL;
static ble_control_cb _ble_ctl_cb = NULL;

void http_server_set_callbacks(port_data_cb ports, settings_cb settings,
                               port_control_cb port_ctl, setting_set_cb setting_set,
                               protocol_toggle_cb proto_toggle,
                               ble_control_cb ble_ctl) {
    _port_data_cb = ports;
    _settings_cb = settings;
    _port_ctl_cb = port_ctl;
    _setting_set_cb = setting_set;
    _proto_toggle_cb = proto_toggle;
    _ble_ctl_cb = ble_ctl;
}

/* ==================== Config API ==================== */

static void _json_cfg(cJSON *root) {
    cJSON_AddStringToObject(root, "wifi_ssid", _cfg->wifi_ssid);
    cJSON_AddStringToObject(root, "wifi_pass", _cfg->wifi_pass);
    cJSON_AddStringToObject(root, "device_mac", _cfg->device_mac);
    cJSON_AddStringToObject(root, "device_token", _cfg->device_token);
    cJSON_AddStringToObject(root, "device_ble_key", _cfg->device_ble_key);
    cJSON_AddStringToObject(root, "mqtt_broker", _cfg->mqtt_broker);
    cJSON_AddNumberToObject(root, "mqtt_port", _cfg->mqtt_port);
    cJSON_AddStringToObject(root, "mqtt_user", _cfg->mqtt_user);
    cJSON_AddStringToObject(root, "mqtt_pass", _cfg->mqtt_pass);
    cJSON_AddStringToObject(root, "mqtt_topic_prefix", _cfg->mqtt_topic_prefix);
    cJSON_AddBoolToObject(root, "mqtt_enable", _cfg->mqtt_enable);
}

static int _get_config_handler(httpd_req_t *req) {
    cJSON *root = cJSON_CreateObject();
    _json_cfg(root);
    char *json = cJSON_PrintUnformatted(root);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, json);
    cJSON_free(json); cJSON_Delete(root);
    return 0;
}

static int _post_config_handler(httpd_req_t *req) {
    char buf[1024];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { httpd_resp_send_500(req); return -1; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON"); return -1; }

    #define SET_STR(f, k) do { cJSON *j = cJSON_GetObjectItem(root, k); \
        if (j && cJSON_IsString(j)) strncpy(_cfg->f, cJSON_GetStringValue(j), sizeof(_cfg->f)-1); } while(0)
    SET_STR(wifi_ssid, "wifi_ssid"); SET_STR(wifi_pass, "wifi_pass");
    SET_STR(device_mac, "device_mac"); SET_STR(device_token, "device_token");
    SET_STR(device_ble_key, "device_ble_key"); SET_STR(mqtt_broker, "mqtt_broker");
    SET_STR(mqtt_user, "mqtt_user"); SET_STR(mqtt_pass, "mqtt_pass");
    SET_STR(mqtt_topic_prefix, "mqtt_topic_prefix");

    cJSON *je = cJSON_GetObjectItem(root, "mqtt_enable");
    if (je && cJSON_IsBool(je)) _cfg->mqtt_enable = cJSON_IsTrue(je);
    cJSON *jp = cJSON_GetObjectItem(root, "mqtt_port");
    if (jp && cJSON_IsNumber(jp)) _cfg->mqtt_port = (uint16_t)cJSON_GetNumberValue(jp);
    _cfg->valid = (_cfg->wifi_ssid[0] != '\0' && _cfg->device_mac[0] != '\0');

    ESP_LOGI(TAG, "Config saved: wifi=%s mqtt=%s:%d", _cfg->wifi_ssid, _cfg->mqtt_broker, _cfg->mqtt_port);
    config_store_save(_cfg);
    cJSON_Delete(root);

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddBoolToObject(resp, "ok", true);
    cJSON_AddStringToObject(resp, "message", "Config saved. Rebooting...");
    char *rjson = cJSON_PrintUnformatted(resp);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, rjson);
    cJSON_free(rjson); cJSON_Delete(resp);

    if (_on_save) _on_save();
    return 0;
}

/* ==================== Dashboard API ==================== */

static int _get_ports_handler(httpd_req_t *req) {
    if (!_port_data_cb) { httpd_resp_send_500(req); return -1; }
    cJSON *root = _port_data_cb();
    char *json = cJSON_PrintUnformatted(root);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_sendstr(req, json);
    cJSON_free(json); cJSON_Delete(root);
    return 0;
}

static int _get_settings_handler(httpd_req_t *req) {
    if (!_settings_cb) { httpd_resp_send_500(req); return -1; }
    cJSON *root = _settings_cb();
    char *json = cJSON_PrintUnformatted(root);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, json);
    cJSON_free(json); cJSON_Delete(root);
    return 0;
}

static int _post_port_handler(httpd_req_t *req) {
    char buf[256];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { httpd_resp_send_500(req); return -1; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON"); return -1; }

    cJSON *jp = cJSON_GetObjectItem(root, "port");
    cJSON *ja = cJSON_GetObjectItem(root, "action");
    bool ok = false;
    if (jp && ja && cJSON_IsString(jp) && cJSON_IsString(ja) && _port_ctl_cb) {
        ok = _port_ctl_cb(cJSON_GetStringValue(jp), cJSON_GetStringValue(ja));
    }
    cJSON_Delete(root);

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddBoolToObject(resp, "ok", ok);
    char *rjson = cJSON_PrintUnformatted(resp);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, rjson);
    cJSON_free(rjson); cJSON_Delete(resp);
    return 0;
}

static int _post_setting_handler(httpd_req_t *req) {
    char buf[256];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { httpd_resp_send_500(req); return -1; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON"); return -1; }

    cJSON *jp = cJSON_GetObjectItem(root, "piid");
    cJSON *jv = cJSON_GetObjectItem(root, "value");
    bool ok = false;
    if (jp && jv && cJSON_IsNumber(jp) && cJSON_IsNumber(jv) && _setting_set_cb) {
        ok = _setting_set_cb((int)cJSON_GetNumberValue(jp), (int)cJSON_GetNumberValue(jv));
    }
    cJSON_Delete(root);

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddBoolToObject(resp, "ok", ok);
    char *rjson = cJSON_PrintUnformatted(resp);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, rjson);
    cJSON_free(rjson); cJSON_Delete(resp);
    return 0;
}

/* ==================== BLE Control API ==================== */

static int _post_ble_handler(httpd_req_t *req) {
    char buf[128];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { httpd_resp_send_500(req); return -1; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON"); return -1; }
    cJSON *je = cJSON_GetObjectItem(root, "enabled");
    bool ok = false;
    if (je && cJSON_IsBool(je) && _ble_ctl_cb) {
        ok = _ble_ctl_cb(cJSON_IsTrue(je));
    }
    cJSON_Delete(root);
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddBoolToObject(resp, "ok", ok);
    char *rjson = cJSON_PrintUnformatted(resp);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, rjson);
    cJSON_free(rjson); cJSON_Delete(resp);
    return 0;
}

/* ==================== Protocol Toggle API ==================== */

static int _post_protocol_handler(httpd_req_t *req) {
    char buf[256];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) { httpd_resp_send_500(req); return -1; }
    buf[len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    if (!root) { httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON"); return -1; }
    cJSON *jp = cJSON_GetObjectItem(root, "port");
    cJSON *jproto = cJSON_GetObjectItem(root, "protocol");
    cJSON *ja = cJSON_GetObjectItem(root, "action");
    bool ok = false;
    if (jp && jproto && ja && cJSON_IsString(jp) && cJSON_IsString(jproto) && cJSON_IsString(ja) && _proto_toggle_cb) {
        ok = _proto_toggle_cb(cJSON_GetStringValue(jp), cJSON_GetStringValue(jproto),
                              strcmp(cJSON_GetStringValue(ja), "on") == 0);
    }
    cJSON_Delete(root);
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddBoolToObject(resp, "ok", ok);
    char *rjson = cJSON_PrintUnformatted(resp);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, rjson);
    cJSON_free(rjson); cJSON_Delete(resp);
    return 0;
}

/* ==================== OTA Upload ==================== */

static int _post_ota_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "OTA upload started");
    esp_ota_handle_t ota_handle;
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    if (!part) { ESP_LOGE(TAG, "No OTA partition"); httpd_resp_send_500(req); return -1; }
    esp_err_t err = esp_ota_begin(part, OTA_SIZE_UNKNOWN, &ota_handle);
    if (err != ESP_OK) { ESP_LOGE(TAG, "OTA begin failed: %s", esp_err_to_name(err)); httpd_resp_send_500(req); return -1; }

    int total = 0, remaining = req->content_len;
    char buf[1024];
    while (remaining > 0) {
        int recv = httpd_req_recv(req, buf, remaining > sizeof(buf) ? sizeof(buf) : remaining);
        if (recv <= 0) { esp_ota_abort(ota_handle); httpd_resp_send_500(req); return -1; }
        if (esp_ota_write(ota_handle, buf, recv) != ESP_OK) { esp_ota_abort(ota_handle); httpd_resp_send_500(req); return -1; }
        total += recv; remaining -= recv;
    }
    if (esp_ota_end(ota_handle) != ESP_OK) { httpd_resp_send_500(req); return -1; }
    esp_ota_set_boot_partition(part);
    ESP_LOGI(TAG, "OTA complete: %d bytes, rebooting...", total);

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddBoolToObject(resp, "ok", true);
    char msg[64]; snprintf(msg, sizeof(msg), "OTA done (%d bytes). Rebooting...", total);
    cJSON_AddStringToObject(resp, "message", msg);
    char *rjson = cJSON_PrintUnformatted(resp);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, rjson);
    cJSON_free(rjson); cJSON_Delete(resp);

    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
    return 0;
}

/* ==================== Dashboard HTML ==================== */

static const char _DASH[] =
"<!DOCTYPE html><html><head><meta charset='utf-8'>"
"<meta name='viewport' content='width=device-width,initial-scale=1,user-scalable=no'>"
"<title>CUKTECH 10 Ultra</title><style>"
":root{--bg:#121215;--card:#000;--card-b:rgba(137,246,243,0.5);--text:rgba(255,255,255,0.9);--dim:rgba(255,255,255,0.4);--sub:rgba(255,255,255,0.6);--c1:#46B4FF;--c2:#FF7A00;--c3:#89D8F3;--a:#FFD24B}"
"body.light{--bg:#f0f0f5;--card:#fff;--card-b:rgba(0,0,0,0.1);--text:#1a1a1a;--dim:#888;--sub:#666}"
"*{margin:0;padding:0;box-sizing:border-box}"
"body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Noto Sans SC','MiSans',sans-serif;background:#0a0a0f;color:var(--text);display:flex;justify-content:center;min-height:100vh;overflow-x:hidden}"
".phone{width:100%;max-width:430px;min-height:100vh;background:var(--bg);position:relative}"
".nav{position:fixed;top:0;left:0;right:0;height:48px;max-width:430px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;padding:0 16px;background:var(--bg);z-index:100;border-radius:0 0 18px 18px}"
".nt{font-size:16px;font-weight:500;color:var(--text)}"
".th{width:28px;height:28px;font-size:18px;line-height:28px;text-align:center;cursor:pointer}"
".top{padding:48px 0 20px;background:linear-gradient(180deg,var(--bg),var(--bg));text-align:center;position:sticky;top:0;z-index:1}"
".tpl{font-size:13px;color:var(--dim)}"
".tpv{font-size:56px;font-weight:700;line-height:1.1}"
".tpu{font-size:16px;color:var(--sub);margin-left:2px}"
".pr{display:flex;justify-content:center;gap:6px;margin:16px 12px 0;flex-wrap:wrap}"
".pb{background:var(--card);border:1px solid var(--card-b);border-radius:14px;padding:14px 8px;min-width:76px;flex:1;text-align:center;transition:.3s}"
".pn{font-size:12px;color:var(--dim);font-weight:500;margin-bottom:6px}"
".pw{font-size:24px;font-weight:700;color:var(--text)}"
".pw .w{font-size:12px;color:var(--dim);font-weight:400}"
".pvi{font-size:11px;color:var(--sub);margin-top:4px}"
".pp{font-size:10px;margin-top:6px;padding:2px 8px;border-radius:8px;display:inline-block}"
".sec{font-size:13px;font-weight:600;padding:16px 20px 8px;color:var(--dim)}"
".card{background:var(--card);border:1px solid var(--card-b);border-radius:14px;margin:6px 16px;padding:14px 16px}"
".row{display:flex;align-items:center;justify-content:space-between;padding:10px 0}"
".row+.row{border-top:1px solid var(--card-b)}"
".rl{font-size:14px;color:var(--text)}"
".rv{font-size:14px;color:var(--dim)}"
".tg{position:relative;width:44px;height:24px;cursor:pointer}"
".tg input{display:none}"
".tg .sl{position:absolute;inset:0;background:#333;border-radius:12px;transition:.3s}"
".tg .sl::after{content:'';position:absolute;width:20px;height:20px;background:#888;border-radius:50%;top:2px;left:2px;transition:.3s}"
".tg input:checked+.sl{background:#4CAF50}"
".tg input:checked+.sl::after{left:22px;background:#fff}"
".sg{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px}"
".scb{background:var(--card);border:2px solid var(--card-b);border-radius:12px;padding:10px 4px;text-align:center;cursor:pointer;transition:.2s}"
".scb.active{border-color:#4CAF50}"
".scb .si{font-size:22px}"
".scb .sn{font-size:11px;color:var(--dim);margin-top:4px}"
".scb.active .sn{color:#4CAF50}"
".pg{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:6px}"
".pcb{background:var(--card);border:2px solid var(--card-b);border-radius:10px;padding:8px 4px;text-align:center;cursor:pointer;transition:.2s;font-size:12px;color:var(--dim)}"
".pcb.on{border-color:#4CAF50;color:#81C784}"
".ph{font-size:12px;color:var(--dim);font-weight:600;margin:8px 0 4px}"
".ota{margin:20px 16px 40px}"
".ota input[type=file]{display:none}"
".ob{width:100%;padding:14px;background:var(--card);border:1px solid var(--card-b);border-radius:12px;color:var(--dim);font-size:14px;cursor:pointer;text-align:center}"
".ob:active{opacity:.7}"
".os{font-size:12px;color:var(--dim);margin-top:8px;text-align:center}"
"a{color:var(--c1);text-decoration:none}"
"</style></head><body>"
"<div class='phone'>"
"<div class='nav'><div class='nt'>CUKTECH 10 Ultra</div><div class='th' id='thBtn' onclick='toggleTheme()'></div></div>"

"<div class='top'>"
"<div class='tpl'>Total Power</div>"
"<div class='tpv' id='tp'>0<span class='tpu'>W</span></div>"
"<div class='pr' id='ports'></div>"
"</div>"

"<div class='sec'>BLE 连接</div>"
"<div class='card'>"
"<div class='row'><span class='rl'>连接状态</span><span class='rv' id='bleSt'>--</span></div>"
"<div class='row'><span class='rl'>BLE 控制</span><label class='tg'><input type='checkbox' id='bleEn' onchange='toggleBle(this.checked)'><span class='sl'></span></label></div>"
"</div>"

"<div class='sec'>端口控制</div>"
"<div class='card' id='portctl'></div>"

"<div class='sec'>协议控制</div>"
"<div class='card' id='protos'></div>"

"<div class='sec'>设置</div>"
"<div class='card'>"
"<div class='row'><span class='rl'>场景模式</span><span class='rv' id='v5'>--</span></div>"
"<div class='row'><span class='rl'>息屏时间</span><span class='rv' id='v6'>--</span></div>"
"<div class='row'><span class='rl'>语言</span><span class='rv' id='v13'>--</span></div>"
"<div class='row'><span class='rl'>USB-A小电流</span><label class='tg'><input type='checkbox' id='s15' onchange='setS(15,this.checked?1:0)'><span class='sl'></span></label></div>"
"<div class='row'><span class='rl'>空闲息屏</span><label class='tg'><input type='checkbox' id='s19' onchange='setS(19,this.checked?1:0)'><span class='sl'></span></label></div>"
"<div class='row'><span class='rl'>屏幕方向锁</span><label class='tg'><input type='checkbox' id='s20' onchange='setS(20,this.checked?1:0)'><span class='sl'></span></label></div>"
"</div>"

"<div class='sec'>场景模式</div>"
"<div class='card'><div class='sg' id='scenes'></div>"
"<div style='margin-top:10px;font-size:12px;color:var(--dim);text-align:center;min-height:18px' id='sceneDesc'>--</div></div>"

"<div class='sec'>固件更新</div>"
"<div class='card ota'><input type='file' id='fw' accept='.bin'>"
"<div class='ob' onclick=\"document.getElementById('fw').click()\">选择 .bin 固件文件</div>"
"<div class='os' id='otas'>点击上方选择固件</div>"
"<div class='ob' onclick='doOta()' style='margin-top:8px;border-color:#FF9800;color:#FF9800'>上传并刷写</div></div>"

"<div style='text-align:center;padding:16px;font-size:12px;color:var(--dim)'>"
"<a href='/config'>高级配置</a></div>"
"</div>"

"<script>"
"var PN=['C1','C2','C3','USB-A'],PM={};"
"var SCENES=[{v:1,n:'AI模式',i:'\\u{1F916}',d:'自动识别设备智能匹配最优充电功率'},{v:2,n:'数码生态',i:'\\u{1F4BB}',d:'多口同时充电均衡分配功率'},{v:3,n:'单口模式',i:'\\u{1F50C}',d:'单口最大功率输出优先C1口'},{v:4,n:'均衡模式',i:'\\u{2696}\\uFE0F',d:'多个端口均衡分配充电功率'}];"
"var SN={1:'AI模式',2:'数码生态',3:'单口模式',4:'均衡模式'};"
"var SCR={0:'5分钟',1:'1分钟',2:'10分钟',3:'30分钟',4:'常亮'};"
"var LNG={0:'English',1:'中文'};"
"var PM2={};"

"var PMAP=[{p:'c1',n:'C1',ps:[{n:'PD',b:0},{n:'PPS',b:1},{n:'UFCS',b:2}]},{p:'c2',n:'C2',ps:[{n:'PD',b:8},{n:'PPS',b:9},{n:'UFCS',b:10}]},{p:'c3',n:'C3',ps:[{n:'UFCS',b:16},{n:'SCP',b:17}]},{p:'a',n:'USB-A',ps:[{n:'UFCS',b:24},{n:'SCP',b:25}]}];"

"function init(){"
"var h='';"
"['c1','c2','c3','a'].forEach(function(p,i){"
"h+='<div class=\"row\"><span class=\"rl\">'+PN[i]+'</span>'"
"+'<label class=\"tg\"><input type=\"checkbox\" id=\"pc_'+p+'\" onchange=\"setPort(\\''+p+'\\',this.checked?\\'on\\':\\'off\\')\"><span class=\"sl\"></span></label></div>';});"
"document.getElementById('portctl').innerHTML=h;"

"h='';SCENES.forEach(function(s){"
"h+='<div class=\"scb\" id=\"sc'+s.v+'\" onclick=\"setS(5,'+s.v+')\">'"
"+'<div class=\"si\">'+s.i+'</div><div class=\"sn\">'+s.n+'</div></div>';});"
"document.getElementById('scenes').innerHTML=h;"

"h='';PMAP.forEach(function(pg){"
"h+='<div class=\"ph\">'+pg.n+'</div><div class=\"pg\">';"
"pg.ps.forEach(function(pr){"
"h+='<div class=\"pcb\" id=\"pb_'+pg.p+'_'+pr.n.toLowerCase()+'\" onclick=\"setProto(\\''+pg.p+'\\',\\''+pr.n.toLowerCase()+'\\')\">'+pr.n+'</div>';});"
"h+='</div>';});"
"document.getElementById('protos').innerHTML=h;"
"}"
"init();"

"function upd(){"
"fetch('/api/ports').then(function(r){return r.json()}).then(function(d){"
"var h='',tp=0;var PC=['var(--c1)','var(--c2)','var(--c3)','var(--a)'];"
"d.forEach(function(p,i){tp+=p.power;"
"h+='<div class=\"pb\"><div class=\"pn\">'+PN[i]+'</div>';"
"h+='<div class=\"pw\" style=\"color:'+PC[i]+'\">'+p.power.toFixed(1)+'<span class=\"w\">W</span></div>';"
"h+='<div class=\"pvi\">'+p.voltage.toFixed(1)+'V  '+p.current.toFixed(1)+'A</div>';"
"h+='<div class=\"pp\" style=\"background:'+(p.active?'rgba(76,175,80,0.2)':'rgba(255,255,255,0.05)');"
"h+=';color:'+(p.active?'#81C784':'var(--dim)')+'\">'+p.protocol+'</div></div>';});"
"document.getElementById('ports').innerHTML=h;"
"document.getElementById('tp').textContent=tp.toFixed(1);"
"}).catch(function(){});"

"fetch('/api/settings').then(function(r){return r.json()}).then(function(d){"
"PM=d;"
"['c1','c2','c3','a'].forEach(function(p,i){"
"var el=document.getElementById('pc_'+p);"
"if(el)el.checked=!!(d['16']&(1<<(i==3?3:i)));});"
"document.getElementById('v5').textContent=SN[d['5']]||'--';"
"document.getElementById('v6').textContent=SCR[d['6']]||'--';"
"document.getElementById('v13').textContent=LNG[d['13']]||'--';"
"document.getElementById('s15').checked=!!d['15'];"
"document.getElementById('s19').checked=!!d['19'];"
"document.getElementById('s20').checked=!!d['20'];"
"document.getElementById('bleEn').checked=!!d['ble_enabled'];"

"SCENES.forEach(function(s){"
"var el=document.getElementById('sc'+s.v);"
"if(el)el.className='scb'+(d['5']==s.v?' active':'');});"
"var sd=document.getElementById('sceneDesc');"
"if(sd){var sv=d['5'];SCENES.forEach(function(s){if(s.v===sv)sd.textContent=s.d;});}"
"var v21=d['21']||0;"
"PMAP.forEach(function(pg){pg.ps.forEach(function(pr){"
"var el=document.getElementById('pb_'+pg.p+'_'+pr.n.toLowerCase());"
"if(!el)return;"
"el.className='pcb'+((v21&(1<<pr.b))?' on':'');"
"var pdOff=(pg.p==='c1'||pg.p==='c2')&&!(v21&(1<<(pg.p==='c1'?0:8)));"
"var dis=pr.n.toLowerCase()==='pps'&&pdOff;"
"el.style.opacity=dis?'0.3':'1';"
"el.style.pointerEvents=dis?'none':'auto';"
"});});"
"}).catch(function(){});}"

"function setPort(p,a){"
"fetch('/api/port',{method:'POST',headers:{'Content-Type':'application/json'},"
"body:JSON.stringify({port:p,action:a})}).then(function(){setTimeout(upd,500);});}"

"function setProto(p,proto){"
"var v=PM['21']||0;var b=0;"
"PMAP.forEach(function(pg){if(pg.p===p)pg.ps.forEach(function(pr){if(pr.n.toLowerCase()===proto)b=pr.b;});});"
"var on=!(v&(1<<b));"
"fetch('/api/protocol',{method:'POST',headers:{'Content-Type':'application/json'},"
"body:JSON.stringify({port:p,protocol:proto,action:on?'on':'off'})}).then(function(){setTimeout(upd,500);});}"

"function setS(piid,v){"
"fetch('/api/setting',{method:'POST',headers:{'Content-Type':'application/json'},"
"body:JSON.stringify({piid:piid,value:v})}).then(function(){setTimeout(upd,500);});}"

"function toggleTheme(){"
"var b=document.body;var isLight=b.classList.contains('light');"
"b.classList.toggle('light');"
"localStorage.setItem('theme',isLight?'dark':'light');"
"document.getElementById('thBtn').textContent=isLight?'\u2600\uFE0F':'\u{1F319}';}"

"(function(){var t=localStorage.getItem('theme');"
"if(t==='light'){document.body.classList.add('light');"
"document.getElementById('thBtn').textContent='\u{1F319}';}"
"else{document.getElementById('thBtn').textContent='\u2600\uFE0F';}})();"

"setInterval(upd,2000);upd();"

"function updBle(){"
"fetch('/api/settings').then(function(r){return r.json()}).then(function(d){"
"var st=d['ble_enabled'];"
"document.getElementById('bleEn').checked=!!st;"
"document.getElementById('bleSt').textContent=st?'已启用':'已禁用';"
"document.getElementById('bleSt').style.color=st?'#81C784':'#888';"
"}).catch(function(){});}"
"setInterval(updBle,5000);updBle();"

"function toggleBle(on){"
"var st=document.getElementById('bleSt');"
"st.textContent='切换中...';"
"fetch('/api/ble',{method:'POST',headers:{'Content-Type':'application/json'},"
"body:JSON.stringify({enabled:on})}).then(function(){setTimeout(updBle,2000);});"
"}"

"function doOta(){"
"var f=document.getElementById('fw').files[0];"
"var s=document.getElementById('otas');"
"if(!f){s.textContent='请选择.bin文件';return;}"
"s.textContent='上传中 '+f.name+' ('+f.size+' bytes)...';"
"var xhr=new XMLHttpRequest();"
"xhr.upload.onprogress=function(e){if(e.lengthComputable)s.textContent='上传中... '+Math.round(e.loaded/e.total*100)+'%';};"
"xhr.onload=function(){try{var d=JSON.parse(xhr.responseText);s.textContent=d.message;s.style.color='#4CAF50'}catch(e){s.textContent='错误: '+xhr.status;s.style.color='#f44336'}};"
"xhr.onerror=function(){s.textContent='上传失败';s.style.color='#f44336'};"
"xhr.open('POST','/api/ota');xhr.send(f);}"
"</script></body></html>";

static int _get_dash_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "text/html");
    httpd_resp_send_chunk(req, _DASH, sizeof(_DASH) - 1);
    httpd_resp_send_chunk(req, NULL, 0);
    return 0;
}

/* ==================== Config Page HTML ==================== */

static const char _CFG_HTML[] =
"<!DOCTYPE html><html><head><meta charset='utf-8'>"
"<meta name='viewport' content='width=device-width,initial-scale=1'>"
"<title>CUKTECH Config</title><style>"
"body{font-family:system-ui;max-width:600px;margin:0 auto;padding:20px;background:#f5f5f5}"
"h1{text-align:center;color:#333}"
".card{background:#fff;border-radius:8px;padding:20px;margin:10px 0;box-shadow:0 2px 4px rgba(0,0,0,0.1)}"
"label{display:block;margin:8px 0 4px;font-weight:600;font-size:14px}"
"input{width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;box-sizing:border-box;font-size:14px}"
"button{width:100%;padding:12px;background:#2196F3;color:#fff;border:none;border-radius:4px;font-size:16px;cursor:pointer;margin-top:12px}"
".toggle{display:flex;align-items:center;gap:10px;margin:8px 0}"
".toggle input{display:none}"
".toggle .sl{width:48px;height:26px;background:#ccc;border-radius:13px;cursor:pointer;position:relative;transition:.3s}"
".toggle .sl::after{content:'';width:22px;height:22px;background:#fff;border-radius:50%;position:absolute;top:2px;left:2px;transition:.3s}"
".toggle input:checked+.sl{background:#4CAF50}"
".toggle input:checked+.sl::after{left:24px}"
".section{font-size:16px;font-weight:bold;color:#2196F3;margin-top:16px;border-bottom:1px solid #eee;padding-bottom:4px}"
"</style></head><body>"
"<h1>CUKTECH Config</h1>"
"<div class='card'><form id='f'>"
"<div class='section'>WiFi</div>"
"<label>SSID</label><input id='wifi_ssid' required>"
"<label>Password</label><input id='wifi_pass' type='password'>"
"<div class='section'>Device</div>"
"<label>MAC</label><input id='device_mac' required>"
"<label>Token</label><input id='device_token' required>"
"<label>BLE Key</label><input id='device_ble_key' required>"
"<div class='section'>MQTT</div>"
"<div class='toggle'><input type='checkbox' id='mqtt_enable' checked><label class='sl' for='mqtt_enable'></label><span>Enable MQTT</span></div>"
"<label>Broker</label><input id='mqtt_broker'>"
"<label>Port</label><input id='mqtt_port' type='number' value='1883'>"
"<label>Username</label><input id='mqtt_user'>"
"<label>Password</label><input id='mqtt_pass' type='password'>"
"<label>Topic Prefix</label><input id='mqtt_topic_prefix' value='cuktech/charger'>"
"<button type='submit'>Save & Reboot</button>"
"</form></div>"
"<script>"
"fetch('/api/config').then(function(r){return r.json()}).then(function(d){"
"Object.keys(d).forEach(function(k){var e=document.getElementById(k);if(!e)return;"
"if(e.type==='checkbox'){e.checked=!!d[k]}else{e.value=d[k]}});});"
"document.getElementById('f').onsubmit=function(e){"
"e.preventDefault();var d={};"
"['wifi_ssid','wifi_pass','device_mac','device_token','device_ble_key',"
"'mqtt_broker','mqtt_user','mqtt_pass','mqtt_topic_prefix'].forEach(function(k){d[k]=document.getElementById(k).value});"
"d.mqtt_port=parseInt(document.getElementById('mqtt_port').value)||1883;"
"d.mqtt_enable=document.getElementById('mqtt_enable').checked;"
"fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})"
".then(function(r){return r.json()}).then(function(d){alert(d.message);setTimeout(function(){window.location='/'},2000)});"
"};</script></body></html>";

static int _get_config_page_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "text/html");
    httpd_resp_send_chunk(req, _CFG_HTML, sizeof(_CFG_HTML) - 1);
    httpd_resp_send_chunk(req, NULL, 0);
    return 0;
}

/* ==================== Server Start ==================== */

void http_server_start(DeviceConfig *cfg, http_config_cb on_save) {
    _cfg = cfg;
    _on_save = on_save;

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 20;
    config.server_port = 80;
    config.max_resp_headers = 4096;
    config.stack_size = 8192;

    if (httpd_start(&_server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start HTTP server");
        return;
    }

    const httpd_uri_t uris[] = {
        { .uri = "/api/config",   .method = HTTP_GET,  .handler = _get_config_handler },
        { .uri = "/api/config",   .method = HTTP_POST, .handler = _post_config_handler },
        { .uri = "/api/ports",    .method = HTTP_GET,  .handler = _get_ports_handler },
        { .uri = "/api/settings", .method = HTTP_GET,  .handler = _get_settings_handler },
        { .uri = "/api/port",     .method = HTTP_POST, .handler = _post_port_handler },
        { .uri = "/api/setting",  .method = HTTP_POST, .handler = _post_setting_handler },
        { .uri = "/api/protocol", .method = HTTP_POST, .handler = _post_protocol_handler },
        { .uri = "/api/ble",      .method = HTTP_POST, .handler = _post_ble_handler },
        { .uri = "/api/ota",      .method = HTTP_POST, .handler = _post_ota_handler },
        { .uri = "/",             .method = HTTP_GET,  .handler = _get_dash_handler },
        { .uri = "/dashboard",    .method = HTTP_GET,  .handler = _get_dash_handler },
        { .uri = "/config",       .method = HTTP_GET,  .handler = _get_config_page_handler },
        { .uri = "/*",            .method = HTTP_GET,  .handler = _get_dash_handler },
    };
    for (int i = 0; i < sizeof(uris)/sizeof(uris[0]); i++) {
        httpd_register_uri_handler(_server, &uris[i]);
    }
    ESP_LOGI(TAG, "HTTP server started on port 80");
}

void http_server_stop(void) {
    if (_server) { httpd_stop(_server); _server = NULL; }
}
