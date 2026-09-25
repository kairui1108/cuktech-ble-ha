"""Tests for history.py - SQLite port history storage."""
import time
import pytest


class TestPortHistory:
    """Test PortHistory SQLite operations."""

    def test_record_and_query(self, history, mock_ble_data):
        """Test recording and querying port data."""
        history.record_port_data(1, mock_ble_data)
        rows = history.query_history(1, hours=1)
        assert len(rows) == 1
        assert rows[0]["voltage"] == 20.1
        assert rows[0]["current"] == 2.5

    def test_record_multiple_ports(self, history):
        """Test recording data for multiple ports."""
        for port in range(1, 5):
            history.record_port_data(port, {
                "voltage": 5.0 * port,
                "current": 1.0,
                "power": 5.0 * port,
                "active": True,
                "protocol": "PD",
            })

        for port in range(1, 5):
            rows = history.query_history(port, hours=1)
            assert len(rows) == 1
            assert rows[0]["voltage"] == 5.0 * port

    def test_query_with_interval(self, history):
        """Test query with downsampling interval."""
        # Record multiple data points
        for i in range(10):
            history.record_port_data(1, {
                "voltage": 20.0,
                "current": 1.0,
                "power": 20.0,
                "active": True,
                "protocol": "PD",
            })
            time.sleep(0.01)

        rows = history.query_history(1, hours=1, interval=1)
        assert len(rows) >= 1
        assert "bucket" in rows[0]

    def test_statistics(self, history, mock_ble_data):
        """Test statistics calculation."""
        for _ in range(5):
            history.record_port_data(1, mock_ble_data)

        stats = history.get_statistics(1, hours=1)
        assert stats["samples"] == 5
        assert stats["port"] == 1
        assert stats["voltage"]["avg"] == 20.1

    def test_export_csv(self, history, mock_ble_data):
        """Test CSV export."""
        history.record_port_data(1, mock_ble_data)
        csv_data = history.export_csv(1, hours=1)
        assert "timestamp" in csv_data
        assert "voltage" in csv_data
        assert "20.1" in csv_data

    def test_cleanup_old_data(self, history):
        """Test that old data is cleaned up."""
        # Insert old data
        history._conn.execute(
            "INSERT INTO port_history (timestamp, port, voltage, current, power, active, protocol) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time() - 200000, 1, 10.0, 1.0, 10.0, 1, "PD")
        )
        history._conn.commit()

        # Cleanup
        history._cleanup_old_data()

        # Verify old data is removed
        rows = history.query_history(1, hours=100)
        assert len(rows) == 0

    def test_thread_safety(self, history, mock_ble_data):
        """Test concurrent writes with threading lock."""
        import threading

        def write_data():
            for _ in range(10):
                history.record_port_data(1, mock_ble_data)

        threads = [threading.Thread(target=write_data) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = history.query_history(1, hours=1)
        assert len(rows) == 50  # 5 threads * 10 records

    def test_empty_database(self, history):
        """Test query on empty database."""
        rows = history.query_history(1, hours=1)
        assert rows == []

    def test_multi_port_query(self, history):
        """Test multi-port query."""
        for port in range(1, 5):
            history.record_port_data(port, {
                "voltage": 5.0,
                "current": 1.0,
                "power": 5.0,
                "active": True,
                "protocol": "PD",
            })

        rows = history.query_history_multi(1, 4, hours=1, interval=1)
        assert len(rows) == 4
        ports_in_result = {row["port"] for row in rows}
        assert ports_in_result == {1, 2, 3, 4}


class TestBatchCommit:
    """批量提交（H2）：缓冲写入后读取路径自动 flush，保证写后读一致性。"""

    def test_batch_records_visible_on_read(self, history, mock_ble_data):
        """多次快速写入（未到提交阈值）后，读取前自动落盘可见。"""
        for _ in range(3):
            history.record_port_data(1, mock_ble_data)
        rows = history.query_history(1, hours=1)
        assert len(rows) == 3

    def test_flush_commits_pending(self, history, mock_ble_data):
        """flush() 将缓冲区中的采样强制落盘。"""
        for _ in range(3):
            history.record_port_data(1, mock_ble_data)
        history.flush()
        assert len(history._pending) == 0
        rows = history.query_history(1, hours=1)
        assert len(rows) == 3

    def test_statistics_sees_buffered_records(self, history, mock_ble_data):
        """统计数据读取前同样 flush 缓冲，samples 计数完整。"""
        for _ in range(5):
            history.record_port_data(1, mock_ble_data)
        stats = history.get_statistics(1, hours=1)
        assert stats["samples"] == 5


class TestRuntimeMeta:
    """运行时开关持久化（DB meta 单源）。"""

    def test_session_recording_default_true(self, history):
        """meta 缺失时默认为开启（向后兼容）。"""
        assert history.get_session_recording() is True

    def test_session_recording_set_get(self, history):
        """set/get 往返。"""
        history.set_session_recording(False)
        assert history.get_session_recording() is False
        history.set_session_recording(True)
        assert history.get_session_recording() is True

    def test_session_recording_persists_across_reconnect(self, temp_db):
        """开关状态写入 DB 后，重连/重启仍然生效。"""
        from history import PortHistory

        h1 = PortHistory(db_path=temp_db)
        h1.connect()
        h1.set_session_recording(False)
        h1.close()

        h2 = PortHistory(db_path=temp_db)
        h2.connect()
        assert h2.get_session_recording() is False
        h2.close()

    def test_get_meta_after_close(self, history):
        """连接关闭后读取返回默认值，不抛异常。"""
        history.close()
        assert history.get_session_recording() is True

    def test_web_language_default_auto(self, history):
        """meta 缺失时默认为 auto（跟随系统）。"""
        assert history.get_web_language() == "auto"

    def test_web_language_set_get(self, history):
        """set/get 往返。"""
        history.set_web_language("zh-CN")
        assert history.get_web_language() == "zh-CN"
        history.set_web_language("en")
        assert history.get_web_language() == "en"
        history.set_web_language("auto")
        assert history.get_web_language() == "auto"

    def test_web_language_normalizes(self, history):
        """读取时归一化大小写/变体；未知值回退 auto。"""
        history.set_web_language("zh-cn")
        assert history.get_web_language() == "zh-CN"
        history.set_web_language("ZH-HANS")
        assert history.get_web_language() == "zh-CN"
        history.set_web_language("en-us")
        assert history.get_web_language() == "en"
        history.set_web_language("de")
        assert history.get_web_language() == "auto"

    def test_web_language_persists_across_reconnect(self, temp_db):
        """语言偏好写入 DB 后，重连/重启仍然生效。"""
        from history import PortHistory

        h1 = PortHistory(db_path=temp_db)
        h1.connect()
        h1.set_web_language("en")
        h1.close()

        h2 = PortHistory(db_path=temp_db)
        h2.connect()
        assert h2.get_web_language() == "en"
        h2.close()


class TestChargeLimits:
    """充电量阈值 meta 存取（单源 = history.db meta，键 charge_limit_wh）。"""

    def test_default_all_disabled(self, history):
        """未设置时四口全部禁用，mode 为默认值。"""
        limits = history.get_charge_limits()
        assert set(limits) == {"c1", "c2", "c3", "a"}
        for entry in limits.values():
            assert entry["wh"] == 0.0
            assert entry["mode"] == "once"

    def test_set_get_roundtrip(self, history):
        history.set_charge_limits({
            "c1": {"wh": 30.0, "mode": "always"},
            "c2": {"wh": 10, "mode": "once"},
        })
        limits = history.get_charge_limits()
        assert limits["c1"] == {"wh": 30.0, "mode": "always"}
        assert limits["c2"] == {"wh": 10.0, "mode": "once"}
        # 未提及的端口回落到禁用
        assert limits["c3"] == {"wh": 0.0, "mode": "once"}

    def test_set_normalizes_dirty_values(self, history):
        """非法值写库时归一为禁用，保证读写往返一致。"""
        history.set_charge_limits({
            "c1": {"wh": float("nan"), "mode": "always"},
            "c2": {"wh": float("inf"), "mode": "always"},
            "c3": {"wh": -5, "mode": "always"},
            "a": {"wh": 20, "mode": "sometimes"},
        })
        limits = history.get_charge_limits()
        assert limits["c1"] == {"wh": 0.0, "mode": "always"}
        assert limits["c2"] == {"wh": 0.0, "mode": "always"}
        assert limits["c3"] == {"wh": 0.0, "mode": "always"}
        # 合法 wh 保留，非法 mode 回落
        assert limits["a"] == {"wh": 20.0, "mode": "once"}

    def test_bare_number_form_accepted(self, history):
        """兼容简写 {"c1": 30}（DB 脏数据 / 手工改库）。"""
        history.set_meta("charge_limit_wh", '{"c1": 30}')
        limits = history.get_charge_limits()
        assert limits["c1"] == {"wh": 30.0, "mode": "once"}

    def test_corrupt_json_falls_back_to_disabled(self, history):
        for bad in ("not json", "[]", '"str"', "123"):
            history.set_meta("charge_limit_wh", bad)
            limits = history.get_charge_limits()
            assert all(e["wh"] == 0.0 for e in limits.values()), f"{bad!r} should disable all"

    def test_unknown_keys_ignored(self, history):
        history.set_meta("charge_limit_wh", '{"c9": {"wh": 99}, "c1": {"wh": 5}}')
        limits = history.get_charge_limits()
        assert set(limits) == {"c1", "c2", "c3", "a"}
        assert limits["c1"]["wh"] == 5.0

    def test_persists_across_reconnect(self, temp_db):
        from history import PortHistory

        h1 = PortHistory(db_path=temp_db)
        h1.connect()
        h1.set_charge_limits({"c1": {"wh": 42.0, "mode": "always"}})
        h1.close()

        h2 = PortHistory(db_path=temp_db)
        h2.connect()
        assert h2.get_charge_limits()["c1"] == {"wh": 42.0, "mode": "always"}
        h2.close()

    def test_once_limit_survives_restart(self, temp_db):
        """重启不消费 once 限额（决策 C：重启不算会话终止）。

        once 只在"观察到真实会话终止"或"触发关断"时消费，因此限额必须能
        跨越进程重启存活；always 同理。
        """
        from history import PortHistory

        h1 = PortHistory(db_path=temp_db)
        h1.connect()
        h1.set_charge_limits({
            "c1": {"wh": 30.0, "mode": "once"},
            "c2": {"wh": 20.0, "mode": "always"},
        })
        h1.close()

        h2 = PortHistory(db_path=temp_db)
        h2.connect()   # 模拟新的进程启动
        limits = h2.get_charge_limits()
        assert limits["c1"] == {"wh": 30.0, "mode": "once"}
        assert limits["c2"] == {"wh": 20.0, "mode": "always"}
        h2.close()


class TestSessionCleanup:
    """会话清理（H5）：闭环会话过期回收 + 崩溃孤儿会话启动回收。"""

    def test_cleanup_removes_expired_closed_sessions(self, history):
        """过期闭环会话及其采样点应被清理（此前 charge_sessions 永不删除）。"""
        sid = history.start_session(1, protocol="PD")
        history.record_charge_point(sid, 20.0, 2.5, 50.0, "PD")
        history.end_session(sid, 1.0, 50.0, 20.0, 2.5, 600)
        # 把该会话的 end_time 改到保留期之外
        history._conn.execute(
            "UPDATE charge_sessions SET end_time = ? WHERE id = ?",
            (time.time() - 200000, sid))
        history._conn.commit()

        history._cleanup_old_data()

        sessions, _ = history.get_sessions(port=1, period="all")
        assert len(sessions) == 0
        assert history.get_session_points(sid) == []

    def test_cleanup_keeps_recent_closed_sessions(self, history):
        """保留期内闭环会话不受清理影响。"""
        sid = history.start_session(1, protocol="PD")
        history.end_session(sid, 1.0, 50.0, 20.0, 2.5, 600)
        history._cleanup_old_data()
        sessions, _ = history.get_sessions(port=1, period="all")
        assert any(s["id"] == sid for s in sessions)

    def test_connect_reaps_orphan_sessions(self, temp_db):
        """崩溃遗留的未结束会话（end_time IS NULL）在下次启动 connect 时被清理。"""
        from history import PortHistory

        h1 = PortHistory(db_path=temp_db)
        h1.connect()
        sid = h1.start_session(1, protocol="PD")
        h1.record_charge_point(sid, 20.0, 2.5, 50.0, "PD")
        # 不调用 end_session，模拟进程崩溃
        h1.close()

        h2 = PortHistory(db_path=temp_db)
        h2.connect()  # 应回收孤儿会话
        assert h2.get_session_points(sid) == []
        sessions, _ = h2.get_sessions(port=1, period="all")
        assert len(sessions) == 0
        h2.close()
