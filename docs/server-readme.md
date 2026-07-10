# CUKTECH 10 GaN Charger Ultra - BLE Server

> **[English](README.en.md)**

独立的 BLE 服务器，用于连接 CUKTECH 充电器并通过 MQTT 推送实时数据到 Home Assistant。

## 功能特性

- **BLE 连接与 MiOT 认证**：自动连接充电器，支持断线自动重连
- **实时数据推送**：通过 MQTT 发布电压、电流、功率、协议等数据
- **协议检测**：自动识别 PD / PD Fixed / PD PPS / QC / USB-A 充电协议
- **Web 管理界面**：实时功率曲线图、端口控制、设备设置，支持 6 种主题
- **HTTP API**：提供 RESTful 接口供外部系统调用
- **CORS 支持**：跨域请求已配置，可嵌入 HA html-card 或 iframe

## 系统要求

- Python 3.10+
- Linux 系统（需蓝牙适配器）
- MQTT Broker（如 EMQX、Mosquitto）

## 快速开始

### 1. 获取设备 Token

使用 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) 从米家云端获取：

```bash
pip install xiaomi_cloud_tokens_extractor
python -m xiaomi_cloud_tokens_extractor
```

选择你的 CUKTECH 充电器，获取：
- `MAC` - 设备蓝牙 MAC 地址（如 `3C:CD:73:34:AE:59`）
- `Token` - 设备 Token（12 字节 hex）
- `BLE Key` - BLE 认证密钥（16 字节 hex）

### 2. 安装

```bash
git clone https://github.com/kairui1108/cuktech-ble-server.git
cd cuktech-ble-server

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. 配置

复制 `config.yaml.example` 为 `config.yaml` 并填入你的配置：

```yaml
ble:
  mac: "3C:CD:73:34:AE:59"
  token: "your_token_here"
  ble_key: "your_ble_key_here"

mqtt:
  host: "192.168.1.63"
  port: 1883
  username: "admin"
  password: "your_password"
  keepalive: 60
  topic_prefix: "cuktech/charger"

server:
  host: "0.0.0.0"
  port: 8199
  command_timeout: 10.0
  reconnect_base_delay: 1.0
  reconnect_max_delay: 300.0
  settings_refresh_interval: 60.0
```

也可通过环境变量配置（优先级高于 config.yaml）：

```bash
export CUKTECH_DEVICE_MAC="3C:CD:73:34:AE:59"
export CUKTECH_DEVICE_TOKEN="your_token"
export CUKTECH_DEVICE_BLE_KEY="your_ble_key"
export MQTT_HOST="192.168.1.63"
export MQTT_PORT="1883"
export MQTT_USER="admin"
export MQTT_PASS="your_password"
```

### 4. 启动

```bash
./cuktech_ctl.sh start
```

## Web 管理界面

访问 `http://<服务器IP>:8199/`

### 功能

- **设备信息**：连接状态、BLE 控制、总功率、最高电压
- **功率曲线**：Chart.js 实时折线图，显示各端口及总功率趋势
- **端口监控**：C1/C2/C3/A 四端口独立控制，显示电压、电流、功率、协议
- **设备设置**：场景模式、息屏时间、语言等下拉框控制
- **倒计时设置**：快速按钮 + 自定义分钟输入

### 主题

支持 6 种主题切换：暗色、深蓝、海洋、灰色、浅色、跟随系统

## MQTT Topics

| Topic | 方向 | 说明 |
|-------|------|------|
| `cuktech/charger/port/c1` | 发布 | C1 端口数据（JSON） |
| `cuktech/charger/port/c2` | 发布 | C2 端口数据（JSON） |
| `cuktech/charger/port/c3` | 发布 | C3 端口数据（JSON） |
| `cuktech/charger/port/a` | 发布 | A 端口数据（JSON） |
| `cuktech/charger/settings` | 发布 | 设备设置（retain） |
| `cuktech/charger/status` | 发布 | 连接状态 |
| `cuktech/charger/set` | 订阅 | 设置命令 |
| `cuktech/charger/port` | 订阅 | 端口控制命令 |

端口数据格式：
```json
{
  "voltage": 20.1,
  "current": 2.5,
  "power": 50.25,
  "protocol": "PD",
  "active": true
}
```

## HTTP API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 获取充电器完整状态 |
| `/api/enable` | POST | 启用/禁用 BLE 连接 `{"enabled": true/false}` |
| `/api/set` | POST | 设置 PIID 值 `{"piid": N, "value": V}` |
| `/api/port` | POST | 控制端口开关 `{"port": "c1", "on": true}` |
| `/api/countdown` | POST | 设置倒计时 `{"port": "c1", "minutes": 30}` |

## 服务管理

```bash
./cuktech_ctl.sh start     # 启动
./cuktech_ctl.sh stop      # 停止
./cuktech_ctl.sh restart   # 重启
./cuktech_ctl.sh status    # 查看状态
./cuktech_ctl.sh log       # 查看日志
```

### 开机自启

**方式 A：systemd（推荐）**

```bash
cd systemd && ./install-service.sh
```

**方式 B：crontab**

```bash
@reboot /path/to/cuktech_ctl.sh start
```

## 架构

```
CUKTECH 充电器 ←BLE→ BLE Server ←MQTT→ Home Assistant
                         ↓
                    Web UI / HTTP API
```

BLE Server 作为独立进程运行，通过 BLE 连接充电器获取数据，经 MQTT 推送到 Home Assistant。Web UI 和 HTTP API 由同一进程提供服务。

## 协议支持

| 协议 | 说明 |
|------|------|
| PD | USB Power Delivery |
| PD Fixed | PD 固定电压档位（5/9/12/15/20V） |
| PD PPS | PD 可编程电源 |
| QC | Quick Charge |
| USB-A | USB-A 充电（DCP） |
| idle | 无设备连接 |

## 致谢

- [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) - BLE 协议参考实现
- [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) - 协议检测参考
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) - 小米设备 Token 提取工具
- [bleak](https://github.com/hbldh/bleak) - BLE 通信库
- [paho-mqtt](https://eclipse.dev/paho/) - MQTT 客户端

## 许可证

MIT License
