# CUKTECH 10 GaN Charger Ultra - Home Assistant Integration

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1+-green.svg)](https://www.home-assistant.io/)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kairui1108&repository=cuktech-ble-ha-integration&category=integration)
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cuktech_charger)

通过 BLE（低功耗蓝牙）将 CUKTECH 10 GaN Charger Ultra 充电器接入 Home Assistant，实现实时功率监控、端口控制和自动化。

## 效果预览

![HA Integration](./docs/ha_integration.png)

![HA Lovelace](./docs/ha_lovelace.png)

## 功能特性

### BLE Server
- **实时功率监控**：通过 MQTT 推送电压、电流、功率数据
- **功率曲线图**：Web UI 实时显示各端口及总功率曲线
- **端口控制**：远程开关 C1/C2/C3/A 端口
- **倒计时设置**：为每个端口设置充电倒计时（支持自定义分钟数）
- **设置管理**：场景模式、息屏时间、语言等设置
- **BLE 自动重连**：断开后自动重连，指数退避策略
- **MQTT LWT**：崩溃时自动通知 HA 设备离线
- **SQLite 历史数据**：端口数据持久化存储，支持统计和导出

### HA Integration
- **BLE 连接控制**：开关实体控制 BLE 连接/断开，二进制传感器显示连接状态
- **端口传感器**：电压、电流、功率、协议类型
- **端口控制**：开关控制 C1/C2/C3/A 端口
- **设置管理**：场景模式、息屏时间、语言等选择器
- **倒计时设置**：数字实体控制各端口充电倒计时
- **设备信息同步**：型号、固件版本从 BLE 服务器实时同步

### Web 管理界面
- 实时功率曲线图（各端口 + 总功率）
- 端口开关控制
- BLE 连接/断开控制
- 设备设置管理
- 倒计时设置（支持自定义和快捷选择）
- 日志级别管理

### 已知限制

- **单设备**：当前架构仅支持同时连接一个充电器，多设备支持将在后续版本更新
- **协议检测**：充电协议（PD/QC/USB-A 等）基于端口电压和 PDO 数据推断，仅供参考，可能与实际协议不完全一致
- **协议开关限制**：C1 协议开关已可用，但可能间接影响 C2 端口协议（需手动通过米家 App 恢复）；C2/C3/A 的 PD/PPS 开关暂不可用，请使用米家 App 操作。详见 [开发文档](docs/develop-notes.md)
- **平台支持**：开发与测试均基于 Linux 环境，其他平台（macOS、Windows）的兼容性未经验证，使用风险自行承担

## 架构说明

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  CUKTECH 10 GaN │───▶│   BLE Server    │───▶│  Home Assistant │
│     Charger     │ BLE │  (Port 8199)    │ MQTT│    Integration  │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                              │                         │
                              │ HTTP API / Web UI       │ MQTT
                              ▼                         ▼
                        ┌─────────────┐         ┌─────────────┐
                        │  Web 界面   │         │   Dashboard  │
                        │  + Shell API│         │   Buttons   │
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
│   │   ├── protocol.py            # BLE 协议常量和工具
│   │   ├── controller.py          # BLE 连接和命令处理
│   │   └── cli.py                 # CLI 用户界面
│   ├── ha_server.py               # HTTP API + MQTT 服务
│   ├── ble_manager.py             # BLE 连接管理
│   ├── state.py                   # 状态管理
│   ├── history.py                 # SQLite 历史数据
│   ├── config.py                  # 配置（支持 YAML）
│   ├── config.yaml.example        # 配置模板
│   ├── check_env.sh               # 环境检查脚本
│   ├── cuktech_ctl.sh             # 服务控制脚本
│   ├── web/
│   │   └── index.html             # Web 前端界面
│   ├── tests/                     # 单元测试 (101 tests)
│   └── systemd/                   # systemd 服务配置
│
├── ha_integration/                # HA 自定义集成
│   └── custom_components/cuktech_charger/
│       ├── __init__.py            # Coordinator
│       ├── binary_sensor.py       # 端口状态 + BLE 连接状态
│       ├── config_flow.py         # 配置流程（支持 reauth）
│       ├── const.py               # 常量定义
│       ├── manifest.json
│       ├── number.py              # 倒计时数字实体
│       ├── select.py              # 选择器实体
│       ├── sensor.py              # 传感器实体
│       ├── switch.py              # 开关实体 + BLE 连接控制
│       ├── strings.json           # 英文翻译
│       ├── translations/          # 多语言翻译
│       ├── brand/                 # HACS 品牌图标
│       └── icon.png
│
├── docs/                          # 文档
│   ├── integration-readme.md
│   └── integration-readme-en.md
│
├── LICENSE
├── README.md
├── RELEASE_NOTES.md
└── bump-version.sh
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

### 2. 检查环境

```bash
cd ble_server
./check_env.sh
```

确认 Python、蓝牙适配器、BLE 支持等全部通过。

### 3. 部署 BLE Server

```bash
cd ble_server

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

### 4. 安装 HA 集成

**方式 A：HACS 安装（推荐）**

1. 点击上方 **[Open in HACS]** 按钮，将本仓库添加为自定义集成
2. 安装后重启 Home Assistant
3. 点击 **[Add integration]** 按钮，搜索 "CUKTECH Charger" 添加

**方式 B：手动安装**

```bash
cp -r ha_integration/custom_components/cuktech_charger /config/custom_components/
```

重启 Home Assistant。

## 实体说明

| 实体类型 | 实体名 | 功能 |
|----------|--------|------|
| switch | 连接控制 | BLE 连接/断开 |
| binary_sensor | 连接状态 | BLE 连接状态 |
| sensor | 端口电压/电流/功率 | 实时监控 |
| sensor | 端口协议 | PD/QC/USB-A |
| sensor | 总功率 | 所有端口功率之和 |
| switch | 端口控制 | 开关 C1/C2/C3/A |
| select | 场景模式 | AI/数码/单口/均衡 |
| select | 息屏时间 | 5分钟/1分钟/10分钟等 |
| number | 倒计时 | 各端口充电倒计时 |

## API 接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 获取充电器状态 |
| `/api/enable` | POST | 启用/禁用 BLE 连接 |
| `/api/set` | POST | 设置 PIID 值 |
| `/api/port` | POST | 控制端口开关 |
| `/api/chart` | GET | 获取图表数据 |
| `/api/history/{port}` | GET | 查询历史数据 |
| `/api/statistics/{port}` | GET | 统计分析 |
| `/api/export/{port}` | GET | CSV 导出 |
| `/api/log-level` | GET/POST | 日志级别管理 |

## MQTT 主题

| 主题 | 说明 |
|------|------|
| `cuktech/charger/port/{c1\|c2\|c3\|a}` | 端口数据（推送） |
| `cuktech/charger/settings` | 设置数据（retain） |
| `cuktech/charger/status` | 连接状态（retain + LWT） |
| `cuktech/charger/set` | 设置命令（订阅） |
| `cuktech/charger/port` | 端口控制命令（订阅） |

## 依赖

### BLE Server
- Python 3.10+
- bleak >= 0.21
- paho-mqtt >= 2.0
- aiohttp >= 3.9
- cryptography >= 41
- pyyaml >= 6.0

### HA Integration
- Home Assistant 2024.1+
- MQTT（集成依赖）

## 测试

```bash
# BLE Server (101 tests)
cd ble_server && .venv/bin/python -m pytest tests/

# HA Integration (70 tests)
cd ha_integration && python -m pytest tests/
```

## 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

## 致谢

- [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) - BLE 协议参考实现
- [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) - 协议检测参考
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) - 小米设备 Token 提取工具
- [bleak](https://github.com/hbldh/bleak) - BLE 通信库
- [paho-mqtt](https://eclipse.dev/paho/) - MQTT 客户端
