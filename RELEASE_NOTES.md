# Release Notes

## v1.0.10

### BLE Server — 充电会话记录开关与性能优化

#### 新增功能
- **充电会话记录开关**: 通过 `/api/session-recording`、`config.html` 控制是否记录充电会话历史。关闭后不再写库，但实时显示、充电完成事件（MQTT 通知）与功率图表照常，**即时生效，无需重启**（DB meta 单源，不依赖 config.yaml 重启流程）
- **恢复记录机制**: 重新打开开关时，正在充电的端口自动转为真实记录（补建 DB 会话 + 从打开时刻重新累计能量），曲线与历史立即恢复，无需等到下次充电

#### 改进
- **历史批量写库**: `port_history` 批量提交（50 行或 1s），大幅降低高频采样下的 COMMIT 写放大与 WAL 增长；读路径自动 flush 保证写后读一致
- **查询去阻塞**: `handle_chart`/`handle_statistics`/`handle_export` 的同步 SQLite 查询移入线程池，避免阻塞事件循环（与 `handle_sessions` 等已有做法对齐）
- **会话清理**: 过期闭环会话及其采样点级联删除，`charge_sessions` 不再无限累积；`connect` 启动时回收崩溃遗留的孤儿会话（`end_time IS NULL`）
- **DB meta 单源存储**: 运行时开关状态持久化在 `history.db` 的 `meta` 表，随数据库自然备份/清除，无需新增 config.yaml 字段（避免双源问题）
- **内存占位 sid**: 关闭记录期间使用负值占位会话 ID 保持会话生命周期完整，`/api/sessions`、`/api/energy/stats` 的实时部分不受影响，重新打开后正在充电的会话正常显示
- **事件与记录解耦**: 关闭记录仅影响 DB 写入，MQTT 充电完成事件（HA 通知）始终发布，上层自动化不受影响
- **phone.js 屏显时间**: 补上 `SCREEN_TIMES[5] = "1分钟"`（value 5 是米家插件对 1分钟 的实际编码），与 app.js 及 HA 端映射一致，防止手机页显示 undefined

### HA Integration

#### 修复
- **屏显时间显示未知**: 依据米家插件逆向确认 PIID 6 实际编码为 `1=5分钟, 2=10分钟, 3=30分钟, 4=常亮, 5=1分钟`（value 5 即 1分钟，value 0 非法）；此前映射为带注释的长字符串导致不在 Select options 列表中 → HA 状态校验返回 unknown；改为与 ble-server 一致的 `"1分钟"`，修复显示
- **BLE 断连陈旧数据**: 设备断开时清空 `_port_data`，端口实体展示 unknown 而非断连前的误导性读数
- **MQTT 就绪等待**: 改用官方 `mqtt.async_wait_for_mqtt_client()`（内部限时 50s），不再阻塞最长 ~90s 的自制重试/探测发布

#### 改进
- **清理未使用代码**: 移除 6 个平台文件中未使用的 `logging` / `_LOGGER` 导入，删除 `TOPIC_PROBE`（`__probe__` 主题）、`MQTT_RETRY_*` 常量
- **测试基建**: 声明 dev 依赖 `pytest` / `pytest-asyncio`（`pyproject.toml`），补齐异步测试（13 例此前无法运行，现全部通过）

### 测试

- **总计 253 个测试**: BLE Server，全部通过

## v1.0.9

### BLE Server — SSE 稳定性、实时曲线与性能优化

#### 新增功能
- **桌面端实时功率曲线**: 端口详情弹窗新增 `⚡ 实时` 按钮，SSE `port_update` 驱动 500ms 去抖更新，右对齐填充实现曲线从右侧进入
- **移动端实时功率曲线**: phone.html 组合图表改用 `phoneChartData` 时间戳缓冲区，x 轴显示 `HH:MM:SS` 标签，5 分钟滑动窗口（`PHONE_CHART_MAX=150`）
- **静态文件预加载缓存**: 启动时扫描 `static/` 目录全部读入内存 + 预 gzip 压缩，运行时零磁盘 I/O、零 gzip 开销
- **会话自动超时**: 5 分钟无操作自动清理 `XiaomiCloudClient`，QR 扫码完成后取消定时器，避免 session 泄漏

#### 改进
- **SSE 推送稳定性**: `SSEEmitter` 增加 `_pending_status` 机制，status 事件永不被队列淘汰；`MAX_QUEUE_SIZE` 64 → 128；添加 `threading.Lock` 并发保护
- **BLE 连接健壮性**: 熔断器（20 次失败 → 300s 冷却），重连延迟 ±25% jitter 防冲突
- **静态文件缓存头**: 设置 `Cache-Control`，浏览器 7 天强缓存
- **长稳解密失败恢复与推送隔离**: 针对设备长时间充电后偶发的「连续解密失败 → 会话过期 → 重连」故障，完成以下稳定性优化：
  - **根因修复（设备 it 计数器溢出）**: 设备推送帧仅携带 `it` 计数器的低 16 位，但加密 nonce 使用完整的 4 字节 `it`。原 `decrypt()` 假定 `it` 高 16 位恒为 0，当设备持续推送使 `it` 超过 65535（约连续运行 11~13 小时）后 nonce 失配，导致推送帧解密失败。修复为跟踪设备 `it` 高 16 位，通过低 16 位回绕进位重建完整 `it`，从根本上消除长时运行后的解密失败与会话过期重连
  - **settings 刷新与推送隔离**: GET/SET 响应等待期间不再丢弃端口实时推送帧，而是重放回 `cmd_recv` 队列交由主循环处理，消除端口数据丢失与推送帧错位
  - **解密失败快速恢复**: 连续解密失败阈值从 10 次降至 3 次，数据丢失窗口由约 10 秒缩短至约 3 秒，更快触发会话恢复
  - **降低 settings 刷新频率**: `settings_refresh_interval` 由 20s 调整为 60s，降低主动 GET 吞掉设备推送帧的概率
- **MQTT BLE 连接控制**: 修复 HA「BLE连接」开关通过 MQTT 控制断开/连接不生效的问题——新增订阅并处理消息，调用 `ble.start()` / `ble.request_stop()` 响应，与 Bemfa 命令同线程安全模式

### HA Integration

#### 改进
- **Coordinator 重构**: MQTT coordinator 独立化，`_notify_callbacks` 异常隔离防止回调链断裂
- **实体去重**: `_async_add_entities` 日志去重，避免重复注册警告
- **BLE 超时清理**: `_clear_pending_after_delay` 任务管理，充电 session 完成后自动取消
- **`async_will_remove_from_hass`**: 所有 Entity 添加 `super().async_will_remove_from_hass()` 调用

### 测试

- **总计 230 个测试**: BLE Server 全部通过
- **测试覆盖**: SSEmitter 状态去重、静态文件缓存、实时曲线缓冲区、Xiaomi session 超时流程

## v1.0.8

### BLE Server — 实时推送与配置管理

#### 新增功能
- **SSE 事件流**: Server-Sent Events 实时推送端口数据、状态、设置、协议变更至 Web 前端，替代 2s 轮询
- **Web 配置页面**: `/config.html` 在线编辑 BLE/MQTT/Bemfa/Server 配置，保存后自动重启
- **Xiaomi Cloud 登录**: 扫码登录小米账号，自动获取设备 Token 和 BLE Key
- **连接质量工具提示**: 悬浮查看 BLE/MQTT/Bemfa 三路连接评分、解密率、连接时长、重连频率
- **BLE 连接质量指标**: 后端持续评估解密成功率、推送间隔、重连频率、LNS 状态
- **LTTB 降采样优化**: 列预提取 + 缓存边界值 + 去除冗余除法，2M→600 点 408ms
- **配置持久化**: `config.yaml` 支持在线修改并持久化，日志等级变更同步写入

#### 协议检测
- **硬件协议码（PIID 17/18）**: 新增硬件协议码解析，支持 C1/C2（PIID 17）和 C3/A（PIID 18），与米家 App 一致
- **启动顺序调整**: `_read_initial_settings()` 提前到 init_push 处理之前，确保硬件协议码在端口数据显示前就绪
- **硬件码优先策略**: `estimate_protocol_number()` 优先使用硬件协议码（`hw_protocol`），无硬件码时降级为启发式推断
- **零值保护**: PIID 17/18 刷新返回 0 时保留旧值，避免协议码被无效数据覆盖
- **协议去抖**: `_proto_buf` 连续 3 次相同才确认协议变更，端口空闲时立即清空缓冲
- **置信度系统**: 协议变更时置信度 0.90，5 分钟半衰期衰减至 0.40（下限）
- **PD vs PPS 区分优化**: 高压段（≥12V）PD 档位匹配阈值放宽至 0.3V，降低线损导致的误判

### Web UI

- **SSE 驱动实时更新**: 桌面端增量 DOM 更新（`updatePortDOM`），手机端增量渲染（`applyPortUpdate`）
- **重连自动同步**: SSE `onopen` 事件触发全量拉取，断连后自动恢复显示
- **bfcache 支持**: `pagehide` 关闭 EventSource，`pageshow` 重建，页面返回无需重载
- **配置页**: 新增配置管理页面，支持 BLE/MQTT/Bemfa/Server 参数修改
- **小米云 Token 提取**: 配置页集成二维码扫码登录小米账号，自动获取设备参数
- **响应式**: 适配桌面端和移动端新功能入口
- **质量评分显示**: BLE 徽章悬浮查看连接质量评分

### HA 集成

#### 改进
- **数据通道优化**: MQTT 主通道优先，HTTP 降级为回退，消除双重写入
- **reauth 流程**: `async_step_reauth` 增加 `async_set_unique_id`，正确匹配已有配置条目
- **时间函数选型**: `time.time()` 改为 `hass.loop.time()`，避免系统时钟调整导致可用性误判
- **异常日志**: `_async_health_check` 中 JSON 解析异常从静默吞掉改为 `_LOGGER.warning`
- **健康检查状态同步**: HTTP 健康检查成功时更新 `_last_status_time`，避免 MQTT 断线时错误标记为不可用

### 测试

- **总计 240+ 测试**: BLE Server + HA Integration 全部通过

## v1.0.7

### BLE Server — 充电记录与电量统计

#### 新增功能
- **充电记录（Charge Session）**：自动记录每次充电的起止时间、总电量、峰值功率、协议类型
- **能量积分（Energy Integration）**：基于梯形积分法实时累积端口充电电量
- **LTTB 降采样**：session 详情曲线按需降采样，前端渲染流畅
- **充电记录 API**：`/api/sessions`、`/api/sessions/{id}/points`、`/api/energy/stats`
- **前端充电记录卡片**：支持翻页查看历史充电记录，点击查看详情曲线

#### 修复
- **功率曲线为零**：BLE 推送间歇期定时采样器补写数据，修复电压电流恒定时历史功率显示为 0
- **会话终止误判**：ChargeEndDetector 阈值单位错配（A vs W）导致 session 在正常充电时被提前结束
- **协议检测卡死**：端口空闲时未清空协议去抖缓冲，协议值卡在旧值

### HA Integration

- **充电事件实体**：新增 `event.cuktech_charge_session`，实时推送充电完成事件

## v1.0.6

### BLE Server — 巴法云接入

#### 新增功能
- **巴法云 (Bemfa) 集成**: 接入小爱同学/小度音箱，语音控制充电器端口开关
- **Web 控制面板**: 仪表盘新增巴法云启停开关，运行时切换无需重启
- **MQTT 多路复用**: `mqtt_publish` 自动分发到 HA MQTT 和巴法云
- **Ping/Pong 保活**: 30s 心跳 + 20s 超时 + 3 次丢失自动重连

#### 优化
- **前端页面**： 优化功率图更新逻辑
- **BLE 连接**： 优化 BLE 连接稳定性

### HA Integration

兼容 ESP32 固件和 BLE Server。

## v1.0.5

### BLE Server — 协议开关控制

#### 新增功能
- **PIID21 协议开关修复**: SET 编码支持 2 字节 piid、正确 tl 编码、动态 total_len
- **TLV 编码重构**: 提取 `_build_miot_tlv()` 静态方法，统一 UINT8/UINT32 编码
- **`/api/protocol` 端点**: 支持 toggle/set/bulk/value 四种操作模式
- **本地状态同步**: 协议开关操作后立即更新本地 state，提升响应一致性

#### Bug 修复
- **Session 密钥泄露**: `print()` 改为 `_LOGGER.debug()` (controller.py)
- **私有锁访问**: `state._lock` 公开为 `state.lock` 属性 (state.py + ha_server.py)
- **模块导入兜底**: `state.py` 添加 `try/except ImportError` 路径修复

#### 代码质量
- `_build_miot_tlv`: 消除重复 plaintext 拼接逻辑 (controller.py)
- `assertion_error: NoneType` 守卫增强

### HA Integration — 协议开关实体

#### 新增实体
- **CuktechProtocolSwitch**: 10 个协议开关实体（C1/C2: PD/PPS/UFCS, C3/A: UFCS/SCP）
- **PPS PD 依赖**: C1/C2 PPS 实体在 PD 关闭时自动显示关闭状态

#### 新增协议
- `protocol_switches` 属性: 解码 PIID 21 为 per-port per-protocol 字典 (coordinator)
- `_encode_protocol_extend`: 编码协议开关状态回 PIID 21 值
- `async_set_protocol`: 带锁的读-改-写操作

### 测试

- **新增 37 个测试**: BLE Server +20 (TLV 编码/协议开关/API), HA +17 (协议开关实体/编解码)
- **总计 222 个测试**: BLE Server 135 + HA Integration 87，全部通过
- **aiohttp 安装**: 修复 ha_server 测试环境依赖

## v1.0.4

### BLE Server — 协议对话

#### 协议检测 (Protocol V2)
- **协议检测引擎**: state_protocol_v2.py 米家协议号(1-10)映射引擎，电压+code 启发式估算
- **PIID 21 protocol_ctl_extend**: 支持读写协议扩展命令
- **多字节 SET/GET**: controller.py 支持 poco-endian 多字节值
- **PD 子类型检测**: _estimate_pd_subtype — <12V 判 PPS default，>=12V 判 PD default
- **code 0x70**: 添加 C1/C2 PD 电压检查，避免误判为 QC
- **PDO 集成**: PDO + protocol_switches 联合检测，提升 PPS 识别准确率
- **PD-off 检测**: PIID21 PD 禁用时强制 5V

#### 安全修复
- **HMAC 时序攻击**: != → hmac.compare_digest() (controller.py)
- **文件句柄泄露**: open() → with open() (ble_manager.py)
- **SQLite 线程安全**: _write_lock → _db_lock 覆盖所有读写操作 (history.py)
- **MQTT 密码泄露**: 防止异常日志中打印 MQTT 密码
- **ETag**: hashlib.md5 → hashlib.sha256 (ha_server.py)

#### 代码质量
- 死导入清理: io (controller.py), struct (protocol.py)
- 全局单例: 添加 reset_server() 用于测试清理 (ha_server.py)
- DRY: 提取 _try_decode_inline() 通用辅助方法 (controller.py)
- _drain_pending_pushes 超时: 添加 10s 截止时间 + 100 帧上限 (controller.py)
- get_properties 静默失败: 部分失败时记录 warning (controller.py)
- BLE handle_enable 封装: _stop_event → is_running + request_stop() (ha_server.py)

#### 移动端页面 (phone.html)
- 全新手机端自适应界面，自动检测手机浏览器跳转
- 设备图片 USB 端口叠加层（图标 + 实时功率）
- 场景模式选择器（AI/数码生态/单口/均衡），配模式描述
- 端口控制卡片（独立开关）
- 功率曲线折线图（每端口独立 Chart.js，Y 轴自动缩放）
- 功率占比分布条（含空闲功率）
- 延时关闭滑动控制（0-240 分钟无级调节）
- 连接状态卡片 + Toast 提示
- 深色/浅色主题（跟随系统 + 手动切换）
- CSS/JS 独立为外部文件

#### 桌面端 Web UI
- CSS 独立为 index.css，phone.css
- 添加场景模式描述文案
- 清理未使用图标（76 → 36 个）
- 删除 phone_test.html

#### MQTT 解耦
- **默认不启用 MQTT**，mqtt.enabled: false
- 仅 config.yaml 设置 enabled: true 或 MQTT_ENABLED=1 时连接

#### 跨平台兼容
- 非 Linux 平台跳过 bluetoothctl 操作（_force_disconnect_bluetooth, _find_ble_adapter）
- BLE 连接功能由 bleak 库处理，macOS/Windows 正常使用

#### BLE 修复与优化
- start() 中 elif last_error: 分支顺序修复，POWERED_OFF 的 60 秒延迟生效
- 蓝牙关闭时降低日志频率（warning + 60s，无栈追踪）
- 多处防止闪烁保护（3 秒内忽略 API 返回）
- 连接按钮改为轮询 status 直到确认
- _force_disconnect_bluetooth: disconnect 后 sleep(3) 等待 LL 断开确认
- _connect: 固定 sleep(3) 等待适配器初始化
- _disconnect: 始终执行 GATT cleanup
- Auth 失败后等待从 2s 增至 3s
- 适配器就绪等待从 10s 增至 15s
- NoneType 守卫: 主循环 + inline data + multiframe + controller 添加 if not self.ctrl
- controller.start_notify: 包裹 try/except，单个失败不影响其他通道

### HA Integration

- (协议检测移至 BLE Server 侧，移除 CuktechProtocolSwitch)
- const.py: 移除 TOPIC_PROTOCOL
- sensor.py: PROTOCOL_OPTIONS 对齐米家（5V/QC/AFC/FCP/SCP/PD/PPS/UFCS）
- ConfigFlow 设备名更新为完整产品名

### 文档

- **protocol_ctl_extend**: controller.py 添加 PIID 21 用途注释
- **PIID 映射**: protocol.py 注释更新
- **Lovelace 示例**: ha_config/example_lovelace.yaml 更新
- **docs/**: 新增 MIJIA_PLUGIN_ANALYSIS.md 逆向分析文档

### 测试

- **新增测试**: protocol_detection 协议检测、HA integration 集成测试
- **测试隔离**: conftest.py 全局 mock asyncio.create_subprocess_exec
- **总计 185 个测试**: BLE Server 115 + HA Integration 70，全部通过

## v1.0.3

### BLE Server

- **BLE 连接稳定性**: 
  - `_force_disconnect_bluetooth`: disconnect 后 sleep(3) 等待 LL 断开确认
  - `_connect`: 移除激进 GATT 检查，改为固定 sleep(3) 等待适配器初始化
  - `_disconnect`: 始终执行 GATT cleanup（stop_notify + client.disconnect），确保设备收到断连通知
  - `handle_enable(false)`: 先 await ble_task 完成再 power cycle，避免竞态
  - `_force_disconnect_bluetooth`: 适配器就绪等待从 10s 增至 15s
  - Auth 失败后等待从 2s 增至 3s
- **NoneType 错误修复**: 
  - 主循环 + `_handle_inline_data` + `_handle_multiframe` 添加 `if not self.ctrl` 守卫
  - `controller.connect()`: start_notify 包裹 try/except，单个失败不影响其他通道
- **设备信息**: device_model 前缀 `njcuk.fitting.ad1204_`，通过 BLE 读取并同步到 HA

### HA Integration

- **BLE 连接控制实体**:
  - `CuktechConnectionSwitch`: 开关控制 BLE 连接/断开
  - `CuktechConnectionBinarySensor`: 显示当前 BLE 连接状态
  - `async_enable_ble`: asyncio.Lock + 30s 超时 + 乐观更新 + 失败回退
  - `ble_enabled` 与 `ble_connected` 自动同步
- **switch available**: 添加 `ble_pending` 检查，操作中禁用开关
- **ConfigFlow**: 默认设备名更新为完整产品名
- **controller.py**: start_notify 包裹 try/except，单个失败不影响其他通道

### 测试

- **总计 171 个测试**: BLE Server 101 + HA Integration 70，全部通过

## v1.0.2

### BLE Server — 连接稳定性修复

- **BLE 认证重连修复**: 设备 session 状态不同步时，Phase A 恢复 + Phase B RCV_RDY 重试机制
- **bluetoothctl disconnect MAC**: 断连时发送明确 BLE 断连通知，让设备重置 session
- **Auth 失败不再重复 power cycle**: 避免设备收到多次断连通知导致状态混乱
- **power cycle 后适配器就绪检查**: 轮询 `hciconfig hci0` 等待 UP 状态
- **desync 检测**: Phase A 恢复后二次 drain 清理残留 key exchange 数据
- **auth 失败 CCCD 清理**: 断连前调用 stop_all_notifications() 清除 GATT 订阅
- **stop() 竞态修复**: _stop_event 触发后跳过 GATT 操作，避免和 power cycle 并发
- **AuthConnectionError 自定义异常**: 区分 auth 失败和普通连接错误
- **auth 失败退避策略**: 连续失败 5 次后停止重试，通知用户手动重启充电器
- **MQTT LWT**: 添加 Last Will and Testament，崩溃时自动通知 HA
- **BLE keepalive**: 每 10 秒读 GATT 特征值检测空闲断连
- **测试隔离**: conftest.py 全局 mock asyncio.create_subprocess_exec，防止测试影响真实 BLE

### BLE Server — 代码质量

- **模块拆分**: ble.py → protocol.py / controller.py / cli.py
- **PIID 统一定义**: protocol.py 新增 READABLE_SETTINGS_PIIDS
- **_recv_get_response**: 推送通知不再延长 deadline，防止无限超时
- **chart 优化**: 单次遍历构建 power/voltage/current 数组，消除重复 strftime
- **cuktech_ctl.sh**: 新增 clear-log 和 clear-history 命令，动态生成 systemd service

### HA Integration

- **_notify_callbacks**: 遍历 list(self._callbacks) 副本，防止遍历中被修改
- **async_will_remove_from_hass**: 所有 Entity 添加 super() 调用
- **test_health_failures**: 重命名测试名以匹配实际断言
- **PIID 6 重复值**: 添加注释说明设备固件行为

### Web UI

- **MQTT 状态显示**: BLE 和 MQTT 连接状态并排显示（绿/红色圆点）
- **按钮防重复提交**: bleToggle/bleRestart 添加 disabled 保护
- **按钮文字直接决定操作**: 不依赖 bleConnected 状态，避免状态不同步

### 文档

- **README**: 目录结构更新（protocol.py/controller.py/cli.py）
- **docs**: 实体名称格式修正、countdown 范围更新、reconnect 配置更新
- **bump-version.sh**: 同时更新 ha_integration/pyproject.toml 版本

### 测试

- **总计 171 个测试**: BLE Server 101 + HA Integration 70，全部通过
- **新增测试**: BLEManager 重连循环/Auth 失败/Multiframe 边界/并发命令/解密失败计数/MQTT 重连

## v1.0.1

### BLE Server

- **日志系统优化**: 使用 logging 模块替代 print()，支持日志级别控制
- **密钥安全**: 移除所有加密密钥（随机密钥、HMAC、会话密钥）的日志输出
- **HTTP 缓存**: /api/status 端点添加响应缓存，状态变化时自动失效
- **状态缓存**: ChargerState 添加 to_dict() 缓存，减少锁竞争
- **multiframe 修复**: 修复多帧数据处理逻辑，添加帧数上限检查（256）
- **MQTT 命令修复**: port 命令添加缺失的 cmd_future 参数
- **端口验证**: MQTT 端口命令添加 PORT_BITS 验证
- **settings 刷新优化**: 刷新间隔从 500ms 降至 100ms，14 个属性从 7s 降至 1.4s
- **异常日志**: _fetch_settings 失败时记录 DEBUG 级别日志
- **CORS 优化**: 移除 Allow-Credentials 头，改为回显请求 Origin
- **systemd 支持**: 新增服务单元文件、日志轮转配置、一键安装脚本
- **日志轮转**: 保留最近 3 天日志，自动压缩

### HA Integration

- **HACS 支持**: 添加 hacs.json，支持通过 HACS 一键安装
- **My Home Assistant 徽章**: README 添加一键添加集成按钮
- **Coordinator 简化**: data 属性直接返回 settings dict，移除多余包装
- **双重可用性检测**: MQTT status + HTTP 健康检查联合判断
- **返回类型修复**: CuktechCountdown.native_value 返回类型修正为 float | None
- **data 安全**: 返回 settings 拷贝而非引用

### 文档

- **中英文 README**: server 和 integration 各提供中英文版本
- **语言切换**: README 顶部添加语言切换链接
- **致谢列表**: 添加项目依赖和参考实现致谢
- **systemd 文档**: 添加服务安装和使用说明

## v1.0.0

- 初始发布
