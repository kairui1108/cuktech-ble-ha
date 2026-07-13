// ── API & Config ──
const API_BASE = window.location.origin;
const SCENE_NAMES = { 1: 'AI模式', 2: '数码生态', 3: '单口模式', 4: '均衡模式' };
const SCENE_IMAGES = { 1: 'ai', 2: 'apple', 3: 'single', 4: 'balance' };
const SCENE_BTN_IMAGES = { 1: 'ai', 2: 'mac', 3: 'single', 4: 'balance' };
const SCENE_PIID = 5;
const SCREEN_TIMES = ['5分钟', '1分钟', '10分钟', '30分钟', '常亮'];
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
    screenTime: 0,
    bleConnected: false,
    ports: { c1:{v:0,a:0,w:0,protocol:'idle',enabled:true}, c2:{v:0,a:0,w:0,protocol:'idle',enabled:true}, c3:{v:0,a:0,w:0,protocol:'idle',enabled:true}, a:{v:0,a:0,w:0,protocol:'idle',enabled:true} },
    settings: {},
    firmware: '',
    trickleEnabled: false,
    history: { c1: [], c2: [], c3: [], a: [] }
};

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
                }
            }
        }
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
        status.textContent = '已连接';
        status.style.color = 'var(--text)';
        btn.textContent = '断开设备';
        btn.style.background = 'rgba(255,59,48,0.15)';
        btn.style.color = '#FF3B30';
    } else {
        dot.style.background = '#666';
        status.textContent = '未连接';
        status.style.color = 'var(--text-dim)';
        btn.textContent = '连接设备';
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
    btn.textContent = enable ? '连接中...' : '断开中...';
    if (enable) toast('正在连接设备，请稍候...', true);
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
    renderDelayOff();
    renderSettingsUI();
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
        document.getElementById('sceneBadgeText').textContent = SCENE_NAMES[state.scene];
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
    document.getElementById('sceneName').textContent = SCENE_NAMES[state.scene];
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

    // Port power rows above each chart
    for (const key of PORT_KEYS) {
        const p = state.ports[key];
        const enabled = state.ports[key].enabled;
        const w = enabled ? p.w : 0;
        const status = enabled && w > 0 ? w.toFixed(1) : '--';
        const protocol = enabled && p.protocol ? p.protocol : '';
        const row = document.getElementById('portPower' + key.toUpperCase() + 'Row');
        if (row) {
            row.innerHTML = `<div class="port-power-row" style="margin-bottom:2px;">
                <div class="port-power-dot" style="background:${PORT_COLORS[key]}"></div>
                <span class="port-power-name">${PORT_NAMES[key]}</span>
                <span class="port-power-w">${status}</span>
                <span class="port-power-w-unit">W</span>
                <span class="port-power-protocol">${protocol}</span>
            </div>`;
        }
    }
}

let portCharts = {};
function renderCharts() {
    let totalW = 0;
    for (const p of Object.values(state.ports)) {
        if (p.enabled) totalW += p.w;
    }
    if (!state._totalHistory) state._totalHistory = [];
    state._totalHistory.push(totalW);
    if (state._totalHistory.length > 30) state._totalHistory.shift();

    // Mini bar chart
    const miniChart = document.getElementById('miniChart');
    if (miniChart) {
        const maxVal = Math.max(1, ...state._totalHistory);
        let miniHtml = '';
        for (const v of state._totalHistory) {
            const h = Math.max(2, (v / maxVal) * 100);
            miniHtml += `<div class="mini-bar" style="height:${h}%;opacity:${v > 0 ? 1 : 0.3}"></div>`;
        }
        miniChart.innerHTML = miniHtml;
    }

    // Update port history
    for (const key of PORT_KEYS) {
        const p = state.ports[key];
        const w = (p.enabled && p.w > 0) ? p.w : 0;
        state.history[key].push(w);
        if (state.history[key].length > 30) state.history[key].shift();
    }

    const textColor = isDark ? '#999' : '#666';
    const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)';
    const chartIds = { c1: 'chartC1', c2: 'chartC2', c3: 'chartC3', a: 'chartA' };

    for (const key of PORT_KEYS) {
        const canvas = document.getElementById(chartIds[key]);
        if (!canvas) continue;
        if (portCharts[key]) portCharts[key].destroy();
        const labels = state.history[key].map((_,i) => i);
        const data = state.history[key];
        const maxVal = Math.max(10, ...data, 1);
        portCharts[key] = new Chart(canvas, {
            type: 'line',
            data: {
                labels,
                datasets: [{ data, borderColor: PORT_COLORS[key], borderWidth: 1.5, tension: 0.4, pointRadius: 0, fill: false }]
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
                plugins: { legend: { display: false } },
                scales: {
                    x: { display: false },
                    y: { display: false, min: 0, max: Math.ceil(maxVal * 1.1) }
                }
            }
        });
    }
}

function renderSettingsUI() {
    const st = document.getElementById('screenTimeVal');
    if (st) st.innerHTML = SCREEN_TIMES[state.screenTime] + ' <img src="static/plugin_imgs/main_charger_dark_icon_more.png" alt="">';
    const tt = document.getElementById('toggleTrickle');
    if (tt) tt.checked = state.trickleEnabled;
}

function renderPowerDist() {
    const bar = document.getElementById('powerDist');
    const text = document.getElementById('powerDistText');
    if (!bar || !text) return;
    const MAX_W = 120;
    let powers = [];
    let totalActive = 0;
    for (const key of PORT_KEYS) {
        const p = state.ports[key];
        const w = (state.ports[key].enabled && state.ports[key].v > 0) ? p.w : 0;
        totalActive += w;
        powers.push({ key, name: PORT_NAMES[key], w, color: PORT_COLORS[key] });
    }
    const idleW = Math.max(0, MAX_W - totalActive);
    powers.push({ key:'idle', name:'空闲', w: idleW, color: 'rgba(255,255,255,0.06)' });
    bar.innerHTML = powers.map(x => `<div style="width:${(x.w/MAX_W*100).toFixed(1)}%;height:100%;background:${x.color};transition:width 0.5s;"></div>`).join('');
    text.innerHTML = powers.map(x => {
        const pct = (x.w / MAX_W * 100).toFixed(0);
        return `<span style="color:${x.color};${x.w > 0 ? '' : 'opacity:0.3;'}">${x.name} ${pct}%</span>`;
    }).join('');
}

// ── Delay Off ──
const delayMinutes = { c1: 0, c2: 0, c3: 0, a: 0 };
const DELAY_PIIDS = { c1: 9, c2: 10, c3: 11, a: 12 };
function renderDelayOff() {
    const grid = document.getElementById('delayOffGrid');
    if (!grid) return;
    // Only show active ports (w > 0)
    const activeKeys = PORT_KEYS.filter(key => state.ports[key].v > 0);
    if (activeKeys.length === 0) { grid.innerHTML = '<div style="font-size:13px;color:var(--text-dim);text-align:center;padding:12px;">暂无活跃端口</div>'; return; }
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
                <span id="delayVal_${key}" style="font-size:14px;font-weight:600;color:${min>0?dotColor:'var(--text-dim)'};">${min > 0 ? min + '分钟' : '未设置'}</span>
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
                    valEl.textContent = v > 0 ? v + '分钟' : '未设置';
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
    state.screenTime = (state.screenTime + 1) % SCREEN_TIMES.length;
    document.getElementById('screenTimeVal').innerHTML =
        SCREEN_TIMES[state.screenTime] + ' <img src="static/plugin_imgs/main_charger_dark_icon_more.png" alt="">';
    markLocalChange();
    try { await fetch(`${API_BASE}/api/set`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ piid: 6, value: state.screenTime }) }); } catch(e) {}
}

// ── Navbar scroll hide ──
const navbar = document.querySelector('.navbar');
function hideNavOnScroll(scrollY) {
    if (navbar) navbar.classList.toggle('hide', scrollY > 60);
}
const phone = document.querySelector('.phone');
if (phone) phone.addEventListener('scroll', () => hideNavOnScroll(phone.scrollTop));
window.addEventListener('scroll', () => hideNavOnScroll(window.scrollY));

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

// ── Init ──
renderAll();
fetchStatus();
setInterval(fetchStatus, 2000);
