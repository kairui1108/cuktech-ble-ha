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
    if (d >= yesterday) return I18N.t('charge.yesterdayTime', { time: `${h}:${m}` });
    return `${d.getMonth()+1}/${d.getDate()} ${h}:${m}`;
}

function fmtDuration(sec) {
    if (!sec) return '--';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h > 0 ? `${h}h${m}m` : `${m}min`;
}

// 会话行 / 详情里的文本都来自 DB 与设备上报，进 innerHTML 与属性前一律转义。
// （onclick 的函数名另有白名单校验，两者不是同一件事。）
function esc(v) {
    return String(v === undefined || v === null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
// 长期供电（常供）会话的详情：
//   _dsPermanent 当前详情是不是常供会话（服务端返回为准）
// 常供曲线只在内存里保留 1 小时，且界面**不再提供窗口长度/左右滑动**——
// 永远取"服务端能给的整段"（缺省窗口=内存保留时长）并贴最新，
// 所以这里不再有窗口宽度/右端状态。
let _dsPermanent = false;
let _dsReqToken = 0; // 详情请求序号：丢弃乱序返回的旧响应

function setDownsample(target) {
    _dsTarget = parseInt(target) || 0;
    if (_currentSessionId) showSessionDetail(_currentSessionId);
}

// 常供会话的曲线只在内存里留 1 小时，且**没有窗口长度/滑动控件**：
// 永远取服务端能给的整段并贴最新（见 fetchSessionPoints）。所以这里只剩一件事要
// 说明——"这次会话没有采样曲线"（只存了统计）。那张图区高度就是弹窗里最大的
// 一块空白，没有曲线时应当把它整个收起来，只留这行说明。
function ensureNoCurveNote(show) {
    const detail = document.getElementById('sessionDetail');
    if (!detail) return null;
    let note = document.getElementById('sdNoCurveNote');
    if (!note) {
        note = document.createElement('div');
        note.id = 'sdNoCurveNote';
        note.className = 'sd-no-curve';
        // 插到"标题行所在的那个容器"里、紧跟标题之后（图表区之前）。
        // 不能按 #sessionDetail 的子节点算：桌面标题行在 .sd-sheet 内部，
        // 插到 detail 下会落到弹窗外边，文字永远看不见。
        const head = detail.querySelector('.sd-head') || detail.firstElementChild;
        const host = (head && head.parentNode) || detail;
        host.insertBefore(note, head ? head.nextSibling : host.firstChild);
    }
    if (note.dataset.key !== 'noCurve') {
        note.dataset.key = 'noCurve';
        note.textContent = I18N.t('charge.noCurve');
    }
    note.style.display = show ? '' : 'none';
    return note;
}

// 详情里"只对曲线有意义"的控件（精度下拉 / 导出 CSV）：没有采样点时必须收起。
//   · 常供会话的曲线只留在内存（不落库），导出端点读的是库 → 导出的是一张空表；
//     精度（下采样）也只作用在曲线上，没有曲线时它调不动任何东西。
//   · 没有任何采样点的会话（0Wh 已被清理）同理。
// 两页都靠标记类挂钩子（.sd-curve-only）：桌面是"精度"整块 + 导出按钮，
// 手机只有"精度"那一行（该页详情本来就没有导出按钮）。
function setCurveControlsVisible(visible) {
    document.querySelectorAll('.sd-curve-only').forEach(function (el) {
        if (el.style) el.style.display = visible ? '' : 'none';
    });
}

// 没有曲线时把图表区整个收起来（它的定高就是那块大空白）；有曲线时恢复。
// 桌面是 .sd-chart 容器、手机是内联定高的 canvas 父节点，都从 canvas 往上找。
function setSessionChartVisible(visible) {
    const chart = document.getElementById('sessionChart');
    if (!chart) return;
    const box = chart.parentNode;
    if (box && box.style) box.style.display = visible ? '' : 'none';
}

function showSessionDetailEmpty(permanent, stats, noCurve) {
    const detail = document.getElementById('sessionDetail');
    if (!detail) return;
    detail.style.display = 'block';
    const el = (id) => document.getElementById(id);
    const startTs = stats && stats.start_time ? stats.start_time : null;
    const endTs = stats && stats.end_time ? stats.end_time : null;
    if (el('sdTitle')) {
        if (permanent) {
            el('sdTitle').textContent =
                `${I18N.t('charge.permanentWindow')}${startTs ? ' · ' + fmtTime(startTs) : ''}`;
        } else if (startTs) {
            el('sdTitle').textContent = `${fmtTime(startTs)}${endTs ? ' → ' + fmtTime(endTs) : ''}`;
        } else {
            el('sdTitle').textContent = '--';
        }
    }
    if (el('sdDuration')) el('sdDuration').textContent = stats ? fmtDuration(stats.duration_sec) : '--';
    // 指标要么全填、要么全清：只填一半的话，上一个会话的 Wh/W/V/A 会挂在新标题下面
    const metricIds = ['sdEnergy', 'sdAvgP', 'sdPeakP', 'sdAvgV', 'sdAvgI'];
    if (stats) {
        if (el('sdEnergy')) el('sdEnergy').textContent = (stats.total_wh || 0).toFixed(1);
        if (el('sdAvgP')) el('sdAvgP').textContent = (stats.avg_power_w || 0).toFixed(1);
        if (el('sdPeakP')) el('sdPeakP').textContent = (stats.peak_power_w || 0).toFixed(1);
        if (el('sdAvgV')) el('sdAvgV').textContent = (stats.avg_voltage || 0).toFixed(1);
        if (el('sdAvgI')) el('sdAvgI').textContent = (stats.avg_current || 0).toFixed(2);
    } else {
        metricIds.forEach((id) => { if (el(id)) el(id).textContent = '--'; });
    }
    if (_sessionChart) { _sessionChart.destroy(); _sessionChart = null; }
    setSessionChartVisible(false);      // 没有曲线：收起定高的图表区（最大一块空白）
    ensureNoCurveNote(!!noCurve);
    setCurveControlsVisible(false);     // 没有曲线：精度下拉与导出按钮一并收起
}

// Fetch session detail points
async function fetchSessionPoints(sessionId) {
    try {
        const q = [];
        if (_dsTarget > 0) q.push(`downsample=${_dsTarget}`);
        // 常供会话不再传 window/to：服务端缺省就是"内存保留时长（1 小时）+ 贴最新"，
        // 界面也没有窗口控件可以改变它。
        const res = await fetch(`${API}/api/sessions/${sessionId}/points${q.length ? '?' + q.join('&') : ''}`);
        // fetch 不对 4xx/5xx reject：不检查 status 的话，服务端 400（例如 to 越界）
        // 的错误体会被当成正常响应解析，面板静默变空白且没有任何提示。
        // 返回 null 让调用方区分"加载失败"与"这次会话真的没有点"。
        if (!res.ok) return null;
        return await res.json();
    } catch (e) { return null; }
}

// Render stats summary
function renderStats(containerId, stats) {
    const el = document.getElementById(containerId);
    if (!el) return;
    const s = `color:var(--text)`;
    const l = `color:var(--text-dim)`;
    el.innerHTML = `
        <div class="mini-stat"><div class="mini-stat-value" style="${s}">${stats.total_wh ? stats.total_wh.toFixed(1) : '0'}</div><div class="mini-stat-label" style="${l}">${I18N.t('charge.totalWh')}</div></div>
        <div class="mini-stat"><div class="mini-stat-value" style="${s}">${stats.session_count || 0}</div><div class="mini-stat-label" style="${l}">${I18N.t('charge.sessionCount')}</div></div>
        <div class="mini-stat"><div class="mini-stat-value" style="${s}">${stats.avg_power_w ? stats.avg_power_w.toFixed(1) : '0'}</div><div class="mini-stat-label" style="${l}">${I18N.t('charge.avgPower')}</div></div>
        <div class="mini-stat"><div class="mini-stat-value" style="${s}">${stats.peak_power_w ? stats.peak_power_w.toFixed(1) : '0'}</div><div class="mini-stat-label" style="${l}">${I18N.t('charge.peakPower')}</div></div>`;
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
        el.innerHTML = `<div class="session-empty" style="text-align:center;color:var(--text-dim);padding:16px;font-size:13px;">${I18N.t('charge.noRecords')}</div>`;
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
        const protoHtml = proto ? `<span style="font-size:11px;color:var(--accent-ink);margin-left:6px;">${esc(proto)}</span>` : '';
        const isActive = s.is_active;
        // 长期供电状态只在"充电量限额"卡里体现，记录行不加徽标（行本身仍可点开
        // 窗口详情：permanent 标记通过 data-* 传给详情浮层，不参与视觉渲染）。
        const isPerm = !!s.permanent;
        const wh = (s.total_wh && s.total_wh > 0) ? s.total_wh.toFixed(1) : '0';
        const activeDot = isActive ? `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--dot-on,#34C759);margin-right:4px;animation:pulse 1.5s infinite;"></span>` : '';
        // 行原来是 div[onclick]，键盘/读屏用户打不开详情：补 role/tabindex/aria-label + 回车与空格
        const rowLabel = `${portNames[s.port] || s.port} ${fmtTime(s.start_time)} ${I18N.t('charge.energy', { wh })}`;
        // 只有可信的标识符才能拼进 onclick 字符串：那是"可执行代码"不是属性值，
        // 转义引号挡不住注入。非标识符（例如带参数的调用）一律退回默认处理函数。
        const open = /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(onClick || '') ? onClick : 'showSessionDetail';
        return `
        <div class="session-item" data-id="${s.id}" data-permanent="${isPerm ? 1 : 0}" onclick="(${open})(this.dataset.id, this.dataset.permanent === '1')"
             role="button" tabindex="0" aria-label="${esc(rowLabel)}"
             onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();(${open})(this.dataset.id, this.dataset.permanent === '1');}"
             style="display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid rgba(128,128,128,0.1);cursor:pointer;">
            <div style="display:flex;align-items:center;gap:8px;flex:1;min-width:0;">
                ${activeDot}
                <div style="width:8px;height:8px;border-radius:50%;background:${portColors[s.port] || '#888'};flex-shrink:0;"></div>
                <span style="font-size:13px;color:var(--text);flex-shrink:0;">${esc(portNames[s.port] || s.port)}</span>
                <span style="font-size:12px;color:var(--text-dim);flex-shrink:0;">${fmtTime(s.start_time)}${!isActive && s.end_time ? ' ~ ' + fmtTime(s.end_time) : ''}</span>
                ${protoHtml}
            </div>
            <span style="font-size:13px;font-weight:600;color:${isActive ? 'var(--success,#34C759)' : 'var(--text)'};flex-shrink:0;margin-left:8px;">${I18N.t('charge.energy', { wh: wh })}</span>
        </div>`;
    }).join('');
}

// Show session detail (chart + stats)
async function showSessionDetail(sessionId, permanent) {
    // 面板被关掉再打开同一条会话，用户眼里是"重新打开"，窗口要回到最新；
    // 只按 id 判新会让上次滑到的位置残留下来（closeSessionDetail 没清状态）。
    const detailEl = document.getElementById('sessionDetail');
    const reopened = !detailEl || detailEl.style.display === 'none';
    const isNew = reopened || String(sessionId) !== String(_currentSessionId);
    _currentSessionId = sessionId;
    if (isNew) {
        _dsPermanent = !!permanent;
    } else if (permanent !== undefined) {
        _dsPermanent = !!permanent;
    }
    // 详情里还有精度下拉等控件，点得比往返快时响应可能乱序回来：丢弃不是最后一次
    // 请求的响应，否则统计/曲线会被旧载荷覆盖。
    const reqToken = ++_dsReqToken;
    const data = await fetchSessionPoints(sessionId);
    if (reqToken !== _dsReqToken) return;
    if (!data) return;      // 请求失败：保留上一次的内容，别把面板清空
    // 服务端是常供与否的唯一权威（列表里的标记可能已过时），且必须能双向翻转：
    // 端口中途退出常供后，服务端走 DB 回落分支会给出 permanent=false。
    if ('permanent' in data) _dsPermanent = !!data.permanent;
    const stats = data.stats || null;
    const points = data.points || [];
    if (points.length === 0) {
        // 常供会话不落曲线点：会话结束后只剩统计（活跃但刚起步时也走这里）
        if (stats && (_dsPermanent || stats.start_time)) {
            showSessionDetailEmpty(_dsPermanent, stats, !!data.no_curve);
        }
        return;
    }

    setSessionChartVisible(true);       // 有曲线：恢复图表区并收起"没有曲线"说明
    ensureNoCurveNote(false);
    // 常供会话有曲线（内存窗口）但库里没有点：导出仍是空表，精度也只对库里的点
    // 有意义 → 一并收起
    setCurveControlsVisible(!_dsPermanent);

    const totalWh = points.reduce((sum, p, i) => {
        if (i === 0) return 0;
        const dt = (p.timestamp - points[i-1].timestamp) / 3600;
        return sum + ((points[i-1].power + p.power) / 2) * dt;
    }, 0);
    const spanSec = points[points.length-1].timestamp - points[0].timestamp;
    let peakPower = 0;
    for (let i = 0; i < points.length; i++) {
        if (points[i].power > peakPower) peakPower = points[i].power;
    }
    const avgVoltage = points.reduce((s, p) => s + p.voltage, 0) / points.length;
    const avgCurrent = points.reduce((s, p) => s + p.current, 0) / points.length;

    // 统计口径：常供会话恒用服务端给的**整个会话**统计（窗口里只有最近一段，
    // 按窗口现算会偏小）；普通会话保持原样（活跃会话 DB 行还没写总量，用点现算）。
    const base = _dsPermanent && stats ? stats : null;
    const duration = base ? (base.duration_sec || 0) : spanSec;
    const energy = base ? (base.total_wh || 0) : totalWh;
    const avgPower = base ? (base.avg_power_w || 0)
        : (spanSec > 0 ? (totalWh / (spanSec / 3600)) : 0);
    const peak = base ? (base.peak_power_w || 0) : peakPower;
    const avgV = base ? (base.avg_voltage || 0) : avgVoltage;
    const avgI = base ? (base.avg_current || 0) : avgCurrent;

    // Show detail panel
    const detail = document.getElementById('sessionDetail');
    if (detail) {
        detail.style.display = 'block';
        const el = (id) => document.getElementById(id);
        const title = _dsPermanent
            ? I18N.t('charge.permanentWindow')
            : `${fmtTime(points[0].timestamp)} → ${fmtTime(points[points.length-1].timestamp)}`;
        if (el('sdTitle')) el('sdTitle').textContent = title;
        if (el('sdDuration')) el('sdDuration').textContent = fmtDuration(duration);
        if (el('sdEnergy')) el('sdEnergy').textContent = energy.toFixed(1);
        if (el('sdAvgP')) el('sdAvgP').textContent = avgPower.toFixed(1);
        if (el('sdPeakP')) el('sdPeakP').textContent = peak.toFixed(1);
        if (el('sdAvgV')) el('sdAvgV').textContent = avgV.toFixed(1);
        if (el('sdAvgI')) el('sdAvgI').textContent = avgI.toFixed(2);

        // Render chart
        renderSessionChart(points);
    }
}

function renderSessionChart(points) {
    const canvas = document.getElementById('sessionChart');
    if (!canvas) return;
    // X 轴刻度：把"索引 → 时间"的换算交给 ticks.callback，而不是预先把标签写进
    // labels 数组。原因：Chart.js 自己决定抽哪几个索引当刻度（autoSkip + maxTicksLimit），
    // 预先按索引打点只有恰好命中它的选择才显示——原来"每 60 点标一个"，300 点档下
    // 只剩 19:37 一个刻度，等于没有横轴。
    const labels = points.map(() => '');
    const tickLabel = (value) => {
        const p = points[value];
        return p ? fmtTime(p.timestamp) : '';
    };
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
                            return I18N.t('charge.powerTooltip', { power: ctx.parsed.y.toFixed(1) });
                        },
                        afterLabel: function(ctx) {
                            const p = points[ctx.dataIndex];
                            return p && p.protocol ? I18N.t('charge.protocolTooltip', { protocol: p.protocol }) : '';
                        }
                    }
                }
            },
            scales: {
                x: { display: true, ticks: { maxTicksLimit: 6, font: { size: 10 }, color: 'rgba(128,128,128,0.5)', callback: tickLabel }, grid: { display: false } },
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
                    // 主题判定要同时认两套信号：桌面页（index）用 html[data-appearance]，
                    // 手机页（phone.js）用 body.light。原来只看 body.light，桌面页恒为
                    // isDark=true，于是浅色主题下白线白字画在白底浮层上、协议标注整条看不见。
                    const isDark = document.documentElement.getAttribute('data-appearance') !== 'light'
                        && !document.body.classList.contains('light');
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
// 每页条数：默认 2（手机端）。桌面端在 startChargeHistoryAutoRefresh 的第 5 个参数里
// 传更大的值（index 页传 4：卡片高度要和右栏配平，行数多了就得压缩别的卡）。
//
// 注意这个数字是"列表里总共显示多少行"：正在充电的会话会插队排在最前面
// （is_active=true），行数不固定——活跃会话一多仍按固定条数取数，卡片就会变高。
// 所以实际请求条数 = _chPageSize - 活跃会话数，由 refreshChargeHistory() 每轮计算。
let _chPageSize = 2;
let _chActiveCount = 0;   // 上一次响应里的活跃会话数（0 = 还没拉过数据）
let _chSignature = '';    // 上一轮渲染的数据指纹：没变就不重写 DOM（否则 hover/焦点会被打断）
let _chStatsSig = '';     // 统计块同理

// 指纹前缀：周期 / 页码 / 每页条数 / 语言——任何一项变了都必须重画，
// 哪怕数据一模一样（切语言时中文数据指纹不变，但文案要换）。
function _chSignaturePrefix() {
    const loc = (typeof I18N !== 'undefined' && I18N.getLocale) ? I18N.getLocale() : '';
    return `${window._chPeriod}|${_chPage}|${_chPageSize}|${loc}`;
}

function _chFetchLimit() {
    // 至少取 1 条：活跃会话为 0 时就是完整的 _chPageSize
    return Math.max(1, _chPageSize - _chActiveCount);
}

function closeSessionDetail() {
    const detail = document.getElementById('sessionDetail');
    if (detail) detail.style.display = 'none';
}

function chGoPage(page) {
    _chPage = page;
    refreshChargeHistory();
}

// Auto-refresh sessions
function startChargeHistoryAutoRefresh(containerId, statsId, period, interval, pageSize) {
    window._chContainerId = containerId;
    window._chStatsId = statsId;
    if (pageSize > 0) _chPageSize = pageSize;
    window._chPeriod = period;
    refreshChargeHistory();
    setInterval(refreshChargeHistory, interval || 30000);
    // SSE: refresh immediately on session completion
    // Listen for CustomEvent dispatched by main SSE handler (avoids second SSE connection)
    if (!window._chSseListening) {
        window._chSseListening = true;
        window.addEventListener('sse-session-end', () => refreshChargeHistory());
    }
}

function refreshChargeHistory(force) {
    const containerId = window._chContainerId;
    const statsId = window._chStatsId;
    const period = window._chPeriod;
    // 实际请求条数 = 目标总行数 - 活跃会话数（活跃会话会插队排在最前面）
    const limit = _chFetchLimit();
    fetchEnergyStats(period).then(stats => {
        // 统计块同理：数字没变就不重画
        const sig = _chSignaturePrefix() + '|stats|' + JSON.stringify(stats);
        if (force || sig !== _chStatsSig) {
            _chStatsSig = sig;
            renderStats(statsId, stats);
        }
    });
    fetchSessions(null, period, limit, _chPage).then(data => {
        // 活跃会话数与上一轮不同（开始充电/充满/换口）：用修正后的条数再取一次，
        // 保证"列表总行数 = _chPageSize"恒成立，卡片高度因此不会跳
        const activeCount = (data.sessions || []).filter(x => x.is_active).length;
        if (activeCount !== _chActiveCount) {
            _chActiveCount = activeCount;
            refreshChargeHistory(force);
            return;
        }
        // 最后一页无数据时自动回退上一页
        if (data.sessions && data.sessions.length === 0 && data.page > 1) {
            _chPage = data.page - 1;
            fetchSessions(null, period, _chFetchLimit(), _chPage).then(data2 => {
                _chSignature = '';
                renderSessionList(containerId, data2.sessions);
                renderPagination(containerId, data2);
            });
            return;
        }
        // 行内容没变就不重建 DOM：轮询只负责"发现变化"，不负责"每轮重画"。
        // 否则每轮 innerHTML 都会打断 hover / 键盘焦点，活跃会话的呼吸点动画也会重启。
        const sig = _chSignaturePrefix() + '|' + JSON.stringify(data.sessions || []);
        if (!force && sig === _chSignature) return;
        _chSignature = sig;
        renderSessionList(containerId, data.sessions);
        renderPagination(containerId, data);
    });
}

function renderPagination(containerId, data) {
    const el = document.getElementById(containerId);
    if (!el || !data.pages || data.pages <= 1) return;
    const pag = document.createElement('div');
    pag.style.cssText = 'display:flex;justify-content:center;align-items:center;gap:8px;padding:10px 0;font-size:12px;';
    const btnStyle = 'padding:4px 10px;border-radius:6px;border:1px solid var(--card-border);background:var(--card-bg);color:var(--text-dim);font-size:11px;';
    const disStyle = btnStyle + 'opacity:0.4;cursor:not-allowed;';
    pag.innerHTML = `
        <button onclick="chGoPage(${Math.max(1, data.page - 1)})" ${data.page <= 1 ? 'disabled' : ''}
            style="${data.page <= 1 ? disStyle : btnStyle}">${I18N.t('charge.prevPage')}</button>
        <span style="color:var(--text-dim);">${data.page} / ${data.pages}</span>
        <button onclick="chGoPage(${Math.min(data.pages, data.page + 1)})" ${data.page >= data.pages - 1 ? 'disabled' : ''}
            style="${data.page >= data.pages - 1 ? disStyle : btnStyle}">${I18N.t('charge.nextPage')}</button>`;
    el.appendChild(pag);
}

// ── Locale change: refresh dynamic charge-history content ──
if (typeof I18N !== 'undefined' && typeof I18N.onChange === 'function') {
    I18N.onChange(function () {
        if (window._chContainerId) {
            refreshChargeHistory();
            if (_currentSessionId) showSessionDetail(_currentSessionId);
        }
    });
}
