"""Bemfa cloud MQTT client + HTTP API.

Fully aligned with the Bemfa HA integration's Python logic:
- Topic format: "hass" + md5(entity_id) + "006" (switch)
- MQTT: subscribe "{topic}" for commands, publish "{topic}/set" for state
- HTTP: POST to api.bemfa.com for topic registration
- Keepalive: ping/pong every 30s (hassping topic), reconnect after 3 lost
"""
import asyncio
import hashlib
import logging
import threading
import time
from typing import Callable, Optional

import aiohttp
import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

# Bemfa constants (aligned with HA integration)
MQTT_HOST = "bemfa.com"
MQTT_PORT = 9501
MQTT_KEEPALIVE = 600

TOPIC_PREFIX = "hass"
TOPIC_PUBLISH = "{topic}/set"  # publish state here
TOPIC_PING = f"{TOPIC_PREFIX}ping"

INTERVAL_PING_SEND = 30      # send ping every 30s
INTERVAL_PING_RECEIVE = 20   # detect lost after 20s
MAX_PING_LOST = 3            # reconnect after 3 consecutive lost

CREATE_TOPIC_URL = "http://api.bemfa.com/api/user/addtopic/"

MSG_ON = "on"
MSG_OFF = "off"


class BemfaDevice:
    """Represents a Bemfa switch device."""

    def __init__(self, entity_id: str, name: str):
        self.entity_id = entity_id
        self.name = name
        self._topic: Optional[str] = None
        self._pub_topic: Optional[str] = None

    @property
    def topic(self) -> str:
        if self._topic is None:
            md5 = hashlib.md5(self.entity_id.encode("utf-8")).hexdigest()
            self._topic = f"{TOPIC_PREFIX}{md5}006"
        return self._topic

    @property
    def pub_topic(self) -> str:
        if self._pub_topic is None:
            self._pub_topic = TOPIC_PUBLISH.format(topic=self.topic)
        return self._pub_topic


class BemfaClient:
    """Bemfa cloud MQTT client with HTTP topic registration."""

    def __init__(self, uid: str):
        self._uid = uid
        self._client: Optional[mqtt.Client] = None
        self._devices: dict[str, BemfaDevice] = {}
        self._state_cache: dict[str, str] = {}  # topic -> "on"/"off"
        self._command_callbacks: dict[str, Callable[[bool], None]] = {}
        self._connected = False
        self._connect_time = 0.0
        self._lock = threading.Lock()
        # Ping/pong keepalive (aligned with HA integration)
        self._ping_lost = 0
        self._ping_publish_task: Optional[asyncio.Task] = None
        self._ping_receive_task: Optional[asyncio.Task] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def add_device(self, entity_id: str, name: str) -> BemfaDevice:
        """Register a device to sync with Bemfa."""
        dev = BemfaDevice(entity_id, name)
        self._devices[entity_id] = dev
        return dev

    def on_command(self, entity_id: str, callback: Callable[[bool], None]):
        """Register command callback for a device."""
        self._command_callbacks[entity_id] = callback

    async def start(self):
        """Connect MQTT and register topics."""
        if self._client:
            return

        # Register topics via HTTP
        await self._register_topics()

        # Connect MQTT (in thread pool since paho is synchronous)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._connect_mqtt)

        # Start ping/pong keepalive (aligned with HA integration)
        self._ping_lost = 0
        self._start_ping_cycle()
        _LOGGER.info("Bemfa client started")

    async def stop(self):
        """Disconnect MQTT and cleanup."""
        # Cancel ping tasks
        for task in (self._ping_publish_task, self._ping_receive_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._ping_publish_task = None
        self._ping_receive_task = None

        if self._client:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._disconnect_mqtt)
            self._client = None

        self._connected = False
        _LOGGER.info("Bemfa client stopped")

    def publish_state(self, entity_id: str, state: str):
        """Publish state to Bemfa. Called from any thread."""
        with self._lock:
            if not self._client or not self._connected:
                return
            dev = self._devices.get(entity_id)
            if not dev:
                return
            self._state_cache[dev.topic] = state
            self._client.publish(dev.pub_topic, state, qos=1, retain=True)

    # ---- MQTT ----

    def _connect_mqtt(self):
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, self._uid, mqtt.MQTTv311
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        try:
            self._client.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
            self._client.loop_start()
            _LOGGER.info("Bemfa MQTT connecting to %s:%s", MQTT_HOST, MQTT_PORT)
        except Exception as e:
            _LOGGER.error("Bemfa MQTT connection failed: %s", e)
            try:
                self._client.loop_stop()
            except Exception:
                pass
            self._client = None

    def _disconnect_mqtt(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        self._connected = False

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            with self._lock:
                self._connected = True
            self._connect_time = time.time()
            _LOGGER.info("Bemfa MQTT connected")

            # Subscribe to all device topics
            for dev in self._devices.values():
                client.subscribe(dev.topic, 1)
                _LOGGER.debug("Bemfa subscribed: %s", dev.topic)

            # Subscribe to ping topic
            client.subscribe(TOPIC_PING, 1)

            # Publish initial states so Bemfa marks device online
            with self._lock:
                for dev in self._devices.values():
                    state = self._state_cache.get(dev.topic, MSG_OFF)
                    client.publish(dev.pub_topic, state, qos=1, retain=True)
        else:
            _LOGGER.warning("Bemfa MQTT connect failed: rc=%s", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        with self._lock:
            self._connected = False
        _LOGGER.warning("Bemfa MQTT disconnected (rc=%s)", rc)

    def _on_message(self, client, userdata, message):
        topic = message.topic
        data = message.payload.decode("utf-8", errors="replace")

        # Handle ping pong (aligned with HA integration)
        if topic == TOPIC_PING:
            if self._ping_receive_task is not None:
                self._ping_receive_task.cancel()
                self._ping_receive_task = None
                self._ping_lost = 0
            return

        # Find device by topic
        for entity_id, dev in self._devices.items():
            if dev.topic == topic:
                # Ignore echo during grace period (10s after connect, aligned with ESP32)
                now = time.time()
                if now - self._connect_time < 10:
                    _LOGGER.debug("Bemfa ignoring echo: %s=%s", topic, data)
                    return

                on = data.strip().lower() == "on"
                _LOGGER.info("Bemfa recv: %s=%s", entity_id, data)

                # Execute command
                cb = self._command_callbacks.get(entity_id)
                if cb is None:
                    _LOGGER.warning("Bemfa no callback registered for %s", entity_id)
                    return
                try:
                    ok = cb(on)
                    if ok:
                        with self._lock:
                            self._state_cache[topic] = MSG_ON if on else MSG_OFF
                except Exception as e:
                    _LOGGER.error("Bemfa command error: %s=%s: %s", entity_id, data, e)
                break

    # ---- HTTP API ----

    async def _register_topics(self):
        """Register all device topics via HTTP API."""
        _LOGGER.info("Bemfa registering %d topics...", len(self._devices))
        async with aiohttp.ClientSession() as session:
            for dev in self._devices.values():
                try:
                    async with session.post(
                        CREATE_TOPIC_URL,
                        data={
                            "uid": self._uid,
                            "topic": dev.topic,
                            "type": 1,
                            "name": dev.name,
                        },
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            _LOGGER.info("Bemfa registered: %s (%s)", dev.topic, dev.name)
                        else:
                            _LOGGER.warning(
                                "Bemfa register failed: %s HTTP %d", dev.topic, resp.status
                            )
                except Exception as e:
                    _LOGGER.warning("Bemfa register error: %s: %s", dev.topic, e)
                await asyncio.sleep(0.1)  # small delay between requests

    # ---- Ping/Pong Keepalive (aligned with HA integration) ----

    def _start_ping_cycle(self):
        """Start a ping cycle. Recursive: each cycle schedules the next.
        
        Matches official HA integration's _ping() pattern exactly.
        Only ONE receive task exists at any time.
        """
        async def _publish_job():
            await asyncio.sleep(INTERVAL_PING_SEND)
            with self._lock:
                if not self._client or not self._connected:
                    # Not connected yet — retry next cycle
                    self._start_ping_cycle()
                    return
                self._client.publish(TOPIC_PING, "ping")  # QoS 0 (matches official)
            # Start receive monitor for THIS cycle
            self._ping_receive_task = asyncio.create_task(_receive_job())
            # Schedule next cycle (recursive, ensures one-at-a-time)
            self._start_ping_cycle()

        async def _receive_job():
            await asyncio.sleep(INTERVAL_PING_RECEIVE)
            self._ping_lost += 1
            _LOGGER.warning("Bemfa ping lost (%d/%d)", self._ping_lost, MAX_PING_LOST)
            if self._ping_lost == MAX_PING_LOST:  # == (matches official)
                self._ping_lost = 0
                _LOGGER.warning("Bemfa max ping lost, reconnecting...")
                await self._reconnect()

        self._ping_publish_task = asyncio.create_task(_publish_job())

    async def _reconnect(self):
        """Disconnect and reconnect MQTT, restart ping cycle."""
        self._ping_lost = 0
        # Cancel publish chain only (future cycles), not _ping_receive_task
        if self._ping_publish_task and not self._ping_publish_task.done():
            self._ping_publish_task.cancel()
            try:
                await self._ping_publish_task
            except asyncio.CancelledError:
                pass
        self._ping_publish_task = None
        self._ping_receive_task = None
        # Disconnect MQTT
        loop = asyncio.get_event_loop()
        if self._client:
            await loop.run_in_executor(None, self._disconnect_mqtt)
        # Reconnect MQTT
        await loop.run_in_executor(None, self._connect_mqtt)
        # Restart ping cycle (fresh chain, no duplicate tasks)
        self._start_ping_cycle()
