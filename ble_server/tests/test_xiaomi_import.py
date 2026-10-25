"""小米云**多设备**导入（`/api/xiaomi/import`）与名称同步的服务端契约。

为什么值得单独锁：BLE Key 只能按 did 单取（`blt_get_beaconkey`），多台设备就是
多次云调用。如果这段逻辑写错，用户的感受是"导入了 3 台，只有 1 台能用"，
而配置页拿不到 BLE Key 的设备会保存成功却在启动时认证失败——最贵的那种错。

这些用例全部用替身客户端，不触网。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import ha_server  # noqa: E402
from golden_harness import mock_request  # noqa: E402


class FakeCloudError(Exception):
    pass


class FakeDevice:
    def __init__(self, did, mac, token, name="", model="njcuk01"):
        self.did = did
        self.mac = mac
        self.token = token
        self.name = name
        self.model = model


class FakeClient:
    """`get_beaconkey` 按 did 返回；`failing` 里的 did 抛异常（模拟单台云调用失败）。"""

    def __init__(self, keys=None, failing=()):
        self.keys = keys or {}
        self.failing = set(failing)
        self.calls = []

    def get_beaconkey(self, did):
        self.calls.append(did)
        if did in self.failing:
            raise FakeCloudError("cloud boom")
        return self.keys.get(did)


def _json_request(body, path="/api/xiaomi/import"):
    req = mock_request("POST", path)

    async def _json():
        return body

    req.json = _json
    return req


@pytest.fixture
def server(monkeypatch):
    s = ha_server.Server.__new__(ha_server.Server)
    s._xiaomi_sessions = {}
    monkeypatch.setattr(ha_server, "_load_xiaomi_cloud",
                        lambda: (FakeClient, FakeCloudError))
    return s


def _seed(server, devices, client):
    """造一个"已扫码完成"的会话（等价于 qr/complete 之后的形状）。"""
    server._xiaomi_sessions["sess1"] = {"client": client, "timer": None,
                                        "devices": devices}


DESK = FakeDevice("did-desk", "aa:bb:cc:dd:ee:02", "11" * 16, "书桌充电器")
BED = FakeDevice("did-bed", "aa:bb:cc:dd:ee:03", "22" * 16, "Bedroom")


class TestXiaomiImport:
    async def test_imports_multiple_devices_in_one_call(self, server):
        client = FakeClient(keys={"did-desk": "key-desk", "did-bed": "key-bed"})
        _seed(server, [DESK, BED], client)

        resp = await server.handle_xiaomi_import(
            _json_request({"session_id": "sess1", "dids": ["did-desk", "did-bed"]}))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        got = {d["did"]: d for d in body["devices"]}
        assert got["did-desk"] == {"did": "did-desk", "name": "书桌充电器",
                                  "mac": "aa:bb:cc:dd:ee:02", "token": "11" * 16,
                                  "model": "njcuk01", "ble_key": "key-desk"}
        assert got["did-bed"]["name"] == "Bedroom"
        assert got["did-bed"]["ble_key"] == "key-bed"
        assert client.calls == ["did-desk", "did-bed"]

    async def test_one_failure_does_not_sink_the_batch(self, server):
        client = FakeClient(keys={"did-desk": "key-desk"}, failing={"did-bed"})
        _seed(server, [DESK, BED], client)

        resp = await server.handle_xiaomi_import(
            _json_request({"session_id": "sess1", "dids": ["did-desk", "did-bed"]}))
        body = json.loads(resp.body)
        got = {d["did"]: d for d in body["devices"]}
        assert got["did-desk"]["ble_key"] == "key-desk"
        assert "cloud boom" in got["did-bed"]["error"]
        assert "token" not in got["did-bed"], "失败的那台不能带半份凭据回去"

    async def test_unknown_did_is_reported_not_guessed(self, server):
        client = FakeClient(keys={"did-desk": "key-desk"})
        _seed(server, [DESK], client)

        resp = await server.handle_xiaomi_import(
            _json_request({"session_id": "sess1", "dids": ["did-desk", "did-ghost"]}))
        got = {d["did"]: d for d in json.loads(resp.body)["devices"]}
        assert got["did-ghost"]["error"] == "device_not_in_list"
        assert "did-ghost" not in client.calls, "不知道的 did 不该去问云端"

    async def test_session_survives_for_a_second_round(self, server):
        """分两批导入都要能用：导入后会话仍在，且定时器被重挂（否则永久泄漏）。"""
        client = FakeClient(keys={"did-desk": "key-desk", "did-bed": "key-bed"})
        _seed(server, [DESK, BED], client)

        await server.handle_xiaomi_import(
            _json_request({"session_id": "sess1", "dids": ["did-desk"]}))
        entry = server._xiaomi_sessions.get("sess1")
        assert entry is not None, "导入一次就把会话删了 → 第二批要重新扫码"
        assert entry["timer"] is not None, "会话还在却没有超时定时器 = 泄漏"

        resp = await server.handle_xiaomi_import(
            _json_request({"session_id": "sess1", "dids": ["did-bed"]}))
        assert json.loads(resp.body)["devices"][0]["ble_key"] == "key-bed"
        entry["timer"].cancel()

    async def test_rejects_empty_selection_and_unknown_session(self, server):
        client = FakeClient()
        _seed(server, [DESK], client)
        for body in ({"session_id": "sess1", "dids": []},
                     {"session_id": "sess1"},
                     {"session_id": "sess1", "dids": ["  "]}):
            resp = await server.handle_xiaomi_import(_json_request(body))
            assert resp.status == 400, body
        resp = await server.handle_xiaomi_import(
            _json_request({"session_id": "nope", "dids": ["did-desk"]}))
        assert resp.status == 400
        assert "会话已过期" in json.loads(resp.body)["error"]

    async def test_beaconkey_endpoint_still_works_and_keeps_session(self, server):
        client = FakeClient(keys={"did-desk": "key-desk", "did-bed": "key-bed"})
        _seed(server, [DESK, BED], client)

        resp = await server.handle_xiaomi_beaconkey(
            _json_request({"session_id": "sess1", "did": "did-desk"},
                          path="/api/xiaomi/beaconkey"))
        assert json.loads(resp.body)["ble_key"] == "key-desk"
        assert "sess1" in server._xiaomi_sessions, "单台取 key 也不该销毁会话"
        entry = server._xiaomi_sessions["sess1"]
        if entry["timer"]:
            entry["timer"].cancel()


class TestXiaomiImportBudget:
    """P2-1：批量导入必须有整体预算，别让云端卡住变成 N 次悬死。

    `requests` 的超时已由 `xiaomi_cloud.CLOUD_HTTP_TIMEOUT` 兜住；这里再锁服务端的
    "整体截止时间"：超出预算的设备逐台返回 `timeout`，请求本身必须按时收尾
    （30s 中间件只会把浏览器救出去，线程不会）。
    """

    async def test_over_budget_devices_report_timeout(self, server, monkeypatch):
        client = FakeClient(keys={"did-desk": "k1", "did-bed": "k2"})
        _seed(server, [DESK, BED], client)
        monkeypatch.setattr(server, "XIAOMI_IMPORT_BUDGET", -1.0)   # 立即超预算

        resp = await server.handle_xiaomi_import(
            _json_request({"session_id": "sess1", "dids": ["did-desk", "did-bed"]}))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert [d.get("error") for d in body["devices"]] == ["timeout", "timeout"]
        assert client.calls == [], "超预算就不该再去调云端"
        entry = server._xiaomi_sessions["sess1"]
        if entry["timer"]:
            entry["timer"].cancel()

    def test_cloud_client_sets_http_timeouts(self):
        """`requests` 默认无限等：`_api_call` 必须显式给 (连接, 读取) 超时。"""
        import inspect
        import xiaomi_cloud

        src = inspect.getsource(xiaomi_cloud)
        assert src.count("timeout=CLOUD_HTTP_TIMEOUT") >= 4, \
            "登录两步 + 两个 API 客户端的 post/get 都要带超时"
        assert xiaomi_cloud.CLOUD_HTTP_TIMEOUT == (5, 15)


class TestXiaomiSessionLifecycle:
    async def test_expired_session_is_dropped_by_timer(self, server):
        client = FakeClient()
        _seed(server, [DESK], client)
        server._arm_xiaomi_timer("sess1", 0.01)
        await asyncio.sleep(0.05)
        assert "sess1" not in server._xiaomi_sessions, "过期会话必须被回收"

    async def test_arm_is_idempotent(self, server):
        """重复挂定时器不应留下多个（每次重挂先取消上一个）。"""
        client = FakeClient()
        _seed(server, [DESK], client)
        server._arm_xiaomi_timer("sess1", 60)
        first = server._xiaomi_sessions["sess1"]["timer"]
        server._arm_xiaomi_timer("sess1", 60)
        second = server._xiaomi_sessions["sess1"]["timer"]
        assert first is not second and first.cancelled()
        second.cancel()
