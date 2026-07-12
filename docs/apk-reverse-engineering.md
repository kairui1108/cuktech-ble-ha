# BLE Spec 协议逆向分析报告

> 最后更新: 2026-07-12
> 分析来源: Xiaomi Home APK DEX 反编译 + Hermes 插件 bundle 提取 + Frida 动态 Hook + HCI btsnoop 抓包

---

## 一、协议栈层次

```
React Native Plugin (JS) — Hermes indexed-ram-bundle
  └─ _miot.Bluetooth.spec.setPropertiesValue(mac, json)
       │  json = {objects: [{siid, piid, value, type: 5}]}
       └─ Xiaomi Native SDK (Java/C++)
            ├─ m78.setPropertiesValue → 构建 FlatBuffers/TLV 消息
            ├─ pr3.OooO0o0() → broadcast "action.miot.write.specv2.ble.data"
            ├─ SpecWriteChannelManager$Sender → 接收广播
            ├─ mp0.OooO0OO() → AES-CCM 加密
            └─ IBleChannelWriter.write(data, frameType=0) → Binder IPC → GATT
```

---

## 二、认证与加密 (共享层)

**BLE Spec 与 MiOT 共用同一认证流程和会话密钥。**

### 认证流程 (rv0)
按标准 MiOT BLE 登录 (0xa4 → 0x24 → HKDF → HMAC)。认证后派生:
- `session_key[0:16]`  = dev_key (接收方向解密)
- `session_key[16:32]` = app_key (发送方向加密)
- `session_key[32:36]` = dev_iv
- `session_key[36:40]` = app_iv

### HKDF 密钥派生
```python
# 已验证与 ha-cuk-ble 项目的 derive_login() 完全一致
salt = app_rand + dev_rand
HKDF(algorithm=SHA256, salt=salt, info=b"mible-login-info", length=64).derive(token)
```

**验证来源:** Frida HMAC-SHA256 hook 捕获了完整的密钥派生过程，Auth HMAC 计算与 Mi Home 一致。

### 加密 (mp0.OooO0OO)
```python
# 已验证与 ha-cuk-ble 的 encrypt() 完全一致
nonce = app_iv(4) + b"\x00"*4 + counter(4 LE)  # 12 bytes
ciphertext = AES-CCM(app_key, tag=4).encrypt(nonce, plaintext)
frame = [0100] + [counter:2B LE] + ciphertext   # GATT 写入帧
```

**注意:** 加密 nonce 使用 4 字节 counter，输出帧使用 2 字节 counter。解密 nonce 使用 `dev_iv + 0x00*6 + counter(2 LE)`。

---

## 三、完整写入链路

```
pr3.OooO0o0()
  └─ broadcast Intent("action.miot.write.specv2.ble.data")
       extra: "mac"=mac, "value"=data

SpecWriteChannelManager$Sender.onReceive()
  └─ mp0.OooO0OO(mac, data, callback)

mp0.OooO0OO(mac, data, callback)
  ├─ sessionKey = sessionKeyRepo.get(mac)
  ├─ app_key = sessionKey[16:32]
  ├─ app_iv  = sessionKey[36:40]
  ├─ encrypted = AES-CCM(app_key).encrypt(nonce, data)
  ├─ frame = [send_it:2B LE] + encrypted
  └─ IBleChannelWriter.write(frame, frameType=0)

IBleChannelWriter.write(frame, frameType=0)
  └─ OooO0o.OooO → IBleConnectManager.writeNoRsp(mac, FE95, 0000001A, frame)
      └─ BluetoothGatt.writeCharacteristic(CMD_SEND, frame, WRITE_TYPE_NO_RESPONSE)
```

**关键发现:**
- BLE Spec SET 的 GATT 通道是 **0000001A (CMD_SEND)** — 与 MiOT SET **相同** ✅
- 写入方式是 **writeNoRsp** (无 MiOT header/RCV_RDY 握手)
- 加密方式与 MiOT **完全一致**

---

## 四、GATT 通道映射

| UUID 后缀 | 用途 | 方向 |
|-----------|------|------|
| 00000019 | 认证数据 (auth_data) | Write + Notify |
| 0000001a | CMD_SEND (MiOT + BLE Spec 共享) | Write + Notify |
| 0000001b | CMD_RECV | Write + Notify |
| 0000001c | 设备信息 + 通知 | Write + Notify |
| 00000005 | BLE Spec 通知通道 | Notify |
| 00000010 | 认证控制 (auth_ctrl) | Write |
| 0000001d | 固件版本 | Read |

---

## 五、端口数据格式 (BLE Spec)

### 来源: `0f20` 帧 → cmd_recv 通道

**帧格式:**
```
[0f 20][seq:2B LE][type=04][count=01][siid=02][piid][00 04][50][value:4B LE]
```

### 32-bit 端口值 (`parsePortInfo` - 插件源码确认)

```
value.toString(2).padStart(32, '0')
  bits  0-7  (substr 0,8):   voltage_raw  (V×10)
  bits  8-15 (substr 8,8):   current_raw  (A×10)
  bits 16-23 (substr 16,8):  protocol_number (1-10, 硬件直出)
  bits 24-31 (substr 24,8):  port_status
```

### 协议号查表 (`getC1C2ProtocolStr` - 插件源码确认)

| 协议号 | 名称 |
|--------|------|
| 0 | idle |
| 1,2 | 5V |
| 3 | QC |
| 4 | AFC |
| 5 | FCP |
| 6 | SCP |
| 7 | PD |
| 8,9 | PPS |
| 10 | UFCS |

### C3/A 端口说明
C3/A 的 BLE Spec 帧中 `protocol_number` 字节使用 MiOT code (0x60, 0x70, 0x80)，非标准协议号。需 fallback 到启发式检测（基于电压档位）。

---

## 六、PIID 完整映射 (siid=2)

| PIID | 名称 | 类型 | 说明 |
|------|------|------|------|
| 1 | port_info_c1 | u32 | C1 端口数据 |
| 2 | port_info_c2 | u32 | C2 端口数据 |
| 3 | port_info_c3 | u32 | C3 端口数据 |
| 4 | port_info_a | u32 | USB-A 端口数据 |
| 5 | scene_mode | u8 | 场景模式 1-4 |
| 6 | screen_save_time | u8 | 息屏时间 |
| 7 | protocol_ctrl | u8 | 协议控制 bit flags |
| 9-12 | delay_time_{c1,c2,c3,a} | int | 延时关闭 |
| 13 | screen_language | u8 | 屏幕语言 |
| 15 | switch_usba_trickle | bool | USB-A 常通电 |
| 16 | port_ctrl | u8 | 端口开关 bitmask |
| 17 | c1_c2_protocol | u32 | C1/C2 PDO 协议能力 |
| 18 | c3_a_protocol | u32 | C3/A PDO 协议能力 |
| 19 | idle_screen_off | bool | 空闲息屏 |
| 20 | screen_direction_lock | bool | 屏幕方向锁 |
| 21 | protocol_ctl_extend | u32 | 协议扩展开关 |

### PIID 21 位定义 (`setProtocolExtend` - 插件源码确认)

```javascript
// 编码 (插件源码)
c1Flags = 0x08 | (ufcs ? 0x04 : 0) | (pps ? 0x02 : 0) | (pd ? 0x01 : 0)
c2Flags = 0x08 | (ufcs ? 0x04 : 0) | (pps ? 0x02 : 0) | (pd ? 0x01 : 0)
c3Flags = (scp ? 0x02 : 0) | (ufcs ? 0x01 : 0)
aFlags  = (scp ? 0x02 : 0) | (ufcs ? 0x01 : 0)
protocolExtendValue = (aFlags << 24) | (c3Flags << 16) | (c2Flags << 8) | c1Flags
```

所有协议开关默认为 **ON**。米家通过 `setPropertiesValue([{siid:2, piid:21, value, type:5}])` 发送。

### PIID 16 位定义 (端口开关)
```
bit0=C1, bit1=C2, bit2=C3, bit3=A
0x0F = 全开, 0x00 = 全关
```

---

## 七、关键技术发现

### 1. HCI btsnoop 抓包分析
通过 Android 蓝牙 HCI snoop log 对比验证：
- Mi Home 的 PIID21 SET 帧格式: `0100[seq:2B LE][AES-CCM ciphertext:19B]` = 23 字节
- 与我们的 `_encrypt` 输出**完全一致** ✅
- 米家操作 C2 PD 时只发送 PIID21 SET，**无额外 setup/reset 命令**

### 2. Frida 动态 Hook
- ✅ BluetoothGatt.writeCharacteristic: 捕获所有 PIID21 SET 帧
- ✅ HMAC-SHA256: 捕获 HKDF 密钥派生，确认与 `ha-cuk-ble` 的 `derive_login()` 一致
- ✅ Auth HMAC: 数学验证通过，app_key 正确
- ❌ AES-CCM plaintext: BLE 加密在 native 层，Java hook 不可达

## 八、社区项目对比

| 项目 | 类型 | PIID21 协议开关 |
|------|------|:---:|
| [zuyan9/ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) | HA 集成, 350+⭐ | ❌ "writable semantics unknown" |
| [zhyzhaogit/cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller) | 独立控制器 + Web UI | ❌ 未实现 |
| **ruikai/cuktech** (本项目) | BLE Server + HA 集成 | ⚠️ C1 可用, C2/C3/A 未攻克 |

---

## 九、实现状态

### 已完成 ✅

| 功能 | 实现 | 说明 |
|------|------|------|
| BLE 连接 + 认证 | MiOT auth (共享) | 与米家 App 相同 |
| 端口数据读取 | BLE Spec `0f20` 帧解析 | 硬件协议号查表, 100% 准确 |
| 端口协议检测 | 硬件 protocol_number + 查表 | C3/A fallback 启发式 |
| 端口开关 (PIID16) | MiOT 1-byte SET | bit0=C1, bit1=C2, bit2=C3, bit3=A |
| 端口 reset | PIID16 OFF→ON | 支持 C1/C2 |
| C1 协议开关 | MiOT SET PIID21 (低字节) | 双向工作 ✅ |
| PIID SET (1-byte) | MiOT SET | 所有 PIID |
| FlatBuffers 编码器 | `flatbuffers.py` | sae 类完全兼容 |


### 未完成 ❌

| 功能 | 原因 |
|------|------|
| C2/C3/A 协议开关 | 设备拒绝 MiOT SET 高字节写入 |
| BLE Spec TLV SET | 帧发送成功但设备不执行 |
| 完全去除 MiOT | 写入仍需 MiOT 通道 |

---

## 十、阻塞项: C2/C3/A 协议开关

### 现象
- MiOT 4-byte SET PIID21: C1 (低字节) 设备执行 ✅, C2/C3/A (高字节) ACK 但不执行 ❌
- BLE Spec TLV SET: 设备不响应
- 相同 token、相同 HKDF、相同加密 — Mi Home 可以, 我们不行

### 已排除的原因
- ❌ 帧格式: HCI + Frida 双重验证, 完全一致
- ❌ 加密算法: 与 `ha-cuk-ble` 项目交叉验证, 一致
- ❌ GATT 通道: smali + Frida 确认 CMD_SEND
- ❌ Token: 与 Mi Home 使用相同的 Xiaomi-cloud-tokens-extractor token
- ❌ 认证流程: HCI 对比, 完全相同

### 推测根因
设备固件层面对 C2/C3/A 写入有额外权限控制，可能与 auth 后的某种状态标记或插件版本相关。ha-cuk-ble 项目也未攻克此问题 ("writable semantics still unknown")。

---

## 十一、关键 Smali 类映射

| obfuscated 类 | 功能 | DEX 位置 |
|---------------|------|----------|
| `_m_j/mp0` | AES-CCM 加密 + 帧封装 | classes12.dex |
| `_m_j/gr1` | GATT 写入器 | classes13.dex |
| `_m_j/wn5` | IBleChannelWriter 接口 | classes13.dex |
| `SpecWriteChannelManager$Sender` | BLE Spec 写入广播接收器 | classes12.dex |
| `SpecWriteChannelManager$OooO` | 写入回调 | classes12.dex |
| `OooO0o` | IBleConnectManager 写入实现 | classes12.dex |
| `_m_j/au0` | UUID 常量 (OooOOOO=0x001A) | classes13.dex |
| `_m_j/j7c` | SpecWriteChannelManager 工厂 | classes12.dex |
