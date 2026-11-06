"""Next-day ("buy today's close, sell tomorrow's close") overnight model.

This is deliberately a separate model from the intraday desk: it only uses completed daily
bars, it is long-only, and its audited exit is the next session's close. A signal is emitted
at a day's close when that close is oversold:

    RSI(14) <= 38  AND  3-day return <= -1.0%

Entry = the signal day's close. Outcome = the next session's close. No look-ahead: bar i
only sees bars <= i, and the outcome uses bar i+1. No stop or target is part of the audited
result; the next-day range and excursion stats are context only.

The rule was fixed on economic logic (oversold mean reversion) and then measured on the same
three years of daily bars it reports, so the rates are in-sample. The audit exposes the older
two-thirds vs the newest third so that stability is visible, and the live gate refuses a call
when a segment's own measured rate falls below the floor.
"""
from __future__ import annotations

import datetime
import statistics

import analysis

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

PARAMS = {
    "name": "Overnight oversold bounce",
    "side": "LONG",
    "rsiPeriod": 14,
    "atrPeriod": 14,
    "rsiMax": 38.0,
    "ret3Max": -0.01,
    "gradeA": {"rsiMax": 30.0, "ret3Max": -0.02},
    "targetAtr": 0.5,
    "lookback": 60,
    "minSamples": 30,
    "minWinRate": 55.0,
    "strongWinRate": 60.0,
    "description": "Buy the close when RSI(14) <= 38 and the 3-day return <= -1%; sell the "
                   "next session's close. Long-only overnight mean reversion.",
}


def ist_date(ts):
    return datetime.datetime.fromtimestamp(ts, IST).date()


def prepare(rows):
    """Sort/dedupe real daily bars and compute the indicators once."""
    by_day = {}
    for c in rows or []:
        try:
            ts = int(c["t"])
            o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(x == x for x in (o, h, l, cl)) or cl <= 0:
            continue
        by_day[ts] = {"t": ts, "open": o, "high": h, "low": l, "close": cl,
                      "volume": c.get("volume") or 0, "src": c.get("src")}
    bars = [by_day[k] for k in sorted(by_day)]
    closes = [c["close"] for c in bars]
    return {
        "bars": bars,
        "closes": closes,
        "rsi": analysis.rsi_series(closes, PARAMS["rsiPeriod"]),
        "atr": analysis.atr_series(bars, PARAMS["atrPeriod"]),
    }


def _features(prepared, i):
    bars, closes = prepared["bars"], prepared["closes"]
    if i < 0 or i >= len(bars):
        return None
    rsi, atr = prepared["rsi"][i], prepared["atr"][i]
    if rsi is None or atr is None or atr <= 0 or i < 3:
        return None
    c = bars[i]
    ret3 = closes[i] / closes[i - 3] - 1
    rng = c["high"] - c["low"]
    return {
        "rsi": round(rsi, 2),
        "atr": atr,
        "ret3": ret3,
        "ret3Pct": round(ret3 * 100, 2),
        "close": c["close"],
        "strength": round((c["close"] - c["low"]) / rng, 3) if rng > 0 else 0.5,
        "date": ist_date(c["t"]).isoformat(),
        "ts": c["t"],
    }


def _grade(feat, params):
    a = params["gradeA"]
    return "A" if (feat["rsi"] <= a["rsiMax"] and feat["ret3"] <= a["ret3Max"]) else "B"


def signal_at(prepared, i, params=None):
    """The rule at bar i. Uses only bars <= i. Returns None or a LONG signal dict."""
    p = params or PARAMS
    feat = _features(prepared, i)
    if not feat:
        return None
    if feat["rsi"] > p["rsiMax"] or feat["ret3"] > p["ret3Max"]:
        return None
    return {
        "side": "LONG",
        "grade": _grade(feat, p),
        "date": feat["date"],
        "entry": round(feat["close"], 2),
        "rsi": feat["rsi"],
        "ret3Pct": feat["ret3Pct"],
        "atr": round(feat["atr"], 2),
        "strength": feat["strength"],
    }


def collect(rows, params=None, since_ts=None):
    """Every signal with its next-session outcome, strictly no look-ahead.

    Signal at bar i uses bars <= i; the outcome is the close (and open/high/low) of bar i+1.
    """
    p = params or PARAMS
    prepared = prepare(rows)
    bars, closes = prepared["bars"], prepared["closes"]
    trades = []
    for i in range(p["lookback"], len(bars) - 1):
        sig = signal_at(prepared, i, p)
        if not sig:
            continue
        if since_ts is not None and bars[i]["t"] < since_ts:
            continue
        feat = _features(prepared, i)
        nxt = bars[i + 1]
        entry = closes[i]
        pts = nxt["close"] - entry
        target = entry + p["targetAtr"] * feat["atr"]
        trades.append({
            "date": sig["date"],
            "nextDate": ist_date(nxt["t"]).isoformat(),
            "ts": bars[i]["t"],
            "side": "LONG",
            "grade": sig["grade"],
            "rsi": sig["rsi"],
            "ret3Pct": sig["ret3Pct"],
            "atr": sig["atr"],
            "strength": sig["strength"],
            "entry": round(entry, 2),
            "exit": round(nxt["close"], 2),
            "points": round(pts, 2),
            "pct": round(pts / entry * 100, 3),
            "win": pts > 0,
            "target": round(target, 2),
            "targetHit": nxt["close"] >= target,
            "targetTouched": nxt["high"] >= target,
            "gapPts": round(nxt["open"] - entry, 2),
            "gapWin": nxt["open"] > entry,
            "mfe": round(nxt["high"] - entry, 2),
            "mae": round(nxt["low"] - entry, 2),
        })
    return trades


def _basic(trades):
    n = len(trades)
    if not n:
        return {"samples": 0, "wins": 0, "losses": 0, "winRate": None,
                "avgPts": None, "avgPct": None, "profitFactor": None,
                "expectancyPts": None, "targetRate": None, "touchTargetPct": None,
                "gapWinRate": None, "avgGapPts": None, "medianMfe": None,
                "medianMae": None, "bestPts": None, "worstPts": None}
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    gross_win = sum(t["points"] for t in wins)
    gross_loss = -sum(t["points"] for t in losses)
    return {
        "samples": n,
        "wins": len(wins),
        "losses": len(losses),
        "winRate": round(len(wins) / n * 100, 1),
        "avgPts": round(sum(t["points"] for t in trades) / n, 1),
        "avgPct": round(sum(t["pct"] for t in trades) / n, 3),
        "avgWinPts": round(gross_win / len(wins), 1) if wins else None,
        "avgLossPts": round(-gross_loss / len(losses), 1) if losses else None,
        "profitFactor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancyPts": round((gross_win - gross_loss) / n, 1),
        "targetRate": round(sum(t["targetHit"] for t in trades) / n * 100, 1),
        "touchTargetPct": round(sum(t["targetTouched"] for t in trades) / n * 100, 1),
        "gapWinRate": round(sum(t["gapWin"] for t in trades) / n * 100, 1),
        "avgGapPts": round(sum(t["gapPts"] for t in trades) / n, 1),
        "medianMfe": round(statistics.median(t["mfe"] for t in trades), 1),
        "medianMae": round(statistics.median(t["mae"] for t in trades), 1),
        "bestPts": round(max(t["points"] for t in trades), 1),
        "worstPts": round(min(t["points"] for t in trades), 1),
    }


def _months(trades):
    months = {}
    for t in trades:
        months.setdefault(t["date"][:7], []).append(t["points"])
    if not months:
        return {"months": 0, "monthsPositive": 0, "worstMonth": None, "bestMonth": None}
    ranked = sorted(months.items(), key=lambda kv: sum(kv[1]))
    return {
        "months": len(months),
        "monthsPositive": sum(1 for v in months.values() if sum(v) > 0),
        "worstMonth": {"month": ranked[0][0], "pts": round(sum(ranked[0][1]), 1)},
        "bestMonth": {"month": ranked[-1][0], "pts": round(sum(ranked[-1][1]), 1)},
    }


def summarize(trades, params=None, examples_limit=12):
    """Honest stats for a list of overnight trades."""
    p = params or PARAMS
    trades = sorted(trades, key=lambda t: t["ts"])
    stats = _basic(trades)
    n = stats["samples"]
    if not n:
        stats.update({"lowSample": True, "qualified": False, "older": _basic([]),
                      "newer": _basic([]), "grades": {}, "uniqueDates": 0,
                      "dateWinRate": None, "examples": [], "windowStart": None,
                      "windowEnd": None, "note": "no oversold closes in the window"})
        return stats
    cut = max(1, int(n * 2 / 3))
    older = _basic(trades[:cut])
    newer = _basic(trades[cut:])
    by_date = {}
    for t in trades:
        by_date.setdefault(t["date"], []).append(t["points"])
    stats.update({
        "older": older,
        "newer": newer,
        "grades": {
            "A": _basic([t for t in trades if t["grade"] == "A"]),
            "B": _basic([t for t in trades if t["grade"] == "B"]),
        },
        "uniqueDates": len(by_date),
        "dateWinRate": round(sum(1 for v in by_date.values() if sum(v) > 0)
                             / len(by_date) * 100, 1),
        "windowStart": trades[0]["date"],
        "windowEnd": trades[-1]["date"],
        "lowSample": n < p["minSamples"],
        "qualified": n >= p["minSamples"] and (stats["winRate"] or 0) >= p["minWinRate"],
        "examples": list(reversed(trades))[:examples_limit] if examples_limit else [],
    })
    stats.update(_months(trades))
    stats["rule"] = rule_text(p)
    return stats


def live(rows, params=None):
    """The signal at the newest bar, with the plan and the reasons when there is no signal."""
    p = params or PARAMS
    prepared = prepare(rows)
    bars = prepared["bars"]
    if len(bars) < p["lookback"]:
        return {"ok": False, "signal": False,
                "reason": f"only {len(bars)} daily bars — need {p['lookback']} to measure"}
    i = len(bars) - 1
    feat = _features(prepared, i)
    if not feat:
        return {"ok": False, "signal": False, "reason": "daily indicators unavailable"}
    sig = signal_at(prepared, i, p)
    reasons = []
    if feat["rsi"] > p["rsiMax"]:
        reasons.append(f"RSI(14) {feat['rsi']:.1f} is above {p['rsiMax']:.0f}")
    if feat["ret3"] > p["ret3Max"]:
        reasons.append(f"3-day return {feat['ret3Pct']:+.2f}% is above {p['ret3Max'] * 100:.2f}%")
    out = {
        "ok": True,
        "signal": bool(sig),
        "side": "LONG" if sig else None,
        "date": feat["date"],
        "entry": round(feat["close"], 2),
        "rsi": feat["rsi"],
        "ret3Pct": feat["ret3Pct"],
        "atr": round(feat["atr"], 2),
        "strength": feat["strength"],
        "exitRule": "next session's close",
        "target": round(feat["close"] + p["targetAtr"] * feat["atr"], 2),
        "targetMove": round(p["targetAtr"] * feat["atr"], 2),
        "riskGuide": round(feat["close"] - feat["atr"], 2),
        "riskGuidePts": round(feat["atr"], 2),
        "range": round(feat["atr"], 2),
        "grade": sig["grade"] if sig else None,
        "reasons": reasons,
        "rule": rule_text(p),
    }
    return out


def rule_text(params=None):
    p = params or PARAMS
    a = p["gradeA"]
    return (f"LONG at the close when RSI({p['rsiPeriod']}) <= {p['rsiMax']:.0f} and the "
            f"3-day return <= {p['ret3Max'] * 100:.0f}%; exit at the next session's close. "
            f"Grade A = RSI <= {a['rsiMax']:.0f} and 3-day return <= {a['ret3Max'] * 100:.0f}%.")
