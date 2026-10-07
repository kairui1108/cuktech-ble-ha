"""Tests for energy tracker."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from energy import (AdaptiveEnergyIntegrator, PortEnergyState,
                    ChargeSessionDetector, SessionStartGate, tail_threshold_w)
from energy import (limit_reached, normalize_charge_limit,
                    MAX_LIMIT_WH, LIMIT_MODES, DEFAULT_LIMIT_MODE)


def test_basic_accumulation():
    """20V * 1A for 30s = 0.167Wh."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 1.0, 0.0)
    integ.update(state, 20.0, 1.0, 30.0)
    expected = 20.0 * 30 / 3600
    assert abs(state.total_wh - expected) < 0.01, f"Expected ~{expected:.4f}Wh, got {state.total_wh}"
    print("PASS: test_basic_accumulation")


def test_zero_power():
    """Zero current = zero energy."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 0.0, 0.0)
    integ.update(state, 20.0, 0.0, 10.0)
    assert state.total_wh == 0.0, f"Expected 0Wh, got {state.total_wh}"
    print("PASS: test_zero_power")


def test_irregular_interval_skipped():
    """Gap > 30s should be skipped."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 1.0, 0.0)
    integ.update(state, 20.0, 1.0, 50.0)
    assert state.total_wh == 0.0, f"Expected 0Wh after skip, got {state.total_wh}"
    print("PASS: test_irregular_interval_skipped")


def test_overshoot_protection():
    """10x power spike should be capped."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 1.0, 0.0)
    integ.update(state, 200.0, 1.0, 1.0)
    assert state.total_wh < 200, f"Expected capped, got {state.total_wh}"
    print("PASS: test_overshoot_protection")


def test_multiple_accumulation():
    """10 points at 5s intervals = 45s total."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    for i in range(10):
        integ.update(state, 20.0, 1.0, i * 5.0)
    expected = 20.0 * 45 / 3600
    assert abs(state.total_wh - expected) < 0.01, f"Expected ~{expected:.4f}Wh, got {state.total_wh}"
    print("PASS: test_multiple_accumulation")


def _feed(det, t0, seconds, power_w, voltage=20.0, step=1.0):
    """按 step 秒喂一段恒定功率，返回下一采样时刻。"""
    t = t0
    while t <= t0 + seconds:
        det.feed(t, voltage, power_w / voltage if voltage else 0.0)
        t += step
    return t


def _time_to_end(det, t_start, *, power=0.3, voltage=20.0, step=1.0,
                 wh=20.0, session_start=0.0, max_sec=30 * 60):
    """从 t_start 起持续低功率，返回"多久后判定结束"（秒）；未结束返回 None。"""
    t = t_start
    limit = t_start + max_sec
    while t <= limit:
        det.feed(t, voltage, power / voltage if voltage else 0.0)
        if det.should_end(t, wh, session_start):
            return t - t_start
        t += step
    return None


def test_session_end_requires_convergence_and_hold():
    """充满尾巴：60W 充 5 分钟后落到 0.5W → 连续收敛 HOLD_SEC 才结束。"""
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    end = _time_to_end(det, t)
    assert end is not None, "0.3W 稳态（逼近 0）持续够久应当判出结束"
    # 下限是 HOLD_SEC；上限含"慢窗冲掉充电段"的滞后（SLOW_SEC）与判定步进
    assert ChargeSessionDetector.HOLD_SEC <= end <= ChargeSessionDetector.HOLD_SEC + 300, end
    print(f"PASS: test_session_end_requires_convergence_and_hold (涓流后 {end:.0f}s)")


def test_time_based_not_frame_based():
    """同一物理过程用 1s / 2s / 3s 采样，判定时刻应基本一致（旧实现是帧数，差 2 倍）。"""
    ends = []
    for step in (1.0, 2.0, 3.0):
        det = ChargeSessionDetector()
        t = _feed(det, 0.0, 300, 60.0, step=step)
        ends.append(_time_to_end(det, t, step=step))
    assert all(e is not None for e in ends), ends
    spread = max(ends) - min(ends)
    assert spread <= 120, f"采样率不该决定判定时刻：{ends}"
    print(f"PASS: test_time_based_not_frame_based ({[round(e) for e in ends]}s)")


def test_short_taper_is_not_enough():
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    t = _feed(det, t, 5 * 60, 0.3)
    assert not det.should_end(t, 20.0, 0.0), "只低了 5 分钟不算结束"
    print("PASS: test_short_taper_is_not_enough")


def test_occasional_fluctuation_is_tolerated():
    """偶发波动不该打断收敛计时：300s 窗口内每 60s 里 5s 跳到 1.5W。

    这正是用户描述的场景（充满后偶发 ~1.5W 波动）——中位数 + 波动预算要把它吸收。
    """
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    start = t
    end = None
    while t <= start + 20 * 60:
        spike = (int(t - start) % 60) < 5          # 5s/60s ≈ 8% 占空比
        det.feed(t, 20.0, (1.5 if spike else 0.1) / 20.0)
        if det.should_end(t, 20.0, 0.0):
            end = t - start
            break
        t += 1.0
    assert end is not None, "偶发尖峰不该阻止判满"
    assert end <= ChargeSessionDetector.HOLD_SEC + 400, end
    print(f"PASS: test_occasional_fluctuation_is_tolerated ({end:.0f}s)")


def test_sustained_recovery_resets_hold():
    """持续回升（真恢复充电）必须清零计时：8 分钟涓流后充回 40W 一分钟 → 重新数。"""
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    t = _feed(det, t, 8 * 60, 0.3)
    assert det.converged(), "8 分钟涓流应已收敛"
    t = _feed(det, t, 60, 40.0)          # 回升 60s > RECOVER_SEC
    assert not det.converged()
    recovery_end = t
    end = _time_to_end(det, t, max_sec=25 * 60)
    assert end is not None
    total_after_recovery = end + (recovery_end - t)
    assert total_after_recovery >= ChargeSessionDetector.HOLD_SEC, total_after_recovery
    print(f"PASS: test_sustained_recovery_resets_hold (回升后再等 {total_after_recovery:.0f}s)")


def test_mid_charge_plateau_is_not_converged():
    """中途低功率平台（60W 会话降到 8W 并保持 30 分钟）不算收敛。"""
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    t = _feed(det, t, 30 * 60, 8.0)
    assert not det.converged()
    assert not det.should_end(t, 40.0, 0.0)
    print("PASS: test_mid_charge_plateau_is_not_converged")


def test_voltage_collapse_blocks_convergence():
    """电压塌掉（掉线/拔出）不能算"低功率收敛"。"""
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    t = _feed(det, t, 12 * 60, 0.0, voltage=0.5)
    assert not det.converged()
    assert not det.should_end(t, 20.0, 0.0)
    print("PASS: test_voltage_collapse_blocks_convergence")


def test_min_energy_and_duration_gates():
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    t = _feed(det, t, 12 * 60, 0.3)
    assert not det.should_end(t, 0.02, 0.0), "能量不足不算结束"
    assert not det.should_end(t, 20.0, t - 60), "时长不足不算结束"
    print("PASS: test_min_energy_and_duration_gates")


def test_threshold_shape_self_calibrates():
    """T = max(0.05W, min(0.6W, 20%×充电峰值))：大功率按残留量级、小功率按比例。

    这张表就是"低功耗设备能否被正确识别"的核心：5V/0.5W 耳机阈值 0.1W、
    5V/1W 阈值 0.2W —— 它们充电时远高于阈值（不会判满），掉到涓流才结束。
    """
    assert tail_threshold_w(0.3) == 0.06
    assert tail_threshold_w(0.5) == 0.1
    assert tail_threshold_w(1.0) == 0.2
    assert tail_threshold_w(2.0) == 0.4
    assert tail_threshold_w(3.0) == 0.6      # 比例项刚好够到上限
    assert tail_threshold_w(5.0) == 0.6
    assert tail_threshold_w(20.0) == 0.6     # cap：判据是"功率逼近 0"，不是某个残留水平
    assert tail_threshold_w(60.0) == 0.6
    assert tail_threshold_w(0.0) == 0.05          # 噪声地板
    # 检测器内部用同一函数
    det = ChargeSessionDetector()
    _feed(det, 0.0, 120, 1.0)
    assert det.threshold_w() == 0.2, det.threshold_w()
    print("PASS: test_threshold_shape_self_calibrates")


def test_threshold_constants_are_the_module_ones():
    """阈值只能由模块常量决定（单一来源）。

    历史上类里另有一份同名的 TAIL_CAP_W/PEAK_RATIO/ABS_TAIL_MIN_W，从不参与计算
    ——"调阈值"改了没效果，排查"为什么不判满"时被它误导过。这里既钉住模块常量，
    也钉住"改模块常量真的会改变结果"。
    """
    import energy
    assert (energy.TAIL_MIN_W, energy.TAIL_CAP_W, energy.TAIL_PEAK_RATIO) == (0.05, 0.6, 0.20)
    assert not hasattr(ChargeSessionDetector, "TAIL_CAP_W"), "类里不得再有同名死常量"
    assert not hasattr(ChargeSessionDetector, "PEAK_RATIO")
    assert not hasattr(ChargeSessionDetector, "ABS_TAIL_MIN_W")
    old = energy.TAIL_CAP_W
    try:
        energy.TAIL_CAP_W = 0.9
        assert tail_threshold_w(60.0) == 0.9, "改模块常量必须真的生效"
    finally:
        energy.TAIL_CAP_W = old
    assert tail_threshold_w(60.0) == 0.6
    print("PASS: test_threshold_constants_are_the_module_ones")


def test_5v_trickle_at_one_current_step_is_judged_full():
    """5V 档 0.1A 的涓流必须判满（实机 C1 的 bug 现场）。

    充电器电流上报步进是 0.1A：5.1V × 0.1A = 0.51W。旧的 0.5W 上限（严格大于）
    把这个最典型的涓流判成"仍在充电"，收敛计时被波动预算反复清零 → 永不判满。
    """
    det = ChargeSessionDetector()
    _feed(det, 0.0, 60, 20.0, voltage=20.0)          # 峰值 20W → 阈值取上限
    assert det.threshold_w() == 0.6
    # 尾巴：5.1V/0.1A 稳定涓流（0.51W）
    _feed(det, 1.0, 200, 0.51, voltage=5.1)
    assert det.converged(), "5.1V/0.1A = 0.51W 应被视为接近 0（旧 0.5W 上限下判不了）"
    assert not det.converged() or det.p_fast() <= det.threshold_w()
    # 0.15A（0.77W）仍算在充电
    det2 = ChargeSessionDetector()
    _feed(det2, 0.0, 60, 20.0, voltage=20.0)
    _feed(det2, 1.0, 200, 0.77, voltage=5.1)
    assert not det2.converged(), "0.77W 不该被判满"
    print("PASS: test_5v_trickle_at_one_current_step_is_judged_full")


def test_5v_alternating_trickle_reaches_full():
    """实机 C1 的尾巴模式（0V/0.1A 交替）必须能走到判满。

    回放真实采样：旧的 0.5W 上限下，交替模式只有 ~36% 样本收敛 → 600s 保持期内
    grace 累积 ~0.6s/s、三分钟就耗尽 120s 预算 → 计时被清零 17 次，3 小时判不满。
    """
    det = ChargeSessionDetector()
    _feed(det, 0.0, 120, 20.0, voltage=9.0)          # 充电段（峰值 20W）
    t = 120.0
    ended = None
    for k in range(1200):                            # 交替 0/0.1A，最多 20 分钟
        i = 0.1 if k % 2 == 0 else 0.0
        t += 1.0
        det.feed(t, 5.1, i)
        if det.should_end(t, 5.0, 0.0):
            ended = t - 120.0
            break
    assert ended is not None, "0/0.1A 交替（5.1V）必须能判满"
    assert 600 <= ended <= 700, f"应在 HOLD_SEC 后不久判满，实际 {ended}s"
    print(f"PASS: test_5v_alternating_trickle_reaches_full (涓流后 {ended:.0f}s)")


def test_low_power_device_end_to_end():
    """""5V/1W 蓝牙耳机：充电过程中不判满；掉到 0.05W 后连续收敛 10 分钟才结束。"""""
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 20 * 60, 1.0, voltage=5.0)          # 1W 充 20 分钟
    assert not det.converged(), "1W 明显高于阈值 0.2W，不能判满"
    assert not det.should_end(t, 0.3, 0.0)
    end = _time_to_end(det, t, power=0.05, voltage=5.0, wh=0.3)   # 掉到 0.05W
    assert end is not None and end >= ChargeSessionDetector.HOLD_SEC, end
    print(f"PASS: test_low_power_device_end_to_end (涓流后 {end:.0f}s)")


def test_half_watt_device_recognized():
    """5V/0.5W（0.1A）耳机：阈值 0.1W，同样不会被误判成"已满"。"""
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 15 * 60, 0.5, voltage=5.0)
    assert det.threshold_w() == 0.1
    assert not det.converged(), "0.5W 仍在充电（阈值 0.1W）"
    end = _time_to_end(det, t, power=0.03, voltage=5.0, wh=0.2)
    assert end is not None, end
    print(f"PASS: test_half_watt_device_recognized (涓流后 {end:.0f}s)")


def test_reset_clears_windows():
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    end = _time_to_end(det, t)
    assert end is not None
    det.reset()
    assert det.peak_power() == 0.0 and not det.converged()
    # reset 之后必须重新积累，不能沿用上一轮的收敛计时
    assert not det.should_end(t + end + 10, 20.0, 0.0)
    print("PASS: test_reset_clears_windows")


def test_start_gate_requires_sustained_power():
    """基础门限 = max(0.5W, 0.1A×电压)，且要连续保持 30s（去抖）。"""
    gate = SessionStartGate()
    assert gate.base_threshold_w(5.0) == 0.5      # 5V：0.1A
    assert gate.base_threshold_w(20.0) == 2.0     # 20V：0.1A（与旧规则等价）
    assert not gate.should_start(0.0, 12.0, 20.0), "首帧不开始"
    assert not gate.should_start(10.0, 12.0, 20.0), "10s 不够 START_HOLD_SEC"
    assert gate.should_start(31.0, 12.0, 20.0), "连续 30s 后开始"
    gate2 = SessionStartGate()
    gate2.should_start(0.0, 12.0, 20.0)
    assert not gate2.should_start(1.0, 1.0, 20.0), "掉回门限以下要重新计时"
    print("PASS: test_start_gate_requires_sustained_power")


def test_start_gate_exposes_run_start_for_backfill():
    """门控要把"本段连续达标负载的起点"留给调用方——预会话回填的边界就靠它。

    这一步很容易漏：should_start() 原来在返回 True 前直接把 _above_since 清成 None，
    调用方拿不到起点就只能回填到"放行时刻"，等于没回填。
    """
    gate = SessionStartGate()
    assert gate.last_run_start() is None, "还没放行过，不该有起点"

    assert not gate.should_start(1000.0, 1.0, 5.0)     # 首帧：开始计时
    assert not gate.should_start(1020.0, 1.0, 5.0)
    assert gate.last_run_start() is None, "未放行前不得暴露起点"
    assert gate.should_start(1030.0, 1.0, 5.0) is True
    assert gate.last_run_start() == 1000.0, gate.last_run_start()

    # 中途掉到门限以下 → 重新计时，放行后起点跟着更新
    assert not gate.should_start(1100.0, 0.1, 5.0)
    assert not gate.should_start(1200.0, 1.0, 5.0)
    assert gate.should_start(1231.0, 1.0, 5.0) is True
    assert gate.last_run_start() == 1200.0, gate.last_run_start()
    print("PASS: test_start_gate_exposes_run_start_for_backfill")


def test_low_power_devices_can_start_sessions():
    """5V 耳机（0.5W / 1W）必须能开会话；20V 下 1.5W 的"插着不充"不算。"""
    for watts in (0.5, 1.0):
        gate = SessionStartGate()
        assert not gate.should_start(0.0, watts, 5.0)
        assert gate.should_start(31.0, watts, 5.0), f"5V/{watts}W 应当能开始会话"
    idle = SessionStartGate()
    idle.should_start(0.0, 1.5, 20.0)
    assert not idle.should_start(60.0, 1.5, 20.0), "20V/1.5W（0.075A）不该开会话"
    print("PASS: test_low_power_devices_can_start_sessions")


def test_post_session_blocks_residual_until_unplug():
    """会话刚结束后：残留（1.5W）无论多久都不重开；拔插或明显重新充电才解除。"""
    gate = SessionStartGate()
    gate.note_session_end(peak_power=60.0)
    assert gate.threshold_now(20.0) == 2.0        # max(基础 2.0W, 2×T_low 1.0W)
    for t in range(0, 3 * 3600, 60):              # 持续 3 小时
        assert not gate.should_start(t, 1.5, 20.0), "残留功耗不得重开会话"
    # 拔插（空载）后回到基础门限：小设备也能重新开始
    gate.note_no_load()
    assert gate.threshold_now(5.0) == 0.5
    gate.should_start(3 * 3600, 1.0, 5.0)                    # 起计时
    assert gate.should_start(3 * 3600 + 31, 1.0, 5.0), "拔出后插入的小设备应能开始"
    print("PASS: test_post_session_blocks_residual_until_unplug")


def test_post_session_restart_needs_clear_power():
    gate = SessionStartGate()
    gate.note_session_end(peak_power=60.0)
    assert not gate.should_start(10.0, 1.5, 20.0), "1.5W 低于重开门限 2W"
    gate.should_start(20.0, 8.0, 20.0)                       # 起计时
    assert gate.should_start(51.0, 8.0, 20.0), "8W 持续 30s 算重新开始充电"
    assert gate.threshold_now(20.0) == 2.0, "开始后解除残留保护"
    print("PASS: test_post_session_restart_needs_clear_power")


def test_post_session_threshold_scales_with_low_power_device():
    """小设备（峰值 1W）结束后：重开门限 0.4W —— 残留挡住、续充放行。"""
    gate = SessionStartGate()
    gate.note_session_end(peak_power=1.0)
    assert gate.threshold_now(5.0) == 0.5         # max(0.5, max(0.4, 2×0.2))
    gate2 = SessionStartGate()
    gate2.note_session_end(peak_power=1.0)
    assert not gate2.should_start(600.0, 0.05, 5.0), "耳机充满后的残留不得开新会话"
    gate2.should_start(700.0, 1.0, 5.0)                      # 起计时
    assert gate2.should_start(731.0, 1.0, 5.0), "耳机重新开始充电应能开会话"
    print("PASS: test_post_session_threshold_scales_with_low_power_device")


def test_jitter_does_not_break_low_power_verdict():
    """波动越界一点也能靠波动预算吸收：每 60s 里 15s 超过阈值（1.5W vs T=1W）。"""
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    start = t
    end = None
    while t <= start + 25 * 60:
        spike = (int(t - start) % 60) < 10      # 10s/60s ≈ 17% 占空比（预算 20% 内）
        det.feed(t, 20.0, (1.5 if spike else 0.05) / 20.0)
        if det.should_end(t, 20.0, 0.0):
            end = t - start
            break
        t += 1.0
    assert end is not None, "占空比 17% 的 1.5W 波动仍应判出结束（波动预算 20% 内）"
    assert end <= ChargeSessionDetector.HOLD_SEC + 600, end
    print(f"PASS: test_jitter_does_not_break_low_power_verdict ({end:.0f}s)")


def test_real_recovery_still_resets_with_jitter_floor():
    """但"真的又开始充"必须清零：持续超过恢复门槛(5W)45 秒以上。"""
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    t = _feed(det, t, 20 * 60, 0.2)             # 长时间低功率（已收敛）
    assert det.converged()
    t = _feed(det, t, 60, 12.0)                 # 12W 持续一分钟 = 真的又开始充
    assert not det.converged()
    assert det.recovery_sec() >= ChargeSessionDetector.RECOVER_SEC
    print("PASS: test_real_recovery_still_resets_with_jitter_floor")


def test_screen_level_fluctuation_is_not_recovery():
    """几瓦的短促波动（屏幕亮一下）不算"充电恢复"：不超恢复门槛/不够 45s。"""
    det = ChargeSessionDetector()
    t = _feed(det, 0.0, 300, 60.0)
    t = _feed(det, t, 20 * 60, 0.2)
    det.feed(t + 1, 20.0, 4.0 / 20.0)           # 4W 一闪（低于 5W 门槛）
    det.feed(t + 20, 20.0, 8.0 / 20.0)          # 8W 但只持续 20s（<45s）
    det.feed(t + 21, 20.0, 0.2 / 20.0)
    assert det.recovery_sec() < ChargeSessionDetector.RECOVER_SEC
    print("PASS: test_screen_level_fluctuation_is_not_recovery")
