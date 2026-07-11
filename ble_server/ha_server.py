"""CUKTECH BLE data server for Home Assistant integration.

BLE data is published to MQTT for real-time updates in Home Assistant.
"""
import asyncio
import gzip
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from aiohttp import web

from config import load_config, LOG_LEVELS
from state import ChargerState, PORT_BITS, PORT_NAMES, PORT_DEFAULT, VALID_PIIDS, PIID_RANGES
from ble_manager import BLEManager, set_status_cache_invalidator
from history import PortHistory

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
        self._chart_cache = {}
        self._chart_cache_ttl = 10
        self._chart_cache_max = 50
        self.history = PortHistory(
            db_path=self.config.server.history_db_path,
            retention_days=self.config.server.history_retention_days,
        )
        log_file = Path(__file__).parent / ".log_level"
        log_level = self.config.server.log_level
        if log_file.exists():
            saved = log_file.read_text().strip()
            if saved in LOG_LEVELS:
                log_level = saved
        logging.getLogger().setLevel(LOG_LEVELS.get(log_level, logging.INFO))

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
            s = get_server()
            if s.ble:
                s.ble.set_mqtt_publisher(s.mqtt_publish)
            s.setup_mqtt_subscriptions()

        def on_disconnect(client, userdata, flags, rc, properties=None):
            _LOGGER.warning("MQTT disconnected (rc=%s)", rc)

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_disconnect = on_disconnect

        self.mqtt_client.will_set(
            self.config.topic_status,
            json.dumps({"connected": False}),
            retain=True, qos=1
        )

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
                    _LOGGER.info("MQTT set command: piid=%d value=%d", piid_int, value_int)
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
                    _LOGGER.info("MQTT port command: port=%s action=%s", port, action)
                elif msg.topic == f"{server.config.mqtt.topic_prefix}/protocol":
                    data = {k: v for k, v in payload.items() if k in ("port", "protocol", "switches", "value")}
                    server.loop.call_soon_threadsafe(
                        server.ble.cmd_queue.put_nowait,
                        ("protocol", data, None))
                    _LOGGER.info("MQTT protocol command: %s", data)
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                _LOGGER.error("MQTT cmd parse error: %s", e)
            except Exception as e:
                _LOGGER.error("MQTT cmd error: %s", e)

        self.mqtt_client.on_message = on_mqtt_message
        self.mqtt_client.subscribe(f"{self.config.mqtt.topic_prefix}/set")
        self.mqtt_client.subscribe(f"{self.config.mqtt.topic_prefix}/port")
        self.mqtt_client.subscribe(f"{self.config.mqtt.topic_prefix}/protocol")
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
        mqtt_connected = self.mqtt_client is not None and self.mqtt_client.is_connected()
        data["mqtt_connected"] = mqtt_connected
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

    async def handle_protocol(self, request):
        """POST /api/protocol — 协议开关控制 (PIID 21)."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        data = {k: v for k, v in body.items() if k in ("port", "protocol", "switches", "value")}
        if not data:
            return web.json_response({"ok": False, "error": "need port+protocol, switches, or value"}, status=400)
        result = await self.ble.send_command("protocol", data)
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
                app_ = request.app
                if "ble_task" in app_:
                    old = app_["ble_task"]
                    if old and not old.done():
                        old.cancel()
                        try:
                            await old
                        except asyncio.CancelledError:
                            pass
                app_["ble_task"] = asyncio.create_task(self.ble.start())
        else:
            async with self._start_lock:
                self.ble._stop_event.set()
                app_ = request.app
                if "ble_task" in app_ and app_["ble_task"] and not app_["ble_task"].done():
                    try:
                        await asyncio.wait_for(app_["ble_task"], timeout=10)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                # _disconnect() (在 start() 的 finally 中) 已进行完整的 Bleak 清理，
                # 不需要额外调用 _force_disconnect_bluetooth()（仅用于错误恢复/关机）
            for piid in range(1, 5):
                await self.state.update_port(piid, PORT_DEFAULT)
            if self.mqtt_client and self.mqtt_client.is_connected():
                for piid, pname in PORT_NAMES.items():
                    self.mqtt_publish(f"{self.config.topic_port}/{pname}", PORT_DEFAULT)
                self.mqtt_publish(self.config.topic_status, {"connected": False}, retain=True)
            else:
                _LOGGER.warning("MQTT not connected, port data not cleared via MQTT")
        self.invalidate_status_cache()
        return web.json_response({"ok": True, "enabled": enabled})

    async def handle_log_level(self, request):
        """Get or set log level."""
        if request.method == "GET":
            current = logging.getLogger().level
            level_name = logging.getLevelName(current).lower()
            return web.json_response({
                "level": level_name,
                "available": list(LOG_LEVELS.keys()),
            })

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        level = data.get("level", "").lower()
        if level not in LOG_LEVELS:
            return web.json_response({"ok": False, "error": f"invalid level: {level}"}, status=400)

        logging.getLogger().setLevel(LOG_LEVELS[level])
        log_file = Path(__file__).parent / ".log_level"
        log_file.write_text(level)
        _LOGGER.info("Log level changed to %s", level)
        return web.json_response({"ok": True, "level": level})

    async def handle_chart(self, request):
        """Get chart-ready data for all ports with caching and ETag."""
        try:
            hours = min(float(request.query.get("hours", 1)), 720)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "invalid hours parameter"}, status=400)
        try:
            interval = max(int(request.query.get("interval", 30)), 5)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "invalid interval parameter"}, status=400)
        cache_key = f"{hours}:{interval}"

        # Check cache
        now = time.time()
        if cache_key in self._chart_cache:
            cached_time, cached_etag, cached_body = self._chart_cache[cache_key]
            if now - cached_time < self._chart_cache_ttl:
                # Check ETag
                if_none_match = request.headers.get("If-None-Match")
                if if_none_match == cached_etag:
                    return web.Response(status=304)
                return web.Response(
                    body=cached_body,
                    content_type="application/json",
                    headers={"ETag": cached_etag},
                )

        # Generate data
        now_ts = time.time()
        start_ts = now_ts - hours * 3600
        aligned_start = (int(start_ts) // interval) * interval

        use_date = hours > 12
        epochs = list(range(aligned_start, int(now_ts) + 1, interval))
        if use_date:
            all_labels = [time.strftime('%m-%d %H:%M', time.localtime(t)) for t in epochs]
        else:
            all_labels = [time.strftime('%H:%M', time.localtime(t)) for t in epochs]

        raw_rows = self.history.query_history_multi(1, 4, hours, interval)

        # Build port_data with epoch int as key, store tuple instead of dict
        port_data = {p: {} for p in range(1, 5)}
        for row in raw_rows:
            port_data[row["port"]][int(row["bucket"])] = (
                row["power"], row["voltage"], row["current"]
            )

        # Pre-allocate arrays for single-pass construction
        n_labels = len(all_labels)
        power_per_port = [[0.0] * n_labels for _ in range(5)]   # [0..3] ports, [4] total
        voltage_per_port = [[0.0] * n_labels for _ in range(4)]
        current_per_port = [[0.0] * n_labels for _ in range(4)]

        # Single pass to fill all arrays
        for i, epoch in enumerate(epochs):
            total = 0.0
            for port in range(1, 5):
                entry = port_data[port].get(epoch)
                if entry is not None:
                    p, v, c = entry
                    power_per_port[port - 1][i] = round(p, 1)
                    voltage_per_port[port - 1][i] = round(v, 2)
                    current_per_port[port - 1][i] = round(c, 2)
                    total += p
            power_per_port[4][i] = round(total, 1)

        port_names = ["C1", "C2", "C3", "A"]
        power_datasets = [{"label": port_names[p], "data": power_per_port[p]} for p in range(4)]
        voltage_datasets = [{"label": port_names[p], "data": voltage_per_port[p]} for p in range(4)]
        current_datasets = [{"label": port_names[p], "data": current_per_port[p]} for p in range(4)]

        result = {
            "ok": True,
            "labels": all_labels,
            "datasets": {
                "power": power_datasets + [{"label": "Total", "data": power_per_port[4]}],
                "voltage": voltage_datasets,
                "current": current_datasets,
            },
        }

        body = json.dumps(result, ensure_ascii=False).encode()
        etag = hashlib.md5(body).hexdigest()

        # Update cache with cleanup
        self._chart_cache[cache_key] = (now, etag, body)
        if len(self._chart_cache) > self._chart_cache_max:
            expired = [k for k, (t, _, _) in self._chart_cache.items() if now - t > self._chart_cache_ttl]
            for k in expired:
                del self._chart_cache[k]
            # If still over max, remove oldest entries
            if len(self._chart_cache) > self._chart_cache_max:
                sorted_keys = sorted(self._chart_cache.keys(), key=lambda k: self._chart_cache[k][0])
                for k in sorted_keys[:len(self._chart_cache) - self._chart_cache_max]:
                    del self._chart_cache[k]

        return web.Response(
            body=body,
            content_type="application/json",
            headers={"ETag": etag},
        )

    async def handle_statistics(self, request):
        """Get port statistics."""
        try:
            port = int(request.match_info.get("port", 1))
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        hours = min(float(request.query.get("hours", 24)), 720)

        if port not in range(1, 5):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)

        stats = self.history.get_statistics(port, int(hours))
        return web.json_response({"ok": True, "data": stats})

    async def handle_export(self, request):
        """Export port history as CSV."""
        try:
            port = int(request.match_info.get("port", 1))
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        hours = min(float(request.query.get("hours", 24)), 720)

        if port not in range(1, 5):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)

        csv_data = self.history.export_csv(port, int(hours))
        return web.Response(
            body=csv_data,
            content_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=port_{port}_history.csv"},
        )

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
    origin = request.headers.get("Origin", "")
    s = get_server()
    allowed_origins = {
        f"http://localhost:{s.config.server.port}",
        f"http://127.0.0.1:{s.config.server.port}",
    }
    if origin and origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@web.middleware
async def gzip_middleware(request, handler):
    response = await handler(request)
    if request.headers.get("Accept-Encoding", "").find("gzip") == -1:
        return response
    if response.content_length is not None and response.content_length < 1024:
        return response
    if response.content_type and "text" not in response.content_type and "json" not in response.content_type:
        return response
    body = response.body
    if isinstance(body, bytes):
        compressed = gzip.compress(body)
        if len(compressed) < len(body):
            response.body = compressed
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
    return response


@web.middleware
async def cache_middleware(request, handler):
    response = await handler(request)
    if request.path.startswith("/static/"):
        if request.path.endswith((".js", ".css", ".png", ".ico", ".woff", ".woff2")):
            if os.environ.get("CUKTECH_ENV") == "development":
                response.headers["Cache-Control"] = "no-cache"
            else:
                response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response


app = web.Application(middlewares=[cors_middleware, gzip_middleware, cache_middleware])
app.router.add_get("/", lambda r: get_server().handle_index(r))
app.router.add_get("/api/status", lambda r: get_server().handle_status(r))
app.router.add_post("/api/set", lambda r: get_server().handle_set(r))
app.router.add_post("/api/port", lambda r: get_server().handle_port(r))
app.router.add_post("/api/protocol", lambda r: get_server().handle_protocol(r))
app.router.add_post("/api/enable", lambda r: get_server().handle_enable(r))
app.router.add_get("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_post("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_get("/api/chart", lambda r: get_server().handle_chart(r))
app.router.add_get("/api/statistics/{port}", lambda r: get_server().handle_statistics(r))
app.router.add_get("/api/export/{port}", lambda r: get_server().handle_export(r))
app.router.add_static("/static", WEB_DIR / "static", show_index=False)


async def on_startup(app_):
    s = get_server()
    async with s._start_lock:
        s.loop = asyncio.get_running_loop()
        set_status_cache_invalidator(s.invalidate_status_cache)
        s.history.connect()
        s.ble.set_history(s.history)
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
    s.history.close()


app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    s = get_server()
    web.run_app(app, host="0.0.0.0", port=s.config.server.port)
