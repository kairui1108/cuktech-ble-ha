"""FlatBuffers 编码器 - 匹配 Xiaomi BLE Spec 协议.

sae 类方法映射:
  OooOOO(13)    = startObject(13 fields)
  OooO0o0(f, b) = addByte(field, value)     ← 存1字节，跳过0
  OooO0o(f, i)  = addInt(field, value)      ← 存4字节int，跳过0
  OooO0oo(f, L) = addLong(field, value)     ← 存8字节long，跳过0
  OooOOO0()     = endObject → 返回 root_offset
  OooOOOo(sz)   = finish(sz) (在 eee 调用后会调用)
  OooOOo()      = sizedByteArray → 返回完整 buffer

BLE Spec 消息格式 (eee.OooO00o):
  field 0: opcode (byte)   - 0x04 = SET, 需要确认 GET 的值
  field 1: siid (int)      - 服务ID
  field 2: piid (int)      - 属性ID
  field 3: flag (byte)     - 标志位 (来源: Build.VERSION.SDK_INT)
  field 4: value (int)     - 属性值 (主要值)
  field 5-7: extra values  - 扩展值部分 (通常为0，跳过)
  field 8: did (long)      - 设备ID (固定64位)
  field 9-12: extra params - 额外参数 (通常为0，跳过)

FlatBuffers 内存布局:
  [table_data] [vtable_offset:4B] [vtable]
  vtable = [size:2][table_size:2][field_offsets:2*N]
"""
import struct


class FlatBuf:
    """FlatBuffers 编码器，与 sae 类行为一致."""

    def __init__(self):
        self.data = bytearray()   # 反向构建的数据区
        self.fields = {}          # {field_id: offset_from_table_start}

    def add_byte(self, fid: int, val: int) -> None:
        """OooO0o0: 添加 1 字节字段，0 值跳过."""
        if val == 0:
            return
        off = len(self.data)
        self.data.append(val & 0xFF)
        self.fields[fid] = off

    def add_int(self, fid: int, val: int) -> None:
        """OooO0o: 添加 4 字节 int 字段，0 值跳过."""
        if val == 0:
            return
        off = len(self.data)
        self.data.extend(struct.pack('<I', val & 0xFFFFFFFF))
        self.fields[fid] = off

    def add_long(self, fid: int, val: int) -> None:
        """OooO0oo: 添加 8 字节 long 字段，0 值跳过."""
        if val == 0:
            return
        off = len(self.data)
        self.data.extend(struct.pack('<Q', val & 0xFFFFFFFFFFFFFFFF))
        self.fields[fid] = off

    def build(self, num_fields: int) -> bytes:
        """构建完整 FlatBuffers buffer (endObject + finish)."""
        table_data_len = len(self.data)
        table_size = table_data_len + 4  # +4 for vtable_offset 字段

        if not self.fields:
            return struct.pack('<I', 4) + struct.pack('<HH', 4, table_size)

        max_fid = max(self.fields.keys())
        vtable_entries = max_fid + 1
        vtable_size = 4 + vtable_entries * 2

        vtable = bytearray()
        vtable.extend(struct.pack('<H', vtable_size))
        vtable.extend(struct.pack('<H', table_size))

        for fid in range(vtable_entries):
            if fid in self.fields:
                field_off = table_size - self.fields[fid]
                vtable.extend(struct.pack('<H', field_off))
            else:
                vtable.extend(struct.pack('<H', 0))

        vtable_offset = len(vtable)

        final = bytearray(self.data)
        final.extend(struct.pack('<I', vtable_offset))
        final.extend(vtable)

        return bytes(final)


def encode_set_property(
    siid: int = 2,
    piid: int = 21,
    value: int = 0,
    flag: int = 0,
    did: int = 0,
) -> bytes:
    """编码 BLE Spec SET 属性消息 (opcode=0x04).

    Args:
        siid: 服务 ID (默认 2 = charger service)
        piid: 属性 ID (如 21 = protocol_ctl_extend)
        value: 属性值 (32-bit)
        flag: 标志位 (Android SDK_INT 或 0)
        did: 设备 ID (64-bit, 通常为0)

    Returns:
        FlatBuffers 编码的消息字节
    """
    b = FlatBuf()
    b.add_int(12, 0)      # placeholder
    b.add_int(11, 0)
    b.add_int(10, 0)
    b.add_int(9, 0)
    b.add_long(8, did)
    b.add_int(7, 0)
    b.add_int(6, 0)
    b.add_int(5, 0)
    b.add_int(4, value)
    b.add_int(2, piid)
    b.add_int(1, siid)
    b.add_byte(3, flag)
    b.add_byte(0, 4)      # opcode = 0x04 = SET
    return b.build(13)


def encode_get_property(
    siid: int = 2,
    piid: int = 21,
    flag: int = 0,
    did: int = 0,
) -> bytes:
    """编码 BLE Spec GET 属性消息 (opcode=0x02? 待确认).

    注意：GET opcode 尚未从 smali 确认，使用 0x02 (MiOT GET 相同值)作为推测。
    """
    b = FlatBuf()
    b.add_int(12, 0)
    b.add_int(11, 0)
    b.add_int(10, 0)
    b.add_int(9, 0)
    b.add_long(8, did)
    b.add_int(7, 0)
    b.add_int(6, 0)
    b.add_int(5, 0)
    b.add_int(4, 0)
    b.add_int(2, piid)
    b.add_int(1, siid)
    b.add_byte(3, flag)
    b.add_byte(0, 2)      # opcode = 0x02 = GET (推测)
    return b.build(13)
