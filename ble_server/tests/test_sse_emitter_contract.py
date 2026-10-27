"""`DeviceSSEEmitter` 的接口形状（协作约定 ①：`emit` 保持**两参**）。

为什么单独一个文件：这里是**同步**的形状/透传断言，与 async 的 golden 对比混在
一起会被 `pytest.mark.asyncio` 误标；分开也更容易定位"谁动了 SSE 出口"。

两参的含义：per-device 包装器在**构造时**就知道自己属于哪台设备（`device_id`），
所以 `emit(event_type, data)` 不需要第三个参数；跨设备广播是
`s.sse.emit(..., device_id=...)` 直接调底层 `SSEEmitter`，不经过本包装器。
"""
import inspect


def test_emit_signature_stays_two_arguments():
    """不加 `device_id=` 第三参（加了两参契约与本文件的透传断言都得改）。"""
    import ha_server

    params = list(inspect.signature(ha_server.DeviceSSEEmitter.emit).parameters)
    assert params == ["self", "event_type", "data"], params


class _RecordingEmitter:
    """真 `SSEEmitter` 的最小替身：只记下"以什么参数被调用"。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def emit(self, event_type: str, data: dict, device_id: str | None = None) -> None:
        self.calls.append((event_type, data, device_id))


class TestPassthrough:
    def test_single_device_passthrough_is_exact(self):
        """`multi=False`：完全透传 —— 不加字段、连 device_id 都不带给底层分桶。"""
        import ha_server

        base = _RecordingEmitter()
        ha_server.DeviceSSEEmitter(base, "desk", multi=False).emit("status", {"connected": True})
        assert base.calls == [("status", {"connected": True}, None)]

    def test_multi_device_injects_device_and_buckets(self):
        """`multi=True`：payload 加 `device`，底层按 device_id 分桶（latest-only 键）。"""
        import ha_server

        base = _RecordingEmitter()
        ha_server.DeviceSSEEmitter(base, "desk", multi=True).emit("status", {"connected": True})
        assert base.calls == [("status", {"connected": True, "device": "desk"}, "desk")]
