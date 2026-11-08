"""SYNTHETIC frame builders for the tests. Nothing here comes from a real device or a capture.

Every frame is assembled byte by byte from the documented layout (docs/PROTOCOL.md):

    FC | len (2, BE) | seq (2) | MAC (6) | kind (1) | 5A | command (2, BE) | body ... | XOR (1)

The MAC is a locally administered synthetic address; the productKey/deviceName are plain placeholders.
The checksum is computed here independently of catlink_local.protocol so the tests cross-check it.
"""

TEST_MAC = "02:ca:7b:0c:00:01"
TEST_MAC_BYTES = bytes.fromhex(TEST_MAC.replace(":", ""))
OTHER_MAC_BYTES = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x99])   # synthetic "some other box"
PK, DN = "testpk", "testdn"
CMD_TOPIC = "/ext/rrpc/+/%s/%s/user/function/down_cmd" % (PK, DN)
REPLY_TOPIC = "/ext/rrpc/+/%s/%s/user/heartbeat/reply" % (PK, DN)
HB_TOPIC = "%s/%s/user/heartbeat/up" % (PK, DN)
FN_TOPIC = "%s/%s/user/function/up" % (PK, DN)

HEARTBEAT = 0x035A
VISIT = 0x025A
CLEAN_END = 0x0B5A
DRAWER_EVENT = 0x295A
TIME_REQUEST = 0x2A5A

# Arbitrary schedule-table filler, 3 bytes per slot (meaning not decoded; the gateway only skips it).
_SCHEDULE_FILL = (6, 30, 0x7F, 11, 15, 0x7F, 15, 45, 0x7F, 19, 0, 0x7F, 21, 30, 0x7F)


def xor(data):
    result = 0
    for value in data:
        result ^= value
    return result


def box_frame(command, body, mac=TEST_MAC, kind=0x00, seq=0x0001):
    """A box -> host frame. `mac` may be a "xx:..." string or 6 bytes."""
    mac_bytes = bytes.fromhex(mac.replace(":", "")) if isinstance(mac, str) else bytes(mac)
    inner = seq.to_bytes(2, "big") + mac_bytes + bytes([kind, 0x5A]) + command.to_bytes(2, "big") + bytes(body)
    pre = bytes([0xFC]) + len(inner).to_bytes(2, "big") + inner
    return pre + bytes([xor(pre[1:])])


def heartbeat_body(state=0, occ=0, touch=0, kg=1, cg=48, visit=4, mode=0, litter=2, light=1, extra=0,
                   slots=4, bin_full=0, drawer=0, rssi=-60, clock_h=9, clock_m=15, clean_delay=3,
                   night=1, night_start=22, night_end=6, sound=1, burial=1, continuous=0, burial_minutes=5):
    """A heartbeat body (52 + 3*slots bytes). Offsets >= 41 shift by 3 per schedule slot beyond 4."""
    s = 3 * (slots - 4)
    body = bytearray(52 + 3 * slots)
    body[0] = 0x01
    body[1] = litter
    body[2] = state
    body[7] = clean_delay
    body[8] = occ
    body[10] = bin_full       # waste bin full (fixed offset)
    body[11] = mode
    body[13] = light
    body[14] = clock_h
    body[15] = clock_m
    body[16] = kg
    body[17] = cg
    body[19] = night
    body[20] = night_start
    body[22] = night_end
    body[26] = rssi & 0xFF    # Wi-Fi RSSI, signed byte (fixed offset)
    body[28] = slots & 0xFF   # slot count
    for i, v in enumerate(_SCHEDULE_FILL[:3 * slots]):
        body[29 + i] = v      # schedule table
    if 41 + s < len(body):
        body[41 + s] = touch
        body[45 + s] = sound
        body[46 + s] = drawer  # waste drawer out (1) / in (0); 49 with 5 slots
        body[47 + s] = burial
        body[48 + s] = continuous
        body[55 + s] = extra   # an UNMAPPED byte, flipped to test the diagnostic channel
        body[58 + s] = burial_minutes
        body[60 + s] = visit
    return bytes(body)


def heartbeat(mac=TEST_MAC, **fields):
    return box_frame(HEARTBEAT, heartbeat_body(**fields), mac=mac)


def visit_frame(mm, ss, kg, cg, mac=TEST_MAC):
    """Visit report: body 01 03 mm ss kg cg (duration m:s, box weight kg + hundredths, not reliable)."""
    return box_frame(VISIT, bytes([0x01, 0x03, mm, ss, kg, cg]), mac=mac)


def clean_end_frame(body=b"\x01\x01", mac=TEST_MAC):
    return box_frame(CLEAN_END, body, mac=mac)


def drawer_event_frame(mac=TEST_MAC):
    return box_frame(DRAWER_EVENT, b"\x01\x01", mac=mac)


def time_request_frame(mac=TEST_MAC):
    return box_frame(TIME_REQUEST, b"\x01\x00", mac=mac)


def corrupt(frame):
    """Flip every bit of the checksum byte."""
    bad = bytearray(frame)
    bad[-1] ^= 0xFF
    return bytes(bad)
