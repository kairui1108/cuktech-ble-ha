#!/bin/bash
set -e

# CUKTECH BLE ESP32 — 编译/烧录/监控
# 用法:
#   ./flash.sh                          编译+烧录（自动检测串口，默认 ESP32）
#   ./flash.sh build                    仅编译
#   ./flash.sh flash                    编译+烧录
#   ./flash.sh monitor                  编译+烧录+串口监控
#   ./flash.sh clean                    清除编译产物
#   ./flash.sh port                     自动检测 ESP32 串口
#   ./flash.sh /dev/ttyUSBx             指定串口编译+烧录
#
# 指定芯片型号:
#   --chip esp32   ESP32（默认）
#   --chip esp32s3 ESP32-S3
#   --chip esp32c3 ESP32-C3
#
# 例:
#   ./flash.sh --chip esp32s3              ESP32-S3 自动检测串口
#   ./flash.sh flash --chip esp32c3        ESP32-C3 仅烧录
#   ./flash.sh monitor --chip esp32s3 /dev/ttyACM0   ESP32-S3 指定串口+监控

TARGET="esp32"
PORT=""
CMD="flash"
DIR="$(cd "$(dirname "$0")" && pwd)"
IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"

# 解析参数
while [ $# -gt 0 ]; do
    case "$1" in
        --chip)
            TARGET="$2"; shift 2 ;;
        build|b|flash|f|monitor|m|clean|c|port|p)
            CMD="$1"; shift ;;
        -h|--help)
            echo "用法: ./flash.sh [命令] [--chip 型号] [串口]"
            echo ""
            echo "命令:"
            echo "  无参数    编译+烧录（自动检测串口）"
            echo "  build     仅编译"
            echo "  flash     编译+烧录"
            echo "  monitor   编译+烧录+串口监控"
            echo "  clean     清除编译产物"
            echo "  port      检测 ESP32 串口"
            echo ""
            echo "芯片型号 (--chip):"
            echo "  esp32    ESP32（默认）"
            echo "  esp32s3  ESP32-S3"
            echo "  esp32c3  ESP32-C3"
            echo "  esp32c2  ESP32-C2"
            echo "  esp32h2  ESP32-H2"
            echo ""
            echo "示例:"
            echo "  ./flash.sh                                ESP32 自动检测串口"
            echo "  ./flash.sh --chip esp32s3                 ESP32-S3"
            echo "  ./flash.sh flash --chip esp32c3           ESP32-C3 仅烧录"
            echo "  ./flash.sh /dev/ttyUSB0                   指定串口"
            echo "  ./flash.sh monitor --chip esp32s3 /dev/ttyACM0  S3+指定串口+监控"
            exit 0
            ;;
        /dev/*)
            PORT="$1"; shift ;;
        *)
            echo "未知参数: $1（使用 --help 查看用法）"
            exit 1
            ;;
    esac
done

# 根据芯片型号自动选择默认串口
default_port_for_chip() {
    case "$TARGET" in
        esp32|esp32s3|esp32c3|esp32c2|esp32h2|esp32s2)
            echo "/dev/ttyUSB0" ;;
        *)  echo "/dev/ttyUSB0" ;;
    esac
}

# 自动检测 ESP32 串口
auto_detect_port() {
    for p in /dev/ttyUSB* /dev/ttyACM*; do
        [ -e "$p" ] || continue
        if python -m esptool --chip "$TARGET" --port "$p" --baud 115200 chip_id 2>/dev/null | grep -qi "chip\|esp32\|esp32c3\|esp32s3"; then
            echo "$p"
            return 0
        fi
    done
    echo ""
    return 1
}

# 初始化 ESP-IDF 环境
init_idf() {
    export PATH="$HOME/.local/lib/python3.11/site-packages/cmake/data/bin:$HOME/.local/bin:$PATH"
    . "$IDF_PATH/export.sh" > /dev/null 2>&1
}

# 合并 bootloader + 分区表 + otadata + app 为单个镜像。
# 用 IDF 内置的 merge-bin：偏移量直接取自 build/flash_args（即 partitions.csv），
# 这里不再抄一份，避免分区表改了而脚本没改。产物是 build/merged-binary.bin。
_merge_bins() {
    echo "==> 合并为 merged-binary.bin..."
    idf.py merge-bin > /dev/null
}

# 获取烧录参数
_flash_params() {
    case "$TARGET" in
        esp32s3)   echo "qio 8MB" ;;
        esp32c3)   echo "dio 4MB" ;;
        *)         echo "dio 4MB" ;;
    esac
}

cd "$DIR"

case "$CMD" in
    build|b)
        init_idf
        echo "==> 编译固件 ($TARGET)..."
        idf.py set-target "$TARGET" 2>&1 | tail -3
        idf.py build
        _merge_bins
        echo "==> 编译完成 (build/merged-binary.bin)"
        ;;
    flash|f)
        init_idf
        if [ -z "$PORT" ]; then
            echo "==> 自动检测 $TARGET 串口..."
            PORT=$(auto_detect_port)
            if [ -z "$PORT" ]; then
                echo "✗ 未检测到 $TARGET 串口"
                exit 1
            fi
            echo "==> 检测到: $PORT"
        fi
        echo "==> 编译+烧录 $TARGET 到 $PORT..."
        idf.py set-target "$TARGET" 2>&1 | tail -1
        idf.py build
        _merge_bins
        read -r FLASH_MODE FLASH_SIZE <<< "$(_flash_params)"
        python -m esptool --chip "$TARGET" -p "$PORT" -b 460800 \
            --before default_reset --after hard_reset \
            write_flash --flash_mode "$FLASH_MODE" --flash_size "$FLASH_SIZE" \
            0x0 build/merged-binary.bin
        echo "==> 烧录完成"
        ;;
    monitor|m)
        init_idf
        if [ -z "$PORT" ]; then
            PORT=$(auto_detect_port)
            if [ -z "$PORT" ]; then
                echo "✗ 未检测到 $TARGET 串口"
                exit 1
            fi
        fi
        echo "==> 编译+烧录+监控 $TARGET ($PORT)..."
        idf.py set-target "$TARGET" 2>&1 | tail -1
        idf.py build
        _merge_bins
        read -r FLASH_MODE FLASH_SIZE <<< "$(_flash_params)"
        python -m esptool --chip "$TARGET" -p "$PORT" -b 460800 \
            --before default_reset --after hard_reset \
            write_flash --flash_mode "$FLASH_MODE" --flash_size "$FLASH_SIZE" \
            0x0 build/merged-binary.bin
        echo "==> 启动串口监控 (Ctrl+] 退出)..."
        idf.py -p "$PORT" monitor
        ;;
    clean|c)
        init_idf
        echo "==> 清除编译产物..."
        rm -rf build
        echo "==> 完成"
        ;;
    port|p)
        echo "==> 搜索 $TARGET 串口..."
        PORT=$(auto_detect_port)
        if [ -n "$PORT" ]; then
            echo "==> $TARGET: $PORT"
        else
            echo "✗ 未检测到 $TARGET 串口"
            exit 1
        fi
        ;;
    *)
        echo "未知命令: $CMD"
        exit 1
        ;;
esac
