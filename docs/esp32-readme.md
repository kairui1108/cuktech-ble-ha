# CUKTECH Charger BLE Bridge - ESP32 Firmware

[English](esp32-readme-en.md)

将酷态科充电器通过 ESP32 接入 Home Assistant。

```
充电器 ←BLE→ ESP32 ──MQTT──→ Home Assistant
                         ├── HTTP（配置页 / 仪表盘 / OTA）
                         └── NVS（配置持久化）
```

## Home Assistant 集成

本固件仅提供 ESP32 蓝牙网关功能。需要在 Home Assistant 中查看和控制充电器，请安装对应的集成：

👉 **[cuktech-ble-ha-integration](https://github.com/kairui1108/cuktech-ble-ha-integration)**

该集成支持：
- 端口数据实时显示（电压 / 电流 / 功率）
- 端口开关控制
- 协议开关（PD / PPS / UFCS / SCP）
- 场景模式切换
- BLE 连接状态监控

## 快速开始

### 硬件

- ESP32 / ESP32-S3 / ESP32-C3
- 任意一款即可，无需外接蓝牙适配器

### 1. 烧录固件

从 [Releases](https://github.com/kairui1108/cuktech-ble-ha/releases) 下载预编译固件，或自行编译：

```bash
# 自行编译（需要 ESP-IDF v5.3）
cd esp32_ble
idf.py set-target esp32
idf.py build
idf.py -p /dev/ttyUSB0 flash
```

### 2. 首次配置

烧录后 ESP32 会启动 **AP 配网模式**：

1. 连接 WiFi `CUKTECH-Setup`，密码 `12345678`
2. 浏览器打开 `http://192.168.4.1/`
3. 填写：WiFi 信息、MQTT 服务器、设备凭据
4. 保存后自动重启连接

### 3. 获取设备凭据

需要从米家获取充电器的 BLE 信息：

- **MAC 地址**（6 字节 hex）
- **Token**（12 字节 hex）
- **BLE Key**（16 字节 hex）

> 可使用 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) 获取。

## Web 面板

连接成功后，浏览器访问 ESP32 的 IP 地址即可打开内置仪表盘：

- 实时查看各端口电压 / 电流 / 功率
- 端口开关控制
- 协议开关（PD / PPS / UFCS / SCP）
- 场景模式切换
- BLE 连接开关
- OTA 固件更新

## 文件结构

```
esp32_ble/
├── CMakeLists.txt         # ESP-IDF 构建入口
├── main/
│   ├── main.c             # WiFi / MQTT / HTTP / OTA / 任务调度
│   ├── ble_manager.c/.h   # BLE 状态机 + 认证 + 异步命令
│   ├── miot_auth.c/.h     # 小米 MiOT 认证加密
│   ├── miot_protocol.c/.h # 协议常量 + TLV 编解码
│   ├── config.h           # 编译时默认值
│   ├── config_store.c/.h  # NVS 配置持久化
│   ├── http_server.c/.h   # Web 配置页 + REST API
│   ├── ota_update.c/.h    # HTTP OTA 更新
│   └── queue_msg.h        # 跨任务消息定义
├── partitions.csv         # 分区表（3MB app + OTA）
├── sdkconfig.defaults     # ESP-IDF 默认配置
└── sdkconfig              # 本地 sdkconfig（gitignore）
```

## 构建说明

### 依赖

- [ESP-IDF v5.3](https://docs.espressif.com/projects/esp-idf/en/v5.3/esp32/get-started/index.html)

### 构建

```bash
idf.py set-target esp32    # 或 esp32s3 / esp32c3
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

### 多平台

支持 `set-target esp32s3` 和 `esp32c3`。C3 为单核，任务自动适配。

## 协议

MIT

## 技术栈

| 组件 | 说明 |
|---|---|
| **ESP-IDF v5.3** | 框架 |
| **NimBLE** | BLE 协议栈（Central） |
| **ESP-MQTT** | MQTT 客户端（QoS 1, retain） |
| **lwIP** | TCP/IP 栈 + HTTP Server |
| **mbedTLS** | AES-CCM / SHA256 / HKDF |
| **cJSON** | JSON 编解码 |
