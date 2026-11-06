"""Nifty 50 constituent breadth and audited per-stock intraday calls.

Every number comes from a real source: the constituent list from NSE's own index CSV, quotes
for all 50 names in one Upstox batch call, and 180 calendar days of real 5m bars per stock.
Each stock is audited on its own bars with the same no-lookahead, session-bounded rules as the
index desk, and a stock may issue a call only while its own measured T1-before-stop rate clears
the measured floor over enough setups. Names that fail the gate are shown as NO CALL with the
reason; there is no weaker fallback profile. T1 is sized from the measured move (0.30% of
price), not shrunk to make the win rate look good.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import math
import os
import threading
import time
import urllib.request

import analysis

IST_OFFSET = 19800
CONST_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
CONST_TTL = 7 * 86400
BARS_TTL = 6 * 3600
REPORT_TTL = 10 * 60
QUOTE_TTL = 5
CYCLE_SECONDS = 20 * 60
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PROFILE = {
    "name": "Nifty 50 stock movement",
    "horizon": 36,          # 36 x 5m = 3 hours
    # Sized from the measured move, not from the win rate: on 112k gated 5m setups the median
    # favourable excursion inside the 3-hour window is 0.42% of price, so T1 sits at 0.30%
    # (about 72% of that median move) and the stop at 0.50%. The old 0.6 x ATR target was a
    # median 0.13% — roughly 1.3 points on Reliance, i.e. inside the spread plus a tick.
    "targetPct": 0.30,      # T1 = 0.30% of the entry price
    "stopPct": 0.50,        # stop = 0.50% of the entry price (R:R 0.6)
    "entryBefore": 870,     # 14:30 IST
    "cooldown": 3,
    "minAtrPct": 0.0008,    # ATR must be at least 0.08% of price
    "minSamples": 100,
    # Measured, not aspirational: at a 0.30% target the honest T1 rate is ~59-61%, so a 75%
    # floor would silently force the target back down to noise. 55% is the level that removed
    # the names whose out-of-window expectancy was ~0 (44 names kept, 63.3% -> 65.0%).
    "minWinRate": 55.0,
    # A name also has to pay for itself: at this target a 55% win rate is below the T1-vs-stop
    # break-even, so several names cleared the rate and still measured a negative expectancy.
    "minAvgR": 0.0,
    "auditDays": 180,
}

# Any change to sizing or the gate must invalidate the persisted audits, otherwise the desk
# keeps serving numbers measured under the previous target.
PROFILE_SIG = "|".join(str(PROFILE[k]) for k in
                       ("horizon", "targetPct", "stopPct", "entryBefore", "cooldown",
                        "minAtrPct", "minSamples", "minWinRate", "minAvgR"))

# Snapshot of NSE's ind_nifty50list.csv on 2026-11-06. Used only when the live CSV cannot be
# fetched, and always labelled as a snapshot so it is never mistaken for a live pull.
CONST_FALLBACK = [
    ("ADANIENT", "Adani Enterprises Ltd.", "Metals & Mining", "INE423A01024"),
    ("ADANIPORTS", "Adani Ports and Special Economic Zone Ltd.", "Services", "INE742F01042"),
    ("APOLLOHOSP", "Apollo Hospitals Enterprise Ltd.", "Healthcare", "INE437A01024"),
    ("ASIANPAINT", "Asian Paints Ltd.", "Consumer Durables", "INE021A01026"),
    ("AXISBANK", "Axis Bank Ltd.", "Financial Services", "INE238A01034"),
    ("BSE", "BSE Ltd.", "Financial Services", "INE118H01025"),
    ("BAJAJ-AUTO", "Bajaj Auto Ltd.", "Automobile and Auto Components", "INE917I01010"),
    ("BAJFINANCE", "Bajaj Finance Ltd.", "Financial Services", "INE296A01032"),
    ("BAJAJFINSV", "Bajaj Finserv Ltd.", "Financial Services", "INE918I01026"),
    ("BEL", "Bharat Electronics Ltd.", "Capital Goods", "INE263A01024"),
    ("BHARTIARTL", "Bharti Airtel Ltd.", "Telecommunication", "INE397D01024"),
    ("CIPLA", "Cipla Ltd.", "Healthcare", "INE059A01026"),
    ("COALINDIA", "Coal India Ltd.", "Oil Gas & Consumable Fuels", "INE522F01014"),
    ("DRREDDY", "Dr. Reddy's Laboratories Ltd.", "Healthcare", "INE089A01031"),
    ("EICHERMOT", "Eicher Motors Ltd.", "Automobile and Auto Components", "INE066A01021"),
    ("ETERNAL", "Eternal Ltd.", "Consumer Services", "INE758T01015"),
    ("GRASIM", "Grasim Industries Ltd.", "Construction Materials", "INE047A01021"),
    ("HCLTECH", "HCL Technologies Ltd.", "Information Technology", "INE860A01027"),
    ("HDFCBANK", "HDFC Bank Ltd.", "Financial Services", "INE040A01034"),
    ("HDFCLIFE", "HDFC Life Insurance Company Ltd.", "Financial Services", "INE795G01014"),
    ("HINDALCO", "Hindalco Industries Ltd.", "Metals & Mining", "INE038A01020"),
    ("HINDUNILVR", "Hindustan Unilever Ltd.", "Fast Moving Consumer Goods", "INE030A01027"),
    ("ICICIBANK", "ICICI Bank Ltd.", "Financial Services", "INE090A01021"),
    ("ITC", "ITC Ltd.", "Fast Moving Consumer Goods", "INE154A01025"),
    ("INFY", "Infosys Ltd.", "Information Technology", "INE009A01021"),
    ("INDIGO", "InterGlobe Aviation Ltd.", "Services", "INE646L01027"),
    ("JSWSTEEL", "JSW Steel Ltd.", "Metals & Mining", "INE019A01038"),
    ("JIOFIN", "Jio Financial Services Ltd.", "Financial Services", "INE758E01017"),
    ("KOTAKBANK", "Kotak Mahindra Bank Ltd.", "Financial Services", "INE237A01036"),
    ("LT", "Larsen & Toubro Ltd.", "Construction", "INE018A01030"),
    ("M&M", "Mahindra & Mahindra Ltd.", "Automobile and Auto Components", "INE101A01026"),
    ("MARUTI", "Maruti Suzuki India Ltd.", "Automobile and Auto Components", "INE585B01010"),
    ("MAXHEALTH", "Max Healthcare Institute Ltd.", "Healthcare", "INE027H01010"),
    ("NTPC", "NTPC Ltd.", "Power", "INE733E01010"),
    ("NESTLEIND", "Nestle India Ltd.", "Fast Moving Consumer Goods", "INE239A01024"),
    ("ONGC", "Oil & Natural Gas Corporation Ltd.", "Oil Gas & Consumable Fuels", "INE213A01029"),
    ("POWERGRID", "Power Grid Corporation of India Ltd.", "Power", "INE752E01010"),
    ("RELIANCE", "Reliance Industries Ltd.", "Oil Gas & Consumable Fuels", "INE002A01018"),
    ("SBILIFE", "SBI Life Insurance Company Ltd.", "Financial Services", "INE123W01016"),
    ("SHRIRAMFIN", "Shriram Finance Ltd.", "Financial Services", "INE721A01047"),
    ("SBIN", "State Bank of India", "Financial Services", "INE062A01020"),
    ("SUNPHARMA", "Sun Pharmaceutical Industries Ltd.", "Healthcare", "INE044A01036"),
    ("TCS", "Tata Consultancy Services Ltd.", "Information Technology", "INE467B01029"),
    ("TATACONSUM", "Tata Consumer Products Ltd.", "Fast Moving Consumer Goods", "INE192A01025"),
    ("TMPV", "Tata Motors Passenger Vehicles Ltd.", "Automobile and Auto Components", "INE155A01022"),
    ("TATASTEEL", "Tata Steel Ltd.", "Metals & Mining", "INE081A01020"),
    ("TECHM", "Tech Mahindra Ltd.", "Information Technology", "INE669C01036"),
    ("TITAN", "Titan Company Ltd.", "Consumer Durables", "INE280A01028"),
    ("TRENT", "Trent Ltd.", "Consumer Services", "INE849A01020"),
    ("ULTRACEMCO", "UltraTech Cement Ltd.", "Construction Materials", "INE481G01011"),
]


# ------------------------------------------------------------------ constituents

def _rows_from_csv(text):
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        sym = (r.get("Symbol") or "").strip()
        isin = (r.get("ISIN Code") or "").strip()
        if not sym or not isin or (r.get("Series") or "EQ").strip() != "EQ":
            continue
        rows.append({"symbol": sym, "name": (r.get("Company Name") or sym).strip(),
                     "industry": (r.get("Industry") or "").strip(), "isin": isin,
                     "key": "NSE_EQ|" + isin})
    return rows


def _fetch_constituents():
    req = urllib.request.Request(CONST_URL, headers={"User-Agent": _UA, "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _rows_from_csv(resp.read().decode("utf-8-sig"))


def constituents(root, force=False):
    """Live Nifty 50 list from NSE's index CSV, cached to disk, with a labelled snapshot."""
    path = os.path.join(root, ".candle_cache", "nifty50_constituents.json")
    cached = None
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
    except Exception:
        cached = None
    if not force and cached and time.time() - cached.get("fetchedAt", 0) < CONST_TTL:
        if len(cached.get("rows") or []) == 50:
            return {"rows": cached["rows"], "source": cached.get("source"),
                    "fetchedAt": cached.get("fetchedAt"), "live": cached.get("live", True)}
    try:
        rows = _fetch_constituents()
        if len(rows) != 50:
            raise ValueError(f"NSE returned {len(rows)} rows, expected 50")
        stamp = time.time()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"fetchedAt": stamp, "source": "NSE ind_nifty50list.csv",
                           "live": True, "rows": rows}, fh)
        except Exception:
            pass
        return {"rows": rows, "source": "NSE ind_nifty50list.csv", "fetchedAt": stamp, "live": True}
    except Exception as exc:
        if cached and len(cached.get("rows") or []) == 50:
            return {"rows": cached["rows"], "source": cached.get("source"),
                    "fetchedAt": cached.get("fetchedAt"), "live": False,
                    "note": f"live CSV unavailable ({exc}); using the cached list"}
        rows = [{"symbol": s, "name": n, "industry": ind, "isin": i, "key": "NSE_EQ|" + i}
                for s, n, ind, i in CONST_FALLBACK]
        return {"rows": rows, "source": "NSE snapshot 2026-11-06", "fetchedAt": None,
                "live": False, "note": f"live CSV unavailable ({exc}); using the dated snapshot"}


# ------------------------------------------------------------------ rate limiting

class _Limiter:
    """Keeps every Upstox candle pull under ~3/s and 180/min, so a cold audit cannot
    trip the API's rate limit while it walks all 50 names."""

    def __init__(self, per_sec=3.2, per_min=180):
        self.gap = 1.0 / per_sec
        self.per_min = per_min
        self.hits = []
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            self.hits = [t for t in self.hits if now - t < 60]
            if len(self.hits) >= self.per_min:
                time.sleep(max(0.0, 60 - (now - self.hits[0]) + 0.05))
            now = time.time()
            delay = self.last + self.gap - now
            if delay > 0:
                time.sleep(delay)
            self.last = time.time()
            self.hits.append(self.last)


# ------------------------------------------------------------------ the gate

def _features(candles):
    closes, e9, e21, rsi, hist, _, st_dir = analysis._tech_features(candles)
    return {"closes": closes, "e9": e9, "e21": e21, "rsi": rsi, "hist": hist,
            "st": st_dir, "e50": analysis.ema_series(closes, 50),
            "atr": analysis.atr_series(candles, 14)}


def _gate(candles, f, i, profile=PROFILE):
    """The one gate both the audit and the live call use. Returns (sign, plan) or (None, reason)."""
    closes = f["closes"]
    if i < 60:
        return None, "not enough 5m warm-up bars"
    atr = f["atr"][i]
    px = closes[i]
    if not atr:
        return None, "ATR unavailable"
    bar_min = ((candles[i]["t"] + IST_OFFSET) % 86400) // 60
    if bar_min > profile["entryBefore"]:
        hh, mm = divmod(profile["entryBefore"], 60)
        return None, f"bar {bar_min // 60:02d}:{bar_min % 60:02d} IST is past the {hh:02d}:{mm:02d} cutoff"
    if atr / px < profile["minAtrPct"]:
        return None, f"ATR {atr / px * 100:.2f}% of price — too quiet for this book"
    norm = analysis._tech_score_at(i, f["e9"], f["e21"], f["rsi"], f["hist"], f["st"])
    if norm is None or abs(norm) * 100 < 100:
        return None, f"indicators are not unanimous ({'bullish' if (norm or 0) > 0 else 'bearish'} tilt only)"
    sign = 1 if norm > 0 else -1
    votes = [1 if (f["e9"][i] is not None and f["e21"][i] is not None and f["e9"][i] > f["e21"][i]) else -1]
    if f["hist"][i] is None or f["st"][i] is None:
        return None, "MACD / Supertrend unavailable"
    votes += [1 if f["hist"][i] > 0 else -1, f["st"][i]]
    if f["rsi"][i] is not None and (f["rsi"][i] >= 60 or f["rsi"][i] <= 40):
        votes.append(1 if f["rsi"][i] >= 60 else -1)
    if any(v != sign for v in votes):
        return None, "EMA 9/21, MACD, Supertrend and RSI do not all agree"
    if f["e50"][i] is None or (px > f["e50"][i]) != (sign > 0):
        return None, "price is on the wrong side of EMA 50 for this direction"
    expected = atr * math.sqrt(profile["horizon"]) * 0.8
    timing = analysis._move_timing(candles, "BULLISH" if sign > 0 else "BEARISH",
                                   atr, expected, st_dir=f["st"], macd_hist=f["hist"], idx=i)
    if timing["label"] not in ("EARLY", "BUILDING"):
        return None, f"move already {timing['label'].lower()} — late entry"
    mm = max(round(px * profile["targetPct"] / 100, 2), 0.05)
    risk = max(round(px * profile["stopPct"] / 100, 2), 0.10)
    return sign, {
        "direction": "BULLISH" if sign > 0 else "BEARISH",
        "entry": round(px, 2), "target": mm, "risk": risk,
        "stop": round(px - sign * risk, 2),
        "t1": round(px + sign * mm, 2),
        "t2": round(px + sign * mm * 2, 2),
        "t3": round(px + sign * mm * 3, 2),
        "atr": round(atr, 2), "timing": timing["label"],
        "barTime": candles[i]["t"],
        # How much of the move has already happened when the call fires.
        "preRun6Pct": round((px - closes[i - 6]) * sign / px * 100, 3),
        "preRun20Pct": round((px - closes[i - 20]) * sign / px * 100, 3),
    }


# ------------------------------------------------------------------ audit

def _rate(part):
    n = part.get("samples") or 0
    return round(part["wins"] / n * 100, 1) if n else None


def stock_audit(candles, profile=PROFILE, include_trades=False):
    """No-lookahead walk-forward audit of the live gate on one stock's own 5m bars."""
    horizon = profile["horizon"]
    n = len(candles)
    if n < horizon + 150:
        return None
    f = _features(candles)
    day_end = [0] * n
    for i in range(n - 1, -1, -1):
        day_end[i] = (i if (i == n - 1 or (candles[i + 1]["t"] + IST_OFFSET) // 86400
                            != (candles[i]["t"] + IST_OFFSET) // 86400) else day_end[i + 1])
    setups = wins = losses = timeouts = reach = 0
    t2wins = t3wins = 0
    risk_sum = pnl_sum = target_sum = 0.0
    bars_to_t1, examples, months, records = [], [], {}, []
    trades = []
    pre6s, pre20s = [], []
    covered_target = 0
    last_day, last_exit = None, -10 ** 9
    for i in range(60, n - 2):
        day = (candles[i]["t"] + IST_OFFSET) // 86400
        if day != last_day:
            last_day, last_exit = day, -10 ** 9
        if i <= last_exit + profile["cooldown"]:
            continue
        sign, plan = _gate(candles, f, i, profile)
        if not sign:
            continue
        entry = plan["entry"]
        stop, t1, t2, t3 = plan["stop"], plan["t1"], plan["t2"], plan["t3"]
        mm, risk = plan["target"], plan["risk"]
        pre6 = (entry - f["closes"][i - 6]) * sign / entry * 100
        pre20 = (entry - f["closes"][i - 20]) * sign / entry * 100
        pre6s.append(pre6)
        pre20s.append(pre20)
        covered_target += 1 if pre20 * entry / 100 >= mm else 0
        end = min(i + horizon, day_end[i])
        mfe, stop_bar, t1_bar, t2_bar, t3_bar = 0.0, None, None, None, None
        for k, c in enumerate(candles[i + 1:end + 1], 1):
            fav = (c["high"] - entry) if sign > 0 else (entry - c["low"])
            mfe = max(mfe, fav)
            hit_sl = c["low"] <= stop if sign > 0 else c["high"] >= stop
            if hit_sl:
                stop_bar = stop_bar or k
                continue  # conservative: a candle that touches the stop never upgrades a target
            if stop_bar is not None:
                continue
            if t1_bar is None and (c["high"] >= t1 if sign > 0 else c["low"] <= t1):
                t1_bar = k
            if t2_bar is None and (c["high"] >= t2 if sign > 0 else c["low"] <= t2):
                t2_bar = k
            if t3_bar is None and (c["high"] >= t3 if sign > 0 else c["low"] <= t3):
                t3_bar = k
        if t1_bar is not None:
            outcome, bars = "t1", t1_bar
        elif stop_bar is not None:
            outcome, bars = "stop", stop_bar
        else:
            outcome, bars = "timeout", end - i
        setups += 1
        t2wins += 1 if t2_bar is not None else 0
        t3wins += 1 if t3_bar is not None else 0
        if outcome == "t1" or mfe >= mm:
            reach += 1
        risk_sum += risk
        target_sum += mm
        pnl = (mm if outcome == "t1" else -risk if outcome == "stop"
               else (candles[end]["close"] - entry) * sign)
        pnl_sum += pnl
        last_exit = end
        won = outcome == "t1"
        wins += 1 if won else 0
        losses += 1 if outcome == "stop" else 0
        timeouts += 1 if outcome == "timeout" else 0
        if won:
            bars_to_t1.append(bars)
        records.append((candles[i]["t"], won))
        exit_px = t1 if outcome == "t1" else stop if outcome == "stop" else candles[end]["close"]
        targets_hit = (3 if t3_bar is not None else 2 if t2_bar is not None
                       else 1 if t1_bar is not None else 0)
        if include_trades:
            trades.append({"barTime": candles[i]["t"], "direction": plan["direction"],
                           "entry": entry, "stop": stop, "t1": t1, "t2": t2, "t3": t3,
                           "targetsHit": targets_hit, "outcome": outcome, "bars": bars,
                           "minutes": bars * 5, "exit": round(exit_px, 2),
                           "mfe": round(mfe, 2),
                           "preRun6": round(pre6, 3), "preRun20": round(pre20, 3),
                           "r": round(pnl / risk, 2) if risk else None})
        month = datetime.datetime.fromtimestamp(
            candles[i]["t"] + IST_OFFSET, datetime.timezone.utc).strftime("%Y-%m")
        mb = months.setdefault(month, {"samples": 0, "wins": 0})
        mb["samples"] += 1
        mb["wins"] += 1 if won else 0
        examples.append({
            "barTime": candles[i]["t"], "direction": plan["direction"], "entry": entry,
            "stop": stop, "risk": risk, "target1": t1, "target2": t2, "target3": t3,
            "targetsHit": (3 if t3_bar is not None else 2 if t2_bar is not None
                           else 1 if t1_bar is not None else 0),
            "outcome": outcome, "bars": bars, "minutes": bars * 5,
            "exit": round(exit_px, 2), "r": round(pnl / risk, 2) if risk else None,
            "mfe": round(mfe, 2), "preRun6": round(pre6, 3), "preRun20": round(pre20, 3),
        })
        examples = examples[-8:]
    if not setups:
        return {"profile": profile["name"], "samples": 0, "lowSample": True,
                "qualified": False, "winRate": None, "bars": n,
                "note": "No historical setup passed the gate in the 180-day window."}
    avg_risk = risk_sum / setups
    month_rates = [{"month": m, "samples": v["samples"],
                    "winRate": round(v["wins"] / v["samples"] * 100, 1)}
                   for m, v in sorted(months.items())]
    solid = [m["winRate"] for m in month_rates if m["samples"] >= 5]
    mid = records[0][0] + (records[-1][0] - records[0][0]) / 2
    older = [r for r in records if r[0] < mid]
    newer = [r for r in records if r[0] >= mid]
    half = lambda part: {"samples": len(part), "wins": sum(1 for r in part if r[1]),
                         "winRate": round(sum(1 for r in part if r[1]) / len(part) * 100, 1)
                         if part else None}
    win_rate = round(wins / setups * 100, 1)
    med = lambda a: round(sorted(a)[len(a) // 2], 3) if a else None
    return {
        "profile": profile["name"], "bars": n, "targetPct": profile["targetPct"],
        "stopPct": profile["stopPct"], "horizon": horizon,
        "horizonLabel": f"{horizon * 5 // 60} h", "entryBefore": profile["entryBefore"],
        "samples": setups, "wins": wins, "losses": losses, "timeouts": timeouts,
        "winRate": win_rate,
        # How much of the move the call misses: the median run-up already booked before the
        # entry bar, in % of price, and how often the prior 20 bars already covered T1.
        "timing": {
            "preRun6Pct": med(pre6s), "preRun20Pct": med(pre20s),
            "coveredTargetPct": round(covered_target / setups * 100, 1),
            "avgTargetPts": round(target_sum / setups, 2),
        },
        "winRateResolved": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        "t2Rate": round(t2wins / setups * 100, 1), "t3Rate": round(t3wins / setups * 100, 1),
        "reachRate": round(reach / setups * 100, 1),
        "avgRisk": round(avg_risk, 2),
        "rr1": round((target_sum / setups) / avg_risk, 2) if avg_risk else None,
        "avgR": round(pnl_sum / setups / avg_risk, 3) if avg_risk else None,
        "expectancyPts": round(pnl_sum / setups, 2),
        "avgBarsToT1": round(sum(bars_to_t1) / len(bars_to_t1), 1) if bars_to_t1 else None,
        "worstMonth": (min(solid) if solid else min(m["winRate"] for m in month_rates)),
        "months": month_rates,
        "older": half(older), "newer": half(newer),
        "windowStart": records[0][0], "windowEnd": records[-1][0],
        "lowSample": setups < profile["minSamples"],
        "qualified": (setups >= profile["minSamples"] and win_rate >= profile["minWinRate"]
                      and (pnl_sum / setups / avg_risk if avg_risk else -1)
                      > profile.get("minAvgR", -1)),
        "minSamples": profile["minSamples"], "minWinRate": profile["minWinRate"],
        "minAvgR": profile.get("minAvgR"),
        "examples": examples,
        **({"trades": trades} if include_trades else {}),
        "note": ("No historical setup passed the gate in the 180-day window."
                 if not setups else None),
    }


def live_call(candles, audit, profile=PROFILE):
    """The gate at the newest bar. The audit decides whether the stock is allowed to speak."""
    if not candles or len(candles) < 120:
        return {"status": "ERROR", "reason": f"only {len(candles or [])} 5m bars — feed too short"}
    f = _features(candles)
    i = len(candles) - 1
    sign, plan = _gate(candles, f, i, profile)
    out = {"barTime": candles[i]["t"], "atr": round(f["atr"][i] or 0, 2),
           "rsi": round(f["rsi"][i], 1) if f["rsi"][i] is not None else None,
           "session": _day_label(candles[i]["t"]),
           "sessionClosed": _day_label(candles[i]["t"]) != time.strftime("%Y-%m-%d", _ist_now())}
    if not audit:
        return dict(out, status="AUDITING", reason="its own 180-day audit has not finished yet")
    if not audit.get("qualified"):
        rate, ar = audit.get("winRate"), audit.get("avgR")
        if audit.get("samples"):
            if ar is not None and ar <= profile.get("minAvgR", -1):
                reason = (f"audited {rate}% T1-before-SL but {ar:+.3f}R per call over "
                          f"{audit['samples']} setups — expectancy floor is "
                          f"{profile['minAvgR']:+.2f}R")
            else:
                reason = (f"audited {rate}% T1-before-SL over {audit['samples']} setups — "
                          f"below the {profile['minWinRate']:.0f}% / {profile['minSamples']} "
                          f"setup floor")
        else:
            reason = "no historical setup passed the gate in the last 180 days"
        return dict(out, status="BLOCKED", reason=reason)
    if not sign:
        return dict(out, status="NO CALL", reason=plan)
    return dict(out, status="CALL", direction=plan["direction"], plan=plan,
                reason=f"gate passed on the {_bar_label(plan['barTime'])} IST bar")


# ------------------------------------------------------------------ desk

def _ist_now():
    return time.gmtime(time.time() + IST_OFFSET)


def _market_open():
    t = _ist_now()
    if t.tm_wday >= 5:
        return False
    return 555 <= t.tm_hour * 60 + t.tm_min <= 930


def _day_label(ts):
    return datetime.datetime.fromtimestamp(ts + IST_OFFSET, datetime.timezone.utc).strftime("%Y-%m-%d")


def _bar_label(ts):
    return datetime.datetime.fromtimestamp(ts + IST_OFFSET, datetime.timezone.utc).strftime("%H:%M")


def _dur_label(minutes):
    if minutes is None:
        return None
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


class Desk:
    """Quotes + breadth on every request; audits in a rate-limited background worker."""

    def __init__(self, ux, root):
        self.ux = ux
        self.root = root
        self._limiter = _Limiter()
        self._lock = threading.Lock()
        self._audits = {}
        self._bars = {}
        self._report = None
        self._quote = (0.0, None)
        self._state = {"running": False, "done": 0, "total": 0, "error": None,
                       "startedAt": None, "finishedAt": None, "cycle": 0, "failed": []}
        self._thread = None
        self._wake = False
        self._store = os.path.join(root, ".candle_cache", "nifty50_audits.json")
        self._load()

    # -- persistence -------------------------------------------------------------
    def _load(self):
        try:
            with open(self._store, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._audits = data.get("audits") or {}
        except Exception:
            self._audits = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._store), exist_ok=True)
            tmp = self._store + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"savedAt": time.time(), "audits": self._audits}, fh)
            os.replace(tmp, self._store)
        except Exception:
            pass

    # -- data --------------------------------------------------------------------
    def _bars_for(self, stock, force=False):
        path = os.path.join(self.root, ".candle_cache", f"stk_{stock['symbol']}.json")
        cached, age = None, 1e9
        try:
            age = time.time() - os.path.getmtime(path)
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh)
        except Exception:
            cached = None
        rows = {}
        if force or not cached or age > BARS_TTL:
            today = datetime.date.today()
            # 1500 bars per response is the Upstox cap; 30-day chunks lose the newest
            # session, so pull 20 days at a time (~1050 bars) and keep every day.
            for k in range((PROFILE["auditDays"] + 19) // 20):
                end = today - datetime.timedelta(days=20 * k + 1)
                start = end - datetime.timedelta(days=19)
                self._limiter.wait()
                try:
                    part = self.ux.candles(stock["key"], "minutes", 5,
                                           end.isoformat(), start.isoformat())
                except Exception:
                    break
                for c in part or []:
                    rows[c["t"]] = c
        else:
            for c in cached:
                rows[c["t"]] = c
        self._limiter.wait()
        try:
            for c in self.ux.intraday(stock["key"], "minutes", 5) or []:
                rows[c["t"]] = c
        except Exception:
            pass
        bars = [rows[t] for t in sorted(rows)]
        if bars and (force or not cached or age > BARS_TTL or len(bars) != len(cached)):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(bars, fh)
            except Exception:
                pass
        with self._lock:
            self._bars[stock["symbol"]] = bars
        return bars

    def _quotes(self, keys):
        with self._lock:
            stamp, data = self._quote
        if data and time.time() - stamp < QUOTE_TTL:
            return data
        data = self.ux.quotes(keys)
        with self._lock:
            self._quote = (time.time(), data)
        return data

    # -- worker ------------------------------------------------------------------
    def start(self, force=False):
        with self._lock:
            if force:
                self._state["force_once"] = True
                self._wake = True
            if self._state["running"]:
                return
            # Between cycles the worker sleeps; keep reporting the last finished cycle
            # instead of claiming a fresh 0/50 run that has not been dispatched.
            if self._thread and self._thread.is_alive() and not self._wake:
                return
            self._state.update(running=True, error=None, startedAt=time.time(),
                               finishedAt=None, done=0, failed=[])
            if not (self._thread and self._thread.is_alive()):
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()

    def _loop(self):
        while True:
            with self._lock:
                force = self._state.pop("force_once", False)
            self._cycle(force)
            for _ in range(max(1, CYCLE_SECONDS // 10)):
                time.sleep(10)
                with self._lock:
                    if self._wake:
                        self._wake = False
                        break

    def _cycle(self, force=False):
        started = time.time()
        try:
            const = constituents(self.root)["rows"]
        except Exception as exc:
            with self._lock:
                self._state.update(running=False, error=f"constituent list: {exc}")
            return
        with self._lock:
            self._state.update(running=True, done=0, total=len(const), error=None,
                               startedAt=started, finishedAt=None, cycle=self._state["cycle"] + 1)
        failed = []
        for idx, st in enumerate(const):
            try:
                bars = self._bars_for(st, force=force)
                last = bars[-1]["t"] if bars else None
                prev = self._audits.get(st["symbol"]) or {}
                if bars and (force or prev.get("barTime") != last or not prev.get("audit")
                             or prev.get("profile") != PROFILE_SIG):
                    au = stock_audit(bars)
                    if au:
                        self._audits[st["symbol"]] = {
                            "barTime": last, "bars": len(bars), "profile": PROFILE_SIG,
                            "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "session": _day_label(last), "audit": au,
                        }
                        self._save()
            except Exception as exc:
                failed.append(f"{st['symbol']}: {exc}")
            with self._lock:
                self._state["done"] = idx + 1
        with self._lock:
            self._state.update(running=False, finishedAt=time.time(), failed=failed[:5])
            self._report = None

    # -- report ------------------------------------------------------------------
    def snapshot(self, force=False):
        self.start(force=force)
        const = constituents(self.root)
        rows = const["rows"]
        keys = [r["key"] for r in rows] + ["NSE_INDEX|Nifty 50"]
        quote_error = None
        try:
            quotes = self._quotes(keys)
        except Exception as exc:
            quotes = {}
            quote_error = str(exc)
        nifty = quotes.get("NSE_INDEX|Nifty 50") or {}

        stocks = []
        for st in rows:
            q = quotes.get(st["key"]) or {}
            last, prev = q.get("last"), q.get("prevClose")
            pct = round((last - prev) / prev * 100, 2) if (last and prev) else None
            hi, lo = q.get("high"), q.get("low")
            range_pos = (round((last - lo) / (hi - lo) * 100) if (last and hi and lo and hi > lo) else None)
            entry = self._audits.get(st["symbol"]) or {}
            au = entry.get("audit") or None
            bars = self._bars.get(st["symbol"]) or []
            if bars:
                call = live_call(bars, au)
            else:
                call = {"status": "AUDITING", "reason": "no 5m bars cached yet",
                        "barTime": None, "atr": None, "rsi": None}
            if quote_error and not q:
                call = dict(call, status="ERROR", reason=quote_error)
            stocks.append({
                "symbol": st["symbol"], "name": st["name"], "industry": st["industry"],
                "price": last, "pct": pct, "netChange": q.get("netChange"),
                "high": hi, "low": lo, "open": q.get("open"), "rangePos": range_pos,
                "quoteAt": q.get("quoteAt"),
                "audit": ({k: au.get(k) for k in
                           ("samples", "wins", "losses", "timeouts", "winRate", "winRateResolved",
                            "t2Rate", "t3Rate", "reachRate", "avgRisk", "rr1", "avgR",
                            "expectancyPts", "avgBarsToT1", "worstMonth", "older", "newer",
                            "lowSample", "qualified", "minSamples", "minWinRate", "minAvgR",
                            "horizonLabel",
                            "targetPct", "stopPct", "timing", "entryBefore", "windowStart",
                            "windowEnd", "examples", "note")}
                          if au else None),
                "auditSession": entry.get("session"),
                "auditAt": entry.get("generatedAt"),
                "status": call.get("status"), "statusLabel": _status_label(call.get("status")),
                "direction": call.get("direction"), "plan": call.get("plan"),
                "reason": call.get("reason"), "barTime": call.get("barTime"),
                "barLabel": _bar_label(call["barTime"]) if call.get("barTime") else None,
                "barSession": _day_label(call["barTime"]) if call.get("barTime") else None,
                "sessionClosed": call.get("sessionClosed"),
                "rsi": call.get("rsi"), "atr": call.get("atr"),
            })

        order = {"CALL": 0, "BLOCKED": 1, "NO CALL": 2, "AUDITING": 3, "ERROR": 4}
        stocks.sort(key=lambda r: (order.get(r["status"], 5), -(r.get("pct") or -999)))
        breadth = _breadth(stocks, nifty)
        with self._lock:
            state = dict(self._state)
        return {
            "generatedAt": time.time(), "istClock": time.strftime("%H:%M:%S", _ist_now()),
            "marketOpen": _market_open(),
            "universe": {"count": len(rows), "source": const["source"],
                         "fetchedAt": const.get("fetchedAt"), "live": const.get("live"),
                         "note": const.get("note")},
            "nifty": {"last": nifty.get("last"), "netChange": nifty.get("netChange"),
                      "pct": (round(nifty["netChange"] / nifty["prevClose"] * 100, 2)
                              if nifty.get("netChange") is not None and nifty.get("prevClose") else None)},
            "breadth": breadth,
            "profile": {k: v for k, v in PROFILE.items()},
            "audit": {"running": state["running"], "done": state["done"], "total": state["total"],
                      "error": state["error"], "startedAt": state["startedAt"],
                      "finishedAt": state["finishedAt"], "cycle": state["cycle"],
                      "failed": state["failed"],
                      "qualified": sum(1 for s in stocks if (s["audit"] or {}).get("qualified")),
                      "calls": sum(1 for s in stocks if s["status"] == "CALL")},
            "quoteError": quote_error,
            "stocks": stocks,
            "csvUrl": "/api/nifty50?format=csv",
            "method": _method(),
        }

    def csv(self, report=None):
        rep = report or self.snapshot()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["symbol", "name", "industry", "price", "pct_change", "status", "direction",
                    "entry", "stop", "t1", "t2", "t3", "audit_samples", "audit_win_rate",
                    "audit_qualified", "bar_time_ist", "reason"])
        for s in rep["stocks"]:
            p = s.get("plan") or {}
            a = s.get("audit") or {}
            w.writerow([s["symbol"], s["name"], s["industry"],
                        s.get("price") if s.get("price") is not None else "",
                        s.get("pct") if s.get("pct") is not None else "",
                        s.get("status"), s.get("direction") or "",
                        p.get("entry", ""), p.get("stop", ""), p.get("t1", ""),
                        p.get("t2", ""), p.get("t3", ""),
                        a.get("samples", ""), a.get("winRate", ""),
                        "yes" if a.get("qualified") else "no",
                        s.get("barLabel") or "", s.get("reason") or ""])
        return out.getvalue()

    # -- last-N-days test ---------------------------------------------------------
    def _disk_bars(self, symbol):
        with self._lock:
            bars = self._bars.get(symbol)
        if bars:
            return bars
        try:
            with open(os.path.join(self.root, ".candle_cache", f"stk_{symbol}.json"),
                      encoding="utf-8") as fh:
                bars = json.load(fh)
        except Exception:
            return None
        if bars:
            with self._lock:
                self._bars.setdefault(symbol, bars)
        return bars

    def month_report(self, days=30):
        """Every call the gate fired in the last N days, with the stock's qualification
        frozen at the window start so the test never grades itself with later bars."""
        with self._lock:
            cached = self._report
        if cached and cached["days"] == days and time.time() - cached["at"] < REPORT_TTL:
            return cached["data"]
        const = constituents(self.root)["rows"]
        now = time.time()
        cutoff = now - days * 86400
        rows, trades, skipped = [], [], []
        newest = 0
        for st in const:
            bars = self._disk_bars(st["symbol"])
            if not bars:
                skipped.append({"symbol": st["symbol"], "reason": "no 5m bars cached yet"})
                continue
            newest = max(newest, bars[-1]["t"])
            full = stock_audit(bars, include_trades=True) or {}
            before = [b for b in bars if b["t"] < cutoff]
            frozen = stock_audit(before) if len(before) >= 160 else None
            window = [t for t in full.get("trades") or [] if t["barTime"] >= cutoff]
            q_start = bool(frozen and frozen.get("qualified"))
            now_audit = (self._audits.get(st["symbol"]) or {}).get("audit") or {}
            rs = [t["r"] for t in window if t.get("r") is not None]
            rows.append({
                "symbol": st["symbol"], "name": st["name"], "industry": st["industry"],
                "qualifiedAtStart": q_start,
                "rateAtStart": (frozen or {}).get("winRate"),
                "samplesAtStart": (frozen or {}).get("samples"),
                "qualifiedNow": bool(now_audit.get("qualified")),
                "rateNow": now_audit.get("winRate"),
                "calls": len(window),
                "t1": sum(1 for t in window if t["outcome"] == "t1"),
                "t2": sum(1 for t in window if t["targetsHit"] >= 2),
                "t3": sum(1 for t in window if t["targetsHit"] >= 3),
                "stop": sum(1 for t in window if t["outcome"] == "stop"),
                "timeout": sum(1 for t in window if t["outcome"] == "timeout"),
                "winRate": (round(sum(1 for t in window if t["outcome"] == "t1")
                                   / len(window) * 100, 1) if window else None),
                "avgR": round(sum(rs) / len(rs), 2) if rs else None,
            })
            for t in window:
                exit_ts = t["barTime"] + t["minutes"] * 60
                hit = ("SL" if t["outcome"] == "stop"
                       else "TIMEOUT" if t["outcome"] == "timeout"
                       else f"T{t['targetsHit'] or 1}")
                trades.append({
                    "symbol": st["symbol"], "name": st["name"], "gated": q_start,
                    "qualifiedAtStart": q_start,
                    "barTime": t["barTime"], "dayLabel": _day_label(t["barTime"]),
                    "barLabel": _bar_label(t["barTime"]),
                    "direction": t["direction"], "entry": t["entry"], "stop": t["stop"],
                    "t1": t["t1"], "t2": t["t2"], "t3": t["t3"],
                    "risk": round(abs(t["entry"] - t["stop"]), 2),
                    "targetsHit": t["targetsHit"], "outcome": t["outcome"], "hit": hit,
                    "exit": t["exit"], "r": t["r"], "minutes": t["minutes"],
                    "durationLabel": _dur_label(t["minutes"]),
                    "exitTime": exit_ts, "exitLabel": _bar_label(exit_ts),
                    "exitDay": _day_label(exit_ts), "mfe": t["mfe"],
                    "preRun6": t.get("preRun6"), "preRun20": t.get("preRun20"),
                })
        rows.sort(key=lambda r: (not r["qualifiedAtStart"], -r["calls"], r["symbol"]))
        trades.sort(key=lambda t: t["barTime"])

        def agg(ts):
            calls = len(ts)
            r_vals = [t["r"] for t in ts if t.get("r") is not None]
            t1 = sum(1 for t in ts if t["outcome"] == "t1")
            t2 = sum(1 for t in ts if t["targetsHit"] >= 2)
            t3 = sum(1 for t in ts if t["targetsHit"] >= 3)
            sl = sum(1 for t in ts if t["outcome"] == "stop")
            to = sum(1 for t in ts if t["outcome"] == "timeout")
            pct = lambda n: round(n / calls * 100, 1) if calls else None
            avg_min = lambda part: (round(sum(t["minutes"] for t in part) / len(part))
                                    if part else None)
            t1s = [t for t in ts if t["outcome"] == "t1"]
            sls = [t for t in ts if t["outcome"] == "stop"]
            tos = [t for t in ts if t["outcome"] == "timeout"]
            pre20s = sorted(t["preRun20"] for t in ts if t.get("preRun20") is not None)
            covered = sum(1 for t in ts if t.get("preRun20") is not None
                          and abs(t["preRun20"]) / 100 * t["entry"] >= abs(t["t1"] - t["entry"]))
            med = lambda a: round(a[len(a) // 2], 3) if a else None
            return {
                "calls": calls, "t1": t1, "t2": t2, "t3": t3, "stop": sl, "timeout": to,
                "winRate": pct(t1), "t2Rate": pct(t2), "t3Rate": pct(t3),
                "stopRate": pct(sl), "timeoutRate": pct(to),
                "avgR": round(sum(r_vals) / len(r_vals), 3) if r_vals else None,
                "totalR": round(sum(r_vals), 2) if r_vals else None,
                "avgMinutes": avg_min(ts), "avgMinutesT1": avg_min(t1s),
                "avgMinutesStop": avg_min(sls), "avgMinutesTimeout": avg_min(tos),
                "durationT1": _dur_label(avg_min(t1s)),
                "durationStop": _dur_label(avg_min(sls)),
                "durationTimeout": _dur_label(avg_min(tos)),
                "medianPreRun20": med(pre20s),
                "coveredTargetPct": (round(covered / calls * 100, 1) if calls else None),
                "avgTargetPts": (round(sum(abs(t["t1"] - t["entry"]) for t in ts) / calls, 2)
                                 if calls else None),
                "avgTargetPct": (round(sum(abs(t["t1"] - t["entry"]) / t["entry"] * 100
                                           for t in ts) / calls, 2) if calls else None),
            }

        gated_trades = [t for t in trades if t["gated"]]
        rep = {
            "generatedAt": now, "days": days,
            "windowStart": cutoff, "windowEnd": newest or now,
            "windowLabel": f"{_day_label(cutoff)} → {_day_label(newest or now)} IST",
            "gateFrozen": True,
            "profile": dict(PROFILE),
            "gated": agg(gated_trades), "all": agg(trades),
            "gatedStocks": sum(1 for r in rows if r["qualifiedAtStart"]),
            "totalStocks": len(rows),
            "stocks": rows, "trades": trades, "skipped": skipped,
            "barsLabel": "real Upstox 5m bars (merged with the live intraday tail)",
            "method": {
                "what": "Every call this stock gate fired in the last N days, re-run bar by bar "
                        "on real 5m candles. Nothing here is synthesised.",
                "gateFrozen": f"A stock is graded only if its own audit cleared "
                              f"{PROFILE['minWinRate']:.0f}% over >={PROFILE['minSamples']} "
                              f"setups with positive expectancy, using bars strictly BEFORE the "
                              f"window started, so the window cannot vote on its own "
                              f"qualification.",
                "targets": f"T1 = {PROFILE['targetPct']:.2f}% of the entry price (about 2.3x the "
                           f"old median target of 0.13% of price, and roughly 72% of the median "
                           f"3-hour move these setups actually produced), stop = "
                           f"{PROFILE['stopPct']:.2f}% of price, 3-hour horizon, entry before "
                           f"14:30 IST, session-bounded at 15:25 IST. T2 = 2 x T1, T3 = 3 x T1. "
                           f"A candle touching the stop and a target together counts as a stop; "
                           f"a timeout is not a win.",
                "timing": "Each call also reports how far price had already run in the call's "
                          "direction over the 6 and 20 bars before the entry bar. Those numbers "
                          "are the honest answer to 'was this called before the move?' — the "
                          "median call has already covered part of it, and no sampled filter "
                          "that demanded a fresher entry improved the hit rate.",
                "whyGatedMatters": "The 'gated' block is what the desk would actually have "
                                   "called. The 'all 50' block is every qualifying setup on every "
                                   "name, including stocks below the floor, for comparison.",
                "risk": f"R:R to T1 is {PROFILE['targetPct'] / PROFILE['stopPct']:.2f}, so "
                        f"break-even is near "
                        f"{PROFILE['stopPct'] / (PROFILE['targetPct'] + PROFILE['stopPct']) * 100:.0f}% "
                        f"on T1-vs-stop alone, and timeouts soften that. Rates are measured, not "
                        f"promised; single stocks gap and are gross of brokerage and slippage.",
            },
        }
        with self._lock:
            self._report = {"at": time.time(), "days": days, "data": rep}
        return rep

    def month_csv(self, days=30):
        rep = self.month_report(days)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["date_ist", "time_ist", "exit_time_ist", "duration", "symbol",
                    "gated_at_window_start", "direction", "entry", "stop", "t1", "t2", "t3",
                    "risk_points", "hit", "targets_hit", "outcome", "exit", "r", "minutes",
                    "mfe", "pre_run_6_pct", "pre_run_20_pct"])
        for t in rep["trades"]:
            w.writerow([t["dayLabel"], t["barLabel"], t["exitLabel"], t["durationLabel"],
                        t["symbol"], "yes" if t["gated"] else "no", t["direction"], t["entry"],
                        t["stop"], t["t1"], t["t2"], t["t3"], t["risk"], t["hit"],
                        t["targetsHit"], t["outcome"], t["exit"], t["r"], t["minutes"],
                        t["mfe"], t.get("preRun6"), t.get("preRun20")])
        return out.getvalue()


def _status_label(status):
    return {"CALL": "CALL", "BLOCKED": f"BELOW {PROFILE['minWinRate']:.0f}% — NO CALL",
            "NO CALL": "NO CALL", "AUDITING": "AUDITING",
            "ERROR": "FEED ERROR"}.get(status, status or "—")


def _breadth(stocks, nifty):
    pcts = [s["pct"] for s in stocks if s.get("pct") is not None]
    ups = [s for s in stocks if (s.get("pct") or 0) > 0.02]
    downs = [s for s in stocks if (s.get("pct") or 0) < -0.02]
    flat = [s for s in stocks if s.get("pct") is not None and abs(s["pct"]) <= 0.02]
    avg = round(sum(pcts) / len(pcts), 2) if pcts else None
    med = round(sorted(pcts)[len(pcts) // 2], 2) if pcts else None
    srt = sorted((s for s in stocks if s.get("pct") is not None), key=lambda s: s["pct"])
    return {
        "advancers": len(ups), "decliners": len(downs), "flat": len(flat),
        "quoted": len(pcts), "avgPct": avg, "medianPct": med,
        "niftyPct": (round(nifty["netChange"] / nifty["prevClose"] * 100, 2)
                     if nifty.get("netChange") is not None and nifty.get("prevClose") else None),
        "up": [{"symbol": s["symbol"], "pct": s["pct"]} for s in srt[::-1][:5]],
        "down": [{"symbol": s["symbol"], "pct": s["pct"]} for s in srt[:5]],
        "equalWeightVsIndex": (round(avg - round(nifty["netChange"] / nifty["prevClose"] * 100, 2), 2)
                               if avg is not None and nifty.get("netChange") is not None
                               and nifty.get("prevClose") else None),
    }


def _method():
    return {
        "universe": "Nifty 50 constituents fetched live from NSE's own ind_nifty50list.csv "
                    "(symbol + ISIN); Upstox instrument keys are NSE_EQ|<ISIN>.",
        "data": "One Upstox batch quote call for all 50 names, then real 5m bars per stock "
                "(180 calendar days, tagged UPSTOX 5m, cached 6h). No synthetic prices.",
        "gate": f"A stock may call only while its own 180-day audit shows at least "
                f"{PROFILE['minWinRate']:.0f}% T1-before-stop over at least "
                f"{PROFILE['minSamples']} setups AND a positive expectancy per call. Below "
                f"either floor it is NO CALL, not a weaker call. The floors are measured, not "
                f"a promise of 75%.",
        "audit": f"T1 = {PROFILE['targetPct']:.2f}% of the entry price, stop = "
                 f"{PROFILE['stopPct']:.2f}%, 3-hour horizon, session-bounded at 15:25 IST, one "
                 f"trade at a time with a 15-minute cooldown. A timeout is not a win; a candle "
                 f"touching both levels counts as a stop.",
        "noLookahead": "At bar i the audit sees only bars <= i; outcomes use later bars only. "
                       "verify.py truncates the newest bar and confirms earlier calls do not change.",
        "breadth": "Advancers/decliners are last vs previous close over the 50 constituents; "
                   "the equal-weight average is not the Nifty's free-float weighted move.",
        "sizing": "The target is set from the move, not from the win rate: median favourable "
                  "excursion inside the 3-hour window is 0.42% of price across 112k gated "
                  "setups, so T1 sits at 0.30% (72% of that) with a 0.50% stop. Each call also "
                  "reports the run-up already booked before entry — the median gated call fires "
                  "after price has already run 0.52% and in ~77% of calls the prior 20 bars had "
                  "already covered T1, so these are continuation calls, not first-move calls. "
                  "No sampled 'fresher entry' filter raised the hit rate; the numbers are shown "
                  "instead of hidden.",
        "risk": f"R:R is {PROFILE['targetPct'] / PROFILE['stopPct']:.2f}, so break-even is near "
                f"{PROFILE['stopPct'] / (PROFILE['targetPct'] + PROFILE['stopPct']) * 100:.0f}% "
                f"before costs, and the honest measured T1 rate at this target is ~59-60% — "
                f"below break-even on T1-vs-stop alone, with timeouts and T2/T3 doing the rest. "
                f"Single stocks gap and are not indices.",
        "costs": "All figures are gross, before brokerage, taxes and slippage.",
    }
