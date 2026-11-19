"""STRICT short-term call selection for Nifty, Bank Nifty and Sensex.

Six signal families (VWAP reclaim, trend pullback, opening-range breakout, EMA 9/21 cross, 20-candle breakout, MACD zero-cross) are
evaluated on 1, 5 and 15-minute candles. Every signal is scored by a per-index linear model (strict_model.json, ridge regression on the
single-lot R-multiple of ~100 days of real calls). A call is issued only when

    * the score clears the index's threshold (the top ~10-20 % of all signals),
    * no other call of the SAME index is still open (1, 5 and 15 minute calls at the same time count once),
    * fewer than MAX_PER_DAY calls were already issued for that index today.

Selection is causal: signals are processed in the order their candle completes, using only candles <= the signal candle.
Training and live scoring use the same feature code (this file), so the numbers in the model file are what the page runs.
"""
from __future__ import annotations

import bisect
import collections
import json
import os
import statistics

import analysis

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "strict_model.json")
SEGMENTS = ("NIFTY", "BANKNIFTY", "SENSEX")
TFS = (1, 5)                                   # 15-minute calls lost money in testing, so they are not used
# B tier = a looser filter (strict_model.json "thrB") that adds ~0.5 calls per session. Out of sample it looked fine (Nifty 59%, Sensex 59.5% wins) but on the
# 120-day replay the B calls won only 49.5% (+9 per call) and pulled the totals down, so it is OFF by default. Set STRICT_B_TIER=1 to turn it on.
USE_B_TIER = os.environ.get("STRICT_B_TIER", "0").strip() in ("1", "true", "yes")
CROSS_DEDUPE_MIN = 5                            # the same side called on several indices within this many minutes counts as ONE sure call (best score kept);
                                                # the others are shown on the 60%+ confidence page. 0 = off
CONF60_MIN = 0.60                               # "60%+ confidence" page: the extra calls of a looser cut-off, used only if they won >= 60% out of sample
# Trend-day exception (tested on 120 days, kept only where both the win rate and the net rose): when the index has already moved a lot today in the
# call's direction (day_move in its top share) and the higher timeframe agrees, the score cut-off is lowered and the quality filter is skipped.
TREND = {"NIFTY": {"dm_q": 0.70, "lower": 0.10}, "BANKNIFTY": None, "SENSEX": None}   # Bank Nifty: lowered its win rate; Sensex: lowered its net - not used
FAMILIES = ("vwapx", "pullback", "orb", "ema", "brk", "macd")
MAX_PER_DAY = 5
HORIZON_MIN = {1: 60, 5: 120}          # how long a call is followed
HTF = {1: 5, 5: 15}                       # higher timeframe used for the trend-alignment feature
FIRST_ENTRY, LAST_ENTRY = 9 * 60 + 25, 14 * 60 + 15        # SURE calls: entries 09:25-14:15 (locked)
CONF_LAST_ENTRY = 15 * 60 + 15                               # confidence-tier calls may be generated until 15:15
CONF_PATH = os.path.join(HERE, "conf_model.json")
SESSION_CLOSE = 15 * 60 + 30
# Per-index call rules (found by a walk-forward search, see train_strict.py): which candle sizes may issue a call and in which time window.
# "window" = (earliest, latest) signal time as hours after 09:15 (None = no limit): 2.75 -> 12:00, 0.75 -> 10:00, 4.25 -> 13:30.
RULES = {
    # "confirm" = (points, minutes): after the signal candle closes, the call is entered only if price trades that many points FURTHER in the call's
    # direction within that many minutes (a stop-entry order at signal close +/- points); otherwise it is cancelled. Out-of-sample this lifted
    # Sensex from 67% to 70% wins / +38 to +54 points per call in every 30-day block; Bank Nifty did better without it.
    "NIFTY": {"tfs": (5,), "window": (None, None), "confirm": None},          # 5-minute candles only (confirmation: 71% wins out of sample but
                                                                              # only +5.5 per call on the 120-day replay, so not used)
    "BANKNIFTY": {"tfs": (1,), "window": (None, None), "confirm": None},       # 1-minute candles, all day, enter at the signal close
    "SENSEX": {"tfs": (5,), "window": (None, None), "confirm": (4.5, 10)},     # 5-minute candles, all day
}


# Quality filter per index (found by a driver analysis of the issued calls, kept only because it raised the win rate AND did not lower the net in
# every 30-day out-of-sample block): a selected call is dropped when this input is in its weakest third. The cut is stored in strict_model.json
# ("filter"), measured on the model's own selected calls during training.
FILTERS = {
    "NIFTY": ("hour", "low"),                # skip the earliest third of the day's signals (dedicated Nifty search: 68.8% wins / +16.2 out of sample,
                                             # 64.2% / +14.5 on the 120-day replay, every 30-day block profitable)
    "BANKNIFTY": None,                       # tested (skip price at the wrong end of the day's range): same - not used
    "SENSEX": ("rsi_side", "high"),          # RSI already stretched in the call's direction (late entry): 62% wins, +63 per call on the replay
}


def is_trend(mdl, x):
    t = (mdl or {}).get("trend")
    if not t:
        return False
    return x["feat"][FEATURE_NAMES.index("day_move")] >= t["dmCut"] and x["feat"][FEATURE_NAMES.index("htf_align")] > 0


def threshold(mdl, x):
    if is_trend(mdl, x):
        return mdl["trend"]["thr"]
    return mdl.get("thrB", mdl["thr"]) if USE_B_TIER else mdl["thr"]


def prob(mdl, sc):
    """Historical rate of 'T1 before the stop' for signals scoring about this much (calibration table in strict_model.json)."""
    best = None
    for lo, wr in (mdl or {}).get("calib") or []:
        if sc >= lo:
            best = wr
    return best


def passes(mdl, x):
    if is_trend(mdl, x):
        return True
    f = (mdl or {}).get("filter")
    if not f:
        return True
    v = x["feat"][FEATURE_NAMES.index(f["feature"])]
    return v > f["cut"] if f["side"] == "low" else v < f["cut"]


def filter_cut(seg, picks):
    """Cut value (a third of the way in) for the index's filter, from a set of selected calls."""
    if not FILTERS.get(seg):
        return None
    name, side = FILTERS[seg]
    v = sorted(x["feat"][FEATURE_NAMES.index(name)] for x in picks)
    if len(v) < 30:
        return None
    return {"feature": name, "side": side, "cut": v[len(v) // 3] if side == "low" else v[2 * len(v) // 3]}


def allowed(seg, x):
    r = RULES.get(seg) or {}
    if r.get("tfs") and x["tf"] not in r["tfs"]:
        return False
    lo, hi = r.get("window") or (None, None)
    h = x["feat"][0]
    return (lo is None or h >= lo) and (hi is None or h <= hi)


FEATURE_NAMES = ["hour", "atr_ratio", "trend_strength", "vwap_distance", "candle_position", "rsi_side", "range_position", "ema_slope",
                 "day_move", "pre_run", "htf_align", "other_families", "f_vwapx", "f_pullback", "f_orb", "f_ema", "f_brk", "f_macd",
                 "tf1", "tf5"]

_model = {"mtime": None, "data": {}}
_conf = {"mtime": None, "data": {}}


def conf_model():
    try:
        m = os.path.getmtime(CONF_PATH)
    except OSError:
        return {}
    if _conf["mtime"] != m:
        with open(CONF_PATH, encoding="utf-8") as fh:
            _conf["data"] = json.load(fh)
        _conf["mtime"] = m
    return _conf["data"]


def model():
    try:
        m = os.path.getmtime(MODEL_PATH)
    except OSError:
        return {}
    if _model["mtime"] != m:
        with open(MODEL_PATH, encoding="utf-8") as fh:
            _model["data"] = json.load(fh)
        _model["mtime"] = m
    return _model["data"]


def _day(t):
    return (t + 19800) // 86400


def _mins(t):
    return ((t + 19800) % 86400) // 60


# ---------------------------------------------------------------------------------------------------- features
def features(cs):
    closes = [c["close"] for c in cs]
    vw, s, k, cur = [0.0] * len(cs), 0.0, 0, None
    for i, c in enumerate(cs):
        d = _day(c["t"])
        if d != cur:
            cur, s, k = d, 0.0, 0
        s += (c["high"] + c["low"] + c["close"]) / 3
        k += 1
        vw[i] = s / k
    return {"e9": analysis.ema_series(closes, 9), "e21": analysis.ema_series(closes, 21), "e50": analysis.ema_series(closes, 50),
            "rsi": analysis.rsi_series(closes, 14), "atr": analysis.atr_series(cs, 14), "vw": vw, "cl": closes,
            "macd_h": analysis.macd_series(closes)[2]}


def raw_signals(cs, F, start=60, last_entry=None):
    """{family: [(i, side)]} for candles i >= start, using candles <= i only."""
    out = {f: [] for f in FAMILIES}
    n = len(cs)
    d0 = None
    or_hi = or_lo = None
    for i in range(max(60, start), n):
        c = cs[i]
        m = _mins(c["t"])
        a = F["atr"][i]
        if not a or m < FIRST_ENTRY or m > (last_entry or LAST_ENTRY):
            continue
        e9, e21, e50, r = F["e9"][i], F["e21"][i], F["e50"][i], F["rsi"][i]
        if None in (e9, e21, e50, r):
            continue
        close, vw = c["close"], F["vw"][i]
        pc, pv = cs[i - 1]["close"], F["vw"][i - 1]
        # 1. VWAP reclaim in a trend
        if pc <= pv and close > vw and r >= 50 and e21 > e50:
            out["vwapx"].append((i, 1))
        elif pc >= pv and close < vw and r <= 50 and e21 < e50:
            out["vwapx"].append((i, -1))
        # 2. trend pullback resume
        pb, e9p = cs[i - 1], F["e9"][i - 1]
        if e9p is not None:
            if pb["low"] <= e9p * 1.0002 and close > pb["high"] and close > e9 and 45 <= r <= 65 and e21 > e50:
                out["pullback"].append((i, 1))
            elif pb["high"] >= e9p * 0.9998 and close < pb["low"] and close < e9 and 35 <= r <= 55 and e21 < e50:
                out["pullback"].append((i, -1))
        # 3. opening-range breakout (range = first 15 minutes)
        if m >= 9 * 60 + 30:
            j = i
            while j > 0 and _day(cs[j - 1]["t"]) == _day(c["t"]):
                j -= 1
            rng = [x for x in cs[j:i + 1] if _mins(x["t"]) < 9 * 60 + 30]
            if rng:
                orh, orl = max(x["high"] for x in rng), min(x["low"] for x in rng)
                if pc <= orh < close and e21 > e50 and r > 50:
                    out["orb"].append((i, 1))
                elif pc >= orl > close and e21 < e50 and r < 50:
                    out["orb"].append((i, -1))
        # 4. EMA 9/21 cross with VWAP and RSI
        p9, p21 = F["e9"][i - 1], F["e21"][i - 1]
        if p9 is not None and p21 is not None:
            if p9 <= p21 and e9 > e21 and close > vw and r > 52:
                out["ema"].append((i, 1))
            elif p9 >= p21 and e9 < e21 and close < vw and r < 48:
                out["ema"].append((i, -1))
        # 5. 20-candle breakout in a trend
        hi = max(x["high"] for x in cs[i - 20:i])
        lo = min(x["low"] for x in cs[i - 20:i])
        if close > hi and e21 > e50 and close > vw:
            out["brk"].append((i, 1))
        elif close < lo and e21 < e50 and close < vw:
            out["brk"].append((i, -1))
        # 6. MACD histogram zero-cross in a trend
        h, hp = F["macd_h"][i], F["macd_h"][i - 1]
        if h is not None and hp is not None:
            if hp <= 0 < h and e21 > e50 and r > 50:
                out["macd"].append((i, 1))
            elif hp >= 0 > h and e21 < e50 and r < 50:
                out["macd"].append((i, -1))
    return out


def _htf_index(cs, tf, hcs, htf):
    ends = [h["t"] + htf * 60 for h in hcs]
    return [bisect.bisect_right(ends, c["t"] + tf * 60) - 1 for c in cs]


def candidates(cs, tf, hcs, start_t=None, last_entry=None):
    """Every raw signal with its feature vector. cs = completed candles of this size, hcs = completed higher-timeframe candles."""
    if len(cs) < 120 or not hcs:
        return []
    F = features(cs)
    start_i = 60
    if start_t is not None:
        start_i = max(60, bisect.bisect_left([c["t"] for c in cs], start_t))
    sig = raw_signals(cs, F, start_i, last_entry)
    HF = features(hcs)
    hmap = _htf_index(cs, tf, hcs, HTF[tf])
    atrs = F["atr"]
    by_i = collections.defaultdict(lambda: collections.defaultdict(set))
    for fam, lst in sig.items():
        for i, s in lst:
            by_i[i][s].add(fam)
    # lookups that need the whole day
    dopen, run_hi, run_lo, cur = {}, [0.0] * len(cs), [0.0] * len(cs), None
    hi_ = lo_ = 0.0
    for i, c in enumerate(cs):
        d = _day(c["t"])
        if d not in dopen:
            dopen[d] = c["open"]
        if d != cur:
            cur, hi_, lo_ = d, c["high"], c["low"]
        hi_, lo_ = max(hi_, c["high"]), min(lo_, c["low"])
        run_hi[i], run_lo[i] = hi_, lo_
    out = []
    lookback = max(1, 60 // tf)
    for fam, lst in sig.items():
        for i, s in lst:
            c, a = cs[i], atrs[i]
            e21, e50, r = F["e21"][i], F["e50"][i], F["rsi"][i]
            vals = [x for x in atrs[max(0, i - 500):i + 1] if x]
            med = statistics.median(vals) if vals else a
            cp = (c["close"] - c["low"]) / max(c["high"] - c["low"], 1e-9)
            if s < 0:
                cp = 1 - cp
            rp = (c["close"] - run_lo[i]) / max(run_hi[i] - run_lo[i], 1e-9)
            if s < 0:
                rp = 1 - rp
            h, hok = hmap[i], 0.0
            if h >= 51 and HF["e21"][h] is not None and HF["e50"][h] is not None:
                up = HF["e21"][h] > HF["e50"][h] and HF["cl"][h] > HF["vw"][h]
                dn = HF["e21"][h] < HF["e50"][h] and HF["cl"][h] < HF["vw"][h]
                hok = 1.0 if (up if s > 0 else dn) else -1.0
            fams = set()
            for j in range(i - 3, i + 1):
                fams |= by_i[j][s] if j in by_i else set()
            e21_6 = F["e21"][i - 6]
            feat = [(_mins(c["t"]) - 555) / 60.0, a / med if med else 1.0, abs(e21 - e50) / a, abs(c["close"] - F["vw"][i]) / a, cp,
                    (r if s > 0 else 100 - r) / 100.0, rp, ((e21 - e21_6) / a * s) if e21_6 is not None else 0.0,
                    (c["close"] - dopen[_day(c["t"])]) / a * s, (c["close"] - cs[i - lookback]["close"]) / a * s, hok,
                    float(len(fams) - 1)] + [1.0 if fam == f else 0.0 for f in FAMILIES] + [float(tf == t) for t in TFS]
            day_start = c["t"] - ((c["t"] + 19800) % 86400)
            end = min(c["t"] + (HORIZON_MIN[tf] // tf + 1) * tf * 60, day_start + SESSION_CLOSE * 60)
            out.append({"tf": tf, "i": i, "t": c["t"], "done": c["t"] + tf * 60, "end": end, "side": s, "fam": fam, "feat": feat})
    return out


def score(mdl, feat):
    mu, sd, w = mdl["mu"], mdl["sd"], mdl["w"]
    return sum(((feat[j] - mu[j]) / sd[j]) * w[j] for j in range(len(feat))) + w[-1]


def select(cands, mdl, max_per_day=MAX_PER_DAY):
    """Causal selection for ONE index across all candle sizes: score >= threshold, one open call at a time, max per day."""
    picks, per, blocked = [], collections.Counter(), -1
    for x in sorted(cands, key=lambda c: (c["done"], -c.get("score", 0.0))):
        if x["done"] < blocked:                         # another call of this index is still open
            continue
        if x["score"] < threshold(mdl, x):              # trend days use a lower cut-off; B tier (off by default) a looser one
            continue
        x["grade"] = "A" if (x["score"] >= mdl["thr"] or is_trend(mdl, x)) else "B"
        d = _day(x["t"])
        if per[d] >= max_per_day:
            continue
        per[d] += 1
        blocked = x["end"]
        picks.append(x)
    return picks


def cross_dedupe(chosen, window_min=None):
    """chosen = [(segment, candidate with 'margin')]. The same side signalled on several indices within the window is ONE market call:
    keep the index whose score clears its own threshold by the most."""
    w = (CROSS_DEDUPE_MIN if window_min is None else window_min) * 60
    if not w:
        return list(chosen)
    items = sorted(chosen, key=lambda sx: sx[1]["done"])
    out, used = [], set()
    for i, (seg, c) in enumerate(items):
        if i in used:
            continue
        grp = [j for j in range(i, len(items)) if j not in used and items[j][1]["side"] == c["side"] and abs(items[j][1]["done"] - c["done"]) <= w]
        best = max(grp, key=lambda j: items[j][1]["margin"])
        used.update(grp)
        out.append(items[best])
    return out


# ---------------------------------------------------------------------------------------------------- live use
_CACHE = {}
LAST_CONF60 = {}   # (segment, tf) -> {candle time: (side, score, family, "C", probability, reason)} from the last prepare()


def prepare(candles_by_tf, now, days=120):
    """Compute the selected calls for every index. Returns {(segment, tf): {candle_time: (side, score, family)}}.
    Finished days are cached; today is recomputed on every call."""
    out, chosen, conf60 = {}, [], []
    mdls = model()
    today0 = int(now - ((now + 19800) % 86400))
    first = today0 - days * 86400
    for seg in SEGMENTS:
        mdl = mdls.get(seg)
        if not mdl:
            continue
        allc, pool = [], []
        for tf in TFS:
            cs = [c for c in (candles_by_tf.get(tf) or {}).get(seg) or [] if c.get("done", True) is not False and c["t"] + c.get("dur", tf * 60) <= now]
            raw5 = [c for c in (candles_by_tf.get(5) or {}).get(seg) or [] if c.get("done", True) is not False and c["t"] + c.get("dur", 300) <= now]
            h = HTF[tf]
            if h == 5:
                hcs = raw5
            else:
                hcs = [c for c in (candles_by_tf.get(h) or {}).get(seg) or []] if candles_by_tf.get(h) else None
                if not hcs:
                    import shortterm
                    hcs = shortterm.resample(raw5, h)
                hcs = [c for c in hcs if c["t"] + h * 60 <= now]
            ck = (seg, tf, today0, len(cs) and cs[0]["t"])
            if ck not in _CACHE:
                for k in [k for k in _CACHE if k[0] == seg and k[1] == tf and k[2] != today0]:
                    _CACHE.pop(k, None)
                old = candidates(cs, tf, hcs, start_t=first, last_entry=CONF_LAST_ENTRY)
                _CACHE[ck] = [x for x in old if x["t"] < today0]
            fresh = [x for x in candidates(cs, tf, hcs, start_t=today0, last_entry=CONF_LAST_ENTRY) if x["t"] >= today0]
            for x in _CACHE[ck] + fresh:
                if x["t"] < first:
                    continue
                pool.append(x)
                if allowed(seg, x) and _mins(x["t"]) <= LAST_ENTRY:          # SURE calls: unchanged rules, entries until 14:15
                    x["score"] = score(mdl, x["feat"])
                    allc.append(x)
        sure_ids = set()
        for x in select(allc, mdl):
            if not passes(mdl, x):
                continue
            x["margin"] = x["score"] - threshold(mdl, x)
            chosen.append((seg, x))
            sure_ids.add((x["tf"], x["t"]))
        # 60%+ confidence: every other signal whose historical win rate at its score is >= CONF60_MIN (own one-at-a-time / per-day rule)
        cm = conf_model().get(seg)
        if cm:
            rest = []
            for x in pool:
                if (x["tf"], x["t"]) in sure_ids or x["tf"] not in cm["tfs"] or _mins(x["t"]) > cm.get("lastEntry", CONF_LAST_ENTRY):
                    continue
                cs_ = score(cm, x["feat"])
                if cs_ >= cm["thr"]:
                    rest.append(dict(x, score=cs_, prob=(cm.get("outOfSample") or {}).get("winRate", 0) / 100.0,
                                     why="confidence tier" + (" (after 14:15)" if _mins(x["t"]) > LAST_ENTRY else "")))
            for x in select(rest, dict(cm, thr=-1e9, trend=None), max_per_day=cm.get("maxPerDay", 8)):
                conf60.append((seg, x))
    kept = cross_dedupe(chosen)
    kept_ids = {(s, x["tf"], x["t"]) for s, x in kept}
    for seg, x in chosen:
        if (seg, x["tf"], x["t"]) not in kept_ids:          # same move called on another index at the same moment: counted once
            mate = next((s for s, y in kept if y["side"] == x["side"] and abs(y["done"] - x["done"]) <= CROSS_DEDUPE_MIN * 60), None)
            m_ = model().get(seg) or {}
            conf60.append((seg, dict(x, prob=((m_.get("outOfSample") or {}).get("winRate") or 0) / 100.0, why="same move already called on %s" % (mate or "another index"))))
    for seg, x in kept:
        out.setdefault((seg, x["tf"]), {})[x["t"]] = (x["side"], round(x["score"], 3), x["fam"], x.get("grade", "A"))
    groups = []                                              # same side within CROSS_DEDUPE_MIN on several indexes = one counted call
    for seg, x in sorted(conf60, key=lambda sx: sx[1]["done"]):
        g = next((g for g in groups if g["side"] == x["side"] and abs(g["done"] - x["done"]) <= max(CROSS_DEDUPE_MIN, 5) * 60), None)
        if g is None:
            groups.append({"side": x["side"], "done": x["done"], "lead": seg})
            x["group"] = len(groups)
        else:
            x["group"] = groups.index(g) + 1
            if "same move" not in (x.get("why") or ""):
                x["why"] = (x.get("why") or "") + "; same move as %s (counted once)" % g["lead"]
    LAST_CONF60.clear()
    for seg, x in conf60:
        LAST_CONF60.setdefault((seg, x["tf"]), {})[x["t"]] = (x["side"], round(x["score"], 3), x["fam"], "C", round(x.get("prob") or 0, 3), x.get("why", ""), x.get("group"))
    return out


def _why_not_sure(mdl, x):
    if x["score"] < threshold(mdl, x):
        return "score below the sure-call cut-off"
    if not passes(mdl, x):
        return "quality filter (%s)" % ((mdl.get("filter") or {}).get("feature") or "")
    return "another call of this index was open"
