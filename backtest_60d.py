"""Honest 60-day backtest on real Upstox 5m candles, with mutually exclusive outcome buckets.

Rules (per call, walk-forward, no lookahead, same gates/profiles as the live desk):
  * stop touched before T1                      -> SL only
  * T1 touched, then stop before T2             -> T1 only
  * T2 touched (even if stop/T1-level later)    -> T2 only (not counted in T1)
  * T3 touched                                  -> T3 only (not counted in T1/T2)
  * a candle touching both a target and the stop is scored as the stop (conservative)
  * a stop touched after a target never erases that target, and targets after a stop are ignored
  * no level touched by 15:25 IST               -> TIMEOUT (not a win)

Usage:  python backtest_60d.py [days]
"""
import datetime
import json
import sys

import analysis
import server

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def bucket(ex):
    if ex["outcome"] == "stop":
        return "SL"
    if ex["outcome"] == "timeout":
        return "TIMEOUT"
    if ex["outcome"] != "t1":
        return "OPEN"
    return {1: "T1", 2: "T2", 3: "T3"}[ex["targetsHit"]]


def main():
    server._sync_token_from_env()
    if not server.UX.token:
        sys.exit("No UPSTOX_ACCESS_TOKEN in .env")
    tot = {"calls": 0, "SL": 0, "T1": 0, "T2": 0, "T3": 0, "TIMEOUT": 0, "OPEN": 0}
    rows_out, per_seg = [], []
    for iid in server.TRADEABLE_IDS:
        inst = next(i for i in server.INSTRUMENTS if i["id"] == iid)
        server.AUDIT_DAYS[iid] = DAYS + 45      # warm-up for indicators / capture lookback
        candles = server._audit_candles(inst)
        if len(candles) < 200:
            per_seg.append({"id": iid, "error": f"only {len(candles)} real candles"})
            continue
        res = analysis.validate_forecast(
            candles, inst["minMove"], server.FORECAST_PROFILES[server.PROFILE_BY_ID[iid]],
            since_ts=candles[-1]["t"] - DAYS * 86400, examples_limit=None)
        seg = {"id": iid, "minMove": inst["minMove"], "calls": 0, "SL": 0, "T1": 0, "T2": 0,
               "T3": 0, "TIMEOUT": 0, "OPEN": 0,
               "first": datetime.datetime.fromtimestamp(candles[0]["t"] + 19800,
                                                        datetime.timezone.utc).strftime("%Y-%m-%d"),
               "last": datetime.datetime.fromtimestamp(candles[-1]["t"] + 19800,
                                                       datetime.timezone.utc).strftime("%Y-%m-%d")}
        for ex in (res or {}).get("examples", []):
            b = bucket(ex)
            seg["calls"] += 1
            seg[b] += 1
            tot["calls"] += 1
            tot[b] += 1
            stamp = datetime.datetime.fromtimestamp(ex["barTime"] + 19800, datetime.timezone.utc)
            rows_out.append({"segment": iid, "date": stamp.strftime("%Y-%m-%d"),
                             "time": stamp.strftime("%H:%M"), "side": ex["direction"],
                             "entry": ex["entry"], "sl": ex["stop"], "t1": ex["target1"],
                             "t2": ex["target2"], "t3": ex["target3"], "bucket": b})
        per_seg.append(seg)

    def pct(n, d):
        return f"{n / d * 100:5.1f}%" if d else "   n/a"

    print(f"\n=== {DAYS}-day honest backtest, real Upstox 5m candles ===")
    hdr = f"{'segment':<11}{'calls':>6}{'SL':>5}{'T1':>5}{'T2':>5}{'T3':>5}{'T/O':>5}   " \
          f"{'SL%':>6}{'T1%':>7}{'T2%':>7}{'T3%':>7}{'T1+%':>7}"
    print(hdr)
    for s in per_seg + [dict(tot, id="ALL")]:
        if s.get("error"):
            print(f"{s['id']:<11} ERROR {s['error']}")
            continue
        n = s["calls"]
        print(f"{s['id']:<11}{n:>6}{s['SL']:>5}{s['T1']:>5}{s['T2']:>5}{s['T3']:>5}{s['TIMEOUT']:>5}   "
              f"{pct(s['SL'], n):>6}{pct(s['T1'], n):>7}{pct(s['T2'], n):>7}{pct(s['T3'], n):>7}"
              f"{pct(s['T1'] + s['T2'] + s['T3'], n):>7}")
    print("T1/T2/T3 columns are exclusive buckets (highest target reached before SL). "
          "T1+% = any target before SL.")
    with open("backtest_60d_result.json", "w") as fh:
        json.dump({"days": DAYS, "segments": per_seg, "total": tot, "calls": rows_out}, fh, indent=1)


if __name__ == "__main__":
    main()
