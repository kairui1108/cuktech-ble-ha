# BLE Spec 协议逆向分析报告

> 逆向日期: 2026-07-12
> 分支: feature/ble-spec-mode
> 基于: 米家 App v1.1.10 SDK classes9/12 + jadx + baksmali 3.0.9

---

## 一、协议栈层次

```
React Native Plugin (setPropertiesValue)
    ↓
m78.OooO00o(vae)          ← 核心编码方法 (classes12.dex, 5800行 smali)
    ↓
sae (FlatBuffers Builder)  ← 消息序列化 (classes9.dex)
eee.OooO00o()             ← FlatBuffers 表组合 (classes9.dex)
    ↓
加密: AES-CCM (同 MiOT 会话密钥)
    ↓
BLE 通道: 0000001c (device_info), 帧协议: header → RCV_RDY → encrypted → RCV_OK
响应: 0000001a (cmd_send) 或 cmd_recv
```

---

## 二、关键特征通道 (GATT)

| UUID | Handle | 属性 | 用途 |
|------|--------|------|------|
| `00000005` | 0x0009 | read, notify | BLE Spec 通知 (设备状态推送) |
| `0000001a` | 0x0019 | write, notify | 命令发送 (同 MiOT) |
| `0000001b` | 0x001c | notify | 命令接收 (同 MiOT) |
| `0000001c` | 0x001f | write-without-response, notify | **BLE Spec 命令通道** |
| `00000019` | 0x0010 | write, notify | 认证数据 |

---

## 三、消息序列化: FlatBuffers

### 3.1 sae 类 (FlatBuffers 构建器)

```java
// 核心方法
OooOO0(ByteBuffer)       // 初始化构建器，设置 LE byte order
OooOOO(int fieldCount)   // startObject - 开始构建 N 字段的表
OooO0o0(field, byte)     // addByte - 写入 byte 字段
OooO0oo(field, long)     // addLong - 写入 int64 字段
OooOOOO(field, int)      // addOffset - 写入 table/vector 偏移引用
OooO0O0(CharSequence)    // createString - 创建 UTF-8 字符串，返回偏移
OooOOO0()                // endObject - 完成表构建，返回表偏移
OooOOOo(rootOffset)      // finish - 完成根缓冲区
OooOOo()                 // sizedByteArray - 获取最终字节
```

### 3.2 eee 类 (消息表组合器)

`eee.OooO00o(sae, opcode, siid_off, piid_off, flag, v1..v8, did)` 构建 FlatBuffers 表:

```
startObject(13)           // 13 个字段
  field 0: byte   (opcode)
  field 1: int    (siid - 偏移引用)
  field 2: int    (piid - 偏移引用)
  field 3: byte   (flag)
  field 4-7: int  (value parts)
  field 8: long   (did - 设备ID)
  field 9-12: int (additional params)
endObject()
```

**警告**: `addInt(field, value)` 在这里写的是**缓冲区偏移量**（嵌套 FlatBuffers 表引用），不是简单 int32 值！

### 3.3 消息结构 (推测)

```
FlatBuffers Root Table:
┌─────────────────────────────────┐
│ root_table_offset (4B LE)       │
├─────────────────────────────────┤
│ [vtable]                        │
│   vtable_size (2B)              │
│   object_size (2B)              │
│   field_0_offset (2B)  ← byte  │
│   field_1_offset (2B)  ← int   │
│   ...                           │
├─────────────────────────────────┤
│ [data area]                     │
│   String "opcode" data          │
│   String "siid" data            │
│   String "piid" data            │
│   Int values                    │
│   Long did value                │
└─────────────────────────────────┘
```

---

## 四、帧协议

### 4.1 发送 (写入 0000001c)

```
1. 写入 header: [00 00 00 00 01 00]  → 0000001c
2. 等待 RCV_RDY: [00 00 01 01]       ← 0000001a notify
3. 写入 frame:  [01 00] + encrypted   → 0000001c
4. 等待 RCV_OK:  [00 00 01 00]        ← 0000001a notify
```

加密: AES-CCM，使用 MiOT 登录派生的会话密钥。

### 4.2 接收 (来自 cmd_recv)

```
响应帧头: 0f 20 (vs MiOT 的 0c 20)
格式: [0f 20 seq flags type siid piid ... data ...]
```

### 4.3 命令帧头对比

| 协议 | SET 请求 | SET 响应 |
|------|---------|---------|
| MiOT | `0c 20` | `0c 20` |
| BLE Spec | `0c 20` (复用) | `0f 20` |

---

## 五、数据类结构

### 5.1 vae (请求参数)

```java
class vae {
    yge OooO00o;           // 设备统计信息
    ArrayList OooO0O0;     // 属性列表 (i3c 对象)
    ArrayList OooO;        // 额外列表
    long OooO0OO;          // 时间戳1
    long OooO0Oo;          // 时间戳2
    boolean OooO0o0;       // 标志
    byte OooO0oO;          // 字节标志
    String OooO0oo;        // MAC/ID
    boolean OooOO0;        // 其他标志
}
```

### 5.2 i3c (属性值)

```java
class i3c {
    int OooO00o;              // siid
    int OooO0O0;              // piid
    Object OooO0OO;           // value
    SpecValueType OooO0Oo;    // 值类型 (UINT8/UINT16/UINT32/BOOL)
    int OooO0o0;              // flags
}
```

### 5.3 sae (FlatBuffers 构建器)

```java
class sae {
    ByteBuffer OooO00o;       // 输出缓冲区 (LE)
    int OooO0O0;              // 写位置
    int OooO0OO;              // 对齐大小
    int[] OooO0Oo;            // 字段位置表
    boolean OooO0o;           // object 构建中
    int OooO0o0;              // 字段数
    int[] OooO;               // vtable 去重缓存
}
```

### 5.4 dee (响应值)

```java
class dee {
    int OooO00o;              // 状态码
    byte[] OooO0O0;           // 响应数据
}
```

---

## 六、协议操作码

基于测试：

| Opcode | 行为 |
|--------|------|
| 0 | 帧协议完成，无响应 |
| 1 | 设备拒绝 (no RCV_RDY) |
| 2 | 设备拒绝 |
| 3 | 帧协议完成 (与0相同) |
| 5 | 设备拒绝 |

推测 opcode=0 可能是 `getProperties`，opcode=3 可能是 `setProperties`。

---

## 七、解密示例

**请求 (未加密的 probuf-like 负载)**:
```
0b 00 00 08 02 10 15 25 0f 08 00 00
│  │  │  │  │  │  │  │  └──────────┘
│  │  │  │  │  │  │  │     value: fixed32 0x080F (C2 PD off)
│  │  │  │  │  │  │  field 4: fixed32
│  │  │  │  │  │  field 2: varint 21 (piid)
│  │  │  │  │  field 1: varint 2 (siid)
│  │  │  │  field 0: varint 0 (opcode)
│  │  │  varint length prefix: 11 bytes
```

**响应 (解密后)**:
```
0f 20 24 00 04 01 02 02 00 04 50 01 0b 0a c9
│─────│ │  │  │  │  │  │  │  │  └──────────┘
  帧头  seq │  │  │  │  │  │  50  value data
            type │  │  │  │  piid? (336?)
                 siid=2? len=4
                     SET result (0x04=success?)
```

---

## 八、待完成工作

1. [ ] 实现完整的 FlatBuffers 编码 (替代当前的 protobuf-like 格式)
2. [ ] 确定正确的 opcode 值 (0=get, 3=set?)
3. [ ] 正确构建字符串引用 (siid/piid 需要是 FlatBuffers string offsets)
4. [ ] 验证 value 编码 (fixed32 vs varint)
5. [ ] 集成到 BLE server 主循环
6. [ ] 添加 HA integration 支持

详见本项目 issue tracker。
