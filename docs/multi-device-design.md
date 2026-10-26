# 多设备架构设计

---

## 0. 一页结论

1. **单设备假设在外壳，不在内壳**。`BLEManager` / `CuktechBLEController` / `ChargerState`
   本来就是按实例持有资源（构造注入、无模块级单例），复制 N 份几乎免费。真正的单设备假设在
   外壳：`Server` 的挂载方式、MQTT topic、history schema、SSE、Web 前端，以及
   **适配器级的破坏性恢复动作**（`scan off` / power cycle / `os.execv`）。
2. **方案核心四件套**：`DeviceRuntime` 注册表 + **只读 property + per-request ContextVar
   路由**（绝不做可变赋值）；`AdapterCoordinator`（扫描合并 / 并发闸门 / 分级恢复）；
   HA 侧把全局 topic 常量降级为 **per-entry 缺省值**；**验证与回滚配套**（golden 快照 +
   虚拟充电器 + 归档脚本）。
3. **ESP32 零改动**：它的 topic 前缀本就按设备配（NVS），"前缀 = 设备命名空间"天然适配。
4. **分六阶段** P0→P5，每阶段独立可发布。**P0–P4 已落地；P5（两台真机 72h canary）阻塞于
   第二台硬件，未通过前多真机能力标 Beta。**

---

## 1. 关键事实（设计的地基）

| 判断 | 依据 |
|---|---|
| 事件量级无压力 | N 台 = N×(重连循环 + 1s 采样 timer)；N×4Hz 对单进程单循环是噪音级别 |
| 适配器是**全局独占资源** | 扫描是适配器级操作；`bluetoothctl power off` 杀掉所有连接 → 必须收口到唯一协调器 |
| 只有一台真机 | "多台真机并发认证"无法验证 → 必须用虚拟充电器（controller 层、真加解密）+ 分级验证策略 |
| 共享 DB 有降级陷阱 | 旧代码按 `port` 查询、无 `device_id` 条件 → 回滚不止是改配置（§6.1） |

---

## 2. 三原则

1. **单设备逐字节不变**（P1 原则）：不写 `devices:` 就等于改造前，用 golden 快照锁死；
   多设备才出现的新字段/新端点，绝不在单设备响应里加。
2. **降险优先于"更优"**：单设备时恢复阶梯直接退化为现状（该 power cycle 就 power cycle、
   该 `execv` 就 `execv`），不为理论优雅牺牲已验证的行为。
3. **可测**：每个机制都要有能在"一台真机"条件下运行的验收手段，否则等于没落地。

---

## 3. ble_server 架构

### 3.1 总体结构

单进程单事件循环。`Server` 持有 `DeviceRegistry`（有序，**primary 恒在首位**）与
`SharedServices`（`AdapterCoordinator` / MQTT 连接 ×1 / `BemfaClient` ×1 /
`PortHistory` ×1）。每台设备一份 `ChargerState` + `BLEManager` + `DeviceHistory` +
自己的 status/chart 缓存键。

### 3.2 `DeviceRuntime` 与设备注册表

`DeviceRuntime` = `device_id` / `mac` / `token` / `name` / `topics` / `state` / `manager` /
`history`（facade）/ `spec` / `supervisor` / `task`（**只由 supervisor 持有**）。

`DeviceRegistry.multi` 是"要不要加多设备字段"的唯一判据（SSE `device` 字段、
`/api/health` 的 `devices` 分区、`init` 的 `devices` 列表都看它）。

**每设备 10 项装配钩子**（`Server.mount_device_hooks`，缺一项这台设备就残废，且多数是静默的）：

| # | 钩子 | 缺失后果 |
|---|---|---|
| 1 | scoped SSE（`DeviceSSEEmitter`） | 事件无设备归属，前端把设备 2 画成 primary |
| 2 | MQTT publisher | 完全不发 MQTT |
| 3 | status cache invalidator | 该设备 `/api/status` 永不失效 |
| 4 | restart handler（捕获 `rt`） | 认证卡死无人重建，且绝不能捕获 `s.ble` |
| 5 | quality provider（捕获 `rt`） | quality 显示成 primary 的 |
| 6 | history facade | 不落库，或写进 `default` 行 |
| 7 | 录像开关 | 只加载 primary 的设置 |
| 8–10 | 限额 / 常供 / 即停 | 同上 |

### 3.3 primary 兼容层与 per-request 路由（安全相关）

`ble` / `state` / `history` 是**只读 property**：默认解析 primary，请求上下文可覆盖。

- 请求上下文由 `device_middleware`（最内层）设置：支持 `/api/device/{id}/...`（路径，
  404 可探测）与 `?device=`（查询参数）两种寻址，无参 = primary；
- **五条红线**：① MQTT/SSE/定时器等回调没有请求上下文，**必须闭包捕获 `rt`**，用
  `self.ble` 会永远落到 primary；② 长连接内的设备列表用 `registry` 显式构造；③ 不得依赖
  `self.ble` 判断"当前设备"；④ 重启/质量等回调捕获 `rt`；⑤ 事件归属只能由
  `DeviceSSEEmitter` 注入。
- **P0-2 禁令**：生产 `__init__` 绝不出现 `self.ble = ...` / `self.state = ...` /
  `self.history = ...`。getter 见 `_xxx_assigned` 非空就永久短路 → ContextVar 静默失效
  （比报错更危险）。构造尾部 `_assert_no_assignment()` **raise RuntimeError**（不用 assert：
  `python -O` 会把它删掉）。`Server.__new__` 的 17 处手工装配仍走 setter。

### 3.4 `AdapterCoordinator`：共享网卡

进程内**唯一**允许触碰适配器/bluetoothctl 的模块，由 `Server` 建一个注入所有 manager。

- **扫描**：`scan_lock` + 按 MAC 的 pending 表，同一窗口只扫一次；扫描前的 `scan off`
  清理收口到协调器（以前每个 manager 都做，会掐掉别人的扫描）；
- **并发**：`connect_sem`（默认 2）包住"扫描/连接/认证"窗口；启动错峰
  `server.startup_stagger_sec`（默认 2s，第 N 台 = N×；N=1 恒为 0）；
- **门闸**：`recovery_gate` 在 L2 期间关闭，所有 manager 每次重连尝试前
  `await wait_recovery_gate()`——否则下电期间的重连会刷爆失败计数，把"一台故障"放大成
  "全场熔断"。

### 3.5 分级恢复（禁止跳级）

| 级别 | 动作 | 条件 |
|---|---|---|
| **L1** | `bluetoothctl disconnect <mac>` + 本设备重连 | 一切失败的首选；只影响目标设备 |
| **L1.5** | `bluetoothctl remove <mac>`（清该 MAC 的 BlueZ/GATT 缓存） | L1 后仍失败；大量"认证卡死/缓存损坏"到这一级即可解决 |
| **L2** | 适配器 power off/on（保留旧时序） | **仅 N=1 或全员离线**；多设备且有活连接时跳过、退回 L1.5 + 退避；必须走门闸 |
| **L3** | 认证卡死 | N=1 仍 `os.execv`（与现状一致）；N>1 先做**进程内 per-device 重建** |

- **L2 时序**：关门闸 → 逐个 L1 断开 → power off → 3s → power on → 就绪探测（15 轮，
  两种 `bluetoothctl show` 方言都试，失败退 sysfs）→ 开门闸（异常路径也必须开）。
- **L3 生命周期**（不是"new 一个实例"）：停任务 → cmd_queue 待应答 future 全部 settle 为
  `ConnectionError("device restarting")` → 以 `link_loss` 关闭该设备活跃会话（once 限额与
  已积分能量保留）→ 新建 manager → 重挂全部 10 项钩子 → 启动。
  **重建必须由 supervisor 排进独立任务**：在 manager 自己的任务里调 `stop()` 等于 await
  自己的取消，永远走不完。
- **升级条件**：≥2 台同时 auth-stuck、或适配器级错误签名 → 升级为进程重启。

### 3.6 MQTT：前缀即设备

`TopicNamespace(prefix)` 生成 `{prefix}/{status,settings,port,port/{name},set,ble,charge_event}`。
primary 的 prefix **就是** `mqtt.topic_prefix`（不是镜像，就是 legacy 命名空间）；其它设备
默认 `{base}/{device_id}`，可显式覆盖（前缀互相嵌套是合法且默认的形态，命令订阅是**精确
匹配**、无 wildcard，所以不会串台）。

命令路由用 `topic → (device_id, kind)` 精确查表（回调里禁止用 `self.ble`）。
**LWT 只有一条**（挂 primary 的 status）：paho 每个连接只有一个遗嘱；其它设备的离线判定
交给 HA 的 30s HTTP 健康检查（ESP32 也无 LWT，本来就不依赖它）。

### 3.7 HTTP API：老端点 = primary 视图

同一组 handler 双重挂载（`/api/status` 与 `/api/device/{id}/status`），**零 handler 复制**；
设备解析统一由 middleware 完成。新增 `/api/devices`；`/api/health` 在 N>1 时附 `devices`
真值分区；chart 缓存键加设备维度 `{device_id}:{hours}:{interval}`。

### 3.8 SSE：字段只在 N>1 加

`DeviceSSEEmitter`：`multi=False` 完全透传（连分桶键都不变），`multi=True` 给每条事件注入
`device`，并让 `status` 按 `(queue, device)` 维度各自保留最新。
**边界**：旧缓存 JS + 新多设备配置时，设备 2 的事件会交错进旧 UI——靠 `?v=` 版本号破缓存，
配置页加设备后提示硬刷新。

### 3.9 History：一列之遥

```sql
ALTER TABLE port_history    ADD COLUMN device_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE charge_sessions ADD COLUMN device_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX idx_port_history_dev_port_time ON port_history(device_id, port, timestamp);
```

- 老库启动自动 ALTER，既有行归 `default` → 曲线/会话无缝；**索引必须在 ALTER 之后建**
  （先建会抛 `no such column`，旧版启动即崩的老 bug）；
- `device_id` 一律**关键字参数、排在最后**（不破坏既有调用形状：当时全部用例都按
  `record_port_data(port, data)` 调）；
- meta 命名空间：primary 保持裸键（`charge_limit_wh` 等），其它设备 `{device_id}:{key}`；
  服务级 meta（`web_language`）刻意不分区；
- `DeviceHistory` facade **显式白名单**（不用 `__getattr__` 兜底）：漏一项会 AttributeError
  而不是静默写进 `default` 行；不代理 `connect/close/flush`；
- 会话级读校验归属（别人的 / 不存在的 sid → 404，不再伪装成"没有曲线"）。

### 3.10 配置演进（向后兼容的糖）

`devices:` 节（不含 primary；primary 永远来自 `ble:`）。校验在**启动时**做，宁可拒绝启动也
不"能跑就先跑"：id 规则（`^[a-z0-9][a-z0-9_-]*$` + 保留字）、MAC 全局唯一、token 合法 hex、
前缀不得等于另一台设备的某个 topic。

**不变式：保存 = 启动可接受**。`/api/config` 保存时先写临时文件，再用**与启动相同的入口**
`load_config(tmp_path)` 试算，失败即 400 且删临时文件——否则"存得进去、起不来"，重启被拒后
Web UI 也没了，用户只能手改 YAML。

### 3.11 Bemfa

- 实体 id 规则：primary 沿用 legacy（`cuktech_c1/…cuktech_ble`，老语音指令不许失效），
  其它设备 `{device_id}_c1` 等；
- **`bemfa.entities`**（一行 = 名字 + 设备 + 端口）是建模唯一真源；**没写过这个键**才按
  `allowed_devices × 5 + name_c*` 派生（升级后语音指令一个都不变）。`entities_defined`
  区分"没配过"与"配了 0 条"；
- 派生规则只有一份实现（`_derived_bemfa_plan`），并经 `GET /api/config.derivedEntities`
  下发给配置页做种子——否则配置页自己拼名字会与注册时的显示名漂移，而且"只铺 primary 5 行"
  会让其它设备的开关在保存后消失；
- `_bemfa_publish` 走装配好的 `(device, port) → entity_id` 映射：表里没有的口不发状态；
- 云端残留：上一轮注册过的 id 记在 **DB meta**（`bemfa_registered`），下次启动做差集删除
  （不写 config.yaml：那是用户手写的文件，`yaml.dump` 会抹掉注释与键序）。

### 3.12 Web UI

`device-store.js`：per-device 数据桶 + 当前指针 + 统一 `api()` 出口（自动带 `?device=`）+
SSE 分发器。**DOM 骨架永不重建**：切换设备 = 挪指针 + 重放现有渲染函数（`Proxy` 视图让
既有渲染代码一行不改）。单设备时切换器整条隐藏、事件无 `device` 按 primary 处理。
多设备工作台（`device-bench.js`）：牌堆式切换 + "全部设备"汇总。

### 3.13 per-device 监管与可观测性

`RuntimeSupervisor` 是 `rt.task` 的**唯一持有者**：`start()/stop()`、意外退出监控、带退避
与上限的重建；`handle_enable` 只能调它（直接 cancel/create 会被当成意外退出、立刻重建）。
端口采样 timer 静默死掉同样触发重建（"假在线"最隐蔽的形态）。
日志用实例级 `LoggerAdapter`：非 primary 打 `[desk] ` 前缀，primary 不打（单设备日志逐字节不变）。
L1.5 退避状态（升级不上去时）经 `/api/devices.recovery` 暴露，健康设备不带该键。

---

## 4. ha_integration

### 4.1 per-entry 前缀

模块级 `TOPIC_*` 常量**降级为缺省值**，coordinator 在实例上解析出
`topic_port/settings/status/set/charge_event/ble`；订阅、发布、命令一律走实例属性 →
多 entry 天然不串台。老 entry 没有 `topic_prefix` 键 → `entry_string()` 返回 None →
运行时回退默认值，**无需 `async_migrate_entry`**，unique_id 不重算。

### 4.2 Config Flow：设备发现 + 前缀

`GET /api/devices` 三级回退：404/超时（ESP32、旧 server）→ 单设备流程；1 台 → 自动选中并
取其前缀；N 台 → 设备选择列表（名称 · MAC · 连接状态 · 前缀，同名同型号也能分辨）。
`unique_id = cuktech_{md5(server_url|topic_prefix)[:16]}`，**仅影响新建 entry**。
reauth 沿用 entry 现有 unique_id，不重算。选择期间设备消失 → 重新发现，绝不猜。

### 4.3 ESP32 兼容约束

全部保留：只用 `/api/status` 与 MQTT 前缀寻址、无 `/api/devices` 时回退、无 `charge_event`
时降级、无 LWT 时靠 HTTP 健康检查、限额端点 404 → `local` 模式。

### 4.4 限额委派

探测链 `GET /api/device/{id}/charge-limits` → 404 → `GET /api/charge-limits` → local。
**只有 404 才降级**（超时/500 换 URL 也一样失败，继续尝试只拖慢启动）。

### 4.5 设备身份

`device_info.name` 取 entry title（多 entry 时不再是一串同名设备）；`identifiers` 仍是
`(DOMAIN, entry_id)`——换 MAC 不该让实体在注册表里分裂。options flow 可改
名称/server_url/topic_prefix/device_id 并立即 reload（否则改前缀只能删了重加，实体
unique_id 全断）。

**设备名自动同步**：名字只在"添加集成那一刻"对得上是不够的——之后在 Web 里把设备改叫
"书桌充电器"，HA 卡片仍是老名字，两台同型号充电器只能靠 `_2` 后缀区分。

这里其实有**两个名字**，必须分开看：**设备名**（设备卡片、实体所属设备）与**集成条目名**
（也就是"中枢"那层的标签 = entry title）。前者永远跟服务端走；后者默认也跟随（多设备时
它是区分两张卡最省事的办法），但用户在选项里起过名就固定住——固定条目标签不该顺带把设备名
也冻住，两件事不绑死。实体侧通过 `coordinator.display_name` 读设备名，取值链是
`服务端同步名 → entry.title → DEVICE_INFO["name"]`。

时点：每次 `async_setup`（含 reload、HA 重启）在转发平台**之前**拉一次 `/api/devices`
（与"等 MQTT 就绪"并发，不额外拖慢启动）；另挂 5 分钟周期任务（`NAME_SYNC_INTERVAL`）
覆盖"服务端改名了、HA 既没重启也没重载"，`unload` 时注销。

四条边界，让"自动同步"不等于"自动覆盖"：

* 绑定了 `device_id` 就只认**那台**设备的名字（多设备列表里不会拿错）；
* 老的单设备 entry（无 `device_id`）只在服务端刚好报一台时同步，报多台时不猜；
* 服务端没配 `name`（上报的就是 id 本身）视为"没意见"，不动任何名字；服务端不可达时
  静默跳过（ESP32/旧 server 本来就没这个端点）；
* 用户显式起的名优先：选项里改过 → `name_pinned` 固定条目标签；设备页改过设备名 →
  注册表里的 `name_by_user` 优先，自动同步跳过它。清空选项里的名称 = 取消置位、交回同步。

---

## 5. 一致性保障（验收机制）

P1 原则不是口号，用测试锁死。编号与完整版一致（代码注释里的 `§5.1/§5.3/§5.4/§5.5` 就是下面这几条）。

##### 5.1 Golden 快照

8 份单设备基线（`/api/status`、`/api/health`、SSE `init`/`settings`/`status+port`、MQTT
`publishes`/`session`/`charge_event`）+ 3 份多设备基线（`/api/devices`、N=2 的 health、
N=2 的 SSE init）。**除时间戳类字段（唯一白名单 `uptime_ms`）外无排除项**；HTTP body 比原串、
SSE 比线字节、MQTT 比 `topic+payload+retain+QoS`。带守卫用例：改一个字段 / 一个 retain 就红。

##### 5.2 现有测试基线

每阶段合并门槛 = 全量测试绿；17 处 `Server.__new__(Server)` 手工装配经 §3.3 的 property
setter 继续有效（不要求改写老测试）。

##### 5.3 并发路由零串台

两设备 + 两个并发请求指向不同设备端点，在 `send_command` 里制造 `await` 交错，断言各自
cmd_queue 只收到自己的命令、且命令发出时 `current_device_id()` 仍是自己那台。
防的是 §3.3 的"并发交错把命令发给错误充电器"——安全事故，不是显示 bug。

##### 5.4 路由/装配身份断言

生产构造后 `server.ble/state/history` 均指向 primary 且三个 `_xxx_assigned` 为空；
在 `/api/device/desk/...` 处理期间（真 middleware）三者均指向 `registry["desk"]`。
这条锁的是 P0-2 禁令本身。

##### 5.5 装配完整性

2–3 台设备经**真** `on_startup` 装配，逐项断言 10 个钩子：各自的 MQTT 前缀、状态缓存隔离、
SSE 事件带 `device`（单设备不带）、限额/常供/录像开关从各自 meta 加载、quality 来自各自
provider、history 写入带各自 `device_id`、`on_shutdown` 后两台任务均结束且会话按 shutdown 收尾。

##### 5.6 配置回退

只有 `ble:` 节时 registry 恰有一个 `default`、`TopicNamespace.prefix == mqtt.topic_prefix`；
两节共存 / 跨节重复 MAC / primary 缺失 / 前缀撞车各有断言。

##### 5.7 分级恢复

Mock `bluetoothctl`：N=1 时序与旧版逐条一致；N=2 有活连接时**不出现 power off**；L2 期间
门闸生效（无 manager 在适配器未就绪时发起连接）；L3 重建按生命周期收尾（pending future
全部失败、会话以 `link_loss` 关闭）。

##### 现状

| 手段 | 现状 |
|---|---|
| golden 逐字节 | ✅ 通过（单设备口径未被放水） |
| 并发路由 / 身份 / 装配完整性 | ✅ 通过（真 Server + 真 on_startup） |
| 单真机条件下的多设备全链路 | ✅ **虚拟充电器**：继承真 controller、只替换连接/握手/入站管道，帧几何取自真机 btsnoop、加解密走生产 AES-CCM；3 台虚拟机跑通注册/路由/SSE/DB/命令隔离 |
| 两台真机 canary | ⛔ **未做**（P5，缺硬件） |

测试规模：ble_server 806、ha_integration 280、前端 node 契约 836 断言 + i18n 校验。

---

## 6. 兼容矩阵与回滚

| 场景 | 结果 |
|---|---|
| 老 `config.yaml`（只有 `ble:`） | 单设备 registry，行为逐字节不变 |
| 老 HA entry（无 `topic_prefix`） | 回退默认前缀，unique_id 不重算 |
| 旧 `history.db` | 启动自动 ALTER，数据归 `default`，曲线无缝 |
| 新 HA + 旧 server / ESP32 | `/api/devices` 404 → 单设备流程 |
| ESP32 固件 | 零改动 |
| Bemfa 老语音指令 | primary 实体 id 不变 |

##### 6.1 回滚（共享 DB 的降级陷阱）

**不是"换回镜像"就行**：旧代码按 `port` 查询、无 `device_id` 条件，会把设备 2 的行读进
设备 1 的图表——只把配置改回单设备不够。

1. 升级前 `VACUUM INTO` 备份 `history.db` + 备份 `config.yaml`（Runbook 强制项）；
2. `./cuktech_ctl.sh history-down`：把 `device_id != 'default'` 的行归档到 `*_archive`
   表（幂等、可先 `--dry-run`）；
3. `./cuktech_ctl.sh clean-retained`（可 `--prefix` 指定已删除/改名的旧前缀）：残留
   retained 会在 broker 上变成幽灵设备——MQTT 没有"按通配符发布"，所以按已知叶子 × 实际前缀
   枚举，绝不硬编码 `cuktech/#`。

---

## 7. 阶段与状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | golden 快照 + 真机 notify 帧 fixtures + 测试脚手架 | ✅ |
| P1 | 内核设备化（registry/ContextVar/10 钩子/supervisor）+ 虚拟充电器 + history 迁移 | ✅ |
| P2 | `AdapterCoordinator` + 分级恢复 + 门闸 | ✅ |
| P3 | 多设备出口（`/api/devices`、SSE、MQTT 前缀、Bemfa 命名空间、Web UI、配置页） | ✅ |
| P4 | HA per-entry（发现/选择/options/唯一 id） | ✅ |
| P5 | 两台真机 72h canary | ⛔ 阻塞硬件 → 多设备标 **Beta** |

---

## 8. 实现与设计的偏差（评审要点）

设计没有写、但实现中确定下来的决定，逐条记录理由：

1. **认证失败阈值 15 → 3 + 重启预算**：真机实测"链路硬断 → 设备重放旧 session 数据"后，
   进程内等 120/180/300s 全都不成功，换新进程 30s 必好；原阈值等于"假离线约 1 小时"。
   现在 3 次即升级 L3，前面两次快重试（30/60s）只吃抖动，一小时最多重建 3 次（超出转 600s
   慢退避 + `device_session_stuck`），防重启风暴。
2. **删设备/改前缀时清 retained**：设计只提了回滚脚本，但设备消失后 broker 上的 retain
   会让 HA/MQTT Explorer 看到幽灵设备。现在保存配置时对比新旧清单与各设备前缀，
   在**写盘之后、重启之前**用空 payload + qos1 清掉（`wait_for_publish` 必须带超时——它在事件循环里）。
   历史数据一个字不动（用户资产）。
3. **`bemfa.entities` 实体表**：设计 §3.11 只有 `allowed_devices × 5` 的派生规则，多设备后
   N 台机器的开关在巴法云 App 里全叫"C口1开关"。现在名字与 `(设备,端口)` 绑定，并给配置页
   下发派生种子做无损迁移。
4. **`bemfa_registered` 存 DB meta**（不是 config.yaml）：服务端记账不该改写用户手写的文件。
5. **LWT 仍只有一条**：paho 每连接一个遗嘱；为每台设备造独立连接代价过大，改由 HA 的
   HTTP 健康检查兜底（设计 §3.6 已接受）。
6. **扫描合并的粒度**：`_pending` 按 MAC 建键，同一台设备被并发请求才合并；不同设备仍是
   串行扫描（`scan_lock`），靠启动错峰掩盖。10 台冷启动 ≈ 10×`scan_timeout`，是已知的
   可优化点，未做。
7. **多设备工作台 UI**（`device-bench.js`，牌堆切换 + 全设备汇总）超出 §3.12 的"pill 切换器"，
   是后续交互迭代的产物。

---

## 9. 风险与未完成

| 风险 | 现状 |
|---|---|
| 两台真机互相干扰（并发认证、射频争用） | **未验证**（P5，缺硬件）；软件链路已由虚拟充电器 + 多 entry 测试覆盖 |
| 设备"假在线"（task 静默死） | supervisor 监控 + 定时器看门狗 + 立即发 offline；N=1 曾是完全盲区 |
| 幽灵 retained / 幽灵会话 | 保存期清理 + 启动全设备收尾（`close_stale_sessions(None)`）+ 归档脚本 |
| 解释器/依赖漂移（CI 假失败） | conftest 启动自查 paho≥2.0；CI 从 `pyproject` 装依赖并跑前端契约测试 |
| golden 只覆盖单设备面 | 多设备 golden 仅 `status/health/sse init`；`port/set/chart/sessions` 的 device 维度尚无字节级基线 |

---

## 附：代码地图

| 机制 | 落点 |
|---|---|
| 设备注册表 / 路由 / 钩子 | `ble_server/ha_server.py`：`DeviceRuntime`、`DeviceRegistry`、`DeviceSSEEmitter`、`RuntimeSupervisor`、`device_middleware`、`mount_device_hooks` |
| 适配器协调与恢复 | `ble_server/adapter_coordinator.py`（`recover_device` / `recover_adapter` / `restart_device`） |
| 设备维度写入与查询 | `ble_server/history.py`：`device_id` 列、`meta_key`、`DeviceHistory` |
| 配置与校验 | `ble_server/config.py`：`DeviceConfig`、`TopicNamespace`、`validate_device_specs`、`validate_bemfa_entities` |
| 前端中枢 | `ble_server/web/static/device-store.js`、`device-bench.js`、`config.html` 内联脚本 |
| HA per-entry | `ha_integration/custom_components/cuktech_charger/{const,__init__,config_flow}.py` |
| 验证手段 | `ble_server/tests/golden_harness.py`、`tests/charger_sim.py`、`tests/test_multi_device_*.py` |
| 回滚脚本 | `ble_server/scripts/history_down.py`、`scripts/clean_retained.py`（`cuktech_ctl.sh history-down / clean-retained`） |
