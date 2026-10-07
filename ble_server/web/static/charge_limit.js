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

    // 端口模式（长期供电 / 充满即停）：与限额分开一个端点（布尔集合，非数值阈值）
    var modes = {
        permanent_ports: [], full_off_ports: {}, full_off_fired: [],
        loaded: false, error: null
    };
    var modesPending = {};

    function fetchPortModes() {
        return fetch(API_BASE + '/api/port-modes')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.ok) {
                    modes.permanent_ports = data.permanent_ports || [];
                    modes.full_off_ports = data.full_off_ports || {};
                    modes.full_off_fired = data.full_off_fired || [];
                    modes.loaded = true;
                    modes.error = null;
                } else {
                    modes.error = (data && data.error) || 'invalid response';
                }
                return modes;
            })
            .catch(function (e) {
                modes.error = e && e.message ? e.message : String(e);
                return modes;
            });
    }

    // 保存单个端口模式。field: 'permanent' | 'full_off'，value: bool，
    // mode 只对 full_off 有意义（once / always，与限额同一套语义）。
    // 后端强制互斥（长期供电端口不允许即停），返回的整份状态直接覆盖本地。
    //
    // 在途保护按 port+field 占坑：两枚开关互不阻塞（用户"常供 → 立刻点自动"
    // 或网络慢时连点不会静默丢掉最后一次操作）；无论成功失败都要释放，
    // 否则一次同步抛错会永久卡住该端口的这枚开关。
    function savePortMode(port, field, value, mode) {
        var slot = port + ':' + field;
        if (modesPending[slot]) return Promise.resolve({ ok: false, error: 'pending' });
        modesPending[slot] = true;
        var release = function () { modesPending[slot] = false; };
        var body = { port: port };
        body[field] = !!value;
        if (field === 'full_off' && value && mode) body.mode = mode;
        return fetch(API_BASE + '/api/port-modes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
            .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); })
            .then(function (res) {
                release();
                if (res.body && res.body.ok) {
                    modes.permanent_ports = res.body.permanent_ports || [];
                    modes.full_off_ports = res.body.full_off_ports || {};
                    modes.full_off_fired = res.body.full_off_fired || [];
                    modes.error = null;
                    return { ok: true, modes: modes };
                }
                var msg = (res.body && res.body.error) || ('HTTP ' + res.status);
                modes.error = msg;
                return { ok: false, error: msg, modes: modes };
            })
            .catch(function (e) {
                release();
                var msg = e && e.message ? e.message : String(e);
                modes.error = msg;
                return { ok: false, error: msg, modes: modes };
            });
    }

    function isPermanent(port) { return modes.permanent_ports.indexOf(port) >= 0; }
    function isFullOff(port) { return !!(modes.full_off_ports || {})[port]; }
    function fullOffMode(port) { return (modes.full_off_ports || {})[port] || ''; }
    function fullOffFired(port) { return modes.full_off_fired.indexOf(port) >= 0; }
    // "已配置"= 有能量限额、开了充满即停、或设为长期供电；"已触发"= 前两者之一真断过电。
    // 状态文字、卡组下方状态点统一用这两个判定，避免几处各写一套。
    // 常供口在卡面上收起了限额控件，但状态点仍要实心——否则"这口有设置"在图示上看不出来。
    function isArmed(port) {
        return entryFor(port).wh > 0 || isFullOff(port) || isPermanent(port);
    }
    function isFired(port) { return !!entryFor(port).fired || fullOffFired(port); }

    function esc(text) {
        return String(text === undefined || text === null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // 模式下拉由各页提供（桌面 limit-mode-<port> / 手机 limitMode_<port>）：
    // 点"充满即停"时把当前选中的 once/always 一起提交，语义与"设置"按钮一致。
    var modeReader = null;
    function setModeReader(fn) { modeReader = fn; }
    function readMode(port) {
        try {
            return (typeof modeReader === 'function' ? modeReader(port) : '') || null;
        } catch (e) { return null; }
    }

    // ── 两枚紧凑开关（长期供电 / 充满即停） ──
    // 结构两页共用（样式各自定义）：圆点 + 两字标签，细节只放 title/aria-label。
    // 关键是**不新增行**——它们挂进卡片头部（端口名与状态之间），卡高与改版前一致。
    var MODE_FIELDS = [
        { field: 'permanent', labelKey: 'chargeLimit.permanent', hintKey: 'chargeLimit.permanentHint' },
        { field: 'full_off', labelKey: 'chargeLimit.fullOff', hintKey: 'chargeLimit.fullOffHint' }
    ];

    // extraClass：借用页面自己的预设值按钮样式（桌面 .countdown-quick-btn /
    //   手机 .charge-limit-chip），让"自动"和 5/10/…Wh 长得一模一样；此时不带圆点，
    //   状态靠端口色（is-on / is-fired）。不给就用卡片头部那枚胶囊样式。
    function chipHtml(port, field, extraClass) {
        var spec = null;
        MODE_FIELDS.forEach(function (f) { if (f.field === field) spec = f; });
        if (!spec) return '';
        // port 会拼进 onclick 的 JS 字符串里——那是"可执行代码"不是属性值，
        // 只转义引号挡不住注入。白名单外的端口名一律拒绝生成按钮。
        if (PORT_KEYS.indexOf(port) < 0) return '';
        var label = t(spec.labelKey);
        var hint = t(spec.hintKey);
        // 卡头那枚"长期供电"做成**开关**（轨道+滑块）而不是徽标：胶囊样式跟状态徽标
        // 长得一样，用户不会觉得它能点；开关是通用语言，一眼就知道是控件。
        // 可点区域由 CSS 的 ::before 撑到触屏尺寸，布局高度不动（卡高不能变）。
        if (!extraClass && spec.field === 'permanent') {
            return '<button type="button" class="limit-perm-switch"'
                + ' id="mode-permanent-' + port + '"'
                + ' role="switch" aria-checked="false"'
                + ' title="' + esc(hint) + '" aria-label="' + esc(label + '：' + hint) + '"'
                + ' onclick="togglePortMode(\'' + port + '\',\'permanent\')">'
                + '<span class="limit-perm-switch-track" aria-hidden="true"></span>'
                + '<span class="limit-perm-switch-text">' + esc(label) + '</span></button>';
        }
        var cls = extraClass ? extraClass + ' limit-auto'
                             : 'limit-mode-chip limit-mode-chip--' + spec.field;
        var dot = extraClass ? '' : '<span class="limit-mode-chip-dot" aria-hidden="true"></span>';
        return '<button type="button" class="' + cls + '"'
            + ' id="mode-' + spec.field + '-' + port + '"'
            + ' role="switch" aria-checked="false"'
            + ' title="' + esc(hint) + '" aria-label="' + esc(label + '：' + hint) + '"'
            + ' onclick="togglePortMode(\'' + port + '\',\'' + spec.field + '\')">'
            + dot
            + '<span class="limit-mode-chip-label">' + esc(label) + '</span></button>';
    }

    // 卡头那一组只放"长期供电"；"充满即停"和 5/10/…Wh 预设值同排（见两页的卡面模板）
    function modeChipsHtml(port) {
        return '<div class="limit-modes">' + chipHtml(port, 'permanent') + '</div>';
    }

    // ── 长期供电的二次确认 ──
    // 开启常供时后端会**清掉该端口的充电量限额**（不可撤销）。有额度时先点一次只把
    // 控件切到"确认"态并给一句提示，4s 内再点一次才真的开——单次误触不会抹掉配置。
    // 状态只活在本模块里：它只改控件自己的文字/类名，页面那次 5s 轮询碰不到这些。
    var permConfirmTimers = {};
    var PERM_CONFIRM_MS = 4000;

    function permanentSwitchEl(port) {
        return document.getElementById('mode-permanent-' + port);
    }

    function disarmPermanentConfirm(port) {
        var el = permanentSwitchEl(port);
        if (permConfirmTimers[port]) {
            clearTimeout(permConfirmTimers[port]);
            permConfirmTimers[port] = null;
        }
        if (!el || !el.classList.contains('is-confirm')) return;
        el.classList.remove('is-confirm');
        var txt = el.querySelector('.limit-perm-switch-text');
        if (txt) txt.textContent = t('chargeLimit.permanent');
    }

    function armPermanentConfirm(port) {
        var el = permanentSwitchEl(port);
        if (!el) return;
        var txt = el.querySelector('.limit-perm-switch-text');
        if (txt) txt.textContent = t('chargeLimit.permanentConfirm');
        el.classList.add('is-confirm');
        notify(t('chargeLimit.permanentClearsLimit'));
        if (permConfirmTimers[port]) clearTimeout(permConfirmTimers[port]);
        permConfirmTimers[port] = setTimeout(function () {
            disarmPermanentConfirm(port);
        }, PERM_CONFIRM_MS);
    }

    // 共享的开关点击处理（两页都由这里实现，避免各写一遍互斥/提示逻辑）
    function togglePortMode(port, field) {
        if (!PORT_KEYS.length || PORT_KEYS.indexOf(port) < 0) {   // 见 chipHtml 的转义说明
            return Promise.resolve({ ok: false, error: 'bad port' });
        }
        var next = field === 'permanent' ? !isPermanent(port) : !isFullOff(port);
        // 开启常供会抹掉限额：有额度时先要一次确认
        if (field === 'permanent' && next && entryFor(port).wh > 0) {
            var el = permanentSwitchEl(port);
            if (el && !el.classList.contains('is-confirm')) {
                armPermanentConfirm(port);
                return Promise.resolve({ ok: false, error: 'confirm' });
            }
            disarmPermanentConfirm(port);
        }
        if (field === 'permanent') disarmPermanentConfirm(port);
        var mode = (field === 'full_off' && next) ? readMode(port) : null;
        // 在途反馈：请求期间把控件置灰并挡住重复点击（服务端也有在途保护，
        // 但用户看不到，点两次会以为没生效）
        var btn = document.getElementById('mode-' + field + '-' + port);
        var clearBusy = function () { if (btn) btn.removeAttribute('aria-busy'); };
        if (btn) btn.setAttribute('aria-busy', 'true');
        return savePortMode(port, field, next, mode).then(function (res) {
            clearBusy();
            // pending = 同一枚开关还在途中，属"重复点击"，不是失败，静默忽略
            if (!res.ok && res.error !== 'pending' && res.error !== 'confirm') {
                notify(t('chargeLimit.saveFailed', { msg: res.error }));
            }
            if (res.ok && field === 'permanent' && next && typeof fetchLimits === 'function') {
                return fetchLimits().then(function () {
                    if (typeof global.updateChargeLimitUI === 'function') {
                        global.updateChargeLimitUI();
                    }
                    return res;
                });
            }
            if (typeof global.updateChargeLimitUI === 'function') global.updateChargeLimitUI();
            return res;
        });
    }

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

    // ── 堆叠卡组的翻页判定（手机端） ──
    // 抽成纯函数放这里，是因为"划多远算翻页""转多少格"是这套交互里唯一有分支的
    // 计算；DOM 手势那部分没法在 node 里测，这两个可以。

    var SWIPE_MIN_PX = 46;    // 位移下限：阈值太低会在输入框上误翻页
    var SWIPE_RATIO = 0.22;   // 或卡片宽度的这个比例，取两者中较大的

    // dx: 手指水平位移（向左为负）；width: 栈顶卡宽度。
    // 返回 +1 向后翻 / -1 向前翻 / 0 回弹。左右对称，共用同一套阈值。
    function swipeDecision(dx, width) {
        if (!isFinite(dx) || !isFinite(width) || width <= 0) return 0;
        var need = Math.max(SWIPE_MIN_PX, width * SWIPE_RATIO);
        if (dx <= -need) return 1;
        if (dx >= need) return -1;
        return 0;
    }

    // 把栈序轮转 steps 格（正数向后翻）。返回新数组、不改入参——翻页动画期间
    // 调用方仍要读旧序。steps 为任意整数，按长度取模，越界自动回绕。
    function flipOrder(order, steps) {
        var n = order.length;
        if (n < 2) return order.slice();
        var k = ((steps % n) + n) % n;
        if (!k) return order.slice();
        return order.slice(k).concat(order.slice(0, k));
    }

    // ── 状态描述（两页共用文案，样式各自处理） ──

    function entryFor(port) {
        return state.limits[port] || { wh: 0, mode: MODE_ONCE, session_wh: 0, is_charging: false, fired: false };
    }

    // "已充 12.3 / 30 Wh"；未设限额时只显示已充能量（长期供电卡另有状态面板）
    function progressText(port) {
        var e = entryFor(port);
        if (e.wh > 0) {
            return t('chargeLimit.progress',
                     { used: (e.session_wh || 0).toFixed(1), total: e.wh });
        }
        // 只开了充满即停：没有目标值，就只报本次已充多少（进度条同时显示）
        if (isFullOff(port)) {
            return t('chargeLimit.used', { used: (e.session_wh || 0).toFixed(1) });
        }
        return '';
    }

    // ── 长期供电卡的状态面板 ──
    // 这张卡收起了所有限额控件，卡高仍由同排/同一沓的其它卡决定：腾出来的空间要
    // 用"读数"填满，而不是空着，也不是放一个不随数据变化的装饰圆环。
    // 面板上不放**实时**数字：实时功率每帧都在跳，而且"这个口此刻在不在供电"由卡头
    // 那个呼吸状态点表达更直观。所以这里只放统计值：
    //   主读数   = 本会话平均功率（服务端按"会话能量 ÷ 会话时长"算，结束后仍然准确）
    //   ── 发丝线 ──
    //   次级指标 = 本次已输出 / 已持续（待机时改报上次输出，没有就报"暂无输出"）
    //   说明     = 为什么不记录曲线（一行，避免用户以为数据丢了）
    // 高度由两页各自的 CSS 定（桌面卡比手机卡高，字号也大一档），面板只管次序。
    function metricHtml(label, valueHtml) {
        return '<div class="limit-perm-metric">'
            + '<span class="limit-perm-k">' + esc(label) + '</span>'
            + '<span class="limit-perm-v">' + valueHtml + '</span></div>';
    }

    // 数值 + 单位：单位小一号、次级墨色，跟数字同一行（tabular-nums 下不抖）
    function numHtml(num, unit) {
        return esc(num) + (unit ? '<span class="limit-perm-u">' + esc(unit) + '</span>' : '');
    }

    // 人类可读时长（走 i18n，不用 "1h 23m" 这类缩写：中文界面上 m 到底是分还是月不明确）
    // 分档：<1h 报分钟，<24h 报"小时+分"，≥24h 只报"天+小时"（分钟在按天计的常供会话里
    // 没有信息量），≥100 天再省掉小时（三个多月后小时同样没有信息量）。
    // 单位一律紧贴数字（5小时18分 / 2天3小时）：桌面卡片的等分列只有 147px（24px 字号），
    // 带空格时 "23 小时 59 分" 要 158px、"99 天 23 小时" 要 158px，都会被省略号截掉；
    // 紧凑写法下最长的 99天23小时只要 139px，全档位都不会截断。
    function fmtDurationText(sec) {
        var s = Math.max(0, Math.floor(Number(sec) || 0));
        var d = Math.floor(s / 86400);
        var h = Math.floor((s % 86400) / 3600);
        var m = Math.floor((s % 3600) / 60);
        if (d >= 100) return t('chargeLimit.permanentDurDayOnly', { d: d });
        if (d > 0) return t('chargeLimit.permanentDurDay', { d: d, h: h });
        if (h > 0) return t('chargeLimit.permanentDurHour', { h: h, m: m });
        return t('chargeLimit.permanentDurMin', { m: m });
    }

    function permanentPanelHtml(port) {
        var e = entryFor(port);
        var active = !!e.is_charging;
        var wh = e.session_wh || 0;
        var avg = isFinite(e.avg_power_w) ? e.avg_power_w : 0;

        var metrics;
        if (active) {
            metrics = metricHtml(t('chargeLimit.permanentOut'), numHtml(wh.toFixed(1), 'Wh'));
            if (e.session_sec > 0) {
                metrics += metricHtml(t('chargeLimit.permanentDurLabel'),
                                      esc(fmtDurationText(e.session_sec)));
            }
        } else if (wh > 0) {
            // 待机：报"上次输出"，不提时长（会话已结束，卡片不再展示那次的时长）
            metrics = metricHtml(t('chargeLimit.permanentLastOut'), numHtml(wh.toFixed(1), 'Wh'));
        } else {
            // 从未输出过：标签仍用"上次输出"与"上次平均功率"对齐，值写"暂无输出"
            metrics = metricHtml(t('chargeLimit.permanentLastOut'),
                                 esc(t('chargeLimit.permanentNoOutput')));
        }

        // 主读数标签带"本次/上次"：同一张卡在会话结束后仍然显示上一段的平均功率，
        // 不写清楚会被读成"现在正在以这个功率供电"。
        var avgLabel = active ? t('chargeLimit.permanentAvg')
                              : t('chargeLimit.permanentAvgLast');

        return '<div class="limit-perm-main">'
            + '<span class="limit-perm-k limit-perm-avgk">' + esc(avgLabel) + '</span>'
            + '<span class="limit-perm-power' + (active ? '' : ' is-idle') + '">'
            + avg.toFixed(1) + '<span class="limit-perm-u">W</span></span>'
            + '</div>'
            + '<div class="limit-perm-metrics">' + metrics + '</div>'
            + '<div class="limit-perm-note">' + esc(t('chargeLimit.permanentNote')) + '</div>';
    }

    function statusText(port) {
        // 与状态点同源：常供口也算"已启用"（卡面上那行文字被收起，但读屏/提示仍会念）
        var text = isArmed(port) ? t('chargeLimit.enabled') : t('chargeLimit.off');
        // 两个来源（能量限额 / 充满即停）合并成一个"已触发"后缀：分开写会拼出
        // "已启用 · 已触发 · 已充满停止"这种重复又矛盾的状态。
        if (isFired(port)) text += ' · ' + t('chargeLimit.fired');
        return text;
    }

    // 已充 / 限额 的进度百分比（0-100），未设限额或无会话时为 0
    function progressPct(port) {
        var e = entryFor(port);
        if (e.wh > 0) {
            var pct = (e.session_wh || 0) / e.wh * 100;
            return Math.max(0, Math.min(100, pct));
        }
        // 充满即停没有目标值：供电中就把槽填满（表示"已武装、正在供电"），空闲则空槽
        if (isFullOff(port)) return e.is_charging ? 100 : 0;
        return 0;
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            fetchLimits: fetchLimits,
            fetchPortModes: fetchPortModes,
            saveLimit: saveLimit,
            savePortMode: savePortMode,
            togglePortMode: togglePortMode,
            isPermanent: isPermanent,
            isFullOff: isFullOff,
            fullOffFired: fullOffFired,
            modeChipsHtml: modeChipsHtml,
            MODE_FIELDS: MODE_FIELDS,
            parseWhInput: parseWhInput,
            swipeDecision: swipeDecision,
            flipOrder: flipOrder
        };
    }

    global.ChargeLimit = {
        PORT_KEYS: PORT_KEYS,
        QUICK_WH: QUICK_WH,
        MODE_ONCE: MODE_ONCE,
        MODE_ALWAYS: MODE_ALWAYS,
        MODE_FIELDS: MODE_FIELDS,
        SWIPE_MIN_PX: SWIPE_MIN_PX,
        SWIPE_RATIO: SWIPE_RATIO,
        state: state,
        modes: modes,
        setNotifier: setNotifier,
        notify: notify,
        fetchLimits: fetchLimits,
        fetchPortModes: fetchPortModes,
        saveLimit: saveLimit,
        savePortMode: savePortMode,
        togglePortMode: togglePortMode,
        isPermanent: isPermanent,
        isFullOff: isFullOff,
        fullOffMode: fullOffMode,
        fullOffFired: fullOffFired,
        isArmed: isArmed,
        isFired: isFired,
        chipHtml: chipHtml,
        modeChipsHtml: modeChipsHtml,
        permanentPanelHtml: permanentPanelHtml,
        setModeReader: setModeReader,
        parseWhInput: parseWhInput,
        swipeDecision: swipeDecision,
        flipOrder: flipOrder,
        entryFor: entryFor,
        progressText: progressText,
        statusText: statusText,
        progressPct: progressPct,
        t: t
    };

    // 开关点击处理器挂在全局：卡面 HTML 是字符串拼出来的 onclick，两页共用同一实现
    global.togglePortMode = togglePortMode;
})(typeof window !== 'undefined' ? window : this);
