"""CATLINK Open X local gateway = an observe-only box-facing MQTT broker PLUS a Home Assistant bridge.

THE BOX-FACING HALF (observe-only contract)
  Bind CATLINK_BOX_BIND_IP:CATLINK_BOX_BIND_PORT, accept only the box (MAC from the ARP table, plus the IP
  when CATLINK_BOX_IP is set), parse CONNECT without ever reading the password, answer only
  CONNACK/SUBACK/PUBACK/PINGRESP, log every box PUBLISH with its full payload hex, one-client takeover;
  systemd Restart=always is the backstop. The box session loop MUST NOT be destabilised by anything on the
  HA side: all decode and all HA work is wrapped in try/except and run off the box threads (a shared
  snapshot + an event), so a bug there can never raise into the box's reader loop. send() to the box
  refuses anything but those four control packets.

TWO CLEANLY SEPARABLE HALVES ON TOP
  1. SENSOR HALF (always on, read-only). A table-driven decoder (protocol.py) turns box heartbeats/visit
     reports into a state snapshot, and a hand-rolled MQTT client publishes Home Assistant MQTT discovery
     + state to your Mosquitto as CATLINK_HA_USER. No path here can ever write to the box. Safe on its own
     (CATLINK_COMMANDS=0, the default).
  2. COMMAND HALF (off unless CATLINK_COMMANDS=1). Subscribes to <BASE>/<entity>/set on your Mosquitto and,
     ONLY when an HA-originated message arrives, builds the matching box frame and publishes it to the box.
     MOVEMENT commands (clean/level) pass the idle+empty+no-touch+fresh+scale-near-empty+subscribed
     interlock, resume passes its own paused gate, or they are refused and logged. Settings commands require
     only that the box session is live and subscribed. Nothing ever auto-fires: there is no timer, no
     trigger file, no automation inside this process.

publish_to_box() is the ONE socket-write path that can PUBLISH to the box. It raises when CATLINK_COMMANDS
is not 1, and it learns {pk}/{dn} from the box's own SUBSCRIBE and uses the configured box MAC -- no
device identifier is stored by this process.

SECRETS. The Mosquitto password is read once from a systemd credential ($CREDENTIALS_DIRECTORY/ha_mqtt_pass)
or the file named by CATLINK_HA_PASS_FILE. It is never logged and never echoed. The box CONNECT password is
never parsed at all.

LOGS. JSONL on stdout (journald under systemd). Every box frame is logged in full hex, so a not-yet-decoded
signal (deodorant, litter low, error/anti-pinch, ...) can be decoded retrospectively from history. Unknown
frames and changed-but-unmapped heartbeat bytes are ALSO surfaced to HA as diagnostic entities so a
real-world event can be correlated live.

Configuration: CATLINK_* environment variables, see config.py and config/catlink-local.env.example.
"""
import collections
import datetime
import errno
import json
import os
import re
import signal
import socket
import statistics
import sys
import threading
import time

from . import __version__
from . import config as config_mod
from .cats import CatTracker, display_name
from .log import configure as configure_log
from .log import log
from .mqtt_min import MAX_PACKET, HaMqtt, encode_length
from .protocol import (CLEAN_END_COMMAND, COMMAND_TOPIC_RE, COMMANDS, DRAWER_EVENT_COMMAND,
                       HEARTBEAT_COMMAND, TIME_REQ_COMMAND, VISIT_COMMAND, build_box_frame, build_state,
                       decode_heartbeat, frame_fields)

# ----------------------------------------------------------------------------------------------------
# Configuration (set once by configure() from config.load("gateway"))
# ----------------------------------------------------------------------------------------------------
CFG = None
BOX_BIND = None
BOX_MAC_BYTES = None
COMMANDS_ENABLED = False
DISCOVERY_PREFIX = "homeassistant"
BASE = "catlink/litterbox"
NODE = "catlink_litterbox"
DEVICE = {}
AVAIL_TOPIC = STATE_TOPIC = DIAG_TOPIC = VISITS_TOPIC = None
CAT_COMMANDS = set()

VISITS_KEEP = 15
CONNECT_TIMEOUT_S = 15
NO_KEEPALIVE_TIMEOUT_S = 600
PROTOCOLS = {(b"MQIsdp", 3), (b"MQTT", 4)}

# Everything the box-side send() may ever write, by first byte. PUBLISH (0x30-0x3f) is NOT here:
# publish_to_box() is its only path and it is gated on COMMANDS_ENABLED.
ALLOWED_TX = {0x20: "connack", 0x40: "puback", 0x90: "suback", 0xD0: "pingresp"}
COMMAND_CODES = frozenset(c.command for c in COMMANDS.values())   # the box echoes these as replies

# Interlock thresholds (the safety envelope; deliberately not configurable). Movement commands
# (clean/level/resume) act on the LATEST heartbeat, which can be stale; the box emits a steady ~100 s
# heartbeat when idle (observed: about 100 s when idle) and speeds up to roughly every 12 s when
# active. An earlier revision allowed 150 s -- too loose: a cat could enter after a 100 s-old
# "idle" heartbeat. 130 s is used (>~130 s => the box is slow/disconnected, not merely idle, so movement
# is refused). The clean/level gate acts on the single LATEST heartbeat (idle+not-occupied+not-touched+
# fresh+scale-near-empty). An earlier revision also required the last 3 heartbeats to all be idle+empty;
# it was removed because at the ~100 s idle cadence it took ~200-300 s to satisfy, which silently refused
# a post-visit auto-clean at a 1-minute clean delay (the latest beat 60 s after a visit is the fresh
# cat-exit idle beat). The residual sub-heartbeat window (a cat entering just after the latest idle beat)
# is covered by the box's own physical anti-pinch; occupancy cannot be polled on demand.
HEARTBEAT_MAX_AGE_S = 130
SCALE_TOLERANCE = 30  # hundredths of a kg
EMPTY_SAMPLES = 10
MIN_EMPTY_SAMPLES = 2
OFFLINE_AFTER_S = 300  # HA availability flips to offline if no fresh box heartbeat within this

# Deposit / visit-weight decode guards (see docs/DESIGN.md). The box scale resolves 10 g and the
# occupancy flag lags the scale by ~1 heartbeat (~3 s) at both entry and exit. These constants defend the
# empty-scale baseline, the deposit, and the visit weight against that lag and against sub-noise
# readings. Pure STATE/DECODE; no effect on the box side.
EMPTY_BASELINE_MAX_JUMP = 0.15   # kg: a new "empty" baseline that jumps > this ABOVE the current one is
                                 # NOT accepted outright (a cat on the scale while occupied=0). Also the
                                 # floor a sample must clear ABOVE the baseline to count as a genuine
                                 # cat-on-scale reading.
DEPOSIT_NOISE_FLOOR_G = 30       # g: a computed deposit below this is at/under the scale noise floor -> 0
RELITTER_CONFIRM_SAMPLES = 3     # a higher empty level is adopted as the baseline only after this many
RELITTER_TOLERANCE_KG = 0.10     # consecutive idle+unoccupied heartbeats agree within this (a real litter
                                 # top-up persists; a one-off cat-on-scale lag reading does not).
LITTER_WEIGHT_SAMPLES = 5        # litter_weight = median of the last N trusted empty baselines ...
LITTER_WEIGHT_MIN_SAMPLES = 3    # ... published once at least this many exist (a few minutes after start)
MIN_SETTLED_SAMPLES = 2          # settled visit weight = MEDIAN of the genuine cat-on-scale samples (robust
                                 # to a partial-support low sample); fall back to the box peak below this.
LATE_REPORT_GRACE_S = 180        # a visit report arriving this soon after its occupancy closed is paired with
                                 # it (seen 40-50 s late when a cat stepped into a running clean cycle)
# The box re-zeroes its scale at power-up WITH the litter in it (a power-cycle took the empty scale from
# ~3.4 kg to 0.00). litter_weight therefore carries an offset across a re-zero: the first trusted empty
# reading of a NEW box session that sits near 0 while the last known litter weight was substantial. Within
# one session a low reading is never treated as a re-zero.
LITTER_REZERO_MIN_PREV = 1.0     # kg: last known litter weight at/above this ...
LITTER_REZERO_MAX_NOW = 0.30     # kg: ... and the new session's first empty reading at/under this = re-zero


def configure(cfg):
    """Install a validated config (config.load('gateway')) into the module globals."""
    global CFG, BOX_BIND, BOX_MAC_BYTES, COMMANDS_ENABLED, DISCOVERY_PREFIX, BASE, NODE, DEVICE
    global AVAIL_TOPIC, STATE_TOPIC, DIAG_TOPIC, VISITS_TOPIC, CAT_COMMANDS
    CFG = cfg
    BOX_BIND = (cfg.box_bind_ip, cfg.box_bind_port)
    BOX_MAC_BYTES = cfg.box_mac_bytes
    COMMANDS_ENABLED = bool(cfg.commands)
    DISCOVERY_PREFIX = cfg.discovery_prefix
    BASE = cfg.base
    NODE = cfg.node
    DEVICE = {"identifiers": [cfg.device_id], "name": cfg.device_name, "manufacturer": "CATLINK",
              "model": "Open X (C08)", "sw_version": "gateway " + __version__}
    AVAIL_TOPIC = BASE + "/availability"
    STATE_TOPIC = BASE + "/state"
    DIAG_TOPIC = BASE + "/diag"
    VISITS_TOPIC = BASE + "/visits"      # json_attributes source for the visit_count sensor (recent_visits)
    # Gateway-internal commands; they never reach the box. The cat ones exist only when CATLINK_CATS is set.
    CAT_COMMANDS = ({"correct_last"} | {"set_ref_" + slug for slug, _ in cfg.cats}) if cfg.cats else set()
    CAT_COMMANDS = CAT_COMMANDS | {"litter_reset"}
    configure_log(max_bytes=cfg.log_max_bytes)


class Closed(Exception):
    """The peer went away; not a sign of anything wrong. Its text is a fixed reason, never data."""


class Bad(Exception):
    """The session looks wrong. Its text is a fixed reason, never data."""


def field(data, offset):
    """An MQTT length-prefixed field: returns (value, next offset)."""
    if offset + 2 > len(data):
        raise Bad("truncated_field")
    end = offset + 2 + int.from_bytes(data[offset:offset + 2], "big")
    if end > len(data):
        raise Bad("truncated_field")
    return data[offset + 2:end], end


def text(raw):
    return bytes(raw).decode("utf-8", "replace")


# Box topics are [/ext/rrpc/<id>/]{productKey}/{deviceName}/user/...; logs show them with the two device
# identifiers replaced, so a shared log does not identify the box.
_BOX_TOPIC_RE = re.compile(r"^(/ext/rrpc/[^/]+/)?[^/]+/[^/]+/(user/.+)$")
CLIENT_PARAMS_LOGGED = ("securemode", "signmethod")   # non-identifying client-id parameters


def redact_topic(topic):
    match = _BOX_TOPIC_RE.match(topic)
    if match:
        return "%s{pk}/{dn}/%s" % (match.group(1) or "", match.group(2))
    return "<unrecognised topic, %d levels>" % len(topic.split("/"))


def client_id_summary(client_id):
    """MQTT client id "<id>|k=v,k=v|": log the parameter names and the securemode/signmethod values only;
    the leading id and any other value may identify the device."""
    params = {}
    for item in client_id.partition("|")[2].strip("|").split(","):
        key, _, value = item.partition("=")
        if key:
            params[key] = value
    return {"client_id_len": len(client_id), "client_param_keys": sorted(params),
            "client_params": {k: params[k] for k in CLIENT_PARAMS_LOGGED if k in params}}


# ====================================================================================================
# Shared gateway state (between the box threads and the HA bridge thread)
# ====================================================================================================
class GatewayState:
    """One lock, one snapshot. The box threads write; the HA bridge reads. No MQTT from the box threads."""

    def __init__(self, visits_file, cats=(), cats_file=None, litter_file=None):
        self.visits_file = visits_file
        self.litter_file = litter_file
        self.lock = threading.Lock()
        self.dirty = threading.Event()
        self.state = {}                      # HA-facing state dict
        self.diag = {}                       # HA-facing diagnostics dict
        self.last_heartbeat_at = None        # monotonic
        self.prev_unmapped = None
        self.unmapped_change_count = 0
        # Deposit estimate: weight left behind on the most recent completed visit, from the change
        # in the box's empty-scale baseline across the visit (the empty scale does not read 0).
        self._empty_baseline = None          # last TRUSTED stable empty-scale reading, kg
        self._pre_visit_baseline = None       # trusted empty baseline snapshotted when the cat entered, kg
        self._prev_occupied = False
        self._visit_open = False
        self._occupied_samples = []           # genuine cat-on-scale readings this visit (settled = median)
        self._high_candidate = None           # a sustained higher empty level under confirmation, kg
        self._high_count = 0                  # consecutive idle+unoccupied readings agreeing with it
        self._litter_hist = collections.deque(maxlen=LITTER_WEIGHT_SAMPLES)  # recent trusted baselines, kg
        self._litter_offset = 0.0            # kg added to the raw empty scale after a power-up re-zero
        self._litter_known = None            # last published litter_weight (persisted), kg
        self._new_box_session = False
        self._load_litter()
        if self._litter_known is not None:
            self.state["litter_weight"] = self._litter_known   # restart: no blank litter sensor
        self._visit_saw_cycle = False        # this occupancy overlapped a clean cycle (state not idle)
        self._closed_visit = None            # last closed occupancy awaiting a late box report
        self._pending_visit = None           # {time, peak, duration} captured at the visit report
        self.recent_visits = collections.deque(maxlen=VISITS_KEEP)  # oldest -> newest
        self._load_visits()
        # Cat identification + weight tracking (pure analysis of the settled weight; see cats.py).
        # Off (None) unless CATLINK_CATS names 1 or 2 cats.
        self.cats = CatTracker(cats, cats_file) if cats else None
        if self.cats is not None:
            self.state.update(self.cats._fields())   # seed the cat sensors before the first visit
        self._restore_last_visit()

    def _restore_last_visit(self):
        """Seed the last-visit sensors from the persisted visit log, so a restart republishes the real
        last visit instead of leaving HA with whatever it held before (or nothing). Best-effort."""
        last = self.recent_visits[-1] if self.recent_visits else None
        if not isinstance(last, dict):
            return
        try:
            if isinstance(last.get("weight"), (int, float)) and not isinstance(last.get("weight"), bool):
                self.state["last_visit_weight"] = last["weight"]
            if isinstance(last.get("deposit"), int) and not isinstance(last.get("deposit"), bool):
                self.state["last_deposit_weight"] = last["deposit"]
            match = re.match(r"^(\d+)m (\d+)s$", str(last.get("duration", "")))
            if match:
                mm, ss = int(match.group(1)), int(match.group(2))
                self.state["last_visit_duration"] = "%d:%02d" % (mm, ss)
                self.state["last_visit_seconds"] = mm * 60 + ss
            if self.cats is not None and last.get("cat") in self.cats.CATS + ("unknown",):
                self.state.update(self.cats._fields(last_cat=last["cat"], last_conf=int(last.get("conf", 0))))
        except (TypeError, ValueError):
            pass

    def _touch(self):
        self.dirty.set()

    def correct_last(self, payload):
        """GATEWAY-INTERNAL: relabel the most recent visit (payload = a cat slug). Never touches the box
        or any interlock. Runs on the HA reader thread; isolated by the lock + the caller's try/except."""
        if self.cats is None:
            return None
        cat = (payload or "").strip().lower()
        with self.lock:
            fields = self.cats.correct_last(cat)
            if fields is not None:
                self.state.update(fields)
                self.cats.save()
                self._touch()
        return fields

    def set_ref(self, cat, payload):
        """GATEWAY-INTERNAL: set a per-cat reference from a manual weigh-in (payload kg). Never touches
        the box or any interlock."""
        if self.cats is None:
            return None
        with self.lock:
            fields = self.cats.set_ref(cat, payload)
            if fields is not None:
                self.state.update(fields)
                self.cats.save()
                self._touch()
        return fields

    def on_heartbeat(self, raw):
        now = time.monotonic()
        with self.lock:
            self.last_heartbeat_at = now
            if not raw.get("valid"):
                self.diag["last_unusable_heartbeat"] = raw.get("reason")
                self.diag["slot_count"] = raw.get("slot_count")
                self._touch()
                return
            self.state = build_state(raw, self.state)
            self._update_deposit()
            unmapped = raw.get("_unmapped", {})
            self.diag["unmapped_bytes"] = "".join("%02x" % v for _, v in sorted(unmapped.items(), key=lambda kv: int(kv[0])))
            self.diag["unmapped_map"] = json.dumps(unmapped, separators=(",", ":"))
            self.diag["slot_count"] = raw.get("slots")
            if self.prev_unmapped is not None and unmapped != self.prev_unmapped:
                self.unmapped_change_count += 1
                self.diag["unmapped_changed_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
                self.diag["unmapped_change_count"] = self.unmapped_change_count
                log("unmapped_changed", count=self.unmapped_change_count, prev=self.prev_unmapped, now=unmapped)
            self.prev_unmapped = unmapped
            self._touch()

    def _update_deposit(self):
        """Estimate the deposit and the settled visit weight from the scale, defending against the
        occupancy-lag and the 10 g noise floor. Called under self.lock, right after build_state. Pure
        bookkeeping on self.state; never touches the box side. A rough estimate (scale noise ~20-30 g).

        Three guards:
          1. Baseline guard: the occupancy flag lags the scale by ~3 s, so a cat-on-scale reading can
             arrive while occupied=0. A reading at/under baseline+EMPTY_BASELINE_MAX_JUMP is the normal
             empty level (small drift, the tens-of-grams post-deposit shift) and is accepted. A reading
             ABOVE that band is NOT accepted outright: it is either a ONE-OFF cat-on-scale lag reading
             (rejected, last trusted baseline kept) OR a real litter top-up. A top-up is adopted only
             once RELITTER_CONFIRM_SAMPLES consecutive idle+unoccupied readings agree within
             RELITTER_TOLERANCE_KG -- so a >150 g re-litter is not rejected forever.
          2. Deposit noise floor: a computed deposit below DEPOSIT_NOISE_FLOOR_G is at/under the scale
             noise floor and is reported as 0 ("no measurable deposit").
          3. Settled weight: the box's 0x025a weight is a max-hold peak that overstates. We collect the
             genuine cat-on-scale samples (occupied AND clearly above the baseline, so the entry/exit lag
             samples near empty are excluded) and report settled = MEDIAN(samples) - pre_visit_baseline,
             which a single partial-support (low) sample cannot drag down the way min() could; with fewer
             than MIN_SETTLED_SAMPLES genuine samples we fall back to the box max-hold peak.
        A two-consecutive post-visit confirmation is intentionally NOT done: the live heartbeat cadence
        gives only a single idle+unoccupied reading between a visit ending and the auto-clean, so
        requiring two would drop visits or measure post-clean (clump already sifted)."""
        scale = self.state.get("live_scale")          # kg, from bytes 16-17
        occupied = self.state.get("occupied")
        idle = self.state.get("state") == "idle"
        if scale is None or occupied is None:
            return
        # Cat just entered: snapshot the last TRUSTED empty baseline and start a settled-weight scan.
        if occupied and not self._prev_occupied:
            self._pre_visit_baseline = self._empty_baseline
            self._visit_open = True
            self._occupied_samples = []
            self._visit_saw_cycle = False
        # A cat that steps into a running clean cycle shows up as occupancy while the box is not idle
        # (interrupted / cleaning): the drum is turned, so the scale does not carry the whole cat.
        if self._visit_open and not idle:
            self._visit_saw_cycle = True
        # While occupied, collect every reading that is clearly a cat on the scale (above the baseline by
        # more than the lag/noise band). The settled weight is the MEDIAN of these, not the max-hold peak.
        if occupied and self._pre_visit_baseline is not None:
            if scale - self._pre_visit_baseline > EMPTY_BASELINE_MAX_JUMP:
                self._occupied_samples.append(scale)
        # Stable empty reading (box idle and unoccupied): on the first such reading after a visit,
        # finalize the deposit + settled weight; then settle the baseline behind the jump guard.
        if idle and not occupied:
            if self._visit_open and self._pre_visit_baseline is not None:
                grams = int(round((scale - self._pre_visit_baseline) * 1000))
                if grams < 0:                          # noise or a scale re-zero -> treat as none
                    grams = 0
                if grams < DEPOSIT_NOISE_FLOOR_G:      # at/under the 10 g scale noise floor -> none
                    grams = 0
                if self._visit_saw_cycle:              # the drum moved mid-visit: the shift is not a deposit
                    grams = 0
                self.state["last_deposit_weight"] = grams
                # Finalize the visit now that its deposit + settled weight are known: append + persist
                # the record so the recent-visits table includes it and survives restarts.
                if self._pending_visit is not None:
                    self._finalize_visit(self._pending_visit, list(self._occupied_samples),
                                         self._pre_visit_baseline, grams, orphan=self._visit_saw_cycle)
                    self._pending_visit = None
                    self._closed_visit = None
                else:
                    # The box sometimes reports a visit only AFTER the occupancy closed (a cat that stepped
                    # into a clean cycle: the report follows the resumed cycle). Keep this occupancy's data
                    # briefly so that late report is paired with it, never with a later, unrelated one.
                    self._closed_visit = {"at": time.monotonic(), "samples": list(self._occupied_samples),
                                          "pre": self._pre_visit_baseline, "grams": grams,
                                          "saw_cycle": self._visit_saw_cycle}
                self._visit_open = False
                self._occupied_samples = []
            # Baseline guard. At/under baseline+band == the normal empty level: accept and clear any
            # pending re-litter candidate. Above the band while idle+unoccupied == either a one-off
            # cat-on-scale lag reading (keep the trusted baseline) or a real litter top-up: adopt the
            # higher level only after RELITTER_CONFIRM_SAMPLES consecutive readings agree within tolerance.
            if self._empty_baseline is None or scale <= self._empty_baseline + EMPTY_BASELINE_MAX_JUMP:
                self._empty_baseline = scale
                self._high_candidate = None
                self._high_count = 0
            else:
                if (self._high_candidate is not None
                        and abs(scale - self._high_candidate) <= RELITTER_TOLERANCE_KG):
                    self._high_count += 1
                else:
                    self._high_candidate = scale
                    self._high_count = 1
                if self._high_count >= RELITTER_CONFIRM_SAMPLES:
                    self._empty_baseline = scale
                    self._high_candidate = None
                    self._high_count = 0
            # Litter weight = median of the last few TRUSTED empty baselines (idle, unoccupied; a one-off
            # high lag reading is already rejected above, the median rides out a one-off low one). The box
            # has no litter-level sensor; HA derives "litter low" from this (see docs/DESIGN.md).
            if self._empty_baseline is not None:
                raw = self._empty_baseline
                if self._new_box_session:
                    # First trusted empty reading of a new box session. The box re-zeroes at power-up with
                    # the litter in it: near 0 now, but substantial before -> carry the old weight over.
                    self._new_box_session = False
                    prev = self._litter_known
                    if prev is not None and prev >= LITTER_REZERO_MIN_PREV and raw <= LITTER_REZERO_MAX_NOW:
                        self._litter_offset = round(prev - raw, 2)
                        self._litter_hist.clear()
                        log("litter_rezero", prev=prev, raw=raw, offset=self._litter_offset)
                        self._persist_litter()
                self._litter_hist.append(raw)
                if len(self._litter_hist) >= LITTER_WEIGHT_MIN_SAMPLES:
                    lw = round(statistics.median(self._litter_hist) + self._litter_offset, 2)
                    self.state["litter_weight"] = lw
                    if lw != self._litter_known:
                        self._litter_known = lw
                        self._persist_litter()
        self._prev_occupied = occupied

    def _finalize_visit(self, pending, samples, pre, grams, orphan=False):
        """Append + persist one completed visit with its settled weight, deposit and cat label. Called
        under self.lock. `orphan`: the visit overlapped a clean cycle, so its weight is not one cat."""
        record = dict(pending)
        peak = record.get("peak")
        is_settled = pre is not None and len(samples) >= MIN_SETTLED_SAMPLES
        if is_settled:
            settled = round(statistics.median(samples) - pre, 2)
            if settled < 0:
                settled = 0.0
        else:                                          # too few genuine samples -> box max-hold peak
            settled = peak
        record["weight"] = settled
        record["deposit"] = grams
        if orphan:
            record["during_clean"] = True
        self.state["last_visit_weight"] = settled
        # Cat identification: label this visit from its settled weight and update the tracked weights.
        # PURE ANALYSIS -- fully isolated so it can never break decode or reach the box side.
        if self.cats is not None:
            try:
                self.state.update(self.cats.on_visit(settled, record, settled=is_settled, orphan=orphan))
                self.cats.save()
            except Exception as exc:  # pragma: no cover - defensive
                log("cat_label_error", error=type(exc).__name__)
        self.recent_visits.append(record)
        self._persist_visits()

    def on_box_session(self):
        """A new box MQTT session started (box boot, Wi-Fi reconnect, or a gateway restart)."""
        with self.lock:
            self._new_box_session = True

    def litter_reset(self):
        """GATEWAY-INTERNAL (button): the box's zero is the EMPTY drum again. For a full litter change:
        empty the drum, power-cycle the box, press this, refill. Drops the carried re-zero offset; the litter
        weight re-appears after LITTER_WEIGHT_MIN_SAMPLES readings. Never touches the box."""
        with self.lock:
            self._litter_offset = 0.0
            self._litter_known = None
            self._litter_hist.clear()
            self._new_box_session = False
            self.state.pop("litter_weight", None)
            self._persist_litter()
            log("litter_reset")
            self._touch()

    def _load_litter(self):
        if not self.litter_file:
            return
        try:
            with open(self.litter_file) as handle:
                data = json.load(handle)
            off, known = data.get("offset", 0.0), data.get("litter_weight")
            if isinstance(off, (int, float)) and not isinstance(off, bool) and 0 <= off <= 10:
                self._litter_offset = float(off)
            if isinstance(known, (int, float)) and not isinstance(known, bool) and 0 <= known <= 10:
                self._litter_known = float(known)
        except (OSError, ValueError, AttributeError):
            pass

    def _persist_litter(self):
        """Atomic, best-effort; never raises. Written only when the rounded litter weight changes."""
        if not self.litter_file:
            return
        try:
            tmp = self.litter_file + ".tmp"
            with open(tmp, "w") as handle:
                json.dump({"offset": self._litter_offset, "litter_weight": self._litter_known}, handle)
            os.replace(tmp, self.litter_file)
        except OSError as exc:
            log("litter_persist_failed", error=type(exc).__name__)

    def _load_visits(self):
        """Reload the persisted recent-visits log on startup. Best-effort; never raises."""
        try:
            with open(self.visits_file) as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
            for row in rows[-VISITS_KEEP:]:
                if isinstance(row, dict):
                    self.recent_visits.append(row)
        except (OSError, ValueError):
            pass

    def _persist_visits(self):
        """Write the rolling visit log atomically. Called under self.lock. Best-effort; never raises."""
        try:
            tmp = self.visits_file + ".tmp"
            with open(tmp, "w") as handle:
                for row in self.recent_visits:
                    handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            os.replace(tmp, self.visits_file)
        except OSError as exc:
            log("visits_persist_failed", error=type(exc).__name__)

    def on_visit(self, body):
        # Body: 01 03 mm ss kg cg  -- duration minutes:seconds, weight kg.hundredths (max-hold; can overstate).
        if len(body) >= 6:
            mm, ss, kg, cg = body[2], body[3], body[4], body[5]
            now = datetime.datetime.now().astimezone()
            peak = round(kg + cg / 100.0, 2)
            with self.lock:
                # Provisional: show the box's max-hold weight until _update_deposit replaces it with the
                # settled weight (median cat-on-scale sample - baseline) when the visit finalizes.
                self.state["last_visit_weight"] = peak
                self.state["last_visit_duration"] = "%d:%02d" % (mm, ss)
                self.state["last_visit_seconds"] = mm * 60 + ss
                self.state["last_visit_at"] = now.isoformat(timespec="seconds")
                # Stage the visit row (host clock; the box clock is unreliable). It is appended to the
                # recent-visits log once _update_deposit computes this visit's deposit + settled weight,
                # at which point `weight` becomes the settled value and `peak` keeps the box max-hold.
                pending = {
                    "time": now.strftime("%b %-d %H:%M"),
                    "peak": peak,
                    "duration": "%dm %02ds" % (mm, ss),
                }
                if self._visit_open:
                    self._pending_visit = pending     # normal: finalized at the next idle+empty heartbeat
                else:
                    # Late report: the occupancy already closed. Pair it with that occupancy if it closed
                    # recently; otherwise there is nothing to measure it against. Either way finalize it
                    # NOW so it can never attach to a later, unrelated visit.
                    closed = self._closed_visit
                    self._closed_visit = None
                    if closed is not None and time.monotonic() - closed["at"] <= LATE_REPORT_GRACE_S:
                        self._finalize_visit(pending, closed["samples"], closed["pre"], closed["grams"],
                                             orphan=closed["saw_cycle"])
                    else:
                        self._finalize_visit(pending, [], None, 0, orphan=True)
                    log("visit_late_report", paired=closed is not None)
                self._touch()

    def on_unknown_frame(self, command, body):
        # Distinct, greppable log line so a brand-new signal type is easy to find in history.
        log("unknown_frame", command=("0x%04x" % command) if command is not None else None,
            body_len=len(body), body_hex=body.hex())
        with self.lock:
            self.diag["last_unknown_frame"] = {
                "command": "0x%04x" % command if command is not None else None,
                "body_len": len(body),
                "body_hex": body.hex(),
                "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self.diag["last_unknown_command"] = ("0x%04x" % command) if command is not None else "unparsable"
            self._touch()

    def snapshot(self):
        with self.lock:
            fresh = self.last_heartbeat_at is not None and (time.monotonic() - self.last_heartbeat_at) < OFFLINE_AFTER_S
            # recent_visits attribute payload, most recent first, for the visit_count sensor.
            visits = json.dumps({"recent_visits": list(self.recent_visits)[::-1]}, separators=(",", ":"))
            return dict(self.state), dict(self.diag), visits, fresh


GW = None


# ====================================================================================================
# BOX-FACING BROKER (observe-only contract)
# ====================================================================================================
state_lock = threading.Lock()
active = None
command_filter = None     # the box's own command-topic subscription (learned); source of {pk}/{dn}
heartbeat_topic = None


class Session:
    def __init__(self, sid, conn, port):
        self.sid = sid
        self.conn = conn
        self.port = port
        self.replaced = False
        self.tx_lock = threading.Lock()          # the command thread and this reader both may write
        self.istate = threading.Lock()           # held across interlock checks + a box publish
        self.connected = False
        self.subscriptions = set()
        self.latest = None                        # latest interlock view of the heartbeat
        self.latest_at = None
        self.empties = collections.deque(maxlen=EMPTY_SAMPLES)

    def send(self, data):
        """Every box-side control write. KeyError (non-control packet) ends the session, nothing sent."""
        name = ALLOWED_TX[data[0]]
        with self.tx_lock:
            self.conn.sendall(data)
        log("tx", sid=self.sid, type=name, bytes=len(data))

    def publish_to_box(self, command, body):
        """The ONLY path that can PUBLISH to the box. Gated on COMMANDS_ENABLED. Builds the frame for THIS
        box ({pk}/{dn} learned from the box's SUBSCRIBE; a fresh 19-digit RRPC id) and sends at QoS 0."""
        if not COMMANDS_ENABLED:
            raise RuntimeError("commands_disabled")
        if command_filter is None:
            raise RuntimeError("no_command_topic_learned")
        frame = build_box_frame(command, body, BOX_MAC_BYTES)
        rrpc_id = str(time.time_ns())
        if len(rrpc_id) != 19:
            raise RuntimeError("bad_rrpc_id")
        topic = command_filter.replace("+", rrpc_id, 1).encode()
        length = 2 + len(topic) + len(frame)
        packet = b"\x30" + encode_length(length) + len(topic).to_bytes(2, "big") + topic + frame
        with self.tx_lock:
            self.conn.sendall(packet)
        log("box_command_sent", sid=self.sid, command="0x%04x" % command, body_hex=body.hex(),
            frame_bytes=len(frame), rrpc_id_len=len(rrpc_id))

    def read_exact(self, count):
        data = bytearray()
        while len(data) < count:
            chunk = self.conn.recv(count - len(data))
            if not chunk:
                raise Closed("peer_closed")
            data += chunk
        return data

    def read_packet(self):
        header = self.read_exact(1)[0]
        remaining = 0
        for index in range(4):
            value = self.read_exact(1)[0]
            remaining += (value & 0x7F) << (7 * index)
            if not value & 0x80:
                break
        else:
            raise Bad("bad_remaining_length")
        if remaining > MAX_PACKET:
            raise Bad("packet_too_large")
        return header, self.read_exact(remaining)

    def connect(self):
        """Reads CONNECT, logs the safe parts, always wipes the buffer, answers CONNACK. Never reads the password."""
        self.conn.settimeout(CONNECT_TIMEOUT_S)
        header, body = self.read_packet()
        try:
            if header != 0x10:
                raise Bad("first_packet_not_connect")
            proto, pos = field(body, 0)
            if pos + 4 > len(body):
                raise Bad("short_connect")
            level, flags = body[pos], body[pos + 1]
            keepalive = int.from_bytes(body[pos + 2:pos + 4], "big")
            pos += 4
            if (bytes(proto), level) not in PROTOCOLS:
                raise Bad("unknown_protocol")
            client_id, pos = field(body, pos)
            if flags & 0x04:
                _, pos = field(body, pos)
                _, pos = field(body, pos)
            username = None
            if flags & 0x80:
                username, pos = field(body, pos)
            # The username (deviceName&productKey) and the client id carry device identifiers: only
            # their presence/length and the non-identifying client-id parameters are logged.
            record = dict(
                protocol=bytes(proto).decode("ascii", "replace"), level=level, keepalive=keepalive,
                clean_session=bool(flags & 0x02), will=bool(flags & 0x04), has_username=bool(flags & 0x80),
                has_password=bool(flags & 0x40),
                username_len=None if username is None else len(username),
                **client_id_summary(text(client_id)),
            )
        finally:
            body[:] = bytes(len(body))
        log("connect", sid=self.sid, port=self.port, **record)
        self.conn.settimeout(keepalive * 1.5 + 5 if keepalive else NO_KEEPALIVE_TIMEOUT_S)
        self.send(b"\x20\x02\x00\x00")
        self.connected = True
        try:
            GW.on_box_session()     # pure analysis bookkeeping (litter re-zero check); never sends
        except Exception as exc:  # pragma: no cover - defensive
            log("session_hook_error", error=type(exc).__name__)

    def on_subscribe(self, body):
        global command_filter, heartbeat_topic
        if len(body) < 3:
            raise Bad("short_subscribe")
        pos, grants, topics = 2, bytearray(), []
        while pos < len(body):
            topic, pos = field(body, pos)
            if pos >= len(body):
                raise Bad("short_subscribe")
            qos = body[pos]
            pos += 1
            topics.append({"topic": text(topic), "qos": qos})
            grants.append(min(qos & 3, 1))
        log("subscribe", sid=self.sid, topics=[{"topic": redact_topic(t["topic"]), "qos": t["qos"]} for t in topics])
        with self.istate:
            self.subscriptions.update(t["topic"] for t in topics)
        # Learn {pk}/{dn} from the box's own command-topic subscription.
        for t in topics:
            match = COMMAND_TOPIC_RE.fullmatch(t["topic"])
            if match:
                with state_lock:
                    command_filter = t["topic"]
                    heartbeat_topic = "%s/%s/user/heartbeat/up" % (match[1], match[2])
        self.send(b"\x90" + encode_length(2 + len(grants)) + bytes(body[:2]) + bytes(grants))

    def on_publish(self, header, body):
        topic, pos = field(body, 0)
        qos = (header >> 1) & 3
        packet_id = None
        if qos:
            if pos + 2 > len(body):
                raise Bad("short_publish")
            packet_id = bytes(body[pos:pos + 2])
            pos += 2
        payload = bytes(body[pos:])
        topic_s = text(topic)
        # Full hex of every box frame is logged here -- the retrospective-decode guarantee.
        log("publish", sid=self.sid, topic=redact_topic(topic_s), qos=qos, dup=bool(header & 8), retain=bool(header & 1),
            length=len(payload), payload_hex=payload.hex())
        # Decode is best-effort and fully isolated: a bug here can never raise into the reader loop.
        try:
            self.ingest(topic_s, payload)
        except Exception as exc:  # pragma: no cover - defensive
            log("decode_error", sid=self.sid, error=type(exc).__name__)
        if qos == 1:
            self.send(b"\x40\x02" + packet_id)
        elif qos != 0:
            raise Bad("unsupported_qos")

    def ingest(self, topic_s, payload):
        """Decode a box frame into shared state and keep the interlock view. Never sends anything."""
        command, body = frame_fields(payload, BOX_MAC_BYTES)
        if command is None:
            GW.on_unknown_frame(None, payload)
            return
        if command == HEARTBEAT_COMMAND:
            raw = decode_heartbeat(body)
            self._update_interlock_view(raw, body)
            GW.on_heartbeat(raw)
        elif command == VISIT_COMMAND:
            GW.on_visit(body)
        elif command in (CLEAN_END_COMMAND, TIME_REQ_COMMAND):
            pass  # known, nothing to surface beyond the full-hex log above
        elif command in COMMAND_CODES:
            # The box's reply to a command we sent (e.g. 0x015a body 0100 after clean): known, not new.
            log("command_reply", sid=self.sid, command="0x%04x" % command, body_hex=body.hex())
        elif command == DRAWER_EVENT_COMMAND:
            # Known event (drawer reinsertion); a greppable line, deliberately NOT the unknown-frame channel.
            # It does not mean the drawer was emptied, so nothing is derived from it (by design).
            log("drawer_event", sid=self.sid, command="0x%04x" % command, body_hex=body.hex())
        else:
            # A frame type we do not decode yet (could carry a new signal) -> diagnostic channel.
            GW.on_unknown_frame(command, body)

    def _update_interlock_view(self, raw, body):
        """Keep exactly the fields the movement interlock needs, slot-count-aware."""
        with self.istate:
            previous = self.latest
            if previous is not None and previous.get("empty"):
                self.empties.append(previous["scale"])
            if not raw.get("valid"):
                self.latest = {"valid": False, "reason": raw.get("reason")}
            else:
                slots = raw["slots"]
                scale = body[16] * 100 + body[17]
                view = {"valid": True, "reason": "ok", "state": raw["state_raw"],
                        "occupied": raw["occupied_raw"], "touch": raw["touch_raw"], "scale": scale}
                view["empty"] = view["state"] == 0 and view["occupied"] == 0 and view["touch"] == 0
                self.latest = view
            self.latest_at = time.monotonic()

    def interlocks(self, live, now):
        """The clean/level movement interlock, as (name, passed, value). Acts on the single LATEST
        heartbeat -- session live+subscribed, heartbeat valid+fresh (<HEARTBEAT_MAX_AGE_S), idle, not
        occupied, not touched, scale near empty. (An earlier revision's "recently_quiescent" check,
        requiring the last 3 heartbeats to all be idle+empty, was removed: at the ~100 s idle cadence it
        needed ~200-300 s to satisfy and so silently refused a post-visit auto-clean at a 1-minute clean
        delay; the box's physical anti-pinch is the acknowledged backstop for the sub-heartbeat window.)"""
        latest = self.latest
        valid = latest is not None and latest.get("valid")
        age = None if self.latest_at is None else round(now - self.latest_at, 1)
        checks = [
            ("session_live", bool(live and self.connected), self.sid),
            ("subscribed_to_command_topic", command_filter in self.subscriptions, len(self.subscriptions)),
            ("heartbeat_valid", bool(valid), None if latest is None else latest.get("reason")),
            ("heartbeat_age", age is not None and age < HEARTBEAT_MAX_AGE_S, age),
            ("state_idle", bool(valid and latest["state"] == 0), latest["state"] if valid else None),
            ("not_occupied", bool(valid and latest["occupied"] == 0), latest["occupied"] if valid else None),
            ("not_touched", bool(valid and latest["touch"] == 0), latest["touch"] if valid else None),
        ]
        empties = sorted(self.empties)
        if not valid:
            checks.append(("scale_near_empty", False, "no_valid_heartbeat"))
        elif len(empties) < MIN_EMPTY_SAMPLES:
            checks.append(("scale_near_empty", False, "only_%d_empty_readings" % len(empties)))
        else:
            reference = empties[(len(empties) - 1) // 2]  # lower median
            checks.append(("scale_near_empty", abs(latest["scale"] - reference) <= SCALE_TOLERANCE,
                           {"scale": latest["scale"], "reference": reference, "samples": len(empties)}))
        return checks

    def resume_interlocks(self, live, now):
        """Resume's own gate. A resumable box is PAUSED (state_raw==3), never idle, so clean/level's
        idle+empty+scale checks can never pass for it (dead button). Resume instead requires the
        box to be paused AND not occupied AND not touched, on a fresh+valid heartbeat, with the session
        live+subscribed -- the same occupancy/touch backstop as a movement command, without idle/empty."""
        latest = self.latest
        valid = latest is not None and latest.get("valid")
        age = None if self.latest_at is None else round(now - self.latest_at, 1)
        return [
            ("session_live", bool(live and self.connected), self.sid),
            ("subscribed_to_command_topic", command_filter in self.subscriptions, len(self.subscriptions)),
            ("heartbeat_valid", bool(valid), None if latest is None else latest.get("reason")),
            ("heartbeat_age", age is not None and age < HEARTBEAT_MAX_AGE_S, age),
            ("state_paused", bool(valid and latest["state"] == 3), latest["state"] if valid else None),
            ("not_occupied", bool(valid and latest["occupied"] == 0), latest["occupied"] if valid else None),
            ("not_touched", bool(valid and latest["touch"] == 0), latest["touch"] if valid else None),
        ]

    def run(self):
        self.connect()
        while True:
            header, body = self.read_packet()
            kind = header >> 4
            if header == 0x82:
                self.on_subscribe(body)
            elif kind == 3:
                self.on_publish(header, body)
            elif header == 0xC0 and not body:
                log("pingreq", sid=self.sid)
                self.send(b"\xd0\x00")
            elif header == 0xE0 and not body:
                raise Closed("mqtt_disconnect")
            else:
                log("unsupported_packet", sid=self.sid, header=header, length=len(body))
                raise Bad("unsupported_packet")


def serve(session):
    global active
    started = time.monotonic()
    try:
        session.run()
        reason = "ended"
    except Closed as exc:
        reason = str(exc)
    except Bad as exc:
        reason = str(exc)
    except socket.timeout:
        reason = "read_timeout"
    except OSError as exc:
        reason = "replaced" if session.replaced else "socket_" + type(exc).__name__
    except Exception as exc:  # pragma: no cover - defensive
        reason = "internal_" + type(exc).__name__
    finally:
        try:
            session.conn.close()
        except OSError:
            pass
    log("disconnect", sid=session.sid, reason=reason, seconds=round(time.monotonic() - started, 1))
    with state_lock:
        if active is session:
            active = None


def is_box(ip):
    """Peer pin: the MAC from the ARP table (retried briefly), and the IP when CATLINK_BOX_IP is set."""
    return config_mod.peer_is_box(CFG, ip, attempts=5)


# ====================================================================================================
# COMMAND PATH (command half) -- HA message -> interlock -> box frame. Never auto-fires.
# The routing table (COMMANDS) and the body builders live in protocol.py.
# ====================================================================================================
def handle_command(object_id, payload):
    """Runs on the HA reader thread. Isolated: any failure is logged and cannot reach the box loop."""
    # GATEWAY-INTERNAL commands (cat ID, litter reset) are handled here and NEVER forwarded to the box:
    # they do not look up COMMANDS, build a box frame, consult an interlock, or call publish_to_box.
    if object_id == "litter_reset":
        try:
            if (payload or "").strip().upper() == "PRESS":
                GW.litter_reset()
        except Exception as exc:  # pragma: no cover - defensive
            log("litter_reset_error", error=type(exc).__name__)
        return
    if object_id in CAT_COMMANDS:
        try:
            if object_id == "correct_last":
                fields = GW.correct_last(payload)
            else:
                fields = GW.set_ref(object_id[len("set_ref_"):], payload)
            log("cat_command", object=object_id, applied=bool(fields is not None))
        except Exception as exc:  # pragma: no cover - defensive
            log("cat_command_error", object=object_id, error=type(exc).__name__)
        return
    spec = COMMANDS.get(object_id)
    if spec is None:
        log("command_unknown", object=object_id)
        return
    try:
        body = spec.builder(payload)
    except (ValueError, TypeError) as exc:
        log("command_rejected", object=object_id, reason="bad_payload:%s" % type(exc).__name__)
        return
    with state_lock:
        session = active
    if session is None:
        log("command_refused", object=object_id, reasons=["no_session"])
        return
    with session.istate:
        with state_lock:
            live = active is session
        if spec.gate in ("move", "resume"):
            now = time.monotonic()
            checks = session.interlocks(live, now) if spec.gate == "move" else session.resume_interlocks(live, now)
            for name, passed, value in checks:
                log("interlock", object=object_id, name=name, ok=bool(passed), value=value)
            failed = [name for name, passed, _ in checks if not passed]
            if failed:
                log("command_refused", object=object_id, reasons=failed)
                return
        else:  # "live": pause/cancel + all settings -- session-live + subscribed only
            if not (live and session.connected and command_filter in session.subscriptions):
                log("command_refused", object=object_id, reasons=["session_not_live_or_unsubscribed"])
                return
        try:
            session.publish_to_box(spec.command, body)
        except Exception as exc:
            log("command_send_failed", object=object_id, error=type(exc).__name__)


# ----------------------------------------------------------------------------------------------------
# HA MQTT discovery
# ----------------------------------------------------------------------------------------------------
# Entity ids are minted by Home Assistant from the device name + the entity name
# (e.g. "CATLINK Open X" + "Clean now" -> button.catlink_open_x_clean_now), so no object_id is sent.
def _avail_block():
    return {"availability_topic": AVAIL_TOPIC, "payload_available": "online", "payload_not_available": "offline"}


def _sensor(object_id, name, tmpl, topic=None, device_class=None, unit=None, diagnostic=False,
            icon=None, json_attrs=None, measurement=False):
    cfg = {"name": name, "unique_id": NODE + "_" + object_id,
           "state_topic": topic or STATE_TOPIC, "value_template": tmpl, "device": DEVICE}
    cfg.update(_avail_block())
    if device_class:
        cfg["device_class"] = device_class
    if unit:
        cfg["unit_of_measurement"] = unit
    if measurement:
        cfg["state_class"] = "measurement"   # numeric reading: HA keeps long-term statistics
    if diagnostic:
        cfg["entity_category"] = "diagnostic"
    if icon:
        cfg["icon"] = icon
    if json_attrs:
        cfg["json_attributes_topic"] = json_attrs
    return ("sensor", object_id, cfg)


def _binary(object_id, name, tmpl, device_class=None, diagnostic=False):
    cfg = {"name": name, "unique_id": NODE + "_" + object_id,
           "state_topic": STATE_TOPIC, "value_template": tmpl, "payload_on": "True", "payload_off": "False",
           "device": DEVICE}
    cfg.update(_avail_block())
    if device_class:
        cfg["device_class"] = device_class
    if diagnostic:
        cfg["entity_category"] = "diagnostic"
    return ("binary_sensor", object_id, cfg)


def sensor_discovery():
    """The SENSOR half: every entity here is read-only. Safe on its own."""
    items = [
        _binary("occupied", "Litter box occupied", "{{ value_json.occupied }}", device_class="occupancy"),
        _binary("cleaning", "Litter box cleaning", "{{ value_json.cleaning }}", device_class="running"),
        _binary("bin_full", "Waste bin full", "{{ value_json.bin_full }}", device_class="problem"),
        _binary("drawer_out", "Waste drawer", "{{ value_json.drawer_out }}", device_class="door"),
        _sensor("wifi_signal", "Wi-Fi signal", "{{ value_json.wifi_signal }}", device_class="signal_strength",
                unit="dBm", diagnostic=True, measurement=True),
        _sensor("state", "Litter box state", "{{ value_json.state }}", icon="mdi:cat"),
        _sensor("mode", "Litter box mode", "{{ value_json.mode }}"),
        _sensor("clean_delay", "Clean delay", "{{ value_json.clean_delay }}", unit="min"),
        _sensor("litter_type", "Litter type", "{{ value_json.litter_type }}"),
        _sensor("night_mode", "Night mode", "{{ value_json.night_mode }}"),
        _sensor("night_start", "Night start hour", "{{ value_json.night_start }}", unit="h", diagnostic=True),
        _sensor("night_end", "Night end hour", "{{ value_json.night_end }}", unit="h", diagnostic=True),
        _sensor("light", "Indicator light", "{{ value_json.light }}"),
        _sensor("sound", "Notification sound", "{{ value_json.sound }}"),
        _sensor("continuous", "Continuous cleaning", "{{ value_json.continuous }}"),
        _sensor("burial", "Soft-stool burial", "{{ value_json.burial }}"),
        _sensor("burial_minutes", "Burial delay", "{{ value_json.burial_minutes }}", unit="min", diagnostic=True),
        _sensor("last_visit_weight", "Last visit weight", "{{ value_json.last_visit_weight }}",
                device_class="weight", unit="kg", measurement=True),
        _sensor("last_visit_duration", "Last visit duration", "{{ value_json.last_visit_duration }}"),
        _sensor("last_deposit_weight", "Last deposit weight", "{{ value_json.last_deposit_weight }}",
                unit="g", icon="mdi:scale-balance", measurement=True),
        _sensor("visit_count", "Visit counter", "{{ value_json.visit_count }}",
                icon="mdi:counter", json_attrs=VISITS_TOPIC),
        _sensor("box_clock", "Box clock", "{{ value_json.box_clock }}", diagnostic=True),
        _sensor("live_scale", "Live scale", "{{ value_json.live_scale }}", device_class="weight", unit="kg",
                diagnostic=True, measurement=True),
        # Litter in the drum: median of recent trusted empty-scale readings. Absent from the state until
        # enough readings exist (HA shows unknown). The "litter low" threshold lives in HA.
        _sensor("litter_weight", "Litter weight", "{{ value_json.litter_weight | default(none) }}",
                device_class="weight", unit="kg", icon="mdi:weight", measurement=True),
        # --- diagnostics for retrospective / live mapping of not-yet-decoded signals ---
        _sensor("slot_count", "Schedule slot count", "{{ value_json.slot_count }}", diagnostic=True),
        _sensor("unmapped_bytes", "Unmapped heartbeat bytes", "{{ value_json.unmapped_bytes }}",
                topic=DIAG_TOPIC, diagnostic=True, icon="mdi:help-rhombus-outline"),
        _sensor("unmapped_changed_at", "Unmapped bytes changed", "{{ value_json.unmapped_changed_at }}",
                topic=DIAG_TOPIC, diagnostic=True),
        _sensor("unmapped_change_count", "Unmapped change count", "{{ value_json.unmapped_change_count }}",
                topic=DIAG_TOPIC, diagnostic=True),
        _sensor("last_unknown_command", "Last unknown frame", "{{ value_json.last_unknown_command | default(none) }}",
                topic=DIAG_TOPIC, diagnostic=True, icon="mdi:alert-rhombus-outline"),
    ]
    items += cat_discovery()
    return items


def cat_discovery():
    """Cat identification sensors (read-only; only when CATLINK_CATS is set)."""
    cats = CFG.cats if CFG is not None else []
    if not cats:
        return []
    items = []
    for slug, _ in cats:
        shown = display_name(slug)
        # The weight sensors carry the whole state as json_attributes so a dashboard tile can show a
        # plain-language trend word as secondary content without a second entity.
        items.append(_sensor(slug + "_weight", shown + " weight", "{{ value_json.%s_weight }}" % slug,
                             device_class="weight", unit="kg", icon="mdi:cat", json_attrs=STATE_TOPIC,
                             measurement=True))
        items.append(_sensor(slug + "_trend_7d", shown + " trend 7d", "{{ value_json.%s_trend_7d }}" % slug,
                             unit="g", diagnostic=True, icon="mdi:trending-up"))
    if len(cats) == 2:
        items.append(_sensor("cat_gap", "Cat gap", "{{ value_json.cat_gap }}", unit="g", diagnostic=True,
                             icon="mdi:arrow-expand-horizontal"))
    # last_visit_cat is the diagnostic "Cat a 82%" text; last_cat / last_conf ride along as
    # json_attributes so a dashboard can render a number-free label from them.
    items.append(_sensor("last_visit_cat", "Last visit cat", "{{ value_json.last_visit_cat }}", icon="mdi:cat",
                         json_attrs=STATE_TOPIC))
    items.append(_sensor("labelled_pct", "Labelled pct", "{{ value_json.labelled_pct }}", unit="%",
                         diagnostic=True, icon="mdi:label-percent"))
    items.append(_sensor("correction_count", "Correction count", "{{ value_json.correction_count }}",
                         diagnostic=True, icon="mdi:pencil"))
    return items


def command_discovery():
    """The COMMAND half: buttons/selects/switches/numbers. Only published when COMMANDS_ENABLED."""
    def base(object_id, name):
        cfg = {"name": name, "unique_id": NODE + "_cmd_" + object_id,
               "command_topic": "%s/%s/set" % (BASE, object_id), "device": DEVICE}
        cfg.update(_avail_block())
        return cfg

    def button(object_id, name, icon):
        cfg = base(object_id, name)
        cfg["payload_press"] = "PRESS"
        cfg["icon"] = icon
        return ("button", object_id, cfg)

    def switch(object_id, name):
        cfg = base(object_id, name)
        cfg.update({"payload_on": "ON", "payload_off": "OFF", "state_topic": STATE_TOPIC,
                    "value_template": "{{ 'ON' if value_json.%s else 'OFF' }}" % object_id})
        return ("switch", object_id, cfg)

    def optimistic_switch(object_id, name):
        # No heartbeat readback is mapped for these, so HA keeps the last commanded state.
        cfg = base(object_id, name)
        cfg.update({"payload_on": "ON", "payload_off": "OFF", "optimistic": True})
        return ("switch", object_id, cfg)

    def select(object_id, name, options, tmpl):
        cfg = base(object_id, name)
        cfg.update({"options": options, "state_topic": STATE_TOPIC, "value_template": tmpl})
        return ("select", object_id, cfg)

    def number(object_id, name, lo, hi, step, unit):
        cfg = base(object_id, name)
        # command_template coerces to int so HA sends "5", not "5.0": the integer-minute builders also
        # tolerate a float string, but this keeps the wire payload clean.
        cfg.update({"min": lo, "max": hi, "step": step, "unit_of_measurement": unit,
                    "state_topic": STATE_TOPIC, "value_template": "{{ value_json.%s }}" % object_id,
                    "command_template": "{{ value | int }}"})
        return ("number", object_id, cfg)

    return [
        button("clean", "Clean now", "mdi:broom"),
        button("pause", "Pause", "mdi:pause"),
        button("resume", "Resume", "mdi:play"),
        button("cancel", "Cancel", "mdi:stop"),
        button("level", "Level litter", "mdi:format-horizontal-align-center"),
        select("mode", "Mode", ["automatic", "manual"], "{{ value_json.mode }}"),
        select("litter_type", "Litter type", ["bentonite", "mixed"], "{{ value_json.litter_type }}"),
        number("clean_delay", "Clean delay", 1, 10, 1, "min"),
        number("burial_minutes", "Burial delay", 1, 60, 1, "min"),
        switch("light", "Indicator light"),
        switch("sound", "Notification sound"),
        switch("continuous", "Continuous cleaning"),
        switch("burial", "Soft-stool burial"),
        optimistic_switch("child_lock", "Child lock"),
        optimistic_switch("kitty", "Kitty mode"),
        # Gateway-internal (never reaches the box): after a full litter change with the box power-cycled
        # on an EMPTY drum, drop the carried re-zero offset. Its topic is subscribed in both builds.
        button("litter_reset", "Litter reset (scale zeroed empty)", "mdi:scale-off"),
    ]


def _config_topic(component, object_id):
    return "%s/%s/%s/%s/config" % (DISCOVERY_PREFIX, component, NODE, object_id)


def publish_discovery(client):
    items = sensor_discovery()
    if COMMANDS_ENABLED:
        items += command_discovery()
    for component, object_id, cfg in items:
        client.publish(_config_topic(component, object_id), json.dumps(cfg, separators=(",", ":")), retain=True)
    cleared = 0
    if not COMMANDS_ENABLED:
        # Sensor-only: remove any command entities left over from an earlier CATLINK_COMMANDS=1 run. An
        # empty retained payload on a discovery topic deletes that entity in HA (and is a no-op if none).
        for component, object_id, _ in command_discovery():
            client.publish(_config_topic(component, object_id), "", retain=True)
            cleared += 1
    log("ha_discovery_published", count=len(items), commands=COMMANDS_ENABLED, commands_cleared=cleared)


# ----------------------------------------------------------------------------------------------------
# HA bridge thread
# ----------------------------------------------------------------------------------------------------
def read_ha_password():
    """Read the Mosquitto password from the systemd credential or CATLINK_HA_PASS_FILE. Never logged."""
    if CFG.ha_pass_file:
        with open(CFG.ha_pass_file) as handle:
            return handle.read().strip()
    raise RuntimeError("ha_no_password_source")  # fixed reason string; carries no secret


def on_ha_message(topic, payload):
    # Expect BASE/<object>/set
    parts = topic.split("/")
    if len(parts) >= 2 and parts[-1] == "set":
        handle_command(parts[-2], payload)


def _error_fields(exc):
    """Log fields for an HA-side error without leaking data: the exception type always; the message only
    when it is one of this code's fixed "ha_..." reason strings; the errno name for OS errors (never the
    message, which can carry a path)."""
    fields = {"error": type(exc).__name__}
    message = str(exc)
    if message.startswith("ha_"):
        fields["reason"] = message
    elif isinstance(exc, OSError) and exc.errno in errno.errorcode:
        fields["reason"] = errno.errorcode[exc.errno]
    return fields


def ha_bridge():
    """Owns the Mosquitto connection. Isolated from the box loop; reconnects forever; never crashes it.
    The password is re-read before every connection attempt (every 5 s while it fails). Under systemd the
    password comes from LoadCredential=, which systemd copies in only when the unit starts (and a missing
    source file stops the unit from starting at all), so there a fixed password needs a unit restart; the
    retry matters when the gateway runs outside systemd with CATLINK_HA_PASS_FILE."""
    client_id = "%s-%d" % (NODE, os.getpid())
    last_avail = None
    last_password_error = None
    while True:
        try:
            password = read_ha_password()
        except Exception as exc:
            fields = _error_fields(exc)
            if fields != last_password_error:   # log once per distinct problem, then retry quietly
                log("ha_no_password", retry_s=5, **fields)
                last_password_error = fields
            time.sleep(5)
            continue
        last_password_error = None
        client = HaMqtt(CFG.ha_host, CFG.ha_port, client_id, CFG.ha_user, password,
                        lwt=(AVAIL_TOPIC, "offline", True), on_message=on_ha_message)
        try:
            client.connect()
            publish_discovery(client)
            if COMMANDS_ENABLED:
                client.subscribe("%s/+/set" % BASE)   # the "+" also covers the cat-ID command topics
                log("ha_subscribed_commands")
            elif CAT_COMMANDS:
                # Sensor-only: still accept the gateway-internal commands (pure analysis, never reach the
                # box), so correction / weigh-in / litter reset work without the box command half.
                for obj in sorted(CAT_COMMANDS):
                    client.subscribe("%s/%s/set" % (BASE, obj))
                log("ha_subscribed_cat_commands")
            # reader thread for inbound PUBLISH (command messages)
            stop = threading.Event()
            reader_errors = []

            def reader():
                try:
                    while not stop.is_set():
                        client.loop_once()
                except Exception as exc:
                    reader_errors.append(exc)
                    stop.set()
                    GW.dirty.set()   # wake the publish loop so the disconnect is noticed at once

            threading.Thread(target=reader, daemon=True).start()
            last_avail = None
            last_state_str = None   # publish STATE/DIAG/VISITS only when the serialized JSON changes,
            last_diag_str = None    # so unchanged retained payloads are not re-published every 5 s.
            last_visits_str = None
            next_ping = time.monotonic() + client.keepalive / 2
            while not stop.is_set():
                state, diag, visits_str, fresh = GW.snapshot()
                avail = "online" if fresh else "offline"
                if avail != last_avail:
                    client.publish(AVAIL_TOPIC, avail, retain=True)
                    last_avail = avail
                # First publish on this connection always goes out (last_*_str is None); thereafter only
                # when the payload actually changed. Availability/ping below are unaffected.
                state_str = json.dumps(state, separators=(",", ":"))
                if state_str != last_state_str:
                    client.publish(STATE_TOPIC, state_str, retain=True)
                    last_state_str = state_str
                diag_str = json.dumps(diag, separators=(",", ":"))
                if diag_str != last_diag_str:
                    client.publish(DIAG_TOPIC, diag_str, retain=True)
                    last_diag_str = diag_str
                # recent_visits -> json_attributes of the visit_count sensor. Retained + published on
                # first connect (from the reloaded log), so the table repopulates after a restart.
                if visits_str != last_visits_str:
                    client.publish(VISITS_TOPIC, visits_str, retain=True)
                    last_visits_str = visits_str
                # wake on new box data (dirty) or every 5 s to refresh availability / ping
                GW.dirty.wait(timeout=5)
                GW.dirty.clear()
                if time.monotonic() >= next_ping:
                    client.ping()
                    next_ping = time.monotonic() + client.keepalive / 2
            # The reader thread ended (broker closed the connection, read timeout, ...): report it as a
            # disconnect with its reason, then reconnect below.
            raise reader_errors[0] if reader_errors else OSError("ha_reader_stopped")
        except Exception as exc:
            log("ha_disconnected", **_error_fields(exc))
        finally:
            client.close()
        time.sleep(5)  # back off, then reconnect


# ====================================================================================================
# main
# ====================================================================================================
def on_term(_signum, _frame):
    log("broker_exit", reason="sigterm", code=0)
    os._exit(0)


def main():
    global active, GW
    try:
        cfg = config_mod.load("gateway")
    except config_mod.ConfigError as exc:
        sys.stderr.write("catlink-gateway: configuration error: %s\n" % exc)
        raise SystemExit(2)
    configure(cfg)
    signal.signal(signal.SIGTERM, on_term)
    if cfg.skip_peer_check:
        log("peer_check_disabled", warning="CATLINK_SKIP_PEER_CHECK=1: ANY peer is accepted on the box port; "
                                           "for tests only")
    GW = GatewayState(cfg.visits_file, cfg.cats, cfg.cats_file, cfg.litter_file)
    if not cfg.no_ha:
        threading.Thread(target=ha_bridge, daemon=True).start()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(BOX_BIND)
    server.listen(8)
    log("broker_start", bind="%s:%d" % BOX_BIND, pid=os.getpid(), mode="gateway", commands=COMMANDS_ENABLED,
        ha=not cfg.no_ha, cats=len(cfg.cats), version=__version__)
    sid = 0
    while True:
        conn, peer = server.accept()
        if not is_box(peer[0]):
            log("foreign_peer", peer=peer[0])
            try:
                conn.close()
            except OSError:
                pass
            continue
        sid += 1
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        session = Session(sid, conn, peer[1])
        with state_lock:
            previous, active = active, session
        if previous is not None:
            previous.replaced = True
            try:
                previous.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        threading.Thread(target=serve, args=(session,), daemon=True).start()


if __name__ == "__main__":
    main()
