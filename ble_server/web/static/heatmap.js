/*!
 * heatmap.js — 用电统计卡片的「热力图」tab：GitHub 贡献图风格的年度充电热力图。
 *
 * 约束与取舍：
 *  - 零构建、手写 CSS、跟随主题变量：不引入 Chart.js / React / Tailwind。
 *  - 只依赖 DeviceStore（自动带 ?device=）与 I18N；不依赖 Chart.js。
 *  - **不自己建定时器、不自己订阅 SSE / device-changed**：取数只由
 *    app.js 的 refreshEnergyCard() 触发；设备切换由 resetEnergyCard() 调 reset()。
 *  - 多次触发靠 _seq + 单飞去重：切 tab / 切设备 / 60s 轮询同时发生时只发一路请求。
 *  - **格子尺寸跟着卡片走**：量出面板可用宽高算出 --hm-cell/--hm-gap/--hm-r，
 *    由 CSS 铺满整行（原来写死 10px，右侧会空出 80px 死区）。宽度封顶时按
 *    7 行的高度预算回退；窄屏先压缝宽、再缩格子——**卡内永远不出现滚动条**。
 *  - 悬停气泡自绘：原生 title 有 ~1s 延迟、样式不可控、还会和自绘气泡同时出现。
 *    没有记录的格子也给气泡（"无充电记录" / "无统计数据"），否则一年里绝大多数
 *    格子悬停都是哑的。
 *
 * 纯函数（周对齐 / 分级 / 指纹 / 尺寸）导出在 window.HeatmapPure 上，供
 * tests/js/heatmap_test.js 直接断言。
 */
(function (global) {
    'use strict';

    var DAYS = 365;
    // 左边只标 周一 / 周三 / 周五（与 GitHub 一致），全部 7 行会让留白过宽。
    var WEEKDAY_ROWS = [0, 2, 4];

    // ── 尺寸预算 ────────────────────────────────────────────────
    // 星期标签列 20px + 主区 gap 4px：算格子宽度时要先扣掉
    var GUTTER_W = 24;
    var MIN_CELL = 8;        // 小于这个就先把缝压窄，而不是让格子继续缩
    var FLOOR_CELL = 3.5;    // 绝对下限：再小就不是矩阵了
    var TINY_CELL = 7;       // 小于这个就藏掉星期标签列（字比格子高，会叠在一起）
    var MAX_CELL = 13;       // 视觉上限（超宽屏再大就成色块了）
    var ROW_GAP = 4;         // 面板里 summary / months / legend 之间的间距
    var LABEL_W = 26;        // 月份标签实际占位（"11月" ≈ 21px + 一点呼吸）

    var state = {
        data: null,
        seq: 0,
        inflight: null,
        inflightSeq: -1,
        fp: '',
        view: null,     // date → cell（悬停气泡查表用）
        cells: null,    // 布局数组（键盘导航按索引走）
        cols: 53,
        cursorIdx: -1,
        metrics: null,
        animated: false,
        tip: null
    };

    // ── 日期与数字 ──────────────────────────────────────────────

    function pad2(n) { return (n < 10 ? '0' : '') + n; }

    function midnight(d) {
        return new Date(d.getFullYear(), d.getMonth(), d.getDate());
    }

    function addDays(d, n) {
        var x = midnight(d);
        x.setDate(x.getDate() + n);
        return x;
    }

    function dayKey(d) {
        return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
    }

    /** 'YYYY-MM-DD' → 本地 Date（无效输入返回 null）。 */
    function parseDay(s) {
        if (typeof s !== 'string') return null;
        var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
        if (!m) return null;
        var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
        return isNaN(d.getTime()) ? null : d;
    }

    // 周一起始：Mon=0 … Sun=6（JS 的 getDay() 是 Sun=0）。
    function mondayIndex(d) { return (d.getDay() + 6) % 7; }

    function formatWh(wh) {
        var v = Number(wh) || 0;
        if (v >= 1000) return (v / 1000).toFixed(2) + ' kWh';
        return v.toFixed(1) + ' Wh';
    }

    /** 分级：0 = 无电量，1..4 = 阈值以上。与后端 `_compute_heat_levels` 配套。 */
    function classify(wh, levels) {
        if (!(wh > 0)) return 0;
        var n = 0;
        var list = levels || [];
        for (var i = 0; i < list.length; i++) {
            if (wh > list[i]) n++;
        }
        return Math.min(4, 1 + n);
    }

    // ── 布局（纯函数） ──────────────────────────────────────────

    /**
     * 输入 {days:[{date,wh,sessions,peak_w}], data_since, levels}，
     * 输出列优先的格子数组（cell 0..6 = 第一周 周一..周日）。
     */
    function buildLayout(data, endDate) {
        var end = midnight(endDate || new Date());
        var start = addDays(end, -(DAYS - 1));
        var gridStart = addDays(start, -mondayIndex(start));
        // 最后一周补齐到周日；这样列数正好是整数周（53 或 54）。
        var gridEnd = addDays(addDays(end, -mondayIndex(end)), 6);
        var cols = Math.round(((gridEnd - gridStart) / 86400000 + 1) / 7);

        var byDate = {};
        var days = (data && data.days) || [];
        for (var i = 0; i < days.length; i++) byDate[days[i].date] = days[i];

        var dataSince = (data && data.data_since) || null;
        var levels = (data && data.levels) || [];

        var cells = [];
        var months = [];
        var lastMonth = -1;
        for (var c = 0; c < cols; c++) {
            var firstInRange = null;
            for (var r = 0; r < 7; r++) {
                var d = addDays(gridStart, c * 7 + r);
                var key = dayKey(d);
                var inRange = d >= start && d <= end;
                if (inRange && firstInRange === null) firstInRange = d;
                if (!inRange) {
                    cells.push({ date: key, inRange: false, wh: 0, level: 0 });
                    continue;
                }
                var rec = byDate[key];
                var wh = rec ? (Number(rec.wh) || 0) : 0;
                var noData = !!(dataSince && key < dataSince);
                cells.push({
                    date: key,
                    inRange: true,
                    wh: wh,
                    sessions: rec ? (Number(rec.sessions) || 0) : 0,
                    noData: noData,
                    level: noData ? -1 : classify(wh, levels)
                });
            }
            if (firstInRange !== null && firstInRange.getMonth() !== lastMonth) {
                months.push({ col: c, month: firstInRange.getMonth() });
                lastMonth = firstInRange.getMonth();
            }
        }
        return { start: dayKey(start), end: dayKey(end), cols: cols, cells: cells, months: months };
    }

    function layoutFingerprint(data) {
        var days = (data && data.days) || [];
        var range = (data && data.range) || {};
        var parts = [range.end, data && data.data_since,
                     (data && data.levels || []).join(',')];
        for (var i = 0; i < days.length; i++) {
            parts.push(days[i].date + ':' + days[i].wh + ':' + days[i].sessions);
        }
        return parts.join('|');
    }

    /**
     * 由可用宽高反算格子尺寸（纯函数，供测试）。
     *  - 宽度优先：53 列 + 缝正好铺满 availW，**无论多窄都必须塞得进去**——
     *    卡内不允许出现滚动条，所以宁可把格子压小、把缝压到 1px；
     *  - 缝宽三档：3px（透气）→ 2px → 1px，按"格子还能不能看"逐档退；
     *  - availH > 0 时再受"7 行塞进面板高度"约束（宽屏下高度才是瓶颈）；
     *  - 圆角随格子等比，10px 上约 2.6px，看起来像 GitHub / easyui 那种软方块。
     */
    function fitCell(availW, availH, cols) {
        var n = Math.round(Number(cols)) || 1;
        if (n < 1) n = 1;
        var w = Number(availW) > 0 ? Number(availW) : 0;
        var h = Number(availH) > 0 ? Number(availH) : 0;
        // 面板隐藏（切到别的 tab）时量不到宽度：给一个中性尺寸，切回来会重算。
        if (!w) return { cell: 10, gap: 2, radius: 2.6 };

        var gap = 3;
        var cell = (w + gap) / n - gap;
        while (cell < MIN_CELL && gap > 1) {
            gap -= 1;
            cell = (w + gap) / n - gap;
        }
        if (h) {
            var byH = (h - 6 * gap) / 7;
            if (byH < cell) cell = byH;
        }
        if (cell > MAX_CELL) cell = MAX_CELL;
        if (cell < FLOOR_CELL) cell = FLOOR_CELL;      // 再小就不是矩阵了
        // 向下取到 0.1：宁可差半像素留白，也不能因四舍五入顶出一根滚动条
        cell = Math.floor(cell * 10) / 10;
        return {
            cell: cell,
            gap: gap,
            radius: Math.max(1.5, Math.min(3.5, Math.round(cell * 0.26 * 10) / 10))
        };
    }

    // ── DOM / i18n 小工具 ───────────────────────────────────────

    function el(id) {
        return global.document ? global.document.getElementById(id) : null;
    }

    function t(key, params) {
        return (global.I18N && global.I18N.t) ? global.I18N.t(key, params) : key;
    }

    function esc(s) {
        return String(s === undefined || s === null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function setText(id, text) {
        var node = el(id);
        if (!node) return;
        node.textContent = text || '';
        node.hidden = !text;
    }

    function paneEl() {
        return (global.document && global.document.querySelector)
            ? global.document.querySelector('[data-energy-pane="calendar"]')
            : null;
    }

    function prefersStill() {
        if (!global.matchMedia) return true;         // 量不到就别演，宁可没有
        try { return global.matchMedia('(prefers-reduced-motion: reduce)').matches; }
        catch (e) { return true; }
    }

    // ── 尺寸：量面板 → 写 CSS 变量 ──────────────────────────────

    /** 面板里留给"格子矩阵"的高度：面板高度减去三行文字与间距。 */
    function gridRoom(pane) {
        var room = pane.clientHeight || 0;
        if (!room) return 0;
        var rows = ['.heatmap-summary', '.heatmap-months', '.heatmap-legend'];
        for (var i = 0; i < rows.length; i++) {
            var node = pane.querySelector ? pane.querySelector(rows[i]) : null;
            if (!node) continue;
            var rect = node.getBoundingClientRect ? node.getBoundingClientRect() : null;
            room -= (rect ? rect.height : (node.offsetHeight || 0)) + ROW_GAP;
        }
        return Math.max(0, room);
    }

    /**
     * 只有"面板高度是固定值"时才能按高度封顶。≤1023px 时面板是 height:auto，
     * 量到的高度就等于内容高度——拿它封顶会越算越小，所以那种断点下不封顶，
     * 让宽度说了算（与 index.css 的媒体查询同一条线）。
     */
    function heightLimited(pane) {
        if (!pane || !pane.clientWidth) return 0;
        if (!global.matchMedia) return 0;
        try {
            if (!global.matchMedia('(min-width: 1024px)').matches) return 0;
        } catch (e) { return 0; }
        return gridRoom(pane);
    }

    function measure(pane, cols) {
        var w = (pane && pane.clientWidth) || 0;
        var room = heightLimited(pane);
        var metrics = fitCell(w ? w - GUTTER_W : 0, room, cols);
        // 格子被压到 7px 以下时，星期那三个字的行高比格子还大，会叠成一坨：
        // 干脆不显示（GitHub 手机端同样省掉），把 24px 的标签列还给格子。
        if (w && metrics.cell < TINY_CELL) {
            metrics = fitCell(w, room, cols);
            metrics.tiny = true;
        }
        return metrics;
    }

    function applyMetrics(pane, cols) {
        var metrics = measure(pane, cols);
        state.metrics = metrics;
        if (pane && pane.classList && typeof pane.classList.toggle === 'function') {
            pane.classList.toggle('is-tiny', !!metrics.tiny);
        }
        var style = pane && pane.style;
        if (!style || typeof style.setProperty !== 'function') return;
        style.setProperty('--hm-cell', metrics.cell + 'px');
        style.setProperty('--hm-gap', metrics.gap + 'px');
        style.setProperty('--hm-r', metrics.radius + 'px');
        style.setProperty('--hm-cols', String(cols));
    }

    /** 窗口尺寸变化后重算格子（不重新取数、不重画 DOM：CSS 变量一改就重排）。 */
    function remeasure() {
        var pane = paneEl();
        if (!pane || !state.cols) return;
        applyMetrics(pane, state.cols);
    }

    // ── 悬停气泡 ────────────────────────────────────────────────

    function ensureTip() {
        if (state.tip && state.tip.parentNode) return state.tip;
        var doc = global.document;
        if (!doc || typeof doc.createElement !== 'function' || !doc.body) return null;
        var tip = doc.createElement('div');
        tip.className = 'hm-tip';
        tip.setAttribute('role', 'tooltip');
        tip.hidden = true;
        doc.body.appendChild(tip);
        state.tip = tip;
        return tip;
    }

    /** 一天的口径一句话：有电量 → "92.6 Wh · 26 次充电"；没记录 → 说明为什么空。 */
    function cellDetail(c) {
        if (!c) return '';
        if (c.noData) return t('heatmap.tipNoStats');
        if (!(c.wh > 0)) return t('heatmap.tipEmpty');
        var s = formatWh(c.wh);
        if (c.sessions) s += ' · ' + t('heatmap.tipSessions', { count: c.sessions });
        return s;
    }

    /** 两行：数值在上（主）、日期在下（次）。与 easyui Activity Heatmap 一致。 */
    function tipHtml(c) {
        var detail = cellDetail(c);
        var empty = c.noData || !(c.wh > 0);
        var val = empty
            ? '<span class="hm-tip-val is-muted">' + esc(detail) + '</span>'
            : '<span class="hm-tip-val">' + esc(formatWh(c.wh)) +
              (c.sessions ? '<i>· ' + esc(t('heatmap.tipSessions', { count: c.sessions })) + '</i>' : '') +
              '</span>';
        return val + '<span class="hm-tip-date">' + esc(c.date) + '</span>';
    }

    function positionTip(cell, tip) {
        var r = cell.getBoundingClientRect();
        var tw = tip.offsetWidth || 120;
        var th = tip.offsetHeight || 40;
        var left = r.left + r.width / 2 - tw / 2;
        var top = r.top - th - 8;
        if (top < 4) top = r.bottom + 8;               // 贴着视口顶部时翻到下面
        var vw = global.innerWidth || 0;
        if (vw) left = Math.max(4, Math.min(left, vw - tw - 4));
        tip.style.left = Math.round(left) + 'px';
        tip.style.top = Math.round(top) + 'px';
    }

    function showTip(cell, c) {
        var tip = ensureTip();
        if (!tip || !c) return;
        tip.innerHTML = tipHtml(c);
        tip.hidden = false;
        positionTip(cell, tip);
    }

    function hideTip() {
        if (state.tip) state.tip.hidden = true;
    }

    function cellFromEvent(e) {
        var target = e && e.target;
        if (!target || typeof target.closest !== 'function') return null;
        return target.closest('.hm-cell');
    }

    // ── 键盘：方向键在格子里走，游标那格弹同一个气泡 ──────────────

    function paintCursor(grid, c) {
        var prev = (grid.querySelector && grid.querySelector('.hm-cell.is-cursor')) || null;
        if (prev && prev.classList) prev.classList.remove('is-cursor');
        var node = grid.children ? grid.children[state.cursorIdx] : null;
        if (node && node.classList) node.classList.add('is-cursor');
        if (node) showTip(node, c);
        var live = el('heatmapLive');
        if (live && c) live.textContent = c.date + ' ' + cellDetail(c);
    }

    function clearCursor() {
        var grid = el('heatmapGrid');
        var prev = (grid && grid.querySelector && grid.querySelector('.hm-cell.is-cursor')) || null;
        if (prev && prev.classList) prev.classList.remove('is-cursor');
        var live = el('heatmapLive');
        if (live) live.textContent = '';
    }

    /** 游标移动：delta 按"天"算（列优先，所以右移一周 = +7）。 */
    function moveCursor(grid, delta) {
        var cells = state.cells;
        if (!grid || !cells || !cells.length) return;
        var i = state.cursorIdx;
        if (i < 0) {
            // 第一次按方向键：从今天往前找最近一个有效格
            i = cells.length - 1;
            while (i > 0 && !cells[i].inRange) i--;
        } else {
            i += delta;
            var step = delta > 0 ? 1 : -1;
            while (i >= 0 && i < cells.length && !cells[i].inRange) i += step;
            if (i < 0 || i >= cells.length) return;
        }
        if (!cells[i] || !cells[i].inRange) return;
        state.cursorIdx = i;
        paintCursor(grid, cells[i]);
    }

    function onKeyDown(e) {
        var key = e && e.key;
        var grid = el('heatmapGrid');
        var delta = 0;
        if (key === 'ArrowRight') delta = 7;
        else if (key === 'ArrowLeft') delta = -7;
        else if (key === 'ArrowDown') delta = 1;
        else if (key === 'ArrowUp') delta = -1;
        else if (key === 'Home') {
            if (e.preventDefault) e.preventDefault();
            state.cursorIdx = -1;
            moveCursor(grid, 1);
            return;
        } else if (key === 'End') {
            if (e.preventDefault) e.preventDefault();
            state.cursorIdx = -1;
            moveCursor(grid, 1);            // 首次进入即落在今天
            return;
        } else if (key === 'Escape') {
            state.cursorIdx = -1;
            clearCursor();
            hideTip();
            return;
        } else {
            return;
        }
        if (e.preventDefault) e.preventDefault();
        moveCursor(grid, delta);
    }

    function bindGrid() {
        var grid = el('heatmapGrid');
        if (!grid || typeof grid.addEventListener !== 'function' || grid.__hmTipBound) {
            return;
        }
        grid.__hmTipBound = true;
        grid.addEventListener('mouseover', function (e) {
            var cell = cellFromEvent(e);
            var key = cell && cell.getAttribute('data-d');
            var c = key && state.view ? state.view[key] : null;
            if (!c) { hideTip(); return; }
            // 没有记录的格子也给气泡："无充电记录" / "无统计数据"。
            showTip(cell, c);
        });
        grid.addEventListener('mouseout', function (e) {
            // 在格子之间移动时 relatedTarget 仍是网格内的格子：不闪。
            var to = e && e.relatedTarget;
            if (to && grid.contains && grid.contains(to)) return;
            hideTip();
        });
        grid.addEventListener('keydown', onKeyDown);
        grid.addEventListener('blur', function () {
            state.cursorIdx = -1;
            clearCursor();
            hideTip();
        });
        // 触屏没有 hover：点一下格子把气泡钉住，点别处收起（不建定时器）
        grid.addEventListener('click', function (e) {
            var cell = cellFromEvent(e);
            var key = cell && cell.getAttribute('data-d');
            var c = key && state.view ? state.view[key] : null;
            if (!c) { hideTip(); return; }
            showTip(cell, c);
        });
        if (global.document && typeof global.document.addEventListener === 'function') {
            global.document.addEventListener('click', function (e) {
                if (cellFromEvent(e)) return;      // 格子自己的 click 已经处理
                hideTip();
            }, true);
        }
    }

    // 滚动 / 缩放后气泡的视口坐标就过期了：直接隐藏，等下一次悬停。
    if (global.document && typeof global.document.addEventListener === 'function') {
        global.document.addEventListener('scroll', hideTip, true);
    }
    if (typeof global.addEventListener === 'function') {
        global.addEventListener('resize', function () {
            hideTip();
            remeasure();
        });
    }

    function emitRendered() {
        // app.js 监听这个事件更新卡头（热力图数据晚于 renderEnergy 到达）。
        if (!global.document || typeof global.document.dispatchEvent !== 'function') return;
        if (typeof global.CustomEvent !== 'function') return;
        try {
            global.document.dispatchEvent(new global.CustomEvent('heatmap-rendered'));
        } catch (e) { /* 老浏览器 / 测试桩：忽略 */ }
    }

    function clearView() {
        var grid = el('heatmapGrid');
        if (grid) grid.innerHTML = '';
        var months = el('heatmapMonths');
        if (months) months.innerHTML = '';
        var weekdays = el('heatmapWeekdays');
        if (weekdays) weekdays.innerHTML = '';
        setText('heatmapPeak', '');
        setText('heatmapNoData', '');
        state.view = null;
        state.cells = null;
        state.cursorIdx = -1;
        hideTip();
        var pane = paneEl();
        if (pane) pane.classList.add('is-empty');
        emitRendered();
    }

    // ── 渲染 ────────────────────────────────────────────────────

    function render() {
        var grid = el('heatmapGrid');
        if (!grid) return;
        var data = state.data;

        if (!data || !data.days || !data.days.length) {
            clearView();
            setText('heatmapNoData', t('heatmap.empty'));
            if (grid.setAttribute) grid.setAttribute('aria-label', t('heatmap.ariaShort'));
            return;
        }

        var fp = layoutFingerprint(data);
        if (fp === state.fp) { emitRendered(); return; }
        state.fp = fp;

        var layout = buildLayout(
            data, parseDay((data.range && data.range.end) || '') || undefined);
        var pane = paneEl();
        if (pane) pane.classList.remove('is-empty');
        state.cols = layout.cols;
        state.cells = layout.cells;
        applyMetrics(pane, layout.cols);

        // 入场错峰只演第一次（切语言/轮询重绘不再抖）：给首帧的格子挂 .hm-cell--in
        var animate = !state.animated && !prefersStill();
        if (animate) state.animated = true;

        var html = [];
        var view = {};
        for (var i = 0; i < layout.cells.length; i++) {
            var c = layout.cells[i];
            if (!c.inRange) {
                html.push('<i class="hm-cell hm-cell--void"></i>');
                continue;
            }
            // 与 GitHub 一致：统计起点之前也是普通 0 级色（不是空心框），
            // "从哪天开始有统计"由上方文案 + 气泡说明。
            var cls = 'hm-cell lv' + (c.noData ? 0 : c.level);
            var extra = '';
            if (animate) {
                cls += ' hm-cell--in';
                extra = ' style="--d:' + ((i / 7 | 0) * 0.004).toFixed(3) + 's"';
            }
            // 气泡内容在 mouseover 时按 data-d 查 state.view，不写原生 title
            // （原生 title 有延迟、样式不可控，还会和自绘气泡同时出现）。
            html.push('<i class="' + cls + '" data-d="' + c.date + '"' + extra + '></i>');
            view[c.date] = c;
        }
        grid.innerHTML = html.join('');
        state.view = view;
        bindGrid();
        hideTip();          // 重渲染后旧气泡的位置已失效

        var monthNames = t('heatmap.months');
        if (!Array.isArray(monthNames)) monthNames = [];
        var months = layout.months;                       // 按列升序
        var byCol = {};
        for (var m = 0; m < months.length; m++) byCol[months[m].col] = m;
        var lastLabel = months.length ? months[months.length - 1].col : -1;
        // 月份标签抽稀：格子被压小后（窄窗）相邻两个月的标签会叠成一坨，
        // 按"一个标签要多宽 / 一列多少 px"反算步长，只标其中一部分。13 个下标
        // （首月 + 12 个月）里任何步长都能整除 12，所以末月永远留得住。
        var pitch = state.metrics ? (state.metrics.cell + state.metrics.gap) : 12;
        var minCols = Math.max(1, Math.ceil(LABEL_W / (pitch > 0 ? pitch : 12)));
        var step = Math.max(1, Math.ceil(minCols / 4.4));   // 一个月 ≈ 4.4 列
        var monthHtml = '';
        for (var col = 0; col < layout.cols; col++) {
            var idx = byCol[col];
            var show = idx !== undefined &&
                (idx % step === 0 || idx === months.length - 1);
            // 最后一枚月份标签向右贴住网格右边缘（justify-self:end）：它比一格宽，
            // 左对齐会溢出到卡片外，被裁掉或顶出滚动条。
            var lastCls = (show && col === lastLabel) ? ' is-last' : '';
            monthHtml += '<span class="heatmap-month' + lastCls + '">' +
                (show ? esc(monthNames[months[idx].month] || '') : '') + '</span>';
        }
        var monthsEl = el('heatmapMonths');
        if (monthsEl) monthsEl.innerHTML = monthHtml;

        var wd = t('heatmap.weekdays');
        if (!Array.isArray(wd)) wd = [];
        var wdHtml = '';
        for (var r = 0; r < 7; r++) {
            wdHtml += '<span class="heatmap-weekday">' +
                (WEEKDAY_ROWS.indexOf(r) >= 0 && wd[r] ? esc(wd[r]) : '') + '</span>';
        }
        var wdEl = el('heatmapWeekdays');
        if (wdEl) wdEl.innerHTML = wdHtml;

        var peak = data.peak;
        setText('heatmapPeak', peak
            ? t('heatmap.peak', { date: peak.date, wh: formatWh(peak.wh) })
            : '');
        // "统计数据自 … 起"不再占摘要行（一行只有峰值，靠右）；这件事由每格的气泡
        // 说（起点之前是"无统计数据"）。#heatmapNoData 只留给空态提示用。
        setText('heatmapNoData', '');
        if (grid.setAttribute) {
            grid.setAttribute('aria-label', peak
                ? t('heatmap.aria', { date: peak.date, wh: formatWh(peak.wh) })
                : t('heatmap.ariaShort'));
        }

        // 轮询会整块重画：键盘游标留在原来那一格，气泡跟着回来
        if (state.cursorIdx >= 0 && global.document &&
                global.document.activeElement === grid) {
            state.cursorIdx = Math.min(state.cursorIdx, layout.cells.length - 1);
            paintCursor(grid, layout.cells[state.cursorIdx]);
        }
        emitRendered();
    }

    // ── 取数（单飞 + 序号作废） ─────────────────────────────────

    function fetchData() {
        var store = global.DeviceStore;
        if (!store || typeof store.api !== 'function') {
            return Promise.reject(new Error('DeviceStore not ready'));
        }
        return store.api('/api/energy/heatmap').then(function (res) {
            if (!res || res.status === 304) return null;   // 走浏览器缓存，保留旧图
            return res.json();
        });
    }

    function refresh() {
        var seq = state.seq;
        // 单飞：同一设备的在途请求直接复用（切 tab / 轮询 / 切设备可能同时触发）。
        if (state.inflight && state.inflightSeq === seq) return state.inflight;
        var p = fetchData().then(function (data) {
            if (seq !== state.seq) return null;            // 旧设备的迟到响应：丢弃
            if (data && data.days) {
                state.data = data;
                render();
            }
            return data;
        }).catch(function (err) {
            if (seq === state.seq && global.console) {
                console.error('Heatmap error:', err);
            }
            return null;
        });
        state.inflight = p;
        state.inflightSeq = seq;
        p.then(function () { if (state.inflight === p) state.inflight = null; });
        return p;
    }

    function reset() {
        state.seq++;
        state.data = null;
        state.fp = '';
        state.inflight = null;
        state.animated = false;     // 换设备：入场动效重新演一次
        state.metrics = null;
        clearView();
    }

    function ensure() {
        if (state.data) {
            // 切回这一屏时尺寸可能变了（旋转/拉窗口）：只重算 CSS 变量，不重发请求
            remeasure();
            return Promise.resolve(state.data);
        }
        return refresh();
    }

    // ── 导出 ────────────────────────────────────────────────────

    global.HeatmapPure = {
        dayKey: dayKey,
        parseDay: parseDay,
        classify: classify,
        buildLayout: buildLayout,
        fitCell: fitCell,
        formatWh: formatWh,
        layoutFingerprint: layoutFingerprint
    };

    global.Heatmap = {
        ensure: ensure,
        refresh: refresh,
        reset: reset,
        hasData: function () {
            return !!(state.data && state.data.days && state.data.days.length);
        },
        totalWh: function () {
            return state.data ? (Number(state.data.total_wh) || 0) : 0;
        },
        _state: state
    };

    // 语言切换：文案在语言包里，重渲染即可，**不重新请求**。
    if (global.I18N && typeof global.I18N.onChange === 'function') {
        global.I18N.onChange(function () {
            state.fp = '';
            if (state.data) render();
        });
    }
})(typeof window !== 'undefined' ? window : this);
