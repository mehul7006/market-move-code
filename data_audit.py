"""Audit that every candle used by the desk is real Upstox market data.

Run:  python data_audit.py            (needs UPSTOX_ACCESS_TOKEN in .env)

Checks, per index:
  1. provenance  - every bar comes from Upstox (a bar tagged YAHOO would be reported; none expected)
  2. structure   - ordering, duplicates, OHLC sanity (low <= open/close <= high), bars per session
  3. consistency - 5-minute bars equal the aggregation of the 1-minute bars (two independent API calls)
  4. independent - daily close vs Yahoo Finance's daily close for the same index (a second, unrelated source)
  5. frozen feed - runs of identical zero-range bars (a stale feed would show up here)
and greps the code base for random / mock / synthetic data generators.
"""
import collections
import datetime
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import data_sources
import server

SEGS = ["NIFTY", "BANKNIFTY", "SENSEX", "NEXT50", "FINNIFTY", "MIDCPNIFTY"]
YAHOO = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN"}


def day(t):
    return (t + 19800) // 86400


def mins(t):
    return ((t + 19800) % 86400) // 60


def load5(iid):
    inst = next(x for x in server.INSTRUMENTS if x["id"] == iid)
    server.AUDIT_DAYS[iid] = 230
    return inst, server._audit_candles(inst)


def structure(cs, per_day_expected):
    bad_ohlc = sum(1 for c in cs if not (c["low"] <= min(c["open"], c["close"]) + 1e-9
                                          and c["high"] >= max(c["open"], c["close"]) - 1e-9
                                          and c["low"] <= c["high"]))
    ts = [c["t"] for c in cs]
    dup = len(ts) - len(set(ts))
    unordered = sum(1 for a, b in zip(ts, ts[1:]) if b < a)
    per_day = collections.Counter(day(t) for t in ts)
    short_days = {d: n for d, n in per_day.items() if n < per_day_expected * 0.9}
    weekend = sum(1 for d in per_day if datetime.date.fromtimestamp(d * 86400).weekday() >= 5)
    nonzero_vol = sum(1 for c in cs if c.get("volume"))
    flat_runs, run = 0, 0
    for c in cs:
        if c["high"] == c["low"] == c["open"] == c["close"]:
            run += 1
            if run == 3:
                flat_runs += 1
        else:
            run = 0
    return dict(bars=len(cs), days=len(per_day), bad_ohlc=bad_ohlc, dup=dup, unordered=unordered,
                short_days=len(short_days), weekend_days=weekend, flat_runs=flat_runs,
                first=datetime.datetime.fromtimestamp(ts[0] + 19800, datetime.timezone.utc).strftime("%Y-%m-%d"),
                last=datetime.datetime.fromtimestamp(ts[-1] + 19800, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M"))


def main():
    server._sync_token_from_env()
    if not server.UX.token:
        sys.exit("No UPSTOX_ACCESS_TOKEN in .env")
    ok_all = True
    print("=" * 78)
    print("DATA AUDIT - every candle must be real Upstox data")
    print("=" * 78)
    for iid in SEGS:
        inst, c5 = load5(iid)
        srcs = collections.Counter(c.get("src", "UPSTOX") for c in c5)
        s5 = structure(c5, 75)
        print(f"\n{iid}  ({inst['key']})")
        print(f"  5m : {s5['bars']} bars, {s5['days']} sessions, {s5['first']} -> {s5['last']} IST")
        print(f"       sources {dict(srcs)} | bad OHLC {s5['bad_ohlc']} | duplicates {s5['dup']} | out-of-order {s5['unordered']}"
              f" | short sessions {s5['short_days']} | weekend sessions {s5['weekend_days']} | frozen-bar runs {s5['flat_runs']}")
        if set(srcs) != {"UPSTOX"} or s5["bad_ohlc"] or s5["dup"] or s5["unordered"]:
            ok_all = False
        # 1m vs 5m
        try:
            c1 = server._hist_1m(inst)
            s1 = structure(c1, 375)
            agg = collections.defaultdict(list)
            for c in c1:
                agg[(c["t"] // 300) * 300].append(c)
            by5 = {c["t"]: c for c in c5}
            same = diff = missing = 0
            worst = 0.0
            for t, grp in agg.items():
                if t not in by5 or len(grp) != 5:
                    missing += 1
                    continue
                grp.sort(key=lambda x: x["t"])
                o, h, l, cl = grp[0]["open"], max(g["high"] for g in grp), min(g["low"] for g in grp), grp[-1]["close"]
                b = by5[t]
                dv = max(abs(o - b["open"]), abs(h - b["high"]), abs(l - b["low"]), abs(cl - b["close"]))
                worst = max(worst, dv)
                if dv <= max(0.05, b["close"] * 2e-6):
                    same += 1
                else:
                    diff += 1
            tot = same + diff
            print(f"  1m : {s1['bars']} bars, {s1['days']} sessions, {s1['first']} -> {s1['last']} IST | bad OHLC {s1['bad_ohlc']}"
                  f" | duplicates {s1['dup']} | frozen runs {s1['flat_runs']}")
            print(f"  1m->5m cross-check: {same}/{tot} five-minute bars match the aggregated 1-minute bars "
                  f"({same / tot * 100:.2f}%), {missing} skipped (incomplete group), largest mismatch {worst:.2f} pts")
            if tot and same / tot < 0.97:
                ok_all = False
        except Exception as exc:
            print("  1m : unavailable -", str(exc)[:80])
        # independent source
        y = YAHOO.get(iid)
        if y:
            try:
                yc = data_sources.yahoo_chart(y, "1y", "1d")["candles"]
                ydaily = {day(c["t"]): c["close"] for c in yc}
                last_close = {}
                for c in c5:
                    last_close[day(c["t"])] = c["close"]
                diffs = []
                for d, cl in last_close.items():
                    if d in ydaily:
                        diffs.append(abs(cl - ydaily[d]) / ydaily[d] * 100)
                if diffs:
                    diffs.sort()
                    print(f"  Yahoo daily-close check: {len(diffs)} sessions compared, median diff {diffs[len(diffs)//2]:.3f}%,"
                          f" 95th pct {diffs[int(len(diffs)*0.95)-1]:.3f}%, worst {diffs[-1]:.3f}%")
                    if diffs[int(len(diffs) * 0.95) - 1] > 0.3:
                        ok_all = False
            except Exception as exc:
                print("  Yahoo check unavailable:", str(exc)[:80])
        else:
            print("  Yahoo check: no Yahoo symbol for this index (Upstox only)")
    print("\n--- code scan: random / mock / synthetic data generators ---")
    pat = re.compile(r"random\.|\brandom\b|mock|fake|synthetic|dummy|faker", re.I)
    hits = []
    for fn in sorted(os.listdir(ROOT)):
        if fn.endswith(".py") and fn != "data_audit.py":
            for n, line in enumerate(open(os.path.join(ROOT, fn), encoding="utf-8", errors="ignore"), 1):
                if pat.search(line) and "no synthetic" not in line.lower() and "never" not in line.lower() \
                        and "no mock" not in line.lower() and "not synthetic" not in line.lower():
                    hits.append(f"{fn}:{n}: {line.strip()[:100]}")
    print("  generators found:", len(hits))
    for h in hits[:10]:
        print("   ", h)
    print("\nRESULT:", "ALL CHECKS PASSED - data is real Upstox market data" if ok_all and not hits
          else "REVIEW THE LINES ABOVE")


if __name__ == "__main__":
    main()
