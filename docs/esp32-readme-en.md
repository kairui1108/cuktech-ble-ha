# CUKTECH Charger BLE Bridge - ESP32 Firmware

[中文](esp32-readme.md)

Bridge your CUKTECH charger to Home Assistant via ESP32.

```
Charger ←BLE→ ESP32 ──MQTT──→ Home Assistant
                         ├── HTTP (Config page / Dashboard / OTA)
                         └── NVS (Persistent config storage)
```

## Home Assistant Integration

This firmware provides the ESP32 BLE gateway only. To view and control the charger in Home Assistant, install the companion integration:

👉 **[cuktech-ble-ha-integration](https://github.com/kairui1108/cuktech-ble-ha-integration)**

Features:
- Real-time port data (voltage / current / power)
- Port on/off control
- Protocol switching (PD / PPS / UFCS / SCP)
- Scene mode selection
- BLE connection status monitoring

## Quick Start

### Hardware

- ESP32 / ESP32-S3 / ESP32-C3
- Any model works — no external BLE adapter needed

### 1. Flash Firmware

Download pre-built firmware from [Releases](https://github.com/kairui1108/cuktech-ble-ha/releases), or build from source:

```bash
# Build from source (requires ESP-IDF v5.3)
cd esp32_ble
idf.py set-target esp32
idf.py build
idf.py -p /dev/ttyUSB0 flash
```

### 2. First-Time Setup

After flashing, the ESP32 boots into **AP configuration mode**:

1. Connect to WiFi `CUKTECH-Setup` (password: `12345678`)
2. Open browser to `http://192.168.4.1/`
3. Enter: WiFi credentials, MQTT broker, device BLE credentials
4. Save — the device reboots and connects automatically

### 3. Obtain Device Credentials

You need the following from your charger (via Xiaomi Home / MiJia app):

- **MAC address** (6-byte hex, e.g. `3c:cd:73:xx:xx:xx`)
- **Token** (12-byte hex)
- **BLE Key** (16-byte hex)

> Use [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) to extract them.

## Web Dashboard

Once connected, open the ESP32's IP address in a browser:

- Real-time voltage / current / power per port
- Port on/off control
- Protocol switching (PD / PPS / UFCS / SCP)
- Scene mode selection
- BLE connection toggle
- OTA firmware update

## File Structure

```
esp32_ble/
├── CMakeLists.txt         # ESP-IDF build entry
├── main/
│   ├── main.c             # WiFi / MQTT / HTTP / OTA / task scheduling
│   ├── ble_manager.c/.h   # BLE state machine + auth + async commands
│   ├── miot_auth.c/.h     # MiOT authentication & encryption
│   ├── miot_protocol.c/.h # Protocol constants & TLV encoding
│   ├── config.h           # Compile-time defaults
│   ├── config_store.c/.h  # NVS config persistence
│   ├── http_server.c/.h   # Web config page + REST API
│   ├── ota_update.c/.h    # HTTP OTA update
│   └── queue_msg.h        # Cross-task message types
├── partitions.csv         # Partition table (3MB app + OTA)
├── sdkconfig.defaults     # ESP-IDF default config
└── sdkconfig              # Local sdkconfig (gitignored)
```

## Build Instructions

### Prerequisites

- [ESP-IDF v5.3](https://docs.espressif.com/projects/esp-idf/en/v5.3/esp32/get-started/index.html)

### Build

```bash
idf.py set-target esp32    # or esp32s3 / esp32c3
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

### Multi-Platform

Supports `set-target esp32s3` and `esp32c3`. C3 is single-core — tasks automatically adapt.

## License

MIT

## Tech Stack

| Component | Description |
|---|---|
| **ESP-IDF v5.3** | Framework |
| **NimBLE** | BLE stack (Central) |
| **ESP-MQTT** | MQTT client (QoS 1, retain) |
| **lwIP** | TCP/IP stack + HTTP Server |
| **mbedTLS** | AES-CCM / SHA256 / HKDF |
| **cJSON** | JSON encoder/decoder |
