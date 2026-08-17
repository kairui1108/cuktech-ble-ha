#!/usr/bin/env node
/**
 * Smoke test: evaluates each page's scripts (app.js, phone.js, charge_history.js,
 * config.html inline script, embedded pages) in a sandbox with the REAL i18n
 * runtime and permissive DOM/fetch/Chart stubs, to catch ReferenceErrors and
 * other load-time/runtime wiring bugs introduced by the i18n changes.
 *
 * Usage: node tests/js/smoke_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.resolve(__dirname, '../../web');
const STATIC = path.join(WEB, 'static');

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

function makeEl() {
    return new Proxy(function el() {}, {
        get(t, p) {
            if (p === 'style' || p === 'dataset') return {};
            if (p === 'classList') {
                return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
            }
            if (p === 'children' || p === 'options') return [];
            return makeNoop();
        },
        set() { return true; },
        apply() { return makeNoop(); },
        construct() { return makeEl(); },
    });
}

function buildSandbox() {
    const sandbox = {
        console,
        Intl,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        Promise,
        JSON,
        Math,
        Date,
        RegExp,
        String,
        Number,
        Boolean,
        Object,
        Array,
        isNaN,
        parseFloat,
        parseInt,
        Error,
        fetch: async (url) => {
            // Serve a server-side language preference so every page's
            // initFromServer() → applyServerLanguage() path is exercised.
            if (String(url).indexOf('/api/web-language') !== -1) {
                return { ok: true, status: 200, json: async () => ({ ok: true, language: 'en' }) };
            }
            return { ok: true, status: 200, json: async () => ({}) };
        },
        EventSource: function () { this.onopen = null; this.onmessage = null; this.onerror = null; this.close = function () {}; },
        Chart: function () { return buildChart(); },
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        navigator: { language: 'en-US' },
        location: { origin: 'http://charger.local' },
        getComputedStyle: () => ({ getPropertyValue: () => '' }),
        addEventListener() {},
        removeEventListener() {},
    };
    sandbox.window = sandbox;
    const document = {
        documentElement: makeEl(),
        body: makeEl(),
        head: makeEl(),
        title: '',
        readyState: 'complete',
        getElementById: () => makeEl(),
        querySelector: () => makeEl(),
        querySelectorAll: () => [],
        createElement: () => makeEl(),
        addEventListener() {},
    };
    sandbox.document = document;
    const ctx = vm.createContext(sandbox);
    return ctx;
}

function buildChart() {
    return {
        data: { labels: [], datasets: [] },
        options: { scales: {} },
        _peakData: null,
        update() {},
        destroy() {},
        scales: {},
        canvas: { parentNode: makeEl() },
        ctx: {},
    };
}

function load(js) {
    const ctx = buildSandbox();
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'locales/zh-CN.js'), 'utf8'), ctx);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'locales/en.js'), 'utf8'), ctx);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'i18n.js'), 'utf8'), ctx);
    vm.runInContext(js, ctx);
    return ctx;
}

// Flip the language back and forth: exercises every I18N.onChange re-render
// hook registered by the page scripts (rerenderDynamic, renderAll, refreshChargeHistory...).
function flipLocales(ctx, cycles) {
    const n = cycles || 2;
    for (let i = 0; i < n; i++) {
        vm.runInContext("I18N.setLocale('en'); I18N.setLocale('zh-CN');", ctx);
    }
}

const checks = [];
function check(name, fn) { checks.push({ name, fn }); }

function extractInlineScripts(htmlPath) {
    const html = fs.readFileSync(htmlPath, 'utf8');
    const scripts = [];
    const re = /<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g;
    let m;
    while ((m = re.exec(html))) scripts.push(m[1]);
    return scripts.join('\n');
}

check('index.html chain: chart-config + chart-loader + charge_history + app', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'chart-config.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'chart-loader.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'charge_history.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8'),
    ].join('\n;\n');
    const ctx = load(parts);
    flipLocales(ctx); // exercises app.js rerenderDynamic + charge_history refresh
});

check('phone.html chain: charge_history + phone.js', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'charge_history.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'phone.js'), 'utf8'),
    ].join('\n;\n');
    const ctx = load(parts);
    flipLocales(ctx); // exercises phone.js renderAll on locale change
});

check('config.html inline script', () => {
    load(extractInlineScripts(path.join(WEB, 'config.html')));
});

check('countdown.html inline script', () => {
    const ctx = load(extractInlineScripts(path.join(STATIC, 'countdown.html')));
    flipLocales(ctx);
});

check('device_info.html inline script', () => {
    const ctx = load(extractInlineScripts(path.join(STATIC, 'device_info.html')));
    flipLocales(ctx);
});

check('port_monitor.html inline script', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'chart-config.js'), 'utf8'),
        extractInlineScripts(path.join(STATIC, 'port_monitor.html')),
    ].join('\n;\n');
    const ctx = load(parts);
    flipLocales(ctx);
});

check('power_chart.html inline script', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'chart-config.js'), 'utf8'),
        extractInlineScripts(path.join(STATIC, 'power_chart.html')),
    ].join('\n;\n');
    load(parts);
});

let failed = 0;
for (const { name, fn } of checks) {
    try {
        fn();
        console.log('  ✓ ' + name);
    } catch (e) {
        failed++;
        console.error('  ✗ ' + name);
        console.error('    ' + (e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n    ') : e));
    }
}
console.log(failed === 0 ? `\nAll ${checks.length} smoke checks passed.` : `\n${failed}/${checks.length} smoke checks FAILED.`);
process.exit(failed === 0 ? 0 : 1);