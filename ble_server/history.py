"""CUKTECH BLE Server - SQLite history storage for port data."""
import csv
import io
import json
import logging
import math
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from energy import normalize_charge_limit, DEFAULT_LIMIT_MODE, LIMIT_MODE_ALWAYS
    from state import PORT_NAMES
except ImportError:
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from energy import normalize_charge_limit, DEFAULT_LIMIT_MODE, LIMIT_MODE_ALWAYS
    from state import PORT_NAMES

_LOGGER = logging.getLogger("cuktech_history")

DEFAULT_RETENTION_DAYS = 2
# 充电记录（charge_sessions）的保留期：会话行很便宜（一年 ~1.5 MB），
# 单独给用户一个旋钮，让"近 30 天/全部"两个 tab 真正有内容。
DEFAULT_SESSION_RETENTION_DAYS = 30
# 会话详情曲线（charge_points）独立短保留：它是秒级数据里最贵的那层，
# 用户看详情曲线多半只看最近几次，不跟记录期一起放大。
POINTS_RETENTION_DAYS = 7
# 每日电量（daily_energy）保留期：热力图固定看最近一年，多留无意义。
DAILY_RETENTION_DAYS = 365
DEFAULT_DB_PATH = "port_history.db"
# primary 设备的存储身份（§3.10：history/meta/legacy topic 的兼容都锚定在这个 id 上）。
# 老库升级后所有既有行都归它，因此它同时是"未指定设备"的含义 —— 单设备部署下
# 所有 SQL 的行为与加列之前完全一致。
DEFAULT_DEVICE_ID = "default"


class PortHistory:
    """SQLite-based port history storage."""

    # 批量提交参数：降低高频采样下的写放大（每次 BLE 推送不再单独 COMMIT）
    BATCH_SIZE = 50          # 缓冲行数达到该值即强制提交
    # 距上次提交超过该秒数即强制提交。原先 1.0s，而 port_history 实际只有约
    # 0.8 行/秒，批量阈值永远达不到 → 变成"每秒一次 commit"（约 3600 次/小时）。
    # 放宽到 5s 可把提交次数降到约 1/5；读取路径都会先 flush()，不影响可见性。
    BATCH_INTERVAL = 5.0
    # WAL 回写下限：WAL 小于它就不做 checkpoint（SQLite 自带 wal_autocheckpoint
    # =1000 页兜底）。原先每 5 分钟无条件 PASSIVE 回写，WAL 很小时也照样把脏页
    # 抄回主库，是磁盘写入的主要来源之一。
    WAL_CHECKPOINT_MIN_BYTES = 4 * 1024 * 1024
    # 能量积分的采样断档上限（秒）：相邻采样点间隔超过它就按它计，防止闲置/掉线的
    # 空档被当成持续输出计入能量（fork 审查指出的"凭空计费"）。
    ENERGY_GAP_CAP_SEC = 30
    # 均压/均流重算时的采样断档上限（秒）：超过它的区间不参与加权，避免把中间的空载
    # 挂载段（v>0/i=0）或掉线空档计入均值。
    AVG_VI_GAP_CAP_SEC = 1800
    #: 日汇总缓冲的 key 上限（一年 × 4 口 × 若干设备远低于它）。
    #: DB 持续写失败时内存不能无限涨；超限丢弃新增并告警，flush 成功后恢复。
    DAILY_MAX_KEYS = 4096

    def __init__(self, db_path: str = DEFAULT_DB_PATH,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 session_retention_days: int = DEFAULT_SESSION_RETENTION_DAYS):
        self.db_path = db_path
        self.retention_days = retention_days
        self.session_retention_days = session_retention_days
        self._conn: Optional[sqlite3.Connection] = None
        self._db_lock = threading.Lock()  # 保护所有读写操作
        self._last_cleanup = 0
        self._last_wal_checkpoint = 0
        self._pending: list[tuple] = []   # 待批量写入的 port_history 行
        self._pending_points: list[tuple] = []  # 待批量写入的 charge_points 行
        self._last_commit = 0.0
        # 日汇总的内存缓冲：{(day, device_id, port): wh}。
        # 只存电量：会话次数/时长/峰值由 end_session 的 SQL 直写，走不到这里。
        # 独立小锁，事件循环里的 add_energy_delta 只拿它、绝不碰 _db_lock。
        self._daily: dict[tuple, float] = {}
        # 已交给 executemany、但还没 commit 的那一批；失败时用来原样放回，
        # 否则一次 SQLITE_BUSY / 磁盘错误就永久丢掉一段能量（基线已前移）。
        self._daily_inflight: dict[tuple, float] | None = None
        self._daily_overflow_warned = False
        self._daily_lock = threading.Lock()

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
        # 崩溃/强杀遗留的未闭合会话（end_time IS NULL）在这里收尾：按采样点补算
        # 能量/峰值，结束时刻取最后一个采样点——**不是**整行删掉。删掉会把一整段
        # 充电的能量、均压均流、时长连同采样点一起丢掉，历史列表里也再看不见。
        # 只有能量过小的（<0.05Wh，等同没充过）才按常规口径删除，见该方法。
        # 这是启动清理的**唯一**入口：以前另有一个"直接 DELETE"的 reap，两者同时
        # 存在时后者（在 connect 里先跑）会把行删光，让这里的收尾永远无事可做。
        try:
            self.close_stale_sessions()
        except Exception as e:      # 兜底遗留失败不该拖垮整个启动
            _LOGGER.error("Failed to close stale sessions on startup: %s", e)
        # 日汇总回填必须早于清理：清理会按新 cutoff 删旧会话，删了就没得回填。
        # 首次升级时 daily_energy 必然为空，整表重建是空操作；之后靠 meta 版本号跳过。
        self._backfill_daily_if_needed()
        self._cleanup_old_data()
        # 以连接时刻为提交基准：否则 _last_commit=0 会让启动后的第一个采样点
        # 立刻触发一次 flush（无谓的一次提交）。
        self._last_commit = time.time()
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
        self._conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS port_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                port INTEGER NOT NULL,
                voltage REAL,
                current REAL,
                power REAL,
                active INTEGER,
                protocol TEXT,
                device_id TEXT NOT NULL DEFAULT '{DEFAULT_DEVICE_ID}'
            );
            CREATE INDEX IF NOT EXISTS idx_port_history_port ON port_history(port);
            CREATE INDEX IF NOT EXISTS idx_port_history_timestamp ON port_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_port_history_port_time ON port_history(port, timestamp);
            -- device_id 相关索引**不在这里建**：旧库此刻还没有该列，建索引会直接
            -- 抛 "no such column" 让启动失败。ALTER 迁移之后再统一建（见 _create_tables 末尾）。

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
                created_at REAL DEFAULT (strftime('%s','now')),
                device_id TEXT NOT NULL DEFAULT '{DEFAULT_DEVICE_ID}'
            );
            CREATE INDEX IF NOT EXISTS idx_charge_sessions_port ON charge_sessions(port);
            CREATE INDEX IF NOT EXISTS idx_charge_sessions_start ON charge_sessions(start_time);
            -- idx_charge_sessions_device 同样挪到 ALTER 之后（旧库无 device_id 列）。

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
        # Migration（§3.9）：旧库加 device_id 列。DEFAULT 'default' 让既有行自动归到
        # primary 设备——这正是"旧 history.db 无缝保留"的实现点：不需要回填脚本，
        # 不需要备份迁移，历史曲线/会话在升级后仍然只属于那台老设备。
        for table in ("port_history", "charge_sessions"):
            try:
                self._conn.execute(f"SELECT device_id FROM {table} LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN device_id "
                    f"TEXT NOT NULL DEFAULT '{DEFAULT_DEVICE_ID}'")
                _LOGGER.info("Migrated %s: added device_id column (default %r)",
                             table, DEFAULT_DEVICE_ID)
        self._conn.commit()
        # 索引在 ALTER 之后单独建：老库的 CREATE INDEX IF NOT EXISTS 已在上面跑过，
        # 但那时列还不存在，必须再建一次。
        self._conn.executescript(f"""
            CREATE INDEX IF NOT EXISTS idx_port_history_device_port_time
                ON port_history(device_id, port, timestamp);
            CREATE INDEX IF NOT EXISTS idx_charge_sessions_device
                ON charge_sessions(device_id, start_time);

            -- 每日电量（热力图数据源）。物理上"每天每设备每口最多一行"，
            -- 逻辑上就是需求的"每天一条"（查询时 SUM）。不受采样保留期约束，
            -- 只由 _cleanup_old_data 按 DAILY_RETENTION_DAYS 裁剪。
            CREATE TABLE IF NOT EXISTS daily_energy (
                day           TEXT    NOT NULL,
                device_id     TEXT    NOT NULL DEFAULT '{DEFAULT_DEVICE_ID}',
                port          INTEGER NOT NULL,
                wh            REAL    NOT NULL DEFAULT 0,
                session_count INTEGER NOT NULL DEFAULT 0,
                duration_sec  INTEGER NOT NULL DEFAULT 0,
                peak_power_w  REAL    NOT NULL DEFAULT 0,
                updated_at    REAL    NOT NULL,
                PRIMARY KEY (device_id, day, port)
            ) WITHOUT ROWID;
        """)
        self._conn.commit()

    # ── Runtime meta storage (single source for runtime toggles) ──

    @staticmethod
    def meta_key(key: str, device_id: str = DEFAULT_DEVICE_ID) -> str:
        """meta 键的设备命名空间（§3.9）。

        primary（`default`）**保持裸键**：旧库里的 `charge_limit_wh` /
        `session_recording` / `permanent_ports` / `full_off_ports` 就是这些键，
        升级后 primary 必须原样读到自己的设置。其他设备用 `{device_id}:{key}`。
        非 primary 不回落到裸键——那会让设备 2 继承设备 1 的限额（串台）。
        """
        if not device_id or device_id == DEFAULT_DEVICE_ID:
            return key
        return f"{device_id}:{key}"

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

    def get_session_recording(self, *, device_id: str = DEFAULT_DEVICE_ID) -> bool:
        """Whether charge session recording is enabled (DB meta, default True)."""
        value = self.get_meta(self.meta_key("session_recording", device_id), "true").strip().lower()
        return value not in ("0", "false", "no", "off")

    def set_session_recording(self, enabled: bool, *, device_id: str = DEFAULT_DEVICE_ID) -> None:
        """Persist the charge session recording toggle (DB meta, survives restart)."""
        self.set_meta(self.meta_key("session_recording", device_id),
                      "true" if enabled else "false")

    # ── 端口模式集合（长期供电 / 充满即停，按端口名持久化） ──

    PERMANENT_PORTS_META_KEY = "permanent_ports"
    FULL_OFF_PORTS_META_KEY = "full_off_ports"

    @staticmethod
    def _clean_port_names(names) -> list:
        """把任意输入归一成合法端口名列表（去重、小写、保持 PORT_NAMES 顺序）。

        meta 脏数据 / 前端输入都不该让调用方崩溃：非法项静默丢弃。
        """
        valid = set(PORT_NAMES.values())
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, (list, tuple, set, frozenset)):
            return []
        wanted = {str(n).strip().lower() for n in names}
        return [name for name in PORT_NAMES.values() if name in (wanted & valid)]

    def _get_port_set(self, key: str, device_id: str = DEFAULT_DEVICE_ID) -> list:
        """读取端口名集合（JSON 数组存 meta）；缺失/损坏一律回落空列表。"""
        raw = self.get_meta(self.meta_key(key, device_id), "")
        if not raw:
            return []
        try:
            stored = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.warning("Port mode meta %s is not valid JSON, falling back to empty", key)
            return []
        return self._clean_port_names(stored)

    def _set_port_set(self, key: str, names, device_id: str = DEFAULT_DEVICE_ID) -> list:
        """写入端口名集合，返回归一后的列表（与读取对称，保证往返一致）。"""
        clean = self._clean_port_names(names)
        self.set_meta(self.meta_key(key, device_id), json.dumps(clean))
        return clean

    def get_permanent_ports(self, *, device_id: str = DEFAULT_DEVICE_ID) -> list:
        """长期供电设备端口（不写充电曲线点，会话统计照常记录）。"""
        return self._get_port_set(self.PERMANENT_PORTS_META_KEY, device_id)

    def set_permanent_ports(self, names, *, device_id: str = DEFAULT_DEVICE_ID) -> list:
        return self._set_port_set(self.PERMANENT_PORTS_META_KEY, names, device_id)

    def get_full_off(self, *, device_id: str = DEFAULT_DEVICE_ID) -> dict:
        """充满后自动关闭端口的端口 → 模式（once/always）。

        存储形状是 {端口名: mode}；兼容旧版本的数组形状（当时没有模式，语义等价于
        always：一直有效直到用户关掉），读到数组时统一升级成 always。
        """
        raw = self.get_meta(self.meta_key(self.FULL_OFF_PORTS_META_KEY, device_id), "")
        if not raw:
            return {}
        try:
            stored = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.warning("Port mode meta %s is not valid JSON, falling back to empty",
                            self.FULL_OFF_PORTS_META_KEY)
            return {}
        if isinstance(stored, dict):
            out = {}
            for name in self._clean_port_names(list(stored.keys())):
                _, mode = normalize_charge_limit(0, stored.get(name))
                out[name] = mode
            return out
        # 旧形状：["c1"] → {"c1": "always"}
        return {name: LIMIT_MODE_ALWAYS for name in self._clean_port_names(stored)}

    def set_full_off(self, entries, *, device_id: str = DEFAULT_DEVICE_ID) -> dict:
        """写入 {端口名: mode}，返回归一后的字典（与读取对称）。"""
        if isinstance(entries, (list, tuple, set, frozenset)) or isinstance(entries, str):
            entries = {name: LIMIT_MODE_ALWAYS for name in self._clean_port_names(entries)}
        clean = {}
        if isinstance(entries, dict):
            for name in self._clean_port_names(list(entries.keys())):
                _, mode = normalize_charge_limit(0, entries.get(name))
                clean[name] = mode
        self.set_meta(self.meta_key(self.FULL_OFF_PORTS_META_KEY, device_id), json.dumps(clean))
        return clean

    def get_web_language(self) -> str:
        """Web UI language preference (DB meta, default 'auto' = follow system).

        **服务级 meta，刻意不做设备命名空间**（§3.9）：语言属于浏览器/服务，不属于
        某台充电器。调用方因此走 `s.history_store` 而不是 `s.history`（facade），
        免得看起来像"按设备读"却读到别人的设置。

        Returns 'auto' (follow system), 'zh-CN' or 'en'.
        """
        value = self.get_meta("web_language", "auto").strip().lower()
        if value in ("zh", "zh-cn", "zh-hans"):
            return "zh-CN"
        if value.startswith("en"):
            return "en"
        return "auto"

    def set_web_language(self, lang: str) -> None:
        """Persist the Web UI language preference (DB meta, survives restart)."""
        self.set_meta("web_language", lang)

    # ── Charge limits (自动断电阈值，单源 = DB meta) ──

    LIMIT_META_KEY = "charge_limit_wh"

    def get_charge_limits(self, *, device_id: str = DEFAULT_DEVICE_ID) -> dict:
        """读取各端口充电量阈值，返回 {port_name: {"wh": float, "mode": str}}。

        meta 缺失/JSON 损坏/字段类型非法时逐端口回落禁用（wh=0），不抛异常。
        端口名以 PORT_NAMES 为准（c1/c2/c3/a），meta 中的未知键忽略。
        """
        limits = {name: {"wh": 0.0, "mode": DEFAULT_LIMIT_MODE}
                  for name in PORT_NAMES.values()}
        raw = self.get_meta(self.meta_key(self.LIMIT_META_KEY, device_id), "")
        if not raw:
            return limits
        try:
            stored = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.warning("Charge limits meta is not valid JSON, falling back to disabled")
            return limits
        if not isinstance(stored, dict):
            _LOGGER.warning("Charge limits meta is not an object, falling back to disabled")
            return limits
        for name in limits:
            entry = stored.get(name)
            if entry is None:
                continue
            if isinstance(entry, dict):
                wh, mode = normalize_charge_limit(entry.get("wh"), entry.get("mode"))
            else:
                # 兼容简写形式 {"c1": 30}
                wh, mode = normalize_charge_limit(entry, None)
            limits[name] = {"wh": wh, "mode": mode}
        return limits

    def set_charge_limits(self, limits: dict, *,
                          device_id: str = DEFAULT_DEVICE_ID) -> None:
        """Persist per-port charge limits as JSON in the meta table.

        入参形如 {port_name: {"wh": float, "mode": str}}；未知键忽略，
        非法值归一为禁用（与 get_charge_limits 对称，保证往返一致）。
        device_id 为关键字参数并置于末尾（保持单设备调用点不变）。
        """
        clean = {}
        for name in PORT_NAMES.values():
            entry = limits.get(name) if isinstance(limits, dict) else None
            if isinstance(entry, dict):
                wh, mode = normalize_charge_limit(entry.get("wh"), entry.get("mode"))
            else:
                wh, mode = normalize_charge_limit(entry, None)
            clean[name] = {"wh": wh, "mode": mode}
        self.set_meta(self.meta_key(self.LIMIT_META_KEY, device_id), json.dumps(clean))

    def _checkpoint_wal(self) -> bool:
        """WAL 达到阈值才回写主库（按阈值触发，而非无条件周期性回写）。

        SQLite 另有 wal_autocheckpoint=1000 页兜底；这里只是避免"WAL 还很小时
        也被强制回写"造成的多余磁盘写入。返回是否真的执行了 checkpoint。
        """
        try:
            wal_path = Path(str(self.db_path) + "-wal")
            if wal_path.exists() and wal_path.stat().st_size < self.WAL_CHECKPOINT_MIN_BYTES:
                return False
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            return True
        except Exception:
            return False

    def _cleanup_old_data(self):
        """按分层保留期清理：功率曲线 / 会话记录 / 会话曲线 / 每日电量。

        四层各自的 cutoff：
          · port_history        → retention_days（默认 2 天，贵）
          · charge_sessions     → session_retention_days（默认 30 天，便宜）
          · charge_points       → min(session_retention_days, POINTS_RETENTION_DAYS)
          · daily_energy        → DAILY_RETENTION_DAYS（默认 365 天）
        """
        now = time.time()
        curve_cutoff = now - (self.retention_days * 86400)
        session_cutoff = now - (self.session_retention_days * 86400)
        points_days = max(1, min(self.session_retention_days, POINTS_RETENTION_DAYS))
        points_cutoff = now - (points_days * 86400)
        daily_cutoff_day = (
            datetime.now() - timedelta(days=DAILY_RETENTION_DAYS - 1)
        ).strftime("%Y-%m-%d")

        self._conn.execute("DELETE FROM port_history WHERE timestamp < ?", (curve_cutoff,))
        # 删除顺序仍然是 points → sessions：charge_points 没有 device_id/port，
        # 归属只能靠 session_id 反查（history_down.py:52 的同一条教训）。
        #
        # 判据用"点自身时间戳"而不是"所属会话的 end_time"：记录期放大到 30 天后，
        # 后者会把详情曲线一起留 30 天。但按时间戳无差别删会踩一个回归——
        # close_stale_sessions 靠采样点给崩溃遗留会话补算能量，点没了就得到 0 Wh，
        # 随后整条会话被当成"没有能量"删掉。未闭环会话的点必须留下。
        self._conn.execute(
            """DELETE FROM charge_points WHERE timestamp < ?
               AND session_id NOT IN
                   (SELECT id FROM charge_sessions WHERE end_time IS NULL)""",
            (points_cutoff,))
        self._conn.execute(
            """DELETE FROM charge_sessions WHERE end_time IS NOT NULL AND end_time < ?""",
            (session_cutoff,))
        # 每日电量是用户资产：只按天裁剪，不参与上面任何曲线/会话的 cutoff。
        self._conn.execute("DELETE FROM daily_energy WHERE day < ?", (daily_cutoff_day,))
        self._conn.commit()

    def flush(self):
        """强制将缓冲区中的采样批量落盘提交。线程安全。"""
        if not self._conn:
            return
        with self._db_lock:
            self._flush_pending()

    def _flush_pending(self):
        """将缓冲行批量 INSERT 并提交（port_history 与 charge_points 共用一次提交）。

        调用方必须已持有 _db_lock。
        """
        if not self._pending and not self._pending_points and not self._daily:
            return
        try:
            if self._pending:
                self._conn.executemany(
                    """INSERT INTO port_history (timestamp, port, voltage, current, power, active, protocol, device_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._pending,
                )
                self._pending.clear()
            if self._pending_points:
                self._conn.executemany(
                    """INSERT INTO charge_points (session_id, timestamp, voltage, current, power, protocol)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    self._pending_points,
                )
                self._pending_points.clear()
            # 日汇总与上面两表共用同一次 commit；必须在 commit() 之前。
            self._flush_daily()
            self._conn.commit()
            with self._daily_lock:
                self._daily_inflight = None
                self._daily_overflow_warned = False
            self._last_commit = time.time()
            # 周期性清理/checkpoint 挂在提交路径上，只按时间间隔触发（每 1h / 5min）；
            # checkpoint 本身还会再判一次 WAL 阈值（见 _checkpoint_wal）。
            if self._last_commit - self._last_cleanup > 3600:
                self._cleanup_old_data()
                self._last_cleanup = self._last_commit
            if self._last_commit - self._last_wal_checkpoint > 300:
                self._checkpoint_wal()
                self._last_wal_checkpoint = self._last_commit
        except Exception as e:
            _LOGGER.error("Failed to record port data: %s", e)
            self._pending.clear()
            self._pending_points.clear()
            try:
                self._conn.rollback()
            except Exception:
                pass
            # 日汇总是不可重算的（基线已经前移）：放回缓冲，下一次写重试。
            self._restore_daily_inflight()

    # ── 每日电量（daily_energy，热力图数据源）──

    #: 会话维度的 UPSERT：只累加 count/duration/peak，**不动 wh**（wh 由增量写入）。
    _UPSERT_DAILY_SESSION = """
        INSERT INTO daily_energy (day, device_id, port, wh, session_count,
                                  duration_sec, peak_power_w, updated_at)
        VALUES (?, ?, ?, 0, 1, ?, ?, ?)
        ON CONFLICT(device_id, day, port) DO UPDATE SET
          session_count = session_count + 1,
          duration_sec  = duration_sec + excluded.duration_sec,
          peak_power_w  = MAX(peak_power_w, excluded.peak_power_w),
          updated_at    = excluded.updated_at
    """

    #: 回填用的 UPSERT：整行覆盖（回填前已 DELETE，或只写尚无增量的日期）。
    _UPSERT_DAILY_SET = """
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

    #: 非替换回填：只补缺失的 (device_id, day, port)，已有行（实时增量）绝不覆盖。
    _INSERT_DAILY_MISSING = """
        INSERT OR IGNORE INTO daily_energy
          (day, device_id, port, wh, session_count, duration_sec,
           peak_power_w, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    def add_energy_delta(self, port: int, wh: float, ts: float, *,
                         device_id: str = DEFAULT_DEVICE_ID) -> None:
        """累加一段能量到它发生的那个本地日。调用方不持锁、不做 IO。

        只做 dict 累加：它在事件循环里被每个 BLE 帧调用，一旦去抢 `_db_lock`
        就会被一次慢查询阻塞整个事件循环。
        """
        if not self._conn or wh <= 0 or not math.isfinite(wh):
            return
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        with self._daily_lock:     # 微秒级；锁序 _db_lock → _daily_lock，反向禁止
            key = (day, device_id, port)
            cur = self._daily.get(key)
            if cur is None:
                if len(self._daily) >= self.DAILY_MAX_KEYS:
                    if not self._daily_overflow_warned:
                        self._daily_overflow_warned = True
                        _LOGGER.error(
                            "daily_energy buffer full (%d keys, DB writes failing?); "
                            "dropping new deltas until a flush succeeds",
                            len(self._daily))
                    return
                cur = 0.0
            self._daily[key] = cur + wh

    def _flush_daily(self) -> None:
        """把内存缓冲落到 daily_energy（调用方必须已持 `_db_lock` 且正处在一次提交里）。

        `_daily_inflight` 一直保留到 commit 成功：executemany 或 commit 失败时
        `_restore_daily_inflight()` 会把整批按 key 加回缓冲，下一次提交重试。
        """
        with self._daily_lock:
            batch, self._daily = self._daily, {}
            if not batch:
                return
            self._daily_inflight = batch
        now = time.time()
        try:
            self._conn.executemany(
                """INSERT INTO daily_energy (day, device_id, port, wh, session_count,
                                             duration_sec, peak_power_w, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(device_id, day, port) DO UPDATE SET
                     wh            = wh + excluded.wh,
                     session_count = session_count + excluded.session_count,
                     duration_sec  = duration_sec + excluded.duration_sec,
                     peak_power_w  = MAX(peak_power_w, excluded.peak_power_w),
                     updated_at    = excluded.updated_at""",
                [(d, dev, p, v, 0, 0, 0.0, now)
                 for (d, dev, p), v in batch.items()])
        except Exception:
            self._restore_daily_inflight()
            raise

    def _restore_daily_inflight(self) -> None:
        """把没提交成功的一批日汇总按 key 加回缓冲（调用方持有 `_db_lock`）。"""
        with self._daily_lock:
            batch, self._daily_inflight = self._daily_inflight, None
            if not batch:
                return
            for key, v in batch.items():
                self._daily[key] = self._daily.get(key, 0.0) + v

    def get_daily_energy(self, days: int = 365, *,
                         device_id: str = DEFAULT_DEVICE_ID) -> dict:
        """按天聚合查询（热力图）。返回 `{"days": [...], "data_since": ...}`。

        `data_since` 取该设备**全量**的 `MIN(day)`，而不是查询窗口的起点：
        前端要用它把"统计开始之前"的格子画成无数据，而不是 0。
        """
        if not self._conn:
            return {"days": [], "data_since": None}
        days = max(1, int(days or 365))
        now = time.time()
        end_day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        start_day = (datetime.fromtimestamp(now) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        with self._db_lock:
            self._flush_pending()   # 缓冲中的增量要先落盘，否则"今天"那格不实时
            rows = self._conn.execute(
                """SELECT day, SUM(wh) AS wh, SUM(session_count) AS sessions,
                          MAX(peak_power_w) AS peak_w
                   FROM daily_energy
                   WHERE device_id = ? AND day >= ? AND day <= ?
                   GROUP BY day
                  HAVING SUM(wh) > 0
                   ORDER BY day""",
                (device_id, start_day, end_day)).fetchall()
            since = self._conn.execute(
                "SELECT MIN(day) AS d FROM daily_energy WHERE device_id = ?",
                (device_id,)).fetchone()
        return {
            "days": [{
                "date": r["day"],
                "wh": round(r["wh"] or 0.0, 2),
                "sessions": int(r["sessions"] or 0),
                "peak_w": round(r["peak_w"] or 0.0, 1),
            } for r in rows],
            "data_since": (since["d"] if since and since["d"] else None),
        }

    def backfill_daily_energy(self, *, replace: bool = True) -> dict:
        """从 charge_sessions 重建 daily_energy。

        回填口径：整段会话记在 `start_time` 所在日（与 get_energy_stats /
        get_sessions 一致）。在线增量按帧归日，两者对跨零点会话的归属不同，
        这是刻意取舍——没有采样点就无法拆分，而常供端口根本没有采样点。

        `replace=True` 会先清空整表。启动路径上表必然为空（首次升级），是空操作；
        服务跑过之后再调用会丢掉"纯增量日"，所以脚本默认拒绝非空表。
        `replace=False` 只补**缺失**的 (device, day, port)，已有行原样保留
        （与脚本 `--force` 的 INSERT OR IGNORE 语义一致）。
        """
        if not self._conn:
            return {"rows": 0, "sessions": 0, "first_day": None, "last_day": None}
        with self._db_lock:
            self._flush_pending()
            rows = self._conn.execute(
                """SELECT date(start_time, 'unixepoch', 'localtime') AS day,
                          device_id, port,
                          SUM(total_wh)      AS wh,
                          COUNT(*)           AS c,
                          SUM(duration_sec)  AS dur,
                          MAX(peak_power_w)  AS peak
                   FROM charge_sessions
                   WHERE total_wh > 0 AND end_time IS NOT NULL
                   GROUP BY day, device_id, port
                   ORDER BY day""").fetchall()
            if replace:
                self._conn.execute("DELETE FROM daily_energy")
            now = time.time()
            sql = self._UPSERT_DAILY_SET if replace else self._INSERT_DAILY_MISSING
            self._conn.executemany(
                sql,
                [(r["day"], r["device_id"], r["port"], round(r["wh"] or 0.0, 4),
                  int(r["c"] or 0), int(r["dur"] or 0), round(r["peak"] or 0.0, 2), now)
                 for r in rows])
            self._conn.commit()
        return {
            "rows": len(rows),
            "sessions": sum(int(r["c"] or 0) for r in rows),
            "first_day": rows[0]["day"] if rows else None,
            "last_day": rows[-1]["day"] if rows else None,
        }

    def _backfill_daily_if_needed(self) -> None:
        """启动时的一次性回填。失败只记日志，不拖垮启动（下次启动会重试）。"""
        if self.get_meta("daily_stats_version", ""):
            return
        try:
            # 表非空说明已经有真实数据（旧库没写版本号、或手工恢复了备份）：
            # 只能补缺失键，绝不能 DELETE 后重建——纯增量日没有会话可重算，
            # 清掉就是静默丢用户资产。
            empty = self._conn.execute(
                "SELECT 1 FROM daily_energy LIMIT 1").fetchone() is None
            result = self.backfill_daily_energy(replace=empty)
            self.set_meta("daily_stats_version", "1")
            self.set_meta("daily_stats_since",
                          time.strftime("%Y-%m-%d", time.localtime()))
            _LOGGER.info("daily_energy backfilled: %d rows / %d sessions (%s ~ %s)",
                         result["rows"], result["sessions"],
                         result["first_day"], result["last_day"])
        except Exception as e:
            _LOGGER.warning("daily_energy backfill failed (will retry next start): %s", e)

    def audit_daily_energy(self) -> dict:
        """日汇总 vs 会话表按 (day, device_id, port) 对账，返回漂移。"""
        if not self._conn:
            return {"daily_wh": 0.0, "session_wh": 0.0, "drift": []}
        with self._db_lock:
            self._flush_pending()
            daily = {(r["day"], r["device_id"], r["port"]): (r["wh"] or 0.0)
                     for r in self._conn.execute(
                         """SELECT day, device_id, port, SUM(wh) AS wh
                            FROM daily_energy GROUP BY day, device_id, port""")}
            ses = {(r["day"], r["device_id"], r["port"]): (r["wh"] or 0.0)
                   for r in self._conn.execute(
                       """SELECT date(start_time, 'unixepoch', 'localtime') AS day,
                                 device_id, port, SUM(total_wh) AS wh
                          FROM charge_sessions WHERE total_wh > 0
                          GROUP BY day, device_id, port""")}
        drift = []
        for key in sorted(set(daily) | set(ses)):
            a, b = daily.get(key, 0.0), ses.get(key, 0.0)
            if abs(a - b) > max(0.05, 0.01 * max(abs(a), abs(b))):
                drift.append({"day": key[0], "device_id": key[1], "port": key[2],
                              "daily_wh": round(a, 2), "session_wh": round(b, 2)})
        return {"daily_wh": round(sum(daily.values()), 2),
                "session_wh": round(sum(ses.values()), 2),
                "drift": drift}

    def purge_daily_energy(self, before_day: str) -> int:
        """删除 `before_day` 之前的日汇总，返回删除行数。供脚本使用。"""
        if not self._conn or not before_day:
            return 0
        with self._db_lock:
            cur = self._conn.execute(
                "DELETE FROM daily_energy WHERE day < ?", (before_day,))
            self._conn.commit()
            return cur.rowcount

    def record_port_data(self, port: int, data: dict, *,
                         device_id: str = DEFAULT_DEVICE_ID):
        """Record port data to database (synchronous, called from async via executor).

        批量缓冲：攒到 BATCH_SIZE 或距上次提交超过 BATCH_INTERVAL 再批量写入，
        大幅降低高频采样下的 COMMIT 次数与写放大。读取路径会自动 flush 未落盘
        的缓冲行，保证"写入后立即读取可见"。

        `device_id` 是**关键字参数且排在最后**（§3.9 的签名约束）：`port` 无默认值、
        `device_id` 有默认值，位置参数顺序不允许；而且既有 540 个用例都以
        `record_port_data(port, data)` 调用，加在第一位会全挂。缺省值 = primary，
        因此单设备部署的写入与加列之前逐字节等价。
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
            device_id,
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
        interval: Optional[int] = None,
        *,
        device_id: str = DEFAULT_DEVICE_ID,
    ) -> list[dict]:
        """Query port history with optional downsampling.

        Args:
            port: Port number (1-4)
            hours: Number of hours to query
            interval: Aggregation interval in seconds (None = raw data)
            device_id: 只返回该设备的采样（缺省 primary ⇒ 与加列前一致）
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
                WHERE port = ? AND timestamp >= ? AND device_id = ?
                GROUP BY bucket
                ORDER BY bucket""",
                (interval, interval, port, cutoff, device_id)
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT timestamp, voltage, current, power, active, protocol
                FROM port_history
                WHERE port = ? AND timestamp >= ? AND device_id = ?
                ORDER BY timestamp""",
                (port, cutoff, device_id)
            ).fetchall()

        return [dict(row) for row in rows]

    def get_statistics(self, port: int, hours: int = 24, *,
                       device_id: str = DEFAULT_DEVICE_ID) -> dict:
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
                    (SELECT SUM(p.power * MIN(p.timestamp - p.prev_ts, ?)) / 3600.0
                     FROM (
                         SELECT timestamp, power,
                                LAG(timestamp) OVER (ORDER BY timestamp) as prev_ts
                         FROM port_history
                         WHERE port = ? AND timestamp >= ? AND active = 1 AND device_id = ?
                     ) p
                     WHERE p.prev_ts IS NOT NULL),
                0) as energy_wh
            FROM port_history
            WHERE port = ? AND timestamp >= ? AND device_id = ?""",
            # 采样断档上限 30s：闲置/掉线期间相邻两个采样点可能相隔很久, 不封顶会
            # 把这段空档按某个功率"积分"成能量（凭空计费）。
            (self.ENERGY_GAP_CAP_SEC, port, cutoff, device_id, port, cutoff, device_id)
        ).fetchone()

        if not row or row["samples"] == 0:
            return {"port": port, "hours": hours, "samples": 0}

        return {
            "port": port,
            "hours": hours,
            "samples": row["samples"],
            "first_seen": datetime.fromtimestamp(row["first_seen"]).isoformat() if row["first_seen"] else None,
            "last_seen": datetime.fromtimestamp(row["last_seen"]).isoformat() if row["last_seen"] else None,
            # 注意用 `is not None` 而不是真值判断：空载端口的 min/avg 就是 0，
            # 用真值判断会把它当成"没有数据"返回 null（历史遗留 bug）。
            "voltage": {
                "avg": round(row["avg_voltage"], 2) if row["avg_voltage"] is not None else None,
                "min": round(row["min_voltage"], 2) if row["min_voltage"] is not None else None,
                "max": round(row["max_voltage"], 2) if row["max_voltage"] is not None else None,
            },
            "current": {
                "avg": round(row["avg_current"], 2) if row["avg_current"] is not None else None,
                "max": round(row["max_current"], 2) if row["max_current"] is not None else None,
            },
            "power": {
                "avg": round(row["avg_power"], 2) if row["avg_power"] is not None else None,
                "max": round(row["max_power"], 2) if row["max_power"] is not None else None,
                "total_wh": round(row["energy_wh"], 2) if row["energy_wh"] is not None else 0,
            },
            "active_ratio": round(row["active_count"] / row["samples"], 2) if row["samples"] > 0 else 0,
        }

    def export_csv(self, port: int, hours: int = 24, *,
                   device_id: str = DEFAULT_DEVICE_ID) -> str:
        """Export port history as CSV string."""
        if not self._conn:
            return ""

        self.flush()

        cutoff = time.time() - (hours * 3600)
        rows = self._conn.execute(
            """SELECT timestamp, voltage, current, power, active, protocol
            FROM port_history
            WHERE port = ? AND timestamp >= ? AND device_id = ?
            ORDER BY timestamp""",
            (port, cutoff, device_id)
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

    def export_session_csv(self, session_id: int, *,
                           device_id: Optional[str] = None) -> str:
        """把**单个充电会话**的采样点导成 CSV。

        与 export_csv(port, hours) 的区别：那个按端口+时间窗导原始采样（包含会话之间的
        空载片段），这个只导某一次会话的点（充电曲线本体），供会话详情浮层的"导出"用。
        第一行是会话元信息（# 开头的注释行，Excel/Numbers 会当文本行保留），
        之后是标准表头 + 采样点。

        `device_id` 非 None 时校验会话归属（§3.9）：不属于该设备一律按"不存在"处理，
        不泄露"这个 id 存在"。None = 内部路径，不校验。
        """
        if not self._conn or not session_id:
            return ""

        self.flush()

        sess = self._conn.execute(
            """SELECT id, port, start_time, end_time, total_wh, avg_power_w, peak_power_w,
                      avg_voltage, avg_current, duration_sec, protocol, device_id
               FROM charge_sessions WHERE id = ?""",
            (session_id,),
        ).fetchone()
        if not sess or (device_id is not None and sess["device_id"] != device_id):
            return ""

        pts = self._conn.execute(
            """SELECT timestamp, voltage, current, power, protocol
               FROM charge_points WHERE session_id = ? ORDER BY timestamp""",
            (session_id,),
        ).fetchall()

        def _ts(v):
            return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S") if v else ""

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([f"# session {sess['id']}",
                         f"port {PORT_NAMES.get(sess['port'], sess['port'])}",
                         f"protocol {sess['protocol'] or ''}"])
        writer.writerow([f"# start {_ts(sess['start_time'])}",
                         f"end {_ts(sess['end_time'])}",
                         f"duration_s {sess['duration_sec'] or 0}"])
        writer.writerow([f"# energy_wh {round(sess['total_wh'] or 0, 2)}",
                         f"avg_power_w {round(sess['avg_power_w'] or 0, 2)}",
                         f"peak_power_w {round(sess['peak_power_w'] or 0, 2)}"])
        writer.writerow([])
        writer.writerow(["timestamp", "datetime", "voltage", "current", "power", "protocol"])
        for p in pts:
            writer.writerow([
                p["timestamp"], _ts(p["timestamp"]),
                p["voltage"], p["current"], p["power"], p["protocol"],
            ])
        return output.getvalue()

    def query_history_multi(self, start_port: int, end_port: int, hours: float, interval: int,
                            *, device_id: str = DEFAULT_DEVICE_ID) -> list[dict]:
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
            WHERE port >= ? AND port <= ? AND timestamp >= ? AND device_id = ?
            GROUP BY port, bucket
            ORDER BY port, bucket""",
            (interval, interval, start_port, end_port, cutoff, device_id)
        ).fetchall()

        return [dict(row) for row in rows]

    # ── Charge Session Management ──

    def start_session(self, port: int, protocol: str = "",
                      start_time: Optional[float] = None, *,
                      device_id: str = DEFAULT_DEVICE_ID) -> int:
        """Start a new charge session, return session_id.

        start_time 显式传入时用它（开会话时会把起判前那 30s 回填进本会话，起点必须
        跟着回填，否则 start_time + duration_sec 会比 end_time 多出一截，历史行自相矛盾）。
        """
        if not self._conn:
            return 0
        with self._db_lock:
            try:
                cursor = self._conn.execute(
                    """INSERT INTO charge_sessions (port, start_time, protocol, device_id)
                       VALUES (?, ?, ?, ?)""",
                    (port, time.time() if start_time is None else float(start_time),
                     protocol, device_id),
                )
                self._conn.commit()
                return cursor.lastrowid
            except Exception as e:
                _LOGGER.error("Failed to start session: %s", e)
                return 0

    def record_charge_point(self, session_id: int, voltage: float,
                            current: float, power: float, protocol: str = ""):
        """缓冲一条会话采样点；攒够 BATCH_SIZE 或超过 BATCH_INTERVAL 才批量落盘。

        原先每点一次 INSERT + COMMIT（充电时约 0.8 点/秒 → 每秒一次 fsync），
        是磁盘写入放大的主要来源。现与 port_history 共用缓冲与同一次提交。
        读取路径（get_session_points / compute_session_avg_vi / end_session）
        都会先 flush()，保证"写入后立即读取可见"。
        """
        if not self._conn or not session_id:
            return
        row = (session_id, time.time(), voltage, current, power, protocol)
        with self._db_lock:
            self._pending_points.append(row)
            now = time.time()
            if (len(self._pending_points) >= self.BATCH_SIZE
                    or now - self._last_commit >= self.BATCH_INTERVAL):
                self._flush_pending()

    def record_charge_points(self, session_id: int, rows) -> int:
        """批量缓冲会话采样点（补写预会话回填用），返回入队条数。

        与 record_charge_point 共用同一个待写缓冲与提交节奏，区别只在：
          · 一次拿锁入队多行（回填最长 120 行，别逐行开 executor）；
          · 时间戳用采样时的真实时间，而不是入队时刻——否则回填的点会全部挤在
            "开会话"那一秒上，曲线头部被压成一根竖线。
        rows: 可迭代的 (timestamp, voltage, current, power, protocol)
        """
        if not self._conn or not session_id or not rows:
            return 0
        queued = [(session_id, float(t), v, i, p, proto) for t, v, i, p, proto in rows]
        with self._db_lock:
            self._pending_points.extend(queued)
            if (len(self._pending_points) >= self.BATCH_SIZE
                    or time.time() - self._last_commit >= self.BATCH_INTERVAL):
                self._flush_pending()
        return len(queued)

    def compute_session_avg_vi(self, session_id: int) -> tuple:
        """从本会话采样点计算时间加权均压/均流, 返回 (avg_v, avg_i) 或 (None, None)。

        闭合会话时传入的是"断电/低电流那一刻"的瞬时值(≈0), 直接落库会让历史
        摘要的均压/均流恒为 0。这里改用采样点做梯形(时间)加权平均, 与桌面端
        口径一致; 断档超过 30 分钟的区间不参与, 避免把空载挂载态拉低均值。
        """
        if not self._conn or not session_id:
            return None, None
        # 先把缓冲中的采样点落盘，否则会漏掉最近几秒的点，均值偏低
        self.flush()
        try:
            # 与写路径共用同一把锁：本方法会被 end_session / 启动回填在
            # executor 线程上调用，与 record_charge_point 的写并发访问同一条
            # sqlite 连接（check_same_thread=False），不加锁会读到写事务中间态。
            with self._db_lock:
                rows = self._conn.execute(
                    """SELECT timestamp, voltage, current FROM charge_points
                       WHERE session_id = ? ORDER BY timestamp""",
                    (session_id,)
                ).fetchall()
        except Exception as e:
            _LOGGER.error("Failed to read charge points for avg: %s", e)
            return None, None
        pts = [(r["timestamp"], r["voltage"], r["current"]) for r in rows
               if r["voltage"] is not None and r["current"] is not None]
        if len(pts) < 2:
            if len(pts) == 1:
                return float(pts[0][1]), float(pts[0][2])
            return None, None
        wsum = vsum = isum = 0.0
        for (t0, v0, i0), (t1, v1, i1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt <= 0 or dt > self.AVG_VI_GAP_CAP_SEC:
                continue
            # 梯形加权: 区间内以两端均值代表该段
            wsum += dt
            vsum += dt * (v0 + v1) / 2.0
            isum += dt * (i0 + i1) / 2.0
        if wsum <= 0:
            # 全部区间都超限: 退化为算术平均, 至少不是 0
            return (sum(p[1] for p in pts) / len(pts),
                    sum(p[2] for p in pts) / len(pts))
        return vsum / wsum, isum / wsum

    def backfill_session_avg_vi(self) -> int:
        """启动回填: 重算历史中 avg=0 但有采样点的已闭合会话。返回修复行数。

        批量提交（单次 commit）而不是每条一次：启动阶段可能有大量历史脏行,
        逐条 fsync 会明显拖慢就绪时间。
        """
        if not self._conn:
            return 0
        fixed = 0
        updates = []
        try:
            with self._db_lock:
                ids = [r["id"] for r in self._conn.execute(
                    """SELECT id FROM charge_sessions
                       WHERE end_time IS NOT NULL
                         AND (avg_voltage IS NULL OR avg_voltage = 0)
                         AND EXISTS (SELECT 1 FROM charge_points
                                     WHERE session_id = charge_sessions.id)"""
                ).fetchall()]
        except Exception as e:
            _LOGGER.error("Failed to scan sessions for backfill: %s", e)
            return 0
        for sid in ids:
            avg_v, avg_i = self.compute_session_avg_vi(sid)
            if avg_v is None:
                continue
            updates.append((round(avg_v, 2), round(avg_i, 2), sid))
        if not updates:
            return 0
        try:
            with self._db_lock:
                self._conn.executemany(
                    """UPDATE charge_sessions
                       SET avg_voltage = ?, avg_current = ? WHERE id = ?""",
                    updates)
                self._conn.commit()
            fixed = len(updates)
        except Exception as e:
            _LOGGER.error("Failed to backfill session averages: %s", e)
            return 0
        if fixed:
            _LOGGER.info("Backfilled avg voltage/current for %d sessions", fixed)
        return fixed

    def end_session(self, session_id: int, total_wh: float, peak_power_w: float,
                    avg_voltage: float, avg_current: float, duration_sec: int,
                    end_time: Optional[float] = None):
        """End a charge session with final stats.

        avg_voltage/avg_current 传入的是闭合瞬间的瞬时值(通常已归零), 仅作为
        "没有采样点"时的回落; 有采样点时一律用采样点的时间加权均值重算。
        end_time 缺省为当前时刻；收尾遗留会话时传"最后一个采样点"更诚实。
        """
        if not self._conn or not session_id:
            return
        avg_power = total_wh / (duration_sec / 3600.0) if duration_sec > 0 else 0
        calc_v, calc_i = self.compute_session_avg_vi(session_id)
        if calc_v is not None:
            avg_voltage, avg_current = calc_v, calc_i
        with self._db_lock:
            try:
                # 先读旧 end_time：只有"本次真正闭合"的行才累加会话次数/时长/峰值。
                # 重复调用（异常路径 / 关停批量清理）不能把同一次充电记两遍。
                row = self._conn.execute(
                    """SELECT start_time, port, device_id, end_time
                       FROM charge_sessions WHERE id = ?""",
                    (session_id,)).fetchone()
                self._conn.execute(
                    """UPDATE charge_sessions SET
                       end_time = ?, total_wh = ?, avg_power_w = ?,
                       peak_power_w = ?, avg_voltage = ?, avg_current = ?,
                       duration_sec = ?
                       WHERE id = ?""",
                    (end_time if end_time is not None else time.time(),
                     round(total_wh, 4), round(avg_power, 2),
                     round(peak_power_w, 2), round(avg_voltage, 2),
                     round(avg_current, 2), duration_sec, session_id),
                )
                if row is not None and row["end_time"] is None:
                    # 会话维度统计的唯一写入口：close_stale_sessions（崩溃遗留）
                    # 也经这里，自动获得同样的行为。
                    day = time.strftime("%Y-%m-%d", time.localtime(row["start_time"]))
                    self._conn.execute(
                        self._UPSERT_DAILY_SESSION,
                        (day, row["device_id"], row["port"],
                         int(duration_sec or 0), round(peak_power_w or 0.0, 2),
                         time.time()))
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to end session: %s", e)

    def compute_session_totals(self, session_id: int) -> tuple:
        """按采样点现算本次会话的 (能量 Wh, 峰值功率 W)。

        会话进行中 charge_sessions.total_wh / peak_power_w 还是 0（只在
        end_session 时才写），所以收尾遗留会话不能信这两列，得按点现算——
        与前端曲线的梯形积分同一口径，断档超限的区间不参与。
        """
        if not self._conn or not session_id:
            return 0.0, 0.0
        with self._db_lock:
            self._flush_pending()
            rows = self._conn.execute(
                """SELECT timestamp, power FROM charge_points
                   WHERE session_id = ? ORDER BY timestamp""",
                (session_id,)).fetchall()
        pts = [(r["timestamp"], r["power"] or 0.0) for r in rows]
        if not pts:
            return 0.0, 0.0
        peak = max(p for _, p in pts)
        if len(pts) < 2:
            return 0.0, peak
        wh = 0.0
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt <= 0 or dt > self.AVG_VI_GAP_CAP_SEC:
                continue
            wh += (p0 + p1) / 2.0 * dt / 3600.0
        return wh, peak

    def close_stale_sessions(self, device_id: Optional[str] = None) -> int:
        """收尾上一次进程遗留的未闭合会话（end_time IS NULL），返回处理条数。

        正常关机时 BLEManager._close_active_sessions 会把它们写完整；只有被强杀
        /崩溃才会留下这种行——列表会永远把它们当"充电中"。能量与峰值按采样点
        现算（进行中的行这两列还是 0），结束时刻取最后一个采样点（没有点就退回
        start_time）；能量过小的按常规口径删除，避免留下 0Wh 空行。

        启动清理的唯一入口是 connect()（运行中不能调用：那时 end_time IS NULL
        就是"正在充电"的正常状态）。

        `device_id=None`（缺省）= **全设备**收尾，正是 connect() 要的语义：多设备下
        若按某一台收尾，其他设备崩溃遗留的行会永远挂在库里（幽灵会话、once 限额
        永不释放）。`DeviceHistory` facade 传自己的 id，只收本设备。
        """
        if not self._conn:
            return 0
        device_clause = "" if device_id is None else " AND device_id = ?"
        device_params: tuple = () if device_id is None else (device_id,)
        with self._db_lock:
            self._flush_pending()
            rows = self._conn.execute(
                """SELECT id, start_time, total_wh, peak_power_w
                   FROM charge_sessions WHERE end_time IS NULL""" + device_clause,
                device_params).fetchall()
        closed = 0
        for row in rows:
            sid = row["id"]
            with self._db_lock:
                last = self._conn.execute(
                    "SELECT MAX(timestamp) AS ts FROM charge_points WHERE session_id = ?",
                    (sid,)).fetchone()
            start_ts = row["start_time"] or time.time()
            end_ts = (last["ts"] if last and last["ts"] else None) or start_ts
            if end_ts < start_ts:
                end_ts = start_ts
            total_wh = row["total_wh"] or 0.0
            peak = row["peak_power_w"] or 0.0
            if total_wh <= 0 or peak <= 0:
                calc_wh, calc_peak = self.compute_session_totals(sid)
                total_wh = total_wh or calc_wh
                peak = peak or calc_peak
            if total_wh < 0.05:      # 与 _close_session 同一门槛：没有能量的行不留
                self.delete_session(sid)
            else:
                self.end_session(sid, total_wh, peak, 0.0, 0.0,
                                 int(end_ts - start_ts), end_time=end_ts)
            closed += 1
        if closed:
            _LOGGER.info("Closed %d stale session(s) left open by a previous run", closed)
        return closed

    def delete_session(self, session_id: int):
        """Delete a session and its points (for 0Wh sessions)."""
        if not self._conn or not session_id:
            return
        with self._db_lock:
            try:
                # 缓冲中的点要先落盘再删，否则删除之后 flush 会把它们又插回来。
                # （_flush_pending 会清空缓冲，所以删完无需再过滤 _pending_points）
                self._flush_pending()
                self._conn.execute("DELETE FROM charge_points WHERE session_id = ?", (session_id,))
                self._conn.execute("DELETE FROM charge_sessions WHERE id = ?", (session_id,))
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to delete session: %s", e)

    def get_sessions(self, port: Optional[int] = None, period: str = "today",
                     limit: int = 10, offset: int = 0, *,
                     device_id: str = DEFAULT_DEVICE_ID) -> tuple:
        """Query charge sessions（只返回 `device_id` 名下的会话）。"""
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
                   EXISTS(SELECT 1 FROM charge_points
                          WHERE charge_points.session_id = charge_sessions.id)
                       AS has_points,
                   COUNT(*) OVER() AS total
                   FROM charge_sessions
                   WHERE start_time >= ? AND total_wh > 0 AND device_id = ?"""
        params = [cutoff, device_id]

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

    def get_session(self, session_id: int, *, device_id: Optional[str] = None) -> Optional[dict]:
        """单个会话行（详情浮层用）。

        长期供电会话不保存曲线点，但会话统计照常落库，因此详情要能单独取回这一行。

        `device_id` 非 None 时校验归属，不匹配按"不存在"返回 None（§3.9：会话级读
        也必须校验，否则 `/api/device/desk/sessions/<primary 的 id>` 能越权读到
        primary 的历史）。
        """
        if not self._conn:
            return None
        # flush + 查询必须同一把锁内完成：本方法与 get_session_points 一样会被
        # handle_session_points 在 executor 线程上调用，与 record_charge_point 的
        # 写并发访问同一条 sqlite 连接（check_same_thread=False）。只锁住 flush
        # 的话，紧接着的 SELECT 仍可能读到写事务的中间态。
        sql = """SELECT id, port, start_time, end_time, total_wh, avg_power_w,
                          peak_power_w, avg_voltage, avg_current, duration_sec, protocol
                   FROM charge_sessions WHERE id = ?"""
        params: tuple = (session_id,)
        if device_id is not None:
            # 归属校验放进 WHERE 而不是取回来再比对：返回的 dict 形状必须与加列前
            # 完全一致（`/api/sessions` 会把它直接透给前端，多一个 device_id 就是
            # 外部可见变化，见 §3.8）。
            sql += " AND device_id = ?"
            params = (session_id, device_id)
        with self._db_lock:
            self._flush_pending()   # 缓冲中的点要先落盘，否则详情取不到最近几秒
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def get_session_points(self, session_id: int, *,
                           device_id: Optional[str] = None) -> list[dict]:
        """Get all data points for a charge session.

        归属校验同 get_session：先确认会话属于该设备，否则返回空列表。
        """
        if not self._conn:
            return []
        # 同上：flush 与 SELECT 必须在同一把锁内，否则能读到写事务中间态。
        with self._db_lock:
            self._flush_pending()   # 缓冲中的点要先落盘，否则详情图会缺最近几秒
            if device_id is not None:
                owner = self._conn.execute(
                    "SELECT device_id FROM charge_sessions WHERE id = ?",
                    (session_id,)).fetchone()
                if not owner or owner["device_id"] != device_id:
                    return []
            rows = self._conn.execute(
                """SELECT timestamp, voltage, current, power, protocol
                   FROM charge_points WHERE session_id = ?
                   ORDER BY timestamp""",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _period_window(period: str):
        """把周期名换算成 [start, end) 时间窗；end=None 表示"到此刻为止"。

        口径说明：today / yesterday 是自然日；week / month 是**滚动** 7 / 30 天，
        不是自然周 / 自然月——前端的文案因此写"近 7 天 / 近 30 天"，两者必须一致。
        """
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "today":
            return midnight.timestamp(), None
        if period == "yesterday":
            return midnight.timestamp() - 86400, midnight.timestamp()
        if period == "week":
            return time.time() - 7 * 86400, None
        if period == "month":
            return time.time() - 30 * 86400, None
        return 0.0, None

    def get_energy_stats(self, period: str = "today", *,
                         device_id: str = DEFAULT_DEVICE_ID) -> dict:
        """Get aggregated energy statistics（只统计该设备名下的会话）。"""
        if not self._conn:
            return {"period": period, "total_wh": 0, "session_count": 0}

        start, end = self._period_window(period)
        where = "start_time >= ?" + (" AND start_time < ?" if end is not None else "") + " AND device_id = ?"
        params = ([start] if end is None else [start, end]) + [device_id]

        row = self._conn.execute(
            f"""SELECT
                COUNT(*) as session_count,
                COALESCE(SUM(total_wh), 0) as total_wh,
                COALESCE(MAX(peak_power_w), 0) as peak_power_w,
                COALESCE(SUM(duration_sec), 0) as total_duration_sec
            FROM charge_sessions WHERE {where} AND total_wh > 0""",
            params,
        ).fetchone()

        by_port = self._conn.execute(
            f"""SELECT port, COALESCE(SUM(total_wh), 0) as wh, COUNT(*) as count
               FROM charge_sessions WHERE {where} AND total_wh > 0
               GROUP BY port""",
            params,
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

    def get_protocol_stats(self, period: str = "today", *,
                           device_id: str = DEFAULT_DEVICE_ID) -> dict:
        """按充电协议聚合电量与会话数（"快充到底跑没跑上"）。

        口径与 get_energy_stats 完全一致（同一个 _period_window），所以卡片上两个
        视图的合计值能对上。历史记录里 protocol 可能为空（早于该列存在），
        统一归到 'unknown' 而不是丢掉——否则各协议之和对不上总量。
        """
        if not self._conn:
            return {"period": period, "protocols": [], "total_wh": 0, "session_count": 0}

        start, end = self._period_window(period)
        where = "start_time >= ?" + (" AND start_time < ?" if end is not None else "") + " AND device_id = ?"
        params = ([start] if end is None else [start, end]) + [device_id]

        rows = self._conn.execute(
            f"""SELECT COALESCE(NULLIF(TRIM(protocol), ''), 'unknown') AS proto,
                       COALESCE(SUM(total_wh), 0) AS wh,
                       COUNT(*) AS count,
                       COALESCE(MAX(peak_power_w), 0) AS peak_w
                FROM charge_sessions WHERE {where} AND total_wh > 0
                GROUP BY proto ORDER BY wh DESC""",
            params,
        ).fetchall()

        protocols = [
            {"protocol": r["proto"], "wh": round(r["wh"], 2),
             "count": r["count"], "peak_w": round(r["peak_w"], 1)}
            for r in rows
        ]
        return {
            "period": period,
            "protocols": protocols,
            "total_wh": round(sum(p["wh"] for p in protocols), 2),
            "session_count": sum(p["count"] for p in protocols),
        }


class DeviceHistory:
    """共享 `PortHistory` 的**按设备视图**（设计文档 §3.2）。

    存在理由：`BLEManager` 有 12 处 `self._history.*` 写入、`Server` 有 16 处
    `self.history.*` 调用，签名全部保持"无 device 参数"的样子——由本 facade 在内部
    代填 `device_id`。这样两个调用方一行不改，也不会串设备。

    两条纪律：

    1. **显式白名单，不用 `__getattr__` 兜底**。P0-1 的失败模式就是"漏了一项装配、
       而且静默"：隐式代理会让"忘了给某个写入带 device_id"变成一个永远不会被发现的
       bug（写进 default 行，界面上看起来只是"历史少了一段"）。写清单意味着新增
       PortHistory 方法时这里会 AttributeError，逼人做决定。
    2. **不代理连接生命周期**。`connect()/close()/flush()` 属于共享 store，绝不能
       经某台设备的视图去关——`close_all()` 关闭共享连接会顺手掐掉其它设备。
       服务级 meta（`web_language`）同理不经此视图（§3.9）。
    """

    __slots__ = ("store", "device_id")

    def __init__(self, store: "PortHistory", device_id: str = DEFAULT_DEVICE_ID):
        self.store = store
        self.device_id = device_id or DEFAULT_DEVICE_ID

    # ── 每日电量（设备维度）──
    def add_energy_delta(self, port: int, wh: float, ts: float):
        return self.store.add_energy_delta(port, wh, ts, device_id=self.device_id)

    def get_daily_energy(self, days: int = 365):
        return self.store.get_daily_energy(days, device_id=self.device_id)

    # ── 采样写入 / 查询（设备维度）──
    def record_port_data(self, port: int, data: dict):
        return self.store.record_port_data(port, data, device_id=self.device_id)

    def query_history(self, port: int, hours: int = 24, interval: Optional[int] = None):
        return self.store.query_history(port, hours, interval, device_id=self.device_id)

    def query_history_multi(self, start_port: int, end_port: int, hours: float, interval: int):
        return self.store.query_history_multi(
            start_port, end_port, hours, interval, device_id=self.device_id)

    def get_statistics(self, port: int, hours: int = 24):
        return self.store.get_statistics(port, hours, device_id=self.device_id)

    def export_csv(self, port: int, hours: int = 24):
        return self.store.export_csv(port, hours, device_id=self.device_id)

    # ── 会话（设备维度）──
    def start_session(self, port: int, protocol: str = "",
                      start_time: Optional[float] = None) -> int:
        return self.store.start_session(port, protocol, start_time,
                                        device_id=self.device_id)

    def get_sessions(self, port: Optional[int] = None, period: str = "today",
                     limit: int = 10, offset: int = 0):
        return self.store.get_sessions(port, period, limit, offset,
                                       device_id=self.device_id)

    def get_energy_stats(self, period: str = "today"):
        return self.store.get_energy_stats(period, device_id=self.device_id)

    def get_protocol_stats(self, period: str = "today"):
        return self.store.get_protocol_stats(period, device_id=self.device_id)

    def close_stale_sessions(self) -> int:
        """只收本设备的遗留会话（store.connect() 里的那个是 None=全设备）。"""
        return self.store.close_stale_sessions(self.device_id)

    # ── 会话级读：带归属校验（§3.9 越权读）──
    def get_session(self, session_id: int):
        return self.store.get_session(session_id, device_id=self.device_id)

    def get_session_points(self, session_id: int):
        return self.store.get_session_points(session_id, device_id=self.device_id)

    def export_session_csv(self, session_id: int):
        return self.store.export_session_csv(session_id, device_id=self.device_id)

    # ── 会话级写：sid 已锁定归属，不再校验（传 None 的语义同 PortHistory）──
    def record_charge_point(self, session_id: int, voltage: float,
                            current: float, power: float, protocol: str = ""):
        return self.store.record_charge_point(session_id, voltage, current, power, protocol)

    def record_charge_points(self, session_id: int, rows) -> int:
        return self.store.record_charge_points(session_id, rows)

    def end_session(self, session_id: int, total_wh: float, peak_power_w: float,
                    avg_voltage: float, avg_current: float, duration_sec: int,
                    end_time: Optional[float] = None):
        return self.store.end_session(session_id, total_wh, peak_power_w,
                                      avg_voltage, avg_current, duration_sec, end_time)

    def delete_session(self, session_id: int):
        return self.store.delete_session(session_id)

    def compute_session_avg_vi(self, session_id: int) -> tuple:
        return self.store.compute_session_avg_vi(session_id)

    def compute_session_totals(self, session_id: int) -> tuple:
        return self.store.compute_session_totals(session_id)

    # ── 设备级 meta（§3.9：非 primary 走 `{id}:{key}` 命名空间）──
    def get_session_recording(self) -> bool:
        return self.store.get_session_recording(device_id=self.device_id)

    def set_session_recording(self, enabled: bool) -> None:
        return self.store.set_session_recording(enabled, device_id=self.device_id)

    def get_permanent_ports(self) -> list:
        return self.store.get_permanent_ports(device_id=self.device_id)

    def set_permanent_ports(self, names) -> list:
        return self.store.set_permanent_ports(names, device_id=self.device_id)

    def get_full_off(self) -> dict:
        return self.store.get_full_off(device_id=self.device_id)

    def set_full_off(self, entries) -> dict:
        return self.store.set_full_off(entries, device_id=self.device_id)

    def get_charge_limits(self) -> dict:
        return self.store.get_charge_limits(device_id=self.device_id)

    def set_charge_limits(self, limits: dict) -> None:
        return self.store.set_charge_limits(limits, device_id=self.device_id)

    # ── 缓冲区可见性（只读触发，不拥有缓冲）──
    def flush(self) -> None:
        return self.store.flush()

    def __repr__(self) -> str:  # 日志/断言里能看出是哪个设备的视图
        return f"<DeviceHistory device_id={self.device_id!r} store={id(self.store):#x}>"
