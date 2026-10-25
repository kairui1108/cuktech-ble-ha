/*!
 * device-store.js — Web UI 的多设备状态中枢（index.html / phone.html 共用）
 *
 * 为什么需要这一层（§3.12）：
 *   P1 之后每台设备在服务端都有自己的状态，SSE 在多设备时给每条事件带 `device`，
 *   所有 /api/* 都接受 `?device=`。前端要做的事不是"再渲染一套界面"，而是把
 *   "每台设备的数据"和"当前在看哪台"分开：
 *     · 数据按 device_id 分桶（stores）——所有事件**先落桶**，桶是唯一真值；
 *     · DOM 只表达"当前设备"这一份快照，切换 = 挪指针 + 把现有渲染函数重放一遍。
 *   所以 DOM 骨架永不重建：id 不变、图片不重载、getElementById 全部复用，
 *   改动集中在数据层，渲染层一行不用动。
 *
 * 单设备零感知（§3.12 验收）：
 *   服务端只在 ≥2 台时才发 `devices` 列表与 `device` 字段。于是单设备时这里
 *   只是一条恒指向 primary 的指针——切换器整条隐藏、事件缺 `device` 按 primary
 *   处理、请求只多一个 `?device=default` 参数，界面与改造前完全一致。旧浏览器
 *   缓存的旧 JS（事件里没有 device）同样落到 primary，不会报错。
 *
 * 依赖：只用 fetch / URL / localStorage / CustomEvent；SSE 长连接仍由页面持有
 * （/api/events 是一条全局流，不带 ?device=，设备归属由事件字段表达）。
 */
(function (global) {
    'use strict';

    var PRIMARY = 'default';        // 与后端 PRIMARY_DEVICE_ID 一致（§3.10）
    var STORAGE_KEY = 'cuktech-device';

    var buckets = new Map();        // device_id -> {ports, settings, protocolSwitches, connected, …}
    var current = PRIMARY;          // 当前设备指针
    var devices = [];               // SSE init 给的设备清单；空 = 单设备 / 旧服务端
    var defaults = null;            // 桶的初始形状（各页自己的形状，见下面 init）
    var applyPorts = null;          // 端口数据的落桶方式（两页的端口形状不同）
    var serviceEvent = null;        // 服务级字段（MQTT 连接）的旁路回调
    var replay = [];                // 换设备时要重放的渲染函数，顺序即渲染顺序
    var listeners = [];             // 直接注册的 device-changed 回调
    var switcher = null;            // 切换器容器（页面里可选）
    var switcherSig = '';           // 设备清单指纹：清单没变就不重建切换器 DOM
    var view = null;                // 当前设备桶的读写视图（Proxy）

    // ── 桶 ──

    function bucketOf(id) {
        var b = buckets.get(id);
        if (!b) {
            b = defaults ? defaults() : {};
            buckets.set(id, b);
        }
        return b;
    }

    function currentBucket() { return bucketOf(current); }

    // 当前设备那一桶的读写视图。用 Proxy 而不是给每个变量配一对访问器：
    // 页面里的渲染函数（app.js 约 900 行）遍布全文，让它们继续写 `dev.ports`
    // 只需要这里一个定义，那些函数一行都不用改。
    function makeView() {
        return new Proxy({}, {
            get: function (target, prop) { return currentBucket()[prop]; },
            set: function (target, prop, value) { currentBucket()[prop] = value; return true; },
            has: function (target, prop) { return prop in currentBucket(); }
        });
    }

    // ── 当前设备指针：localStorage 记忆 + ?device= URL 同步 ──

    function readUrlDevice() {
        try { return new URL(global.location.href).searchParams.get('device'); } catch (e) { return null; }
    }

    function readStored() {
        try { return global.localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
    }

    // 选中态同时写 URL（可刷新可分享）与 localStorage（记住上次）。
    function persist(id) {
        try { global.localStorage.setItem(STORAGE_KEY, id); } catch (e) {}
        try {
            var u = new URL(global.location.href);
            u.searchParams.set('device', id);
            if (global.history && global.history.replaceState) {
                global.history.replaceState(null, '', u.toString());
            }
        } catch (e) {}
    }

    function resolveInitial() {
        // ?device= 优先：分享出去的链接必须一打开就落在那台设备上
        var fromUrl = readUrlDevice();
        if (fromUrl) { current = fromUrl; persist(fromUrl); return; }
        var saved = readStored();
        current = saved || PRIMARY;
    }

    // ── 统一请求出口 ──

    function url(path) {
        var u = new URL(path, global.location.origin);
        // 老端点缺省 = primary，所以单设备时这个参数对服务端是纯冗余（§3.12 验收口径）
        u.searchParams.set('device', current);
        return u.toString();
    }

    function api(path, opts) {
        return global.fetch(url(path), opts);
    }

    // ── 设备清单与切换器 ──

    function esc(text) {
        return String(text === undefined || text === null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function setDevices(list) {
        devices = Array.isArray(list) ? list.filter(function (d) { return d && d.id; }) : [];
        // 记忆里的 id 可能已经被删掉（设备从 config 里移除）：回退 primary。
        // 不回退的话接下来每个请求都会 404 device_not_found —— 界面一片空白，
        // 用户完全看不出是"设备没了"还是"服务挂了"。
        if (devices.length && !devices.some(function (d) { return d.id === current; })) {
            var prev = current;
            current = devices[0].id;
            persist(current);
            changed(prev);
        }
        renderSwitcher();
    }

    // 切换器 DOM：pills 只在"设备清单变了"时重建（每帧重建会打断 hover 与点击），
    // 选中态与在线圆点每帧只改 class。
    function renderSwitcher() {
        if (!switcher) return;
        // 单设备（或服务端没给列表）：整条隐藏 —— 与改造前的视觉完全一致
        if (devices.length < 2) {
            if (!switcher.hidden) {
                switcher.hidden = true;
                switcher.innerHTML = '';
                switcherSig = '';
            }
            markBody(false);
            return;
        }
        switcher.hidden = false;
        markBody(true);
        // 指纹里带语言：pills 里唯一需要翻译的是 aria-label，切语言时得重建一次
        var locale = (global.I18N && global.I18N.getLocale) ? global.I18N.getLocale() : '';
        var sig = locale + '\u0003' + devices.map(function (d) { return d.id + '\u0001' + (d.name || d.id); }).join('\u0002');
        if (sig !== switcherSig) {
            switcherSig = sig;
            var label = (global.I18N && global.I18N.t) ? global.I18N.t('device.switcher') : 'Devices';
            switcher.innerHTML = '<div class="wrap"><div class="device-tabs" role="tablist" aria-label="'
                + esc(label) + '">'
                + devices.map(function (d) {
                    return '<button type="button" class="time-btn device-pill" role="tab" data-device="'
                        + esc(d.id) + '" aria-selected="false">'
                        + '<i class="device-dot" aria-hidden="true"></i>'
                        + '<span class="device-pill-name">' + esc(d.name || d.id) + '</span></button>';
                }).join('')
                + '</div></div>';
        }
        var pills = switcher.querySelectorAll('.device-pill');
        for (var i = 0; i < pills.length; i++) {
            var id = pills[i].getAttribute('data-device');
            var pill = pills[i];
            var b = buckets.get(id);
            // 见过该设备的事件就以事件为准（更实时），否则用清单里的快照
            var online = b && b.bleConnected !== undefined
                ? !!b.bleConnected
                : !!(devices[i] && devices[i].connected);
            pill.classList.toggle('active', id === current);
            pill.setAttribute('aria-selected', id === current ? 'true' : 'false');
            var dot = pill.querySelector('.device-dot');
            if (dot) dot.classList.toggle('is-on', online);
        }
    }

    // 手机页的切换器是固定定位的，出现时要把吸顶导航下面的内容整体下移一条，
    // 否则设备图会从它底下穿过去。桌面的切换器在文档流里，这条 class 对它无副作用。
    function markBody(visible) {
        if (!global.document || !global.document.body || !global.document.body.classList) return;
        global.document.body.classList.toggle('has-device-switcher', !!visible);
    }

    function bindSwitcher(el) {        switcher = el;
        if (!el || !el.addEventListener) return;
        // 事件委托：pills 是随时重建的，逐个挂监听会随重建泄漏
        el.addEventListener('click', function (e) {
            var t = e.target;
            var pill = (t && t.closest) ? t.closest('.device-pill') : null;
            if (!pill) return;
            switchDevice(pill.getAttribute('data-device'));
        });
    }

    // ── 切换与重放 ──

    function changed(prev) {
        var detail = { device: current, previous: prev };
        try {
            if (typeof global.CustomEvent === 'function') {
                global.dispatchEvent(new global.CustomEvent('device-changed', { detail: detail }));
            }
        } catch (e) {}
        for (var i = 0; i < listeners.length; i++) {
            try { listeners[i](detail); } catch (e) {}
        }
    }

    function replayAll() {
        for (var i = 0; i < replay.length; i++) {
            // 单步失败不该让后面的重放全不跑（半边旧数据比明确报错更难查）
            try { replay[i](current); } catch (e) { console.error('device replay step failed:', i, e); }
        }
    }

    function switchDevice(id) {
        if (!id || id === current) return false;
        var prev = current;
        current = id;
        persist(id);
        renderSwitcher();
        replayAll();
        changed(prev);
        return true;
    }

    // ── SSE 分发器 ──

    function deviceOf(msg) { return (msg && msg.device) || PRIMARY; }

    function emitSessionEnd(msg) {
        try {
            if (typeof global.CustomEvent === 'function') {
                global.dispatchEvent(new global.CustomEvent('sse-session-end', { detail: msg }));
            }
        } catch (e) {}
    }

    /**
     * 把一条 SSE 事件按设备落桶，并回答"这条事件属于当前设备吗"。
     * 返回 true 时调用方继续走原有的 DOM 渲染分支（那一行都不用改）；
     * 返回 false 表示事件属于别的设备——只留快照，不动 DOM。
     */
    function dispatch(msg) {
        if (!msg) return false;
        var dev = deviceOf(msg);
        // 归一化写回：页面派发给 charge_history 的 CustomEvent（detail = msg）
        // 因此也自带归属，消费方按 device 过滤即可（§3.12）
        msg.device = dev;
        var b = bucketOf(dev);

        if (msg.type === 'init') {
            if (msg.devices) {
                setDevices(msg.devices);
            } else if (current !== PRIMARY) {
                // 没有 devices 列表 = 单设备（或还没升级的服务端）：此时 primary
                // 是唯一存在的设备，记忆/URL 里的别的 id 一定已经失效。不回退的话
                // 之后每个请求都会 404 device_not_found，界面一片空白且看不出原因。
                var prevDev = current;
                current = PRIMARY;
                persist(PRIMARY);
                changed(prevDev);
            }
        }

        if (msg.type === 'init' || msg.type === 'status') {
            if (msg.connected !== undefined) b.connected = !!msg.connected;
            if (msg.authenticated !== undefined) b.authenticated = !!msg.authenticated;
            b.bleConnected = !!(b.connected && b.authenticated);
            if (msg.firmware_version) b.firmware = msg.firmware_version;
        }
        if (msg.settings) b.settings = msg.settings;
        if (msg.protocol_switches) b.protocolSwitches = msg.protocol_switches;
        if (msg.protocol_extend !== undefined) b.protocolExtend = msg.protocol_extend;
        if (msg.type === 'quality') b.quality = msg;
        // 端口数据的形状是页面私有的（桌面用 API 的 1..4/voltage，手机用 c1..a/v-a-w），
        // 所以落桶方式由页面注册，不在这里猜。
        if (applyPorts) applyPorts(b, msg);

        // 服务级字段（MQTT 连接）不属于任何设备：非当前设备的事件也要吃下去，
        // 否则页头的 MQTT 徽章会停在打开页面那一刻的状态。
        if (dev !== current && serviceEvent) serviceEvent(msg);

        if (devices.length > 1) renderSwitcher();
        if (dev === current) return true;

        // 页面打开时指针可能就停在非 primary（?device= 或 localStorage 记忆）。
        // 这条 init 只带 primary 的状态，当前设备那一桶还是空的 —— 设备清单落地后
        // 立刻重放一次，界面才会变成"当前设备"的样子（随后由页面自取数据）。
        // 单设备（没有清单）时这条分支不会进，改造前的渲染路径完全不变。
        if (msg.type === 'init' && devices.length > 1) replayAll();

        // 别的设备的会话结束：本页列表是按 ?device= 取的，与它无关。但事件仍要
        // 转发（带 device），由 charge_history.js 自己按归属过滤。
        if (msg.type === 'session_end') emitSessionEnd(msg);
        return false;
    }

    // ── 初始化 ──

    /**
     * @param {object} opts
     *   defaults()      → 一台设备的空桶（形状由页面决定）
     *   applyPorts(b,m) → 把事件里的端口数据落进桶 b（形状由页面决定）
     *   service(msg)    → 服务级字段旁路（可选）
     *   replay          → 换设备时按顺序重放的渲染函数数组
     *   switcherId      → 切换器容器 id（可选；页面没有切换器就传空）
     * @returns {Proxy} 当前设备桶的读写视图
     */
    function init(opts) {
        opts = opts || {};
        defaults = opts.defaults || null;
        applyPorts = opts.applyPorts || null;
        serviceEvent = opts.service || null;
        replay = opts.replay || [];
        resolveInitial();
        view = makeView();
        if (opts.switcherId && global.document) bindSwitcher(global.document.getElementById(opts.switcherId));
        return view;
    }

    global.DeviceStore = {
        PRIMARY: PRIMARY,
        init: init,
        api: api,
        url: url,
        dispatch: dispatch,
        switchDevice: switchDevice,
        renderSwitcher: renderSwitcher,
        current: function () { return current; },
        devices: function () { return devices.slice(); },
        get: function (id) { return bucketOf(id); },
        onChange: function (fn) { listeners.push(fn); }
    };
})(typeof window !== 'undefined' ? window : this);
