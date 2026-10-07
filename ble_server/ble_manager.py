"""CUKTECH BLE Server - BLE connection manager with auto-reconnect."""
import asyncio
import logging
import sys
import os
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

try:
    from cuktech_ble.controller import CuktechBLEController, CHAR_CMD_RECV, CHAR_FW_VERSION, AuthConnectionError
    from cuktech_ble.protocol import READABLE_SETTINGS_PIIDS, UUID_FE95, mac_str_to_bytes
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
    from cuktech_ble.controller import CuktechBLEController, CHAR_CMD_RECV, CHAR_FW_VERSION, AuthConnectionError
    from cuktech_ble.protocol import READABLE_SETTINGS_PIIDS, UUID_FE95, mac_str_to_bytes

from state import ChargerState, PORT_NAMES, PORT_BITS, PORT_DEFAULT, decode_port, decode_pdo_caps
from energy import (limit_reached, normalize_charge_limit, DEFAULT_LIMIT_MODE,
                    LIMIT_MODE_ONCE)

_LOGGER = logging.getLogger("cuktech_ble")

# 端口名 -> PIID（PORT_NAMES 的反向映射，限额配置以稳定端口名对外）
PORT_IDS = {name: piid for piid, name in PORT_NAMES.items()}

# 会话终止原因：决定 once 限额是否被消费（见 _release_limit）
END_REASON_USER_OFF = "port_off"      # 用户/限额触发的端口关闭
END_REASON_UNPLUG = "unplug"          # 拔出负载（V=0,I=0 主动探测确认）
END_REASON_LOW_POWER = "low_power"    # 低功率收敛自然结束（充满/维持态）
END_REASON_NO_LOAD = "no_load"        # 端口无负载但仍在协商电压（设备充满后不吸电）
                                      # —— 同样算"自然结束"（不再是 USER_OFF），但自动断电
                                      #    要等它持续够久（NO_LOAD_ARM_SEC）才武装，避免把
                                      #    自适应充电的短暂停顿当成充满。
END_REASON_LINK_LOSS = "link_loss"    # BLE 链路中断/重连（基础设施，会话可续）
END_REASON_SHUTDOWN = "shutdown"      # 服务停止/关机
END_REASON_UNKNOWN = "unknown"        # 未标注原因（保守：视为真实终止）

# 这些原因不清零 once 限额——会话并非用户意图终止，保留用户刚设的限制。
END_REASONS_PRESERVING_LIMIT = frozenset({END_REASON_LINK_LOSS, END_REASON_SHUTDOWN})

_status_cache_invalidator = None


def set_status_cache_invalidator(invalidator):
    global _status_cache_invalidator
    _status_cache_invalidator = invalidator


def _invalidate():
    if _status_cache_invalidator:
        _status_cache_invalidator()


def _has_bluetoothctl():
    """Check if bluetoothctl is available."""
    import shutil
    return shutil.which("bluetoothctl") is not None


class BLEManager:
    # ── Concurrency & recovery limits ──
    CMD_QUEUE_MAXSIZE = 500      # prevent unbounded command growth
    RECONNECT_JITTER = 0.25      # ±25% jitter on reconnect delays
    CIRCUIT_BREAKER_MAX_FAIL = 20  # consecutive failures before cooling off
    CIRCUIT_BREAKER_COOLDOWN = 300  # 5 minutes
    MAX_AUTH_FAILURES = 15  # consecutive auth failures before restarting process
    # 认证卡死重启前，等这么久把"正在重启"状态发出去（MQTT/SSE 一次投递的时间）
    AUTH_STUCK_NOTIFY_SEC = 2.0
    LIMIT_RETRY_SEC = 15    # 限额关断命令未生效的重试窗口（命令超时 10s）
    # 连续解密失败多少次判定会话密钥失步并触发重连。设备偶尔会发噪声帧/控制帧,
    # 成功解密即清零；多帧子帧错位修好后这里的失败率应显著下降（见 P2）。
    DECRYPT_FAIL_LIMIT = 3
    MULTIFRAME_MAX_FRAMES = 100   # 多帧帧数钳制（损坏的帧头可能上报超大 count）
    MULTIFRAME_DEADLINE_SEC = 30  # 多帧接收总超时，防止损坏帧头阻塞主循环
    # 采样"新鲜度"：定时器路径只在超过这段时间没有推送时，才代表该口的真实状态
    # （否则会用同一份陈旧 V/I 重复喂判定）
    SAMPLE_FRESH_SEC = 2.0
    # "端口无负载"去抖：单帧 V/I=0（推送间隙/固件瞬时不上报）绝不能立刻结束会话。
    # 取 15s 由实测定：2 天 1Hz 采样里，会话内"零电流后恢复"的停顿 C1 最长 14s
    # （86 次中 ≥3s 有 32 次、≥8s 只剩 2 次），3s 会把 34 次停顿判成会话结束，
    # 8s 仍剩 2 次，15s 归零。代价只是"设备确实停止取电/拔出"的识别晚十几秒：
    # C1/C2 的拔出另有推送帧 + 15s 空闲主动重读的快路径，且 120s/600s 的断电
    # 武装窗口远大于此。切段的隐性代价更高——session_wh 归零 → Wh 限额进度重来；
    # 恢复后的负载若连续不足 START_HOLD_SEC，连新会话都开不出来，那段能量不记录。
    NO_LOAD_DEBOUNCE_SEC = 15.0
    # 预会话环形缓冲：留住"开会话之前"最近这么久的采样，供开会话时回填。
    # 起判要求功率连续达标 START_HOLD_SEC=30s 才开会话，那 30s 里设备**已经在取电**，
    # 但当时既没有会话（能量不积分）、也没有 sid（曲线点不写）——不留缓冲的话每段
    # 会话的头部固定缺 30s：能量少 0.04~0.54Wh（20Wh 限额的 0.2~2.7%）、曲线从中间
    # 开始、起点与时长晚 30s、峰值还可能漏掉前段的最高点。
    # 120s = 30s 门控 + 余量（慢爬升/中途抖动重试）；内存 4 端口 × 120 帧，几十 KB。
    PRE_SESSION_SEC = 120.0
    # "设备彻底不吸电"要持续这么久，才算"自然结束"并允许自动断电
    # （比会话结束本身保守：自适应充电的短暂停顿不会把端口断掉）
    NO_LOAD_ARM_SEC = 600.0
    # 会话刚以 no_load 结束的端口：已经有"一整段充电 + 停止取电"作证据，确认窗口
    # 可以短得多（真正恢复充电会在 START_HOLD_SEC≈30s 内重开会话并撤销挂起）
    NO_LOAD_ARM_SESSION_SEC = 120.0
    # "确实在充电"的功率门限。判据必须用功率而不是电流：满电设备会周期性冒
    # 0.1A（≈0.5W）的涓流脉冲，按"电流>0 就重置窗口"会让窗口永远走不完
    # （实机：c1 会话结束后端口一直不关，自动断电从未被武装）。
    NO_LOAD_BUSY_W = 2.0
    # 充满即停：会话被"自动判定结束"（low_power）后关闭端口。
    FULL_OFF_MIN_WH = 1.0         # 本会话能量下限，防瞬时低功率误断
    FULL_OFF_DEFAULT_MODE = DEFAULT_LIMIT_MODE  # 未指定模式时与限额一致（once）
    FULL_OFF_RETRY_SEC = 15.0     # 断电命令未生效的重试窗口（命令超时 10s）
    FULL_OFF_MAX_ATTEMPTS = 4     # 重试上限（约 60s），超出放弃并告警
    # 长期供电端口：曲线点不落库，仅留内存环形窗口供详情浮层滑动查看
    PERMANENT_RETAIN_SEC = 3600   # 内存保留时长（1h）
    PERMANENT_POINT_MIN_INTERVAL = 1.0  # 最小采样间隔（秒）

    def __init__(self, mac, token, state, config):
        self.mac = mac
        self.token = bytes.fromhex(token)
        self.state = state
        self.config = config
        self.ctrl = None
        self.cmd_queue = asyncio.Queue(maxsize=self.CMD_QUEUE_MAXSIZE)
        self._stop_event = asyncio.Event()
        self._port_timer_task = None
        self._mqtt_publish = None
        self._sse_emitter = None
        self._quality_provider = None
        # 自愈重启处理器（服务层注入；未注入时只能退回"退出等外部管理器拉起"，见
        # _recover_from_auth_stuck）
        self._restart_handler = None
        # 充电会话记录开关：关闭时只停止 DB 写入（start/point/end 均 gate），
        # 内存会话生命周期/实时显示/充电完成事件照常。启动时由服务器从 DB meta 注入。
        self.record_sessions: bool = True
        # 记录关闭期间用于占位的内存会话 id（负值递减，不与真实 DB id 冲突）
        self._fake_sid_counter: int = 0
        self._reconnect_attempts = 0
        self._decrypt_failures = 0
        self._total_frames = 0
        self._last_notify_time = 0.0
        self._reconnect_times = []  # timestamps of recent reconnects
        self._keepalive_fails = 0
        self._auth_fail_count = 0
        self._ble_connect_time = 0.0  # timestamp of current BLE connection
        self._base_reconnect_delay = config.server.reconnect_base_delay
        self._max_reconnect_delay = config.server.reconnect_max_delay
        self._history = None
        # 端口写未确认时置位：让主循环下一轮空闲立刻重读 settings，尽快把本地状态
        # 与设备真实状态对齐（正常刷新周期最长可达一两分钟）。
        self._settings_refresh_now = False
        self._sess_lock = threading.Lock()  # protects _active_sessions (accessed only from event loop)
        self._circuit_breaker_cooldown = 0.0
        self._circuit_breaker_failures = 0
        # Energy tracking
        from energy import (AdaptiveEnergyIntegrator, PortEnergyState,
                            ChargeSessionDetector, SessionStartGate)
        self._energy_integrator = AdaptiveEnergyIntegrator()
        self._energy_states = {i: PortEnergyState() for i in range(1, 5)}
        # 会话边界判定（时间窗+功率）与开始门限（功率+持续+静默期）
        self._session_dets = {i: ChargeSessionDetector() for i in range(1, 5)}
        self._start_gates = {i: SessionStartGate() for i in range(1, 5)}
        self._active_sessions = {}  # port -> session_id
        # 与 _no_load_armed 一样按四个口预置：空字典的话首个无负载样本只记录
        # 起始时刻就返回，去抖实际被拉长了一个采样间隔，且字典大小无人收敛。
        self._no_load_since = {i: None for i in range(1, 5)}   # port -> 开始"无负载"的时间戳
        self._no_load_armed = {i: False for i in range(1, 5)}
        # 上一次会话是不是"设备还插着但停止取电"（no_load）结束的：决定确认窗口长短
        self._no_load_from_session = {i: False for i in range(1, 5)}
        # Charge limits (指定充电量后自动关断端口)
        # wh<=0 = 禁用；mode: once(命中即消费清零) / always(长期有效，每次会话重新武装)
        self._charge_limits = {i: 0.0 for i in range(1, 5)}
        self._limit_modes = {i: DEFAULT_LIMIT_MODE for i in range(1, 5)}
        self._limit_fired = {i: False for i in range(1, 5)}   # 本会话已入队关断（防重入）
        self._limit_fired_at = {i: 0.0 for i in range(1, 5)}
        # 端口模式（启动时由服务器从 DB meta 注入）
        # permanent_ports：长期供电设备 —— 只停充电曲线点（charge_points）写入，
        #   会话行/耗能统计/充满事件照常；曲线留在内存环形窗口里供详情滑动查看。
        # full_off_ports：充满后自动关闭端口（复用会话检测的"判满"结果）。
        self.permanent_ports: set = set()
        self.full_off: dict = {}    # {piid: once|always}，空 = 关闭充满即停
        self._full_off_pending = {i: 0.0 for i in range(1, 5)}   # 已判满、待确认断电
        self._full_off_attempts = {i: 0 for i in range(1, 5)}
        self._full_off_last_try = {i: 0.0 for i in range(1, 5)}
        self._full_off_fired = {i: False for i in range(1, 5)}   # 上次会话是否已充满断电
        self._permanent_points = {i: deque() for i in range(1, 5)}
        self._permanent_last_point = {i: 0.0 for i in range(1, 5)}
        # 预会话环形缓冲：未充电时的最近采样，供开会话时回填起点/能量/曲线。
        # 待补写的点不落在这个字典里，而是随会话起点（闭包）传递——否则"会话秒断
        # 又立刻重开"时，新会话会把上一段的待补点写进自己的 sid。
        self._pre_samples = {i: deque() for i in range(1, 5)}
        # Protocol debounce: track consecutive protocol readings per port
        self._proto_buf = {i: [] for i in range(1, 5)}  # port -> [last N protocols]
        self._PROTO_DEBOUNCE_N = 3  # consecutive readings to confirm protocol
        # Idle port verification: when a port stops receiving BLE pushes, actively
        # GET it after IDLE_VERIFY_SEC to detect unplug (V/I stuck at last value).
        self._IDLE_VERIFY_SEC = 15          # idle > 15s → trigger one active GET
        self._last_verify_time = {i: 0.0 for i in range(1, 5)}
        self._pending_verify = set()        # ports queued for verify (avoids duplicate enqueue)
        # C3/USB-A share a merged sense channel (status_raw 0x11) and the firmware
        # only ever pushes live notifications for C1/C2 — C3/A never arrive via
        # push, so without an active poll they stay stuck at the idle default
        # even while charging. Poll them directly on a fixed cadence instead.
        self._C3A_POLL_INTERVAL_SEC = 3
        # verify_port commands can fail silently (no exception, just "no
        # response") when the link has gone unresponsive at the protocol
        # level while BLE itself still reports connected -- observed
        # hanging indefinitely with zero reconnect attempts, since nothing
        # here ever raised. Force a reconnect once failures stack up.
        self._verify_fail_streak = 0
        self._VERIFY_FAIL_STREAK_LIMIT = 5

    def set_mqtt_publisher(self, publisher):
        self._mqtt_publish = publisher

    def set_sse_emitter(self, emitter):
        self._sse_emitter = emitter

    def set_quality_provider(self, provider):
        """Set a callback that returns combined quality dict from all sources."""
        self._quality_provider = provider

    def set_restart_handler(self, handler):
        """注册"进程自愈重启"处理器（由服务层提供，见 _recover_from_auth_stuck）。

        BLEManager 自己不知道该怎么重启（要按平台停 MQTT/Bemfa/历史再决定
        execv 还是干净退出），所以只留一个钩子；ha_server 启动时挂上自己的
        _restart。
        """
        self._restart_handler = handler

    def _sse_emit(self, event_type, data):
        """Emit SSE event if emitter is connected. Sync — emitter uses threading.Lock internally."""
        if self._sse_emitter:
            self._sse_emitter.emit(event_type, data)

    def set_history(self, history):
        self._history = history

    @property
    def is_running(self) -> bool:
        """是否正在运行 (不处于停止状态)。"""
        return not self._stop_event.is_set()

    def connection_quality(self) -> dict:
        """Estimate BLE connection quality (0-100) from available metrics."""
        total = self._total_frames or 1
        # 1. Decrypt success rate (40%)
        decrypt_score = max(0, ((total - self._decrypt_failures) / total) * 100)
        # 2. Notification responsiveness — time since last BLE push (30%)
        notify_age = time.time() - self._last_notify_time if self._last_notify_time else 999
        notify_score = max(0, min(100, 100 - notify_age * 10))
        # 3. Reconnect frequency in last 5 min (20%)
        recent = sum(1 for t in self._reconnect_times if time.time() - t < 300)
        reconnect_score = max(0, 100 - recent * 25)
        # 4. Keepalive success (10%)
        keepalive_fails = self._keepalive_fails
        keepalive_score = max(0, 100 - keepalive_fails * 33)
        score = round(decrypt_score * 0.4 + notify_score * 0.3 +
                      reconnect_score * 0.2 + keepalive_score * 0.1)
        # Connection uptime
        uptime = int(time.time() - self._ble_connect_time) if self._ble_connect_time else 0
        # Last push age
        last_push_age = round(time.time() - self._last_notify_time) if self._last_notify_time else None
        # Next reconnect delay (when disconnected)
        next_delay = self._get_reconnect_delay() if self._reconnect_attempts > 0 else None
        return {
            "score": score,
            "decrypt": round(decrypt_score),
            "notify": round(notify_score),
            "reconnect_score": round(reconnect_score),
            "reconnect_count_5m": recent,
            "keepalive": round(keepalive_score),
            "total_frames": total,
            "decrypt_failures": self._decrypt_failures,
            "uptime": uptime,
            "last_push_age": last_push_age,
            "next_reconnect_delay": next_delay,
        }

    # ── 端口模式（长期供电 / 充满即停） ──────────────────────────────
    # 两者互斥：常供端口若"充满即停"会把长期供电的负载断掉，语义自相矛盾，
    # 因此在设置常供时强制清掉该口的即停标记（API 层另有 400 兜底）。

    @staticmethod
    def _normalize_port_names(names) -> set:
        """把任意输入归一成 {piid} 集合（非法项静默丢弃，不抛异常）。"""
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, (list, tuple, set, frozenset)):
            return set()
        wanted = {str(n).strip().lower() for n in names}
        return {piid for name, piid in PORT_IDS.items() if name in wanted}

    def set_permanent_ports(self, names) -> list:
        """设置长期供电端口，返回归一后的端口名列表（持久化由调用方负责）。"""
        self.permanent_ports = self._normalize_port_names(names)
        for piid in self.permanent_ports:
            self.full_off.pop(piid, None)           # 互斥
            # 互斥必须连"已挂起的断电"一起撤销：否则刚声明长期供电的端口仍会被
            # 在途的 off 命令/重试断掉（_enforce_full_off 只看 _full_off_pending）
            self._full_off_pending[piid] = 0.0
            self._full_off_attempts[piid] = 0
            self._full_off_fired[piid] = False
            self._charge_limits[piid] = 0.0         # 长期供电没有"限额断电"这回事
            self._limit_fired[piid] = False
            self._permanent_points[piid].clear()
            self._permanent_last_point[piid] = 0.0
        return self.get_port_modes_state()["permanent_ports"]

    def is_permanent_name(self, name) -> bool:
        """按端口名判断是否长期供电（API 层做参数校验用）。"""
        return PORT_IDS.get(str(name).strip().lower()) in self.permanent_ports

    def set_full_off(self, entries) -> dict:
        """设置"充满即停"（端口名 → 模式）。接受 {name: mode} 或 [name]（默认 once）。

        长期供电端口一律剔除；不再启用的端口顺手清掉"已充满断电"标记，
        免得下次打开时还挂着上一次的旧状态。
        """
        if isinstance(entries, str) or isinstance(entries, (list, tuple, set, frozenset)):
            # 列表形式按默认模式启用；键必须是端口名（_normalize_port_names 给的是 piid）
            entries = {PORT_NAMES[p]: self.FULL_OFF_DEFAULT_MODE
                       for p in self._normalize_port_names(entries)}
        clean = {}
        if isinstance(entries, dict):
            for name, mode in entries.items():
                piid = PORT_IDS.get(str(name).strip().lower())
                if piid is None or piid in self.permanent_ports:
                    continue
                _, norm_mode = normalize_charge_limit(0, mode)
                clean[piid] = norm_mode
        for piid in list(self._full_off_fired):
            if piid not in clean:
                self._full_off_fired[piid] = False
                # 撤销尚未确认的自动断电：否则关掉"充满即停"后，在途的 off 与
                # 重试（_enforce_full_off 只看 _full_off_pending）仍会把端口断掉
                self._full_off_pending[piid] = 0.0
                self._full_off_attempts[piid] = 0
        self.full_off = clean
        return self.get_port_modes_state()["full_off_ports"]

    def is_full_off(self, piid: int) -> bool:
        return piid in self.full_off

    def full_off_mode(self, piid: int) -> str:
        return self.full_off.get(piid, "")

    def get_port_modes_state(self) -> dict:
        """端口模式状态（供 /api/port-modes 与 /api/status 读取）。

        端口名按 PORT_NAMES 顺序输出（c1/c2/c3/a），与 meta 里的归一顺序一致，
        前端 indexOf 判定与人工核对都省一次排序。
        """
        ordered = [(piid, name) for name, piid in PORT_IDS.items()]
        return {
            "permanent_ports": [n for p, n in ordered if p in self.permanent_ports],
            # {端口名: once|always}：模式跟限额一样是"一次性 / 长期有效"
            "full_off_ports": {n: self.full_off[p] for p, n in ordered if p in self.full_off},
            "full_off_fired": [n for p, n in ordered if self._full_off_fired[p]],
        }

    def get_live_session_data(self) -> dict:
        """Get real-time energy data for active charging sessions.
        Returns dict mapping port (1-4) to {session_id, session_wh, max_power, start_time}.

        长期供电端口同样出现在这里（耗能统计照常计入），但带 permanent=True 标记，
        前端据此用紧凑样式渲染并走"内存滑动窗口"详情。
        """
        result = {}
        for port, es in self._energy_states.items():
            if es.is_charging and port in self._active_sessions:
                result[port] = {
                    "session_id": self._active_sessions[port],
                    "session_wh": round(es.session_wh, 4),
                    "max_power": round(es.max_power, 2),
                    "start_time": es.session_start,
                    "permanent": port in self.permanent_ports,
                }
        return result

    # ── 长期供电端口的会话曲线（内存窗口，不落库） ──

    def _append_permanent_point(self, piid: int, voltage: float, current: float,
                                protocol: str = "", timestamp: Optional[float] = None) -> None:
        """常供端口的采样点进内存环形窗口：按时间裁剪，超出保留时长即淘汰。

        timestamp 显式传入时用它（预会话回填要把点落在原采样时刻上）；缺省用当前时刻。

        能量/峰值/时长是会话级累计（PortEnergyState），与窗口淘汰无关——这正是
        "窗口滑动时被移出窗口的数据仍计入统计"的实现方式。
        """
        now = time.time() if timestamp is None else timestamp
        if now - self._permanent_last_point[piid] < self.PERMANENT_POINT_MIN_INTERVAL:
            return   # 节流到 ≥1s/点：窗口内存上限 ~3600 点/端口
        self._permanent_points[piid].append((
            round(now, 3), round(voltage, 2), round(current, 2),
            round(voltage * current, 1), protocol or ""))
        self._permanent_last_point[piid] = now
        cutoff = now - self.PERMANENT_RETAIN_SEC
        pts = self._permanent_points[piid]
        while pts and pts[0][0] < cutoff:
            pts.popleft()

    def _permanent_port_of_session(self, session_id) -> Optional[int]:
        """会话 id -> 端口（仅当前活跃会话可解析；已结束的常供会话无曲线可查）。"""
        for port, sid in self._active_sessions.items():
            if sid == session_id:
                return port
        return None

    def get_permanent_session_window(self, session_id, window_sec: float = 900.0,
                                     to_ts: Optional[float] = None) -> Optional[dict]:
        """常供会话的详情数据：内存窗口内的点 + **整个会话**的统计。

        返回 None 表示该会话不在内存里（非活跃 / 非长供）——调用方回落 DB 路径。
        window_sec 决定曲线窗口宽度，to_ts 是窗口右端（缺省=最新），窗口可在保留
        时长内左右滑动；统计恒为会话级累计，不受窗口与淘汰影响。
        """
        port = self._permanent_port_of_session(session_id)
        if port is None or port not in self.permanent_ports:
            return None
        es = self._energy_states[port]
        pts = self._permanent_points[port]
        now = time.time()
        end = min(float(to_ts) if to_ts else now, now)
        width = max(60.0, float(window_sec or 900.0))
        start = end - width
        rows = [p for p in pts if start <= p[0] <= end]
        available_from = pts[0][0] if pts else now
        available_to = pts[-1][0] if pts else now
        duration = int((now - es.session_start) if es.session_start else 0)
        dur_h = duration / 3600.0
        ps = self.state.ports.get(port)
        return {
            "permanent": True,
            "points": [
                {"timestamp": t, "voltage": v, "current": i, "power": p, "protocol": proto}
                for t, v, i, p, proto in rows
            ],
            "stats": {
                "session_id": session_id,
                "port": port,
                "start_time": es.session_start,
                "end_time": None,
                "total_wh": round(es.session_wh, 4),
                "avg_power_w": round(es.session_wh / dur_h, 1) if dur_h > 0 else 0,
                "peak_power_w": round(es.max_power, 2),
                "avg_voltage": round(ps.voltage, 2) if ps else 0,
                "avg_current": round(ps.current, 2) if ps else 0,
                "duration_sec": duration,
            },
            "window": {
                "retain_sec": self.PERMANENT_RETAIN_SEC,
                "window_sec": width,
                "from": start,
                "to": end,
                "available_from": available_from,
                "available_to": available_to,
                "count": len(rows),
                "total_points": len(pts),
            },
        }

    # ── Charge limits ────────────────────────────────────────────────
    # 语义：本端口"本次充电会话"输出能量达到阈值后自动关闭该端口断电。
    # 阈值单位是充电器输出能量（es.session_wh，由 V×I 梯形积分），不是被充设备的
    # 实际充入电量——线损与设备内转换损耗使后者偏小。
    # mode: once=命中即消费清零（一次性）；always=长期有效，每次会话重新武装。

    def set_charge_limits(self, limits: dict) -> dict:
        """应用限额配置（启动注入 / API 调用），返回归一后的完整状态。

        进程内即时生效；DB 写入由调用方负责（set_meta 是同步 sqlite 调用，
        不应压在事件循环上）。
        """
        for name, entry in (limits or {}).items():
            piid = PORT_IDS.get(name)
            if piid is None:
                continue
            if isinstance(entry, dict):
                wh, mode = normalize_charge_limit(entry.get("wh"), entry.get("mode"))
            else:
                wh, mode = normalize_charge_limit(entry, None)
            self._charge_limits[piid] = wh
            self._limit_modes[piid] = mode
        return self.get_charge_limits_state()

    def get_charge_limits_state(self) -> dict:
        """当前限额配置 + 各端口本会话充电进度（供 API/前端读取）。

        session_wh 是"本会话已输出能量"，前端据此显示"已充 X / 限额 Y Wh"。
        session_sec / avg_power_w 是本会话的时长与平均功率（常供卡用它替代实时功率：
        实时功率每帧都在跳，卡片上放统计值更有意义）。会话结束后时长按
        "last_end_time − session_start" 算，不会随挂钟继续变大，平均值因此仍然准确。
        """
        now = time.time()
        out = {}
        for p in range(1, 5):
            es = self._energy_states[p]
            end = now if es.is_charging else es.last_end_time
            if es.session_start and end and end > es.session_start:
                session_sec = end - es.session_start
            else:
                session_sec = 0.0
            avg_w = es.session_wh / (session_sec / 3600.0) if session_sec > 0 else 0.0
            out[PORT_NAMES.get(p, str(p))] = {
                "wh": self._charge_limits[p],
                "mode": self._limit_modes[p],
                "fired": self._limit_fired[p],
                "session_wh": round(es.session_wh, 3),
                "session_sec": round(session_sec, 1),
                "avg_power_w": round(avg_w, 2),
                "is_charging": es.is_charging,
                "session_start": es.session_start,
            }
        return out

    def _enforce_charge_limit(self, piid: int, timestamp: float) -> None:
        """达到阈值时把"关闭该端口"入队，由命令循环统一执行。

        走 cmd_queue 而非直接 await，是为了与用户命令串行执行，避免与
        _connect_and_run 的 MIOT 序列在 GATT 上交错。真正的关断、会话闭合、
        状态广播全部复用 _handle_port_command 既有路径。

        命中后置 _limit_fired 收敛窗口内（命令入队到执行有 1-3 帧推送）的重复
        入队；若 LIMIT_RETRY_SEC 内端口仍在充电（命令超时/失败），说明配置未
        生效，复位标记让下一帧重试。
        """
        if piid in self.permanent_ports:
            return   # 长期供电端口不做限额断电（API 也会拒绝设置，这里是兜底）
        wh = self._charge_limits[piid]
        if wh <= 0:
            return
        es = self._energy_states[piid]
        if not es.is_charging or not limit_reached(es.session_wh, wh):
            return
        if self._limit_fired[piid]:
            if timestamp - self._limit_fired_at[piid] < self.LIMIT_RETRY_SEC:
                return
            _LOGGER.warning("Charge limit for port %d not enforced within %ds, retrying",
                            piid, self.LIMIT_RETRY_SEC)
        self._limit_fired[piid] = True
        self._limit_fired_at[piid] = timestamp
        _LOGGER.info("Charge limit reached: port=%s %.2fWh >= %.2fWh (%s), switching off",
                     PORT_NAMES.get(piid, piid), es.session_wh, wh, self._limit_modes[piid])
        if not self._enqueue_port_off(piid):
            self._limit_fired[piid] = False

    def _enqueue_port_off(self, piid: int) -> bool:
        """把"关闭该端口"入队（与用户命令串行，由命令循环统一执行）。"""
        try:
            self.cmd_queue.put_nowait(("port", (PORT_NAMES[piid], "off"), None))
            return True
        except asyncio.QueueFull:
            _LOGGER.error("Command queue full, port off not enqueued for port %d", piid)
            return False

    # ── 充满即停（判满 → 自动关闭端口） ──
    # "充到什么时候算结束"由 energy.ChargeSessionDetector 判定（时间窗 + 功率 +
    # 波动豁免，连续收敛 HOLD_SEC），会话以 END_REASON_LOW_POWER 结束时武装断电；
    # "设备彻底不吸电"（END_REASON_NO_LOAD）另走持续 NO_LOAD_ARM_SEC 的保守武装。

    def _arm_full_off(self, piid: int, reason: str, timestamp: float) -> None:
        """会话"自动结束"（low_power）后挂起"该端口自动断电"。

        只在开启该选项的端口生效；能量过小的会话（瞬时低功率/刚插上就涓流）不武装，
        避免误断。会话以其它原因结束（用户关端口/拔出/链路中断/关机）不在此列。
        """
        if not self.is_full_off(piid) or reason != END_REASON_LOW_POWER:
            return
        es = self._energy_states[piid]
        if es.session_wh < self.FULL_OFF_MIN_WH:
            _LOGGER.info("Full-off skipped for port %s: session only %.2fWh (< %.2fWh)",
                         PORT_NAMES.get(piid, piid), es.session_wh, self.FULL_OFF_MIN_WH)
            return
        self._full_off_pending[piid] = timestamp
        self._full_off_attempts[piid] = 0
        self._full_off_last_try[piid] = timestamp
        _LOGGER.info("Charging session auto-ended (port=%s, %.2fWh): auto power-off armed",
                     PORT_NAMES.get(piid, piid), es.session_wh)
        # 同 _maybe_arm_full_off：入队失败保留 pending，让 _enforce_full_off 重试
        self._enqueue_port_off(piid)

    def _maybe_arm_full_off(self, piid: int, timestamp: float,
                            reason: str = "session_end") -> None:
        """挂起"自动断电"（只在开启该选项且尚未挂起的端口）。

        reason 只进日志：no_load = 端口已连续确认"没在充电"够久（见 _manage_session
        分支 D），这时不设能量门槛——时间窗本身就是证据。

        这里**不能**沿用 _arm_full_off 的 FULL_OFF_MIN_WH 门槛：门槛读的是
        `es.session_wh`（最近一次会话的能量），而设备充满后常会周期性冒几秒的
        小会话（实测：10.2Wh 大会话之后跟了一个 7 秒 0Wh 会话），那会把证据
        覆盖成 0，导致自动断电再也武装不起来——正是"会话结束了端口却不关"。
        """
        if not self.is_full_off(piid) or self._full_off_pending[piid]:
            return
        es = self._energy_states[piid]
        self._full_off_pending[piid] = timestamp
        self._full_off_attempts[piid] = 0
        self._full_off_last_try[piid] = timestamp
        _LOGGER.info("Auto power-off armed (port=%s, %s, %.2fWh this session, %.1fW peak)",
                     PORT_NAMES.get(piid, piid), reason, es.session_wh, es.max_power)
        # 入队失败不能清掉 pending：清了就没有重试了（_enforce_full_off 只看
        # pending，且 no_load 路径还有 _no_load_armed 挡着重新武装）。
        # 留着 pending，由 _enforce_full_off 按 FULL_OFF_RETRY_SEC 重试。
        self._enqueue_port_off(piid)

    def _enforce_full_off(self, piid: int, timestamp: float) -> None:
        """确认"判满断电"是否生效；端口仍 active 就按窗口重试，超上限放弃。

        必须在"端口已无功率"之外判定：挂起时该口已停止充电，只等 readback
        变 inactive（见 1s 定时器里的调用点）。
        """
        if not self._full_off_pending[piid]:
            return
        name = PORT_NAMES.get(piid, piid)
        ps = self.state.ports.get(piid)
        if not ps or not ps.active:
            self._full_off_pending[piid] = 0.0
            self._full_off_fired[piid] = True
            _LOGGER.info("Full-off confirmed: port %s switched off", name)
            # once：命中即消费（与限额 once 同语义），always 留着下次会话继续生效
            if self.full_off_mode(piid) == LIMIT_MODE_ONCE:
                self.full_off.pop(piid, None)
                # 消费后功能变回"未启用"，"已触发"就不能再挂着：它只是个"本次会话
                # 断过电"的瞬时标记，留着会让卡片长期显示自相矛盾的
                # "未启用 · 已触发"，状态点也一直停在警示色。
                # （always 模式不清：功能仍生效，"已启用 · 已触发"是有意义的信息，
                #   下次会话起点由 _start_session 复位。）
                self._full_off_fired[piid] = False
                _LOGGER.info("One-shot full-off consumed (port %s)", name)
                self._persist_full_off_async()
            return
        self._full_off_fired[piid] = False
        if timestamp - self._full_off_last_try[piid] < self.FULL_OFF_RETRY_SEC:
            return
        attempts = self._full_off_attempts[piid]
        if attempts >= self.FULL_OFF_MAX_ATTEMPTS:
            self._full_off_pending[piid] = 0.0
            _LOGGER.warning("Full-off for port %s not confirmed after %d attempts, giving up",
                            name, attempts)
            return
        self._full_off_attempts[piid] = attempts + 1
        self._full_off_last_try[piid] = timestamp
        _LOGGER.warning("Full-off for port %s not confirmed within %ds, retrying (%d/%d)",
                        name, int(self.FULL_OFF_RETRY_SEC), attempts + 1,
                        self.FULL_OFF_MAX_ATTEMPTS)
        # 入队失败保留 pending：下次到点再试，直接清掉就再也没有重试机会了
        self._enqueue_port_off(piid)

    def _release_limit(self, piid: int, reason: str = END_REASON_UNKNOWN) -> None:
        """会话终止时的限额生命周期处理。

        - _limit_fired 一律复位（会话已终止，与"重新武装"双保险）。
        - once：仅在"真实终止"（端口关闭/拔出/充电自然结束）时消费清零；
          link_loss / shutdown 属基础设施中断，会话可续，保留限额——否则一次
          BLE 抖动或一次服务重启就会静默解除用户刚设的限制。
        - always：长期有效，此处不动，由下次会话起点重新武装。
        """
        self._limit_fired[piid] = False
        if self._charge_limits[piid] <= 0:
            return
        if self._limit_modes[piid] != LIMIT_MODE_ONCE:
            return
        if reason in END_REASONS_PRESERVING_LIMIT:
            _LOGGER.info("Charge limit preserved on port %d (reason=%s, mode=once)",
                         piid, reason)
            return
        self._charge_limits[piid] = 0.0
        _LOGGER.info("One-shot charge limit consumed (port %d, reason=%s, %.2fWh session)",
                     piid, reason, self._energy_states[piid].session_wh)
        self._persist_limits_async()

    def _persist_limits_async(self) -> None:
        """把限额写回 DB meta（once 被消费后自动回写）。非阻塞。

        同步 sqlite 写不该压在事件循环上，因此走线程池。
        """
        if not self._history:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 关机末期无事件循环（对齐 _close_session 既有处理）：内存已更新，
            # DB 保留旧值，重启后按旧值重新武装，属可接受降级。
            _LOGGER.warning("No event loop, charge limits not persisted to DB")
            return
        snapshot = {
            PORT_NAMES.get(p, str(p)): {
                "wh": self._charge_limits[p], "mode": self._limit_modes[p],
            }
            for p in range(1, 5)
        }
        task = loop.run_in_executor(None, self._history.set_charge_limits, snapshot)
        task.add_done_callback(
            lambda t: _LOGGER.error("Persist charge limits failed: %s", t.exception())
            if t.exception() else None)

    def _persist_full_off_async(self) -> None:
        """把"充满即停"写回 DB meta（once 被消费后自动回写）。非阻塞。"""
        if not self._history:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _LOGGER.warning("No event loop, full-off modes not persisted to DB")
            return
        snapshot = {PORT_NAMES[p]: m for p, m in self.full_off.items()}
        task = loop.run_in_executor(None, self._history.set_full_off, snapshot)
        task.add_done_callback(
            lambda t: _LOGGER.error("Persist full-off modes failed: %s", t.exception())
            if t.exception() else None)

    async def request_stop(self):
        """请求停止 BLE 循环 (设置 _stop_event，不直接断开)。"""
        self._close_active_sessions(END_REASON_SHUTDOWN)
        self._stop_event.set()

    def _close_active_sessions(self, reason: str = END_REASON_UNKNOWN):
        """Gracefully close all active charge sessions on shutdown.

        事件（MQTT 充电完成）始终发布；DB 记录仅当真实会话（正 sid）且记录
        开关开启时写入。reason 决定 once 限额是否被消费（见 _release_limit）。
        """
        now = time.time()
        for port, es in self._energy_states.items():
            if es.is_charging:
                with self._sess_lock:
                    sid = self._active_sessions.pop(port, None)
                duration = int(now - (es.session_start or now))
                self._session_dets[port].reset()
                es.is_charging = False
                self._release_limit(port, reason)
                es.last_end_time = now
                db_sid = sid if (sid and sid > 0) else 0
                if es.session_wh >= 0.05:
                    ps = self.state.ports.get(port)
                    if ps:
                        self._publish_charge_event(
                            port, db_sid, es, now,
                            ps.voltage, ps.current, duration)
                    # 措辞与 _close_session 里那条统一：每次会话闭合只有一种格式，
                    # 都能用同一个 "reason=" 检索到。这条是关停时的批量清理路径，
                    # 不走 _close_session，所以自己打一行。
                    _LOGGER.info("Session %s closed (port=%s, %.1fWh, %ds, reason=%s)",
                                 sid if sid else "n/a", PORT_NAMES.get(port, port),
                                 es.session_wh, duration, reason)
                    if db_sid and self._history and self.record_sessions:
                        self._history.end_session(sid, es.session_wh, es.max_power, 0, 0, duration)

    def resume_recording_sessions(self) -> None:
        """打开记录开关时调用：把关闭期间仍在充电的占位会话立即转为真实记录。

        对每个持有占位（负）sid 的端口：异步补建 DB 会话（start_session）并
        从打开时刻起重新累计该端口能量（丢置关闭期间的积分，与"关闭不记录"
        语义一致）。此后该充电会话的曲线/历史立即有数据，无需等到下次充电。
        注意：必须在事件循环内调用。
        """
        if not self._history:
            return
        loop = asyncio.get_running_loop()
        now = time.time()
        for port, sid in list(self._active_sessions.items()):
            if sid >= 0:
                continue  # 已是真实会话（记录开启时创建）
            es = self._energy_states.get(port)
            if not es or not es.is_charging:
                continue
            ps = self.state.ports.get(port)
            protocol = (ps.protocol if ps else "") or ""
            # 从打开时刻重新开始记录：重置会话起点、能量累计与峰值
            # （峰值不保留切换前的值，避免污染转正会话的统计）
            es.session_start = now
            es.session_wh = 0.0
            es.max_power = 0.0
            es.max_current = 0.0

            task = loop.run_in_executor(None, self._history.start_session, port, protocol)

            def _on_upgrade(t, p=port, fake=sid):
                """转正回调：若窗口内会话已结束或被新会话替换，立即闭合刚建的
                DB 会话行（start_session 已完成，避免孤儿行）；否则写入真 sid。"""
                if t.exception():
                    _LOGGER.error("Resume recording session failed for port %d: %s", p, t.exception())
                    return
                new_sid = t.result()
                if not new_sid:
                    _LOGGER.error("Resume recording session failed for port %d: DB returned no sid", p)
                    return
                with self._sess_lock:
                    es2 = self._energy_states.get(p)
                    if es2 is None or not es2.is_charging or self._active_sessions.get(p) != fake:
                        # 转正窗口内会话已结束/被替换：闭合刚建的 DB 行
                        self._close_resumed_orphan(p, new_sid)
                        return
                    self._active_sessions[p] = new_sid
                _LOGGER.info("Session recording resumed for port %d (sid=%s)", p, new_sid)

            task.add_done_callback(_on_upgrade)

    def _close_resumed_orphan(self, port: int, session_id: int) -> None:
        """闭合转正期间被过早创建的 DB 会话行（会话在 start_session 完成前已结束）。"""
        if not self._history:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _LOGGER.warning(
                "_close_resumed_orphan: no event loop, session %d left open for port %d",
                session_id, port)
            return
        task = loop.run_in_executor(None, self._history.delete_session, session_id)
        task.add_done_callback(
            lambda t, _sid=session_id: _LOGGER.error(
                "Close resumed orphan session %d failed: %s", _sid, t.exception())
            if t.exception() else None)

    def _record_charge_point(self, piid: int, voltage: float, current: float,
                             protocol: str = "") -> bool:
        """写入会话采样点（仅真实会话、记录开启、且非常供端口；返回是否提交写库）。

        常供端口（permanent_ports）不落 charge_points，曲线改存内存环形窗口
        （供详情浮层滑动查看）；会话行与耗能统计照常记录，因此这里先于
        record_sessions 判定常供，避免"关了记录开关连内存窗口也没了"的混淆。
        """
        # 常供端口先判：它根本不写 charge_points，与 record_sessions 无关，
        # 只看有没有会话（记录关闭时 sid 是负值占位，内存窗口仍应继续积累）。
        if piid in self.permanent_ports and self._energy_states[piid].is_charging:
            self._append_permanent_point(piid, voltage, current, protocol)
            return False
        sid = self._active_sessions.get(piid)
        if not sid or sid <= 0:
            return False
        if not self.record_sessions:
            return False
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(
            None, self._history.record_charge_point,
            sid, voltage, current, round(voltage * current, 1), protocol)
        task.add_done_callback(
            lambda t: _LOGGER.error("Record charge point failed: %s", t.exception()) if t.exception() else None)
        return True

    def _session_active(self, piid: int) -> bool:
        """该端口是否存在需要闭合的会话（内存态或已注册 sid）。

        会话建立是两步的：push 处理器先同步置 is_charging=True，再把
        start_session 交给线程池，sid 由回调写入 _active_sessions。因此存在
        "is_charging=True 但尚无 sid" 的窗口（DB 写失败时则长期如此）。
        只看 _active_sessions 会漏掉该窗口：端口被关断后 is_charging 永远
        停在 True，get_live_session_data() 会持续上报一个已断电端口的"实时会话"。
        """
        return piid in self._active_sessions or self._energy_states[piid].is_charging

    def _should_sample_port(self, piid: int, ps) -> bool:
        """定时器这一轮要不要对这一口做采样 / 会话判定。

        读数为 0 的口通常无事可做（省掉空转与空曲线点），**但只要会话还开着就
        必须继续处理**：设备拔出后固件不再推送这个口，而"无负载"结束判定要靠
        连续样本走完 NO_LOAD_DEBOUNCE_SEC——跳过它会让去抖永远走不完，会话永久
        停在"活跃"，也会连带跳过下面的 verify_port 主动重读。

        顺带：读数全零（电压塌掉）＝端口真的空了，在这里解除"刚结束"的残留保护。
        释放只能发生在这里——这一口没有会话、读数全零，下面会直接跳过，
        _manage_session 永远跑不到；而残留保护抬高的重开门限（最高 1.0W）会把
        5V 口上 0.5~1W 的小设备永久挡在会话之外（基线是结束后 60s 就回落）。
        """
        if ps is None:
            return False
        if ps.voltage < self._session_dets[piid].VOLTAGE_FLOOR_V and ps.current <= 0:
            self._start_gates[piid].note_no_load()
        if ps.voltage > 0 or ps.current > 0:
            return True
        return self._session_active(piid)

    # ── 会话生命周期（开始 / 结束的唯一入口） ──────────────────────
    # 两条采样路径都调这里：BLE 推送帧（C1/C2 有推送）与 1s 定时器（C3/USB-A 没有
    # 推送、以及推送停掉时）。判定全部按"时间窗 + 功率"，与采样率无关；"端口无负载"
    # 走独立去抖，绝不单帧结束会话。

    def _port_enabled(self, piid: int) -> bool:
        """端口开关状态（PIID16 位掩码，与 /api/status 的 enabled 同源）。"""
        mask = self.state.settings.get("16")
        if not isinstance(mask, int):
            return True          # 读不到就当开着（宁可继续会话，也不凭空结束）
        name = PORT_NAMES.get(piid)
        bit = PORT_BITS.get(name)
        return True if bit is None else bool(mask & (1 << bit))

    def _manage_session(self, piid: int, timestamp: float, voltage: float,
                        current: float, active: bool, protocol: str = "") -> None:
        """会话开始/结束/采样的统一处理（两条采样路径的唯一入口）。

        每次调用都喂一次检测器：两条路径都已经用 SAMPLE_FRESH_SEC 门闩保证
        "这一帧确实是新采样"，不会拿同一份陈旧 V/I 重复喂。
        """
        es = self._energy_states[piid]
        det = self._session_dets[piid]
        gate = self._start_gates[piid]
        power = voltage * current

        det.feed(timestamp, voltage, current)
        # 未充电时的采样进预会话缓冲：其中最后 ~30s 就是"已达起判门限、正在等
        # START_HOLD_SEC 走完"的那一段，开会话时会被回填（见 _start_session）。
        if not es.is_charging:
            self._buffer_pre_session(piid, timestamp, voltage, current, protocol)

        if es.is_charging:
            # A) 端口被关掉（用户/限额/倒计时）→ 这是我们自己的动作，无需去抖
            if not self._port_enabled(piid):
                self._close_session(piid, timestamp, voltage, current,
                                    END_REASON_USER_OFF)
                return
            # B) 端口报无负载：先按端口状态位判断"是拔出还是设备不吸电"，并做去抖
            #    "无负载"只看电流（电压为 0 而电流不为 0 在物理上不成立；反过来
            #    电压仍在协商范围内、电流为 0 才是"充满/维持"的真实场景）。
            #    电压只用来分类结果，不用来决定是否进入本分支。
            if not active or current <= 0:
                gate.note_no_load()
                since = self._no_load_since.get(piid)
                if since is None:
                    self._no_load_since[piid] = timestamp
                    _LOGGER.debug("Port %s reads no load, debouncing (%ds)",
                                  PORT_NAMES.get(piid, piid), self.NO_LOAD_DEBOUNCE_SEC)
                    return
                if timestamp - since < self.NO_LOAD_DEBOUNCE_SEC:
                    return
                # 电压还在协商范围内 → 设备仍在、只是不吸电（充满/维持）＝自然结束
                # 电压塌掉 → 真的拔出了
                reason = (END_REASON_UNPLUG if voltage < det.VOLTAGE_FLOOR_V
                          else END_REASON_NO_LOAD)
                sid = self._close_session(piid, timestamp, voltage, current, reason)
                # 这条只补"电压 + 是拔出还是设备不吸电"的现场值；reason 由
                # _close_session 里那条统一打（避免同一次闭合出现两条 reason=）
                _LOGGER.info("Session %s ended by no-load (port=%s, %.1fWh, %.1fV)",
                             sid if sid else "n/a", PORT_NAMES.get(piid, piid),
                             es.session_wh, voltage)
                return
            self._no_load_since[piid] = None
            self._no_load_armed[piid] = False
            # C) 有负载：低功率收敛判定（时间窗 + 功率 + 波动豁免）
            if det.should_end(timestamp, es.session_wh, es.session_start):
                sid = self._close_session(piid, timestamp, voltage, current,
                                          END_REASON_LOW_POWER)
                if sid:
                    _LOGGER.info("Session %d ended by low-power convergence "
                                 "(port=%s, %.1fWh, p_fast=%.2fW, T=%.2fW, held=%.0fs)",
                                 sid, PORT_NAMES.get(piid, piid), es.session_wh,
                                 det.p_fast(), det.threshold_w(), det.HOLD_SEC)
            return

        # D) 端口上"没在充电"：持续足够久（NO_LOAD_ARM_SEC）才武装自动断电。
        #    判据是**功率**不是电流：满电设备会周期性冒 0.1A（≈0.5W）的涓流脉冲，
        #    按"电流>0 就重置"会让窗口永远走不完（实机：会话结束了端口一直不关）。
        #    电压必须仍在协商范围内——电压塌了是拔出，不该断（也没意义）。
        #    不能拿 active 当判据：active = in_use or V>0 or I>0，只要电压还在它
        #    就是 True，用它过滤会让本分支永不成立（插着不充正是 active=True）。
        if power > self.NO_LOAD_BUSY_W:
            self._no_load_since[piid] = None
            self._no_load_armed[piid] = False
            self._no_load_from_session[piid] = False
        elif (self.is_full_off(piid) and not self._full_off_pending[piid]
                and not self._no_load_armed[piid]
                and voltage >= det.VOLTAGE_FLOOR_V):
            # 不能用 setdefault：键被预置成了 None，setdefault 只认"键不存在"，
            # 会一路返回 None 让计时永远起步不了（首样本白记一次）。
            since = self._no_load_since.get(piid)
            if since is None:
                since = timestamp
                self._no_load_since[piid] = since
            # 会话刚以 no_load 结束：已有一整段充电作证据，确认窗口更短
            window = (self.NO_LOAD_ARM_SESSION_SEC if self._no_load_from_session[piid]
                      else self.NO_LOAD_ARM_SEC)
            if timestamp - since >= window:
                self._no_load_armed[piid] = True
                self._maybe_arm_full_off(piid, timestamp, reason="no_load")
        # E) 开始判定：功率门限 + 持续 + 结束后的静默期（防残留反复开会话）
        if active and self._port_enabled(piid) and gate.should_start(timestamp, det.p_fast(), voltage):
            self._start_session(piid, timestamp, voltage, current, protocol)

    # ── 预会话缓冲（开会话前那 30s 的采样，用于回填） ──────────────

    def _buffer_pre_session(self, piid: int, timestamp: float, voltage: float,
                            current: float, protocol: str = "") -> None:
        """把未充电时的采样放进环形缓冲；按时间裁剪到 PRE_SESSION_SEC。

        时间戳存**原始值**不做取整：取材时用的是门控记下的精确 run_start 与当前帧
        精确 timestamp，取整会让边界帧的取舍随舍入方向摇摆（首帧被排除 / 当前帧被
        反过来包含）。电压电流按上报分辨率取整无妨。
        """
        buf = self._pre_samples[piid]
        buf.append((timestamp, round(voltage, 2), round(current, 2),
                    protocol or ""))
        cutoff = timestamp - self.PRE_SESSION_SEC
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def _take_pre_session_samples(self, piid: int, run_start: float,
                                  now: float) -> list:
        """取出 [run_start, now) 的预会话采样（时间升序）并清空缓冲。

        只取"本段连续达标负载"的范围：抖动/试探负载（没走完 START_HOLD_SEC）根本
        不会走到这里（_start_session 只在门控放行时调用），所以回填不会把噪声变成会话。
        """
        buf = self._pre_samples[piid]
        out = [s for s in buf if run_start <= s[0] < now]
        buf.clear()
        return out

    def _flush_pre_session_points(self, sid, pre) -> None:
        """把预会话采样批量补写成曲线点（行由调用方按会话传入，避免串写）。

        常供端口不会走到这里（它的点进内存窗口）；占位 sid（记录关闭）<=0 时跳过。
        """
        if not pre or not self._history or not sid or sid <= 0:
            return
        rows = [(t, v, i, round(v * i, 1), p) for t, v, i, p in pre]
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(None, self._history.record_charge_points, sid, rows)
        task.add_done_callback(
            lambda t: _LOGGER.error("Backfill %d points failed: %s", len(rows), t.exception())
            if t.exception() else None)

    def _start_session(self, piid: int, timestamp: float, voltage: float,
                       current: float, protocol: str = "") -> None:
        """开会话：内存会话生命周期始终完整（记录关闭时用占位 sid）。

        开会话时会把"门控等待期"（最多 START_HOLD_SEC=30s）的采样回填进本会话：
        起点、能量、峰值、检测器窗口，以及（拿到 sid 后）曲线点——详见各处注释。
        """
        es = self._energy_states[piid]
        gate = self._start_gates[piid]
        # ① 预会话回填的取材：本段"连续达标负载"从哪一刻开始（门控放行前刚记下的）
        run_start = gate.last_run_start() or timestamp
        pre = self._take_pre_session_samples(piid, run_start, timestamp)
        es.is_charging = True
        es.session_wh = 0
        # 起点回填到本段负载真正开始的时刻（否则每段会话都晚 START_HOLD_SEC）
        es.session_start = pre[0][0] if pre else timestamp
        es.max_power = voltage * current
        es.max_current = current
        if pre:
            # 峰值必须含预会话窗口的全部帧——包括首帧：起判首帧就可能已经是本段最高点
            # （之后回落到涓流），而能量回填循环是从 pre[1:] 开始的，首帧会漏掉。
            # 这里直接取 pre 的最大值，不依赖积分器"间隔正常才更新峰值"的顺带行为。
            es.max_power = max(es.max_power, max(v * i for _t, v, i, _p in pre))
            es.max_current = max(es.max_current, max(i for _t, v, i, _p in pre))
        self._no_load_since[piid] = None
        self._no_load_armed[piid] = False
        self._no_load_from_session[piid] = False
        det = self._session_dets[piid]
        det.reset()
        # ② 检测器预热：先喂回填样本、再喂当前样本，让 p_fast/p_slow/峰值把这段
        #    算进去（否则峰值可能漏掉前段最高点，阈值 T_low 跟着偏低）
        for t, v, i, _p in pre:
            det.feed(t, v, i)
        det.feed(timestamp, voltage, current)
        # ③ 能量回填：按时间序梯形积分。先把 last_time 定位到第一帧，避免拿"上一次
        #    会话遗留的 last_time"去积分（那会把会话之间的空档也算进本会话）。
        if pre:
            es.last_time = pre[0][0]
            es.last_power = pre[0][1] * pre[0][2]
            # 累计量 total_wh/daily_wh 不能跟着再算一遍：push 路径对每一帧都无条件积分过
            # （含这段"已在取电但还没开会话"的窗口），回填只该把这段能量补进本会话
            # （session_wh 已在上面归零）。积分器没有"只加 session_wh"的入口，故先存后还原。
            total_before, daily_before = es.total_wh, es.daily_wh
            for t, v, i, _p in pre[1:]:
                self._energy_integrator.update(es, v, i, t)
            self._energy_integrator.update(es, voltage, current, timestamp)
            es.total_wh, es.daily_wh = total_before, daily_before
            _LOGGER.info("Backfilled %d pre-session samples (%.0fs) into port=%s session",
                         len(pre), timestamp - pre[0][0], PORT_NAMES.get(piid, piid))
        # 会话起点重新武装限额（always 长期有效靠此持续；once 若已消费则 wh<=0）
        self._limit_fired[piid] = False
        # 充满即停同样按会话重新武装：清挂起/重试/已触发，并丢掉上一会话的内存曲线
        self._full_off_pending[piid] = 0.0
        self._full_off_attempts[piid] = 0
        self._full_off_fired[piid] = False
        self._permanent_points[piid].clear()
        self._permanent_last_point[piid] = 0.0
        # ④ 曲线回填：常供端口只进内存窗口（它本来就不落 charge_points，别写两份）；
        #    其余端口等拿到 sid 后在回调里批量入 history 的待写缓冲。
        db_backfill = None
        if pre:
            if piid in self.permanent_ports:
                # 常供端口不落 charge_points（曲线在内存窗口里），别写第二份
                for t, v, i, p in pre:
                    self._append_permanent_point(piid, v, i, p, timestamp=t)
            elif self.record_sessions:
                db_backfill = pre      # 等拿到 sid 后补写，随闭包传递
        _LOGGER.info("Session started (port=%s, %.1fW, protocol=%s)",
                     PORT_NAMES.get(piid, piid), voltage * current, protocol or "idle")
        if not self._history:
            return
        loop = asyncio.get_running_loop()
        if self.record_sessions:
            task = loop.run_in_executor(
                None, self._history.start_session, piid, protocol, es.session_start)

            def _on_session_start(t, p=piid, rows=db_backfill):
                if t.exception():
                    _LOGGER.error("Start session failed for port %d: %s", p, t.exception())
                    return
                new_sid = t.result()
                if not new_sid:
                    _LOGGER.error("Start session failed for port %d: DB returned no sid", p)
                    return
                with self._sess_lock:
                    es2 = self._energy_states.get(p)
                    if es2 is None or not es2.is_charging or self._active_sessions.get(p) is not None:
                        # 回调前会话已结束/被新会话取代：闭合刚建的 DB 行
                        self._close_resumed_orphan(p, new_sid)
                        return
                    self._active_sessions[p] = new_sid
                # sid 到手后再补曲线点（常供端口走内存窗口，这里 rows 为 None）
                self._flush_pre_session_points(new_sid, rows)

            task.add_done_callback(_on_session_start)
        else:
            # 记录关闭：不写库，用内存伪造负 sid 保持会话（实时显示/事件照常）
            self._fake_sid_counter -= 1
            with self._sess_lock:
                self._active_sessions[piid] = self._fake_sid_counter

    def _close_session(self, piid, timestamp, voltage=0, current=0,
                       reason: str = END_REASON_UNKNOWN):
        """Close a charge session: cleanup state, notify, and write to DB.

        会话记录开关（record_sessions）只影响 DB 写入：关闭记录期间创建的会话
        使用内存占位 sid（负值），实时显示与充电完成事件（MQTT，存在有效能量
        >=0.05Wh 时）照常；仅真实会话（开启期间创建的正 sid）且开关开启时才落库。
        重开开关后正在充电的会话保持显示（占位 sid 仍在 _active_sessions）。

        reason 标注终止原因，决定 once 限额是否被消费（见 _release_limit）。
        所有会话终止路径都收敛到本方法的 is_charging 跃迁，因此限额清理挂在
        跃迁处即可覆盖全部出口（本方法有多个 return）。
        """
        es = self._energy_states[piid]
        with self._sess_lock:
            sid = self._active_sessions.pop(piid, None)
        if sid is None and not es.is_charging:
            return None
        # 仅在真实跃迁时清理限额：占位清理路径（sid 残留但 is_charging 已 False）
        # 会再次进入本方法，此时限额早已消费，重复清理会误伤新会话的配置。
        was_charging = es.is_charging
        es.is_charging = False
        if was_charging:
            self._release_limit(piid, reason)
            self._arm_full_off(piid, reason, timestamp)
            # 开始门限：拔出（电压塌掉）＝端口真的空了，直接解除残留保护，下一次
            # 插入是全新的一次充电；其余原因（NO_LOAD 满电维持 / LOW_POWER 收敛 /
            # 用户关端口）都进入静默期抬高重开门限，防止残留功耗反复开新会话。
            if reason == END_REASON_UNPLUG:
                self._start_gates[piid].note_no_load()
            else:
                self._start_gates[piid].note_session_end(es.max_power)
            self._session_dets[piid].reset()
            if reason != END_REASON_NO_LOAD:
                # NO_LOAD 要保留起始时刻：自动断电要等它持续够久才武装（见 _manage_session）
                self._no_load_since[piid] = None
                self._no_load_armed[piid] = False
                self._no_load_from_session[piid] = False
            else:
                # 设备还插着、只是停止取电：允许用较短的确认窗口武装自动断电
                self._no_load_from_session[piid] = True
            if piid in self.permanent_ports:
                # 会话结束即释放内存窗口：常供曲线不落库，不为已结束会话留数据
                self._permanent_points[piid].clear()
                self._permanent_last_point[piid] = 0.0
        es.last_end_time = timestamp
        duration = int(timestamp - (es.session_start or timestamp))
        # 结束原因统一在这里落一条日志：所有终止路径都收敛到 _close_session，但调用点
        # 是分散的（用户/限额关端口、定时器发现拔出、无负载去抖、低功率收敛…），过去
        # 只有"无负载/低功率"两条分支自带 reason=，其余路径只发充电完成事件、不打原因
        # ——排查时只能靠相邻的 POST /api/port 反推（实测踩过：网页手动关 C2，日志里
        # 只剩 "Charge event published"）。was_charging 守卫保证每次真实闭合恰好一条，
        # 占位清理的二次进入不会重复打。
        if was_charging:
            _LOGGER.info("Session %s closed (port=%s, %.1fWh, %ds, reason=%s)",
                         sid if sid else "n/a", PORT_NAMES.get(piid, piid),
                         es.session_wh, duration, reason)
        # 仅真实会话（开启记录期间创建的正 sid）可以写库；负 sid 为关闭期间占位
        db_sid = sid if (sid and sid > 0) else 0

        if es.session_wh < 0.05:
            # 无有效能量（如瞬时插拔）：不发事件、不写库（恢复旧语义）；
            # 真实会话（正 sid）需清理其占用的 DB 会话行
            if db_sid and self._history and self.record_sessions:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    _LOGGER.warning("_close_session: no event loop, skip DB delete for session %d", sid)
                    return sid
                task = loop.run_in_executor(
                    None, self._history.delete_session, sid)
                task.add_done_callback(
                    lambda t, _sid=sid: _LOGGER.error("Close session %d failed: %s", _sid, t.exception()) if t.exception() else None)
            else:
                _LOGGER.info("Charge session ended without recording (port %d, %.1fWh, %ds)",
                             piid, es.session_wh, duration)
            return sid

        # 充电完成事件：仅在存在有效能量（>=0.05Wh）时发布，记录开关不影响
        # HA 通知；未落库会话（recorded=False）session_id 用 0 表示。
        self._publish_charge_event(piid, db_sid, es, timestamp,
                                   voltage, current, duration)

        if not db_sid:
            # 记录关闭期间的会话（占位 sid）：事件已发，不写库、不发 SSE
            _LOGGER.info("Charge session ended without DB recording (port %d, %.1fWh, %ds)",
                         piid, es.session_wh, duration)
            return sid

        # Emit SSE session_end event for real-time UI update
        self._sse_emit("session_end", {
            "session_id": sid,
            "port": PORT_NAMES.get(piid, str(piid)),
            "port_id": piid,
            "total_wh": round(es.session_wh, 4),
            "peak_power_w": round(es.max_power, 2),
            "duration_sec": duration,
        })
        if self._history and self.record_sessions:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _LOGGER.warning("_close_session: no event loop, skip DB write for session %d", sid)
                return sid
            task = loop.run_in_executor(
                None, self._history.end_session, sid,
                round(es.session_wh, 4), round(es.max_power, 2),
                round(voltage, 2), round(current, 2), duration)
            task.add_done_callback(
                lambda t, _sid=sid: _LOGGER.error("Close session %d failed: %s", _sid, t.exception()) if t.exception() else None)
        return sid

    def _publish_charge_event(self, piid, sid, es, timestamp, voltage, current, duration):
        """Publish charge completion event via MQTT.

        sid 约定：0 表示会话未落库（记录关闭 / DB 启动失败），此时 recorded=False；
        正值为真实 DB 会话 id（recorded=True）。
        """
        if not self._mqtt_publish:
            return
        try:
            ps = self.state.ports.get(piid)
            payload = {
                "event": "charge_end",
                "port": PORT_NAMES.get(piid, str(piid)),
                "port_id": piid,
                "session_id": sid,
                "recorded": bool(sid),
                "start_time": datetime.fromtimestamp(es.session_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if es.session_start else None,
                "end_time": datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": duration,
                "energy_wh": round(es.session_wh, 4),
                "avg_power_w": round(es.session_wh / (duration / 3600), 1) if duration > 0 else 0,
                "max_power_w": round(es.max_power, 2),
                "protocol": (ps.protocol if ps else "") or "idle",
                "voltage": round(voltage, 2) if voltage else round(ps.voltage, 2) if ps else 0,
                "current": round(current, 2) if current else round(ps.current, 2) if ps else 0,
            }
            self._mqtt_publish(self.config.topic_charge_event, payload)
            _LOGGER.info("Charge event published: port=%s energy=%.1fWh duration=%ds",
                         PORT_NAMES.get(piid, str(piid)), es.session_wh, duration)
        except Exception as err:
            _LOGGER.error("Failed to publish charge event: %s", err)

    @staticmethod
    def _auth_backoff_delay(auth_fail_count: int) -> int:
        """认证失败后的分钟级退避延迟（表驱动，与设备会话清除节奏对齐）。

        ≥5 次: 10 分钟（设备 BLE 会话需足够时间清除）
        ≥3 次: 5 分钟
        否则: 2 * 次数，封顶 3 分钟
        """
        if auth_fail_count >= 5:
            return 600
        if auth_fail_count >= 3:
            return 300
        return min(120 * auth_fail_count, 180)

    @staticmethod
    def _should_restart_process(auth_fail_count: int) -> bool:
        """连续认证失败达到阈值后，重启整个进程以恢复 BLE 会话。"""
        return auth_fail_count >= BLEManager.MAX_AUTH_FAILURES

    async def _recover_from_auth_stuck(self) -> None:
        """认证连续失败到上限：重启进程以重建 BLE 会话（本方法不会正常返回）。

        优先走服务层注册的重启处理器（ha_server._restart）——它按平台分流：
        Linux 用 os.execv 真正自愈（**不需要**外部守护），win32 干净退出交给
        拉起方（execv 后 WinRT 事件循环无法在新映像内重建）。

        没有处理器时退回 os._exit(1)：那条路**必须有外部进程管理器**
        （systemd / supervisor / 脚本）才会被重新拉起，否则服务会一直停着
        —— 实机踩过：本机既没有 systemd 单元，crontab 里那条 ensure_server.sh
        又指向不存在的路径，结果 15 次失败后进程退出、停机 3 小时 25 分。
        os._exit 不执行 finally 里的 _disconnect()，但进程整体退出后 BLE/GATT
        句柄随进程回收；遗留的会话行由启动时的收尾逻辑补完。
        """
        _LOGGER.critical(
            "Auth failed %d times consecutively. "
            "Restarting process to recover BLE session.",
            self._auth_fail_count)
        self._publish_status(
            {"connected": False, "error": "auth_stuck_restarting"}, retain=True)
        # 给 MQTT/SSE 一点时间把上面这条状态发出去，再重启
        await asyncio.sleep(self.AUTH_STUCK_NOTIFY_SEC)
        if self._restart_handler is not None:
            await self._restart_handler()
            _LOGGER.error("Restart handler returned without restarting the process")
        else:
            _LOGGER.warning(
                "No restart handler registered: exiting now; an external supervisor "
                "is required to bring the service back")
        os._exit(1)

    def _get_reconnect_delay(self):
        """Calculate exponential backoff delay with jitter."""
        delay = min(
            self._base_reconnect_delay * (2 ** min(self._reconnect_attempts, 10)),
            self._max_reconnect_delay
        )
        # Add jitter (±25%) to prevent thundering herd
        if delay > 1.0:
            jitter = delay * self.RECONNECT_JITTER * (random.random() * 2 - 1)
            delay += jitter
        return max(0.5, delay)

    def _check_circuit_breaker(self) -> bool:
        """Return True if the circuit breaker is open (should NOT connect)."""
        now = time.time()
        if self._circuit_breaker_cooldown > 0:
            if now < self._circuit_breaker_cooldown:
                return True
            # Cooldown expired
            self._circuit_breaker_cooldown = 0.0
            self._circuit_breaker_failures = 0
        return False

    def _record_circuit_breaker_failure(self):
        """Record a failure; trip circuit breaker if threshold reached."""
        self._circuit_breaker_failures += 1
        if self._circuit_breaker_failures >= self.CIRCUIT_BREAKER_MAX_FAIL:
            _LOGGER.warning(
                "Circuit breaker tripped (%d failures), cooling off for %ds",
                self._circuit_breaker_failures, self.CIRCUIT_BREAKER_COOLDOWN,
            )
            self._circuit_breaker_cooldown = time.time() + self.CIRCUIT_BREAKER_COOLDOWN

    async def start(self):
        self._stop_event.clear()
        self._reconnect_attempts = 0
        self._decrypt_failures = 0
        self._auth_fail_count = 0
        first_run = True
        last_error = None
        while not self._stop_event.is_set():
            # Check circuit breaker before attempting connection
            if self._check_circuit_breaker():
                remaining = int(self._circuit_breaker_cooldown - time.time())
                _LOGGER.warning(
                    "Circuit breaker open, waiting %ds before next attempt",
                    max(remaining, 0),
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=max(remaining, 30),
                    )
                    break
                except asyncio.TimeoutError:
                    continue

            try:
                await self._connect_and_run()
                self._reconnect_attempts = 0
                self._decrypt_failures = 0
                self._auth_fail_count = 0
                self._circuit_breaker_failures = 0
                first_run = False
                last_error = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                last_error = e
                self._record_circuit_breaker_failure()
                err_str = str(e)
                if 'POWERED_OFF' in err_str or 'No powered Bluetooth' in err_str:
                    _LOGGER.warning("Bluetooth is powered off, will retry in 60s...")
                elif 'Charger not found' in err_str or 'BLE scan failed' in err_str:
                    # 可恢复的扫描错误，不需要完整堆栈
                    _LOGGER.warning("BLE loop error: %s (retry %d)", e, self._reconnect_attempts + 1)
                else:
                    _LOGGER.error("BLE loop error: %s", e, exc_info=True)
            finally:
                await self._disconnect()
            if not self._stop_event.is_set():
                if isinstance(last_error, AuthConnectionError):
                    # auth 失败可能有两类原因:
                    # 1. 设备端 session 未清除 (需等待设备自然超时)
                    # 2. BlueZ GATT 缓存损坏 (需 power cycle 本地适配器)
                    # 因此 auth 失败也应重置本地适配器，避免陷入永久失败
                    self._reconnect_attempts = 0  # reset: auth failure has its own counter
                    self._auth_fail_count += 1
                    await self._force_disconnect_bluetooth()
                    if self._should_restart_process(self._auth_fail_count):
                        await self._recover_from_auth_stuck()
                    elif self._auth_fail_count >= 5:
                        _LOGGER.error(
                            "Auth failed %d times consecutively. "
                            "Device session is stuck. Please power-cycle the charger "
                            "(unplug and replug) to reset its BLE session.",
                            self._auth_fail_count)
                        self._publish_status({"connected": False, "error": "device_session_stuck"}, retain=True)
                        # 等待 10 分钟后自动重试（给设备足够时间清除 BLE 会话）
                        delay = self._auth_backoff_delay(self._auth_fail_count)
                    else:
                        delay = self._auth_backoff_delay(self._auth_fail_count)
                    _LOGGER.warning("Auth failed %d times, reset adapter and waiting %ds...",
                                    self._auth_fail_count, delay)
                elif last_error and ('POWERED_OFF' in str(last_error) or 'No powered Bluetooth' in str(last_error)):
                    delay = 60  # Bluetooth powered off, check less frequently
                elif last_error and 'Charger not found' in str(last_error):
                    # 充电器不在范围内: 不需要 power cycle 适配器，只重试扫描
                    delay = self._get_reconnect_delay()
                elif last_error:
                    await self._force_disconnect_bluetooth()
                    delay = self._get_reconnect_delay()
                else:
                    delay = self._get_reconnect_delay()
                self._reconnect_attempts += 1
                self._reconnect_times.append(time.time())
                # Prune to last 10 minutes
                cutoff = time.time() - 600
                self._reconnect_times = [t for t in self._reconnect_times if t > cutoff]
                if 'Charger not found' in str(last_error or ''):
                    _LOGGER.info("Waiting %.0fs before retry (attempt %d)...", delay, self._reconnect_attempts)
                elif 'POWERED_OFF' not in str(last_error or '') and 'No powered Bluetooth' not in str(last_error or ''):
                    _LOGGER.info("Reconnecting in %.0fs (attempt %d)...", delay, self._reconnect_attempts)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass

    async def stop(self):
        self._close_active_sessions(END_REASON_SHUTDOWN)
        self._stop_event.set()
        await self._disconnect()
        if _has_bluetoothctl():
            await self._force_disconnect_bluetooth()

    def _find_ble_adapter(self):
        """自动检测支持 BLE 的蓝牙适配器名称（如 hci0, hci1）"""
        if not os.path.exists("/sys/class/bluetooth"):
            return "hci0"
        import glob
        hci_devs = sorted(glob.glob("/sys/class/bluetooth/hci*"))
        for hci_dir in hci_devs:
            hci_name = os.path.basename(hci_dir)
            if ":" in hci_name:
                continue
            if os.path.isdir(os.path.join(hci_dir, "device")):
                return hci_name
        return "hci0"

    async def _connect(self):
        _LOGGER.info("Scanning for charger...")

        # 先清理 BlueZ 残留扫描状态（避免 InProgress 错误）
        await self._stop_ble_scan()
        # 等待 BlueZ 清理扫描状态，避免与 BleakScanner 内部扫描冲突
        await asyncio.sleep(0.5)

        from bleak import BleakScanner
        try:
            found = await BleakScanner.find_device_by_address(
                self.mac, timeout=self.config.ble.scan_timeout)
        except Exception as e:
            err_str = str(e)
            _LOGGER.error("BLE scan failed: %s", e)
            # InProgress 错误 → 适配器状态异常，需要 power cycle
            if 'InProgress' in err_str:
                await self._force_disconnect_bluetooth()
            raise ConnectionError(f"BLE scan failed: {e}")

        # macOS/CoreBluetooth never exposes a peripheral's real BLE MAC --
        # it substitutes a randomized per-host UUID instead (privacy), so
        # find_device_by_address against the configured real MAC can never
        # match there, even when the charger is advertising normally. The
        # charger doesn't advertise the MiOT service UUID in its top-level
        # service list either (only in a MiBeacon service-data payload), so
        # match on that payload instead -- it carries the real MAC in
        # little-endian form (bytes[-6:]), which lets us confirm we've
        # found *this* charger and not some other Xiaomi device.
        if not found and sys.platform == "darwin":
            try:
                devices = await BleakScanner.discover(
                    timeout=self.config.ble.scan_timeout, return_adv=True)
            except Exception as e:
                _LOGGER.error("BLE scan failed: %s", e)
                raise ConnectionError(f"BLE scan failed: {e}")
            target_mac_bytes = mac_str_to_bytes(self.mac)  # already reversed (little-endian)
            for _addr, (dev, adv) in devices.items():
                payload = (adv.service_data or {}).get(UUID_FE95)
                if payload and payload[-6:] == target_mac_bytes:
                    found = dev
                    break

        if not found:
            _LOGGER.warning("Charger not found with MAC: %s (will retry)", self.mac)
            raise ConnectionError("Charger not found")

        self.ctrl = CuktechBLEController(self.mac, self.token)
        # 注册端口推送回调：controller 在 GET/SET 等待响应窗口里收到端口推送时
        # 直接交给这里处理，避免推送被吞（旧代码的回放条件 b4==0x02 永假）。
        self.ctrl.on_port_push = self._process_decrypted_frame
        if sys.platform == "darwin":
            # mac_bytes (derived from the real MAC above) still feeds the
            # MiOT auth handshake -- only the connection-time identifier
            # needs to be the CoreBluetooth address bleak actually resolved.
            self.ctrl.mac = found.address
        await self.ctrl.connect()

        _LOGGER.info("Connected, waiting for device to settle...")
        await asyncio.sleep(2)

        await self.ctrl.read_device_info()
        _LOGGER.info("Connected, authenticating...")
        # 存储设备信息到 state
        await self.state.update_device_info(self.ctrl.device_model, self.ctrl.firmware_version)

        if not await self.ctrl.authenticate():
            _LOGGER.warning("Auth failed, disconnecting BLE...")
            try:
                if self.ctrl.client and self.ctrl.client.is_connected:
                    await self.ctrl.stop_all_notifications()
                    await self.ctrl.client.disconnect()
            except Exception:
                pass
            # 等待设备处理断连，避免旧连接未完全释放时新连接冲突
            await asyncio.sleep(3)
            raise AuthConnectionError("Auth failed")

        self._auth_fail_count = 0  # reset on successful auth
        self._verify_fail_streak = 0
        self._ble_connect_time = time.time()
        self._last_notify_time = 0.0  # reset so quality shows "无" until first push
        await self.state.set_connection(True, True)
        _invalidate()
        _LOGGER.info("Authenticated!")

        # 立即推送连接状态，前端无需等待 15 个 PIID 读完
        self._publish_status({"connected": True, "authenticated": True}, retain=True)

        # 先读取 PIID 17 (c1_c2_protocol) 获取硬件协议代码
        # 再处理 init_push 端口数据，确保 hw_protocol 已就绪
        await self._read_initial_settings()

        # 处理认证后设备推送的初始端口数据
        if self.ctrl and self.ctrl.init_push_frames:
            _LOGGER.info("Processing %d init push frames", len(self.ctrl.init_push_frames))
            for frame in self.ctrl.init_push_frames:
                await self._try_process_inline_frame(frame)
            self.ctrl.init_push_frames = []

        # Build full state for status event
        ports_data = {}
        port_ctl = self.state.settings.get("16", 0x0F)
        for piid, pname in PORT_NAMES.items():
            ps = self.state.ports.get(piid)
            port_data = ps.to_dict() if ps else dict(PORT_DEFAULT)
            port_data["enabled"] = bool(port_ctl & (1 << (piid - 1)))
            ports_data[str(piid)] = port_data
        self._publish_status({
            "connected": True,
            "authenticated": True,
            "device_model": self.ctrl.device_model,
            "firmware_version": self.ctrl.firmware_version,
            "ports": ports_data,
            "settings": self.state.settings,
            "protocol_switches": self.state.protocol_switches,
            "protocol_extend": self.state.protocol_extend,
        }, retain=True)

        await asyncio.sleep(2)

    async def _disconnect(self):
        if self.ctrl:
            client = self.ctrl.client if self.ctrl else None
            was_connected = bool(client and client.is_connected)
            # 始终进行 GATT cleanup，确保设备收到干净的 BLE LL disconnect
            # （无论是否 stop，设备端都需要感知断开以清除 auth session）
            try:
                if client and client.is_connected:
                    await self.ctrl.stop_all_notifications()
            except Exception:
                pass
            try:
                if client and client.is_connected:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=3.0)
                    except Exception:
                        pass
            except Exception:
                pass
            self.ctrl = None
            self._ble_connect_time = 0.0  # reset uptime on disconnect
            self._last_notify_time = 0.0  # reset push tracking on disconnect
            self._total_frames = 0       # reset frame counter on disconnect
            # Close active charge sessions on disconnect。链路中断属基础设施故障
            # （服务会重连，会话可续），不清零 once 限额——否则一次 BLE 抖动就会
            # 静默解除用户刚设的限制（见 END_REASONS_PRESERVING_LIMIT）。
            self._close_active_sessions(END_REASON_LINK_LOSS)
            if was_connected and not self._stop_event.is_set():
                _LOGGER.error("BLE device disconnected unexpectedly")
        await self.state.set_connection(False, False)
        _invalidate()
        self._publish_status({
            "connected": False,
            "device_model": self.state.device_model,
            "firmware_version": self.state.firmware_version,
        }, retain=True)
        # bluetoothctl disconnect MAC 由 _force_disconnect_bluetooth() 统一处理
        # 此处不再重复调用，避免设备收到多次断连通知导致状态混乱

    async def _stop_ble_scan(self):
        """Cancel any lingering BLE scan on the adapter before starting a new one.
        Prevents [org.bluez.Error.InProgress] Operation already in progress.
        """
        if not _has_bluetoothctl():
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "scan", "off",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            pass

    async def _force_disconnect_bluetooth(self):
        """使用 bluetoothctl 强制断开蓝牙连接并重置适配器。

        仅在 Linux + bluetoothctl 可用时执行；其它平台由 bleak 层处理断连，
        适配器电源循环属于 Linux 特有的 BlueZ 恢复手段，跳过不影响功能。
        """
        if not _has_bluetoothctl():
            _LOGGER.info("bluetoothctl not available, skipping adapter power cycle")
            return
        # 先停止残留扫描，再断开已有连接，最后重置适配器
        await self._stop_ble_scan()
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "disconnect", self.mac,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            # 等待 BLE Link Layer disconnect 完成
            await asyncio.sleep(1)
        except Exception as e:
            _LOGGER.warning("bluetoothctl disconnect failed: %s", e)
        # 重置蓝牙适配器以清理残留状态
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "power", "off",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            # 等待 BLE 芯片完全下电
            await asyncio.sleep(3)
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "power", "on",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            # 等待适配器就绪，最多15秒
            hci = self._find_ble_adapter()
            for _ in range(15):
                await asyncio.sleep(1)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "bluetoothctl", "show", hci,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
                    if b"Powered: yes" in stdout:
                        _LOGGER.info("BT adapter ready after power cycle")
                        break
                except Exception:
                    try:
                        power_file = f"/sys/class/bluetooth/{hci}/power"
                        if os.path.exists(power_file):
                            with open(power_file) as f:
                                if f.read().strip() == "1":
                                    _LOGGER.info("BT adapter ready (via sysfs)")
                                    break
                    except Exception:
                        pass
            else:
                _LOGGER.warning("BT adapter not ready after 15s, proceeding anyway")
        except Exception as e:
            _LOGGER.warning("bluetoothctl power cycle failed: %s", e)

    async def _connect_and_run(self):
        await self._connect()
        self._keepalive_fails = 0
        self._settings_fail_streak = 0
        last_refresh = time.time()
        last_notify = time.time()
        last_keepalive = time.time()

        # Start 1-second background timer for port_history + energy accumulation
        self._port_timer_task = asyncio.ensure_future(self._port_timer())

        try:
            while not self._stop_event.is_set():
                await self._process_commands()

                if not self.ctrl:
                    break

                try:
                    data = await asyncio.wait_for(
                        self.ctrl.wait_notify("cmd_recv"), timeout=2.0)
                    if not self.ctrl:
                        break
                    last_notify = time.time()
                    self._last_notify_time = last_notify
                    self._total_frames += 1
                except asyncio.TimeoutError:
                    now = time.time()
                    # Refresh settings during idle (no BLE push — avoids data loss from drain)
                    if (now - last_refresh > self.config.server.settings_refresh_interval
                            or self._settings_refresh_now):
                        self._settings_refresh_now = False
                        await self._refresh_settings()
                        now = time.time()
                        last_refresh = now
                        last_notify = now
                        # Zombie-channel guard: _fetch_settings only logs when every
                        # readable PIID fails, it never forces a reconnect on its own.
                        # If that happens repeatedly in a row, the BLE transport is
                        # alive (no disconnect callback fires) but GATT reads are
                        # dead (observed as e.g. "Service Discovery has not been
                        # performed yet") -- force a reconnect the same way the
                        # keepalive-failure path already does below.
                        if getattr(self, "_settings_fail_streak", 0) >= 2:
                            _LOGGER.warning(
                                "Settings refresh failed %d times in a row "
                                "(all PIID reads failing) -- BLE channel is a "
                                "zombie, forcing reconnect",
                                self._settings_fail_streak)
                            raise ConnectionError(
                                "BLE channel stale: repeated full PIID read failure")
                    if now - last_keepalive > 10:
                        if self.ctrl and self.ctrl.client and self.ctrl.client.is_connected:
                            try:
                                # NOTE: must stay response=False — this device's
                                # characteristics are all Write-Without-Response,
                                # so response=True would time out on a healthy link
                                # and cause false reconnects. Link loss is instead
                                # detected via the disconnect callback below.
                                await self.ctrl.client.write_gatt_char(
                                    CHAR_CMD_RECV, bytes([0x00, 0x00, 0x00, 0x00]), response=False)
                                last_keepalive = now
                                self._keepalive_fails = 0
                            except Exception:
                                self._keepalive_fails += 1
                                if self._keepalive_fails >= 3:
                                    _LOGGER.warning("Keepalive failed 3 times, reconnecting")
                                    raise ConnectionError("BLE keepalive failed")
                    if now - last_notify > 60:
                        client = self.ctrl.client if self.ctrl else None
                        # Detect silent link loss. Bleak 3.x has no disconnect
                        # callback and is_connected may lag, so do an active probe:
                        # read the firmware version characteristic — a real BLE read
                        # fails once the link is actually gone.
                        lost = not client or not client.is_connected
                        if not lost:
                            try:
                                await asyncio.wait_for(
                                    client.read_gatt_char(CHAR_FW_VERSION), timeout=3.0)
                            except Exception:
                                lost = True
                        if lost:
                            _LOGGER.warning("BLE connection lost (is_connected=%s), triggering reconnect",
                                            client.is_connected if client else None)
                            raise ConnectionError("BLE disconnected")
                    continue
                except Exception as e:
                    _LOGGER.warning("BLE notification error: %s", e)
                    raise

                if not data or len(data) < 4:
                    continue

                if data[2] == 0x02 and len(data) >= 4:
                    await self._handle_inline_data(data)
                elif data[2] == 0x00 and len(data) >= 6:
                    await self._handle_multiframe(data)
                else:
                    await self._try_process_inline_frame(data)
        finally:
            if self._port_timer_task:
                self._port_timer_task.cancel()
                try:
                    await self._port_timer_task
                except asyncio.CancelledError:
                    pass

    async def _port_timer(self):
        """1-second timer: write port_history + energy + charge_points for ports
        that are NOT receiving BLE pushes (stable V/I). Also emits connection quality every 5s."""
        quality_tick = 0
        while not self._stop_event.is_set():
            await asyncio.sleep(1)
            # Emit connection quality every 5s (independent of history)
            quality_tick += 1
            if quality_tick % 5 == 0:
                q = self._quality_provider() if self._quality_provider else {"ble": self.connection_quality()}
                self._sse_emit("quality", q)
            now = time.time()
            # C3/USB-A never receive a live push from the firmware (only C1/C2 do) —
            # poll their status directly on a fixed cadence so a plugged-in device is
            # ever discovered, instead of sitting at the idle default forever.
            for piid in (3, 4):
                if (now - self._last_verify_time[piid] > self._C3A_POLL_INTERVAL_SEC
                        and piid not in self._pending_verify):
                    self._pending_verify.add(piid)
                    try:
                        self.cmd_queue.put_nowait(("verify_port", piid, None))
                        self._last_verify_time[piid] = now
                    except asyncio.QueueFull:
                        self._pending_verify.discard(piid)
            # 判满断电的确认/重试与历史写入无关，且必须早于下面的 V/I 过滤：
            # 挂起时端口已停止充电（V/I 可能为 0），只等 readback 变 inactive。
            for piid in range(1, 5):
                self._enforce_full_off(piid, now)
            if not self._history or self._stop_event.is_set():
                continue
            loop = asyncio.get_running_loop()
            for piid in range(1, 5):
                try:
                    ps = self.state.ports.get(piid)
                    es = self._energy_states[piid]
                    if not self._should_sample_port(piid, ps):
                        continue
                    # BLE handler already recorded if last_time < 2s ago
                    idle = (es.last_time is None
                            or (now - es.last_time) > self.SAMPLE_FRESH_SEC)
                    # Active verification: if the port has been idle (no BLE push)
                    # longer than IDLE_VERIFY_SEC, enqueue a GET so the main loop
                    # actively re-samples it. This catches the case where the unplug
                    # push was lost (we were busy in an active GET) and ps stays at
                    # the stale pre-unplug V/I forever.
                    if (idle and es.last_time is not None
                            and now - es.last_time > self._IDLE_VERIFY_SEC
                            and now - self._last_verify_time[piid] > self._IDLE_VERIFY_SEC
                            and piid not in self._pending_verify):
                        self._pending_verify.add(piid)
                        try:
                            self.cmd_queue.put_nowait(("verify_port", piid, None))
                            self._last_verify_time[piid] = now
                        except asyncio.QueueFull:
                            self._pending_verify.discard(piid)
                    if idle:
                        # 定时器只在"没有推送"时代表这一口的真实状态（C3/A 全靠它）
                        self._manage_session(piid, now, ps.voltage, ps.current,
                                             active=ps.active, protocol=ps.protocol or "")
                        # 采样点同样要写：C3/USB-A 全靠定时器驱动，不写就没有曲线，
                        # end_session 的均压/均流也会退化成 0。常供端口走内存窗口，
                        # 那里自带 1s 节流且 0A 也是有效曲线点，故不受 current>0 限制。
                        if (self._history and es.is_charging
                                and piid in self._active_sessions
                                and (ps.current > 0 or piid in self.permanent_ports)):
                            self._record_charge_point(piid, ps.voltage, ps.current,
                                                      ps.protocol or "")
                        # Only integrate if current > 0 (no power transfer at 0A)
                        if es.is_charging and ps.current > 0:
                            self._energy_integrator.update(
                                es, ps.voltage, ps.current, now)
                            # 充电量达到阈值 → 入队关断（与 push 路径同一判定）
                            self._enforce_charge_limit(piid, now)
                        # port_history: always write for chart continuity
                        task = loop.run_in_executor(
                            None, self._history.record_port_data,
                            piid, ps.to_dict())
                        task.add_done_callback(
                            lambda t: _LOGGER.error("Timer record_port_data failed: %s", t.exception()) if t.exception() else None)
                except Exception as e:
                    _LOGGER.error("Port timer error for piid %d: %s", piid, e, exc_info=True)

    async def _fetch_settings(self, update_existing=False):
        settings = dict(self.state.settings) if update_existing else {}
        pdo_caps = {}
        fail_count = 0
        for piid in READABLE_SETTINGS_PIIDS:
            try:
                result = await self.ctrl.send_miot_command(2, piid)
                if result and "value" in result:
                    settings[str(piid)] = result["value"]
                    if piid == 17:
                        pdo_caps["c1c2"] = decode_pdo_caps(result["value"], "c1", "c2")
                        # PIID 17 byte[0]=C1 协议代码, byte[2]=C2 协议代码
                        # 与米家 parseC1C2ProtocolInfo 一致
                        val32 = result["value"] & 0xFFFFFFFF
                        c1_proto = (val32 >> 24) & 0xFF
                        c2_proto = (val32 >> 8) & 0xFF
                        # 零值保护在 state 层自动处理
                        await self.state.set_hw_protocol_codes(c1_proto, c2_proto)
                        _LOGGER.info("PIID17 hw_protocol_codes: C1=%d C2=%d (raw=0x%08X)",
                                     self.state._hw_protocol_c1, self.state._hw_protocol_c2, val32)
                    elif piid == 18:
                        pdo_caps["c3a"] = decode_pdo_caps(result["value"], "c3", "a")
                        # PIID 18 byte[0]=C3 协议代码, byte[2]=A 协议代码
                        val32 = result["value"] & 0xFFFFFFFF
                        c3_proto = (val32 >> 24) & 0xFF
                        a_proto = (val32 >> 8) & 0xFF
                        await self.state.set_hw_protocol_codes_c3a(c3_proto, a_proto)
                        _LOGGER.info("PIID18 hw_protocol_codes: C3=%d A=%d (raw=0x%08X)",
                                     self.state._hw_protocol_c3, self.state._hw_protocol_a, val32)
                    elif piid == 21:
                        await self.state.update_protocol_extend(result["value"])
                        self._sse_emit("protocol", {"switches": self.state.protocol_switches,
                                                    "protocol_extend": result["value"]})
            except Exception as e:
                fail_count += 1
                _LOGGER.debug("Failed to read PIID %d: %s", piid, e)
        if fail_count >= len(READABLE_SETTINGS_PIIDS):
            self._settings_fail_streak = getattr(self, "_settings_fail_streak", 0) + 1
            _LOGGER.warning(
                "All %d PIID reads failed, BLE channel may be broken (streak=%d)",
                fail_count, self._settings_fail_streak)
        else:
            self._settings_fail_streak = 0
        await self.state.update_settings(settings)
        await self.state.update_pdo_caps(pdo_caps)
        _invalidate()
        self._publish_settings(retain=True)
        # Detect port control changes (PIID 16) — firmware may close ports via countdown
        self._emit_port_control_changes()

    def _emit_port_state(self, piid: int, port_info: dict = None):
        """Unified port state emission: build data + publish MQTT + emit SSE.

        Args:
            piid: Port ID (1-4)
            port_info: Optional pre-built port data dict (e.g. from BLE decode).
                       If None, reads from state based on PIID 16 enabled flag.
        """
        port_ctl = self.state.settings.get("16", 0x0F)
        is_enabled = bool(port_ctl & (1 << (piid - 1)))

        if port_info is not None:
            # Caller provided data (e.g. BLE push decoded data)
            data = dict(port_info)
            data["enabled"] = is_enabled  # PIID 16 port control; port_info.active tells device presence
        elif is_enabled:
            # Port enabled — use current state
            ps = self.state.ports.get(piid)
            data = ps.to_dict() if ps else dict(PORT_DEFAULT)
            data["enabled"] = True
        else:
            # Port disabled — use zeros
            data = dict(PORT_DEFAULT)
            data["enabled"] = False

        self._publish_port(PORT_NAMES[piid], data, retain=True)
        self._sse_emit("port_update", {"port_id": piid, "port": PORT_NAMES[piid], "data": data})

    def _emit_port_control_changes(self):
        """Check if PIID 16 (port control) changed and emit SSE events for affected ports."""
        new_ctl = self.state.settings.get("16", 0x0F)
        old_ctl = getattr(self, '_last_port_ctl', None)
        self._last_port_ctl = new_ctl
        if old_ctl is None or old_ctl == new_ctl:
            return
        for piid in range(1, 5):
            bit = 1 << (piid - 1)
            was_on = bool(old_ctl & bit)
            now_on = bool(new_ctl & bit)
            if was_on != now_on:
                self._emit_port_state(piid)
                _LOGGER.info("Port %s %s (PIID16 changed: 0x%02X→0x%02X)",
                             PORT_NAMES[piid], "enabled" if now_on else "disabled",
                             old_ctl, new_ctl)

    async def _read_initial_settings(self):
        await self._fetch_settings(update_existing=False)
        for piid, pname in PORT_NAMES.items():
            self._publish_port(pname, PORT_DEFAULT, retain=True)

    async def _refresh_settings(self):
        await self._fetch_settings(update_existing=True)

    async def _process_commands(self):
        while True:
            try:
                cmd_type, cmd_data, cmd_future = self.cmd_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if cmd_type == "set":
                    await self._handle_set_command(cmd_data, cmd_future)
                elif cmd_type == "port":
                    await self._handle_port_command(cmd_data, cmd_future)
                elif cmd_type == "verify_port":
                    await self._handle_verify_port(cmd_data, cmd_future)

            except ConnectionError:
                # A command handler deliberately forced this to signal the
                # link is dead -- propagate to _connect_and_run's caller so
                # the normal reconnect path actually runs, instead of the
                # queue silently absorbing it forever.
                raise
            except Exception as e:
                _LOGGER.error("Command error: %s", e)
                if cmd_future and not cmd_future.done():
                    cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_set_command(self, cmd_data, cmd_future):
        piid, value = cmd_data
        try:
            res = await self.ctrl.send_miot_command(2, piid, value=value)
            # 同 _handle_port_command: 无响应/被拒才失败; ACK-only 视为成功,
            # 落地的是"意图写入的 value"而非设备回显(本机 SET 多为 ACK-only)。
            if not res:
                _LOGGER.warning("SET piid=%s rejected by device (no response), "
                                "state not updated", piid)
                if cmd_future and not cmd_future.done():
                    cmd_future.set_result(
                        {"ok": False, "error": "device did not confirm setting"})
                return
            await self.state.update_settings({str(piid): value})
            # 同步协议扩展缓存，防止后续 toggle 读到过期值
            if piid == 21:
                await self.state.update_protocol_extend(value)
                self._sse_emit("protocol", {"switches": self.state.protocol_switches,
                                            "protocol_extend": value})
            _invalidate()
            self._publish_settings(retain=True)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": True})
        except Exception as e:
            _LOGGER.error("Set command error: %s", e)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_port_command(self, cmd_data, cmd_future):
        port, action = cmd_data
        try:
            # 端口开关是"读-改-写 PIID16 位掩码"。基线取值策略（评审修正）：
            #   1. 权威值优先：先向设备 GET（健康链路上可靠，且不受缓存过期影响；
            #      固件可能通过倒计时自行关口，缓存最长可能滞后一两个刷新周期）
            #   2. GET 失败 → 回落本地缓存（绝不按 0 兜底：掩码算错会把其余正在
            #      供电的口一起关掉，且不可自愈）
            #   3. 两者都没有 → 拒绝写入
            # port == "all" 的掩码是常量（0x0F/0x00），不依赖基线，因此不做此限制。
            # 也不做"值未变就跳过"的判断：缓存可能滞后（固件倒计时会自行关口），
            # 若缓存恰好等于目标值就会整条命令被跳过、设备没收到写入却返回 ok，
            # 正是本改动要消除的"假成功"。all 的写入是幂等的，直接下发。
            if port == "all":
                new_val = 0x0F if action == "on" else 0x00
                cur_val = None
                always_write = True
            else:
                always_write = False
                cur_val = None
                cur = await self.ctrl.send_miot_command(2, 16)
                if cur and isinstance(cur.get("value"), int):
                    cur_val = cur["value"]
                    # 权威基线：值没变可以安全跳过写入
                else:
                    cached = self.state.settings.get("16")
                    if isinstance(cached, int):
                        cur_val = cached
                        # 基线不可信（缓存可能滞后于固件自行关口）→ 无条件下发，
                        # 否则"缓存恰好等于目标值"会让命令被整条跳过却返回 ok，
                        # 又是一次假成功。
                        always_write = True
                        _LOGGER.warning("PIID16 read failed, using cached mask 0x%02X "
                                        "as baseline (unconditional write)", cached)
                if cur_val is None:
                    _LOGGER.error("Port %s %s aborted: PIID16 baseline unknown, "
                                  "refusing to write", port, action)
                    if cmd_future and not cmd_future.done():
                        cmd_future.set_result(
                            {"ok": False, "error": "port state (PIID16) unavailable"})
                    return
                bit = PORT_BITS[port]
                new_val = cur_val | (1 << bit) if action == "on" else cur_val & ~(1 << bit)
            if always_write or new_val != cur_val:
                res = await self.ctrl.send_miot_command(2, 16, value=new_val)
                # SET 结果判读(实测本机 SET 多为 ACK-only, 没有回显值):
                #   None              → 无响应 / 设备回错误码拒绝 → 不落地
                #   value 非 None     → 有 Result 回显 → 以设备回显值为准
                #   ack_only          → 设备已接受但无回显 → 落地我们意图写入的值
                # 未确认就改写状态会让"设备没生效、前端显示已生效"长期分叉。
                if not res:
                    # res is None 覆盖两种情形：真的无响应/超时，或设备回了带错误码的
                    # Result（_recv_set_response 里检查 pt[9]/pt[10] 后返回 None）。
                    _LOGGER.warning("Port %s %s not confirmed by device "
                                    "(no response or device error), state not updated",
                                    port, action)
                    # 实测: 本机 PIID16 写入可能"回了错误码但设备已执行"。这里不谎报
                    # 成功，但立刻安排一次 settings 重读，让本地状态尽快收敛到真实值。
                    self._settings_refresh_now = True
                    if cmd_future and not cmd_future.done():
                        cmd_future.set_result(
                            {"ok": False, "error": "device did not confirm port change"})
                    return
                # 设备回显与我们意图不一致时，以设备回显为准：settings["16"] 是后续
                # 每次读-改-写的基线，写入意图值会让基线永久偏移，之后的开关可能在
                # 错误的掩码上做 OR/AND（误开或误关用户没碰过的口）。
                effective_val = new_val
                if res.get("value") is not None and int(res["value"]) != new_val:
                    effective_val = int(res["value"])
                    _LOGGER.warning("Port %s %s: device reports mask 0x%02X "
                                    "(intended 0x%02X), adopting device value",
                                    port, action, effective_val, new_val)
                await self.state.update_settings({"16": effective_val})
                # Emit port state for all changed ports (SSE + MQTT)
                if port == "all":
                    for piid in range(1, 5):
                        if not bool(effective_val & (1 << (piid - 1))):
                            if self._session_active(piid):
                                self._close_session(piid, time.time(),
                                                    reason=END_REASON_USER_OFF)
                            await self.state.update_port(piid, PORT_DEFAULT)
                        self._emit_port_state(piid)
                else:
                    piid = {"c1": 1, "c2": 2, "c3": 3, "a": 4}.get(port)
                    if piid:
                        if action == "off":
                            if self._session_active(piid):
                                # 用户手动关端口，或限额触发（_enforce_charge_limit
                                # 入队的正是 ("port", (name,"off"))）——两者同路。
                                self._close_session(piid, time.time(),
                                                    reason=END_REASON_USER_OFF)
                            await self.state.update_port(piid, PORT_DEFAULT)
                        self._emit_port_state(piid)
                _invalidate()
            _invalidate()
            self._publish_settings(retain=True)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": True, "value": new_val})
        except Exception as e:
            _LOGGER.error("Port command error: %s", e)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_verify_port(self, cmd_data, cmd_future):
        """Actively GET a port's status rather than waiting for a BLE push.

        Used for two cases:
          - C1/C2 idle too long (push frame for an unplug may have been lost,
            leaving a stale V/I forever).
          - C3/A, which the firmware never pushes live notifications for at
            all — they only get discovered/refreshed via this active GET.
        """
        piid = cmd_data if isinstance(cmd_data, int) else (cmd_data[0] if isinstance(cmd_data, (list, tuple)) else None)
        self._pending_verify.discard(piid)
        if not piid or piid not in range(1, 5) or not self.ctrl:
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": "invalid piid"})
            return
        try:
            result = await self.ctrl.send_miot_command(2, piid)
            self._last_verify_time[piid] = time.time()
            if not result or not result.get("raw"):
                # No response — connection may be stale.
                self._verify_fail_streak += 1
                _LOGGER.debug("verify_port piid=%d: no response (streak=%d)",
                               piid, self._verify_fail_streak)
                if cmd_future and not cmd_future.done():
                    cmd_future.set_result({"ok": False, "error": "no response"})
                if self._verify_fail_streak >= self._VERIFY_FAIL_STREAK_LIMIT:
                    raise ConnectionError(
                        f"verify_port failed {self._verify_fail_streak}x in a row, forcing reconnect")
                return
            self._verify_fail_streak = 0
            raw = result["raw"]
            hw_protocol = await self.state.get_hw_protocol(piid)
            pdo_data = None
            if piid in (1, 2):
                pdo_data = self.state.pdo_caps.get("c1c2", {}).get(PORT_NAMES[piid])
            elif piid in (3, 4):
                pdo_data = self.state.pdo_caps.get("c3a", {}).get(PORT_NAMES[piid])
            port_info = decode_port(piid, raw, pdo_data,
                                    protocol_switches=self.state.protocol_switches,
                                    hw_protocol=hw_protocol)
            if not port_info:
                _LOGGER.debug("verify_port piid=%d: decode_port returned None (raw=%s)",
                              piid, raw.hex())
                if cmd_future and not cmd_future.done():
                    cmd_future.set_result({"ok": False, "error": "decode failed"})
                return
            old = self.state.ports.get(piid)
            # 会话还开着但端口已无负载 → 必须结束会话。这与"读数有没有变化"无关：
            # 拔出后的 0V/0A 可能早就写进状态了（丢的只是"结束会话"这一步），
            # 只看变化的话这个会话会一直挂到重启。
            if not port_info["active"] and self._session_active(piid):
                self._close_session(piid, time.time(), reason=END_REASON_UNPLUG)
            # Only update if the live reading differs meaningfully from what we
            # hold — prevents redundant MQTT/SSE emits on unchanged polls.
            if old is None or (port_info["voltage"], port_info["current"]) != (old.voltage, old.current):
                _LOGGER.info("verify_port: Port %s update: %s", PORT_NAMES[piid], port_info)
                await self.state.update_port(piid, port_info)
                _invalidate()
                self._emit_port_state(piid, port_info)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": True, "value": result.get("value")})
        except ConnectionError:
            # Deliberately forced above once verify_port has failed
            # repeatedly -- let it propagate to trigger a real reconnect
            # instead of being swallowed like an ordinary command error.
            raise
        except Exception as e:
            _LOGGER.warning("verify_port piid=%d error: %s", piid, e)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_inline_data(self, data):
        if not self.ctrl:
            return
        await self.ctrl.client.write_gatt_char(
            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x03, 0x00]), response=False)
        await self._try_process_inline_frame(data)

    async def _handle_hw_protocol_push(self, piid: int, val32: int):
        """处理 PIID 17/18 协议号推送（固件 PD/PPS 协商变更时主动推送）。

        布局对齐米家 parseC1C2ProtocolInfo（u32, MSB→LSB）:
          PIID17: [C1_proto][C1_power][C2_proto][C2_power]
          PIID18: [C3_proto][C3_power][A_proto ][A_power ]
        即时更新 hw_protocol 缓存与 pdo_caps，并向前端广播协议开关状态。
        """
        hi_proto = (val32 >> 24) & 0xFF
        lo_proto = (val32 >> 8) & 0xFF
        if piid == 17:
            await self.state.set_hw_protocol_codes(hi_proto, lo_proto)
            pdo_key, high_port, low_port = "c1c2", "c1", "c2"
        else:
            await self.state.set_hw_protocol_codes_c3a(hi_proto, lo_proto)
            pdo_key, high_port, low_port = "c3a", "c3", "a"
        pdo_caps = dict(self.state.pdo_caps)
        pdo_caps[pdo_key] = decode_pdo_caps(val32, high_port, low_port)
        await self.state.update_pdo_caps(pdo_caps)
        self.state.settings[str(piid)] = val32
        _LOGGER.info("PIID%d hw_protocol push: hi=%d lo=%d (raw=0x%08X)",
                     piid, hi_proto, lo_proto, val32)

    def _decrypt_payload(self, encrypted_payload, raw_data=None):
        """解密设备载荷并做会话失步判定。成功返回明文, 失败返回 None。

        内联帧传 raw_data[4:], 多帧传"拼接后的整体 payload"——两者都是设备
        加密的完整载荷, 必须整体解密一次。多帧的每个子帧只有 2 字节帧号前缀,
        逐个按内联帧剥 4 字节解密必然偏移错位、解密失败(并因此触发误重连)。
        """
        pt = self.ctrl.decrypt(encrypted_payload)
        if pt and len(pt) >= 8:
            self._decrypt_failures = 0
            _LOGGER.debug("decrypt ok: len=%d pt=%s", len(pt), pt.hex())
            return pt
        if not pt:
            it = raw_data[:2].hex() if raw_data is not None and len(raw_data) >= 2 else "??"
            kind = ("single" if raw_data is not None and len(raw_data) >= 3 and raw_data[2] == 0x02
                    else ("multi" if raw_data is not None and len(raw_data) >= 3 and raw_data[2] == 0x00
                          else "other"))
            _LOGGER.debug("decrypt failed (kind=%s it=0x%s)", kind, it)
        else:
            _LOGGER.debug("decrypt output too short (%d < 8)", len(pt))
        self._decrypt_failures += 1
        if self._decrypt_failures >= self.DECRYPT_FAIL_LIMIT:
            _LOGGER.warning("Decrypt failed %d times consecutively, session stale, "
                            "triggering reconnect", self._decrypt_failures)
            raise ConnectionError("Session stale due to consecutive decrypt failures")
        return None

    async def _try_process_inline_frame(self, raw_data):
        """内联帧(data[2]==0x02): 剥 4 字节头后解密, 再交给下游处理。"""
        if not self.ctrl:
            return
        _LOGGER.debug("inline_frame: raw=%s len=%d",
                      raw_data.hex() if raw_data else "null",
                      len(raw_data) if raw_data else 0)
        pt = self._decrypt_payload(raw_data[4:], raw_data)
        if pt is None:
            return
        await self._process_decrypted_frame(pt)

    async def _process_decrypted_frame(self, pt):
        """处理已解密的 MiOT 明文帧(端口推送 / 协议号推送等)。"""
        b4 = pt[4]
        piid = pt[7] if len(pt) > 7 else -1

        # PIID 17/18 协议号推送（固件在 PD/PPS 协商变更时主动推送，对齐米家
        # parseC1C2ProtocolInfo）：value = u32，[proto_hi][pwr_hi][proto_lo][pwr_lo]。
        # 即时更新 hw_protocol 缓存，消除 60s 刷新周期内的协议显示滞后。
        if b4 == 0x04 and piid in (17, 18):
            if len(pt) >= 16:
                await self._handle_hw_protocol_push(piid, int.from_bytes(pt[12:16], 'little'))
            return

        # 优先使用 PIID 17 的硬件协议代码 (c1_c2_protocol Spec 属性)
        # 与米家 parseC1C2ProtocolInfo 一致: byte[0]=C1, byte[2]=C2
        hw_protocol = await self.state.get_hw_protocol(piid)

        if b4 == 0x04 and piid in PORT_NAMES:
            pdo_data = None
            if piid in (1, 2):
                pdo_data = self.state.pdo_caps.get("c1c2", {}).get(PORT_NAMES[piid])
            elif piid in (3, 4):
                pdo_data = self.state.pdo_caps.get("c3a", {}).get(PORT_NAMES[piid])
            port_info = decode_port(piid, pt, pdo_data,
                                    protocol_switches=self.state.protocol_switches,
                                    hw_protocol=hw_protocol)
            if port_info:
                _LOGGER.info("Port %s update: %s", PORT_NAMES[piid], port_info)
            else:
                _LOGGER.debug("Port %s: decode_port returned None (pt=%s)", PORT_NAMES[piid], pt.hex())
            if port_info:
                # Protocol debounce: only update protocol after N consecutive same readings
                new_proto = port_info.get("protocol", "")
                buf = self._proto_buf[piid]
                buf.append(new_proto)
                if len(buf) > self._PROTO_DEBOUNCE_N:
                    buf.pop(0)
                old = self.state.ports.get(piid)
                if old and len(buf) >= self._PROTO_DEBOUNCE_N:
                    if len(set(buf)) == 1:
                        # All N readings are the same — stable, use new protocol
                        pass
                    else:
                        # Not stable yet — keep old protocol
                        port_info["protocol"] = old.protocol
                # Port idle → clear protocol immediately (don't let debounce block it)
                if not port_info.get("active", True):
                    port_info["protocol"] = "idle"
                    self._proto_buf[piid].clear()
                await self.state.update_port(piid, port_info)
                if old is None or old.to_dict() != port_info:
                    _invalidate()
                    self._emit_port_state(piid, port_info)

                # ── Data processing: runs on EVERY push, not gated by change detection ──
                voltage = port_info.get("voltage", 0)
                current = port_info.get("current", 0)
                timestamp = time.time()
                es = self._energy_states[piid]

                # Accumulate energy (trapezoidal integration needs continuous timestamps)
                self._energy_integrator.update(es, voltage, current, timestamp)

                # 会话生命周期：唯一入口（定时器路径也走它），内部按"时间窗+功率"判定
                self._manage_session(piid, timestamp, voltage, current,
                                     active=port_info.get("active", False),
                                     protocol=port_info.get("protocol", ""))

                # 充电量达到阈值 → 入队关断该端口（限流断电）。
                # 放在会话管理之后：本帧若已因拔插/低电流结束会话（is_charging
                # 已置 False），判定自然跳过，不会多入队一条多余的 off 命令。
                # 与用户命令共用 _handle_port_command 路径（命令循环异步执行后
                # 才真正关断），本帧继续走完采样/曲线写入无副作用。
                self._enforce_charge_limit(piid, timestamp)
                # 判满断电的确认/重试（挂起后端口已停止充电，与上面的判定互不影响）
                self._enforce_full_off(piid, timestamp)

                # 记录采样点（真实会话且记录开启时；全站仅此一处 + 定时器路径）
                if self._history and es.is_charging and piid in self._active_sessions:
                    self._record_charge_point(
                        piid, voltage, current, port_info.get("protocol", ""))

                # Record to history (existing)
                if self._history and port_info.get("active", False):
                    loop = asyncio.get_running_loop()
                    task = loop.run_in_executor(None, self._history.record_port_data, piid, port_info)
                    task.add_done_callback(
                        lambda t: _LOGGER.error("History write failed: %s", t.exception()) if t.exception() else None)

    async def _handle_multiframe(self, data):
        """多帧数据：拼接子帧后整体解密一次。

        子帧格式是 [帧号 2 字节][数据...]，与内联帧的 4 字节头不同——按内联帧
        逐个解密必然偏移错位。这里参照 _recv_auth_response 的正确做法：剥掉每
        个子帧的 2 字节帧号、拼接成完整加密载荷，再解密一次、交给下游处理。

        同时做了三重防阻塞（损坏的帧头可能上报超大 count）：
        - 帧数钳制到 MULTIFRAME_MAX_FRAMES
        - 总 deadline 兜底（原先逐帧 3s 超时 × 超大 count 可阻塞数十小时）
        - 收不到帧立即终止，不空转等满
        """
        if not self.ctrl:
            return
        raw_count = data[4] + 0x100 * data[5]
        frame_count = min(raw_count, self.MULTIFRAME_MAX_FRAMES)
        if frame_count != raw_count:
            _LOGGER.warning("Multiframe count %d exceeds limit, clamped to %d",
                            raw_count, frame_count)
        await self.ctrl.client.write_gatt_char(
            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x01]), response=False)
        payload = b''
        received_count = 0
        deadline = time.monotonic() + self.MULTIFRAME_DEADLINE_SEC
        for _ in range(frame_count):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _LOGGER.warning("Multiframe deadline reached after %d/%d frames",
                                received_count, frame_count)
                break
            frame = await self.ctrl.wait_notify("cmd_recv", timeout=min(remaining, 3.0))
            if not frame:
                break                      # 没帧了就停，不要空转等满 frame_count
            received_count += 1
            payload += frame[2:]           # 剥 2 字节帧号后拼接
        await self.ctrl.client.write_gatt_char(
            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x00]), response=False)
        if received_count != raw_count:
            # 不完整（丢帧 / 超时 / 被钳制截断）：拼接出来的载荷必然解不开,
            # 但那是"没收全"而不是"密钥失步" —— 不能计入 _decrypt_failures,
            # 否则连续几次坏帧就会触发一次毫无必要的整链重连。
            _LOGGER.warning("Multiframe incomplete: received %d/%d frames "
                            "(clamped to %d), skipping decrypt",
                            received_count, raw_count, frame_count)
            return
        if not payload:
            return
        # 整体解密一次（ConnectionError 让上层重连，与内联路径一致）
        pt = self._decrypt_payload(payload, data)
        if pt is None:
            return
        await self._process_decrypted_frame(pt)

    def _publish_status(self, payload, retain=False):
        if self._mqtt_publish:
            self._mqtt_publish(self.config.topic_status, payload, retain=retain)
        self._sse_emit("status", payload)

    def _publish_settings(self, retain=False):
        if self._mqtt_publish:
            self._mqtt_publish(self.config.topic_settings, self.state.settings, retain=retain)
        self._sse_emit("settings", {"settings": self.state.settings})

    def _publish_port(self, port_name, data, retain=False):
        if self._mqtt_publish:
            self._mqtt_publish(f"{self.config.topic_port}/{port_name}", data, retain=retain)

    async def send_command(self, cmd_type, cmd_data, timeout=None):
        if not self.ctrl or not self.state.authenticated:
            return {"ok": False, "error": "not connected"}
        timeout = timeout or self.config.server.command_timeout
        future = asyncio.get_running_loop().create_future()
        await self.cmd_queue.put((cmd_type, cmd_data, future))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "command timeout"}
