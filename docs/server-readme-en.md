# CUKTECH 10 GaN Charger Ultra - BLE Server

> **[中文](README.md)**

Standalone BLE server for connecting to CUKTECH chargers and pushing real-time data to Home Assistant via MQTT.

## Features

- **BLE Connection & MiOT Authentication**: Auto-connect to charger with automatic reconnection
- **Real-time Data Push**: Publish voltage, current, power, protocol via MQTT
- **Protocol Detection**: Auto-detect PD / PD Fixed / PD PPS / QC / USB-A charging protocols
- **Web UI**: Real-time power charts, port control, device settings, 6 themes
- **HTTP API**: RESTful interface for external systems
- **CORS Support**: Cross-origin requests configured, embeddable in HA html-card or iframe

## Requirements

- Python 3.10+
- Linux (Bluetooth adapter required)
- MQTT Broker (e.g. EMQX, Mosquitto)

## Quick Start

### 1. Get Device Token

Use [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) to extract from Mi Cloud:

```bash
pip install xiaomi_cloud_tokens_extractor
python -m xiaomi_cloud_tokens_extractor
```

Select your CUKTECH charger and get:
- `MAC` - Bluetooth MAC address (e.g. `3C:CD:73:34:AE:59`)
- `Token` - Device token (12-byte hex)
- `BLE Key` - BLE authentication key (16-byte hex)

### 2. Install

```bash
git clone https://github.com/kairui1108/cuktech-ble-server.git
cd cuktech-ble-server

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Configure

Copy `config.yaml.example` to `config.yaml` and fill in your values:

```yaml
ble:
  mac: "3C:CD:73:34:AE:59"
  token: "your_token_here"
  ble_key: "your_ble_key_here"

mqtt:
  host: "192.168.1.63"
  port: 1883
  username: "admin"
  password: "your_password"
  keepalive: 60
  topic_prefix: "cuktech/charger"

server:
  host: "0.0.0.0"
  port: 8199
  command_timeout: 10.0
  reconnect_delay: 5.0
  settings_refresh_interval: 60.0
```

Or use environment variables (higher priority than config.yaml):

```bash
export CUKTECH_DEVICE_MAC="3C:CD:73:34:AE:59"
export CUKTECH_DEVICE_TOKEN="your_token"
export CUKTECH_DEVICE_BLE_KEY="your_ble_key"
export MQTT_HOST="192.168.1.63"
export MQTT_PORT="1883"
export MQTT_USER="admin"
export MQTT_PASS="your_password"
```

### 4. Start

```bash
./cuktech_ctl.sh start
```

## Web UI

Access at `http://<server-ip>:8199/`

### Features

- **Device Info**: Connection status, BLE control, total power, max voltage
- **Power Chart**: Chart.js real-time line chart for port and total power trends
- **Port Monitor**: C1/C2/C3/A independent control with voltage, current, power, protocol
- **Device Settings**: Scene mode, screen timeout, language dropdowns
- **Countdown**: Quick buttons + custom minute input

### Themes

6 themes: Dark, Deep Blue, Ocean, Gray, Light, Follow System

## MQTT Topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `cuktech/charger/port/c1` | Publish | C1 port data (JSON) |
| `cuktech/charger/port/c2` | Publish | C2 port data (JSON) |
| `cuktech/charger/port/c3` | Publish | C3 port data (JSON) |
| `cuktech/charger/port/a` | Publish | A port data (JSON) |
| `cuktech/charger/settings` | Publish | Device settings (retained) |
| `cuktech/charger/status` | Publish | Connection status |
| `cuktech/charger/set` | Subscribe | Settings command |
| `cuktech/charger/port` | Subscribe | Port control command |

Port data format:
```json
{
  "voltage": 20.1,
  "current": 2.5,
  "power": 50.25,
  "protocol": "PD",
  "active": true
}
```

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Get full charger status |
| `/api/enable` | POST | Enable/disable BLE `{"enabled": true/false}` |
| `/api/set` | POST | Set PIID value `{"piid": N, "value": V}` |
| `/api/port` | POST | Control port on/off `{"port": "c1", "on": true}` |
| `/api/countdown` | POST | Set countdown `{"port": "c1", "minutes": 30}` |

## Service Management

```bash
./cuktech_ctl.sh start     # Start
./cuktech_ctl.sh stop      # Stop
./cuktech_ctl.sh restart   # Restart
./cuktech_ctl.sh status    # Status
./cuktech_ctl.sh log       # View logs
```

### Auto-start on Boot

**Option A: systemd (Recommended)**

```bash
cd systemd && ./install-service.sh
```

**Option B: crontab**

```bash
@reboot /path/to/cuktech_ctl.sh start
```

## Architecture

```
CUKTECH Charger ←BLE→ BLE Server ←MQTT→ Home Assistant
                          ↓
                     Web UI / HTTP API
```

BLE Server runs as a standalone process, connects to the charger via BLE, and pushes data to Home Assistant via MQTT. Web UI and HTTP API are served by the same process.

## Protocol Support

| Protocol | Description |
|----------|-------------|
| PD | USB Power Delivery |
| PD Fixed | PD fixed voltage (5/9/12/15/20V) |
| PD PPS | PD Programmable Power Supply |
| QC | Quick Charge |
| USB-A | USB-A charging (DCP) |
| idle | No device connected |

## Acknowledgments

- [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) - BLE protocol reference implementation
- [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) - Protocol detection reference
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) - Xiaomi device token extractor
- [bleak](https://github.com/hbldh/bleak) - BLE communication library
- [paho-mqtt](https://eclipse.dev/paho/) - MQTT client

## License

MIT License
