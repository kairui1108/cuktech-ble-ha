"""Shared fixtures for CUKTECH BLE Server tests."""
import asyncio
import os
import sqlite3
import tempfile
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# ha_server 在 **导入时** 就会按 CUKTECH_LOG_FILE 建立轮转文件处理器；测试期间
# 必须指向临时文件，否则会把测试日志写进生产日志（/tmp/cuktech_server.log）。
# 这行必须在任何 `import ha_server` 之前执行。
os.environ["CUKTECH_LOG_FILE"] = os.path.join(
    tempfile.gettempdir(), "cuktech_server_test.log")

from history import PortHistory


@pytest.fixture(autouse=True)
def mock_subprocess():
    """Prevent any test from calling real subprocess (hciconfig, bluetoothctl, etc.)."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    with patch("asyncio.create_subprocess_exec", return_value=AsyncMock(return_value=mock_proc)):
        yield


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def history(temp_db):
    """Create a PortHistory instance with temporary database."""
    h = PortHistory(db_path=temp_db, retention_days=2)
    h.connect()
    yield h
    h.close()


@pytest.fixture
def mock_ble_data():
    """Sample BLE port data."""
    return {
        "voltage": 20.1,
        "current": 2.5,
        "power": 50.25,
        "active": True,
        "protocol": "PD",
    }
