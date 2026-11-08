"""HTTP contract for GET /api/energy/heatmap (design doc §4.8).

不启真 socket：直接调用 handler，断言 body 契约、ETag/304、空库与设备隔离。
"""
import json
import time
from datetime import datetime, timedelta

import pytest

from ha_server import Server, _compute_heat_levels
from history import DEFAULT_DEVICE_ID, DeviceHistory, PortHistory


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


@pytest.fixture
def store(temp_db):
    h = PortHistory(db_path=temp_db, retention_days=2, session_retention_days=30)
    h.connect()
    yield h
    h.close()


def make_server(history):
    s = Server.__new__(Server)
    s.history = history
    return s


class _Request:
    def __init__(self, headers=None):
        self.headers = headers or {}


class TestHeatLevels:
    def test_route_is_mounted_for_path_addressing(self):
        """两处路由必须同时挂：`/api/energy/heatmap` 与 `/api/device/{id}/...`。"""
        import ha_server
        assert ("GET", "/energy/heatmap", "handle_heatmap") in \
            ha_server._DEVICE_SCOPED_ROUTES

    def test_four_strictly_increasing_thresholds(self):
        levels = _compute_heat_levels([1, 2, 3, 4, 5, 6, 7, 8])
        assert len(levels) == 3
        assert all(a < b for a, b in zip(levels, levels[1:]))

    def test_flat_distribution_falls_back_to_max_proportions(self):
        """全是同一天同值：分位数会塌缩，必须回落成按最大值的比例。"""
        levels = _compute_heat_levels([10.0] * 8)
        assert levels == [2.5, 5.0, 7.5]

    def test_empty_and_zero_values(self):
        assert _compute_heat_levels([]) == []
        assert _compute_heat_levels([0, 0.0, None]) == []


class TestHeatmapHandler:
    @pytest.mark.asyncio
    async def test_contract_and_aggregates(self, store):
        now = time.time()
        yday = now - 86400
        DeviceHistory(store, DEFAULT_DEVICE_ID).add_energy_delta(1, 3.0, yday)
        DeviceHistory(store, DEFAULT_DEVICE_ID).add_energy_delta(2, 1.0, yday)
        DeviceHistory(store, DEFAULT_DEVICE_ID).add_energy_delta(1, 6.0, now)
        sid = store.start_session(1, "PD", start_time=now)
        store.end_session(sid, total_wh=6.0, peak_power_w=42.0,
                          avg_voltage=20.0, avg_current=1.0,
                          duration_sec=1200, end_time=now + 1200)
        store.flush()

        resp = await make_server(DeviceHistory(store)).handle_heatmap(_Request())
        body = json.loads(resp.body)

        assert resp.status == 200
        assert resp.content_type == "application/json"
        assert resp.headers["Cache-Control"] == "no-cache"
        assert body["unit"] == "Wh"
        assert body["range"]["end"] == datetime.now().strftime("%Y-%m-%d")
        assert body["range"]["start"] == (
            datetime.now() - timedelta(days=364)).strftime("%Y-%m-%d")
        assert body["total_wh"] == 10.0
        assert body["session_count"] == 1          # 只有今天这次会话闭合
        assert body["active_days"] == 2
        assert body["data_since"] == day_of(yday)

        days = {d["date"]: d for d in body["days"]}
        assert days[day_of(yday)]["wh"] == 4.0
        assert days[day_of(now)]["wh"] == 6.0
        assert days[day_of(now)]["sessions"] == 1
        assert days[day_of(now)]["peak_w"] == 42.0

        peak = body["peak"]
        assert peak["wh"] == 6.0

        levels = body["levels"]
        assert len(levels) == 3
        assert all(a < b for a, b in zip(levels, levels[1:]))

    @pytest.mark.asyncio
    async def test_etag_and_304(self, store):
        DeviceHistory(store).add_energy_delta(1, 2.0, time.time())
        store.flush()
        server = make_server(DeviceHistory(store))

        first = await server.handle_heatmap(_Request())
        etag = first.headers["ETag"]
        assert len(etag) == 64

        second = await server.handle_heatmap(_Request({"If-None-Match": etag}))
        assert second.status == 304
        assert not second.body
        assert second.headers["ETag"] == etag

        # 内容变化（同一天再充一点）后 ETag 必须变
        DeviceHistory(store).add_energy_delta(1, 1.0, time.time())
        store.flush()
        third = await server.handle_heatmap(_Request({"If-None-Match": etag}))
        assert third.status == 200
        assert third.headers["ETag"] != etag

    @pytest.mark.asyncio
    async def test_empty_library(self, store):
        resp = await make_server(DeviceHistory(store)).handle_heatmap(_Request())
        body = json.loads(resp.body)

        assert body["days"] == []
        assert body["data_since"] is None
        assert body["peak"] is None
        assert body["levels"] == []
        assert body["total_wh"] == 0
        assert body["active_days"] == 0
        assert body["session_count"] == 0

    @pytest.mark.asyncio
    async def test_per_device_isolation(self, store):
        now = time.time()
        DeviceHistory(store, "devA").add_energy_delta(1, 1.0, now)
        DeviceHistory(store, "devB").add_energy_delta(1, 9.0, now)
        store.flush()

        a = json.loads((await make_server(
            DeviceHistory(store, "devA")).handle_heatmap(_Request())).body)
        b = json.loads((await make_server(
            DeviceHistory(store, "devB")).handle_heatmap(_Request())).body)

        assert a["total_wh"] == 1.0
        assert b["total_wh"] == 9.0
        assert a["days"][0]["date"] == b["days"][0]["date"] == day_of(now)
