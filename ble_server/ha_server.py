"""CUKTECH BLE data server for Home Assistant integration.

BLE data is published to MQTT for real-time updates in Home Assistant.
"""
import asyncio
import contextlib
import re
import sys
import warnings
warnings.filterwarnings('ignore', message='.*default MTU.*')
import gzip
import hashlib
import json
import logging
import logging.handlers
import math
import os
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from aiohttp import web

from config import (
    load_config, config_from_mapping, LOG_LEVELS, PRIMARY_DEVICE_ID, DeviceConfig,
    TopicNamespace, BEMFA_PORTS, BEMFA_PORT_LABELS, BEMFA_DEFAULT_NAMES,
    bemfa_entity_triple,
)
from state import ChargerState, PORT_BITS, PORT_NAMES, PORT_DEFAULT, VALID_PIIDS, PIID_RANGES
from ble_manager import (
    BLEManager, set_status_cache_invalidator, END_REASON_SHUTDOWN, END_REASON_LINK_LOSS,
)
from adapter_coordinator import AdapterCoordinator
from energy import MAX_LIMIT_WH, LIMIT_MODES
from history import PortHistory, DeviceHistory
from log_utils import install_log_filter
from bemfa_client import BemfaClient, MSG_ON, MSG_OFF


def _load_xiaomi_cloud():
    """按需导入小米云扫码登录客户端（延迟导入）。

    该模块只被 3 个低频端点使用（/api/xiaomi/login、/qr/complete、/beaconkey），
    却会连带拉进 requests + pycryptodome —— 实测在真实进程里常驻约 6.3 MB。
    原先放在模块顶层急切导入，等于为一个几乎不用的功能长期占内存；
    改为首次调用时才导入（结果由 sys.modules 缓存，代价只付一次，首次请求略慢）。

    返回 (QrCodeXiaomiCloudClient, XiaomiCloudLoginError)；依赖缺失时抛
    ImportError，由调用方转成明确的接口错误。
    """
    from xiaomi_cloud import QrCodeXiaomiCloudClient, XiaomiCloudLoginError
    return QrCodeXiaomiCloudClient, XiaomiCloudLoginError


# 日志文件路径：由 cuktech_ctl.sh 通过 CUKTECH_LOG_FILE 传入（默认与脚本一致），
# 两边必须指向同一个文件，`cuktech_ctl.sh log` 才读得到。
LOG_FILE = os.environ.get("CUKTECH_LOG_FILE", "/tmp/cuktech_server.log")
LOG_MAX_BYTES = int(os.environ.get("CUKTECH_LOG_MAX_BYTES", 20 * 1024 * 1024))
LOG_BACKUP_COUNT = int(os.environ.get("CUKTECH_LOG_BACKUP_COUNT", 5))


def _setup_logging() -> None:
    """根 logger：按大小轮转的文件处理器 + 控制台。

    文件处理器是主日志（cuktech_ctl.sh log 读的就是它），按 LOG_MAX_BYTES 切割、
    保留 LOG_BACKUP_COUNT 份，避免长期运行无限增长（info 级别下约 9 MB/天）。
    控制台输出由 ctl 重定向到 *.console.log，用于保留"日志系统就绪前"的启动期
    异常（导入错误、解释器告警等）。
    """
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(device)s%(message)s",
                            "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        try:
            fh = logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception as e:      # 路径不可写等 → 退回仅控制台输出
            logging.getLogger("cuktech_server").warning(
                "File log handler disabled (%s): %s", LOG_FILE, e)
    # 仅交互式前台运行时才把日志打到终端。被 cuktech_ctl.sh 重定向到文件时若再加
    # 一个 StreamHandler，会和主日志内容完全重复（实测两文件行数相同），白白翻倍。
    if sys.stderr.isatty() and not any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    # 格式串里有 `%(device)s` 后，任何没带该属性的 record 都会被 Formatter 抛
    # ValueError 并被 handler 吞掉 —— 日志行直接消失。过滤器把缺的属性补成空串
    # （单设备输出逐字节不变），必须挂在 root 与每个 handler 上，见 log_utils。
    install_log_filter()


_setup_logging()
_LOGGER = logging.getLogger("cuktech_server")

#: 界面语言是"跨页面的即时偏好"，一旦被浏览器缓存住，用户改了语言也会继续看到
#: 旧的（真实踩过：页面一半中文一半英文，或者整页英文但设置里写着中文）。
_NO_STORE = {"Cache-Control": "no-store, max-age=0"}


_sse_log = logging.getLogger("cuktech_sse")


# ── Request size limit (prevent DoS via large payloads) ──
MAX_REQUEST_BODY_SIZE = 1024 * 1024  # 1 MB

# ── MQTT 待确认队列上限 ──
# paho 对 QoS>0 的待确认消息默认 max_queued_messages=0（无上限）：连接"看起来还在"
# 但 broker 不再回 PUBACK（半开 TCP、NAT 超时、broker 卡住）时，1Hz 的状态发布会
# 一直堆进 _out_messages。实测每条约 1.2KB → 几小时就能吃掉上百 MB。低频状态主题
# 已改用 QoS0（QoS0 不入队），这里再给低频的 QoS1 路径一个硬上限兜底。
MQTT_MAX_QUEUED_MESSAGES = 200
MQTT_MAX_INFLIGHT_MESSAGES = 20

# ── 图表缓存的内存保护 ──
# 缓存里存的是**整段 JSON body**：720h@30s 实测 6.6MB，而缓存按"条数"上限 10 条，
# 最坏情况能吃掉几十 MB。这里改成"条数 + 总字节"双重封顶，并给长区间强制更粗的桶。
CHART_CACHE_MAX_BYTES = 8 * 1024 * 1024      # 缓存 body 总量上限
CHART_CACHE_MAX_ENTRY_BYTES = 1024 * 1024    # 单条 body 超过它就不进缓存
# hours 超过阈值 → interval 下限（UI 自身最长 24h@300s，不受影响）
_CHART_INTERVAL_FLOORS = ((168, 1800), (72, 600), (24, 300))


def _chart_interval_floor(hours: float) -> int:
    """长区间的桶宽下限：避免构造型查询生成数 MB 的 body。"""
    for threshold, floor in _CHART_INTERVAL_FLOORS:
        if hours > threshold:
            return floor
    return 5      # ≤24h：沿用请求值（最小 5s）


def _chart_cache_bytes(cache) -> int:
    """缓存里所有 body 的字节总量（条数很少，直接累加即可）。"""
    total = 0
    for entry in cache.values():
        try:
            total += len(entry[2])
        except (TypeError, IndexError):
            continue
    return total


def _bemfa_entity_signature(raw) -> list:
    """`bemfa.entities` 的规范化指纹：排序后的 (设备, 端口, 名称) 三元组。

    用来判断"这次保存有没有动过语音实体"。直接比原始列表不行：前端/手写 YAML 的
    字段顺序、缺省 device 写法都会让内容完全相同的两份配置看起来不同 —— 那会让
    每次保存都打上 `modified`，于是每次重启都把巴法云主题删了重建一遍。
    """
    return sorted((device, port, name)
                  for name, device, port in
                  (bemfa_entity_triple(e) for e in (raw or [])))


class SSEEmitter:
    """SSE event broadcaster — push events to all connected browser clients."""

    MAX_QUEUE_SIZE = 128

    def __init__(self):
        self._clients: set[asyncio.Queue] = set()
        self._pending_status: dict[int, str] = {}  # id(queue) -> latest status payload
        self._lock = threading.Lock()

    def add_client(self, queue: asyncio.Queue):
        with self._lock:
            self._clients.add(queue)
        _sse_log.info("SSE client connected (total: %d)", len(self._clients))

    def remove_client(self, queue: asyncio.Queue):
        with self._lock:
            self._clients.discard(queue)
            # 多设备时同一个 queue 有 N 个 (qid, device_id) 分桶，全部清掉，
            # 否则断开的客户端会在 _pending_status 里留下永久"排水沟"。
            for key in [k for k in self._pending_status if k[0] == id(queue)]:
                self._pending_status.pop(key, None)
        _sse_log.info("SSE client disconnected (total: %d)", len(self._clients))

    def _put_or_drop(self, q: asyncio.Queue, payload: str):
        """Put payload in queue; drop oldest if full."""
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            _sse_log.warning("SSE queue full, dropping oldest event (qsize=%s)", q.qsize())
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def emit(self, event_type: str, data: dict, device_id: str | None = None):
        """Broadcast event to all connected clients.

        Status events are kept as 'latest only' — stale statuses are replaced,
        never enqueued alongside newer statuses.

        `device_id` 只用于 **latest-only 分桶**（§3.8/§3.2）：多设备时每台设备的
        status 各自保留最新一条，互不覆盖；单设备（device_id=None）时 `_pending_status`
        的键退化成 `(id(q), None)`，与改造前的 `id(q)` 一一对应、行为不变。
        事件体**不**在这里加任何字段——payload 里的 `device` 由 `DeviceSSEEmitter`
        决定加不加（multi 才加），这样单设备字节完全不受影响。
        """
        payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
        is_status = event_type == "status"
        with self._lock:
            for q in self._clients:
                key = (id(q), device_id)
                if is_status:
                    # Status: just update pending, don't enqueue (replaces stale status)
                    self._pending_status[key] = payload
                else:
                    # Non-status: flush pending status first, then enqueue
                    status = self._pending_status.pop(key, None)
                    if status:
                        self._put_or_drop(q, status)
                    self._put_or_drop(q, payload)

    def flush_status(self, q: asyncio.Queue, device_id: str | None = None) -> str | None:
        """Return and clear the latest pending status for a queue (if any).
        Called by SSE handler before each queue read to ensure the newest status
        is sent ahead of all other events.
        """
        with self._lock:
            return self._pending_status.pop((id(q), device_id), None)


# ─────────────────────────────────────────────────────────────────────────────
# 多设备内核（设计文档 §3.2 / §3.3 / §3.13）
#
# 一句话：`Server` 从"持有一台设备"变成"持有 registry"，而 `self.ble`/`self.state`/
# `self.history` 变成**只读 property**——默认 primary，按 per-request ContextVar 解析。
# 绝不做成可变赋值：两个并发请求在 await 处交错会把命令发给错误的充电器。
# ─────────────────────────────────────────────────────────────────────────────

# 当前请求所属设备。只由 `device_middleware` 设置；**禁止**把它当成"当前设备"的
# 通用通道——设备自己触发的后台回调（定时器/quality/MQTT 命令）没有请求上下文，
# 经它解析会静默落到 primary（见 §3.3 五条红线）。
_current_rt: ContextVar = ContextVar("cuktech_current_rt", default=None)


@dataclass
class DeviceRuntime:
    """一台设备的全部运行期资源（§3.2）。"""

    device_id: str                       # primary 固定 "default"
    mac: str
    token: str
    name: str
    topics: "TopicNamespace"
    state: ChargerState
    manager: BLEManager
    history: "DeviceHistory"
    spec: "DeviceConfig | None" = None
    supervisor: "RuntimeSupervisor | None" = None
    task: asyncio.Task | None = None     # 仅由 supervisor 创建/持有

    @property
    def is_primary(self) -> bool:
        return self.device_id == PRIMARY_DEVICE_ID

    def describe(self) -> dict:
        """`/api/devices` 的单项（§3.7）。"""
        return {
            "id": self.device_id,
            "name": self.name or self.device_id,
            "mac": self.mac,
            "connected": bool(self.state.connected),
            "topic_prefix": self.topics.prefix,
            "is_primary": self.is_primary,
        }


class DeviceRegistry:
    """有序设备表（primary 在首位，运行期静态不变）。"""

    def __init__(self):
        self._items: "OrderedDict[str, DeviceRuntime]" = OrderedDict()

    def add(self, rt: DeviceRuntime) -> DeviceRuntime:
        if rt.device_id in self._items:
            raise ValueError(f"设备 id 重复：{rt.device_id}")
        self._items[rt.device_id] = rt
        return rt

    def get(self, device_id: str) -> DeviceRuntime | None:
        return self._items.get(device_id)

    @property
    def primary(self) -> DeviceRuntime:
        return next(iter(self._items.values()))

    def __iter__(self):
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, device_id: str) -> bool:
        return device_id in self._items

    def __getitem__(self, device_id: str) -> DeviceRuntime:
        return self._items[device_id]

    def keys(self):
        return list(self._items.keys())

    def values(self):
        return list(self._items.values())

    def items(self):
        return list(self._items.items())

    @property
    def multi(self) -> bool:
        """是否多设备：**所有"多设备才加字段"的地方都以它为准**（§3.8）。"""
        return len(self._items) > 1


class DeviceSSEEmitter:
    """按设备归属的 SSE 出口（§3.2/§3.8）。

    `multi=False` 时**完全透传**——连 `device_id` 都不带给底层分桶，保证单设备事件流
    与改造前逐字节一致；`multi=True` 时给每条事件注入 `device`，并让 status 按
    `(queue, device)` 维度各自保留最新。
    """

    __slots__ = ("base", "device_id", "multi")

    def __init__(self, base: SSEEmitter, device_id: str, multi: bool):
        self.base = base
        self.device_id = device_id
        self.multi = multi

    def emit(self, event_type: str, data: dict) -> None:
        if not self.multi:
            self.base.emit(event_type, data)
            return
        self.base.emit(event_type, {**data, "device": self.device_id},
                       device_id=self.device_id)

    def __repr__(self) -> str:
        return (f"<DeviceSSEEmitter device={self.device_id!r} multi={self.multi}>")


class RuntimeSupervisor:
    """`rt.task` 的唯一持有者：启动、停止、意外退出监控与重建（§3.13）。

    为什么必须有它：task 静默死掉 = 设备永久"假在线"，而 HTTP 还在正常应答，
    没有任何东西会来发现它。N=1 时这是盲区，N≥2 时不可接受。

    重建走 §3.5 L3 的同一套顺序：重挂全部钩子 → 重启任务。`handle_enable` 只能调
    `start()/stop()`；直接 cancel/create `rt.task` 会被当成意外退出立刻重建。
    """

    MAX_REBUILDS = 5            # 连续重建上限：超了只报警不再自动拉，避免无限重启风暴
    REBUILD_BASE_DELAY = 2.0
    REBUILD_MAX_DELAY = 60.0

    def __init__(self, runtime: "DeviceRuntime", *, mount_hooks=None,
                 startup_delay: float = 0.0):
        self.rt = runtime
        self._mount_hooks = mount_hooks          # Callable[[DeviceRuntime], None]
        self._startup_delay = startup_delay
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._rebuilds = 0
        self._rebuild_task: asyncio.Task | None = None

    def set_startup_delay(self, seconds: float) -> None:
        """设置启动错峰（§3.4）。装配顺序在注册表填满后才确定，所以是 setter。"""
        self._startup_delay = float(seconds or 0.0)

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """启动（幂等：已在运行则忽略）。"""
        self._stopping = False
        if self.is_running:
            return
        self._spawn()

    async def stop(self) -> None:
        """停止：先标记再请求，最后收割任务。

        顺序不能反 —— 先 `_stopping=True` 才能让 done 回调知道"这是我们要它停的"，
        而不是意外退出（否则每次正常停止都会触发一次重建）。
        """
        self._stopping = True
        if self._rebuild_task and not self._rebuild_task.done():
            self._rebuild_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rebuild_task
            self._rebuild_task = None
        mgr = self.rt.manager
        try:
            await mgr.request_stop()
        except Exception as e:
            _LOGGER.warning("[%s] request_stop failed: %s", self.rt.device_id, e)
        task = self._task
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
        self.rt.task = None

    async def schedule_rebuild(self, rebuild_resources) -> None:
        """在**独立任务**里做 L3 进程内重建（§3.5）。

        为什么不就地做：重建要从"停这个 manager 的任务"开始，而重建请求往往就是
        这个任务自己发出来的（认证卡死路径）——就地做等于 await 自己的取消。
        这里只负责调度，调用方（manager）随后自行退出重连循环。
        """
        if self._rebuild_task is not None and not self._rebuild_task.done():
            _LOGGER.info("[%s] 重建已在进行中，忽略重复请求", self.rt.device_id)
            return
        self._rebuild_task = asyncio.create_task(
            self._do_rebuild(rebuild_resources),
            name=f"rebuild-{self.rt.device_id}")

    async def _do_rebuild(self, rebuild_resources) -> None:
        try:
            await self.stop()                 # 停任务（request_stop 里按 link_loss 收尾会话）
            await rebuild_resources(self.rt)  # 换 manager + 重挂钩子
            self._rebuilds = 0                # 重建成功：清连续失败计数
            self.start()
        except Exception as e:
            _LOGGER.error("[%s] 进程内重建失败：%s: %s",
                          self.rt.device_id, type(e).__name__, e)

    def _spawn(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"ble-{self.rt.device_id}")
        self.rt.task = self._task
        self._task.add_done_callback(self._on_done)

    async def _run(self) -> None:
        if self._startup_delay > 0:
            # 启动错峰（§3.4）：N 台同时上电时的认证风暴防护，代价为零
            await asyncio.sleep(self._startup_delay)
        await self.rt.manager.start()

    def _on_done(self, task: asyncio.Task) -> None:
        if self._stopping:
            return
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            # manager.start() 正常返回 = 它自己决定不干了（stop_event 被置位等），
            # 不算异常退出，但也要留痕：否则就是又一个静默假在线。
            _LOGGER.warning("[%s] BLE 任务自行返回，不再自动重建", self.rt.device_id)
            return
        _LOGGER.error("[%s] BLE 任务意外退出：%s: %s",
                      self.rt.device_id, type(exc).__name__, exc)
        self._publish_offline()
        self._schedule_rebuild()

    def _publish_offline(self) -> None:
        """立刻发布该设备 offline（MQTT retained + scoped SSE）——"假在线"的对手。"""
        try:
            self.rt.manager.publish_status(
                {"connected": False, "error": "task_exited"}, retain=True)
        except Exception as e:
            _LOGGER.warning("[%s] offline 发布失败：%s", self.rt.device_id, e)

    def _schedule_rebuild(self) -> None:
        if self._rebuilds >= self.MAX_REBUILDS:
            _LOGGER.critical(
                "[%s] 已连续重建 %d 次，停止自动重建（需人工介入或升级为进程重启）",
                self.rt.device_id, self._rebuilds)
            return
        self._rebuilds += 1
        delay = min(self.REBUILD_BASE_DELAY * (2 ** (self._rebuilds - 1)),
                    self.REBUILD_MAX_DELAY)
        _LOGGER.warning("[%s] %.1fs 后重建 BLE 任务（第 %d 次）",
                        self.rt.device_id, delay, self._rebuilds)
        self._rebuild_task = asyncio.create_task(self._rebuild_after(delay))

    async def _rebuild_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._stopping:
            return
        try:
            # 重挂**全部**钩子：新 manager 实例不能继承旧实例的 publisher/invalidator
            if self._mount_hooks:
                self._mount_hooks(self.rt)
            self._spawn()
        except Exception as e:
            _LOGGER.error("[%s] 重建失败：%s", self.rt.device_id, e)
            self._schedule_rebuild()


class Server:
    """单进程单事件循环，持有 N 台设备（§3.1）。

    **P0-2 禁令**：这里的 `__init__` 只构建 registry / history_store，**绝不**出现
    `self.ble = ...` / `self.state = ...` / `self.history = ...` 赋值。一旦写了，
    三个 property 的 setter 会把值塞进 `_xxx_assigned`，getter 见其非空就永久短路
    —— ContextVar 永不生效，所有 per-device 请求静默落到 primary（history 更糟：
    落到无设备维度的共享 store，16 处查询/写入全部串台）。比直接报错更危险，
    所以 `__init__` 尾部有断言（`_assert_no_assignment`）。
    """

    def __init__(self, config=None, *, controller_factory=None):
        self.config = config if config is not None else load_config()
        # ── 测试装配 / 显式覆盖槽位（仅 `Server.__new__` 与老测试使用）──
        self._ble_assigned = None
        self._state_assigned = None
        self._history_assigned = None
        # 虚拟充电器注入点（§7 P1）：可传单个工厂或 {device_id: 工厂}
        self._controller_factory = controller_factory
        # 共享 BLE 适配器协调器（§3.4/§3.5，P2）：进程内**唯一**允许触碰适配器/
        # bluetoothctl 的模块。必须在 _build_runtime 之前建好——每个 manager 都要拿它。
        self.adapter = AdapterCoordinator(
            connect_limit=2,
            startup_stagger_sec=self.config.server.startup_stagger_sec,
            log=_LOGGER,
        )
        # 共享存储 + 设备注册表（primary 在首位，运行期静态不变）
        self._history_store = PortHistory(
            db_path=self.config.server.history_db_path,
            retention_days=self.config.server.history_retention_days,
        )
        self.registry = DeviceRegistry()
        for spec in self.config.device_specs():
            self._build_runtime(spec)
        self.mqtt_client = None
        self._mqtt_connect_time = 0.0
        self._mqtt_disconnect_count = 0
        self._mqtt_publish_failures = 0
        self.loop = None
        self._start_lock = asyncio.Lock()
        # 状态缓存按设备分桶（§3.2：容器留在 Server，键升级为复合键——比搬进
        # runtime 对 17 处测试装配的破坏小得多）
        self._status_cache: dict[str, bytes] = {}
        self._chart_cache: OrderedDict = OrderedDict()
        self._chart_cache_ttl = 10
        self._chart_cache_max = 10
        # 协调器的判定依赖"现在有几台设备 / 重建找得到 rt"，所以钩子在 registry 建好
        # 之后才接（above 的 _build_runtime 已经跑完）。
        self.adapter.set_device_count_provider(lambda: len(self.registry))
        self.adapter.set_rebuild_hook(self._rebuild_device_inprocess)
        self.adapter.set_process_restart_hook(self._restart)
        self._chart_cache_max_bytes = CHART_CACHE_MAX_BYTES
        self._chart_cache_max_entry_bytes = CHART_CACHE_MAX_ENTRY_BYTES
        self.sse = SSEEmitter()
        # 会话形状 {client, timer, devices}：`devices` 在扫码完成后落一次，供批量导入
        # 用——凭据（token/ble_key）只在服务端内存里过一遍，前端不必拿在手里再回传。
        self._xiaomi_sessions: dict[str, dict] = {}
        self._start_time = time.time()
        self.bemfa: BemfaClient | None = None
        # (device_id, port) → 巴法云实体 id 的映射，`setup_bemfa` 装配时落一次。
        # `_bemfa_publish` 走它而不是每次现算：自定义实体表后"哪个实体绑哪个口"只有
        # 装好的那份 plan 说得准（派生规则只是兜底）。
        self._bemfa_map: dict = {}
        # 后台任务挂在实例上而不是 aiohttp 的 app_ 里：应用启动完成后再写 app_
        # 会触发 aiohttp 的 DeprecationWarning（"Changing state of started or
        # joined application is deprecated"），未来版本会直接报错。
        # 任务持有归 `rt.supervisor`（§3.2/§3.13）：`Server._ble_task` 已删除，
        # handle_enable 只调 supervisor.start()/stop()。
        self._backfill_task: asyncio.Task | None = None
        effective_level = self.config.server.log_level
        logging.getLogger().setLevel(LOG_LEVELS.get(effective_level, logging.INFO))
        env_var = os.environ.get("CUKTECH_LOG_LEVEL")
        source = "环境变量 CUKTECH_LOG_LEVEL" if env_var else "config.yaml"
        _LOGGER.info("日志级别: %s (来源: %s)", effective_level, source)
        self._assert_no_assignment()

    # ── 设备注册表构建 ──

    def _build_runtime(self, spec, *, mount_hooks=None) -> DeviceRuntime:
        """按一份 `DeviceConfig` 建齐该设备的运行期资源（§3.2）。"""
        topics = self.config.topics_for(spec)
        state = ChargerState()
        manager = self._make_manager(spec, state, topics)
        rt = DeviceRuntime(
            device_id=spec.device_id,
            mac=spec.mac,
            token=spec.token,
            name=spec.display_name,
            topics=topics,
            state=state,
            manager=manager,
            history=DeviceHistory(self.history_store, spec.device_id),
            spec=spec,
        )
        rt.supervisor = RuntimeSupervisor(rt, mount_hooks=self.mount_device_hooks)
        self.registry.add(rt)
        return rt

    def _make_manager(self, spec, state, topics) -> BLEManager:
        factory = self._controller_factory
        if isinstance(factory, dict):
            factory = factory.get(spec.device_id)
        return BLEManager(
            spec.mac, spec.token, state, self.config,
            device_id=spec.device_id,
            topics=topics,
            controller_factory=factory,
            adapter=self.adapter,
        )

    def emit_device_event(self, event_type: str, data: dict) -> None:
        """发一条**带设备归属**的 SSE（§3.8）。只给设备维度的 handler 用。

        为什么不能直接 `self.sse.emit(...)`：共享出口不知道事件属于哪台设备，多设备
        时前端 `deviceOf(msg)` 会把它归给 primary，于是"给 B 拨协议开关，A 的界面跟着
        变"——命令打对了，展示串了。设备归属只有 `DeviceSSEEmitter` 知道。

        退化路径：手工装配（`Server.__new__`，17 处老测试）或钩子未挂的 manager 没有
        出口，此时退回共享 `sse`，与改造前逐字节一致（单设备事件流本来就不该带
        device 字段）。
        """
        try:
            manager = self.current_runtime().manager
        except (AttributeError, RuntimeError):
            manager = None            # registry 尚未装配
        if manager is not None and manager.sse_emitter is not None:
            manager.emit_event(event_type, data)
            return
        if hasattr(self, "sse"):
            self.sse.emit(event_type, data)

    # ── 装配面：每台设备的 10 项钩子（§3.2 P0-1） ──

    def mount_device_hooks(self, rt: DeviceRuntime) -> None:
        """把一台设备的**全部**钩子挂到它的 manager 上。

        缺任何一项都会让这台设备残废，而且多数是静默的：
        1 scoped SSE（缺 → 事件无设备归属，前端把设备 2 画成 primary）
        2 MQTT publisher（缺 → 完全不发 MQTT）
        3 status invalidator（缺 → 该设备 /api/status 永不失效）
        4 restart handler（缺 → 认证卡死无人重建，且绝不能捕获 s.ble）
        5 quality provider（缺/捕获错 → quality 显示成 primary）
        6 history（缺 → 不落库；捕获错 → 写进 default 行）
        7 录像开关 8 限额 9 常供 10 即停（缺 → 只加载 primary 的设置）
        """
        multi = self.registry.multi
        rt.manager.set_sse_emitter(DeviceSSEEmitter(self.sse, rt.device_id, multi=multi))
        rt.manager.set_mqtt_publisher(self.mqtt_publish)
        rt.manager.set_status_cache_invalidator(
            lambda rt=rt: self.invalidate_status_cache(rt.device_id))
        rt.manager.set_restart_handler(self.make_restart_handler(rt))
        rt.manager.set_quality_provider(self.make_quality_provider(rt))
        rt.manager.set_history(rt.history)
        rt.manager.record_sessions = rt.history.get_session_recording()
        rt.manager.set_charge_limits(rt.history.get_charge_limits())
        # 顺序：先常供再即停 —— set_permanent_ports 会剔除同口的即停标记（互斥）
        rt.manager.set_permanent_ports(rt.history.get_permanent_ports())
        rt.manager.set_full_off(rt.history.get_full_off())

    def make_restart_handler(self, rt: DeviceRuntime):
        """per-device 重启处理器（§3.3 红线 4：**捕获 rt，不捕获 s.ble**）。"""
        async def _restart_this_device(*args, **kwargs):
            await self.restart_device(rt)
        return _restart_this_device

    def make_quality_provider(self, rt: DeviceRuntime):
        """per-device quality（§3.3 红线 3：由每设备的 timer 在请求上下文之外调用）。"""
        def _quality():
            return {
                "ble": rt.manager.connection_quality(),
                "mqtt": self.mqtt_quality(),
                "bemfa": (self.bemfa.quality() if self.bemfa else
                          {"score": 0, "uptime": 0, "ping_lost": 0, "reconnect_count": 0}),
            }
        return _quality

    async def restart_device(self, rt: DeviceRuntime) -> None:
        """认证卡死的处置（§3.5 L3）。

        N=1：仍走进程重启（与改造前完全一致 —— P1 原则优先于"理论更优"）。
        N>1：进程内重建**这一台**，按 L3 生命周期：停任务 → cmd_queue 的待应答
        future 全部 settle 为失败 → 以 link_loss 关闭本设备活跃会话（once 限额保留、
        已积分能量保留）→ 新建 manager 实例 → 重挂全部钩子 → 启动。

        升级为进程重启的条件（≥2 台同时 auth-stuck / 适配器级错误）属 P2 的
        分级恢复策略；这里只保证"不会为了一台设备把全场打下线"。
        """
        if len(self.registry) <= 1:
            await self._restart()
            return
        _LOGGER.error("[%s] 认证卡死，进程内重建该设备（其他设备不受影响）", rt.device_id)
        # 关键：**不能就地重建**。本方法是从该设备的 manager 任务里被调用的，而重建
        # 第一步就是 `supervisor.stop()` → cancel 掉那个任务 → 等于 await 自己的取消，
        # 重建永远走不完（P1 遗留的自取消死锁）。所以交给 supervisor 的独立任务。
        await rt.supervisor.schedule_rebuild(self._rebuild_device_resources)

    async def _rebuild_device_resources(self, rt: DeviceRuntime) -> None:
        """L3 重建的资源层部分（此任务已停、会话已按 link_loss 收尾）。"""
        # 队列里等待响应的命令全部失败收尾，否则调用方会挂到超时
        while True:
            try:
                item = rt.manager.cmd_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            fut = item[2] if isinstance(item, tuple) and len(item) > 2 else None
            if fut is not None and not fut.done() and not fut.cancelled():
                fut.set_exception(ConnectionError("device restarting"))
        rt.manager.close_active_sessions(END_REASON_LINK_LOSS)
        rt.manager = self._make_manager(rt.spec, rt.state, rt.topics)
        # 重挂**全部** 10 项钩子：新 manager 是干净实例，漏一项这台设备就残废
        self.mount_device_hooks(rt)
        _LOGGER.info("[%s] 设备重建完成", rt.device_id)

    async def _rebuild_device_inprocess(self, mac: str) -> None:
        """协调器 L3 钩子：按 MAC 找到 runtime 并重建（§3.5）。"""
        for rt in self.registry:
            if rt.mac.lower() == str(mac).lower():
                await self.restart_device(rt)
                return
        _LOGGER.warning("L3 rebuild requested for unknown MAC %s", mac)

    def _assert_no_assignment(self) -> None:
        """P0-2 第③条：生产构造后三个槽位必须为空，否则 ContextVar 已失效。

        **故意用 raise 而不是 assert**：它守的是"per-device 路由静默失效"这条最危险
        的红线（界面照常响应，只是所有命令都打到 primary）。`python -O` 会把 assert
        整条删掉，于是最该拦住的那种部署反而没有任何检查——宁可启动崩掉。
        """
        leaked = [name for name, val in (
            ("_ble_assigned", self._ble_assigned),
            ("_state_assigned", self._state_assigned),
            ("_history_assigned", self._history_assigned),
        ) if val is not None]
        if leaked:
            raise RuntimeError(
                f"生产 __init__ 给 {leaked} 赋了值：`ble`/`state`/`history` 的 getter 会永久"
                "短路，per-device 路由静默失效（设计文档 §3.3 P0-2）")

    @property
    def history_store(self):
        """共享存储（服务级 meta / connect / close / backfill 的唯一入口）。

        `history_store` 未构造且测试只装配了 `_history_assigned` 时回退到它（§3.2）。
        **不得**在这里回退 `self.history` —— 那会让 history getter 与它互相递归。
        """
        store = getattr(self, "_history_store", None)
        if store is not None:
            return store
        return getattr(self, "_history_assigned", None)

    # ── primary 兼容层（§3.3）：只读 property，按请求上下文解析 ──

    @property
    def primary(self) -> DeviceRuntime:
        return self.registry.primary

    def current_runtime(self) -> DeviceRuntime:
        """当前请求的设备；无上下文（全局后台路径）→ primary。

        **适用边界**：只对"没有设备归属"的全局路径成立。由设备 X 触发的回调必须
        闭包捕获 rt，不能靠它解析（§3.3 五条红线）。
        """
        return self._current_rt_or_primary(required=True)

    def _current_rt_or_primary(self, *, required: bool):
        """解析当前设备；`required=False` 时"没有设备"返回 None。

        后者专供 `self.history` 这类**可缺省**依赖：老代码里 `if self.history:` 是
        "历史功能可能没开"的守卫，手工装配（`Server.__new__`、无 registry）时必须
        继续得到 None，而不是抛异常。
        """
        rt = _current_rt.get()
        if rt is not None:
            return rt
        registry = getattr(self, "registry", None)
        if registry is None or len(registry) == 0:
            if required:
                raise RuntimeError("设备注册表尚未初始化（生产路径不应发生）")
            return None
        return registry.primary

    def current_device_id(self) -> str:
        try:
            return self.current_runtime().device_id
        except Exception:
            # 未装配 registry（`Server.__new__` 测试路径）：退回 primary 的存储 id，
            # 让缓存键/日志等纯记账用途仍然可用。生产构造后 registry 一定存在。
            return PRIMARY_DEVICE_ID

    @property
    def _status_cache_map(self) -> dict:
        """状态缓存字典（懒创建）。

        `Server.__new__` 手工装配的 17 处老测试不会设置它；懒创建让这些用例继续
        工作，而生产路径在 `__init__` 里已经建好，行为无差别。
        """
        cache = getattr(self, "_status_cache", None)
        if cache is None:
            cache = {}
            self._status_cache = cache
        return cache

    @property
    def ble(self):
        if getattr(self, "_ble_assigned", None) is not None:
            return self._ble_assigned
        return self.current_runtime().manager

    @ble.setter
    def ble(self, value):
        # 仅供测试装配（`Server.__new__`）与显式覆盖；生产 __init__ 禁止走到这里。
        self._ble_assigned = value

    @property
    def state(self):
        if getattr(self, "_state_assigned", None) is not None:
            return self._state_assigned
        return self.current_runtime().state

    @state.setter
    def state(self, value):
        self._state_assigned = value

    @property
    def history(self):
        """当前设备的 history 视图（assigned-fallback，§3.2）。

        顺序：`_history_assigned`（测试装配）→ 当前 runtime 的 facade → primary 的。
        **不在 getter 里回退 `self.history`**（那就是自己，无限递归）。
        """
        if getattr(self, "_history_assigned", None) is not None:
            return self._history_assigned
        rt = self._current_rt_or_primary(required=False)
        return rt.history if rt is not None else None

    @history.setter
    def history(self, value):
        self._history_assigned = value

    # ── 设备命名空间的残留清理（§3.6/§3.10） ──

    #: 清 retained 用的 QoS。与 `clean_retained.py` 对齐（那边一直是 qos=1）：
    #: qos=0 只是"交给本地 socket"，紧接着的 `_restart()` 会断 MQTT，消息可能根本没
    #: 出去——而那时旧前缀已经不在配置里，脚本也推导不出来，等于永久幽灵。
    RETAINED_CLEAR_QOS = 1
    #: `wait_for_publish()` 的等待上限（秒）。**必须给**：paho 的默认值是 None，
    #: 语义是"永远等"（内部 `Condition.wait(None)`）。而这段代码跑在 aiohttp 的
    #: 事件循环里——broker 连着却不回 PUBACK（broker 卡死 / TCP 缓冲满 / 半死连接）
    #: 时，一次配置保存就能把整台服务器冻住：所有设备的 HTTP、SSE、命令全部停摆。
    RETAINED_CLEAR_TIMEOUT = 2.0

    def _mqtt_clear_retained(self, topic: str) -> bool:
        """清掉一条 retained 消息（**空负载** + retain + qos 1）。

        不能用 `mqtt_publish(topic, None, retain=True)`：那个函数会
        `json.dumps(None)` → 往 retained 里塞一个字符串 "null"，订阅方看到的仍是
        一条"有内容"的消息。清除 retained 的唯一正确形状是空 payload。

        `wait_for_publish(timeout)`：清理发生在"写盘之后、重启之前"的 1 秒窗口里，
        没确认就断电式重启 = 清除白做；但**必须带超时**，理由见
        `RETAINED_CLEAR_TIMEOUT`。老版本 paho 没有这个方法，`getattr` 兜底。

        **阻塞调用**（paho 内部等 threading.Condition）——只允许从工作线程调，
        见 `_purge_namespace`。
        """
        if not (self.mqtt_client and self.mqtt_client.is_connected()):
            return False
        try:
            info = self.mqtt_client.publish(topic, "", retain=True, qos=self.RETAINED_CLEAR_QOS)
            waiter = getattr(info, "wait_for_publish", None)
            if callable(waiter):
                try:
                    waiter(self.RETAINED_CLEAR_TIMEOUT)
                except TypeError:      # paho 1.x 的签名不接受参数：宁可不等也别卡死
                    waiter()
            return True
        except Exception as e:
            self._mqtt_publish_failures += 1
            _LOGGER.warning("清 retained 失败 %s: %s", topic, e)
            return False

    def _clear_retained_batch(self, topics) -> list[str]:
        """逐条清（**阻塞**，只允许在线程里调）。"""
        return [t for t in topics if self._mqtt_clear_retained(t)]

    @staticmethod
    def _namespace_topics(ns) -> list[str]:
        """一台设备的全部"状态型"主题（要清的就是这些）。"""
        topics = [ns.status, ns.settings, ns.port, ns.ble, ns.charge_event]
        topics += [ns.port_topic(name) for name in PORT_NAMES.values()]
        return topics

    async def _purge_namespace(self, ns, device_id: str, why: str) -> list[str]:
        """清一台设备命名空间下的 retained（在**工作线程**里跑，不占事件循环）。

        为什么必须丢线程：`wait_for_publish()` 是阻塞调用，paho 内部就是在等一个
        `threading.Condition`。放在事件循环里调，broker 一卡住整台服务器就冻住
        （所有设备的 HTTP/SSE/命令一起停摆）——清 9 条 × N 台设备，每条都可能等。
        `mqtt_client.publish()` 本身是线程安全的（paho 明确支持跨线程 publish）。
        """
        topics = self._namespace_topics(ns)
        cleared = await asyncio.to_thread(self._clear_retained_batch, topics)
        _LOGGER.warning("[%s] %s：清理 %d/%d 条 retained 主题（历史数据保留）",
                        device_id, why, len(cleared), len(topics))
        return cleared

    async def purge_removed_devices(self, removed) -> list[str]:
        """设备从配置里消失后，清掉它在 broker 上的 retained 状态。

        为什么必须清：`{prefix}/status`、`.../port/c1` 等都是 retained 的，设备没了
        它们还留在 broker 上——HA / MQTT Explorer 会看到一个**永远停在最后一次上报**
        的幽灵设备，用户以为设备还在。清理是幂等的：同 id 加回来，几秒内就被真实
        状态覆盖。

        **历史数据一个字不动**：那是用户数据，删了不可逆。同 id 重新加回来时曲线与
        会话会原样回来；确需清库走 `clean_retained.py` / `history_down.py`。
        """
        cleared: list[str] = []
        for rt in removed:
            cleared += await self._purge_namespace(
                rt.topics, rt.device_id, "设备已从配置移除（同 id 加回即恢复）")
        return cleared

    async def purge_stale_prefix(self, rt, new_prefix: str) -> list[str]:
        """设备**还在**、但 `topic_prefix` 改了：旧前缀的 retained 同样是幽灵。

        这条缺口以前没人补：`purge_removed_devices` 只对比"消失的设备"，
        而 `clean_retained.py` 的前缀是从**当前配置**推导的——旧前缀改完之后就不在
        配置里了，脚本也推导不出来，等于永久幽灵。所以必须在改前缀的当场清。
        """
        return await self._purge_namespace(
            rt.topics, rt.device_id, f"topic 前缀已改为 {new_prefix}")

    def mqtt_publish(self, topic, payload, retain=False):
        """Publish to all enabled MQTT clients (multiplex)."""
        # HA MQTT
        if self.mqtt_client and self.mqtt_client.is_connected():
            try:
                self.mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)
            except Exception:
                self._mqtt_publish_failures += 1
        # Bemfa
        if self.bemfa and self.bemfa.is_connected:
            self._bemfa_publish(topic, payload)

    def _bemfa_entity_ids(self, rt) -> dict:
        """一台设备的语音实体 id（§3.11）。

        primary **必须**沿用现有 id（`cuktech_c1/…/cuktech_ble`）——用户配好的
        语音指令不能因为多设备改造失效；其他设备用 `{device_id}_c1` 前缀。
        """
        if rt.device_id == PRIMARY_DEVICE_ID:
            ids = {"c1": "cuktech_c1", "c2": "cuktech_c2", "c3": "cuktech_c3",
                   "a": "cuktech_usb_a", "ble": "cuktech_ble"}
        else:
            prefix = rt.device_id
            ids = {name: f"{prefix}_{'usb_a' if name == 'a' else name}"
                   for name in ("c1", "c2", "c3", "a")} | {"ble": f"{prefix}_ble"}
        # `bemfa_<name>:` 覆盖项（老用户的实体 id 可能是任意值，改名会让巴法云里
        # 的自动化全断，所以允许逐项钉死；没写就用上面的派生规则）
        overrides = getattr(getattr(rt, "spec", None), "bemfa_names", None) or {}
        for name, entity_id in overrides.items():
            if entity_id:
                ids[name] = str(entity_id)
        return ids

    def _bemfa_device_for_topic(self, topic):
        """topic 前缀 → 设备（§3.6 命名空间：前缀就是设备身份）。

        前缀可能互相前缀包含（`cuktech/charger` 与 `cuktech/charger/desk`），所以
        必须取**最长匹配**，否则 primary 会吃掉别的设备的事件。
        """
        best = None
        for rt in self.registry:
            prefix = rt.topics.prefix
            if (topic == prefix or topic.startswith(prefix + "/")) and (
                    best is None or len(prefix) > len(best.topics.prefix)):
                best = rt
        return best

    def _bemfa_entity_plan(self) -> list:
        """本次要建到巴法云的实体清单（§3.11）：一行 = 一台设备的某个开关。

        返回 `[{device_id, port, name, entity_id, rt}]`，**`rt` 一并带上**：命令回调
        必须捕获它（红线：回调里没有请求上下文，用 `self.ble` 会永远落到 primary）。

        两种来源：
        - 配了实体表（`bemfa.entities` 非空，或显式写了这个键）→ 完全照表来，
          名字/设备/端口都是用户填的；
        - 否则 → 老配置（`allowed_devices` × 5 个开关 + `name_c*`），行为与改造前
          逐字节一致，老用户升级后语音指令一个都不变。
        """
        plan: list = []
        seen_ids: set = set()

        def _push(rt, port, name, eid):
            # 同一个实体 id 绝不能建两次：`BemfaClient._devices` 是 dict，后来者会
            # 覆盖前者 → 两个开关抢一个 topic，按哪个都只动其中一处。
            if not eid or eid in seen_ids:
                if eid:
                    _LOGGER.warning("Bemfa 实体 id 重复（%s），已跳过 %s/%s",
                                    eid, rt.device_id, port)
                return
            seen_ids.add(eid)
            plan.append({"device_id": rt.device_id, "port": port, "name": name,
                         "entity_id": eid, "rt": rt})

        entries = list(getattr(self.config.bemfa, "entities", None) or [])
        # `entities_defined` 才决定"用不用这张表"：空表 + 有键 = 用户就要 0 个实体；
        # 空表 + 没键 = 老配置，走下面的兜底派生（升级后语音指令一个都不变）。
        if entries or getattr(self.config.bemfa, "entities_defined", False):
            for ent in entries:
                name, device_id, port = bemfa_entity_triple(ent)
                name = name.strip() or f"{BEMFA_PORT_LABELS.get(port, port)}开关"
                rt = self.registry.get(device_id)
                if rt is None:
                    # 配置校验（validate_bemfa_entities）已经拦过，走到这里只可能是
                    # 运行期把设备删了：跳过，但要在日志里能查到
                    _LOGGER.warning("Bemfa 实体 `%s` 指向的设备 `%s` 不存在，已跳过",
                                    name, device_id)
                    continue
                _push(rt, port, name, self._bemfa_entity_ids(rt).get(port))
            return plan

        # ── 老配置兜底 ──
        for item in self._derived_bemfa_plan():
            _push(item["rt"], item["port"], item["name"], item["entity_id"])
        return plan

    def _derived_bemfa_plan(self) -> list:
        """老配置（没写过 `bemfa.entities` 键）的派生清单：`allowed_devices` × 5。

        独立成方法是为了让**配置页也能拿到同一份派生结果**：老用户第一次在新页面上
        点保存时，前端必须把这张表原样铺出来再提交，否则"配了 `allowed_devices: ["*"]`
        的用户"会因为页面只铺 primary 的 5 行而丢掉其它设备的语音开关——而且服务端
        随后会把它们从巴法云真的删掉（`registered` 差集）。派生规则只有这一份，
        前端不再自己拼名字（`GET /api/config` 直接回 `derivedEntities`）。
        """
        registry = getattr(self, "registry", None)
        if registry is None:          # `Server.__new__` 手工装配的测试路径
            return []
        plan: list = []
        allowed = self.config.bemfa.allowed_devices or [PRIMARY_DEVICE_ID]
        names = {"c1": self.config.bemfa.name_c1, "c2": self.config.bemfa.name_c2,
                 "c3": self.config.bemfa.name_c3, "a": self.config.bemfa.name_a,
                 "ble": self.config.bemfa.name_ble}
        for rt in registry:
            if "*" not in allowed and rt.device_id not in allowed:
                continue
            ids = self._bemfa_entity_ids(rt)
            for port in BEMFA_PORTS:
                if rt.device_id == PRIMARY_DEVICE_ID:
                    label = names.get(port) or BEMFA_DEFAULT_NAMES.get(port, port)
                elif port == "ble":
                    label = f"{rt.name} 蓝牙开关"
                else:
                    label = f"{rt.name} {BEMFA_PORT_LABELS.get(port, port)}"
                plan.append({"device_id": rt.device_id, "port": port,
                             "name": label, "entity_id": ids.get(port), "rt": rt})
        return plan

    def _bemfa_entity_for(self, rt, port):
        """(设备, 端口) → 实体 id。表里没有这个开关就返回 None（不发状态）。

        装配过的映射优先；没有映射时**只有一种情况可以退回派生规则**：还没装配
        （单元测试、早于 `setup_bemfa` 的发布）。一旦配了实体表，派生 id 就不是
        "这个口有开关"的证据——比如用户删掉了 primary 的 C2，那时再按派生规则发
        状态，服务端会往一个没注册过的实体上发（线上是空转，测试里则是假阳性）。
        """
        plan_map = getattr(self, "_bemfa_map", None) or {}
        eid = plan_map.get((rt.device_id, port))
        if eid:
            return eid
        if plan_map or getattr(self.config.bemfa, "entities_defined", False):
            return None
        return self._bemfa_entity_ids(rt).get(port)

    def _bemfa_publish(self, topic, payload):
        """Map HA MQTT topics to Bemfa device states（按设备命名空间路由）。"""
        rt = self._bemfa_device_for_topic(topic)
        if rt is None:
            return
        if not isinstance(payload, dict):
            return
        rest = topic[len(rt.topics.prefix):].lstrip("/")
        if rest.startswith("port/"):
            entity_id = self._bemfa_entity_for(rt, rest.split("/")[-1])
            if entity_id:
                self.bemfa.publish_state(entity_id, MSG_ON if payload.get("active") else MSG_OFF)
        elif rest == "status":
            entity_id = self._bemfa_entity_for(rt, "ble")
            if entity_id:
                self.bemfa.publish_state(entity_id, MSG_ON if payload.get("connected") else MSG_OFF)

    async def setup_bemfa(self):
        """Initialize Bemfa client if enabled."""
        if not self.config.bemfa.enabled or not self.config.bemfa.uid:
            _LOGGER.info("Bemfa disabled or UID not configured")
            return

        # Cleanup old client if exists
        if self.bemfa:
            await self.bemfa.stop()

        self.bemfa = BemfaClient(self.config.bemfa.uid, modified=self.config.bemfa.modified)

        plan = self._bemfa_entity_plan()
        self._bemfa_map = {(item["device_id"], item["port"]): item["entity_id"]
                           for item in plan}

        def _port_cmd(rt, port, on):
            """命令回调**必须捕获 rt**（§3.3 红线）：这里没有请求上下文，

            用 `self.ble` 解析会永远落到 primary —— 一条语音指令就会去开别人家的口。
            """
            _LOGGER.info("Bemfa command [%s]: %s %s", rt.device_id, port,
                         "on" if on else "off")
            try:
                self.loop.call_soon_threadsafe(
                    rt.manager.cmd_queue.put_nowait,
                    ("port", (port, "on" if on else "off"), None))
                return True
            except Exception as e:
                _LOGGER.error("Bemfa port cmd failed [%s]: %s", rt.device_id, e)
                return False

        def _ble_cmd(rt, on):
            """BLE 开关：只能经 supervisor 启停（§3.13），不得直接 cancel/create 任务。"""
            _LOGGER.info("Bemfa BLE command [%s]: %s", rt.device_id,
                         "on" if on else "off")
            try:
                if on:
                    self.loop.call_soon_threadsafe(rt.supervisor.start)
                else:
                    asyncio.run_coroutine_threadsafe(
                        rt.supervisor.stop(), self.loop)
                return True
            except Exception as e:
                _LOGGER.error("Bemfa BLE cmd failed [%s]: %s", rt.device_id, e)
                return False

        for item in plan:
            eid, rt, port = item["entity_id"], item["rt"], item["port"]
            self.bemfa.add_device(eid, item["name"])
            if port == "ble":
                self.bemfa.on_command(eid, lambda on, rt=rt: _ble_cmd(rt, on))
            else:
                self.bemfa.on_command(
                    eid, lambda on, rt=rt, port=port: _port_cmd(rt, port, on))

        # ── 云端残留：从配置里删掉的实体，在巴法云 App 里还留着一个按了没反应的开关 ──
        # 用上一次成功注册的 id 快照做差集——只有服务端自己知道上一轮到底注册过什么
        # （用户手改 YAML 也照样对得上）。快照存在 **DB meta**，见 `_bemfa_registered_snapshot`。
        previous = self._bemfa_registered_snapshot()
        stale = [eid for eid in previous if eid not in set(self._bemfa_map.values())]
        if stale:
            _LOGGER.info("Bemfa 清理已删除的实体 %d 个: %s", len(stale), ", ".join(stale))
            await self.bemfa.delete_topics(stale)

        await self.bemfa.start()

        # Clear modified flag after successful topic registration
        if self.config.bemfa.modified:
            self._clear_bemfa_modified()
        # 记下这一轮注册的实体 id，供下次启动做残留清理
        registered = sorted(set(self._bemfa_map.values()))
        if registered != previous:
            self._save_bemfa_registered(registered)

    def _config_path(self) -> Path:
        return Path(os.environ.get("CUKTECH_CONFIG_PATH", str(Path(__file__).parent / "config.yaml")))

    #: 上一轮注册到巴法云的实体 id 快照（供下次启动清云端残留）。
    #: **存在 DB meta 而不是 config.yaml**：这是服务端自己的运行期记账，而
    #: `yaml.safe_load + yaml.dump` 会把用户手写的注释、键序、引号风格整份抹掉
    #: （config.yaml.example 就是一份带注释的模板，README 让人 cp 过去改）。
    #: meta 表本来就是干这个的（`permanent_ports` / `web_language` 都在那儿）。
    BEMFA_REGISTERED_META_KEY = "bemfa_registered"

    def _bemfa_registered_snapshot(self) -> list[str]:
        """上一次成功注册的实体 id（DB meta 为准，兼容早期写在 YAML 里的字段）。"""
        store = self.history_store
        ids: list[str] = []
        if store is not None:
            try:
                raw = store.get_meta(self.BEMFA_REGISTERED_META_KEY, "")
            except Exception:
                raw = ""
            if raw:
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                if isinstance(parsed, list):
                    ids = [str(x) for x in parsed if x]
        if not ids:
            # 迁移：10fed6b 曾把它写进 config.yaml 的 `bemfa.registered`，读一次并迁走
            legacy = [str(x) for x in (getattr(self.config.bemfa, "registered", None) or []) if x]
            if legacy:
                _LOGGER.info("Bemfa registered 快照从 config.yaml 迁移到 DB meta（%d 项）",
                             len(legacy))
                ids = sorted(set(legacy))
                self._save_bemfa_registered(ids)
        return sorted(set(ids))

    def _save_bemfa_registered(self, entity_ids) -> None:
        """记下本轮注册的实体 id（只写 DB meta，绝不碰 config.yaml）。"""
        store = self.history_store
        if store is None:                 # 手工装配的测试路径
            return
        payload = json.dumps(sorted(set(entity_ids)))
        try:
            store.set_meta(self.BEMFA_REGISTERED_META_KEY, payload)
            _LOGGER.info("Bemfa 已记录本轮注册的实体 %d 个", len(set(entity_ids)))
        except Exception:
            _LOGGER.warning("Bemfa registered 快照写入失败", exc_info=True)

    def _set_bemfa_yaml_bool(self, key: str, value: bool) -> bool:
        """只改 config.yaml 里 `bemfa.<key>` 这**一行**，其余字节原样保留。

        为什么要做到"只改一行"：服务端记账曾经用 `yaml.safe_load` + `yaml.dump`
        整份重写，那会把用户手写的注释、顶层键顺序（dump 默认 sort_keys）和引号风格
        全部抹掉——用户只会看到"我保存了一次设置，注释全没了"。
        找不到该键时在 `bemfa:` 节头后补一行；找不到 `bemfa:` 节就整体追加。
        失败一律返回 False（记账失败绝不影响启动）。
        """
        try:
            config_path = self._config_path()
            if not config_path.exists():      # 纯环境变量部署：不凭空造文件
                return False
            text = config_path.read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            literal = "true" if value else "false"
            section_start = None
            for idx, line in enumerate(lines):
                if re.match(r"^bemfa\s*:\s*(#.*)?$", line.rstrip("\n")):
                    section_start = idx
                    break
            if section_start is None:
                suffix = "" if text.endswith(("\n", "")) else "\n"
                config_path.write_text(f"{text}{suffix}bemfa:\n  {key}: {literal}\n",
                                       encoding="utf-8")
                return True
            # 在节内找 `key:`（缩进 > 0；遇到下一个顶层键就停）
            body_end = len(lines)
            for idx in range(section_start + 1, len(lines)):
                if lines[idx].strip() and not lines[idx].startswith((" ", "\t")):
                    body_end = idx
                    break
            for idx in range(section_start + 1, body_end):
                m = re.match(r"^(\s+)" + re.escape(key) + r"\s*:", lines[idx])
                if m:
                    indent = m.group(1)
                    newline = "\n" if lines[idx].endswith("\n") else ""
                    lines[idx] = f"{indent}{key}: {literal}{newline}"
                    config_path.write_text("".join(lines), encoding="utf-8")
                    return True
            # 节存在但没有这个键：插在节头后面
            newline = "\n" if lines[section_start].endswith("\n") else "\n"
            lines.insert(section_start + 1, f"  {key}: {literal}{newline}")
            config_path.write_text("".join(lines), encoding="utf-8")
            return True
        except Exception:
            _LOGGER.warning("更新 config.yaml 的 bemfa.%s 失败", key, exc_info=True)
            return False

    def _clear_bemfa_modified(self):
        """Set bemfa.modified=false in config.yaml after topic re-registration."""
        if self._set_bemfa_yaml_bool("modified", False):
            _LOGGER.info("Bemfa modified flag cleared")

    async def handle_health(self, request):
        """GET /api/health - health check endpoint."""
        ble_connected = self.ble.ctrl is not None and self.state.connected
        mqtt_connected = self.mqtt_client is not None and self.mqtt_client.is_connected()
        bemfa_connected = self.bemfa is not None and self.bemfa.is_connected
        all_healthy = bool(ble_connected or self.config.server.port > 0)
        payload = {
            "ok": True,
            "healthy": all_healthy,
            "components": {
                "ble": ble_connected,
                "mqtt": mqtt_connected,
                "bemfa": bemfa_connected,
            },
            "uptime_ms": int((time.time() - getattr(self, '_start_time', time.time())) * 1000),
        }
        # 单设备：响应保持原样（不改变 healthy 语义，避免破坏既有 docker/监控假设）；
        # 多设备：附加 devices 真值分区（§3.13）。`healthy` 字段本身的语义缺陷
        # （ble_connected or port>0 实际恒真）留给独立立项，不与多设备耦合。
        if self.registry.multi:
            payload["devices"] = {
                rt.device_id: {
                    "connected": bool(rt.state.connected),
                    "authenticated": bool(rt.state.authenticated),
                }
                for rt in self.registry
            }
        return web.json_response(payload)

    async def handle_devices(self, request):
        """GET /api/devices - 设备清单（§3.7，多设备发现端点）。

        无遗留消费者，因此它的存在本身不构成字节一致性破坏——"老面不变"指的是
        老端点的响应。单设备时返回一台 primary，Web UI 据此隐藏切换器（§3.12）。

        `recovery` 只在**卡在 L1.5 退避里**的设备上加：多设备且有活连接时阶梯升不
        上去（L2 会掐断别人），它会无限重试，运维在界面上只看得到 connected=false。
        正常设备不带这个键，响应逐字节不变。
        """
        adapter = getattr(self, "adapter", None)
        devices = []
        for rt in self.registry:
            item = rt.describe()
            state = adapter.backoff_state(rt.mac) if adapter is not None else None
            if state:
                item["recovery"] = state
            devices.append(item)
        return web.json_response({"ok": True, "devices": devices})

    async def handle_bemfa(self, request):
        """GET /api/bemfa - get Bemfa status."""
        enabled = self.bemfa is not None
        connected = self.bemfa is not None and self.bemfa.is_connected
        uid = self.config.bemfa.uid
        uid_display = f"{uid[:4]}****" if len(uid) > 4 else uid
        return web.json_response({
            "enabled": enabled,
            "connected": connected,
            "uid": uid_display,
            "configured": bool(uid),
        })

    async def setup_mqtt(self):
        if not self.config.mqtt.enabled:
            _LOGGER.info("MQTT disabled, running in standalone web server mode")
            return

        import paho.mqtt.client as mqtt
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        # 见模块常量注释：给待确认队列硬上限，避免断线/半开连接期间无限堆积
        self.mqtt_client.max_queued_messages_set(MQTT_MAX_QUEUED_MESSAGES)
        self.mqtt_client.max_inflight_messages_set(MQTT_MAX_INFLIGHT_MESSAGES)
        if self.config.mqtt.username:
            self.mqtt_client.username_pw_set(self.config.mqtt.username, self.config.mqtt.password)
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

        def on_connect(client, userdata, flags, rc, properties=None):
            _LOGGER.info("MQTT connected (rc=%s)", rc)
            s = get_server()
            s._mqtt_connect_time = time.time()
            # 遍历 registry（§3.2 P0-1）：只给 s.ble 挂 publisher 会让设备 2 完全
            # 不发 MQTT —— 而且静默（没有异常、没有日志、界面只是"不动"）。
            s.mount_mqtt_publishers()
            s.setup_mqtt_subscriptions()

        def on_disconnect(client, userdata, flags, rc, properties=None):
            _LOGGER.warning("MQTT disconnected (rc=%s)", rc)
            get_server()._mqtt_disconnect_count += 1

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_disconnect = on_disconnect

        self.mqtt_client.will_set(
            # LWT 只有一个，保留挂在 primary 的 status 上（§3.6）。多设备的离线判定
            # 由 HA 侧已有的 30s HTTP 健康检查兜底 —— 它本来就不依赖遗嘱（ESP32 也没有）。
            self._lwt_topic(),
            json.dumps({"connected": False}),
            retain=True, qos=1
        )

        for attempt in range(3):
            try:
                self.mqtt_client.connect(self.config.mqtt.host, self.config.mqtt.port, self.config.mqtt.keepalive)
                self.mqtt_client.loop_start()
                _LOGGER.info("MQTT connecting to %s:%s", self.config.mqtt.host, self.config.mqtt.port)
                break
            except Exception:
                _LOGGER.error("MQTT connection failed (attempt %d/3): %s:%s",
                              attempt + 1, self.config.mqtt.host, self.config.mqtt.port)
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    self.mqtt_client = None

        if self.mqtt_client:
            self.mount_mqtt_publishers()

    def mount_mqtt_publishers(self) -> None:
        """给**每一台**设备重挂 MQTT publisher（§3.2 P0-1 第2项）。

        独立的公开方法是因为它有两条调用路径：`setup_mqtt()` 结尾、以及 paho 的
        `on_connect` 回调（重连后必须重挂，否则重连即失声）。漏掉任何一条路径，
        设备 2 就完全不发 MQTT。
        """
        registry = getattr(self, "registry", None)
        if registry is None:
            # `Server.__new__` 手工装配的测试路径：只有显式给的 manager。
            assigned = getattr(self, "_ble_assigned", None)
            if assigned is not None:
                assigned.set_mqtt_publisher(self.mqtt_publish)
            return
        for rt in registry:
            rt.manager.set_mqtt_publisher(self.mqtt_publish)

    def _lwt_topic(self) -> str:
        """遗嘱主题 = primary 的 status（§3.6：paho 只有一个遗嘱）。"""
        try:
            return self.config.topics_for(self.registry.primary.spec).status
        except Exception:
            return self.config.topic_status

    def setup_mqtt_subscriptions(self):
        """为每台设备订阅 `{prefix}/set`、`{prefix}/port`、`{prefix}/ble`（§3.6）。

        **路由按 topic 精确查表**：MQTT 回调没有请求上下文，也不该有（§3.3 红线 1）。
        这里**禁止**使用 `self.ble` —— 它会在无上下文时解析到 primary，把设备 2 的
        命令发给设备 1。单设备时订阅集合与改造前完全相同（3 条 topic）。
        """
        if not self.mqtt_client:
            return
        if getattr(self, "registry", None) is None:
            # `Server.__new__` 手工装配的测试路径（无 registry）：没有设备就谈不上
            # 按前缀路由，直接跳过。生产构造后 registry 一定存在。
            return
        server = self
        # topic → (device_id, kind)。精确匹配，无 wildcard，天然不会串台。
        routes: dict[str, tuple[str, str]] = {}
        for rt in self.registry:
            routes[rt.topics.set] = (rt.device_id, "set")
            routes[rt.topics.port] = (rt.device_id, "port")
            routes[rt.topics.ble] = (rt.device_id, "ble")

        def on_mqtt_message(client, userdata, msg):
            try:
                target = routes.get(msg.topic)
                if target is None:
                    # 不是本进程任何设备的命令主题（可能是残留的 retained，
                    # 或回滚后遗留的幽灵前缀）：静默丢弃，不要误会成"当前设备"。
                    return
                device_id, kind = target
                rt = server.registry.get(device_id)
                if rt is None:
                    return
                payload = json.loads(msg.payload)
                if server.loop is None:
                    return
                if kind == "set":
                    piid = payload.get("piid")
                    value = payload.get("value")
                    if piid is None or value is None:
                        _LOGGER.warning("[%s] Invalid set command: missing piid or value", device_id)
                        return
                    try:
                        piid_int = int(piid)
                        value_int = int(value)
                    except (ValueError, TypeError):
                        _LOGGER.warning("[%s] Invalid set command: piid/value must be integers", device_id)
                        return
                    if piid_int not in VALID_PIIDS:
                        _LOGGER.warning("[%s] Invalid set command: piid %d not valid", device_id, piid_int)
                        return
                    min_val, max_val = PIID_RANGES[piid_int]
                    if not (min_val <= value_int <= max_val):
                        _LOGGER.warning("[%s] Invalid set command: value %d out of range [%d, %d]",
                                        device_id, value_int, min_val, max_val)
                        return
                    server.loop.call_soon_threadsafe(
                        rt.manager.cmd_queue.put_nowait,
                        ("set", (piid_int, value_int), None))
                    _LOGGER.info("[%s] MQTT set command: piid=%d value=%d", device_id, piid_int, value_int)
                elif kind == "port":
                    port = payload.get("port")
                    action = payload.get("action")
                    if not port or action not in ("on", "off"):
                        _LOGGER.warning("[%s] Invalid port command: port=%s action=%s",
                                        device_id, port, action)
                        return
                    if port not in PORT_BITS and port != "all":
                        _LOGGER.warning("[%s] Invalid port command: unknown port %s", device_id, port)
                        return
                    server.loop.call_soon_threadsafe(
                        rt.manager.cmd_queue.put_nowait,
                        ("port", (port, action), None))
                    _LOGGER.info("[%s] MQTT port command: port=%s action=%s", device_id, port, action)
                elif kind == "ble":
                    enabled = payload.get("enabled")
                    if enabled is None or not isinstance(enabled, bool):
                        _LOGGER.warning("[%s] Invalid BLE command: 'enabled' must be a bool", device_id)
                        return
                    _LOGGER.info("[%s] MQTT BLE command: %s", device_id,
                                 "enable" if enabled else "disable")
                    try:
                        if enabled:
                            # supervisor 是任务唯一持有者（§3.13）：直接 create/cancel
                            # 会被当成意外退出、立刻触发重建。
                            server.loop.call_soon_threadsafe(rt.supervisor.start)
                        else:
                            asyncio.run_coroutine_threadsafe(rt.supervisor.stop(), server.loop)
                    except Exception as e:
                        _LOGGER.error("[%s] MQTT BLE cmd failed: %s", device_id, e)
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                _LOGGER.error("MQTT cmd parse error: %s", e)
            except Exception as e:
                _LOGGER.error("MQTT cmd error: %s", e)

        self.mqtt_client.on_message = on_mqtt_message
        for topic in routes:
            self.mqtt_client.subscribe(topic)

        _LOGGER.info("MQTT subscriptions ready (%d device(s), %d topic(s))",
                     len(self.registry), len(routes))

    def invalidate_status_cache(self, device_id=None):
        """失效**目标设备**的状态缓存（§3.2 第3项）。

        `device_id=None` ⇒ 取当前上下文（无上下文即 primary）：单设备部署与老调用点
        `invalidate_status_cache()` 的行为因此与改造前完全一致。
        装配时必须给每台 manager 注册 `lambda rt=rt: s.invalidate_status_cache(rt.device_id)`
        —— 共用 primary 的闭包会让设备 2 的状态变更永远不失效自己的缓存。
        """
        cache = self._status_cache_map
        if device_id is not None:
            cache.pop(device_id, None)
            return
        cache.pop(self.current_device_id(), None)

    def mqtt_quality(self) -> dict:
        """Return MQTT connection quality metrics."""
        if not self.mqtt_client:
            return {"score": 0, "uptime": 0, "disconnects": 0, "publish_failures": 0}
        connected = self.mqtt_client.is_connected()
        if not connected:
            return {"score": 0, "uptime": 0, "disconnects": self._mqtt_disconnect_count,
                    "publish_failures": self._mqtt_publish_failures}
        uptime = int(time.time() - self._mqtt_connect_time) if self._mqtt_connect_time else 0
        # Disconnect penalty: each disconnect costs 15 points
        dc_score = max(0, 100 - self._mqtt_disconnect_count * 15)
        # Publish failure penalty
        pf_score = max(0, 100 - self._mqtt_publish_failures * 5)
        score = round(dc_score * 0.6 + pf_score * 0.4)
        return {"score": score, "uptime": uptime, "disconnects": self._mqtt_disconnect_count,
                "publish_failures": self._mqtt_publish_failures}

    async def handle_status(self, request):
        device_id = self.current_device_id()
        cache = self._status_cache_map
        cached = cache.get(device_id)
        if cached:
            return web.Response(
                body=cached,
                content_type="application/json",
            )
        data = await self.state.to_dict()
        data["mqtt_connected"] = self.mqtt_client is not None and self.mqtt_client.is_connected()
        data["session_recording"] = bool(self.ble.record_sessions)
        modes = self.ble.get_port_modes_state()
        data["permanent_ports"] = modes["permanent_ports"]
        data["full_off_ports"] = modes["full_off_ports"]
        body = await asyncio.to_thread(
            lambda: json.dumps(data, ensure_ascii=False).encode()
        )
        cache[device_id] = body
        return web.Response(
            body=body,
            content_type="application/json",
        )

    async def handle_set(self, request):
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        piid = data.get("piid")
        value = data.get("value")
        if piid is None or value is None:
            return web.json_response({"ok": False, "error": "missing piid or value"}, status=400)
        try:
            piid_int = int(piid)
            value_int = int(value)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "piid and value must be integers"}, status=400)
        if piid_int not in VALID_PIIDS:
            return web.json_response({"ok": False, "error": f"invalid piid: {piid_int}"}, status=400)
        min_val, max_val = PIID_RANGES[piid_int]
        if not (min_val <= value_int <= max_val):
            return web.json_response({"ok": False, "error": f"value must be between {min_val} and {max_val}"}, status=400)
        result = await self.ble.send_command("set", (piid_int, value_int))
        self.invalidate_status_cache()
        return web.json_response(result)

    async def handle_port(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        port = body.get("port", "").lower()
        action = body.get("action", "").lower()
        if action not in ("on", "off"):
            return web.json_response({"ok": False, "error": "action must be on/off"}, status=400)
        if port not in PORT_BITS and port != "all":
            return web.json_response({"ok": False, "error": f"unknown port: {port}"}, status=400)
        result = await self.ble.send_command("port", (port, action))
        self.invalidate_status_cache()
        return web.json_response(result)

    async def handle_protocol(self, request):
        """处理协议开关 (PIID 21)。

        请求体:
          {"port": "c1", "protocol": "pd"}           # toggle
          {"port": "c1", "protocol": "pd", "action": "on"}   # 显式开关
          {"switches": {"c1": {"pd": true, ...}}}     # 批量设置
          {"value": 50532111}                         # 直接写原始值
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        try:
            state = self.ble.state
            if "value" in body:
                new_val = int(body["value"])
                if not (0 <= new_val <= 0xFFFFFFFF):
                    return web.json_response({"ok": False, "error": "value out of range (0-0xFFFFFFFF)"}, status=400)
            elif "switches" in body:
                new_val = ChargerState.encode_protocol_extend(body["switches"])
            elif "port" in body and "protocol" in body:
                port = body["port"].lower()
                proto = body["protocol"].lower()
                action = body.get("action", "toggle")

                # 加锁确保 read-modify-write-send 原子性，防止竞态
                async with state.lock:
                    current_val = state.protocol_extend
                    switches = dict(state.protocol_switches)

                    if port not in switches or proto not in switches[port]:
                        return web.json_response({"ok": False, "error": f"unknown {port}.{proto}"}, status=400)

                    cur = switches[port][proto]
                    if action == "toggle":
                        new_state = not cur
                    elif action == "on":
                        new_state = True
                    elif action == "off":
                        new_state = False
                    else:
                        return web.json_response({"ok": False, "error": f"invalid action: {action}"}, status=400)

                    _LOGGER.info("Protocol switch: %s.%s %s->%s (current 0x%08X)",
                                 port, proto, cur, new_state, current_val)

                    # 构建新的开关状态
                    new_switches = dict(switches)
                    new_switches[port] = dict(switches[port])
                    new_switches[port][proto] = new_state

                    new_val = ChargerState.encode_protocol_extend(new_switches)

                # 锁外发送 SET (send_command 内部也有 async 操作)
                result = await self.ble.send_command("set", (21, new_val))
                # 同步本地状态，确保后续 GET 读到最新值
                if result and result.get("ok"):
                    await state.update_protocol_extend(new_val)
                    self.emit_device_event(
                        "protocol", {"switches": state.protocol_switches,
                                     "protocol_extend": new_val})
                self.invalidate_status_cache()
                return web.json_response(result)
            else:
                return web.json_response({"ok": False, "error": "missing port/protocol or value"}, status=400)

            # 批量/原始值路径：锁外发送
            result = await self.ble.send_command("set", (21, new_val))
            if result and result.get("ok"):
                await state.update_protocol_extend(new_val)
                self.emit_device_event(
                    "protocol", {"switches": state.protocol_switches,
                                 "protocol_extend": new_val})
            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Protocol switch error: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_enable(self, request):
        """启停 BLE 连接（**唯一**需要动内线的端点，§3.2）。

        只调 `rt.supervisor.start()/stop()`：supervisor 是任务唯一持有者，直接
        cancel/create `rt.task` 会被 done 回调当成"意外退出"立刻重建。
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        enabled = body.get("enabled", True)
        # 设备由 middleware 解析（`/api/device/{id}/enable` 或 `?device=`）；
        # 老端点无参 → primary，行为与改造前一致。
        rt = self.current_runtime()
        if enabled:
            async with self._start_lock:
                if rt.supervisor.is_running:
                    return web.json_response({"ok": True, "enabled": True, "note": "already running"})
                rt.supervisor.start()
        else:
            async with self._start_lock:
                await rt.supervisor.stop()
                # _disconnect() (在 start() 的 finally 中) 已进行完整的 Bleak 清理，
                # 不需要额外调用 _force_disconnect_bluetooth()（仅用于错误恢复/关机）
            for piid in range(1, 5):
                await rt.state.update_port(piid, PORT_DEFAULT)
            if self.mqtt_client and self.mqtt_client.is_connected():
                # 清零用的是**本设备**的 topic（沿用 self.config.topic_* 会清掉
                # primary 的主题 —— §3.2 明确点名的 bug）。
                for piid, pname in PORT_NAMES.items():
                    self.mqtt_publish(rt.topics.port_topic(pname), PORT_DEFAULT)
                self.mqtt_publish(rt.topics.status, {"connected": False}, retain=True)
            else:
                _LOGGER.warning("MQTT not connected, port data not cleared via MQTT")
        self.invalidate_status_cache(rt.device_id)
        return web.json_response({"ok": True, "enabled": enabled})

    async def handle_log_level(self, request):
        """Get or set log level."""
        if request.method == "GET":
            current = logging.getLogger().level
            level_name = logging.getLevelName(current).lower()
            return web.json_response({
                "level": level_name,
                "available": list(LOG_LEVELS.keys()),
            })

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        level = data.get("level", "").lower()
        if level not in LOG_LEVELS:
            return web.json_response({"ok": False, "error": f"invalid level: {level}"}, status=400)

        logging.getLogger().setLevel(LOG_LEVELS[level])
        # Persist to config.yaml so the change survives restart
        try:
            import yaml
            config_path = self._config_path()
            cfg = {}
            if config_path.exists():
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
            if "server" not in cfg:
                cfg["server"] = {}
            cfg["server"]["log_level"] = level
            # Atomic write: write to temp file then rename
            tmp_path = config_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            os.replace(tmp_path, config_path)
        except Exception:
            _LOGGER.warning("Failed to persist log level to config.yaml", exc_info=True)
        _LOGGER.info("Log level changed to %s", level)
        return web.json_response({"ok": True, "level": level})

    async def handle_session_recording(self, request):
        """GET/POST /api/session-recording — 充电会话记录开关。

        状态持久化在 history.db 的 meta 表（单源，随数据库备份/清除），
        即时生效，无需重启服务。关闭后不再创建新的充电会话记录，但充电
        完成事件（MQTT charge_event，HA 通知）照常发布。
        """
        if request.method == "GET":
            return web.json_response({"ok": True, "enabled": bool(self.ble.record_sessions)})
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return web.json_response({"ok": False, "error": "enabled must be boolean"}, status=400)
        was_enabled = bool(self.ble.record_sessions)
        self.ble.record_sessions = enabled
        if self.history:
            self.history.set_session_recording(enabled)
        if enabled and not was_enabled:
            # 打开瞬间：把关闭期间仍在充电的占位会话立即转为真实记录，
            # 使正在充电的端口从此刻起恢复曲线/历史（无需等到下次充电）
            self.ble.resume_recording_sessions()
        self.invalidate_status_cache()
        _LOGGER.info("Charge session recording %s", "enabled" if enabled else "disabled")
        return web.json_response({"ok": True, "enabled": enabled})

    async def handle_port_modes(self, request):
        """GET/POST /api/port-modes — 端口模式：长期供电 / 充满即停。

        两者都持久化在 history.db 的 meta 表（单源，随数据库备份/清除），
        即时生效、无需重启。

        - permanent 长期供电设备：该端口不写充电曲线点（charge_points），曲线只
          留在内存滑动窗口里供详情浮层查看；会话行与耗能统计照常记录。
        - full_off 充满即停：复用会话检测的"判满"结果——20s/180s 两个功率中位数都低于
          T_low = max(0.05W, min(0.6W, 20%×本次会话峰值))，且该收敛状态连续保持
          HOLD_SEC=600s（期间偶发非收敛累计不超过 GRACE_SEC=120s；出现连续 ≥45s
          超过 max(5W, 2×T_low) 则视为"恢复充电"，计时清零重来）。判满后会话以
          low_power 自然结束，随即自动关闭该端口。和限额一样带模式：
          once 命中即消费（一次），always 每次会话重新生效。

        互斥：长期供电端口不允许开即停（否则把长期供电的负载断掉，语义自相矛盾）。
        """
        if request.method == "GET":
            state = self.ble.get_port_modes_state()
            state["ok"] = True
            return web.json_response(state)
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        port = str(data.get("port", "")).strip().lower()
        if port not in PORT_BITS:
            return web.json_response({"ok": False, "error": f"unknown port: {port}"}, status=400)
        permanent = data.get("permanent")
        full_off = data.get("full_off")
        if permanent is None and full_off is None:
            return web.json_response(
                {"ok": False, "error": "permanent or full_off required"}, status=400)
        for key, value in (("permanent", permanent), ("full_off", full_off)):
            if value is not None and not isinstance(value, bool):
                return web.json_response(
                    {"ok": False, "error": f"{key} must be boolean"}, status=400)

        current = self.ble.get_port_modes_state()
        permanent_ports = set(current["permanent_ports"])
        full_off_modes = dict(current["full_off_ports"])
        if permanent is not None:
            if permanent:
                permanent_ports.add(port)
                full_off_modes.pop(port, None)   # 互斥：设为长期供电即清掉该口即停
            else:
                permanent_ports.discard(port)
        if full_off is not None:
            if full_off and port in permanent_ports:
                return web.json_response(
                    {"ok": False,
                     "error": f"port {port} is permanent power, full_off is not applicable"},
                    status=400)
            if full_off:
                mode_raw = data.get("mode")
                if mode_raw is None:
                    full_off_modes[port] = self.ble.FULL_OFF_DEFAULT_MODE
                else:
                    mode = str(mode_raw).strip().lower()
                    if mode not in LIMIT_MODES:
                        return web.json_response(
                            {"ok": False, "error": f"mode must be one of {LIMIT_MODES}"},
                            status=400)
                    full_off_modes[port] = mode
            else:
                full_off_modes.pop(port, None)

        loop = asyncio.get_running_loop()
        if self.history:
            # 同步 sqlite 写放线程池，避免阻塞事件循环
            await loop.run_in_executor(
                None, self.history.set_permanent_ports, sorted(permanent_ports))
            await loop.run_in_executor(
                None, self.history.set_full_off, full_off_modes)
        # 长期供电端口不保留限额（与即停互斥同理）：登记为长期供电时把该口限额清零
        # 并落库——否则界面上"限额已设"与实际行为不一致（看着设了却永不断电）。
        # 必须在 set_permanent_ports 之前读快照：那个方法也会在内存里清零，先设就查不到。
        limits = self.ble.get_charge_limits_state()
        stale = [name for name in permanent_ports if limits.get(name, {}).get("wh", 0) > 0]
        if stale:
            merged = {name: {"wh": e["wh"], "mode": e["mode"]} for name, e in limits.items()}
            for name in stale:
                merged[name]["wh"] = 0.0
            if self.history:
                await loop.run_in_executor(None, self.history.set_charge_limits, merged)
            self.ble.set_charge_limits(merged)
            _LOGGER.info("Charge limits cleared for permanent power ports: %s", sorted(stale))
        self.ble.set_permanent_ports(permanent_ports)
        self.ble.set_full_off(full_off_modes)
        self.invalidate_status_cache()
        state = self.ble.get_port_modes_state()
        _LOGGER.info("Port modes updated: permanent=%s full_off=%s",
                     state["permanent_ports"], state["full_off_ports"])
        state["ok"] = True
        return web.json_response(state)

    async def handle_web_language(self, request):
        """GET/POST /api/web-language — Web UI language (DB meta, instant).

        GET 返回当前语言偏好（'auto' | 'zh-CN' | 'en'），POST 持久化到
        history.db 的 meta 表（单源，随数据库备份/清除），即时生效、无需重启。
        'auto' 表示跟随浏览器/系统语言（回退中文）。
        """
        if request.method == "GET":
            # 服务级 meta，不经设备 facade（§3.9：web_language 不属于任何设备）
            store = getattr(self, "history_store", None)
            lang = store.get_web_language() if store else "auto"
            return web.json_response({"ok": True, "language": lang}, headers=_NO_STORE)
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        raw = str(data.get("language", "")).strip().lower()
        if raw in ("zh", "zh-cn", "zh-hans"):
            canonical = "zh-CN"
        elif raw == "en":
            canonical = "en"
        elif raw == "auto":
            canonical = "auto"
        else:
            return web.json_response({"ok": False, "error": "invalid language"}, status=400)
        if self.history:
            store = getattr(self, "history_store", None)
            if store:      # 无 store 的手工装配（测试）→ 仅本次生效，不持久化
                store.set_web_language(canonical)
        _LOGGER.info("Web UI language set to %s", canonical)
        return web.json_response({"ok": True, "language": canonical}, headers=_NO_STORE)

    async def handle_charge_limits(self, request):
        """GET/POST /api/charge-limits — 指定充电量后自动关断端口。

        阈值单位是充电器输出能量（Wh，由 V×I 梯形积分得出），不是被充设备的
        实际充入电量——线损与设备内转换损耗使后者偏小。

        mode: once   达到阈值即消费清零（一次性）
              always 长期有效，每次充电会话重新武装

        GET 返回每端口配置 + 本会话实时进度（wh/mode/fired/session_wh/is_charging），
        前端据此显示"已充 X / 限额 Y Wh"并自算剩余。配置持久化在 history.db 的
        meta 表（单源，随数据库备份/清除），即时生效、无需重启。
        """
        if request.method == "GET":
            return web.json_response({
                "ok": True,
                "limits": self.ble.get_charge_limits_state(),
            })
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        if "limits" in data:
            raw = data["limits"]
            if not isinstance(raw, dict) or not raw:
                return web.json_response(
                    {"ok": False, "error": "limits must be a non-empty object"}, status=400)
            updates = raw
        elif "port" in data:
            port = str(data["port"]).strip().lower()
            if port not in PORT_BITS:
                return web.json_response({"ok": False, "error": f"unknown port: {port}"}, status=400)
            updates = {port: {"wh": data.get("wh"), "mode": data.get("mode")}}
        else:
            return web.json_response({"ok": False, "error": "missing port or limits"}, status=400)

        # 校验：任何非法项都应拒绝整个请求，而不是静默归一成"禁用"（否则用户
        # 以为设了限额，实际被封禁，属于危险的静默降级）。
        cleaned = {}
        for port, entry in updates.items():
            port = str(port).strip().lower()
            if port not in PORT_BITS:
                return web.json_response({"ok": False, "error": f"unknown port: {port}"}, status=400)
            wh_raw = entry.get("wh") if isinstance(entry, dict) else entry
            mode_raw = entry.get("mode") if isinstance(entry, dict) else None
            try:
                wh = float(wh_raw)
            except (TypeError, ValueError):
                return web.json_response(
                    {"ok": False, "error": f"port {port}: wh must be a number"}, status=400)
            if not math.isfinite(wh) or wh < 0 or wh > MAX_LIMIT_WH:
                return web.json_response(
                    {"ok": False, "error": f"port {port}: wh must be finite and within 0-{MAX_LIMIT_WH:g}"},
                    status=400)
            if wh > 0 and self.ble.is_permanent_name(port):
                return web.json_response(
                    {"ok": False,
                     "error": f"port {port} is permanent power, charge limit is not applicable"},
                    status=400)
            if mode_raw is None:
                mode = None   # 保留该端口既有 mode
            else:
                mode = str(mode_raw).strip().lower()
                if mode not in LIMIT_MODES:
                    return web.json_response(
                        {"ok": False, "error": f"port {port}: mode must be one of {LIMIT_MODES}"},
                        status=400)
            cleaned[port] = {"wh": wh} if mode is None else {"wh": wh, "mode": mode}

        # 合并既有配置（未提及的端口与 mode 保持原值），写库后应用到内存
        merged = {name: {"wh": e["wh"], "mode": e["mode"]}
                  for name, e in self.ble.get_charge_limits_state().items()}
        for port, entry in cleaned.items():
            merged[port]["wh"] = entry["wh"]
            if "mode" in entry:
                merged[port]["mode"] = entry["mode"]

        # 同步 sqlite 写：放到线程池，避免阻塞事件循环
        if self.history:
            await asyncio.get_running_loop().run_in_executor(
                None, self.history.set_charge_limits, merged)
        self.ble.set_charge_limits(merged)
        self.invalidate_status_cache()
        _LOGGER.info("Charge limits updated: %s",
                     {p: f'{e["wh"]:g}Wh/{e["mode"]}' for p, e in merged.items() if e["wh"] > 0})
        return web.json_response({
            "ok": True,
            "limits": self.ble.get_charge_limits_state(),
        })

    async def handle_chart(self, request):
        """Get chart-ready data for all ports with caching and ETag."""
        try:
            hours = min(float(request.query.get("hours", 1)), 720)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "invalid hours parameter"}, status=400)
        try:
            interval = max(int(request.query.get("interval", 30)), 5)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "invalid interval parameter"}, status=400)
        # 长区间强制更粗的桶：body 是整段 JSON，720h@30s 会生成 6.6MB（只是把同样的
        # 点画得更密）。先粗化再算 cache_key，保证缓存键反映真正生效的 interval。
        interval = max(interval, _chart_interval_floor(hours))
        # 缓存键加设备维度（§3.7）：`{device_id}:{hours}:{interval}`。
        # interval 仍在末段，运维/测试按 `:1800` 之类后缀匹配键的习惯不受影响。
        cache_key = f"{self.current_device_id()}:{hours}:{interval}"

        # Check cache
        now = time.time()
        entry = self._chart_cache.get(cache_key)
        if entry:
            cached_time, cached_etag, cached_body, _ = entry
            if now - cached_time < self._chart_cache_ttl:
                if_none_match = request.headers.get("If-None-Match")
                if if_none_match == cached_etag:
                    return web.Response(status=304)
                return web.Response(
                    body=cached_body,
                    content_type="application/json",
                    headers={"ETag": cached_etag},
                )

        # Generate data in thread pool (strftime + loops + json.dumps + sha256 are all CPU-bound)
        now_ts = time.time()
        start_ts = now_ts - hours * 3600
        aligned_start = (int(start_ts) // interval) * interval
        use_date = hours > 12
        aligned_now = (int(now_ts) // interval) * interval
        epochs = list(range(aligned_start, aligned_now, interval))
        # SQLite 查询为同步阻塞调用，放入线程池执行，避免阻塞事件循环
        raw_rows = await asyncio.to_thread(self.history.query_history_multi, 1, 4, hours, interval)

        def _build_chart(epochs_, labels, raw_rows_):
            port_data = {p: {} for p in range(1, 5)}
            for row in raw_rows_:
                port_data[row["port"]][int(row["bucket"])] = (
                    row["power"], row["voltage"], row["current"]
                )

            n = len(labels)
            power = [[0.0] * n for _ in range(5)]
            voltage = [[0.0] * n for _ in range(4)]
            current = [[0.0] * n for _ in range(4)]

            for i, epoch in enumerate(epochs_):
                total = 0.0
                for port in range(1, 5):
                    entry_ = port_data[port].get(epoch)
                    if entry_ is not None:
                        p, v, c = entry_
                        power[port - 1][i] = round(p, 1)
                        voltage[port - 1][i] = round(v, 2)
                        current[port - 1][i] = round(c, 2)
                        total += p
                power[4][i] = round(total, 1)

            port_names = ["C1", "C2", "C3", "A"]
            result = {
                "ok": True,
                "labels": labels,
                "datasets": {
                    "power": [{"label": port_names[p], "data": power[p]} for p in range(4)]
                           + [{"label": "Total", "data": power[4]}],
                    "voltage": [{"label": port_names[p], "data": voltage[p]} for p in range(4)],
                    "current": [{"label": port_names[p], "data": current[p]} for p in range(4)],
                },
            }
            body = json.dumps(result, ensure_ascii=False).encode()
            etag = hashlib.sha256(body).hexdigest()
            return body, etag

        if use_date:
            all_labels = [time.strftime('%m-%d %H:%M', time.localtime(t)) for t in epochs]
        else:
            all_labels = [time.strftime('%H:%M', time.localtime(t)) for t in epochs]

        body, etag = await asyncio.to_thread(_build_chart, epochs, all_labels, raw_rows)

        # 更新缓存：条数 + 总字节双重封顶（OrderedDict 按插入序淘汰最旧）。
        # 单个超大 body 直接不入缓存——缓存是加速用的，不值得为它留几 MB。
        max_entry = getattr(self, "_chart_cache_max_entry_bytes", CHART_CACHE_MAX_ENTRY_BYTES)
        max_bytes = getattr(self, "_chart_cache_max_bytes", CHART_CACHE_MAX_BYTES)
        if len(body) <= max_entry:
            self._chart_cache[cache_key] = (now, etag, body, now)
            # 覆盖已存在的键时 OrderedDict 会**保留原位置**，而"TTL 过期后重算同一个
            # cache_key"正是常见路径：不把它移到队尾，下面的淘汰就可能先删掉刚算好的
            # 这一条（之后同一图表每次请求都未命中，白跑 SQLite + json.dumps）。
            self._chart_cache.move_to_end(cache_key)
            while len(self._chart_cache) > self._chart_cache_max:
                self._chart_cache.popitem(last=False)
            # 至少保留刚写入的这一条，避免"单条就超总量上限"时把缓存清空
            while (len(self._chart_cache) > 1
                   and _chart_cache_bytes(self._chart_cache) > max_bytes):
                self._chart_cache.popitem(last=False)

        return web.Response(
            body=body,
            content_type="application/json",
            headers={"ETag": etag},
        )

    async def handle_statistics(self, request):
        """Get port statistics."""
        try:
            port = int(request.match_info.get("port", 1))
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        hours = min(float(request.query.get("hours", 24)), 720)

        if port not in range(1, 5):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)

        stats = await asyncio.to_thread(self.history.get_statistics, port, int(hours))
        return web.json_response({"ok": True, "data": stats})

    async def handle_export(self, request):
        """Export port history as CSV."""
        try:
            port = int(request.match_info.get("port", 1))
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        hours = min(float(request.query.get("hours", 24)), 720)

        if port not in range(1, 5):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)

        csv_data = await asyncio.to_thread(self.history.export_csv, port, int(hours))
        return web.Response(
            body=csv_data,
            content_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=port_{port}_history.csv"},
        )

    MOBILE_UA = re.compile(r'Android|iPhone|iPod|webOS|BlackBerry|Windows Phone', re.I)

    async def handle_index(self, request):
        ua = request.headers.get('User-Agent', '')
        path = '/phone.html' if self.MOBILE_UA.search(ua) else '/index.html'
        entry = _static_cache.get(path)
        if entry:
            accept_gzip = request.headers.get("Accept-Encoding", "").find("gzip") != -1
            if accept_gzip and entry["gzipped"]:
                body = entry["gzipped"]
                headers = {
                    "Content-Type": "text/html",
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-cache",
                }
            else:
                body = entry["raw"]
                headers = {
                    "Content-Type": "text/html",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-cache",
                }
            return web.Response(body=body, headers=headers)
        return web.FileResponse(WEB_DIR / path.lstrip('/'))

    # ── Charge Session API ──

    async def handle_sessions(self, request):
        """GET /api/sessions?port=c1&period=today&limit=10&page=1"""
        port_str = request.query.get("port", "")
        period = request.query.get("period", "today")
        limit = min(int(request.query.get("limit", "10")), 50)
        page = max(1, int(request.query.get("page", "1")))

        port = None
        if port_str:
            port_map = {"c1": 1, "c2": 2, "c3": 3, "a": 4}
            port = port_map.get(port_str)

        loop = asyncio.get_running_loop()
        sessions, total = await loop.run_in_executor(
            None, self.history.get_sessions, port, period, limit, (page - 1) * limit)

        # Merge live energy data for active sessions
        now = time.time()
        live = self.ble.get_live_session_data()
        db_session_ids = {s.get("id") for s in sessions}
        for port_id, ld in live.items():
            sid = ld.get("session_id")
            matched = False
            for s in sessions:
                if s.get("id") == sid:
                    s["total_wh"] = ld["session_wh"]
                    s["peak_power_w"] = max(s.get("peak_power_w", 0), ld["max_power"])
                    # Recalculate avg from live data (DB values are stale for active sessions)
                    start_time = ld.get("start_time") or now
                    dur_sec = max(1, int(now - start_time))
                    dur_h = dur_sec / 3600.0
                    s["avg_power_w"] = round(ld["session_wh"] / dur_h, 1) if dur_h > 0 else 0
                    port_state = self.ble.state.ports.get(port_id)
                    if port_state:
                        s["avg_voltage"] = round(port_state.voltage, 2)
                        s["avg_current"] = round(port_state.current, 2)
                    s["duration_sec"] = dur_sec
                    s["is_active"] = True
                    s["permanent"] = bool(ld.get("permanent"))
                    matched = True
                    break
            if not matched and sid:
                # Active session not in DB results (total_wh=0, filtered out) — add it
                start_time = ld.get("start_time") or now
                dur_sec = max(1, int(now - start_time))
                dur_h = dur_sec / 3600.0
                avg_p = round(ld["session_wh"] / dur_h, 1) if dur_h > 0 else 0
                # Current V/I from port state as approximate averages for active session
                port_state = self.ble.state.ports.get(port_id)
                avg_v = round(port_state.voltage, 2) if port_state else 0
                avg_i = round(port_state.current, 2) if port_state else 0
                sessions.insert(0, {
                    "id": sid, "port": port_id, "start_time": start_time,
                    "end_time": None, "total_wh": ld["session_wh"],
                    "avg_power_w": avg_p, "peak_power_w": ld["max_power"],
                    "avg_voltage": avg_v, "avg_current": avg_i,
                    "duration_sec": dur_sec,
                    "protocol": port_state.protocol if port_state else "", "is_active": True,
                    "permanent": bool(ld.get("permanent")),
                })
                total += 1

        # Mark unmatched sessions as inactive
        live_sids = {ld.get("session_id") for ld in live.values()}
        for s in sessions:
            if s.get("id") not in live_sids:
                s["is_active"] = False
            # 只有"正在供着电的常供会话"才mark：历史行可能是标记常供之前录的，
            # 那时曲线是照常落库的，标成常供会让详情看起来像"曲线丢了"。
            s["permanent"] = bool(s.get("permanent")) and bool(s.get("is_active"))
            if s["permanent"] and s.get("port") not in self.ble.permanent_ports:
                s["permanent"] = False

        return web.json_response({
            "sessions": sessions,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, (total + limit - 1) // limit),
        })

    async def handle_session_points(self, request):
        """GET /api/sessions/{id}/points?downsample=600[&window=3600]

        普通会话：DB 里的全量采样点（形状不变）。
        长期供电会话：曲线点不落库，改从内存窗口取；`window` 是窗口宽度（秒，
        缺省/<=0 = 内存保留时长 1 小时＝能给的都给了），`to` 是窗口右端
        （缺省=最新）。界面已不再提供窗口长度/滑动控件，这两个参数保留给
        脚本/调试按需取更窄的一段。两种会话都附带整个会话的 stats
        （能耗/峰值/时长），前端统计块一律用它——窗口只影响曲线，不影响统计。
        """
        sid = int(request.match_info["id"])
        target = int(request.query.get("downsample", "0"))
        # 默认窗口 = 内存保留时长（1 小时）：常供会话的曲线只在内存里留这么久，
        # 界面也不再提供"窗口长度"选择，所以缺省就应当是"能给的都给了"。
        # 读不到属性时退回 3600，绝不因为一个缺省值把整个接口打成 400。
        try:
            default_window = float(self.ble.PERMANENT_RETAIN_SEC)
        except (AttributeError, TypeError, ValueError):
            default_window = 3600.0
        try:
            window_sec = float(request.query.get("window", str(default_window)))
            to_raw = request.query.get("to")
            to_ts = float(to_raw) if to_raw else None
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "window/to must be numbers"}, status=400)
        if window_sec <= 0:
            window_sec = float(self.ble.PERMANENT_RETAIN_SEC)

        loop = asyncio.get_running_loop()
        # 归属校验先行：别的设备的 sid、以及根本不存在的 sid，一律 404。
        # 以前这里靠 `get_session_points` 的"非本设备 → 空列表"返回 200 + no_curve，
        # 前端与排障者看到的是"这次会话没有曲线"，而真实原因是"这不是你的会话"
        # ——和同组的 /sessions/{id}/export（404）自相矛盾（§3.9）。
        # 常供会话的曲线只在内存窗口里、可能还没有 DB 行，所以窗口命中就不判 404。
        session = await loop.run_in_executor(None, self.history.get_session, sid)
        win = self.ble.get_permanent_session_window(sid, window_sec, to_ts)
        if win is None and session is None:
            return web.json_response(
                {"ok": False, "error": "session not found"}, status=404)
        if win is not None:
            points = win["points"]
            if target > 0 and len(points) > target:
                from downsample import lttb_downsample
                points = await loop.run_in_executor(
                    None, lttb_downsample, points, target)
            return web.json_response({**win, "points": points})

        points = await loop.run_in_executor(
            None, self.history.get_session_points, sid)
        if target > 0 and len(points) > target:
            from downsample import lttb_downsample
            points = await loop.run_in_executor(
                None, lttb_downsample, points, target)

        resp = {"points": points}
        if not points:
            # 长期供电会话不落曲线点：明确告诉前端"是没存曲线"，不是加载失败
            resp["no_curve"] = True
        # 服务端是常供与否的唯一权威，且必须**每次都给出**（含 false）：
        # 端口中途退出常供后，前端若只"见到 true 才置位"会一直挂着旧标题与
        # "没有曲线"提示。DB 回落分支同样要回答这个问题。
        resp["permanent"] = bool(session and session.get("port") in self.ble.permanent_ports)
        if session:
            # 会话级统计（前端统计块优先用它，避免"只按窗口点现算"的偏差）
            duration = session.get("duration_sec") or 0
            if not session.get("end_time"):
                duration = max(1, int(time.time() - (session.get("start_time") or time.time())))
            resp["stats"] = {
                "session_id": sid,
                "port": session.get("port"),
                "start_time": session.get("start_time"),
                "end_time": session.get("end_time"),
                "total_wh": round(session.get("total_wh") or 0, 4),
                "avg_power_w": session.get("avg_power_w") or 0,
                "peak_power_w": session.get("peak_power_w") or 0,
                "avg_voltage": session.get("avg_voltage") or 0,
                "avg_current": session.get("avg_current") or 0,
                "duration_sec": duration,
            }
            if session.get("port") in self.ble.permanent_ports:
                # 常供端口不落曲线点：这里只有"活跃但内存窗口刚起步/已结束"两种情况，
                # 统计照给；permanent 标记由内存窗口分支负责，历史行不标。
                resp["window"] = None
        return web.json_response(resp)

    async def handle_session_export(self, request):
        """GET /api/sessions/{id}/export — 单个会话的采样点 CSV（会话详情浮层的"导出"）。

        与 /api/export/{port} 的区别：那个是"某端口 + 时间窗"的原始采样（含会话之间的
        空载片段），这个只导这一次会话。
        """
        try:
            sid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"ok": False, "error": "invalid session id"}, status=400)
        loop = asyncio.get_running_loop()
        csv_data = await loop.run_in_executor(None, self.history.export_session_csv, sid)
        if not csv_data:
            return web.json_response({"ok": False, "error": "session not found"}, status=404)
        return web.Response(
            body=csv_data,
            content_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=session_{sid}.csv"},
        )

    async def handle_energy_stats(self, request):
        """GET /api/energy/stats?period=today"""
        period = request.query.get("period", "today")
        loop = asyncio.get_running_loop()
        stats = await loop.run_in_executor(
            None, self.history.get_energy_stats, period)

        # Merge live data from active sessions
        now = time.time()
        live = self.ble.get_live_session_data()
        live_count = len(live)
        for port, ld in live.items():
            stats["total_wh"] = round(stats["total_wh"] + ld["session_wh"], 2)
            stats["peak_power_w"] = max(stats.get("peak_power_w", 0), ld["max_power"])
            # Add live duration for active sessions
            if ld.get("start_time"):
                live_duration = int(now - ld["start_time"])
                stats["total_duration_sec"] = stats.get("total_duration_sec", 0) + live_duration
            port_key = str(port)
            if port_key not in stats.get("by_port", {}):
                stats.setdefault("by_port", {})[port_key] = {"wh": 0, "count": 0}
            stats["by_port"][port_key]["wh"] = round(
                stats["by_port"][port_key]["wh"] + ld["session_wh"], 2)
            stats["by_port"][port_key]["count"] = stats["by_port"][port_key].get("count", 0) + 1
            stats["by_port"][port_key]["is_active"] = True

        # DB count excludes active sessions (total_wh=0), add them back
        stats["session_count"] = stats.get("session_count", 0) + live_count

        # Recalculate avg_power_w from total energy and total duration
        total_dur = stats.get("total_duration_sec", 0)
        total_wh = stats.get("total_wh", 0)
        stats["avg_power_w"] = round(total_wh / (total_dur / 3600), 1) if total_dur > 0 else 0

        return web.json_response(stats)

    async def handle_energy_protocols(self, request):
        """GET /api/energy/protocols?period=today

        按协议聚合电量/会话数（前端的"快充协议分布"视图）。走 SQL 聚合而不是让前端
        拉 /api/sessions 自己算：后者单页最多 50 条，今日就有 120+ 条会话，前端算出来的
        分布是错的（只有最近 50 条）。
        """
        period = request.query.get("period", "today")
        loop = asyncio.get_running_loop()
        stats = await loop.run_in_executor(
            None, self.history.get_protocol_stats, period)

        # 进行中的会话在 DB 里 total_wh=0（不会进聚合），用实时积分补上，
        # 口径与 handle_energy_stats 的 by_port 一致（两边合计值能对上）。
        live = self.ble.get_live_session_data()
        by_proto = {p["protocol"]: p for p in stats["protocols"]}
        for port, ld in live.items():
            port_state = self.ble.state.ports.get(port)
            proto = (port_state.protocol if port_state else "") or "unknown"
            entry = by_proto.get(proto)
            if entry is None:
                entry = {"protocol": proto, "wh": 0.0, "count": 0, "peak_w": 0.0, "is_active": True}
                stats["protocols"].append(entry)
                by_proto[proto] = entry
            entry["wh"] = round(entry["wh"] + ld["session_wh"], 2)
            entry["count"] += 1
            entry["peak_w"] = round(max(entry["peak_w"], ld["max_power"]), 1)
            entry["is_active"] = True
        if live:
            stats["protocols"].sort(key=lambda p: p["wh"], reverse=True)
            stats["total_wh"] = round(sum(p["wh"] for p in stats["protocols"]), 2)
            stats["session_count"] = sum(p["count"] for p in stats["protocols"])

        return web.json_response(stats)

    async def handle_sse(self, request):
        """GET /api/events — Server-Sent Events stream."""
        response = web.StreamResponse(
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        response.content_type = "text/event-stream"
        await response.prepare(request)

        queue: asyncio.Queue = asyncio.Queue(maxsize=SSEEmitter.MAX_QUEUE_SIZE)
        self.sse.add_client(queue)

        # Send full state on connect so client can initialize
        try:
            # 长连接期间不依赖请求上下文：设备列表用显式 registry 构造（§3.3 红线 2），
            # 且只在多设备时附加（§3.8：单设备事件流必须逐字节不变）。
            full_state = await self.state.to_dict()
            full_state["type"] = "init"
            full_state["mqtt_connected"] = self.mqtt_client is not None and self.mqtt_client.is_connected()
            if self.registry.multi:
                full_state["devices"] = [rt.describe() for rt in self.registry]
            await response.write(
                f"data: {json.dumps(full_state, ensure_ascii=False)}\n\n".encode()
            )
        except Exception as e:
            _sse_log.error("Failed to send SSE init event: %s", e)

        try:
            while True:
                # Send latest pending status before each event (stale status never delivered)
                # 多设备时每个设备的 status 各有 (queue, device) 分桶，逐个 flush；
                # 单设备时只有一个桶（device_id=None），与改造前完全一致。
                if self.registry.multi:
                    for rt in self.registry:
                        pending = self.sse.flush_status(queue, rt.device_id)
                        if pending:
                            await response.write(f"data: {pending}\n\n".encode())
                else:
                    status_msg = self.sse.flush_status(queue)
                    if status_msg:
                        await response.write(f"data: {status_msg}\n\n".encode())
                # Keepalive every 15s + event wait
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    await response.write(f"data: {msg}\n\n".encode())
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
        except asyncio.CancelledError:
            _sse_log.debug("SSE handler cancelled")
        except (ConnectionError, OSError, RuntimeError) as e:
            _sse_log.debug("SSE client disconnected (%s: %s)", type(e).__name__, e)
        except asyncio.TimeoutError:
            _sse_log.debug("SSE keepalive timeout, closing connection")
        except Exception as e:
            _sse_log.warning("SSE handler error: %s: %s", type(e).__name__, e)
        finally:
            self.sse.remove_client(queue)
            _sse_log.info("SSE client cleaned up (total: %d)", len(self.sse._clients))
        return response

    # ── Configuration API ──

    @staticmethod
    def _mask(val):
        """Mask sensitive value: show first 4 + **** + last 4."""
        if not val or len(val) <= 8:
            return "****"
        return val[:4] + "****" + val[-4:]

    async def handle_config_get(self, request):
        """GET /api/config — return current config with masked sensitive fields."""
        cfg = self.config
        return web.json_response({
            "ok": True,
            "config": {
                "ble": {
                    "mac": cfg.ble.mac,
                    "name": cfg.ble.name,
                    "token": self._mask(cfg.ble.token),
                    "ble_key": self._mask(cfg.ble.ble_key),
                    "scan_timeout": cfg.ble.scan_timeout,
                },
                "mqtt": {
                    "enabled": cfg.mqtt.enabled,
                    "host": cfg.mqtt.host,
                    "port": cfg.mqtt.port,
                    "username": cfg.mqtt.username,
                    "password": self._mask(cfg.mqtt.password),
                    "topic_prefix": cfg.mqtt.topic_prefix,
                },
                "bemfa": {
                    "enabled": cfg.bemfa.enabled,
                    "uid": self._mask(cfg.bemfa.uid),
                    "name_c1": cfg.bemfa.name_c1,
                    "name_c2": cfg.bemfa.name_c2,
                    "name_c3": cfg.bemfa.name_c3,
                    "name_a": cfg.bemfa.name_a,
                    "name_ble": cfg.bemfa.name_ble,
                    # 自定义语音实体表（§3.11）：前端据此画"一行一个开关"的表格
                    "entities": [{"name": name, "device": device, "port": port}
                                 for name, device, port in
                                 (bemfa_entity_triple(e) for e in (cfg.bemfa.entities or []))],
                    # 老配置（没写过 entities 键）要让前端知道"这空表是没配过，不是配了 0 条"
                    "entitiesDefined": bool(cfg.bemfa.entities_defined),
                    "ports": list(BEMFA_PORTS),
                    # 老配置的**派生清单**（`allowed_devices` × 5，服务端同一份实现）：
                    # 前端拿它当"首次保存"的种子，才不会把 `allowed_devices: ["*"]`
                    # 那些设备的开关丢掉。名字也由这里给，前端不再自己拼。
                    "derivedEntities": [
                        {"name": item["name"], "device": item["device_id"],
                         "port": item["port"]}
                        for item in self._derived_bemfa_plan()],
                    "allowedDevices": list(cfg.bemfa.allowed_devices or []),
                },
                "server": {
                    "port": cfg.server.port,
                    "log_level": cfg.server.log_level,
                    "history_retention_days": cfg.server.history_retention_days,
                },
                # 设备卡片数据源（§3.12 config.html）：primary 恒在第一位且
                # `is_primary=True`（前端据此禁用删除按钮）。敏感字段照旧打码。
                "devices": [{
                    "device_id": rt.device_id,
                    "name": rt.name,
                    "mac": rt.mac,
                    "token": self._mask(rt.token),
                    "ble_key": self._mask(rt.spec.ble_key),
                    "topic_prefix": rt.topics.prefix,
                    "is_primary": rt.device_id == PRIMARY_DEVICE_ID,
                } for rt in self.registry],
            },
        })

    async def handle_config_save(self, request):
        """POST /api/config — save config to config.yaml and restart."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        config_data = data.get("config", {})
        config_path = self._config_path()

        try:
            import yaml
            # Load existing config to preserve unknown fields
            existing = {}
            if config_path.exists():
                with open(config_path) as f:
                    existing = yaml.safe_load(f) or {}

            # Merge — skip masked placeholder values (****)
            SENSITIVE_KEYS = {"token", "ble_key", "password", "uid"}
            for section in ("ble", "mqtt", "bemfa", "server"):
                if section in config_data:
                    if section not in existing:
                        existing[section] = {}
                    for k, v in config_data[section].items():
                        if k in SENSITIVE_KEYS and v and "****" in str(v):
                            continue  # Skip masked values, keep original
                        existing[section][k] = v

            # Validate server.log_level value (prevent config corruption from frontend)
            if "server" in existing and "log_level" in existing["server"]:
                level = existing["server"]["log_level"]
                if level not in LOG_LEVELS:
                    _LOGGER.warning(
                        "Ignoring invalid log_level '%s' in config save, fallback to 'info'",
                        level,
                    )
                    existing["server"]["log_level"] = "info"

            # ── 设备列表（§3.10/§3.12）：前端发统一数组，后端决定文件结构 ──
            devices_payload = config_data.get("devices")
            if devices_payload is not None:
                error = self._apply_devices_payload(existing, devices_payload)
                if error:
                    return web.json_response({"ok": False, "error": error}, status=400)

            # 巴法云实体名/绑定变了 → 打 modified：云端主题的**显示名是注册时定的**
            # （主题本身只由 entity_id 派生），改名必须 delete+recreate 才生效。
            if "bemfa" in config_data:
                bemfa_existing = existing.get("bemfa", {})
                NAME_KEYS = ("name_c1", "name_c2", "name_c3", "name_a", "name_ble")
                old_bemfa: dict = {}
                if config_path.exists():
                    with open(config_path) as f:
                        old_bemfa = (yaml.safe_load(f) or {}).get("bemfa", {}) or {}
                old_view = ({k: old_bemfa.get(k) for k in NAME_KEYS},
                            _bemfa_entity_signature(old_bemfa.get("entities")))
                new_view = ({k: config_data["bemfa"].get(k) for k in NAME_KEYS},
                            _bemfa_entity_signature(config_data["bemfa"].get("entities")))
                if old_view != new_view:
                    bemfa_existing["modified"] = True

            # Atomic write: write to temp file then rename
            tmp_path = config_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)

            # ── 不变式：**保存 = 启动可接受**（§3.10）──
            # 落盘前用与启动**完全相同**的入口（load_config）把这份文件试算一遍。
            # 保存期的分节校验再全也可能漏掉某种组合，而漏掉的代价不是"保存失败"，
            # 是 _delayed_restart() 之后进程被 load_config 拒绝启动、Web UI 一起
            # 消失，用户只能手改 YAML 才能救回来。宁可现在 400，也不写一个起不来的文件。
            try:
                validated = load_config(tmp_path)
            except Exception as e:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                _LOGGER.error("Config rejected before write: %s", e)
                return web.json_response({"ok": False, "error": str(e)}, status=400)

            os.replace(tmp_path, config_path)

            # 设备被删掉后的残留：清它的 retained（否则 broker 上留下幽灵设备），
            # 但保留历史数据。放在重启之前——重启会断 MQTT，那时就发不出清了。
            kept_ids = {spec.device_id for spec in validated.device_specs()}
            removed = [rt for rt in self.registry if rt.device_id not in kept_ids]
            if removed:
                await self.purge_removed_devices(removed)
            # 设备**还在**但改了 topic 前缀：旧前缀的 retained 同样是幽灵，而且
            # clean_retained.py 从当前配置推导前缀，改完就再也推导不出旧的了。
            new_prefixes = {spec.device_id: validated.topics_for(spec).prefix
                            for spec in validated.device_specs()}
            for rt in self.registry:
                if rt.device_id in kept_ids:
                    new_prefix = new_prefixes.get(rt.device_id)
                    if new_prefix and new_prefix != rt.topics.prefix:
                        await self.purge_stale_prefix(rt, new_prefix)

            _LOGGER.info("Config saved to %s, restarting...", config_path)

            # Schedule restart after response (use asyncio.sleep instead of deprecated call_later+ensure_future)
            loop = asyncio.get_running_loop()
            loop.create_task(self._delayed_restart())

            return web.json_response({"ok": True, "message": "配置已保存，服务将在 1 秒后重启"})
        except ImportError:
            return web.json_response({"ok": False, "error": "yaml module not installed"}, status=500)
        except Exception as e:
            _LOGGER.error("Config save failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    #: `devices:` 节允许落盘的字段（与 `DeviceConfig.from_mapping` 对齐）
    _DEVICE_KEYS = ("id", "name", "mac", "token", "ble_key", "topic_prefix", "bemfa_ble")

    def _effective_sections(self, existing: dict) -> dict:
        """合成"本次要写入的那份配置"的有效 YAML 视图，用来跑启动校验。

        两个来源：**盘上待写的内容优先**（它含本次修改，比如换了 mqtt.topic_prefix），
        进程当前配置补齐缺项（前端拿不到明文凭证，`existing` 里通常没有 token/
        password——缺项不等于"没配"，照启动口径填上现在的有效值）。

        用 `asdict(self.config)` 而不是手抄字段清单：抄一份就多一处会漂移的地方，
        而这条校验的全部意义就是"与启动同一口径"。
        """
        from dataclasses import asdict
        merged = asdict(self.config)
        merged.pop("devices", None)          # 由调用方给"本次要落盘"的形态
        for section, values in (existing or {}).items():
            current = merged.get(section)
            if isinstance(values, dict) and isinstance(current, dict):
                current.update(values)
            else:
                merged[section] = values
        return merged

    def _apply_devices_payload(self, existing: dict, devices) -> str | None:
        """把前端的设备数组写回 yaml 结构；返回错误消息（None = 成功）。

        **两种形状都接受**（前端在演进，后端必须两种都吃得住）：

        A) 统一数组：每项带 `is_primary` 标记 —— 存的是"一组设备卡片"，后端决定
           文件结构（primary → `ble:` 节，其余 → `devices:` 节）；
        B) 前端当前形状：primary 已在 `config.ble` 里（走通用合并逻辑），
           `config.devices` 只放附加设备 —— 这里只负责把附加设备落成 `devices:` 节。

        结构约束（§3.10）：primary **永远**展开回 `ble:` 节（老版本工具/脚本仍能读）；
        primary 不可删除；写盘前用**生产校验规则**验一遍——多设备配错的代价是
        "控制到错误的充电器"，不能落盘再说。
        """
        if devices is None:
            return None                      # 前端没提交设备列表：保持 YAML 原样
        if not isinstance(devices, list):
            return "配置错误：设备列表格式非法（应为数组）"

        # ── 形状 A：统一数组（任一项带 `is_primary` 键即为 A 形；前端 B 形从不下发该键）──
        if any(isinstance(d, dict) and "is_primary" in d for d in devices):
            primaries = [d for d in devices if isinstance(d, dict) and d.get("is_primary")]
            if len(primaries) != 1:
                return "配置错误：必须且只能有一台 primary 设备（primary 不可删除）"
            primary = primaries[0]
            ble_section = dict(existing.get("ble") or {})
            for key in ("mac", "name", "token", "ble_key"):
                val = primary.get(key)
                if val is None:
                    continue
                if key in ("token", "ble_key") and ("****" in str(val)
                                                    or not str(val).strip()):
                    # 打码值 = 用户没改；空串 = 用户清空了输入框（页面拿不到明文，
                    # 清框是很容易的误操作）。两者都必须按 no-op 处理——凭证擦除
                    # 是不可逆的，救回来只能手改 YAML。B 形的 extras 一直是这么判的。
                    continue
                if key == "name" and not str(val).strip():
                    continue             # 清空名字按 no-op（宁可不变也不误删）
                ble_section[key] = val
            if "scan_timeout" in primary:
                ble_section["scan_timeout"] = primary["scan_timeout"]
            existing["ble"] = ble_section
            extras_in = [d for d in devices if not d.get("is_primary")]
        else:
            # ── 形状 B：附加设备已在 devices 数组里，primary 在 ble 段 ──
            extras_in = [d for d in devices if isinstance(d, dict)]

        # ── 附加设备：清洗字段 + 打码/空 token 继承 YAML 里的旧值 ──
        old_by_id = {}
        for entry in (existing.get("devices") or []):
            if isinstance(entry, dict) and entry.get("id"):
                old_by_id[str(entry["id"])] = entry

        extras = []
        for raw in extras_in:
            norm = dict(raw)
            if not norm.get("id") and norm.get("device_id"):
                norm["id"] = norm["device_id"]      # 两种键名都收（见 DeviceConfig.from_mapping）
            entry = {k: norm[k] for k in self._DEVICE_KEYS if norm.get(k) not in (None, "")}
            dev_id = str(entry.get("id") or "").strip()
            if dev_id:
                entry["id"] = dev_id
                prev = old_by_id.get(dev_id) or {}
                token = str(entry.get("token") or "")
                if not token or "****" in token:
                    # 空/打码 = "没改"：沿用 YAML 里的旧 token（页面拿不到明文，
                    # 不这样处理就只能逼用户把 token 重填一遍）
                    if prev.get("token"):
                        entry["token"] = prev["token"]
                    else:
                        entry.pop("token", None)
                ble_key = str(entry.get("ble_key") or "")
                if "****" in ble_key:
                    entry["ble_key"] = prev.get("ble_key", "")
                    if not entry["ble_key"]:
                        entry.pop("ble_key")
            extras.append(entry)

        # ── 必填项（与 `_parse_devices` 对齐）：缺了就明确报错，而不是写个半残 YAML ──
        for entry in extras:
            dev_id = entry.get("id") or ""
            if not dev_id:
                return "配置错误：附加设备缺少 id（id 即 MQTT 前缀后缀）"
            if not str(entry.get("mac") or "").strip():
                return f"配置错误：设备 `{dev_id}` 缺少 mac"
            if not str(entry.get("token") or "").strip():
                return f"配置错误：设备 `{dev_id}` 缺少 token（新增设备必须填 token）"

        # ── 用生产校验规则验一遍（id 规则 / MAC 全局唯一 / 前缀 / token hex）──
        #
        # 走 `config_from_mapping`（= `load_config` 的实现体），而不是拿进程启动时的
        # `self.config.mqtt` 拼一份临时 Config：同一份 payload 里既改 mqtt.topic_prefix
        # 又带设备前缀时，旧基前缀会让"前缀撞车"校验判成通过，写盘后重启才被拒——
        # 那时服务已经重启，Web UI 也没了，用户只能手改 YAML（§3.10 最怕的形态）。
        try:
            probe = self._effective_sections(existing)
            probe["devices"] = extras       # 校验的是**本次要落盘**的形态
            config_from_mapping(probe)
        except ValueError as e:
            return str(e)
        except Exception as e:      # 形如 token 不是 hex 之类
            return f"配置错误：{e}"

        if extras:
            existing["devices"] = extras
        else:
            existing.pop("devices", None)    # 删光附加设备后不留空节残留
        return None

    async def _delayed_restart(self):
        """Delay 1 second then restart."""
        await asyncio.sleep(1.0)
        await self._restart()

    async def _restart(self):
        """Gracefully restart the server by re-exec-ing the process."""
        _LOGGER.info("Restarting server...")
        s = get_server()
        # 先停全部 supervisor（防重建）→ 收尾连接 → 关共享 store → execv。
        # 顺序不能反：先拿掉任务持有者，否则停机过程中某台设备的"意外退出"回调
        # 会立刻把任务重建起来。
        for rt in s.registry:
            with contextlib.suppress(Exception):
                await rt.supervisor.stop()
        if s.mqtt_client:
            s.mqtt_client.loop_stop()
            s.mqtt_client.disconnect()
        if s.bemfa:
            try:
                await asyncio.wait_for(s.bemfa.stop(), timeout=3.0)
            except Exception:
                pass
        s.history_store.close()
        # Windows 下 os.execv 替换映像后 WinRT/bleak 的事件循环无法在新映像内重建:
        # HTTP 还能应答, 但 BLE 循环不再运转（connected 恒 false、无扫描日志）,
        # 只能手动触发才恢复。因此 win32 改为干净退出, 由拉起方(监听进程退出的
        # 桌面端)重新拉起; Linux/BlueZ 保持 os.execv 原语义。
        if sys.platform == "win32":
            _LOGGER.info("Restarting server (win32: clean exit, launcher will respawn)")
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            # 退出码取 1（非 0）而不是 0：本仓库的 systemd 单元用的是
            # Restart=on-failure，认证卡死的自愈路径也用 os._exit(1)，
            # 用 0 会被"只重启异常退出"的托管当成正常停止而不拉起，
            # 表现为"保存配置后服务就停了"。
            os._exit(1)
        os.execv(sys.executable, [sys.executable, str(Path(__file__).parent / "ha_server.py")])

    # ── Xiaomi Cloud API ──

    #: 等扫码的会话寿命（秒）
    XIAOMI_SCAN_TTL = 300
    #: 批量导入的整体预算（秒）。必须明显小于 30s 的请求上限（留出云调用的读超时
    #: 15s 与收尾时间）：超预算的设备逐台返回 `timeout`，而不是让浏览器等到 504。
    XIAOMI_IMPORT_BUDGET = 20.0

    def _xiaomi_session_lock(self, entry: dict) -> asyncio.Lock:
        """会话级互斥锁（懒创建）：一个 `requests.Session` 同时只能被一个线程用。"""
        lock = entry.get("lock")
        if lock is None:
            lock = asyncio.Lock()
            entry["lock"] = lock
        return lock

    #: 扫码完成后可以导入的窗口（秒）。每次导入都重新计时，够用户分批选设备；
    #: 别设太短：用户可能先去翻设备铭牌确认 MAC，回来时"会话已过期要重扫"很恼人。
    XIAOMI_IMPORT_TTL = 300

    def _arm_xiaomi_timer(self, session_id: str, ttl: float) -> None:
        """给会话（重）挂一个超时清理定时器。

        扫码完成时必须**重挂**而不是取消：等扫码的阶段已经过了，但会话里还留着
        云客户端与设备清单，没有定时器就等于永久泄漏（`requests.Session` + token）。
        """
        entry = self._xiaomi_sessions.get(session_id)
        if entry is None:
            return
        timer = entry.get("timer")
        if timer and not timer.cancelled():
            timer.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            entry["timer"] = None
            return
        entry["timer"] = loop.call_later(ttl, self._cleanup_xiaomi_session, session_id)

    def _cleanup_xiaomi_session(self, session_id: str):
        """超时清理：移除过期 Xiaomi session，释放相关资源。"""
        entry = self._xiaomi_sessions.pop(session_id, None)
        if entry:
            timer = entry.get("timer")
            if timer and not timer.cancelled():
                timer.cancel()
            _LOGGER.info("Xiaomi session %s expired, cleaned up", session_id[:8])

    async def handle_xiaomi_login(self, request):
        """POST /api/xiaomi/login — start QR code login, return QR URL."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        server = data.get("server", "cn").strip()
        if server not in ("cn", "de", "us", "ru", "tw", "sg", "in", "i2"):
            return web.json_response({"ok": False, "error": "无效的服务器区域"}, status=400)

        # 延迟导入：requests/pycryptodome 只在真正发起扫码登录时才加载
        try:
            QrCodeXiaomiCloudClient, XiaomiCloudLoginError = _load_xiaomi_cloud()
        except ImportError as e:
            _LOGGER.error("Xiaomi cloud client unavailable: %s", e)
            return web.json_response(
                {"ok": False, "error": f"小米云登录依赖缺失: {e}"}, status=503)

        try:
            import secrets as _secrets
            loop = asyncio.get_running_loop()

            def _start():
                client = QrCodeXiaomiCloudClient(server)
                return client.start_qr_login(), client

            result, client = await loop.run_in_executor(None, _start)
            session_id = _secrets.token_hex(16)
            # 等扫码的 5 分钟窗口；扫码完成后改挂"导入窗口"（见 qr_complete）
            self._xiaomi_sessions[session_id] = {
                "client": client, "timer": None, "devices": [],
            }
            self._arm_xiaomi_timer(session_id, self.XIAOMI_SCAN_TTL)

            return web.json_response({
                "ok": True,
                "qr_image": result["qr_image"],
                "login_url": result["login_url"],
                "session_id": session_id,
            })
        except XiaomiCloudLoginError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            _LOGGER.error("Xiaomi QR start failed: %s", e)
            return web.json_response({"ok": False, "error": f"启动失败: {e}"}, status=500)

    async def handle_xiaomi_qr_complete(self, request):
        """POST /api/xiaomi/qr/complete — long-poll for QR scan, return devices."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        session_id = data.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"ok": False, "error": "缺少 session_id"}, status=400)

        entry = self._xiaomi_sessions.get(session_id)
        if not entry:
            return web.json_response({"ok": False, "error": "会话已过期，请重新获取二维码"}, status=400)
        client = entry["client"]

        # 延迟导入（此时客户端实例已存在，模块通常已加载，仅取异常类型）
        try:
            XiaomiCloudLoginError = _load_xiaomi_cloud()[1]
        except ImportError as e:
            _LOGGER.error("Xiaomi cloud client unavailable: %s", e)
            return web.json_response(
                {"ok": False, "error": f"小米云登录依赖缺失: {e}"}, status=503)

        try:
            loop = asyncio.get_running_loop()

            def _complete():
                client.complete_qr_login()
                return client.get_devices()

            devices = await loop.run_in_executor(None, _complete)
            # 设备清单留在服务端会话里：批量导入时按 did 取 BLE Key，前端不必
            # 把 token 拿在手里回传（见 handle_xiaomi_import）。
            entry["devices"] = devices
            # 等扫码的阶段结束 → 改挂"导入窗口"定时器（不能取消，否则会话永久泄漏）
            self._arm_xiaomi_timer(session_id, self.XIAOMI_IMPORT_TTL)

            cuktech_devices = [d for d in devices if "njcuk" in d.model or "fitting" in d.model]
            all_devices = [{"did": d.did, "mac": d.mac, "token": d.token,
                            "name": d.name, "model": d.model} for d in devices]

            return web.json_response({
                "ok": True,
                "devices": all_devices,
                "cuktech": [{"did": d.did, "mac": d.mac, "token": d.token,
                             "name": d.name, "model": d.model} for d in cuktech_devices],
            })
        except XiaomiCloudLoginError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            _LOGGER.error("Xiaomi QR complete failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_xiaomi_beaconkey(self, request):
        """POST /api/xiaomi/beaconkey — 取**一台**设备的 BLE Key。

        保留给脚本/调试；配置页已改走 `/api/xiaomi/import`（批量 + 名称同步）。
        会话不再用完即弃：导入窗口内可以继续取其它设备（到期由定时器回收）。
        """
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        session_id = str(data.get("session_id", "")).strip()
        did = str(data.get("did", "")).strip()

        if not session_id or not did:
            return web.json_response({"ok": False, "error": "参数不完整"}, status=400)

        entry = self._xiaomi_sessions.get(session_id)
        if not entry:
            return web.json_response({"ok": False, "error": "会话已过期，请重新扫码"}, status=400)

        try:
            XiaomiCloudLoginError = _load_xiaomi_cloud()[1]
        except ImportError as e:
            _LOGGER.error("Xiaomi cloud client unavailable: %s", e)
            return web.json_response(
                {"ok": False, "error": f"小米云登录依赖缺失: {e}"}, status=503)

        try:
            async with self._xiaomi_session_lock(entry):
                ble_key = await asyncio.get_running_loop().run_in_executor(
                    None, entry["client"].get_beaconkey, did)
            self._arm_xiaomi_timer(session_id, self.XIAOMI_IMPORT_TTL)
            if ble_key:
                return web.json_response({"ok": True, "ble_key": ble_key})
            return web.json_response({"ok": False, "error": "未找到 BLE Key"}, status=404)
        except XiaomiCloudLoginError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            _LOGGER.error("Get beaconkey failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_xiaomi_import(self, request):
        """POST /api/xiaomi/import — 一次导入**多台**设备（名称 + token + BLE Key）。

        为什么必须是批量端点：BLE Key 只能**按 did 单取**
        （`blt_get_beaconkey`），多台设备就是多次云调用。让前端逐台循环发请求意味着
        N 次网络往返 + N 次会话过期风险，而且要由前端把 token 拿在手里再回传——
        凭据没必要离开服务端。这里一次调用、服务端内循环、逐台报错。

        请求：`{"session_id": "...", "dids": ["did1", "did2"]}`
        响应：`{"ok": true, "devices": [{did, name, mac, token, model, ble_key} | {did, error}]}`

        单台失败**不影响**其它台（返回逐台结果，前端据此报告"N 台新增 / M 台失败"）。
        """
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        session_id = str(data.get("session_id", "")).strip()
        dids = data.get("dids")
        if isinstance(dids, str):
            dids = [dids]
        if not session_id:
            return web.json_response({"ok": False, "error": "缺少 session_id"}, status=400)
        if not isinstance(dids, list) or not dids:
            return web.json_response({"ok": False, "error": "请至少选择一台设备"}, status=400)
        dids = [str(d).strip() for d in dids if str(d).strip()]
        if not dids:
            return web.json_response({"ok": False, "error": "请至少选择一台设备"}, status=400)

        entry = self._xiaomi_sessions.get(session_id)
        if not entry:
            return web.json_response({"ok": False, "error": "会话已过期，请重新扫码"}, status=400)

        try:
            XiaomiCloudLoginError = _load_xiaomi_cloud()[1]
        except ImportError as e:
            _LOGGER.error("Xiaomi cloud client unavailable: %s", e)
            return web.json_response(
                {"ok": False, "error": f"小米云登录依赖缺失: {e}"}, status=503)

        known = {d.did: d for d in (entry.get("devices") or [])}
        client = entry["client"]

        # 截止时间：HTTP 请求本身有 30s 上限（request_timeout_middleware），批量导入
        # 必须在它之前收尾，否则浏览器拿到 504 而线程还在跑——那正是"无超时网络调用
        # 被放大 N 倍"的形态。
        #
        # 只判"下一发之前还剩多少"是不够的：已经发出去的那一发照样能跑满它自己的
        # 超时（连接 5s + 读取 15s），于是总耗时 = 预算 + 一发的最坏值（20+20=40s > 30s）。
        # 所以**把剩余预算当成这一发的超时下发**，让"最后一发"也不可能超支。
        budget = self.XIAOMI_IMPORT_BUDGET
        started = time.monotonic()

        def _remaining() -> float:
            return budget - (time.monotonic() - started)

        def _fetch_all():
            out = []
            for did in dids:
                dev = known.get(did)
                if dev is None:
                    # 不在本次扫码拿到的清单里（伪造的 did / 清单已过期）：明确报错，
                    # 不要拿个空 token 让配置页把设备写残。
                    out.append({"did": did, "error": "device_not_in_list"})
                    continue
                left = _remaining()
                if left <= 0.5:            # 0.5s 以内不值得再开一发
                    out.append({"did": did, "name": dev.name, "mac": dev.mac,
                                "model": dev.model, "error": "timeout"})
                    continue
                try:
                    key = client.get_beaconkey(did, timeout=min(left, 15.0)) or ""
                except Exception as e:      # 单台失败不能拖垮整批
                    out.append({"did": did, "name": dev.name, "mac": dev.mac,
                                "model": dev.model, "error": str(e)})
                    continue
                out.append({"did": dev.did, "name": dev.name, "mac": dev.mac,
                            "token": dev.token, "model": dev.model, "ble_key": key})
            return out

        # `requests.Session` 不是线程安全的：两个标签页同时点导入（或边导入边取单台
        # key）就是两个线程操作同一个 Session。按会话串行化——批量导入本来就该独占。
        lock = self._xiaomi_session_lock(entry)
        async with lock:
            try:
                results = await asyncio.get_running_loop().run_in_executor(None, _fetch_all)
            except XiaomiCloudLoginError as e:
                return web.json_response({"ok": False, "error": str(e)}, status=400)
            except Exception as e:
                _LOGGER.error("Xiaomi import failed: %s", e)
                return web.json_response({"ok": False, "error": str(e)}, status=500)

        # 每次导入重挂窗口：允许用户分两批选设备，都算同一次登录
        self._arm_xiaomi_timer(session_id, self.XIAOMI_IMPORT_TTL)
        ok_count = len([r for r in results if not r.get("error")])
        _LOGGER.info("Xiaomi import: %d/%d device(s) resolved", ok_count, len(results))
        return web.json_response({"ok": True, "devices": results})


WEB_DIR = Path(__file__).parent / "web"
_server = None

# ── 静态文件缓存（启动时预加载 + 预压缩） ──
_static_cache = {}
_GZIP_TYPES = (".js", ".css", ".html", ".svg", ".json", ".txt")


def _static_cache_key(static_dir, fpath) -> str:
    """构造静态缓存 key (/static/<相对路径>)。

    用 .as_posix() 统一为正斜杠：Windows 上 Path.relative_to() 返回的路径用
    反斜杠（如 plugin_imgs\\logo.png），与浏览器请求的 /static/plugin_imgs/logo.png
    不匹配，handle_cached_static 会查不到缓存而返回 404。根目录文件不受影响，
    子目录文件（plugin_imgs/*、locales/* 等）必须用本函数。
    """
    return "/static/" + fpath.relative_to(static_dir).as_posix()


def _cache_static_files():
    """递归扫描 static 目录，预加载并预压缩所有文件到内存。"""
    static_dir = WEB_DIR / "static"
    if not static_dir.is_dir():
        return
    for fpath in static_dir.rglob("*"):
        if not fpath.is_file():
            continue
        key = _static_cache_key(static_dir, fpath)
        raw = fpath.read_bytes()
        ext = fpath.suffix.lower()
        # 按后缀推断 content-type
        mime_map = {
            ".js": "application/javascript",
            ".css": "text/css",
            ".html": "text/html",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".svg": "image/svg+xml",
            ".json": "application/json",
            ".txt": "text/plain",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".webp": "image/webp",
        }
        ct = mime_map.get(ext, "application/octet-stream")
        # 预计算 gzip 版本
        gzipped = None
        if ext in _GZIP_TYPES and len(raw) >= 1024:
            compressed = gzip.compress(raw)
            if len(compressed) < len(raw):
                gzipped = compressed
        _static_cache[key] = {
            "raw": raw,
            "gzipped": gzipped,
            "content_type": ct,
        }
    # 根目录 HTML 文件也加入缓存
    for html_name in ("index.html", "phone.html", "config.html"):
        html_path = WEB_DIR / html_name
        if html_path.is_file():
            raw = html_path.read_bytes()
            compressed = gzip.compress(raw)
            gzipped = compressed if len(compressed) < len(raw) else None
            _static_cache[f"/{html_name}"] = {
                "raw": raw,
                "gzipped": gzipped,
                "content_type": "text/html",
            }


async def handle_cached_static(request):
    """从内存缓存响应静态文件，避免磁盘 I/O 和运行时 gzip。"""
    entry = _static_cache.get(request.path)
    if entry is None:
        raise web.HTTPNotFound()
    accept_gzip = request.headers.get("Accept-Encoding", "").find("gzip") != -1
    if accept_gzip and entry["gzipped"]:
        body = entry["gzipped"]
        headers = {
            "Content-Type": entry["content_type"],
            "Content-Encoding": "gzip",
            "Content-Length": str(len(body)),
            "Cache-Control": ("no-cache" if entry["content_type"] == "text/html" else "public, max-age=604800, immutable"),
        }
    else:
        body = entry["raw"]
        headers = {
            "Content-Type": entry["content_type"],
            "Content-Length": str(len(body)),
            "Cache-Control": ("no-cache" if entry["content_type"] == "text/html" else "public, max-age=604800, immutable"),
        }
    return web.Response(body=body, headers=headers)


def get_server():
    """获取全局 Server 单例 (惰性初始化)。"""
    global _server
    if _server is None:
        _server = Server()
    return _server


def reset_server():
    """重置全局 Server 单例 (仅用于测试)。"""
    global _server
    _server = None


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    origin = request.headers.get("Origin", "")
    s = get_server()
    allowed_origins = {
        f"http://localhost:{s.config.server.port}",
        f"http://127.0.0.1:{s.config.server.port}",
    }
    if origin and origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@web.middleware
async def request_size_limit_middleware(request, handler):
    """Reject requests with body exceeding MAX_REQUEST_BODY_SIZE."""
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BODY_SIZE:
                    return web.json_response(
                        {"ok": False, "error": "Request body too large"},
                        status=413,
                    )
            except ValueError:
                pass
    return await handler(request)


@web.middleware
async def request_timeout_middleware(request, handler):
    """Apply a per-request timeout (30s for API).

    SSE（/api/events）是长连接，**不设超时**：原先给它 120s 超时，服务端每 2 分钟
    主动掐断一次，浏览器只能不停重连（真机日志 32.5 小时里 145 次连接 / 157 次断开）。
    连接寿命改由 handle_sse 自己维持（keepalive 心跳 + 客户端断开时退出）。
    """
    if request.path == "/api/events":
        return await handler(request)
    if request.path.startswith("/api/"):
        timeout = 30.0
    else:
        # Static files / HTML — no timeout
        return await handler(request)
    try:
        return await asyncio.wait_for(handler(request), timeout=timeout)
    except asyncio.TimeoutError:
        _LOGGER.warning("Request timeout: %s %s", request.method, request.path)
        return web.json_response(
            {"ok": False, "error": "Request timeout"},
            status=504,
        )


@web.middleware
async def gzip_middleware(request, handler):
    response = await handler(request)
    # 已预压缩的静态文件（Content-Encoding 已设置）跳过
    if response.headers.get("Content-Encoding") == "gzip":
        return response
    if not hasattr(response, 'body'):
        return response
    if request.headers.get("Accept-Encoding", "").find("gzip") == -1:
        return response
    if response.content_length is not None and response.content_length < 1024:
        return response
    if response.content_type and "text" not in response.content_type and "json" not in response.content_type:
        return response
    body = response.body
    if isinstance(body, bytes) and len(body) >= 1024:
        compressed = await asyncio.to_thread(gzip.compress, body)
        if len(compressed) < len(body):
            response.body = compressed
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
    return response


@web.middleware
async def cache_middleware(request, handler):
    response = await handler(request)
    # 已由 handle_cached_static / handle_index 设置缓存头的文件跳过
    if response.headers.get("Cache-Control"):
        return response
    if request.path.startswith("/static/"):
        if request.path.endswith((".js", ".css", ".png", ".ico", ".woff", ".woff2")):
            if os.environ.get("CUKTECH_ENV") == "development":
                response.headers["Cache-Control"] = "no-cache"
            else:
                response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response


@web.middleware
async def device_middleware(request, handler):
    """把"请求指向哪台设备"解析成 ContextVar（§3.3 方案 A，安全相关）。

    两种寻址都支持（§3.12 决策 3）：路径 `/api/device/{id}/...`（HA 集成用，404 可探测
    回退）与 `?device=` 查询参数（Web UI 用，对现有 URL 拼接最友好）。
    无参请求不设上下文 → 落到 primary，老端点的行为因此完全不变。

    并发安全：aiohttp 每个请求独立 task，ContextVar 是 context 局部——请求之间互不
    可见，不存在"B 改了 A 的设备"这种交错。
    """
    dev = request.match_info.get("device_id") or request.query.get("device")
    if not dev:
        return await handler(request)
    rt = get_server().registry.get(dev)
    if rt is None:
        return web.json_response({"ok": False, "error": "device_not_found"}, status=404)
    token = _current_rt.set(rt)
    try:
        return await handler(request)
    finally:
        _current_rt.reset(token)


app = web.Application(middlewares=[
    request_size_limit_middleware,
    cors_middleware,
    gzip_middleware,
    cache_middleware,
    request_timeout_middleware,
    # 放在最后 = 最内层：设备解析必须在其它中间件之后、handler 之前生效
    device_middleware,
])
app.router.add_get("/", lambda r: get_server().handle_index(r))
app.router.add_get("/phone.html", handle_cached_static)
app.router.add_get("/config.html", handle_cached_static)
app.router.add_get("/static/{tail:.*}", handle_cached_static)
app.router.add_get("/api/health", lambda r: get_server().handle_health(r))
app.router.add_get("/api/status", lambda r: get_server().handle_status(r))
app.router.add_post("/api/set", lambda r: get_server().handle_set(r))
app.router.add_post("/api/port", lambda r: get_server().handle_port(r))
app.router.add_post("/api/enable", lambda r: get_server().handle_enable(r))
app.router.add_post("/api/protocol", lambda r: get_server().handle_protocol(r))
app.router.add_get("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_post("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_get("/api/session-recording", lambda r: get_server().handle_session_recording(r))
app.router.add_post("/api/session-recording", lambda r: get_server().handle_session_recording(r))
app.router.add_get("/api/web-language", lambda r: get_server().handle_web_language(r))
app.router.add_post("/api/web-language", lambda r: get_server().handle_web_language(r))
app.router.add_get("/api/charge-limits", lambda r: get_server().handle_charge_limits(r))
app.router.add_post("/api/charge-limits", lambda r: get_server().handle_charge_limits(r))
app.router.add_get("/api/port-modes", lambda r: get_server().handle_port_modes(r))
app.router.add_post("/api/port-modes", lambda r: get_server().handle_port_modes(r))
app.router.add_get("/api/chart", lambda r: get_server().handle_chart(r))
app.router.add_get("/api/statistics/{port}", lambda r: get_server().handle_statistics(r))
app.router.add_get("/api/export/{port}", lambda r: get_server().handle_export(r))
app.router.add_get("/api/bemfa", lambda r: get_server().handle_bemfa(r))
app.router.add_get("/api/sessions", lambda r: get_server().handle_sessions(r))
app.router.add_get("/api/sessions/{id}/points", lambda r: get_server().handle_session_points(r))
app.router.add_get("/api/sessions/{id}/export", lambda r: get_server().handle_session_export(r))
app.router.add_get("/api/energy/stats", lambda r: get_server().handle_energy_stats(r))
app.router.add_get("/api/energy/protocols", lambda r: get_server().handle_energy_protocols(r))
app.router.add_get("/api/events", lambda r: get_server().handle_sse(r))
app.router.add_get("/api/config", lambda r: get_server().handle_config_get(r))
app.router.add_post("/api/config", lambda r: get_server().handle_config_save(r))
app.router.add_post("/api/xiaomi/login", lambda r: get_server().handle_xiaomi_login(r))
app.router.add_post("/api/xiaomi/qr/complete", lambda r: get_server().handle_xiaomi_qr_complete(r))
app.router.add_post("/api/xiaomi/beaconkey", lambda r: get_server().handle_xiaomi_beaconkey(r))
app.router.add_post("/api/xiaomi/import", lambda r: get_server().handle_xiaomi_import(r))

# ── 多设备寻址（§3.7）：同一组 handler 的第二种挂载，**零 handler 复制** ──
#
# 新路径不是 20 个复制品：路由表把同一批 handler 再挂一遍，设备解析统一由
# `device_middleware` 完成（未知名 404），handler 与其辅助方法经 property 自动取
# 当前设备，对路由无感。老端点一条不动，响应形状不变。
app.router.add_get("/api/devices", lambda r: get_server().handle_devices(r))

_DEVICE_SCOPED_ROUTES = (
    ("GET", "/status", "handle_status"),
    ("POST", "/set", "handle_set"),
    ("POST", "/port", "handle_port"),
    ("POST", "/enable", "handle_enable"),
    ("POST", "/protocol", "handle_protocol"),
    ("GET", "/session-recording", "handle_session_recording"),
    ("POST", "/session-recording", "handle_session_recording"),
    ("GET", "/charge-limits", "handle_charge_limits"),
    ("POST", "/charge-limits", "handle_charge_limits"),
    ("GET", "/port-modes", "handle_port_modes"),
    ("POST", "/port-modes", "handle_port_modes"),
    ("GET", "/chart", "handle_chart"),
    ("GET", "/statistics/{port}", "handle_statistics"),
    ("GET", "/export/{port}", "handle_export"),
    ("GET", "/sessions", "handle_sessions"),
    ("GET", "/sessions/{id}/points", "handle_session_points"),
    ("GET", "/sessions/{id}/export", "handle_session_export"),
    ("GET", "/energy/stats", "handle_energy_stats"),
    ("GET", "/energy/protocols", "handle_energy_protocols"),
)

for _method, _path, _handler_name in _DEVICE_SCOPED_ROUTES:
    app.router.add_route(
        _method, f"/api/device/{{device_id}}{_path}",
        (lambda name: (lambda r: getattr(get_server(), name)(r)))(_handler_name),
    )


async def on_startup(app_):
    _cache_static_files()
    _LOGGER.info("Static files cached: %d files (%.1f KB raw / %.1f KB gzip)",
                 len(_static_cache),
                 sum(len(v["raw"]) for v in _static_cache.values()) / 1024,
                 sum(len(v["gzipped"] or v["raw"]) for v in _static_cache.values()) / 1024)
    s = get_server()
    async with s._start_lock:
        s.loop = asyncio.get_running_loop()
        # 模块级 shim 只服务 primary/测试（§3.5：保留为兼容 shim）；每台设备另有
        # 自己的实例钩子，在 mount_device_hooks 里注册。
        set_status_cache_invalidator(s.invalidate_status_cache)
        s.history_store.connect()
        # connect() 内部已经以 `close_stale_sessions(None)`（全设备）收尾了上次强杀/
        # 崩溃遗留的未闭合会话，这里不再重复调用：清理只有一个入口。
        stagger = float(s.adapter.startup_stagger_sec or 0.0)
        for index, rt in enumerate(s.registry):
            # 10 项钩子，见 mount_device_hooks —— 缺一项设备 2 就残废
            s.mount_device_hooks(rt)
            # 启动错峰（§3.4）：N 台同时上电时的认证风暴防护，成本为零
            rt.supervisor.set_startup_delay(stagger * index)
        await s.setup_mqtt()
        if s.mqtt_client:
            s.setup_mqtt_subscriptions()
        if s.config.bemfa.enabled:
            await s.setup_bemfa()
        for rt in s.registry:
            # rt.task 只由 supervisor 创建/持有（取代原 Server._ble_task）
            rt.supervisor.start()
        # 回填历史会话的均压/均流：旧版本闭合会话时落库的是"断电瞬间"的瞬时值
        # (≈0)，导致历史摘要恒为 0。有采样点的已闭合会话重算一次（全设备一次跑完）。
        # 放在 MQTT/BLE 起来之后再跑（fire-and-forget），避免维护性任务拖慢就绪；
        # 修正值只被历史接口消费，晚几秒无影响。
        async def _backfill_session_avg():
            try:
                fixed = await asyncio.get_running_loop().run_in_executor(
                    None, s.history_store.backfill_session_avg_vi)
                if fixed:
                    _LOGGER.info("Session avg backfill done: %d sessions", fixed)
            except Exception as e:
                _LOGGER.warning("Session avg backfill failed: %s", e)

        s._backfill_task = asyncio.create_task(_backfill_session_avg())


async def on_shutdown(app_):
    _LOGGER.info("Shutting down...")
    s = get_server()
    # ① 先停全部 supervisor（防重建）：不先拿掉任务持有者，下面收尾期间任何一次
    #    "意外退出"都会立刻把任务建回来，收尾永远收不干净。
    #    supervisor.stop() 内部 request_stop() 会以 shutdown 原因关闭活跃会话。
    for rt in s.registry:
        with contextlib.suppress(Exception):
            await rt.supervisor.stop()
    # ② 兜底再收一次：必须标注 shutdown 原因 —— 只有"用户意图终止"（端口关闭/
    #    拔出/充电自然结束）才消费 once 限额，服务停止不是用户意图终止。
    for rt in s.registry:
        with contextlib.suppress(Exception):
            rt.manager.close_active_sessions(END_REASON_SHUTDOWN)
    if s.bemfa:
        try:
            await asyncio.wait_for(s.bemfa.stop(), timeout=3.0)
        except Exception:
            pass
    # ③ 回填任务同样要收尾：它跑在 executor 线程里访问 history 连接，
    # 若不取消/等待就 close() 连接，会与在途查询/写入竞争（报错且工作静默丢弃，
    # 事件循环还可能报 "Task was destroyed but it is pending"）。
    backfill_task = s._backfill_task
    if backfill_task and not backfill_task.done():
        backfill_task.cancel()
        try:
            await backfill_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _LOGGER.warning("Backfill task error during shutdown: %s", e)
    if s.mqtt_client:
        s.mqtt_client.loop_stop()
        s.mqtt_client.disconnect()
    # ④ 最后关共享 store（N 台设备的唯一写者）
    s.history_store.close()


app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    s = get_server()
    web.run_app(app, host="0.0.0.0", port=s.config.server.port)
