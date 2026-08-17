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
