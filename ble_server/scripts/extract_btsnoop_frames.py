#!/usr/bin/env python3
"""
extract_btsnoop_frames.py -- recover real BLE notification frames from btsnoop HCI captures.

Part of the CUKTECH charger BLE test tooling: it decodes the captures in
``docs/hci_log/`` down to the exact bytes the server receives on a GATT notification, so
recorded over-the-air traffic can be replayed as fixtures (see
``tests/fixtures/README.md`` for the fixture schema and provenance rules).

Decode path
-----------
    btsnoop record -> H4 (HCI UART) packet -> HCI ACL Data
                   -> L2CAP (reassemble fragments, demux CID 0x0004)
                   -> ATT PDU (0x1B Handle Value Notification / 0x1D Indication)
                   -> notification value  ==  one fixture frame

What one "frame" is
------------------
The fixture unit is the **ATT value bytes of one notification PDU** -- the payload after
stripping the 3-byte ATT header (opcode + 2-byte little-endian handle), with no HCI or
L2CAP framing. That is precisely what bleak hands the server, matching the dispatch at
``ble_manager.py:1802-1810`` (switch on ``data[2]``: 0x02 -> ``_handle_inline_data``,
0x00 -> ``_handle_multiframe``, else ``_try_process_inline_frame``). The 4-byte frame
header is kept; ``controller._try_decode_inline`` strips ``data[:4]`` itself.

Observed on-wire layout (verified against the decoders and against the captures)::

    inline frame       [ctr_lo ctr_hi][0x02][msg_type][it_lo it_hi][AES-CCM ciphertext||tag4]
                       \_______ 4-byte frame header ______/\________ encrypted envelope ______/
    multiframe header  [ctr_lo ctr_hi][0x00][data_id][count_lo count_hi]
    multiframe subseq  [frm_lo frm_hi][ciphertext chunk]      (concatenate chunks, decrypt ONCE)

``it`` is the device's incrementing message counter; ``data[0:2]`` is *not* the it (in
these captures it stays 0x0000 while the envelope ``it`` at ``data[4:6]`` counts 1, 2, 3...).

Encryption
----------
Everything after the frame header is MiOT AES-CCM (4-byte tag). Session keys are not in
the capture, but they are *recoverable* from it, because the login handshake crosses the
auth characteristic in clear text::

    app_random  = ATT Write Command to auth_data, value == 0x01 0x00 + random(16)
    dev_random  = auth_data notification, frame header byte 2 == 0x02, value[4:2]
    derived     = HKDF-SHA256(ikm=<12-byte device token>, salt=app_random||dev_random,
                              info=b"mible-login-info", length=64)
    dev_key=derived[0:16]  app_key=derived[16:32]  dev_iv=derived[32:36]  app_iv=derived[36:40]
    nonce       = dev_iv + b"\\x00"*4 + uint32_le(it)          (with 16-bit wrap tracking)

``--decrypt`` does that and then runs the ciphertext through the **production**
``CuktechBLEController.decrypt()`` -- the tool never re-implements the cipher, so a frame
that decrypts here decrypts identically in the server. ``--verify-handshake`` checks the
derivation first without needing any frame to decode: it recomputes the app login HMAC
``HMAC-SHA256(app_key, salt)`` and compares it with the 32-byte HMAC the app actually
wrote on the wire, which proves the token + randoms are the right ones.

Sniffer quirks these captures contain (all handled; do not "simplify" them away)
-------------------------------------------------------------------------------
* Packet_Boundary bits are unreliable -- 0b00, 0b01 and 0b10 all appear on records that
  are complete. Reassembly is therefore driven by the declared L2CAP length, not by PB.
* Long notifications really are split: e.g. one record announces L2CAP length 28 while
  carrying 23 bytes, and a later record supplies the 5-byte tail. Reading either record
  alone gives an ATT PDU whose CCM tag cannot verify, which looks exactly like "wrong
  keys". This is why naive extractors reported fewer frames than exist.
* The ESP32 relay channel (connection handle 0x2edc, L2CAP CID 0x02ff, ~500-byte records
  of 0xf2 padding) is the sniffer's own uplink and carries no ATT. In
  ``btsnoop_hci_old.log`` it is the *only* thing recorded, so that file has zero
  notifications.
* Timestamps are documented as microseconds since 2000-01-01, but the sniffer's epoch is
  wrong: those values land in year 4026 (btsnoop-spec epoch) / 3996 (Unix epoch). Absolute
  wall-clock is therefore unusable; ordering and deltas inside a capture -- and *between*
  captures, which match the files' mtimes -- are correct, and ``--fixtures`` anchors
  ``captured_at`` on each capture's mtime for that reason.

Usage
-----
    python3 scripts/extract_btsnoop_frames.py --survey  ../docs/hci_log/btsnoop_hci.log
    python3 scripts/extract_btsnoop_frames.py --summary --dump ../docs/hci_log/*.log
    python3 scripts/extract_btsnoop_frames.py --verify-handshake --token-hex <24hex> FILE
    python3 scripts/extract_btsnoop_frames.py --decrypt --token-hex <24hex> --jsonl out.jsonl FILE...
    python3 scripts/extract_btsnoop_frames.py --decrypt --token-hex <24hex> \
            --fixtures tests/fixtures/notify_frames.json ../docs/hci_log/btsnoop_{hci,c2pd,c2pd2,c2pd_on}.log

Read-only with respect to the captures and to production code; the only writes are the
explicit ``--jsonl`` / ``--fixtures`` outputs. Secrets are never printed: token, session
keys and randoms are used in memory and reported as "present/verified" only.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

BTSNOOP_MAGIC = b"btsnoop\x00"
BTSNOOP_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)  # per spec; sniffer clock differs

DLT_H4, DLT_H4_ALT, DLT_LL = 1001, 1002, 1003
H4_COMMAND, H4_ACL, H4_SCO, H4_EVENT, H4_ISO = 0x01, 0x02, 0x03, 0x04, 0x05

L2CAP_CID_ATT = 0x0004
L2CAP_CID_SIGNALLING = 0x0005
L2CAP_CID_LE_SIGNALLING = 0x0006
DYNAMIC_CID_MIN = 0x0040
MAX_SDU = 1024                      # ATT MTU ceiling; bounds any bogus declared length

ATT_ERROR_RESPONSE = 0x01
ATT_FIND_INFO_RSP = 0x05
ATT_READ_BY_GROUP_TYPE_REQ = 0x08
ATT_READ_BY_GROUP_TYPE_RSP = 0x09
ATT_READ_BY_TYPE_REQ = 0x0A
ATT_READ_BY_TYPE_RSP = 0x0B
ATT_READ_REQ = 0x0C
ATT_WRITE_REQ = 0x12
ATT_WRITE_CMD = 0x52          # Write Command (no response); long form, 2-byte handle
ATT_HANDLE_VALUE_NOTIF = 0x1B
ATT_HANDLE_VALUE_IND = 0x1D
ATT_HANDLE_VALUE_CONF = 0x1E

NOTIFY_OPCODES = (ATT_HANDLE_VALUE_NOTIF, ATT_HANDLE_VALUE_IND)

ATT_OPCODE_NAMES = {
    0x01: "Error Response", 0x02: "Exchange MTU Req", 0x03: "Exchange MTU Rsp",
    0x04: "Find Info Req", 0x05: "Find Info Rsp", 0x06: "Find By Type Req",
    0x07: "Find By Type Rsp", 0x08: "Read By Group Type Req",
    0x09: "Read By Group Type Rsp", 0x0A: "Read By Type Req",
    0x0B: "Read By Type Rsp", 0x0C: "Read Req", 0x0D: "Read Rsp",
    0x12: "Write Req", 0x13: "Write Rsp", 0x16: "Prepare Write Req",
    0x1B: "Handle Value Notification", 0x1D: "Handle Value Indication",
    0x1E: "Handle Value Confirmation", 0x52: "Write Command",
}

UUID_PRIMARY_SERVICE = 0x2800
UUID_CHARACTERISTIC_DECL = 0x2803

#: 16-bit characteristic UUID shorts of the MiOT service 0xFE95, with the value handle
#: from ``src/cuktech_ble/protocol.py`` as fallback when discovery finds nothing.
CHARACTERISTICS = {
    0x0004: ("fw_version", 0x0008),
    0x0010: ("auth_ctrl", 0x000D),
    0x0019: ("auth_data", 0x0010),
    0x001A: ("cmd_send", 0x0019),
    0x001B: ("cmd_recv", 0x001C),      # <- the notify characteristic we want
    0x001C: ("device_info", 0x001F),
}
NOTIFY_CHAR_UUID = 0x001B
AUTH_CHAR_UUID = 0x0019

KIND_INLINE = "inline"          # data[2] == 0x02 -> _handle_inline_data
KIND_MULTIFRAME = "multiframe"  # data[2] == 0x00 -> _handle_multiframe
KIND_OTHER = "other"            # anything else  -> _try_process_inline_frame


def classify_frame(payload: bytes) -> str:
    """Classify a notification payload exactly like ``ble_manager.py:1805-1810`` dispatches.

    ``ble_manager`` tests ``data[2] == 0x02`` first (inline), then ``data[2] == 0x00 and
    len(data) >= 6`` (multiframe header), and falls through to the generic inline handler
    for everything else -- so a short 0x00 frame is "other", not "multiframe".
    """
    if len(payload) < 4:
        return KIND_OTHER
    if payload[2] == 0x02:
        return KIND_INLINE
    if payload[2] == 0x00 and len(payload) >= 6:
        return KIND_MULTIFRAME
    return KIND_OTHER


# ---------------------------------------------------------------------------
# btsnoop -> HCI packets
# ---------------------------------------------------------------------------

@dataclass
class HciPacket:
    h4_type: int          # H4 packet-type indicator
    data: bytes           # full record payload, *including* the H4 indicator byte
    sent: bool            # True = host -> controller
    ts: datetime          # per-spec conversion; see module docstring on the clock caveat
    ts_us: int            # raw btsnoop timestamp field
    record_index: int


def read_btsnoop_header(raw: bytes) -> Tuple[int, int]:
    if len(raw) < 16 or raw[:8] != BTSNOOP_MAGIC:
        raise ValueError("not a btsnoop file (bad magic)")
    return struct.unpack(">II", raw[8:16])


def iter_hci_packets(path: Path) -> Iterator[HciPacket]:
    """Yield H4-framed HCI packets: 16-byte file header, then 24-byte record headers."""
    raw = path.read_bytes()
    version, dlt = read_btsnoop_header(raw)
    if dlt not in (DLT_H4, DLT_H4_ALT):
        raise ValueError(
            f"{path.name}: datalink type {dlt} is not H4 UART framing (1001/1002); "
            f"HCI LL (1003) captures would need link-layer reassembly and are unsupported."
        )
    off = 16
    index = 0
    while off + 24 <= len(raw):
        # record header: original_length, included_length, flags, cumulative_drops,
        # then a signed 64-bit microsecond timestamp
        orig_len, incl_len, flags, drops = struct.unpack(">IIII", raw[off:off + 16])
        ts_us = struct.unpack(">q", raw[off + 16:off + 24])[0]
        off += 24
        if incl_len > len(raw) - off:
            print(f"  note: trailing record {index} truncated; stopping", file=sys.stderr)
            break
        payload = raw[off:off + incl_len]
        off += incl_len
        if drops or orig_len != incl_len:
            print(f"  note: record {index}: orig={orig_len} incl={incl_len} drops={drops}",
                  file=sys.stderr)
        if payload:
            yield HciPacket(
                h4_type=payload[0],
                data=payload,
                sent=not (flags & 0x01),      # bit0: 0 = sent, 1 = received
                ts=datetime.fromtimestamp(ts_us / 1e6, timezone.utc),
                ts_us=ts_us,
                record_index=index,
            )
        index += 1


# ---------------------------------------------------------------------------
# HCI ACL -> L2CAP -> ATT
# ---------------------------------------------------------------------------

@dataclass
class AttPdu:
    opcode: int
    handle: Optional[int]
    value: bytes
    sent: bool
    ts: datetime
    ts_us: int
    record_index: int
    conn_handle: int


class L2capReassembler:
    """Rebuild L2CAP SDUs from HCI ACL records.

    These captures come from the ESP32 sniffer firmware, whose Packet_Boundary bits are
    not trustworthy: 0b00, 0b01 and 0b10 all show up on records that are complete, and
    long ATT PDUs genuinely do span records (one announces L2CAP length 28 with 23 bytes
    present, a later record carries the 5-byte tail). Keying reassembly off PB would
    corrupt exactly the frames this tool exists to preserve -- a truncated AES-CCM frame
    fails its tag check and looks indistinguishable from "wrong session keys".

    So the declared L2CAP length drives everything, per connection handle:

      * if an SDU is still open on this handle -> append this record's bytes to it
      * otherwise the record must begin with a plausible L2CAP header; a header whose
        length exceeds what is present opens a pending SDU
      * bytes that are neither a continuation nor a plausible header are counted as
        orphans and dropped -- never emitted as a frame
    """

    FIXED_CIDS = frozenset({0x0001, 0x0002, L2CAP_CID_ATT, L2CAP_CID_SIGNALLING,
                            L2CAP_CID_LE_SIGNALLING, 0x0007, 0x0008, 0x0009, 0x000A})

    def __init__(self) -> None:
        self._open: Dict[int, Tuple[int, int, bytes]] = {}   # conn -> (cid, want, buffer)
        self.reassembled = 0
        self.orphans = 0
        self.unparsable = 0

    def feed(self, pkt: HciPacket) -> List[Tuple[int, bytes]]:
        body = pkt.data[1:]
        if len(body) < 4:
            return []
        handle_info, acl_len = struct.unpack("<HH", body[:4])
        conn = handle_info & 0x0FFF
        data = body[4:4 + acl_len] if acl_len else body[4:]
        if not data:
            return []

        pending = self._open.get(conn)
        if pending is not None:
            cid, want, buf = pending
            buf = buf + data
            if len(buf) < want:
                self._open[conn] = (cid, want, buf)
                return []
            self.reassembled += 1
            del self._open[conn]
            out: List[Tuple[int, bytes]] = [(cid, buf[:want])]
            if len(buf) > want:                 # a second SDU rode in on the same record
                out.extend(self._walk(conn, buf[want:]))
            return out

        return self._walk(conn, data)

    def _walk(self, conn: int, buf: bytes) -> List[Tuple[int, bytes]]:
        out: List[Tuple[int, bytes]] = []
        pos = 0
        while pos + 4 <= len(buf):
            l2cap_len, cid = struct.unpack("<HH", buf[pos:pos + 4])
            body = buf[pos + 4:]
            if not (cid in self.FIXED_CIDS or cid >= DYNAMIC_CID_MIN):
                self.orphans += 1        # not an L2CAP header at all -> stranded bytes
                return out
            if l2cap_len == 0 or l2cap_len > MAX_SDU:
                self.unparsable += 1
                return out
            pos += 4
            if l2cap_len <= len(body):
                out.append((cid, body[:l2cap_len]))
                pos += l2cap_len
                continue
            self._open[conn] = (cid, l2cap_len, body)   # fragmented; await the tail
            break
        return out


class SnoopDecoder:
    """One decode pass over a capture: ATT PDUs plus the GATT table discovered en route."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.version = self.dlt = 0
        self.packets = self.acl_packets = 0
        self.att: List[AttPdu] = []
        self.l2cap = L2capReassembler()
        self.attr_uuids: Dict[int, int] = {}           # attribute handle -> 16-bit UUID
        self.char_value: Dict[int, int] = {}           # characteristic UUID -> value handle
        self.relay_records = 0                          # sniffer uplink records (no ATT)
        self.first_ts = self.last_ts = None

    def run(self) -> "SnoopDecoder":
        raw = self.path.read_bytes()
        self.version, self.dlt = read_btsnoop_header(raw)
        for pkt in iter_hci_packets(self.path):
            self.packets += 1
            self.first_ts = pkt.ts if self.first_ts is None else self.first_ts
            self.last_ts = pkt.ts
            if pkt.h4_type != H4_ACL:
                continue
            self.acl_packets += 1
            for cid, sdu in self.l2cap.feed(pkt):
                if cid == L2CAP_CID_ATT:
                    self._feed_att(sdu, pkt)
                else:
                    self.relay_records += 1
        return self

    def _feed_att(self, sdu: bytes, pkt: HciPacket) -> None:
        if not sdu:
            return
        opcode, rest = sdu[0], sdu[1:]
        handle: Optional[int] = None
        if opcode in NOTIFY_OPCODES or opcode == ATT_HANDLE_VALUE_CONF:
            if len(rest) < 3:
                return
            handle = struct.unpack("<H", rest[:2])[0]
            value = rest[2:]
        elif opcode in (ATT_WRITE_REQ, ATT_WRITE_CMD, ATT_READ_REQ,
                        ATT_READ_BY_TYPE_REQ, ATT_READ_BY_GROUP_TYPE_REQ):
            if len(rest) < 2:
                return
            handle = struct.unpack("<H", rest[:2])[0]
            value = rest[2:]
        else:
            value = rest
        self.att.append(AttPdu(opcode=opcode, handle=handle, value=value, sent=pkt.sent,
                               ts=pkt.ts, ts_us=pkt.ts_us, record_index=pkt.record_index,
                               conn_handle=0))
        if opcode == ATT_READ_BY_GROUP_TYPE_RSP:
            self._parse_group_type_rsp(value)
        elif opcode == ATT_READ_BY_TYPE_RSP:
            self._parse_read_by_type_rsp(value)

    def _parse_group_type_rsp(self, value: bytes) -> None:
        """Read By Group Type Rsp over 0x2803: [item_len][[decl_h][props][value_h][uuid]...]"""
        if not value:
            return
        item_len = value[0]
        if item_len < 7:
            return
        body = value[1:]
        for i in range(0, len(body) - item_len + 1, item_len):
            item = body[i:i + item_len]
            decl_h, value_h = struct.unpack("<HH", item[0:2] + item[3:5])
            uuid = _short_uuid(item[5:])
            if uuid is None:
                continue
            self.attr_uuids[decl_h] = UUID_CHARACTERISTIC_DECL
            self.attr_uuids[value_h] = uuid
            self.char_value[uuid] = value_h

    def _parse_read_by_type_rsp(self, value: bytes) -> None:
        if not value:
            return
        item_len = value[0]
        if item_len < 7:
            return
        body = value[1:]
        for i in range(0, len(body) - item_len + 1, item_len):
            item = body[i:i + item_len]
            handle = struct.unpack("<H", item[:2])[0]
            uuid = _short_uuid(item[2:4])
            if uuid is None:
                continue
            self.attr_uuids[handle] = uuid
            if uuid == UUID_CHARACTERISTIC_DECL:
                value_h = struct.unpack("<H", item[5:7])[0]
                char_uuid = _short_uuid(item[7:])
                if char_uuid is not None:
                    self.attr_uuids[value_h] = char_uuid
                    self.char_value.setdefault(char_uuid, value_h)

    def resolve_value_handle(self, char_uuid: int) -> Tuple[int, bool]:
        """(value handle, discovered?) -- captured GATT table first, protocol.py fallback second."""
        if char_uuid in self.char_value:
            return self.char_value[char_uuid], True
        if char_uuid in CHARACTERISTICS:
            return CHARACTERISTICS[char_uuid][1], False
        raise KeyError(f"no handle known for characteristic UUID 0x{char_uuid:04x}")

    def notifications(self, handle: int) -> List[AttPdu]:
        return [p for p in self.att if p.opcode in NOTIFY_OPCODES and p.handle == handle
                and not p.sent]

    def opcode_histogram(self) -> Dict[int, int]:
        hist: Dict[int, int] = {}
        for p in self.att:
            hist[p.opcode] = hist.get(p.opcode, 0) + 1
        return hist


def _short_uuid(b: bytes) -> Optional[int]:
    """16-bit UUID short from a 2-byte or 16-byte attribute type (little-endian on air)."""
    if len(b) >= 16:
        return int.from_bytes(b[0:2], "little")
    if len(b) >= 2:
        return int.from_bytes(b[:2], "little")
    return None


# ---------------------------------------------------------------------------
# MiOT session recovery + decryption through the production code path
# ---------------------------------------------------------------------------

@dataclass
class Handshake:
    """The login material recovered from one capture, plus verification results."""
    ts: Optional[datetime] = None
    app_random: Optional[bytes] = None        # random(16) the app wrote, clear text
    dev_random: Optional[bytes] = None        # random(16) the device notified, clear text
    observed_app_hmac: Optional[bytes] = None  # the 32-byte HMAC the app actually sent
    token_present: bool = False
    handshake_verified: bool = False          # recomputed app HMAC == observed one
    keys: Optional[Dict[str, bytes]] = None
    controller: object = None
    notes: List[str] = field(default_factory=list)

    def redacted_summary(self) -> str:
        parts = [f"app_random={'yes' if self.app_random else 'no'}",
                 f"dev_random={'yes' if self.dev_random else 'no'}",
                 f"token={'provided' if self.token_present else 'absent'}",
                 f"handshake_hmac_verified={self.handshake_verified}"]
        return " ".join(parts)


def recover_handshake(dec: SnoopDecoder) -> Handshake:
    """Pull app_random / dev_random / app-HMAC out of the auth characteristic traffic."""
    hs = Handshake()
    auth_handle, _ = dec.resolve_value_handle(AUTH_CHAR_UUID)

    # The app writes its random as a 12-byte-prefixed frame; the 18-byte write whose value
    # is [0x01 0x00] + random(16) is the key exchange (controller.py:462-463).
    for p in dec.att:
        if p.sent and p.handle == auth_handle and p.opcode in (ATT_WRITE_CMD, ATT_WRITE_REQ):
            if len(p.value) == 18 and p.value[0:2] == b"\x01\x00":
                if hs.app_random is None:
                    hs.app_random, hs.ts = p.value[2:], p.ts
                # the *next* 34-byte write is [0x01 0x00] + login HMAC(32)
                continue
            if len(p.value) == 34 and p.value[0:2] == b"\x01\x00" and hs.app_random:
                hs.observed_app_hmac = p.value[2:]

    # The device answers with an inline frame: [ctr(2)][0x02][msg_type][random(16)...]
    for p in dec.notifications(auth_handle):
        if len(p.value) >= 20 and p.value[2] == 0x02:
            hs.dev_random = p.value[4:20]
            break
    if hs.app_random and not hs.dev_random:
        hs.notes.append("device random not seen: capture started after the key exchange")
    return hs


def derive_keys(token: bytes, app_random: bytes, dev_random: bytes) -> Dict[str, bytes]:
    """HKDF-SHA256(ikm=token, salt=app_random||dev_random, info=b'mible-login-info', 64).

    Mirrors controller.py:_try_authenticate (and the ESP32 firmware's derive_session_keys,
    which is byte-for-byte the same construction).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    derived = HKDF(algorithm=hashes.SHA256(), length=64, salt=app_random + dev_random,
                   info=b"mible-login-info").derive(token)
    return {"dev_key": derived[0:16], "app_key": derived[16:32],
            "dev_iv": derived[32:36], "app_iv": derived[36:40]}


def verify_handshake(hs: Handshake, keys: Dict[str, bytes]) -> bool:
    """Recompute the app login HMAC and compare it with what the capture carried.

    This proves the token and both randoms are the right ones *before* trusting any
    decryption result -- a wrong token otherwise shows up only as an opaque CCM tag error
    on every frame, which is easy to mistake for "the captures are unusable".
    """
    import hmac
    import hashlib
    if not (hs.observed_app_hmac and hs.app_random and hs.dev_random):
        return False
    salt = hs.app_random + hs.dev_random
    return hmac.new(keys["app_key"], salt, hashlib.sha256).digest() == hs.observed_app_hmac


def build_controller_class():
    """Return the *production* ``CuktechBLEController`` so nothing is re-implemented here."""
    # 只用**本仓库**的 src（脚本所在位置的上一级）：写死开发机的绝对路径会让这个
    # 复现工具在别人的机器上静默回退到"找不到"，而不是报错。
    for src_root in (Path(__file__).resolve().parent.parent / "src",):
        if (src_root / "cuktech_ble" / "controller.py").is_file() \
                and str(src_root) not in sys.path:
            sys.path.insert(0, str(src_root))
    try:
        from cuktech_ble.controller import CuktechBLEController
    except ImportError as exc:                      # pragma: no cover - env dependent
        raise ImportError(
            "cannot import cuktech_ble.controller -- run with the server venv "
            f"(ble_server/.venv/bin/python) so bleak/cryptography resolve. Detail: {exc}"
        )
    return CuktechBLEController


def make_decryptor(controller_cls, keys: Dict[str, bytes]):
    """A real controller instance with only the session keys injected.

    ``__init__`` would build bleak/device state we neither have nor need; ``decrypt()``
    touches just ``_session_keys`` and its ``it``-rollover counters, so ``object.__new__``
    is enough to reach the production AES-CCM path.
    """
    ctrl = object.__new__(controller_cls)
    ctrl._session_keys = keys
    ctrl._dev_it_hi = 0
    ctrl._last_dev_it_lo = 0
    return ctrl


def read_token_from_config(path: Path) -> Optional[bytes]:
    """Read the 12-byte MiOT token out of a server config.yaml. The value is never printed."""
    try:
        import yaml
    except ImportError:
        print("pyyaml unavailable; use --token-hex or the server venv", file=sys.stderr)
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    token = str((data.get("ble") or {}).get("token") or "").strip()
    if len(token) != 24:
        return None
    try:
        raw = bytes.fromhex(token)
    except ValueError:
        return None
    return raw if len(raw) == 12 else None


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    source: str
    ts: datetime
    ts_us: int
    t_rel_ms: int
    record_index: int
    att_handle: int
    kind: str
    payload: bytes
    plaintext: Optional[bytes] = None
    group_index: Optional[int] = None
    group_frames_total: Optional[int] = None
    group_ok: Optional[bool] = None
    notes: str = ""

    def to_fixture(self, fixture_id: str) -> dict:
        out = {
            "id": fixture_id,
            "source": self.source,
            "captured_at": None,          # filled by build_fixture_doc (mtime-anchored estimate)
            "sniffer_clock_iso": self.ts.isoformat(),   # per-spec conversion; epoch is bogus
            "t_rel_ms": self.t_rel_ms,
            "record_index": self.record_index,
            "att_handle": f"0x{self.att_handle:04x}",
            "kind": self.kind,
            "hex": self.payload.hex(),
            "frame_header": {
                "ctr": self.payload[:2].hex(),
                "format": f"0x{self.payload[2]:02x}",
                "msg_type": f"0x{self.payload[3]:02x}" if len(self.payload) > 3 else None,
            },
            "plaintext_hex": self.plaintext.hex() if self.plaintext else None,
            "encrypted": True,
            "notes": self.notes,
        }
        if self.kind == KIND_INLINE and len(self.payload) >= 6:
            out["device_it"] = struct.unpack("<H", self.payload[4:6])[0]
        if self.kind == KIND_MULTIFRAME and len(self.payload) >= 6:
            out["multiframe"] = {
                "data_id": f"0x{self.payload[3]:02x}",
                "frames_announced": self.payload[4] + 0x100 * self.payload[5],
                "frames_collected": self.group_frames_total,
                "complete": self.group_ok,
                "position_in_group": self.group_index,
            }
        if self.plaintext and len(self.plaintext) >= 8:
            out["decoded"] = {
                "b4": f"0x{self.plaintext[4]:02x}",
                "siid": self.plaintext[5],
                "piid": self.plaintext[7],
            }
        return out


def group_multiframe(pdus: List[AttPdu], handle: int) -> List[Tuple[bytes, int, bool]]:
    """Rebuild whole multiframe exchanges, mirroring ``_handle_multiframe``.

    Returns [(first_frame_payload_bytes, subframes_collected, complete)] for every
    announced group, in capture order. The first frame of a group is the *header*
    notification -- exactly what the server's dispatch hands ``_handle_multiframe`` -- so
    it is what a fixture must replay; the subframes follow it in the same order.
    """
    groups = []
    i = 0
    while i < len(pdus):
        v = pdus[i].value
        if len(v) >= 6 and v[2] == 0x00:
            want = v[4] + 0x100 * v[5]          # announced *frame count*, not byte count
            j = i + 1
            while j < len(pdus) and (j - i - 1) < want:
                j += 1
            collected = j - i - 1
            groups.append((v, collected, collected == want))
            i = j
        else:
            i += 1
    return groups


def extract_frames(path: Path, handle_override: Optional[int] = None) -> Tuple[
        SnoopDecoder, List[Frame], int, bool]:
    """Decode a capture into notification frames. Returns (decoder, frames, handle, discovered)."""
    dec = SnoopDecoder(path).run()
    if handle_override is not None:
        handle, discovered = handle_override, False
    else:
        handle, discovered = dec.resolve_value_handle(NOTIFY_CHAR_UUID)

    pdus = dec.notifications(handle)
    first_ts = pdus[0].ts_us if pdus else 0
    frames: List[Frame] = []
    for p in pdus:
        frames.append(Frame(source=str(path), ts=p.ts, ts_us=p.ts_us,
                            t_rel_ms=(p.ts_us - first_ts) // 1000,
                            record_index=p.record_index, att_handle=handle,
                            kind=classify_frame(p.value), payload=bytes(p.value),
                            notes="handle discovered from captured GATT table" if discovered
                            else "handle from protocol.py fallback table"))
    # annotate multiframe groups so a replay knows which header is whole vs starved
    groups = group_multiframe(pdus, handle)
    if groups:
        idx = 0
        for header, collected, complete in groups:
            for f in frames:
                if f.payload == header and f.group_index is None:
                    f.group_frames_total, f.group_ok, f.group_index = collected, complete, idx
                    f.notes += (f"; multiframe header: {collected} subframes "
                               f"{'reassembled' if complete else 'INCOMPLETE'}")
                    idx += 1
    return dec, frames, handle, discovered


def attach_plaintext(frames: List[Frame], dec: SnoopDecoder, token: bytes,
                     controller_cls) -> Handshake:
    """Recover session keys, verify them, then decrypt inline frames in place."""
    hs = recover_handshake(dec)
    hs.token_present = len(token) == 12
    if not (hs.app_random and hs.dev_random):
        hs.notes.append("cannot derive keys: handshake material missing from capture")
        return hs
    hs.keys = derive_keys(token, hs.app_random, hs.dev_random)
    hs.handshake_verified = verify_handshake(hs, hs.keys)
    if not hs.handshake_verified:
        hs.notes.append("app login HMAC does NOT match: token is not the one this "
                        "session used (rotated?) -- frames stay undecryptable")
    ctrl = make_decryptor(controller_cls, hs.keys)
    hs.controller = ctrl
    for f in frames:
        if f.kind != KIND_INLINE or len(f.payload) <= 6:
            continue
        try:
            f.plaintext = ctrl.decrypt(f.payload[4:])
        except Exception as exc:
            hs.notes.append(f"decrypt raised {type(exc).__name__}")
    return hs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _decode_port_enrichment(plaintext: Optional[bytes]) -> Optional[dict]:
    """Run the *production* port decoder on a recovered plaintext, when it applies.

    Port pushes are the frames the server actually acts on: ``_process_decrypted_frame``
    gates on ``b4 == pt[4] == 0x04`` and ``piid = pt[7]`` in ``PORT_NAMES``. Feeding those
    through ``state_protocol_v2.decode_port`` is the honest end-to-end check that a frame
    is not just well-encrypted but semantically real. Any other frame returns None.
    """
    if not plaintext or len(plaintext) < 8:
        return None
    root = Path(__file__).resolve().parent.parent      # ble_server/ (state_protocol_v2.py)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from state_protocol_v2 import decode_port
    except ImportError:
        return None
    piid = plaintext[7]
    if plaintext[4] != 0x04 or piid not in (1, 2, 3, 4):
        return None
    try:
        info = decode_port(piid, plaintext)
    except Exception:
        return None
    return info or None


def build_fixture_doc(frames: List[Frame], captures: List[Path]) -> dict:
    """Assemble the documented fixture document (see tests/fixtures/README.md).

    ``captured_at`` is an *estimate*: the sniffer's own clock has a broken epoch (it
    renders as year 4026 under the btsnoop spec), so absolute time is anchored to the
    capture file's mtime minus the frame's offset from the capture's last record. Ordering
    within a capture is exact (raw microseconds); the wall clock is good to roughly a
    minute. Both raw and relative values are kept so nothing is hidden by the estimate.
    """
    mtimes = {}
    for path in captures:
        try:
            mtimes[str(path.resolve())] = path.stat().st_mtime
        except OSError:
            pass
    last_ts: Dict[str, int] = {}
    for f in frames:
        key = str(Path(f.source).resolve())
        last_ts[key] = max(last_ts.get(key, f.ts_us), f.ts_us)

    records = []
    seen_ids: Dict[str, int] = {}
    repo_root = Path(__file__).resolve().parent.parent.parent      # .../cuktech
    for f in frames:
        stem = Path(f.source).stem.replace("btsnoop_", "").replace("_log", "")
        seen_ids[stem] = seen_ids.get(stem, 0) + 1
        fid = f"{stem}-{seen_ids[stem]:04d}"
        rec = f.to_fixture(fid)

        key = str(Path(f.source).resolve())
        end = last_ts.get(key, f.ts_us)
        if key in mtimes:
            est = datetime.fromtimestamp(mtimes[key] - (end - f.ts_us) / 1e6,
                                        timezone.utc).astimezone()
            rec["captured_at"] = est.isoformat(timespec="seconds")
        # normalise the source path to be repo-relative (docs/hci_log/x.log)
        try:
            rec["source"] = str(Path(f.source).resolve().relative_to(repo_root))
        except ValueError:
            rec["source"] = Path(f.source).name
        rec["ts_us_raw"] = f.ts_us
        rec["capture_file"] = Path(f.source).name

        decoded = _decode_port_enrichment(f.plaintext)
        if decoded is not None:
            rec["decoded_port"] = decoded
            rec["notes"] = (rec["notes"] + "; " if rec["notes"] else "") + \
                "port push, decodes via state_protocol_v2.decode_port"
        elif f.plaintext is not None and len(f.plaintext) < 8:
            rec["notes"] = (rec["notes"] + "; " if rec["notes"] else "") + (
                f"short plaintext ({len(f.plaintext)}B): ble_manager._decrypt_payload "
                f"requires >=8B, so it counts as a decrypt failure in production")
        records.append(rec)

    return {
        "schema_version": 1,
        "generated_by": "ble_server/scripts/extract_btsnoop_frames.py",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": {
            "origin": "REAL device traffic -- HCI captures of a CUKTECH charger, "
                      "recorded 2025-07-12 with the ESP32 BLE sniffer",
            "synthetic": False,
            "capture_files": sorted({Path(f.source).name for f in frames}),
            "frame_definition": (
                "ATT Handle Value Notification value bytes: the payload after the "
                "3-byte ATT header, 4-byte CUKTECH frame header INCLUDED, no "
                "HCI/L2CAP framing. Exactly what ble_manager dispatches on data[2]."),
            "notify_characteristic": "0x001b (cmd_recv), ATT value handle 0x001c, "
                                     "Mi service 0000fe95-...",
            "encrypted": True,
            "plaintext_note": (
                "hex is the raw ciphertext as sent over the air; plaintext_hex is the "
                "real MiOT frame recovered with the session keys from that capture's own "
                "login handshake. Session keys and the device token are deliberately NOT "
                "stored here."),
            "replay_note": (
                "Because frames are session-encrypted, replaying hex through the "
                "production decrypt path needs that session's keys. Tests should either "
                "stub ctrl.decrypt to return plaintext_hex (the pattern already used in "
                "tests/test_ble_manager.py), or feed plaintext_hex to "
                "_process_decrypted_frame."),
            "timestamp_note": (
                "captured_at is the sniffer's per-spec conversion of its clock, which has "
                "a broken epoch and is NOT wall-clock; captured_at_estimated anchors to "
                "the capture file mtime. Trust t_rel_ms / ts_us_raw for ordering."),
        },
        "frames": records,
    }


def cmd_survey(paths: List[Path]) -> int:
    for path in paths:
        dec = SnoopDecoder(path).run()
        print(f"\n=== {path} ===")
        print(f"  btsnoop v{dec.version} datalink={dec.dlt}")
        print(f"  records={dec.packets} acl={dec.acl_packets} "
              f"reassembled_sdus={dec.l2cap.reassembled} orphan_fragments={dec.l2cap.orphans}")
        if dec.relay_records:
            print(f"  non-ATT L2CAP records (sniffer uplink): {dec.relay_records}")
        print(f"  clock window (per-spec conversion, see caveat): {dec.first_ts} -> {dec.last_ts}")
        hist = dec.opcode_histogram()
        print(f"  ATT PDUs: {len(dec.att)}")
        for op in sorted(hist):
            print(f"     0x{op:02x} {ATT_OPCODE_NAMES.get(op, '?'):<28} {hist[op]:5d}")
        if dec.attr_uuids:
            print("  GATT table discovered from the capture:")
            for h in sorted(dec.attr_uuids):
                u = dec.attr_uuids[h]
                label = CHARACTERISTICS.get(u, (f"uuid_0x{u:04x}",))[0]
                print(f"     0x{h:04x} {label}")
        else:
            print("  GATT table: not discoverable (no Read By Type/Group responses)")
        for uuid in sorted(dec.char_value):
            name = CHARACTERISTICS.get(uuid, (f"0x{uuid:04x}",))[0]
            print(f"  characteristic {name}: value handle 0x{dec.char_value[uuid]:04x}")
        by_handle: Dict[int, int] = {}
        for p in dec.att:
            if p.opcode in NOTIFY_OPCODES and not p.sent:
                by_handle[p.handle] = by_handle.get(p.handle, 0) + 1
        print("  notifications by value handle:")
        if not by_handle:
            print("     (none)")
        for h in sorted(by_handle):
            u = dec.attr_uuids.get(h)
            print(f"     0x{h:04x} ({CHARACTERISTICS.get(u, ('?',))[0]}) {by_handle[h]:5d}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="extract_btsnoop_frames.py",
        description="Extract real BLE notification frames from btsnoop HCI captures.",
        epilog=__doc__.split("Usage\n-----")[0][-1200:] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("files", nargs="*", type=Path, help="btsnoop capture file(s)")
    ap.add_argument("--survey", action="store_true",
                    help="ATT opcode histogram, discovered GATT table, notification handles")
    ap.add_argument("--summary", action="store_true",
                    help="per-capture frame counts and kind/PIID distribution")
    ap.add_argument("--dump", action="store_true", help="print every notification as hex")
    ap.add_argument("--dedup", action="store_true", help="collapse byte-identical payloads")
    ap.add_argument("--handle", type=lambda s: int(s, 16), default=None,
                    help="override the notification ATT value handle (hex)")
    ap.add_argument("--jsonl", type=Path, default=None, metavar="OUT.jsonl",
                    help="write one JSON object per frame")
    ap.add_argument("--fixtures", type=Path, default=None, metavar="OUT.json",
                    help="write the tests/fixtures/notify_frames.json fixture file")
    ap.add_argument("--decrypt", action="store_true",
                    help="recover the session keys from the capture and decrypt via the "
                         "production CuktechBLEController.decrypt()")
    ap.add_argument("--verify-handshake", action="store_true",
                    help="only prove the token/randoms are right (recompute the login HMAC)")
    ap.add_argument("--token-hex", help="12-byte MiOT device token as 24 hex chars")
    ap.add_argument("--token-file", type=Path, help="config.yaml to read the token from")
    args = ap.parse_args(argv)

    if not args.files:
        ap.error("no capture files given")
    token = None
    if args.token_hex:
        try:
            token = bytes.fromhex(args.token_hex.strip())
        except ValueError:
            print("--token-hex 必须是 24 个十六进制字符（12 字节），不含分隔符",
                  file=sys.stderr)
            return 2
    elif args.token_file:
        token = read_token_from_config(args.token_file)
    if token is not None and len(token) != 12:
        print("token must be 12 bytes (24 hex chars)", file=sys.stderr)
        return 2
    if (args.decrypt or args.verify_handshake) and token is None:
        print("--decrypt/--verify-handshake need --token-hex or --token-file", file=sys.stderr)
        return 2

    if args.survey:
        return cmd_survey(args.files)

    controller_cls = None
    if args.decrypt:
        try:
            controller_cls = build_controller_class()
        except ImportError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    all_frames: List[Frame] = []
    for path in args.files:
        dec, frames, handle, discovered = extract_frames(path, args.handle)
        hs = None
        if token is not None:
            if args.decrypt:
                hs = attach_plaintext(frames, dec, token, controller_cls)
            elif args.verify_handshake:
                hs = recover_handshake(dec)
                hs.token_present = True
                if hs.app_random and hs.dev_random:
                    hs.keys = derive_keys(token, hs.app_random, hs.dev_random)
                    hs.handshake_verified = verify_handshake(hs, hs.keys)
                else:
                    hs.notes.append("handshake material incomplete in this capture")
        if hs is not None:
            print(f"\n=== {path.name} handshake ===")
            print(f"  {hs.redacted_summary()}")
            for n in hs.notes:
                print(f"  note: {n}")
        kinds: Dict[str, int] = {}
        piids: Dict[int, int] = {}
        for f in frames:
            kinds[f.kind] = kinds.get(f.kind, 0) + 1
            if f.plaintext and len(f.plaintext) > 7:
                piids[f.plaintext[7]] = piids.get(f.plaintext[7], 0) + 1
        if args.summary:
            print(f"\n=== {path.name} ===")
            print(f"  notify handle             : 0x{handle:04x}"
                  f"{' (discovered)' if discovered else ' (protocol.py fallback)'}")
            print(f"  notification PDUs         : {len(frames)}")
            print(f"  distinct payloads         : {len({f.payload for f in frames})}")
            print(f"  kinds by data[2]          : {dict(sorted(kinds.items()))}")
            ok = sum(1 for f in frames if f.plaintext)
            if ok or args.decrypt:
                print(f"  decrypted (CCM tag ok)    : {ok}")
                print(f"  PIID distribution (pt[7]) : {dict(sorted(piids.items()))}")
        if args.dump:
            seen = set()
            for f in frames:
                if args.dedup and f.payload in seen:
                    continue
                seen.add(f.payload)
                line = (f"  +{f.t_rel_ms:>8d}ms rec{f.record_index:>6d} "
                        f"{f.kind:<10} {f.payload.hex()}")
                if f.plaintext:
                    line += f"  PT={f.plaintext.hex()}"
                print(line)
        all_frames.extend(frames)

    if args.jsonl or args.fixtures:
        if args.fixtures:
            doc = build_fixture_doc(all_frames, args.files)
            args.fixtures.parent.mkdir(parents=True, exist_ok=True)
            args.fixtures.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
            n_pt = sum(1 for f in all_frames if f.plaintext)
            n_port = sum(1 for r in doc["frames"] if "decoded_port" in r)
            print(f"wrote {len(all_frames)} frames to {args.fixtures} "
                  f"({n_pt} with recovered plaintext, {n_port} decode as port pushes)")
        if args.jsonl:
            args.jsonl.write_text(
                "\n".join(json.dumps(f.to_fixture(f"{Path(f.source).stem}-{i:04d}"))
                          for i, f in enumerate(all_frames, 1)) + "\n", encoding="utf-8")
            print(f"wrote {len(all_frames)} frame records to {args.jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
