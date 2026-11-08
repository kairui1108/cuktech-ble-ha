"""Tests for config.py - Configuration management."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import load_config, BLEConfig, MQTTConfig, ServerConfig, Config


class TestBLEConfig:
    """Test BLE configuration."""

    def test_valid_config(self):
        """Test valid BLE config."""
        config = BLEConfig(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff")
        assert config.mac == "AA:BB:CC:DD:EE:FF"
        assert config.token == "aabbccddeeff"

    @patch.dict(os.environ, {"CUKTECH_DEVICE_MAC": ""})
    def test_missing_mac_raises(self):
        """Test that missing MAC does not crash (graceful warning)."""
        cfg = BLEConfig(mac="", token="aabbccddeeff")
        assert cfg.mac == ""

    @patch.dict(os.environ, {"CUKTECH_DEVICE_MAC": "XX:XX:XX:XX:XX:XX"})
    def test_placeholder_mac_raises(self):
        """Test that placeholder MAC does not crash (graceful warning)."""
        cfg = BLEConfig(mac="XX:XX:XX:XX:XX:XX", token="aabbccddeeff")
        assert cfg.mac == "XX:XX:XX:XX:XX:XX"

    @patch.dict(os.environ, {"CUKTECH_DEVICE_TOKEN": ""})
    def test_missing_token_raises(self):
        """Test that missing token does not crash (graceful warning)."""
        cfg = BLEConfig(mac="AA:BB:CC:DD:EE:FF", token="")
        assert cfg.token == ""


class TestMQTTConfig:
    """Test MQTT configuration."""

    def test_default_values(self):
        """Test default MQTT config values."""
        config = MQTTConfig()
        assert config.host == "localhost"
        assert config.port == 1883
        assert config.username == ""
        assert config.password == ""
        assert config.keepalive == 60
        assert config.topic_prefix == "cuktech/charger"


class TestServerConfig:
    """Test Server configuration."""

    def test_default_values(self):
        """Test default server config values."""
        config = ServerConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 8199
        assert config.log_level == "info"
        assert config.history_retention_days == 2
        # 曲线（2 天）与会话记录（30 天）是两条独立的保留期；后者是热力图的
        # 回填来源，必须比曲线长。
        assert config.session_retention_days == 30
        assert config.reconnect_base_delay == 1.0
        assert config.reconnect_max_delay == 300.0


class TestConfig:
    """Test Config class."""

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_topic_properties(self):
        """Test topic property generation."""
        config = load_config()
        assert config.topic_port == "cuktech/charger/port"
        assert config.topic_settings == "cuktech/charger/settings"
        assert config.topic_status == "cuktech/charger/status"


class TestLoadConfig:
    """Test config loading."""

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_load_from_env(self):
        """Test loading config from environment variables."""
        config = load_config()
        assert config.ble.mac == "AA:BB:CC:DD:EE:FF"
        assert config.ble.token == "aabbccddeeff"

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
        "CUKTECH_LOG_LEVEL": "debug",
    })
    def test_log_level_from_env(self):
        """Test log level from environment variable."""
        config = load_config()
        assert config.server.log_level == "debug"

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
        "CUKTECH_HISTORY_RETENTION_DAYS": "7",
    })
    def test_retention_days_from_env(self):
        """Test history retention days from environment variable."""
        config = load_config()
        assert config.server.history_retention_days == 7

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
        "CUKTECH_SESSION_RETENTION_DAYS": "90",
    })
    def test_session_retention_days_from_env(self):
        """会话记录保留期与曲线保留期互不影响。"""
        config = load_config()
        assert config.server.session_retention_days == 90
        assert config.server.history_retention_days == 2

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
        "CUKTECH_SESSION_RETENTION_DAYS": "0",
    })
    def test_session_retention_days_rejects_zero(self):
        """0 会让清理把当天刚闭合并写入的记录也删掉。"""
        with pytest.raises(ValueError):
            load_config()

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
        "CUKTECH_SESSION_RETENTION_DAYS": "not-a-number",
    })
    def test_session_retention_days_rejects_garbage(self):
        with pytest.raises(ValueError):
            load_config()


class TestHistoryDbPath:
    """`history_db_path` 为空时必须回落默认文件。

    回归：`config.yaml.example` 里这一项就是 `""`，原生部署按 README `cp` 起步。
    以前空串被原样传给 `sqlite3.connect("")` —— 那是 SQLite 的**临时库**，重启即丢；
    `cuktech_ctl.sh clear-history` 也找不到文件（它按配置解析路径）。
    """

    def test_empty_falls_back_to_default(self, monkeypatch):
        from config import config_from_mapping
        monkeypatch.delenv("CUKTECH_HISTORY_DB_PATH", raising=False)
        cfg = config_from_mapping({"ble": {"mac": "", "token": ""},
                                   "server": {"history_db_path": ""}})
        assert cfg.server.history_db_path == "port_history.db"

    def test_explicit_path_is_kept(self, monkeypatch):
        from config import config_from_mapping
        monkeypatch.delenv("CUKTECH_HISTORY_DB_PATH", raising=False)
        cfg = config_from_mapping({"ble": {}, "server": {"history_db_path": "data/x.db"}})
        assert cfg.server.history_db_path == "data/x.db"

    def test_env_still_overrides(self, monkeypatch):
        from config import config_from_mapping
        monkeypatch.setenv("CUKTECH_HISTORY_DB_PATH", "/data/port_history.db")
        cfg = config_from_mapping({"ble": {}, "server": {"history_db_path": ""}})
        assert cfg.server.history_db_path == "/data/port_history.db"
