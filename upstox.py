"""Upstox API client - real-time quotes, v3 candles, option chain with OI and Greeks."""
from __future__ import annotations

import base64
import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.upstox.com/v2"
BASE3 = "https://api.upstox.com/v3"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class UpstoxError(RuntimeError):
    pass


class CallGovernor:
    """Keeps the app far below Upstox's request limits (standard plan: 50/second, 500/minute, 2000 per 30 minutes).

    * a local budget (default 300 a minute, 1,500 per 30 minutes - override with UPSTOX_MAX_PER_MIN / UPSTOX_MAX_PER_30MIN): when it is used up the call is
      not sent; the app keeps showing its last good data (see server.cached) and tries again when the window frees up;
    * a 429 from Upstox starts a cool-down (its Retry-After, at least 30 s, doubling on repeats up to 5 min) during which no call is sent;
    * usage counters per endpoint (see /api/upstox-usage).
    """

    def __init__(self):
        import collections
        import threading
        self.lock = threading.Lock()
        self.stamps = collections.deque()          # (time, endpoint)
        self.cool_until = 0.0
        self.cool_step = 30.0
        self.counts = collections.Counter()
        self.blocked = 0
        self.limited = 0                           # 429s received
        self.max_min = int(os.environ.get("UPSTOX_MAX_PER_MIN", "300"))
        self.max_30 = int(os.environ.get("UPSTOX_MAX_PER_30MIN", "1500"))

    @staticmethod
    def endpoint(url):
        path = urllib.parse.urlparse(url).path
        return "/".join(path.split("/")[2:5]) or path

    def acquire(self, url):
        now = time.time()
        with self.lock:
            if now < self.cool_until:
                self.blocked += 1
                raise UpstoxError("Upstox call skipped: cooling down after a rate-limit response (%ds left)" % int(self.cool_until - now))
            while self.stamps and now - self.stamps[0][0] > 1800:
                self.stamps.popleft()
            last_min = sum(1 for t, _ in self.stamps if now - t <= 60)
            if last_min >= self.max_min or len(self.stamps) >= self.max_30:
                self.blocked += 1
                raise UpstoxError("Upstox call skipped: local request budget used (%d in the last minute, %d in 30 minutes)" % (last_min, len(self.stamps)))
            ep = self.endpoint(url)
            self.stamps.append((now, ep))
            self.counts[ep] += 1

    def auth_failed(self):
        """HTTP 401: the token is expired / invalid. Stop asking for a while (no point hammering Upstox) - a newly saved token calls reset()."""
        with self.lock:
            self.cool_until = max(self.cool_until, time.time() + 120.0)

    def reset(self):
        with self.lock:
            self.cool_until, self.cool_step = 0.0, 30.0

    def rate_limited(self, retry_after=None):
        with self.lock:
            self.limited += 1
            wait = max(float(retry_after or 0), self.cool_step)
            self.cool_until = time.time() + wait
            self.cool_step = min(self.cool_step * 2, 300.0)

    def ok(self):
        with self.lock:
            if time.time() > self.cool_until:
                self.cool_step = 30.0

    def usage(self):
        now = time.time()
        with self.lock:
            per = {}
            for t, ep in self.stamps:
                d = per.setdefault(ep, [0, 0])
                d[1] += 1
                if now - t <= 60:
                    d[0] += 1
            return {"lastMinute": sum(v[0] for v in per.values()), "last30Minutes": len(self.stamps), "byEndpoint": {k: {"lastMinute": v[0], "last30Minutes": v[1]} for k, v in per.items()},
                    "totalSinceStart": sum(self.counts.values()), "skippedByBudget": self.blocked, "rateLimitResponses": self.limited,
                    "coolingDownSeconds": max(0, int(self.cool_until - now)), "budget": {"perMinute": self.max_min, "per30Minutes": self.max_30}}


GOVERNOR = CallGovernor()


def load_env(path):
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def token_seconds_left(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return int(exp - time.time())
    except Exception:
        return None


def exchange_code(code, redirect_uri, key, secret):
    """Swap a login `code` for an access token (Upstox v2 token endpoint)."""
    body = urllib.parse.urlencode({
        "code": code, "client_id": key, "client_secret": secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(BASE + "/login/authorization/token", data=body,
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded",
                                          "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpstoxError("Token exchange failed: "
                          + exc.read().decode("utf-8", "ignore")[:300]) from None
    except Exception as exc:
        raise UpstoxError("Token exchange failed: " + str(exc)) from None
    token = (res or {}).get("access_token")
    if not token:
        raise UpstoxError("No access_token in Upstox response")
    return token


class UpstoxClient:
    def __init__(self, token):
        self.token = token

    def _get(self, url, timeout=25):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "User-Agent": _UA,
        })
        data = None
        for attempt in range(3):
            # transient network blips (connection reset, 5xx) are retried; a 429 starts a cool-down instead of hammering the API
            GOVERNOR.acquire(url)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                GOVERNOR.ok()
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "ignore")[:300]
                if exc.code == 401:
                    GOVERNOR.auth_failed()
                    raise UpstoxError(f"HTTP 401: {body}") from None
                if exc.code == 429:
                    GOVERNOR.rate_limited(exc.headers.get("Retry-After"))
                    raise UpstoxError(f"HTTP 429: rate limit - pausing Upstox calls: {body}") from None
                if exc.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise UpstoxError(f"HTTP {exc.code}: {body}") from None
            except Exception as exc:
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise UpstoxError(str(exc)) from None
        if isinstance(data, dict) and data.get("status") == "error":
            errs = data.get("errors") or [{}]
            raise UpstoxError(errs[0].get("message") or "Upstox API error")
        return data.get("data") if isinstance(data, dict) else data

    # ---------------------------------------------------------------- market data

    def quotes(self, keys):
        url = BASE + "/market-quote/quotes?instrument_key=" + urllib.parse.quote(",".join(keys), safe="")
        data = self._get(url) or {}
        out = {}
        for v in data.values():
            last = v.get("last_price")
            net = v.get("net_change")
            ohlc = v.get("ohlc") or {}
            out[v["instrument_token"]] = {
                "last": last,
                "netChange": net,
                "prevClose": round(last - net, 2) if (last is not None and net is not None) else None,
                "open": ohlc.get("open"), "high": ohlc.get("high"), "low": ohlc.get("low"),
                "quoteAt": v.get("last_trade_time"),
            }
        return out

    def candles(self, key, unit, interval, to_date, from_date):
        url = (f"{BASE3}/historical-candle/{urllib.parse.quote(key, safe='')}/"
               f"{unit}/{interval}/{to_date}/{from_date}")
        data = self._get(url) or {}
        return self._normalise(data.get("candles") or [])

    def intraday(self, key, unit="minutes", interval=5):
        url = (f"{BASE3}/historical-candle/intraday/{urllib.parse.quote(key, safe='')}/"
               f"{unit}/{interval}")
        data = self._get(url) or {}
        return self._normalise(data.get("candles") or [])

    @staticmethod
    def _normalise(rows):
        out = []
        for r in rows:
            try:
                ts = int(datetime.datetime.fromisoformat(r[0]).timestamp())
                out.append({"t": ts, "open": float(r[1]), "high": float(r[2]),
                            "low": float(r[3]), "close": float(r[4]), "volume": r[5]})
            except Exception:
                continue
        out.sort(key=lambda c: c["t"])
        return out

    def profile(self):
        """Cheapest call that proves a token is accepted by Upstox."""
        return self._get(BASE + "/user/profile") or {}

    # ---------------------------------------------------------------- options

    def expiries(self, key):
        data = self._get(BASE + "/option/contract?instrument_key=" + urllib.parse.quote(key, safe="")) or []
        today = datetime.date.today().isoformat()
        return sorted({c["expiry"] for c in data if c.get("expiry") and c["expiry"] > today})

    def chain(self, key, expiry):
        url = (BASE + "/option/chain?instrument_key=" + urllib.parse.quote(key, safe="")
               + "&expiry_date=" + expiry)
        return self._get(url) or []

    def chain_as_records(self, key, expiry):
        """Convert Upstox chain rows to the records.data shape analysis.analyze_chain expects."""
        rows = self.chain(key, expiry)
        out = []
        for r in rows:
            ce = r.get("call_options") or {}
            pe = r.get("put_options") or {}
            cmd, pmd = ce.get("market_data") or {}, pe.get("market_data") or {}
            cg, pg = ce.get("option_greeks") or {}, pe.get("option_greeks") or {}
            if not cmd and not pmd:
                continue

            def leg(md, gr):
                oi = md.get("oi") or 0
                prev = md.get("prev_oi") or 0
                return {
                    "oi": oi, "chnginOI": oi - prev, "volume": md.get("volume") or 0,
                    "ltp": md.get("ltp"),
                    "iv": gr.get("iv") or None,
                    "delta": gr.get("delta"), "gamma": gr.get("gamma"),
                    "theta": gr.get("theta"), "vega": gr.get("vega"),
                }

            out.append({"strike": r["strike_price"],
                        "CE": {expiry: leg(cmd, cg)},
                        "PE": {expiry: leg(pmd, pg)}})
        return {"records": {"expiryDate": [expiry], "data": out,
                            "underlyingValue": rows[0].get("underlying_spot_price") if rows else None}}
