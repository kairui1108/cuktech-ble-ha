#!/usr/bin/env node
/**
 * device-bench.js 的契约测试（多设备工作台：切换条 + 全部设备汇总）。
 *
 * 为什么要有这一份：多设备界面最容易"看起来有、其实不对"——名字串台、瓦数不刷新、
 * 汇总清单不排序、单设备时多出一块空白、中文界面里冒出内部 id 或英文文案。
 * 这些都不是崩溃，而是"用户以为自己在看 A、其实看的是 B"。
 *
 * 手法：真的 device-store.js + 真的 device-bench.js，DOM 用一个可查询的最小实现
 * （能按 class 选、能读文本、能计 innerHTML 重建次数），语言用真的两套 locale
 * 包驱动一个最小 I18N —— 因此断言能直接落到"用户看到的那几个字"上。
 *
 * Usage: node tests/js/device_bench_test.js
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

/* ── 最小 DOM：解析 innerHTML，按 class 查询，读写文本/属性 ─────────────── */

const VOID_TAGS = new Set(['br', 'img', 'input', 'hr', 'meta', 'link']);
const ATTR_RE = /([\w-]+)(?:\s*=\s*"([^"]*)")?/g;

function parseAttrs(str) {
    const attrs = {};
    let m;
    ATTR_RE.lastIndex = 0;
    while ((m = ATTR_RE.exec(str))) attrs[m[1]] = m[2] === undefined ? '' : m[2];
    return attrs;
}

function classSetOf(attrs) {
    const set = new Set(((attrs && attrs.class) || '').split(/\s+/).filter(Boolean));
    return set;
}

/** 极简 inline style：**直接赋值**（`el.style.opacity = …`，bench 跟手时就是这么写的）
 *  也要能读回来——查"拖拽被打断后透明度有没有清干净"必须看得到它。 */
function makeStyle() {
    const store = {};
    return new Proxy(store, {
        set(t, k, v) { t[k] = String(v); return true; },
        get(t, k) {
            if (k === 'setProperty') return (n, v) => { t[n] = String(v); };
            if (k === 'getPropertyValue') return (n) => (n in t ? t[n] : '');
            if (k === 'removeProperty') return (n) => { delete t[n]; };
            return t[k];
        }
    });
}

/** 统一的元素对象：解析出来的节点与 getElementById 拿到的节点形状一致。 */
function makeNode(tag, attrs) {
    const node = {
        tag: tag || 'div',
        attrs: attrs || {},
        children: [],
        parent: null,
        id: (attrs && attrs.id) || '',
        hidden: false,
        tabIndex: 0,
        _text: '',
        _html: '',
        _cls: classSetOf(attrs),
        style: makeStyle(),
        listeners: {},
        addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
        removeEventListener() {},
        setAttribute(k, v) {
            this.attrs[k] = String(v);
            if (k === 'class') { this._cls = classSetOf(this.attrs); }
        },
        getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
        removeAttribute(k) { delete this.attrs[k]; },
        // closest 只往上看自己与祖先，够 event-delegate 用
        closest(sel) {
            const want = String(sel).replace(/^\./, '');
            let cur = this;
            while (cur) {
                if (cur._cls && cur._cls.has(want)) return cur;
                cur = cur.parent;
            }
            return null;
        },
        querySelectorAll(sel) { return findNodes(this, sel); },
        querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
        appendChild(child) { child.parent = this; this.children.push(child); },
        fire(type, ev) {
            ev = ev || {};
            if (!ev.target) ev.target = this;
            (this.listeners[type] || []).forEach((fn) => fn(ev));
        },
        get textContent() {
            if (this._text) return this._text;
            return this.children.map((c) => c.textContent).join('');
        },
        set textContent(v) { this._text = String(v === undefined || v === null ? '' : v); this.children = []; }
    };
    node.classList = {
        add: (c) => { node._cls.add(c); node.attrs.class = [...node._cls].join(' '); },
        remove: (c) => { node._cls.delete(c); node.attrs.class = [...node._cls].join(' '); },
        toggle: (c, on) => {
            if (on === undefined) { node._cls.has(c) ? node._cls.delete(c) : node._cls.add(c); }
            else if (on) node._cls.add(c); else node._cls.delete(c);
            node.attrs.class = [...node._cls].join(' ');
        },
        contains: (c) => node._cls.has(c)
    };
    Object.defineProperty(node, 'innerHTML', {
        get() { return node._html; },
        set(v) {
            node._html = v;
            node.children = [];
            parseInto(node, v);
        }
    });
    return node;
}

/** 把一段 HTML 解析成 node 的子节点树（父指针要正确，event-delegate 依赖它）。 */
function parseInto(root, html) {
    const stack = [root];
    const re = /<(\/?)([a-zA-Z][\w-]*)((?:\s*[\w-]+(?:\s*=\s*"[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
    let m;
    while ((m = re.exec(html))) {
        if (m[5] !== undefined) {
            const text = m[5];
            if (text.trim()) {
                const tn = makeNode('#text', {});
                tn._text = text;
                tn.parent = stack[stack.length - 1];
                stack[stack.length - 1].children.push(tn);
            }
            continue;
        }
        const [, closing, tag, attrStr, selfClose] = m;
        if (closing) {
            if (stack.length > 1) stack.pop();
            continue;
        }
        const el = makeNode(tag, parseAttrs(attrStr || ''));
        el.parent = stack[stack.length - 1];
        stack[stack.length - 1].children.push(el);
        if (!selfClose && !VOID_TAGS.has(tag)) stack.push(el);
    }
}

function allEls(node, out) {
    out = out || [];
    (node.children || []).forEach((c) => {
        if (c.tag !== '#text') { out.push(c); allEls(c, out); }
    });
    return out;
}

/** 解析单个选择器片段：tag / .class / [attr] / [attr="v"] 的组合 */
function parsePart(part) {
    let rest = String(part);
    let tag = null;
    const tm = /^[a-zA-Z][\w-]*/.exec(rest);
    if (tm) { tag = tm[0].toLowerCase(); rest = rest.slice(tm[0].length); }
    const classes = [];
    let attr = null;
    let want;
    const re = /\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
    let m;
    while ((m = re.exec(rest))) {
        if (m[1]) classes.push(m[1]);
        else { attr = m[2]; want = m[3]; }
    }
    return { tag: tag, classes: classes, attr: attr, want: want };
}

function matchNode(n, p) {
    if (p.tag && n.tag.toLowerCase() !== p.tag) return false;
    for (let i = 0; i < p.classes.length; i++) {
        if (!(n._cls && n._cls.has(p.classes[i]))) return false;
    }
    if (p.attr) {
        if (!(p.attr in n.attrs)) return false;
        if (p.want !== undefined && n.attrs[p.attr] !== p.want) return false;
    }
    return true;
}

/** 支持后代选择器（".bench-w b" 这种），够覆盖 bench 用到的查询。 */
function findNodes(root, sel) {
    const parts = String(sel).trim().split(/\s+/).filter(Boolean).map(parsePart);
    let scopes = [root];
    let found = [];
    for (let i = 0; i < parts.length; i++) {
        found = [];
        for (let j = 0; j < scopes.length; j++) {
            allEls(scopes[j]).forEach((n) => {
                if (matchNode(n, parts[i])) found.push(n);
            });
        }
        scopes = found;
    }
    return found;
}

function textOf(node) {
    return node && node.textContent ? node.textContent : '';
}

function makeElement(id) {
    const el = makeNode('div', { id: id || '' });
    el.id = id || '';
    return el;
}

/* ── 沙箱 ───────────────────────────────────────────────────────────── */

function loadLocales() {
    const sandbox = { console, global: null };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    for (const lang of ['zh-CN', 'en']) {
        vm.runInContext(fs.readFileSync(path.join(STATIC, `locales/${lang}.js`), 'utf8'), sandbox);
    }
    return sandbox.I18N_RESOURCES;
}

function makeI18N(packs) {
    let locale = 'zh-CN';
    const listeners = [];
    function lookup(dict, key) {
        return key.split('.').reduce((n, p) => (n == null ? undefined : n[p]), dict);
    }
    return {
        listeners,
        getLocale: () => locale,
        setLocale(next) { locale = next; listeners.forEach((fn) => fn(next)); },
        t(key, params) {
            let v = lookup(packs[locale] || {}, key);
            if (v == null) v = lookup(packs['zh-CN'] || {}, key);
            if (v == null) return key;
            return String(v).replace(/\{\{\s*([\w.]+)\s*\}\}/g,
                (m, name) => (params && params[name] !== undefined ? String(params[name]) : m));
        },
        onChange(fn) { listeners.push(fn); }
    };
}

function buildEnv(opts) {
    opts = opts || {};
    const packs = loadLocales();
    const i18n = makeI18N(packs);
    if (opts.locale) i18n.setLocale(opts.locale);

    const els = new Map();
    const body = makeNode('body', {});
    const bodyClass = body.classList;
    const document = {
        body: body,
        getElementById(id) {
            if (!els.has(id)) els.set(id, makeElement(id));
            return els.get(id);
        },
        addEventListener() {},
        querySelectorAll() { return []; },
        documentElement: { setAttribute() {} },
        readyState: 'complete'
    };

    const timers = [];
    /* 可控时钟：翻牌收尾靠 setTimeout(320)，但真实浏览器里后台标签页的定时器会被
       节流甚至压根不回调，所以忙标记另有"超时自动放开"。要验证那一条，就得能把
       **时间**往前推而不推进定时器队列——这与 flushTimers 是两件事。 */
    const clock = { t: Date.now() };
    const FakeDate = function () { return new Date(clock.t); };
    FakeDate.now = () => clock.t;
    FakeDate.parse = Date.parse;
    FakeDate.UTC = Date.UTC;
    const sandbox = {
        __fetches: [],
        console: { log() {}, warn() {}, error() {} },
        Intl, URL, document, I18N: i18n,
        navigator: { language: 'zh-CN' },
        location: { origin: 'http://ble:8199', href: 'http://ble:8199/', search: '' },
        history: { replaceState() {} },
        localStorage: { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
        setTimeout: (fn, ms) => { timers.push({ fn, ms: ms || 0 }); return timers.length; },
        clearTimeout() {},
        setInterval: () => 0, clearInterval() {},
        CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },
        dispatchEvent() {},
        fetch: (u) => {
            sandbox.__fetches.push(String(u));
            if (String(u) === '/api/devices' && opts.devicesEndpoint) {
                return Promise.resolve({
                    ok: true, status: 200,
                    json: () => Promise.resolve({ ok: true, devices: opts.devicesEndpoint })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, total_wh: 0 }) });
        },
        Promise, JSON, Math, Date: FakeDate, RegExp, String, Number, Boolean, Object, Array, Error, Map, Set, isNaN, parseFloat, parseInt
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    for (const f of ['device-store.js', 'device-bench.js']) {
        vm.runInContext(fs.readFileSync(path.join(STATIC, f), 'utf8'), sandbox, { filename: f });
    }
    /**
     * 推进"已经到点"的定时器（默认全部）。翻牌的收尾是 320ms，而"吃掉 click"的
     * 悬挂保护是 400ms —— 测试要的是"翻牌完了、click 还没来"这一刻，所以按延时筛。
     */
    const flushTimers = (maxMs) => {
        const limit = maxMs === undefined ? Infinity : maxMs;
        const keep = [];
        timers.forEach((t) => {
            if (t.ms > limit) { keep.push(t); return; }
            try { t.fn(); } catch (e) { /* 定时器里的失败不该吞掉测试 */ }
        });
        timers.length = 0;
        keep.forEach((t) => timers.push(t));
    };
    return { sandbox, i18n, els, bodyClass, document, flushTimers, clock };
}

const DEVICES = [
    { id: 'default', name: '', mac: '3C:CD:73:34:AE:59', connected: true, topic_prefix: 'cuktech/charger', is_primary: true },
    { id: 'desk', name: '书桌充电器', mac: 'AA:BB:CC:DD:EE:02', connected: true, topic_prefix: 'cuktech/charger/desk', is_primary: false },
    { id: 'bed', name: '卧室', mac: 'AA:BB:CC:DD:EE:03', connected: false, topic_prefix: 'cuktech/charger/bed', is_primary: false }
];

/** 造一台设备的桶内容（桌面页形状：ports 的 key 是 1..4） */
function seedBucket(store, id, spec) {
    const b = store.get(id);
    b.connected = !!spec.connected;
    b.authenticated = !!spec.connected;
    b.bleConnected = !!(spec.connected && spec.connected);
    b.ports = {};
    [1, 2, 3, 4].forEach((p) => {
        b.ports[String(p)] = {
            voltage: 0, current: 0,
            power: (spec.power && spec.power[p]) || 0,
            active: !!((spec.power && spec.power[p]) || 0),
            enabled: true, protocol: 'idle'
        };
    });
}

function boot(opts) {
    opts = opts || {};
    const env = buildEnv(opts);
    const { sandbox } = env;
    const store = sandbox.DeviceStore;

    // 桌面页的折算函数（与 app.js 里 benchSummary 同源，测试里独立写一份，
    // 这样 bench 本身的契约不依赖 app.js 的实现细节）
    function summarise(b) {
        let watts = 0;
        const ports = ['1', '2', '3', '4'].map((id) => {
            const p = (b.ports || {})[id] || {};
            const w = Number(p.power) || 0;
            if (p.active) watts += w;
            return { key: id, label: 'C' + id, active: !!p.active, power: w, enabled: p.enabled !== false, color: 'var(--port-c' + id + ')' };
        });
        return { online: !!(b.connected && b.authenticated), watts, ports };
    }

    sandbox.DeviceBench.init({
        mode: opts.mode || 'desktop', railId: 'deviceSwitcher', fleetId: 'fleetView',
        preview: !!opts.preview,
        previewSeed: opts.previewSeed,
        summarise,
        fetchEnergy: (id, done) => done(whByDevice[id] === undefined ? null : whByDevice[id])
    });
    const whByDevice = opts.energy || {};

    // 设备清单走真实入口：SSE init 的 devices 字段
    store.dispatch({ type: 'init', devices: opts.devices === undefined ? DEVICES : opts.devices });
    Object.entries(opts.buckets || {}).forEach(([id, spec]) => seedBucket(store, id, spec));
    sandbox.DeviceBench.refresh();
    return {
        env,
        store,
        rail: env.els.get('deviceSwitcher'),
        fleet: env.els.get('fleetView'),
        sandbox,
        clock: env.clock,
        flush: env.flushTimers,
    };
}

const tiles = (rail) => rail.querySelectorAll('.bench-tile[data-device]');

/** 点某张卡片（牌堆里只点得到露出来的邻卡）。 */
const clickTile = (rail, id) => rail.fire('click', {
    target: {
        closest: (sel) => (sel === '.bench-tile'
            ? tiles(rail).find((t) => t.getAttribute('data-device') === id) : null)
    }
});
/** 滚轮翻牌 */
const wheel = (rail, dy) => rail.fire('wheel', { deltaY: dy, deltaX: 0, preventDefault() {} });
/** 方向键翻牌 */
const key = (rail, k) => rail.fire('keydown', { key: k, preventDefault() {} });
/** 拖动翻牌：从 x=200 拖到 x=200+dx */
function swipe(rail, dx) {
    rail.fire('pointerdown', { clientX: 200, pointerId: 1, pointerType: 'touch', button: 0 });
    rail.fire('pointermove', { clientX: 200 + dx, pointerId: 1, pointerType: 'touch' });
    rail.fire('pointerup', { clientX: 200 + dx, pointerId: 1, pointerType: 'touch' });
}
const tileById = (rail, id) => tiles(rail).find((t) => t.getAttribute('data-device') === id);
const rows = (fleet) => fleet.querySelectorAll('.fleet-row');

/* ── A. 单设备：只显示一张卡（它同时是这台设备的设置入口） ─────────── */

console.log('\n── A. 单设备 ──');
{
    const { rail, fleet, env } = boot({ devices: [DEVICES[0]] });
    ok(rail.hidden === false, '单设备时切换条照样出现（设置入口在这张卡上）');
    eq(tiles(rail).length, 1, '只有一张卡片');
    ok(!tileById(rail, '__all__'), '单设备时没有"全部"这一面（一张卡的汇总就是它自己）');
    eq(tileById(rail, 'default').style.getPropertyValue('--depth'), '0', '唯一那张就是栈顶');
    ok(tileById(rail, 'default').classList.contains('is-top'), '唯一那张可见');
    eq(rail.querySelectorAll('.bench-pager').length, 0,
       '一张卡时不生成翻牌条（没有可翻的方向，箭头/进度点都是噪音）');
    ok(env.bodyClass.contains('has-device-bench'), 'body 挂上工作台类（桌面侧栏有这一格）');
    ok(fleet.hidden !== false, '单设备时汇总页不显示');

    // 只有一张卡：没有"下一张"，拖动/滚轮/方向键都不该有反应（卡片不该被拖起来晃）
    const only = tileById(rail, 'default');
    rail.fire('pointerdown', { clientX: 200, pointerId: 3 });
    rail.fire('pointermove', { clientX: 60, pointerId: 3 });
    rail.fire('pointerup', { clientX: 60, pointerId: 3 });
    ok(!only.style.transform, '单设备拖动：卡片不跟手（没有可翻的目标）');
    ok(!only.style.opacity, '也不会留下半透明的渐隐');
    ok(!rail.querySelectorAll('.bench-deck')[0].classList.contains('is-shifting'),
       'shifting 状态一直是关的（不会把"下一张"放出来）');
    let prevented = false;
    rail.fire('wheel', { deltaY: 60, deltaX: 0, preventDefault() { prevented = true; } });
    ok(!prevented, '单设备时滚轮不被吃掉：页面照常滚动');
    prevented = false;
    rail.fire('keydown', { key: 'ArrowRight', preventDefault() { prevented = true; } });
    ok(!prevented, '单设备时方向键同样留给页面');
}
{
    // 单设备的服务端**不发** devices 清单（§3.8：单设备事件流逐字节不变）：
    // 这时按"就一台"补一张卡片，名字等 /api/devices 探测回来再补全。
    const { rail } = boot({ devices: [] });
    ok(rail.hidden === false, 'SSE 没给设备清单（单设备）时也有一张卡');
    eq(tiles(rail).length, 1, '就是那一张');
    eq(tiles(rail)[0].getAttribute('data-device'), 'default', 'id 回退到 primary');
    ok(textOf(tileById(rail, 'default')).includes('默认充电器'),
       '名字退化成"默认充电器"（宁可这样，也不把内部 id 露出来）');
}
{
    // 手机页不再用这副牌：设备切换长在标题栏的下拉列表里（phone.js 自己渲染），
    // bench 在手机上只负责"全部设备"页。
    const { fleet, sandbox } = boot({ railId: 'noRail' });
    sandbox.DeviceBench.enterFleet();
    ok(fleet.hidden === false, '"全部设备"页仍然由 bench 管（手机上唯一剩下的部分）');
    eq(fleet.querySelectorAll('.fleet-row').length, 3, '清单照常按设备列出');
}

// 单设备时再向只读端点 /api/devices 探一次（SSE 清单为空）：卡片上要显示真名。
// 断言放在收尾的 setTimeout 里（那一刻微任务已排空，探测的 .then 链也走完了）。
const PROBE = boot({
    devices: [],
    devicesEndpoint: [{ id: 'default', name: '客厅充电器', mac: 'AA:BB:CC:DD:EE:09', is_primary: true, connected: true }]
});
const NO_PROBE = boot({});          // 多设备：SSE 已经给了清单

/* ── B. 切换条：名字、瓦数、端口微点、功率脊 ─────────────────────── */

console.log('\n── B. 切换条 ──');
const B = boot({
    buckets: {
        default: { connected: true, power: { 1: 30 } },
        desk: { connected: true, power: { 2: 60 } },
        bed: { connected: false, power: {} }
    }
});
{
    const { rail } = B;
    ok(rail.hidden === false, '多设备时切换条出现');
    eq(tiles(rail).length, 4, '3 台设备 = 3 块瓷砖 + 1 块"全部"');
    eq(tiles(rail)[0].getAttribute('data-device'), '__all__', '"全部"排在最左');

    const primaryTile = tileById(rail, 'default');
    ok(textOf(primaryTile).includes('默认充电器'),
       'primary 没名字时显示"默认充电器"，不是内部 id `default`', textOf(primaryTile));
    ok(!textOf(primaryTile).includes('default'), '内部 id 决不出现在用户可见文本里');
    ok(textOf(tileById(rail, 'desk')).includes('书桌充电器'), '有名字的设备显示配置名');

    const w = (id) => textOf(tileById(rail, id).querySelector('.bench-w')).replace(/\s/g, '');
    eq(w('default'), '30W', 'primary 显示实时瓦数');
    eq(w('desk'), '60W', 'desk 显示实时瓦数');
    eq(w('bed'), '0W', '离线设备显示 0W（不是空白）');

    // 功率脊：刻度是**整机 120W**，不是"占全家的份额"
    // （份额会随设备数变化：两台时 60W 就占满一半，多插一台这条线就缩一半）
    const share = (id) => Number(tileById(rail, id).querySelector('.bench-spine').style.getPropertyValue('--share'));
    ok(Math.abs(share('desk') - 60 / 120) < 1e-6, 'desk 的功率脊 = 60/120（半格）');
    ok(Math.abs(share('default') - 30 / 120) < 1e-6, 'primary 的功率脊 = 30/120');
    eq(share('bed'), 0, '离线设备的功率脊为 0');
    // "全部"那张的刻度是 n × 120W：合计 90W / (3 × 120) = 0.25
    ok(Math.abs(share('__all__') - 90 / 360) < 1e-6, '"全部"的功率脊 = 合计 / (n×120)');
    // 同一台设备、同一读数，设备数不同时长度必须一样
    const two = boot({
        devices: [DEVICES[0], DEVICES[1]],
        buckets: { default: { connected: true, power: { 1: 30 } }, desk: { connected: true, power: { 2: 60 } } }
    });
    const share2 = Number(tileById(two.rail, 'desk').querySelector('.bench-spine').style.getPropertyValue('--share'));
    ok(Math.abs(share2 - share('desk')) < 1e-6,
       '换成两台设备后 desk 的脊长度不变（刻度与设备数无关）');
    // 上限封顶：超过 120W 不会画到卡外
    const over = boot({
        devices: [DEVICES[0], DEVICES[1]],
        buckets: { default: { connected: true, power: { 1: 120 } }, desk: { connected: true, power: { 2: 60 } } }
    });
    const shareOver = Number(tileById(over.rail, 'default').querySelector('.bench-spine').style.getPropertyValue('--share'));
    eq(shareOver, 1, '满 120W 就是整条，再多也不溢出');
    // 有读数但很小时给一点最小可见量（0 与"正在充一点"必须区分得开）
    const tiny = boot({
        devices: [DEVICES[0], DEVICES[1]],
        buckets: { default: { connected: true, power: { 1: 1 } }, desk: { connected: false, power: {} } }
    });
    const shareTiny = Number(tileById(tiny.rail, 'default').querySelector('.bench-spine').style.getPropertyValue('--share'));
    eq(shareTiny, 0.02, '1W（1/120 ≈ 0.83%）也画一条最小可见的脊，而不是看不见');
    ok(shareTiny < share('default'), '但仍然明显短于 30W 那条（能看出差别）');

    // 端口微点：只有带电的口亮
    const dots = tileById(rail, 'desk').querySelectorAll('.bench-port');
    eq(dots.length, 4, '每块瓷砖 4 个端口微点');
    eq(dots.map((d) => d.classList.contains('is-on')), [false, true, false, false],
       '只有 c2 在供电（微点与设备图上的口位一一对应）');

    ok(textOf(tileById(rail, '__all__')).replace(/\s/g, '').includes('90W'),
       '"全部"显示全家合计瓦数');
    ok(textOf(tileById(rail, '__all__')).includes('2/3'), '"全部"显示在线台数');
    ok(!tileById(rail, '__all__').classList.contains('active'), '默认停在单设备视图："全部"不是选中态');
    ok(tileById(rail, 'default').classList.contains('active'), '默认选中态落在当前设备（primary）上');
}

/* ── C. 全部设备（汇总）：排序、行内容、点行进设备 ───────────────── */

console.log('\n── C. 全部设备汇总 ──');
{
    const { rail, fleet, store } = B;
    tiles(rail).find((t) => t.getAttribute('data-device') === '__all__')
        .parent && null;
    // 点"全部"
    rail.fire('click', { target: { closest: (sel) => (sel === '.bench-tile' ? tileById(rail, '__all__') : null) } });

    ok(fleet.hidden === false, '进入汇总页：容器显示');
    eq(rows(fleet).map((r) => r.getAttribute('data-device')), ['desk', 'default', 'bed'],
       '按实时功率从高到低排序（60W → 30W → 0W）');
    const first = rows(fleet)[0];
    ok(textOf(first).includes('书桌充电器'), '行里有设备名');
    ok(textOf(first).replace(/\s/g, '').includes('60'), '行里有实时瓦数');
    ok(textOf(fleet).replace(/\s/g, '').includes('90W'), '表头有全家合计');
    ok(store.current() === 'default', '进汇总页不改变当前设备指针（请求仍指向真实设备）');

    // 点行 → 切到那台并退出汇总
    const deskRow = rows(fleet).find((r) => r.getAttribute('data-device') === 'desk');
    fleet.fire('click', { target: { closest: (sel) => (sel === '.fleet-row' ? deskRow : null) } });
    ok(store.current() === 'desk', '点汇总里的行 → 切到该设备');
    ok(fleet.hidden === true, '切设备后自动退出汇总页');
    ok(tileById(rail, 'desk').classList.contains('active'), '切换条选中态跟着走');
}

/* ── D. i18n：切语言后切换条与汇总页都跟着换 ───────────────────── */

console.log('\n── D. 中英切换 ──');
{
    const { rail, fleet, env } = B;
    ok(textOf(tileById(rail, '__all__')).includes('全部'), '中文：全部');
    env.i18n.setLocale('en');
    ok(textOf(tileById(rail, '__all__')).includes('All'), '英文：All（切语言后重建瓷砖）');
    ok(textOf(tileById(rail, 'default')).includes('Main charger'),
       '英文：无名 primary 显示 Main charger', textOf(tileById(rail, 'default')));
    ok(!textOf(tileById(rail, 'default')).includes('default'), '英文下同样不泄露内部 id');

    env.sandbox.DeviceBench.enterFleet();
    ok(textOf(fleet).includes('All chargers'), '英文：汇总页标题跟着换');
    ok(rows(fleet).length === 3, '切语言后汇总行数不变');
}

/* ── E. 离线/未知设备：宁可显示"—"，也不能崩 ───────────────────── */

console.log('\n── E. 边界 ──');
{
    const { rail, fleet, store } = boot({
        devices: DEVICES,
        buckets: { default: { connected: true, power: { 1: 5 } } }
    });
    ok(tiles(rail).length === 4, '只有一台有数据时，其它设备照样出现在切换条里');
    eq(textOf(tileById(rail, 'desk').querySelector('.bench-w')).replace(/\s/g, ''), '0W',
       '没数据的设备按 0W 显示（不留空白）');
    store.dispatch({ type: 'init', devices: [DEVICES[0]] });   // 设备被删到只剩一台
    ok(rail.hidden === false, '设备被删到只剩一台 → 切换条留着，收缩成一张卡');
    eq(tiles(rail).length, 1, '只剩那一张');
    ok(!tileById(rail, '__all__'), '"全部"这一面同时消失（汇总已无意义）');
    ok(fleet.hidden !== false, '同时退出/隐藏汇总页');
}

// 手机页那套切换（标题栏下拉列表）用的是**同一份**清单：导出 devices() 就是为了
// 让 ?preview=multi 的伪造设备不只出现在桌面页。
{
    const pv = boot({ preview: true, previewSeed: (id, b) => { if (b) b.seeded = true; } });
    ok(typeof pv.sandbox.DeviceBench.devices === 'function', 'bench 导出 devices()');
    const ids = pv.sandbox.DeviceBench.devices().map((d) => d.id);
    ok(ids.indexOf('pv1') >= 0 && ids.indexOf('pv2') >= 0 && ids.indexOf('pv3') >= 0,
       '预览模式下清单里含三台伪造设备（手机菜单与桌面牌堆共用这一份）', ids.join(','));
    const plain = boot({});
    ok(plain.sandbox.DeviceBench.devices().every((d) => ['default', 'desk', 'bed'].indexOf(d.id) >= 0),
       '非预览时就是真清单，不掺伪造设备');
}

/* ── F. 预览模式（?preview=multi）：单台硬件也能验收"点卡片真的切" ── */

console.log('\n── F. 预览模式 ──');
{
    // 桌面页 app.js 里的 previewSeed 是这个形状（测试里独立写一份，
    // 这样"bench 只负责叫它、不猜桶形状"这条契约才是被验证的那个）
    function previewSeed(id, bucket) {
        const online = id !== 'pv3';
        const spec = { pv1: { 2: 45 }, pv2: { 1: 18 }, pv3: {} }[id] || {};
        bucket.connected = online;
        bucket.authenticated = online;
        bucket.bleConnected = online;
        bucket.previewWh = online ? 5 : 0;
        bucket.settings = { 6: 3 };
        bucket.ports = {};
        [1, 2, 3, 4].forEach((p) => {
            const w = (spec[p] || 0);
            bucket.ports[String(p)] = {
                voltage: w ? 20 : 0, current: w ? w / 20 : 0, power: w,
                active: w > 0, enabled: online, protocol: w ? 'PD' : 'idle'
            };
        });
    }

    const { rail, fleet, store, sandbox, flush } = boot({
        devices: [DEVICES[0]], preview: true, previewSeed
    });
    const realSwitch = sandbox.DeviceStore.switchDevice;
    ok(rail.hidden === false, '单设备 + 预览：切换条出现');
    eq(tiles(rail).length, 5, '预览 = 真机 1 台 + 伪造 3 台 + "全部"（真机留在列表里）');
    ok(textOf(rail).includes('PREVIEW'), '界面上标了 PREVIEW，不会被当成真实数据');

    // 卡片上的读数来自各自的桶（不是 bench 自己编的数字）
    eq(textOf(tileById(rail, 'pv1').querySelector('.bench-w')).replace(/\s/g, ''), '45W',
       'pv1 显示自己桶里的 45W');
    eq(textOf(tileById(rail, 'pv2').querySelector('.bench-w')).replace(/\s/g, ''), '18W',
       'pv2 显示自己桶里的 18W');
    eq(textOf(tileById(rail, 'pv3').querySelector('.bench-w')).replace(/\s/g, ''), '0W',
       '离线的那台是 0W');
    ok(tileById(rail, 'default').classList.contains('active'), '预览默认真停在真机那张（不会一进来就被换掉）');
    ok(!tileById(rail, 'pv1').classList.contains('active'), '伪造设备默认不是选中态');

    // 点任意一张卡 = 把它翻到栈顶（牌堆里被压住的卡也点得到）
    sandbox.__fetches.length = 0;
    const before = store.current();
    clickTile(rail, 'pv1');
    flush(320);
    eq(before, 'default', '点之前停在真机');
    eq(store.current(), 'pv1', '点卡片就切过去了（走真实 store.switchDevice）');
    ok(tileById(rail, 'pv1').classList.contains('active'), '高亮跟着走');
    ok(tileById(rail, 'pv1').classList.contains('is-top'), '被点的那张翻到了栈顶');

    // 再往后一张：滚轮翻牌
    wheel(rail, 60);
    flush(320);
    eq(store.current(), 'pv2', '滚轮翻一张 = 下一台');
    ok(tileById(rail, 'pv2').classList.contains('active'), '翻到哪张，选中态就在哪张');

    // 服务端没有这台设备：请求必须被拦在本地，一个都不能发出去
    eq(sandbox.__fetches.length, 0, '切到伪造设备不产生任何网络请求');
    const resp = store.api('/api/status');
    ok(resp && typeof resp.then === 'function',
       'api() 对伪造设备返回 Promise（与 fetch 同形状，调用方一行不用改）');
    eq(sandbox.__fetches.length, 0, '取状态也不出网');
    // 假响应的内容必须是"这台伪造设备自己的快照"——页面靠它渲染端口/设置卡
    resp.then((r) => r.json()).then((v) => {
        ok(v && v.ok === true, '假响应是 ok 的');
        ok(v.connected === true && v.authenticated === true,
           '连接态取自该伪造设备的桶（页面显示为已连接，与卡片上的灯一致）');
        eq(Object.keys(v.ports || {}).length, 4, '假响应带四个端口（端口卡照常渲染）');
        eq((v.ports['1'] || {}).power, 18, '端口读数就是种子数据（18W）');
    });

    // 预览不污染用户的记忆/链接
    ok(store.url('/api/status').indexOf('device=pv2') >= 0, '预览态下 url() 仍按当前设备拼（便于排查）');
    ok(sandbox.localStorage.getItem('cuktech-device') !== 'pv2',
       '预览设备不写 localStorage（记忆仍是上次的真机，刷新不会落在伪造设备上）');

    // 翻回真机：请求恢复走网络（真机那条路没有被预览污染）
    sandbox.__fetches.length = 0;
    clickTile(rail, 'default');
    flush(320);
    eq(store.current(), 'default', '点卡片翻回真机');
    store.api('/api/status');
    eq(sandbox.__fetches.length, 1, '切回真机后请求真的发出去');

    // 汇总页同样按各桶的真值排序、点行进设备
    clickTile(rail, '__all__');
    ok(fleet.hidden === false, '预览下也能进汇总页');
    eq(rows(fleet).length, 4, '汇总页 4 行 = 真机 + 3 台伪造（离线那台也在）');
    eq(rows(fleet)[0].getAttribute('data-device'), 'pv1', '45W 排最前');
    eq(rows(fleet)[1].getAttribute('data-device'), 'pv2', '18W 其次');
    eq(rows(fleet)[3].getAttribute('data-device'), 'pv3', '0W 排最后');
    const pv1Row = rows(fleet).find((r) => r.getAttribute('data-device') === 'pv1');
    fleet.fire('click', { target: { closest: (sel) => (sel === '.fleet-row' ? pv1Row : null) } });
    eq(store.current(), 'pv1', '点汇总里的伪造设备同样真的切');
    ok(fleet.hidden === true, '并退出汇总页');
}

/* ── G. 两种布局：桌面侧栏竖排堆栈 / 手机顶部横滑条 ───────────────── */

console.log('\n── G. 布局（桌面堆栈 vs 手机横条）──');
{
    const desk = boot({ buckets: { default: { connected: true, power: { 1: 30 } } } });
    const rail = desk.rail;
    ok(rail.querySelectorAll('.bench-stack').length === 1, '桌面：容器是 .bench-stack（侧栏里的一叠）');
    ok(rail.querySelectorAll('.bench-deck').length === 1, '桌面：卡片放在 .bench-deck 里（一副牌，横滑翻牌）');
    ok(rail.querySelectorAll('.wrap').length === 0,
       '桌面：不套 .wrap —— 侧栏已有自己的内边距，套上会多出 20px 缩进');
    ok(rail.querySelectorAll('.bench').length === 0, '桌面：没有横排 .bench 容器');
    const first = rail.querySelector('.bench-tile');
    ok(first.parent.tag === 'div' && first.parent._cls.has('bench-deck'), '桌面：卡片是 .bench-deck 的直接子节点');
}

/* ── I. 牌堆交互：栈序(--depth) / 拖动 / 滚轮 / 方向键 ─────────────── */

console.log('\n── I. 牌堆交互 ──');
{
    const { rail, fleet, store, flush, clock, sandbox } = boot({
        buckets: {
            default: { connected: true, power: { 1: 30 } },
            desk: { connected: true, power: { 2: 60 } },
            bed: { connected: false, power: {} }
        }
    });
    const depth = (id) => tileById(rail, id).style.getPropertyValue('--depth');
    const isTop = (id) => tileById(rail, id).classList.contains('is-top');

    // 栈序 = [当前设备, …其余按注册顺序, 全部]；depth 0 是栈顶
    ok(isTop('default'), '栈顶就是当前设备');
    eq(depth('default'), '0', '栈顶 depth = 0');
    eq(depth('desk'), '1', '下一张在牌堆第 2 层（depth 决定层级，不决定站位）');
    eq(depth('__all__'), '3', '"全部"在牌堆末尾');
    eq(tileById(rail, 'desk').attrs['aria-hidden'], 'true', '下层卡片看不见，也就对读屏隐藏');
    eq(tileById(rail, 'bed').tabIndex, -1, '下层卡片不进 Tab 序列');
    eq(tileById(rail, 'default').tabIndex, 0, '栈顶那张可聚焦');
    // 语义：桌面这副牌不是 tablist（卡片是"打开设置"的按钮），当前那张用 aria-current
    eq(tileById(rail, 'default').attrs['aria-current'], 'true', '可见的那张标成 aria-current');
    eq(tileById(rail, 'desk').attrs['aria-current'], 'false', '看不见的几张不是 current');
    eq(rail.querySelector('.bench-deck').attrs.role, 'group', '牌堆的容器是 group（不是 tablist）');
    ok(tileById(rail, 'default').attrs['data-device'] && !tileById(rail, 'default').attrs.role,
       '卡片是普通按钮：单击 = 打开设置，不是切换 tab');
    // 卡片下面那条翻牌条 = "这里能切换"的全部说明（没有任何文字）：
    // ‹ 进度点 › —— 箭头能点，点说明"共几张、现在第几张"。
    // （点的顺序是固定名单：全部、设备 1、设备 2…，与牌堆的轮转无关，
    //  否则点会跟着翻牌一起动，等于没有信息）
    const pager = rail.querySelectorAll('.bench-pager');
    eq(pager.length, 1, '卡片下面有一条翻牌条');
    const dotEls = pager[0].querySelectorAll('.bench-dot');
    eq(dotEls.length, 4, '一排进度点（全部 + 3 台设备）');
    eq(dotEls.filter((d) => d.classList.contains('is-on')).length, 1, '亮的点只有一个');
    ok(dotEls[1].classList.contains('is-on'),
       '当前设备（primary）亮名单里第 2 颗点（"全部"占第 1 颗）');
    ok(!dotEls[0].classList.contains('is-on'), '"全部"那颗是暗的');
    const navs = pager[0].querySelectorAll('.bench-nav');
    eq(navs.length, 2, '两端各一颗箭头（都能点，就是"翻一张"）');
    eq(navs.map((b) => b.getAttribute('data-step')).join(','), '-1,1', '左 = 上一张，右 = 下一张');
    ok(!navs[0].hidden && !navs[1].hidden, '多张卡时箭头可见');
    ok(textOf(navs[0]) === '', '箭头里没有文字（提示靠图形，不靠文案）');
    eq(navs[0].getAttribute('aria-label'), '上一台', '箭头只给读屏留了 aria 文案');
    // 卡片右端的齿轮：整卡是按钮，点它打开这台的设置（齿轮 = 设置，不用解释）
    ok(tileById(rail, 'default').querySelectorAll('.bench-cog').length === 1,
       '卡片右端有一枚齿轮（"点这张卡进设置"的图形提示）');
    ok(!('title' in tileById(rail, 'default').attrs),
       '卡片上没有 title 文字提示（提示全部交给设计）');

    // 滚轮翻一张 + 冷却（此时栈序 [default, desk, bed, 全部]，下一张是设备）
    wheel(rail, 60);
    flush(320);
    eq(store.current(), 'desk', '滚轮往下 = 翻下一张');
    wheel(rail, 60);
    flush(320);
    eq(store.current(), 'desk', '滚轮有冷却：一次手势不会连翻好几张');

    // 拖动翻牌（阈值取 ChargeLimit.swipeDecision：46px 或卡宽的 22%）
    swipe(rail, -120);
    flush(320);
    eq(store.current(), 'bed', '往左拖过阈值 = 翻下一张');

    // 划牌之后浏览器补的那一下 click 要被吃掉（否则划完立刻弹设置窗）
    const opened = [];
    sandbox.openSettingsModal = (name) => opened.push(name || '');
    clickTile(rail, 'default');
    eq(store.current(), 'bed', '划牌结束补的那一下点按被吞掉（不会误切）');
    eq(opened.length, 0, '也不会顺手弹出设置弹窗');

    // 拖不够阈值：回弹，不切（新手势会解除上一条"吃掉 click"的悬挂状态）
    swipe(rail, 10);
    flush(320);
    eq(store.current(), 'bed', '拖不够阈值 = 回弹，不切');

    // 单击卡片 = 打开这台设备的设置弹窗（右栏那张空卡片已经撤掉，入口在这里）
    clickTile(rail, 'bed');
    flush(320);
    eq(opened.length, 1, '单击卡片 → 弹出设置弹窗');
    ok(opened[0].includes('卧室'), '弹窗标题带的是这台的设备名', opened[0]);
    eq(store.current(), 'bed', '点当前设备那张不切走（只是开设置）');
    ok(isTop('bed'), '它本来就是栈顶');
    eq(depth('bed'), '0', '栈顶 depth 归 0');

    // 点到别台设备的卡（手机横条上就是这样）：先切过去，再弹那台的设置
    clickTile(rail, 'desk');
    flush(320);
    eq(store.current(), 'desk', '点到别台设备：先切过去');
    eq(opened.length, 2, '再弹一次设置');
    ok(opened[1].includes('书桌充电器'), '标题换成那台的名字', opened[1]);

    // 方向键：轮转一整圈回到原处（牌堆是环，不是到头就停）
    const start = store.current();
    const n = 4;                                  // 3 台设备 + "全部"
    for (let i = 0; i < n; i++) {
        key(rail, 'ArrowRight');
        flush(320);
    }
    eq(store.current(), start, '方向键翻满一圈回到同一台');
    ok(fleet.hidden === true, '中途经过"全部"会进汇总页，翻过去又退出');

    // ── 跟手/翻牌全程都不许动透明度 ──
    // 卡片一旦半透明，它和新卡片上的功率数字就会在同一块地方叠着显示（重影）。
    // 所以跟手只跟位置，翻出去也只平移、不渐隐。
    const topId = () => {
        const hit = tiles(rail).find((t) => t.style.getPropertyValue('--depth') === '0');
        return hit ? hit.getAttribute('data-device') : '';
    };
    const top0 = topId();
    rail.fire('pointerdown', { clientX: 200, pointerId: 9 });
    rail.fire('pointermove', { clientX: 120, pointerId: 9 });
    const draggedTile = tileById(rail, top0);
    ok(!!draggedTile.style.transform, '跟手期间卡片跟着手走（有位移）');
    ok(!draggedTile.style.opacity, '跟手期间不写透明度（否则两张卡的数字会重影）');
    // 翻出去的那张也只平移：位移了，尺寸/透明度都不变
    key(rail, 'ArrowRight');
    const flown = tileById(rail, top0);
    ok(!!flown.style.transform && !flown.style.opacity,
       '翻牌时被换下那张只滑出去，不渐隐');
    flush(320);
    rail.fire('lostpointercapture', { pointerId: 9 });
    eq(draggedTile.style.transform, '', '指针被打断后位移归位');
    swipe(rail, -120);
    flush(320);
    ok(topId() !== top0, '打断后新手势照样能翻牌（拖拽状态没卡住）');

    // ── 忙标记的寿命：收尾定时器丢了也不能把整副牌卡死 ──
    // 真实浏览器里后台标签页的 setTimeout 会被节流、冻过的页面甚至不回调；
    // 320ms 收尾一旦不来，deckFlipBusy 会永久为真 —— 表现就是"能滑一次后面动不了"。
    const stallTop = topId();
    key(rail, 'ArrowRight');                    // 收尾定时器故意不推进
    const midTop = topId();
    ok(midTop !== stallTop, '第一次翻牌生效');
    key(rail, 'ArrowRight');
    eq(topId(), midTop, '收尾之前的新手势被忽略（不会连翻两张）');
    clock.t += 1000;                            // 只推时钟：等于"收尾定时器彻底丢了"
    key(rail, 'ArrowRight');
    ok(topId() !== midTop, '忙标记超时自动放开：牌堆不会永久卡死');

    // 拖了一半、连 pointerup 都没收到（手指划出窗口）：同样不能卡住
    flush(320);                                 // 先让上一次翻牌收尾，别和"忙"混在一起
    const lostTop = topId();
    rail.fire('pointerdown', { clientX: 200, pointerId: 11 });
    rail.fire('pointermove', { clientX: 120, pointerId: 11 });
    clock.t += 1000;
    swipe(rail, -120);
    flush(320);
    ok(topId() !== lostTop, '丢掉 pointerup 的拖拽同样会过期，不挡后面的手势');

    // ── 翻牌条上的箭头：桌面上不用滚轮、不用拖，单击也能翻 ──
    const navOf = (step) => rail.querySelectorAll('.bench-nav')
        .find((b) => Number(b.getAttribute('data-step')) === step);
    const from = topId();
    rail.fire('click', { target: navOf(1) });
    flush(320);
    ok(topId() !== from, '单击右侧箭头 = 翻下一张');
    const mid2 = topId();
    rail.fire('click', { target: navOf(-1) });
    flush(320);
    ok(topId() !== mid2, '单击左侧箭头 = 翻回上一张（方向相反）');
}

/* ── H. CSS 契约：一行高的牌堆 + 两栏底对齐（不拉伸） ───────────────── */

console.log('\n── H. 样式契约（index.css）──');
{
    const css = fs.readFileSync(path.join(STATIC, 'index.css'), 'utf8');
    // 行首锚定：浅色主题里也有 `.bench-dot { … }` 这种"带前缀"的规则，
    // 直接 indexOf 会先命中它们（拿到的是浅色覆盖而不是基础规则）。
    const block = (sel) => {
        let i = css.indexOf('\n' + sel + ' {');
        if (i < 0) i = css.indexOf(sel + ' {');
        if (i < 0) return '';
        return css.slice(i, css.indexOf('}', i) + 1);
    };

    const deck = block('.bench-deck');
    ok(deck.length > 0, '.bench-deck 存在');
    ok(/display:\s*grid/.test(deck) && /grid-template-columns:\s*100%/.test(deck),
       '牌堆是 grid 且列宽 100%（所有卡片落在同一格）');
    ok(/overflow:\s*hidden/.test(deck), '划出去的卡滑出即裁掉（overflow: hidden）');
    ok(!/--bench-peek/.test(deck) && !/padding-bottom:\s*calc/.test(deck),
       '不再靠 padding 给下层卡留"露边"高度：只显示一张卡，高度 = 一张卡');
    ok(!/overflow-x:\s*(auto|scroll)/.test(deck) && !/scroll-snap-type/.test(deck),
       '不再是"隐藏滚动条的横滑条"：桌面上那种滑法既没滚动条也没滚轮，滑不动');
    ok(/touch-action:\s*pan-y/.test(deck), '横向手势归翻牌、纵向留给页面滚动');

    const tile = block('.bench-tile');
    ok(/grid-area:\s*1\s*\/\s*1/.test(tile), '所有卡片落在同一格（叠起来，而不是并排）');
    ok(/width:\s*100%/.test(tile), '卡片占满整行（不再按 --depth 缩窄）');
    // 功率脊是绝对定位的：包含块必须是这张卡，否则它会挂到 .bench-stack 上，
    // 跑到下面的翻牌条底下（真实踩过的 bug）
    ok(/position:\s*relative/.test(tile), '卡片自己是定位元素（功率脊贴在卡内底边）');
    const spineCss = block('.bench-spine');
    ok(/position:\s*absolute/.test(spineCss) && /bottom:\s*\d+px/.test(spineCss),
       '功率脊绝对定位在卡内的底边');
    ok(!/translateY\(calc\(var\(--depth/.test(tile),
       '不再按 --depth 往下错开：只有一张卡，没有"露在下面的边"');
    ok(!/flex-wrap/.test(deck), '牌堆不换行：换行就等于又变成多行高');

    // 只显示栈顶那张：下层必须真的看不见（不是"露一条边"）
    const hidden = block('.bench-tile:not(.is-top)');
    ok(/opacity:\s*0/.test(hidden) && /visibility:\s*hidden/.test(hidden),
       '非栈顶卡片完全藏起来（opacity 0 + visibility hidden）');
    ok(!/opacity/.test(tile.split('transition:')[1] || ''),
       '卡片的过渡里没有 opacity：渐隐会让两张卡的功率数字叠成重影');
    ok(/is-shifting \.bench-tile:not\(\.is-top\)/.test(css),
       '拖动/翻牌期间把"下一张"放出来（否则拖开是一块空白）');

    // ── 卡面必须是"页面里的另一张卡"：同一枚卡面令牌，而不是分段控件的灰轨道 ──
    // 深色主题下 --seg-track 是 16% 的灰，放进右栏和上下的卡片明显不是一家人；
    // 而且它是半透明的，叠牌时下层会透上来（重影）。--card-bg 两套主题都不透明。
    ok(/background:\s*var\(--card-bg\)/.test(tile), '卡面用 --card-bg（与页面其他卡片同一枚令牌）');
    ok(/border:\s*1px solid var\(--card-border\)/.test(tile), '描边用 --card-border');
    ok(/border-radius:\s*var\(--r-card\)/.test(tile), '圆角用 --r-card（和其他卡片一致）');
    ok(/box-shadow:\s*var\(--shadow-card\)/.test(tile), '投影用 --shadow-card');
    ok(!/--seg-track/.test(tile) && !/--seg-thumb/.test(tile),
       '卡面不再借用分段控件的 --seg-* 色（那是给控件轨道用的灰）');
    const bgVals = [...css.matchAll(/--card-bg:\s*([^;]+);/g)].map((m) => m[1].trim());
    ok(bgVals.length >= 2 && bgVals.every((v) => /^#[0-9a-f]{3,8}$/i.test(v)),
       '--card-bg 在两套主题下都是不透明色（叠牌时不会透底）', bgVals.join(' / '));
    const hoverTile = block('.bench-tile:hover');
    ok(/background:\s*var\(--card-hover-bg\)/.test(hoverTile),
       '悬停用 --card-hover-bg（与其他卡片的 hover 同一套）');
    // 可见的只有"当前"那张，所以选中态不该再换底色/描边（换了两张卡看起来就不一样了）
    const activeTile = block('.bench-tile.active');
    ok(!/background/.test(activeTile) && !/border-color/.test(activeTile),
       '选中态不换底色、不换描边（只有一张卡可见，换了反而与别的卡片不一致）');
    ok(/cursor:\s*grab/.test(activeTile), '选中态保留"可拖动翻牌"的指针形状');
    ok(/border-style:\s*dashed/.test(block('.bench-tile.bench-all')),
       '"全部"用虚线描边区分（它是汇总卡，不是某台设备）');

    // ── 两套主题都不再需要"专门的卡面覆盖" ──
    // 卡面走 .card 的令牌之后，深浅两套主题自动跟着走；以前那几段浅色覆盖
    // （白卡 + 中性描边 + 额外投影）正好也是"卡片与别处不一致"的来源之一。
    ok(!/html\[data-appearance="light"\] \.bench-deck/.test(css),
       '没有浅色专用的卡面覆盖：令牌自己就是白卡（--card-bg: #fff）');
    ok(!/--accent/.test(tile) && !/--accent/.test(hoverTile),
       '卡面与悬停都不用品牌青（浅色主题下白底配青边会像"选中失败"的提示框）');

    // ── "这里能切换 / 能开设置"全靠设计，不靠文字 ──
    const pagerCss = block('.bench-pager');
    ok(pagerCss.length > 0, '翻牌条有自己的样式');
    const navCss = block('.bench-nav');
    ok(/cursor:\s*pointer/.test(navCss), '箭头是可点的按钮');
    ok(/opacity:\s*0?\.\d/.test(navCss), '箭头静止时也看得见（不是只有 hover 才出现）');
    ok(/\.bench-nav\[hidden\]\s*\{[^}]*display:\s*none/.test(css), '只有一张卡时箭头隐藏');
    ok(/\.bench-stack:hover \.bench-nav/.test(css), '指针落在整副牌上时箭头提亮');
    ok(/\.bench-nav:hover/.test(css), '箭头自己也有 hover 态（可点感）');
    const dotCss = block('.bench-dot');
    ok(/width:\s*5px/.test(dotCss), '进度点 5px');
    ok(/\.bench-dot\.is-on\s*\{[^}]*width:\s*12px/.test(css),
       '当前那颗拉长成小胶囊（"第几张"读起来比"哪颗亮"快）');
    const cogCss = block('.bench-cog');
    ok(cogCss.length > 0 && /opacity:\s*0?\.\d/.test(cogCss), '卡片右端的齿轮静止时是暗的');
    ok(/pointer-events:\s*none/.test(cogCss),
       '齿轮本身不吃点击（整张卡就是它的点击区）');
    ok(/\.bench-tile:hover \.bench-cog/.test(css), '悬停卡片时齿轮提亮并转动（"我能点"）');
    ok(/transform:\s*rotate\(/.test(css.slice(css.indexOf('.bench-tile:hover .bench-cog'),
                                             css.indexOf('.bench-tile:hover .bench-cog') + 260)),
       '转动是那一档 hover 里的事，不影响布局');
    ok(/\.bench-tile:focus-visible \.bench-cog/.test(css), '键盘聚焦时同样提亮');
    ok(!/\.bench-tile\[title\]/.test(css), '提示不靠 title 属性');

    // 图标来自页面里的那份 sprite（不额外引资源）
    const indexHtml = fs.readFileSync(path.join(STATIC, '../index.html'), 'utf8');
    ok(indexHtml.indexOf('id="i-chevron-left"') > 0 && indexHtml.indexOf('id="i-chevron-right"') > 0,
       'index.html 的图标 sprite 里有左右箭头');

    // 手机那条横滑条已经撤掉：bench 不再有"页/模式"分支，只画侧栏牌堆 + 汇总页
    const benchSrc = fs.readFileSync(path.join(STATIC, 'device-bench.js'), 'utf8');
    ok(!/opts\.mode/.test(benchSrc), 'bench 不再按页面模式分叉（手机改用标题栏下拉列表）');
    ok(!/bench-scroll/.test(benchSrc), '不再生成手机那条横滑条');

    // 瓦数字号：卡片的主角，要比设备名大一档（上一版 13px 和名字一样大）
    const nameBlock = block('.bench-name');
    const wattBlock = block('.bench-w b');
    const px = (b) => Number((b.match(/font-size:\s*([\d.]+)px/) || [])[1]);
    ok(px(wattBlock) >= 17 && px(wattBlock) > px(nameBlock),
       '卡片上的功率数字明显大于设备名（' + px(wattBlock) + 'px > ' + px(nameBlock) + 'px）');
    // 两栏底部对齐：靠均摊间距，不是把卡片拉高
    const mq = css.slice(css.indexOf('@media (min-width: 1024px)'));
    const mqBlock = mq.slice(0, mq.indexOf('\n}'));
    ok(/justify-content:\s*space-between/.test(mqBlock),
       '宽屏下两栏用 space-between 把富余高度摊进间距（最后一张卡贴栏底）');
    ok(!/\.col-(main|side)\s*\{[^}]*flex:\s*1/.test(mqBlock.replace(/\s+/g, ' ')),
       '没有给栏里塞 flex:1 去拉伸卡片');
    ok(!/\.card\b[^{]*\{[^}]*align-self:\s*stretch/.test(mqBlock), '没有把卡片 align-self 拉满');
}

// 预览那段用了 .then（假响应是 Promise）：先让微任务排空，再收尾
setTimeout(() => {
    // 单设备探测：延迟 1500ms 才发，等 SSE init 落地（多设备页面因此不会白问）
    ok(PROBE.sandbox.__fetches.indexOf('/api/devices') < 0,
       '探测不是当场发的：先等 SSE init（多设备页面就不会白问一次）');
    PROBE.flush(2000);
    ok(PROBE.sandbox.__fetches.indexOf('/api/devices') >= 0,
       '单设备时延迟后向 /api/devices 探一次（SSE 里没有 devices 清单）');
    NO_PROBE.flush(2000);
    ok(NO_PROBE.sandbox.__fetches.indexOf('/api/devices') < 0,
       '多设备时清单一到就取消探测：一个多余请求都不发');
    // 探测的响应是 Promise：等它的 .then 链走完再收尾判断
    // （多排几轮微任务：fetch → json() → setDevices 是三跳，链比断言长）
    const drain = (n) => (n <= 0 ? Promise.resolve() : Promise.resolve().then(() => drain(n - 1)));
    drain(8).then(() => {
        ok(textOf(tileById(PROBE.rail, 'default')).includes('客厅充电器'),
           '探回来之后卡片用真实设备名', textOf(tileById(PROBE.rail, 'default')));
        ok(PROBE.sandbox.__fetches.filter((u) => u === '/api/devices').length === 1,
           '探测只发一次（不会变成每秒一次的轮询）');
        console.log(`\n${failed ? 'FAILED: ' + failed + ' check(s)' : 'All ' + passed + ' device-bench checks passed.'}`);
        process.exit(failed ? 1 : 0);
    });
}, 0);
