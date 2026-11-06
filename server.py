"""Market Signal Desk - local dashboard server.

Data policy: Upstox live feed first, Yahoo real candles as fallback, and a hard ERROR
state when neither is reachable. There is no synthetic/mock data path.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analysis
import data_sources as yahoo
import upstox

PORT = int(os.environ.get("PORT", "8787"))
ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")

ENV = upstox.load_env(os.path.join(ROOT, ".env"))
TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN") or ENV.get("UPSTOX_ACCESS_TOKEN", "")
UX = upstox.UpstoxClient(TOKEN)

INSTRUMENTS = [
    {"id": "SENSEX", "name": "BSE Sensex", "group": "prime", "key": "BSE_INDEX|SENSEX",
     "chain": "BSE_INDEX|SENSEX", "step": 100, "minMove": 40, "maxMove": 80, "yahoo": "^BSESN"},
    {"id": "NIFTY", "name": "Nifty 50", "group": "prime", "key": "NSE_INDEX|Nifty 50",
     "chain": "NSE_INDEX|Nifty 50", "step": 50, "minMove": 25, "maxMove": 50, "yahoo": "^NSEI"},
    {"id": "BANKNIFTY", "name": "Nifty Bank", "group": "prime", "key": "NSE_INDEX|Nifty Bank",
     "chain": "NSE_INDEX|Nifty Bank", "step": 100, "minMove": 50, "maxMove": 100, "yahoo": "^NSEBANK"},
    {"id": "FINNIFTY", "name": "Nifty Fin Service", "group": "prime", "key": "NSE_INDEX|Nifty Fin Service",
     "chain": "NSE_INDEX|Nifty Fin Service", "step": 100, "minMove": 30, "maxMove": 60, "yahoo": None},
    {"id": "MIDCPNIFTY", "name": "Nifty Midcap Select", "group": "prime", "key": "NSE_INDEX|NIFTY MID SELECT",
     "chain": "NSE_INDEX|NIFTY MID SELECT", "step": 25, "minMove": 20, "maxMove": 40, "yahoo": None},
    {"id": "IT", "name": "Nifty IT", "group": "sector", "key": "NSE_INDEX|Nifty IT",
     "chain": None, "step": 50, "minMove": 30, "maxMove": 60, "yahoo": "^CNXIT"},
    {"id": "AUTO", "name": "Nifty Auto", "group": "sector", "key": "NSE_INDEX|Nifty Auto",
     "chain": None, "step": 50, "minMove": 25, "maxMove": 50, "yahoo": "^CNXAUTO"},
    {"id": "PHARMA", "name": "Nifty Pharma", "group": "sector", "key": "NSE_INDEX|Nifty Pharma",
     "chain": None, "step": 20, "minMove": 20, "maxMove": 40, "yahoo": "^CNXPHARMA"},
    {"id": "FMCG", "name": "Nifty FMCG", "group": "sector", "key": "NSE_INDEX|Nifty FMCG",
     "chain": None, "step": 20, "minMove": 40, "maxMove": 80, "yahoo": "^CNXFMCG"},
    {"id": "METAL", "name": "Nifty Metal", "group": "sector", "key": "NSE_INDEX|Nifty Metal",
     "chain": None, "step": 20, "minMove": 15, "maxMove": 30, "yahoo": "^CNXMETAL"},
    {"id": "ENERGY", "name": "Nifty Energy", "group": "sector", "key": "NSE_INDEX|Nifty Energy",
     "chain": None, "step": 20, "minMove": 30, "maxMove": 60, "yahoo": "^CNXENERGY"},
    {"id": "MEDIA", "name": "Nifty Media", "group": "sector", "key": "NSE_INDEX|Nifty Media",
     "chain": None, "step": 10, "minMove": 10, "maxMove": 20, "yahoo": "^CNXMEDIA"},
    {"id": "REALTY", "name": "Nifty Realty", "group": "sector", "key": "NSE_INDEX|Nifty Realty",
     "chain": None, "step": 10, "minMove": 10, "maxMove": 20, "yahoo": "^CNXREALTY"},
    {"id": "INFRA", "name": "Nifty Infra", "group": "sector", "key": "NSE_INDEX|Nifty Infra",
     "chain": None, "step": 20, "minMove": 25, "maxMove": 50, "yahoo": "^CNXINFRA"},
    {"id": "PSUBANK", "name": "Nifty PSU Bank", "group": "sector", "key": "NSE_INDEX|Nifty PSU Bank",
     "chain": None, "step": 10, "minMove": 30, "maxMove": 60, "yahoo": "^CNXPSUBANK"},
    {"id": "NEXT50", "name": "Nifty Next 50", "group": "sector", "key": "NSE_INDEX|Nifty Next 50",
     "chain": None, "step": 25, "minMove": 30, "maxMove": 60, "yahoo": None},
]

FORECAST_PROFILES = {
    "DEFAULT": {"key": "DEFAULT", "name": "standard", "horizon": 12, "stopAtr": 0.5,
                "stopMoveMult": None, "minCapture": 50, "minConviction": 50,
                "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 1.0, "blend": None,
                "auditBars": 100000},
    # 75% profile, measured on six months of that instrument's own 5m bars: 3-hour horizon,
    # stop at 3.4x the minimum-move target, all four indicators (incl. an extreme RSI) aligned,
    # ATR-expected move >=4x the target, entries before 14:30 IST. The wide stop is what lifts
    # T1-before-SL to ~77%; R:R is ~0.29, so the Proof panel shows expectancy next to it.
    "PRECISION": {"key": "PRECISION", "name": "75% precision", "horizon": 36,
                  "stopAtr": 1.0, "stopMoveMult": 3.4,
                  "minCapture": 0, "minConviction": 55, "minTechConviction": 100,
                  "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 0, "blend": None,
                  "unanimity": True, "cooldown": 3, "auditBars": 100000, "minWinRate": 75,
                  "minExpMult": 4, "entryBefore": 870},
    # Previous 65-69% profile, kept for the measured head-to-head in the Proof panel.
    "BALANCED": {"key": "BALANCED", "name": "balanced 69%", "horizon": 30,
                 "stopAtr": 1.0, "stopMoveMult": 2.2,
                 "minCapture": 0, "minConviction": 55, "minTechConviction": 75,
                 "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 0, "blend": None,
                 "unanimity": True, "cooldown": 3, "auditBars": 100000, "minWinRate": 65},
    # Older Sensex-only profile, kept as the fallback when the precision audit slips.
    "SENSEX": {"key": "SENSEX", "name": "sensex-specific", "horizon": 9, "stopAtr": 0.6,
               "stopMoveMult": None, "minCapture": 55, "minConviction": 55,
               "allowedTiming": ("EARLY",), "rrGate": 1.0,
               "blend": {"atr": 0.3, "realizedVol": 0.2, "optionImplied": 0.5},
               "auditBars": 100000},
}

# Nifty and Sensex are audited over six months so the precision profile is judged on
# many regimes, not one hot week. Other segments keep a one-month audit for speed.
AUDIT_DAYS = {"NIFTY": 180, "SENSEX": 180}

_cache = {}
_cache_lock = threading.Lock()
_payload = {"t": 0.0, "data": None}
_lock = threading.Lock()


def cached(name, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[name] = (time.time(), value)
    return value


def _validation(inst_id, profile_key, candles, min_move):
    key = f"val:{inst_id}:{profile_key}:{candles[-1]['t']}:{len(candles)}"
    return cached(key, 60, lambda: analysis.validate_forecast(
        candles, min_move, FORECAST_PROFILES[profile_key]))


def _history(inst, days=30):
    """Upstox 5m history over `days`, chunked (the API caps one request at ~30 days)."""
    path = os.path.join(ROOT, ".candle_cache", f"hist_{inst['id']}_{days}.json")

    def fetch():
        try:
            if time.time() - os.path.getmtime(path) < 6 * 3600:
                with open(path) as fh:
                    rows = json.load(fh)
                if len(rows) > 100:
                    return rows
        except Exception:
            pass
        rows = {}
        today = datetime.date.today()
        for k in range((days + 29) // 30):
            end = today - datetime.timedelta(days=30 * k + 1)
            start = end - datetime.timedelta(days=29)
            try:
                part = UX.candles(inst["key"], "minutes", 5, end.isoformat(), start.isoformat())
            except Exception:
                break
            if not part:
                break
            for c in part:
                rows.setdefault(c["t"], c)
        bars = [rows[t] for t in sorted(rows)]
        if bars:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fh:
                    json.dump(bars, fh)
            except Exception:
                pass
        return bars

    return cached(f"hist:{inst['id']}:{days}", 1800, fetch)


def _audit_candles(inst):
    """Real 5m bars for the walk-forward audit (chart keeps only the recent slice)."""
    rows = {}
    try:
        for c in _history(inst, AUDIT_DAYS.get(inst["id"], 30)):
            rows[c["t"]] = c
    except Exception:
        pass
    for c in _intraday(inst)["rows"]:
        rows[c["t"]] = c
    return [rows[k] for k in sorted(rows)]


def _pick_profile(inst):
    """Nifty/Sensex run the measured 75% profile; its own audit gate then refuses to issue
    a call if the live six-month measurement falls below 75%."""
    if inst["id"] not in ("NIFTY", "SENSEX"):
        return "DEFAULT"
    return "PRECISION" if len(_audit_candles(inst)) >= 1000 else "DEFAULT"


def ist_now():
    return time.gmtime(time.time() + 19800)


def market_open():
    t = ist_now()
    if t.tm_wday >= 5:
        return False
    minutes = t.tm_hour * 60 + t.tm_min
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def _cache_path(inst):
    return os.path.join(ROOT, ".candle_cache", inst["id"] + ".json")


def _load_cached_candles(inst):
    try:
        with open(_cache_path(inst), encoding="utf-8") as fh:
            rows = json.load(fh)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save_cached_candles(inst, rows):
    try:
        os.makedirs(os.path.dirname(_cache_path(inst)), exist_ok=True)
        tmp = _cache_path(inst) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows[-800:], fh)
        os.replace(tmp, _cache_path(inst))
    except Exception:
        pass


def _yahoo_5m(inst):
    if not inst.get("yahoo"):
        return []
    return yahoo.yahoo_chart(inst["yahoo"], "5d", "5m").get("candles") or []


def _intraday(inst):
    def fetch():
        today = datetime.date.today()
        start = today - datetime.timedelta(days=6)
        merged = {}
        try:
            for c in UX.candles(inst["key"], "minutes", 5, today.isoformat(), start.isoformat()):
                merged[c["t"]] = c
        except Exception:
            pass
        try:
            for c in UX.intraday(inst["key"], "minutes", 5):
                merged[c["t"]] = c
        except Exception:
            if not merged:
                raise
        # Upstox rolls the intraday window at midnight IST; keep the completed session
        # from the last successful pull so real candles are not lost overnight.
        for c in _load_cached_candles(inst):
            merged.setdefault(c["t"], c)
        rows = [merged[k] for k in sorted(merged)]
        # Upstox EOD history also lags the just-finished session overnight. Fill only the
        # missing tail from Yahoo's real 5m series (never invented bars) so the signal is
        # not computed on stale candles while the quote is already a session ahead.
        filled = 0
        if rows and inst.get("yahoo"):
            try:
                tail_t = rows[-1]["t"]
                for c in _yahoo_5m(inst):
                    if c["t"] <= tail_t or c["t"] in merged:
                        continue
                    # Yahoo repeats the 15:30 close as a flat extra bar; Upstox closes at 15:25.
                    hm = time.gmtime(c["t"] + 19800)
                    if (hm.tm_hour, hm.tm_min) == (15, 30) and (c["t"] - 300) in merged:
                        continue
                    merged[c["t"]] = dict(c, src="YAHOO")
                    filled += 1
                if filled:
                    rows = [merged[k] for k in sorted(merged)]
            except Exception:
                filled = 0
        # BSE index intraday can freeze before the close: zero-range bars repeating the last
        # value while the quote keeps moving. Those are stale, not prices, so a trailing run
        # of them is replaced with Yahoo's real 5m bar for the same minute (tagged YAHOO).
        if rows and inst.get("yahoo"):
            try:
                ybars = {c["t"]: c for c in _yahoo_5m(inst)}
                stale = []
                k = len(rows) - 1
                while k >= 0 and rows[k]["t"] in ybars:
                    a, y = rows[k], ybars[rows[k]["t"]]
                    flat = a["high"] == a["low"] == a["close"]
                    if not flat or abs(y["close"] - a["close"]) / a["close"] <= 0.0005:
                        break
                    stale.append(k)
                    k -= 1
                for k in stale:
                    merged[rows[k]["t"]] = dict(ybars[rows[k]["t"]], src="YAHOO")
                if stale:
                    rows = [merged[t] for t in sorted(merged)]
            except Exception:
                pass
        if rows:
            _save_cached_candles(inst, rows)
        # tag-based count keeps the provenance label after the filled bars are cached
        yahoo_fill = sum(1 for c in rows[-150:] if c.get("src") == "YAHOO")
        return {"rows": rows, "yahooFill": yahoo_fill}
    return cached(f"intra:{inst['id']}", 20, fetch)


def _daily(inst):
    today = datetime.date.today()
    start = today - datetime.timedelta(days=420)
    return cached(f"daily:{inst['id']}", 1800,
                  lambda: UX.candles(inst["key"], "days", 1, today.isoformat(), start.isoformat()))


def _expiries(chain_key):
    return cached(f"exp:{chain_key}", 1800, lambda: UX.expiries(chain_key))


def _chain(chain_key, expiry):
    return cached(f"chain:{chain_key}:{expiry}", 15,
                  lambda: UX.chain_as_records(chain_key, expiry))


def _build_one(inst, quotes, quote_error):
    errors = []
    q = quotes.get(inst["key"]) if quotes else None
    candles, timeframe, candle_src, daily = [], "5m", "ERROR", []
    yahoo_fill = 0

    try:
        intra = _intraday(inst)
        yahoo_fill = intra.get("yahooFill") or 0
        if len(intra["rows"]) >= 30:
            candles, timeframe = intra["rows"][-400:], "5m"
        if candles:
            candle_src = "UPSTOX+YAHOO" if yahoo_fill else "UPSTOX"
        else:
            errors.append("Upstox returned no candles")
    except Exception as exc:
        errors.append(f"Upstox candles: {exc}")

    try:
        daily = _daily(inst)
    except Exception:
        daily = []
    if not candles and daily:
        candles, timeframe = daily[-200:], "1D"
        candle_src = "UPSTOX"

    if not candles and inst.get("yahoo"):
        try:
            y = yahoo.yahoo_chart(inst["yahoo"], "6mo", "1d")
            if y.get("candles"):
                candles, daily = y["candles"][-200:], y["candles"]
                timeframe, candle_src = "1D", "YAHOO"
            if y.get("spot") and not q:
                q = {"last": y["spot"], "netChange": None, "prevClose": None, "quoteAt": None}
        except Exception as exc:
            errors.append(f"Yahoo fallback: {exc}")

    spot = (q or {}).get("last")
    if spot is None and candles:
        spot = candles[-1]["close"]
    change_pct = None
    if q and q.get("prevClose") and spot:
        change_pct = round((spot / q["prevClose"] - 1) * 100, 2)
    elif len(daily) >= 2:
        change_pct = round((daily[-1]["close"] / daily[-2]["close"] - 1) * 100, 2)

    options, opt_src, opt_error = None, "N/A", None
    if inst.get("chain"):
        try:
            exps = _expiries(inst["chain"])
            if not exps:
                raise RuntimeError("no future expiry listed")
            recs = _chain(inst["chain"], exps[0])
            options = analysis.analyze_chain(recs, spot, inst["step"]) if spot else None
            opt_src = "UPSTOX" if options else "ERROR"
            if not options:
                opt_error = "option chain returned no usable rows"
        except Exception as exc:
            opt_src, opt_error = "ERROR", f"Upstox options: {exc}"

    signal = None
    if candles and spot and len(candles) >= 40:
        signal = analysis.build_signal(spot, candles, options, timeframe)
    forecast = None
    if signal:
        profile_key = "DEFAULT"
        profile_audit = None
        if inst["id"] in ("NIFTY", "SENSEX") and timeframe == "5m" and len(candles) >= 80:
            profile_key = _pick_profile(inst)
            profile_audit = _validation(inst["id"], profile_key,
                                        _audit_candles(inst), inst["minMove"])
        forecast = analysis.build_forecast(spot, candles, options, signal, timeframe,
                                           inst["minMove"], inst["maxMove"],
                                           params=FORECAST_PROFILES[profile_key],
                                           audit=profile_audit)

    return {
        "id": inst["id"], "name": inst["name"], "group": inst["group"],
        "spot": round(spot, 2) if spot else None,
        "changePct": change_pct,
        "timeframe": timeframe,
        "minMove": inst["minMove"], "maxMove": inst["maxMove"],
        "lastQuoteAt": (q or {}).get("quoteAt"),
        "source": {
            "quote": "UPSTOX" if (quotes and inst["key"] in quotes) else ("YAHOO" if q else "ERROR"),
            "candles": candle_src, "options": opt_src,
        },
        "error": "; ".join(errors) if errors else (quote_error and f"quote: {quote_error}" or None),
        "optionsError": opt_error,
        "candles": candles,
        "options": options,
        "signal": signal,
        "forecast": forecast,
    }


def build_market(force=False):
    global _payload
    with _lock:
        if not force and _payload["data"] and time.time() - _payload["t"] < 2.5:
            return _payload["data"]

        quotes, quote_error, upstox_ok = {}, None, True
        try:
            quotes = cached("quotes", 2.5, lambda: UX.quotes([i["key"] for i in INSTRUMENTS]))
            if not quotes:
                raise RuntimeError("empty quote response")
        except Exception as exc:
            upstox_ok, quote_error = False, str(exc)

        with ThreadPoolExecutor(max_workers=4) as pool:
            items = list(pool.map(lambda i: _build_one(i, quotes, quote_error), INSTRUMENTS))

        token_left = upstox.token_seconds_left(TOKEN)
        notes = []
        if not upstox_ok:
            notes.append(f"UPSTOX FEED DOWN: {quote_error[:200]}. Charts fall back to Yahoo real data; "
                         "option/OI panels are disabled. Refresh the token with: python upstox_login.py")
        elif token_left is not None and token_left < 1800:
            notes.append(f"Upstox access token expires in {max(token_left // 60, 0)} min - "
                         "run: python upstox_login.py")
        opt_fail = [i["id"] for i in items if i["source"]["options"] == "ERROR"]
        if opt_fail:
            notes.append("Option chain failed for: " + ", ".join(opt_fail))
        candle_fail = [i["id"] for i in items if i["source"]["candles"] == "ERROR"]
        if candle_fail:
            notes.append("No real candles for: " + ", ".join(candle_fail))
        filled = [i["id"] for i in items if i["source"]["candles"] == "UPSTOX+YAHOO"]
        if filled:
            notes.append("Overnight gap: Upstox EOD history lags the just-finished session, so the missing "
                         "bars for " + ", ".join(filled) + " come from Yahoo's real 5m series "
                         "(labelled UPSTOX+YAHOO). Every bar is real; nothing is synthesised.")

        _payload = {"t": time.time(), "data": {
            "generatedAt": int(time.time()),
            "istClock": time.strftime("%Y-%m-%d %H:%M:%S", ist_now()),
            "marketOpen": market_open(),
            "feeds": {
                "upstox": {"online": upstox_ok, "tokenLeft": token_left, "error": quote_error},
            },
            "feedNotes": notes,
            "instruments": items,
        }}
        return _payload["data"]


def build_research(segment_ids):
    data = build_market(force=True)
    items = [i for i in data["instruments"] if not segment_ids or i["id"] in segment_ids]

    def conf(i):
        return i["signal"]["confidence"] if i["signal"] else -1

    def is_qualified(i):
        return bool((i.get("forecast") or {}).get("qualified"))

    ranked = sorted(items, key=lambda i: (is_qualified(i), conf(i)), reverse=True)
    actionable = [i for i in ranked if i["signal"] and i["signal"]["direction"] != "NEUTRAL"]
    qualified = [i for i in actionable if is_qualified(i)]
    bullish = [i for i in actionable if i["signal"]["direction"] == "BULLISH"]
    bearish = [i for i in actionable if i["signal"]["direction"] == "BEARISH"]
    neutral = [i for i in items if not i["signal"] or i["signal"]["direction"] == "NEUTRAL"]
    top = qualified[0] if qualified else None
    nearest = actionable[0] if actionable else None

    rows = []
    for i in ranked:
        s = i["signal"]
        o = i.get("options") or {}
        f = i.get("forecast") or {}
        rows.append({
            "id": i["id"], "name": i["name"], "group": i["group"],
            "spot": i["spot"], "changePct": i["changePct"],
            "direction": s["direction"] if s else "ERROR",
            "confidence": s["confidence"] if s else None,
            "entry": s["entry"] if s else None, "stop": s["stop"] if s else None,
            "target1": s["target1"] if s else None, "target2": s["target2"] if s else None,
            "hitRate": (s.get("hitRate") or {}).get("hitRate") if s else None,
            "pcr": o.get("pcr"), "maxPain": o.get("maxPain"), "oiFlow": o.get("oiFlow"),
            "iv": o.get("iv"), "timeframe": i["timeframe"],
            "expectedMove": f.get("expectedMove"), "minMove": f.get("minMove"),
            "maxMove": f.get("maxMove"), "captureProb": f.get("captureProb"),
            "timing": f.get("timing"), "qualified": bool(f.get("qualified")),
            "forecastStatus": f.get("status"), "plan": f.get("plan"),
            "horizon": f.get("horizonLabel"), "reasons": f.get("reasons", [])[:4],
            "drivers": [f"{x['label']}: {'BUY' if x['vote'] > 0 else 'SELL' if x['vote'] < 0 else 'flat'}"
                        for x in (s["strategies"] if s else []) if x["weight"]][:3],
            "source": i["source"], "error": i["error"], "optionsError": i["optionsError"],
        })

    breadth = "bullish" if len(bullish) > len(bearish) else (
        "bearish" if len(bearish) > len(bullish) else "mixed")

    if top:
        fs = top["forecast"]
        plan = fs.get("plan") or {}
        verdict = (
            f"{len(qualified)} of {len(items)} segments clear the minimum pre-move threshold "
            f"(Nifty 25 / Sensex 40 pts; {fs['horizonLabel']}). Highest-conviction qualifying call: "
            f"{top['name']} {top['signal']['direction']} at {top['signal']['confidence']}% — expected "
            f"±{fs['expectedMove']:.0f} pts vs threshold {fs['minMove']}–{fs['maxMove']}, "
            f"{fs['timing'].lower()} timing, historical capture {fs['captureProb']}%"
            + (f" of {fs['captureSamples']} cases" if fs.get("captureSamples") else "")
            + f". Entry {plan.get('entry')}, SL {plan.get('stop')}, "
            f"T1 {plan.get('target1')} ({plan.get('target1Move')} pts), "
            f"T2 {plan.get('target2')} ({plan.get('target2Move')} pts), "
            f"T3 {plan.get('target3')} ({plan.get('target3Move')} pts).")
        o = top.get("options") or {}
        if o.get("pcr") is not None:
            verdict += f" OI context: PCR {o['pcr']}, max pain {int(o['maxPain']) if o.get('maxPain') else '—'}."
        if top["signal"]["direction"].lower() != breadth:
            verdict += " Breadth and the strongest qualifying call disagree — size accordingly."
    elif nearest:
        f = nearest.get("forecast") or {}
        verdict = (
            f"No qualifying pre-move opportunity across {len(items)} scanned segments. "
            f"{len(actionable)} directional signal(s), but none clears every gate (expected move ≥4× the "
            f"target, all indicators agreeing, entry before 14:30 IST, profile audit and pre-move timing). "
            f"Nearest: {nearest['name']} {nearest['signal']['direction']} "
            f"at {nearest['signal']['confidence']}% — expected ±{f.get('expectedMove')} pts vs minimum "
            f"{f.get('minMove')} ({f.get('status')}, {str(f.get('timing', '')).lower()} timing). "
            "No trade is the correct output; wait for compression + a fresh trigger.")
    else:
        missing = [i for i in items if not i["signal"]]
        if items and len(missing) == len(items):
            verdict = (f"No real market data across {len(items)} scanned segments — no prediction is "
                       "possible and no numbers are invented. Fix the feed (Upstox token: "
                       "python upstox_login.py), then rescan.")
        else:
            verdict = (f"No actionable conviction across {len(items)} scanned segments — all inside the "
                       "neutral band. Wait for a break of the option OI range before taking a side.")

    return {
        "generatedAt": int(time.time()),
        "istClock": time.strftime("%Y-%m-%d %H:%M:%S", ist_now()),
        "feeds": data["feeds"],
        "feedNotes": data["feedNotes"],
        "scanned": len(items),
        "qualifiedCount": len(qualified),
        "breadth": {"bullish": len(bullish), "bearish": len(bearish),
                    "neutral": len(neutral)},
        "verdict": verdict,
        "topPick": next((r for r in rows if r["id"] == top["id"]), None) if top else None,
        "nearest": next((r for r in rows if r["id"] == nearest["id"]), None) if (nearest and not top) else None,
        "rows": rows,
    }


def build_proof():
    """Evidence page: feed provenance + walk-forward audit of the current forecast profiles."""
    data = build_market(force=True)
    items = {i["id"]: i for i in data["instruments"]}
    provenance, validation = [], {}
    for iid in ("NIFTY", "SENSEX"):
        it = items[iid]
        f = it.get("forecast") or {}
        last = it["candles"][-1] if it["candles"] else None
        provenance.append({
            "id": iid, "name": it["name"],
            "quote": it["source"]["quote"], "candles": it["source"]["candles"],
            "options": it["source"]["options"], "spot": it["spot"],
            "changePct": it["changePct"], "timeframe": it["timeframe"],
            "bars": len(it["candles"]), "lastBar": last["t"] if last else None,
            "lastClose": last["close"] if last else None,
            "quoteAt": it["lastQuoteAt"],
            "optionExpiry": (it["options"] or {}).get("expiry"),
            "forecastStatus": f.get("status"), "expectedMove": f.get("expectedMove"),
            "profile": f.get("profile"), "minMove": it["minMove"],
        })
        if it["candles"] and it["timeframe"] == "5m":
            inst = next(x for x in INSTRUMENTS if x["id"] == iid)
            audit = _audit_candles(inst)
            active_key = f.get("profileKey") or ("SENSEX" if iid == "SENSEX" else "DEFAULT")
            validation[iid] = {"active": _validation(iid, active_key, audit, it["minMove"]),
                               "activeProfile": f.get("profile"),
                               "auditBars": len(audit)}
            if iid in ("NIFTY", "SENSEX"):
                validation[iid]["selected"] = active_key
                validation[iid]["comparison"] = {
                    key: _validation(iid, key, audit, it["minMove"])
                    for key in ("PRECISION", "BALANCED")}

    return {
        "generatedAt": int(time.time()),
        "istClock": time.strftime("%Y-%m-%d %H:%M:%S", ist_now()),
        "feeds": data["feeds"],
        "provenance": provenance,
        "validation": validation,
        "method": {
            "candles": "Upstox v3 5-minute candles. Nifty and Sensex audits use six months "
                       "(~9,300 bars, fetched in 30-day chunks); other segments use one month. "
                       "When Upstox EOD history lags the just-finished session, the missing tail is "
                       "filled from Yahoo's real 5m series and labelled UPSTOX+YAHOO.",
            "noLookahead": "At historical bar i the audit only uses bars <= i; the outcome uses bars i+1..i+horizon. "
                           "One trade at a time with a 15-minute cooldown, exactly like the profile.",
            "profileGate": "A 75% call requires EMA 9/21, MACD, Supertrend and an extreme RSI to all agree "
                           "(conviction 100% incl. RSI), ATR-expected move >=4x the minimum target, entry "
                           "before 14:30 IST, and this audit to still measure >=75% T1-before-SL. Otherwise "
                           "the desk returns the blocking reason and no call — it never falls back to a "
                           "lower-hit-rate profile just to have something to say.",
            "outcome": "T1 (minimum move) hit before stop within the horizon. If both levels are touched in "
                       "one candle, the stop is counted first (conservative). 'Reach' separately counts the "
                       "minimum move being touched at any point in the horizon, even after a stop.",
            "optionHistory": "Historical option chains are not backtested (only the current chain is available); "
                             "the option-implied move affects live forecasts only.",
            "reproduce": "python verify.py  — independently pulls Upstox and diffs it against this dashboard. "
                         "python research_opt.py --fetch  — re-runs the six-month profile search.",
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/market":
            try:
                body = json.dumps(build_market("1" in qs.get("force", []))).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return
        if parsed.path == "/api/research":
            ids = [s for s in (qs.get("segments", [""])[0]).split(",") if s]
            try:
                body = json.dumps(build_research(ids)).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if parsed.path == "/api/proof":
            try:
                body = json.dumps(build_proof()).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        rel = "index.html" if parsed.path == "/" else urllib.parse.unquote(parsed.path.lstrip("/"))
        safe = os.path.normpath(os.path.join(PUBLIC, rel))
        if not safe.startswith(PUBLIC) or not os.path.isfile(safe):
            self._send(404, b"Not found", "text/plain")
            return
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon"}.get(
                     os.path.splitext(safe)[1], "text/plain")
        with open(safe, "rb") as f:
            self._send(200, f.read(), ctype)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Market Signal Desk running at  http://localhost:{PORT}")
    left = upstox.token_seconds_left(TOKEN)
    print(f"Upstox token: {'missing' if not TOKEN else (f'valid ~{left // 3600}h {(left % 3600) // 60}m' if left and left > 0 else 'EXPIRED')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
