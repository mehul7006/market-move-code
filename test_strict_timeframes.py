"""Test the strict short-term logic on EVERY candle size (1, 5, 15 and 30 minutes) for Nifty, Bank Nifty and Sensex, on real Upstox candles.

    python test_strict_timeframes.py 180      (or 120)

For each index and candle size the same recipe the app uses is applied: six signal types -> per-index score model (trained on that candle
size's signals) -> top share of scores -> one open call per index at a time, max 5 a day -> the index's quality filter -> Sensex's
confirmation entry. Levels are the app's (T1 / stop: Nifty 22.5/25, Bank Nifty 63/70, Sensex 81/90; T2 = 2x T1, T3 = 3x T1). Every call is
followed on the real 1-minute path (stop first when one minute touches both), single lot, stop to entry after T1, closed at the end of its window.

IMPORTANT: the score model is fitted on all of the data it is then replayed on (in-sample), so these numbers are optimistic. The app itself
issues Nifty on 5-minute, Bank Nifty on 1-minute and Sensex on 5-minute candles (marked "app" below) - the walk-forward numbers for those are
in strict_training_report.txt.

Writes strict_tf_test_<N>d_calls.csv (every call: generation time, entry time and price, SL / T1 / T2 / T3 with the time each was hit) and
strict_tf_test_<N>d_report.md.
"""
import bisect
import collections
import datetime
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import server
import shortterm as st
import strict
import train_strict as ts

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 180
TFS = (1, 5, 15, 30)
HTF = {1: 5, 5: 15, 15: 30, 30: 60}
HORIZON = {1: 60, 5: 120, 15: 120, 30: 120}
APP_TF = {seg: tuple((strict.RULES.get(seg) or {}).get("tfs") or ()) for seg in strict.SEGMENTS}
IST = datetime.timezone.utc


def clock(t):
    return datetime.datetime.fromtimestamp(t + 19800, IST).strftime("%Y-%m-%d %H:%M") if t else ""


def walk(m1, m1t, start_t, end_t, side, entry, T1, S):
    """Follow one call on the 1-minute path. Returns the full record of touches."""
    a, b = bisect.bisect_left(m1t, start_t), bisect.bisect_left(m1t, end_t)
    if b - a < max(3, 0.9 * ((end_t - start_t) // 60)):
        return None
    levels = (T1, 2 * T1, 3 * T1)
    hit = [None, None, None]
    stop, mx, sl_at, exit_at, reason, pts = -S, 0.0, None, None, None, None
    best_before_stop = 0
    for bar in m1[a:b]:
        fav = (bar["high"] - entry) if side > 0 else (entry - bar["low"])
        adv = (bar["low"] - entry) if side > 0 else (entry - bar["high"])
        if adv <= stop:
            pts, exit_at = stop, bar["t"]
            if stop <= -S + 1e-9:
                sl_at, reason = bar["t"], "Stop loss hit"
            else:
                reason = "Stop moved to entry after T1 - hit"
            break
        for j, lv in enumerate(levels):
            if hit[j] is None and fav >= lv:
                hit[j] = bar["t"]
                best_before_stop = j + 1
        mx = max(mx, fav)
        if mx >= T1:
            stop = max(stop, 0.0)
    else:
        last = m1[b - 1]
        pts = (last["close"] - entry) * side
        exit_at, reason = last["t"] + 60, "Window ended - closed at last price"
    result = ("T%d" % best_before_stop) if best_before_stop else ("SL" if sl_at else "TIMEOUT")
    return {"hit": hit, "sl_at": sl_at, "exit_at": exit_at, "reason": reason, "pts": round(pts, 2), "win": hit[0] is not None, "result": result}


def confirm_fill(m1, m1t, done_t, side, close, d, wait):
    trigger = close + side * d
    a = bisect.bisect_left(m1t, done_t)
    for bar in m1[a:]:
        if bar["t"] >= done_t + wait * 60:
            break
        if (side > 0 and bar["high"] >= trigger) or (side < 0 and bar["low"] <= trigger):
            return bar["t"], trigger
    return None


def main():
    server._sync_token_from_env()
    now = time.time()
    first = now - DAYS * 86400
    rows, summary = [], []
    for seg in strict.SEGMENTS:
        inst = next(x for x in server.INSTRUMENTS if x["id"] == seg)
        c5 = server._candles_days(inst, 300)
        m1 = server._hist_1m(inst)
        m1t = [c["t"] for c in m1]
        series = {1: m1, 5: c5, 15: st.resample(c5, 15), 30: st.resample(c5, 30), 60: st.resample(c5, 60)}
        T1, S = ts.LEVELS[seg]
        conf = (strict.RULES.get(seg) or {}).get("confirm")
        for tf in TFS:
            saved_h, saved_hz = dict(strict.HTF), dict(strict.HORIZON_MIN)
            strict.HTF.update(HTF)
            strict.HORIZON_MIN.update(HORIZON)
            try:
                cands = strict.candidates(series[tf], tf, series[HTF[tf]])
            finally:
                strict.HTF.clear(); strict.HTF.update(saved_h)
                strict.HORIZON_MIN.clear(); strict.HORIZON_MIN.update(saved_hz)
            cs = series[tf]
            # outcome of every signal (entered at the signal close) - the model's training label
            for x in cands:
                x["entry"] = cs[x["i"]]["close"]
                x["o"] = walk(m1, m1t, x["done"], x["end"], x["side"], x["entry"], T1, S)
            cands = [x for x in cands if x["o"]]
            if len(cands) < 200:
                summary.append((seg, tf, None))
                continue
            y = [(1.0 if x["o"]["win"] else 0.0) if ts.LABEL[seg] == "win" else x["o"]["pts"] / S for x in cands]
            mdl = ts.fit_ridge([x["feat"] for x in cands], y)
            sc = sorted(strict.score(mdl, x["feat"]) for x in cands)
            mdl["thr"] = sc[int((1 - ts.TOP_SHARE[seg]) * len(sc))]
            for x in cands:
                x["score"] = strict.score(mdl, x["feat"])
            picks_all = strict.select(cands, mdl)
            mdl["filter"] = strict.filter_cut(seg, picks_all)
            calls = []
            for p in picks_all:
                if p["t"] < first or not strict.passes(mdl, p):
                    continue
                entry_at, entry = p["done"], p["entry"]
                if conf:
                    f = confirm_fill(m1, m1t, p["done"], p["side"], p["entry"], conf[0], conf[1])
                    if not f:
                        continue
                    entry_at, entry = f
                o = walk(m1, m1t, entry_at, p["end"], p["side"], entry, T1, S)
                if not o:
                    continue
                sgn = p["side"]
                calls.append(o)
                rows.append([clock(p["t"])[:10], seg, tf, "yes" if tf in APP_TF[seg] else "", "BUY" if sgn > 0 else "SELL",
                             clock(p["done"]), clock(entry_at), round(entry, 2), round(p["entry"], 2),
                             round(entry - sgn * S, 2), clock(o["sl_at"]),
                             round(entry + sgn * T1, 2), clock(o["hit"][0]), round(entry + 2 * sgn * T1, 2), clock(o["hit"][1]),
                             round(entry + 3 * sgn * T1, 2), clock(o["hit"][2]),
                             o["result"], clock(o["exit_at"]), o["reason"], o["pts"], "yes" if o["win"] else "no", p["fam"]])
            summary.append((seg, tf, calls))
    sess = len({r[0] for r in rows}) or 1
    out_csv = os.path.join(ROOT, f"strict_tf_test_{DAYS}d_calls.csv")
    import csv
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "index", "candle_min", "issued_by_app", "side", "generated_at_ist", "entry_at_ist", "entry", "signal_close",
                    "SL", "SL_hit_at", "T1", "T1_hit_at", "T2", "T2_hit_at", "T3", "T3_hit_at", "result", "exit_at", "exit_reason",
                    "points_single_lot", "win_T1_before_SL", "signal"])
        w.writerows(sorted(rows, key=lambda r: (r[1], r[2], r[6])))
    L = [f"# Strict short-term logic on every candle size - last {DAYS} days (real Upstox data)", "",
         f"Run {clock(now)} IST. Levels T1 / stop: Nifty 22.5/25, Bank Nifty 63/70, Sensex 81/90 (T2 = 2x T1, T3 = 3x T1). "
         "In-sample replay: the score model is fitted on the same data, so expect live results lower. 'App' = the candle size the app issues.", "",
         "| Index | Candle | App | Calls | Calls/session | Win (T1 before SL) | T1 only | T2 only | T3 | SL first | Timeout | Net pts/call | Total pts |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg, tf, calls in summary:
        if not calls:
            L.append(f"| {seg} | {tf}m | {'yes' if tf in APP_TF[seg] else ''} | 0 | - | - | - | - | - | - | - | - | - |")
            continue
        n = len(calls)
        c = collections.Counter(x["result"] for x in calls)
        L.append(f"| {seg} | {tf}m | {'yes' if tf in APP_TF[seg] else ''} | {n} | {n / sess:.2f} | {sum(x['win'] for x in calls) / n * 100:.1f}% | "
                 f"{c['T1']} | {c['T2']} | {c['T3']} | {c['SL']} | {c['TIMEOUT']} | {sum(x['pts'] for x in calls) / n:+.1f} | {sum(x['pts'] for x in calls):+.0f} |")
    out_md = os.path.join(ROOT, f"strict_tf_test_{DAYS}d_report.md")
    open(out_md, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\nWrote {out_csv} ({len(rows)} calls)\nWrote {out_md}")


if __name__ == "__main__":
    main()
