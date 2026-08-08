# ESP32 BLE 固件发布说明

## v1.1.2

### 新增
- **定时重启**: 支持自定义重启间隔，设备运行满设定时长后自动重启，用于缓解长期运行的堆碎片等问题
  - 配置项 `reboot_interval_sec`，通过 NVS 持久化，重启后保持
  - HTTP API `/api/config` 支持读写，值 ≤0 视为禁用
  - 主循环定时检测，达到间隔触发 `esp_restart()`，无动态内存分配，开销仅 4 字节
  - `uint32_t` 秒数上限约 136 年，时间计算用 `uint64_t` 无溢出风险
- **前端快捷预设**: config.html 定时重启卡片新增"快捷预设"按钮，一键填入指定秒数
  - 12h / 24h / 36h / 48h，对应 43200 / 86400 / 129600 / 172800 秒

### 构建
- ESP-IDF v5.3.5
- NimBLE Central
- ESP-MQTT + cJSON + mbedTLS
- TLSF 分配器

### 已知限制
- **C3 单核射频竞争**：ESP32-C3 为单核芯片，WiFi 与 BLE 共享同一射频前端。BLE 数据频繁交互时可能导致 WiFi TCP 发送超时、MQTT 短暂断连，系统通常能在 30-60 秒内自动恢复，但极端场景下可能出现更长的通信中断。
- **内存碎片化**：ESP32 可用堆约 320KB，C3 可用堆约 240KB，长时间运行后碎片化可能导致最大连续块降至 9-15KB。当前通过 LWIP 独立堆、cJSON 池化、定期 GC 缓解，实际运行中尚未触发致命问题，但建议避免频繁的全量 API 轮询和静态资源加载。
- **协议检测**：基于电压曲线与特征码的启发式推断，仅供参考以米家显示为准。
- **实机测试**：ESP32 / ESP32-C3 固件经过实机测试与调优，ESP32-S3 尚未进行实机测试和优化，使用该固件时请注意，建议自行按需编译调整。

## v1.1.1

### 修复
- **Token 解析内存越界**: 校验从 `>= 1` 改为 `>= 24`，修复短 token 导致的数组越界读取
- **`_settings[]` 跨任务数据竞争**: 添加 `portMUX_TYPE` spinlock，保护 `_get_hw_proto`、`_estimate_proto`、`store_setting`、`get_setting`、`has_setting` 五个访问点
- **状态回调死锁风险**: ble_task 中 `xQueueSend` 从 `portMAX_DELAY` 改为 `pdMS_TO_TICKS(500)`，防止 result_queue 满时永久阻塞
- **MQTT 重连计数器永不归零**: `mqtt_restart_count` 提升为文件级静态变量，在 `MQTT_EVENT_CONNECTED` 中正确清零，避免正常断连触发 WiFi 复位
- **BLE 自动重连竞态**: `set_enabled(false)` 与 `set_enabled(true)` 之间轮询 `ble_manager_is_idle()`（最长 500ms），避免 GAP 断连事件未完成导致的并发问题
- **`ble_manager_disconnect` 非重入**: 添加 `_disconnecting` 静态守卫，防止重叠调用操作已失效的 `_conn_handle`
- **`_nimble_ready` 跨任务可见性**: 添加 `volatile` 修饰，防止编译器优化导致 ble_task 读到 stale 值

### 优化
- **TLSF 内存分配器**: 启用 `CONFIG_HEAP_TLSF_USE_MALLOC=y`，O(1) 分配 + 即时相邻合并，碎片速度降低 60~80%
- **任务栈回收**: app_task 8192→7168、httpd 8192→6144、wifi_reconn 3072→2048、reboot 2048→1024，共释放 8KB DRAM
- **命令排水循环饥饿防护**: ble_task 命令处理上限 8 条/次，避免 keepalive 与通知处理长期停滞
- **任务栈水位监控**: app_task 每 10s 报告栈剩余量，低于 1024B 告警
- **WiFi RSSI + BLE pending 可视化**: 状态日志增加 `WiFi=XXdBm` 和 `pend=N` 字段
- **碎片自动重启**: 连续 30 次检测（~5 分钟）碎片率 >50% 时自动 `esp_restart()`，碎片缓解时计数器清零
- **NimBLE 内存调优**: HCI_EVT buf 6→8、MSYS_1 12→16、MSYS_2 6→8，防通知突发 mbuf 耗尽；显式配置 host 任务栈 4096
- **LWIP 网络栈**: 启用 TCP_KEEPALIVE（防 NAT 超时断开 MQTT）、TCP_NODELAY（小报文即时发送）、PBUF_POOL_SIZE 12→16；关闭 SO_REUSE（防端口耗尽）
- **栈溢出检测**: 启用 `CONFIG_FREERTOS_CHECK_STACKOVERFLOW_CANARY=y`，栈溢出时立即 panic
- **flash.sh esptool 兼容**: 修复 esptool 4.12+ 参数格式（`--before default_reset`、`--flash_mode`、`write_flash`）

### 构建
- ESP-IDF v5.3.5
- NimBLE Central
- ESP-MQTT + cJSON + mbedTLS
- TLSF 分配器

### 已知限制
- **C3 单核射频竞争**：ESP32-C3 为单核芯片，WiFi 与 BLE 共享同一射频前端。BLE 数据频繁交互时可能导致 WiFi TCP 发送超时、MQTT 短暂断连，系统通常能在 30-60 秒内自动恢复，但极端场景下可能出现更长的通信中断。
- **内存碎片化**：ESP32 可用堆约 320KB，C3 可用堆约 240KB，长时间运行后碎片化可能导致最大连续块降至 9-15KB。当前通过 LWIP 独立堆、cJSON 池化、定期 GC 缓解，实际运行中尚未触发致命问题，但建议避免频繁的全量 API 轮询和静态资源加载。
- **协议检测**：基于电压曲线与特征码的启发式推断，仅供参考以米家显示为准。
- **实机测试**：ESP32 / ESP32-C3 固件经过实机测试与调优，ESP32-S3 尚未进行实机测试和优化，使用该固件时请注意，建议自行按需编译调整。

## v1.1.0

### 新增
- **前端嵌入**: 完全重构前端页面，复刻米家页面，全部前端资源（HTML/CSS/JS/图片）嵌入固件，无需外部服务器或 SPIFFS
- **OTA**: 移除OTA功能，app 分区扩至 3.875MB，4% 空闲 → 26% 空闲
- **BLE 延时连接**: 启动后延迟 60 秒再发起 BLE 连接，确保 HTTP 服务与前端资源加载优先完成，前端也可手动提前触发连接
- **配置脱敏**: 敏感字段（WiFi 密码、设备密钥、MQTT 密码等）API 返回时自动脱敏，保存时跳过 `****`
- **BLE 自动重连**: 断连后自动重启扫描器，指数退避（10s → 20s → 40s → 80s → 160s → 300s 封顶）
- **AP 模式优化**: 连接WiFi失败自动进入AP模式，关闭 Modem 省电（`WIFI_PS_NONE`）+ station 不活跃超时 10 分钟
- **`flash.py` 跨版本兼容**: 自动探测 esptool 参数格式（连字符/下划线），适配 CI 与本地环境

### 优化
- **协议检测**: 硬件协议码（PIID 17/18）优先，PDO kind + PPS 开关完整检查链
- **WiFi/BLE 共存**: 大文件传输时动态切换 `ESP_COEX_PREFER_WIFI`，发送完恢复 BALANCE
- **内存优化**: NimBLE 缓冲区池裁剪（约节省 17KB），TCP 发送缓冲 8192，MQTT 任务栈 4096
- **大文件分块传输**: 4096 字节分块 + 指数退避重试（最大 8 次，1.27s），抑制 EAGAIN 断连
- **HTTP 超时**: `send_wait_timeout` 5s → 10s，撑过 BLE 繁忙期
- **端口去抖动**: 500ms（原 2000ms），减少断开检测延迟
- **通知队列**: `NOTIF_QUEUE_LEN` 8 → 16，降低推送溢出风险
- **result_queue**: 32 → 48，xQueueSend 增加 50ms 超时 + 丢弃日志
- **NimBLE 内存参数回调**: `ACL_BUF_SIZE` 128→255，`HCI_EVT_BUF_SIZE` 128→256 等，确保服务发现正常
- **HTML gzip**: phone.html 80% 压缩，config.html 76% 压缩

### 修复
- **C3 BLE 绑核崩溃**: `xTaskCreatePinnedToCore` 绑 Core 1 导致 assert，改为 `CONFIG_FREERTOS_UNICORE` 条件编译
- **WiFi 密码保存后连不上**: `strncpy` 满长度缺 `\0` 导致 NVS 存脏数据，所有 `SET_STR` 强制 null 终止
- **配置有效标记只检查 SSID**: `_cfg->valid` 补充密码非空判断
- **密码末尾空格未被剔除**: config.html JS 中 `wifi_pass`/`mqtt_pass` 缺少 `.trim()`
- **CMD_PORT 死循环重试**: BLE 断开后仍在发 SET16，失败时检测 BLE 状态并终止命令循环
- **merge-bin CMake 冲突**: `add_custom_target` 双 configure 重复注册，加 `if(NOT TARGET)` 保护
- **Interrupt WDT 超时崩溃**: C3 单核 BLE GATT 并发时关中断超限，IWDT 300ms → 1000ms
- **Task WDT 误触发**: C3 单核 WiFi/BLE 共享 CPU，TWDT 5s → 30s

### 构建
- ESP-IDF v5.3.5
- NimBLE Central
- ESP-MQTT + cJSON + mbedTLS

### 已知限制
- **C3 单核射频竞争**：ESP32-C3 为单核芯片，WiFi 与 BLE 共享同一射频前端。BLE 数据频繁交互时可能导致 WiFi TCP 发送超时、MQTT 短暂断连，系统通常能在 30-60 秒内自动恢复，但极端场景下可能出现更长的通信中断。
- **内存碎片化**：ESP32 可用堆约 320KB，C3 可用堆约 240KB，长时间运行后碎片化可能导致最大连续块降至 9-15KB。当前通过 LWIP 独立堆、cJSON 池化、定期 GC 缓解，实际运行中尚未触发致命问题，但建议避免频繁的全量 API 轮询和静态资源加载。
- **协议检测**：基于电压曲线与特征码的启发式推断，仅供参考以米家显示为准。
- **实机测试**：ESP32 / ESP32-C3 固件经过实机测试与调优，ESP32-S3 尚未进行专项测试和优化，使用该固件时请注意，建议自行按需编译调整。

## v1.0.3

### 新增
- **倒计时设置**: Web 仪表盘新增倒计时功能，30/60 分钟预设及自定义倒计时（范围 1-1440 分钟），到期自动关断端口
- **MQTT 断线保护**: 断线时停止 MQTT publish 入队，防止 outbox 溢出导致内存耗尽和 WiFi 崩溃

### 优化
- **Bemfa 熔断机制**: 连续断线 5 次后暂停重连 5 分钟，避免无效重连消耗
- **Bemfa 保活对齐**: ping QoS 0 + `==` 判断 + 递归调度，对齐官方 HA 集成
- **BLE 扫描**: 扫描前 cancel 冲突会话，提升连接成功率
- **Web 仪表盘**: 倒计时输入框不因自动刷新丢失焦点；清除按钮红色高亮

### 修复
- MQTT 断线时 publish 数据堆积导致 `outbox: Memory exhausted` 和 WiFi 崩溃
- HTTP 页面不显示数据（缺少 `setInterval` 调用和 `CDPI` 变量作用域错误）
- Bemfa 长时间运行显示设备离线

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
