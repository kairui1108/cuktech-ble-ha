"""P0 脚手架自身的契约测试（多设备配置形状）。

设计文档 §7 P0 的第四项交付："make_config() 多设备测试脚手架"。
P1 的 `load_config()`/`DeviceRegistry` 将直接消费这里的形状，所以先把形状钉死：
生成的 YAML 能被真 `load_config()` 解析、topic 派生属性不变、devices 节结构符合 §3.10。
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))

from conftest import (  # noqa: E402
    DEFAULT_PRIMARY_MAC, DEFAULT_PRIMARY_TOKEN, DEFAULT_TOPIC_PREFIX,
    make_config, make_config_yaml, make_device, write_config,
)


class TestSingleDeviceShape:
    def test_topic_properties_unchanged_by_scaffold(self):
        """脚手架不得改变 legacy topic 语义（§3.10：primary 就是 legacy 命名空间）。"""
        cfg = make_config()
        assert cfg.topic_status == f"{DEFAULT_TOPIC_PREFIX}/status"
        assert cfg.topic_settings == f"{DEFAULT_TOPIC_PREFIX}/settings"
        assert cfg.topic_port == f"{DEFAULT_TOPIC_PREFIX}/port"
        assert cfg.topic_charge_event == f"{DEFAULT_TOPIC_PREFIX}/charge_event"
        assert cfg.devices == []

    def test_yaml_parses_through_real_load_config(self, monkeypatch, tmp_path):
        """生成的 YAML 走真 `load_config()`，值必须与直接构造一致。

        这条是脚手架的"不自娱自乐"保证：如果哪天 YAML 生成器和 Config 漂移了，
        这里先炸。
        """
        from config import load_config

        path = write_config(tmp_path, server={
            "host": "127.0.0.1", "port": 8199, "log_level": "warning",
            "history_retention_days": 30,
            "history_db_path": str(tmp_path / "history.db"),
        })
        monkeypatch.setenv("CUKTECH_CONFIG_PATH", path)
        for key in ("CUKTECH_DEVICE_MAC", "CUKTECH_DEVICE_TOKEN", "MQTT_HOST",
                    "MQTT_PORT", "CUKTECH_SERVER_PORT", "CUKTECH_HISTORY_DB_PATH"):
            monkeypatch.delenv(key, raising=False)
        loaded = load_config()
        direct = make_config()
        assert loaded.ble.mac == direct.ble.mac == DEFAULT_PRIMARY_MAC
        assert loaded.ble.token == direct.ble.token == DEFAULT_PRIMARY_TOKEN
        assert loaded.mqtt.topic_prefix == direct.mqtt.topic_prefix
        assert loaded.server.port == direct.server.port
        assert loaded.server.history_db_path == str(tmp_path / "history.db")


class TestMultiDeviceShape:
    def test_devices_are_ordered_and_disjoint_from_primary(self):
        """`devices:` 追加在 primary 之后，且 primary 不出现在列表里（§3.10）。"""
        cfg = make_config(devices=[make_device("desk"), make_device("bed")])
        assert [d.device_id for d in cfg.devices] == ["desk", "bed"]
        assert cfg.ble.mac == DEFAULT_PRIMARY_MAC   # primary 仍来自 ble: 节

    def test_yaml_devices_section_round_trips(self):
        """devices 节结构（id/name/mac/token/topic_prefix）符合 §3.10 的字段集。"""
        docs = yaml.safe_load(make_config_yaml(
            devices=[make_device("desk"),
                     make_device("bed", topic_prefix="cuktech/bed")]
        ))
        assert [d["id"] for d in docs["devices"]] == ["desk", "bed"]
        assert docs["devices"][0]["topic_prefix"] == ""       # 空 = 自动派生
        assert docs["devices"][1]["topic_prefix"] == "cuktech/bed"
        assert docs["devices"][0]["mac"] != docs["devices"][1]["mac"]
        assert docs["devices"][0]["token"].startswith("ff")

    def test_empty_devices_section_is_written_not_omitted(self):
        """`devices=[]` 写出空节：P1 校验要区分"节缺失"与"节为空"。"""
        docs = yaml.safe_load(make_config_yaml(devices=[]))
        assert "devices" in docs and docs["devices"] == []
        docs_single = yaml.safe_load(make_config_yaml())
        assert "devices" not in docs_single

    def test_extra_device_tokens_are_valid_hex(self):
        """每台设备的 token 必须能被 `bytes.fromhex`（BLEManager 构造即解）。"""
        for dev in (make_device("desk"), make_device("bed"), make_device("x9")):
            bytes.fromhex(dev["token"])
        cfg = make_config(devices=[make_device("desk")])
        bytes.fromhex(cfg.ble.token)

    def test_load_config_parses_devices_section(self, monkeypatch, tmp_path):
        """P1：`load_config()` 已能解析 `devices:` 节（P0 时这里断言的是"还不认"）。

        P0 记录现状、P1 翻面 —— 这份断言的翻转本身就是"改动发生在哪一行"的证据。
        """
        from config import load_config

        path = write_config(tmp_path, devices=[make_device("desk")])
        monkeypatch.setenv("CUKTECH_CONFIG_PATH", path)
        loaded = load_config()
        assert [d.device_id for d in loaded.devices] == ["desk"]
        specs = loaded.device_specs()
        assert specs[0].device_id == "default" and specs[0].is_primary
        assert specs[1].device_id == "desk"
        # 前缀自动派生（§3.10：空 = `{mqtt.topic_prefix}/{id}`）
        assert loaded.topics_for(specs[1]).prefix == f"{DEFAULT_TOPIC_PREFIX}/desk"
        assert loaded.topics_for(specs[0]).prefix == DEFAULT_TOPIC_PREFIX
