"""Back-test the CONFIDENCE-tier calls (shown on their own page, never counted with the sure calls) on real Upstox candles.

    python test_conf_tier.py 120

Runs the same engine the website uses (strict.prepare -> shortterm replay on the real 1-minute path) and lists every confidence call of the last N days:
generation time, entry, SL / T1 / T2 / T3 with the time each was hit, result and points. The same move on several indexes at the same moment is
shown for each index but counted once ("group"). Writes conf_tier_test_<N>d_calls.csv and prints the summary.
"""
import collections
import csv
import datetime
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import server
import shortterm as st
import strict

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 120


def clock(t):
    return datetime.datetime.fromtimestamp(t + 19800, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M") if t else ""


def main():
    server._sync_token_from_env()
    now = time.time()
    c5, m1 = {}, {}
    for seg in strict.SEGMENTS:
        inst = next(x for x in server.INSTRUMENTS if x["id"] == seg)
        c5[seg], m1[seg] = server._candles_days(inst, 300), server._hist_1m(inst)
    series = {1: m1, 5: c5, 15: {k: st.resample(v, 15) for k, v in c5.items()}}
    st.prepare_strict(series, now)
    first = now - DAYS * 86400
    calls = []
    for (seg, tf), picks in st._PICKS60.items():
        p = st.STRATEGIES.get((seg, tf))
        cs = st._usable(series[tf][seg], tf, now)
        fine = [c for c in m1[seg] if c["t"] + 60 <= now] if tf > 1 else None
        for c in st._replay_one(cs, dict(p, confirm=None), fine=fine, now=now, picks=picks, pending_sink={}):
            if c["t"] >= first:
                calls.append(c)
    calls.sort(key=lambda c: c["entryAt"])
    rows, groups = [], {}
    for c in calls:
        v = c["variants"]["tgt"]
        result = "SL" if (v["kind"] == "SL" and not v["win"]) else ("T%d" % v["best"] if v["best"] else "TIMEOUT")
        g = (c["date"], c.get("group"))
        groups.setdefault(g, []).append(c)
        rows.append([c["date"], c["segment"], c["tf"], c["side"], clock(c["t"] + c["tf"] * 60), clock(c["entryAt"]), c["entry"],
                     v["sl"], clock(v["slAt"]) if v["kind"] == "SL" else "", v["t1"], clock(v["t1At"]), v["t2"], clock(v["t2At"]), v["t3"], clock(v["t3At"]),
                     result, clock(v["exitAt"]), v["pts"], "yes" if v["win"] else "no", c.get("group"), c.get("why") or ""])
    out = os.path.join(ROOT, f"conf_tier_test_{DAYS}d_calls.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "index", "candle_min", "side", "generated_at_ist", "entry_at_ist", "entry", "SL", "SL_hit_at", "T1", "T1_hit_at", "T2", "T2_hit_at",
                    "T3", "T3_hit_at", "result", "exit_at", "points_single_lot", "win_T1_before_SL", "group", "why"])
        w.writerows(rows)
    sessions = len({r[0] for r in rows}) or 1
    def line(name, v):
        n = len(v)
        if not n:
            print(f"  {name:<34} 0 calls"); return
        win = sum(c["variants"]["tgt"]["win"] for c in v) / n * 100
        pts = sum(c["variants"]["tgt"]["pts"] for c in v)
        print(f"  {name:<34} {n:>4} calls ({n / sessions:.2f}/session)  win {win:4.1f}%  net {pts / n:+6.1f}  total {pts:+7.0f}")
    print(f"CONFIDENCE-TIER calls, last {DAYS} days ({sessions} sessions) - NOT counted with the sure calls")
    line("all shown", calls)
    for seg in strict.SEGMENTS:
        line("   " + seg, [c for c in calls if c["segment"] == seg])
    late = [c for c in calls if ((c["t"] + 19800) % 86400) // 60 >= 14 * 60]
    line("   signals at or after 14:00", late)
    # counted once per group: the group's result = the first call's (by entry time)
    lead = [sorted(v, key=lambda c: c["entryAt"])[0] for v in groups.values()]
    line("COUNTED (same move = 1)", lead)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
