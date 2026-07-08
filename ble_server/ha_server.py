"""CUKTECH BLE data server for Home Assistant integration.

BLE data is published to MQTT for real-time updates in Home Assistant.
"""
import asyncio
import json
import logging
from pathlib import Path
from aiohttp import web

from config import load_config
from state import ChargerState, PORT_BITS, PORT_NAMES, PORT_DEFAULT, VALID_PIIDS, PIID_RANGES
from ble_manager import BLEManager, set_status_cache_invalidator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_LOGGER = logging.getLogger("cuktech_server")


class Server:
    def __init__(self):
        self.config = load_config()
        self.state = ChargerState()
        self.ble = BLEManager(self.config.ble.mac, self.config.ble.token, self.state, self.config)
        self.mqtt_client = None
        self.loop = None
        self._start_lock = asyncio.Lock()
        self._status_cache_bytes = None
        self._status_cache_valid = False

    def mqtt_publish(self, topic, payload, retain=False):
        if self.mqtt_client and self.mqtt_client.is_connected():
            self.mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)

    def setup_mqtt(self):
        import paho.mqtt.client as mqtt
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.config.mqtt.username:
            self.mqtt_client.username_pw_set(self.config.mqtt.username, self.config.mqtt.password)
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

        def on_connect(client, userdata, flags, rc, properties=None):
            _LOGGER.info("MQTT connected (rc=%s)", rc)

        def on_disconnect(client, userdata, flags, rc, properties=None):
            _LOGGER.warning("MQTT disconnected (rc=%s)", rc)

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_disconnect = on_disconnect

        try:
            self.mqtt_client.connect(self.config.mqtt.host, self.config.mqtt.port, self.config.mqtt.keepalive)
            self.mqtt_client.loop_start()
            _LOGGER.info("MQTT connecting to %s:%s", self.config.mqtt.host, self.config.mqtt.port)
        except Exception as e:
            _LOGGER.error("MQTT connection failed: %s", e)
            self.mqtt_client = None

        if self.mqtt_client:
            self.ble.set_mqtt_publisher(self.mqtt_publish)

    def setup_mqtt_subscriptions(self):
        if not self.mqtt_client:
            return
        server = self

        def on_mqtt_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload)
                if server.loop is None:
                    return
                if msg.topic == f"{server.config.mqtt.topic_prefix}/set":
                    piid = payload.get("piid")
                    value = payload.get("value")
                    if piid is None or value is None:
                        _LOGGER.warning("Invalid set command: missing piid or value")
                        return
                    try:
                        piid_int = int(piid)
                        value_int = int(value)
                    except (ValueError, TypeError):
                        _LOGGER.warning("Invalid set command: piid/value must be integers")
                        return
                    if piid_int not in VALID_PIIDS:
                        _LOGGER.warning("Invalid set command: piid %d not valid", piid_int)
                        return
                    min_val, max_val = PIID_RANGES[piid_int]
                    if not (min_val <= value_int <= max_val):
                        _LOGGER.warning("Invalid set command: value %d out of range [%d, %d]", value_int, min_val, max_val)
                        return
                    server.loop.call_soon_threadsafe(
                        server.ble.cmd_queue.put_nowait,
                        ("set", (piid_int, value_int), None))
                elif msg.topic == f"{server.config.mqtt.topic_prefix}/port":
                    port = payload.get("port")
                    action = payload.get("action")
                    if not port or action not in ("on", "off"):
                        _LOGGER.warning("Invalid port command: port=%s action=%s", port, action)
                        return
                    if port not in PORT_BITS and port != "all":
                        _LOGGER.warning("Invalid port command: unknown port %s", port)
                        return
                    server.loop.call_soon_threadsafe(
                        server.ble.cmd_queue.put_nowait,
                        ("port", (port, action), None))
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                _LOGGER.error("MQTT cmd parse error: %s", e)
            except Exception as e:
                _LOGGER.error("MQTT cmd error: %s", e)

        self.mqtt_client.on_message = on_mqtt_message
        self.mqtt_client.subscribe(f"{self.config.mqtt.topic_prefix}/set")
        self.mqtt_client.subscribe(f"{self.config.mqtt.topic_prefix}/port")
        _LOGGER.info("MQTT subscriptions ready")

    def invalidate_status_cache(self):
        self._status_cache_valid = False

    async def handle_status(self, request):
        if self._status_cache_valid and self._status_cache_bytes:
            return web.Response(
                body=self._status_cache_bytes,
                content_type="application/json",
            )
        data = await self.state.to_dict()
        self._status_cache_bytes = json.dumps(data, ensure_ascii=False).encode()
        self._status_cache_valid = True
        return web.Response(
            body=self._status_cache_bytes,
            content_type="application/json",
        )

    async def handle_set(self, request):
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        piid = data.get("piid")
        value = data.get("value")
        if piid is None or value is None:
            return web.json_response({"ok": False, "error": "missing piid or value"}, status=400)
        try:
            piid_int = int(piid)
            value_int = int(value)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "piid and value must be integers"}, status=400)
        if piid_int not in VALID_PIIDS:
            return web.json_response({"ok": False, "error": f"invalid piid: {piid_int}"}, status=400)
        min_val, max_val = PIID_RANGES[piid_int]
        if not (min_val <= value_int <= max_val):
            return web.json_response({"ok": False, "error": f"value must be between {min_val} and {max_val}"}, status=400)
        result = await self.ble.send_command("set", (piid_int, value_int))
        self.invalidate_status_cache()
        return web.json_response(result)

    async def handle_port(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        port = body.get("port", "").lower()
        action = body.get("action", "").lower()
        if action not in ("on", "off"):
            return web.json_response({"ok": False, "error": "action must be on/off"}, status=400)
        if port not in PORT_BITS and port != "all":
            return web.json_response({"ok": False, "error": f"unknown port: {port}"}, status=400)
        result = await self.ble.send_command("port", (port, action))
        self.invalidate_status_cache()
        return web.json_response(result)

    async def handle_enable(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        enabled = body.get("enabled", True)
        if enabled:
            async with self._start_lock:
                if not self.ble._stop_event.is_set():
                    return web.json_response({"ok": True, "enabled": True, "note": "already running"})
                asyncio.create_task(self.ble.start())
        else:
            async with self._start_lock:
                await self.ble.stop()
            for piid in range(1, 5):
                await self.state.update_port(piid, PORT_DEFAULT)
            for piid, pname in PORT_NAMES.items():
                self.mqtt_publish(f"{self.config.topic_port}/{pname}", PORT_DEFAULT)
            self.mqtt_publish(self.config.topic_status, {"connected": False}, retain=True)
        self.invalidate_status_cache()
        return web.json_response({"ok": True, "enabled": enabled})

    async def handle_index(self, request):
        return web.FileResponse(WEB_DIR / "index.html")


WEB_DIR = Path(__file__).parent / "web"
server = None


def get_server():
    global server
    if server is None:
        server = Server()
    return server


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    origin = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


app = web.Application(middlewares=[cors_middleware])
app.router.add_get("/", lambda r: get_server().handle_index(r))
app.router.add_get("/api/status", lambda r: get_server().handle_status(r))
app.router.add_post("/api/set", lambda r: get_server().handle_set(r))
app.router.add_post("/api/port", lambda r: get_server().handle_port(r))
app.router.add_post("/api/enable", lambda r: get_server().handle_enable(r))
app.router.add_static("/static", WEB_DIR / "static", show_index=False)


async def on_startup(app_):
    s = get_server()
    async with s._start_lock:
        s.loop = asyncio.get_running_loop()
        set_status_cache_invalidator(s.invalidate_status_cache)
        s.setup_mqtt()
        if s.mqtt_client:
            s.setup_mqtt_subscriptions()
        else:
            _LOGGER.warning("MQTT not connected, BLE commands via MQTT will not work")
        app_["ble_task"] = asyncio.create_task(s.ble.start())


async def on_shutdown(app_):
    _LOGGER.info("Shutting down...")
    s = get_server()
    await s.ble.stop()
    ble_task = app_.get("ble_task")
    if ble_task:
        ble_task.cancel()
        try:
            await ble_task
        except asyncio.CancelledError:
            pass
    if s.mqtt_client:
        s.mqtt_client.loop_stop()
        s.mqtt_client.disconnect()


app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    s = get_server()
    web.run_app(app, host="0.0.0.0", port=s.config.server.port)
