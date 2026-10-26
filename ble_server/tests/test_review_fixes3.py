"""OCR 评审（PR #16）在服务端侧带出的缺陷与脚手架缺陷。

每条都对应一个"测试看不见、真机上才发作"的具体失败模式，因此这里各钉一条：

1. 扫描合并的**领导者被取消**时，跟随者手里的 Future 永远不会被收尾 ——
   `await pending` 没有超时，那个 MAC 之后再也重连不上，且没有任何日志。
   （取消在 `RuntimeSupervisor.stop()` / per-device 重建时是常态。）
2. `Server._DEVICE_KEYS` 是 `devices:` 落盘白名单，曾经只放行 `bemfa_ble`，
   而 `DeviceConfig.from_mapping` 收任意 `bemfa_*`：手写的 `bemfa_c1` 会被
   任意一次 Web 保存静默丢弃 → 实体 id 回落派生值 → 巴法云主题变化。
3. 虚拟充电器：`request_count` 从没自增过（文档承诺的计数恒为 0）、替身客户端
   缺 `read_gatt_char`（生产的 60s 静默探测会把它变成"设备掉线"）。
4. 测试脚手架：`make_device` 按 id **长度**生成 MAC（desk/bed 撞车）、
   `_yaml_scalar` 把 dict 渲染成字符串、`diff_report` 截断时用负切片。
"""
import asyncio
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import ha_server  # noqa: E402
from adapter_coordinator import AdapterCoordinator  # noqa: E402
from charger_sim import VirtualChargerController  # noqa: E402
from config import BEMFA_PORTS, DeviceConfig  # noqa: E402
from conftest import _yaml_scalar, make_config_yaml, make_device  # noqa: E402
from golden_harness import diff_report, golden_env  # noqa: E402


class TestScanCoalescingCancellation:
    """领导者被取消时，跟随者必须拿到结果/照常重试，而不是永久悬挂。"""

    @pytest.fixture
    def coord(self):
        coord = AdapterCoordinator(device_count_provider=lambda: 2)

        async def noop():
            return None

        async def stuck_scan(timeout):        # 模拟卡在真实扫描里的窗口
            await asyncio.sleep(10)
            return {}

        coord.scan_cleanup = noop
        coord._discover = stuck_scan
        return coord

    async def test_waiters_are_released_when_leader_is_cancelled(self, coord):
        MAC = "AA:BB:CC:DD:EE:02"
        leader = asyncio.ensure_future(coord.find_device(MAC, 1.0))
        await asyncio.sleep(0.05)             # 让领导者进入扫描窗口
        waiter = asyncio.ensure_future(coord.find_device(MAC, 1.0))
        await asyncio.sleep(0.05)             # 让跟随者挂到同一个 Future 上

        leader.cancel()
        # 修复前：跟随者永远等下去（没有任何超时），这里 TimeoutError
        result = await asyncio.wait_for(waiter, timeout=1.0)
        assert result is None, "被取消的扫描窗口应表达为'这次没扫到'，让调用方重试"

    async def test_pending_slot_is_cleared_after_cancellation(self, coord):
        MAC = "AA:BB:CC:DD:EE:03"
        leader = asyncio.ensure_future(coord.find_device(MAC, 1.0))
        await asyncio.sleep(0.05)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        assert MAC.lower() not in coord._pending, (
            "取消后必须摘牌，否则后续所有 find_device 都会挂到一个死 Future 上")


class TestDeviceKeyWhitelist:
    """`devices:` 落盘白名单必须覆盖 `from_mapping` 认的全部 `bemfa_*` 键。"""

    def test_every_bemfa_port_key_is_writable(self):
        keys = set(ha_server.Server._DEVICE_KEYS)
        missing = [f"bemfa_{port}" for port in BEMFA_PORTS if f"bemfa_{port}" not in keys]
        assert not missing, (
            f"落盘白名单缺 {missing}：用户手写的实体 id 覆盖会在 Web 保存后消失")

    async def test_web_save_keeps_hand_written_entity_names(self):
        """端到端：手写的 `bemfa_c1` 经过一次 Web 保存必须还在（曾经被静默丢弃）。"""
        desk = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16,
                "bemfa_c1": "desk_c1_switch", "bemfa_ble": "desk_ble"}
        async with golden_env(devices=[{"id": "desk", "mac": "AA:BB:CC:DD:EE:02",
                                        "token": "11" * 16}]) as env:
            existing = {"ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "00" * 16},
                        "devices": [dict(desk)]}
            err = env.server._apply_devices_payload(existing, [dict(desk)])
            assert err is None, err
        written = existing["devices"][0]
        assert written["bemfa_c1"] == "desk_c1_switch"
        assert written["bemfa_ble"] == "desk_ble"

    def test_whitelist_matches_what_from_mapping_parses(self):
        """两处规则不许各自漂移：解析得到的键，必须都在白名单里。"""
        item = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02",
                **{f"bemfa_{port}": f"name_{port}" for port in BEMFA_PORTS}}
        cfg = DeviceConfig.from_mapping(item)
        assert set(cfg.bemfa_names) == set(BEMFA_PORTS)
        for port in cfg.bemfa_names:
            assert f"bemfa_{port}" in ha_server.Server._DEVICE_KEYS


class TestVirtualChargerContract:
    """虚拟设备的计数与 GATT 读口必须与生产 controller 的用法一致。"""

    def _ctrl(self):
        return VirtualChargerController("AA:BB:CC:DD:EE:04", "11" * 16)

    async def test_request_count_tracks_handled_frames(self):
        ctrl = self._ctrl()
        await ctrl.connect()
        from cuktech_ble.protocol import CHAR_CMD_SEND
        req = ctrl._build_miot_tlv(1, 2, 16, None)         # GET 一条属性
        wire = bytes([0x01, 0x00]) + ctrl._encrypt(req)     # 数据帧（头部帧已握过手）
        await ctrl.client.write_gatt_char(CHAR_CMD_SEND, wire)
        assert ctrl.request_count == 1, (
            "request_count 恒为 0 时，任何断言它的用例都是假绿/假红")

    async def test_read_gatt_char_returns_firmware(self):
        """生产的 60s 静默链路探测会读固件版本；缺这个方法会被当成'链路丢失'。"""
        from cuktech_ble.protocol import CHAR_FW_VERSION
        ctrl = self._ctrl()
        raw = await ctrl.client.read_gatt_char(CHAR_FW_VERSION)
        assert raw.rstrip(b"\x00").decode("ascii") == ctrl.charger.firmware


class TestHarnessRegressions:
    """测试脚手架自身的坑（踩了会让"用的地方"看起来像是业务 bug）。"""

    def test_make_device_mac_is_content_derived(self):
        """同为 4 字符的 id 不能撞 MAC（desk/bed 曾经都是 AA:BB:CC:DD:EE:0E）。"""
        a, b = make_device("desk"), make_device("bed")
        assert a["mac"] != b["mac"]
        assert make_device("desk")["mac"] == a["mac"], "同一 id 必须稳定派生"
        assert a["token"] != b["token"]

    def test_device_macs_pass_duplicate_validation(self):
        """生成的 MAC 必须能过 P1 的"MAC 不可重复"校验（脚手架不能自带非法配置）。"""
        from config import Config, validate_device_specs
        cfg = Config()
        cfg.ble.mac = "AA:BB:CC:DD:EE:01"
        cfg.ble.token = "00112233445566778899aabbccddeeff"
        cfg.devices = [DeviceConfig.from_mapping(make_device(i)) for i in ("desk", "bed")]
        validate_device_specs(cfg)      # 不抛 = 通过

    def test_yaml_scalar_renders_dict_as_mapping(self):
        text = make_config_yaml(bemfa={"entities": [{"name": "x", "device": "default",
                                                     "port": "c1"}]})
        docs = yaml.safe_load(text)
        assert docs["bemfa"]["entities"] == [
            {"name": "x", "device": "default", "port": "c1"}]

    def test_yaml_scalar_rejects_nothing_silently(self):
        assert _yaml_scalar({"a": 1}) == '{a: 1}'

    def test_diff_report_never_uses_negative_slice(self):
        """limit 用尽后递归截断不能变成负切片（那会丢掉**尾部**差异 = 最深那条）。"""
        deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        other = {"a": {"b": {"c": {"d": {"e": 2}}}}}
        lines = diff_report(deep, other, limit=1)
        assert any("e" in line for line in lines), (
            f"深层差异必须出现在报告里，实际只有：{lines}")
