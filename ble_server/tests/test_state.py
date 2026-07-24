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
        pt = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x70, 15, 90])
        result = decode_port(3, pt)
        assert result is not None
        assert result["voltage"] == 9.0
        assert result["current"] == 1.5
        assert result["protocol"] == "QC"

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

    def test_protocol_extend_default(self):
        """Test protocol_extend starts at 0 and decode yields all False."""
        state = ChargerState()
        assert state.protocol_extend == 0
        sw = state.protocol_switches
        assert sw["c1"]["pd"] is False
        assert sw["c1"]["pps"] is False
        assert sw["c1"]["ufcs"] is False
        assert sw["c2"]["pd"] is False
        assert sw["c2"]["pps"] is False
        assert sw["c2"]["ufcs"] is False
        assert sw["c3"]["scp"] is False
        assert sw["c3"]["ufcs"] is False
        assert sw["a"]["scp"] is False
        assert sw["a"]["ufcs"] is False

    def test_update_protocol_extend(self):
        """Test update_protocol_extend sets value and syncs to settings."""
        state = ChargerState()

        async def update():
            # c1 PD=1, PPS=1, UFCS=1, reserved=1 => 0x0F
            # c2 PD=1, PPS=1, UFCS=1, reserved=1 => 0x0F << 8 = 0x0F00
            # c3 UFCS=1 => 0x01 << 16 = 0x10000
            # a SCP=1 => 0x02 << 24 = 0x02000000
            # total = 0x02010F0F
            await state.update_protocol_extend(0x02010F0F)

        asyncio.run(update())
        assert state.protocol_extend == 0x02010F0F
        assert state.settings.get("21") == 0x02010F0F
        sw = state.protocol_switches
        assert sw["c1"]["pd"] is True
        assert sw["c1"]["pps"] is True
        assert sw["c1"]["ufcs"] is True
        assert sw["c2"]["pd"] is True
        assert sw["c2"]["pps"] is True
        assert sw["c2"]["ufcs"] is True
        assert sw["c3"]["scp"] is False  # bit 17 not set in this value
        assert sw["c3"]["ufcs"] is True  # bit 16 set
        assert sw["a"]["scp"] is True    # bit 25 set
        assert sw["a"]["ufcs"] is False  # bit 24 not set

    def test_encode_protocol_extend_all_on(self):
        """Test encoding: all protocols ON."""
        switches = {
            "c1": {"pd": True, "pps": True, "ufcs": True},
            "c2": {"pd": True, "pps": True, "ufcs": True},
            "c3": {"ufcs": True, "scp": True},
            "a":  {"ufcs": True, "scp": True},
        }
        # c1=0x0F, c2=0x0F<<8=0x0F00, c3=0x03<<16=0x30000, a=0x03<<24=0x3000000
        result = ChargerState.encode_protocol_extend(switches)
        assert result == 0x03030F0F

    def test_encode_protocol_extend_all_off(self):
        """Test encoding: all protocols OFF (c1/c2 still have reserved bit)."""
        switches = {
            "c1": {"pd": False, "pps": False, "ufcs": False},
            "c2": {"pd": False, "pps": False, "ufcs": False},
            "c3": {"ufcs": False, "scp": False},
            "a":  {"ufcs": False, "scp": False},
        }
        # c1=0x08, c2=0x08<<8=0x0800
        result = ChargerState.encode_protocol_extend(switches)
        assert result == 0x00000808  # only reserved bits set

    def test_protocol_switches_roundtrip(self):
        """Test decode(encode(switches)) == switches."""
        state = ChargerState()
        original = {
            "c1": {"pd": True, "pps": False, "ufcs": True},
            "c2": {"pd": False, "pps": True, "ufcs": False},
            "c3": {"ufcs": True, "scp": False},
            "a":  {"ufcs": False, "scp": True},
        }
        encoded = ChargerState.encode_protocol_extend(original)

        async def update():
            await state.update_protocol_extend(encoded)

        asyncio.run(update())
        decoded = state.protocol_switches
        for port in ["c1", "c2", "c3", "a"]:
            for proto in original[port]:
                assert decoded[port][proto] == original[port][proto], \
                    f"Mismatch for {port}.{proto}"

    def test_lock_property(self):
        """Test lock property returns the same lock instance."""
        state = ChargerState()
        assert state.lock is state._lock


class TestHwProtocol:
    """Test hardware protocol code extraction from PIID 17/18."""

    def test_hw_protocol_initial(self):
        """Initial hw_protocol should be None for all ports."""
        state = ChargerState()
        for piid in (1, 2, 3, 4):
            assert state.get_hw_protocol(piid) is None

    def test_set_hw_protocol_c1c2(self):
        """PIID 17 sets C1/C2 protocol codes."""
        state = ChargerState()
        state.set_hw_protocol_codes(9, 7)
        assert state.get_hw_protocol(1) == 9   # C1 = PPS
        assert state.get_hw_protocol(2) == 7   # C2 = PD
        assert state.get_hw_protocol(3) is None  # C3 untouched
        assert state.get_hw_protocol(4) is None  # A untouched

    def test_set_hw_protocol_c3a(self):
        """PIID 18 sets C3/A protocol codes."""
        state = ChargerState()
        state.set_hw_protocol_codes_c3a(7, 0)
        assert state.get_hw_protocol(3) == 7   # C3 = PD
        assert state.get_hw_protocol(4) is None  # A = 0 → None
        assert state.get_hw_protocol(1) is None  # C1 untouched

    def test_zero_protection_c1c2(self):
        """PIID 17 zero values should not overwrite existing protocol codes."""
        state = ChargerState()
        state.set_hw_protocol_codes(9, 7)  # set C1=PPS, C2=PD
        state.set_hw_protocol_codes(0, 7)  # C1=0 should be ignored
        assert state.get_hw_protocol(1) == 9  # C1 preserved
        assert state.get_hw_protocol(2) == 7  # C2 updated

    def test_zero_protection_c3a(self):
        """PIID 18 zero values should not overwrite."""
        state = ChargerState()
        state.set_hw_protocol_codes_c3a(7, 0)
        assert state.get_hw_protocol(3) == 7
        state.set_hw_protocol_codes_c3a(0, 3)
        assert state.get_hw_protocol(3) == 7  # preserved
        assert state.get_hw_protocol(4) == 3  # updated

    def test_piid17_byte_extraction(self):
        """Verify PIID 17 value parsing: byte[0]=C1, byte[2]=C2."""
        # raw=0x090F0764 → C1=9(PPS), C2=7(PD)
        val32 = 0x090F0764
        c1 = (val32 >> 24) & 0xFF  # byte[0] = 0x09 = 9
        c2 = (val32 >> 8) & 0xFF   # byte[2] = 0x07 = 7
        assert c1 == 9  # PPS
        assert c2 == 7  # PD

        # raw=0x07460937 → C1=7(PD), C2=9(PPS) (swapped devices)
        val32 = 0x07460937
        c1 = (val32 >> 24) & 0xFF
        c2 = (val32 >> 8) & 0xFF
        assert c1 == 7  # PD
        assert c2 == 9  # PPS

    def test_piid18_byte_extraction(self):
        """Verify PIID 18 value parsing: byte[0]=C3, byte[2]=A."""
        # raw=0x071E0000 → C3=7(PD), A=0(idle)
        val32 = 0x071E0000
        c3 = (val32 >> 24) & 0xFF
        a = (val32 >> 8) & 0xFF
        assert c3 == 7  # PD
        assert a == 0   # idle


class TestEstimateProtocolHw:
    """Test hw_protocol override in estimate_protocol_number."""

    def test_hw_protocol_overrides_heuristic(self):
        """When hw_protocol is provided, it should be used directly."""
        from state_protocol_v2 import estimate_protocol_number, RawPortData
        raw = RawPortData(in_use=True, code=0x04, current_raw=8, voltage_raw=200)
        result = estimate_protocol_number(2, raw, hw_protocol=4)
        assert result == 4  # AFC, not PD

    def test_hw_protocol_none_falls_back(self):
        """When hw_protocol is None, heuristic should run."""
        from state_protocol_v2 import estimate_protocol_number, RawPortData
        raw = RawPortData(in_use=True, code=0x07, current_raw=5, voltage_raw=200)
        result = estimate_protocol_number(2, raw, hw_protocol=None)
        assert result == 7  # PD via heuristic

    def test_decode_port_with_hw_protocol(self):
        """decode_port should pass hw_protocol through."""
        # Build a valid MiOT inline frame: [0c20][seq:2B][00][04][01][02][02][00][01][10][01][04][00][01][00]
        frame = bytes([0x0c, 0x20, 0x01, 0x00, 0x04, 0x01, 0x02, 0x02,
                        0x00, 0x01, 0x10, 0x01, 0x04, 0x00, 0x01, 0x00])
        # hw_protocol=8 (PPS) should override heuristic
        result = decode_port(2, frame, hw_protocol=8)
        assert result is not None
        assert result["protocol"] == "PPS"

    def test_decode_port_no_hw_protocol(self):
        """decode_port without hw_protocol uses heuristic."""
        # [in_use=1][code=0x07][current=5][voltage=200] → last 4 bytes
        frame = bytes([0x0c, 0x20, 0x01, 0x00, 0x04, 0x01, 0x02, 0x02,
                        0x01, 0x07, 0x05, 0xC8])
        result = decode_port(2, frame)
        assert result is not None
        # code=0x07 at 20V → heuristic gives PD
        assert result["protocol"] == "PD"
