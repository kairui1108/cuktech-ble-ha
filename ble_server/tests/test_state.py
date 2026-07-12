"""Tests for state.py - Protocol decoding and state management."""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from state import decode_port, decode_pdo_caps, ChargerState, PortState, PORT_NAMES


class TestDecodePort:
    """Test port data decoding."""

    def test_idle_port(self):
        """Test decoding an idle port."""
        # Last 4 bytes: in_use=0, protocol=0, current=0, voltage=0
        pt = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
        result = decode_port(1, pt)
        assert result is not None
        assert result["voltage"] == 0.0
        assert result["current"] == 0.0
        assert result["power"] == 0.0
        assert result["active"] is False
        assert result["protocol"] == "idle"

    def test_active_pd_port(self):
        """Test decoding an active PD port."""
        # Last 4 bytes: in_use=1, protocol=0x0a (PD), current=25 (2.5A), voltage=201 (20.1V)
        pt = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x0a, 25, 201])
        result = decode_port(1, pt)
        assert result is not None
        assert result["voltage"] == 20.1
        assert result["current"] == 2.5
        assert result["power"] == 50.2
        assert result["active"] is True
        # V2: 0x0A + 20.1V close to 20V PD Fixed
        assert "PD" in result["protocol"]

    def test_active_qc_port(self):
        """Test decoding an active QC port (C3 supports QC)."""
        # C3 port, code=0x70 (QC), current=15 (1.5A), voltage=90 (9.0V)
        pt = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x70, 15, 90])
        result = decode_port(3, pt)
        assert result is not None
        assert result["voltage"] == 9.0
        assert result["current"] == 1.5
        assert result["protocol"] == "QC"

    def test_c1c2_code_70_pd_voltage(self):
        """C1/C2 code=0x70 at PD standard voltage should be detected as PD."""
        # C2 port, code=0x70, 9.0V → matches PD fixed voltage → PD
        pt = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x70, 15, 90])
        result = decode_port(2, pt)
        assert result is not None
        assert result["protocol"] == "PD"

    def test_active_usba_port(self):
        """Test decoding an active USB-A port."""
        # Last 4 bytes: in_use=1, protocol=0x60 (USB-A), current=10 (1.0A), voltage=50 (5.0V)
        pt = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x60, 10, 50])
        result = decode_port(4, pt)
        assert result is not None
        assert result["voltage"] == 5.0
        assert result["current"] == 1.0
        assert result["protocol"] == "5V"

    def test_short_data(self):
        """Test decoding with insufficient data."""
        pt = bytes([1, 0, 0])
        result = decode_port(1, pt)
        assert result is None

    def test_power_calculation(self):
        """Test power is correctly calculated as voltage * current."""
        # Last 4 bytes: in_use=1, protocol=0x0a, current=30 (3.0A), voltage=120 (12.0V)
        pt = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x0a, 30, 120])
        result = decode_port(1, pt)
        assert result["voltage"] == 12.0
        assert result["current"] == 3.0
        assert result["power"] == 36.0


class TestDecodePdoCaps:
    """Test PDO capability decoding."""

    def test_basic_decode(self):
        """Test basic PDO capability decoding."""
        # value = 0x08010701: low_half=0x0701 (PD Fixed), high_half=0x0801 (PD PPS)
        # Function signature: decode_pdo_caps(value, high_port, low_port)
        # high_port="c1" gets high_half (PD PPS), low_port="c2" gets low_half (PD Fixed)
        value = 0x08010701
        result = decode_pdo_caps(value, "c1", "c2")
        assert result["c1"]["kind"] == "PD PPS"  # high_port gets high_half
        assert result["c2"]["kind"] == "PD Fixed"  # low_port gets low_half

    def test_empty_caps(self):
        """Test decoding with empty capabilities."""
        value = 0x00000000
        result = decode_pdo_caps(value, "c1", "c2")
        assert result["c1"]["kind"] is None
        assert result["c2"]["kind"] is None


class TestChargerState:
    """Test ChargerState management."""

    def test_initial_state(self):
        """Test initial state values."""
        state = ChargerState()
        assert state.connected is False
        assert state.authenticated is False
        assert len(state.ports) == 4
        assert state.settings == {}

    def test_port_update(self):
        """Test port data update."""
        state = ChargerState()
        data = {"voltage": 20.0, "current": 2.0, "power": 40.0, "active": True, "protocol": "PD"}

        async def update():
            await state.update_port(1, data)

        asyncio.run(update())
        assert state.ports[1].voltage == 20.0
        assert state.ports[1].protocol == "PD"

    def test_settings_update(self):
        """Test settings update."""
        state = ChargerState()
        settings = {"5": 1, "6": 0}

        async def update():
            await state.update_settings(settings)

        asyncio.run(update())
        assert state.settings == {"5": 1, "6": 0}

    def test_to_dict(self):
        """Test to_dict serialization."""
        state = ChargerState()

        async def test():
            await state.update_port(1, {"voltage": 12.0, "current": 1.0, "power": 12.0, "active": True, "protocol": "PD"})
            return await state.to_dict()

        result = asyncio.run(test())
        assert result["connected"] is False
        # Port keys in to_dict are integers
        assert result["ports"][1]["voltage"] == 12.0
        assert result["ports"][1]["protocol"] == "PD"
