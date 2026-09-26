"""Constants for CUKTECH Charger integration."""
from datetime import timedelta

DOMAIN = "cuktech_charger"
CONF_SERVER_URL = "server_url"
# 旧版 config entry 把地址存在 "host" 键下（与 "mac"/"token"/"ble_key" 同批）。
# 不读它的话，这类既有条目会静默回落 DEFAULT_SERVER_URL——服务端不在 localhost
# 的用户就会莫名其妙退化成 local 模式（限额不再与服务端同步）。
CONF_HOST = "host"
DEFAULT_SERVER_URL = "http://localhost:8199"

# MQTT Topics
TOPIC_PREFIX = "cuktech/charger"
TOPIC_PORT = f"{TOPIC_PREFIX}/port"
TOPIC_SETTINGS = f"{TOPIC_PREFIX}/settings"
TOPIC_STATUS = f"{TOPIC_PREFIX}/status"
TOPIC_SET = f"{TOPIC_PREFIX}/set"
TOPIC_CHARGE_EVENT = f"{TOPIC_PREFIX}/charge_event"

# Port mapping
PORT_MAP = {"c1": 1, "c2": 2, "c3": 3, "a": 4}
# piid -> 小写端口名（PORT_MAP 的反向表）。MQTT topic 用端口名、实体层用 piid，
# 反向查表比每次在 _on_port_message 里遍历 PORT_MAP（~1Hz）更直接。
PIID_TO_PORT = {piid: port for port, piid in PORT_MAP.items()}
PORT_NAMES = {1: "C1", 2: "C2", 3: "C3", 4: "A"}

# PIID names from the MIOT spec
PIID_NAMES = {
    1: "C1口数据",
    2: "C2口数据",
    3: "C3口数据",
    4: "A口数据",
    5: "场景模式",
    6: "息屏时间",
    7: "协议控制",
    8: "倒计时设置",
    9: "C1口倒计时",
    10: "C2口倒计时",
    11: "C3口倒计时",
    12: "A口倒计时",
    13: "语言",
    14: "进入界面",
    15: "USB-A小电流",
    16: "端口控制",
    19: "空闲息屏",
    20: "屏幕方向锁",
}

# PIID display values
PIID_DISPLAY = {
    5: {1: "AI模式", 2: "数码生态", 3: "单口模式", 4: "均衡模式"},
    # PIID 6 (screen_save_time): 与米家插件一致 (raw value -> 含义)
    # 1=5分钟, 2=10分钟, 3=30分钟, 4=常亮, 5=1分钟。value 0 不是有效设备值。
    6: {1: "5分钟", 2: "10分钟", 3: "30分钟", 4: "常亮", 5: "1分钟"},
    7: None,  # PIID 7 = bit flags (SCP/MiPPS/UFCS), 不需要显示映射
    13: {0: "English", 1: "中文"},
    15: {0: "关闭", 1: "开启"},
    19: {0: "关闭", 1: "开启"},
    20: {0: "关闭", 1: "开启"},
}

# Select options for each setting
SELECT_PIIDS = {
    5: {"name": "场景模式", "icon": "mdi:cog", "options": ["AI模式", "数码生态", "单口模式", "均衡模式"]},
    6: {"name": "息屏时间", "icon": "mdi:monitor", "options": ["1分钟", "5分钟", "10分钟", "30分钟", "常亮"]},
    13: {"name": "语言", "icon": "mdi:translate", "options": ["English", "中文"]},
}

# Derive option map from SELECT_PIIDS and PIID_DISPLAY (keep first match for duplicates)
SELECT_OPTION_MAP = {}
for piid, cfg in SELECT_PIIDS.items():
    display = PIID_DISPLAY.get(piid, {})
    option_map = {}
    for k, v in display.items():
        if v in cfg["options"] and v not in option_map:
            option_map[v] = k
    SELECT_OPTION_MAP[piid] = option_map

# Protocol switch bit definitions (PIID 21)
# c1: bit0=PD, bit1=PPS, bit2=UFCS, bit3=保留(固定1)
# c2: bit8=PD, bit9=PPS, bit10=UFCS, bit11=保留(固定1)
# c3: bit16=UFCS, bit17=SCP
# a:  bit24=UFCS, bit25=SCP
PROTOCOL_BITS = {
    "c1": {"pd": 0, "pps": 1, "ufcs": 2},
    "c2": {"pd": 8, "pps": 9, "ufcs": 10},
    "c3": {"ufcs": 16, "scp": 17},
    "a":  {"ufcs": 24, "scp": 25},
}

# ── 各端口支持的协议开关 (switch 平台) ──
PROTOCOL_SWITCHES = [
    ("c1", "pd", "C1 PD"),
    ("c1", "pps", "C1 PPS"),
    ("c1", "ufcs", "C1 UFCS"),
    ("c2", "pd", "C2 PD"),
    ("c2", "pps", "C2 PPS"),
    ("c2", "ufcs", "C2 UFCS"),
    ("c3", "ufcs", "C3 UFCS"),
    ("c3", "scp", "C3 SCP"),
    ("a", "ufcs", "USB-A UFCS"),
    ("a", "scp", "USB-A SCP"),
]

# ── 布尔设置开关 (switch 平台) ──
SETTING_PIIDS = {
    15: {"name": "USB-A小电流", "icon": "mdi:usb-port"},
    19: {"name": "空闲息屏", "icon": "mdi:monitor-off"},
    20: {"name": "屏幕方向锁", "icon": "mdi:screen-rotation-lock"},
}

# ── 端口电源开关 (switch 平台) ──
PORT_SWITCHES = {
    "c1": {"name": "C1 端口", "icon": "mdi:usb-c-port", "bit": 0},
    "c2": {"name": "C2 端口", "icon": "mdi:usb-c-port", "bit": 1},
    "c3": {"name": "C3 端口", "icon": "mdi:usb-c-port", "bit": 2},
    "a":  {"name": "USB-A 端口", "icon": "mdi:usb-port", "bit": 3},
}

# ── 倒计时 (number 平台): piid -> 配置 ──
COUNTDOWN_PIIDS = {
    9: {"name": "C1 倒计时", "icon": "mdi:timer-cog-outline"},
    10: {"name": "C2 倒计时", "icon": "mdi:timer-cog-outline"},
    11: {"name": "C3 倒计时", "icon": "mdi:timer-cog-outline"},
    12: {"name": "USB-A 倒计时", "icon": "mdi:timer-cog-outline"},
}

# ── 传感器协议选项 ──
PROTOCOL_OPTIONS = ["idle", "5V", "QC", "AFC", "FCP", "SCP", "PD", "PPS", "UFCS", "Unknown"]

# Device info
DEVICE_INFO = {
    "name": "酷态科10号超级电能充Ultra 充电器",
    "manufacturer": "CUKTECH",
    "model": "njcuk.fitting.ad1204",
    "sw_version": "",
}

# ── 充电量限额 (charge limits) ──
# 阈值单位是充电器输出能量（Wh，由 V×I 梯形积分得出），不是被充设备的实际充入
# 电量——线损与设备内转换损耗使后者偏小（典型 5~15%）。wh=0 表示禁用该端口限额。
#
# 算法与常数源自 ble_server/energy.py：同一台充电器、同一份 1Hz V/I 推送，
# 换后端时用户不该感到差异。energy_engine.py 从这里取常数（单一真源），
# 判定与断电执行也在那里。
MAX_LIMIT_WH = 1000.0
LIMIT_MODE_ONCE = "once"      # 达到阈值即关断并消费清零（一次性）
LIMIT_MODE_ALWAYS = "always"  # 长期有效，每次充电会话重新武装
# select 实体的 options 顺序即此顺序；mode 语义见 docs/integration-readme.md
LIMIT_MODES = (LIMIT_MODE_ONCE, LIMIT_MODE_ALWAYS)
DEFAULT_LIMIT_MODE = LIMIT_MODE_ONCE

LIMIT_STORE_VERSION = 1       # Store schema version (bump on layout change)
# 限额后端：local=HA 本地积分并判定（任意后端通用）；
#           server=委派给 Python BLE server 的 /api/charge-limits。
LIMIT_BACKEND_LOCAL = "local"
LIMIT_BACKEND_SERVER = "server"
# 委派模式下轮询服务端限额快照的周期。GET 只读内存态、无 IO，所以可以取得比较
# 密：这个快照同时承载 session_wh（实体显示与 Web UI 一致），间隔太大会看到跳变。
# 请求超时复用 HTTP_TIMEOUT —— 打的是同一个 server_url，没有理由两套。
LIMIT_POLL_INTERVAL = timedelta(seconds=5)

# ── 限额实体 (per-port number/select/sensor 三件套 × 4 端口) ──
# port -> 显示标签，复用 PORT_NAMES（与同设备的 C1/A 电压电流功率传感器同风格）。
# 各平台自行拼后缀（charge limit / charge limit mode / session energy），避免出现
# "C1 charge limit session energy" 这类叠词名。
CHARGE_LIMIT_PORTS = {
    port: PORT_NAMES[piid] for port, piid in PORT_MAP.items()
}
CHARGE_LIMIT_ICON = "mdi:battery-charging-60"

# ── 运行时常量 (coordinator 使用，避免魔法值散落) ──
HEALTH_CHECK_INTERVAL = timedelta(seconds=30)   # HTTP 健康检查周期
HTTP_TIMEOUT = 10                                # HTTP 请求超时 (秒)
BLE_OPERATION_TIMEOUT = 30                       # BLE 开关操作超时 (秒)
CHARGE_EVENT_BUFFER = 50                         # 充电事件缓冲上限 (条)
STATUS_STALE_SECONDS = 30                        # 状态消息超过此秒数视为过期 (与检查周期一致)
