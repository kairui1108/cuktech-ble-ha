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
        self.deleted = []
        self.started = False
        self.is_connected = True

    def add_device(self, entity_id, name):
        self.devices[entity_id] = name

    def on_command(self, entity_id, cb):
        self.commands[entity_id] = cb

    def publish_state(self, entity_id, state):
        self.states.append((entity_id, state))

    async def delete_topics(self, entity_ids):
        self.deleted.extend(entity_ids)
        return len(entity_ids)

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


class TestBemfaEntityTable:
    """自定义语音开关表（§3.11）：一行 = (显示名, 设备, 端口)。

    这块是"多设备 × 用户自己命名"的交点：名字不能只属于设备（那样 N 台的开关
    全叫"C口1开关"），也不能只属于端口（那样一台机器只能有一套名字）。所以既
    要证明"配了几条就只建几条"，也要证明"每条的开关只动它绑的那台设备的那个口"。
    """

    @pytest.fixture
    async def dual(self, monkeypatch):
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            monkeypatch.setattr(ha_server, "BemfaClient", FakeBemfa)
            s.config.bemfa.enabled = True
            s.config.bemfa.uid = "test-uid"
            s.bemfa = None
            yield s

    @staticmethod
    def _bind(s, *rows):
        """把 (名字, 设备, 端口) 三元组绑成实体表，并打开 `entities_defined`。"""
        from config import BemfaEntityConfig
        s.config.bemfa.entities = [BemfaEntityConfig(name=n, device=d, port=p)
                                   for n, d, p in rows]
        s.config.bemfa.entities_defined = True
        # 老字段故意设成"全给"：证明实体表一旦存在就完全接管建模
        s.config.bemfa.allowed_devices = ["*"]

    async def test_plan_binds_name_to_device_and_port(self, dual):
        self._bind(dual, ("客厅 C1", "default", "c1"),
                   ("书房 C1", "desk", "c1"), ("书房蓝牙", "desk", "ble"))
        plan = dual._bemfa_entity_plan()
        assert [(i["device_id"], i["port"], i["name"], i["entity_id"]) for i in plan] == [
            ("default", "c1", "客厅 C1", "cuktech_c1"),
            ("desk", "c1", "书房 C1", "desk_c1"),
            ("desk", "ble", "书房蓝牙", "desk_ble"),
        ], "表里写了三条就只该有三条（不是 N 台 × 5 的笛卡尔积）"

    async def test_plan_ignores_allowed_devices(self, dual):
        """实体表存在时 allowed_devices 不再参与建模（否则会多建出一堆没名字的）。"""
        self._bind(dual, ("客厅 C1", "default", "c1"))
        assert len(dual._bemfa_entity_plan()) == 1

    async def test_empty_table_means_no_entities(self, dual):
        """显式空表 = 用户一个开关都不要，绝不能回退成"派生 5 个"。"""
        self._bind(dual)
        await dual.setup_bemfa()
        assert dual._bemfa_entity_plan() == []
        assert dual.bemfa.devices == {}
        assert dual.bemfa.commands == {}

    async def test_setup_registers_exactly_the_table(self, dual):
        self._bind(dual, ("客厅 C1", "default", "c1"), ("书房 C2", "desk", "c2"))
        await dual.setup_bemfa()
        assert dual.bemfa.devices == {"cuktech_c1": "客厅 C1", "desk_c2": "书房 C2"}
        assert set(dual.bemfa.commands) == {"cuktech_c1", "desk_c2"}

    async def test_command_carries_the_bound_device_and_port(self, dual):
        """红线：语音指令只能进它绑的那台设备的队列，而且只动那一行写的端口。"""
        self._bind(dual, ("书房 C2", "desk", "c2"))
        await dual.setup_bemfa()
        desk = dual.registry.get("desk")
        before = dual.primary.manager.cmd_queue.qsize()
        dual.bemfa.commands["desk_c2"](True)
        await asyncio.sleep(0)   # 回调走 call_soon_threadsafe，排到事件循环再取
        assert dual.primary.manager.cmd_queue.qsize() == before, \
            "语音指令串到了 primary —— 会去开别人家的端口"
        assert desk.manager.cmd_queue.qsize() == 1
        assert desk.manager.cmd_queue.get_nowait()[:2] == ("port", ("c2", "on"))

    async def test_publish_only_for_bound_entities(self, dual):
        self._bind(dual, ("书房 C2", "desk", "c2"))
        await dual.setup_bemfa()
        dual._bemfa_publish("cuktech/charger/desk/port/c2", {"active": True})
        dual._bemfa_publish("cuktech/charger/desk/port/c1", {"active": True})
        dual._bemfa_publish("cuktech/charger/desk/status", {"connected": True})
        assert dual.bemfa.states == [("desk_c2", "on")], \
            "表里没有的口/开关不该发状态（派生 id 不是'这个口有开关'的证据）"

    async def test_ble_entity_carries_connection_state(self, dual):
        self._bind(dual, ("书房蓝牙", "desk", "ble"))
        await dual.setup_bemfa()
        dual._bemfa_publish("cuktech/charger/desk/status", {"connected": False})
        assert dual.bemfa.states == [("desk_ble", "off")]

    async def test_stale_entity_is_deleted_from_cloud(self, dual):
        """从表里删掉的实体，巴法云 App 里不能留一个按了没反应的死开关。"""
        dual.config.bemfa.registered = ["old_c1", "cuktech_c1"]
        self._bind(dual, ("客厅 C1", "default", "c1"))
        await dual.setup_bemfa()
        assert dual.bemfa.deleted == ["old_c1"]

    async def test_registered_snapshot_goes_to_db_meta(self, dual):
        """回写记在 **DB meta**，config.yaml 一个字节都不动。

        以前写进 YAML 的 `bemfa.registered`：`yaml.safe_load + yaml.dump` 整份重写，
        会把用户手写的注释、顶层键序（dump 默认 sort_keys）和引号风格全部抹掉。
        这是服务端自己的运行期记账，meta 表本来就是干这个的。
        """
        import json
        from pathlib import Path
        before = Path(dual._config_path()).read_bytes()
        self._bind(dual, ("客厅 C1", "default", "c1"), ("书房 C2", "desk", "c2"))
        await dual.setup_bemfa()
        stored = json.loads(dual.history_store.get_meta(
            ha_server.Server.BEMFA_REGISTERED_META_KEY, "[]"))
        assert stored == ["cuktech_c1", "desk_c2"], stored
        assert Path(dual._config_path()).read_bytes() == before, \
            "记账绝不该改写用户的 config.yaml"

    async def test_config_get_exposes_the_table(self, dual):
        import json
        self._bind(dual, ("客厅 C1", "default", "c1"))
        resp = await dual.handle_config_get(mock_request("GET", "/api/config"))
        bemfa = json.loads(resp.body)["config"]["bemfa"]
        assert bemfa["entities"] == [{"name": "客厅 C1", "device": "default", "port": "c1"}]
        assert bemfa["entitiesDefined"] is True
        assert list(bemfa["ports"]) == ["c1", "c2", "c3", "a", "ble"]


class TestBemfaEntityPersistence:
    """保存路径：实体表要能存进 YAML、改没改要能被识别、指向不存在的设备要拦下。"""

    @pytest.fixture
    async def env(self, monkeypatch, tmp_path):
        async with golden_env(devices=[DESK]) as e:
            s = e.server
            monkeypatch.setattr(ha_server, "BemfaClient", FakeBemfa)
            cfg_path = tmp_path / "config.yaml"

            async def _no_restart():
                return None
            monkeypatch.setattr(s, "_config_path", lambda: cfg_path)
            monkeypatch.setattr(s, "_delayed_restart", _no_restart)
            yield s, cfg_path

    @staticmethod
    def _write(cfg_path, bemfa):
        import yaml
        cfg_path.write_text(yaml.safe_dump({
            "ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "00" * 16},
            "devices": [{"id": "desk", "mac": DESK["mac"], "token": DESK["token"]}],
            "bemfa": bemfa,
        }), encoding="utf-8")

    @staticmethod
    def _payload(**bemfa):
        return {"config": {
            "ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "00****" + "0" * 4},
            "devices": [{"id": "desk", "mac": DESK["mac"], "token": ""}],
            "bemfa": bemfa,
        }}

    async def _save(self, s, payload):
        req = mock_request("POST", "/api/config")

        async def _json():
            return payload
        req.json = _json
        return await s.handle_config_save(req)

    async def test_entities_round_trip_and_mark_modified(self, env):
        import yaml
        s, cfg_path = env
        self._write(cfg_path, {"enabled": True, "uid": "uid-1", "name_c1": "老名字"})
        rows = [{"name": "客厅 C1", "device": "default", "port": "c1"},
                {"name": "书房 C1", "device": "desk", "port": "c1"}]
        resp = await self._save(s, self._payload(enabled=True, uid="uid-1", entities=rows))
        assert resp.status == 200, resp.body
        saved = yaml.safe_load(cfg_path.read_text())["bemfa"]
        assert saved["entities"] == rows
        assert saved["name_c1"] == "老名字", "老字段没被前端带上就该原样保留"
        assert saved["modified"] is True, "实体变了必须让下次启动重建云端主题（改显示名）"

        # 存下来的东西必须能被生产解析器吃下去
        from config import config_from_mapping
        cfg = config_from_mapping(yaml.safe_load(cfg_path.read_text()))
        assert [(e.name, e.device, e.port) for e in cfg.bemfa.entities] == \
            [("客厅 C1", "default", "c1"), ("书房 C1", "desk", "c1")]

    async def test_unchanged_entities_do_not_mark_modified(self, env):
        import yaml
        s, cfg_path = env
        rows = [{"name": "客厅 C1", "device": "default", "port": "c1"}]
        self._write(cfg_path, {"enabled": True, "uid": "uid-1",
                               "entities": rows, "modified": False})
        resp = await self._save(s, self._payload(enabled=True, uid="uid-1", entities=rows))
        assert resp.status == 200, resp.body
        saved = yaml.safe_load(cfg_path.read_text())["bemfa"]
        assert saved["modified"] is False, \
            "没改实体却打 modified → 每次保存都会把巴法云主题删了重建一遍"

    async def test_entity_pointing_at_removed_device_is_rejected(self, env):
        """删设备却留着它的语音开关 → 保存当场 400，而不是重启后起不来。"""
        import yaml
        s, cfg_path = env
        self._write(cfg_path, {"enabled": True, "uid": "uid-1"})
        payload = {
            "config": {
                "ble": {"mac": "AA:BB:CC:DD:EE:01", "token": "00****" + "0" * 4},
                "devices": [],       # desk 被删了
                "bemfa": {"enabled": True, "uid": "uid-1",
                          "entities": [{"name": "书房 C1", "device": "desk", "port": "c1"}]},
            },
        }
        before = cfg_path.read_text()
        resp = await self._save(s, payload)
        assert resp.status == 400
        import json
        err = json.loads(resp.body)["error"]
        assert "desk" in err and "不存在" in err, err
        assert cfg_path.read_text() == before, "被拒的配置一个字节都不该落盘"


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
