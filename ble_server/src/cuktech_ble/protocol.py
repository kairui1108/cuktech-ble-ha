"""
CUKTECH 10 GaN Charger Ultra - BLE direct controller
===================================================
Connects to the charger over local Bluetooth LE without cloud access.

Configuration is read from environment variables:
  CUKTECH_DEVICE_MAC       Example: AA:BB:CC:DD:EE:FF
  CUKTECH_DEVICE_TOKEN     12-byte hex token from your own device
  CUKTECH_DEVICE_BLE_KEY   16-byte hex BLE key from your own device

BLE GATT 服务/特征:
  Service 0xFE95 (Xiaomi MiOT):
    0x001f (char 0x001c) - 设备信息 (Write NoResp / Notify)
    0x000d (char 0x0010) - 认证控制 (Write NoResp / Notify)
    0x0010 (char 0x0019) - 认证数据 (Write NoResp / Notify)
    0x0019 (char 0x001a) - 命令通道 (Write NoResp / Notify)
    0x001c (char 0x001b) - 数据通道 (Write NoResp / Notify)
    0x0008 (char 0x0004) - 固件版本 (Read)

MiOT Spec (SIID=2, charger service):
  PIID 1-4:   端口信息 C1/C2/C3/A (read/notify)
  PIID 5:     场景模式 1=AI,2=Apple,3=Single,4=Balance (read/write)
  PIID 6:     息屏时间 0=5min,1=10min,2=30min,3=OFF,4=1min (read/write)
  PIID 7:     协议控制 (read/write)
  PIID 8:     倒计时设置 (read/write)
  PIID 9-12:  各端口倒计时(分钟) (read/write)
  PIID 13:    语言 0=EN,1=CN (read/write)
  PIID 14:    进入界面 1-5 (write only)
  PIID 15:    USB-A常通电 (read/write)
  PIID 16:    端口控制 (read/write)
  PIID 19:    空闲息屏 (read/write)
  PIID 20:    屏幕方向锁定 (read/write)

Usage:
  python cuktech_ble.py scan              # 扫描查找设备
  python cuktech_ble.py info              # 读取设备信息(无需认证)
  python cuktech_ble.py auth              # 测试认证流程
  python cuktech_ble.py status            # 读取充电器状态
  python cuktech_ble.py set-mode ai       # 设置场景模式
  python cuktech_ble.py set-screen 10     # 设置息屏时间
  python cuktech_ble.py set-language cn   # 设置语言
"""

import argparse
import asyncio
import hashlib
import io
import logging
import os
import secrets
import struct
import sys
import time

_LOGGER = logging.getLogger("cuktech_ble")

# 修复 Windows 控制台中文乱码
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

try:
    from bleak import BleakClient, BleakScanner
except ImportError as exc:
    BleakClient = None
    BleakScanner = None
    _BLEAK_IMPORT_ERROR = exc
else:
    _BLEAK_IMPORT_ERROR = None

try:
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.hmac import HMAC as CryptoHMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
except ImportError as exc:
    HKDF = None
    CryptoHMAC = None
    hashes = None
    default_backend = None
    AESCCM = None
    _CRYPTO_IMPORT_ERROR = exc
else:
    _CRYPTO_IMPORT_ERROR = None


def mac_str_to_bytes(mac_str):
    """MAC 地址字符串转字节数组 (reversed for Xiaomi protocol)。"""
    parts = mac_str.replace("-", ":").split(":")
    return bytes([int(p, 16) for p in reversed(parts)])


def require_runtime_dependencies():
    """Raise a clear error if optional BLE runtime dependencies are missing."""
    missing = []
    try:
        import bleak
    except ImportError:
        missing.append("bleak")
    try:
        import cryptography
    except ImportError:
        missing.append("cryptography")
    if missing:
        raise RuntimeError(
            "Missing runtime dependencies: "
            + ", ".join(missing)
            + ". Install them with `python -m pip install -e .`."
        )


# ============================================================
# Device configuration
# ============================================================
PLACEHOLDER_DEVICE_MAC = "AA:BB:CC:DD:EE:FF"


def _env_hex_bytes(name, default_hex, expected_len):
    """Read a fixed-size hex value from the environment."""
    value = os.environ.get(name, "").strip()
    if not value:
        return bytes.fromhex(default_hex)
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a hex string") from exc
    if len(raw) != expected_len:
        raise ValueError(f"{name} must decode to {expected_len} bytes")
    return raw


DEVICE_MAC = os.environ.get("CUKTECH_DEVICE_MAC", PLACEHOLDER_DEVICE_MAC).strip() or PLACEHOLDER_DEVICE_MAC
DEVICE_TOKEN = _env_hex_bytes("CUKTECH_DEVICE_TOKEN", "000000000000000000000000", 12)
DEVICE_BLE_KEY = _env_hex_bytes("CUKTECH_DEVICE_BLE_KEY", "00000000000000000000000000000000", 16)
PRODUCT_ID = 0x660e  # 26126

# GATT Handles (从 BLE 日志分析得出)
HANDLE_DEVICE_INFO = 0x001f       # 设备信息查询 (char 0x001c)
HANDLE_AUTH_CTRL = 0x000d         # 认证控制 (char 0x0010)
HANDLE_AUTH_DATA = 0x0010         # 认证数据 (char 0x0019)
HANDLE_CMD_SEND = 0x0019          # 命令发送 (char 0x001a)
HANDLE_CMD_RECV = 0x001c          # 命令接收 (char 0x001b)
HANDLE_FW_VERSION = 0x0008        # 固件版本 (char 0x0004)

# GATT Characteristic UUIDs (16-bit short UUIDs under 0xFE95 service)
UUID_FE95 = "0000fe95-0000-1000-8000-00805f9b34fb"
CHAR_DEVICE_INFO = "0000001c-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_CTRL = "00000010-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_DATA = "00000019-0000-1000-8000-00805f9b34fb"
CHAR_CMD_SEND = "0000001a-0000-1000-8000-00805f9b34fb"
CHAR_CMD_RECV = "0000001b-0000-1000-8000-00805f9b34fb"
CHAR_FW_VERSION = "00000004-0000-1000-8000-00805f9b34fb"

# Notification CCCD handles
CCCD_DEVICE_INFO = 0x0020
CCCD_AUTH_DATA = 0x0011
CCCD_AUTH_CTRL_NOTIFY = 0x000e
CCCD_CMD_SEND = 0x001a
CCCD_CMD_RECV = 0x001d

# MiOT 常量
SIID_CHARGER = 2

# 属性定义表 (SIID=2)
PIID_NAMES = {
    1: 'C1口数据', 2: 'C2口数据', 3: 'C3口数据', 4: 'A口数据',
    5: '场景模式', 6: '息屏时间', 7: '协议控制', 8: '倒计时设置',
    9: 'C1口倒计时', 10: 'C2口倒计时', 11: 'C3口倒计时', 12: 'A口倒计时',
    13: '语言', 14: '进入界面', 15: 'USB-A常通电', 16: '端口控制',
    17: '未知-17', 18: '未知-18', 19: '空闲息屏', 20: '屏幕方向锁',
}

PIID_DISPLAY = {
    5:  {1: 'AI模式', 2: '数码生态', 3: '单口模式', 4: '均衡模式'},
    6:  {0: '5分钟', 1: '1分钟(旧)', 2: '10分钟', 3: '30分钟', 4: '常亮', 5: '1分钟'},
    13: {0: 'English', 1: '中文'},
    15: {0: '关闭', 1: '开启'},
    19: {0: '关闭', 1: '开启'},
    20: {0: '关闭', 1: '开启'},
}

TIMER_PORTS = {'c1': 9, 'c2': 10, 'c3': 11, 'a': 12}

# 端口控制位掩码 (PIID 16): bit0=C1, bit1=C2, bit2=C3, bit3=A
PORT_BITS = {'c1': 0, 'c2': 1, 'c3': 2, 'a': 3}

PROTOCOL_NAMES = {
    0x01: "PD", 0x03: "PD", 0x04: "PD", 0x05: "PD", 0x06: "PD",
    0x07: "PD Fixed", 0x08: "PD PPS", 0x0a: "PD", 0x0b: "PD",
    0x30: "PD", 0x60: "USB-A", 0x70: "QC", 0x80: "PD",
}

PD_FIXED_VOLTAGES = {5.0, 9.0, 12.0, 15.0, 20.0}

PDO_KIND_BY_HIGH_BYTE = {
    0x07: "PD Fixed",
    0x08: "PD PPS",
}


