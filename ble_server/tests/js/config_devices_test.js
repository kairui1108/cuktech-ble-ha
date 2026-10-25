#!/usr/bin/env node
/**
 * config.html 设备卡片列表的契约测试（多设备 §3.12 / §3.10）。
 *
 * 为什么单独测 config.html 的那段内联脚本：它是整个多设备改造里**唯一会写配置**
 * 的地方，而保存 payload 的形状有两个硬约束，写错了不会报错、只会在重启后
 * 悄悄变成另一种配置：
 *   1. 第一台必须展开回 `ble:` 节（老工具/脚本只认这个节，primary 的 id 恒为
 *      default 也是锚在这里）；
 *   2. 其余设备进 `devices:` 节，字段名必须与 config.py 的 from_mapping 一致
 *      （id/name/mac/token/ble_key/topic_prefix/bemfa_*）。
 * 另外锁住"primary 不可删除"这条前端提示（服务端也会校验，但让用户点了没反应
 * 是最差的一种交互）。
 *
 * Usage: node tests/js/config_devices_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.resolve(__dirname, '../../web');

let failed = 0;
let passed = 0;
function ok(cond, label, detail) {
    if (cond) { passed++; console.log(`  ok   ${label}`); }
    else { failed++; console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`); }
}
function eq(actual, expected, label) {
    ok(JSON.stringify(actual) === JSON.stringify(expected), label,
       `got ${JSON.stringify(actual)} want ${JSON.stringify(expected)}`);
}

function extractInlineScripts(htmlPath) {
    const html = fs.readFileSync(htmlPath, 'utf8');
    const scripts = [];
    const re = /<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g;
    let m;
    while ((m = re.exec(html))) scripts.push(m[1]);
    return scripts.join('\n');
}

// 服务端真实形状的桩：/api/config 只给 primary 的凭据（掩码），/api/devices 给设备清单
const CONFIG_PAYLOAD = {
    ok: true,
    config: {
        ble: { mac: 'AA:BB:CC:DD:EE:01', token: '****1234', ble_key: '****abcd', scan_timeout: 10 },
        mqtt: { enabled: false, host: '', port: 1883, username: '', password: '', topic_prefix: 'cuktech/charger' },
        bemfa: { enabled: false, uid: '', name_c1: '', name_c2: '', name_c3: '', name_a: '', name_ble: '' },
        server: { port: 8199, log_level: 'info', history_retention_days: 2 },
    },
};
const DEVICES_PAYLOAD = {
    ok: true,
    devices: [
        { id: 'default', name: '书房充电器', mac: 'AA:BB:CC:DD:EE:01', connected: true, topic_prefix: 'cuktech/charger', is_primary: true },
        { id: 'desk', name: '书桌充电器', mac: 'AA:BB:CC:DD:EE:02', connected: false, topic_prefix: 'cuktech/charger/desk', is_primary: false },
    ],
};

function buildSandbox() {
    const els = new Map();
    const posts = [];
    function makeEl(id) {
        const el = {
            id: id || '', value: '', checked: false, textContent: '', innerHTML: '', hidden: false,
            style: { setProperty() {}, getPropertyValue() { return ''; }, removeProperty() {}, cssText: '' },
            dataset: {}, className: '', children: [], options: [],
            classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
            setAttribute() {}, getAttribute() { return null; }, appendChild() {}, remove() {},
            addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
            closest() { return null; }, focus() {}, blur() {},
        };
        return el;
    }
    function getEl(id) {
        if (!els.has(id)) els.set(id, makeEl(id));
        return els.get(id);
    }
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        Intl, URL,
        setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; },
        clearTimeout() {}, setInterval: () => 0, clearInterval() {},
        Promise, JSON, Math, Date, RegExp, String, Number, Boolean, Object, Array,
        isNaN, parseFloat, parseInt, Error, Map, Set,
        fetch: (url, init) => {
            const u = String(url);
            if (init && init.method === 'POST') {
                posts.push({ url: u, body: JSON.parse(init.body || '{}') });
                // 小米云批量导入：返回的载荷由用例通过 __xiaomiImport 设定
                if (u.indexOf('/api/xiaomi/import') >= 0) {
                    return Promise.resolve({
                        ok: true, status: 200,
                        json: () => Promise.resolve(sandbox.__xiaomiImport
                            || { ok: true, devices: [] }),
                    });
                }
                return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
            }
            if (u.indexOf('/api/devices') >= 0) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(DEVICES_PAYLOAD) });
            if (u.indexOf('/api/config') >= 0) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(CONFIG_PAYLOAD) });
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
        },
        confirm: () => sandbox.__confirmAnswer !== false,
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        navigator: { language: 'zh-CN' },
        location: { origin: 'http://charger.local', href: 'http://charger.local/config.html' },
        getComputedStyle: () => ({ getPropertyValue: () => '' }),
        addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
        CustomEvent: function (type, init) { this.type = type; this.detail = init && init.detail; },
    };
    sandbox.window = sandbox;
    sandbox.document = {
        documentElement: makeEl('html'), body: makeEl('body'), head: makeEl('head'),
        title: '', readyState: 'complete',
        getElementById: getEl, querySelector: () => null, querySelectorAll: () => [],
        createElement: () => makeEl('new'), addEventListener() {},
    };
    vm.createContext(sandbox);
    for (const f of ['static/locales/zh-CN.js', 'static/locales/en.js', 'static/i18n.js']) {
        vm.runInContext(fs.readFileSync(path.join(WEB, f), 'utf8'), sandbox, { filename: f });
    }
    vm.runInContext(extractInlineScripts(path.join(WEB, 'config.html')), sandbox, { filename: 'config.html' });
    sandbox.__posts = posts;
    return sandbox;
}

function peek(ctx, expr) { return vm.runInContext(expr, ctx); }

const ctx = buildSandbox();
const devices = () => JSON.parse(peek(ctx, 'JSON.stringify(_devices)'));

// loadConfig() 是文件末尾直接调用的 async 函数：等它的微任务跑完再断言
setTimeout(async () => {
    console.log('\n-- 加载：设备清单来自 /api/devices，凭据来自 /api/config --');
    {
        const list = devices();
        eq(list.length, 2, '两台设备各生成一张卡片');
        eq(list[0].is_primary, true, '第一张恒为 primary');
        eq(list[0].id, 'default', 'primary 的 id 固定为 default（不可配置）');
        eq(list[0].name, '书房充电器', 'primary 名字取自 /api/devices');
        eq(list[0].token, '****1234', 'primary 的 token 用 /api/config 的掩码值预填（原值不下发）');
        eq(list[1].id, 'desk', '第二台是附加设备');
        eq(list[1].topic_prefix, 'cuktech/charger/desk', '附加设备的 topic 前缀跟着设备走');
        eq(list[1].token, '', '非 primary 的 token 页面拿不到 → 留空 = 保持不变');
        ok(peek(ctx, 'document.getElementById("deviceList").innerHTML').indexOf('device-card-1') >= 0,
           '两张卡片确实渲染进列表');
        ok(peek(ctx, 'document.getElementById("deviceList").innerHTML').indexOf('主设备') >= 0,
           'primary 卡片上标了"主设备"');
    }

    console.log('\n-- primary 不可删除（前端提示，服务端另有校验） --');
    {
        peek(ctx, 'removeDevice(0)');
        eq(devices().length, 2, '对 primary 调删除 → 列表不变');
        peek(ctx, 'removeDevice(1)');
        eq(devices().length, 1, '对附加设备调删除 → 真删（confirm 通过时）');
        // 复原成两台，供后面的保存用例使用
        peek(ctx, `_devices.splice(1, 0, { id: 'desk', name: '书桌充电器', mac: 'AA:BB:CC:DD:EE:02',
                 token: '', ble_key: '', topic_prefix: '', bemfa_ble: '', is_primary: false, connected: false })`);
    }

    console.log('\n-- 保存 payload：第一台展开回 ble: 节，其余进 devices: 节 --');
    {
        // 用户在新加的设备上填了 id/mac/token
        peek(ctx, 'addDevice()');
        eq(devices().length, 3, 'addDevice() 追加一张卡片');
        peek(ctx, `updateDeviceField(2, 'id', 'balcony')`);
        peek(ctx, `updateDeviceField(2, 'name', '阳台充电器')`);
        peek(ctx, `updateDeviceField(2, 'mac', 'AA:BB:CC:DD:EE:03')`);
        peek(ctx, `updateDeviceField(2, 'token', 'tok-balcony')`);
        // 第二台补上凭据（页面拿不到原值，用户重填才会上报）
        peek(ctx, `updateDeviceField(1, 'token', 'tok-desk')`);

        await peek(ctx, 'saveConfig()');
        const post = ctx.__posts.filter((p) => p.url.indexOf('/api/config') >= 0).pop();
        ok(!!post, '保存走了 POST /api/config');
        const cfg = post.body.config;

        eq(cfg.ble.mac, 'AA:BB:CC:DD:EE:01', 'ble 节拿到的就是 primary 的 mac');
        eq(cfg.ble.token, '****1234', 'primary 的掩码 token 原样回传（服务端据此保留原值）');
        eq(cfg.ble.name, '书房充电器', 'primary 的显示名随卡片一起回写（与 /api/devices 报来的一致）');

        eq(cfg.devices.length, 2, 'devices 节 = 除 primary 外的两台');
        eq(cfg.devices[0].id, 'desk', '附加设备 id 字段名与 config.py 一致');
        eq(cfg.devices[0].token, 'tok-desk', '用户重填的 token 上报');
        ok(!('bemfa_ble' in cfg.devices[0]), '空的 bemfa 覆盖项不写进 YAML');
        eq(cfg.devices[1].id, 'balcony', '新加的设备也进 devices 节');
        eq(cfg.devices[1].name, '阳台充电器', '名字一并保存');
        eq(cfg.devices[1].mac, 'AA:BB:CC:DD:EE:03', 'mac 一并保存');
        ok(!('is_primary' in cfg.devices[1]) && !('connected' in cfg.devices[1]),
           'payload 只带配置字段（不含运行时的 is_primary/connected）');

        // 清空名字：不能写空串。GET /api/config 不返回 ble.name，"没设置"与
        // "被清空"在页面上无法区分，写空串会误删配置里已有的名字 —— 宁可这次不清。
        peek(ctx, `updateDeviceField(0, 'name', '')`);
        await peek(ctx, 'saveConfig()');
        const postCleared = ctx.__posts.filter((p) => p.url.indexOf('/api/config') >= 0).pop();
        ok(!('name' in postCleared.body.config.ble), '名字清空时不写 ble.name（保住配置里已有的名字）');

        // 主设备起名后要写回 ble.name
        peek(ctx, `updateDeviceField(0, 'name', '书房主充')`);
        await peek(ctx, 'saveConfig()');
        const post2 = ctx.__posts.filter((p) => p.url.indexOf('/api/config') >= 0).pop();
        eq(post2.body.config.ble.name, '书房主充', 'primary 起了名就写进 ble.name');
    }

    console.log('\n-- 本地校验拦下明显会起不来的配置 --');
    {
        // 非法设备 id：服务端 validate_device_specs 会拒绝启动，必须在前端先拦
        peek(ctx, `updateDeviceField(1, 'id', 'Desk 1')`);
        const before = ctx.__posts.length;
        await peek(ctx, 'saveConfig()');
        eq(ctx.__posts.length, before, '非法 id → 不提交（不会被重启后才发现的错误拖下水）');
        peek(ctx, `updateDeviceField(1, 'id', 'desk')`);

        // 缺 token
        peek(ctx, `updateDeviceField(1, 'token', '')`);
        const before2 = ctx.__posts.length;
        await peek(ctx, 'saveConfig()');
        eq(ctx.__posts.length, before2, '缺 token → 不提交');
    }

    console.log('\n-- 小米云：勾选多台一次导入 + 名称同步 --');
    {
        // 云端清单：desk 是**已配置**的（MAC 大小写/分隔符不同，必须认成同一台），
        // 另一台是新设备（英文名 → 用它生成合法 id）
        peek(ctx, `showDeviceList({ cuktech: [
            { did: 'did-desk', name: '书桌（小米云）', mac: 'AABBCCDDEE02', model: 'njcuk01' },
            { did: 'did-new', name: 'Bedroom', mac: 'aa:bb:cc:dd:ee:09', model: 'njcuk01' },
        ] })`);
        eq(peek(ctx, 'JSON.stringify(_xiaomiDevices.length)'), '2', '清单落在页面上（不是逐台点选）');
        eq(peek(ctx, 'JSON.stringify(Array.from(_xiaomiSelected))'), '["did-new"]',
           '默认只勾"还没配置过"的那台（已配置的不会被重复添加）');
        ok(peek(ctx, 'document.getElementById("xiaomiDevices").innerHTML').indexOf('已配置') >= 0,
           '已配置的设备在清单里标出来');
        ok(peek(ctx, 'document.getElementById("xiaomiDevices").innerHTML').indexOf('checkbox') >= 0,
           '每行是勾选框（多选的前提）');

        // 勾上全部 + 同步名称，一次导入
        peek(ctx, `_xiaomiSessionId = 'sess1'`);
        const before = devices();
        peek(ctx, `document.getElementById('xiaomiSyncNames').checked = true; xiaomiSelectAll(true)`);
        ctx.__xiaomiImport = {
            ok: true,
            devices: [
                { did: 'did-desk', name: '书桌（小米云）', mac: 'AABBCCDDEE02',
                  token: 'new-token-desk', model: 'njcuk01', ble_key: 'key-desk' },
                { did: 'did-new', name: 'Bedroom', mac: 'aa:bb:cc:dd:ee:09',
                  token: 'new-token-2', model: 'njcuk01', ble_key: 'key-2' },
            ],
        };
        await peek(ctx, 'importXiaomiSelected()');

        const post = ctx.__posts.filter((p) => p.url.indexOf('/api/xiaomi/import') >= 0).pop();
        ok(!!post, '导入走 /api/xiaomi/import（批量端点）');
        eq(post.body.dids.sort(), ['did-desk', 'did-new'], '一次把选中的 did 全发出去');
        eq(post.body.session_id, 'sess1', '会话 id 跟着走');

        const after = devices();
        const byMac = (list, mac) => list.find((d) =>
            String(d.mac || '').replace(/[^0-9a-fA-F]/g, '').toLowerCase() === mac);
        eq(after.length, before.length + 1, '已配置的更新、新的追加（只多一张卡片）');
        const desk = byMac(after, 'aabbccddee02');
        eq(desk.token, 'new-token-desk', '按 MAC 匹配到已有卡片并更新 token');
        eq(desk.ble_key, 'key-desk', 'BLE Key 一起更新');
        eq(desk.name, '书桌（小米云）', '勾了同步名称 → 覆盖卡片里的旧名字');
        const fresh = byMac(after, 'aabbccddee09');
        ok(!!fresh, '新设备被追加进卡片列表');
        eq(fresh.name, 'Bedroom', '新设备的名称来自云端（同步名称的核心诉求）');
        eq(fresh.id, 'bedroom', '英文名生成合法设备 id（小写 slug）');
        eq(fresh.token, 'new-token-2', 'token 写入');
        eq(fresh.ble_key, 'key-2', 'BLE Key 写入');
        ok(peek(ctx, 'document.getElementById("xiaomiImportStatus").textContent').indexOf('1') >= 0,
           '导入结果给用户看（新增/更新计数）');
    }

    console.log('\n-- 小米云：不勾"同步名称"时保留用户自己起的名字 --');
    {
        peek(ctx, `findDeviceIndex(d => normMac(d.mac) === 'aabbccddee02')`);
        peek(ctx, `_devices[findDeviceIndex(d => normMac(d.mac) === 'aabbccddee02')].name = '我自己起的名字'`);
        peek(ctx, `showDeviceList({ cuktech: [
            { did: 'did-desk', name: '书桌（小米云）', mac: 'aabbccddee02', model: 'njcuk01' } ] })`);
        peek(ctx, `document.getElementById('xiaomiSyncNames').checked = false; xiaomiSelectAll(true)`);
        ctx.__xiaomiImport = {
            ok: true,
            devices: [{ did: 'did-desk', name: '书桌（小米云）', mac: 'aabbccddee02',
                        token: 'rotated-token', model: 'njcuk01', ble_key: 'key-3' }],
        };
        await peek(ctx, 'importXiaomiSelected()');
        const after = devices();
        const desk = after.find((d) =>
            String(d.mac || '').replace(/[^0-9a-fA-F]/g, '').toLowerCase() === 'aabbccddee02');
        eq(desk.name, '我自己起的名字', '没勾同步名称 → 用户起的名字不能被覆盖');
        eq(desk.token, 'rotated-token', '但凭据照常更新（换 token 是导入的主要用途）');
    }

    console.log('\n-- 小米云：没选设备时不该发请求 --');
    {
        const before = ctx.__posts.filter((p) => p.url.indexOf('/api/xiaomi/import') >= 0).length;
        peek(ctx, 'xiaomiSelectAll(false)');
        await peek(ctx, 'importXiaomiSelected()');
        eq(ctx.__posts.filter((p) => p.url.indexOf('/api/xiaomi/import') >= 0).length, before,
           '空选择 → 不发请求（只在界面提示）');
        eq(peek(ctx, 'JSON.stringify(_xiaomiDevices.length)'), '1', '清单还在（可以改选）');
    }

    console.log(failed === 0
        ? `\nAll ${passed} config device-list checks passed.`
        : `\n${failed}/${passed + failed} config device-list checks FAILED.`);
    process.exit(failed === 0 ? 0 : 1);
}, 0);
