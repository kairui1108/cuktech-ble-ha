# CUKTECH 10 GaN Charger Ultra - BLE Server

独立的 BLE 服务器，用于连接 CUKTECH 充电器并通过 MQTT 推送实时数据到 Home Assistant。

## 功能特性

- **BLE 连接与 MiOT 认证**：自动连接充电器，支持断线自动重连
- **BLE 连接稳定性**：power cycle 后 LL 断连确认、GATT 就绪等待、指数退避重连
- **实时数据推送**：通过 SSE 事件流实时推送端口数据、状态变更至 Web 前端，MQTT 推送至 HA
- **SSE 事件流**：Server-Sent Events 即时推送端口更新、状态切换、设置变更，替代 2s 轮询
- **协议检测**：自动识别 PD / PD Fixed / PD PPS / QC / USB-A 充电协议
- **协议开关控制**：通过 API 独立控制各端口 PD/PPS/UFCS/SCP 协议开关
- **Web 管理界面**：实时功率曲线图、端口控制、协议控制、设备设置、巴法云启停，支持 6 种主题
- **Web 配置页面**：通过 `config.html` 在线修改配置，支持小米云扫码自动获取设备信息
- **HTTP API**：提供 RESTful 接口供外部系统调用
- **MQTT LWT**：崩溃时自动通知 HA 设备离线
- **巴法云 (Bemfa) 接入**：支持小爱同学/小度音箱语音控制充电器端口
- **充电记录与电量统计**：自动记录充电会话（起止时间、电量、峰值功率），Web UI 查看历史详情
- **电量积分 (Wh)**：基于梯形积分实时累积端口充电能量
- **充电量限额**：为每个端口设置"充到指定 Wh 自动关断"（`once` 一次性 / `always` 长期有效），index/phone 页卡片直接设置
- **SQLite 历史数据**：端口数据持久化存储，支持统计和导出
- **BLE 连接质量评估**：实时评分（0-100），包含解密率、通知响应、连接稳定性等指标
- **充电完成通知**：通过 MQTT 推送充电完成事件，支持 HA 自动化
- **环境检查**：`check_env.sh` 一键检查系统兼容性

## 系统要求

### Docker 部署
- Linux 系统（需蓝牙适配器）
- Docker + Docker Compose

### 传统部署
- Python 3.10+
- Linux 系统（需蓝牙适配器）
- BlueZ 5.66+（推荐 5.71）
- MQTT Broker（如 EMQX、Mosquitto）

## 快速开始（推荐）

### 方式一：Web 配置页面（最简单）

首次配置无需手动编辑配置文件：

```bash
# 1. 克隆仓库
git clone https://github.com/kairui1108/cuktech-ble-ha.git
cd cuktech-ble-ha/ble_server

# 2. 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 3. 复制配置模板
cp config.yaml.example config.yaml

# 4. 启动服务
./cuktech_ctl.sh start

# 5. 打开浏览器访问 Web 配置页面
#    http://<服务器IP>:8199/config.html

# 6. 在配置页面中：
#    a. 点击「小米云自动获取」→ 用米家 App 扫码 → 自动填充 MAC/Token/BLE Key
#    b. 填写 MQTT 配置（如需接入 Home Assistant）
#    c. 填写巴法云 UID（如需接入小爱同学）
#    d. 点击「保存配置并重启」
```

**配置页面功能：**
- 小米云 QR 码扫码登录，自动获取设备 MAC、Token、BLE Key
- BLE / MQTT / 巴法云 / 服务器参数在线修改
- 巴法云设备显示名称自定义
- 敏感信息（Token、BLE Key、密码）自动脱敏显示
- 保存后自动重启服务

### 方式二：手动配置

```bash
# 1. 获取设备 Token
# 使用 Xiaomi-cloud-tokens-extractor 或 Web 配置页面获取
pip install xiaomi_cloud_tokens_extractor
python -m xiaomi_cloud_tokens_extractor

# 2. 复制配置模板并填入设备信息
cp config.yaml.example config.yaml
vim config.yaml

# 3. 启动服务
./cuktech_ctl.sh start
```

### 方式三：Docker 部署

镜像内置默认 `config.yaml`（来自 `config.yaml.example`），首次启动无需配置文件。

#### 推荐方式（数据持久化）：

```bash
# 创建数据目录
mkdir -p data

docker run -d \
  --name cuktech-ble \
  --network host \
  --privileged \
  --restart unless-stopped \
  -v $(pwd)/data:/data \
  -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro \
  -e CUKTECH_CONFIG_PATH=/data/config.yaml \
  -e CUKTECH_HISTORY_DB_PATH=/data/port_history.db \
  ghcr.io/kairui1108/cuktech-ble-server:latest

# 访问 http://<服务器IP>:8199/config.html 通过 Web 页面配置
# 配置自动保存到 ./data/config.yaml，重启后保留
```

同一份 `config.yaml` 可同时包含 BLE 参数和 Web 页面中设置的所有配置项，详参 [`config.yaml.example`](config.yaml.example)。
```

#### Docker Compose

```bash
# 创建配置文件（不要用 touch，空文件会覆盖容器内默认配置）
# 方式一：克隆仓库后复制模板
git clone https://github.com/kairui1108/cuktech-ble-ha.git
cd kuktech-ble-ha/ble_server
cp config.yaml.example config.yaml

# 方式二：直接写入默认配置（无需 clone）
cat > config.yaml << 'EOF'
ble:
  mac: "XX:XX:XX:XX:XX:XX"
  token: ""
  ble_key: ""
mqtt:
  enabled: true
  host: ""
  port: 1883
server:
  port: 8199
  settings_refresh_interval: 10.0
EOF

# 启动
docker compose up -d
```

> **重要**：`docker-compose.yml` 中 `./config.yaml:/app/config.yaml` 挂载时，如果文件不存在，Docker 会创建一个**目录**而非文件。使用 `cp` 或 `cat` 确保文件存在且包含默认值。

## Web 管理界面

| 页面 | 地址 | 说明 |
|------|------|------|
| 主控台 | `http://<IP>:8199/` | 实时功率、端口控制、协议控制、设备设置 |
| 手机版 | `http://<IP>:8199/phone.html` | 移动端适配 |
| 配置页面 | `http://<IP>:8199/config.html` | 修改配置、小米云扫码获取设备信息 |
| 端口监控 | `http://<IP>:8199/static/port_monitor.html` | 四端口独立监控 |
| 倒计时 | `http://<IP>:8199/static/countdown.html` | 倒计时快速设置 |
| 设备信息 | `http://<IP>:8199/static/device_info.html` | 设备详情与 BLE 控制 |

### 配置页面使用指南

1. **首次配置**：打开 `/config.html`，点击「小米云自动获取」，用米家 App 扫码，自动获取设备信息
2. **修改配置**：直接在页面修改参数，点击「保存配置并重启」
3. **MQTT 配置**：填入 Broker 地址、端口、用户名密码，启用后接入 Home Assistant
4. **巴法云配置**：填入私钥，可自定义各端口设备显示名称
5. **敏感信息**：Token、BLE Key、密码等自动脱敏显示，修改时输入新值即可覆盖

### BLE 连接质量

悬浮 BLE 徽章可查看连接质量评分（0-100），包含：
- 解密成功率、通知响应性、连接稳定性、5 分钟内重连次数
- 连接时长、最后推送时间、下次重连延迟
- MQTT 和巴法云连接状态

## HTTP API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/events` | GET | SSE 事件流（端口数据、状态、设置、质量实时推送） |
| `/api/status` | GET | 获取充电器完整状态 |
| `/api/enable` | POST | 启用/禁用 BLE 连接 |
| `/api/set` | POST | 设置 PIID 值 |
| `/api/port` | POST | 控制端口开关 |
| `/api/protocol` | POST | 控制协议开关 |
| `/api/config` | GET/POST | 读取/保存配置 |
| `/api/xiaomi/login` | POST | 小米云 QR 码登录 |
| `/api/xiaomi/qr/complete` | POST | 完成 QR 码登录 |
| `/api/xiaomi/beaconkey` | POST | 获取 BLE Key |
| `/api/log-level` | GET/POST | 日志级别管理 |
| `/api/bemfa` | GET | 巴法云状态查询 |
| `/api/chart` | GET | 图表数据 |
| `/api/sessions` | GET | 充电记录 |
| `/api/sessions/{id}/export` | GET | 导出**单个会话**的采样点 CSV（会话详情浮层的“导出 CSV”） |
| `/api/export/{port}` | GET | 导出某端口 + 时间窗的原始采样 CSV（含会话之间的空载片段；供脚本/第三方使用，Web 界面已改为上面的会话级导出） |
| `/api/energy/stats` | GET | 能量统计（按周期汇总，含 `by_port` 分端口明细） |
| `/api/energy/protocols` | GET | 按充电协议聚合的电量与次数（Web"用电统计 → 快充协议"视图；服务端 GROUP BY，避免前端翻页拼不全） |
| `/api/charge-limits` | GET/POST | 充电量限额（充到指定 Wh 自动关断端口） |

### 充电量限额

阈值的单位是**充电器输出能量**（Wh，由 V×I 梯形积分得出），不是被充设备的
**实际充入电量**——线损与设备内转换损耗使后者偏小（典型 5~15%）。

```bash
# 设置 C1 充到 30Wh 自动关断，长期有效
curl -X POST http://localhost:8199/api/charge-limits \
  -H 'Content-Type: application/json' \
  -d '{"port":"c1","wh":30,"mode":"always"}'

# 批量设置；wh=0 表示关闭该端口限额
curl -X POST http://localhost:8199/api/charge-limits \
  -H 'Content-Type: application/json' \
  -d '{"limits":{"c1":{"wh":30,"mode":"always"},"a":{"wh":0}}}'

# 查询（含各端口本会话已充 Wh / 是否充电中 / 是否已触发）
curl http://localhost:8199/api/charge-limits
```

`mode` 语义：

| mode | 达到阈值 | 未达阈值会话结束（拔插/手动关端口/充满） | BLE 抖动重连 / 服务重启 |
|---|---|---|---|
| `once`（默认） | 关断并清零 | 清零 | 保留 |
| `always` | 关断，保留待下次充电 | 保留 | 保留 |

配置持久化在 `history.db` 的 meta 表，即时生效、无需重启。超冲量取决于采样周期
（约 1 秒推送 + 命令循环），100W 负载下约 0.05 Wh。

**参数校验**：`wh` 必须是有限数且落在 `0 ~ 1000`（`MAX_LIMIT_WH`），`mode` 必须是
`once`/`always`；**任一项非法则整个请求被拒绝（HTTP 400）**，不会静默归一成"禁用"
——否则用户以为设了限额、实际被禁用，属于危险的静默降级。`wh=0` 表示关闭该端口限额。
（HA 集成的 number 实体上限与此一致：`0 ~ 1000`。）

## 服务管理

```bash
./cuktech_ctl.sh start         # 启动
./cuktech_ctl.sh stop          # 停止
./cuktech_ctl.sh restart       # 重启
./cuktech_ctl.sh status        # 查看状态
./cuktech_ctl.sh log [n]       # 查看日志
./cuktech_ctl.sh clear-log     # 清空日志
./cuktech_ctl.sh clear-history # 清空历史数据库
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -v  # 240+ tests
```

## 致谢

- [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) - BLE 协议参考
- [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) - 协议检测参考
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) - 小米设备 Token 提取
- [bleak](https://github.com/hbldh/bleak) - BLE 通信库
- [paho-mqtt](https://eclipse.dev/paho/) - MQTT 客户端

## 许可证

MIT License
