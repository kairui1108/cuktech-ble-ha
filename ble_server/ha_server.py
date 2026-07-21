"""CUKTECH BLE data server for Home Assistant integration.

BLE data is published to MQTT for real-time updates in Home Assistant.
"""
import asyncio
import re
import warnings
warnings.filterwarnings('ignore', message='.*default MTU.*')
import gzip
import hashlib
import json
import logging
import os
import signal
import time
from pathlib import Path
from aiohttp import web

from config import load_config, LOG_LEVELS
from state import ChargerState, PORT_BITS, PORT_NAMES, PORT_DEFAULT, VALID_PIIDS, PIID_RANGES, PROTOCOL_SWITCH_BITS
from ble_manager import BLEManager, set_status_cache_invalidator
from history import PortHistory
from bemfa_client import BemfaClient, MSG_ON, MSG_OFF

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
        self.bemfa: BemfaClient | None = None
        log_file = Path(__file__).parent / ".log_level"
        log_level = self.config.server.log_level
        if log_file.exists():
            saved = log_file.read_text().strip()
            if saved in LOG_LEVELS:
                log_level = saved
        logging.getLogger().setLevel(LOG_LEVELS.get(log_level, logging.INFO))

    def mqtt_publish(self, topic, payload, retain=False):
        """Publish to all enabled MQTT clients (multiplex)."""
        # HA MQTT
        if self.mqtt_client and self.mqtt_client.is_connected():
            self.mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)
        # Bemfa
        if self.bemfa and self.bemfa.is_connected:
            self._bemfa_publish(topic, payload)

    def _bemfa_publish(self, topic, payload):
        """Map HA MQTT topics to Bemfa device states."""
        topic_prefix = self.config.mqtt.topic_prefix
        # Port state: {prefix}/port/{port_name}
        if topic.startswith(f"{topic_prefix}/port/"):
            port_name = topic.split("/")[-1]
            entity_map = {
                "c1": "cuktech_c1",
                "c2": "cuktech_c2",
                "c3": "cuktech_c3",
                "a": "cuktech_usb_a",
            }
            entity_id = entity_map.get(port_name)
            if entity_id and isinstance(payload, dict):
                state = MSG_ON if payload.get("active") else MSG_OFF
                self.bemfa.publish_state(entity_id, state)
        # BLE status: {prefix}/status
        elif topic == f"{topic_prefix}/status":
            if isinstance(payload, dict):
                connected = payload.get("connected", False)
                self.bemfa.publish_state("cuktech_ble", MSG_ON if connected else MSG_OFF)

    async def setup_bemfa(self):
        """Initialize Bemfa client if enabled."""
        if not self.config.bemfa.enabled or not self.config.bemfa.uid:
            _LOGGER.info("Bemfa disabled or UID not configured")
            return

        # Cleanup old client if exists
        if self.bemfa:
            await self.bemfa.stop()

        self.bemfa = BemfaClient(self.config.bemfa.uid)

        # Register devices
        self.bemfa.add_device("cuktech_c1", "C口1开关")
        self.bemfa.add_device("cuktech_c2", "C口2开关")
        self.bemfa.add_device("cuktech_c3", "C口3开关")
        self.bemfa.add_device("cuktech_usb_a", "USB-A开关")
        self.bemfa.add_device("cuktech_ble", "蓝牙开关")

        # Register command callbacks
        def _port_cmd(port, on):
            _LOGGER.info("Bemfa command: %s %s", port, "on" if on else "off")
            try:
                self.loop.call_soon_threadsafe(
                    self.ble.cmd_queue.put_nowait,
                    ("port", (port, "on" if on else "off"), None))
                return True
            except Exception as e:
                _LOGGER.error("Bemfa port cmd failed: %s", e)
                return False

        def _ble_cmd(on):
            _LOGGER.info("Bemfa BLE command: %s", "on" if on else "off")
            try:
                if on:
                    asyncio.run_coroutine_threadsafe(self.ble.start(), self.loop)
                else:
                    asyncio.run_coroutine_threadsafe(self.ble.request_stop(), self.loop)
                return True
            except Exception as e:
                _LOGGER.error("Bemfa BLE cmd failed: %s", e)
                return False

        self.bemfa.on_command("cuktech_c1", lambda on: _port_cmd("c1", on))
        self.bemfa.on_command("cuktech_c2", lambda on: _port_cmd("c2", on))
        self.bemfa.on_command("cuktech_c3", lambda on: _port_cmd("c3", on))
        self.bemfa.on_command("cuktech_usb_a", lambda on: _port_cmd("a", on))
        self.bemfa.on_command("cuktech_ble", _ble_cmd)

        await self.bemfa.start()

    async def handle_bemfa(self, request):
        """GET /api/bemfa - get Bemfa status."""
        enabled = self.bemfa is not None
        connected = self.bemfa is not None and self.bemfa.is_connected
        uid = self.config.bemfa.uid
        uid_display = f"{uid[:4]}****" if len(uid) > 4 else uid
        return web.json_response({
            "enabled": enabled,
            "connected": connected,
            "uid": uid_display,
            "configured": bool(uid),
        })

    async def setup_mqtt(self):
        if not self.config.mqtt.enabled:
            _LOGGER.info("MQTT disabled, running in standalone web server mode")
            return

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

        for attempt in range(3):
            try:
                self.mqtt_client.connect(self.config.mqtt.host, self.config.mqtt.port, self.config.mqtt.keepalive)
                self.mqtt_client.loop_start()
                _LOGGER.info("MQTT connecting to %s:%s", self.config.mqtt.host, self.config.mqtt.port)
                break
            except Exception:
                _LOGGER.error("MQTT connection failed (attempt %d/3): %s:%s",
                              attempt + 1, self.config.mqtt.host, self.config.mqtt.port)
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
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
        """处理协议开关 (PIID 21)。

        请求体:
          {"port": "c1", "protocol": "pd"}           # toggle
          {"port": "c1", "protocol": "pd", "action": "on"}   # 显式开关
          {"switches": {"c1": {"pd": true, ...}}}     # 批量设置
          {"value": 50532111}                         # 直接写原始值
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        try:
            state = self.ble.state
            if "value" in body:
                new_val = int(body["value"])
                if not (0 <= new_val <= 0xFFFFFFFF):
                    return web.json_response({"ok": False, "error": "value out of range (0-0xFFFFFFFF)"}, status=400)
            elif "switches" in body:
                new_val = ChargerState.encode_protocol_extend(body["switches"])
            elif "port" in body and "protocol" in body:
                port = body["port"].lower()
                proto = body["protocol"].lower()
                action = body.get("action", "toggle")

                # 加锁确保 read-modify-write-send 原子性，防止竞态
                async with state.lock:
                    current_val = state.protocol_extend
                    switches = dict(state.protocol_switches)

                    if port not in switches or proto not in switches[port]:
                        return web.json_response({"ok": False, "error": f"unknown {port}.{proto}"}, status=400)

                    cur = switches[port][proto]
                    if action == "toggle":
                        new_state = not cur
                    elif action == "on":
                        new_state = True
                    elif action == "off":
                        new_state = False
                    else:
                        return web.json_response({"ok": False, "error": f"invalid action: {action}"}, status=400)

                    _LOGGER.info("Protocol switch: %s.%s %s->%s (current 0x%08X)",
                                 port, proto, cur, new_state, current_val)

                    # 构建新的开关状态
                    new_switches = dict(switches)
                    new_switches[port] = dict(switches[port])
                    new_switches[port][proto] = new_state

                    new_val = ChargerState.encode_protocol_extend(new_switches)

                # 锁外发送 SET (send_command 内部也有 async 操作)
                result = await self.ble.send_command("set", (21, new_val))
                # 同步本地状态，确保后续 GET 读到最新值
                if result and result.get("ok"):
                    await state.update_protocol_extend(new_val)
                self.invalidate_status_cache()
                return web.json_response(result)
            else:
                return web.json_response({"ok": False, "error": "missing port/protocol or value"}, status=400)

            # 批量/原始值路径：锁外发送
            result = await self.ble.send_command("set", (21, new_val))
            if result and result.get("ok"):
                await state.update_protocol_extend(new_val)
            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Protocol switch error: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_enable(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        enabled = body.get("enabled", True)
        if enabled:
            async with self._start_lock:
                if self.ble.is_running:
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
                await self.ble.request_stop()
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
        aligned_now = (int(now_ts) // interval) * interval
        epochs = list(range(aligned_start, aligned_now, interval))
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
        etag = hashlib.sha256(body).hexdigest()

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

    MOBILE_UA = re.compile(r'Android|iPhone|iPod|webOS|BlackBerry|Windows Phone', re.I)

    async def handle_index(self, request):
        ua = request.headers.get('User-Agent', '')
        if self.MOBILE_UA.search(ua):
            return web.FileResponse(WEB_DIR / 'phone.html')
        return web.FileResponse(WEB_DIR / 'index.html')

    # ── Charge Session API ──

    async def handle_sessions(self, request):
        """GET /api/sessions?port=c1&period=today&limit=10&page=1"""
        port_str = request.query.get("port", "")
        period = request.query.get("period", "today")
        limit = min(int(request.query.get("limit", "10")), 50)
        page = max(1, int(request.query.get("page", "1")))

        port = None
        if port_str:
            port_map = {"c1": 1, "c2": 2, "c3": 3, "a": 4}
            port = port_map.get(port_str)

        loop = asyncio.get_running_loop()
        sessions, total = await loop.run_in_executor(
            None, self.history.get_sessions, port, period, limit, (page - 1) * limit)

        # Merge live energy data for active sessions
        now = time.time()
        live = self.ble.get_live_session_data()
        db_session_ids = {s.get("id") for s in sessions}
        for port_id, ld in live.items():
            sid = ld.get("session_id")
            matched = False
            for s in sessions:
                if s.get("id") == sid:
                    s["total_wh"] = ld["session_wh"]
                    s["peak_power_w"] = max(s.get("peak_power_w", 0), ld["max_power"])
                    # Recalculate avg from live data (DB values are stale for active sessions)
                    start_time = ld.get("start_time") or now
                    dur_sec = max(1, int(now - start_time))
                    dur_h = dur_sec / 3600.0
                    s["avg_power_w"] = round(ld["session_wh"] / dur_h, 1) if dur_h > 0 else 0
                    port_state = self.ble.state.ports.get(port_id)
                    if port_state:
                        s["avg_voltage"] = round(port_state.voltage, 2)
                        s["avg_current"] = round(port_state.current, 2)
                    s["duration_sec"] = dur_sec
                    s["is_active"] = True
                    matched = True
                    break
            if not matched and sid:
                # Active session not in DB results (total_wh=0, filtered out) — add it
                start_time = ld.get("start_time") or now
                dur_sec = max(1, int(now - start_time))
                dur_h = dur_sec / 3600.0
                avg_p = round(ld["session_wh"] / dur_h, 1) if dur_h > 0 else 0
                # Current V/I from port state as approximate averages for active session
                port_state = self.ble.state.ports.get(port_id)
                avg_v = round(port_state.voltage, 2) if port_state else 0
                avg_i = round(port_state.current, 2) if port_state else 0
                sessions.insert(0, {
                    "id": sid, "port": port_id, "start_time": start_time,
                    "end_time": None, "total_wh": ld["session_wh"],
                    "avg_power_w": avg_p, "peak_power_w": ld["max_power"],
                    "avg_voltage": avg_v, "avg_current": avg_i,
                    "duration_sec": dur_sec,
                    "protocol": port_state.protocol if port_state else "", "is_active": True,
                })
                total += 1

        # Mark unmatched sessions as inactive
        live_sids = {ld.get("session_id") for ld in live.values()}
        for s in sessions:
            if s.get("id") not in live_sids:
                s["is_active"] = False

        return web.json_response({
            "sessions": sessions,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, (total + limit - 1) // limit),
        })

    async def handle_session_points(self, request):
        """GET /api/sessions/{id}/points?downsample=600"""
        sid = int(request.match_info["id"])
        target = int(request.query.get("downsample", "0"))
        loop = asyncio.get_running_loop()
        points = await loop.run_in_executor(
            None, self.history.get_session_points, sid)
        if target > 0 and len(points) > target:
            from downsample import lttb_downsample
            points = await loop.run_in_executor(
                None, lttb_downsample, points, target)
        return web.json_response({"points": points})

    async def handle_energy_stats(self, request):
        """GET /api/energy/stats?period=today"""
        period = request.query.get("period", "today")
        loop = asyncio.get_running_loop()
        stats = await loop.run_in_executor(
            None, self.history.get_energy_stats, period)

        # Merge live data from active sessions
        now = time.time()
        live = self.ble.get_live_session_data()
        live_count = len(live)
        for port, ld in live.items():
            stats["total_wh"] = round(stats["total_wh"] + ld["session_wh"], 2)
            stats["peak_power_w"] = max(stats.get("peak_power_w", 0), ld["max_power"])
            # Add live duration for active sessions
            if ld.get("start_time"):
                live_duration = int(now - ld["start_time"])
                stats["total_duration_sec"] = stats.get("total_duration_sec", 0) + live_duration
            port_key = str(port)
            if port_key not in stats.get("by_port", {}):
                stats.setdefault("by_port", {})[port_key] = {"wh": 0, "count": 0}
            stats["by_port"][port_key]["wh"] = round(
                stats["by_port"][port_key]["wh"] + ld["session_wh"], 2)
            stats["by_port"][port_key]["count"] = stats["by_port"][port_key].get("count", 0) + 1
            stats["by_port"][port_key]["is_active"] = True

        # DB count excludes active sessions (total_wh=0), add them back
        stats["session_count"] = stats.get("session_count", 0) + live_count

        # Recalculate avg_power_w from total energy and total duration
        total_dur = stats.get("total_duration_sec", 0)
        total_wh = stats.get("total_wh", 0)
        stats["avg_power_w"] = round(total_wh / (total_dur / 3600), 1) if total_dur > 0 else 0

        return web.json_response(stats)


WEB_DIR = Path(__file__).parent / "web"
_server = None


def get_server():
    """获取全局 Server 单例 (惰性初始化)。"""
    global _server
    if _server is None:
        _server = Server()
    return _server


def reset_server():
    """重置全局 Server 单例 (仅用于测试)。"""
    global _server
    _server = None


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
app.router.add_get("/phone.html", lambda r: web.FileResponse(WEB_DIR / "phone.html"))
app.router.add_get("/api/status", lambda r: get_server().handle_status(r))
app.router.add_post("/api/set", lambda r: get_server().handle_set(r))
app.router.add_post("/api/port", lambda r: get_server().handle_port(r))
app.router.add_post("/api/enable", lambda r: get_server().handle_enable(r))
app.router.add_post("/api/protocol", lambda r: get_server().handle_protocol(r))
app.router.add_get("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_post("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_get("/api/chart", lambda r: get_server().handle_chart(r))
app.router.add_get("/api/statistics/{port}", lambda r: get_server().handle_statistics(r))
app.router.add_get("/api/export/{port}", lambda r: get_server().handle_export(r))
app.router.add_get("/api/bemfa", lambda r: get_server().handle_bemfa(r))
app.router.add_get("/api/sessions", lambda r: get_server().handle_sessions(r))
app.router.add_get("/api/sessions/{id}/points", lambda r: get_server().handle_session_points(r))
app.router.add_get("/api/energy/stats", lambda r: get_server().handle_energy_stats(r))
app.router.add_static("/static", WEB_DIR / "static", show_index=False)


async def on_startup(app_):
    s = get_server()
    async with s._start_lock:
        s.loop = asyncio.get_running_loop()
        set_status_cache_invalidator(s.invalidate_status_cache)
        s.history.connect()
        s.ble.set_history(s.history)
        await s.setup_mqtt()
        if s.mqtt_client:
            s.setup_mqtt_subscriptions()
        if s.config.bemfa.enabled:
            await s.setup_bemfa()
        app_["ble_task"] = asyncio.create_task(s.ble.start())


async def on_shutdown(app_):
    _LOGGER.info("Shutting down...")
    s = get_server()
    # Close active sessions first (fast, sync DB write)
    s.ble._close_active_sessions()
    # BLE disconnect with timeout
    try:
        await asyncio.wait_for(s.ble.request_stop(), timeout=5.0)
    except asyncio.TimeoutError:
        _LOGGER.warning("BLE stop timed out, forcing disconnect")
        await asyncio.wait_for(s.ble._disconnect(), timeout=3.0)
    except Exception as e:
        _LOGGER.error("BLE stop error: %s", e)
    if s.bemfa:
        try:
            await asyncio.wait_for(s.bemfa.stop(), timeout=3.0)
        except Exception:
            pass
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

    async def _on_startup(app_):
        # Register SIGTERM handler inside event loop for safe asyncio calls
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(app.shutdown()))

    app.on_startup.append(_on_startup)
    web.run_app(app, host="0.0.0.0", port=s.config.server.port)
