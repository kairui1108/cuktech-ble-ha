# CUKTECH 10 GaN Charger Ultra - Home Assistant Integration

> **[中文](README.md)**

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kairui1108&repository=cuktech-ble-ha-integration&category=integration)
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cuktech_charger)

Home Assistant integration for CUKTECH chargers via MQTT, providing real-time monitoring, port control, and automation support.

## Prerequisites

[BLE Server](https://github.com/kairui1108/cuktech-ble-server) must be deployed first.

## Installation

### Via HACS (Recommended)

1. Click **[Open in HACS]** above to add this repository as a custom integration
2. Search for "CUKTECH Charger" and install
3. Restart Home Assistant
4. Click **[Add integration]** above, search for "CUKTECH Charger" and configure

### Manual Installation

```bash
cp -r custom_components/cuktech_charger /config/custom_components/
```

Restart Home Assistant, then add the integration from the Integrations page.

## Configuration

| Field | Description | Default |
|-------|-------------|---------|
| Name | Integration display name | CUKTECH Charger |
| Server URL | BLE Server HTTP address | `http://localhost:8199` |

## Features

- **Real-time Power Monitoring**: Voltage, current, power data per port via MQTT
- **Protocol Detection**: Auto-detect PD / PD Fixed / PD PPS / QC / USB-A protocols
- **Port Control**: Remote on/off for C1/C2/C3/A ports
- **Scene Mode**: AI Smart / Apple 2.4A / Single Port Priority / Balanced
- **Countdown Timer**: Per-port charging countdown
- **Device Settings**: Screen timeout, language, USB-A always-on, etc.
- **Entity Availability**: MQTT status + HTTP health check dual detection, entities auto-unavailable when server is offline

## Entities

### Sensors

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.cuktech_c1_voltage` | C1 Voltage | V |
| `sensor.cuktech_c1_current` | C1 Current | A |
| `sensor.cuktech_c1_power` | C1 Power | W |
| `sensor.cuktech_c1_protocol` | C1 Protocol | - |
| `sensor.cuktech_c2_voltage` | C2 Voltage | V |
| `sensor.cuktech_c2_current` | C2 Current | A |
| `sensor.cuktech_c2_power` | C2 Power | W |
| `sensor.cuktech_c2_protocol` | C2 Protocol | - |
| `sensor.cuktech_c3_voltage` | C3 Voltage | V |
| `sensor.cuktech_c3_current` | C3 Current | A |
| `sensor.cuktech_c3_power` | C3 Power | W |
| `sensor.cuktech_c3_protocol` | C3 Protocol | - |
| `sensor.cuktech_a_voltage` | A Voltage | V |
| `sensor.cuktech_a_current` | A Current | A |
| `sensor.cuktech_a_power` | A Power | W |
| `sensor.cuktech_a_protocol` | A Protocol | - |
| `sensor.cuktech_total_power` | Total Power | W |
| `sensor.cuktech_c1_countdown` | C1 Countdown | min |
| `sensor.cuktech_c2_countdown` | C2 Countdown | min |
| `sensor.cuktech_c3_countdown` | C3 Countdown | min |
| `sensor.cuktech_a_countdown` | A Countdown | min |
| `sensor.cuktech_idle_screenoff` | Idle Screen Off | - |
| `sensor.cuktech_screen_dir_lock` | Screen Dir Lock | - |

### Switches

| Entity | Description |
|--------|-------------|
| `switch.cuktech_c1_port` | C1 Port Switch |
| `switch.cuktech_c2_port` | C2 Port Switch |
| `switch.cuktech_c3_port` | C3 Port Switch |
| `switch.cuktech_a_port` | A Port Switch |
| `switch.cuktech_usb_a_always_on` | USB-A Always On |
| `switch.cuktech_idle_screenoff` | Idle Screen Off |
| `switch.cuktech_screen_dir_lock` | Screen Dir Lock |

### Selects

| Entity | Description | Options |
|--------|-------------|---------|
| `select.cuktech_scene_mode` | Scene Mode | AI Smart / Apple 2.4A / Single Port / Balanced |
| `select.cuktech_screen_save_time` | Screen Timeout | 5min / 4min / 3min / 2min / 1min |
| `select.cuktech_language` | Language | English / 中文 |

### Binary Sensors

| Entity | Description |
|--------|-------------|
| `binary_sensor.cuktech_c1_active` | C1 Active |
| `binary_sensor.cuktech_c2_active` | C2 Active |
| `binary_sensor.cuktech_c3_active` | C3 Active |
| `binary_sensor.cuktech_a_active` | A Active |

### Numbers

| Entity | Description | Range |
|--------|-------------|-------|
| `number.cuktech_c1_countdown` | C1 Countdown Setting | 0-999 min |
| `number.cuktech_c2_countdown` | C2 Countdown Setting | 0-999 min |
| `number.cuktech_c3_countdown` | C3 Countdown Setting | 0-999 min |
| `number.cuktech_a_countdown` | A Countdown Setting | 0-999 min |

## Protocol Reference

| Protocol | Description |
|----------|-------------|
| idle | No device connected |
| PD | USB Power Delivery |
| PD Fixed | PD fixed voltage (5/9/12/15/20V) |
| PD PPS | PD Programmable Power Supply |
| QC | Quick Charge |
| USB-A | USB-A charging (DCP) |

## Automation Examples

### Charge Complete Notification

```yaml
automation:
  - alias: "CUKTECH Charge Complete"
    trigger:
      - platform: state
        entity_id: binary_sensor.cuktech_c1_active
        to: "off"
    condition:
      - condition: template
        value_template: "{{ trigger.from_state.state == 'on' }}"
    action:
      - service: notify.notify
        data:
          message: "CUKTECH C1 charging complete"
```

### High Power Alert

```yaml
automation:
  - alias: "CUKTECH High Power Alert"
    trigger:
      - platform: numeric_state
        entity_id: sensor.cuktech_total_power
        above: 100
    action:
      - service: notify.notify
        data:
          message: "CUKTECH total power exceeds 100W: {{ states('sensor.cuktech_total_power') }}W"
```

### Off-Peak Scene Switch

```yaml
automation:
  - alias: "Off-Peak Balanced Charging"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.cuktech_scene_mode
        data:
          option: "Balanced"
```

## Troubleshooting

### Entity Shows Unavailable

- Check BLE Server is running: `curl http://<server-ip>:8199/api/status`
- Check MQTT Broker is reachable
- Check HA logs for connection errors

### Data Not Updating

- Confirm BLE Server is connected to charger (Web UI shows "Connected")
- Check MQTT subscription: use MQTT Explorer to view `cuktech/charger/` topics

### Port Control Not Responding

- Confirm BLE connection is normal
- Check if charger supports that port control (some models have different port counts)

## Acknowledgments

- [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) - BLE protocol reference implementation
- [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) - Protocol detection reference
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) - Xiaomi device token extractor
- [bleak](https://github.com/hbldh/bleak) - BLE communication library
- [paho-mqtt](https://eclipse.dev/paho/) - MQTT client

## License

MIT License
