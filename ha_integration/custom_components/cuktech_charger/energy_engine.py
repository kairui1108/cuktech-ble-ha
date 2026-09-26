"""CUKTECH Charger — 充电量限额引擎（纯逻辑，无 Home Assistant 依赖）。

为什么独立成模块：与 `protocol_codec.py` 同款理由——把"多大的电流算会话在充"
这类有分支的核心计算从 HA 平台代码里抽出来，才能在不启动 Home Assistant 的
前提下红绿验证；平台文件只保留实体胶水。

本模块是 ble_server 端 `energy.py` + `ble_manager.py` 限额逻辑的移植，语义 1:1
对齐（同一台充电器、同一份 1Hz V/I 推送，换后端不该有认知差异）：

    ble_server/energy.py      →  AdaptiveEnergyIntegrator / ChargeEndDetector /
                                 limit_reached / normalize_charge_limit
    ble_server/ble_manager.py →  ChargeLimitTracker 的会话边界与 once/always 语义

移植范围有意为之地排除了所有持久化/异步/MQTT 副作用：HA 侧没有 in-flight
会话需要 ceil synchronously 落库，也不该把 sqlite 写压在事件循环上。

执行通道说明（重要）：本模块**只负责判定**，不负责关断。达到阈值后由调用方
发布 MQTT `{"port":..., "action":"off"}`——该 topic 两端早就通了：

    Python: ha_server.py 订阅 `{prefix}/port` → ble.cmd_queue → MIOT SET PIID 16
    ESP32:  main/main.c:356 订阅 `{prefix}/port` → CMD_SET PIID 16

所以同一套判定逻辑在两种后端下共用同一条断电路径，固件无需改动。
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional

# 限额常数取自 const.py（集成内的单一真源；见 CHANGELOG 记录的约定
# "magic values centralized in const.py"）。本模块只实现语义，不另立一份。
# LIMIT_MODE_ALWAYS 本模块不直接比较，但和它的兄弟一起再导出：模式名属于本
# 模块的公开契约（normalize_charge_limit 返回它们），调用方与测试都从这一处取。
from .const import (  # noqa: F401  (LIMIT_MODE_ALWAYS 为有意重导出)
    DEFAULT_LIMIT_MODE,
    LIMIT_MODE_ALWAYS,
    LIMIT_MODE_ONCE,
    LIMIT_MODES,
    PORT_MAP,
)

# ── 端口 ──
# 直接取 PORT_MAP 的 key（小写，与 MQTT topic 后缀 `cuktech/charger/port/{c1..a}`
# 一致）。不在这里另写一份字面量：CHANGELOG 1.1.0 记录过同类漂移
# （"订阅按 PORT_MAP 迭代，不再硬编码 ("c1","c2","c3","a")"）。
PORTS: tuple[str, ...] = tuple(PORT_MAP)

# ── 会话终止原因 ──
# 决定 once 限额是否被消费（见 ChargeLimitTracker.end_session）
END_REASON_USER_OFF = "port_off"      # 端口关闭（用户手动 / 限额触发）
END_REASON_UNPLUG = "unplug"          # 拔出负载（V=0, I=0）
END_REASON_LOW_POWER = "low_power"    # 功率衰减/低电流自然结束（充满）
END_REASON_LINK_LOSS = "link_loss"    # BLE 链路中断/重连（基础设施，会话可续）
END_REASON_SHUTDOWN = "shutdown"      # Home Assistant 停止/重启
END_REASON_UNKNOWN = "unknown"        # 未标注原因（保守：视为真实终止）

# 这些原因不清零 once 限额：会话不是用户意图终止的，保留用户刚设的限制。
# 否则一次 BLE 抖动或一次 HA 重启就会静默解除限制。
END_REASONS_PRESERVING_LIMIT = frozenset({END_REASON_LINK_LOSS, END_REASON_SHUTDOWN})


def limit_reached(session_wh: float, limit_wh: float) -> bool:
    """本会话输出能量是否已达到阈值（limit_wh <= 0 表示禁用）。"""
    if limit_wh <= 0:
        return False
    return session_wh >= limit_wh


def normalize_charge_limit(wh, mode=None) -> tuple[float, str]:
    """把外部输入（HA number 实体 / Store 脏数据）归一成 (wh, mode)。

    返回的 wh 恒为有限非负数（0 = 禁用），mode 恒为 LIMIT_MODES 之一。
    NaN/inf/负数/非数值一律视为 0（禁用），不抛异常——Store 里的脏数据与
    前端输入都不该让调用方崩溃。与 ble_server/energy.py 同名函数一致。
    """
    try:
        value = float(wh)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value < 0:
        value = 0.0
    norm_mode = str(mode).strip().lower() if mode is not None else DEFAULT_LIMIT_MODE
    if norm_mode not in LIMIT_MODES:
        norm_mode = DEFAULT_LIMIT_MODE
    return value, norm_mode


@dataclass
class PortEnergyState:
    """Per-port energy tracking state.

    Carries only what this module and its callers actually read. The server's
    `energy.PortEnergyState` tracks more (daily totals with a date stamp, peak
    current) because it also feeds session recording and history rows; HA has no
    such layer, and keeping unused fields here would imply behaviour — a daily
    rollover, say — that nothing implements.
    """
    total_wh: float = 0.0      # 自 HA 启动/Store 恢复起的累计（跨会话）
    session_wh: float = 0.0    # 本会话已输出能量 —— 限额判定用这个
    is_charging: bool = False
    last_power: float = 0.0
    last_time: Optional[float] = None
    max_power: float = 0.0
    last_end_time: float = 0.0  # 供"距上次结束 60s 内抬高启动阈值"防抖


class AdaptiveEnergyIntegrator:
    """Trapezoidal integration for charger output energy.

    Trapezoidal is used for all intervals — the accuracy difference vs Simpson
    at 1s push intervals is <0.1%, while trapezoidal avoids double-counting
    issues with overlapping Simpson windows on irregular data.

    MAX_GAP_SEC is load-bearing here, not defensive padding: on subscribe we
    immediately receive the broker's retained last sample, whose timestamp may
    be minutes-to-hours old. Without the gap guard that single frame would
    integrate a bogus multi-hour trapezoid and could trip a limit instantly.
    """

    MAX_GAP_SEC = 30.0

    def update(self, state: PortEnergyState, voltage: float, current: float,
               timestamp: float) -> float:
        """Update energy state with new measurement. Returns total_wh."""
        power = voltage * current

        if state.last_time is None:
            state.last_time = timestamp
            state.last_power = power
            return state.total_wh

        dt = timestamp - state.last_time

        # Skip irregular intervals (disconnection, retained first frame, clock rollback)
        if dt <= 0 or dt > self.MAX_GAP_SEC:
            state.last_time = timestamp
            state.last_power = power
            return state.total_wh

        dt_hours = dt / 3600.0

        # Trapezoidal integration
        energy = (state.last_power + power) / 2.0 * dt_hours

        state.total_wh += energy
        state.session_wh += energy
        state.last_power = power
        state.last_time = timestamp
        if power > state.max_power:
            state.max_power = power

        return state.total_wh


class ChargeEndDetector:
    """Determines charging session boundaries using power-based threshold.

    Session ends when avg power < 1W for 10 consecutive minutes. This catches
    both gradual trickle-down and sudden disconnect, regardless of voltage.
    """

    LOW_POWER_DURATION_SEC = 600  # 10 minutes
    COOLDOWN_SEC = 30
    WINDOW_SIZE = 300

    def __init__(self):
        self._low_power_start: Optional[float] = None
        self._cooldown_until: float = 0
        self._power_window: deque = deque(maxlen=1800)

    def update(self, power: float, timestamp: float) -> None:
        """Track power over time."""
        self._power_window.append(power)

    def should_end_session(self, state: PortEnergyState, timestamp: float) -> bool:
        """Check if charging session should end (avg power < 1W for 10min)."""
        if timestamp < self._cooldown_until:
            return False

        if len(self._power_window) < self.WINDOW_SIZE:
            return False

        avg = statistics.mean(list(self._power_window)[-self.WINDOW_SIZE:])
        threshold = 1.0  # ~0.05A at 20V or ~0.2A at 5V

        if avg < threshold:
            if self._low_power_start is None:
                self._low_power_start = timestamp
            if timestamp - self._low_power_start > self.LOW_POWER_DURATION_SEC:
                return True
        else:
            self._low_power_start = None

        return False

    def reset(self, timestamp: float = 0.0) -> None:
        """Clear all state (session ended / port re-armed)."""
        self._cooldown_until = timestamp + self.COOLDOWN_SEC
        self._low_power_start = None
        self._power_window.clear()


@dataclass
class LimitConfig:
    """One port's persisted limit configuration."""
    wh: float = 0.0                      # 0 = 禁用
    mode: str = DEFAULT_LIMIT_MODE


@dataclass
class _PortRuntime:
    """Per-port runtime non persisted state.

    total_wh IS persisted across restarts so long term accounting survives
    HA bounces; everything else is rebuilt from live MQTT samples.
    """
    state: PortEnergyState = field(default_factory=PortEnergyState)
    detector: ChargeEndDetector = field(default_factory=ChargeEndDetector)
    low_current_count: int = 0
    fired: bool = False
    fired_at: float = 0.0


class ChargeLimitTracker:
    """4-port charge-limit engine: accumulate Wh, detect sessions, decide cutoff.

    Pure logic — callers own persistence (HA `Store`) and actuation
    (MQTT port off). `ingest()` mutates state; `due_cutoffs()` returns the
    ports to switch off **now**, leaving the publish decision to the caller.

    Session start/end mirrors ble_server `_process_push_probe` exactly:

        start: active and current > threshold(0.1, or 0.3 within 60s of last end)
        end:   port inactive           -> user_off
               current <= 0.1 for N    -> low_power
               detector: avg<1W 10min  -> low_power
    """

    # consecutive readings below threshold to end session (ble_manager: _LOW_CURRENT_N)
    LOW_CURRENT_N = 300
    START_THRESHOLD = 0.1
    START_THRESHOLD_RECENT = 0.3   # within RECENT_END_SEC of the previous session end
    RECENT_END_SEC = 60
    LIMIT_RETRY_SEC = 15           # mirrors ble_manager.LIMIT_RETRY_SEC (命令超时 ~10s)

    def __init__(self, ports: Iterable[str] = PORTS) -> None:
        self._ports: tuple[str, ...] = tuple(ports)
        self._limits: dict[str, LimitConfig] = {p: LimitConfig() for p in self._ports}
        self._rt: dict[str, _PortRuntime] = {p: _PortRuntime() for p in self._ports}
        self._integrator = AdaptiveEnergyIntegrator()

    # ── Configuration ────────────────────────────────────────────────

    @property
    def ports(self) -> tuple[str, ...]:
        return self._ports

    def set_limit(self, port: str, wh, mode=None) -> LimitConfig:
        """Set one port's limit. wh<=0 disables. Returns the applied config.

        `mode=None` keeps the port's current mode instead of resetting it to the
        default. That mirrors ble_server's `handle_charge_limits`, which merges
        an omitted `mode` into the stored config (`if "mode" in entry`) — so
        editing only the threshold (the HA number entity does exactly that)
        doesn't silently turn a user's `always` back into `once`.
        """
        rt = self._rt.get(port)
        if rt is None:
            return LimitConfig()
        cfg = self._limits[port]
        cfg.wh, _ = normalize_charge_limit(wh, None)
        if mode is not None:
            _, cfg.mode = normalize_charge_limit(cfg.wh, mode)
        # Changing the limit re-arms enforcement for the running session.
        rt.fired = False
        return cfg

    def set_mode(self, port: str, mode) -> LimitConfig:
        """Change only the mode, preserving wh."""
        return self.set_limit(port, self._limits[port].wh, mode)

    def get_limit(self, port: str) -> LimitConfig:
        return self._limits.get(port, LimitConfig())

    def load_limits(self, raw: dict) -> None:
        """Restore persisted {port: {"wh":..,"mode":..}} tolerating junk.

        Unknown ports ignored; missing ports left disabled. Corrupt values
        normalize to disabled instead of raising — a bad Store file must not
        brick integration setup.
        """
        if not isinstance(raw, dict):
            return
        for port in self._ports:
            entry = raw.get(port)
            if isinstance(entry, dict):
                wh, mode = normalize_charge_limit(entry.get("wh"), entry.get("mode"))
            else:
                wh, mode = normalize_charge_limit(entry, None)
            cfg = self._limits[port]
            cfg.wh = wh
            cfg.mode = mode

    def snapshot_limits(self) -> dict:
        """Serialize configuration for persistence."""
        return {p: {"wh": c.wh, "mode": c.mode} for p, c in self._limits.items()}

    # ── Energy state (read-only views for entities) ──────────────────

    def session_wh(self, port: str) -> float:
        return self._rt[port].state.session_wh

    def total_wh(self, port: str) -> float:
        return self._rt[port].state.total_wh

    def is_charging(self, port: str) -> bool:
        return self._rt[port].state.is_charging

    def has_fired(self, port: str) -> bool:
        return self._rt[port].fired

    def max_power(self, port: str) -> float:
        return self._rt[port].state.max_power

    def remaining_wh(self, port: str) -> Optional[float]:
        """Remaining Wh before cutoff, None when no limit is armed."""
        cfg = self._limits[port]
        if cfg.wh <= 0:
            return None
        return max(0.0, cfg.wh - self._rt[port].state.session_wh)

    def restore_totals(self, raw: dict) -> None:
        """Restore accumulated total_wh per port from a previous HA run.

        session_wh intentionally NOT restored: the in-flight session's energy
        cannot be recovered (HA wasn't sampling), and restoring half of it
        would risk a premature cutoff. Restarts err toward over-charging.
        """
        if not isinstance(raw, dict):
            return
        for port in self._ports:
            try:
                value = float(raw.get(port))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                self._rt[port].state.total_wh = value

    def snapshot_totals(self) -> dict:
        return {p: self._rt[p].state.total_wh for p in self._ports}

    # ── Core loop ────────────────────────────────────────────────────

    def ingest(self, port: str, voltage: float, current: float,
               active: bool, timestamp: float) -> None:
        """Feed one MQTT port sample. Mutates energy/session state."""
        rt = self._rt.get(port)
        if rt is None:
            return
        es = rt.state

        self._integrator.update(es, voltage, current, timestamp)
        rt.detector.update(voltage * current, timestamp)

        # Gradual power decline check runs on every sample, not just low current.
        if es.is_charging and rt.detector.should_end_session(es, timestamp):
            self.end_session(port, END_REASON_LOW_POWER, timestamp)
            return

        # Session management (mirrors ble_manager session block)
        start_threshold = self.START_THRESHOLD
        if es.last_end_time and (timestamp - es.last_end_time) < self.RECENT_END_SEC:
            start_threshold = self.START_THRESHOLD_RECENT

        if active and current > start_threshold and not es.is_charging:
            # Start new session — also re-arms enforcement (this is what makes
            # `always` survive across sessions; `once` consumed itself already).
            rt.low_current_count = 0
            es.is_charging = True
            es.session_wh = 0.0
            es.max_power = voltage * current
            rt.fired = False
            rt.detector.reset(timestamp)
        elif not active and es.is_charging:
            # Port switched off — end immediately (user action or our own cutoff)
            self.end_session(port, END_REASON_USER_OFF, timestamp)
        elif current <= 0.1 and es.is_charging:
            rt.low_current_count += 1
            if rt.low_current_count >= self.LOW_CURRENT_N:
                self.end_session(port, END_REASON_LOW_POWER, timestamp)
        elif current <= 0.1 and not es.is_charging:
            pass  # idle port, nothing to do

    def end_session(self, port: str, reason: str = END_REASON_UNKNOWN,
                    timestamp: float = 0.0) -> None:
        """Terminate the current session and apply once-mode consumption.

        - `fired` always resets (session is over — double safety with re-arming).
        - once: consumed only on real termination; link_loss / shutdown preserve
          the limit because the session is resumable.
        - always: untouched, re-armed at next session start.
        """
        rt = self._rt.get(port)
        if rt is None:
            return
        es = rt.state
        rt.fired = False
        rt.low_current_count = 0
        rt.detector.reset(timestamp)
        was_charging = es.is_charging
        es.is_charging = False
        if was_charging:
            es.last_end_time = timestamp

        cfg = self._limits[port]
        if cfg.wh <= 0 or cfg.mode != LIMIT_MODE_ONCE:
            return
        if reason in END_REASONS_PRESERVING_LIMIT:
            return
        # consumed
        cfg.wh = 0.0

    def end_session_all(self, reason: str = END_REASON_SHUTDOWN,
                        timestamp: float = 0.0) -> None:
        """Terminate every active session (integration unload / HA shutdown).

        Callers should pass END_REASON_SHUTDOWN (the default) so that `once`
        limits survive an HA restart: the charge itself was not terminated by
        user intent, and silently dropping a freshly armed limit would be a
        nasty surprise.
        """
        for port in self._ports:
            self.end_session(port, reason, timestamp)

    def due_cutoffs(self, timestamp: float) -> list[str]:
        """Ports whose session energy reached its armed limit and need cutoff.

        Idempotent within LIMIT_RETRY_SEC: once `fired`, repeat calls are
        suppressed so we don't spam the broker while the command is in flight.
        If the port is still charging after the retry window, the command
        evidently failed — reset so the next sample retries.
        """
        due: list[str] = []
        for port in self._ports:
            cfg = self._limits[port]
            if cfg.wh <= 0:
                continue
            rt = self._rt[port]
            es = rt.state
            if not es.is_charging or not limit_reached(es.session_wh, cfg.wh):
                continue
            if rt.fired:
                if timestamp - rt.fired_at < self.LIMIT_RETRY_SEC:
                    continue
                # Command didn't take effect — allow retry this round.
                rt.fired = False
            rt.fired = True
            rt.fired_at = timestamp
            due.append(port)
        return due
