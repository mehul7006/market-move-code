"""Market Signal Desk - local dashboard server.

Data policy: Upstox live feed first, Yahoo real candles as fallback, and a hard ERROR
state when neither is reachable. There is no synthetic/mock data path.
"""
from __future__ import annotations

import base64
import csv
import datetime
import hmac
import io
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analysis
import data_sources as yahoo
import desk_env
import nifty50
import overnight
import shortterm
import upstox

PORT = int(os.environ.get("PORT", "8787"))
ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
# Where downloaded history is cached. On Render point CACHE_DIR at the persistent disk (e.g. /data/cache).
CACHE_DIR = os.environ.get("CACHE_DIR") or os.path.join(ROOT, ".candle_cache")
# LITE_MODE=1 is for small hosts (Render free tier: 0.1 CPU, 512 MB, sleeps when idle). Requests never wait for a heavy
# build (they get a 202 "warming up" and the page retries), the background refresh only runs while someone is looking and
# slows to what the CPU can sustain, and the 6-month scorecard is served from a snapshot instead of being recomputed.
LITE = os.environ.get("LITE_MODE") == "1"
_BOOT = time.time()
_last_request = time.time()
_cycle = {"sec": 3}

ENV = desk_env.current()
TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN") or ENV.get("UPSTOX_ACCESS_TOKEN", "")
ANALYTICS_TOKEN = os.environ.get("UPSTOX_ANALYTICS_TOKEN") or ENV.get("UPSTOX_ANALYTICS_TOKEN", "")
UX = upstox.UpstoxClient(TOKEN, ANALYTICS_TOKEN or None)
_token_lock = threading.Lock()

# Nifty 50 stock book: additive to the six index segments. Quotes and breadth answer every
# request; the per-stock six-month audits run in a rate-limited background worker.
NIFTY50 = nifty50.Desk(UX, ROOT)

INSTRUMENTS = [
    {"id": "SENSEX", "name": "BSE Sensex", "group": "prime", "key": "BSE_INDEX|SENSEX",
     "chain": "BSE_INDEX|SENSEX", "step": 100, "minMove": 40, "maxMove": 80,
     "yahoo": "^BSESN", "tradeable": True},
    {"id": "NIFTY", "name": "Nifty 50", "group": "prime", "key": "NSE_INDEX|Nifty 50",
     "chain": "NSE_INDEX|Nifty 50", "step": 50, "minMove": 25, "maxMove": 50,
     "yahoo": "^NSEI", "tradeable": True},
    {"id": "BANKNIFTY", "name": "Nifty Bank", "group": "prime", "key": "NSE_INDEX|Nifty Bank",
     "chain": "NSE_INDEX|Nifty Bank", "step": 100, "minMove": 50, "maxMove": 100,
     "yahoo": "^NSEBANK", "tradeable": True},
    {"id": "FINNIFTY", "name": "Nifty Fin Service", "group": "prime", "key": "NSE_INDEX|Nifty Fin Service",
     "chain": "NSE_INDEX|Nifty Fin Service", "step": 100, "minMove": 30, "maxMove": 60,
     "yahoo": None, "tradeable": True},
    {"id": "MIDCPNIFTY", "name": "Nifty Midcap Select", "group": "prime", "key": "NSE_INDEX|NIFTY MID SELECT",
     "chain": "NSE_INDEX|NIFTY MID SELECT", "step": 25, "minMove": 20, "maxMove": 40,
     "yahoo": None, "tradeable": True},
    {"id": "NEXT50", "name": "Nifty Next 50", "group": "prime", "key": "NSE_INDEX|Nifty Next 50",
     "chain": None, "step": 25, "minMove": 60, "maxMove": 120,
     "yahoo": None, "tradeable": True},
    {"id": "IT", "name": "Nifty IT", "group": "sector", "key": "NSE_INDEX|Nifty IT",
     "chain": None, "step": 50, "minMove": 30, "maxMove": 60, "yahoo": "^CNXIT",
     "tradeable": False},
    {"id": "AUTO", "name": "Nifty Auto", "group": "sector", "key": "NSE_INDEX|Nifty Auto",
     "chain": None, "step": 50, "minMove": 25, "maxMove": 50, "yahoo": "^CNXAUTO",
     "tradeable": False},
    {"id": "PHARMA", "name": "Nifty Pharma", "group": "sector", "key": "NSE_INDEX|Nifty Pharma",
     "chain": None, "step": 20, "minMove": 20, "maxMove": 40, "yahoo": "^CNXPHARMA",
     "tradeable": False},
    {"id": "FMCG", "name": "Nifty FMCG", "group": "sector", "key": "NSE_INDEX|Nifty FMCG",
     "chain": None, "step": 20, "minMove": 40, "maxMove": 80, "yahoo": "^CNXFMCG",
     "tradeable": False},
    {"id": "METAL", "name": "Nifty Metal", "group": "sector", "key": "NSE_INDEX|Nifty Metal",
     "chain": None, "step": 20, "minMove": 15, "maxMove": 30, "yahoo": "^CNXMETAL",
     "tradeable": False},
    {"id": "ENERGY", "name": "Nifty Energy", "group": "sector", "key": "NSE_INDEX|Nifty Energy",
     "chain": None, "step": 20, "minMove": 30, "maxMove": 60, "yahoo": "^CNXENERGY",
     "tradeable": False},
    {"id": "MEDIA", "name": "Nifty Media", "group": "sector", "key": "NSE_INDEX|Nifty Media",
     "chain": None, "step": 10, "minMove": 10, "maxMove": 20, "yahoo": "^CNXMEDIA",
     "tradeable": False},
    {"id": "REALTY", "name": "Nifty Realty", "group": "sector", "key": "NSE_INDEX|Nifty Realty",
     "chain": None, "step": 10, "minMove": 10, "maxMove": 20, "yahoo": "^CNXREALTY",
     "tradeable": False},
    {"id": "INFRA", "name": "Nifty Infra", "group": "sector", "key": "NSE_INDEX|Nifty Infra",
     "chain": None, "step": 20, "minMove": 25, "maxMove": 50, "yahoo": "^CNXINFRA",
     "tradeable": False},
    {"id": "PSUBANK", "name": "Nifty PSU Bank", "group": "sector", "key": "NSE_INDEX|Nifty PSU Bank",
     "chain": None, "step": 10, "minMove": 30, "maxMove": 60, "yahoo": "^CNXPSUBANK",
     "tradeable": False},
]

# Six index segments only. Each profile was measured on six months of that segment's own
# 5m bars with session-bounded outcomes (closed at 15:25 IST), a wide stop and
# unanimous indicators. The audit gate below refuses a call when the live measurement
# drops under 75%. The wide stop means low R:R (~0.25-0.4) - see the Proof panel for
# average R and break-even, not just the hit rate.
FORECAST_PROFILES = {
    "PREC_SENSEX": {"key": "PREC_SENSEX", "name": "Sensex precision", "horizon": 54,
                    "stopAtr": 1.0, "stopMoveMult": 3.0, "minCapture": 0,
                    "minConviction": 55, "minTechConviction": 100,
                    "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 0, "blend": None,
                    "unanimity": True, "cooldown": 3, "auditBars": 100000, "minWinRate": 75,
                    "minExpMult": 4, "entryBefore": 870, "sessionOnly": True,
                    "requireEma50": True, "requireRsiExtreme": True},
    "PREC_NIFTY": {"key": "PREC_NIFTY", "name": "Nifty precision", "horizon": 54,
                   "stopAtr": 1.0, "stopMoveMult": 2.5, "minCapture": 55,
                   "minConviction": 55, "minTechConviction": 100,
                   "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 0, "blend": None,
                   "unanimity": True, "cooldown": 3, "auditBars": 100000, "minWinRate": 75,
                   "minExpMult": 4, "entryBefore": 840, "sessionOnly": True,
                   "requireEma50": True, "requireRsiExtreme": False},
    "PREC_BANKNIFTY": {"key": "PREC_BANKNIFTY", "name": "Bank Nifty precision", "horizon": 54,
                       "stopAtr": 1.0, "stopMoveMult": 4.0, "minCapture": 55,
                       "minConviction": 55, "minTechConviction": 100,
                       "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 0, "blend": None,
                       "unanimity": True, "cooldown": 3, "auditBars": 100000, "minWinRate": 75,
                       "minExpMult": 4, "entryBefore": 840, "sessionOnly": True,
                       "requireEma50": False, "requireRsiExtreme": False},
    "PREC_FINNIFTY": {"key": "PREC_FINNIFTY", "name": "Fin Nifty precision", "horizon": 54,
                      "stopAtr": 1.0, "stopMoveMult": 2.5, "minCapture": 55,
                      "minConviction": 55, "minTechConviction": 100,
                      "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 0, "blend": None,
                      "unanimity": True, "cooldown": 3, "auditBars": 100000, "minWinRate": 75,
                      "minExpMult": 4, "entryBefore": 840, "sessionOnly": True,
                      "requireEma50": False, "requireRsiExtreme": False},
    "PREC_MIDCPNIFTY": {"key": "PREC_MIDCPNIFTY", "name": "Midcap Select precision", "horizon": 54,
                        "stopAtr": 1.0, "stopMoveMult": 4.0, "minCapture": 0,
                        "minConviction": 55, "minTechConviction": 100,
                        "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 0, "blend": None,
                        "unanimity": True, "cooldown": 3, "auditBars": 100000, "minWinRate": 75,
                        "minExpMult": 4, "entryBefore": 870, "sessionOnly": True,
                        "requireEma50": False, "requireRsiExtreme": True},
    "PREC_NEXT50": {"key": "PREC_NEXT50", "name": "Next 50 precision", "horizon": 45,
                    "stopAtr": 1.0, "stopMoveMult": 4.0, "minCapture": 55,
                    "minConviction": 55, "minTechConviction": 100,
                    "allowedTiming": ("EARLY", "BUILDING"), "rrGate": 0, "blend": None,
                    "unanimity": True, "cooldown": 3, "auditBars": 100000, "minWinRate": 75,
                    "minExpMult": 5, "entryBefore": 870, "sessionOnly": True,
                    "requireEma50": True, "requireRsiExtreme": False},
}

PROFILE_BY_ID = {"SENSEX": "PREC_SENSEX", "NIFTY": "PREC_NIFTY",
                 "BANKNIFTY": "PREC_BANKNIFTY", "FINNIFTY": "PREC_FINNIFTY",
                 "MIDCPNIFTY": "PREC_MIDCPNIFTY", "NEXT50": "PREC_NEXT50"}
TRADEABLE_IDS = [i["id"] for i in INSTRUMENTS if i.get("tradeable")]

# Every tradeable segment is audited over six months so the hit rate is judged across
# many regimes, not one hot week.
AUDIT_DAYS = {iid: 180 for iid in TRADEABLE_IDS}

# Next-day model: a separate long-only overnight book, measured on ~3 years of daily bars.
OVERNIGHT_CALENDAR_DAYS = 1150
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

_cache = {}
_cache_lock = threading.Lock()
_payload = {"t": 0.0, "data": None}
_lock = threading.Lock()


# How long data is reused before Upstox is asked again (seconds). Live touches do not need fast candle calls: the latest traded price is folded into the
# forming 1-minute candle (see _inject_ltp), so quotes stay fast and everything else is slow. Override with environment variables if needed.
def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


TRADED_IDS = ("NIFTY", "BANKNIFTY", "SENSEX")
QUOTE_TTL = _envf("UPSTOX_TTL_QUOTES", 5)               # one batched call for every index
INTRA1M_TTL = _envf("UPSTOX_TTL_INTRA1M", 20)           # 1-minute candles of the three traded indexes
INTRA5M_TTL = _envf("UPSTOX_TTL_INTRA5M", 30)           # 5-minute candles of the traded indexes
INTRA5M_CONTEXT_TTL = _envf("UPSTOX_TTL_INTRA5M_CONTEXT", 180)   # sector / other indexes (chart context only)
CHAIN_TTL = _envf("UPSTOX_TTL_CHAIN", 30)               # option chain (OI, greeks)
FUT_TTL = _envf("UPSTOX_TTL_FUT", 30)                   # futures volume / OI
REFRESH_OPEN_SEC = _envf("UPSTOX_REFRESH_OPEN", 6)      # background refresh while the market is open
REFRESH_CLOSED_SEC = _envf("UPSTOX_REFRESH_CLOSED", 30)   # and when it is closed


def cached(name, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        value = fn()
    except Exception:
        if hit:                       # the call failed or was held back (rate limit / budget): keep showing the last good data
            return hit[1]
        raise
    with _cache_lock:
        _cache[name] = (time.time(), value)
    return value


def _validation(inst_id, profile_key, candles, min_move):
    key = f"val:{inst_id}:{profile_key}:{candles[-1]['t']}:{len(candles)}"
    return cached(key, 60, lambda: analysis.validate_forecast(
        candles, min_move, FORECAST_PROFILES[profile_key]))


def _recent_validation(inst_id, profile_key, candles, min_move, days=30):
    """Same audit on just the last `days` calendar days, with T2/T3-before-stop rates."""
    last = candles[-1]["t"]
    key = f"recent:{inst_id}:{profile_key}:{last}:{len(candles)}:{days}"
    return cached(key, 60, lambda: analysis.validate_forecast(
        candles, min_move, FORECAST_PROFILES[profile_key],
        since_ts=last - days * 86400))


def _recent_all(inst_id, profile_key, candles, min_move, days=30):
    """Every call of the same 30-day audit, with all three targets, exit and R."""
    last = candles[-1]["t"]
    key = f"recent-all:{inst_id}:{profile_key}:{last}:{len(candles)}:{days}"
    return cached(key, 60, lambda: analysis.validate_forecast(
        candles, min_move, FORECAST_PROFILES[profile_key],
        since_ts=last - days * 86400, examples_limit=None))


def _call_totals(rows):
    # Mutually exclusive: each call is counted once, at the highest target hit before the stop.
    stats = {"calls": len(rows),
             "t1": sum(1 for r in rows if r["outcome"] == "t1" and (r.get("targetsHit") or 0) == 1),
             "t2": sum(1 for r in rows if r["outcome"] == "t1" and (r.get("targetsHit") or 0) == 2),
             "t3": sum(1 for r in rows if r["outcome"] == "t1" and (r.get("targetsHit") or 0) >= 3),
             "stops": sum(1 for r in rows if r["outcome"] == "stop"),
             "timeouts": sum(1 for r in rows if r["outcome"] == "timeout")}
    closed = [r for r in rows if r.get("r") is not None]
    stats["avgR"] = round(sum(r["r"] for r in closed) / len(closed), 3) if closed else None
    anyt = stats["t1"] + stats["t2"] + stats["t3"]
    stats["anyTargetRate"] = round(anyt / stats["calls"] * 100, 1) if stats["calls"] else None
    stats["slRate"] = round(stats["stops"] / stats["calls"] * 100, 1) if stats["calls"] else None
    stats["winRate"] = round(stats["t1"] / stats["calls"] * 100, 1) if stats["calls"] else None
    stats["t2Rate"] = round(stats["t2"] / stats["calls"] * 100, 1) if stats["calls"] else None
    stats["t3Rate"] = round(stats["t3"] / stats["calls"] * 100, 1) if stats["calls"] else None
    return stats


def filter_calls(res, name="all"):
    """Scope the call list to T1/T2/T3-before-stop, stops or timeouts."""
    name = (name or "all").lower()
    if name == "all":
        res["filter"] = "all"
        res["filteredFrom"] = len(res["calls"])
        return res
    preds = {"t1": lambda r: r["outcome"] == "t1" and (r.get("targetsHit") or 0) == 1,
             "t2": lambda r: r["outcome"] == "t1" and (r.get("targetsHit") or 0) == 2,
             "t3": lambda r: r["outcome"] == "t1" and (r.get("targetsHit") or 0) >= 3,
             "sl": lambda r: r["outcome"] == "stop",
             "to": lambda r: r["outcome"] == "timeout"}
    pred = preds.get(name)
    if pred is None:
        return filter_calls(res, "all")
    rows = [r for r in res["calls"] if pred(r)]
    out = dict(res)
    out["calls"] = rows
    out["totals"] = _call_totals(rows)
    out["filter"] = name
    out["filteredFrom"] = len(res["calls"])
    return out


def _auth_config():
    env = desk_env.current()
    return env.get("DESK_USER") or "", env.get("DESK_PASS") or "", env.get("DESK_ADMIN_KEY") or ""


def _sync_token_from_env():
    """Pick up a token written by upstox_login.py or the Access panel without a restart."""
    env_now = desk_env.current()
    atok = env_now.get("UPSTOX_ANALYTICS_TOKEN") or ""
    if atok and atok != UX.fallback:
        UX.fallback = atok
    tok = env_now.get("UPSTOX_ACCESS_TOKEN") or ""
    if tok and tok != UX.token:
        with _token_lock:
            UX.set_primary(tok)
            with _cache_lock:
                _cache.clear()
            _payload.update({"t": 0.0, "data": None})
        globals()["TOKEN"] = tok


def friendly_feed_error(err):
    """One calm sentence instead of Upstox's raw JSON when the token is the problem."""
    t = str(err or "")
    if "401" in t or "UDAPI100050" in t or "Invalid token" in t or "expired" in t.lower():
        return ("Upstox access token expired or invalid - open Access and paste a fresh token (or a 1-year read-only Analytics token). "
                "The last data stays on screen meanwhile.")
    if "rate limit" in t.lower() or "cooling down" in t or "budget" in t:
        return "Upstox calls are paused for a moment to stay under its rate limit - the last data stays on screen."
    return t[:200]


def _expires_label(seconds):
    if seconds is None:
        return None
    if seconds <= 0:
        return "expired"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _feed_probe():
    """One cheap real quote, so the Access panel can prove the new token fetches data."""
    try:
        q = cached("quotes", QUOTE_TTL, lambda: UX.quotes([i["key"] for i in INSTRUMENTS]))
        nifty = q.get("NSE_INDEX|Nifty 50") or {}
        return {"online": True, "nifty": nifty.get("last"), "error": None}
    except Exception as exc:
        return {"online": False, "nifty": None, "error": str(exc)[:220]}


_ngrok = {"t": 0.0, "url": None}


def _ngrok_url():
    """Public tunnel URL if share.py/ngrok is running locally (4040 API)."""
    if time.time() - _ngrok["t"] < 10:
        return _ngrok["url"]
    url = None
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=0.7) as resp:
            for t in (json.loads(resp.read().decode("utf-8")) or {}).get("tunnels", []):
                if str(t.get("public_url", "")).startswith("https://"):
                    url = t["public_url"]
                    break
    except Exception:
        url = None
    _ngrok.update({"t": time.time(), "url": url})
    return url


def _is_analytics(token):
    """Upstox's long-lived read-only token carries "isExtended": true in its payload."""
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return bool(json.loads(base64.urlsafe_b64decode(p)).get("isExtended"))
    except Exception:
        return False


def token_status():
    with _token_lock:
        token = UX.token
    left = upstox.token_seconds_left(token) if token else None
    a_left = upstox.token_seconds_left(UX.fallback) if UX.fallback else None
    primary_ok = bool(token) and (left is None or left > 0) and not upstox.GOVERNOR.token_blocked(token)
    user, password, admin_key = _auth_config()
    return {
        "configured": bool(token) or bool(UX.fallback),
        "expiresInSec": left,
        "expiresLabel": _expires_label(left),
        "dailyToken": {"configured": bool(token), "valid": primary_ok, "expiresLabel": _expires_label(left)},
        "analyticsToken": {"configured": bool(UX.fallback), "expiresLabel": _expires_label(a_left), "inUse": bool(UX.using_fallback or (UX.fallback and not primary_ok)),
                           "switches": UX.switches},
        "feed": _feed_probe(),
        "publicUrl": _ngrok_url(),
        "siteLogin": bool(user and password),
        "adminKeyConfigured": bool(admin_key),
        "serverTimeIst": time.strftime("%Y-%m-%d %H:%M:%S", ist_now()),
    }


def _extract_token(raw):
    """Accept an access token, a `Bearer ...` header value, or the full redirect URL."""
    raw = (raw or "").strip().strip('"').strip("'")
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        code = (urllib.parse.parse_qs(parsed.query).get("code") or [""])[0]
        if not code:
            raise ValueError("that URL has no code= parameter. Paste the access token, or the "
                             "full address you were redirected to after logging in.")
        env = desk_env.current()
        key, secret = env.get("UPSTOX_API_KEY"), env.get("UPSTOX_API_SECRET")
        if not (key and secret):
            raise ValueError("exchanging a login code needs UPSTOX_API_KEY and UPSTOX_API_SECRET "
                             "in .env; otherwise paste the access token itself")
        redirect = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
        return upstox.exchange_code(code, redirect, key, secret)
    if len(raw) < 60 or raw.count(".") < 2:
        raise ValueError("that does not look like an Upstox access token (JWT)")
    return raw


def apply_analytics_token(token):
    """Save Upstox's long-lived read-only Analytics token as the automatic fallback (the daily token, if any, stays the primary)."""
    left = upstox.token_seconds_left(token)
    if left is not None and left <= 0:
        raise ValueError("that Analytics token is already expired; generate a fresh one")
    probe = upstox.UpstoxClient(token)
    probe.quotes([INSTRUMENTS[0]["key"]])            # proves it can read market data
    desk_env.write_var("UPSTOX_ANALYTICS_TOKEN", token)
    with _token_lock:
        UX.fallback = token
    upstox.GOVERNOR.reset()
    with _cache_lock:
        _cache.clear()
    _payload.update({"t": 0.0, "data": None})
    return {"ok": True, "configured": True, "kind": "analytics", "expiresInSec": left, "expiresLabel": _expires_label(left),
            "userName": "Analytics token (read-only, long-lived)", "feed": _feed_probe(), "publicUrl": _ngrok_url(),
            "message": "Analytics token saved as the automatic fallback. The daily token (if any) stays the primary; this one takes over whenever it expires."}


def apply_access_token(raw):
    """Validate with Upstox, save to the gitignored .env, and swap the live client."""
    token = _extract_token(raw)
    if _is_analytics(token):
        return apply_analytics_token(token)
    probe = upstox.UpstoxClient(token)
    upstox.GOVERNOR.reset()
    try:
        prof = probe.profile()
    except upstox.UpstoxError as exc:
        # Upstox's 1-year read-only "Analytics" token cannot read the user profile - accept it when it can read market data
        try:
            probe.quotes([INSTRUMENTS[0]["key"]])
            prof = {"user_name": "read-only market-data token (long-lived)"}
        except upstox.UpstoxError:
            raise exc
    left = upstox.token_seconds_left(token)
    if left is not None and left <= 0:
        raise ValueError("that token is already expired; generate a fresh one")
    desk_env.write_var("UPSTOX_ACCESS_TOKEN", token)
    with _token_lock:
        globals()["TOKEN"] = token
        UX.set_primary(token)
        with _cache_lock:
            _cache.clear()
        _payload.update({"t": 0.0, "data": None})
    feed = _feed_probe()
    return {
        "ok": True,
        "configured": True,
        "expiresInSec": left,
        "expiresLabel": _expires_label(left),
        "userName": prof.get("user_name") or prof.get("user_id"),
        "feed": feed,
        "publicUrl": _ngrok_url(),
        "message": ("Token saved to .env and applied live - no restart. "
                    + ("Live Nifty quote fetched from Upstox." if feed["online"]
                       else "Token accepted, but the quote probe failed: "
                            + str(feed.get("error")))),
    }


def build_recent_calls(days=30):
    """Full call list for the last `days` calendar days: entry, T1/T2/T3, stop, outcome.

    Same walk-forward audit as the 30-day table, but every call is returned instead of
    the last six. Rows are labelled with the IST date/time and sorted newest first.
    """
    data = build_market(force=True)
    items = {i["id"]: i for i in data["instruments"]}
    rows, segments = [], []
    for iid in TRADEABLE_IDS:
        it = items.get(iid) or {}
        inst = next(x for x in INSTRUMENTS if x["id"] == iid)
        key = PROFILE_BY_ID[iid]
        entry = {"id": iid, "name": it.get("name") or inst["name"],
                 "profile": FORECAST_PROFILES[key]["name"], "profileKey": key,
                 "minMove": inst["minMove"], "calls": 0, "error": None,
                 "windowStart": None, "windowEnd": None,
                 "winRate": None, "t2Rate": None, "t3Rate": None}
        segments.append(entry)
        candles = it.get("candles") or []
        if it.get("timeframe") != "5m" or len(candles) < 100:
            entry["error"] = it.get("error") or "no 5m candles from Upstox"
            continue
        try:
            res = _recent_all(iid, key, _audit_candles(inst), inst["minMove"], days) or {}
        except Exception as exc:
            entry["error"] = str(exc)
            continue
        if not res.get("samples"):
            entry["error"] = res.get("note") or "no setups in the window"
            continue
        entry.update(calls=res["samples"], windowStart=res["windowStart"],
                     windowEnd=res["windowEnd"], winRate=res["winRate"],
                     t2Rate=res["t2Rate"], t3Rate=res["t3Rate"])
        for e in res["examples"]:
            e = dict(e)
            stamp = datetime.datetime.fromtimestamp(e["barTime"] + 19800,
                                                    datetime.timezone.utc)
            e.update(segment=iid, name=entry["name"], minMove=inst["minMove"],
                     date=stamp.strftime("%Y-%m-%d"), time=stamp.strftime("%H:%M"))
            # clock time of the 5m candle in which each level was first touched (None = not touched)
            for key, bars in (("t1At", e.get("t1Bars")), ("t2At", e.get("t2Bars")),
                              ("t3At", e.get("t3Bars")), ("slAt", e.get("stopBars"))):
                e[key] = (e["barTime"] + bars * 300) if bars else None
            e["entryAt"] = e["barTime"] + 300
            rows.append(e)
    rows.sort(key=lambda r: (r["barTime"], r["segment"]), reverse=True)
    stats = _call_totals(rows)
    return {
        "generatedAt": int(time.time()),
        "istClock": time.strftime("%Y-%m-%d %H:%M:%S", ist_now()),
        "days": days,
        "scope": "Calls only for Sensex, Nifty, Bank Nifty, Fin Nifty, Midcap Select and Next 50.",
        "segments": segments,
        "totals": stats,
        "calls": rows,
        "method": "Same no-lookahead walk-forward audit and profile gates as the 30-day table. "
                  "T1 = minimum move, T2 = 2x, T3 = 3x. A target counts only if touched before "
                  "the stop; a candle touching both is scored as a stop first. Timeout = neither "
                  "level by 15:25 IST, closed at the last price, not a win. Exit is the touched "
                  "level (T1 or stop) or the last price on a timeout; R uses the stop distance "
                  "as 1R, before costs. The deployed plan still exits at T1.",
    }


def _hist_1m(inst):
    """Real Upstox 1-minute history (~189 days, 27-day chunks), disk-cached per IST date, plus today."""
    path = os.path.join(CACHE_DIR, f"hist1m_{inst['id']}.json")

    def fetch():
        stamp = str(_ist_today())
        try:
            with open(path) as fh:
                blob = json.load(fh)
            if blob.get("date") == stamp and time.time() - os.path.getmtime(path) < 6 * 3600 \
                    and len(blob.get("rows", [])) > 1000:
                return blob["rows"]
        except Exception:
            pass
        rows = {}
        today = datetime.date.today()
        for k in range(3 if LITE else 7):        # ~189 days for the 6-month scorecard; 81 days in lite mode
            end = today - datetime.timedelta(days=27 * k + 1)
            start = end - datetime.timedelta(days=26)
            try:
                part = UX.candles(inst["key"], "minutes", 1, end.isoformat(), start.isoformat())
            except Exception:
                break
            if not part:
                break
            for c in part:
                rows.setdefault(c["t"], c)
        bars = [rows[t] for t in sorted(rows)]
        if len(bars) > 1000:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fh:
                    json.dump({"date": stamp, "rows": bars}, fh)
            except Exception:
                pass
        return bars

    rows = {c["t"]: c for c in cached(f"hist1m:{inst['id']}", 1800, fetch)}
    # Minute bars this app has already seen (today's session, and the last few days) are kept and saved, because Upstox's
    # historical endpoint can lag by hours after the session ends (and after midnight it returns nothing for the day that
    # just finished). Bars only ever grow, so a failed or short fetch can never make seen minutes vanish.
    store = _i1m_store.setdefault(inst["id"], {"rows": {}, "loaded": False, "saved": 0})
    spath = os.path.join(CACHE_DIR, f"intra1m_{inst['id']}.json")
    if not store["loaded"]:
        store["loaded"] = True
        try:
            with open(spath) as fh:
                for c in json.load(fh):
                    store["rows"][c["t"]] = c
            store["saved"] = len(store["rows"])
        except Exception:
            pass
    try:
        for c in cached(f"intra1m:{inst['id']}", INTRA1M_TTL, lambda: UX.intraday(inst["key"], "minutes", 1)):
            store["rows"][c["t"]] = c
    except Exception:
        pass
    cutoff = time.time() - 5 * 86400
    for t in [t for t in store["rows"] if t < cutoff]:
        del store["rows"][t]
    if len(store["rows"]) - store["saved"] >= 20:
        try:
            os.makedirs(os.path.dirname(spath), exist_ok=True)
            with open(spath, "w") as fh:
                json.dump(sorted(store["rows"].values(), key=lambda c: c["t"]), fh)
            store["saved"] = len(store["rows"])
        except Exception:
            pass
    today0 = int(time.time() - ((time.time() + 19800) % 86400))
    for t, c in store["rows"].items():
        if t >= today0 or t not in rows:        # today's bars come from the live feed; older ones only fill gaps in history
            rows[t] = c
    return [rows[t] for t in sorted(rows)]


_i1m_store = {}


_FUT_UNDER = {"NIFTY": ("NSE", "NSE_FO", "NIFTY"), "BANKNIFTY": ("NSE", "NSE_FO", "BANKNIFTY"),
              "SENSEX": ("BSE", "BSE_FO", "SENSEX")}


def _future_keys():
    """Near-month future instrument key per index, from Upstox's public instrument master (cached per day)."""
    path = os.path.join(CACHE_DIR, "futkeys.json")
    stamp = str(_ist_today())
    try:
        with open(path) as fh:
            blob = json.load(fh)
        if blob.get("date") == stamp:
            return blob["keys"]
    except Exception:
        pass
    import gzip
    keys, now_ms = {}, time.time() * 1000
    for exch in ("NSE", "BSE"):
        req = urllib.request.Request(
            f"https://assets.upstox.com/market-quote/instruments/exchange/{exch}.json.gz",
            headers={"User-Agent": "Mozilla/5.0"})
        ins = json.loads(gzip.decompress(urllib.request.urlopen(req, timeout=90).read()))
        for seg, (ex, fo, under) in _FUT_UNDER.items():
            if ex != exch:
                continue
            futs = [x for x in ins if x.get("segment") == fo and x.get("instrument_type") == "FUT"
                    and x.get("underlying_symbol") == under and x.get("expiry", 0) >= now_ms - 86400000]
            if futs:
                keys[seg] = min(futs, key=lambda x: x["expiry"])["instrument_key"]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"date": stamp, "keys": keys}, fh)
    except Exception:
        pass
    return keys


def _fut_candles(key):
    """Real 5m futures candles with volume + open interest (7th field), last ~3 sessions."""
    def fetch():
        out, today = {}, datetime.date.today()
        path = ("https://api.upstox.com/v3/historical-candle/{k}/minutes/5/{e}/{s}")
        for url in (path.format(k=urllib.parse.quote(key, safe=""), e=today.isoformat(),
                                s=(today - datetime.timedelta(days=6)).isoformat()),
                    BASE3_INTRADAY.format(k=urllib.parse.quote(key, safe=""))):
            try:
                req = urllib.request.Request(url, headers={
                    "Authorization": "Bearer " + (UX.active_token() or ""), "Accept": "application/json", "User-Agent": upstox._UA})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode("utf-8")).get("data") or {}
            except Exception:
                continue
            for r in data.get("candles") or []:
                try:
                    ts = int(datetime.datetime.fromisoformat(r[0]).timestamp())
                    out[ts] = (float(r[4]), float(r[5] or 0), float(r[6] or 0))
                except Exception:
                    continue
        return [(t,) + out[t] for t in sorted(out)]
    return cached(f"futc:{key}", FUT_TTL, fetch)


BASE3_INTRADAY = "https://api.upstox.com/v3/historical-candle/intraday/{k}/minutes/5"


def _futures_context():
    """Live volume / OI read from the near-month future. Context only - never used to gate a call."""
    out = {}
    try:
        keys = cached("futkeys", 3600, _future_keys)
    except Exception:
        return out
    now = time.time()
    for seg, key in keys.items():
        try:
            rows = [r for r in _fut_candles(key) if r[0] + 300 <= now]
            if len(rows) < 30:
                continue
            last = rows[-1]
            prev = [r[2] for r in rows[-21:-1] if r[2] > 0]
            vr = last[2] / (sum(prev) / len(prev)) if prev else None
            ref = rows[-7]                       # six candles (30 min) back
            day0 = next((r for r in rows if _day_ist(r[0]) == _day_ist(last[0])), rows[0])
            dpx, doi = last[1] - ref[1], last[3] - ref[3]

            def buildup(dp, do):
                if do > 0:
                    return "Long buildup (price up, OI up)" if dp > 0 else "Short buildup (price down, OI up)"
                return "Short covering (price up, OI down)" if dp > 0 else "Long unwinding (price down, OI down)"
            out[seg] = {
                "contract": key, "asOf": last[0], "volume": last[2], "volumeRatio": round(vr, 2) if vr else None,
                "oi": last[3], "oiChange30m": doi, "oiChangeDay": last[3] - day0[3],
                "read": buildup(dpx, doi) if doi else "OI unchanged",
                "volumeRead": ("Heavy volume" if vr and vr >= 1.5 else "Light volume" if vr and vr < 0.7
                               else "Normal volume") if vr else None,
            }
        except Exception:
            continue
    return out


def _day_ist(t):
    return int(t + 19800) // 86400


_short_lock = threading.Lock()


def _inject_ltp(cmap1, quotes, now):
    """Fold the latest real traded price into the forming 1-minute candle, so a level touched in the last seconds
    counts even if the candle feed lags. Only while the market is open and the quote is fresh."""
    live = {}
    minute0 = int(now // 60 * 60)
    for pid, rows in list(cmap1.items()):
        inst = next(x for x in INSTRUMENTS if x["id"] == pid)
        q = (quotes or {}).get(inst["key"]) or {}
        ltp = q.get("last")
        try:
            qat = int(q.get("quoteAt")) / 1000.0
        except (TypeError, ValueError):
            qat = 0
        live[pid] = {"last": ltp, "at": int(qat) if qat else None, "fresh": bool(ltp and now - qat < 120)}
        if not (market_open() and ltp and now - qat < 120 and rows):
            continue
        rows = list(rows)
        last = rows[-1]
        if last["t"] == minute0:
            rows[-1] = dict(last, high=max(last["high"], ltp), low=min(last["low"], ltp), close=ltp)
        elif last["t"] < minute0:
            rows.append({"t": minute0, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": 0, "liveTick": True})
        cmap1[pid] = rows
    return live


def build_short(days=60):
    """Short-term page payload (shortterm.py): 1m / 5m / 15m / 30m. Real Upstox candles only. Open calls are
    tracked live, including the still-forming minute, so a touch counts the moment price reaches a level."""
    def make():
        cmap = {1: {}, 5: {}, 15: {}, 30: {}}
        errors = {}

        def fetch_one(pid):                    # the four indices are independent: fetch them in parallel
            inst = next(x for x in INSTRUMENTS if x["id"] == pid)
            out = {"pid": pid, "base5": None, "m1": None, "e5": None, "e1": None}
            try:
                out["base5"] = _audit_candles(inst)
            except Exception as exc:
                out["e5"] = str(exc)
            try:
                out["m1"] = _hist_1m(inst)
            except Exception as exc:
                out["e1"] = str(exc)
            return out
        with ThreadPoolExecutor(max_workers=8) as pool:
            fut_ctx = pool.submit(_futures_context)
            for o in pool.map(fetch_one, list(shortterm.SEGMENTS)):
                pid = o["pid"]
                if o["base5"] is not None:
                    cmap[5][pid] = o["base5"]
                    cmap[15][pid] = shortterm.resample(o["base5"], 15)
                    cmap[30][pid] = shortterm.resample(o["base5"], 30)
                else:
                    errors[pid] = o["e5"]
                if o["m1"] is not None:
                    cmap[1][pid] = o["m1"]
                else:
                    errors.setdefault(pid + "/1m", o["e1"])
            futures_ctx = fut_ctx.result()
        now = time.time()
        try:
            quotes = cached("quotes", 2.5, lambda: UX.quotes([i["key"] for i in INSTRUMENTS]))
        except Exception:
            quotes = {}
        ltps = _inject_ltp(cmap[1], quotes, now)
        res = shortterm.build(cmap, days=days, now=now, futures=futures_ctx)
        res["ltp"] = ltps
        res["marketOpen"] = market_open()
        res["serverTime"] = now
        for blk in res["timeframes"].values():
            for seg in blk["segments"]:
                err = errors.get(seg["id"]) or (errors.get(seg["id"] + "/1m") if blk["tf"] == 1 else None)
                if err:
                    seg["error"] = err
        return res
    with _short_lock:                      # single flight: concurrent pollers share one computation
        return cached(f"short:{days}", 4, make)


_active_lock = threading.Lock()


def build_active():
    """Every call generated in the current (or last) session - long AND short term - with live status."""
    import activecalls

    def make():
        now = time.time()
        sh = build_short(60)
        lt = build_today()
        cmap1 = {}

        def one(pid):
            inst = next(x for x in INSTRUMENTS if x["id"] == pid)
            try:
                return pid, _hist_1m(inst)
            except Exception:
                return pid, []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for pid, rows in pool.map(one, TRADEABLE_IDS):
                cmap1[pid] = rows
        try:
            quotes = cached("quotes", 2.5, lambda: UX.quotes([i["key"] for i in INSTRUMENTS]))
        except Exception:
            quotes = {}
        ltps = _inject_ltp(cmap1, quotes, now)
        return activecalls.build(sh, lt, cmap1, ltps, FORECAST_PROFILES, PROFILE_BY_ID, now, market_open())
    with _active_lock:
        return cached("active", 3, make)


def _short_csv(res, tf="all"):
    """Every short-term call with all levels and the clock time each one was touched (IST)."""
    def clock(t):
        return (datetime.datetime.fromtimestamp(t + 19800, datetime.timezone.utc)
                .strftime("%Y-%m-%d %H:%M") if t else "")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "entry_time_ist", "candle_min", "index", "side", "entry", "SL", "SL_pts", "SL_touched_at",
                "T1", "T1_pts", "T1_touched_at", "T1_after_min", "T2", "T2_pts", "T2_touched_at", "T2_after_min",
                "T3", "T3_pts", "T3_touched_at", "T3_after_min", "result", "plan_exit_at", "plan_exit_reason",
                "plan_pts", "pts_if_all_out_at_T1", "max_favourable_pts", "patterns"])
    calls = []
    for k, blk in res["timeframes"].items():
        if k != "all" and tf in ("all", k):
            calls.extend(blk["calls"])
    calls.sort(key=lambda r: (r["t"], r["tf"], r["segment"]))
    for r in calls:
        w.writerow([r["date"], clock(r["entryAt"])[11:], r["tf"], r["segment"], r["side"], r["entry"], r["sl"],
                    r["slPts"], clock(r.get("slAt")), r["t1"], r["t1Pts_"], clock(r.get("t1At")), r.get("t1After") or "",
                    r["t2"], r["t2Pts_"], clock(r.get("t2At")), r.get("t2After") or "", r["t3"], r["t3Pts_"],
                    clock(r.get("t3At")), r.get("t3After") or "", r["result"], clock(r.get("planExitAt")),
                    r.get("planReason") or "", r["planPts"], r["t1ExitPts"], r["mfe"], "; ".join(r.get("patterns") or [])])
    return out.getvalue()


def _calls_csv(res):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date_ist", "time_ist", "segment", "side", "entry", "target1", "target2",
                "target3", "stop", "stop_pts", "outcome", "exit", "mfe_pts",
                "targets_hit", "bars", "minutes", "r_multiple", "min_move",
                "T1_touched_at", "T2_touched_at", "T3_touched_at", "SL_touched_at"])
    for r in res["calls"]:
        w.writerow([r["date"], r["time"], r["segment"], r["direction"], r["entry"],
                    r["target1"], r["target2"], r["target3"], r["stop"],
                    r.get("stopDist"), r["outcome"], r.get("exit"), r.get("mfe"),
                    r.get("targetsHit"), r.get("bars"), r.get("minutes"),
                    r.get("r"), r.get("minMove"),
                    *[(datetime.datetime.fromtimestamp(r[k] + 19800, datetime.timezone.utc)
                       .strftime("%Y-%m-%d %H:%M") if r.get(k) else "")
                      for k in ("t1At", "t2At", "t3At", "slAt")]])
    return out.getvalue()


def _history(inst, days=30):
    """Upstox 5m history over `days`, chunked (the API caps one request at ~30 days)."""
    path = os.path.join(CACHE_DIR, f"hist_{inst['id']}_{days}.json")

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
        # One 5m response is capped at 1500 bars, so a 30-day chunk silently drops the
        # newest session. 20-day chunks (~1050 bars) always reach the last completed day.
        for k in range((days + 19) // 20):
            end = today - datetime.timedelta(days=20 * k + 1)
            start = end - datetime.timedelta(days=19)
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


def _candles_days(inst, days):
    """Real Upstox 5m bars over `days` calendar days (history chunks + today), without touching AUDIT_DAYS."""
    rows = {}
    try:
        for c in _history(inst, days):
            rows[c["t"]] = c
    except Exception:
        pass
    for c in _intraday(inst)["rows"]:
        rows[c["t"]] = c
    return [rows[k] for k in sorted(rows)]


_score_lock = threading.Lock()


def build_scorecard():
    import scorecard
    with _score_lock:
        return cached("scorecard", 600, lambda: scorecard.build(sys.modules[__name__]))


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


def _pick_profile(inst, timeframe=None, bars=0):
    """Only the six index segments get calls, and only on a real 5m series.

    There is no lower-hit-rate fallback: if the profile's own six-month audit is weak or
    unavailable, `build_forecast` returns the blocking reason and no call. Sector indices
    are chart context only.
    """
    if not inst.get("tradeable"):
        return None
    key = PROFILE_BY_ID.get(inst["id"])
    if not key or (timeframe is not None and (timeframe != "5m" or bars < 80)):
        return None
    return key


def ist_now():
    return time.gmtime(time.time() + 19800)


def market_open():
    t = ist_now()
    if t.tm_wday >= 5:
        return False
    minutes = t.tm_hour * 60 + t.tm_min
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def _cache_path(inst):
    return os.path.join(CACHE_DIR, inst["id"] + ".json")


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
    """Real Yahoo 5m bars used only to fill a lagging tail. Short timeout and a 60 s cache so a slow Yahoo can
    never stall the dashboard (a failure simply means no fill, exactly as before)."""
    if not inst.get("yahoo"):
        return []

    def fetch():
        try:
            return yahoo.yahoo_chart(inst["yahoo"], "5d", "5m", timeout=5).get("candles") or []
        except Exception:
            return []
    return cached(f"y5:{inst['yahoo']}", 60, fetch)


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
    return cached(f"intra:{inst['id']}", INTRA5M_TTL if inst["id"] in TRADED_IDS else INTRA5M_CONTEXT_TTL, fetch)


def _daily(inst):
    today = datetime.date.today()
    start = today - datetime.timedelta(days=420)
    return cached(f"daily:{inst['id']}", 1800,
                  lambda: UX.candles(inst["key"], "days", 1, today.isoformat(), start.isoformat()))


# ---------------------------------------------------------------- next-day / overnight
# This block is additive: it never changes the intraday forecasts above. It is a separate
# long-only daily model measured on ~3 years of real daily bars, with its own gate.

def _ist_date(ts):
    return datetime.datetime.fromtimestamp(ts + 19800, datetime.timezone.utc).date()


def _ist_midnight(day):
    return int(datetime.datetime(day.year, day.month, day.day, tzinfo=IST).timestamp())


def _normalise_daily(rows):
    """One bar per IST day, OHLC floats, sorted. Daily bars are stamped at IST midnight."""
    by_t = {}
    for c in rows or []:
        try:
            t = _ist_midnight(_ist_date(int(c["t"])))
            o, h, l, cl = (float(c["open"]), float(c["high"]),
                           float(c["low"]), float(c["close"]))
        except (KeyError, TypeError, ValueError):
            continue
        if cl <= 0:
            continue
        by_t[t] = {"t": t, "open": o, "high": h, "low": l, "close": cl,
                   "volume": c.get("volume") or 0}
    return [by_t[k] for k in sorted(by_t)]


def _overnight_daily(inst):
    """~3 years of real daily bars: Upstox first, Yahoo (real, labelled) as fallback."""
    path = os.path.join(CACHE_DIR, f"on_{inst['id']}_3y.json")

    def fetch():
        try:
            if time.time() - os.path.getmtime(path) < 6 * 3600:
                with open(path, encoding="utf-8") as fh:
                    hit = json.load(fh)
                if len(hit.get("rows") or []) > 250:
                    return hit
        except Exception:
            pass
        error = None
        try:
            end = datetime.date.today()
            start = end - datetime.timedelta(days=OVERNIGHT_CALENDAR_DAYS)
            rows = _normalise_daily(
                UX.candles(inst["key"], "days", 1, end.isoformat(), start.isoformat()))
            src = "UPSTOX"
        except Exception as exc:
            rows, src, error = [], "ERROR", str(exc)
        if len(rows) < 300 and inst.get("yahoo"):
            try:
                alt = _normalise_daily(
                    yahoo.yahoo_chart(inst["yahoo"], "5y", "1d").get("candles") or [])
                if len(alt) > len(rows):
                    rows, src, error = alt, "YAHOO", None
            except Exception as exc:
                error = error or str(exc)
        if len(rows) < 60:
            raise RuntimeError(error or "no daily history from Upstox or Yahoo")
        out = {"rows": rows, "src": src}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(out, fh)
            os.replace(tmp, path)
        except Exception:
            pass
        return out

    return cached(f"on-daily:{inst['id']}", 1800, fetch)


def _today_daily_bar(it, today):
    """Aggregate today's real 5m bars into a daily bar until Upstox publishes the EOD bar."""
    rows = [c for c in (it.get("candles") or []) if _ist_date(c["t"]) == today]
    if not rows:
        return None
    last = rows[-1]
    hm = time.gmtime(last["t"] + 19800)
    return {
        "t": _ist_midnight(today),
        "open": rows[0]["open"],
        "high": max(c["high"] for c in rows),
        "low": min(c["low"] for c in rows),
        "close": last["close"],
        "volume": 0,
        "complete": (hm.tm_hour, hm.tm_min) >= (15, 25),
        "lastBar": last["t"],
        "src": "UPSTOX 5m->1D",
    }


def _overnight_bars(hist, it, today):
    """Daily history plus today's real bar (official EOD when available, else 5m aggregate)."""
    bars = [dict(c) for c in hist["rows"]]
    agg = _today_daily_bar(it, today)
    if agg:
        idx = next((k for k, b in enumerate(bars) if _ist_date(b["t"]) == today), None)
        if idx is None:
            bars.append(agg)
        elif not agg["complete"]:
            bars[idx] = dict(bars[idx], open=agg["open"], high=agg["high"],
                             low=agg["low"], close=agg["close"], src=agg["src"])
        bars.sort(key=lambda b: b["t"])
    return bars, agg


def build_overnight():
    """Additive next-day model payload. All existing intraday endpoints are unchanged."""
    data = build_market()
    items = {i["id"]: i for i in data["instruments"]}
    today = _ist_today()
    now = ist_now()
    now_min = now.tm_hour * 60 + now.tm_min
    params = overnight.PARAMS
    segments, pooled, recent_pooled, signals = [], [], [], []
    for inst in INSTRUMENTS:
        if not inst.get("tradeable"):
            continue
        it = items.get(inst["id"]) or {}
        entry = {
            "id": inst["id"], "name": inst["name"], "profile": params["name"],
            "status": "ERROR", "statusLabel": "FEED ERROR", "forming": False,
            "error": None, "sessionDate": None, "isToday": False,
            "sessionComplete": False, "dataSource": None, "bars": 0,
            "fromDate": None, "toDate": None, "current": None, "audit": None,
            "recent": None, "qualified": False, "blockReason": None,
            "plan": None, "lastTrade": None,
        }
        segments.append(entry)
        try:
            hist = _overnight_daily(inst)
        except Exception as exc:
            entry["error"] = f"daily history: {exc}"
            entry["dataSource"] = "ERROR"
            continue
        bars, agg = _overnight_bars(hist, it, today)
        if len(bars) < 60:
            entry["error"] = "not enough daily bars to measure"
            continue
        try:
            trades = overnight.collect(bars)
            audit = overnight.summarize(trades)
            recent = overnight.summarize(
                [t for t in trades if t["ts"] >= bars[-1]["t"] - 30 * 86400])
            cur = overnight.live(bars)
        except Exception as exc:
            entry["error"] = f"overnight audit: {exc}"
            continue
        last = bars[-1]
        last_date = _ist_date(last["t"])
        session_today = last_date == today
        if session_today:
            complete = bool(agg["complete"]) if agg is not None else now_min >= 15 * 60 + 25
        else:
            complete = True
        forming = session_today and not complete
        qualified = bool(audit.get("qualified"))
        block = None
        samples = audit.get("samples") or 0
        rate = audit.get("winRate")
        if samples < params["minSamples"]:
            block = (f"only {samples} signals in three years — need {params['minSamples']} "
                     "to measure an edge")
        elif (rate or 0) < params["minWinRate"]:
            block = (f"three-year direction {rate}% is below the "
                     f"{params['minWinRate']:.0f}% floor")
        if cur.get("signal") and qualified:
            status = "FORMING" if forming else "CALL"
        elif cur.get("signal"):
            status = "BLOCKED"
        else:
            status = "NO SETUP"
        label = {"CALL": "CALL — sell the next close", "FORMING": "FORMING — only after 15:25",
                 "BLOCKED": "BLOCKED — edge below floor", "NO SETUP": "NO SETUP"}[status]
        plan = None
        if cur.get("signal"):
            plan = {
                "side": "LONG", "entry": cur["entry"], "entryDate": cur["date"],
                "exit": "next trading session close",
                "target": cur["target"], "targetMove": cur["targetMove"],
                "riskGuide": cur["riskGuide"], "riskGuidePts": cur["riskGuidePts"],
                "range": cur["range"], "grade": cur["grade"],
                "measuredWinRate": rate, "measuredSamples": samples,
                "measuredAvgPts": audit.get("avgPts"),
            }
        last_src = ("UPSTOX 5m->1D" if last.get("src") == "UPSTOX 5m->1D"
                    else f"{hist['src']} 1D")
        named = []
        for t in trades:
            t = dict(t, id=inst["id"], name=inst["name"])
            named.append(t)
            pooled.append(t)
            if t["ts"] >= bars[-1]["t"] - 30 * 86400:
                recent_pooled.append(t)
        signals.extend(named)
        entry.update({
            "status": status, "statusLabel": label, "forming": forming,
            "sessionDate": last_date.isoformat(), "isToday": session_today,
            "sessionComplete": complete, "dataSource": last_src,
            "bars": len(bars), "fromDate": _ist_date(bars[0]["t"]).isoformat(),
            "toDate": last_date.isoformat(), "current": cur, "audit": audit,
            "recent": recent, "qualified": qualified, "blockReason": block,
            "plan": plan, "lastTrade": dict(audit["examples"][0], id=inst["id"],
                                             name=inst["name"])
            if audit.get("examples") else None,
        })

    pooled_stats = overnight.summarize(pooled)
    recent_stats = overnight.summarize(recent_pooled)
    signals.sort(key=lambda t: t["ts"], reverse=True)
    active = [s for s in segments if s["status"] in ("CALL", "FORMING")]
    blocked = [s for s in segments if s["status"] == "BLOCKED"]
    errors = [s for s in segments if s["status"] == "ERROR"]
    session_dates = sorted({s["sessionDate"] for s in segments if s["sessionDate"]},
                           reverse=True)
    as_of = session_dates[0] if session_dates else today.isoformat()

    def fmt_signed(v, nd=1):
        return "—" if v is None else f"{v:+.{nd}f}"

    if active:
        names = ", ".join(f"{s['name']} ({s['current']['grade']})" for s in active)
        verdict = (
            f"{len(active)} of six index segments trigger the long-only overnight rule for the "
            f"{as_of} close: {names}. Entry is that close; the audited exit is the next trading "
            f"session's close. Three-year audit: {pooled_stats['samples']} signals over "
            f"{pooled_stats['uniqueDates']} distinct oversold days, {pooled_stats['winRate']}% closed "
            f"higher the next session (date-level {pooled_stats['dateWinRate']}%), average "
            f"{fmt_signed(pooled_stats['avgPts'])} pts per call before costs. Older two-thirds "
            f"{pooled_stats['older']['winRate']}%, newest third {pooled_stats['newer']['winRate']}%. "
            "Signals cluster: multiple segment calls from one oversold day are one market bet, "
            "not independent trades.")
    elif blocked:
        verdict = (
            f"{len(blocked)} segment(s) are oversold enough to trigger, but their own three-year "
            f"direction rate is below the {params['minWinRate']:.0f}% floor: "
            + ", ".join(f"{s['name']} ({s['audit'].get('winRate')}%)" for s in blocked)
            + ". No call is issued — the model refuses a setup it cannot measure.")
    elif errors and not any(s["audit"] for s in segments):
        verdict = ("No real daily history is available, so the overnight model has nothing to "
                   "measure and invents nothing. Fix the Upstox token/feed and reopen this panel.")
    else:
        verdict = (
            "No segment is oversold enough tonight: the rule needs RSI(14) ≤ 38 and a 3-day "
            "return ≤ −1.00%. Standing aside is the correct output — this model has no short "
            "side. Reopen after a weak close.")
    if any(s["forming"] for s in segments):
        verdict += (" The latest bar is still forming; nothing is a call until the 15:25 IST "
                    "close.")
    if errors and any(s["audit"] for s in segments):
        verdict += (" Feed errors: " + ", ".join(f"{s['name']} ({s['error']})" for s in errors)
                    + ".")

    feed_notes = list(data.get("feedNotes") or [])
    yahoo_daily = [s["name"] for s in segments if (s["dataSource"] or "").startswith("YAHOO")]
    if yahoo_daily:
        feed_notes.append("Next-day daily history for " + ", ".join(yahoo_daily)
                          + " comes from Yahoo's real daily bars (labelled YAHOO) because the "
                            "Upstox daily pull failed.")
    if errors:
        feed_notes.append("Next-day model unavailable for " + ", ".join(
            f"{s['name']} — {s['error']}" for s in errors) + ".")

    return {
        "generatedAt": int(time.time()),
        "istClock": time.strftime("%Y-%m-%d %H:%M:%S", ist_now()),
        "marketOpen": market_open(),
        "asOfDate": as_of,
        "feeds": data["feeds"],
        "feedNotes": feed_notes,
        "scope": "Next-day model for the six index segments only: Sensex, Nifty, Bank Nifty, "
                 "Fin Nifty, Midcap Select and Next 50.",
        "rule": overnight.rule_text(),
        "params": {
            "rsiMax": params["rsiMax"], "ret3MaxPct": params["ret3Max"] * 100,
            "gradeA": {"rsiMax": params["gradeA"]["rsiMax"],
                       "ret3MaxPct": params["gradeA"]["ret3Max"] * 100},
            "targetAtr": params["targetAtr"], "minWinRate": params["minWinRate"],
            "minSamples": params["minSamples"],
        },
        "verdict": verdict,
        "audit": pooled_stats,
        "recent": recent_stats,
        "segments": segments,
        "calls": [{"id": s["id"], "name": s["name"], "status": s["status"],
                   "plan": s["plan"]} for s in active],
        "signals": signals,
        "method": {
            "rule": overnight.rule_text(),
            "entryExit": "Entry is the signal day's close; the outcome is the next session's "
                         "close. No stop or target is part of the measured result.",
            "noLookahead": "At bar i the signal uses only bars <= i; the outcome uses bar i+1 "
                           "only. The verify.py check truncates the newest bar and confirms no "
                           "earlier outcome changes.",
            "longOnly": "There is no short side. In the same three-year test, overbought "
                        "closes did not show a stable next-day short edge.",
            "clustering": "Signals cluster on the same oversold days, so several segment calls "
                          "from one day are one market bet, not independent trades.",
            "data": "Upstox daily candles (~3 years). Until Upstox publishes today's EOD bar, "
                    "today's daily bar is aggregated from real Upstox 5m bars and labelled "
                    "UPSTOX 5m->1D. Yahoo daily is a labelled fallback only where the segment "
                    "has a symbol.",
            "risk": "No stop is audited. The plan shows the next-day ATR range, the median "
                    "adverse excursion and the worst close-to-close loss; an overnight gap can "
                    "be larger than any stop.",
            "costs": "All returns are gross, before brokerage, taxes and slippage. Pooled gross "
                     "expectancy is about +0.17% per call, so costs are material.",
            "inSample": "The rule was fixed first and then measured on the same three years, so "
                        "these are in-sample rates. The older/newer split and the live 55% floor "
                        "are shown so the edge is not oversold.",
            "scope": "Six index segments only; sector indices are chart context and never get a "
                     "next-day call.",
            "reproduce": "python verify.py recomputes this audit from an independent daily pull.",
        },
    }


def _overnight_csv(res):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["signal_date", "exit_date", "segment", "grade", "side", "rsi14",
                "ret3_pct", "entry_close", "exit_close", "points", "pct", "gap_points",
                "target", "target_hit", "win"])
    for t in res.get("signals") or []:
        w.writerow([t["date"], t["nextDate"], t["id"], t["grade"], t["side"], t["rsi"],
                    t["ret3Pct"], t["entry"], t["exit"], t["points"], t["pct"],
                    t["gapPts"], t["target"], int(t["targetHit"]), int(t["win"])])
    return out.getvalue()



def _expiries(chain_key):
    return cached(f"exp:{chain_key}", 1800, lambda: UX.expiries(chain_key))


def _chain(chain_key, expiry):
    return cached(f"chain:{chain_key}:{expiry}", CHAIN_TTL,
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
            opt_src, opt_error = "ERROR", f"Upstox options: {friendly_feed_error(exc)}"

    signal = None
    if candles and spot and len(candles) >= 40:
        signal = analysis.build_signal(spot, candles, options, timeframe)
    forecast = None
    profile_key = _pick_profile(inst, timeframe, len(candles))
    if signal and profile_key:
        profile_audit = _validation(inst["id"], profile_key,
                                    _audit_candles(inst), inst["minMove"])
        forecast = analysis.build_forecast(spot, candles, options, signal, timeframe,
                                           inst["minMove"], inst["maxMove"],
                                           params=FORECAST_PROFILES[profile_key],
                                           audit=profile_audit)

    return {
        "id": inst["id"], "name": inst["name"], "group": inst["group"],
        "tradeable": bool(inst.get("tradeable")),
        "profileKey": profile_key,
        "spot": round(spot, 2) if spot else None,
        "changePct": change_pct,
        "timeframe": timeframe,
        "minMove": inst["minMove"], "maxMove": inst["maxMove"],
        "lastQuoteAt": (q or {}).get("quoteAt"),
        "source": {
            "quote": "UPSTOX" if (quotes and inst["key"] in quotes) else ("YAHOO" if q else "ERROR"),
            "candles": candle_src, "options": opt_src,
        },
        "error": friendly_feed_error("; ".join(errors)) if errors else (quote_error and f"quote: {friendly_feed_error(quote_error)}" or None),
        "optionsError": opt_error,
        "candles": candles,
        "options": options,
        "signal": signal,
        "forecast": forecast,
    }


_mb = {"t": None, "b": b""}


def _market_bytes(force=False):
    """Serve the already-built market payload instantly (a background thread keeps it fresh). Only when nothing has
    been built yet, or the copy is stale, does the request wait for a rebuild."""
    data, t = _payload["data"], _payload["t"]
    if LITE:                                           # never wait for a rebuild: the background thread owns it
        if data is None:
            return None
        if _mb["t"] != _payload["t"]:
            _mb["b"], _mb["t"] = json.dumps(data).encode("utf-8"), _payload["t"]
        return _mb["b"]
    age = time.time() - t if data else None
    if data is None or age > 120 or (force and age > 8):
        data = build_market(force)
    if _mb["t"] != _payload["t"]:
        _mb["b"], _mb["t"] = json.dumps(data).encode("utf-8"), _payload["t"]
    return _mb["b"]


_last = {}
_building = {}
_BG_OWNED = ("short", "active")
_bg_started = {"on": False}


def _remember(name, data):
    if isinstance(data, dict):
        data["refreshSeconds"] = round(_cycle["sec"])
    _last[name] = (time.time(), data)
    return data


def _lower_priority():
    """Run heavy background work at the lowest OS priority (Linux per-thread nice), so on a 0.1-CPU host the web
    handlers always get the CPU first and a request never waits behind a build."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
    except Exception:
        pass


def _kick(name, builder):
    """Build in a background thread (single flight) so no request ever waits on a heavy computation."""
    if _building.get(name):
        return

    def run():
        _lower_priority()
        _building[name] = True
        try:
            _remember(name, builder())
        except Exception:
            pass
        finally:
            _building[name] = False
    threading.Thread(target=run, daemon=True, name="build-" + name).start()


def _age(name):
    ent = _last.get(name)
    return int(time.time() - ent[0]) if ent else None


def _serve(name, builder, max_open=25, max_closed=300):
    """Answer instantly from the latest finished build. Normal mode: rebuild in the request only if nothing is ready or
    the copy is too old. LITE mode: never wait - return the latest copy (even if old, the page shows its age) or None
    while the very first build is still running."""
    ent = _last.get(name)
    if LITE:
        own = name in _BG_OWNED and _bg_started["on"]       # the background loop already builds these - don't duplicate
        if ent is None:
            if not own:
                _kick(name, builder)
            return None
        if time.time() - ent[0] > 120 and not own:
            _kick(name, builder)
        return ent[1]
    if ent and time.time() - ent[0] < (max_open if market_open() else max_closed):
        return ent[1]
    return _remember(name, builder())


def _warming(handler, what):
    handler._send(202, json.dumps({"warming": True, "what": what, "seconds": int(time.time() - _BOOT)}).encode(),
                  "application/json; charset=utf-8")


def _score_loader():
    """LITE: the precomputed 6-month snapshot (recomputing it takes minutes on a 0.1-CPU host)."""
    snap = os.path.join(ROOT, "scorecard_snapshot.json")
    if LITE and os.path.exists(snap):
        with open(snap, encoding="utf-8") as fh:
            d = json.load(fh)
        d["snapshot"] = True
        return d
    return build_scorecard()


def _background_refresher():
    """Keep the dashboard and short-term data warm so opening the page never waits on Upstox / Yahoo."""
    _lower_priority()
    _bg_started["on"] = True
    while True:
        t0 = time.time()
        if LITE and t0 - _last_request > 600:          # nobody is looking: save CPU, let the host sleep
            time.sleep(10)
            continue
        try:
            build_market(force=True)
        except Exception:
            pass
        try:
            _remember("short", build_short(60))
            _remember("active", build_active())
            if not LITE and time.time() - _last.get("score", (0,))[0] > 1800:      # heavy: every 30 min
                _remember("score", build_scorecard())
        except Exception:
            pass
        dur = time.time() - t0
        floor = REFRESH_OPEN_SEC if market_open() else REFRESH_CLOSED_SEC
        _cycle["sec"] = max(floor, 2 * dur) if LITE else floor
        time.sleep(_cycle["sec"] if LITE else floor)


def build_market(force=False):
    global _payload
    _sync_token_from_env()
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
            notes.append(f"UPSTOX FEED DOWN: {friendly_feed_error(quote_error)}. Charts fall back to Yahoo real data; "
                         "option/OI panels are disabled. Open Access and paste a fresh token, or run "
                         "python upstox_login.py.")
        elif token_left is not None and token_left < 1800:
            notes.append(f"Upstox access token expires in {max(token_left // 60, 0)} min - "
                         "paste a fresh one in the Access panel.")
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
    items = [i for i in data["instruments"] if i.get("tradeable")]
    if segment_ids:
        items = [i for i in items if i["id"] in segment_ids]

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
            "profileKey": i.get("profileKey"),
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
            f"{len(qualified)} of {len(items)} tracked index segments clear every gate of their own "
            f"measured profile (unanimous indicators, ATR expected move ≥4× target, entry before "
            f"14:30/14:35 IST, six-month audit ≥75%; {fs['horizonLabel']}). "
            f"Highest-conviction qualifying call: {top['name']} {top['signal']['direction']} at "
            f"{top['signal']['confidence']}% — expected ±{fs['expectedMove']:.0f} pts for a "
            f"{fs['minMove']}-pt T1, {fs['timing'].lower()} timing, historical capture "
            f"{fs['captureProb']}%"
            + (f" of {fs['captureSamples']} cases" if fs.get("captureSamples") else "")
            + f". Entry {plan.get('entry')}, SL {plan.get('stop')} "
            f"({plan.get('riskPerUnit')} pts), T1 {plan.get('target1')} "
            f"({plan.get('target1Move')} pts), T2 {plan.get('target2')} "
            f"({plan.get('target2Move')} pts), T3 {plan.get('target3')} "
            f"({plan.get('target3Move')} pts).")
        o = top.get("options") or {}
        if o.get("pcr") is not None:
            verdict += f" OI context: PCR {o['pcr']}, max pain {int(o['maxPain']) if o.get('maxPain') else '—'}."
        if top["signal"]["direction"].lower() != breadth:
            verdict += " Breadth and the strongest qualifying call disagree — size accordingly."
    elif nearest:
        f = nearest.get("forecast") or {}
        verdict = (
            f"No qualifying call across the {len(items)} tracked index segments. "
            f"{len(actionable)} directional signal(s), but none clears every gate of its own profile "
            f"(unanimity, expected move ≥4× the target, entry cutoff, EMA 50/RSI filter and "
            f"six-month audit ≥75%). Nearest: {nearest['name']} {nearest['signal']['direction']} "
            f"at {nearest['signal']['confidence']}% — expected ±{f.get('expectedMove')} pts vs T1 "
            f"{f.get('minMove')} ({f.get('status')}, {str(f.get('timing', '')).lower()} timing). "
            "No trade is the correct output; wait for compression + a fresh trigger.")
    else:
        missing = [i for i in items if not i["signal"]]
        if items and len(missing) == len(items):
            verdict = (f"No real market data across the {len(items)} tracked index segments — no "
                       "prediction is possible and no numbers are invented. Fix the feed (Upstox "
                       "token: python upstox_login.py), then rescan.")
        else:
            verdict = (f"No actionable conviction across the {len(items)} tracked index segments — "
                       "all inside the neutral band. Wait for a break of the option OI range before "
                       "taking a side.")

    return {
        "generatedAt": int(time.time()),
        "istClock": time.strftime("%Y-%m-%d %H:%M:%S", ist_now()),
        "feeds": data["feeds"],
        "feedNotes": data["feedNotes"],
        "scope": "Calls only for Sensex, Nifty, Bank Nifty, Fin Nifty, Midcap Select and "
                 "Next 50. Sector indices are chart context only.",
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
    """Evidence page: feed provenance + walk-forward audit of each tracked segment profile."""
    data = build_market(force=True)
    items = {i["id"]: i for i in data["instruments"]}
    provenance, validation = [], {}
    for iid in TRADEABLE_IDS:
        it = items.get(iid) or {}
        inst = next(x for x in INSTRUMENTS if x["id"] == iid)
        f = it.get("forecast") or {}
        candles = it.get("candles") or []
        last = candles[-1] if candles else None
        provenance.append({
            "id": iid, "name": it.get("name") or inst["name"],
            "quote": (it.get("source") or {}).get("quote"),
            "candles": (it.get("source") or {}).get("candles"),
            "options": (it.get("source") or {}).get("options"),
            "spot": it.get("spot"), "changePct": it.get("changePct"),
            "timeframe": it.get("timeframe"),
            "bars": len(candles), "lastBar": last["t"] if last else None,
            "lastClose": last["close"] if last else None,
            "quoteAt": it.get("lastQuoteAt"),
            "optionExpiry": (it.get("options") or {}).get("expiry"),
            "forecastStatus": f.get("status"), "expectedMove": f.get("expectedMove"),
            "profile": f.get("profile") or FORECAST_PROFILES[PROFILE_BY_ID[iid]]["name"],
            "profileKey": PROFILE_BY_ID[iid], "minMove": it.get("minMove"),
        })
        if candles and it.get("timeframe") == "5m":
            key = PROFILE_BY_ID[iid]
            audit = _audit_candles(inst)
            validation[iid] = {
                "selected": key,
                "activeProfile": FORECAST_PROFILES[key]["name"],
                "auditBars": len(audit),
                "active": _validation(iid, key, audit, it["minMove"]),
                "recent": _recent_validation(iid, key, audit, it["minMove"]),
            }

    return {
        "generatedAt": int(time.time()),
        "istClock": time.strftime("%Y-%m-%d %H:%M:%S", ist_now()),
        "feeds": data["feeds"],
        "provenance": provenance,
        "validation": validation,
        "scope": "Calls only for Sensex, Nifty, Bank Nifty, Fin Nifty, Midcap Select and Next 50.",
        "method": {
            "candles": "Upstox v3 5-minute candles. Every tracked segment is audited over six "
                       "months (~9,300 bars, fetched in 30-day chunks). When Upstox EOD history "
                       "lags the just-finished session, the missing tail is filled from Yahoo's "
                       "real 5m series and labelled UPSTOX+YAHOO.",
            "noLookahead": "At historical bar i the audit only uses bars <= i; the outcome uses "
                           "bars i+1..i+horizon. One trade at a time, cooldown resets each session, "
                           "exactly like the profile.",
            "sessionOutcome": "Intraday outcome window closes at 15:25 IST on the entry day. A "
                              "call that hits neither T1 nor the stop is a TIMEOUT closed at the "
                              "last price and is NOT counted as a win. If one candle touches both "
                              "T1 and the stop, the stop is scored first (conservative).",
            "recentTargets": "The last-30-days table re-runs the same profile and gates on the newest "
                             "30 calendar days and tracks T1, T2 (2x) and T3 (3x) separately. A target "
                             "counts only if it was touched before the stop; a candle touching a target "
                             "and the stop together is a stop. The live call plan still exits at T1 - "
                             "the T2/T3 rates describe a multi-target variant, not the deployed exit.",
            "profileGate": "Each segment has its own measured profile (wide stop 2.5-4x the T1 "
                           "target, 45-54 bar horizon, unanimous EMA 9/21 + MACD + Supertrend, "
                           "ATR expected move >=4-5x T1, entry cutoff 14:00/14:30 IST, plus "
                           "segment-specific EMA 50 / extreme-RSI / capture-odds filters). A call "
                           "is issued only while that segment's own six-month audit still measures "
                           ">=75% T1-before-stop; otherwise the desk returns the blocking reason "
                           "and no call. It never falls back to a lower-hit-rate profile.",
            "selectionBias": "The profile parameters were selected on the same six months they "
                             "are measured on, so these are in-sample hit rates, not a forward "
                             "guarantee. The live gate re-runs the audit continuously and blocks "
                             "calls when the measured rate drops under 75%.",
            "risk": "The high hit rate comes from a wide stop: R:R on T1 is only ~0.25-0.4, so "
                    "break-even sits near 71-80%. Expectancy is shown as average R next to the "
                    "hit rate; a 75% hit rate with R:R 0.33 is still roughly break-even before "
                    "costs.",
            "optionHistory": "Historical option chains are not backtested (only the current chain "
                             "is available); the option-implied move affects live forecasts only.",
            "reproduce": "python verify.py  — independently pulls Upstox and diffs it against this "
                         "dashboard. python .research/segment_study2.py — re-runs the six-month "
                         "per-segment profile search.",
        },
    }


def _ist_today():
    return datetime.datetime.fromtimestamp(time.time() + 19800,
                                           datetime.timezone.utc).date()


def build_today():
    """Every call the desk's gates would have issued today, replayed bar by bar.

    Same 5m slice the live forecast runs on, same profile the desk picked per segment,
    same one-at-a-time cooldown. At bar i only bars <= i decide the call. If a segment's
    feed is down it is listed with its error and contributes no calls — nothing invented.
    """
    data = build_market()
    items = {i["id"]: i for i in data["instruments"]}
    tracked = [i for i in data["instruments"] if i.get("tradeable")]
    days = [datetime.datetime.fromtimestamp(it["candles"][-1]["t"] + 19800,
                                            datetime.timezone.utc).date()
            for it in tracked if it["timeframe"] == "5m" and it["candles"]]
    day = max(days) if days else _ist_today()
    calls, segments, as_of = [], [], 0
    for inst in INSTRUMENTS:
        if not inst.get("tradeable"):
            continue
        it = items.get(inst["id"]) or {}
        candles = it.get("candles") or []
        key = PROFILE_BY_ID[inst["id"]]
        profile = FORECAST_PROFILES[key]
        entry = {"id": inst["id"], "name": inst["name"], "profileKey": key,
                 "profile": profile["name"], "calls": 0, "t1": 0, "sl": 0, "open": 0,
                 "timeouts": 0, "winRate": None, "winRateResolved": None, "reach": 0,
                 "avgR": None, "avgT1Min": None, "avgSLMin": None, "avgMfe": None,
                 "error": None}
        segments.append(entry)
        if it.get("timeframe") != "5m" or len(candles) < 100:
            entry["error"] = it.get("error") or "no 5m candles from Upstox"
            continue
        try:
            audit = _validation(inst["id"], key, _audit_candles(inst), inst["minMove"]) or {}
        except Exception as exc:
            entry["error"] = f"audit: {exc}"
            continue
        measured = audit.get("winRate")
        measured_n = audit.get("samples") or 0
        if measured is None or measured_n < 30:
            entry["error"] = (f"six-month audit has only {measured_n} samples — "
                              "no calls until it is measurable")
            continue
        if measured < profile["minWinRate"]:
            entry["error"] = (f"six-month audit {measured}% is below the "
                              f"{profile['minWinRate']}% floor — no calls")
            continue
        try:
            res = cached(
                f"today:{inst['id']}:{key}:{candles[-1]['t']}:{len(candles)}", 30,
                lambda c=candles, k=key, mm=inst["minMove"], mx=inst["maxMove"]:
                    analysis.replay_session(c, mm, mx, FORECAST_PROFILES[k], day=day))
        except Exception as exc:
            entry["error"] = str(exc)
            continue
        for c in res["calls"]:
            c.update(id=inst["id"], name=inst["name"], profile=entry["profile"],
                     profileKey=key)
        entry.update(res["stats"])
        entry["auditWinRate"] = measured
        calls.extend(res["calls"])
        as_of = max(as_of, candles[-1]["t"])
    calls.sort(key=lambda c: (c["t"], c["id"]))
    return {
        "generatedAt": int(time.time()),
        "istClock": time.strftime("%Y-%m-%d %H:%M:%S", ist_now()),
        "day": day.isoformat(),
        "isToday": day == _ist_today(),
        "marketOpen": market_open(),
        "asOf": as_of or None,
        "feeds": data["feeds"],
        "scope": "Calls only for the six index segments: Sensex, Nifty, Bank Nifty, Fin Nifty, "
                 "Midcap Select, Next 50.",
        "totals": analysis.session_stats(calls),
        "segments": segments,
        "calls": calls,
        "method": {
            "scope": "Only Sensex, Nifty, Bank Nifty, Fin Nifty, Midcap Select and Next 50 "
                     "generate calls. Sector indices are chart context only.",
            "replay": "Bar by bar through the same gates as that segment's live profile: "
                      "unanimous indicators, ATR-expected move, entry cutoff, pre-move timing, "
                      "EMA 50 / extreme-RSI and measured capture where the profile asks. Only "
                      "bars up to each decision are used.",
            "outcome": "Session-bounded: the outcome window closes at 15:25 IST on the entry "
                       "day. T1 before stop = win. A call that hits neither level is a TIMEOUT "
                       "closed at the last price and counts as a non-win. A candle touching both "
                       "T1 and SL is scored as a stop (conservative).",
            "winRate": "Win rate = T1 hits ÷ all calls, including timeouts and still-open calls. "
                       "It is not computed only over resolved calls.",
            "profiles": "Each segment runs its own measured precision profile and only while its "
                        "six-month audit still measures >=75% T1-before-stop. Otherwise the "
                        "segment is listed with the blocking reason and contributes no calls.",
            "open": "A call whose session has not ended and whose levels are untouched is OPEN, "
                    "shown with elapsed minutes and MFE so far (not counted as a win).",
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _is_local(self):
        """True for a browser on this machine hitting the server directly (not via ngrok/nginx,
        which connect from loopback too but add X-Forwarded-* headers)."""
        fwd = (self.headers.get("X-Forwarded-For") or self.headers.get("X-Forwarded-Host")
               or self.headers.get("X-Real-IP") or self.headers.get("Forwarded"))
        return self.client_address[0] in ("127.0.0.1", "::1") and not fwd

    def _gate(self):
        """Site login protects PUBLIC links only; local use (localhost) never asks for a login."""
        _sync_token_from_env()          # a token saved to .env is applied on the very next request
        if self._is_local():
            return True
        user, password, _ = _auth_config()
        if not (user and password):
            return True
        header = self.headers.get("Authorization", "")
        supplied = ""
        if header.startswith("Basic "):
            try:
                supplied = base64.b64decode(header[6:].strip()).decode("utf-8")
            except Exception:
                supplied = ""
        if supplied and hmac.compare_digest(supplied, f"{user}:{password}"):
            return True
        self._send(401, json.dumps({"error": "site password required"}).encode(),
                   "application/json; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="Market Signal Desk", charset="UTF-8"'})
        return False

    def _admin(self, body_key=""):
        _, _, admin_key = _auth_config()
        if self._is_local():
            return True
        supplied = self.headers.get("X-Admin-Key", "") or body_key or ""
        if admin_key and supplied and hmac.compare_digest(supplied, admin_key):
            return True
        time.sleep(0.35)
        error = ("admin key not configured - set DESK_ADMIN_KEY in .env and restart server.py"
                 if not admin_key else
                 "wrong or missing admin key - open .env and copy DESK_ADMIN_KEY")
        self._send(401, json.dumps({"ok": False, "error": error}).encode(),
                   "application/json; charset=utf-8")
        return False

    def _send(self, code, body, ctype, extra=None):
        extra = dict(extra or {})
        if (len(body) > 1400 and "gzip" in (self.headers.get("Accept-Encoding") or "")
                and (ctype.startswith("application/json") or ctype.startswith("text/")
                     or "javascript" in ctype or "svg" in ctype)):
            import gzip
            body = gzip.compress(body, 5)               # ~650 KB of market JSON -> ~100 KB over the tunnel
            extra["Content-Encoding"] = "gzip"
            extra["Vary"] = "Accept-Encoding"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":          # for Render / uptime monitors; reveals nothing
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if not self._gate():
            return
        global _last_request
        _last_request = time.time()
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            return
        if parsed.path == "/api/token":
            if not self._admin():
                return
            try:
                body = json.dumps(token_status()).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if parsed.path == "/api/market":
            try:
                body = _market_bytes("1" in qs.get("force", []))
                if body is None:
                    _warming(self, "market data")
                else:
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

        if parsed.path == "/api/today":
            try:
                body = json.dumps(build_today()).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if parsed.path == "/api/active":
            try:
                res = _serve("active", build_active)
                if res is None:
                    _warming(self, "active calls")
                else:
                    self._send(200, json.dumps(dict(res, ageSeconds=_age("active"))).encode("utf-8"),
                               "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if parsed.path == "/api/upstox-usage":
            body = json.dumps(upstox.GOVERNOR.usage()).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        if parsed.path == "/api/recorder":
            try:
                import optrecorder
                body = json.dumps(dict(optrecorder.summary(sys.modules[__name__]), enabled=optrecorder.enabled(sys.modules[__name__]))).encode("utf-8")
                return self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}).encode("utf-8"), "application/json; charset=utf-8")
        if parsed.path == "/api/scorecard":
            try:
                res = _serve("score", _score_loader, 3600, 3600)
                if res is None:
                    _warming(self, "scorecard")
                    return
                if qs.get("format", ["json"])[0] == "csv":
                    import scorecard
                    self._send(200, scorecard.to_csv(res).encode("utf-8"), "text/csv; charset=utf-8",
                               {"Content-Disposition": 'attachment; filename="scorecard.csv"'})
                    return
                self._send(200, json.dumps(res).encode("utf-8"), "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if parsed.path == "/api/short":
            try:
                days = max(5, min(150, int(qs.get("days", ["60"])[0] or 60)))
                res = _serve("short", lambda: build_short(60)) if days == 60 else build_short(days)
                if res is None:
                    _warming(self, "short-term calls")
                    return
                if days == 60:
                    res = dict(res, ageSeconds=_age("short"))
                if qs.get("format", ["json"])[0] == "csv":
                    tf = qs.get("tf", ["all"])[0]
                    self._send(200, _short_csv(res, tf).encode("utf-8"), "text/csv; charset=utf-8",
                               {"Content-Disposition": f'attachment; filename="short_term_calls_{tf}.csv"'})
                    return
                self._send(200, json.dumps(res).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if parsed.path == "/api/overnight":
            try:
                res = build_overnight()
                if qs.get("format", ["json"])[0] == "csv":
                    days = max(1, min(1200, int(qs.get("days", ["365"])[0] or 365)))
                    cutoff = time.time() - days * 86400
                    res = dict(res, signals=[s for s in res["signals"]
                                             if s["ts"] >= cutoff])
                    self._send(200, _overnight_csv(res).encode("utf-8"),
                               "text/csv; charset=utf-8",
                               {"Content-Disposition":
                                f'attachment; filename="overnight-{days}d.csv"'})
                else:
                    self._send(200, json.dumps(res).encode("utf-8"),
                               "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if parsed.path == "/api/nifty50":
            try:
                rep = NIFTY50.snapshot("1" in qs.get("force", []))
                if qs.get("format", ["json"])[0] == "csv":
                    self._send(200, NIFTY50.csv(rep).encode("utf-8"),
                               "text/csv; charset=utf-8",
                               {"Content-Disposition":
                                'attachment; filename="nifty50-breadth-calls.csv"'})
                else:
                    self._send(200, json.dumps(rep).encode("utf-8"),
                               "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if parsed.path == "/api/stocktest":
            try:
                days = max(1, min(90, int(qs.get("days", ["30"])[0])))
                if qs.get("format", ["json"])[0] == "csv":
                    self._send(200, NIFTY50.month_csv(days).encode("utf-8"),
                               "text/csv; charset=utf-8",
                               {"Content-Disposition":
                                f'attachment; filename="nifty50-last{days}d-calls.csv"'})
                else:
                    self._send(200, json.dumps(NIFTY50.month_report(days)).encode("utf-8"),
                               "application/json; charset=utf-8")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return


        if parsed.path == "/api/calls":
            try:
                days = max(1, min(180, int(qs.get("days", ["30"])[0] or 30)))
                flt = qs.get("filter", ["all"])[0]
                res = filter_calls(build_recent_calls(days), flt)
                if qs.get("format", ["json"])[0] == "csv":
                    self._send(200, _calls_csv(res).encode("utf-8"),
                               "text/csv; charset=utf-8",
                               {"Content-Disposition":
                                f'attachment; filename="calls-{days}d-{res["filter"]}.csv"'})
                else:
                    self._send(200, json.dumps(res).encode("utf-8"),
                               "application/json; charset=utf-8")
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

    def do_POST(self):
        if not self._gate():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/token":
            self._send(404, b"Not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 16384:
            self._send(400, json.dumps({"ok": False, "error": "send a small JSON body"}).encode(),
                       "application/json; charset=utf-8")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send(400, json.dumps({"ok": False, "error": "body must be JSON"}).encode(),
                       "application/json; charset=utf-8")
            return
        if not self._admin(str(payload.get("adminKey") or "")):
            return
        try:
            res = apply_access_token(str(payload.get("token") or ""))
            self._send(200, json.dumps(res).encode("utf-8"), "application/json; charset=utf-8")
        except (ValueError, upstox.UpstoxError) as exc:
            self._send(400, json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as exc:
            self._send(500, json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"),
                       "application/json; charset=utf-8")


def main():
    if os.environ.get("REQUIRE_LOGIN") == "1":
        user, password, _ = _auth_config()
        if not (user and password):
            sys.exit("REFUSING TO START: REQUIRE_LOGIN=1 but DESK_USER / DESK_PASS are not set. This server is public, "
                     "so set both as environment variables (Render dashboard > Environment) and redeploy.")
    admin_key, created = desk_env.ensure_admin_key()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Market Signal Desk running at  http://localhost:{PORT}")
    left = upstox.token_seconds_left(TOKEN)
    print(f"Upstox token: {'missing' if not TOKEN else (f'valid ~{left // 3600}h {(left % 3600) // 60}m' if left and left > 0 else 'EXPIRED')}")
    print(f"Access panel key: {admin_key if created else 'saved in .env as DESK_ADMIN_KEY'}")
    site_user, site_pass, _ = _auth_config()
    if site_user and site_pass:
        print(f"Site login applies to public links only (user '{site_user}'); localhost needs no login")
    if LITE:
        sys.setswitchinterval(0.001)       # hand the interpreter lock to waiting request threads more often
    threading.Thread(target=_background_refresher, daemon=True, name="market-refresher").start()
    try:
        import optrecorder
        if optrecorder.enabled(sys.modules[__name__]):
            threading.Thread(target=optrecorder.loop, args=(sys.modules[__name__],), daemon=True, name="option-recorder").start()
            print("Option-chain recorder ON (set RECORD_OPTIONS=0 to turn off)")
    except Exception as exc:
        print("Option-chain recorder not started:", exc)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
