# Release Notes

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
