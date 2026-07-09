"""Shared fixtures for CUKTECH BLE Server tests."""
import asyncio
import sqlite3
import tempfile
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from history import PortHistory


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
