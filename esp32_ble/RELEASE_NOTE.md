# ESP32 BLE 固件发布说明

## v1.0.2

### 变更
- **保活机制优化**：从 60s 定时发布改为 ping/pong（hassping topic）
  - 每 30s 发送 ping，20s 超时检测
  - 连续 3 次 ping 丢失自动重连
  - 连接后发布初始状态到巴法云
- **启动宽限期**：从 5s 增加到 10s，每次重连/断开重新激活，防止回声导致 BLE 被误禁用
- **DNS 预解析**：HTTP 注册前先解析 `api.bemfa.com`，失败则等待重试，避免 HTTP 0 错误
- **状态缓存保护**：`portMUX` 保护 `_port_state`/`_ble_state` 读写，避免多任务竞态
- **命令失败不更新缓存**：BLE 断连时命令失败，不会错误更新状态缓存

### 修复
- 修复启动时巴法云回声命令导致 BLE 被误 disable
- 修复 HTTP 注册 DNS 解析失败（HTTP 0）
- 修复保活发布总是 off（改为缓存实际状态）

## v1.0.1

### 新增
- **巴法云接入**：支持小爱同学 / 小度语音控制充电器端口开关，无需安装 HA 集成
  - 5 个设备：C口1开关、C口2开关、C口3开关、USB-A开关、蓝牙开关
  - Topic 自动注册（`hass` + MD5 + `006`），设备名自动设置
  - 60 秒保活机制，发布实际端口状态
  - 启动 5 秒宽限期，过滤巴法云回声命令
  
### 优化
- HTTP 注册添加 5 秒超时
- 注册失败改为 WARN 日志并注明 MQTT 仍可用
- UID 日志脱敏（仅显示前 4 位）

### 构建
- ESP-IDF v5.3.5
- NimBLE Central
- ESP-MQTT + cJSON + mbedTLS

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
- ESP-IDF v5.3.5
- NimBLE Central
- ESP-MQTT + cJSON + mbedTLS
