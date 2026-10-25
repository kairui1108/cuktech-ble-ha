"""`tools/history_down.py` 的回滚演练（设计文档 §6.1、§7 P1 验收"往返演练"）。

锁死三件事：

1. 归档后主表**只剩 primary 的行**——旧代码"不带 device_id 的查询"因此不会串台；
2. `charge_points` 的归档必须按**会话归属**判断（它自己没有 device_id 列），否则旧
   代码按 session_id 仍能画出第二台设备的曲线；
3. 幂等：重跑零改动，`--dry-run` 一个字节都不写。
"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import history_down  # noqa: E402
from history import PortHistory  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """一台 primary + 一台 desk 的真实 schema 库（走生产 `connect()` 建表）。"""
    path = tmp_path / "history.db"
    store = PortHistory(db_path=str(path))
    store.connect()
    try:
        # primary（default）数据
        store.record_port_data(1, {"voltage": 5.0, "current": 1.0, "power": 5.0,
                                   "active": True, "protocol": "PD"})
        sid_p = store.start_session(1, "PD")
        store.record_charge_points(sid_p, [(1764000000.0, 5.0, 1.0, 5.0, "PD")])
        store.end_session(sid_p, 1.0, 5.0, 5.0, 1.0, 60)
        # desk（非 primary）数据
        store.record_port_data(1, {"voltage": 9.0, "current": 2.0, "power": 18.0,
                                   "active": True, "protocol": "PD"},
                               device_id="desk")
        sid_d = store.start_session(1, "PD", device_id="desk")
        store.record_charge_points(sid_d, [(1764000000.0, 9.0, 2.0, 18.0, "PD")])
        store.end_session(sid_d, 2.0, 18.0, 9.0, 2.0, 60)  # end_session 按全局唯一 id 更新
        store.set_charge_limits({"c1": {"wh": 5.0, "mode": "once"}}, device_id="desk")
        store.set_charge_limits({"c1": {"wh": 3.0, "mode": "always"}})
        store.flush()
    finally:
        store.close()
    return str(path)


def _rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _counts(path):
    return {t: _rows(path, f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
            for t in ("port_history", "charge_sessions", "charge_points", "meta")}


class TestArchive:
    def test_main_tables_keep_only_primary(self, db):
        before = _counts(db)
        stats = history_down.archive_non_primary(db)
        assert stats["port_history"] == 1
        assert stats["charge_sessions"] == 1
        assert stats["charge_points"] == 1
        assert stats["meta"] == 1

        after = _counts(db)
        assert after["port_history"] == before["port_history"] - 1
        assert after["charge_sessions"] == before["charge_sessions"] - 1
        assert after["charge_points"] == before["charge_points"] - 1
        assert after["meta"] == before["meta"] - 1
        assert history_down.remaining_devices(db) == ["default"]

    def test_old_code_queries_no_longer_see_other_devices(self, db):
        """旧代码的查询**不带** device_id —— 归档后它只看得到 primary 的行。"""
        history_down.archive_non_primary(db)
        rows = _rows(db, "SELECT port, voltage, current FROM port_history WHERE port = ?",
                     (1,))
        assert [r["current"] for r in rows] == [1.0], "旧查询读到了别的设备的行"
        sessions = _rows(db, "SELECT id, total_wh FROM charge_sessions WHERE port = ?",
                         (1,))
        assert [s["total_wh"] for s in sessions] == [1.0]
        points = _rows(db, "SELECT voltage, current FROM charge_points")
        assert [(p["voltage"], p["current"]) for p in points] == [(5.0, 1.0)]

    def test_points_are_archived_by_session_ownership(self, db):
        """charge_points 没有 device_id：只能按会话归属归档，不能漏也不能多。"""
        history_down.archive_non_primary(db)
        archived = _rows(db, "SELECT voltage, current FROM charge_points_archive")
        assert [(p["voltage"], p["current"]) for p in archived] == [(9.0, 2.0)]

    def test_meta_namespace_keys_archived_and_primary_kept(self, db):
        history_down.archive_non_primary(db)
        keys = {r["key"] for r in _rows(db, "SELECT key FROM meta")}
        assert "charge_limit_wh" in keys, "primary 的裸键必须留在主库"
        assert not any(k.startswith("desk:") for k in keys)
        assert "desk:charge_limit_wh" in {
            r["key"] for r in _rows(db, "SELECT key FROM meta_archive")}

    def test_idempotent_second_run_changes_nothing(self, db):
        history_down.archive_non_primary(db)
        snapshot = {t: _rows(db, f"SELECT * FROM {t} ORDER BY 1")
                    for t in ("port_history", "charge_sessions", "charge_points", "meta")}
        again = history_down.archive_non_primary(db)
        assert again == {"charge_points": 0, "charge_sessions": 0,
                         "port_history": 0, "meta": 0}
        after = {t: _rows(db, f"SELECT * FROM {t} ORDER BY 1") for t in snapshot}
        assert after == snapshot
        # 归档表也没有出现重复（原 id 是主键 + INSERT OR IGNORE）
        assert _rows(db, "SELECT COUNT(*) AS n FROM charge_sessions_archive")[0]["n"] == 1

    def test_dry_run_writes_nothing(self, db):
        before = _counts(db)
        stats = history_down.archive_non_primary(db, dry_run=True)
        assert stats["port_history"] == 1
        assert _counts(db) == before
        assert _rows(db, "SELECT name FROM sqlite_master WHERE name LIKE '%_archive'") == []


class TestLegacySchema:
    @pytest.fixture
    def legacy_db(self, tmp_path):
        """真正的旧库：没有 device_id 列，且索引也在（模拟升级前的历史库）。"""
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE port_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL, port INTEGER NOT NULL,
                voltage REAL, current REAL, power REAL, active INTEGER, protocol TEXT);
            CREATE TABLE charge_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, port INTEGER NOT NULL,
                start_time REAL NOT NULL, end_time REAL, total_wh REAL DEFAULT 0,
                avg_power_w REAL DEFAULT 0, peak_power_w REAL DEFAULT 0,
                avg_voltage REAL DEFAULT 0, avg_current REAL DEFAULT 0,
                duration_sec INTEGER DEFAULT 0, protocol TEXT DEFAULT '',
                created_at REAL DEFAULT (strftime('%s','now')));
            CREATE TABLE charge_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
                timestamp REAL NOT NULL, voltage REAL, current REAL, power REAL,
                protocol TEXT DEFAULT '');
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta (key, value) VALUES ('charge_limit_wh', '{}');
        """)
        # 时间戳取"刚发生"：升级时 connect() 会按 retention_days 清理过期行
        conn.execute(
            "INSERT INTO port_history (timestamp, port, voltage, current, power,"
            " active, protocol) VALUES (?, 1, 5.0, 1.0, 5.0, 1, 'PD')",
            (time.time() - 60,))
        conn.commit()
        conn.close()
        return str(path)

    def test_legacy_db_is_noop(self, legacy_db):
        """旧库（无 device_id）→ 没有任何可归档的东西，脚本必须什么都不做。"""
        stats = history_down.archive_non_primary(legacy_db)
        assert "note" in stats
        assert _counts(legacy_db)["port_history"] == 1
        assert history_down.remaining_devices(legacy_db) == ["default"]

    def test_legacy_db_starts_up_and_migrates(self, legacy_db):
        """旧库必须能直接启动：ALTER 加列 → 老数据归 default → device 索引建出来。

        回归：`_create_tables` 的第一段 executescript 曾把 `device_id` 索引写在
        ALTER 之前，旧库（还没有该列）会在 `connect()` 直接抛
        `no such column: device_id` —— 服务器对老用户直接起不来。
        """
        store = PortHistory(db_path=legacy_db)
        store.connect()
        try:
            rows = store.query_history(1, hours=1, device_id="default")
            assert rows, "升级后老数据必须仍然属于 primary"
            assert [r["current"] for r in rows] == [1.0]
            assert store.get_charge_limits()  # 老 meta 裸键照旧可读
        finally:
            store.close()
        indexes = {r["name"] for r in _rows(
            legacy_db, "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_port_history_device_port_time" in indexes
        assert "idx_charge_sessions_device" in indexes


class TestUpgradeDownRoundTrip:
    def test_upgrade_then_down_restores_old_query_semantics(self, tmp_path):
        """往返演练：旧库 → 升级（加列，行归 default）→ 加一台设备 → 降级 → 旧查询一致。"""
        path = tmp_path / "roundtrip.db"
        # ① 旧库
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE port_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL, port INTEGER NOT NULL,
                voltage REAL, current REAL, power REAL, active INTEGER, protocol TEXT);
            CREATE TABLE charge_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, port INTEGER NOT NULL,
                start_time REAL NOT NULL, end_time REAL, total_wh REAL DEFAULT 0,
                avg_power_w REAL DEFAULT 0, peak_power_w REAL DEFAULT 0,
                avg_voltage REAL DEFAULT 0, avg_current REAL DEFAULT 0,
                duration_sec INTEGER DEFAULT 0, protocol TEXT DEFAULT '',
                created_at REAL DEFAULT (strftime('%s','now')));
            CREATE TABLE charge_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
                timestamp REAL NOT NULL, voltage REAL, current REAL, power REAL,
                protocol TEXT DEFAULT '');
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # 旧行必须"刚发生"：升级时 `connect()` 会按 retention_days 清理过期数据
        now = time.time()
        conn.execute(
            "INSERT INTO port_history (timestamp, port, voltage, current, power,"
            " active, protocol) VALUES (?, 1, 5.0, 1.0, 5.0, 1, 'PD')", (now - 60,))
        conn.execute(
            "INSERT INTO charge_sessions (port, start_time, end_time, total_wh,"
            " duration_sec, protocol) VALUES (1, ?, ?, 1.0, 60, 'PD')",
            (now - 120, now - 60,))
        conn.commit()
        conn.close()

        # ② 升级：生产代码 ALTER 加列，既有行归 default
        store = PortHistory(db_path=str(path))
        store.connect()
        try:
            rows = store.query_history(1, hours=1, device_id="default")
            assert [r["current"] for r in rows] == [1.0], "升级后老数据必须归 primary"
            # ③ 新增一台设备的数据
            store.record_port_data(1, {"voltage": 9.0, "current": 2.0, "power": 18.0,
                                       "active": True, "protocol": "PD"},
                                   device_id="desk")
            store.flush()
        finally:
            store.close()

        # ④ 降级：归档非 primary
        history_down.archive_non_primary(str(path))

        # ⑤ 旧代码的查询结果与升级前**完全一致**
        assert [(r["current"]) for r in _rows(
            path, "SELECT current FROM port_history WHERE port = ?", (1,))] == [1.0]
        assert [(r["total_wh"]) for r in _rows(
            path, "SELECT total_wh FROM charge_sessions WHERE port = ?", (1,))] == [1.0]
