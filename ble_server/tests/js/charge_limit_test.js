#!/usr/bin/env node
/**
 * Unit tests for web/static/charge_limit.js — the shared charge-limit card logic.
 *
 * The card is rendered by two different pages (index.html / phone.html) with
 * different DOM and CSS, so the logic lives in one module and is exercised here
 * in isolation with a real I18N stub and a stub fetch.
 *
 * Usage: node tests/js/charge_limit_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC = path.resolve(__dirname, '../../web/static');

function load() {
    const sandbox = {
        console,
        location: { origin: 'http://example.invalid' },
        fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    };
    sandbox.window = sandbox;
    // 真实 I18N 的 t() 会插值；这里只标记调用与参数，便于断言断言
    sandbox.I18N = { t: (k, p) => (p ? `${k}:${JSON.stringify(p)}` : k) };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'charge_limit.js'), 'utf8'), sandbox);
    return sandbox;
}

let failed = 0;
let passed = 0;
function eq(actual, expected, label) {
    if (JSON.stringify(actual) === JSON.stringify(expected)) {
        passed++;
        console.log(`  ok   ${label}`);
    } else {
        failed++;
        console.log(`  FAIL ${label}: got ${JSON.stringify(actual)} want ${JSON.stringify(expected)}`);
    }
}

const sandbox = load();
const CL = sandbox.ChargeLimit;

console.log('\n-- parseWhInput (非法输入必须在客户端拦下，不发给后端) --');
eq(CL.parseWhInput('30'), 30, "parseWhInput('30')");
eq(CL.parseWhInput(0), 0, "parseWhInput(0) 表示关闭");
eq(CL.parseWhInput('0'), 0, "parseWhInput('0')");
eq(CL.parseWhInput(''), null, "空字符串 -> null");
eq(CL.parseWhInput('   '), null, "纯空格 -> null（Number(' ')=0 会误判成关闭）");
eq(CL.parseWhInput('abc'), null, "'abc' -> null");
eq(CL.parseWhInput('-5'), null, '负数 -> null');
eq(CL.parseWhInput(null), null, 'null -> null');
eq(CL.parseWhInput(undefined), null, 'undefined -> null');
eq(CL.parseWhInput(Infinity), null, 'Infinity -> null');
eq(CL.parseWhInput(-Infinity), null, '-Infinity -> null');
eq(CL.parseWhInput(NaN), null, 'NaN -> null');

console.log('\n-- 未设限额 --');
CL.state.limits = { c1: { wh: 0, mode: 'once', session_wh: 0, is_charging: false, fired: false } };
eq(CL.progressText('c1'), '', '未设限额不显示进度');
eq(CL.statusText('c1'), 'chargeLimit.off', '状态显示关闭');
eq(CL.progressPct('c1'), 0, '进度 0');

console.log('\n-- 已设限额 + 充电中 --');
CL.state.limits.c1 = { wh: 30, mode: 'once', session_wh: 12.34, is_charging: true, fired: false };
eq(CL.progressPct('c1'), 12.34 / 30 * 100, '进度百分比（未取整，直接用于 CSS 宽度）');
eq(CL.statusText('c1'), 'chargeLimit.enabled', '已设限额：状态显示"已启用"（模式由下拉体现）');
eq(CL.progressText('c1'), 'chargeLimit.progress:{"used":"12.3","total":30}', '进度文案带已充/限额');

console.log('\n-- always 模式与已触发 --');
CL.state.limits.c1 = { wh: 30, mode: 'always', session_wh: 30, is_charging: true, fired: true };
eq(CL.statusText('c1'), 'chargeLimit.enabled · chargeLimit.fired', '状态：已启用 + 已触发');
eq(CL.progressPct('c1'), 100, '进度封顶 100');

console.log('\n-- 只开充满即停（没有能量限额） --');
// 状态/进度/卡头状态点共用 isArmed / isFired：这两个判定漏掉 full_off
// 的话，只开"自动"的端口会显示成"未启用"，而测试仍然全绿。
CL.state.limits = { c1: { wh: 0, mode: 'once', session_wh: 3.5, is_charging: true, fired: false } };
CL.modes.permanent_ports = [];
CL.modes.full_off_ports = { c1: 'once' };
CL.modes.full_off_fired = [];
eq(CL.isArmed('c1'), true, '只开即停也算"已配置"');
eq(CL.statusText('c1'), 'chargeLimit.enabled', '只开即停：状态显示已启用');
eq(CL.progressText('c1'), 'chargeLimit.used:{"used":"3.5"}', '没有目标值时只报已充');
eq(CL.progressPct('c1'), 100, '供电中把槽填满（表示已武装）');
CL.state.limits.c1.is_charging = false;
eq(CL.progressPct('c1'), 0, '空闲时空槽');

console.log('\n-- 已触发合并成一个后缀（不重复） --');
CL.state.limits.c1 = { wh: 0, mode: 'once', session_wh: 3.5, is_charging: false, fired: false };
CL.modes.full_off_fired = ['c1'];
eq(CL.isFired('c1'), true, 'full_off 命中也算已触发');
// 仍开着即停（isArmed 为真），所以前缀是 enabled，但"已触发"只挂一次
eq(CL.statusText('c1'), 'chargeLimit.enabled · chargeLimit.fired', '只挂一个"已触发"后缀');
CL.state.limits.c1.fired = true;
eq(CL.statusText('c1'), 'chargeLimit.enabled · chargeLimit.fired',
   '两个来源都命中也只挂一次（避免"已触发 · 已充满停止"）');

console.log('\n-- 长期供电卡的状态面板：只放统计值 --');
CL.modes.full_off_fired = [];
// 平均功率 / 会话时长都来自服务端（/api/charge-limits 的 avg_power_w / session_sec）
CL.state.limits.c1 = { wh: 0, mode: 'once', session_wh: 12.5, session_sec: 7200,
                       avg_power_w: 6.25, is_charging: true,
                       fired: false, session_start: Math.floor(Date.now() / 1000) - 7200 };
const check = (cond, label) => eq(!!cond, true, label);
const panel = CL.permanentPanelHtml('c1');
check(/>6\.3<span class="limit-perm-u">W<\/span>/.test(panel), '主读数=平均功率（1 位小数 + 单位）');
check(panel.indexOf('chargeLimit.permanentAvg') >= 0, '标着"平均功率"（不会被读成实时值）');
check(panel.indexOf('limit-perm-state') < 0, '没有"供电中"状态标记');
check(panel.indexOf('limit-perm-vi') < 0, '没有实时 V/A 行');
check(/<span class="limit-perm-v">12\.5<span class="limit-perm-u">Wh<\/span>/.test(panel),
      '本次已输出能量作为指标');
check(panel.indexOf('chargeLimit.permanentDurHour') >= 0, '已持续时长走 i18n（2 小时，取服务端 session_sec）');
check(panel.indexOf('limit-perm-metrics') >= 0 && panel.indexOf('limit-perm-note') >= 0,
      '指标区与说明各自成块');

// 待机：会话已结束，仍报上一段的平均功率，但标签要写清"上次"
CL.state.limits.c1.is_charging = false;
const idlePanel = CL.permanentPanelHtml('c1');
check(idlePanel.indexOf('chargeLimit.permanentAvgLast') >= 0, '待机标签改"上次平均功率"');
check(idlePanel.indexOf('limit-perm-power is-idle') >= 0, '待机主读数转次级墨色');
check(/>6\.3<span class="limit-perm-u">W<\/span>/.test(idlePanel), '数值仍是上一段的平均（不归零）');
check(idlePanel.indexOf('chargeLimit.permanentDurLabel') < 0, '待机不再报时长');

console.log('\n-- 堆叠卡组翻页判定（swipeDecision） --');
// 阈值 = max(46px, 宽度的 22%)；330px 宽的卡片 -> 72.6px
eq(CL.swipeDecision(-90, 330), 1, '左滑过阈值 -> 向后翻');
eq(CL.swipeDecision(90, 330), -1, '右滑过阈值 -> 向前翻');
eq(CL.swipeDecision(-40, 330), 0, '左滑不足 -> 回弹');
eq(CL.swipeDecision(40, 330), 0, '右滑不足 -> 回弹');
eq(CL.swipeDecision(0, 330), 0, '没有位移 -> 回弹');
eq(CL.swipeDecision(-60, 200), 1, '窄卡回落到 46px 下限（46>44）');
eq(CL.swipeDecision(-45, 200), 0, '窄卡 45px 仍未达 46px 下限');
eq(CL.swipeDecision(-200, 0), 0, '宽度为 0（未布局）不翻页');
eq(CL.swipeDecision(NaN, 330), 0, 'NaN 位移不翻页');
eq(CL.swipeDecision(-90, Infinity), 0, '非有限宽度不翻页');
eq(CL.swipeDecision(-73, 330), 1, '恰好越过阈值即翻（>= 而非 >）');

console.log('\n-- 栈序轮转（flipOrder） --');
const ORDER = ['c1', 'c2', 'c3', 'a'];
eq(CL.flipOrder(ORDER, 1), ['c2', 'c3', 'a', 'c1'], '向后翻一格');
eq(CL.flipOrder(ORDER, -1), ['a', 'c1', 'c2', 'c3'], '向前翻一格');
eq(CL.flipOrder(ORDER, 2), ['c3', 'a', 'c1', 'c2'], '向后翻两格');
eq(CL.flipOrder(ORDER, 4), ORDER, '翻满一圈回到原序');
eq(CL.flipOrder(ORDER, 5), ['c2', 'c3', 'a', 'c1'], '越界按长度取模');
eq(CL.flipOrder(ORDER, -5), ['a', 'c1', 'c2', 'c3'], '负向越界同样回绕');
eq(CL.flipOrder(ORDER, 0), ORDER, '0 步不动');
eq(CL.flipOrder(ORDER, 1), ['c2', 'c3', 'a', 'c1'], '0 步之后入参仍未被改动（动画期间要读旧序）');
eq(CL.flipOrder(['c1'], 1), ['c1'], '单元素卡组安全');
eq(CL.flipOrder([], 1), [], '空卡组安全');

console.log('\n-- 边界 --');
CL.state.limits.c1 = { wh: 10, mode: 'once', session_wh: 50, is_charging: true, fired: true };
eq(CL.progressPct('c1'), 100, '超冲量进度不超 100');
CL.state.limits.c1 = { wh: 30, mode: 'once', session_wh: -5, is_charging: false, fired: false };
eq(CL.progressPct('c1'), 0, '异常负值进度不为负');
CL.state.limits = {};
eq(CL.entryFor('c9').wh, 0, '未知端口回落安全默认');
eq(CL.entryFor('c1').mode, 'once', '未知端口默认 mode');
eq(CL.progressPct('c1'), 0, '未知端口进度 0（不抛异常）');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
