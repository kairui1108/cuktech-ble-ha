# CUKTECH 10 GaN Charger Ultra - BLE Server

> **[中文](README.md)**

Standalone BLE server for connecting CUKTECH chargers and pushing real-time data to Home Assistant via MQTT.

## Features

- **BLE Connection & MiOT Auth**: Auto-connect charger with reconnection support
- **BLE Stability**: LL disconnect confirmation, GATT ready wait, exponential backoff
- **Real-time Data**: SSE event stream pushes port data and status to Web frontend; MQTT push to HA
- **SSE Event Stream**: Server-Sent Events push real-time port updates, status changes, and setting changes — replaces 2s polling
- **Protocol Detection**: Auto-detect PD / PD Fixed / PD PPS / QC / USB-A
- **Web UI**: Real-time charts, port control, settings, Bemfa toggle, 6 themes
- **Web Config Page**: Online configuration via `config.html`, Xiaomi Cloud QR login for auto device setup
- **HTTP API**: RESTful endpoints for external systems
- **MQTT LWT**: Auto-notify HA on crash
- **Bemfa Cloud**: XiaoAi / DuerOS voice control for charger ports
- **Charge Sessions & Energy Stats**: Auto-record charge sessions (duration, energy Wh, peak power), view history via Web UI
- **Energy Integration (Wh)**: Real-time trapezoidal energy accumulation per port
- **SQLite History**: Persistent port data with statistics and CSV export
- **BLE Connection Quality**: Real-time scoring (0-100) with decrypt rate, notification response, stability metrics
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

## Quick Start

### Option A: Web Config Page (Easiest)

no manual config file editing needed:

```bash
git clone https://github.com/kairui1108/cuktech-ble-ha.git
cd cuktech-ble-ha/ble_server
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
cp config.yaml.example config.yaml
./cuktech_ctl.sh start
```

Then open `http://<server-ip>:8199/config.html`:
1. Click "Get QR Code" → scan with Mi Home app → auto-fill MAC/Token/BLE Key
2. Fill in MQTT settings (for Home Assistant)
3. Click "Save & Restart"

### Option B: Docker

#### Run with persistent data directory (recommended):

```bash
docker run -d \
  --name cuktech-ble --network host --privileged --restart unless-stopped \
  -v $(pwd)/data:/data \
  -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro \
  -e CUKTECH_CONFIG_PATH=/data/config.yaml \
  -e CUKTECH_HISTORY_DB_PATH=/data/port_history.db \
  ghcr.io/kairui1108/cuktech-ble-server:latest
# Then open http://<server-ip>:8199/config.html to configure via web UI
# All config changes are saved to ./data/config.yaml and persist across restarts
```
```

### Docker Compose

```bash
# Option 1: Clone repo and copy template
git clone https://github.com/kairui1108/cuktech-ble-ha.git
cd kuktech-ble-ha/ble_server
cp config.yaml.example config.yaml

# Option 2: Create config directly (no clone needed)
cat > config.yaml << 'EOF'
ble:
  mac: "XX:XX:XX:XX:XX:XX"
  token: ""
  ble_key: ""
mqtt:
  enabled: true
  host: ""
  port: 1883
server:
  port: 8199
  settings_refresh_interval: 10.0
EOF

# Start
docker compose up -d
```

> **Warning**: If `config.yaml` does not exist when Docker Compose starts, Docker will create a **directory** instead of a file. Use `cp` or `cat` to create a valid file with defaults.

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

**Option A: Web Config Page (Recommended)**

After starting the server, open `http://<server-ip>:8199/config.html` and use the Xiaomi Cloud QR login to auto-fetch MAC, Token, and BLE Key.

**Option B: Manual**

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

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `http://<IP>:8199/` | Real-time power, port control, protocol control |
| Mobile | `http://<IP>:8199/phone.html` | Mobile-optimized view |
| Config | `http://<IP>:8199/config.html` | Online config, Xiaomi Cloud QR login |

- **SSE real-time push**: Port data, status, quality scores delivered via SSE event stream
- Real-time power charts (Chart.js)
- Port control (C1/C2/C3/A)
- BLE connection quality metrics (hover BLE badge)
- Bemfa custom device names
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
| `/api/events` | GET | SSE event stream (port data, status, settings, quality) |
| `/api/status` | GET | Full charger status |
| `/api/enable` | POST | Enable/disable BLE `{"enabled": true/false}` |
| `/api/set` | POST | Set PIID value `{"piid": N, "value": V}` |
| `/api/port` | POST | Control port `{"port": "c1", "action": "on/off"}` |
| `/api/protocol` | POST | Control protocol switches |
| `/api/config` | GET/POST | Read/save configuration |
| `/api/xiaomi/login` | POST | Xiaomi Cloud QR login |
| `/api/xiaomi/qr/complete` | POST | Complete QR login |
| `/api/xiaomi/beaconkey` | POST | Get BLE Key |
| `/api/log-level` | GET/POST | Log level management |
| `/api/bemfa` | GET | Bemfa status |
| `/api/chart` | GET | Chart data |
| `/api/sessions` | GET | Charge sessions |
| `/api/energy/stats` | GET | Energy statistics |

## Tests

```bash
.venv/bin/python -m pytest tests/ -v  # 240+ tests
```

## Known Limitations

- **Single Device**: Current architecture supports only one charger at a time. Multi-device support is planned for future releases.
- **Protocol Detection**: Protocol is derived from the authoritative firmware push (PIID 17/18, consistent with the Xiaomi Home app) and updates immediately on PD/PPS change — no periodic 60s refresh lag. A coarse voltage-based inference is used only in an extreme cold start (when PIID 17 is never received and its read fails).
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
