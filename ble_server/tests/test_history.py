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

    def test_legacy_array_meta_upgrades_to_always(self, history):
        """旧版本把"充满即停"存成数组（没有模式），读到要升级成 always。"""
        from history import PortHistory
        history.set_meta(PortHistory.FULL_OFF_PORTS_META_KEY, '["c2"]')
        assert history.get_full_off() == {"c2": "always"}

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

    def test_connect_closes_orphan_sessions(self, temp_db):
        """崩溃遗留的未结束会话（end_time IS NULL）在下次启动 connect 时收尾。

        收尾＝按采样点补算能量/峰值、结束时刻取最后一个采样点，会话与曲线都留着；
        只有能量过小（<0.05Wh，等同没充过）的才按常规口径删除。以前这里是另一套
        "直接 DELETE"的清理，会把一整段充电的数据丢掉，而且它先跑，让收尾永远
        无事可做。
        """
        from history import PortHistory

        h1 = PortHistory(db_path=temp_db)
        h1.connect()
        t0 = time.time() - 600
        # ① 有能量的孤儿：10 段 × 60s × 9W ≈ 1.5Wh
        sid_wh = h1.start_session(1, protocol="PD")
        # ② 只有一个采样点的孤儿：梯形积分不出能量
        sid_empty = h1.start_session(3, protocol="PD")
        with h1._db_lock:
            h1._conn.execute("UPDATE charge_sessions SET start_time = ? WHERE id = ?",
                             (t0, sid_wh))
            for k in range(11):
                h1._conn.execute(
                    """INSERT INTO charge_points
                       (session_id, timestamp, voltage, current, power, protocol)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (sid_wh, t0 + k * 60, 9.0, 1.0, 9.0, "PD"))
            h1._conn.execute(
                """INSERT INTO charge_points
                   (session_id, timestamp, voltage, current, power, protocol)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid_empty, t0, 9.0, 1.0, 9.0, "PD"))
            h1._conn.commit()
        h1.close()      # 不调用 end_session，模拟进程崩溃

        h2 = PortHistory(db_path=temp_db)
        h2.connect()    # 应在这里收尾

        kept = h2.get_session(sid_wh)
        assert kept is not None, "有能量的遗留会话必须被收尾，而不是删掉"
        assert kept["end_time"] is not None
        assert abs(kept["end_time"] - (t0 + 600)) < 1.0, "结束时刻取最后一个采样点"
        assert kept["total_wh"] == pytest.approx(1.5, abs=0.05), "能量按采样点现算"
        assert h2.get_session_points(sid_wh), "采样点要留着（曲线还在）"
        sessions, _ = h2.get_sessions(port=1, period="all")
        assert [s["id"] for s in sessions] == [sid_wh]

        assert h2.get_session(sid_empty) is None, "没有能量的孤儿按常规口径删除"
        assert h2.get_session_points(sid_empty) == []
        h2.close()


class TestSessionAvgVI:
    """均压/均流：闭合时传入的是断电瞬间值(≈0), 必须从采样点重算。"""

    def _point(self, history, sid, ts, v, i):
        history._conn.execute(
            """INSERT INTO charge_points (session_id, timestamp, voltage, current, power, protocol)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sid, ts, v, i, v * i, "PD"))
        history._conn.commit()

    def test_end_session_recomputes_from_points(self, history):
        """有采样点时, avg 用采样点的时间加权均值, 而不是传入的瞬时 0 值。"""
        sid = history.start_session(1, protocol="PD")
        t = time.time()
        for k in range(4):                       # 2A / 4A 交替, 权重相同
            self._point(history, sid, t + k * 10, 20.0, 2.0 if k % 2 == 0 else 4.0)
        history.end_session(sid, 1.0, 80.0, 0.0, 0.0, 600)

        row = history._conn.execute(
            "SELECT avg_voltage, avg_current FROM charge_sessions WHERE id = ?",
            (sid,)).fetchone()
        assert row["avg_voltage"] == pytest.approx(20.0, abs=0.1)
        assert row["avg_current"] == pytest.approx(3.0, abs=0.1)

    def test_end_session_falls_back_without_points(self, history):
        """没有采样点时保持旧行为（用传入值）。"""
        sid = history.start_session(1, protocol="PD")
        history.end_session(sid, 1.0, 50.0, 19.5, 2.5, 600)
        row = history._conn.execute(
            "SELECT avg_voltage, avg_current FROM charge_sessions WHERE id = ?",
            (sid,)).fetchone()
        assert row["avg_voltage"] == pytest.approx(19.5)
        assert row["avg_current"] == pytest.approx(2.5)

    def test_gap_longer_than_cap_excluded(self, history):
        """断档超过 30 分钟的区间不参与加权。"""
        sid = history.start_session(1, protocol="PD")
        t = time.time()
        self._point(history, sid, t, 20.0, 2.0)
        self._point(history, sid, t + 7200, 0.0, 0.0)   # 中间空 2 小时
        history.end_session(sid, 1.0, 40.0, 0.0, 0.0, 600)
        row = history._conn.execute(
            "SELECT avg_voltage FROM charge_sessions WHERE id = ?", (sid,)).fetchone()
        # 超限区间被跳过 → 退化为算术平均 (20 + 0) / 2 = 10
        assert row["avg_voltage"] == pytest.approx(10.0, abs=0.1)

    def test_backfill_fixes_historical_zero_rows(self, history):
        """启动回填: avg=0 但有采样点的旧会话被重算。"""
        sid = history.start_session(1, protocol="PD")
        t = time.time()
        for k in range(2):
            self._point(history, sid, t + k * 10, 15.0, 1.0)
        history.end_session(sid, 1.0, 15.0, 0.0, 0.0, 600)
        # 改回"旧版本"的脏数据
        history._conn.execute(
            "UPDATE charge_sessions SET avg_voltage = 0, avg_current = 0 WHERE id = ?",
            (sid,))
        history._conn.commit()

        fixed = history.backfill_session_avg_vi()
        assert fixed >= 1
        row = history._conn.execute(
            "SELECT avg_voltage, avg_current FROM charge_sessions WHERE id = ?",
            (sid,)).fetchone()
        assert row["avg_voltage"] == pytest.approx(15.0, abs=0.1)
        assert row["avg_current"] == pytest.approx(1.0, abs=0.1)


class TestEnergyGapCap:
    """能量积分断档上限: 闲置空档不得被积分成能量。"""

    def test_energy_integration_caps_long_gaps(self, history):
        """相邻采样点间隔远超 30s 时, 按 30s 封顶积分。"""
        now = time.time()
        history._conn.execute(
            """INSERT INTO port_history (port, timestamp, voltage, current, power, active)
               VALUES (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?)""",
            (1, now, 20.0, 2.0, 40.0, 1,
             1, now + 3600, 20.0, 2.0, 40.0, 1))
        history._conn.commit()
        stats = history.get_statistics(1, hours=24)
        # 不封顶会积出 40W × 1h = 40Wh; 封顶后 ≈ 40W × 30s ≈ 0.33Wh
        assert stats.get("energy_wh", 0) < 1.0, \
            f"断档未被封顶: {stats.get('energy_wh')}"


class TestWriteAmplification:
    """写放大优化：charge_points 批量提交 + WAL 按阈值 checkpoint。"""

    def _count_points(self, history, sid):
        return history._conn.execute(
            "SELECT COUNT(*) FROM charge_points WHERE session_id = ?", (sid,)
        ).fetchone()[0]

    def test_charge_point_is_buffered_until_flush(self, history):
        """采样点先入缓冲，不逐点提交（原先每点一次 INSERT+COMMIT）。"""
        sid = history.start_session(1, protocol="PD")
        history.record_charge_point(sid, 20.0, 1.0, 20.0, "PD")
        assert self._count_points(history, sid) == 0, "不应立即落盘"
        history.flush()
        assert self._count_points(history, sid) == 1

    def test_get_session_points_flushes_buffer(self, history):
        """读取路径要先把缓冲落盘，否则详情图缺最近几秒。"""
        sid = history.start_session(1, protocol="PD")
        history.record_charge_point(sid, 20.0, 1.0, 20.0, "PD")
        pts = history.get_session_points(sid)
        assert len(pts) == 1
        assert pts[0]["voltage"] == pytest.approx(20.0)

    def test_compute_avg_sees_buffered_point(self, history):
        """均压/均流重算前会 flush，缓冲中的点不能被漏掉。"""
        sid = history.start_session(1, protocol="PD")
        history.record_charge_point(sid, 19.5, 2.0, 39.0, "PD")
        avg_v, avg_i = history.compute_session_avg_vi(sid)
        assert avg_v == pytest.approx(19.5, abs=0.01)
        assert avg_i == pytest.approx(2.0, abs=0.01)

    def test_end_session_flushes_then_computes(self, history):
        """会话闭合时缓冲点必须先落盘再重算均值（否则均值偏低）。"""
        sid = history.start_session(1, protocol="PD")
        history.record_charge_point(sid, 20.0, 1.5, 30.0, "PD")
        history.end_session(sid, 1.0, 30.0, 0.0, 0.0, 600)
        row = history._conn.execute(
            "SELECT avg_voltage, avg_current FROM charge_sessions WHERE id = ?", (sid,)
        ).fetchone()
        assert row["avg_voltage"] == pytest.approx(20.0, abs=0.01)
        assert row["avg_current"] == pytest.approx(1.5, abs=0.01)

    def test_wal_checkpoint_skipped_when_wal_small(self, history):
        """WAL 未达阈值时不做 checkpoint（避免无谓的脏页回写）。"""
        assert history._checkpoint_wal() is False

    def test_batch_interval_relaxed(self, history):
        """提交间隔不得退回 1s —— 那是"每秒一次 commit"的根源。"""
        from history import PortHistory
        assert PortHistory.BATCH_INTERVAL >= 5.0

    def test_checkpoint_threshold_is_reasonable(self):
        from history import PortHistory
        assert PortHistory.WAL_CHECKPOINT_MIN_BYTES >= 1024 * 1024


class TestPortModeMeta:
    """端口模式（长期供电 / 充满即停）的 meta 读写。"""

    def test_defaults_empty(self, history):
        assert history.get_permanent_ports() == []
        assert history.get_full_off() == {}

    def test_round_trip_and_normalization(self, history):
        """大小写归一、去重、未知端口丢弃，输出顺序跟 PORT_NAMES（c1/c2/c3/a）。"""
        history.set_permanent_ports(["C2", "c2", "a", "bogus", None, 3])
        assert history.get_permanent_ports() == ["c2", "a"]
        history.set_full_off({"c3": "always", "c1": "once", "bogus": "once"})
        assert history.get_full_off() == {"c1": "once", "c3": "always"}, "非法端口丢弃，模式保留"

    def test_garbage_input_becomes_empty(self, history):
        history.set_permanent_ports("c1")
        assert history.get_permanent_ports() == ["c1"], "单个字符串按一个端口处理"
        history.set_full_off(["c1"])
        assert history.get_full_off() == {"c1": "always"}, "列表形式按 always 处理"
        history.set_full_off({"c1": "sometimes"})
        assert history.get_full_off() == {"c1": "once"}, "非法模式回落默认（once）"

    def test_corrupt_meta_falls_back_to_empty(self, history):
        from history import PortHistory
        history.set_meta(PortHistory.PERMANENT_PORTS_META_KEY, "{not json")
        assert history.get_permanent_ports() == []
        history.set_meta(PortHistory.PERMANENT_PORTS_META_KEY, '{"c1": true}')
        assert history.get_permanent_ports() == []

    def test_legacy_array_meta_upgrades_to_always(self, history):
        """旧版本把"充满即停"存成数组（没有模式），读到要升级成 always。"""
        from history import PortHistory
        history.set_meta(PortHistory.FULL_OFF_PORTS_META_KEY, '["c2"]')
        assert history.get_full_off() == {"c2": "always"}

    def test_persists_across_reconnect(self, temp_db):
        from history import PortHistory
        h1 = PortHistory(db_path=temp_db, retention_days=2)
        h1.connect()
        h1.set_permanent_ports(["c2"])
        h1.set_full_off({"c1": "always"})
        h1.close()

        h2 = PortHistory(db_path=temp_db, retention_days=2)
        h2.connect()
        try:
            assert h2.get_permanent_ports() == ["c2"]
            assert h2.get_full_off() == {"c1": "always"}
        finally:
            h2.close()
