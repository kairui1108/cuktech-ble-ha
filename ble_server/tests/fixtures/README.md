# `tests/fixtures/` — recorded BLE notification frames

Test fixtures extracted from **real over-the-air HCI captures** of a CUKTECH charger.
Nothing in this directory is synthetic unless a file says so explicitly.

| File | What it is | Real or synthetic |
|---|---|---|
| `notify_frames.json` | 142 real `ATT Handle Value Notification` frames on the charger's notify characteristic, with their recovered MiOT plaintext | **REAL** — decoded from `docs/hci_log/*.log` |
| `README.md` | this file | — |

---

## 1. Provenance

**What was captured.** The ATT traffic of a CUKTECH power bank / charger (the MiOT
`FE95` GATT service, characteristic UUID `0x001b` = `cmd_recv`, ATT value handle
`0x001c`). Device under test: charger MAC `3C:CD:73:34:AE:59`, firmware string
`HS6621C` (Bluetrum SoC), firmware version `2.1.2` — both read straight out of the
captures (`device_info` / `fw_version` characteristic reads).

**Where it came from.** Five btsnoop captures in `docs/hci_log/`, recorded
**2026-07-12** (system clock of the recording host; see the timestamp caveat in §5):

| Capture | Frames kept | Port pushes | Notes |
|---|---|---|---|
| `btsnoop_hci.log`   | 48 | 33 | session 1, ~2 min |
| `btsnoop_c2pd.log`  | 30 | 17 | session 2 |
| `btsnoop_c2pd2.log` | 33 | 20 | session 3 |
| `btsnoop_c2pd_on.log` | 31 | 19 | session 4 |
| `btsnoop_hci_old.log` | **0** | 0 | see §6 — contains no ATT at all |
| **total** | **142** | **89** | all `inline` (`data[2] == 0x02`) |

**How it was captured.** An ESP32 BLE sniffer wrote the HCI stream to btsnoop files
while an Android host (advertising itself as `kairui Redmi K30 5G`) was connected to
the charger and running the MiOT login + telemetry flow. The sniffer's own relay
traffic is interleaved in the same files and is discarded by the parser (§6).

**Extraction.** Every frame was produced by `ble_server/scripts/extract_btsnoop_frames.py`
(committed alongside for reproducibility). The frames are not hand-written, not
re-typed, and not fabricated; each `hex` value is byte-for-byte what the sniffer
recorded, and each `plaintext_hex` is the output of the **production**
`CuktechBLEController.decrypt()` for that ciphertext.

**No secrets in this directory.** The device token and the derived MiOT session keys
(`dev_key` / `dev_iv` / `app_key` / `app_iv`) and the handshake randoms are deliberately
**not** stored here. Telemetry plaintext (voltages, currents, PIIDs) is not a credential.

---

## 2. What one "frame" is

A frame is the **ATT value bytes of one notification PDU**: the payload after the
3-byte ATT header (opcode `0x1b` + 2-byte handle), with **no** HCI or L2CAP framing
and **with** the leading 4-byte CUKTECH frame header. That is exactly the byte string
`bleak` hands the server, and exactly what `ble_manager.py:1805-1810` dispatches on:

```python
if data[2] == 0x02:            # -> _handle_inline_data(data)     (fixture kind "inline")
elif data[2] == 0x00 and len(data) >= 6:   # -> _handle_multiframe(data)  ("multiframe")
else:                          # -> _try_process_inline_frame(data)        ("other")
```

On-wire layout of the `inline` frames found here:

```
offset 0    1    2        3          4      5      6 ..
       ctr_lo ctr_hi  0x02   msg_type  it_lo  it_hi  AES-CCM ciphertext .. tag(4)
       \______ 4-byte frame header ______/ \_______ encrypted envelope ______/
```

`ctr` is `0x0000` throughout these captures; `it` (`device_it` in the JSON) is the
device's incrementing message counter and is what the CCM nonce is built from.

---

## 3. Schema of `notify_frames.json`

Top level:

```jsonc
{
  "schema_version": 1,
  "generated_by": "ble_server/scripts/extract_btsnoop_frames.py",
  "generated_at": "2026-10-25T…",
  "provenance": { … },        // free-form documentation of origin, see §1
  "frames": [ Frame, … ]      // ordered: capture order, captures as listed in §1
}
```

Each `Frame`:

| Field | Type | Meaning |
|---|---|---|
| `id` | str | stable id, `<capture>-<nnnn>` e.g. `hci-0001`, `c2pd2-0017` |
| `source` | str | repo-relative capture path, e.g. `docs/hci_log/btsnoop_hci.log` |
| `capture_file` | str | bare capture filename |
| `captured_at` | str | **estimated** wall clock (mtime-anchored, ±~1 min, see §5) |
| `sniffer_clock_iso` | str | the sniffer's own clock converted per btsnoop spec — wrong epoch, do not rely on |
| `ts_us_raw` | int | raw btsnoop timestamp field, verbatim |
| `t_rel_ms` | int | ms since the first frame **of that capture**; exact and monotonic |
| `record_index` | int | btsnoop record index (for `-A` hunting in Wireshark/wireshark-cli) |
| `att_handle` | str | `"0x001c"` — the notify characteristic's value handle |
| `kind` | str | `"inline"` \| `"multiframe"` \| `"other"`, per the dispatch above |
| `hex` | str | **the frame**: notification value bytes, lowercase hex |
| `frame_header` | obj | decoded `{ctr, format, msg_type}` of `hex[0:4]` |
| `device_it` | int | inline frames only: `it` from `hex[4:6]` (little-endian) |
| `multiframe` | obj | multiframe headers only — absent here (see §6) |
| `encrypted` | bool | `true`: everything after the 4-byte header is AES-CCM |
| `plaintext_hex` | str\|null | the real MiOT plaintext recovered offline |
| `decoded` | obj | `{b4, siid, piid}` read the way `_process_decrypted_frame` reads it (`pt[4]`, `pt[5]`, `pt[7]`) |
| `decoded_port` | obj | present for the 89 port pushes: output of the production `state_protocol_v2.decode_port(piid, pt)` — `voltage`, `current`, `power`, `active`, `protocol`, … |
| `notes` | str | extraction notes, including the short-plaintext caveat (§6) |

PIID distribution across the 142 frames (`pt[7]`): `1: 59, 2: 33, 3: 6, 4: 6, 8: 3,
17: 18, 21: 13` — i.e. C1/C2/C3/A port pushes plus PIID 17 (C1/C2 hardware protocol)
and PIID 21 settings pushes.

---

## 4. How to replay these

The frames are **session-encrypted**, so there are two supported replay styles:

1. **Dispatch/transport tests** (what `tests/test_ble_manager.py` already does): feed
   `bytes.fromhex(frame["hex"])` into the notification handler and stub
   `mgr.ctrl.decrypt` to return `bytes.fromhex(frame["plaintext_hex"])`. This exercises
   `data[2]` routing, ACK writes, debounce and state updates against real frame shapes.
2. **Decode tests**: feed `bytes.fromhex(frame["plaintext_hex"])` straight into
   `_process_decrypted_frame(pt)` / `decode_port(piid, pt)`. `decoded_port` gives the
   expected result to assert against — the fixtures were generated with the production
   decoder, so it round-trips exactly.

Replaying `hex` through the *real* AES-CCM path additionally needs that capture's
session keys. They are derivable (the login handshake is in the capture, see the
tool's docstring), but **the token they were derived with is not the one currently in
`ble_server/config.yaml`** — it rotated after the capture (proved by the login-HMAC
check failing with today's token and passing with the capture-era one). That token is
therefore intentionally not recorded here either.

---

## 5. Timestamp caveat

The sniffer wrote a btsnoop timestamp with a **broken epoch**: converted per the spec
it renders as year *3996*. Absolute times in these files are unusable. What *is*
reliable is (a) ordering and deltas inside a file, and (b) deltas **between** files —
they match the capture files' mtimes to the second, which is how `captured_at` was
recovered: each frame's offset from its capture's last record is subtracted from that
capture's file mtime. Treat `captured_at` as good to about a minute, and prefer
`t_rel_ms` / `ts_us_raw` for anything ordering-sensitive.

---

## 6. What is *not* in here (honest gaps)

* **No multiframe frames.** All 142 recorded notifications are `inline`
  (`data[2] == 0x02`); not a single `data[2] == 0x00` multiframe header was observed on
  the notify characteristic, so `_handle_multiframe` still has no real recording to
  replay against. Its existing unit tests remain synthetic. Capturing one would need a
  session where the device answers a large multi-attribute read while the sniffer is
  running.
* **No auth-channel frames.** Frames on `auth_data` (`0x0010`) and `cmd_send`
  (`0x0019`) exist in the captures and were used to recover the session keys, but they
  are protocol handshake traffic, not telemetry, and are not part of this fixture set.
* **4 frames have a 5-byte plaintext** (`05 20 xx 00 f0`). They are real, but
  `ble_manager._decrypt_payload` requires `len(pt) >= 8` and therefore counts them as
  decrypt failures. They are kept (with a `notes` marker) because they are what the
  device really sends; see the accompanying report for why that threshold matters.
* **`btsnoop_hci_old.log` yields nothing.** Every ACL record in it belongs to the
  sniffer's own relay channel (connection handle `0x2edc`, L2CAP CID `0x02ff`, 0xf2
  padding) and carries no ATT PDUs at all — 0 notifications, 0 handshake material.
  It is listed above for completeness, not as a source.

---

## 7. Regenerating

```bash
cd ble_server
# token: the one this capture session used (NOT the one in config.yaml post-rotation)
./.venv/bin/python scripts/extract_btsnoop_frames.py \
    --verify-handshake --token-hex <24 hex chars> ../docs/hci_log/btsnoop_hci.log
./.venv/bin/python scripts/extract_btsnoop_frames.py \
    --decrypt --token-hex <24 hex chars> --fixtures tests/fixtures/notify_frames.json \
    ../docs/hci_log/btsnoop_hci.log ../docs/hci_log/btsnoop_c2pd.log \
    ../docs/hci_log/btsnoop_c2pd2.log ../docs/hci_log/btsnoop_c2pd_on.log
```

`--survey` on a capture prints the ATT opcode histogram, the GATT table discovered from
the capture, and the notify handles — useful when adding a new capture to this set.
Generation is deterministic for a given input; only `generated_at` changes.
