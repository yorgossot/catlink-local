"""Pure unit tests of catlink_local.protocol (no sockets, no config)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vectors as v  # noqa: E402
from catlink_local import protocol as p  # noqa: E402


def body_of(frame):
    command, body = p.frame_fields(frame, v.TEST_MAC_BYTES)
    assert command is not None, body
    return body


class EnvelopeTests(unittest.TestCase):
    def test_good_frame_round_trips(self):
        frame = v.box_frame(0x035A, b"\x01\x02\x03")
        self.assertEqual(p.frame_fields(frame, v.TEST_MAC_BYTES), (0x035A, b"\x01\x02\x03"))

    def test_wrong_header(self):
        frame = bytearray(v.heartbeat())
        frame[0] = 0xFD
        self.assertEqual(p.frame_fields(bytes(frame), v.TEST_MAC_BYTES), (None, "bad_header"))

    def test_too_short_is_bad_header(self):
        self.assertEqual(p.frame_fields(b"\xfc\x00\x01", v.TEST_MAC_BYTES), (None, "bad_header"))
        self.assertEqual(p.frame_fields(b"", v.TEST_MAC_BYTES), (None, "bad_header"))

    def test_wrong_length(self):
        frame = v.heartbeat() + b"\x00"
        self.assertEqual(p.frame_fields(frame, v.TEST_MAC_BYTES), (None, "bad_length"))

    def test_wrong_checksum(self):
        self.assertEqual(p.frame_fields(v.corrupt(v.heartbeat()), v.TEST_MAC_BYTES), (None, "bad_checksum"))

    def test_other_mac(self):
        frame = v.heartbeat(mac=v.OTHER_MAC_BYTES)
        self.assertEqual(p.frame_fields(frame, v.TEST_MAC_BYTES), (None, "not_this_box"))

    def test_xor_matches_independent_implementation(self):
        frame = v.visit_frame(1, 42, 3, 21)
        self.assertEqual(p.xor_of(frame[1:-1]), v.xor(frame[1:-1]))
        self.assertEqual(p.xor_of(frame[1:-1]), frame[-1])


class HeartbeatDecodeTests(unittest.TestCase):
    FIELDS = dict(state=1, occ=0x26, touch=1, kg=2, cg=7, visit=9, mode=1, litter=0, light=0,
                  bin_full=1, drawer=1, rssi=-48, clock_h=13, clock_m=5, clean_delay=7,
                  night=0, night_start=21, night_end=8, sound=0, burial=0, continuous=1, burial_minutes=12)

    def decode(self, **kw):
        return p.decode_heartbeat(v.heartbeat_body(**kw))

    def test_4_and_5_slots_decode_the_same_logical_values(self):
        four = self.decode(slots=4, **self.FIELDS)
        five = self.decode(slots=5, **self.FIELDS)
        self.assertTrue(four["valid"] and five["valid"])
        self.assertEqual((four["slots"], five["slots"]), (4, 5))
        for f in p.HEARTBEAT_FIELDS:
            if f.name == "slot_count":
                continue
            self.assertEqual(four[f.name], five[f.name], f.name)
        expected = {"state_raw": 1, "occupied_raw": 0x26, "touch_raw": 1, "scale_kg": 2, "scale_cg": 7,
                    "visit_count": 9, "mode_raw": 1, "litter_type_raw": 0, "light": False, "bin_full": True,
                    "drawer_out": True, "wifi_rssi": -48, "clock_h": 13, "clock_m": 5, "clean_delay": 7,
                    "night_mode": False, "night_start": 21, "night_end": 8, "sound": False, "burial": False,
                    "continuous": True, "burial_minutes": 12}
        for name, value in expected.items():
            self.assertEqual(four[name], value, name)

    def test_fixed_offsets_do_not_shift(self):
        body = bytearray(v.heartbeat_body(slots=5))
        body[10] = 1
        body[26] = 0xD0
        raw = p.decode_heartbeat(bytes(body))
        self.assertIs(raw["bin_full"], True)
        self.assertEqual(raw["wifi_rssi"], -48)

    def test_shifted_offsets_with_5_slots(self):
        body = bytearray(v.heartbeat_body(slots=5, drawer=0, burial_minutes=0, visit=0))
        body[49] = 1     # drawer: 46 + 3
        body[61] = 17    # burial minutes: 58 + 3
        body[63] = 33    # visit counter: 60 + 3
        raw = p.decode_heartbeat(bytes(body))
        self.assertEqual((raw["drawer_out"], raw["burial_minutes"], raw["visit_count"]), (True, 17, 33))

    def test_unknown_slot_counts_and_mismatched_length(self):
        for slots in (3, 6):
            raw = p.decode_heartbeat(v.heartbeat_body(slots=slots))
            self.assertEqual((raw["valid"], raw["reason"], raw["slot_count"]), (False, "unknown_layout", slots))
        body = v.heartbeat_body(slots=4) + b"\x00"
        raw = p.decode_heartbeat(body)
        self.assertEqual((raw["valid"], raw["reason"], raw["body_len"]), (False, "unknown_layout", 65))
        body = bytearray(v.heartbeat_body(slots=4))
        body[28] = 5     # says 5 slots but is 4-slot sized
        self.assertEqual(p.decode_heartbeat(bytes(body))["reason"], "unknown_layout")

    def test_short_body(self):
        self.assertEqual(p.decode_heartbeat(bytes(28)), {"valid": False, "reason": "short_body"})
        self.assertEqual(p.decode_heartbeat(b""), {"valid": False, "reason": "short_body"})

    def test_unmapped_excludes_schedule_and_mapped_offsets(self):
        for slots in (4, 5):
            raw = p.decode_heartbeat(v.heartbeat_body(slots=slots))
            unmapped = {int(k) for k in raw["_unmapped"]}
            schedule = set(range(p.SLOT_COUNT_OFFSET, p.SLOT_COUNT_OFFSET + 1 + 3 * slots))
            mapped = {p.resolved_offset(f.offset, slots) for f in p.HEARTBEAT_FIELDS}
            self.assertFalse(unmapped & schedule)
            self.assertFalse(unmapped & mapped)
            self.assertEqual(unmapped | schedule | mapped, set(range(p.heartbeat_body_len(slots))))
            self.assertIn(str(55 + 3 * (slots - 4)), raw["_unmapped"])

    def test_unmapped_byte_value_is_reported(self):
        raw = p.decode_heartbeat(v.heartbeat_body(extra=0x42))
        self.assertEqual(raw["_unmapped"]["55"], 0x42)

    def test_signed_rssi(self):
        self.assertEqual(self.decode(rssi=-48)["wifi_rssi"], -48)
        body = bytearray(v.heartbeat_body())
        body[26] = 0xD0
        self.assertEqual(p.decode_heartbeat(bytes(body))["wifi_rssi"], -48)
        body[26] = 0x05
        self.assertEqual(p.decode_heartbeat(bytes(body))["wifi_rssi"], 5)

    def test_onoff_other_values_are_none(self):
        body = bytearray(v.heartbeat_body())
        body[13] = 2
        body[10] = 0xFF
        raw = p.decode_heartbeat(bytes(body))
        self.assertIsNone(raw["light"])
        self.assertIsNone(raw["bin_full"])


class BuildStateTests(unittest.TestCase):
    def state(self, prev=None, **kw):
        return p.build_state(p.decode_heartbeat(v.heartbeat_body(**kw)), prev)

    def test_strings(self):
        st = self.state(state=0, mode=0, litter=2)
        self.assertEqual((st["state"], st["mode"], st["litter_type"]), ("idle", "automatic", "mixed"))
        st = self.state(state=3, mode=1, litter=0)
        self.assertEqual((st["state"], st["mode"], st["litter_type"]), ("paused", "manual", "bentonite"))
        st = self.state(state=4, mode=2)
        self.assertEqual((st["state"], st["mode"]), ("cancelling", "scheduled"))
        self.assertEqual(self.state(state=2)["state"], "interrupted")

    def test_unknown_fallbacks(self):
        st = self.state(state=9, mode=7, litter=5)
        self.assertEqual((st["state"], st["mode"], st["litter_type"]), ("unknown_9", "unknown", "unknown"))
        self.assertEqual(p.build_state({}, None)["state"], "unknown_None")

    def test_cleaning_and_occupied(self):
        st = self.state(state=1, occ=0)
        self.assertEqual((st["cleaning"], st["occupied"]), (True, False))
        st = self.state(state=0, occ=0x26)
        self.assertEqual((st["cleaning"], st["occupied"]), (False, True))
        self.assertIs(self.state(occ=0x22)["occupied"], True)

    def test_clock_formatting_and_guard(self):
        self.assertEqual(self.state(clock_h=9, clock_m=5)["box_clock"], "09:05")
        self.assertNotIn("box_clock", self.state(clock_h=24, clock_m=0))
        self.assertNotIn("box_clock", self.state(clock_h=23, clock_m=60))
        prev = {"box_clock": "01:02"}
        self.assertEqual(self.state(prev=prev, clock_h=25)["box_clock"], "01:02")

    def test_scale_rounding(self):
        self.assertEqual(self.state(kg=3, cg=21)["live_scale"], 3.21)
        self.assertEqual(self.state(kg=1, cg=7)["live_scale"], 1.07)
        self.assertEqual(self.state(kg=0, cg=99)["live_scale"], 0.99)

    def test_contract_keys(self):
        st = self.state()
        for key in ("state", "cleaning", "occupied", "mode", "litter_type", "clean_delay", "night_mode",
                    "night_start", "night_end", "light", "sound", "continuous", "burial", "burial_minutes",
                    "visit_count", "live_scale", "slot_count", "bin_full", "drawer_out", "wifi_signal",
                    "box_clock"):
            self.assertIn(key, st)

    def test_prev_is_kept_and_not_mutated(self):
        prev = {"last_visit_weight": 3.21}
        st = self.state(prev=prev)
        self.assertEqual(st["last_visit_weight"], 3.21)
        self.assertEqual(prev, {"last_visit_weight": 3.21})


class BoxFrameTests(unittest.TestCase):
    def test_layout_and_checksum(self):
        frame = p.build_box_frame(0x015A, b"\x01\x01", v.TEST_MAC_BYTES)
        self.assertEqual(frame[0], 0xFC)
        self.assertEqual(int.from_bytes(frame[1:3], "big"), len(frame) - 4)
        self.assertEqual(frame[3:5], b"\x00\x01")
        self.assertEqual(frame[5:11], v.TEST_MAC_BYTES)
        self.assertEqual(frame[11], 0x01)
        self.assertEqual(frame[12], 0x5A)
        self.assertEqual(frame[13:15], b"\x01\x5a")
        self.assertEqual(frame[15:-1], b"\x01\x01")
        self.assertEqual(frame[-1], v.xor(frame[1:-1]))
        self.assertEqual(p.frame_fields(frame, v.TEST_MAC_BYTES), (0x015A, b"\x01\x01"))
        self.assertEqual(len(frame), 18)

    def test_other_mac_is_rejected_by_frame_fields(self):
        frame = p.build_box_frame(0x015A, b"\x01\x01", v.OTHER_MAC_BYTES)
        self.assertEqual(p.frame_fields(frame, v.TEST_MAC_BYTES), (None, "not_this_box"))


class CommandBuilderTests(unittest.TestCase):
    def build(self, name, payload):
        return p.COMMANDS[name].builder(payload)

    def test_movement(self):
        self.assertEqual(p.COMMANDS["clean"], p.CMD(0x015A, "move", p.COMMANDS["clean"].builder))
        self.assertEqual(self.build("clean", "PRESS"), b"\x01\x01")
        self.assertEqual(self.build("pause", "PRESS"), b"\x01\x00")
        self.assertEqual(self.build("resume", "PRESS"), b"\x01\x01")
        self.assertEqual(self.build("cancel", "PRESS"), b"\x01\x08")
        self.assertEqual(self.build("level", "PRESS"), b"\x01\x04")
        gates = {k: c.gate for k, c in p.COMMANDS.items()}
        self.assertEqual((gates["clean"], gates["level"], gates["resume"], gates["pause"], gates["cancel"]),
                         ("move", "move", "resume", "live", "live"))
        for name in ("mode", "litter_type", "clean_delay", "light", "sound", "continuous", "burial",
                     "burial_minutes", "child_lock", "kitty"):
            self.assertEqual(gates[name], "live", name)

    def test_selectors(self):
        expected = {"clean": 0x015A, "mode": 0x045A, "litter_type": 0x285A, "clean_delay": 0x085A,
                    "light": 0x235A, "sound": 0x075A, "continuous": 0x2F5A, "burial": 0x2E5A,
                    "burial_minutes": 0x3C5A, "child_lock": 0x055A, "kitty": 0x335A}
        for name, command in expected.items():
            self.assertEqual(p.COMMANDS[name].command, command, name)

    def test_onoff(self):
        for payload in ("ON", "on", "1", "TRUE", " true "):
            self.assertEqual(self.build("light", payload), b"\x01\x01")
        for payload in ("OFF", "off", "0", "FALSE"):
            self.assertEqual(self.build("sound", payload), b"\x01\x00")
        with self.assertRaises(ValueError):
            self.build("kitty", "maybe")

    def test_mode(self):
        self.assertEqual(self.build("mode", "automatic"), b"\x01\x00")
        self.assertEqual(self.build("mode", "auto"), b"\x01\x00")
        self.assertEqual(self.build("mode", "Manual"), b"\x01\x01")
        with self.assertRaises(ValueError):
            self.build("mode", "scheduled")
        with self.assertRaises(ValueError):
            self.build("mode", "{bad json")

    def test_clean_delay(self):
        for minutes in (1, 3, 5, 7, 10):
            self.assertEqual(self.build("clean_delay", str(minutes)), bytes([1, minutes]))
        self.assertEqual(self.build("clean_delay", "5.0"), b"\x01\x05")
        for bad in ("2", "0", "11", "99"):
            with self.assertRaises(ValueError):
                self.build("clean_delay", bad)
        with self.assertRaises(ValueError):
            self.build("clean_delay", "soon")

    def test_burial_minutes(self):
        self.assertEqual(self.build("burial_minutes", "1"), b"\x01\x01")
        self.assertEqual(self.build("burial_minutes", "60"), b"\x01\x3c")
        self.assertEqual(self.build("burial_minutes", "5.0"), b"\x01\x05")
        for bad in ("0", "61"):
            with self.assertRaises(ValueError):
                self.build("burial_minutes", bad)

    def test_litter_type(self):
        self.assertEqual(self.build("litter_type", "bentonite"), b"\x01\x00")
        self.assertEqual(self.build("litter_type", "MIXED"), b"\x01\x02")
        with self.assertRaises(ValueError):
            self.build("litter_type", "silica")


class OffsetTests(unittest.TestCase):
    def test_resolved_offset_table(self):
        table = [(1, 4, 1), (10, 5, 10), (26, 5, 26), (28, 5, 28), (40, 5, 40), (41, 4, 41), (41, 5, 44),
                 (46, 5, 49), (50, 5, 53), (58, 5, 61), (60, 5, 63), (60, 4, 60)]
        for off, slots, expected in table:
            self.assertEqual(p.resolved_offset(off, slots), expected, (off, slots))

    def test_body_len(self):
        self.assertEqual((p.heartbeat_body_len(4), p.heartbeat_body_len(5)), (64, 67))

    def test_command_topic_regex(self):
        m = p.COMMAND_TOPIC_RE.fullmatch(v.CMD_TOPIC)
        self.assertEqual((m[1], m[2]), (v.PK, v.DN))
        self.assertIsNone(p.COMMAND_TOPIC_RE.fullmatch(v.REPLY_TOPIC))


if __name__ == "__main__":
    unittest.main()
