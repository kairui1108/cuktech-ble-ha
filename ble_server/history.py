"""CUKTECH BLE Server - SQLite history storage for port data."""
import asyncio
import csv
import io
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger("cuktech_history")

DEFAULT_RETENTION_DAYS = 2
DEFAULT_DB_PATH = "port_history.db"


class PortHistory:
    """SQLite-based port history storage."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.db_path = db_path
        self.retention_days = retention_days
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = threading.Lock()
        self._last_cleanup = 0

    def connect(self):
        """Open database connection and create tables."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()
        self._cleanup_old_data()
        _LOGGER.info("History database connected: %s", self.db_path)

    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _create_tables(self):
        """Create database tables."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS port_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                port INTEGER NOT NULL,
                voltage REAL,
                current REAL,
                power REAL,
                active INTEGER,
                protocol TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_port_history_port ON port_history(port);
            CREATE INDEX IF NOT EXISTS idx_port_history_timestamp ON port_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_port_history_port_time ON port_history(port, timestamp);
        """)

    def _cleanup_old_data(self):
        """Remove data older than retention period."""
        cutoff = time.time() - (self.retention_days * 86400)
        self._conn.execute("DELETE FROM port_history WHERE timestamp < ?", (cutoff,))
        self._conn.commit()

    def record_port_data(self, port: int, data: dict):
        """Record port data to database (synchronous, called from async via executor)."""
        if not self._conn:
            return
        with self._write_lock:
            try:
                self._conn.execute(
                    """INSERT INTO port_history (timestamp, port, voltage, current, power, active, protocol)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        time.time(),
                        port,
                        data.get("voltage"),
                        data.get("current"),
                        data.get("power"),
                        1 if data.get("active") else 0,
                        data.get("protocol"),
                    )
                )
                self._conn.commit()
                # Periodic cleanup every hour
                now = time.time()
                if now - self._last_cleanup > 3600:
                    self._cleanup_old_data()
                    self._last_cleanup = now
            except Exception as e:
                _LOGGER.error("Failed to record port data: %s", e)

    def query_history(
        self,
        port: int,
        hours: int = 24,
        interval: Optional[int] = None
    ) -> list[dict]:
        """Query port history with optional downsampling.

        Args:
            port: Port number (1-4)
            hours: Number of hours to query
            interval: Aggregation interval in seconds (None = raw data)
        """
        if not self._conn:
            return []

        cutoff = time.time() - (hours * 3600)

        # WAL mode allows concurrent reads without locking
        if interval:
            rows = self._conn.execute(
                """SELECT
                    (CAST(timestamp / ? AS INTEGER) * ?) as bucket,
                    AVG(voltage) as voltage,
                    AVG(current) as current,
                    AVG(power) as power,
                    MAX(active) as active,
                    COUNT(*) as samples
                FROM port_history
                WHERE port = ? AND timestamp >= ?
                GROUP BY bucket
                ORDER BY bucket""",
                (interval, interval, port, cutoff)
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT timestamp, voltage, current, power, active, protocol
                FROM port_history
                WHERE port = ? AND timestamp >= ?
                ORDER BY timestamp""",
                (port, cutoff)
            ).fetchall()

        return [dict(row) for row in rows]

    def get_statistics(self, port: int, hours: int = 24) -> dict:
        """Get statistical summary for a port."""
        if not self._conn:
            return {}

        cutoff = time.time() - (hours * 3600)
        row = self._conn.execute(
            """SELECT
                COUNT(*) as samples,
                MIN(timestamp) as first_seen,
                MAX(timestamp) as last_seen,
                AVG(voltage) as avg_voltage,
                MAX(voltage) as max_voltage,
                MIN(voltage) as min_voltage,
                AVG(current) as avg_current,
                MAX(current) as max_current,
                AVG(power) as avg_power,
                MAX(power) as max_power,
                SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) as active_count,
                SUM(power) * 5 / 3600 as energy_wh
            FROM port_history
            WHERE port = ? AND timestamp >= ?""",
            (port, cutoff)
        ).fetchone()

        if not row or row["samples"] == 0:
            return {"port": port, "hours": hours, "samples": 0}

        return {
            "port": port,
            "hours": hours,
            "samples": row["samples"],
            "first_seen": datetime.fromtimestamp(row["first_seen"]).isoformat() if row["first_seen"] else None,
            "last_seen": datetime.fromtimestamp(row["last_seen"]).isoformat() if row["last_seen"] else None,
            "voltage": {
                "avg": round(row["avg_voltage"], 2) if row["avg_voltage"] else None,
                "min": round(row["min_voltage"], 2) if row["min_voltage"] else None,
                "max": round(row["max_voltage"], 2) if row["max_voltage"] else None,
            },
            "current": {
                "avg": round(row["avg_current"], 2) if row["avg_current"] else None,
                "max": round(row["max_current"], 2) if row["max_current"] else None,
            },
            "power": {
                "avg": round(row["avg_power"], 2) if row["avg_power"] else None,
                "max": round(row["max_power"], 2) if row["max_power"] else None,
                "total_wh": round(row["energy_wh"], 2) if row["energy_wh"] else 0,
            },
            "active_ratio": round(row["active_count"] / row["samples"], 2) if row["samples"] > 0 else 0,
        }

    def export_csv(self, port: int, hours: int = 24) -> str:
        """Export port history as CSV string."""
        if not self._conn:
            return ""

        cutoff = time.time() - (hours * 3600)
        rows = self._conn.execute(
            """SELECT timestamp, voltage, current, power, active, protocol
            FROM port_history
            WHERE port = ? AND timestamp >= ?
            ORDER BY timestamp""",
            (port, cutoff)
        ).fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp", "datetime", "voltage", "current", "power", "active", "protocol"])

        for row in rows:
            writer.writerow([
                row["timestamp"],
                datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
                row["voltage"],
                row["current"],
                row["power"],
                "yes" if row["active"] else "no",
                row["protocol"],
            ])

        return output.getvalue()

    def query_history_multi(self, start_port: int, end_port: int, hours: float, interval: int) -> list[dict]:
        """Query history for multiple ports in a single query."""
        if not self._conn:
            return []

        cutoff = time.time() - (hours * 3600)
        rows = self._conn.execute(
            """SELECT port,
                (CAST(timestamp / ? AS INTEGER) * ?) as bucket,
                AVG(voltage) as voltage,
                AVG(current) as current,
                AVG(power) as power,
                COUNT(*) as samples
            FROM port_history
            WHERE port >= ? AND port <= ? AND timestamp >= ?
            GROUP BY port, bucket
            ORDER BY port, bucket""",
            (interval, interval, start_port, end_port, cutoff)
        ).fetchall()

        return [dict(row) for row in rows]
