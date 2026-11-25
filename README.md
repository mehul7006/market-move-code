# Market Signal Desk

Local real-data dashboard for **Sensex, Nifty 50, Bank Nifty, Fin Nifty, Midcap Select, Next 50 and 10
Nifty sector indices**. The six index segments generate entry/SL/T1/T2/T3 calls, each through its own
six-month tested profile. Sector indices are chart context only — they never generate calls. Click any
segment to see its direction, conviction, the full strategy board (every vote with its raw inputs), real
option/OI analytics (PCR, max pain, OI flow, put/call walls, ATM IV, straddle, Greeks) and a walk-forward
hit-rate measured on that instrument's own candles. A separate **Nifty 50** book shows all 50
constituents with live quotes, equal-weight breadth and a per-stock call only for names whose own
six-month audit clears 75%.

## Run

```bash
cd market-dashboard
python server.py            # Python 3.10+, stdlib only - no pip install
```

Open **http://localhost:8787**. The page polls every 5 s while the "Live · 5s" switch is on.

## Use it from anywhere — ngrok tunnel + nginx

```bash
python share.py                # start ngrok (downloads it once), print the public URL + logins
python share.py --nginx-only   # only write/refresh the nginx reverse-proxy config
python share.py --status       # current public URL and credentials
python share.py --stop         # stop the tunnel share.py started
```

`share.py` creates the access secrets in the gitignored `.env` the first time and prints them:

- `DESK_USER` / `DESK_PASS` — HTTP Basic login for the public link. It is enforced by the app
  itself, so it protects the ngrok URL too (ngrok points straight at 8787, bypassing nginx).
  Browsers ask once; the same credentials also satisfy the nginx auth block.
- `DESK_ADMIN_KEY` — unlocks the in-page **Access** panel that swaps the Upstox token.
  It has nothing to do with the Upstox secret; it only guards the token-write endpoint.
- `NGROK_AUTHTOKEN` — your ngrok authtoken, used from `.env` or ngrok's own saved login;
  if neither exists the script asks once and saves it.

nginx: the script writes `nginx/market-desk.conf` and `nginx/.htpasswd`, then prints the exact
command for the nginx build on this machine (`E:\nginx-1.28.3`):

```bash
"E:\nginx-1.28.3\nginx.exe" -p "E:\nginx-1.28.3" -c "<repo>\nginx\market-desk.conf"
```

It proxies `http://localhost:8899` → `127.0.0.1:8787` with no caching and a 5-minute read timeout
(the Proof audit is deliberately slow). Port 8899 is used because 8080 is already taken here; change
it with `--nginx-port`. Reload after editing: add `-s reload`. `--no-password` writes a config
without auth — trusted networks only. To disable the app-side login, delete `DESK_USER` and
`DESK_PASS` from `.env`.

ngrok's free tier shows its own browser warning page on the first visit to a new link; click through
it once. The page's API calls send `ngrok-skip-browser-warning`, so the dashboard itself is unaffected.

### Paste the Upstox token in the page

The header **Access** button opens the panel. Paste either the access token or the full URL you were
redirected to after Upstox login (its `code=` is exchanged using the secret already in `.env`).
The server validates the token against Upstox's profile endpoint *before* writing anything, then
saves it to `.env`, swaps the live client and clears the caches — no restart and no code edit. The
panel also shows the token's remaining life, a live Nifty quote proving the feed works, and the
current public URL. The token itself is never returned by the API or rendered in the page, and the
endpoint refuses requests without `DESK_ADMIN_KEY`.

## Data policy — real feed or a visible error, never mock data

| Feed | Source | Behaviour when unavailable |
|---|---|---|
| Spot quotes | Upstox `/market-quote/quotes` | quote chip shows `ERROR`, header pill turns red `UPSTOX OFFLINE` |
| Intraday 5m + daily candles | Upstox v3 historical/intraday | falls back to **real** Yahoo daily candles, chip `YAHOO`; otherwise `ERROR` and the chart is replaced by "No real market data for this segment" |
| Overnight / stale tail | Yahoo real 5m bars | Upstox EOD history lags the just-finished session after the IST midnight rollover, so the missing bars are filled from Yahoo. BSE index intraday can also freeze before the close (zero-range bars repeating the last value while the quote keeps moving); that stale trailing run is replaced by Yahoo's real 5m bar for the same minute. Both cases make the chip read `UPSTOX+YAHOO` (every bar real) |
| Option chain, OI, IV, Greeks | Upstox `/option/contract` + `/option/chain` | `OPTION FEED ERROR` card with the broker's message; no numbers are invented |
| Signal | Computed only from loaded real candles | "SIGNAL UNAVAILABLE — NO REAL DATA" card |

There is no synthetic/demo path in the code. If Upstox is down you see red errors and, at most, the
labelled Yahoo fallback — never fabricated prices or OI.

## Credentials

Credentials live in `market-dashboard/.env` (gitignored):

```
UPSTOX_API_KEY=...
UPSTOX_API_SECRET=...
UPSTOX_ACCESS_TOKEN=...
```

An env var overrides the file: `UPSTOX_ACCESS_TOKEN=... python server.py`.

### Token lifetime and refresh

Upstox access tokens expire. The header pill shows the remaining lifetime (`UPSTOX LIVE · 3h 40m`);
when the token is expired/invalid the pill shows `UPSTOX OFFLINE` with the API error and a hint.
Refresh it:

```bash
python upstox_login.py https://your-app-redirect-uri
```

The redirect URI must be the one registered on your Upstox app. Open the printed login URL, log in,
copy the `code=` value from the redirected address, paste it — the token is written into `.env` and
the running server picks it up on the next request. Or skip the terminal entirely and paste the token
(or the whole redirected URL) into the **Access** panel in the header.

## Move forecast — only fires when the minimum move is realistic

Each signal gets a real expected-move forecast. The six call-enabled segments use their own measured
precision profile (45–54 × 5m horizon, closed by 15:25 IST); sectors show chart context and no call:

- three independent estimators: ATR(14) × √horizon, realized candle σ × spot × √horizon, and the
  ATM straddle scaled from expiry to the horizon — the displayed estimate is their median, and the
  full estimator range is shown for audit
- a measured capture probability from this instrument's own history: of past same-direction setups,
  how often the minimum move was covered within the horizon
- a pre-move timing check: `EARLY` (range compressed at the 20-bar breakout edge, fresh
  Supertrend/MACD flip), `BUILDING`, `IN PROGRESS` (already broke out), `EXTENDED` (move mostly done)
- a trade plan with **three targets**: T1 = the minimum required move, T2 = the expected move clamped
  to the target zone, T3 = T2 + one more T2−T1 step (the stretch target). The precision profiles set
  the stop at 2.5–4 × the minimum move.

A call is shown as `OPPORTUNITY · PRE-MOVE` only when **all** of that segment's gates pass:
non-neutral direction, expected move ≥ minimum, a 100% technical score (EMA 9/21 + MACD + Supertrend;
RSI only votes at extremes), an ATR-expected move ≥ 4 × the target (5 × on Next 50), entry before
14:00/14:30 IST, timing still EARLY or BUILDING, the segment's EMA 50 / extreme-RSI / capture-odds
filters, and its live six-month audit still measuring ≥ 75% T1-before-stop over **all calls**
(timeouts count as non-wins). Otherwise the card shows the exact blocking reason
(`BELOW MIN MOVE`, `ATR EXPECTED MOVE … REQUIRED`, `ENTRY WINDOW CLOSED`, `LOW CONVICTION`,
`LOW SETUP QUALITY`, `INDICATORS SPLIT`, `NOT ALIGNED WITH EMA 50`, `RSI NOT AT AN EXTREME`,
`PROFILE EDGE WEAK`, `PROFILE AUDIT SAMPLE TOO SMALL`, `LOW CAPTURE ODDS`, `MOVE ALREADY STARTED`,
`RISK > REWARD`, `NO BIAS`) and the advice says no trade — it never forces a prediction.

Per-segment T1 / target zone: Sensex 40–80, Nifty 25–50, Bank Nifty 50–100, Fin Nifty 30–60,
Midcap Select 20–40, Next 50 60–120 points. Sector indices show no plan at all.

The research scanner re-runs the forecast on the six call-enabled segments, ranks qualifying pre-move
calls first, and reports `N of 6 segments qualifying`. If nothing qualifies it says so and lists the
nearest candidate — no fabricated call.

## Six call-enabled segments — measured ≥75% T1-before-stop, session-bounded

The old generic 60-minute / 0.5 × ATR plan hit T1 before the stop only ~41–44%, because the stop was
much tighter than the fixed target. The fix is not one universal rule: each of the six index segments
gets its own measured profile. They share the geometry (wide stop, unanimous trend/momentum indicators,
high expected-move filter, early entry cutoff, one trade at a time with a 15-minute cooldown) and differ
in horizon, stop multiple, entry cutoff and quality filters.

| Segment | T1 | Horizon | Stop | Entry by | Extra filter | Setups | **T1 win · all calls** | Worst month | Reach | Avg risk | R:R | Avg R | Exp / setup |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BSE Sensex | 40 | 54 (4.5 h) | 3.0 × T1 | 14:30 | EMA 50 + extreme RSI | 173 | **79.2%** | 70.4% | 89.6% | 120 pts | 0.33 | +0.059 | +7.1 pts |
| Nifty 50 | 25 | 54 | 2.5 × T1 | 14:00 | EMA 50 + capture 55% | 114 | **77.2%** | 71.4% | 84.2% | 62.5 pts | 0.40 | +0.088 | +5.5 pts |
| Bank Nifty | 50 | 54 | 4.0 × T1 | 14:00 | capture 55% | 123 | **78.9%** | 71.4% | 85.4% | 200 pts | 0.25 | +0.004 | +0.7 pts |
| Fin Nifty | 30 | 54 | 2.5 × T1 | 14:00 | capture 55% | 121 | **80.2%** | 61.9% | 84.3% | 75 pts | 0.40 | +0.126 | +9.4 pts |
| Midcap Select | 20 | 54 | 4.0 × T1 | 14:30 | extreme RSI | 146 | **78.8%** | 70.4% | 81.5% | 80 pts | 0.25 | +0.049 | +4.0 pts |
| Next 50 | 60 | 45 (3.75 h) | 4.0 × T1 | 14:30 | EMA 50 + capture 55%, exp ≥5× | 153 | **78.4%** | 70.6% | 85.0% | 240 pts | 0.25 | −0.006 | −1.4 pts |

Measured over six months of each segment's own 5m bars (11 May → 6 Nov 2026, 9,325 bars — every bar
the feed returned). "T1 win · all calls" = T1 hit before the stop ÷ every setup, with **timeouts counted as
non-wins**. Outcomes are session-bounded: the window closes at 15:25 IST on the entry day, so a call
that hits neither level is a timeout closed at the last price, not an overnight rescue. A candle that
touches both levels is scored as a stop first. The live gate re-runs this audit on every refresh and
refuses calls when the measured rate is below 75% or the sample is under 30 setups.

**Read this honestly.**

- These parameters were selected on the same six months they are measured on. The rates are
  **in-sample**, not a forward guarantee. A different six months can be worse; the floor is a
  continuously re-run audit gate, not a promise.
- The high hit rate is bought with a wide stop. R:R at T1 is 0.25–0.4, so break-even is roughly
  71–80% before costs. Bank Nifty and Next 50 are near zero expectancy even at ~79% hits; Fin Nifty
  and Nifty show a small positive average R. Check the Avg R / Exp columns rather than the hit rate
  alone.
- One month can be weak: Fin Nifty's worst month measured 61.9%, Bank Nifty's 200-pt stop means one
  stop wipes several T1 wins.
- Sector indices are deliberately excluded from call generation. `research_opt.py` and
  `.research/segment_study2.py` reproduce the search offline with the same no-lookahead outcome
  chronology.

### Last 30 days — T1, T2 and all three targets before the stop

The Proof panel re-runs the same profiles and gates on the newest 30 calendar days (07 Oct → 06 Nov
2026, all bars merged with the live intraday tail). T1 is the minimum move; T2 = 2× and T3 = 3× that
move. A target counts only when touched before the stop, and a candle touching a target and the stop
together is scored as a stop (conservative). Timeouts are not wins.

| Segment | Calls | T1 before SL | T2 before SL | T3 (all) before SL | Stops | Timeouts | Avg R (exit at T1) |
|---|---|---|---|---|---|---|---|
| BSE Sensex | 26 | 76.9% | 76.9% | 65.4% | 6 | 0 | +0.026 |
| Nifty 50 | 21 | 81.0% | 47.6% | 42.9% | 4 | 0 | +0.133 |
| Bank Nifty | 21 | 90.5% | 71.4% | 57.1% | 2 | 0 | +0.131 |
| Fin Nifty | 21 | 85.7% | 76.2% | 57.1% | 3 | 0 | +0.200 |
| Midcap Select | 29 | 79.3% | 65.5% | 51.7% | 2 | 4 | +0.078 |
| Next 50 | 30 | 80.0% | 70.0% | 56.7% | 5 | 1 | +0.010 |

One month is 21–30 setups per segment, so these are recent samples, not new proof. T2/T3 describe a
multi-target variant; the deployed call plan still exits at T1, so the Avg R column uses the T1 exit.
The six-month table above remains the basis for the live ≥75% gate.

The same section has **All 148 calls — entry, T1/T2/T3 and SL**: a lazy-loaded, newest-first row view
of every call behind those percentages (date/time IST, segment, side, entry, T1/T2/T3, SL, outcome,
exit, MFE, bars, R), plus filter buttons (All / T1 hit / T2 hit / T3 hit / Stops / Timeouts) and a
**Download CSV** link that follows the active filter. The data comes from:

```
GET /api/calls?days=30                 # JSON: segments, totals, every call
GET /api/calls?days=30&filter=t2       # only calls that reached T2 before the stop
GET /api/calls?days=30&filter=t2&format=csv
```

`filter` accepts `all`, `t1`, `t2`, `t3`, `sl`, `to`. `verify.py` re-runs these rows from its own
independent Upstox pull and diffs entry, T1, T2, T3, SL and outcome for every call, and checks that
the T2 filter returns exactly the recomputed T2 hits — not just the aggregate percentages.

## Proof and independent verification

Two ways to check that the predictions are computed from real market data:

1. **Proof button** in the header — provenance for all six call-enabled segments (quote/candle/option
   source, bars, last bar, last close), the live forecast profile, and a no-lookahead walk-forward
   audit of that exact profile on six months of 5m bars: setups, wins/losses/timeouts, T1-before-stop
   win rate over all calls, worst month, minimum move reached, average bars to T1, average risk,
   R:R at T1, average R, break-even hit rate and expectancy, plus the last six audited setups.
   A last-30-days table adds separately measured T1, T2 and T3-before-stop rates per segment, and a
   "Load all N calls" view lists every one of those calls with T1/T2/T3, SL, exit, MFE and R (CSV too).
2. **`python verify.py`** — a separate CLI that pulls Upstox quotes/history and Yahoo 5m bars *again*,
   diffs every overlapping bar against the dashboard, pulls its own six-month history for all six
   segments, recomputes every audit and exits non-zero on any mismatch. It never trusts the dashboard's
   own numbers. The Nifty 50 block adds: the served constituent set equals NSE's live CSV, breadth is the
   arithmetic of the 50 quotes, every stock obeys the 55% / ≥100-setup / positive-expectancy gate (a
   CALL must be qualified, a BLOCKED stock must not be), three quotes match an independent pull, two
   stocks' 180-day audits are re-pulled and recomputed (samples within ±3, rate within 1 point),
   truncating the newest bar leaves every earlier setup unchanged, the live status matches the
   independent gate, and the CSV has 50 rows.

```bash
python server.py     # terminal 1
python verify.py     # terminal 2 — prints PASS/FAIL per check
```

The audit is honest by construction: at bar *i* it only sees bars ≤ *i*; the outcome window is capped
at 15:25 IST on the entry day; one trade at a time with the same cooldown the live profile uses; a
candle touching both T1 and the stop is scored as a stop; a call that hits neither is a timeout closed
at the last price and is **not** a win; "reach" (minimum move touched at any point) is reported
separately from the tradable win rate. Historical option chains do not exist in the feed, so OI/Greeks
affect the live forecast only and are never claimed as backtested. Average R and expectancy are shown
next to the hit rate so the wide-stop trade-off is visible rather than hidden.

## Research scanner

The **Research** button in the header scans the six call-enabled segments (sector indices are not
selectable), then runs a forced refresh and ranks them:

- breadth (bullish / bearish / neutral), scan verdict and highest-conviction pick
- per-segment direction, confidence, entry, stop, T1/T2/T3, PCR, max pain, OI flow, IV, timeframe, drivers
- every row can be opened directly on the dashboard ("Open")

`/api/research?segments=SENSEX,NIFTY` returns the same report as JSON.

## Today's calls

The **Today's calls** button replays the session bar by bar for the six call-enabled segments through
the exact gates each live profile uses (indicator unanimity, ATR-expected move, entry window, pre-move
timing, and where the profile asks, EMA 50 / extreme RSI / measured capture). At each 5m bar only bars
up to that bar decide the call, so the list is not a log of past advice and cannot peek ahead.

Outcomes are session-bounded: the window closes at 15:25 IST on the entry day. Each call shows entry,
stop (with its distance in points), T1/T2/T3 with point targets, R:R to T1, the result, how long it
took, and the maximum favourable excursion. The summary gives calls, T1 hits, stop hits, **timeouts**,
still-open calls, win rate over **all calls**, win rate over resolved calls, average R, minimum move
reached, and average time to T1/stop. A TIMEOUT hit neither level by 15:25 and is closed at the last
price — it is not a win. A call still inside its window while the session is live is marked **OPEN**
with elapsed minutes. Segments whose feed failed, have too little history, or whose six-month audit is
below 75% are listed with the reason and contribute no calls.

The panel is a replay of real candles, refreshed on each open — the six-month measurement that gates
each profile lives in the Proof panel, not in a single session.

## Next-day model — buy today's close, sell tomorrow's close

The **Next-day** button opens a separate long-only overnight book. It is additive: the intraday engine,
profiles, 75% gate and every other panel are unchanged.

Rule (daily bars only):

- **Signal:** at a session close, RSI(14) ≤ 38 **and** the 3-day return ≤ −1%.
- **Entry:** that close. **Exit:** the next trading session's close.
- **Grade A:** RSI ≤ 30 and 3-day return ≤ −2%; everything else is Grade B.
- **Long only.** Overbought closes showed no stable next-day short edge in the same test.
- **No stop or target is audited.** The 0.5 × ATR target and next-day ATR range are context only; the
  measured result is close-to-close, so an overnight gap can exceed any risk guide.

Measured over ~3 years of real daily bars (`overnight.py`, reproduced independently by `verify.py`):

- 366 signals on 128 distinct oversold days; next close higher **62.6%** (date-level **64.8%**)
- average **+50.9 points (+0.171%)** per call before costs; profit factor **1.29**
- older two-thirds 59.4%, newest third 68.9%; Grade A 69.5% (59 signals), Grade B 61.2% (307)
- 21 of 25 months positive; worst close-to-close loss −2,775.6 pts (Next 50)
- per segment: Sensex 61.0% (59), Nifty 56.5% (62), Bank Nifty 66.0% (50), Fin Nifty 66.7% (54),
  Midcap Select 62.1% (66), Next 50 64.0% (75)

A segment only receives a call when its own audit has at least **30 signals** and a next-close
direction rate of at least **55%**. Otherwise the panel shows **BLOCKED** (rule triggered, edge below
the floor) or **NO SETUP**. Signals cluster on the same weak days, so several segment rows from one day
are one market bet, not independent trades. The rates are in-sample and gross of brokerage, taxes and
slippage; with +0.17% gross expectancy, costs are material.

Daily bars come from Upstox (~3 years). Until Upstox publishes today's EOD bar, today's daily bar is
aggregated from the real Upstox 5m series and labelled `UPSTOX 5m->1D`; Yahoo daily is a labelled
fallback where a symbol exists. **Download signals CSV** exports the audited ledger.

## BTST — next-session open prediction

Shown at the end of the **Next-day · BTST** page (`btst.py`, `GET /api/btst`). A separate model: it does not read or
change the intraday sure calls, the confidence calls, the short-term book or the overnight model.

- **Predicts** the direction of the next session's OPEN versus today's close for each index segment, with the
  out-of-sample measured gap-up rate for similar evenings, a predicted open and a typical zone.
- **Six BTST strategies** (long only, fixed round thresholds, each kept because it held in all four eras 2015-18,
  2019-21, 2022-24, 2025-26): momentum close + VIX easing, gap-down recovered, strong close + breadth + VIX easing,
  breakout close, 3-day trend + strong close, high-VIX strong close; plus confluence (3+ agree).
- **Confidence** = the lowest of the firing strategy's shrunk win rate on that index over all history, the last ~2
  years and the last ~1 year (shrunk toward 50% by 10 imaginary coin-flip trades; ≥ 25 past setups required).
- **Call tiers:** SURE = confidence ≥ 70% and the walk-forward model gives ≥ 60% gap-up (or the original
  momentum-close SURE gate); HIGH-CONFIDENCE = confidence ≥ 60% and model ≥ 60%. Anything lower is a lean only.
- **Measured (causal replay, gross, Nifty/Sensex/Bank Nifty/Fin Nifty):** 2,012 calls, 74.1% opened in favour;
  70.8% since 2023, 68.1% since 2025, 66.3% in 2026. Per index last year: Nifty 73%, Fin Nifty 67%, Sensex 61%,
  Bank Nifty 50% (weakest — treat its calls with care).
- **No STBT:** six gap-down strategies were tested the same way and all won only 30-48% — Indian indices gap up after
  weak days more often than not — so there are no short calls.
- **Timing:** FORMING before 15:10 IST, ENTRY WINDOW 15:10–15:30, FINAL after the close. Exit at the next open.
  No overnight stop: every card shows the worst gap seen.
- **Data:** ~10 years of Upstox daily bars (Yahoo fallback); sessions missing from the EOD history are aggregated
  from real Upstox 5m bars and labelled `UPSTOX 5m->1D`. `python test_btst.py` checks the tiers, the ≥ 60% replay
  and that removing future bars changes no earlier feature, probability or call.

## Nifty 50 stock book — breadth plus audited per-stock calls

The **Nifty 50** button opens the whole index, not just the index future: all 50 constituents with a
live quote each, an equal-weight breadth count, and a per-stock intraday call only for names that clear
the same kind of audit the six segments use.

- **Universe:** NSE's own `ind_nifty50list.csv` (Company Name, Industry, Symbol, ISIN). Cached for 7
  days; if NSE is unreachable the desk uses a dated snapshot of that same file and labels it as such
  (`universe.live: false`, with the snapshot date in `note`). Instrument keys are `NSE_EQ|<ISIN>`;
  quotes for all 50 plus the Nifty index arrive in one Upstox batch call.
- **Data:** 180 calendar days of real Upstox 5m bars per stock (the v3 history endpoint caps a request
  at ~30 days, so it is pulled in six chunks and merged with the live intraday tail). Cached for 6
  hours per stock in `.candle_cache/`; audits persist in `.candle_cache/nifty50_audits.json`.
- **Per-stock gate:** identical structure to the segment profiles — EMA 9/21 + MACD histogram +
  Supertrend + RSI-extreme unanimity, EMA 50 regime, entry before 14:30 IST, ATR(14) at least 0.08% of
  price. T1 = **0.30% of the entry price**, stop = **0.50%** (R:R 0.60), 36-bar (3-hour) horizon,
  session-bounded at 15:25 IST, 3-bar cooldown, one trade at a time. A candle touching T1 and the stop
  together counts as a stop; a timeout is closed at the last price and is **not** a win.
- **Why those sizes:** they come from the movement, not from the win rate. Across 112k gated 5m
  triggers, the median favourable excursion inside the 3-hour window is **0.42% of price** (p25 0.19%,
  p75 0.76%, p90 1.22%) and 62% of setups reach 0.30%. The old 0.6×ATR target had a median of just
  0.13% of price — about a third of the move that was actually there. 0.30% is ~72% of the median move
  with a 0.50% stop; a 0.25% target had the best total R, a 0.35% target cut the hit rate faster than it
  raised R, so 0.30% is the deployed middle.
- **Gate floor:** a stock may call only while its own 180-day audit shows **≥55% T1-before-stop over
  ≥100 setups AND a positive average R** (expectancy above zero at the deployed 0.60 R:R). Everything
  else is shown as **BLOCKED — no call** with the measured rate, sample size and average R, or **NO CALL**
  when the stock qualifies but the live gate is not triggered right now. There is no weaker fallback
  profile, and a stock that has not finished auditing shows **AUDITING**, never a guess.
- **Measured spread (2026-11-07, 180-day audits, ~211–232 setups each):** qualification rates run from
  ONGC 51.1% to HINDUNILVR 68.0%; **29 of 50** names clear both limbs. The rate alone is not enough:
  ICICIBANK 55.2% / −0.001R, POWERGRID 56.1% / −0.017R, EICHERMOT 56.5% / −0.026R, NTPC 57.0% / −0.034R
  and AXISBANK 57.2% / −0.020R clear 55% but are blocked because their expectancy is negative. The line
  falls where the data puts it; the desk does not lower it to make more names speak.
- **Breadth:** advancers / flat / decliners with a ±0.02% dead band, plus the equal-weight and median
  % change against the Nifty's own %. This is a count of constituents, **not** the index's free-float
  weighted contribution, and single stocks can gap in ways an index does not.
- **Risk:** R:R to T1 is 0.60 (stop = 1.67 × T1), so break-even is ≈62% before costs. Measured T1 rates
  at this target sit at 56–68% per qualified name (~61% pooled) — a thin margin, not a promise. The
  rates are in-sample on the same 180 days and gross of brokerage and slippage. This is a research
  gate, not a forecast of the next trade.

The audit runs in a background worker that lazy-starts on the first request: ~3.2 candle requests/s
(180/min), all 50 stocks in one pass (cold ≈ 2 minutes with 300 history requests, warm ≈ 20 seconds),
refreshed every 20 minutes. The panel polls quotes every 30 seconds and shows the audit progress bar
until the first pass completes. **Download CSV** exports the current book (price, % change, status,
plan, audit samples and rate, bar time, reason). **Re-audit** forces a fresh pull of all 50.

### Last 30 days — what the gate actually called

The **30-day test** button inside the Nifty 50 panel replays the last 30 calendar days on the same real
5m bars and reports every call it fired: calls, T1 hits, T2 hits, T3 hits, stops and timeouts, per stock
and in total, plus the most recent calls and a CSV ledger.

- **No peeking:** a stock is graded only if its own audit cleared the 55% / ≥100-setup / positive-R gate
  using bars **strictly before the window** (frozen at the window start). The window cannot vote on its
  own admission, and the frozen rate is shown next to each stock.
- **What counts:** T1 is touched before the stop; T2 = 2 × T1 and T3 = 3 × T1 touched before any stop;
  a candle that touches the stop and a target together counts as a stop; a timeout (neither level by
  15:25 IST) is closed at the last price and is **not** a win.
- **Latest call per stock:** one row per stock with the side (long/short), the entry share price, the
  T1/T2/T3 and SL price levels (with the points and % distance from entry), and that stock's measured
  win rate over the window plus its frozen gate rate. The per-stock summary above it shows hit counts —
  its T1/T2/T3 columns are counts, not prices.
- **Every prediction, with its levels:** each row shows the entry, the SL, T1/T2/T3, the level that
  was reached before the stop (T1/T2/T3, SL or TIMEOUT), the bar time the call fired, how long it
  took, the exit time and price and the R result. Exit price is the T1 exit the desk deploys, so a
  T3 row still banked T1. Timings measured over the window: T1 resolves in ~27 minutes on average,
  a stop in ~37 minutes, a timeout runs the full ~118 minutes.
- **Measured 2026-10-08 → 2026-11-06:** 26 of 50 names qualified before the window (the rest had not
  cleared the frozen gate); **978 calls**, **621 T1 (63.5%)**, **393 T2 (40.2%)**, **252 T3 (25.8%)**,
  **309 stops (31.6%)**, **48 timeouts (4.9%)**, average **+0.06R** per call (total +59.1R at T1). For
  comparison the ungated all-50 slice is 1,884 calls at 62.4% with 626 stops and +0.035R — the gate now
  adds ~1 point of hit rate and nearly doubles the per-call expectancy, and it keeps the desk from
  calling the 21 names that measured a negative R.
- **How the win rate is counted:** T1 ÷ all calls, where a call is a win only if T1 is touched before
  the stop. A candle that touches both T1 and the stop counts as a stop, a timeout is not a win, and
  after a stop no target can be added. The 63.5% is an exit-at-T1 number; a call that ran to T3 but was
  never stopped was already banked at T1.
- **Timing honesty:** the audit measures how far price had already moved in the call's direction before
  entry. Median run over the prior 20 bars is **+0.613% of price**, and in **81.6% of calls that prior
  run had already covered T1** — these are continuation calls, not fresh reversals. Four tested
  "fresher entry" guards (pullback, bar-age, run-cap and volume filters) all cut the number of calls and
  none raised the hit rate, so the desk reports the lateness instead of pretending it is early. The six
  index segments have the same shape: **94–96% of their calls had already covered T1** in the prior 20
  bars, against a forward excursion of about 5× T1.
- **Honest read:** R:R to T1 is 0.60 (stop = 1.67 × T1), so break-even is ≈62% before costs against a
  measured 63.5% — a thin margin that brokerage and slippage can erase. The gate threshold was chosen on
  overlapping history, and these are gross figures on 30 days of one market regime. The edge is in the
  selection and the expectancy floor, not in a large per-trade payoff.

## API

- `GET /api/market?force=1` — full payload: feed status, per-instrument candles, options, signal, forecast
- `GET /api/research?segments=ID1,ID2` — scan report (omit `segments` for all six call-enabled)
- `GET /api/proof` — provenance + per-segment walk-forward audits behind the Proof panel
- `GET /api/today` — today's calls replayed through the live gates, with session and per-segment stats
- `GET /api/call-confidence` — read-only: the measured win rate behind each kind of call (sure, confidence, desk profile), shown as the Confidence chip next to every generated call; it never changes which calls are generated
- `GET /api/btst` — BTST next-open predictions, SURE / HIGH calls and the causal track record; `?format=csv` returns every replayed call
- `GET /api/overnight` — next-day model audit, live signals and plans; `?format=csv&days=365` returns
  the full audited signal ledger as CSV
- `GET /api/nifty50` — all 50 constituents: live quote, breadth, own 180-day audit, status/plan;
  `?force=1` re-audits, `?format=csv` returns the stock book as CSV
- `GET /api/stocktest?days=30` — last-N-days stock calls with the gate frozen before the window,
  T1/T2/T3/stop/timeout counts per stock and in total; `?format=csv` returns the call ledger

## Signal engine (what is actually used)

- Trend: EMA 9/21, EMA 50 regime, Supertrend 10 × 1.5
- Momentum: Wilder RSI(14), MACD(12,26,9) histogram
- Volatility: ATR(14) for stop distance and R multiple
- Options: PCR, OI flow (PE−CE ΔOI), max pain pull, OI range position, put/call walls, delta-weighted
  near-ATM OI skew, ATM IV and Greeks (Δ, Γ, Θ, Vega)
- Weighted votes → score in −1…+1 → direction thresholds ±0.18; entry = spot; SL = Supertrend or
  1.5 × ATR; T1 = 1R, T2 = 2R; conviction = |score| × 100 (capped 95). These are the *signal* levels;
  when a move forecast qualifies, the card, chart and research table use the forecast plan instead
  (T1 = minimum move, T2 = expected move, T3 = stretch target, stop set by the active profile).

## Honest note on accuracy

The 77–80% T1-before-stop figures are **wide-stop** base rates measured in-sample over six months, not
a free lunch: R:R at T1 is 0.25–0.4, so break-even is roughly 71–80% before costs. Bank Nifty and
Next 50 are near zero expectancy even at ~79% hits; Fin Nifty and Nifty show a small positive average
R. A single month can be weak (Fin Nifty's worst measured 61.9%). The desk's edge is that it refuses
to call when the audit, sample size or the indicators do not clear the gate; it never forces a
prediction and never uses a lower-hit-rate fallback. Treat the output as structured research plus risk
discipline. Derivatives carry substantial risk.

The next-day book is a different model and is not held to the intraday 75% standard: it is a ~62.6%
direction rate, has no audited stop, and its signals cluster by date. It is long-only, in-sample, and
gross of costs — a weak close followed by a large gap down is the risk it does not hedge.

## Files

- `server.py` — HTTP server, Upstox orchestration, caching, market payload + research report + proof + today's replay
- `verify.py` — independent provenance/accuracy checker (separate Upstox + Yahoo pulls)
- `upstox.py` — Upstox v2/v3 client (quotes, candles, option contracts, chain → OI/Greeks)
- `upstox_login.py` — OAuth code exchange, rewrites the token in `.env`
- `analysis.py` — indicators, option analytics, composite signal, move forecast, walk-forward audits, session replay
- `btst.py` — BTST next-open model: six strategies, confidence tiers, walk-forward probability and causal replay
- `overnight.py` — next-day oversold model: signal, no-lookahead ledger, audit and live plan
- `nifty50.py` — Nifty 50 constituents, breadth, background 180-day per-stock audit and gate
- `research_opt.py` — offline six-month profile search (stop width × horizon × filters, monthly folds)
- `data_sources.py` — real Yahoo chart fallback
- `public/` — dashboard (vanilla JS, canvas charts, no CDN)

## Later: packaging as an APK

The same dashboard can be wrapped into an Android APK with Capacitor (WebView + packaged local server).
Do that after the feeds and token-refresh flow are settled.

## Deploy on Render (free tier)

The repo ships a `Dockerfile` and a `render.yaml` Blueprint. The app is pure Python (no packages to install).

1. Push this repo to GitHub, then in Render: **New + → Blueprint → select the repo** (or **New + → Web Service → Docker**
   with plan **Free**, region **Singapore**, health-check path `/healthz`).
2. Set these environment variables (Render asks for them; the server **refuses to start** without a login):

   | Variable | What |
   |---|---|
   | `DESK_USER`, `DESK_PASS` | the site login (required) |
   | `DESK_ADMIN_KEY` | unlocks the **Access** panel where you paste the daily Upstox token |
   | `UPSTOX_API_KEY`, `UPSTOX_API_SECRET` | your Upstox app credentials |
   | `UPSTOX_ACCESS_TOKEN` | optional - you can paste it in the Access panel instead |
   | `LITE_MODE=1` | already set by the Dockerfile / blueprint |

3. Open the Render URL, log in, press **Access**, paste a fresh Upstox token (it expires every night ~03:30 IST).

### What "LITE_MODE" does on the free tier
Render free = **0.1 CPU, 512 MB RAM, no disk, sleeps after ~15 min without visitors**. LITE mode makes the app safe there:
requests never wait for a heavy build (pages answer *"waking up"* and retry by themselves), the background refresh runs only
while someone is looking and runs at the lowest OS priority, and the 6-month scorecard is served from `scorecard_snapshot.json`
(refresh it locally with `python scorecard.py --snapshot` and commit). Measured under those exact limits (`docker run --cpus 0.1
--memory 512m`): dashboard ready ~90-100 s after the server wakes, every page ready in ~4.5 min, peak memory ~200 MB.

Things to know: after a sleep/restart the pasted token is gone (no disk) - paste it again or update `UPSTOX_ACCESS_TOKEN` in the
Render dashboard; to keep the service awake use a free uptime pinger (UptimeRobot / cron-job.org) on `/healthz` every 10 minutes
(about 744 h/month, inside Render's 750 free hours); live tracking refreshes about every 1-2 minutes on 0.1 CPU, not every 5 s.

Run it locally the same way: `docker build -t market-desk . && docker run --env-file .env -p 10000:10000 market-desk`.

## Managing a short-term call (one lot, no partial booking)
How the single position is managed once a call is open changes your **profit**, never the win rate (a win is still "T1 reached
before the stop"). Pick a rule on the Short term page:

| Rule | What you do (one lot) |
|---|---|
| **Runner** (default) | move the stop to entry once T1 is touched, then hold until the window ends or the stop is hit |
| Protect early | once price is +½ of T1, move the stop to entry + ¼ of T1, then hold - fewer full stop-outs, less robust |
| Fixed T3 target | stop to entry after T1, take profit at T3 |

Backtest on 1,485 real calls (older history, then the last 60 days): Runner earned **0.227 R / 0.44 R per call versus 0.118 / 0.206**
for the old "1/3 at each target" plan, and stays positive (+0.03 / +0.10 R) under a strict same-minute assumption. About 24% of calls are
still full stop-outs and about 6 in 10 end near zero; a few big trend moves pay for them. I also tested entry filters (higher-timeframe
agreement, volatility, trend strength, VWAP distance, candle shape, time of day), wider and tighter stops, breakout / opening-range /
previous-day-level signals, and pullback / confirmation entries: none reduced stop-outs without losing profit or held up on the last
60 days, so the entry and stops were left unchanged. Brokerage and slippage are not included.

## Choosing the T1 size (short term)
Shipped ladders keep T1 at about 1/4-1/2 of the stop - that is what holds 70%+ wins. A bigger T1 pays more per winning call but wins
less often (1,485 real calls, win rate = T1 before the stop): T1 = 1/2 stop 70.8%, 3/4 stop 64.0%, equal to the stop 57.5%. Entry filters
and new signal types (opening-range, breakout, previous-day levels, all combined) did not restore 70% at T1 = stop (best filters +2-5
points; new signals win 45-51% there). The Short term page's "Target size" card lets you view the same calls with T1 = 3/4 or equal to the
stop (single-lot Runner). `python test_short_term.py 120` prints the full comparison for 1, 5, 15 and 30 minute candles.

### Strategy-builder research for "T1 = SL" (what was tested, on real data)
Goal tested: T1 equal to the stop, win rate 65-70%+, much higher average points per call. Result: **not reachable with the data available.**
- Entries scored with 33 features (candlestick patterns, momentum over 1-24 bars, EMA/VWAP/RSI/MACD positions, volatility regime, day-range
  position, prior-day levels, gap, 15m/30m trend, time of day, day of week) by a boosted model trained only on earlier months and tested on the
  next unseen month (Jul-Nov): out-of-sample win rate at T1 = SL was 38-59% and the model almost never scored anything above 60%.
- The same model used as a gate on the current VWAP-reclaim signals helped only inconsistently (e.g. Nifty 53% -> 57%).
- New signal types (opening-range, breakout, previous-day high/low, new-day-high momentum, all combined): 45-53% at T1 = SL.
- 25 single entry filters and the best filter pairs: +2-5 win-rate points at best, not stable on the last 60 days.
- Volume / OI: index candles carry no volume; futures volume/OI exist only for ~4 weeks (47 Nifty signals) and did not help. Option greeks
  / delta history is not available (expired contracts need the paid Upstox Plus plan), so they could not be backtested.
- Best honest T1 = SL setup: size picked per index and candle from older data, checked on the unseen last 60 days: 61.1% wins pooled over
  429 calls (see the "T1 = SL (tuned per index)" choice). Average per call there: Nifty about +4 to +5, Sensex +9 to +34, Bank Nifty +6 to +18,
  Next 50 +27 to +107 points - well below Nifty 25-30 / Sensex 75-100, which no tested approach came close to.

## Checking someone else's calls (and why "average per call" claims differ)
`python audit_calls.py calls.csv` replays any list of calls (date, time, index, side, entry, sl, t1, t2, t3) on the real Upstox 1-minute
candles and reports which level was touched first, when, the win rate, the points you could actually book (exit at T1 or the stop) and the
"hindsight" points (the best price reached after the call - not tradeable). On this project's own calls, the hindsight average is Nifty
~40-50 and Sensex ~180 points per call while the tradeable average is +2 to +19: the same calls produce a "30-40 per call, 70-80% wins" headline
when counted by hindsight. Telegram groups also usually quote option-premium points (an ATM option moves roughly half as much as the index).
`python test_short_term.py 120` prints the three ways of counting side by side for every index and candle size.

## Strict short-term selection (Nifty, Bank Nifty, Sensex)
`strict.py` scans six signal types (VWAP reclaim, trend pullback, opening-range breakout, EMA cross, 20-candle breakout, MACD cross) and scores every signal
with a per-index model (`strict_model.json`, fitted by `python train_strict.py` on real Upstox candles). Each index has its own rules (`strict.RULES`, `train_strict.py`):

| Index | T1 / stop | Candles | Share of signals that become calls |
|---|---|---|---|
| Nifty | 22.5 / 25 | 1m + 5m | top 10% |
| Bank Nifty | 63 / 70 | 1m only | top 5% |
| Sensex | 81 / 90 | 5m only | top 40% |

T1 is 90% of the stop (never smaller); T2 = 2x and T3 = 3x T1. One open call per index at a time (a 1m and a 5m signal at the same moment count once), at most 5 a day.
Out-of-sample (trained on earlier data, tested on three 30-day blocks): Nifty 2.2 calls/session, 65.1% wins, +9.7 pts/call; Bank Nifty 1.2, 70.3%, +35.7;
Sensex 2.0, 66.9%, +38.5; all three 324 calls, 67.0%, +26.4 per call, +8,553 points. 120-day replay: 449 calls, 57.9% wins, +22.7 per call, +10,196 points.
Tested and rejected (did not hold up): 10 extra inputs (peer-index direction, previous-day levels, gap, streaks, a slower timeframe), a boosted-stump model, and
stronger ridge / recency weighting (better out of sample but worse on the 120-day replay, so treated as noise). The rules were the best of many combinations
on that same test, so expect live results somewhat lower. `strict.CROSS_DEDUPE_MIN = 5` would
count the same side called on several indexes within 5 minutes as ONE call (best score kept); it is off because it removes profitable calls.
Re-run `train_strict.py` (needs a valid Upstox token) every few weeks; `python test_short_term.py 120` lists every call of the last 120 days.

### B tier (more calls) - tested, off by default
`strict_model.json` also holds a looser threshold per index (`thrB`: Nifty top 15%, Sensex top 50%; Bank Nifty none - its extra calls lost money). Turned on
(`STRICT_B_TIER=1`) it adds ~0.5-1.4 calls a session, labelled "B tier" on the page. Out of sample the B calls won about 59%, but on the 120-day replay they won
49.5% (+9.4 per call) and lowered the totals (494 calls, 55.7% wins, +18.2 per call, +9,001 points against 449 calls, 57.9%, +22.7, +10,196 without them),
so it stays off.

## Option-chain recorder
`optrecorder.py` saves a snapshot of every index's near-expiry option chain (OI, change in OI, volume, IV, delta/gamma/theta/vega for the strikes around the
money, put/call ratio, plus futures OI/volume) every 5 minutes while the market is open, to `<CACHE_DIR>/optrec/<INDEX>_<YYYYMMDD>.jsonl`. Upstox serves no history of
these without the Plus plan, so this starts one. It runs inside the server (on by default; `RECORD_OPTIONS=0` turns it off; off on the Render free tier unless
`RECORD_OPTIONS=1`, because the free disk is wiped when the service sleeps). `python optrecorder.py --summary` or `/api/recorder` show what has been recorded;
after a few weeks the fields can be added to `train_strict.py` as inputs and tested walk-forward like everything else. Only real Upstox data is stored.

### Confirmation entry (Sensex)
A Sensex call is entered only if price trades 4.5 points beyond the signal close, in the call's direction, within 10 minutes (a stop-entry order); the levels are
measured from that price, otherwise the call is cancelled (`strict.RULES[...]["confirm"]`). Out-of-sample: 70.2% wins and +54 per call against 66.9% / +38.5, better in
every 30-day block; 120-day replay: 58.8% wins, +42.6 per call, +7,751 points against 58.0%, +37.2, +7,182. Tested and not used: Nifty (more wins out of sample, lower
net on the replay), Bank Nifty (worse), pull-back limit entries (fewer wins: the calls that pull back are the ones that fail) and a two-model consensus gate (no gain).
While a call waits for its trigger the Today's calls page shows it as "BUY ABOVE x until hh:mm".

### What drives accuracy (driver analysis) and the Sensex quality filter
Among the calls the strategy issues, the inputs that separated winners from losers most (out of sample, low third vs high third of the input):
Nifty - move so far today (58% vs 77% wins), how many other signal types agree (59% vs 72%); Bank Nifty - position in the day's range (57% vs 82%);
Sensex - run-up before the signal (74% vs 63%), distance from VWAP (78% vs 68%), RSI stretch (70% vs 61%). Each "skip the weakest third" filter was tested;
only Sensex's (skip calls whose RSI is already stretched in the call's direction, `strict.FILTERS`) improved both the win rate and the net in every 30-day block
AND on the 120-day replay (Sensex 62.0% wins, +63.4 per call against 58.8%, +42.6). The Nifty and Bank Nifty filters looked good out of sample but lowered the net on
the replay, so they are off. 120-day replay, all three indexes: 377 calls, 59.2% wins, +28.4 per call, +10,690 points (before: 438 calls, 58.2%, +24.6, +10,764).

### Dedicated Nifty strategy
Searched: stops 25-60 (T1 90-100% of the stop), model target, candle sizes, time windows, strictness, confirmation entries and one quality filter, each judged out of
sample AND on the 120-day replay. Chosen: T1 22.5 / stop 25, 5-minute candles only, model trained on "T1 before the stop", top 10% of scores, signals before 09:40 skipped.
Out-of-sample 42 calls, 73.8% wins, +20.9 per call; 120-day replay 67 calls, 64.2% wins, +14.5 per call (before: 156 calls, 56.4%, +9.3). Larger stops (30-60) did not
reach 62% wins with a positive net in every block. All three indexes on the replay: 288 calls, 61.8% wins, +35.5 per call, +10,214 points.

### Sure calls vs 60%+ confidence calls, same-moment rule, trend-day exception
- **Sure calls** (the Today's calls page, and every count / win rate / net figure): the strict per-index logic. When the same side is called on several indexes
  within 5 minutes it is ONE sure call - the index whose score clears its cut-off by the most (`strict.CROSS_DEDUPE_MIN = 5`).
- **60%+ confidence calls** (a separate view, never counted): the same-moment calls on the other indexes. Looser score cut-offs were tested for this page, but
  their extra calls won only 44-57% out of sample, so they are not shown (`train_strict.py` prints that test; `thr60` in the model stays empty until one reaches 60%).
- **Trend-day exception (Nifty only)**: when Nifty has already moved a lot today in the call's direction and the higher timeframe agrees, the cut-off is lowered
  and the 09:40 filter skipped. Tested also for Bank Nifty (win rate fell) and Sensex (net fell). Entries until 15:00 and a separate afternoon cut-off were
  tested and did not help.
- 120-day replay, sure calls: 255 calls, 62.7% wins, +36.8 per call, +9,376 points (Nifty 72.1% / +16.1, Bank Nifty 58.5% / +11.1, Sensex 62.7% / +64.7).

## Upstox request budget (rate limits)
Upstox's standard limits are 50 requests/second, 500/minute and 2,000 per 30 minutes. The app used to ask for about 220 calls a minute while the market was open
(5-minute candles of all 16 instruments every ~9 s, 1-minute candles every 3 s, ...), which could trip the 30-minute limit. Now:
- `upstox.CallGovernor` keeps a local budget (300/minute and 1,500 per 30 minutes; `UPSTOX_MAX_PER_MIN`, `UPSTOX_MAX_PER_30MIN`). When it is used up the call is not sent and the
  page keeps showing the last good data (`server.cached` falls back to the previous value on any failure).
- A 429 from Upstox starts a cool-down (its Retry-After, at least 30 s, doubling up to 5 min) with no calls in between, instead of retrying immediately.
- Data is reused longer: quotes 5 s, traded indexes' 1-minute candles 20 s and 5-minute candles 30 s, other indexes 180 s, option chain and futures 30 s; the
  background refresh runs every 6 s while the market is open and every 30 s when it is closed (`UPSTOX_TTL_*`, `UPSTOX_REFRESH_*` to change). Live stop / target touches
  still use the latest traded price from the 5-second quote, so they are not delayed.
- Estimated load while open: ~220 -> ~50 calls a minute (about 1,500 per 30 minutes). `/api/upstox-usage` shows the real counts per endpoint.
The access token itself still expires every night (about 03:30 IST) - that is Upstox's rule and does not depend on the number of calls.

## Render: avoid pasting the Upstox token again and again
An ordinary Upstox access token dies every night (~03:30 IST) and Render's free disk is wiped whenever the service sleeps, so a pasted token is forgotten. The fix that needs
no code change on Render: generate Upstox's **Analytics token** (Developer Apps -> your app -> Analytics tab; read-only, valid 1 year, market + historical data) and put it in the Render
environment variable **UPSTOX_ACCESS_TOKEN** once. The app accepts it (it validates it with a market-data call, because this token cannot read the user profile), it survives
sleeping and redeploys, and a newer token in the environment always beats an older saved one. When the token is expired or invalid the page now shows ONE calm sentence
("Upstox access token expired or invalid - open Access and paste a fresh token ...") instead of raw errors, keeps showing the last data, and stops calling Upstox for 2 minutes
at a time instead of hammering it; saving a new token clears that pause immediately.

### Two tokens: daily token + 1-year Analytics token (automatic fallback)
`UPSTOX_ACCESS_TOKEN` = the normal daily token; `UPSTOX_ANALYTICS_TOKEN` = Upstox's 1-year read-only Analytics token (both in the gitignored `.env`, or as Render environment variables;
`render.yaml` has an empty slot for each so Render asks for the values - never commit them). The app uses the daily token while it is valid. When it is expired, missing or rejected
(HTTP 401 "invalid token") it switches to the Analytics token automatically, for that call and the following ones, and goes back to the daily token as soon as a fresh one is saved.
Pasting a token in the Access panel works for both: an Analytics token (its payload says `isExtended`) is saved as the fallback and leaves the daily token alone. Checked against the real
API with the Analytics token: quotes, 1m / 5m / daily candles (live and historical), option expiries and the option chain with OI, volume and greeks all work. It cannot read the user
profile (Upstox refuses that for it), which the app does not need. `/api/token` shows both tokens' status.

### Token priority (fixed): the token you paste always wins
Why Render could show "token expired" while Upstox still accepted the token: (1) Render's free plan wipes saved files whenever the service sleeps, so a token pasted in the Access
panel was forgotten and the app fell back to an old value in the environment variable; (2) the old rule "the token that expires later wins" let a long-lived token in the environment
beat the daily token you had just pasted. Now `desk_env` sorts every token by kind and ranks them each time they are read: a VALID token first, a normal daily token over the
long-lived Analytics token, then the most recently issued (`iat`). An Analytics token found in either slot becomes the fallback (never the first priority). When the daily token
is missing, expired or rejected the app uses the Analytics token automatically, and it looks at the daily token again every 5 minutes so a one-off glitch does not leave it on the
fallback. The Access panel shows "Feed is using", the daily token and the Analytics token separately. The tokens' own expiry (`exp`) is Upstox's expiry (~03:30 IST), so "expired"
in the panel means the app's copy of the token is expired or was lost, not that Upstox changed anything. On Render the app pings its own public URL every 10 minutes during market hours (`KEEPALIVE_MODE=market`, the default: weekdays 08:45-15:45 IST; `always` = 24/7, ~744 of Render's 750 free
hours per service per month; `off`) so the free service does not sleep and forget a pasted token mid-session.

### Why Render showed 0 calls (fixed)
The Docker image copied only `*.py`, `public/` and three named files, so `strict_model.json` and `conf_model.json` were NOT in the image: with no model the engine cannot score any signal and
issued no sure and no confidence calls. The Dockerfile now copies both, and the page shows "STRATEGY MODEL FILE MISSING" (and the log prints "Strategy models: MISSING") if one is ever
absent. The first start on the free 0.1-CPU plan was also very slow (about 22 minutes), which looked like "nothing shows" and failed health checks: signal detection is now 4-5x cheaper
(same calls, scores within 0.001), lite mode loads 54 days of 1-minute and 60 days of 5-minute history, computes the last 25 days of calls, and refreshes the call pages before the
dashboard. Measured in a 0.1-CPU / 512 MB container with only the Analytics token: ready in about 2 minutes with the same calls as the full-CPU build.
