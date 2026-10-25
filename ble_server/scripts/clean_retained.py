#!/usr/bin/env python3
"""回滚用：清理非 primary 设备的 retained MQTT 消息（设计文档 §6.1）。

为什么需要它
------------
回滚到旧版本后，服务器不再发布 `{base}/{id}/...`，但 broker 里那些 retained 消息
还在——旧 HA 集成 / MQTT Explorer 会把它读成一台"幽灵设备"：第二台充电器的数据
永远停在回滚前那一刻。清理方式是对这些 topic 重新 publish **空 payload + retain**，
broker 就会把 retained 消息删掉。

两条硬约束（§6.1 明确写出）：

1. **不得硬编码 `cuktech/#`**——`mqtt.topic_prefix` 可配，前缀要按配置推导；
2. MQTT 协议**没有**"按通配符发布"这种操作（`#` 只能用于订阅），所以按"已知叶子
   topic × 每台设备的实际前缀"枚举，而不是给 broker 发一个通配符。

用法
----
    .venv/bin/python scripts/clean_retained.py --dry-run   # 先看要清哪些 topic
    .venv/bin/python scripts/clean_retained.py             # 真清
    ./cuktech_ctl.sh clean-retained                      # 等价封装

    # 已从配置里删掉 / 改掉前缀的设备（推导不出来，必须手工给）：
    .venv/bin/python scripts/clean_retained.py --dry-run --prefix cuktech/charger/desk

（设计文档 §6.1 的运维脚本，与 `history_down.py` 同级。）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: 服务器会 retain 的叶子 topic（端口数据/设置/连接状态）。空 payload + retain 即删除。
RETAINED_LEAVES = (
    "status",
    "settings",
    "port/c1", "port/c2", "port/c3", "port/a",
)


def topics_to_clear(prefixes) -> list[str]:
    """`{prefix}/{leaf}` 全组合（去重排序，便于 dry-run 输出稳定）。"""
    return sorted({f"{p}/{leaf}" for p in prefixes if p for leaf in RETAINED_LEAVES})


def non_primary_prefixes(config) -> list[str]:
    """配置里**非 primary** 设备的 topic 前缀（primary 的 retained 消息要保留）。

    前缀一律经 `Config.topics_for()` 推导——空 = 自动 `{base}/{id}`，绝不在脚本里
    拼死字符串（§6.1 "不得硬编码 cuktech/#"）。
    """
    from config import PRIMARY_DEVICE_ID

    out = []
    for spec in config.device_specs():
        if spec.is_primary or spec.device_id == PRIMARY_DEVICE_ID:
            continue
        out.append(config.topics_for(spec).prefix)
    return sorted(set(out))


def clean(client, topics, *, qos: int = 1) -> int:
    """对每个 topic 发空 payload + retain（broker 收到就删 retained）。返回条数。"""
    n = 0
    for topic in topics:
        client.publish(topic, payload=b"", qos=qos, retain=True)
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Clear retained MQTT topics of non-primary devices (§6.1 rollback).")
    ap.add_argument("--config", default="",
                    help="config.yaml 路径（默认与服务器一致：CUKTECH_CONFIG_PATH "
                         "或脚本同目录 config.yaml）")
    ap.add_argument("--dry-run", action="store_true", help="只打印 topic，不连接 broker")
    ap.add_argument("--wait", type=float, default=1.0, help="发布后等待秒数（默认 1s）")
    ap.add_argument("--prefix", action="append", default=[], metavar="PREFIX",
                    help="额外清理这些前缀（可重复）。用于「已经从配置里删掉或改掉的旧"
                         "前缀」：那种前缀无法从当前配置推导，只能手工指定，这也是 MQTT "
                         "里唯一剩下的补救路径。例：--prefix cuktech/charger/desk")
    args = ap.parse_args(argv)

    if args.config:                       # 与服务器同一套环境变量口径
        os.environ["CUKTECH_CONFIG_PATH"] = args.config
    from config import load_config  # 延迟导入：--help 不该依赖 yaml/paho

    config = load_config()
    prefixes = non_primary_prefixes(config)
    # 手工指定的前缀（已从配置里删除/改名的旧前缀）与推导出来的合并去重：
    # 服务端现在会在删除/改前缀的当场清理，但历史遗留（升级前就产生的幽灵）只能靠这里。
    manual = [p.strip().rstrip("/") for p in (args.prefix or []) if p.strip()]
    if manual:
        print(f"额外指定前缀: {', '.join(manual)}")
    prefixes = sorted(set(prefixes) | set(manual))
    if not prefixes:
        print("没有非 primary 设备，无需清理（单设备/ESP32 场景）；"
              "如要清已删除设备的旧前缀，用 --prefix 指定")
        return 0
    topics = topics_to_clear(prefixes)
    print(f"前缀: {', '.join(prefixes)}")
    print(f"待清理 {len(topics)} 个 topic:")
    for t in topics:
        print(f"  {t}")
    if args.dry_run:
        print("(dry-run：未连接 broker)")
        return 0

    mqtt_cfg = config.mqtt
    import time

    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if mqtt_cfg.username:
        client.username_pw_set(mqtt_cfg.username, mqtt_cfg.password)
    client.connect(mqtt_cfg.host, mqtt_cfg.port, 5)
    client.loop_start()
    try:
        clean(client, topics)
        time.sleep(args.wait)      # 等 PUBACK/写出，别没发完就断
    finally:
        client.loop_stop()
        client.disconnect()
    print("清理完成。可用 MQTT Explorer 确认 retained 已消失。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
