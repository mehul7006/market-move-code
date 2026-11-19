"""Short-term call engine - a separate, tested strategy per index AND per candle size (1m / 5m / 15m / 30m).

Completely separate from the long-term precision profiles in analysis.py / server.py: it shares only
the plain indicator helpers (EMA / RSI / ATR) and never changes them. Real Upstox candles only.

Strategies (all decided on COMPLETED candles only - no lookahead):
  vwapx    "VWAP reclaim in a trend" - the candle that takes price back through the session VWAP while the
           EMAs already point the same way. It fires at the START of the leg, not after it has run.
           long : previous close <= VWAP, this close > VWAP, RSI >= 50, trend filter true  (short mirrors)
  pullback "Trend pullback resume" - previous candle tagged EMA9, this candle closes beyond the previous
           candle's extreme and EMA9 with RSI 45-65 (short 35-55), trend filter true.
  Trend filter: "strong" = EMA9 > EMA21 > EMA50 and price on the right side of VWAP; "trend" = EMA21 vs EMA50.

A (segment, candle size) pair only issues calls if its strategy held >= 70% wins on history chosen BEFORE the
last 60 days AND in the held-back last 60 days. Everything else shows a live read only (no call):
  * Fin Nifty, Midcap Select - no reliable edge on any candle size.
  * 30m - no strategy reached 70% on any index.
Candlestick patterns are recognised and displayed, but requiring them did NOT raise the hit rate in testing
(1m 63% vs 63%, 5m 65% vs 65%), so they are information, not a filter. Index candles carry no volume; futures
volume/OI is shown as live context only - the liquid history (~30 calls) was too short to validate it.

Outcome - walk forward on later candles; a candle touching a target and the stop counts as the stop.
          Each call lands in exactly ONE bucket: SL, T1, T2, T3 or TIMEOUT/OPEN, where a target bucket
          is the highest target reached before the stop (a later stop never removes it).
Plan    - book 1/3 at each of T1, T2, T3 and move the stop to entry once T1 is hit (also reported:
          exiting everything at T1).
"""
from __future__ import annotations

import collections
import datetime
import math
import statistics
import time

import bisect

import analysis

TIMEFRAMES = (1, 5, 15, 30)
SESSION_CLOSE_MIN = 15 * 60 + 30   # minute the last bar closes; a test may lower it to 15:15 to ignore the exchange's frozen tail
SEGMENTS = {"NIFTY": "Nifty 50", "BANKNIFTY": "Bank Nifty", "SENSEX": "Sensex"}   # short term: these three only
NO_EDGE = {"FINNIFTY": "Fin Nifty", "MIDCPNIFTY": "Midcap Select"}
_COMMON = {"firstEntry": 9 * 60 + 25, "lastEntry": 14 * 60 + 15, "maxPerDay": 3}


def _p(seg, tf, family, bias, t1, t2, t3, sl, horizon_min, **kw):
    d = dict(_COMMON, seg=seg, tf=tf, family=family, bias=bias, t1=t1, t2=t2, t3=t3, sl=sl,
             horizon=max(1, horizon_min // tf), horizonMin=horizon_min,
             name=f"{SEGMENTS[seg]} {tf}m short-term")
    d.update(kw)
    return d


# ONE position, no partial booking and no extra lots. How it is managed once the call is open changes the PROFIT, never the
# win rate (a win is still "T1 reached before the stop"). Backtested on 1,485 real calls (older history, then the last 60 days):
#   runner   stop -> entry once T1 is touched, then hold until the window ends (or the stop). The best single-lot rule: average
#            profit per call 0.227 R older / 0.44 R last 60 days, versus 0.118 / 0.206 for the old 1/3-at-each-target plan. About
#            24% of calls are full stop-outs and about 6 in 10 end near zero; a few big trend moves pay for the rest.
#   protect  stop -> entry + 0.25xT1 once price is +0.5xT1. Fewer full stop-outs (about 17%) but less robust: under a strict
#            same-minute assumption its profit disappears.
#   target   stop -> entry after T1, take profit at T3. A fixed-target bracket: smaller profit (0.155 R / 0.278 R).
# Everything else I tried did NOT help: entry filters (higher-timeframe, volatility, trend strength, VWAP distance, candle shape,
# time of day), wider or tighter stops, breakout / opening-range / previous-day-level signals, pullback and confirmation entries.
MANAGE = {
    "runner": {"be_trig": 1.0, "be_off": 0.0, "tp": None},
    "protect": {"be_trig": 0.5, "be_off": 0.25, "tp": None},
    "target": {"be_trig": 1.0, "be_off": 0.0, "tp": "t3"},
}
EXIT_STYLES = tuple(MANAGE)
# T1 SIZE. The shipped ladders keep T1 at roughly 1/4 - 1/2 of the stop, which is what holds 70%+ wins. Making T1 as big as the
# stop doubles what a winning call pays but cannot keep the win rate: on 1,485 real calls the win rate (T1 before the stop) went
# 70.8% -> 64.0% -> 57.5% as T1/stop went 0.5 -> 0.75 -> 1.0, and no entry filter or new signal type restored it (best filters +2-5
# points; breakout / opening-range / previous-day signals win 45-51% at T1 = stop). So the bigger T1 sizes are offered as a CHOICE,
# computed on the same calls with the single-lot Runner rule, never as the default.
LADDER_RATIOS = (0.75, 1.0)
# TARGET profile (the page default): T1 = SL = X points, T2 = 2X, T3 = 3X. X is set so that a WINNING call books a meaningful amount:
# Nifty 25, Sensex 75, Bank Nifty 75, Next 50 80. Tested and rejected: a 45-point stop for Sensex / Bank Nifty (earns clearly less on the two-signal
# call set: Sensex 1m +14 vs +23, 5m +5 vs +15 per call), and requiring 1-3 other strategies to agree (win rate did not rise, calls fell). Measured on real calls this gives an average win of about +24 to +30
# on Nifty and +69 to +108 on Sensex, at the price of a lower win rate (about 55-63%, not 70%+) and about 40% stop-outs.
# "eq" = T1 and the stop BOTH equal to `eqx` points (own stop, own T1), tuned per index and candle size from OLDER history only
# (win rate >= 55%, best average points), then checked on the unseen last 60 days: pooled 61.1% wins over 429 calls. No size,
# filter, new signal type, volume/OI feature or machine-learning gate reached 65-70% wins at T1 = SL (see README), so this is
# offered as a choice with its real win rate beside it, never as the default.
EXIT_LABELS = {"runner": "Runner (recommended)", "protect": "Protect early", "target": "Fixed T3 target"}

# (segment, candle minutes) -> strategy. STRICT mode: calls are chosen across ALL candle sizes and six signal families by the per-index
# score model in strict_model.json (see strict.py); a (segment, candle) entry here only carries that candle size's levels and window.
# T1 = stop = X points (strict_model.json: Nifty 25, Bank Nifty 60, Sensex 75), T2 = 2X, T3 = 3X.
import strict as _strict

_X_DEFAULT = {"NIFTY": (22.5, 25), "BANKNIFTY": (63, 70), "SENSEX": (81, 90)}     # (T1, stop) in points, from strict_model.json; T1 >= 90% of the stop
TARGET_X = {seg: float((_strict.model().get(seg) or {}).get("T1") or x[0]) for seg, x in _X_DEFAULT.items()}         # T1 distance
TARGET_SL = {seg: float((_strict.model().get(seg) or {}).get("stop") or x[1]) for seg, x in _X_DEFAULT.items()}      # stop distance
STRATEGIES = {
    (seg, tf): _p(seg, tf, "strict", "trend", TARGET_X[seg], 2 * TARGET_X[seg], 3 * TARGET_X[seg], TARGET_SL[seg], _strict.HORIZON_MIN[tf], strict=True,
                  confirm=(_strict.RULES.get(seg) or {}).get("confirm"))
    for seg in _X_DEFAULT for tf in _strict.TFS
}
assert all(TARGET_X[k] >= 0.9 * TARGET_SL[k] - 1e-9 for k in TARGET_X), "short-term T1 must be at least 90% of the stop"
_PENDING = {}   # (segment, candle minutes) -> calls waiting for their confirmation move right now
_PICKS = {}   # (segment, candle minutes) -> {candle open time: (side, score, family)}, set by prepare_strict()


def prepare_strict(candles_by_tf, now, days=200):
    """Choose the calls for every index (all candle sizes together) before any replay()."""
    _PICKS.clear()
    _PENDING.clear()
    _PICKS.update(_strict.prepare(candles_by_tf, now, days=days))


# what testing found where a pair has no strategy (shown to the user instead of an invented call)
NO_STRATEGY_NOTE = {
    30: "No strategy reached 70% wins on 30-minute candles for any index (too few setups per day, wide stops) - "
        "live read only.",
    "default": "No tested strategy for this index on this candle size held 70% wins on both the older history "
               "and the held-back last 60 days - live read only, no call is issued.",
}


def resample(cs5, tf):
    """Real 5m candles -> tf-minute candles aligned to 09:15 IST. `done` = every 5m member is in."""
    if tf == 5:
        return cs5
    full = tf // 5
    out, cur = [], None
    for c in cs5:
        key = (_day(c["t"]), (_mins(c["t"]) - 555) // tf)
        if key != cur:
            cur = key
            out.append({"t": c["t"], "open": c["open"], "high": c["high"], "low": c["low"],
                        "close": c["close"], "volume": 0, "n": 1, "tend": c["t"], "dur": tf * 60})
        else:
            o = out[-1]
            o["high"], o["low"], o["close"] = max(o["high"], c["high"]), min(o["low"], c["low"]), c["close"]
            o["n"] += 1
            o["tend"] = c["t"]
    for o in out:
        o["done"] = o["n"] >= full or _mins(o["tend"]) >= 15 * 60 + 25
    return out


def _day(t):
    return (t + 19800) // 86400


def _mins(t):
    return ((t + 19800) % 86400) // 60


def _features(cs):
    closes = [c["close"] for c in cs]
    vw, s, k, cur = [0.0] * len(cs), 0.0, 0, None
    for i, c in enumerate(cs):
        d = _day(c["t"])
        if d != cur:
            cur, s, k = d, 0.0, 0
        s += (c["high"] + c["low"] + c["close"]) / 3
        k += 1
        vw[i] = s / k
    return {"e9": analysis.ema_series(closes, 9), "e21": analysis.ema_series(closes, 21),
            "e50": analysis.ema_series(closes, 50), "rsi": analysis.rsi_series(closes, 14),
            "atr": analysis.atr_series(cs, 14), "vw": vw}


def _bias_ok(F, cs, i, sign, mode):
    e9, e21, e50, vw, c = F["e9"][i], F["e21"][i], F["e50"][i], F["vw"][i], cs[i]["close"]
    if mode == "strong":
        return (e9 > e21 > e50 and c > vw) if sign > 0 else (e9 < e21 < e50 and c < vw)
    return (e21 > e50) if sign > 0 else (e21 < e50)


def _signal(cs, F, i, p):
    """+1 / -1 / 0 using candles <= i only."""
    if i < 60:
        return 0
    c = cs[i]
    m = _mins(c["t"])
    if m < p["firstEntry"] or m > p["lastEntry"]:
        return 0
    e9, e21, e50, r = F["e9"][i], F["e21"][i], F["e50"][i], F["rsi"][i]
    if None in (e9, e21, e50, r) or not F["atr"][i]:
        return 0
    if p["family"] == "vwapx":
        pc, pv, vw = cs[i - 1]["close"], F["vw"][i - 1], F["vw"][i]
        if pc <= pv and c["close"] > vw and r >= 50 and _bias_ok(F, cs, i, 1, p["bias"]):
            return 1
        if pc >= pv and c["close"] < vw and r <= 50 and _bias_ok(F, cs, i, -1, p["bias"]):
            return -1
        return 0
    pb, e9p = cs[i - 1], F["e9"][i - 1]
    if e9p is None:
        return 0
    lo, hi = p.get("rsi", (45, 65))
    if (pb["low"] <= e9p * 1.0002 and c["close"] > pb["high"] and c["close"] > e9
            and lo <= r <= hi and _bias_ok(F, cs, i, 1, p["bias"])):
        return 1
    if (pb["high"] >= e9p * 0.9998 and c["close"] < pb["low"] and c["close"] < e9
            and (100 - hi) <= r <= (100 - lo) and _bias_ok(F, cs, i, -1, p["bias"])):
        return -1
    return 0


def patterns(cs, i, atr):
    """Candlestick patterns ending at candle i (candles <= i only). Returns (bullish, bearish) name lists."""
    if i < 2 or not atr:
        return [], []
    c, p, pp = cs[i], cs[i - 1], cs[i - 2]
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    body, rng = abs(cl - o), max(h - l, 1e-9)
    lw, uw = min(o, cl) - l, h - max(o, cl)
    pb, ppb = abs(p["close"] - p["open"]), abs(pp["close"] - pp["open"])
    bull, bear = [], []
    if cl > o and p["close"] < p["open"] and cl >= p["open"] and o <= p["close"] and body > pb:
        bull.append("Bullish engulfing")
    if cl < o and p["close"] > p["open"] and cl <= p["open"] and o >= p["close"] and body > pb:
        bear.append("Bearish engulfing")
    if body > 0 and lw >= 2 * body and uw <= body and rng >= 0.5 * atr:
        bull.append("Hammer")
    if body > 0 and uw >= 2 * body and lw <= body and rng >= 0.5 * atr:
        bear.append("Shooting star")
    if body >= 0.7 * rng and rng >= 0.9 * atr:
        (bull if cl > o else bear).append("Bullish thrust candle" if cl > o else "Bearish thrust candle")
    if p["high"] < pp["high"] and p["low"] > pp["low"]:
        if cl > p["high"]:
            bull.append("Inside-bar breakout up")
        if cl < p["low"]:
            bear.append("Inside-bar breakdown")
    if pp["close"] < pp["open"] and ppb >= 0.6 * atr and pb <= 0.4 * ppb and cl > o and cl > (pp["open"] + pp["close"]) / 2:
        bull.append("Morning star")
    if pp["close"] > pp["open"] and ppb >= 0.6 * atr and pb <= 0.4 * ppb and cl < o and cl < (pp["open"] + pp["close"]) / 2:
        bear.append("Evening star")
    return bull, bear


def _round(x, nd=2):
    return None if x is None else round(x, nd)


def _confirm_fill(cs, i, sign, p, fine, now):
    """Confirmation entry: the first 1-minute bar (after the signal candle closed) that trades `d` points further in the call's direction,
    within `wait` minutes. Returns ("fill", minute_start, price) / ("pending", trigger, until) / ("skip", trigger, None)."""
    d, wait = p["confirm"]
    tf = p["tf"]
    te = cs[i]["t"] + tf * 60
    trigger = cs[i]["close"] + sign * d
    until = te + wait * 60
    bars = fine if tf > 1 else (cs[i + 1:] + [c for c in (fine or []) if c["t"] > cs[-1]["t"]])
    for c in bars or []:
        if c["t"] < te:
            continue
        if c["t"] >= until:
            break
        if (sign > 0 and c["high"] >= trigger) or (sign < 0 and c["low"] <= trigger):
            return "fill", c["t"], trigger
    if now is not None and now < until:
        return "pending", trigger, until
    return "skip", trigger, None


def _trade(cs, i, sign, p, fine=None, fine_t=None, now=None, fill=None):
    """Walk the path after the entry candle and return the call outcome fields.

    The path is the real 1-minute candles when they are available for the whole window (so we know
    whether the stop or a target was touched FIRST, instead of guessing inside a 5m/15m candle),
    otherwise the signal candles themselves with the conservative rule "a candle touching both counts
    as the stop"."""
    entry = cs[i]["close"] if not fill else fill[1]
    n, tf = len(cs), p["tf"]
    t1, t2, t3, sl = p["t1"], p["t2"], p["t3"], p["sl"]
    day_start = cs[i]["t"] - ((cs[i]["t"] + 19800) % 86400)
    deadline = min(cs[i]["t"] + p["horizon"] * tf * 60, day_start + (SESSION_CLOSE_MIN - 1) * 60)
    end = i
    while end + 1 < n and cs[end + 1]["t"] <= deadline and _day(cs[end + 1]["t"]) == _day(cs[i]["t"]):
        end += 1
    te = cs[i]["t"] + tf * 60 if not fill else fill[0]   # entry = close of the signal candle, or the minute the confirmation traded
    # full scoring window = signal candle + horizon candles, capped at the close. Open calls are tracked all the
    # way to "now" on the 1-minute path INCLUDING the still-forming minute, so a touch counts the moment the
    # minute's high/low reaches a level, without waiting for any candle to complete.
    wfull = min(cs[i]["t"] + (p["horizon"] + 1) * tf * 60, day_start + SESSION_CLOSE_MIN * 60)
    covered_to = min(cs[end]["t"] + tf * 60, day_start + SESSION_CLOSE_MIN * 60)   # the last 30m candle ends after the close
    path, step, res = [c for c in cs[i + 1:end + 1] if c["t"] >= te], tf * 60, "%sm" % tf
    if fine and tf > 1:
        a, b = bisect.bisect_left(fine_t, te), bisect.bisect_left(fine_t, wfull)
        avail_end = min(wfull, fine_t[b - 1] + 60) if b > a else te
        need = (avail_end - te) // 60
        if need > 0 and (b - a) >= 0.9 * need and avail_end >= covered_to:
            path, step, res = fine[a:b], 60, "1m"
    elif fine and tf == 1:                    # fine = the forming 1-minute candle(s) after the last completed one
        tail = [c for c in fine if c["t"] > cs[end]["t"] and c["t"] <= wfull - 60 and _day(c["t"]) == _day(cs[i]["t"])]
        if tail:
            path = path + tail
    stopped, maxhit, stop_t, hit_t = False, 0, None, {}
    mfe = 0.0
    plans = {k: {"stop": -sl, "pts": 0.0, "alive": True, "endT": None, "reason": None, "kind": None, "maxfav": 0.0}
             for k in MANAGE}
    t1_pts, t1_done = 0.0, False
    for c in path:
        hi = (c["high"] - entry) * sign if sign > 0 else (entry - c["low"])
        lo = (c["low"] - entry) if sign > 0 else (entry - c["high"])
        mfe = max(mfe, hi)
        sl_hit = lo <= -sl
        if not stopped:
            if sl_hit:
                stopped, stop_t = True, c["t"]
            else:
                for j, t in enumerate((t1, t2, t3), 1):
                    if hi >= t and j > maxhit:
                        maxhit = j
                        hit_t[j] = c["t"]
        if not t1_done:
            if sl_hit:
                t1_pts, t1_done = -sl, True
            elif hi >= t1:
                t1_pts, t1_done = t1, True
        for k, pl in plans.items():
            if not pl["alive"]:
                continue
            rule = MANAGE[k]
            if lo <= pl["stop"]:                      # stop first inside a candle
                pl["pts"] = pl["stop"]
                if pl["stop"] <= -sl + 1e-9:
                    pl["kind"], pl["reason"] = "SL", "Stop loss touched"
                elif pl["stop"] > 1e-9:
                    pl["kind"], pl["reason"] = "LOCK", "Stop moved above entry after the trigger - touched (profit locked)"
                else:
                    pl["kind"], pl["reason"] = "BE", "Stop moved to entry after T1 - touched"
                pl["alive"], pl["endT"] = False, c["t"]
                continue
            if rule["tp"] and hi >= t3:
                pl["pts"], pl["kind"], pl["reason"] = t3, "TP", "Target T3 booked"
                pl["alive"], pl["endT"] = False, c["t"]
                continue
            pl["maxfav"] = max(pl["maxfav"], hi)
            if pl["maxfav"] >= rule["be_trig"] * t1:      # protective stop applies from the NEXT candle on
                pl["stop"] = max(pl["stop"], rule["be_off"] * t1)
    complete = ((end + 1 < n) or cs[end]["t"] >= deadline
                or cs[end]["t"] + tf * 60 >= day_start + SESSION_CLOSE_MIN * 60
                or (now is not None and now >= wfull))
    resolved = stopped or maxhit == 3
    last = ((path[-1]["close"] if path else cs[end]["close"]) - entry) * sign
    if not complete and not resolved:
        bucket = "OPEN"
    elif maxhit:
        bucket = "T%d" % maxhit
    elif stopped:
        bucket = "SL"
    else:
        bucket = "TIMEOUT"
    for pl in plans.values():
        if pl["alive"]:                               # still open at the end of the data: mark to market
            pl["pts"] = last
            pl["endT"] = path[-1]["t"] if path else None
            pl["kind"] = "END"
            pl["reason"] = ("Window ended - closed at last price" if complete
                            else "Still running - hold with the stop at entry")
            pl["alive"] = not complete
    if not t1_done:
        t1_pts = last

    def after(t):                              # minutes after entry by which the level had been touched
        return int((t - te) // 60 + step // 60) if t else None
    dec_t = stop_t if bucket == "SL" else hit_t.get(maxhit)

    def variant(t1v, slv):
        """Single-lot Runner with its own T1 and stop on the SAME real path (stop first inside a minute; stop to entry after T1)."""
        vstop, mx, t1_t, vpts, vkind, vend = -slv, 0.0, None, None, None, None
        t2_t = t3_t = None
        for c in path:
            vhi, vlo = ((c["high"] - entry, c["low"] - entry) if sign > 0 else (entry - c["low"], entry - c["high"]))
            if vlo <= vstop:
                vpts, vkind, vend = vstop, ("SL" if vstop <= -slv + 1e-9 else "BE"), c["t"]
                break
            if vhi >= t1v and t1_t is None:
                t1_t = c["t"]
            if vhi >= 2 * t1v and t2_t is None:
                t2_t = c["t"]
            if vhi >= 3 * t1v and t3_t is None:
                t3_t = c["t"]
            mx = max(mx, vhi)
            if mx >= t1v:
                vstop = max(vstop, 0.0)
        else:
            vpts, vkind, vend = last, "END", (path[-1]["t"] if path else None)
        return {"t1": _round(entry + sign * t1v, 2), "t1Pts": _round(t1v, 1), "t1At": t1_t, "t1After": after(t1_t),
                "t2": _round(entry + sign * 2 * t1v, 2), "t2Pts": _round(2 * t1v, 1), "t2At": t2_t, "t2After": after(t2_t),
                "t3": _round(entry + sign * 3 * t1v, 2), "t3Pts": _round(3 * t1v, 1), "t3At": t3_t, "t3After": after(t3_t),
                "best": 3 if t3_t else 2 if t2_t else 1 if t1_t else 0,
                "sl": _round(entry - sign * slv, 2), "slPts": _round(slv, 1), "slAt": vend if vkind == "SL" else None,
                "slAfter": after(vend) if vkind == "SL" else None,
                "win": t1_t is not None, "pts": _round(vpts, 1), "kind": vkind, "exitAt": vend,
                "alive": vkind == "END" and not complete}
    variants = {("%g" % r): variant(r * sl, sl) for r in LADDER_RATIOS}
    if p.get("eqx"):
        variants["eq"] = variant(p["eqx"], p["eqx"])
    if TARGET_X.get(p["seg"]):
        variants["tgt"] = variant(TARGET_X[p["seg"]], TARGET_SL[p["seg"]])
    return {"bucket": bucket, "endIdx": end, "mfe": _round(mfe, 1), "res": res,
            "live": bool(path) and now is not None and (path[-1]["t"] + 60 > now if res == "1m" else False),
            "plans": {k: {"pts": _round(v["pts"], 1), "exitAt": v["endT"], "exitAfter": after(v["endT"]),
                          "reason": v["reason"], "kind": v["kind"], "alive": v["alive"]} for k, v in plans.items()},
            "t1Pts": _round(t1_pts, 1), "variants": variants,
            "t1At": hit_t.get(1), "t2At": hit_t.get(2), "t3At": hit_t.get(3), "slAt": stop_t,
            "t1After": after(hit_t.get(1)), "t2After": after(hit_t.get(2)), "t3After": after(hit_t.get(3)),
            "slAfter": after(stop_t), "minutes": after(dec_t),
            "exit": _round(entry + sign * (
                -sl if bucket == "SL" else {1: t1, 2: t2, 3: t3}.get(maxhit, last)), 2)}


def _replay_one(cs, p, since_ts=None, fine=None, now=None, until_ts=None, picks=None):
    """Every call strategy `p` would have issued on `cs` (completed candles of p['tf'] minutes).
    `fine` = real 1-minute candles used to decide which level came first."""
    tf = p["tf"]
    if len(cs) < 120:
        return []
    F = _features(cs)
    fine_t = [c["t"] for c in fine] if fine else None
    out, blocked_t, per_day = [], -1, collections.Counter()
    lookback = max(1, 60 // tf)
    # the latest completed candle may itself be a signal, so a fresh call appears as soon as its candle closes
    for i in range(60, len(cs)):
        if picks is None and cs[i]["t"] <= blocked_t:            # one trade at a time: wait for the previous window to end
            continue
        if since_ts and cs[i]["t"] < since_ts:
            continue
        if until_ts and cs[i]["t"] >= until_ts:
            break
        pk = None
        if picks is not None:                   # strict mode: the cross-candle selection already applied every rule
            pk = picks.get(cs[i]["t"])
            s = pk[0] if pk else 0
        else:
            s = _signal(cs, F, i, p)
        if not s:
            continue
        d = _day(cs[i]["t"])
        if picks is None and per_day[d] >= p["maxPerDay"]:
            continue
        fill = None
        if p.get("confirm"):
            how, a1, a2 = _confirm_fill(cs, i, s, p, fine, now)
            if how == "pending":
                _PENDING.setdefault((p["seg"], tf), []).append({
                    "segment": p["seg"], "tf": tf, "name": p["name"], "t": cs[i]["t"], "side": "BUY" if s > 0 else "SELL",
                    "signalClose": _round(cs[i]["close"]), "trigger": _round(a1), "until": a2, "sl": _round(a1 - s * p["sl"]),
                    "t1": _round(a1 + s * p["t1"]), "t2": _round(a1 + s * p["t2"]), "t3": _round(a1 + s * p["t3"]),
                    "slPts": p["sl"], "t1Pts": p["t1"], "t2Pts": p["t2"], "t3Pts": p["t3"], "family": (pk[2] if pk else p["family"]),
                    "id": "%s-%s-%s" % (p["seg"], tf, cs[i]["t"])})
                continue
            if how == "skip":
                continue
            fill = (a1, a2)
        tr = _trade(cs, i, s, p, fine, fine_t, now, fill=fill)
        per_day[d] += 1
        blocked_t = cs[i]["t"] + p["horizon"] * tf * 60 * p.get("gapFrac", 1.0)   # 1.0 = one trade at a time
        e = cs[i]["close"] if not fill else fill[1]
        stamp = datetime.datetime.fromtimestamp(cs[i]["t"] + 19800, datetime.timezone.utc)
        bull, bear = patterns(cs, i, F["atr"][i])
        pb, pr = patterns(cs, i - 1, F["atr"][i - 1])
        mine = (bull + pb) if s > 0 else (bear + pr)
        style = p.get("exit", "runner")
        pr = tr["plans"][style]
        out.append({
            "segment": p["seg"], "tf": tf, "name": p["name"], "t": cs[i]["t"],
            "date": stamp.strftime("%Y-%m-%d"), "time": stamp.strftime("%H:%M"),
            "side": "BUY" if s > 0 else "SELL", "entry": _round(e),
            "sl": _round(e - s * p["sl"]), "t1": _round(e + s * p["t1"]),
            "t2": _round(e + s * p["t2"]), "t3": _round(e + s * p["t3"]),
            "slPts": p["sl"], "t1Pts_": p["t1"], "t2Pts_": p["t2"], "t3Pts_": p["t3"],
            "result": tr["bucket"], "scoredOn": tr["res"], "minutes": tr["minutes"],
            "t1Min": tr["t1After"], "entryAt": cs[i]["t"] + tf * 60 if not fill else fill[0],
            "confirmed": bool(fill), "signalClose": _round(cs[i]["close"]),
            "t1At": tr["t1At"], "t2At": tr["t2At"], "t3At": tr["t3At"], "slAt": tr["slAt"],
            "t1After": tr["t1After"], "t2After": tr["t2After"], "t3After": tr["t3After"],
            "slAfter": tr["slAfter"], "exitStyle": style, "plans": tr["plans"], "variants": tr["variants"],
            "planExitAt": pr["exitAt"], "planReason": pr["reason"], "planExitAfter": pr["exitAfter"],
            "planAlive": pr["alive"],
            "exit": tr["exit"], "mfe": tr["mfe"], "planPts": pr["pts"], "t1ExitPts": tr["t1Pts"],
            "live": tr["live"], "id": "%s-%s-%s%s" % (p["seg"], tf, cs[i]["t"], "-b" if p.get("isAlt") else ""), "family": (pk[2] if pk else p["family"]), "score": (pk[1] if pk else None), "grade": (pk[3] if pk else "A"),
            "preRun": _round((e - cs[i - lookback]["close"]) * s, 1),
            "patterns": sorted(set(mine)),
        })
    return out


def _pts_stats(closed, getter, kind=None):
    """Profit statistics for one way of managing the trades. A call 'gains' when it makes more than +0.1 of its stop,
    'loses' when it falls more than -0.1 of its stop, and is a 'scratch' (about zero) in between."""
    pts = [(getter(r), r["slPts"]) for r in closed]
    n = len(pts)
    gains = [p for p, sl in pts if p > 0.1 * sl]
    loses = [-p for p, sl in pts if p < -0.1 * sl]
    scr = n - len(gains) - len(loses)
    wins_sum = sum(p for p, _ in pts if p > 0)
    loss_sum = sum(-p for p, _ in pts if p <= 0)
    return {
        "avgPlanPts": round(sum(p for p, _ in pts) / n, 1) if n else None,
        "totalPlanPts": round(sum(p for p, _ in pts), 1) if n else 0,
        "avgWinPts": round(sum(gains) / len(gains), 1) if gains else None,       # average of the calls that gained
        "avgLossPts": round(sum(loses) / len(loses), 1) if loses else None,      # average of the calls that lost
        "gainRate": round(len(gains) / n * 100, 1) if n else None,
        "scratchRate": round(scr / n * 100, 1) if n else None,
        "lossRate": round(len(loses) / n * 100, 1) if n else None,
        "profitFactor": round(wins_sum / loss_sum, 2) if loss_sum else None,
        # share of calls that ended with the ORIGINAL stop touched (a full stop-out) under this way of managing the trade
        "stopRate": round(sum(1 for r in closed if kind(r) == "SL") / n * 100, 1) if (n and kind) else None,
    }


def summarize(calls):
    n = len(calls)
    # a call is "closed" once its bucket is final AND the managed single-lot trade has ended (a runner can still be open)
    closed = [r for r in calls if r["result"] != "OPEN" and not r.get("planAlive")]
    c = collections.Counter(r["result"] for r in closed)
    anyt = c["T1"] + c["T2"] + c["T3"]

    def rate(x, d=n):
        return round(x / d * 100, 1) if d else None
    t1m = [r["t1Min"] for r in closed if r.get("t1Min")]
    pre = [r["preRun"] for r in closed if r.get("preRun") is not None]
    out = {
        "calls": n, "SL": c["SL"], "T1": c["T1"], "T2": c["T2"], "T3": c["T3"],
        "timeouts": c["TIMEOUT"], "open": n - len(closed),
        "slRate": rate(c["SL"], len(closed)), "t1Rate": rate(c["T1"], len(closed)),
        "t2Rate": rate(c["T2"], len(closed)), "t3Rate": rate(c["T3"], len(closed)),
        "winRate": rate(anyt, len(closed)),
        "avgT1ExitPts": round(sum(r["t1ExitPts"] for r in closed) / len(closed), 1) if closed else None,
        "medianT1Min": round(statistics.median(t1m)) if t1m else None,
        "medianPreRun": round(statistics.median(pre), 1) if pre else None,
    }
    out.update(_pts_stats(closed, lambda r: r["planPts"], lambda r: r["plans"][r["exitStyle"]]["kind"]))   # default rule
    out["styles"] = {k: _pts_stats(closed, lambda r, k=k: r["plans"][k]["pts"], lambda r, k=k: r["plans"][k]["kind"])
                     for k in EXIT_STYLES} if closed else {}
    # the same calls under other T1 / stop sizes (single-lot Runner): win rate = T1 touched before the stop
    out["ladders"] = {}
    if closed and all("variants" in r for r in closed):
        keys = ["cur"] + ["%g" % r for r in LADDER_RATIOS] + [k for k in ("tgt", "eq") if all(k in r["variants"] for r in closed)]
        for key in keys:
            if key == "cur":
                runner = lambda r: r["plans"]["runner"]["pts"]
                kind = lambda r: r["plans"]["runner"]["kind"]
                win = lambda r: r["result"] in ("T1", "T2", "T3")
                t1p = lambda r: r["t1Pts_"]
                slp = lambda r: r["slPts"]
            else:
                runner = lambda r, key=key: r["variants"][key]["pts"]
                kind = lambda r, key=key: r["variants"][key]["kind"]
                win = lambda r, key=key: r["variants"][key]["win"]
                t1p = lambda r, key=key: r["variants"][key]["t1Pts"]
                slp = lambda r, key=key: r["variants"][key]["slPts"]
            g = _pts_stats(closed, runner, kind)
            wins = [r for r in closed if win(r)]
            g["winRate"] = round(len(wins) / len(closed) * 100, 1)
            g["avgWinCallPts"] = round(sum(runner(r) for r in wins) / len(wins), 1) if wins else None      # Runner: what a winning call books
            g["avgBookPts"] = round(sum(t1p(r) for r in wins) / len(wins), 1) if wins else None            # if you sell everything at T1
            br = [(t1p(r) if win(r) else -slp(r) if kind(r) == "SL" else runner(r)) for r in closed]
            g["bracketAvgPts"] = round(sum(br) / len(br), 1)                                                # exit at T1 or the stop
            out["ladders"][key] = g
    return out


def live_read(cs, tf, p):
    """What the selected candle size says right now, from completed candles. Informational."""
    F = _features(cs)
    i = len(cs) - 1
    e9, e21, e50, r, vw, atr = F["e9"][i], F["e21"][i], F["e50"][i], F["rsi"][i], F["vw"][i], F["atr"][i]
    if None in (e9, e21, e50, r) or not atr:
        return None
    px = cs[i]["close"]
    checks = [("EMA9 vs EMA21", e9 > e21), ("EMA21 vs EMA50", e21 > e50), ("Price vs VWAP", px > vw),
              ("RSI vs 50", r > 50), ("Last 3 candles", px > cs[i - 3]["close"])]
    up = sum(1 for _, v in checks if v)
    bias = "BULLISH" if up >= 4 else "BEARISH" if up <= 1 else "MIXED"
    bull, bear = patterns(cs, i, atr)
    pb, pr = patterns(cs, i - 1, F["atr"][i - 1])
    setup = None
    if p:
        if p["family"] == "strict":
            setup = "Strict mode: a call needs a high score from the six signal types and no other call open for this index."
        elif p["family"] == "vwapx":
            setup = ("Watching for price to reclaim VWAP from below in an up-trend, or lose it from above in a "
                     "down-trend. Price is %s pts %s VWAP." % (_round(abs(px - vw), 1), "above" if px > vw else "below"))
        else:
            setup = "Watching for a pullback to EMA9 followed by a close beyond the previous candle."
    return {"bias": bias, "alignedUp": up, "factors": [{"name": n, "up": v} for n, v in checks],
            "rsi": _round(r, 1), "atr": _round(atr, 1), "vwap": _round(vw), "spot": _round(px),
            "ema": {"e9": _round(e9), "e21": _round(e21), "e50": _round(e50)},
            "patterns": {"bullish": sorted(set(bull + pb)), "bearish": sorted(set(bear + pr))},
            "setup": setup, "candleTime": cs[i]["t"]}


def _usable(cands, tf, now):
    out = []
    for c in cands:
        if c.get("done", True) is False:
            continue
        if c["t"] + c.get("dur", tf * 60) > now:
            continue
        out.append(c)
    return out


ALT_DEDUP_MIN = 20   # an alternate-family call is dropped when the main family already called the same side this close in time


def replay(cs, p, since_ts=None, fine=None, now=None, until_ts=None):
    """Calls of the main family plus the OTHER family (VWAP reclaim <-> trend pullback) on the same candles, so each index and
    candle size issues about twice as many calls. Measured on 120 days of real data both families win about the same
    (58% at T1 = SL, 76% with the small T1), so the extra calls keep the same quality. Set p['alt'] = False to turn this off."""
    if p.get("strict"):
        return _replay_one(cs, p, since_ts=since_ts, fine=fine, now=now, until_ts=until_ts, picks=_PICKS.get((p["seg"], p["tf"]), {}))
    main = _replay_one(cs, p, since_ts=since_ts, fine=fine, now=now, until_ts=until_ts)
    if p.get("alt") is False or p.get("isAlt"):
        return main
    other = dict(p, family="pullback" if p["family"] == "vwapx" else "vwapx", isAlt=True)
    extra = _replay_one(cs, other, since_ts=since_ts, fine=fine, now=now, until_ts=until_ts)
    keep = [c for c in extra if not any(m["side"] == c["side"] and abs(m["t"] - c["t"]) <= ALT_DEDUP_MIN * 60 for m in main)]
    return sorted(main + keep, key=lambda c: c["t"])


_HIST = {}   # (segment, tf, IST day) -> calls of earlier days; they can no longer change, so compute once a day


def build(candles_by_tf, days=60, now=None, futures=None):
    """candles_by_tf: {tf_minutes: {segment: [candles]}}. Returns the API payload with one block per candle size."""
    now = now or time.time()
    futures = futures or {}
    prepare_strict(candles_by_tf, now)
    blocks = {}
    for tf in TIMEFRAMES:
        segments, all_calls, active = [], [], []
        for seg, label in SEGMENTS.items():
            p = STRATEGIES.get((seg, tf))
            ent = {"id": seg, "name": f"{label}", "tf": tf, "validated": bool(p), "error": None,
                   "smallSample": bool(p and p.get("smallSample"))}
            segments.append(ent)
            cs = _usable((candles_by_tf.get(tf) or {}).get(seg) or [], tf, now)
            if len(cs) < 200:
                ent["error"] = "not enough real %sm candles from Upstox" % tf
                continue
            ent["spot"], ent["lastBar"] = cs[-1]["close"], cs[-1]["t"]
            try:
                ent["live"] = live_read(cs, tf, p)
            except Exception as exc:
                ent["live"] = None
            if seg in futures:
                ent["futures"] = futures[seg]
            if not p:
                ent["note"] = NO_STRATEGY_NOTE.get(tf, NO_STRATEGY_NOTE["default"])
                continue
            ent.update(family=p["family"],
                       familyLabel="Strict multi-strategy score (6 signal types)",
                       ladder={"t1": p["t1"], "t2": p["t2"], "t3": p["t3"], "sl": p["sl"], "eqx": p.get("eqx"), "tgtx": TARGET_X.get(p["seg"]), "tgtsl": TARGET_SL.get(p["seg"])},
                       horizonMin=p["horizonMin"], exitStyle=p.get("exit", "runner"))
            raw1 = (candles_by_tf.get(1) or {}).get(seg) or []
            live1 = [c for c in raw1 if c["t"] <= now]                       # includes the forming minute
            fine = live1 if tf > 1 else [c for c in live1 if c["t"] + 60 > now]
            fine = fine or None
            today0 = int(now - ((now + 19800) % 86400))
            hkey = (seg, tf, today0)
            if hkey not in _HIST:
                _HIST[hkey] = replay(cs, p, fine=fine, now=now, until_ts=today0)
                for k in [k for k in _HIST if k[2] != today0]:
                    _HIST.pop(k, None)
            calls = _HIST[hkey] + replay(cs, p, since_ts=today0, fine=fine, now=now)
            since = cs[-1]["t"] - days * 86400
            recent = [r for r in calls if r["t"] >= since]
            older = [r for r in calls if r["t"] < since]
            last_day = datetime.datetime.fromtimestamp(cs[-1]["t"] + 19800, datetime.timezone.utc).strftime("%Y-%m-%d")
            ent.update(stats=summarize(recent), older=summarize(older),
                       olderDays=max(0, round((since - cs[0]["t"]) / 86400)),
                       lastSession=summarize([r for r in recent if r["date"] == last_day]),
                       lastSessionDate=last_day)
            all_calls.extend(recent)
        all_calls.sort(key=lambda r: (r["t"], r["segment"]), reverse=True)
        blocks[str(tf)] = {"tf": tf, "segments": segments, "totals": summarize(all_calls), "calls": all_calls,
                           "active": [r for r in all_calls if r["result"] == "OPEN"]}
    merged = sorted((r for b in blocks.values() for r in b["calls"]), key=lambda r: (r["t"], r["tf"], r["segment"]),
                    reverse=True)
    blocks["all"] = {
        "tf": "all", "calls": merged, "totals": summarize(merged),
        "active": [r for r in merged if r["result"] == "OPEN"],
        "segments": [dict(s, name=f"{s['name']} · {b['tf']}m") for b in list(blocks.values()) if b["tf"] != "all"
                     for s in b["segments"] if s.get("validated") and s.get("stats")],
    }
    ist = time.gmtime(now + 19800)
    return {
        "pending": [x for v in _PENDING.values() for x in v],
        "generatedAt": int(now), "istClock": time.strftime("%Y-%m-%d %H:%M:%S", ist), "days": days,
        "target": {"winRate": 70}, "timeframes": blocks,
        "noEdge": [{"id": k, "name": v} for k, v in NO_EDGE.items()],
        "method": {
            "signal": "A separate strategy per index and per candle size. Nifty, Bank Nifty, Sensex: VWAP reclaim in a "
                      "trend - price crossing back through the day's VWAP while EMA9/21/50 already point the same way, "
                      "so the call fires at the start of the leg; Next 50 uses the same idea. All decided on "
                      "completed candles. Independent of the long-term precision profiles.",
            "window": "Entries 09:25-14:15 IST, max 3 per index per day, one trade at a time. Each call is scored for "
                      "its window (1-2 hours) or until 15:30 IST.",
            "buckets": "Every call is counted once, at the highest target reached before the stop "
                       "(SL before T1 = SL only; T1 then SL = T1; T2 reached = T2 only; T3 = T3). "
                       "Win = T1, T2 or T3 reached before the stop. Outcomes are scored on the real 1-MINUTE candles, so "
                       "which came first (SL or a target) is read from the minute path, not guessed inside a 5m/15m "
                       "candle; only if both are touched inside the same 1-minute candle is it counted as the stop. "
                       "(Where 1-minute history is missing, the signal candles are used and a double-touch counts as the stop.)",
            "plan": "One position, no extra lots. Default rule (Runner): the stop moves to entry once T1 is touched and you hold "
                    "until the window ends or the stop is hit. Profit changes with the rule, never the win rate. Before "
                    "brokerage and slippage; index points, not option premium. 1-minute targets are only 9-67 points, so costs "
                    "can erase most of that edge.",
            "exitStyles": "Tested on 1,485 real calls: the single-lot Runner rule earned 0.227 R per call on older history and "
                          "0.44 R on the last 60 days, versus 0.118 / 0.206 for the old 1/3-at-each-target plan. Even under a strict "
                          "assumption (a minute that touches T1 and falls back to entry is stopped) it stays positive (+0.03 / "
                          "+0.10 R). About 24% of calls are still full stop-outs and about 6 in 10 end near zero; a few big trend "
                          "moves pay for them. Entry filters, other stop widths, other signal types and pullback / confirmation "
                          "entries were all tested and did NOT reduce stop-outs without losing profit.",
            "risk": "T1 is deliberately close, which is what makes 70%+ wins possible, so a stop-out costs more than a "
                    "typical win. The edge is the win rate and profit factor, not a 4:1 payoff. 5m stops are about 0.4x "
                    "the final target; going lower (0.2-0.3x) dropped the win rate under 70% on the older history for "
                    "every index. Extra entry filters (higher-timeframe agreement, volatility regime, distance from VWAP, "
                    "candle quality, trend strength) looked better on older data but failed on the last 60 days, so they "
                    "are not used.",
            "patterns": "Candlestick patterns (engulfing, hammer, shooting star, thrust candle, inside-bar breakout, "
                        "morning/evening star) are recognised and shown on every call and live read. Requiring them did "
                        "not raise the win rate in testing, so they are not used as a gate.",
            "volumeOi": "Index candles have zero volume. Futures volume and open interest are shown as live context "
                        "(Nifty / Bank Nifty / Sensex near-month future). The liquid history available without the paid "
                        "Upstox Plus plan was only ~30 calls, which is too short to prove they improve accuracy.",
            "honesty": "Strategies and levels were chosen on history before the last 60 days; the last 60 days were the "
                       "check. Each index/candle size has only 30-110 calls in that window, so a 70% rate carries roughly "
                       "+/-10 points of uncertainty. Judge it on calls made from today onward.",
        },
    }
