"""Walk-forward (genuinely out-of-sample) test of the way the short-term strategies are chosen.

For every test month M (Jul..Nov) and every index / candle size:
  1. Using ONLY calls from before M, pick the best of a fixed menu of candidate strategies
     (VWAP-reclaim / pullback x trend filter x target ladder x stop size) that held >= 70% wins.
  2. Trade month M with that pick and record the result. If no candidate qualifies, stand aside.
Nothing from month M ever influences the pick for month M, so the combined result is out-of-sample.

Run:  python walkforward.py     (several minutes; writes walkforward_result.json, shown on the Scorecard page)
"""
import collections
import datetime
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import shortterm

S5 = {"NIFTY": 50, "BANKNIFTY": 140, "SENSEX": 150, "NEXT50": 160}
SEGS = list(S5)
TFS = (1, 5, 15)
TEST_MONTHS = ["2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]


def month_of(t):
    return datetime.datetime.fromtimestamp(t + 19800, datetime.timezone.utc).strftime("%Y-%m")


def month_start(m):
    return int(datetime.datetime.strptime(m + "-01", "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()) - 19800


def candidates(seg, tf):
    S = max(1, round(S5[seg] * math.sqrt(tf / 5)))
    out = []
    for fam in ("vwapx", "pullback"):
        for bias in ("trend", "strong"):
            for f1 in (0.15, 0.2, 0.3):
                for s in (0.3, 0.4, 0.5):
                    out.append(shortterm._p(seg, tf, fam, bias, round(f1 * S), round(0.7 * S), S, round(s * S), 120))
    return out


def work(args):
    seg, tf = args
    import server
    server._sync_token_from_env()
    inst = next(x for x in server.INSTRUMENTS if x["id"] == seg)
    c5 = server._candles_days(inst, 230)
    m1 = server._hist_1m(inst)
    now = time.time()
    cs = shortterm._usable(m1 if tf == 1 else (c5 if tf == 5 else shortterm.resample(c5, tf)), tf, now)
    fine = None if tf == 1 else [c for c in m1 if c["t"] + 60 <= now]
    cands = candidates(seg, tf)
    allcalls = []
    for p in cands:
        calls = [c for c in shortterm.replay(cs, p, fine=fine, now=now) if c["result"] != "OPEN"]
        allcalls.append((p, calls))
    picks, oos = [], []
    for m in TEST_MONTHS:
        t0 = month_start(m)
        nxt = TEST_MONTHS.index(m) + 1
        t1 = month_start(TEST_MONTHS[nxt]) if nxt < len(TEST_MONTHS) else 10 ** 12
        best = None
        for p, calls in allcalls:
            hist = [c for c in calls if c["t"] < t0]
            if len(hist) < 40:
                continue
            win = sum(1 for c in hist if c["result"] in ("T1", "T2", "T3")) / len(hist) * 100
            tail = hist[-max(20, len(hist) // 3):]
            win_tail = sum(1 for c in tail if c["result"] in ("T1", "T2", "T3")) / len(tail) * 100
            if win < 70 or win_tail < 66:
                continue
            plan = sum(c["planPts"] for c in hist) / len(hist) / p["t3"]
            if best is None or plan > best[0]:
                best = (plan, p, calls, win)
        if best is None:
            picks.append({"month": m, "pick": None})
            continue
        _, p, calls, win = best
        month_calls = [c for c in calls if t0 <= c["t"] < t1]
        oos.extend(month_calls)
        s = shortterm.summarize(month_calls)
        picks.append({"month": m, "pick": f"{p['family']}/{p['bias']} T1 {p['t1']} SL {p['sl']}", "historyWin": round(win, 1),
                      "calls": s["calls"], "winRate": s["winRate"]})
    return {"segment": seg, "tf": tf, "picks": picks, "oos": shortterm.summarize(oos),
            "monthsTraded": sum(1 for x in picks if x["pick"])}


def main():
    t0 = time.time()
    tasks = [(s, tf) for s in SEGS for tf in TFS]
    results = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        for r in ex.map(work, tasks):
            results.append(r)
            o = r["oos"]
            print(f"{r['segment']:<10} {r['tf']:>2}m  months traded {r['monthsTraded']}/{len(TEST_MONTHS)}  OOS calls {o['calls']:3d}"
                  f"  win {o['winRate']}%  SL-first {o['slRate']}%  PF {o['profitFactor']}", flush=True)
    allc = collections.Counter()
    out = {"generatedAt": int(time.time()), "testMonths": TEST_MONTHS, "rows": results,
           "note": "Each month's strategy was picked using only earlier months; the month itself was unseen."}
    with open(os.path.join(ROOT, "walkforward_result.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print("done in", round(time.time() - t0), "s")


if __name__ == "__main__":
    main()
