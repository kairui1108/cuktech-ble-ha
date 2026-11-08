#!/usr/bin/env python3
"""daily_energy（充电热力图数据源）维护脚本。

子命令
------
    backfill   从 charge_sessions 重建 / 补全日汇总
    audit      日汇总 vs 会话表按 (day, device_id, port) 对账
    orphans    列出各设备的数据范围（删除设备后残留的行）
    remap      把一台设备的全部历史（日汇总/会话/采样/meta）合并到另一台
               （重扫码 / 换主设备后修串位）
    purge      删除某天之前的日汇总
    export     导出 CSV（默认 stdout）

安全约束
--------
· 默认拒绝写**非空**表：服务跑过之后，daily_energy 里有"纯增量日"——
  会话门槛以下的小充电、当天进行中的会话。整表重建会把它们删掉。
· `--force`   允许写非空表，但**只补缺失的键**（INSERT OR IGNORE），不覆盖已有行。
· `--replace` 只在配合 `--force` 时生效：先清空再重建（最彻底，也最危险）；
  文档里的重跑写法是 `--force --replace`。
· 写操作请在**服务停止后**执行：运行中会和在线增量抢同一天的行（双计）。

用法
----
    ./cuktech_ctl.sh stop
    sqlite3 data/port_history.db "VACUUM INTO 'history.backup.db'"
    .venv/bin/python scripts/daily_stats.py --db data/port_history.db backfill --dry-run
    .venv/bin/python scripts/daily_stats.py --db data/port_history.db backfill --force --replace
    .venv/bin/python scripts/daily_stats.py --db data/port_history.db audit
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from history import DEFAULT_DB_PATH  # noqa: E402

_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS daily_energy (
        day           TEXT    NOT NULL,
        device_id     TEXT    NOT NULL DEFAULT 'default',
        port          INTEGER NOT NULL,
        wh            REAL    NOT NULL DEFAULT 0,
        session_count INTEGER NOT NULL DEFAULT 0,
        duration_sec  INTEGER NOT NULL DEFAULT 0,
        peak_power_w  REAL    NOT NULL DEFAULT 0,
        updated_at    REAL    NOT NULL,
        PRIMARY KEY (device_id, day, port)
    ) WITHOUT ROWID
"""

#: 与 history.py 的 `meta` 表同构。恢复场景下脚本可能先于服务建库，
#: 必须一起建出来，否则回填完成后的版本标记写不进去。
_META_SQL = """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
"""

#: 回填成功后写入的标记：服务启动时 `_backfill_daily_if_needed()` 见到它
#: 就不再执行 `replace=True` 的整表重建，恢复出来的数据不会被清掉。
_META_VERSION = "1"

_SELECT_SESSIONS = """
    SELECT date(start_time, 'unixepoch', 'localtime') AS day,
           device_id, port,
           SUM(total_wh)      AS wh,
           COUNT(*)           AS c,
           SUM(duration_sec)  AS dur,
           MAX(peak_power_w)  AS peak
    FROM charge_sessions
    WHERE total_wh > 0 AND end_time IS NOT NULL
    GROUP BY day, device_id, port
    ORDER BY day
"""

_UPSERT_SET = """
    INSERT INTO daily_energy (day, device_id, port, wh, session_count,
                              duration_sec, peak_power_w, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(device_id, day, port) DO UPDATE SET
      wh            = excluded.wh,
      session_count = excluded.session_count,
      duration_sec  = excluded.duration_sec,
      peak_power_w  = excluded.peak_power_w,
      updated_at    = excluded.updated_at
"""

_INSERT_MISSING = """
    INSERT OR IGNORE INTO daily_energy
      (day, device_id, port, wh, session_count, duration_sec, peak_power_w, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_ADD = """
    INSERT INTO daily_energy (day, device_id, port, wh, session_count,
                              duration_sec, peak_power_w, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(device_id, day, port) DO UPDATE SET
      wh            = wh + excluded.wh,
      session_count = session_count + excluded.session_count,
      duration_sec  = duration_sec + excluded.duration_sec,
      peak_power_w  = MAX(peak_power_w, excluded.peak_power_w),
      updated_at    = excluded.updated_at
"""


def _connect(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_SQL)
    conn.execute(_META_SQL)
    conn.commit()


def _mark_backfilled(conn: sqlite3.Connection, now: float) -> None:
    """写入"已回填"标记，与 `PortHistory._backfill_daily_if_needed` 的判据一致。

    没有这一步，恢复/重建出来的 daily_energy 会在下次启动时被
    `replace=True` 的启动回填整表删掉（见 README 的 `--from-backup` 流程）。
    """
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('daily_stats_version', ?)",
        (_META_VERSION,))
    row = conn.execute("SELECT MIN(day) AS d FROM daily_energy").fetchone()
    since = row["d"] if row and row["d"] else time.strftime(
        "%Y-%m-%d", time.localtime(now))
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('daily_stats_since', ?)",
        (since,))


def _session_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(_SELECT_SESSIONS).fetchall()


def _rows_payload(rows, now: float) -> list[tuple]:
    return [
        (r["day"], r["device_id"], r["port"], round(r["wh"] or 0.0, 4),
         int(r["c"] or 0), int(r["dur"] or 0), round(r["peak"] or 0.0, 2), now)
        for r in rows
    ]


# ── 子命令 ──────────────────────────────────────────────────────

def cmd_backfill(args) -> int:
    target_path = Path(args.db)
    if not target_path.exists() and not args.from_backup:
        print(f"ERROR: 数据库不存在：{args.db}（如需从备份重建，请加 --from-backup）",
              file=sys.stderr)
        return 2

    target = _connect(str(target_path))
    try:
        _ensure_table(target)
        existing = target.execute(
            "SELECT COUNT(*) FROM daily_energy").fetchone()[0]
        if existing and not args.force:
            print(f"ERROR: daily_energy 已有 {existing} 行。服务跑过之后，"
                  "表里有增量独有数据（门槛以下的小充电 / 进行中的会话），"
                  "直接重建会丢掉它们。\n"
                  "  只补缺失：  --force\n"
                  "  清空重建：  --force --replace", file=sys.stderr)
            return 1

        if args.from_backup:
            src = _connect(args.from_backup, readonly=True)
            try:
                if not _table_exists(src, "charge_sessions"):
                    print(f"ERROR: 备份库里没有 charge_sessions：{args.from_backup}",
                          file=sys.stderr)
                    return 2
                rows = _session_rows(src)
            finally:
                src.close()
        else:
            if not _table_exists(target, "charge_sessions"):
                print(f"ERROR: 库里没有 charge_sessions：{args.db}", file=sys.stderr)
                return 2
            rows = _session_rows(target)

        payload = _rows_payload(rows, time.time())
        if args.dry_run:
            first = payload[0][0] if payload else "-"
            last = payload[-1][0] if payload else "-"
            print(f"DRY-RUN: 将写入 {len(payload)} 行（{first} ~ {last}），"
                  f"现有 {existing} 行；"
                  f"{'清空重建' if args.replace else ('只补缺失' if args.force else '空表写入')}")
            return 0

        if args.replace:
            target.execute("DELETE FROM daily_energy")
            sql = _UPSERT_SET
        elif args.force:
            sql = _INSERT_MISSING
        else:
            sql = _UPSERT_SET
        target.executemany(sql, payload)
        # 必须在同一次提交里打标记：否则恢复的数据会在下次启动被清掉。
        _mark_backfilled(target, time.time())
        target.commit()
        total = target.execute("SELECT COUNT(*) FROM daily_energy").fetchone()[0]
        print(f"已回填 {len(payload)} 行，daily_energy 现在 {total} 行")
        return 0
    finally:
        target.close()


def _open_ro(args) -> sqlite3.Connection | None:
    """只读打开；库不存在时报错而不是让 sqlite 建一个空库。"""
    if not Path(args.db).exists():
        print(f"ERROR: 数据库不存在：{args.db}", file=sys.stderr)
        return None
    return _connect(args.db, readonly=True)


def cmd_audit(args) -> int:
    conn = _open_ro(args)
    if conn is None:
        return 2
    try:
        if not _table_exists(conn, "daily_energy"):
            print("daily_energy 不存在（服务还没启动过？）", file=sys.stderr)
            return 2
        daily = {(r["day"], r["device_id"], r["port"]): (r["wh"] or 0.0)
                 for r in conn.execute(
                     """SELECT day, device_id, port, SUM(wh) AS wh
                        FROM daily_energy GROUP BY day, device_id, port""")}
        ses = {(r["day"], r["device_id"], r["port"]): (r["wh"] or 0.0)
               for r in conn.execute(
                   """SELECT date(start_time, 'unixepoch', 'localtime') AS day,
                             device_id, port, SUM(total_wh) AS wh
                      FROM charge_sessions WHERE total_wh > 0
                      GROUP BY day, device_id, port""")}
    finally:
        conn.close()

    drift = []
    for key in sorted(set(daily) | set(ses)):
        a, b = daily.get(key, 0.0), ses.get(key, 0.0)
        if abs(a - b) > max(0.05, 0.01 * max(abs(a), abs(b))):
            drift.append((key, a, b))
    print(f"日汇总合计 {sum(daily.values()):.2f} Wh / "
          f"会话合计 {sum(ses.values()):.2f} Wh；漂移 {len(drift)} 行")
    for (day, dev, port), a, b in drift[:50]:
        print(f"  {day} {dev} p{port}: daily={a:.2f} session={b:.2f}")
    if len(drift) > 50:
        print(f"  ... 其余 {len(drift) - 50} 行省略")
    print("预期漂移来源：门槛以下的小充电、崩溃丢的最后一个批次、跨零点会话的归日差异。")
    return 0


def cmd_orphans(args) -> int:
    conn = _open_ro(args)
    if conn is None:
        return 2
    try:
        if not _table_exists(conn, "daily_energy"):
            print("daily_energy 不存在（服务还没启动过？）", file=sys.stderr)
            return 2
        rows = conn.execute(
            """SELECT device_id, COUNT(*) AS n, MIN(day) AS first_day,
                      MAX(day) AS last_day, SUM(wh) AS wh
               FROM daily_energy GROUP BY device_id ORDER BY device_id""").fetchall()
    finally:
        conn.close()
    if not rows:
        print("daily_energy 为空")
        return 0
    for r in rows:
        print(f"{r['device_id']}: {r['n']} 行, {r['first_day']} ~ {r['last_day']}, "
              f"{r['wh']:.2f} Wh")
    return 0


def cmd_remap(args) -> int:
    if args.before == args.after:
        print("ERROR: old/new device_id 相同", file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print(f"ERROR: 数据库不存在：{args.db}", file=sys.stderr)
        return 2
    conn = _connect(args.db)
    try:
        if not _table_exists(conn, "daily_energy"):
            print("daily_energy 不存在（服务还没启动过？）", file=sys.stderr)
            return 2

        def _has_device_id(table: str) -> bool:
            return (_table_exists(conn, table)
                    and "device_id" in [r[1] for r in
                                        conn.execute(f"PRAGMA table_info({table})")])

        rows = conn.execute(
            "SELECT day, port, wh, session_count, duration_sec, peak_power_w, updated_at "
            "FROM daily_energy WHERE device_id = ?", (args.before,)).fetchall()
        sessions = (conn.execute(
            "SELECT COUNT(*) FROM charge_sessions WHERE device_id = ?",
            (args.before,)).fetchone()[0]
            if _has_device_id("charge_sessions") else 0)
        history = (conn.execute(
            "SELECT COUNT(*) FROM port_history WHERE device_id = ?",
            (args.before,)).fetchone()[0]
            if _has_device_id("port_history") else 0)
        # meta 键是 `{device_id}:{key}` 命名空间；`%`/`_` 是 LIKE 通配符。
        like_id = (args.before.replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
        meta_like = f"{like_id}:%"
        meta_keys = (conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE ? ESCAPE '\\'",
            (meta_like,)).fetchone()[0] if _table_exists(conn, "meta") else 0)

        if not any((rows, sessions, history, meta_keys)):
            print(f"没有 {args.before!r} 的任何历史数据")
            return 0
        print(f"{args.before!r} → {args.after!r}: "
              f"日汇总 {len(rows)} 行、会话 {sessions} 条、采样 {history} 行、"
              f"meta {meta_keys} 个"
              f"{'（DRY-RUN，未写库）' if args.dry_run else ''}")
        if args.dry_run:
            return 0

        # 只搬日汇总会让 audit 永久报漂移、旧设备的详情曲线指向"已不存在的设备"。
        # 会话 id 是全局主键，UPDATE device_id 不会碰撞；charge_points 靠
        # session_id 关联，会话一搬曲线自动跟过来。
        if sessions:
            conn.execute("UPDATE charge_sessions SET device_id = ? WHERE device_id = ?",
                         (args.after, args.before))
        if history:
            conn.execute("UPDATE port_history SET device_id = ? WHERE device_id = ?",
                         (args.after, args.before))
        if meta_keys:
            # 目标设备已有的同名键优先（INSERT OR IGNORE），再删旧命名空间。
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) "
                "SELECT ? || substr(key, ?), value FROM meta "
                "WHERE key LIKE ? ESCAPE '\\'",
                (f"{args.after}:", len(args.before) + 2, meta_like))
            conn.execute("DELETE FROM meta WHERE key LIKE ? ESCAPE '\\'",
                         (meta_like,))

        if rows:
            payload = [
                (r["day"], args.after, r["port"], r["wh"], r["session_count"],
                 r["duration_sec"], r["peak_power_w"],
                 r["updated_at"] or time.time())
                for r in rows
            ]
            conn.executemany(_UPSERT_ADD, payload)
            conn.execute("DELETE FROM daily_energy WHERE device_id = ?",
                         (args.before,))
        conn.commit()
        print("完成")
        return 0
    finally:
        conn.close()


def cmd_purge(args) -> int:
    if not Path(args.db).exists():
        print(f"ERROR: 数据库不存在：{args.db}", file=sys.stderr)
        return 2
    conn = _connect(args.db)
    try:
        if not _table_exists(conn, "daily_energy"):
            print("daily_energy 不存在（服务还没启动过？）", file=sys.stderr)
            return 2
        n = conn.execute(
            "SELECT COUNT(*) FROM daily_energy WHERE day < ?",
            (args.before,)).fetchone()[0]
        print(f"将删除 {n} 行（day < {args.before}）"
              f"{'（DRY-RUN，未写库）' if args.dry_run else ''}")
        if args.dry_run or n == 0:
            return 0
        conn.execute("DELETE FROM daily_energy WHERE day < ?", (args.before,))
        conn.commit()
        print("完成")
        return 0
    finally:
        conn.close()


def cmd_export(args) -> int:
    conn = _open_ro(args)
    if conn is None:
        return 2
    try:
        if not _table_exists(conn, "daily_energy"):
            print("daily_energy 不存在（服务还没启动过？）", file=sys.stderr)
            return 2
        rows = conn.execute(
            """SELECT day, device_id, port, wh, session_count, duration_sec,
                      peak_power_w, updated_at
               FROM daily_energy ORDER BY day, device_id, port""").fetchall()
    finally:
        conn.close()
    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        writer = csv.writer(out)
        writer.writerow(["day", "device_id", "port", "wh", "session_count",
                         "duration_sec", "peak_power_w", "updated_at"])
        for r in rows:
            writer.writerow([r["day"], r["device_id"], r["port"], r["wh"],
                             r["session_count"], r["duration_sec"],
                             r["peak_power_w"], r["updated_at"]])
    finally:
        if out is not sys.stdout:
            out.close()
    if args.out:
        print(f"已导出 {len(rows)} 行到 {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="daily_energy maintenance (charging heatmap data source)")
    ap.add_argument("--db", default=DEFAULT_DB_PATH,
                    help=f"history sqlite file (default: {DEFAULT_DB_PATH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backfill", help="从 charge_sessions 重建 / 补全日汇总")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    p.add_argument("--force", action="store_true",
                   help="允许写非空表（只补缺失的键，不覆盖已有行）")
    p.add_argument("--replace", action="store_true",
                   help="清空后重建（危险；配合 --force 使用）")
    p.add_argument("--from-backup", metavar="DB", default=None,
                   help="从备份库读取 charge_sessions，写入当前库")

    p = sub.add_parser("audit", help="日汇总 vs 会话表对账")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("orphans", help="列出各设备的数据范围")
    p.set_defaults(func=cmd_orphans)

    p = sub.add_parser("remap", help="把 old 的全部历史合并到 new")
    p.add_argument("before", metavar="OLD")
    p.add_argument("after", metavar="NEW")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_remap)

    p = sub.add_parser("purge", help="删除某天之前的日汇总")
    p.add_argument("--before", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("export", help="导出 CSV")
    p.add_argument("--out", default=None, help="输出文件（默认 stdout）")
    p.set_defaults(func=cmd_export)

    args = ap.parse_args(argv)
    if args.cmd == "backfill":
        return cmd_backfill(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
