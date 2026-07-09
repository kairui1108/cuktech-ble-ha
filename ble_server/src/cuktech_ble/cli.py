
"""CUKTECH BLE CLI - Command line interface for BLE operations."""
import argparse
import asyncio
import sys

from .protocol import (
    DEVICE_MAC, DEVICE_TOKEN, SIID_CHARGER, PIID_NAMES, PIID_DISPLAY,
    PORT_BITS, TIMER_PORTS, SET_COMMANDS,
    mac_str_to_bytes, require_runtime_dependencies,
)
from .controller import CuktechBLEController, CHAR_CMD_RECV

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    BleakScanner = None
    BleakClient = None


# ============================================================
# 扫描功能
# ============================================================
async def scan_devices(timeout=10):
    """扫描附近的 BLE 设备，查找酷态科充电器。"""
    require_runtime_dependencies()
    print(f"[*] 扫描 BLE 设备 ({timeout}秒)...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    found = False
    print(f"\n找到 {len(devices)} 个 BLE 设备:\n")
    # devices is dict: {address: (BLEDevice, AdvertisementData)}
    sorted_devs = sorted(devices.values(), key=lambda x: x[1].rssi or -999, reverse=True)
    for d, adv in sorted_devs:
        name = adv.local_name or d.name or "Unknown"
        rssi = adv.rssi or 0
        is_target = DEVICE_MAC.lower() in (d.address or "").lower()
        is_njcuk = "njcuk" in name.lower() if name else False

        if is_target or is_njcuk:
            marker = " <<<< 目标设备!"
            found = True
        else:
            marker = ""

        # 只显示有名字的设备或目标设备
        if name != "Unknown" or is_target:
            print(f"  {d.address}  RSSI={rssi:4d}  {name}{marker}")

    if not found:
        print(f"\n[!] 未找到目标设备 ({DEVICE_MAC})")
        print("    请确认充电器已通电且蓝牙开启")

    return found


# ============================================================
# 列出 GATT 服务
# ============================================================
async def list_services(mac=DEVICE_MAC):
    """连接设备并列出所有 GATT 服务和特征。"""
    print(f"[*] 连接 {mac} 并枚举 GATT 服务...")
    async with BleakClient(mac) as client:
        print(f"[+] 已连接! MTU={client.mtu_size}\n")
        for service in client.services:
            print(f"Service: {service.uuid}  [{service.description}]")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  Char: {char.uuid}  Handle=0x{char.handle:04X}  [{props}]")
                print(f"        {char.description}")
                for desc in char.descriptors:
                    print(f"    Desc: {desc.uuid}  Handle=0x{desc.handle:04X}")


# ============================================================
# 主要工作流
# ============================================================
async def cmd_info(mac=DEVICE_MAC):
    """读取设备基本信息。"""
    ctrl = CuktechBLEController(mac)
    try:
        await ctrl.connect()
        await ctrl.read_device_info()
    finally:
        await ctrl.disconnect()


async def cmd_auth(mac=DEVICE_MAC):
    """测试认证流程。"""
    ctrl = CuktechBLEController(mac)
    try:
        await ctrl.connect()
        await ctrl.read_device_info()
        print()
        result = await ctrl.authenticate()
        if result:
            print("\n[+] 认证成功，设备已就绪")
        else:
            print("\n[!] 认证失败")
            print("    可能原因:")
            print("    1. Token 不正确 (需要重新提取)")
            print("    2. 设备使用增强认证协议 (ECDH)")
            print("    3. 需要先在米家App中解绑再重新绑定")
    finally:
        await ctrl.disconnect()


async def cmd_status(mac=DEVICE_MAC):
    """读取充电器状态。"""
    ctrl = CuktechBLEController(mac)
    try:
        await ctrl.connect()
        await ctrl.read_device_info()
        print()
        if await ctrl.authenticate():
            print("\n[*] 读取充电器状态...")
            props = [
                (SIID_CHARGER, 5),   # 场景模式
                (SIID_CHARGER, 6),   # 息屏时间
                (SIID_CHARGER, 13),  # 语言
                (SIID_CHARGER, 15),  # USB-A 常通电
                (SIID_CHARGER, 19),  # 空闲息屏
                (SIID_CHARGER, 20),  # 屏幕方向锁
            ]
            results = await ctrl.get_properties(props)

            print("\n[*] 充电器状态:")
            for (siid, piid), val in results.items():
                name = PIID_NAMES.get(piid, f'PIID {piid}')
                display = PIID_DISPLAY.get(piid, {})
                print(f"  {name}: {display.get(val, val)}")
        else:
            print("\n[!] 认证失败，无法读取状态")
    finally:
        await ctrl.disconnect()


async def cmd_probe(mac=DEVICE_MAC):
    """探测所有属性 (PIID 1-20)。"""
    ctrl = CuktechBLEController(mac)
    try:
        await ctrl.connect()
        await ctrl.read_device_info()
        print()
        if await ctrl.authenticate():
            print("\n[*] 探测所有属性 (SIID=2, PIID 1-20)...\n")
            # 跳过 PIID 14 (write-only) 和 1-4 (推送数据，GET 可能不支持)
            skip = {14}
            push_piids = {1, 2, 3, 4}
            for piid in range(1, 21):
                name = PIID_NAMES.get(piid, f'未知-{piid}')
                if piid in skip:
                    print(f"  PIID {piid:2d} [{name:8s}]: (只写, 跳过)")
                    continue
                result = await ctrl.send_miot_command(SIID_CHARGER, piid)
                if result and result.get('value') is not None:
                    val = result['value']
                    display = PIID_DISPLAY.get(piid, {})
                    display_str = display.get(val, '')
                    if display_str:
                        display_str = f' ({display_str})'
                    raw_hex = result['raw'].hex() if result.get('raw') else ''
                    print(f"  PIID {piid:2d} [{name:8s}]: {val}{display_str}  raw={raw_hex}")
                else:
                    print(f"  PIID {piid:2d} [{name:8s}]: 无响应")
                await asyncio.sleep(0.3)
        else:
            print("\n[!] 认证失败")
    finally:
        await ctrl.disconnect()


async def cmd_monitor(mac=DEVICE_MAC):
    """实时监控端口充电数据。"""
    ctrl = CuktechBLEController(mac)
    try:
        await ctrl.connect()
        await ctrl.read_device_info()
        print()
        if await ctrl.authenticate():
            print("\n[*] 实时监控端口充电数据 (Ctrl+C 退出)...\n")
            port_names = {1: 'C1', 2: 'C2', 3: 'C3', 4: 'A '}
            last_values = {}
            try:
                while True:
                    data = await ctrl._wait_notify("cmd_recv", timeout=3.0)
                    if not data or len(data) < 4:
                        continue

                    if data[2] == 0x02 and len(data) >= 4:
                        encrypted_payload = data[4:]
                        await ctrl.client.write_gatt_char(
                            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x03, 0x00]),
                            response=False)
                        pt = ctrl._decrypt(encrypted_payload)
                        if not pt or len(pt) < 8:
                            continue

                        b4 = pt[4]
                        piid = pt[7] if len(pt) > 7 else -1

                        # 只处理端口数据推送 (B4=0x04, piid=1-4)
                        if b4 == 0x04 and piid in port_names:
                            # 复用 decode_port 解析逻辑
                            port_info = decode_port(piid, pt)
                            if port_info:
                                name = port_names[piid]
                                # 跳过重复值
                                key = piid
                                current = (port_info["voltage"], port_info["current"], port_info["power"])
                                if last_values.get(key) == current:
                                    continue
                                last_values[key] = current

                                print(f"  [{name}] "
                                      f"V={port_info['voltage']:.1f} "
                                      f"I={port_info['current']:.1f} "
                                      f"P={port_info['power']:.1f} "
                                      f"protocol={port_info['protocol']}")

                    elif data[2] == 0x00 and len(data) >= 6:
                        frame_count = data[4] + 0x100 * data[5]
                        await ctrl.client.write_gatt_char(
                            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x01]),
                            response=False)
                        for _ in range(frame_count):
                            await ctrl._wait_notify("cmd_recv", timeout=3.0)
                        await ctrl.client.write_gatt_char(
                            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x00]),
                            response=False)

            except KeyboardInterrupt:
                print("\n[*] 监控已停止")
        else:
            print("\n[!] 认证失败")
    finally:
        await ctrl.disconnect()


async def cmd_generic(mac, action, params):
    """通用 get/set 命令。"""
    ctrl = CuktechBLEController(mac)
    try:
        await ctrl.connect()
        await ctrl.read_device_info()
        print()
        if await ctrl.authenticate():
            piid = int(params[0])
            name = PIID_NAMES.get(piid, f'PIID {piid}')
            if action == "get":
                result = await ctrl.send_miot_command(SIID_CHARGER, piid)
                if result and result.get('value') is not None:
                    val = result['value']
                    display = PIID_DISPLAY.get(piid, {})
                    display_str = display.get(val, '')
                    if display_str:
                        display_str = f' ({display_str})'
                    print(f"[+] {name} = {val}{display_str}")
                    if result.get('raw'):
                        print(f"    raw: {result['raw'].hex()}")
                else:
                    print(f"[!] {name}: 无响应")
            else:
                val = int(params[1])
                result = await ctrl.send_miot_command(SIID_CHARGER, piid, value=val)
                if result:
                    print(f"[+] {name} 已设置: {val}")
                    await asyncio.sleep(0.5)
                    readback = await ctrl.send_miot_command(SIID_CHARGER, piid)
                    if readback and readback.get('value') is not None:
                        print(f"[*] 验证读回: {readback['value']}")
                else:
                    print(f"[!] {name}: 设置失败")
        else:
            print("\n[!] 认证失败")
    finally:
        await ctrl.disconnect()


# 命名属性的值映射
SET_COMMANDS = {
    'set-mode':     {'piid': 5,  'name': '场景模式',
                     'values': {'ai': 1, 'apple': 2, 'single': 3, 'balance': 4}},
    'set-screen':   {'piid': 6,  'name': '息屏时间',
                     'values': {'1': 4, '5': 0, '10': 1, '30': 2, 'off': 3}},
    'set-language':  {'piid': 13, 'name': '语言',
                     'values': {'en': 0, 'cn': 1}},
    'set-usba':     {'piid': 15, 'name': 'USB-A常通电',
                     'values': {'on': 1, 'off': 0, '1': 1, '0': 0}},
    'goto':         {'piid': 14, 'name': '显示界面', 'write_only': True,
                     'values': {'1': 1, '2': 2, '3': 3, '4': 4, '5': 5}},
    'set-idle':     {'piid': 19, 'name': '空闲息屏',
                     'values': {'on': 1, 'off': 0, '1': 1, '0': 0}},
    'set-orient':   {'piid': 20, 'name': '屏幕方向锁',
                     'values': {'on': 1, 'off': 0, '1': 1, '0': 0}},
}


async def cmd_set_property(mac, prop_name, params):
    """设置充电器属性。"""
    # 倒计时特殊处理 (需要两个参数: 端口 + 分钟数)
    if prop_name == 'set-timer':
        if len(params) < 2:
            print("[!] 用法: set-timer <c1/c2/c3/a> <分钟数>")
            return
        port = params[0].lower()
        piid = TIMER_PORTS.get(port)
        if piid is None:
            print(f"[!] 无效端口: {port} (可选: c1/c2/c3/a)")
            return
        try:
            minutes = int(params[1])
        except ValueError:
            print(f"[!] 无效分钟数: {params[1]}")
            return
        ctrl = CuktechBLEController(mac)
        try:
            await ctrl.connect()
            await ctrl.read_device_info()
            print()
            if await ctrl.authenticate():
                result = await ctrl.send_miot_command(SIID_CHARGER, piid, value=minutes)
                if result:
                    print(f"[+] {port.upper()} 口倒计时已设置: {minutes} 分钟")
                    await asyncio.sleep(0.5)
                    readback = await ctrl.send_miot_command(SIID_CHARGER, piid)
                    if readback and readback.get('value') is not None:
                        print(f"[*] 验证读回: {readback['value']} 分钟")
            else:
                print("\n[!] 认证失败")
        finally:
            await ctrl.disconnect()
        return

    # 端口控制特殊处理 (位掩码: 读取当前值 → 修改对应位 → 写回)
    if prop_name == 'set-port':
        if len(params) < 2:
            print("[!] 用法: set-port <c1/c2/c3/a/all> <on/off>")
            return
        port = params[0].lower()
        action = params[1].lower()
        if action not in ('on', 'off'):
            print(f"[!] 无效操作: {action} (可选: on/off)")
            return
        ctrl = CuktechBLEController(mac)
        try:
            await ctrl.connect()
            await ctrl.read_device_info()
            print()
            if await ctrl.authenticate():
                # 先读取当前端口控制值
                current = await ctrl.send_miot_command(SIID_CHARGER, 16)
                if not current or current.get('value') is None:
                    print("[!] 无法读取当前端口状态")
                    return
                cur_val = current['value']
                port_names = ['C1', 'C2', 'C3', 'A']
                cur_bits = f"{''.join(port_names[i] if cur_val & (1<<i) else '--' for i in range(4))}"
                print(f"  当前端口状态: {cur_val} (0b{cur_val:04b}) [{cur_bits}]")

                if port == 'all':
                    new_val = 0x0F if action == 'on' else 0x00
                else:
                    bit = PORT_BITS.get(port)
                    if bit is None:
                        print(f"[!] 无效端口: {port} (可选: c1/c2/c3/a/all)")
                        return
                    if action == 'on':
                        new_val = cur_val | (1 << bit)
                    else:
                        new_val = cur_val & ~(1 << bit)

                if new_val == cur_val:
                    print(f"  端口已经是目标状态，无需修改")
                    return

                result = await ctrl.send_miot_command(SIID_CHARGER, 16, value=new_val)
                if result:
                    new_bits = f"{''.join(port_names[i] if new_val & (1<<i) else '--' for i in range(4))}"
                    print(f"[+] 端口控制已设置: {new_val} (0b{new_val:04b}) [{new_bits}]")
                    await asyncio.sleep(0.5)
                    readback = await ctrl.send_miot_command(SIID_CHARGER, 16)
                    if readback and readback.get('value') is not None:
                        rb = readback['value']
                        rb_bits = f"{''.join(port_names[i] if rb & (1<<i) else '--' for i in range(4))}"
                        print(f"[*] 验证读回: {rb} (0b{rb:04b}) [{rb_bits}]")
            else:
                print("\n[!] 认证失败")
        finally:
            await ctrl.disconnect()
        return

    # 通用命名属性处理
    cmd_def = SET_COMMANDS.get(prop_name)
    if not cmd_def:
        print(f"[!] 未知命令: {prop_name}")
        return

    value_str = params[0] if params else None
    if not value_str:
        options = '/'.join(k for k in cmd_def['values'] if not k.isdigit() or k in ('1', '0'))
        print(f"[!] {prop_name} 需要参数 (可选: {options})")
        return

    val = cmd_def['values'].get(value_str.lower())
    if val is None:
        options = '/'.join(k for k in cmd_def['values'] if not k.isdigit() or k in ('1', '0'))
        print(f"[!] 无效值: {value_str} (可选: {options})")
        return

    ctrl = CuktechBLEController(mac)
    try:
        await ctrl.connect()
        await ctrl.read_device_info()
        print()
        if await ctrl.authenticate():
            result = await ctrl.send_miot_command(SIID_CHARGER, cmd_def['piid'], value=val)
            if result:
                print(f"[+] {cmd_def['name']}已设置: {value_str}")
                if not cmd_def.get('write_only'):
                    await asyncio.sleep(0.5)
                    readback = await ctrl.send_miot_command(SIID_CHARGER, cmd_def['piid'])
                    if readback and readback.get('value') is not None:
                        display = PIID_DISPLAY.get(cmd_def['piid'], {})
                        rb_val = readback['value']
                        print(f"[*] 验证读回: {display.get(rb_val, rb_val)}")
        else:
            print("\n[!] 认证失败")
    finally:
        await ctrl.disconnect()


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="CUKTECH 10 GaN Charger Ultra - BLE 直连控制器"
    )
    all_commands = [
        "scan", "info", "services", "auth", "status", "probe", "monitor",
        "get", "set",
        "set-mode", "set-screen", "set-language", "set-usba",
        "goto", "set-timer", "set-port", "set-idle", "set-orient",
        "help",
    ]
    parser.add_argument("command", nargs="?", default="help",
                        choices=all_commands, help="执行的命令")
    parser.add_argument("params", nargs="*", default=[],
                        help="命令参数")
    parser.add_argument("--mac", default=DEVICE_MAC,
                        help=f"设备 MAC 地址 (默认: {DEVICE_MAC})")

    args = parser.parse_args()

    if args.command == "help" or len(sys.argv) == 1:
        print("""
CUKTECH 10 GaN Charger Ultra - BLE 直连控制器

基本命令:
  scan                     扫描查找设备
  info                     读取设备信息 (无需认证)
  services                 列出 GATT 服务和特征
  auth                     测试 BLE 认证流程

查询命令:
  status                   读取充电器常用状态
  probe                    探测所有属性 (PIID 1-20)
  monitor                  实时监控端口充电数据
  get <piid>               读取指定属性 (原始值)

设置命令:
  set <piid> <value>       设置指定属性 (原始值)
  set-mode <mode>          场景模式 (ai/apple/single/balance)
  set-screen <time>        息屏时间 (1/5/10/30/off 分钟)
  set-language <lang>      语言 (en/cn)
  set-usba <switch>        USB-A 常通电 (on/off)
  goto <page>              切换显示界面 (1-5)
  set-timer <port> <min>   端口倒计时 (c1/c2/c3/a + 分钟数)
  set-port <port> <on/off> 端口开关 (c1/c2/c3/a/all)
  set-idle <switch>        空闲息屏 (on/off)
  set-orient <switch>      屏幕方向锁定 (on/off)

示例:
  python cuktech_ble.py status
  python cuktech_ble.py probe
  python cuktech_ble.py set-mode ai
  python cuktech_ble.py set-timer c1 30
  python cuktech_ble.py get 16
  python cuktech_ble.py set 16 1
""")
        return

    set_commands = {"set-mode", "set-screen", "set-language", "set-usba",
                    "goto", "set-timer", "set-port", "set-idle", "set-orient"}

    if args.command == "scan":
        asyncio.run(scan_devices())
    elif args.command == "info":
        asyncio.run(cmd_info(args.mac))
    elif args.command == "services":
        asyncio.run(list_services(args.mac))
    elif args.command == "auth":
        asyncio.run(cmd_auth(args.mac))
    elif args.command == "status":
        asyncio.run(cmd_status(args.mac))
    elif args.command == "probe":
        asyncio.run(cmd_probe(args.mac))
    elif args.command == "monitor":
        asyncio.run(cmd_monitor(args.mac))
    elif args.command in ("get", "set"):
        if not args.params:
            print(f"[!] 用法: {args.command} <piid> [value]")
            return
        if args.command == "set" and len(args.params) < 2:
            print("[!] 用法: set <piid> <value>")
            return
        asyncio.run(cmd_generic(args.mac, args.command, args.params))
    elif args.command in set_commands:
        if args.command not in ("set-timer", "set-port") and not args.params:
            print(f"[!] {args.command} 需要参数值")
            return
        asyncio.run(cmd_set_property(args.mac, args.command, args.params))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
