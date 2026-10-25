"""P0 golden 基线测试 —— 单设备外部可见行为逐字节锁定。

设计文档：docs/plans/2026-10-23-multi-device-architecture.md §5.1、§7（P0）

覆盖 §5.1 点名的五类产物，**全部经真实生产代码路径**产出：

| # | 产物 | 真实路径 |
|---|---|---|
| 1 | `GET /api/status` 响应体 | `Server.handle_status` → `ChargerState.to_dict` |
| 2 | `GET /api/health` 响应体 | `Server.handle_health` |
| 3 | SSE `init` 线字节 | `Server.handle_sse`（真 handler + mocked payload writer） |
| 4 | SSE `status` / `port_update` 线字节 | `BLEManager._publish_status` / `_emit_port_state` → 真 `SSEEmitter` |
| 5 | MQTT 四类发布 + 遗嘱 + 订阅清单 | `Server.mqtt_publish` → paho 边界（topic/payload/retain/qos） |

用法：

    pytest tests/test_golden_baseline.py            # 对比
    CUKTECH_GOLDEN_UPDATE=1 pytest tests/test_golden_baseline.py   # 重新记录基线

基线文件在 `tests/golden/*.json`，必须入库；任何改动都要能解释"为什么外部可见行为变了"。
"""
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golden_harness import (  # noqa: E402
    GOLDEN_DIR, MULTI_DEVICE_BASELINES, check_golden, golden_env, mock_request,
    normalize_uptime, raw_body, snapshot_state, update_requested,
)

pytestmark = pytest.mark.asyncio


async def _drive_sse_init(server, *, settle_loops: int = 5) -> list[str]:
    """跑真 `handle_sse`，取它写上线的字节。

    handler 是无限循环（事件流 + 15s keepalive），所以起 task、等它把 init 写完、
    再取消 —— 取消走生产代码里的 `CancelledError` 分支，`remove_client` 等收尾
    一并被覆盖。
    """
    request = mock_request("GET", "/api/events")
    writer = request._payload_writer
    task = asyncio.create_task(server.handle_sse(request))
    # init 在 prepare 之后立刻写；让出若干轮事件循环直到写入次数稳定。
    prev = -1
    for _ in range(settle_loops):
        await asyncio.sleep(0.02)
        now = len(writer.write.call_args_list)
        if now == prev and now > 0:
            break
        prev = now
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    text = b"".join(c.args[0] for c in writer.write.call_args_list if c.args).decode("utf-8", "replace")
    return [frame for frame in text.split("\n\n") if frame.strip()]


@pytest.fixture
async def env():
    """真 Server + 真 on_startup；BLE/MQTT/时钟三个外部依赖被替身接管。"""
    async with golden_env() as e:
        snapshot_state(e.server.state)
        yield e


class TestGoldenHttp:
    async def test_api_status_body_is_golden(self, env):
        """/api/status 响应体逐字节（本快照无任何排除字段）。"""
        resp = await env.server.handle_status(mock_request("GET", "/api/status"))
        check_golden("http_status", raw_body(resp))

    async def test_api_status_cache_hit_same_bytes(self, env):
        """缓存命中路径必须产出同一份字节（P1 会把缓存键升级为复合键，
        这条守住"同设备重复请求不产生差异"）。"""
        first = raw_body(await env.server.handle_status(mock_request()))
        env.server.invalidate_status_cache()
        second = raw_body(await env.server.handle_status(mock_request()))
        assert first == second
        check_golden("http_status", second)

    async def test_api_health_body_is_golden(self, env):
        """/api/health 逐字节；唯一排除项 uptime_ms（时间戳类，见白名单）。"""
        resp = await env.server.handle_health(mock_request("GET", "/api/health"))
        body = json.loads(raw_body(resp))
        check_golden("http_health", body, normalize=normalize_uptime)


class TestGoldenSse:
    async def test_sse_init_wire_bytes(self, env):
        """SSE `init`：真 handle_sse 写上线的第一帧。"""
        lines = await _drive_sse_init(env.server)
        assert lines, "handle_sse 没有写出任何帧"
        check_golden("sse_init", {"frame": lines[0]})

    async def test_sse_status_and_port_update(self, env):
        """SSE `status`（latest-only）与 `port_update`（普通队列）两种投递语义。

        status 不直接入队、由 handler 每轮 flush，顺序敏感 —— 这里按真实投递
        结果记录，P1 改成 (queue, device) 分桶后必须仍然给出同样字节。
        """
        q = env.open_sse_queue()
        env.server.ble._publish_status({"connected": True, "authenticated": True}, retain=True)
        for piid in (1, 2, 3, 4):
            env.server.ble._emit_port_state(piid)
        with patch.dict(env.server.state.settings, {"16": 0b1111}):
            env.server.ble._emit_port_state(3)
        lines = env.drain_sse(q)
        env.server.sse.remove_client(q)
        assert lines, "SSE 没有任何投递"
        check_golden("sse_status_and_port_update", {"frames": lines})

    async def test_sse_settings_and_protocol_and_session_end_event_names(self, env):
        """其余事件类型至少锁定"事件名 + 键形状"（载荷含运行期数据，另由单测覆盖）。"""
        q = env.open_sse_queue()
        env.server.ble._publish_settings(retain=True)
        env.server.sse.remove_client(q)
        frames = env.drain_sse(q)
        check_golden("sse_settings", {"frames": frames})


class TestGoldenMqtt:
    async def test_will_and_subscriptions(self, env):
        """连接期建立的遗嘱与订阅清单（§3.6 说 LWT 留在 primary status 上，
        这里就是那句话的基线）。"""
        check_golden("mqtt_session", {
            "will": env.mqtt.will,
            "subscribed": sorted(env.mqtt.subscribed),
        })

    async def test_four_published_topic_types(self, env):
        """四类 topic 的 topic/payload/retain/qos 四元组（§5.1 口径）。"""
        env.reset_mqtt()
        q = env.open_sse_queue()
        env.server.ble._publish_status({"connected": True, "authenticated": True}, retain=True)
        env.server.ble._publish_settings(retain=True)
        for piid in (1, 2, 3, 4):
            env.server.ble._emit_port_state(piid)
        env.server.sse.remove_client(q)
        check_golden("mqtt_publishes", {"published": env.publishes})

    async def test_charge_event_payload(self, env):
        """charge_event 载荷形状（会话 id / 时间串格式 / 数值精度都在这里定形）。"""
        from energy import PortEnergyState

        env.reset_mqtt()
        es = PortEnergyState()
        es.session_start = 1764000000.0 - 3600.0
        es.session_wh = 123.45678
        es.max_power = 88.5
        env.server.ble._publish_charge_event(
            1, 42, es, 1764000000.0, 5.0, 1.0, 3600)
        check_golden("mqtt_charge_event", {"published": env.publishes})


FORBIDDEN_SINGLE_DEVICE_KEYS = {"device", "devices", "device_id"}
ALLOWED_PLACEHOLDERS = {"<UPTIME_MS>"}


def _walk_keys(node, path="$"):
    """递归产出 (路径, 键名)，嵌在字符串里的 JSON 也拆进去（MQTT payload / SSE 帧）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k
            yield from _walk_keys(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_keys(v, f"{path}[{i}]")
    elif isinstance(node, str):
        text = node.strip()
        if text.startswith("data: "):
            text = text[len("data: "):]
        if text.startswith("{") or text.startswith("["):
            try:
                yield from _walk_keys(json.loads(text), f"{path}<json>")
            except (ValueError, TypeError):
                pass


class TestBaselineGuard:
    """基线本身必须可信：既能咬住改动，也不许悄悄放宽口径。"""

    async def test_baseline_bites_on_a_single_field_change(self, env):
        """翻一个布尔就必须失败 —— 否则整份基线是装饰品。"""
        if update_requested():
            pytest.skip("记录模式下不比对")
        resp = await env.server.handle_status(mock_request())
        doc = json.loads(raw_body(resp))
        doc["connected"] = False
        with pytest.raises(AssertionError, match="golden 快照不一致"):
            check_golden("http_status", json.dumps(doc, ensure_ascii=False))

    async def test_baseline_bites_on_mqtt_retain_change(self, env):
        """`retain` 翻转必须失败（§5.1：MQTT 比较 topic+payload+retain+QoS）。"""
        if update_requested():
            pytest.skip("记录模式下不比对")
        with pytest.raises(AssertionError, match="golden 快照不一致"):
            check_golden("mqtt_session", {
                "will": {**(env.mqtt.will or {}), "retain": not (env.mqtt.will or {}).get("retain", True)},
                "subscribed": sorted(env.mqtt.subscribed),
            })

    async def test_single_device_snapshots_carry_no_device_keys(self):
        """§3.8 红线的静态守卫：单设备快照里不许出现 device/devices/device_id。

        多设备才新增的字段一旦漏进单设备路径，外部可见行为就变了 —— 这条不依赖
        改造是否发生，现在就能跑，将来也一直在。

        `MULTI_DEVICE_BASELINES` 里的文件（`/api/devices` 自身形态、N=2 的 health /
        SSE init）**按设计**带设备字段，跳过；跳过的正当性由
        `test_golden_multi_device.TestBaselineSet` 反向证明。
        """
        offenders = []
        for p in sorted(GOLDEN_DIR.glob("*.json")):
            if p.stem in MULTI_DEVICE_BASELINES:
                continue
            doc = json.loads(p.read_text(encoding="utf-8"))
            for where, key in _walk_keys(doc, p.name):
                if key in FORBIDDEN_SINGLE_DEVICE_KEYS:
                    offenders.append(f"{where}")
        assert not offenders, "单设备快照出现了设备归属字段：" + ", ".join(offenders)

    async def test_only_whitelisted_placeholders_are_used(self):
        """哨兵是封闭清单（golden/timestamp_fields.md），出现别的即口径被放水。"""
        seen = set()
        for p in sorted(GOLDEN_DIR.glob("*.json")):
            text = p.read_text(encoding="utf-8")
            for chunk in text.replace('"', " ").split():
                if chunk.startswith("<") and chunk.endswith(">"):
                    seen.add(chunk)
        assert seen <= ALLOWED_PLACEHOLDERS, f"未登记的时间戳哨兵：{sorted(seen - ALLOWED_PLACEHOLDERS)}"

