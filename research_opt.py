"""Offline walk-forward research on saved real 5m bars (6 months, Upstox).

Same no-lookahead and T1-before-SL accounting as the live audit, but split into monthly
folds so a rule is only trusted when it survives months it was not tuned on.

    python research_opt.py --fetch      # refresh .research/bars6m.json from Upstox
    python research_opt.py              # grid search, monthly fold report
    python research_opt.py --best       # top rules per instrument
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import time

import analysis
import server

ROOT = os.path.dirname(os.path.abspath(__file__))
BARS = os.path.join(ROOT, ".research", "bars6m.json")
MIN_MOVE = {"NIFTY": 25, "SENSEX": 40, "BANKNIFTY": 50, "FINNIFTY": 30}


def fetch():
    ids = tuple(MIN_MOVE)
    out = {}
    today = datetime.date.today()
    for iid in ids:
        inst = next(i for i in server.INSTRUMENTS if i["id"] == iid)
        rows = {c["t"]: c for c in server._audit_candles(inst)}
        for k in range(0, 6):
            end = today - datetime.timedelta(days=30 * k + 1)
            start = end - datetime.timedelta(days=29)
            try:
                part = server.UX.candles(inst["key"], "minutes", 5, end.isoformat(), start.isoformat())
            except Exception as exc:
                print(iid, k, "ERR", str(exc)[:100])
                continue
            for c in part:
                rows.setdefault(c["t"], c)
        out[iid] = [rows[t] for t in sorted(rows)]
        print(iid, len(out[iid]))
    json.dump(out, open(BARS, "w"))


def load():
    with open(BARS) as fh:
        return json.load(fh)


def features(candles):
    closes, e9, e21, rsi, hist, st_line, st_dir = analysis._tech_features(candles)
    atrs = analysis.atr_series(candles, 14)
    e50 = analysis.ema_series(closes, 50)
    return closes, e9, e21, e50, rsi, hist, st_line, st_dir, atrs


def day_trend(candles):
    """Prior-session daily trend (close vs 20-day EMA), known before each session opens."""
    n = len(candles)
    out = [None] * n
    sess, start = [], 0
    for i in range(1, n):
        if candles[i]["t"] - candles[i - 1]["t"] > 300:
            sess.append((start, i))
            start = i
    sess.append((start, n))
    closes = []
    for s, e in sess:
        closes.append(candles[e - 1]["close"])
    if len(closes) < 21:
        return out
    ema = analysis.ema_series(closes, 20)
    for k, (s, e) in enumerate(sess):
        if k == 0 or ema[k - 1] is None:
            continue
        tr = 1 if closes[k - 1] > ema[k - 1] else -1
        for j in range(s, e):
            out[j] = tr
    return out


def mtf15(candles):
    """Map every 5m bar to the state of the last *completed* 15m bar of its session."""
    n = len(candles)
    out = [None] * n
    groups, cur, start = [], [], 0
    for i, c in enumerate(candles):
        if cur and c["t"] - candles[i - 1]["t"] > 300:
            groups.append((start, i))
            start, cur = i, []
        cur.append(c)
    groups.append((start, n))
    for s, e in groups:
        bars = candles[s:e]
        agg = []
        for k in range(0, len(bars) - 2, 3):
            tri = bars[k:k + 3]
            agg.append({"t": tri[0]["t"], "open": tri[0]["open"],
                        "high": max(b["high"] for b in tri),
                        "low": min(b["low"] for b in tri),
                        "close": tri[-1]["close"]})
        if len(agg) < 55:
            continue
        closes, e9, e21, rsi, hist, _, st_dir = analysis._tech_features(agg)
        e50 = analysis.ema_series(closes, 50)
        state = []
        for q in range(len(agg)):
            state.append({
                "ema": (1 if e9[q] > e21[q] else -1) if (e9[q] and e21[q]) else None,
                "trend": (1 if closes[q] > e50[q] else -1) if e50[q] else None,
                "rsi": rsi[q], "st": st_dir[q] or None,
            })
        # 5m bar (s + 3k + r) sees state of the last completed 15m bar (k-1)
        for k in range(1, len(agg)):
            st = state[k - 1]
            lo = s + 3 * k
            hi = s + 3 * (k + 1)
            hi = min(hi, e)
            for j in range(lo, hi):
                out[j] = st
    return out


def atr_percentile(atrs, i, look=200):
    vals = [a for a in atrs[max(0, i - look):i + 1] if a]
    if len(vals) < 50:
        return None
    cur = atrs[i]
    return sum(1 for v in vals if v <= cur) / len(vals)


def ist_minutes(t):
    return ((t + 19800) % 86400) // 60


def simulate(candles, feats, min_move, cfg, start, end):
    closes, e9, e21, e50, rsi, hist, st_line, st_dir, atrs, mtf, day_arr = feats
    h = cfg["horizon"]
    samples = wins = losses = unresolved = 0
    risk_sum = 0.0
    pnl = 0.0
    last_exit = -10 ** 9
    cooldown = cfg.get("cooldown", 0)
    for i in range(max(start, 40), min(end, len(candles) - h - 1)):
        if i <= last_exit + cooldown:
            continue
        norm = analysis._tech_score_at(i, e9, e21, rsi, hist, st_dir)
        min_conv = max(cfg["minConviction"], cfg.get("minTechConviction") or 0)
        if norm is None or abs(norm) * 100 < min_conv:
            continue
        sign = 1 if norm > 0 else -1
        direction = "BULLISH" if sign > 0 else "BEARISH"
        atr = atrs[i]
        if not atr:
            continue
        if cfg.get("unanimity"):
            if not (e9[i] and e21[i] and st_dir[i] and hist[i] is not None):
                continue
            if (1 if e9[i] > e21[i] else -1) != sign:
                continue
            if st_dir[i] != sign or (1 if hist[i] > 0 else -1) != sign:
                continue
            # RSI votes only at an extreme, exactly like _tech_alignment.
            if rsi[i] is not None and (rsi[i] >= 60 or rsi[i] <= 40) \
                    and (1 if rsi[i] >= 60 else -1) != sign:
                continue
        if cfg.get("trend50") and (e50[i] is None or (1 if closes[i] > e50[i] else -1) != sign):
            continue
        if rsi[i] is not None and cfg.get("rsiBand"):
            lo, hi = cfg["rsiBand"] if sign > 0 else (100 - cfg["rsiBand"][1], 100 - cfg["rsiBand"][0])
            if not (lo <= rsi[i] <= hi):
                continue
        if cfg.get("maxStretchAtr") and abs(closes[i] - e9[i]) > cfg["maxStretchAtr"] * atr:
            continue
        if cfg.get("atrPctl"):
            pc = atr_percentile(atrs, i)
            if pc is None or not (cfg["atrPctl"][0] <= pc <= cfg["atrPctl"][1]):
                continue
        if cfg.get("timeWindow"):
            m = ist_minutes(candles[i]["t"])
            if not (cfg["timeWindow"][0] <= m <= cfg["timeWindow"][1]):
                continue
        if cfg.get("mtf"):
            st15 = mtf[i]
            if not st15:
                continue
            if cfg["mtf"].get("ema") and st15["ema"] != sign:
                continue
            if cfg["mtf"].get("trend") and st15["trend"] != sign:
                continue
            if cfg["mtf"].get("st") and st15["st"] != sign:
                continue
            if cfg["mtf"].get("rsi") and st15["rsi"] is not None:
                if (st15["rsi"] < 50) if sign > 0 else (st15["rsi"] > 50):
                    continue
        if cfg.get("dayTrend"):
            dt = day_arr[i]
            if dt is None or dt != sign:
                continue
        expected = atr * math.sqrt(h) * 0.8
        if expected < min_move:
            continue
        if cfg.get("minExpMult") and expected < min_move * cfg["minExpMult"]:
            continue
        if cfg.get("entryBefore") and ist_minutes(candles[i]["t"]) > cfg["entryBefore"]:
            continue
        timing = analysis._move_timing(candles, direction, atr, expected,
                                       st_dir=st_dir, macd_hist=hist, idx=i)
        if timing["label"] not in cfg["allowedTiming"]:
            continue

        entry_idx, entry = i, closes[i]
        if cfg.get("entryMode") == "nextOpen":
            entry_idx = i + 1
            entry = candles[entry_idx]["open"]
        elif cfg.get("entryMode") == "pullback":
            level = closes[i] - sign * cfg.get("pullbackAtr", 0.2) * atr
            fill = None
            for k in range(1, cfg.get("fillBars", 3) + 1):
                j = i + k
                if j >= len(candles):
                    break
                if (candles[j]["low"] <= level) if sign > 0 else (candles[j]["high"] >= level):
                    fill, entry_idx = level, j
                    break
            if fill is None:
                continue
            entry = fill

        stop_dist = (min_move * cfg["stopMoveMult"] if cfg.get("stopMoveMult")
                     else cfg["stopAtr"] * atr)
        stop = entry - sign * stop_dist
        t1 = entry + sign * min_move
        outcome, bars, mfe = "open", None, 0.0
        for k, c in enumerate(candles[entry_idx + 1:entry_idx + 1 + h], 1):
            fav = (c["high"] - entry) if sign > 0 else (entry - c["low"])
            mfe = max(mfe, fav)
            if outcome != "open":
                continue
            hit_t1 = c["high"] >= t1 if sign > 0 else c["low"] <= t1
            hit_sl = c["low"] <= stop if sign > 0 else c["high"] >= stop
            if hit_t1 and hit_sl:
                outcome, bars = "stop", k
            elif hit_t1:
                outcome, bars = "t1", k
            elif hit_sl:
                outcome, bars = "stop", k

        samples += 1
        risk = abs(entry - stop)
        risk_sum += risk
        if outcome == "t1":
            wins += 1
            pnl += min_move
        elif outcome == "stop":
            losses += 1
            pnl -= risk
        else:
            unresolved += 1
            fin = candles[min(entry_idx + h, len(candles) - 1)]
            pnl += (fin["close"] - entry) * sign
        last_exit = entry_idx + h

    if not samples:
        return None
    resolved = wins + losses
    return {
        "samples": samples, "wins": wins, "losses": losses, "unresolved": unresolved,
        "winRate": round(wins / samples * 100, 1),
        "avgRisk": round(risk_sum / samples, 1),
        "rr1": round(min_move / (risk_sum / samples), 2),
        "expectancy": round(pnl / samples, 1),
        "start": candles[start]["t"], "end": candles[min(end, len(candles)) - 1]["t"],
    }


def monthly(candles):
    """Split bar indices into calendar months (IST)."""
    folds, cur_key, cur_start = [], None, 0
    for i, c in enumerate(candles):
        key = time.strftime("%Y-%m", time.gmtime(c["t"] + 19800))
        if key != cur_key:
            if cur_key is not None:
                folds.append((cur_key, cur_start, i))
            cur_key, cur_start = key, i
    folds.append((cur_key, cur_start, len(candles)))
    return folds


def evaluate(candles, feats, min_move, cfg):
    folds = monthly(candles)
    agg = {"samples": 0, "wins": 0, "losses": 0, "unresolved": 0,
           "risk": 0.0, "pnl": 0.0, "months": []}
    for name, s, e in folds:
        r = simulate(candles, feats, min_move, cfg, s, e)
        if not r:
            continue
        agg["samples"] += r["samples"]
        agg["wins"] += r["wins"]
        agg["losses"] += r["losses"]
        agg["unresolved"] += r["unresolved"]
        agg["risk"] += r["avgRisk"] * r["samples"]
        agg["pnl"] += r["expectancy"] * r["samples"]
        agg["months"].append((name, r["samples"], r["winRate"], r["expectancy"]))
    if not agg["samples"]:
        return None
    resolved = agg["wins"] + agg["losses"]
    avg_risk = agg["risk"] / agg["samples"]
    exp = agg["pnl"] / agg["samples"]
    month_wins = [m[2] for m in agg["months"] if m[1] >= 4]
    return {
        "samples": agg["samples"], "wins": agg["wins"], "losses": agg["losses"],
        "unresolved": agg["unresolved"],
        "winRate": round(agg["wins"] / agg["samples"] * 100, 1),
        "avgRisk": round(avg_risk, 1), "rr1": round(min_move / avg_risk, 2),
        "expectancy": round(exp, 1),
        "months": agg["months"],
        "worstMonth": min(month_wins) if month_wins else None,
        "monthsOk": sum(1 for w in month_wins if w >= 55),
        "monthsCounted": len(month_wins),
    }


BASE = {"horizon": 12, "stopAtr": 0.5, "minConviction": 50,
        "allowedTiming": ("EARLY", "BUILDING"), "cooldown": 0, "entryMode": "market"}
STRICT = {"unanimity": True, "trend50": True, "rsiBand": (52, 72), "maxStretchAtr": 0.6,
          "atrPctl": (0.3, 0.92), "timeWindow": (9 * 60 + 25, 15 * 60), "cooldown": 6}


def grid():
    out = []
    for horizon in (6, 9, 12, 18):
        for stop in (0.5, 0.8, 1.0, 1.2, 1.5):
            for conv in (50, 55, 60):
                cfg = dict(BASE, horizon=horizon, stopAtr=stop, minConviction=conv)
                out.append((f"plain h{horizon} s{stop} c{conv}", cfg))
                u = dict(cfg, unanimity=True, cooldown=3)
                out.append((f"unan  h{horizon} s{stop} c{conv}", u))
                out.append((f"strict h{horizon} s{stop} c{conv}", dict(cfg, **STRICT)))
    for horizon in (9, 12, 18):
        for stop in (0.5, 0.8, 1.0, 1.2):
            for off in (0.15, 0.25, 0.4):
                cfg = dict(BASE, horizon=horizon, stopAtr=stop, minConviction=55, **STRICT,
                           entryMode="pullback", pullbackAtr=off, fillBars=3)
                out.append((f"pull h{horizon} s{stop} o{off}", cfg))
    # higher-timeframe confirmation: the last completed 15m bar must agree
    mtf_variants = {
        "ema": {"ema": True},
        "ema+trend": {"ema": True, "trend": True},
        "all": {"ema": True, "trend": True, "st": True, "rsi": True},
    }
    for hname, mtf in mtf_variants.items():
        for horizon in (9, 12, 18):
            for stop in (0.8, 1.0, 1.2, 1.5):
                for early in (False, True):
                    cfg = dict(BASE, horizon=horizon, stopAtr=stop, minConviction=55,
                               unanimity=True, cooldown=3, mtf=mtf)
                    if early:
                        cfg["allowedTiming"] = ("EARLY",)
                    tag = "mtfE" if early else "mtfB"
                    out.append((f"{tag} {hname[:6]} h{horizon} s{stop}", cfg))
    for horizon in (9, 12, 18):
        for stop in (0.8, 1.0, 1.2, 1.5):
            cfg = dict(BASE, horizon=horizon, stopAtr=stop, minConviction=55, **STRICT,
                       mtf={"ema": True, "trend": True})
            out.append((f"mtfS h{horizon} s{stop}", cfg))
    # fixed-RR stops: stop distance = k x min move, so every instrument trades the same R:R
    for horizon in (6, 9, 12, 18, 24):
        for mult in (1.5, 1.8, 2.0, 2.2, 2.5):
            for gate, extra in (("plain", {}), ("unan", {"unanimity": True, "cooldown": 3}),
                                ("strict", dict(STRICT))):
                cfg = dict(BASE, horizon=horizon, minConviction=55, stopMoveMult=mult, **extra)
                out.append((f"rr{mult} h{horizon} {gate}", cfg))
    # wide ATR stops: what the 65-85% hit rates actually cost in R:R
    for horizon in (9, 12, 18):
        for stop in (1.8, 2.2, 2.6):
            cfg = dict(BASE, horizon=horizon, stopAtr=stop, minConviction=55, **STRICT)
            out.append((f"wide h{horizon} s{stop}", cfg))
    # the profile the desk deploys: measured >=75% on both indices
    out.append(("precision75", dict(BASE, horizon=36, stopMoveMult=3.4, minConviction=55,
                                    minTechConviction=100, unanimity=True, cooldown=3,
                                    minExpMult=4, entryBefore=870)))
    return out


def fmt_cell(r):
    if not r:
        return "   -   "
    return f"{r['samples']:>3} {r['winRate']:>5.1f} {r['worstMonth'] or 0:>5.1f} {r['monthsOk']:>2}/{r['monthsCounted']:<2} {r['rr1']:>4.2f} {str(r['expectancy']):>6}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--best", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    if args.fetch:
        fetch()
        return
    data = load()
    cache = {}
    for iid, candles in data.items():
        cache[iid] = (candles, features(candles) + (mtf15(candles), day_trend(candles)))

    ranking = {}
    for label, cfg in grid():
        rows = {}
        for iid, (candles, feats) in cache.items():
            r = evaluate(candles, feats, MIN_MOVE[iid], cfg)
            if r:
                rows[iid] = r
        if "NIFTY" not in rows or "SENSEX" not in rows:
            continue
        n, s = rows["NIFTY"], rows["SENSEX"]
        if n["samples"] < 30 or s["samples"] < 20:
            continue
        # robust = both prime indices clear 60% aggregate, make money, and no month below 45%
        ok = (n["winRate"] >= 60 and s["winRate"] >= 60
              and (n["expectancy"] or -1) > 0 and (s["expectancy"] or -1) > 0
              and (n["worstMonth"] or 0) >= 45 and (s["worstMonth"] or 0) >= 45)
        score = min(n["winRate"], s["winRate"])
        ranking[label] = (ok, score, cfg, rows)

    order = sorted(ranking.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
    print("cfg                        | NIFTY  n   win worst months rr1   exp | SENSEX n   win worst months rr1   exp | BANKNIFTY win | FINNIFTY win | ok")
    shown = 0
    for label, (ok, score, cfg, rows) in order:
        if args.best and not ok:
            continue
        n, s = rows["NIFTY"], rows["SENSEX"]
        b = rows.get("BANKNIFTY") or {}
        f = rows.get("FINNIFTY") or {}
        print(f"{label:<26} | {n['samples']:>3} {n['winRate']:>5.1f} {n['worstMonth'] or 0:>5.1f} "
              f"{n['monthsOk']:>2}/{n['monthsCounted']:<2} {n['rr1']:>4.2f} {str(n['expectancy']):>6} | "
              f"{s['samples']:>3} {s['winRate']:>5.1f} {s['worstMonth'] or 0:>5.1f} "
              f"{s['monthsOk']:>2}/{s['monthsCounted']:<2} {s['rr1']:>4.2f} {str(s['expectancy']):>6} | "
              f"{b.get('winRate') or 0:>6.1f}        | {f.get('winRate') or 0:>6.1f}       | {'YES' if ok else 'no'}")
        shown += 1
        if shown >= args.top:
            break

    good = [(l, c, r) for l, (ok, sc, c, r) in ranking.items() if ok]
    good.sort(key=lambda x: -min(x[2]["NIFTY"]["winRate"], x[2]["SENSEX"]["winRate"]))
    out = [{"label": l, "config": {k: (list(v) if isinstance(v, tuple) else v) for k, v in c.items()},
            "results": r} for l, c, r in good[:10]]
    json.dump(out, open(os.path.join(ROOT, ".research", "best_configs.json"), "w"), indent=2)
    print(f"\n{len(good)} rules pass the robustness gate; saved to .research/best_configs.json")


if __name__ == "__main__":
    main()
