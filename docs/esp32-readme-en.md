# CUKTECH Charger BLE Bridge - ESP32 Firmware

[中文](esp32-readme.md)

Bridge your CUKTECH charger to Home Assistant and Bemfa cloud via ESP32. The ESP32 itself serves a full web UI — no external services required for setup, real-time monitoring, and control.

```
Charger ←BLE→ ESP32 ──MQTT──→ Home Assistant
         │            ├──Bemfa──→ XiaoAi / XiaoDu
         │            ├── HTTP built-in Web Dashboard ──→ Browser direct control
         │            │     (Dashboard / Config, zero-dependency)
         │            └── NVS (Persistent config storage)
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

## Bemfa Cloud (XiaoAi / XiaoDu Voice Control)

Built-in Bemfa cloud support — control your charger with voice commands, no Home Assistant required.

### Setup

1. Register at [Bemfa](https://www.bemfa.com) and get your **UID** (32-char private key)
2. Open the ESP32 web config page, find the "巴法云（接入小爱、小度）" section
3. Enter your UID, enable Bemfa, save and reboot
4. 5 switches are automatically registered:
   - C口1开关 (C-Port 1), C口2开关 (C-Port 2), C口3开关 (C-Port 3)
   - USB-A开关 (USB-A), 蓝牙开关 (BLE)
5. Open XiaoAi APP → devices appear automatically
6. Voice control: "Hey XiaoAi, turn on C-Port 1 switch"

### Notes

- Device names are pre-set in Chinese for XiaoAi recognition
- Port states sync in real-time via BLE

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

## Firmware Update (OTA)

The firmware uses a dual-slot layout (`ota_0` / `ota_1`, 0x1F0000 ≈ 1.94MB each) and accepts firmware uploads over HTTP. A new image that fails to stay healthy is rolled back automatically.

```bash
# Running slot, update state and progress
curl http://<ESP32-IP>/api/ota

# Upload firmware (build/cuktech_ble.bin from idf.py build)
curl --data-binary @build/cuktech_ble.bin \
     -H "Content-Type: application/octet-stream" \
     http://<ESP32-IP>/api/ota
```

- BLE is switched off during the upload so WiFi gets the radio; the device reboots into the new image when done.
- The new image must run healthily for **60 seconds** before it is marked valid. If it crashes or reboots earlier, the bootloader rolls back to the previous image.
- If WiFi cannot connect and the device enters AP setup mode (the image itself booted normally), it is marked valid immediately so a later reboot — for example right after saving WiFi settings — cannot trigger a spurious rollback.
- The partition table has changed to a dual-OTA layout. **Devices still running the old single-partition firmware need one full USB flash first** (`idf.py -p /dev/ttyUSB0 flash`); OTA works from then on. NVS (WiFi / device credentials) is preserved.
- The upload endpoint has no authentication, like the rest of the API. Use it only on a trusted LAN.

## Web Dashboard (Zero-Dependency)

The firmware includes a complete built-in web UI. Once connected, open the ESP32's IP address in any browser — no Home Assistant, no MQTT broker, no external services required.

- Real-time voltage / current / power per port
- Port on/off control
- Protocol switching (PD / PPS / UFCS / SCP)
- Scene mode selection
- BLE connection toggle
- **UI language**: Simplified Chinese and English (English shown by default for any other browser language); auto-detected from the browser language, or set manually once on the config page ("Display → Interface Language", remembered per browser)
- **Fast loads**: embedded assets carry ETags and answer 304s, so scripts cost only a header round-trip on revisit and images stay cached for a week; the 70 KB chart library loads `async` so it no longer holds up the page scripts or the first data refresh; **Concurrency and memory**: at most two static responses stream at once, one MSS (1460 B) per chunk and a 3-MSS TCP send window, so six parallel browser connections can no longer exhaust the heap (measured: 38/38 requests served); images are re-encoded as WebP at 2x their display size, cutting the image payload from 1.65 MB to 290 KB (all static assets: 1.73 MB → 370 KB), which brings a cold page load down to ~8-11 s with BLE connected and ~1.3 s on a revisit; **BLE/Wi-Fi coexistence**: after authentication the firmware asks for a 200 ms connection interval with latency 2 and scans at 50% duty, so the single C3 radio does not drag TCP round trips out to ~100 ms

## File Structure

```
esp32_ble/
├── CMakeLists.txt         # ESP-IDF build entry
├── main/
│   ├── main.c             # WiFi / HTTP / task scheduling / command queues
│   ├── device_state.c/.h  # Single source of truth (ports / settings / BLE)
│   ├── uplink.c/.h        # MQTT telemetry (QoS 0, coalesced, Bemfa fan-out)
│   ├── ble_manager.c/.h   # BLE state machine + auth + serialized async commands
│   ├── miot_auth.c/.h     # MiOT authentication & encryption
│   ├── miot_protocol.c/.h # Protocol constants & TLV encoding
│   ├── config.h           # Compile-time defaults
│   ├── config_store.c/.h  # NVS config persistence
│   ├── bemfa.c/.h         # Bemfa cloud MQTT + HTTP API
│   ├── http_server.c/.h   # Web config page + REST API (keep-alive / async assets)
│   ├── ota.c/.h           # Dual-slot HTTP OTA (async upload + rollback)
│   └── queue_msg.h        # Cross-task message types
├── data/                  # Embedded web assets (WebP q75 + gzip)
├── tools/                 # Asset compression & header generation
├── partitions.csv         # Partition table (dual OTA, 0x1F0000 per slot)
├── sdkconfig.defaults     # ESP-IDF default config
└── sdkconfig              # Local sdkconfig (gitignored)
```

## Build Instructions

### Prerequisites

- [ESP-IDF v5.3](https://docs.espressif.com/projects/esp-idf/en/v5.3/esp32/get-started/index.html)

### Build

```bash
cd esp32_ble
python tools/generate_embedded.py   # writes main/embedded_files.h (gitignored; required on a fresh checkout and after changing the front end)
idf.py set-target esp32    # or esp32s3 / esp32c3
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

`flash.sh` flashes `build/merged-binary.bin` at 0x0; the merge is IDF's built-in `idf.py merge-bin`, whose offsets come straight from `build/flash_args` (= `partitions.csv`), so the script keeps no second copy.

### Multi-Platform

Supports `set-target esp32`, `esp32s3` and `esp32c3`. Configuration is layered:

- `sdkconfig.defaults`: settings shared by all three targets (NimBLE pools, lwIP send window, Wi-Fi RX/TX buffer caps, MQTT, stability switches, ...); only symbols that exist on every target belong here
- `sdkconfig.defaults.esp32`: dual-core overrides for classic ESP32 (`BTDM_CTRL_MODE_BLE_ONLY`, NimBLE on core 1, CPU1 idle-task watchdog)
- `sdkconfig.defaults.esp32s3`: dual core + 8 MB QIO flash + 240 MHz + 2 BLE controller activity slots (the flash settings must match the module; change them for a 4 MB / DIO board)
- `sdkconfig.defaults.esp32c3`: single-core overrides only (`FREERTOS_UNICORE`, 2 BLE controller activity slots, 1 s interrupt WDT)

Target-specific symbols (`BTDM_CTRL_MODE_BLE_ONLY`, `ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1`, `BT_NIMBLE_PINNED_TO_CORE_1`) must stay in the target files: they do not exist on S3/C3 or on single-core C3, and putting them in the shared file makes every build print unknown kconfig symbol. Use the IDF 5.3 `BT_NIMBLE_TRANSPORT_*` names for NimBLE buffers; the old `BT_NIMBLE_ACL_BUF_*` / `BT_NIMBLE_HCI_EVT_*` names only work through `components/bt/sdkconfig.rename` and are deprecated.

`.github/workflows/firmware.yml` builds `esp32`, `esp32c3` and `esp32s3` with ESP-IDF v5.3.5 on every change under `esp32_ble/`, and runs `idf.py merge-bin` to validate the merged image layout.

**Note**: keep `CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM` / `DYNAMIC_TX_BUFFER_NUM` in the shared defaults. The ESP32/S3 IDF defaults are 32/32, and one image-heavy page load drained the heap from 73 KB to 2.5 KB and stalled the HTTP server for ~30 s; the measured 8/6 values avoid that.

**Note (BLE link compatibility)**: a classic ESP32 (this board is an ESP32-D0WDQ6, revision v1.0) cannot establish a link-layer connection to the AD1204 charger (HS6621C, BLE 5.0) — every `ble_gap_connect` ends with HCI 0x3E (574, "connection failed to be established"). This is independent of the firmware version: the same failure occurs with the released v1.1.4 code from git, and the BLE scan/connect code has not changed since the first release. The same board connects fine as a central to other peripherals (including a random-address device at −71 dBm) and to a host-emulated peripheral (MTU 256), while an ESP32-C3 (BLE 5.0) and the host's BlueZ (BLE 5.2) both connect to the charger at the same location (−65 dBm). RF, heap and the connect flow are ruled out; the most likely cause is an interoperability problem between this first-generation ESP32 silicon's BLE controller and the charger. Use a C3 or S3 (BLE 5.0, newer controllers) for this charger.

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
