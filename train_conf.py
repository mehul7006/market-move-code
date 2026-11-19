"""Train the CONFIDENCE-tier model (conf_model.json) - the calls on the "confidence calls" page, which are shown but never counted.

    python train_conf.py

The sure-call logic (strict_model.json) is locked and NOT touched. This tier takes every signal from 09:25 to 15:15 that is not a sure call,
scores it with its own per-index model and keeps the loosest cut-off whose calls still reached T1 before the stop at least 55% of the time with a
positive net in every block - checked out of sample (trained on earlier data, three 30-day blocks) AND on a 120-day replay. Entries are at the signal
close, levels are the index's sure-call levels. One open confidence call per index at a time, at most 8 a day.
"""
import collections
import datetime
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import server
import shortterm as st
import strict
import train_strict as ts

CONF_PATH = os.path.join(ROOT, "conf_model.json")
# per index, chosen by the walk-forward search (see README): model target, candle sizes, share of signals kept
CONF = {"NIFTY": {"label": "pnl", "tfs": (1, 5), "share": 0.15},
        "BANKNIFTY": {"label": "win", "tfs": (1, 5), "share": 0.05},
        "SENSEX": {"label": "win", "tfs": (1, 5), "share": 0.40}}
MIN_WIN = 55.0
MAX_PER_DAY = 8
DAY = 86400


def mins(t):
    return ((t + 19800) % DAY) // 60


def select(cands):
    per, blocked, out = collections.Counter(), -1, []
    for x in sorted(cands, key=lambda c: (c["done"], -c["cs"])):
        if x["done"] < blocked:
            continue
        d = (x["t"] + 19800) // DAY
        if per[d] >= MAX_PER_DAY:
            continue
        per[d] += 1
        blocked = x["end"]
        out.append(x)
    return out


def main():
    server._sync_token_from_env()
    now = time.time()
    sure = strict.model()
    out, report = {}, []
    P = lambda s="": (print(s), report.append(s))
    P(f"CONFIDENCE-TIER TRAINING - {datetime.datetime.fromtimestamp(now + 19800, datetime.timezone.utc):%Y-%m-%d %H:%M} IST (sure-call model untouched)")
    for seg in strict.SEGMENTS:
        inst = next(x for x in server.INSTRUMENTS if x["id"] == seg)
        c5, m1 = server._candles_days(inst, 300), server._hist_1m(inst)
        m1t = [c["t"] for c in m1]
        series = {1: m1, 5: c5, 15: st.resample(c5, 15)}
        T1, S = ts.LEVELS[seg]
        cfg = CONF[seg]
        cands = []
        for tf in strict.TFS:
            cs = series[tf]
            for x in strict.candidates(cs, tf, series[strict.HTF[tf]], last_entry=strict.CONF_LAST_ENTRY):
                x["entry"] = cs[x["i"]]["close"]
                r = ts.outcome(m1, m1t, x, T1, S)
                if r is None:
                    continue
                x["pnl"], x["win"] = r
                cands.append(x)
        mdl = sure[seg]
        sure_c = [dict(x, score=strict.score(mdl, x["feat"])) for x in cands if strict.allowed(seg, x) and mins(x["t"]) <= strict.LAST_ENTRY]
        sure_ids = {(p["tf"], p["t"]) for p in strict.select(sure_c, mdl) if strict.passes(mdl, p)}
        pool = [x for x in cands if (x["tf"], x["t"]) not in sure_ids and x["tf"] in cfg["tfs"]]
        target = (lambda x: 1.0 if x["win"] else 0.0) if cfg["label"] == "win" else (lambda x: x["pnl"] / S)
        last = max(x["t"] for x in cands)
        folds = [(last - (90 - 30 * k) * DAY, last - (60 - 30 * k) * DAY) for k in range(3)]
        folds[-1] = (folds[-1][0], last + 1)
        P("")
        P(f"== {seg}: T1 {T1} / stop {S}; model target {cfg['label']}, candles {cfg['tfs']}, top {cfg['share']:.0%} of scores, 09:25-15:15, not sure calls")
        oos = []
        for lo, hi in folds:
            train = [x for x in cands if x["end"] < lo]
            m = ts.fit_ridge([x["feat"] for x in train], [target(x) for x in train])
            sc = sorted(strict.score(m, x["feat"]) for x in train)
            thr = sc[int((1 - cfg["share"]) * len(sc))]
            picks = select([dict(x, cs=strict.score(m, x["feat"])) for x in pool if lo <= x["t"] < hi and strict.score(m, x["feat"]) >= thr])
            oos += picks
            n = len(picks)
            P(f"   test block {datetime.datetime.fromtimestamp(lo + 19800, datetime.timezone.utc):%Y-%m-%d}: {n} calls, win {sum(p['win'] for p in picks) / max(n, 1) * 100:.1f}%, "
              f"net {sum(p['pnl'] for p in picks) / max(n, 1):+.1f}")
        n = len(oos)
        stats = {"calls": n, "winRate": round(sum(p["win"] for p in oos) / max(n, 1) * 100, 1), "netPerCall": round(sum(p["pnl"] for p in oos) / max(n, 1), 1)}
        P(f"   OUT-OF-SAMPLE: {n} calls, win {stats['winRate']}%, net {stats['netPerCall']:+} per call" + ("" if stats["winRate"] >= MIN_WIN else "   <-- BELOW 55%"))
        m = ts.fit_ridge([x["feat"] for x in cands], [target(x) for x in cands])
        sc = sorted(strict.score(m, x["feat"]) for x in cands)
        m.update({"thr": sc[int((1 - cfg["share"]) * len(sc))], "label": cfg["label"], "tfs": list(cfg["tfs"]), "share": cfg["share"],
                  "lastEntry": strict.CONF_LAST_ENTRY, "maxPerDay": MAX_PER_DAY, "outOfSample": stats, "features": strict.FEATURE_NAMES,
                  "trainedThrough": datetime.datetime.fromtimestamp(last + 19800, datetime.timezone.utc).strftime("%Y-%m-%d")})
        out[seg] = m
    with open(CONF_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    with open(os.path.join(ROOT, "conf_training_report.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")
    P(f"\nWrote {CONF_PATH}")


if __name__ == "__main__":
    main()
