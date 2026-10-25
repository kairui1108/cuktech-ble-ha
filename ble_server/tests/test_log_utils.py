"""`log_utils.LogDeviceFilter` 的契约测试（§3.13 per-device 日志标签）。

被锁死的四条契约（协作者指定，改动即破坏单设备逐字节一致性）：

1. 没有 `device` 属性的 record：输出里不出现 `[]`、不出现 `None`；
2. 带 `device` 的 record：输出里出现 `[desk] `（含尾空格），且**只包装一次**；
3. 安装两次不重复加 handler / 不重复加 filter（幂等）；
4. 单设备一行日志与"未安装 filter"下的同一格式输出**逐字节相同**。

另加一条（本模块发现的真实陷阱）：`Logger.filter` 不参与 propagate，子 logger 的记录
**不经过** root logger 的 filters，只经过 root 的 handlers —— 所以过滤器必须同时挂在
handler 上，否则加了 `%(device)s` 之后子 logger 的日志行会静默丢失。
"""
import io
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from log_utils import (  # noqa: E402
    DEVICE_ATTR, LogDeviceFilter, device_tag, install_log_filter,
)

# ha_server `_setup_logging` 改造后的格式串（`device` 槽位在 message 之前、无分隔符）
NEW_FMT = "%(asctime)s %(name)s %(levelname)s %(device)s%(message)s"
# 改造前的生产格式串（与 NEW_FMT 逐字符相同，只是没有 device 槽位）——
# 单设备必须与它逐字节一致：`levelname` 后面那个空格属于格式串，device 槽位是纯增量的。
OLD_FMT = "%(asctime)s %(name)s %(levelname)s %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"

# 固定 `created`，让 asctime 与运行时刻无关（不依赖 TZ：对比只在本进程内做）
CREATED = 1764000000.0


def _record(msg: str = "hello", name: str = "cuktech_server",
            level: int = logging.INFO) -> logging.LogRecord:
    rec = logging.LogRecord(name, level, "test_log_utils.py", 42, msg, (), None)
    rec.created = CREATED
    return rec


def _formatter(fmt: str) -> logging.Formatter:
    return logging.Formatter(fmt, DATE_FMT)


@pytest.fixture
def isolated_root():
    """把 root 换成单个 StringIO handler，退出时原样还原。

    过滤器是"装在 root 上"的，测它就必须接管 root；但 pytest 自己的 caplog handler
    也挂在 root 上，所以保存/还原是必须的，不能直接 `clear()`。
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = list(root.filters)
    saved_level = root.level
    saved_raise = logging.raiseExceptions
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_formatter(NEW_FMT))
    root.handlers[:] = [handler]
    root.filters[:] = []
    root.setLevel(logging.INFO)
    try:
        yield stream, handler
    finally:
        root.handlers[:] = saved_handlers
        root.filters[:] = saved_filters
        root.setLevel(saved_level)
        logging.raiseExceptions = saved_raise


@pytest.fixture
def child_logger():
    """一棵干净的子 logger（走 propagate → root handler，模拟真实日志路径）。"""
    log = logging.getLogger("cuktech_test_child")
    log.setLevel(logging.INFO)
    log.propagate = True
    log.handlers[:] = []
    log.filters[:] = []
    try:
        yield log
    finally:
        log.handlers[:] = []
        log.filters[:] = []
        log.propagate = True
        logging.Logger.manager.loggerDict.pop("cuktech_test_child", None)


class TestFilterContract:
    """①②④：record 层面的契约（不经 handler，直接看格式化结果）。"""

    def test_missing_device_renders_without_brackets_or_none(self):
        """① 无 device 属性 → 补空串；输出里不出现 `[]`，也不出现 `None`。"""
        flt = LogDeviceFilter()
        rec = _record()
        assert not hasattr(rec, DEVICE_ATTR)

        assert flt.filter(rec) is True                 # 永不否决记录
        assert rec.device == ""                        # 补的是空串，不是 None

        line = _formatter(NEW_FMT).format(rec)
        assert "[]" not in line
        assert "None" not in line
        assert line.endswith(" INFO hello")            # 槽位不留下多余空格

    def test_record_with_device_keeps_tag_and_is_not_double_wrapped(self):
        """② 带 device 的 record → 输出含 `[desk] `，且只包装一次。"""
        flt = LogDeviceFilter()
        rec = _record("hello")
        rec.device = device_tag("desk")
        assert rec.device == "[desk] "

        assert flt.filter(rec) is True
        assert rec.device == "[desk] "                 # 已有值原样通过，不二次包装

        line = _formatter(NEW_FMT).format(rec)
        assert "[desk] hello" in line                  # 含尾空格，紧跟 message
        assert line.count("[desk]") == 1

    def test_device_tag_is_empty_for_blank_ids(self):
        """`device_tag` 是 `""`/`None`/primary → 空串（单设备零字节痕迹的来源）。"""
        assert device_tag("") == ""
        assert device_tag(None) == ""
        # primary（兼容层锚定的 `default`）不打标签：单设备每行日志必须逐字节不变
        assert device_tag("default") == ""
        assert device_tag("desk") == "[desk] "

    def test_single_device_line_is_byte_identical_to_unfiltered(self):
        """④ 单设备（无 device）一行 → 与"未安装 filter 的旧格式串"逐字节相同。"""
        flt = LogDeviceFilter()
        rec_new = _record("端口 1 已启用")
        assert flt.filter(rec_new) is True
        filtered_line = _formatter(NEW_FMT).format(rec_new)

        # 旧格式串 + 完全不过过滤器的 record：这就是改造前的生产输出
        unfiltered_line = _formatter(OLD_FMT).format(_record("端口 1 已启用"))

        assert filtered_line == unfiltered_line

    def test_multi_device_line_equals_same_line_without_slot(self):
        """带标签的行 == 旧格式串下"message 自己带前缀"的行（等价性，不是相似性）。"""
        flt = LogDeviceFilter()
        rec = _record("已连接")
        rec.device = device_tag("desk")
        flt.filter(rec)
        assert _formatter(NEW_FMT).format(rec) == _formatter(OLD_FMT).format(_record("[desk] 已连接"))


class TestInstall:
    """③ 幂等安装；附带的 propagate 陷阱回归。"""

    def test_install_twice_does_not_duplicate_handlers_or_filters(self, isolated_root):
        """③ 安装两次：handler 数量不变，filter 仍是同一个实例且只有一份。"""
        _, handler = isolated_root
        root = logging.getLogger()

        first = install_log_filter()
        handlers_after_first = list(root.handlers)
        assert len(root.filters) == 1
        assert sum(isinstance(f, LogDeviceFilter) for f in handler.filters) == 1

        second = install_log_filter()
        third = install_log_filter()

        assert first is second is third                # 复用同一实例
        assert root.handlers == handlers_after_first   # 不加 handler
        assert len(root.filters) == 1                  # 不加第二份 filter
        assert sum(isinstance(f, LogDeviceFilter) for f in handler.filters) == 1
        assert handler.filters[0] is first

    def test_late_added_handler_gets_the_same_filter(self, isolated_root):
        """root 上新加的 handler（如 `_setup_logging` 的 RotatingFileHandler）补挂同一实例。"""
        root = logging.getLogger()
        flt = install_log_filter()

        late = logging.StreamHandler(io.StringIO())
        late.setFormatter(_formatter(NEW_FMT))
        root.addHandler(late)
        try:
            assert install_log_filter() is flt
            assert [f for f in late.filters if isinstance(f, LogDeviceFilter)] == [flt]
        finally:
            root.removeHandler(late)

    def test_child_logger_lines_survive_the_device_slot(self, isolated_root, child_logger):
        """子 logger 的记录必须能活到 handler（filters 不参与 propagate 的陷阱）。

        未安装过滤器时，root handler 的 `%(device)s` 找不到属性 → `Formatter` 抛
        `ValueError`，被 `Handler.handleError` 吞掉 → **整行日志丢失**。安装后正常。
        """
        stream, _ = isolated_root
        logging.raiseExceptions = False                # 隐藏 handleError 的 traceback 噪声

        child_logger.info("before filter")
        assert stream.getvalue() == ""                 # 行被吞掉了（就是没有过滤器时的现状）

        install_log_filter()
        child_logger.info("after %s", "filter")
        text = stream.getvalue()
        assert "after filter" in text
        assert "[]" not in text and "None" not in text

    def test_adapter_tagged_line_arrives_once(self, isolated_root, child_logger):
        """`LoggerAdapter(_LOGGER, {"device": device_tag("desk")})` 的端到端形态。"""
        stream, _ = isolated_root
        install_log_filter()
        log = logging.LoggerAdapter(child_logger, {DEVICE_ATTR: device_tag("desk")})
        log.info("已连接")
        text = stream.getvalue()
        assert text.count("[desk]") == 1
        assert "INFO [desk] 已连接" in text


class TestProductionWiring:
    """生产接线：`ha_server._setup_logging` 的格式串与过滤器（不是 local 试验品）。"""

    def test_setup_logging_adds_slot_and_installs_filter(self):
        """格式串含 `%(device)s`，且每个带该槽位的 handler 都挂了过滤器。"""
        import ha_server  # noqa: F401 —— 导入即执行 `_setup_logging()`

        root = logging.getLogger()
        slotted = [h for h in root.handlers
                   if h.formatter and "%(device)s" in (h.formatter._fmt or "")]
        assert slotted, "生产日志格式串里必须有 device 槽位"
        for handler in slotted:
            assert any(isinstance(f, LogDeviceFilter) for f in handler.filters), \
                "带 device 槽位的 handler 没挂过滤器 → 未带标签的日志行会被丢弃"
        assert any(isinstance(f, LogDeviceFilter) for f in root.filters)
        # 幂等：再装一次不改变任何东西
        assert install_log_filter() is install_log_filter()

    def test_blank_message_produces_no_trailing_garbage(self):
        """槽位为空时整行只多一个字节都不允许（`levelname` 后紧接 message）。"""
        rec = _record("x")
        LogDeviceFilter().filter(rec)
        line = _formatter(NEW_FMT).format(rec)
        assert line == _formatter(OLD_FMT).format(_record("x"))
