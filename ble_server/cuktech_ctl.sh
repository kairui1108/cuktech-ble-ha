#!/bin/bash
set -euo pipefail
# CUKTECH BLE Server - Service control script
# Usage: ./cuktech_ctl.sh {start|stop|restart|status|log|clear-log|clear-history}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="/tmp/cuktech_ble_server.pid"
LOG_FILE="/tmp/cuktech_server.log"
# 控制台输出（stdout/stderr）单独一个文件：主日志由 ha_server 的
# RotatingFileHandler 负责按大小切割，两个写入者不能写同一个文件。
CONSOLE_LOG="${LOG_FILE%.log}.console.log"
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

# 服务端实际读取的配置文件：口径与 config.py 的 `load_config` 一致——
# `CUKTECH_CONFIG_PATH` 优先（相对路径按脚本目录解析，do_start 会先 cd 过来），
# 没设或指向的文件不存在时回落脚本目录的 config.yaml。
# 以前各函数硬编码 `$SCRIPT_DIR/config.yaml`：用 CUKTECH_CONFIG_PATH 部署时，
# 脚本读的是另一份（或根本不存在的）配置，清历史/清 MQTT 都会操作错对象。
resolve_config_path() {
    local path="${CUKTECH_CONFIG_PATH:-}"
    if [ -n "$path" ]; then
        case "$path" in
            /*) ;;
            *)  path="$SCRIPT_DIR/$path" ;;
        esac
        if [ -f "$path" ]; then
            printf '%s\n' "$path"
            return 0
        fi
    fi
    printf '%s\n' "$SCRIPT_DIR/config.yaml"
}
CONFIG_FILE="$(resolve_config_path)"

# 历史库路径：**以配置文件里的 server.history_db_path 为准**（相对路径按脚本目录
# 解析），其次认 README 推荐的 `data/port_history.db` 持久化布局，最后才退回脚本目录。
# 以前 clear-history / history-down 都硬编码 `$SCRIPT_DIR/port_history.db`：配置成
# `data/port_history.db` 的部署跑清理只会打印 "No database found"，历史一条都没删。
resolve_history_db() {
    # 1) 环境变量优先（与服务端 config_from_mapping 的优先级一致：env > config.yaml）。
    #    相对路径按脚本目录解析：服务端就是在这个目录下启动的（do_start 会 cd 过来），
    #    否则从别处调用 ctl 会 rm 到别的目录去。
    if [ -n "${CUKTECH_HISTORY_DB_PATH:-}" ]; then
        case "$CUKTECH_HISTORY_DB_PATH" in
            /*) printf '%s\n' "$CUKTECH_HISTORY_DB_PATH" ;;
            *)  printf '%s\n' "$SCRIPT_DIR/$CUKTECH_HISTORY_DB_PATH" ;;
        esac
        return 0
    fi
    # 2) 配置文件里的 server.history_db_path（相对路径按脚本目录解析）
    local db=""
    if [ -f "$CONFIG_FILE" ]; then
        db=$("$PYTHON" -c 'import sys
try:
    import yaml
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
except Exception:
    cfg = {}
print(str((cfg.get("server") or {}).get("history_db_path") or "").strip())' \
            "$CONFIG_FILE" 2>/dev/null || true)
    fi
    if [ -n "$db" ]; then
        case "$db" in
            /*) printf '%s\n' "$db" ;;
            *)  printf '%s\n' "$SCRIPT_DIR/$db" ;;
        esac
        return 0
    fi
    # 3) 纯环境变量/无配置部署：就是服务端的默认文件名（cwd 也是脚本目录）
    printf '%s\n' "$SCRIPT_DIR/port_history.db"
}

mqtt_cleanup() {
    echo "Clearing MQTT data..."
    "$PYTHON" -c "
import json, sys
from pathlib import Path

# Load config from YAML
config_path = Path('$CONFIG_FILE')
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
cfg_path = Path('$CONFIG_FILE')
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
cfg_path = Path('$CONFIG_FILE')
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
cfg_path = Path('$CONFIG_FILE')
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
    # 主日志交给 ha_server 的 RotatingFileHandler（按 20MB 切割、保留 5 份）；
    # 这里只把 stdout/stderr 落到 console 文件，用于保留"日志系统就绪前"的
    # 启动期异常（导入错误、解释器告警等）。
    export CUKTECH_LOG_FILE="$LOG_FILE"
    # glibc 每个线程最多注册一个 64MB 虚拟 arena，7 个线程下匿名映射里会留下
    # 数 MB "已释放但不归还"的页。压到 2 个 arena（实测 RSS 降约 3-5MB；1Hz
    # 的采样负载没有争用风险）。外部显式设置时尊重外部值。
    export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
    nohup "$PYTHON" -u "$SERVER" >> "$CONSOLE_LOG" 2>&1 &
    local pid=$!
    echo $pid > "$PID_FILE"
    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        echo "Server started (PID: $pid, Log: $LOG_FILE)"
        return 0
    else
        echo "Server failed to start."
        echo "  main log    : $LOG_FILE"
        echo "  console log : $CONSOLE_LOG"
        [ -f "$CONSOLE_LOG" ] && tail -20 "$CONSOLE_LOG"
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
    elif [ -f "$CONSOLE_LOG" ]; then
        echo "(主日志不存在，显示控制台日志)"
        tail -${1:-50} "$CONSOLE_LOG"
    else
        echo "No log file found"
    fi
}

do_clear_log() {
    echo "Clearing service log..."
    local cleared=0
    for f in "$LOG_FILE" "$CONSOLE_LOG" "$LOG_FILE".*; do
        if [ -f "$f" ]; then
            > "$f"
            echo "  ✓ Cleared $f"
            cleared=1
        fi
    done
    if [ "$cleared" -eq 0 ]; then
        echo "  No log file found"
    fi
}

do_clear_history() {
    echo "Clearing history database..."
    if pid=$(get_pid); then
        echo "Server is running (PID: $pid) — 先 stop 再清库：SQLite 还持有旧文件的 inode，"
        echo "运行时删掉只是 unlink，进程还在往旧库里写，直到退出才真正释放。"
        return 1
    fi
    local db
    db="$(resolve_history_db)"
    if [ -f "$db" ]; then
        rm -f "$db" "${db}-wal" "${db}-shm"
        echo "  ✓ Removed $db"
    else
        echo "  No database found"
    fi
}

do_history_down() {
    # 回滚用（设计文档 §6.1）：把非 primary 设备的历史归档到 *_archive，主表恢复
    # 为旧代码可读。先停服务再跑（写库进程持有 WAL）。幂等，可重跑。
    local db
    db="$(resolve_history_db)"
    if [ ! -f "$db" ]; then
        echo "No history database found at $db"
        return 1
    fi
    if pid=$(get_pid); then
        echo "Server is running (PID: $pid) — 先 stop 再归档，否则 WAL 写入会与之竞争"
        return 1
    fi
    echo "Backing up $db → ${db}.bak ..."
    cp -f "$db" "${db}.bak"
    "$PYTHON" "$SCRIPT_DIR/scripts/history_down.py" --db "$db" "$@"
}

do_daily_stats() {
    # daily_energy（热力图数据源）维护，见 scripts/daily_stats.py 与设计文档 §4.6。
    # 读子命令（audit/orphans/export）可以热跑；写子命令要求先 stop：
    # 在线增量与脚本整表重建会同时写同一天的行，造成双计。
    case "${1:-}" in
        backfill|remap|purge)
            if pid=$(get_pid); then
                echo "Server is running (PID: $pid) — 先 stop 再改 daily_energy，否则会和在线增量抢同一天的行"
                return 1
            fi
            ;;
    esac
    local db
    db="$(resolve_history_db)"
    "$PYTHON" "$SCRIPT_DIR/scripts/daily_stats.py" --db "$db" "$@"
}

do_clean_retained() {
    # 回滚用（§6.1）：清掉非 primary 设备的 retained 消息，否则 broker 里留着
    # "幽灵设备"。前缀按配置推导（不硬编码 cuktech/#）。
    "$PYTHON" "$SCRIPT_DIR/scripts/clean_retained.py" "$@"
}

case "${1:-help}" in
    start)        do_start ;;
    stop)         do_stop ;;
    restart)      do_restart ;;
    status)       do_status ;;
    log)          do_log "${2:-50}" ;;
    clear-log)    do_clear_log ;;
    clear-history) do_clear_history ;;
    history-down) shift; do_history_down "$@" ;;
    daily-stats)  shift; do_daily_stats "$@" ;;
    clean-retained) shift; do_clean_retained "$@" ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|log [lines]|clear-log|clear-history|history-down|daily-stats|clean-retained}"
        echo ""
        echo "Commands:"
        echo "  start         - Start the BLE server"
        echo "  stop          - Stop the BLE server"
        echo "  restart       - Restart the BLE server"
        echo "  status        - Check server status"
        echo "  log [n]       - Show last n lines of log (default: 50)"
        echo "  clear-log     - Clear service log file"
        echo "  clear-history - Clear history database"
        echo "  history-down  - 回滚前归档非 primary 设备的历史（幂等，先自动备份）"
        echo "  daily-stats   - daily_energy 维护：backfill/audit/orphans/remap/purge/export"
        echo "  clean-retained- 清理非 primary 设备的 retained MQTT 消息（回滚用）"
        exit 1
        ;;
esac
