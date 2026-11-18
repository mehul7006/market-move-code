"""Scorecard: every strategy tested over 60 / 120 / 180 days, per segment, long term and short term.

Real Upstox candles only. Every call is counted once (SL / T1 / T2 / T3 / TIMEOUT) at the highest target reached
before the stop, and scored on the real 1-minute path wherever 1-minute history exists.

Honesty: the short-term strategies were chosen using history up to ~60 days ago and checked on the last 60 days,
so the 120- and 180-day windows overlap the data they were tuned on. They show CONSISTENCY, not a fresh out-of-sample
proof. `walkforward_result.json` (python walkforward.py) is the genuinely out-of-sample test of the selection method.
"""
from __future__ import annotations

import bisect
import collections
import datetime
import json
import os
import time

import analysis
import shortterm

ROOT = os.path.dirname(os.path.abspath(__file__))
WINDOWS = (60, 120, 180)
TARGET_WIN = 70.0


def _month(t):
    return datetime.datetime.fromtimestamp(t + 19800, datetime.timezone.utc).strftime("%Y-%m")


def _rate(n, d):
    return round(n / d * 100, 1) if d else None


# ------------------------------------------------------------------ scoring of long-term calls on the 1m path
def score_path(path, entry, sign, sl, t1, t2, t3):
    """Bucket on a candle path: highest target touched before the stop; a candle touching both = stop."""
    stopped, maxhit = False, 0
    for c in path:
        if sign > 0:
            hit_sl, tops = c["low"] <= sl, (c["high"] >= t1, c["high"] >= t2, c["high"] >= t3)
        else:
            hit_sl, tops = c["high"] >= sl, (c["low"] <= t1, c["low"] <= t2, c["low"] <= t3)
        if hit_sl:
            stopped = True
            break
        for j, on in enumerate(tops, 1):
            if on and j > maxhit:
                maxhit = j
    if maxhit:
        return "T%d" % maxhit
    return "SL" if stopped else "TIMEOUT"


def _bucket5(ex):
    if ex["outcome"] == "stop":
        return "SL"
    if ex["outcome"] == "t1":
        return "T%d" % max(1, ex.get("targetsHit") or 1)
    return "TIMEOUT"


def _long_stats(buckets):
    n = len(buckets)
    c = collections.Counter(buckets)
    anyt = c["T1"] + c["T2"] + c["T3"]
    return {"calls": n, "SL": c["SL"], "T1": c["T1"], "T2": c["T2"], "T3": c["T3"], "timeouts": c["TIMEOUT"],
            "winRate": _rate(anyt, n), "slRate": _rate(c["SL"], n), "t1Rate": _rate(c["T1"], n),
            "t2Rate": _rate(c["T2"], n), "t3Rate": _rate(c["T3"], n)}


def _verdict(win_by_window, calls_by_window, pf_by_window=None):
    """PASS = >= 70% in every window; WEAK = last 60 days pass but a longer window does not; FAIL otherwise."""
    wins = [win_by_window.get(w) for w in WINDOWS]
    if any(x is None for x in wins):
        return "NO DATA"
    if all(x >= TARGET_WIN for x in wins):
        return "PASS"
    if wins[0] >= TARGET_WIN:
        return "WEAK"
    return "FAIL"


def long_rows(server, c5_by_seg, m1_by_seg, now):
    rows = []
    for iid in server.TRADEABLE_IDS:
        inst = next(x for x in server.INSTRUMENTS if x["id"] == iid)
        key = server.PROFILE_BY_ID[iid]
        prof = server.FORECAST_PROFILES[key]
        cs = [c for c in c5_by_seg.get(iid, []) if c["t"] + 300 <= now]
        row = {"segment": iid, "name": inst["name"], "profile": prof["name"], "minMove": inst["minMove"],
               "stopRule": "%.1fx min move" % prof["stopMoveMult"], "error": None}
        if len(cs) < 400:
            row["error"] = "not enough real 5m candles"
            rows.append(row)
            continue
        last = cs[-1]["t"]
        res = analysis.validate_forecast(cs, inst["minMove"], prof, since_ts=last - max(WINDOWS) * 86400,
                                         examples_limit=None) or {}
        exs = res.get("examples") or []
        m1 = [c for c in (m1_by_seg.get(iid) or []) if c["t"] + 60 <= now]
        m1t = [c["t"] for c in m1]
        first1 = m1t[0] if m1t else None
        horizon = prof["horizon"]
        b5, b1 = [], []
        for ex in exs:
            sign = 1 if ex["direction"] == "BULLISH" else -1
            b5.append(_bucket5(ex))
            sc = None
            if first1 and ex["barTime"] >= first1 + 3600:
                te = ex["barTime"] + 300
                day_start = ex["barTime"] - ((ex["barTime"] + 19800) % 86400)
                wend = min(ex["barTime"] + (horizon + 1) * 300, day_start + 930 * 60)
                a, b = bisect.bisect_left(m1t, te), bisect.bisect_left(m1t, wend)
                if b - a >= 0.9 * ((wend - te) // 60):
                    sc = score_path(m1[a:b], ex["entry"], sign, ex["stop"], ex["target1"], ex["target2"], ex["target3"])
            b1.append(sc or _bucket5(ex))                # 1-minute path where available, else the 5m result
        row["windows"], row["windows5m"] = {}, {}
        wins, calls = {}, {}
        for w in WINDOWS:
            cut = last - w * 86400
            sel = [(x, y, e) for x, y, e in zip(b1, b5, exs) if e["barTime"] >= cut]
            row["windows"][str(w)] = _long_stats([x for x, _, _ in sel])
            row["windows5m"][str(w)] = _long_stats([y for _, y, _ in sel])
            wins[w], calls[w] = row["windows"][str(w)]["winRate"], row["windows"][str(w)]["calls"]
        months = collections.defaultdict(list)
        for x, e in zip(b1, exs):
            months[_month(e["barTime"])].append(x)
        row["months"] = [{"month": m, "calls": len(v), "winRate": _rate(sum(1 for x in v if x != "SL" and x != "TIMEOUT"), len(v))}
                         for m, v in sorted(months.items())]
        row["verdict"] = _verdict(wins, calls)
        rows.append(row)
    return rows


# ------------------------------------------------------------------ short term
def short_rows(cmap, now):
    out = {}
    for tf in shortterm.TIMEFRAMES:
        rows = []
        for seg, label in shortterm.SEGMENTS.items():
            p = shortterm.STRATEGIES.get((seg, tf))
            row = {"segment": seg, "name": label, "tf": tf, "validated": bool(p), "error": None}
            rows.append(row)
            cs = shortterm._usable((cmap.get(tf) or {}).get(seg) or [], tf, now)
            if len(cs) < 200:
                row["error"] = "not enough real %sm candles" % tf
                continue
            if not p:
                row["note"] = shortterm.NO_STRATEGY_NOTE.get(tf, shortterm.NO_STRATEGY_NOTE["default"])
                continue
            fine = None
            if tf > 1:
                fine = [c for c in (cmap.get(1) or {}).get(seg) or [] if c["t"] + 60 <= now] or None
            calls = [c for c in shortterm.replay(cs, p, fine=fine, now=now) if c["result"] != "OPEN"]
            last = cs[-1]["t"]
            row.update(family=p["family"], ladder={"t1": p["t1"], "t2": p["t2"], "t3": p["t3"], "sl": p["sl"]},
                       horizonMin=p["horizonMin"], smallSample=bool(p.get("smallSample")),
                       dataFrom=datetime.datetime.fromtimestamp(cs[0]["t"] + 19800, datetime.timezone.utc).strftime("%Y-%m-%d"))
            row["windows"] = {}
            wins, calls_n = {}, {}
            for w in WINDOWS:
                sel = [c for c in calls if c["t"] >= last - w * 86400]
                row["windows"][str(w)] = shortterm.summarize(sel)
                wins[w], calls_n[w] = row["windows"][str(w)]["winRate"], row["windows"][str(w)]["calls"]
            months = collections.defaultdict(list)
            for c in calls:
                months[_month(c["t"])].append(c)
            row["months"] = [{"month": m, "calls": len(v),
                              "winRate": _rate(sum(1 for c in v if c["result"] in ("T1", "T2", "T3")), len(v)),
                              "planPts": round(sum(c["planPts"] for c in v), 1)} for m, v in sorted(months.items())]
            row["verdict"] = _verdict(wins, calls_n)
            row["scoredOn"] = dict(collections.Counter(c["scoredOn"] for c in calls))
        out[str(tf)] = rows
    return out


def sensitivity_short(cmap, now):
    """Same strategies with every trade forced to end by 15:15 (the exchange's index feed is flat 15:15-15:29)."""
    saved = shortterm.SESSION_CLOSE_MIN
    res = []
    try:
        for (seg, tf), p in shortterm.STRATEGIES.items():
            cs = shortterm._usable((cmap.get(tf) or {}).get(seg) or [], tf, now)
            if len(cs) < 200:
                continue
            fine = None
            if tf > 1:
                fine = [c for c in (cmap.get(1) or {}).get(seg) or [] if c["t"] + 60 <= now] or None
            last = cs[-1]["t"]
            row = {"segment": seg, "tf": tf}
            for label, close in (("full", 15 * 60 + 30), ("cap1515", 15 * 60 + 15)):
                shortterm.SESSION_CLOSE_MIN = close
                calls = [c for c in shortterm.replay(cs, p, fine=fine, now=now) if c["result"] != "OPEN"]
                sel = [c for c in calls if c["t"] >= last - 180 * 86400]
                s = shortterm.summarize(sel)
                row[label] = {"calls": s["calls"], "winRate": s["winRate"], "profitFactor": s["profitFactor"]}
            res.append(row)
    finally:
        shortterm.SESSION_CLOSE_MIN = saved
    return res


def build(server, now=None):
    now = now or time.time()
    c5, m1 = {}, {}
    for inst in server.INSTRUMENTS:
        if inst["id"] not in server.TRADEABLE_IDS:
            continue
        iid = inst["id"]
        try:
            c5[iid] = server._candles_days(inst, 230)
        except Exception:
            c5[iid] = []
        try:
            m1[iid] = server._hist_1m(inst)
        except Exception:
            m1[iid] = []
    cmap = {1: {k: m1.get(k, []) for k in shortterm.SEGMENTS}, 5: {k: c5.get(k, []) for k in shortterm.SEGMENTS}}
    for tf in (15, 30):
        cmap[tf] = {k: shortterm.resample(c5.get(k, []), tf) for k in shortterm.SEGMENTS}
    rows_short = short_rows(cmap, now)
    payload = {
        "generatedAt": int(now), "windows": list(WINDOWS), "targetWin": TARGET_WIN,
        "long": long_rows(server, c5, m1, now),
        "short": rows_short,
        "noEdge": [{"id": k, "name": v} for k, v in shortterm.NO_EDGE.items()],
        "sensitivity": sensitivity_short(cmap, now),
        "dataSpan": {k: {"from": datetime.datetime.fromtimestamp(v[0]["t"] + 19800, datetime.timezone.utc).strftime("%Y-%m-%d"),
                         "to": datetime.datetime.fromtimestamp(v[-1]["t"] + 19800, datetime.timezone.utc).strftime("%Y-%m-%d"),
                         "bars": len(v)} for k, v in (("5m NIFTY", c5.get("NIFTY") or [{"t": 0}]),
                                                      ("1m NIFTY", m1.get("NIFTY") or [{"t": 0}]))},
    }
    ap = os.path.join(ROOT, "data_audit_report.txt")
    if os.path.exists(ap):
        payload["audit"] = open(ap, encoding="utf-8", errors="ignore").read()
    wf = os.path.join(ROOT, "walkforward_result.json")
    if os.path.exists(wf):
        try:
            payload["walkforward"] = json.load(open(wf, encoding="utf-8"))
        except Exception:
            pass
    return payload


def to_csv(payload):
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["term", "segment", "candle", "window_days", "calls", "win_pct", "sl_first_pct", "t1_only_pct",
                "t2_only_pct", "t3_pct", "timeouts", "profit_factor", "avg_plan_pts", "verdict"])
    for r in payload["long"]:
        for wd, s in (r.get("windows") or {}).items():
            w.writerow(["long", r["segment"], "5m signals (scored on 1m)", wd, s["calls"], s["winRate"], s["slRate"],
                        s["t1Rate"], s["t2Rate"], s["t3Rate"], s["timeouts"], "", "", r.get("verdict")])
    for tf, rows in payload["short"].items():
        for r in rows:
            for wd, s in (r.get("windows") or {}).items():
                w.writerow(["short", r["segment"], tf + "m", wd, s["calls"], s["winRate"], s["slRate"], s["t1Rate"],
                            s["t2Rate"], s["t3Rate"], s["timeouts"], s["profitFactor"], s["avgPlanPts"], r.get("verdict")])
    return out.getvalue()


if __name__ == "__main__":
    # python scorecard.py --snapshot   -> writes scorecard_snapshot.json (served on small hosts that cannot recompute it)
    import sys
    sys.path.insert(0, ROOT)
    import server
    server._sync_token_from_env()
    if "--snapshot" in sys.argv:
        data = build(server)
        data["snapshotAt"] = int(time.time())
        with open(os.path.join(ROOT, "scorecard_snapshot.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        print("wrote scorecard_snapshot.json")
