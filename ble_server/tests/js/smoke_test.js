#!/usr/bin/env node
/**
 * Smoke test: evaluates each page's scripts (app.js, phone.js, charge_history.js,
 * config.html inline script, embedded pages) in a sandbox with the REAL i18n
 * runtime and permissive DOM/fetch/Chart stubs, to catch ReferenceErrors and
 * other load-time/runtime wiring bugs introduced by the i18n changes.
 *
 * Usage: node tests/js/smoke_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.resolve(__dirname, '../../web');
const STATIC = path.join(WEB, 'static');

function makeNoop() {
    const fn = function noop() {};
    return new Proxy(fn, {
        get(t, p) {
            if (p === Symbol.toPrimitive) return () => 0;
            return makeNoop();
        },
        set() { return true; },
        apply() { return makeNoop(); },
        construct() { return makeNoop(); },
    });
}

// 真实的 CSSStyleDeclaration 至少要有 setProperty/removeProperty——phone.js 用它
// 往卡片上写 --depth 这类自定义属性。只给 {} 的话这些赋值会被静默跳过，
// 冒烟测试就测不到那条路径了。按元素缓存一份，便于"写后读"。
function makeStyle() {
    const props = {};
    return {
        setProperty(k, v) { props[k] = String(v); },
        getPropertyValue(k) { return props[k] || ''; },
        removeProperty(k) { delete props[k]; },
    };
}

function makeEl() {
    const style = makeStyle();
    return new Proxy(function el() {}, {
        get(t, p) {
            if (p === 'style') return style;
            if (p === 'dataset') return {};
            if (p === 'classList') {
                return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
            }
            if (p === 'children' || p === 'options') return [];
            return makeNoop();
        },
        set() { return true; },
        apply() { return makeNoop(); },
        construct() { return makeEl(); },
    });
}

function buildSandbox(loc) {
    const fetched = [];
    const events = [];
    const listeners = {};
    const sandbox = {
        console,
        Intl,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        Promise,
        JSON,
        Math,
        Date,
        URL,
        RegExp,
        String,
        Number,
        Boolean,
        Object,
        Array,
        isNaN,
        parseFloat,
        parseInt,
        Error,
        fetch: async (url) => {
            fetched.push(String(url));
            // Serve a server-side language preference so every page's
            // initFromServer() → applyServerLanguage() path is exercised.
            if (String(url).indexOf('/api/web-language') !== -1) {
                return { ok: true, status: 200, json: async () => ({ ok: true, language: 'en' }) };
            }
            // /api/status 是切设备重放链的终点：给一个空但**形状正确**的载荷，
            // 否则 updateUI 在 stub 的 {} 上抛异常，日志里全是与本次改动无关的堆栈。
            if (String(url).indexOf('/api/status') !== -1) {
                return {
                    ok: true, status: 200,
                    json: async () => ({
                        ok: true, connected: false, authenticated: false,
                        ports: {}, settings: {}, protocol_switches: {}, protocol_extend: 0,
                    }),
                };
            }
            return { ok: true, status: 200, json: async () => ({}) };
        },
        EventSource: function () { this.onopen = null; this.onmessage = null; this.onerror = null; this.close = function () {}; },
        Chart: function () { return buildChart(); },
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        navigator: { language: 'en-US' },
        location: Object.assign({ origin: 'http://charger.local', href: 'http://charger.local/', search: '' }, loc || {}),
        getComputedStyle: () => ({ getPropertyValue: () => '' }),
        // 页面里有跟随系统深浅色的分支（setTheme）：跑计时器时会被摸到
        matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {} }),
        // 页面之间靠 CustomEvent 通信（device-changed / sse-session-end）：桩成
        // no-op 的话，"切设备后各组件重拉"这条路径等于完全没被测到。
        CustomEvent: class CustomEvent {
            constructor(type, init) {
                this.type = type;
                this.detail = init && init.detail;
            }
        },
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener(type, fn) {
            const arr = listeners[type] || [];
            const i = arr.indexOf(fn);
            if (i >= 0) arr.splice(i, 1);
        },
        dispatchEvent(ev) {
            events.push(ev);
            (listeners[ev.type] || []).forEach((fn) => fn(ev));
            return true;
        },
    };
    sandbox.window = sandbox;
    const document = {
        documentElement: makeEl(),
        body: makeEl(),
        head: makeEl(),
        title: '',
        readyState: 'complete',
        getElementById: () => makeEl(),
        querySelector: () => makeEl(),
        querySelectorAll: () => [],
        createElement: () => makeEl(),
        addEventListener() {},
    };
    sandbox.document = document;
    sandbox.__fetched = fetched;
    sandbox.__events = events;
    const ctx = vm.createContext(sandbox);
    return ctx;
}

function buildChart() {
    return {
        data: { labels: [], datasets: [] },
        options: { scales: {} },
        _peakData: null,
        update() {},
        destroy() {},
        scales: {},
        canvas: { parentNode: makeEl() },
        ctx: {},
    };
}

function load(js, loc) {
    const ctx = buildSandbox(loc);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'locales/zh-CN.js'), 'utf8'), ctx);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'locales/en.js'), 'utf8'), ctx);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'i18n.js'), 'utf8'), ctx);
    vm.runInContext(js, ctx);
    return ctx;
}

// Flip the language back and forth: exercises every I18N.onChange re-render
// hook registered by the page scripts (rerenderDynamic, renderAll, refreshChargeHistory...).
function flipLocales(ctx, cycles) {
    const n = cycles || 2;
    for (let i = 0; i < n; i++) {
        vm.runInContext("I18N.setLocale('en'); I18N.setLocale('zh-CN');", ctx);
    }
}

const checks = [];
function check(name, fn) { checks.push({ name, fn }); }

function extractInlineScripts(htmlPath) {
    const html = fs.readFileSync(htmlPath, 'utf8');
    const scripts = [];
    const re = /<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g;
    let m;
    while ((m = re.exec(html))) scripts.push(m[1]);
    return scripts.join('\n');
}

function indexChain() {
    return [
        fs.readFileSync(path.join(STATIC, 'chart-config.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'chart-loader.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'device-store.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'device-bench.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'charge_history.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'charge_limit.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8'),
    ].join('\n;\n');
}

check('index.html chain: chart-config + chart-loader + device-store + device-bench + charge_history + charge_limit + app', () => {
    const ctx = load(indexChain());
    flipLocales(ctx); // exercises app.js rerenderDynamic + charge_history refresh
});

check('phone.html chain: device-store + device-bench + charge_history + charge_limit + phone.js', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'device-store.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'device-bench.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'charge_history.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'charge_limit.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'phone.js'), 'utf8'),
    ].join('\n;\n');
    const ctx = load(parts);
    flipLocales(ctx); // exercises phone.js renderAll on locale change
});

// ── 点卡片 = 整页换成那台设备的数据 ────────────────────────────────────
// 这条是用户在界面上唯一能感知的"多设备"行为：设备面板、端口控制、图表、设置、
// 用电统计、记录列表必须一起换过去。测法是跑真实页面脚本 + 真实 device-store，
// 切完设备后看三个证据：指针动了、device-changed 发了、重放链真的发了带
// ?device=<新设备> 的请求（app.js 的 replay 会重拉状态与历史）。
check('点设备卡片：整页重放，且请求带新设备', async () => {
    const ctx = load(indexChain());
    const res = vm.runInContext(`(function () {
        var DEVICES = [
            { id: 'default', name: '', mac: '3C:CD:73:00:11:22', connected: true, topic_prefix: 'cuktech/charger', is_primary: true },
            { id: 'desk', name: 'Desk', mac: 'AA:BB:CC:DD:EE:02', connected: true, topic_prefix: 'cuktech/charger/desk', is_primary: false }
        ];
        DeviceStore.dispatch({ type: 'init', devices: DEVICES, connected: true, authenticated: true, ports: {} });
        __fetched.length = 0;
        __events.length = 0;
        var before = DeviceStore.current();
        DeviceStore.switchDevice('desk');
        var urls = __fetched.slice();
        return {
            promise: new Promise(function (r) { setTimeout(r, 30); }),   // 放走重放链上的 await
            before: before,
            after: DeviceStore.current(),
            changed: __events.filter(function (e) { return e.type === 'device-changed'; }).length,
            detail: (__events.filter(function (e) { return e.type === 'device-changed'; })[0] || {}).detail || null,
            askedForDesk: urls.filter(function (u) { return u.indexOf('device=desk') >= 0; }),
            leakedDefault: urls.filter(function (u) { return u.indexOf('device=default') >= 0; })
        };
    })()`, ctx);

    await res.promise;

    if (res.before !== 'default' || res.after !== 'desk') {
        throw new Error('设备指针没切过去：' + res.before + ' → ' + res.after);
    }
    if (res.changed !== 1) {
        throw new Error('device-changed 应发 1 次，实际 ' + res.changed + ' 次');
    }
    if (!res.detail || res.detail.device !== 'desk') {
        throw new Error('device-changed.detail 没带新设备：' + JSON.stringify(res.detail));
    }
    // 重放链的证据：切完之后必须有为新设备发出的请求（状态/图表/统计/限额…）
    const lateDesk = vm.runInContext('__fetched.filter(function (u) { return u.indexOf("device=desk") >= 0; })', ctx);
    const lateDefault = vm.runInContext('__fetched.filter(function (u) { return u.indexOf("device=default") >= 0; })', ctx);
    res.askedForDesk = lateDesk;
    res.leakedDefault = lateDefault;
    if (res.askedForDesk.length < 3) {
        throw new Error('切设备后只为新设备发了 ' + res.askedForDesk.length + ' 个请求：'
            + JSON.stringify(res.askedForDesk));
    }
    if (res.leakedDefault.length) {
        throw new Error('切设备后仍在请求旧设备：' + JSON.stringify(res.leakedDefault.slice(0, 3)));
    }
    // 图表数据必须已被清空重拉（否则屏幕上先是上一台的曲线）
    const chart = vm.runInContext('__fetched.some(function (u) { return u.indexOf("/api/chart") >= 0 && u.indexOf("device=desk") >= 0; })', ctx);
    if (!chart) throw new Error('切设备后没有为新设备重拉图表历史');
});

// ── 预览模式（?preview=multi）：伪造设备必须"真的能切" ─────────────────
// 这是用户实际踩到的问题：切了卡片页面还是真机的数据。根因是预览早先只改高亮、
// 不动指针也不灌数据。现在预览走真 store：伪造设备是真实的桶（由页面的
// previewSeed 灌数据）、切换走真 switchDevice、只有网络被 store 拦掉。
check('预览模式：伪造设备是真的桶，切得过去，且不出网', async () => {
    const ctx = load(indexChain(), {
        href: 'http://charger.local/index.html?preview=multi',
        search: '?preview=multi',
    });
    const res = vm.runInContext(`(function () {
        var pv2 = DeviceStore.get('pv2');
        var seeded = !!(pv2 && pv2.ports && Object.keys(pv2.ports).length === 4);
        var watts = seeded ? Object.keys(pv2.ports).reduce(function (n, k) {
            return n + (Number(pv2.ports[k].power) || 0);
        }, 0) : 0;
        __fetched.length = 0;
        var switched = DeviceStore.switchDevice('pv2');
        var payload = DeviceStore.api('/api/status').then(function (r) { return r.json(); });
        return { seeded: seeded, watts: watts, switched: switched,
                 now: DeviceStore.current(), fetched: __fetched.slice(),
                 payload: payload };
    })()`, ctx);

    const payload = await res.payload;
    if (!res.seeded) throw new Error('预览设备 pv2 的桶没有被 previewSeed 灌上 4 个端口');
    if (!(res.watts > 0)) throw new Error('预览设备 pv2 的读数是 0W —— 页面切过去看不出变化');
    if (res.switched !== true || res.now !== 'pv2') {
        throw new Error('切到预览设备失败：switchDevice=' + res.switched + ' current=' + res.now);
    }
    if (res.fetched.length) {
        throw new Error('预览设备居然发了网络请求：' + JSON.stringify(res.fetched.slice(0, 3)));
    }
    // 假响应必须带着这台伪造设备自己的快照（页面靠它渲染端口/设置卡）
    if (!payload || payload.ok !== true || !payload.ports || Object.keys(payload.ports).length !== 4) {
        throw new Error('api() 对预览设备返回的假响应不完整：' + JSON.stringify(payload).slice(0, 120));
    }
});

check('config.html inline script', () => {
    load(extractInlineScripts(path.join(WEB, 'config.html')));
});

check('countdown.html inline script', () => {
    const ctx = load(extractInlineScripts(path.join(STATIC, 'countdown.html')));
    flipLocales(ctx);
});

check('device_info.html inline script', () => {
    const ctx = load(extractInlineScripts(path.join(STATIC, 'device_info.html')));
    flipLocales(ctx);
});

check('port_monitor.html inline script', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'chart-config.js'), 'utf8'),
        extractInlineScripts(path.join(STATIC, 'port_monitor.html')),
    ].join('\n;\n');
    const ctx = load(parts);
    flipLocales(ctx);
});

check('power_chart.html inline script', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'chart-config.js'), 'utf8'),
        extractInlineScripts(path.join(STATIC, 'power_chart.html')),
    ].join('\n;\n');
    load(parts);
});

(async function main() {
    let failed = 0;
    for (const { name, fn } of checks) {
        try {
            await fn();
            console.log('  ✓ ' + name);
        } catch (e) {
            failed++;
            console.error('  ✗ ' + name);
            console.error('    ' + (e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n    ') : e));
        }
    }
    console.log(failed === 0 ? `\nAll ${checks.length} smoke checks passed.` : `\n${failed}/${checks.length} smoke checks FAILED.`);
    process.exit(failed === 0 ? 0 : 1);
})();