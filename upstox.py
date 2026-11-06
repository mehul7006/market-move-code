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


class UpstoxClient:
    def __init__(self, token):
        self.token = token

    def _get(self, url, timeout=25):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "User-Agent": _UA,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")[:300]
            raise UpstoxError(f"HTTP {exc.code}: {body}") from None
        except Exception as exc:
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
