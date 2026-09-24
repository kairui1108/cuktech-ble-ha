/*!
 * charge_limit.js — 充电量限额控制卡片（index.html 与 phone.html 共享）
 *
 * 功能：为每个端口设置"充到多少 Wh 自动关断"。
 *   - 阈值单位是充电器输出能量（Wh），不是被充设备的实际充入电量
 *     ——线损与设备内转换损耗使后者偏小（典型 5~15%）。
 *   - mode=once：达到阈值关断一次后自动失效；always：长期有效，每次充电重新生效。
 *   - 卡片同时显示本会话已充能量，便于估算剩余。
 *
 * 为什么独立成模块：index.html 与 phone.html 是两套独立 DOM/CSS，但限额的
 * 数据契约、请求形状、交互语义必须一致——逻辑放这里，两页只负责挂载点与样式。
 *
 * 依赖：I18N（locale 运行时）。API 形状见 ha_server.handle_charge_limits。
 */
(function (global) {
    'use strict';

    var API_BASE = global.location ? global.location.origin : '';
    var PORT_KEYS = ['c1', 'c2', 'c3', 'a'];
    var MODE_ONCE = 'once';
    var MODE_ALWAYS = 'always';

    // 快捷阈值（Wh）：5～25 覆盖常见移动设备电池量级，步长 5
    var QUICK_WH = [5, 10, 15, 20, 25];

    function t(key, params) {
        return (global.I18N && global.I18N.t) ? global.I18N.t(key, params) : key;
    }

    // 提示条由各页注入：index.html 用 app.js 的 showToast，phone.html 用 phone.js 的
    // toast。共享模块不假设任何一页存在哪个 API。
    var notifier = null;
    function setNotifier(fn) { notifier = fn; }
    function notify(msg) {
        if (typeof notifier === 'function') notifier(msg);
    }

    // 每页各自维护一份最新状态，避免重复请求
    var state = { limits: {}, loaded: false, error: null };
    var pending = {};   // port -> true，防重复提交

    function fetchLimits() {
        return fetch(API_BASE + '/api/charge-limits')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.ok && data.limits) {
                    state.limits = data.limits;
                    state.loaded = true;
                    state.error = null;
                } else {
                    state.error = (data && data.error) || 'invalid response';
                }
                return state;
            })
            .catch(function (e) {
                state.error = e && e.message ? e.message : String(e);
                return state;
            });
    }

    // 保存单个端口。wh=0 表示关闭该端口限额。
    function saveLimit(port, wh, mode) {
        if (pending[port]) return Promise.resolve(state);
        pending[port] = true;
        var body = { port: port, wh: wh };
        if (mode) body.mode = mode;
        return fetch(API_BASE + '/api/charge-limits', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
            .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); })
            .then(function (res) {
                pending[port] = false;
                if (res.body && res.body.ok && res.body.limits) {
                    state.limits = res.body.limits;
                    state.error = null;
                    return { ok: true, state: state };
                }
                // 校验失败（NaN/inf/越界/非法 mode）：后端整体拒绝，前端保持原值
                var msg = (res.body && res.body.error) || ('HTTP ' + res.status);
                state.error = msg;
                return { ok: false, error: msg, state: state };
            })
            .catch(function (e) {
                pending[port] = false;
                var msg = e && e.message ? e.message : String(e);
                state.error = msg;
                return { ok: false, error: msg, state: state };
            });
    }

    function parseWhInput(raw) {
        if (raw === '' || raw === null || raw === undefined) return null;
        // 纯空白必须先拦下：Number('   ') === 0，否则用户误输入空格会被当成
        // "关闭限额"而不是无效输入（静默降级）。
        if (typeof raw === 'string' && raw.trim() === '') return null;
        var v = Number(raw);
        if (!isFinite(v) || v < 0) return null;
        return v;
    }

    // ── 状态描述（两页共用文案，样式各自处理） ──

    function entryFor(port) {
        return state.limits[port] || { wh: 0, mode: MODE_ONCE, session_wh: 0, is_charging: false, fired: false };
    }

    // "已充 12.3 / 30 Wh"；未设限额时只显示已充能量
    function progressText(port) {
        var e = entryFor(port);
        if (!(e.wh > 0)) return '';
        var used = e.session_wh || 0;
        return t('chargeLimit.progress', { used: used.toFixed(1), total: e.wh });
    }

    function statusText(port) {
        var e = entryFor(port);
        if (!(e.wh > 0)) return t('chargeLimit.off');
        var base = e.mode === MODE_ALWAYS ? t('chargeLimit.always') : t('chargeLimit.once');
        return e.fired ? base + ' · ' + t('chargeLimit.fired') : base;
    }

    // 已充 / 限额 的进度百分比（0-100），未设限额或无会话时为 0
    function progressPct(port) {
        var e = entryFor(port);
        if (!(e.wh > 0)) return 0;
        var pct = (e.session_wh || 0) / e.wh * 100;
        return Math.max(0, Math.min(100, pct));
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { fetchLimits: fetchLimits, saveLimit: saveLimit, parseWhInput: parseWhInput };
    }

    global.ChargeLimit = {
        PORT_KEYS: PORT_KEYS,
        QUICK_WH: QUICK_WH,
        MODE_ONCE: MODE_ONCE,
        MODE_ALWAYS: MODE_ALWAYS,
        state: state,
        setNotifier: setNotifier,
        notify: notify,
        fetchLimits: fetchLimits,
        saveLimit: saveLimit,
        parseWhInput: parseWhInput,
        entryFor: entryFor,
        progressText: progressText,
        statusText: statusText,
        progressPct: progressPct,
        t: t
    };
})(typeof window !== 'undefined' ? window : this);
