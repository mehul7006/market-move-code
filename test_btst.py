"""Checks for the BTST next-open model (btst.py) and its server plumbing.

    python test_btst.py

Uses the daily history the server cached (.candle_cache/btst_*_daily.json, written the first time
/api/btst is opened). Nothing here touches the intraday call engines.
"""
import datetime
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import btst

CACHE = os.environ.get("CACHE_DIR") or os.path.join(ROOT, ".candle_cache")
PEERS = ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "NEXT50", "MIDCPNIFTY")
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def load(iid):
    path = os.path.join(CACHE, f"btst_{iid}_daily.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["rows"]


def test_gate():
    print("gate")
    s = {"side": "LONG", "grade": "A"}
    good = {"samples": 100, "winRate": 78.0, "recent": {"samples": 25, "winRate": 75.0}}
    check(btst.gate(s, good, 0.7)[0], "all gates pass -> SURE")
    check(not btst.gate(s, dict(good, samples=10), 0.7)[0], "too few samples -> not sure")
    check(not btst.gate(s, dict(good, winRate=66.0), 0.7)[0], "hit rate under 70% -> not sure")
    check(not btst.gate(s, dict(good, recent={"samples": 25, "winRate": 60.0}), 0.7)[0], "recent under 65% -> not sure")
    check(not btst.gate(s, good, 0.55)[0], "model disagrees -> not sure")
    check(not btst.gate(s, good, None)[0], "no model -> not sure")
    sh = {"side": "SHORT", "grade": "A"}
    check(btst.gate(sh, good, 0.3)[0] and not btst.gate(sh, good, 0.5)[0], "short side needs a low gap-up probability")


def test_setup():
    print("setup")
    base = {"breadthPeers": 5, "aboveEma": True, "vixKnown": True,
            "f": {"body": 0.8, "breadth": 0.7, "vixChg": -0.2}}
    check(btst.setup_at(base) == {"side": "LONG", "grade": "A"}, "momentum close + VIX down -> LONG A")
    check(btst.setup_at(dict(base, f=dict(base["f"], vixChg=0.1))) == {"side": "LONG", "grade": "B"}, "VIX up -> LONG B")
    check(btst.setup_at(dict(base, vixKnown=False)) == {"side": "LONG", "grade": "B"}, "VIX unknown -> never Grade A")
    check(btst.setup_at(dict(base, aboveEma=False)) is None, "below the 20-day average -> no LONG")
    check(btst.setup_at(dict(base, breadthPeers=2)) is None, "breadth from fewer than 3 indices -> no setup")
    check(btst.setup_at(dict(base, f=dict(base["f"], body=0.5))) is None, "body under 0.6 ATR -> no setup")
    short = {"breadthPeers": 5, "aboveEma": False, "vixKnown": True,
             "f": {"body": -0.9, "breadth": -0.8, "vixChg": 0.3}}
    check(btst.setup_at(short) == {"side": "SHORT", "grade": "A"}, "mirror -> SHORT A")


def test_no_lookahead(peers, vix):
    print("no look-ahead (NIFTY)")
    rows = peers["NIFTY"]
    full = btst.prepare(rows, vix, peers)
    probs_full, _, _ = btst.walk_forward(full)
    cut = 40
    trunc_rows = rows[:-cut]
    tpeers = {k: v[:-cut] if k == "NIFTY" else v for k, v in peers.items()}
    part = btst.prepare(trunc_rows, vix, tpeers)
    probs_part, _, _ = btst.walk_forward(part)
    n = len(part)
    same_f = all(part[k]["f"] == full[k]["f"] for k in range(n))
    check(same_f, f"features of the first {n} sessions are identical when the newest {cut} bars are removed")
    same_p = all((probs_part[k] is None and probs_full[k] is None) or
                 (probs_part[k] is not None and abs(probs_part[k] - probs_full[k]) < 1e-12) for k in range(n))
    check(same_p, "every walk-forward probability is identical when future bars are removed")
    led_full = [t for t in btst.sure_ledger(full, probs_full) if t["ts"] < part[-1]["ts"]]
    led_part = btst.sure_ledger(part, probs_part)
    check([t["date"] for t in led_full] == [t["date"] for t in led_part],
          "the causal SURE ledger is identical when future bars are removed")
    check(part[-1]["outcome"] is None and full[n - 1]["outcome"] is not None,
          "the newest session has no outcome until the next bar exists")


def test_book(peers, vix):
    print("strategy book")
    P = btst.PARAMS
    check(btst.call_tier(72, 0.65) == "SURE", "confidence 72% + model 65% -> SURE")
    check(btst.call_tier(72, 0.55) is None, "model under 60% -> no call even at 72% confidence")
    check(btst.call_tier(63, 0.62) == "HIGH", "confidence 63% + model 62% -> HIGH")
    check(btst.call_tier(58, 0.9) is None, "confidence under 60% -> no call")
    check(btst.shrunk_rate(10, 10) < 80, "10/10 wins is shrunk well below 100% (small samples cannot look sure)")
    check(btst.strategy_confidence([(0, True)] * 10, 100) is None, "fewer than 25 past setups -> no confidence")
    rows = peers["NIFTY"]
    full = btst.prepare(rows, vix, peers)
    pf, _, _ = btst.walk_forward(full)
    cut = 60
    tpeers = {k: v[:-cut] if k == "NIFTY" else v for k, v in peers.items()}
    part = btst.prepare(rows[:-cut], vix, tpeers)
    pp, _, _ = btst.walk_forward(part)
    cf, _, _ = btst.strategy_book(full, pf)
    cp, _, _ = btst.strategy_book(part, pp)
    key = lambda c: (c["date"], c["tier"], c["confidence"], c["strategy"])
    check([key(c) for c in cf if c["ts"] < part[-1]["ts"]] == [key(c) for c in cp],
          "every past call, tier and confidence is identical when the newest 60 bars are removed")
    pool = []
    for iid in ("NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY"):
        f = btst.prepare(peers[iid], vix, peers)
        pr, _, _ = btst.walk_forward(f)
        pool += btst.strategy_book(f, pr)[0]
    for label, sel in (("all years", pool), ("2023+", [c for c in pool if c["date"] >= "2023"]),
                       ("2025+", [c for c in pool if c["date"] >= "2025"])):
        rate = sum(c["win"] for c in sel) / len(sel) * 100
        print(f"     traded four, {label}: {len(sel)} calls, {rate:.1f}% opened in favour")
        check(rate >= 60, f"BTST calls on Nifty/Sensex/Bank/Fin win >= 60% ({label})")


def test_analyse(peers, vix):
    print("analyse (all indices)")
    for iid in PEERS:
        t = time.time()
        r = btst.analyse(peers[iid], vix, peers)
        if not r["ok"]:
            check(False, f"{iid}: {r['reason']}")
            continue
        ss, la, sa = r["sureStats"], r["audit"]["tiers"]["LONG_A"], r["audit"]["tiers"]["SHORT_A"]
        print(f"     {iid:11s} {time.time() - t:.2f}s  SURE {ss['samples']} @ {ss['winRate']}% (2y {ss['recent']['winRate']}%)"
              f"  LONG_A {la['winRate']}%  SHORT_A {sa['winRate']}%  base {r['audit']['baseUpRate']}%")
        check(all(t["win"] == (t["pts"] > 0) for t in r["sureLedger"]), f"{iid}: win means the open was beyond the entry")
        p = r["prediction"]
        check(p["probUp"] is None or 0 <= p["probUp"] <= 1, f"{iid}: probability in range")
        if r["sure"]:
            check(r["setup"] is not None and not r["blockReasons"], f"{iid}: SURE implies a setup and no block reason")
        b = r["book"]
        check(all(c["tier"] in ("SURE", "HIGH") and c["confidence"] >= 60 for c in b["calls"]),
              f"{iid}: every book call has >= 60% confidence")
        print(f"     {'':11s} book: {b['stats']['ALL']['samples']} calls @ {b['stats']['ALL']['winRate']}%"
              f"  last year {b['stats']['ALL']['lastYear']['winRate']}%  live tier {b['tier']}")


def test_server_bars():
    print("server: missing sessions filled from real 5m bars")
    import server
    ist = server.IST

    def ts(d, h, m):
        return int(datetime.datetime(d.year, d.month, d.day, h, m, tzinfo=ist).timestamp())

    d1, d2, d3 = datetime.date(2026, 11, 20), datetime.date(2026, 11, 23), datetime.date(2026, 11, 25)
    hist = {"rows": [{"t": server._ist_midnight(d1), "open": 100, "high": 101, "low": 99, "close": 100.5}]}
    candles = []
    for d, close_at in ((d2, (15, 25)), (d3, (13, 0))):
        t = ts(d, 9, 15)
        end = ts(d, *close_at)
        px = 100.0
        while t <= end:
            candles.append({"t": t, "open": px, "high": px + 1, "low": px - 1, "close": px + 0.2})
            px += 0.2
            t += 300
    bars, agg = server._btst_bars(hist, {"candles": candles}, d3)
    days = [server._ist_date(b["t"]) for b in bars]
    check(days == [d1, d2, d3], "a lagging EOD session (23rd) and today's forming bar (25th) are both present")
    check(bars[1]["src"] == "UPSTOX 5m->1D" and agg and not agg["complete"], "filled bars are labelled and today is forming")
    # a partial (not full-session) past day must not be invented into a daily bar
    partial = [c for c in candles if server._ist_date(c["t"]) == d2 and c["t"] >= ts(d2, 13, 0)]
    bars2, _ = server._btst_bars(hist, {"candles": partial}, d3)
    check([server._ist_date(b["t"]) for b in bars2] == [d1], "a past day with only part of its session is not used")


def main():
    test_gate()
    test_setup()
    try:
        peers = {k: load(k) for k in PEERS}
        vix = load("INDIAVIX")
    except FileNotFoundError:
        print("(no cached daily history yet - open /api/btst once, then re-run for the data tests)")
        peers = None
    if peers:
        test_no_lookahead(peers, vix)
        test_book(peers, vix)
        test_analyse(peers, vix)
    test_server_bars()
    print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
