"""Audit ANY list of calls (for example from a Telegram group) against REAL Upstox 1-minute candles.

    python audit_calls.py calls.csv

calls.csv columns (header required, extra columns ignored):
    date        YYYY-MM-DD
    time        HH:MM        IST time the call was given
    index       NIFTY | BANKNIFTY | SENSEX | FINNIFTY | MIDCPNIFTY | NEXT50
    side        BUY | SELL
    entry       price (leave blank to use the 1-minute close at that time)
    sl          stop-loss price
    t1,t2,t3    target prices (t2, t3 optional)
    minutes     optional, how long the call stays valid (default 120)

For every call it reports which level was touched FIRST, and when, on the real minute path (a minute that touches both the stop and a
target is scored as the stop). Then it prints the numbers three ways, so claims can be compared like with like:
    * win rate           calls that reached T1 before the stop
    * bracket points     exit at T1 (or the stop)           <- what you could actually book
    * hindsight points   the best price reached after the call (NOT tradeable - this is how many "average per call" claims are built)
Points are INDEX points. If a group quotes OPTION premium points, convert: an at-the-money option moves roughly half as much as the index.
Calls older than the available 1-minute history (about 6 months) cannot be checked and are listed as such.
"""
import csv
import datetime
import sys
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import server

IST = datetime.timezone.utc


def to_ts(date, hhmm):
    d = datetime.datetime.strptime(date.strip() + " " + hhmm.strip(), "%Y-%m-%d %H:%M")
    return int(d.replace(tzinfo=IST).timestamp()) - 19800


def clock(t):
    return datetime.datetime.fromtimestamp(t + 19800, IST).strftime("%H:%M") if t else "-"


def num(x):
    x = (x or "").strip().replace(",", "")
    return float(x) if x else None


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    server._sync_token_from_env()
    rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8-sig")))
    cache = {}
    results = []
    print(f"{'date':<11}{'time':<6}{'index':<11}{'side':<5}{'entry':>10}{'SL':>10}{'T1':>10} | {'first touch':<22}{'T1 at':>7}{'T2 at':>7}{'T3 at':>7}{'SL at':>7} | outcome")
    for r in rows:
        idx = r["index"].strip().upper()
        inst = next((x for x in server.INSTRUMENTS if x["id"] == idx), None)
        if not inst:
            print(f"{r['date']:<11}{r['time']:<6}{idx:<11} unknown index"); continue
        if idx not in cache:
            cache[idx] = server._hist_1m(inst)
        bars = cache[idx]
        t0 = to_ts(r["date"], r["time"])
        window = int(num(r.get("minutes")) or 120) * 60
        path = [b for b in bars if t0 <= b["t"] < t0 + window]
        if len(path) < 3:
            print(f"{r['date']:<11}{r['time']:<6}{idx:<11}{r['side']:<5} NO 1-MINUTE DATA for this time (older than the history, or a holiday)"); continue
        sign = 1 if r["side"].strip().upper() in ("BUY", "LONG") else -1
        entry = num(r.get("entry")) or path[0]["close"]
        sl, t1, t2, t3 = num(r.get("sl")), num(r.get("t1")), num(r.get("t2")), num(r.get("t3"))
        touched, first, stopped, best = {}, None, False, 0.0
        for b in path:
            hi = (b["high"] - entry) * sign if sign > 0 else (entry - b["low"])
            lo = (b["low"] - entry) if sign > 0 else (entry - b["high"])
            best = max(best, hi)
            hit_sl = sl is not None and (lo <= -(abs(entry - sl)))
            if hit_sl and not stopped:
                stopped = True
                touched["SL"] = b["t"]
                if first is None:
                    first = "SL"
                break
            for name, lvl in (("T1", t1), ("T2", t2), ("T3", t3)):
                if lvl is not None and name not in touched and hi >= abs(lvl - entry):
                    touched[name] = b["t"]
                    if first is None:
                        first = name
        win = "T1" in touched
        t1pts = abs(t1 - entry) if t1 else 0.0
        slpts = abs(entry - sl) if sl else 0.0
        sl_first = first == "SL"                                  # the stop came before any target
        bracket = t1pts if win else (-slpts if sl_first else (path[-1]["close"] - entry) * sign)
        outcome = ("WIN (T1 before SL)" if win else "STOP-LOSS first" if sl_first else "neither level in the window")
        results.append({"win": win, "stopped": sl_first, "bracket": bracket, "best": best})
        print(f"{r['date']:<11}{r['time']:<6}{idx:<11}{r['side'].strip().upper():<5}{entry:>10.2f}{(sl or 0):>10.2f}{(t1 or 0):>10.2f} | {(first or '-'):<22}"
              f"{clock(touched.get('T1')):>7}{clock(touched.get('T2')):>7}{clock(touched.get('T3')):>7}{clock(touched.get('SL')):>7} | {outcome}")
    n = len(results)
    if not n:
        return
    wins = sum(1 for x in results if x["win"])
    print("\n" + "=" * 100)
    print(f"{n} calls checked on real data")
    print(f"  win rate (T1 before the stop)          : {wins / n * 100:5.1f}%   (stop-outs {sum(1 for x in results if x['stopped']) / n * 100:.1f}%)")
    print(f"  average points per call, bracket       : {sum(x['bracket'] for x in results) / n:+7.1f}   <- exit at T1 or the stop: what you could actually book")
    print(f"  average points per call, hindsight     : {sum(x['best'] for x in results) / n:+7.1f}   <- best price reached after the call: NOT tradeable")
    print("  (index points; an at-the-money option moves roughly half as much as the index)")


if __name__ == "__main__":
    main()
