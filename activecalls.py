"""Active calls: every call generated in the current (or last) session, long term AND short term, in one list.

A call is RUNNING until its stop is touched, T3 is reached, or its window ends. Each call is tracked on the real
1-minute path INCLUDING the still-forming minute and the latest traded price, so a level counts the moment price
touches it. Where a minute touches both the stop and a target, it is scored as the stop (order cannot be known).
Real Upstox data only.
"""
from __future__ import annotations

import bisect
import datetime

import shortterm


def scan(path, sign, sl, t1, t2, t3):
    """Walk a 1-minute path. Returns (touched, result) where result is SL / T1 / T2 / T3 once the stop was
    touched or T3 reached, else None. `touched` maps level -> epoch of the minute in which it was first touched."""
    touched, maxhit, stopped = {}, 0, False
    for c in path:
        if sign > 0:
            hit_sl, tops = c["low"] <= sl, (c["high"] >= t1, c["high"] >= t2, c["high"] >= t3)
        else:
            hit_sl, tops = c["high"] >= sl, (c["low"] <= t1, c["low"] <= t2, c["low"] <= t3)
        if hit_sl:
            touched["sl"] = c["t"]
            stopped = True
            break
        for j, on in enumerate(tops, 1):
            if on and j > maxhit:
                maxhit = j
                touched["t%d" % j] = c["t"]
        if maxhit == 3:
            break
    if stopped:
        return touched, ("T%d" % maxhit if maxhit else "SL")
    if maxhit == 3:
        return touched, "T3"
    return touched, None


def _row(term, seg, name, candle, side, entry_at, entry, sl, t1, t2, t3, touched, status, window_end, now,
         ltp, scored_on, extra=None):
    best = max([int(k[1]) for k in touched if k.startswith("t")] or [0])
    r = {
        "term": term, "segment": seg, "name": name, "candle": candle, "side": side,
        "generatedAt": entry_at, "entry": round(entry, 2), "sl": round(sl, 2),
        "t1": round(t1, 2), "t2": round(t2, 2), "t3": round(t3, 2),
        "slPts": round(abs(entry - sl), 1), "t1Pts": round(abs(t1 - entry), 1),
        "t2Pts": round(abs(t2 - entry), 1), "t3Pts": round(abs(t3 - entry), 1),
        "touched": touched, "status": status, "best": best, "windowEnd": window_end,
        "running": status == "RUNNING", "scoredOn": scored_on,
        "openMinutes": max(0, int((min(now, window_end) - entry_at) // 60)) if window_end else None,
        "ltp": ltp,
    }
    if extra:
        r.update(extra)
    return r


def build(short_payload, today_payload, cmap1, ltps, profiles, profile_by_id, now, market_open):
    session = today_payload.get("day")
    rows = []

    # ------------------------------------------------------------------ short term (already tracked live)
    for c in short_payload["timeframes"]["all"]["calls"]:
        if c["date"] != session:
            continue
        p = shortterm.STRATEGIES.get((c["segment"], c["tf"])) or {}
        day_start = c["t"] - ((c["t"] + 19800) % 86400)
        wend = min(c["entryAt"] + p.get("horizonMin", 120) * 60, day_start + shortterm.SESSION_CLOSE_MIN * 60)
        touched = {k: c[k + "At"] for k in ("sl", "t1", "t2", "t3") if c.get(k + "At")}
        status = "RUNNING" if (c["result"] == "OPEN" or c.get("planAlive")) else c["result"]   # a runner is still open after T3
        rows.append(_row("short", c["segment"], c["name"], "%sm" % c["tf"], c["side"], c["entryAt"], c["entry"],
                         c["sl"], c["t1"], c["t2"], c["t3"], touched, status, wend, now,
                         (ltps.get(c["segment"]) or {}).get("last"), c.get("scoredOn"),
                         {"patterns": c.get("patterns") or [], "id": c["id"]}))

    # ------------------------------------------------------------------ long term (scanned on the 1m path here)
    for c in today_payload.get("calls") or []:
        seg = c["id"]
        prof = profiles[profile_by_id[seg]]
        sign = 1 if c["side"] == "LONG" else -1
        entry_at = c["t"] + 300
        day_start = c["t"] - ((c["t"] + 19800) % 86400)
        wend = min(c["t"] + (prof["horizon"] + 1) * 300, day_start + 930 * 60)
        m1 = cmap1.get(seg) or []
        m1t = [x["t"] for x in m1]
        a, b = bisect.bisect_left(m1t, entry_at), bisect.bisect_left(m1t, wend)
        need = (min(now, wend) - entry_at) // 60
        scored = "1m"
        if need <= 0 or (b - a) >= 0.9 * need:
            touched, res = scan(m1[a:b], sign, c["stop"], c["t1"], c["t2"], c["t3"])
        else:                                    # 1-minute history missing: fall back to the 5m engine's own result
            scored = "5m"
            touched = {}
            res = {"t1": "T1", "stop": "SL"}.get(c.get("outcome"))
        if res is None and now >= wend:
            res = "TIMEOUT"
        status = "RUNNING" if res is None else res
        rows.append(_row("long", seg, c["name"], "5m signal", "BUY" if sign > 0 else "SELL", entry_at, c["entry"],
                         c["stop"], c["t1"], c["t2"], c["t3"], touched, status, wend, now,
                         (ltps.get(seg) or {}).get("last"), scored,
                         {"profile": c.get("profile"), "id": "L-%s-%s" % (seg, c["t"])}))

    rows.sort(key=lambda r: (0 if r["running"] else 1, -r["generatedAt"]))
    run = [r for r in rows if r["running"]]
    cnt = {
        "total": len(rows), "running": len(run),
        "runningShort": sum(1 for r in run if r["term"] == "short"),
        "runningLong": sum(1 for r in run if r["term"] == "long"),
        "shortTotal": sum(1 for r in rows if r["term"] == "short"),
        "longTotal": sum(1 for r in rows if r["term"] == "long"),
        "hit": sum(1 for r in rows if r["status"] in ("T1", "T2", "T3")),
        "stopped": sum(1 for r in rows if r["status"] == "SL"),
        "timeout": sum(1 for r in rows if r["status"] == "TIMEOUT"),
    }
    return {
        "serverTime": now, "marketOpen": market_open, "sessionDate": session,
        "isToday": bool(today_payload.get("isToday")), "ltp": ltps, "counts": cnt, "calls": rows,
        "istClock": datetime.datetime.fromtimestamp(now + 19800, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "notes": {
            "running": "RUNNING = the call's stop has not been touched and its window (1-2 hours, or 15:25 for long term) has not "
                       "ended. A short-term call that touched T1, T2 or T3 stays running: with the Runner rule you keep the single lot, "
                       "stop at entry, until the window ends or the stop is hit.",
            "live": "Every running call is checked against the still-forming 1-minute candle and the latest traded price, "
                    "so a touch counts the moment it happens. If one minute touches both the stop and a target, it is "
                    "scored as the stop.",
            "long": "Long-term calls are the original precision-profile calls (5m signals, wide stops).",
        },
    }
