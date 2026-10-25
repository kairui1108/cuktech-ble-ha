"""P1 配置回退与校验测试（设计文档 §3.10、§5.6）。

两块内容：
1. **向后兼容**：只有 `ble:` 节时，设备解析结果恰是"一台 primary"，前缀等于
   `mqtt.topic_prefix`，topic 派生与今天的 `Config.topic_*` 逐字节一致。
2. **校验规则**：`ble:` 缺失、MAC 跨节重复、id 非法/重复/保留字、token 非 hex、
   前缀撞车——各有断言。这些不是"输入卫生"，而是"别让两台设备串台"的最后一道闸。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from conftest import (  # noqa: E402
    DEFAULT_PRIMARY_MAC, DEFAULT_TOPIC_PREFIX, EXTRA_DEVICE_TOKEN,
    make_config, make_device,
)

from config import (  # noqa: E402
    Config, DeviceConfig, PRIMARY_DEVICE_ID, TopicNamespace, validate_device_specs,
)


def _cfg(devices=None, *, mac=DEFAULT_PRIMARY_MAC, token=EXTRA_DEVICE_TOKEN,
         prefix=DEFAULT_TOPIC_PREFIX):
    return make_config(
        ble={"mac": mac, "token": token},
        mqtt={"topic_prefix": prefix},
        devices=devices,
    )


class TestSingleDeviceFallback:
    def test_no_devices_section_yields_exactly_one_primary(self):
        cfg = _cfg()
        specs = cfg.device_specs()
        assert len(specs) == 1
        assert specs[0].device_id == PRIMARY_DEVICE_ID
        assert specs[0].is_primary is True

    def test_primary_prefix_is_mqtt_prefix(self):
        """primary 的前缀就是 `mqtt.topic_prefix` —— "它就是 legacy 命名空间本身"。"""
        cfg = _cfg()
        ns = cfg.topics_for(cfg.primary_device())
        assert ns.prefix == DEFAULT_TOPIC_PREFIX

    def test_primary_topics_identical_to_legacy_properties(self):
        """`TopicNamespace` 与老 `Config.topic_*` 必须逐字节一致（单设备零感知的地基）。"""
        cfg = _cfg()
        ns = cfg.topics_for(cfg.primary_device())
        assert ns.status == cfg.topic_status
        assert ns.settings == cfg.topic_settings
        assert ns.port == cfg.topic_port
        assert ns.charge_event == cfg.topic_charge_event
        assert ns.port_topic("c1") == f"{cfg.topic_port}/c1"
        assert ns.set == f"{DEFAULT_TOPIC_PREFIX}/set"
        assert ns.ble == f"{DEFAULT_TOPIC_PREFIX}/ble"

    def test_empty_devices_list_means_single_device(self):
        """`devices: []` 合法且等价单设备（显式声明"没有附加设备"）。"""
        cfg = _cfg(devices=[])
        assert cfg.device_specs() == [cfg.primary_device()]
        validate_device_specs(cfg)      # 不抛异常即通过


class TestPrefixDerivation:
    def test_empty_topic_prefix_derives_from_base(self):
        cfg = _cfg(devices=[make_device("desk")])
        specs = cfg.device_specs()
        assert cfg.topics_for(specs[1]).prefix == f"{DEFAULT_TOPIC_PREFIX}/desk"

    def test_explicit_topic_prefix_wins(self):
        cfg = _cfg(devices=[make_device("desk", topic_prefix="cuktech/bedroom")])
        specs = cfg.device_specs()
        assert cfg.topics_for(specs[1]).prefix == "cuktech/bedroom"

    def test_nested_prefix_is_allowed(self):
        """嵌套是**默认形态**：命令订阅精确匹配，嵌套不会串台（§3.6）。"""
        cfg = _cfg(devices=[make_device("desk"), make_device("bed")])
        validate_device_specs(cfg)      # 派生出的 cuktech/charger/{desk,bed} 合法

    def test_relevant_collision_is_rejected(self):
        """某台设备的前缀正好等于另一台的主题 → 真串台，必须拒绝。"""
        cfg = _cfg(devices=[make_device("desk", topic_prefix=f"{DEFAULT_TOPIC_PREFIX}/port")])
        with pytest.raises(ValueError, match="主题重叠"):
            validate_device_specs(cfg)

    def test_identical_prefixes_rejected(self):
        cfg = _cfg(devices=[make_device("desk", topic_prefix="cuktech/x"),
                            make_device("bed", topic_prefix="cuktech/x")])
        with pytest.raises(ValueError, match="前缀相同"):
            validate_device_specs(cfg)


class TestValidationRules:
    def test_devices_without_ble_section_rejected(self):
        cfg = Config()
        cfg.devices = [DeviceConfig(device_id="desk", mac="AA:BB:CC:DD:EE:02",
                                    token=EXTRA_DEVICE_TOKEN)]
        with pytest.raises(ValueError, match="primary 设备必须在 `ble:` 节配置"):
            validate_device_specs(cfg)

    def test_duplicate_mac_across_sections_rejected(self):
        cfg = _cfg(devices=[make_device("desk", mac=DEFAULT_PRIMARY_MAC)])
        with pytest.raises(ValueError, match="同时出现在"):
            validate_device_specs(cfg)

    def test_duplicate_mac_is_case_insensitive(self):
        cfg = _cfg(devices=[make_device("desk", mac=DEFAULT_PRIMARY_MAC.lower())])
        with pytest.raises(ValueError, match="同时出现在"):
            validate_device_specs(cfg)

    def test_duplicate_device_id_rejected(self):
        cfg = _cfg(devices=[make_device("desk"), make_device("desk")])
        with pytest.raises(ValueError, match="设备 id `desk` 重复"):
            validate_device_specs(cfg)

    @pytest.mark.parametrize("bad_id", ["Port", "de sk", "-desk", "desk.", "端口"])
    def test_invalid_slug_rejected(self, bad_id):
        cfg = _cfg(devices=[make_device("desk")])
        cfg.devices[0].device_id = bad_id
        with pytest.raises(ValueError, match="非法"):
            validate_device_specs(cfg)

    @pytest.mark.parametrize("reserved", ["port", "set", "status", "ble", "settings",
                                          "charge_event", "all", "default"])
    def test_reserved_device_id_rejected(self, reserved):
        cfg = _cfg(devices=[make_device("desk")])
        cfg.devices[0].device_id = reserved
        with pytest.raises(ValueError, match="保留字|重复"):
            validate_device_specs(cfg)

    def test_non_hex_token_rejected(self):
        cfg = _cfg(devices=[make_device("desk", token="zzzz")])
        with pytest.raises(ValueError, match="token 不是合法十六进制串"):
            validate_device_specs(cfg)

    def test_odd_length_token_rejected(self):
        cfg = _cfg(devices=[make_device("desk", token="abc")])
        with pytest.raises(ValueError, match="token 不是合法十六进制串"):
            validate_device_specs(cfg)

    def test_device_missing_token_is_a_parse_error(self, tmp_path, monkeypatch):
        """解析期就报错（而不是等 BLEManager 构造时炸 bytes.fromhex）。"""
        from config import load_config

        p = Path(tmp_path) / "c.yaml"
        p.write_text(
            f'ble:\n  mac: "{DEFAULT_PRIMARY_MAC}"\n  token: "{EXTRA_DEVICE_TOKEN}"\n'
            'mqtt:\n  topic_prefix: "cuktech/charger"\n'
            'devices:\n  - id: desk\n    mac: "AA:BB:CC:DD:EE:02"\n', encoding="utf-8")
        monkeypatch.setenv("CUKTECH_CONFIG_PATH", str(p))
        with pytest.raises(ValueError, match="缺少 `token`"):
            load_config()


class TestTopicNamespaceShape:
    def test_port_topic_shape(self):
        ns = TopicNamespace("a/b")
        assert ns.port_topic("c1") == "a/b/port/c1"
        assert ns.command_topics == ("a/b/set", "a/b/port", "a/b/ble")

    def test_trailing_slash_normalised(self):
        cfg = _cfg(devices=[make_device("desk", topic_prefix="cuktech/desk/")])
        specs = cfg.device_specs()
        assert cfg.topics_for(specs[1]).status == "cuktech/desk/status"
