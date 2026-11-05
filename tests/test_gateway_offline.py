"""Offline end-to-end test of the gateway, plus startup/config validation.

Runs the REAL gateway (`python3 -I -u gateway.py`) as an isolated subprocess against loopback fakes on
free high ports; it never touches a real box or a real Mosquitto. State files live in a temp dir.

Fakes:
  * a minimal MQTT broker (the "HA sink") on 127.0.0.1:<p>, which checks the gateway's username+password,
    collects its discovery/state/diag/availability PUBLISHes, and can inject command PUBLISHes back;
  * a fake box client that connects to the gateway's box port, subscribes to the command topic (so the
    gateway learns {pk}/{dn}), and replays synthetic heartbeat/visit/unknown/corrupt frames (vectors.py).

Proves, in sensor-only (CATLINK_COMMANDS=0) and command (=1) modes, with and without CATLINK_CATS:
  1. correct HA discovery (read-only set in sensor mode; actuators added only in command mode);
  2. correct state decoding of a heartbeat and a visit report, INCLUDING the settled-median visit weight
     (not the box max-hold peak) and the sustained-litter-top-up baseline adoption;
  3. an UNKNOWN frame and a CHANGED unmapped byte surface on the diagnostic channel (no error);
  4. clean fires the correct box frame ONLY when the LATEST-heartbeat movement interlock passes and
     resume fires ONLY on its own paused+not-occupied gate; no frame is ever sent without an HA command;
  5. the box session survives malformed HA commands and a corrupt box frame;
  6. the Mosquitto password is never written to the gateway's log;
  7. with cats: the cat sensors are discovered (both modes) and the GATEWAY-INTERNAL cat commands
     (correct_last / set_ref_<slug>) apply locally and send NOTHING to the box; without cats: no cat
     topics are subscribed and no cat keys appear in state.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vectors as v  # noqa: E402
from catlink_local import config  # noqa: E402
from catlink_local import gateway as gwmod  # noqa: E402
from catlink_local import log as logmod  # noqa: E402

LAUNCHER = os.path.join(ROOT, "gateway.py")
PASSWORD = "offline-test-password-not-real"
HA_USER = "catlinktest"
PREFIX = "hatest"
BASE = "catlinktest/box"
NODE = "catlink_litterbox"
CATS_ENV = "cat_a=3.20,cat_b=4.60"
CAT_SENSORS = {"cat_a_weight", "cat_b_weight", "cat_a_trend_7d", "cat_b_trend_7d", "cat_gap",
               "last_visit_cat", "labelled_pct", "correction_count"}
CAT_STATE_KEYS = {"cat_a_weight", "cat_b_weight", "cat_a_trend_7d", "cat_b_trend_7d", "cat_a_trend_word",
                  "cat_b_trend_word", "cat_gap", "labelled_pct", "correction_count", "cat_degraded"}
ACTUATOR_COMPONENTS = {"button", "switch", "select", "number"}


def clean_env(**extra):
    env = {k: val for k, val in os.environ.items()
           if not k.startswith("CATLINK_") and k not in ("CREDENTIALS_DIRECTORY", "STATE_DIRECTORY")}
    env.update(extra)
    return env


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- tiny MQTT codec ------------------------------------------------------------------------------
def enc_len(value):
    out = bytearray()
    while True:
        value, digit = divmod(value, 128)
        out.append(digit | (0x80 if value else 0))
        if not value:
            return bytes(out)


def mstr(s):
    b = s.encode()
    return len(b).to_bytes(2, "big") + b


def read_exact(sock, n):
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise OSError("closed")
        out += chunk
    return bytes(out)


def read_packet(sock):
    header = read_exact(sock, 1)[0]
    remaining = 0
    for i in range(4):
        val = read_exact(sock, 1)[0]
        remaining += (val & 0x7F) << (7 * i)
        if not val & 0x80:
            break
    body = read_exact(sock, remaining) if remaining else b""
    return header, body


# ---- fake HA broker (sink) ------------------------------------------------------------------------
class HaSink:
    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.lock = threading.Lock()
        self.msgs = []          # (topic, payload, retain)
        self.auth_ok = None
        self.will = None
        self.conn = None
        self.subs = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        self.conn = conn
        _, body = read_packet(conn)
        pos = 2 + int.from_bytes(body[0:2], "big")  # protocol name
        flags = body[pos + 1]
        pos += 4

        def nxt(p):
            n = int.from_bytes(body[p:p + 2], "big")
            return body[p + 2:p + 2 + n], p + 2 + n
        _, pos = nxt(pos)  # client id
        if flags & 0x04:
            wt, pos = nxt(pos)
            wm, pos = nxt(pos)
            self.will = (wt.decode(), wm.decode(), bool(flags & 0x20))
        user = pw = None
        if flags & 0x80:
            user, pos = nxt(pos)
        if flags & 0x40:
            pw, pos = nxt(pos)
        self.auth_ok = (user == HA_USER.encode() and pw == PASSWORD.encode())
        conn.sendall(b"\x20\x02\x00" + (b"\x00" if self.auth_ok else b"\x05"))
        try:
            while True:
                h, b = read_packet(conn)
                k = h >> 4
                if k == 8:  # SUBSCRIBE
                    pid, p, filters = b[0:2], 2, []
                    while p < len(b):
                        n = int.from_bytes(b[p:p + 2], "big")
                        p += 2
                        filters.append(b[p:p + n].decode())
                        p += n + 1
                    with self.lock:
                        self.subs += filters
                    conn.sendall(b"\x90" + enc_len(2 + len(filters)) + pid + b"\x00" * len(filters))
                elif k == 3:  # PUBLISH from the gateway
                    tn = int.from_bytes(b[0:2], "big")
                    with self.lock:
                        self.msgs.append((b[2:2 + tn].decode(), b[2 + tn:].decode("utf-8", "replace"), bool(h & 1)))
                elif h == 0xC0:
                    conn.sendall(b"\xd0\x00")
                elif h == 0xE0:
                    break
        except OSError:
            pass

    def inject(self, topic, payload, retain=False):
        body = mstr(topic) + payload.encode()
        self.conn.sendall(bytes([0x30 | (1 if retain else 0)]) + enc_len(len(body)) + body)

    def latest(self, topic):
        with self.lock:
            for t, p, _ in reversed(self.msgs):
                if t == topic:
                    return p
        return None

    def latest_json(self, suffix):
        return json.loads(self.latest("%s/%s" % (BASE, suffix)) or "{}")

    def configs(self):
        """Non-empty discovery configs."""
        with self.lock:
            return [(t, p, r) for t, p, r in self.msgs if t.endswith("/config") and p]

    def cleared(self):
        """Discovery topics that received an empty payload (entity removal), as (topic, retained)."""
        with self.lock:
            return [(t, r) for t, p, r in self.msgs if t.endswith("/config") and not p]

    def close(self):
        for s in (self.conn, self.srv):
            try:
                if s:
                    s.close()
            except OSError:
                pass


# ---- fake box -------------------------------------------------------------------------------------
class FakeBox:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.received = []  # PUBLISHes the gateway sent to the box (commands)
        vh = mstr("MQIsdp") + bytes([3, 0xC2]) + (120).to_bytes(2, "big")   # user + password flags
        # synthetic client id / username in the vendor's shape: they carry pk/dn, which must never be logged
        payload = (mstr("%s.%s|securemode=3,signmethod=hmacsha1,timestamp=1|" % (v.PK, v.DN))
                   + mstr("%s&%s" % (v.DN, v.PK)) + mstr("box-secret-never-parsed"))
        self.sock.sendall(b"\x10" + enc_len(len(vh + payload)) + vh + payload)
        h, _ = read_packet(self.sock)
        assert h == 0x20, "box connack"
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                h, b = read_packet(self.sock)
                if (h >> 4) == 3:
                    tn = int.from_bytes(b[0:2], "big")
                    self.received.append((b[2:2 + tn].decode(), b[2 + tn:]))
        except OSError:
            pass

    def subscribe(self, topic):
        body = (1).to_bytes(2, "big") + mstr(topic) + b"\x00"
        self.sock.sendall(b"\x82" + enc_len(len(body)) + body)
        time.sleep(0.2)  # let the gateway process the SUBSCRIBE; the reader thread discards the SUBACK

    def publish(self, topic, payload):
        body = mstr(topic) + payload
        self.sock.sendall(b"\x30" + enc_len(len(body)) + body)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def wait_for(predicate, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class GatewayScenarioTests(unittest.TestCase):
    """Each test runs the full scenario in one mode; every check is collected and reported together."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="catlink_gw_")
        self.failures = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def check(self, name, ok, detail=""):
        if not ok:
            self.failures.append("%s %s" % (name, ("- %r" % (detail,)) if detail != "" else ""))

    def test_sensor_mode(self):
        self.scenario(commands=False, cats=False)

    def test_command_mode(self):
        self.scenario(commands=True, cats=False)

    def test_sensor_mode_with_cats(self):
        self.scenario(commands=False, cats=True)

    def test_command_mode_with_cats(self):
        self.scenario(commands=True, cats=True)

    # ------------------------------------------------------------------------------------------------
    def scenario(self, commands, cats):
        tag = ("commands" if commands else "sensor") + ("+cats" if cats else "")
        check = lambda name, ok, detail="": self.check("[%s] %s" % (tag, name), ok, detail)  # noqa: E731
        sink = HaSink()
        box_port = free_port()
        passfile = os.path.join(self.dir, "ha_mqtt.pass")
        with open(passfile, "w") as fh:
            fh.write(PASSWORD + "\n")
        os.chmod(passfile, 0o600)
        logpath = os.path.join(self.dir, "gateway.log")
        env = clean_env(CATLINK_BOX_BIND_IP="127.0.0.1", CATLINK_BOX_BIND_PORT=str(box_port),
                        CATLINK_SKIP_PEER_CHECK="1", CATLINK_BOX_MAC=v.TEST_MAC,
                        CATLINK_HA_HOST="127.0.0.1", CATLINK_HA_PORT=str(sink.port),
                        CATLINK_HA_USER=HA_USER, CATLINK_HA_PASS_FILE=passfile,
                        CATLINK_COMMANDS="1" if commands else "0",
                        CATLINK_DISCOVERY_PREFIX=PREFIX, CATLINK_BASE=BASE,
                        CATLINK_VISITS_FILE=os.path.join(self.dir, "visits.jsonl"),
                        CATLINK_CATS_FILE=os.path.join(self.dir, "cats.json"),
                        CATLINK_CATS=CATS_ENV if cats else "")
        logfh = open(logpath, "w")
        proc = subprocess.Popen([sys.executable, "-I", "-u", LAUNCHER], env=env, stdout=logfh,
                                stderr=subprocess.STDOUT)
        box = None

        def logtext():
            with open(logpath) as fh:
                return fh.read()

        def st_now():
            return sink.latest_json("state")

        def diag_now():
            return sink.latest_json("diag")

        try:
            if not wait_for(lambda: '"event":"broker_start"' in logtext(), timeout=10):
                self.fail("gateway did not start: %s" % logtext())
            box = FakeBox(box_port)
            box.subscribe(v.CMD_TOPIC)   # gateway learns {pk}/{dn}
            box.subscribe(v.REPLY_TOPIC)

            # --- auth + discovery ---
            check("gateway authenticated to HA broker", wait_for(lambda: sink.auth_ok is True))
            check("LWT is retained offline on the availability topic",
                  sink.will == ("%s/availability" % BASE, "offline", True), sink.will)
            check("discovery published", wait_for(lambda: '"event":"ha_discovery_published"' in logtext()))
            time.sleep(0.3)
            configs = sink.configs()
            cfgs = {t.split("/")[1] + "/" + t.split("/")[3]: json.loads(p) for t, p, _ in configs}
            published = [json.loads(line) for line in logtext().splitlines()
                         if '"event":"ha_discovery_published"' in line]
            check("discovery count matches the log", published and published[0]["count"] == len(configs),
                  (published and published[0].get("count"), len(configs)))
            check("all discovery retained", configs and all(r for _, _, r in configs))
            check("discovery topics are PREFIX/<component>/NODE/<object_id>/config",
                  all(t.startswith(PREFIX + "/") and t.split("/")[2] == NODE for t, _, _ in configs))
            components = {t.split("/")[1] for t, _, _ in configs}
            if commands:
                check("actuators present (button+switch+select+number)", ACTUATOR_COMPONENTS <= components,
                      sorted(components))
                clean_cfg = cfgs.get("button/clean", {})
                check("clean button contract", clean_cfg.get("command_topic") == BASE + "/clean/set"
                      and clean_cfg.get("payload_press") == "PRESS"
                      and clean_cfg.get("unique_id") == NODE + "_cmd_clean" and clean_cfg.get("name") == "Clean now",
                      clean_cfg)
                check("child_lock + kitty are optimistic",
                      cfgs.get("switch/child_lock", {}).get("optimistic") is True
                      and cfgs.get("switch/kitty", {}).get("optimistic") is True)
                check("command mode clears nothing", sink.cleared() == [], sink.cleared())
            else:
                check("NO actuators in sensor mode", not (ACTUATOR_COMPONENTS & components), sorted(components))
                # stale command entities from an earlier CATLINK_COMMANDS=1 run are removed: an empty
                # retained payload on each of the 15 command discovery topics
                cleared = sink.cleared()
                cleared_ids = {t.split("/")[1] + "/" + t.split("/")[3] for t, _ in cleared}
                expected = {"button/clean", "button/pause", "button/resume", "button/cancel", "button/level",
                            "select/mode", "select/litter_type", "number/clean_delay", "number/burial_minutes",
                            "switch/light", "switch/sound", "switch/continuous", "switch/burial",
                            "switch/child_lock", "switch/kitty"}
                check("sensor mode clears the 15 command discovery topics (retained, empty)",
                      cleared_ids == expected and len(cleared) == 15 and all(r for _, r in cleared)
                      and all(t.startswith(PREFIX + "/") and t.split("/")[2] == NODE for t, _ in cleared),
                      sorted(cleared_ids ^ expected))
                check("discovery log reports commands_cleared=15",
                      published and published[0].get("commands_cleared") == 15, published[:1])
            unmapped_cfg = cfgs.get("sensor/unmapped_bytes", {})
            check("unmapped_bytes is diagnostic from BASE/diag",
                  unmapped_cfg.get("entity_category") == "diagnostic"
                  and unmapped_cfg.get("state_topic") == BASE + "/diag")
            bf, dw, ws = cfgs.get("binary_sensor/bin_full", {}), cfgs.get("binary_sensor/drawer_out", {}), \
                cfgs.get("sensor/wifi_signal", {})
            check("bin_full problem, drawer_out door, wifi_signal dBm diagnostic",
                  bf.get("device_class") == "problem" and dw.get("device_class") == "door"
                  and ws.get("unit_of_measurement") == "dBm" and ws.get("entity_category") == "diagnostic",
                  (bf.get("device_class"), dw.get("device_class"), ws.get("unit_of_measurement")))
            occ_cfg = cfgs.get("binary_sensor/occupied", {})
            check("sensor contract (unique_id, name, device, no object_id)",
                  occ_cfg.get("unique_id") == NODE + "_occupied" and occ_cfg.get("name") == "Litter box occupied"
                  and occ_cfg.get("device", {}).get("name") == "CATLINK Open X"
                  and occ_cfg.get("device", {}).get("identifiers") == ["catlink_open_x"]
                  and "object_id" not in occ_cfg, occ_cfg)
            check("visit_count carries recent_visits attributes",
                  cfgs.get("sensor/visit_count", {}).get("json_attributes_topic") == BASE + "/visits")
            measured = {"sensor/live_scale", "sensor/last_visit_weight", "sensor/last_deposit_weight",
                        "sensor/wifi_signal"} | ({"sensor/cat_a_weight", "sensor/cat_b_weight"} if cats else set())
            with_class = {k for k, c in cfgs.items() if c.get("state_class") == "measurement"}
            check("state_class measurement exactly on the numeric measurements", with_class == measured,
                  sorted(with_class ^ measured))
            sensors = {t.split("/")[3] for t, _, _ in configs if t.split("/")[1] == "sensor"}
            if cats:
                check("cat sensors present in discovery", CAT_SENSORS <= sensors, sorted(CAT_SENSORS - sensors))
                check("cat sensor names", cfgs.get("sensor/cat_a_weight", {}).get("name") == "Cat a weight"
                      and cfgs.get("sensor/cat_b_trend_7d", {}).get("name") == "Cat b trend 7d")
                check("cat state seeded before the first visit",
                      wait_for(lambda: CAT_STATE_KEYS <= set(st_now())), sorted(st_now()))
            else:
                check("no cat sensors without CATLINK_CATS", not (CAT_SENSORS & sensors), sorted(CAT_SENSORS & sensors))

            # --- subscriptions on the HA side ---
            with sink.lock:
                subs = sorted(sink.subs)
            if commands:
                check("command mode subscribes BASE/+/set only", subs == [BASE + "/+/set"], subs)
            elif cats:
                check("sensor mode with cats subscribes only the cat topics",
                      subs == sorted([BASE + "/correct_last/set", BASE + "/set_ref_cat_a/set",
                                      BASE + "/set_ref_cat_b/set"]), subs)
            else:
                check("sensor mode without cats subscribes nothing", subs == [], subs)

            # --- decode a heartbeat + a visit ---
            box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0, touch=0, kg=1, cg=48, visit=4, extra=0))
            box.publish(v.FN_TOPIC, v.visit_frame(1, 42, 3, 21))
            ok = wait_for(lambda: st_now().get("visit_count") == 4 and st_now().get("last_visit_duration") == "1:42")
            st = st_now()
            check("state decoded", ok and st.get("mode") == "automatic" and st.get("litter_type") == "mixed"
                  and st.get("live_scale") == 1.48 and st.get("occupied") is False and st.get("cleaning") is False
                  and st.get("state") == "idle" and st.get("box_clock") == "09:15",
                  {k: st.get(k) for k in ("mode", "litter_type", "live_scale", "occupied", "cleaning", "state")})
            check("visit decoded (provisional weight = peak)",
                  st.get("last_visit_weight") == 3.21 and st.get("last_visit_duration") == "1:42"
                  and st.get("last_visit_seconds") == 102 and "last_visit_at" in st,
                  {k: st.get(k) for k in ("last_visit_weight", "last_visit_duration", "last_visit_seconds")})
            check("availability online", wait_for(lambda: sink.latest(BASE + "/availability") == "online"))
            if not cats:
                check("no cat keys in state without CATLINK_CATS",
                      not (CAT_STATE_KEYS | {"last_cat", "last_conf", "last_visit_cat"}) & set(st), sorted(st))

            # --- diagnostics: unknown frame ---
            box.publish(v.FN_TOPIC, v.box_frame(0x995A, b"\x01\x02\x03\x04"))
            ok = wait_for(lambda: diag_now().get("last_unknown_command") == "0x995a")
            check("unknown frame on diagnostic channel", ok and diag_now().get("last_unknown_frame", {}).get("body_len") == 4,
                  diag_now().get("last_unknown_frame"))

            # --- diagnostics: a changed UNMAPPED byte bumps the counter ---
            count0 = diag_now().get("unmapped_change_count", 0)
            box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0, touch=0, kg=1, cg=48, visit=4, extra=0x42))
            ok = wait_for(lambda: diag_now().get("unmapped_change_count", 0) > count0)
            check("changed unmapped byte surfaced", ok and "unmapped_changed_at" in diag_now()
                  and json.loads(diag_now().get("unmapped_map", "{}")).get("55") == 0x42, diag_now())

            # --- bin full / waste drawer / Wi-Fi RSSI (decode-only; both modes) ---
            box.publish(v.HB_TOPIC, v.heartbeat(bin_full=1, drawer=1, rssi=-48))
            ok = wait_for(lambda: st_now().get("wifi_signal") == -48)
            st = st_now()
            check("4-slot: bin full + drawer out + signed RSSI decoded",
                  ok and st.get("bin_full") is True and st.get("drawer_out") is True and st.get("wifi_signal") == -48,
                  {k: st.get(k) for k in ("bin_full", "drawer_out", "wifi_signal")})
            box.publish(v.HB_TOPIC, v.heartbeat(slots=5, bin_full=0, drawer=1, rssi=-52, visit=21))
            ok = wait_for(lambda: st_now().get("wifi_signal") == -52)
            st = st_now()
            check("5-slot: drawer at the shifted byte, bin clear, RSSI -52, visit count shifted",
                  ok and st.get("drawer_out") is True and st.get("bin_full") is False and st.get("wifi_signal") == -52
                  and st.get("visit_count") == 21 and st.get("slot_count") == 5,
                  {k: st.get(k) for k in ("drawer_out", "bin_full", "wifi_signal", "visit_count", "slot_count")})
            box.publish(v.HB_TOPIC, v.heartbeat(slots=5, drawer=0, rssi=-50, visit=21))
            ok = wait_for(lambda: st_now().get("wifi_signal") == -50)
            check("5-slot: drawer back in reads off", ok and st_now().get("drawer_out") is False,
                  st_now().get("drawer_out"))
            # 0x295a / 0x0b5a / 0x2a5a are KNOWN frames: none may reach the unknown-frame channel
            unk_before = diag_now().get("last_unknown_frame")
            box.publish(v.FN_TOPIC, v.drawer_event_frame())
            box.publish(v.FN_TOPIC, v.clean_end_frame())
            box.publish(v.FN_TOPIC, v.time_request_frame())
            box.publish(v.HB_TOPIC, v.heartbeat(rssi=-47))      # back to the 4-slot layout; also a sync point
            ok = wait_for(lambda: st_now().get("wifi_signal") == -47)
            check("known frames are not counted as unknown", ok and diag_now().get("last_unknown_frame") == unk_before,
                  (unk_before, diag_now().get("last_unknown_frame")))
            # unusable heartbeat layout is reported, not guessed
            box.publish(v.HB_TOPIC, v.heartbeat(slots=6))
            ok = wait_for(lambda: diag_now().get("last_unusable_heartbeat") == "unknown_layout")
            check("unknown slot count reported as unusable heartbeat", ok, diag_now().get("last_unusable_heartbeat"))

            # --- command path ---
            if commands:
                # build up empty-scale samples so scale_near_empty can pass
                for _ in range(4):
                    box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0, touch=0, kg=1, cg=48, visit=4))
                    time.sleep(0.3)
                # FAIL case: the LATEST heartbeat shows the box occupied -> clean must NOT fire.
                box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0x26, touch=0, kg=3, cg=20, visit=4))
                time.sleep(0.8)
                before = len(box.received)
                sink.inject(BASE + "/clean/set", "PRESS")
                time.sleep(1.5)
                check("movement REFUSED when occupied (no frame sent)", len(box.received) == before,
                      "received=%d" % (len(box.received) - before))
                refused = [json.loads(line) for line in logtext().splitlines() if '"event":"command_refused"' in line]
                check("refusal logged with reasons", refused and "not_occupied" in refused[-1].get("reasons", []),
                      refused[-1:] if refused else None)
                # PASS case: a SINGLE not-occupied, fresh, idle+empty heartbeat arriving right after the
                # occupied one ALLOWS clean (the post-visit auto-clean case at a 1-minute clean delay).
                box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0, touch=0, kg=1, cg=48, visit=4))
                time.sleep(0.5)
                sink.inject(BASE + "/clean/set", "PRESS")
                ok = wait_for(lambda: len(box.received) > before, timeout=6)
                frame_ok = False
                if ok:
                    topic, frame = box.received[-1]
                    frame_ok = (topic.startswith("/ext/rrpc/")
                                and topic.endswith("/%s/%s/user/function/down_cmd" % (v.PK, v.DN))
                                and len(topic.split("/")[3]) == 19
                                and frame[0] == 0xFC and int.from_bytes(frame[13:15], "big") == 0x015A
                                and frame[11] == 0x01 and frame[15:-1] == b"\x01\x01"
                                and frame[5:11] == v.TEST_MAC_BYTES and v.xor(frame[1:-1]) == frame[-1])
                check("movement FIRES correct clean frame when interlock passes", ok and frame_ok)

                # resume gate: paused + not-occupied + not-touched + fresh
                box.publish(v.HB_TOPIC, v.heartbeat(state=3, occ=0x26, touch=0, kg=3, cg=20, visit=4))
                time.sleep(0.6)
                nres = len(box.received)
                sink.inject(BASE + "/resume/set", "PRESS")
                time.sleep(1.5)
                check("resume REFUSED when occupied (paused but cat present)", len(box.received) == nres,
                      "received=%d" % (len(box.received) - nres))
                box.publish(v.HB_TOPIC, v.heartbeat(state=3, occ=0, touch=0, kg=1, cg=48, visit=4))
                time.sleep(0.5)
                sink.inject(BASE + "/resume/set", "PRESS")
                ok = wait_for(lambda: len(box.received) > nres, timeout=6)
                rf = box.received[-1][1] if ok else b""
                check("resume FIRES when paused + not-occupied",
                      ok and int.from_bytes(rf[13:15], "big") == 0x015A and rf[15:-1] == b"\x01\x01")

                # a settings command (session-live only) fires a correct frame
                n2 = len(box.received)
                sink.inject(BASE + "/litter_type/set", "bentonite")
                ok = wait_for(lambda: len(box.received) > n2, timeout=6)
                lf = box.received[-1][1] if ok else b""
                check("settings command fires correct frame",
                      ok and int.from_bytes(lf[13:15], "big") == 0x285A and lf[15:-1] == b"\x01\x00")

                # a RETAINED command PUBLISH (stray replay) must be DROPPED before dispatch -> no box frame
                nr = len(box.received)
                sink.inject(BASE + "/litter_type/set", "mixed", retain=True)
                time.sleep(1.2)
                check("retained command dropped (no frame to box)", len(box.received) == nr
                      and '"event":"ha_retained_dropped"' in logtext(), "received=%d" % (len(box.received) - nr))

                # malformed HA commands must be rejected and NOT crash the session
                n3 = len(box.received)
                sink.inject(BASE + "/clean_delay/set", "99")       # out of range
                sink.inject(BASE + "/nonsense/set", "PRESS")       # unknown object
                sink.inject(BASE + "/mode/set", "{bad json")       # bad payload
                sink.inject(BASE + "/mode/set", "scheduled")       # not settable
                time.sleep(1.2)
                check("malformed commands sent nothing to the box", len(box.received) == n3,
                      "received=%d" % (len(box.received) - n3))
                text = logtext()
                check("malformed commands logged", '"event":"command_rejected"' in text
                      and '"event":"command_unknown"' in text)
            else:
                # sensor mode: a command message must do NOTHING (not even subscribed)
                sink.inject(BASE + "/clean/set", "PRESS")
                time.sleep(1.2)
                check("command topic ignored (no frame to box)", len(box.received) == 0)

            # --- box session survives a corrupt frame ---
            box.publish(v.HB_TOPIC, v.corrupt(v.heartbeat()))
            box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0, touch=0, visit=9))
            ok = wait_for(lambda: st_now().get("visit_count") == 9)
            check("box session survives a corrupt frame", ok and diag_now().get("last_unknown_command") == "unparsable",
                  diag_now().get("last_unknown_command"))

            # --- settled-median visit weight + sustained baseline adoption (decode-only; both modes) ---
            # (A) Establish a trusted empty baseline at 1.50 kg (idle + unoccupied).
            for _ in range(2):
                box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0, touch=0, kg=1, cg=50, visit=11))
                time.sleep(0.25)
            # Cat enters: three genuine cat-on-scale readings well above baseline+band: 4.50/4.40/4.60 kg.
            for cg in (50, 40, 60):
                box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0x26, touch=0, kg=4, cg=cg, visit=11))
                time.sleep(0.25)
            # The box's own visit report carries a max-hold PEAK that overstates (5.00 kg).
            box.publish(v.FN_TOPIC, v.visit_frame(1, 30, 5, 0))
            ok = wait_for(lambda: st_now().get("last_visit_weight") == 5.0)
            check("provisional visit weight is the box max-hold peak", ok, st_now().get("last_visit_weight"))
            # Cat exits: the first idle+unoccupied reading (1.55 kg) finalizes the visit.
            box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0, touch=0, kg=1, cg=55, visit=12))
            # settled weight = MEDIAN(4.50,4.40,4.60) - baseline(1.50) = 3.00, NOT the 5.00 peak.
            ok = wait_for(lambda: st_now().get("last_visit_weight") == 3.0)
            st = st_now()
            check("settled visit weight = median(cat-on-scale) - baseline, not the peak",
                  ok and st.get("last_visit_weight") == 3.0, st.get("last_visit_weight"))
            # deposit = exit baseline (1.55) - pre-visit baseline (1.50) = 0.05 kg = 50 g (> 30 g noise floor).
            check("deposit weight from the baseline shift", st.get("last_deposit_weight") == 50,
                  st.get("last_deposit_weight"))
            # (B) Sustained baseline adoption: a litter top-up to 2.00 kg held over 3 consecutive idle+
            # unoccupied readings is ADOPTED as the new empty baseline (a one-off high reading would be
            # rejected). Adoption shows in the NEXT visit's deposit, measured from ~2.00 kg, not 1.55.
            for _ in range(3):
                box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0, touch=0, kg=2, cg=0, visit=12))
                time.sleep(0.25)
            for _ in range(2):
                box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0x26, touch=0, kg=5, cg=0, visit=12))
                time.sleep(0.25)
            box.publish(v.FN_TOPIC, v.visit_frame(1, 0, 6, 0))
            box.publish(v.HB_TOPIC, v.heartbeat(state=0, occ=0, touch=0, kg=2, cg=10, visit=13))
            ok = wait_for(lambda: st_now().get("visit_count") == 13 and st_now().get("last_deposit_weight") == 100)
            st = st_now()
            # Adopted: deposit = 2.10 - 2.00 = 100 g (NOT 2.10 - 1.55 = 550 g); settled = median(5.0,5.0) - 2.0 = 3.0.
            check("sustained litter top-up adopted as new baseline (deposit from ~2.00 kg)",
                  ok and st.get("last_deposit_weight") == 100 and st.get("last_visit_weight") == 3.0,
                  {"deposit": st.get("last_deposit_weight"), "weight": st.get("last_visit_weight")})

            # --- recent visits (BASE/visits, newest first) + persistence ---
            ok = wait_for(lambda: len(sink.latest_json("visits").get("recent_visits", [])) >= 2)
            rows = sink.latest_json("visits").get("recent_visits", [])
            newest = rows[0] if rows else {}
            keys = {"time", "peak", "duration", "weight", "deposit"} | ({"cat", "conf"} if cats else set())
            check("recent_visits newest first with the documented row", ok and set(newest) == keys
                  and newest.get("peak") == 6.0 and newest.get("weight") == 3.0 and newest.get("deposit") == 100
                  and newest.get("duration") == "1m 00s" and len(rows) <= 15, newest)
            check("visits persisted to the state file", os.path.exists(os.path.join(self.dir, "visits.jsonl")))
            if cats:
                check("visit carries a cat label", newest.get("cat") in ("cat_a", "cat_b", "unknown")
                      and isinstance(newest.get("conf"), int), newest)

            # --- GATEWAY-INTERNAL cat commands: apply locally, send NOTHING to the box ---
            box_n = len(box.received)
            if cats:
                before_cc = st_now().get("correction_count", 0)
                sink.inject(BASE + "/correct_last/set", "cat_b")
                okc = wait_for(lambda: st_now().get("correction_count", 0) > before_cc)
                check("correct_last relabels locally (correction_count++)", okc, st_now().get("correction_count"))
                sink.inject(BASE + "/set_ref_cat_a/set", "3.30")
                ok = wait_for(lambda: '"event":"cat_command","object":"set_ref_cat_a","applied":true' in logtext())
                check("set_ref_cat_a applied", ok)
                check("cats.json written", wait_for(lambda: os.path.exists(os.path.join(self.dir, "cats.json"))))
            else:
                sink.inject(BASE + "/correct_last/set", "cat_b")
                sink.inject(BASE + "/set_ref_cat_a/set", "3.30")
                time.sleep(1.0)
                check("cat commands ignored without CATLINK_CATS",
                      '"event":"cat_command"' not in logtext() and "correction_count" not in st_now())
            time.sleep(0.5)
            check("cat commands sent NOTHING to the box", len(box.received) == box_n,
                  "received=%d" % (len(box.received) - box_n))
            if not commands:
                check("no frame EVER sent to the box in sensor mode", len(box.received) == 0)

            # --- gateway process healthy throughout ---
            check("gateway process still running", proc.poll() is None)
        finally:
            if box is not None:
                box.close()
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = None
            logfh.close()
            sink.close()

        text = logtext()
        check("exits 0 on SIGTERM", code == 0 and '"event":"broker_exit"' in text, code)
        check("password never written to the log", PASSWORD not in text)
        check("box CONNECT password never parsed or logged", "box-secret-never-parsed" not in text)
        check("productKey / deviceName never logged (username, client id, topics)",
              v.PK not in text and v.DN not in text, [ln for ln in text.splitlines() if v.PK in ln or v.DN in ln][:2])
        connects = [json.loads(ln) for ln in text.splitlines() if '"event":"connect"' in ln]
        check("connect logs username presence/length and safe client params only",
              connects and connects[0].get("has_username") is True and "username" not in connects[0]
              and connects[0].get("username_len") == len(v.DN) + 1 + len(v.PK)
              and connects[0].get("client_params") == {"securemode": "3", "signmethod": "hmacsha1"}
              and connects[0].get("client_param_keys") == ["securemode", "signmethod", "timestamp"],
              connects[:1])
        check("topics logged with {pk}/{dn} placeholders",
              '"topic":"{pk}/{dn}/user/heartbeat/up"' in text
              and '"/ext/rrpc/+/{pk}/{dn}/user/function/down_cmd"' in text)
        check("peer_check_disabled logged", '"event":"peer_check_disabled"' in text)
        check("0x295a logged as a known drawer_event",
              '"event":"drawer_event"' in text and '"unknown_frame","command":"0x295a"' not in text)
        check("every box frame logged in full hex", '"event":"publish"' in text and '"payload_hex":"fc' in text)
        self.assertEqual([], self.failures, "\n" + "\n".join(self.failures))


class HaBridgeResilienceTests(unittest.TestCase):
    """The HA bridge retries a missing password file and logs fixed, secret-free reasons."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="catlink_gw_")
        self.sink = HaSink()
        self.passfile = os.path.join(self.dir, "ha_mqtt.pass")
        self.logpath = os.path.join(self.dir, "gateway.log")
        env = clean_env(CATLINK_BOX_BIND_IP="127.0.0.1", CATLINK_BOX_BIND_PORT=str(free_port()),
                        CATLINK_SKIP_PEER_CHECK="1", CATLINK_BOX_MAC=v.TEST_MAC,
                        CATLINK_HA_HOST="127.0.0.1", CATLINK_HA_PORT=str(self.sink.port),
                        CATLINK_HA_USER=HA_USER, CATLINK_HA_PASS_FILE=self.passfile,
                        CATLINK_VISITS_FILE=os.path.join(self.dir, "visits.jsonl"))
        self.logfh = open(self.logpath, "w")
        self.proc = subprocess.Popen([sys.executable, "-I", "-u", LAUNCHER], env=env, stdout=self.logfh,
                                     stderr=subprocess.STDOUT)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.logfh.close()
        self.sink.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def events(self, name):
        with open(self.logpath) as fh:
            return [json.loads(line) for line in fh if '"event":"%s"' % name in line]

    def write_password(self, password):
        with open(self.passfile, "w") as fh:
            fh.write(password + "\n")

    def test_missing_password_file_is_retried(self):
        self.assertTrue(wait_for(lambda: self.events("ha_no_password"), timeout=10))
        time.sleep(6)                                   # at least one quiet retry
        missing = self.events("ha_no_password")
        self.assertEqual(len(missing), 1, missing)      # logged once per distinct problem
        self.assertEqual((missing[0]["error"], missing[0]["reason"]), ("FileNotFoundError", "ENOENT"))
        self.assertNotIn(self.passfile, json.dumps(missing))   # no path in the log
        self.write_password(PASSWORD)
        self.assertTrue(wait_for(lambda: self.sink.auth_ok is True, timeout=12))
        self.assertTrue(wait_for(lambda: self.events("ha_discovery_published"), timeout=5))
        self.assertIsNone(self.proc.poll())

    def test_refused_connack_reason_is_logged(self):
        self.write_password("wrong-password-not-real")
        self.assertTrue(wait_for(lambda: self.events("ha_disconnected"), timeout=12))
        ev = self.events("ha_disconnected")[0]
        self.assertEqual((ev["error"], ev["reason"]), ("OSError", "ha_connack_refused_5"))
        with open(self.logpath) as fh:
            self.assertNotIn("wrong-password-not-real", fh.read())


    def test_broker_closing_the_connection_is_logged(self):
        self.write_password(PASSWORD)
        self.assertTrue(wait_for(lambda: self.events("ha_discovery_published"), timeout=12))
        self.sink.conn.shutdown(socket.SHUT_RDWR)     # Mosquitto goes away
        self.assertTrue(wait_for(lambda: self.events("ha_disconnected"), timeout=12))
        ev = self.events("ha_disconnected")[0]
        self.assertEqual((ev["error"], ev["reason"]), ("OSError", "ha_closed"))
        self.assertIsNone(self.proc.poll())


class LogAndErrorFieldTests(unittest.TestCase):
    def tearDown(self):
        logmod.reset()

    def test_no_cap_by_default(self):
        lines = []
        logmod.configure(max_bytes=0, writer=lines.append)
        for i in range(2000):
            logmod.log("e", i=i, pad="x" * 50)
        self.assertEqual(len(lines), 2000)
        self.assertNotIn(b"log_cap_reached", b"".join(lines))

    def test_positive_cap(self):
        lines = []
        logmod.configure(max_bytes=4096, writer=lines.append)
        for i in range(500):
            logmod.log("e", i=i)
        self.assertEqual(lines[-1], b'{"event":"log_cap_reached"}\n')
        self.assertLessEqual(sum(len(x) for x in lines), 4096 + 64)

    def test_error_fields(self):
        f = gwmod._error_fields(OSError("ha_connack_refused_5"))
        self.assertEqual(f, {"error": "OSError", "reason": "ha_connack_refused_5"})
        f = gwmod._error_fields(FileNotFoundError(2, "No such file", "/some/secret/path"))
        self.assertEqual(f, {"error": "FileNotFoundError", "reason": "ENOENT"})
        f = gwmod._error_fields(ValueError("payload with data"))
        self.assertEqual(f, {"error": "ValueError"})
        f = gwmod._error_fields(RuntimeError("ha_no_password_source"))
        self.assertEqual(f["reason"], "ha_no_password_source")

    def test_topic_redaction(self):
        self.assertEqual(gwmod.redact_topic(v.HB_TOPIC), "{pk}/{dn}/user/heartbeat/up")
        self.assertEqual(gwmod.redact_topic(v.CMD_TOPIC), "/ext/rrpc/+/{pk}/{dn}/user/function/down_cmd")
        self.assertEqual(gwmod.redact_topic("/ext/rrpc/1234/%s/%s/user/function/down_cmd" % (v.PK, v.DN)),
                         "/ext/rrpc/1234/{pk}/{dn}/user/function/down_cmd")
        self.assertEqual(gwmod.redact_topic("%s/odd" % v.PK), "<unrecognised topic, 2 levels>")

    def test_client_id_summary(self):
        s = gwmod.client_id_summary("%s.%s|securemode=3,signmethod=hmacsha1,ext=1|" % (v.PK, v.DN))
        self.assertEqual(s["client_params"], {"securemode": "3", "signmethod": "hmacsha1"})
        self.assertEqual(s["client_param_keys"], ["ext", "securemode", "signmethod"])
        self.assertNotIn(v.PK, json.dumps(s))
        self.assertEqual(gwmod.client_id_summary("plain")["client_param_keys"], [])


class StartupTests(unittest.TestCase):
    def run_gateway(self, **extra):
        env = clean_env(**extra)
        return subprocess.run([sys.executable, "-I", LAUNCHER], env=env, capture_output=True, text=True,
                              timeout=20)

    def test_missing_mac_exits_2_and_names_it(self):
        res = self.run_gateway(CATLINK_BOX_BIND_IP="127.0.0.1")
        self.assertEqual(res.returncode, 2)
        self.assertIn("CATLINK_BOX_MAC", res.stderr)

    def test_no_env_at_all_exits_2(self):
        res = self.run_gateway()
        self.assertEqual(res.returncode, 2)
        self.assertIn("CATLINK_BOX_MAC", res.stderr)

    def test_bad_mac_exits_2_without_echoing_it(self):
        res = self.run_gateway(CATLINK_BOX_MAC="zz:not:a:mac", CATLINK_BOX_BIND_IP="127.0.0.1")
        self.assertEqual(res.returncode, 2)
        self.assertIn("CATLINK_BOX_MAC", res.stderr)
        self.assertNotIn("zz:not:a:mac", res.stderr)

    def test_missing_bind_ip(self):
        res = self.run_gateway(CATLINK_BOX_MAC=v.TEST_MAC)
        self.assertEqual(res.returncode, 2)
        self.assertIn("CATLINK_BOX_BIND_IP", res.stderr)

    def test_any_bind_refused(self):
        res = self.run_gateway(CATLINK_BOX_MAC=v.TEST_MAC, CATLINK_BOX_BIND_IP="0.0.0.0")
        self.assertEqual(res.returncode, 2)
        self.assertIn("CATLINK_ALLOW_ANY_BIND", res.stderr)

    def test_three_cats_refused(self):
        res = self.run_gateway(CATLINK_BOX_MAC=v.TEST_MAC, CATLINK_BOX_BIND_IP="127.0.0.1",
                               CATLINK_CATS="cat_a=2.0,cat_b=2.4,cat_c=3.0")
        self.assertEqual(res.returncode, 2)
        self.assertIn("CATLINK_CATS", res.stderr)

    def test_bad_flag_refused(self):
        res = self.run_gateway(CATLINK_BOX_MAC=v.TEST_MAC, CATLINK_BOX_BIND_IP="127.0.0.1", CATLINK_COMMANDS="yes")
        self.assertEqual(res.returncode, 2)
        self.assertIn("CATLINK_COMMANDS", res.stderr)


class ConfigTests(unittest.TestCase):
    BASE_ENV = {"CATLINK_BOX_MAC": v.TEST_MAC, "CATLINK_BOX_BIND_IP": "127.0.0.1"}

    def load(self, role="gateway", **extra):
        return config.load(role, dict(self.BASE_ENV, **extra))

    def test_defaults(self):
        c = self.load()
        self.assertEqual((c.box_mac, c.box_mac_bytes, c.box_ip), (v.TEST_MAC, v.TEST_MAC_BYTES, ""))
        self.assertEqual((c.box_bind_ip, c.box_bind_port), ("127.0.0.1", 1883))
        self.assertEqual((c.auth_bind_ip, c.auth_bind_port), ("127.0.0.1", 443))
        self.assertEqual((c.advertised_mqtt_host, c.advertised_mqtt_port), ("127.0.0.1", 1883))
        self.assertEqual((c.ha_host, c.ha_port, c.ha_user, c.ha_pass_file), ("127.0.0.1", 1883, "catlink", ""))
        self.assertEqual((c.commands, c.no_ha, c.skip_peer_check, c.allow_any_bind), (False, False, False, False))
        self.assertEqual((c.discovery_prefix, c.base, c.node), ("homeassistant", "catlink/litterbox", "catlink_litterbox"))
        self.assertEqual((c.device_name, c.device_id), ("CATLINK Open X", "catlink_open_x"))
        self.assertEqual(c.cats, [])
        self.assertEqual((c.visits_file, c.cats_file),
                         ("/var/lib/catlink-local/visits.jsonl", "/var/lib/catlink-local/cats.json"))
        self.assertEqual(c.log_max_bytes, 0)            # no cap: journald rotates
        self.assertEqual(self.load(CATLINK_LOG_MAX_BYTES="8192").log_max_bytes, 8192)

    def test_systemd_directories(self):
        c = self.load(STATE_DIRECTORY="/run/state-a:/run/state-b", CREDENTIALS_DIRECTORY="/run/creds")
        self.assertEqual(c.visits_file, "/run/state-a/visits.jsonl")
        self.assertEqual(c.cats_file, "/run/state-a/cats.json")
        self.assertEqual(c.ha_pass_file, "/run/creds/ha_mqtt_pass")
        self.assertEqual((c.auth_cert, c.auth_key), ("/run/creds/cert.pem", "/run/creds/key.pem"))
        c = self.load(CREDENTIALS_DIRECTORY="/run/creds", CATLINK_HA_PASS_FILE="/etc/x.pass")
        self.assertEqual(c.ha_pass_file, "/etc/x.pass")

    def test_overrides(self):
        c = self.load(CATLINK_BOX_IP="192.0.2.5", CATLINK_BOX_BIND_PORT="18830", CATLINK_AUTH_BIND_IP="127.0.0.2",
                      CATLINK_ADVERTISED_MQTT_HOST="198.51.100.1", CATLINK_COMMANDS="1",
                      CATLINK_BOX_MAC="02-CA-7B-0C-00-01")
        self.assertEqual((c.box_ip, c.box_bind_port, c.auth_bind_ip), ("192.0.2.5", 18830, "127.0.0.2"))
        self.assertEqual((c.advertised_mqtt_host, c.advertised_mqtt_port), ("198.51.100.1", 18830))
        self.assertEqual((c.commands, c.box_mac), (True, v.TEST_MAC))

    def test_errors_name_the_variable(self):
        cases = [("CATLINK_BOX_BIND_PORT", "0"), ("CATLINK_BOX_BIND_PORT", "http"), ("CATLINK_BOX_IP", "box"),
                 ("CATLINK_BASE", "catlink/#"), ("CATLINK_DISCOVERY_PREFIX", "/ha"), ("CATLINK_NODE", "a b"),
                 ("CATLINK_SKIP_PEER_CHECK", "true"), ("CATLINK_LOG_MAX_BYTES", "10")]
        for name, value in cases:
            with self.assertRaises(config.ConfigError, msg=name) as ctx:
                self.load(**{name: value})
            self.assertIn(name, str(ctx.exception))

    def test_any_bind_needs_opt_in(self):
        with self.assertRaises(config.ConfigError):
            self.load(CATLINK_BOX_BIND_IP="0.0.0.0")
        self.assertEqual(self.load(CATLINK_BOX_BIND_IP="0.0.0.0", CATLINK_ALLOW_ANY_BIND="1").box_bind_ip, "0.0.0.0")
        with self.assertRaises(config.ConfigError):
            self.load("authresp", CATLINK_AUTH_BIND_IP="0.0.0.0", CATLINK_AUTH_CERT="c", CATLINK_AUTH_KEY="k")

    def test_authresp_needs_cert_source(self):
        with self.assertRaises(config.ConfigError) as ctx:
            self.load("authresp")
        self.assertIn("CATLINK_AUTH_CERT", str(ctx.exception))
        self.assertEqual(self.load("authresp", CREDENTIALS_DIRECTORY="/run/creds").auth_cert, "/run/creds/cert.pem")

    def test_peer_pinning(self):
        c = self.load(CATLINK_BOX_IP="192.0.2.5")
        self.assertFalse(config.peer_is_box(c, "127.0.0.1"))          # wrong IP
        self.assertTrue(config.peer_is_box(self.load(CATLINK_SKIP_PEER_CHECK="1"), "127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
