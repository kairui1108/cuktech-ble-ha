"""P0 基线锁定 —— 单设备外部可见行为的 golden 快照脚手架。

设计文档：docs/plans/2026-10-23-multi-device-architecture.md §5.1、§7（P0）

只做一个环境：在**完全确定**的条件下跑**真实的**生产装配路径
（`Server.__init__` → `on_startup` → handler → `BLEManager._publish_*` → SSE/MQTT），
把外部可见产物（HTTP 响应体、SSE 线字节、MQTT 发布四元组）交给对比。

三条纪律：

1. **不复制生产逻辑**。handler / SSEEmitter / BLEManager 发布路径 / MQTT 挂载全是真身。
   被替换的只有三类外部依赖：BLE 硬件（`BLEManager.start` 空转等停）、MQTT broker
   （`paho.Client` 换成记录器）、墙钟（`time.time` 冻结）。
2. **无排除项**。除 `golden/timestamp_fields.md` 白名单里的时间戳类字段外，
   一律逐字节比对。裁剪口径固定，不允许"这字段看起来随机就跳过"。
3. **本模块不含断言**。断言在 `tests/test_golden_baseline.py`。

P1 的"装配完整性测试"（§5.5）复用 `golden_env`：它已经在跑真实 `on_startup`，
从"一台设备"扩到"N 台设备"即可。
"""
import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

GOLDEN_DIR = Path(__file__).parent / "golden"

# 冻结墙钟：2025-11-24T21:20:00Z。这个数本身没有含义，"固定"才是要求——
# 快照里凡是来源于 time.time() 的值都必须由这个常数决定；一旦某处把"运行时刻"
# 泄进外部可见载荷，golden 就会炸，这正是它要抓的回归。
FROZEN_NOW = 1764000000.0

# primary 设备身份（golden 只认这一份，不接受环境漂移）
GOLDEN_MAC = "AA:BB:CC:DD:EE:01"
GOLDEN_TOKEN = "00112233445566778899aabbccddeeff"
TOPIC_PREFIX = "cuktech/charger"

# 多设备基线白名单：这些快照**合法地**携带 `device`/`devices` 字段（§3.8：只在 N>1
# 出现），所以 `TestBaselineGuard` 的"单设备快照不许有设备字段"静态扫描必须跳过它们。
# 跳过是有条件的、可证伪的：`test_golden_multi_device.TestBaselineSet` 反向断言
# 白名单里的文件都存在且确实带设备字段 —— 白名单不能变成放水后门。
MULTI_DEVICE_BASELINES = frozenset({
    "http_devices",        # 端点自身形态（N=1 也带 devices 列表，§3.7）
    "http_health_multi",   # N=2 health 的 devices 真值分区（§3.13）
    "sse_init_multi",      # N=2 SSE init 的 devices 列表（§3.8）
})

_GOLDEN_CONFIG_YAML = f"""
ble:
  mac: "{GOLDEN_MAC}"
  token: "{GOLDEN_TOKEN}"
  ble_key: ""
  scan_timeout: 10

mqtt:
  enabled: true
  host: "127.0.0.1"
  port: 1883
  keepalive: 60
  topic_prefix: "cuktech/charger"
server:
  host: "127.0.0.1"
  port: 8199
  log_level: "warning"
  history_retention_days: 30
  history_db_path: "@DB_PATH@"

bemfa:
  enabled: false
"""


def _as_text(payload) -> str:
    if payload is None:
        return ""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", "replace")
    return str(payload)


class RecordingMQTTClient:
    """`paho.mqtt.client.Client` 的记录替身：只记不发。

    记录点选在 **paho 边界**而不是 `Server.mqtt_publish`：后者内部
    `json.dumps(payload, ensure_ascii=False)` 本身就是外部可见契约的一部分
    （键序、转义、非 ASCII 处理），必须在快照里被锁住。
    """

    CallbackAPIVersion = type("CallbackAPIVersion", (), {"VERSION2": 2})

    def __init__(self, *args, **kwargs):
        self._connected = False
        self.published: list[dict] = []
        self.subscribed: list[str] = []
        self.will: dict | None = None
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    # ── 生命周期 ──
    def connect(self, host, port, keepalive, **kwargs):
        self._connected = True

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    # ── 配置面（服务端会调，替身照单全收）──
    def username_pw_set(self, *args, **kwargs):
        pass

    def max_queued_messages_set(self, *args, **kwargs):
        pass

    def max_inflight_messages_set(self, *args, **kwargs):
        pass

    def reconnect_delay_set(self, *args, **kwargs):
        pass

    # ── 线上动作 ──
    def publish(self, topic, payload, retain=False, qos=0, **kwargs):
        self.published.append({
            "topic": topic,
            "payload": _as_text(payload),
            "retain": bool(retain),
            "qos": int(qos),
        })

    def will_set(self, topic, payload, retain=False, qos=0, **kwargs):
        self.will = {
            "topic": topic,
            "payload": _as_text(payload),
            "retain": bool(retain),
            "qos": int(qos),
        }

    def subscribe(self, topic, *args, **kwargs):
        if isinstance(topic, (list, tuple)):
            self.subscribed.extend(t if isinstance(t, str) else t[0] for t in topic)
        else:
            self.subscribed.append(topic)


async def _ble_start_stub(self):
    """BLE 主循环替身：报告"起来了"，然后等停止。

    真 `start()` 会扫描/连接/认证，golden 不需要硬件；但**需要** `on_startup`
    照常创建那个 task（P1 装配面测试正是要验证 task 归属与 supervisor）。
    """
    self._golden_started = True
    with contextlib.suppress(asyncio.CancelledError):
        await self._stop_event.wait()


def snapshot_state(state) -> None:
    """把 ChargerState 置成一个固定的、覆盖面尽量广的形状。

    刻意覆盖：4 口不同协议（PD/PPS/QC/idle）、PIID 16 端口控制半开（1/2 开、3/4 关，
    让 `_emit_port_state` 的 enabled/非 enabled 两条分支都进快照）、PIID 21 多位置 1、
    settings 有值、型号/固件非空。数值取二进制可精确表示的，避免尾数抖动。
    """
    from state import PortState

    state.connected = True
    state.authenticated = True
    state.ports = {
        1: PortState(voltage=5.0, current=1.0, power=5.0, active=True, protocol="PD"),
        2: PortState(voltage=9.0, current=2.0, power=18.0, active=True, protocol="PPS"),
        3: PortState(voltage=12.0, current=0.75, power=9.0, active=True, protocol="QC"),
        4: PortState(voltage=0.0, current=0.0, power=0.0, active=False, protocol="idle"),
    }
    state.settings = {
        "5": 2, "6": 3, "8": 0, "9": 0, "10": 0, "11": 0, "12": 0,
        "13": 0, "15": 0, "16": 0b0011, "19": 1, "20": 0,
    }
    # c1: 保留位+PD+PPS+UFCS = 0b1111；c2: 仅保留位 = 0b1000；c3: UFCS+SCP = 0b11；a: 0
    state._protocol_extend = (0 << 24) | (0b11 << 16) | (0b1000 << 8) | 0b1111
    state.device_model = "Cuktech 100W Pocket"
    state.firmware_version = "2.1.2_0073"
    state._invalidate_cache()


class GoldenEnv:
    """一台真 Server + 三个被替换的外部依赖。"""

    def __init__(self, server, config_path, db_path, mqtt):
        self.server = server
        self.config_path = config_path
        self.db_path = db_path
        self.mqtt = mqtt

    @property
    def publishes(self) -> list[dict]:
        return list(self.mqtt.published) if self.mqtt else []

    def reset_mqtt(self) -> None:
        if self.mqtt:
            self.mqtt.published.clear()
            self.mqtt.subscribed.clear()
            self.mqtt.will = None

    def open_sse_queue(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.server.sse.MAX_QUEUE_SIZE)
        self.server.sse.add_client(q)
        return q

    def drain_sse(self, queue: asyncio.Queue, limit: int = 32) -> list[str]:
        """非阻塞取出已排好的 SSE 线，元素即**线上字节**（`data: {...}\\n\\n`）。

        `status` 是 latest-only（不入队，存 `_pending_status`），要先 flush，
        与真实 SSE handler 每轮先 `flush_status` 的顺序一致。
        """
        out: list[str] = []
        while len(out) < limit:
            flushed = self.server.sse.flush_status(queue)
            if flushed:
                out.append(f"data: {flushed}\n\n")
            try:
                msg = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            out.append(f"data: {msg}\n\n")
        return out


@contextlib.asynccontextmanager
async def golden_env(*, mqtt_enabled: bool = True, apply_startup: bool = True,
                     devices: list[dict] | None = None,
                     controller_factory=None, real_start: bool = False):
    """确定性单设备环境：真 `Server.__init__`（+ 真 `on_startup`）。

    `devices` 为 P1 预留的多设备配置注入（P0 阶段恒为 None → 只有 primary）。

    `controller_factory` / `real_start=True` 是虚拟充电器的入口（§7 P1）：
    传了工厂就必须让 `BLEManager.start()` 真的跑，否则注入点永远用不上。

    必须在运行中的事件循环里进入（on_startup 会 create_task），即只能用于
    async 测试 / async fixture。退出时跑真 `on_shutdown` 收尾并撤销补丁。
    """
    import ble_manager as bm_module
    import ha_server

    tmp = tempfile.mkdtemp(prefix="cuktech_golden_")
    config_path = os.path.join(tmp, "config.yaml")
    db_path = os.path.join(tmp, "history.db")
    yaml_text = _GOLDEN_CONFIG_YAML.replace("@DB_PATH@", db_path.replace("\\", "/"))
    if devices:
        yaml_text += "\ndevices:\n" + "".join(
            f"  - id: {d['id']}\n    mac: {d['mac']}\n    token: {d['token']}\n"
            for d in devices)
    Path(config_path).write_text(yaml_text, encoding="utf-8")

    prev = {k: os.environ.get(k) for k in ("CUKTECH_CONFIG_PATH", "CUKTECH_HISTORY_DB_PATH")}
    os.environ["CUKTECH_CONFIG_PATH"] = config_path
    os.environ["CUKTECH_HISTORY_DB_PATH"] = db_path

    holder: dict[str, RecordingMQTTClient | None] = {"client": None}

    def _factory(*args, **kwargs):
        client = RecordingMQTTClient(*args, **kwargs)
        holder["client"] = client
        return client

    patches = []
    if not real_start:
        # 冻结墙钟是 golden 基线可对比的前提。虚拟充电器反过来：推送/会话/落库都
        # 依赖时间推进（"距上次采样 1s 以上才写"这类节流），冻结时间会让历史永远为空。
        patches.append(patch("time.time", return_value=FROZEN_NOW))
    if not real_start:
        # 默认把 BLE 主循环换成"永不连接"的桩：golden 基线只关心对外行为，
        # 不想跑真扫描。虚拟充电器场景反过来——必须真跑，否则测不到注入点。
        patches.append(patch.object(bm_module.BLEManager, "start", _ble_start_stub))
    if mqtt_enabled:
        patches.append(patch("paho.mqtt.client.Client", _factory))

    for p in patches:
        p.start()
    ha_server.reset_server()
    ha_server._static_cache.clear()

    env: GoldenEnv | None = None
    try:
        server = (ha_server.Server(controller_factory=controller_factory)
                  if controller_factory is not None else ha_server.get_server())
        ha_server._server = server
        if not mqtt_enabled:
            server.config.mqtt.enabled = False
        # 启动错峰（§3.4）在生产默认 2s/台；测试里归零，否则每加一台设备就多睡 2s，
        # 而且会让"启动即断言"的用例变成时序赌博。
        server.config.server.startup_stagger_sec = 0.0
        server.adapter.startup_stagger_sec = 0.0
        env = GoldenEnv(server, config_path, db_path, None)
        if apply_startup:
            await ha_server.on_startup(None)
            env.mqtt = holder["client"]
        yield env
    finally:
        if env is not None and apply_startup:
            with contextlib.suppress(Exception):
                await ha_server.on_shutdown(None)
        for p in patches:
            with contextlib.suppress(Exception):
                p.stop()
        ha_server.reset_server()
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with contextlib.suppress(Exception):
            for name in os.listdir(tmp):
                os.unlink(os.path.join(tmp, name))
            os.rmdir(tmp)


# ── golden 记录 / 对比 ──

def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def update_requested() -> bool:
    return os.environ.get("CUKTECH_GOLDEN_UPDATE") == "1"


def write_golden(name: str, value) -> Path:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    p = golden_path(name)
    if isinstance(value, str):
        value = {"raw": value}
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def diff_report(expected, actual, limit: int = 20) -> list[str]:
    """人类可读的逐字节差异（快照对比失败时唯一的排障线索，值得写好）。"""
    lines: list[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in sorted(set(expected) | set(actual)):
            e, a = expected.get(k, "<缺失>"), actual.get(k, "<缺失>")
            if e != a:
                lines.append(f"  [{k}] 期望={e!r} 实际={a!r}")
                if isinstance(e, dict) and isinstance(a, dict):
                    lines.extend("  " + s for s in diff_report(e, a, limit=limit - len(lines)))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            lines.append(f"  长度不同：期望 {len(expected)}，实际 {len(actual)}")
        for i in range(min(len(expected), len(actual))):
            if expected[i] != actual[i]:
                lines.append(f"  [{i}] 期望={expected[i]!r} 实际={actual[i]!r}")
    else:
        e, a = repr(expected), repr(actual)
        if e != a:
            lines.append(f"  期望={e}")
            lines.append(f"  实际={a}")
    return lines[:limit]


def check_golden(name: str, value, *, normalize=None) -> None:
    """无排除项的逐字节基线对比；`CUKTECH_GOLDEN_UPDATE=1` 时写基线。

    `normalize` 只允许处理 `golden/timestamp_fields.md` 白名单内的时间戳字段，
    且必须传回同样的结构（不裁剪键）。
    """
    if isinstance(value, str):
        value = {"raw": value}
    if normalize is not None:
        value = normalize(value)
    p = golden_path(name)
    if update_requested():
        written = write_golden(name, value)
        print(f"[golden] 已记录 {written.name}")
        return
    if not p.exists():
        raise AssertionError(
            f"golden 基线缺失：{p}（用 CUKTECH_GOLDEN_UPDATE=1 pytest 生成后再提交）")
    expected = json.loads(p.read_text(encoding="utf-8"))
    if expected != value:
        raise AssertionError(
            f"golden 快照不一致：{name}\n" + "\n".join(diff_report(expected, value)))


def normalize_uptime(doc: dict) -> dict:
    """把 uptime_ms 换成哨兵（唯一允许的时间戳类排除，见 timestamp_fields.md）。"""
    out = dict(doc)
    if "uptime_ms" in out:
        out["uptime_ms"] = "<UPTIME_MS>"
    return out


def json_body(response) -> dict:
    """从 aiohttp Response 取 JSON 文本（保留原串以便逐字节比对）。"""
    raw = response.body
    if raw is None:
        raise AssertionError("handler 未产生 body（生产路径变化？）")
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    return json.loads(raw)


def raw_body(response) -> str:
    raw = response.body
    if raw is None:
        raise AssertionError("handler 未产生 body（生产路径变化？）")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8")
    return str(raw)


def mock_request(method: str = "GET", path: str = "/", **kwargs) -> Any:
    from aiohttp.test_utils import make_mocked_request
    return make_mocked_request(method, path, **kwargs)
