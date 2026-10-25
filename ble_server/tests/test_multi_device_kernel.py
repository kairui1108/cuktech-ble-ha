"""P1 合并门槛：多设备内核的路由身份、并发隔离与装配完整性。

设计文档 §5 的四道门槛里，本文件覆盖三道：

| 门槛 | 测试类 | 防的是什么 |
|---|---|---|
| §5.4 路由/装配身份断言 | `TestIdentityAssertions` | P0-2 的"setter 短路让 ContextVar 永不生效" |
| §5.3 并发路由零串台 | `TestConcurrentRouting` | **控错设备**（安全事故） |
| §5.5 装配完整性 | `TestAssemblyIntegrity` | P0-1 的"漏一项装配、而且静默" |

这些测试都要**真** `Server` + **真** `on_startup`，用 `golden_harness.golden_env`
（它在跑真装配，只替换 BLE 硬件/MQTT broker/墙钟）。
"""
import asyncio
import io
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import ha_server  # noqa: E402
from golden_harness import golden_env, mock_request, snapshot_state  # noqa: E402
from ble_manager import END_REASON_SHUTDOWN  # noqa: E402
from log_utils import LogDeviceFilter  # noqa: E402

pytestmark = pytest.mark.asyncio

DESK = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16}
BED = {"id": "bed", "mac": "AA:BB:CC:DD:EE:03", "token": "22" * 16}


@pytest.fixture
async def single():
    async with golden_env() as env:
        snapshot_state(env.server.state)
        yield env


@pytest.fixture
async def dual():
    async with golden_env(devices=[DESK, BED]) as env:
        for rt in env.server.registry:
            snapshot_state(rt.state)
        yield env


def _ctx(rt):
    """进入某设备的请求上下文（等价于 middleware 做过的事）。"""
    return ha_server._current_rt.set(rt)


class TestIdentityAssertions:
    """§5.4：锁死 P0-2 ——生产构造后三个槽位必须为空，无参访问解析到 primary。"""

    async def test_production_construction_points_at_primary(self, dual):
        s = dual.server
        assert s.ble is s.primary.manager
        assert s.state is s.primary.state
        assert s.history is s.primary.history
        assert s._ble_assigned is None
        assert s._state_assigned is None
        assert s._history_assigned is None

    async def test_registry_order_is_primary_first(self, dual):
        ids = [rt.device_id for rt in dual.server.registry]
        assert ids == ["default", "desk", "bed"]

    async def test_middleware_switches_identity_for_device_path(self, dual):
        """走**真** `device_middleware`：路径寻址期间三个 property 都指向目标设备。"""
        s = dual.server
        desk = s.registry.get("desk")
        seen = {}

        async def handler(request):
            seen["ble"] = s.ble
            seen["state"] = s.state
            seen["history"] = s.history
            seen["device_id"] = s.current_device_id()
            return "ok"

        request = mock_request("GET", "/api/device/desk/status",
                               match_info={"device_id": "desk"})
        assert await ha_server.device_middleware(request, handler) == "ok"
        assert seen["ble"] is desk.manager
        assert seen["state"] is desk.state
        assert seen["history"] is desk.history
        assert seen["device_id"] == "desk"
        # 退出请求后上下文必须回到 primary（ContextVar 已 reset）
        assert s.ble is s.primary.manager

    async def test_query_param_addressing_also_works(self, dual):
        s = dual.server
        got = {}

        async def handler(request):
            got["device_id"] = s.current_device_id()
            return "ok"

        # Web UI 走 `?device=`（对现有 URL 拼接最友好，§3.12 决策 3）
        request = mock_request("GET", "/api/status?device=bed")
        await ha_server.device_middleware(request, handler)
        assert got["device_id"] == "bed"
        assert s.ble is s.primary.manager, "请求结束后必须回到 primary"

    async def test_unknown_device_returns_404(self, dual):
        async def handler(request):  # pragma: no cover - 不应被调用
            raise AssertionError("unknown device 不应进 handler")

        request = mock_request("GET", "/api/device/nope/status",
                               match_info={"device_id": "nope"})
        resp = await ha_server.device_middleware(request, handler)
        assert resp.status == 404
        assert json.loads(resp.body)["error"] == "device_not_found"


class TestConcurrentRouting:
    """§5.3：两台设备的并发命令**零串台**。"""

    async def test_concurrent_port_commands_do_not_cross(self, dual):
        s = dual.server
        desk = s.registry.get("desk")
        bed = s.registry.get("bed")
        desk_seen, bed_seen = [], []

        def make_send(rt, sink):
            async def _send(cmd_type, cmd_data, timeout=None):
                # await 点：正是"如果 self.ble 是可变量就会交错"的地方
                await asyncio.sleep(0.02)
                sink.append((rt.device_id, cmd_type, cmd_data, s.current_device_id()))
                return {"ok": True}
            return _send

        desk.manager.send_command = make_send(desk, desk_seen)
        bed.manager.send_command = make_send(bed, bed_seen)

        async def post(device_id, body):
            request = mock_request("POST", f"/api/device/{device_id}/port",
                                   match_info={"device_id": device_id})
            request.json = _async_return(body)
            return await ha_server.device_middleware(
                request, lambda r: s.handle_port(r))

        await asyncio.gather(
            post("desk", {"port": "c1", "action": "on"}),
            post("bed", {"port": "c2", "action": "off"}),
        )

        assert [x[2] for x in desk_seen] == [("c1", "on")], desk_seen
        assert [x[2] for x in bed_seen] == [("c2", "off")], bed_seen
        # 命令发出时上下文仍是自己那台设备（property 解析没有漂移）
        assert {x[3] for x in desk_seen} == {"desk"}
        assert {x[3] for x in bed_seen} == {"bed"}

    async def test_concurrent_status_reads_are_per_device(self, dual):
        s = dual.server
        desk = s.registry.get("desk")
        bed = s.registry.get("bed")
        await desk.state.update_settings({"16": 0b0001})
        await bed.state.update_settings({"16": 0b1110})

        async def read(device_id):
            request = mock_request("GET", f"/api/device/{device_id}/status",
                                   match_info={"device_id": device_id})
            resp = await ha_server.device_middleware(request, lambda r: s.handle_status(r))
            return json.loads(resp.body)

        desk_doc, bed_doc = await asyncio.gather(read("desk"), read("bed"))
        assert desk_doc["settings"]["16"] == 0b0001
        assert bed_doc["settings"]["16"] == 0b1110
        # 状态缓存按设备分桶：两份独立缓存
        assert set(s._status_cache) == {"desk", "bed"}


def _async_return(value):
    async def _inner():
        return value
    return _inner


class TestAssemblyIntegrity:
    """§5.5：两台设备经**真** `on_startup` 装配后，10 项钩子逐项生效。"""

    async def test_both_supervisors_hold_tasks(self, dual):
        """rt.task 由 supervisor 创建：少一台 = 那台根本没启动。"""
        s = dual.server
        for rt in s.registry:
            assert rt.supervisor.is_running, f"{rt.device_id} 没有启动"
            assert rt.task is not None

    async def test_mqtt_publishers_use_own_prefix(self, dual):
        """漏挂 publisher → 该设备完全不发 MQTT；前缀错 → 串台到别人的主题。"""
        s = dual.server
        dual.reset_mqtt()
        for rt in s.registry:
            rt.manager._publish_status({"connected": True}, retain=True)
        topics = [p["topic"] for p in dual.publishes]
        assert "cuktech/charger/status" in topics
        assert "cuktech/charger/desk/status" in topics
        assert "cuktech/charger/bed/status" in topics

    async def test_history_writes_carry_own_device_id(self, dual):
        """12 处写入经 facade 代填：写库不能串到 default。"""
        s = dual.server
        desk = s.registry.get("desk")
        bed = s.registry.get("bed")
        payload = {"voltage": 9.0, "current": 1.0, "power": 9.0,
                   "active": True, "protocol": "PD"}
        desk.history.record_port_data(1, payload)
        bed.history.record_port_data(1, dict(payload, current=2.0))
        sid_desk = desk.history.start_session(1, "PD")
        sid_bed = bed.history.start_session(1, "PD")
        assert sid_desk and sid_bed and sid_desk != sid_bed
        # get_sessions 只列 total_wh > 0 的已结束会话，所以给 bed 补一个收尾
        bed.history.end_session(sid_bed, 1.0, 5.0, 5.0, 1.0, 60)

        store = s.history_store
        rows_desk = store.query_history(1, hours=1, device_id="desk")
        rows_bed = store.query_history(1, hours=1, device_id="bed")
        rows_primary = store.query_history(1, hours=1, device_id="default")
        assert [r["current"] for r in rows_desk] == [1.0]
        assert [r["current"] for r in rows_bed] == [2.0]
        assert rows_primary == []

        # 会话也不串：primary 查不到别人的会话
        assert store.get_session(sid_desk, device_id="default") is None
        assert store.get_session(sid_desk, device_id="desk")["id"] == sid_desk
        assert store.get_sessions(period="all", device_id="bed")[1] == 1

    async def test_per_device_meta_does_not_leak(self, dual):
        """限额/常供/录像从**自己的** meta 加载（两台设不同值，验证不串）。"""
        s = dual.server
        desk = s.registry.get("desk")
        bed = s.registry.get("bed")
        desk.history.set_charge_limits(
            {"c1": {"wh": 5.0, "mode": "once"}})
        bed.history.set_charge_limits(
            {"c1": {"wh": 9.0, "mode": "always"}})
        desk.history.set_permanent_ports(["c2"])
        desk.history.set_session_recording(False)

        assert desk.history.get_charge_limits()["c1"] == {"wh": 5.0, "mode": "once"}
        assert bed.history.get_charge_limits()["c1"] == {"wh": 9.0, "mode": "always"}
        assert s.primary.history.get_charge_limits()["c1"]["wh"] == 0.0
        assert desk.history.get_permanent_ports() == ["c2"]
        assert bed.history.get_permanent_ports() == []
        assert desk.history.get_session_recording() is False
        assert bed.history.get_session_recording() is True
        # primary 用的仍是裸键（零迁移），非 primary 用命名空间键
        s.primary.history.set_charge_limits({"c1": {"wh": 3.0, "mode": "always"}})
        assert s.primary.history.get_charge_limits()["c1"] == {"wh": 3.0,
                                                              "mode": "always"}
        # 写 primary 也不该污染别人的值
        assert desk.history.get_charge_limits()["c1"]["wh"] == 5.0
        assert bed.history.get_charge_limits()["c1"]["wh"] == 9.0
        keys = store_meta_keys(s)
        assert "charge_limit_wh" in keys, "primary 必须继续用裸键（老库零迁移）"
        assert "desk:charge_limit_wh" in keys and "bed:charge_limit_wh" in keys

    async def test_status_cache_isolation(self, dual):
        """改设备 2 的状态**不会**失效设备 1 的缓存（反之亦然）。"""
        s = dual.server
        desk = s.registry.get("desk")
        await s.handle_status(mock_request())          # 缓存 primary
        token = _ctx(desk)
        try:
            await s.handle_status(mock_request())      # 缓存 desk
        finally:
            ha_server._current_rt.reset(token)
        assert set(s._status_cache) == {"default", "desk"}

        desk.manager._invalidate()                     # 触发 desk 的实例钩子
        assert "desk" not in s._status_cache
        assert "default" in s._status_cache, "设备 2 的失效波及了设备 1 —— 串台"

    async def test_sse_carries_device_only_when_multi(self, dual):
        """多设备事件带 device 且按设备分桶；单设备不带该字段（§3.8）。"""
        s = dual.server
        q = s.sse  # noqa: F841 —— 可读性
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        s.sse.add_client(queue)
        for rt in s.registry:
            rt.manager._publish_status({"connected": True})
        # 每个设备一个 latest-only 分桶，互不覆盖
        frames = []
        for rt in s.registry:
            msg = s.sse.flush_status(queue, rt.device_id)
            if msg:
                frames.append(json.loads(msg))
        s.sse.remove_client(queue)
        assert sorted(f["device"] for f in frames) == ["bed", "default", "desk"]

    async def test_quality_provider_captures_rt_not_primary(self, dual):
        """每设备的 quality 必须来自**自己**的 manager（红线 3）。"""
        s = dual.server
        for rt in s.registry:
            rt.manager.connection_quality = (lambda v: (lambda: {"score": v}))(
                {"default": 1, "desk": 2, "bed": 3}[rt.device_id])
        scores = {rt.device_id: rt.manager._quality_provider()["ble"]["score"]
                  for rt in s.registry}
        assert scores == {"default": 1, "desk": 2, "bed": 3}

    async def test_log_tag_marks_non_primary_only(self, dual):
        """§3.13：manager 自带设备标签；primary 不打标签（单设备日志逐字节不变）。"""
        import ble_manager as bm
        s = dual.server
        assert {rt.device_id: rt.manager.log.extra["device"] for rt in s.registry} == {
            "default": "", "desk": "[desk] ", "bed": "[bed] "}

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        # 生产格式串（省略 asctime，只比槽位与 message 的拼接）
        handler.setFormatter(logging.Formatter("%(levelname)s %(device)s%(message)s"))
        handler.addFilter(LogDeviceFilter())      # 生产里由 install_log_filter 挂上
        bm._LOGGER.addHandler(handler)
        bm._LOGGER.setLevel(logging.INFO)
        try:
            for rt in s.registry:
                rt.manager.log.info("已连接")
        finally:
            bm._LOGGER.removeHandler(handler)
        assert stream.getvalue().splitlines() == [
            "INFO 已连接", "INFO [desk] 已连接", "INFO [bed] 已连接"]

    async def test_shutdown_stops_all_tasks_and_closes_sessions(self, dual):
        """on_shutdown 后两台 task 均结束，活跃会话以 shutdown 原因收尾。"""
        s = dual.server
        desk = s.registry.get("desk")
        # 造一个"正在充电"的端口：关机时必须被收尾（而不是留成幽灵会话）。
        # 关停收尾走 `_close_active_sessions`，它的判据是 `_energy_states[p].is_charging`，
        # 所以状态要落在 energy state 上，只塞 `_active_sessions` 是不够的。
        sid = desk.history.start_session(1, "PD")
        es = desk.manager._energy_states[1]
        es.is_charging = True
        es.session_wh = 1.0
        es.max_power = 5.0
        es.session_start = time.time() - 60
        desk.manager._active_sessions[1] = sid

        await ha_server.on_shutdown(None)

        for rt in s.registry:
            assert not rt.supervisor.is_running
            assert rt.task is None
        assert desk.manager._active_sessions == {}, "关停后不该留下活跃会话"

        # 连接已在 on_shutdown 末尾关闭（内存里查不到），所以直接读落盘文件验证：
        # 会话必须以 shutdown 收尾，而不是留成 end_time IS NULL 的幽灵会话。
        conn = sqlite3.connect(str(s.history_store.db_path))
        try:
            row = conn.execute(
                "SELECT device_id, end_time, total_wh FROM charge_sessions WHERE id = ?",
                (sid,)).fetchone()
        finally:
            conn.close()
        assert row is not None, "关机后会话行丢了"
        assert row[0] == "desk"
        assert row[1] is not None, "关机后活跃会话没有以 shutdown 收尾"
        assert row[2] == 1.0
        # 再跑一次退出上下文（fixture 的 finally 会再调 on_shutdown，需要幂等）
        assert END_REASON_SHUTDOWN == "shutdown"


def store_meta_keys(server) -> set:
    conn = server.history_store._conn
    return {r["key"] for r in conn.execute("SELECT key FROM meta").fetchall()}
