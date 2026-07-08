"""CUKTECH BLE Server - Configuration management.

Supports YAML config file and environment variables.
YAML file takes precedence over environment variables.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path


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

    def __post_init__(self):
        if not self.mac or self.mac == "XX:XX:XX:XX:XX:XX":
            raise ValueError("CUKTECH_DEVICE_MAC 未配置，请设置环境变量或 config.yaml")
        if not self.token:
            raise ValueError("CUKTECH_DEVICE_TOKEN 未配置，请设置环境变量或 config.yaml")


@dataclass
class MQTTConfig:
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
    reconnect_delay: float = 5.0
    settings_refresh_interval: float = 60.0


@dataclass
class Config:
    ble: BLEConfig = field(default_factory=BLEConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

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
    )

    mqtt = MQTTConfig(
        host=os.environ.get("MQTT_HOST", mqtt_cfg.get("host", "localhost")),
        port=int(os.environ.get("MQTT_PORT", mqtt_cfg.get("port", 1883))),
        username=os.environ.get("MQTT_USER", mqtt_cfg.get("username", "")),
        password=os.environ.get("MQTT_PASS", mqtt_cfg.get("password", "")),
        keepalive=mqtt_cfg.get("keepalive", 60),
        topic_prefix=mqtt_cfg.get("topic_prefix", "cuktech/charger"),
    )

    server = ServerConfig(
        host=server_cfg.get("host", "0.0.0.0"),
        port=server_cfg.get("port", 8199),
        command_timeout=server_cfg.get("command_timeout", 10.0),
        reconnect_delay=server_cfg.get("reconnect_delay", 5.0),
        settings_refresh_interval=server_cfg.get("settings_refresh_interval", 60.0),
    )

    return Config(ble=ble, mqtt=mqtt, server=server)
