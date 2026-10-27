/*!
 * device-bench.js — 多设备"工作台"：设备切换条 + 全部设备汇总
 *
 * 为什么另起一个文件而不是塞进 device-store.js：
 *   store 只管"数据按设备分桶 + 当前指针 + 请求出口"，它不知道界面长什么样；
 *   这里只管"把多台设备画成一条可点的仪表条和一页汇总"，不碰网络与指针。
 *   手机页（phone.html）不用这条牌堆：那边的设备切换长在标题栏的下拉列表里
 *   （phone.js 自己渲染）；bench 在手机上只负责"全部设备"那一页。
 *   两页共用 summarise() 折算——两页的端口桶形状本来就不同。
 *
 *   桌面这副牌一次只显示一张卡：卡片落在同一个 grid 格子里，只有栈顶那张可见，
 *   左右拖动 / 滚轮 / 方向键 / 下面的 ‹ › 箭头翻牌；单击卡片 = 打开这台设备的设置。
 *
 * 设计取向（为什么不是"再来一排 pill"）：
 *   一台充电器时页面是一块仪表：设备图 + 四个端口光条。多台之后要回答的问题
 *   变成"现在哪台在干活、总共多少瓦"——所以切换条做成**微型仪表**：每台一枚
 *   瓷砖，带链路灯、四个端口微点、实时瓦数，底边一条"功率脊"按该台占全家的
 *   份额显示长度。脊就是这条栏唯一花哨的地方，其余保持安静。
 *   "全部"不是一个总览大数字，而是一张**清单**：一行一台，按实时功率从高到低，
 *   点哪行就去哪台。多台设备时人的第一问题就是"谁在充"，排序在这里是有意义的
 *   信息，所以允许它变动（切换条本身按注册顺序固定，绝不让按钮位置跳动）。
 *
 * 单设备零变化：devices.length < 2 时整条与汇总页都不产生任何 DOM，
 * index.html / phone.html 的像素与改造前完全一致。
 */
(function (global) {
    'use strict';

    var ALL = '__all__';
    var TICK_MS = 1000;          // 只读内存里的桶，不发请求
    var ENERGY_TTL_MS = 60000;   // "今日用电"60 秒内不重复拉

    var store = null;
    var opts = null;
    var rail = null;
    var fleetHost = null;
    var mode = 'device';         // 'device' | 'fleet'
    var railSig = '';
    var fleetSig = '';
    var timer = null;
    var energy = {};             // device_id -> {wh, at, pending}
    var preview = false;         // ?preview=multi：在伪造设备上验收切换（见 fakeDevices）

    function t(key, params) {
        return (global.I18N && global.I18N.t) ? global.I18N.t(key, params) : key;
    }

    function esc(v) {
        return String(v === undefined || v === null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // ── 预览：在没有第二台硬件的机器上验证"切换真的生效" ────────────────
    // 这套界面在单设备时按设计**完全不出现**，所以只有一台充电器就没法验收
    // "点卡片 → 整页换成那台"。URL 带 ?preview=multi 时登记 3 台伪造设备：
    //
    //   · 它们是**真的 store 桶**，由页面自带的 previewSeed() 灌入数据（因为桶的
    //     形状是各页私有的，bench 不能瞎猜）；
    //   · 点卡片走的是**真的** store.switchDevice → replay → 页面各组件重画，
    //     所以看到的是真机制，不是一张效果图；
    //   · 唯一被截断的是网络：store 认出这些 id 后不发请求（服务端没有它们），
    //     也就不会 404，更不会碰真机。
    //
    // 换句话说：除了数据来源，切设备这条路和真机多设备一模一样。
    function fakeDevices() {
        return [
            { id: 'pv1', name: 'Charger A', mac: '00:11:22:33:44:01', is_primary: false, connected: true },
            { id: 'pv2', name: 'Charger B', mac: '00:11:22:33:44:02', is_primary: false, connected: true },
            { id: 'pv3', name: 'Charger C', mac: '00:11:22:33:44:03', is_primary: false, connected: false }
        ];
    }

    /** 一台设备时服务端**不发** devices 清单（§3.8：单设备事件流必须逐字节不变），
     *  可界面这边仍然要一张"这就是当前设备"的卡片 —— 它是设备状态，也是这台设备
     *  唯一的设置入口。所以清单为空时补一条自带记录（名字随后由 /api/devices 补全，
     *  见 init 里的一次性探测）。 */
    function soloDevice() {
        return {
            id: currentId() || (store && store.PRIMARY) || 'default',
            name: '', mac: '', is_primary: true
        };
    }

    function realDevices() {
        var list = store ? store.devices() : [];
        return list.length ? list : (store ? [soloDevice()] : []);
    }

    // 真机 + 3 台伪造设备：真机必须留在列表里 —— 这样"切到伪造设备再切回来"
    // 走的仍是真机那条数据通路，能证明预览没有污染真实链路。
    // 列表每次现算：设备清单是 SSE init 之后才到的，init 时缓存一份会漏掉真机。
    function previewDevices() {
        return realDevices().concat(fakeDevices());
    }

    /** 给伪造设备灌数据。tick 也调一次，读数才会跟着真机一起动。 */
    function seedPreview() {
        if (!preview || !opts.previewSeed) return;
        var fakes = fakeDevices();
        for (var i = 0; i < fakes.length; i++) {
            var d = fakes[i];
            try { opts.previewSeed(d.id, store.get(d.id), i); } catch (e) { console.error('preview seed failed:', e); }
        }
    }

    function devices() {
        return preview ? previewDevices() : realDevices();
    }

    function currentId() {
        return store ? store.current() : null;
    }

    function summaryOf(id) {
        var b = store.get(id) || {};
        var s = (opts.summarise || function () { return {}; })(b) || {};
        s.online = s.online !== undefined
            ? !!s.online
            : !!(b.bleConnected || (b.connected && b.authenticated));
        s.watts = Math.max(0, Number(s.watts) || 0);
        s.ports = s.ports || [];
        return s;
    }

    function totalWatts() {
        var sum = 0;
        devices().forEach(function (d) { sum += summaryOf(d.id).watts; });
        return sum;
    }

    function onlineCount() {
        return devices().filter(function (d) { return summaryOf(d.id).online; }).length;
    }

    function deviceName(d) {
        if (d.name && d.name !== d.id) return d.name;
        // primary 的 name 允许为空（服务端就只有 ble: 节，没有名字字段）：
        // 这时候绝不能把内部 id `default` 当名字显示给用户。
        if (d.id === store.PRIMARY) return t('device.primaryName');
        return d.name || d.id;
    }

    // ── 切换条 ──

    //: 单台充电器的功率上限（整机 120W）。功率脊的长度 = 这台当前功率 / 120W。
    //  刻意**不是**"占全家的份额"：份额会随设备数量变化（两台时 60W 就占满一半），
    //  家里多插一台充电器就让同一条读数线缩短一半 —— 那条线就不像刻度了。
    //  整机上限是固定刻度，跨设备、跨设备数都横向可比。
    var MAX_WATTS = 120;

    function maxWatts() {
        var m = Number(opts && opts.maxWatts);
        return m > 0 ? m : MAX_WATTS;
    }

    /** 一条脊的比例：0W 一定给 0（留最小可见宽度会把"离线/空载"画成"正在出力"，
     *  一条脊的长度在这是信息，不能为了好看而撒谎）；有读数才给一点最小可见量。 */
    function gaugeShare(watts, max) {
        if (!max || !(watts > 0)) return 0;
        return Math.min(1, Math.max(0.02, watts / max));
    }

    function portDots(ports) {
        return ports.map(function (p) {
            var on = p.active ? ' is-on' : '';
            var dim = p.enabled === false ? ' is-off-mode' : '';
            var title = p.label + (p.active ? ' ' + Math.round(p.power || 0) + 'W' : '');
            return '<i class="bench-port' + on + dim + '" title="' + esc(title) + '"'
                + ' style="--pc:' + esc(p.color || 'currentColor') + '"></i>';
        }).join('');
    }

    /** 牌堆下方那条"翻牌条"：‹ 进度点 ›。
     *  它就是"这里可以左右切换"的那个提示——不用任何文字：
     *  两端的箭头是**真的箭头**（单击翻一张），中间一圈小点标出共几张、现在第几张。
     *  只有一张卡时整条不生成。 */
    function pagerHtml(n) {
        if (n < 2) return '';
        var dots = '';
        for (var i = 0; i < n; i++) dots += '<i class="bench-dot"></i>';
        return '<div class="bench-pager">'
            + '<button type="button" class="bench-nav prev" data-step="-1" aria-label="'
            + esc(t('device.prev')) + '">'
            + '<svg class="ic" aria-hidden="true"><use href="#i-chevron-left"/></svg></button>'
            + '<span class="bench-dots" aria-hidden="true">' + dots + '</span>'
            + '<button type="button" class="bench-nav next" data-step="1" aria-label="'
            + esc(t('device.next')) + '">'
            + '<svg class="ic" aria-hidden="true"><use href="#i-chevron-right"/></svg></button>'
            + '</div>';
    }

    function renderRail() {
        if (!rail) return;
        var list = devices();
        // 一台设备也要有这张卡：它同时是这台设备的**设置入口**
        // （单击卡片 = 弹出该设备的设置弹窗）。
        if (!list.length) {
            if (!rail.hidden) {
                rail.hidden = true;
                rail.innerHTML = '';
                railSig = '';
                if (mode === 'fleet') exitFleet(true);
            }
            document.body.classList.remove('has-device-bench');
            return;
        }
        rail.hidden = false;
        document.body.classList.add('has-device-bench');

        // 只有一台设备时**没有**"全部"卡：一张卡的"汇总"就是它自己。
        var multi = list.length >= 2;
        var locale = (global.I18N && global.I18N.getLocale) ? global.I18N.getLocale() : '';
        // 文本随数据每秒变，所以整条按"结构签名"重建：只有名单/语言变了才重来，
        // 数字部分每帧只改文本节点（重建 innerHTML 会让正在按的按钮丢掉 :active）
        var sig = locale + '\u0003' + (multi ? 'm' : 's') + '\u0003'
            + list.map(function (d) { return d.id + '\u0001' + deviceName(d); }).join('\u0002');
        if (sig !== railSig) {
            railSig = sig;
            var pager = pagerHtml((multi ? 1 : 0) + list.length);
            // 卡片右端那枚齿轮：整卡是一颗按钮，点它打开的是这台的设置。
            // 齿轮 = 设置，这是唯一不需要解释的图形；整张卡就是它的点击区，
            // 所以这里只是一枚**提示**（pointer-events: none），不用做成按钮。
            var cog = '<svg class="ic bench-cog" aria-hidden="true"><use href="#i-cog"/></svg>';
            // 侧栏里的这副牌只显示栈顶那张（.bench-deck 里的卡全落在同一格，
            // 下层 opacity 0 + hidden）。
            rail.innerHTML = '<div class="bench-stack">'
                + (preview
                    ? '<p class="bench-preview-tag" title="Preview devices: fabricated data,'
                      + ' no requests are sent">PREVIEW</p>'
                    : '')
                + '<div class="bench-deck" role="group" aria-label="'
                + esc(t('device.switcher')) + '">'
                + (multi
                    ? '<button type="button" class="bench-tile bench-all" data-device="' + ALL + '"'
                      + ' aria-label="' + esc(t('device.all')) + '，'
                      + esc(t('device.allHint')) + '">'
                      + '<i class="bench-led" aria-hidden="true"></i>'
                      + '<span class="bench-body">'
                      + '<span class="bench-name">' + esc(t('device.all')) + '</span>'
                      + '<span class="bench-meta"><span class="bench-online"></span></span>'
                      + '</span>'
                      + '<span class="bench-right">'
                      + '<span class="bench-w"><b></b><em>W</em></span>'
                      + '</span>'
                      + cog
                      + '<span class="bench-spine" aria-hidden="true"></span>'
                      + '</button>'
                    : '')
                + list.map(function (d) {
                    return '<button type="button" class="bench-tile" data-device="' + esc(d.id) + '"'
                        + ' aria-label="' + esc(deviceName(d)) + '，'
                        + esc(t('device.cardHint')) + '">'
                        + '<i class="bench-led" aria-hidden="true"></i>'
                        + '<span class="bench-body">'
                        + '<span class="bench-name">' + esc(deviceName(d)) + '</span>'
                        + '<span class="bench-ports" aria-hidden="true"></span>'
                        + '</span>'
                        + '<span class="bench-right">'
                        + '<span class="bench-w"><b></b><em>W</em></span>'
                        + '</span>'
                        + cog
                        + '<span class="bench-spine" aria-hidden="true"></span>'
                        + '</button>';
                }).join('')
                + '</div>'
                + pager
                + '</div>';
            // 端口微点结构只在这里建一次（数量固定 4），数值每帧改 class/style
            var tiles = rail.querySelectorAll('.bench-tile[data-device]');
            for (var i = 0; i < tiles.length; i++) {
                var id = tiles[i].getAttribute('data-device');
                if (id === ALL) continue;
                var host = tiles[i].querySelector('.bench-ports');
                var s2 = summaryOf(id);
                if (host && s2.ports.length) host.innerHTML = portDots(s2.ports);
            }
        }

        var total2 = totalWatts();
        var tiles2 = rail.querySelectorAll('.bench-tile[data-device]');
        for (var j = 0; j < tiles2.length; j++) {
            var tile = tiles2[j];
            var did = tile.getAttribute('data-device');
            var active = (mode === 'fleet') ? (did === ALL) : (did === currentId());
            tile.classList.toggle('active', active);
            tile.setAttribute('aria-current', active ? 'true' : 'false');

            if (did === ALL) {
                tile.querySelector('.bench-w b').textContent = String(Math.round(total2));
                var on = onlineCount();
                tile.querySelector('.bench-online').textContent =
                    t('device.onlineCount', { online: on, total: list.length });
                tile.querySelector('.bench-led').classList.toggle('is-on', on > 0);
                // "全部"这张的刻度是 n × 120W（几台设备的容量加起来）
                tile.querySelector('.bench-spine').style.setProperty(
                    '--share', String(gaugeShare(total2, maxWatts() * Math.max(1, list.length))));
                continue;
            }

            var sum = summaryOf(did);
            tile.querySelector('.bench-w b').textContent = String(Math.round(sum.watts));
            tile.querySelector('.bench-led').classList.toggle('is-on', sum.online);
            tile.querySelector('.bench-spine').style.setProperty('--share', String(gaugeShare(sum.watts, maxWatts())));
            var portEls = tile.querySelectorAll('.bench-port');
            for (var k = 0; k < portEls.length; k++) {
                var p = sum.ports[k];
                if (!p) continue;
                portEls[k].classList.toggle('is-on', !!p.active);
                portEls[k].style.setProperty('--pc', p.color || 'currentColor');
            }
        }

        // 牌堆：每帧按"当前是第几张"重排（层级、可见性、进度点）
        layoutDeck();
    }

    // ── 牌堆：与"充电量限额"那沓卡（phone.js 的 bindLimitGesture / .charge-limit-deck）
    // 同一套机制，不再自己造轮子 ────────────────────────────────────────
    //
    // 机制就三条，全部照搬那副牌：
    //   1. 所有卡片落在同一个 grid 格子里（grid-area: 1/1），栈序由 --depth 表达：
    //      depth 越大越靠下、越窄，只在底边露一条 —— 所以整叠只占**一张卡的高度**
    //      加几条露边，而不是叠成一团互相盖住正文；
    //   2. 翻页 = 轮转栈序（ChargeLimit.flipOrder）+ 被换下那张飞出去；
    //   3. 手势阈值、竖向让行、click 抑制都用 ChargeLimit 里现成的那套
    //      （swipeDecision / SWIPE_MIN_PX），两个牌堆的手感一致。
    //
    // 上一版自己写成了"绝对定位 + 左右错开"，问题正是：卡片并排错开后正面互相
    // 压住（看着像重影），而且只翻得动一次（收尾状态没清干净）。
    var deckFlipBusy = false;     // 翻页动画期间忽略新手势
    var deckFlipAt = 0;           // 翻页开始时刻（忙标记的自动失效兜底）
    var deckDrag = null;          // 正在拖牌
    var deckSuppressClick = false;// 刚划过牌：吃掉紧随其后的 click
    var deckOrder = [];           // [0] = 栈顶，其余按 depth 依次压在下面
    var wheelAcc = 0;
    var lastStepAt = 0;
    var WHEEL_STEP = 26;
    var STEP_COOLDOWN_MS = 220;
    var BUSY_TTL_MS = 800;        // 忙标记最长存活时间

    /** 是否该忽略这次手势。
     *  翻牌收尾靠 setTimeout(320)，但后台标签页里定时器会被节流、页面被系统冻过之后
     *  回调也可能干脆不来了——标记一旦挂住，整副牌就永久没反应（"能滑一次后面就
     *  动不了了"正是这么发生的）。所以忙标记带一个寿命：超时自动放开。 */
    function deckBusy() {
        if (deckFlipBusy && Date.now() - deckFlipAt > BUSY_TTL_MS) deckFlipBusy = false;
        return deckFlipBusy;
    }

    function deckTiles() {
        return rail ? rail.querySelectorAll('.bench-tile[data-device]') : [];
    }

    function tileFor(id) {
        var tiles = deckTiles();
        for (var i = 0; i < tiles.length; i++) {
            if (tiles[i].getAttribute('data-device') === id) return tiles[i];
        }
        return null;
    }

    function rotate(list, steps) {
        if (global.ChargeLimit && global.ChargeLimit.flipOrder) {
            return global.ChargeLimit.flipOrder(list, steps);
        }
        var n = list.length;
        if (n < 2) return list.slice();
        var k = ((steps % n) + n) % n;
        return k ? list.slice(k).concat(list.slice(0, k)) : list.slice();
    }

    function swipeDecision(dx, width) {
        if (global.ChargeLimit && global.ChargeLimit.swipeDecision) {
            return global.ChargeLimit.swipeDecision(dx, width);
        }
        var need = Math.max(46, (width || 320) * 0.22);
        if (dx <= -need) return 1;
        if (dx >= need) return -1;
        return 0;
    }

    /** 牌面全序：多台时先"全部"，再按设备清单顺序；只有一台时没有"全部"这一面。 */
    function deckIds() {
        var list = devices();
        var ids = (list.length >= 2) ? [ALL] : [];
        for (var i = 0; i < list.length; i++) ids.push(list[i].id);
        return ids;
    }

    /** 把栈序对齐到现实：名单没变就保持当前轮转；名单变了就按固定名单重建
     *  （再让"当前那台"翻到顶上）。
     *  注意不能"把新出现的追加到末尾"：从单设备变成两台时，"全部"会被追加到
     *  当前设备**后面**，牌序就成了 [设备, 全部, …]，翻牌顺序跟着乱。 */
    function syncDeckOrder() {
        var ids = deckIds();
        var same = (deckOrder.length === ids.length);
        for (var i = 0; same && i < ids.length; i++) {
            if (deckOrder.indexOf(ids[i]) < 0) same = false;
        }
        var next = same ? deckOrder.slice() : ids.slice();
        var want = (mode === 'fleet') ? ALL : currentId();
        var at = next.indexOf(want);
        if (at > 0) next = rotate(next, at);
        deckOrder = next;
    }

    function deckHost() {
        return rail ? rail.querySelector('.bench-deck') : null;
    }

    /** 翻牌/拖动期间把"下一张"放出来：栈顶那张渐隐或飞走时，底下不该是空白。
     *  收尾（或指针被打断）时再关上，回到"只有一张卡"。 */
    function setShifting(on) {
        var host = deckHost();
        if (host && host.classList) host.classList.toggle('is-shifting', !!on);
    }

    /** 每帧重排：层级、谁在栈顶（"栈顶"= 唯一可见的那张）、进度点。
     *  站位/宽度不再由 --depth 决定（卡片是一张一张显示的），--depth 只写层级
     *  与可读性：它是"这张在牌堆里第几层"，也是测试与调试唯一需要的那个数。 */
    function layoutDeck() {
        var tiles = deckTiles();
        if (!tiles.length) return;
        syncDeckOrder();
        var ids = deckIds();
        var activeAt = ids.indexOf((mode === 'fleet') ? ALL : currentId());
        for (var i = 0; i < tiles.length; i++) {
            var el = tiles[i];
            var depth = deckOrder.indexOf(el.getAttribute('data-device'));
            if (depth < 0) depth = deckOrder.length;
            el.style.setProperty('--depth', String(depth));
            el.style.zIndex = String(20 - depth);
            el.classList.toggle('is-top', depth === 0);
            el.tabIndex = depth === 0 ? 0 : -1;
            // 下层卡片看不见，也就不该被读屏念到、不该进 Tab 序列
            el.setAttribute('aria-hidden', depth === 0 ? 'false' : 'true');
        }
        // 翻牌条：亮的那颗点 = 现在停在第几张（点的顺序是固定名单，不跟着轮转）
        var dotEls = rail.querySelectorAll('.bench-pager .bench-dot');
        for (var k = 0; k < dotEls.length; k++) {
            dotEls[k].classList.toggle('is-on', k === activeAt);
        }
        // 只有一张卡：没有可翻的方向，箭头藏起来（只剩一颗点或什么都没有）
        var navs = rail.querySelectorAll('.bench-nav');
        for (var j = 0; j < navs.length; j++) {
            navs[j].hidden = deckOrder.length < 2;
        }
    }

    /** 翻 steps 格（正数向后）。outX：被换下那张飞出的方向（-1 左 / +1 右）。 */
    function flipDeck(steps, outX) {
        if (deckBusy() || deckOrder.length < 2 || !steps) return false;
        var top = tileFor(deckOrder[0]);
        var target = rotate(deckOrder, steps)[0];
        deckFlipBusy = true;
        deckFlipAt = Date.now();
        setShifting(true);          // 飞走的那张要一直看得见，直到落位
        if (top) {
            top.style.transition = '';                    // 恢复样式表里的过渡
            top.style.zIndex = '30';                      // 飞出去时盖在"下一张"之上
            // 只平移、**不渐隐**：卡片一旦半透明，它和新卡片上的功率数字会在
            // 同一块地方叠着显示（用户看到的就是"数字重影"）。实心卡片滑出去，
            // 下一张从它下面露出来，这才是"翻牌"该有的样子。
            top.style.transform = 'translate(' + (outX * 118) + '%, 0)';
        }
        // 指针先走：store.renderSwitcher → renderRail → layoutDeck 会重排栈序
        if (target === ALL) {
            if (mode !== 'fleet') enterFleet();
        } else {
            if (mode === 'fleet') exitFleet();
            if (target !== store.current()) store.switchDevice(target);
        }
        layoutDeck();
        if (global.setTimeout) {
            global.setTimeout(function () {
                if (top) {
                    // 无声地落回牌堆末位：先关过渡再改位，否则看得见它飞回来
                    top.style.transition = 'none';
                    top.style.transform = '';
                    top.style.zIndex = '';
                    void top.offsetWidth;
                    top.style.transition = '';
                }
                deckFlipBusy = false;
                setShifting(false);
            }, 320);
        } else {
            deckFlipBusy = false;
            setShifting(false);
        }
        return true;
    }

    // ── 手势 ──

    function onDeckPointerDown(e) {
        // 上一次拖拽没收到 pointerup（手指划出窗口 / 指针被别的元素抢走 / 系统打断）
        // 时，deckDrag 会一直挂着，之后每次 pointerdown 都被当成"还没结束"——
        // 同样会造成"能滑一次后面就动不了"。挂太久就自己放开。
        if (deckDrag && Date.now() - (deckDrag.t0 || 0) > BUSY_TTL_MS) deckDrag = null;
        if (deckBusy() || deckDrag) return;
        // 只有一张卡：没有"下一张"可翻，就别把卡片拖起来（拖了也没用，只会晃）
        if (deckOrder.length < 2) return;
        deckSuppressClick = false;
        var tag = e.target && e.target.tagName ? String(e.target.tagName).toUpperCase() : '';
        if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
        // 点在翻牌箭头上：那是"翻一张"的按钮，不是要拖牌
        if (e.target && e.target.closest && e.target.closest('.bench-nav')) return;
        var el = tileFor(deckOrder[0]);
        if (!el) return;
        deckDrag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, dx: 0, moved: false, el: el, t0: Date.now() };
    }

    function onDeckPointerMove(e) {
        var d = deckDrag;
        if (!d || e.pointerId !== d.id) return;
        var dx = e.clientX - d.x0;
        var dy = e.clientY - (d.y0 === undefined ? e.clientY : d.y0);
        if (!d.moved) {
            if (Math.abs(dx) < 8) return;
            // 纵向为主的手势交还给页面滚动（touch-action: pan-y 已放行纵向）
            if (Math.abs(dx) <= Math.abs(dy)) { deckDrag = null; return; }
            d.moved = true;
            setShifting(true);        // 底下的"下一张"要露出来（否则拖开是一片空）
            if (d.el.style) d.el.style.transition = 'none';   // 跟手期间不能有过渡
            var host = rail.querySelector('.bench-deck');
            if (host && host.setPointerCapture) {
                try { host.setPointerCapture(e.pointerId); } catch (err) { /* 指针已失效 */ }
            }
        }
        d.dx = dx;
        // 跟手只动位置，不动透明度：半透明会让两张卡的功率数字在同一位置
        // 叠着显示（重影）。下一张本来就压在下面，卡片挪开就露出来了。
        if (d.el.style) d.el.style.transform = 'translate(' + dx + 'px, 0)';
    }

    function onDeckPointerUp(e) {
        var d = deckDrag;
        if (!d || (e.pointerId !== undefined && e.pointerId !== d.id)) return;
        deckDrag = null;
        if (!d.moved) return;                 // 只是点了一下，交给 click
        // 抖动不等于划牌：位移明显时才吃掉紧随其后的 click
        if (Math.abs(d.dx) > 20) {
            deckSuppressClick = true;
            // 兜底：万一没有 click 跟上来，也不会一直吞掉后面的点击
            if (global.setTimeout) global.setTimeout(function () { deckSuppressClick = false; }, 400);
        }
        if (d.el.style) {
            d.el.style.transition = '';
        }
        var cancelled = !!(e && e.type === 'pointercancel');
        var dir = cancelled ? 0 : swipeDecision(d.dx, Number(d.el.offsetWidth) || 320);
        if (!dir) {                            // 没过阈值：回弹
            if (d.el.style) d.el.style.transform = '';
            setShifting(false);
            return;
        }
        // 只有一张卡时翻不动（没有"下一张"）：卡片必须放回原位，
        // 否则它会停在拖出去的位置、还带着半透明。
        if (!flipDeck(dir, dir > 0 ? -1 : 1)) {
            if (d.el.style) d.el.style.transform = '';   // 只有一张卡：放回原位
            setShifting(false);
        }
    }

    /** 指针被系统或别的元素收走（lostpointercapture、window 失焦）：
     *  把卡片放回原位，别让跟手时的位移留在栈顶上。 */
    function onDeckPointerAbort(e) {
        var d = deckDrag;
        if (!d || (e && e.pointerId !== undefined && e.pointerId !== d.id)) return;
        deckDrag = null;
        setShifting(false);
        if (d.el && d.el.style) {
            d.el.style.transition = '';
            d.el.style.transform = '';
        }
    }

    function onDeckWheel(e) {
        if (deckBusy()) return;
        // 只有一张卡：滚轮留给页面滚动，别吃掉（没有可翻的目标）
        if (deckOrder.length < 2) return;
        var d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        if (!d) return;
        if (e.preventDefault) e.preventDefault();   // 在牌堆上滚 = 翻牌
        wheelAcc += d;
        if (Math.abs(wheelAcc) < WHEEL_STEP) return;
        var dir = wheelAcc > 0 ? 1 : -1;
        wheelAcc = 0;
        var now = Date.now();
        if (now - lastStepAt < STEP_COOLDOWN_MS) return;
        if (flipDeck(dir, dir > 0 ? -1 : 1)) lastStepAt = now;
    }

    function onDeckKey(e) {
        if (!e || !e.key) return;
        var dir = 0;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') dir = 1;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') dir = -1;
        else return;
        if (deckOrder.length < 2) return;       // 只有一张卡：方向键留给页面
        if (e.preventDefault) e.preventDefault();
        flipDeck(dir, dir > 0 ? -1 : 1);
    }


    // ── 全部设备（汇总）──

    function energyOf(id) {
        var e = energy[id];
        return (e && e.wh !== undefined) ? e.wh : null;
    }

    function fetchEnergy() {
        if (preview || !opts.fetchEnergy) return;   // 预览设备服务端不存在，不去问

        var now = Date.now();
        devices().forEach(function (d) {
            var e = energy[d.id];
            if (e && (e.pending || now - e.at < ENERGY_TTL_MS)) return;
            energy[d.id] = { at: now, pending: true, wh: e ? e.wh : undefined };
            opts.fetchEnergy(d.id, function (wh) {
                energy[d.id] = { at: Date.now(), pending: false, wh: wh };
                if (mode === 'fleet') renderFleet(true);
            });
        });
    }

    function renderFleet(force) {
        if (!fleetHost || mode !== 'fleet') return;
        var list = devices();
        if (list.length < 2) return;

        var rows = list.map(function (d, index) {
            return { d: d, index: index, sum: summaryOf(d.id) };
        });
        // 汇总页按实时功率排序：这里"顺序"承载信息（谁在干活），所以允许变动；
        // 功率相同则回到注册顺序，避免每次 tick 抖来抖去。
        rows.sort(function (a, b) {
            return (b.sum.watts - a.sum.watts) || (a.index - b.index);
        });

        var total = totalWatts();
        var on = onlineCount();
        var today = 0, hasToday = false;
        list.forEach(function (d) {
            var wh = energyOf(d.id);
            if (wh !== null && wh !== undefined) { today += wh; hasToday = true; }
        });

        var sig = rows.map(function (r) { return r.d.id; }).join('\u0001')
            + '\u0004' + ((global.I18N && global.I18N.getLocale) ? global.I18N.getLocale() : '');
        if (force || sig !== fleetSig) {
            fleetSig = sig;
            fleetHost.innerHTML = '<div class="wrap fleet-wrap">'
                + '<div class="fleet-head">'
                + '<h2 class="fleet-title">' + esc(t('device.fleetTitle')) + '</h2>'
                + '<p class="fleet-totals">'
                + '<span>' + esc(t('device.fleetNow')) + ' <b>' + Math.round(total) + '</b> W</span>'
                + '<span>' + esc(t('device.fleetOnline')) + ' <b>' + on + '/' + list.length + '</b></span>'
                + (hasToday ? '<span>' + esc(t('device.fleetToday')) + ' <b>' + Math.round(today) + '</b> Wh</span>' : '')
                + '</p>'
                + '</div>'
                + '<ul class="fleet-list">'
                + rows.map(function (r) {
                    var sum = r.sum;
                    return '<li class="fleet-row" data-device="' + esc(r.d.id) + '" tabindex="0" role="button">'
                        + '<i class="fleet-led' + (sum.online ? ' is-on' : '') + '" aria-hidden="true"></i>'
                        + '<span class="fleet-id">'
                        + '<b class="fleet-name">' + esc(deviceName(r.d)) + '</b>'
                        + '<span class="fleet-mac">' + esc(r.d.mac || '') + '</span>'
                        + '</span>'
                        + '<span class="fleet-dots" aria-hidden="true"></span>'
                        + '<span class="fleet-nums"><b class="fleet-w">' + Math.round(sum.watts) + '</b><em>W</em>'
                        + '<span class="fleet-today"></span></span>'
                        + '</li>';
                }).join('')
                + '</ul></div>';

            var rowEls = fleetHost.querySelectorAll('.fleet-row');
            for (var i = 0; i < rowEls.length; i++) {
                var did = rowEls[i].getAttribute('data-device');
                var sum2 = summaryOf(did);
                var dotsHost = rowEls[i].querySelector('.fleet-dots');
                if (dotsHost) {
                    dotsHost.innerHTML = sum2.ports.map(function (p) {
                        var on2 = p.active ? ' is-on' : '';
                        var off = p.enabled === false ? ' is-off-mode' : '';
                        return '<i class="fleet-port' + on2 + off + '" style="--pc:' + esc(p.color || 'currentColor')
                            + '" title="' + esc(p.label + (p.active ? ' ' + Math.round(p.power || 0) + 'W' : '')) + '"></i>';
                    }).join('');
                }
            }
        }

        // 数值每帧刷新（不重建 DOM）
        var rowEls2 = fleetHost.querySelectorAll('.fleet-row');
        for (var j = 0; j < rowEls2.length; j++) {
            var id = rowEls2[j].getAttribute('data-device');
            var s = summaryOf(id);
            rowEls2[j].querySelector('.fleet-w').textContent = String(Math.round(s.watts));
            var wh = energyOf(id);
            rowEls2[j].querySelector('.fleet-today').textContent =
                wh === null || wh === undefined ? '' : t('device.todayWh', { wh: Math.round(wh) });
        }
        var totalsEl = fleetHost.querySelectorAll('.fleet-totals b');
        if (totalsEl[0]) totalsEl[0].textContent = String(Math.round(total));
        if (totalsEl[1]) totalsEl[1].textContent = on + '/' + devices().length;
        if (totalsEl[2] && hasToday) totalsEl[2].textContent = String(Math.round(today));
    }

    // ── 模式切换 ──

    function enterFleet() {
        if (mode === 'fleet') return;
        mode = 'fleet';
        document.body.classList.add('fleet-mode');
        if (fleetHost) fleetHost.hidden = false;
        fetchEnergy();
        renderFleet(true);
        renderRail();
        if (opts.onMode) { try { opts.onMode(true); } catch (e) {} }
    }

    function exitFleet(silent) {
        if (mode !== 'fleet') return;
        mode = 'device';
        document.body.classList.remove('fleet-mode');
        if (fleetHost) fleetHost.hidden = true;
        renderRail();
        if (!silent && opts.onMode) { try { opts.onMode(false); } catch (e) {} }
    }

    /** 打开某台设备的设置弹窗。
     *  右栏原来那张"设备设置"卡片已经去掉（它什么也没显示，只是一颗按钮），
     *  入口改成**单击切换条上的卡片**：卡片是谁，弹出来的就是谁的设置
     *  （设置项跟着设备的桶走，切换设备 = 换一份设置）。 */
    function openSettings(id) {
        if (id === ALL) { enterFleet(); return; }
        if (mode === 'fleet') exitFleet();
        if (id !== store.current()) store.switchDevice(id);
        if (opts.onPick) { try { opts.onPick(id); } catch (e) {} }
        var name = '';
        var list = devices();
        for (var i = 0; i < list.length; i++) {
            if (list[i].id === id) { name = deviceName(list[i]); break; }
        }
        if (global.openSettingsModal) {
            try { global.openSettingsModal(name); } catch (e) { console.error('open settings failed:', e); }
        }
    }

    /**
     * 选一台设备（汇总清单里点某行）。
     */
    function selectDevice(id) {
        if (id === ALL) { enterFleet(); return; }
        if (mode === 'fleet') exitFleet();
        // 预览设备也是真的桶、真的指针：切过去整页跟着换（请求由 store 拦掉）
        if (id !== store.current()) store.switchDevice(id);
        if (opts.onPick) { try { opts.onPick(id); } catch (e) {} }
    }

    /** 翻牌箭头：左 = 上一张（向后翻），右 = 下一张。被换下那张从反方向飞走。 */
    function onDeckNav(e) {
        var btn = e.target && e.target.closest ? e.target.closest('.bench-nav') : null;
        if (!btn) return;
        var step = Number(btn.getAttribute('data-step')) || 1;
        flipDeck(step, step > 0 ? -1 : 1);
    }

    function onRailClick(e) {
        if (deckSuppressClick) {      // 划牌结束浏览器补的那一下 click
            deckSuppressClick = false;
            return;
        }
        var tile = e.target && e.target.closest ? e.target.closest('.bench-tile') : null;
        if (!tile) return;
        var id = tile.getAttribute('data-device');
        // 这副牌只显示栈顶一张（= 当前设备），单击它 = 打开这台的设置。
        openSettings(id);
    }

    function onFleetClick(e) {
        var row = e.target && e.target.closest ? e.target.closest('.fleet-row') : null;
        if (!row) return;
        selectDevice(row.getAttribute('data-device'));
    }

    function onFleetKey(e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        var row = e.target && e.target.closest ? e.target.closest('.fleet-row') : null;
        if (!row) return;
        e.preventDefault();
        selectDevice(row.getAttribute('data-device'));
    }

    // ── 生命周期 ──

    function tick() {
        seedPreview();          // 预览数据要跟着动：手机上 bench 没有牌堆，但仍然要灌
        var visible = (rail && !rail.hidden) || mode === 'fleet';
        if (!visible) return;
        renderRail();
        if (mode === 'fleet') renderFleet(false);
    }

    function start() {
        if (timer) return;
        timer = global.setInterval(tick, TICK_MS);
    }

    /**
     * @param {object} o
     *   railId        牌堆容器 id（不传/找不到就只跑"全部设备"页）
     *   fleetId       汇总页容器 id（可选）
     *   summarise(b)  由页面提供：把一桶状态折成 {online, watts, ports:[{key,label,active,power,color,enabled}]}
     *   fetchEnergy(id, cb) 可选：取该设备今日用电（Wh），失败回调 null
     *   onMode(on)    进入/退出汇总页（页面据此暂停单设备渲染即可）
     *   onPick(id)    用户明确选了某台设备（页面可据此重放/拉数据）
     */
    function init(o) {
        opts = o || {};
        store = global.DeviceStore;
        if (!store || !global.document) return;
        if (opts.preview) {
            preview = true;
            if (store.setPreviewIds) store.setPreviewIds(fakeDevices().map(function (d) { return d.id; }));
        }
        rail = global.document.getElementById(opts.railId || 'deviceSwitcher');
        fleetHost = opts.fleetId ? global.document.getElementById(opts.fleetId) : null;
        if (fleetHost) { fleetHost.hidden = true; fleetHost.classList.add('fleet-view'); }

        if (rail && rail.addEventListener) {
            rail.addEventListener('click', onRailClick);
            rail.addEventListener('click', onDeckNav);
            // 翻牌：拖动 / 滚轮 / 方向键 / 下面的箭头
            rail.addEventListener('pointerdown', onDeckPointerDown);
            rail.addEventListener('pointermove', onDeckPointerMove);
            rail.addEventListener('pointerup', onDeckPointerUp);
            rail.addEventListener('pointercancel', onDeckPointerUp);
            rail.addEventListener('lostpointercapture', onDeckPointerAbort);
            rail.addEventListener('wheel', onDeckWheel, { passive: false });
            rail.addEventListener('keydown', onDeckKey);
        }
        if (fleetHost && fleetHost.addEventListener) {
            fleetHost.addEventListener('click', onFleetClick);
            fleetHost.addEventListener('keydown', onFleetKey);
        }
        // 切换条的长相交给 bench：device-store 保留"设备清单变了"这个信号
        // （setDevices → renderSwitcher），但不再自己画 pills —— 否则两边都写
        // rail 的 innerHTML/hidden，设备删到只剩一台时旧 pills 会把收起状态覆盖掉。
        if (rail && store.setSwitcherRenderer) store.setSwitcherRenderer(renderRail);

        // store 的指针变化（外部 switchDevice、URL 回退）也要重画选中态
        store.onChange(function () {
            if (store.devices().length) probeState = 'done';   // 清单到了：不必再问 /api/devices
            renderRail();
            if (mode === 'fleet') renderFleet(false);
        });
        if (global.I18N && global.I18N.onChange) {
            global.I18N.onChange(function () {
                railSig = '';      // 语言变了：名字与 aria 文案都要重画
                fleetSig = '';
                renderRail();
                if (mode === 'fleet') renderFleet(true);
            });
        }
        seedPreview();
        renderRail();
        scheduleProbe();
        // 刻意**不**在这里挪指针：预览只加卡片，不改变"你打开页面看到哪台"。
        // 设备清单是 SSE init 之后才到的，这时候强行切到伪造设备会在真实设备
        // 到达时再被 store 拦回来（is_primary 回退），屏幕上就会出现一次闪切。
        start();
    }

    /** 单设备时 SSE 里没有 devices（§3.8 的红线），但那张卡片要显示设备名，
     *  所以清单为空时向只读端点 /api/devices 问一次——它不走事件流，
     *  不改变单设备页面的任何既有响应。问回来就交给 store，后面全按真清单走。
     *
     *  推迟一点再问：SSE init 比脚本初始化晚到（几百毫秒），当场问的话多设备页面
     *  也会白问一次。等待期间清单一到就取消。 */
    var PROBE_DELAY_MS = 1500;
    var probeState = '';             // '' 未安排 / 'pending' 等 SSE 落地 / 'done'
    function probeDevices() {
        if (probeState === 'done' || !store || !global.fetch) return;
        if (store.devices().length) { probeState = 'done'; return; }   // 清单已到，不必问
        probeState = 'done';
        var p = global.fetch('/api/devices');
        if (!p || !p.then) return;
        p.then(function (r) { return (r && r.ok) ? r.json() : null; })
            .then(function (data) {
                if (data && data.devices && data.devices.length && store.setDevices) {
                    store.setDevices(data.devices);
                }
            })
            .catch(function () { /* 拿不到就继续用兜底那条（名字退化成"默认充电器"） */ });
    }

    function scheduleProbe() {
        if (probeState || !store || !global.fetch) return;
        if (store.devices().length) return;      // SSE 已经带了清单（多设备）
        probeState = 'pending';
        if (global.setTimeout) global.setTimeout(probeDevices, PROBE_DELAY_MS);
        else probeDevices();
    }

    function refresh() { railSig = ''; fleetSig = ''; renderRail(); if (mode === 'fleet') renderFleet(true); }

    global.DeviceBench = {
        ALL: ALL,
        init: init,
        refresh: refresh,
        // 有效设备清单（含 ?preview=multi 的伪造设备、单设备时的兜底一条）：
        // 手机上切换条不在 bench 手里（标题栏下拉列表由 phone.js 渲染），
        // 但清单必须和 bench 用的是同一份，否则"预览设备"只在桌面看得见。
        devices: function () { return devices(); },
        isFleet: function () { return mode === 'fleet'; },
        enterFleet: enterFleet,
        exitFleet: exitFleet
    };
})(typeof window !== 'undefined' ? window : this);
