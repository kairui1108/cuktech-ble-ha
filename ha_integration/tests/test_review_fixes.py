"""OCR 评审（PR #16）带出的两条**只在真机上才会塌**的缺陷。

它们有一个共同点：本仓库的测试替身比真实环境宽松，于是测试全绿、真机失效。

1. `entry_string()` 用 `isinstance(source, dict)` 判断 entry 容器。真实 HA 的
   `ConfigEntry.data` / `.options` 是 `MappingProxyType`（不是 dict 子类），于是
   `device_id` / `topic_prefix` 恒取不到：两个 entry 订阅同一组 topic（A 的读数
   流进 B 的实体）、`/api/device/...` 全回落成 primary、options 表单永远显示默认值。
   测试里 entry 替身是普通 dict，所以这条路从来没被覆盖过。
2. 限额的 404 降级链对 GET 是对的（读错设备最多数值不对），但 `_async_limit_request`
   把 POST 也塞进同一条链：设备被删/改名后设备维度 URL 回 404，写请求降级到老端点，
   经 device_middleware 落到 primary —— 用户的限额被静默写进另一台充电器，还返回 200。
"""
import sys
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from conftest import AsyncContextManager

from custom_components.cuktech_charger import CuktechMQTTCoordinator
from custom_components.cuktech_charger.const import (
    CONF_DEVICE_ID,
    CONF_SERVER_URL,
    CONF_TOPIC_PREFIX,
    TOPIC_PREFIX,
    entry_string,
)

SERVER = "http://ble:8199"


def make_entry(entry_id: str, data: dict, *, frozen: bool = False):
    """玩具 entry。`frozen=True` 复刻真实 HA 的只读视图（MappingProxyType）。"""
    entry = MagicMock()
    entry.entry_id = entry_id
    container = MappingProxyType(dict(data)) if frozen else dict(data)
    entry.data = container
    entry.options = MappingProxyType({}) if frozen else {}
    return entry


def desk_entry(*, frozen: bool = False):
    return make_entry(
        "entry_desk",
        {"server_url": SERVER, "device_id": "desk",
         "topic_prefix": "cuktech/charger/desk"},
        frozen=frozen,
    )


def _resp(status=200, payload=None):
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=b"")
    if payload is not None:
        resp.json = AsyncMock(return_value=payload)
    else:
        resp.json = AsyncMock(side_effect=Exception("no json"))
    return resp


def _session(responses):
    session = MagicMock()
    session.post = MagicMock(
        side_effect=[AsyncContextManager(r) for r in responses]
    )
    return session


class TestEntryContainerType:
    """`entry_string` 必须认得真实 HA 的 MappingProxyType。"""

    def test_mapping_proxy_is_read(self):
        entry = desk_entry(frozen=True)
        assert entry_string(entry, CONF_DEVICE_ID) == "desk"
        assert entry_string(entry, CONF_TOPIC_PREFIX) == "cuktech/charger/desk"

    def test_mapping_proxy_entry_gets_its_own_topics(self, mock_hass):
        """容器类型判错的后果：两个 entry 抢同一组 topic（串台）。"""
        frozen = CuktechMQTTCoordinator(mock_hass, desk_entry(frozen=True))
        plain = CuktechMQTTCoordinator(mock_hass, desk_entry(frozen=False))
        assert frozen.topic_port == plain.topic_port == "cuktech/charger/desk/port"
        assert frozen.device_id == plain.device_id == "desk"
        assert frozen.topic_port != f"{TOPIC_PREFIX}/port"

    def test_missing_key_still_returns_none(self):
        """老 entry（无 device_id / topic_prefix）必须继续回退默认值。"""
        entry = make_entry("old", {"server_url": SERVER}, frozen=True)
        assert entry_string(entry, CONF_DEVICE_ID) is None
        assert entry_string(entry, CONF_TOPIC_PREFIX) is None

    def test_magicmock_entry_does_not_leak(self):
        """entry 替身是 MagicMock 时不能把 MagicMock 当成值（拼成乱码 topic）。"""
        entry = MagicMock()
        entry.data = MagicMock()
        entry.options = MagicMock()
        assert entry_string(entry, CONF_DEVICE_ID) is None


class TestWritePathDoesNotCrossDevices:
    """写路径不许跨设备降级：404 = 这台设备在服务端没了，不是"换个端点再试"。"""

    @pytest.mark.asyncio
    async def test_post_success_uses_scoped_url_only(self, mock_hass):
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _session([_resp(200, {"limits": {"c1": {"wh": 30.0}}})])
        with patch(
            "custom_components.cuktech_charger.async_get_clientsession",
            return_value=session,
        ):
            assert await coord._async_post_limit("c1", 30.0) is True
        assert [c.args[0] for c in session.post.call_args_list] == [
            f"{SERVER}/api/device/desk/charge-limits"
        ]

    @pytest.mark.asyncio
    async def test_post_404_fails_instead_of_writing_to_primary(self, mock_hass):
        """核心回归：设备维度 404 之后**不能**再 POST 一次老端点。

        老端点经 device_middleware 在无上下文时落到 primary —— 那等于把书房的
        限额静默写进客厅那台，而且同样返回 200，上层判定"成功"。
        """
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = _session([_resp(404), _resp(200, {"limits": {}})])
        with patch(
            "custom_components.cuktech_charger.async_get_clientsession",
            return_value=session,
        ):
            assert await coord._async_post_limit("c1", 30.0) is False
        urls = [c.args[0] for c in session.post.call_args_list]
        assert urls == [f"{SERVER}/api/device/desk/charge-limits"], (
            f"写请求降级到了 {urls}：这会把限额写进 primary 那台充电器")

    @pytest.mark.asyncio
    async def test_read_path_keeps_the_fallback_ladder(self, mock_hass):
        """读路径保留阶梯：读错设备最多数值不对，不污染对方状态。"""
        coord = CuktechMQTTCoordinator(mock_hass, desk_entry())
        session = MagicMock()
        session.get = MagicMock(side_effect=[
            AsyncContextManager(_resp(404)),
            AsyncContextManager(_resp(200, {"limits": {"c1": {"wh": 7.0}}})),
        ])
        with patch(
            "custom_components.cuktech_charger.async_get_clientsession",
            return_value=session,
        ):
            limits, reason = await coord._async_limit_request()
        assert reason == "ok"
        assert limits == {"c1": {"wh": 7.0}}
        assert [c.args[0] for c in session.get.call_args_list] == [
            f"{SERVER}/api/device/desk/charge-limits",
            f"{SERVER}/api/charge-limits",
        ]

    @pytest.mark.asyncio
    async def test_legacy_entry_post_is_unchanged(self, mock_hass):
        """ESP32 / 老 entry（无 device_id）的写请求与改造前逐字一致。"""
        entry = make_entry("old", {"server_url": SERVER})
        coord = CuktechMQTTCoordinator(mock_hass, entry)
        session = _session([_resp(200, {"limits": {"c1": {"wh": 5.0}}})])
        with patch(
            "custom_components.cuktech_charger.async_get_clientsession",
            return_value=session,
        ):
            assert await coord._async_post_limit("c1", 5.0) is True
        assert [c.args[0] for c in session.post.call_args_list] == [
            f"{SERVER}/api/charge-limits"
        ]
