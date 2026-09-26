# CUKTECH 10 GaN Charger Ultra - Home Assistant Integration

> **[中文](README.md)**

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kairui1108&repository=cuktech-ble-ha-integration&category=integration)
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cuktech_charger)

Connect CUKTECH charger to Home Assistant via MQTT for real-time monitoring, port control, and automation.

## Prerequisites

BLE Server must be deployed first: [BLE Server](https://github.com/kairui1108/cuktech-ble-server)

## Installation

### Via HACS (Recommended)

1. Click **[Open in HACS]** above to add as custom integration
2. Search "CUKTECH Charger" and install
3. Restart Home Assistant
4. Click **[Add integration]** and search "CUKTECH Charger"

### Manual Installation

```bash
cp -r custom_components/cuktech_charger /config/custom_components/
```

Restart Home Assistant and add the integration.

## Configuration

| Field | Description | Default |
|-------|-------------|---------|
| Name | Display name | CUKTECH 10 GaN Charger Ultra |
| Server URL | BLE Server HTTP address | `http://localhost:8199` |

Re-authentication supported when server URL changes.

## Features

- **Real-time power monitoring**: Voltage, current, power via MQTT
- **Protocol detection**: Auto-detect PD / PD Fixed / PD PPS / QC / USB-A
- **BLE connection control**: Switch to enable/disable, binary sensor for status
- **Port control**: Remote on/off for C1/C2/C3/A ports
- **Scene modes**: AI / Digital Eco / Single Port / Balanced
- **Countdown timer**: 0-1440 minutes per port
- **Device settings**: Screen timeout, language, USB-A always-on
- **Device info sync**: Model and firmware version synced from BLE server
- **Charge event**: `charge_end` event entity fires on charge completion, enabling notification automations
- **Dual availability**: MQTT status + HTTP health check
- **Charge limit**: auto power-off at a configured Wh per port, with once/always modes and live session progress (see below)

## Entities

### Binary Sensor

| Entity | Description |
|--------|-------------|
| `binary_sensor.cuktech_charger_c1_active` | C1 active status |
| `binary_sensor.cuktech_charger_c2_active` | C2 active status |
| `binary_sensor.cuktech_charger_c3_active` | C3 active status |
| `binary_sensor.cuktech_a_active` | A active status |
| `binary_sensor.cuktech_charger_ble_connected` | BLE connection status |

### Sensor

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.cuktech_charger_c1_voltage` | C1 voltage | V |
| `sensor.cuktech_charger_c1_current` | C1 current | A |
| `sensor.cuktech_charger_c1_power` | C1 power | W |
| `sensor.cuktech_charger_c1_protocol` | C1 protocol | - |
| `sensor.cuktech_charger_c2_voltage` | C2 voltage | V |
| `sensor.cuktech_charger_c2_current` | C2 current | A |
| `sensor.cuktech_charger_c2_power` | C2 power | W |
| `sensor.cuktech_charger_c2_protocol` | C2 protocol | - |
| `sensor.cuktech_charger_c3_voltage` | C3 voltage | V |
| `sensor.cuktech_charger_c3_current` | C3 current | A |
| `sensor.cuktech_charger_c3_power` | C3 power | W |
| `sensor.cuktech_charger_c3_protocol` | C3 protocol | - |
| `sensor.cuktech_a_voltage` | A voltage | V |
| `sensor.cuktech_a_current` | A current | A |
| `sensor.cuktech_a_power` | A power | W |
| `sensor.cuktech_a_protocol` | A protocol | - |
| `sensor.cuktech_charger_total_power` | Total power | W |

### Switch

| Entity | Description |
|--------|-------------|
| `switch.cuktech_charger_ble_control` | BLE connection control |
| `switch.cuktech_charger_c1_port` | C1 port switch |
| `switch.cuktech_charger_c2_port` | C2 port switch |
| `switch.cuktech_charger_c3_port` | C3 port switch |
| `switch.cuktech_a_port` | A port switch |

### Select

| Entity | Description | Options |
|--------|-------------|---------|
| `select.cuktech_scene_mode` | Scene mode | AI / Digital Eco / Single Port / Balanced |
| `select.cuktech_screen_save_time` | Screen timeout | 5min / 1min / 10min / 30min / Always on |
| `select.cuktech_language` | Language | English / 中文 |

### Number

| Entity | Description | Range |
|--------|-------------|-------|
| `number.cuktech_charger_c1_countdown` | C1 countdown | 0-1440 min |
| `number.cuktech_charger_c2_countdown` | C2 countdown | 0-1440 min |
| `number.cuktech_charger_c3_countdown` | C3 countdown | 0-1440 min |
| `number.cuktech_a_countdown` | A countdown | 0-1440 min |
| `number.cuktech_charger_c1_charge_limit` | C1 charge limit (0 = off) | 0-1000 Wh |
| `number.cuktech_charger_c2_charge_limit` | C2 charge limit (0 = off) | 0-1000 Wh |
| `number.cuktech_charger_c3_charge_limit` | C3 charge limit (0 = off) | 0-1000 Wh |
| `number.cuktech_a_charge_limit` | A charge limit (0 = off) | 0-1000 Wh |

### Charge limit entities

Three entities per port, 12 in total:

| Platform | Suffix | Description |
|----------|--------|-------------|
| `number` | `_{port}_charge_limit` | Threshold in Wh; **0 disables** the limit |
| `select` | `_{port}_charge_limit_mode` | `once` (clears after firing) / `always` (re-arms each session) |
| `sensor` | `_{port}_session_energy` | Energy delivered this session (Wh), with `remaining_wh` / `limit_wh` / `limit_mode` / `is_charging` attributes |

> The `entity_id` prefix comes from the **device name** (this integration defaults
> to the Chinese product name, so real ids look like
> `sensor.ku_tai_ke_10hao_..._c1_session_energy`). Rename the device in HA for
> shorter ids. Other tables on this page use the same illustrative convention.

## Charge limit

When the threshold is reached the integration switches the port off over MQTT
port control — a channel **both** the Python BLE server and the ESP32 firmware
already implement, so limits work under either backend with **no firmware
change**.

The unit is *charger output energy* (V×I trapezoidal integration), not the
energy actually stored in the charged device: cable and conversion losses make
the latter 5~15% smaller. This matches the Web UI / `/api/charge-limits` exactly.

### Two modes of operation

On startup the integration probes `GET /api/charge-limits` to pick a backend:

| Backend | Trigger | Config storage | Displayed figures | Applies to |
|---------|---------|----------------|-------------------|------------|
| `server` (delegated) | HTTP 200 | Python server (two-way sync with the Web UI) | Server snapshot (incl. `session_wh` / `is_charging`) | Python BLE server |
| `local` (own metering) | HTTP 404 / unreachable | HA `Store` (local persistence) | HA's own integration of the 1Hz MQTT samples | ESP32 firmware, or when the server is offline |

**Metering and enforcement are separate concerns**: under either backend HA always
integrates the 1Hz MQTT port samples locally, so the readout stays live and the
feature survives the REST API going away. The backend only decides *who owns the
config and who cuts the power*. In delegated mode the entities mirror the server
snapshot, so they match the Web UI exactly.

The ESP32 firmware does no energy accounting (it only pushes instantaneous
V/I/P), so it **always** lands in `local`: HA does the integration. Since those
samples are identical under both firmwares, accuracy matches the Python side. In
short — the actuation channel is shared; only metering and decision moved to HA.

To tell which backend is active, check the `backend` attribute on any charge-limit
entity, or the startup log line `Charge limits delegated to BLE server (...)`
versus `Charge limits metered locally (no BLE server API at ...: ...)` (the latter
includes the reason for falling back).

`mode` semantics (identical to the Python table):

| mode | Threshold reached | Session ends early (unplug/manual off/full) | BLE reconnect / HA restart |
|---|---|---|---|
| `once` | cut off, then cleared | cleared | preserved |
| `always` | cut off, kept for next session | kept | kept |

### Notes

- **Configure the limit in one place only.** If the Web UI and HA hold
  different thresholds each enforces independently — harmless, but the
  remaining-energy readouts will disagree. With the Python server, prefer the
  delegated mode so there is a single source of truth.
- **Overshoot**: about 0.05 Wh at a 100 W load (1 Hz sampling + command round trip).
- **HA restart**: energy already accumulated in an ongoing session cannot be
  recovered (no sampling while down) and restarts from 0. This errs toward
  over-charging rather than risking a premature cut-off. Long-run totals persist.
- `session_energy` resets per session, so it is **not** suitable for HA's Energy
  dashboard (that needs a monotonic kWh sensor); these are separate concerns.

## 效果预览

![HA Integration](https://raw.githubusercontent.com/kairui1108/cuktech-ble-ha/main/docs/ha_integration.png)

![HA Lovelace](https://raw.githubusercontent.com/kairui1108/cuktech-ble-ha/main/docs/ha_lovelace.png)

### Known Limitations

- **Single Device**: Current architecture supports only one charger at a time. Multi-device support is planned for future releases.
- **Protocol Detection**: Protocol is derived from the authoritative firmware push (PIID 17/18, consistent with the Xiaomi Home app) and updates immediately on PD/PPS change — no periodic 60s refresh lag. A coarse voltage-based inference is used only in an extreme cold start (when PIID 17 is never received and its read fails).
- **Platform Support**: Development and testing are done exclusively on Linux. Compatibility with other platforms (macOS, Windows) has not been verified — use at your own risk.

## License

MIT License
