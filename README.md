# Market Signal Desk

Local real-data dashboard for **Sensex, Nifty 50, Bank Nifty, Fin Nifty, Midcap Select and 11 Nifty sector
indices**. Click a segment to get its final signal from the current level — direction, entry, stop-loss,
T1/T2/T3, conviction, the full strategy board (every vote with its raw inputs), real option/OI analytics
(PCR, max pain, OI flow, put/call walls, ATM IV, straddle, Greeks) and a walk-forward hit-rate measured
on that instrument's own candles.

## Run

```bash
cd market-dashboard
python server.py            # Python 3.10+, stdlib only - no pip install
```

Open **http://localhost:8787**. The page polls every 5 s while the "Live · 5s" switch is on.

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
copy the `code=` value from the redirected address, paste it, then restart `server.py`. The new token
is written into `.env`.

## Move forecast — only fires when the minimum move is realistic

Each signal gets a real expected-move forecast (3 hours / 36 × 5m candles for the Nifty and Sensex
precision profile, ~60 minutes / 12 × 5m elsewhere, or next session on the Yahoo daily fallback):

- three independent estimators: ATR(14) × √horizon, realized candle σ × spot × √horizon, and the
  ATM straddle scaled from expiry to the horizon — the displayed estimate is their median, and the
  full estimator range is shown for audit
- a measured capture probability from this instrument's own history: of past same-direction setups,
  how often the minimum move was covered within the horizon
- a pre-move timing check: `EARLY` (range compressed at the 20-bar breakout edge, fresh
  Supertrend/MACD flip), `BUILDING`, `IN PROGRESS` (already broke out), `EXTENDED` (move mostly done)
- a trade plan with **three targets**: T1 = the minimum required move, T2 = the expected move clamped
  to the target zone, T3 = T2 + one more T2−T1 step (the stretch target). The precision profile sets
  the stop at 3.4 × the minimum move; the standard profile widens the structural stop to 0.5 × ATR
  when it sits inside market noise.

A call is shown as `OPPORTUNITY · PRE-MOVE` only when **all** of the active profile's gates pass:
non-neutral direction, expected move ≥ minimum, conviction ≥ 55 with a 100% technical score
(EMA 9/21 + MACD + Supertrend + an extreme RSI all agreeing), an ATR-expected move ≥ 4 × the target,
entry before 14:30 IST, timing still EARLY or BUILDING, and the live six-month audit still measuring
≥ 75% T1-before-SL for Nifty/Sensex. Otherwise the card shows the exact blocking reason
(`BELOW MIN MOVE`, `ATR EXPECTED MOVE … REQUIRED`, `ENTRY WINDOW CLOSED`, `LOW CONVICTION`,
`LOW SETUP QUALITY`, `INDICATORS SPLIT`, `PROFILE EDGE WEAK`, `LOW CAPTURE ODDS`,
`MOVE ALREADY STARTED`, `RISK > REWARD`, `NO BIAS`) and the advice says no trade — it never forces
a prediction.

Per-instrument minimum / target-zone points: Sensex 40–80, Nifty 25–50, Bank Nifty 50–100,
Fin Nifty 30–60, Midcap Select 20–40, sectors 10–60 depending on index level.

The research scanner re-runs this forecast on every selected segment, ranks qualifying pre-move calls
first, and reports `N of M segments qualifying`. If nothing qualifies it says so and lists the nearest
candidate — no fabricated call.

## 75% precision profile — measured, and gated at 75%

The old 60-minute / 0.5 × ATR plan hit T1 before SL only ~41% on Nifty because the stop was much
tighter than the fixed 25-pt target. The precision profile (`analysis.py` + `server.py`) fixes that
by changing the trade geometry and the entry quality together:

| Rule | Value |
|---|---|
| Horizon | 36 × 5m (3 h) |
| Stop | 3.4 × the minimum-move target (Nifty 85 pts, Sensex 136 pts) |
| Entry filter | EMA 9/21, MACD, Supertrend and an extreme RSI must all agree (technical score 100%) |
| Expected move | ATR(14) × √36 × 0.8 ≥ 4 × the minimum target |
| Timing | EARLY or BUILDING only, entry before 14:30 IST |
| Conviction | aggregate signal ≥ 55 |
| Sequencing | one trade at a time, 3-bar (15 min) cooldown |
| Live gate | refuses new calls if the six-month audit drops below 75% |

Six months of that instrument's own 5m bars (mid-May → Nov 2026, every bar the feed returned):

| Instrument | Setups | T1-before-SL win | Worst month | Reach | Avg risk | R:R at T1 | Expectancy |
|---|---|---|---|---|---|---|---|
| Nifty 50 | 124 | **77.4%** | 62.5% | 79.0% | 85 pts | 0.29 | +5.3 pts |
| BSE Sensex | 186 | **76.9%** | 59.1% | 89.8% | 136 pts | 0.29 | −0.7 pts |
| Bank Nifty (same rule, not tuned) | 179 | 75.4% | 63.3% | 83.2% | 170 pts | 0.29 | −1.2 pts |
| Fin Nifty (same rule, not tuned) | 153 | 72.5% | 52.4% | 77.1% | 102 pts | 0.29 | −1.6 pts |

The previous 69% profile is kept as `BALANCED` and audited side by side in the Proof panel
(Nifty 238 setups / 69.3%, Sensex 238 / 66.8%, R:R 0.45).

**Read this honestly.** 75%+ is real here, but it is bought with a very wide stop: R:R is ≈0.29 and
the break-even hit rate is ≈77%, so expectancy is roughly flat before costs (Nifty slightly positive,
Sensex slightly negative). One bad month measures ~59–62%, so the 75% floor is a long-run audit gate,
not a per-week promise. 80–85% needs a stop of ~3.8 × the target (~3.5 × ATR) and clearly negative
expectancy, so it is deliberately not offered as an accuracy claim. `research_opt.py` reproduces the
whole search offline, including the outcome chronology (entry bar excluded from the outcome window).

## Proof and independent verification

Two ways to check that the predictions are computed from real market data:

1. **Proof button** in the header — provenance (quote/candle/option source, bars, last bar, last close),
   the live forecast profile, and a no-lookahead walk-forward audit of that exact profile on every 5m
   bar the feed returns (180 days for Nifty/Sensex, 30 days for the rest): setups, wins/losses,
   T1-before-SL win rate, worst month, minimum move reached, average bars to T1, average risk,
   R:R at T1, expectancy, plus the last six audited setups.
2. **`python verify.py`** — a separate CLI that pulls Upstox quotes/history and Yahoo 5m bars *again*,
   diffs every overlapping bar against the dashboard, pulls its own six-month history, recomputes both
   audits and exits non-zero on any mismatch. It never trusts the dashboard's own numbers.

```bash
python server.py     # terminal 1
python verify.py     # terminal 2 — prints PASS/FAIL per check
```

The audit is honest by construction: at bar *i* it only sees bars ≤ *i*; the outcome uses the next
horizon; one trade at a time with the same cooldown the live profile uses; a candle touching both T1
and SL is scored as a stop; "reach" (minimum move touched at any point) is reported separately from
the tradable win rate. Historical option chains do not exist in the feed, so OI/Greeks affect the
live forecast only and are never claimed as backtested. Expectancy is shown next to the win rate so
the wide-stop trade-off is visible rather than hidden.

## Research scanner

The **Research** button in the header asks which segments to scan, then runs a forced refresh and ranks
them:

- breadth (bullish / bearish / neutral), scan verdict and highest-conviction pick
- per-segment direction, confidence, entry, stop, T1/T2/T3, PCR, max pain, OI flow, IV, timeframe, drivers
- every row can be opened directly on the dashboard ("Open")

`/api/research?segments=SENSEX,NIFTY` returns the same report as JSON.

## API

- `GET /api/market?force=1` — full payload: feed status, per-instrument candles, options, signal, forecast
- `GET /api/research?segments=ID1,ID2` — scan report (omit `segments` for all 16)
- `GET /api/proof` — provenance + walk-forward audit behind the Proof panel

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

The 75%+ figure is a **wide-stop** base rate measured over six months, not a free lunch: R:R at T1
is ≈0.29 and the break-even hit rate is ≈77%, so expectancy is roughly flat before costs. A single
month can measure 59–62%. The desk's edge is that it refuses to call when the audit or the indicators
do not clear the gate; it never forces a prediction. Treat the output as structured research plus risk
discipline. Derivatives carry substantial risk.

## Files

- `server.py` — HTTP server, Upstox orchestration, caching, market payload + research report + proof
- `verify.py` — independent provenance/accuracy checker (separate Upstox + Yahoo pulls)
- `upstox.py` — Upstox v2/v3 client (quotes, candles, option contracts, chain → OI/Greeks)
- `upstox_login.py` — OAuth code exchange, rewrites the token in `.env`
- `analysis.py` — indicators, option analytics, composite signal, move forecast, walk-forward audits
- `research_opt.py` — offline six-month profile search (stop width × horizon × filters, monthly folds)
- `data_sources.py` — real Yahoo chart fallback
- `public/` — dashboard (vanilla JS, canvas charts, no CDN)

## Later: packaging as an APK

The same dashboard can be wrapped into an Android APK with Capacitor (WebView + packaged local server).
Do that after the feeds and token-refresh flow are settled.
