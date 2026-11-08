"""In-process tests of the gateway's per-visit pipeline with the cat tracker: occupancy state machine, plateau
segments, report pairing, baseline / deposit / litter guards, state files, the stale-v2 guard and the HA cat
fields. A fake clock drives catlink_local.gateway.GatewayState directly; no box, no MQTT, no network.

Traces are SYNTHETIC, shaped like what a real box sends: an empty-scale reading about every 100 s while idle,
one every 10-15 s while a cat is inside, the occupancy flag lagging the scale by about one reading, the box's
own visit report (`01 03 mm ss kg cg`) at the cat's exit. Two configured cats weigh about 3.05 and 3.30 kg (close,
the hard case); litter readings are relative to an empty-scale baseline of 0-2 kg.
"""
import glob
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from catlink_local import cats as cats_mod  # noqa: E402
from catlink_local import config  # noqa: E402
from catlink_local import gateway as gw  # noqa: E402

T_START = 1.9e9                 # a synthetic epoch; every case starts here
CATS = [("cat_a", 3.00), ("cat_b", 3.30)]   # CATLINK_CATS seeds (rough)
REFS = (3.045, 3.30)            # the weights the cats are established at (a migrated 0.1 cats.json)
OCC = 0x26                      # an "occupied" heartbeat byte


def write_v1(path, refs, end_t, n=6, step_h=8.0, extra=None, names=("cat_a", "cat_b")):
    """A cats.json of catlink-local 0.1: refs + n confident samples per cat ending at end_t. The tracker migrates
    it on start (read only) into established tracks."""
    data = {"refs": dict(zip(names, refs)),
            "samples": {c: [{"t": end_t - step_h * 3600 * (n - 1 - i), "w": w} for i in range(n)]
                        for c, w in zip(names, refs)},
            "correction_count": 0, "total_visits": 2 * n, "labelled_visits": 2 * n, "degraded": False}
    data.update(extra or {})
    with open(path, "w") as fh:
        json.dump(data, fh)


class PipelineCase(unittest.TestCase):
    """Fake clock + captured logs + temp state files; helpers to replay heartbeats and box reports."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="catlink_pipe_")
        self.clock = [T_START]
        self.logs = []
        self._saved = (gw.time, gw.log, cats_mod.log)
        gw.time = types.SimpleNamespace(time=lambda: self.clock[0], monotonic=lambda: self.clock[0],
                                        sleep=lambda s: None, time_ns=lambda: int(self.clock[0] * 1e9))
        gw.log = cats_mod.log = lambda event, **f: self.logs.append((event, f))
        self.cats_file = os.path.join(self.dir, "cats.json")
        self.visits_file = os.path.join(self.dir, "visits.jsonl")
        self.litter_file = os.path.join(self.dir, "litter.json")
        self.seed_end = 0.0

    def tearDown(self):
        gw.time, gw.log, cats_mod.log = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---- construction ----------------------------------------------------------------------------
    def state(self, cats=CATS):
        return gw.GatewayState(self.visits_file, cats, self.cats_file, self.litter_file)

    def fresh(self, refs=REFS, litter=None, cats=CATS):
        """A gateway on fresh state files. `refs`: the cats are established at these weights (a 0.1 cats.json
        with 6 samples each, the newest 2 h ago); None = no cat state at all."""
        del self.logs[:]
        if litter is not None:
            with open(self.litter_file, "w") as fh:
                json.dump(litter, fh)
        if refs:
            write_v1(self.cats_file, refs, self.clock[0] - 7200, names=[c for c, _ in cats])
        self.seed_end = self.clock[0] - 7200
        return self.state(cats)

    def drop_v2(self):
        p = gw.v2_path_for(self.cats_file)
        if os.path.exists(p):
            os.remove(p)

    def new_w(self, gs, cat):
        """The cat's weight samples taken since fresh() (the migrated ones excluded)."""
        return [round(q[1], 3) for q in gs.cats.samples[cat] if q[0] > self.seed_end]

    def events(self, name):
        return [f for e, f in self.logs if e == name]

    # ---- replay ----------------------------------------------------------------------------------
    def hb(self, gs, scale, occ=False, state=0):
        c = int(round(scale * 100))
        gs.on_heartbeat({"valid": True, "scale_kg": c // 100, "scale_cg": c % 100, "state_raw": state,
                         "occupied_raw": OCC if occ else 0, "mode_raw": 0, "litter_type_raw": 2, "_unmapped": {},
                         "slots": 4})

    def report(self, gs, seconds, peak):
        c = int(round(peak * 100))
        gs.on_visit(bytes([1, 3, seconds // 60, seconds % 60, c // 100, c % 100]))

    def go(self, gs, steps, watch=None):
        """Relative steps from the current clock: (dt, occ, scale[, state]) heartbeats or (dt, "R", seconds, peak)
        box reports."""
        for st in steps:
            self.clock[0] += st[0]
            if st[1] == "R":
                self.report(gs, st[2], st[3])
            else:
                self.hb(gs, st[2], occ=bool(st[1]), state=st[3] if len(st) > 3 else 0)
            if watch:
                watch(gs)

    @staticmethod
    def run_(base, net, n, step=10):
        """n occupied readings of a cat weighing `net` on an empty level `base`."""
        return [(step, 1, round(base + net, 2))] * n

    @staticmethod
    def empties(level, n=3):
        return [(0, 0, level)] + [(100, 0, level)] * (n - 1)

    def fields(self, gs):
        return gs.cats.fields(self.clock[0])

    def published(self, gs):
        return json.loads(gs.snapshot()[4])


class SegmentTests(PipelineCase):
    def test_single_live_reading_is_the_weight_not_the_box_report(self):
        gs = self.fresh()
        self.go(gs, self.empties(0.00, 5) + [(51, 1, 3.26), (51, 1, 0.03), (9, "R", 50, 3.28), (2, 0, 0.02)])
        v = gs.recent_visits[-1]
        self.assertEqual((v["weight"], v["kind"], v["cat"], v["peak"]), (3.26, "single", "cat_b", 3.28), v)
        self.assertIsInstance(v["conf"], int)
        self.assertLess(v["conf"], 100)                      # weight alone is never certain
        self.assertEqual(self.new_w(gs, "cat_b"), [3.26])    # the box report agrees: a weight sample
        self.assertEqual(self.new_w(gs, "cat_a"), [])
        self.assertTrue(v.get("clamped"))                    # the empty scale sat at 0.00
        self.assertEqual((gs.state["last_visit_weight"], gs.state["last_visit_peak"]), (3.26, 3.28))

    def test_two_cats_in_one_occupancy(self):
        gs = self.fresh()
        self.go(gs, self.empties(0.10, 5) + [(21, 1, 3.38), (30, 1, 3.39), (6, 1, 3.39), (8, 1, 4.35),
                                             (88, 1, 3.19), (21, 1, 3.17), (66, 1, 3.17), (15, 1, 3.18),
                                             (52, 1, 3.18), (67, 1, 3.19), (39, 1, 3.18), (14, 1, 6.37),
                                             (54, 1, 0.13), (10, "R", 425, 6.37), (1, 0, 0.13)])
        v, f = gs.recent_visits[-1], self.fields(gs)
        self.assertEqual((v["cat"], v["cats"], v["weights"], v["n"]), ("two_cats", ["cat_b", "cat_a"],
                                                                     [3.29, 3.08], [3, 7]), v)
        self.assertIsNone(v["weight"])
        self.assertIsNone(v["conf"])
        self.assertEqual((f["cat_b_visits_total"], f["cat_a_visits_total"], f["two_cat_visits_total"]), (1, 1, 1))
        self.assertEqual(f["last_visit_cat"], "Cat b + Cat a")
        self.assertIsNone(gs.state["last_visit_weight"])
        self.assertEqual((self.new_w(gs, "cat_b"), self.new_w(gs, "cat_a")), ([3.29], [3.08]))
        self.assertEqual(self.published(gs)["readings_above_window_24h"], 2)   # both-cats readings never counted

    def test_a_reading_above_the_window_is_not_the_weight(self):
        gs = self.fresh()
        self.go(gs, self.empties(1.98) + [(100, 0, 2.82), (66, 0, 4.56), (2, 1, 5.66), (14, 1, 5.34),
                                          (31, "R", 48, 3.40), (1, 0, 1.98)])
        v = gs.recent_visits[-1]
        self.assertEqual((v["weight"], v["kind"], v["cat"]), (3.36, "single", "cat_b"), v)
        self.assertEqual(self.published(gs)["readings_above_window_24h"], 1)

    def test_a_single_reading_the_box_contradicts_is_never_a_sample(self):
        gs = self.fresh()
        self.go(gs, self.empties(2.00) + [(62, 1, 5.26), (11, 1, 5.92), (32, 1, 2.03), (11, "R", 44, 4.89),
                                          (2, 0, 2.01)])
        v = gs.recent_visits[-1]
        self.assertEqual(v["weight"], 3.26, v)
        self.assertIn(v["cat"], ("cat_b", "unknown"))
        self.assertEqual((self.new_w(gs, "cat_a"), self.new_w(gs, "cat_b")), ([], []))

    def test_same_cat_on_two_plateaus_is_one_visit(self):
        gs = self.fresh(refs=(3.015, 3.29))
        self.go(gs, self.empties(0.05) + [(15, 1, round(0.05 + w, 2)) for w in (3.42, 3.43, 3.29, 3.30, 3.29)]
                + [(0, "R", 60, 3.43), (3, 0, 0.08)])
        v, f = gs.recent_visits[-1], self.fields(gs)
        self.assertEqual((v["cat"], v["weight"], v["n"]), ("cat_b", 3.29, 3), v)
        self.assertTrue(v.get("segments_merged"))
        self.assertNotIn("weights", v)
        self.assertEqual((f["two_cat_visits_total"], f["cat_b_visits_total"]), (0, 1))
        self.assertEqual(self.new_w(gs, "cat_b"), [3.29])


class OccupancyTests(PipelineCase):
    def test_occupancy_flap_holds_the_visit_and_an_unreported_visit_is_kept(self):
        gs = self.fresh()
        deposits = []
        self.go(gs, self.empties(0.04, 2) + [(100, 0, 0.05), (41, 1, 3.09), (59, 1, 3.11), (47, 1, 3.12),
                                              (72, 0, 3.11), (5, 1, 3.12), (25, 1, 3.45), (17, 0, 0.12, 1),
                                              (6, 0, 0.91, 1), (56, 0, 0.00), (100, 0, 0.00)],
                watch=lambda g: deposits.append(g.state.get("last_deposit_weight")))
        self.assertTrue(all(d is None or d <= gw.DEPOSIT_MAX_G for d in deposits), deposits)
        self.assertEqual(len(gs.recent_visits), 0)            # still waiting for a late box report
        self.assertTrue(self.events("visit_hold"))
        self.go(gs, [(101, 0, 0.00)])
        v = gs.recent_visits[-1]
        self.assertEqual((v["source"], v["cat"], v["peak"], v["deposit"]), ("gateway", "cat_a", None, 0), v)
        self.assertAlmostEqual(v["weight"], 3.06, delta=0.02)

    def test_a_short_unreported_peek_is_not_a_visit(self):
        gs = self.fresh()
        self.go(gs, [(0, 0, 0.08), (74, 0, 0.07), (8, 1, 3.34), (18, 0, 0.07), (101, 0, 0.07), (100, 0, 0.07)])
        self.assertEqual(len(gs.recent_visits), 0)
        self.assertTrue(self.events("occupancy_unreported"))

    def test_restart_mid_visit_and_flush_on_exit(self):
        gs = self.fresh()
        # the gateway (re)started with a cat already inside: no baseline -> no weight, never a sample
        self.go(gs, [(0, 1, 3.37), (30, 1, 3.38), (40, "R", 70, 3.37), (2, 0, 0.05), (100, 0, 0.05), (100, 0, 0.05)])
        self.assertEqual([(r["cat"], r["weight"]) for r in gs.recent_visits], [("unknown", None)])
        self.assertEqual((self.new_w(gs, "cat_a"), self.new_w(gs, "cat_b")), ([], []))
        # the next cat gets its own row, not the previous report
        self.go(gs, [(328, 1, 3.10), (20, 1, 3.10), (20, 1, 3.11), (20, "R", 60, 3.15), (2, 0, 0.08)])
        self.assertEqual((gs.recent_visits[-1]["cat"], gs.recent_visits[-1]["peak"], gs.recent_visits[-1]["weight"]),
                         ("cat_a", 3.15, 3.05))
        # SIGTERM with a reported visit still open: finalized, not lost
        self.go(gs, [(2938, 1, 3.35), (20, 1, 3.35), (30, "R", 50, 3.36)])
        gs.flush_inflight()
        self.assertEqual(len(gs.recent_visits), 3)
        self.assertEqual((gs.recent_visits[-1]["cat"], gs.recent_visits[-1]["weight"]), ("cat_b", 3.27))
        self.assertIsNone(gs._occ)

    def test_hold_capped_after_ten_minutes(self):
        gs = self.fresh()
        self.go(gs, self.empties(0.05, 2) + [(20, 1, 3.33), (20, 1, 3.33), (10, "R", 30, 3.34), (10, 0, 3.33),
                                              (200, 0, 3.37), (180, 0, 3.32), (221, 0, 3.38)])
        v = gs.recent_visits[-1]
        self.assertEqual((v.get("held"), v["weight"], v["cat"], v["deposit"]), (True, 3.28, "cat_b", None), v)
        self.assertIsNone(gs._occ)
        self.assertTrue(self.events("visit_hold_cap"))

    def test_a_big_deposit_settles_the_hold(self):
        gs = self.fresh(refs=(3.02, 3.29))
        self.go(gs, self.empties(2.0) + self.run_(2.0, 3.29, 9) + [(0, "R", 70, 3.29), (3, 0, 2.17), (100, 0, 2.17)])
        v = gs.recent_visits[-1]
        self.assertEqual((v["cat"], v["weight"], v["deposit"]), ("cat_b", 3.29, 170), v)
        self.assertEqual(gs.state["last_deposit_weight"], 170)

    def test_an_implausible_deposit_is_published_as_unknown(self):
        gs = self.fresh()
        self.go(gs, self.empties(0.05, 2) + [(20, 1, 3.33), (20, 1, 3.33), (10, "R", 30, 3.34), (10, 0, 0.50),
                                              (100, 0, 0.50)])
        v = gs.recent_visits[-1]
        self.assertIsNone(gs.state["last_deposit_weight"])
        self.assertEqual((v["deposit"], v["weight"]), (None, 3.28), v)
        self.assertTrue(self.events("deposit_implausible"))


class PairingTests(PipelineCase):
    def test_a_late_report_pairs_with_its_own_occupancy(self):
        gs = self.fresh()
        self.go(gs, [(0, 0, 0.05), (100, 0, 0.05), (20, 1, 3.34), (30, 1, 3.34), (30, 1, 3.35), (10, 0, 0.06),
                     (30, 1, 3.10), (20, 1, 3.10),          # the second cat enters
                     (5, "R", 45, 3.36),                    # the first cat's LATE report
                     (15, 1, 3.11), (5, "R", 30, 3.13),     # the second cat's own report
                     (15, "R", 30, 3.95),                   # a stray second report
                     (1, 0, 0.08)])
        rows = list(gs.recent_visits)
        mine = [r for r in rows if r["peak"] in (3.36, 3.13)]
        self.assertEqual([(r["cat"], r["weight"]) for r in mine], [("cat_b", 3.29), ("cat_a", 3.04)], rows)
        stray = [r for r in rows if r["peak"] == 3.95]
        self.assertEqual(len(rows), 3)
        self.assertEqual((stray[0]["cat"], stray[0]["weight"]), ("unknown", None))

    def test_a_cat_in_a_running_clean_cycle(self):
        gs = self.fresh()
        self.go(gs, self.empties(0.10) + [(10, 1, 3.15), (10, 1, 3.16), (10, 1, 3.15), (10, "R", 30, 3.20),
                                          (5, 0, 0.14)])
        self.assertEqual([r["cat"] for r in gs.recent_visits], ["cat_a"])
        # a cat steps into the running cycle; the box reports it ~40 s after the cycle resumed and ended
        self.go(gs, [(60, 0, 0.12, 1), (10, 1, 1.20, 2), (10, 0, 1.05, 1), (20, 0, 1.15), (40, "R", 25, 0.95)])
        last = gs.recent_visits[-1]
        self.assertEqual(len(gs.recent_visits), 2)
        self.assertEqual((last["cat"], last["deposit"], last.get("during_clean"), last["weight"]),
                         ("unknown", 0, True, None), last)
        self.assertEqual(gs.state["last_deposit_weight"], 0)
        # a later occupancy with no report of its own never takes that report
        self.go(gs, [(300, 1, 4.60), (5, 0, 1.15), (300, 0, 1.15)])
        self.assertEqual(len(gs.recent_visits), 2)
        self.assertEqual(self.new_w(gs, "cat_b"), [])

    def test_pairing_uses_the_span_bias(self):
        gs = self.fresh(refs=(3.02, 3.29))
        self.go(gs, self.empties(2.0) + [(10, 1, 5.29), (3, 0, 2.0), (20, 1, 5.29)] + [(10, 1, 5.29)] * 5
                + [(8, 1, 5.29), (0, "R", 26, 3.30), (3, 0, 2.0)])
        v = gs.recent_visits[-1]
        self.assertEqual((v["peak"], v["weight"], v["cat"], len(gs.recent_visits)), (3.30, 3.29, "cat_b", 1))

    def test_a_late_gateway_visit_is_inserted_in_time_order(self):
        gs = self.fresh(refs=(3.015, 3.29))
        self.go(gs, self.empties(0.05) + self.run_(0.05, 3.02, 9) + [(3, 0, 0.06), (40, 0, 0.06)]
                + self.run_(0.06, 3.29, 7) + [(0, "R", 45, 3.31), (3, 0, 0.08)])
        newest_at = gs.state.get("last_visit_at")
        self.go(gs, [(100, 0, 0.08), (100, 0, 0.08)])
        rows, f = list(gs.recent_visits), self.fields(gs)
        self.assertEqual([(r["cat"], r["source"]) for r in rows], [("cat_a", "gateway"), ("cat_b", "box")])
        self.assertEqual(gs.state.get("last_visit_at"), newest_at)   # "last visit" never goes back in time
        self.assertTrue(f["last_visit_cat"].startswith("Cat b "))
        self.assertEqual(gs.state["last_visit_weight"], 3.29)
        gs.correct_last("cat_a")                                      # the correction targets the newest visit
        self.assertEqual([(r["cat"], r["source"]) for r in gs.recent_visits], [("cat_a", "gateway"), ("cat_a", "box")])

    def test_a_late_report_upgrades_the_gateway_row_it_describes(self):
        gs = self.fresh(refs=(3.02, 3.29))
        self.go(gs, self.empties(2.0) + self.run_(2.0, 3.02, 33) + [(3, 0, 2.0), (100, 0, 2.0), (100, 0, 2.0),
                                                                   (5, "R", 300, 3.02), (100, 0, 2.0)])
        rows, f = list(gs.recent_visits), self.fields(gs)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0].get("upgraded_from"), rows[0]["peak"]), ("box", "gateway", 3.02))
        self.assertEqual((f["cat_a_visits_total"], f["unknown_visits_total"]), (1, 0))

    def test_a_report_that_does_not_overlap_never_upgrades(self):
        gs = self.fresh(refs=(3.02, 3.29))
        self.go(gs, self.empties(2.0) + self.run_(2.0, 3.02, 7, step=14) + [(5, 0, 2.0), (100, 0, 2.0), (100, 0, 2.0)]
                + self.run_(2.0, 3.29, 5, step=14))
        gs.flush_inflight()
        gs = self.state()                                             # restart on the same state files
        gs.on_box_session()
        self.go(gs, [(10, "R", 85, 3.31), (5, 0, 2.0), (100, 0, 2.0)])
        rows = list(gs.recent_visits)
        self.assertEqual([(r["cat"], r["source"], r["peak"]) for r in rows],
                         [("cat_a", "gateway", None), ("unknown", "box", 3.31)])
        self.assertEqual(self.fields(gs)["unknown_visits_total"], 1)

    def test_a_duplicate_report_is_ignored(self):
        for steps in (self.run_(2.0, 3.29, 6) + [(0, "R", 40, 3.31), (1, "R", 40, 3.31), (3, 0, 2.0)],
                      self.run_(2.0, 3.29, 6) + [(0, "R", 60, 3.31)] + self.run_(2.0, 3.29, 3)
                      + [(0, "R", 60, 3.31), (5, 0, 2.0)],
                      self.run_(2.0, 3.29, 6) + [(0, "R", 60, 3.31), (5, 0, 2.0), (20, "R", 60, 3.31)]):
            with self.subTest(steps=len(steps)):
                self.clock[0] += 86400
                for p in glob.glob(os.path.join(self.dir, "*")):
                    os.remove(p)
                gs = self.fresh(refs=(3.02, 3.29))
                self.go(gs, self.empties(2.0) + steps + [(100, 0, 2.0), (100, 0, 2.0)])
                rows = list(gs.recent_visits)
                self.assertEqual([r["cat"] for r in rows], ["cat_b"], rows)
                self.assertEqual(self.fields(gs)["unknown_visits_total"], 0)
        self.assertTrue(self.events("visit_report_duplicate"))

    def test_two_reports_in_one_occupancy_are_two_cats(self):
        gs = self.fresh(refs=(3.02, 3.29))
        self.go(gs, self.empties(2.0) + self.run_(2.0, 3.02, 6) + [(0, "R", 40, 3.05)] + self.run_(2.0, 3.29, 6)
                + [(0, "R", 40, 3.31), (3, 0, 2.0), (100, 0, 2.0)])
        rows, f = list(gs.recent_visits), self.fields(gs)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["cat"], rows[0]["cats"], rows[0].get("peaks")),
                         ("two_cats", ["cat_a", "cat_b"], [3.05, 3.31]))
        self.assertEqual((f["cat_a_visits_total"], f["cat_b_visits_total"], f["two_cat_visits_total"],
                          f["unknown_visits_total"]), (1, 1, 1, 0))

    def test_two_reports_of_one_cat_are_not_summed(self):
        gs = self.fresh(refs=(3.06, 3.29))
        before = self.fields(gs)["cat_a_visits_total"]
        self.go(gs, self.empties(2.0) + self.run_(2.0, 3.31, 5) + [(0, "R", 30, 3.31)] + self.run_(2.0, 3.13, 3)
                + [(0, "R", 20, 3.25), (4, 0, 2.0), (100, 0, 2.0)])
        rows, f = list(gs.recent_visits), self.fields(gs)
        self.assertEqual([r["cat"] for r in rows], ["cat_b", "unknown"], rows)
        self.assertEqual(rows[1]["peak"], 3.25)
        self.assertEqual((f["cat_a_visits_total"], f["two_cat_visits_total"]), (before, 0))

    def test_two_reports_of_one_cat_on_two_plateaus_give_two_rows(self):
        gs = self.fresh(refs=(3.02, 3.29))
        self.go(gs, self.empties(2.0) + self.run_(2.0, 3.25, 5) + [(0, "R", 30, 3.26)] + self.run_(2.0, 3.40, 6)
                + [(0, "R", 40, 3.41), (4, 0, 2.0), (100, 0, 2.0)])
        rows = list(gs.recent_visits)
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(rows[0]["cat"], "cat_b")
        self.assertIn(rows[0]["weight"], (3.25, 3.40))
        self.assertNotIn("weights", rows[0])
        self.assertNotIn("peaks", rows[0])
        self.assertEqual(rows[0]["duration"], "0m 30s")
        self.assertEqual((rows[1]["cat"], rows[1]["peak"]), ("unknown", 3.41))

    def test_a_still_cat_with_the_flag_off_is_one_visit(self):
        gs = self.fresh(refs=(3.02, 3.29))
        self.go(gs, self.empties(2.0) + self.run_(2.0, 3.29, 6, step=14) + [(100, 0, 5.29)] * 3
                + self.run_(2.0, 3.29, 5, step=14) + [(0, "R", 454, 3.31), (5, 0, 2.0), (100, 0, 2.0), (100, 0, 2.0)])
        rows = list(gs.recent_visits)
        self.assertEqual([(r["cat"], r["weight"]) for r in rows], [("cat_b", 3.29)])
        self.assertFalse(self.events("visit_hold_settled"))


class BaselineAndLitterTests(PipelineCase):
    def test_baseline_median_and_drop_confirmation(self):
        gs = self.fresh()
        self.go(gs, [(0, 0, 0.10), (100, 0, 0.12), (100, 0, 0.11), (100, 0, 0.65)])
        b1 = gs._empty_baseline
        self.go(gs, [(30, 0, 0.00)])                       # a one-off LOW reading (no clean cycle) ...
        b2 = gs._empty_baseline
        self.go(gs, [(10, 0, 0.11)])                       # ... followed by a normal one
        b3, cand = gs._empty_baseline, gs._low_candidate
        self.go(gs, [(60, 0, 0.00), (100, 0, 0.00)])
        self.assertEqual((b1, b2, b3, cand, gs._empty_baseline), (0.11, 0.11, 0.11, None, 0.0))
        self.go(gs, [(100, 1, 3.30), (20, 1, 3.30), (20, "R", 40, 3.31), (2, 0, 0.05)])
        v = gs.recent_visits[-1]
        self.assertEqual((v["pre"], v["baseline_unc"], v["deposit"], v.get("clamped"), v["weight"]),
                         (0.0, 0.0, 50, True, 3.30), v)

    def test_baseline_spread_is_recorded(self):
        gs = self.fresh()
        self.go(gs, [(0, 0, 0.10), (100, 0, 0.12), (100, 0, 0.11), (40, 1, 3.40), (20, 1, 3.40),
                     (20, "R", 30, 3.31), (2, 0, 0.16)])
        v = gs.recent_visits[-1]
        self.assertEqual((v["baseline_unc"], v["pre"], v.get("clamped")), (0.02, 0.11, None), v)

    def test_first_empty_after_a_clean_is_the_baseline(self):
        gs = self.fresh(refs=(3.02, 3.29))
        self.go(gs, self.empties(2.20, 4) + self.run_(2.20, 3.29, 9)
                + [(0, "R", 70, 3.29), (3, 0, 2.26), (60, 0, 2.26), (60, 0, 2.26, 1), (60, 0, 2.26, 1),
                   (100, 0, 2.05)] + self.run_(2.05, 3.29, 9) + [(0, "R", 70, 3.29), (3, 0, 2.05)])
        v = gs.recent_visits[-1]
        self.assertEqual((v["cat"], v["weight"], v["pre"]), ("cat_b", 3.29, 2.05), v)
        self.assertEqual(self.new_w(gs, "cat_a"), [])

    def test_a_visit_during_an_unconfirmed_drop_is_flagged(self):
        gs = self.fresh(refs=(3.02, 3.29))
        self.go(gs, self.empties(2.20) + [(100, 0, 2.05)] + self.run_(2.20, 3.08, 6) + [(0, "R", 50, 3.08),
                                                                                       (3, 0, 2.06)])
        v = gs.recent_visits[-1]
        self.assertEqual(v.get("baseline_alt"), 2.05, v)
        self.assertEqual((self.new_w(gs, "cat_a"), self.new_w(gs, "cat_b")), ([], []))

    def test_rezero_check_once_per_session(self):
        gs = self.fresh(litter={"offset": 3.10, "litter_weight": 3.18})
        gs.on_box_session()                                # a reconnect / restart: raw ~0.07, not a re-zero
        self.go(gs, [(0, 0, 0.09), (100, 0, 0.06), (100, 0, 0.07), (100, 0, 0.07), (100, 0, 0.07)])
        self.assertEqual(gs._litter_offset, 3.10)
        self.assertFalse(gs._new_box_session)
        self.assertFalse(self.events("litter_rezero"))

    def test_a_real_power_up_rezero_is_carried_over(self):
        gs = self.fresh(litter={"offset": 0.0, "litter_weight": 3.25})
        self.go(gs, self.empties(3.25))
        gs.on_box_session()                                # power-cycle: the scale zeroes with the litter in
        self.go(gs, [(600, 0, 0.00), (100, 0, 0.00), (100, 0, 0.00)])
        self.assertEqual((gs._litter_offset, gs.state.get("litter_weight")), (3.25, 3.25))


class StateFileTests(PipelineCase):
    def test_corrupt_files_are_moved_aside(self):
        self.fresh()
        self.drop_v2()
        with open(self.cats_file, "w") as fh:
            fh.write('{"refs": {"cat_a": 3.0, "cat_b"')
        with open(self.visits_file, "w") as fh:
            fh.write(json.dumps({"time": "Jan 7 09:15", "weight": 3.26, "cat": "cat_b", "conf": 97}) + "\n{oops\n")
        with open(self.litter_file, "w") as fh:
            fh.write("[1, 2")
        del self.logs[:]
        gs = self.state()
        moved = sorted(os.path.basename(p).split(".corrupt-")[0] for p in glob.glob(os.path.join(self.dir, "*.corrupt-*")))
        # cats.json is the tracker's read-only migration source: logged and left untouched
        self.assertEqual(moved, ["litter.json", "visits.jsonl"])
        self.assertTrue(open(self.cats_file).read().startswith('{"refs": {"cat_a": 3.0'))
        self.assertEqual(len(self.events("state_file_corrupt")), 2)
        self.assertTrue(self.events("cats_state_unreadable"))
        self.assertTrue(self.events("litter_offset_unknown"))
        f = self.fields(gs)
        self.assertEqual(len(gs.recent_visits), 1)
        self.assertEqual((f["cat_a_weight"], f["cat_b_weight"]), (None, None))   # seeds are never weights
        self.assertEqual(f["last_visit_cat"], "Cat b 97%")
        gs.cats.save()
        gs._persist_visits()
        json.load(open(gw.v2_path_for(self.cats_file)))
        self.assertEqual(glob.glob(os.path.join(self.dir, "*.tmp")), [])

    def test_litter_reset_after_an_unknown_offset(self):
        with open(self.litter_file, "w") as fh:
            fh.write("{bad")
        gs = self.fresh()
        self.go(gs, self.empties(0.40, 4))
        self.assertNotIn("litter_weight", gs.state)        # offset unknown: never a ~0 kg litter weight
        gs.litter_reset()
        self.go(gs, [(100, 0, 0.40)] * 3)
        self.assertEqual(gs.state.get("litter_weight"), 0.40)

    def test_correct_last_after_a_restart(self):
        gs = self.fresh()
        self.go(gs, self.empties(0.05, 2) + [(20, 1, 3.22), (20, 1, 3.22), (20, "R", 40, 3.23), (2, 0, 0.05)])
        first = dict(gs.recent_visits[-1])
        gs2 = self.state()
        self.assertIsNotNone(gs2.correct_last("cat_b"))
        self.assertEqual(first["cat"], "unknown")
        self.assertEqual(gs2.recent_visits[-1]["cat"], "cat_b")
        self.assertEqual(self.new_w(gs2, "cat_b"), [3.17])
        with open(self.visits_file) as fh:
            self.assertEqual(json.loads(fh.read().splitlines()[-1])["cat"], "cat_b")

    def test_bad_values_in_memory_still_publish_strict_json(self):
        gs = self.fresh()
        gs.cats.tracks["cat_a"]["last_seen"] = float("inf")
        gs.cats.tracks["cat_b"]["w"] = float("nan")
        gs.cats.samples["cat_b"] = [[self.clock[0], float("nan"), 2]]
        gs.cats.last_fields = {"last_cat": "cat_b", "last_conf": "97", "last_visit_cat": "Cat b"}
        payload = gs.snapshot()[4]
        pj = json.loads(payload, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        # the tracker's error fallback is published as "unavailable", never a status it does not know
        self.assertEqual((pj["cat_status"], pj["cat_a_stale_reason"], pj["cat_a_weight"]),
                         ("unavailable", "unavailable", None))

    def test_downgrade_then_upgrade_stale_guard(self):
        def downgrade_then(edit):
            for p in glob.glob(os.path.join(self.dir, "*")):
                os.remove(p)
            write_v1(self.cats_file, (3.04, 3.30), self.clock[0],
                     extra={"visits_total": {"cat_a": 5, "cat_b": 9}, "total_visits": 14, "labelled_visits": 14})
            self.state()
            stamp = json.load(open(gw.v2_path_for(self.cats_file)))["v1"].get("cats_json_sha256")
            data = json.load(open(self.cats_file))
            edit(data)                                    # what the older version wrote meanwhile
            with open(self.cats_file, "w") as fh:
                json.dump(data, fh)
            del self.logs[:]
            self.state()
            return stamp, bool(self.events("cats_v2_stale"))
        stamp, stale = downgrade_then(lambda d: d["refs"].update(cat_a=3.20))      # a weigh-in only
        self.assertEqual(len(stamp or ""), 64)
        self.assertTrue(stale)
        self.assertTrue(self.events("cats_state_migrated"))
        self.assertTrue(downgrade_then(lambda d: d.update(correction_count=1))[1])  # a correction only
        self.assertFalse(downgrade_then(lambda d: None)[1])                          # nothing written
        # a v2 without the stamp falls back to the migrated counts, normalised like the tracker
        for p in glob.glob(os.path.join(self.dir, "*")):
            os.remove(p)
        write_v1(self.cats_file, (3.04, 3.30), self.clock[0], extra={"total_visits": "x", "labelled_visits": 12})
        self.state()
        v2p = gw.v2_path_for(self.cats_file)
        data = json.load(open(v2p))
        data["v1"].pop("cats_json_sha256", None)
        with open(v2p, "w") as fh:
            json.dump(data, fh)
        del self.logs[:]
        self.state()
        self.assertFalse(self.events("cats_v2_stale"))
        cur = json.load(open(self.cats_file))
        cur["labelled_visits"] = 13
        with open(self.cats_file, "w") as fh:
            json.dump(cur, fh)
        del self.logs[:]
        self.state()
        self.assertTrue(self.events("cats_v2_stale"))

    def test_corrupt_v2_is_quarantined_and_the_tracker_starts_fresh(self):
        gs = self.fresh()
        with open(gw.v2_path_for(self.cats_file), "w") as fh:
            fh.write('{"version": 2, "tracks": [1, 2]')
        del self.logs[:]
        gs = self.state()
        f = self.fields(gs)
        self.assertTrue(glob.glob(gw.v2_path_for(self.cats_file) + ".corrupt-*"))
        self.assertEqual((f["cat_status"], f["cat_a_weight"]), ("learning", None))
        self.assertFalse(self.events("cats_state_migrated"))       # no silent revert to install-time state
        v2 = json.load(open(gw.v2_path_for(self.cats_file)))
        self.assertEqual(v2["v1"].get("cats_json_sha256"), gw.GatewayState._file_sha256(self.cats_file))

    def test_cats_json_is_never_written(self):
        gs = self.fresh()
        before = open(self.cats_file, "rb").read()
        self.go(gs, self.empties(0.05, 2) + [(20, 1, 3.35), (20, 1, 3.35), (20, "R", 40, 3.36), (2, 0, 0.05)])
        gs.correct_last("cat_a")
        gs.set_ref("cat_b", "3.31")
        self.assertEqual(open(self.cats_file, "rb").read(), before)
        self.assertTrue(os.path.exists(gw.v2_path_for(self.cats_file)))


class HaFieldTests(PipelineCase):
    @classmethod
    def setUpClass(cls):
        cls._cfg = gw.CFG
        env = {"CATLINK_BOX_MAC": "02:ca:7b:0c:00:01", "CATLINK_BOX_BIND_IP": "127.0.0.1",
               "CATLINK_CATS": "cat_a=3.00,cat_b=3.30"}
        gw.configure(config.load("gateway", env))

    @classmethod
    def tearDownClass(cls):
        if cls._cfg is not None:
            gw.configure(cls._cfg)

    def items(self):
        return {oid: cfg for comp, oid, cfg in gw.sensor_discovery()}

    def test_cat_entities(self):
        items = self.items()
        for c in ("cat_a", "cat_b"):
            w = items[c + "_weight"]
            self.assertEqual((w["unit_of_measurement"], w["state_class"], w["state_topic"], w["json_attributes_topic"]),
                             ("kg", "measurement", gw.CATS_TOPIC, gw.CATS_TOPIC))
            for s in ("_weight_lo", "_weight_hi"):
                self.assertEqual((items[c + s]["unit_of_measurement"], items[c + s]["state_class"]), ("kg", "measurement"))
            self.assertEqual(items[c + "_visits"]["state_class"], "total_increasing")
        self.assertEqual(items["unknown_visits"]["state_class"], "total_increasing")
        self.assertEqual(items["two_cat_visits"]["state_class"], "total_increasing")
        self.assertIn("Weight changed suddenly", items["cat_a_stale_reason"]["value_template"])
        self.assertIn("'needs_weigh_in': 'Weigh every cat'", items["cat_status"]["value_template"])
        for oid in ("cluster_low", "cluster_high", "cluster_low_n", "cluster_high_n", "visits_24h_total",
                    "readings_above_window_24h", "scale_shift", "cat_a_last_weigh_in", "cat_b_stale_reason"):
            self.assertEqual(items[oid]["state_topic"], gw.CATS_TOPIC, oid)
        for oid in ("visits_24h_total", "readings_above_window_24h", "scale_shift", "cluster_low_n"):
            self.assertEqual(items[oid].get("entity_category"), "diagnostic", oid)
        self.assertFalse([o for o, c in items.items() if c.get("json_attributes_topic") == gw.STATE_TOPIC])
        self.assertIn("last_visit_peak", items["last_visit_peak"]["value_template"])
        # entity ids of 0.1 are kept (dashboards keep working)
        self.assertLessEqual({"cat_a_weight", "cat_b_weight", "cat_a_trend_7d", "cat_gap", "last_visit_cat",
                              "labelled_pct", "correction_count"}, set(items))

    def test_one_cat_has_no_pair_entities(self):
        gw.configure(config.load("gateway", {"CATLINK_BOX_MAC": "02:ca:7b:0c:00:01",
                                             "CATLINK_BOX_BIND_IP": "127.0.0.1", "CATLINK_CATS": "solo=4.10"}))
        try:
            items = self.items()
            self.assertIn("solo_weight", items)
            self.assertFalse({"cat_gap", "cluster_low", "two_cat_visits"} & set(items))
        finally:
            self.setUpClass()

    def test_needs_weigh_in_and_a_weigh_in_session(self):
        gs = self.fresh(refs=(3.04, 3.30))
        for c in ("cat_a", "cat_b"):
            gs.cats._mark_lost(c, self.clock[0])
        f = self.published(gs)
        self.assertEqual((f["cat_status"], f["cat_a_weight"], f["cat_b_weight"], f["cat_a_stale_reason"]),
                         ("needs_weigh_in", None, None, "not_identified"))
        saved, gw.GW = gw.GW, gs
        try:
            self.clock[0] += 3600
            gw.handle_command("set_ref_cat_a", "3.07")       # HA helper -> MQTT set_ref_<slug> -> tracker
            f1 = self.published(gs)
            self.clock[0] += 300
            gw.handle_command("set_ref_cat_b", "3.32")
            f2 = self.published(gs)
        finally:
            gw.GW = saved
        self.assertTrue((f1["cat_a_last_weigh_in"] or "").startswith("pending"), f1["cat_a_last_weigh_in"])
        self.assertEqual(f1["cat_status"], "needs_weigh_in")
        self.assertEqual(f2["cat_status"], "ok")
        self.assertAlmostEqual(f2["cat_a_weight"], 3.07, delta=0.02)
        self.assertAlmostEqual(f2["cat_b_weight"], 3.32, delta=0.02)
        self.assertTrue(f2["cat_b_last_weigh_in"].startswith("accepted"))
        self.assertTrue(self.events("cat_command"))

    def test_weight_interval(self):
        gs = self.fresh(refs=(3.04, 3.30))
        self.go(gs, self.empties(0.05, 2) + [(20, 1, 3.40), (20, 1, 3.40), (10, "R", 20, 3.36), (2, 0, 0.05)])
        f = self.published(gs)
        self.assertIsNotNone(f["cat_b_weight"])
        self.assertLessEqual(f["cat_b_weight_lo_kg"], f["cat_b_weight"])
        self.assertLessEqual(f["cat_b_weight"], f["cat_b_weight_hi_kg"])
        self.assertLessEqual(f["cat_b_weight_hi_kg"] - f["cat_b_weight_lo_kg"], 0.301)

    def test_scale_shift_is_published(self):
        gs = self.fresh()
        f = self.published(gs)
        self.assertIn("scale_shift", f)
        self.assertIsNone(f["scale_shift"])
        gs.cats.scale_events.append([self.clock[0] - 600, "cat_a", 52])
        gs.cats.save()
        f = self.published(gs)
        self.assertTrue(f["scale_shift"].startswith("scale_shift +52 g "), f["scale_shift"])
        self.assertEqual(self.state().cats.fields(self.clock[0])["scale_shift"], f["scale_shift"])

    def test_reason_texts_get_one_example(self):
        cases = {
            "rejected 3.30 kg 2030-03-01T09:00: the box disagrees: weigh both cats, then enter it again changed by "
            "0.01 kg": "then enter it again changed by 0.01 kg (e.g. 3.31 for 3.30)",
            "pending 3.30 kg 2030-03-01T09:00: -40 g from the box weight; enter it again changed by 0.01 kg within "
            "24 h to confirm": "enter it again changed by 0.01 kg (e.g. 3.31 for 3.30) within 24 h to confirm",
            "rejected 3.05 kg 2030-03-01T09:00: matches Cat a's box weight; weigh Cat a too":
                "matches Cat a's box weight; weigh Cat a too",
        }
        for text, want in cases.items():
            once = gw._ha_cat_fields({"cat_b_last_weigh_in": text, "cat_b_visits_total": 1}, ("cat_b",))
            twice = gw._ha_cat_fields(once, ("cat_b",))
            self.assertTrue(once["cat_b_last_weigh_in"].endswith(want), once)
            self.assertEqual(once, twice)
            self.assertLessEqual(once["cat_b_last_weigh_in"].count("(e.g."), 1)

    def test_a_wild_weigh_in_is_rejected_with_a_reason(self):
        gs = self.fresh(refs=(3.04, 3.30))
        gs.set_ref("cat_b", "0.3")
        f = self.published(gs)
        self.assertTrue(f["cat_b_last_weigh_in"].startswith("rejected"), f["cat_b_last_weigh_in"])
        self.assertIn("enter it again changed by 0.01 kg (e.g. 0.31 for 0.30)", f["cat_b_last_weigh_in"])
        f2 = self.state().cats.fields(self.clock[0])
        self.assertAlmostEqual(f2["cat_b_weight"] or 0, 3.30, delta=0.03)
        self.assertEqual(glob.glob(os.path.join(self.dir, "*.corrupt-*")), [])


class CatCountTests(PipelineCase):
    def test_no_cats_still_weighs_visits(self):
        gs = gw.GatewayState(self.visits_file, (), self.cats_file, self.litter_file)
        self.go(gs, self.empties(0.50) + [(20, 1, 4.60), (20, 1, 4.60), (20, "R", 40, 4.30), (2, 0, 0.52)])
        v = gs.recent_visits[-1]
        self.assertEqual((v["weight"], v["source"]), (4.10, "box"), v)
        self.assertNotIn("cat", v)
        self.assertIsNone(gs.snapshot()[4])
        self.assertIsNone(gs.correct_last("x"))
        self.assertFalse(os.path.exists(self.cats_file) or os.path.exists(gw.v2_path_for(self.cats_file)))

    def test_one_cat(self):
        gs = self.fresh(refs=(4.10,), cats=[("solo", 4.0)])
        self.go(gs, self.empties(0.50) + [(20, 1, 4.60), (20, 1, 4.60), (20, "R", 40, 4.64), (2, 0, 0.52)])
        v, f = gs.recent_visits[-1], self.fields(gs)
        self.assertEqual((v["weight"], v["cat"]), (4.10, "solo"), v)
        self.assertEqual(f["solo_visits_total"], 1)
        self.assertIsNone(f["cat_gap"])

    def test_three_cats(self):
        cats3 = [("cat_a", 3.0), ("cat_b", 3.6), ("cat_c", 4.4)]
        gs = self.fresh(refs=(3.05, 3.60, 4.40), cats=cats3)
        self.go(gs, self.empties(0.50) + [(20, 1, 4.90), (20, 1, 4.90), (20, "R", 40, 4.92), (2, 0, 0.52)])
        self.go(gs, [(300, 1, 3.55), (20, 1, 3.55), (20, "R", 40, 3.56), (2, 0, 0.52)])
        rows = list(gs.recent_visits)
        self.assertEqual([(r["cat"], r["weight"]) for r in rows], [("cat_c", 4.40), ("cat_a", 3.03)], rows)   # 3.55 on the new 0.52 level
        f = self.published(gs)
        self.assertEqual((f["cat_c_visits_total"], f["cat_a_visits_total"], f["cat_b_visits_total"]), (1, 1, 0))


if __name__ == "__main__":
    unittest.main()
