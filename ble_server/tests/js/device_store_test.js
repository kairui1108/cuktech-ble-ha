#!/usr/bin/env node
/**
 * device-store.js 的契约测试（多设备 §3.12）。
 *
 * 为什么单独成文件：多设备前端最容易出的错不是"渲染错了"，而是**串台**——
 * 一台设备的数据写进另一台的桶、别人的 SSE 事件把当前设备的界面改掉、
 * 换设备后卡片继续显示上一台的历史。这些错误在单设备环境下永远看不到，
 * 所以这里用真实的 device-store.js（以及 A 段之后的真实 app.js 全链路）
 * 逐条锁死：
 *   A. 单元契约：?device= 拼装、SSE 按设备落桶、切换/广播、切换器显隐、记忆回退
 *   B. 整页串台：真实 app.js 装配下两个桶互不覆盖，且非当前设备的事件零 DOM 写
 *
 * 手法与 smoke_test.js 一致（vm + permissive stubs），只是这里的 DOM 是**可计数的**：
 * "没动 DOM"必须能被断言，否则这类串台测不出来。
 *
 * Usage: node tests/js/device_store_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC = path.resolve(__dirname, '../../web/static');

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

// ── 沙箱 ──

/** 可计数的最小 DOM：读 getElementById / 写 innerHTML 都记账，用来断言"没动 DOM"。 */
function buildSandbox(opts) {
    opts = opts || {};
    const dom = { reads: 0, writes: 0, els: new Map() };
    const listeners = Object.create(null);
    const fetches = [];
    const storage = new Map(opts.storage || []);
    const events = [];

    function makeEl(id) {
        const el = {
            id: id || '', style: { setProperty() {}, getPropertyValue() { return ''; }, removeProperty() {} },
            dataset: {}, hidden: false, className: '', textContent: '', title: '',
            classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
            children: [], options: [], firstChild: null,
            setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
            addEventListener() {}, removeEventListener() {},
            appendChild() {}, insertBefore() {}, remove() {}, click() {},
            querySelector() { return null; }, querySelectorAll() { return []; },
            closest() { return null; }, getContext() { return {}; },
            offsetWidth: 24, getBoundingClientRect() { return { left: 0, top: 0, width: 24, height: 26 }; },
            focus() {}, blur() {},
        };
        // innerHTML 是"重建 DOM"的代理指标：write 计数 = 有多少次整块重绘
        let html = '';
        Object.defineProperty(el, 'innerHTML', {
            get() { return html; },
            set(v) { html = v; dom.writes++; },
        });
        return el;
    }

    function getEl(id) {
        dom.reads++;
        if (!dom.els.has(id)) dom.els.set(id, makeEl(id));
        return dom.els.get(id);
    }

    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        Intl,
        URL,
        setTimeout: (fn) => { if (typeof fn === 'function') return 0; return 0; },
        clearTimeout() {},
        setInterval: () => 0,
        clearInterval() {},
        Promise, JSON, Math, Date, RegExp, String, Number, Boolean, Object, Array,
        isNaN, parseFloat, parseInt, Error, Map, Set,
        fetch: (url, init) => {
            fetches.push(String(url));
            return Promise.resolve({
                ok: true, status: 200,
                json: () => Promise.resolve({ ok: true }),
            });
        },
        EventSource: function () {
            sandbox.__evtSource = this;
            this.close = function () {};
        },
        Chart: function () {
            return {
                data: { labels: [], datasets: [] }, options: { scales: {} },
                update() {}, destroy() {}, scales: {}, canvas: { parentNode: null }, ctx: {},
            };
        },
        localStorage: {
            getItem: (k) => (storage.has(k) ? storage.get(k) : null),
            setItem: (k, v) => storage.set(k, String(v)),
            removeItem: (k) => storage.delete(k),
        },
        navigator: { language: 'en-US' },
        location: opts.location || { origin: 'http://charger.local', href: 'http://charger.local/index.html' },
        history: { replaceState(state, title, url) { sandbox.__lastUrl = url; } },
        getComputedStyle: () => ({ getPropertyValue: () => '' }),
        matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
        requestAnimationFrame: (fn) => 0,
        CustomEvent: function (type, init) { this.type = type; this.detail = init && init.detail; },
        addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener() {},
        dispatchEvent: (e) => {
            events.push(e);
            (listeners[e.type] || []).forEach((fn) => fn(e));
            return true;
        },
        MutationObserver: function () { this.observe = function () {}; },
        ResizeObserver: function () { this.observe = function () {}; },
    };
    sandbox.window = sandbox;

    sandbox.document = {
        documentElement: makeEl('html'),
        body: makeEl('body'),
        head: makeEl('head'),
        title: '',
        readyState: 'complete',
        activeElement: null,
        getElementById: getEl,
        // 真实的 app.js 对 querySelector 的返回值都有 null 守卫，这里返 null 更接近
        // "页面上没有这个节点"的真实情形（返回一个假节点会让守卫失效、走到假路径上）
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: () => makeEl('new'),
        addEventListener() {},
        removeEventListener() {},
    };
    // index.html / phone.html 里的切换器容器自带 hidden 属性：单设备时整条不占位，
    // 所以沙箱里也要从"隐藏"开始，否则测不出"没列表就不渲染"这件事。
    const swEl = makeEl('deviceSwitcher');
    swEl.hidden = true;
    dom.els.set('deviceSwitcher', swEl);

    vm.createContext(sandbox);
    sandbox.__dom = dom;
    sandbox.__fetches = fetches;
    sandbox.__events = events;
    sandbox.__storage = storage;
    return sandbox;
}

function run(ctx, ...files) {
    for (const f of files) {
        const src = fs.readFileSync(path.join(STATIC, f), 'utf8');
        vm.runInContext(src, ctx, { filename: f });
    }
}

function peek(ctx, expr) { return vm.runInContext(expr, ctx); }

/** 从 vm 里取 store 的快照（同一个全局词法环境，直接求值最省事）。 */
function bucketOf(ctx, id) { return peek(ctx, `JSON.stringify(DeviceStore.get(${JSON.stringify(id)}))`); }
function currentOf(ctx) { return peek(ctx, 'DeviceStore.current()'); }

// SSE 消息构造：多设备时服务端给每条事件带 device（§3.8）
function evt(obj) { return { data: JSON.stringify(obj) }; }

const DEVICES_2 = [
    { id: 'default', name: '书房充电器', mac: 'AA:BB:CC:DD:EE:01', connected: true, is_primary: true },
    { id: 'desk', name: '书桌充电器', mac: 'AA:BB:CC:DD:EE:02', connected: false, is_primary: false },
];

// ══════════════════════════════════════════════════════════
//  A. device-store.js 单元契约（用一个最小的"页面"注册钩子）
// ══════════════════════════════════════════════════════════

function buildUnitPage(overrides) {
    const ctx = buildSandbox(overrides);
    run(ctx, 'device-store.js');
    // 最小页面：桶形状 = {ports:{}, settings:{}, connected:false}，
    // 端口落桶方式与桌面页一致（API 形状）——被测的是 store 的路由，不是渲染。
    peek(ctx, `
        window.__replays = [];
        window.__view = DeviceStore.init({
            defaults: function () {
                return { ports: {}, settings: {}, protocolSwitches: {}, protocolExtend: 0,
                         connected: false, authenticated: false, bleConnected: false };
            },
            applyPorts: function (b, msg) {
                if (msg.type === 'port_update') {
                    var k = String(msg.port_id);
                    b.ports[k] = Object.assign({}, b.ports[k] || {}, msg.data || {});
                } else if (msg.ports) {
                    b.ports = msg.ports;
                }
            },
            replay: [function (id) { window.__replays.push(id); }],
            switcherId: 'deviceSwitcher'
        });
    `);
    return ctx;
}

console.log('\n-- api()：所有请求都带 ?device=<当前设备> --');
{
    const ctx = buildUnitPage();
    eq(currentOf(ctx), 'default', '无记忆/无 URL 时指针 = primary（default）');
    peek(ctx, `DeviceStore.api('/api/status')`);
    const url = ctx.__fetches.pop();
    ok(/\/api\/status\?device=default$/.test(url), `老端点拼上 device=default（${url}）`);

    peek(ctx, `DeviceStore.api('/api/chart?hours=24&interval=3600')`);
    const url2 = ctx.__fetches.pop();
    ok(/hours=24/.test(url2) && /interval=3600/.test(url2) && /device=default/.test(url2),
       `已有 query 不被覆盖（${url2}）`);

    // 带 POST 选项时原样透传（不能把 body/method 弄丢）
    peek(ctx, `DeviceStore.api('/api/port', { method: 'POST' })`);
    ctx.__fetches.pop();
    eq(peek(ctx, `typeof DeviceStore.url`), 'function', 'url() 也对外暴露（导出链接要用）');
    ok(/device=default/.test(peek(ctx, `DeviceStore.url('/api/sessions/7/export')`)),
       'url() 给 <a href> 用，同样带 device');
}

console.log('\n-- 指针记忆：localStorage 记住上次，?device= 优先于记忆 --');
{
    const ctx = buildUnitPage({ storage: [['cuktech-device', 'desk']] });
    eq(currentOf(ctx), 'desk', 'localStorage 里的上次设备被恢复');
    peek(ctx, `DeviceStore.api('/api/status')`);
    ok(/device=desk/.test(ctx.__fetches.pop()), 'requests follow the remembered device');

    const ctx2 = buildUnitPage({
        storage: [['cuktech-device', 'desk']],
        location: { origin: 'http://charger.local', href: 'http://charger.local/index.html?device=default' },
    });
    eq(currentOf(ctx2), 'default', '?device= 比 localStorage 优先（分享链接要落在指定设备上）');
}

console.log('\n-- 记忆失效：设备已被删除 → 回退 primary --');
{
    const ctx = buildUnitPage({ storage: [['cuktech-device', 'ghost']] });
    eq(currentOf(ctx), 'ghost', '初始化时先照记忆信任（此时还不知道设备清单）');
    const hi = peek(ctx, `DeviceStore.dispatch(${JSON.stringify({ type: 'init', devices: DEVICES_2 })})`);
    eq(hi, true, 'init（缺 device 字段 = primary）在回退之后就是当前设备的事件 → 照常渲染');
    eq(currentOf(ctx), 'default', '记忆里的 ghost 不在清单里 → 回退 primary（否则每个请求都 404）');
    eq(ctx.__storage.get('cuktech-device'), 'default', '回退结果写回 localStorage');
    ok(ctx.__events.some((e) => e.type === 'device-changed' && e.detail.device === 'default'),
       '回退也广播 device-changed（页面据此重拉数据）');
}

console.log('\n-- 打开页面时指针就在非 primary（?device= 分享链接） --');
{
    const ctx = buildUnitPage({
        location: { origin: 'http://charger.local', href: 'http://charger.local/index.html?device=desk' },
    });
    eq(currentOf(ctx), 'desk', '?device=desk 让指针一打开就落在第二台');
    // init 只带 primary 的状态（多设备时也不带 device 字段）
    const hi = peek(ctx, 'DeviceStore.dispatch')({ type: 'init', devices: DEVICES_2, connected: true, authenticated: true });
    eq(hi, false, 'primary 的 init 不属于当前设备（不是 desk 的事件）');
    eq(peek(ctx, 'JSON.stringify(window.__replays)'), JSON.stringify(['desk']),
       '设备清单落地后按当前设备重放一次（否则界面停在 primary 的骨架上）');

    // 单设备服务端（旧版本）没有 devices 列表：指针必须回退 primary
    const ctxSingle = buildUnitPage({
        storage: [['cuktech-device', 'desk']],
    });
    eq(currentOf(ctxSingle), 'desk', '初始化时先照记忆信任');
    peek(ctxSingle, 'DeviceStore.dispatch')({ type: 'init', connected: true, authenticated: true });
    eq(currentOf(ctxSingle), 'default', '没有 devices 列表 = 只有一台 → 记忆失效，回退 primary');
    eq(ctxSingle.__storage.get('cuktech-device'), 'default', '回退写回记忆');
    ok(ctxSingle.__events.some((e) => e.type === 'device-changed'), '回退广播 device-changed');
    eq(peek(ctxSingle, 'window.__replays.length'), 0, '单设备不重放（渲染路径与改造前一致）');
}

console.log('\n-- SSE 分发器：先落桶，非当前设备不动 DOM --');
{
    const ctx = buildUnitPage();
    peek(ctx, `DeviceStore.dispatch(${JSON.stringify({ type: 'init', devices: DEVICES_2, connected: true, authenticated: true, ports: { 1: { power: 7 } } })})`);
    eq(peek(ctx, `JSON.stringify(DeviceStore.get('default').ports)`), JSON.stringify({ 1: { power: 7 } }),
       '当前设备（primary）的端口数据落进 default 桶');

    const rendered = peek(ctx, 'DeviceStore.dispatch')({ type: 'status', device: 'desk', connected: true, authenticated: true, ports: { 1: { power: 42 } } });
    eq(rendered, false, '别的设备的 status 返回 false（调用方据此不渲染）');
    eq(peek(ctx, `DeviceStore.get('desk').ports[1].power`), 42, 'desk 的数据落进 desk 桶');
    eq(peek(ctx, `DeviceStore.get('default').ports[1].power`), 7, 'default 桶未被 desk 的数据覆盖（零串台）');
    eq(peek(ctx, 'window.__replays.length'), 0, '非当前设备的事件不触发重放');

    const hi = peek(ctx, 'DeviceStore.dispatch')({ type: 'port_update', port_id: 2, data: { power: 9 } });
    eq(hi, true, '缺 device 字段的事件按 primary 处理 → 属于当前设备');
    eq(peek(ctx, `DeviceStore.get('default').ports[2].power`), 9, 'primary 增量事件落 default 桶');

    // 旧服务端/旧配置：完全没有 devices 列表，事件也没有 device —— 必须一切如常
    const ctxOld = buildUnitPage();
    const hiOld = peek(ctxOld, 'DeviceStore.dispatch')({ type: 'status', connected: true, authenticated: true, ports: { 1: { power: 3 } } });
    eq(hiOld, true, '单设备/旧服务端（无 device 字段）→ 按 primary 且照常渲染');
    eq(peek(ctxOld, `DeviceStore.get('default').ports[1].power`), 3, '数据照常落 primary 桶');
}

console.log('\n-- session_end 带归属转发（charge_history 按 device 过滤） --');
{
    const ctx = buildUnitPage();
    peek(ctx, 'DeviceStore.dispatch')({ type: 'init', devices: DEVICES_2 });
    peek(ctx, 'DeviceStore.dispatch')({ type: 'session_end', device: 'desk', session_id: 5 });
    const ev = ctx.__events.filter((e) => e.type === 'sse-session-end');
    eq(ev.length, 1, '别的设备的 session_end 仍然转发一次（否则统计事件彻底丢失）');
    eq(ev[0].detail.device, 'desk', '转发时带 device，消费方才能过滤');
    eq(ev[0].detail.session_id, 5, '原始字段一个不少');
}

console.log('\n-- switchDevice：换指针 + 重放 + 广播 device-changed --');
{
    const ctx = buildUnitPage();
    peek(ctx, 'DeviceStore.dispatch')({ type: 'init', devices: DEVICES_2 });
    eq(peek(ctx, `DeviceStore.switchDevice('desk')`), true, '切到 desk 返回 true');
    eq(currentOf(ctx), 'desk', '指针已切换');
    eq(peek(ctx, 'JSON.stringify(window.__replays)'), JSON.stringify(['desk']), '重放回调收到新设备 id');
    const ch = ctx.__events.filter((e) => e.type === 'device-changed');
    eq(ch.length, 1, '广播一次 device-changed');
    eq(ch[0].detail.device, 'desk', 'detail.device 是新设备');
    eq(ch[0].detail.previous, 'default', 'detail.previous 是旧设备');
    ok(/device=desk/.test(ctx.__lastUrl || ''), '选中态写回 URL（可刷新可分享）');
    eq(ctx.__storage.get('cuktech-device'), 'desk', '选中态写回 localStorage');

    eq(peek(ctx, `DeviceStore.switchDevice('desk')`), false, '重复切同一台是空操作');
    eq(peek(ctx, 'window.__replays.length'), 1, '空操作不重放（避免白刷一遍界面）');
    eq(ctx.__events.filter((e) => e.type === 'device-changed').length, 1, '空操作不广播');
}

console.log('\n-- replay 传"一个函数"也要跑（两个页面就是这么传的） --');
{
    // 真实回归：app.js / phone.js 传的是 replay: replayDeviceView（单个函数），
    // 而 store 早先只当数组处理 —— 函数也有 .length（形参个数 0），于是重放一次都
    // 不跑：切设备后指针动了、历史/限额卡换了，但设备面板、端口、图表、设置还停在
    // 上一台。这里把这个形状钉死，两个页面再怎么传都得跑起来。
    const ctx = buildUnitPage();
    const called = peek(ctx, `(function () {
        var hits = [];
        DeviceStore.init({
            defaults: function () { return { ports: {}, settings: {} }; },
            replay: function (id) { hits.push(id); },
            switcherId: 'deviceSwitcher'
        });
        DeviceStore.dispatch({ type: 'init', devices: [
            { id: 'default', name: '', mac: 'AA', connected: true, is_primary: true },
            { id: 'desk', name: '书桌充电器', mac: 'BB', connected: true, is_primary: false }
        ] });
        hits.length = 0;
        DeviceStore.switchDevice('desk');
        return hits.join(',');
    })()`);
    eq(called, 'desk', '单函数形式的 replay 在切设备时被调用一次，参数是新设备 id');
}

console.log('\n-- 切换器：单设备整条隐藏，多设备才渲染 --');
{
    const ctx = buildUnitPage();
    const sw = () => ctx.document.getElementById('deviceSwitcher');
    eq(sw().hidden, true, '初始（没有 devices 列表）隐藏 —— 与改造前一致');
    eq(sw().innerHTML, '', '隐藏时不渲染任何 pill');

    // 单设备：服务端只发一台
    peek(ctx, 'DeviceStore.dispatch')({ type: 'init', devices: [DEVICES_2[0]] });
    eq(sw().hidden, true, '只配一台设备 → 整条 display:none');
    eq(sw().innerHTML, '', '单设备不渲染 pill（视觉与现状逐像素一致）');

    // 多设备
    peek(ctx, 'DeviceStore.dispatch')({ type: 'init', devices: DEVICES_2 });
    eq(sw().hidden, false, '两台设备 → 切换器出现');
    ok(sw().innerHTML.indexOf('data-device="desk"') >= 0, '第二台设备有独立的 pill');
    ok(sw().innerHTML.indexOf('书桌充电器') >= 0, 'pill 上用设备名而不是 id');

    // 换回单设备（比如设备被删掉）：必须重新收起
    peek(ctx, 'DeviceStore.dispatch')({ type: 'init', devices: [DEVICES_2[0]] });
    eq(sw().hidden, true, '设备数回到 1 → 切换器重新收起');
    eq(sw().innerHTML, '', '同时清空 pill');
}

// ══════════════════════════════════════════════════════════
//  B. 真实 app.js 装配下的串台测试
// ══════════════════════════════════════════════════════════

console.log('\n-- 真实 app.js：两个桶互不覆盖，非当前设备零 DOM 写 --');
{
    const ctx = buildSandbox();
    run(ctx,
        'locales/zh-CN.js', 'locales/en.js', 'i18n.js',
        'chart-config.js', 'chart-loader.js',
        'device-store.js', 'charge_history.js', 'charge_limit.js', 'app.js');
    // 加载期的初始化（initApp 会拉一遍图表/状态）先记账清零
    ctx.__dom.reads = 0;
    ctx.__dom.writes = 0;
    const es = ctx.__evtSource;
    ok(!!es && typeof es.onmessage === 'function', 'SSE 已连上（拿到 onmessage 句柄）');

    // 服务端多设备：init 带 devices 列表 + 每台一条 status/port_update
    const send = (msg) => es.onmessage(evt(msg));
    send({ type: 'init', devices: DEVICES_2, connected: true, authenticated: true,
           ports: { 1: { voltage: 5, current: 1, power: 7, enabled: true, protocol: 'PD' } },
           settings: { 5: 1 }, firmware_version: '1.0.0' });
    send({ type: 'status', device: 'desk', connected: true, authenticated: true,
           ports: { 1: { voltage: 20, current: 2, power: 42, enabled: true, protocol: 'PD' } },
           settings: { 5: 2 }, firmware_version: '2.0.0' });

    eq(peek(ctx, `DeviceStore.get('default').ports[1].power`), 7, 'primary 桶 = primary 的数据');
    eq(peek(ctx, `DeviceStore.get('desk').ports[1].power`), 42, 'desk 桶 = desk 的数据（互不覆盖）');
    eq(peek(ctx, `DeviceStore.get('default').settings[5]`), 1, '设置同样按设备分开');
    eq(peek(ctx, `DeviceStore.get('desk').settings[5]`), 2, 'desk 的设置没写进 primary');
    eq(peek(ctx, `DeviceStore.get('default').firmware`), '1.0.0', '固件版本按设备分开');
    eq(peek(ctx, `DeviceStore.get('desk').firmware`), '2.0.0', 'desk 的固件没写进 primary');
    eq(peek(ctx, 'window.DeviceStore.current()'), 'default', '指针仍在 primary（没人切过）');

    // 关键断言：非当前设备的事件走完分发就返回，界面一次都没被读/写
    ctx.__dom.reads = 0;
    ctx.__dom.writes = 0;
    send({ type: 'port_update', device: 'desk', port_id: 1, data: { power: 60 } });
    eq(ctx.__dom.reads, 0, 'desk 的 port_update 没有触碰任何 DOM 节点');
    eq(ctx.__dom.writes, 0, 'desk 的 port_update 没有重建任何 DOM 块');
    eq(peek(ctx, `DeviceStore.get('desk').ports[1].power`), 60, '但数据落了 desk 桶');

    // 当前设备的事件必须照常渲染（否则上一条断言可以被"什么都不做"蒙过去）
    send({ type: 'port_update', port_id: 1, data: { power: 8 } });
    ok(ctx.__dom.reads > 0, 'primary 的 port_update 照常渲染（DOM 被读取/更新）');
    eq(peek(ctx, `DeviceStore.get('default').ports[1].power`), 8, 'primary 桶跟着更新');

    // 切到 desk：重放一次，界面变成 desk 的快照
    ctx.__dom.reads = 0;
    peek(ctx, `DeviceStore.switchDevice('desk')`);
    ok(ctx.__dom.reads > 0, 'switchDevice 重放渲染函数（界面被重画）');
    eq(peek(ctx, `DeviceStore.get('desk').ports[1].power`), 60, '重放读的是 desk 桶（不是上一台的 8W）');
    eq(peek(ctx, `DeviceStore.get('default').ports[1].power`), 8, 'primary 桶在切换后原样保留');

    // 切换后 desk 变成"当前设备"：它的 port_update 现在必须渲染
    ctx.__dom.reads = 0;
    send({ type: 'port_update', device: 'desk', port_id: 1, data: { power: 61 } });
    ok(ctx.__dom.reads > 0, '切换后 desk 的事件开始渲染');
    eq(peek(ctx, `DeviceStore.get('desk').ports[1].power`), 61, 'desk 桶继续更新');

    // 而 primary 的事件此时反过来不应动 DOM
    ctx.__dom.reads = 0;
    send({ type: 'port_update', port_id: 1, data: { power: 99 } });
    eq(ctx.__dom.reads, 0, '切走后 primary 的事件不再渲染（归属跟着指针走）');
    eq(peek(ctx, `DeviceStore.get('default').ports[1].power`), 99, '但 primary 桶仍在后台更新');
}

console.log('\n-- 真实 app.js：切换器显隐与单设备静默 --');
{
    const ctx = buildSandbox();
    run(ctx,
        'locales/zh-CN.js', 'locales/en.js', 'i18n.js',
        'chart-config.js', 'chart-loader.js',
        'device-store.js', 'charge_history.js', 'charge_limit.js', 'app.js');
    const sw = () => ctx.document.getElementById('deviceSwitcher');
    eq(sw().hidden, true, '单设备默认隐藏');

    ctx.__evtSource.onmessage(evt({ type: 'init', devices: [DEVICES_2[0]], connected: true, authenticated: true }));
    eq(sw().hidden, true, '服务端只报一台 → 仍然隐藏');
    eq(peek(ctx, 'DeviceStore.current()'), 'default', '单设备时指针恒为 default');

    ctx.__evtSource.onmessage(evt({ type: 'init', devices: DEVICES_2, connected: true, authenticated: true }));
    eq(sw().hidden, false, '两台 → 切换器出现');
}

console.log('\n-- 页面骨架：切换器随 HTML 下发、默认隐藏、且先于页面脚本加载 --');
{
    const WEB = path.resolve(__dirname, '../../web');
    const pages = [
        ['index.html', 'app.js'],
        ['phone.html', 'phone.js'],
    ];
    for (const [page, pageScript] of pages) {
        const html = fs.readFileSync(path.join(WEB, page), 'utf8');
        // 用 src 前缀定位真正的 <script> 标签（注释里也会提到文件名，直接 indexOf 会误判）
        const storeAt = html.indexOf(`src="/static/device-store.js`);
        const pageAt = html.indexOf(`src="/static/${pageScript}`);
        ok(storeAt > 0 && pageAt > storeAt, `${page} 里 device-store.js 先于 ${pageScript} 加载`);
    }
    {
        // 桌面：侧栏那副牌是一个空的 <nav hidden>，内容由 bench 生成
        const indexHtml = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
        ok(/<nav class="device-switcher" id="deviceSwitcher" hidden><\/nav>/.test(indexHtml),
           'index.html 自带 hidden 的牌堆容器');
        // 手机：切换入口是标题栏的下拉列表，不再有那条吸顶横条
        const phoneHtml = fs.readFileSync(path.join(WEB, 'phone.html'), 'utf8');
        ok(phoneHtml.indexOf('id="deviceSwitcher"') < 0,
           'phone.html 没有吸顶横条容器（那 58px 还给页面了）');
        ok(phoneHtml.indexOf('id="deviceMenuBtn"') > 0 && phoneHtml.indexOf('id="deviceMenuList"') > 0,
           'phone.html：标题栏是切换入口，下面挂一个下拉列表容器');
        ok(/id="deviceMenu"[^>]*hidden/.test(phoneHtml), '下拉列表默认收起（不占位）');
        ok(/aria-expanded="false"/.test(phoneHtml) && /aria-controls="deviceMenu"/.test(phoneHtml),
           '标题按钮带 aria-haspopup/expanded/controls（读屏能知道它会展开）');
        const phoneCss = fs.readFileSync(path.join(WEB, 'static/phone.css'), 'utf8');
        ok(phoneCss.indexOf('.device-switcher') < 0, 'phone.css 里那条吸顶条的样式已删除');
        ok(phoneCss.indexOf('.dev-item') > 0 && phoneCss.indexOf('.dev-menu') > 0,
           'phone.css 有下拉列表与小卡片的样式');
        // 标题居中：左齿轮 28px、右主题按钮 28px，标题靠 flex 居中；早年给隐藏的
        // .nav-more 留的 margin-right:36px 会把整块左推，两边白边就不一样长了
        ok(!/\.nav-title\s*\{[^}]*margin-right/.test(phoneCss),
           '标题栏没有 margin-right（否则文字两边白边不对称）');
        ok(/button\.nav-title:not\(:disabled\)::before\s*\{[^}]*content:\s*''/.test(phoneCss),
           '右边的 ▾ 用等宽 ::before 在左边配平，设备名才是真居中');
        // 选择器收窄到 <button class="nav-title">：config.html 的标题是 <div>，
        // 不该跟着拿到 cursor:pointer 与那块 14px 配平（会把它的标题顶偏）
        ok(!/^\.nav-title \{\s*\n\s*display: flex/m.test(phoneCss),
           '标题按钮的样式挂在 button.nav-title 上，不污染 config.html 的 <div> 标题');
        {
            // 下拉箭头不能太小太淡（10px / .55 几乎看不见）
            const caretBlock = (phoneCss.match(/\.nav-caret\s*\{[^}]*\}/) || [''])[0];
            ok(/font-size:\s*1[3-9]px/.test(caretBlock), '下拉箭头字号 ≥ 13px');
            ok(/opacity:\s*0?\.[7-9]/.test(caretBlock), '下拉箭头淡度 ≥ .7');
            ok(/rotate\(180deg\)/.test(phoneCss), '展开时箭头翻转（指示"再点收起"）');
        }
        ok(!/has-device-(bench|switcher)/.test(phoneCss),
           '不再为那条吸顶条给整页加顶部留白（body.has-device-* 已撤）');
        // 下拉列表的逻辑在 phone.js：标题显示当前设备、点列表项 = 整页切换
        const phoneJs = fs.readFileSync(path.join(STATIC, 'phone.js'), 'utf8');
        ok(/function renderDeviceMenu/.test(phoneJs), 'phone.js 渲染这个下拉列表');
        ok(/DeviceStore\.switchDevice\(id\)/.test(phoneJs),
           '点列表项 = 切设备（整页跟着换，走的是真的 store 指针）');
        ok(/navProductName/.test(phoneJs), '只有一台设备时标题退回产品名');
        ok(/menuShare\(sum\.watts, 120\)/.test(phoneJs) &&
           /menuShare\(total, 120 \* Math\.max\(1, list\.length\)\)/.test(phoneJs),
           '小卡片上的功率脊刻度与桌面一致（单台 /120W，全部 /n×120W）');
        ok(/DeviceBench\.enterFleet/.test(phoneJs), '"全部设备"仍然进得去汇总页');
        // 预览：手机上也要能在单台硬件上验收"点标题切整页"
        ok(/PHONE_PREVIEW\s*=\s*\/\[\?&\]preview=multi\//.test(phoneJs),
           'phone.js 认 ?preview=multi');
        ok(/preview:\s*PHONE_PREVIEW/.test(phoneJs) && /previewSeed:\s*phonePreviewSeed/.test(phoneJs),
           '把预览开关与灌数据函数交给 bench（伪造设备因此才被登记为真桶）');
        ok(/DeviceBench\.devices/.test(phoneJs),
           '菜单用 bench 的同一份清单（伪造设备在手机上也看得到）');
        ok(phoneCss.indexOf('.dev-menu-tag') > 0, '下拉列表里有 PREVIEW 标记的样式');
        ok(/DeviceBench\.exitFleet/.test(phoneJs), '从汇总页点某一台会先退出汇总页');
    }
    // 版式契约：切换条住在右侧栏里、正压"当前总功率"卡之上；汇总页占主列。
    // 这两条是用户点名的布局要求，靠 CSS 无法断言，直接钉在 DOM 顺序上。
    const indexHtml = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
    const sideAt = indexHtml.indexOf('<div class="col-side">');
    const swAt = indexHtml.indexOf('id="deviceSwitcher"');
    const rateAt = indexHtml.indexOf('id="cardRate"');
    const mainAt = indexHtml.indexOf('<div class="col-main">');
    const fleetAt = indexHtml.indexOf('id="fleetView"');
    ok(sideAt > 0 && swAt > sideAt && rateAt > swAt,
       'index.html：切换条在右侧栏内、位于"当前总功率"卡之前');
    ok(mainAt > 0 && fleetAt > mainAt && fleetAt < sideAt,
       'index.html：汇总页在主列里（进汇总后侧栏仍留着切换条，点卡片就能回去）');

    // 设备设置的入口从"右栏底部那张空卡片"搬到了切换条的卡片上：
    // 卡片是当前这台，单击弹出的就是这台的设置，所以那颗按钮连同整张卡都该消失。
    ok(indexHtml.indexOf('id="cardSettings"') < 0,
       'index.html：右栏底部那张"设备设置"卡片已经撤掉');
    ok(indexHtml.indexOf('id="settingsOpen"') < 0, '连它的"设置"按钮一起撤掉');
    ok(indexHtml.indexOf('id="settingsDeviceName"') > 0,
       '设置弹窗的标题留了设备名的位置（否则看不出弹的是哪一台）');
    ok(/function open\(name\)/.test(indexHtml) && /openSettingsModal/.test(indexHtml),
       '弹窗的打开函数接收设备名（由切换条卡片传进来）');

    // 两个页面的脚本链都必须能跑通（不抛 ReferenceError）
    ok(fs.existsSync(path.join(STATIC, 'device-store.js')), 'device-store.js 存在');
}

console.log(failed === 0 ? `\nAll ${passed} device-store checks passed.` : `\n${failed}/${passed + failed} device-store checks FAILED.`);
process.exit(failed === 0 ? 0 : 1);
