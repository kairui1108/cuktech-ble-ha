# CUKTECH Charger BLE Bridge - ESP32 Firmware

[English](esp32-readme-en.md)

将酷态科充电器通过 ESP32 接入 Home Assistant 和巴法云。ESP32 本身即提供完整的 Web 控制界面，无需依赖任何外部服务即可完成配置和监控。

```
充电器 ←BLE→ ESP32 ──MQTT──→ Home Assistant
         │            ├──巴法云──→ 小爱同学 / 小度
         │            ├── HTTP 内置 Web 界面 ──→ 浏览器直接控制
         │            │     （仪表盘 / 配置, 零依赖）
         │            └── NVS（配置持久化）
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

## 巴法云接入（小爱同学 / 小度）

固件内置巴法云支持，无需 Home Assistant 即可通过语音控制充电器。

### 使用步骤

1. 注册 [巴法云](https://www.bemfa.com)，获取 **UID**（32 位私钥）
2. 打开 ESP32 Web 配置页，找到「巴法云（接入小爱、小度）」区域
3. 填入 UID，启用巴法云，保存重启
4. 设备自动注册 5 个开关到巴法云：
   - C口1开关、C口2开关、C口3开关、USB-A开关、蓝牙开关
5. 打开小爱 APP → 自动发现设备
6. 语音控制：「小爱同学，打开C口1开关」

### 注意事项

- 设备名已自动设置为中文，小爱可直接识别
- 端口开关通过 BLE 实时同步，状态变化自动更新到巴法云

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

## 固件升级（OTA）

固件采用双分区布局（`ota_0` / `ota_1`，每槽 0x1F0000 ≈ 1.94MB），支持通过 Web 上传升级，新固件异常时自动回滚。

```bash
# 查看当前运行分区、升级状态与进度
curl http://<ESP32-IP>/api/ota

# 上传固件（文件为 idf.py build 生成的 build/cuktech_ble.bin）
curl --data-binary @build/cuktech_ble.bin \
     -H "Content-Type: application/octet-stream" \
     http://<ESP32-IP>/api/ota
```

- 升级期间会自动关闭 BLE 并优先保障 WiFi，上传完成后设备自动重启进入新固件。
- 新固件需连续稳定运行 **60 秒**才被标记为有效；若在此期间崩溃或重启，bootloader 自动回滚到旧固件。
- 若 WiFi 无法连接而进入 AP 配网模式（说明固件本身已正常启动），会立即标记为有效，避免用户保存配置后重启时误回滚。
- 分区表已改为双 OTA 布局。**仍在使用旧单分区固件的设备，需要先用 USB 完整烧录一次**（`idf.py -p /dev/ttyUSB0 flash`），之后即可使用 OTA。NVS（WiFi / 设备凭据）保持不变。
- 上传接口与其他 API 一样不带鉴权，请仅在可信局域网内使用。

## Web 面板（零依赖）

固件内置完整 Web 控制界面，连接成功后直接在浏览器访问 ESP32 的 IP 地址即可使用——无需 Home Assistant、无需 MQTT 服务器、无需任何外部依赖。

- 实时查看各端口电压 / 电流 / 功率
- 端口开关控制
- 协议开关（PD / PPS / UFCS / SCP）
- 场景模式切换
- BLE 连接开关
- **界面语言**：支持简体中文与 English（其他浏览器语言默认显示英文）；按浏览器语言自动匹配，也可在配置页「显示 → 界面语言」手动设置并记住选择
- **加载优化**：内嵌资源带 ETag 并支持 304，脚本每次打开仅回传头部、图片缓存 7 天，重复访问几乎瞬时；70 KB 的图表库以 `async` 加载，不阻塞页面脚本与首次数据刷新；**并发与内存**：静态文件最多两路并发、每块 1 MSS（1460 B）、TCP 发送窗口 3 MSS，六个连接同时拉取整个页面时堆内存不再被耗尽（实测 38 次请求全部成功）；图片按 2× 显示尺寸重新编码为 WebP，图片总量从 1.65 MB 降到 290 KB（静态资源整体 1.73 MB → 370 KB），BLE 已连接时整页冷加载约 8–11 秒、重复访问约 1.3 秒；**BLE/WiFi 共存**：鉴权完成后把 BLE 连接间隔申请到 200 ms、latency 2，扫描占空比降到 50%，避免单核 C3 的双射频共存把 TCP 往返拖慢到约 100 ms

## 文件结构

```
esp32_ble/
├── CMakeLists.txt         # ESP-IDF 构建入口
├── main/
│   ├── main.c             # WiFi / HTTP / 任务调度 / 命令队列
│   ├── device_state.c/.h  # 设备状态单一数据源（端口 / 设置 / BLE 状态）
│   ├── uplink.c/.h        # MQTT 遥测（QoS 0，限速合并，巴法云分发）
│   ├── ble_manager.c/.h   # BLE 状态机 + 认证 + 串行异步命令
│   ├── miot_auth.c/.h     # 小米 MiOT 认证加密
│   ├── miot_protocol.c/.h # 协议常量 + TLV 编解码
│   ├── config.h           # 编译时默认值
│   ├── config_store.c/.h  # NVS 配置持久化
│   ├── bemfa.c/.h         # 巴法云 MQTT + HTTP API
│   ├── http_server.c/.h   # Web 配置页 + REST API（keep-alive / 异步静态文件）
│   ├── ota.c/.h           # 双分区 HTTP OTA（异步上传 + 回滚保护）
│   └── queue_msg.h        # 跨任务消息定义
├── data/                  # 内嵌 Web 资源（WebP q75 + gzip）
├── tools/                 # 资源压缩与 C 头文件生成
├── partitions.csv         # 分区表（双 OTA，每槽 0x1F0000）
├── sdkconfig.defaults     # ESP-IDF 默认配置
└── sdkconfig              # 本地 sdkconfig（gitignore）
```

## 构建说明

### 依赖

- [ESP-IDF v5.3](https://docs.espressif.com/projects/esp-idf/en/v5.3/esp32/get-started/index.html)

### 构建

```bash
cd esp32_ble
python tools/generate_embedded.py   # 生成 main/embedded_files.h（已 gitignore，首次/改前端后必须执行）
idf.py set-target esp32    # 或 esp32s3 / esp32c3
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

`flash.sh` 会把 `build/merged-binary.bin` 整包烧到 0x0；合并用 IDF 内置的 `idf.py merge-bin`，偏移直接来自 `build/flash_args`（也就是 `partitions.csv`），脚本里不再抄一份。

### 多平台

支持 `set-target esp32`、`esp32s3` 和 `esp32c3`。配置分层：

- `sdkconfig.defaults`：三个目标共用的设置（NimBLE 池、lwIP 发送窗口、WiFi 收发缓冲上限、MQTT、稳定性开关等），只放所有目标都存在的符号
- `sdkconfig.defaults.esp32`：经典 ESP32 的双核覆盖（`BTDM_CTRL_MODE_BLE_ONLY`、NimBLE 绑核 1、CPU1 空闲任务看门狗）
- `sdkconfig.defaults.esp32s3`：双核 + 8 MB QIO flash + 240 MHz + BLE 控制器活动槽 2（flash 参数需与实际模组一致，4 MB/DIO 模组要相应修改）
- `sdkconfig.defaults.esp32c3`：仅单核 C3 的覆盖项（`FREERTOS_UNICORE`、BLE 控制器活动槽 2、中断看门狗 1 s）

目标相关符号（`BTDM_CTRL_MODE_BLE_ONLY`、`ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1`、`BT_NIMBLE_PINNED_TO_CORE_1`）必须放在目标文件里：它们分别在 S3/C3 或单核 C3 上不存在，放进公共文件会让每次构建都打印 unknown kconfig symbol。NimBLE 的缓冲符号请使用 IDF 5.3 的 `BT_NIMBLE_TRANSPORT_*` 名称；旧的 `BT_NIMBLE_ACL_BUF_*` / `BT_NIMBLE_HCI_EVT_*` 只靠 `components/bt/sdkconfig.rename` 迁移，已被标记为废弃。

`.github/workflows/firmware.yml` 会在每次修改 `esp32_ble/` 时用 ESP-IDF v5.3.5 编译 `esp32` / `esp32c3` / `esp32s3` 三个目标，并执行 `idf.py merge-bin` 校验合并固件的布局。

**注意**：`CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM` / `DYNAMIC_TX_BUFFER_NUM` 必须保留在公共配置里。ESP32/S3 的 IDF 默认值是 32/32，一次图片较多的整页加载就会把堆从 73 KB 压到 2.5 KB，HTTP 服务会卡死约 30 秒；8/6 的实测值可避免该问题。

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
