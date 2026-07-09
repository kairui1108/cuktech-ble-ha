"""Tests for protocol.py - BLE protocol constants and utilities."""
import sys
import os
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.cuktech_ble.protocol import (
    mac_str_to_bytes, require_runtime_dependencies,
    DEVICE_MAC, DEVICE_TOKEN, PRODUCT_ID,
    PROTOCOL_NAMES, PD_FIXED_VOLTAGES, PDO_KIND_BY_HIGH_BYTE,
    PORT_BITS, PIID_NAMES, PIID_DISPLAY,
)


class TestMacStrToBytes:
    """Test MAC address conversion."""

    def test_normal_mac(self):
        """Test normal MAC address conversion."""
        result = mac_str_to_bytes("AA:BB:CC:DD:EE:FF")
        assert result == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])

    def test_dashes_mac(self):
        """Test MAC address with dashes."""
        result = mac_str_to_bytes("AA-BB-CC-DD-EE-FF")
        assert result == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])

    def test_lowercase_mac(self):
        """Test lowercase MAC address."""
        result = mac_str_to_bytes("aa:bb:cc:dd:ee:ff")
        assert result == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])


class TestRequireRuntimeDependencies:
    """Test runtime dependency checking."""

    def test_no_exception_when_deps_installed(self):
        """Test no exception when bleak and cryptography are installed."""
        try:
            require_runtime_dependencies()
        except RuntimeError:
            pytest.fail("Should not raise when dependencies are installed")

    def test_raises_when_bleak_missing(self):
        """Test exception when bleak is missing."""
        import unittest.mock
        with unittest.mock.patch.dict('sys.modules', {'bleak': None}):
            with pytest.raises(RuntimeError, match="bleak"):
                require_runtime_dependencies()


class TestEnvHexBytes:
    """Test environment variable hex byte parsing."""

    def test_valid_hex(self):
        """Test valid hex string is parsed correctly."""
        from src.cuktech_ble.protocol import _env_hex_bytes
        from unittest.mock import patch
        with patch.dict('os.environ', {'TEST_VAR': 'aabbccdd'}):
            result = _env_hex_bytes('TEST_VAR', '00000000', 4)
            assert result == bytes([0xaa, 0xbb, 0xcc, 0xdd])

    def test_empty_uses_default(self):
        """Test empty env var uses default."""
        from src.cuktech_ble.protocol import _env_hex_bytes
        from unittest.mock import patch
        with patch.dict('os.environ', {'TEST_VAR': ''}, clear=False):
            os.environ.pop('TEST_VAR', None)
            result = _env_hex_bytes('TEST_VAR', '00112233', 4)
            assert result == bytes([0x00, 0x11, 0x22, 0x33])

    def test_invalid_hex_raises(self):
        """Test invalid hex string raises ValueError."""
        from src.cuktech_ble.protocol import _env_hex_bytes
        from unittest.mock import patch
        with patch.dict('os.environ', {'TEST_VAR': 'xyz'}):
            with pytest.raises(ValueError, match="hex string"):
                _env_hex_bytes('TEST_VAR', '00000000', 4)

    def test_invalid_length_raises(self):
        """Test wrong length raises ValueError."""
        from src.cuktech_ble.protocol import _env_hex_bytes
        from unittest.mock import patch
        with patch.dict('os.environ', {'TEST_VAR': 'aabb'}):
            with pytest.raises(ValueError, match="bytes"):
                _env_hex_bytes('TEST_VAR', '00000000', 4)

    def test_odd_length_hex_raises(self):
        """Test odd-length hex string raises ValueError."""
        from src.cuktech_ble.protocol import _env_hex_bytes
        from unittest.mock import patch
        with patch.dict('os.environ', {'TEST_VAR': 'aabbcc'}):
            with pytest.raises(ValueError):
                _env_hex_bytes('TEST_VAR', '00000000', 4)


class TestProtocolConstants:
    """Test protocol constants are correctly defined."""

    def test_device_mac_format(self):
        """Test DEVICE_MAC is a valid MAC address format."""
        assert ":" in DEVICE_MAC or DEVICE_MAC == "AA:BB:CC:DD:EE:FF"

    def test_protocol_names_coverage(self):
        """Test PROTOCOL_NAMES covers common protocols."""
        assert 0x0a in PROTOCOL_NAMES  # PD
        assert 0x70 in PROTOCOL_NAMES  # QC
        assert 0x60 in PROTOCOL_NAMES  # USB-A

    def test_port_bits(self):
        """Test PORT_BITS mapping."""
        assert PORT_BITS["c1"] == 0
        assert PORT_BITS["c2"] == 1
        assert PORT_BITS["c3"] == 2
        assert PORT_BITS["a"] == 3

    def test_pdo_kind_mapping(self):
        """Test PDO_KIND_BY_HIGH_BYTE mapping."""
        assert PDO_KIND_BY_HIGH_BYTE[0x07] == "PD Fixed"
        assert PDO_KIND_BY_HIGH_BYTE[0x08] == "PD PPS"
