# ESP32 BLE 固件发布说明

## v1.0.0

### 功能
- BLE 连接酷态科充电器（MiOT 协议）
- 加密通信（AES-CCM + HKDF + HMAC-SHA256）
- MQTT 数据发布（QoS 1, retain）
- Web 配置页面：首次启动 AP 配网，浏览器配置凭据
- Web 仪表盘：实时端口电压 / 电流 / 功率
- 端口开关控制
- 协议开关（PD / PPS / UFCS / SCP）
- 场景模式切换
- BLE 连接开关
- HTTP OTA 更新
- 自动重连

### 硬件支持
- ESP32 / ESP32-S3 / ESP32-C3

### 构建
- ESP-IDF v5.3
- NimBLE Central
- ESP-MQTT + cJSON + mbedTLS
