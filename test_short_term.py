"""Out-of-the-box test of the CURRENT short-term logic on REAL Upstox candles.

    python test_short_term.py            # last 120 days (default)
    python test_short_term.py 180        # any window in days

It re-runs the same engine the website uses (shortterm.py) separately for the 1, 5, 15 and 30 minute candles, with the
single-lot Runner rule, and writes:
    short_term_test_<N>d_calls.csv      every call: generation time, entry/SL/T1/T2/T3, the time each level was touched,
                                         result, exit time and points
    short_term_test_<N>d_report.md      the summary below

Nothing is simulated or random: every bar comes from Upstox (the script counts the sources to prove it). The 30-minute
candle has NO validated strategy in the app (no setting reached 70% wins), so it is run here with the research setting
only to show what the logic would do - it is labelled "NOT ISSUED".
"""
import collections
import csv
import datetime
import math
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import server
import shortterm as st

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 120
S5 = {"NIFTY": 50, "BANKNIFTY": 140, "SENSEX": 150, "NEXT50": 160}
IST = datetime.timezone.utc


def clock(t):
    return datetime.datetime.fromtimestamp(t + 19800, IST).strftime("%Y-%m-%d %H:%M") if t else ""


def research_30m(seg):
    """30m is not issued by the app. Same VWAP-reclaim logic as the 5m strategy, ladder scaled to 30m volatility."""
    base = st.STRATEGIES.get((seg, 5))
    S = round(S5[seg] * math.sqrt(30 / 5))
    p = st._p(seg, 30, base["family"], base["bias"], round(0.15 * S), round(0.7 * S), S, round(0.4 * S), 120)
    p["notIssued"] = True
    return p


def main():
    server._sync_token_from_env()
    if not server.UX.token:
        sys.exit("No UPSTOX_ACCESS_TOKEN in .env")
    now = time.time()
    print("=" * 100)
    print(f"SHORT-TERM LOGIC TEST - last {DAYS} days on REAL Upstox candles - run {clock(now)} IST")
    print("=" * 100)

    # ---------------------------------------------------------------- real data + provenance
    c5, m1 = {}, {}
    for seg in S5:
        inst = next(x for x in server.INSTRUMENTS if x["id"] == seg)
        c5[seg] = server._candles_days(inst, 300)
        m1[seg] = server._hist_1m(inst)
    src = collections.Counter(c.get("src", "UPSTOX") for seg in S5 for c in c5[seg] + m1[seg])
    print(f"DATA: {sum(len(v) for v in c5.values()):,} five-minute bars and {sum(len(v) for v in m1.values()):,} one-minute bars "
          f"for {len(S5)} indices; bar sources: {dict(src)}")
    print(f"      5m history {clock(c5['NIFTY'][0]['t'])[:10]} -> {clock(c5['NIFTY'][-1]['t'])};  "
          f"1m history {clock(m1['NIFTY'][0]['t'])[:10]} -> {clock(m1['NIFTY'][-1]['t'])}")
    if set(src) != {"UPSTOX"}:
        print("      WARNING: some bars are not tagged UPSTOX - review before trusting the result")
    series = {1: m1, 5: c5, 15: {k: st.resample(v, 15) for k, v in c5.items()}, 30: {k: st.resample(v, 30) for k, v in c5.items()}}

    rows, summary = [], {}
    for tf in (1, 5, 15, 30):
        calls_tf = []
        for seg in S5:
            p = st.STRATEGIES.get((seg, tf)) or (research_30m(seg) if tf == 30 else None)
            if not p:
                continue
            cs = st._usable(series[tf][seg], tf, now)
            fine = None if tf == 1 else [c for c in m1[seg] if c["t"] + 60 <= now]
            allc = st.replay(cs, p, fine=fine, now=now)
            since = cs[-1]["t"] - DAYS * 86400
            for c in allc:
                if c["t"] >= since and c["result"] != "OPEN" and not c.get("planAlive"):
                    c["notIssued"] = bool(p.get("notIssued"))
                    calls_tf.append(c)
        summary[tf] = calls_tf
        rows.extend(calls_tf)
    rows.sort(key=lambda r: (r["tf"], r["t"], r["segment"]))

    # ---------------------------------------------------------------- csv with every call
    out_csv = os.path.join(ROOT, f"short_term_test_{DAYS}d_calls.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["candle_min", "issued_by_app", "index", "side", "generated_at_ist", "entry", "SL", "SL_touched_at",
                    "T1", "T1_touched_at", "T1_after_min", "T2", "T2_touched_at", "T2_after_min", "T3", "T3_touched_at",
                    "T3_after_min", "result_bucket", "single_lot_exit_at", "single_lot_exit_reason", "single_lot_points",
                    "full_stop_out", "max_favourable_pts", "patterns"])
        for r in rows:
            pl = r["plans"][r["exitStyle"]]
            w.writerow([r["tf"], "NO (research only)" if r["notIssued"] else "yes", r["segment"], r["side"], clock(r["entryAt"]),
                        r["entry"], r["sl"], clock(r.get("slAt")), r["t1"], clock(r.get("t1At")), r.get("t1After") or "",
                        r["t2"], clock(r.get("t2At")), r.get("t2After") or "", r["t3"], clock(r.get("t3At")), r.get("t3After") or "",
                        r["result"], clock(pl["exitAt"]), pl["reason"], pl["pts"], "yes" if pl["kind"] == "SL" else "no",
                        r["mfe"], "; ".join(r.get("patterns") or [])])

    # ---------------------------------------------------------------- report
    L = []
    P = L.append

    def stats(calls):
        n = len(calls)
        if not n:
            return None
        win = [c for c in calls if c["result"] in ("T1", "T2", "T3")]
        pts = [c["plans"][c["exitStyle"]]["pts"] for c in calls]
        gains = [p for p, c in zip(pts, calls) if p > 0.1 * c["slPts"]]
        loses = [-p for p, c in zip(pts, calls) if p < -0.1 * c["slPts"]]
        pos = sum(p for p in pts if p > 0)
        neg = -sum(p for p in pts if p <= 0)
        wpts = [p for p, c in zip(pts, calls) if c["result"] in ("T1", "T2", "T3")]
        return {
            "n": n, "win": len(win) / n * 100, "sl": sum(1 for c in calls if c["result"] == "SL") / n * 100,
            "t1": sum(1 for c in calls if c["result"] == "T1") / n * 100, "t2": sum(1 for c in calls if c["result"] == "T2") / n * 100,
            "t3": sum(1 for c in calls if c["result"] == "T3") / n * 100, "to": sum(1 for c in calls if c["result"] == "TIMEOUT") / n * 100,
            "avg": sum(pts) / n, "total": sum(pts), "avg_win_calls": sum(wpts) / len(wpts) if wpts else None,
            "avg_gain": sum(gains) / len(gains) if gains else None, "avg_loss": sum(loses) / len(loses) if loses else None,
            "gain": len(gains) / n * 100, "loss": len(loses) / n * 100, "pf": pos / neg if neg else None,
            "stop_out": sum(1 for c in calls if c["plans"][c["exitStyle"]]["kind"] == "SL") / n * 100,
        }

    def tmed(calls, key):
        v = [c[key] for c in calls if c.get(key)]
        return (round(statistics.median(v)), round(sum(v) / len(v)), len(v)) if v else None

    f = lambda x, d=1: "-" if x is None else f"{x:.{d}f}"
    P(f"# Short-term logic test - last {DAYS} days, real Upstox candles")
    P(f"Run {clock(now)} IST. Bars: {dict(src)}. Single-lot **Runner** rule: stop moves to entry once T1 is touched, then hold until the window ends or the stop is hit.")
    P("")
    P("**Win rate** = calls that reached T1, T2 or T3 before the stop. **Points per call** = average single-lot Runner points over ALL calls (losses included).")
    scored = {tf: dict(collections.Counter(c["scoredOn"] for c in summary[tf])) for tf in (1, 5, 15, 30)}
    P("Path used to decide which level came first (1m = the real 1-minute candles): " + "; ".join(f"{tf}m calls -> {v}" for tf, v in scored.items()))
    P("")
    P("## T1 size - what a bigger T1 does (same calls, same stop, single lot, Runner rule)")
    P("T1 as a fraction of the stop. **Win rate = T1 reached before the stop.** The shipped default keeps T1 small because that is what holds 70%+ wins.")
    P("| Candle | T1 size | Win rate | Full stop-outs | Avg points / call | Avg points of a winning call |")
    P("|---|---|---|---|---|---|")
    for tf in (1, 5, 15, 30):
        calls = summary[tf]
        if not calls:
            continue
        n = len(calls)
        for key, label in (("cur", "shipped (about 1/4-1/2 of stop)"), ("0.75", "3/4 of stop"), ("1", "equal to stop (current stop)"), ("eq", "T1 = SL, size tuned per index")):
            if key == "eq" and not all("eq" in c["variants"] for c in calls):
                continue                                   # 30m research setting has no tuned equal pair
            if key == "cur":
                wins = [c for c in calls if c["result"] in ("T1", "T2", "T3")]
                pts = [c["plans"]["runner"]["pts"] for c in calls]
                stop = sum(1 for c in calls if c["plans"]["runner"]["kind"] == "SL")
                wp = [c["plans"]["runner"]["pts"] for c in wins]
            else:
                wins = [c for c in calls if c["variants"][key]["win"]]
                pts = [c["variants"][key]["pts"] for c in calls]
                stop = sum(1 for c in calls if c["variants"][key]["kind"] == "SL")
                wp = [c["variants"][key]["pts"] for c in wins]
            tag = " (not issued)" if tf == 30 else ""
            P(f"| {tf}m{tag} | {label} | **{len(wins) / n * 100:.1f}%** | {stop / n * 100:.1f}% | {sum(pts) / n:+.1f} | {sum(wp) / len(wp) if wp else 0:+.1f} |")
    P("")
    P("## NET average per call - ALL calls, losses subtracted from profits (single lot)")
    P("Real expectancy = (sum of every call's profit and loss) / number of calls. **Target** = T1 = SL = Nifty 25 / Sensex 75 / Bank Nifty 75 / Next 50 80; **Tuned** = equal T1 = SL per index and candle; **High win rate** = small-T1 ladder.")
    P("| Candle | Index | Profile | T1 = SL | Calls | Win rate | Full stop-outs | **NET avg pts per call (Runner)** | NET (exit at T1 or stop) | Avg of a winning call | Net pts per day | Total pts |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|")
    sessions = len({c["date"] for tf_ in (1, 5, 15) for c in summary[tf_]}) or 1
    for tf in (1, 5, 15):
        for seg in S5:
            for key, label in (("tgt", "Target"), ("eq", "Tuned"), ("cur", "High win rate")):
                cc = [c for c in summary[tf] if c["segment"] == seg and (key == "cur" or key in c["variants"])]
                if not cc:
                    continue
                if key == "cur":
                    win = lambda c: c["result"] in ("T1", "T2", "T3")
                    pts = [c["plans"]["runner"]["pts"] for c in cc]
                    kind = lambda c: c["plans"]["runner"]["kind"]
                    t1p = lambda c: c["t1Pts_"]; slp = lambda c: c["slPts"]; xs = f"{cc[0]['t1Pts_']}/{cc[0]['slPts']}"
                else:
                    v = lambda c, key=key: c["variants"][key]
                    win = lambda c, v=v: v(c)["win"]
                    pts = [v(c)["pts"] for c in cc]
                    kind = lambda c, v=v: v(c)["kind"]
                    t1p = lambda c, v=v: v(c)["t1Pts"]; slp = lambda c, v=v: v(c)["slPts"]; xs = f"{v(cc[0])['t1Pts']}"
                ws = [c for c in cc if win(c)]
                br = [(t1p(c) if win(c) else -slp(c) if kind(c) == "SL" else p) for c, p in zip(cc, pts)]
                wp = [p for c, p in zip(cc, pts) if win(c)]
                stop = sum(1 for c in cc if kind(c) == "SL")
                P(f"| {tf}m | {seg} | {label} | {xs} | {len(cc)} | {len(ws) / len(cc) * 100:.1f}% | {stop / len(cc) * 100:.1f}% | **{sum(pts) / len(cc):+.1f}** | {sum(br) / len(cc):+.1f} | "
                  f"{sum(wp) / len(wp) if wp else 0:+.1f} | {sum(pts) / sessions:+.1f} | {sum(pts):+.0f} |")
    P("")
    P("## The same calls, counted three ways (this is why 'average per call' numbers differ so much)")
    P("- **Hindsight average** = the best price reached after each call, averaged. It is NOT tradeable: nobody can sell every call at its exact top. Many 'average per call' claims are built this way.")
    P("- **Bracket** = exit at T1 or at the stop. **Runner** = the single-lot rule used on the page. Both are what you could actually book. Index points; an at-the-money option moves roughly half as much as the index.")
    P("| Candle | Index | Calls | Win rate (T1 before SL) | Hindsight avg best move / call | Bracket avg pts / call | Runner avg pts / call |")
    P("|---|---|---|---|---|---|---|")
    for tf in (1, 5, 15):
        for seg in S5:
            cc = [c for c in summary[tf] if c["segment"] == seg]
            if not cc:
                continue
            wins = [c for c in cc if c["result"] in ("T1", "T2", "T3")]
            br = [(c["t1Pts_"] if c["result"] in ("T1", "T2", "T3") else -c["slPts"] if c["result"] == "SL" else c["plans"]["runner"]["pts"]) for c in cc]
            P(f"| {tf}m | {seg} | {len(cc)} | {len(wins) / len(cc) * 100:.1f}% | **{sum(c['mfe'] for c in cc) / len(cc):+.1f}** | {sum(br) / len(cc):+.1f} | {sum(c['plans']['runner']['pts'] for c in cc) / len(cc):+.1f} |")
    P("")
    P("## Summary by candle size")
    P("| Candle | Calls | **Win rate** | SL first | T1 only | T2 only | T3 | **Avg points / call** | Avg pts of winning calls | Avg gain (gaining calls) | Avg loss (losing calls) | Profit factor | Gaining / ≈0 / losing |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for tf in (1, 5, 15, 30):
        s = stats(summary[tf])
        tag = " *(NOT ISSUED - research setting)*" if tf == 30 else ""
        if not s:
            P(f"| {tf}m{tag} | 0 | no call generated | | | | | | | | | | |")
            continue
        P(f"| {tf}m{tag} | {s['n']} | **{f(s['win'])}%** | {f(s['sl'])}% | {f(s['t1'])}% | {f(s['t2'])}% | {f(s['t3'])}% | **{s['avg']:+.1f}** | "
          f"{f(s['avg_win_calls'])} | {f(s['avg_gain'])} | {f(s['avg_loss'])} | {f(s['pf'], 2)} | {f(s['gain'], 0)}% / {f(100 - s['gain'] - s['loss'], 0)}% / {f(s['loss'], 0)}% |")
    P("")
    P("(Points are index points, pooled across indices of different price levels - read the per-index tables below for like-for-like numbers.)")
    for tf in (1, 5, 15, 30):
        calls = summary[tf]
        if not calls:
            continue
        P("")
        P(f"## {tf}-minute candles" + (" - NOT ISSUED by the app (research setting only)" if tf == 30 else ""))
        P("| Index | Calls | Win rate | SL first | T1 | T2 | T3 | Avg points / call | Avg pts of winning calls | Total points | Full stop-outs | Ladder (T1/T2/T3, SL) |")
        P("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for seg in S5:
            cc = [c for c in calls if c["segment"] == seg]
            s = stats(cc)
            if not s:
                continue
            c0 = cc[0]
            P(f"| {seg} | {s['n']} | {f(s['win'])}% | {f(s['sl'])}% | {f(s['t1'])}% | {f(s['t2'])}% | {f(s['t3'])}% | {s['avg']:+.1f} | {f(s['avg_win_calls'])} | {s['total']:+.0f} | "
              f"{f(s['stop_out'])}% | +{c0['t1Pts_']}/+{c0['t2Pts_']}/+{c0['t3Pts_']}, SL -{c0['slPts']} |")
        P("")
        P(f"Time from call generation to each level (minutes after entry, median / mean, number of calls): "
          f"T1 {tmed(calls, 't1After')}, T2 {tmed(calls, 't2After')}, T3 {tmed(calls, 't3After')}, SL {tmed([c for c in calls if c['result'] == 'SL'], 'slAfter')}")
        by_hour = collections.Counter(c["time"][:2] for c in calls)
        P(f"Calls by hour of generation (IST): " + ", ".join(f"{h}h:{n}" for h, n in sorted(by_hour.items())))
        P("")
        P("First 6 and last 6 calls (full detail for every call is in the CSV):")
        P("| Generated (IST) | Index | Side | Entry | SL | T1 touched | T2 touched | T3 touched | SL touched | Result | Single-lot exit | Points |")
        P("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for c in calls[:6] + calls[-6:]:
            pl = c["plans"][c["exitStyle"]]
            P(f"| {clock(c['entryAt'])} | {c['segment']} | {c['side']} | {c['entry']} | {c['sl']} | {clock(c.get('t1At'))[11:] or '-'} | {clock(c.get('t2At'))[11:] or '-'} | "
              f"{clock(c.get('t3At'))[11:] or '-'} | {clock(c.get('slAt'))[11:] or '-'} | {c['result']} | {clock(pl['exitAt'])[11:] or '-'} {pl['kind']} | {pl['pts']:+.1f} |")
    P("")
    P("## How to read the honesty caveats")
    P("- The strategies and levels were chosen on history before the last 60 days, so a 120-day window includes data they were tuned on: it shows consistency, not a fresh out-of-sample proof (see walkforward.py for that).")
    P("- Points are before brokerage and slippage and are index points, not option premium.")
    P("- A minute that touches both the stop and a target is scored as the stop. The Runner rule is more exposed than the old plan to a minute that touches T1 and falls straight back to entry.")
    P(f"- Every call is scored on the real 1-minute path; bars come only from Upstox ({dict(src)}).")
    report = "\n".join(L)
    out_md = os.path.join(ROOT, f"short_term_test_{DAYS}d_report.md")
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(report)
    print(f"\nWrote {out_csv}  ({len(rows)} calls)\nWrote {out_md}")


if __name__ == "__main__":
    main()
