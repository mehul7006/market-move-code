"""Indicator math, option-chain analytics and the composite signal engine.

Honesty rules baked into this module:
- Every strategy vote is shown with its raw inputs so a human can audit it.
- `hitRate` is a real walk-forward count on the instrument's own candles (technical
  strategies only, next-candle horizon) - not a claimed prediction accuracy.
"""
from __future__ import annotations

import datetime
import math
import time


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def _compact(n):
    a = abs(n)
    if a >= 1e7:
        return f"{n / 1e7:+.2f} Cr"
    if a >= 1e5:
        return f"{n / 1e5:+.2f} L"
    if a >= 1e3:
        return f"{n / 1e3:+.1f} K"
    return f"{n:+d}"


def ema_series(vals, p):
    n, out = len(vals), [None] * len(vals)
    if n < p:
        return out
    e = sum(vals[:p]) / p
    out[p - 1] = e
    k = 2 / (p + 1)
    for i in range(p, n):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_series(vals, p=14):
    n, out = len(vals), [None] * len(vals)
    if n <= p:
        return out
    gains = losses = 0.0
    for i in range(1, p + 1):
        d = vals[i] - vals[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / p, losses / p
    out[p] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(p + 1, n):
        d = vals[i] - vals[i - 1]
        ag = (ag * (p - 1) + (d if d > 0 else 0)) / p
        al = (al * (p - 1) + (-d if d < 0 else 0)) / p
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd_series(vals):
    e12, e26 = ema_series(vals, 12), ema_series(vals, 26)
    line = [None if (e12[i] is None or e26[i] is None) else e12[i] - e26[i] for i in range(len(vals))]
    sig = [None] * len(vals)
    first = next((i for i, v in enumerate(line) if v is not None), None)
    if first is not None:
        for k, v in enumerate(ema_series(line[first:], 9)):
            if v is not None:
                sig[first + k] = v
    hist = [None if (line[i] is None or sig[i] is None) else line[i] - sig[i] for i in range(len(vals))]
    return line, sig, hist


def atr_series(candles, p=14):
    n, out = len(candles), [None] * len(candles)
    if n <= p:
        return out
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["high"] - c["low"])
        else:
            pc = candles[i - 1]["close"]
            trs.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    a = sum(trs[1:p + 1]) / p
    out[p] = a
    for i in range(p + 1, n):
        a = (a * (p - 1) + trs[i]) / p
        out[i] = a
    return out


def supertrend(candles, p=10, mult=1.5):
    n = len(candles)
    atrs = atr_series(candles, p)
    line, direction = [None] * n, [0] * n
    fub = flb = None
    for i in range(1, n):
        if atrs[i] is None:
            continue
        c, prev = candles[i], candles[i - 1]
        mid = (c["high"] + c["low"]) / 2
        ub, lb = mid + mult * atrs[i], mid - mult * atrs[i]
        fub = ub if (fub is None or ub < fub or prev["close"] > fub) else fub
        flb = lb if (flb is None or lb > flb or prev["close"] < flb) else flb
        prev_dir = direction[i - 1] or 1
        if prev_dir == 1:
            direction[i] = -1 if c["close"] < flb else 1
        else:
            direction[i] = 1 if c["close"] > fub else -1
        line[i] = flb if direction[i] == 1 else fub
    return line, direction


# ---------------------------------------------------------------- option chain

def analyze_chain(chain, spot, step):
    """Turn a raw NSE option-chain payload (or demo equivalent) into OI analytics."""
    recs = (chain or {}).get("records", {}).get("data") or []
    expiries = (chain or {}).get("records", {}).get("expiryDate") or []
    if not recs or not expiries or not spot:
        return None
    expiry = expiries[0]
    strikes, ce_oi = [], 0
    pe_oi = ce_chg = pe_chg = 0
    ce_oi = 0
    for rec in recs:
        leg_ce = (rec.get("CE") or {}).get(expiry)
        leg_pe = (rec.get("PE") or {}).get(expiry)
        if not leg_ce and not leg_pe:
            continue
        s = {
            "strike": rec["strike"],
            "ceOI": (leg_ce or {}).get("oi") or 0, "peOI": (leg_pe or {}).get("oi") or 0,
            "ceChg": (leg_ce or {}).get("chnginOI") or 0, "peChg": (leg_pe or {}).get("chnginOI") or 0,
            "ceVol": (leg_ce or {}).get("volume") or 0, "peVol": (leg_pe or {}).get("volume") or 0,
            "ceLTP": (leg_ce or {}).get("ltp"), "peLTP": (leg_pe or {}).get("ltp"),
            "ceIV": (leg_ce or {}).get("iv"), "peIV": (leg_pe or {}).get("iv"),
            "ceDelta": (leg_ce or {}).get("delta"), "peDelta": (leg_pe or {}).get("delta"),
            "ceGamma": (leg_ce or {}).get("gamma"), "peGamma": (leg_pe or {}).get("gamma"),
            "ceTheta": (leg_ce or {}).get("theta"), "peTheta": (leg_pe or {}).get("theta"),
            "ceVega": (leg_ce or {}).get("vega"), "peVega": (leg_pe or {}).get("vega"),
        }
        strikes.append(s)
        ce_oi += s["ceOI"]
        pe_oi += s["peOI"]
        ce_chg += s["ceChg"]
        pe_chg += s["peChg"]
    if not strikes or not ce_oi:
        return None
    strikes.sort(key=lambda s: s["strike"])
    atm = min(strikes, key=lambda s: abs(s["strike"] - spot))
    iv_band = [ (s["ceIV"] + s["peIV"]) / 2 for s in strikes
                if s["ceIV"] and s["peIV"] and abs(s["strike"] - spot) <= 5 * step ]
    iv = round(sum(iv_band) / len(iv_band), 2) if iv_band else None
    straddle = (atm["ceLTP"] or 0) + (atm["peLTP"] or 0) or None
    expected_move_pct = round(straddle / spot * 100, 2) if straddle else None

    best_pain, best_val = None, None
    for cand in strikes:
        p = cand["strike"]
        val = sum(s["ceOI"] * max(0.0, p - s["strike"]) + s["peOI"] * max(0.0, s["strike"] - p)
                  for s in strikes)
        if best_val is None or val < best_val:
            best_val, best_pain = val, p

    supports = sorted(strikes, key=lambda s: -s["peOI"])[:3]
    resistances = sorted(strikes, key=lambda s: -s["ceOI"])[:3]
    near = sorted(strikes, key=lambda s: abs(s["strike"] - spot))[:13]
    near.sort(key=lambda s: s["strike"])

    # delta-weighted positioning, restricted to strikes where deltas carry signal
    call_exp = put_exp = 0.0
    for s in strikes:
        if abs(s["strike"] - spot) > 10 * step:
            continue
        if s["ceDelta"]:
            call_exp += abs(s["ceDelta"]) * s["ceOI"]
        if s["peDelta"]:
            put_exp += abs(s["peDelta"]) * s["peOI"]
    delta_skew = round((call_exp - put_exp) / (call_exp + put_exp), 3) if (call_exp + put_exp) else None

    def _avg(*vals):
        vals = [v for v in vals if v]
        return round(sum(vals) / len(vals), 4) if vals else None

    greeks = {
        "deltaCE": atm["ceDelta"], "deltaPE": atm["peDelta"],
        "gamma": _avg(atm["ceGamma"], atm["peGamma"]),
        "theta": _avg(atm["ceTheta"], atm["peTheta"]),
        "vega": _avg(atm["ceVega"], atm["peVega"]),
    }

    return {
        "expiry": expiry,
        "pcr": round(pe_oi / ce_oi, 2),
        "oiFlow": pe_chg - ce_chg,
        "deltaSkew": delta_skew,
        "greeks": greeks,
        "atm": atm["strike"],
        "iv": iv,
        "straddle": _r(straddle),
        "expectedMovePct": expected_move_pct,
        "maxPain": best_pain,
        "supports": sorted(s["strike"] for s in supports),
        "resistances": sorted(s["strike"] for s in resistances),
        "rows": near,
        "totals": {"ceOI": ce_oi, "peOI": pe_oi, "ceChg": ce_chg, "peChg": pe_chg},
    }


# ---------------------------------------------------------------- composite signal

def _tech_features(candles):
    closes = [c["close"] for c in candles]
    e9, e21 = ema_series(closes, 9), ema_series(closes, 21)
    rsi = rsi_series(closes, 14)
    _, _, hist = macd_series(closes)
    st_line, st_dir = supertrend(candles, 10, 1.5)
    return closes, e9, e21, rsi, hist, st_line, st_dir


def _tech_score_at(i, e9, e21, rsi, hist, st_dir):
    parts = []
    if e9[i] is not None and e21[i] is not None:
        parts.append((18, 1 if e9[i] > e21[i] else -1))
    if rsi[i] is not None:
        parts.append((12, 1 if rsi[i] >= 60 else (-1 if rsi[i] <= 40 else 0)))
    if hist[i] is not None:
        parts.append((12, 1 if hist[i] > 0 else -1))
    if st_dir[i]:
        parts.append((16, st_dir[i]))
    w = sum(x[0] for x in parts)
    return (sum(x[0] * x[1] for x in parts) / w) if w else None


def walk_forward(candles, lookback=250, threshold=0.18):
    """Real hit-rate of the technical half of the engine on this instrument's own history."""
    closes, e9, e21, rsi, hist, _, st_dir = _tech_features(candles)
    n = len(closes)
    if n < 40:
        return None
    start = max(34, n - lookback)
    wins = losses = 0
    for i in range(start, n - 1):
        norm = _tech_score_at(i, e9, e21, rsi, hist, st_dir)
        if norm is None or abs(norm) < threshold:
            continue
        went_up = closes[i + 1] > closes[i]
        if (norm > 0) == went_up:
            wins += 1
        else:
            losses += 1
    trades = wins + losses
    if trades < 15:
        return None
    return {"trades": trades, "wins": wins, "hitRate": round(wins / trades * 100, 1),
            "horizon": "next candle (tech strategies only)"}


# ---------------------------------------------------------------- move forecast

def _per_candle_sigma(closes, lookback=60):
    rets = []
    for i in range(max(1, len(closes) - lookback), len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 10:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))


def _implied_move(options, timeframe, horizon):
    """Option-implied 1-sigma move for the forecast horizon (ATM straddle ≈ 0.8 sigma)."""
    straddle = (options or {}).get("straddle")
    if not straddle:
        return None
    sigma_to_expiry = straddle * 0.8
    if timeframe != "5m":
        return sigma_to_expiry
    try:
        days = max((datetime.date.fromisoformat(options["expiry"]) - datetime.date.today()).days, 1)
    except Exception:
        return None
    # 375 trading minutes per session; scale the to-expiry sigma to the horizon
    return sigma_to_expiry * math.sqrt(min(horizon * 5 / (days * 375), 1.0))


def move_capture(candles, direction, min_move, horizon, lookback=300, threshold=0.18):
    """Measured on this instrument's own history: of past same-direction signals, how often
    did price travel at least `min_move` points in that direction within `horizon` candles."""
    if direction not in ("BULLISH", "BEARISH") or min_move <= 0:
        return None
    closes, e9, e21, rsi, hist, _, st_dir = _tech_features(candles)
    n = len(closes)
    if n < horizon + 40:
        return None
    sign = 1 if direction == "BULLISH" else -1
    start = max(34, n - lookback)
    samples = wins = 0
    for i in range(start, n - horizon):
        norm = _tech_score_at(i, e9, e21, rsi, hist, st_dir)
        if norm is None or norm * sign <= threshold:
            continue
        fut = candles[i + 1:i + 1 + horizon]
        if not fut:
            continue
        best = (max(c["high"] for c in fut) - closes[i]) if sign > 0 else (closes[i] - min(c["low"] for c in fut))
        samples += 1
        if best >= min_move:
            wins += 1
    if samples < 20:
        return None
    return {"prob": round(wins / samples * 100, 1), "samples": samples, "wins": wins}


def _move_timing(candles, direction, atr, expected_move, st_dir=None, macd_hist=None, idx=None):
    """Is the move still ahead of us (EARLY/BUILDING) or already gone (IN PROGRESS/EXTENDED)?"""
    n = len(candles)
    idx = n - 1 if idx is None else idx
    if direction not in ("BULLISH", "BEARISH") or idx < 25 or not atr:
        return {"label": "NO BIAS", "note": "No directional signal to time.", "compression": None, "room": None}
    sign = 1 if direction == "BULLISH" else -1
    px = candles[idx]["close"]
    recent = candles[idx - 6:idx]
    win20 = candles[idx - 20:idx]
    hi, lo = max(c["high"] for c in win20), min(c["low"] for c in win20)
    band = max(c["high"] for c in recent) - min(c["low"] for c in recent)
    compression = round(band / (atr * math.sqrt(6)), 2)
    room = (hi - px) if sign > 0 else (px - lo)
    moved = (px - recent[0]["close"]) * sign
    broke = px > hi if sign > 0 else px < lo

    if st_dir is None:
        _, st_dir = supertrend(candles, 10, 1.5)
    if macd_hist is None:
        _, _, macd_hist = macd_series([c["close"] for c in candles])
    fresh_flip = any(st_dir[idx - k] and st_dir[idx - k] != st_dir[idx] for k in range(1, min(4, idx + 1)))
    macd_flip = any((macd_hist[idx - k] is not None and macd_hist[idx - k - 1] is not None
                     and (macd_hist[idx - k] > 0) != (macd_hist[idx - k - 1] > 0))
                    for k in range(0, min(3, idx)))

    if moved >= expected_move * 1.2:
        label, note = "EXTENDED", f"{moved:.0f} pts of the expected {expected_move:.0f} pt move already done."
    elif broke and moved >= expected_move * 0.5:
        label, note = "IN PROGRESS", f"20-bar extreme already broken; {moved:.0f} pts done — late entry."
    elif compression <= 0.8 and room <= max(expected_move * 0.6, atr):
        label = "EARLY"
        note = (f"6-bar range is {compression:.2f}x ATR (compressed), only {room:.0f} pts to the "
                f"20-bar breakout — move has not started.")
    elif fresh_flip or macd_flip:
        label, note = "EARLY", "Fresh Supertrend/MACD flip — momentum just turned, move ahead."
    else:
        label = "BUILDING"
        note = "Trend intact, but range is not compressed at the edge yet."
    return {"label": label, "note": note, "compression": compression,
            "room": round(room, 1), "moved": round(moved, 1), "broke": broke}


DEFAULT_FORECAST_PARAMS = {
    "key": "DEFAULT", "name": "standard",
    "horizon": 12, "stopAtr": 0.5, "stopMoveMult": None,
    "minCapture": 50, "minConviction": 50, "minTechConviction": None,
    "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 1.0, "blend": None,
    "unanimity": False, "cooldown": 0, "auditBars": 100000, "minWinRate": None,
    "minExpMult": 1.0, "entryBefore": None,
}


def _forecast_params(params):
    p = dict(DEFAULT_FORECAST_PARAMS)
    if params:
        p.update(params)
    return p


def _tech_alignment(candles, direction):
    """Technical conviction, unanimity and per-indicator votes at the last bar."""
    closes, e9, e21, rsi, hist, st_line, st_dir = _tech_features(candles)
    i = len(closes) - 1
    norm = _tech_score_at(i, e9, e21, rsi, hist, st_dir)
    sign = 1 if direction == "BULLISH" else (-1 if direction == "BEARISH" else 0)
    votes = {}
    if e9[i] is not None and e21[i] is not None:
        votes["EMA 9/21"] = 1 if e9[i] > e21[i] else -1
    if hist[i] is not None:
        votes["MACD"] = 1 if hist[i] > 0 else -1
    if st_dir[i]:
        votes["Supertrend"] = st_dir[i]
    if rsi[i] is not None and (rsi[i] >= 60 or rsi[i] <= 40):
        votes["RSI 14"] = 1 if rsi[i] >= 60 else -1
    unanimous = bool(votes) and len(votes) >= 3 and all(v == sign for v in votes.values()) and sign != 0
    return {
        "score": norm,
        "conviction": round(abs(norm) * 100, 1) if norm is not None else None,
        "votes": votes,
        "unanimous": unanimous,
        "against": sum(1 for v in votes.values() if v == -sign),
    }


def build_forecast(spot, candles, options, signal, timeframe, min_move, max_move, params=None,
                   audit=None):
    """Real expected-move estimate + measured capture odds + pre-move timing.

    `params` selects the instrument's forecast profile (horizon, stop width, blend weights,
    conviction/capture gates and allowed timing). `audit` is that profile's measured
    walk-forward result; a profile with a `minWinRate` refuses to call a trade when the
    measurement no longer clears it. A call is qualified only when every gate passes;
    otherwise the blocking reason is returned — never a forced prediction.
    """
    if not signal or not candles or not spot:
        return None
    p = _forecast_params(params)
    direction = signal["direction"]
    sign = 1 if direction == "BULLISH" else (-1 if direction == "BEARISH" else 0)
    horizon = 1 if timeframe != "5m" else p["horizon"]
    horizon_label = ("next session" if timeframe != "5m"
                     else f"next ~{horizon * 5} min ({horizon} x 5m candles)")
    closes = [c["close"] for c in candles]
    atr = (atr_series(candles, 14) or [None])[-1]
    align = _tech_alignment(candles, direction) if timeframe == "5m" else None

    estimators = {}
    if atr:
        estimators["atr"] = round(atr * math.sqrt(horizon) * 0.8, 1)
    sigma = _per_candle_sigma(closes, 60)
    if sigma:
        estimators["realizedVol"] = round(spot * sigma * math.sqrt(horizon), 1)
    implied = _implied_move(options, timeframe, horizon)
    if implied:
        estimators["optionImplied"] = round(implied, 1)
    vals = sorted(v for v in estimators.values() if v and v > 0)
    if not vals:
        return None
    weights = {k: w for k, w in (p.get("blend") or {}).items() if estimators.get(k)}
    if weights:
        expected = round(sum(estimators[k] * w for k, w in weights.items()) / sum(weights.values()), 1)
    else:
        mid = len(vals) // 2
        expected = round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 1)

    capture = move_capture(candles, direction, min_move, horizon)
    # The audit's expected move is ATR-only (the only estimator it can reconstruct), so the
    # pre-move timing gate must use the same number or live calls would not match the audit.
    gate_expected = (atr * math.sqrt(horizon) * 0.8) if (timeframe == "5m" and atr) else expected
    timing = _move_timing(candles, direction, atr, gate_expected)

    reasons = []
    for s in sorted([x for x in signal["strategies"] if x["vote"]], key=lambda x: -x["weight"])[:3]:
        side = "BUY" if s["vote"] > 0 else "SELL"
        reasons.append(f"{s['label']}: {s['detail']} → {side} (weight {s['weight']})")
    if options:
        if options.get("pcr") is not None:
            reasons.append(f"OI: PCR {options['pcr']} (puts {'heavier' if options['pcr'] >= 1 else 'lighter'} than calls)")
        if options.get("oiFlow") is not None:
            reasons.append(f"OI flow Δ(PE−CE) {_compact(options['oiFlow'])} — "
                           f"{'put writers adding' if options['oiFlow'] > 0 else 'call writers adding'}")
        if options.get("maxPain"):
            reasons.append(f"Max pain {int(options['maxPain'])} vs spot {_r(spot)} — "
                           f"{'pull up' if spot < options['maxPain'] else 'pull down'}")
        if options.get("deltaSkew") is not None:
            reasons.append(f"Delta-weighted OI skew {options['deltaSkew']:+.2f}")
    if "atr" in estimators:
        reasons.append(f"ATR(14) {_r(atr)} × √{horizon} → {estimators['atr']:.0f} pts expected")
    if "realizedVol" in estimators:
        reasons.append(f"Realized candle σ {sigma * 100:.3f}% → {estimators['realizedVol']:.0f} pts")
    if "optionImplied" in estimators:
        reasons.append(f"ATM straddle {options.get('straddle')} implies {estimators['optionImplied']:.0f} pts "
                       f"for the {horizon_label}")
    if capture:
        reasons.append(f"Own history: {capture['prob']}% of {capture['samples']} same-direction setups "
                       f"reached {min_move} pts within the horizon")
    reasons.append(timing["note"])

    plan = None
    if direction != "NEUTRAL":
        stop, risk = signal["stop"], abs(signal["stop"] - spot)
        stop_adjusted = False
        if p.get("stopMoveMult"):
            stop = spot - sign * min_move * p["stopMoveMult"]
            risk = abs(spot - stop)
            stop_adjusted = True
            reasons.append(f"75% profile: stop set at {p['stopMoveMult']}× the "
                           f"{min_move}-pt target ({_r(risk)} pts). Wide stop, lower R:R — "
                           f"that is what makes T1-before-SL reliable.")
        elif atr and risk < p["stopAtr"] * atr:
            stop = spot - sign * p["stopAtr"] * atr
            risk = abs(spot - stop)
            stop_adjusted = True
            reasons.append(f"Structure stop was only {abs(signal['stop'] - spot):.2f} pts away "
                           f"(< {p['stopAtr']}×ATR); plan widens it to {_r(stop)} "
                           f"({p['stopAtr']}×ATR) so noise does not stop the trade.")
        t1_move = min_move
        t2_move = min(max(expected, min_move), max_move)
        t3_move = t2_move + max(t2_move - t1_move, t1_move)
        t1, t2, t3 = spot + sign * t1_move, spot + sign * t2_move, spot + sign * t3_move
        plan = {
            "entry": _r(spot), "stop": _r(stop),
            "target1": _r(t1), "target2": _r(t2), "target3": _r(t3),
            "riskPerUnit": _r(risk),
            "target1Move": t1_move, "target2Move": round(t2_move, 1),
            "target3Move": round(t3_move, 1),
            "rr1": round(t1_move / risk, 1) if risk else None,
            "rr2": round(t2_move / risk, 1) if risk else None,
            "rr3": round(t3_move / risk, 1) if risk else None,
            "stopAdjusted": stop_adjusted,
            "stopRule": (f"{p['stopMoveMult']}× min move" if p.get("stopMoveMult")
                         else f"max(structure, {p['stopAtr']}×ATR)"),
        }

    # Each gate is independent (an `if`, not `elif`): a later gate must still run when an
    # earlier one passes, otherwise a passing audit gate would silently disable the rest.
    status = "OPPORTUNITY"
    if direction == "NEUTRAL":
        status = "NO BIAS"
    if status == "OPPORTUNITY" and expected < min_move:
        status = f"BELOW MIN MOVE ({expected:.0f} < {min_move})"
    # The audit only ever sees the ATR estimator, so this gate must use the same one — the
    # blended median would let live calls through that the measured profile never takes.
    atr_exp = gate_expected
    if status == "OPPORTUNITY" and p.get("minExpMult") and atr_exp is not None \
            and atr_exp < min_move * p["minExpMult"]:
        status = (f"ATR EXPECTED MOVE {atr_exp:.0f} < {round(min_move * p['minExpMult'])} "
                  f"REQUIRED ({p['minExpMult']:.0f}× target)")
    if status == "OPPORTUNITY" and p.get("entryBefore"):
        now_min = ((candles[-1]["t"] + 19800) % 86400) // 60
        if now_min > p["entryBefore"]:
            status = f"ENTRY WINDOW CLOSED (after {p['entryBefore'] // 60:02d}:{p['entryBefore'] % 60:02d} IST)"
    if status == "OPPORTUNITY" and timing["label"] in ("IN PROGRESS", "EXTENDED"):
        status = "MOVE ALREADY STARTED"
    if status == "OPPORTUNITY" and timing["label"] not in p["allowedTiming"]:
        status = f"TIMING {timing['label']} — WAIT FOR TRIGGER"
    if status == "OPPORTUNITY" and p.get("unanimity") and align and not align["unanimous"]:
        status = f"INDICATORS SPLIT ({align['against']} against) — NO TRADE"
    if status == "OPPORTUNITY" and p.get("minTechConviction") \
            and (align or {}).get("conviction") is not None \
            and align["conviction"] < p["minTechConviction"]:
        status = f"LOW SETUP QUALITY ({align['conviction']:.0f}%)"
    if status == "OPPORTUNITY" and signal["confidence"] < p["minConviction"]:
        status = "LOW CONVICTION"
    if status == "OPPORTUNITY" and p.get("minWinRate"):
        measured = (audit or {}).get("winRate")
        if measured is None:
            status = "PROFILE AUDIT UNAVAILABLE — NO CALL"
        elif measured < p["minWinRate"]:
            status = f"PROFILE EDGE WEAK ({measured}% < {p['minWinRate']}%)"
    if status == "OPPORTUNITY" and capture and p["minCapture"] \
            and capture["prob"] < p["minCapture"]:
        status = "LOW CAPTURE ODDS"
    if status == "OPPORTUNITY" and plan and p["rrGate"] \
            and plan["rr2"] is not None and plan["rr2"] < p["rrGate"]:
        status = "RISK > REWARD"
    qualified = status == "OPPORTUNITY"

    return {
        "direction": direction,
        "minMove": min_move, "maxMove": max_move,
        "horizon": horizon, "horizonLabel": horizon_label,
        "expectedMove": expected,
        "estimateLow": vals[0], "estimateHigh": vals[-1],
        "estimators": estimators,
        "captureProb": capture["prob"] if capture else None,
        "captureSamples": capture["samples"] if capture else None,
        "timing": timing["label"], "timingNote": timing["note"],
        "compression": timing.get("compression"), "roomToBreakout": timing.get("room"),
        "status": status, "qualified": qualified,
        "confidence": signal["confidence"],
        "techConviction": (align or {}).get("conviction"),
        "techVotes": (align or {}).get("votes"),
        "unanimous": (align or {}).get("unanimous"),
        "auditWinRate": (audit or {}).get("winRate"),
        "auditSamples": (audit or {}).get("samples"),
        "auditExpectancy": (audit or {}).get("expectancyPts"),
        "profile": p["name"], "profileKey": p["key"],
        "gates": {"minConviction": p["minConviction"], "minCapture": p["minCapture"],
                  "allowedTiming": list(p["allowedTiming"]), "rrGate": p["rrGate"],
                  "stopAtr": p["stopAtr"], "stopMoveMult": p.get("stopMoveMult"),
                  "minTechConviction": p.get("minTechConviction"),
                  "minWinRate": p.get("minWinRate"),
                  "minExpMult": p.get("minExpMult"), "entryBefore": p.get("entryBefore")},
        "plan": plan,
        "reasons": reasons,
    }



def validate_forecast(candles, min_move, params=None, lookback=1000):
    """Walk-forward audit of a forecast profile on the instrument's own 5m candles.

    At every historical bar only information available up to that bar is used. A setup
    counts only if it would have passed the same gates as live (conviction, unanimity,
    expected move, profile timing), with the same one-at-a-time cooldown. Outcome = T1
    (minimum move) hit before stop within the horizon; if both are touched inside one
    candle the stop is assumed first (conservative).
    """
    p = _forecast_params(params)
    horizon = p["horizon"]
    n = len(candles)
    if n < horizon + 60:
        return None
    closes, e9, e21, rsi, hist, _, st_dir = _tech_features(candles)
    atrs = atr_series(candles, 14)
    start = max(40, n - (p.get("auditBars") or lookback))
    samples = wins = losses = unresolved = reach = 0
    risk_sum = 0.0
    resolved_risk_sum = 0.0
    bars_to_t1, examples, months = [], [], {}
    last_exit, cooldown = -10 ** 9, p.get("cooldown") or 0
    for i in range(start, n - horizon):
        if i <= last_exit + cooldown:
            continue
        norm = _tech_score_at(i, e9, e21, rsi, hist, st_dir)
        min_conv = max(p["minConviction"], p.get("minTechConviction") or 0)
        if norm is None or abs(norm) * 100 < min_conv:
            continue
        sign = 1 if norm > 0 else -1
        atr = atrs[i]
        if not atr:
            continue
        if p.get("unanimity"):
            # Mirrors _tech_alignment: EMA 9/21, MACD, Supertrend always vote; RSI only
            # votes when it is at an extreme, so a neutral RSI neither helps nor blocks.
            votes = [1 if (e9[i] is not None and e21[i] is not None and e9[i] > e21[i]) else -1]
            if hist[i] is None or st_dir[i] is None:
                continue
            votes += [1 if hist[i] > 0 else -1, st_dir[i]]
            if rsi[i] is not None and (rsi[i] >= 60 or rsi[i] <= 40):
                votes.append(1 if rsi[i] >= 60 else -1)
            if any(v != sign for v in votes):
                continue
        expected = atr * math.sqrt(horizon) * 0.8
        if expected < min_move:
            continue
        if p.get("minExpMult") and expected < min_move * p["minExpMult"]:
            continue
        if p.get("entryBefore"):
            bar_min = ((candles[i]["t"] + 19800) % 86400) // 60
            if bar_min > p["entryBefore"]:
                continue
        direction = "BULLISH" if sign > 0 else "BEARISH"
        timing = _move_timing(candles, direction, atr, expected,
                              st_dir=st_dir, macd_hist=hist, idx=i)
        if timing["label"] not in p["allowedTiming"]:
            continue
        entry = closes[i]
        stop_dist = min_move * p["stopMoveMult"] if p.get("stopMoveMult") else p["stopAtr"] * atr
        stop = entry - sign * stop_dist
        t1 = entry + sign * min_move
        outcome, bars = "open", None
        mfe = 0.0
        for k, c in enumerate(candles[i + 1:i + 1 + horizon], 1):
            fav = (c["high"] - entry) if sign > 0 else (entry - c["low"])
            mfe = max(mfe, fav)  # full-horizon excursion, even after the trade resolves
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
        month = datetime.datetime.fromtimestamp(
            candles[i]["t"] + 19800, datetime.timezone.utc).strftime("%Y-%m")
        mb = months.setdefault(month, {"samples": 0, "wins": 0})
        mb["samples"] += 1
        risk_sum += abs(entry - stop)
        last_exit = i + horizon
        if outcome in ("t1", "stop"):
            resolved_risk_sum += abs(entry - stop)
        if outcome == "t1":
            wins += 1
            mb["wins"] += 1
            bars_to_t1.append(bars)
        elif outcome == "stop":
            losses += 1
        else:
            unresolved += 1
        if outcome == "t1" or mfe >= min_move:
            reach += 1
        examples.append({
            "barTime": candles[i]["t"], "direction": direction,
            "entry": _r(entry), "expected": _r(expected, 1),
            "stop": _r(stop), "target1": _r(t1),
            "outcome": outcome, "bars": bars, "mfe": _r(mfe, 1),
        })
        examples = examples[-6:]
    if not samples:
        return {"profile": p["name"], "samples": 0, "lowSample": True,
                "minMove": min_move, "horizon": horizon,
                "note": "No historical setup passed this profile's gates in the lookback window."}
    avg_risk = risk_sum / samples
    resolved = wins + losses
    avg_resolved_risk = (resolved_risk_sum / resolved) if resolved else 0.0
    expectancy = ((wins * min_move - losses * avg_resolved_risk) / resolved) if resolved else None
    return {
        "profile": p["name"], "profileKey": p["key"],
        "minMove": min_move, "horizon": horizon, "horizonLabel": f"{horizon * 5} min",
        "stopAtr": p["stopAtr"], "stopMoveMult": p.get("stopMoveMult"),
        "stopRule": (f"{p['stopMoveMult']}× min move" if p.get("stopMoveMult")
                     else f"{p['stopAtr']}×ATR"),
        "unanimity": bool(p.get("unanimity")), "cooldown": cooldown,
        "minExpMult": p.get("minExpMult"), "entryBefore": p.get("entryBefore"),
        "lookbackBars": n - start,
        "windowStart": candles[start]["t"], "windowEnd": candles[n - 1]["t"],
        "samples": samples, "wins": wins, "losses": losses, "unresolved": unresolved,
        "winRate": round(wins / samples * 100, 1),
        "reachRate": round(reach / samples * 100, 1),
        "avgRisk": round(avg_risk, 1),
        "rr1": round(min_move / avg_risk, 2) if avg_risk else None,
        "expectancyPts": round(expectancy, 1) if expectancy is not None else None,
        "avgBarsToT1": round(sum(bars_to_t1) / len(bars_to_t1), 1) if bars_to_t1 else None,
        "lowSample": samples < 15,
        "months": [{"month": m, "samples": v["samples"],
                    "winRate": round(v["wins"] / v["samples"] * 100, 1)}
                   for m, v in sorted(months.items())],
        "worstMonth": min(round(v["wins"] / v["samples"] * 100, 1)
                          for v in months.values()),
        "examples": examples,
    }


def build_signal(spot, candles, options, timeframe):
    closes, e9, e21, rsi, hist, st_line, st_dir = _tech_features(candles)
    e50 = ema_series(closes, 50)
    atrs = atr_series(candles, 14)
    i = len(closes) - 1
    px = spot or closes[i]

    strategies = []

    def add(label, detail, vote, weight):
        strategies.append({"label": label, "detail": detail, "vote": vote, "weight": weight})

    if e9[i] is not None and e21[i] is not None:
        add("Trend - EMA 9/21", f"{_r(e9[i])} vs {_r(e21[i])}", 1 if e9[i] > e21[i] else -1, 18)
    if rsi[i] is not None:
        add("Momentum - RSI 14", f"RSI {_r(rsi[i])}",
            1 if rsi[i] >= 60 else (-1 if rsi[i] <= 40 else 0), 12)
    if hist[i] is not None:
        add("MACD histogram", f"{_r(hist[i])}", 1 if hist[i] > 0 else -1, 12)
    if st_dir[i]:
        add("Supertrend 10 x 1.5", f"line {_r(st_line[i])}", st_dir[i], 16)
    if e50[i] is not None:
        add("Regime - EMA 50", "price above" if px > e50[i] else "price below",
            1 if px > e50[i] else -1, 8)

    if options:
        if options["pcr"] is not None:
            add("Options - PCR (OI)", f"PCR {options['pcr']}",
                1 if options["pcr"] >= 1.15 else (-1 if options["pcr"] <= 0.85 else 0), 12)
        if options["oiFlow"] is not None:
            add("Options - OI build-up", f"d(PE-CE) {_compact(options['oiFlow'])}",
                1 if options["oiFlow"] > 0 else -1, 10)
        if options["maxPain"]:
            add("Options - Max pain pull", f"{options['maxPain']} vs {_r(px)}",
                1 if px < options["maxPain"] else -1, 8)
        sup = max([s for s in options["supports"] if s < px], default=None)
        res = min([r for r in options["resistances"] if r > px], default=None)
        if sup and res:
            pos = (px - sup) / (res - sup)
            add("Options - OI range", f"{sup} - {res} | at {round(pos * 100)}%",
                1 if pos >= 0.7 else (-1 if pos <= 0.3 else 0), 10)
        if options["expectedMovePct"]:
            add("Expiry math", f"IV {options['iv']}% | straddle {options['straddle']} "
                f"(move +/- {options['expectedMovePct']}%)", 0, 0)
        if options.get("deltaSkew") is not None:
            ds = options["deltaSkew"]
            add("Options - delta-weighted OI", f"skew {ds:+.2f}",
                1 if ds > 0.08 else (-1 if ds < -0.08 else 0), 10)

    weight = sum(x["weight"] for x in strategies) or 1
    raw = sum(x["vote"] * x["weight"] for x in strategies)
    norm = raw / weight
    direction = "BULLISH" if norm >= 0.18 else ("BEARISH" if norm <= -0.18 else "NEUTRAL")

    atr = atrs[i] or px * 0.006
    if direction == "BEARISH":
        stop = st_line[i] if (st_line[i] and st_line[i] > px) else px + 1.5 * atr
        risk = abs(stop - px)
        t1, t2 = px - risk, px - 2 * risk
    else:
        stop = st_line[i] if (st_line[i] and st_line[i] < px) else px - 1.5 * atr
        risk = abs(px - stop)
        t1, t2 = px + risk, px + 2 * risk

    return {
        "direction": direction,
        "score": round(norm, 3),
        "confidence": min(95, round(abs(norm) * 100)),
        "entry": _r(px),
        "stop": _r(stop),
        "target1": _r(t1),
        "target2": _r(t2),
        "riskPerShare": _r(risk),
        "riskReward": "1 : 2",
        "atr": _r(atr),
        "timeframe": timeframe,
        "strategies": strategies,
        "hitRate": walk_forward(candles),
        "oiRange": {"support": sup, "resistance": res} if options else None,
    }
