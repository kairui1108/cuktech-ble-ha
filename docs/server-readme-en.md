# CUKTECH 10 GaN Charger Ultra - BLE Server

> **[中文](README.md)**

Standalone BLE server for connecting CUKTECH chargers and pushing real-time data to Home Assistant via MQTT.

## Features

- **BLE Connection & MiOT Auth**: Auto-connect charger with reconnection support
- **BLE Stability**: LL disconnect confirmation, GATT ready wait, exponential backoff
- **Real-time Data**: MQTT publish voltage, current, power, protocol per port
- **Protocol Detection**: Auto-detect PD / PD Fixed / PD PPS / QC / USB-A
- **Web UI**: Real-time charts, port control, settings, Bemfa toggle, 6 themes
- **HTTP API**: RESTful endpoints for external systems
- **MQTT LWT**: Auto-notify HA on crash
- **Bemfa Cloud**: XiaoAi / DuerOS voice control for charger ports
- **SQLite History**: Persistent port data with statistics and CSV export
- **Environment Check**: `check_env.sh` for system compatibility

## Requirements

### Docker
- Linux with Bluetooth adapter
- Docker + Docker Compose

### Native
- Python 3.10+
- Linux with Bluetooth adapter
- BlueZ 5.66+ (5.71 recommended)
- MQTT Broker (EMQX, Mosquitto, etc.)

## Docker (Recommended)

### Pull and run

```bash
# 1. Create config file
cat > config.yaml << EOF
ble:
  mac: "XX:XX:XX:XX:XX:XX"
  token: "your_token_12bytes_hex"
  ble_key: "your_ble_key_16bytes_hex"
mqtt:
  # Set to true to enable MQTT (for Home Assistant integration), false to run as standalone web server
  enabled: true
  host: ""
  port: 1883
  username: ""
  password: ""
  keepalive: 60
  topic_prefix: "cuktech/charger"

server:
  host: "0.0.0.0"
  port: 8199
  command_timeout: 10.0
  reconnect_base_delay: 1.0
  reconnect_max_delay: 300.0
  settings_refresh_interval: 60.0
  log_level: "error"
  history_retention_days: 2
  history_db_path: ""
EOF

# 2. Run container
docker run -d \
  --name cuktech-ble \
  --network host \
  --privileged \
  --restart unless-stopped \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/data:/data \
  -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro \
  -e CUKTECH_HISTORY_DB_PATH=/data/port_history.db \
  ghcr.io/kairui1108/cuktech-ble-server:latest

# 3. Check logs
docker logs -f cuktech-ble
```

### Docker Compose pull & run (recommended)

```bash
git clone https://github.com/kairui1108/cuktech-ble-ha.git
cd cuktech-ble-ha

# edit config, fill in your device info
vim ble_server/docker/docker-compose.pull.yml

# pull image and start (no local build needed)
docker compose -f ble_server/docker/docker-compose.pull.yml up -d
```

### Build locally

```bash
cd ble_server
# use config file to run, edit config.yaml with your device info
cp config.yaml.example config.yaml
docker compose -f docker/docker-compose.yml up -d

# use env file to run, edit docker/docker-compose.env.yml with your device info
docker compose -f docker/docker-compose.env.yml up -d
```

### Notes on Bluetooth

- Container uses `--network host` to share host network
- Host D-Bus socket is mounted for BlueZ access
- `--privileged` is required for BLE hardware access
- Other Bluetooth applications on host are unaffected

## Quick Start (Native)

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
| `cuktech/charger/port/{c1,c2,c3,a}` | Publish | Port data (JSON, retain) |
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
| `/api/bemfa` | GET/POST | Bemfa status & toggle control |

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

## Known Limitations

- **Single Device**: Current architecture supports only one charger at a time. Multi-device support is planned for future releases.
- **Protocol Detection**: Charging protocol identification (PD/QC/USB-A etc.) is inferred from port voltage and PDO data, and may not always match the actual protocol.
- **Platform Support**: Development and testing are done exclusively on Linux. Compatibility with other platforms (macOS, Windows) has not been verified — use at your own risk.

## Protocol Support

| Protocol | Description |
|----------|-------------|
| 5V | USB 5V |
| PD | USB Power Delivery |
| PPS | PD Programmable Power Supply |
| QC | Quick Charge |
| AFC | Samsung Adaptive Fast Charging |
| FCP | Huawei Fast Charge Protocol |
| SCP | Huawei Super Charge Protocol |
| UFCS | Universal Fast Charging Specification |
| idle | No device connected |

## License

MIT License
