# CUKTECH 10 GaN Charger Ultra - Home Assistant Integration

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1+-green.svg)](https://www.home-assistant.io/)  


[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kairui1108&repository=cuktech-ble-ha-integration&category=integration)
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cuktech_charger)

通过 BLE（低功耗蓝牙）将 CUKTECH 10 GaN Charger Ultra 充电器接入 Home Assistant，实现实时功率监控、端口控制和自动化。

## 效果预览

### 集成页面

![HA Integration](docs/ha_integration.png)

### Lovelace 仪表盘

![HA Lovelace](docs/ha_lovelace.png)

## 功能特性

- **实时功率监控**：通过 MQTT 推送电压、电流、功率数据
- **功率曲线图**：Web UI 实时显示各端口及总功率曲线
- **端口控制**：远程开关 C1/C2/C3/A 端口
- **倒计时设置**：为每个端口设置充电倒计时（支持自定义分钟数）
- **设置管理**：场景模式、息屏时间、语言等设置
- **自动重连**：BLE 断开后自动重连
- **Web 管理界面**：独立 Web UI，支持实时监控和控制
- **YAML 配置**：支持 config.yaml 配置文件，也支持环境变量

## 架构说明

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  CUKTECH 10 GaN │────▶│   BLE Server    │────▶│  Home Assistant │
│     Charger     │ BLE │  (Port 8199)    │ MQTT│    Integration  │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                              │                         │
                              │ HTTP API / Web UI       │ MQTT
                              ▼                         ▼
                        ┌─────────────┐         ┌─────────────┐
                        │  Web 界面   │         │   Dashboard  │
                        │  + Shell API│         │   Buttons    │
                        └─────────────┘         └─────────────┘
```

- **BLE Server**：运行在主机上，负责 BLE 连接、数据采集、MQTT 推送和 Web 界面
- **HA Integration**：订阅 MQTT 数据，在 HA 中创建实体
- **Web UI**：内置管理界面，支持实时监控、端口控制、倒计时设置

## 目录结构

```
cuktech-ble-ha/
├── ble_server/                    # BLE 服务端
│   ├── src/cuktech_ble/
│   │   ├── __init__.py
│   │   ├── protocol.py              # BLE 协议常量和工具
│   │   ├── controller.py            # BLE 连接和命令处理
│   │   └── cli.py                   # CLI 用户界面
│   ├── ha_server.py               # HTTP API + MQTT 服务
│   ├── ble_manager.py             # BLE 连接管理
│   ├── state.py                   # 状态管理
│   ├── history.py                 # SQLite 历史数据
│   ├── config.py                  # 配置（支持 YAML）
│   ├── config.yaml.example        # 配置模板
│   ├── web/
│   │   └── index.html             # Web 前端界面
│   ├── tests/                     # 单元测试 (91 tests)
│   │   ├── test_protocol.py
│   │   ├── test_controller.py
│   │   ├── test_ble_manager.py
│   │   ├── test_ha_server.py
│   │   ├── test_history.py
│   │   ├── test_config.py
│   │   └── test_state.py
│   ├── requirements.txt
│   ├── pyproject.toml
│   └── systemd/                   # systemd 服务配置
│
├── ha_integration/                # HA 自定义集成
│   └── custom_components/cuktech_charger/
│       ├── __init__.py
│       ├── binary_sensor.py
│       ├── config_flow.py
│       ├── const.py
│       ├── manifest.json
│       ├── number.py
│       ├── select.py
│       ├── sensor.py
│       ├── strings.json
│       └── switch.py
│
├── ha_config/                     # HA 配置示例
│   ├── cuktech_api.sh             # HTTP API 桥接脚本
│   ├── cuktech_ctl.sh             # 状态查询脚本
│   ├── cuktech_buttons.yaml       # 按钮模板
│   └── shell_command.yaml         # Shell 命令定义
│
├── LICENSE
└── README.md
```

## 安装步骤

### 1. 获取设备 Token 和 BLE Key

使用 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) 从米家云端获取设备信息：

```bash
pip install xiaomi_cloud_tokens_extractor
python -m xiaomi_cloud_tokens_extractor
```

选择你的 CUKTECH 充电器，获取：
- `MAC` - 设备蓝牙 MAC 地址
- `Token` - 设备 Token（12 字节 hex）
- `BLE Key` - BLE 认证密钥（16 字节 hex）

### 2. 部署 BLE Server

```bash
git clone https://github.com/kairui1108/cuktech-ble-ha.git
cd cuktech-ble-ha/ble_server

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

#### 配置方式（二选一）

**方式 A：YAML 配置文件（推荐）**

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml 填入你的配置
```

**方式 B：环境变量**

```bash
export CUKTECH_DEVICE_MAC="XX:XX:XX:XX:XX:XX"
export CUKTECH_DEVICE_TOKEN="your_token_here"
export CUKTECH_DEVICE_BLE_KEY="your_ble_key_here"
export MQTT_HOST="your_mqtt_broker"
export MQTT_PORT="1883"
export MQTT_USER="your_username"
export MQTT_PASS="your_password"
```

> 优先级：环境变量 > config.yaml

```bash
./cuktech_ctl.sh start
```

### 3. 安装 HA 集成

**方式 A：HACS 安装（推荐）**

1. 点击上方 **[Open in HACS]** 按钮，将本仓库添加为自定义集成
2. 安装后重启 Home Assistant
3. 点击 **[Add integration]** 按钮，搜索 "CUKTECH Charger" 添加

**方式 B：手动安装**

```bash
cp -r ha_integration/custom_components/cuktech_charger /config/custom_components/
cp ha_config/cuktech_api.sh /config/
cp ha_config/cuktech_ctl.sh /config/
cp ha_config/shell_command.yaml /config/
```

在 `configuration.yaml` 中添加：

```yaml
shell_command: !include shell_command.yaml
```

重启 Home Assistant。

## Web 管理界面

BLE Server 内置 Web 界面，访问 `http://<BLE_SERVER_IP>:8199/`

**功能：**
- 实时功率曲线图（各端口 + 总功率）
- 端口开关控制
- BLE 连接/断开控制
- 设备设置管理
- 倒计时设置（支持自定义和快捷选择）

## API 接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 获取充电器状态 |
| `/api/enable` | POST | 启用/禁用 BLE 连接 |
| `/api/set` | POST | 设置 PIID 值（带校验） |
| `/api/port` | POST | 控制端口开关 |

### PIID 参考

| PIID | 属性 | 值范围 |
|------|------|--------|
| 5 | 场景模式 | 1-4 (AI/数码/单口/均衡) |
| 6 | 息屏时间 | 0-5 |
| 9-12 | C1/C2/C3/A 倒计时 | 0-1440 分钟 |
| 13 | 语言 | 0-1 (EN/CN) |
| 15 | USB-A 常通电 | 0-1 |
| 16 | 端口控制 | 0-15 (位掩码) |
| 19 | 空闲息屏 | 0-1 |
| 20 | 屏幕方向锁 | 0-1 |

### 示例

```bash
# 获取状态
curl http://localhost:8199/api/status

# 设置场景模式为 AI
curl -X POST http://localhost:8199/api/set -d '{"piid": 5, "value": 1}'

# 设置 C1 倒计时 30 分钟
curl -X POST http://localhost:8199/api/set -d '{"piid": 9, "value": 30}'

# 关闭 C1 端口
curl -X POST http://localhost:8199/api/port -d '{"port": "c1", "action": "off"}'
```

## MQTT 主题

| 主题 | 说明 |
|------|------|
| `cuktech/charger/port/{c1\|c2\|c3\|a}` | 端口数据 |
| `cuktech/charger/settings` | 设置数据 |
| `cuktech/charger/status` | 连接状态 |
| `cuktech/charger/set` | 设置命令 |
| `cuktech/charger/port` | 端口控制命令 |

## 依赖

- Python 3.10+
- bleak >= 0.21
- paho-mqtt >= 2.0
- aiohttp >= 3.9
- cryptography >= 41
- pyyaml >= 6.0
- Home Assistant 2024.1+

## 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

## 贡献

欢迎提交 Issue 和 Pull Request！

## 致谢

- [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) - BLE 协议参考实现
- [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) - 协议检测参考
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) - 小米设备 Token 提取工具
- [bleak](https://github.com/hbldh/bleak) - BLE 通信库
- [paho-mqtt](https://eclipse.dev/paho/) - MQTT 客户端
