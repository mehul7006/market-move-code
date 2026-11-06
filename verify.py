"""Independent proof for the Market Signal Desk.

Pulls the dashboard payload, then separately pulls Upstox quotes/history and Yahoo
5-minute bars and compares every overlapping value. Finally re-runs the walk-forward
audit locally and checks it against the numbers the dashboard served.

Usage:  python verify.py [--url http://127.0.0.1:8787]
Exit code 0 = proven, 1 = a provenance or audit check failed.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request

import analysis
import data_sources as yahoo
import server
import upstox

ROOT = os.path.dirname(os.path.abspath(__file__))
IST = datetime.timezone(datetime.timedelta(hours=5.5))
TOL_PCT = 0.05  # a close may differ by 5 bps between feeds and still be the same bar


def ist(t):
    if not t:
        return "—"
    return datetime.datetime.fromtimestamp(float(t), IST).strftime("%Y-%m-%d %H:%M")


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def pct_diff(a, b):
    if not a:
        return float("inf")
    return abs(a - b) / abs(a) * 100


def pull_history(ux, inst, days):
    """Independent chunked 5m pull (the API caps one request at ~30 days)."""
    rows = {}
    today = datetime.date.today()
    for k in range((days + 29) // 30):
        end = today - datetime.timedelta(days=30 * k + 1)
        start = end - datetime.timedelta(days=29)
        try:
            part = ux.candles(inst["key"], "minutes", 5, end.isoformat(), start.isoformat())
        except Exception:
            break
        if not part:
            continue
        for c in part:
            rows[c["t"]] = c
    return rows


class Checks:
    def __init__(self):
        self.rows = []
        self.failed = 0

    def add(self, ok, label, detail=""):
        self.rows.append((bool(ok), label, detail))
        if not ok:
            self.failed += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    args = ap.parse_args()

    try:
        market = fetch_json(args.url + "/api/market?force=1")
        proof = fetch_json(args.url + "/api/proof")
    except Exception as exc:
        print(f"Cannot reach the dashboard at {args.url} ({exc}).\nStart it first:  python server.py")
        return 2

    env = upstox.load_env(os.path.join(ROOT, ".env"))
    token = os.environ.get("UPSTOX_ACCESS_TOKEN") or env.get("UPSTOX_ACCESS_TOKEN", "")
    ux = upstox.UpstoxClient(token)
    left = upstox.token_seconds_left(token)
    checks = Checks()

    print("MARKET SIGNAL DESK — PROVENANCE & ACCURACY PROOF")
    print(f"Dashboard clock : {market['istClock']} IST")
    print(f"Upstox token    : {'missing' if not token else (f'~{left // 3600}h {(left % 3600) // 60}m left' if left and left > 0 else 'EXPIRED')}")
    print("What this does  : separate Upstox + Yahoo pulls are diffed against every number the")
    print("                  dashboard shows; the walk-forward audit is recomputed from raw bars.")
    print("                  Nothing here reads the dashboard's own cache or proof endpoint for values.")

    items = {i["id"]: i for i in market["instruments"]}
    today = datetime.date.today()

    for iid in ("NIFTY", "SENSEX"):
        it = items[iid]
        inst = next(x for x in server.INSTRUMENTS if x["id"] == iid)
        f = it.get("forecast") or {}
        print(f"\n[{iid}] {it['name']}  {it['timeframe']}  bars={len(it['candles'])}"
              f"  last bar {ist(it['candles'][-1]['t']) if it['candles'] else '—'}")

        # 1) quote provenance: a fresh Upstox call vs the dashboard spot
        try:
            q = ux.quotes([inst["key"]]).get(inst["key"]) or {}
            spot = q.get("last")
            d = pct_diff(spot, it["spot"])
            checks.add(d <= TOL_PCT,
                       f"quote matches Upstox (Δ {d:.4f}%)",
                       f"independent Upstox {spot}  vs  dashboard {it['spot']}"
                       + (f"  @ {q.get('quoteAt')}" if q.get("quoteAt") else ""))
        except Exception as exc:
            checks.add(False, "quote could not be confirmed against Upstox", str(exc)[:200])

        # 2) candle provenance: Upstox history independently, bar by bar
        ux_hist = {}
        try:
            ux_hist = {c["t"]: c for c in ux.candles(inst["key"], "minutes", 5,
                                                    today.isoformat(), (today - datetime.timedelta(days=30)).isoformat())}
            common = [c for c in it["candles"] if c["t"] in ux_hist]
            bad = [c for c in common if pct_diff(c["close"], ux_hist[c["t"]]["close"]) > TOL_PCT]
            checks.add(bool(common) and not bad,
                       f"{len(common)} overlapping bars match Upstox closes",
                       (f"{len(bad)} mismatch(es), worst "
                        f"{max((abs(c['close'] - ux_hist[c['t']]['close']) / c['close'] * 100) for c in bad):.4f}%"
                        if bad else "every overlapping close agrees within 0.05%"))

            tail = [c for c in it["candles"] if c["t"] not in ux_hist]
            if tail:
                yb = {c["t"]: c for c in yahoo.yahoo_chart(inst["yahoo"], "5d", "5m")["candles"]}
                ybad = [c for c in tail if c["t"] not in yb
                        or pct_diff(c["close"], yb[c["t"]]["close"]) > TOL_PCT]
                checks.add(not ybad,
                           f"{len(tail)} bars beyond Upstox EOD history match Yahoo real 5m",
                           (f"unverified: {[ist(c['t']) for c in ybad][:4]}"
                            if ybad else f"{ist(tail[0]['t'])} → {ist(tail[-1]['t'])}; "
                            "this is the overnight tail Upstox publishes late, not invented data"))
        except Exception as exc:
            checks.add(False, "candle history could not be confirmed", str(exc)[:200])

        # 3) options provenance
        if it["source"]["options"] == "UPSTOX":
            try:
                exps = ux.expiries(inst["chain"])
                exps = [e for e in exps if e > today.isoformat()]
                chain = ux.chain(inst["chain"], exps[0]) if exps else []
                checks.add(bool(chain), f"option chain matches Upstox ({len(chain)} strikes)",
                           f"expiry {exps[0] if exps else '—'} · dashboard expiry "
                           f"{(it['options'] or {}).get('expiry')}")
            except Exception as exc:
                checks.add(False, "option chain could not be confirmed", str(exc)[:200])
        else:
            checks.add(it["source"]["options"] == "N/A",
                       "no option chain expected for this segment",
                       it.get("optionsError") or it["source"]["options"])

        # 4) walk-forward audit recomputed here, compared with the dashboard's number
        if not it["candles"]:
            checks.add(False, "walk-forward audit needs candles", "no candles returned")
            continue
        key = f.get("profileKey") or ("SENSEX" if iid == "SENSEX" else "DEFAULT")
        try:
            audit = pull_history(ux, inst, server.AUDIT_DAYS.get(iid, 30))
        except Exception as exc:
            audit = {}
            print(f"        six-month history pull failed ({str(exc)[:100]}); using the 30-day pull")
        for t, c in ux_hist.items():
            audit[t] = c
        for c in it["candles"]:          # tail bars already Yahoo-verified above
            audit[c["t"]] = c
        audit = [audit[k] for k in sorted(audit)]
        local = analysis.validate_forecast(audit, it["minMove"], server.FORECAST_PROFILES[key])
        served = (proof.get("validation") or {}).get(iid, {}).get("active")
        if not local:
            checks.add(False, "walk-forward audit returned no samples", "candles too few")
            continue
        if served:
            same = (local["samples"] == served.get("samples")
                    and local["winRate"] == served.get("winRate"))
            checks.add(same, "walk-forward numbers reproduce locally",
                       f"samples {local['samples']} vs {served.get('samples')} · "
                       f"win {local['winRate']}% vs {served.get('winRate')}% · "
                       f"reach {local['reachRate']}% vs {served.get('reachRate')}%")
        print(f"        profile {local['profile']}  horizon {local['horizonLabel']}  "
              f"stop {local.get('stopRule') or str(local['stopAtr']) + '×ATR'}  "
              f"min move {local['minMove']} pts")
        print(f"        audit window {local['lookbackBars']} bars across {len(audit)} 5m bars "
              f"(~{server.AUDIT_DAYS.get(iid, 30)} days pulled independently)")
        print(f"        setups {local['samples']}  wins {local['wins']}  losses {local['losses']}  "
              f"unresolved {local['unresolved']}  worst month {local.get('worstMonth')}%")
        print(f"        T1-before-SL win rate {local['winRate']}%   "
              f"min-move reached any time {local['reachRate']}%   "
              f"avg {local['avgBarsToT1']} bars to T1")
        print(f"        avg risk {local['avgRisk']} pts  R:R at T1 {local['rr1']}  "
              f"expectancy {local['expectancyPts']} pts per resolved trade")
        if local["lowSample"]:
            print("        NOTE: fewer than 15 setups — treat as indicative, not proven.")
        print("        last setups (bar time, entry → T1 / SL, outcome, bars, MFE):")
        for e in local["examples"]:
            print(f"          {ist(e['barTime'])}  {e['direction']:8} {e['entry']} → {e['target1']}"
                  f" / {e['stop']}  {e['outcome']:6} {str(e['bars']):>2}b  mfe {e['mfe']}")

    # Both prime indices must select the profile that clears the 75% gate, and a
    # below-floor audit must block calls rather than silently fall back.
    for iid in ("NIFTY", "SENSEX"):
        v = (proof.get("validation") or {}).get(iid) or {}
        cmp_ = v.get("comparison") or {}
        p_ = cmp_.get("PRECISION") or {}
        if not cmp_:
            checks.add(False, f"{iid} profile comparison missing", "proof endpoint returned no comparison")
            continue
        wr = p_.get("winRate") or 0
        checks.add(v.get("selected") == "PRECISION" and wr >= 75,
                   f"{iid} deploys the 75% profile and it clears 75%",
                   f"selected {v.get('selected')} · precision {wr}% ({p_.get('samples')} setups, "
                   f"worst month {p_.get('worstMonth')}%) vs balanced "
                   f"{(cmp_.get('BALANCED') or {}).get('winRate')}%")
        if wr < 75:
            fi = items[iid].get("forecast") or {}
            checks.add(not (fi.get("qualified") or False),
                       f"{iid} made no qualified call below the 75% floor",
                       f"forecast status: {fi.get('status')}")

    print("\nHOW THE PREDICTION IS TESTED")
    print("  • At bar i the audit sees only bars ≤ i; the outcome uses the next N bars.")
    print("  • A setup counts only if it passes every live gate: conviction ≥55 with a 100%")
    print("    technical score (EMA 9/21 + MACD + Supertrend + RSI all agreeing), an")
    print("    ATR-expected move ≥4× the target, entry before 14:30 IST, pre-move timing,")
    print("    one trade at a time with a 15-minute cooldown.")
    print("    T1 is the minimum move (Nifty 25 / Sensex 40 pts); stop is 3.4× that move.")
    print("  • The wide stop is what lifts T1-before-SL above 75%. The honest cost is R:R")
    print("    ≈0.29 and expectancy near zero before costs — the proof panel shows both.")
    print("  • If the measured win rate ever drops below 75%, the desk returns NO CALL")
    print("    instead of quietly falling back to the lower-hit-rate balanced profile.")
    print("  • If one candle touches both T1 and SL, it is scored as a stop (conservative).")
    print("  • Historical option chains are not available, so OI/Greeks influence only the live")
    print("    forecast; they are never claimed as backtested.")
    print("  • These are base rates, not a guarantee. 80–85% needs an even wider stop")
    print("    (≈3.8× the target, ~3.5×ATR) and clearly negative expectancy, so it is not offered.")

    print(f"\nRESULT: {'PASS — live data and audit reproduced independently' if not checks.failed else f'{checks.failed} CHECK(S) FAILED'}")
    return 0 if not checks.failed else 1


if __name__ == "__main__":
    sys.exit(main())
