"""多设备（N≥2）外部可见行为的 golden 基线 + 契约断言（§3.7 / §3.8 / §3.13）。

与 `test_golden_baseline.py` 的分工：那份锁**单设备逐字节不变**（P0 红线），这份锁
**多设备才新增的那部分字段**，并且同一份 `golden_env` 真装配路径（真 `Server.__init__`
→ 真 `on_startup` → 真 handler）只是配置从 1 台扩到 2 台。

基线文件（`tests/golden/`，没有 `*_devices.txt`——约定就是 JSON + `check_golden`）：

| 基线 | 内容 | 归一化 |
|---|---|---|
| `http_health_multi.json` | N=2 `GET /api/health`，必须含 `devices` 分区 | `normalize_uptime`（唯一白名单项） |
| `http_devices.json` | `GET /api/devices` 的**单设备**形态（N=1 也必须有一条基线） | 无 |
| `sse_init_multi.json` | N=2 SSE `init` 帧，含 `devices` 列表 | 无 |

`/api/devices` 响应契约（生产侧按此实现，这里逐项断言）：

    {"ok": true, "devices": [{id, name, mac, connected, topic_prefix, is_primary}]}

顺序 = 配置顺序（primary 首位）。

另外锁两条 §3.8 红线：
- 多设备事件带 `device`，且每个设备的 `status` 各占一个 (queue, device) 桶，互不覆盖；
- 单设备事件**不带** `device` 字段、`/api/health` **不带** `devices` 分区。

重新录制：`CUKTECH_GOLDEN_UPDATE=1 pytest tests/test_golden_multi_device.py`
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golden_harness import (  # noqa: E402
    GOLDEN_MAC, MULTI_DEVICE_BASELINES, TOPIC_PREFIX, check_golden, golden_env,
    json_body, mock_request, normalize_uptime, raw_body, snapshot_state,
)
# SSE init 的驱动方式（起 task、等 init 写完、取消）与单设备基线完全一致，
# 复用同一份实现，避免两处对"init 何时算写完"的口径漂移。
from test_golden_baseline import _drive_sse_init  # noqa: E402

pytestmark = pytest.mark.asyncio

DESK_ID = "desk"
DESK_MAC = "AA:BB:CC:DD:EE:0A"
DESK_TOKEN = "ffeeddccbbaa99887766554433221100"

#: `/api/devices` 单项的字段集（§3.7；`DeviceRuntime.describe()` 的完整输出）
DEVICE_KEYS = {"id", "name", "mac", "connected", "topic_prefix", "is_primary"}

#: §3.13 的 `devices` 分区字段（`last_seen` 尚未落地时不强制存在，出现即被基线锁定）
HEALTH_DEVICE_REQUIRED = {"connected", "authenticated"}
HEALTH_DEVICE_ALLOWED = HEALTH_DEVICE_REQUIRED | {"last_seen"}


def _desk() -> dict:
    return {"id": DESK_ID, "mac": DESK_MAC, "token": DESK_TOKEN}


@pytest.fixture
async def env_single():
    async with golden_env() as env:
        snapshot_state(env.server.state)
        yield env


@pytest.fixture
async def env_multi():
    async with golden_env(devices=[_desk()]) as env:
        for rt in env.server.registry:
            snapshot_state(rt.state)
        yield env


def _frame_payload(line: str) -> dict:
    """把 `drain_sse` 给出的线上字节（`"data: {...}\\n\\n"`）还原成 payload。"""
    text = line.strip()
    if text.startswith("data: "):
        text = text[len("data: "):]
    return json.loads(text)


class TestApiDevices:
    async def test_single_device_contract_and_golden(self, env_single):
        """N=1：`/api/devices` 仍是一台 primary（Web UI 据此隐藏切换器）。"""
        resp = await env_single.server.handle_devices(mock_request("GET", "/api/devices"))
        raw = raw_body(resp)
        doc = json.loads(raw)

        assert list(doc) == ["ok", "devices"]
        assert doc["ok"] is True
        assert len(doc["devices"]) == 1
        only = doc["devices"][0]
        assert set(only) == DEVICE_KEYS
        assert only["id"] == "default"
        assert only["is_primary"] is True
        assert only["mac"] == GOLDEN_MAC
        assert only["topic_prefix"] == TOPIC_PREFIX

        check_golden("http_devices", raw)

    async def test_multi_device_order_and_fields(self, env_multi):
        """N=2：顺序 = 配置顺序（primary 首位），字段集与契约完全一致。"""
        resp = await env_multi.server.handle_devices(mock_request("GET", "/api/devices"))
        doc = json.loads(raw_body(resp))

        assert doc["ok"] is True
        devs = doc["devices"]
        assert [d["id"] for d in devs] == ["default", DESK_ID]
        assert [d["is_primary"] for d in devs] == [True, False]
        for d in devs:
            assert set(d) == DEVICE_KEYS

        primary, desk = devs
        assert primary["mac"] == GOLDEN_MAC
        assert primary["topic_prefix"] == TOPIC_PREFIX
        assert desk["mac"] == DESK_MAC
        # 附加设备的前缀默认派生 `{base}/{id}`（§3.6），与 primary 不相等、不嵌套成命令主题
        assert desk["topic_prefix"] == f"{TOPIC_PREFIX}/{DESK_ID}"
        # 状态快照把两台都置为已连接
        assert primary["connected"] is True and desk["connected"] is True


class TestApiHealthMulti:
    async def test_multi_device_has_devices_partition(self, env_multi):
        """N=2：`/api/health` 附加 `devices` 真值分区（§3.13）；老字段一字不改。"""
        resp = await env_multi.server.handle_health(mock_request("GET", "/api/health"))
        body = json_body(resp)

        assert body["ok"] is True
        assert set(body) >= {"ok", "healthy", "components", "uptime_ms", "devices"}
        assert body["devices"], "多设备必须给出 devices 分区"

        assert set(body["devices"]) == {"default", DESK_ID}
        for device_id, entry in body["devices"].items():
            assert HEALTH_DEVICE_REQUIRED <= set(entry) <= HEALTH_DEVICE_ALLOWED, device_id
            assert entry["connected"] is True, device_id
            assert entry["authenticated"] is True, device_id
        # 分区是"附加"：components 仍是 primary 视角（§3.13 不做语义变更）
        assert set(body["components"]) == {"ble", "mqtt", "bemfa"}

        check_golden("http_health_multi", body, normalize=normalize_uptime)

    async def test_single_device_has_no_devices_partition(self, env_single):
        """§3.8 红线：单设备响应一个字都不许多 —— 不加 `devices`。"""
        resp = await env_single.server.handle_health(mock_request("GET", "/api/health"))
        body = json_body(resp)
        assert "devices" not in body
        assert set(body) == {"ok", "healthy", "components", "uptime_ms"}


class TestSseMulti:
    async def test_sse_init_carries_devices_list(self, env_multi):
        """N=2 SSE `init`：附加 `devices` 列表（§3.8），其余字段与单设备同构。"""
        lines = await _drive_sse_init(env_multi.server)
        assert lines, "handle_sse 没有写出任何帧"
        assert lines[0].startswith("data: ")
        payload = json.loads(lines[0][len("data: "):])

        assert payload["type"] == "init"
        devs = payload["devices"]
        assert [d["id"] for d in devs] == ["default", DESK_ID]
        assert [d["is_primary"] for d in devs] == [True, False]
        for d in devs:
            assert set(d) == DEVICE_KEYS
        assert payload["mqtt_connected"] is True

        check_golden("sse_init_multi", {"frame": lines[0]})

    async def test_multi_device_status_carries_device_and_does_not_mix(self, env_multi):
        """§3.8：多设备事件带 `device`，且 status 按 (queue, device) 分桶互不覆盖。"""
        primary_rt = env_multi.server.primary
        desk_rt = env_multi.server.registry[DESK_ID]
        q = env_multi.open_sse_queue()          # latest-only 只投给"已在册的客户端"
        try:
            primary_rt.manager._publish_status({"connected": True, "authenticated": True}, retain=True)
            desk_rt.manager._publish_status({"connected": False, "authenticated": False}, retain=True)
            # 与 `handle_sse` 多设备分支同序：逐个设备 flush 自己的桶
            lines = [
                pending for rt in env_multi.server.registry
                if (pending := env_multi.server.sse.flush_status(q, rt.device_id))
            ]
        finally:
            env_multi.server.sse.remove_client(q)

        assert len(lines) == 2, f"两台设备各应保留一条 status（不互相覆盖），实际 {lines}"
        by_device = {}
        for line in lines:
            payload = _frame_payload(line)
            assert payload["type"] == "status"
            assert "device" in payload, "多设备事件必须带 device 字段"
            by_device[payload["device"]] = payload
        assert set(by_device) == {"default", DESK_ID}
        assert by_device["default"]["connected"] is True
        assert by_device[DESK_ID]["connected"] is False

    async def test_single_device_status_has_no_device_field(self, env_single):
        """§3.8 红线：单设备事件流逐字节不变 —— 不许出现 `device` 字段。"""
        q = env_single.open_sse_queue()
        env_single.server.ble._publish_status(
            {"connected": True, "authenticated": True}, retain=True)
        lines = env_single.drain_sse(q)
        env_single.server.sse.remove_client(q)
        assert lines, "SSE 没有任何投递"
        for line in lines:
            assert "device" not in _frame_payload(line)


class TestBaselineSet:
    """基线集合自身的守卫：白名单不能变成"随便放水"的后门。"""

    @staticmethod
    def _mentions_device(node) -> bool:
        """递归找设备维度字段；嵌在字符串里的 JSON（SSE 帧、raw body）也拆进去。"""
        if isinstance(node, dict):
            return any(k in ("device", "devices", "device_id") or TestBaselineSet._mentions_device(v)
                       for k, v in node.items())
        if isinstance(node, list):
            return any(TestBaselineSet._mentions_device(v) for v in node)
        if isinstance(node, str):
            text = node.strip()
            if text.startswith("data: "):
                text = text[len("data: "):]
            if text.startswith("{") or text.startswith("["):
                try:
                    return TestBaselineSet._mentions_device(json.loads(text))
                except (ValueError, TypeError):
                    return False
        return False

    async def test_multi_device_baselines_exist_and_carry_device_keys(self):
        """白名单里的每份基线都必须存在，并且确实带设备维度字段。

        反向守卫：`TestBaselineGuard` 会跳过白名单文件（它们合法地带 device 字段），
        所以这里必须证明跳过是**有据**的 —— 否则白名单就成了藏污纳垢的地方。
        """
        for name in sorted(MULTI_DEVICE_BASELINES):
            path = Path(__file__).parent / "golden" / f"{name}.json"
            assert path.exists(), f"多设备基线缺失：{path}"
            doc = json.loads(path.read_text(encoding="utf-8"))
            assert self._mentions_device(doc), f"{name} 不含设备维度字段"
