"""Train the strict call-selection model (strict_model.json) on REAL Upstox candles and print its walk-forward test.

    python train_strict.py

For each index: every raw signal of the six families on 1/5/15-minute candles gets a feature vector (strict.features) and a single-lot
outcome on the real 1-minute path (T1 points for a win, a stop twice as far, stop to entry after T1, followed for the call window). A ridge regression
learns the outcome in R-multiples. The walk-forward test trains on everything BEFORE each 30-day block and issues calls in that
block with the same rules the app uses (score >= threshold, one open call per index, max 3 a day) - the numbers printed are the
honest out-of-sample ones. The shipped model is then fitted on all data up to now.
"""
import bisect
import collections
import datetime
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import server
import shortterm as st
import strict

LEVELS = {"NIFTY": (22.5, 25), "BANKNIFTY": (63, 70), "SENSEX": (81, 90)}   # (T1, stop) in index points: T1 is at least 90% of the stop
TOP_SHARE = {"NIFTY": 0.10, "BANKNIFTY": 0.05, "SENSEX": 0.40}      # A tier: share of signals that become calls
TOP_SHARE_B = {"NIFTY": 0.15, "BANKNIFTY": None, "SENSEX": 0.50}   # B tier (looser filter, a few extra lower-confidence calls); Bank Nifty has none - its extra calls lost money
LABEL = {"NIFTY": "win", "BANKNIFTY": "win", "SENSEX": "pnl"}      # what the model learns: profit in stop units, or simply "T1 before the stop"


def target(x, seg):
    return (1.0 if x["win"] else 0.0) if LABEL[seg] == "win" else x["pnl"] / LEVELS[seg][1]    # share of all signals that is allowed to become a call
DAY = 86400


def fit_ridge(X, y, lam=30.0):
    n, d = len(X), len(X[0])
    mu = [sum(r[j] for r in X) / n for j in range(d)]
    sd = [max(1e-6, math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / n)) for j in range(d)]
    Z = [[(r[j] - mu[j]) / sd[j] for j in range(d)] + [1.0] for r in X]
    d1 = d + 1
    A = [[0.0] * d1 for _ in range(d1)]
    b = [0.0] * d1
    for z, yy in zip(Z, y):
        for p in range(d1):
            zp = z[p]
            b[p] += zp * yy
            Ap = A[p]
            for q in range(p, d1):
                Ap[q] += zp * z[q]
    for p in range(d1):
        for q in range(p):
            A[p][q] = A[q][p]
    for p in range(d):
        A[p][p] += lam
    M = [A[i][:] + [b[i]] for i in range(d1)]
    for i in range(d1):
        piv = max(range(i, d1), key=lambda r: abs(M[r][i]))
        M[i], M[piv] = M[piv], M[i]
        pv = M[i][i] or 1e-9
        for j in range(i, d1 + 1):
            M[i][j] /= pv
        for r in range(d1):
            if r != i and M[r][i]:
                f = M[r][i]
                for j in range(i, d1 + 1):
                    M[r][j] -= f * M[i][j]
    return {"mu": mu, "sd": sd, "w": [M[i][d1] for i in range(d1)]}


def outcome(m1, m1t, x, T1, S):
    """single lot: win = T1 touched before the stop; stop to entry after T1; followed to the end of the call window."""
    c_end = x["done"]
    a, b = bisect.bisect_left(m1t, c_end), bisect.bisect_left(m1t, x["end"])
    if b - a < max(3, 0.9 * ((x["end"] - c_end) // 60)):
        return None
    side = x["side"]
    entry = x["entry"]
    stop, mx, win = -S, 0.0, False
    last = 0.0
    for bar in m1[a:b]:
        hi, lo, cl = ((bar["high"] - entry, bar["low"] - entry, bar["close"] - entry) if side > 0
                      else (entry - bar["low"], entry - bar["high"], entry - bar["close"]))
        last = cl
        if lo <= stop:
            return stop, win
        if hi >= T1:
            win = True
        mx = max(mx, hi)
        if mx >= T1:
            stop = max(stop, 0.0)
    return last, win


def day_of(t):
    return (t + 19800) // DAY


def clock(t):
    return datetime.datetime.fromtimestamp(t + 19800, datetime.timezone.utc).strftime("%Y-%m-%d")


def main():
    server._sync_token_from_env()
    now = time.time()
    models, report, pooled = {}, [], []
    P = lambda s="": (print(s), report.append(s))
    P(f"STRICT MODEL TRAINING - real Upstox candles - {datetime.datetime.fromtimestamp(now + 19800, datetime.timezone.utc):%Y-%m-%d %H:%M} IST")
    for seg in strict.SEGMENTS:
        inst = next(x for x in server.INSTRUMENTS if x["id"] == seg)
        c5 = server._candles_days(inst, 300)
        m1 = server._hist_1m(inst)
        m1t = [c["t"] for c in m1]
        series = {1: m1, 5: c5, 15: st.resample(c5, 15), 30: st.resample(c5, 30)}
        T1, S = LEVELS[seg]
        cands = []
        for tf in strict.TFS:
            cs = series[tf]
            h = strict.HTF[tf]
            for x in strict.candidates(cs, tf, series[h]):
                x["entry"] = cs[x["i"]]["close"]
                r = outcome(m1, m1t, x, T1, S)
                if r is None:
                    continue
                x["pnl"], x["win"] = r
                cands.append(x)
        last = max(x["t"] for x in cands)
        P("")
        P(f"== {seg}: {len(cands)} signals with a full real 1-minute path, {clock(min(x['t'] for x in cands))} -> {clock(last)}; T1 = {T1} points, stop = {S} points (model target: {LABEL[seg]})")
        folds = [(last - (90 - 30 * k) * DAY, last - (60 - 30 * k) * DAY) for k in range(3)]
        folds[-1] = (folds[-1][0], last + 1)
        calls, grades = [], []
        for lo, hi in folds:
            train = [x for x in cands if x["end"] < lo]
            test = [dict(x) for x in cands if lo <= x["t"] < hi and strict.allowed(seg, x)]
            if len(train) < 500:
                continue
            mdl = fit_ridge([x["feat"] for x in train], [target(x, seg) for x in train])
            sc = sorted(strict.score(mdl, x["feat"]) for x in train)
            mdl["thr"] = sc[int((1 - TOP_SHARE[seg]) * len(sc))]
            if TOP_SHARE_B[seg]:
                mdl["thrB"] = sc[int((1 - TOP_SHARE_B[seg]) * len(sc))]
            for x in test:
                x["score"] = strict.score(mdl, x["feat"])
            trsel = [dict(x, score=strict.score(mdl, x["feat"])) for x in train if strict.allowed(seg, x)]
            mdl["filter"] = strict.filter_cut(seg, strict.select(trsel, mdl))
            picks = [p for p in strict.select(test, mdl) if strict.passes(mdl, p)]
            calls += [(p["t"], p["win"], p["pnl"]) for p in picks]
            pooled += [(seg, dict(p, margin=p["score"] - mdl["thr"])) for p in picks]
            gA = [p for p in picks if p["grade"] == "A"]
            gB = [p for p in picks if p["grade"] == "B"]
            grades.append((len(gA), sum(p["win"] for p in gA), sum(p["pnl"] for p in gA), len(gB), sum(p["win"] for p in gB), sum(p["pnl"] for p in gB)))
            n = len(picks)
            P(f"   test block {clock(lo)} -> {clock(min(hi, last))}: {n} calls, win {sum(p['win'] for p in picks) / max(n, 1) * 100:4.1f}%, "
              f"net {sum(p['pnl'] for p in picks) / max(n, 1):+6.1f} pts per call")
        n = len(calls)
        sessions = len({day_of(t) for t, _, _ in calls}) or 1
        total_sessions = len({day_of(x["t"]) for x in cands if x["t"] >= folds[0][0]})
        oos = {"calls": n, "perSession": round(n / max(total_sessions, 1), 2), "winRate": round(sum(c[1] for c in calls) / max(n, 1) * 100, 1),
               "netPerCall": round(sum(c[2] for c in calls) / max(n, 1), 1), "blocks": len(folds)}
        P(f"   OUT-OF-SAMPLE (3 blocks x 30 days, trained only on earlier data): {n} calls = {oos['perSession']} per session, "
          f"win {oos['winRate']}%, NET {oos['netPerCall']:+} points per call")
        nA, wA, pA, nB, wB, pB = (sum(g[i] for g in grades) for i in range(6))
        oos["gradeA"] = {"calls": nA, "winRate": round(wA / max(nA, 1) * 100, 1), "netPerCall": round(pA / max(nA, 1), 1)}
        oos["gradeB"] = {"calls": nB, "winRate": round(wB / max(nB, 1) * 100, 1), "netPerCall": round(pB / max(nB, 1), 1)}
        P(f"      A-grade: {nA} calls, win {oos['gradeA']['winRate']}%, net {oos['gradeA']['netPerCall']:+}   |   B-grade: {nB} calls, win {oos['gradeB']['winRate']}%, net {oos['gradeB']['netPerCall']:+}")
        train = [x for x in cands]
        mdl = fit_ridge([x["feat"] for x in train], [target(x, seg) for x in train])
        sc = sorted(strict.score(mdl, x["feat"]) for x in train)
        mdl["thr"] = sc[int((1 - TOP_SHARE[seg]) * len(sc))]
        if TOP_SHARE_B[seg]:
            mdl["thrB"] = sc[int((1 - TOP_SHARE_B[seg]) * len(sc))]
        trsel = [dict(x, score=strict.score(mdl, x["feat"])) for x in train if strict.allowed(seg, x)]
        mdl["filter"] = strict.filter_cut(seg, strict.select(trsel, mdl))
        top = sorted(zip(strict.FEATURE_NAMES, mdl["w"][:-1]), key=lambda kv: -abs(kv[1]))[:5]
        P("   strongest features: " + ", ".join(f"{k} {v:+.3f}" for k, v in top))
        mdl.update({"rules": {"tfs": list(strict.RULES[seg]["tfs"]), "window": list(strict.RULES[seg]["window"])}, "T1": T1, "stop": S, "X": T1, "topShare": TOP_SHARE[seg], "features": strict.FEATURE_NAMES, "trainedThrough": clock(last), "trainedSignals": len(train),
                    "outOfSample": oos})
        models[seg] = mdl
    if pooled:
        P("")
        P("== ALL THREE INDEXES TOGETHER (out-of-sample)")
        for label, sel in (("each index separately", pooled), (f"same side within {strict.CROSS_DEDUPE_MIN} min on several indexes = ONE call", strict.cross_dedupe(pooled))):
            n = len(sel)
            P(f"   {label}: {n} calls, win {sum(p['win'] for _, p in sel) / n * 100:.1f}%, net {sum(p['pnl'] for _, p in sel) / n:+.1f} pts per call, total {sum(p['pnl'] for _, p in sel):+.0f} pts")
    with open(strict.MODEL_PATH, "w", encoding="utf-8") as fh:
        json.dump(models, fh)
    with open(os.path.join(ROOT, "strict_training_report.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")
    P("")
    P(f"Wrote {strict.MODEL_PATH}")


if __name__ == "__main__":
    main()
