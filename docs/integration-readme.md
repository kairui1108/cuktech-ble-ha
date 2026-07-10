# CUKTECH 10 GaN Charger Ultra - Home Assistant Integration

> **[English](README.en.md)**

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kairui1108&repository=cuktech-ble-ha-integration&category=integration)
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cuktech_charger)

通过 MQTT 将 CUKTECH 充电器数据接入 Home Assistant，提供实时监控、端口控制和自动化支持。

## 效果预览

### 集成页面

![HA Integration](ha_integration.png)

### Lovelace 仪表盘

![HA Lovelace](ha_lovelace.png)

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
| 名称 | 集成显示名称 | CUKTECH Charger |
| 服务器地址 | BLE Server HTTP 地址 | `http://localhost:8199` |

## 功能特性

- **实时功率监控**：通过 MQTT 推送各端口电压、电流、功率数据
- **协议检测**：自动识别 PD / PD Fixed / PD PPS / QC / USB-A 充电协议
- **端口控制**：远程开关 C1/C2/C3/A 端口
- **场景模式**：AI 智能 / Apple 2.4A / 单口优先 / 均衡充电
- **倒计时设置**：为每个端口设置充电倒计时
- **设备设置**：息屏时间、语言、USB-A 常通电等
- **实体可用性**：MQTT 状态 + HTTP 健康检查双重检测，服务器离线时实体自动变为不可用

## 实体列表

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
| `sensor.cuktech_charger_c1_countdown` | C1 倒计时 | min |
| `sensor.cuktech_charger_c2_countdown` | C2 倒计时 | min |
| `sensor.cuktech_charger_c3_countdown` | C3 倒计时 | min |
| `sensor.cuktech_a_countdown` | A 倒计时 | min |
| `sensor.cuktech_idle_screenoff` | 空闲息屏 | - |
| `sensor.cuktech_screen_dir_lock` | 屏幕方向锁 | - |

### 开关（Switch）

| 实体 | 说明 |
|------|------|
| `switch.cuktech_charger_c1_port` | C1 端口开关 |
| `switch.cuktech_charger_c2_port` | C2 端口开关 |
| `switch.cuktech_charger_c3_port` | C3 端口开关 |
| `switch.cuktech_a_port` | A 端口开关 |
| `switch.cuktech_usb_a_always_on` | USB-A 常通电 |
| `switch.cuktech_idle_screenoff` | 空闲息屏 |
| `switch.cuktech_screen_dir_lock` | 屏幕方向锁 |

### 选择器（Select）

| 实体 | 说明 | 选项 |
|------|------|------|
| `select.cuktech_scene_mode` | 场景模式 | AI智能 / Apple 2.4A / 单口优先 / 均衡充电 |
| `select.cuktech_screen_save_time` | 息屏时间 | 5分钟 / 4分钟 / 3分钟 / 2分钟 / 1分钟 |
| `select.cuktech_language` | 语言 | English / 中文 |

### 二进制传感器（Binary Sensor）

| 实体 | 说明 |
|------|------|
| `binary_sensor.cuktech_charger_charger_c1_active` | C1 活跃状态 |
| `binary_sensor.cuktech_charger_charger_c2_active` | C2 活跃状态 |
| `binary_sensor.cuktech_charger_charger_c3_active` | C3 活跃状态 |
| `binary_sensor.cuktech_a_active` | A 活跃状态 |

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
| PD | USB Power Delivery |
| PD Fixed | PD 固定电压档位（5/9/12/15/20V） |
| PD PPS | PD 可编程电源（可调电压） |
| QC | Quick Charge |
| USB-A | USB-A 充电（DCP） |

## 自动化示例

### 充电完成通知

```yaml
automation:
  - alias: "CUKTECH 充电完成通知"
    trigger:
      - platform: state
        entity_id: binary_sensor.cuktech_charger_charger_c1_active
        to: "off"
    condition:
      - condition: template
        value_template: "{{ trigger.from_state.state == 'on' }}"
    action:
      - service: notify.notify
        data:
          message: "CUKTECH C1 端口充电已完成"
```

### 总功率过高告警

```yaml
automation:
  - alias: "CUKTECH 总功率告警"
    trigger:
      - platform: numeric_state
        entity_id: sensor.cuktech_charger_total_power
        above: 100
    action:
      - service: notify.notify
        data:
          message: "CUKTECH 充电器总功率超过 100W：{{ states('sensor.cuktech_charger_total_power') }}W"
```

### 电价时段自动切换场景

```yaml
automation:
  - alias: "谷电时段切换均衡充电"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.cuktech_scene_mode
        data:
          option: "均衡充电"
```

## 故障排除

### 实体显示不可用

- 检查 BLE Server 是否运行：`curl http://<服务器IP>:8199/api/status`
- 检查 MQTT Broker 是否可达
- 检查 HA 日志中的连接错误

### 数据不更新

- 确认 BLE Server 已连接充电器（Web UI 显示"已连接"）
- 检查 MQTT 订阅：使用 MQTT Explorer 查看 `cuktech/charger/` topic

### 端口控制无响应

- 确认 BLE 连接正常
- 检查充电器是否支持该端口控制（部分型号端口数量不同）

## 致谢

- [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) - BLE 协议参考实现
- [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) - 协议检测参考
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) - 小米设备 Token 提取工具
- [bleak](https://github.com/hbldh/bleak) - BLE 通信库
- [paho-mqtt](https://eclipse.dev/paho/) - MQTT 客户端

## 许可证

MIT License
