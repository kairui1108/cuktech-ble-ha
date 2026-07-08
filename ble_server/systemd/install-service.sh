#!/bin/bash
# Install CUKTECH BLE Server as systemd service
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/cuktech-ble-server.service"
LOGROTATE_FILE="$SCRIPT_DIR/cuktech-ble-server.logrotate"
VENV_DIR="$(dirname "$SCRIPT_DIR")/.venv"

echo "Installing CUKTECH BLE Server systemd service..."

# Copy service file
sudo cp "$SERVICE_FILE" /etc/systemd/system/cuktech-ble-server.service
sudo sed -i "s|ExecStart=.*|ExecStart=$VENV_DIR/bin/python -u $(dirname "$SCRIPT_DIR")/ha_server.py|" /etc/systemd/system/cuktech-ble-server.service
sudo sed -i "s|WorkingDirectory=.*|WorkingDirectory=$(dirname "$SCRIPT_DIR")|" /etc/systemd/system/cuktech-ble-server.service
sudo systemctl daemon-reload

# Copy logrotate config
sudo cp "$LOGROTATE_FILE" /etc/logrotate.d/cuktech-ble-server

# Enable and start service
sudo systemctl enable cuktech-ble-server.service
sudo systemctl start cuktech-ble-server.service

echo "Service installed and started!"
echo "  Status: sudo systemctl status cuktech-ble-server"
echo "  Logs:   sudo journalctl -u cuktech-ble-server -f"
echo "  Stop:   sudo systemctl stop cuktech-ble-server"
