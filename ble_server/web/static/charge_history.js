// ── Charge History Module (shared between phone.html and index.html) ──
const API = window.location.origin;
let _sessionChart = null;

// Format timestamp to local time string
function fmtTime(ts) {
    if (!ts) return '--';
    const d = new Date(ts * 1000);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const isToday = d >= today;
    const h = String(d.getHours()).padStart(2, '0');
    const m = String(d.getMinutes()).padStart(2, '0');
    if (isToday) return `${h}:${m}`;
    const yesterday = new Date(today - 86400000);
    if (d >= yesterday) return `昨天 ${h}:${m}`;
    return `${d.getMonth()+1}/${d.getDate()} ${h}:${m}`;
}

function fmtDuration(sec) {
    if (!sec) return '--';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h > 0 ? `${h}h${m}m` : `${m}min`;
}

// Fetch energy stats
async function fetchEnergyStats(period) {
    try {
        const res = await fetch(`${API}/api/energy/stats?period=${period || 'today'}`);
        return await res.json();
    } catch (e) { return { total_wh: 0, session_count: 0, avg_power_w: 0 }; }
}

// Fetch sessions list
async function fetchSessions(port, period, limit, page) {
    try {
        let url = `${API}/api/sessions?period=${period || 'today'}&limit=${limit || 10}&page=${page || 1}`;
        if (port) url += `&port=${port}`;
        const res = await fetch(url);
        return await res.json();
    } catch (e) { return { sessions: [], total: 0, page: 1, pages: 1 }; }
}

let _dsTarget = 300;
let _currentSessionId = null;

function setDownsample(target) {
    _dsTarget = parseInt(target) || 0;
    if (_currentSessionId) showSessionDetail(_currentSessionId);
}

// Fetch session detail points
async function fetchSessionPoints(sessionId) {
    try {
        const ds = _dsTarget > 0 ? `?downsample=${_dsTarget}` : '';
        const res = await fetch(`${API}/api/sessions/${sessionId}/points${ds}`);
        return await res.json();
    } catch (e) { return { points: [] }; }
}

// Render stats summary
function renderStats(containerId, stats) {
    const el = document.getElementById(containerId);
    if (!el) return;
    const s = `color:var(--text)`;
    const l = `color:var(--text-dim)`;
    el.innerHTML = `
        <div class="mini-stat"><div class="mini-stat-value" style="${s}">${stats.total_wh ? stats.total_wh.toFixed(1) : '0'}</div><div class="mini-stat-label" style="${l}">总充电 Wh</div></div>
        <div class="mini-stat"><div class="mini-stat-value" style="${s}">${stats.session_count || 0}</div><div class="mini-stat-label" style="${l}">充电次数</div></div>
        <div class="mini-stat"><div class="mini-stat-value" style="${s}">${stats.avg_power_w ? stats.avg_power_w.toFixed(1) : '0'}</div><div class="mini-stat-label" style="${l}">平均功率 W</div></div>
        <div class="mini-stat"><div class="mini-stat-value" style="${s}">${stats.peak_power_w ? stats.peak_power_w.toFixed(1) : '0'}</div><div class="mini-stat-label" style="${l}">峰值功率 W</div></div>`;
}

// Render session list
function renderSessionList(containerId, sessions, onClick) {
    const el = document.getElementById(containerId);
    if (!el) return;
    // Filter out orphaned sessions: no end_time and not currently active,
    // or ended with 0Wh (data was lost before protocol column was added)
    const filtered = (sessions || []).filter(s => {
        if (s.is_active) return true;
        if (!s.end_time) return false;
        if (!s.total_wh || s.total_wh <= 0) return false;
        return true;
    });
    if (filtered.length === 0) {
        el.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:16px;font-size:13px;">暂无充电记录</div>';
        return;
    }
    const portNames = {1:'C1', 2:'C2', 3:'C3', 4:'A'};
    // Use page-specific port colors: phone.html has PORT_COLORS, index.html has CSS vars
    let portColors;
    if (typeof PORT_COLORS !== 'undefined') {
        portColors = {1: PORT_COLORS.c1, 2: PORT_COLORS.c2, 3: PORT_COLORS.c3, 4: PORT_COLORS.a};
    } else {
        const cs = getComputedStyle(document.documentElement);
        portColors = {
            1: cs.getPropertyValue('--port-c1').trim() || '#03a9f4',
            2: cs.getPropertyValue('--port-c2').trim() || '#7c4dff',
            3: cs.getPropertyValue('--port-c3').trim() || '#389e3d',
            4: cs.getPropertyValue('--port-a').trim() || '#ffa42b',
        };
    }
    el.innerHTML = filtered.map(s => {
        const proto = s.protocol || '';
        const protoHtml = proto ? `<span style="font-size:11px;color:var(--accent);margin-left:6px;">${proto}</span>` : '';
        const isActive = s.is_active;
        const wh = (s.total_wh && s.total_wh > 0) ? s.total_wh.toFixed(1) : '0';
        const activeDot = isActive ? `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#34C759;margin-right:4px;animation:pulse 1.5s infinite;"></span>` : '';
        return `
        <div class="session-item" data-id="${s.id}" onclick="(${onClick || 'showSessionDetail'})(this.dataset.id)"
             style="display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid rgba(128,128,128,0.1);cursor:pointer;">
            <div style="display:flex;align-items:center;gap:8px;flex:1;min-width:0;">
                ${activeDot}
                <div style="width:8px;height:8px;border-radius:50%;background:${portColors[s.port] || '#888'};flex-shrink:0;"></div>
                <span style="font-size:13px;color:var(--text);flex-shrink:0;">${portNames[s.port] || s.port}</span>
                <span style="font-size:12px;color:var(--text-dim);flex-shrink:0;">${fmtTime(s.start_time)}${!isActive && s.end_time ? ' ~ ' + fmtTime(s.end_time) : ''}</span>
                ${protoHtml}
            </div>
            <span style="font-size:13px;font-weight:600;color:${isActive ? 'var(--success,#34C759)' : 'var(--text)'};flex-shrink:0;margin-left:8px;">电量：${wh}Wh</span>
        </div>`;
    }).join('');
}

// Show session detail (chart + stats)
async function showSessionDetail(sessionId) {
    _currentSessionId = sessionId;
    const data = await fetchSessionPoints(sessionId);
    if (!data.points || data.points.length === 0) return;

    const points = data.points;
    const totalWh = points.reduce((sum, p, i) => {
        if (i === 0) return 0;
        const dt = (p.timestamp - points[i-1].timestamp) / 3600;
        return sum + ((points[i-1].power + p.power) / 2) * dt;
    }, 0);
    const duration = points[points.length-1].timestamp - points[0].timestamp;
    const avgPower = duration > 0 ? (totalWh / (duration / 3600)) : 0;
    let peakPower = 0;
    for (let i = 0; i < points.length; i++) {
        if (points[i].power > peakPower) peakPower = points[i].power;
    }
    const avgVoltage = points.reduce((s, p) => s + p.voltage, 0) / points.length;
    const avgCurrent = points.reduce((s, p) => s + p.current, 0) / points.length;

    // Show detail panel
    const detail = document.getElementById('sessionDetail');
    if (detail) {
        detail.style.display = 'block';
        const el = (id) => document.getElementById(id);
        if (el('sdTitle')) el('sdTitle').textContent = `${fmtTime(points[0].timestamp)} → ${fmtTime(points[points.length-1].timestamp)}`;
        if (el('sdDuration')) el('sdDuration').textContent = fmtDuration(duration);
        if (el('sdEnergy')) el('sdEnergy').textContent = totalWh.toFixed(1);
        if (el('sdAvgP')) el('sdAvgP').textContent = avgPower.toFixed(1);
        if (el('sdPeakP')) el('sdPeakP').textContent = peakPower.toFixed(1);
        if (el('sdAvgV')) el('sdAvgV').textContent = avgVoltage.toFixed(1);
        if (el('sdAvgI')) el('sdAvgI').textContent = avgCurrent.toFixed(2);

        // Render chart
        renderSessionChart(points);
    }
}

function renderSessionChart(points) {
    const canvas = document.getElementById('sessionChart');
    if (!canvas) return;
    const labels = points.map((p, i) => i % 60 === 0 ? fmtTime(p.timestamp) : '');
    const powers = points.map(p => p.power);

    // Find protocol transitions for annotations
    const protoChanges = [];
    let lastProto = '';
    points.forEach((p, i) => {
        const proto = p.protocol || '';
        if (proto && proto !== lastProto) {
            protoChanges.push({ index: i, proto });
            lastProto = proto;
        }
    });

    if (_sessionChart) _sessionChart.destroy();
    _sessionChart = new Chart(canvas, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                data: powers,
                borderColor: 'rgba(3,169,244,0.8)',
                backgroundColor: 'rgba(3,169,244,0.1)',
                borderWidth: 1.5,
                fill: true,
                tension: 0.3,
                pointRadius: 0,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: function(ctx) {
                            return '功率: ' + ctx.parsed.y.toFixed(1) + 'W';
                        },
                        afterLabel: function(ctx) {
                            const p = points[ctx.dataIndex];
                            return p && p.protocol ? '协议: ' + p.protocol : '';
                        }
                    }
                }
            },
            scales: {
                x: { display: true, ticks: { maxTicksLimit: 6, font: { size: 10 }, color: 'rgba(128,128,128,0.5)' }, grid: { display: false } },
                y: { display: true, ticks: { font: { size: 10 }, color: 'rgba(128,128,128,0.5)' }, grid: { color: 'rgba(128,128,128,0.1)' } }
            },
            interaction: { intersect: false, mode: 'index' }
        },
        plugins: [{
            id: 'protocolLines',
            afterDraw(chart) {
                if (protoChanges.length === 0) return;
                const ctx = chart.ctx;
                const xScale = chart.scales.x;
                const yScale = chart.scales.y;
                protoChanges.forEach(c => {
                    if (c.index === 0) return;
                    const x = xScale.getPixelForValue(c.index);
                    const isDark = !document.body.classList.contains('light');
                    const lineColor = isDark ? 'rgba(255,255,255,0.25)' : 'rgba(0,0,0,0.2)';
                    const textColor = isDark ? 'rgba(255,255,255,0.7)' : 'rgba(0,0,0,0.6)';
                    ctx.save();
                    ctx.strokeStyle = lineColor;
                    ctx.setLineDash([4, 4]);
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    ctx.moveTo(x, chart.chartArea.top);
                    ctx.lineTo(x, chart.chartArea.bottom);
                    ctx.stroke();
                    ctx.fillStyle = textColor;
                    ctx.font = '10px sans-serif';
                    ctx.fillText(c.proto, x + 3, chart.chartArea.top + 12);
                    ctx.restore();
                });
            }
        }]
    });
}

// Pagination state
let _chPage = 1;
const _chPageSize = 2;

function closeSessionDetail() {
    const detail = document.getElementById('sessionDetail');
    if (detail) detail.style.display = 'none';
}

function chGoPage(page) {
    _chPage = page;
    refreshChargeHistory();
}

// Auto-refresh sessions
function startChargeHistoryAutoRefresh(containerId, statsId, period, interval) {
    window._chContainerId = containerId;
    window._chStatsId = statsId;
    window._chPeriod = period;
    refreshChargeHistory();
    setInterval(refreshChargeHistory, interval || 30000);
}

function refreshChargeHistory() {
    const containerId = window._chContainerId;
    const statsId = window._chStatsId;
    const period = window._chPeriod;
    fetchEnergyStats(period).then(stats => renderStats(statsId, stats));
    fetchSessions(null, period, _chPageSize, _chPage).then(data => {
        renderSessionList(containerId, data.sessions);
        renderPagination(containerId, data);
    });
}

function renderPagination(containerId, data) {
    const el = document.getElementById(containerId);
    if (!el || !data.pages || data.pages <= 1) return;
    const pag = document.createElement('div');
    pag.style.cssText = 'display:flex;justify-content:center;align-items:center;gap:8px;padding:10px 0;font-size:12px;';
    pag.innerHTML = `
        <button onclick="chGoPage(${Math.max(1, data.page - 1)})" ${data.page <= 1 ? 'disabled' : ''}
            style="padding:4px 10px;border-radius:6px;border:1px solid var(--card-border);background:var(--card-bg);color:var(--text-dim);cursor:pointer;font-size:11px;">上一页</button>
        <span style="color:var(--text-dim);">${data.page} / ${data.pages}</span>
        <button onclick="chGoPage(${Math.min(data.pages, data.page + 1)})" ${data.page >= data.pages ? 'disabled' : ''}
            style="padding:4px 10px;border-radius:6px;border:1px solid var(--card-border);background:var(--card-bg);color:var(--text-dim);cursor:pointer;font-size:11px;">下一页</button>`;
    el.appendChild(pag);
}
