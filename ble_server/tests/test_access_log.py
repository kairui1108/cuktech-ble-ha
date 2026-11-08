"""访问日志默认关闭（`aiohttp.access` → WARNING）。

背景：aiohttp 的访问日志固定 INFO、每请求一行，实测占生产日志行数 ~2/3（约
8 MB/天）。业务事件由各 logger 自己记录，访问日志默认没有信息增量；排查请求时
用 `CUKTECH_ACCESS_LOG=1` 临时开回 INFO。

两条契约锁死：
1. 默认不产出访问日志（否则日志体积会悄悄翻回去）；
2. 环境变量能重新打开（否则线上排查时没有开关）。
"""
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ha_server  # noqa: E402

ACCESS = "aiohttp.access"


@pytest.fixture(autouse=True)
def restore_levels():
    """`_setup_logging` 会改 root 与 aiohttp.access 的级别，测试后还原。"""
    access = logging.getLogger(ACCESS)
    root = logging.getLogger()
    saved_access, saved_root = access.level, root.level
    try:
        yield
    finally:
        access.setLevel(saved_access)
        root.setLevel(saved_root)


def test_access_log_off_by_default(monkeypatch):
    monkeypatch.delenv("CUKTECH_ACCESS_LOG", raising=False)
    ha_server._setup_logging()
    level = logging.getLogger(ACCESS).level
    assert level == logging.WARNING
    # WARNING 下 aiohttp 的 INFO 访问日志即使被调用也不会落盘
    assert not logging.getLogger(ACCESS).isEnabledFor(logging.INFO)


@pytest.mark.parametrize("value", ["1", "true", "yes", "info", "INFO"])
def test_access_log_can_be_enabled_by_env(monkeypatch, value):
    monkeypatch.setenv("CUKTECH_ACCESS_LOG", value)
    ha_server._setup_logging()
    assert logging.getLogger(ACCESS).isEnabledFor(logging.INFO)


def test_root_level_change_does_not_revive_access_log(monkeypatch):
    """`/api/log-level` 调的是 root；显式子级别必须仍然压住访问日志。"""
    monkeypatch.delenv("CUKTECH_ACCESS_LOG", raising=False)
    ha_server._setup_logging()
    logging.getLogger().setLevel(logging.DEBUG)
    assert not logging.getLogger(ACCESS).isEnabledFor(logging.INFO)
