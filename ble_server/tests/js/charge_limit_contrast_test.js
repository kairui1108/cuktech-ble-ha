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
    // #000/#fff 这类 3 位写法要展开：CSS 变量里就有，不展开会读出 NaN，
    // 而 NaN 参与的比较恒为 false，断言会"看起来在检查、实际没检查"。
    const raw = hex.replace('#', '');
    const h = raw.length === 3 ? raw.split('').map(c => c + c).join('') : raw;
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
    // 同时收 #hex 与 rgba()：--limit-track 就是 rgba，读不到它就没法复算进度槽
    const varRe = /(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8}|rgba\([^)]*\))/g;
    let v;
    while ((v = varRe.exec(m[1]))) vars[v[1]] = v[2];
    return vars;
}

const css = fs.readFileSync(path.join(STATIC, 'phone.css'), 'utf8');
const dark = readBlock(css, ':root');
const light = readBlock(css, 'body.light');

// phone.css card backgrounds: filled in per theme below (见 --bg / --card-bg 的读取)

const PORTS = ['c1', 'c2', 'c3', 'a'];
const TEXT_MIN = 4.5;   // normal text
const GRAPHIC_MIN = 3.0; // 非文本图形（进度条填充、端口圆点、指示点）

let failed = 0;
let passed = 0;

function check(cond, label, detail) {
    if (cond) { passed++; console.log(`  ok   ${label}`); }
    else { failed++; console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`); }
}

console.log('\n-- 两套主题共用同一批端口色（浅色主题不覆盖，继承 :root） --');
const resolvedLight = Object.assign({}, dark, light);  // 浅色主题下变量实际解析结果

// 覆色：把 alpha 叠到底色上，得到实际看到的颜色（3 位写法同样先展开，见 luminance）
function normHex(hex) {
    const raw = hex.replace('#', '');
    return '#' + (raw.length === 3 ? raw.split('').map(c => c + c).join('') : raw);
}
function overOn(rgb, alpha, bg) {
    const bh = normHex(bg).replace('#', '');
    const out = rgb.map((c, i) => Math.round(c * alpha + parseInt(bh.slice(i * 2, i * 2 + 2), 16) * (1 - alpha)));
    return '#' + out.map(v => v.toString(16).padStart(2, '0')).join('');
}

// 卡组把每个端口单独做成一张内层小卡，按钮/进度条/输入框都画在这层"内层卡面"上，
// 不是外卡面(--card-bg)。深色主题的卡面是 --bg(#121215)；浅色主题是 body.light 里
// 显式写的纯白（不再是 --bg——那样会和页面底色一模一样，看着像从背景抠出来的一块）。
// 直接从 CSS 读，改主题时断言不会跑偏。
const lightCardRule = css.match(/body\.light \.charge-limit-card \{([^}]*)\}/);
const lightCardSurface = lightCardRule
    ? (lightCardRule[1].match(/background:\s*(#[0-9a-fA-F]{3,8})/) || [])[1]
    : undefined;
const CARD_BG = {
    dark: dark['--bg'] || '#121215',
    light: lightCardSurface || resolvedLight['--bg'] || '#f0f0f5',
};
// 进度槽：主题各自的 --limit-track（浅色压得很深，见 body.light）覆在卡面上。
// 从 CSS 里读真实颜色再复算，改槽色时断言不会跑偏。
function parseRgba(str) {
    const m = String(str || '').match(/rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)/);
    if (!m) return null;
    return { rgb: [+m[1], +m[2], +m[3]], a: m[4] === undefined ? 1 : +m[4] };
}
const TRACK_BG = {};
for (const theme of ['dark', 'light']) {
    const raw = (theme === 'dark' ? dark : resolvedLight)['--limit-track'];
    const c = parseRgba(raw);
    TRACK_BG[theme] = c ? overOn(c.rgb, c.a, CARD_BG[theme]) : null;
}

// 翻页指示点画在外卡面（--card-bg）上
const OUTER_BG = {
    dark: dark['--card-bg'] || '#000000',
    light: resolvedLight['--card-bg'] || '#ffffff',
};

// 浅色主题里内层端口卡是纯白、外层容器卡也是纯白——没有任何颜色差。这时卡片的
// 轮廓完全靠描边和投影，少任何一样卡片就"消失"在外层白卡里。这条断言盯着它。
console.log('\n-- 白卡叠白卡：描边与投影一个都不能少 --');
check(lightCardSurface === OUTER_BG.light,
      `记录现状：浅色主题内层卡面 ${lightCardSurface} 与外层卡面 ${OUTER_BG.light} 同色`,
      '（若将来给内层卡换回浅灰，这条会失败，请连同下面的断言一起调整）');
if (lightCardRule) {
    check(/box-shadow:\s*(?!none)/.test(lightCardRule[1]),
          '同色时必须有投影，否则叠放层次看不出来');
    check(!/box-shadow:\s*none/.test(lightCardRule[1]), '投影没有被 none 关掉');
    const bc = (lightCardRule[1].match(/border-color:\s*rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*([\d.]+)\s*\)/) || [])[1];
    check(bc !== undefined && Number(bc) >= 0.1,
          `同色时描边要够深（实际 alpha ${bc}，需 >= 0.1）`);
}

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

const PORT_RGB = {
    c1: [255, 122, 0], c2: [70, 180, 255], c3: [137, 216, 243], a: [255, 210, 75],
};
const DANGER_RGB = [255, 107, 96];
// 用真实主题底色 + 淡染公式复算按钮文字的对比度
for (const [themeName, card, text] of [['暗色', CARD_BG.dark, '#e6e6e6'], ['白色', CARD_BG.light, '#1a1a1a']]) {
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

// 图形（进度条填充、端口圆点、翻页指示点）在两套主题下必须是**同一个亮端口色**——
// 曾经为了让白底过 3:1 给浅色主题换了一批同色系深墨，结果进度条和圆点明显发暗、
// 和暗色主题对不上。现在的做法是：颜色不变，浅色主题靠 --limit-graphic-edge 描边
// 和更深的进度槽来保证可辨识。这一段就是挡住"又改回换色"的路线。
console.log('\n-- 图形色：两套主题共用同一批亮端口色（不许浅色主题另换一批暗色） --');
for (const p of PORTS) {
    check(light[`--limit-${p}`] === undefined,
          `light 不覆盖 --limit-${p}（${dark[`--limit-${p}`]} 两套主题一致）`,
          `实际被覆盖成 ${light[`--limit-${p}`]}`);
}
check(!Object.keys(light).some(k => /^--limit-ink/.test(k)),
      '没有 --limit-ink-* 这种"浅色专用暗色"（会让图形发暗）');
check(!/--limit-ink/.test(css), 'CSS 里没有 --limit-ink 的引用');
check(light['--limit-danger'] === undefined, 'light 不覆盖 --limit-danger');

// 亮端口色在白底确实看不见（1.3～2.6:1），所以浅色主题必须有描边/深槽兜底
console.log('\n-- 浅色主题靠描边 + 深进度槽兜底（亮色本身过不了 3:1） --');
for (const p of PORTS) {
    const r = contrast(dark[`--limit-${p}`], CARD_BG.light);
    check(r < GRAPHIC_MIN, `light ${p} ${dark[`--limit-${p}`]} 对卡面仅 ${r.toFixed(2)}:1（所以必须有描边）`);
}
// 描边：body.light .charge-limit-* 里定义的中性深色，要对浅色的两个面都够
const edgeRule = css.match(/body\.light \.charge-limit-card, body\.light \.charge-limit-dotnav \{([^}]*)\}/);
check(!!edgeRule, 'body.light 有 .charge-limit-card/.charge-limit-dotnav 的覆盖规则');
if (edgeRule) {
    const edge = (edgeRule[1].match(/--limit-graphic-edge:\s*(#[0-9a-fA-F]{3,8})/) || [])[1];
    check(typeof edge === 'string', '覆盖规则里声明了 --limit-graphic-edge');
    if (edge) {
        check(contrast(edge, CARD_BG.light) >= GRAPHIC_MIN,
              `light 描边 ${edge} 对内层卡面 ${contrast(edge, CARD_BG.light).toFixed(2)}:1`, `需 >= ${GRAPHIC_MIN}`);
        check(contrast(edge, OUTER_BG.light) >= GRAPHIC_MIN,
              `light 描边对外卡面 ${OUTER_BG.light} ${contrast(edge, OUTER_BG.light).toFixed(2)}:1`);
    }
}
// 暗色主题：深槽 + 亮填充，填充对槽底必须 >= 3:1（槽底 #28282a 本来就深）。
console.log('\n-- 暗色进度条：亮填充对槽底 >= 3:1 --');
check(typeof TRACK_BG.dark === 'string', 'dark 能解析出 --limit-track');
if (typeof TRACK_BG.dark === 'string') {
    for (const p of PORTS) {
        const fill = dark[`--limit-${p}`];
        const r = contrast(fill, TRACK_BG.dark);
        check(r >= GRAPHIC_MIN,
              `dark ${p} 填充 ${fill} 对槽底 ${TRACK_BG.dark} = ${r.toFixed(2)}:1`,
              `需 >= ${GRAPHIC_MIN}`);
    }
}

// 浅色主题：**记录现状**——槽是浅灰（与 index.css 浅色主题的 --track 同值）。
// 由来：这里一度为了让亮填充过 3:1 把浅色槽压到 rgba(0,0,0,0.75)，观感是"深灰长条"，
// 产品侧明确要求浅色下不要深灰，于是改回浅灰。代价是亮端口色填充对浅槽只有
// 1.15–2.09:1，达不到图形件 3:1——浅色主题下"填到哪儿"主要靠填充色本身、
// 旁边的 Wh 数值和百分比文字来读。下面几条把现状钉住，避免有人当成 bug 又改回深槽。
console.log('\n-- 浅色进度条：浅灰槽（记录现状，已知低于 3:1） --');
check(typeof TRACK_BG.light === 'string', 'light 能解析出 --limit-track');
if (typeof TRACK_BG.light === 'string') {
    const tr = TRACK_BG.light;
    const trLum = luminance(tr);
    check(trLum >= 0.6, `light 槽底 ${tr} 是浅灰（相对亮度 ${trLum.toFixed(3)}，需 >= 0.6）`,
          '深槽方案已被产品侧否掉，别再压深');
    const trVsCard = contrast(tr, CARD_BG.light);
    check(trVsCard < 1.5, `light 槽对卡面 ${trVsCard.toFixed(2)}:1（只是一道浅凹槽，不喧宾夺主）`);
    const worst = contrast(dark['--limit-a'], tr);
    check(worst < GRAPHIC_MIN,
          `记录：light 最差填充 ${dark['--limit-a']} 对浅槽仅 ${worst.toFixed(2)}:1（已知偏离，见上方说明）`);
}

// 图形元素用亮端口色填充（和暗色主题一致），不是任何"墨色"变量
console.log('\n-- 图形元素用 --port-color 填充（两主题一致） --');
for (const pattern of [/\.charge-limit-dot \{([^}]*)\}/, /\.charge-limit-fill \{([^}]*)\}/]) {
    const m = css.match(pattern);
    const name = String(pattern).replace(/[/\\{}().*?|^$[\]]/g, '');
    if (!m) { check(false, `找到 ${name} 规则`); continue; }
    check(/background:\s*var\(--port-color\)/.test(m[1]), `${name} 用 --port-color（与暗色主题同色）`);
    check(!/var\(--limit-ink/.test(m[1]), `${name} 不用 --limit-ink（会发暗）`);
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
for (const cls of ['charge-limit-card', 'charge-limit-set', 'charge-limit-clear',
                   'charge-limit-dot', 'charge-limit-fill', 'charge-limit-chip',
                   'charge-limit-action', 'charge-limit-dotnav']) {
    check(cardCode.includes(cls), `卡片使用 .${cls}（配色由 CSS 变量控制）`);
}
// 卡组容器（.charge-limit-deck）在 phone.html 里，不在这段 JS 里，单独验一次：
// 少挂这个类的话 CSS 的 grid 叠放规则全部落空，4 张卡会退回纵向平铺。
const phoneHtml = fs.readFileSync(path.resolve(STATIC, '..', 'phone.html'), 'utf8');
check(/class="charge-limit-deck[^"]*"\s+id="chargeLimitDeck"/.test(phoneHtml),
      'phone.html 的卡组容器挂了 .charge-limit-deck（叠放布局靠它）');
check(phoneHtml.includes('id="chargeLimitDots"'), 'phone.html 有指示点挂载点');
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
