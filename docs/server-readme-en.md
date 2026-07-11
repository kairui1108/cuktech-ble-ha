# CUKTECH 10 GaN Charger Ultra - BLE Server

> **[中文](README.md)**

Standalone BLE server for connecting CUKTECH chargers and pushing real-time data to Home Assistant via MQTT.

## Features

- **BLE Connection & MiOT Auth**: Auto-connect charger with reconnection support
- **BLE Stability**: LL disconnect confirmation, GATT ready wait, exponential backoff
- **Real-time Data**: MQTT publish voltage, current, power, protocol per port
- **Protocol Detection**: Auto-detect PD / PD Fixed / PD PPS / QC / USB-A
- **Web UI**: Real-time charts, port control, settings, 6 themes
- **HTTP API**: RESTful endpoints for external systems
- **MQTT LWT**: Auto-notify HA on crash
- **SQLite History**: Persistent port data with statistics and CSV export
- **Environment Check**: `check_env.sh` for system compatibility

## Requirements

- Python 3.10+
- Linux with Bluetooth adapter
- BlueZ 5.66+ (5.71 recommended)
- MQTT Broker (EMQX, Mosquitto, etc.)

## Quick Start

### 1. Get Device Token

```bash
pip install xiaomi_cloud_tokens_extractor
python -m xiaomi_cloud_tokens_extractor
```

### 2. Check Environment

```bash
./check_env.sh
```

### 3. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 4. Configure

```bash
cp config.yaml.example config.yaml
# Edit config.yaml with your settings
```

### 5. Start

```bash
./cuktech_ctl.sh start
```

## Service Management

```bash
./cuktech_ctl.sh start         # Start
./cuktech_ctl.sh stop          # Stop
./cuktech_ctl.sh restart       # Restart
./cuktech_ctl.sh status        # Status
./cuktech_ctl.sh log [n]       # Last n log lines
./cuktech_ctl.sh clear-log     # Clear log
./cuktech_ctl.sh clear-history # Clear history DB
```

## Web UI

Access at `http://<SERVER_IP>:8199/`

- Real-time power charts (Chart.js)
- Port control (C1/C2/C3/A)
- Device settings
- Log level management
- 6 theme options

## MQTT Topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `cuktech/porter/port/{c1,c2,c3,a}` | Publish | Port data (JSON) |
| `cuktech/charger/settings` | Publish | Settings (retain) |
| `cuktech/charger/status` | Publish | Connection status (retain + LWT) |
| `cuktech/charger/set` | Subscribe | Set command |
| `cuktech/charger/port` | Subscribe | Port control command |

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Full charger status |
| `/api/enable` | POST | Enable/disable BLE `{"enabled": true/false}` |
| `/api/set` | POST | Set PIID value `{"piid": N, "value": V}` |
| `/api/port` | POST | Control port `{"port": "c1", "action": "on/off"}` |
| `/api/chart` | GET | Chart data |
| `/api/history/{port}` | GET | History data |
| `/api/statistics/{port}` | GET | Statistics |
| `/api/export/{port}` | GET | CSV export |
| `/api/log-level` | GET/POST | Log level management |

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

## Known Limitations

- **Single Device**: Current architecture supports only one charger at a time. Multi-device support is planned for future releases.
- **Protocol Detection**: Charging protocol identification (PD/QC/USB-A etc.) is inferred from port voltage and PDO data, and may not always match the actual protocol.

## License

MIT License
