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
import hashlib
import json
import math
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
from .cats import CatTracker, display_name, v2_path_for
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
AVAIL_TOPIC = STATE_TOPIC = DIAG_TOPIC = VISITS_TOPIC = CATS_TOPIC = None
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

def configure(cfg):
    """Install a validated config (config.load('gateway')) into the module globals."""
    global CFG, BOX_BIND, BOX_MAC_BYTES, COMMANDS_ENABLED, DISCOVERY_PREFIX, BASE, NODE, DEVICE
    global AVAIL_TOPIC, STATE_TOPIC, DIAG_TOPIC, VISITS_TOPIC, CATS_TOPIC, CAT_COMMANDS
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
    CATS_TOPIC = BASE + "/cats"          # the cat entities' state + attributes: retained, published on change
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


# Visit / deposit / baseline guards (see docs/DESIGN.md). The box scale resolves 10 g and the occupancy flag lags
# the scale by about one heartbeat at entry and exit. Pure STATE/DECODE; no effect on the box side.
EMPTY_BASELINE_MAX_JUMP = 0.15   # kg: a new "empty" reading more than this ABOVE the baseline is not accepted
                                 # outright (a cat on the scale while occupied=0). Also the floor a reading must
                                 # clear above the baseline to count as a cat-on-scale sample, and the level above
                                 # which an unoccupied reading keeps a visit open (the cat is still on the scale).
BASELINE_EMPTIES = 3             # pre-visit baseline = median of the last N trusted empty readings since the
                                 # last clean cycle / visit (one reading moves 60-130 g with the drum position)
BASELINE_DROP_KG = 0.10          # an empty reading more than this BELOW the baseline needs ...
BASELINE_DROP_CONFIRM = 2        # ... this many consecutive readings that agree within ...
BASELINE_DROP_AGREE_KG = 0.05    # ... this, before it is adopted (one-off low readings happen)
BASELINE_CLAMP_KG = 0.005        # pre-visit baseline at/under this = the scale may be clamped at 0.00 (flagged)
DEPOSIT_NOISE_FLOOR_G = 30       # g: a computed deposit below this is at/under the scale noise floor -> 0
DEPOSIT_MAX_G = 300              # g: a computed deposit above this is not a deposit (cat still on, drum moved,
                                 # litter added) -> published as None (unknown), and logged
RELITTER_CONFIRM_SAMPLES = 3     # a higher empty level is adopted as the baseline only after this many
RELITTER_TOLERANCE_KG = 0.10     # consecutive idle+unoccupied heartbeats agree within this (a real litter
                                 # top-up persists; a one-off cat-on-scale lag reading does not).
LITTER_WEIGHT_SAMPLES = 5        # litter_weight = median of the last N trusted empty baselines ...
LITTER_WEIGHT_MIN_SAMPLES = 3    # ... published once at least this many exist (a few minutes after start)
# Per-visit weight. The box's own visit report (`01 03 mm ss kg cg`) is NOT a reliable weight: on a real box it was
# within +-30 g of the cat only about a third of the time, often 100 g or more high and sometimes far LOW. It is
# kept as `peak` (informational, last_visit_peak) and as a cross-check for a single live reading only.
CLUSTER_WIDTH_KG = 0.05          # a plateau = live samples within this of each other (in-visit sd is ~9 g)
TWO_CAT_MIN_SAMPLES = 2          # two plateaus with at least this many samples each ...
TWO_CAT_MIN_GAP_KG = 0.12        # ... and at least this far apart = two cats in one occupancy (two segments)
VISIT_HOLD_MAX_S = 600           # an unoccupied reading still > baseline+0.15 keeps the visit open (the occupancy
                                 # flag can flap while the cat sits still); after this long it is closed (weight
                                 # from the occupied samples, no deposit) ...
VISIT_HOLD_AGREE_KG = 0.02       # ... or earlier, when 2 consecutive held readings agree within this AND the
VISIT_HOLD_NOT_CAT_MARGIN_KG = 0.30  # level is this far below the whole-cat window: the new empty level (a big
                                 # deposit / litter added), not a cat sitting still with the flag off
PAIR_BIAS_S = 20                 # an occupancy runs this much longer than the box's own visit duration (7-32 s
                                 # in every real pairing observed); used when pairing a report with an occupancy
PAIR_OPEN_MIN_S = -15            # a report goes to the open occupancy (no report yet) when its span minus the
PAIR_OPEN_MAX_S = 60             # report's duration is within these; otherwise the closest candidate by score
PAIR_SCORE_MAX_S = 45            # a closed occupancy takes a report only with a pairing score at/under this
REPORT_OVERLAP_S = 30            # a late report upgrades a gateway row only if its visit started by row end + this
TWO_REPORT_AGREE_KG = 0.08       # two reports in one occupancy count as two cats' total only if each is within
                                 # this of its own plateau (else a perched same cat looks like two cats)
REPORT_DUP_S = 5                 # an identical box report within this many seconds is a redelivery (ignored)
REPORT_UPGRADE_S = 30            # an unpairable late report whose duration matches (+- this, around the bias) ...
REPORT_UPGRADE_WINDOW_S = 600    # ... a gateway-sourced row that ended within this IS that visit (upgraded)
GATEWAY_VISIT_MIN_S = 60         # an occupancy the box never reports becomes a gateway-sourced visit when it
GATEWAY_VISIT_MIN_SAMPLES = 2    # lasted this long, has a plateau of this many samples, saw no clean cycle,
                                 # and no report arrived within LATE_REPORT_GRACE_S of its close
LATE_REPORT_GRACE_S = 180        # a box visit report arriving this soon after an occupancy closed may be paired
                                 # with it (seen 40-50 s late when a cat stepped into a running clean cycle)
NO_CATS_WINDOW_KG = (0.5, 15.0)  # the whole-one-cat window when no cats are configured (no tracker)
# The box re-zeroes its scale at power-up WITH the litter in it (a power-cycle took the empty scale from ~3.4 kg
# to 0.00), so litter_weight carries an offset across a re-zero. It is checked ONCE per box session, on the first
# 2 agreeing idle+empty readings: a re-zero = they sit near 0, the last known litter weight was substantial, AND
# the raw reading actually dropped (a reconnect or a gateway restart does not re-anchor the offset).
LITTER_REZERO_MIN_PREV = 1.0     # kg: last known litter weight at/above this ...
LITTER_REZERO_MAX_NOW = 0.30     # kg: ... and the new session's empty reading at/under this ...
LITTER_REZERO_MIN_DROP = 1.0     # kg: ... and at least this far below the raw level the offset predicts
LITTER_REZERO_AGREE_KG = 0.03    # kg: the two session readings that decide must agree within this

# Sanity ranges for values read back from a state file (anything else = a corrupt file, quarantined).
STATE_W_MAX = 15.0               # kg


# ====================================================================================================
# STATE FILES -- atomic + fsync; a corrupt file is moved aside (.corrupt-<ts>) and logged, never silently
# replaced. All under the StateDirectory; best-effort, never raises into the caller.
# ====================================================================================================
def _atomic_write(path, text_):
    """Write `text_` to `path` via tmp + fsync + rename + directory fsync (power cuts)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(text_)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _quarantine(path, what, error):
    """A state file that does not parse is renamed <path>.corrupt-<ts> (kept for inspection) and logged."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = "%s.corrupt-%s" % (path, stamp)
    try:
        os.replace(path, dest)
        log("state_file_corrupt", file=what, moved_to=os.path.basename(dest), error=error)
    except OSError as exc:
        log("state_file_corrupt", file=what, moved_to=None, error=error, rename_error=type(exc).__name__)


def _fnum_range(value, lo, hi, what="number"):
    """A finite real number within [lo, hi] (bools, strings, NaN, +-inf, 1e999, huge ints -> ValueError)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("bad_" + what)
    try:
        v = float(value)
    except (OverflowError, ValueError):
        raise ValueError("bad_" + what) from None
    if not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError("bad_" + what)
    return v


def _num_or_none(value, lo, hi):
    try:
        return _fnum_range(value, lo, hi)
    except ValueError:
        return None


def _json_safe(obj, depth=0):
    """Replace non-finite floats (NaN/inf would be invalid JSON for HA) with None; cap pathological depth."""
    if depth > 20:
        return None
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v, depth + 1) for v in obj]
    return obj


def _finite_tree(obj, depth=0):
    """True if a parsed JSON value has no NaN/inf anywhere and is not pathologically deep."""
    if depth > 20:
        return False
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_finite_tree(v, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return all(_finite_tree(v, depth + 1) for v in obj)
    return True


def _row_seconds(row):
    """A visit row's duration "Xm YYs" -> seconds, or None."""
    m = re.match(r"^(\d+)m (\d+)s$", str(row.get("duration", "")))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _row_epoch(row):
    """A visit row's ISO `ts` -> epoch, or None (rows written by 0.1 have no ts)."""
    try:
        return datetime.datetime.fromisoformat(row["ts"]).timestamp()
    except Exception:
        return None


_ENTER_AGAIN = re.compile(r"\benter (?:it )?again(?: changed by 0\.01 kg)?")


def _ha_cat_fields(fields, cats):
    """What the cat entities publish, from the tracker's fields (HA-side wording only; the tracker is untouched):
      * HA's input_number helpers only send a CHANGED value (state trigger), so the tracker's "enter it again changed
        by 0.01 kg" (its confirmation accepts any value within 50 g) is published with the example for this very
        value: "enter it again changed by 0.01 kg (e.g. 2.31 for 2.30)"; applied once (idempotent);
      * the tracker's error fallback (fields() could not compute; every counter None) publishes cat_status and the
        stale reasons as "unavailable", never a status / "OK" it does not know."""
    out = dict(fields)
    for c in cats:
        text_ = out.get(c + "_last_weigh_in")
        if isinstance(text_, str) and _ENTER_AGAIN.search(text_) and "(e.g." not in text_:
            m = re.match(r"^\S+ (\d+\.\d\d) kg", text_)
            eg = "%.2f for %s" % (float(m.group(1)) + 0.01, m.group(1)) if m else "2.21 for 2.20"
            out[c + "_last_weigh_in"] = _ENTER_AGAIN.sub("enter it again changed by 0.01 kg (e.g. %s)" % eg, text_)
    if all(out.get(c + "_visits_total") is None for c in cats):
        out["cat_status"] = "unavailable"
        for c in cats:
            out[c + "_stale_reason"] = "unavailable"
    return out


# ====================================================================================================
# PER-VISIT OBSERVATION (pure): which live readings are one whole cat, and how many cats were there
# ====================================================================================================
def visit_segments(samples, lo, hi, peak=None):
    """Turn one occupancy's live cat-on-scale readings into 0, 1 or 2 whole-cat segments. Pure.

    samples: [(epoch, net_kg)] chronological, net of the pre-visit baseline. lo/hi: the tracker's whole-one-cat
    window. Readings outside it (paws on the rim, a jump landing, both cats) never count. The rest are grouped
    into plateaus (max width CLUSTER_WIDTH_KG); the visit weight is the median of the largest plateau. Two
    plateaus with >= TWO_CAT_MIN_SAMPLES each, >= TWO_CAT_MIN_GAP_KG apart = two cats (chronological segments).
    A tie between the two largest plateaus = no segment (unknown). All arithmetic in integer grams.
    Returns (segments, info); a segment is {"w", "n", "spread", "peak_delta"} (kg; peak_delta = peak - w)."""
    lo_g, hi_g = int(round(lo * 1000)), int(round(hi * 1000))
    width_g = int(round(CLUSTER_WIDTH_KG * 1000))
    pool = []
    low = high = 0
    for t, w in samples:
        w_g = int(round(w * 1000))
        if w_g < lo_g:
            low += 1
        elif w_g > hi_g:
            high += 1
        else:
            pool.append((w_g, t))
    clusters = []
    while pool:
        pool.sort()
        best = None   # (count, -spread, -i) -> the most samples, then the tightest, then the lightest
        j = 0
        for i in range(len(pool)):
            if j < i:
                j = i
            while j + 1 < len(pool) and pool[j + 1][0] - pool[i][0] <= width_g:
                j += 1
            key = (j - i + 1, -(pool[j][0] - pool[i][0]), -i)
            if best is None or key > best[0]:
                best = (key, i, j)
        _, i, j = best
        clusters.append(pool[i:j + 1])
        pool = pool[:i] + pool[j + 1:]
    info = {"kept": sum(len(c) for c in clusters), "below": low, "above": high,
            "plateaus": [len(c) for c in clusters], "tie": False}

    def seg(cluster):
        ws = sorted(w for w, _ in cluster)
        w = int(round(statistics.median(ws))) / 1000.0
        return {"w": w, "n": len(ws), "spread": (ws[-1] - ws[0]) / 1000.0,
                "peak_delta": None if peak is None else round(peak - w, 3), "_t": min(t for _, t in cluster)}

    segments = []
    if clusters:
        first = clusters[0]
        second = clusters[1] if len(clusters) > 1 else None
        if second is not None and len(first) >= TWO_CAT_MIN_SAMPLES and len(second) >= TWO_CAT_MIN_SAMPLES and \
                abs(statistics.median([w for w, _ in first]) - statistics.median([w for w, _ in second])) \
                >= int(round(TWO_CAT_MIN_GAP_KG * 1000)):
            segments = sorted([seg(first), seg(second)], key=lambda s: s["_t"])
        elif second is not None and len(second) == len(first):
            info["tie"] = True
        else:
            segments = [seg(first)]
    for s in segments:
        s.pop("_t", None)
    return segments, info


# ====================================================================================================
# Shared gateway state (between the box threads and the HA bridge thread)
# ====================================================================================================
class GatewayState:
    """One lock, one snapshot. The box threads write; the HA bridge reads. No MQTT from the box threads."""

    def __init__(self, visits_file, cats=(), cats_file=None, litter_file=None):
        self.visits_file = visits_file
        self.cats_file = cats_file
        self.litter_file = litter_file
        self.cat_config = [(slug, float(seed)) for slug, seed in cats]
        self.lock = threading.Lock()
        self.dirty = threading.Event()
        self.state = {}                      # HA-facing state dict
        self.diag = {}                       # HA-facing diagnostics dict
        self.last_heartbeat_at = None        # monotonic
        self.prev_unmapped = None
        self.unmapped_change_count = 0
        # Empty-scale baseline (litter level) and the per-visit pre-visit baseline.
        self._empty_baseline = None          # TRUSTED empty level, kg: median of _empties (guard reference)
        self._empties = collections.deque(maxlen=BASELINE_EMPTIES)  # trusted readings since the last clean/visit
        self._drum_moved = False             # a clean cycle ran since the last trusted empty reading
        self._high_candidate = None          # a sustained higher empty level under confirmation, kg
        self._high_count = 0                 # consecutive idle+unoccupied readings agreeing with it
        self._low_candidate = None           # a lower empty level under confirmation (drop > BASELINE_DROP_KG)
        self._low_count = 0
        self._litter_hist = collections.deque(maxlen=LITTER_WEIGHT_SAMPLES)  # recent trusted baselines, kg
        self._litter_offset = 0.0             # kg added to the raw empty scale after a box power-up re-zero
        self._litter_known = None             # last published litter_weight (persisted), kg
        self._litter_offset_known = True      # False while litter.json could not be read/trusted: the re-zero
                                              # offset is unknown, so litter_weight is NOT published (and the file
                                              # is not overwritten) until the Litter reset button
        self._new_box_session = False         # a re-zero check is due (once per box session)
        self._session_raw = []                # this session's idle+empty raw readings until the check decides
        # Visit state machine: one open occupancy, plus closed ones awaiting a (late) box report.
        self._occ = None
        self._closed = []
        self._occ_seq = 0
        self._last_report = None              # (body, monotonic) of the previous box report (duplicate guard)
        self._paired_reports = []             # [(body, monotonic)] reports paired within LATE_REPORT_GRACE_S
        self._newest_end = 0.0                # end time of the newest finalized visit (row order, last_visit_*)
        self.recent_visits = collections.deque(maxlen=VISITS_KEEP)  # oldest -> newest (by end time)
        # Cat identification + weight tracking (pure analysis; see cats.py). None unless CATLINK_CATS names cats.
        self.cats = None
        # Every load is isolated: a bad file is quarantined by its loader; nothing here may raise (a crash loop
        # would take the box's broker down). This is the last line.
        for step in (self._load_litter, self._load_visits, self._load_cats, self._restore_last_visit):
            try:
                step()
            except Exception as exc:  # pragma: no cover - defensive
                log("state_restore_error", step=step.__name__, error=type(exc).__name__)

    # ---- cat tracker ----------------------------------------------------------------------------
    def _window(self, now_w):
        """The whole-one-cat weight window (kg) the pipeline keeps readings in."""
        if self.cats is None:
            return NO_CATS_WINDOW_KG
        return self.cats.plausible_range(now_w)

    def _load_cats(self):
        """The tracker persists to cats_v2.json and reads cats.json (the 0.1 format) only to migrate, never writes
        it, so a downgrade finds it untouched. Its loader validates every field; anything it still raises on must
        not stop the gateway: quarantine the v2 file, then the v1 file, then run without persistence.
        A stale cats_v2.json: after a downgrade the older gateway kept writing cats.json (a visit, a weigh-in, a
        correction), so cats.json is no longer the file the tracker migrated. Then the newer cats.json is the truth:
        the stale v2 is moved aside (kept, logged) and cats.json migrated again. When v2 is created, the sha256 of
        the cats.json bytes it came from is stamped into the tracker's own "v1" record (saved and loaded verbatim):
        stale = that sha differs (content, not mtime: a host without an RTC battery can boot with a stale clock).
        A v2 without the stamp falls back to the migrated visit counts, normalised the tracker's way."""
        if not self.cat_config:
            return
        names = tuple(slug for slug, _ in self.cat_config)
        seeds = dict(self.cat_config)
        path = self.cats_file
        v2 = v2_path_for(path)
        if path:
            try:
                if os.path.exists(v2) and os.path.exists(path):
                    stale, why = self._v2_is_stale(v2)
                    if stale:
                        dest = "%s.stale-%s" % (v2, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
                        os.replace(v2, dest)
                        log("cats_v2_stale", moved_to=os.path.basename(dest), why=why)
            except Exception as exc:
                log("cats_v2_stale_check_failed", error=type(exc).__name__)
        created = bool(path) and not os.path.exists(v2)
        for bad in (None, v2, path, "no-persistence"):
            if bad == "no-persistence" or not path:
                if path:
                    log("cats_state_unusable", running_without_persistence=True)
                self.cats = CatTracker(names, seeds, None, now=time.time())
                break
            if bad is not None and os.path.exists(bad):
                _quarantine(bad, "cats_v2" if bad != path else "cats", "tracker_init")
                created = True
            try:
                self.cats = CatTracker(names, seeds, path, now=time.time())
                if created or not os.path.exists(v2):
                    # a migration (or a fresh start) is on disk at once, stamped with the cats.json it came from
                    self._stamp_v1_source()
                    self.cats.save()
                break
            except Exception as exc:
                log("cats_tracker_init_error", error=type(exc).__name__)
        last = self.recent_visits[-1] if self.recent_visits else None
        if last is not None:
            try:
                self.cats.attach_last_record(last)
            except Exception as exc:  # pragma: no cover - defensive
                log("cat_restore_error", error=type(exc).__name__)

    @staticmethod
    def _file_sha256(path):
        try:
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return None

    def _stamp_v1_source(self):
        """Record which cats.json bytes this v2 state came from (sha256; None = there was no cats.json)."""
        v1 = getattr(self.cats, "v1", None)
        if not isinstance(v1, dict):
            v1 = {}
        v1["cats_json_sha256"] = self._file_sha256(self.cats_file) if os.path.exists(self.cats_file) else None
        self.cats.v1 = v1

    def _v2_is_stale(self, v2):
        """(stale, why): has cats.json changed since the tracker's v2 state was made from it?"""
        with open(v2) as fh:
            snap = json.load(fh).get("v1")
        snap = snap if isinstance(snap, dict) else {}
        now_sha = self._file_sha256(self.cats_file)
        if "cats_json_sha256" in snap:
            if now_sha is not None and snap["cats_json_sha256"] != now_sha:
                return True, "cats.json changed since the migration (sha256)"
            return False, None
        # no stamp: the migrated visit counts, normalised like the tracker
        try:
            with open(self.cats_file) as fh:
                cur = json.load(fh)
        except Exception:
            return False, None              # an unreadable / corrupt cats.json never invalidates v2
        names = {slug for slug, _ in self.cat_config}
        if not (isinstance(cur, dict) and isinstance(cur.get("refs"), dict) and set(cur["refs"]) == names):
            return False, None
        norm = lambda d, k: d[k] if isinstance(d.get(k), int) and not isinstance(d.get(k), bool) and d[k] >= 0 else 0  # noqa: E731
        now_counts = (norm(cur, "total_visits"), norm(cur, "labelled_visits"))
        was = (norm(snap, "total_visits"), norm(snap, "labelled_visits"))
        if not snap or now_counts != was:
            return True, "cats.json visit counts %s differ from the migrated %s" % (now_counts, was)
        return False, None

    def _restore_last_visit(self):
        """Restore the last visit's weight / box report / duration / deposit from the persisted visit log, so a
        restart republishes the real last visit instead of a stale value."""
        if self._litter_known is not None and self._litter_offset_known:
            self.state["litter_weight"] = self._litter_known   # restart: no blank litter sensor
        last = self.recent_visits[-1] if self.recent_visits else None
        if not isinstance(last, dict):
            return
        weight = _num_or_none(last.get("weight"), 0.0, STATE_W_MAX)
        if weight is not None or "weight" in last and last["weight"] is None:
            self.state["last_visit_weight"] = weight
        peak = _num_or_none(last.get("peak"), 0.0, 100.0)
        if peak is not None:
            self.state["last_visit_peak"] = peak
        dep = last.get("deposit")
        if (dep is None and "deposit" in last) or (isinstance(dep, int) and not isinstance(dep, bool)
                                                   and 0 <= dep <= DEPOSIT_MAX_G):
            self.state["last_deposit_weight"] = dep
        secs = _row_seconds(last)
        if secs is not None:
            self.state["last_visit_duration"] = "%d:%02d" % divmod(secs, 60)
            self.state["last_visit_seconds"] = secs
        if isinstance(last.get("ts"), str):
            self.state["last_visit_at"] = last["ts"]
        self._newest_end = _row_epoch(last) or 0.0

    def _touch(self):
        self.dirty.set()

    def correct_last(self, payload):
        """GATEWAY-INTERNAL: relabel the most recent visit (payload = a cat slug, or "unknown"). Never touches the
        box or any interlock. Runs on the HA reader thread; isolated by the lock + the caller's try/except."""
        if self.cats is None:
            return None
        cat = (payload or "").strip().lower()
        with self.lock:
            fields = self.cats.correct_last(cat, time.time())
            if fields is not None:
                self.cats.save()
                self._persist_visits()     # the relabelled row (the dashboard table reads it)
                self._touch()
        return fields

    def set_ref(self, cat, payload):
        """GATEWAY-INTERNAL: a kitchen-scale weigh-in of one cat (payload kg). Never touches the box or any
        interlock."""
        if self.cats is None:
            return None
        with self.lock:
            fields = self.cats.set_ref(cat, payload, time.time())
            if fields is not None:
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

    # ---- visit state machine ------------------------------------------------------------------
    def _update_deposit(self):
        """Per-heartbeat visit + baseline bookkeeping. Called under self.lock right after build_state. Pure
        bookkeeping on self.state; never touches the box side.

          * An occupancy opens on the first occupied reading and snapshots the pre-visit baseline (median of the
            last BASELINE_EMPTIES trusted empties since the last clean/visit) and their spread. Every occupied
            reading more than EMPTY_BASELINE_MAX_JUMP above it is a live sample (net kg, timestamped).
          * It closes on the first idle+unoccupied reading back near the baseline. An unoccupied reading still
            above baseline+0.15 is the cat still on the scale (the occupancy flag flapped) and HOLDS the visit
            open -- until 2 consecutive held readings agree within VISIT_HOLD_AGREE_KG at a level that is not a
            cat (the new empty level: a big deposit / litter -> close, deposit from it) or VISIT_HOLD_MAX_S passes
            (close; the occupied samples still give the weight; no deposit).
          * Deposit = post - pre (0 under the noise floor or when the drum moved; None above DEPOSIT_MAX_G).
          * A closed occupancy waits up to LATE_REPORT_GRACE_S for its box report (on_visit pairs it). If none
            comes and it was a real visit (>= GATEWAY_VISIT_MIN_S, a plateau, no clean cycle), it is finalized
            as a gateway-sourced visit; otherwise dropped and logged.
          * Baseline guard: a reading > 0.15 above the baseline is adopted only after RELITTER_CONFIRM_SAMPLES
            agreeing readings (litter top-up); one > BASELINE_DROP_KG below only after BASELINE_DROP_CONFIRM --
            except the first empty reading after a clean cycle, which is adopted directly (the clean removed
            litter)."""
        scale = self.state.get("live_scale")          # kg, from bytes 16-17
        occupied = self.state.get("occupied")
        idle = self.state.get("state") == "idle"
        if scale is None or occupied is None:
            return
        now_m, now_w = time.monotonic(), time.time()
        self._expire_closed(now_m)
        if not idle:
            self._empties.clear()                     # the drum moves: a new baseline window after the cycle
            self._drum_moved = True
        occ = self._occ
        if occupied:
            if occ is None:
                occ = self._occ = self._open_occupancy(now_m, now_w)
            occ["hold"] = []                          # (a flap: the same visit continues)
            occ["last_occ_m"], occ["last_occ_w"] = now_m, now_w
            if not idle:                              # a cat in a running/interrupted clean cycle
                occ["saw_cycle"] = True
                occ["cycle_seen"] = True
            self._sample(occ, scale, now_w)
            return
        if not idle:
            if occ is not None:
                occ["cycle_seen"] = True              # the drum moved after the cat left: no deposit
            return
        # idle and not occupied
        if occ is not None:
            pre = occ["pre"]
            if pre is not None and not occ["cycle_seen"] and scale - pre > EMPTY_BASELINE_MAX_JUMP:
                # still loaded: the cat (occupancy flap) -- or a new level (big deposit / litter added)
                hold = occ["hold"]
                if not hold:
                    occ["hold_since"] = now_m
                    log("visit_hold", occ=occ["id"], scale=scale, pre=pre)
                hold.append(scale)
                # 2 agreeing held readings settle the hold only if the level is NOT a cat (a big deposit / litter
                # added, below the whole-cat window by a margin): a cat sitting still with the flag off would also
                # read steady, and must not be split into two visits -- it is held until the cap instead.
                not_a_cat = scale - pre < self._window(now_w)[0] - VISIT_HOLD_NOT_CAT_MARGIN_KG
                if not_a_cat and len(hold) >= 2 and abs(hold[-1] - hold[-2]) <= VISIT_HOLD_AGREE_KG + 1e-9:
                    log("visit_hold_settled", occ=occ["id"], level=scale)
                    self._close_occupancy(occ, scale, now_m, now_w)     # the new empty level
                    self._baseline_update(scale)
                    self._litter_update(scale)
                    return
                if now_m - occ["hold_since"] < VISIT_HOLD_MAX_S:
                    return
                occ["forced"] = True
                log("visit_hold_cap", occ=occ["id"], seconds=int(now_m - occ["opened_m"]))
                self._close_occupancy(occ, None, now_m, now_w)
                return                                # still not an empty reading: no baseline update
            self._close_occupancy(occ, scale, now_m, now_w)
        self._baseline_update(scale)
        self._litter_update(scale)

    def _open_occupancy(self, now_m, now_w):
        self._occ_seq += 1
        empties = list(self._empties)
        return {"id": self._occ_seq, "opened_m": now_m, "opened_w": now_w, "last_occ_m": now_m,
                "last_occ_w": now_w, "pre": self._empty_baseline,
                "pre_spread": round(max(empties) - min(empties), 3) if empties else None,
                # a drop under confirmation when the cat entered: the weight could be off by it (flagged)
                "baseline_alt": self._low_candidate,
                "samples": [], "saw_cycle": False, "cycle_seen": False, "hold": [], "hold_since": None,
                "forced": False, "reports": [], "post": None, "grams": None, "closed_m": None, "closed_w": None}

    @staticmethod
    def _sample(occ, scale, now_w):
        pre = occ["pre"]
        if pre is not None and scale - pre > EMPTY_BASELINE_MAX_JUMP:
            occ["samples"].append((now_w, round(scale - pre, 3)))

    def _close_occupancy(self, occ, post, now_m, now_w):
        pre = occ["pre"]
        grams = None
        if pre is not None and post is not None:
            grams = int(round((post - pre) * 1000))
            if grams < DEPOSIT_NOISE_FLOOR_G:      # noise, a re-zero, or litter kicked out -> none
                grams = 0
            if occ["cycle_seen"]:                  # the drum moved mid/after-visit: the shift is not a deposit
                grams = 0
            if grams > DEPOSIT_MAX_G:              # reachable: a held visit settling > 0.30 kg up (litter added)
                log("deposit_implausible", occ=occ["id"], grams=grams)
                grams = None
        self.state["last_deposit_weight"] = grams
        occ.update(post=post, grams=grams, closed_m=now_m, closed_w=now_w)
        self._occ = None
        self._empties.clear()                      # the post-visit level starts a new baseline window
        if occ["reports"]:
            self._finalize(occ, occ["reports"], "box")
        else:
            self._closed.append(occ)               # wait for a late report (or become a gateway visit)

    def _occ_seconds(self, occ, now_m=None):
        end = occ["last_occ_m"] if occ["closed_m"] is not None else (now_m if now_m is not None else occ["last_occ_m"])
        return max(0, int(round(end - occ["opened_m"])))

    def _gateway_eligible(self, occ):
        if occ["saw_cycle"] or occ["pre"] is None:
            return False
        if self._occ_seconds(occ) < GATEWAY_VISIT_MIN_S:
            return False
        lo, hi = self._window(time.time())
        segs, _ = visit_segments(occ["samples"], lo, hi)
        return any(s["n"] >= GATEWAY_VISIT_MIN_SAMPLES for s in segs)

    def _expire_closed(self, now_m, flush=False):
        if not self._closed:
            return
        keep = []
        for occ in self._closed:
            if not flush and now_m - occ["closed_m"] <= LATE_REPORT_GRACE_S:
                keep.append(occ)
            elif self._gateway_eligible(occ):
                self._finalize(occ, [], "gateway")
            else:
                log("occupancy_unreported", occ=occ["id"], seconds=self._occ_seconds(occ),
                    samples=len(occ["samples"]), during_clean=occ["saw_cycle"])
        self._closed = keep

    # ---- baseline + litter ----------------------------------------------------------------------
    def _accept_empty(self, scale):
        self._empties.append(scale)
        self._empty_baseline = round(statistics.median(self._empties), 3)
        self._high_candidate, self._high_count = None, 0
        self._low_candidate, self._low_count = None, 0
        self._drum_moved = False

    def _baseline_update(self, scale):
        base = self._empty_baseline
        if base is None:
            self._accept_empty(scale)
            return
        s_g, b_g = int(round(scale * 1000)), int(round(base * 1000))
        if s_g > b_g + int(round(EMPTY_BASELINE_MAX_JUMP * 1000)):
            # one-off cat-on-scale lag reading (rejected) or a real litter top-up (adopted once confirmed)
            self._low_candidate, self._low_count = None, 0
            if self._high_candidate is not None and abs(scale - self._high_candidate) <= RELITTER_TOLERANCE_KG:
                self._high_count += 1
            else:
                self._high_candidate, self._high_count = scale, 1
            if self._high_count >= RELITTER_CONFIRM_SAMPLES:
                self._empties.clear()
                self._accept_empty(scale)
        elif s_g < b_g - int(round(BASELINE_DROP_KG * 1000)):
            self._high_candidate, self._high_count = None, 0
            if self._drum_moved:
                # first empty reading after a clean cycle: the clean really removed litter -> adopt now
                self._empties.clear()
                self._accept_empty(scale)
                return
            # outside a cycle: a one-off low reading (a hand, handling) or real litter removal
            if self._low_candidate is not None and abs(scale - self._low_candidate) <= BASELINE_DROP_AGREE_KG + 1e-9:
                self._low_count += 1
            else:
                self._low_candidate, self._low_count = scale, 1
            if self._low_count >= BASELINE_DROP_CONFIRM:
                first = self._low_candidate
                self._empties.clear()
                self._empties.append(first)
                self._accept_empty(scale)
        else:
            self._accept_empty(scale)

    def _litter_update(self, scale):
        """litter_weight = median of the last few trusted baselines (+ the carried re-zero offset). Pure
        analysis for an HA "litter low" alarm; the box itself has no litter-level sensor."""
        if self._empty_baseline is None:
            return
        if self._new_box_session:
            # Once per box session, from 2 agreeing raw readings: did the box re-zero with the litter in it?
            self._session_raw = (self._session_raw + [scale])[-2:]
            if len(self._session_raw) < 2 or abs(self._session_raw[0] - self._session_raw[1]) > LITTER_REZERO_AGREE_KG + 1e-9:
                return                                 # undecided: keep the published litter weight as is
            raw = round((self._session_raw[0] + self._session_raw[1]) / 2.0, 3)
            self._new_box_session = False
            self._session_raw = []
            prev = self._litter_known
            if (prev is not None and prev >= LITTER_REZERO_MIN_PREV and raw <= LITTER_REZERO_MAX_NOW
                    and (prev - self._litter_offset) - raw >= LITTER_REZERO_MIN_DROP):
                self._litter_offset = round(prev - raw, 2)
                self._litter_hist.clear()
                log("litter_rezero", prev=prev, raw=raw, offset=self._litter_offset)
                self._persist_litter()
        self._litter_hist.append(self._empty_baseline)
        if not self._litter_offset_known:
            self.state.pop("litter_weight", None)       # offset unknown: no (possibly ~0 kg) litter weight
            return
        if len(self._litter_hist) >= LITTER_WEIGHT_MIN_SAMPLES:
            lw = round(statistics.median(self._litter_hist) + self._litter_offset, 2)
            self.state["litter_weight"] = lw
            if lw != self._litter_known:
                self._litter_known = lw
                self._persist_litter()

    # ---- finalize -----------------------------------------------------------------------------
    def _finalize(self, occ, reports, source):
        """Build the per-visit observation, let the tracker label it, insert + persist the visit row (in time
        order). Called under self.lock. `occ` None = a box report with nothing to measure it against (orphan).
        `reports` = the occupancy's box reports (0, 1, or 2 for two cats in one continuous occupancy)."""
        now_w = time.time()
        reports = list(reports or [])
        forced = bool(occ and occ["forced"])
        during_clean = bool(occ and occ["saw_cycle"])
        pre = occ["pre"] if occ else None
        segs, info = [], {"tie": False}
        if occ is not None and pre is not None and not during_clean:
            lo, hi = self._window(now_w)
            segs, info = visit_segments(occ["samples"], lo, hi)
        orphan_reports = []
        if len(reports) == 2 and len(segs) != 2:
            orphan_reports = reports[1:]           # a second report with no second cat: its own unknown row
            reports = reports[:1]
        # each segment's box-report cross-check: its own report (two reports = two cats, chronological), else the
        # one report of the visit
        for i, s in enumerate(segs):
            if reports:
                rep = reports[i] if len(reports) == len(segs) else reports[0]
                s["peak_delta"] = round(rep["peak"] - s["w"], 3)
        peak = reports[0]["peak"] if reports else None
        if reports:
            dur_s = sum(r["dur_s"] for r in reports)
            # the visit's end: its report (sent at exit), or the close if the report came late
            end_w = reports[-1]["at_w"] if occ is None else min(occ["closed_w"] or reports[-1]["at_w"],
                                                                  reports[-1]["at_w"])
        else:
            dur_s, end_w = self._occ_seconds(occ), occ["closed_w"] or occ["last_occ_w"]
        newest = end_w >= self._newest_end
        obs = {"t": end_w, "duration_s": dur_s, "source": source,
               "above_window": int(info.get("above") or 0),   # readings dropped above the window (diagnostic)
               "segments": [dict(s) for s in segs],
               # two box reports in one occupancy = the box counted two visits: the tracker gets their total, its
               # "both cats at once" evidence against "the same cat on both plateaus" (only when each report
               # agrees with its own plateau; else the first report)
               "peak": (round(sum(r["peak"] for r in reports), 2)
                        if len(reports) == 2 and len(segs) == 2
                        and all(s.get("peak_delta") is not None and abs(s["peak_delta"]) <= TWO_REPORT_AGREE_KG + 1e-9
                                for s in segs)
                        else peak),
               "baseline_unc": occ["pre_spread"] if occ else None,
               "clamped": pre is not None and pre <= BASELINE_CLAMP_KG, "orphan": occ is None,
               "during_clean": during_clean, "pre": pre, "newest": newest,
               "baseline_alt": occ["baseline_alt"] if occ else None}
        when = datetime.datetime.fromtimestamp(end_w).astimezone()
        record = {"time": when.strftime("%b %-d %H:%M"), "ts": when.isoformat(timespec="seconds"),
                  "peak": peak, "duration": "%dm %02ds" % divmod(dur_s, 60),
                  "weight": segs[0]["w"] if len(segs) == 1 else None,
                  "deposit": occ["grams"] if occ else None, "source": source,
                  "kind": ("two" if len(segs) == 2 else "none" if not segs
                           else "single" if segs[0]["n"] < 2 else "plateau")}
        if len(reports) == 2:
            record["peaks"] = [r["peak"] for r in reports]
        if segs:
            record["n"] = [s["n"] for s in segs] if len(segs) == 2 else segs[0]["n"]
        if len(segs) == 2:
            record["weights"] = [s["w"] for s in segs]
        for key, value in (("tie", info.get("tie") or None), ("pre", pre), ("baseline_unc", obs["baseline_unc"]),
                           ("clamped", obs["clamped"] or None), ("during_clean", during_clean or None),
                           ("held", forced or None), ("baseline_alt", obs["baseline_alt"])):
            if value is not None:
                record[key] = value
        if self.cats is not None:
            try:
                self.cats.on_visit(obs, record, now_w)
                self.cats.save()
            except Exception as exc:  # pragma: no cover - defensive: never breaks decode or reaches the box side
                log("cat_label_error", error=type(exc).__name__)
        if record.get("cat") != "two_cats":
            # one cat (or unknown, or no tracker): the row keeps ONE weight -- the kept plateau, else none
            record.pop("weights", None)
            record.pop("cats", None)
            if isinstance(record.get("n"), list):
                if record.get("weight") is None:
                    record.pop("n", None)
                else:
                    record["n"] = max(record["n"])
        if len(reports) == 2 and record.get("cat") != "two_cats" and self.cats is not None:
            # two box reports but not two different cats (the tracker merged the plateaus): the box counted two
            # visits, so the second report is its own unknown row (our count matches the box's counter)
            orphan_reports.append(reports[1])
            reports = reports[:1]
            dur_s = reports[0]["dur_s"]
            record.pop("peaks", None)
            record["duration"] = "%dm %02ds" % divmod(dur_s, 60)
        if newest:                                  # HA's "last visit" never goes back in time
            self._newest_end = end_w
            self.state["last_visit_weight"] = record.get("weight")   # never the box report (last_visit_peak)
            self.state["last_visit_peak"] = peak
            self.state["last_visit_duration"] = "%d:%02d" % divmod(dur_s, 60)
            self.state["last_visit_seconds"] = dur_s
            self.state["last_visit_at"] = record["ts"]
        self._insert_row(record, end_w)
        log("visit_finalized", source=source, kind=record["kind"], weight=record.get("weight"), n=record.get("n"),
            cat=record.get("cat"), cats=record.get("cats"), deposit=record["deposit"], peak=peak, newest=newest)
        for rep in orphan_reports:
            self._finalize(None, [rep], "box")

    def _insert_row(self, record, end_w):
        """Append, then restore time order if this visit ended before an already-listed one."""
        self.recent_visits.append(record)
        rows = list(self.recent_visits)
        if len(rows) >= 2 and (_row_epoch(rows[-2]) or 0) > end_w:
            rows.sort(key=lambda r: _row_epoch(r) or 0)
            self.recent_visits.clear()
            self.recent_visits.extend(rows)
        self._persist_visits()

    def flush_inflight(self, timeout=2.0):
        """On SIGTERM (upgrade/restart): finalize what would otherwise be lost -- an open occupancy whose box
        report already arrived, and closed occupancies still waiting for a report that qualify as gateway
        visits. Best-effort; never blocks long."""
        if not self.lock.acquire(timeout=timeout):
            return
        try:
            occ = self._occ
            if occ is not None and occ["reports"]:
                occ.update(closed_m=time.monotonic(), closed_w=time.time())
                self._occ = None
                self._finalize(occ, occ["reports"], "box")
                log("visit_flushed_on_exit", occ=occ["id"])
            self._expire_closed(time.monotonic(), flush=True)
        finally:
            self.lock.release()

    def litter_reset(self):
        """GATEWAY-INTERNAL (button): the box's zero is the EMPTY drum again. For a full litter change: empty
        the drum, power-cycle the box, press this, refill. Drops the carried re-zero offset; the litter weight
        re-appears after LITTER_WEIGHT_MIN_SAMPLES readings. Never touches the box."""
        with self.lock:
            self._litter_offset = 0.0
            self._litter_offset_known = True
            self._litter_known = None
            self._litter_hist.clear()
            self._new_box_session = False
            self._session_raw = []
            self.state.pop("litter_weight", None)
            self._persist_litter()
            log("litter_reset")
            self._touch()
        return True

    def on_box_session(self):
        """A new box MQTT session started (box boot, Wi-Fi reconnect, or a gateway restart)."""
        with self.lock:
            self._new_box_session = True
            self._session_raw = []

    def _load_litter(self):
        """litter.json; anything that does not parse or holds non-finite / out-of-range values is quarantined."""
        if not self.litter_file:
            return
        try:
            with open(self.litter_file) as handle:
                text_ = handle.read()
        except FileNotFoundError:
            return                                     # first run: no offset carried (true 0)
        except Exception as exc:
            reason = "unreadable_" + type(exc).__name__
        else:
            try:
                data = json.loads(text_)
                if not isinstance(data, dict):
                    raise ValueError("not_an_object")
                off = _fnum_range(data.get("offset", 0.0), 0.0, 10.0, "offset")
                known = data.get("litter_weight")
                known = _fnum_range(known, 0.0, 10.0, "litter_weight") if known is not None else None
                self._litter_offset, self._litter_known = off, known
                return
            except Exception as exc:
                reason = type(exc).__name__
        # Unreadable or untrustworthy: the carried re-zero offset (often ~3 kg) is UNKNOWN -- never assume 0, or
        # litter_weight would read ~0 and fire a false litter-low alarm. No litter weight until Litter reset.
        _quarantine(self.litter_file, "litter", reason)
        self._litter_offset_known = False
        log("litter_offset_unknown", reason=reason)

    def _persist_litter(self):
        """Atomic + fsync, best-effort; never raises. Written only when the rounded litter weight changes, and
        never while the offset is unknown (that would replace the real offset with a guess)."""
        if not self.litter_file or not self._litter_offset_known:
            return
        try:
            _atomic_write(self.litter_file, json.dumps({"offset": self._litter_offset,
                                                        "litter_weight": self._litter_known}))
        except OSError as exc:
            log("litter_persist_failed", error=type(exc).__name__)

    def _load_visits(self):
        """Reload the persisted recent-visits log on startup. A line that does not parse (or holds NaN/inf, or
        is pathologically nested) moves the file aside (.corrupt-<ts>, logged); the good rows are kept."""
        try:
            with open(self.visits_file) as handle:
                lines = [line for line in handle if line.strip()]
        except FileNotFoundError:
            return
        except Exception as exc:
            _quarantine(self.visits_file, "visits", "unreadable_" + type(exc).__name__)
            return
        rows, bad = [], 0
        for line in lines:
            try:
                row = json.loads(line)
                ok = isinstance(row, dict) and _finite_tree(row)
            except Exception:
                ok = False
            if ok:
                rows.append(row)
            else:
                bad += 1
        if bad:
            _quarantine(self.visits_file, "visits", "bad_lines_%d" % bad)
        for row in rows[-VISITS_KEEP:]:
            self.recent_visits.append(row)

    def _persist_visits(self):
        """Write the rolling visit log atomically (+ fsync). Called under self.lock. Never raises."""
        try:
            _atomic_write(self.visits_file, "".join(json.dumps(_json_safe(row), separators=(",", ":")) + "\n"
                                                    for row in self.recent_visits))
        except (OSError, TypeError, ValueError) as exc:
            log("visits_persist_failed", error=type(exc).__name__)

    def _pair_score(self, occ, dur_s, now_m):
        """How far an occupancy's span is from a box report's duration, given the real bias: the occupancy runs
        PAIR_BIAS_S longer than the box's own duration (7-32 s in every real pairing observed)."""
        span = self._occ_seconds(occ, now_m) - sum(r["dur_s"] for r in occ["reports"])   # minus a first cat's
        return abs(span - dur_s - PAIR_BIAS_S)

    def _open_fits(self, occ, dur_s, now_m):
        """The open occupancy can take this report: its span (minus a first cat's reported duration) runs
        PAIR_OPEN_MIN_S..PAIR_OPEN_MAX_S longer than the report's duration."""
        span = self._occ_seconds(occ, now_m) - sum(r["dur_s"] for r in occ["reports"])
        return len(occ["reports"]) < 2 and PAIR_OPEN_MIN_S <= span - dur_s <= PAIR_OPEN_MAX_S

    def on_visit(self, body):
        """The box's visit report, body `01 03 mm ss kg cg` (duration mm:ss, a weight kg.hundredths that is NOT
        a reliable weight). Pairing, in order:
          1. a byte-identical report within REPORT_DUP_S, or identical to one already paired within
             LATE_REPORT_GRACE_S, is a redelivery -> ignored;
          2. the open occupancy, when its span fits the report (the box reports at the cat's exit); a second
             report on it = the second cat of one continuous occupancy;
          3. else the best occupancy that closed within LATE_REPORT_GRACE_S (or the open one if it fits), by
             duration with the real span bias, accepted only with a score <= PAIR_SCORE_MAX_S;
          4. else it upgrades a gateway-sourced row whose visit it overlaps (matching duration);
          5. else its own unknown row. A closed occupancy is never paired twice."""
        if len(body) >= 6:
            mm, ss, kg, cg = body[2], body[3], body[4], body[5]
            peak = round(kg + cg / 100.0, 2)
            with self.lock:
                now_m, now_w = time.monotonic(), time.time()
                key = bytes(body[:6])
                self._paired_reports = [(k, t) for k, t in self._paired_reports if now_m - t <= LATE_REPORT_GRACE_S]
                if (self._last_report is not None and self._last_report[0] == key
                        and now_m - self._last_report[1] <= REPORT_DUP_S) \
                        or any(k == key for k, _ in self._paired_reports):
                    log("visit_report_duplicate", reported=mm * 60 + ss)
                    return
                self._last_report = (key, now_m)
                report = {"peak": peak, "dur_s": mm * 60 + ss, "at_w": now_w}
                cur = self._occ
                cur_fits = cur is not None and self._open_fits(cur, report["dur_s"], now_m)
                occ = None
                if cur_fits and not cur["reports"]:
                    occ = cur
                else:
                    cands = [o for o in self._closed
                             if not o["reports"] and now_m - o["closed_m"] <= LATE_REPORT_GRACE_S]
                    if cur_fits:
                        cands.append(cur)
                    scored = [(self._pair_score(o, report["dur_s"], now_m), o) for o in cands]
                    scored = [(sc, o) for sc, o in scored if sc <= PAIR_SCORE_MAX_S]
                    if scored:
                        # the best score; within 15 s of it -> the most recent (the box reports at exit)
                        best = min(sc for sc, _ in scored)
                        occ = max((o for sc, o in scored if sc <= best + 15), key=lambda o: o["opened_m"])
                if occ is not None:
                    self._paired_reports.append((key, now_m))
                if occ is not None and occ is cur:
                    occ["reports"].append(report)    # finalized when the occupancy closes
                elif occ is not None:
                    self._closed.remove(occ)
                    occ["reports"].append(report)
                    self._finalize(occ, occ["reports"], "box")
                    log("visit_late_report", paired=True, occ=occ["id"], seconds=self._occ_seconds(occ),
                        reported=report["dur_s"])
                elif self._upgrade_gateway_row(report):
                    self._paired_reports.append((key, now_m))
                    log("visit_late_report", paired="gateway_row", reported=report["dur_s"])
                else:
                    self._finalize(None, [report], "box")
                    log("visit_late_report", paired=False, reported=report["dur_s"])
                self._touch()

    def _upgrade_gateway_row(self, report):
        """A report too late to pair: if a gateway-sourced row of matching duration (+-REPORT_UPGRADE_S around the
        span bias) is the visit this report describes -- the report's visit (at_w - dur_s .. at_w) overlaps the
        row (starts no later than REPORT_OVERLAP_S after the row ended) -- attach the report (source box) instead
        of counting the visit twice. A report for a later visit (a cat in the box during a restart) never takes
        an earlier cat's row: it becomes its own unknown row."""
        for row in reversed(self.recent_visits):
            end = _row_epoch(row)
            secs = _row_seconds(row)
            if row.get("source") != "gateway" or end is None or secs is None:
                continue
            if report["at_w"] - end > REPORT_UPGRADE_WINDOW_S or end > report["at_w"]:
                continue
            if report["at_w"] - report["dur_s"] > end + REPORT_OVERLAP_S:
                continue
            if abs(secs - report["dur_s"] - PAIR_BIAS_S) <= REPORT_UPGRADE_S:
                row["source"] = "box"
                row["upgraded_from"] = "gateway"
                row["peak"] = report["peak"]
                if row is self.recent_visits[-1]:
                    self.state["last_visit_peak"] = report["peak"]
                self._persist_visits()
                return True
        return False

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

    def cat_payload(self):
        """The retained cats-topic payload (JSON text), or None without cats / on an error. Called under the lock."""
        if self.cats is None:
            return None
        try:
            return json.dumps(_json_safe(_ha_cat_fields(self.cats.fields(time.time()), self.cats.CATS)),
                              separators=(",", ":"), allow_nan=False)
        except Exception as exc:  # pragma: no cover - defensive
            log("cat_fields_error", error=type(exc).__name__)
            return None

    def snapshot(self):
        """(state, diag, visits JSON, fresh, cats JSON or None) for the HA bridge."""
        with self.lock:
            fresh = self.last_heartbeat_at is not None and (time.monotonic() - self.last_heartbeat_at) < OFFLINE_AFTER_S
            # recent_visits attribute payload, most recent first, for the visit_count sensor.
            visits = json.dumps({"recent_visits": _json_safe(list(self.recent_visits)[::-1])}, separators=(",", ":"))
            return dict(self.state), dict(self.diag), visits, fresh, self.cat_payload()


def build_gateway_state(cfg):
    """GatewayState for a validated config. Last line: if building it still raises, the state files are
    quarantined and it is built again from nothing -- never a crash loop of the box's broker on bad state."""
    args = (cfg.visits_file, cfg.cats, cfg.cats_file, cfg.litter_file)
    try:
        return GatewayState(*args)
    except Exception as exc:  # pragma: no cover - defensive
        log("state_init_error", error=type(exc).__name__)
        for path, what in ((cfg.cats_file, "cats"), (v2_path_for(cfg.cats_file), "cats_v2"),
                           (cfg.visits_file, "visits"), (cfg.litter_file, "litter")):
            if path and os.path.exists(path):
                _quarantine(path, what, "init_" + type(exc).__name__)
        return GatewayState(*args)


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
            icon=None, json_attrs=None, measurement=False, state_class=None):
    cfg = {"name": name, "unique_id": NODE + "_" + object_id,
           "state_topic": topic or STATE_TOPIC, "value_template": tmpl, "device": DEVICE}
    cfg.update(_avail_block())
    if device_class:
        cfg["device_class"] = device_class
    if unit:
        cfg["unit_of_measurement"] = unit
    if measurement or state_class:
        cfg["state_class"] = state_class or "measurement"   # numeric: HA keeps long-term statistics
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
        # The box's own visit report, kept apart from the visit weight (it is not a reliable weight).
        _sensor("last_visit_peak", "Last visit box report", "{{ value_json.last_visit_peak | default(none) }}",
                device_class="weight", unit="kg", diagnostic=True),
    ]
    items += cat_discovery()
    return items


def cat_discovery():
    """Cat identification + weight tracking sensors (read-only; only when CATLINK_CATS is set). Every entity
    reads CATS_TOPIC, a small retained payload published only when a cat field changes (never per heartbeat: no
    recorder churn). The weight, status and last-visit sensors take that same payload as json_attributes, so a
    dashboard can show the trend word, the weight range and the raw status without extra entities."""
    cats = CFG.cats if CFG is not None else []
    if not cats:
        return []
    items = []
    for slug, _ in cats:
        shown = display_name(slug)
        items += [
            _sensor(slug + "_weight", shown + " weight", "{{ value_json.%s_weight }}" % slug, topic=CATS_TOPIC,
                    device_class="weight", unit="kg", icon="mdi:cat", json_attrs=CATS_TOPIC, measurement=True),
            _sensor(slug + "_trend_7d", shown + " growth", "{{ value_json.%s_growth_g_week }}" % slug,
                    topic=CATS_TOPIC, unit="g/wk", diagnostic=True, icon="mdi:trending-up"),
            _sensor(slug + "_visits", shown + " visits", "{{ value_json.%s_visits_total }}" % slug,
                    topic=CATS_TOPIC, icon="mdi:counter", state_class="total_increasing"),
            _sensor(slug + "_visits_24h", shown + " visits 24h", "{{ value_json.%s_visits_24h }}" % slug,
                    topic=CATS_TOPIC, icon="mdi:counter", measurement=True),
            _sensor(slug + "_last_seen", shown + " last seen", "{{ value_json.%s_last_seen }}" % slug,
                    topic=CATS_TOPIC, device_class="timestamp", icon="mdi:cat"),
            # why the weight is blank (readable text; the raw reason is the <slug>_stale_reason attribute)
            _sensor(slug + "_stale_reason", shown + " weight status",
                    ("{%% set r = value_json.%(c)s_stale_reason %%}"
                     "{%% if r == 'not_seen' %%}{{ 'Not seen for 3 days' if value_json.%(c)s_last_seen else 'Not seen yet' }}"
                     "{%% elif r == 'not_identified' %%}Not identified: weigh every cat"
                     "{%% elif r == 'sudden_change' %%}Weight changed suddenly: weigh every cat"
                     "{%% elif r == 'unavailable' %%}unavailable"
                     "{%% elif value_json.%(c)s_weight is number %%}OK{%% else %%}Unknown{%% endif %%}") % {"c": slug},
                    topic=CATS_TOPIC, icon="mdi:scale-unbalanced"),
            # the published weight's +-2 sd interval (the weight is the track's prediction now)
            _sensor(slug + "_weight_lo", shown + " weight low", "{{ value_json.%s_weight_lo_kg }}" % slug,
                    topic=CATS_TOPIC, device_class="weight", unit="kg", icon="mdi:arrow-collapse-down",
                    measurement=True),
            _sensor(slug + "_weight_hi", shown + " weight high", "{{ value_json.%s_weight_hi_kg }}" % slug,
                    topic=CATS_TOPIC, device_class="weight", unit="kg", icon="mdi:arrow-collapse-up",
                    measurement=True),
            _sensor(slug + "_last_weigh_in", shown + " last weigh-in",
                    "{{ (value_json.%s_last_weigh_in or 'none yet')[:250] }}" % slug,
                    topic=CATS_TOPIC, icon="mdi:scale"),
        ]
    if len(cats) >= 2:
        items.append(_sensor("cat_gap", "Cat gap", "{{ value_json.cat_gap }}", topic=CATS_TOPIC, unit="g",
                             diagnostic=True, icon="mdi:arrow-expand-horizontal"))
    items += [
        # readable text; the raw value (ok / learning / cant_tell_apart / needs_weigh_in) is the cat_status attribute
        _sensor("cat_status", "Cat ID status",
                "{{ {'ok': 'OK', 'learning': 'Learning the cats', 'cant_tell_apart': \"Can't tell them apart\", "
                "'needs_weigh_in': 'Weigh every cat'}.get(value_json.cat_status, value_json.cat_status) }}",
                topic=CATS_TOPIC, icon="mdi:cat", json_attrs=CATS_TOPIC),
    ]
    if len(cats) >= 2:
        items += [
            # the label-free weight clusters the box sees (published even while the cats can't be named)
            _sensor("cluster_low", "Lightest cat cluster", "{{ value_json.cluster_low_kg }}", topic=CATS_TOPIC,
                    device_class="weight", unit="kg", icon="mdi:scale"),
            _sensor("cluster_high", "Heaviest cat cluster", "{{ value_json.cluster_high_kg }}", topic=CATS_TOPIC,
                    device_class="weight", unit="kg", icon="mdi:scale"),
            _sensor("cluster_low_n", "Lightest cluster visits", "{{ value_json.cluster_low_n }}", topic=CATS_TOPIC,
                    diagnostic=True, icon="mdi:counter"),
            _sensor("cluster_high_n", "Heaviest cluster visits", "{{ value_json.cluster_high_n }}",
                    topic=CATS_TOPIC, diagnostic=True, icon="mdi:counter"),
        ]
    items += [
        _sensor("visits_24h_total", "Visits 24h", "{{ value_json.visits_24h_total }}", topic=CATS_TOPIC,
                diagnostic=True, icon="mdi:counter", measurement=True),
        # the box read every cat heavier / lighter at once (litter top-up, tare drift): not an alarm
        _sensor("scale_shift", "Scale shift", "{{ value_json.scale_shift if value_json.scale_shift else 'none' }}",
                topic=CATS_TOPIC, diagnostic=True, icon="mdi:scale-balance"),
        _sensor("readings_above_window_24h", "Readings above window 24h",
                "{{ value_json.readings_above_window_24h }}", topic=CATS_TOPIC, diagnostic=True,
                icon="mdi:arrow-collapse-up"),
        # last_visit_cat: "Cat b 97%" / "Unknown" / "Cat a + Cat b"; last_cat / last_conf ride along as attributes
        _sensor("last_visit_cat", "Last visit cat", "{{ value_json.last_visit_cat }}", topic=CATS_TOPIC,
                icon="mdi:cat", json_attrs=CATS_TOPIC),
        _sensor("labelled_pct", "Labelled 7d", "{{ value_json.labelled_pct_7d }}", topic=CATS_TOPIC, unit="%",
                diagnostic=True, icon="mdi:label-percent"),
        _sensor("correction_count", "Correction count", "{{ value_json.correction_count }}", topic=CATS_TOPIC,
                diagnostic=True, icon="mdi:pencil"),
        _sensor("unknown_visits", "Unknown visits", "{{ value_json.unknown_visits_total }}", topic=CATS_TOPIC,
                diagnostic=True, icon="mdi:help-circle-outline", state_class="total_increasing"),
    ]
    if len(cats) >= 2:
        items.append(_sensor("two_cat_visits", "Two-cat visits", "{{ value_json.two_cat_visits_total }}",
                             topic=CATS_TOPIC, diagnostic=True, icon="mdi:cat", state_class="total_increasing"))
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
            last_cats_str = None
            next_ping = time.monotonic() + client.keepalive / 2
            while not stop.is_set():
                state, diag, visits_str, fresh, cats_str = GW.snapshot()
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
                # cat entities: their own small retained payload, only on change (a visit, a command, or a
                # time-based field such as visits_24h / a stale weight ageing out)
                if cats_str is not None and cats_str != last_cats_str:
                    client.publish(CATS_TOPIC, cats_str, retain=True)
                    last_cats_str = cats_str
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
    try:
        if GW is not None:
            GW.flush_inflight()      # pure analysis bookkeeping (a reported visit still open); never sends
    except Exception as exc:  # pragma: no cover - defensive
        log("flush_error", error=type(exc).__name__)
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
    GW = build_gateway_state(cfg)
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
