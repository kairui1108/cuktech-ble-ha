"""`tools/clean_retained.py` 的契约测试（设计文档 §6.1 回滚：清理幽灵设备）。

三件事：

1. **不硬编码前缀**：`mqtt.topic_prefix` 可配，清理用的 topic 必须按配置推导；
2. **只清非 primary**：primary 的 retained 消息是旧版本/HACS 依赖的数据，清掉等于
   让设备"消失"；
3. MQTT 没有"按 `#` 通配发布"，所以是"已知叶子 × 实际前缀"枚举——叶子集合漏一个，
   回滚后 broker 里就留一条幽灵数据。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import clean_retained  # noqa: E402
from conftest import make_config  # noqa: E402

DESK = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16}


class FakeClient:
    def __init__(self):
        self.calls = []

    def publish(self, topic, payload=None, qos=0, retain=False, **kw):
        self.calls.append({"topic": topic, "payload": payload,
                           "qos": qos, "retain": retain})


class TestPrefixDerivation:
    def test_primary_prefix_is_never_cleared(self):
        cfg = make_config(devices=[DESK])
        assert clean_retained.non_primary_prefixes(cfg) == ["cuktech/charger/desk"]

    def test_custom_topic_prefix_is_followed(self):
        """改了 `mqtt.topic_prefix` 就必须跟着改——不许出现写死的 cuktech。"""
        cfg = make_config(mqtt={"topic_prefix": "home/cuk"},
                          devices=[DESK])
        prefixes = clean_retained.non_primary_prefixes(cfg)
        assert prefixes == ["home/cuk/desk"]
        topics = clean_retained.topics_to_clear(prefixes)
        assert all(t.startswith("home/cuk/desk/") for t in topics)
        assert not any("cuktech" in t for t in topics)

    def test_explicit_device_prefix_is_respected(self):
        cfg = make_config(mqtt={"topic_prefix": "cuktech/charger"},
                          devices=[dict(DESK, topic_prefix="custom/desk")])
        assert clean_retained.non_primary_prefixes(cfg) == ["custom/desk"]

    def test_single_device_config_has_nothing_to_clean(self):
        assert clean_retained.non_primary_prefixes(make_config()) == []


class TestTopicEnumeration:
    def test_every_retained_leaf_is_covered(self):
        topics = clean_retained.topics_to_clear(["cuktech/charger/desk"])
        assert set(topics) == {
            "cuktech/charger/desk/status",
            "cuktech/charger/desk/settings",
            "cuktech/charger/desk/port/c1",
            "cuktech/charger/desk/port/c2",
            "cuktech/charger/desk/port/c3",
            "cuktech/charger/desk/port/a",
        }

    def test_topics_are_sorted_and_deduplicated(self):
        topics = clean_retained.topics_to_clear(["b/x", "a/x", "a/x"])
        assert topics == sorted(topics)
        assert len(topics) == len(set(topics))


class TestClean:
    def test_publishes_empty_payload_with_retain(self):
        """空 payload + retain=True 是 MQTT 规定的"删除 retained"操作。"""
        client = FakeClient()
        topics = clean_retained.topics_to_clear(["cuktech/charger/desk"])
        assert clean_retained.clean(client, topics) == len(topics)
        assert [c["topic"] for c in client.calls] == topics
        assert all(c["payload"] == b"" and c["retain"] is True for c in client.calls)


class TestCli:
    def test_dry_run_never_connects(self, monkeypatch):
        """`--dry-run` 必须是纯本地操作（排障时不该去碰 broker）。"""
        import paho.mqtt.client as mqtt

        def boom(*a, **k):  # pragma: no cover - 不应被调用
            raise AssertionError("dry-run 不应该连接 broker")

        monkeypatch.setattr(mqtt.Client, "connect", boom)
        cfg_path = Path(__file__).parent / "fixtures" / "clean_retained_config.yaml"
        cfg_path.parent.mkdir(exist_ok=True)
        cfg_path.write_text(
            "ble:\n  mac: 'AA:BB:CC:DD:EE:01'\n  token: '" + "00" * 16 + "'\n"
            "mqtt:\n  enabled: true\n  topic_prefix: 'cuktech/charger'\n"
            "devices:\n  - id: desk\n    mac: 'AA:BB:CC:DD:EE:02'\n"
            "    token: '" + "11" * 16 + "'\n")
        try:
            assert clean_retained.main(["--config", str(cfg_path), "--dry-run"]) == 0
        finally:
            cfg_path.unlink()

    def test_single_device_short_circuits(self, monkeypatch):
        """单设备配置：直接返回，不连接 broker。"""
        import paho.mqtt.client as mqtt
        monkeypatch.setattr(mqtt.Client, "connect",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("不该连接")))
        cfg_path = Path(__file__).parent / "fixtures" / "clean_retained_single.yaml"
        cfg_path.parent.mkdir(exist_ok=True)
        cfg_path.write_text("ble:\n  mac: 'AA:BB:CC:DD:EE:01'\n  token: '"
                            + "00" * 16 + "'\nmqtt:\n  topic_prefix: 'cuktech/charger'\n")
        try:
            assert clean_retained.main(["--config", str(cfg_path)]) == 0
        finally:
            cfg_path.unlink()
