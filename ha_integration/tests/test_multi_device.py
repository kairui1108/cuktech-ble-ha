"""多设备（一设备一 entry）行为测试。

覆盖 §4 的 P4 落地面：
  * per-entry topic 前缀：两个 entry 指向同一台 server 时订阅/发布/去重互不串台；
  * 老 entry（无 device_id / topic_prefix 键）行为与改造前逐字一致；
  * config flow 的设备发现三级回退（404/超时 → 单设备，1 台 → 自动选中，N 台 → 选择）；
  * 限额探测链的设备维度（§4.4）与 ESP32 兼容约束（无 /api/charge-limits 仍走 local）；
  * options flow 改前缀后 entry 重载生效。
"""
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_NAME

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from conftest import AsyncContextManager

from custom_components.cuktech_charger import CuktechMQTTCoordinator
from custom_components.cuktech_charger.const import (
    API_DEVICES,
    CONF_DEVICE_ID,
    CONF_SERVER_URL,
    CONF_TOPIC_PREFIX,
    LIMIT_BACKEND_LOCAL,
    LIMIT_BACKEND_SERVER,
    LIMIT_MODE_ALWAYS,
    TOPIC_PREFIX,
)

SERVER = "http://ble:8199"

# 本 entry 的完整 topic 集（叶子形状固定，只有前缀随 entry 变）
LEAVES = ("port", "settings", "status", "set", "charge_event", "ble")


def make_entry(entry_id: str, data: dict, title: str | None = None):
    """构造一个玩具 config entry（含 options，与真实 ConfigEntry 形状一致）。"""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = dict(data)
    entry.options = {}
    entry.title = data.get("name", "CUKTECH Charger") if title is None else title
    return entry


def primary_entry():
    """primary 设备的 entry：没有 device_id，也没有 topic_prefix（运行时回退）。"""
    return make_entry("entry_primary", {"server_url": SERVER}, title="客厅充电器")


def desk_entry(prefix: str = "cuktech/charger/desk", device_id: str = "desk"):
    return make_entry(
        "entry_desk",
        {"server_url": SERVER, "device_id": device_id, "topic_prefix": prefix},
        title="书桌充电器",
    )


def two_coordinators(hass):
    return (
        CuktechMQTTCoordinator(hass, primary_entry()),
        CuktechMQTTCoordinator(hass, desk_entry()),
    )


# ── HTTP / MQTT 测试替身 ──


def _resp(status=200, payload=None):
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=b"")
    if payload is not None:
        resp.json = AsyncMock(return_value=payload)
    else:
        resp.json = AsyncMock(side_effect=Exception("no json"))
    return resp


def _session(responses=None, exc=None):
    """session 替身：GET 依序返回 responses（每项被 AsyncContextManager 包一层）。"""
    session = MagicMock()
    if exc is not None:
        session.get = MagicMock(side_effect=exc)
    else:
        session.get = MagicMock(
            side_effect=[AsyncContextManager(r) for r in (responses or [])]
        )
    return session


def _get_urls(session) -> list[str]:
    return [call.args[0] for call in session.get.call_args_list]


def _mqtt_mock(subscribed=None):
    """MQTT 模块替身：记录订阅主题（可选），发布成功。"""
    mock_mqtt = MagicMock()
    mock_mqtt.async_wait_for_mqtt_client = AsyncMock(return_value=True)
    mock_mqtt.async_publish = AsyncMock()

    async def _subscribe(_hass, topic, _cb):
        if subscribed is not None:
            subscribed.append(topic)
        return MagicMock()

    mock_mqtt.async_subscribe = AsyncMock(side_effect=_subscribe)
    return mock_mqtt


async def run_setup(coord) -> list[str]:
    """跑一次真实 async_setup（订阅 + 健康检查 + 限额探测），返回订阅到的主题。

    服务端不可达 → 健康检查失败、限额落 local，这正是 ESP32/离线场景的路径。
    """
    topics: list[str] = []
    with patch(
        'custom_components.cuktech_charger.mqtt', _mqtt_mock(topics)
    ), patch(
        'custom_components.cuktech_charger.async_get_clientsession',
        return_value=_session(exc=Exception("unreachable")),
    ):
        await coord.async_setup()
    return topics


class TestPerEntryTopics:
    """一设备一 entry：前缀、订阅、发布、去重全部按 entry 隔离。"""

    def test_topic_attributes_are_per_entry(self, mock_hass):
        primary, desk = two_coordinators(mock_hass)
        assert primary.topic_prefix == TOPIC_PREFIX
        assert desk.topic_prefix == "cuktech/charger/desk"
        assert primary.topic_port == "cuktech/charger/port"
        assert primary.topic_settings == "cuktech/charger/settings"
        assert primary.topic_status == "cuktech/charger/status"
        assert primary.topic_set == "cuktech/charger/set"
        assert primary.topic_charge_event == "cuktech/charger/charge_event"
        assert primary.topic_ble == "cuktech/charger/ble"
        assert desk.topic_port == "cuktech/charger/desk/port"
        assert desk.topic_settings == "cuktech/charger/desk/settings"
        assert desk.topic_status == "cuktech/charger/desk/status"
        assert desk.topic_set == "cuktech/charger/desk/set"
        assert desk.topic_charge_event == "cuktech/charger/desk/charge_event"
        assert desk.topic_ble == "cuktech/charger/desk/ble"
        # 叶子形状不变（ESP32 固件零改动的前提）
        for leaf in LEAVES:
            assert getattr(desk, f"topic_{leaf}").endswith(f"/{leaf}")

    @pytest.mark.asyncio
    async def test_subscriptions_do_not_cross(self, mock_hass):
        primary, desk = two_coordinators(mock_hass)
        primary_topics = await run_setup(primary)
        desk_topics = await run_setup(desk)

        assert set(primary_topics) == {
            "cuktech/charger/port/c1",
            "cuktech/charger/port/c2",
            "cuktech/charger/port/c3",
            "cuktech/charger/port/a",
            "cuktech/charger/settings",
            "cuktech/charger/status",
            "cuktech/charger/charge_event",
        }
        assert set(desk_topics) == {
            "cuktech/charger/desk/port/c1",
            "cuktech/charger/desk/port/c2",
            "cuktech/charger/desk/port/c3",
            "cuktech/charger/desk/port/a",
            "cuktech/charger/desk/settings",
            "cuktech/charger/desk/status",
            "cuktech/charger/desk/charge_event",
        }
        # 零重叠：否则 A 的 status 会喂进 B 的实体
        assert not set(primary_topics) & set(desk_topics)

    @pytest.mark.asyncio
    async def test_publish_topics_do_not_cross(self, mock_hass):
        primary, desk = two_coordinators(mock_hass)
        published: list[str] = []
        mock_mqtt = _mqtt_mock()
        mock_mqtt.async_publish = AsyncMock(
            side_effect=lambda _hass, topic, _payload: published.append(topic)
        )
        with patch('custom_components.cuktech_charger.mqtt', mock_mqtt):
            for coord in (primary, desk):
                await coord.async_set_value(5, 1)
                await coord.async_port_control("c1", "off")
                await coord.async_enable_ble(True)

        assert published == [
            "cuktech/charger/set",
            "cuktech/charger/port",
            "cuktech/charger/ble",
            "cuktech/charger/desk/set",
            "cuktech/charger/desk/port",
            "cuktech/charger/desk/ble",
        ]

    def test_charge_event_dedup_is_per_entry(self, mock_hass):
        """同一事件落在两个 entry 上都必须触发；重投递各自去重。"""
        primary, desk = two_coordinators(mock_hass)
        cb_primary = MagicMock()
        cb_desk = MagicMock()
        primary.register_charge_event_callback(cb_primary)
        desk.register_charge_event_callback(cb_desk)

        msg = MagicMock()
        msg.topic = "cuktech/charger/charge_event"
        msg.payload = json.dumps({
            "event": "charge_end", "port": "c1", "session_id": 7,
            "end_time": "2026-01-01 12:00:00", "energy_wh": 12.0,
        }).encode()

        primary._on_charge_event(msg)
        desk._on_charge_event(msg)
        assert cb_primary.call_count == 1
        assert cb_desk.call_count == 1

        # 再投递一次：两个 entry 各自的去重键生效，不会重复触发
        primary._on_charge_event(msg)
        desk._on_charge_event(msg)
        assert cb_primary.call_count == 1
        assert cb_desk.call_count == 1

    def test_charge_limits_are_per_entry(self, mock_hass):
        """限额引擎与持久化 key 都按 entry 隔离（不许一台的限额管到另一台）。"""
        primary, desk = two_coordinators(mock_hass)
        primary._charge_limits.set_limit("c1", 30)
        assert primary.charge_limit_wh("c1") == 30.0
        assert desk.charge_limit_wh("c1") == 0.0
        assert primary._limits_store.key != desk._limits_store.key
        assert desk._limits_store.key.endswith(desk.entry.entry_id)

    def test_port_payloads_land_in_own_entry(self, mock_hass):
        """两个 entry 各吃自己的 port topic，端口数据不串。"""
        primary, desk = two_coordinators(mock_hass)
        msg = MagicMock()
        msg.topic = "cuktech/charger/desk/port/c1"
        msg.payload = json.dumps({"voltage": 20.0, "current": 1.5, "active": True}).encode()

        desk._on_port_message(msg)
        assert desk.port_data["1"]["voltage"] == 20.0
        assert primary.port_data == {}


class TestLegacyEntryUnchanged:
    """老 entry（无新键）→ 前缀回退缺省值，行为与改造前一致。"""

    def test_prefix_falls_back_to_module_default(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, primary_entry())
        assert coord.device_id is None
        assert coord.topic_prefix == TOPIC_PREFIX
        assert coord.topic_port == "cuktech/charger/port"
        assert coord.topic_settings == "cuktech/charger/settings"
        assert coord.topic_status == "cuktech/charger/status"
        assert coord.topic_set == "cuktech/charger/set"
        assert coord.topic_charge_event == "cuktech/charger/charge_event"
        assert coord.topic_ble == "cuktech/charger/ble"

    def test_legacy_entry_loader_key_still_honoured(self, mock_hass):
        """老 entry 只有 "host"（无 server_url / 无前缀键）时不出错也不换前缀。"""
        entry = make_entry("legacy", {"host": "http://192.168.1.50:8199", "mac": "AA:BB"})
        coord = CuktechMQTTCoordinator(mock_hass, entry)
        assert coord.server_url == "http://192.168.1.50:8199"
        assert coord.topic_prefix == TOPIC_PREFIX

    def test_magicmock_options_does_not_pollute_prefix(self, mock_hass, mock_entry):
        """回归：conftest 的 entry 是 MagicMock，options.get() 会返回 truthy MagicMock。

        直接用 entry.data.get(key) or entry.options.get(key) 会把前缀拼成乱码，
        所有老 entry 一起失联——entry_string() 只接受真正的字符串。
        """
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        assert coord.topic_prefix == TOPIC_PREFIX
        assert coord.topic_status == "cuktech/charger/status"

    @pytest.mark.asyncio
    async def test_legacy_entry_subscriptions_are_unchanged(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, primary_entry())
        topics = await run_setup(coord)
        # 与改造前的常量集合逐字相同（本测试就是"老 entry 行为不变"的锁）
        assert topics == [
            "cuktech/charger/port/c1",
            "cuktech/charger/port/c2",
            "cuktech/charger/port/c3",
            "cuktech/charger/port/a",
            "cuktech/charger/settings",
            "cuktech/charger/status",
            "cuktech/charger/charge_event",
        ]

    @pytest.mark.asyncio
    async def test_legacy_entry_publishes_legacy_topics(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, primary_entry())
        published: list[str] = []
        mock_mqtt = _mqtt_mock()
        mock_mqtt.async_publish = AsyncMock(
            side_effect=lambda _hass, topic, _payload: published.append(topic)
        )
        with patch('custom_components.cuktech_charger.mqtt', mock_mqtt):
            await coord.async_set_value(6, 2)
            await coord.async_port_control("c3", "on")
            await coord.async_enable_ble(False)
        assert published == [
            "cuktech/charger/set",
            "cuktech/charger/port",
            "cuktech/charger/ble",
        ]

    def test_legacy_entry_has_single_limit_url(self, mock_hass):
        """老 entry 的限额探测不额外打一发必然 404 的设备维度请求。"""
        coord = CuktechMQTTCoordinator(mock_hass, primary_entry())
        assert coord._limit_urls() == [f"{SERVER}/api/charge-limits"]

    @pytest.mark.asyncio
    async def test_legacy_entry_health_check_url_unchanged(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, primary_entry())
        session = _session([_resp(200, {"connected": True})])
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coord._async_health_check(None)
        assert _get_urls(session) == [f"{SERVER}/api/status"]


class TestDeviceScopedHttp:
    """绑定了设备的 entry 在 HTTP 上必须只问自己那台设备。"""

    @pytest.mark.asyncio
    async def test_health_check_targets_own_device(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _session([_resp(200, {"connected": True})])
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coord._async_health_check(None)
        assert _get_urls(session) == [f"{SERVER}/api/device/desk/status"]

    @pytest.mark.asyncio
    async def test_ble_http_fallback_targets_own_device(self, mock_hass):
        """MQTT 发不出去时（broker 掉线）走 HTTP，也必须打自己那台设备。"""
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        mock_mqtt = _mqtt_mock()
        mock_mqtt.async_publish = AsyncMock(side_effect=Exception("broker down"))
        session = MagicMock()
        session.post = MagicMock(return_value=AsyncContextManager(_resp(200)))
        with patch('custom_components.cuktech_charger.mqtt', mock_mqtt), patch(
            'custom_components.cuktech_charger.async_get_clientsession',
            return_value=session,
        ):
            assert await coord.async_enable_ble(True) is True
        assert session.post.call_args.args[0] == f"{SERVER}/api/device/desk/enable"


class TestLimitProbeDeviceDimension:
    """§4.4：限额端点探测链带设备维度，且 ESP32 的 local 回退不动。"""

    def test_scoped_url_is_first(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        assert coord._limit_urls() == [
            f"{SERVER}/api/device/desk/charge-limits",
            f"{SERVER}/api/charge-limits",
        ]

    @pytest.mark.asyncio
    async def test_scoped_200_delegates_immediately(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _session([_resp(200, {"ok": True, "limits": {
            "c1": {"wh": 30.0, "mode": LIMIT_MODE_ALWAYS}}})])
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coord._async_probe_limit_backend()
        assert coord.limit_backend == LIMIT_BACKEND_SERVER
        assert _get_urls(session) == [f"{SERVER}/api/device/desk/charge-limits"]

    @pytest.mark.asyncio
    async def test_404_on_scoped_path_falls_back_to_legacy_endpoint(self, mock_hass):
        """新 server 上若有设备维度的路径就吃它；404 则降级到老端点（§4.4 第二级）。"""
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _session([
            _resp(404),
            _resp(200, {"ok": True, "limits": {"c1": {"wh": 12.0}}}),
        ])
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coord._async_probe_limit_backend()
        assert coord.limit_backend == LIMIT_BACKEND_SERVER
        assert coord.charge_limit_wh("c1") == 12.0
        assert _get_urls(session) == [
            f"{SERVER}/api/device/desk/charge-limits",
            f"{SERVER}/api/charge-limits",
        ]

    @pytest.mark.asyncio
    async def test_esp32_both_paths_404_stays_local_and_meters(self, mock_hass):
        """ESP32 兼容约束 1/2：没有 /api/charge-limits → local 引擎，且 V×I 照旧积分。"""
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _session([_resp(404), _resp(404)])
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coord._async_probe_limit_backend()
        assert coord.limit_backend == LIMIT_BACKEND_LOCAL

        # local 模式下 _async_ingest_port 双后端同款计量（约束 2 不动）
        coord._async_ingest_port(1, {"voltage": 20.0, "current": 2.0, "active": True})
        coord.hass.loop.time.return_value = 1010.0
        coord._async_ingest_port(1, {"voltage": 20.0, "current": 2.0, "active": True})
        assert coord.session_energy_wh("c1") > 0

    @pytest.mark.asyncio
    async def test_timeout_does_not_retry_other_url(self, mock_hass):
        """超时换 URL 也不会好：只试一次就落 local，不拖慢启动探测。"""
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _session(exc=Exception("timeout"))
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coord._async_probe_limit_backend()
        assert coord.limit_backend == LIMIT_BACKEND_LOCAL
        assert len(_get_urls(session)) == 1

    @pytest.mark.asyncio
    async def test_post_write_uses_scoped_path(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        coord._limit_backend = LIMIT_BACKEND_SERVER
        session = MagicMock()
        session.post = MagicMock(
            return_value=AsyncContextManager(
                _resp(200, {"ok": True, "limits": {"c1": {"wh": 40.0}}})
            )
        )
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coord.async_set_charge_limit("c1", 40)
        assert session.post.call_args.args[0] == f"{SERVER}/api/device/desk/charge-limits"


# ── config flow ──


def _flow(hass, context=None):
    from custom_components.cuktech_charger.config_flow import ConfigFlow

    flow = ConfigFlow.__new__(ConfigFlow)
    flow.hass = hass
    flow.context = context or {}
    flow._pending = {}
    flow._devices = []
    flow.async_create_entry = MagicMock(
        return_value={"type": "create_entry", "title": "", "data": {}}
    )
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow.async_abort = MagicMock(return_value={"type": "abort"})
    return flow


def _created(flow) -> dict:
    """返回 last async_create_entry 的 (title, data)。"""
    kwargs = flow.async_create_entry.call_args.kwargs
    return kwargs


def _device(dev_id, name, mac="AA:BB:CC:DD:EE:FF", connected=True, prefix=""):
    return {
        "id": dev_id, "name": name, "mac": mac,
        "connected": connected, "topic_prefix": prefix,
    }


def _devices_payload(devices):
    return _resp(200, {"ok": True, "devices": devices})


class TestConfigFlowDiscovery:
    """§4.2：设备发现 + 前缀选择，含 ESP32/旧 server 的回退。"""

    @pytest.mark.asyncio
    async def test_devices_404_falls_back_to_single_device_flow(self):
        """/api/devices 404（ESP32、旧 server）→ 现有单设备流程，且存缺省前缀。"""
        flow = _flow(MagicMock())
        session = _session([_resp(200, {"connected": True}), _resp(404)])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            await flow.async_step_user(
                {"name": "ESP32 充电器", "server_url": "http://esp32.local"}
            )
        data = _created(flow)["data"]
        assert data["name"] == "ESP32 充电器"
        assert data["server_url"] == "http://esp32.local"
        assert data[CONF_TOPIC_PREFIX] == TOPIC_PREFIX
        assert CONF_DEVICE_ID not in data
        assert _created(flow)["title"] == "ESP32 充电器"

    @pytest.mark.asyncio
    async def test_discovery_timeout_falls_back_to_single_device_flow(self):
        flow = _flow(MagicMock())
        session = MagicMock()
        session.get = MagicMock(side_effect=[
            AsyncContextManager(_resp(200, {"connected": True})),
            Exception("timeout"),
        ])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            await flow.async_step_user(
                {"name": "Charger", "server_url": SERVER}
            )
        assert CONF_DEVICE_ID not in _created(flow)["data"]
        assert _created(flow)["data"][CONF_TOPIC_PREFIX] == TOPIC_PREFIX

    @pytest.mark.asyncio
    async def test_unparsable_discovery_body_falls_back(self):
        """200 但不是 JSON（例如反代塞了个登录页）→ 当"发现不了"处理，不炸流程。"""
        flow = _flow(MagicMock())
        bad = _resp(200)
        bad.json = AsyncMock(side_effect=ValueError("not json"))
        session = _session([_resp(200, {"connected": True}), bad])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            await flow.async_step_user({"name": "Charger", "server_url": SERVER})
        assert CONF_DEVICE_ID not in _created(flow)["data"]

    @pytest.mark.asyncio
    async def test_user_typed_prefix_used_without_discovery(self):
        """改过 NVS 前缀的 ESP32 用户手填前缀即可。"""
        flow = _flow(MagicMock())
        session = _session([_resp(200, {"connected": True}), _resp(404)])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            await flow.async_step_user({
                "name": "书房", "server_url": "http://esp32.local",
                CONF_TOPIC_PREFIX: "home/charger/study",
            })
        assert _created(flow)["data"][CONF_TOPIC_PREFIX] == "home/charger/study"

    @pytest.mark.asyncio
    async def test_single_device_is_auto_selected(self):
        """200 且只有 1 台 → 自动选中，前缀取服务端报的（而不是用户手填的）。"""
        flow = _flow(MagicMock())
        session = _session([
            _resp(200, {"connected": True}),
            _devices_payload([_device("default", "客厅充电器", prefix=TOPIC_PREFIX)]),
        ])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            await flow.async_step_user(
                {"name": "手填的名字", "server_url": SERVER,
                 CONF_TOPIC_PREFIX: "手填的前缀"}
            )
        data = _created(flow)["data"]
        assert data[CONF_DEVICE_ID] == "default"
        assert data[CONF_TOPIC_PREFIX] == TOPIC_PREFIX
        assert _created(flow)["title"] == "客厅充电器"
        flow.async_show_form.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_devices_prompt_selection_then_entry(self):
        """200 且 3 台 → 进设备选择步骤，选中后 entry 落 device_id/topic_prefix。"""
        flow = _flow(MagicMock())
        session = _session([
            _resp(200, {"connected": True}),
            _devices_payload([
                _device("default", "客厅充电器", prefix=TOPIC_PREFIX),
                _device("desk", "书桌充电器", prefix="cuktech/charger/desk"),
                _device("study", "书房充电器", connected=False,
                        prefix="cuktech/charger/study"),
            ]),
        ])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            form = await flow.async_step_user({"name": "x", "server_url": SERVER})
        flow.async_create_entry.assert_not_called()
        assert form["step_id"] == "device"
        schema_keys = set(form["data_schema"])
        assert schema_keys == {CONF_DEVICE_ID}

        result = await flow.async_step_device({CONF_DEVICE_ID: "desk"})
        assert result["type"] == "create_entry"
        data = _created(flow)["data"]
        assert data[CONF_DEVICE_ID] == "desk"
        assert data[CONF_TOPIC_PREFIX] == "cuktech/charger/desk"
        assert data["server_url"] == SERVER
        assert _created(flow)["title"] == "书桌充电器"

    @pytest.mark.asyncio
    async def test_stale_selection_reruns_discovery(self):
        """选中的设备在提交时已不在列表里（服务端换了配置）→ 重新发现并让用户再选。"""
        flow = _flow(MagicMock())
        flow._devices = [_device("a", "A")]
        flow._pending = {"name": "x", "server_url": SERVER}
        session = _session([_devices_payload([_device("a", "A")])])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            form = await flow.async_step_device({CONF_DEVICE_ID: "b"})
        flow.async_create_entry.assert_not_called()
        assert form["step_id"] == "device"
        assert form["errors"]["base"] == "device_not_found"
        assert [d["id"] for d in flow._devices] == ["a"]

    @pytest.mark.asyncio
    async def test_lost_discovery_restarts_from_first_step(self):
        """重新发现也拿不到设备列表 → 回第一步（而不是展示空下拉框）。"""
        flow = _flow(MagicMock())
        flow._devices = [_device("a", "A")]
        flow._pending = {"name": "x", "server_url": SERVER}
        session = _session([_resp(404)])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            form = await flow.async_step_device({CONF_DEVICE_ID: "b"})
        flow.async_create_entry.assert_not_called()
        assert form["step_id"] == "user"
        assert form["errors"]["base"] == "cannot_connect"

    @pytest.mark.asyncio
    async def test_unique_id_contains_md5_of_url_and_prefix(self):
        flow = _flow(MagicMock())
        prefix = "cuktech/charger/desk"
        session = _session([
            _resp(200, {"connected": True}),
            _devices_payload([
                _device("default", "客厅", prefix=TOPIC_PREFIX),
                _device("desk", "书桌", prefix=prefix),
            ]),
        ])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            await flow.async_step_user({"name": "x", "server_url": SERVER})
            await flow.async_step_device({CONF_DEVICE_ID: "desk"})
        expected = "cuktech_" + hashlib.md5(
            f"{SERVER}|{prefix}".encode()).hexdigest()[:16]
        assert flow.async_set_unique_id.call_args.args[0] == expected

    @pytest.mark.asyncio
    async def test_unique_id_for_single_device_uses_default_prefix(self):
        flow = _flow(MagicMock())
        session = _session([_resp(200, {"connected": True}), _resp(404)])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            await flow.async_step_user({"name": "x", "server_url": SERVER})
        expected = "cuktech_" + hashlib.md5(
            f"{SERVER}|{TOPIC_PREFIX}".encode()).hexdigest()[:16]
        assert flow.async_set_unique_id.call_args.args[0] == expected

    @pytest.mark.asyncio
    async def test_device_step_without_discovery_returns_to_user_step(self):
        """流程被从中间恢复但发现结果已丢（HA 重启）→ 回第一步，不崩在空下拉框。"""
        flow = _flow(MagicMock())
        form = await flow.async_step_device(None)
        assert form["step_id"] == "user"

    def test_device_label_shows_identity_fields(self):
        """选择项标签必须带 name/mac/连接状态/前缀——同型号两台靠名称选不出来。"""
        from custom_components.cuktech_charger.config_flow import _device_label

        label = _device_label(_device("desk", "书桌充电器",
                                      mac="AA:BB:CC:DD:EE:FF",
                                      connected=False,
                                      prefix="cuktech/charger/desk"))
        assert "书桌充电器" in label
        assert "AA:BB:CC:DD:EE:FF" in label
        assert "未连接" in label
        assert "cuktech/charger/desk" in label
        # 服务端没报前缀时回退缺省值，用户不至于看到一个空尾巴
        assert TOPIC_PREFIX in _device_label(_device("x", "X", prefix=""))


# ── options flow ──


def _options_flow(hass, entry):
    from custom_components.cuktech_charger.config_flow import CuktechOptionsFlow

    flow = CuktechOptionsFlow(config_entry=entry)
    flow.hass = hass
    flow.async_create_entry = MagicMock(
        return_value={"type": "create_entry", "title": "", "data": {}}
    )
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})
    return flow


def _hass_with_entry_updates(hass):
    """让 async_update_entry 真的写回 entry（真实 HA 就是这样），reload 可断言。"""
    hass.config_entries = MagicMock()
    hass.config_entries.async_reload = AsyncMock()

    def _update(entry, **kwargs):
        if "data" in kwargs:
            entry.data = kwargs["data"]
        if "options" in kwargs:
            entry.options = kwargs["options"]
        if "title" in kwargs:
            entry.title = kwargs["title"]

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update)
    return hass


class TestOptionsFlow:
    """§4.5：不删 entry 也能改前缀（删了重加会换 entry_id，实体历史全断）。"""

    @pytest.mark.asyncio
    async def test_init_form_offers_prefix_and_device_id(self, mock_hass):
        """§4.5：前缀 / server_url / 显示名 / device_id 都能改，一个都不能少。

        少了 server_url 和显示名，"换网关"和"起个好认的名字"就只能删了重加，
        而重加会换 entry_id → 实体 unique_id 全变 → 历史与自动化断掉。
        """
        entry = desk_entry()
        flow = _options_flow(mock_hass, entry)
        form = await flow.async_step_init(None)
        assert form["step_id"] == "init"
        assert set(form["data_schema"]) == {
            CONF_NAME, CONF_SERVER_URL, CONF_TOPIC_PREFIX, CONF_DEVICE_ID,
        }

    @pytest.mark.asyncio
    async def test_saving_can_change_server_url_and_display_name(self, mock_hass):
        hass = _hass_with_entry_updates(mock_hass)
        entry = desk_entry()
        entry.title = "书桌充电器"
        flow = _options_flow(hass, entry)

        await flow.async_step_init({
            CONF_NAME: "书房充电器",
            CONF_SERVER_URL: "http://ble2:8199",
            CONF_TOPIC_PREFIX: "cuktech/charger/study",
            CONF_DEVICE_ID: "study",
        })

        assert entry.data[CONF_SERVER_URL] == "http://ble2:8199"
        assert entry.title == "书房充电器", "显示名走 title，不进 data"
        # 设备名即时生效于 device_info（实体上的 name 属性）
        from custom_components.cuktech_charger.sensor import CuktechPortSensor
        coord = CuktechMQTTCoordinator(hass, entry)
        sensor = CuktechPortSensor(coord, entry, 1, "C1", "voltage")
        assert sensor.device_info["name"] == "书房充电器"

    @pytest.mark.asyncio
    async def test_blank_server_url_keeps_current_value(self, mock_hass):
        """回归：留空不能把 server_url 从 entry 里删掉（否则静默指向缺省地址）。"""
        hass = _hass_with_entry_updates(mock_hass)
        entry = desk_entry()
        flow = _options_flow(hass, entry)
        await flow.async_step_init({CONF_TOPIC_PREFIX: "cuktech/charger/desk",
                                    CONF_DEVICE_ID: "desk", CONF_SERVER_URL: "  "})
        assert entry.data[CONF_SERVER_URL] == SERVER

    @pytest.mark.asyncio
    async def test_saving_updates_entry_and_reloads(self, mock_hass):
        hass = _hass_with_entry_updates(mock_hass)
        entry = desk_entry()
        flow = _options_flow(hass, entry)

        await flow.async_step_init({
            CONF_TOPIC_PREFIX: "cuktech/charger/study",
            CONF_DEVICE_ID: "study",
        })

        assert entry.data[CONF_TOPIC_PREFIX] == "cuktech/charger/study"
        assert entry.data[CONF_DEVICE_ID] == "study"
        hass.config_entries.async_update_entry.assert_called_once()
        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)

        # 新前缀立即生效（reload 之后 coordinator 会按新值重新订阅）
        coord = CuktechMQTTCoordinator(hass, entry)
        assert coord.topic_port == "cuktech/charger/study/port"
        assert coord.device_id == "study"

    @pytest.mark.asyncio
    async def test_blank_prefix_restores_default(self, mock_hass):
        hass = _hass_with_entry_updates(mock_hass)
        entry = desk_entry()
        flow = _options_flow(hass, entry)
        await flow.async_step_init({CONF_TOPIC_PREFIX: "   ", CONF_DEVICE_ID: "desk"})
        assert entry.data[CONF_TOPIC_PREFIX] == TOPIC_PREFIX

    @pytest.mark.asyncio
    async def test_blank_device_id_unbinds_device(self, mock_hass):
        """留空 = 解除设备绑定（回到 ESP32/旧 server 的寻址方式）。"""
        hass = _hass_with_entry_updates(mock_hass)
        entry = desk_entry()
        flow = _options_flow(hass, entry)
        await flow.async_step_init({CONF_TOPIC_PREFIX: "cuktech/charger/desk",
                                    CONF_DEVICE_ID: ""})
        assert CONF_DEVICE_ID not in entry.data
        assert CuktechMQTTCoordinator(hass, entry).device_id is None


# ── 设备命名 ──


class TestDeviceNaming:
    """§4.5：设备名取 entry.title，实体 unique_id 仍带 entry_id 前缀。"""

    def test_device_info_name_is_entry_title(self, mock_hass):
        from custom_components.cuktech_charger.sensor import CuktechPortSensor

        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        entry = desk_entry()
        sensor = CuktechPortSensor(coord, entry, 1, "C1", "voltage")
        assert sensor.device_info["name"] == "书桌充电器"
        assert sensor.device_info["identifiers"] == {
            ("cuktech_charger", entry.entry_id)
        }
        # unique_id 契约不变：多个 entry 靠 entry_id 前缀天然不冲突
        assert sensor._attr_unique_id == f"{entry.entry_id}_port_1_voltage"

    def test_device_info_falls_back_when_title_missing(self, mock_hass):
        from custom_components.cuktech_charger.const import DEVICE_INFO
        from custom_components.cuktech_charger.sensor import CuktechPortSensor

        entry = make_entry("e1", {"server_url": SERVER}, title="")
        coord = CuktechMQTTCoordinator(mock_hass, entry)
        sensor = CuktechPortSensor(coord, entry, 1, "C1", "voltage")
        assert sensor.device_info["name"] == DEVICE_INFO["name"]


# ── 设备名自动同步（§4.5）──


def _sync_session(devices):
    """GET 替身：/api/devices 给设备表，其余端点给一份"连上了"的答复。

    用函数式 side_effect 而不是响应列表：setup 期间会打好几个 GET
    （设备发现 / 健康检查 / 限额探测），按顺序摆列表会让用例跟着实现顺序碎掉。
    """
    session = MagicMock()

    def _get(url, **_kwargs):
        if str(url).endswith(API_DEVICES):
            return AsyncContextManager(_resp(200, {"ok": True, "devices": devices}))
        return AsyncContextManager(_resp(200, {"connected": True, "limits": {}}))

    session.get = MagicMock(side_effect=_get)
    session.post = MagicMock(return_value=AsyncContextManager(_resp(200, {"limits": {}})))
    return session


def _entry(data: dict, title: str):
    """带指定"当前显示名"的 entry：用来验证"改名才写 entry"。"""
    return make_entry("entry_x", data, title=title)


def _device_registry(device_id="dev1", name_by_user=None):
    """设备注册表替身：每次调用都先清记录（桩是模块级 MagicMock，会跨用例累积）。"""
    from types import SimpleNamespace

    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get.return_value
    registry.async_update_device.reset_mock()
    registry.async_get_device.reset_mock()
    registry.async_get_device.return_value = SimpleNamespace(
        id=device_id, name_by_user=name_by_user)
    return registry


class TestDeviceNameSync:
    """服务端改了 `devices[].name`，HA 不需要手动重命名 entry。

    设备名有两个来源（服务端配置 / HA entry title），以前只在"添加集成那一刻"
    对得上：之后在 Web 里改名，HA 卡片还是老名字；两台同型号充电器只能靠 `_2`
    后缀区分。现在每次 setup 对齐一次。
    """

    async def test_server_rename_updates_entry_title(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _sync_session([
            _device("default", "客厅充电器", prefix=TOPIC_PREFIX),
            _device("desk", "书桌充电器 2 号", prefix="cuktech/charger/desk"),
        ])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()
        mock_hass.config_entries.async_update_entry.assert_called_once()
        assert mock_hass.config_entries.async_update_entry.call_args.kwargs["title"] == "书桌充电器 2 号"

    async def test_adopts_own_device_not_the_primary(self, mock_hass):
        """多设备列表里必须按 device_id 取自己那台的名字。"""
        coord = CuktechMQTTCoordinator(mock_hass, _entry(
            {"server_url": SERVER, "device_id": "desk",
             "topic_prefix": "cuktech/charger/desk"}, title="CUKTECH Charger"))
        session = _sync_session([
            _device("default", "客厅充电器", prefix=TOPIC_PREFIX),
            _device("desk", "书桌充电器", prefix="cuktech/charger/desk"),
        ])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()
        title = mock_hass.config_entries.async_update_entry.call_args.kwargs["title"]
        assert title == "书桌充电器"

    async def test_unnamed_device_does_not_clobber_title(self, mock_hass):
        """服务端没配 name（上报的就是 id）→ 视为"没意见"，不覆盖用户起的名字。"""
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _sync_session([_device("desk", "desk", prefix="cuktech/charger/desk")])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()
        mock_hass.config_entries.async_update_entry.assert_not_called()

    async def test_same_name_is_a_noop(self, mock_hass):
        """名字没变就不写 entry（避免每次启动都触发一次 entry 更新）。"""
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _sync_session([_device("desk", "书桌充电器", prefix="cuktech/charger/desk")])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()
        mock_hass.config_entries.async_update_entry.assert_not_called()

    async def test_legacy_entry_syncs_only_when_unambiguous(self, mock_hass):
        """老 entry（无 device_id）：只有服务端刚好报一台时才同步，多台时保持原样。"""
        legacy = _entry({"server_url": SERVER}, title="CUKTECH Charger")
        coord = CuktechMQTTCoordinator(mock_hass, legacy)
        session = _sync_session([_device("default", "客厅充电器", prefix=TOPIC_PREFIX)])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()
        assert mock_hass.config_entries.async_update_entry.call_args.kwargs["title"] == "客厅充电器"

        mock_hass.config_entries.async_update_entry.reset_mock()
        coord2 = CuktechMQTTCoordinator(
            mock_hass, _entry({"server_url": SERVER}, title="CUKTECH Charger"))
        session2 = _sync_session([
            _device("default", "客厅充电器", prefix=TOPIC_PREFIX),
            _device("desk", "书桌充电器", prefix="cuktech/charger/desk"),
        ])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session2,
        ):
            await coord2._async_sync_device_name()
        mock_hass.config_entries.async_update_entry.assert_not_called()

    async def test_pinned_title_stays_while_device_name_still_syncs(self, mock_hass):
        """两件事分开：条目名（中枢那层）固定不动，设备名照旧跟着服务端。

        用户"给这个集成起个好记的名字"和"设备叫什么"是两个诉求，绑死会让固定
        条目名顺带把设备名也冻住——那正是上一版实现的毛病。
        """
        from custom_components.cuktech_charger.const import CONF_NAME_PINNED

        registry = _device_registry()

        entry = _entry({"server_url": SERVER, "device_id": "desk",
                        "topic_prefix": "cuktech/charger/desk",
                        CONF_NAME_PINNED: True}, title="我的书桌充电器")
        coord = CuktechMQTTCoordinator(mock_hass, entry)
        session = _sync_session([_device("desk", "服务端改的名字",
                                        prefix="cuktech/charger/desk")])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()
        mock_hass.config_entries.async_update_entry.assert_not_called()   # 条目名固定
        assert coord.display_name == "服务端改的名字"                     # 设备名同步
        registry.async_update_device.assert_called_once_with("dev1", name="服务端改的名字")

    async def test_registry_device_is_renamed_too(self, mock_hass):
        """设备卡片上的名字也要跟着变 —— 只改 entry title 的话列表里还是旧名。"""
        registry = _device_registry()

        coord = CuktechMQTTCoordinator(mock_hass, _entry(
            {"server_url": SERVER, "device_id": "desk",
             "topic_prefix": "cuktech/charger/desk"}, title="CUKTECH Charger"))
        session = _sync_session([_device("desk", "书桌充电器",
                                        prefix="cuktech/charger/desk")])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()
        registry.async_update_device.assert_called_once_with("dev1", name="书桌充电器")

    async def test_user_renamed_device_is_not_overwritten_in_registry(self, mock_hass):
        """用户在设备页改过的名字（name_by_user）优先于自动同步。"""
        registry = _device_registry(name_by_user="我自己起的名字")

        coord = CuktechMQTTCoordinator(mock_hass, _entry(
            {"server_url": SERVER, "device_id": "desk",
             "topic_prefix": "cuktech/charger/desk"}, title="CUKTECH Charger"))
        session = _sync_session([_device("desk", "书桌充电器",
                                        prefix="cuktech/charger/desk")])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()
        registry.async_update_device.assert_not_called()

    async def test_entity_device_info_uses_synced_name(self, mock_hass):
        """实体所属设备的显示名 = 同步到的设备名（而不是 entry title）。"""
        from custom_components.cuktech_charger.sensor import CuktechPortSensor

        entry = _entry({"server_url": SERVER, "device_id": "desk",
                        "topic_prefix": "cuktech/charger/desk",
                        "name_pinned": True}, title="我的书桌充电器")
        coord = CuktechMQTTCoordinator(mock_hass, entry)
        sensor = CuktechPortSensor(coord, entry, 1, "C1", "voltage")
        assert sensor.device_info["name"] == "我的书桌充电器", "没同步过时回落到 title"

        session = _sync_session([_device("desk", "书桌充电器",
                                        prefix="cuktech/charger/desk")])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()
        assert sensor.device_info["name"] == "书桌充电器", "同步后设备名跟着服务端"

    async def test_periodic_sync_is_registered_and_cancelled(self, mock_hass):
        """周期对齐要挂在 setup 上、并在 unload 时注销。

        没注销 = 每次 reload 多一个定时器（HA 里最经典的一种慢性泄漏），
        而它的症状是"越跑越慢"，跟"改名同步"看起来毫无关系。
        """
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _sync_session([_device("desk", "书桌充电器",
                                        prefix="cuktech/charger/desk")])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ), patch('custom_components.cuktech_charger.mqtt', _mqtt_mock()):
            await coord.async_setup()
        unsub = coord._name_sync_unsub
        assert unsub is not None, "setup 必须挂上周期对齐任务"

        await coord.async_unload()
        # 只断言"调用过"：MagicMock 连 `if unsub:` 求值时的 __bool__ 也记成一次
        # call，用 assert_called_once 会因为多出来的那条记录假红。
        assert unsub.called, "unload 必须注销周期任务"
        assert coord._name_sync_unsub is None

    async def test_sync_failure_never_blocks_setup(self, mock_hass):
        """发现端点不可用（ESP32 / 旧 server / 掉线）只是"没有新名字"，不是故障。"""
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = MagicMock()
        session.get = MagicMock(side_effect=Exception("unreachable"))
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ):
            await coord._async_sync_device_name()      # 不抛
        mock_hass.config_entries.async_update_entry.assert_not_called()

    async def test_runs_during_setup_before_platforms(self, mock_hass):
        """必须挂在 setup 上（而不是靠用户手动触发），否则只有重载才生效。"""
        coord = CuktechMQTTCoordinator(mock_hass, _entry(
            {"server_url": SERVER, "device_id": "desk",
             "topic_prefix": "cuktech/charger/desk"}, title="CUKTECH Charger"))
        session = _sync_session([_device("desk", "书桌充电器", prefix="cuktech/charger/desk")])
        with patch(
            'custom_components.cuktech_charger.config_flow.async_get_clientsession',
            return_value=session,
        ), patch('custom_components.cuktech_charger.mqtt', _mqtt_mock()):
            await coord.async_setup()
        assert mock_hass.config_entries.async_update_entry.call_args.kwargs["title"] == "书桌充电器"

    @pytest.mark.asyncio
    async def test_unnamed_device_gets_a_distinguishable_title(self):
        """服务端没给设备配 `name` 时，两台不能都叫同一个默认名。"""
        from custom_components.cuktech_charger.config_flow import DEFAULT_NAME

        flow = _flow(MagicMock())
        session = _session([
            _resp(200, {"connected": True}),
            _devices_payload([_device("desk", "desk"), _device("bed", "bed")]),
        ])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            await flow.async_step_user({"name": "x", "server_url": SERVER})
        await flow.async_step_device({CONF_DEVICE_ID: "desk"})
        assert _created(flow)["title"] == f"{DEFAULT_NAME} (desk)"

    @pytest.mark.asyncio
    async def test_unnamed_primary_keeps_the_plain_default_name(self):
        """单设备且服务端没配名字：显示名不要冒出 `(default)` 这种内部 id。"""
        from custom_components.cuktech_charger.config_flow import DEFAULT_NAME

        flow = _flow(MagicMock())
        session = _session([
            _resp(200, {"connected": True}),
            _devices_payload([_device("default", "default", prefix=TOPIC_PREFIX)]),
        ])
        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession',
                   return_value=session):
            await flow.async_step_user({"name": "x", "server_url": SERVER})
        assert _created(flow)["title"] == DEFAULT_NAME

    @pytest.mark.asyncio
    async def test_renaming_pins_the_name_and_clearing_unpins(self, mock_hass):
        """改名字要能被"记住"，清空名字要能交回自动同步 —— 两条都要能走通。"""
        from custom_components.cuktech_charger.const import CONF_NAME_PINNED

        hass = _hass_with_entry_updates(mock_hass)
        entry = desk_entry()
        entry.title = "书桌充电器"
        flow = _options_flow(hass, entry)
        await flow.async_step_init({
            CONF_NAME: "我的书桌充电器",
            CONF_SERVER_URL: SERVER,
            CONF_TOPIC_PREFIX: "cuktech/charger/desk",
            CONF_DEVICE_ID: "desk",
        })
        assert entry.title == "我的书桌充电器"
        assert entry.data.get(CONF_NAME_PINNED) is True, "用户起的名要被记下来"

        flow2 = _options_flow(hass, entry)
        await flow2.async_step_init({
            CONF_NAME: "",            # 清空 = 交回自动同步
            CONF_SERVER_URL: SERVER,
            CONF_TOPIC_PREFIX: "cuktech/charger/desk",
            CONF_DEVICE_ID: "desk",
        })
        assert not entry.data.get(CONF_NAME_PINNED)
        assert entry.title == "我的书桌充电器", "清空名称不该把显示名抹掉，只是交回同步"
