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
