"""评审发现项的回归测试（P1-1 / P1-2 + 若干 P2）。

每条用例都对应一个**真实发生过**的故障形态，不是"顺便测一下"：

* P1-1 `/api/protocol` 的 SSE 直接用了共享 `self.sse` → 事件缺 `device` 字段，
  前端 `deviceOf(msg)` 把它归给 primary：给 B 拨协议开关，A 的界面跟着变
  （命令打对了，展示串了）。
* P1-2 保存配置时的设备校验拿的是**进程启动时**的 mqtt 配置：同一份 payload 里
  既改 `mqtt.topic_prefix` 又带设备显式前缀时，"前缀撞车"判成通过 → 写盘 →
  `_delayed_restart()` 之后进程被 `load_config` 拒绝启动，Web UI 一起没了。
* P2 会话曲线越权读返回 200 + `no_curve`（伪装成"没有曲线"），与同组的
  `/sessions/{id}/export`（404）自相矛盾。
* P2 A 形设备数组里清空 token 输入框 = 不可逆的凭证擦除。
* P2 服务层伸手改 manager / supervisor 的私有方法（边界只是名义上的）；
  `_assert_no_assignment` 用 assert（`python -O` 下整条消失）。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import ha_server  # noqa: E402
from golden_harness import golden_env, mock_request  # noqa: E402

DESK = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16}
BED = {"id": "bed", "mac": "AA:BB:CC:DD:EE:03", "token": "22" * 16}


def _json_request(method, path, body, **kwargs):
    req = mock_request(method, path, **kwargs)

    async def _json():
        return body

    req.json = _json
    return req


class TestProtocolSSECarriesDevice:
    """P1-1：设备维度的 SSE 必须带 `device`（§3.8）。"""

    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK]) as env:
            yield env

    async def test_protocol_event_carries_target_device(self, dual):
        s = dual.server
        desk = s.registry.get("desk")

        async def fake_send(cmd_type, cmd_data, timeout=None):
            return {"ok": True}

        desk.manager.send_command = fake_send
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        s.sse.add_client(queue)
        try:
            token = ha_server._current_rt.set(desk)
            try:
                resp = await s.handle_protocol(
                    _json_request("POST", "/api/device/desk/protocol", {"value": 0x0B9A0F0F}))
            finally:
                ha_server._current_rt.reset(token)
            assert json.loads(resp.body)["ok"] is True
            frames = []
            while True:
                try:
                    frames.append(json.loads(queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break
        finally:
            s.sse.remove_client(queue)

        protocol = [f for f in frames if f.get("type") == "protocol"]
        assert protocol, "拨协议开关必须发一条 protocol 事件"
        assert all(f.get("device") == "desk" for f in protocol), \
            f"protocol 事件缺设备归属 → 前端会把 B 的变更画到 A 上：{protocol}"
        assert protocol[0]["protocol_extend"] == 0x0B9A0F0F

    async def test_single_device_protocol_event_has_no_device_field(self):
        """单设备零感知：事件体不得凭空多出 `device`（§3.8 逐字节口径）。"""
        async with golden_env() as env:          # 单设备：只有 primary
            s = env.server
            queue: asyncio.Queue = asyncio.Queue(maxsize=64)
            s.sse.add_client(queue)
            try:
                s.emit_device_event("protocol", {"switches": {}, "protocol_extend": 1})
                frame = json.loads(queue.get_nowait())
            finally:
                s.sse.remove_client(queue)
        assert "device" not in frame, f"单设备事件不该带 device：{frame}"

    async def test_helper_falls_back_to_shared_sse_without_registry(self):
        """手工装配（`Server.__new__`，17 处老测试）没有 registry/manager：
        退回共享出口，行为与改造前一致，而不是抛异常或静默丢弃。"""
        s = ha_server.Server.__new__(ha_server.Server)
        seen = []

        class _SSE:
            def emit(self, event_type, data):
                seen.append((event_type, data))

        s.sse = _SSE()
        s.emit_device_event("protocol", {"protocol_extend": 7})
        assert seen == [("protocol", {"protocol_extend": 7})]


class TestConfigSaveValidatesLikeStartup:
    """P1-2：保存 = 启动可接受（§3.10）。"""

    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK]) as env:
            yield env

    async def test_prefix_collision_after_mqtt_change_is_rejected(self, dual):
        """同一份 payload 里改基前缀 + 带设备显式前缀 → 保存期就要 400。

        旧实现拿 `self.config.mqtt`（进程启动时的旧基前缀）做 probe，于是这里判成
        "不冲突"，写盘后重启才被 load_config 拒绝 —— 那时 Web UI 已经没了。
        """
        existing = {
            "ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "00" * 16},
            # 新的基前缀正好等于 desk 的显式前缀
            "mqtt": {"topic_prefix": "cuktech/charger/desk"},
            "devices": [{"id": "desk", "mac": DESK["mac"], "token": DESK["token"],
                         "topic_prefix": "cuktech/charger/desk"}],
        }
        err = dual.server._apply_devices_payload(existing, [
            {"device_id": "default", "mac": "AA:BB:CC:DD:EE:01", "is_primary": True},
            {"device_id": "desk", "mac": DESK["mac"], "token": DESK["token"],
             "topic_prefix": "cuktech/charger/desk"},
        ])
        assert err and "前缀" in err, f"前缀撞车必须在保存期拦下：{err!r}"

    async def test_save_refuses_file_that_would_not_start(self, dual, monkeypatch):
        """文件级不变式：落盘前用启动同一个入口（load_config）试算，失败 → 400 且不写。"""
        cfg_path = dual.server._config_path()
        before = cfg_path.read_text(encoding="utf-8")

        def boom(path=None):
            raise ValueError("配置错误：模拟启动拒绝")

        monkeypatch.setattr(ha_server, "load_config", boom)
        resp = await dual.server.handle_config_save(_json_request(
            "POST", "/api/config", {"config": {"server": {"log_level": "info"}}}))
        assert resp.status == 400
        assert "模拟启动拒绝" in json.loads(resp.body)["error"]
        assert cfg_path.read_text(encoding="utf-8") == before, "校验失败绝不能落盘"
        assert not cfg_path.with_suffix(".tmp").exists(), "失败后临时文件要清掉"

    async def test_a_shape_empty_token_is_not_a_wipe(self, dual):
        """A 形（统一数组）里清空 token/ble_key 输入框 = 没改，不是擦除凭证。

        凭证擦除不可逆（只能手改 YAML）。B 形一直按"空 = 没改"处理，A 形以前漏了。
        """
        existing = {"ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "00" * 16,
                            "ble_key": "ab" * 16}}
        err = dual.server._apply_devices_payload(existing, [
            {"device_id": "default", "mac": "AA:BB:CC:DD:EE:01",
             "token": "", "ble_key": "", "is_primary": True},
        ])
        assert err is None, err
        assert existing["ble"]["token"] == "00" * 16, "空串把 token 擦掉了"
        assert existing["ble"]["ble_key"] == "ab" * 16, "空串把 ble_key 擦掉了"


class TestCrossDeviceSessionIs404:
    """P2：越权/不存在的会话曲线一律 404，不能伪装成"没有曲线"。"""

    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK, BED]) as env:
            yield env

    async def _points(self, s, rt, sid):
        token = ha_server._current_rt.set(rt)
        try:
            req = mock_request("GET", f"/api/device/{rt.device_id}/sessions/{sid}/points",
                               match_info={"id": str(sid)})
            return await s.handle_session_points(req)
        finally:
            ha_server._current_rt.reset(token)

    async def test_other_devices_session_is_404(self, dual):
        s = dual.server
        desk = s.registry.get("desk")
        bed = s.registry.get("bed")
        sid = bed.history.start_session(1, protocol="PD")
        try:
            resp = await self._points(s, desk, sid)
            assert resp.status == 404, "别人的会话必须 404（与 /sessions/{id}/export 对齐）"
            assert json.loads(resp.body)["error"] == "session not found"

            # 自己的会话照旧 200；没有曲线点时才回 no_curve
            resp = await self._points(s, bed, sid)
            assert resp.status == 200
            body = json.loads(resp.body)
            assert body["points"] == [] and body.get("no_curve") is True
        finally:
            bed.history.end_session(sid, 1.0, 10.0, 20.0, 0.5, 60)

    async def test_unknown_session_is_404(self, dual):
        resp = await self._points(dual.server, dual.server.registry.get("desk"), 987654)
        assert resp.status == 404


class TestServiceBoundaries:
    """P2：服务层不伸手改别人的私有成员；红线守卫不依赖 assert。"""

    def test_manager_public_exits_delegate(self):
        from ble_manager import BLEManager

        mgr = object.__new__(BLEManager)
        seen = []
        mgr._publish_status = lambda payload, retain=False: seen.append(("status", payload, retain))
        mgr._close_active_sessions = lambda reason: seen.append(("sessions", reason))
        mgr.publish_status({"connected": False}, retain=True)
        mgr.close_active_sessions("shutdown")
        assert seen == [("status", {"connected": False}, True), ("sessions", "shutdown")]

    def test_supervisor_startup_delay_setter(self):
        sup = ha_server.RuntimeSupervisor.__new__(ha_server.RuntimeSupervisor)
        sup.set_startup_delay("2.5")
        assert sup._startup_delay == 2.5
        sup.set_startup_delay(None)
        assert sup._startup_delay == 0.0

    def test_no_assignment_guard_raises_not_asserts(self):
        """`python -O` 会把 assert 整条删掉，而被删掉的正是最危险那条红线。"""
        s = ha_server.Server.__new__(ha_server.Server)
        s._ble_assigned = None
        s._state_assigned = object()
        s._history_assigned = None
        with pytest.raises(RuntimeError, match="_state_assigned"):
            s._assert_no_assignment()


class TestRemovedDeviceLeftovers:
    """P2：设备从配置里删掉后，retained 幽灵要清，历史数据要留。"""

    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK]) as env:
            yield env

    async def test_removed_device_retained_cleared_history_kept(self, dual):
        s = dual.server
        desk = s.registry.get("desk")

        # 先给 desk 造一条历史会话：删设备**不该**动它（删了不可逆）
        sid = desk.history.start_session(1, protocol="PD")
        desk.history.end_session(sid, 1.0, 10.0, 20.0, 0.5, 60)
        assert desk.history.get_sessions(1), "前置条件：desk 有历史会话"

        # 重启在这里必须被挡住：本用例只验保存路径的行为
        restarted = []

        async def fake_restart():
            restarted.append(True)

        s._delayed_restart = fake_restart
        dual.reset_mqtt()

        payload = {"config": {
            "ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "c1b1****c162"},
            "devices": [{"device_id": "default", "mac": "AA:BB:CC:DD:EE:01",
                         "is_primary": True}],       # ← desk 被删掉了
        }}
        resp = await s.handle_config_save(_json_request("POST", "/api/config", payload))
        assert resp.status == 200, resp.body

        cleared = [p for p in dual.publishes]
        topics = {p["topic"] for p in cleared}
        assert "cuktech/charger/desk/status" in topics, f"幽灵 retained 没清掉：{topics}"
        assert all(p["payload"] == "" and p["retain"] for p in cleared
                   if p["topic"].startswith("cuktech/charger/desk")), \
            f"清 retained 必须是空负载 + retain（'null' 不是清除）：{cleared}"
        assert not any(t.startswith("cuktech/charger/status") for t in topics), \
            "primary 的主题不该被清（设备还在）"

        assert desk.history.get_sessions(1), "删设备不该删历史数据"
        assert not restarted, "本用例里重启被替身挡下（真路径会 1s 后重启）"

    async def test_kept_devices_are_not_purged(self, dual):
        """没删设备时一条清空都不该发（避免每次保存都往 broker 写噪音）。"""
        s = dual.server
        s._delayed_restart = _noop_coro
        dual.reset_mqtt()
        payload = {"config": {
            "ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "c1b1****c162"},
            "devices": [{"device_id": "default", "mac": "AA:BB:CC:DD:EE:01",
                         "is_primary": True},
                        {"device_id": "desk", "mac": DESK["mac"],
                         "token": DESK["token"]}],
        }}
        resp = await s.handle_config_save(_json_request("POST", "/api/config", payload))
        assert resp.status == 200, resp.body
        assert dual.publishes == [], f"没删设备却发了清除：{dual.publishes}"


async def _noop_coro(*args, **kwargs):
    return None


class TestSingleDeviceParser:
    """P2：`devices:` 只有**一份**解析实现（`DeviceConfig.from_mapping`）。

    以前 `_parse_devices` 自己抄了一遍键名（只认 `id`，而 from_mapping 认
    `id`/`device_id`）与字段清洗：配置页写得进、启动读不出（或反过来）只是时间问题。
    """

    def test_parse_devices_accepts_both_key_names(self):
        from config import BLEConfig, _parse_devices

        specs = _parse_devices(
            {"devices": [{"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16},
                         {"device_id": "bed", "mac": "AA:BB:CC:DD:EE:03",
                          "token": "22" * 16}]},
            BLEConfig(mac="AA:BB:CC:DD:EE:01", token="00" * 16, scan_timeout=7))
        assert [s.device_id for s in specs] == ["desk", "bed"]
        assert [s.scan_timeout for s in specs] == [7, 7], "默认 scan_timeout 走 ble 节"

    def test_errors_are_locatable(self):
        """报错要能定位：缺 id 报下标（那时还没有名字可指），缺 mac/token 报设备名。"""
        from config import BLEConfig, _parse_devices

        ble = BLEConfig(mac="AA:BB:CC:DD:EE:01", token="00" * 16)
        with pytest.raises(ValueError, match=r"devices\[1\]"):
            _parse_devices({"devices": [{"id": "desk", "mac": "AA:BB:CC:DD:EE:02",
                                         "token": "11" * 16},
                                        {"mac": "AA:BB:CC:DD:EE:03",
                                         "token": "22" * 16}]}, ble)     # 缺 id
        with pytest.raises(ValueError, match=r"`bed` 缺少 `mac`"):
            _parse_devices({"devices": [{"id": "bed", "token": "22" * 16}]}, ble)


class TestRecoveryStateOnDevicesEndpoint:
    """P2：卡在 L1.5 退避里的设备要能从 `/api/devices` 看出来。"""

    async def test_stuck_device_is_visible_healthy_one_is_untouched(self, monkeypatch):
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            desk = s.registry.get("desk")

            async def fake_l1(mac):
                return None

            async def fake_l15(mac):
                return None

            # 不碰真适配器：只跑阶梯的分支判定
            monkeypatch.setattr(s.adapter, "_l1_disconnect", fake_l1)
            monkeypatch.setattr(s.adapter, "_l15_remove", fake_l15)
            s.adapter.set_device_count_provider(lambda: 3)
            s.adapter.register_connection(s.primary.mac)      # 别人还在线 → 升不上去

            for _ in range(3):
                await s.adapter.recover_device(desk.mac)

            resp = await s.handle_devices(mock_request("GET", "/api/devices"))
            by_id = {d["id"]: d for d in json.loads(resp.body)["devices"]}
            assert "recovery" in by_id["desk"], f"退避中的设备必须可见：{by_id['desk']}"
            assert by_id["desk"]["recovery"]["level"] == "L1.5"
            assert by_id["desk"]["recovery"]["count"] >= 1
            assert "recovery" not in by_id["default"], \
                "健康设备不该多出这个键（响应要逐字节不变）"


class TestRetainedCleanupEdges:
    """P2-2：设备还在但 topic 前缀改了 → 旧前缀的 retained 也必须清。

    这条缺口以前没人补：`clean_retained.py` 的前缀是从**当前配置**推导的，前缀一改
    旧的就再也推导不出来，等于永久幽灵。
    """

    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK]) as env:
            yield env

    async def test_changed_prefix_clears_old_retained(self, dual):
        s = dual.server
        s._delayed_restart = _noop_coro
        dual.reset_mqtt()
        payload = {"config": {
            "ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "c1b1****c162"},
            "devices": [{"device_id": "default", "mac": "AA:BB:CC:DD:EE:01",
                         "is_primary": True},
                        {"device_id": "desk", "mac": DESK["mac"],
                         "token": DESK["token"],
                         # 前缀从 .../desk 改成 .../desk2（设备还在，只是换了命名空间）
                         "topic_prefix": "cuktech/charger/desk2"}],
        }}
        resp = await s.handle_config_save(_json_request("POST", "/api/config", payload))
        assert resp.status == 200, resp.body

        topics = {p["topic"] for p in dual.publishes}
        assert "cuktech/charger/desk/status" in topics, \
            f"改前缀后旧前缀的 retained 没清（永久幽灵）：{sorted(topics)}"
        assert "cuktech/charger/desk/port/c1" in topics
        assert not any(t.startswith("cuktech/charger/desk2/") for t in topics), \
            "新前缀还没发布过，不该去清它"

    async def test_retained_clear_uses_qos1(self, dual):
        """qos=0 只是交给本地 socket：紧接着重启会断 MQTT，清除可能根本没发出去。"""
        s = dual.server
        desk = s.registry.get("desk")
        dual.reset_mqtt()
        s.purge_removed_devices([desk])
        cleared = [p for p in dual.publishes if p["topic"].endswith("/status")]
        assert cleared and all(p["qos"] == 1 for p in cleared), cleared


class TestRecoveryFieldIsPublicOnly:
    async def test_alerted_is_not_exposed(self, monkeypatch):
        """`/api/devices.recovery` 只给对外语义，内部记账（alerted）不外泄。"""
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            desk = s.registry.get("desk")

            async def fake_l1(mac):
                return None

            async def fake_l15(mac):
                return None

            monkeypatch.setattr(s.adapter, "_l1_disconnect", fake_l1)
            monkeypatch.setattr(s.adapter, "_l15_remove", fake_l15)
            s.adapter.set_device_count_provider(lambda: 3)
            s.adapter.register_connection(s.primary.mac)
            for _ in range(4):
                await s.adapter.recover_device(desk.mac)

            state = s.adapter.backoff_state(desk.mac)
            assert "alerted" not in state, state
            assert set(state) <= {"mac", "count", "since", "last_at", "level", "reason"}
            doc = json.loads((await s.handle_devices(
                mock_request("GET", "/api/devices"))).body)
            rec = {d["id"]: d for d in doc["devices"]}["desk"]["recovery"]
            assert "alerted" not in rec
