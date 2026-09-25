// ── API & Config ──
const API_BASE = window.location.origin;

// Localized scene names/descriptions (keys into the i18n resource packs)
function sceneName(mode) { return I18N.t('scene.' + ({ 1: 'ai', 2: 'eco', 3: 'single', 4: 'balanced' }[mode] || 'ai')); }
function sceneDesc(mode) { return I18N.t('scene.desc' + ({ 1: 'Ai', 2: 'Eco', 3: 'Single', 4: 'Balanced' }[mode] || 'Ai')); }
// PIID 6 息屏时间: 数组下标即原始值(1-5)，与米家插件一致。index 0 是占位，非有效设备值。
const SCREEN_TIME_KEYS = ['', 'settings.min5', 'settings.min10', 'settings.min30', 'settings.alwaysOn', 'settings.min1'];
function screenTimeLabel(idx) { return I18N.t(SCREEN_TIME_KEYS[idx] || 'settings.min5'); }

// Update HTML overlay lines on the combined chart using chart's scale positions
function drawPeakLines(chart) {
    try {
        var d = chart && chart._peakData;
        if (!d) return;
        var ys = chart.scales && chart.scales.y;
        if (!ys) return;
        
        var container = chart.canvas && chart.canvas.parentNode;
        if (!container) return;
        var el0w = document.getElementById('chartLines0W');
        var elPeak = document.getElementById('chartLinesPeak');
        var elLabel = document.getElementById('chartLinesLabel');
        if (!el0w || !elPeak || !elLabel) return;
        
        var zeroY = Math.round(ys.getPixelForValue(0));
        var peakY = Math.round(ys.getPixelForValue(d.peakPower));
        var isDark = d.isDark;
        
        // 0W line at bottom
        el0w.style.top = zeroY + 'px';
        el0w.style.borderTopColor = isDark ? 'rgba(255,255,255,0.2)' : 'rgba(0,0,0,0.2)';
        el0w.style.display = 'block';
        
        // Peak line
        if (peakY < zeroY - 4) {
            elPeak.style.top = peakY + 'px';
            elPeak.style.display = 'block';
            elLabel.textContent = Math.round(d.currentPeak) + 'W';
            elLabel.style.display = 'block';
            elLabel.style.top = (peakY - 8) + 'px';
            elLabel.style.right = '4px';
        } else {
            elPeak.style.display = 'none';
            elLabel.style.display = 'none';
        }
    } catch(e) {}
}
const SCENE_IMAGES = { 1: 'ai', 2: 'apple', 3: 'single', 4: 'balance' };
const SCENE_BTN_IMAGES = { 1: 'ai', 2: 'mac', 3: 'single', 4: 'balance' };
const SCENE_PIID = 5;
const PORT_KEYS = ['c1', 'c2', 'c3', 'a'];
const PORT_NAMES = { c1: 'C1', c2: 'C2', c3: 'C3', a: 'USB-A' };
const PORT_COLORS = { c1: '#FF7A00', c2: '#46B4FF', c3: '#89D8F3', a: '#FFD24B' };
const API_PORT_MAP = { 1: 'c1', 2: 'c2', 3: 'c3', 4: 'a' };

// ── State ──
let lastLocalChange = 0;
function markLocalChange() { lastLocalChange = Date.now(); }
function isRecentLocal() { return Date.now() - lastLocalChange < 3000; }
let state = {
    scene: 1,
    screenTime: 1,
    bleConnected: false,
    ports: { c1:{v:0,a:0,w:0,protocol:'idle',enabled:true}, c2:{v:0,a:0,w:0,protocol:'idle',enabled:true}, c3:{v:0,a:0,w:0,protocol:'idle',enabled:true}, a:{v:0,a:0,w:0,protocol:'idle',enabled:true} },
    settings: {},
    firmware: '',
    trickleEnabled: false,
    history: { c1: [], c2: [], c3: [], a: [] },
    protocolSwitches: {},
    protocolExtend: 0,
};
// Real-time chart data buffer: {ts, c1, c2, c3, a}[]
let phoneChartData = [];
const PHONE_CHART_MAX = 150;   // 显示最近 150 个点 ≈ 5 分钟（2s 间隔）
const PHONE_CHART_BUF = 300;   // 缓冲区保留 300 个点 ≈ 10 分钟
function phoneSnapshot() {
    return {
        ts: Date.now(),
        c1: state.bleConnected && state.ports.c1.enabled ? (state.ports.c1.w || 0) : 0,
        c2: state.bleConnected && state.ports.c2.enabled ? (state.ports.c2.w || 0) : 0,
        c3: state.bleConnected && state.ports.c3.enabled ? (state.ports.c3.w || 0) : 0,
        a: state.bleConnected && state.ports.a.enabled ? (state.ports.a.w || 0) : 0,
    };
}
function phoneBuildTimeLabels(buf, offset, count) {
    return buf.slice(offset, offset + count).map(e => {
        const d = new Date(e.ts);
        return String(d.getHours()).padStart(2,'0') + ':' +
               String(d.getMinutes()).padStart(2,'0') + ':' +
               String(d.getSeconds()).padStart(2,'0');
    });
}

// ── API Fetch ──
async function fetchStatus() {
    try {
        const res = await fetch(`${API_BASE}/api/status`);
        const data = await res.json();
        state.bleConnected = data.connected && data.authenticated;
        state.firmware = data.firmware_version || '';
        
        // Map API ports (1,2,3,4) to state format (c1,c2,c3,a)
        if (data.ports) {
            for (const [id, port] of Object.entries(data.ports)) {
                const key = API_PORT_MAP[id];
                if (key && state.ports[key]) {
                    state.ports[key].v = port.voltage || 0;
                    state.ports[key].a = port.current || 0;
                    state.ports[key].w = port.power || 0;
                    if (!isRecentLocal()) state.ports[key].enabled = port.enabled !== false;
                    state.ports[key].protocol = port.protocol || 'idle';
                    state.ports[key].status_raw = port.status_raw;
                }
            }
        }
        if (data.protocol_switches) state.protocolSwitches = data.protocol_switches;
        if (data.protocol_extend !== undefined) state.protocolExtend = data.protocol_extend;
        if (data.settings) {
            state.settings = data.settings;
            const sceneVal = data.settings['5'];
            if (sceneVal && sceneVal > 0 && !isRecentLocal()) state.scene = sceneVal;
            if (!isRecentLocal()) {
                if (data.settings['6'] !== undefined) state.screenTime = data.settings['6'];
                if (data.settings['15'] !== undefined) state.trickleEnabled = data.settings['15'] === 1;
            }
            if (!isRecentLocal()) {
                for (const key of PORT_KEYS) {
                    const v = data.settings[String(DELAY_PIIDS[key])];
                    if (v !== undefined) delayMinutes[key] = parseInt(v) || 0;
                }
            }
        }
        updateConnectionUI();
        renderAll();
    } catch (e) { console.error('API fetch error:', e); }
}

function updateConnectionUI() {
    const dot = document.getElementById('connectDot');
    const status = document.getElementById('connectStatus');
    const btn = document.getElementById('connectBtn');
    if (!dot || !status || !btn) return;
    if (state.bleConnected) {
        hideToast();
        dot.style.background = '#34C759';
        status.textContent = I18N.t('common.connected');
        status.style.color = 'var(--text)';
        btn.textContent = I18N.t('common.disconnect');
        btn.style.background = 'rgba(255,59,48,0.15)';
        btn.style.color = '#FF3B30';
    } else {
        dot.style.background = '#666';
        status.textContent = I18N.t('common.disconnected');
        status.style.color = 'var(--text-dim)';
        btn.textContent = I18N.t('common.connect');
        btn.style.background = 'rgba(255,255,255,0.1)';
        btn.style.color = 'var(--text)';
    }
}

function toast(msg, persist) {
    let el = document.getElementById('toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'toast';
        el.style.cssText = 'position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:999;background:rgba(0,0,0,0.85);color:#fff;padding:10px 20px;border-radius:20px;font-size:14px;pointer-events:none;transition:opacity 0.3s;opacity:0;white-space:nowrap;';
        document.body.appendChild(el);
    }
    clearTimeout(el._timer);
    el.textContent = msg;
    el.style.opacity = '1';
    if (!persist) el._timer = setTimeout(() => el.style.opacity = '0', 3000);
}
function hideToast() { const el = document.getElementById('toast'); if (el) el.style.opacity = '0'; }

async function toggleConnection() {
    const btn = document.getElementById('connectBtn');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    const enable = !state.bleConnected;
    btn.textContent = enable ? I18N.t('common.connectingDots') : I18N.t('common.disconnecting');
    if (enable) toast(I18N.t('phone.connectToast'), true);
    markLocalChange();
    try {
        await fetch(`${API_BASE}/api/enable`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: enable }) });
        let start = Date.now();
        while (Date.now() - start < 10000) {
            await new Promise(r => setTimeout(r, 500));
            const res = await fetch(`${API_BASE}/api/status`);
            const data = await res.json();
            if ((data.connected && data.authenticated) === enable) break;
        }
        await fetchStatus();
    } catch(e) { console.error(e); }
    finally { btn.disabled = false; }
}

// ── Render ──
function renderAll() {
    renderDeviceArea();
    renderSceneCard();
    renderPortCtl();
    renderRateCard();
    renderCharts();
    renderPowerDist();
    renderChargeLimit();
    renderDelayOff();
    renderSettingsUI();
    renderProtocolSwitches();
}
function renderDeviceArea() {
    let totalW = 0, hasAny = false;
    for (const [key, p] of Object.entries(state.ports)) {
        if (p.enabled && p.w > 0) { totalW += p.w; hasAny = true; }
    }

    const unconnectedImg = document.getElementById('unconnectedImg');
    const deviceContainer = document.getElementById('deviceContainer');
    const img = document.getElementById('deviceImg');
    const glow = document.getElementById('darkGlow');
    const badge = document.getElementById('sceneBadge');

    if (hasAny) {
        unconnectedImg.classList.add('hidden');
        deviceContainer.classList.add('show');
        deviceContainer.classList.add('charging');
        glow.classList.add('active');
        badge.classList.add('show');
        document.getElementById('sceneBadgeIcon').src = `static/plugin_imgs/main_card_scene_icon_${SCENE_IMAGES[state.scene]}.png`;
        document.getElementById('sceneBadgeText').textContent = sceneName(state.scene);
    } else {
        unconnectedImg.classList.remove('hidden');
        deviceContainer.classList.remove('show');
        deviceContainer.classList.remove('charging');
        glow.classList.remove('active');
        badge.classList.remove('show');
    }

    // USB overlay modules
    const modulePositions = { c1: 36, c2: 68, c3: 100, a: 133 };
    for (const [key, p] of Object.entries(state.ports)) {
        const mod = document.getElementById('usbModule' + key.toUpperCase());
        const powerEl = document.getElementById('usbPower' + key.toUpperCase());
        if (p.enabled && p.w > 0) {
            mod.classList.add('active');
            mod.style.top = modulePositions[key] + 'px';
            powerEl.textContent = p.w.toFixed(1) + 'W';
        } else {
            mod.classList.remove('active');
        }
    }
}

function renderSceneCard() {
    document.getElementById('sceneName').textContent = sceneName(state.scene);
    const desc = document.getElementById('sceneDesc');
    if (desc) desc.textContent = sceneDesc(state.scene) || '';
    const arrow = document.getElementById('sceneArrow');
    arrow.classList.toggle('show', true);

    document.querySelectorAll('.scene-btn').forEach(btn => {
        const mode = parseInt(btn.dataset.mode);
        const active = mode === state.scene;
        btn.classList.toggle('active', active);
        const imgEl = document.getElementById('sceneImg' + mode);
        if (imgEl) {
            const imgName = SCENE_BTN_IMAGES[mode];
            const theme = isDark ? 'dark' : 'light';
            imgEl.src = `static/plugin_imgs/main_charger_${theme}_${imgName}_${active ? 'on' : 'off'}.png`;
        }
    });
}

function renderPortCtl() {
    for (const key of PORT_KEYS) {
        const p = state.ports[key];
        const enabled = p.enabled !== false;
        const toggle = document.getElementById('toggle' + key.toUpperCase());
        if (toggle) toggle.checked = enabled;
        const icon = document.querySelector(`#toggle${key.toUpperCase()}`).closest('.port-ctl-item').querySelector('.port-ctl-port-icon img');
        if (icon) {
            icon.src = enabled
                ? `static/plugin_imgs/main_card_port_${key}_on.png`
                : `static/plugin_imgs/main_card_port_${key}_off.png`;
        }
    }
}

function renderRateCard() {
    let totalW = 0;
    for (const p of Object.values(state.ports)) {
        if (p.enabled) totalW += p.w;
    }
    document.getElementById('totalPowerNum').textContent = totalW.toFixed(1);

    // Check if C3+USB-A merged (0x11 = merged mode)
    const isMerged = state.ports.c3?.status_raw === 0x11;

    // Port power rows above each chart
    for (const key of PORT_KEYS) {
        const row = document.getElementById('portPower' + key.toUpperCase() + 'Row');
        if (!row) continue;

        if (key === 'a' && isMerged) {
            row.style.display = 'none';
            continue;
        }
        row.style.display = '';

        const p = state.ports[key];
        const enabled = state.ports[key].enabled;
        const w = enabled ? p.w : 0;
        const status = enabled && w > 0 ? w.toFixed(1) : '--';
        const protocol = enabled && p.protocol ? p.protocol : '';
        const name = (key === 'c3' && isMerged) ? 'C3&A' : PORT_NAMES[key];
        row.innerHTML = `<div class="port-power-row" style="margin-bottom:2px;">
            <div class="port-power-dot" style="background:${PORT_COLORS[key]}"></div>
            <span class="port-power-name">${name}</span>
            <span class="port-power-w">${status}</span>
            <span class="port-power-w-unit">W</span>
            <span class="port-power-protocol">${protocol}</span>
        </div>`;
    }
}

let portCharts = {};
let phoneChartDebounce = null;
function renderCharts() {
    // Mini bar chart
    const miniChart = document.getElementById('miniChart');
    if (miniChart) {
        if (!state._totalHistory) state._totalHistory = [];
        const maxVal = Math.max(1, ...state._totalHistory);
        let miniHtml = '';
        for (const v of state._totalHistory) {
            const h = Math.max(2, (v / maxVal) * 100);
            miniHtml += `<div class="mini-bar" style="height:${h}%;opacity:${v > 0 ? 1 : 0.3}"></div>`;
        }
        miniChart.innerHTML = miniHtml;
    }

    // Combined chart with right-to-left effect + 时间标签
    const combinedCanvas = document.getElementById('chartCombined');
    if (combinedCanvas) {
        // 计算动态峰值（使用实际数据，不计填充的零值）
        let currentPeak = 0;
        for (const e of phoneChartData) {
            for (const key of PORT_KEYS) {
                if (e[key] > currentPeak) currentPeak = e[key];
            }
        }
        const peakPower = currentPeak > 0 ? currentPeak * 1.18 : 60;

        // 从 phoneChartData 构建右对齐填充数据 + 时间标签
        function buildChartData() {
            const showBuf = phoneChartData.slice(-PHONE_CHART_MAX);
            const padding = PHONE_CHART_MAX - showBuf.length;
            const padLabels = new Array(padding).fill('--:--:--');
            const padZeros = new Array(padding).fill(0);
            const realLabels = phoneBuildTimeLabels(phoneChartData, Math.max(0, phoneChartData.length - showBuf.length), showBuf.length);
            const labels = [...padLabels, ...realLabels];
            const datasets = PORT_KEYS.map(key => ({
                data: [...padZeros, ...showBuf.map(e => e[key])]
            }));
            return { labels, datasets };
        }

        if (portCharts.combined) {
            // Update existing chart in place (no flicker)
            const chart = portCharts.combined;
            const { labels, datasets } = buildChartData();
            chart.data.labels = labels;
            PORT_KEYS.forEach((key, i) => {
                chart.data.datasets[i].data = datasets[i].data;
            });
            chart.options.scales.y.max = peakPower;
            chart.update('none');
            chart._peakData = { peakPower, currentPeak, isDark };
            drawPeakLines(chart);
        } else {
            // First render: create chart with right-to-left padding
            const { labels, datasets } = buildChartData();
            portCharts.combined = new Chart(combinedCanvas, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: PORT_KEYS.map((key, i) => ({
                        label: PORT_NAMES[key],
                        data: datasets[i].data,
                        borderColor: PORT_COLORS[key],
                        borderWidth: 1.5,
                        tension: 0.4,
                        pointRadius: 0,
                        fill: false,
                    }))
                },
                options: {
                    responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
                    interaction: { intersect: false, mode: 'index' },
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { display: true, grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: '#888', maxTicksLimit: 8, font: { size: 9 }, maxRotation: 0 } },
                        y: { display: false, min: 0, max: peakPower },
                    }
                }
            });
            portCharts.combined._peakData = { peakPower, currentPeak, isDark };
            drawPeakLines(portCharts.combined);
        }
    }
}

function renderSettingsUI() {
    const st = document.getElementById('screenTimeVal');
    if (st) st.innerHTML = screenTimeLabel(state.screenTime) + ' <img src="static/plugin_imgs/main_charger_dark_icon_more.png" alt="">';
    const tt = document.getElementById('toggleTrickle');
    if (tt) tt.checked = state.trickleEnabled;
}

function renderProtocolSwitches() {
    const sw = state.protocolSwitches;
    if (!sw || Object.keys(sw).length === 0) return;
    const labels = { pd: 'PD', pps: 'PPS', ufcs: 'UFCS', scp: 'SCP' };
    for (const port of PORT_KEYS) {
        const ps = sw[port];
        const el = document.getElementById('portProtos_' + port);
        if (!el || !ps) continue;
        const protoKeys = Object.keys(ps);
        let html = '';
        for (const pk of protoKeys) {
            // PD 关闭时隐藏 PPS 按钮（硬件不支持）
            if ((port === 'c1' || port === 'c2') && pk === 'pps' && !sw[port].pd) continue;
            const on = ps[pk];
            html += `<button class="proto-btn ${on ? 'on' : ''}" data-port="${port}" data-proto="${pk}" onclick="phoneToggleProtocol(this)">${labels[pk] || pk}</button>`;
        }
        // C1/C2 提示 PD 与 PPS 关联，C3/A 提示需插拔
        if (port === 'c1' || port === 'c2') {
            html += `<div style="font-size:9px;color:var(--text-dim);margin-top:2px;">${I18N.t('modal.ppsNote')}</div>`;
        } else {
            html += `<div style="font-size:9px;color:var(--text-dim);margin-top:2px;">${I18N.t('phone.replugNote')}</div>`;
        }
        el.innerHTML = html;
    }
}

async function phoneToggleProtocol(btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    const port = btn.dataset.port;
    const proto = btn.dataset.proto;
    // 用显式 action + 确定值，避免「SSE 广播已成真→再用 ! 反转」的竞态，
    // 以及「fetch 失败仍乐观取反」的双重 bug（与 index 页同源修复）。
    const wasOn = !!(state.protocolSwitches[port] && state.protocolSwitches[port][proto]);
    const action = wasOn ? 'off' : 'on';
    try {
        const res = await fetch(`${API_BASE}/api/protocol`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ port, protocol: proto, action })
        });
        const data = await res.json();
        // 仅在成功时优化更新；写入确定值（非 ! 取反），与 SSE 收敛一致
        if (data && data.ok && state.protocolSwitches[port]) {
            state.protocolSwitches[port][proto] = action === 'on';
            renderProtocolSwitches();
        }
    } catch (e) { console.error('Protocol toggle error:', e); }
    finally { btn.disabled = false; }
}

function renderPowerDist() {
    const bar = document.getElementById('powerDist');
    const text = document.getElementById('powerDistText');
    if (!bar || !text) return;
    let powers = [];
    let totalActive = 0;
    for (const key of PORT_KEYS) {
        const p = state.ports[key];
        const w = (state.ports[key].enabled && p.w > 0) ? p.w : 0;
        totalActive += w;
        powers.push({ key, name: PORT_NAMES[key], w, color: PORT_COLORS[key] });
    }
    const total = totalActive || 1;
    bar.innerHTML = powers.map(x => `<div style="width:${(x.w/total*100).toFixed(1)}%;height:100%;background:${x.color};transition:width 0.5s;"></div>`).join('');
    text.innerHTML = powers.map(x => {
        const pct = (x.w / total * 100).toFixed(0);
        return `<span style="color:${x.color};${x.w > 0 ? '' : 'opacity:0.3;'}">${x.name} ${pct}%</span>`;
    }).join('');
}

// ── Charge Limit (充到指定 Wh 自动关断) ──
// 数据契约/请求形状/交互语义由 charge_limit.js 统一提供，见该文件头部说明。
// 配色走 phone.css 的 .charge-limit-* 类（--limit-* 变量随 body.light 切换）；
// 不要把 PORT_COLORS 内联进 style——内联优先级高于样式表且不随主题变化。
//
// 手机端把 4 个端口叠成一沓卡：只有栈顶那张完整可见，其余按 depth 逐层下移并
// 收窄，只露出底部一条边；左右滑动或点下方指示点切换。整卡高度因此从 4 份降到
// 1 份（约 -70%），这是这次改版的目的。栈序存在 limitOrder 里，[0] 为栈顶。
let chargeLimitRendered = false;
let limitOrder = PORT_KEYS.slice();
let limitFlipBusy = false;   // 翻页动画期间忽略新手势，避免状态错位
let limitDrag = null;
let limitSuppressClick = false;   // 刚划过卡：吃掉紧随其后的 click

function limitCardEl(key) { return document.getElementById('limitCard_' + key); }

function limitCardHtml(key, CL) {
    return `<div class="charge-limit-card ${key}" id="limitCard_${key}">
                <div class="charge-limit-head">
                    <div class="charge-limit-title">
                        <div class="charge-limit-dot"></div>
                        <span class="charge-limit-name">${PORT_NAMES[key]}</span>
                    </div>
                    <span class="charge-limit-status" id="limitStatus_${key}">${I18N.t('chargeLimit.off')}</span>
                </div>
                <div class="charge-limit-track"><div class="charge-limit-fill" id="limitBar_${key}"></div></div>
                <div class="charge-limit-progress" id="limitProgress_${key}"></div>
                <div class="charge-limit-inputs">
                    <input type="number" class="charge-limit-wh" id="limitWh_${key}" min="0" max="1000" step="1" placeholder="${I18N.t('chargeLimit.placeholder')}">
                    <select class="charge-limit-mode" id="limitMode_${key}">
                        <option value="once">${I18N.t('chargeLimit.once')}</option>
                        <option value="always">${I18N.t('chargeLimit.always')}</option>
                    </select>
                </div>
                <div class="charge-limit-quick">
                    ${CL.QUICK_WH.map(w => `<button class="charge-limit-chip" onclick="setChargeLimitQuick('${key}', ${w})">${w}${I18N.t('chargeLimit.unit')}</button>`).join('')}
                </div>
                <div class="charge-limit-actions">
                    <button class="charge-limit-action charge-limit-set" id="limitSet_${key}" onclick="applyChargeLimit('${key}')">${I18N.t('chargeLimit.set')}</button>
                    <button class="charge-limit-action charge-limit-clear" id="limitClear_${key}" onclick="clearChargeLimit('${key}')">${I18N.t('chargeLimit.clear')}</button>
                </div>
            </div>`;
}

// 指示点：aria-label 带上端口名与当前状态，不滑动也能被读屏读到各端口状态。
function limitDotHtml(key, CL) {
    return `<button type="button" class="charge-limit-dotnav ${key}" id="limitDot_${key}"
                    onclick="showChargeLimitPort('${key}')" aria-label="${PORT_NAMES[key]} ${CL.statusText(key)}"></button>`;
}

function renderChargeLimit() {
    const deck = document.getElementById('chargeLimitDeck');
    if (!deck || typeof ChargeLimit === 'undefined') return;
    const CL = ChargeLimit;

    if (!chargeLimitRendered) {
        const dots = document.getElementById('chargeLimitDots');
        deck.innerHTML = PORT_KEYS.map(k => limitCardHtml(k, CL)).join('');
        if (dots) dots.innerHTML = PORT_KEYS.map(k => limitDotHtml(k, CL)).join('');
        bindLimitGesture();
        chargeLimitRendered = true;
    }
    layoutLimitDeck();
    releaseLimitDeckIntro();   // --depth 刚落盘，首帧不该播"入场动画"
    updateChargeLimitUI();
}

// 把栈序写进 DOM：depth 决定下移量与收窄量（真正的位置/尺寸在 phone.css 里）。
function layoutLimitDeck() {
    const deck = document.getElementById('chargeLimitDeck');
    if (deck && deck.style && typeof deck.style.setProperty === 'function') {
        deck.style.setProperty('--limit-count', String(limitOrder.length));
    }
    for (const key of PORT_KEYS) {
        const el = limitCardEl(key);
        if (!el) continue;
        const depth = limitOrder.indexOf(key);
        if (el.style && typeof el.style.setProperty === 'function') {
            el.style.setProperty('--depth', String(depth < 0 ? PORT_KEYS.length : depth));
        }
        el.style.zIndex = String(20 - depth);
        // 下层卡被上层完全盖住、只剩一条边，键盘和读屏不该停在看不见的控件上。
        // inert 不支持时就什么都不设：宁可让它们可聚焦，也不要 aria-hidden 盖住
        // 仍可聚焦的元素（那是明确的 ARIA 违规）。
        if ('inert' in el) {
            el.inert = depth !== 0;
            el.setAttribute('aria-hidden', depth !== 0 ? 'true' : 'false');
        }
    }
}

// 首帧不播动画：--depth 是渲染后才写上去的，不关掉过渡会让 4 张卡在页面加载时
// 当着用户的面"滑"一遍。带 .no-anim 强制一次回流后再摘掉即可。
function releaseLimitDeckIntro() {
    const deck = document.getElementById('chargeLimitDeck');
    if (!deck || !deck.classList || typeof deck.classList.contains !== 'function') return;
    if (!deck.classList.contains('no-anim')) return;
    void deck.offsetWidth;
    deck.classList.remove('no-anim');
}

// ── 翻页 ──

// steps: 正数向后翻。outX: 被换下那张飞出的方向（-1 左 / +1 右）。
function flipLimitDeck(steps, outX) {
    const el = limitCardEl(limitOrder[0]);
    limitOrder = ChargeLimit.flipOrder(limitOrder, steps);
    limitFlipBusy = true;
    if (el) {
        el.style.transition = '';   // 恢复样式表里的过渡
        el.style.transform = 'translate(' + outX * 118 + '%, 0)';
        el.style.opacity = '0';
    }
    layoutLimitDeck();              // 其余卡片各自前进一格（带过渡）
    updateChargeLimitUI();
    setTimeout(function () {
        if (el) {
            // 让飞出的那张无声地落回牌堆末位：先关过渡再改位，否则看得见它飞回来
            el.style.transition = 'none';
            el.style.transform = '';
            el.style.opacity = '';
            void el.offsetWidth;
            el.style.transition = '';
        }
        limitFlipBusy = false;
    }, 320);
}

// 点指示点直接跳到某个端口。取步数较短的那个方向转，动画方向才跟手感一致。
function showChargeLimitPort(key) {
    if (limitFlipBusy || limitDrag) return;
    const n = PORT_KEYS.length;
    const from = limitOrder.indexOf(key);
    if (from <= 0) return;
    const steps = from <= n - from ? from : from - n;
    flipLimitDeck(steps, steps > 0 ? -1 : 1);
}

function bindLimitGesture() {
    const deck = document.getElementById('chargeLimitDeck');
    if (!deck || typeof deck.addEventListener !== 'function') return;
    deck.addEventListener('pointerdown', onLimitPointerDown);
    deck.addEventListener('pointermove', onLimitPointerMove);
    deck.addEventListener('pointerup', onLimitPointerUp);
    deck.addEventListener('pointercancel', onLimitPointerUp);
    // 捕获阶段拦下划卡末尾的那次 click：手势可以从快捷值按钮上起手，否则
    // "想翻页"会顺手把限额设成滑过的那一档。
    deck.addEventListener('click', onLimitDeckClick, true);
}

function onLimitDeckClick(e) {
    if (!limitSuppressClick) return;
    limitSuppressClick = false;
    e.stopPropagation();
    if (e.preventDefault) e.preventDefault();
}

function onLimitPointerDown(e) {
    if (limitFlipBusy || limitDrag) return;
    limitSuppressClick = false;
    // 输入框/下拉里的按下是原生编辑操作，不参与翻页
    const tag = e.target && e.target.tagName ? String(e.target.tagName).toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || tag === 'OPTION') return;
    const el = limitCardEl(limitOrder[0]);
    if (!el) return;
    limitDrag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, dx: 0, moved: false, el: el };
}

function onLimitPointerMove(e) {
    const d = limitDrag;
    if (!d || e.pointerId !== d.id) return;
    const dx = e.clientX - d.x0;
    const dy = e.clientY - d.y0;
    if (!d.moved) {
        if (Math.abs(dx) < 8) return;
        // 纵向为主的手势交还给页面滚动（touch-action: pan-y 已放行纵向）
        if (Math.abs(dx) <= Math.abs(dy)) { limitDrag = null; return; }
        d.moved = true;
        d.el.style.transition = 'none';   // 跟手期间不能有过渡
        const deck = document.getElementById('chargeLimitDeck');
        if (deck && deck.setPointerCapture) {
            try { deck.setPointerCapture(e.pointerId); } catch (err) { /* 指针已失效，忽略 */ }
        }
    }
    d.dx = dx;
    d.el.style.transform = 'translate(' + dx + 'px, 0)';
    d.el.style.opacity = String(Math.max(0.4, 1 - Math.abs(dx) / 420));   // 渐隐，露出下一张
}

function onLimitPointerUp(e) {
    const d = limitDrag;
    if (!d || e.pointerId !== d.id) return;
    limitDrag = null;
    if (!d.moved) return;             // 只是点了下按钮，交给原生 click
    // 抖动不等于划卡：只有位移明显时才吃掉紧随其后的 click。设限额是这张卡的
    // 主操作，要是连"手指抖了 10px"的点击也一起吞掉，用户会以为没点上。
    if (Math.abs(d.dx) > ChargeLimit.SWIPE_MIN_PX / 2) {
        limitSuppressClick = true;
        // 400ms 兜底：万一没有 click 跟上来，也不会一直吞掉后续的键盘激活
        setTimeout(function () { limitSuppressClick = false; }, 400);
    }
    d.el.style.transition = '';
    // 被系统打断（pointercancel，例如浏览器接管了滚动）一律回弹，不做翻页判断
    const dir = e.type === 'pointercancel'
        ? 0
        : ChargeLimit.swipeDecision(d.dx, Number(d.el.offsetWidth) || 320);
    if (!dir) {                       // 没过阈值：回弹
        d.el.style.transform = '';
        d.el.style.opacity = '';
        return;
    }
    flipLimitDeck(dir, dir > 0 ? -1 : 1);
}

function updateChargeLimitUI() {
    if (typeof ChargeLimit === 'undefined') return;
    const CL = ChargeLimit;
    for (const key of PORT_KEYS) {
        const e = CL.entryFor(key);
        const statusEl = document.getElementById(`limitStatus_${key}`);
        if (statusEl) {
            statusEl.textContent = CL.statusText(key);
            // 已设限额时用端口色（.on 由 CSS 取变量，同样跟随主题）
            statusEl.classList.toggle('on', e.wh > 0);
        }
        const barEl = document.getElementById(`limitBar_${key}`);
        if (barEl) barEl.style.width = CL.progressPct(key) + '%';
        const progEl = document.getElementById(`limitProgress_${key}`);
        if (progEl) progEl.textContent = CL.progressText(key);
        // 不覆盖正在编辑的输入框
        const inputEl = document.getElementById(`limitWh_${key}`);
        if (inputEl && document.activeElement !== inputEl) {
            inputEl.value = e.wh > 0 ? e.wh : '';
        }
        const modeEl = document.getElementById(`limitMode_${key}`);
        if (modeEl && !modeEl.dataset.touched) modeEl.value = e.mode || 'once';
        // 指示点：空心/实心/警示色 + 当前项拉长，见 phone.css 注释
        const dotEl = document.getElementById(`limitDot_${key}`);
        if (dotEl) {
            const set = e.wh > 0;
            dotEl.classList.toggle('is-set', set);
            dotEl.classList.toggle('is-fired', set && !!e.fired);
            dotEl.classList.toggle('is-current', limitOrder[0] === key);
            dotEl.setAttribute('aria-current', limitOrder[0] === key ? 'true' : 'false');
            dotEl.setAttribute('aria-label', PORT_NAMES[key] + ' ' + CL.statusText(key));
        }
    }
}

async function refreshChargeLimit() {
    if (typeof ChargeLimit === 'undefined') return;
    await ChargeLimit.fetchLimits();
    updateChargeLimitUI();
}

function setChargeLimitQuick(key, wh) {
    const input = document.getElementById(`limitWh_${key}`);
    if (input) input.value = wh;
    applyChargeLimit(key);
}

async function applyChargeLimit(key) {
    const input = document.getElementById(`limitWh_${key}`);
    const modeEl = document.getElementById(`limitMode_${key}`);
    const wh = ChargeLimit.parseWhInput(input ? input.value : '');
    if (wh === null) { toast(I18N.t('chargeLimit.saveFailed', { msg: I18N.t('chargeLimit.placeholder') })); return; }
    if (modeEl) modeEl.dataset.touched = '1';
    const res = await ChargeLimit.saveLimit(key, wh, modeEl ? modeEl.value : null);
    if (modeEl) modeEl.dataset.touched = '';
    toast(res.ok
        ? (wh > 0 ? I18N.t('chargeLimit.saved') : I18N.t('chargeLimit.cleared'))
        : I18N.t('chargeLimit.saveFailed', { msg: res.error }));
    updateChargeLimitUI();
}

async function clearChargeLimit(key) {
    const res = await ChargeLimit.saveLimit(key, 0, null);
    toast(res.ok ? I18N.t('chargeLimit.cleared')
                 : I18N.t('chargeLimit.saveFailed', { msg: res.error }));
    updateChargeLimitUI();
}

// ── Delay Off ──
const delayMinutes = { c1: 0, c2: 0, c3: 0, a: 0 };
const DELAY_PIIDS = { c1: 9, c2: 10, c3: 11, a: 12 };
function renderDelayOff() {
    const grid = document.getElementById('delayOffGrid');
    if (!grid) return;
    // Only show active ports (w > 0)
    const activeKeys = PORT_KEYS.filter(key => state.ports[key].v > 0);
    if (activeKeys.length === 0) { grid.innerHTML = `<div style="font-size:13px;color:var(--text-dim);text-align:center;padding:12px;">${I18N.t('phone.noActivePorts')}</div>`; return; }
    let html = '';
    activeKeys.forEach((key, idx) => {
        const min = delayMinutes[key] || 0;
        const dotColor = PORT_COLORS[key];
        const sliderId = `delaySlider_${key}`;
        html += `<div>
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                <div style="display:flex;align-items:center;gap:8px;">
                    <div style="width:10px;height:10px;border-radius:50%;background:${dotColor};flex-shrink:0;"></div>
                    <span style="font-size:15px;color:var(--text);">${PORT_NAMES[key]}</span>
                </div>
                <span id="delayVal_${key}" style="font-size:14px;font-weight:600;color:${min>0?dotColor:'var(--text-dim)'};">${min > 0 ? I18N.t('common.minutes', { count: min }) : I18N.t('common.notSet')}</span>
            </div>
            <input type="range" id="${sliderId}" min="0" max="240" value="${min}" step="1" class="delay-slider"
                style="background:linear-gradient(to right,${dotColor} ${min/240*100}%,rgba(255,255,255,0.08) ${min/240*100}%); --thumb-color:${dotColor};">
                <style>#${sliderId}::-webkit-slider-thumb{background:${dotColor}} #${sliderId}::-moz-range-thumb{background:${dotColor}}</style>
        </div>`;
        if (idx < activeKeys.length - 1) html += `<div style="height:1px;background:rgba(255,255,255,0.04);margin:18px 0;"></div>`;
    });
    grid.innerHTML = html;
    for (const key of activeKeys) {
        const slider = document.getElementById(`delaySlider_${key}`);
        if (slider) {
            slider.oninput = function() {
                const v = parseInt(this.value);
                delayMinutes[key] = v;
                const valEl = document.getElementById('delayVal_' + key);
                if (valEl) {
                    valEl.textContent = v > 0 ? I18N.t('common.minutes', { count: v }) : I18N.t('common.notSet');
                    valEl.style.color = v > 0 ? PORT_COLORS[key] : 'var(--text-dim)';
                }
                this.style.background = `linear-gradient(to right,${PORT_COLORS[key]} ${v/240*100}%,rgba(255,255,255,0.08) ${v/240*100}%)`;
            };
            slider.onchange = async function() {
                const v = parseInt(this.value);
                markLocalChange();
                try { await fetch(`${API_BASE}/api/set`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ piid: DELAY_PIIDS[key], value: v }) }); } catch(e) {}
            };
        }
    }
}

// ── Actions ──
async function setScene(mode) {
    state.scene = mode;
    markLocalChange();
    renderAll();
    try {
        await fetch(`${API_BASE}/api/set`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ piid: SCENE_PIID, value: mode }) });
    } catch(e) { console.error('setScene error:', e); }
}

async function togglePort(key) {
    const on = !state.ports[key].enabled;
    state.ports[key].enabled = on;
    markLocalChange();
    renderAll();
    try {
        await fetch(`${API_BASE}/api/port`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ port: key, action: on ? 'on' : 'off' }) });
    } catch(e) { console.error(e); }
}

async function toggleTrickle() {
    state.trickleEnabled = !state.trickleEnabled;
    markLocalChange();
    try { await fetch(`${API_BASE}/api/set`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ piid: 15, value: state.trickleEnabled ? 1 : 0 }) }); } catch(e) {}
}

async function cycleScreenTime() {
    // 有效原始值 1-5（1=5分钟,2=10分钟,3=30分钟,4=常亮,5=1分钟），跳过占位的 index 0
    state.screenTime = ((state.screenTime - 1) % 5 + 6) % 5 + 1;
    document.getElementById('screenTimeVal').innerHTML =
        screenTimeLabel(state.screenTime) + ' <img src="static/plugin_imgs/main_charger_dark_icon_more.png" alt="">';
    markLocalChange();
    try { await fetch(`${API_BASE}/api/set`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ piid: 6, value: state.screenTime }) }); } catch(e) {}
}

// ── Top-view fade on scroll ──
const topView = document.querySelector('.top-view');
function handleFade(scrollY) {
    if (topView) {
        var progress = Math.min(1, Math.max(0, (scrollY - 40) / 220));
        topView.style.opacity = (1 - progress).toFixed(3);
    }
}
const phone = document.querySelector('.phone');
if (phone) phone.addEventListener('scroll', () => handleFade(phone.scrollTop));
window.addEventListener('scroll', () => handleFade(window.scrollY));

// ── Theme Toggle ──
let isDark = true;
function toggleTheme() {
    isDark = !isDark;
    document.body.classList.toggle('light', !isDark);
    const deviceImg = document.getElementById('deviceImg');
    if (deviceImg) {
        deviceImg.src = isDark ? 'static/plugin_imgs/main_charger_dark_ad1204_all.png' : 'static/plugin_imgs/main_charger_light_ad1204_all.png';
    }
    document.getElementById('themeBtn').textContent = isDark ? '☀️' : '🌙';
    renderSceneCard();
    renderCharts();
}

// ── 数据推送（仅定时器调用，避免去抖与定时器重复写入） ──
function phonePushData() {
    let totalW = 0;
    for (const p of Object.values(state.ports)) {
        if (p.enabled) totalW += p.w;
    }
    if (!state._totalHistory) state._totalHistory = [];
    if (totalW > 0 || state._totalHistory.length > 0) {
        state._totalHistory.push(totalW);
        if (state._totalHistory.length > 30) state._totalHistory.shift();
    }
    let hasData = false;
    for (const key of PORT_KEYS) {
        if (state.bleConnected && state.ports[key].enabled && state.ports[key].w > 0) hasData = true;
    }
    if (phoneChartData.length > 0 || hasData) {
        phoneChartData.push(phoneSnapshot());
        if (phoneChartData.length > PHONE_CHART_BUF) phoneChartData.shift();
    }
}

// ── Init ──
renderAll();
initPhoneSSE();
// 定时器：先 push 数据再渲染（去抖只渲染不 push，杜绝重复点）
setInterval(() => { phonePushData(); renderCharts(); }, 2000);
// 安全兜底：每 30s 轮询 /api/status 校正因 SSE 队列丢事件导致的连接状态偏差
setInterval(async () => {
    try {
        const res = await fetch(`${API_BASE}/api/status`);
        const data = await res.json();
        const realConn = data.connected && data.authenticated;
        if (realConn !== state.bleConnected) {
            state.bleConnected = realConn;
            updateConnectionUI();
            if (realConn && data.ports) {
                for (const [id, port] of Object.entries(data.ports)) {
                    const key = API_PORT_MAP[id];
                    if (key && state.ports[key]) {
                        state.ports[key].v = port.voltage || 0;
                        state.ports[key].a = port.current || 0;
                        state.ports[key].w = port.power || 0;
                        state.ports[key].enabled = port.enabled !== false;
                        state.ports[key].protocol = port.protocol || 'idle';
                    }
                }
                renderAll();
            }
        }
    } catch (e) {}
}, 30000);

// ── SSE (Server-Sent Events) ──
function initPhoneSSE() {
    const evtSource = new EventSource(`${API_BASE}/api/events`);
    evtSource.onopen = () => {
        document.getElementById('connectDot').style.background = '#34C759';
        // SSE init event handles state sync; no fetchStatus needed
    };
    evtSource.onmessage = (e) => {
        try {
            const msg = JSON.parse(e.data);
            switch (msg.type) {
                case 'init':
                    applyFullStatus(msg);
                    break;
                case 'port_update':
                    applyPortUpdate(msg.port_id, msg.data);
                    break;
                case 'status':
                    state.bleConnected = msg.connected && msg.authenticated;
                    if (msg.firmware_version) state.firmware = msg.firmware_version;
                    updateConnectionUI();
                    if (!state.bleConnected) {
                        // Disconnect: clear port data to avoid showing stale values
                        for (const key of PORT_KEYS) {
                            state.ports[key].v = 0;
                            state.ports[key].a = 0;
                            state.ports[key].w = 0;
                            state.ports[key].protocol = 'idle';
                        }
                        renderDeviceArea();
                        renderRateCard();
                        renderPowerDist();
                    } else if (msg.ports) {
                        // Reconnect: apply full state
                        for (const [id, port] of Object.entries(msg.ports)) {
                            const key = API_PORT_MAP[id];
                            if (key && state.ports[key]) {
                                state.ports[key].v = port.voltage || 0;
                                state.ports[key].a = port.current || 0;
                                state.ports[key].w = port.power || 0;
                                state.ports[key].enabled = port.enabled !== false;
                                state.ports[key].protocol = port.protocol || 'idle';
                            }
                        }
                        renderDeviceArea();
                        renderRateCard();
                        renderPowerDist();
                    }
                    if (msg.settings) applySettingsUpdate(msg.settings);
                    if (msg.protocol_switches) state.protocolSwitches = msg.protocol_switches;
                    if (msg.protocol_extend !== undefined) state.protocolExtend = msg.protocol_extend;
                    break;
                case 'settings':
                    if (msg.settings) applySettingsUpdate(msg.settings);
                    break;
                case 'protocol':
                    if (msg.switches) state.protocolSwitches = msg.switches;
                    if (msg.protocol_extend !== undefined) state.protocolExtend = msg.protocol_extend;
                    renderProtocolSwitches();
                    break;
                case 'session_end':
                    window.dispatchEvent(new CustomEvent('sse-session-end', { detail: msg }));
                    break;
            }
        } catch (err) { console.error('SSE parse error:', err); }
    };
    evtSource.onerror = () => {
        document.getElementById('connectDot').style.background = '#666';
    };
    // bfcache: close on leave, reopen on return
    window.addEventListener('pagehide', () => { if (evtSource) { evtSource.close(); evtSource = null; } });
    window.addEventListener('pageshow', () => { if (typeof initPhoneSSE === 'function') initPhoneSSE(); });
}

function applyFullStatus(data) {
    state.bleConnected = data.connected && data.authenticated;
    state.firmware = data.firmware_version || '';
    if (data.ports) {
        for (const [id, port] of Object.entries(data.ports)) {
            const key = API_PORT_MAP[id];
            if (key && state.ports[key]) {
                state.ports[key].v = port.voltage || 0;
                state.ports[key].a = port.current || 0;
                state.ports[key].w = port.power || 0;
                if (!isRecentLocal()) state.ports[key].enabled = port.enabled !== false;
                state.ports[key].protocol = port.protocol || 'idle';
                state.ports[key].status_raw = port.status_raw;
            }
        }
    }
    if (data.protocol_switches) state.protocolSwitches = data.protocol_switches;
    if (data.protocol_extend !== undefined) state.protocolExtend = data.protocol_extend;
    if (data.settings) applySettingsUpdate(data.settings);
    updateConnectionUI();
    renderAll();
}

function applyPortUpdate(portId, portData) {
    const key = API_PORT_MAP[portId];
    if (!key || !state.ports[key]) return;
    state.ports[key].v = portData.voltage || 0;
    state.ports[key].a = portData.current || 0;
    state.ports[key].w = portData.power || 0;
    if (!isRecentLocal()) state.ports[key].enabled = portData.enabled !== false;
    state.ports[key].protocol = portData.protocol || 'idle';
    state.ports[key].status_raw = portData.status_raw;
    // 500ms 去抖刷新组合图表（数据到达时及时更新，稳定期由 2s 定时器补充）
    if (phoneChartDebounce) clearTimeout(phoneChartDebounce);
    phoneChartDebounce = setTimeout(() => {
        phoneChartDebounce = null;
        renderCharts();
    }, 500);
    // Incremental render — skip chart (decoupled to debounce+2s timer)
    renderDeviceArea();
    renderRateCard();
    renderPowerDist();
    renderDelayOff();
}

function applySettingsUpdate(settings) {
    state.settings = settings;
    const sceneVal = settings['5'];
    if (sceneVal && sceneVal > 0 && !isRecentLocal()) state.scene = sceneVal;
    if (!isRecentLocal()) {
        if (settings['6'] !== undefined) state.screenTime = settings['6'];
        if (settings['15'] !== undefined) state.trickleEnabled = settings['15'] === 1;
    }
    if (!isRecentLocal()) {
        for (const key of PORT_KEYS) {
            const v = settings[String(DELAY_PIIDS[key])];
            if (v !== undefined) delayMinutes[key] = parseInt(v) || 0;
        }
    }
    renderAll();
}

// ── Charge History ──
if (typeof startChargeHistoryAutoRefresh === 'function') {
    startChargeHistoryAutoRefresh('chargeSessionList', 'chargeStats', 'today', 2000);
}

// ── Charge Limit: 初次加载 + 进度轮询（本会话已充 Wh 不在 /api/status 里） ──
refreshChargeLimit();
setInterval(refreshChargeLimit, 5000);

// ── Locale change: re-render all dynamic content ──
if (typeof I18N !== 'undefined' && typeof I18N.onChange === 'function') {
    I18N.onChange(function () {
        updateConnectionUI();
        chargeLimitRendered = false;   // 卡片文案（含 once/always 选项）需重建
        renderAll();
    });
}
