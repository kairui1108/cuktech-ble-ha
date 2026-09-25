#!/usr/bin/env node
/**
 * 浅色主题调色板回归测试（index.css）。
 *
 * 为什么单独测：桌面页的浅色主题被反馈过"整体暗淡"——根因是两类取值一起偏了
 *   1. 亮端口色/淡染底当小字：白底 1.4–2.6:1，看着发灰；
 *   2. 为了过 AA 把品牌色一路压深，压到没有彩度（#0d7c8a 这类）。
 * 解法是把"品牌色"拆成两支：--accent 管淡染底/描边/图形（可以饱和），
 * --accent-ink 管文字（必须过 4.5:1）；淡染底强度用 --wash* 令牌按主题分档。
 * 这个文件把这几条约定固定下来，免得下次调色又调回灰的或调回不可读的。
 *
 * 阈值：WCAG 2.1 —— 正文 4.5:1，非文本图形 3:1。
 *
 * Usage: node tests/js/index_light_contrast_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const STATIC = path.resolve(__dirname, '../../web/static');

function channel(c) {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

function normHex(hex) {
    const raw = String(hex).trim().replace('#', '');
    return raw.length === 3 ? raw.split('').map(c => c + c).join('') : raw;
}

function rgb(hex) {
    const h = normHex(hex);
    return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16));
}

function luminance(hex) {
    const [r, g, b] = rgb(hex);
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(fg, bg) {
    const a = luminance(fg), b = luminance(bg);
    const [hi, lo] = a > b ? [a, b] : [b, a];
    return (hi + 0.05) / (lo + 0.05);
}

function parseRgba(str) {
    const m = String(str || '').match(/rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)/);
    if (!m) return null;
    return { rgb: [+m[1], +m[2], +m[3]], a: m[4] === undefined ? 1 : +m[4] };
}

function overOn(fgRgb, alpha, bgHex) {
    const b = rgb(bgHex);
    const out = fgRgb.map((c, i) => Math.round(c * alpha + b[i] * (1 - alpha)));
    return '#' + out.map(v => v.toString(16).padStart(2, '0')).join('');
}

/** 抽出某个选择器块里的 `--name: <值>;`；值支持 #hex 与 rgba()。 */
function readBlock(css, selector) {
    const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}');
    const m = css.match(re);
    if (!m) throw new Error(`selector not found in index.css: ${selector}`);
    const vars = {};
    // 值可能是 #hex / rgba() / var(--x) / 逗号分隔的 rgb 三元组（--*-rgb）/ 纯数字（--wash）
    const varRe = /(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|var\([^)]*\)|[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+|[\d.]+)/g;
    let v;
    while ((v = varRe.exec(m[1]))) vars[v[1]] = v[2];
    return vars;
}

const css = fs.readFileSync(path.join(STATIC, 'index.css'), 'utf8');
const dark = readBlock(css, ':root');
// 浅色主题的令牌块（html[data-appearance="light"]）
const light = readBlock(css, 'html[data-appearance="light"]');
// 浅色下没覆盖的令牌继承 :root
const L = Object.assign({}, dark, light);

const CARD = L['--card-bg'] || '#ffffff';   // 卡片面
const TEXT_MIN = 4.5;
const GRAPHIC_MIN = 3.0;

let passed = 0, failed = 0;
function check(cond, label, detail) {
    if (cond) { passed++; console.log(`  ok   ${label}`); }
    else { failed++; console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`); }
}
const f = (n) => Number(n).toFixed(2);

console.log('\n-- 浅色主题：品牌色必须拆成"底色"与"文字墨"两支 --');
check(light['--accent'] !== undefined && light['--accent-ink'] !== undefined,
      '浅色主题分别声明了 --accent 与 --accent-ink');
check(dark['--accent-ink'] === 'var(--accent)',
      '深色主题两者同值（--accent-ink: var(--accent)），视觉零变化',
      `实际 ${dark['--accent-ink']}`);

console.log('\n-- 文字墨：正文 ≥4.5:1 --');
const inkCases = [
    ['--accent-ink on 卡片面', L['--accent-ink'], CARD],
    ['--text on 卡片面', L['--text'], CARD],
    ['--text-sub on 卡片面', L['--text-sub'], CARD],
    ['--success on 卡片面', L['--success'], CARD],
    ['--warning on 卡片面', L['--warning'], CARD],
    ['--danger on 卡片面', L['--danger'], CARD],
];
for (const [label, fg, bg] of inkCases) {
    const c = contrast(fg, bg);
    check(c >= TEXT_MIN, `${label} ${fg} → ${f(c)}:1`, `需要 ≥${TEXT_MIN}`);
}
// 记录现状：--text-dim 与 phone.css 的 body.light (#888) 逐值对齐，白底 3.54:1，
// 低于小字 AA 的 4.5:1。这是"桌面与手机一个配色"的有意取舍（用户明确要求），
// 底线是 3:1——再往下调就等于把单位/轴刻度做成看不清的装饰。
const dimC = contrast(L['--text-dim'], CARD);
check(dimC >= 3.0, `--text-dim ${L['--text-dim']} → ${f(dimC)}:1（与手机页一致，低于小字 AA 但守住 3:1）`,
      '低于 3:1 就该改回 #6f7076（4.94:1）');

console.log('\n-- 淡染底上的文字（按钮）：按各主题的 --wash 复算 ≥4.5:1 --');
for (const theme of ['dark', 'light']) {
    const t = theme === 'dark' ? Object.assign({}, dark) : L;
    const bg = theme === 'dark' ? (dark['--card-bg'] || '#000000') : CARD;
    for (const [name, rgbVar, inkVar] of [
        ['action 按钮（连接设备/设置）', '--action-rgb', '--action-ink'],
        ['danger 按钮（断开设备/关闭）', '--danger-rgb', '--on-danger'],
    ]) {
        const wash = parseRgba(`rgba(${t[rgbVar]}, ${t['--wash']})`);
        if (!wash) { check(false, `${theme} ${name} 能解析 --wash`); continue; }
        const surface = overOn(wash.rgb, wash.a, bg);
        let ink = t[inkVar];
        if (ink === 'var(--accent-ink)') ink = t['--accent-ink'];
        if (ink === 'var(--accent)') ink = t['--accent'];
        if (ink === 'var(--danger)') ink = t['--danger'];
        const c = contrast(ink, surface);
        check(c >= TEXT_MIN, `${theme} ${name}: 文字 ${ink} / 淡染底 ${surface} → ${f(c)}:1`, `需要 ≥${TEXT_MIN}`);
    }
}

console.log('\n-- 浅色主题的淡染底要比深色浓（白底上 16% 看着就是灰的） --');
const dWash = parseFloat(dark['--wash']), lWash = parseFloat(light['--wash']);
check(lWash > dWash, `浅色 --wash ${lWash} > 深色 --wash ${dWash}`);
check(parseFloat(light['--edge']) > parseFloat(dark['--edge']),
      `浅色 --edge ${light['--edge']} > 深色 --edge ${dark['--edge']}`);

console.log('\n-- 图形（3:1 即可）：柱、点、协议色 --');
const graphicCases = [
    ['--total-line（「总」模式的总功率曲线）', L['--total-line']],
    ['--proto-pd', L['--proto-pd']],
    ['--dot-on（已连接圆点）', L['--dot-on']],
    ['--dot-warn（连接中圆点）', L['--dot-warn']],
    ['--proto-pps', L['--proto-pps']],
    ['--proto-ufcs', L['--proto-ufcs']],
    ['--proto-scp', L['--proto-scp']],
];
for (const [label, color] of graphicCases) {
    const raw = parseRgba(color);
    const surface = raw ? overOn(raw.rgb, raw.a, CARD) : color;
    const c = contrast(surface, CARD);
    check(c >= GRAPHIC_MIN, `${label} ${color} → ${f(c)}:1`, `需要 ≥${GRAPHIC_MIN}`);
}

console.log('\n-- 用电柱与侧栏迷你柱状图同色；协议色不得再用品牌青 --');
check(L['--chart-bar'] === 'var(--port-c2)',
      '每小时用电柱 --chart-bar = var(--port-c2)（与右侧"当前总功率"迷你柱状图同色）',
      `实际 ${L['--chart-bar']}`);
check(L['--proto-pd'] !== L['--accent'] && L['--proto-pd'] !== L['--accent-ink'],
      `协议色 PD ${L['--proto-pd']} 不是品牌青（--accent ${L['--accent']}）`,
      '品牌色在"快充协议"tab 里会和数据色混淆');
// 记录现状：--chart-bar 是亮蓝（白底 2.27:1），低于图形件 3:1，
// 这是"与侧栏迷你柱状图保持一致"的有意选择（侧栏那张图本来就是这个蓝）。
const barC = contrast(/^#/.test(L['--chart-bar']) ? L['--chart-bar'] : '#46B4FF', CARD);
check(barC >= 1.5, `--chart-bar（解析为 --port-c2 #46B4FF）对卡面 ${f(barC)}:1（记录现状，低于 3:1）`);

console.log('\n-- 亮端口色不能当小字用（浅色下 1.4–2.6:1）：浅色必须有圆点描边兜底 --');
// 选择器列表可能随主题增删，只要求"浅色规则里出现 .energy-dot 且带内描边"
const hasDotRing = /html\[data-appearance="light"\][^{}]*\.energy-dot[^{}]*\{[^{}]*box-shadow/.test(css);
check(hasDotRing, '浅色主题给 .energy-dot/.share-dot 加了内描边（撑出圆点轮廓）');

console.log('\n-- 桌面浅色令牌与 phone.css body.light 必须逐值一致（同一个配色） --');
const phoneCss = fs.readFileSync(path.join(STATIC, 'phone.css'), 'utf8');
const phoneLight = readBlock(phoneCss, 'body.light');
// 手机页没有 --track，只有 --limit-track（无进度槽的等价物）
const PARITY = [
    ['--bg', '--bg'], ['--card-bg', '--card-bg'], ['--card-border', '--card-border'],
    ['--text', '--text'], ['--text-sub', '--text-sub'], ['--text-dim', '--text-dim'],
    ['--pval-color', '--pval-color'],
    // 进度槽：桌面浅色 --track 与手机 --limit-track 同值（浅灰，两边一致）
    ['--track', '--limit-track'],
];
// 只比"颜色本身"，空格写法不计（rgba(0, 0, 0, .1) 与 rgba(0,0,0,.1) 等价）
const norm = (v) => String(v || '').replace(/\s+/g, '').toLowerCase();
for (const [idxVar, phoneVar] of PARITY) {
    check(norm(L[idxVar]) === norm(phoneLight[phoneVar]),
          `${idxVar} 与手机页 ${phoneVar} 一致（${L[idxVar]}）`,
          `桌面 ${L[idxVar]} vs 手机 ${phoneLight[phoneVar]}`);
}

console.log('\n-- 主操作按钮（连接设备 / 设置）= 淡蓝 --action-*（不跟端口色、不用品牌青） --');
check(L['--action-rgb'] === '70, 180, 255', `--action-rgb 是端口蓝 ${L['--action-rgb']}`);
const idxSet = css.match(/\.countdown-toggle-btn\.set \{([^}]*)\}/);
check(!!idxSet, 'index 的 .countdown-toggle-btn.set 已定义');
if (idxSet) {
    check(/background:[^;]*--action-rgb[^;]*var\(--wash\)/.test(idxSet[1]),
          '设置按钮用 action 蓝淡染底（与标题栏连接按钮同一套令牌）',
          idxSet[1].replace(/\s+/g, ' ').slice(0, 90));
    check(/border:[^;]*--action-rgb[^;]*var\(--edge\)/.test(idxSet[1]), '设置按钮描边同为 action 蓝');
    check(/color:\s*var\(--action-ink\)/.test(idxSet[1]), '设置按钮文字用 --action-ink');
    check(!/--port-color/.test(idxSet[1]), '设置按钮不再跟端口色（端口色只标识端口）');
}
const topBtn = css.match(/\.topbar-status \.btn-primary \{([^}]*)\}/);
check(!!topBtn, '标题栏 .btn-primary（连接设备）已定义');
if (topBtn) {
    check(/--action-rgb/.test(topBtn[1]), '连接设备按钮用 action 蓝');
    check(/color:\s*var\(--action-ink\)/.test(topBtn[1]), '连接设备按钮文字用 --action-ink');
}
// 文字墨必须在各自的淡染底上过 4.5:1
for (const [theme, card, rgb, wash, inkName] of [
    ['暗色', dark['--card-bg'] || '#000000', dark['--action-rgb'], dark['--wash'], dark['--action-ink']],
    ['浅色', CARD, L['--action-rgb'], L['--wash'], L['--action-ink']],
]) {
    const [r, g, b] = String(rgb).split(',').map(Number);
    const surface = overOn([r, g, b], Number(wash), card);
    const c = contrast(inkName, surface);
    check(c >= TEXT_MIN, `${theme} action 按钮文字 ${inkName} / 淡染底 ${surface} → ${f(c)}:1`, `需要 ≥${TEXT_MIN}`);
}

console.log('\n-- 端口色本身不许被浅色主题覆盖（两套主题共用同一批值） --');
for (const p of ['c1', 'c2', 'c3', 'a']) {
    check(light['--port-' + p] === undefined, `浅色主题未覆盖 --port-${p}`);
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
