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
