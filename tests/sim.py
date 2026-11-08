"""Synthetic visit streams for the cat tracker tests (catlink_local/cats.py). Noise model shaped like real box
data (not real data):
  * whole-cat plateau median = true + N(0, 25 g), rounded to the 10 g scale step
  * ~30 % of visits have a single sample (n=1); the box report agrees within 50 g for 85 % of them,
    otherwise it is off by 0.1-1.0 kg
  * ~10 % not-one-cat visits: partial support (true - 50..250 g), extra load (true + 100..400 g), wild
  * ~5 % visits with no usable sample at all (box report only), ~3 % two-cat occupancies (two segments, box
    report = both cats' weight), ~3 % ONE cat on two plateaus (perched on the rim 120-300 g low, then in)
  * baseline uncertainty 0-30 g, 25 % clamped baselines; visit rates per cat as given
The gateway pipeline is emulated: a segment outside tracker.plausible_range(now) is dropped.
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from catlink_local import cats as T  # noqa: E402

DAY = 86400.0
T0 = 1.8e9

SD = 0.025
P_SINGLE = 0.30
P_AGREE = 0.85
P_NOTONE = 0.10
P_NOSEG = 0.05
P_TWO = 0.03
P_SAME2 = 0.03


def lin(w0, g_week):
    return lambda d: w0 + g_week / 1000.0 * d / 7.0


def r2(x):
    return round(x + 1e-9, 2)


def gen_visit(rng, cat, true, others):
    """Raw visit description independent of the tracker: list of segment dicts with truth, + peak."""
    u = rng.random()
    if u < P_NOSEG:
        return {"segs": [], "peak": r2(true + rng.uniform(-0.05, 0.5)), "truth": [cat], "notone": False}
    if P_NOSEG + P_TWO <= u < P_NOSEG + P_TWO + P_SAME2:
        perched = r2(true - rng.uniform(0.12, 0.30) + rng.gauss(0, 0.01))
        whole = r2(true + rng.gauss(0, SD))
        segs = [{"w": perched, "n": rng.randint(2, 4), "truth": cat, "notone": True},
                {"w": whole, "n": rng.randint(2, 6), "truth": cat, "notone": False}]
        return {"segs": segs, "peak": r2(true + rng.uniform(-0.05, 0.3)), "truth": [cat, cat], "notone": False,
                "same2": True}
    if u < P_NOSEG + P_TWO and others:
        oc, ot = rng.choice(others)
        segs = []
        for c, tw in rng.sample([(cat, true), (oc, ot)], 2):
            segs.append({"w": r2(tw + rng.gauss(0, SD)), "n": rng.randint(2, 6), "truth": c, "notone": False})
        return {"segs": segs, "peak": r2(true + ot), "truth": [s["truth"] for s in segs], "notone": False}
    notone = rng.random() < P_NOTONE
    if notone:
        k = rng.random()
        if k < 0.45:
            w = true - rng.uniform(0.05, 0.25)
        elif k < 0.80:
            w = true + rng.uniform(0.10, 0.40)
        else:
            w = rng.uniform(1.6, 2.9)
    else:
        w = true + rng.gauss(0, SD)
    w = r2(w)
    n = 1 if rng.random() < P_SINGLE else rng.randint(2, 8)
    if n == 1:
        pd = rng.uniform(-0.05, 0.05) if rng.random() < P_AGREE else rng.choice([-1, 1]) * rng.uniform(0.1, 1.0)
    else:
        pd = rng.uniform(-0.06, 0.5)
    return {"segs": [{"w": w, "n": n, "truth": cat, "notone": notone, "pd": pd}], "peak": r2(w + pd),
            "truth": [cat], "notone": notone}


def obs_for_new(v, t, tr, bunc, clamped):
    lo, hi = tr.plausible_range(t)
    segs = []
    for s in v["segs"]:
        if lo - 1e-9 <= s["w"] <= hi + 1e-9:
            segs.append({"w": s["w"], "n": s["n"], "spread": 0.0 if s["n"] == 1 else 0.02,
                         "peak_delta": round(v["peak"] - s["w"], 3) if s["n"] == 1 else None})
    if len(v["segs"]) == 1 and v["segs"][0]["n"] == 1 and segs:
        segs[0]["peak_delta"] = round(v["segs"][0]["pd"], 3)
    kept_truth = [s["truth"] for s in v["segs"] if lo - 1e-9 <= s["w"] <= hi + 1e-9]
    return ({"t": t, "duration_s": 60, "source": "box", "segments": segs, "peak": v["peak"],
             "baseline_unc": bunc, "clamped": clamped, "orphan": False, "during_clean": False}, kept_truth)


def run(truths, days, rates, seed, cats=("cat_a", "cat_b"), seeds=None, weighins=(), start_new=None,
        snap_every=1.0):
    """truths: {cat: f(day)->kg}; rates {cat: visits/day}. Returns dict with logs + snapshots."""
    rng = random.Random(seed)
    seeds = seeds or {c: truths[c](0) for c in cats}
    new = start_new() if start_new else T.CatTracker(cats, seeds, None, now=T0)
    events = []
    for c in cats:
        t = 0.0
        while True:
            t += rng.expovariate(rates[c])
            if t >= days:
                break
            events.append((t, 1, "visit", c))
    for d, c, kg in weighins:
        events.append((d, 0, "weighin", (c, kg)))
    k = 0
    while k * snap_every <= days:
        events.append((k * snap_every - 1e-7, 2, "snap", None))
        k += 1
    events.sort(key=lambda e: (e[0], e[1]))
    nlog, snaps = [], []
    for d, _, kind, arg in events:
        t = T0 + d * DAY
        if kind == "visit":
            cat = arg
            true = truths[cat](d)
            others = [(c, truths[c](d)) for c in cats if c != cat]
            v = gen_visit(rng, cat, true, others)
            bunc = round(rng.uniform(0, 0.03), 2)
            clamped = rng.random() < 0.25
            obs, kept = obs_for_new(v, t, new, bunc, clamped)
            rec = {}
            new.on_visit(obs, rec, now=t)
            # per-segment labels from the tracker (a merged one-cat visit labels the plateau it kept)
            if new.last and new.last.get("t") == t and len(new.last.get("labels") or []) == len(kept):
                labels, confs = new.last["labels"], new.last.get("confs") or []
            else:
                labels, confs = rec.get("cats") or [rec["cat"]], []
            for i, tc in enumerate(kept):
                lab = labels[i] if i < len(labels) else "unknown"
                cf = confs[i] if confs and i < len(confs) else None
                nlog.append({"d": d, "truth": tc, "label": lab, "conf": cf, "notone": v["notone"],
                             "single": obs["segments"][i]["n"] == 1, "same2": bool(v.get("same2"))})
            if not kept:
                nlog.append({"d": d, "truth": v["truth"][0], "label": rec["cat"], "conf": None,
                             "notone": v["notone"], "noseg": True})
        elif kind == "weighin":
            new.set_ref(arg[0], arg[1], now=t)
        else:
            f = new.fields(now=t)
            s = {"d": round(d), "status": f["cat_status"], "new": {}, "true": {}, "f": f}
            for c in cats:
                s["true"][c] = truths[c](d)
                s["new"][c] = (f[c + "_weight"], f[c + "_growth_g_week"], f[c + "_trend_word"])
            snaps.append(s)
    return {"nlog": nlog, "snaps": snaps, "new": new}


def label_stats(log, d0=0, d1=1e9, cats=None):
    rows = [r for r in log if d0 <= r["d"] < d1 and not r.get("noseg")]
    n = len(rows)
    lab = [r for r in rows if r["label"] not in ("unknown", "two_cats")]
    wrong = [r for r in lab if r["label"] != r["truth"]]
    return {"n": n, "labelled": len(lab), "wrong": len(wrong),
            "wrong_pct": 100.0 * len(wrong) / n if n else 0.0,
            "wrong_of_labelled_pct": 100.0 * len(wrong) / len(lab) if lab else 0.0,
            "unknown_pct": 100.0 * (n - len(lab)) / n if n else 0.0}
