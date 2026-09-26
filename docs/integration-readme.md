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
- **充电量限额**：为端口设置"充到指定 Wh 自动断电"，支持一次性 / 长期有效两种模式，并显示本会话已充电量与剩余量（详见下文）

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
| `number.cuktech_charger_c1_charge_limit` | C1 充电量限额（0=关闭） | 0-1000 Wh |
| `number.cuktech_charger_c2_charge_limit` | C2 充电量限额（0=关闭） | 0-1000 Wh |
| `number.cuktech_charger_c3_charge_limit` | C3 充电量限额（0=关闭） | 0-1000 Wh |
| `number.cuktech_a_charge_limit` | A 充电量限额（0=关闭） | 0-1000 Wh |

### 充电量限额实体

每端口三个实体，共 12 个：

| 平台 | 实体后缀 | 说明 |
|------|----------|------|
| `number` | `_{port}_charge_limit` | 限额阈值（Wh），**0 = 关闭该端口限额** |
| `select` | `_{port}_charge_limit_mode` | `once`（达标后自动失效）/ `always`（长期有效） |
| `sensor` | `_{port}_session_energy` | 本会话已输出能量（Wh），附 `remaining_wh` / `limit_wh` / `limit_mode` / `is_charging` 等属性 |

> 上表的 `entity_id` 前缀由**设备名**决定（本集成默认设备名是中文产品名，因此
> 实际 id 形如 `sensor.ku_tai_ke_10hao_..._c1_session_energy`）。若想要简短 id，
> 在 HA 里把设备名改成英文即可。本页其余表格用的是同一套示意约定。

## 充电量限额

达到阈值后集成通过 MQTT 端口控制通道关断该端口——该通道 Python BLE 服务器与
ESP32 固件均已实现，因此**两种后端下限额功能都可用，无需修改固件**。

限额单位 (Wh) 的语义是「充电器输出能量」（V×I 梯形积分），不是被充设备实际
充入的电量——线损与转换损耗使后者偏小（典型 5~15%）。与 Web UI / REST API
的 `/api/charge-limits` 完全一致。

### 两种工作模式

集成启动时探测后端是否支持 `GET /api/charge-limits`，据此选择后端：

| 后端 | 触发条件 | 配置存储 | 显示数据来源 | 适用 |
|------|----------|----------|--------------|------|
| `server`（委派） | HTTP 返回 200 | Python 服务器（与 Web UI 双向同步） | 服务端快照（含 `session_wh` / `is_charging`） | Python BLE 服务器 |
| `local`（本地计量） | 返回 404 / 无法连接 | HA `Store` 本地持久化 | HA 本地积分（1Hz MQTT 采样） | ESP32 固件、或服务器离线时 |

**计量与执行是两件事**：无论哪种后端，HA 都会用 MQTT 上的 1Hz 端口数据本地积分
（这样读数才有实时性，也保证 REST 掉线时功能不中断）；后端只决定*谁拥有配置、
谁负责断电*。委派模式下实体显示服务端快照，因此与 Web UI 完全一致。

ESP32 固件不做电量统计（只推送瞬时 V/I/P），因此**必然**走 `local`：积分由 HA
完成——这份 1Hz 数据在两种后端下完全相同，所以精度与 Python 端一致。本地模式的
实质是"执行通道是共用的，只有计量与判定搬到了 HA 侧"。

判断当前处于哪种后端：任一限额实体的 `backend` 属性，或 HA 日志中启动时的那行
`Charge limits delegated to BLE server (...)` / `Charge limits metered locally (no BLE
server API at ...: ...)`（后者会带上回退原因）。

`mode` 语义（与 Python 端同名表格一致）：

| mode | 达到阈值 | 会话未达标即结束（拔插/手动关端口/充满） | BLE 抖动重连 / HA 重启 |
|---|---|---|---|
| `once` | 关断并清零 | 清零 | 保留 |
| `always` | 关断，保留待下次充电 | 保留 | 保留 |

### 使用建议

- **只在其中一处设置限额**：若同时在 Web UI 和 HA 设置了不同阈值，两边会各自
  独立判定，谁先达标谁先关断（无害，但剩余量显示会不一致）。用 Python 服务器
  时推荐用集成的委派模式，即只在一处维护。
- **超冲量**：约 0.05 Wh（100W 负载），取决于 1Hz 采样与命令往返。
- **HA 重启**：正在进行的会话其累计电量无法恢复（重启期间没采样），会从 0
  重新累计——**方向是"宁可多充一点，也不会误提前关断"**。长期累计值保留。
- 本集成的 `session_energy` 是**按会话**归零的量，不适合直接接入 HA 能源面板
  （那需要一个单调递增的 kWh 传感器）；两者是独立的议题。
- **归零时机**：会话结束（拔插 / 关断 / 充满）时**不清零**，要等**下一次会话开始**
  才归零。所以端口空闲时该传感器读到的是"上一次充了多少"——写自动化时别把它当成
  "当前正在充多少"，配合 `is_charging` 属性判断更稳。这与 Python 端语义一致。

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
- **充电协议检测**：协议显示以固件推送（PIID 17/18，与米家 App 一致）为准，协商变更时即时更新，无周期性刷新滞后；仅在极端冷启动（PIID 17 从未收到且读取失败）时降级为基于电压的粗略推断
- **平台支持**：开发与测试均基于 Linux 环境，其他平台（macOS、Windows）的兼容性未经验证，使用风险自行承担

## 致谢

- [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) - BLE 协议参考实现
- [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) - 协议检测参考
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) - 小米设备 Token 提取工具
- [bleak](https://github.com/hbldh/bleak) - BLE 通信库
- [paho-mqtt](https://eclipse.dev/paho/) - MQTT 客户端

## 许可证

MIT License
