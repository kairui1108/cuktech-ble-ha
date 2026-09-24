#!/usr/bin/env node
/**
 * Contrast regression test for the charge-limit card colours.
 *
 * Why this exists: the card originally received its port colours via inline
 * `style` attributes built from `PORT_COLORS` (hardcoded dark-theme hex). Inline
 * styles outrank stylesheets AND do not react to the theme, so in the phone
 * page's white theme #FFD24B (USB-A) was rendered on white at 1.38:1 —
 * effectively unreadable, which is the bug this guards against.
 *
 * What it checks: every port colour declared for the charge-limit card is
 * declared for BOTH themes (dark + light) and meets WCAG AA against the card
 * background it is actually painted on. It reads the real CSS so a colour
 * tweak that breaks contrast fails here instead of shipping.
 *
 * Thresholds: WCAG 2.1 — 4.5:1 for normal text, 3:1 for non-text graphics
 * (progress bars). The buttons are outline-style, so their label sits directly
 * on the card background.
 *
 * Usage: node tests/js/charge_limit_contrast_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const STATIC = path.resolve(__dirname, '../../web/static');

// ── WCAG relative luminance / contrast ──

function channel(c) {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

function luminance(hex) {
    const h = hex.replace('#', '');
    const [r, g, b] = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16));
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(fg, bg) {
    const a = luminance(fg);
    const b = luminance(bg);
    const [hi, lo] = a > b ? [a, b] : [b, a];
    return (hi + 0.05) / (lo + 0.05);
}

// ── Extract the colour declarations from the real stylesheet ──

/** Pull `--name: #hex;` pairs out of a given selector block in phone.css. */
function readBlock(css, selector) {
    const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}');
    const m = css.match(re);
    if (!m) throw new Error(`selector not found in phone.css: ${selector}`);
    const vars = {};
    const varRe = /(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})/g;
    let v;
    while ((v = varRe.exec(m[1]))) vars[v[1]] = v[2];
    return vars;
}

const css = fs.readFileSync(path.join(STATIC, 'phone.css'), 'utf8');
const dark = readBlock(css, ':root');
const light = readBlock(css, 'body.light');

// phone.css card backgrounds for each theme
const CARD_BG = { dark: '#000000', light: '#ffffff' };  // --card-bg per theme

const PORTS = ['c1', 'c2', 'c3', 'a'];
const TEXT_MIN = 4.5;   // normal text
const GRAPHIC_MIN = 3.0; // progress bar fill

let failed = 0;
let passed = 0;

function check(cond, label, detail) {
    if (cond) { passed++; console.log(`  ok   ${label}`); }
    else { failed++; console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`); }
}

console.log('\n-- 两套主题共用同一批端口色（浅色主题不覆盖，继承 :root） --');
const resolvedLight = Object.assign({}, dark, light);  // 浅色主题下变量实际解析结果
for (const p of PORTS) {
    const c = dark[`--limit-${p}`];
    check(typeof c === 'string', `:root 声明 --limit-${p}`);
    check(light[`--limit-${p}`] === undefined || light[`--limit-${p}`] === c,
          `light 主题的 --limit-${p} 与深色一致（${c}）`,
          `实际 ${light[`--limit-${p}`]}`);
}
check(typeof dark['--limit-danger'] === 'string', ':root 声明 --limit-danger');
check(light['--limit-danger'] === undefined || light['--limit-danger'] === dark['--limit-danger'],
      'light 的 --limit-danger 与深色一致');

// 亮色端口色无法作为白底小字（1.44～2.6:1），因此按钮改为"实底 + 深色字"，
// 颜色只作为填充出现。文字色固定 #1a1a1a。
// 按钮 = 端口色淡染底(0.15) + 主题文字色。文字不能用端口色：端口色是亮色，
// 作白底小字仅 1.44～2.6:1（见下一节），作淡染底则没问题。
console.log('\n-- 按钮：淡染底 + 主题文字色 --');
const setRule = css.match(/\.charge-limit-set \{([^}]*)\}/);
const clearRule = css.match(/\.charge-limit-clear \{([^}]*)\}/);
check(!!setRule && !!clearRule, '两个按钮规则已定义');
if (setRule && clearRule) {
    check(/color:\s*var\(--text\)/.test(setRule[1]), 'set 按钮文字用 var(--text)');
    check(/background:\s*rgba\(var\(--port-color-rgb\),\s*0\.15\)/.test(setRule[1]),
          'set 按钮用 0.15 淡染底（实底全饱和过于扎眼）');
    check(!/color:\s*var\(--port-color\)/.test(setRule[1]),
          'set 按钮文字不用端口色（亮色小字不可读）');
    check(/color:\s*var\(--text\)/.test(clearRule[1]), 'clear 按钮文字用 var(--text)');
    check(/rgba\(var\(--limit-danger-rgb\),\s*0\.15\)/.test(clearRule[1]),
          'clear 按钮用 0.15 淡染底');
}

// 用真实主题底色 + 淡染公式复算按钮文字的对比度。
function overOn(rgb, alpha, bg) {
    const bh = bg.replace('#', '');
    const out = rgb.map((c, i) => Math.round(c * alpha + parseInt(bh.slice(i * 2, i * 2 + 2), 16) * (1 - alpha)));
    return '#' + out.map(v => v.toString(16).padStart(2, '0')).join('');
}
const PORT_RGB = {
    c1: [255, 122, 0], c2: [70, 180, 255], c3: [137, 216, 243], a: [255, 210, 75],
};
const DANGER_RGB = [255, 107, 96];
for (const [themeName, card, text] of [['暗色', '#000000', '#e6e6e6'], ['白色', '#ffffff', '#1a1a1a']]) {
    for (const [p, rgb] of Object.entries(PORT_RGB)) {
        const bg = overOn(rgb, 0.15, card);
        const r = contrast(text, bg);
        check(r >= TEXT_MIN, `${themeName} set ${p} 文字 ${text} on ${bg} = ${r.toFixed(2)}:1`, `需 >= ${TEXT_MIN}`);
    }
    const dbg = overOn(DANGER_RGB, 0.15, card);
    check(contrast(text, dbg) >= TEXT_MIN,
          `${themeName} clear 文字 ${text} on ${dbg} = ${contrast(text, dbg).toFixed(2)}:1`);
}

console.log('\n-- 浅色主题下亮色端口色确实不能作小字（记录该约束，防止有人改回去） --');
for (const p of PORTS) {
    const c = dark[`--limit-${p}`];
    const r = contrast(c, '#ffffff');
    check(r < TEXT_MIN,
          `light ${p} ${c} 作白底小字仅 ${r.toFixed(2)}:1（故不可用作文字色）`);
}

console.log('\n-- phone.js 不得再把颜色内联进卡片（内联覆盖样式表且不随主题变化） --');
const phoneJs = fs.readFileSync(path.join(STATIC, 'phone.js'), 'utf8');
const cardBlock = phoneJs.slice(
    phoneJs.indexOf('// ── Charge Limit'),
    phoneJs.indexOf('// ── Delay Off ──'));
// Strip // comments first: the block's own explanatory comment mentions PORT_COLORS
// and must not be mistaken for a usage.
const cardCode = cardBlock.split('\n').map(l => l.replace(/\/\/.*$/, '')).join('\n');

check(!/style="[^"]*(background|color)\s*:/i.test(cardCode),
      'charge limit 卡片 HTML 无内联颜色');
check(!/PORT_COLORS\[/.test(cardCode),
      'charge limit 卡片代码不引用 PORT_COLORS');
for (const hex of ['#FFD24B', '#FF7A00', '#46B4FF', '#89D8F3']) {
    check(!cardCode.includes(hex), `卡片代码无硬编码 ${hex}`);
}
for (const cls of ['charge-limit-row', 'charge-limit-set', 'charge-limit-clear',
                   'charge-limit-dot', 'charge-limit-fill', 'charge-limit-chip',
                   'charge-limit-action']) {
    check(cardCode.includes(cls), `卡片使用 .${cls}（配色由 CSS 变量控制）`);
}
// 不能再复用 .sim-btn：它的底色写死 rgba(0,0,0,0.6)，在白色卡片上合成 #666，
// 与浅色主题的 --text-sub(#666) 文字叠出 1.00:1（完全不可见的回归）。
check(!/class="sim-btn/.test(cardCode) && !/sim-btn/.test(cardCode),
      'charge limit 卡片不复用 .sim-btn（其底色写死深色、不随主题变化）');

// ── index.html 的等价保护 ──
// index 用 applyTheme() 逐个注入 --port-c*（不是 class），所以它的卡片进度条/端口名
// 依赖主题里显式声明这些变量；漏声明就会退回 :root 的深色值，在白底上不可读。
console.log('\n-- index 卡片模板不得内联硬编码端口色（须用 var(--port-*)） --');
const appJs = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');
const idxCard = appJs.slice(appJs.indexOf('function renderChargeLimit()'),
                            appJs.indexOf('function setChargeLimitQuick'));
const idxCode = idxCard.split('\n').map(l => l.replace(/\/\/.*$/, '')).join('\n');
for (const hex of ['#FFD24B', '#FF7A00', '#46B4FF', '#89D8F3']) {
    check(!idxCode.includes(hex), `index 卡片代码无硬编码 ${hex}`);
}
check(/var\(--port-/.test(idxCode), 'index 卡片用 var(--port-*) 取端口色（随主题解析）');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
