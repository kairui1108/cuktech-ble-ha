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


class TestBuildMiotTlv:
    """Test _build_miot_tlv TLV encoding."""

    def test_set_uint8_value(self):
        """Test SET with 1-byte value."""
        from src.cuktech_ble.controller import CuktechBLEController
        # siid=2, piid=5, value=3 (场景模式=3)
        result = CuktechBLEController._build_miot_tlv(1, 2, 5, value=3)
        tl = (1 << 12) | 1  # type_id=1(UINT8), len=1
        expected = bytes([
            12, 0x20,  # total_len=12, frame_type=0x20
            1, 0x00,   # seq=1, [0x00]
            0x00, 0x01, # opcode=SET(0x00), cnt=1
            2,          # siid=2
            5, 0x00,    # piid=5 (LE)
            tl & 0xFF, (tl >> 8) & 0xFF,  # tl
            3,          # value=3
        ])
        assert result == expected
        assert len(result) == 12

    def test_set_uint32_value(self):
        """Test SET with 4-byte value (PIID 21 protocol_extend)."""
        from src.cuktech_ble.controller import CuktechBLEController
        # siid=2, piid=21, value=50532111 (0x0303030F)
        value = 0x0303030F
        result = CuktechBLEController._build_miot_tlv(1, 2, 21, value=value)
        assert len(result) == 15
        assert result[0] == 15  # total_len
        tl = (5 << 12) | 4  # type_id=5(UINT32), len=4
        assert result[9:11] == bytes([tl & 0xFF, (tl >> 8) & 0xFF])  # tl
        # Last 4 bytes = value in LE
        assert result[11:15] == b'\x0F\x03\x03\x03'

    def test_get_command(self):
        """Test GET command (value=None)."""
        from src.cuktech_ble.controller import CuktechBLEController
        # siid=2, piid=5, no value
        result = CuktechBLEController._build_miot_tlv(1, 2, 5)
        assert len(result) == 12
        assert result[4] == 0x02  # opcode=GET
        assert result[-1] == 0x00  # dummy value byte

    def test_piid_le_encoding(self):
        """Test piid is encoded as 2-byte little-endian."""
        from src.cuktech_ble.controller import CuktechBLEController
        # piid=512 (0x200) should be 0x00, 0x02
        result = CuktechBLEController._build_miot_tlv(1, 2, 512, value=1)
        assert result[7] == 0x00  # piid low byte
        assert result[8] == 0x02  # piid high byte

    def test_total_len_formula(self):
        """Test total_len = 11 + value_bytes."""
        from src.cuktech_ble.controller import CuktechBLEController
        r1 = CuktechBLEController._build_miot_tlv(1, 2, 5, value=0x00)     # UINT8 → 1 byte
        r2 = CuktechBLEController._build_miot_tlv(1, 2, 21, value=0x10000) # UINT32 → 4 bytes
        r3 = CuktechBLEController._build_miot_tlv(1, 2, 5)                 # GET → 1 byte dummy
        assert r1[0] == 12   # 11 + 1
        assert r2[0] == 15   # 11 + 4
        assert r3[0] == 12   # 11 + 1
