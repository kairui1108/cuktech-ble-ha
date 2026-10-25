"""虚拟充电器的验证（设计文档 §7 P1：「1 真机 + 8 虚拟机跑通注册/路由/SSE/DB 全链路」）。

三层断言，从便宜到贵：

1. **密码学往返**（drift guard）：虚拟设备产出的帧必须能被**生产** `decrypt()` 解开，
   生产 `_encrypt()` 产出的帧必须能被虚拟设备解开。任何一侧改了 nonce 布局、
   tag 长度、`it` 字节序，这里先红。
2. **帧 → 生产解码**：一帧端口推送喂进 `_process_decrypted_frame`，状态必须按
   `decode_port` 的口径更新。
3. **全链路**：`Server(controller_factory=...)` + 真 `on_startup`，3 台虚拟充电器
   同时上线——注册表顺序、per-device 状态、SSE 归属、DB 的 device_id 全部到位。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import ha_server  # noqa: E402
from golden_harness import golden_env, snapshot_state  # noqa: E402
from charger_sim import (  # noqa: E402
    SIM_SESSION_KEYS, VirtualCharger, VirtualChargerController,
    make_controller_factory, sim_decrypt_app, sim_encrypt_dev,
)
from golden_harness import mock_request  # noqa: E402


class TestCryptoRoundTrip:
    """① 虚拟设备的帧必须走**生产**的加解密实现。"""

    def test_device_frame_decrypts_with_production_decrypt(self):
        ctrl = VirtualChargerController("AA:BB:CC:DD:EE:01", "00" * 16)
        pt = bytes([16, 0x20, 0, 0, 0x04, 0x01, 2, 1, 0, 0, 0, 0, 1, 0x07, 30, 90])
        env = sim_encrypt_dev(SIM_SESSION_KEYS, it=0, plaintext=pt)
        # 帧里只有 it 低 16 位，生产的 decrypt 期望["it_lo","it_hi", ciphertext]
        assert ctrl.decrypt(env) == pt

    def test_dev_it_wraparound_is_handled_by_production_decrypt(self):
        """`it` 跨 65535：生产侧靠低 16 位回绕检测补高位，虚拟设备必须能配合。"""
        ctrl = VirtualChargerController("AA:BB:CC:DD:EE:01", "00" * 16)
        pt = bytes([16, 0x20, 0, 0, 0x04, 0x01, 2, 1, 0, 0, 0, 0, 1, 0x07, 30, 90])
        assert ctrl.decrypt(sim_encrypt_dev(SIM_SESSION_KEYS, 65535, pt)) == pt
        # 回绕：设备计数器继续自增到 65536，线上只带低 16 位（=0）；
        # 生产侧靠回绕检测把高位补回，nonce 因此仍然是 65536。
        assert ctrl.decrypt(sim_encrypt_dev(SIM_SESSION_KEYS, 65536, pt)) == pt

    def test_app_frame_decrypts_on_device_side(self):
        ctrl = VirtualChargerController("AA:BB:CC:DD:EE:01", "00" * 16)
        req = ctrl._build_miot_tlv(1, 2, 16, None)
        enc = ctrl._encrypt(req)                      # 生产的 app→device 加密
        assert sim_decrypt_app(SIM_SESSION_KEYS, enc) == req

    def test_wire_geometry_matches_recorded_frames(self):
        """notify 帧头 4 字节 + it(2) + ciphertext(含 4 字节 tag)，与 fixtures 一致。"""
        ctrl = VirtualChargerController("AA:BB:CC:DD:EE:01", "00" * 16)
        pt = ctrl.charger.port_push_plaintext(1)
        frame = ctrl.frame_for(pt)
        assert frame[0:2] == b"\x00\x00"       # ctr
        assert frame[2] == 0x02                # inline 格式标记（生产按它分流）
        assert len(frame) == 4 + 2 + len(pt) + 4


class TestDeviceFrameToProductionDecode:
    """② 一帧推送 → 生产解码路径。"""

    async def test_port_push_updates_state_through_real_decoder(self, single):
        env = single
        mgr = env.server.ble
        ctrl = VirtualChargerController(mgr.mac, "00" * 16)
        ctrl.charger.set_port(2, voltage=9.0, current=2.0, code=0x07)
        pt = ctrl.charger.port_push_plaintext(2)
        await mgr._process_decrypted_frame(pt)
        ps = mgr.state.ports[2]
        assert (ps.voltage, ps.current) == (9.0, 2.0)
        assert ps.active is True

    async def test_get_reply_encodes_value_production_parses(self):
        """设备侧 GET 应答布局必须与 `_recv_get_response` 的偏移一致。"""
        ctrl = VirtualChargerController("AA:BB:CC:DD:EE:01", "00" * 16)
        ctrl.charger.settings[16] = 0x0D
        pt = ctrl.charger.get_reply_plaintext(2, 16)
        # 生产解析：b4=pt[4], siid=pt[6], piid=pt[7], err=pt[9:11], len=pt[11],
        # type=pt[12], value=pt[13:17]
        assert (pt[4], pt[6], pt[7]) == (0x03, 2, 16)
        assert (pt[9], pt[10]) == (0, 0)
        assert pt[11] == 4 and pt[12] == 5
        assert int.from_bytes(pt[13:17], "little") == 0x0D

    async def test_port_get_reply_is_decodable(self):
        """端口 GET 应答：最后 4 字节就是端口读数（verify_port 要 decode_port）。"""
        ctrl = VirtualChargerController("AA:BB:CC:DD:EE:01", "00" * 16)
        ctrl.charger.set_port(3, voltage=12.0, current=1.5, code=0x60)
        pt = ctrl.charger.get_reply_plaintext(2, 3)
        from state import decode_port
        info = decode_port(3, pt)
        assert info is not None
        assert (info["voltage"], info["current"]) == (12.0, 1.5)


class TestDeviceStateMachine:
    def test_set_mask_controls_ports(self):
        dev = VirtualCharger()
        req = bytes([11, 0x20, 1, 0, 0x00, 0x01, 2, 16, 0,
                     0x01, 0x10]) + bytes([0x0A])       # type=1,len=1,value=0x0A
        dev.handle_request(req)
        assert dev.mask() == 0x0A
        assert dev.enabled == {"c1": False, "c2": True, "c3": False, "a": True}

    def test_get_returns_current_mask(self):
        dev = VirtualCharger()
        dev.settings[16] = 0x03
        reply = dev.handle_request(bytes([11, 0x20, 1, 0, 0x02, 0x01, 2, 16, 0,
                                          0x01, 0x10]) + b"\x00")[0]
        assert reply[4] == 0x03
        assert int.from_bytes(reply[13:17], "little") == 0x03


# ── 全链路：3 台虚拟充电器 + 真 on_startup ──

DEVICES = [
    {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16},
    {"id": "bed", "mac": "AA:BB:CC:DD:EE:03", "token": "22" * 16},
]


@pytest.fixture
async def single():
    """单设备环境（端口解码类测试用；不启动真 BLE 循环）。"""
    async with golden_env() as env:
        snapshot_state(env.server.state)
        yield env


@pytest.fixture
async def virtual_fleet(monkeypatch):
    """3 台虚拟充电器（primary + desk + bed），跑真 `on_startup` + 真 `start()`。"""
    from golden_harness import GOLDEN_MAC

    # BLE 扫描是唯一的真实硬件依赖：用 controller_factory 注入 + 假扫描绕开它。
    # P2 起扫描统一走 AdapterCoordinator（一次 discover 服务所有等待者），所以这里
    # 替换的是 `BleakScanner.discover` —— 顺带也就测到了扫描合并这条路径。
    import bleak

    class _Adv:
        service_data: dict = {}

    found = {mac: (type("Dev", (), {"address": mac})(), _Adv())
             for mac in [GOLDEN_MAC, DEVICES[0]["mac"], DEVICES[1]["mac"]]}
    scanned = {"count": 0}

    async def fake_discover(timeout=None, return_adv=False, **kwargs):
        scanned["count"] += 1
        return dict(found)

    monkeypatch.setattr(bleak.BleakScanner, "discover",
                        staticmethod(fake_discover))

    # 三台设备各自把 c1 设成不同读数，用来证明路由不串台
    chargers = {
        GOLDEN_MAC: VirtualCharger(seed=1),
        DEVICES[0]["mac"]: VirtualCharger(seed=2),
        DEVICES[1]["mac"]: VirtualCharger(seed=3),
    }
    for seed, mac in enumerate((GOLDEN_MAC, DEVICES[0]["mac"], DEVICES[1]["mac"])):
        chargers[mac].set_port(1, voltage=5.0 + seed, current=1.0 + seed,
                               in_use=True, code=0x07)
    factory = make_controller_factory(push_interval=0.02, chargers=chargers)

    async with golden_env(devices=DEVICES, controller_factory=factory,
                          real_start=True) as env:
        for rt in env.server.registry:
            snapshot_state(rt.state)

        # 等三台都真正"活"起来：`connected` 在认证时就翻真，但端口读数要等主循环
        # 开始消费 cmd_recv 推送（`_connect` 内部有 2s+2s 的固件稳定等待）。
        async def _ready():
            return all(rt.manager.state.ports[1].voltage > 0
                       for rt in env.server.registry)

        for _ in range(600):
            if await _ready():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("虚拟充电器没在 30s 内上线: %r" % (
                {rt.device_id: rt.manager.state.connected for rt in env.server.registry},))

        # 让 1s 的 port_history 采样至少落两条（历史断言需要）
        for _ in range(100):
            if all(env.server.history_store.query_history(
                    1, hours=1, device_id=rt.device_id) for rt in env.server.registry):
                break
            await asyncio.sleep(0.1)
        env.factory = factory
        env.chargers = chargers
        env.scanned = scanned
        yield env


class TestFleetEndToEnd:
    async def test_all_devices_register_and_go_online(self, virtual_fleet):
        s = virtual_fleet.server
        assert [rt.device_id for rt in s.registry] == ["default", "desk", "bed"]
        for rt in s.registry:
            assert rt.manager.state.authenticated is True, f"{rt.device_id} 未认证"
            # 设备信息来自虚拟设备（read_device_info 走的是真实现）
            assert rt.manager.state.device_model == "CUKTECH-VIRTUAL"
            assert rt.manager.state.connected is True, f"{rt.device_id} 未在线"

    async def test_each_device_reports_its_own_port_reading(self, virtual_fleet):
        """路由/C 口读数按设备隔离：三台的 c1 读数必须各自不同且正确。"""
        s = virtual_fleet.server
        expected = {"default": 5.0, "desk": 6.0, "bed": 7.0}
        got = {}
        for rt in s.registry:
            request = mock_request("GET", f"/api/device/{rt.device_id}/status",
                                   match_info={"device_id": rt.device_id})
            resp = await ha_server.device_middleware(request,
                                                    lambda r: s.handle_status(r))
            doc = json.loads(resp.body)
            got[rt.device_id] = doc["ports"]["1"]["voltage"]
            assert doc["ports"]["1"]["voltage"] == expected[rt.device_id]
        assert got == expected

    async def test_db_rows_carry_the_right_device_id(self, virtual_fleet):
        """port_history 三份写入各自带自己的 device_id（不是全塞给 default）。"""
        s = virtual_fleet.server
        store = s.history_store
        seen = {}
        for rt in s.registry:
            rows = store.query_history(1, hours=1, device_id=rt.device_id)
            seen[rt.device_id] = len(rows)
            assert rows, f"{rt.device_id} 没有写入 port_history"
            assert all(r["current"] > 0 for r in rows)
        assert set(seen) == {"default", "desk", "bed"}

    async def test_commands_reach_only_their_target_device(self, virtual_fleet):
        """并发命令零串台：desk 关 c1 时 bed 的掩码不受影响。"""
        s = virtual_fleet.server
        chargers = virtual_fleet.chargers
        desk_ctrl = virtual_fleet.factory.created[DEVICES[0]["mac"]]
        bed_ctrl = virtual_fleet.factory.created[DEVICES[1]["mac"]]

        async def post(device_id, body):
            request = mock_request("POST", f"/api/device/{device_id}/port",
                                   match_info={"device_id": device_id})

            async def _json():
                return body
            request.json = _json
            return await ha_server.device_middleware(request,
                                                    lambda r: s.handle_port(r))

        await post("desk", {"port": "c1", "action": "off"})
        await asyncio.sleep(0.2)
        assert desk_ctrl.charger.mask() & 0x01 == 0, "desk 的 c1 没被关掉"
        assert bed_ctrl.charger.mask() & 0x01 == 0x01, "命令串到了 bed"

    async def test_sse_frames_carry_device_identity(self, virtual_fleet):
        s = virtual_fleet.server
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        s.sse.add_client(queue)
        try:
            for rt in s.registry:
                rt.manager._publish_status({"connected": True})
            frames = []
            for rt in s.registry:
                msg = s.sse.flush_status(queue, rt.device_id)
                if msg:
                    frames.append(json.loads(msg))
        finally:
            s.sse.remove_client(queue)
        assert sorted(f["device"] for f in frames) == ["bed", "default", "desk"]

    async def test_single_device_rebuild_does_not_disturb_others(self, virtual_fleet):
        """单台断线/卡死只重建那一台：其他设备的任务/状态/读数必须原封不动。

        这是 P3 验收里"单台断线不影响其他"的软件面证据（真机面由 P5 canary 覆盖）。
        """
        s = virtual_fleet.server
        desk = s.registry.get("desk")
        others = [rt for rt in s.registry if rt.device_id != "desk"]
        before = {rt.device_id: (rt.manager, rt.task, rt.manager.state.ports[1].voltage)
                  for rt in others}

        await s.restart_device(desk)
        await asyncio.wait_for(desk.supervisor._rebuild_task, timeout=10.0)

        for rt in others:
            manager, task, voltage = before[rt.device_id]
            assert rt.manager is manager, f"{rt.device_id} 的 manager 被顺手重建了"
            assert rt.task is task, f"{rt.device_id} 的任务被停掉了"
            assert rt.supervisor.is_running
            assert rt.manager.state.ports[1].voltage == voltage

        # 被重建的那台要能自己回来（新 manager 走同一条 controller_factory 路径）
        for _ in range(400):
            if desk.manager.state.ports[1].voltage > 0:
                break
            await asyncio.sleep(0.05)
        assert desk.manager.state.ports[1].voltage == 6.0
