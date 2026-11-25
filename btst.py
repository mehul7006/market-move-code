"""BTST / STBT next-session OPEN prediction for the index segments.

A separate book from everything else in the desk: the intraday sure-call engine, the confidence
calls, the short-term book and the overnight close-to-close model are not imported and not changed.
The only shared code is the plain indicator helpers in analysis.py (RSI / ATR series).

What it predicts
    The direction of the NEXT SESSION'S OPEN relative to today's close (gap up / gap down),
    for every index, every evening, with a probability, plus a "SURE" BTST call only when a
    measured high-accuracy setup fires and every gate passes.

The setup (found on 2022-2024 data, then checked on 2015-2021 and 2024-2026, which it never saw)
    Momentum close (BTST LONG):
        * today's body (close - open) >= 0.6 x ATR(14)          trend day, buyers in control
        * index-complex breadth >= +0.5                          Nifty/Bank/Fin/Sensex/Next50 all closed
                                                                 in the top quarter of their ranges
        * close >= 20-day average                                 with the trend, not a bounce
        Grade A adds: India VIX fell today (fear easing into the close).
    Why it works: NSE/BSE index closes are a 30-minute average of the last half hour, so a market
    still rising into 15:30 leaves its last traded prices above the official close and the
    pre-open auction tends to start there; strong broad trend days also carry into the next
    morning. The mirror (weak close -> gap down) is measured and gated the same way, and on
    Indian indices it historically does NOT clear the bar (indices gap up after weak days more
    often than not), so STBT "sure" calls are rare by design.

Probability model (shown for every index, call or not)
    A ridge linear-probability model on 11 close-of-day features, refitted walk-forward every 21
    sessions on a rolling window of the previous ~4 years. Every probability in the audit was
    produced by a model that had only seen earlier sessions.

No look-ahead
    Features at day i use bars <= i (ATR uses bar i-1). The outcome of day i is the open of i+1.
    Day i's sample only enters the model after the open of i+1 is known (i.e. from day i+1 on).
"""
from __future__ import annotations

import datetime
import math
import statistics

import analysis

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

PARAMS = {
    "name": "BTST momentum close",
    "atrPeriod": 14,
    "emaPeriod": 20,
    "bodyMin": 0.6,           # |close - open| >= 0.6 x ATR
    "breadthMin": 0.5,        # complex breadth (each peer's close location, -1..+1, averaged)
    "minPeers": 3,            # breadth needs at least three indices on that date
    "minSamples": 30,         # a grade needs this many past setups before it may be SURE
    "sureWinRate": 70.0,      # all-history open-direction hit rate floor for SURE
    "sureRecentWinRate": 65.0,  # ... and the most recent `recentSessions` must hold this
    "recentSessions": 500,    # ~2 years
    "recentMinSamples": 10,
    "modelAgreeLong": 0.60,   # the walk-forward probability must agree with the setup
    "modelAgreeShort": 0.40,
    "leanProb": 0.60,         # prediction is shown as a lean beyond this
    "stratMinSamples": 25,
    "stratRecent": 250,       # strategy confidence also has to hold over the last ~1 year (fast regime check)    # a strategy needs this many resolved setups on the index before it can score
    "sureConfidence": 70.0,   # strategy-book tiers: SURE >= 70% conservative confidence (and model >= modelAgreeLong)
    "highConfidence": 60.0,   # HIGH-CONFIDENCE >= 60%
    "highModelMin": 0.60,     # ... and the probability model must agree (gap-up probability >= 60%)
    "refit": 21,              # walk-forward refit cadence (sessions)
    "window": 1000,           # rolling training window (sessions)
    "warmup": 400,            # sessions before the first out-of-sample probability
    "ridge": 3.0,
}

FEATURES = ["clv", "body", "ret1", "rsi14", "dist20", "vixChg", "gapRate", "breadth",
            "gapToday", "ret5", "friday"]
FEATURE_LABELS = {
    "clv": "close location in today's range", "body": "close - open (ATR)",
    "ret1": "day return %", "rsi14": "RSI(14)", "dist20": "distance from 20-day average",
    "vixChg": "India VIX change today", "gapRate": "share of gap-ups in the last 60 sessions",
    "breadth": "index-complex breadth", "gapToday": "today's opening gap (ATR)",
    "ret5": "5-day return", "friday": "weekend ahead",
}


def ist_date(ts):
    return datetime.datetime.fromtimestamp(int(ts), IST).date()


# ------------------------------------------------------------------ data prep

def _clean(rows):
    by_day = {}
    for c in rows or []:
        try:
            d = ist_date(c["t"])
            o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(x == x for x in (o, h, l, cl)) or cl <= 0 or h < l:
            continue
        by_day[d] = {"t": int(c["t"]), "d": d, "open": o, "high": h, "low": l, "close": cl,
                     "src": c.get("src")}
    return [by_day[k] for k in sorted(by_day)]


def _close_location(bar):
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return None
    return ((bar["close"] - bar["low"]) / rng - 0.5) * 2.0


def breadth_map(peers):
    """date -> mean close location (-1..+1) over the peer indices that traded that day."""
    acc = {}
    for rows in (peers or {}).values():
        for b in _clean(rows):
            v = _close_location(b)
            if v is not None:
                acc.setdefault(b["d"], []).append(v)
    return {d: (sum(v) / len(v), len(v)) for d, v in acc.items()}


def vix_map(vix_rows):
    """date -> India VIX % change vs the previous session's close."""
    rows = _clean(vix_rows)
    out = {}
    for i in range(1, len(rows)):
        out[rows[i]["d"]] = rows[i]["close"] / rows[i - 1]["close"] - 1
    return out


def prepare(rows, vix_rows=None, peers=None, params=None):
    """Feature rows for every session that has enough history. Each row also carries its
    next-session outcome when that session exists."""
    p = params or PARAMS
    bars = _clean(rows)
    closes = [b["close"] for b in bars]
    atr = analysis.atr_series([{k: b[k] for k in ("open", "high", "low", "close")} for b in bars],
                              p["atrPeriod"])
    rsi = analysis.rsi_series(closes, 14)
    vix = vix_map(vix_rows)
    vix_level = {b["d"]: b["close"] for b in _clean(vix_rows)}
    brd = breadth_map(peers)
    out = []
    n = p["emaPeriod"]
    for i in range(max(61, n), len(bars)):
        b = bars[i]
        A = atr[i - 1]
        if A is None or A <= 0 or rsi[i] is None:
            continue
        clv = _close_location(b)
        if clv is None:
            continue
        ema = sum(closes[i - n + 1:i + 1]) / n
        gaps = [bars[j]["open"] > closes[j - 1] for j in range(i - 59, i + 1)]
        br = brd.get(b["d"])
        f = {
            "clv": clv,
            "body": (b["close"] - b["open"]) / A,
            "ret1": (closes[i] / closes[i - 1] - 1) * 100,
            "rsi14": (rsi[i] - 50) / 20,
            "dist20": (closes[i] - ema) / A / 2,
            "vixChg": vix.get(b["d"], 0.0) * 10,
            "gapRate": (sum(gaps[:-1]) / 59 - 0.5) * 4,
            "breadth": br[0] if br and br[1] >= p["minPeers"] else 0.0,
            "gapToday": (b["open"] - closes[i - 1]) / A,
            "ret5": (closes[i] / closes[i - 5] - 1) * 50,
            "friday": 1.0 if b["d"].weekday() == 4 else 0.0,
        }
        row = {
            "i": i, "date": b["d"].isoformat(), "ts": b["t"], "close": round(b["close"], 2),
            "open": b["open"], "atr": A, "f": f,
            "breadthPeers": br[1] if br else 0,
            "vixKnown": b["d"] in vix,
            "vixPct": round(vix[b["d"]] * 100, 2) if b["d"] in vix else None,
            "aboveEma": closes[i] >= ema, "ema20": round(ema, 2),
            "aboveEma50": closes[i] >= sum(closes[i - 49:i + 1]) / 50,
            "abovePrevHigh": closes[i] > bars[i - 1]["high"],
            "up3": closes[i] > closes[i - 1] > closes[i - 2],
            "vixLevel": vix_level.get(b["d"]),
            "outcome": None,
        }
        if i + 1 < len(bars):
            nx = bars[i + 1]
            gap = nx["open"] - b["close"]
            row["outcome"] = {
                "nextDate": nx["d"].isoformat(), "nextOpen": round(nx["open"], 2),
                "gapPts": round(gap, 2), "gapPct": round(gap / b["close"] * 100, 3),
                "up": gap > 0,
                "nextHigh": round(nx["high"], 2), "nextLow": round(nx["low"], 2),
                "nextClose": round(nx["close"], 2),
            }
        out.append(row)
    return out


# ------------------------------------------------------------------ setup

def setup_at(row, params=None):
    """The momentum-close setup on one feature row, or None. Uses only that row's features."""
    p = params or PARAMS
    f = row["f"]
    if row["breadthPeers"] < p["minPeers"]:
        return None
    if f["body"] >= p["bodyMin"] and f["breadth"] >= p["breadthMin"] and row["aboveEma"]:
        side = "LONG"
        grade_a = row["vixKnown"] and f["vixChg"] < 0
    elif f["body"] <= -p["bodyMin"] and f["breadth"] <= -p["breadthMin"] and not row["aboveEma"]:
        side = "SHORT"
        grade_a = row["vixKnown"] and f["vixChg"] > 0
    else:
        return None
    return {"side": side, "grade": "A" if grade_a else "B"}


def setup_reasons(row, params=None):
    """Plain-language list of which setup conditions pass / fail for the newest row."""
    p = params or PARAMS
    f = row["f"]
    up = [
        (f["body"] >= p["bodyMin"], f"body {f['body']:+.2f}×ATR (needs ≥ +{p['bodyMin']})"),
        (f["breadth"] >= p["breadthMin"], f"breadth {f['breadth']:+.2f} (needs ≥ +{p['breadthMin']})"),
        (row["aboveEma"], f"close {'above' if row['aboveEma'] else 'below'} the 20-day average"),
    ]
    dn = [
        (f["body"] <= -p["bodyMin"], f"body {f['body']:+.2f}×ATR (needs ≤ −{p['bodyMin']})"),
        (f["breadth"] <= -p["breadthMin"], f"breadth {f['breadth']:+.2f} (needs ≤ −{p['breadthMin']})"),
        (not row["aboveEma"], f"close {'below' if not row['aboveEma'] else 'above'} the 20-day average"),
    ]
    return {"long": [{"ok": ok, "text": t} for ok, t in up],
            "short": [{"ok": ok, "text": t} for ok, t in dn]}


# ------------------------------------------------------------------ walk-forward model

def _solve(A, b):
    n = len(b)
    M = [A[i][:] + [b[i]] for i in range(n)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[piv] = M[piv], M[i]
        if abs(M[i][i]) < 1e-12:
            return None
        for r in range(n):
            if r != i and M[r][i]:
                fct = M[r][i] / M[i][i]
                for c in range(i, n + 1):
                    M[r][c] -= fct * M[i][c]
    return [M[i][n] / M[i][i] for i in range(n)]


def _vec(row):
    return [row["f"][k] for k in FEATURES] + [1.0]


class _Ridge:
    """Rolling-window ridge regression kept as running sums, so a walk-forward refit is a
    small solve instead of a pass over the whole history."""

    def __init__(self, d, lam):
        self.d, self.lam = d, lam
        self.A = [[0.0] * d for _ in range(d)]
        self.b = [0.0] * d
        self.n = 0
        self.w = None

    def _upd(self, z, y, s):
        for a in range(self.d):
            za = z[a] * s
            self.b[a] += za * y
            Aa = self.A[a]
            for c in range(self.d):
                Aa[c] += za * z[c]
        self.n += s

    def add(self, z, y):
        self._upd(z, y, 1)

    def remove(self, z, y):
        self._upd(z, y, -1)

    def fit(self):
        A = [r[:] for r in self.A]
        for a in range(self.d - 1):        # intercept is not penalised
            A[a][a] += self.lam
        A[-1][-1] += 1e-9
        w = _solve(A, self.b)
        if w:
            self.w = w
        return self.w

    def prob(self, z):
        if not self.w:
            return None
        return min(0.98, max(0.02, sum(a * c for a, c in zip(self.w, z))))


def walk_forward(rows, params=None):
    """Out-of-sample probability for every row after warm-up, plus a model fitted on all known
    outcomes for the newest row. Row k's sample enters training only from row k+1 on (its
    outcome is the open of the session after k, known before row k+1 closes)."""
    p = params or PARAMS
    d = len(FEATURES) + 1
    rg = _Ridge(d, p["ridge"])
    in_window = []
    last_fit = None
    probs = [None] * len(rows)
    for k, row in enumerate(rows):
        if k >= 1 and rows[k - 1]["outcome"] is not None:
            prev = rows[k - 1]
            z = _vec(prev)
            y = 1.0 if prev["outcome"]["up"] else 0.0
            rg.add(z, y)
            in_window.append((z, y))
            if len(in_window) > p["window"]:
                oz, oy = in_window.pop(0)
                rg.remove(oz, oy)
        if rg.n >= p["warmup"] and (last_fit is None or k - last_fit >= p["refit"]):
            rg.fit()
            last_fit = k
        if rg.w is not None:
            probs[k] = rg.prob(_vec(row))
    weights = None
    if rg.w:
        weights = {name: round(w, 4) for name, w in zip(FEATURES + ["intercept"], rg.w)}
    # the newest row: refit on everything known right now so the live probability is current
    live_prob = None
    if rows and rg.n >= p["warmup"]:
        rg.fit()
        live_prob = rg.prob(_vec(rows[-1]))
    return probs, live_prob, weights


# ------------------------------------------------------------------ audit

BUCKETS = [(0.0, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.01)]


def _rate(xs):
    return round(sum(xs) / len(xs) * 100, 1) if xs else None


def tier_stats(trades):
    n = len(trades)
    if not n:
        return {"samples": 0, "wins": 0, "winRate": None, "avgPts": None, "avgPct": None,
                "avgWinPts": None, "avgLossPts": None, "worstPts": None, "bestPts": None,
                "profitFactor": None, "touchRate": None}
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    gw = sum(t["pts"] for t in wins)
    gl = -sum(t["pts"] for t in losses)
    return {
        "samples": n, "wins": len(wins), "winRate": round(len(wins) / n * 100, 1),
        "avgPts": round(sum(t["pts"] for t in trades) / n, 1),
        "avgPct": round(sum(t["pct"] for t in trades) / n, 3),
        "avgWinPts": round(gw / len(wins), 1) if wins else None,
        "avgLossPts": round(-gl / len(losses), 1) if losses else None,
        "worstPts": round(min(t["pts"] for t in trades), 1),
        "bestPts": round(max(t["pts"] for t in trades), 1),
        "profitFactor": round(gw / gl, 2) if gl > 0 else None,
        "touchRate": _rate([t["touch"] for t in trades]),
    }


def _trade(row, setup, prob):
    o = row["outcome"]
    sgn = 1 if setup["side"] == "LONG" else -1
    pts = o["gapPts"] * sgn
    # "touch": the next session traded beyond the entry in the trade's favour at some point
    touch = (o["nextHigh"] > row["close"]) if sgn > 0 else (o["nextLow"] < row["close"])
    return {
        "date": row["date"], "nextDate": o["nextDate"], "ts": row["ts"],
        "side": setup["side"], "grade": setup["grade"],
        "entry": row["close"], "nextOpen": o["nextOpen"],
        "pts": round(pts, 2), "pct": round(o["gapPct"] * sgn, 3), "win": pts > 0,
        "touch": touch, "prob": None if prob is None else round(prob, 3),
        "body": round(row["f"]["body"], 2), "breadth": round(row["f"]["breadth"], 2),
        "vixPct": row["vixPct"],
    }


def audit(rows, probs, params=None):
    """Every past setup with its next-open outcome, per side/grade, plus the model's
    out-of-sample calibration. Only rows with a known outcome are counted."""
    p = params or PARAMS
    done = [k for k, r in enumerate(rows) if r["outcome"] is not None]
    trades = []
    for k in done:
        s = setup_at(rows[k], p)
        if s:
            trades.append(_trade(rows[k], s, probs[k]))
    recent_cut = rows[done[-1]]["ts"] if done else 0
    if len(done) > p["recentSessions"]:
        recent_cut = rows[done[-p["recentSessions"]]]["ts"]
    tiers = {}
    for side in ("LONG", "SHORT"):
        for grade in ("A", "B"):
            tt = [t for t in trades if t["side"] == side and t["grade"] == grade]
            st = tier_stats(tt)
            st["recent"] = tier_stats([t for t in tt if t["ts"] >= recent_cut])
            years = {}
            for t in tt:
                years.setdefault(t["date"][:4], []).append(t["win"])
            st["years"] = {y: {"n": len(v), "winRate": _rate(v)} for y, v in sorted(years.items())}
            tiers[f"{side}_{grade}"] = st
        both = [t for t in trades if t["side"] == side]
        tiers[side] = tier_stats(both)
        tiers[side]["recent"] = tier_stats([t for t in both if t["ts"] >= recent_cut])
    # model calibration (out-of-sample only)
    oos = [(probs[k], rows[k]["outcome"]) for k in done if probs[k] is not None]

    def _calib(pairs):
        out = []
        for lo, hi in BUCKETS:
            sel = [o for pr, o in pairs if lo <= pr < hi]
            out.append({"lo": lo, "hi": min(hi, 1.0), "n": len(sel),
                        "upRate": _rate([o["up"] for o in sel]),
                        "avgGapPts": round(sum(o["gapPts"] for o in sel) / len(sel), 1)
                        if sel else None})
        return out

    calib = _calib(oos)
    calib_recent = _calib(oos[-p["recentSessions"]:])
    lean_l = [o for pr, o in oos if pr >= p["leanProb"]]
    lean_s = [o for pr, o in oos if pr <= 1 - p["leanProb"]]
    model = {
        "samples": len(oos),
        "from": rows[next(k for k in done if probs[k] is not None)]["date"] if oos else None,
        "directionHit": _rate([(pr >= 0.5) == o["up"] for pr, o in oos]),
        "baseUpRate": _rate([o["up"] for _, o in oos]),
        "leanUp": {"n": len(lean_l), "hit": _rate([o["up"] for o in lean_l])},
        "leanDown": {"n": len(lean_s), "hit": _rate([not o["up"] for o in lean_s])},
        "calibration": calib,
        "calibrationRecent": calib_recent,
    }
    gaps = [rows[k]["outcome"]["gapPts"] for k in done[-60:]]
    return {
        "trades": trades,
        "tiers": tiers,
        "model": model,
        "sessions": len(done),
        "from": rows[done[0]]["date"] if done else None,
        "to": rows[done[-1]]["date"] if done else None,
        "baseUpRate": _rate([rows[k]["outcome"]["up"] for k in done]),
        "recentBaseUpRate": _rate([rows[k]["outcome"]["up"] for k in done[-p["recentSessions"]:]]),
        "medianAbsGap60": round(statistics.median(abs(g) for g in gaps), 1) if gaps else None,
        "avgGap60": round(sum(gaps) / len(gaps), 1) if gaps else None,
    }


def _bucket_for(prob, calib):
    for b in calib:
        if b["lo"] <= prob < b["hi"] or (prob >= 1.0 and b["hi"] >= 1.0):
            return b
    return None


def gate(setup, tier, prob, params=None):
    """(sure, reasons). Every gate must pass for a SURE call."""
    p = params or PARAMS
    reasons = []
    n = tier.get("samples") or 0
    wr = tier.get("winRate")
    rec = tier.get("recent") or {}
    if n < p["minSamples"]:
        reasons.append(f"only {n} past Grade {setup['grade']} {setup['side']} setups on this index — "
                       f"needs {p['minSamples']}")
    elif (wr or 0) < p["sureWinRate"]:
        reasons.append(f"Grade {setup['grade']} {setup['side']} measured {wr}% — below the "
                       f"{p['sureWinRate']:.0f}% floor")
    if (rec.get("samples") or 0) < p["recentMinSamples"]:
        reasons.append(f"only {rec.get('samples') or 0} setups in the last ~2 years — needs "
                       f"{p['recentMinSamples']}")
    elif (rec.get("winRate") or 0) < p["sureRecentWinRate"]:
        reasons.append(f"last ~2 years {rec.get('winRate')}% — below the "
                       f"{p['sureRecentWinRate']:.0f}% recent floor")
    if prob is None:
        reasons.append("probability model not available")
    elif setup["side"] == "LONG" and prob < p["modelAgreeLong"]:
        reasons.append(f"model gives a gap-up only {prob * 100:.0f}% — needs "
                       f"≥ {p['modelAgreeLong'] * 100:.0f}% to agree")
    elif setup["side"] == "SHORT" and prob > p["modelAgreeShort"]:
        reasons.append(f"model gives a gap-down only {(1 - prob) * 100:.0f}% — needs "
                       f"≥ {(1 - p['modelAgreeShort']) * 100:.0f}% to agree")
    return not reasons, reasons


def sure_ledger(rows, probs, params=None):
    """Causal replay of the SURE gate: on each past session the tier statistics are rebuilt
    from setups whose outcome was already known that evening, exactly as the live gate would
    have seen them. Returns every session that would have printed SURE, with its outcome."""
    p = params or PARAMS
    known = {}          # tier -> list of (row index, win)
    pending = []        # (row index, tier key, win) waiting for their outcome to be known
    out = []
    for k, row in enumerate(rows):
        # the setup at k-1 resolves at the open of session k: known before k closes
        still = []
        for (j, key, win) in pending:
            if j <= k - 1:
                known.setdefault(key, []).append((j, win))
            else:
                still.append((j, key, win))
        pending = still
        s = setup_at(row, p)
        if not s:
            continue
        key = f"{s['side']}_{s['grade']}"
        hist = known.get(key, [])
        rec = [w for j, w in hist if j >= k - p["recentSessions"]]
        tier = {"samples": len(hist), "winRate": _rate([w for _, w in hist]),
                "recent": {"samples": len(rec), "winRate": _rate(rec)}}
        ok, _ = gate(s, tier, probs[k], p)
        if row["outcome"] is not None:
            t = _trade(row, s, probs[k])
            pending.append((k, key, t["win"]))
            if ok:
                out.append(t)
    return out


# ------------------------------------------------------------------ strategy book (BTST, long only)
# Six gap-up strategies, each grounded in a known overnight effect and kept only because it held in every
# era 2015-2018 / 2019-2021 / 2022-2024 / 2025-2026 on the traded indices. Thresholds are fixed round
# numbers, not optimised. Six STBT (gap-down) candidates were tested the same way - weak close, breakdown
# with VIX up, overbought fade, shooting star, VIX spike, trend collapse - and all lost (30-48% on Nifty,
# Sensex, Bank Nifty, Fin Nifty): Indian indices gap UP after weak days more often than not, so the book
# has no short strategy rather than a short strategy that loses.

def _peers_ok(r, p):
    return r["breadthPeers"] >= p["minPeers"]


def _vix_down(r):
    return r["vixKnown"] and r["f"]["vixChg"] < 0


STRATEGIES = [
    {"key": "MOMENTUM_VIX", "name": "Momentum close + VIX easing",
     "text": "close − open ≥ 0.6×ATR, complex breadth ≥ +0.5, close above the 20-day average, India VIX lower",
     "fn": lambda r, p: _peers_ok(r, p) and r["f"]["body"] >= 0.6 and r["f"]["breadth"] >= 0.5
     and r["aboveEma"] and _vix_down(r)},
    {"key": "GAP_RECOVERY", "name": "Gap-down recovered",
     "text": "opened ≥ 0.3×ATR below yesterday's close, then closed ≥ 0.4×ATR above its open in the top 30% of the range",
     "fn": lambda r, p: r["f"]["gapToday"] <= -0.3 and r["f"]["body"] >= 0.4 and r["f"]["clv"] >= 0.4},
    {"key": "STRONG_CLOSE_VIX", "name": "Strong close + breadth + VIX easing",
     "text": "close in the top 20% of the range, complex breadth ≥ +0.6, day return > +0.3%, India VIX lower",
     "fn": lambda r, p: _peers_ok(r, p) and r["f"]["clv"] >= 0.6 and r["f"]["breadth"] >= 0.6
     and r["f"]["ret1"] > 0.3 and _vix_down(r)},
    {"key": "BREAKOUT", "name": "Breakout close",
     "text": "close above yesterday's high, in the top 25% of the range, breadth ≥ +0.4, above the 50-day average",
     "fn": lambda r, p: _peers_ok(r, p) and r["abovePrevHigh"] and r["f"]["clv"] >= 0.5
     and r["f"]["breadth"] >= 0.4 and r["aboveEma50"]},
    {"key": "TREND3", "name": "3-day trend + strong close",
     "text": "three rising closes in a row, close in the top 25% of the range, breadth ≥ +0.5",
     "fn": lambda r, p: _peers_ok(r, p) and r["up3"] and r["f"]["clv"] >= 0.5 and r["f"]["breadth"] >= 0.5},
    {"key": "HIGH_VIX_BOUNCE", "name": "High-VIX strong close",
     "text": "India VIX ≥ 18, close in the top 25% of the range, day return positive",
     "fn": lambda r, p: (r["vixLevel"] or 0) >= 18 and r["f"]["clv"] >= 0.5 and r["f"]["ret1"] > 0},
]
CONFLUENCE = {"key": "CONFLUENCE", "name": "Confluence (3+ strategies agree)",
              "text": "at least three of the six strategies fire on the same close"}
STRATEGY_BY_KEY = {s["key"]: s for s in STRATEGIES + [CONFLUENCE]}


def fired_strategies(row, params=None):
    p = params or PARAMS
    keys = [s["key"] for s in STRATEGIES if s["fn"](row, p)]
    if len(keys) >= 3:
        keys.append("CONFLUENCE")
    return keys


def shrunk_rate(wins, n, prior=10):
    """Win rate pulled toward 50% by `prior` imaginary coin-flip trades: a small sample cannot look sure."""
    return (wins + prior * 0.5) / (n + prior) * 100 if n + prior else 50.0


def strategy_confidence(hist, k, params=None):
    """Conservative confidence for one strategy at session k from its resolved history [(j, win)]:
    the lower of the shrunk all-history and shrunk last-~2-years win rates. None when too few samples."""
    p = params or PARAMS
    n = len(hist)
    if n < p["stratMinSamples"]:
        return None
    w = sum(1 for _, x in hist if x)
    rec = [x for j, x in hist if j >= k - p["recentSessions"]]
    yr = [x for j, x in hist if j >= k - p["stratRecent"]]
    return round(min(shrunk_rate(w, n), shrunk_rate(sum(rec), len(rec)), shrunk_rate(sum(yr), len(yr))), 1)


def call_tier(conf, prob, params=None):
    p = params or PARAMS
    if conf is None or prob is None:
        return None
    if conf >= p["sureConfidence"] and prob >= p["modelAgreeLong"]:
        return "SURE"
    if conf >= p["highConfidence"] and prob >= p["highModelMin"]:
        return "HIGH"
    return None


def strategy_book(rows, probs, params=None):
    """Causal replay of the whole strategy book on one index.

    For every session the confidence of each firing strategy is computed only from that strategy's setups
    whose next open was already known that evening. Returns (calls, per-strategy history, live evaluation)."""
    p = params or PARAMS
    known = {}          # strategy -> [(row index, win)]
    pending = []        # (row index, keys, win)
    calls = []
    live = None
    for k, row in enumerate(rows):
        still = []
        for (j, keys, win) in pending:
            if j <= k - 1:
                for key in keys:
                    known.setdefault(key, []).append((j, win))
            else:
                still.append((j, keys, win))
        pending = still
        keys = fired_strategies(row, p)
        evals = []
        for key in keys:
            hist = known.get(key, [])
            conf = strategy_confidence(hist, k, p)
            rec = [x for j, x in hist if j >= k - p["recentSessions"]]
            evals.append({"key": key, "name": STRATEGY_BY_KEY[key]["name"], "confidence": conf,
                          "samples": len(hist), "winRate": _rate([x for _, x in hist]),
                          "recentSamples": len(rec), "recentWinRate": _rate(rec)})
        scored = [e for e in evals if e["confidence"] is not None]
        best = max(scored, key=lambda e: e["confidence"]) if scored else None
        conf = best["confidence"] if best else None
        tier = call_tier(conf, probs[k], p) if keys else None
        if k == len(rows) - 1:
            live = {"strategies": evals, "best": best, "confidence": conf, "tier": tier,
                    "count": len([x for x in keys if x != "CONFLUENCE"])}
        if row["outcome"] is not None and keys:
            o = row["outcome"]
            win = o["gapPts"] > 0
            pending.append((k, keys, win))
            if tier:
                calls.append({
                    "date": row["date"], "nextDate": o["nextDate"], "ts": row["ts"], "side": "LONG",
                    "tier": tier, "confidence": conf, "strategy": best["key"], "strategyName": best["name"],
                    "strategies": keys, "entry": row["close"], "nextOpen": o["nextOpen"],
                    "pts": round(o["gapPts"], 2), "pct": round(o["gapPct"], 3), "win": win,
                    "touch": o["nextHigh"] > row["close"],
                    "prob": None if probs[k] is None else round(probs[k], 3),
                    "grade": "S" if tier == "SURE" else "H",
                    "body": round(row["f"]["body"], 2), "breadth": round(row["f"]["breadth"], 2),
                    "vixPct": row["vixPct"],
                })
    # per-strategy record (every resolved setup, causal or not, is the same set of outcomes)
    rec_cut = len(rows) - 1 - p["recentSessions"]
    table = []
    for s in STRATEGIES + [CONFLUENCE]:
        hist = known.get(s["key"], [])
        yrs = {}
        for j, x in hist:
            yrs.setdefault(rows[j]["date"][:4], []).append(x)
        rec = [x for j, x in hist if j >= rec_cut]
        table.append({"key": s["key"], "name": s["name"], "text": s["text"], "samples": len(hist),
                      "winRate": _rate([x for _, x in hist]), "recentSamples": len(rec),
                      "recentWinRate": _rate(rec),
                      "years": {y: {"n": len(v), "winRate": _rate(v)} for y, v in sorted(yrs.items())}})
    return calls, table, live


def analyse(rows, vix_rows=None, peers=None, params=None):
    """Full per-index result: audit, model, and the live prediction for the newest bar."""
    p = params or PARAMS
    feat = prepare(rows, vix_rows, peers, p)
    if len(feat) < p["warmup"] + 50:
        return {"ok": False, "reason": f"only {len(feat)} usable daily sessions — need "
                                        f"{p['warmup'] + 50} to train and audit"}
    probs, live_prob, weights = walk_forward(feat, p)
    au = audit(feat, probs, p)
    ledger = sure_ledger(feat, probs, p)
    recent_cut = feat[-p["recentSessions"]]["ts"] if len(feat) > p["recentSessions"] else 0
    last = feat[-1]
    setup = setup_at(last, p)
    prob = live_prob
    bucket = None
    calib_src = None
    if prob is not None:
        rb = _bucket_for(prob, au["model"]["calibrationRecent"])
        fb = _bucket_for(prob, au["model"]["calibration"])
        if rb and rb["n"] >= 25:
            bucket, calib_src = rb, "last ~2 years"
        elif fb and fb["n"] >= 25:
            bucket, calib_src = fb, "full history"
    measured_up = bucket["upRate"] if bucket else None
    exp_gap = bucket["avgGapPts"] if bucket else au["avgGap60"]
    zone = au["medianAbsGap60"]
    sure, why = (False, [])
    tier = None
    if setup:
        tier = au["tiers"][f"{setup['side']}_{setup['grade']}"]
        sure, why = gate(setup, tier, prob, p)
    # the lean is judged on what this score bucket actually did out of sample, not the raw score
    if prob is None:
        lean, direction = "NO MODEL", None
    elif measured_up is None:
        # the score sits in a range the model has rarely produced: there is nothing measured to lean on
        lean, direction = "NO MEASURED EDGE (rare score)", None
    elif measured_up >= p["leanProb"] * 100:
        lean, direction = "GAP-UP LEAN", "UP"
    elif measured_up <= (1 - p["leanProb"]) * 100:
        lean, direction = "GAP-DOWN LEAN", "DOWN"
    else:
        lean, direction = "NO EDGE (≈ coin flip)", ("UP" if measured_up >= 50 else "DOWN")
    f = last["f"]
    pred = {
        "date": last["date"], "close": last["close"], "atr": round(last["atr"], 2),
        "probUp": None if prob is None else round(prob, 3),
        "direction": direction, "lean": lean,
        "measuredUpRate": measured_up, "bucketN": bucket["n"] if bucket else 0,
        "calibratedOn": calib_src,
        "expectedGapPts": exp_gap,
        "predictedOpen": round(last["close"] + (exp_gap or 0), 2),
        "zoneLow": round(last["close"] + (exp_gap or 0) - (zone or 0), 2),
        "zoneHigh": round(last["close"] + (exp_gap or 0) + (zone or 0), 2),
        "zonePts": zone,
        "features": {k: round(v, 3) for k, v in f.items()},
        "body": round(f["body"], 2), "breadth": round(f["breadth"], 2),
        "breadthPeers": last["breadthPeers"], "vixPct": last["vixPct"],
        "aboveEma": last["aboveEma"], "ema20": last["ema20"],
        "conditions": setup_reasons(last, p),
    }
    plan = None
    if setup:
        sgn = 1 if setup["side"] == "LONG" else -1
        avg_win = tier.get("avgWinPts") or exp_gap or 0
        worst = tier.get("worstPts")
        plan = {
            "side": setup["side"], "grade": setup["grade"],
            "action": "BUY at close (BTST)" if sgn > 0 else "SELL at close (STBT)",
            "entry": last["close"], "entryDate": last["date"],
            "exit": "sell at the next session's open (09:15-09:20 IST)" if sgn > 0
                    else "buy back at the next session's open (09:15-09:20 IST)",
            "target": round(last["close"] + sgn * abs(avg_win), 2),
            "targetPts": round(abs(avg_win), 1),
            "worstPts": worst,
            "riskGuide": round(last["close"] - sgn * abs(worst or last["atr"]), 2),
            "measuredWinRate": tier.get("winRate"), "measuredSamples": tier.get("samples"),
            "recentWinRate": (tier.get("recent") or {}).get("winRate"),
            "recentSamples": (tier.get("recent") or {}).get("samples"),
            "avgPts": tier.get("avgPts"), "profitFactor": tier.get("profitFactor"),
        }
    # strategy book: the BTST calls (SURE >= 70% / HIGH >= 60% conservative confidence, model agreeing)
    bcalls, btable, blive = strategy_book(feat, probs, p)
    cut_2y = feat[-p["recentSessions"]]["ts"] if len(feat) > p["recentSessions"] else 0
    cut_1y = feat[-p["stratRecent"]]["ts"] if len(feat) > p["stratRecent"] else 0

    def _stats(xs):
        return dict(tier_stats(xs), recent=tier_stats([t for t in xs if t["ts"] >= cut_2y]),
                    lastYear=tier_stats([t for t in xs if t["ts"] >= cut_1y]))

    book_stats = {"ALL": _stats(bcalls),
                  "SURE": _stats([t for t in bcalls if t["tier"] == "SURE"]),
                  "HIGH": _stats([t for t in bcalls if t["tier"] == "HIGH"])}
    fired = {e["key"]: e for e in (blive or {}).get("strategies") or []}
    book_plan = None
    if blive and blive["tier"]:
        mine = book_stats[blive["tier"]]
        avg_win = mine.get("avgWinPts") or 0
        book_plan = {
            "side": "LONG", "tier": blive["tier"], "confidence": blive["confidence"],
            "strategy": blive["best"]["name"], "strategyKey": blive["best"]["key"],
            "action": "BUY at close (BTST)",
            "entry": last["close"], "entryDate": last["date"],
            "exit": "sell at the next session's open (09:15-09:20 IST)",
            "target": round(last["close"] + abs(avg_win), 2), "targetPts": round(abs(avg_win), 1),
            "worstPts": mine.get("worstPts"),
            "measuredWinRate": mine.get("winRate"), "measuredSamples": mine.get("samples"),
            "recentWinRate": mine["recent"].get("winRate"), "recentSamples": mine["recent"].get("samples"),
            "probUp": None if prob is None else round(prob, 3),
        }
    book = {
        "tier": (blive or {}).get("tier"),
        "confidence": (blive or {}).get("confidence"),
        "best": (blive or {}).get("best"),
        "firedCount": (blive or {}).get("count", 0),
        "strategies": [dict(fired.get(t["key"], {}), key=t["key"], name=t["name"], text=t["text"],
                            fired=t["key"] in fired, allWinRate=t["winRate"], allSamples=t["samples"],
                            recentWinRateAll=t["recentWinRate"], recentSamplesAll=t["recentSamples"])
                       for t in btable],
        "plan": book_plan,
        "stats": book_stats,
        "calls": bcalls,
        "table": btable,
    }
    return {
        "ok": True,
        "book": book,
        "prediction": pred,
        "setup": setup,
        "sure": sure,
        "blockReasons": why,
        "plan": plan,
        "audit": {k: v for k, v in au.items() if k != "trades"},
        "trades": au["trades"],
        "sureLedger": ledger,
        "sureStats": dict(tier_stats(ledger),
                          recent=tier_stats([t for t in ledger if t["ts"] >= recent_cut])),
        "recentPredictions": [
            {"date": r["date"], "probUp": None if probs[k] is None else round(probs[k], 3),
             "predicted": None if probs[k] is None else ("UP" if probs[k] >= 0.5 else "DOWN"),
             "gapPts": r["outcome"]["gapPts"] if r["outcome"] else None,
             "actual": None if not r["outcome"] else ("UP" if r["outcome"]["up"] else "DOWN"),
             "hit": None if (probs[k] is None or not r["outcome"])
             else ((probs[k] >= 0.5) == r["outcome"]["up"]),
             "setup": (lambda s: f"{s['side']} {s['grade']}" if s else None)(setup_at(r, p))}
            for k, r in list(enumerate(feat))[-15:]
        ],
        "weights": weights,
    }


def rule_text(params=None):
    p = params or PARAMS
    return (f"BTST LONG when today's close - open ≥ {p['bodyMin']}×ATR(14), index-complex breadth "
            f"≥ +{p['breadthMin']} (Nifty, Bank Nifty, Fin Nifty, Sensex, Next 50 closing in the top "
            f"quarter of their ranges) and the close is above its 20-day average; Grade A also needs "
            f"India VIX lower on the day. STBT SHORT is the exact mirror (Grade A: VIX higher). "
            f"Entry: today's close. Exit: next session's open.")
