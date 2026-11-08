#!/usr/bin/env node
/**
 * heatmap.js 的契约测试。
 *
 * 热力图最容易错的三类：
 *   1. 周对齐 / 列数（写死 53 会少一列；起点补到周一、终点补到周日）；
 *   2. 分级与"无数据"（起点之前必须是空心，不是 0 级色）；
 *   3. 取数收敛（切 tab + 轮询 + 切设备同时触发时只能发一路；旧设备的迟到响应不能覆盖新图）。
 *
 * 纯函数与渲染分开断言：布局/分级不依赖 DOM，渲染用最小 DOM 记账。
 *
 * Usage: node tests/js/heatmap_test.js
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

function makeSandbox() {
    const els = new Map();
    const created = [];
    const pane = { classList: { _set: new Set(), add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); }, contains(c) { return this._set.has(c); } } };
    const fetches = [];
    function makeEl(id) {
        return {
            id, innerHTML: '', textContent: '', hidden: false,
            style: {}, dataset: {}, parentNode: null,
            offsetWidth: 121, offsetHeight: 50,
            classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
            setAttribute() {}, getAttribute() { return null; },
            addEventListener(type, fn) { (this._h = this._h || {})[type] = fn; },
            appendChild(child) { child.parentNode = this; return child; },
            contains() { return true; },
            getBoundingClientRect() {
                return { left: 100, top: 500, width: 10, height: 10,
                         right: 110, bottom: 510 };
            },
        };
    }
    const document = {
        documentElement: makeEl('html'),
        body: makeEl('body'),
        head: makeEl('head'),
        readyState: 'complete',
        getElementById(id) {
            if (!els.has(id)) els.set(id, makeEl(id));
            return els.get(id);
        },
        querySelector(sel) { return sel.indexOf('calendar') >= 0 ? pane : null; },
        querySelectorAll() { return []; },
        createElement() { const e = makeEl(''); created.push(e); return e; },
        addEventListener() {},
        dispatchEvent() { return true; },
    };
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        Intl, URL, Promise, JSON, Math, Date, RegExp, String, Number, Boolean,
        Object, Array, isNaN, parseFloat, parseInt, Error, Map, Set,
        navigator: { language: 'zh-CN' },
        CustomEvent: class CustomEvent { constructor(type) { this.type = type; } },
        dispatchEvent() { return true; },
        innerWidth: 1200,
        document,
        DeviceStore: {
            api(url) {
                fetches.push(String(url));
                return Promise.resolve({
                    status: 200,
                    json: async () => sandbox.__payload,
                });
            },
        },
        __fetches: fetches,
        __els: els,
        __created: created,
        __pane: pane,
    };
    sandbox.window = sandbox;
    const ctx = vm.createContext(sandbox);
    const run = (file) => vm.runInContext(fs.readFileSync(path.join(STATIC, file), 'utf8'), ctx);
    run('locales/zh-CN.js');
    run('locales/en.js');
    run('i18n.js');
    run('heatmap.js');
    vm.runInContext("I18N.setLocale('zh-CN');", ctx);
    return ctx;
}

function payload() {
    return {
        range: { start: '2025-11-09', end: '2026-11-08' },
        data_since: '2026-11-01',
        unit: 'Wh',
        total_wh: 92.61,
        active_days: 1,
        session_count: 26,
        peak: { date: '2026-11-07', wh: 92.61 },
        levels: [0.5, 1.2, 3.4],
        days: [
            { date: '2026-11-07', wh: 92.61, sessions: 26, peak_w: 88.0 },
            { date: '2026-11-08', wh: 0.4, sessions: 1, peak_w: 5.0 },
        ],
    };
}

async function main() {
    const ctx = makeSandbox();
    const P = ctx.HeatmapPure;
    const H = ctx.Heatmap;

    // ── 纯函数 ──
    eq(P.dayKey(new Date(2026, 10, 8)), '2026-11-08', 'dayKey 本地日期');
    eq(P.dayKey(P.parseDay('2026-11-08')), '2026-11-08', 'parseDay 按本地日期解析');
    ok(P.parseDay('nope') === null && P.parseDay(null) === null,
       'parseDay 拒绝非法输入');
    eq(P.formatWh(999.9), '999.9 Wh', 'formatWh < 1kWh');
    eq(P.formatWh(1000), '1.00 kWh', 'formatWh = 1kWh');
    eq(P.formatWh(3245.6), '3.25 kWh', 'formatWh > 1kWh');

    const levels = [1, 2, 3];
    eq([0, 0.5, 1.5, 2.5, 3.5, 10].map((v) => P.classify(v, levels)),
       [0, 1, 2, 3, 4, 4], 'classify 五档');

    // 尺寸：格子要铺满卡片宽度（这是"卡片不被留白撑空"的关键一步）。
    // 697px / 53 列是 1440 桌面实测的可用宽度。
    const desk = P.fitCell(697, 101, 53);
    ok(Math.abs(desk.cell * 53 + desk.gap * 52 - 697) < 1.5,
       `桌面：53 列正好铺满 697px（cell ${desk.cell} gap ${desk.gap}）`);
    ok(desk.gap === 3 && desk.cell > 10 && desk.cell <= 11,
       '桌面：格子放大到 10–11px、缝 3px', JSON.stringify(desk));
    ok(desk.radius >= 2 && desk.radius <= 3.5, '圆角随格子等比', String(desk.radius));
    ok(P.fitCell(900, 60, 53).cell <= P.fitCell(900, 400, 53).cell,
       '高度不够时格子按高度回退（不顶破固定高度的面板）');
    ok(P.fitCell(900, 60, 53).cell * 7 <= 60, '高度封顶后 7 行塞得进预算');
    // 卡内不许出现滚动条：再窄也要把 53 列塞进可用宽度里
    for (const w of [304, 420, 520, 640]) {
        const f = P.fitCell(w, 101, 53);
        ok(f.cell * 53 + f.gap * 52 <= w + 0.6,
           `窄屏 ${w}px：53 列塞得进（cell ${f.cell} gap ${f.gap}）`);
    }
    eq(P.fitCell(304, 101, 53).gap, 1, '窄屏先把缝压到 1px，而不是让格子缩成沙');
    ok(P.fitCell(304, 101, 53).cell < 8, '窄屏格子允许小于 8px（不横向滚动）');
    ok(P.fitCell(120, 101, 53).cell >= 3.5, '极端窄时守住 3.5px 下限');
    eq(P.fitCell(0, 0, 53), { cell: 10, gap: 2, radius: 2.6 },
       '量不到宽度（面板隐藏）时给中性尺寸');

    const data = payload();
    const layout = P.buildLayout(data, new Date(2026, 10, 8));
    eq(layout.start, '2025-11-09', '布局起点 = end - 364 天');
    eq(layout.end, '2026-11-08', '布局终点 = end');
    ok(layout.cols === 53 || layout.cols === 54, `列数推导为 53/54（实际 ${layout.cols}）`);
    eq(layout.cells.length, layout.cols * 7, '格子数 = 列 × 7');
    eq(layout.cells[0].date, '2025-11-03', '第一格是起点所在周的周一');
    eq(layout.cells[0].inRange, false, '周一早于起点 → 不可见占位');
    eq(layout.months.length, 13, '滚动一年跨 13 个月标签（首尾同月各一次）');

    const byDate = {};
    layout.cells.forEach((c) => { byDate[c.date] = c; });
    eq(byDate['2026-11-07'].level, 4, '92.61 Wh → lv4');
    eq(byDate['2026-11-08'].level, 1, '0.4 Wh → lv1');
    eq(byDate['2026-11-06'].level, 0, '无记录 → lv0');
    eq(byDate['2026-10-31'].noData, true, 'data_since 之前 → noData');
    eq(byDate['2026-10-31'].level, -1, 'noData 不参与分级');
    eq(byDate['2025-11-03'].inRange, false, '起点之前不可见');

    const fp1 = P.layoutFingerprint(data);
    const data2 = payload();
    data2.days[0].wh = 99;
    ok(fp1 !== P.layoutFingerprint(data2), '数值变化会改变指纹');
    const data3 = payload();
    data3.range = { start: '2025-11-09', end: '2026-11-09' };
    ok(fp1 !== P.layoutFingerprint(data3), 'range.end 变化会改变指纹');

    // ── 渲染 + 取数 ──
    ctx.__payload = payload();
    const before = ctx.__fetches.length;
    H.reset();
    eq(ctx.__els.get('heatmapGrid').innerHTML, '', 'reset 清空网格');
    ok(ctx.__pane.classList.contains('is-empty'), 'reset 后回到空态');

    const p1 = H.refresh();
    const p2 = H.refresh();            // 同一设备、同一 seq → 复用
    ok(p1 === p2, 'refresh 单飞：并发调用复用同一个 Promise');
    await p1;
    eq(ctx.__fetches.length, before + 1, '单飞只发一路请求');

    const grid = ctx.__els.get('heatmapGrid');
    const cellCount = (grid.innerHTML.match(/<i class="hm-cell/g) || []).length;
    eq(cellCount, layout.cells.length, '渲染出全部格子（含不可见占位）');
    ok(grid.innerHTML.indexOf('hm-cell lv3') >= 0 || grid.innerHTML.indexOf('hm-cell lv4') >= 0,
       '有数据的格子带分级 class');
    ok(grid.innerHTML.indexOf('no-data') < 0, '起点之前也用普通 0 级色（GitHub 同款）');
    ok(byDate['2026-10-31'].noData && /class="hm-cell lv0[^"]*"[^>]*data-d="2026-10-31"/.test(grid.innerHTML),
       '起点之前渲染成 lv0，但内部仍标记 noData（气泡里说明"没有统计数据"）');
    const peak = ctx.__els.get('heatmapPeak');
    ok(peak.textContent.indexOf('峰值') >= 0 && peak.textContent.indexOf('2026-11-07') >= 0,
       '峰值文案走中文语言包', peak.textContent);
    eq(ctx.__els.get('heatmapNoData').textContent, '',
       '有数据时摘要行不再显示"统计数据自…"（该口径挪进每格气泡）');
    ok(!ctx.__pane.classList.contains('is-empty'), '有数据时移除空态');

    // 服务端时区才是日期键的口径：布局终点必须跟 range.end 走，而不是浏览器今天。
    const anchoredPayload = payload();
    anchoredPayload.range = { start: '2025-11-16', end: '2026-11-15' };
    ctx.__payload = anchoredPayload;
    H.reset();
    await H.refresh();
    const anchoredHtml = ctx.__els.get('heatmapGrid').innerHTML;
    ok(anchoredHtml.indexOf('data-d="2026-11-15"') >= 0,
       '布局终点用服务端 range.end（跨时区不丢最新一天）');
    ok(anchoredHtml.indexOf('data-d="2026-11-16"') < 0,
       'range.end 之后是占位格');
    ctx.__payload = payload();
    H.reset();
    await H.refresh();

    // 语言切换：只重渲染，不重新请求
    const fetchCount = ctx.__fetches.length;
    vm.runInContext("I18N.setLocale('en');", ctx);
    eq(ctx.__fetches.length, fetchCount, '切语言不重新请求');
    ok(ctx.__els.get('heatmapPeak').textContent.indexOf('Peak') >= 0,
       '切语言后峰值文案变成英文', ctx.__els.get('heatmapPeak').textContent);

    // ── 悬停气泡（GitHub 同款自绘，不用原生 title）──
    vm.runInContext("I18N.setLocale('zh-CN');", ctx);
    const gridEl = ctx.__els.get('heatmapGrid');
    ok(gridEl.innerHTML.indexOf('title=') < 0, '格子不再用原生 title（避免双气泡）');
    ok(typeof (gridEl._h || {}).mouseover === 'function', '网格绑定了 mouseover 委托');
    const fakeCell = (date) => ({
        getAttribute: () => date,
        getBoundingClientRect: () => ({ left: 100, top: 500, width: 10, height: 10,
                                        right: 110, bottom: 510 }),
    });
    gridEl._h.mouseover({ target: { closest: () => fakeCell('2026-11-07') } });
    const tip = ctx.__created.find((e) => e.className === 'hm-tip');
    ok(tip && tip.hidden === false, '悬停后气泡可见');
    ok(tip.innerHTML.indexOf('2026-11-07') >= 0 && tip.innerHTML.indexOf('92.6 Wh') >= 0,
       '气泡显示日期与当日电量', tip && tip.innerHTML);
    ok(tip.innerHTML.indexOf('26 次充电') >= 0, '气泡显示充电次数', tip && tip.innerHTML);
    gridEl._h.mouseover({ target: { closest: () => fakeCell('2026-11-06') } });
    ok(tip.hidden === false && tip.innerHTML.indexOf('无充电记录') >= 0,
       '零电量日显示"无充电记录"');
    // 没有记录的格子也必须给气泡：否则一年里绝大多数格子悬停都是哑的
    gridEl._h.mouseover({ target: { closest: () => fakeCell('2026-10-31') } });
    ok(tip.hidden === false && tip.innerHTML.indexOf('无统计数据') >= 0,
       '统计起点之前的格子也给气泡（说明为什么是空的）', tip.innerHTML);
    ok(tip.innerHTML.indexOf('2026-10-31') >= 0, '气泡始终带日期');
    // 占位格（一周首尾不属于统计区间的那几格）没有 data-d：不该弹
    gridEl._h.mouseover({ target: { closest: () => ({ getAttribute: () => null }) } });
    ok(tip.hidden === true, '占位格（无 data-d）不弹气泡');

    // 设备切换：reset 后旧响应作废
    const oldSeq = H._state.seq;
    H.reset();
    ok(H._state.seq === oldSeq + 1, 'reset 自增序号');
    ok(H._state.data === null, 'reset 丢弃旧设备数据');

    // 空库：走空态
    ctx.__payload = { range: {}, data_since: null, total_wh: 0, peak: null,
                      levels: [], days: [] };
    H.reset();
    await H.refresh();
    eq(ctx.__els.get('heatmapGrid').innerHTML, '', '空库不画网格');
    ok(ctx.__pane.classList.contains('is-empty'), '空库显示空态');
    ok(ctx.__els.get('heatmapNoData').textContent.length > 0, '空库有提示文案');

    // 304：保留旧图（不覆盖成空）
    ctx.__payload = payload();
    H.reset();
    await H.refresh();
    const htmlBefore304 = ctx.__els.get('heatmapGrid').innerHTML;
    ctx.DeviceStore.api = (url) => {
        ctx.__fetches.push(String(url));
        return Promise.resolve({ status: 304, json: async () => { throw new Error('304 不该解析 body'); } });
    };
    await H.refresh();
    eq(ctx.__els.get('heatmapGrid').innerHTML, htmlBefore304, '304 保留旧图');

    console.log(`\n${passed} passed, ${failed} failed`);
    process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
