"""Unit tests of the cat tracker (catlink_local/cats.py): synthetic visits only, `now` always injected.

The gateway pipeline around it (segments, pairing, persistence, HA fields) is tested in test_pipeline.py."""
import json
import math
import os
import random
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import time  # noqa: E402

from catlink_local import cats as T  # noqa: E402
from catlink_local import config  # noqa: E402


def W_OK(w):
    return T.W_MIN <= w <= T.W_MAX

LOGS = []
T.log = lambda ev, **k: LOGS.append((ev, k))   # the tracker looks its log() up at call time
DAY = 86400.0
T0 = 1.8e9
CATS = ("cat_a", "cat_b")
SEEDS = {"cat_a": 2.00, "cat_b": 2.37}


def synthetic_v1():
    """A cats.json of catlink-local 0.1 (the old median tracker): refs + confident samples over ~3 days, a lighter
    cat seen 4 times and a heavier one 10 times, one correction. Synthetic values and times."""
    a = [2.09, 2.06, 2.13, 2.10]
    b = [2.33, 2.32, 2.34, 2.37, 2.40, 2.37, 2.35, 2.35, 2.31, 2.36]
    start = T0 - 3 * DAY
    return {"refs": {"cat_a": 2.085, "cat_b": 2.37},
            "samples": {"cat_a": [{"t": start + i * 0.9 * DAY, "w": w} for i, w in enumerate(a)],
                        "cat_b": [{"t": start + i * 0.3 * DAY + 3600, "w": w} for i, w in enumerate(b)]},
            "correction_count": 1, "total_visits": 25, "labelled_visits": 20, "degraded": False}


def write_v1(path, data=None):
    with open(path, "w") as fh:
        json.dump(data or synthetic_v1(), fh)


def obs(t, *segs, peak=None, **kw):
    """segs: (w, n) or (w, n, peak_delta)."""
    out = []
    for s in segs:
        d = {"w": s[0], "n": s[1], "spread": 0.0 if s[1] == 1 else 0.02, "peak_delta": None}
        if len(s) > 2:
            d["peak_delta"] = s[2]
        out.append(d)
    o = {"t": t, "duration_s": 60, "source": "box", "segments": out, "peak": peak, "baseline_unc": 0.0,
         "clamped": False, "orphan": False, "during_clean": False}
    o.update(kw)
    return o


def established(days=6, cat_a=2.10, cat_b=2.36, seed=1, path=None, g_cat_a=0.0, confirm=True):
    """A two-cat tracker fed `days` of clean visits (Cat a 2/day, Cat b 6/day, sd 20 g)."""
    rng = random.Random(seed)
    tr = T.CatTracker(CATS, SEEDS, path, now=T0)
    t = T0
    ev = []
    for d in range(days):
        for k in range(2):
            ev.append((T0 + (d + 0.1 + 0.45 * k) * DAY, "cat_a"))
        for k in range(6):
            ev.append((T0 + (d + 0.05 + 0.15 * k) * DAY, "cat_b"))
    ev.sort()
    for t, c in ev:
        base = cat_a + g_cat_a * (t - T0) / DAY if c == "cat_a" else cat_b
        w = round(base + rng.gauss(0, 0.02), 2)
        tr.on_visit(obs(t, (w, 4)), {}, now=t)
    if confirm:
        # an upgrade migrates an identity the owner already has; a fresh install's seed-order
        # bootstrap stays "in doubt" until a weigh-in session (tested separately)
        for c in CATS:
            tr.tracks[c]["doubt"] = False
    return tr, ev[-1][0]


class Contract(unittest.TestCase):
    def test_fields_and_record(self):
        tr, t = established()
        rec = {}
        f = tr.on_visit(obs(t + 600, (2.36, 5)), rec, now=t + 600)
        for c in CATS:
            for k in ("weight", "weight_unc_g", "growth_g_week", "trend_word", "last_seen", "stale",
                      "visits_total", "visits_24h"):
                self.assertIn("%s_%s" % (c, k), f)
        for k in ("cat_status", "cat_gap", "labelled_pct_7d", "unknown_visits_total", "two_cat_visits_total",
                  "correction_count", "last_cat", "last_conf", "last_visit_cat"):
            self.assertIn(k, f)
        self.assertEqual(rec["cat"], "cat_b")
        self.assertIsInstance(rec["conf"], int)
        self.assertLessEqual(rec["conf"], 99)
        self.assertEqual(f["last_visit_cat"], "Cat b %d%%" % rec["conf"])
        self.assertEqual(f["cat_status"], "ok")
        self.assertIsInstance(f["cat_gap"], int)
        self.assertTrue(f["cat_b_last_seen"].startswith("20"))
        self.assertRegex(f["cat_b_last_seen"], r"[+-]\d\d:\d\d$")
        lo, hi = tr.plausible_range(t)
        self.assertLess(lo, 2.10)
        self.assertGreater(hi, 2.36)
        self.assertLess(hi, 2.10 + 2.36)

    def test_unknown_has_no_conf(self):
        tr, t = established()
        rec = {}
        tr.on_visit(obs(t + 600), rec, now=t + 600)          # no segment at all
        self.assertEqual(rec["cat"], "unknown")
        self.assertIsNone(rec["conf"])
        self.assertEqual(tr.fields(t + 600)["last_visit_cat"], "Unknown")

    def test_orphan_and_during_clean(self):
        tr, t = established()
        for kw in ({"orphan": True}, {"during_clean": True}):
            rec = {}
            n = len(tr.samples["cat_b"])
            tr.on_visit(obs(t + 600, (2.36, 5), **kw), rec, now=t + 600)
            self.assertEqual(rec["cat"], "unknown")
            self.assertEqual(len(tr.samples["cat_b"]), n)
            t += 1000

    def test_deterministic(self):
        a, t = established(seed=3)
        b, _ = established(seed=3)
        self.assertEqual(a.fields(t + 5), b.fields(t + 5))

    def test_no_wall_clock(self):
        tr, t = established()
        f1 = tr.fields(t + 3600)
        f2 = tr.fields(t + 3600)
        self.assertEqual(f1, f2)                              # pure function of state + now
        self.assertFalse(tr.fields(t + 3600)["cat_b_stale"])   # t is far in the future of time.time()

    def test_save_load_v2_roundtrip(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            tr, t = established(path=p)
            tr.save()
            self.assertFalse(os.path.exists(p))                  # rollback-safe: cats.json is never written
            with open(T.v2_path_for(p)) as fh:
                data = json.load(fh)
            self.assertEqual(data["version"], 2)
            tr2 = T.CatTracker(CATS, SEEDS, p, now=t)
            self.assertEqual(tr.fields(t + 10), tr2.fields(t + 10))
            self.assertFalse(os.path.exists(T.v2_path_for(p) + ".tmp"))
        finally:
            shutil.rmtree(d)

    def test_corrupt_file_is_moved_aside_and_logged(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            with open(T.v2_path_for(p), "w") as fh:
                fh.write("{not json")
            LOGS.clear()
            tr = T.CatTracker(CATS, SEEDS, p, now=T0)
            self.assertTrue(any(f.startswith("cats_v2.json.corrupt-") for f in os.listdir(d)))
            self.assertTrue(any(ev == "cats_state_unreadable" for ev, _ in LOGS))
            f = tr.fields(T0)
            self.assertIsNone(f["cat_a_weight"])                # never publishes a seed as a weight
            self.assertEqual(f["cat_status"], "learning")
        finally:
            shutil.rmtree(d)


class Migration(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "cats.json")
        self.v1 = synthetic_v1()
        write_v1(self.p, self.v1)
        self.now = max(s["t"] for c in self.v1["samples"].values() for s in c) + 3600

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_migrates_v1(self):
        tr = T.CatTracker(CATS, SEEDS, self.p, now=self.now)
        f = tr.fields(self.now)
        self.assertEqual(f["cat_status"], "ok")
        self.assertAlmostEqual(f["cat_a_weight"], 2.085, delta=0.03)
        self.assertAlmostEqual(f["cat_b_weight"], 2.35, delta=0.02)
        self.assertEqual(f["correction_count"], self.v1["correction_count"])
        self.assertEqual(len(tr.samples["cat_b"]), len(self.v1["samples"]["cat_b"]))
        self.assertEqual(tr.v1["total_visits"], 25)
        before = open(self.p).read()
        tr.save()
        self.assertEqual(open(self.p).read(), before)            # the v1 file is read, never written
        with open(T.v2_path_for(self.p)) as fh:
            self.assertEqual(json.load(fh)["version"], 2)
        tr2 = T.CatTracker(CATS, SEEDS, self.p, now=self.now)
        self.assertEqual(tr2.fields(self.now)["cat_b_weight"], f["cat_b_weight"])
        # and a single live reading (2.31, the box report 2.33) is Cat b
        rec = {}
        tr2.on_visit(obs(self.now + 600, (2.31, 1, -0.02)), rec, now=self.now + 600)
        self.assertEqual(rec["cat"], "cat_b")

    def test_v1_for_other_cats_is_ignored(self):
        tr = T.CatTracker(("a", "b"), {"a": 2.0, "b": 2.4}, self.p, now=self.now)
        self.assertEqual(tr.fields(self.now)["cat_status"], "learning")
        self.assertTrue(any(ev == "cats_state_mismatch" for ev, _ in LOGS))


class WeighIns(unittest.TestCase):
    def test_float_edges_never_disable_labelling(self):
        # 2.40 - 2.25 = 0.1499999... latched "degraded" in the old tracker
        for cat, kg in (("cat_a", 2.25), ("cat_b", 2.40), ("cat_a", 2.20), ("cat_a", 2.21), ("cat_b", 2.25)):
            tr, t = established()
            f = tr.set_ref(cat, kg, now=t + 60)
            self.assertIsNotNone(f)
            labs = []
            for k in range(12):
                tt = t + 3600 * (k + 1)
                rec = {}
                tr.on_visit(obs(tt, ((2.10, 4) if k % 3 == 0 else (2.36, 4))), rec, now=tt)
                labs.append(rec["cat"])
            self.assertGreaterEqual(sum(l != "unknown" for l in labs), 9, (cat, kg, labs))
            self.assertNotIn("cat_a", [l for l, k in zip(labs, range(12)) if k % 3 != 0], (cat, kg))
            # labelling is never disabled; a weigh-in >= 100 g off the box track keeps the status at
            # needs_weigh_in for a week (the published interval must contain a recent weigh-in)
            want = "ok" if (cat, kg) == ("cat_b", 2.40) else "needs_weigh_in"
            self.assertEqual(tr.fields(t + 13 * 3600)["cat_status"], want, (cat, kg))

    def test_consistent_weigh_in_is_accepted(self):
        tr, t = established()
        before = tr.fields(t)["cat_a_weight"]
        tr.set_ref("cat_a", 2.08, now=t + 60)
        self.assertEqual(tr.weighins[-1]["action"], "accepted")
        self.assertAlmostEqual(tr.fields(t + 60)["cat_a_weight"], 2.08, delta=0.03)
        self.assertNotEqual(before, None)

    def test_wildly_wrong_weigh_in_is_rejected_not_fatal(self):
        tr, t = established()
        tr.set_ref("cat_a", 2.80, now=t + 60)
        self.assertEqual(tr.weighins[-1]["action"], "rejected")
        rec = {}
        tr.on_visit(obs(t + 3600, (2.10, 4)), rec, now=t + 3600)
        self.assertEqual(rec["cat"], "cat_a")

    def test_bad_payloads(self):
        tr, t = established()
        for bad in ("x", None, float("nan"), 0.1, 50, "inf"):
            self.assertIsNone(tr.set_ref("cat_a", bad, now=t))
        self.assertIsNone(tr.set_ref("mochi", 2.0, now=t))
        self.assertIsNotNone(tr.set_ref("cat_a", "2.11", now=t))

    def test_crossing_then_weigh_in_restores_identity(self):
        import sim
        tt, ts = sim.lin(2.11, 200), (lambda d: 2.37)
        r = sim.run({"cat_a": tt, "cat_b": ts}, 30, {"cat_a": 2.0, "cat_b": 7.0}, 2003, seeds=SEEDS,
                    weighins=[(21.0, "cat_a", round(tt(21.0), 2)), (21.001, "cat_b", 2.37)])
        before = sim.label_stats(r["nlog"], 10, 21)
        after = sim.label_stats(r["nlog"], 22, 30)
        self.assertLessEqual(before["wrong_pct"], 3.0)                # converged: unknown, not wrong
        self.assertGreaterEqual(after["labelled"], 0.6 * after["n"])  # identity restored ...
        self.assertLessEqual(after["wrong_pct"], 3.0)                 # ... correctly
        self.assertEqual(r["snaps"][24]["status"], "ok")


class Staleness(unittest.TestCase):
    def test_stale_on_read(self):
        tr, t = established()
        f = tr.fields(t + 3600)
        self.assertFalse(f["cat_a_stale"])
        self.assertIsNotNone(f["cat_a_weight"])
        self.assertGreater(f["cat_b_visits_24h"], 0)
        g = tr.fields(t + 73 * 3600)
        self.assertTrue(g["cat_a_stale"] and g["cat_b_stale"])
        self.assertIsNone(g["cat_a_weight"])
        self.assertEqual(g["cat_b_visits_24h"], 0)
        self.assertIsNone(g["cat_gap"])
        self.assertEqual(g["cat_b_trend_word"], "not enough data")   # pruned on read: 14 d window
        self.assertEqual(tr.fields(t + 3600), f)                      # reading does not mutate


class Trend(unittest.TestCase):
    def test_not_enough_data(self):
        tr, t = established(days=4)                      # spans < 5 days
        f = tr.fields(t)
        self.assertIsNone(f["cat_a_growth_g_week"])
        self.assertEqual(f["cat_a_trend_word"], "not enough data")

    def test_flat_is_steady_and_growth_is_gaining(self):
        tr, t = established(days=12)
        f = tr.fields(t)
        self.assertEqual(f["cat_b_trend_word"], "steady")
        tr2, t2 = established(days=12, cat_a=1.85, g_cat_a=0.025)     # +175 g/week, stays clear of Cat b
        f2 = tr2.fields(t2)
        self.assertEqual(f2["cat_a_trend_word"], "gaining")
        self.assertAlmostEqual(f2["cat_a_growth_g_week"], 175, delta=60)


class Segments(unittest.TestCase):
    def test_two_segment_visit(self):
        tr, t = established()
        rec = {}
        f = tr.on_visit(obs(t + 600, (2.34, 3), (2.12, 7), peak=4.49), rec, now=t + 600)   # two cats, one occupancy
        self.assertEqual(rec["cat"], "two_cats")
        self.assertEqual(rec["cats"], ["cat_b", "cat_a"])
        self.assertEqual(f["two_cat_visits_total"], 1)
        self.assertEqual(f["last_visit_cat"], "Cat b + Cat a")

    def test_two_segments_one_named_cat_is_one_visit(self):
        tr, t = established()
        rec = {}
        n_before = tr.visits_total["cat_b"]
        tr.on_visit(obs(t + 600, (2.36, 5), (2.80, 2)), rec, now=t + 600)
        self.assertEqual(rec["cat"], "cat_b")
        self.assertEqual(tr.visits_total["cat_b"], n_before + 1)
        self.assertEqual(tr.two_cat_visits_total, 0)
        # owner says both plateaus were Cat b: still one visit, one sample, from the larger plateau
        f = tr.correct_last("cat_b", now=t + 700)
        self.assertEqual(f["two_cat_visits_total"], 0)
        self.assertEqual([s[1] for s in tr.samples["cat_b"] if s[0] == t + 600], [2.36])

    def test_single_sample_admission(self):
        tr, t = established()
        n = len(tr.samples["cat_b"])
        rec = {}
        tr.on_visit(obs(t + 600, (2.35, 1, 0.02)), rec, now=t + 600)          # box agrees
        self.assertEqual(rec["cat"], "cat_b")
        self.assertEqual(len(tr.samples["cat_b"]), n + 1)
        w = tr.tracks["cat_b"]["w"]
        rec = {}
        tr.on_visit(obs(t + 1200, (2.32, 1, 0.91)), rec, now=t + 1200)        # the box said 1.41
        self.assertEqual(len(tr.samples["cat_b"]), n + 1)                      # never a sample ...
        self.assertEqual(tr.tracks["cat_b"]["w"], w)                           # ... nor an update
        self.assertIn(rec["cat"], ("cat_b", "unknown"))
        rec = {}
        tr.on_visit(obs(t + 1800, (2.36, 1)), rec, now=t + 1800)              # no box report
        self.assertEqual(len(tr.samples["cat_b"]), n + 1)

    def test_not_one_cat_outliers(self):
        tr, t = established()
        for k, w in enumerate((1.40, 2.75, 3.20, 0.94, 2.62)):
            rec = {}
            n = {c: len(tr.samples[c]) for c in CATS}
            tr.on_visit(obs(t + 600 * (k + 1), (w, 3)), rec, now=t + 600 * (k + 1))
            self.assertEqual(rec["cat"], "unknown", w)
            self.assertEqual({c: len(tr.samples[c]) for c in CATS}, n, w)

    def test_baseline_unc_and_clamp_only_widen_a_little(self):
        tr, t = established()
        rec = {}
        tr.on_visit(obs(t + 600, (2.34, 4), baseline_unc=0.03, clamped=True), rec, now=t + 600)
        self.assertEqual(rec["cat"], "cat_b")


class CorrectLast(unittest.TestCase):
    def test_correct_relabel_counts_and_persists(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            tr, t = established(path=p)
            rec = {}
            tr.on_visit(obs(t + 600, (2.23, 4)), rec, now=t + 600)
            tr.save()
            tr2 = T.CatTracker(CATS, SEEDS, p, now=t + 700)            # survives a restart
            before = dict(tr2.visits_total)
            f = tr2.correct_last("cat_a", now=t + 700)
            self.assertIsNotNone(f)
            self.assertEqual(f["last_cat"], "cat_a")
            self.assertEqual(f["correction_count"], 1)
            self.assertGreaterEqual(tr2.visits_total["cat_a"], before["cat_a"])
            self.assertGreaterEqual(tr2.visits_total["cat_b"], before["cat_b"])     # never decreases
            self.assertIn(2.23, [s[1] for s in tr2.samples["cat_a"]])
            f = tr2.correct_last("unknown", now=t + 800)
            self.assertEqual(f["last_cat"], "unknown")
            self.assertNotIn(2.23, [s[1] for s in tr2.samples["cat_a"]])
            self.assertIsNone(tr2.correct_last("mochi", now=t + 800))
        finally:
            shutil.rmtree(d)

    def test_correct_mutates_in_memory_record(self):
        tr, t = established()
        rec = {}
        tr.on_visit(obs(t + 600, (2.36, 5)), rec, now=t + 600)
        tr.correct_last("cat_a", now=t + 700)
        self.assertEqual(rec["cat"], "cat_a")
        self.assertEqual(rec["conf"], 100)

    def test_late_visit_is_counted_but_not_last(self):
        tr, t = established()
        rec_a, rec_b = {}, {}
        tr.on_visit(obs(t + 600, (2.36, 5)), rec_a, now=t + 600)
        total = tr.visits_total["cat_a"]
        tr.on_visit(obs(t + 450, (2.10, 4), source="gateway"), rec_b, now=t + 650)   # finalized late
        self.assertEqual(rec_b["cat"], "cat_a")
        self.assertEqual(tr.visits_total["cat_a"], total + 1)
        self.assertEqual(tr.fields(t + 650)["last_cat"], "cat_b")
        tr.correct_last("cat_a", now=t + 700)                     # targets the newest visit (t+600)
        self.assertEqual(rec_a["cat"], "cat_a")
        self.assertEqual(rec_b["cat"], "cat_a")

    def test_newest_flag_and_baseline_alt(self):
        tr, t = established()
        rec_a, rec_b = {}, {}
        tr.on_visit(obs(t + 600, (2.36, 5), newest=True), rec_a, now=t + 600)
        n_cat_a = len(tr.samples["cat_a"])
        w = tr.tracks["cat_a"]["w"]
        tr.on_visit(obs(t + 700, (2.10, 4), newest=False, baseline_alt=True), rec_b, now=t + 700)
        self.assertEqual(rec_b["cat"], "cat_a")                      # labelled and counted ...
        self.assertEqual(tr.fields(t + 700)["cat_a_visits_24h"], tr.fields(t + 599)["cat_a_visits_24h"] + 1)
        self.assertEqual(len(tr.samples["cat_a"]), n_cat_a)           # ... never a weight sample
        self.assertEqual(tr.tracks["cat_a"]["w"], w)
        self.assertEqual(tr.fields(t + 700)["last_cat"], "cat_b")    # not "last"
        tr.correct_last("cat_a", now=t + 800)
        self.assertEqual(rec_a["cat"], "cat_a")

    def test_none_before_first_visit(self):
        tr = T.CatTracker(CATS, SEEDS, None, now=T0)
        self.assertIsNone(tr.correct_last("cat_a", now=T0))


class NeverStuck(unittest.TestCase):
    def test_stuck_track_is_repaired_by_label_free_net(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            t0 = T0 - 3 * DAY
            json.dump({"refs": {"cat_a": 1.90, "cat_b": 2.37},
                       "samples": {"cat_a": [{"t": t0 + i * 25000, "w": 1.90} for i in range(10)],
                                   "cat_b": [{"t": t0 + i * 25000, "w": 2.37} for i in range(10)]},
                       "correction_count": 0, "total_visits": 20, "labelled_visits": 20, "degraded": False},
                      open(p, "w"))
            tr = T.CatTracker(CATS, SEEDS, p, now=T0)
            rng = random.Random(5)
            t = T0
            for k in range(40):                                   # 5 days: Cat a really 2.11
                t += 0.125 * DAY
                c = "cat_a" if k % 4 == 0 else "cat_b"
                tr.on_visit(obs(t, (round((2.11 if c == "cat_a" else 2.37) + rng.gauss(0, 0.02), 2), 4)), {}, now=t)
            self.assertAlmostEqual(tr.fields(t)["cat_a_weight"], 2.11, delta=0.04)
            self.assertGreaterEqual(tr.repairs_total, 1)
        finally:
            shutil.rmtree(d)


class OneCat(unittest.TestCase):
    def test_one_cat_rough_seed_grows(self):
        tr = T.CatTracker(("solo",), {"solo": 2.00}, None, now=T0)
        rng = random.Random(2)
        t = T0
        labs = []
        for k in range(4 * 42):                                   # 6 weeks, 4/day, +150 g/week
            t += DAY / 4
            true = 2.10 + 0.15 * (t - T0) / DAY / 7
            rec = {}
            tr.on_visit(obs(t, (round(true + rng.gauss(0, 0.025), 2), 4)), rec, now=t)
            labs.append(rec["cat"])
        f = tr.fields(t)
        self.assertAlmostEqual(f["solo_weight"], true, delta=0.06)
        self.assertEqual(f["solo_trend_word"], "gaining")
        self.assertIsNone(f["cat_gap"])
        self.assertEqual(f["cat_status"], "ok")
        self.assertGreater(labs.count("solo"), 0.9 * len(labs))
        self.assertGreater(tr.plausible_range(t)[1], true + 0.25)

    def test_one_cat_far_seed_bootstraps(self):
        tr = T.CatTracker(("solo",), [3.00], None, now=T0)
        t = T0
        for k in range(12):
            t += DAY / 4
            tr.on_visit(obs(t, (2.10 + 0.01 * (k % 3), 4)), {}, now=t)
        self.assertAlmostEqual(tr.fields(t)["solo_weight"], 2.11, delta=0.04)


class ThreeCats(unittest.TestCase):
    def test_three_cats(self):
        cats = ("mochi", "cat_a", "cat_b")
        tr = T.CatTracker(cats, [2.0, 2.6, 3.3], None, now=T0)
        rng = random.Random(4)
        truth = {"mochi": 2.0, "cat_a": 2.6, "cat_b": 3.3}
        t = T0
        ok = n = 0
        for k in range(150):
            t += DAY / 12
            c = cats[k % 3]
            rec = {}
            tr.on_visit(obs(t, (round(truth[c] + rng.gauss(0, 0.025), 2), 4)), rec, now=t)
            if k > 30:
                n += 1
                ok += rec["cat"] == c
        self.assertGreater(ok, 0.9 * n)
        f = tr.fields(t)
        self.assertEqual(f["cat_status"], "ok")
        self.assertAlmostEqual(f["cat_gap"], 600, delta=40)
        rec = {}
        tr.on_visit(obs(t + 60, (2.0, 3), (3.3, 3)), rec, now=t + 60)
        self.assertEqual(rec["cats"], ["mochi", "cat_b"])

    def test_bad_construction(self):
        with self.assertRaises(ValueError):
            T.CatTracker(("a", "a"), [2, 2], None, now=T0)
        with self.assertRaises(ValueError):
            T.CatTracker((), [], None, now=T0)
        with self.assertRaises(ValueError):
            T.CatTracker(("a", "b"), [2.0], None, now=T0)


class Robustness(unittest.TestCase):
    """Restart, migration, bad state, weigh-in sessions, crossings."""

    def test_new_fields(self):
        tr, t = established()
        f = tr.fields(t)
        for k in ("visits_24h_total", "cluster_low_kg", "cluster_high_kg", "cluster_low_n", "cluster_high_n",
                  "cat_a_stale_reason", "cat_b_stale_reason", "cat_a_last_weigh_in", "cat_b_last_weigh_in"):
            self.assertIn(k, f)
        self.assertGreaterEqual(f["visits_24h_total"], f["cat_a_visits_24h"] + f["cat_b_visits_24h"])
        self.assertIsNone(f["cat_a_stale_reason"])
        g = tr.fields(t + 80 * 3600)
        self.assertEqual(g["cat_a_stale_reason"], "not_seen")
        tr.set_ref("cat_a", 2.10, now=t + 60)
        self.assertTrue(tr.fields(t + 60)["cat_a_last_weigh_in"].startswith("accepted 2.10 kg"))
        self.assertAlmostEqual(f["cluster_low_kg"], 2.10, delta=0.04)
        self.assertAlmostEqual(f["cluster_high_kg"], 2.36, delta=0.04)

    def test_baseline_alt_is_a_value_and_may_be_zero(self):
        tr, t = established()
        n = len(tr.samples["cat_b"])
        rec = {}
        tr.on_visit(obs(t + 600, (2.35, 4), pre=0.14, baseline_alt=0.0), rec, now=t + 600)   # 2.35 or 2.49
        self.assertEqual(rec["cat"], "unknown")
        self.assertTrue(rec.get("baseline_ambiguous"))
        rec = {}
        tr.on_visit(obs(t + 1200, (2.36, 4), pre=0.02, baseline_alt=0.0), rec, now=t + 1200)  # 2.36 or 2.38
        self.assertEqual(rec["cat"], "cat_b")
        self.assertEqual(len(tr.samples["cat_b"]), n)                 # never a sample

    def test_newest_false_with_no_last_does_not_crash(self):
        tr = T.CatTracker(CATS, SEEDS, None, now=T0)
        rec = {}
        tr.on_visit(obs(T0 + 60, (2.36, 4), newest=False), rec, now=T0 + 60)
        self.assertIn(rec["cat"], ("cat_b", "unknown"))
        self.assertIsNone(tr.last)

    def test_attach_last_record_after_restart(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            tr, t = established(path=p)
            row = {"ts": "2027-01-15T12:00:00+00:00"}
            tr.on_visit(obs(t + 600, (2.36, 4)), row, now=t + 600)
            tr.save()
            tr2 = T.CatTracker(CATS, SEEDS, p, now=t + 700)
            reloaded = dict(row)
            tr2.attach_last_record(reloaded)
            tr2.correct_last("cat_a", now=t + 800)
            self.assertEqual(reloaded["cat"], "cat_a")               # the dashboard row is relabelled too
            # migrated state without "last": seeded from the row, relabel-only
            tr3 = T.CatTracker(CATS, SEEDS, None, now=t)
            row3 = {"ts": "2027-01-15T12:00:00+00:00", "cat": "unknown", "weight": 2.31, "n": 1}
            tr3.attach_last_record(row3)
            self.assertIsNotNone(tr3.correct_last("cat_b", now=t + 900))
            self.assertEqual(row3["cat"], "cat_b")
        finally:
            shutil.rmtree(d)

    def test_never_writes_v1_and_reads_shim_counters(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            v1 = synthetic_v1()
            v1.update(shim=1, visits_total={"cat_a": 7, "cat_b": 33}, unknown_visits_total=9, two_cat_visits_total=1,
                      visit_log=[{"t": 1799996400.0, "cats": ["cat_a"]}],
                      last={"t": 1799996400.0, "cat": "cat_a", "conf": 97, "cats": None, "w": 2.1})
            with open(p, "w") as fh:
                json.dump(v1, fh)
            before = open(p).read()
            tr = T.CatTracker(CATS, SEEDS, p, now=1799996783.0)
            f = tr.fields(1799996783.0)
            self.assertEqual((f["cat_a_visits_total"], f["cat_b_visits_total"]), (7, 33))
            self.assertEqual(f["unknown_visits_total"], 9)
            self.assertEqual(f["last_visit_cat"], "Cat a 97%")
            tr.save()
            self.assertEqual(open(p).read(), before)
            self.assertTrue(os.path.exists(os.path.join(d, "cats_v2.json")))
        finally:
            shutil.rmtree(d)

    def test_migration_growth_from_v1_slope(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            t0 = T0 - 5 * DAY
            json.dump({"refs": {"cat_a": 2.10, "cat_b": 2.37},
                       "samples": {"cat_a": [{"t": t0 + i * DAY / 2, "w": round(2.00 + 0.0125 * i, 3)} for i in range(9)],
                                   "cat_b": [{"t": t0 + i * DAY / 2, "w": 2.35} for i in range(9)]},
                       "correction_count": 0, "total_visits": 18, "labelled_visits": 18, "degraded": False}, open(p, "w"))
            tr = T.CatTracker(CATS, SEEDS, p, now=T0)
            self.assertGreater(tr.tracks["cat_a"]["g"] * 7000, 100)      # +175 g/week in the v1 samples
            self.assertLess(abs(tr.tracks["cat_b"]["g"] * 7000), 40)
        finally:
            shutil.rmtree(d)

    def test_bad_state_entries_are_dropped_not_fatal(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            tr, t = established(path=p)
            tr.save()
            v2 = T.v2_path_for(p)
            data = json.load(open(v2))
            data["tracks"]["cat_a"]["Pww"] = -1
            data["samples"]["cat_b"].append([t, "x", 2])
            data["recent"].append([t, None, 0.1])
            data["last"] = 5
            data["visits"].append("junk")
            json.dump(data, open(v2, "w"))
            tr2 = T.CatTracker(CATS, SEEDS, p, now=t)
            self.assertEqual(tr2.tracks["cat_a"]["ident"], "init")        # that track fell back to its seed
            self.assertEqual(tr2.tracks["cat_b"]["ident"], "ok")          # the rest is kept
            json.dumps(tr2.fields(t + 60), allow_nan=False)
            tr2.on_visit(obs(t + 120, (2.36, 4)), {}, now=t + 120)
        finally:
            shutil.rmtree(d)

    def test_absurd_weight_is_bounded(self):
        tr, t = established()
        start = time.time()
        for k, w in enumerate((1e6, 1e300, -5.0, float("nan"))):
            tr.on_visit(obs(t + 60 * (k + 1), (w, 4)), {}, now=t + 60 * (k + 1))
        tr._lf_check(t + 600, force=True)
        self.assertLess(time.time() - start, 2.0)
        self.assertTrue(all(W_OK(r[1]) for r in tr.recent))

    def test_no_offset_is_learned(self):
        tr, t = established(days=8)
        for k, (c, kg) in enumerate((("cat_a", 2.21), ("cat_b", 2.27), ("cat_a", 2.01), ("cat_b", 2.26))):
            tr.set_ref(c, kg, now=t - (3 - k * 0.5) * DAY)
        self.assertEqual(tr.diagnostics(t)["offset_g"], 0)             # kitchen scale = box scale

    def test_weigh_in_session_settles_identity_only_with_both_cats(self):
        tr, t = established()
        tr.tracks["cat_a"]["doubt"] = tr.tracks["cat_b"]["doubt"] = True   # identity in doubt (e.g. after an overlap)
        tr.set_ref("cat_b", 2.10, now=t + 60)                              # Cat a's value typed for Cat b
        self.assertEqual(tr.weighins[-1]["action"], "pending")
        self.assertTrue(tr.tracks["cat_b"]["doubt"])                        # nothing settled by one entry
        self.assertAlmostEqual(tr.tracks["cat_b"]["w"], 2.36, delta=0.03)   # ... and no track moved
        tr.set_ref("cat_a", 2.10, now=t + 120)                             # same value for both -> rejected
        self.assertEqual(tr.weighins[-1]["action"], "rejected")
        self.assertEqual(tr.weighins[-2]["action"], "rejected")
        tr.set_ref("cat_a", 2.10, now=t + 2 * DAY)                         # a proper session, next time
        tr.set_ref("cat_b", 2.36, now=t + 2 * DAY + 60)
        self.assertEqual(tr.weighins[-1]["detail"], "accepted (session)")
        self.assertFalse(tr.tracks["cat_a"]["doubt"] or tr.tracks["cat_b"]["doubt"])

    def test_unidentified_cat_is_never_ok(self):
        tr, t = established()
        for k in range(6):                                                # 3 days of unknown-only visits
            tr.on_visit(obs(t + (k + 1) * DAY / 2, (2.23, 4)), {}, now=t + (k + 1) * DAY / 2)
        self.assertEqual(tr.status(t + 3.1 * DAY), "needs_weigh_in")

    def test_second_agreeing_weigh_in_overrides_a_rejection(self):
        tr, t = established()
        tr.set_ref("cat_a", 1.85, now=t + 60)
        self.assertEqual(tr.weighins[-1]["action"], "rejected")
        self.assertIn("enter it again changed by 0.01 kg", tr.fields(t + 60)["cat_a_last_weigh_in"])
        tr.set_ref("cat_a", 1.86, now=t + 3600)
        # the box still sees Cat a at 2.10 and nothing near 1.86: a confirmation alone is not enough
        self.assertEqual(tr.weighins[-1]["action"], "rejected")
        self.assertIn("weigh both cats", tr.weighins[-1]["reason"])
        tr.set_ref("cat_b", 2.36, now=t + 3700)
        tr.set_ref("cat_a", 1.85, now=t + 3800)
        self.assertEqual(tr.weighins[-1]["detail"], "accepted (reset)")
        self.assertAlmostEqual(tr.tracks["cat_a"]["w"], 1.85, delta=0.03)
        # ... but while the box's clusters still hold 2.10, the reset weight is not published as "ok"
        self.assertNotEqual(tr.fields(t + 3800)["cat_status"], "ok")

    def test_wrong_order_public_seeds_fixed_by_weigh_in_or_correction(self):
        for how in ("weigh_in", "correct_last"):
            tr = T.CatTracker(("a", "b"), {"a": 2.75, "b": 2.40}, None, now=T0)   # seeds in the wrong order
            rng = random.Random(2)
            t = T0
            for k in range(48):
                t += DAY / 8
                c = "a" if k % 2 else "b"
                tr.on_visit(obs(t, (round((2.40 if c == "a" else 2.75) + rng.gauss(0, 0.02), 2), 4)), {}, now=t)
            if how == "weigh_in":                          # the owner weighs both cats (a session)
                tr.set_ref("a", 2.40, now=t + 60)
                self.assertEqual(tr.weighins[-1]["action"], "pending")       # one entry alone settles nothing
                tr.set_ref("b", 2.75, now=t + 120)
            else:                                          # two corrections pointing the same way
                for k in range(2):
                    tr.on_visit(obs(t + 60 + 600 * k, (2.40, 4)), {}, now=t + 60 + 600 * k)
                    tr.correct_last("a", now=t + 120 + 600 * k)
            labs = []
            for k in range(16):
                t += DAY / 8
                c = "a" if k % 2 else "b"
                rec = {}
                tr.on_visit(obs(t, (round((2.40 if c == "a" else 2.75) + rng.gauss(0, 0.02), 2), 4)), rec, now=t)
                labs.append((c, rec["cat"]))
            right = sum(1 for c, l in labs if c == l)
            self.assertGreaterEqual(right, 13, (how, labs))

    def test_same_cat_rim_then_in_is_one_visit(self):
        tr, t = established()
        wt, vt = tr.tracks["cat_a"]["w"], tr.visits_total["cat_a"]
        for k, (a, b) in enumerate((((2.20, 3), (2.37, 4)), ((2.37, 4), (2.20, 3)), ((2.24, 2), (2.37, 3)))):
            rec = {}
            tr.on_visit(obs(t + 600 * (k + 1), a, b), rec, now=t + 600 * (k + 1))
            self.assertEqual(rec["cat"], "cat_b")
            self.assertTrue(rec.get("segments_merged"))
        self.assertEqual(tr.visits_total["cat_a"], vt)
        self.assertEqual(tr.tracks["cat_a"]["w"], wt)
        self.assertEqual(tr.two_cat_visits_total, 0)

    def _feed(self, tr, t0, days, f_t, f_s, seed=1):
        rng = random.Random(seed)
        ev = sorted([(t0 + (k + rng.random()) * DAY / 2, "cat_a") for k in range(int(days * 2))] +
                    [(t0 + (k + rng.random()) * DAY / 7, "cat_b") for k in range(int(days * 7))])
        out = []
        for t, c in ev:
            d = (t - t0) / DAY
            rec = {}
            tr.on_visit(obs(t, (round((f_t if c == "cat_a" else f_s)(d) + rng.gauss(0, 0.02), 2), 4)), rec, now=t)
            out.append((d, c, rec["cat"]))
        return out, ev[-1][0]

    def test_overlap_is_not_absorbing_after_a_dip(self):
        """Cat b sits 60 g above Cat a for 6 days, then goes back. Since Cat b MOVED during the episode, weight alone
        cannot prove which cat is which: the status asks for a weigh-in instead of a silent blank,
        nothing is labelled wrong, and one weigh-in session brings the labels back."""
        tr, t = established(seed=3)
        def cat_b(d):
            return 2.37 - 0.10 * d if d < 2 else (2.17 if d < 8 else (2.17 + 0.10 * (d - 8) if d < 10 else 2.37))
        labs, t1 = self._feed(tr, t, 20, lambda d: 2.11, cat_b, seed=3)
        late = [x for x in labs if x[0] >= 14]
        self.assertEqual(sum(1 for d, c, l in late if l in CATS and l != c), 0)
        self.assertIn(tr.status(t1), ("ok", "needs_weigh_in"))
        tr.set_ref("cat_a", 2.11, now=t1 + 60)
        tr.set_ref("cat_b", 2.37, now=t1 + 120)
        labs2, t2 = self._feed(tr, t1 + 180, 3, lambda d: 2.11, lambda d: 2.37, seed=4)
        self.assertGreater(sum(1 for d, c, l in labs2 if l == c), 0.75 * len(labs2))
        self.assertEqual(sum(1 for d, c, l in labs2 if l in CATS and l != c), 0)
        self.assertEqual(tr.status(t2), "ok")

    def test_absence_does_not_create_ambiguity(self):
        tr, t = established(seed=5)
        labs, t1 = self._feed(tr, t + 14 * DAY, 3, lambda d: 2.11, lambda d: 2.37, seed=6)
        self.assertGreater(sum(1 for d, c, l in labs if l == c), 0.9 * len(labs))
        self.assertEqual(tr.status(t1), "ok")

    def test_crossing_is_never_a_silent_permanent_blank(self):
        tr, t = established(seed=7)
        labs, t1 = self._feed(tr, t, 40, lambda d: 2.11 + 0.18 * d / 7, lambda d: 2.37 + 0.065 * d / 7, seed=7)
        self.assertIn(tr.status(t1), ("needs_weigh_in", "ok"))
        wrong = sum(1 for d, c, l in labs if l in CATS and l != c)
        self.assertLessEqual(wrong, 0.03 * len(labs))

    def test_step_towards_the_other_cat_gives_no_confident_wrong_streak(self):
        tr, t = established(seed=9)
        labs, t1 = self._feed(tr, t, 14, lambda d: 2.11, lambda d: 2.22, seed=9)     # cat_b -150 g at once
        named = [x for x in labs if x[2] in CATS]
        wrong = [x for x in named if x[2] != x[1]]
        self.assertLessEqual(len(wrong), max(3, 0.10 * len(named)), wrong[:10])


class IdentityAnchors(unittest.TestCase):
    """In-box identity anchor, sudden change, fresh evidence, interval fields."""

    def _lose_both(self, tr):
        for c in CATS:
            tr._mark_lost(c, tr.tracks[c]["t"])
        self.assertEqual(tr.status(tr.tracks["cat_b"]["t"] + 60), "needs_weigh_in")

    def test_two_box_corrections_settle_identity(self):
        tr, t = established()
        self._lose_both(tr)
        tr.on_visit(obs(t + 600, (2.11, 6)), {}, now=t + 600)
        tr.correct_last("cat_a", now=t + 700)
        self.assertEqual(tr.tracks["cat_a"]["ident"], "lost")          # one cat alone settles nothing
        tr.on_visit(obs(t + 3 * 3600, (2.37, 6)), {}, now=t + 3 * 3600)
        f = tr.correct_last("cat_b", now=t + 3 * 3600 + 60)
        self.assertEqual(f["cat_status"], "ok")
        self.assertAlmostEqual(f["cat_a_weight"], 2.11, delta=0.005)
        self.assertAlmostEqual(f["cat_b_weight"], 2.37, delta=0.005)
        self.assertTrue(any(ev == "cats_box_session" for ev, _ in LOGS))
        rec = {}
        tr.on_visit(obs(t + 4 * 3600, (2.10, 4)), rec, now=t + 4 * 3600)
        self.assertEqual(rec["cat"], "cat_a")

    def test_box_corrections_too_close_or_too_far_apart_in_time(self):
        tr, t = established()
        self._lose_both(tr)
        tr.on_visit(obs(t + 600, (2.30, 6)), {}, now=t + 600)
        tr.correct_last("cat_a", now=t + 700)
        tr.on_visit(obs(t + 1200, (2.34, 6)), {}, now=t + 1200)
        tr.correct_last("cat_b", now=t + 1300)                         # 40 g apart: can't tell which is which
        self.assertEqual(tr.tracks["cat_b"]["ident"], "lost")
        tr2, t2 = established()
        self._lose_both(tr2)
        tr2.on_visit(obs(t2 + 600, (2.11, 6)), {}, now=t2 + 600)
        tr2.correct_last("cat_a", now=t2 + 700)
        t3 = t2 + 26 * 3600
        tr2.on_visit(obs(t3, (2.37, 6)), {}, now=t3)
        tr2.correct_last("cat_b", now=t3 + 60)                         # > 24 h after Cat a's
        self.assertEqual(tr2.tracks["cat_b"]["ident"], "lost")

    def test_box_session_survives_restart(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            tr, t = established(path=p)
            self._lose_both(tr)
            tr.on_visit(obs(t + 600, (2.11, 6)), {}, now=t + 600)
            tr.correct_last("cat_a", now=t + 700)
            tr.save()
            tr2 = T.CatTracker(CATS, SEEDS, p, now=t + 800)
            tr2.on_visit(obs(t + 3600, (2.37, 6)), {}, now=t + 3600)
            self.assertEqual(tr2.correct_last("cat_b", now=t + 3700)["cat_status"], "ok")
        finally:
            shutil.rmtree(d)

    def test_common_mode_shift_is_not_a_sudden_change(self):
        # litter top-up / tare drift: both cats read 90 g heavier from now on
        LOGS.clear()
        tr, t = established(days=8)
        rng = random.Random(3)
        for k in range(40):
            tt = t + 3600 * (k + 1)
            c = "cat_a" if k % 4 == 0 else "cat_b"
            w = (2.10 if c == "cat_a" else 2.36) + 0.09 + rng.gauss(0, 0.015)
            tr.on_visit(obs(tt, (round(w, 3), 5)), {}, now=tt)
        self.assertFalse(any(ev == "cats_cusum_alarm" for ev, _ in LOGS), [e for e, _ in LOGS][-10:])
        self.assertTrue(all(tr.tracks[c]["ident"] == "ok" for c in CATS))

    def test_sudden_step_flags_sudden_change(self):
        LOGS.clear()
        tr, t = established(days=8)
        rng = random.Random(4)
        for k in range(30):
            tt = t + 3600 * (k + 1)
            c = "cat_a" if k % 4 == 0 else "cat_b"
            w = 2.10 if c == "cat_a" else 2.24          # Cat b drops 120 g at once
            tr.on_visit(obs(tt, (round(w + rng.gauss(0, 0.015), 3), 5)), {}, now=tt)
        f = tr.fields(t + 31 * 3600)
        # the step is too big to be labelled Cat b: the clusters stop holding Cat b's track (a CUSUM alarm
        # is the slower path for a gradual one); the unnamed cluster weights stay published
        self.assertEqual(f["cat_status"], "needs_weigh_in")
        self.assertEqual(f["cat_b_stale_reason"], "sudden_change")
        self.assertAlmostEqual(f["cluster_high_kg"], 2.24, delta=0.05)   # a 3-day fit, partly pre-step
        self.assertAlmostEqual(f["cluster_low_kg"], 2.10, delta=0.02)
        self.assertAlmostEqual(f["cat_a_weight"], 2.10, delta=0.02)

    def test_ok_needs_fresh_evidence_of_every_cat(self):
        tr, t = established()
        for k in range(30):                             # only Cat b visits for 75 h
            tt = t + 2.5 * 3600 * (k + 1)
            tr.on_visit(obs(tt, (2.36, 5)), {}, now=tt)
        self.assertNotEqual(tr.fields(t + 76 * 3600)["cat_status"], "ok")

    def test_weight_interval_fields(self):
        tr, t = established()
        f = tr.fields(t)
        for c in CATS:
            self.assertLess(f[c + "_weight_lo_kg"], f[c + "_weight"])
            self.assertGreater(f[c + "_weight_hi_kg"], f[c + "_weight"])
            self.assertAlmostEqual(f[c + "_weight_hi_kg"] - f[c + "_weight"], 2 * f[c + "_weight_unc_g"] / 1000, delta=0.002)


class Persistence(unittest.TestCase):
    """Corrections vs later weigh-ins, restarts, bad state files, and HA's "no identical re-entry" constraint."""

    def test_correct_last_keeps_a_later_weigh_in(self):
        tr, t = established()
        tr.on_visit(obs(t + 600, (2.11, 5)), {}, now=t + 600)
        tr.set_ref("cat_b", 2.45, now=t + 3600)
        w_after = tr.tracks["cat_b"]["w"]
        self.assertGreater(w_after, 2.37)
        tr.correct_last("cat_a", now=t + 7200)
        self.assertAlmostEqual(tr.tracks["cat_b"]["w"], w_after, delta=1e-9)
        self.assertEqual(tr.tracks["cat_b"]["last_wi"], t + 3600)

    def test_correct_last_keeps_a_later_session(self):
        tr, t = established()
        for c in CATS:
            tr._mark_lost(c, t - 3600)
        tr.on_visit(obs(t + 60, (2.13, 5)), {}, now=t + 60)
        tr.set_ref("cat_a", 2.12, now=t + 600)
        tr.set_ref("cat_b", 2.36, now=t + 900)
        self.assertEqual(tr.status(t + 1000), "ok")
        tr.correct_last("cat_a", now=t + 1200)
        self.assertEqual(tr.status(t + 1300), "ok")
        self.assertTrue(all(tr.tracks[c]["ident"] == "ok" for c in CATS))

    def test_floor_weigh_in_survives_restart(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            tr, t = established(path=p)
            tr.set_ref("cat_a", 0.2, now=t + 60)
            tr.set_ref("cat_a", 0.2, now=t + 120)            # confirmed reset to the floor
            tr.save()
            LOGS.clear()
            tr2 = T.CatTracker(CATS, SEEDS, p, now=t + 200)
            self.assertFalse(any(ev == "cats_state_dropped" for ev, _ in LOGS), LOGS)
            self.assertEqual(tr2.tracks["cat_a"]["ident"], tr.tracks["cat_a"]["ident"])
            self.assertAlmostEqual(tr2.tracks["cat_a"]["w"], tr.tracks["cat_a"]["w"], delta=1e-9)
        finally:
            shutil.rmtree(d)

    def test_wrong_shape_or_version_is_quarantined_and_logged(self):
        for bad in ({"version": 2, "tracks": []}, {"version": 3, "tracks": {}}, {"tracks": {}}):
            d = tempfile.mkdtemp()
            try:
                p = os.path.join(d, "cats.json")
                with open(T.v2_path_for(p), "w") as fh:
                    json.dump(bad, fh)
                LOGS.clear()
                tr = T.CatTracker(CATS, SEEDS, p, now=T0)
                self.assertTrue(any(ev == "cats_state_unreadable" for ev, _ in LOGS), bad)
                self.assertTrue(any(f.startswith("cats_v2.json.corrupt-") for f in os.listdir(d)), bad)
                self.assertEqual(tr.fields(T0)["cat_status"], "learning")
            finally:
                shutil.rmtree(d)

    def test_unreadable_v2_does_not_remigrate_v1(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cats.json")
            write_v1(p)
            with open(T.v2_path_for(p), "w") as fh:
                fh.write("{not json")
            LOGS.clear()
            tr = T.CatTracker(CATS, SEEDS, p, now=T0)
            self.assertFalse(any(ev.startswith("cats_migrat") for ev, _ in LOGS), [e for e, _ in LOGS])
            self.assertTrue(all(tr.tracks[c]["ident"] == "init" for c in CATS))
            self.assertEqual(sum(tr.visits_total.values()), 0)
        finally:
            shutil.rmtree(d)

    def test_confirm_with_value_changed_by_10_g(self):
        tr, t = established()
        tr.set_ref("cat_a", 1.80, now=t + 60)                # 300 g off: rejected
        self.assertEqual(tr.weighins[-1]["action"], "rejected")
        tr.set_ref("cat_b", 2.36, now=t + 300)               # both cats weighed (the box disagrees with 1.80)
        tr.set_ref("cat_a", 1.81, now=t + 600)               # HA cannot resend 1.80: +0.01 confirms
        self.assertEqual(tr.weighins[-1]["detail"], "accepted (reset)")

class WeighInEdgeCases(unittest.TestCase):
    """Re-entries, duplicates, unidentified cats, box misclicks, far resets, session rates, scale shifts."""

    def test_reentry_changed_by_10_g_never_swaps(self):
        tr, t = established()
        tr.set_ref("cat_a", 2.33, now=t + 60)                     # typo: matches Cat b
        self.assertEqual(tr.weighins[-1]["action"], "rejected")
        self.assertNotIn("again", tr.weighins[-1]["reason"])
        tr.set_ref("cat_a", 2.34, now=t + 600)                    # the same statement re-entered
        self.assertFalse(tr.weighins[-1]["detail"].startswith("accepted (swapped"))
        self.assertAlmostEqual(tr.tracks["cat_a"]["w"], 2.10, delta=0.03)
        self.assertAlmostEqual(tr.tracks["cat_b"]["w"], 2.36, delta=0.03)

    def test_two_cats_statements_still_swap(self):
        tr, t = established()
        tr.set_ref("cat_a", 2.36, now=t + 60)                     # Cat a's entry matches Cat b ...
        tr.set_ref("cat_b", 2.10, now=t + 120)                    # ... and Cat b's matches Cat a
        self.assertTrue(tr.weighins[-1]["detail"].startswith("accepted (swapped"))

    def test_same_cat_weighed_twice_is_a_duplicate(self):
        tr, t = established()
        tr.set_ref("cat_a", 2.11, now=t + 60)
        tr.set_ref("cat_b", 2.12, now=t + 120)                    # Cat a again, entered as Cat b
        self.assertEqual(tr.weighins[-1]["action"], "rejected")
        self.assertIn("entered twice", tr.weighins[-1]["reason"])
        tr.set_ref("cat_b", 2.13, now=t + 600)
        self.assertFalse(tr.weighins[-1]["detail"].startswith("accepted (swapped"))
        self.assertAlmostEqual(tr.tracks["cat_b"]["w"], 2.36, delta=0.03)

    def test_close_crossing_session_is_not_a_duplicate(self):
        tr, t = established()
        tr.tracks["cat_a"]["w"], tr.tracks["cat_b"]["w"] = 2.31, 2.34    # kittens crossing
        tr.set_ref("cat_a", 2.31, now=t + 60)
        tr.set_ref("cat_b", 2.33, now=t + 120)
        self.assertNotEqual(tr.weighins[-1]["action"], "rejected")

    def test_unidentified_cat_is_blank(self):
        tr, t = established(days=8)
        rng = random.Random(4)
        f = None
        for k in range(30):
            tt = t + 3600 * (k + 1)
            c = "cat_a" if k % 4 == 0 else "cat_b"
            w = 2.10 if c == "cat_a" else 2.22
            f = tr.on_visit(obs(tt, (round(w + rng.gauss(0, 0.015), 3), 5)), {}, now=tt)
        self.assertIn("cat_b", tr._unidentified_cats(t + 30 * 3600) | {c for c in CATS if tr._unsupported(c, t + 30 * 3600)})
        self.assertIsNone(f["cat_b_weight"])
        self.assertIsNotNone(f["cat_a_weight"])

    def test_rejected_typo_does_not_blank(self):
        tr, t = established()
        tr.set_ref("cat_a", 1.60, now=t + 60)                     # far typo, rejected
        f = tr.fields(t + 120)
        self.assertIsNotNone(f["cat_a_weight"])

    def _both_lost(self, tr, t):
        for c in CATS:
            tr._mark_lost(c, t - 3600)

    def test_box_misclick_does_not_settle(self):
        tr, t = established()
        self._both_lost(tr, t)
        tr._lf_check(t, force=True)                              # fresh clusters at 2.10 / 2.36
        tr.on_visit(obs(t + 600, (2.11, 6)), {}, now=t + 600)
        tr.correct_last("cat_a", now=t + 700)
        tr.on_visit(obs(t + 3600, (2.20, 6)), {}, now=t + 3600)  # another Cat a visit, misclicked Cat b
        tr.correct_last("cat_b", now=t + 3700)
        self.assertEqual(tr.tracks["cat_b"]["ident"], "lost")

    def test_box_perched_or_single_reading_does_not_anchor(self):
        tr, t = established()
        self._both_lost(tr, t)
        tr.on_visit(obs(t + 600, (1.93, 1)), {}, now=t + 600)   # a single sample
        tr.correct_last("cat_a", now=t + 700)
        self.assertEqual(tr.box_ids, [])

    def test_confirmed_far_reset_needs_both_cats_when_box_disagrees(self):
        tr, t = established()
        tr._lf_check(t, force=True)
        tr.set_ref("cat_a", 1.81, now=t + 60)
        tr.set_ref("cat_a", 1.82, now=t + 600)
        self.assertEqual(tr.weighins[-1]["action"], "rejected")
        self.assertIn("box disagrees", tr.weighins[-1]["reason"])
        self.assertAlmostEqual(tr.tracks["cat_a"]["w"], 2.10, delta=0.03)
        self.assertNotEqual(tr.status(t + 700), "ok")

    def test_session_rate_ignores_pending_and_other_cat(self):
        tr, t = established(g_cat_a=0.025)
        for c in CATS:
            tr._mark_lost(c, t - 3600)
        tr.weighins.append({"t": t - 7 * DAY, "cat": "cat_b", "kg": 2.90, "action": "pending", "detail": "pending",
                            "reason": None, "box": None})
        w = {"t": t, "cat": "cat_b", "kg": 2.36, "action": "pending"}
        g, _ = tr._session_rate("cat_b", w, list(CATS))
        self.assertGreaterEqual(g, T.SESSION_RATE_RANGE[0])
        self.assertLess(abs(g), 0.01)                           # not (2.36 - 2.90) / 7, not Cat a's 25 g/day

    def test_overlap_stops_publishing_after_escalation(self):
        tr, t = established()
        tr.overlap.append({"pair": ["cat_b", "cat_a"], "since": t - 49 * 3600, "sep_since": None,
                           "order": ["cat_a", "cat_b"]})
        f = tr.fields(t)
        self.assertEqual(f["cat_status"], "needs_weigh_in")
        self.assertIsNone(f["cat_a_weight"])
        self.assertIsNone(f["cat_b_weight"])

    def test_scale_shift_is_visible(self):
        tr, t = established(days=8)
        rng = random.Random(3)
        f = None
        for k in range(60):
            tt = t + 3600 * (k + 1)
            c = "cat_a" if k % 4 == 0 else "cat_b"
            w = (2.10 if c == "cat_a" else 2.36) + 0.09 + rng.gauss(0, 0.015)
            f = tr.on_visit(obs(tt, (round(w, 3), 5)), {}, now=tt)
        self.assertIn("scale_shift", f)
        if any(ev == "cats_scale_shift" for ev, _ in LOGS):
            self.assertTrue((f["scale_shift"] or "").startswith("scale_shift"))


class CatsConfigTests(unittest.TestCase):
    """CATLINK_CATS parsing (catlink_local/config.py): rough seeds, any order, up to config.MAX_CATS cats."""

    def test_empty_is_off(self):
        self.assertEqual(config.parse_cats(""), [])
        self.assertEqual(config.parse_cats("   "), [])
        self.assertEqual(config.parse_cats(None), [])

    def test_list_and_order(self):
        self.assertEqual(config.parse_cats("cat_a=3.20,cat_b=4.60"), [("cat_a", 3.2), ("cat_b", 4.6)])
        self.assertEqual(config.parse_cats(" cat_a = 3.2 , cat_b=4.6 "), [("cat_a", 3.2), ("cat_b", 4.6)])
        self.assertEqual(config.parse_cats("solo=4.2"), [("solo", 4.2)])
        # rough seeds in any order: the heavier cat may come first (a weigh-in session fixes a wrong guess)
        self.assertEqual(config.parse_cats("cat_a=4.6,cat_b=3.2"), [("cat_a", 4.6), ("cat_b", 3.2)])
        self.assertEqual(len(config.parse_cats("a=2.0,b=2.4,c=3.1")), 3)

    def test_errors_name_the_variable(self):
        bad = ["cat_a=3.2,cat_a=4.6",            # duplicate
               "Cat=3.2", "1cat=3.2", "cat-a=3.2", "cat_a", "cat_a=", "cat_a=heavy",
               "unknown=3.2", "last_visit=3.2", "two_cat=3.2", "two_cats=3.2", "cat_a=0.1", "cat_a=40",
               "a=2.0,b=2.4,c=3.1,d=4.0"]       # more than MAX_CATS
        for value in bad:
            with self.assertRaises(config.ConfigError, msg=value) as ctx:
                config.parse_cats(value)
            self.assertIn("CATLINK_CATS", str(ctx.exception))

    def test_too_many_message(self):
        with self.assertRaises(config.ConfigError) as ctx:
            config.parse_cats("a=1,b=2,c=3,d=4")
        self.assertIn("at most %d" % config.MAX_CATS, str(ctx.exception))
        self.assertLessEqual(config.MAX_CATS, T.MAX_CATS)

    def test_load_carries_cats(self):
        env = {"CATLINK_BOX_MAC": "02:CA:7B:0C:00:01", "CATLINK_BOX_BIND_IP": "127.0.0.1",
               "CATLINK_CATS": "cat_a=3.20,cat_b=4.60"}
        cfg = config.load("gateway", env)
        self.assertEqual(cfg.cats, [("cat_a", 3.2), ("cat_b", 4.6)])
        self.assertEqual(cfg.box_mac, "02:ca:7b:0c:00:01")
        self.assertEqual(config.load("gateway", dict(env, CATLINK_CATS="")).cats, [])

    def test_display_name(self):
        self.assertEqual(T.display_name("cat_a"), "Cat a")
        self.assertEqual(T.display_name("whiskers"), "Whiskers")


if __name__ == "__main__":
    unittest.main()
