# Release Notes

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
