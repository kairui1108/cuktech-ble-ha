# Release Notes

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
