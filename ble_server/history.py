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

    # 批量提交参数：降低高频采样下的写放大（每次 BLE 推送不再单独 COMMIT）
    BATCH_SIZE = 50          # 缓冲行数达到该值即强制提交
    BATCH_INTERVAL = 1.0     # 距上次提交超过该秒数即强制提交

    def __init__(self, db_path: str = DEFAULT_DB_PATH, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.db_path = db_path
        self.retention_days = retention_days
        self._conn: Optional[sqlite3.Connection] = None
        self._db_lock = threading.Lock()  # 保护所有读写操作
        self._last_cleanup = 0
        self._last_wal_checkpoint = 0
        self._pending: list[tuple] = []   # 待批量写入的 port_history 行
        self._last_commit = 0.0

    def connect(self):
        """Open database connection and create tables."""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")  # checkpoint every 1000 pages
        self._create_tables()
        self._reap_orphan_sessions()
        self._cleanup_old_data()
        _LOGGER.info("History database connected: %s", self.db_path)

    def close(self):
        """Close database connection with checkpoint and graceful shutdown."""
        if self._conn:
            with self._db_lock:
                # 落盘缓冲区中的采样，避免关闭时丢失最近 ~1s 的数据
                self._flush_pending()
            try:
                # Try to checkpoint WAL before closing
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            _LOGGER.info("History database closed")

    def _create_tables(self):
        """Create database tables and run migrations."""
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

            CREATE TABLE IF NOT EXISTS charge_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                port INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL,
                total_wh REAL DEFAULT 0,
                avg_power_w REAL DEFAULT 0,
                peak_power_w REAL DEFAULT 0,
                avg_voltage REAL DEFAULT 0,
                avg_current REAL DEFAULT 0,
                duration_sec INTEGER DEFAULT 0,
                protocol TEXT DEFAULT '',
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_charge_sessions_port ON charge_sessions(port);
            CREATE INDEX IF NOT EXISTS idx_charge_sessions_start ON charge_sessions(start_time);

            CREATE TABLE IF NOT EXISTS charge_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                voltage REAL,
                current REAL,
                power REAL,
                protocol TEXT DEFAULT '',
                FOREIGN KEY (session_id) REFERENCES charge_sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_charge_points_session ON charge_points(session_id);
            CREATE INDEX IF NOT EXISTS idx_charge_points_timestamp ON charge_points(timestamp);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # Migration: add protocol column to charge_points if missing
        try:
            self._conn.execute("SELECT protocol FROM charge_points LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE charge_points ADD COLUMN protocol TEXT DEFAULT ''")
            self._conn.commit()

    # ── Runtime meta storage (single source for runtime toggles) ──

    def get_meta(self, key: str, default: str = "") -> str:
        """Read a runtime meta value from the DB. Returns default if absent/failed."""
        if not self._conn:
            return default
        with self._db_lock:
            try:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
                return row["value"] if row else default
            except Exception as e:
                _LOGGER.error("Failed to read meta %s: %s", key, e)
                return default

    def set_meta(self, key: str, value: str):
        """Persist a runtime meta value (upsert)."""
        if not self._conn:
            return
        with self._db_lock:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (key, value))
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to write meta %s: %s", key, e)

    def get_session_recording(self) -> bool:
        """Whether charge session recording is enabled (DB meta, default True)."""
        value = self.get_meta("session_recording", "true").strip().lower()
        return value not in ("0", "false", "no", "off")

    def set_session_recording(self, enabled: bool) -> None:
        """Persist the charge session recording toggle (DB meta, survives restart)."""
        self.set_meta("session_recording", "true" if enabled else "false")

    def _checkpoint_wal(self):
        """Run WAL checkpoint if enough pages have accumulated."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def _reap_orphan_sessions(self):
        """启动时清理崩溃遗留的未结束会话（end_time IS NULL）及其采样点。

        进程崩溃（未走 on_shutdown 的 _close_active_sessions）会留下
        end_time IS NULL / total_wh=0 的会话行；重启后内存会话状态已丢失，
        这类行不可能再被 end_session 更新，属于永久孤儿数据——get_sessions
        按 total_wh>0 过滤使它们永远不可见、也永远不被清理。
        仅在启动时执行（运行时正常进行中的会话 end_time IS NULL，不可删）。
        """
        if not self._conn:
            return
        try:
            self._conn.execute(
                """DELETE FROM charge_points WHERE session_id IN
                   (SELECT id FROM charge_sessions WHERE end_time IS NULL)""")
            removed = self._conn.execute(
                "DELETE FROM charge_sessions WHERE end_time IS NULL").rowcount
            self._conn.commit()
            if removed:
                _LOGGER.info("Reaped %d orphan charge session(s) from unclean shutdown", removed)
        except Exception as e:
            _LOGGER.error("Failed to reap orphan charge sessions: %s", e)

    def _cleanup_old_data(self):
        """Remove data older than retention period (port samples + closed sessions)."""
        cutoff = time.time() - (self.retention_days * 86400)
        self._conn.execute("DELETE FROM port_history WHERE timestamp < ?", (cutoff,))
        # 清理过期闭环会话及其采样点（charge_sessions 行此前从不删除，会无限累积）
        self._conn.execute(
            """DELETE FROM charge_points WHERE session_id IN
               (SELECT id FROM charge_sessions WHERE end_time IS NOT NULL AND end_time < ?)""",
            (cutoff,))
        self._conn.execute(
            """DELETE FROM charge_sessions WHERE end_time IS NOT NULL AND end_time < ?""",
            (cutoff,))
        self._conn.commit()

    def flush(self):
        """强制将缓冲区中的采样批量落盘提交。线程安全。"""
        if not self._conn:
            return
        with self._db_lock:
            self._flush_pending()

    def _flush_pending(self):
        """将缓冲行批量 INSERT 并提交。调用方必须已持有 _db_lock。"""
        if not self._pending:
            return
        try:
            self._conn.executemany(
                """INSERT INTO port_history (timestamp, port, voltage, current, power, active, protocol)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                self._pending,
            )
            self._pending.clear()
            self._conn.commit()
            self._last_commit = time.time()
            # 周期性清理/checkpoint 仍挂在提交路径上，但只按时间间隔触发（每 1h / 5min）
            if self._last_commit - self._last_cleanup > 3600:
                self._cleanup_old_data()
                self._last_cleanup = self._last_commit
            if self._last_commit - self._last_wal_checkpoint > 300:
                self._checkpoint_wal()
                self._last_wal_checkpoint = self._last_commit
        except Exception as e:
            _LOGGER.error("Failed to record port data: %s", e)
            self._pending.clear()
            try:
                self._conn.rollback()
            except Exception:
                pass

    def record_port_data(self, port: int, data: dict):
        """Record port data to database (synchronous, called from async via executor).

        批量缓冲：攒到 BATCH_SIZE 或距上次提交超过 BATCH_INTERVAL 再批量写入，
        大幅降低高频采样下的 COMMIT 次数与写放大。读取路径会自动 flush 未落盘
        的缓冲行，保证"写入后立即读取可见"。
        """
        if not self._conn:
            return
        row = (
            time.time(),
            port,
            data.get("voltage"),
            data.get("current"),
            data.get("power"),
            1 if data.get("active") else 0,
            data.get("protocol"),
        )
        with self._db_lock:
            self._pending.append(row)
            now = time.time()
            if (len(self._pending) >= self.BATCH_SIZE
                    or now - self._last_commit >= self.BATCH_INTERVAL):
                self._flush_pending()

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

        # 落盘缓冲采样，保证"写入后立即读取"的一致性（批量提交引入 ≤1s 缓冲）
        self.flush()

        cutoff = time.time() - (hours * 3600)

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

        self.flush()

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
                COALESCE(
                    (SELECT SUM(p.power * (p.timestamp - p.prev_ts)) / 3600.0
                     FROM (
                         SELECT timestamp, power,
                                LAG(timestamp) OVER (ORDER BY timestamp) as prev_ts
                         FROM port_history
                         WHERE port = ? AND timestamp >= ? AND active = 1
                     ) p
                     WHERE p.prev_ts IS NOT NULL),
                0) as energy_wh
            FROM port_history
            WHERE port = ? AND timestamp >= ?""",
            (port, cutoff, port, cutoff)
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
                "total_wh": round(row["energy_wh"], 2) if row["energy_wh"] is not None else 0,
            },
            "active_ratio": round(row["active_count"] / row["samples"], 2) if row["samples"] > 0 else 0,
        }

    def export_csv(self, port: int, hours: int = 24) -> str:
        """Export port history as CSV string."""
        if not self._conn:
            return ""

        self.flush()

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

        self.flush()

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

    # ── Charge Session Management ──

    def start_session(self, port: int, protocol: str = "") -> int:
        """Start a new charge session, return session_id."""
        if not self._conn:
            return 0
        with self._db_lock:
            try:
                cursor = self._conn.execute(
                    """INSERT INTO charge_sessions (port, start_time, protocol)
                       VALUES (?, ?, ?)""",
                    (port, time.time(), protocol),
                )
                self._conn.commit()
                return cursor.lastrowid
            except Exception as e:
                _LOGGER.error("Failed to start session: %s", e)
                return 0

    def record_charge_point(self, session_id: int, voltage: float,
                            current: float, power: float, protocol: str = ""):
        """Record a single data point for a charge session."""
        if not self._conn or not session_id:
            return
        with self._db_lock:
            try:
                self._conn.execute(
                    """INSERT INTO charge_points (session_id, timestamp, voltage, current, power, protocol)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session_id, time.time(), voltage, current, power, protocol),
                )
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to record charge point: %s", e)

    def end_session(self, session_id: int, total_wh: float, peak_power_w: float,
                    avg_voltage: float, avg_current: float, duration_sec: int):
        """End a charge session with final stats."""
        if not self._conn or not session_id:
            return
        avg_power = total_wh / (duration_sec / 3600.0) if duration_sec > 0 else 0
        with self._db_lock:
            try:
                self._conn.execute(
                    """UPDATE charge_sessions SET
                       end_time = ?, total_wh = ?, avg_power_w = ?,
                       peak_power_w = ?, avg_voltage = ?, avg_current = ?,
                       duration_sec = ?
                       WHERE id = ?""",
                    (time.time(), round(total_wh, 4), round(avg_power, 2),
                     round(peak_power_w, 2), round(avg_voltage, 2),
                     round(avg_current, 2), duration_sec, session_id),
                )
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to end session: %s", e)

    def delete_session(self, session_id: int):
        """Delete a session and its points (for 0Wh sessions)."""
        if not self._conn or not session_id:
            return
        with self._db_lock:
            try:
                self._conn.execute("DELETE FROM charge_points WHERE session_id = ?", (session_id,))
                self._conn.execute("DELETE FROM charge_sessions WHERE id = ?", (session_id,))
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to delete session: %s", e)

    def get_sessions(self, port: Optional[int] = None, period: str = "today",
                     limit: int = 10, offset: int = 0) -> tuple:
        """Query charge sessions."""
        if not self._conn:
            return [], 0

        now = time.time()
        if period == "today":
            from datetime import datetime
            cutoff = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
        elif period == "yesterday":
            from datetime import datetime
            today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
            cutoff = today_start - 86400
            limit_end = today_start
        elif period == "week":
            cutoff = now - 7 * 86400
        elif period == "month":
            cutoff = now - 30 * 86400
        else:
            cutoff = 0

        query = """SELECT id, port, start_time, end_time, total_wh, avg_power_w,
                   peak_power_w, avg_voltage, avg_current, duration_sec, protocol,
                   COUNT(*) OVER() AS total
                   FROM charge_sessions WHERE start_time >= ? AND total_wh > 0"""
        params = [cutoff]

        if port is not None:
            query += " AND port = ?"
            params.append(port)

        if period == "yesterday":
            query += " AND start_time < ?"
            params.append(limit_end)

        query += " ORDER BY end_time IS NULL DESC, start_time DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(query, params).fetchall()
        total = rows[0]["total"] if rows else 0

        return [dict(row) for row in rows], total

    def get_session_points(self, session_id: int) -> list[dict]:
        """Get all data points for a charge session."""
        if not self._conn:
            return []
        rows = self._conn.execute(
            """SELECT timestamp, voltage, current, power, protocol
               FROM charge_points WHERE session_id = ?
               ORDER BY timestamp""",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_energy_stats(self, period: str = "today") -> dict:
        """Get aggregated energy statistics."""
        if not self._conn:
            return {"period": period, "total_wh": 0, "session_count": 0}

        now = time.time()
        if period == "today":
            from datetime import datetime
            cutoff = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
        elif period == "yesterday":
            from datetime import datetime
            today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
            cutoff = today_start - 86400
            limit_end = today_start
        elif period == "week":
            cutoff = now - 7 * 86400
        elif period == "month":
            cutoff = now - 30 * 86400
        else:
            cutoff = 0

        params1 = [cutoff]
        params2 = [cutoff]
        extra = ""
        if period == "yesterday":
            extra = " AND start_time < ?"
            params1.append(limit_end)
            params2.append(limit_end)

        row = self._conn.execute(
            f"""SELECT
                COUNT(*) as session_count,
                COALESCE(SUM(total_wh), 0) as total_wh,
                COALESCE(MAX(peak_power_w), 0) as peak_power_w,
                COALESCE(SUM(duration_sec), 0) as total_duration_sec
            FROM charge_sessions WHERE start_time >= ?{extra} AND total_wh > 0""",
            params1,
        ).fetchone()

        by_port = self._conn.execute(
            f"""SELECT port, COALESCE(SUM(total_wh), 0) as wh, COUNT(*) as count
               FROM charge_sessions WHERE start_time >= ?{extra} AND total_wh > 0
               GROUP BY port""",
            params2,
        ).fetchall()

        # Calculate avg power from total energy and duration (more accurate than DB avg)
        total_dur = row["total_duration_sec"] or 0
        total_wh = row["total_wh"] or 0
        avg_power = round(total_wh / (total_dur / 3600), 1) if total_dur > 0 else 0

        return {
            "period": period,
            "total_wh": round(total_wh, 2),
            "session_count": row["session_count"],
            "avg_power_w": avg_power,
            "peak_power_w": round(row["peak_power_w"], 1),
            "total_duration_sec": total_dur,
            "by_port": {str(r["port"]): {"wh": round(r["wh"], 2), "count": r["count"]}
                        for r in by_port},
        }
