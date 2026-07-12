# BLE Spec 协议逆向分析报告

## 一、协议栈层次

```
React Native Plugin (JS)
  └─ _miot.Bluetooth.spec.setPropertiesValue(mac, json)
       │  json = {objects: [{siid, piid, value}]}
       └─ Xiaomi Native SDK (Java/C++)
            ├─ m78.setPropertiesValue → 构建 FlatBuffers 消息
            ├─ bhe → BLE 管道管理
            ├─ uhb → 协议版本检查 + 帧封装
            ├─ xp  → BLE GATT 写入
            └─ rv0 → BLE 连接管理
```

## 二、FlatBuffers 消息格式

### 编码器: sae 类

```
OooOOO(13)    = startObject(13 fields)
OooO0o0(f, b) = addByte(field, value)     ← 跳过 0 值
OooO0o(f, i)  = addInt(field, value)      ← 跳过 0 值
OooO0oo(f, L) = addLong(field, value)     ← 跳过 0 值
OooOOO0()     = endObject()               ← 返回 root_offset
OooOOOo(sz)   = finish(sz)
OooOOo()      = sizedByteArray()          ← 返回完整字节数组

内存布局:
  [table_data(LE)] [vtable_offset:4B] [vtable]
  vtable = [size:2B][table_size:2B][field_offsets:2B*N]
  field_offset = 0 表示字段不存在
```

### eee.OooO00o 消息结构 (SET 属性)

| Field | Type   | Wire | 含义 |
|-------|--------|------|------|
| 0     | byte   | inline | opcode (4 = SET) |
| 1     | int    | inline | siid (2 = charger) |
| 2     | int    | inline | piid (21 = protocol_extend) |
| 3     | byte   | inline | flag (Build.VERSION.SDK_INT) |
| 4     | int    | inline | value (属性值) |
| 5-7   | int    | inline | 扩展值 (通常为0，跳过) |
| 8     | long   | inline | did (设备ID，通常为0) |
| 9-12  | int    | inline | 额外参数 (通常为0，跳过) |

## 三、帧协议 (uhb 类)

### 设置帧 (OooO0o0)
```
[0x05 0x20] [opcode:2B LE] [0xF0]
= 5 bytes
```

- `0x2005` → 帧头: opcode=0x005, version=2
- `0xF0` → 设置帧类型标记

### 响应帧
```
[0x0F 0x20] [seq] [data...]
```
- `0x0F 0x20` → BLE Spec 响应头 (opcode=0xF20, version=0)

## 四、GATT 通道

| UUID 后缀 | GATT Handle | 方向 | 用途 |
|-----------|-------------|------|------|
| 00000019 | ? | Write | 认证数据 |
| 0000001a | 24 | Notify | CMD_SEND 通知 |
| 0000001b | 26 | Write/Notify | CMD_RECV (MiOT) |
| 0000001c | 30 | Write/Notify | BLE Spec 设备信息通道 |
| 0000001d | 32 | Read | 固件版本 |
| 00000005 | 9 | Read/Notify | BLE Spec 通知通道 |

## 五、端口数据格式差异

### MiOT 模式 (当前使用)
```
port_data = [in_use:1B, code:1B, current_raw:1B, voltage_raw:1B]
protocol = 需要从 code + voltage 启发式估算
```

### BLE Spec 模式 (米家 App)
```
port_data = 32-bit integer:
  bits 31-24: voltage_raw (V×10)
  bits 23-16: current_raw (A×10)
  bits 15-8:  protocol_number (1-10, 直接查表)
  bits 7-0:   port_status (0=idle)
```

### 协议号映射 (getC1C2ProtocolStr)

| 序号 | 协议名称 |
|------|---------|
| 0 | idle |
| 1 | 5V |
| 2 | 5V |
| 3 | QC |
| 4 | AFC |
| 5 | FCP |
| 6 | SCP |
| 7 | PD |
| 8 | PPS |
| 9 | PPS |
| 10 | UFCS |

## 六、PIID 完整映射

| PIID | 名称 | 类型 | 说明 |
|------|------|------|------|
| 1 | port_info_c1 | 32-bit | C1 端口数据 |
| 2 | port_info_c2 | 32-bit | C2 端口数据 |
| 3 | port_info_c3 | 32-bit | C3 端口数据 |
| 4 | port_info_a | 32-bit | USB-A 端口数据 |
| 5 | scene_mode | 8-bit | 场景模式 1-4 |
| 6 | screen_save_time | 8-bit | 息屏时间 0-5 |
| 7 | protocol_ctrl | 8-bit | 协议控制 bit flags |
| 13 | screen_language | 8-bit | 屏幕语言 0/1 |
| 15 | switch_usba_trickle | 8-bit | USB-A 常通电 0/1 |
| 16 | port_ctrl | 8-bit | 端口开关 bits |
| 17 | c1_c2_protocol | 32-bit | C1/C2 协议信息 |
| 18 | c3_a_protocol | 32-bit | C3/A 协议信息 |
| 19 | idle_screen_off | 8-bit | 空闲息屏 0/1 |
| 20 | screen_direction_lock | 8-bit | 屏幕方向锁 0/1 |
| 21 | protocol_ctl_extend | 32-bit | 协议扩展开关 |

### PIID 21 位定义 (protocol_ctl_extend)

```
c1_flags (bits 0-7):  bit0=PD, bit1=PPS, bit2=UFCS, bit3=保留(固定1)
c2_flags (bits 8-15): bit8=PD, bit9=PPS, bit10=UFCS, bit11=保留(固定1)
c3_flags (bits 16-23): bit16=UFCS, bit17=SCP
a_flags (bits 24-31): bit24=UFCS, bit25=SCP

编码: value = (a_flags << 24) | (c3_flags << 16) | (c2_flags << 8) | c1_flags
示例: 0x090F = C1:pd+pps+ufcs, C2:pd only
```

## 七、已实现功能

| 功能 | 状态 | 说明 |
|------|------|------|
| BLE 连接 + MiOT 认证 | ✅ | 完整实现 |
| 端口协议检测 (启发式) | ✅ | 结果与米家 App 一致 |
| PIID SET/GET (1-byte) | ✅ | 所有 PIID |
| PIID SET (4-byte MiOT) | ✅ | 发送被设备认可但高字节未执行 |
| C1 协议开关 (PD/PPS/UFCS) | ✅ | 低字节写入，硬件执行 ✅ |
| C2/C3/A 协议开关 | ❌ | 需 4-byte 写入，MiOT 不支持 |
| FlatBuffers 编码器 | ✅ | 完整实现 sae 兼容编码 |
| BLE Spec 通道探测 | ✅ | 00000005/0000001c/0000001a |
| BLE Spec 消息收发 | ⚠️ | 响应收到但未正确执行 SET |

## 八、已知限制

1. **PIID21 多字节写入**: MiOT 不支持完整的 32-bit PIID21 写入。C2/C3/A 协议开关必须通过米家 App 设置
2. **端口数据格式**: MiOT 模式使用 [in_use][code][cur][volt] 格式，非 BLE Spec 的 32-bit 格式
3. **BLE Spec 实现**: 完整的 BLE Spec 协议需要 Xiaomi Native SDK，未能从 APK 中完全提取

## 九、待完成工作 (BLE Spec 完整支持)

1. [ ] 逆向 Xiaomi Native SDK 中 `setPropertiesValue` 的 BLE write 实现
2. [ ] 确认 BLE Spec 数据帧的正确 opcode
3. [ ] 实现 BLE Spec 帧协议的完整握手
4. [ ] 迁移端口数据解析到 32-bit 格式
5. [ ] 实现 PIID21 完整 32-bit 读写
