# Release Notes

## v1.0.2

### BLE Server

- **模块拆分**: ble.py 拆分为 protocol.py（常量）/ controller.py（核心逻辑）/ cli.py（用户界面）
- **BLEManager 单元测试**: 新增 28 个测试覆盖 init/reconnect/publish/process/multiframe/inline/send/disconnect
- **协议测试**: 新增 _env_hex_bytes、odd-length hex、MAC 格式测试
- **BleakClient/HKDF 导入修复**: try/except 导入避免运行时 NameError
- **CORS 安全**: 白名单 localhost Origin，移除未使用的 PUT/DELETE 方法
- **MQTT LWT**: 添加 Last Will and Testament，崩溃时自动通知 HA
- **Falsity zero 修复**: energy_wh 0.0 值不再被错误过滤
- **pad_len 防护**: 防止负数 pad_len 导致协议失步
- **multiframe 完整消费**: 大帧数时消费所有帧而非只消费 10 帧
- **队列竞态修复**: drain queue 使用 while True + try/except 替代 empty() 检查
- **弃用 API 修复**: 全部 asyncio.get_event_loop() 替换为 get_running_loop()
- **Windows 控制台修复**: sys.stdout 副作用移到 CLI 入口函数
- **query 参数校验**: float/int 转换加 try/except 返回 400

### HA Integration

- **Entity 类单元测试**: 新增 30 个测试实例化真实 Entity 类
- **ConfigFlow 完整测试**: async_step_user 表单/创建/去重/错误/中断
- **Availability 修复**: HTTP 失败时尊重 MQTT 连接状态
- **MQTT connected:false 修复**: 不再误标记设备为 available
- **重复实体移除**: PIID 19/20 从 SENSOR_PIIDS 移除，避免 sensor+switch 重复
- **MQTT 异常捕获**: async_set_value/port_control 加 try/except
- **Translation key**: config_flow 错误使用 HA 翻译机制
- **Entity category**: 诊断/配置实体添加 EntityCategory
- **HA 基类 Mock**: conftest.py 使用真实 HA 基类支持 @property

### 测试

- **总计 161 个测试**: BLE Server 91 + HA Integration 70，全部通过
- **覆盖模块**: protocol/controller/ble_manager/ha_server/history/config/state/config_flow/coordinator/entities

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
