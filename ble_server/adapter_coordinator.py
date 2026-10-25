"""BLE 适配器共享协调器（设计文档 §3.4 / §3.5，P2 阶段）。

为什么必须有它
--------------
改造前所有恢复动作都是"整机级"的：每次重连自己扫一遍、每次连接前
`bluetoothctl scan off`、`InProgress` 就整块适配器 power off/on、认证卡死就
`os.execv`。只有一台设备时这些都无害——**多设备时它们互相伤害**：

| 现状 | 多设备危害 |
|---|---|
| 每个 manager 独立 `BleakScanner.find_device_by_address` | 并发扫描撞 BlueZ `InProgress` |
| 每次连接前 `bluetoothctl scan off` | 全局终止**其他设备**正在进行的扫描 |
| `InProgress` → 适配器 power off/on | 杀掉**其他所有设备**的连接 |
| 认证卡死 → `os.execv` | 所有设备一起掉线 |

所以本模块是**进程内唯一允许触碰适配器 / bluetoothctl 的模块**。它做四件事：

1. **扫描合并**：N 个 manager 同时找设备 → 一个扫描窗口服务所有等待者，扫描前
   清理（`scan off`）在 `scan_lock` 内每窗口执行一次；
2. **连接信号量 + 启动错峰**：并发连接/认证上限（默认 2），manager 按注册顺序
   间隔 `startup_stagger_sec` 启动——10 台上电不会变成认证风暴；
3. **分级恢复**（§3.5，禁止跳级）：L1 只断开目标设备 → L1.5 `remove` 清该 MAC 的
   BlueZ 缓存 → L2 适配器 power cycle（**仅** N=1 或全部设备已离线，且必须走
   quiesce 门闸）→ L3 认证卡死（N=1 仍进程重启，N>1 先做进程内 per-device 重建）；
4. **门闸**：`recovery_gate` 关闭期间所有 manager 的重连循环停在门口，避免"一台
   设备的故障"被退避重连放大成"全场熔断"。

N=1 时行为与改造前一致
----------------------
单设备时 `recover_device()` 直接走 L2（power cycle），命令时序与旧的
`BLEManager._force_disconnect_bluetooth()` **逐条相同**：`scan off` → `disconnect`
→ 1s → `power off` → 3s → `power on` → 15 次就绪探测（`bluetoothctl show`，失败
再退回 sysfs）。"单设备零感知"不是口号，这条时序 old→new 要能对照。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil

#: 恢复级别（与 §3.5 表格一一对应；顺序即"只能升级不能跳级"的顺序）
LEVEL_DEVICE = "L1"            # bluetoothctl disconnect <mac> + 本设备重连
LEVEL_REMOVE = "L1.5"          # bluetoothctl remove <mac>（清 BlueZ 缓存）
LEVEL_ADAPTER = "L2"           # 适配器 power off/on（带 quiesce 门闸）
LEVEL_INPROCESS = "L3-inprocess"   # 进程内 per-device 重建
LEVEL_PROCESS = "L3-process"       # 整进程重启（os.execv）

#: 连续多少次 L1 失败后升级到 L1.5。1 次不够：链路抖动本身就该由 L1 吸收。
L1_FAILURES_BEFORE_REMOVE = 1

#: 同样的 MAC 连续多少次 L1.5 失败后才有资格申请 L2（除非 N=1）
L15_FAILURES_BEFORE_ADAPTER = 1


def has_bluetoothctl() -> bool:
    return shutil.which("bluetoothctl") is not None


class AdapterCoordinator:
    """共享 BLE 适配器的唯一调度者（单例，由 `Server` 建并注入每个 manager）。"""

    #: 适配器就绪探测的轮数（每次 1s）——与旧 `_force_disconnect_bluetooth` 一致
    READY_PROBE_ROUNDS = 15

    def __init__(self, *, device_count_provider=None, connect_limit: int = 2,
                 startup_stagger_sec: float = 2.0, log=None):
        self._device_count = device_count_provider or (lambda: 1)
        self.connect_sem = asyncio.Semaphore(connect_limit)
        self.scan_lock = asyncio.Lock()
        self.adapter_lock = asyncio.Lock()
        # 门闸默认放行；只有 L2 期间关闭（§3.5 第 1 步）
        self.recovery_gate = asyncio.Event()
        self.recovery_gate.set()
        self.live_connections: set[str] = set()
        self.startup_stagger_sec = startup_stagger_sec
        self.log = log
        # 扫描合并：正在等结果的 MAC → 各自的 Future
        self._pending: dict[str, asyncio.Future] = {}
        # 分级恢复的失败计数（按 MAC）
        self._l1_failures: dict[str, int] = {}
        self._l15_failures: dict[str, int] = {}
        # 认证卡死中的设备数：≥2 台同时卡住 = 适配器级故障，必须升级为进程重启
        self._stuck_devices: set[str] = set()
        # 注入点：L3 进程内重建 / 进程重启（由 Server 提供，本模块不拥有 runtime）
        self._rebuild_hook = None
        self._process_restart_hook = None
        # 可注入的睡眠（测试里换成 no-op，避免真等 3s/15s）
        self.sleep = asyncio.sleep
        # 排障计数（测试也据此断言"到底升到哪一级"）
        self.adapter_resets = 0
        self.scans = 0

    # ────────────────────── 装配 ──────────────────────

    def set_device_count_provider(self, fn) -> None:
        self._device_count = fn

    def set_rebuild_hook(self, fn) -> None:
        """注册 L3 进程内重建钩子：`async fn(mac)`。"""
        self._rebuild_hook = fn

    def set_process_restart_hook(self, fn) -> None:
        """注册进程重启钩子：`async fn()`（`os.execv` 由服务层负责，按平台分流）。"""
        self._process_restart_hook = fn

    @property
    def device_count(self) -> int:
        try:
            return int(self._device_count())
        except Exception:
            return 1

    def _log(self, level: str, msg: str, *args) -> None:
        if self.log is not None:
            getattr(self.log, level)(msg, *args)

    # ────────────────────── 链路登记 ──────────────────────

    def register_connection(self, mac: str) -> None:
        self.live_connections.add(mac)

    def unregister_connection(self, mac: str) -> None:
        self.live_connections.discard(mac)

    def has_live_connections(self) -> bool:
        return bool(self.live_connections)

    # ────────────────────── 并发控制 ──────────────────────

    @contextlib.asynccontextmanager
    async def connect_slot(self):
        """连接 + 认证窗口（上限 `connect_limit`）。超出就排队，不失败。"""
        async with self.connect_sem:
            yield

    async def wait_recovery_gate(self) -> None:
        """每次重连尝试**之前**都要过这道门（L2 期间会挡住所有 manager）。"""
        await self.recovery_gate.wait()

    async def stagger_delay(self, index: int) -> None:
        """启动错峰：第 index 台设备启动前等 index × startup_stagger_sec。

        单设备（index=0）恒为 0 延迟——N=1 的启动时序与改造前完全一致。
        """
        if index <= 0 or self.startup_stagger_sec <= 0:
            return
        await self.sleep(index * self.startup_stagger_sec)

    # ────────────────────── 扫描（合并 + 清理收口）──────────────────────

    async def scan_cleanup(self) -> None:
        """`bluetoothctl scan off`：清掉 BlueZ 残留扫描，避免 `InProgress`。

        归属从 manager 挪到这里：只允许在扫描窗口内、由协调器执行一次。
        """
        if not has_bluetoothctl():
            return
        await self._bluetoothctl("scan", "off")

    async def _discover(self, timeout: float) -> dict:
        """一次真实扫描 → 两种索引：按地址、按 MiBeacon service-data 里的真 MAC。"""
        from bleak import BleakScanner
        from cuktech_ble.protocol import UUID_FE95

        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        index: dict[str, object] = {}
        for addr, pair in found.items():
            dev = pair[0] if isinstance(pair, tuple) else pair
            adv = pair[1] if isinstance(pair, tuple) else None
            index[str(addr).lower()] = dev
            payload = ((getattr(adv, "service_data", None) or {}).get(UUID_FE95)
                       if adv is not None else None)
            if payload and len(payload) >= 6:
                # macOS/CoreBluetooth 从不暴露真 MAC（隐私随机化 UUID），
                # 只能靠 MiBeacon service-data 尾 6 字节（真 MAC 的小端形式）确认身份。
                index["miot:" + bytes(payload[-6:]).hex()] = dev
        return index

    async def find_device(self, mac: str, timeout: float):
        """找设备；同一窗口内的多个等待者共享**一次**扫描。

        返回 bleak 设备对象，找不到返回 None（调用方按"Charger not found"处理）。
        """
        key = mac.lower()
        pending = self._pending.get(key)
        if pending is not None and not pending.done():
            return await pending

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        try:
            async with self.scan_lock:
                # 等锁期间别的扫描可能已经把这个 MAC 找出来了
                if fut.done():
                    return fut.result()
                await self.scan_cleanup()
                try:
                    self.scans += 1
                    index = await self._discover(timeout)
                except Exception as e:
                    self._log("warning", "BLE scan failed: %s", e)
                    index = {}
                # 一次扫描服务所有等待者（合并的全部意义）
                for waiter_key, waiter in list(self._pending.items()):
                    if waiter.done():
                        continue
                    dev = (index.get(waiter_key)
                           or index.get("miot:" + self._mac_bytes_hex(waiter_key)))
                    waiter.set_result(dev)
                if not fut.done():          # 自己也不在 _pending 里了（防御）
                    fut.set_result(None)
                return fut.result()
        finally:
            self._pending.pop(key, None)

    @staticmethod
    def _mac_bytes_hex(mac: str) -> str:
        """MAC → MiBeacon 尾 6 字节的小端十六进制（与 `mac_str_to_bytes` 同序）。"""
        try:
            from cuktech_ble.protocol import mac_str_to_bytes
            return mac_str_to_bytes(mac).hex()
        except Exception:
            return ""

    # ────────────────────── 分级恢复（§3.5）──────────────────────

    async def _bluetoothctl(self, *args) -> bytes:
        if not has_bluetoothctl():
            return b""
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return stdout or b""
        except Exception:
            return b""

    async def _l1_disconnect(self, mac: str) -> None:
        await self._bluetoothctl("disconnect", mac)
        self.unregister_connection(mac)
        await self.sleep(1)      # 等 BLE Link Layer disconnect 完成（与旧时序一致）

    async def _l15_remove(self, mac: str) -> None:
        await self._bluetoothctl("remove", mac)

    def _can_reset_adapter(self) -> bool:
        """L2 的准入条件（§3.5）：N=1（保持旧节奏）或所有设备都已离线。"""
        return self.device_count <= 1 or not self.has_live_connections()

    async def recover_device(self, mac: str) -> str:
        """按分级阶梯恢复一台设备，返回**实际执行到的级别**（禁止跳级）。

        - 始终先做 L1（只影响目标设备）；
        - L1 连续失败 `L1_FAILURES_BEFORE_REMOVE` 次 → L1.5；
        - 只有 N=1 或全体离线才允许 L2；多设备且有活连接时停在 L1.5 + 退避。
        """
        if self.device_count <= 1:
            # N=1：分级阶梯整体退化 —— 改造前就是"断开 + 适配器 power cycle"，
            # 单设备时下电不会伤害别人，也没有 L1.5 的中间态可言。时序必须一致。
            await self._l1_disconnect(mac)
            await self.recover_adapter()
            return LEVEL_ADAPTER

        await self._l1_disconnect(mac)
        self._l1_failures[mac] = self._l1_failures.get(mac, 0) + 1
        if self._l1_failures[mac] <= L1_FAILURES_BEFORE_REMOVE:
            self._log("info", "L1 recovery for %s (disconnect + reconnect)", mac)
            return LEVEL_DEVICE

        await self._l15_remove(mac)
        self._l15_failures[mac] = self._l15_failures.get(mac, 0) + 1
        if (self.device_count > 1 and self.has_live_connections()
                and self._l15_failures[mac] <= L15_FAILURES_BEFORE_ADAPTER):
            self._log("warning",
                      "L1.5 recovery for %s (removed BlueZ cache; adapter left alone "
                      "because other devices are still connected)", mac)
            return LEVEL_REMOVE

        if not self._can_reset_adapter():
            self._log("warning",
                      "L1.5 recovery for %s — adapter power cycle skipped "
                      "(N=%d, %d live connection(s))", mac, self.device_count,
                      len(self.live_connections))
            return LEVEL_REMOVE

        await self.recover_adapter()
        self._l1_failures.pop(mac, None)
        self._l15_failures.pop(mac, None)
        return LEVEL_ADAPTER

    async def recover_adapter(self) -> None:
        """L2：适配器 power off/on，**必须**走 quiesce 门闸（§3.5）。

        1. 关门闸 → 所有 manager 的重连循环停在 `wait_recovery_gate()`；
        2. 逐个 L1 断开所有活链路；
        3. power off → 3s → power on → 就绪探测（15 轮）；
        4. 开门闸 → 各 manager 按注册顺序错峰放行（错峰由 `stagger_delay` 负责）。
        """
        async with self.adapter_lock:
            self.recovery_gate.clear()
            try:
                for mac in list(self.live_connections):
                    await self._l1_disconnect(mac)
                await self._power_cycle()
                self.adapter_resets += 1
            finally:
                # 无论 power cycle 成功与否都必须开门：门闸关着不放行 = 全场停摆
                self.recovery_gate.set()

    async def _power_cycle(self) -> None:
        """适配器下电/上电 + 就绪探测：命令与时序与旧实现逐条一致。"""
        if not has_bluetoothctl():
            self._log("info", "bluetoothctl not available, skipping adapter power cycle")
            return
        await self.scan_cleanup()
        await self._bluetoothctl("power", "off")
        await self.sleep(3)      # 等 BLE 芯片完全下电
        await self._bluetoothctl("power", "on")
        hci = _find_ble_adapter()
        for _ in range(self.READY_PROBE_ROUNDS):
            await self.sleep(1)
            out = await self._bluetoothctl("show", hci)
            if b"Powered: yes" in out:
                self._log("info", "BT adapter ready after power cycle")
                return
            try:
                power_file = f"/sys/class/bluetooth/{hci}/power"
                if os.path.exists(power_file):
                    with open(power_file) as f:
                        if f.read().strip() == "1":
                            self._log("info", "BT adapter ready (via sysfs)")
                            return
            except Exception:
                pass
        self._log("warning", "BT adapter not ready after %ds, proceeding anyway",
                  self.READY_PROBE_ROUNDS)

    async def restart_device(self, mac: str) -> str:
        """L3：认证卡死恢复。N=1 走进程重启（与现状一致），N>1 走进程内重建。

        升级条件（§3.5）：**≥2 台设备同时** auth-stuck → 视为适配器级故障，必须
        升级为整进程重启，不允许各设备并行重建（否则互相踩适配器）。
        """
        self._stuck_devices.add(mac)
        if self.device_count <= 1:
            return await self._escalate_process("auth stuck with a single device")
        if len(self._stuck_devices) >= 2:
            return await self._escalate_process(
                f"{len(self._stuck_devices)} devices auth-stuck concurrently")
        if self._rebuild_hook is None:
            return await self._escalate_process("no in-process rebuild hook registered")
        self._log("critical", "Auth stuck on %s: rebuilding that device in-process "
                              "(process restart would drop %d other device(s))",
                  mac, self.device_count - 1)
        await self._rebuild_hook(mac)
        self._stuck_devices.discard(mac)
        self._l1_failures.pop(mac, None)
        self._l15_failures.pop(mac, None)
        return LEVEL_INPROCESS

    async def _escalate_process(self, reason: str) -> str:
        self._log("critical", "Escalating to process restart: %s", reason)
        if self._process_restart_hook is None:
            self._log("error", "No process restart hook registered; cannot self-heal")
            return LEVEL_PROCESS
        await self._process_restart_hook()
        return LEVEL_PROCESS

    # ── 测试/排障：清掉某台设备的失败计数（例如它成功连上之后）──
    def note_connected(self, mac: str) -> None:
        self.register_connection(mac)
        self._stuck_devices.discard(mac)
        self._l1_failures.pop(mac, None)
        self._l15_failures.pop(mac, None)


def _find_ble_adapter() -> str:
    """自动检测支持 BLE 的适配器名（hci0/hci1…）：与 `BLEManager._find_ble_adapter` 同源。"""
    if not os.path.exists("/sys/class/bluetooth"):
        return "hci0"
    import glob
    for hci_dir in sorted(glob.glob("/sys/class/bluetooth/hci*")):
        name = os.path.basename(hci_dir)
        if ":" in name:
            continue
        if os.path.isdir(os.path.join(hci_dir, "device")):
            return name
    return "hci0"
