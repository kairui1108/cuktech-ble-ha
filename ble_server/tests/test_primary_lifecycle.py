"""主设备生命周期（§3.10）：换主设备 / 清空主设备 / 首次导入写进主设备。

为什么值得单独锁：primary 是配置文件里的 `ble:` 节，`device_id` 恒为 `default`
（legacy MQTT 主题、历史分区、HA unique_id 全都锚在它上面），所以它**不能删**。
但"不能删"不等于"不能换"——换机、云上残留一台同名设备、或者首次安装时把设备
导进了附加卡片，都会留下一个删不掉的"孤儿主设备"。统一数组（A 形）就是这两个
动作的入口，而它有两个容易写错的地方：

1. **换主设备**时两台设备的 `device_id` 全变了（default ↔ 新 id），前端又只能
   拿到打码的 token/ble_key；明文必须按 **MAC** 从旧文件里继承。按 id 继承会
   直接丢凭据（保存被 400 拦下，或者更糟：把上一台的 token 带到新设备上）。
2. **清空主设备**时，空值必须真的落盘成"没有"，不能被"空值 = 没改"的旧规则
   救回来——否则孤儿名字/凭据永远抹不掉。

这里的服务端测试不碰 BLE（golden_env 默认把 `BLEManager.start` 换成桩），
只锁"配置落盘契约"。
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import ha_server  # noqa: E402
from golden_harness import GOLDEN_MAC, GOLDEN_TOKEN, golden_env, mock_request  # noqa: E402

# 附加设备（golden_env 会把它写进 config.yaml 的 devices: 节）
DESK = {"id": "desk", "mac": "AA:BB:CC:DD:EE:02", "token": "11" * 16,
        "ble_key": "beefbeef"}


def _existing(*, with_desk=True):
    """盘上那份配置的内存形态（primary + 可选的一台 desk）。"""
    doc = {
        "ble": {"mac": GOLDEN_MAC, "token": GOLDEN_TOKEN,
                "ble_key": "cafecafe", "name": "客厅充电器", "scan_timeout": 10},
    }
    if with_desk:
        doc["devices"] = [{"id": DESK["id"], "mac": DESK["mac"],
                           "token": DESK["token"], "ble_key": DESK["ble_key"]}]
    return doc


class TestPromotePrimary:
    """把 desk 提成主设备：两张卡的 id 交换，明文凭据按 MAC 跟着设备走。"""

    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK]) as env:
            yield env

    async def test_promote_inherits_both_sides_credentials_by_mac(self, dual):
        existing = _existing()
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "书桌充电器", "mac": DESK["mac"],
             "token": "****abcd", "ble_key": "****ef01", "is_primary": True},
            {"id": "living", "name": "客厅充电器", "mac": GOLDEN_MAC,
             "token": "****c162", "ble_key": "****fecf", "is_primary": False},
        ])
        assert err is None, err

        # 新 primary = 原来 devices: 里的那台，明文从 devices: 节按 MAC 继承
        assert existing["ble"]["mac"] == DESK["mac"]
        assert existing["ble"]["token"] == DESK["token"]
        assert existing["ble"]["ble_key"] == DESK["ble_key"]
        assert existing["ble"]["name"] == "书桌充电器"
        # 降级下来的老 primary 变成普通附加设备，明文从 ble: 节按 MAC 继承
        assert existing["devices"] == [{
            "id": "living", "name": "客厅充电器", "mac": GOLDEN_MAC,
            "token": GOLDEN_TOKEN, "ble_key": "cafecafe",
        }]

    async def test_promote_with_empty_token_inherits_from_devices(self, dual):
        """页面载入的附加设备拿不到 token（接口不下发），换上来时提交的是空串。

        这和打码值是**同一条分支**：空串不等于"用户要清空凭据"，而是"页面没有明
        文"。必须按 MAC 从 devices: 节里把 token/BLE Key 继承回来，否则换一次主
        设备就要用户把 token 重敲一遍（而且敲错就认证到别的设备上）。
        """
        existing = _existing()
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "书桌充电器", "mac": DESK["mac"],
             "token": "", "ble_key": "", "is_primary": True},
            {"id": "living", "name": "客厅充电器", "mac": GOLDEN_MAC,
             "token": "****c162", "ble_key": "", "is_primary": False},
        ])
        assert err is None, err
        assert existing["ble"]["token"] == DESK["token"]
        assert existing["ble"]["ble_key"] == DESK["ble_key"]
        assert existing["devices"][0]["token"] == GOLDEN_TOKEN
        # 降级下来的那台也一样：页面不给附加设备下发 ble_key（空串），不继承就会
        # 在每次保存时把它的 BLE Key 从 YAML 里抹掉，下次启动认证失败。
        assert existing["devices"][0]["ble_key"] == "cafecafe"

    async def test_plain_save_keeps_extra_ble_key_when_page_omits_it(self, dual):
        """普通保存：页面永远不给附加设备下发 ble_key（空串），不能把它抹掉。

        这是 A 形最容易踩的一脚：`_DEVICE_KEYS` 过滤会把空串丢掉，若只对"打码值"
        做继承，YAML 里的 `ble_key` 就在每次保存时静默消失——设备下次启动认证失败，
        而用户什么都没改。
        """
        existing = _existing()
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "客厅充电器", "mac": GOLDEN_MAC,
             "token": "****c162", "ble_key": "****fecf", "is_primary": True},
            {"id": "desk", "name": "", "mac": DESK["mac"],
             "token": "****abcd", "ble_key": "", "is_primary": False},
        ])
        assert err is None, err
        saved = existing["devices"][0]
        assert saved["token"] == DESK["token"]
        assert saved["ble_key"] == DESK["ble_key"], "空串 ≠ 清空，必须继承旧 BLE Key"

    async def test_promote_does_not_leak_old_primary_ble_key(self, dual):
        """新 primary 自己没有 BLE Key 时，绝不能把上一台的 Key 带过去。"""
        existing = {
            "ble": {"mac": GOLDEN_MAC, "token": GOLDEN_TOKEN,
                    "ble_key": "cafecafe", "name": "客厅充电器"},
            "devices": [{"id": "desk", "mac": DESK["mac"], "token": DESK["token"]}],
        }
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "书桌充电器", "mac": DESK["mac"],
             "token": "****abcd", "ble_key": "****ef01", "is_primary": True},
            {"id": "living", "name": "客厅充电器", "mac": GOLDEN_MAC,
             "token": "****c162", "ble_key": "****fecf", "is_primary": False},
        ])
        assert err is None, err
        assert "ble_key" not in existing["ble"], \
            "desk 本来没有 BLE Key，换上来之后不该继承老 primary 的"
        assert existing["devices"][0]["ble_key"] == "cafecafe", \
            "被换下去的那台仍然应该拿到它自己的 BLE Key"

    async def test_promote_rejects_when_two_primaries(self, dual):
        err = dual.server._apply_devices_payload(_existing(), [
            {"id": "default", "mac": DESK["mac"], "is_primary": True},
            {"id": "living", "mac": GOLDEN_MAC, "is_primary": True},
        ])
        assert err and "primary" in err

    async def test_changed_mac_drops_credentials_it_cannot_verify(self, dual):
        """手改主设备 MAC：旧 token/ble_key 属于旧设备，不能再挂在它头上（§3.10.1）。

        "查不到 MAC"有两种来源：换了新设备、或 MAC 打错。两种都不能把上一台的凭据
        搬过来——否则保存下来的是"新设备 + 旧设备的 token"，认证失败会被误判成
        网络问题，而凭据本就不该跨设备复用。清空后用户重填或重新扫码导入即可。
        """
        existing = _existing(with_desk=False)
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "", "mac": "AA:BB:CC:DD:EE:99",
             "token": "****c162", "ble_key": "****fecf", "is_primary": True},
        ])
        assert err is None, err          # 单设备允许只有 MAC（还没填 token）
        assert existing["ble"]["mac"] == "AA:BB:CC:DD:EE:99"
        assert "token" not in existing["ble"], "不能把旧设备的 token 挂到新 MAC 上"
        assert "ble_key" not in existing["ble"]
        assert "name" not in existing["ble"]

    async def test_extra_mac_change_does_not_reuse_old_token(self, dual):
        """附加设备改了 MAC：按 id 继承会拿旧设备的 token → 必须拒绝而不是静默沿用。"""
        existing = _existing()
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "客厅充电器", "mac": GOLDEN_MAC,
             "token": "****c162", "ble_key": "", "is_primary": True},
            {"id": "desk", "mac": "AA:BB:CC:DD:EE:99",
             "token": "****abcd", "ble_key": "", "is_primary": False},
        ])
        assert err and "缺少 token" in err, err
        assert existing["devices"][0]["mac"] == DESK["mac"], "被拒的配置一个字节都不该改"

    async def test_extra_mac_unchanged_still_inherits(self, dual):
        """MAC 没变（普通编辑）：空/打码 token 仍按 id 继承，不逼用户重敲。"""
        existing = _existing()
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "客厅充电器", "mac": GOLDEN_MAC,
             "token": "****c162", "ble_key": "", "is_primary": True},
            {"id": "desk", "mac": DESK["mac"], "token": "", "ble_key": "",
             "is_primary": False},
        ])
        assert err is None, err
        assert existing["devices"][0]["token"] == DESK["token"]
        assert existing["devices"][0]["ble_key"] == DESK["ble_key"]


class TestClearPrimary:
    """清空主设备：孤儿主设备唯一能"去掉"的方式（primary 不能删除）。"""

    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK]) as env:
            yield env

    async def test_clear_single_device_wipes_identity(self, dual):
        existing = _existing(with_desk=False)
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "", "mac": "", "token": "", "ble_key": "",
             "is_primary": True, "clear_credentials": True},
        ])
        assert err is None, err
        ble = existing["ble"]
        assert "mac" not in ble and "token" not in ble
        assert "ble_key" not in ble and "name" not in ble
        assert "devices" not in existing

    async def test_clear_is_honoured_even_with_masked_values(self, dual):
        """页面上 token 一直是打码值：清空必须能穿透"打码 = 没改"的保护。"""
        existing = _existing(with_desk=False)
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "", "mac": "", "token": "****c162",
             "ble_key": "****fecf", "is_primary": True, "clear_credentials": True},
        ])
        assert err is None, err
        assert "token" not in existing["ble"]
        assert "ble_key" not in existing["ble"]

    async def test_clear_with_extras_is_rejected(self, dual):
        """还有附加设备时不能清空：附加设备必须挂在已配置的 primary 下（§3.10）。"""
        existing = _existing()
        err = dual.server._apply_devices_payload(existing, [
            {"id": "default", "name": "", "mac": "", "token": "", "is_primary": True,
             "clear_credentials": True},
            {"id": "desk", "mac": DESK["mac"], "token": "****abcd", "is_primary": False},
        ])
        assert err and "primary" in err

    async def test_cleared_primary_can_be_saved_end_to_end(self, dual, monkeypatch):
        """清空 + 保存 = 启动可接受：写盘后用生产解析器能读回来。"""
        cfg_path = Path(dual.config_path)
        before = yaml.safe_load(cfg_path.read_text())

        async def _no_restart():
            return None
        monkeypatch.setattr(dual.server, "_delayed_restart", _no_restart)

        payload = {"config": {
            "devices": [{"id": "default", "name": "", "mac": "", "token": "",
                         "ble_key": "", "is_primary": True, "clear_credentials": True}],
            # A 形是权威的：这里塞一份旧 ble 也不该赢
            "ble": {"mac": GOLDEN_MAC, "token": "****dead", "name": "MUST-NOT-WIN"},
        }}
        resp = await _post(dual.server, payload)
        assert resp.status == 200, resp.body

        saved = yaml.safe_load(cfg_path.read_text())
        assert saved["ble"].get("mac", "") == ""
        assert "MUST-NOT-WIN" not in yaml.safe_dump(saved)
        assert "devices" not in saved
        from config import config_from_mapping
        cfg = config_from_mapping(saved)          # 与启动同一个入口
        assert cfg.primary_device().mac == ""
        assert before["ble"]["mac"] == GOLDEN_MAC  # 前提：盘上原来是有配置的


class TestExtraOptionalFields:
    """附加设备的可选字段：网页保存不能把它们静默改写/抹掉。"""

    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK]) as env:
            yield env

    @staticmethod
    def _primary(**extra):
        return {"id": "default", "name": "客厅充电器", "mac": GOLDEN_MAC,
                "token": "****c162", "ble_key": "", "is_primary": True, **extra}

    @staticmethod
    def _desk(**extra):
        return {"id": "desk", "mac": DESK["mac"], "token": "****abcd",
                "ble_key": "", "is_primary": False, **extra}

    async def test_omitted_bemfa_overrides_are_inherited(self, dual):
        """页面/旧客户端不发 `bemfa_*` → 必须沿用 YAML 里的覆盖项。

        否则任意一次网页保存都会抹掉 `bemfa_c1` 这类不可编辑的覆盖项，巴法云
        实体 id 回落成派生值，用户配好的语音联动静默失效。
        """
        existing = _existing()
        existing["devices"][0]["bemfa_ble"] = "desk_voice"
        existing["devices"][0]["bemfa_c1"] = "desk_c1_voice"
        err = dual.server._apply_devices_payload(
            existing, [self._primary(), self._desk()])
        assert err is None, err
        saved = existing["devices"][0]
        assert saved["bemfa_ble"] == "desk_voice"
        assert saved["bemfa_c1"] == "desk_c1_voice"

    async def test_explicit_empty_bemfa_override_unpins(self, dual):
        """页面发来显式空串 = 用户解绑，回落派生 id（不是"没改"）。"""
        existing = _existing()
        existing["devices"][0]["bemfa_ble"] = "desk_voice"
        err = dual.server._apply_devices_payload(
            existing, [self._primary(), self._desk(bemfa_ble="")])
        assert err is None, err
        assert "bemfa_ble" not in existing["devices"][0]

    async def test_derived_topic_prefix_is_not_persisted(self, dual):
        """页面回传的运行期前缀（= 自动派生值）不能落成显式覆盖。

        落成显式覆盖后，再改 `mqtt.topic_prefix` 时附加设备不会跟着走——而用户
        从来没有手动设置过这一项。
        """
        existing = _existing()
        existing["mqtt"] = {"topic_prefix": "cuktech/charger"}
        err = dual.server._apply_devices_payload(
            existing, [self._primary(),
                       self._desk(topic_prefix="cuktech/charger/desk")])
        assert err is None, err
        assert "topic_prefix" not in existing["devices"][0]

    async def test_explicit_topic_prefix_is_kept(self, dual):
        """用户真的手填了不同前缀 → 必须落盘（这是显式覆盖）。"""
        existing = _existing()
        existing["mqtt"] = {"topic_prefix": "cuktech/charger"}
        err = dual.server._apply_devices_payload(
            existing, [self._primary(),
                       self._desk(topic_prefix="home/desk")])
        assert err is None, err
        assert existing["devices"][0]["topic_prefix"] == "home/desk"

    async def test_non_list_devices_returns_400(self, dual):
        """`devices` 不是数组 → 400 格式错误，而不是 500 TypeError。"""
        import json

        resp = await _post(dual.server, {"config": {"devices": 123}})
        assert resp.status == 400, resp.body
        assert "格式非法" in json.loads(resp.body)["error"]

    async def test_config_get_exposes_bemfa_overrides(self, dual):
        """`/api/config` 必须下发覆盖项，配置页才能在保存时原样带回。"""
        import json

        rt = dual.server.registry.get("desk")
        rt.spec.bemfa_names = {"ble": "desk_voice", "c1": "desk_c1_voice"}
        resp = await dual.server.handle_config_get(
            mock_request("GET", "/api/config"))
        devices = json.loads(resp.body)["config"]["devices"]
        desk = next(d for d in devices if d["device_id"] == "desk")
        assert desk["bemfa"] == {"ble": "desk_voice", "c1": "desk_c1_voice"}


class TestUnifiedPayloadRoundTrip:
    """A 形统一数组端到端：换主设备后落盘的文件必须能被启动入口吃下去。"""

    @pytest.fixture
    async def dual(self):
        async with golden_env(devices=[DESK]) as env:
            yield env

    async def test_promote_round_trip(self, dual, monkeypatch):
        cfg_path = Path(dual.config_path)

        async def _no_restart():
            return None
        monkeypatch.setattr(dual.server, "_delayed_restart", _no_restart)

        payload = {"config": {
            "devices": [
                {"id": "default", "name": "书桌充电器", "mac": DESK["mac"],
                 "token": "****abcd", "ble_key": "****ef01", "is_primary": True},
                {"id": "living", "name": "客厅充电器", "mac": GOLDEN_MAC,
                 "token": "****c162", "ble_key": "****fecf", "is_primary": False},
            ],
            "ble": {"mac": GOLDEN_MAC, "name": "MUST-NOT-WIN"},
        }}
        resp = await _post(dual.server, payload)
        assert resp.status == 200, resp.body

        saved = yaml.safe_load(cfg_path.read_text())
        assert saved["ble"]["mac"] == DESK["mac"]
        assert saved["ble"]["token"] == DESK["token"], "打码 token 要按 MAC 从 devices: 继承"
        assert saved["ble"]["name"] == "书桌充电器"
        assert "MUST-NOT-WIN" not in yaml.safe_dump(saved)
        # golden 配置里 primary 本来就没有 ble_key，降级下来的那台也不该凭空长出来
        assert saved["devices"] == [{
            "id": "living", "name": "客厅充电器", "mac": GOLDEN_MAC,
            "token": GOLDEN_TOKEN,
        }]

        from config import config_from_mapping
        cfg = config_from_mapping(saved)          # 启动入口（含 §3.10 全量校验）
        specs = cfg.device_specs()
        assert [s.device_id for s in specs] == ["default", "living"]
        assert [s.token for s in specs] == [DESK["token"], GOLDEN_TOKEN]

    async def test_first_use_import_into_empty_primary(self, dual, monkeypatch):
        """全新安装：主设备为空，第一台设备直接写进 primary（不再多出一张卡）。"""
        cfg_path = Path(dual.config_path)
        cfg_path.write_text(yaml.safe_dump({
            "ble": {"mac": "XX:XX:XX:XX:XX:XX", "token": ""},
            "mqtt": {"enabled": True, "topic_prefix": "cuktech/charger"},
            "bemfa": {"enabled": False},
            "server": {"history_db_path": str(dual.db_path)},
        }), encoding="utf-8")

        async def _no_restart():
            return None
        monkeypatch.setattr(dual.server, "_delayed_restart", _no_restart)

        resp = await _post(dual.server, {"config": {"devices": [
            {"id": "default", "name": "CUKTECH 10 GaN Charger Ultra",
             "mac": DESK["mac"], "token": DESK["token"], "ble_key": DESK["ble_key"],
             "is_primary": True},
        ]}})
        assert resp.status == 200, resp.body

        saved = yaml.safe_load(cfg_path.read_text())
        assert saved["ble"] == {"mac": DESK["mac"], "token": DESK["token"],
                                "ble_key": DESK["ble_key"],
                                "name": "CUKTECH 10 GaN Charger Ultra"}
        assert "devices" not in saved


async def _post(server, payload):
    req = mock_request("POST", "/api/config")

    async def _json():
        return payload
    req.json = _json
    return await server.handle_config_save(req)
