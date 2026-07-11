#!/bin/bash
# CUKTECH BLE Server - 环境检查脚本
# 支持: Linux (x86_64/ARM), BlueZ 5.66+, Docker 容器
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ERRORS=0
WARNINGS=0

check_pass() { echo "  ✓ $1"; }
check_fail() { echo "  ✗ $1"; ERRORS=$((ERRORS + 1)); }
check_warn() { echo "  ⚠ $1"; WARNINGS=$((WARNINGS + 1)); }

echo "========================================"
echo " CUKTECH BLE Server - Environment Check"
echo "========================================"
echo ""

# ==========================================
# 0. 平台检测
# ==========================================
OS_TYPE=$(uname -s 2>/dev/null || echo "Unknown")
if [ "$OS_TYPE" != "Linux" ]; then
    check_fail "Unsupported OS: $OS_TYPE"
    echo ""
    echo "  This server is designed for Linux. Compatibility with"
    echo "  other platforms (macOS, Windows) has not been tested."
    echo "  Use at your own risk."
    echo ""
    echo "Result: FAIL (unsupported platform)"
    exit 1
fi

# Docker 检测
if [ -f "/.dockerenv" ] || grep -q "docker" /proc/1/cgroup 2>/dev/null; then
    check_warn "Running inside Docker container (ensure --privileged and /dev/hci* mounted)"
fi

# ==========================================
# 1. Python 环境
# ==========================================
echo "[Python]"
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 --version 2>&1)
    PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "")
    PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "")
    if [ -n "$PY_MAJOR" ] && [ -n "$PY_MINOR" ]; then
        if [ "$PY_MAJOR" -gt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 10 ]; }; then
            check_pass "$PY_VER"
        else
            check_warn "$PY_VER (need >= 3.10)"
        fi
    else
        check_warn "$PY_VER (could not determine version)"
    fi
else
    check_fail "python3 not installed"
fi

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ -f "$VENV_PYTHON" ]; then
    VENV_VER=$("$VENV_PYTHON" --version 2>&1)
    check_pass "venv: $VENV_VER"
    PYTHON="$VENV_PYTHON"
else
    check_warn "venv not found, using system python3"
    PYTHON="python3"
fi

# ==========================================
# 2. Python 依赖
# ==========================================
echo ""
echo "[Python Dependencies]"
for pkg in bleak cryptography aiohttp paho.mqtt.client yaml; do
    if "$PYTHON" -c "import $pkg" 2>/dev/null; then
        check_pass "$pkg"
    else
        check_fail "$pkg not installed (run: $PYTHON -m pip install -e $SCRIPT_DIR)"
    fi
done

# ==========================================
# 3. 蓝牙工具
# ==========================================
echo ""
echo "[Bluetooth Tools]"

HAS_BTCTL=0
HAS_HCICONFIG=0
HAS_BTMGMT=0

if command -v bluetoothctl >/dev/null 2>&1; then
    check_pass "bluetoothctl"
    HAS_BTCTL=1
else
    check_warn "bluetoothctl not found"
fi

if command -v hciconfig >/dev/null 2>&1; then
    check_pass "hciconfig"
    HAS_HCICONFIG=1
else
    check_warn "hciconfig not found (deprecated in BlueZ 5.72+, using btmgmt fallback)"
fi

if command -v btmgmt >/dev/null 2>&1; then
    check_pass "btmgmt"
    HAS_BTMGMT=1
else
    check_warn "btmgmt not found"
fi

if [ $HAS_BTCTL -eq 0 ] && [ $HAS_BTMGMT -eq 0 ]; then
    check_fail "No Bluetooth management tool available (need bluetoothctl or btmgmt)"
fi

# ==========================================
# 4. 蓝牙适配器详情
# ==========================================
echo ""
echo "[Bluetooth Adapter(s)]"

BT_DIR="/sys/class/bluetooth"
if [ ! -d "$BT_DIR" ] || [ -z "$(ls "$BT_DIR"/hci* 2>/dev/null)" ]; then
    check_fail "No Bluetooth adapter found"
else
    index=1
    for hci_dir in "$BT_DIR"/hci*; do
        hci_name=$(basename "$hci_dir")

        # 跳过虚拟适配器
        if echo "$hci_name" | grep -q ":"; then
            continue
        fi

        # 跳过无 device 子目录的（不是真实 USB/PCI 适配器）
        if [ ! -d "$hci_dir/device" ]; then
            continue
        fi

        # 检查设备类型（过滤纯虚拟设备）
        devtype_file="$hci_dir/device/uevent"
        if [ -f "$devtype_file" ]; then
            devtype=$(grep "DEVTYPE=" "$devtype_file" 2>/dev/null | cut -d= -f2 || true)
            if [ "$devtype" = "virtual" ]; then
                continue
            fi
        fi

        echo ""
        echo "  Adapter #$index ($hci_name)"
        echo "  ----------------------------------------"

        # 从 bluetoothctl list/show 获取 MAC 和 Name
        mac_addr=""
        name=""
        if [ $HAS_BTCTL -eq 1 ]; then
            bt_list=$(bluetoothctl list 2>/dev/null || true)
            mac_addr=$(echo "$bt_list" | awk -v hci="$hci_name" '/Controller/ && $0 ~ hci {print $2}' | head -1)
            [ -z "$mac_addr" ] && mac_addr=$(echo "$bt_list" | awk '/Controller/ {print $2; exit}')
            [ -z "$mac_addr" ] && mac_addr="Unknown"

            if [ "$mac_addr" != "Unknown" ]; then
                bt_detail=$(bluetoothctl show "$mac_addr" 2>/dev/null || true)
                name=$(echo "$bt_detail" | awk -F': ' '/^\tName:/ {print $2; exit}')
            fi
        fi
        [ -z "$mac_addr" ] && mac_addr="Unknown"
        [ -z "$name" ] && name="(unknown)"

        echo "  Address          : $mac_addr"
        echo "  Name             : $name"

        # 驱动信息
        dev=$(readlink -f "$hci_dir/device" 2>/dev/null || true)
        driver="Unknown"
        if [ -n "$dev" ]; then
            drv=$(basename "$(readlink "$dev/driver" 2>/dev/null)" 2>/dev/null || true)
            [ -n "$drv" ] && driver="$drv"
        fi
        echo "  Driver           : $driver"

        # Power 和能力信息
        if [ $HAS_HCICONFIG -eq 1 ]; then
            hciconfig_out=$(hciconfig "$hci_name" 2>/dev/null || true)

            if echo "$hciconfig_out" | grep -q "UP"; then
                echo "  Power            : ON"
            else
                echo "  Power            : OFF"
            fi

            hcifeatures=$(hciconfig "$hci_name" features 2>/dev/null || true)

            if echo "$hcifeatures" | grep -qiE "le support|le and"; then
                echo "  BLE Support      : YES"
            else
                echo "  BLE Support      : NO"
            fi

            if echo "$hcifeatures" | grep -qi "EDR"; then
                echo "  BR/EDR           : YES"
            else
                echo "  BR/EDR           : NO"
            fi
        elif [ $HAS_BTMGMT -eq 1 ]; then
            # hciconfig 不可用时用 btmgmt
            btmgmt_out=$(btmgmt info 2>/dev/null || true)

            if [ -n "$btmgmt_out" ]; then
                if echo "$btmgmt_out" | grep -q "powered"; then
                    echo "  Power            : ON"
                else
                    echo "  Power            : OFF"
                fi

                if echo "$btmgmt_out" | grep -q "le "; then
                    echo "  BLE Support      : YES"
                else
                    echo "  BLE Support      : NO"
                fi

                if echo "$btmgmt_out" | grep -q "bredr"; then
                    echo "  BR/EDR           : YES"
                else
                    echo "  BR/EDR           : NO"
                fi
            else
                echo "  Power            : (unable to detect)"
                echo "  BLE Support      : (unable to detect)"
                echo "  BR/EDR           : (unable to detect)"
            fi
        else
            echo "  Power            : (no tool available)"
            echo "  BLE Support      : (no tool available)"
            echo "  BR/EDR           : (no tool available)"
        fi

        index=$((index+1))
    done
fi

# ==========================================
# 5. 用户权限
# ==========================================
echo ""
echo "[Permissions]"
if groups 2>/dev/null | grep -qw "bluetooth"; then
    check_pass "User in bluetooth group"
else
    check_warn "User not in bluetooth group (systemd service may use SupplementaryGroups)"
fi

# ==========================================
# 总结
# ==========================================
echo ""
echo "========================================="
if [ $ERRORS -eq 0 ]; then
    echo "Result: PASS ($WARNINGS warnings)"
    exit 0
else
    echo "Result: FAIL ($ERRORS errors, $WARNINGS warnings)"
    exit 1
fi
