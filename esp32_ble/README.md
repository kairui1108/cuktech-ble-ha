# CUKTECH Charger BLE Bridge - ESP32

将酷态科充电器通过 ESP32 接入 Home Assistant。

## 架构

```
充电器 ←BLE→ ESP32 ──MQTT──→ Home Assistant
```

## 文件结构

```
esp32_ble/
├── platformio.ini       # PlatformIO 构建配置
├── src/
│   ├── main.cpp         # 入口：初始化、WiFi、MQTT、主循环
│   ├── config.h         # 配置：WiFi/MQTT/BLE 凭据
│   ├── miot_protocol.h  # MiOT 协议常量 + TLV 编码
│   ├── miot_auth.h      # 认证 + 加密 (HKDF/HMAC/AES-CCM)
│   └── ble_manager.h    # BLE 连接管理器 + 状态机
└── README.md
```

## 硬件要求

- ESP32（原版）/ ESP32-S3 / ESP32-C3
- USB 蓝牙适配器（如果需要外接）
- PlatformIO IDE（VSCode 扩展）或 Arduino IDE

## 使用步骤

### 1. 获取设备凭据

使用 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) 获取：

- MAC 地址
- Token（12 字节 hex）
- BLE Key（16 字节 hex）

### 2. 编辑配置

打开 `src/config.h`，填入：

```cpp
#define WIFI_SSID        "你的WiFi名称"
#define WIFI_PASSWORD    "你的WiFi密码"
#define DEVICE_MAC       "XX:XX:XX:XX:XX:XX"
#define DEVICE_TOKEN     "你的Token_12字节hex"
#define DEVICE_BLE_KEY   "你的BLE_Key_16字节hex"
#define MQTT_BROKER      "你的MQTT服务器IP"
```

### 3. 编译上传

```bash
# 使用 PlatformIO
pio run -t upload

# 或者使用 Arduino IDE
# 选择 ESP32 Dev Module / ESP32-S3 / ESP32-C3
```

### 4. 查看结果

打开串口监视器，看到 `✅ Connected & Ready!` 说明连接成功。

## 状态机

```
IDLE → SCANNING → CONNECTING → AUTHENTICATING → READY (连接成功)
                                  ↓ (失败)
                              RECONNECT → SCANNING (自动重连)
```

## 后续可扩展功能

- [ ] 读取端口数据（电压/电流/功率）
- [ ] 端口开关控制
- [ ] 协议开关控制
- [ ] 场景模式切换
- [ ] Wi-Fi 配网 Portal
- [ ] OTA 更新
