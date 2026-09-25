#!/usr/bin/env node
/**
 * Behavioural test for the phone page's stacked charge-limit deck.
 *
 * Why this exists: the deck (4 port cards overlapping in one grid cell, swipe to
 * switch) is pure DOM + pointer-gesture code, and there is no browser in this
 * project's toolchain — a mistyped depth or a flipped sign would ship silently.
 * So the deck gets a small hand-written DOM: real elements for the charge-limit
 * ids, a permissive stub for everything else phone.js touches at load. That is
 * enough to drive renderChargeLimit / showChargeLimitPort / the pointer handlers
 * and assert on the resulting DOM state.
 *
 * What it pins down:
 *   - markup/classes the stylesheet depends on (depth is written as --depth)
 *   - only the front card is reachable (inert + aria-hidden on the others)
 *   - swiping past the threshold rotates the deck; below it snaps back
 *   - vertical drags and drags starting on an input are not hijacked
 *   - the click that follows a swipe is swallowed (a swipe must not press a chip)
 *   - the flown-out card lands silently at the back of the stack
 *   - the front card survives a locale re-render
 *
 * Usage: node tests/js/charge_limit_deck_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC = path.resolve(__dirname, '../../web/static');
const PORT_KEYS = ['c1', 'c2', 'c3', 'a'];
const FLIP_SETTLE_MS = 360;   // 略大于 phone.css 里 0.3s 的过渡 + JS 的 320ms 收尾

// ── 最小 DOM ──

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

function makeStyle() {
    const props = {};
    return {
        setProperty(k, v) { props[k] = String(v); },
        getPropertyValue(k) { return props[k] || ''; },
        removeProperty(k) { delete props[k]; },
    };
}

/** 宽松替身：phone.js 加载期会碰到几十个别的元素，它们与卡组无关。 */
function makeEl() {
    const style = makeStyle();
    return new Proxy(function el() {}, {
        get(t, p) {
            if (p === 'style') return style;
            if (p === 'dataset') return {};
            if (p === 'classList') return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
            if (p === 'children' || p === 'options') return [];
            return makeNoop();
        },
        set() { return true; },
        apply() { return makeNoop(); },
        construct() { return makeEl(); },
    });
}

/** 卡组相关的元素用真对象：要能断言 classList / --depth / 事件处理。 */
function makeTrackedEl(id) {
    const classes = new Set();
    const attrs = {};
    const handlers = {};
    return {
        id,
        innerHTML: '',
        textContent: '',
        value: '',
        offsetWidth: 330,
        offsetHeight: 200,
        inert: false,
        dataset: {},
        style: makeStyle(),
        classList: {
            add: c => classes.add(c),
            remove: c => classes.delete(c),
            contains: c => classes.has(c),
            toggle(c, force) {
                const on = force === undefined ? !classes.has(c) : !!force;
                if (on) classes.add(c); else classes.delete(c);
                return on;
            },
        },
        setAttribute(k, v) { attrs[k] = String(v); },
        getAttribute(k) { return k in attrs ? attrs[k] : null; },
        removeAttribute(k) { delete attrs[k]; },
        addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        // 测试用后门：把事件直接派发给注册的处理函数
        fire(type, ev) { (handlers[type] || []).forEach(fn => fn(ev)); },
        _classes: classes,
        _attrs: attrs,
    };
}

function buildSandbox() {
    const els = {};
    for (const key of PORT_KEYS) {
        for (const prefix of ['limitCard_', 'limitDot_', 'limitStatus_', 'limitBar_',
                              'limitProgress_', 'limitWh_', 'limitMode_']) {
            els[prefix + key] = makeTrackedEl(prefix + key);
        }
    }
    els.chargeLimitDeck = makeTrackedEl('chargeLimitDeck');
    els.chargeLimitDeck.classList.add('no-anim');   // phone.html 里的初始类
    els.chargeLimitDots = makeTrackedEl('chargeLimitDots');

    const sandbox = {
        console,
        Intl,
        setTimeout,
        clearTimeout,
        setInterval: () => 0,      // 页面上的轮询不需要在测试里真的跑
        clearInterval: () => {},
        Promise, JSON, Math, Date, RegExp, String, Number, Boolean, Object, Array,
        isNaN, parseFloat, parseInt, Error,
        fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, limits: {} }) }),
        EventSource: function () { this.close = function () {}; },
        Chart: function () { return { data: { labels: [], datasets: [] }, options: { scales: {} }, update() {}, destroy() {} }; },
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        navigator: { language: 'zh-CN' },
        location: { origin: 'http://charger.local' },
        getComputedStyle: () => ({ getPropertyValue: () => '' }),
        addEventListener() {},
        removeEventListener() {},
    };
    sandbox.window = sandbox;
    sandbox.document = {
        documentElement: makeEl(),
        body: makeEl(),
        head: makeEl(),
        title: '',
        readyState: 'complete',
        activeElement: null,
        getElementById: id => els[id] || makeEl(),
        querySelector: () => makeEl(),
        querySelectorAll: () => [],
        createElement: () => makeEl(),
        addEventListener() {},
    };
    vm.createContext(sandbox);
    // 真实 i18n 运行时：文案与 t() 行为和页面一致（phone.js 加载期就会渲染一次）
    for (const f of ['locales/zh-CN.js', 'locales/en.js', 'i18n.js',
                     'charge_history.js', 'charge_limit.js', 'phone.js']) {
        vm.runInContext(fs.readFileSync(path.join(STATIC, f), 'utf8'), sandbox, { filename: f });
    }
    sandbox.__els = els;
    return sandbox;
}

/** 读 vm 里的顶层绑定（runInContext 共享同一份全局词法环境）。 */
function peek(ctx, expr) { return vm.runInContext(expr, ctx); }

let failed = 0;
let passed = 0;
function check(cond, label, detail) {
    if (cond) { passed++; console.log(`  ok   ${label}`); }
    else { failed++; console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`); }
}
function eq(actual, expected, label) {
    check(JSON.stringify(actual) === JSON.stringify(expected), label,
          `got ${JSON.stringify(actual)} want ${JSON.stringify(expected)}`);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

/** 卡组的核心不变量：每张卡的 --depth 必须等于它在栈序里的位置。 */
function checkDepthsMatchOrder(label) {
    const o = order();
    eq(PORT_KEYS.map(k => depthOf(k)), PORT_KEYS.map(k => String(o.indexOf(k))), label);
}

const ctx = buildSandbox();
const els = ctx.__els;
const depthOf = key => els['limitCard_' + key].style.getPropertyValue('--depth');
const transformOf = key => String(els['limitCard_' + key].style.transform || '');
const order = () => JSON.parse(peek(ctx, 'JSON.stringify(limitOrder)'));
const top = () => order()[0];
const swipe = (dx, dy) => {
    const target = { tagName: 'DIV' };
    const x0 = 200;
    els.chargeLimitDeck.fire('pointerdown', { pointerId: 7, clientX: x0, clientY: 100, target });
    els.chargeLimitDeck.fire('pointermove', { pointerId: 7, clientX: x0 + dx, clientY: 100 + (dy || 0), target });
    els.chargeLimitDeck.fire('pointerup', { pointerId: 7, clientX: x0 + dx, clientY: 100 + (dy || 0), target });
};

(async function main() {
    console.log('\n-- 渲染：4 张内层小卡 + 4 个指示点 --');
    const deckHtml = els.chargeLimitDeck.innerHTML;
    const dotsHtml = els.chargeLimitDots.innerHTML;
    check(PORT_KEYS.every(k => deckHtml.includes(`id="limitCard_${k}"`)), '4 张卡片都渲染了');
    check(PORT_KEYS.every(k => deckHtml.includes(`class="charge-limit-card ${k}"`)),
          '卡片带 .charge-limit-card 与端口类（CSS 靠后者取端口墨色）');
    check(PORT_KEYS.every(k => dotsHtml.includes(`id="limitDot_${k}"`)), '4 个指示点都渲染了');
    check(dotsHtml.includes('class="charge-limit-dotnav'), '指示点用 .charge-limit-dotnav');
    check(dotsHtml.includes('aria-label='), '指示点带 aria-label（读屏能听到端口与状态）');
    check(!/style="[^"]*(background|color)\s*:/i.test(deckHtml), '卡片 HTML 无内联颜色');

    console.log('\n-- 叠放：depth / z-index / 只有栈顶可交互 --');
    eq(depthOf('c1'), '0', 'c1 初始在栈顶（--depth 0）');
    eq([depthOf('c2'), depthOf('c3'), depthOf('a')], ['1', '2', '3'], '其余按序下沉 1/2/3 层');
    eq([els.limitCard_c1.style.zIndex, els.limitCard_c2.style.zIndex,
        els.limitCard_c3.style.zIndex, els.limitCard_a.style.zIndex], ['20', '19', '18', '17'],
       'z-index 依次递减');
    checkDepthsMatchOrder('4 张卡的 --depth 与栈序一一对应');
    eq(els.chargeLimitDeck.style.getPropertyValue('--limit-count'), '4',
       'deck 声明 --limit-count（CSS 用它算露边内边距）');
    check(els.limitCard_c1.inert === false && els.limitCard_c2.inert === true,
          '栈顶 inert=false，下层 inert=true（否则能 Tab 进看不见的表单）');
    eq([els.limitCard_c1.getAttribute('aria-hidden'), els.limitCard_c2.getAttribute('aria-hidden')],
       ['false', 'true'], 'aria-hidden 与 inert 同步');
    check(!els.chargeLimitDeck._classes.has('no-anim'),
          '首帧后摘掉 .no-anim（否则翻页动画会被一并关掉）');

    console.log('\n-- 指示点状态跟限额走 --');
    ctx.ChargeLimit.state.limits = {
        c1: { wh: 0, mode: 'once', session_wh: 0, is_charging: false, fired: false },
        c2: { wh: 30, mode: 'always', session_wh: 12, is_charging: true, fired: false },
        c3: { wh: 20, mode: 'once', session_wh: 20, is_charging: false, fired: true },
        a: { wh: 0, mode: 'once', session_wh: 0, is_charging: false, fired: false },
    };
    peek(ctx, 'updateChargeLimitUI()');
    check(!els.limitDot_c1._classes.has('is-set'), '未设限额：空心');
    check(els.limitDot_c2._classes.has('is-set') && !els.limitDot_c2._classes.has('is-fired'),
          '已设限额：实心');
    check(els.limitDot_c3._classes.has('is-set') && els.limitDot_c3._classes.has('is-fired'),
          '已触发关断：额外警示样式');
    check(els.limitDot_c1._classes.has('is-current') && !els.limitDot_c2._classes.has('is-current'),
          '只有栈顶那个点是 is-current');
    check(els.limitDot_c2.getAttribute('aria-label').length > 1, 'aria-label 带上了状态文案');

    console.log('\n-- 点指示点直接跳到目标端口 --');
    const flown = top();                        // 跳页时被换下的是旧栈顶
    peek(ctx, "showChargeLimitPort('c3')");     // [c1,c2,c3,a] -> 转 2 格
    eq(order(), ['c3', 'a', 'c1', 'c2'], 'c3 被转到栈顶（轮转，不是重排）');
    eq(depthOf('c3'), '0', 'c3 拿到 --depth 0');
    eq(depthOf('c1'), '2', '被换下的 c1 落到 depth 2');
    eq(els.limitDot_c3.getAttribute('aria-current'), 'true', '指示点跟随栈顶');

    console.log('\n-- 动画收尾：飞出的卡片静默落回牌堆里它那一格 --');
    await sleep(FLIP_SETTLE_MS);
    eq(flown, 'c1', '被换下的是旧栈顶 c1');
    checkDepthsMatchOrder('跳页收尾后 --depth 仍与栈序一致');
    check(transformOf(flown) === '', '清掉了内联位移（不残留 translate）', transformOf(flown));

    console.log('\n-- 手势：横向划过阈值 = 翻页 --');
    els.chargeLimitDeck.fire('pointerdown', { pointerId: 1, clientX: 200, clientY: 100, target: { tagName: 'DIV' } });
    els.chargeLimitDeck.fire('pointermove', { pointerId: 1, clientX: 110, clientY: 104, target: { tagName: 'DIV' } });
    check(transformOf(top()).indexOf('translate(-90px') === 0,
          '跟手期间栈顶卡跟着位移', transformOf(top()));
    els.chargeLimitDeck.fire('pointerup', { pointerId: 1, clientX: 110, clientY: 104, target: { tagName: 'DIV' } });
    eq(order(), ['a', 'c1', 'c2', 'c3'], '左滑 90px（超过阈值）向后翻一格');
    eq(depthOf('a'), '0', '新栈顶就位');
    check(String(els.limitCard_c3.style.opacity) === '0', '被换下的卡片飞出去了（透明度归零）');
    await sleep(FLIP_SETTLE_MS);
    checkDepthsMatchOrder('翻页收尾后 --depth 仍与栈序一致');
    check(order()[PORT_KEYS.length - 1] === 'c3', '单步翻页时，被换下的卡片落到牌堆末位');

    console.log('\n-- 手势：没过阈值 = 回弹，不翻页 --');
    const before = order();
    swipe(-40, 0);
    eq(order(), before, '位移 40px 未达阈值，栈序不变');
    check(transformOf(top()) === '', '回弹：内联位移被清掉', transformOf(top()));

    console.log('\n-- 手势：纵向 / 输入框起手都不抢 --');
    swipe(-10, 160);
    eq(order(), before, '纵向为主的拖动不翻页（交还页面滚动）');
    const inputTarget = { tagName: 'INPUT' };
    els.chargeLimitDeck.fire('pointerdown', { pointerId: 4, clientX: 200, clientY: 100, target: inputTarget });
    els.chargeLimitDeck.fire('pointermove', { pointerId: 4, clientX: 40, clientY: 100, target: inputTarget });
    els.chargeLimitDeck.fire('pointerup', { pointerId: 4, clientX: 40, clientY: 100, target: inputTarget });
    eq(order(), before, '从输入框起手的拖动不翻页（那是原生编辑手势）');

    console.log('\n-- 划卡末尾的 click 必须被吃掉（但抖动不算划卡） --');
    let stopped = 0;
    const click = () => els.chargeLimitDeck.fire('click', { stopPropagation() { stopped++; }, preventDefault() {} });
    const outgoing = top();
    swipe(-110, 0);
    eq(order()[3], outgoing, '划动生效，被换下的卡片排到末位');
    click();
    eq(stopped, 1, '紧跟划卡的那次 click 被拦下（否则会顺手按到快捷值按钮）');
    click();
    eq(stopped, 1, '只拦一次，后续正常点击不受影响');
    await sleep(FLIP_SETTLE_MS);
    check(String(els['limitCard_' + outgoing].style.opacity || '') === '',
          '飞出的卡片收尾时清掉了内联透明度');

    // 手指抖了 10px 仍然是"点"，不能把设限额这个主操作静默吞掉
    let jitterStopped = 0;
    els.chargeLimitDeck.fire('pointerdown', { pointerId: 8, clientX: 200, clientY: 100, target: { tagName: 'BUTTON' } });
    els.chargeLimitDeck.fire('pointermove', { pointerId: 8, clientX: 192, clientY: 100, target: { tagName: 'BUTTON' } });
    els.chargeLimitDeck.fire('pointerup', { pointerId: 8, clientX: 192, clientY: 100, target: { tagName: 'BUTTON' } });
    els.chargeLimitDeck.fire('click', { stopPropagation() { jitterStopped++; }, preventDefault() {} });
    eq(jitterStopped, 0, '抖动 8px 仍算点击，click 不被拦截');
    check(String(els['limitCard_' + top()].style.opacity || '') === '',
          '抖动结束后栈顶卡透明度已复位');

    console.log('\n-- 被系统打断（pointercancel）一律回弹 --');
    const beforeCancel = order();
    // 真实事件对象都带 type（onLimitPointerUp 靠它区分 up / cancel）
    els.chargeLimitDeck.fire('pointerdown', { type: 'pointerdown', pointerId: 9, clientX: 200, clientY: 100, target: { tagName: 'DIV' } });
    els.chargeLimitDeck.fire('pointermove', { type: 'pointermove', pointerId: 9, clientX: 60, clientY: 100, target: { tagName: 'DIV' } });
    els.chargeLimitDeck.fire('pointercancel', { type: 'pointercancel', pointerId: 9, clientX: 60, clientY: 100, target: { tagName: 'DIV' } });
    eq(order(), beforeCancel, 'pointercancel 不翻页（浏览器接管的滚动不该被当成划卡）');
    check(String(els['limitCard_' + top()].style.opacity || '') === '',
          '回弹后栈顶卡透明度复位');

    console.log('\n-- 切语言重建 DOM 后仍停在原来那张 --');
    const front = top();
    peek(ctx, "I18N.setLocale('en'); I18N.setLocale('zh-CN')");
    eq(order()[0], front, '重建后栈顶不变（用户不会被弹回第一个端口）');
    checkDepthsMatchOrder('重建后 --depth 与栈序一致');
    check(els.chargeLimitDeck.innerHTML.includes('charge-limit-card'), '重建后卡片仍在');
    check(!els.chargeLimitDeck._classes.has('no-anim'), '重建后同样摘掉 .no-anim');

    console.log(`\n${passed} passed, ${failed} failed`);
    process.exit(failed === 0 ? 0 : 1);
})();
