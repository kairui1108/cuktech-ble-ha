"""Constants for CUKTECH Charger integration."""
DOMAIN = "cuktech_charger"
CONF_SERVER_URL = "server_url"
DEFAULT_SERVER_URL = "http://localhost:8199"

# MQTT Topics
TOPIC_PREFIX = "cuktech/charger"
TOPIC_PORT = f"{TOPIC_PREFIX}/port"
TOPIC_SETTINGS = f"{TOPIC_PREFIX}/settings"
TOPIC_STATUS = f"{TOPIC_PREFIX}/status"
TOPIC_SET = f"{TOPIC_PREFIX}/set"
TOPIC_PROTOCOL = f"{TOPIC_PREFIX}/protocol"
TOPIC_PROBE = f"{TOPIC_PREFIX}/__probe__"

# Port mapping
PORT_MAP = {"c1": 1, "c2": 2, "c3": 3, "a": 4}
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
    15: "USB-A常通电",
    16: "端口控制",
    19: "空闲息屏",
    20: "屏幕方向锁",
}

# PIID display values
PIID_DISPLAY = {
    5: {1: "AI模式", 2: "数码生态", 3: "单口模式", 4: "均衡模式"},
    6: {0: "5分钟", 1: "1分钟", 2: "10分钟", 3: "30分钟", 4: "常亮", 5: "1分钟(设备固件: value=5 也表示1分钟)"},
    7: None,  # PIID 7 = bit flags (SCP/MiPPS/UFCS), 不需要显示映射
    13: {0: "English", 1: "中文"},
    15: {0: "关闭", 1: "开启"},
    19: {0: "关闭", 1: "开启"},
    20: {0: "关闭", 1: "开启"},
}

# Select options for each setting
SELECT_PIIDS = {
    5: {"name": "场景模式", "icon": "mdi:cog", "options": ["AI模式", "数码生态", "单口模式", "均衡模式"]},
    6: {"name": "息屏时间", "icon": "mdi:monitor", "options": ["5分钟", "1分钟", "10分钟", "30分钟", "常亮"]},
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

# Device info
DEVICE_INFO = {
    "name": "酷态科10号超级电能充Ultra 充电器",
    "manufacturer": "CUKTECH",
    "model": "njcuk.fitting.ad1204",
    "sw_version": "",
}
