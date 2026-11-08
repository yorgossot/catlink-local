# CATLINK Open X local protocol

What one CATLINK Open X on firmware **04.22** (hasscc `deviceType` C08) does on the wire, as observed
by pointing it at a local login responder and MQTT broker. Single box, one firmware, observed over days.
Other boxes, firmware or litter types may differ.

Every row carries a confidence tag:

| Tag | Meaning |
|---|---|
| **confirmed** | changed live and read back, or consistent across many events |
| **observed** | seen, but only a few times |
| **hypothesis** | the byte was constant or changed once; the meaning is inferred |
| **unverified** | taken from the reference project, not tested on this box |

Device identifiers are never shown. Topic templates use `{pk}` (productKey) and `{dn}` (deviceName).
All example bytes below are **synthetic**, built from the layout with the made-up MAC
`02:ca:7b:0c:00:01`.

## 1. Transport and host names

| Item | Value | Confidence |
|---|---|---|
| Protocol | MQTT 3.1 (protocol name `MQIsdp`), keepalive 120 s | confirmed |
| Transport | plaintext TCP 1883 | confirmed |
| Login | HTTPS on TCP 443, before any MQTT | confirmed |
| Auth names | `iot-auth-global.aliyuncs.com`, `iot-auth-global-x.aliyuncs.com` | confirmed |
| MQTT names | `<instance>.mqtt.iothub.aliyuncs.com`, `<instance>-x.mqtt.iothub.aliyuncs.com` | confirmed |
| Names actually used | only the `-x` variants | observed |

`<instance>` is specific to the vendor account/region; read yours from your DNS query log
([SETUP.md](SETUP.md) step 1). Only the `-x` names were contacted, but redirect all four: a box that
falls back to the plain names would otherwise reach the cloud.

### Topics

| Direction | Topic | Purpose |
|---|---|---|
| box subscribes | `/ext/rrpc/+/{pk}/{dn}/user/function/down_cmd` | commands (RRPC) |
| box subscribes | `/ext/rrpc/+/{pk}/{dn}/user/heartbeat/reply` | heartbeat replies |
| box publishes | `{pk}/{dn}/user/heartbeat/up` | heartbeat frames |
| box publishes | `{pk}/{dn}/user/function/up` | event frames (visit, end of clean, ...) |

Heartbeat cadence: about 100 s when idle, much faster (roughly every 12 s) while something is happening; a state change arrives at once as an extra heartbeat.
**confirmed**

**RRPC.** A command is a PUBLISH to `/ext/rrpc/<id>/{pk}/{dn}/user/function/down_cmd` with a request id of
the sender's choosing; the box answers on the same topic. The gateway learns `{pk}` and `{dn}` from the
box's own SUBSCRIBE. **confirmed**

**Minimal broker.** CONNACK, SUBACK, PUBACK and PINGRESP are enough to keep the box connected and
reporting; a broker that never PUBLISHes is a complete passive monitor. Sending commands needs PUBLISH
to the subscribed topic. The box does not need its clock set to keep reporting. **confirmed**

## 2. Login bootstrap

Before MQTT, the box makes an HTTPS request to the auth host to learn which broker to use.

| Item | Value | Confidence |
|---|---|---|
| Paths | `POST /auth/bootstrap`, `POST /auth/devicename` | confirmed (bootstrap seen on this box; devicename from the reference SDK) |
| Accepted reply | JSON containing `"code":200` and `iotId`, `iotToken`, `host`, `port` anywhere in the body | confirmed |
| MQTT mode | `securemode=3` = plaintext TCP | confirmed |
| TLS | TLS 1.2 handshake completes against a **self-signed 2048-bit RSA** certificate | observed |
| Certificate validation | none on 04.22 (consistent with the SDK's `MBEDTLS_SSL_VERIFY_OPTIONAL` default) | observed |
| Blocked (not answered) login | the box re-joins Wi-Fi about every 40 s and sends nothing | unverified (reported for another box) |

The firmware's parser looks for the tokens rather than a fixed structure, so one reply shape serves both
paths. The responder in this repo sends a reply of this shape:

```json
{"code":200,"message":"success","data":{"iotId":"LOCAL-iotid","iotToken":"LOCAL-iottoken",
 "resources":{"mqtt":{"host":"<host-lan-ip>","port":1883}}}}
```

**This is the load-bearing assumption.** If a firmware update starts validating the certificate, the
login fails and the box never reaches the local broker. The method is "answer the login locally", not
"block the cloud": a blocked login leaves the box offline.

## 3. Frame envelope

Each MQTT payload is one binary frame.

| Bytes | Field | Value | Confidence |
|---|---|---|---|
| 0 | header | `FRAME_HEADER = 0xFC` | confirmed |
| 1–2 | length, big-endian | `length + 4 == total frame length` | confirmed |
| 3–4 | sequence | `IN_FRAME_SEQ = 0x0001` on every command frame recorded | observed |
| 5–10 | the box's MAC | | confirmed |
| 11 | kind | `KIND_CMD = 1` on commands to the box | confirmed |
| 12 | subsystem | `SUBSYSTEM = 0x5A` on every frame seen | confirmed |
| 13–14 | command, big-endian | e.g. `0x035A` heartbeat; the low byte is always `5a` | confirmed |
| 15 … n-2 | body | byte 0 is `01` in every frame type on this box | confirmed |
| n-1 | checksum | XOR of bytes 1 … n-2 | confirmed (0 bad frames in every capture) |

Synthetic example, a **Clean** command (`0x015A`, body `01 01`) for MAC `02:ca:7b:0c:00:01`:

```
fc | 00 0e | 00 01 | 02 ca 7b 0c 00 01 | 01 | 5a | 01 5a | 01 01 | b1
```

Length `0x000e` = 14 inner bytes, total 18. Checksum `b1` = XOR of everything between the header and
itself.

The reference decoder expects a leading body byte `00` and a 60-byte heartbeat body; it rejects this box's
frames as written.

## 4. Heartbeat `0x035A`

Body length is **`52 + 3 × slots`**: 64 bytes with 4 schedule slots, 67 with 5. Byte 28 holds the slot
count (`SLOT_COUNT_OFFSET = 28`), followed by 3 bytes per slot (hour, minute, day mask; `7f` = every
day, Sunday the lowest bit). **Every offset from 41 up (`SHIFT_AT = 41`) moves by +3 per slot beyond 4.**
A decoder must read the slot count first and refuse any slot count or length it has not validated; the
gateway accepts only 4 or 5 and reports anything else as `unknown_layout`.

Offsets are given for 4 slots, with the 5-slot offset where it differs.

| Offset (4 / 5 slots) | Meaning | Values | Confidence |
|---|---|---|---|
| 1 | litter type | `00` bentonite, `02` mixed | confirmed |
| 2 | state | `00` idle, `01` cleaning (mechanism running), `02` interrupted, `03` paused, `04` cancelling (~40 s, ends without `0x0B5A`) | observed |
| 4–5 | firmware version | read `04 22` on firmware 04.22 | hypothesis |
| 7 | clean delay, minutes | follows `0x085A` | hypothesis (followed the setting for all values tried but one) |
| 8 | occupancy | `26` occupied, `00` empty; `22` once, at an interruption | observed |
| 9 | levelling | `02` while levelling | observed (once) |
| **10** | **waste bin full** (IR light curtain) | `01` full, `00` clear; while full the light blinks and the box refuses to clean | **confirmed** |
| 11 | mode | `00` automatic, `01` manual, `02` scheduled | hypothesis (follows `0x045A`) |
| 13 | indicator light | `01` on, `00` off | confirmed |
| 14–15 | box clock, hour and minute | runs on its own time, hours off on the tested box; not set by the gateway | observed |
| 16–17 | live scale, kg and hundredths | reads the litter load when empty (well above 0); ~±0.01 kg noise | confirmed |
| 19 | night mode | `01` on, `00` off | confirmed |
| 20–23 | night start hour/minute, end hour/minute | after turning night mode off, byte 20 read `a8` once, cause unknown | hypothesis |
| **26** | **Wi-Fi RSSI**, signed dBm | e.g. `d0` = −48 | **confirmed** (not shifted) |
| 28 | schedule slot count, then 3 bytes per slot | | confirmed |
| 41 / 44 | touch / interruption | `01` while a cycle was interrupted by touch | observed |
| 42, 44 (4 slots) | the reference project's occupancy fields | always `00` here; they do not work on this box | observed |
| 45 / 48 | notification sound | `01` on, `00` off | confirmed |
| **46 / 49** | **waste drawer** | `01` pulled out, `00` in | **confirmed** |
| 47 / 50 | soft-stool burial | `01` on, `00` off; reads `00` outside automatic mode | confirmed |
| 48 / 51 | continuous cleaning | `01` on, `00` off | confirmed |
| 50 / 53 | unknown | went `00 → 01` when the drawer was put back | observed (once), not interpreted |
| 57 / 60 | unknown | `00` only in the first heartbeat after power-on | observed (once) |
| **58 / 61** | **burial delay, minutes** | follows `0x3C5A` | **confirmed** |
| 60 / 63 | visit counter | +1 per visit report, not per clean; **resets to 0 when the drawer is put back** | confirmed |

Not yet identified: deodorant level, litter low, error/anti-pinch, and readbacks for child lock and kitty
mode (their commands are acknowledged, but no heartbeat byte was seen to change).

## 5. Event frames from the box

### Visit report `0x025A` (`VISIT_COMMAND`)

Body `01 03 mm ss kg cg`. **confirmed** (layout); **observed** (meaning of the weight).

| Bytes | Meaning |
|---|---|
| 0–1 | `01 03` |
| 2–3 | duration, minutes and seconds |
| 4–5 | weight, kg and hundredths |

Synthetic example: `01 03 01 2a 03 15` = 1:42, 3.21 kg.

- **Duration** is `mm ss`. Reading bytes 2–3 as one big-endian number of seconds (as the reference
  project does) puts visit starts at moments when the occupancy flag was off and the scale flat.
- **Weight is not a reliable weight.** Early observations looked like a max-hold (jumps, landings and a hand
  on the box inflate it), but over a longer series it was within ±30 g of the cat only about a third of the
  time, often 100 g or more high, and sometimes far *below* the cat. The gateway publishes it only as
  `last_visit_peak` (the row's `peak`) and uses it as a cross-check.
- **Visit weight**: the median of the largest plateau of live cat-on-scale heartbeat readings during the
  visit (at least `0.15` kg above the baseline, inside the tracker's whole-one-cat window), minus the
  pre-visit empty baseline; no plateau, no weight. See [DESIGN.md](DESIGN.md) "Visit weight and deposit".
- **Timing**: the report arrives at the cat's exit; the occupancy flag runs 7–32 s longer than the reported
  duration, and after a cat stepped into a running clean cycle the report came 40–50 s after the occupancy
  ended.
- **Deposit estimate**: the shift in the empty baseline across the visit. Below 30 g it is reported as 0
  (scale noise). A higher empty level after a litter top-up is adopted only after 3 consecutive idle,
  unoccupied heartbeats agree within 0.10 kg. A deposit of a few tens of grams can show up as a baseline
  shift, but this is a rough estimate; the scale cannot tell urine from stool.

### End of clean `0x0B5A` (`CLEAN_END_COMMAND`)

| Body | Meaning | Confidence |
|---|---|---|
| `01 01` | after a clean started by the button or a command | confirmed |
| `01 04` | after levelling | confirmed |
| `01 00` | after a cycle labelled automatic | observed |

A cancelled cycle ends without `0x0B5A`. A manual clean took about 60 s from the first heartbeat flag to
`0x0B5A`.

### Drawer event `0x295A` (`DRAWER_EVENT_COMMAND`)

Body `01 01`, sent when the waste drawer was put back after being out; the visit counter reset to 0 at
the same moment. **observed** (once). Logged as `drawer_event`, not interpreted.

### Time request `0x2A5A` (`TIME_REQ_COMMAND`)

Body `01 00`, seen once and never retried. The gateway does not answer it.
**observed**

## 6. Commands to the box

All are PUBLISHed as frames with `kind = 1` to the RRPC topic. The box replies with body `01 00` on the
same topic.

| Command | Action | Body | Confidence |
|---|---|---|---|
| `0x015A` | clean / resume | `01 01` | confirmed |
| `0x015A` | pause | `01 00` | confirmed |
| `0x015A` | cancel | `01 08` | confirmed |
| `0x015A` | level litter | `01 04` | confirmed |
| `0x045A` | mode | `01 00` automatic, `01 01` manual; scheduled = `01 02` + slot count + `hh mm mask` per slot | confirmed (scheduled observed, not sent by the gateway) |
| `0x085A` | clean delay | `01` + minutes (app offers 1, 3, 5, 7, 10) | confirmed |
| `0x285A` | litter type | `01 00` bentonite, `01 02` mixed | confirmed |
| `0x0E5A` | night mode | `01 00` off; `01 01 hh mm hh mm` on with start and end | observed (not sent by the gateway) |
| `0x235A` | indicator light | `01 01` on, `01 00` off | confirmed |
| `0x075A` | notification sound | `01 01` / `01 00` | confirmed |
| `0x2F5A` | continuous cleaning | `01 01` / `01 00` | confirmed |
| `0x2E5A` | soft-stool burial | `01 01` / `01 00` | confirmed |
| `0x3C5A` | burial delay | `01` + minutes | confirmed |
| `0x055A` | child lock | `01 01` / `01 00` | observed (acknowledged; no readback) |
| `0x335A` | kitty mode | `01 01` / `01 00` | observed (acknowledged; no readback) |
| `0x245A` | info query | `01`; the reply carries signal text and the Wi-Fi network name | observed (not sent by the gateway) |
| `0x0D5A` | set clock | calendar fields: year−2000, month, day, weekday, hh, mm, ss | unverified (reference only; not sent) |

## 7. Behaviour notes

- **Touch pauses, then resumes.** A touch during a cycle set state `02` and the touch byte, then the cycle
  carried on by itself and ended normally. **observed**
- **Flags lag the scale.** A heartbeat showed a clearly loaded scale with occupancy and touch both `00`.
  The occupancy flag trails the scale by about one fast heartbeat at entry and exit. Never treat decoded
  flags as proof the box is empty. **observed**
- **No automatic clean off the cloud.** With the box on the local broker and in automatic mode, each
  visit was reported up as usual (`0x025A`), and the box never started a clean on its own afterwards,
  over many visits. Every clean that happened in that time was commanded (button, or a `0x015A` clean
  sent by the gateway). The reading is that, in automatic mode, the cloud sends the clean command after a
  visit report; hence the example automation. **observed**
- **Bin full blocks cleaning.** While byte 10 is `01` the light blinks and the box refuses to clean.
  **confirmed**
- **Drawer out is not "emptied".** Pulling the drawer out and back says nothing about whether the bin
  was emptied; nothing is derived from it. **confirmed**

## 8. Differences from the reference C08 project

| Aspect | Reference (C08) | Open X, firmware 04.22 |
|---|---|---|
| Heartbeat body | 60 bytes | `52 + 3 × slots` (64 with 4 slots, 67 with 5) |
| Leading body byte | `00` | `01` in every frame type |
| Visit report prefix | `00 03` | `01 03` |
| Visit duration | big-endian seconds | `mm ss` |
| End of clean | not described | `0x0B5A` with `01 00` / `01 01` / `01 04` |
| Host names used | vendor names | only the `-x` variants |
| Login | `/auth/devicename`, needs the vendor's HTTPS | `/auth/bootstrap` too; a local self-signed responder is accepted |
| Occupancy | offsets 42, 44 | offset 8; 42 and 44 read 0 |
| Scale | needs per-box calibration | empty reads the litter load; the box's visit weight is not reliable |

## 9. Mapping a new byte

1. Watch the diagnostic entities **Unmapped heartbeat bytes**, **Unmapped bytes changed** and **Unmapped
   change count**: they move whenever a byte outside the field table changes.
2. Do one thing on the box (or wait for the event), note the minute, then look at the full frames:
   `journalctl -u catlink-gateway -o cat --since "-10 min" | grep '"event":"publish"'`
3. Diff the heartbeat bodies before and after. Remember the +3-per-slot shift from offset 41.
4. Add one `HFIELD` line to `HEARTBEAT_FIELDS` in `catlink_local/protocol.py`, one discovery entry in
   `catlink_local/gateway.py` if it should appear in HA, and one synthetic test vector in `tests/`.
5. Report it as described in [CONTRIBUTING.md](../CONTRIBUTING.md), without your device's identifiers.
