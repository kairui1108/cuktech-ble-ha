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
eq(CL.statusText('c1'), 'chargeLimit.once', '状态显示仅一次');
eq(CL.progressText('c1'), 'chargeLimit.progress:{"used":"12.3","total":30}', '进度文案带已充/限额');

console.log('\n-- always 模式与已触发 --');
CL.state.limits.c1 = { wh: 30, mode: 'always', session_wh: 30, is_charging: true, fired: true };
eq(CL.statusText('c1'), 'chargeLimit.always · chargeLimit.fired', '状态：长期有效 + 已触发');
eq(CL.progressPct('c1'), 100, '进度封顶 100');

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
