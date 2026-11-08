"""Cat identification + weight tracking: generic N cats (1..MAX_CATS), stdlib only, Python >= 3.9.

PURE ANALYSIS. Never touches the box side (send()/ALLOWED_TX/publish_to_box/build_box_frame/interlocks).
Input: one observation per finalized visit, reduced by the gateway pipeline to 0, 1 or 2 plateau segments.

Model:
  * Per cat a Kalman track of [box-scale weight w (kg), growth rate g (kg/day)]; the rate decays to 0 with
    RATE_TAU_DAYS. The predicted variance grows with time since the last update but, for labelling and for the
    "can't tell apart" test, by at most PRED_CAP_SD / OVERLAP_CAP_SD: an absence, a clock jump or a sparse cat
    never manufactures ambiguity on its own.
  * Each segment is scored against: "whole cat c" (Gaussian, variance = capped prediction + measurement), "c
    perched" (100-300 g below c), "c + extra" (100-450 g above c) and junk (uniform). A segment is labelled c only
    when P(whole c) >= LABEL_MIN_POST and it is within GATE_Z sd of c. Stated conf = P(visitor = c), capped at 99. Equal priors: no visit rate, timing, alternation or
    lighter/heavier ordering enters a label.
  * Two segments are scored jointly: the same cat on both (perched on the rim, then in; prior SAME2_PRIOR), two
    different cats (TWO2_PRIOR) or one junk plateau. A box report >= the two lightest cats' sum makes "same cat"
    10x less likely. "two_cats" needs both segments label-grade for two different cats and P(same cat) < SAME2_MAX;
    otherwise it is ONE visit labelled from its label-grade plateau.
  * Tracks move only on label-grade evidence (a labelled segment, a weigh-in, an owner correction, or the
    label-free net). A one-sided CUSUM per track on its innovations towards another cat detects a sudden change
    (a health signal): both identities go "lost" (labels unknown, <c>_stale_reason "sudden_change", the unnamed
    cluster weights stay published) until the label-free net or a session settles them. When the other cat's
    readings significantly moved the same way by >= 40 g (litter top-up, tare drift) it is a scale shift: no alarm,
    shown in the `scale_shift` field, at most once per cat per COMMON_HOURS. Segment weights are already relative
    to each visit's own empty-box baseline.
  * "Can't tell apart": entered only when two tracks' capped predictions overlap AND at least OVERLAP_MIN_AMBIG
    recent visits fit both. Both are frozen, labels unknown. Exit is the label-free net, on data since the overlap
    began: two separated, tight (sd <= 30 g), well-populated components for >= OVERLAP_EXIT_HOURS, matched by a
    who-stayed-put test in which EVERY cat must still be near where it was (variance growth capped at one day)
    with posterior >= LF_ASSIGN_MIN and the frozen order kept -> resume; otherwise "needs_weigh_in" (identities lost, labels
    unknown, weights None, clusters still published). After OVERLAP_ESCALATE_HOURS the status reads
    needs_weigh_in anyway. Several identities lost together are re-acquired by the same test.
  * Label-free net: N-component mixture (+ outlier, linear drift) over LF_WINDOW_DAYS of admissible segments,
    labelled or not. It bootstraps seed tracks (fresh install: seed ORDER), exits overlaps, re-acquires lost
    identities, repairs stuck tracks, and feeds cluster_low/high_kg to the owner.
  * Weigh-ins: timestamped kitchen weights. WEIGHTS ARE PUBLISHED ON THE KITCHEN SCALE: box and kitchen scale are
    taken to read the same (no offset is learned; a constant difference shows up 1:1 in the published weight).
    For a cat whose identity is settled a weigh-in is a measurement; one far from its track is rejected with a
    published reason unless a second entry (within 50 g, 24 h) confirms it AND the fresh clusters do not contradict
    it (else every cat must be weighed); one that fits another cat's track swaps the two only on ANOTHER cat's
    statement pointing the same way within SWAP_VOTE_HOURS (8 days): a re-entry never swaps. For cats whose
    identity is not settled (lost, overlap, doubt) entries wait for a SESSION: every such cat weighed within 24 h,
    values >= 60 g apart, each near a distinct fresh label-free cluster. Never by elimination. The same value
    (<= 5 g, or <= 30 g when it does not fit its own track) entered for two cats in a session is rejected for both.
    A session's growth rate comes from the cat's own earlier ACCEPTED weigh-in or its own track, clamped. In "can't tell apart", a session too close to settle
    identity still updates each track that its value fits (the kitchen scale knows who is who).
  * In-box identity anchor: correct_last of every unsettled cat within 24 h on plateau-grade box readings >= 60 g
    apart, each on a distinct fresh cluster, is a session too (box scale only, no kitchen/box offset).
  * Published weight = the track's prediction now (rate included), with <c>_weight_lo_kg / _hi_kg = +-2 sd; never
    with sd > PUBLISH_MAX_SD. In "can't tell apart" it stays published while the cat is still seen (ambiguous
    visits) and the sd allows, with growth extrapolated <= OVERLAP_RATE_DAYS; once the overlap escalates
    (needs_weigh_in) only for ESCALATED_WI_HOURS after the cat's last accepted weigh-in. Outside an overlap a cat
    that is unidentified, or whose newest accepted weigh-in disagrees with the track, is not published.
  * Status is "ok" only with fresh evidence: every cat labelled or weighed within FRESH_OK_HOURS; no cat
    unidentified (no label for max(12 h, 4 median gaps) capped at 48 h while >= 3 weighed visits since its last
    label stayed unknown); a fresh label-free fit has a cluster within 75 g of every track (with a single cluster:
    unless the track has readings of its own; that weight is not published either); and a weigh-in of the last
    7 days lies inside the published +-2 sd interval + 50 g.
  * Trend: Theil-Sen slope of labelled samples with a robust Student-t bound; "not enough data" below
    TREND_MIN_N samples or a TREND_MIN_SPAN_DAYS span.
  * Persistence: <path stem>_v2.json (the v1 file at `path` is read for migration and never written).
  * Everything time-dependent uses the injected `now` / obs["t"]; time.time() is only a default.

Interface: CatTracker(cats, seeds, path, now=None); plausible_range(now); on_visit(obs, record, now);
correct_last(cat, now); set_ref(cat, payload, now); fields(now); save(); attach_last_record(record);
diagnostics(now). Logs go through the package's JSONL log() (catlink_local/log.py). See docs/DESIGN.md.
"""
import datetime
import itertools
import json
import math
import os
import time

from .log import log

# ---------------------------------------------------------------------------------------------------
# Tuning constants (kg, days unless stated). Measured noise on a real box (plateau median sd 24-28 g between
# visits, ~10 g within a visit, 10 g resolution).
# ---------------------------------------------------------------------------------------------------
R_VISIT_SD = 0.025          # sd of a multi-sample plateau median around the cat's true weight
R_SINGLE_SD = 0.030         # single sample that the box report confirms
R_DISAGREE_SD = 0.050       # extra sd of a single sample the box report contradicts (labelled, never a sample)
SINGLE_AGREE_KG = 0.08      # |peak_delta| up to this = the box report confirms a single sample
R_CLAMP_SD = 0.015          # extra sd when the pre-visit baseline sat on the 0.00 clamp
R_WEIGHIN_SD = 0.030        # kitchen-scale weigh-in of a squirming kitten, incl. any box/kitchen difference
Q_W = 0.010 ** 2            # weight random walk, kg^2/day
Q_G = 0.004 ** 2            # growth-rate random walk, (kg/day)^2/day
RATE_TAU_DAYS = 60.0        # a coasting rate decays to 0 with this time constant (kittens keep growing)
PRED_CAP_SD = 0.060         # labelling: predicted variance grows by at most this^2 over the last update
PRED_RATE_DAYS = 3.0        # ... and its rate is extrapolated for at most this long
OVERLAP_CAP_SD = 0.030      # overlap test: by at most this^2
SEED_SD = 0.10              # prior sd of a configured seed weight
SEED_RATE_SD = 0.010        # prior sd of the growth rate (kg/day = 70 g/week)
SEED_LABEL_DAYS = 3.0       # with several cats, a cat still on its seed is labelled only this long

P_PARTIAL = 0.04            # a cat partly on the scale (perched on the rim): reads below it ...
PARTIAL_RANGE = (0.10, 0.30)  # ... by this much (seen: a cat on the rim read 150-180 g light)
P_EXTRA = 0.03              # a cat plus something (the other cat's paws, kicked litter): reads above it ...
EXTRA_RANGE = (0.10, 0.45)  # ... by this much
P_JUNK = 0.03               # nobody / nothing in particular: uniform over UNIFORM_WIDTH kg
UNIFORM_WIDTH = 1.5
SINGLE_OUTLIER_MULT = 2.0   # a single-sample visit is not-one-cat twice as often
SAME2_PRIOR = 0.45          # two plateaus: prior that they are the SAME cat (rim then in, paws then in) ...
SAME2_PARTIAL_SHARE = 0.7   # ... the other plateau perched (else "plus something") ...
TWO2_PRIOR = 0.45           # ... two different cats ...
JUNK2_PRIOR = 0.10          # ... or one plateau junk
SAME2_MAX = 0.10            # "two_cats" only if P(same cat) is below this
TWO_PEAK_MARGIN = 0.30      # a box report >= the two lightest cats' sum - this means both were on at once ...
TWO_PEAK_SAME_FACTOR = 0.1  # ... and makes "the same cat on both plateaus" this much less likely

LABEL_MIN_POST = 0.85       # label a segment only when P(whole cat) >= this ...
GATE_Z = 3.0                # ... and it is within this many predictive sd of that cat
CONF_CAP = 99               # the stated % never reaches 100 (weight alone is never certain)

CUSUM_K = 0.5               # CUSUM on innovations towards another cat (sd units): allowance ...
CUSUM_H = 3.0               # ... and alarm level (a sudden change towards another cat -> "sudden_change")
COMMON_HOURS = 48.0         # an alarm is a scale / litter shift, not a cat, when the other cat's innovations in
COMMON_MIN_N = 4            # ... this window (at least this many) moved the same way by at least ...
COMMON_SHARE = 0.5          # ... this share of the alarming cat's mean shift, significantly (mean z * sqrt(n) >= ...
COMMON_Z = 2.0              # ... this), or >= COMMON_MIN_NEAR of its readings sit at the shifted place (and >= 2x as
COMMON_MIN_NEAR = 3         # ... many as at the old one); the shift must be >= COMMON_MIN_SHIFT_KG; at most one
COMMON_MIN_SHIFT_KG = 0.040  # ... suppression per cat per COMMON_HOURS. Published as `scale_shift` for ...
SCALE_SHIFT_SHOW_HOURS = 72.0  # ... this long

OVERLAP_ENTER_Z = 2.5       # capped-prediction separation (sd) below which two cats may be indistinguishable ...
OVERLAP_AMBIG_Z = 2.5       # ... and a visit within this many sd of both counts as ambiguous evidence ...
OVERLAP_MIN_AMBIG = 2       # ... of which this many ...
OVERLAP_AMBIG_HOURS = 48.0  # ... within this window are required to enter "can't tell apart"
OVERLAP_EXIT_HOURS = 24.0   # separated label-free components for this long end it (resume / needs_weigh_in)
OVERLAP_ESCALATE_HOURS = 48.0  # after this long the status reads needs_weigh_in
LF_EXIT_MAX_SD = 0.030      # ... and is this tight (a perched / partial smear is wider)
LF_EXIT_MIN_SHARE = 0.15    # an overlap-exit component holds at least this share of the visits since the overlap
ANCHOR_EPS = 2e-4           # who-stayed-put exit test: weight of the "this cat moved" component ...
ANCHOR_MAX_KG_PER_DAY = 0.040  # ... and a pairing needing a faster change than this (+50 g) is impossible
ANCHOR_STAY_Z = 2.0         # ... EVERY cat must be within this many sd of where it was (variance growth <= 1 day)
HOME_DAYS = 3.0             # a cat's "home" before an episode = median of its labelled samples in this window
ANCHOR_WIDE = 4.0           # ... which is this many times wider than "stayed put"

LF_WINDOW_DAYS = 3.0        # admissible segments (labelled or not) used for the mixture fit
LF_MAX_POINTS = 300         # newest points used (bounds the cost)
LF_SD = 0.030               # component sd used by the fit
LF_OUT_W = 0.10             # outlier weight in the fit
LF_SLOPE_PRIOR_SD = 0.030   # kg/day ridge on each component's drift
LF_ITERS = 60
LF_MIN_N = 3.0              # a component needs this many effective members ...
LF_BOOT_MIN_N = 3.5         # (fresh install, every cat still on its seed: this many)
LF_MAX_SD = 0.045           # ... a member spread at most this ...
LF_MIN_SEP = 0.10           # ... and to be this far from every other component
LF_ASSIGN_MIN = 0.99        # posterior of the best component->cat assignment needed to act
LF_ASSIGN_EXTRA_SD = 0.020  # model slack in each comparison of the assignment
LF_EVERY_HOURS = 6.0        # while everything is "ok" the net runs at most this often
MIGRATE_MAX_DEV_KG = 0.10   # v1 samples further than this from their cat's median are not migrated
REPAIR_Z = 4.0              # an established track this many sd from its matched component is re-anchored ...
REPAIR_MAX_SUPPORT = 1      # ... only if fewer than this many points of the last ...
REPAIR_AFTER_WEIGHIN_HOURS = 48.0  # an accepted weigh-in is not overruled by the box for this long
REPAIR_SUPPORT_HOURS = 48.0  # ... this many hours still sit on it (stuck)
LEARN_MIN_EFF = 3.0         # updates before a track counts as learned
SUPPORT_GATE_KG = 0.075     # a published weight needs a fresh label-free cluster within this (when >= 2 exist) ...
SUPPORT_MAX_AGE_HOURS = 24.0  # ... from a fit at most this old ...
SUPPORT_MIN_COMPS = 1       # ... that found at least this many valid clusters (with only one: and the track has
SUPPORT_OWN_MIN = 2         # ... fewer than this many readings of its own in REPAIR_SUPPORT_HOURS)
UNIDENTIFIED_HOURS = 48.0   # a cat not labelled for this long while ...
UNIDENTIFIED_MIN_UNKNOWN = 3  # ... at least this many weighed visits stayed unknown -> needs_weigh_in
UNID_GAP_MULT = 4.0         # a frequent visitor is "unidentified" sooner: after this many of its median gaps
UNID_MIN_HOURS = 12.0       # ... between labelled visits (last VISITS_KEEP_DAYS), but never sooner than this
STATUS_WIDEN_KG = 0.60      # while the status is not ok the window reaches this far above the heaviest track
CLUSTER_MAX_AGE_HOURS = 72.0  # cluster_low/high are published from a fit at most this old

WEIGHIN_GATE_Z = 4.0        # a weigh-in further than this from its track is not a plain update
WEIGHIN_CONFIRM_KG = 0.05   # a second entry within this ...
WEIGHIN_CONFIRM_HOURS = 24.0  # ... and this long confirms a rejected one
WEIGHIN_MAX_RESET_KG = 0.50  # an unconfirmed reset never moves a track further than this
WEIGHIN_MIN_KG, WEIGHIN_MAX_KG = 0.2, 12.0
WEIGHIN_SESSION_HOURS = 24.0  # weigh-ins of all cats within this form a session (identity is settled only so)
WEIGHIN_SESSION_MIN_SEP = 0.06  # ... with values at least this far apart
SESSION_RATE_RANGE = (-0.02, 0.04)  # kg/day: a session-anchored kitten's growth rate is clamped to this
WEIGHIN_DUP_KG = 0.005      # the same value (within this) entered for two cats in a session is rejected ...
WEIGHIN_DUP_FAR_KG = 0.030  # ... and within this when it does not fit its own cat's track (same cat weighed twice)
SWAP_MATCH_Z = 2.0          # a weigh-in / correction "matches" another cat's track within this many sd
SWAP_VOTE_HOURS = 192.0     # two owner corrections pointing at the same swap within this -> swap
BOXID_SESSION_HOURS = 24.0  # correct_last of every unsettled cat within this (box readings, no kitchen offset) ...
BOXID_MIN_SEP = 0.06        # ... on visits at least this far apart = a session that settles identity
FRESH_OK_HOURS = 72.0       # "ok" needs a labelled visit or accepted weigh-in of every cat within this
WEIGHIN_CHECK_DAYS = 7.0    # a weigh-in this recent must lie inside the published +-2 sd interval ...
WEIGHIN_CHECK_TOL_KG = 0.05  # ... widened by this (kitchen vs box), else needs_weigh_in

TREND_WINDOW_DAYS = 10.0    # growth = Theil-Sen slope of the labelled samples in this window ...
TREND_MIN_N = 6             # ... with at least this many ...
TREND_MIN_SPAN_DAYS = 5.0   # ... spanning this long, else "not enough data"
TREND_SCALE_FLOOR = 0.025   # residual scale for the slope uncertainty is at least this
TREND_MIN_G_WEEK = 40       # gaining / losing: one-sided 99.5 % bound excludes 0 and |slope| >= this
TREND_SETTLE_DAYS = 1.0     # samples count from this long after a track was (re)established

STALE_HOURS = 72.0          # no labelled visit / accepted weigh-in for this long -> stale, weight None ...
OVERLAP_SEEN_HOURS = 24.0   # ... unless the cat is in "can't tell apart" with ambiguous visits this recent
PUBLISH_MAX_SD = 0.075      # the published weight is the track's prediction now; never with a larger sd than this
OVERLAP_RATE_DAYS = 3.0     # in "can't tell apart" the published growth line extrapolates the rate this long at most
ESCALATED_WI_HOURS = 120.0  # after the overlap escalated, a weight is published only this long after the cat's
                            # last accepted weigh-in
PLAUSIBLE_MARGIN = 0.30     # whole-one-cat window: at least this around every cat ...
SEED_RANGE_KG = 1.0         # ... at least this around a cat still on its seed ...
PLAUSIBLE_Z = 4.0           # ... or this many (uncapped) predictive sd, at most PLAUSIBLE_MAX_MARGIN
PLAUSIBLE_MAX_MARGIN = 1.0
W_MIN, W_MAX = 0.2, 15.0    # any weight outside this is never used
T_MIN, T_MAX = 1577836800.0, 4102444800.0   # 2020-01-01 .. 2100-01-01
SAMPLES_KEEP_DAYS = 28.0
SAMPLES_MAX = 400
RECENT_MAX = 2000
VISITS_KEEP_DAYS = 8.0
WEIGHINS_MAX = 50
MAX_CATS = 6
STATE_VERSION = 2

DAY = 86400.0
_LOG2PI = math.log(2 * math.pi)
_TQ = {1: 63.66, 2: 9.925, 3: 5.841, 4: 4.604, 5: 4.032, 6: 3.707, 7: 3.499, 8: 3.355, 9: 3.250, 10: 3.169,
       12: 3.055, 15: 2.947, 20: 2.845, 30: 2.750, 60: 2.660}   # one-sided 99.5 % Student-t quantiles


def display_name(slug):
    return slug.replace("_", " ").capitalize()


def _fnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _num(x, lo, hi):
    """x as float if it is a finite number within [lo, hi], else raise ValueError."""
    if not _fnum(x) or not lo <= x <= hi:
        raise ValueError("bad number")
    return float(x)


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_logpdf(x, m, var):
    return -0.5 * (_LOG2PI + math.log(var) + (x - m) * (x - m) / var)


def _box_pdf(y, lo, hi, s):
    """Density of U(lo, hi) + N(0, s^2) at y."""
    return max(0.0, _phi((y - lo) / s) - _phi((y - hi) / s)) / (hi - lo)


def _logsumexp(vals):
    m = max(vals)
    if m == -math.inf:
        return m
    return m + math.log(sum(math.exp(v - m) for v in vals))


def _tq(df):
    if df <= 0:
        return math.inf
    best = _TQ[1]
    for k in sorted(_TQ):
        if k <= df:
            best = _TQ[k]
    return best if df < 60 else 2.576


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def _grams(kg):
    return None if kg is None else int(round(kg * 1000))


def _iso(t):
    return None if t is None else datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def v2_path_for(path):
    """cats.json -> cats_v2.json (the v1 file is never written by this tracker: downgrade-safe)."""
    if not path:
        return None
    return path[:-5] + "_v2.json" if path.endswith(".json") else path + ".v2"


class CatTracker:
    def __init__(self, cats, seeds, path, now=None):
        now = time.time() if now is None else float(now)
        cats = tuple(str(c) for c in cats)
        if not 1 <= len(cats) <= MAX_CATS or len(set(cats)) != len(cats):
            raise ValueError("CatTracker needs 1..%d distinct cats" % MAX_CATS)
        if isinstance(seeds, dict):
            seed_map = {c: float(seeds[c]) for c in cats}
        else:
            seeds = list(seeds)
            if len(seeds) != len(cats):
                raise ValueError("one seed per cat")
            seed_map = {c: float(s) for c, s in zip(cats, seeds)}
        for c, s in seed_map.items():
            if not _fnum(s) or not W_MIN <= s <= W_MAX:
                raise ValueError("bad seed for %s" % c)
        self.CATS = cats
        self.seeds = seed_map
        self.path = path                    # v1 file: read for migration only
        self.v2_path = v2_path_for(path)    # what this tracker reads and writes
        self.save_blocked = False
        self._fresh(now)
        self._load(now)

    # ================================================================================================
    # state
    # ================================================================================================
    def _fresh(self, now):
        self.tracks = {c: self._seed_track(c, now) for c in self.CATS}
        self.samples = {c: [] for c in self.CATS}        # [t, y, kind] labelled segments (trend, offset pairs)
        self.recent = []                                  # [t, y, R] admissible segments (label-free net)
        self.ambig = []                                   # [t, [cats it fitted]] ambiguous segments
        self.visits = []                                  # [t, [credited cats or "unknown"], n_weighed]
        self.weighins = []                                # {"t","cat","kg","action","reason","box"}
        self.overlap = []                                 # {"pair":[a,b],"since":t,"sep_since":t|None,"order":[..]}
        self.visits_total = {c: 0 for c in self.CATS}
        self.unknown_visits_total = 0
        self.two_cat_visits_total = 0
        self.correction_count = 0
        self.repairs_total = 0
        self.swap_votes = []                              # [t, cat, other]
        self.box_ids = []                                 # [t, cat, kg]: correct_last readings (box scale)
        self.scale_events = []                            # [t, cat, shift_g]: CUSUM alarms read as a scale shift
        self.innov = {c: [] for c in self.CATS}           # [t, standardized innovation] (common-mode check)
        self.cluster = None                               # {"t", "comps": [[kg, n], ...]} last valid fit
        self.above = []                                   # [t, n] readings above the window (from the pipeline)
        self.lf_last = -math.inf
        self.last = None                                  # last visit (correct_last), JSON-safe
        self.last_fields = {"last_cat": None, "last_conf": None, "last_visit_cat": None}
        self.v1 = None
        self._last_record = None

    def _seed_track(self, c, now):
        return {"w": self.seeds[c], "g": 0.0, "Pww": SEED_SD ** 2, "Pwg": 0.0, "Pgg": SEED_RATE_SD ** 2,
                "P_upd": SEED_SD ** 2, "t": now, "n_eff": 0.0, "ident": "init", "doubt": False,
                "last_seen": None, "last_wi": None, "measured": False, "since": None, "cusum": 0.0,
                "lost_since": None, "sudden": None}

    # ================================================================================================
    # Kalman core
    # ================================================================================================
    @staticmethod
    def _predict(tr, t, cap=None):
        """(w, g, Pww, Pwg, Pgg) of track tr predicted to time t (never backwards). With `cap`, Pww is limited to
        the post-update variance + cap^2 (absence / clock jumps do not manufacture ambiguity)."""
        w, g, Pww, Pwg, Pgg = tr["w"], tr["g"], tr["Pww"], tr["Pwg"], tr["Pgg"]
        dt = min(max(0.0, (t - tr["t"]) / DAY), 3650.0)
        if cap is not None:
            # labelling / overlap: a rate is not extrapolated for more than PRED_RATE_DAYS (a coasting track that
            # was wrong about the rate must not walk onto junk readings with a capped, confident variance)
            dt = min(dt, PRED_RATE_DAYS)
        if dt > 0.0:
            tau = RATE_TAU_DAYS
            x = dt / tau
            phi = math.exp(-x)
            a = tau * (1.0 - phi)
            e1, e2 = 1.0 - phi, 1.0 - phi * phi
            if x < 1e-4:
                i_aa, i_ab, i_bb = dt ** 3 / 3.0, dt ** 2 / 2.0, dt
            else:
                i_bb = tau / 2.0 * e2
                i_ab = tau * tau * (e1 - e2 / 2.0)
                i_aa = tau * tau * (dt - 2.0 * tau * e1 + tau / 2.0 * e2)
            nPww = Pww + 2 * a * Pwg + a * a * Pgg + Q_W * dt + Q_G * i_aa
            nPwg = phi * (Pwg + a * Pgg) + Q_G * i_ab
            nPgg = phi * phi * Pgg + Q_G * i_bb
            w, g, Pww, Pwg, Pgg = w + a * g, phi * g, nPww, nPwg, nPgg
        if cap is not None:
            Pww = min(Pww, tr.get("P_upd", Pww) + cap * cap)
        return w, g, Pww, Pwg, Pgg

    def _pred(self, c, t, cap=None):
        return self._predict(self.tracks[c], t, cap)

    def _kf(self, c, t, y, R):
        """Standard Kalman update of track c at time t with measurement y (variance R)."""
        tr = self.tracks[c]
        w, g, Pww, Pwg, Pgg = self._predict(tr, t)
        S = Pww + R
        kw, kg = Pww / S, Pwg / S
        nu = y - w
        tr["w"] = min(W_MAX, max(W_MIN, w + kw * nu))       # never a state the loader would reject
        tr["g"] = min(0.5, max(-0.5, g + kg * nu))
        tr["Pww"] = max(Pww - kw * Pww, 1e-8)
        tr["Pwg"] = Pwg - kw * Pwg
        tr["Pgg"] = max(Pgg - kg * Pwg, 1e-10)
        lim = math.sqrt(tr["Pww"] * tr["Pgg"]) * 0.999
        tr["Pwg"] = max(-lim, min(lim, tr["Pwg"]))
        tr["P_upd"] = tr["Pww"]
        tr["t"] = max(tr["t"], t)
        tr["measured"] = True
        return nu, S

    def _anchor(self, c, t, w, g, Pww, Pgg):
        tr = self.tracks[c]
        tr.update(w=min(W_MAX, max(W_MIN, w)), g=min(0.5, max(-0.5, g)), Pww=min(Pww, 4.0), Pwg=0.0,
                  Pgg=min(Pgg, 1.0), P_upd=min(Pww, 4.0), t=t, measured=True, cusum=0.0)

    # ================================================================================================
    # classification
    # ================================================================================================
    def _seg_loglik(self, y, R, preds, single):
        """log(prior * likelihood) per class (kind, cat) for one segment. preds: {c: (m, Pww)}."""
        n = len(self.CATS)
        mult = SINGLE_OUTLIER_MULT if single else 1.0
        pp, pe, pu = P_PARTIAL * mult, P_EXTRA * mult, P_JUNK * mult
        pw = max(1e-6, 1.0 - pp - pe - pu)
        out = {}
        for c in self.CATS:
            m, Pww = preds[c]
            var = Pww + R
            s = math.sqrt(var)
            out[("w", c)] = math.log(pw / n) + _norm_logpdf(y, m, var)
            dp = _box_pdf(y, m - PARTIAL_RANGE[1], m - PARTIAL_RANGE[0], s)
            de = _box_pdf(y, m + EXTRA_RANGE[0], m + EXTRA_RANGE[1], s)
            out[("p", c)] = math.log(pp / n) + (math.log(dp) if dp > 0 else -math.inf)
            out[("e", c)] = math.log(pe / n) + (math.log(de) if de > 0 else -math.inf)
        out[("u", None)] = math.log(pu / UNIFORM_WIDTH)
        return out

    def _preds(self, t):
        out = {}
        for c in self.CATS:
            p = self._pred(c, t, cap=PRED_CAP_SD)
            out[c] = (p[0], p[2])
        return out

    def _seg_logdens(self, y, R, preds):
        """log density per class (kind, cat) for one segment, no priors."""
        out = {}
        for c in self.CATS:
            m, Pww = preds[c]
            var = Pww + R
            s = math.sqrt(var)
            out[("w", c)] = _norm_logpdf(y, m, var)
            dp = _box_pdf(y, m - PARTIAL_RANGE[1], m - PARTIAL_RANGE[0], s)
            de = _box_pdf(y, m + EXTRA_RANGE[0], m + EXTRA_RANGE[1], s)
            out[("p", c)] = math.log(dp) if dp > 0 else -math.inf
            out[("e", c)] = math.log(de) if de > 0 else -math.inf
        out[("u", None)] = -math.log(UNIFORM_WIDTH)
        return out

    def _posteriors(self, segs, t, peak=None):
        """segs: [(y, R, single)] (1 or 2). Returns (per-segment marginal {class: p}, P(same cat on both)).
        Two segments: hypotheses "the same cat, whole on one plateau and perched (p) / plus something (e) on the
        other" (prior SAME2_PRIOR), "two different cats" (TWO2_PRIOR; each plateau any class of its cat) and "one
        plateau is junk" (JUNK2_PRIOR). Also leaves self._one_cat = {(cat, seg index): P(one-cat visit of cat
        with that plateau whole)}."""
        preds = self._preds(t)
        self._one_cat = {}
        if len(segs) == 1:
            ll = self._seg_loglik(segs[0][0], segs[0][1], preds, segs[0][2])
            z = _logsumexp(list(ll.values()))
            return [{k: math.exp(v - z) for k, v in ll.items()}], 0.0
        n = len(self.CATS)
        dens = [self._seg_logdens(y, R, preds) for y, R, single in segs]

        def cls_prior(single):
            mult = SINGLE_OUTLIER_MULT if single else 1.0
            pp, pe = P_PARTIAL * mult, P_EXTRA * mult
            return {"w": math.log(1.0 - pp - pe), "p": math.log(pp), "e": math.log(pe)}
        cp = [cls_prior(seg[2]) for seg in segs]
        two_p = TWO2_PRIOR if n >= 2 else 0.0
        same_p = SAME2_PRIOR
        if n >= 2 and _fnum(peak):
            ms = sorted(preds[c][0] for c in self.CATS)
            if peak >= ms[0] + ms[1] - TWO_PEAK_MARGIN:
                same_p *= TWO_PEAK_SAME_FACTOR     # the box saw both cats' weight at once during this visit
        tot = same_p + JUNK2_PRIOR + two_p
        l_same, l_junk = math.log(same_p / tot), math.log(JUNK2_PRIOR / tot)
        joint = {}
        for c in self.CATS:
            base = l_same - math.log(n) + math.log(0.5)
            for other, pk in (("p", SAME2_PARTIAL_SHARE), ("e", 1.0 - SAME2_PARTIAL_SHARE)):
                joint[(("w", c), (other, c))] = base + math.log(pk) + dens[0][("w", c)] + dens[1][(other, c)]
                joint[((other, c), ("w", c))] = base + math.log(pk) + dens[0][(other, c)] + dens[1][("w", c)]
        if two_p > 0:
            l_two = math.log(two_p / tot) - math.log(n * (n - 1))
            for a, b in itertools.permutations(self.CATS, 2):
                for k1 in ("w", "p", "e"):
                    for k2 in ("w", "p", "e"):
                        joint[((k1, a), (k2, b))] = (l_two + cp[0][k1] + cp[1][k2] + dens[0][(k1, a)]
                                                     + dens[1][(k2, b)])
        for c in self.CATS:
            for k in ("w", "p", "e"):
                base = l_junk + math.log(0.5) - math.log(n)
                joint[(("u", None), (k, c))] = base + cp[1][k] + dens[0][("u", None)] + dens[1][(k, c)]
                joint[((k, c), ("u", None))] = base + cp[0][k] + dens[0][(k, c)] + dens[1][("u", None)]
        z = _logsumexp(list(joint.values()))
        keys = list(dens[0].keys())
        m1 = {k: 0.0 for k in keys}
        m2 = {k: 0.0 for k in keys}
        p_same = 0.0
        one = {}
        for (k1, k2), v in joint.items():
            p = math.exp(v - z)
            m1[k1] += p
            m2[k2] += p
            if k1[1] is not None and k1[1] == k2[1]:
                p_same += p
            for j, (ka, kb) in enumerate(((k1, k2), (k2, k1))):
                if ka[0] == "w" and (kb[1] == ka[1] or kb[0] == "u"):
                    one[(ka[1], j)] = one.get((ka[1], j), 0.0) + p
        self._one_cat = one
        return [m1, m2], p_same

    def _visitor(self, post):
        v = {c: 0.0 for c in self.CATS}
        for (kind, c), p in post.items():
            if c is not None:
                v[c] += p
        return v

    def _blocked(self):
        b = set(c for c in self.CATS if self.tracks[c]["ident"] == "lost")
        for ov in self.overlap:
            b.update(ov["pair"])
        return b

    def _label_one(self, y, R, post, t, blocked):
        """(label, conf) for one usable segment from its class posterior."""
        v = self._visitor(post)
        best = max(self.CATS, key=lambda c: post.get(("w", c), 0.0))
        if best in blocked or post.get(("w", best), 0.0) < LABEL_MIN_POST or not self._gate_ok(best, y, R, t):
            return "unknown", None
        return best, min(CONF_CAP, int(100 * v[best]))

    def _gate_ok(self, c, y, R, t):
        """Innovation gate (GATE_Z) and seed-label expiry."""
        tb = self.tracks[c]
        if tb["ident"] == "init" and len(self.CATS) > 1 and t - tb["t"] > SEED_LABEL_DAYS * DAY:
            return False
        m, _, Pww, _, _ = self._pred(c, t, cap=PRED_CAP_SD)
        return abs(y - m) <= GATE_Z * math.sqrt(Pww + R)

    # ================================================================================================
    # visit bookkeeping helpers
    # ================================================================================================
    def _usable_segments(self, obs, t):
        """[(y, R, single, info)] per segment (max 2); y None = unusable. info = {"n", "upd"} where upd False means
        the segment may be labelled but never moves a track or becomes a sample."""
        segs = obs.get("segments") if isinstance(obs.get("segments"), list) else []
        segs = segs[:2]
        if obs.get("orphan") or obs.get("during_clean"):
            return [(None, None, False, None) for _ in segs]
        bu = obs.get("baseline_unc")
        bu = min(float(bu), 0.10) if _fnum(bu) and bu > 0 else 0.0
        extra = (bu / 2.0) ** 2 + (R_CLAMP_SD ** 2 if obs.get("clamped") else 0.0)
        alt_pending = obs.get("baseline_alt") is not None
        lo, hi = self.plausible_range(t)
        out = []
        for s in segs:
            w = s.get("w") if isinstance(s, dict) else None
            n = s.get("n") if isinstance(s, dict) else None
            if (not _fnum(w) or not W_MIN <= w <= W_MAX or not isinstance(n, int) or isinstance(n, bool) or n < 1
                    or not lo - 0.5 <= w <= hi + 0.5):
                out.append((None, None, False, None))
                continue
            info = {"n": n, "upd": not alt_pending}
            if n == 1:
                pd = s.get("peak_delta")
                if not _fnum(pd) or abs(pd) > SINGLE_AGREE_KG + 1e-9:
                    info["upd"] = False
                    out.append((float(w), R_SINGLE_SD ** 2 + R_DISAGREE_SD ** 2 + extra, True, info))
                    continue
                out.append((float(w), R_SINGLE_SD ** 2 + extra, True, info))
            else:
                out.append((float(w), R_VISIT_SD ** 2 + extra, False, info))
        return out

    def _decide(self, segs, t, obs=None):
        """Labels for one visit. Returns (labels per segment, confs per segment, keep index set (segments that
        count), kind) where kind is "two" (two different cats), "one" or "none"."""
        idx = [i for i, s in enumerate(segs) if s[0] is not None]
        labels = ["unknown"] * len(segs)
        confs = [None] * len(segs)
        if not idx:
            return labels, confs, set(), "none"
        blocked = self._blocked()
        posts, p_same = self._posteriors([segs[i][:3] for i in idx], t, (obs or {}).get("peak"))
        if len(idx) == 2:
            i1, i2 = idx
            l1, c1 = self._label_one(segs[i1][0], segs[i1][1], posts[0], t, blocked)
            l2, c2 = self._label_one(segs[i2][0], segs[i2][1], posts[1], t, blocked)
            if l1 in self.CATS and l2 in self.CATS and l1 != l2 and p_same < SAME2_MAX:
                labels[i1], confs[i1], labels[i2], confs[i2] = l1, c1, l2, c2
                return labels, confs, {i1, i2}, "two"
            if (l1 in self.CATS) != (l2 in self.CATS):
                # one plateau is label-grade, the other is not a label-grade cat: one visit of that cat
                keep, lab, cf = (i1, l1, c1) if l1 in self.CATS else (i2, l2, c2)
                labels[keep], confs[keep] = lab, cf
                return labels, confs, {keep}, "one"
            # ONE visit (the same cat perched then in, or one plateau junk): the cat and its whole plateau with
            # the highest joint probability carry the label; nothing label-grade -> unknown, larger plateau kept
            one = self._one_cat
            if one:
                (cat, j), pj = max(one.items(), key=lambda kv: kv[1])
                keep = idx[j]
                y, R = segs[keep][0], segs[keep][1]
                if pj >= LABEL_MIN_POST and cat not in blocked and self._gate_ok(cat, y, R, t):
                    labels[keep] = cat
                    confs[keep] = min(CONF_CAP, int(100 * pj))
                    return labels, confs, {keep}, "one"
            keep = max(idx, key=lambda j: segs[j][3]["n"])
            return labels, confs, {keep}, "one"
        i = idx[0]
        labels[i], confs[i] = self._label_one(segs[i][0], segs[i][1], posts[0], t, blocked)
        return labels, confs, {i}, "one"

    def _alt_check(self, obs, segs, labels, t, record):
        """A visit that opened while a baseline drop was pending (obs["baseline_alt"], kg, may be 0.0): label only
        if the alternative baseline gives the same label; never a sample (handled in _usable)."""
        pre, alt = obs.get("pre"), obs.get("baseline_alt")
        if alt is None or not _fnum(pre) or not _fnum(alt):
            return labels
        shift = pre - alt
        alt_segs = [(y + shift, R, single, info) if y is not None else (y, R, single, info)
                    for y, R, single, info in segs]
        alt_labels, _, _, _ = self._decide(alt_segs, t, obs)
        if alt_labels != labels:
            if record is not None:
                record["baseline_ambiguous"] = True
            return ["unknown"] * len(labels)
        return labels

    # ================================================================================================
    # overlap ("can't tell apart") bookkeeping
    # ================================================================================================
    def _pair_z(self, a, b, t):
        pa, pb = self._pred(a, t, cap=OVERLAP_CAP_SD), self._pred(b, t, cap=OVERLAP_CAP_SD)
        return abs(pa[0] - pb[0]) / math.sqrt(pa[2] + pb[2] + 2 * R_VISIT_SD ** 2)

    def _note_ambiguous(self, segs, t):
        for y, R, single, info in segs:
            if y is None:
                continue
            fit = []
            for c in self.CATS:
                if self.tracks[c]["ident"] != "ok":
                    continue
                m, _, Pww, _, _ = self._pred(c, t, cap=OVERLAP_CAP_SD)
                if abs(y - m) <= OVERLAP_AMBIG_Z * math.sqrt(Pww + R):
                    fit.append(c)
            if len(fit) >= 2:
                self.ambig.append([t, fit])

    def _update_overlap(self, t):
        in_pair = set(c for ov in self.overlap for c in ov["pair"])
        ok = [c for c in self.CATS if self.tracks[c]["ident"] == "ok" and c not in in_pair]
        for a, b in itertools.combinations(sorted(ok), 2):
            if a in in_pair or b in in_pair:
                continue
            if self._pair_z(a, b, t) >= OVERLAP_ENTER_Z:
                continue
            ev = sum(1 for at, fit in self.ambig if t - at <= OVERLAP_AMBIG_HOURS * 3600 and a in fit and b in fit)
            if ev < OVERLAP_MIN_AMBIG:
                continue
            order = sorted((a, b), key=lambda c: self.tracks[c]["w"])
            self.overlap.append({"pair": [a, b], "since": t, "sep_since": None, "order": order, "wi": None})
            in_pair.update((a, b))
            for c in (a, b):
                self.tracks[c]["doubt"] = True
                self.tracks[c]["cusum"] = 0.0
            log("cats_overlap_entered", pair=[a, b], z=round(self._pair_z(a, b, t), 2))

    # ================================================================================================
    # label-free net
    # ================================================================================================
    def _lf_points(self, t):
        pts = [(r[0], r[1]) for r in self.recent if t - LF_WINDOW_DAYS * DAY <= r[0] <= t]
        return pts[-LF_MAX_POINTS:]

    def _lf_fit(self, t_ref, pts, K=None):
        """K-component (default N) mixture with linear drift + uniform outlier. Returns components or None."""
        K = K or len(self.CATS)
        if len(pts) < max(3, K * 2):
            return None
        taus = [(t - t_ref) / DAY for t, _ in pts]
        ys = [y for _, y in pts]
        inits = [sorted([list(self._pred(c, t_ref)[:2]) for c in self.CATS])] if K == len(self.CATS) else []
        lo_y, hi_y = min(ys), max(ys)
        step = max(0.005, (hi_y - lo_y) / 600.0)
        grid = [lo_y + step * i for i in range(int((hi_y - lo_y) / step) + 1)]
        dens = [sum(math.exp(-0.5 * ((x - y) / LF_SD) ** 2) for y in ys) for x in grid]
        modes = sorted((dens[i], grid[i]) for i in range(len(grid))
                       if (i == 0 or dens[i] >= dens[i - 1]) and (i == len(grid) - 1 or dens[i] > dens[i + 1]))
        top = sorted(x for _, x in modes[::-1][:K])
        if len(top) == K:
            inits.append([[x, 0.0] for x in top])
        if not inits:
            srt = sorted(ys)
            inits.append([[srt[min(len(srt) - 1, int((k + 0.5) / K * len(srt)))], 0.0] for k in range(K)])
        var = LF_SD ** 2
        lout = math.log(LF_OUT_W / UNIFORM_WIDTH)
        best = None
        for init in inits:
            comps = [list(ab) for ab in init]
            pis = [(1 - LF_OUT_W) / K] * K
            prev = None
            resp, ll = [], -math.inf
            for _ in range(LF_ITERS):
                resp, ll = [], 0.0
                for tau, y in zip(taus, ys):
                    lv = [math.log(max(pis[k], 1e-9)) + _norm_logpdf(y, comps[k][0] + comps[k][1] * tau, var)
                          for k in range(K)] + [lout]
                    z = _logsumexp(lv)
                    ll += z
                    resp.append([math.exp(v - z) for v in lv[:K]])
                for k in range(K):
                    sw = sum(r[k] for r in resp)
                    if sw < 1e-6:
                        continue
                    s_t = sum(r[k] * tau for r, tau in zip(resp, taus))
                    s_tt = sum(r[k] * tau * tau for r, tau in zip(resp, taus)) + var / LF_SLOPE_PRIOR_SD ** 2
                    s_y = sum(r[k] * y for r, y in zip(resp, ys))
                    s_ty = sum(r[k] * tau * y for r, tau, y in zip(resp, taus, ys))
                    det = sw * s_tt - s_t * s_t
                    if det > 0:
                        comps[k] = [(s_tt * s_y - s_t * s_ty) / det, (sw * s_ty - s_t * s_y) / det]
                tot = sum(sum(r) for r in resp)
                if tot > 0:
                    pis = [(1 - LF_OUT_W) * max(1e-3, sum(r[k] for r in resp) / tot) for k in range(K)]
                cur = [round(v, 7) for ab in comps for v in ab]
                if cur == prev:
                    break
                prev = cur
            if best is None or ll > best[0] + 1e-9:
                best = (ll, comps, resp)
        _, comps, resp = best
        out = []
        for k in range(K):
            sw = sum(r[k] for r in resp)
            if sw < 1e-6:
                out.append({"a": comps[k][0], "b": comps[k][1], "var_a": 1.0, "n": 0.0, "sd": 1.0,
                            "tbar": t_ref, "a_bar": comps[k][0], "var_bar": 1.0})
                continue
            s_t = sum(r[k] * tau for r, tau in zip(resp, taus))
            s_tt = sum(r[k] * tau * tau for r, tau in zip(resp, taus)) + var / LF_SLOPE_PRIOR_SD ** 2
            det = sw * s_tt - s_t * s_t
            var_a = var * s_tt / det if det > 0 else var / sw
            res = sum(r[k] * (y - comps[k][0] - comps[k][1] * tau) ** 2 for r, tau, y in zip(resp, taus, ys))
            tbar = s_t / sw
            out.append({"a": comps[k][0], "b": comps[k][1], "var_a": var_a, "n": sw, "sd": math.sqrt(res / sw),
                        "tbar": t_ref + tbar * DAY, "a_bar": comps[k][0] + comps[k][1] * tbar, "var_bar": var / sw})
        return out

    def _support(self, c, pts, t, perched_counts=False):
        """Points of the last REPAIR_SUPPORT_HOURS within 2 sd of track c's own line and nearer to it than to any
        other track (a track with recent support is not stuck, whatever a mixture fit says)."""
        n = 0
        for tp, y in pts:
            if t - tp > REPAIR_SUPPORT_HOURS * 3600:
                continue
            best, dist, perched = None, None, False
            for b in self.CATS:
                tr = self.tracks[b]
                dt = (tp - tr["t"]) / DAY
                m = tr["w"] + tr["g"] * dt
                z = abs(y - m) / math.sqrt(tr["Pww"] + Q_W * abs(dt) + R_VISIT_SD ** 2)
                if dist is None or z < dist:
                    best, dist = b, z
                if b != c and m - PARTIAL_RANGE[1] <= y <= m - PARTIAL_RANGE[0]:
                    perched = True      # as well explained as another cat perched on the rim: no support
            n += best == c and dist <= 2.0 and (perched_counts or not perched)
        return n

    def _lf_check(self, t, force=False):
        """Run the label-free net at time t. Returns a short dict (diagnostics) or None."""
        if not force and self.status(t) == "ok" and t - self.lf_last < LF_EVERY_HOURS * 3600:
            return None
        self.lf_last = t
        pts = self._lf_points(t)
        boot = all(self.tracks[c]["ident"] == "init" for c in self.CATS)
        min_n = LF_BOOT_MIN_N if boot else LF_MIN_N

        def valid_of(cs):
            v = [k for k, cp in enumerate(cs) if cp["n"] >= min_n and cp["sd"] <= LF_MAX_SD]
            for k1, k2 in itertools.combinations(list(v), 2):
                if abs(cs[k1]["a"] - cs[k2]["a"]) < LF_MIN_SEP:
                    v = [k for k in v if k not in (k1, k2)]
            return v
        comps = self._lf_fit(t, pts)
        if not comps:
            for ov in self.overlap:
                ov["sep_since"] = None
            return None
        valid = valid_of(comps)
        # an N-component fit merges two clusters when there are N+1 (a cat that changed, next to its old place
        # and another cat): a track with no N-cluster near it gets one more look with N+1 components
        extra = []
        if valid and len(self.CATS) > 1:
            need = [self.tracks[c]["w"] for c in self.CATS if self.tracks[c]["ident"] == "ok"
                    and min(abs(self.tracks[c]["w"] - comps[k]["a"]) for k in valid) > SUPPORT_GATE_KG]
            c3 = self._lf_fit(t, pts, K=len(self.CATS) + 1) if need else None
            if c3:
                extra = [c3[k] for k in valid_of(c3)
                         if any(abs(c3[k]["a"] - w) <= SUPPORT_GATE_KG for w in need)]
        if valid:
            self.cluster = {"t": t, "comps": sorted([[round(comps[k]["a"], 3), round(comps[k]["n"], 1)]
                                                     for k in valid] +
                                                    [[round(cp["a"], 3), round(cp["n"], 1)] for cp in extra])}
            if len(self.cluster["comps"]) == 1:
                self.cluster["sup"] = {c: self._support(c, pts, t, perched_counts=True) for c in self.CATS}
        full = len(valid) >= len(self.CATS)
        acted = []
        # 1) overlaps: judged ONLY on data since the overlap began (the window would otherwise still show the
        # separation from before it). Separated for long enough -> resume (who-stayed-put, order kept) or
        # needs_weigh_in.
        for ov in list(self.overlap):
            pts_ov = [q for q in pts if q[0] >= ov["since"]]
            c_ov = self._lf_fit(t, pts_ov) if len(pts_ov) >= 6 else None
            v_ov = valid_of(c_ov) if c_ov else []
            # each must be a real share of the visits, not a smear of perched / contaminated readings
            v_ov = [k for k in v_ov if c_ov[k]["n"] >= LF_EXIT_MIN_SHARE * len(pts_ov) and c_ov[k]["sd"] <= LF_EXIT_MAX_SD]
            if len(v_ov) < 2:
                ov["sep_since"] = None
                continue
            if ov["sep_since"] is None:
                ov["sep_since"] = t
            if t - ov["sep_since"] < OVERLAP_EXIT_HOURS * 3600:
                continue
            a, b = ov["pair"]
            p_pair, kept, mapping = self._anchored([a, b], v_ov, c_ov, t, before=ov["since"])
            kept = kept and sorted((a, b), key=lambda c: c_ov[mapping[c]]["a"]) == ov["order"]
            self.overlap.remove(ov)
            if p_pair >= LF_ASSIGN_MIN and kept:
                for c in (a, b):
                    self._reacquire(c, c_ov[mapping[c]], t, "resume")
                    acted.append([c, "resume"])
            else:
                for c in (a, b):
                    self._mark_lost(c, ov["since"])
                log("cats_overlap_order_unknown", pair=[a, b], p=round(p_pair, 4), order_kept=kept)
                acted.append([a + "+" + b, "needs_weigh_in"])
        if not full:
            return {"valid": len(valid), "acted": acted}
        valid = sorted(valid, key=lambda k: comps[k]["a"])
        if all(self.tracks[c]["ident"] == "init" for c in self.CATS):
            # fresh install: a rough seed carries no usable distance information, only its ORDER (the owner
            # configured it). Matched in seed order; identity stays "in doubt" until a weigh-in / correction.
            perm, p = tuple(sorted(self.CATS, key=lambda c: self.seeds[c])), 1.0
            for k, c in zip(valid, perm):
                cp = comps[k]
                self._anchor(c, t, cp["a"], cp["b"], 2.0 * cp["var_a"], max(SEED_RATE_SD ** 2 / 4, LF_SLOPE_PRIOR_SD ** 2 / max(1.0, cp["n"])))
                tr = self.tracks[c]
                tr.update(ident="ok", doubt=len(self.CATS) > 1, n_eff=max(tr["n_eff"], cp["n"]), since=t)
                log("cats_label_free_bootstrap", cat=c, w=round(cp["a"], 3))
            return {"valid": len(valid), "acted": "bootstrap"}
        preds = {c: self._pred(c, t) for c in self.CATS}
        scores = []
        for comb in itertools.permutations(valid, len(self.CATS)):      # cat i -> component comb[i]
            s = 0.0
            for c, k in zip(self.CATS, comb):
                v = preds[c][2] + comps[k]["var_a"] + LF_ASSIGN_EXTRA_SD ** 2
                s += -0.5 * (math.log(v) + (comps[k]["a"] - preds[c][0]) ** 2 / v)
            scores.append((s, comb))
        z = _logsumexp([s for s, _ in scores])
        sbest, comb = max(scores, key=lambda x: x[0])
        p = math.exp(sbest - z)
        assign = dict(zip(self.CATS, comb))
        lost = [c for c in self.CATS if self.tracks[c]["ident"] == "lost"]
        if len(lost) >= 2:
            # several lost together: judged on data since they were lost only, and only who-stayed-put evidence
            # (every cat back at its place) with the order kept re-acquires them
            since = max(self.tracks[x].get("lost_since") or -math.inf for x in lost)
            pts_l = [q for q in pts if q[0] >= since]
            c_l = self._lf_fit(t, pts_l) if len(pts_l) >= 6 else None
            v_l = [k for k in (valid_of(c_l) if c_l else [])
                   if c_l[k]["n"] >= LF_EXIT_MIN_SHARE * len(pts_l) and c_l[k]["sd"] <= LF_EXIT_MAX_SD]
            if len(v_l) >= len(lost):
                pl, kept, mapping = self._anchored(lost, v_l, c_l, t, before=since)
                if pl >= LF_ASSIGN_MIN and kept:
                    for x in lost:
                        self._reacquire(x, c_l[mapping[x]], t, "reacquire")
                        acted.append([x, "reacquire"])
            lost = [c for c in self.CATS if self.tracks[c]["ident"] == "lost"]
        if p < LF_ASSIGN_MIN:
            return {"valid": len(valid), "p": round(p, 4), "acted": acted}
        for c in self.CATS:
            tr = self.tracks[c]
            cp = comps[assign[c]]
            if any(c in ov["pair"] for ov in self.overlap):
                continue
            if tr["ident"] == "lost":
                if c not in lost:
                    continue            # suspended in this very check: judged on the next one
                if len(lost) == 1:
                    # one lost cat, every other identity in place: the confident assignment is enough
                    self._reacquire(c, cp, t, "reacquire")
                    acted.append([c, "reacquire"])
                continue
            learning = tr["n_eff"] < LEARN_MIN_EFF or tr["ident"] == "init"
            dtb = (cp["tbar"] - tr["t"]) / DAY
            m_bar = tr["w"] + tr["g"] * dtb
            v_bar = tr["Pww"] + 2 * dtb * tr["Pwg"] + dtb * dtb * tr["Pgg"] + Q_W * abs(dtb)
            zc = abs(cp["a_bar"] - m_bar) / math.sqrt(max(v_bar, 1e-8) + cp["var_bar"])
            recent_wi = self._wi_protects(c, t)
            held = any(abs(cp3["a"] - tr["w"]) <= SUPPORT_GATE_KG for cp3 in extra)
            if learning or (zc > REPAIR_Z and not recent_wi and not held
                            and self._support(c, pts, t) < REPAIR_MAX_SUPPORT):
                why = "bootstrap" if learning else "repair"
                if why == "repair":
                    # a repair may move a track, never re-order identities: if the new place would change which
                    # side of another cat it is on, both identities are suspended instead (who-stayed-put decides)
                    crossed = [b for b in self.CATS if b != c and self.tracks[b]["ident"] == "ok"
                               and (self.tracks[b]["w"] - tr["w"]) * (self.tracks[b]["w"] - cp["a"]) < 0]
                    if crossed or any(a[1] == "repair" for a in acted):
                        for x in [c] + crossed:
                            self._mark_lost(x, t)
                        log("cats_label_free_repair_refused", cat=c, crossed=crossed)
                        acted.append([c, "suspend"])
                        continue
                self._reacquire(c, cp, t, why)
                if why == "repair":
                    self.repairs_total += 1
                acted.append([c, why])
        return {"valid": len(valid), "p": round(p, 4), "acted": acted}

    def _mark_lost(self, c, since):
        tr = self.tracks[c]
        if tr["ident"] != "lost":
            tr["lost_since"] = since
        tr.update(ident="lost", doubt=True, cusum=0.0)

    def _home(self, c, before):
        """Where the cat lived before an episode: median of its labelled samples in the HOME_DAYS before
        `before` (>= 3 of them), else None."""
        if before is None or not math.isfinite(before):
            return None
        ys = [x[1] for x in self.samples[c] if before - HOME_DAYS * DAY <= x[0] <= before]
        return _median(ys) if len(ys) >= 3 else None

    def _anchored(self, group, ks, comps, t, before=None):
        """Which of components `ks` belongs to which cat of `group` (tracks frozen / lost since their last update)?
        Each pairing scores exp(-z^2/2) + ANCHOR_EPS exp(-z^2/(2 ANCHOR_WIDE^2)), z against the cat's place: its
        frozen track, or its home before the episode (`before`), whichever fits. Variance growth is capped at one
        day. The mapping is accepted only if EVERY cat is within ANCHOR_STAY_Z of its place: if
        any cat moved, which is which is not in the data. Returns (posterior, order kept?, mapping)."""
        places = {}
        for c in group:
            tr = self.tracks[c]
            dt = max(0.0, (t - tr["t"]) / DAY)
            ps = [(tr["w"], tr["P_upd"] + Q_W * min(dt, 1.0))]
            h = self._home(c, before)
            if h is not None:
                ps.append((h, R_VISIT_SD ** 2 / 3 + Q_W))
            places[c] = ps

        def z2(c, k):
            return min((comps[k]["a"] - w) ** 2 / (v + comps[k]["var_a"] + LF_ASSIGN_EXTRA_SD ** 2)
                       for w, v in places[c])
        best, tot, best_map = -1.0, 0.0, None
        for perm in itertools.permutations(ks, len(group)):
            lik = 1.0
            for c, k in zip(group, perm):
                tr = self.tracks[c]
                dt = max(0.0, (t - tr["t"]) / DAY)
                if min(abs(comps[k]["a"] - w) for w, _ in places[c]) > ANCHOR_MAX_KG_PER_DAY * max(dt, 1.0) + 0.05:
                    lik = 0.0       # no cat changes that fast: this pairing is impossible, not just unlikely
                    break
                q = min(z2(c, k), 1400.0)
                lik *= math.exp(-0.5 * q) + ANCHOR_EPS * math.exp(-0.5 * q / ANCHOR_WIDE ** 2)
            tot += lik
            if lik > best:
                best, best_map = lik, dict(zip(group, perm))
        if best_map is None or tot <= 0:
            return 0.0, False, dict(zip(group, ks))
        if not all(z2(c, k) <= ANCHOR_STAY_Z ** 2 for c, k in best_map.items()):
            return 0.0, False, best_map
        p = best / tot
        kept = (sorted(group, key=lambda c: comps[best_map[c]]["a"]) == sorted(group, key=lambda c: self.tracks[c]["w"]))
        return p, kept, best_map

    def _reacquire(self, c, cp, t, why):
        self._anchor(c, t, cp["a"], cp["b"], 2.0 * cp["var_a"],
                     max(SEED_RATE_SD ** 2 / 4, LF_SLOPE_PRIOR_SD ** 2 / max(1.0, cp["n"])))
        tr = self.tracks[c]
        was_ok = tr["ident"] == "ok"
        tr.update(ident="ok", n_eff=max(tr["n_eff"], cp["n"]), sudden=None)
        if why in ("resume", "reacquire"):
            tr["doubt"] = False             # identity re-established by continuity at >= LF_ASSIGN_MIN
        if why == "bootstrap" or tr.get("since") is None:
            tr["since"] = t                 # a resume / re-acquire keeps the trend history
        log("cats_label_free_" + why, cat=c, w=round(cp["a"], 3))

    # ================================================================================================
    # buffers / pruning
    # ================================================================================================
    def _prune(self, now):
        cut_s = now - SAMPLES_KEEP_DAYS * DAY
        for c in self.CATS:
            self.samples[c] = [s for s in self.samples[c] if s[0] >= cut_s][-SAMPLES_MAX:]
        self.recent = [r for r in self.recent if r[0] >= now - max(LF_WINDOW_DAYS, 1.0) * DAY][-RECENT_MAX:]
        self.ambig = [a for a in self.ambig if a[0] >= now - OVERLAP_AMBIG_HOURS * 3600][-RECENT_MAX:]
        self.visits = [v for v in self.visits if v[0] >= now - VISITS_KEEP_DAYS * DAY][-5000:]
        self.weighins = self.weighins[-WEIGHINS_MAX:]
        self.swap_votes = [v for v in self.swap_votes if v[0] >= now - SWAP_VOTE_HOURS * 3600]
        self.box_ids = [v for v in self.box_ids if v[0] >= now - BOXID_SESSION_HOURS * 3600][-20:]
        self.scale_events = [v for v in self.scale_events
                             if v[0] >= now - max(SCALE_SHIFT_SHOW_HOURS, COMMON_HOURS) * 3600][-20:]
        for c in self.CATS:
            self.innov[c] = [v for v in self.innov[c] if v[0] >= now - COMMON_HOURS * 3600][-200:]
        self.above = [a for a in self.above if a[0] >= now - DAY][-500:]

    # ================================================================================================
    # public: pipeline window
    # ================================================================================================
    def plausible_range(self, now=None):
        now = time.time() if now is None else float(now)
        los, his, ms = [], [], []
        for c in self.CATS:
            w, _, Pww, _, _ = self._pred(c, now)
            margin = min(PLAUSIBLE_MAX_MARGIN, max(PLAUSIBLE_MARGIN, PLAUSIBLE_Z * math.sqrt(Pww + R_VISIT_SD ** 2)))
            if self.tracks[c]["ident"] == "init":
                margin = max(margin, SEED_RANGE_KG)
            los.append(w - margin)
            his.append(w + margin)
            ms.append(w)
        lo, hi = max(W_MIN, min(los)), min(W_MAX, max(his))
        if len(ms) >= 2:
            two = sum(sorted(ms)[:2]) - PLAUSIBLE_MARGIN        # two cats at once are never "one cat"
            hi = max(min(hi, two), max(ms) + 0.15)
        if self.status(now) != "ok":
            # not ok: a cat may have outgrown its track; never let the window hide it
            hi = min(W_MAX, max(hi, max(ms) + STATUS_WIDEN_KG))
        return round(lo, 3), round(hi, 3)

    # ================================================================================================
    # public: one finalized visit
    # ================================================================================================
    def on_visit(self, obs, record, now=None):
        now = time.time() if now is None else float(now)
        obs = obs if isinstance(obs, dict) else {}
        t = obs.get("t")
        t = float(t) if _fnum(t) and T_MIN <= t <= T_MAX else now
        self._prune(now)
        snap = self._snapshot()
        segs = self._usable_segments(obs, t)
        aw = obs.get("above_window")
        if isinstance(aw, int) and not isinstance(aw, bool) and 0 < aw < 10000:
            self.above.append([t, aw])          # readings the pipeline dropped above plausible_range (diagnostic)
        lf = self._lf_check(t)
        self._note_ambiguous(segs, t)
        self._update_overlap(t)
        labels, confs, keep, kind = self._decide(segs, t, obs)
        labels = self._alt_check(obs, segs, labels, t, record)
        if any(l == "unknown" for l in labels) and kind == "two":
            kind = "one"
        ok_before = {c: self.tracks[c]["ident"] == "ok" for c in self.CATS}
        self._apply(segs, labels, keep, t, ok_before)
        for i, (y, R, single, info) in enumerate(segs):
            if y is not None and info and info["upd"] and (kind != "one" or i in keep or labels[i] == "unknown"):
                self.recent.append([t, y, R])
        self._update_overlap(t)
        if "newest" in obs:
            is_last = bool(obs["newest"])
        else:
            is_last = self.last is None or t >= self.last["t"]
        if is_last:
            self.last = {"t": t, "segs": [[y, R, single, info] for y, R, single, info in segs], "snap": snap,
                         "labels": labels, "confs": confs, "keep": sorted(keep), "late_after": False,
                         "ts": record.get("ts") if isinstance(record, dict) and isinstance(record.get("ts"), str) else None}
        elif self.last is not None:
            self.last["late_after"] = True
        self._book(t, segs, labels, confs, keep, record, is_last, corrected=False)
        if lf and lf.get("acted"):
            log("cats_label_free", **{k: v for k, v in lf.items() if k != "acted"}, acted=lf["acted"])
        return self.fields(now)

    def _apply(self, segs, labels, keep, t, ok_before, forced=False):
        """Track updates (label-grade only), CUSUM, trend samples."""
        for i, ((y, R, single, info), lab) in enumerate(zip(segs, labels)):
            if y is None or lab not in self.CATS or i not in keep or not (info and info.get("upd")):
                continue
            tr = self.tracks[lab]
            if tr["ident"] == "init" and len(self.CATS) > 1 and not forced:
                continue        # with several cats a seed track is set by the label-free bootstrap only
            if tr["ident"] == "lost" and not forced:
                continue
            nu, S = self._kf(lab, t, y, R)
            tr["n_eff"] += 1.0
            if tr["ident"] == "init" and tr["n_eff"] >= LEARN_MIN_EFF:
                tr["ident"], tr["since"] = "ok", t
            if ok_before.get(lab):
                self.samples[lab].append([t, y, 1 if single else 2])
            if not forced:
                self.innov[lab].append([t, nu / math.sqrt(S), nu])
                self._cusum(lab, nu, S, t)

    def _cusum(self, c, nu, S, t):
        """One-sided CUSUM of standardized innovations towards the nearest other cat."""
        others = [b for b in self.CATS if b != c]
        if not others:
            return
        m = self.tracks[c]["w"]
        nearest = min(others, key=lambda b: abs(self._pred(b, t)[0] - m))
        sgn = 1.0 if self._pred(nearest, t)[0] > m else -1.0
        tr = self.tracks[c]
        tr["cusum"] = max(0.0, tr["cusum"] + sgn * nu / math.sqrt(S) - CUSUM_K)
        if tr["cusum"] > CUSUM_H:
            # Segment weights are already relative to each visit's own empty-box baseline. A shift that moved the
            # other cat's readings the same way too (litter top-up, scale drift) is the scale, not a cat: logged.
            win = t - COMMON_HOURS * 3600
            mine = [x for x in self.innov[c] if x[0] >= win]
            shift = sum(x[2] for x in mine) / len(mine) if mine else 0.0
            theirs = [x for x in self.innov[nearest] if x[0] >= win]
            common = False
            if (abs(shift) >= COMMON_MIN_SHIFT_KG
                    and not any(e[1] == c and e[0] >= win for e in self.scale_events)):   # once per window
                # (a) the other cat's labelled innovations moved the same way, by >= half as much, significantly
                if len(theirs) >= COMMON_MIN_N:
                    zbar = sum(x[1] for x in theirs) / len(theirs)
                    raw = sum(x[2] for x in theirs) / len(theirs)
                    common = raw / shift >= COMMON_SHARE and abs(zbar) * math.sqrt(len(theirs)) >= COMMON_Z \
                        and zbar * shift > 0
                # (b) the other cat's readings (labelled or not; they may now fall outside its gate) sit at its
                # track + the same shift, and clearly more of them than at its old place
                if not common:
                    mb = self._pred(nearest, t)[0]
                    half = max(0.015, abs(shift) / 2)
                    ys = [y for at, y, _ in self.recent if at >= win]
                    near_shift = sum(1 for y in ys if abs(y - mb - shift) < half)
                    near_orig = sum(1 for y in ys if abs(y - mb) < half)
                    common = near_shift >= COMMON_MIN_NEAR and near_shift >= 2 * near_orig
            if common:
                tr["cusum"] = 0.0
                self.scale_events.append([t, c, _grams(shift)])
                log("cats_scale_shift", cat=c, other=nearest, shift_g=_grams(shift))
                return
            # one-sided drift towards another cat: either this cat changed suddenly (a health signal), or the other
            # cat moved into this one's readings. Both identities are suspended (no names on the label-free
            # clusters) and flagged "sudden_change" until the label-free net or a weigh-in session settles them.
            for x in (c, nearest):
                self._mark_lost(x, t)
                self.tracks[x]["sudden"] = t
            log("cats_cusum_alarm", cat=c, towards=nearest)

    def _book(self, t, segs, labels, confs, keep, record, is_last, corrected):
        named = [labels[i] for i in sorted(keep) if labels[i] in self.CATS]
        distinct = sorted(set(named))
        if len(distinct) == 2:
            cat, conf = "two_cats", None
        elif len(distinct) == 1:
            cat = distinct[0]
            conf = max(cf for l, cf in zip(labels, confs) if l == cat and cf is not None) \
                if any(l == cat and cf is not None for l, cf in zip(labels, confs)) else None
        else:
            cat, conf = "unknown", None
        weighed = sum(1 for i, s in enumerate(segs) if s[0] is not None and (i in keep or len(distinct) < 2))
        weighed = 2 if cat == "two_cats" else min(1, weighed)
        if not corrected:
            for c in distinct:
                self.visits_total[c] += 1
            if cat == "two_cats":
                self.two_cat_visits_total += 1
            elif cat == "unknown":
                self.unknown_visits_total += 1
            self.visits.append([t, distinct[:] if distinct else ["unknown"], weighed])
        for c in distinct:
            tr = self.tracks[c]
            tr["last_seen"] = max(tr["last_seen"] or 0.0, t)
        if record is not None:
            record["cat"] = cat
            record["conf"] = conf
            if cat == "two_cats":
                record["cats"] = [labels[i] for i in sorted(keep)]
                record["confs"] = [confs[i] for i in sorted(keep)]
            else:
                record.pop("cats", None)
                record.pop("confs", None)
            if len(segs) == 2 and cat != "two_cats" and len(keep) == 1:
                # ONE visit on the larger plateau: the row keeps one weight
                k = next(iter(keep))
                record["segments_merged"] = True
                record.pop("weights", None)
                if segs[k][0] is not None:
                    record["weight"] = segs[k][0]
                    record["n"] = segs[k][3]["n"]
                    record["kind"] = "single" if segs[k][3]["n"] < 2 else "plateau"
        if not is_last:
            return
        if cat == "two_cats":
            text = " + ".join(display_name(labels[i]) for i in sorted(keep))
        elif cat == "unknown":
            text = "Unknown"
        else:
            text = "%s %d%%" % (display_name(cat), conf) if conf is not None else display_name(cat)
        self.last_fields = {"last_cat": cat, "last_conf": conf, "last_visit_cat": text}
        if self.last is not None:
            self.last.update(cat=cat, conf=conf, labels=labels, confs=confs, keep=sorted(keep))
        if record is not None:
            self._last_record = record

    # ================================================================================================
    # public: owner commands
    # ================================================================================================
    def _snapshot(self):
        return {"tracks": json.loads(json.dumps(self.tracks)), "overlap": json.loads(json.dumps(self.overlap)),
                "n_samples": {c: len(self.samples[c]) for c in self.CATS}, "repairs_total": self.repairs_total}

    def _restore(self, snap, t):
        self.tracks = json.loads(json.dumps(snap["tracks"]))
        self.overlap = json.loads(json.dumps(snap["overlap"]))
        for c in self.CATS:
            n = snap["n_samples"].get(c, len(self.samples[c]))
            self.samples[c] = self.samples[c][:n] + [s for s in self.samples[c][n:] if s[0] != t]
        self.repairs_total = snap.get("repairs_total", self.repairs_total)

    def _swap(self, a, b, why):
        self.tracks[a], self.tracks[b] = self.tracks[b], self.tracks[a]
        for c in (a, b):
            self.tracks[c]["cusum"] = 0.0
        self.samples[a], self.samples[b] = self.samples[b], self.samples[a]
        log("cats_identity_swapped", cats=[a, b], why=why)

    def correct_last(self, cat, now=None):
        """Owner correction of the newest visit: `cat` or "unknown". The visit's track updates are undone and
        redone as owner-labelled; counters never decrease. A corrected reading that fits ANOTHER cat's track is
        identity evidence: it swaps the two tracks when identity is in doubt, or on a second such correction
        within SWAP_VOTE_HOURS."""
        now = time.time() if now is None else float(now)
        if self.last is None or (cat not in self.CATS and cat != "unknown"):
            return None
        L = self.last
        t = L["t"]
        old_labels = list(L.get("labels") or [])
        if not L.get("late_after") and isinstance(L.get("snap"), dict):
            self._restore(L["snap"], t)
        segs = []
        for x in L.get("segs") or []:
            y, R, single, info = (list(x) + [None] * 4)[:4]
            segs.append((y, R, bool(single), info if isinstance(info, dict) else ({"n": 2, "upd": True} if y is not None else None)))
        if not segs:
            segs, old_labels = [(None, None, False, None)], ["unknown"]
        usable = [i for i, s in enumerate(segs) if s[0] is not None]
        labels = ["unknown"] * len(segs)
        keep = set()
        if cat in self.CATS and usable:
            m = self._pred(cat, t, cap=PRED_CAP_SD)
            i_best = min(usable, key=lambda i: abs(segs[i][0] - m[0])) if len(usable) > 1 else usable[0]
            for i in usable:
                prev = old_labels[i] if i < len(old_labels) else "unknown"
                labels[i] = cat if i == i_best else (prev if prev in self.CATS and prev != cat else "unknown")
            keep = {i for i in usable if labels[i] in self.CATS}
            y, R = segs[i_best][0], segs[i_best][1]
            if cat in self._blocked() or self.tracks[cat]["ident"] == "init":
                # identity not settled: the correction relabels the visit and its counts, but it moves no track
                # and settles no identity (only a weigh-in session does)
                keep_upd = set()
            else:
                self._identity_evidence(cat, y, R, t, "correct_last")
                keep_upd = keep
        else:
            keep_upd = keep
        confs = [100 if l in self.CATS else None for l in labels]
        self._apply(segs, labels, keep_upd, t, {c: True for c in self.CATS}, forced=True)
        if L.get("late_after"):
            for c in self.CATS:
                if not any(labels[i] == c for i in keep):
                    self.samples[c] = [s for s in self.samples[c] if s[0] != t]
        self.correction_count += 1
        old_named = set(l for l in old_labels if l in self.CATS)
        new_named = set(labels[i] for i in keep if labels[i] in self.CATS)
        for c in new_named - old_named:
            self.visits_total[c] += 1
        if not new_named and old_named:
            self.unknown_visits_total += 1
        for v in reversed(self.visits):
            if abs(v[0] - t) < 1e-6:
                v[1] = sorted(new_named) or ["unknown"]
                break
        self._book(t, segs, labels, confs, keep, self._last_record, True, corrected=True)
        log("cats_correct_last", cat=cat, labels=labels)
        self.box_ids = [b for b in self.box_ids if abs(b[0] - t) > 1e-6]
        info_b = segs[i_best][3] if cat in self.CATS and usable else None
        if (cat in self.CATS and usable and len(self.CATS) > 1 and isinstance(info_b, dict) and info_b.get("upd")
                and (info_b.get("n") or 0) >= 2):
            # only a plateau-grade reading (>= 2 samples, admissible) can anchor identity
            self.box_ids.append([t, cat, round(y, 4)])
            self._settle_box_session(t)
        return self.fields(now)

    def _settle_box_session(self, t):
        """In-box identity anchor: the owner put each unsettled cat in the box and named the visit (correct_last),
        all within BOXID_SESSION_HOURS, on readings >= BOXID_MIN_SEP apart. Box readings only (never mixed with
        kitchen weigh-ins), so no box/kitchen offset enters the comparison."""
        group = [c for c in self.CATS if self.tracks[c]["ident"] in ("lost", "init") or self.tracks[c]["doubt"]
                 or any(c in ov["pair"] for ov in self.overlap)]
        latest = {}
        for at, c, kg in sorted(self.box_ids):
            if c in group and 0 <= t - at <= BOXID_SESSION_HOURS * 3600:
                latest[c] = (at, kg)
        if not group or set(latest) != set(group):
            return False
        vals = [latest[c][1] for c in group]
        settled_ms = [self._pred(c, t)[0] for c in self.CATS if c not in group]
        if any(abs(a - b) < BOXID_MIN_SEP for a, b in itertools.combinations(vals + settled_ms, 2)):
            log("cats_box_session_too_close", cats=group, kg=vals)
            return False
        cl = self.cluster      # the weigh-in session's check: each reading on a distinct fresh cluster
        if cl is not None and 0 <= t - cl["t"] <= SUPPORT_MAX_AGE_HOURS * 3600 and len(cl["comps"]) >= len(group):
            used = set()
            for c in group:
                k = min(range(len(cl["comps"])), key=lambda i: abs(cl["comps"][i][0] - latest[c][1]))
                if k in used or abs(cl["comps"][k][0] - latest[c][1]) > SUPPORT_GATE_KG:
                    log("cats_box_session_unmatched", cats=group, kg=vals)
                    return False
                used.add(k)
        self.overlap = [ov for ov in self.overlap if not set(ov["pair"]) & set(group)]
        for c in group:
            at, kg = latest[c]
            self._anchor(c, at, kg, 0.0, R_VISIT_SD ** 2, SEED_RATE_SD ** 2)     # rate re-learned from here
            tr = self.tracks[c]
            tr.update(ident="ok", doubt=False, last_seen=max(tr["last_seen"] or -math.inf, at),
                      n_eff=max(tr["n_eff"], LEARN_MIN_EFF), since=t, lost_since=None, sudden=None)
        self.swap_votes = [v for v in self.swap_votes if not ({v[1], v[2]} & set(group))]
        log("cats_box_session", cats=group, kg=vals)
        return True

    def _identity_evidence(self, cat, y, R, t, why):
        """An owner statement "reading y was `cat`" that fits another cat's track better: swap when either
        identity is in doubt, else count a vote (two within SWAP_VOTE_HOURS swap)."""
        def fits(c, k):
            m, _, Pww, _, _ = self._pred(c, t, cap=PRED_CAP_SD)
            return abs(y - m) <= k * math.sqrt(Pww + R)
        if fits(cat, GATE_Z) or not self.tracks[cat]["measured"]:
            return False
        for b in self.CATS:
            if b == cat or not fits(b, SWAP_MATCH_Z):
                continue
            # two independent owner statements are needed (a repeat, the other cat's weigh-in / correction
            # pointing the same way); a single one may just be the wrong cat entered
            votes = [v for v in self.swap_votes if {v[1], v[2]} == {cat, b}]
            if votes:
                self._swap(cat, b, why)
                self.swap_votes = [v for v in self.swap_votes if not ({v[1], v[2]} == {cat, b})]
                return True
            self.swap_votes.append([t, cat, b])
            return False
        return False

    def set_ref(self, cat, payload, now=None):
        """Owner kitchen-scale weigh-in (kg) of `cat` at `now`. Returns fields, or None for a bad command.
        Box and kitchen scale are taken to read the same (no offset is learned).
          * A cat whose identity is settled: a plain measurement when it fits its track; far from it -> rejected
            (published reason) unless confirmed by a second entry within WEIGHIN_CONFIRM_KG / _HOURS; one that
            fits ANOTHER cat's track only swaps the two on a second, independent statement within SWAP_VOTE_HOURS.
          * A cat whose identity is not settled (lost, in an overlap, or in doubt): the entry waits for a SESSION.
            Identity is re-established only when every unsettled cat has an entry within WEIGHIN_SESSION_HOURS,
            the values are >= WEIGHIN_SESSION_MIN_SEP apart and (when the label-free clusters are fresh) each
            lies near a distinct cluster. Never by elimination.
          * The same value (within WEIGHIN_DUP_KG) entered for two cats in one session is rejected for both."""
        now = time.time() if now is None else float(now)
        if cat not in self.CATS:
            return None
        if isinstance(payload, bool):
            return None
        try:
            kg = float(payload)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(kg) or not WEIGHIN_MIN_KG <= kg <= WEIGHIN_MAX_KG:
            return None
        self._prune(now)
        t = now
        y, R = kg, R_WEIGHIN_SD ** 2
        tr = self.tracks[cat]
        m, _, Pww, _, _ = self._pred(cat, t, cap=PRED_CAP_SD)
        in_gate = tr["measured"] and abs(y - m) <= WEIGHIN_GATE_Z * math.sqrt(Pww + R)
        session = [w for w in self.weighins if 0 <= t - w["t"] <= WEIGHIN_SESSION_HOURS * 3600]
        # duplicate: the same number for another cat, or nearly the same one that does not even fit this cat's
        # track (the same cat weighed twice); a real crossing session fits each track and passes
        dup = [w for w in session if w["cat"] != cat and w["action"] != "rejected"
               and (abs(w["kg"] - kg) <= WEIGHIN_DUP_KG + 1e-9
                    or (abs(w["kg"] - kg) <= WEIGHIN_DUP_FAR_KG + 1e-9 and tr["measured"] and not in_gate))]
        confirmed = any(w["cat"] == cat and w["action"] == "rejected" and abs(w["kg"] - kg) <= WEIGHIN_CONFIRM_KG
                        and 0 <= t - w["t"] <= WEIGHIN_CONFIRM_HOURS * 3600 for w in self.weighins)
        in_ov = any(cat in ov["pair"] for ov in self.overlap)
        # identity not settled: lost / in an overlap / still on a seed, or "in doubt" (seed order, after an episode)
        # with a value that does NOT fit its track. A value that fits its own track settles nothing and is just a
        # measurement, doubt or not.
        unsettled = len(self.CATS) > 1 and (tr["ident"] in ("lost", "init") or in_ov or (tr["doubt"] and not in_gate))
        action, reason = "accepted", None
        if dup:
            action = "rejected"
            reason = "same value as %s's weigh-in within a day: entered twice?" % display_name(dup[0]["cat"])
            for w in dup:
                if w["action"] == "pending":
                    w["action"], w["reason"] = "rejected", "same value entered for %s too" % display_name(cat)
        elif unsettled:
            action = "pending"
            reason = "who is who is settled when every cat is weighed within a day"
        elif not tr["measured"]:
            self._anchor(cat, t, y, 0.0, R, SEED_RATE_SD ** 2)
            action = "accepted (first weight)"
        elif in_gate:
            self._kf(cat, t, y, R)
        else:
            other = None
            for b in self.CATS:
                if b == cat or not self.tracks[b]["measured"]:
                    continue
                mb, _, Pb, _, _ = self._pred(b, t, cap=PRED_CAP_SD)
                if abs(y - mb) <= SWAP_MATCH_Z * math.sqrt(Pb + R):
                    other = b
                    break
            # only ANOTHER cat's statement is a second vote: re-entering the same value is the same statement
            #; a confirmation never swaps
            votes = ([v for v in self.swap_votes if {v[1], v[2]} == {cat, other} and v[1] != cat]
                     if other is not None else [])
            if other is not None and votes:
                self._swap(cat, other, "weigh_in")
                self.swap_votes = [v for v in self.swap_votes if {v[1], v[2]} != {cat, other}]
                self._kf(cat, t, y, R)
                action = "accepted (swapped with %s)" % other
            elif other is not None:
                self.swap_votes.append([t, cat, other])
                action = "rejected"
                reason = ("matches %s's box weight; weigh %s too" % (display_name(other), display_name(other)))
            elif confirmed and self._box_disagrees(cat, y, m, t) and not self._others_weighed(cat, y, t):
                # a confirmed far value the box contradicts (its cluster still sits on the track, none near the
                # value): only a weigh-in of every cat lets it through
                action = "rejected"
                reason = "the box disagrees: weigh both cats, then enter it again changed by 0.01 kg"
            elif confirmed:
                self._anchor(cat, t, y, tr["g"], R, max(tr["Pgg"], SEED_RATE_SD ** 2))
                action = "accepted (reset)"
            else:
                action = "rejected"
                reason = ("%+d g from the box weight; enter it again changed by 0.01 kg within 24 h to confirm"
                          % _grams(y - m))
        box = round(m, 3) if self.tracks[cat]["measured"] else None
        self.weighins.append({"t": t, "cat": cat, "kg": round(kg, 3), "action": action.split(" ")[0],
                              "detail": action, "reason": reason, "box": box})
        if action.startswith("accepted"):
            tr = self.tracks[cat]
            if not action == "accepted":
                tr["since"] = t
            tr.update(last_wi=t, n_eff=max(tr["n_eff"], LEARN_MIN_EFF), cusum=0.0)
            if tr["ident"] == "init":
                tr["ident"] = "ok"
        if action != "rejected" and self.last is not None:
            # a later correct_last must not restore the pre-visit snapshot over this weigh-in
            self.last["late_after"] = True
        if action != "rejected" and len(self.CATS) > 1:
            self._settle_session(t)
        log("cats_weigh_in", cat=cat, kg=round(kg, 3), action=self.weighins[-1]["detail"], reason=reason)
        return self.fields(now)

    def _box_disagrees(self, c, y, m, t):
        """Fresh label-free clusters still hold track c (one within SUPPORT_GATE_KG of m) and none is near y."""
        cl = self.cluster
        if cl is None or not 0 <= t - cl["t"] <= SUPPORT_MAX_AGE_HOURS * 3600 or not cl["comps"]:
            return False
        return (min(abs(a - m) for a, _ in cl["comps"]) <= SUPPORT_GATE_KG
                and min(abs(a - y) for a, _ in cl["comps"]) > SUPPORT_GATE_KG)

    def _others_weighed(self, c, y, t):
        """Every other cat has a non-rejected weigh-in in this session, >= WEIGHIN_SESSION_MIN_SEP from y."""
        for b in self.CATS:
            if b == c:
                continue
            w = next((x for x in reversed(self.weighins) if x["cat"] == b and x["action"] != "rejected"
                      and 0 <= t - x["t"] <= WEIGHIN_SESSION_HOURS * 3600), None)
            if w is None or abs(w["kg"] - y) < WEIGHIN_SESSION_MIN_SEP:
                return False
        return True

    def _wi_protects(self, c, now):
        """An accepted weigh-in within REPAIR_AFTER_WEIGHIN_HOURS that the box may not overrule (a far reset never
        protects)."""
        tr = self.tracks[c]
        if tr["last_wi"] is None or now - tr["last_wi"] >= REPAIR_AFTER_WEIGHIN_HOURS * 3600:
            return False
        w = next((x for x in reversed(self.weighins) if x["cat"] == c and x["action"] == "accepted"), None)
        return w is not None and not str(w.get("detail", "")).startswith("accepted (reset)")

    def _settle_session(self, t):
        """Re-establish identities from a weigh-in SESSION: every unsettled cat weighed within the session, values
        far enough apart, each near a distinct fresh label-free cluster (when there is one)."""
        group = [c for c in self.CATS if self.tracks[c]["ident"] in ("lost", "init") or self.tracks[c]["doubt"]
                 or any(c in ov["pair"] for ov in self.overlap)]
        latest = {}
        for w in self.weighins:
            if (w["cat"] in group and w["action"] in ("pending", "accepted")
                    and 0 <= t - w["t"] <= WEIGHIN_SESSION_HOURS * 3600):
                latest[w["cat"]] = w
        if not group or set(latest) != set(group):
            return False
        vals = [latest[c]["kg"] for c in group]
        settled_ms = [self._pred(c, t)[0] for c in self.CATS if c not in group]
        if any(abs(a - b) < WEIGHIN_SESSION_MIN_SEP for a, b in itertools.combinations(vals + settled_ms, 2)):
            ov_only = all(self.tracks[c]["ident"] == "ok" for c in group)       # nobody lost / on a seed
            for c in group:
                w = latest[c]
                m, _, Pww, _, _ = self._pred(c, w["t"], cap=PRED_CAP_SD)
                if (ov_only and w["action"] == "pending"
                        and abs(w["kg"] - m) <= WEIGHIN_GATE_Z * math.sqrt(Pww + R_WEIGHIN_SD ** 2)):
                    # can't tell apart, but the owner weighed EVERY cat: each value fitting its own track is a
                    # plain measurement (the kitchen scale knows who is who; the box does not). Identity stays open.
                    self._kf(c, w["t"], w["kg"], R_WEIGHIN_SD ** 2)
                    self.tracks[c].update(last_wi=w["t"], cusum=0.0)
                    w["action"], w["detail"], w["reason"] = "accepted", "accepted (in overlap)", None
                else:
                    w["reason"] = "values too close to tell the cats apart; still waiting"
            if ov_only and all(latest[c]["action"] == "accepted" for c in group):
                for ov in self.overlap:
                    if set(ov["pair"]) <= set(group):
                        ov["wi"] = t
            return False
        cl = self.cluster
        if cl is not None and t - cl["t"] <= 24 * 3600 and len(cl["comps"]) >= len(group):
            used = set()
            for c in group:
                k = min(range(len(cl["comps"])), key=lambda i: abs(cl["comps"][i][0] - latest[c]["kg"]))
                if k in used or abs(cl["comps"][k][0] - latest[c]["kg"]) > SUPPORT_GATE_KG:
                    for x in group:
                        latest[x]["reason"] = "does not match the weights the box sees; still waiting"
                    return False
                used.add(k)
        self.overlap = [ov for ov in self.overlap if not set(ov["pair"]) & set(group)]
        rates = {c: self._session_rate(c, latest[c], group) for c in group}
        for c in group:
            w = latest[c]
            tr = self.tracks[c]
            self._anchor(c, t, w["kg"], rates[c][0], R_WEIGHIN_SD ** 2, rates[c][1])
            tr = self.tracks[c]
            tr.update(ident="ok", doubt=False, last_wi=w["t"], n_eff=max(tr["n_eff"], LEARN_MIN_EFF), since=t,
                      lost_since=None, sudden=None)
            w["action"], w["detail"], w["reason"] = "accepted", "accepted (session)", None
        self.swap_votes = [v for v in self.swap_votes if not ({v[1], v[2]} & set(group))]
        log("cats_weigh_in_session", cats=group, kg=vals)
        return True

    def _session_rate(self, c, w, group):
        """Growth rate (kg/day, variance) for a cat re-anchored by a weigh-in session: from its own previous
        ACCEPTED kitchen weigh-in 4..21 days earlier (never a pending, unchecked one); else its own pre-session
        track's rate when the value fits that track; else 0 (re-learned). Never another cat's rate; clamped to
        SESSION_RATE_RANGE."""
        lo, hi = SESSION_RATE_RANGE
        for x in reversed(self.weighins):
            if x is w or x["cat"] != c or x["action"] != "accepted":
                continue
            dd = (w["t"] - x["t"]) / DAY
            if 4.0 <= dd <= 21.0:
                g = min(hi, max(lo, (w["kg"] - x["kg"]) / dd))
                return g, 2 * R_WEIGHIN_SD ** 2 / dd ** 2 + Q_G * dd
        m, g, P, _, Pgg = self._pred(c, w["t"], cap=PRED_CAP_SD)
        if abs(w["kg"] - m) <= SWAP_MATCH_Z * math.sqrt(P + R_WEIGHIN_SD ** 2):
            return min(hi, max(lo, g)), max(Pgg, (SEED_RATE_SD / 2) ** 2)
        return 0.0, SEED_RATE_SD ** 2

    def attach_last_record(self, record):
        """Gateway restart: re-link the persisted last visit to its row (matched on ts), so a correction updates
        that row; seed "last" from the row when the state predates it (migration)."""
        if not isinstance(record, dict):
            return
        ts = record.get("ts")
        if self.last is None:
            # state predates "last" (migration): seed it from the row; it can be relabelled, never re-weighed
            cat = record.get("cat")
            if cat not in self.CATS + ("unknown", "two_cats"):
                return
            try:
                t = datetime.datetime.fromisoformat(ts).timestamp() if isinstance(ts, str) else None
            except ValueError:
                t = None
            w = record.get("weight")
            n = record.get("n")
            seg = ([_num(w, W_MIN, W_MAX), R_VISIT_SD ** 2, n == 1, {"n": n if isinstance(n, int) else 2, "upd": False}]
                   if _fnum(w) and W_MIN <= w <= W_MAX else [None, None, False, None])
            cats = record.get("cats") if isinstance(record.get("cats"), list) else None
            labels = ([c if c in self.CATS else "unknown" for c in cats][:2] if cat == "two_cats" and cats
                      else [cat if cat in self.CATS else "unknown"])
            self.last = {"t": t if t is not None and T_MIN <= t <= T_MAX else T_MIN, "segs": [seg], "snap": None,
                         "labels": labels[:1], "confs": [None], "keep": [0] if labels[0] in self.CATS else [],
                         "late_after": True, "ts": ts if isinstance(ts, str) else None}
            if self.last_fields.get("last_cat") is None:
                conf = record.get("conf")
                conf = int(conf) if _fnum(conf) and 0 <= conf <= 100 and cat in self.CATS else None
                if cat == "two_cats" and cats:
                    text = " + ".join(display_name(c) if c in self.CATS else "Unknown" for c in cats[:2])
                elif cat in self.CATS:
                    text = "%s %d%%" % (display_name(cat), conf) if conf is not None else display_name(cat)
                else:
                    text = "Unknown"
                self.last_fields = {"last_cat": cat, "last_conf": conf, "last_visit_cat": text}
            self._last_record = record
            return
        if self.last.get("ts") is None or self.last.get("ts") == ts:
            self._last_record = record

    # ================================================================================================
    # public: published fields (pure function of state and now; never raises)
    # ================================================================================================
    def _trend(self, c, now):
        since = self.tracks[c].get("since")
        start = max(now - TREND_WINDOW_DAYS * DAY, (since + TREND_SETTLE_DAYS * DAY) if since is not None else -math.inf)
        rows = [s for s in self.samples[c] if start <= s[0] <= now]
        if len(rows) < TREND_MIN_N or (rows[-1][0] - rows[0][0]) < TREND_MIN_SPAN_DAYS * DAY:
            return None, "not enough data"
        rows = rows[-150:]
        ts = [(r[0] - rows[0][0]) / DAY for r in rows]
        ys = [r[1] for r in rows]
        slopes = [(ys[j] - ys[i]) / (ts[j] - ts[i])
                  for i in range(len(ts)) for j in range(i + 1, len(ts)) if ts[j] - ts[i] > 0.04]
        if not slopes:
            return None, "not enough data"
        g = _median(slopes)
        tbar = sum(ts) / len(ts)
        icpt = _median([y - g * t for y, t in zip(ys, ts)])
        dev = _median([abs(y - g * t - icpt) for y, t in zip(ys, ts)])
        scale = max(TREND_SCALE_FLOOR, 1.4826 * dev)
        sxx = sum((t - tbar) ** 2 for t in ts)
        se = scale / math.sqrt(sxx) * 7000.0
        g *= 7000.0
        q = _tq(len(rows) - 2)
        if g - q * se > 0 and g >= TREND_MIN_G_WEEK:
            word = "gaining"
        elif g + q * se < 0 and g <= -TREND_MIN_G_WEEK:
            word = "losing"
        else:
            word = "steady"
        return int(round(g)), word

    def status(self, now=None):
        now = time.time() if now is None else float(now)
        if self.overlap:
            if any(self._ov_escalated(ov, now) for ov in self.overlap):
                return "needs_weigh_in"
            return "cant_tell_apart"
        if any(self.tracks[c]["ident"] == "lost" for c in self.CATS):
            return "needs_weigh_in"
        if any(self.tracks[c]["ident"] == "init" or self.tracks[c]["n_eff"] < LEARN_MIN_EFF for c in self.CATS):
            return "learning"
        if self._unidentified(now) or any(self._unsupported(c, now) or not self._fresh_ok(c, now)
                                          or self._weighin_disagrees(c, now) for c in self.CATS):
            return "needs_weigh_in"
        return "ok"

    @staticmethod
    def _ov_escalated(ov, now):
        """An overlap older than OVERLAP_ESCALATE_HOURS, counted from its start or from the last weigh-in session
        of its cats (the owner did what needs_weigh_in asked)."""
        return now - max(ov["since"], ov.get("wi") or -math.inf) > OVERLAP_ESCALATE_HOURS * 3600

    def _fresh_ok(self, c, now):
        """"ok" needs fresh evidence: a labelled visit or an accepted weigh-in of the cat within FRESH_OK_HOURS."""
        tr = self.tracks[c]
        fresh = max(tr["last_seen"] or -math.inf, tr["last_wi"] or -math.inf)
        return len(self.CATS) < 2 or now - fresh <= FRESH_OK_HOURS * 3600

    def _weighin_disagrees(self, c, now, accepted_only=False):
        """The cat's newest weigh-in (WEIGHIN_CHECK_DAYS) is outside the published +-2 sd interval, taken back to
        the weigh-in's time, widened by WEIGHIN_CHECK_TOL_KG (kitchen vs box). Entries rejected as duplicates or
        more than WEIGHIN_MAX_RESET_KG off (a carrier, a typo) do not count: their reason is published."""
        tr = self.tracks[c]
        if not tr["measured"]:
            return False
        for wi in reversed(self.weighins):
            if wi["cat"] != c or not 0 <= now - wi["t"] <= WEIGHIN_CHECK_DAYS * DAY:
                continue
            if wi["action"] not in ("accepted", "rejected") or (wi.get("reason") or "").startswith("same value"):
                continue
            if accepted_only and wi["action"] != "accepted":
                continue
            dt = (wi["t"] - tr["t"]) / DAY
            m = tr["w"] + tr["g"] * dt
            if abs(wi["kg"] - m) > WEIGHIN_MAX_RESET_KG:
                continue
            sd = math.sqrt(tr["P_upd"] + dt * dt * tr["Pgg"])
            return abs(wi["kg"] - m) > 2 * sd + WEIGHIN_CHECK_TOL_KG
        return False

    def _unidentified(self, now):
        """A cat with no labelled visit for UNIDENTIFIED_HOURS while >= UNIDENTIFIED_MIN_UNKNOWN weighed visits
        were left unknown in that time: something is going on that the labels do not show (never "ok")."""
        return bool(self._unidentified_cats(now))

    def _unidentified_cats(self, now):
        """The set of cats that are unidentified (see _unidentified): judged per cat."""
        out = set()
        if len(self.CATS) < 2:
            return out
        for c in self.CATS:
            ts = sorted(v[0] for v in self.visits if c in v[1] and v[0] <= now)
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            hours = UNIDENTIFIED_HOURS
            if len(gaps) >= 5:
                hours = min(UNIDENTIFIED_HOURS, max(UNID_MIN_HOURS, UNID_GAP_MULT * _median(gaps) / 3600))
            seen = max(self.tracks[c]["last_seen"] or -math.inf, self.tracks[c]["last_wi"] or -math.inf)
            if seen >= now - hours * 3600:
                continue
            # weighed visits left unknown since this cat was last identified
            if sum(1 for v in self.visits if seen < v[0] <= now and v[1] == ["unknown"] and v[2] > 0) >= UNIDENTIFIED_MIN_UNKNOWN:
                out.add(c)
        return out

    def _unsupported(self, c, now):
        """The label-free clusters (fresh, >= 2 of them) do not support this cat's track: no cluster within
        SUPPORT_GATE_KG. Its weight is not published and the status asks for a weigh-in."""
        cl = self.cluster
        tr = self.tracks[c]
        if (len(self.CATS) < 2 or cl is None or now - cl["t"] > SUPPORT_MAX_AGE_HOURS * 3600
                or len(cl["comps"]) < SUPPORT_MIN_COMPS
                or tr["ident"] != "ok" or self._wi_protects(c, now)):
            return False
        if len(cl["comps"]) == 1 and (cl.get("sup") or {}).get(c, SUPPORT_OWN_MIN) >= SUPPORT_OWN_MIN:
            return False        # one cluster only (the other cat's?) but this track has readings of its own
        m = self._pred(c, cl["t"], cap=PRED_CAP_SD)[0] if cl["t"] > tr["t"] else tr["w"]
        return min(abs(m - a) for a, _ in cl["comps"]) > SUPPORT_GATE_KG

    def fields(self, now=None):
        now = time.time() if now is None else float(now)
        try:
            return self._fields(now)
        except Exception as exc:        # never break the gateway's snapshot
            log("cat_fields_error", error=type(exc).__name__)
            out = {}
            for c in self.CATS:
                out.update({c + "_weight": None, c + "_weight_unc_g": None, c + "_weight_lo_kg": None,
                            c + "_weight_hi_kg": None, c + "_growth_g_week": None,
                            c + "_trend_word": "not enough data", c + "_last_seen": None, c + "_stale": True,
                            c + "_stale_reason": None, c + "_visits_total": None, c + "_visits_24h": None,
                            c + "_last_weigh_in": None})
            out.update({"cat_status": "learning", "cat_gap": None, "labelled_pct_7d": None,
                        "unknown_visits_total": None, "two_cat_visits_total": None, "correction_count": None,
                        "visits_24h_total": None, "readings_above_window_24h": None, "scale_shift": None, "cluster_low_kg": None, "cluster_high_kg": None,
                        "cluster_low_n": None, "cluster_high_n": None, "last_cat": None, "last_conf": None,
                        "last_visit_cat": None})
            return out

    def _fields(self, now):
        out = {}
        pub = {}
        off = 0.0                       # weights are on the box scale = the kitchen scale (no offset learned)
        day_visits = [v for v in self.visits if now - DAY <= v[0] <= now]
        unknown_3d = any(v[1] == ["unknown"] and v[2] > 0 for v in self.visits if now - STALE_HOURS * 3600 <= v[0] <= now)
        unid = self._unidentified_cats(now)
        for c in self.CATS:
            tr = self.tracks[c]
            fresh = max(tr["last_seen"] or -math.inf, tr["last_wi"] or -math.inf)
            stale = (not math.isfinite(fresh)) or (now - fresh) > STALE_HOURS * 3600
            # a lost identity (needs_weigh_in) or a seed is never published as a cat's weight
            # in "can't tell apart" the cat is still seen (ambiguous visits), just not named: its growth line is
            # published while it is tight enough (status says cant_tell_apart; lo/hi carry the uncertainty)
            in_ov = any(c in ov["pair"] for ov in self.overlap)
            escalated = (any(c in ov["pair"] and self._ov_escalated(ov, now) for ov in self.overlap)
                         and not (tr["last_wi"] is not None and now - tr["last_wi"] <= ESCALATED_WI_HOURS * 3600))
            seen_ov = in_ov and any(c in a[1] for a in self.ambig if now - OVERLAP_SEEN_HOURS * 3600 <= a[0] <= now)
            m_now, _, P_now, _, _ = self._pred(c, now)
            if in_ov:
                # growth extrapolated at most OVERLAP_RATE_DAYS; the variance keeps the full rate uncertainty
                dtr = min(max(0.0, (now - tr["t"]) / DAY), OVERLAP_RATE_DAYS)
                m_now = tr["w"] + RATE_TAU_DAYS * (1.0 - math.exp(-dtr / RATE_TAU_DAYS)) * tr["g"]
            sd_now = math.sqrt(max(P_now, 1e-10))
            # outside an overlap, a cat that is unidentified, or whose newest ACCEPTED weigh-in disagrees with the
            # track, is not published; a rejected typo only flags the status
            doubtful = not in_ov and (c in unid or self._weighin_disagrees(c, now, accepted_only=True))
            blank = ((stale and not seen_ov) or not tr["measured"] or tr["ident"] in ("init", "lost")
                     or self._unsupported(c, now) or sd_now > PUBLISH_MAX_SD or escalated or doubtful)
            w = None if blank else round(m_now - off, 3)
            pub[c] = w
            g, word = self._trend(c, now)
            reason = None
            if w is None:
                # sudden_change: a CUSUM alarm, or the fresh label-free clusters no longer hold this cat's track
                reason = ("sudden_change" if ((tr["ident"] == "lost" and tr.get("sudden") is not None)
                                              or self._unsupported(c, now)) else
                          "not_identified" if (doubtful or unknown_3d or tr["ident"] == "lost" or in_ov)
                          else "not_seen")
            out[c + "_weight"] = w
            out[c + "_weight_unc_g"] = None if w is None else int(round(1000 * sd_now))
            out[c + "_weight_lo_kg"] = None if w is None else round(w - 2 * sd_now, 3)
            out[c + "_weight_hi_kg"] = None if w is None else round(w + 2 * sd_now, 3)
            out[c + "_growth_g_week"] = g
            out[c + "_trend_word"] = word
            out[c + "_last_seen"] = _iso(tr["last_seen"])
            out[c + "_stale"] = bool(stale)
            out[c + "_stale_reason"] = reason
            out[c + "_visits_total"] = self.visits_total[c]
            out[c + "_visits_24h"] = sum(1 for v in day_visits if c in v[1])
            wi = next((x for x in reversed(self.weighins) if x["cat"] == c), None)
            out[c + "_last_weigh_in"] = (None if wi is None else
                                         ("%s %.2f kg %s" % (wi["action"], wi["kg"], _iso(wi["t"])[:16]))
                                         + (": " + wi["reason"] if wi.get("reason") else ""))
        gaps = [abs(_grams(pub[a]) - _grams(pub[b])) for a, b in itertools.combinations(self.CATS, 2)
                if pub[a] is not None and pub[b] is not None]
        out["cat_status"] = self.status(now)
        out["cat_gap"] = min(gaps) if gaps else None
        week = [l for v in self.visits if now - 7 * DAY <= v[0] <= now for l in v[1]]
        out["labelled_pct_7d"] = (int(round(100.0 * sum(1 for l in week if l in self.CATS) / len(week)))
                                  if week else None)
        out["visits_24h_total"] = len(day_visits)
        ev = [e for e in self.scale_events if 0 <= now - e[0] <= SCALE_SHIFT_SHOW_HOURS * 3600]
        # diagnostic: the box read every cat heavier / lighter at once (litter top-up, tare drift): "scale_shift"
        out["scale_shift"] = (None if not ev else "scale_shift %+d g %s" % (ev[-1][2], _iso(ev[-1][0])[:16]))
        out["readings_above_window_24h"] = sum(n for at, n in self.above if now - DAY <= at <= now)
        cl = self.cluster
        ok_cl = cl is not None and now - cl["t"] <= CLUSTER_MAX_AGE_HOURS * 3600 and len(self.CATS) > 1 \
            and len(cl["comps"]) >= 2
        out["cluster_low_kg"] = round(cl["comps"][0][0] - off, 3) if ok_cl else None
        out["cluster_high_kg"] = round(cl["comps"][-1][0] - off, 3) if ok_cl else None
        out["cluster_low_n"] = int(round(cl["comps"][0][1])) if ok_cl else None
        out["cluster_high_n"] = int(round(cl["comps"][-1][1])) if ok_cl else None
        out["unknown_visits_total"] = self.unknown_visits_total
        out["two_cat_visits_total"] = self.two_cat_visits_total
        out["correction_count"] = self.correction_count
        out.update(self.last_fields)
        return out

    @property
    def offset(self):
        """Compatibility view for probes: no offset is learned any more (always 0)."""
        return {"d": 0.0, "P": 0.0}

    def diagnostics(self, now=None):
        now = time.time() if now is None else float(now)
        return {"offset_g": 0,
                "ident": {c: self.tracks[c]["ident"] for c in self.CATS},
                "doubt": {c: self.tracks[c]["doubt"] for c in self.CATS},
                "overlap": self.overlap, "repairs_total": self.repairs_total, "weighins": self.weighins[-5:],
                "pred": {c: round(self._pred(c, now)[0], 3) for c in self.CATS},
                "plausible_range": self.plausible_range(now), "cluster": self.cluster}

    # ================================================================================================
    # persistence: <stem>_v2.json; the v1 file is read for migration only
    # ================================================================================================
    def _to_json(self):
        return {"version": STATE_VERSION, "cats": list(self.CATS), "seeds": self.seeds, "tracks": self.tracks,
                "samples": self.samples, "recent": self.recent, "ambig": self.ambig, "visits": self.visits,
                "weighins": self.weighins, "overlap": self.overlap, "swap_votes": self.swap_votes,
                "box_ids": self.box_ids, "innov": self.innov, "scale_events": self.scale_events, "cluster": self.cluster, "above": self.above,
                "lf_last": self.lf_last if math.isfinite(self.lf_last) else None,
                "counters": {"visits_total": self.visits_total, "unknown_visits_total": self.unknown_visits_total,
                             "two_cat_visits_total": self.two_cat_visits_total,
                             "correction_count": self.correction_count, "repairs_total": self.repairs_total},
                "last": self.last, "last_fields": self.last_fields, "v1": self.v1}

    def save(self):
        if not self.v2_path:
            return
        if self.save_blocked:
            log("cats_persist_failed", error="state file could not be quarantined; not overwriting it")
            return
        tmp = self.v2_path + ".tmp"
        try:
            data = json.dumps(self._to_json(), separators=(",", ":"), allow_nan=False)
            with open(tmp, "w") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.v2_path)
            try:
                dfd = os.open(os.path.dirname(os.path.abspath(self.v2_path)), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        except (OSError, TypeError, ValueError) as exc:
            log("cats_persist_failed", error=type(exc).__name__)

    def _load(self, now):
        src = None
        for p in (self.v2_path, self.path):
            if p and os.path.exists(p):
                src = p
                break
        if src is None:
            return
        try:
            with open(src) as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            if src == self.v2_path and (data.get("version") != STATE_VERSION or not isinstance(data.get("tracks"), dict)):
                raise ValueError("wrong version or shape")
        except (OSError, ValueError) as exc:
            if src == self.v2_path:
                # quarantined and logged, then a FRESH state: the v1 file is install-time state, never re-migrated
                # here (a deliberate re-migration is the gateway's job)
                self._quarantine(src, now, type(exc).__name__ + ": " + str(exc)[:60])
            else:
                log("cats_state_unreadable", error=type(exc).__name__, moved_to=None, file="v1")
            return
        try:
            if src == self.v2_path:
                dropped = self._load_v2(data, now)
            else:
                dropped = self._migrate_v1(data, now)
        except Exception as exc:          # validation is per field; this is the last resort
            self._fresh(now)
            if src == self.v2_path:
                self._quarantine(src, now, type(exc).__name__)
            else:
                log("cats_state_unreadable", error=type(exc).__name__, moved_to=None, file="v1")
            return
        if dropped:
            log("cats_state_dropped", entries=sorted(set(dropped))[:20])

    def _quarantine(self, src, now, why):
        """Move an unusable v2 file aside (never overwritten) and start fresh. If it cannot be moved, saving is
        disabled so the unreadable file is not replaced either."""
        bad = "%s.corrupt-%d" % (src, int(now))
        try:
            os.replace(src, bad)
        except OSError:
            bad = None
            self.save_blocked = True
        log("cats_state_unreadable", error=why, moved_to=bad, saving=not self.save_blocked)
        self._fresh(now)

    def _valid_track(self, c, tr, now):
        base = self._seed_track(c, now)
        out = dict(base)
        out["w"] = _num(tr["w"], W_MIN, W_MAX)
        out["g"] = _num(tr["g"], -0.5, 0.5)
        out["Pww"] = _num(tr["Pww"], 1e-9, 4.0)
        out["Pgg"] = _num(tr["Pgg"], 1e-12, 1.0)
        out["Pwg"] = _num(tr.get("Pwg", 0.0), -1.0, 1.0)
        lim = math.sqrt(out["Pww"] * out["Pgg"]) * 0.999
        out["Pwg"] = max(-lim, min(lim, out["Pwg"]))
        out["P_upd"] = _num(tr.get("P_upd", out["Pww"]), 1e-9, 4.0)
        out["t"] = _num(tr["t"], T_MIN, T_MAX)
        out["n_eff"] = _num(tr.get("n_eff", 0.0), 0.0, 1e9)
        out["ident"] = tr.get("ident") if tr.get("ident") in ("init", "ok", "lost") else "init"
        out["doubt"] = tr.get("doubt") is True
        for k in ("last_seen", "last_wi", "since", "lost_since", "sudden"):
            v = tr.get(k)
            out[k] = _num(v, T_MIN, T_MAX) if v is not None else None
        out["measured"] = tr.get("measured") is True
        out["cusum"] = _num(tr.get("cusum", 0.0), 0.0, 1e6)
        return out

    def _load_v2(self, data, now):
        dropped = []

        def ok(name, fn, default):
            try:
                return fn()
            except Exception:
                dropped.append(name)
                return default

        saved = data.get("cats") if isinstance(data.get("cats"), list) else []
        if set(saved) != set(self.CATS):
            log("cats_state_mismatch", configured=list(self.CATS), saved=saved)
        tracks = data.get("tracks") if isinstance(data.get("tracks"), dict) else {}
        samples = data.get("samples") if isinstance(data.get("samples"), dict) else {}
        cnt = data.get("counters") if isinstance(data.get("counters"), dict) else {}
        vt = cnt.get("visits_total") if isinstance(cnt.get("visits_total"), dict) else {}
        for c in self.CATS:
            if c in tracks:
                self.tracks[c] = ok("tracks." + c, lambda: self._valid_track(c, tracks[c], now), self._seed_track(c, now))
            rows = samples.get(c) if isinstance(samples.get(c), list) else []
            self.samples[c] = [r for r in (ok("samples." + c, lambda: [_num(x[0], T_MIN, T_MAX), _num(x[1], W_MIN, W_MAX),
                                                                          int(x[2]) if x[2] in (1, 2) else 2], None)
                                           for x in rows) if r is not None]
            self.visits_total[c] = ok("visits_total." + c, lambda: int(_num(vt.get(c, 0), 0, 1e9)), 0)

        def rows3(name, lo2, hi2):
            src = data.get(name) if isinstance(data.get(name), list) else []
            out = []
            for x in src:
                r = ok(name, lambda: [_num(x[0], T_MIN, T_MAX), _num(x[1], lo2, hi2), _num(x[2], 0.0, 1.0)], None)
                if r is not None:
                    out.append(r)
            return out
        self.recent = rows3("recent", W_MIN, W_MAX)
        self.ambig = [a for a in (ok("ambig", lambda: [_num(x[0], T_MIN, T_MAX), [c for c in x[1] if c in self.CATS]], None)
                                  for x in (data.get("ambig") if isinstance(data.get("ambig"), list) else []))
                      if a is not None]
        self.visits = [v for v in (ok("visits", lambda: [_num(x[0], T_MIN, T_MAX),
                                                         [l if l in self.CATS else "unknown" for l in x[1]][:2] or ["unknown"],
                                                         int(_num(x[2] if len(x) > 2 else 1, 0, 2))], None)
                                   for x in (data.get("visits") if isinstance(data.get("visits"), list) else []))
                       if v is not None]
        wis = []
        for x in data.get("weighins") if isinstance(data.get("weighins"), list) else []:
            w = ok("weighins", lambda: {"t": _num(x["t"], T_MIN, T_MAX), "cat": x["cat"] if x["cat"] in self.CATS else 1 / 0,
                                        "kg": _num(x["kg"], WEIGHIN_MIN_KG, WEIGHIN_MAX_KG),
                                        "action": str(x.get("action", "accepted")), "detail": str(x.get("detail", "")),
                                        "reason": x.get("reason") if isinstance(x.get("reason"), str) else None,
                                        "box": _num(x["box"], 0, W_MAX) if x.get("box") is not None else None}, None)
            if w is not None:
                wis.append(w)
        self.weighins = wis
        ovs = []
        for x in data.get("overlap") if isinstance(data.get("overlap"), list) else []:
            o = ok("overlap", lambda: {"pair": [p for p in x["pair"] if p in self.CATS][:2] if len(x["pair"]) == 2 else 1 / 0,
                                       "since": _num(x["since"], T_MIN, T_MAX),
                                       "sep_since": _num(x["sep_since"], T_MIN, T_MAX) if x.get("sep_since") is not None else None,
                                       "order": [p for p in x["order"] if p in self.CATS],
                                       "wi": _num(x["wi"], T_MIN, T_MAX) if x.get("wi") is not None else None}, None)
            if o is not None and len(o["pair"]) == 2 and len(o["order"]) == 2:
                ovs.append(o)
        self.overlap = ovs
        self.swap_votes = [v for v in (ok("swap_votes", lambda: [_num(x[0], T_MIN, T_MAX), x[1], x[2]]
                                          if x[1] in self.CATS and x[2] in self.CATS else 1 / 0, None)
                                       for x in (data.get("swap_votes") if isinstance(data.get("swap_votes"), list) else []))
                           if v is not None]
        self.box_ids = [v for v in (ok("box_ids", lambda: [_num(x[0], T_MIN, T_MAX), x[1] if x[1] in self.CATS else 1 / 0,
                                                           _num(x[2], W_MIN, W_MAX)], None)
                                    for x in (data.get("box_ids") if isinstance(data.get("box_ids"), list) else []))
                        if v is not None]
        cl = data.get("cluster")
        self.cluster = ok("cluster", lambda: None if cl is None else dict(
            {"t": _num(cl["t"], T_MIN, T_MAX), "comps": [[_num(a, W_MIN, W_MAX), _num(n, 0, 1e6)] for a, n in cl["comps"]]},
            **({"sup": {c: int(_num(cl["sup"][c], 0, 1e6)) for c in self.CATS}} if isinstance(cl.get("sup"), dict) else {})), None)
        self.scale_events = [v for v in (ok("scale_events", lambda: [_num(x[0], T_MIN, T_MAX),
                                                                    x[1] if x[1] in self.CATS else 1 / 0,
                                                                    int(_num(x[2], -1e5, 1e5))], None)
                                         for x in (data.get("scale_events") if isinstance(data.get("scale_events"), list)
                                                   else [])) if v is not None]
        inn = data.get("innov") if isinstance(data.get("innov"), dict) else {}
        for c in self.CATS:
            rows = inn.get(c) if isinstance(inn.get(c), list) else []
            self.innov[c] = [r for r in (ok("innov", lambda: [_num(x[0], T_MIN, T_MAX), _num(x[1], -1e3, 1e3),
                                                             _num(x[2], -W_MAX, W_MAX)], None) for x in rows) if r is not None]
        self.above = [g for g in (ok("above", lambda: [_num(x[0], T_MIN, T_MAX), int(_num(x[1], 0, 1e6))], None)
                                  for x in (data.get("above") if isinstance(data.get("above"), list) else []))
                      if g is not None]
        lfl = data.get("lf_last")
        self.lf_last = ok("lf_last", lambda: _num(lfl, T_MIN, T_MAX), -math.inf) if lfl is not None else -math.inf
        for k in ("unknown_visits_total", "two_cat_visits_total", "correction_count", "repairs_total"):
            setattr(self, k, ok(k, lambda: int(_num(cnt.get(k, 0), 0, 1e9)), 0))
        if set(saved) == set(self.CATS):
            self.last = ok("last", lambda: self._valid_last(data.get("last"), now), None)
            lf = data.get("last_fields")
            if isinstance(lf, dict):
                cat = lf.get("last_cat")
                if cat in self.CATS + ("unknown", "two_cats", None):
                    conf = lf.get("last_conf")
                    self.last_fields = {"last_cat": cat,
                                        "last_conf": int(conf) if _fnum(conf) and 0 <= conf <= 100 else None,
                                        "last_visit_cat": lf.get("last_visit_cat") if isinstance(lf.get("last_visit_cat"), str) else None}
                else:
                    dropped.append("last_fields")
        self.v1 = data.get("v1") if isinstance(data.get("v1"), dict) else None
        self._prune(now)
        return dropped

    def _valid_last(self, L, now):
        if L is None:
            return None
        if not isinstance(L, dict):
            raise ValueError("bad last")
        out = {"t": _num(L["t"], T_MIN, T_MAX), "late_after": L.get("late_after") is True,
               "ts": L.get("ts") if isinstance(L.get("ts"), str) else None}
        segs = []
        for x in L.get("segs") or []:
            y, R, single, info = (list(x) + [None] * 4)[:4]
            if y is None:
                segs.append([None, None, bool(single), None])
            else:
                info = info if isinstance(info, dict) else {"n": 2, "upd": True}
                segs.append([_num(y, W_MIN, W_MAX), _num(R, 1e-8, 1.0), bool(single),
                             {"n": int(_num(info.get("n", 2), 1, 1e6)), "upd": info.get("upd") is True}])
        out["segs"] = segs[:2]
        out["labels"] = [l if l in self.CATS else "unknown" for l in (L.get("labels") or [])][:2]
        out["confs"] = [int(c) if _fnum(c) else None for c in (L.get("confs") or [])][:2]
        out["keep"] = [int(k) for k in (L.get("keep") or []) if k in (0, 1)]
        snap = L.get("snap")
        if isinstance(snap, dict) and isinstance(snap.get("tracks"), dict) and set(snap["tracks"]) == set(self.CATS):
            out["snap"] = {"tracks": {c: self._valid_track(c, snap["tracks"][c], now) for c in self.CATS},
                           "overlap": [o for o in snap.get("overlap", []) if isinstance(o, dict) and "pair" in o
                                       and "since" in o and "order" in o],
                           "n_samples": {c: int(_num(snap.get("n_samples", {}).get(c, 0), 0, 1e6)) for c in self.CATS},
                           "repairs_total": int(_num(snap.get("repairs_total", 0), 0, 1e9))}
        else:
            out["snap"] = None
            out["late_after"] = True
        return out

    def _migrate_v1(self, data, now):
        """v1 = {refs, samples{cat:[{t,w}]}, correction_count, total_visits, labelled_visits, degraded} (the
        cats.json of catlink-local 0.1); optional extra keys visits_total / unknown_visits_total /
        two_cat_visits_total / last_seen / visit_log / last are carried over too. Each cat's samples are replayed into
        its track from the v1 reference, with the growth rate initialised from a Theil-Sen slope when the samples
        span >= 2 days. Read only."""
        dropped = []
        refs = data.get("refs") or {}
        if not isinstance(refs, dict) or set(refs) != set(self.CATS):
            log("cats_state_mismatch", configured=list(self.CATS),
                saved=sorted(refs) if isinstance(refs, dict) else None)
            return dropped
        samples = data.get("samples") if isinstance(data.get("samples"), dict) else {}
        for c in self.CATS:
            rows = []
            for x in samples.get(c, []) if isinstance(samples.get(c), list) else []:
                try:
                    rows.append((_num(x["t"], T_MIN, T_MAX), _num(x["w"], W_MIN, W_MAX)))
                except Exception:
                    dropped.append("samples." + c)
            rows.sort()
            if len(rows) >= 3:
                # v1 samples were confident labels of the old tracker, a few of them contaminated (a reading with
                # a second cat's paws on the scale): drop anything far from that cat's median
                med = _median([w for _, w in rows])
                kept = [r for r in rows if abs(r[1] - med) <= MIGRATE_MAX_DEV_KG]
                if len(kept) < len(rows):
                    dropped.append("samples.%s (%d outliers)" % (c, len(rows) - len(kept)))
                rows = kept
            tr = self._seed_track(c, now)
            if _fnum(refs.get(c)) and W_MIN <= refs[c] <= W_MAX:
                tr["w"] = float(refs[c])
            if rows:
                g0, pg0 = 0.0, SEED_RATE_SD ** 2
                if rows[-1][0] - rows[0][0] >= 2 * DAY and len(rows) >= 3:
                    sl = [(b[1] - a[1]) / ((b[0] - a[0]) / DAY) for i, a in enumerate(rows) for b in rows[i + 1:]
                          if b[0] - a[0] > 0.25 * DAY]
                    if sl:
                        g0 = max(-0.05, min(0.05, _median(sl)))
                        pg0 = (0.006) ** 2
                tr.update(t=rows[0][0], w=rows[0][1] if not _fnum(refs.get(c)) else tr["w"], g=g0, Pgg=pg0)
                self.tracks[c] = tr
                for t, w in rows:
                    self._kf(c, t, w, R_VISIT_SD ** 2)
                    self.samples[c].append([t, w, 2])
                    self.recent.append([t, w, R_VISIT_SD ** 2])
                tr = self.tracks[c]
                # the old tracker's labels are the identity the owner already has: the track is "ok" (status
                # "learning" until it has LEARN_MIN_EFF updates)
                tr.update(n_eff=float(len(rows)), ident="ok", last_seen=rows[-1][0], measured=True, since=rows[0][0])
            else:
                self.tracks[c] = tr
        self.recent.sort()
        cnt = lambda k: int(data[k]) if isinstance(data.get(k), int) and not isinstance(data.get(k), bool) and data[k] >= 0 else 0  # noqa: E731
        self.correction_count = cnt("correction_count")
        self.unknown_visits_total = cnt("unknown_visits_total")
        self.two_cat_visits_total = cnt("two_cat_visits_total")
        vt = data.get("visits_total") if isinstance(data.get("visits_total"), dict) else {}
        for c in self.CATS:
            v = vt.get(c)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                self.visits_total[c] = v
        ls = data.get("last_seen") if isinstance(data.get("last_seen"), dict) else {}
        for c in self.CATS:
            if _fnum(ls.get(c)) and T_MIN <= ls[c] <= T_MAX:
                self.tracks[c]["last_seen"] = max(self.tracks[c]["last_seen"] or 0.0, float(ls[c]))
        for v in data.get("visit_log") if isinstance(data.get("visit_log"), list) else []:
            if isinstance(v, dict) and _fnum(v.get("t")) and T_MIN <= v["t"] <= T_MAX and isinstance(v.get("cats"), list):
                named = sorted(set(c for c in v["cats"] if c in self.CATS))
                self.visits.append([float(v["t"]), named or ["unknown"], 2 if len(named) == 2 else 1])
        last = data.get("last")
        if isinstance(last, dict) and last.get("cat") in self.CATS + ("unknown", "two_cats"):
            cat = last["cat"]
            conf = last.get("conf") if _fnum(last.get("conf")) else None
            cats = last.get("cats") if isinstance(last.get("cats"), list) else None
            if cat == "two_cats" and cats:
                text = " + ".join(display_name(c) if c in self.CATS else "Unknown" for c in cats[:2])
            elif cat in self.CATS:
                text = "%s %d%%" % (display_name(cat), conf) if conf is not None else display_name(cat)
            else:
                text = "Unknown"
            self.last_fields = {"last_cat": cat, "last_conf": int(conf) if conf is not None and cat in self.CATS else None,
                                "last_visit_cat": text}
        self.v1 = {"total_visits": cnt("total_visits"), "labelled_visits": cnt("labelled_visits"),
                   "degraded": data.get("degraded") is True, "refs": {c: refs[c] for c in self.CATS},
                   "shim": data.get("shim") is not None, "migrated_at": now}
        self._prune(now)
        log("cats_state_migrated", from_version=1, samples={c: len(self.samples[c]) for c in self.CATS})
        return dropped
