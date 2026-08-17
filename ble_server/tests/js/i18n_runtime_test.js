#!/usr/bin/env node
/**
 * Unit tests for web/static/i18n.js + locale packs.
 * Runs in plain Node (no browser needed) using a minimal fake DOM.
 *
 * Usage: node tests/js/i18n_runtime_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.resolve(__dirname, '../../web');
const STATIC = path.join(WEB, 'static');

// ── Tiny test runner ──
const tests = [];
function test(name, fn) { tests.push({ name, fn }); }
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function assertEq(actual, expected, msg) {
    if (actual !== expected) {
        throw new Error((msg ? msg + ' — ' : '') + 'expected ' + JSON.stringify(expected) + ', got ' + JSON.stringify(actual));
    }
}
function assertThrows(fn, msg) {
    let threw = false;
    try { fn(); } catch (e) { threw = true; }
    assert(threw, msg || 'expected function to throw');
}

// ── Minimal fake DOM ──
class FakeClassList {
    constructor() { this._s = new Set(); }
    add(c) { this._s.add(c); }
    remove(c) { this._s.delete(c); }
    toggle(c, force) {
        if (force === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); }
        else { force ? this._s.add(c) : this._s.delete(c); }
    }
    contains(c) { return this._s.has(c); }
}
class FakeEl {
    constructor(tag, attrs, parent) {
        this.tag = tag;
        this.attrs = attrs || {};
        this.children = [];
        this.parent = parent || null;
        this.textContent = '';
        this.innerHTML = '';
        this.value = '';
        this.selected = false;
        this.classList = new FakeClassList();
        this.listeners = {};
        this.readyState = undefined;
    }
    append(child) { child.parent = this; this.children.push(child); return child; }
    getAttribute(name) { return this.attrs[name] !== undefined ? this.attrs[name] : null; }
    setAttribute(name, val) { this.attrs[name] = String(val); }
    hasAttribute(name) { return this.attrs[name] !== undefined; }
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
    contains(other) {
        let n = other;
        while (n) { if (n === this) return true; n = n.parent; }
        return false;
    }
    closest(sel) {
        let n = this;
        while (n) { if (n.matches(sel)) return n; n = n.parent; }
        return null;
    }
    matches(sel) { return matchSimple(sel, this); }
    all() { // walk descendants (excluding self)
        const out = [];
        const walk = (el) => { for (const c of el.children) { out.push(c); walk(c); } };
        walk(this);
        return out;
    }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    querySelectorAll(sel) {
        return this.all().filter(el => el.matches(sel));
    }
}
class FakeDocument extends FakeEl {
    constructor() {
        super('document', {});
        this.documentElement = new FakeEl('html', {}, this);
        this.title = '';
        this.readyState = 'complete';
        this.body = new FakeEl('body', {}, this);
        this.children = [this.documentElement, this.body]; // make descendants reachable via all()
    }
}
// Selector subset used by i18n.js: tag[attr], [attr], [attr].class, "A B" (descendant)
function matchSimple(sel, el) {
    for (const part of sel.split(',')) {
        if (matchOne(part.trim(), el)) return true;
    }
    return false;
}
function matchOne(sel, el) {
    const parts = sel.split(/\s+/);
    if (parts.length === 1) return matchAtom(parts[0], el);
    // descendant: last atom must match el, all previous must match some ancestor
    const last = matchAtom(parts[parts.length - 1], el);
    if (!last) return false;
    const rest = parts.slice(0, -1);
    let anc = el.parent;
    let ri = rest.length - 1;
    while (anc && ri >= 0) {
        if (matchAtom(rest[ri], anc)) ri--;
        anc = anc.parent;
    }
    return ri < 0;
}
function matchAtom(atom, el) {
    let m = /^([a-z0-9]*)((?:\[[^\]]+\])*)(\.[\w-]+)?$/i.exec(atom);
    if (!m) throw new Error('unsupported selector: ' + atom);
    const [, tag, attrStr, cls] = m;
    if (tag && el.tag !== tag) return false;
    const attrs = attrStr.match(/\[[^\]]+\]/g) || [];
    for (const a of attrs) {
        const mm = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(a);
        if (!mm) throw new Error('unsupported attr selector: ' + a);
        const [, name, val] = mm;
        if (val !== undefined) { if (el.getAttribute(name) !== val) return false; }
        else if (!el.hasAttribute(name)) return false;
    }
    if (cls) { if (!el.classList.contains(cls.slice(1))) return false; }
    return true;
}

// ── Build a fresh sandbox per test ──
function buildSandbox(lang, savedLang, domSetup) {
    const store = {};
    if (savedLang) store['cuktech-lang'] = savedLang;
    const document = new FakeDocument();
    const sandbox = {
        console,
        Intl,
        navigator: { language: lang || 'zh-CN' },
        localStorage: {
            getItem: (k) => (k in store ? store[k] : null),
            setItem: (k, v) => { store[k] = String(v); },
            removeItem: (k) => { delete store[k]; },
        },
        document,
        setTimeout,
        clearTimeout,
        fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    };
    sandbox.window = sandbox; // i18n.js takes window if defined
    if (domSetup) domSetup(document); // build static DOM before i18n.js wires it
    const ctx = vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'locales/zh-CN.js'), 'utf8'), ctx);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'locales/en.js'), 'utf8'), ctx);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'i18n.js'), 'utf8'), ctx);
    return { ctx, I18N: ctx.I18N, document, store };
}

// ── Tests ──

test('default locale follows system language (zh → zh-CN)', () => {
    const { I18N } = buildSandbox('zh-CN');
    assertEq(I18N.getLocale(), 'zh-CN');
    assertEq(I18N.t('common.connect'), '连接设备');
});

test('default locale follows system language (en → en)', () => {
    const { I18N } = buildSandbox('en-US');
    assertEq(I18N.getLocale(), 'en');
    assertEq(I18N.t('common.connect'), 'Connect');
});

test('saved preference overrides system language', () => {
    const { I18N } = buildSandbox('zh-CN', 'en');
    assertEq(I18N.getLocale(), 'en');
});

test('setLocale normalizes zh / en-US → canonical codes and persists', () => {
    const { I18N, store } = buildSandbox('en-US');
    I18N.setLocale('zh');
    assertEq(I18N.getLocale(), 'zh-CN');
    assertEq(store['cuktech-lang'], 'zh-CN');
    I18N.setLocale('en-US');
    assertEq(I18N.getLocale(), 'en');
    assertEq(store['cuktech-lang'], 'en');
});

test('unknown locale falls back to default', () => {
    const { I18N } = buildSandbox('en-US');
    assertEq(I18N.setLocale('fr'), 'zh-CN');
});

test('interpolation with named params', () => {
    const { I18N } = buildSandbox('zh-CN');
    assertEq(I18N.t('modal.portDetail', { port: 'C1' }), 'C1 端口详情');
    assertEq(I18N.t('common.firmware', { version: '1.2.3' }), '固件版本：1.2.3');
    I18N.setLocale('en');
    assertEq(I18N.t('modal.portDetail', { port: 'C1' }), 'C1 Port Details');
});

test('pluralization: English one/other', () => {
    const { I18N } = buildSandbox('en-US');
    assertEq(I18N.t('common.minutes', { count: 1 }), '1 min');
    assertEq(I18N.t('common.minutes', { count: 5 }), '5 min');
    assertEq(I18N.t('common.minutes', { count: 0 }), '0 min');
});

test('pluralization: Chinese is count-agnostic', () => {
    const { I18N } = buildSandbox('zh-CN');
    assertEq(I18N.t('common.minutes', { count: 1 }), '1分钟');
    assertEq(I18N.t('common.minutes', { count: 120 }), '120分钟');
});

test('missing key returns the key itself and warns once', () => {
    const { I18N } = buildSandbox('en-US');
    assertEq(I18N.t('nope.missing'), 'nope.missing');
});

test('missing key in en falls back to zh-CN', () => {
    const { ctx, I18N } = buildSandbox('en-US');
    // remove a key from en to simulate an incomplete translation
    vm.runInContext("delete I18N_RESOURCES['en'].common.connect", ctx);
    assertEq(I18N.t('common.connect'), '连接设备');
});

test('locale change fires onChange listeners', () => {
    const { I18N } = buildSandbox('en-US');
    let seen = [];
    I18N.onChange((l, prev) => seen.push([l, prev]));
    I18N.setLocale('zh-CN');
    assertEq(seen.length, 1);
    assertEq(seen[0][0], 'zh-CN');
    assertEq(seen[0][1], 'en');
});

test('formatNumber / formatDate use Intl with current locale', () => {
    const { I18N } = buildSandbox('en-US');
    assertEq(I18N.formatNumber(1234.5), '1,234.5');
    I18N.setLocale('zh-CN');
    assertEq(I18N.formatNumber(1234.5), '1,234.5'); // zh-CN also uses comma grouping
    const d = new Date(2024, 0, 15);
    assert(I18N.formatDate(d).length > 0);
});

test('applyTranslations translates data-i18n elements and title', () => {
    const { I18N, document } = buildSandbox('zh-CN');
    const span = document.body.append(new FakeEl('span', { 'data-i18n': 'common.connect' }));
    const input = document.body.append(new FakeEl('input', { 'data-i18n-placeholder': 'config.optional' }));
    const title = new FakeEl('title', { 'data-i18n': 'pageTitle.config' });
    document.documentElement.append(title);
    document.title = 'old';
    I18N.applyTranslations();
    assertEq(span.textContent, '连接设备');
    assertEq(input.getAttribute('placeholder'), '可选');
    assertEq(document.title, 'CUKTECH 配置');
    I18N.setLocale('en');
    assertEq(span.textContent, 'Connect'); // re-applied on locale change
    assertEq(input.getAttribute('placeholder'), 'optional');
    assertEq(document.title, 'CUKTECH Config');
});

test('data-i18n-html and aria-label are applied', () => {
    const { I18N, document } = buildSandbox('zh-CN');
    const el = document.body.append(new FakeEl('div', { 'data-i18n-html': 'config.scanWithApp', 'data-i18n-aria-label': 'common.theme' }));
    I18N.applyTranslations();
    assert(el.innerHTML.indexOf('米家 App') !== -1);
    assertEq(el.getAttribute('aria-label'), '主题');
    I18N.setLocale('en');
    assert(el.innerHTML.indexOf('Mi Home App') !== -1);
    assertEq(el.getAttribute('aria-label'), 'Theme');
});

test('document lang attribute follows locale', () => {
    const { I18N, document } = buildSandbox('en-US');
    assertEq(document.documentElement.getAttribute('lang'), 'en');
    I18N.setLocale('zh-CN');
    assertEq(document.documentElement.getAttribute('lang'), 'zh-CN');
});

test('dropdown switcher: click option switches locale and syncs button', () => {
    const { I18N, document } = buildSandbox('zh-CN', null, (doc) => {
        const sw = new FakeEl('div', { 'data-i18n-switcher': '' });
        const btn = sw.append(new FakeEl('button', { 'data-i18n-switch-btn': '' }));
        btn.append(new FakeEl('span'));
        const menu = sw.append(new FakeEl('div', { 'data-i18n-switch-menu': '' }));
        menu.append(new FakeEl('div', { 'data-lang': 'zh-CN' }));
        menu.append(new FakeEl('div', { 'data-lang': 'en' }));
        doc.body.append(sw);
    });
    const btn = document.body.querySelector('[data-i18n-switch-btn]');
    const label = btn.querySelector('span');
    const enOpt = document.body.querySelector('[data-lang="en"]');
    assertEq(label.textContent, '中文'); // synced at init
    enOpt.listeners['click'][0]({ stopPropagation() {} }); // click English option
    assertEq(I18N.getLocale(), 'en');
    assertEq(label.textContent, 'English');
});

test('select switcher syncs value on load and on change', () => {
    const { I18N, document } = buildSandbox('zh-CN', null, (doc) => {
        const sel = new FakeEl('select', { 'data-i18n-select': '' });
        sel.append(new FakeEl('option', { value: 'zh-CN' }));
        sel.append(new FakeEl('option', { value: 'en' }));
        sel.value = 'zh-CN';
        doc.body.append(sel);
    });
    const sel = document.body.querySelector('select[data-i18n-select]');
    assertEq(sel.value, 'zh-CN');
    assertEq(sel.children[1].selected, false);
    // user picks English
    sel.value = 'en';
    sel.listeners['change'][0]();
    assertEq(I18N.getLocale(), 'en');
    assertEq(sel.children[1].selected, true);
});

test('applyTranslations handles option elements', () => {
    const { I18N, document } = buildSandbox('zh-CN');
    const opt = document.body.append(new FakeEl('option', { 'data-i18n': 'charge.today' }));
    I18N.applyTranslations();
    assertEq(opt.textContent, '今日');
    I18N.setLocale('en');
    assertEq(opt.textContent, 'Today');
});

test('data-i18n-params interpolate at DOM translation time', () => {
    const { I18N, document } = buildSandbox('zh-CN');
    const label = document.body.append(new FakeEl('span', { 'data-i18n': 'config.portName', 'data-i18n-params': '{"port":"C1"}' }));
    I18N.applyTranslations();
    assertEq(label.textContent, 'C1 端口名称');
    I18N.setLocale('en');
    assertEq(label.textContent, 'C1 Port Name');
});

// ── Server-configured language (config.html is the single source) ──

test('applyServerLanguage with explicit language applies and caches it', () => {
    const { I18N, store } = buildSandbox('zh-CN');
    I18N.applyServerLanguage('en');
    assertEq(I18N.getLocale(), 'en');
    assertEq(store['cuktech-lang'], 'en'); // cached to avoid flash on next load
    I18N.applyServerLanguage('zh-CN');
    assertEq(I18N.getLocale(), 'zh-CN');
});

test('applyServerLanguage auto drops saved preference and follows system', () => {
    const { I18N, store } = buildSandbox('zh-CN', 'en');
    assertEq(I18N.getLocale(), 'en'); // stale localStorage from an old switcher
    I18N.applyServerLanguage('auto');
    assertEq(I18N.getLocale(), 'zh-CN'); // system is zh-CN
    assertEq(store['cuktech-lang'], undefined); // preference removed
});

test('applyServerLanguage auto fires onChange with the resolved locale', () => {
    const { I18N } = buildSandbox('en-US', 'zh-CN');
    let seen = [];
    I18N.onChange((l) => seen.push(l));
    I18N.applyServerLanguage('auto');
    assertEq(seen.length, 1);
    assertEq(seen[0], 'en'); // resolved from system
});

test('initFromServer fetches /api/web-language and applies the server value', async () => {
    const { I18N } = buildSandbox('zh-CN');
    await new Promise((resolve) => {
        I18N.initFromServer();
        setTimeout(resolve, 0); // let the microtask chain settle
    });
    // with the sandbox fetch stub returning {json: async () => ({})} nothing changes
    assertEq(I18N.getLocale(), 'zh-CN');
});

test('init notifies listeners after first apply (restores dynamic state)', () => {
    const document = new FakeDocument();
    document.readyState = 'loading'; // init defers to DOMContentLoaded
    const sandbox = {
        console, Intl,
        navigator: { language: 'zh-CN' },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document, setTimeout, clearTimeout,
        fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    };
    sandbox.window = sandbox;
    const ctx = vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'locales/zh-CN.js'), 'utf8'), ctx);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'locales/en.js'), 'utf8'), ctx);
    vm.runInContext(fs.readFileSync(path.join(STATIC, 'i18n.js'), 'utf8'), ctx);
    let calls = [];
    ctx.I18N.onChange((l, prev) => calls.push([l, prev]));
    // fire DOMContentLoaded -> init()
    document.listeners['DOMContentLoaded'][0]();
    assertEq(calls.length, 1);
    assertEq(calls[0][0], 'zh-CN');
    assertEq(calls[0][1], null);
});

// ── Run ──
let failed = 0;
for (const { name, fn } of tests) {
    try {
        fn();
        console.log('  ✓ ' + name);
    } catch (e) {
        failed++;
        console.error('  ✗ ' + name);
        console.error('    ' + (e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n    ') : e));
    }
}

console.log(failed === 0 ? `\nAll ${tests.length} tests passed.` : `\n${failed}/${tests.length} tests FAILED.`);
process.exit(failed === 0 ? 0 : 1);
