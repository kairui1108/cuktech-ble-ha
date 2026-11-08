"""`scripts/daily_stats.py` 的安全边界（设计文档 §4.6 / §7 P2）。

锁死三件事：

1. **默认拒绝非空表**——服务跑过之后表里有"纯增量日"，整表重建会悄悄删掉它们；
2. `--force` 只补缺失的键，`--force --replace` 才是清空重建；
3. `--dry-run` 一个字节都不写。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import daily_stats  # noqa: E402
from history import PortHistory  # noqa: E402


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "history.db"
    store = PortHistory(db_path=str(path))
    store.connect()
    yield path, store
    store.close()


def _add_session(store, ts: float, wh: float, peak: float = 20.0, dur: int = 600):
    sid = store.start_session(1, "PD", start_time=ts)
    store.end_session(sid, total_wh=wh, peak_power_w=peak,
                      avg_voltage=20.0, avg_current=1.0,
                      duration_sec=dur, end_time=ts + dur)
    return sid


def _clear_daily(store):
    store._conn.execute("DELETE FROM daily_energy")
    store._conn.commit()


def _daily(store):
    return {r["day"]: r["wh"] for r in store._conn.execute(
        "SELECT day, wh FROM daily_energy")}


def _insert_custom(store, day: str, wh: float):
    store._conn.execute(
        """INSERT INTO daily_energy
           (day, device_id, port, wh, session_count, duration_sec,
            peak_power_w, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
        (day, "default", 1, wh, 1, 60, 5.0, time.time()))
    store._conn.commit()


class TestBackfill:
    def test_writes_empty_table(self, db, capsys):
        path, store = db
        ts = time.time() - 3 * 86400
        _add_session(store, ts, wh=5.0, peak=30.0, dur=900)
        _clear_daily(store)                       # 模拟首次升级：表是空的

        assert daily_stats.main(["--db", str(path), "backfill"]) == 0
        assert _daily(store)[day_of(ts)] == 5.0
        assert "已回填 1 行" in capsys.readouterr().out

    def test_refuses_non_empty_table(self, db, capsys):
        path, store = db
        ts = time.time() - 2 * 86400
        _add_session(store, ts, wh=5.0)
        before = _daily(store)
        assert before, "end_session 应该已经写了一条会话维度行"

        assert daily_stats.main(["--db", str(path), "backfill"]) == 1
        assert _daily(store) == before
        err = capsys.readouterr().err
        assert "非空" in err or "已有" in err

    def test_force_only_fills_missing(self, db):
        path, store = db
        ts = time.time() - 2 * 86400
        _add_session(store, ts, wh=5.0)
        _clear_daily(store)
        custom_day = "2026-01-02"
        _insert_custom(store, custom_day, 9.9)

        assert daily_stats.main(
            ["--db", str(path), "backfill", "--force"]) == 0
        days = _daily(store)
        assert days[custom_day] == 9.9            # 已有行一个字节都不动
        assert days[day_of(ts)] == 5.0            # 缺的键补上

    def test_replace_alone_refused(self, db, capsys):
        """`--replace` 单独用不能绕过非空表保护（README 要求 --force --replace）。"""
        path, store = db
        ts = time.time() - 2 * 86400
        _add_session(store, ts, wh=5.0)
        before = _daily(store)
        assert before

        assert daily_stats.main(
            ["--db", str(path), "backfill", "--replace"]) == 1
        assert _daily(store) == before
        assert "--force" in capsys.readouterr().err

    def test_replace_wipes_and_rebuilds(self, db):
        path, store = db
        ts = time.time() - 2 * 86400
        _add_session(store, ts, wh=5.0)
        _clear_daily(store)
        _insert_custom(store, "2026-01-02", 9.9)

        assert daily_stats.main(
            ["--db", str(path), "backfill", "--force", "--replace"]) == 0
        days = _daily(store)
        assert days == {day_of(ts): 5.0}          # 自定义行被清掉

    def test_replace_is_idempotent(self, db):
        path, store = db
        ts = time.time() - 2 * 86400
        _add_session(store, ts, wh=5.0)
        _clear_daily(store)

        args = ["--db", str(path), "backfill", "--force", "--replace"]
        assert daily_stats.main(args) == 0
        first = _daily(store)
        assert daily_stats.main(args) == 0
        assert _daily(store) == first

    def test_dry_run_writes_nothing(self, db, capsys):
        path, store = db
        ts = time.time() - 2 * 86400
        _add_session(store, ts, wh=5.0)
        _clear_daily(store)

        assert daily_stats.main(
            ["--db", str(path), "backfill", "--dry-run"]) == 0
        assert _daily(store) == {}
        assert "DRY-RUN" in capsys.readouterr().out

    def test_backfill_marks_version_so_restart_keeps_rows(self, tmp_path):
        """恢复流程（如 --from-backup）之后启动不能把数据当"未回填"清掉。"""
        path = tmp_path / "restore.db"
        store = PortHistory(db_path=str(path))
        store.connect()
        try:
            store._conn.execute("DELETE FROM daily_energy")
            store._conn.execute(
                "DELETE FROM meta WHERE key LIKE 'daily_stats_%'")
            store._conn.commit()
            restored_day = day_of(time.time() - 86400)
            _insert_custom(store, restored_day, 7.5)
        finally:
            store.close()

        assert daily_stats.main(
            ["--db", str(path), "backfill", "--force"]) == 0

        # 服务重启：启动回填见到版本标记，跳过 replace=True 的整表重建
        h = PortHistory(db_path=str(path))
        h.connect()
        try:
            assert h.get_meta("daily_stats_version") == "1"
            days = {d["date"]: d["wh"] for d in h.get_daily_energy()["days"]}
            assert days[restored_day] == 7.5
        finally:
            h.close()

    def test_missing_db_errors(self, tmp_path, capsys):
        missing = tmp_path / "nope.db"
        assert daily_stats.main(["--db", str(missing), "backfill"]) == 2
        assert not missing.exists()
        assert "数据库不存在" in capsys.readouterr().err


class TestOtherCommands:
    def test_audit_and_export(self, db, capsys, tmp_path):
        path, store = db
        ts = time.time() - 2 * 86400
        _add_session(store, ts, wh=5.0)
        store.add_energy_delta(1, 3.0, ts)
        store.flush()

        assert daily_stats.main(["--db", str(path), "audit"]) == 0
        out = capsys.readouterr().out
        assert "漂移 1 行" in out

        csv_path = tmp_path / "daily.csv"
        assert daily_stats.main(
            ["--db", str(path), "export", "--out", str(csv_path)]) == 0
        lines = csv_path.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("day,device_id,port")
        assert len(lines) == 2

    def test_remap_merges_devices(self, db):
        path, store = db
        ts = time.time() - 2 * 86400
        store.add_energy_delta(1, 1.0, ts, device_id="old")
        store.add_energy_delta(1, 2.0, ts, device_id="new")
        store.flush()

        assert daily_stats.main(
            ["--db", str(path), "remap", "old", "new"]) == 0
        rows = store._conn.execute(
            "SELECT device_id, wh FROM daily_energy").fetchall()
        assert [tuple(r) for r in rows] == [("new", 3.0)]

    def test_remap_moves_sessions_history_and_meta(self, db):
        """只搬日汇总会让 audit 永久漂移，所以会话/采样/meta 命名空间一起搬。"""
        path, store = db
        ts = time.time() - 2 * 86400
        store.record_port_data(
            1, {"voltage": 5.0, "current": 1.0, "power": 5.0,
                "active": True, "protocol": "PD"}, device_id="old")
        sid = store.start_session(1, "PD", device_id="old", start_time=ts)
        store.end_session(sid, total_wh=2.0, peak_power_w=5.0,
                          avg_voltage=5.0, avg_current=1.0,
                          duration_sec=600, end_time=ts + 600)
        store.set_charge_limits({"c1": {"wh": 10.0, "mode": "once"}},
                                device_id="old")
        store.add_energy_delta(1, 1.0, ts, device_id="old")
        store.flush()

        assert daily_stats.main(
            ["--db", str(path), "remap", "old", "new"]) == 0

        assert {r[0] for r in store._conn.execute(
            "SELECT device_id FROM charge_sessions")} == {"new"}
        assert {r[0] for r in store._conn.execute(
            "SELECT DISTINCT device_id FROM port_history")} == {"new"}
        keys = {r[0] for r in store._conn.execute("SELECT key FROM meta")}
        assert "new:charge_limit_wh" in keys
        assert not any(k.startswith("old:") for k in keys)
        assert {r[0] for r in store._conn.execute(
            "SELECT DISTINCT device_id FROM daily_energy")} <= {"new"}

    def test_purge_before(self, db, capsys):
        path, store = db
        _insert_custom(store, "2026-01-01", 1.0)
        _insert_custom(store, "2026-02-01", 2.0)

        assert daily_stats.main(
            ["--db", str(path), "purge", "--before", "2026-02-01"]) == 0
        assert store._conn.execute(
            "SELECT day FROM daily_energy").fetchall()[0][0] == "2026-02-01"

    def test_orphans_lists_devices(self, db, capsys):
        path, store = db
        _insert_custom(store, "2026-02-01", 2.0)
        assert daily_stats.main(["--db", str(path), "orphans"]) == 0
        assert "default: 1 行" in capsys.readouterr().out
