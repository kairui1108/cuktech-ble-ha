#!/usr/bin/env python3
"""回滚用：把多设备历史从主表归档出去，主表恢复为"旧代码可读"（设计文档 §6.1）。

为什么需要它
------------
新版 `history.py` 给 `port_history` / `charge_sessions` 加了 `device_id` 列，旧代码
查询时**不带** `device_id` 条件（例如 `SELECT ... WHERE port = ?`）——直接退回旧版本，
第二台设备的行会被读进第一台设备的图表里。只把 `config.yaml` 改回单设备是不够的，
数据层也必须降级。

本脚本把 `device_id != 'default'` 的行搬到 `*_archive` 表：

- `charge_points_archive` ← 属于非 primary 会话的采样点（否则旧代码按 session_id
  仍能画出别人的曲线）；
- `charge_sessions_archive` ← 非 primary 会话；
- `port_history_archive` ← 非 primary 采样；
- `meta_archive` ← 非 primary 的 `{id}:{key}` 运行时键（旧代码根本不读，挪走只为
  让主库"看起来就是旧库"）。

**不改表结构**：旧代码的查询都是显式列名（`SELECT id, port, start_time, ...`），多一个
`device_id` 列并不影响可读性；保留它意味着回滚后再升级也不需要额外迁移动作。

幂等：归档表以原始 `id` 为主键，写入用 `INSERT OR IGNORE`；主表删除按同一个条件，
所以重跑第二遍是零改动（`--dry-run` 可先看计划）。

用法
----
    # 1) 先停服务（写库进程持有 WAL，别在跑着的时候动）
    ./cuktech_ctl.sh stop
    # 2) 备份（Runbook 强制项）
    sqlite3 data/port_history.db "VACUUM INTO 'history.backup.db'"
    # 3) 预演 → 执行
    .venv/bin/python scripts/history_down.py --db data/port_history.db --dry-run
    .venv/bin/python scripts/history_down.py --db data/port_history.db

（设计文档 §6.1 就写作 `scripts/history_down.py`，路径一致。）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from history import DEFAULT_DB_PATH, DEFAULT_DEVICE_ID  # noqa: E402

#: 归档顺序**必须**是 points → sessions → port_history：`charge_points` 没有
#: device_id 列，归属只能靠 `session_id IN (SELECT ... FROM charge_sessions)` 推出来，
#: 先删会话就等于把判据删掉了（点会留在主表里，旧代码照样画出别人的曲线）。
TABLES = ("charge_points", "charge_sessions", "port_history")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, name: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]


def _ensure_archive(conn: sqlite3.Connection, table: str) -> None:
    """按主表结构建归档表（去掉 AUTOINCREMENT 之外的差异，另加 archived_at）。"""
    cols = _columns(conn, table)
    if not cols:
        return
    defs = []
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        name, ctype, notnull, default, pk = row[1], row[2], row[3], row[4], row[5]
        if pk:
            defs.append(f"{name} INTEGER PRIMARY KEY")     # 保留原 id，作为幂等键
            continue
        piece = f"{name} {ctype}"
        if notnull:
            piece += " NOT NULL"
        if default is not None:
            # PRAGMA 返回的是原始 SQL 文本：`0` / `'PD'` 可以直接拼，但
            # `strftime('%s','now')` 这种表达式必须加括号，否则建表语法错误。
            raw = str(default)
            simple = raw[:1] in ("'", '"', "-", "+") or raw.replace(".", "", 1).isdigit()
            piece += f" DEFAULT {raw}" if simple else f" DEFAULT ({raw})"
        defs.append(piece)
    defs.append("archived_at REAL NOT NULL")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table}_archive ({', '.join(defs)})")


def archive_non_primary(db_path: str, *, dry_run: bool = False,
                        device_id: str = DEFAULT_DEVICE_ID) -> dict:
    """把非 primary 设备的历史行搬到 `*_archive`；返回每一步的行数（幂等）。

    返回 `{阶段: 行数}`；`dry_run=True` 时只统计不写。
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"history db not found: {path}")

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    stats: dict[str, int] = {}
    try:
        if not _table_exists(conn, "charge_sessions"):
            return {"note": "no history tables (empty db)"}

        # 旧库没有 device_id 列 → 没有任何多设备数据，什么都不用做（幂等友好）。
        if "device_id" not in _columns(conn, "charge_sessions"):
            return {"note": "legacy schema without device_id — nothing to archive"}

        # ① 非 primary 会话的采样点（必须比会话先算：会话删掉后就查不到归属了）
        point_filter = ("session_id IN (SELECT id FROM charge_sessions "
                        f"WHERE device_id != ?)")
        stats["charge_points"] = conn.execute(
            f"SELECT COUNT(*) FROM charge_points WHERE {point_filter}",
            (device_id,)).fetchone()[0]
        # ② 会话本身
        stats["charge_sessions"] = conn.execute(
            "SELECT COUNT(*) FROM charge_sessions WHERE device_id != ?",
            (device_id,)).fetchone()[0]
        # ③ 采样
        stats["port_history"] = conn.execute(
            "SELECT COUNT(*) FROM port_history WHERE device_id != ?",
            (device_id,)).fetchone()[0]
        # ④ 命名空间 meta 键（`{id}:{key}` 形如 `desk:charge_limit_wh`）
        meta_sql = "key LIKE '%:%' AND key NOT LIKE ?"
        stats["meta"] = conn.execute(
            f"SELECT COUNT(*) FROM meta WHERE {meta_sql}",
            (f"%:{device_id}",)).fetchone()[0]

        if dry_run or not any(v for k, v in stats.items()):
            return stats

        for table in TABLES:
            if not _table_exists(conn, table):
                continue
            _ensure_archive(conn, table)
            cols = ", ".join(_columns(conn, table))
            if table == "charge_points":
                where, params = point_filter, (device_id,)
            else:
                where, params = "device_id != ?", (device_id,)
            # 两处都用同一个判据：归档表 INSERT OR IGNORE（原 id 为主键）+ 主表 DELETE
            # ⇒ 重跑不会重复、也不会漏。
            conn.execute(
                f"INSERT OR IGNORE INTO {table}_archive ({cols}, archived_at) "
                f"SELECT {cols}, strftime('%s','now') FROM {table} WHERE {where}",
                params)
            conn.execute(f"DELETE FROM {table} WHERE {where}", params)

        if stats["meta"]:
            if not _table_exists(conn, "meta_archive"):
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta_archive ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, archived_at REAL NOT NULL)")
            conn.execute(
                "INSERT OR IGNORE INTO meta_archive (key, value, archived_at) "
                "SELECT key, value, strftime('%s','now') FROM meta WHERE "
                f"{meta_sql}", (f"%:{device_id}",))
            conn.execute(f"DELETE FROM meta WHERE {meta_sql}", (f"%:{device_id}",))

        conn.commit()
        return stats
    finally:
        conn.close()


def remaining_devices(db_path: str) -> list[str]:
    """主表里还剩哪些 device_id（正常应只有 `default`）——回滚前的自检。"""
    conn = sqlite3.connect(str(db_path))
    try:
        if not _table_exists(conn, "charge_sessions"):
            return []
        if "device_id" not in _columns(conn, "charge_sessions"):
            return [DEFAULT_DEVICE_ID]
        return sorted({r[0] for r in conn.execute(
            "SELECT DISTINCT device_id FROM charge_sessions").fetchall()})
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Roll back multi-device history: archive non-primary rows "
                    "(design doc §6.1, idempotent).")
    ap.add_argument("--db", default=DEFAULT_DB_PATH,
                    help=f"history sqlite file (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    ap.add_argument("--device-id", default=DEFAULT_DEVICE_ID,
                    help="保留在主表的设备 id（默认 default = primary）")
    args = ap.parse_args(argv)

    try:
        stats = archive_non_primary(args.db, dry_run=args.dry_run,
                                   device_id=args.device_id)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    prefix = "DRY-RUN (未写库): " if args.dry_run else "已归档: "
    print(prefix + ", ".join(f"{k}={v}" for k, v in stats.items()))
    if not args.dry_run:
        left = remaining_devices(args.db)
        print(f"主表剩余 device_id: {left}")
        if any(d != args.device_id for d in left):
            print("WARNING: 主表仍有非 primary 数据，旧代码回滚后可能串台，请检查输出",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
