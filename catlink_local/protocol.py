"""CATLINK Open X frame protocol: envelope, heartbeat decoder, state builder, command frames.

Pure functions and constants; nothing here opens a socket, logs, or reads configuration. The box MAC is
always passed in by the caller. The write-up with confidence levels for every field is docs/PROTOCOL.md.

Frame envelope (every frame in both directions):

    FC | len (2, BE) | seq (2) | MAC (6) | kind (1) | 5A | command (2, BE) | body ... | XOR (1)

`len` counts the bytes between the length field and the checksum; the XOR covers everything after the
FC header up to the checksum. Body byte 0 is 01 in every frame seen from this box.
"""
import collections
import re

# ----------------------------------------------------------------------------------------------------
# Envelope + frame selectors (generic; no device identifiers)
# ----------------------------------------------------------------------------------------------------
FRAME_HEADER = 0xFC
SUBSYSTEM = 0x5A
KIND_CMD = 1
IN_FRAME_SEQ = 0x0001           # constant on every command frame recorded from this box
HEARTBEAT_COMMAND = 0x035A
VISIT_COMMAND = 0x025A
CLEAN_END_COMMAND = 0x0B5A      # body 0100 automatic clean, 0101 button/app, 0104 level
TIME_REQ_COMMAND = 0x2A5A
DRAWER_EVENT_COMMAND = 0x295A   # body 0101 seen at waste-drawer reinsertion; known + logged, not
                                # interpreted (nor is the heartbeat byte that flips with it)

# The box's own command-topic subscription: /ext/rrpc/+/{productKey}/{deviceName}/user/function/down_cmd.
# The gateway learns {pk}/{dn} from it at runtime; they are never configured or stored.
COMMAND_TOPIC_RE = re.compile(r"/ext/rrpc/\+/([^/+#]+)/([^/+#]+)/user/function/down_cmd")

# ----------------------------------------------------------------------------------------------------
# Heartbeat layout
# ----------------------------------------------------------------------------------------------------
KNOWN_SLOT_COUNTS = (4, 5)
SLOT_COUNT_OFFSET = 28  # body offset of the schedule slot count; schedule is 3 bytes/slot after it
SHIFT_AT = 41           # body offsets >= this shift by 3*(slots-4)


def heartbeat_body_len(slots):
    return 52 + 3 * slots


def xor_of(data):
    result = 0
    for value in data:
        result ^= value
    return result


# ====================================================================================================
# DECODER (sensor half) -- table-driven so a newly-identified field is a one-line addition.
# ====================================================================================================
# Each HFIELD is (offset_4slot, name, kind). `offset_4slot` is the body offset assuming the 4-slot
# baseline; any offset >= SHIFT_AT is shifted by +3 per extra schedule slot at decode time. `kind`:
#   "u8"     raw byte value
#   "s8"     signed byte (two's complement), e.g. the Wi-Fi RSSI in dBm
#   "onoff"  1 -> True, 0 -> False, else None
# Deriving display values (state text, occupied, cleaning, scale, clock) happens in build_state() from
# these raw fields, so the raw layer stays a pure table.
#
# To MAP A NEW SIGNAL: add one HFIELD line here and (if it should show in HA) one discovery line in
# gateway.sensor_discovery(), plus a test vector. Nothing else changes. Known-but-UNMAPPED readouts are
# deliberately NOT invented:
#   RESERVED / not yet mapped on this firmware -- add an HFIELD when the byte is identified:
#     deodorant             (deodorant/scent cartridge low)
#     litter_low            (litter level low)
#     error                 (fault / anti-pinch / safety trip)
#     child_lock            (0x055a command exists; no heartbeat byte identified)
#     kitty                 (0x335a command exists; no heartbeat byte identified)
#   These stay unexposed until a byte is confirmed. Meanwhile their state still reaches the log: the full
#   heartbeat hex is logged every ~100 s, and the diagnostic "unmapped bytes" entity surfaces any byte
#   not covered by the table, so an event can be caught and mapped from history.
HFIELD = collections.namedtuple("HField", "offset name kind")
HEARTBEAT_FIELDS = [
    HFIELD(1, "litter_type_raw", "u8"),     # 0x285a: 0 bentonite, 2 mixed
    HFIELD(2, "state_raw", "u8"),           # 0 idle,1 running,2 interrupted,3 paused,4 cancelling
    HFIELD(7, "clean_delay", "u8"),         # minutes (0x085a)
    HFIELD(8, "occupied_raw", "u8"),        # 0x26 occupied, else 00 (0x22 seen once at an interruption)
    HFIELD(10, "bin_full", "onoff"),        # waste bin full (IR curtain); box refuses to clean while 1.
                                            # Fixed offset (before the slot block).
    HFIELD(11, "mode_raw", "u8"),           # 0 auto,1 manual,2 scheduled (0x045a)
    HFIELD(13, "light", "onoff"),           # 0x235a
    HFIELD(14, "clock_h", "u8"),
    HFIELD(15, "clock_m", "u8"),
    HFIELD(16, "scale_kg", "u8"),
    HFIELD(17, "scale_cg", "u8"),
    HFIELD(19, "night_mode", "onoff"),      # 0x0e5a
    HFIELD(20, "night_start", "u8"),        # quiet-hours start hour (low confidence; see docs/PROTOCOL.md)
    HFIELD(22, "night_end", "u8"),          # quiet-hours end hour (low confidence)
    HFIELD(26, "wifi_rssi", "s8"),          # Wi-Fi RSSI, signed dBm. Fixed offset.
    HFIELD(SLOT_COUNT_OFFSET, "slot_count", "u8"),
    HFIELD(41, "touch_raw", "u8"),          # shifts with slot count; used by the movement interlock
    HFIELD(45, "sound", "onoff"),           # 0x075a; shifts
    HFIELD(46, "drawer_out", "onoff"),      # waste drawer: 1 = pulled out, 0 = in. Shifts (49 with 5 slots).
                                            # Pulling it does NOT mean it was emptied, so nothing is derived.
    HFIELD(47, "burial", "onoff"),          # 0x2e5a soft-stool burial; shifts
    HFIELD(48, "continuous", "onoff"),      # 0x2f5a; shifts
    HFIELD(58, "burial_minutes", "u8"),     # 0x3c5a burial delay in minutes; shifts
    HFIELD(60, "visit_count", "u8"),        # +1 per visit report, 0 after drawer reinsertion; shifts
]

LITTER_TYPES = {0: "bentonite", 2: "mixed"}
MODES = {0: "automatic", 1: "manual", 2: "scheduled"}
STATES = {0: "idle", 1: "cleaning", 2: "interrupted", 3: "paused", 4: "cancelling"}


def resolved_offset(offset_4slot, slots):
    return offset_4slot + (slots - 4) * 3 if offset_4slot >= SHIFT_AT else offset_4slot


def frame_fields(payload, box_mac_bytes):
    """Validate the envelope and return (command, body) or (None, reason). Never raises."""
    if len(payload) < 16 or payload[0] != FRAME_HEADER:
        return None, "bad_header"
    if int.from_bytes(payload[1:3], "big") + 4 != len(payload):
        return None, "bad_length"
    if xor_of(payload[1:-1]) != payload[-1]:
        return None, "bad_checksum"
    if payload[5:11] != box_mac_bytes:
        return None, "not_this_box"
    return int.from_bytes(payload[13:15], "big"), payload[15:-1]


def decode_heartbeat(body):
    """Return a dict of decoded raw fields + bookkeeping, or {'valid': False, 'reason': ...}.

    Pure and defensive: unknown layouts are reported, never guessed; this never raises on bad input."""
    if len(body) <= SLOT_COUNT_OFFSET:
        return {"valid": False, "reason": "short_body"}
    slots = body[SLOT_COUNT_OFFSET]
    if slots not in KNOWN_SLOT_COUNTS or len(body) != 52 + 3 * slots:
        return {"valid": False, "reason": "unknown_layout", "slot_count": slots, "body_len": len(body)}
    raw = {}
    covered = set()
    for f in HEARTBEAT_FIELDS:
        off = resolved_offset(f.offset, slots)
        if off >= len(body):
            continue
        covered.add(off)
        value = body[off]
        if f.kind == "onoff":
            raw[f.name] = bool(value == 1) if value in (0, 1) else None
        elif f.kind == "s8":
            raw[f.name] = value - 256 if value >= 128 else value
        else:
            raw[f.name] = value
    # The schedule table (slot count byte + 3 bytes per slot) is "covered" even though it is not a field.
    for off in range(SLOT_COUNT_OFFSET, SLOT_COUNT_OFFSET + 1 + 3 * slots):
        covered.add(off)
    unmapped = {str(i): body[i] for i in range(len(body)) if i not in covered}
    raw["valid"] = True
    raw["reason"] = "ok"
    raw["slots"] = slots
    raw["_unmapped"] = unmapped
    return raw


def build_state(raw, prev):
    """Turn decoded raw fields into the HA-facing state dict. `prev` is the previous published state."""
    kg = raw.get("scale_kg")
    cg = raw.get("scale_cg")
    scale = round(kg + cg / 100.0, 2) if kg is not None and cg is not None else None
    state_raw = raw.get("state_raw")
    occ = raw.get("occupied_raw")
    state = dict(prev) if prev else {}
    state.update({
        "state": STATES.get(state_raw, "unknown_%s" % state_raw),
        "cleaning": bool(state_raw == 1),
        "occupied": bool(occ not in (0, None)),
        "mode": MODES.get(raw.get("mode_raw"), "unknown"),
        "litter_type": LITTER_TYPES.get(raw.get("litter_type_raw"), "unknown"),
        "clean_delay": raw.get("clean_delay"),
        "night_mode": raw.get("night_mode"),
        "night_start": raw.get("night_start"),
        "night_end": raw.get("night_end"),
        "light": raw.get("light"),
        "sound": raw.get("sound"),
        "continuous": raw.get("continuous"),
        "burial": raw.get("burial"),
        "burial_minutes": raw.get("burial_minutes"),
        "visit_count": raw.get("visit_count"),
        "live_scale": scale,
        "slot_count": raw.get("slot_count"),
        "bin_full": raw.get("bin_full"),
        "drawer_out": raw.get("drawer_out"),
        "wifi_signal": raw.get("wifi_rssi"),
    })
    h, m = raw.get("clock_h"), raw.get("clock_m")
    if h is not None and m is not None and h < 24 and m < 60:
        state["box_clock"] = "%02d:%02d" % (h, m)
    return state


# ====================================================================================================
# COMMAND FRAMES
# ====================================================================================================
def build_box_frame(command, body, box_mac_bytes):
    """Build a command frame for the box: FC | len | seq=0001 | mac | 01 | 5a | cmd | body | xor."""
    inner = (IN_FRAME_SEQ.to_bytes(2, "big") + bytes(box_mac_bytes)
             + bytes([KIND_CMD, SUBSYSTEM]) + command.to_bytes(2, "big") + bytes(body))
    pre = bytes([FRAME_HEADER]) + len(inner).to_bytes(2, "big") + inner
    return pre + bytes([xor_of(pre[1:])])


# Routing table: object_id -> how to turn an HA payload into a box command. `gate` picks the interlock:
#   "move"   -> clean/level: the full idle+empty+no-touch+fresh+scale interlock (latest heartbeat)
#   "resume" -> resume only: paused+not-occupied+not-touched+fresh (a paused box is never idle)
#   "live"   -> pause/cancel + all settings: session-live+subscribed only
# All generic selectors.
CMD = collections.namedtuple("Cmd", "command gate builder")


def _onoff_body(payload):
    p = payload.strip().upper()
    if p in ("ON", "1", "TRUE"):
        return b"\x01\x01"
    if p in ("OFF", "0", "FALSE"):
        return b"\x01\x00"
    raise ValueError("expected ON/OFF")


def _const(body):
    return lambda payload: body


def _mode_body(payload):
    # Scheduled is intentionally NOT settable here: it needs a slot table that is not synthesised.
    m = {"automatic": b"\x01\x00", "auto": b"\x01\x00", "manual": b"\x01\x01"}.get(payload.strip().lower())
    if m is None:
        raise ValueError("mode must be automatic or manual")
    return m


def _litter_body(payload):
    m = {"bentonite": b"\x01\x00", "mixed": b"\x01\x02"}.get(payload.strip().lower())
    if m is None:
        raise ValueError("litter_type must be bentonite or mixed")
    return m


def _delay_body(payload):
    # HA `number` entities publish a float string ("5.0"); round-trip through float so int() never raises.
    minutes = int(round(float(payload)))
    if minutes not in (1, 3, 5, 7, 10):
        raise ValueError("clean_delay must be one of 1,3,5,7,10")
    return bytes([0x01, minutes])


def _burial_minutes_body(payload):
    minutes = int(round(float(payload)))  # tolerate the "5.0" a number entity sends (see _delay_body)
    if not 1 <= minutes <= 60:
        raise ValueError("burial_minutes out of range")
    return bytes([0x01, minutes])


COMMANDS = {
    # movement (clean/level: full interlock; resume: its own paused gate)
    "clean":  CMD(0x015A, "move", _const(b"\x01\x01")),
    "pause":  CMD(0x015A, "live", _const(b"\x01\x00")),
    "resume": CMD(0x015A, "resume", _const(b"\x01\x01")),
    "cancel": CMD(0x015A, "live", _const(b"\x01\x08")),
    "level":  CMD(0x015A, "move", _const(b"\x01\x04")),
    # settings (session-live only)
    "mode":           CMD(0x045A, "live", _mode_body),
    "litter_type":    CMD(0x285A, "live", _litter_body),
    "clean_delay":    CMD(0x085A, "live", _delay_body),
    "light":          CMD(0x235A, "live", _onoff_body),
    "sound":          CMD(0x075A, "live", _onoff_body),
    "continuous":     CMD(0x2F5A, "live", _onoff_body),
    "burial":         CMD(0x2E5A, "live", _onoff_body),
    "burial_minutes": CMD(0x3C5A, "live", _burial_minutes_body),
    "child_lock":     CMD(0x055A, "live", _onoff_body),
    "kitty":          CMD(0x335A, "live", _onoff_body),
    # Night mode: OFF is 0x0e5a 0100. ON needs start/end hours; not exposed as a plain switch here to
    # avoid synthesising hours. A future HA number/time pair can add it via this same table.
}
