# CUKTECH 10 GaN Charger Ultra - Home Assistant Integration

> **[English](README.en.md)**

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kairui1108&repository=cuktech-ble-ha-integration&category=integration)
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cuktech_charger)

通过 MQTT 将 CUKTECH 充电器数据接入 Home Assistant，提供实时监控、端口控制和自动化支持。

## 前置条件

需要先部署 [BLE Server](https://github.com/kairui1108/cuktech-ble-server)

## 安装

### 通过 HACS（推荐）

1. 点击上方 **[Open in HACS]** 按钮，将本仓库添加为自定义集成
2. 搜索 "CUKTECH Charger" 并安装
3. 重启 Home Assistant
4. 点击 **[Add integration]** 按钮，搜索 "CUKTECH Charger" 添加

### 手动安装

```bash
cp -r custom_components/cuktech_charger /config/custom_components/
```

重启 Home Assistant 后，在集成页面添加配置。

## 配置

添加集成时需要填写：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| 名称 | 集成显示名称 | CUKTECH 10 GaN Charger Ultra |
| 服务器地址 | BLE Server HTTP 地址 | `http://localhost:8199` |

服务器地址变更时支持重新配置（Reauth）。

## 功能特性

- **实时功率监控**：通过 MQTT 推送各端口电压、电流、功率数据
- **协议检测**：自动识别 PD / PD Fixed / PD PPS / QC / USB-A 充电协议
- **BLE 连接控制**：开关实体控制 BLE 连接/断开，二进制传感器显示连接状态
- **端口控制**：远程开关 C1/C2/C3/A 端口
- **协议开关控制**：10 个开关实体，独立控制各端口 PD/PPS/UFCS/SCP 协议
- **场景模式**：AI 智能 / 数码生态 / 单口优先 / 均衡充电
- **倒计时设置**：为每个端口设置充电倒计时（0-1440 分钟）
- **设备设置**：息屏时间、语言、USB-A 小电流、空闲息屏、屏幕方向锁等
- **设备信息同步**：型号、固件版本从 BLE 服务器实时同步
- **充电事件**：充电完成时自动触发 `charge_end` 事件实体，可用于通知自动化和场景联动到 HA
- **实体可用性**：MQTT 状态 + HTTP 健康检查双重检测

## 实体列表

### 二进制传感器（Binary Sensor）

| 实体 | 说明 |
|------|------|
| `binary_sensor.cuktech_charger_c1_active` | C1 活跃状态 |
| `binary_sensor.cuktech_charger_c2_active` | C2 活跃状态 |
| `binary_sensor.cuktech_charger_c3_active` | C3 活跃状态 |
| `binary_sensor.cuktech_a_active` | A 活跃状态 |
| `binary_sensor.cuktech_charger_ble_connected` | BLE 连接状态 |

### 传感器（Sensor）

| 实体 | 说明 | 单位 |
|------|------|------|
| `sensor.cuktech_charger_c1_voltage` | C1 电压 | V |
| `sensor.cuktech_charger_c1_current` | C1 电流 | A |
| `sensor.cuktech_charger_c1_power` | C1 功率 | W |
| `sensor.cuktech_charger_c1_protocol` | C1 协议 | - |
| `sensor.cuktech_charger_c2_voltage` | C2 电压 | V |
| `sensor.cuktech_charger_c2_current` | C2 电流 | A |
| `sensor.cuktech_charger_c2_power` | C2 功率 | W |
| `sensor.cuktech_charger_c2_protocol` | C2 协议 | - |
| `sensor.cuktech_charger_c3_voltage` | C3 电压 | V |
| `sensor.cuktech_charger_c3_current` | C3 电流 | A |
| `sensor.cuktech_charger_c3_power` | C3 功率 | W |
| `sensor.cuktech_charger_c3_protocol` | C3 协议 | - |
| `sensor.cuktech_a_voltage` | A 电压 | V |
| `sensor.cuktech_a_current` | A 电流 | A |
| `sensor.cuktech_a_power` | A 功率 | W |
| `sensor.cuktech_a_protocol` | A 协议 | - |
| `sensor.cuktech_charger_total_power` | 总功率 | W |

### 开关（Switch）

| 实体 | 说明 |
|------|------|
| `switch.cuktech_charger_ble_control` | BLE 连接控制 |
| `switch.cuktech_charger_c1_port` | C1 端口开关 |
| `switch.cuktech_charger_c2_port` | C2 端口开关 |
| `switch.cuktech_charger_c3_port` | C3 端口开关 |
| `switch.cuktech_a_port` | A 端口开关 |
| `switch.cuktech_charger_*_c1_pd` | C1 PD 协议开关 |
| `switch.cuktech_charger_*_c1_pps` | C1 PPS 协议开关（PD 关闭时自动关闭） |
| `switch.cuktech_charger_*_c1_ufcs` | C1 UFCS 协议开关 |
| `switch.cuktech_charger_*_c2_pd` | C2 PD 协议开关 |
| `switch.cuktech_charger_*_c2_pps` | C2 PPS 协议开关（PD 关闭时自动关闭） |
| `switch.cuktech_charger_*_c2_ufcs` | C2 UFCS 协议开关 |
| `switch.cuktech_charger_*_c3_ufcs` | C3 UFCS 协议开关 |
| `switch.cuktech_charger_*_c3_scp` | C3 SCP 协议开关 |
| `switch.cuktech_charger_*_a_ufcs` | USB-A UFCS 协议开关 |
| `switch.cuktech_charger_*_a_scp` | USB-A SCP 协议开关 |

### 选择器（Select）

| 实体 | 说明 | 选项 |
|------|------|------|
| `select.cuktech_scene_mode` | 场景模式 | AI智能 / 数码生态 / 单口优先 / 均衡充电 |
| `select.cuktech_screen_save_time` | 息屏时间 | 5分钟 / 1分钟 / 10分钟 / 30分钟 / 常亮 |
| `select.cuktech_language` | 语言 | English / 中文 |

### 数字（Number）

| 实体 | 说明 | 范围 |
|------|------|------|
| `number.cuktech_charger_c1_countdown` | C1 倒计时设置 | 0-1440 分钟 |
| `number.cuktech_charger_c2_countdown` | C2 倒计时设置 | 0-1440 分钟 |
| `number.cuktech_charger_c3_countdown` | C3 倒计时设置 | 0-1440 分钟 |
| `number.cuktech_a_countdown` | A 倒计时设置 | 0-1440 分钟 |

## 协议说明

| 协议 | 说明 |
|------|------|
| idle | 无设备连接 |
| 5V | USB 5V |
| PD | USB Power Delivery |
| PPS | PD 可编程电源 |
| QC | Quick Charge |
| AFC | Samsung Adaptive Fast Charging |
| FCP | Huawei Fast Charge Protocol |
| SCP | Huawei Super Charge Protocol |
| UFCS | Universal Fast Charging Specification |

## 效果预览

![HA Integration](https://raw.githubusercontent.com/kairui1108/cuktech-ble-ha/main/docs/ha_integration.png)

![HA Lovelace](https://raw.githubusercontent.com/kairui1108/cuktech-ble-ha/main/docs/ha_lovelace.png)


## 故障排除

### 实体显示不可用

- 检查 BLE Server 是否运行：`curl http://<服务器IP>:8199/api/status`
- 检查 MQTT Broker 是否可达
- 通过 HA 实体查看 BLE 连接状态

### 数据不更新

- 确认 BLE Server 已连接充电器（Web UI 显示"已连接"）
- 检查 MQTT 订阅：使用 MQTT Explorer 查看 `cuktech/charger/` topic

### BLE 连接不稳定

- 使用 BLE Server 的 `check_env.sh` 检查蓝牙适配器状态
- 确认用户在 `bluetooth` 组中：`sudo usermod -aG bluetooth $USER`
- BLE Server 日志级别调至 debug 分析认证流程

## 已知限制

- **单设备**：当前架构仅支持同时连接一个充电器，多设备支持将在后续版本更新
- **充电协议检测**：硬件协议码（PIID 17/18）与米家 App 一致，刷新间隔约 60 秒，期间切换协议可能滞后显示；无硬件码时降级为启发式推断
- **平台支持**：开发与测试均基于 Linux 环境，其他平台（macOS、Windows）的兼容性未经验证，使用风险自行承担

## 致谢

- [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) - BLE 协议参考实现
- [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) - 协议检测参考
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) - 小米设备 Token 提取工具
- [bleak](https://github.com/hbldh/bleak) - BLE 通信库
- [paho-mqtt](https://eclipse.dev/paho/) - MQTT 客户端

## 许可证

MIT License
