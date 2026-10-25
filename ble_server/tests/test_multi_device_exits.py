"""P3 服务端出口的现房测试：Bemfa 命名空间 + config 设备管理接口。

P3 的对外出口里，`/api/devices`、双寻址路由、SSE 的 `device` 字段、MQTT per-device
前缀都已随 P1 内核落地（见 `test_multi_device_kernel.py` / `test_golden_multi_device.py`）。
这里补另外两块**只对多设备才成立**的出口：

1. **Bemfa 命名空间（§3.11）**：primary 沿用老实体 id（用户配好的语音指令不能失效），
   其他设备用 `{device_id}_` 前缀；topic 前缀 → 设备必须**最长匹配**，否则 primary
   会吃掉别的设备的事件；命令回调必须捕获 `rt`（否则一条语音指令会开到别人家的口）。
2. **config 设备管理（§3.10 / §3.12）**：前端发统一设备数组，后端决定文件结构
   （primary 回 `ble:` 节、其余进 `devices:` 节），写盘前用生产校验规则验一遍。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import ha_server  # noqa: E402
from golden_harness import golden_env, mock_request  # noqa: E402

DESK = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16}
BED = {"id": "bed", "mac": "AA:BB:CC:DD:EE:03", "token": "22" * 16}


class FakeBemfa:
    def __init__(self, uid="", modified=False):
        self.devices = {}
        self.commands = {}
        self.states = []
        self.started = False
        self.is_connected = True

    def add_device(self, entity_id, name):
        self.devices[entity_id] = name

    def on_command(self, entity_id, cb):
        self.commands[entity_id] = cb

    def publish_state(self, entity_id, state):
        self.states.append((entity_id, state))

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    def quality(self):
        return {"score": 100, "uptime": 1, "ping_lost": 0, "reconnect_count": 0}


class TestBemfaNamespace:
    @pytest.fixture
    async def dual(self, monkeypatch):
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            monkeypatch.setattr(ha_server, "BemfaClient", FakeBemfa)
            yield s

    async def test_primary_keeps_legacy_entity_ids(self, dual):
        """primary 的实体 id 一个都不能改：已配的语音指令锚在这里（零迁移）。"""
        ids = dual._bemfa_entity_ids(dual.primary)
        assert ids == {"c1": "cuktech_c1", "c2": "cuktech_c2", "c3": "cuktech_c3",
                       "a": "cuktech_usb_a", "ble": "cuktech_ble"}

    async def test_other_devices_are_prefixed(self, dual):
        ids = dual._bemfa_entity_ids(dual.registry.get("desk"))
        assert ids == {"c1": "desk_c1", "c2": "desk_c2", "c3": "desk_c3",
                       "a": "desk_usb_a", "ble": "desk_ble"}

    async def test_topic_prefix_matching_is_longest_first(self, dual):
        """`cuktech/charger` 是 `cuktech/charger/desk` 的前缀：必须取最长匹配。"""
        assert dual._bemfa_device_for_topic(
            "cuktech/charger/desk/port/c1").device_id == "desk"
        assert dual._bemfa_device_for_topic(
            "cuktech/charger/port/c1").device_id == "default"
        assert dual._bemfa_device_for_topic("unrelated/topic") is None

    async def test_publish_routes_to_owning_device(self, dual):
        dual.bemfa = FakeBemfa()
        dual._bemfa_publish("cuktech/charger/port/c1", {"active": True})
        dual._bemfa_publish("cuktech/charger/desk/port/c1", {"active": False})
        dual._bemfa_publish("cuktech/charger/desk/status", {"connected": True})
        assert dual.bemfa.states == [("cuktech_c1", "on"), ("desk_c1", "off"),
                                     ("desk_ble", "on")]

    async def test_allowed_devices_default_is_primary_only(self, dual):
        """缺省只给 primary 建模，避免 N 台设备时实体爆炸（§3.11）。"""
        await dual.setup_bemfa() if dual.config.bemfa.enabled else None
        targets = [rt.device_id for rt in dual.registry
                   if "*" in dual.config.bemfa.allowed_devices
                   or rt.device_id in dual.config.bemfa.allowed_devices]
        assert dual.config.bemfa.allowed_devices == ["default"]
        assert targets == ["default"]

    async def test_setup_registers_per_device_and_captures_rt(self, dual, monkeypatch):
        """命令回调必须落到**那台设备**的队列（红线：绝不通过 property 解析）。"""
        dual.config.bemfa.enabled = True
        dual.config.bemfa.uid = "test-uid"
        dual.config.bemfa.allowed_devices = ["*"]
        dual.bemfa = None
        await dual.setup_bemfa()
        assert isinstance(dual.bemfa, FakeBemfa)
        assert dual.bemfa.started is True
        assert "cuktech_c1" in dual.bemfa.devices
        assert "desk_c1" in dual.bemfa.devices
        # desk 的显示名带设备名（primary 用配置里的老名字）
        assert dual.bemfa.devices["desk_c1"] == "desk C口1"
        assert dual.bemfa.devices["cuktech_c1"] == dual.config.bemfa.name_c1

        desk = dual.registry.get("desk")
        before_primary = dual.primary.manager.cmd_queue.qsize()
        dual.bemfa.commands["desk_c1"](True)
        # 命令回调是 bemfa 线程里触发的，走 call_soon_threadsafe 排到事件循环
        await asyncio.sleep(0)
        assert desk.manager.cmd_queue.qsize() == 1, "语音指令没进 desk 的队列"
        assert dual.primary.manager.cmd_queue.qsize() == before_primary, \
            "语音指令串到了 primary —— 会去开别人家的端口"


class TestConfigDevicesApi:
    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK]) as env:
            yield env

    async def test_get_lists_devices_with_primary_flag(self, dual):
        resp = await dual.server.handle_config_get(mock_request("GET", "/api/config"))
        import json
        doc = json.loads(resp.body)["config"]
        devices = doc["devices"]
        assert [d["device_id"] for d in devices] == ["default", "desk"]
        assert [d["is_primary"] for d in devices] == [True, False]
        assert "****" in devices[0]["token"], "敏感字段必须照旧打码"

    async def test_save_rejects_removing_primary(self, dual):
        """primary 不可删除：统一数组里一台 primary 都没有 → 400，绝不落盘。"""
        err = dual.server._apply_devices_payload(
            {}, [{"id": "desk", "mac": DESK["mac"], "token": DESK["token"],
                  "is_primary": False}])
        assert err and "primary" in err

    async def test_save_rejects_dropping_primary_mac(self, dual):
        """B 形（primary 在 `ble:` 段）也不许把 primary 的 mac 清空。"""
        existing = {"ble": {"mac": "", "token": "00" * 16}}
        err = dual.server._apply_devices_payload(
            existing, [{"id": "desk", "mac": DESK["mac"], "token": DESK["token"]}])
        assert err and "primary" in err

    async def test_save_rejects_duplicate_mac(self, dual):
        err = dual.server._apply_devices_payload(
            {}, [{"device_id": "default", "mac": "AA:BB:CC:DD:EE:01", "is_primary": True},
                 {"device_id": "desk", "mac": "AA:BB:CC:DD:EE:01", "token": "11" * 16}])
        assert err and "MAC" in err

    async def test_save_rejects_bad_device_id(self, dual):
        err = dual.server._apply_devices_payload(
            {}, [{"device_id": "default", "mac": "AA:BB:CC:DD:EE:01", "is_primary": True},
                 {"device_id": "Desk Two!", "mac": "AA:BB:CC:DD:EE:02",
                  "token": "11" * 16}])
        assert err and "非法" in err

    async def test_save_writes_primary_to_ble_section(self, dual):
        """primary 展开回 `ble:` 节（老工具仍可读），其余进 `devices:` 节。"""
        existing = {"ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "00" * 16,
                            "ble_key": "", "scan_timeout": 10}}
        err = dual.server._apply_devices_payload(existing, [
            {"device_id": "default", "mac": "AA:BB:CC:DD:EE:01",
             "token": "****abcd", "is_primary": True},
            {"device_id": "bed", "name": "卧室", "mac": "AA:BB:CC:DD:EE:03",
             "token": "22" * 16},
        ])
        assert err is None
        assert existing["ble"]["token"] == "00" * 16, "打码值不能覆盖真实 token"
        assert existing["devices"] == [{"id": "bed", "mac": "AA:BB:CC:DD:EE:03",
                                        "name": "卧室", "token": "22" * 16}]

    async def test_save_drops_devices_section_when_empty(self, dual):
        existing = {"ble": {"mac": "AA:BB:CC:DD:EE:01"}, "devices": [{"id": "old"}]}
        err = dual.server._apply_devices_payload(
            existing, [{"device_id": "default", "mac": "AA:BB:CC:DD:EE:01",
                        "is_primary": True}])
        assert err is None
        assert "devices" not in existing, "删光附加设备后不该留下空节残留"

    async def test_save_round_trips_frontend_payload(self, dual, tmp_path, monkeypatch):
        """锁定前端（config.html）**真实**发出的 payload 形状。

        前端发的是 `{ble:{…primary…}, devices:[附加设备…]}`：primary 走 `ble:` 段
        （token/ble_key 是打码值），`devices:` 里的附加设备带 `id` 且**不带**
        `is_primary`，可选字段可能为空串。这个形状必须能存进 YAML 并回读，
        否则配置页的多设备编辑等于写了个寂寞。
        """
        import yaml
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "00" * 16, "ble_key": "abcd"},
            "devices": [{"id": "desk", "mac": DESK["mac"], "token": DESK["token"],
                         "bemfa_ble": "desk_ble_old"}],
        }))
        monkeypatch.setattr(dual.server, "_config_path", lambda: cfg_path)
        async def _no_restart():
            return None
        monkeypatch.setattr(dual.server, "_delayed_restart", _no_restart)

        payload = {"config": {
            "ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "00****" + "0" * 4,
                    "ble_key": "****"},
            "devices": [
                # 已有设备：token 留空 = 没改，必须继承 YAML 里的旧 token
                {"id": "desk", "name": "书桌", "mac": DESK["mac"], "token": "",
                 "ble_key": "", "topic_prefix": "", "bemfa_ble": "desk_ble"},
                # 新设备：完整填写
                {"id": "bed", "name": "", "mac": BED["mac"], "token": BED["token"],
                 "ble_key": "", "topic_prefix": "", "bemfa_ble": ""},
            ],
            "mqtt": {"enabled": False, "host": "localhost", "port": 1883,
                     "username": "", "password": "****", "topic_prefix": "cuktech/charger"},
            "bemfa": {"enabled": False, "uid": "****"},
            "server": {"port": 8081, "log_level": "info", "history_retention_days": 90},
        }}
        req = mock_request("POST", "/api/config")
        async def _json():
            return payload
        req.json = _json
        resp = await dual.server.handle_config_save(req)
        assert resp.status == 200, resp.body

        saved = yaml.safe_load(cfg_path.read_text())
        assert saved["ble"]["mac"] == "AA:BB:CC:DD:EE:01"
        assert saved["ble"]["token"] == "00" * 16, "打码 token 不能覆盖真实值"
        assert saved["ble"]["ble_key"] == "abcd", "打码 ble_key 不能覆盖真实值"
        by_id = {d["id"]: d for d in saved["devices"]}
        assert set(by_id) == {"desk", "bed"}
        assert by_id["desk"]["token"] == DESK["token"], "空 token 必须继承旧值"
        assert by_id["desk"]["name"] == "书桌"
        assert by_id["desk"]["bemfa_ble"] == "desk_ble"
        assert by_id["bed"]["token"] == BED["token"]
        assert "name" not in by_id["bed"] and "bemfa_ble" not in by_id["bed"], \
            "空的可选字段不该写进 YAML"

        # 写出来的东西必须能被生产解析器吃下去（否则重启即崩）
        from config import _parse_devices, BLEConfig
        ble = BLEConfig(**saved["ble"])
        specs = _parse_devices(saved, ble)
        assert [s.device_id for s in specs] == ["desk", "bed"]
        assert specs[0].bemfa_names == {"ble": "desk_ble"}


class TestNewConfigFields:
    """P2/P3 新增配置项的解析（默认值必须保持单设备行为不变）。"""

    def test_startup_stagger_defaults_to_two_seconds(self, tmp_path):
        from conftest import write_config
        from config import load_config
        import os

        path = write_config(tmp_path)
        os.environ["CUKTECH_CONFIG_PATH"] = path
        try:
            assert load_config().server.startup_stagger_sec == 2.0
        finally:
            os.environ.pop("CUKTECH_CONFIG_PATH", None)

    def test_startup_stagger_is_configurable(self, tmp_path):
        from conftest import write_config
        from config import load_config
        import os

        path = write_config(tmp_path, server={"startup_stagger_sec": 0.5})
        os.environ["CUKTECH_CONFIG_PATH"] = path
        try:
            assert load_config().server.startup_stagger_sec == 0.5
        finally:
            os.environ.pop("CUKTECH_CONFIG_PATH", None)

    def test_bemfa_allowed_devices_default_is_primary_only(self, tmp_path):
        """缺省只给 primary 建语音实体：多设备不该让用户的米家设备列表爆炸。"""
        from conftest import write_config
        from config import load_config
        import os

        path = write_config(tmp_path, devices=[DESK])
        os.environ["CUKTECH_CONFIG_PATH"] = path
        try:
            assert load_config().bemfa.allowed_devices == ["default"]
        finally:
            os.environ.pop("CUKTECH_CONFIG_PATH", None)

    def test_bemfa_allowed_devices_is_configurable(self, tmp_path):
        from conftest import write_config
        from config import load_config
        import os

        path = write_config(tmp_path, bemfa={"allowed_devices": ["default", "desk"]})
        os.environ["CUKTECH_CONFIG_PATH"] = path
        try:
            assert load_config().bemfa.allowed_devices == ["default", "desk"]
        finally:
            os.environ.pop("CUKTECH_CONFIG_PATH", None)
