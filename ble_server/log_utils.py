"""设备维度日志标签（设计文档 §3.13，§3.3 红线配套件）。

为什么需要它
------------
改造后每台设备各有自己的 `BLEManager`，日志里同时有两路并发数据流；不带设备
标识时排障只能靠猜。约定 `%(device)s` 是格式串里唯一的"设备槽位"：

    "%(asctime)s %(name)s %(levelname)s %(device)s%(message)s"

`record.device` 的取值约定是**已装饰好的前缀**（`"[desk] "`，含尾空格）或空串：

- 空串 → 单设备日志行与改造前**逐字节一致**（格式串里不多一个空格）；
- 多设备 → 生产者注入 `device_tag("desk") == "[desk] "`，行变成
  `... INFO [desk] 已连接`。

**primary 不打标签**（`device_id == "default"` → 空串）：`default` 这个 id 是兼容层
的产物（§3.10 锚定 id），对只配一台机器的用户没有任何信息量，给每行都加
`[default] ` 只会让单设备日志"看起来变了"——P1 原则优先。多设备日志里
"没有标签的行 = primary" 由本文档约定，无需靠标签自证。

生产者侧（`BLEManager.log = LoggerAdapter(_LOGGER, {"device": ...})`）自己带值；
本模块的过滤器**只兜底**：

1. 格式串一旦含 `%(device)s`，任何**没有**该属性的 record 都会让 `Formatter`
   抛 `ValueError: Formatting field not found in record: 'device'`，而该异常被
   `Handler.handleError` 吞进 stderr —— **日志行直接丢失**。`LogDeviceFilter`
   把缺失的属性补成空串，这类记录就正常输出。
2. **绝不改写已有值**。`LoggerAdapter` 注入的记录必须原样通过，否则 `"[desk] "`
   会被二次包装成 `"[desk] [desk] "`；也绝不给记录再加包装/替换成 `"None"`。

为什么不仅挂在 root logger 上
------------------------------
`Logger.filter()` 只在"记录由**该** logger 直接产生"时被调用（`Logger.handle`），
**不参与 propagate**：子 logger（`cuktech_server` / `ble_manager` / `cuktech_sse` …）
产生的记录会绕过 root logger 的 `filters`，直接进 root 的 `handlers`。所以
`install_log_filter()` 同时挂 root logger（覆盖直接 `logging.info(...)` 的调用）
**和** root 现有每个 handler（覆盖全部子 logger）——只挂 root 的话，格式串里加了
`%(device)s` 之后绝大多数日志行会静默消失。

使用方（ha_server `_setup_logging`）在建好 handler 之后调用一次
`install_log_filter()` 即可；重复调用是幂等的（复用同一个过滤器实例，不加 handler）。
"""
import logging

from config import PRIMARY_DEVICE_ID

#: 格式串与 record 之间的属性名契约（`%(device)s`）。
DEVICE_ATTR = "device"

#: 过滤器实例名（便于日志/调试时辨认，不参与过滤语义）。
FILTER_NAME = "cuktech.device"


def device_tag(device_id: str | None) -> str:
    """多设备日志前缀：`"[desk] "`（含尾空格）；空/None/primary → `""`。

    返回值直接作为 `record.device`。空串是"单设备逐字节一致"的关键：格式串是
    `%(device)s%(message)s`，空串时既不留下空格也不留下空括号。
    primary（`default`）也不打标签，理由见模块 docstring。
    """
    if not device_id or device_id == PRIMARY_DEVICE_ID:
        return ""
    return f"[{device_id}] "


class LogDeviceFilter(logging.Filter):
    """只做一件事：给缺 `device` 的 record 补空串，**永远返回 True**。

    不做过滤（永不否决记录）、不改写已有值（`LoggerAdapter` 注入的原样通过）、
    不做二次装饰（`"[desk] "` 保持 `"[desk] "`）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, DEVICE_ATTR):
            setattr(record, DEVICE_ATTR, "")
        return True


def _find_filter(owner) -> LogDeviceFilter | None:
    """在 logger/handler 的 filters 里找已安装的本过滤器（幂等安装的判据）。"""
    for f in getattr(owner, "filters", ()):
        if isinstance(f, LogDeviceFilter):
            return f
    return None


def install_log_filter() -> LogDeviceFilter:
    """把 `LogDeviceFilter` 幂等安装到 root 与 root 当前所有 handler 上。

    - 已装过 → 复用**同一个实例**，不重复添加（既不加 handler 也不加 filter）；
    - root 后又新增了 handler → 再调一次会补挂到新 handler 上（对已有 handler 无副作用）。

    返回实际使用的过滤器实例（测试与调用方都可据此断言"只有一个"）。
    """
    root = logging.getLogger()
    flt = _find_filter(root)
    if flt is None:
        flt = LogDeviceFilter(FILTER_NAME)
        root.addFilter(flt)
    for handler in root.handlers:
        if _find_filter(handler) is None:
            handler.addFilter(flt)
    return flt
