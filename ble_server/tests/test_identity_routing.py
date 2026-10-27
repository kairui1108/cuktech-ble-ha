"""§5.4 路由/装配身份断言 —— 锁死 P0-2 那个"setter 短路"陷阱。

核心风险（设计文档 §3.3）：`Server.ble`/`state`/`history` 是**只读 property**，
getter 里 `_xxx_assigned` 一旦非空就**永久短路**。生产 `__init__` 若仍写
`self.ble = BLEManager(...)`，setter 把 primary 塞进 `_ble_assigned`，于是 ContextVar
**永不生效**，所有 `/api/device/{id}/...` 请求静默落到 primary —— 不报错、不告警，
只是控错设备（安全事故），history 更会 16 处查询/写入全部串台。

四条断言（与 §5.4 逐条对应）：

1. 生产构造后 `server.ble is server.primary.manager`、`state`/`history` 同构；
2. `_ble_assigned`/`_state_assigned`/`_history_assigned` 三者均为 `None`
   （外加一条静态守卫：`Server.__init__` 源码里不许出现这三个赋值，防止运行时断言被删）；
3. 经**真** `device_middleware` 处理 `/api/device/desk/...` 期间，三者都指向
   `registry["desk"]`，且请求结束后上下文复原（回到 primary）；
4. 兜底：两个并发请求指向不同设备时互不串台（ContextVar 是 context 局部）。

测试用真 `Server` + 真 `device_middleware`（`golden_env` 提供确定性配置与替身）。
"""
import ast
import asyncio
import inspect
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, str(Path(__file__).parent))

from golden_harness import golden_env, mock_request  # noqa: E402

pytestmark = pytest.mark.asyncio

DESK_ID = "desk"
DESK_MAC = "AA:BB:CC:DD:EE:0A"
DESK_TOKEN = "ffeeddccbbaa99887766554433221100"

#: P0-2 禁令点名的三个属性（生产 `__init__` 绝不可赋值）
FORBIDDEN_INIT_ASSIGNMENTS = {"ble", "state", "history"}


def _desk() -> dict:
    return {"id": DESK_ID, "mac": DESK_MAC, "token": DESK_TOKEN}


@pytest.fixture
async def env():
    """N=2 的真装配（不开 startup：本模块只验路由/身份，不碰 BLE 主循环）。"""
    async with golden_env(devices=[_desk()], apply_startup=False) as e:
        yield e


def _assert_points_at(server, rt) -> None:
    assert server.ble is rt.manager, "server.ble 没有解析到该设备的 manager"
    assert server.state is rt.state, "server.state 没有解析到该设备的状态"
    assert server.history is rt.history, "server.history 没有解析到该设备的 history facade"


class TestProductionConstruction:
    async def test_properties_resolve_to_primary(self, env):
        """① 无请求上下文 → 三者都是 primary 的运行期资源。"""
        server = env.server
        _assert_points_at(server, server.primary)
        assert server.primary.device_id == "default"

    async def test_assignment_slots_are_all_empty(self, env):
        """② 生产构造后三个槽位必须为空（否则 ContextVar 已失效）。"""
        server = env.server
        assert server._ble_assigned is None
        assert server._state_assigned is None
        assert server._history_assigned is None

    async def test_production_init_never_assigns_the_three_properties(self):
        """②（静态守卫）`Server.__init__` 源码里不许出现 `self.ble/state/history = ...`。

        运行时断言（`_assert_no_assignment`）理论上可以被删掉；这条直接读源码，
        删了断言也拦得住。允许 `self._ble_assigned = None` 这类初始化。
        """
        import ha_server

        tree = ast.parse(inspect.getsource(ha_server.Server.__init__).lstrip())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and target.attr in FORBIDDEN_INIT_ASSIGNMENTS):
                    offenders.append(f"self.{target.attr} = ... (第 {node.lineno} 行)")
        assert not offenders, (
            "生产 __init__ 给只读 property 赋了值（P0-2 禁令）：" + ", ".join(offenders))


class TestMiddlewareRouting:
    async def test_device_path_resolves_inside_handler(self, env):
        """③ `/api/device/desk/...` 处理期间三者都指向 `registry["desk"]`。"""
        import ha_server

        server = env.server
        desk_rt = server.registry[DESK_ID]
        seen = {}

        async def handler(request):
            # handler 内部只看 property —— 与真实 handler 的写法一致（零显式传参）
            seen["device_id"] = server.current_device_id()
            seen["ble"] = server.ble
            seen["state"] = server.state
            seen["history"] = server.history
            return web.Response(text="ok")

        request = make_mocked_request(
            "GET", f"/api/device/{DESK_ID}/status", match_info={"device_id": DESK_ID})
        resp = await ha_server.device_middleware(request, handler)

        assert resp.status == 200
        assert seen["device_id"] == DESK_ID
        # 关键身份断言：不是"值相等"，是**同一个对象**
        assert seen["ble"] is desk_rt.manager
        assert seen["state"] is desk_rt.state
        assert seen["history"] is desk_rt.history
        assert seen["ble"] is not server.primary.manager

    async def test_context_is_restored_after_the_request(self, env):
        """中间件退出即复原：请求外仍解析到 primary（无泄漏的 ContextVar）。"""
        import ha_server

        server = env.server

        async def handler(request):
            return web.Response(text="ok")

        request = make_mocked_request(
            "GET", f"/api/device/{DESK_ID}/status", match_info={"device_id": DESK_ID})
        await ha_server.device_middleware(request, handler)

        _assert_points_at(server, server.primary)
        assert server.current_device_id() == "default"

    async def test_query_param_addressing(self, env):
        """`?device=` 寻址（Web UI 用）与路径寻址等价。"""
        import ha_server

        server = env.server
        desk_rt = server.registry[DESK_ID]
        seen = {}

        async def handler(request):
            seen["ble"] = server.ble
            return web.Response(text="ok")

        request = mock_request("GET", f"/api/status?device={DESK_ID}")
        await ha_server.device_middleware(request, handler)
        assert seen["ble"] is desk_rt.manager

    async def test_unknown_device_is_404_and_handler_never_runs(self, env):
        """未知名 → 404 `device_not_found`，handler 不执行（不能静默落 primary）。"""
        import ha_server

        called = []

        async def handler(request):
            called.append(True)
            return web.Response(text="ok")

        request = make_mocked_request(
            "GET", "/api/device/ghost/status", match_info={"device_id": "ghost"})
        resp = await ha_server.device_middleware(request, handler)

        assert resp.status == 404
        assert b"device_not_found" in resp.body
        assert called == []

    async def test_concurrent_requests_do_not_cross_devices(self, env):
        """④ 两个并发请求各持自己的设备上下文，await 交错也不串台（§5.3 门槛的性质）。"""
        import ha_server

        server = env.server
        desk_rt = server.registry[DESK_ID]
        primary_rt = server.primary
        observed: dict[str, list] = {"default": [], "desk": []}

        def make_handler(expected_id: str):
            async def handler(request):
                # 故意在"取到属性之后"让出事件循环：交错点正是事故现场
                await asyncio.sleep(0)
                observed[expected_id].append((
                    server.current_device_id(), server.ble, server.state, server.history))
                await asyncio.sleep(0)
                observed[expected_id].append((
                    server.current_device_id(), server.ble, server.state, server.history))
                return web.Response(text=expected_id)
            return handler

        req_desk = make_mocked_request(
            "GET", f"/api/device/{DESK_ID}/port", match_info={"device_id": DESK_ID})
        req_primary = make_mocked_request("GET", "/api/port")

        await asyncio.gather(
            ha_server.device_middleware(req_desk, make_handler(DESK_ID)),
            ha_server.device_middleware(req_primary, make_handler("default")),
        )

        for device_id, rt in ((DESK_ID, desk_rt), ("default", primary_rt)):
            samples = observed[device_id]
            assert len(samples) == 2
            for seen_id, ble, state, history in samples:
                assert seen_id == device_id
                assert ble is rt.manager
                assert state is rt.state
                assert history is rt.history
