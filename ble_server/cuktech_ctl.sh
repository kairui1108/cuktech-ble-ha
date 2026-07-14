#!/bin/bash
set -euo pipefail
# CUKTECH BLE Server - Service control script
# Usage: ./cuktech_ctl.sh {start|stop|restart|status|log|clear-log|clear-history}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="/tmp/cuktech_ble_server.pid"
LOG_FILE="/tmp/cuktech_server.log"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
SERVER="$SCRIPT_DIR/ha_server.py"

# Environment variables - configure before running (optional, config.yaml is primary)
# export CUKTECH_DEVICE_MAC="XX:XX:XX:XX:XX:XX"
# export CUKTECH_DEVICE_TOKEN=""
# export CUKTECH_DEVICE_BLE_KEY=""
# export MQTT_HOST="localhost"
# export MQTT_PORT="1883"
# export MQTT_USER=""
# export MQTT_PASS=""

get_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    return 1
}

mqtt_cleanup() {
    echo "Clearing MQTT data..."
    "$PYTHON" -c "
import json, sys
from pathlib import Path

# Load config from YAML
config_path = Path('$SCRIPT_DIR/config.yaml')
if config_path.exists():
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except:
        cfg = {}
else:
    cfg = {}

mqtt_cfg = cfg.get('mqtt', {})
host = mqtt_cfg.get('host', 'localhost')
port = mqtt_cfg.get('port', 1883)
user = mqtt_cfg.get('username', '')
pw = mqtt_cfg.get('password', '')
topic = mqtt_cfg.get('topic_prefix', 'cuktech/charger')

import paho.mqtt.client as mqtt
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
if user:
    c.username_pw_set(user, pw)
try:
    c.connect(host, port, 2)
    c.loop_start()
    import time; time.sleep(0.5)
    zero = json.dumps({'voltage':0.0,'current':0.0,'power':0.0,'active':False,'protocol':'idle'})
    for p in ['c1','c2','c3','a']:
        c.publish(topic+'/port/'+p, zero, retain=True)
    c.publish(topic+'/settings', '{}', retain=True)
    c.publish(topic+'/status', json.dumps({'connected':False}), retain=True)
    time.sleep(0.5)
    c.loop_stop()
    c.disconnect()
except: pass
" 2>/dev/null
}

read_server_port() {
    "$PYTHON" -c "
import yaml
from pathlib import Path
cfg_path = Path('$SCRIPT_DIR/config.yaml')
if cfg_path.exists():
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
    print(cfg.get('server', {}).get('port', 8199))
else:
    print(8199)
" 2>/dev/null || echo 8199
}

is_mqtt_enabled() {
    "$PYTHON" -c "
import yaml
from pathlib import Path
cfg_path = Path('$SCRIPT_DIR/config.yaml')
if cfg_path.exists():
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
    enabled = cfg.get('mqtt', {}).get('enabled', False)
    if enabled:
        print(1)
" 2>/dev/null || echo 0
}

do_cleanup() {
    echo "Cleaning up old processes and BLE connections..."
    if [ "$(is_mqtt_enabled)" = "1" ]; then
        mqtt_cleanup
    fi
    local PORT
    PORT=$(read_server_port)
    lsof -i :"$PORT" -t 2>/dev/null | xargs -r kill -9 2>/dev/null || true
    pkill -f "$SCRIPT_DIR/ha_server.py" 2>/dev/null || true
    sleep 2
    pkill -9 -f "$SCRIPT_DIR/ha_server.py" 2>/dev/null || true
    # Read MAC from config.yaml for BLE disconnect
    local MAC
    MAC=$("$PYTHON" -c "
import yaml
from pathlib import Path
cfg_path = Path('$SCRIPT_DIR/config.yaml')
if cfg_path.exists():
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
    print(cfg.get('ble', {}).get('mac', ''))
" 2>/dev/null || echo "")
    if [ -n "$MAC" ] && bluetoothctl info "$MAC" 2>/dev/null | grep -q "Connected: yes"; then
        bluetoothctl disconnect "$MAC" 2>/dev/null || true
        sleep 1
    fi
    # Power cycle BLE adapter to ensure clean state for new process
    if command -v bluetoothctl &>/dev/null; then
        bluetoothctl power off 2>/dev/null || true
        sleep 1
        bluetoothctl power on 2>/dev/null || true
        # Wait up to 10s for adapter to be ready
        for i in $(seq 1 10); do
            sleep 1
            if bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then
                break
            fi
        done
    fi
}

do_start() {
    if pid=$(get_pid); then
        echo "Server already running (PID: $pid)"
        return 1
    fi

    if [ ! -d "$VENV_DIR" ]; then
        echo "Virtual environment not found at $VENV_DIR"
        echo "Run: python3 -m venv $VENV_DIR && $VENV_DIR/bin/pip install -e $SCRIPT_DIR/src/cuktech_ble"
        return 1
    fi

    do_cleanup

    echo "Starting CUKTECH BLE Server..."
    cd "$SCRIPT_DIR"
    nohup "$PYTHON" -u "$SERVER" >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo $pid > "$PID_FILE"
    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        echo "Server started (PID: $pid, Log: $LOG_FILE)"
        return 0
    else
        echo "Server failed to start. Check log: $LOG_FILE"
        rm -f "$PID_FILE"
        return 1
    fi
}

do_stop() {
    if ! pid=$(get_pid); then
        echo "Server not running"
        if [ "$(is_mqtt_enabled)" = "1" ]; then
            mqtt_cleanup
        fi
        return 0
    fi

    echo "Stopping server (PID: $pid)..."
    if [ "$(is_mqtt_enabled)" = "1" ]; then
        mqtt_cleanup
    fi
    kill "$pid" 2>/dev/null
    local count=0
    while kill -0 "$pid" 2>/dev/null && [ $count -lt 10 ]; do
        sleep 0.5
        count=$((count + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "Force killing..."
        kill -9 "$pid" 2>/dev/null
    fi

    rm -f "$PID_FILE"
    echo "Server stopped"
}

do_status() {
    if pid=$(get_pid); then
        echo "Server running (PID: $pid)"
        if command -v curl &>/dev/null; then
            local status
            local PORT
            PORT=$(read_server_port)
            status=$(curl -s --max-time 3 "http://localhost:${PORT}/api/status" 2>/dev/null)
            if [ $? -eq 0 ]; then
                echo "API Response: $status"
            else
                echo "API not responding"
            fi
        fi
        return 0
    else
        echo "Server not running"
        return 1
    fi
}

do_restart() {
    do_stop
    sleep 1
    do_start
}

do_log() {
    if [ -f "$LOG_FILE" ]; then
        tail -${1:-50} "$LOG_FILE"
    else
        echo "No log file found"
    fi
}

do_clear_log() {
    echo "Clearing service log..."
    if [ -f "$LOG_FILE" ]; then
        > "$LOG_FILE"
        echo "  ✓ Cleared $LOG_FILE"
    else
        echo "  No log file found"
    fi
}

do_clear_history() {
    echo "Clearing history database..."
    local db="$SCRIPT_DIR/port_history.db"
    if [ -f "$db" ]; then
        rm -f "$db" "${db}-wal" "${db}-shm"
        echo "  ✓ Removed $db"
    else
        echo "  No database found"
    fi
}

case "${1:-help}" in
    start)        do_start ;;
    stop)         do_stop ;;
    restart)      do_restart ;;
    status)       do_status ;;
    log)          do_log "${2:-50}" ;;
    clear-log)    do_clear_log ;;
    clear-history) do_clear_history ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|log [lines]|clear-log|clear-history}"
        echo ""
        echo "Commands:"
        echo "  start         - Start the BLE server"
        echo "  stop          - Stop the BLE server"
        echo "  restart       - Restart the BLE server"
        echo "  status        - Check server status"
        echo "  log [n]       - Show last n lines of log (default: 50)"
        echo "  clear-log     - Clear service log file"
        echo "  clear-history - Clear history database"
        exit 1
        ;;
esac
