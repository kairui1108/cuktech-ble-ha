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

**本项目额外发现:**
- ✅ PIID21 编码公式已从插件源码提取并验证
- ✅ 通过 Frida Hook 确认了 HKDF 密钥派生与项目一致
- ✅ HCI btsnoop 分析确认 Mi Home 的 PIID21 SET 帧格式
- ⚠️ C1 协议开关已可用，C2/C3/A 未被设备执行
