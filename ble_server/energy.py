"""CUKTECH BLE Server - Energy accumulation with adaptive integration."""
import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Optional


# ── Charge limits (自动断电阈值) ──
# 阈值的语义是"充电器输出能量"（PortEnergyState.session_wh，由 V×I 梯形积分得出），
# 不是被充设备的实际充入电量——线损与设备内转换损耗使后者偏小（典型 5~15%）。
MAX_LIMIT_WH = 1000.0
LIMIT_MODE_ONCE = "once"      # 达到阈值即关断并消费清零（一次性）
LIMIT_MODE_ALWAYS = "always"  # 长期有效，每次充电会话重新武装
LIMIT_MODES = (LIMIT_MODE_ONCE, LIMIT_MODE_ALWAYS)
DEFAULT_LIMIT_MODE = LIMIT_MODE_ONCE


def limit_reached(session_wh: float, limit_wh: float) -> bool:
    """本会话输出能量是否已达到阈值（limit_wh <= 0 表示禁用）。"""
    if limit_wh <= 0:
        return False
    return session_wh >= limit_wh


def normalize_charge_limit(wh, mode=None) -> tuple:
    """把外部输入（API 请求 / DB meta）归一成 (wh, mode)，非法输入回落禁用。

    返回的 wh 恒为有限非负数（0 = 禁用），mode 恒为 LIMIT_MODES 之一。
    NaN/inf/负数/非数值一律视为 0（禁用），不抛异常——DB meta 脏数据与
    前端输入都不该让调用方崩溃。
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
    """Per-port energy tracking state."""
    total_wh: float = 0.0
    session_wh: float = 0.0
    daily_wh: float = 0.0
    daily_date: str = ""
    is_charging: bool = False
    session_start: Optional[float] = None
    last_power: float = 0.0
    last_time: Optional[float] = None
    max_power: float = 0.0
    max_current: float = 0.0
    last_end_time: float = 0.0


class AdaptiveEnergyIntegrator:
    """Trapezoidal integration for charger output energy.

    Trapezoidal integration is used for all intervals — the accuracy
    difference vs Simpson at 1s BLE push intervals is <0.1%, while
    trapezoidal is simpler and avoids double-counting issues with
    overlapping Simpson windows on irregular data.
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

        # Skip irregular intervals (disconnection, pause, time rollback)
        if dt <= 0 or dt > self.MAX_GAP_SEC:
            state.last_time = timestamp
            state.last_power = power
            return state.total_wh

        dt_hours = dt / 3600.0

        # Trapezoidal integration
        energy = (state.last_power + power) / 2.0 * dt_hours

        state.total_wh += energy
        state.session_wh += energy
        state.daily_wh += energy
        state.last_power = power
        state.last_time = timestamp
        if power > state.max_power:
            state.max_power = power
        if current > state.max_current:
            state.max_current = current

        return state.total_wh


# ── 会话边界：全部按"时间窗 + 功率"，抗波动、与采样率无关 ──

def _median(values) -> float:
    return statistics.median(values) if values else 0.0


# ── "充满"判定的阈值（单一来源：tail_threshold_w 与 SessionStartGate 都用它）──
# T = max(TAIL_MIN_W, min(TAIL_CAP_W, TAIL_PEAK_RATIO × 会话充电峰值))
#
# 上限为什么是 0.6W 而不是 0.5W：这条线是**绝对功率**，而充电器的**电流上报步进
# 是绝对的**（实机 C1 是 0.1A/档）。5V/PPS 档下 0.1A × 5.1V = 0.51W 恰好跨过 0.5W，
# 而判据是严格大于——设备最典型的涓流（0/0.1A 交替）于是有一半样本被判成"仍在充电"，
# 收敛计时被 120s 波动预算反复清零，会话永远判不满（实机 C1 回放：3 小时 0 次判满）。
# 0.6W 让它明确落在阈值内侧，同时 0.15A（0.75W）仍算在充电；20V 档 0.6W ≈ 0.03A，
# 与旧值几乎无差别（这类大功率设备的尾巴是 1W 量级，本来就不靠这条线判满）。
TAIL_MIN_W = 0.05        # 噪声地板（≈0.01A@5V）：阈值再低也不低于它
TAIL_CAP_W = 0.6         # 阈值上限
TAIL_PEAK_RATIO = 0.20   # 相对项：20% × 会话充电峰值（小功率设备靠它定阈值）


def tail_threshold_w(peak_power: float) -> float:
    """"充满"的功率阈值：T = max(TAIL_MIN_W, min(TAIL_CAP_W, 20% × 充电峰值))。

    判据的物理依据：**真满时功率是逐渐逼近 0 的**（CC→CV 收敛后设备几乎不再取电），
    所以这条线只取"接近 0"的量级：
      · 大功率设备 → TAIL_CAP_W 上限（0.6W：5V 下 0.1A 的涓流要能判满，见上方说明）；
      · 小功率设备（5V/0.5~1W 耳机）→ 按 20% 自身比例：0.1 / 0.2W；
      · 0.05W 是噪声地板。
    **偶发抖动（例如 300s 窗口里偶尔跳到 1.5W）不靠阈值兜**：交给
    20s/180s 中位数 + 波动预算(GRACE_SEC) + 与阈值解耦的回升门槛(RECOVER_FLOOR_W)，
    所以"偶尔跳一下"既不会让判据失效，也不会被误判成充满。

    注意：阈值只由本模块的 TAIL_* 常量决定（历史上类里另有一份同名常量，从不参与
    计算，改它没有任何效果——已删除，避免再次误导）。
    """
    return max(TAIL_MIN_W, min(TAIL_CAP_W, TAIL_PEAK_RATIO * (peak_power or 0.0)))


class ChargeSessionDetector:
    """判断充电会话是否"自然结束"（设备充满 / 进入维持状态）。

    为什么不再用"电流 ≤0.1A 连续 300 帧"这类判据：
      1) 帧数 ≠ 时间：C1 实测采样 2.0s/点 → 300 帧是 10 分钟；C3/USB-A 没有推送、
         由 1s 定时器驱动 → 300 帧是 5 分钟。采样率一变，"多久算结束"就跟着变。
      2) 电流阈值与电压档位耦合：0.1A 在 20V 是 2W、在 5V 是 0.5W。
      3) 逐帧计数：偶发一次波动就清零，反而让"1.5W 残留 + 偶发尖峰"这种真实尾巴
         被判成"还在充"，迟迟不结束。

    新判据（全部按秒计算，与采样率无关）：
      · p_fast = 最近 FAST_SEC 的功率中位数（中位数天然抗单帧尖峰）
      · p_slow = 最近 SLOW_SEC 的功率中位数（判趋势是否已经平下来）
      · 阈值 T_low = max(TAIL_MIN_W, min(TAIL_CAP_W, TAIL_PEAK_RATIO × p_base))
        （模块常量，见 tail_threshold_w）：上限而不是地板——大功率设备"充满后仅剩
        系统负载"（1~2W）由回升门槛兜住；小功率设备（5V/0.5~1W 的耳机）按自身比例
        算，阈值降到 0.1~0.2W，否则会出现"还在充就被判满"。
      · 波动豁免：考察窗内"超阈值(OVER_MULT×T_low)时间占比" ≤ OVER_RATIO，
        且最近 RECENT_SEC 内没有连续 ≥ RECOVER_SEC 的回升 → 仍视为收敛；
        真正恢复充电（持续回升）→ 收敛计时清零重数
      · 收敛状态需连续保持 HOLD_SEC 才判定结束
    电压低于 VOLTAGE_FLOOR_V 时不算收敛：掉线/拔出不属于"低功率自然结束"，
    那条由会话管理按端口状态去抖后处理。
    """

    FAST_SEC = 20.0              # p_fast 窗口
    SLOW_SEC = 180.0             # p_slow 窗口
    ASSESS_SEC = 300.0           # 波动占比考察窗
    RECENT_SEC = 90.0            # "回升"只在这段内考察（避免几分钟前的尖峰长期阻塞）
    HOLD_SEC = 600.0             # 收敛需连续保持多久 → 判定结束（可配）
    # 阈值的三个常量在模块级（TAIL_MIN_W / TAIL_CAP_W / TAIL_PEAK_RATIO），由
    # tail_threshold_w() 统一计算：这里**不再**放同名常量——历史上放了一份却从不
    # 参与计算，改它没有任何效果（排查"为什么不判满"时被误导过一次）。
    GRACE_SEC = 120.0            # HOLD 窗口内容忍的"非收敛"总时长（≈20%，偶发波动豁免）
    RECOVER_FLOOR_W = 5.0        # "充电恢复"的绝对门槛：几瓦的屏幕/系统负载波动不算恢复
    RECOVER_SEC = 45.0           # 连续超过恢复门槛该秒数 → 判定充电恢复，计时清零
    MIN_WH = 0.05                # 有效会话门槛：能量（小到只挡"插一下就走"；
                                 #   大功率也由 HOLD_SEC 兜着，不会因此早断）
    MIN_SEC = 180.0              # 有效会话门槛：时长
    VOLTAGE_FLOOR_V = 4.0

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        """新会话/拔出/关闭端口：清空全部窗口与状态。"""
        self._samples = deque()      # (t, power)：按时间裁剪的原始窗口
        self._peak = 0.0             # 会话充电峰值 = p_fast 的历史最大值（单调）
        self._hold_since = None
        self._grace = 0.0
        self._last_eval = None
        self._last_t = None
        self._last_v = None

    # ── 采样 ──

    def feed(self, timestamp: float, voltage: float, current: float) -> None:
        power = max(0.0, float(voltage) * float(current))
        self._last_t = timestamp
        self._last_v = voltage
        self._samples.append((timestamp, power))
        cutoff = timestamp - self.ASSESS_SEC
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if voltage < self.VOLTAGE_FLOOR_V:
            # 掉线/拔出：不能算"低功率收敛"，同时清掉计时
            self._hold_since = None
        # 充电峰值 = p_fast（20s 中位数）的历史最大值：单调不降，涓流尾巴不会把它拉低，
        # 也不需要"取哪一段当充电段"的判断——大功率/小功率设备都能自校准。
        fast = self.p_fast()
        if fast > self._peak:
            self._peak = fast

    # ── 统计量（全部按时间窗，不依赖采样个数）──

    def _median_since(self, since: float) -> float:
        return _median([p for t, p in self._samples if t >= since])

    def p_fast(self) -> float:
        return self._median_since((self._last_t or 0.0) - self.FAST_SEC)

    def p_slow(self) -> float:
        return self._median_since((self._last_t or 0.0) - self.SLOW_SEC)

    def peak_power(self) -> float:
        """本会话充电峰值（p_fast 的单调最大值，涓流尾巴不会把它拉低）。"""
        return self._peak

    def threshold_w(self) -> float:
        return tail_threshold_w(self._peak)

    def recover_threshold_w(self) -> float:
        """"充电恢复"门槛：max(5W, 2×T_low)——与 T_low 解耦，避免小阈值下几瓦就判回升。"""
        return max(self.RECOVER_FLOOR_W, self.threshold_w() * 2.0)

    def recovery_sec(self) -> float:
        """最近 RECENT_SEC 内最长的一次连续回升秒数（门槛见 recover_threshold_w）。"""
        limit = self.recover_threshold_w()
        cutoff = (self._last_t or 0.0) - self.RECENT_SEC
        run = longest = 0.0
        prev = None
        for t, p in self._samples:
            if t < cutoff:
                prev = t
                continue
            dt = (t - prev) if prev is not None else 0.0
            prev = t
            if p > limit:
                run += max(0.0, dt)
                longest = max(longest, run)
            else:
                run = 0.0
        return longest

    def converged(self) -> bool:
        """此刻是否"已收敛"：20s/180s 中位数都在阈值下，且电压仍在协商范围内。

        判定只用中位数：几秒的偶发波动撼动不了 20s/180s 的中位数；万一波动长到
        把中位数顶上去，那几秒记作"非收敛"，由 should_end 的波动预算(GRACE_SEC)
        吸收——不会因为"偶尔跳一下"就把 10 分钟计时清零。
        持续回升（≥RECOVER_SEC 超过恢复门槛）由 should_end 单独处理：那要清零重数。
        """
        if self._last_t is None:
            return False
        th = self.threshold_w()
        if self.p_fast() > th or self.p_slow() > th:
            return False
        last_v = self._last_v
        return last_v is None or last_v >= self.VOLTAGE_FLOOR_V

    def converged_sec(self, timestamp: float) -> float:
        """已连续收敛多久（供日志/界面）。"""
        return (timestamp - self._hold_since) if self._hold_since else 0.0

    def should_end(self, timestamp: float, session_wh: float,
                   session_start) -> bool:
        """会话是否应当结束（自然结束）。**每次采样调用一次**（内部按调用间隔累计）。

        收敛状态要连续保持 HOLD_SEC；期间偶发波动累计不超过 GRACE_SEC 不清零
        （"1.5W 残留里偶尔跳一下"不该让计时从头来过），但出现 RECOVER_SEC 以上的
        连续回升、或非收敛累计超预算时立刻清零重数。
        """
        if session_wh < self.MIN_WH:
            self._clear_hold()
            return False
        if session_start and (timestamp - session_start) < self.MIN_SEC:
            self._clear_hold()
            return False
        dt = (timestamp - self._last_eval) if self._last_eval is not None else 0.0
        self._last_eval = timestamp
        if self.recovery_sec() >= self.RECOVER_SEC:
            self._clear_hold()          # 真回升：清零重数
            return False
        if self.converged():
            if self._hold_since is None:
                self._hold_since = timestamp
                self._grace = 0.0
        else:
            if self._hold_since is None:
                return False
            self._grace += max(0.0, dt)
            if self._grace > self.GRACE_SEC:
                self._clear_hold()
                return False
        if self._hold_since is None:
            return False
        return (timestamp - self._hold_since) >= self.HOLD_SEC

    def _clear_hold(self) -> None:
        self._hold_since = None
        self._grace = 0.0


class SessionStartGate:
    """会话开始门限：功率（随电压档位）+ 持续时间 + "刚结束"状态下的更高门槛。

    旧实现的三个坑（都会造成"残留功耗反复开新会话"）：
      1) 门限是电流 0.1A 且单帧判定：5V 档下 1.5W 残留 = 0.3A → 立刻开一轮新会话；
      2) 结束后只把门限抬高 60s（0.3A）→ 之后残留照样开会话；
      3) 抬高门限又会漏掉"小电流续充"。

    这里：
      · 基础门限 = max(START_MIN_W, START_MIN_A × 电压) —— 与旧规则等价（20V 下 2W、
        5V 下 0.5W），保证 5V/0.5W 的耳机这类低功耗设备能被正常识别、开得出会话；
        但**不再用固定功率地板**（上一版用 2.5W 地板，把 5V/0.5~1W 设备直接挡在门外）。
      · 必须连续保持 START_HOLD_SEC（30s）才算开始（去抖）。
      · 会话结束后的"刚结束"状态里，门限抬到 max(基础, max(0.4W, 2×T_low))，
        只有明显重新充电才算新一轮；该状态一直持续到"真的重新充电"或"端口空载
        （拔出）"为止——不用计时器，避免"10 分钟后残留又把会话开回来"。
    """

    START_MIN_W = 0.5            # 功率地板（5V 下 ≈0.1A）
    START_MIN_A = 0.1            # 电流当量：基础门限 = max(0.5W, 0.1A × 电压)
    START_HOLD_SEC = 30.0        # 达到门限需连续保持多久才开会话（去抖）
    RESTART_MIN_W = 0.4          # "刚结束"状态下的最低重开门限
    RESTART_RATIO = 2.0          # 重开门限 = max(0.4W, 2 × T_low(上次会话峰值))

    def __init__(self):
        self._post_session = False     # 上一次会话刚结束（残留保护中）
        self._restart_w = 0.0
        self._above_since = None
        self._last_run_start: Optional[float] = None   # 最近一次开会话那段负载的起点

    def note_session_end(self, peak_power: float = 0.0) -> None:
        """会话结束：进入"刚结束"状态并抬高重开门限。

        残留保护**没有时间衰减**：一直保留到真的重新充电（should_start 成立）
        或端口空载（note_no_load）——用计时器会在若干分钟后把残留误判成新会话。
        """
        self._post_session = True
        self._restart_w = max(self.RESTART_MIN_W,
                              self.RESTART_RATIO * tail_threshold_w(peak_power))
        self._above_since = None

    def note_no_load(self) -> None:
        """端口空载（拔出/设备彻底不吸电）：解除"刚结束"状态，回到基础门限。"""
        self._post_session = False
        self._above_since = None

    def base_threshold_w(self, voltage: float) -> float:
        return max(self.START_MIN_W, self.START_MIN_A * max(0.0, voltage or 0.0))

    def threshold_now(self, voltage: float) -> float:
        base = self.base_threshold_w(voltage)
        return max(base, self._restart_w) if self._post_session else base

    def should_start(self, timestamp: float, p_fast: float, voltage: float) -> bool:
        if p_fast < self.threshold_now(voltage):
            self._above_since = None
            return False
        if self._above_since is None:
            self._above_since = timestamp
        if timestamp - self._above_since < self.START_HOLD_SEC:
            return False
        # 回填边界：本段"连续达标负载"的起点。调用方（BLEManager）用它在开会话时把
        # 这段门控等待期的采样补进会话——必须在清掉 _above_since 之前存下来。
        self._last_run_start = self._above_since
        self._above_since = None
        self._post_session = False      # 真的重新开始充电：解除残留保护
        return True

    def last_run_start(self) -> Optional[float]:
        """最近一次开会话所依据的那段连续达标负载的起点（无则 None，供回填用）。"""
        return self._last_run_start
