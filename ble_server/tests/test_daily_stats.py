"""Tests for daily_energy aggregation (heatmap data source) and its tooling.

分层保留的口径（设计文档 §4.6）：
  · port_history   2 天    · charge_sessions 30 天（可配）
  · charge_points  7 天    · daily_energy    365 天（热力图靠它才有意义）
"""
import math
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from history import (
    DEFAULT_DEVICE_ID,
    DeviceHistory,
    PortHistory,
)


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def local_ts(dt: datetime) -> float:
    """把 naive 本地时间转成 epoch（测试里和 history 的 localtime 口径一致）。"""
    return dt.timestamp()


@pytest.fixture
def store(temp_db):
    h = PortHistory(db_path=temp_db, retention_days=2, session_retention_days=30)
    h.connect()
    yield h
    h.close()


@pytest.fixture
def history(store):
    return DeviceHistory(store, DEFAULT_DEVICE_ID)


class TestAddEnergyDelta:
    def test_flushes_to_its_local_day(self, store, history):
        ts = time.time()
        history.add_energy_delta(1, 1.5, ts)
        history.add_energy_delta(1, 2.5, ts)
        history.add_energy_delta(2, 4.0, ts)
        store.flush()

        data = history.get_daily_energy()
        assert [d["date"] for d in data["days"]] == [day_of(ts)]
        assert data["days"][0]["wh"] == 8.0
        assert data["data_since"] == day_of(ts)

    def test_read_sees_unflushed_delta(self, store, history):
        """get_daily_energy 自己会 flush，否则"今天"那格是过期的。"""
        history.add_energy_delta(1, 2.0, time.time())
        data = history.get_daily_energy()
        assert data["days"][0]["wh"] == 2.0

    def test_daily_buffer_is_bounded(self, store, monkeypatch):
        """DB 持续写失败时缓冲不能无限涨（正常一年也到不了上限）。"""
        monkeypatch.setattr(store, "DAILY_MAX_KEYS", 2)
        now = time.time()
        store.add_energy_delta(1, 1.0, now)
        store.add_energy_delta(2, 1.0, now)
        store.add_energy_delta(3, 1.0, now)   # 超限：丢弃并只告警一次
        assert len(store._daily) == 2
        assert store._daily_overflow_warned is True

    def test_failed_commit_restores_daily_buffer(self, store, monkeypatch):
        """commit 失败时整批增量必须回到缓冲，不能悄悄丢掉。"""
        ts = time.time()
        real = store._conn

        class _CommitBoom:
            def __init__(self, conn):
                self.real = conn

            def executemany(self, *args):
                return self.real.executemany(*args)

            def commit(self):
                raise sqlite3.OperationalError("boom")

            def rollback(self):
                self.real.rollback()

        store.add_energy_delta(1, 2.0, ts)
        monkeypatch.setattr(store, "_conn", _CommitBoom(real))
        store.flush()                     # 提交失败：缓冲应被放回
        monkeypatch.setattr(store, "_conn", real)
        assert store._daily, "失败的增量没有回到缓冲"

        store.flush()                     # 重试成功
        days = {d["date"]: d["wh"] for d in store.get_daily_energy()["days"]}
        assert days[day_of(ts)] == 2.0

    def test_non_positive_and_non_finite_ignored(self, store, history):
        ts = time.time()
        for bad in (0, 0.0, -1.0, -0.001, math.nan, math.inf, -math.inf):
            history.add_energy_delta(1, bad, ts)
        store.flush()
        assert history.get_daily_energy()["days"] == []

    def test_add_energy_delta_never_waits_for_db_lock(self, store):
        """事件循环里每帧都会调它：`_db_lock` 被占着也必须立即返回。"""
        import threading

        done = []

        def worker():
            store.add_energy_delta(1, 1.0, time.time())
            done.append(True)

        with store._db_lock:          # 模拟一次慢查询正持锁
            t = threading.Thread(target=worker)
            t.start()
            t.join(0.5)
        assert done, "add_energy_delta 阻塞在 _db_lock 上了"

        store.flush()
        assert store.get_daily_energy()["days"][0]["wh"] == 1.0

    def test_flush_with_empty_buffers_is_noop(self, store):
        store.flush()
        store.flush()
        assert store.get_daily_energy()["days"] == []

    def test_cross_midnight_splits_by_frame_time(self, store, history):
        """按帧归日：跨零点的一段充电会落到两个本地日。"""
        midnight = datetime.now().replace(hour=0, minute=5, second=0, microsecond=0)
        t1 = local_ts(midnight - timedelta(minutes=10))
        t2 = local_ts(midnight)
        history.add_energy_delta(1, 1.0, t1)
        history.add_energy_delta(1, 3.0, t2)
        store.flush()

        days = {d["date"]: d["wh"] for d in history.get_daily_energy()["days"]}
        assert days == {day_of(t1): 1.0, day_of(t2): 3.0}
        assert day_of(t1) != day_of(t2)

    def test_data_since_uses_min_over_all_rows(self, store, history):
        """data_since 是全量 MIN(day)，不是查询窗口的起点。"""
        now = time.time()
        old = now - 10 * 86400
        history.add_energy_delta(1, 5.0, old)
        history.add_energy_delta(1, 2.0, now)
        store.flush()

        data = history.get_daily_energy(days=1)
        assert [d["date"] for d in data["days"]] == [day_of(now)]
        assert data["days"][0]["wh"] == 2.0
        assert data["data_since"] == day_of(old)

    def test_multi_device_isolated(self, store):
        ts = time.time()
        DeviceHistory(store, "devA").add_energy_delta(1, 1.0, ts)
        DeviceHistory(store, "devB").add_energy_delta(1, 2.0, ts)
        store.flush()

        a = DeviceHistory(store, "devA").get_daily_energy()
        b = DeviceHistory(store, "devB").get_daily_energy()
        assert a["days"][0]["wh"] == 1.0
        assert b["days"][0]["wh"] == 2.0
        assert a["data_since"] == b["data_since"] == day_of(ts)
        assert DeviceHistory(store, "devC").get_daily_energy() == {
            "days": [], "data_since": None}


class TestSessionDimension:
    def test_end_session_counts_once_and_does_not_touch_wh(self, store):
        """session_count/duration/peak 来自会话闭合；wh 只由增量写。"""
        start = time.time() - 1800
        sid = store.start_session(1, "PD", start_time=start)
        store.add_energy_delta(1, 4.0, start + 60)
        store.flush()
        store.end_session(sid, total_wh=4.0, peak_power_w=25.0,
                          avg_voltage=20.0, avg_current=1.0,
                          duration_sec=1800, end_time=start + 1800)

        row = store.get_daily_energy()["days"][0]
        assert row["wh"] == 4.0
        assert row["sessions"] == 1
        assert row["peak_w"] == 25.0

        # 重复闭合（异常路径 / 关停收尾）不能再记一遍
        store.end_session(sid, total_wh=4.0, peak_power_w=25.0,
                          avg_voltage=20.0, avg_current=1.0,
                          duration_sec=1800, end_time=start + 1800)
        row = store.get_daily_energy()["days"][0]
        assert row["sessions"] == 1
        assert row["wh"] == 4.0

    def test_session_day_uses_start_time(self, store):
        """会话维度按 start_time 归日（与 get_energy_stats 一致）。"""
        midnight = datetime.now().replace(hour=0, minute=5, second=0, microsecond=0)
        start = local_ts(midnight - timedelta(minutes=20))     # 昨天 23:45
        sid = store.start_session(1, "PD", start_time=start)
        store.add_energy_delta(1, 10.0, start + 60)
        store.flush()
        store.end_session(sid, total_wh=10.0, peak_power_w=30.0,
                          avg_voltage=20.0, avg_current=0.5,
                          duration_sec=3600, end_time=start + 3600)
        days = store.get_daily_energy()["days"]
        assert [d["date"] for d in days] == [day_of(start)]
        assert days[0]["sessions"] == 1

    def test_zero_energy_day_not_returned(self, store):
        """只有会话、没有能量增量的日子不进 `days`（接口契约：wh > 0）。

        真实来源：升级前开始的会话在升级后闭合，增量表里没有它的帧。
        行本身仍保留（`session_count` 有值），只是不参与热力图。
        """
        ts = time.time() - 600
        sid = store.start_session(1, "PD", start_time=ts)
        store.end_session(sid, total_wh=4.0, peak_power_w=20.0,
                          avg_voltage=20.0, avg_current=1.0,
                          duration_sec=600, end_time=ts + 600)
        assert store.get_daily_energy()["days"] == []
        raw = store._conn.execute("SELECT session_count FROM daily_energy").fetchone()
        assert raw["session_count"] == 1


class TestRetention:
    def _insert_day(self, store, day: str, wh: float = 3.0):
        store._conn.execute(
            """INSERT INTO daily_energy
               (day, device_id, port, wh, session_count, duration_sec,
                peak_power_w, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
            (day, DEFAULT_DEVICE_ID, 1, wh, 1, 60, 5.0, time.time()))
        store._conn.commit()

    def test_cleanup_prunes_rows_older_than_a_year(self, store):
        old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        keep = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
        self._insert_day(store, old)
        self._insert_day(store, keep)

        store._cleanup_old_data()

        days = [d["date"] for d in store.get_daily_energy()["days"]]
        assert old not in days
        assert keep in days

    def test_session_retention_does_not_prune_daily(self, store):
        """即使会话只留 1 天，daily_energy 仍然保留一年。"""
        store.session_retention_days = 1
        day = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
        self._insert_day(store, day)

        store._cleanup_old_data()

        assert day in [d["date"] for d in store.get_daily_energy()["days"]]


class TestBackfill:
    def _make_session(self, store, start: float, wh: float, peak: float,
                      dur: int, device_id: str = DEFAULT_DEVICE_ID):
        sid = store.start_session(1, "PD", start_time=start, device_id=device_id)
        store.end_session(sid, total_wh=wh, peak_power_w=peak,
                          avg_voltage=20.0, avg_current=1.0,
                          duration_sec=dur, end_time=start + dur)

    def test_rebuilds_from_sessions(self, store):
        # 会话保留期只有 30 天，所以回填样本必须落在窗口内。
        d1 = datetime.now() - timedelta(days=3)
        t1 = local_ts(d1.replace(hour=10, minute=0, second=0, microsecond=0))
        t2 = t1 + 8 * 3600
        self._make_session(store, t1, 5.0, 20.0, 600)
        self._make_session(store, t2, 7.0, 30.0, 1200)
        # 门槛以下、没有会话的纯增量日
        t3 = t1 + 86400
        store.add_energy_delta(1, 0.3, t3)
        store.flush()

        result = store.backfill_daily_energy(replace=True)
        assert result["rows"] == 1
        assert result["sessions"] == 2

        days = {d["date"]: d for d in store.get_daily_energy()["days"]}
        assert days[day_of(t1)]["wh"] == 12.0
        assert days[day_of(t1)]["sessions"] == 2
        assert days[day_of(t1)]["peak_w"] == 30.0
        assert day_of(t3) not in days          # replace 语义：整表重建

    def test_replace_false_only_fills_missing(self, store):
        """非替换回填与脚本 --force 同义：只补缺失键，已有行（含增量）不动。"""
        t1 = local_ts((datetime.now() - timedelta(days=3)).replace(
            hour=10, minute=0, second=0, microsecond=0))
        self._make_session(store, t1, 5.0, 20.0, 600)
        t2 = t1 + 86400
        store.add_energy_delta(1, 0.4, t2)
        store.flush()

        # 已有行：t1 是会话收尾写的（wh=0），不许被覆盖
        store.backfill_daily_energy(replace=False)
        raw = {r["day"]: r["wh"] for r in store._conn.execute(
            "SELECT day, wh FROM daily_energy WHERE port = 1")}
        assert raw[day_of(t1)] == 0.0
        days = {d["date"]: d["wh"] for d in store.get_daily_energy()["days"]}
        assert days[day_of(t2)] == 0.4

        # 缺失行：清空后同样的调用要把会话能量补上
        store._conn.execute("DELETE FROM daily_energy")
        store._conn.commit()
        store.backfill_daily_energy(replace=False)
        days = {d["date"]: d["wh"] for d in store.get_daily_energy()["days"]}
        assert days[day_of(t1)] == 5.0
        assert day_of(t2) not in days      # 没有会话的增量日不会被造出来

    def test_backfill_per_device(self, store):
        ts = local_ts((datetime.now() - timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0))
        self._make_session(store, ts, 5.0, 20.0, 600, device_id="devA")
        self._make_session(store, ts, 9.0, 40.0, 900, device_id="devB")

        store.backfill_daily_energy(replace=True)

        a = DeviceHistory(store, "devA").get_daily_energy()["days"]
        b = DeviceHistory(store, "devB").get_daily_energy()["days"]
        assert a[0]["wh"] == 5.0 and a[0]["sessions"] == 1
        assert b[0]["wh"] == 9.0 and b[0]["sessions"] == 1

    def test_startup_backfill_runs_once(self, temp_db):
        h = PortHistory(db_path=temp_db)
        h.connect()
        try:
            assert h.get_meta("daily_stats_version") == "1"
        finally:
            h.close()

        # 第二个进程（重启）：版本标记已存在，不会再把整表清一遍
        h = PortHistory(db_path=temp_db)
        h.connect()
        try:
            h.add_energy_delta(1, 0.2, time.time())
            h.flush()
            assert h.get_meta("daily_stats_version") == "1"
            assert h.get_daily_energy()["days"]  # 纯增量行还在
        finally:
            h.close()


    def test_startup_backfill_never_clears_existing_rows(self, temp_db):
        """版本标记丢失时，启动回填也不能 DELETE 已有日汇总。"""
        t1 = local_ts((datetime.now() - timedelta(days=4)).replace(
            hour=10, minute=0, second=0, microsecond=0))
        t2 = t1 + 86400
        h = PortHistory(db_path=temp_db)
        h.connect()
        try:
            h.add_energy_delta(1, 12.5, t1)
            h.flush()
            self._make_session(h, t2, 5.0, 20.0, 600)
            # 抹掉"补缺失"的那一行和版本号：模拟旧库/手工恢复
            h._conn.execute("DELETE FROM daily_energy WHERE day = ?", (day_of(t2),))
            h._conn.execute("DELETE FROM meta WHERE key = 'daily_stats_version'")
            h._conn.commit()
        finally:
            h.close()

        h = PortHistory(db_path=temp_db)
        h.connect()
        try:
            assert h.get_meta("daily_stats_version") == "1"
            days = {d["date"]: d["wh"] for d in h.get_daily_energy()["days"]}
            assert days[day_of(t1)] == 12.5, "纯增量日被启动回填清掉了"
            assert days[day_of(t2)] == 5.0, "缺失的会话日没有补上"
        finally:
            h.close()


class TestAuditAndPurge:
    def test_audit_reports_drift(self, store):
        ts = time.time()
        sid = store.start_session(1, "PD", start_time=ts)
        store.end_session(sid, total_wh=5.0, peak_power_w=20.0,
                          avg_voltage=20.0, avg_current=1.0,
                          duration_sec=600, end_time=ts + 600)
        # 增量只记了 3 Wh → 漂移 2 Wh
        store.add_energy_delta(1, 3.0, ts)
        store.flush()

        audit = store.audit_daily_energy()
        assert audit["daily_wh"] == 3.0
        assert audit["session_wh"] == 5.0
        assert len(audit["drift"]) == 1
        assert audit["drift"][0]["daily_wh"] == 3.0

    def test_purge_daily_energy(self, store):
        for day in ("2026-01-01", "2026-02-01"):
            store._conn.execute(
                """INSERT INTO daily_energy
                   (day, device_id, port, wh, session_count, duration_sec,
                    peak_power_w, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                (day, DEFAULT_DEVICE_ID, 1, 1.0, 1, 60, 5.0, time.time()))
        store._conn.commit()

        assert store.purge_daily_energy("2026-02-01") == 1
        days = [d["date"] for d in store.get_daily_energy()["days"]]
        assert days == ["2026-02-01"]
