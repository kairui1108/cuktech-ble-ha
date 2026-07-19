"""CUKTECH BLE Server - Configuration management.

Supports YAML config file and environment variables.
YAML file takes precedence over environment variables.
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _load_yaml_config():
    """Load config from YAML file if exists."""
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        config_path = Path.cwd() / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return {}


@dataclass
class BLEConfig:
    mac: str = ""
    token: str = ""
    ble_key: str = ""
    scan_timeout: int = 10

    def __post_init__(self):
        if not self.mac or self.mac == "XX:XX:XX:XX:XX:XX":
            raise ValueError("CUKTECH_DEVICE_MAC 未配置，请设置环境变量或 config.yaml")
        if not self.token:
            raise ValueError("CUKTECH_DEVICE_TOKEN 未配置，请设置环境变量或 config.yaml")


@dataclass
class MQTTConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    keepalive: int = 60
    topic_prefix: str = "cuktech/charger"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8199
    command_timeout: float = 10.0
    settings_refresh_interval: float = 60.0
    log_level: str = "info"
    history_retention_days: int = 2
    history_db_path: str = "port_history.db"
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 300.0


@dataclass
class BemfaConfig:
    enabled: bool = False
    uid: str = ""


@dataclass
class Config:
    ble: BLEConfig = field(default_factory=BLEConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    bemfa: BemfaConfig = field(default_factory=BemfaConfig)

    @property
    def topic_port(self):
        return f"{self.mqtt.topic_prefix}/port"

    @property
    def topic_settings(self):
        return f"{self.mqtt.topic_prefix}/settings"

    @property
    def topic_status(self):
        return f"{self.mqtt.topic_prefix}/status"


def load_config() -> Config:
    """Load config from YAML file, then override with environment variables."""
    ycfg = _load_yaml_config()

    ble_cfg = ycfg.get("ble", {})
    mqtt_cfg = ycfg.get("mqtt", {})
    server_cfg = ycfg.get("server", {})

    ble = BLEConfig(
        mac=os.environ.get("CUKTECH_DEVICE_MAC", ble_cfg.get("mac", "")),
        token=os.environ.get("CUKTECH_DEVICE_TOKEN", ble_cfg.get("token", "")),
        ble_key=os.environ.get("CUKTECH_DEVICE_BLE_KEY", ble_cfg.get("ble_key", "")),
        scan_timeout=ble_cfg.get("scan_timeout", 10),
    )

    try:
        mqtt_port = int(os.environ.get("MQTT_PORT", mqtt_cfg.get("port", 1883)))
    except (ValueError, TypeError) as e:
        raise ValueError(f"MQTT port must be an integer: {e}")

    _mqtt_enabled_env = os.environ.get("MQTT_ENABLED", "").lower()
    mqtt_enabled = _mqtt_enabled_env in ("1", "true", "yes") or mqtt_cfg.get("enabled", False)

    mqtt = MQTTConfig(
        enabled=mqtt_enabled,
        host=os.environ.get("MQTT_HOST", mqtt_cfg.get("host", "localhost")),
        port=mqtt_port,
        username=os.environ.get("MQTT_USER", mqtt_cfg.get("username", "")),
        password=os.environ.get("MQTT_PASS", mqtt_cfg.get("password", "")),
        keepalive=mqtt_cfg.get("keepalive", 60),
        topic_prefix=os.environ.get("MQTT_TOPIC_PREFIX", mqtt_cfg.get("topic_prefix", "cuktech/charger")),
    )

    try:
        history_retention = int(os.environ.get("CUKTECH_HISTORY_RETENTION_DAYS", server_cfg.get("history_retention_days", 2)))
    except (ValueError, TypeError) as e:
        raise ValueError(f"History retention days must be an integer: {e}")
    try:
        reconnect_base_delay = float(server_cfg.get("reconnect_base_delay", 1.0))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Reconnect base delay must be a number: {e}")
    try:
        reconnect_max_delay = float(server_cfg.get("reconnect_max_delay", 300.0))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Reconnect max delay must be a number: {e}")

    server = ServerConfig(
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(os.environ.get("CUKTECH_SERVER_PORT", server_cfg.get("port", 8199))),
        command_timeout=server_cfg.get("command_timeout", 10.0),
        settings_refresh_interval=server_cfg.get("settings_refresh_interval", 60.0),
        log_level=os.environ.get("CUKTECH_LOG_LEVEL", server_cfg.get("log_level", "info")),
        history_retention_days=history_retention,
        history_db_path=os.environ.get("CUKTECH_HISTORY_DB_PATH", server_cfg.get("history_db_path", "port_history.db")),
        reconnect_base_delay=reconnect_base_delay,
        reconnect_max_delay=reconnect_max_delay,
    )

    bemfa_cfg = ycfg.get("bemfa", {})
    bemfa_enabled_env = os.environ.get("BEMFA_ENABLED", "").lower()
    bemfa_enabled = bemfa_enabled_env in ("1", "true", "yes") or bemfa_cfg.get("enabled", False)
    bemfa = BemfaConfig(
        enabled=bemfa_enabled,
        uid=os.environ.get("BEMFA_UID", bemfa_cfg.get("uid", "")),
    )

    return Config(ble=ble, mqtt=mqtt, server=server, bemfa=bemfa)
