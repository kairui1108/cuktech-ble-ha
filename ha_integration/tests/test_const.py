"""Tests for HA Integration constants and select options."""
import sys
import json
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# Add the integration module to path
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "cuktech_charger"))

from const import PORT_MAP, PIID_DISPLAY, SELECT_PIIDS, SELECT_OPTION_MAP


class TestPortMap:
    """Test port mapping constants."""

    def test_port_map_values(self):
        """Test PORT_MAP has correct values."""
        assert PORT_MAP == {"c1": 1, "c2": 2, "c3": 3, "a": 4}

    def test_port_map_keys(self):
        """Test PORT_MAP has correct keys."""
        assert set(PORT_MAP.keys()) == {"c1", "c2", "c3", "a"}


class TestPIIDDisplay:
    """Test PIID display values."""

    def test_scene_mode_options(self):
        """Test scene mode display values."""
        display = PIID_DISPLAY[5]
        assert display[1] == "AI模式"
        assert display[2] == "数码生态"
        assert display[3] == "单口模式"
        assert display[4] == "均衡模式"

    def test_screen_save_time_options(self):
        """Test screen save time display values."""
        display = PIID_DISPLAY[6]
        assert display[0] == "5分钟"
        assert display[1] == "1分钟"
        assert display[2] == "10分钟"
        assert display[3] == "30分钟"
        assert display[4] == "常亮"

    def test_language_options(self):
        """Test language display values."""
        display = PIID_DISPLAY[13]
        assert display[0] == "English"
        assert display[1] == "中文"


class TestSelectOptionMap:
    """Test SELECT_OPTION_MAP consistency."""

    def test_scene_mode_map(self):
        """Test scene mode option mapping."""
        assert SELECT_OPTION_MAP[5]["AI模式"] == 1
        assert SELECT_OPTION_MAP[5]["数码生态"] == 2
        assert SELECT_OPTION_MAP[5]["单口模式"] == 3
        assert SELECT_OPTION_MAP[5]["均衡模式"] == 4

    def test_screen_save_time_map(self):
        """Test screen save time option mapping."""
        assert SELECT_OPTION_MAP[6]["5分钟"] == 0
        assert SELECT_OPTION_MAP[6]["1分钟"] == 1
        assert SELECT_OPTION_MAP[6]["10分钟"] == 2
        assert SELECT_OPTION_MAP[6]["30分钟"] == 3
        assert SELECT_OPTION_MAP[6]["常亮"] == 4

    def test_language_map(self):
        """Test language option mapping."""
        assert SELECT_OPTION_MAP[13]["English"] == 0
        assert SELECT_OPTION_MAP[13]["中文"] == 1


class TestSelectSync:
    """Test SELECT_OPTION_MAP is derived from SELECT_PIIDS."""

    def test_options_match(self):
        """Test that SELECT_OPTION_MAP matches SELECT_PIIDS options."""
        for piid, cfg in SELECT_PIIDS.items():
            assert piid in SELECT_OPTION_MAP
            for option in cfg["options"]:
                assert option in SELECT_OPTION_MAP[piid], f"Option {option} not in map for PIID {piid}"
