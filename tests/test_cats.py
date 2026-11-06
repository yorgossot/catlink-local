"""CatTracker unit tests (0, 1 and 2 configured cats) and CATLINK_CATS parsing.

Deterministic and in-process: temp state files, `now` injected so the rolling window is deterministic.
Seeds and weights are synthetic.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from catlink_local import cats as c  # noqa: E402
from catlink_local import config  # noqa: E402
from catlink_local import log as logmod  # noqa: E402

TWO = [("cat_a", 3.20), ("cat_b", 4.60)]
ONE = [("cat_a", 3.10)]


class TrackerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="catlink_cats_")
        self.path = os.path.join(self.dir, "cats.json")
        self.lines = []
        logmod.configure(writer=self.lines.append)
        self.T0 = time.time()   # samples stay inside the 14-day rolling window; offsets are seconds

    def tearDown(self):
        logmod.reset()
        shutil.rmtree(self.dir, ignore_errors=True)

    def events(self):
        return [json.loads(line) for line in self.lines]

    def tracker(self, cats=TWO):
        return c.CatTracker(cats, self.path)


class TwoCatTests(TrackerCase):
    def test_labelling_and_unknown_band(self):
        t = self.tracker()
        self.assertEqual((t.refs["cat_a"], t.refs["cat_b"]), (3.20, 4.60))
        r = {}
        t.on_visit(3.18, r, now=self.T0)
        self.assertEqual(r["cat"], "cat_a")
        self.assertGreaterEqual(r["conf"], 80)
        r = {}
        t.on_visit(4.65, r, now=self.T0 + 2000)
        self.assertEqual(r["cat"], "cat_b")
        self.assertGreaterEqual(r["conf"], 80)
        r = {}
        t.on_visit(3.91, r, now=self.T0 + 4000)   # refs now 3.18 / 4.65, mid 3.915
        self.assertEqual(r["cat"], "unknown")

    def test_no_timing_rule(self):
        # Visits close together are each classified on their own weight; an earlier visit is never relabelled.
        t = self.tracker()
        r1, r2 = {}, {}
        t.on_visit(3.87, r1, now=self.T0)          # inside the unknown band (mid 3.90 ± 0.06)
        t.on_visit(3.97, r2, now=self.T0 + 300)    # 5 min later, just outside the band
        self.assertEqual(r1["cat"], "unknown")
        self.assertEqual(r2["cat"], "cat_b")
        self.assertEqual(t.samples["cat_a"], [])

    def test_unsettled_weight_is_less_certain(self):
        # A visit too short for a settled weight (the box peak stands in): twice the unknown band, no sample.
        t = self.tracker()
        r1, r2 = {}, {}
        t.on_visit(4.00, r1, now=self.T0, settled=False)        # 100 g past mid: unknown when unsettled
        t.on_visit(4.70, r2, now=self.T0 + 60, settled=False)   # clearly cat_b: labelled, but no sample
        self.assertEqual(r1["cat"], "unknown")
        self.assertEqual(r2["cat"], "cat_b")
        self.assertEqual((t.samples["cat_a"], t.samples["cat_b"]), ([], []))
        self.assertEqual((t.refs["cat_a"], t.refs["cat_b"]), (3.20, 4.60))
        self.assertEqual(t._fields()["labelled_pct"], 50)
        t.correct_last("cat_a")                                  # relabelled, still no sample from a peak weight
        self.assertEqual((r2["cat"], t.samples["cat_a"], t.correction_count), ("cat_a", [], 1))
        self.assertEqual(t._fields()["labelled_pct"], 50)
        r3 = {}
        t.on_visit(4.00, r3, now=self.T0 + 120)                  # the same weight, settled: cat_b
        self.assertEqual(r3["cat"], "cat_b")

    def test_same_weight_revisits_not_split(self):
        # One cat re-entering 4x in 10 min at the same weight must not be split between the cats.
        t = self.tracker()
        rows = []
        for dt, w in ((0, 4.61), (120, 4.64), (360, 4.61), (600, 4.60)):
            r = {}
            t.on_visit(w, r, now=self.T0 + dt)
            rows.append(r)
        self.assertEqual([r["cat"] for r in rows], ["cat_b"] * 4)
        self.assertEqual(t.samples["cat_a"], [])
        self.assertFalse(t.degraded)
        r = {}
        t.on_visit(3.22, r, now=self.T0 + 2700)
        self.assertEqual(r["cat"], "cat_a")

    def test_beyond_fifteen_minutes_not_paired(self):
        t = self.tracker()
        r3, r4 = {}, {}
        t.on_visit(3.88, r3, now=self.T0)
        t.on_visit(3.89, r4, now=self.T0 + 1000)
        self.assertEqual((r3["cat"], r4["cat"]), ("unknown", "unknown"))

    def test_tracked_weight_is_median_of_confident_visits(self):
        t = self.tracker()
        for i, w in enumerate((3.18, 3.22, 3.20)):
            r = {}
            t.on_visit(w, r, now=self.T0 + i * 2000)
            self.assertEqual(r["cat"], "cat_a", i)
        self.assertEqual(t._median("cat_a"), 3.20)
        self.assertEqual(t._fields()["cat_a_weight"], 3.20)

    def test_correction(self):
        t = self.tracker()
        r = {}
        t.on_visit(4.56, r, now=self.T0)
        self.assertEqual(r["cat"], "cat_b")
        fields = t.correct_last("cat_a")
        self.assertIsNotNone(fields)
        self.assertEqual((r["cat"], t.correction_count), ("cat_a", 1))
        self.assertAlmostEqual(t.refs["cat_a"], 4.56, places=6)
        self.assertGreaterEqual(round(t.refs["cat_b"] - t.refs["cat_a"], 3), c.CAT_GAP_FLOOR)
        self.assertEqual(fields["correction_count"], 1)
        self.assertEqual(fields["last_cat"], "cat_a")

    def test_correction_rejects_unknown_slug_and_before_first_visit(self):
        t = self.tracker()
        self.assertIsNone(t.correct_last("cat_a"))      # no visit yet
        t.on_visit(3.20, {}, now=self.T0)
        self.assertIsNone(t.correct_last("cat_z"))
        self.assertIsNone(t.correct_last("unknown"))

    def test_weigh_in_reseed_keeps_gap_floor(self):
        t = self.tracker()
        t.set_ref("cat_a", 3.30)
        self.assertEqual((t.refs["cat_a"], t.refs["cat_b"]), (3.30, 4.60))
        t.set_ref("cat_b", "3.40")                      # gap would be 0.10 -> push cat_a down to 3.25
        self.assertEqual(t.refs["cat_b"], 3.40)
        self.assertEqual(round(t.refs["cat_a"], 3), 3.25)
        self.assertIsNone(t.set_ref("cat_a", 99))
        self.assertIsNone(t.set_ref("cat_a", "heavy"))
        self.assertIsNone(t.set_ref("cat_z", 3.2))

    def test_degraded_mode(self):
        t = self.tracker()
        t.samples["cat_a"] = [{"t": self.T0, "w": 4.30}]
        t.samples["cat_b"] = [{"t": self.T0, "w": 4.34}]
        t._refit()
        self.assertIs(t.degraded, True)
        r = {}
        t.on_visit(4.32, r, now=self.T0 + 100000)
        self.assertEqual(r["cat"], "unknown")
        self.assertIs(t._fields()["cat_degraded"], True)
        t.set_ref("cat_b", 4.90)
        self.assertIs(t.degraded, False)
        self.assertEqual(t.refs["cat_b"], 4.90)

    def test_published_fields(self):
        t = self.tracker()
        fields = t.on_visit(3.18, {}, now=self.T0)
        for key in ("cat_a_weight", "cat_b_weight", "cat_a_trend_7d", "cat_b_trend_7d", "cat_a_trend_word",
                    "cat_b_trend_word", "cat_gap", "labelled_pct", "correction_count", "cat_degraded",
                    "last_cat", "last_conf", "last_visit_cat"):
            self.assertIn(key, fields)
        self.assertEqual(fields["last_visit_cat"], "Cat a %d%%" % fields["last_conf"])
        self.assertEqual(fields["cat_gap"], int(round((fields["cat_b_weight"] - fields["cat_a_weight"]) * 1000)))
        self.assertEqual(fields["labelled_pct"], 100)
        self.assertEqual(fields["cat_a_trend_word"], "steady")

    def test_unknown_label_text(self):
        t = self.tracker()
        fields = t.on_visit(3.90, {}, now=self.T0)
        self.assertEqual(fields["last_cat"], "unknown")
        self.assertTrue(fields["last_visit_cat"].startswith("Unknown "))
        self.assertEqual(fields["labelled_pct"], 0)

    def test_trend_words(self):
        self.assertEqual(c.CatTracker._trend_word(21), "gaining")
        self.assertEqual(c.CatTracker._trend_word(-21), "losing")
        self.assertEqual(c.CatTracker._trend_word(20), "steady")

    def test_trend_uses_two_weeks(self):
        t = self.tracker()
        now = time.time()
        t.samples["cat_a"] = [{"t": now - 10 * 86400, "w": 3.20}, {"t": now - 86400, "w": 3.25}]
        self.assertEqual(t._trend("cat_a"), 50)
        self.assertEqual(t._fields()["cat_a_trend_word"], "gaining")


class OneCatTests(TrackerCase):
    def test_always_labelled_full_confidence(self):
        t = self.tracker(ONE)
        for i, w in enumerate((2.0, 3.3, 5.0)):
            r = {}
            fields = t.on_visit(w, r, now=self.T0 + i * 60)   # even minutes apart
            self.assertEqual((r["cat"], r["conf"]), ("cat_a", c.CAT_SINGLE_CONF))
            self.assertEqual(fields["last_visit_cat"], "Cat a 100%")
        self.assertEqual(t._fields()["labelled_pct"], 100)

    def test_median_tracking_and_seed(self):
        t = self.tracker(ONE)
        self.assertEqual(t._fields()["cat_a_weight"], 3.10)
        for i, w in enumerate((3.00, 3.40, 3.20)):
            t.on_visit(w, {}, now=self.T0 + i * 2000)
        self.assertEqual(t._fields()["cat_a_weight"], 3.20)

    def test_no_pair_fields(self):
        t = self.tracker(ONE)
        fields = t.on_visit(3.0, {}, now=self.T0)
        self.assertNotIn("cat_gap", fields)
        self.assertNotIn("cat_degraded", fields)
        self.assertIn("cat_a_trend_7d", fields)
        self.assertIn("cat_a_trend_word", fields)

    def test_correct_last_is_noop(self):
        t = self.tracker(ONE)
        t.on_visit(3.0, {}, now=self.T0)
        self.assertIsNone(t.correct_last("cat_a"))
        self.assertEqual(t.correction_count, 0)

    def test_set_ref_sets_seed(self):
        t = self.tracker(ONE)
        self.assertIsNotNone(t.set_ref("cat_a", "3.55"))
        self.assertEqual(t.refs["cat_a"], 3.55)
        self.assertEqual(t._fields()["cat_a_weight"], 3.55)
        self.assertIs(t.degraded, False)


class PersistenceTests(TrackerCase):
    def test_round_trip(self):
        t = self.tracker()
        t.on_visit(3.18, {}, now=self.T0)
        t.on_visit(4.65, {}, now=self.T0 + 2000)
        t.correct_last("cat_a")
        t.save()
        u = self.tracker()
        self.assertEqual(u.refs, t.refs)
        self.assertEqual(u.samples, t.samples)
        self.assertEqual((u.correction_count, u.total_visits, u.labelled_visits, u.degraded),
                         (t.correction_count, t.total_visits, t.labelled_visits, t.degraded))
        with open(self.path) as fh:
            data = json.load(fh)
        self.assertEqual(set(data["refs"]), {"cat_a", "cat_b"})

    def test_slug_mismatch_is_ignored_and_logged(self):
        t = self.tracker()
        t.on_visit(3.18, {}, now=self.T0)
        t.save()
        u = self.tracker([("cat_x", 3.30), ("cat_y", 4.90)])
        self.assertEqual(u.refs, {"cat_x": 3.30, "cat_y": 4.90})
        self.assertEqual(u.samples, {"cat_x": [], "cat_y": []})
        self.assertEqual(u.total_visits, 0)
        self.assertIn("cats_state_mismatch", [e["event"] for e in self.events()])
        u.save()                                       # overwritten with the configured slugs
        with open(self.path) as fh:
            self.assertEqual(set(json.load(fh)["refs"]), {"cat_x", "cat_y"})

    def test_two_to_one_cat_change_is_a_mismatch(self):
        t = self.tracker()
        t.save()
        u = self.tracker([("cat_a", 3.0)])
        self.assertEqual(u.refs, {"cat_a": 3.0})
        self.assertIn("cats_state_mismatch", [e["event"] for e in self.events()])

    def test_missing_or_corrupt_file(self):
        t = self.tracker()
        self.assertEqual(t.refs, {"cat_a": 3.20, "cat_b": 4.60})
        with open(self.path, "w") as fh:
            fh.write("{not json")
        u = self.tracker()
        self.assertEqual(u.refs, {"cat_a": 3.20, "cat_b": 4.60})

    def test_save_failure_is_logged(self):
        t = c.CatTracker(TWO, os.path.join(self.dir, "missing-dir", "cats.json"))
        t.save()
        self.assertIn("cats_persist_failed", [e["event"] for e in self.events()])


class TrackerConstructionTests(unittest.TestCase):
    def test_zero_or_three_cats_rejected(self):
        with self.assertRaises(ValueError):
            c.CatTracker([], "/nonexistent/cats.json")
        with self.assertRaises(ValueError):
            c.CatTracker(TWO + [("cat_c", 5.0)], "/nonexistent/cats.json")

    def test_display_name(self):
        self.assertEqual(c.display_name("cat_a"), "Cat a")
        self.assertEqual(c.display_name("whiskers"), "Whiskers")


class CatsConfigTests(unittest.TestCase):
    def test_empty_is_off(self):
        self.assertEqual(config.parse_cats(""), [])
        self.assertEqual(config.parse_cats("   "), [])
        self.assertEqual(config.parse_cats(None), [])

    def test_list_and_order(self):
        self.assertEqual(config.parse_cats("cat_a=3.20,cat_b=4.60"), [("cat_a", 3.2), ("cat_b", 4.6)])
        self.assertEqual(config.parse_cats(" cat_a = 3.2 , cat_b=4.6 "), [("cat_a", 3.2), ("cat_b", 4.6)])
        self.assertEqual(config.parse_cats("solo=4.2"), [("solo", 4.2)])

    def test_errors_name_the_variable(self):
        bad = ["cat_a=4.6,cat_b=3.2",            # heavier first
               "cat_a=3.2,cat_a=4.6",            # duplicate
               "Cat=3.2", "1cat=3.2", "cat-a=3.2", "cat_a", "cat_a=", "cat_a=heavy",
               "unknown=3.2", "last_visit=3.2", "cat_a=0.1", "cat_a=40",
               "cat_a=3.2,cat_b=4.6,cat_c=5.0"]   # more than two
        for value in bad:
            with self.assertRaises(config.ConfigError, msg=value) as ctx:
                config.parse_cats(value)
            self.assertIn("CATLINK_CATS", str(ctx.exception))

    def test_three_entries_message(self):
        with self.assertRaises(config.ConfigError) as ctx:
            config.parse_cats("a=1,b=2,c=3")
        self.assertIn("at most 2", str(ctx.exception))

    def test_load_carries_cats(self):
        env = {"CATLINK_BOX_MAC": "02:CA:7B:0C:00:01", "CATLINK_BOX_BIND_IP": "127.0.0.1",
               "CATLINK_CATS": "cat_a=3.20,cat_b=4.60"}
        cfg = config.load("gateway", env)
        self.assertEqual(cfg.cats, TWO)
        self.assertEqual(cfg.box_mac, "02:ca:7b:0c:00:01")
        self.assertEqual(config.load("gateway", dict(env, CATLINK_CATS="")).cats, [])


if __name__ == "__main__":
    unittest.main()
