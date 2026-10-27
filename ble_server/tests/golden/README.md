# golden 基线（P0 锁定的单设备外部可见行为）

这些 JSON 是**基线**，不是测试数据。任何改动都必须能回答一句：
"哪一项外部可见行为变了，为什么这是对的"。改不动解释就不要改文件。

## 基线数字（P0 记录时，2026-10-25）

| 套件 | 命令 | 结果 |
|---|---|---|
| BLE Server | `ble_server/.venv/bin/pytest -q` | 540 passed（P0 新增 20：golden 13 + 多设备脚手架 7 ⇒ 560） |
| HA 集成 | `ha_integration/.venv/bin/pytest -q` | 239 passed |

这两组数字是每阶段（P1/P2/P3/P4）的合并门槛：只许增加，减少必须解释。
golden 快照自身在 `CUKTECH_GOLDEN_UPDATE=1` 之外重跑两次结果一致（确定性已验证）。

## 怎么跑

```bash
pytest tests/test_golden_baseline.py                    # 对比（CI / 每阶段合并门槛）
CUKTECH_GOLDEN_UPDATE=1 pytest tests/test_golden_baseline.py   # 重新记录
```

## 覆盖范围（设计文档 §5.1 点名的五类）

| 文件 | 内容 | 产出它的真实路径 |
|---|---|---|
| `http_status.json` | `GET /api/status` 响应体原文 | `Server.handle_status` |
| `http_health.json` | `GET /api/health` 响应体 | `Server.handle_health` |
| `sse_init.json` | SSE 首帧线字节 | `Server.handle_sse`（真 handler + mocked payload writer） |
| `sse_status_and_port_update.json` | `status`（latest-only）+ `port_update` 投递序列 | `BLEManager._publish_status` / `_emit_port_state` → `SSEEmitter` |
| `sse_settings.json` | `settings` 事件 | `BLEManager._publish_settings` |
| `mqtt_publishes.json` | 四类 topic 的 `topic/payload/retain/qos` | `Server.mqtt_publish` → paho 边界 |
| `mqtt_charge_event.json` | `charge_event` 载荷 | `BLEManager._publish_charge_event` |
| `mqtt_session.json` | 遗嘱(LWT) + 订阅清单 | `Server.setup_mqtt` / `setup_mqtt_subscriptions` |

环境由 `tests/golden_harness.py:golden_env()` 提供：真 `Server.__init__` + 真
`on_startup`，只替换 BLE 硬件（`BLEManager.start` 空转）、MQTT broker（记录替身）、
墙钟（冻结在 `FROZEN_NOW`）。设备状态由 `snapshot_state()` 固定。

## 口径

- **逐字节**：HTTP 与 SSE 存原始字符串（不是重新 dump 的 JSON），MQTT 存 paho 收到的
  原始 payload 文本 —— 键序、转义、`ensure_ascii=False` 的非 ASCII 处理都在比对范围内。
- **无排除项**：除 `timestamp_fields.md` 白名单外不裁剪任何字段。白名单是封闭清单，
  加一项需要理由。
- 快照里出现 `<UPTIME_MS>` 哨兵之外的一切动态值，都说明有"运行时刻"泄进了外部可见
  载荷 —— 那是缺陷，不是快照噪声。

## 与多设备改造的关系

P1/P2/P3 的验收要求"单设备时逐字节不变"（§2 原则 1、§5.1）。执行方式就是：改造后
不改任何基线文件跑 `pytest tests/test_golden_baseline.py` 必须全绿。多设备才新增的
字段（`device`、`devices` 分区等，§3.8）在单设备配置下**不得**出现在这些快照里 ——
`test_golden_baseline.py::TestBaselineGuard` 里有一条反向验证锁住这一点。

## 多设备基线（N≥2）

同一套 `golden_env` 只把配置从 1 台扩到 2 台（`golden_env(devices=[...])`），
断言在 `tests/test_golden_multi_device.py`：

| 文件 | 内容 | 何时出现 |
|---|---|---|
| `http_devices.json` | `GET /api/devices` 的**单设备**形态（N=1 也有基线） | 端点本身（§3.7） |
| `http_health_multi.json` | N=2 `/api/health` 的 `devices` 真值分区 | N>1（§3.13） |
| `sse_init_multi.json` | N=2 SSE `init` 帧，附 `devices` 列表 | N>1（§3.8） |

```bash
pytest tests/test_golden_multi_device.py                          # 对比
CUKTECH_GOLDEN_UPDATE=1 pytest tests/test_golden_multi_device.py  # 重新记录
```

这三份**按设计**携带 `device`/`devices` 字段，因此列在
`golden_harness.MULTI_DEVICE_BASELINES` 里，被上面的单设备红线扫描跳过。跳过是
可证伪的：`test_golden_multi_device.py::TestBaselineSet` 反向断言白名单里的文件都
存在且确实带设备维度字段 —— 白名单不是放水后门。

`/api/devices` 单项契约固定为 `{id, name, mac, connected, topic_prefix, is_primary}`，
顺序 = 配置顺序（primary 首位）；加字段等于改外部契约，必须同步改基线与本文件。

