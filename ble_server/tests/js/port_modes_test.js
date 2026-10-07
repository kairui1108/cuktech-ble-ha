#!/usr/bin/env node
/**
 * Unit tests for the port-mode part of web/static/charge_limit.js —
 * 长期供电（常供）/ 充满即停 两枚开关的数据契约与卡面结构。
 *
 * 为什么单独测这两枚开关：它们是"卡面上只放两三个字、解释走 title"的紧凑控件，
 * 卡面 HTML 是字符串拼出来的（onclick 依赖全局函数），请求形状又与限额不同
 * （布尔集合而非数值阈值、且互斥由后端强制）——这些都容易在改版时悄悄改坏，
 * 所以在这里把请求体、响应解析、互斥后的本地状态与 HTML 结构钉住。
 *
 * Usage: node tests/js/port_modes_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC = path.resolve(__dirname, '../../web/static');

let lastRequest = null;
let requestCount = 0;
let postCount = 0;   // 只数写请求：开启常供后还会顺带 GET 一次限额，那是另一回事
// holdFetch：让 fetch 停在 pending，用来测"在途时另一枚开关是否被挡"
let holdFetch = null;
let respond = () => ({ ok: true, permanent_ports: [], full_off_ports: {}, full_off_fired: [] });

// 极简 DOM 桩：charge_limit.js 只在"长期供电二次确认 / 在途反馈"里碰 DOM
// （给开关换个文字、加个类名）。这里按 id 造几个可断言的小元素即可。
function makeEl(id) {
    const cls = new Set();
    const el = {
        id, attrs: {},
        get className() { return [...cls].join(' '); },
        classList: {
            add: (c) => cls.add(c),
            remove: (c) => cls.delete(c),
            contains: (c) => cls.has(c),
            toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)),
        },
        setAttribute: (k, v) => { el.attrs[k] = String(v); },
        removeAttribute: (k) => { delete el.attrs[k]; },
        getAttribute: (k) => (k in el.attrs ? el.attrs[k] : null),
    };
    const text = { textContent: '' };
    el.querySelector = () => text;
    el._text = text;
    return el;
}

function load() {
    const sandbox = {
        console,
        location: { origin: 'http://example.invalid' },
        fetch: (url, opts) => {
            lastRequest = { url, opts };
            requestCount += 1;
            if (opts && opts.method === 'POST') postCount += 1;
            if (holdFetch) return holdFetch(url, opts);
            return Promise.resolve({
                status: 200,
                json: () => Promise.resolve(respond()),
            });
        },
    };
    const dom = {};
    sandbox.__dom = dom;
    // 定时器桩：二次确认的 4s 自动还原不该让测试真的等 4 秒；
    // 存下回调交给用例手动触发（见 __timers）。
    const timers = [];
    sandbox.__timers = timers;
    sandbox.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
    sandbox.clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].cancelled = true; };
    sandbox.document = {
        getElementById: (id) => dom[id] || null,
        createElement: () => makeEl('tmp'),
    };
    sandbox.window = sandbox;
    // 真实 I18N 的 t() 会插值；这里标记调用与参数，便于断言"文案确实走 i18n"
    sandbox.I18N = { t: (k, p) => (p ? `${k}:${JSON.stringify(p)}` : k) };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'charge_limit.js'), 'utf8'), sandbox);
    return sandbox;
}

let failed = 0;
let passed = 0;
function check(cond, label, detail) {
    if (cond) {
        passed++;
        console.log(`  ok   ${label}`);
    } else {
        failed++;
        console.log(`  FAIL ${label}${detail ? ': ' + detail : ''}`);
    }
}
function eq(actual, expected, label) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    check(a === e, label, `got ${a} want ${e}`);
}

(async () => {
    const sandbox = load();
    const CL = sandbox.ChargeLimit;
    const dom = sandbox.__dom;   // 极简 DOM 桩（开关的二次确认/在途反馈要动 DOM）

    console.log('\n-- 默认状态 --');
    eq(CL.modes.permanent_ports, [], '初始无常供端口');
    eq(CL.isPermanent('c1'), false, 'isPermanent 默认 false');
    eq(CL.isFullOff('c1'), false, 'isFullOff 默认 false');
    eq(CL.fullOffFired('c1'), false, 'fullOffFired 默认 false');

    console.log('\n-- fetchPortModes 解析 --');
    respond = () => ({
        ok: true,
        permanent_ports: ['c2'],
        full_off_ports: { c1: 'once', c3: 'always' },
        full_off_fired: ['c1'],
    });
    await CL.fetchPortModes();
    eq(lastRequest.url, 'http://example.invalid/api/port-modes', 'GET /api/port-modes');
    eq(CL.isPermanent('c2'), true, '常供端口已识别');
    eq(CL.isPermanent('c1'), false, '普通端口不是常供');
    eq(CL.isFullOff('c3'), true, '即停端口已识别');
    eq(CL.fullOffMode('c3'), 'always', '即停模式已识别');
    eq(CL.fullOffFired('c1'), true, '已充满断电标记透传');

    console.log('\n-- 响应异常时不抛异常、不污染状态 --');
    respond = () => ({ ok: false, error: 'boom' });
    const bad = await CL.fetchPortModes();
    eq(bad.error, 'boom', '错误信息保留');
    eq(CL.isPermanent('c2'), true, '失败不覆盖上一次的有效状态');

    console.log('\n-- savePortMode 请求形状 --');
    respond = () => ({
        ok: true, permanent_ports: ['c2'], full_off_ports: {}, full_off_fired: [],
    });
    const res = await CL.savePortMode('c2', 'permanent', true);
    eq(lastRequest.url, 'http://example.invalid/api/port-modes', 'POST /api/port-modes');
    eq(lastRequest.opts.method, 'POST', '用 POST');
    eq(JSON.parse(lastRequest.opts.body), { port: 'c2', permanent: true }, '请求体只带该字段');
    eq(res.ok, true, '成功返回 ok');
    eq(CL.isPermanent('c2'), true, '本地状态按响应更新');

    console.log('\n-- 后端 400（互斥/非法值）时如实报错 --');
    respond = () => ({ ok: false, error: 'port c2 is permanent power, full_off is not applicable' });
    const rejected = await CL.savePortMode('c2', 'full_off', true);
    eq(rejected.ok, false, '400 视为失败');
    eq(rejected.error.indexOf('not applicable') >= 0, true, '错误信息透传给提示条');
    eq(CL.isFullOff('c2'), false, '被拒后本地不误标为已开启');

    console.log('\n-- togglePortMode 取反 + 不弹成功提示 --');
    const notices = [];
    CL.setNotifier((m) => notices.push(m));
    respond = () => ({
        ok: true, permanent_ports: [], full_off_ports: {}, full_off_fired: [],
    });
    await CL.togglePortMode('c2', 'permanent');      // 当前为 true -> 关
    eq(JSON.parse(lastRequest.opts.body), { port: 'c2', permanent: false }, '再次点击为关闭');
    eq(notices.length, 0, '成功不打扰（卡面状态本身就是反馈）');
    respond = () => ({ ok: false, error: 'nope' });
    await CL.togglePortMode('c2', 'full_off');
    eq(notices.length, 1, '失败才提示');

    console.log('\n-- 卡面结构：卡头放"长期供电"，预设值那行放"充满即停" --');
    const html = CL.modeChipsHtml('c1');
    const fullChip = CL.chipHtml('c1', 'full_off', 'charge-limit-chip');
    check(html.indexOf('id="mode-permanent-c1"') >= 0, '长期供电开关带稳定 id');
    check(fullChip.indexOf('id="mode-full_off-c1"') >= 0, '自动按钮带稳定 id');
    check(fullChip.indexOf('class="charge-limit-chip limit-auto"') >= 0,
          '自动按钮用预设值同款类名（同一排等宽）');
    check(fullChip.indexOf('limit-mode-chip-dot') < 0, '自动按钮不带状态圆点（与预设值一致）');
    check(html.indexOf('mode-full_off-c1') < 0, '卡头那组只放长期供电（自动挪去预设值行）');
    check(html.indexOf('limit-mode-note') < 0 && html.indexOf('limitNote_') < 0,
          '不新增说明行（卡高必须与改版前一致）');
    check((html.match(/<div class="limit-modes">/g) || []).length === 1 &&
          (html.match(/limit-modes/g) || []).length === 1,
          '只返回一个 .limit-modes 包裹（挂进头部，不做块级新行）');
    check((html.match(/role="switch"/g) || []).length === 1 &&
          (fullChip.match(/role="switch"/g) || []).length === 1,
          '两个开关都是 role=switch（读屏可切换语义）');
    check((html.match(/aria-checked="false"/g) || []).length === 1 &&
          (fullChip.match(/aria-checked="false"/g) || []).length === 1, '初始 aria-checked=false');
    check(html.indexOf("togglePortMode('c1','permanent')") >= 0, 'onclick 调全局 togglePortMode（长期供电）');
    check(fullChip.indexOf("togglePortMode('c1','full_off')") >= 0, 'onclick 调全局 togglePortMode（充满即停）');
    check(html.indexOf('chargeLimit.permanent') >= 0 && fullChip.indexOf('chargeLimit.fullOff') >= 0,
          '标签走 i18n 键（不硬编码文案）');
    check(/title="[^"]+"/.test(html) && /title="[^"]+"/.test(fullChip),
          '解释文字在 title（卡面不放长句）');
    check(!/style="[^"]*(color|background)\s*:/i.test(html + fullChip),
          '无内联颜色（配色交给样式表/主题）');
    check(typeof sandbox.togglePortMode === 'function', 'togglePortMode 挂到全局（inline onclick 才找得到）');

    console.log('\n-- 长期供电：限额控件收起（CSS）+ 状态面板 --');
    const cssIndex = fs.readFileSync(path.join(STATIC, 'index.css'), 'utf8');
    const cssPhone = fs.readFileSync(path.join(STATIC, 'phone.css'), 'utf8');
    const histJs = fs.readFileSync(path.join(STATIC, 'charge_history.js'), 'utf8');
    const indexHtml = fs.readFileSync(path.join(STATIC, '..', 'index.html'), 'utf8');
    const phoneHtml = fs.readFileSync(path.join(STATIC, '..', 'phone.html'), 'utf8');
    const esc = (t) => t.replace(/\./g, '\\.');
    for (const [name, css, root, groups] of [
        ['index.css', cssIndex, '.charge-limit-item.is-permanent',
         ['.countdown-input-group', '.countdown-quick', '.countdown-actions',
          '.charge-limit-bar', '.charge-limit-progress', '.limit-auto']],
        ['phone.css', cssPhone, '.charge-limit-card.is-permanent',
         ['.charge-limit-inputs', '.charge-limit-quick', '.charge-limit-actions',
          '.charge-limit-track', '.charge-limit-progress', '.limit-auto']],
    ]) {
        for (const g of groups) {
            const re = new RegExp(esc(root) + '\\s+' + esc(g) + '[^{]*\\{[^}]*display:\\s*none');
            check(re.test(css), `${name}: 长期供电时收起 ${g}`);
        }
        check(new RegExp(esc(root) + '\\s+\\.limit-perm-panel\\s*\\{[^}]*display:\\s*flex').test(css),
              `${name}: 长期供电卡显示状态面板`);
        // 面板里每一层都要有对应样式：主读数 / 状态点 / 发丝线 + 指标 / 说明
        check(/\.limit-perm-power\s*\{[^}]*font-variant-numeric:\s*tabular-nums/.test(css),
              `${name}: 主读数是等宽数字（刷新不左右抖）`);
        check(/\.limit-perm-power\.is-idle\s*\{[^}]*color/.test(css),
              `${name}: 待机时主读数转次级墨色（位置不变）`);
        check(/\.limit-perm-state/.test(css) === false, `${name}: 面板里不再有"供电中"状态标记`);
        check(/\.is-live(::before)?\s*\{[^}]*animation:\s*dot-breathe/.test(css),
              `${name}: 端口活跃时卡头状态点走呼吸动效`);
        check(/@keyframes dot-breathe/.test(css)
              && /prefers-reduced-motion[\s\S]{0,260}animation:\s*none/.test(css),
              `${name}: 有呼吸关键帧，且"降低动效"偏好下降级为静态光环`);
        check(/\.limit-perm-metrics\s*\{[^}]*border-top/.test(css),
              `${name}: 指标区有发丝线分隔（层级不靠降对比度）`);
        check(/\.limit-perm-k\s*\{[^}]*var\(--text-sub\)/.test(css)
              && /\.limit-perm-note\s*\{[^}]*var\(--text-sub\)/.test(css),
              `${name}: 标签/说明用 --text-sub（两主题都过 AA，不用 --text-dim）`);
        check(/\.limit-perm-panel\s*\{\s*display:\s*none/.test(css),
              `${name}: 面板默认不占位（非长期供电卡不受影响）`);
    }
    check(histJs.indexOf('ch-perm-badge') < 0 && cssIndex.indexOf('ch-perm-badge') < 0
          && cssPhone.indexOf('ch-perm-badge') < 0,
          '记录行不再有"长期供电"徽标（状态只在限额卡体现）');
    check(histJs.indexOf('data-permanent') >= 0, '记录行仍带 data-permanent（详情浮层据此进窗口模式，不参与视觉）');
    // 桌面的圆点是 .countdown-port 的 ::before：动画/光环必须挂在伪元素上，
    // 挂在宿主元素上 box-shadow 会画成围着"● C2"的矩形（实测踩过）。
    check(/\.countdown-port\.is-live::before\s*\{/.test(cssIndex),
          'index: 呼吸动效只作用于圆点伪元素（不会出现矩形光晕）');
    check(!/\.countdown-port\.is-live\s*\{/.test(cssIndex),
          'index: 不得直接动画 .countdown-port 本身');

    console.log('\n-- hover 微浮动（限额卡 / 倒计时卡） --');
    // 卡组里卡片的站位是 transform: translateY(--depth)，翻页飞出与跟手拖拽也都往
    // 这一条上写；抬升若也写 transform 就会顶掉站位（牌堆当场塌）。所以这里把
    // "只能走 translate 属性"钉死，并要求两条通道都在过渡表里。
    for (const [name, css, base, hover] of [
        // 桌面两张卡的样式是同一组选择器（限额 / 倒计时），所以块头也一起匹配
        ['index.css', cssIndex,
         '\\.charge-limit-item,\\s*\\.countdown-item\\s*\\{',
         '\\.charge-limit-item:hover,\\s*\\.countdown-item:hover\\s*\\{'],
        ['phone.css', cssPhone, '\\.charge-limit-card\\s*\\{', '\\.charge-limit-card:hover\\s*\\{'],
    ]) {
        check(/@media\s*\(hover:\s*hover\)\s*and\s*\(pointer:\s*fine\)/.test(css),
              `${name}: 微浮动限定在能真 hover 的设备（触摸屏不残留粘住的 hover 态）`);
        check(new RegExp(hover + '[^}]*translate:\\s*0\\s+(?:-?\\d|var\\()').test(css),
              `${name}: hover 用独立的 translate 属性抬升`);
        check(!new RegExp(hover + '[^}]*transform:\\s*translate').test(css),
              `${name}: hover 不得改写 transform（会顶掉牌堆的 --depth 站位）`);
        check(new RegExp(base + '[^}]*transition:[^;]*translate').test(css),
              `${name}: translate 进了过渡表（抬升/落回是滑的，不是瞬移）`);
        check(/\.deck\s*\{[^}]*padding-top|\.charge-limit-deck\s*\{[^}]*padding-top/.test(css),
              `${name}: 卡组顶部预留抬升空档（overflow: hidden 不会切掉抬起来的那条边）`);
    }
    check(/\.charge-limit-item:hover,\s*\.countdown-item:hover/.test(cssIndex),
          'index: 限额卡与倒计时卡共用同一条微浮动规则');
    check(/\.deck\s*\{[^}]*padding-top:\s*calc\(var\(--card-lift\)\s*\*\s*-1\)/.test(cssIndex),
          'index: 预留量跟 --card-lift 联动（深浅两套主题各自对齐）');
    check(/\.charge-limit-deck\s*\{[^}]*padding-top:\s*4px/.test(cssPhone),
          'phone: 预留量 = 抬升 3px + 外圈描边 1px');

    console.log('\n-- 状态面板内容（供电中 / 待机） --');
    respond = () => ({
        ok: true, permanent_ports: ['c1'], full_off_ports: {}, full_off_fired: [],
    });
    const nowSec = Math.floor(Date.now() / 1000);
    // 平均功率/会话时长由服务端给（服务端知道会话结束时刻，前端只靠挂钟现算会在
    // 会话结束后把平均值越算越小）
    CL.state.limits = { c1: { wh: 0, mode: 'once', session_wh: 3.42, session_sec: 7200,
                              avg_power_w: 12.6, is_charging: true,
                              fired: false, session_start: nowSec - 7200 } };
    await CL.fetchPortModes();
    const live = CL.permanentPanelHtml('c1');
    // 主读数改成**统计值**（本会话平均功率），不是实时功率：实时值每帧都在跳
    check(/>12\.6<span class="limit-perm-u">W<\/span>/.test(live), '主读数=平均功率（12.6 W）');
    check(live.indexOf('chargeLimit.permanentAvg') >= 0, '主读数标着"平均功率"');
    check(live.indexOf('chargeLimit.permanentAvgLast') < 0, '供电中不写"上次平均"');
    check(live.indexOf('limit-perm-state') < 0 && live.indexOf('chargeLimit.permanentOn') < 0,
          '不再有"供电中"状态标记（在不在供电由卡头呼吸点表达）');
    check(live.indexOf('limit-perm-vi') < 0, '不放实时 V/A（面板只留统计值）');
    check(live.indexOf('chargeLimit.permanentOut') >= 0
          && /<span class="limit-perm-v">3\.4<span class="limit-perm-u">Wh<\/span>/.test(live),
          '本次已输出能量（会话级累计）作为指标');
    check(live.indexOf('chargeLimit.permanentDurLabel') >= 0
          && live.indexOf('chargeLimit.permanentDurHour') >= 0,
          '已持续时长用服务端给的 session_sec（7200s → 2 小时，不再用挂钟现算）');
    check(live.indexOf('chargeLimit.permanentNote') >= 0, '带一行"不记曲线"说明');
    check(live.indexOf('<') === 0 && (live.match(/</g) || []).length === (live.match(/>/g) || []).length,
          '面板 HTML 标签闭合');

    // 时长分档：<24h 报到分钟；≥24h 只报到"天+小时"（分钟在按天计的常供会话里没有意义，
    // 而且 "23 小时 59 分" 那串在桌面卡片的等分列里放不下、会被省略号截掉）；
    // ≥100 天再省掉小时（三个多月后小时同样没有信息量）。
    const dur = (sec) => {
        CL.state.limits.c1 = { wh: 0, mode: 'once', session_wh: 1, session_sec: sec,
                               avg_power_w: 5, is_charging: true, fired: false };
        const html = CL.permanentPanelHtml('c1');
        // 取"值"那一处（标签 permanentDurLabel 先出现，别匹配到它）；面板里的文案
        // 走 esc()，引号是 &quot;，比对前先还原
        const m = html.match(/chargeLimit\.permanentDur(?:Min|Hour|DayOnly|Day):[^<]*/);
        return m ? m[0].replace(/&quot;/g, '"') : '';
    };
    check(dur(1800).indexOf('chargeLimit.permanentDurMin:{"m":30}') === 0, '30 分钟：只报分钟');
    check(dur(7200).indexOf('chargeLimit.permanentDurHour:{"h":2,"m":0}') === 0, '2 小时：报小时+分钟');
    check(dur(23 * 3600 + 59 * 60).indexOf('chargeLimit.permanentDurHour:{"h":23,"m":59}') === 0,
          '23 小时 59 分：还没到一天，仍报小时+分钟');
    check(dur(24 * 3600).indexOf('chargeLimit.permanentDurDay:{"d":1,"h":0}') === 0, '24 小时整：进位成 1 天 0 小时');
    check(dur(2 * 86400 + 3 * 3600 + 59 * 60).indexOf('chargeLimit.permanentDurDay:{"d":2,"h":3}') === 0,
          '2 天 3 小时 59 分：只报到天+小时，分钟丢掉（不四舍五入成 4 小时）');
    check(dur(99 * 86400 + 23 * 3600).indexOf('chargeLimit.permanentDurDay:{"d":99,"h":23}') === 0,
          '99 天 23 小时：仍在"天+小时"档');
    check(dur(100 * 86400 + 5 * 3600).indexOf('chargeLimit.permanentDurDayOnly:{"d":100}') === 0,
          '100 天：省掉小时（再往后写会被省略号截断）');
    check(dur(3650 * 86400).indexOf('chargeLimit.permanentDurDayOnly:{"d":3650}') === 0, '10 年：只报天数');
    // 面板里不会再出现"小时"档的分钟字段——那正是这条改动的目的
    check(dur(5 * 86400).indexOf('"m"') < 0, '天档不带分钟字段（模板里也就没有分钟可渲染）');

    CL.state.limits.c1 = { wh: 0, mode: 'once', session_wh: 3.42, session_sec: 7200,
                           avg_power_w: 12.6, is_charging: false,
                           fired: false, session_start: null };
    const idle = CL.permanentPanelHtml('c1');
    check(idle.indexOf('limit-perm-power is-idle') >= 0, '待机：主读数转次级墨色');
    check(/>12\.6<span class="limit-perm-u">W<\/span>/.test(idle),
          '待机：仍显示上一段的平均功率（服务端按 能量÷时长 算，结束后不漂）');
    check(idle.indexOf('chargeLimit.permanentAvgLast') >= 0, '待机：标签改"上次平均功率"（否则会被读成当下）');
    check(idle.indexOf('chargeLimit.permanentLastOut') >= 0, '待机：改报上次输出能量');
    check(idle.indexOf('chargeLimit.permanentDurLabel') < 0, '待机：不再报时长');

    CL.state.limits.c1 = { wh: 0, mode: 'once', session_wh: 0, session_sec: 0, avg_power_w: 0,
                           is_charging: false, fired: false, session_start: null };
    const never = CL.permanentPanelHtml('c1');
    check(never.indexOf('chargeLimit.permanentNoOutput') >= 0, '从未输出过：报"暂无输出"而不是 0.0 Wh');

    console.log('\n-- 普通端口不受面板影响 --');
    CL.state.limits = { c1: { wh: 30, mode: 'once', session_wh: 3.42, is_charging: true, fired: false } };
    eq(CL.progressText('c1'), 'chargeLimit.progress:{"used":"3.4","total":30}', '普通端口仍报"已充 / 限额"');
    eq(CL.isPermanent('c1'), true, '（此时 c1 仍是长期供电，仅文案函数独立于模式）');

    console.log('\n-- 充满即停：和限额一样的 once/always + 状态/进度/状态点 --');
    respond = () => ({
        ok: true, permanent_ports: [], full_off_ports: { c1: 'always' }, full_off_fired: [],
    });
    CL.state.limits = { c1: { wh: 0, mode: 'once', session_wh: 5.25, is_charging: true, fired: false } };
    await CL.fetchPortModes();
    eq(CL.isFullOff('c1'), true, 'isFullOff 认字典形状');
    eq(CL.fullOffMode('c1'), 'always', '模式可取');
    eq(CL.isArmed('c1'), true, '只开充满即停也算"已配置"');
    eq(CL.statusText('c1'), 'chargeLimit.enabled', '状态文字：已启用（不再是未启用）');
    eq(CL.progressText('c1'), 'chargeLimit.used:{"used":"5.3"}', '进度文字：已充 X Wh（没有目标值）');
    eq(CL.progressPct('c1'), 100, '供电中进度条填满（无目标值，表示已武装）');
    CL.state.limits.c1.is_charging = false;
    eq(CL.progressPct('c1'), 0, '空闲时进度条空槽');
    respond = () => ({
        ok: true, permanent_ports: [], full_off_ports: {}, full_off_fired: ['c1'],
    });
    await CL.fetchPortModes();
    eq(CL.isArmed('c1'), false, 'once 被消费后回到未配置');
    eq(CL.isFired('c1'), true, '但保留"已充满断电"标记');
    // 两个来源（能量限额 / 充满即停）合并成一个"已触发"后缀：分开写会拼出
    // "已启用 · 已触发 · 已充满停止"这种重复又矛盾的状态。
    eq(CL.statusText('c1'), 'chargeLimit.off · chargeLimit.fired',
       '状态文字同时报未启用与已充满断电');
    CL.state.limits.c1.fired = true;
    eq(CL.statusText('c1'), 'chargeLimit.off · chargeLimit.fired',
       '两条规则共用一个"已触发"后缀（不重复）');

    console.log('\n-- 常供口在下方状态点上也要亮（与设了限额/自动一样） --');
    CL.modes.permanent_ports = ['c1'];
    CL.modes.full_off_ports = {};
    CL.modes.full_off_fired = [];        // 前面的用例留过"已触发"，这里要干净起点
    CL.state.limits = { c1: { wh: 0, mode: 'once', session_wh: 0, is_charging: true, fired: false } };
    eq(CL.isArmed('c1'), true, '只设了长期供电也算"已配置"（状态点要实心）');
    eq(CL.statusText('c1'), 'chargeLimit.enabled', '状态文字同源：已启用');
    eq(CL.isFired('c1'), false, '没触发过，不该是警示色');
    eq(CL.progressText('c1'), '', '常供口不显示限额进度（卡面另有状态面板）');
    CL.modes.permanent_ports = [];
    eq(CL.isArmed('c1'), false, '取消常供且没别的设置 → 回到空心');

    console.log('\n-- once 断电被消费后：只报「未启用」，不再挂「已触发」 --');
    // 后端消费后会给出 full_off_ports {} + full_off_fired []（服务端权威）。
    // 界面必须跟着回到"未启用"：否则卡片显示自相矛盾的"未启用 · 已触发"，
    // 底部状态点也一直停在警示色。
    CL.state.limits.c1 = { wh: 0, mode: 'once', session_wh: 1.2,
                           is_charging: false, fired: false };
    respond = () => ({ ok: true, permanent_ports: [], full_off_ports: {}, full_off_fired: [] });
    await CL.fetchPortModes();
    eq(CL.isFullOff('c1'), false, '即停已消费（关闭）');
    eq(CL.fullOffFired('c1'), false, '已触发标记随之清除');
    eq(CL.isArmed('c1'), false, '不再算"已配置"（能量限额也为 0）');
    eq(CL.isFired('c1'), false, '状态点不该再是警示色');
    eq(CL.statusText('c1'), 'chargeLimit.off', '状态文字只有"未启用"');
    eq(CL.progressText('c1'), '', '没有限额也没有即停 → 不显示进度文字');

    console.log('\n-- 充满即停：请求带模式 + 位置在预设值那一行 --');
    respond = () => ({
        ok: true, permanent_ports: [], full_off_ports: { c1: 'once' }, full_off_fired: [],
    });
    CL.setModeReader(() => 'once');
    CL.modes.full_off_ports = { c1: 'once' };   // 已开启
    await CL.togglePortMode('c1', 'full_off');
    eq(JSON.parse(lastRequest.opts.body), { port: 'c1', full_off: false },
       '已开启时再点是关闭（不带 mode）');
    CL.modes.full_off_ports = {};
    await CL.togglePortMode('c1', 'full_off');
    eq(JSON.parse(lastRequest.opts.body), { port: 'c1', full_off: true, mode: 'once' },
       '开启时把卡面选中的 once/always 一起提交');
    CL.setModeReader(() => 'always');
    CL.modes.full_off_ports = {};
    await CL.togglePortMode('c1', 'full_off');
    eq(JSON.parse(lastRequest.opts.body), { port: 'c1', full_off: true, mode: 'always' },
       '模式跟着下拉走');

    const appJs = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');
    const phoneJs = fs.readFileSync(path.join(STATIC, 'phone.js'), 'utf8');
    console.log('\n-- 卡头状态点：活跃时呼吸（两页都要挂这个类） --');
    // 两个页面的这段都在"没有外层循环 entry 变量"的函数作用域里：必须自己
    // entryFor(key)。曾经桌面页写成裸 e 抛 ReferenceError，中断了整段
    // updateChargeLimitUI —— 面板不填、开关状态也回写不上，卡片变成空卡。
    check(/\.querySelector\('\.countdown-port'\)/.test(appJs)
          && /classList\.toggle\('is-live', !!CL\.entryFor\(key\)\.is_charging\)/.test(appJs),
          'index：卡头端口点按 is_charging 切 is-live（用 entryFor，不要裸 e）');
    check(!/classList\.toggle\('is-live', !!e\.is_charging\)/.test(appJs),
          'index：不得再出现裸 e.is_charging（作用域里没有 e）');
    check(/\.querySelector\('\.charge-limit-dot'\)/.test(phoneJs)
          && /classList\.toggle\('is-live', !!CL\.entryFor\(key\)\.is_charging\)/.test(phoneJs),
          'phone：同上');
    for (const [name, src, rowSel] of [['app.js', appJs, 'countdown-quick'],
                                       ['phone.js', phoneJs, 'charge-limit-quick']]) {
        const row = src.slice(src.indexOf(rowSel));
        check(row.indexOf("chipHtml(key, 'full_off'") >= 0 &&
              row.indexOf("chipHtml(key, 'full_off'") < row.indexOf('</div>'),
              `${name}: 自动按钮在预设值那一行（${rowSel}）`);
        check(src.indexOf("'full_off', 'charge-limit-chip')") >= 0 ||
              src.indexOf("'full_off', 'countdown-quick-btn')") >= 0,
              `${name}: 自动按钮借用预设值的样式类`);
        check(src.indexOf("setModeReader") >= 0, `${name}: 注册了模式读取（点击时带 once/always）`);
        check(src.indexOf('isArmed(key)') >= 0, `${name}: 状态/进度/状态点用统一的"已配置"判定`);
    }
    const zhLocale = fs.readFileSync(path.join(STATIC, 'locales/zh-CN.js'), 'utf8');
    check(/fullOff: '自动'/.test(zhLocale), "按钮文案改成「自动」");
    check(indexHtml.indexOf('ChargeLimit.isArmed') >= 0 && indexHtml.indexOf('ChargeLimit.isFired') >= 0,
          'index.html 的状态点也认充满即停（不再只看 wh>0）');
    // 模式下拉的保护位：两页都要有（否则 5s 轮询会把用户改了还没保存的选择打回去）
    for (const [name, src] of [['app.js', appJs], ['phone.js', phoneJs]]) {
        check(/onchange="limitModeTouched\('/.test(src), `${name}: 模式下拉 onchange 置保护位`);
        check(src.indexOf('dataset.touched') >= 0 && src.indexOf('modeSaved') >= 0,
              `${name}: 保护位生效 + 只改模式也能保存`);
    }

    console.log('\n-- 在途保护按 port+field 占坑（两枚开关互不阻塞） --');
    CL.setNotifier(() => {});
    let pendingResolvers = [];
    holdFetch = () => new Promise((resolve) => {
        pendingResolvers.push(resolve);
    });
    const releaseAll = () => {
        const rs = pendingResolvers;
        pendingResolvers = [];
        rs.forEach((r) => r({
            status: 200,
            json: () => Promise.resolve(
                { ok: true, permanent_ports: ['c3'], full_off_ports: {}, full_off_fired: [] }),
        }));
    };
    const before = requestCount;
    const p1 = CL.savePortMode('c3', 'permanent', true);
    const p2 = CL.savePortMode('c3', 'full_off', true);   // 同一端口的另一枚开关
    eq(requestCount - before, 2, '两枚开关的请求都发出去了（不静默丢掉最后一次操作）');
    releaseAll();
    await p1;
    await p2;
    holdFetch = null;

    console.log('\n-- 同一枚开关在途时返回 pending（重复点击可静默忽略） --');
    pendingResolvers = [];
    holdFetch = () => new Promise((resolve) => { pendingResolvers.push(resolve); });
    const releaseRest = () => {
        const rs = pendingResolvers;
        pendingResolvers = [];
        rs.forEach((r) => r({
            status: 200,
            json: () => Promise.resolve(
                { ok: true, permanent_ports: [], full_off_ports: {}, full_off_fired: [] }),
        }));
    };
    const q1 = CL.savePortMode('c3', 'permanent', false);
    const q2 = CL.savePortMode('c3', 'permanent', false);   // 同一枚，重复点击
    const q2res = await q2;
    eq(q2res.ok, false, '重复点击返回 not-ok');
    eq(q2res.error, 'pending', '并带 pending 标记（调用方据此静默忽略）');
    releaseRest();
    await q1;
    holdFetch = null;
    // 释放后同一枚开关还能再用（同步抛错不会永久卡住）
    respond = () => ({ ok: true, permanent_ports: [], full_off_ports: {}, full_off_fired: [] });
    const q3 = await CL.savePortMode('c3', 'permanent', true);
    eq(q3.ok, true, '请求结束后保护位释放，同一枚开关可再次提交');

    console.log('\n-- 卡头那枚"长期供电"是开关，不是徽标 --');
    const switchHtml = CL.chipHtml('c1', 'permanent');
    check(switchHtml.indexOf('class="limit-perm-switch"') >= 0, '用开关样式（轨道+滑块）');
    check(switchHtml.indexOf('limit-perm-switch-track') >= 0, '有轨道元素');
    check(switchHtml.indexOf('limit-mode-chip') < 0, '不再是胶囊徽标样式（那看着像状态标签）');
    check(switchHtml.indexOf('role="switch"') >= 0 && switchHtml.indexOf('aria-checked') >= 0,
          '保持 switch 语义（读屏可播报开关状态）');
    check(switchHtml.indexOf('id="mode-permanent-c1"') >= 0, 'id 不变（两页的更新逻辑仍找得到）');

    console.log('\n-- 有额度时开启常供要二次确认（后端会清掉该口限额） --');
    dom['mode-permanent-c1'] = makeEl('mode-permanent-c1');
    CL.state.limits = { c1: { wh: 20, mode: 'once', session_wh: 1, is_charging: false, fired: false } };
    CL.modes.permanent_ports = [];
    const reqBefore = postCount;
    const first = await CL.togglePortMode('c1', 'permanent');
    eq(postCount - reqBefore, 0, '第一次点击不发请求（只进入确认态）');
    eq(first.error, 'confirm', '返回 confirm 而不是失败');
    eq(dom['mode-permanent-c1'].classList.contains('is-confirm'), true, '控件进入确认态');
    eq(dom['mode-permanent-c1']._text.textContent, 'chargeLimit.permanentConfirm', '文案换成"再点一次确认"');

    respond = () => ({ ok: true, permanent_ports: ['c1'], full_off_ports: {}, full_off_fired: [] });
    const second = await CL.togglePortMode('c1', 'permanent');
    eq(second.ok, true, '第二次点击才真的开启');
    eq(postCount - reqBefore, 1, '只发了这一次写请求');
    eq(dom['mode-permanent-c1'].classList.contains('is-confirm'), false, '确认态已解除');
    eq(dom['mode-permanent-c1']._text.textContent, 'chargeLimit.permanent', '文案回到"长期供电"');

    console.log('\n-- 确认态超时会自动还原（不会一直挂着等第二次点击） --');
    dom['mode-permanent-c1'] = makeEl('mode-permanent-c1');
    CL.state.limits = { c1: { wh: 20, mode: 'once', session_wh: 1, is_charging: false, fired: false } };
    CL.modes.permanent_ports = [];
    await CL.togglePortMode('c1', 'permanent');            // 进入确认态
    eq(dom['mode-permanent-c1'].classList.contains('is-confirm'), true, '先处于确认态');
    const pending = sandbox.__timers.filter((t) => !t.cancelled).pop();
    check(!!pending && pending.ms === 4000, '确认态挂了 4s 的还原定时器');
    pending.fn();
    eq(dom['mode-permanent-c1'].classList.contains('is-confirm'), false, '超时后自动解除');
    eq(dom['mode-permanent-c1']._text.textContent, 'chargeLimit.permanent', '文案也还原');

    console.log('\n-- 没有额度时不必二次确认（常见路径仍然一次点到） --');
    CL.state.limits = { c1: { wh: 0, mode: 'once', session_wh: 0, is_charging: false, fired: false } };
    dom['mode-permanent-c1'] = makeEl('mode-permanent-c1');
    CL.modes.permanent_ports = [];
    const b2 = postCount;
    respond = () => ({ ok: true, permanent_ports: ['c1'], full_off_ports: {}, full_off_fired: [] });
    const once = await CL.togglePortMode('c1', 'permanent');
    eq(once.ok, true, '一次点击即开启');
    eq(postCount - b2, 1, '写请求直接发出（无需二次确认）');

    console.log('\n-- 在途反馈：请求期间置 aria-busy，回来再撤 --');
    dom['mode-permanent-c2'] = makeEl('mode-permanent-c2');
    CL.modes.permanent_ports = ['c2'];
    let release = null;
    holdFetch = () => new Promise((res) => { release = () => res({ status: 200,
        json: () => Promise.resolve({ ok: true, permanent_ports: [], full_off_ports: {}, full_off_fired: [] }) }); });
    const inflight = CL.togglePortMode('c2', 'permanent');
    eq(dom['mode-permanent-c2'].getAttribute('aria-busy'), 'true', '在途时 aria-busy=true');
    release();
    await inflight;
    eq(dom['mode-permanent-c2'].getAttribute('aria-busy'), null, '返回后撤掉 aria-busy');
    holdFetch = null;

    console.log('\n-- 常供详情：没有窗口长度控件，窗口固定为服务端保留时长 --');
    check(!/sdWindowBar|sd-window|setWindowSize|shiftSessionWindow|showSessionWindowLatest/.test(histJs),
          '窗口长度 / 左右滑动 / 最新 这套控件已彻底移除');
    check(!/window=\$\{|to=\$\{|push\('window/.test(histJs),
          '取点请求不再带 window/to（服务端缺省就是"保留 1 小时 + 贴最新"）');
    check(/\.sd-no-curve/.test(cssIndex) && !/\.sd-window/.test(cssIndex)
          && /\.sd-no-curve/.test(cssPhone) && !/\.sd-window/.test(cssPhone),
          '两页样式都已换成"没有曲线"说明，窗口控件样式清干净');
    check(/setSessionChartVisible\(false\)/.test(histJs)
          && /setSessionChartVisible\(true\)/.test(histJs),
          '没有曲线时收起定高图表区（弹窗里最大的一块空白），有曲线再恢复');
    check(/ensureNoCurveNote/.test(histJs), '仍然说明"为什么没有曲线"');
    // 导出端点读的是库里的点；常供会话不落库（曲线只在内存）→ 导出必是空表；
    // 精度（下采样）同样只对曲线有意义。两者一起按"有没有曲线"收放。
    check(/setCurveControlsVisible\(false\)/.test(histJs)
          && /setCurveControlsVisible\(!_dsPermanent\)/.test(histJs),
          '常供会话 / 没有采样点的会话不再显示"精度"与"导出 CSV"');
    check(/\.sd-curve-only/.test(histJs), '靠共享标记类挂钩子（两页各自标注）');
    for (const [name, html] of [['index.html', indexHtml], ['phone.html', phoneHtml]]) {
        check(/sd-curve-only/.test(html), `${name}: 标了 .sd-curve-only`);
    }
    check((indexHtml.match(/sd-curve-only/g) || []).length === 2,
          'index: 精度整块 + 导出按钮各一处');
    check(/\.sd-accuracy\.sd-curve-only|sd-accuracy sd-curve-only/.test(indexHtml)
          && !/\.sd-accuracy\.sd-curve-only/.test(cssIndex),
          'index: "精度"是整块（标签+下拉）一起收，不是只藏下拉');

    console.log('\n-- 卡头：去掉"未启用/已启用"文字，开关钉在右端 --');
    for (const [name, src, css] of [['app.js', appJs, cssIndex], ['phone.js', phoneJs, cssPhone]]) {
        // 卡头只留"端口名 + 长期供电开关"：原先那行"未启用/已启用"文字已删除。
        // 开关靠 margin-left:auto 钉在右端，所以切常供（卡片收起限额控件）时位置不变。
        check(!/charge-limit-status/.test(src), `${name}: 卡头不再渲染状态文字元素`);
        check(!/limitStatus_|limit-status-/.test(src), `${name}: 也不再更新那个元素`);
        check(/\.limit-modes\s*\{[^}]*margin-left:\s*auto/.test(css),
              `${name}: 开关靠 margin-left:auto 钉在右端（切常供时位置不变）`);
        check(!/\.charge-limit-status/.test(css), `${name}: 状态文字样式已清掉`);
    }

    console.log('\n-- 未知端口安全 --');
    eq(CL.isPermanent(undefined), false, 'undefined 端口不抛异常');
    // 未知端口不生成按钮：port 会拼进 onclick（可执行代码，转义引号挡不住注入），
    // 白名单外的名字一律拒绝，而不是"照常拼出不抛异常"。
    eq(CL.chipHtml('zz', 'permanent'), '', '未知端口不生成开关');
    eq(CL.modeChipsHtml('zz').indexOf('mode-permanent-zz') >= 0, false,
       '未知端口不出现在卡头（不抛、也不拼 HTML）');

    console.log(`\n${passed} passed, ${failed} failed`);
    process.exit(failed === 0 ? 0 : 1);
})();
