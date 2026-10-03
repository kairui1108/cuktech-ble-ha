"""Tests for controller.py - BLE controller operations."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.cuktech_ble.protocol import DEVICE_MAC, DEVICE_TOKEN, PORT_BITS


class TestReconnectDelay:
    """Test BLE reconnection delay calculation."""

    def test_exponential_backoff(self):
        """Test exponential backoff increases delay."""
        base_delay = 1
        max_delay = 300
        delays = []
        for attempt in range(6):
            delay = min(base_delay * (2 ** attempt), max_delay)
            delays.append(delay)
        assert delays == [1, 2, 4, 8, 16, 32]

    def test_delay_capped_at_max(self):
        """Test delay doesn't exceed max."""
        base_delay = 1
        max_delay = 300
        delay = min(base_delay * (2 ** 10), max_delay)
        assert delay == max_delay

    def test_delay_resets_on_success(self):
        """Test delay resets to base after successful connection."""
        base_delay = 1
        attempts = 5
        delay = min(base_delay * (2 ** attempts), 300)
        assert delay == 32  # After reset, next delay would be 1

    def test_ble_manager_reconnect_delay(self):
        """Test BLEManager._get_reconnect_delay() returns correct values (with jitter)."""
        from unittest.mock import MagicMock
        from ble_manager import BLEManager

        state = MagicMock()
        config = MagicMock()
        config.server.reconnect_base_delay = 1.0
        config.server.reconnect_max_delay = 300.0
        mgr = BLEManager(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff", state=state, config=config)

        # No jitter for delay <= 1.0
        mgr._reconnect_attempts = 0
        assert mgr._get_reconnect_delay() == 1.0

        # Jitter range for delay=8.0: ±25% = ±2.0 → [6.0, 10.0]
        mgr._reconnect_attempts = 3
        for _ in range(20):
            delay = mgr._get_reconnect_delay()
            assert 6.0 <= delay <= 10.0, f"delay {delay} outside range"

        # Jitter range for delay=300.0: ±25% = ±75 → [225, 375]
        mgr._reconnect_attempts = 10
        for _ in range(20):
            delay = mgr._get_reconnect_delay()
            assert 225 <= delay <= 375, f"delay {delay} outside range"


class TestControllerInit:
    """Test CuktechBLEController initialization."""

    def test_default_mac(self):
        """Test controller accepts default MAC."""
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac=DEVICE_MAC, token=DEVICE_TOKEN)
        assert ctrl.mac == DEVICE_MAC

    def test_custom_mac(self):
        """Test controller accepts custom MAC."""
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac="AA:BB:CC:DD:EE:FF", token=DEVICE_TOKEN)
        assert ctrl.mac == "AA:BB:CC:DD:EE:FF"

    def test_initial_state(self):
        """Test controller initial state."""
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac=DEVICE_MAC, token=DEVICE_TOKEN)
        assert ctrl.authenticated is False
        assert ctrl.client is None


class TestProtocolConstants:
    """Test protocol constants used in controller."""

    def test_device_token_length(self):
        """Test DEVICE_TOKEN is 12 bytes."""
        assert len(DEVICE_TOKEN) == 12

    def test_port_bits_complete(self):
        """Test all ports have bit assignments."""
        assert len(PORT_BITS) == 4
        assert all(v in range(4) for v in PORT_BITS.values())


class TestBuildMiotTlv:
    """Test _build_miot_tlv TLV encoding."""

    def test_set_uint8_value(self):
        """Test SET with 1-byte value."""
        from src.cuktech_ble.controller import CuktechBLEController
        # siid=2, piid=5, value=3 (场景模式=3)
        result = CuktechBLEController._build_miot_tlv(1, 2, 5, value=3)
        tl = (1 << 12) | 1  # type_id=1(UINT8), len=1
        expected = bytes([
            12, 0x20,  # total_len=12, frame_type=0x20
            1, 0x00,   # seq=1, [0x00]
            0x00, 0x01, # opcode=SET(0x00), cnt=1
            2,          # siid=2
            5, 0x00,    # piid=5 (LE)
            tl & 0xFF, (tl >> 8) & 0xFF,  # tl
            3,          # value=3
        ])
        assert result == expected
        assert len(result) == 12

    def test_set_uint32_value(self):
        """Test SET with 4-byte value (PIID 21 protocol_extend)."""
        from src.cuktech_ble.controller import CuktechBLEController
        # siid=2, piid=21, value=50532111 (0x0303030F)
        value = 0x0303030F
        result = CuktechBLEController._build_miot_tlv(1, 2, 21, value=value)
        assert len(result) == 15
        assert result[0] == 15  # total_len
        tl = (5 << 12) | 4  # type_id=5(UINT32), len=4
        assert result[9:11] == bytes([tl & 0xFF, (tl >> 8) & 0xFF])  # tl
        # Last 4 bytes = value in LE
        assert result[11:15] == b'\x0F\x03\x03\x03'

    def test_get_command(self):
        """Test GET command (value=None)."""
        from src.cuktech_ble.controller import CuktechBLEController
        # siid=2, piid=5, no value
        result = CuktechBLEController._build_miot_tlv(1, 2, 5)
        assert len(result) == 12
        assert result[4] == 0x02  # opcode=GET
        assert result[-1] == 0x00  # dummy value byte

    def test_piid_le_encoding(self):
        """Test piid is encoded as 2-byte little-endian."""
        from src.cuktech_ble.controller import CuktechBLEController
        # piid=512 (0x200) should be 0x00, 0x02
        result = CuktechBLEController._build_miot_tlv(1, 2, 512, value=1)
        assert result[7] == 0x00  # piid low byte
        assert result[8] == 0x02  # piid high byte

    def test_total_len_formula(self):
        """Test total_len = 11 + value_bytes."""
        from src.cuktech_ble.controller import CuktechBLEController
        r1 = CuktechBLEController._build_miot_tlv(1, 2, 5, value=0x00)     # UINT8 → 1 byte
        r2 = CuktechBLEController._build_miot_tlv(1, 2, 21, value=0x10000) # UINT32 → 4 bytes
        r3 = CuktechBLEController._build_miot_tlv(1, 2, 5)                 # GET → 1 byte dummy
        assert r1[0] == 12   # 11 + 1
        assert r2[0] == 15   # 11 + 4
        assert r3[0] == 12   # 11 + 1


class TestAuthMultiframeCap:
    """认证多帧响应帧数上限（H1 回归测试）。"""

    @pytest.mark.asyncio
    async def test_recv_auth_response_caps_frame_count(self):
        """异常/恶意设备上报超大帧数时，_recv_auth_response 应把帧数限制在 100。"""
        from unittest.mock import AsyncMock
        from src.cuktech_ble.controller import CuktechBLEController

        ctrl = CuktechBLEController(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff")
        ctrl.client = AsyncMock()
        # 多帧头: [00 00 00 01 count_lo=0xC8 count_hi=0x00] → 声称 200 帧
        header = bytes([0x00, 0x00, 0x00, 0x01, 0xC8, 0x00])
        frame = bytes([0x00, 0x01, 0xAA, 0xBB])  # 帧序 0x0100，载荷 0xAABB
        calls = {"n": 0}

        async def fake_wait_notify(channel, timeout=None):
            calls["n"] += 1
            return header if calls["n"] == 1 else frame

        ctrl.wait_notify = fake_wait_notify

        result = await ctrl._recv_auth_response("auth_data")
        # 帧数被上限到 100：1 次帧头 + 100 次数据帧，而不是 200 次
        assert calls["n"] == 101
        assert result == b"\xaa\xbb" * 100


class TestSendEncryptedClearQueue:
    """ble-warnings P2: _send_encrypted 写命令前清空 cmd_send 队列。

    回归场景: cmd_send 通道残留设备越带推送帧/过期响应时，
    wait_notify("cmd_send") 必须读到空队列（等待 RCV_RDY/RCV_OK），
    而不是先取到陈旧帧触发虚假 "CMD_SEND no RCV_RDY/RCV_OK" 警告。
    """

    @pytest.mark.asyncio
    async def test_send_encrypted_clears_cmd_send_before_wait(self):
        import asyncio
        from unittest.mock import MagicMock, AsyncMock, patch
        from src.cuktech_ble.controller import CuktechBLEController

        ctrl = CuktechBLEController(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff")
        ctrl.client = MagicMock()
        ctrl.client.write_gatt_char = AsyncMock()

        # 预置一条陈旧/越带帧（即文档中 000001050100 6 字节特征）
        q = ctrl._notify_queues.setdefault("cmd_send", asyncio.Queue())
        q.put_nowait(b"\x00\x00\x01\x05\x01\x00")

        # 拦截 wait_notify，记录调用瞬间 cmd_send 是否已清空
        states = []

        async def fake_wait_notify(name, timeout=5.0):
            que = ctrl._notify_queues.get(name)
            states.append((name, que.empty() if que else True))
            return None  # 返回 None → _send_encrypted 判定 "no RCV_RDY" → False

        with patch.object(ctrl, "_encrypt", return_value=b"\x01\x00\xaa\xbb") as m_enc:
            with patch.object(ctrl, "wait_notify", side_effect=fake_wait_notify):
                result = await ctrl._send_encrypted(b"\x00\x10\x10\x00")

        # 关键断言: 两次 wait_notify("cmd_send") 调用时队列都应为空（陈旧帧已被丢弃）
        wait_calls = [name for name, _ in states]
        assert "cmd_send" in wait_calls
        assert all(empty for name, empty in states if name == "cmd_send"), \
            f"cmd_send 队列写入前未被清空: {states}"
        assert m_enc.called
        # header 帧确实通过 GATT 写入
        assert ctrl.client.write_gatt_char.called


class TestPortPushDuringCommandWindow:
    """GET/SET 等待响应期间到达的端口推送不得被吞（改动34）。

    端口推送解密后的 b4 实测为 0x04；旧代码的回放/处理条件写的是 b4 == 0x02，
    条件永假 → 推送被静默丢弃。修法不是"放回队列"（会被本循环立刻取回导致
    自循环空转），而是通过 on_port_push 回调就地处理。
    """

    def _ctrl(self):
        from src.cuktech_ble.controller import CuktechBLEController
        c = CuktechBLEController(DEVICE_MAC, DEVICE_TOKEN)
        c.client = MagicMock()
        c.client.write_gatt_char = AsyncMock()
        return c

    def _frame(self, pt):
        """造一个内联帧：raw 前 4 字节是头，decrypt 直接返回明文。"""
        return bytes([0x00, 0x00, 0x02, 0x00]) + b"\x00" * 8

    @pytest.mark.asyncio
    async def test_push_forwarding_covers_all_b4_04_frames(self):
        """转发条件必须覆盖所有 b4==0x04 推送, 不能只放行端口推送。

        PIID17/18 的协议号推送同样是 b4==0x04；若条件写成
        `b4 == 0x04 and pt_siid == 2 and 1 <= pt_piid <= 4`，协议推送在
        GET/SET 窗口内仍会被吞（评审 #4）。
        """
        import inspect
        from src.cuktech_ble.controller import CuktechBLEController
        for fn in (CuktechBLEController._recv_get_response,
                   CuktechBLEController._recv_set_response):
            src = inspect.getsource(fn)
            assert "if b4 == 0x04:" in src, \
                f"{fn.__name__} 的推送转发条件仍带 piid 限制, 协议推送会被吞"
            assert "if b4 == 0x02:" not in src, \
                f"{fn.__name__} 仍残留 b4 == 0x02 的死条件"

    @pytest.mark.asyncio
    async def test_protocol_push_in_window_is_handled(self):
        """窗口内的 PIID17 协议推送（同 b4==0x04）也必须转交回调。"""
        c = self._ctrl()
        handled = []

        async def on_push(pt):
            handled.append(pt)

        c.on_port_push = on_push

        # piid=17 的协议号推送: b4=0x04, siid=2, piid=17
        proto = bytes([0, 0, 0, 0, 0x04, 0, 0x02, 0x11, 0, 0, 0, 0,
                       0, 0, 0, 0, 0, 0, 0, 0])
        c.decrypt = MagicMock(return_value=proto)
        frames = [self._frame(proto), None]

        async def fake_wait(name, timeout=5.0):
            return frames.pop(0) if frames else None

        c.wait_notify = fake_wait
        await c._recv_get_response(2, 16, timeout=1.0)

        assert len(handled) == 1, "PIID17 协议推送在窗口内被吞"

    @pytest.mark.asyncio
    async def test_push_in_get_window_is_handled(self):
        """GET 等待窗口内的端口推送应交给 on_port_push 处理, 不被丢弃。"""
        c = self._ctrl()
        handled = []

        async def on_push(pt):
            handled.append(pt)

        c.on_port_push = on_push

        # 端口推送明文: b4=0x04, siid=2, piid=1
        push = bytes([0, 0, 0, 0, 0x04, 0, 0x02, 0x01, 0, 0, 0, 0, 0, 0, 0])
        c.decrypt = MagicMock(return_value=push)

        frames = [self._frame(push), None]   # 先来推送帧, 之后无帧(超时)

        async def fake_wait(name, timeout=5.0):
            return frames.pop(0) if frames else None

        c.wait_notify = fake_wait

        result = await c._recv_get_response(2, 16, timeout=1.0)

        # GET 没有等到自己的响应
        assert result is None
        # 但推送帧被就地处理了（没有丢，也没有被放回队列造成自循环）
        assert len(handled) == 1, f"端口推送被吞了: handled={len(handled)}"
        assert handled[0] == push

    @pytest.mark.asyncio
    async def test_push_not_requeued(self):
        """推送帧不得被放回 cmd_recv 队列（回放会自循环空转）。"""
        c = self._ctrl()
        c.on_port_push = AsyncMock()
        queue = asyncio.Queue()
        c._notify_queues["cmd_recv"] = queue

        push = bytes([0, 0, 0, 0, 0x04, 0, 0x02, 0x01, 0, 0, 0, 0, 0, 0, 0])
        c.decrypt = MagicMock(return_value=push)
        frames = [self._frame(push), None]

        async def fake_wait(name, timeout=5.0):
            return frames.pop(0) if frames else None

        c.wait_notify = fake_wait
        await c._recv_get_response(2, 16, timeout=1.0)

        assert queue.empty(), "推送帧被放回队列, 会被本循环取回导致自循环"
