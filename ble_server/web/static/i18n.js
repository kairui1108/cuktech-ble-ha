/*!
 * i18n.js — lightweight internationalization runtime for the CUKTECH BLE web UI.
 *
 * Why a small runtime instead of i18next/vue-i18n:
 *  - This project is vanilla HTML/CSS/JS served from an embedded device with no
 *    bundler, and every static byte is pre-loaded into server memory.
 *  - i18next (the standard vanilla-JS choice) would add a ~40 KB vendored UMD
 *    plus a language detector for a few hundred strings.
 *  - This module follows i18next's conventions (t(), {{var}} interpolation,
 *    count-based plurals via Intl.PluralRules, Intl-based number/date
 *    formatting) so migrating to a full library later is a drop-in change.
 *
 * Usage:
 *  - Load locale packs FIRST (they populate window.I18N_RESOURCES), then this file.
 *  - Static text:  <span data-i18n="common.connect">连接设备</span>
 *  - Placeholder:  <input data-i18n-placeholder="config.optional">
 *  - Title/aria:   <div data-i18n-title="..."> <div data-i18n-aria-label="...">
 *  - JS strings:   I18N.t('modal.portDetail', { port: 'C1' })
 *  - Plurals:      I18N.t('countdown.minutes', { count: 5 })
 *                  (locale value is { one: '1 min', other: '{{count}} min' })
 *  - Live updates: I18N.onChange(function (locale) { ... re-render ... })
 *  - Switcher:     <div data-i18n-switcher> … <div data-lang="en">English</div> … </div>
 *  - Select:       <select data-i18n-select>…</select>
 */
(function (global) {
    'use strict';

    var STORAGE_KEY = 'cuktech-lang';
    var DEFAULT_LOCALE = 'zh-CN';
    var LANG_NAMES = { 'zh-CN': '中文', 'en': 'English' };

    var resources = global.I18N_RESOURCES || {};
    var current = null;
    var listeners = [];
    var warned = {};
    var switchersWired = false;

    // ── Locale resolution ──

    function getStorage() {
        try { return global.localStorage || null; } catch (e) { return null; }
    }

    // Default language: follow the browser/system language, fall back to Chinese.
    function detectSystemLocale() {
        try {
            var nav = global.navigator || {};
            var lang = nav.language || nav.userLanguage || '';
            return String(lang).toLowerCase().indexOf('zh') === 0 ? 'zh-CN' : 'en';
        } catch (e) { return DEFAULT_LOCALE; }
    }

    function resolveInitialLocale() {
        var store = getStorage();
        var saved = store ? store.getItem(STORAGE_KEY) : null;
        if (saved && resources[saved]) return saved;
        var sys = detectSystemLocale();
        return resources[sys] ? sys : DEFAULT_LOCALE;
    }

    function normalize(locale) {
        if (!locale) return DEFAULT_LOCALE;
        var l = String(locale).toLowerCase().replace(/_/g, '-');
        if (l.indexOf('zh') === 0) return 'zh-CN';
        if (l.indexOf('en') === 0) return 'en';
        return resources[l] ? l : DEFAULT_LOCALE;
    }

    function getLocale() {
        if (!current) current = resolveInitialLocale();
        return current;
    }

    // ── Lookup / interpolation / pluralization ──

    function getDict(locale) { return resources[locale] || {}; }

    function lookup(dict, key) {
        var parts = key.split('.');
        var node = dict;
        for (var i = 0; i < parts.length; i++) {
            if (node == null || typeof node !== 'object') return undefined;
            node = node[parts[i]];
        }
        return node;
    }

    function interpolate(val, params) {
        if (!params || typeof val !== 'string') return val;
        return val.replace(/\{\{\s*([\w.]+)\s*\}\}/g, function (m, name) {
            var v = params[name];
            return (v !== undefined && v !== null) ? String(v) : m;
        });
    }

    function pluralize(val, params) {
        if (!params || params.count === undefined) {
            return interpolate(val, params);
        }
        if (typeof val !== 'object' || val === null) {
            return interpolate(val, params);
        }
        var count = Number(params.count);
        var cat;
        try { cat = new Intl.PluralRules(getLocale()).select(count); } catch (e) {
            cat = count === 1 ? 'one' : 'other';
        }
        var chosen = val[cat] != null ? val[cat] : (val.other != null ? val.other : null);
        if (chosen == null) {
            var first = null;
            for (var k in val) { if (Object.prototype.hasOwnProperty.call(val, k)) { first = val[k]; break; } }
            chosen = first;
        }
        return interpolate(chosen, params);
    }

    function t(key, params) {
        var locale = getLocale();
        var val = lookup(getDict(locale), key);
        if (val == null && locale !== DEFAULT_LOCALE) {
            val = lookup(getDict(DEFAULT_LOCALE), key); // fall back to zh-CN
        }
        if (val == null) {
            if (!warned[key]) {
                warned[key] = true;
                try { console.warn('[i18n] Missing translation key: ' + key); } catch (e) {}
            }
            return key;
        }
        return pluralize(val, params);
    }

    // ── Number / date formatting (Intl based) ──

    function formatNumber(value, options) {
        try { return new Intl.NumberFormat(getLocale(), options || {}).format(value); }
        catch (e) { return String(value); }
    }

    function formatDate(value, options) {
        var d = (value instanceof Date) ? value : new Date(value);
        if (isNaN(d.getTime())) return String(value);
        try { return new Intl.DateTimeFormat(getLocale(), options || {}).format(d); }
        catch (e) { return d.toLocaleString(); }
    }

    // ── DOM binding ──

    function translateEl(el) {
        var params = null;
        var paramsAttr = el.getAttribute ? el.getAttribute('data-i18n-params') : null;
        if (paramsAttr) {
            try { params = JSON.parse(paramsAttr); } catch (e) { params = null; }
        }
        var key = el.getAttribute('data-i18n');
        if (key) el.textContent = t(key, params);
        var ph = el.getAttribute('data-i18n-placeholder');
        if (ph) el.setAttribute('placeholder', t(ph, params));
        var ti = el.getAttribute('data-i18n-title');
        if (ti) el.setAttribute('title', t(ti, params));
        var html = el.getAttribute('data-i18n-html');
        if (html) el.innerHTML = t(html, params);
        var aria = el.getAttribute('data-i18n-aria-label');
        if (aria) el.setAttribute('aria-label', t(aria, params));
    }

    function applyTranslations(root) {
        if (typeof document === 'undefined') return;
        root = root || document;
        if (!root.querySelectorAll) return;
        var nodes = root.querySelectorAll('[data-i18n],[data-i18n-placeholder],[data-i18n-title],[data-i18n-html],[data-i18n-aria-label]');
        for (var i = 0; i < nodes.length; i++) translateEl(nodes[i]);
        var titleEl = root.querySelector ? root.querySelector('title[data-i18n]') : null;
        if (titleEl) document.title = t(titleEl.getAttribute('data-i18n'));
    }

    // ── Language switcher UI (dropdown style + select style) ──

    function syncSwitchers() {
        if (typeof document === 'undefined') return;
        var locale = getLocale();
        var btns = document.querySelectorAll('[data-i18n-switch-btn]');
        for (var i = 0; i < btns.length; i++) {
            var label = btns[i].querySelector('span') || btns[i];
            label.textContent = LANG_NAMES[locale] || locale;
        }
        var opts = document.querySelectorAll('[data-i18n-switcher] [data-lang], [data-i18n-select] option');
        for (var j = 0; j < opts.length; j++) {
            opts[j].classList && opts[j].classList.toggle('active', opts[j].getAttribute('data-lang') === locale);
            if (opts[j].selected !== undefined && opts[j].getAttribute('value') === locale) opts[j].selected = true;
        }
        var selects = document.querySelectorAll('select[data-i18n-select]');
        for (var k = 0; k < selects.length; k++) {
            if (selects[k].value !== locale) selects[k].value = locale;
        }
    }

    function setupSwitchers() {
        if (typeof document === 'undefined' || switchersWired) return;
        switchersWired = true;

        var switchers = document.querySelectorAll('[data-i18n-switcher]');
        for (var i = 0; i < switchers.length; i++) {
            (function (sw) {
                var btn = sw.querySelector('[data-i18n-switch-btn]');
                var menu = sw.querySelector('[data-i18n-switch-menu]');
                if (btn) {
                    btn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        if (menu) menu.classList.toggle('show');
                    });
                }
                var opts = sw.querySelectorAll('[data-lang]');
                for (var o = 0; o < opts.length; o++) {
                    (function (opt) {
                        opt.addEventListener('click', function (e) {
                            e.stopPropagation();
                            setLocale(opt.getAttribute('data-lang'));
                            if (menu) menu.classList.remove('show');
                        });
                    })(opts[o]);
                }
            })(switchers[i]);
        }

        // Close any open language menu on outside click
        document.addEventListener('click', function (e) {
            var open = document.querySelectorAll('[data-i18n-switch-menu].show');
            for (var m = 0; m < open.length; m++) {
                var sw = open[m].closest('[data-i18n-switcher]');
                if (!sw || !sw.contains(e.target)) open[m].classList.remove('show');
            }
        });

        // Select style switcher
        var selects = document.querySelectorAll('select[data-i18n-select]');
        for (var s = 0; s < selects.length; s++) {
            (function (sel) {
                sel.addEventListener('change', function () { setLocale(sel.value); });
            })(selects[s]);
        }
    }

    // ── Core API ──

    function syncDocument() {
        if (typeof document === 'undefined') return;
        document.documentElement.setAttribute('lang', current);
        applyTranslations();
        syncSwitchers();
    }

    function setLocale(locale) {
        var next = normalize(locale);
        var prev = current;
        current = next;
        var store = getStorage();
        if (store) {
            try { store.setItem(STORAGE_KEY, next); } catch (e) {}
        }
        syncDocument();
        for (var i = 0; i < listeners.length; i++) {
            try { listeners[i](next, prev); } catch (e) {}
        }
        return next;
    }

    // Server-configured language (single source of truth, set in config.html):
    //  - 'auto' (or missing): follow the browser/system language — the saved
    //    preference is dropped so the server choice always wins.
    //  - explicit 'zh-CN' / 'en': applied and cached in localStorage to avoid a
    //    flash on next load; the server value still overrides on each page load.
    function applyServerLanguage(lang) {
        var store = getStorage();
        var l = lang ? String(lang).toLowerCase() : 'auto';
        if (l === 'auto' || l === '' || l === 'system') {
            if (store) { try { store.removeItem(STORAGE_KEY); } catch (e) {} }
            var next = detectSystemLocale();
            var prev = current;
            current = next;
            syncDocument();
            for (var i = 0; i < listeners.length; i++) {
                try { listeners[i](next, prev); } catch (e) {}
            }
            return next;
        }
        return setLocale(l);
    }

    // Fetch the language preference configured on the server (config.html is the
    // only UI that changes it) and apply it to this page. Called automatically on
    // init so index.html / phone.html / embedded pages all follow the server
    // setting with no per-page switchers needed.
    function initFromServer() {
        if (typeof fetch === 'undefined' || typeof location === 'undefined') return;
        fetch(location.origin + '/api/web-language')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.ok && data.language) applyServerLanguage(data.language);
            })
            .catch(function () { /* keep local resolution */ });
    }

    function onChange(fn) {
        if (typeof fn === 'function' && listeners.indexOf(fn) === -1) listeners.push(fn);
    }

    function init() {
        if (typeof document === 'undefined') return;
        current = resolveInitialLocale();
        document.documentElement.setAttribute('lang', current);
        applyTranslations();
        setupSwitchers();
        syncSwitchers();
        // Notify listeners once so pages can restore dynamic state that the static
        // re-translation above may have overwritten (e.g. SSE init arriving before
        // DOMContentLoaded), mirroring what setLocale/applyServerLanguage do.
        for (var i = 0; i < listeners.length; i++) {
            try { listeners[i](current, null); } catch (e) {}
        }
        initFromServer();
    }

    if (typeof document !== 'undefined') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', init);
        } else {
            init();
        }
    }

    global.I18N = {
        t: t,
        getLocale: getLocale,
        setLocale: setLocale,
        applyServerLanguage: applyServerLanguage,
        initFromServer: initFromServer,
        onChange: onChange,
        applyTranslations: applyTranslations,
        formatNumber: formatNumber,
        formatDate: formatDate,
        LANG_NAMES: LANG_NAMES,
        VERSION: '1.1.0'
    };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
