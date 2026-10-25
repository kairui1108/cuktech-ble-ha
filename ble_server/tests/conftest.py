"""Shared fixtures for CUKTECH BLE Server tests."""
import asyncio
import os
import sqlite3
import tempfile
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# ha_server 在 **导入时** 就会按 CUKTECH_LOG_FILE 建立轮转文件处理器；测试期间
# 必须指向临时文件，否则会把测试日志写进生产日志（/tmp/cuktech_server.log）。
# 这行必须在任何 `import ha_server` 之前执行。
os.environ["CUKTECH_LOG_FILE"] = os.path.join(
    tempfile.gettempdir(), "cuktech_server_test.log")

# 同理（更隐蔽、真出过事）：配置类 handler 会**真的写盘**——`/api/log-level` 的
# POST 会把级别写回 `_config_path()`，`/api/config`、bemfa modified 回写也一样。
# 默认路径是 ble_server/config.yaml（**生产配置**），于是"跑一次测试"就把生产的
# 日志级别改成 debug，下次重启服务就会莫名其妙变成 debug。
# 这里指向测试专用文件并**先建好**：`_load_yaml_config` 在文件不存在时会回退到
# `Path.cwd()/config.yaml`，那又打回生产文件了。
_TEST_CONFIG_PATH = os.path.join(tempfile.gettempdir(), "cuktech_config_test.yaml")
os.environ["CUKTECH_CONFIG_PATH"] = _TEST_CONFIG_PATH
Path(_TEST_CONFIG_PATH).write_text("server:\n  log_level: info\n", encoding="utf-8")

from history import PortHistory


@pytest.fixture(autouse=True)
def mock_subprocess():
    """Prevent any test from calling real subprocess (hciconfig, bluetoothctl, etc.)."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    with patch("asyncio.create_subprocess_exec", return_value=AsyncMock(return_value=mock_proc)):
        yield


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def history(temp_db):
    """Create a PortHistory instance with temporary database."""
    h = PortHistory(db_path=temp_db, retention_days=2)
    h.connect()
    yield h
    h.close()


@pytest.fixture
def mock_ble_data():
    """Sample BLE port data."""
    return {
        "voltage": 20.1,
        "current": 2.5,
        "power": 50.25,
        "active": True,
        "protocol": "PD",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 多设备测试脚手架（设计文档 docs/plans/2026-10-23-multi-device-architecture.md
# §7 P0："make_config() 多设备测试脚手架"）
#
# P0 阶段先把**形状**定下来：一台 primary（老 `ble:` 节语法糖）+ N 台
# `devices:` 附加设备。P1 的 `load_config()` 必须能解这里生成的 YAML，且
# `DeviceRegistry` 必须按这里的顺序取 primary —— 所以这个 helper 是两份契约的
# 单一来源：既给测试用，也给"配置解析/注册表构建"测试当输入。
#
# 注意 `tests/test_ble_manager.py` 里另有一个同名 `make_config()`，那是 MagicMock
# （会吸收任意属性）。它保持模块局部、不改名，因为 200+ 用例依赖它给 config 随意
# 赋 `topic_*`（真 Config 上是只读 property，赋值会炸）。新写多设备用例请用这里的
# 版本：真对象、真类型错误，才是装配完整性测试要的强度。
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PRIMARY_MAC = "AA:BB:CC:DD:EE:01"
DEFAULT_PRIMARY_TOKEN = "00112233445566778899aabbccddeeff"
DEFAULT_TOPIC_PREFIX = "cuktech/charger"
# 追加设备的凭据必须各不相同且是合法 hex（BLEManager.__init__ 会 bytes.fromhex）
EXTRA_DEVICE_TOKEN = "ffeeddccbbaa99887766554433221100"


def make_device(device_id: str, *, mac: str | None = None, token: str | None = None,
                name: str | None = None, topic_prefix: str = "", **extra) -> dict:
    """一台 `devices:` 节的附加设备（§3.10 字段）。

    `topic_prefix=""` 表示"自动 `{mqtt.topic_prefix}/{id}`"——P1 校验规则要覆盖
    空/显式/重叠三种情形，所以这里保留空串而不是替调用方补全。
    """
    dev = {
        "id": device_id,
        "name": name or device_id,
        "mac": mac or f"AA:BB:CC:DD:EE:{10 + len(device_id):02X}",
        "token": token or EXTRA_DEVICE_TOKEN,
        "topic_prefix": topic_prefix,
    }
    dev.update(extra)
    return dev


def make_config_yaml(*, ble: dict | None = None, devices: list[dict] | None = None,
                    mqtt: dict | None = None, server: dict | None = None,
                    bemfa: dict | None = None) -> str:
    """生成 config.yaml 文本。`devices=None` ⇒ 不出现 `devices:` 节（单设备形态）。"""
    ble = ble or {"mac": DEFAULT_PRIMARY_MAC, "token": DEFAULT_PRIMARY_TOKEN,
                  "ble_key": "", "scan_timeout": 10}
    mqtt = mqtt or {"enabled": True, "host": "127.0.0.1", "port": 1883,
                    "topic_prefix": DEFAULT_TOPIC_PREFIX}
    server = server or {"host": "127.0.0.1", "port": 8199, "log_level": "warning",
                        "history_retention_days": 30, "history_db_path": "port_history.db"}
    lines = ["ble:"]
    lines += [f"  {k}: {_yaml_scalar(v)}" for k, v in ble.items()]
    lines.append("")
    lines.append("mqtt:")
    lines += [f"  {k}: {_yaml_scalar(v)}" for k, v in mqtt.items()]
    lines.append("")
    lines.append("server:")
    lines += [f"  {k}: {_yaml_scalar(v)}" for k, v in server.items()]
    if bemfa:
        lines.append("")
        lines.append("bemfa:")
        lines += [f"  {k}: {_yaml_scalar(v)}" for k, v in bemfa.items()]
    if devices is not None:
        # 空列表也要写出节：P1 的校验规则区分"节缺失"（单设备）与"节为空"（非法）。
        lines.append("")
        if devices:
            lines.append("devices:")
            for dev in devices:
                lines.append(f"  - id: {_yaml_scalar(dev['id'])}")
                for k, v in dev.items():
                    if k == "id":
                        continue
                    lines.append(f"    {k}: {_yaml_scalar(v)}")
        else:
            lines.append("devices: []")
    return "\n".join(lines) + "\n"


def _yaml_scalar(value) -> str:
    if isinstance(value, (list, tuple)):
        # 列表走 flow 风格：`allowed_devices: [default, desk]`。渲染成带引号的字符串
        # 会让测试脚手架"看起来配了"、实际解析成一段文本（比不写更坏）。
        return "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return '""'
    return f'"{value}"'


def write_config(tmp_path, **kwargs) -> str:
    """把 `make_config_yaml` 的结果落到临时文件，返回路径（给 CUKTECH_CONFIG_PATH）。"""
    path = Path(tmp_path) / "config.yaml"
    path.write_text(make_config_yaml(**kwargs), encoding="utf-8")
    return str(path)


def make_config(*, ble: dict | None = None, devices: list[dict] | None = None,
                mqtt: dict | None = None, server: dict | None = None):
    """直接构造真 `Config`（不经 YAML），并挂上 `devices` 列表。

    P1 之前 `Config` 还没有 `devices` 字段：这里用属性赋值先给出形状，P1 把它变成
    dataclass 字段后这段代码仍然有效（值语义一致）。想要"真解析路径"的用请
    `make_config_yaml` + `load_config()`（`golden_harness.golden_env(devices=...)`）。
    """
    from config import BLEConfig, Config, MQTTConfig, ServerConfig

    ble = ble or {}
    mqtt = mqtt or {}
    server = server or {}
    cfg = Config(
        ble=BLEConfig(
            mac=ble.get("mac", DEFAULT_PRIMARY_MAC),
            token=ble.get("token", DEFAULT_PRIMARY_TOKEN),
            ble_key=ble.get("ble_key", ""),
            scan_timeout=ble.get("scan_timeout", 10),
        ),
        mqtt=MQTTConfig(
            enabled=mqtt.get("enabled", True),
            host=mqtt.get("host", "127.0.0.1"),
            port=mqtt.get("port", 1883),
            username=mqtt.get("username", ""),
            password=mqtt.get("password", ""),
            topic_prefix=mqtt.get("topic_prefix", DEFAULT_TOPIC_PREFIX),
        ),
        server=ServerConfig(
            host=server.get("host", "127.0.0.1"),
            port=server.get("port", 8199),
            log_level=server.get("log_level", "warning"),
            history_retention_days=server.get("history_retention_days", 30),
            history_db_path=server.get("history_db_path", "port_history.db"),
        ),
    )
    # P1 起 `devices` 是真 `DeviceConfig` 列表（不是裸 dict）：校验规则与 registry
    # 都消费这个类型，测试脚手架必须与生产同形，否则"通过了测试但配置解析会炸"。
    from config import DeviceConfig

    cfg.devices = [
        d if isinstance(d, DeviceConfig)
        else DeviceConfig.from_mapping(d, default_scan_timeout=cfg.ble.scan_timeout)
        for d in (devices or [])
    ]
    return cfg


@pytest.fixture
def config_factory():
    """`make_config` 的 fixture 形态（多设备用例可读性）。"""
    return make_config


@pytest.fixture
def device_configs(config_factory):
    """primary + 2 台附加设备：多设备用例的最小可用配置。"""
    return config_factory(devices=[make_device("desk"), make_device("bed")])

