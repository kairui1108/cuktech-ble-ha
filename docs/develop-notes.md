# CUKTECH BLE Server 项目状态

> 更新: 2026-07-12

## 功能矩阵

| 功能 | 状态 | 实现方式 |
|------|:----:|----------|
| BLE 连接 + 认证 | ✅ | MiOT Auth |
| 端口数据读取 (V/C/W) | ✅ | BLE Spec `0f20` 帧 + MiOT `0c20` 帧 |
| 端口协议检测 (V2) | ✅ | PDO + PIID21 + 电压启发式 |
| **C1 协议开关** (PD/PPS/UFCS) | ✅ | MiOT SET PIID21 (设备基准编码) |
| **C2 协议开关** (PD/PPS/UFCS) | ⚠️ | SET 有效但可能间接影响 C1 / PD ON 被设备拒绝 |
| **C3 协议开关** (UFCS/SCP) | ❌ | PD ON 被设备拒绝 (session 权限?) |
| **A 协议开关** (UFCS/SCP) | ❌ | 同上 |
| 端口开关 (PIID16) | ✅ | MiOT 1-byte SET |
| 场景模式 (PIID5) | ✅ | MiOT SET |
| 息屏时间 (PIID6) | ✅ | MiOT SET |
| USB-A 常通电 (PIID15) | ✅ | MiOT SET |
| 空闲息屏 (PIID19) | ✅ | MiOT SET |
| 屏幕方向锁 (PIID20) | ✅ | MiOT SET |
| MQTT 发布 | ✅ | 端口数据 + 状态 |
| HTTP API | ✅ | REST API |
| Web UI 协议开关控件 | ✅ | 弹窗内 toggle 开关 |

## 已知问题 & 待修复

### 1. C1 协议开关可能间接影响 C2

**现象:** 通过本项目 toggle C1 PD/PPS 后，C2 的 PIID21 标志可能被设备固件清零。

**原因:**
- 设备 GET PIID21 只返回低 1 字节 (C1)，C2 字节无法直接读取
- 初始读取时按默认值 `0x0F` (全开) 构造，可能与设备实际值不匹配
- 4-byte SET 写入时会将不匹配的 C2 字节写回设备，导致 C2 被关闭

**缓解措施:**
- SET 时非目标端口使用设备当前读取值作为基线
- 17-byte MiOT SET 格式 (含 `property_index=0x0000`) 对齐 Mi Home

**用户需要了解:**
- ⚠️ 操作 C1 协议开关后，请在米家 App 中检查 C2 协议状态
- 如 C2 协议被关闭，需通过米家 App 手动恢复
- C2/C3/A 的 PD/PPS 单向 on 操作可能被设备拒绝 (返回当前值不变)

### 2. C2/C3/A PD ON 被设备拒绝

**现象:** 通过本项目 set PD/PPS=ON 时，设备回读仍为 OFF。

**推测原因:** 设备固件对高字节 PIID21 SET 有权限验证，或 BLE session 缺少某种"管理权限"标记 (cloud binding / package signature)。Mi Home App 通过云端绑定获得完整权限，本地直连 session 受限。

**待研究方向:**
- [ ] 对比 Mi Home 与本地直连的 auth session 差异
- [ ] 尝试通过 Frida 截获 BLE Spec `setPropertiesValue` 的原始 Binder 调用
- [ ] 探索是否可通过 BLE Spec FlatBuffers 通道绕过限制

### 3. PDO 数据动态变化

**现象:** PIID17 (PDO 能力) 的值在设备运行中会改变 (如 kind 从 `0x09`→`0x07`)。

**影响:** 协议检测依赖 PDO kind 判断 PPS 支持，动态值可能导致误判。

**缓解:** 当 PDO=PD Fixed 但 PIID21 中 PPS=ON 时，降级使用电压启发式。

## 协议开关编码细节

### PIID21 SET 命令格式 (17-byte)

```
byte 0-1:   0c 20        (frame header)
byte 2:     seq           (sequence number)
byte 3:     00
byte 4:     00            (SET opcode)
byte 5:     01            (count)
byte 6:     02            (siid: charger service)
byte 7-8:   15 00         (piid: 21, 2-byte LE)
byte 9-10:  00 00         (property_index: main property)
byte 11:    04            (value length: 4)
byte 12:    10            (value type: int32)
byte 13-16: c1 c2 c3 a    (4-byte LE value)
```

### PIID21 位定义

```
bit 0:   C1 PD
bit 1:   C1 PPS
bit 2:   C1 UFCS
bit 3:   C1 reserved (固定 1)
bit 8:   C2 PD
bit 9:   C2 PPS
bit 10:  C2 UFCS
bit 11:  C2 reserved (固定 1)
bit 16:  C3 UFCS
bit 17:  C3 SCP
bit 24:  A UFCS
bit 25:  A SCP
```

## 逆向进展

| 阶段 | 方法 | 成果 |
|------|------|------|
| Smali 反编译 | baksmali | 完整写入链路: pr3→mp0→IBleChannelWriter → CMD_SEND |
| Hermes 分析 | hbctool + 字符串搜索 | `parsePortInfo`, `setProtocolExtend`, `getC1C2ProtocolStr` |
| HCI 抓包 | Android btsnoop + Python 解析 | 确认帧格式与 Mi Home 完全一致 (17-byte) |
| Frida 动态 Hook | Java + GATT hooks | 捕获 PIID21 SET 帧, HKDF 密钥派生验证通过 |
| HCI 离线解密 | `ha-cuk-ble/tools/decrypt_btsnoop_miot.py` 原理 | 确认加密/密钥派生与社区实现一致 |
| 协议检测 V2 | PDO + PIID21 + 电压启发式 | 对齐米家 PPS/PD 判定 |

## 已知社区项目

| 项目 | 作者 | 平台 | PIID21 |
|------|------|------|:---:|
| [ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) | zuyan9 | Home Assistant | ❌ |
| [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) | zhyzhaogit | 独立 Web UI | ❌ |
| **本项目** | ruikai | BLE Server + HA | ⚠️ C1 |

## 目录结构

```
docs/
├── BLESPEC_REVERSE_ENGINEERING.md  # BLE Spec 逆向分析
├── mijia-plugin-analysis.md        # 米家插件分析
├── develop-notes.md                # 本文件
├── dex/                            # DEX 文件 (classes{11,12,13}.dex)
├── smali_out/                      # 反编译 smali
├── plugin_analysis/                # Hermes 插件 + 提取的 JS 源码
├── btsnoop_*.log                   # HCI 抓包日志
├── frida_hook_*.js                 # Frida Hook 脚本
└── frida_hook_*.log                # Frida 输出日志 (在 /tmp/ 下)
```
