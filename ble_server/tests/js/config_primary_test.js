#!/usr/bin/env node
/**
 * config.html 主设备生命周期契约（§3.10）：首次导入 / 换主设备 / 清空主设备。
 *
 * 为什么单独一个套件：`ble:` 节渲染出来的那张卡是**不可删除**的（它同时是 legacy
 * MQTT 主题、历史分区和 HA unique_id 的锚点），所以它一旦和实物对不上，用户就会
 * 永远卡在一张"删不掉、又连不上"的孤儿主设备上。真实 bug 的形态是：
 *
 *   全新安装（`ble:` 还是空的）→ 扫码导入 → 导入**只按 MAC 匹配**、匹配不到就
 *   `push` 一张新卡 → 于是出现"空主设备 + 真实设备"两张卡；主设备删不掉，保存又
 *   被 §3.10 的校验拦下（有附加设备时 primary 必须配齐 MAC/Token）。
 *
 * 现在这条链路被三个契约锁住：
 *   1. 空主设备时，导入的**第一台**设备直接写进主设备（不再新增卡片）；
 *   2. 已经配好的主设备不会被导入覆盖（仍然按 MAC 匹配，截图那种"同名不同 MAC"
 *      的残留设备只会作为第二张卡出现，主设备身份不动）；
 *   3. 主设备可以"换"（设为主设备）和"清"（清空），保存时把权威身份交给服务端
 *      （统一数组 + clear_credentials，见 tests/test_primary_lifecycle.py）。
 *
 * Usage: node tests/js/config_primary_test.js
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

/** 全新安装：`ble:` 里只有模板占位符，`devices:` 一节都没有。 */
const FRESH_BLE = { mac: '', token: '', ble_key: '', scan_timeout: 10, name: '' };
const FRESH_DEVICES = [
    { id: 'default', name: 'default', mac: '', connected: false,
      topic_prefix: 'cuktech/charger', is_primary: true },
];

const CLOUD_DEVICE = {
    did: 'did-1', name: 'CUKTECH 10 GaN Charger Ultra', mac: '3C:CD:73:34:1E:E0',
    token: 'c1b1bb8d51535f31863dc162', model: 'njcuk.fitting.ad1204',
    ble_key: '0802b8f8ab110732ad95c8e94bc9fecf',
};

function buildSandbox({ ble = FRESH_BLE, devices = FRESH_DEVICES } = {}) {
    const els = new Map();
    const posts = [];
    function makeEl(id) {
        const el = {
            id: id || '', value: '', checked: false, textContent: '', innerHTML: '', hidden: false,
            style: { setProperty() {}, getPropertyValue() { return ''; }, removeProperty() {}, cssText: '' },
            dataset: {}, className: '', children: [], options: [], attrs: {},
            classList: {
                _set: new Set(),
                add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
                toggle(c, on) { if (on === undefined ? !this._set.has(c) : on) this._set.add(c); else this._set.delete(c); },
                contains(c) { return this._set.has(c); },
            },
            setAttribute(k, v) { this.attrs[k] = String(v); },
            getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
            appendChild() {}, remove() {},
            addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
            closest() { return null; }, focus() {}, blur() {},
        };
        return el;
    }
    function getEl(id) {
        if (!els.has(id)) els.set(id, makeEl(id));
        return els.get(id);
    }

    const configPayload = {
        ok: true,
        config: {
            ble: { ...ble },
            mqtt: { enabled: false, host: '', port: 1883, username: '', password: '', topic_prefix: 'cuktech/charger' },
            bemfa: { enabled: false, uid: '', name_c1: '', name_c2: '', name_c3: '', name_a: '', name_ble: '',
                     entities: [], entitiesDefined: false, ports: ['c1', 'c2', 'c3', 'a', 'ble'],
                     derivedEntities: [], allowedDevices: ['default'] },
            server: { port: 8199, log_level: 'info', history_retention_days: 2 },
        },
    };
    const devicesPayload = { ok: true, devices: devices.map((d) => ({ ...d })) };

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
                if (u.indexOf('/api/xiaomi/import') >= 0) {
                    return Promise.resolve({
                        ok: true, status: 200,
                        json: () => Promise.resolve(sandbox.__xiaomiImport || { ok: true, devices: [] }),
                    });
                }
                return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
            }
            if (u.indexOf('/api/devices') >= 0) {
                return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(devicesPayload) });
            }
            if (u.indexOf('/api/config') >= 0) {
                return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(configPayload) });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
        },
        confirm: () => sandbox.__confirmAnswer !== false,
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        navigator: { language: 'zh-CN' },
        // reload() 必须存在：saveConfig 成功后会 setTimeout(reload)。沙箱的
        // setTimeout 是同步执行，没有它就会在 try 里抛类型错误、被页面的
        // catch 当成"网络错误"吞掉（不会挂测试，但会污染状态）。
        location: { origin: 'http://charger.local', href: 'http://charger.local/config.html', reload() {} },
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
const devicesOf = (ctx) => JSON.parse(peek(ctx, 'JSON.stringify(_devices)'));
const htmlOf = (ctx) => peek(ctx, 'document.getElementById("deviceList").innerHTML');
const lastPost = (ctx) => ctx.__posts.filter((p) => p.url.indexOf('/api/config') >= 0).pop();

/** 等 loadConfig() 的 await 链跑完（fetch 桩全是已 resolve 的 Promise）。 */
function boot(opts) {
    const ctx = buildSandbox(opts);
    return new Promise((resolve) => setImmediate(() => resolve(ctx)));
}

/** 走一遍"扫码 → 勾选 → 导入"，等价于用户点「小米云自动获取」。 */
async function importDevices(ctx, list, { syncNames = true } = {}) {
    peek(ctx, `_xiaomiSessionId = 'sess1'`);
    const cloud = list.map((d) => ({ did: d.did, name: d.name, mac: d.mac, model: d.model }));
    peek(ctx, `showDeviceList(${JSON.stringify({ cuktech: cloud })})`);
    peek(ctx, `xiaomiSelectAll(true)`);
    peek(ctx, `document.getElementById('xiaomiSyncNames').checked = ${syncNames ? 'true' : 'false'}`);
    ctx.__xiaomiImport = { ok: true, devices: list };
    await peek(ctx, 'importXiaomiSelected()');
}

(async () => {
    console.log('\n-- 全新安装：主设备是一张"待配置"占位卡，不是一台设备 --');
    {
        const ctx = await boot();
        const list = devicesOf(ctx);
        eq(list.length, 1, '只有一张主设备卡');
        eq(list[0].is_primary, true, '它是 primary');
        ok(htmlOf(ctx).indexOf('待配置') >= 0, '卡片上标了"待配置"');
        ok(htmlOf(ctx).indexOf('删除') < 0, '空主设备没有删除按钮（它本来就删不掉）');
        eq(peek(ctx, 'document.getElementById("deviceOnboardingHint").hidden'), false,
           '引导条出现：给用户两条入口（扫码 / 手填）');
        const hintText = peek(ctx, 'document.getElementById("deviceOnboardingHint").textContent');
        ok(hintText.indexOf('小米云自动获取') >= 0 && hintText.indexOf('MAC') >= 0,
           `引导只讲入口，不复述"第一台自动收编"（实际：${hintText}）`);
    }

    console.log('\n-- 首次扫码导入一台：直接写进主设备，不再多出一张卡 --');
    {
        const ctx = await boot();
        await importDevices(ctx, [CLOUD_DEVICE]);

        const list = devicesOf(ctx);
        eq(list.length, 1, '导入后仍然只有一张卡（这就是那个 bug 的修复点）');
        eq(list[0].id, 'default', '它还是主设备（id 恒为 default）');
        eq(list[0].mac, CLOUD_DEVICE.mac, 'MAC 写进了主设备');
        eq(list[0].token, CLOUD_DEVICE.token, 'Token 写进了主设备');
        eq(list[0].ble_key, CLOUD_DEVICE.ble_key, 'BLE Key 写进了主设备');
        eq(list[0].name, CLOUD_DEVICE.name, '勾了同步名称 → 云端名字也写进来');
        eq(peek(ctx, 'document.getElementById("deviceOnboardingHint").hidden'), true,
           '配好之后引导条退场');
        ok(peek(ctx, 'document.getElementById("xiaomiImportStatus").textContent').indexOf('主设备') >= 0,
           '导入结果明确说"写入了主设备"（用户不会去找那张多出来的卡）');

        await peek(ctx, 'saveConfig()');
        const cfg = lastPost(ctx).body.config;
        eq(cfg.devices.length, 1, '保存的是统一数组（只有一项）');
        eq(cfg.devices[0].is_primary, true, '第一项带 is_primary');
        eq(cfg.devices[0].mac, CLOUD_DEVICE.mac, '主设备的 MAC 落盘');
        ok(!cfg.devices[0].clear_credentials, '没有"清空"标记（这是正常配置）');
    }

    console.log('\n-- 导入成功自动关窗；有失败项则留在窗里 --');
    {
        const ctx = await boot();
        await importDevices(ctx, [CLOUD_DEVICE]);
        eq(peek(ctx, 'document.getElementById("xiaomiModal").style.display'), 'none',
           '全部导入成功 → 弹窗自动关闭（少点一次「取消」）');
        ok(peek(ctx, '(document.getElementById("toast") || {}).textContent').indexOf('还没保存') >= 0,
           '关窗后 toast 继续提醒"还没保存"（导入 ≠ 已落盘）');
    }
    {
        const ctx = await boot();
        peek(ctx, `_xiaomiSessionId = 'sess1'`);
        peek(ctx, `showDeviceList(${JSON.stringify({ cuktech: [
            { did: 'did-1', name: '甲', mac: 'AA:BB:CC:DD:EE:01', model: 'njcuk' },
            { did: 'did-9', name: '乙', mac: 'AA:BB:CC:DD:EE:09', model: 'njcuk' } ] })})`);
        peek(ctx, 'xiaomiSelectAll(true)');
        ctx.__xiaomiImport = { ok: true, devices: [
            { did: 'did-1', name: '甲', mac: 'AA:BB:CC:DD:EE:01', token: '1111',
              model: 'njcuk', ble_key: 'k1' },
            { did: 'did-9', error: 'timeout' },
        ] };
        await peek(ctx, 'importXiaomiSelected()');
        ok(peek(ctx, 'document.getElementById("xiaomiModal").style.display') !== 'none',
           '有失败项 → 弹窗不关（用户要看得到是哪台、能不能重试）');
        ok(peek(ctx, 'document.getElementById("xiaomiImportStatus").textContent').indexOf('失败') >= 0,
           '失败信息留在弹窗里');
    }

    console.log('\n-- 主设备为空但设备已经躺在附加卡里：导入把它搬进主设备（不再多一张卡） --');
    {
        const ctx = await boot({
            ble: { mac: '', token: '', ble_key: '', scan_timeout: 10, name: '' },
            devices: [
                { id: 'default', name: 'default', mac: '', connected: false,
                  topic_prefix: 'cuktech/charger', is_primary: true },
                { id: 'desk', name: '书桌充电器', mac: CLOUD_DEVICE.mac, connected: false,
                  topic_prefix: 'cuktech/charger/desk', is_primary: false },
            ],
        });
        eq(devicesOf(ctx).length, 2, '前提：空主设备 + 一张同 MAC 的附加卡');
        await importDevices(ctx, [CLOUD_DEVICE]);
        const list = devicesOf(ctx);
        eq(list.length, 1, '附加卡被搬进主设备槽位（不是只更新附加卡）');
        eq(list[0].id, 'default', '它成了主设备');
        eq(list[0].mac, CLOUD_DEVICE.mac, 'MAC 保留');
        eq(list[0].token, CLOUD_DEVICE.token, 'token 来自导入结果');
        const before = ctx.__posts.length;
        await peek(ctx, 'saveConfig()');
        ok(ctx.__posts.length > before, '保存不再被"主设备为空"拦下');
    }

    console.log('\n-- 导入一半失败：不关窗，但要说明"哪台写进了主设备" --');
    {
        const ctx = await boot();
        peek(ctx, `_xiaomiSessionId = 'sess1'`);
        peek(ctx, `showDeviceList(${JSON.stringify({ cuktech: [
            { did: 'did-1', name: '甲', mac: 'AA:BB:CC:DD:EE:01', model: 'njcuk' },
            { did: 'did-9', name: '乙', mac: 'AA:BB:CC:DD:EE:09', model: 'njcuk' } ] })})`);
        peek(ctx, 'xiaomiSelectAll(true)');
        ctx.__xiaomiImport = { ok: true, devices: [
            { did: 'did-1', name: '甲', mac: 'AA:BB:CC:DD:EE:01', token: '1111',
              model: 'njcuk', ble_key: 'k1' },
            { did: 'did-9', error: 'timeout' },
        ] };
        await peek(ctx, 'importXiaomiSelected()');
        ok(peek(ctx, 'document.getElementById("xiaomiModal").style.display') !== 'none',
           '有失败 → 不关窗');
        const status = peek(ctx, 'document.getElementById("xiaomiImportStatus").textContent');
        ok(status.indexOf('失败') >= 0 && status.indexOf('主设备') >= 0,
           `状态里要同时有失败信息和"写入主设备"（实际：${status}）`);
        eq(devicesOf(ctx).length, 1, '第一台已经进了主设备（不是新增卡）');
    }

    console.log('\n-- 主设备已配好：导入另一台只会新增，不会覆盖主设备（截图那种同名不同 MAC） --');
    {
        const ctx = await boot({
            ble: { mac: '3C:CD:73:34:1E:E0', token: '****c162', ble_key: '****fecf',
                   scan_timeout: 10, name: 'CUKTECH 10 GaN Charger Ultra' },
            devices: [{ id: 'default', name: 'CUKTECH 10 GaN Charger Ultra',
                        mac: '3C:CD:73:34:1E:E0', connected: false,
                        topic_prefix: 'cuktech/charger', is_primary: true }],
        });
        eq(devicesOf(ctx).length, 1, '载入：只有主设备');

        const second = { ...CLOUD_DEVICE, did: 'did-2', mac: '3C:CD:73:38:F6:55' };
        await importDevices(ctx, [second]);

        const list = devicesOf(ctx);
        eq(list.length, 2, '同名但 MAC 不同 → 追加第二张卡（按 MAC 匹配的规则没变）');
        eq(list[0].mac, '3C:CD:73:34:1E:E0', '主设备身份没被覆盖');
        eq(list[0].name, 'CUKTECH 10 GaN Charger Ultra', '主设备名字没被覆盖');
        eq(list[1].mac, second.mac, '新设备进了第二张卡');
        eq(list[1].is_primary, false, '第二张卡不是 primary');
        peek(ctx, 'toggleDeviceEditor(1)');
        ok(htmlOf(ctx).indexOf('设为主设备') >= 0,
           '非主设备卡的编辑区提供"设为主设备"入口（换机/孤儿主设备的出路）');
        eq(htmlOf(ctx).indexOf('设为') >= 0 && htmlOf(ctx).indexOf('device-card-actions') >= 0,
           true, '动作在展开的编辑区里，不挤占卡片头的设备名');
        peek(ctx, 'toggleDeviceEditor(0)');
        ok(htmlOf(ctx).indexOf('清空主设备') >= 0,
           '主设备卡的编辑区提供"清空主设备"入口（不可逆动作放在编辑区 + 二次确认）');
        peek(ctx, 'toggleDeviceEditor(0)');   // 收起，下面的用例从干净状态开始
    }

    console.log('\n-- 换主设备：id 交换 + 语音开关跟着物理设备走 + payload 由服务端重排 --');
    {
        const ctx = await boot({
            ble: { mac: '3C:CD:73:34:1E:E0', token: '****c162', ble_key: '****fecf',
                   scan_timeout: 10, name: '客厅充电器' },
            devices: [{ id: 'default', name: '客厅充电器', mac: '3C:CD:73:34:1E:E0',
                        connected: false, topic_prefix: 'cuktech/charger', is_primary: true }],
        });
        await importDevices(ctx, [{ ...CLOUD_DEVICE, did: 'did-2', mac: '3C:CD:73:38:F6:55',
                                    name: '书桌充电器' }]);
        const before = devicesOf(ctx);
        eq(before.length, 2, '先有主设备 + 一台附加设备');
        // 默认铺了 primary 的 5 个语音开关（seedBemfaEntities 的兜底）→ 它们属于「客厅充电器」
        eq(peek(ctx, 'JSON.stringify(_bemfaEntities.map(e => e.device))'),
           JSON.stringify(['default', 'default', 'default', 'default', 'default']),
           '前提：5 个语音开关都绑在老主设备上');

        peek(ctx, 'promoteDevice(1)');
        const after = devicesOf(ctx);
        eq(after[0].name, '书桌充电器', '第二张卡换成第一张的位置');
        eq(after[0].id, 'default', '新主设备的 id 变成 default');
        eq(after[0].is_primary, true, '新主设备带 is_primary');
        eq(after[0].mac, '3C:CD:73:38:F6:55', 'MAC 跟着设备走');
        eq(after[1].name, '客厅充电器', '老主设备降级成附加设备');
        ok(after[1].id !== 'default' && /^[a-z0-9][a-z0-9_-]*$/.test(after[1].id),
           `降级设备拿到合法的新 id（实际 ${after[1].id}）`);
        eq(after[1].is_primary, false, '降级设备 is_primary=false');
        const demotedId = after[1].id;
        eq(peek(ctx, 'JSON.stringify(_bemfaEntities.map(e => e.device))'),
           JSON.stringify([demotedId, demotedId, demotedId, demotedId, demotedId]),
           '语音开关跟着"客厅充电器"一起降级（绑的是设备，不是卡槽）');

        await peek(ctx, 'saveConfig()');
        const cfg = lastPost(ctx).body.config;
        eq(cfg.devices.length, 2, '保存的是完整的两台设备');
        eq(cfg.devices[0].id, 'default', '第一项是新的 primary');
        eq(cfg.devices[0].is_primary, true, '第一项 is_primary=true');
        eq(cfg.devices[0].mac, '3C:CD:73:38:F6:55', '新 primary 的 MAC 上交');
        eq(cfg.devices[1].id, demotedId, '第二项是老主设备的新 id');
        eq(cfg.devices[1].is_primary, false, '第二项 is_primary=false');
        eq(cfg.devices[1].mac, '3C:CD:73:34:1E:E0', '老主设备的 MAC 上交（服务端按 MAC 继承明文 token）');
    }

    console.log('\n-- 空主设备 + 手工填好的一台：设为主设备 = 直接填空槽，不留残卡 --');
    {
        const ctx = await boot();                       // 全新安装
        peek(ctx, 'addDevice()');
        peek(ctx, `updateDeviceField(1, 'id', 'desk')`);
        peek(ctx, `updateDeviceField(1, 'name', '书桌充电器')`);
        peek(ctx, `updateDeviceField(1, 'mac', 'AA:BB:CC:DD:EE:02')`);
        peek(ctx, `updateDeviceField(1, 'token', '11111111111111111111111111111111')`);
        eq(devicesOf(ctx).length, 2, '前提：空主设备 + 一张手填的卡');

        peek(ctx, 'promoteDevice(1)');
        const list = devicesOf(ctx);
        eq(list.length, 1, '空槽不降级：直接消失（否则会留下一张缺凭据的残卡）');
        eq(list[0].id, 'default', '手填的那张成为主设备');
        eq(list[0].is_primary, true, '带 is_primary');
        eq(peek(ctx, 'JSON.stringify(_bemfaEntities.every(e => e.device === "default"))'), 'true',
           '语音开关仍然绑在 default（它们属于主设备槽位，不属于那张空卡）');

        const before = ctx.__posts.length;
        await peek(ctx, 'saveConfig()');
        ok(ctx.__posts.length > before, '保存通过（只剩一台主设备）');
        const cfg = lastPost(ctx).body.config;
        eq(cfg.devices.length, 1, '落盘只有一台设备');
        eq(cfg.devices[0].mac, 'AA:BB:CC:DD:EE:02', '填好的那台写进主设备');
    }

    console.log('\n-- 换主设备（页面载入的卡：token 不可见）也能成立 --');
    {
        const ctx = await boot({
            ble: { mac: '3C:CD:73:34:1E:E0', token: '****c162', scan_timeout: 10, name: '客厅充电器' },
            devices: [
                { id: 'default', name: '客厅充电器', mac: '3C:CD:73:34:1E:E0', connected: false,
                  topic_prefix: 'cuktech/charger', is_primary: true },
                { id: 'desk', name: '书桌充电器', mac: 'AA:BB:CC:DD:EE:02', connected: true,
                  topic_prefix: 'cuktech/charger/desk', is_primary: false },
            ],
        });
        eq(peek(ctx, '_devices[1].token'), '', '附加设备的明文 token 页面拿不到');
        eq(peek(ctx, '_devices[1].creds_from_server'), true, '但页面知道它已经配好了');
        peek(ctx, 'promoteDevice(1)');
        eq(peek(ctx, '_devices[0].id'), 'default', '载入的附加设备也能换上来');

        const before = ctx.__posts.length;
        await peek(ctx, 'saveConfig()');
        ok(ctx.__posts.length > before,
           '新 primary 的 token 在页面上是空的，但不再被本地校验拦下（服务端按 MAC 继承）');
        const cfg = lastPost(ctx).body.config;
        eq(cfg.devices[0].mac, 'AA:BB:CC:DD:EE:02', '新主设备的 MAC 上交');
        eq(cfg.devices[0].token, '', 'token 留空 = 让服务端沿用旧值');
        eq(cfg.devices[0].is_primary, true, '仍然带 is_primary');
    }

    console.log('\n-- 换完主设备后，降级下来的那张卡可以删掉了 --');
    {
        const ctx = await boot({
            ble: { mac: '3C:CD:73:34:1E:E0', token: '****c162', scan_timeout: 10, name: '客厅充电器' },
            devices: [{ id: 'default', name: '客厅充电器', mac: '3C:CD:73:34:1E:E0',
                        connected: false, topic_prefix: 'cuktech/charger', is_primary: true }],
        });
        await importDevices(ctx, [{ ...CLOUD_DEVICE, did: 'did-2', mac: '3C:CD:73:38:F6:55',
                                    name: '书桌充电器' }]);
        peek(ctx, 'promoteDevice(1)');
        peek(ctx, 'removeDevice(1)');
        const list = devicesOf(ctx);
        eq(list.length, 1, '旧主设备（现在只是普通卡）删得掉');
        eq(list[0].name, '书桌充电器', '留下的正是真正在用的那台');
        ok(htmlOf(ctx).indexOf('删除') < 0, '新主设备仍然没有删除按钮（primary 的结构约束不变）');
    }

    console.log('\n-- 清空主设备：孤儿名字/凭据能清掉，保存时带 clear_credentials --');
    {
        const ctx = await boot({
            ble: { mac: '3C:CD:73:34:1E:E0', token: '****c162', ble_key: '****fecf',
                   scan_timeout: 10, name: 'CUKTECH 10 GaN Charger Ultra' },
            devices: [{ id: 'default', name: 'CUKTECH 10 GaN Charger Ultra',
                        mac: '3C:CD:73:34:1E:E0', connected: false,
                        topic_prefix: 'cuktech/charger', is_primary: true }],
        });
        peek(ctx, 'clearPrimary()');
        const p = devicesOf(ctx)[0];
        eq([p.name, p.mac, p.token, p.ble_key], ['', '', '', ''], '名称/MAC/Token/BLE Key 全清空');
        eq(p.clear_credentials, true, '标记"这是清空，不是没填"');
        eq(peek(ctx, 'document.getElementById("deviceOnboardingHint").hidden'), false,
           '清空后重新回到"待配置"引导态');

        await peek(ctx, 'saveConfig()');
        const cfg = lastPost(ctx).body.config;
        eq(cfg.devices.length, 1, '清空后仍然是"一台主设备"（结构上的 primary 不能消失）');
        eq(cfg.devices[0].clear_credentials, true, 'payload 带上 clear_credentials');
        eq([cfg.devices[0].name, cfg.devices[0].mac, cfg.devices[0].token],
           ['', '', ''], '身份字段以空值上交（服务端据此真的抹掉）');
        ok(!('name' in cfg.ble), 'ble 节不写空串（老服务端不会误解成"没改"）');

        // 清空之后再导入，会重新写回主设备（不需要刷新页面）
        await importDevices(ctx, [CLOUD_DEVICE]);
        const again = devicesOf(ctx);
        eq(again.length, 1, '清空后重新导入：还是只有一张卡');
        eq(again[0].mac, CLOUD_DEVICE.mac, '设备重新写进主设备');
        eq(again[0].clear_credentials, false, '导入成功后撤掉 clear_credentials');
    }

    console.log('\n-- 主设备没配好却有附加设备：本地先拦下（并给出两条出路） --');
    {
        const ctx = await boot({
            ble: { mac: '', token: '', scan_timeout: 10, name: '' },
            devices: [{ id: 'default', name: 'default', mac: '', connected: false,
                        topic_prefix: 'cuktech/charger', is_primary: true },
                      { id: 'desk', name: '书桌充电器', mac: 'AA:BB:CC:DD:EE:02',
                        token: '11111111111111111111111111111111',
                        connected: false, topic_prefix: 'cuktech/charger/desk', is_primary: false }],
        });
        // 附加设备的 token 页面拿不到（/api/devices 不返回），用户重填才能提交；
        // 这里模拟"第二台已经填好"，好让校验走到 primary 那一条。
        peek(ctx, `updateDeviceField(1, 'token', '11111111111111111111111111111111')`);
        const before = ctx.__posts.length;
        await peek(ctx, 'saveConfig()');
        eq(ctx.__posts.length, before, '不提交（服务端也会用同一条规则拒收）');
        const toast = peek(ctx, '(document.getElementById("toast") || {}).textContent') || '';
        ok(toast.indexOf('主设备') >= 0 && toast.indexOf('设为主设备') >= 0,
           `报错里直接给出"设为主设备"的出路（实际：${toast}）`);
    }

    console.log('\n-- 只有 MAC 没有 Token 的新卡：不能设为主设备（否则保存成功、重启认证失败） --');
    {
        const ctx = await boot();                       // 全新安装
        peek(ctx, 'addDevice()');
        peek(ctx, `updateDeviceField(1, 'id', 'desk')`);
        peek(ctx, `updateDeviceField(1, 'name', '书桌充电器')`);
        peek(ctx, `updateDeviceField(1, 'mac', 'AA:BB:CC:DD:EE:02')`);   // 故意不填 token
        peek(ctx, 'promoteDevice(1)');
        eq(devicesOf(ctx).length, 2, '卡片还在（让用户去补 Token）');
        eq(devicesOf(ctx)[0].is_primary, true, '主槽位没有被换掉');
        const toast = peek(ctx, '(document.getElementById("toast") || {}).textContent') || '';
        ok(toast.indexOf('Token') >= 0, `提示要说明缺的是 Token（实际：${toast}）`);
    }

    console.log('\n-- 主设备只有名字/只有 MAC（半成品）：换主设备时不降级成残卡 --');
    {
        const ctx = await boot();                       // 全新安装
        peek(ctx, `updateDeviceField(0, 'name', '旧名字')`);   // 只有名字，没有 MAC/Token
        peek(ctx, 'addDevice()');
        peek(ctx, `updateDeviceField(1, 'id', 'desk')`);
        peek(ctx, `updateDeviceField(1, 'mac', 'AA:BB:CC:DD:EE:02')`);
        peek(ctx, `updateDeviceField(1, 'token', '11111111111111111111111111111111')`);
        peek(ctx, 'promoteDevice(1)');

        const list = devicesOf(ctx);
        eq(list.length, 1, '半成品主设备直接消失，不降级成缺凭据的附加卡');
        eq(list[0].id, 'default', '真实设备成为主设备');
        const before = ctx.__posts.length;
        await peek(ctx, 'saveConfig()');
        ok(ctx.__posts.length > before, '保存能通过（不会被降级卡上的校验拦住）');
    }

    console.log(failed === 0
        ? `\nAll ${passed} primary-lifecycle checks passed.`
        : `\n${failed}/${passed + failed} primary-lifecycle checks FAILED.`);
    process.exit(failed === 0 ? 0 : 1);
})();
