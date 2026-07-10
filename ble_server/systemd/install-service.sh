#!/bin/bash
# Install CUKTECH BLE Server as systemd service
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/cuktech-ble-server.service"
LOGROTATE_FILE="$SCRIPT_DIR/cuktech-ble-server.logrotate"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"
CURRENT_USER="$(whoami)"

echo "Installing CUKTECH BLE Server systemd service..."
echo "  User: $CURRENT_USER"
echo "  Dir:  $PROJECT_DIR"

# Generate service file with current user and paths
sudo tee /etc/systemd/system/cuktech-ble-server.service > /dev/null <<EOF
[Unit]
Description=CUKTECH BLE Server for Home Assistant
After=network.target bluetooth.target
Wants=network.target bluetooth.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/python -u $PROJECT_DIR/ha_server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cuktech-ble

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$PROJECT_DIR /tmp
PrivateTmp=true

# Bluetooth access
SupplementaryGroups=bluetooth

# Environment
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

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
