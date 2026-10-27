"""第二轮评审发现项的回归测试（P1/P2）。

每条用例都对应一个**真实发生过的故障形态**：

* `wait_for_publish()` 不带超时 + 在事件循环里调用 = broker 卡住时整台服务器冻结
  （所有设备的 HTTP/SSE/BLE 命令一起停摆）。
* 配置页给老配置做种子时只铺 primary 的 5 行，而派生规则是 `allowed_devices × 5`：
  配了 `allowed_devices: ["*"]` 的用户只是改了 MQTT 地址，其它设备的语音开关
  就被写没、并被服务端从巴法云删掉。
* 服务端记账（`bemfa.registered`）写进 config.yaml，`yaml.dump` 会把用户手写的
  注释、键序、引号风格整份抹掉。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import ha_server  # noqa: E402
from golden_harness import golden_env, mock_request  # noqa: E402

DESK = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16}


def _json_request(method, path, body, **kwargs):
    req = mock_request(method, path, **kwargs)

    async def _json():
        return body

    req.json = _json
    return req


class _SlowInfo:
    """`publish()` 的返回值：`wait_for_publish` 慢一点，并记录收到的超时。"""

    def __init__(self, sink, delay):
        self._sink = sink
        self._delay = delay

    def wait_for_publish(self, timeout=None):
        self._sink.append(timeout)
        time.sleep(self._delay)


class _SlowBroker:
    """连着但不回 PUBACK 的 broker（真机上表现为 broker 卡死 / 半死连接）。"""

    def __init__(self, delay=0.2):
        self.timeouts = []
        self.delay = delay

    def is_connected(self):
        return True

    def publish(self, *args, **kwargs):
        return _SlowInfo(self.timeouts, self.delay)


class TestRetainedClearNeverBlocksTheLoop:
    async def test_wait_for_publish_gets_a_timeout(self):
        """paho 的默认值是 None = **永远等**；清 retained 必须自己带上限。"""
        s = ha_server.Server.__new__(ha_server.Server)
        s.mqtt_client = _SlowBroker(delay=0.0)
        s._mqtt_publish_failures = 0
        assert s._mqtt_clear_retained("cuktech/charger/status") is True
        assert s.mqtt_client.timeouts == [s.RETAINED_CLEAR_TIMEOUT], \
            "必须把超时传下去，否则 broker 一卡就无限等"

    async def test_purge_runs_off_the_event_loop(self):
        """清 retained 是阻塞调用：必须丢线程，否则整台服务器一起停摆。"""
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            s.mqtt_client = _SlowBroker(delay=0.05)      # 9 条 topic ≈ 0.45s
            ticks = 0

            async def beat():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            task = asyncio.create_task(beat())
            try:
                await asyncio.sleep(0.02)
                before = ticks
                cleared = await s.purge_removed_devices([s.registry.get("desk")])
                after = ticks
            finally:
                task.cancel()

            assert len(cleared) == 9, cleared
            assert after - before >= 5, (
                f"清 retained 期间事件循环被冻住了（心跳只走了 {after - before} 次）")


class TestDerivedEntitiesAreExposedForMigration:
    """老配置的派生清单必须由**服务端**给出，前端照抄才不会丢开关。"""

    @pytest.fixture
    async def legacy(self):
        """模拟 `allowed_devices: ["*"]` 的老配置（没写过 entities 键）。"""
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            s.config.bemfa.enabled = True
            s.config.bemfa.uid = "uid"
            s.config.bemfa.allowed_devices = ["*"]
            s.config.bemfa.entities = []
            s.config.bemfa.entities_defined = False
            yield env

    async def test_config_get_lists_derived_entities(self, legacy):
        s = legacy.server
        resp = await s.handle_config_get(mock_request("GET", "/api/config"))
        bemfa = json.loads(resp.body)["config"]["bemfa"]
        assert bemfa["entitiesDefined"] is False
        derived = bemfa["derivedEntities"]
        assert len(derived) == 10, derived          # 2 台 × 5 个开关
        assert {d["device"] for d in derived} == {"default", "desk"}
        assert bemfa["allowedDevices"] == ["*"]
        # 名字也由服务端给：前端不再自己拼（拼出来的和注册时的显示名会漂移）
        assert all(d["name"] for d in derived)

    async def test_derived_seed_matches_runtime_plan(self, legacy):
        """页面的种子必须与运行期 plan 逐条等价：否则"保存一次"就会删掉别人的开关。"""
        s = legacy.server
        plan = s._bemfa_entity_plan()
        resp = await s.handle_config_get(mock_request("GET", "/api/config"))
        derived = json.loads(resp.body)["config"]["bemfa"]["derivedEntities"]
        assert [(d["device"], d["port"], d["name"]) for d in derived] == \
               [(p["device_id"], p["port"], p["name"]) for p in plan]

    async def test_saving_the_seed_deletes_nothing_from_the_cloud(self, legacy):
        """老配置 → 用派生种子保存 → 重启后实体集合不变（差集为空，不会删云端开关）。"""
        s = legacy.server
        before = {i["entity_id"] for i in s._bemfa_entity_plan()}
        assert len(before) == 10

        resp = await s.handle_config_get(mock_request("GET", "/api/config"))
        derived = json.loads(resp.body)["config"]["bemfa"]["derivedEntities"]
        payload = {"config": {"bemfa": {"enabled": True, "uid": "", "entities": derived}}}
        saved = await s.handle_config_save(_json_request("POST", "/api/config", payload))
        assert saved.status == 200, saved.body

        # 重启才会生效：用落盘后的文件重新解析，再看 plan 与差集
        from config import load_config
        cfg = load_config(legacy.config_path)
        assert cfg.bemfa.entities_defined is True
        assert len(cfg.bemfa.entities) == 10
        s.config.bemfa = cfg.bemfa
        after = [i["entity_id"] for i in s._bemfa_entity_plan()]
        assert sorted(after) == sorted(before), \
            f"保存后实体集合变了 → 差集非空 → 会从巴法云删掉别人的开关：{sorted(after)}"


class TestBemfaBookkeepingStaysOutOfUserConfig:
    """服务端记账不写 config.yaml：那是用户手写的文件，注释与键序不能被动。"""

    async def test_registered_snapshot_uses_db_meta(self):
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            s.config.bemfa.enabled = True
            s.config.bemfa.uid = "uid"
            s.config.bemfa.allowed_devices = ["default"]
            s.config.bemfa.entities = []
            s.config.bemfa.entities_defined = False
            before_bytes = Path(env.config_path).read_bytes()

            await s.setup_bemfa()

            stored = json.loads(s.history_store.get_meta(
                ha_server.Server.BEMFA_REGISTERED_META_KEY, "[]"))
            assert sorted(stored) == sorted(s._bemfa_map.values()), stored
            assert "cuktech_c1" in stored
            assert Path(env.config_path).read_bytes() == before_bytes, \
                "记账不该改写用户的 config.yaml"

    async def test_legacy_yaml_registered_is_migrated_to_meta(self):
        async with golden_env(devices=[DESK]) as env:
            s = env.server
            s.config.bemfa.enabled = True
            s.config.bemfa.uid = "uid"
            s.config.bemfa.registered = ["cuktech_c1", "cuktech_c2"]   # 旧版本写进 YAML 的
            snap = s._bemfa_registered_snapshot()
            assert snap == ["cuktech_c1", "cuktech_c2"]
            migrated = json.loads(s.history_store.get_meta(
                ha_server.Server.BEMFA_REGISTERED_META_KEY, "[]"))
            assert migrated == ["cuktech_c1", "cuktech_c2"]

    async def test_modified_flag_is_patched_in_place(self, tmp_path, monkeypatch):
        """只改 `modified:` 这一行：注释、键序、引号风格原样保留。"""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "# 我的充电器配置（手写注释）\n"
            "bemfa:\n"
            "  enabled: true\n"
            "  modified: true      # 上次改过名字\n"
            "  name_c1: \"C口1开关\"\n"
            "\n"
            "# 主设备\n"
            "ble:\n"
            "  mac: \"AA:BB:CC:DD:EE:01\"   # 别忘了改\n", encoding="utf-8")
        monkeypatch.setenv("CUKTECH_CONFIG_PATH", str(cfg))

        s = ha_server.Server.__new__(ha_server.Server)
        s._clear_bemfa_modified()

        text = cfg.read_text(encoding="utf-8")
        assert "modified: false" in text
        assert "modified: true" not in text
        assert "# 我的充电器配置（手写注释）" in text, "注释被整份抹掉了"
        assert "# 主设备" in text and "# 别忘了改" in text
        assert 'name_c1: "C口1开关"' in text, "引号风格被改写了"
        assert text.index("bemfa:") < text.index("ble:"), "键序被打乱了"

    async def test_modified_flag_inserted_when_absent(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("ble:\n  mac: x\nbemfa:\n  enabled: true\n", encoding="utf-8")
        monkeypatch.setenv("CUKTECH_CONFIG_PATH", str(cfg))

        ha_server.Server.__new__(ha_server.Server)._clear_bemfa_modified()

        text = cfg.read_text(encoding="utf-8")
        assert "bemfa:\n  modified: false\n  enabled: true" in text, text
        import yaml
        doc = yaml.safe_load(text)
        assert doc["bemfa"]["modified"] is False and doc["bemfa"]["enabled"] is True


class TestPahoVersionGuard:
    """解释器选错的伪装：paho 1.x 下会变成 55 个"看不出因果"的 setup error。"""

    def test_guard_passes_in_a_good_env(self):
        import conftest
        assert conftest._paho_version_problem() is None, \
            "守卫不能把正常环境也拦掉（那会让整套测试跑不起来）"

    def test_guard_detects_the_missing_2x_api(self, monkeypatch):
        import paho.mqtt.client as paho_client
        import conftest
        monkeypatch.delattr(paho_client, "CallbackAPIVersion", raising=False)
        problem = conftest._paho_version_problem()
        assert problem and ">=2.0" in problem, problem
