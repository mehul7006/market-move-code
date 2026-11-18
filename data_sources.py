"""Data sources: Yahoo Finance (real candles/spot for NSE/BSE indices) and NSE India public API (option chains, often bot-blocked)."""
import http.cookiejar
import json
import time
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def yahoo_chart(symbol, range_="6mo", interval="1d", timeout=12):
    """Real OHLC candles + live-ish spot for an index symbol (^NSEI, ^BSESN, ^CNXIT, ...)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={range_}&interval={interval}&includePrePost=false")
    res = _get_json(url, timeout)["chart"]["result"][0]
    meta = res["meta"]
    quote = res["indicators"]["quote"][0]
    stamps = res.get("timestamp") or []
    volumes = quote.get("volume") or [None] * len(stamps)
    candles = []
    for i, t in enumerate(stamps):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if None in (o, h, l, c):
            continue
        candles.append({"t": t, "open": o, "high": h, "low": l, "close": c, "volume": volumes[i]})
    return {
        "candles": candles,
        "spot": meta.get("regularMarketPrice"),
        "prevClose": meta.get("chartPreviousClose") or meta.get("previousClose"),
        "dayOpen": meta.get("regularMarketDayOpen"),
        "dayHigh": meta.get("regularMarketDayHigh"),
        "dayLow": meta.get("regularMarketDayLow"),
        "symbol": symbol,
    }


class NseClient:
    """Best effort. NSE wraps its JSON APIs in an Akamai challenge for many networks."""

    def __init__(self):
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._booted_at = 0.0
        self.last_error = None

    def _boot(self):
        self._opener.addheaders = [("User-Agent", _UA), ("Accept", "text/html,application/xhtml+xml"),
                                   ("Accept-Language", "en-US,en;q=0.9")]
        self._opener.open("https://www.nseindia.com/option-chain", timeout=12).read()
        self._booted_at = time.time()

    def get_json(self, path, referer="https://www.nseindia.com/"):
        try:
            if time.time() - self._booted_at > 240:
                self._boot()
            self._opener.addheaders = [("User-Agent", _UA), ("Accept", "application/json"), ("Referer", referer)]
            raw = self._opener.open("https://www.nseindia.com" + path, timeout=12).read()
            if raw.lstrip()[:1] == b"<":
                raise RuntimeError("NSE bot challenge (blocked network)")
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self.last_error = str(exc)
            raise

    def option_chain(self, symbol):
        return self.get_json(
            f"/api/option-chain-indices?symbol={urllib.parse.quote(symbol)}",
            referer="https://www.nseindia.com/option-chain",
        )
