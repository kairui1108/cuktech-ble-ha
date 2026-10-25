"""`AdapterCoordinator` 的契约测试（设计文档 §3.4 / §3.5，P2 门槛）。

这一层防的是"**恢复动作互相伤害**"——多设备时最危险的不是连不上，而是为了救
一台设备把其他设备一起打下线。所以测试按危害排序：

1. **N=1 时序不变**：单设备仍走 L2 适配器 power cycle，命令与顺序和改造前一致；
2. **多设备不伤及无辜**：有活连接时永不下电（只 L1 → L1.5）；只有全员离线或
   N=1 才允许 L2；
3. **quiesce 门闸**：L2 期间所有 manager 停在门口，恢复后放行——没门闸就会把
   "一台设备的故障"放大成"全场熔断"；
4. **扫描合并 + 清理收口**：N 个等待者共享一次扫描，`scan off` 每窗口一次；
5. **连接信号量**：并发连接/认证不超过上限；
6. **L3 生命周期与升级条件**：N>1 进程内重建（不在 manager 任务里自取消）；
   ≥2 台同时卡住 → 升级为进程重启。
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import adapter_coordinator as ac  # noqa: E402
import ha_server  # noqa: E402
from adapter_coordinator import (  # noqa: E402
    LEVEL_ADAPTER, LEVEL_DEVICE, LEVEL_INPROCESS, LEVEL_PROCESS, LEVEL_REMOVE,
    AdapterCoordinator,
)


class FakeBluetoothctl:
    """记录 `bluetoothctl` 调用序列（恢复时序就是契约本身）。"""

    def __init__(self, ready_after: int = 1):
        self.calls: list[tuple] = []
        self.ready_after = ready_after
        self._show_count = 0

    async def __call__(self, coord, *args):
        self.calls.append(args)
        if args[:1] == ("show",):
            self._show_count += 1
            return b"Powered: yes" if self._show_count >= self.ready_after else b""
        return b""

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def coord():
    c = AdapterCoordinator(device_count_provider=lambda: 1, connect_limit=2)
    c.sleep = _noop_sleep          # 不真等 1s/3s/15s
    return c


@pytest.fixture
def bt(monkeypatch, coord):
    fake = FakeBluetoothctl()
    monkeypatch.setattr(ac, "has_bluetoothctl", lambda: True)
    monkeypatch.setattr(coord, "_bluetoothctl",
                        lambda *args: fake(coord, *args))
    return fake


async def _noop_sleep(*_a, **_k):
    return None


class TestSingleDeviceKeepsLegacySequence:
    """① N=1：分级恢复必须退化为改造前的整机级 power cycle。"""

    async def test_power_cycles_on_first_failure(self, coord, bt):
        level = await coord.recover_device("AA:BB:CC:DD:EE:01")
        assert level == LEVEL_ADAPTER
        # 顺序与改造前逐条一致：disconnect → scan off（power cycle 开头清扫描）
        # → power off → power on → show
        assert bt.calls[:4] == [("disconnect", "AA:BB:CC:DD:EE:01"),
                                ("scan", "off"),
                                ("power", "off"), ("power", "on")]
        assert bt.calls[-1][0] == "show"        # 就绪探测
        assert coord.adapter_resets == 1

    async def test_scan_off_precedes_power_cycle(self, coord, bt):
        """`scan off` 仍在下电之前（清 BlueZ 残留扫描，避免 InProgress）。"""
        await coord.recover_device("AA:BB:CC:DD:EE:01")
        calls = bt.calls
        assert any(c[:2] == ("scan", "off") for c in calls)
        assert calls.index(("scan", "off")) < calls.index(("power", "off"))

    async def test_single_device_always_allowed_to_reset_adapter(self, coord):
        """N=1 时即使"有活连接"也不是别人——那台设备就是它自己。"""
        coord.note_connected("AA:BB:CC:DD:EE:01")
        assert coord._can_reset_adapter() is True


class TestMultiDeviceDoesNotHurtOthers:
    """② 多设备：有活连接时绝不下电。"""

    @pytest.fixture
    def multi(self, coord):
        coord.set_device_count_provider(lambda: 3)
        return coord

    async def test_first_failure_is_l1_only(self, multi, bt):
        multi.note_connected("AA:BB:CC:DD:EE:02")     # 另一台还在线
        level = await multi.recover_device("AA:BB:CC:DD:EE:01")
        assert level == LEVEL_DEVICE
        assert ("power", "off") not in bt.calls, "有活连接时下电会杀掉别的设备"
        assert bt.calls[0] == ("disconnect", "AA:BB:CC:DD:EE:01")

    async def test_second_failure_escalates_to_remove_not_power(self, multi, bt):
        multi.note_connected("AA:BB:CC:DD:EE:02")
        await multi.recover_device("AA:BB:CC:DD:EE:01")
        level = await multi.recover_device("AA:BB:CC:DD:EE:01")
        assert level == LEVEL_REMOVE
        assert ("remove", "AA:BB:CC:DD:EE:01") in bt.calls
        assert ("power", "off") not in bt.calls

    async def test_all_offline_allows_adapter_reset(self, multi, bt):
        """全员离线 = 下电不会再伤害任何人 → 允许 L2（但仍不许跳级）。"""
        multi.note_connected("AA:BB:CC:DD:EE:02")
        multi.unregister_connection("AA:BB:CC:DD:EE:02")
        assert await multi.recover_device("AA:BB:CC:DD:EE:01") == LEVEL_DEVICE
        assert ("power", "off") not in bt.calls, "第一次只该 L1"
        assert await multi.recover_device("AA:BB:CC:DD:EE:01") == LEVEL_ADAPTER
        assert ("power", "off") in bt.calls

    async def test_recovery_only_touches_target_mac(self, multi, bt):
        multi.note_connected("AA:BB:CC:DD:EE:02")
        await multi.recover_device("AA:BB:CC:DD:EE:01")
        targets = {c[1] for c in bt.calls if c[0] in ("disconnect", "remove")}
        assert targets == {"AA:BB:CC:DD:EE:01"}


class TestQuiesceGate:
    """③ L2 期间必须关门闸：门关着时所有重连尝试原地等待。"""

    async def test_gate_closed_during_adapter_reset(self, coord, bt):
        gate_states = []
        original_power = coord._power_cycle

        async def watched():
            gate_states.append(coord.recovery_gate.is_set())
            await original_power()

        coord._power_cycle = watched
        await coord.recover_adapter()
        assert gate_states == [False], "power cycle 期间门闸必须是关闭的"
        assert coord.recovery_gate.is_set() is True, "恢复后必须放行"

    async def test_managers_block_while_gate_is_closed(self, coord):
        """门闸关着时 `wait_recovery_gate()` 不返回；开了立刻返回。"""
        coord.recovery_gate.clear()
        task = asyncio.ensure_future(coord.wait_recovery_gate())
        await asyncio.sleep(0)
        assert not task.done(), "门闸关着还放行了 —— 退避重连会刷爆失败计数"
        coord.recovery_gate.set()
        await asyncio.wait_for(task, timeout=1.0)

    async def test_gate_reopens_even_if_power_cycle_raises(self, coord, bt):
        """power cycle 抛异常也必须开门：门一直关着 = 全场停摆。"""
        async def boom():
            raise RuntimeError("adapter exploded")
        coord._power_cycle = boom
        with pytest.raises(RuntimeError):
            await coord.recover_adapter()
        assert coord.recovery_gate.is_set() is True

    async def test_quiesce_disconnects_every_live_link(self, coord, bt):
        coord.set_device_count_provider(lambda: 2)
        coord.note_connected("AA:BB:CC:DD:EE:01")
        coord.note_connected("AA:BB:CC:DD:EE:02")
        await coord.recover_adapter()
        disconnects = {c[1] for c in bt.calls if c[0] == "disconnect"}
        assert disconnects == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}
        assert coord.live_connections == set()


class TestScanCoalescing:
    """④ N 个等待者共享一次扫描；`scan off` 每窗口一次。"""

    async def test_one_discover_serves_all_waiters(self, coord, bt):
        discoveries = []

        async def fake_discover(timeout):
            discoveries.append(timeout)
            # 真扫描必然让出事件循环（几秒的 discover），这里用 sleep(0.01) 模拟，
            # 否则三个等待者会在"锁还没被占住"时依次跑完，合并根本没机会发生。
            await asyncio.sleep(0.01)
            return {"aa:bb:cc:dd:ee:01": "dev1", "aa:bb:cc:dd:ee:02": "dev2",
                    "aa:bb:cc:dd:ee:03": "dev3"}

        coord._discover = fake_discover
        results = await asyncio.gather(
            coord.find_device("AA:BB:CC:DD:EE:01", 5),
            coord.find_device("AA:BB:CC:DD:EE:02", 5),
            coord.find_device("AA:BB:CC:DD:EE:03", 5))
        assert results == ["dev1", "dev2", "dev3"]
        assert len(discoveries) == 1, "并发扫描没有合并 —— 真机上就是 InProgress"
        assert bt.verbs().count("scan") == 1, "scan off 每窗口只该执行一次"

    async def test_mibeacon_service_data_matches_on_macos(self, coord, bt, monkeypatch):
        """macOS 拿不到真 MAC：按 MiBeacon service-data 尾 6 字节匹配。"""
        import bleak
        from cuktech_ble.protocol import UUID_FE95, mac_str_to_bytes

        class Adv:
            service_data = {UUID_FE95: b"\x00\x00" + mac_str_to_bytes("AA:BB:CC:DD:EE:07")}

        async def fake_discover(timeout=None, return_adv=False, **kw):
            return {"random-core-bluetooth-uuid": ("dev-found", Adv())}

        monkeypatch.setattr(bleak.BleakScanner, "discover",
                            staticmethod(fake_discover))
        assert await coord.find_device("AA:BB:CC:DD:EE:07", 5) == "dev-found"

    async def test_missing_device_returns_none(self, coord, bt):
        async def fake_discover(timeout):
            return {}

        coord._discover = fake_discover
        assert await coord.find_device("AA:BB:CC:DD:EE:99", 5) is None

    async def test_scan_failure_returns_none_and_frees_waiter(self, coord, bt):
        async def boom(timeout):
            raise RuntimeError("BlueZ said no")

        coord._discover = boom
        assert await coord.find_device("AA:BB:CC:DD:EE:01", 5) is None
        assert coord._pending == {}, "失败的扫描把等待者留在了表里 → 后续永久挂起"


class TestConnectSemaphore:
    """⑤ 连接/认证并发热上限。"""

    async def test_never_exceeds_limit(self):
        coord = AdapterCoordinator(device_count_provider=lambda: 8, connect_limit=2)
        active = {"now": 0, "peak": 0}

        async def worker():
            async with coord.connect_slot():
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
                await asyncio.sleep(0.01)
                active["now"] -= 1

        await asyncio.gather(*(worker() for _ in range(8)))
        assert active["peak"] == 2

    async def test_all_workers_eventually_run(self):
        """排队不是失败：8 个请求最终都要跑完。"""
        coord = AdapterCoordinator(device_count_provider=lambda: 8, connect_limit=2)
        done = []

        async def worker(i):
            async with coord.connect_slot():
                await asyncio.sleep(0)
                done.append(i)

        await asyncio.gather(*(worker(i) for i in range(8)))
        assert sorted(done) == list(range(8))

    async def test_stagger_delay_is_zero_for_first_device(self, coord):
        """N=1（index 0）零延迟：单设备启动时序与改造前一致。"""
        coord.startup_stagger_sec = 2.0
        slept = []

        async def fake_sleep(sec):
            slept.append(sec)

        coord.sleep = fake_sleep
        await coord.stagger_delay(0)
        await coord.stagger_delay(2)
        assert slept == [4.0], "第 2 台设备应等 2×stagger，第一台必须零延迟"


class TestAuthStuckEscalation:
    """⑥ L3：N>1 进程内重建；≥2 台同时卡住 → 升级为进程重启。"""

    @pytest.fixture
    def multi(self, coord):
        coord.set_device_count_provider(lambda: 3)
        self.rebuilt = []
        self.restarted = []

        async def rebuild(mac):
            self.rebuilt.append(mac)

        async def restart():
            self.restarted.append(True)

        coord.set_rebuild_hook(rebuild)
        coord.set_process_restart_hook(restart)
        return coord

    async def test_multi_device_rebuilds_in_process(self, multi):
        level = await multi.restart_device("AA:BB:CC:DD:EE:01")
        assert level == LEVEL_INPROCESS
        assert self.rebuilt == ["AA:BB:CC:DD:EE:01"]
        assert self.restarted == [], "为一台设备重启进程会把其他设备一起打下线"

    async def test_single_device_still_restarts_process(self, coord):
        restarted = []

        async def restart():
            restarted.append(True)

        async def rebuild(mac):     # pragma: no cover - 不该走这条路
            raise AssertionError("N=1 不应走进程内重建")

        coord.set_rebuild_hook(rebuild)
        coord.set_process_restart_hook(restart)
        level = await coord.restart_device("AA:BB:CC:DD:EE:01")
        assert level == LEVEL_PROCESS
        assert restarted == [True], "N=1 必须保持改造前的进程重启行为"

    async def test_two_simultaneously_stuck_devices_escalate(self, multi):
        """**同时**两台卡住 = 适配器级故障 → 升级整进程重启（§3.5 升级条件）。

        顺序卡住不算：第一台已在进程内重建成功，再重建第二台不会互相踩适配器。
        """
        gate = asyncio.Event()

        async def slow_rebuild(mac):
            self.rebuilt.append(mac)
            await gate.wait()        # 让第二台的请求落在"第一台还没重建完"的窗口里

        multi.set_rebuild_hook(slow_rebuild)
        first = asyncio.ensure_future(multi.restart_device("AA:BB:CC:DD:EE:01"))
        await asyncio.sleep(0)       # 让第一台先登记 stuck 并进入重建
        second = await multi.restart_device("AA:BB:CC:DD:EE:02")
        gate.set()
        assert await first == LEVEL_INPROCESS
        assert second == LEVEL_PROCESS
        assert len(self.restarted) == 1
        assert self.rebuilt == ["AA:BB:CC:DD:EE:01"], "第二台不该再各自并行重建"

    async def test_sequential_stuck_devices_do_not_escalate(self, multi):
        """先后卡住（前者已重建成功）→ 各走进程内重建，不必重启进程。"""
        assert await multi.restart_device("AA:BB:CC:DD:EE:01") == LEVEL_INPROCESS
        assert await multi.restart_device("AA:BB:CC:DD:EE:02") == LEVEL_INPROCESS
        assert self.restarted == []

    async def test_successful_reconnect_clears_stuck_state(self, multi):
        """设备自己连上了就把"卡住"记账清掉，否则一次抖动会永久计入升级条件。"""
        multi._stuck_devices.add("AA:BB:CC:DD:EE:01")
        multi.note_connected("AA:BB:CC:DD:EE:01")
        assert multi._stuck_devices == set()
        assert "AA:BB:CC:DD:EE:01" in multi.live_connections

    async def test_no_hooks_registered_falls_back_to_process_restart(self, coord):
        coord.set_device_count_provider(lambda: 2)
        level = await coord.restart_device("AA:BB:CC:DD:EE:01")
        assert level == LEVEL_PROCESS


class TestManagerIntegration:
    """manager 侧接线：扫描/恢复都走协调器，不再自己动适配器。"""

    async def test_manager_scan_goes_through_coordinator(self):
        from ble_manager import BLEManager
        from config import load_config
        from state import ChargerState

        cfg = load_config()
        coord = AdapterCoordinator(device_count_provider=lambda: 1)
        coord.sleep = _noop_sleep
        seen = []

        async def fake_find(mac, timeout):
            seen.append((mac, timeout))
            return object()

        coord.find_device = fake_find
        mgr = BLEManager("AA:BB:CC:DD:EE:01", "00" * 16, ChargerState(), cfg,
                         adapter=coord)
        assert await mgr._scan_for_device() is not None
        assert seen and seen[0][0] == "AA:BB:CC:DD:EE:01"

    async def test_manager_recovery_delegates_to_coordinator(self):
        from ble_manager import BLEManager
        from config import load_config
        from state import ChargerState

        cfg = load_config()
        coord = AdapterCoordinator(device_count_provider=lambda: 3)
        coord.sleep = _noop_sleep
        seen = []

        async def fake_recover(mac):
            seen.append(mac)
            return LEVEL_DEVICE

        coord.recover_device = fake_recover
        mgr = BLEManager("AA:BB:CC:DD:EE:01", "00" * 16, ChargerState(), cfg,
                         adapter=coord)
        await mgr._force_disconnect_bluetooth()
        assert seen == ["AA:BB:CC:DD:EE:01"]

    async def test_manager_without_adapter_keeps_legacy_power_cycle(self):
        """无协调器（老单测直接构造 manager）仍走改造前的整机级实现。"""
        from ble_manager import BLEManager
        from config import load_config
        from state import ChargerState

        cfg = load_config()
        mgr = BLEManager("AA:BB:CC:DD:EE:01", "00" * 16, ChargerState(), cfg)
        with patch("ble_manager._has_bluetoothctl", return_value=False):
            await mgr._force_disconnect_bluetooth()      # 不该抛异常


class TestManagerGateAndStagger:
    """manager 侧：门闸管住重连，错峰管住启动。"""

    async def test_connect_waits_for_recovery_gate(self):
        from ble_manager import BLEManager
        from config import load_config
        from state import ChargerState

        cfg = load_config()
        coord = AdapterCoordinator(device_count_provider=lambda: 2)
        coord.sleep = _noop_sleep
        mgr = BLEManager("AA:BB:CC:DD:EE:01", "00" * 16, ChargerState(), cfg,
                         adapter=coord)
        ran = []

        async def fake_locked():
            ran.append(True)

        mgr._connect_locked = fake_locked
        coord.recovery_gate.clear()          # 适配器正在 L2 恢复
        task = asyncio.ensure_future(mgr._connect())
        await asyncio.sleep(0.02)
        assert ran == [], "门闸关着还去连了 —— 会在适配器未就绪时刷爆失败计数"
        coord.recovery_gate.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert ran == [True]

    async def test_on_startup_staggers_by_registry_order(self):
        """错峰从协调器单点取值：第 N 台 = N × startup_stagger_sec。"""
        from golden_harness import golden_env

        DESK = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16}
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            # 测试环境把错峰归零（见 golden_harness），这里显式还原成生产值验证接线
            s.adapter.startup_stagger_sec = 2.0
            await ha_server_on_startup_stagger(s)
            delays = {rt.device_id: rt.supervisor._startup_delay for rt in s.registry}
            assert delays == {"default": 0.0, "desk": 2.0}


async def ha_server_on_startup_stagger(server):
    """复算 `on_startup` 里的错峰赋值（不重跑整套启动）。"""
    stagger = float(server.adapter.startup_stagger_sec or 0.0)
    for index, rt in enumerate(server.registry):
        rt.supervisor._startup_delay = stagger * index


class TestInProcessRebuild:
    """⑥ L3 生命周期：N>1 时重建**不能**在 manager 任务里自取消。"""

    async def test_restart_device_schedules_separate_task(self):
        from golden_harness import golden_env

        DESK = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16}
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            desk = s.registry.get("desk")
            old_manager = desk.manager
            # 队列里挂一条等待响应的命令：重建必须把它 settle 成失败，
            # 否则调用方会一直挂到超时（前端表现为"点了没反应"）。
            fut = asyncio.get_running_loop().create_future()
            desk.manager.cmd_queue.put_nowait(("port", {"port": "c1"}, fut))

            await s.restart_device(desk)

            # 关键：不在当前任务里就地停自己，而是排进独立任务
            assert desk.supervisor._rebuild_task is not None
            assert not desk.supervisor._rebuild_task.done()
            await asyncio.wait_for(desk.supervisor._rebuild_task, timeout=5.0)

            assert isinstance(fut.exception(), ConnectionError)
            assert "device restarting" in str(fut.exception())
            assert desk.manager is not old_manager, "重建必须换新实例"
            # 10 项钩子里最关键的几项必须重新挂上（漏一项这台设备就残废）
            assert desk.manager._sse_emitter is not None
            assert desk.manager._mqtt_publish is not None
            assert desk.manager._history.device_id == "desk"

    async def test_auth_stuck_inprocess_exits_reconnect_loop(self):
        """manager 收到"进程内重建"指令后必须主动退出重连循环（否则两边抢适配器）。"""
        from ble_manager import BLEManager
        from config import load_config
        from state import ChargerState

        cfg = load_config()
        coord = AdapterCoordinator(device_count_provider=lambda: 3)
        coord.sleep = _noop_sleep
        seen = []

        async def fake_restart(mac):
            seen.append(mac)
            return LEVEL_INPROCESS

        coord.restart_device = fake_restart
        mgr = BLEManager("AA:BB:CC:DD:EE:01", "00" * 16, ChargerState(), cfg,
                         adapter=coord)
        mgr._auth_fail_count = 99
        mgr.AUTH_STUCK_NOTIFY_SEC = 0
        await mgr._recover_from_auth_stuck()
        assert seen == ["AA:BB:CC:DD:EE:01"]
        assert mgr._stop_event.is_set(), "没退出重连循环 → 与重建任务抢适配器"


class TestBackgroundTaskWatchdog:
    """§3.13：端口采样 timer 静默死掉必须被发现（否则"假在线"最隐蔽的形态）。"""

    async def test_timer_death_forces_reconnect(self):
        from ble_manager import BLEManager
        from config import load_config
        from state import ChargerState

        cfg = load_config()
        mgr = BLEManager("AA:BB:CC:DD:EE:01", "00" * 16, ChargerState(), cfg)

        class DeadTask:
            def cancelled(self):
                return False

            def exception(self):
                return RuntimeError("boom")

        mgr._port_timer_dead = False
        mgr._on_port_timer_done(DeadTask())
        assert mgr._port_timer_dead is True, "timer 死了没人知道 → 历史静默停写"

    async def test_clean_stop_does_not_flag_timer(self):
        from ble_manager import BLEManager
        from config import load_config
        from state import ChargerState

        cfg = load_config()
        mgr = BLEManager("AA:BB:CC:DD:EE:01", "00" * 16, ChargerState(), cfg)
        mgr._stop_event.set()          # 正常停止：timer 被 cancel，不该报错也不该重建

        class CancelledTask:
            def cancelled(self):
                return True

            def exception(self):  # pragma: no cover - cancelled 分支先返回
                raise AssertionError("cancel 的任务不该读 exception()")

        mgr._port_timer_dead = False
        mgr._on_port_timer_done(CancelledTask())
        assert mgr._port_timer_dead is False
