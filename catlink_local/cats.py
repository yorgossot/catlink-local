"""Cat identification + weight tracking from the per-visit settled weight (0, 1 or 2 configured cats).

PURE ANALYSIS. This never touches the box side, send()/ALLOWED_TX, publish_to_box, or any movement or
settings interlock. The only input is the per-visit settled weight the gateway already computes (10 g
resolution, +/-20-30 g noise; two cats' weights may overlap). No camera, no RFID.

Configured with CATLINK_CATS = "slug=kg[,slug=kg]", lighter cat first:
  * 0 cats: the gateway does not create a tracker at all.
  * 1 cat:  every finalised visit is that cat (conf 100); tracked weight = median of the rolling window.
  * 2 cats: the pairwise algorithm in CatTracker below; cats[0] is the lighter, cats[1] the heavier.
"""
import json
import math
import os
import statistics
import time

from .log import log

CAT_UNKNOWN_BAND = 0.06         # kg: a settled weight within midpoint ± this is too ambiguous -> "unknown"
CAT_GAP_FLOOR = 0.15            # kg: references closer than this => "degraded"; re-fit never crosses them
CAT_SIGMA = 0.07                # kg: gaussian width for the confidence % (≈ the scale noise)
CAT_UNSETTLED_BAND_FACTOR = 2   # a visit too short for a settled weight (the box max-hold peak stands in) gets
                                # this x the unknown band and never becomes a weight sample. There is no timing
                                # rule: an earlier "two visits within 15 min are different cats" rule kept
                                # mislabelling one cat coming back several times in a row.
CAT_WINDOW_DAYS = 14            # tracked weight = median of confident visits within this rolling window ...
CAT_WINDOW_MAX = 10             # ... and at most this many most-recent confident visits per cat
CAT_JOINT_CONF = 95             # confidence stamped on a manual correction
CAT_TREND_DEADBAND_G = 20       # |7-day change| at/under this reads as "steady"
CAT_SINGLE_CONF = 100           # confidence stamped on every visit when exactly one cat is configured
SET_REF_MIN_KG = 0.2            # sanity bound on a manual weigh-in (kg); out of range is ignored
SET_REF_MAX_KG = 12.0


def display_name(slug):
    """'cat_a' -> 'Cat a' (used in the last_visit_cat text and the entity names)."""
    return slug.replace("_", " ").capitalize()


class CatTracker:
    """Label each completed visit with a configured cat (or "unknown"), with a confidence %, and track
    each cat's weight as the MEDIAN of its recent CONFIDENT visits.

    PURE ANALYSIS. It is only ever called with a settled weight that the gateway's deposit logic already
    computed; it never reads a socket, never builds or sends a box frame, and never consults a movement
    or settings interlock. All of its state lives here and is persisted to `path` (the StateDirectory).

    Rules with two cats (see docs/DESIGN.md):
      * Classify by gaussian likelihood (σ≈70 g) to the two references, with an UNKNOWN band: a weight
        within midpoint ± CAT_UNKNOWN_BAND, or while "degraded" (references within CAT_GAP_FLOOR), is
        "unknown". Otherwise the nearer cat, with a confidence % from the likelihood ratio. Each visit is
        classified on its own weight only; no timing or alternation assumption (cats do whatever).
      * A visit too short for a settled weight (the box's max-hold peak stands in) is less certain: it
        gets CAT_UNSETTLED_BAND_FACTOR x the unknown band and is never added as a weight sample.
      * Tracked weight = MEDIAN of that cat's confident visits in the rolling window (NOT an EMA, NOT
        the last reading). A 7-day trend and the current gap are derived from the same samples.
      * References update ONLY from (a) a confident rolling re-fit to the per-cat medians, which keeps
        the CAT_GAP_FLOOR (never crosses the cats; flags degraded if the data pulls them closer), and
        (b) explicit manual input (correct_last / set_ref_<slug>). Never an order-dependent EMA on
        self-labels.

    With one cat there is nothing to classify: every visit is that cat at CAT_SINGLE_CONF, there is no
    gap, no degraded state and no unknown band; correct_last is a no-op and set_ref sets the reference
    (the weight reported until the first visit).
    """

    def __init__(self, cats, path):
        cats = list(cats)
        if not 1 <= len(cats) <= 2:
            raise ValueError("CatTracker needs 1 or 2 cats")
        self.CATS = tuple(slug for slug, _ in cats)
        self.path = path
        self.pair = len(self.CATS) == 2
        self.lo = self.CATS[0]                       # the lighter cat (or the only one)
        self.hi = self.CATS[1] if self.pair else None  # the heavier cat
        self.refs = {slug: float(seed) for slug, seed in cats}
        self.samples = {slug: [] for slug in self.CATS}   # [{"t": epoch, "w": kg}] confident, oldest->newest
        self.correction_count = 0
        self.total_visits = 0
        self.labelled_visits = 0
        self.degraded = False
        self._last = None   # most-recent finalized visit: {"at","cat","w","sample","record","settled"} (correct_last)
        self._load()

    # ---- classification -----------------------------------------------------------------------
    def _classify(self, w, band=CAT_UNKNOWN_BAND):
        """Return (cat, conf_pct, confident) for a weight w (kg); within midpoint ± band -> unknown.
        Two cats only."""
        lo, hi = self.refs[self.lo], self.refs[self.hi]
        mid = (lo + hi) / 2.0
        dt, ds = w - lo, w - hi
        pt = math.exp(-(dt * dt) / (2 * CAT_SIGMA * CAT_SIGMA))
        ps = math.exp(-(ds * ds) / (2 * CAT_SIGMA * CAT_SIGMA))
        total = pt + ps
        if total <= 0:
            nearer, conf = (self.lo if w <= mid else self.hi), 50
        else:
            nearer = self.lo if pt >= ps else self.hi
            conf = int(round(100 * max(pt, ps) / total))
        if self.degraded or abs(w - mid) <= band + 1e-9:
            return "unknown", conf, False
        return nearer, conf, True

    # ---- samples / medians / trend ------------------------------------------------------------
    def _add_sample(self, cat, w, t):
        s = {"t": t, "w": w}
        self.samples[cat].append(s)
        self._prune(cat)
        return s

    def _prune(self, cat):
        cutoff = time.time() - CAT_WINDOW_DAYS * 86400
        kept = [s for s in self.samples[cat] if s["t"] >= cutoff]
        self.samples[cat] = kept[-CAT_WINDOW_MAX:]

    def _median(self, cat):
        ws = [s["w"] for s in self.samples[cat]]
        return round(statistics.median(ws), 3) if ws else round(self.refs[cat], 3)

    def _trend(self, cat):
        """7-day change in grams: median of the last 7 days minus the 7 days before. 0 if too little data."""
        now = time.time()
        recent = [s["w"] for s in self.samples[cat] if now - s["t"] <= 7 * 86400]
        older = [s["w"] for s in self.samples[cat] if 7 * 86400 < now - s["t"] <= 14 * 86400]
        if recent and older:
            return int(round((statistics.median(recent) - statistics.median(older)) * 1000))
        return 0

    @staticmethod
    def _trend_word(grams):
        if grams > CAT_TREND_DEADBAND_G:
            return "gaining"
        if grams < -CAT_TREND_DEADBAND_G:
            return "losing"
        return "steady"

    # ---- reference updates (re-fit + manual anchor) -------------------------------------------
    def _refit(self):
        """Re-fit each reference to its cat's confident-visit median, keeping the gap floor. If the fit
        would put them within CAT_GAP_FLOOR (or cross), flag degraded and keep the prior references
        (never cross the cats). Order-independent: a pure function of the current sample sets.
        Two cats only; with one cat the reference changes only by set_ref."""
        if not self.pair:
            return
        lo, hi = self.lo, self.hi
        nt = statistics.median([s["w"] for s in self.samples[lo]]) if self.samples[lo] else self.refs[lo]
        ns = statistics.median([s["w"] for s in self.samples[hi]]) if self.samples[hi] else self.refs[hi]
        if ns - nt >= CAT_GAP_FLOOR:
            self.refs[lo], self.refs[hi] = round(nt, 3), round(ns, 3)
            self.degraded = False
        else:
            self.degraded = True

    def _anchor_ref(self, cat, value):
        """Manual input: set one reference directly and push the other away to keep the gap floor (never
        cross). Clears degraded (a manual weigh-in / correction is ground truth that separates the cats)."""
        self.refs[cat] = round(float(value), 3)
        if not self.pair:
            return
        lo, hi = self.lo, self.hi
        if cat == lo and self.refs[hi] < self.refs[lo] + CAT_GAP_FLOOR:
            self.refs[hi] = round(self.refs[lo] + CAT_GAP_FLOOR, 3)
        if cat == hi and self.refs[lo] > self.refs[hi] - CAT_GAP_FLOOR:
            self.refs[lo] = round(self.refs[hi] - CAT_GAP_FLOOR, 3)
        self.degraded = (self.refs[hi] - self.refs[lo]) < CAT_GAP_FLOOR

    # ---- the per-visit entry point ------------------------------------------------------------
    def on_visit(self, w, record, now=None, settled=True):
        """Finalize a completed visit's cat label. `record` is the recent_visits row, mutated in place to
        carry 'cat' + 'conf'. `settled` is False when w is the box max-hold peak (too few cat-on-scale
        heartbeats). Returns the state fields to publish. `now` is injectable for offline tests."""
        now = time.time() if now is None else now
        self.total_visits += 1
        if not self.pair:
            cat, conf, confident = self.lo, CAT_SINGLE_CONF, True
        else:
            band = CAT_UNKNOWN_BAND * (1 if settled else CAT_UNSETTLED_BAND_FACTOR)
            cat, conf, confident = self._classify(w, band)
        record["cat"] = cat
        record["conf"] = conf
        sample = None
        if confident:
            self.labelled_visits += 1
            if settled:
                sample = self._add_sample(cat, w, now)
                self._refit()
        self._last = {"at": now, "cat": cat, "w": w, "sample": sample, "record": record, "settled": settled}
        return self._fields(last_cat=cat, last_conf=conf)

    def _relabel(self, prev, target):
        """Move a previous visit to `target` (manual correction). Its sample moves with it; a visit that
        had none gains one now, unless its weight was an unsettled box peak. Re-fits afterwards."""
        old = prev["cat"]
        if prev["sample"] is not None and old in self.samples and prev["sample"] in self.samples[old]:
            self.samples[old].remove(prev["sample"])
        if old not in self.CATS:
            self.labelled_visits += 1
        t = prev["sample"]["t"] if prev["sample"] else time.time()
        prev["sample"] = self._add_sample(target, prev["w"], t) if prev.get("settled", True) else None
        prev["cat"], prev["conf"] = target, CAT_JOINT_CONF
        if prev.get("record") is not None:
            prev["record"]["cat"], prev["record"]["conf"] = target, CAT_JOINT_CONF
        self._refit()

    # ---- manual commands (gateway-internal; never reach the box) ------------------------------
    def correct_last(self, cat):
        """correct_last <slug>: relabel the most recent visit, add it as a confident sample for that cat,
        re-fit that reference to the cat's median (manual input), bump correction_count.
        Returns None (not applied) with one cat, for an unknown slug, or before the first visit."""
        if not self.pair or cat not in self.CATS or self._last is None:
            return None
        self._relabel(self._last, cat)
        self.correction_count += 1
        if self.samples[cat]:
            self._anchor_ref(cat, statistics.median([s["w"] for s in self.samples[cat]]))
        return self._fields(last_cat=self._last["cat"], last_conf=self._last["conf"])

    def set_ref(self, cat, payload):
        """set_ref_<slug>: a manual scale weigh-in (kg) sets that reference directly, keeping the 150 g gap
        floor by pushing the other reference away if needed."""
        if cat not in self.CATS:
            return None
        try:
            v = round(float(payload), 3)
        except (TypeError, ValueError):
            return None
        if not SET_REF_MIN_KG <= v <= SET_REF_MAX_KG:   # out of range is ignored
            return None
        self._anchor_ref(cat, v)
        return self._fields()

    # ---- published fields ---------------------------------------------------------------------
    def _fields(self, last_cat=None, last_conf=None):
        fields = {}
        medians = {}
        for slug in self.CATS:
            weight = self._median(slug)
            trend = self._trend(slug)
            medians[slug] = weight
            fields[slug + "_weight"] = weight
            fields[slug + "_trend_7d"] = trend
            fields[slug + "_trend_word"] = self._trend_word(trend)
        if self.pair:
            fields["cat_gap"] = int(round((medians[self.hi] - medians[self.lo]) * 1000))
        fields["labelled_pct"] = (int(round(100 * self.labelled_visits / self.total_visits))
                                  if self.total_visits else 0)
        fields["correction_count"] = self.correction_count
        if self.pair:
            fields["cat_degraded"] = bool(self.degraded)
        if last_cat is not None:
            name = "Unknown" if last_cat == "unknown" else display_name(last_cat)
            fields["last_cat"] = last_cat
            fields["last_conf"] = last_conf
            fields["last_visit_cat"] = "%s %d%%" % (name, last_conf)
        return fields

    # ---- persistence --------------------------------------------------------------------------
    def _load(self):
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                refs = data.get("refs", {})
                if not isinstance(refs, dict) or set(refs) != set(self.CATS):
                    # Saved for a different CATLINK_CATS: ignore it; the next save overwrites it.
                    log("cats_state_mismatch", configured=list(self.CATS),
                        saved=sorted(refs) if isinstance(refs, dict) else None)
                    return
                for c in self.CATS:
                    if isinstance(refs.get(c), (int, float)):
                        self.refs[c] = float(refs[c])
                samples = data.get("samples", {})
                for c in self.CATS:
                    rows = samples.get(c, []) if isinstance(samples, dict) else []
                    if isinstance(rows, list):
                        self.samples[c] = [{"t": float(x["t"]), "w": float(x["w"])}
                                           for x in rows if isinstance(x, dict) and "t" in x and "w" in x]
                self.correction_count = int(data.get("correction_count", 0))
                self.total_visits = int(data.get("total_visits", 0))
                self.labelled_visits = int(data.get("labelled_visits", 0))
                self.degraded = bool(data.get("degraded", False)) and self.pair
        except (OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            for c in self.CATS:
                self._prune(c)

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"refs": self.refs, "samples": self.samples,
                           "correction_count": self.correction_count, "total_visits": self.total_visits,
                           "labelled_visits": self.labelled_visits, "degraded": self.degraded},
                          fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError as exc:
            log("cats_persist_failed", error=type(exc).__name__)
