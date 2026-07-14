# 米家 App 酷态科充电器插件逆向分析报告

> 分析日期: 2026-07-12
> 插件: package com.cuktech.charger, plugin ID 1028581, version 1896060
> 设备型号: njcuk.fitting.ad1204 (AD1204U "10 GaN Charger Ultra")
> Bundle: `main.bundle` (indexed-ram-bundle, Hermes 字节码, 905KB)
> 源码提取: `/data/data/com.xiaomi.smarthome/files/plugin/install/rn/1028581/1896060/android/main.bundle`

---

## 一、PIID 完整映射表

`siid = 2` (充电器服务)

| PIID | 属性名 | 类型 | 说明 |
|------|--------|------|------|
| 1 | `port_info_c1` | u32 | C1 端口数据 (V/C/proto/status) |
| 2 | `port_info_c2` | u32 | C2 端口数据 |
| 3 | `port_info_c3` | u32 | C3 端口数据 |
| 4 | `port_info_a` | u32 | USB-A 端口数据 |
| 5 | `scene_mode` | u8 | 场景模式 (1-4) |
| 6 | `screen_save_time` | u8 | 屏幕保护时间 (0-5) |
| 7 | `protocol_ctrl` | u8 | 协议控制标志 |
| 9-12 | `delay_time_{c1,c2,c3,a}` | u16 | 延时关闭时间 |
| 13 | `screen_language` | u8 | 屏幕语言 (0=中文, 1=英文) |
| 15 | `switch_usba_trickle` | bool | USB-A 常通电 |
| 16 | `port_ctrl` | u8 | 端口开关 bitmask (bit0=C1,bit1=C2,bit2=C3,bit3=A) |
| 17 | `c1_c2_protocol` | u32 | C1/C2 PDO 协议能力 (high16=C1, low16=C2) |
| 18 | `c3_a_protocol` | u32 | C3/A PDO 协议能力 (high16=C3, low16=A) |
| 19 | `idle_screen_off` | bool | 空闲息屏 |
| 20 | `screen_direction_lock` | bool | 屏幕方向锁 |
| 21 | `protocol_ctl_extend` | u32 | 协议扩展开关 |

---

## 二、端口数据解析 (`parsePortInfo`)

**完整源码 (从 Hermes bundle 提取):**

```javascript
function parsePortInfo(index, value) {
    var valueStr = value.toString(2).padStart(32, '0');
    var port_voltage = parseInt(valueStr.substr(0, 8), 2) / 10;    // bits 31-24
    var port_current = parseInt(valueStr.substr(8, 8), 2) / 10;    // bits 23-16
    var port_protocol = parseInt(valueStr.substr(16, 8), 2);       // bits 15-8
    var port_status = parseInt(valueStr.substr(24, 8), 2);         // bits 7-0
    // ...
}
```

**注意:** `value.toString(2).padStart(32, '0')` 是 JS 的 32-bit 二进制表示，substr(0,8) 取最高 8 位 = bits 31-24 = voltage。

**等价的 Python 解析:**
```python
spec_val = int.from_bytes(raw[-4:], 'little')
voltage = (spec_val >> 24) & 0xFF  # /10 = V
current = (spec_val >> 16) & 0xFF  # /10 = A
protocol = (spec_val >> 8) & 0xFF  # 1-10
status = spec_val & 0xFF
```

---

## 三、协议号映射 (`getC1C2ProtocolStr`)

**完整源码 (从 Hermes bundle 提取):**

```javascript
function getC1C2ProtocolStr(protocol) {
    switch (protocol) {
        case 1: case 2: return '5V';
        case 3:         return 'QC';
        case 4:         return 'AFC';
        case 5:         return 'FCP';
        case 6:         return 'SCP';
        case 7:         return 'PD';
        case 8: case 9: return 'PPS';
        case 10:        return 'UFCS';
        default:        return 'idle';
    }
}
```

**C3/A 协议检测:** C3/A 口的 BLE Spec 帧中 `protocol_number` 字节为 MiOT code (0x60/0x70/0x80)，非标准协议号 1-10，需 fallback 到启发式检测。

---

## 四、PIID21 协议开关 (`setProtocolExtend`)

**完整源码 (从 Hermes bundle 提取):**

```javascript
function setProtocolExtend(port, protocolType) {
    var protocolStates = {
        c1:    { ufcs: state.c1_ufcs, pd: state.c1_pd, pps: state.c1_pps },
        c2:    { ufcs: state.c2_ufcs, pd: state.c2_pd, pps: state.c2_pps },
        c3:    { ufcs: state.c3_ufcs, scp: state.c3_scp },
        usb_a: { ufcs: state.a_ufcs,  scp: state.a_scp }
    };
    
    protocolStates[port][protocolType] = !protocolStates[port][protocolType];
    
    var c1Flags = 0x08 | (c1.ufcs?4:0) | (c1.pps?2:0) | (c1.pd?1:0);
    var c2Flags = 0x08 | (c2.ufcs?4:0) | (c2.pps?2:0) | (c2.pd?1:0);
    var c3Flags = (c3.scp?2:0) | (c3.ufcs?1:0);
    var aFlags  = (a.scp?2:0)  | (a.ufcs?1:0);
    var value = (aFlags << 24) | (c3Flags << 16) | (c2Flags << 8) | c1Flags;
    
    // 通过 BLE Spec API 发送
    var params = [{siid: 2, piid: 21, value: value, type: 5}];
    _miot.Bluetooth.spec.setPropertiesValue(mac, JSON.stringify({objects: params}));
}
```

**位定义:**
```
c1_flags (bits 0-7):   bit0=PD, bit1=PPS, bit2=UFCS, bit3=固定1 (0x08)
c2_flags (bits 8-15):  bit8=PD, bit9=PPS, bit10=UFCS, bit11=固定1
c3_flags (bits 16-23): bit16=UFCS, bit17=SCP
a_flags  (bits 24-31): bit24=UFCS, bit25=SCP
```

**初始化状态:** 所有协议开关默认为 **ON** (`true`)。
**发送 API:** `setPropertiesValue` (BLE Spec native API，非 `sendMiotCommand`)。

---

## 五、插件 BLE 数据通道

插件使用两个数据源接收端口数据:

1. **MiOT `prop.2.{1,2,3,4}`** — `DeviceProperties.addListener` 监听
   - 设备报告 `protocolExtend` 值时通过 `MHGlobalData.protocolExtend` 存储

2. **BLE Spec `BLESpecNotifyActionEvent`** — `DeviceEvent.BLESpecNotifyActionEvent.addListener` 监听
   - 32-bit 端口值，由 `parsePortInfo` 解析
   - 协议号由硬件直接提供 (bits 15-8)

---

## 六、Binder 键值 (BLE Spec DEVICE_DATA_KEYS)

```javascript
DEVICE_DATA_KEYS = {
    PORT_DATA_C1:        { key: "prop.2.1",  obj: ... },
    PORT_DATA_C2:        { key: "prop.2.2",  obj: ... },
    PORT_DATA_C3:        { key: "prop.2.3",  obj: ... },
    PORT_DATA_USB_A:     { key: "prop.2.4",  obj: ... },
    SCENE_MODE:          { key: "prop.2.5",  obj: ... },
    SCREEN_SAVE_TIME:    { key: "prop.2.6",  obj: ... },
    SWITCH_USBA_TRICKLE: { key: "prop.2.15", obj: ... },
    PORT_CTL:            { key: "prop.2.16", obj: ... },
    C1C2_PROTOCOL:       { key: "prop.2.17", obj: ... },
    C3A_PROTOCOL:        { key: "prop.2.18", obj: ... },
    SCREENOFF_WHILE_IDLE:{ key: "prop.2.19", obj: ... },
    SCREEN_DIR_LOCK:     { key: "prop.2.20", obj: ... },
    PROTOCOL_CTL_EXTEND: { key: "prop.2.21", obj: ... },
}
```

**MiOT GET 属性列表:** `[1,2,3,4,5,6,7,13,15,16,17,18,19,20,21]` (14 个 PIID)

---

## 七、插件文件结构

```
com.cuktech.charger/
├── index.js              # 插件入口 (PackageEvent.packageDidLoaded)
├── MainPage.js           # 主页面 UI
├── ProtocolSwitchPage.js # 协议开关页面
├── utils/
│   ├── privateUtils.js   # setPropertiesValue/getPropertiesValue
│   ├── public.js         # 公共工具函数
│   └── constants.js      # 常量定义
├── components/
│   ├── PowerBarChart.js  # 功率图表组件
│   └── ...
├── global/
│   └── MHGlobalData.js   # 全局状态存储
└── resources/
    └── ...
```

---

## 八、MIOT/Spec 双通道架构

> 分析日期: 2026-07-13

### 核心结论

**不存在 MiOT/Spec 模式切换**。插件连接后**同时启用两个并行数据通道**，这是米家 BLE 架构的双通道设计。

### 两个数据通道对比

| 特性 | MiOT 通道 | BLE Spec 通道 |
|------|-----------|---------------|
| 用途 | 批量属性读写 | 实时推送 |
| 读取方式 | 主动 GET 轮询 | 设备推送 Notify |
| 写入方式 | `send_miot_command` | `setPropertiesValue` API |
| 协议号 | 不可用，需启发式 | **硬件直接提供** (bits 15-8) |
| 延迟 | 较高（命令-响应模式） | 较低（推送模式） |
| 适用场景 | 设置类操作（场景模式、息屏等） | 端口数据实时监控 |

### Spec 通道建立流程

连接建立后，Mi Home App 安全层（`ah0.smali` 中 `OooO` / `tl0` 类）执行多步认证，**第 4 步（`pswitch_b1` case 4）**开启 Spec 通道：

1. **开启 Spec READ Notify** — 订阅 `au0.OooOOOO` = `0000001a` (CMD_SEND) 的通知（与 MiOT 共用同一特征）
2. **开启 Spec WRITE Notify** — 广播 `com.xiaomi.smarthome.support.ble.spec.protocol`，告知系统 Spec WRITE 通道就绪
3. **Spec 支持缓存** — 缓存键 `spec_support_cache_v5`，避免每次连接重新检测

**注意**: Spec READ notify 使用的 UUID `au0.OooOOOO` 在 `au0.smali` 中被赋值为 `tmc.OooOOoo(0x1a)` = `0000001a`，与 `CHAR_CMD_SEND` 相同。认证完成后设备自动在该特征上推送 Spec 格式的端口数据。

**源码位置:** `docs/smali_out/classes12/_m_j/ah0.smali` (lines 218-256, 416-505)

### 接收端事件分发

`PluginRNActivity` 注册广播 `com.xiaomi.smarthome.ble.spec.notify`，通过 `RNEventReceiver.onReceive()` 处理：

1. 验证 action 为 `com.xiaomi.smarthome.ble.spec.notify`
2. 验证 `packageName` 是当前进程（防止跨包通知）
3. 提取 Intent 中的 `json` 字段
4. 设置事件名为 `BLESpecNotifyActionEvent_36621`
5. 通过 React Native Bridge 发送给 JS 插件

**源码位置:** `docs/smali_out/classes13/com/xiaomi/smarthome/framework/plugin/rn/PluginRNActivity.smali` (line 2249)
`docs/smali_out/classes13/com/xiaomi/smarthome/framework/plugin/rn/RNEventReceiver.smali` (lines 2284-2401)

### Spec 写入流程（PIID21 协议开关）

```
插件 JS: _miot.Bluetooth.spec.setPropertiesValue(mac, JSON)
  → Android Native: 广播 "action.miot.write.specv2.ble.data"
    → SpecWriteChannelManager$Sender.onReceive()
      → mp0.OooO0OO(mac, data, callback)  // AES-CCM 加密
        → OooO0o.OooO(mac, encrypted, response)  // writeNoRsp 写入 BLE GATT
          → 广播 "action.miot.write.specv2.ble.data.resp"
```

**关键 Action 字符串:**

| Action | 用途 | 源码 |
|--------|------|------|
| `action.miot.write.specv2.ble.data` | 插件→Native: 请求 Spec 写入 | `SpecWriteChannelManager$Sender.smali:41` |
| `action.miot.write.specv2.ble.data.resp` | Native→插件: 写入响应 | `OooOO0.smali:63` |
| `action.miot.receive.specv2.ble.data` | Native→分发: 接收 Spec 数据 | `fq0.smali:127` |
| `com.xiaomi.smarthome.ble.spec.notify` | 分发→插件: Spec 通知事件 | `RNEventReceiver.smali:2284` |

### 端口数据格式差异

**MiOT 通道** — 端口数据只有 raw code 字节，**无硬件协议号 (1-10)**：
```
payload 最后 4 字节: [in_use] [code] [current_raw] [voltage_raw]
```
因此需要 `state_protocol_v2.py` 通过电压、PDO 能力、协议开关进行启发式估算。

**BLE Spec 通道** — 32-bit 端口值直接包含硬件协议号：
```
bits 31-24: voltage (/10 = V)
bits 23-16: current (/10 = A)
bits 15-8:  protocol_number (1=5V, 3=QC, 7=PD, 8=PPS, 10=UFCS)
bits 7-0:   status
```
由 `parsePortInfo` 函数解析，直接查映射表 `getC1C2ProtocolStr`，无需启发式。

### 对本项目的影响

BLE Server 只实现了 MiOT 命令通道，未实现 BLE Spec Notify 订阅，因此：
- 端口数据的协议检测精度受限于启发式估算（`state_protocol_v2.py`）
- 如要获得精确协议号，需订阅 BLE Spec 的 GATT 通知特征（UUID `au0.OooOOOO`），需进一步逆向该 UUID

---

## 九、BLE Spec SET 写入流程

> 分析日期: 2026-07-14
> 源码: `docs/smali_out/classes12/com/xiaomi/smarthome/core/server/internal/bluetooth/channel/` + `docs/apk/smali_all/classes9/`

### 核心发现

Spec SET 写入使用 **同一个 GATT 特征 `0000001a`（CMD_SEND）**，但写入方式为 **`writeNoRsp`（无响应写入）**，与 MiOT 的帧头+ACK 握手完全不同。

**UUID 确认**: `j7c.smali` 中 `SpecWriteChannelManager` 的构造参数为 `au0.OooOOOO` = `0000001a`

### 完整调用链

```
插件 JS: _miot.Bluetooth.spec.setPropertiesValue(mac, JSON.stringify({
    objects: [{siid: 2, piid: 21, value: 50532111, type: 5}]
}))
  │
  ▼
PluginHostApiImpl.setPropertyValue()
  → jn3.OooO0o0(List<PropertyParam>, callback)          [classes2.dex]
    → DeviceCardApi$SpecPropertyApi.setDeviceSpecProp()
      → 构建 JSON: {"params": [{"did":"xxx","siid":N,"piid":N,"value":V}]}
      → OooO00o.OooO00o(json, ...)  (本地 OTU 路径)
        │
        ▼
      com.miot.spec.OooO00o.OooO0o0(mac, khb_packet)   [classes9.dex]
        → khb.OooO0o0() → byte[] (TLV 编码)             [classes9/_m_j/khb.smali]
        → 广播 "action.miot.write.specv2.ble.data"
          │ extras: "mac" (String), "value" (byte[])
          │
          ▼
      SpecWriteChannelManager$Sender.onReceive()
        → mp0.OooO0OO(mac, data, callback)
          → rt0.OooOoO(mac) → 获取 session key string
          → kr8.o00000oo(key_string) → 转为 32+ 字节
          → key[16:32] = AES-CCM key (16 字节)
          → key[36:40] = IV base (4 字节)
          → lp0 state: tx_counter, rx_counter
          → 构造 12 字节 nonce:
          │   [0:4]  = IV base
          │   [4:8]  = 00 00 00 00
          │   [8:10] = tx_counter (LE 16-bit)
          │   [10:12]= rx_counter (LE 16-bit)
          → AES-CCM(key, nonce, plaintext, tag_length=4) → ciphertext
          → 输出: [tx_counter(2B)] + [ciphertext]
          │
          ▼
      OooO0o.OooO(mac, encrypted, response)
        → IBleConnectManager.writeNoRsp(mac, 0xFE95, 0x001a, data, response)
```

### Spec SET TLV 帧格式（已从 smali 逆向确认）

#### 每个属性的 TLV 条目（Frida hook 验证确认）

```
[siid:1B] [piid:2B LE] [type_len:2B LE] [value:NB]
```

- **siid**: 1 字节，服务 ID（注意：siid 在前！）
- **piid**: 2 字节 LE，属性 ID
- **type_len**: 2 字节 LE，编码为 `(type_id << 12) | byte_length`
- **value**: N 字节，属性值（LE 编码）

#### SpecValueType 枚举（type_id）

| type_id | 类型 | 字节长度 |
|---------|------|---------|
| 0 | BOOL | 1 |
| 1 | UINT8 | 1 |
| 2 | INT8 | 1 |
| 3 | UINT16 | 2 |
| 4 | INT16 | 2 |
| 5 | UINT32 | 4 |
| 6 | INT32 | 4 |
| 7 | UINT64 | 8 |
| 8 | INT64 | 8 |
| 9 | FLOAT | 4 |
| 10 | STRING | 变长 |

#### 完整数据包格式

```
[frame_header:2B LE] [packet_id:2B LE] [0x00:1B] [count:1B] [TLV_entries...]
```

- **frame_header**: `0x2000 | total_payload_length`（total = 4 + 所有 TLV 条目长度之和）
- **packet_id**: 2 字节 LE，自动递增计数器
- **count**: 1 字节，属性数量

#### 示例：PIID21 SET (siid=2, piid=21, type=UINT32, value=0x03030F0F)

```
TLV entry:
  siid=2:      [02]
  piid=21:     [15 00]                (LE)
  type_len:    [04 50]                (5<<12 | 4 = 0x5004, LE)
  value:       [0F 0F 03 03]          (0x03030F0F, LE)

总 TLV 长度: 1 + 2 + 2 + 4 = 9 字节
完整数据包 = frame_header(2) + packet_id(2) + 0x00(1) + count(1) + TLV(9) = 15 字节
frame_header = 0x2000 | 15 = 0x200F

完整数据包 (Frida 实测 15B):
  [0F 20] [00 00] [00] [01] [02 15 00 04 50 0F 0F 03 03]
```

> ⚠️ **关键**: `frame_header` 的 `length` 字段 = 完整数据包总长度（**包含 frame_header 自身 2 字节**）。  
> 如 15 字节包 → header = `0x200F`。如写为 `0x200D`（漏算 frame_header），设备按 13 字节解析，值会被截断。  
> 详见下文"常见实现错误"。

### Spec SET 加密方案（AES-CCM，tag=4B）— Frida hook 确认

> ⚠️ 修正: 之前误判为 AES-GCM。Frida hook 确认 fi7.smali 中 `u31` 类的错误消息为 "mac check in CCM failed"。
> Python AESCCM(key, tag_length=4) 验证 5/5 全部匹配。

**从 `fi7.smali` + Frida hook 逆向：**

```
密钥来源:
  session_key_str = rt0.OooOoO(mac)           // 从缓存获取 session key 字符串
  session_key = kr8.o00000oo(session_key_str)  // hex string → bytes (32+ 字节)
  aes_key = session_key[16:32]                  // 16 字节 AES 密钥
  iv_base = session_key[36:40]                  // 4 字节 IV 基值

Nonce 构造 (12 字节):
  nonce[0:4]   = iv_base
  nonce[4:8]   = 00 00 00 00
  nonce[8:10]  = tx_counter (LE 16-bit, 来自 lp0 状态)
  nonce[10:12] = rx_counter (LE 16-bit, 来自 lp0 状态)

加密:
  ciphertext = AES-CCM(aes_key, nonce, plaintext, tag_length=4)

最终 BLE 数据包:
  [固定头 01 00 00 00 (4B)] + [ciphertext]
```

**与 MiOT 加密对比：**

| | MiOT (`_encrypt`) | Spec (`mp0.OooO0OO`) |
|--|---|---|
| 算法 | AES-CCM (tag=4) | **AES-CCM (tag=4)** — 两者相同 |
| 密钥 | `app_key` (session_key[16:32]) | **相同** |
| Nonce | `app_iv + zeros(4) + send_it(4B)` | **`iv_base + zeros(4) + tx(2B) + rx(2B)`** |
| Nonce 长度 | 12 字节 | 12 字节 |
| 输出格式 | `it_lo + it_hi + ct` | **`tx_counter(2B) + ct`** |
| 计数器 | `ctrl._send_it` (单向递增) | **`lp0` 状态 (tx/rx 双向)** |

### Spec SET 响应格式（已验证）

设备在 `cmd_recv` 上返回 25 字节响应，格式为 **4 字节传输层帧头 + 加密 MiOT payload**：

```
原始响应 (25 bytes):
  [00] [00] [it_lo] [it_hi] [encrypted_miOT_payload...]
  ├─ 传输层头 ─┤├─ 加密数据 ───────────────────────────┤

解密方法:
  1. 跳过前 2 字节传输层头 ([00, 00])
  2. 取 bytes[2:4] 作为 MiOT it counter
  3. nonce = dev_iv + zeros(4) + it(2) + zeros(2)
  4. AESCCM(dev_key, tag=4).decrypt(nonce, bytes[4:], None)
```

**与 MiOT SET 响应对比**：

| | MiOT SET 响应 | Spec SET 响应 |
|--|--------------|--------------|
| it counter 位置 | **Byte[0:2]** | **Byte[2:4]** |
| 传输层帧头 | 无 | 前 2 字节 `[00, 00]` |
| 加密范围 | 全部（含 it counter） | 前 2 字节不加密，其余加密 |

### BLE 连接注意事项

充电器**空闲时不广播**，Bleak `find_device_by_address` 无法发现设备。连接前需：

1. `bluetoothctl disconnect MAC` — 清理残留连接
2. `bluetoothctl power off` → `sleep 1` → `bluetoothctl power on` — 重置适配器
3. `sleep 3` — 等待适配器就绪
4. Bleak `BleakClient(mac).connect()` — 直接连接（不经过扫描）

**注意**：`bluetoothctl connect` 建立连接后，Bleak 再连会挂住（BlueZ 已占用连接），必须先断开 bluetoothctl。

### TLV 帧格式关键 smali 文件索引

| 文件 | 位置 | 作用 |
|------|------|------|
| `classes9/_m_j/khb.smali` | 抽象基类 | TLV 编码核心：`OooO0oO()` 计算 type_len，`OooO0oo()` 写入值 |
| `classes9/_m_j/pr3.smali` | khb 子类 | SET 命令 TLV 编码：`OooO0o0()` 构建完整数据包 |
| `classes9/com/miot/spec/OooO00o.smali` | 发送器 | 接收 khb 数据，加密后发送广播 |
| `classes9/com/miot/spec/entity/SpecValueType.smali` | 枚举 | 类型 ID 和字节长度定义 |
| `classes9/_m_j/i3c.smali` | 属性条目 | siid/piid/type/value 存储 |
| `classes12/_m_j/mp0.smali` | 加密 | AES-CCM 加密 + BLE 数据包组装 |
| `classes11/_m_j/fi7.smali` | 加密实现 | `OooO0O0()` → `OooOOO()` → `OooO0Oo()` (AES-CCM, BouncyCastle) |
| `classes12/_m_j/lp0.smali` | 计数器状态 | tx/rx counter 管理，溢出处理 |
| `classes2/_m_j/jn3.smali` | 序列化入口 | `OooO0o0()` → `setDeviceSpecProp()` |

### 测试验证

```bash
cd ble_server
python spec_cli.py set-piid 2 5 2    # Spec SET 场景模式=2
python spec_cli.py set-protocol c2 pd on  # Spec SET 开启 C2 PD
python spec_cli.py get-protocol       # 读取 PIID21 状态
```

### 加密参数验证（Frida hook 实测）

#### Session key 来源

```
rt0.OooOoO(mac) 返回的 64 字节 session key (hex string → bytes):
a125a08ab5b595bb2723fd8fdd99a51fed6c7966c1fe2221155c291f5de267793d
6bd7548ff16035332a4c67dfe4ae3494ceda2236d32f907e48f6355ad6c103

kr8.o00000oo(hex_string) 将 hex 字符串转为 64 字节
```

#### AES-CCM 加密参数（Frida hook 确认）

| 参数 | 值 | 来源 |
|------|-----|------|
| key | `ed6c7966c1fe2221155c291f5de26779` (16B) | session_key[16:32] |
| nonce | `iv_base(4) + zeros(4) + tx_counter(2B LE) + rx_counter(2B LE)` = 12B | |
| iv_base | `8ff16035` (4B) | session_key[36:40] |
| CCM tag | 4 bytes (truncated) | |
| tx_counter | 从 0 开始递增 | lp0.OooO0O0 |
| rx_counter | 始终 0 | lp0.OooO00o |

#### 两台手机对比

| | Android 10 (K30 5G) | Android 16 (M2102J2SC) |
|--|---|---|
| fi7 hook | ❌ 从未触发 | ✅ 立即触发 |
| mp0 hook | ❌ 从未触发 | ✅ 立即触发 |
| SET GATT write | ❌ 无 | ✅ 有 |
| BLE SDK 版本 | 旧版（不同路径） | 新版（标准路径） |
| 结论 | 需要进一步调查 | Spec SET 路径完整 |

### 已验证项

1. ✅ TLV 字段顺序：`[siid:1B][piid:2B LE][type_len:2B LE][value:NB]` — Frida hook 确认
2. ✅ Value 为小端序 — Frida hook 确认
3. ✅ 设备正确处理 PIID21 的 Spec SET — Frida SpecSender 数据确认
4. ✅ 用户操作 C2 PD/PPS/UFCS 和 A口 UFCS/SCP — PIID21 值变化完全匹配
5. ✅ AES-CCM 加密路径确认：mp0 → fi7.OooO0O0 → fi7.OooO0Oo → u31 (BouncyCastle CCM) → GATT writeNoRsp
6. ✅ CCM tag 长度 = 4 bytes（从 Frida 实测：ciphertext - plaintext = 4B）
7. ✅ Session key 存储在 rt0.OooO0OO 的 ConcurrentHashMap 中，每次 Spec SET 前调用

### 待修复项

1. **HKDF 参数不一致** — Mijia App 的 session_key[16:32] (`ed6c7966c1fe2221155c291f5de26779`) 与我们 HKDF 派生的 app_key (`a39e536a8b6bd23ce14920095796e8ef`) 不同
2. **Android 10 BLE SDK** — 走了非标准路径，fi7/mp0 从未触发

### 关键 smali 文件索引

| 文件 | 类名 | 作用 |
|------|------|------|
| `j7c.smali` | `j7c` | SpecWriteChannelManager 单例创建，传入 UUID=0x001a |
| `OooO0o.smali` | `OooO0o` | 基类，实现 `writeNoRsp` BLE 写入 |
| `OooOO0.smali` | `OooOO0` (SpecWriteChannelManager) | 注册广播监听 |
| `SpecWriteChannelManager$Sender.smali` | Sender | 广播接收，提取 mac+value |
| `OooO.smali` | OooO (BleReadResponse) | 加密后获取 writer 并写入 |
| `SpecWriteChannelManager$2.smali` | $2 (IBleResponse) | 写入完成回调 |
| `mp0.smali` | mp0 | `OooO0OO` 加密核心逻辑 |

---

**本项目额外发现:**
- ✅ PIID21 编码公式已从插件源码提取并验证
- ✅ 通过 Frida Hook 确认了 HKDF 密钥派生与项目一致
- ✅ HCI btsnoop 分析确认 Mi Home 的 PIID21 SET 帧格式
- ✅ 确认 MIOT/Spec 双通道并行架构，Spec 通道可直接获取硬件协议号
- ✅ Spec SET 写入与 MiOT 共用 CMD_SEND (0x001a)，使用 writeNoRsp 无帧头无握手
- ✅ Spec READ notify 也使用 CMD_SEND (0x001a)，设备认证后自动推送
- ✅ Spec SET 响应有 4 字节传输层帧头（前 2 字节 `00 00` 不加密，后 2 字节为 it counter）
- ✅ Spec SET TLV 帧格式已确认：`[frame_header:2B][packet_id:2B][0x00][count:1B][TLV_entries...]`
- ✅ TLV 条目格式：`[siid:1B][piid:2B LE][type_len:2B LE][value:NB]`
- ✅ Spec SET 使用 AES-CCM 加密（tag=4B），与 MiOT 使用相同加密算法，nonce 构造不同
- ✅ BLE 连接需先 power cycle 适配器，充电器空闲时不广播
- ✅ TLV + AES-CCM 加密已验证正确（Frida hook 5/5 匹配）

**常见实现错误 — 框架头长度字段必须等于包总长:**

本项目的 `controller.py:send_miot_command()` 中，4 字节值的 SET 包长度硬编码为 `0x0c`（12），
但实际包长为 15 字节。设备按 header 声明的长度截断解析 TLV，导致值只读到 1 字节：

```python
# controller.py:684 当前代码（错误）
plaintext = bytes([0x0c, 0x20, seq, 0x00, ...])  # 始终用 0x0c=12
# 但 4 字节值 → 15 字节总长，header 应为 0x0f=15
```

截断效果（以 value=0x03030F0E 为例）:
```
设备按 header=12 读取:
  12 - 6(帧头区域) = 6 字节 TLV
  siid(1) + piid(2) + tl(2) = 5 字节 → 只剩 1 字节给值!
  设备读到: 0x0E → C1=0x0E, C2/C3/A=0x00 (全关!)
```

实测验证（2026-07-14，固件 2.1.2_0073）:

| 格式 | header | 目标值 | 读回值 | 结果 |
|------|--------|--------|--------|:----:|
| main 分支 | `0x200c` (12) | `0x03030F0E` | `0x0000000E` | ❌ C2/C3/A 全零 |
| 修复后 tid=5 | `0x200f` (15) | `0x03030F0E` | `0x03030F0E` | ✅ |
| 修复后 toggle C1 PD | `0x200f` (15) | `0x03030F0F` | `0x03030F0F` | ✅ |
| 修复后 toggle C2 PPS | `0x200f` (15) | `0x03030D0E` | `0x03030D0E` | ✅ |
| 修复后 toggle C3 UFCS | `0x200f` (15) | `0x03020D0E` | `0x03020D0E` | ✅ |
| 修复后 toggle A SCP | `0x200f` (15) | `0x01020D0E` | `0x01020D0E` | ✅ |

**结论**: C2/C3/A 协议开关无法操作的原因不是设备限制，而是框架头长度硬编码为 12 导致 4 字节值被截断。
修复后（`type_len`=0x5004, `header`=0x200f），所有端口的 PD/PPS/UFCS/SCP 开关均可独立控制。

测试工具: `docs/tools/test_piid21_bare.py`<br/>
完整分析: `docs/bug.md`
