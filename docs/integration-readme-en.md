# CUKTECH 10 GaN Charger Ultra - Home Assistant Integration

> **[中文](README.md)**

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kairui1108&repository=cuktech-ble-ha-integration&category=integration)
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cuktech_charger)

Connect CUKTECH charger to Home Assistant via MQTT for real-time monitoring, port control, and automation.

## Preview

![HA Integration](https://raw.githubusercontent.com/kairui1108/cuktech-ble-ha/main/docs/ha_integration.png)

![HA Lovelace](https://raw.githubusercontent.com/kairui1108/cuktech-ble-ha/main/docs/ha_lovelace.png)

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
- **Dual availability**: MQTT status + HTTP health check

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
| `sensor.cuktech_charger_total_power` | Total power | W |

### Switch

| Entity | Description |
|--------|-------------|
| `switch.cuktech_charger_ble_control` | BLE connection control |
| `switch.cuktech_charger_c1_port` | C1 port switch |
| `switch.cuktech_charger_c2_port` | C2 port switch |
| `switch.cuktech_charger_c3_port` | C3 port switch |
| `switch.cuktech_a_port` | A port switch |

### Known Limitations

- **Single Device**: Current architecture supports only one charger at a time. Multi-device support is planned for future releases.
- **Protocol Detection**: Charging protocol identification (PD/QC/USB-A etc.) is inferred from port voltage and PDO data, and may not always match the actual protocol.

## License

MIT License
