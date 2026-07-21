#!/bin/bash
set -e

# If a command is passed, execute it directly
if [ $# -gt 0 ]; then
    exec "$@"
fi

CONFIG_FILE="${CONFIG_FILE:-/app/config.yaml}"

echo "======================================"
echo "  CUKTECH BLE Server Docker"
echo "======================================"

# Check config file
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[WARN] Config file not found at $CONFIG_FILE"
    if [ -n "$CUKTECH_DEVICE_MAC" ]; then
        echo "[INFO] Configuring from environment variables..."
        export CUKTECH_DEVICE_MAC CUKTECH_DEVICE_TOKEN CUKTECH_DEVICE_BLE_KEY
        export MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS MQTT_ENABLED
        export BEMFA_ENABLED BEMFA_UID
    else
        echo "[WARN] No config file and no environment variables set."
        echo "[INFO] Please mount config.yaml or set environment variables."
    fi
else
    echo "[INFO] Config file found: $CONFIG_FILE"
    # Copy to /app/config.yaml only if not already there (avoid cp self-error)
    if [ "$CONFIG_FILE" != "/app/config.yaml" ]; then
        cp "$CONFIG_FILE" /app/config.yaml
    fi
fi

# Check D-Bus (use host's via mounted socket)
if [ -S /var/run/dbus/system_bus_socket ]; then
    echo "[INFO] Host D-Bus socket found"
else
    echo "[WARN] Host D-Bus socket not found! BLE may not work."
    echo "[INFO] Please mount -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro"
fi

# Verify bluetoothctl available
if command -v bluetoothctl > /dev/null 2>&1; then
    echo "[INFO] bluetoothctl found"
else
    echo "[WARN] bluetoothctl not found!"
fi

echo "[INFO] Starting BLE Server..."
exec python3 ha_server.py
