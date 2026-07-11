# 米家 App 酷态科充电器插件逆向分析报告

> 分析日期: 2026-07-12
> 插件版本: 1.1.10 (package: com.cuktech.charger)
> 设备型号: njcuk.fitting.ad1204
> Bundle 类型: indexed-ram-bundle (React Native)

---

## 一、PIID 完整映射表

`siid = 2` (充电器服务)

| PIID | 属性名 | 类型 | 说明 |
|------|--------|------|------|
| 1 | `port_info_c1` | 32-bit | C1 端口数据 (电压/电流/协议/状态) |
| 2 | `port_info_c2` | 32-bit | C2 端口数据 |
| 3 | `port_info_c3` | 32-bit | C3 端口数据 |
| 4 | `port_info_a` | 32-bit | USB-A 端口数据 |
| 5 | `scene_mode` | int | 场景模式 (1-4) |
| 6 | `screen_save_time` | int | 屏幕保护时间 (0-1440) |
| 7 | `protocol_ctrl` | int | 协议控制标志 (SCP/MiPPS/UFCS) |
| 8 | `delay_down_setting` | - | 延时关闭设置 |
| 9 | `delay_time_c1` | int | C1 延时关闭时间 (0-1440) |
| 10 | `delay_time_c2` | int | C2 延时关闭时间 |
| 11 | `delay_time_c3` | int | C3 延时关闭时间 |
| 12 | `delay_time_a` | int | USB-A 延时关闭时间 |
| 13 | `screen_language` | int | 屏幕语言 |
| 14 | `enter` | int | 进入/按键 |
| 15 | `usb_a_always_on` | int | USB-A 常通电 (0/1) |
| 16 | `port_ctrl` | 8-bit | 端口开关控制 |
| 17 | `c1_c2_protocol` | 32-bit | C1/C2 协议信息 |
| 18 | `c3_a_protocol` | 32-bit | C3/A 协议信息 |
| 19 | `screenoff_while_idle` | int | 待机灭屏 (0/1) |
| 20 | `screen_direction_lock` | int | 屏幕方向锁定 (0/1) |
| 21 | `protocol_ctl_extend` | 32-bit | 协议扩展控制 |

---

## 二、协议号映射表（核心）

米家 App 显示充电协议的唯一依据：

```
┌────────┬──────────┬────────────────────────────┐
│ 协议号  │ 显示名称  │ 说明                       │
├────────┼──────────┼────────────────────────────┤
│   0    │ (idle)   │ 空闲/未充电                 │
│   1    │   5V     │ 普通 5V 充电                │
│   2    │   5V     │ 普通 5V 充电                │
│   3    │   QC     │ Quick Charge                │
│   4    │   AFC    │ Samsung Adaptive Fast Charge│
│   5    │   FCP    │ Huawei Fast Charge Protocol │
│   6    │   SCP    │ Huawei Super Charge Protocol│
│   7    │   PD     │ USB Power Delivery (Fixed)  │
│   8    │   PPS    │ PD Programmable Power Supply│
│   9    │   PPS    │ PD PPS (同8)                │
│  10    │   UFCS   │ Universal Fast Charging Spec│
└────────┴──────────┴────────────────────────────┘
```

**关键发现**：米家 App 不做任何协议检测/猜测。协议号由硬件直接上报，
App 只做查表映射。我们在 MiOT 模式下无法获取硬件协议号，只能
通过电压 + code 字节进行启发式估算。

---

## 三、数据解析函数详解

### 3.1 端口数据解析 (`parsePortInfo`)

```
PIID: 1-4 (每个端口独立)

32-bit 值解析:
  Bits 31-24: voltage_raw  (V×10)    示例: 121 = 12.1V
  Bits 23-16: current_raw  (A×10)    示例: 3   = 0.3A
  Bits 15-8:  protocol     协议号     示例: 7   = PD
  Bits 7-0:   port_status   端口状态   示例: 0   = idle

示例:
  值 = 0x79070000
  → 电压: 0x79=121→12.1V
  → 电流: 0x07=7→0.7A
  → 协议: 0x00 (idle)
  → 状态: 0x00

C3 端口状态特殊值:
  0x11 = C3+A 双口模式激活
```

### 3.2 C1/C2 协议信息 (`parseC1C2ProtocolInfo`)

```
PIID: 17

32-bit 值解析:
  Bits 31-24: c1_protocol         C1 协议号 (1-10)
  Bits 23-16: c1_power             C1 功率
  Bits 15-8:  c2_protocol         C2 协议号 (1-10)
  Bits 7-0:   c2_power             C2 功率
```

### 3.3 C3/A 协议信息 (`parseC3AProtocolInfo`)

```
PIID: 18

32-bit 值解析:
  Bits 31-24: c3_protocol         C3 协议号 (1-10)
  Bits 23-16: c3_power             C3 功率
  Bits 15-8:  a_protocol          USB-A 协议号 (1-10)
  Bits 7-0:   a_power             USB-A 功率
```

### 3.4 协议控制标志 (`parseProtocolInfo`)

```
PIID: 7

8-bit 值解析:
  Bit 7: SCP    - Huawei Super Charge Protocol
  Bit 6: MiPPS  - 小米私有 PPS 协议
  Bit 5: UFCS   - 融合快充
```

---

## 四、协议开关控制详解

### 4.1 端口开关 (PIID 16 `port_ctrl`)

控制每个端口的电源输出开关：

```
8-bit 值:
  Bit 7: C1 开关     (1=开启, 0=关闭)
  Bit 6: C2 开关
  Bit 5: C3 开关
  Bit 4: USB-A 开关
  Bits 3-0: 保留
  
示例:
  0b11110000 = 4个端口全开
  0b01110000 = 关闭 C1
  0b11100000 = 关闭 USB-A
```

### 4.2 协议开关 (PIID 21 `protocol_ctl_extend`)

精细控制每个端口支持哪些充电协议。32位值按字节划分给4个端口：

```
格式: [A_flags << 24] | [C3_flags << 16] | [C2_flags << 8] | [C1_flags]

=== C1 Flags (bit 7-0) ===
  Bit 0: PD    - USB Power Delivery
  Bit 1: PPS   - Programmable Power Supply
  Bit 2: UFCS  - 融合快充
  Bit 3: (保留，固定为1)
  Bits 4-7: 保留

=== C2 Flags (bit 15-8) ===
  (同 C1 结构)

=== C3 Flags (bit 23-16) ===
  Bit 0: UFCS  - 融合快充
  Bit 1: SCP   - Super Charge Protocol
  Bits 2-7: 保留

=== USB-A Flags (bit 31-24) ===
  (同 C3 结构)

编码公式:
  c1Flags = 0x08 | (ufcs?0x04:0) | (pps?0x02:0) | (pd?0x01:0)
  c2Flags = 0x08 | (ufcs?0x04:0) | (pps?0x02:0) | (pd?0x01:0)
  c3Flags = (scp?0x02:0) | (ufcs?0x01:0)
  aFlags  = (scp?0x02:0) | (ufcs?0x01:0)
  value   = aFlags<<24 | c3Flags<<16 | c2Flags<<8 | c1Flags
```

**操作示例**：

| 操作 | c1Flags | 说明 |
|------|---------|------|
| 全部开启 | 0x0F | PD + PPS + UFCS + reserved |
| 关闭 PD | 0x0E | PPS + UFCS + reserved |
| 关闭 PPS | 0x0D | PD + UFCS + reserved |
| 关闭 PD+PPS | 0x0C | 仅 UFCS + reserved |
| 全部关闭 | 0x08 | 仅保留位 |

### 4.3 协议模式选择 (PIID 7 `protocol_ctrl`)

手动指定充电协议模式的设置选项：

```
 0: "默认"   - 自动选择
 1: "QC2.0"  - 强制 QC2.0
 2: "QC3.0"  - 强制 QC3.0
 3: "QC4.0"  - 强制 QC4.0
 4: "FCP"    - 强制 FCP (Huawei)
 5: "SCP"    - 强制 SCP (Huawei)
 6: "AFC"    - 强制 AFC (Samsung)
 7: "PE"     - 强制 PE (MediaTek)
 8: "PD"     - 强制 PD
 9: "SFCP"   - 强制 SFCP
10: "UFCS"   - 强制 UFCS
```

---

## 五、端口数据格式差异 (MiOT vs BLE Spec)

### MiOT 模式 (我们当前使用)

```
端口数据载荷: [in_use:1B][code:1B][current_raw:1B][voltage_raw:1B]
  in_use:     是否使用中 (0/1)
  code:       内部状态码 (非米家协议号!)
  current_raw: 电流 (×10 mA)
  voltage_raw: 电压 (×10 mV)

实测 code 值:
  C1/C2 PD:    0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x30
  C1/C2 PPS:   0x08, 0x0A
  C3:          0x60, 0x80
  USB-A:       0x60
  QC 明确:     0x70
```

### BLE Spec 模式 (米家 App 使用)

```
端口数据值: 32-bit 整数
  Bits 31-24: voltage_raw  (V×10)
  Bits 23-16: current_raw  (A×10)
  Bits 15-8:  protocol     协议号 (1-10)
  Bits 7-0:   port_status   端口状态
```

**差异关键**：MiOT 模式下的 `code` 字节不是米家协议号，
而是设备内部状态码。BLE Spec 模式下 protocol 字段直接提供
正确的协议号（1-10），因此米家 App 不需要做任何协议检测。

---

## 六、UI 功能汇总

### 6.1 主界面显示
- 4 个端口实时状态（电压/电流/功率/协议名称）
- 端口开关状态
- 设备连接状态
- 屏幕模式

### 6.2 设置功能
- 端口独立开关 (PIID 16)
- 延时关闭 (PIID 9-12)
- USB-A 常通电 (PIID 15)
- 屏幕保护时间 (PIID 6)
- 屏幕方向锁定 (PIID 20)
- 待机灭屏 (PIID 19)
- 协议开关 (PIID 21) - 关闭特定协议
- 协议模式选择 (PIID 7) - 手动指定协议

### 6.3 协议开关 UI (DischargeProtocol 页面)
- C1: PD / PPS / UFCS 三个开关
- C2: PD / PPS / UFCS 三个开关
- C3: SCP / UFCS 两个开关
- USB-A: SCP / UFCS 两个开关

---

## 七、与当前实现的对比

| 功能 | 米家 App | 本项目 |
|------|----------|--------|
| 协议识别 | 硬件协议号直查 | 启发式估算 |
| 端口开关 | PIID 16 ✅ | 未实现 |
| 协议开关 | PIID 21 ✅ | 未实现 |
| 协议模式 | PIID 7 ✅ | 未实现 |
| USB-A常通 | PIID 15 ✅ | 未实现 |
| 延时关闭 | PIID 9-12 | 未实现 |
| 协议名称 | 5V/QC/PD/PPS... ✅ | 已对齐 ✅ |

---

## 八、TODO / 后续计划

- [ ] 实现 PIID 16 端口开关控制（HA 集成 switch 实体）
- [ ] 实现 PIID 21 协议开关控制（HA 集成 switch 实体）
- [ ] 实现 PIID 7 协议模式选择（HA 集成 select 实体）
- [ ] 实现 PIID 9-12 延时关闭（HA 集成 number 实体）
- [ ] 实现 PIID 15 USB-A 常通电（HA 集成 switch 实体）
- [ ] 探索 MiOT 模式下获取硬件协议号的方法

---

## 九、技术细节

### 9.1 插件加载

```
plugin/install/rn/1028581/1896060/android/
├── main.bundle     (925KB, indexed-ram-bundle)
├── project.json    (package: com.cuktech.charger)
└── drawable-{mdpi,xhdpi,xxhdpi}/
```

### 9.2 通信协议

插件使用双协议栈：
1. **MiOT** (`deviceReceivedMessages`): 旧版协议，按 PIID 逐属性收发
2. **BLE Spec** (`BLESpecNotifyActionEvent`): 新版协议，批量属性推送

两种协议共用相同的 PIID 映射，但数据编码格式不同。

### 9.3 关键 State 变量

```javascript
state = {
    // 端口数据 (0:C1, 1:C2, 2:C3, 3:A)
    portInfoArr: [{Voltage, Current, Power, Protocol, Status}],

    // 协议开关
    switch_c1_protocol_pd, switch_c1_protocol_pps, switch_c1_protocol_ufcs,
    switch_c2_protocol_pd, switch_c2_protocol_pps, switch_c2_protocol_ufcs,
    switch_c3_protocol_scp, switch_c3_protocol_ufcs,
    switch_a_protocol_scp, switch_a_protocol_ufcs,

    // 端口开关
    switch_port_c1, switch_port_c2, switch_port_c3, switch_port_usb_a,

    // USB-A 小电流模式
    switch_usba_trickle,

    // 协议扩展值
    protocol_extend,   // PIID 21 原始值
}
```
