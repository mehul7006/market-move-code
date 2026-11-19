"""Option-chain and futures recorder.

Upstox does not serve historical option-chain data (OI, volume, IV, greeks) without the Plus plan, so signals built on them cannot be
back-tested today. This recorder starts the history: while the market is open it saves a snapshot of every index's near-expiry option chain
(the strikes around the money) every few minutes to  <CACHE_DIR>/optrec/<INDEX>_<YYYYMMDD>.jsonl , one JSON line per snapshot:

    {"t": <epoch>, "seg": "NIFTY", "spot": ..., "expiry": "2026-11-25",
     "pcr_oi": ..., "ce_oi": ..., "pe_oi": ..., "ce_vol": ..., "pe_vol": ..., "atm": <strike>, "atm_iv": ...,
     "strikes": [{"k": strike, "ce": {"oi","prev_oi","vol","ltp","iv","delta","gamma","theta","vega"}, "pe": {...}}, ...],
     "fut": {"oi": ..., "volume": ..., "oiChange30m": ...}}

After a few weeks of recorded sessions `python optrecorder.py --summary` shows what is there, and the data can be added to train_strict.py as
extra inputs (put/call ratio change, OI build-up at the money, IV change) and tested the same walk-forward way as everything else.
Only real Upstox data is stored; if a fetch fails nothing is written for that snapshot.

Set RECORD_OPTIONS=0 to turn it off (it is off by default in LITE mode / Render free tier unless RECORD_OPTIONS=1, because the free disk is wiped
when the service sleeps).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SEGMENTS = ("NIFTY", "BANKNIFTY", "SENSEX")
INTERVAL_SEC = 300          # one snapshot per index every 5 minutes
STRIKES_EACH_SIDE = 12      # strikes kept on each side of the money


def rec_dir(server):
    d = os.path.join(server.CACHE_DIR, "optrec")
    os.makedirs(d, exist_ok=True)
    return d


def compact_snapshot(seg, rows, spot, expiry, step, now, fut=None, keep=STRIKES_EACH_SIDE):
    """rows = raw Upstox option-chain rows (call_options / put_options with market_data and option_greeks). Pure function (unit-tested)."""
    legs = []
    for r in rows:
        ce, pe = r.get("call_options") or {}, r.get("put_options") or {}
        cmd, pmd = ce.get("market_data") or {}, pe.get("market_data") or {}
        cg, pg = ce.get("option_greeks") or {}, pe.get("option_greeks") or {}
        if not cmd and not pmd:
            continue

        def leg(md, gr):
            return {"oi": md.get("oi") or 0, "prev_oi": md.get("prev_oi") or 0, "vol": md.get("volume") or 0, "ltp": md.get("ltp"),
                    "iv": gr.get("iv"), "delta": gr.get("delta"), "gamma": gr.get("gamma"), "theta": gr.get("theta"), "vega": gr.get("vega")}
        legs.append({"k": r["strike_price"], "ce": leg(cmd, cg), "pe": leg(pmd, pg)})
    if not legs or not spot:
        return None
    legs.sort(key=lambda x: x["k"])
    atm = min(legs, key=lambda x: abs(x["k"] - spot))
    i = legs.index(atm)
    near = legs[max(0, i - keep): i + keep + 1]
    ce_oi, pe_oi = sum(x["ce"]["oi"] for x in legs), sum(x["pe"]["oi"] for x in legs)
    ivs = [v for v in (atm["ce"]["iv"], atm["pe"]["iv"]) if v]
    out = {"t": int(now), "seg": seg, "spot": spot, "expiry": expiry, "atm": atm["k"],
           "atm_iv": round(sum(ivs) / len(ivs), 3) if ivs else None,
           "ce_oi": ce_oi, "pe_oi": pe_oi, "pcr_oi": round(pe_oi / ce_oi, 4) if ce_oi else None,
           "ce_vol": sum(x["ce"]["vol"] for x in legs), "pe_vol": sum(x["pe"]["vol"] for x in legs), "strikes": near}
    if fut:
        out["fut"] = {k: fut.get(k) for k in ("oi", "volume", "oiChange30m", "oiChangeDay", "volumeRatio")}
    return out


def record_once(server, now=None):
    """Take one snapshot per index. Returns the number written."""
    now = now or time.time()
    try:
        futures = server._futures_context()
    except Exception:
        futures = {}
    wrote = 0
    for seg in SEGMENTS:
        try:
            inst = next(x for x in server.INSTRUMENTS if x["id"] == seg)
            exps = server._expiries(inst["chain"])
            if not exps:
                continue
            rows = server.UX.chain(inst["chain"], exps[0])
            q = server.UX.quotes([inst["key"]]).get(inst["key"]) or {}
            spot = q.get("last") or (rows[0].get("underlying_spot_price") if rows else None)
            snap = compact_snapshot(seg, rows, spot, exps[0], inst.get("step"), now, futures.get(seg))
            if not snap:
                continue
            day = datetime.datetime.fromtimestamp(now + 19800, datetime.timezone.utc).strftime("%Y%m%d")
            with open(os.path.join(rec_dir(server), f"{seg}_{day}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(snap, separators=(",", ":")) + "\n")
            wrote += 1
        except Exception:
            continue
    return wrote


def loop(server):
    """Background thread: while the market is open, one snapshot per index every INTERVAL_SEC."""
    last = 0.0
    while True:
        try:
            if server.market_open() and time.time() - last >= INTERVAL_SEC:
                record_once(server)
                last = time.time()
        except Exception:
            pass
        time.sleep(20)


def enabled(server):
    flag = os.environ.get("RECORD_OPTIONS")
    if flag is not None:
        return flag.strip() not in ("0", "false", "no", "")
    return not getattr(server, "LITE", False)


def summary(server):
    d = rec_dir(server)
    files = sorted(f for f in os.listdir(d) if f.endswith(".jsonl"))
    out = {"directory": d, "days": {}, "snapshots": 0}
    for f in files:
        seg, day = f[:-6].rsplit("_", 1)
        n = sum(1 for _ in open(os.path.join(d, f), encoding="utf-8"))
        out["days"].setdefault(day, {})[seg] = n
        out["snapshots"] += n
    out["sessions"] = len(out["days"])
    return out


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    import server as _server
    if "--summary" in sys.argv:
        print(json.dumps(summary(_server), indent=1))
    else:
        _server._sync_token_from_env()
        print("snapshots written:", record_once(_server))
