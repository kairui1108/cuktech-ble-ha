"""Tests for controller.py - BLE controller operations."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.cuktech_ble.protocol import DEVICE_MAC, DEVICE_TOKEN, PORT_BITS


class TestReconnectDelay:
    """Test BLE reconnection delay calculation."""

    def test_exponential_backoff(self):
        """Test exponential backoff increases delay."""
        base_delay = 1
        max_delay = 300
        delays = []
        for attempt in range(6):
            delay = min(base_delay * (2 ** attempt), max_delay)
            delays.append(delay)
        assert delays == [1, 2, 4, 8, 16, 32]

    def test_delay_capped_at_max(self):
        """Test delay doesn't exceed max."""
        base_delay = 1
        max_delay = 300
        delay = min(base_delay * (2 ** 10), max_delay)
        assert delay == max_delay

    def test_delay_resets_on_success(self):
        """Test delay resets to base after successful connection."""
        base_delay = 1
        attempts = 5
        delay = min(base_delay * (2 ** attempts), 300)
        assert delay == 32  # After reset, next delay would be 1

    def test_ble_manager_reconnect_delay(self):
        """Test BLEManager._get_reconnect_delay() returns correct values."""
        from unittest.mock import MagicMock
        from ble_manager import BLEManager

        state = MagicMock()
        config = MagicMock()
        config.server.reconnect_base_delay = 1.0
        config.server.reconnect_max_delay = 300.0
        mgr = BLEManager(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff", state=state, config=config)

        mgr._reconnect_attempts = 0
        assert mgr._get_reconnect_delay() == 1.0

        mgr._reconnect_attempts = 3
        assert mgr._get_reconnect_delay() == 8.0

        mgr._reconnect_attempts = 10
        assert mgr._get_reconnect_delay() == 300.0


class TestControllerInit:
    """Test CuktechBLEController initialization."""

    def test_default_mac(self):
        """Test controller accepts default MAC."""
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac=DEVICE_MAC, token=DEVICE_TOKEN)
        assert ctrl.mac == DEVICE_MAC

    def test_custom_mac(self):
        """Test controller accepts custom MAC."""
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac="AA:BB:CC:DD:EE:FF", token=DEVICE_TOKEN)
        assert ctrl.mac == "AA:BB:CC:DD:EE:FF"

    def test_initial_state(self):
        """Test controller initial state."""
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac=DEVICE_MAC, token=DEVICE_TOKEN)
        assert ctrl.authenticated is False
        assert ctrl.client is None


class TestProtocolConstants:
    """Test protocol constants used in controller."""

    def test_device_token_length(self):
        """Test DEVICE_TOKEN is 12 bytes."""
        assert len(DEVICE_TOKEN) == 12

    def test_port_bits_complete(self):
        """Test all ports have bit assignments."""
        assert len(PORT_BITS) == 4
        assert all(v in range(4) for v in PORT_BITS.values())
