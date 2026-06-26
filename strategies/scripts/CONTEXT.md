# EMA-crossover + market-data capture — working context

Handoff doc so a new session can resume without re-deriving. Point me here.

## TL;DR
Two things run on the deployed server: (1) the **EMA-crossover BANKNIFTY strategy** (live,
via OpenAlgo Strategy Host) and (2) **market-data capture** for backtesting — OpenAlgo's
recorder (BANKNIFTY future, quote+volume) plus **mkdskite**, a standalone Go daemon
capturing NIFTY 50 quotes. We're evaluating faster EMA configs (8/17 @ 2m/3m) against the
live 9/21 @ 5m via a tick-replay backtester, accumulating daily captures.

## Server / deploy model
- `ssh root@offramp.oftenuncertain.net`, OpenAlgo at `/root/data/openalgo` (local branch
  `main`, **no git remote**, uncommitted local edits → deploys are **manual** scp, not pull).
- Strategies run via the **Python Strategy Host**: it executes a **timestamped runtime copy**
  (e.g. `strategies/scripts/ema_crossover_banknifty_20260507020538.py`), NOT the repo file —
  editing the repo file alone changes nothing; update the runtime copy + restart via the host.
  App-code changes (e.g. `utils/auth_utils.py`) need scp + `systemctl restart openalgo.service`.
- **Daily broker re-auth** each morning (Zerodha token expires ~3 AM IST); without it the feed
  is dead. Order each morning: **re-auth first, then restart app** (the WS adapter gives up on a
  stale token and won't auto-reconnect).
- Backups left as `*.pre_*.bak` next to changed files.

## Live EMA strategy (`ema_crossover_banknifty.py`, branch `mock/strategies`)
- Default config **9/21 @ 5m**. Entry = **advanced §14 gate**: EMA cross (on the *completed*
  candle, `iloc[-2]`) + decisive gap `|EMA9-EMA21| ≥ REVERSE_CONFIRM_PCT×close` (0.0003 ≈ **17pts**)
  + close-vs-EMA9 + EMA9 slope + volume `> 1.5×SMA(20)`.
- Exits (first-to-fire, tick-driven): **APPE** (arm `ARM_PER_LOT=4000`/lot → ₹8000 @60qty;
  budget `G=30·√peak·√(units/2)`; hard ×2; trend 180s / confirm 30s) · **trailing-SL 0.5%**
  (`TIGHT_TSL_ENABLED=false` → no 0.25% tighten) · **reverse signal** · **EOD 15:14**. Daily-loss
  cap ₹5000. `FEED_MODE=quote` (so the WS bus carries volume for capture).
- All tunable via env (`REVERSE_CONFIRM_PCT`, `ARM_PER_LOT`, `VOLUME_*`, etc.).

## Capture pipelines
- **OpenAlgo recorder** (`websocket_proxy/market_data_recorder.py`): records whatever's on the
  ZMQ bus. `MARKET_DATA_CAPTURE_ENABLED=true`, flush disabled (`FLUSH_EVERY=1000000000`).
  BANKNIFTY future quotes → `/root/data/openalgo/log/market_data_capture/<date>/normalized_market_data.jsonl`.
  Volume present from **2026-06-15** onward (quote mode); 06-09..06-12 are LTP-only.
- **Token hook** (`utils/auth_utils.py::_export_capture_token`, commit 56e97057): on each broker
  login writes `CAPTURE_TOKEN_FILE` (= `/root/data/kite_api_token.txt`, 0600) with
  `{broker,api_key,access_token,date}`. NOTE: access_token is a **composite `api_key:access_token`**
  (OpenAlgo's adapter splits on `:`); readers must take the part after the colon.
- **mkdskite** (`~/ptwiddler/mkdskite.git`, separate Go repo, **no remote yet**): standalone
  daemon, OWN Zerodha WS (zero trading-latency impact). Two instances run via separate systemd timers
  (Mon–Fri 09:00 IST, self-exit 15:30). Reuses token file, resolves symbols via Zerodha instruments dump
  (NO OpenAlgo SymToken dep), hot-swaps on mid-day token rotation.
  - **equity profile** → NIFTY 50 cash stocks, NSE, quote mode → `/root/data/mkdskite/data/<date>/`
  - **fno profile** (new) → nearest-expiry NIFTY/MIDCPNIFTY/BANKNIFTY FUT, NFO, quote mode →
    `/root/data/mkdskite-fno/data/<date>/`. Symbol recorded as base name (e.g. `BANKNIFTY`) not
    tradingsymbol → series is stable across monthly rolls; use `--symbol BANKNIFTY` in backtest.

## Backtesting (`strategies/scripts/`)
- **`backtest_ticks.py`** — replays a day's capture tick-by-tick; resamples to any timeframe;
  exits replayed tick-by-tick, **parity-verified vs live** (`test/test_appe_tsl_parity.py`, 56/56).
  Flags: `--tf --fast --slow --warmup --vol-sma --vol-mult --reverse-confirm-pct
  --gap-gate <pts> --early-entry --er-gate <thr> --er-window-min <min>
  --data-dir <path> --symbol <name>`. `--data-dir` points to an alternate capture dir (e.g.
  FNO captures); `--symbol BANKNIFTY` filters ticks by symbol name — required for multi-symbol
  FNO capture files. Runs PRICE-ONLY vs VOL-FILTER side by side.
  On quote days shows `[real vol]`; LTP-only days fall back to a tick-count proxy.
- **`bt_daily.sh [YYYY-MM-DD]`** — pulls a day's BANKNIFTY capture from the server + runs the
  standard battery: LIVE 9/21-5m · 8/17 @ 2m & 3m · 8/17 @ 3m close-vs-early at `GAP` pts (default 3)
  · `SWEEP=1` adds a gap sweep. Local data dir: `/home/dksha/ptwiddler/backtestdata/<date>/`.

## Findings so far (all SINGLE chop days — directional, not significant)
- The live ~17pt gap gate makes **8/17 on 2m/3m inert** (2-min crossovers can't reach 17pt gap);
  they only trade with the gap relaxed to **≲6pts** (recalibration needed for faster TFs).
- On the one chop day measured (2026-06-16): live 9/21-5m took **0 trades** (89 crossovers rejected,
  mostly thin-gap) → stayed flat, the gate working as intended (vs 06-15 pre-gate which churned 29
  whipsaw trades to the −7,608 daily-loss cap).
- **3m > 2m** that day; the **volume filter helped on 3m** (+4,128 vs −2,232 price-only) — first time
  it added value. **early-entry didn't help** (entered 1-3 min sooner but worse fills).
- The volume filter is `vol > 1.5×SMA(N)`: live uses **SMA(20)** (5m); 2m/3m backtests use **SMA(10)**.

## Regime-follower breakthrough (2026-06-18, 8 days 06-09..06-18; 2 trend days only — tiny sample)
The exit-mechanics experiments (fixed bracket TP/SL, tight-stop "approach A") all LOST on chop
because the **entry** churns; no exit rule saves a bad entry. Stepping back to a **regime filter**
produced the first clean result. Tooling: `regime_scan.py` (Efficiency Ratio over rolling windows
+ ADX + crossover count), `fast_scan.py` (runs / range-expansion / EMA-extension).

- **You cannot detect a trend day in the morning.** 06-12 (the +57k baseline day) trended only
  from ~13:06; its morning was real chop, indistinguishable from 06-16/17. So don't predict the
  day — **confirm a move once it starts**. Every reliable trend/chop discriminator is *lagging*
  (you can't measure "sustained" without elapsed time); fast price-only signals (consecutive bars,
  single-bar range expansion) fire on every day and don't separate. See [[banknifty-regime-detection]].
- **Efficiency Ratio (ER) = |net move| / |total path|** over a trailing window is the cleanest gauge.
  **60-min window** separates trend from chop cleanly (06-12→0.80, 06-09→0.69 vs all 6 chop days
  ≤0.51); 45-min is too noisy (chop leaks in), 90-min lags more. User accepts ~45-min confirmation.
- **Crossover ≠ high-ER.** Crosses fire at regime *transitions* (ER still low); ER confirms ~45 min
  later when there's no fresh cross. So "enter on cross AND ER high" → **zero trades**. Fix: make ER
  a **TRIGGER, not a filter** — when flat and ER≥gate, enter in the CURRENT EMA-alignment direction
  (no cross needed). This is the lagged confirmation entry the 45-min latency buys.
- **WINNING CONFIG = regime-trigger + RIDE (let winners run via APPE/TSL; NO tight stop).** A tight
  stop *hurts* here (chops you out on normal trend pullbacks — 06-12 +9,672 tight vs +16,464 ride).
  Tight stops were only a patch for an unfiltered entry; once the regime filter selects, let it ride.
- **The EMA pair stopped mattering** (5/13 ≡ 7/17 in regime-trigger mode) — entry is by alignment,
  not by cross. The strategy is no longer "EMA crossover" → it's an **ER-confirmed trend-regime
  follower**. Crossover only acts as the reverse/alignment-flip exit backstop.
- **ER threshold sweep (RIDE, 60-min):** clean band ~0.55–0.65, all **0 red days**. Lower = earlier
  entry = more capture: **0.55 → +47,208** over 8 days (3 trades, 0 losers; 06-12 captured +37,800,
  near the full +41,640), 0.60 → +25,872, 0.65 → +26,376; **0.50 leaks the first chop day** (06-11
  −5,040). vs raw baseline +32,316 but with 70 trades / 5 red days / −19k drawdowns. Working default
  **0.60** (capture both trend days, 0 red, buffer from the 0.50 leak); lean toward 0.55 as more
  trend days confirm the floor. **CAVEAT: 4 trades / 2 trend days — proof-of-concept, not an edge.**
- Backtester support: `backtest_ticks.py` flags `--er-gate <thr> --er-window-min <min>` (regime
  trigger), plus `--tp-per-lot/--sl-per-lot` (fixed bracket) and `--sl-per-lot` alone (stop-only).
- **Deployable script: `ema_regime_banknifty.py`** — the regime follower for forward-testing
  in **Analyzer mode**. Same plumbing as the crossover bot (feed/orders/state/EOD); entry is
  the ER trigger. Defaults 5/13 @ 3m, ER≥0.60/60min. **Exit: ER-exit 0.40 (primary) + TSL 0.5%
  (backstop). APPE disabled by default** (see ER-exit section below).

## ER-exit findings (2026-06-26, 15 days 06-09..06-25, 3m bars, --tf 3 --fast 5 --slow 13)

Exit-mode comparison. All 5 active trading days (0-trade chop days excluded):

| Mode | 15-day total | vs RIDE | Verdict |
|------|-------------|---------|---------|
| RIDE (APPE+TSL, current) | +29,653 | baseline | — |
| **ER-exit 0.40 only** | **+37,596** | **+7,943** | **deployed** |
| ER-exit + APPE | +21,516 | −8,137 | don't: APPE churns 06-12 |
| MFE-dynamic ER | +29,328 | −325 | don't: misfires on 06-23 |
| ER-exit 0.40 + no re-entry after ER_EXIT | ~+40,524 | +10,871 | overfitting (1 day) |

- **ER-exit (bar-close semantics)**: at each completed bar, compute rolling ER over the 60-min
  window. If ER < 0.40, close at market. No tick-level APPE — immune to intra-bar pullback noise.
- **Why APPE hurts here**: APPE fires tick-by-tick; on 06-12 (the +32k trend day) it churned 3
  trades (+16,464 vs +32,400 holding). ER-exit stays out until momentum genuinely collapses.
- **06-25 loss (−7,008)**: entry was at intraday top (BUY 58,670, peak 58,675, MFE=5pts). No
  bar-close exit can fix a near-zero MFE entry — the regime filter just occasionally gets it wrong.
- **No-re-entry rule**: saves the 2nd 06-25 SELL (−2,928) but is single-day overfitting. Bank Nifty
  intraday reversals on news are genuine opportunities; ER gate re-filters any re-entry anyway.
- CLI flags in `backtest_ticks.py`: `--er-exit`, `--er-appe`, `--er-tsl`, `--er-tsl-wide`,
  `--er-dynamic`, `--mfe-scale`, `--mfe-trail-frac`, `--mfe-trail-min`, `--er-exit-high`.

## Open items / next steps
0. **Forward-test `ema_regime_banknifty.py` in Analyzer mode** (paper) for several days, esp. trend
   days, before considering live. Confirm ER-exit 0.40 holds out-of-sample (current edge = 2 days).
1. Push `mock/strategies` to `origin` (github perpetualtwiddler/openalgo) — several commits ahead.
2. Set a remote for `mkdskite.git` and push.
3. **Accumulate multi-day FNO captures** (`/root/data/mkdskite-fno/data/<date>/`) starting 06-19;
   run `backtest_ticks.py --data-dir ... --symbol BANKNIFTY` (or NIFTY/MIDCPNIFTY) to evaluate configs.
4. **Evaluate NIFTY and MIDCPNIFTY** with the same regime-follower approach once captures accumulate.
5. Later: add the scoped **F&O profile** to mkdskite for options OI (full mode).
