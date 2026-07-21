"""Tests for bemfa_client.py - Bemfa cloud MQTT client."""
import asyncio
import json
import pytest
import sys
import os
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bemfa_client import (
    BemfaDevice, BemfaClient, MSG_ON, MSG_OFF,
    TOPIC_PREFIX, TOPIC_PUBLISH, TOPIC_PING, MQTT_HOST, MQTT_PORT,
)


class TestBemfaDevice:
    """Test BemfaDevice topic computation."""

    def test_topic_format(self):
        """Topic is 'hass' + md5(entity_id) + '006'."""
        dev = BemfaDevice("charger_c1", "C1 Port")
        assert dev.topic.startswith(TOPIC_PREFIX)
        assert dev.topic.endswith("006")
        # md5('charger_c1') is deterministic
        assert len(dev.topic) == len(TOPIC_PREFIX) + 32 + 3  # prefix + md5 + "006"

    def test_pub_topic(self):
        """Pub topic is topic + '/set'."""
        dev = BemfaDevice("test_device", "Test")
        expected_pub = TOPIC_PUBLISH.format(topic=dev.topic)
        assert dev.pub_topic == expected_pub

    def test_topic_cache(self):
        """Topic is cached after first computation."""
        dev = BemfaDevice("test", "Test")
        topic1 = dev.topic
        topic2 = dev.topic
        assert topic1 == topic2

    def test_pub_topic_cache(self):
        """Pub topic is cached after first computation."""
        dev = BemfaDevice("test", "Test")
        pt1 = dev.pub_topic
        pt2 = dev.pub_topic
        assert pt1 == pt2


class TestBemfaClient:
    """Test BemfaClient MQTT and HTTP operations."""

    @pytest.fixture
    def client(self):
        """Create a BemfaClient instance."""
        c = BemfaClient("test_uid_12345")
        c.add_device("charger_c1", "C1 Port")
        c._client = MagicMock()  # Mock MQTT client
        return c

    @pytest.fixture
    def callback(self):
        """A command callback that returns True."""
        cb = MagicMock(return_value=True)
        return cb

    def test_add_device(self, client):
        """add_device stores and returns a BemfaDevice."""
        dev = client.add_device("charger_c2", "C2 Port")
        assert isinstance(dev, BemfaDevice)
        assert dev.entity_id == "charger_c2"
        assert "charger_c2" in client._devices

    def test_on_command_registration(self, client):
        """on_command stores the callback."""
        cb = lambda on: True
        client.on_command("charger_c1", cb)
        assert client._command_callbacks["charger_c1"] is cb

    def test_is_connected_default(self, client):
        """Initially not connected."""
        assert client.is_connected is False

    def test_on_connect_success(self, client):
        """on_connect with rc=0 sets connected and subscribes."""
        mock_mqtt = MagicMock()
        client._on_connect(mock_mqtt, None, None, 0, None)
        assert client.is_connected is True
        # Should subscribe to device topics + ping topic
        assert mock_mqtt.subscribe.call_count >= 1
        # Should publish initial state
        assert mock_mqtt.publish.call_count >= 1

    def test_on_connect_failure(self, client):
        """on_connect with rc!=0 does not set connected."""
        client._on_connect(None, None, None, 1, None)
        assert client.is_connected is False

    def test_on_disconnect(self, client):
        """on_disconnect clears connected flag."""
        client._on_connect(MagicMock(), None, None, 0, None)
        assert client.is_connected is True
        client._on_disconnect(None, None, None, 0, None)
        assert client.is_connected is False

    def test_publish_state_connected(self, client):
        """publish_state sends MQTT message when connected."""
        client._on_connect(MagicMock(), None, None, 0, None)
        client.publish_state("charger_c1", MSG_ON)
        dev = client._devices["charger_c1"]
        client._client.publish.assert_called_with(
            dev.pub_topic, MSG_ON, qos=1, retain=True)

    def test_publish_state_not_connected(self, client):
        """publish_state does nothing when not connected."""
        client.publish_state("charger_c1", MSG_ON)
        client._client.publish.assert_not_called()

    def test_publish_state_unknown_device(self, client):
        """publish_state for unknown device is silently ignored."""
        mock_mqtt = MagicMock()
        client._on_connect(mock_mqtt, None, None, 0, None)
        client.publish_state("unknown_device", MSG_ON)
        # publish_state calls self._client.publish, not mock_mqtt
        client._client.publish.assert_not_called()

    def test_on_connect_publishes_initial_states(self, client):
        """_on_connect publishes initial state for registered devices."""
        mock_mqtt = MagicMock()
        client._on_connect(mock_mqtt, None, None, 0, None)
        dev = client._devices["charger_c1"]
        mock_mqtt.publish.assert_called_with(
            dev.pub_topic, MSG_OFF, qos=1, retain=True)

    def test_on_message_ping(self, client):
        """Ping message resets ping_lost."""
        client._ping_lost = 2
        client._ping_receive_task = asyncio.Future()
        mock_msg = MagicMock()
        mock_msg.topic = TOPIC_PING
        client._on_message(None, None, mock_msg)
        assert client._ping_lost == 0

    def test_on_message_device_on(self, client, callback):
        """Device 'on' command invokes callback with True."""
        client.on_command("charger_c1", callback)
        client._connect_time = 0  # bypass grace period
        dev = client._devices["charger_c1"]
        mock_msg = MagicMock()
        mock_msg.topic = dev.topic
        mock_msg.payload = b"on"
        client._on_message(None, None, mock_msg)
        callback.assert_called_once_with(True)

    def test_on_message_device_off(self, client, callback):
        """Device 'off' command invokes callback with False."""
        client.on_command("charger_c1", callback)
        client._connect_time = 0
        dev = client._devices["charger_c1"]
        mock_msg = MagicMock()
        mock_msg.topic = dev.topic
        mock_msg.payload = b"off"
        client._on_message(None, None, mock_msg)
        callback.assert_called_once_with(False)

    def test_on_message_echo_grace_period(self, client, callback):
        """Messages within 10s grace period are ignored."""
        client.on_command("charger_c1", callback)
        client._connect_time = 9999999999  # "now" - 10s would be too close
        import time
        client._connect_time = time.time()  # connected just now
        dev = client._devices["charger_c1"]
        mock_msg = MagicMock()
        mock_msg.topic = dev.topic
        mock_msg.payload = b"on"
        client._on_message(None, None, mock_msg)
        callback.assert_not_called()

    def test_on_message_callback_returns_false(self, client):
        """When callback returns False, state cache is NOT updated."""
        cb = MagicMock(return_value=False)
        client.on_command("charger_c1", cb)
        client._connect_time = 0
        dev = client._devices["charger_c1"]
        mock_msg = MagicMock()
        mock_msg.topic = dev.topic
        mock_msg.payload = b"on"
        client._on_message(None, None, mock_msg)
        # State should NOT be cached since callback returned False
        assert client._state_cache.get(dev.topic) is None

    def test_on_message_no_callback(self, client):
        """Message for device without callback logs warning, no error."""
        client._connect_time = 0
        dev = client._devices["charger_c1"]
        mock_msg = MagicMock()
        mock_msg.topic = dev.topic
        mock_msg.payload = b"on"
        # Should not raise
        client._on_message(None, None, mock_msg)

    def test_stop_cancels_ping_tasks(self, client):
        """stop() cancels ping tasks and disconnects MQTT."""
        client._ping_publish_task = asyncio.Future()
        client._ping_receive_task = asyncio.Future()
        asyncio.run(client.stop())
        assert client._ping_publish_task is None
        assert client._ping_receive_task is None
        assert client.is_connected is False

    def test_stop_no_client(self):
        """stop() with no MQTT client works cleanly."""
        c = BemfaClient("uid")
        asyncio.run(c.stop())  # No error expected

    @pytest.mark.asyncio
    async def test_start_http_register_failure(self):
        """start() handles HTTP registration failure gracefully."""
        c = BemfaClient("test_uid")
        c.add_device("charger_c1", "C1")
        with patch("aiohttp.ClientSession.post") as mock_post:
            mock_resp = AsyncMock()
            mock_resp.status = 500
            mock_post.return_value.__aenter__.return_value = mock_resp
            # Mock MQTT connect to avoid real connection
            with patch.object(c, "_connect_mqtt"):
                await c.start()
                mock_post.assert_called_once()
        await c.stop()

    def test_state_cache_on_successful_command(self, client, callback):
        """State cache is updated when callback returns True."""
        client.on_command("charger_c1", callback)
        client._connect_time = 0
        dev = client._devices["charger_c1"]
        mock_msg = MagicMock()
        mock_msg.topic = dev.topic
        mock_msg.payload = b"on"
        client._on_message(None, None, mock_msg)
        assert client._state_cache[dev.topic] == MSG_ON

    def test_mqtt_client_creation(self):
        """_connect_mqtt creates a paho MQTT client."""
        c = BemfaClient("uid123")
        with patch("bemfa_client.mqtt.Client") as mock_cls, \
             patch("bemfa_client.mqtt.CallbackAPIVersion", create=True) as mock_api:
            mock_api.VERSION2 = "VERSION2"
            mock_cls.return_value = MagicMock()
            c._connect_mqtt()
            mock_cls.assert_called_once()
            # Should set callbacks
            mock_client = mock_cls.return_value
            assert mock_client.on_connect == c._on_connect
            assert mock_client.on_disconnect == c._on_disconnect
            assert mock_client.on_message == c._on_message
            mock_client.connect.assert_called_once_with(MQTT_HOST, MQTT_PORT, 600)
