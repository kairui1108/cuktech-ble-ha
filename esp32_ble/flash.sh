#!/bin/bash
set -e

# CUKTECH BLE ESP32 — 编译并烧录
# 用法: ./flash.sh [target] [port]
# 默认: target=esp32, port=/dev/ttyUSB1

TARGET="${1:-esp32}"
PORT="${2:-/dev/ttyUSB1}"
DIR="$(cd "$(dirname "$0")" && pwd)"
IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"

export PATH="$HOME/tools/bin:$PATH"
. "$IDF_PATH/export.sh" > /dev/null 2>&1

cd "$DIR"
idf.py set-target "$TARGET"
idf.py build
idf.py -p "$PORT" flash
