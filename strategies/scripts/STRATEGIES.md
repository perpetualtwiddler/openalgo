# Trading Strategies — OpenAlgo Paper Trading

**Platform:** OpenAlgo on algo.oftenuncertain.net (109.123.248.99)
**Mode:** Analyzer (sandbox) with 5,00,000 INR virtual capital
**Paper trading period:** May 2026
**Broker:** Zerodha (daily auth required before 9:15 AM IST)

---

## Open TODOs / Backlog

Tracked here so nothing slips (most recent context first).

| # | Item | Priority | Notes |
|---|------|----------|-------|
| 1 | **Month-end report → Aug-1 straddle go-live decision** | HIGH (~Jul 31) | Decide straddle-only LIVE (real money, 390 qty, ~₹2L) on data: win rate, worst-day, **slippage estimate**, margin fit, + re-run the hardening scan on the full month. |
| 2 | **Live-slippage measurement** | HIGH (go-live prep) | On the first live days, log fill-price vs LTP per fill → true live edge (paper P&L is an optimistic ceiling; matters most for the thin-edge straddle). |
| 3 | **Opt1: log history-fetch failures** | LOW | Zerodha `/history` "Server disconnected" flakiness is silently skipped; add a logged warning (+ optional retry). |
| 4 | **Go-strategies port decision** (openalgo-go vs manja vs keep-Python) | LOW | Draft in "Go-Based Strategies (PROPOSED)" below; no decision needed yet. |

### Recently completed

- **Pre-auth WS mitigation — RESOLVED via option (a) "log in before 09:05" (validated 2026-07-28).** The 09:05 restart previously booted *before* the ~09:15 login, so the WS broker adapter came up unauthenticated (flapping feed; on 07-27 it even killed the straddle's 09:35 entry with `Incorrect api_key`). Retiming the restart was rejected — 09:16 would land at/after the 09:15 strategy start and could interrupt a live entry. With an early login: **0 auth errors, clean feed 09:15→09:52**. Residual mid-session stalls are the separate upstream Zerodha data-API flakiness (now alerted, see below).
- **Full-history backtest CSV rebuild (2026-07-27)** — and it caught the straddle backtest engine running **stale vs live** (195 qty / no breach exit); fixed to 390 + 0.55% and cross-validated against `straddle_harden_scan.py` (identical 34 trades, BREACH:14/EOD:20). STRADDLE net 19,590 → 60,746. Produced the like-for-like table and the backtest-vs-live reconciliation.
- **Feed-stale TradeBhau alert extended to Opt1 + Regime (2026-07-30)** — the 07-24 alert work had been **straddle-only** (REST option quotes) while the EMA strategies' WS-feed guard was log-only, so the 07-28 09:52 WS stall was silent. Both now push a TradeBhau alert (onset → throttled re-alert → recovery) naming the strategy and whether a position is open/unprotected.
- **Consolidated *Utilised margin* in the straddle's per-trade notification (2026-07-30)** — the consolidated block that follows the 4 leg alerts now shows utilised margin (+ return on margin) for the whole structure, using the same defined-risk basis as the EOD digest (verified identical: ~₹71,838 on 07-24).

---

## 1. NIFTY Iron Butterfly (Short Straddle + OTM Hedge)

**File:** `short_straddle_nifty.py` (server: `short_straddle_nifty_20260507020539.py`)

### Structure
- **SELL** ATM CE + ATM PE (collect premium)
- **BUY** OTM8 CE + OTM8 PE (~400 points out, cap max loss)
- Product: MIS (intraday), auto square-off before deadline

### Position Sizing
| Parameter | Value |
|-----------|-------|
| Lot size | 65 (verified vs broker master contract 2026-07-02) |
| Lots | 6 (doubled from 3 on 2026-07-02) |
| Quantity | 390 per leg |
| Estimated margin | ~80,000-110,000 INR (2x, hedged) |

### Entry Rules
Entry at **9:35 AM IST** (delayed from 9:20 to let opening noise settle). All checks must pass in order:

| # | Filter | Default | Config Env Var |
|---|--------|---------|----------------|
| 1 | Consecutive SL cooldown (2 days) | ON | `CONSECUTIVE_SL_LIMIT=2` |
| 2 | Expiry day skip (gamma risk) | ON | `SKIP_EXPIRY_DAY=true` |
| 3 | Event calendar (RBI, FOMC, CPI) | ON | `SKIP_EVENT_DAYS=true` |
| 4 | Gap open > 1% | ON | `GAP_THRESHOLD_PCT=1.0` |
| 5 | India VIX > 25 | ON | `VIX_THRESHOLD=25.0` |
| 6 | ORB trend breakout > 0.5% | ON | `ORB_BREAKOUT_PCT=0.5` |

### Exit Rules
| Trigger | Threshold | Action |
|---------|-----------|--------|
| Profit target | +25% of net premium | Close all 4 legs |
| Short-strike breach | NIFTY moves ≥0.55% from entry ATM (directional-move cut) | Close all 4 legs |
| Stop-loss | -50% of net premium | Close all 4 legs |
| EOD square-off | 15:14 IST | Close all 4 legs (before analyzer 15:15 cutoff) |
| Position sync | Every 5 seconds | Detect manual/system exits |

At entry the log prints a **breach map** (PE-wing / breach-lo / ATM=max-profit / breach-hi / CE-wing) and every P&L heartbeat shows live `NIFTY <spot> → breach <lo>/<hi>` alongside the `[short/hedge]` split. Added 2026-07-01 (breach) / 2026-07-02 (map + display + ATM-centering fix).

### Safety Features
- **Iron butterfly hedge:** OTM8 wings (~400pts) cap maximum loss; wider wings retain more theta profit on calm days
- **Event calendar:** `event_calendar.json` with 16 confirmed high-volatility dates (Jun–Dec 2026) covering RBI MPC, FOMC, US CPI
- **Consecutive SL cooldown:** Pauses after 2 straight stop-loss days
- **Trade history:** Last 30 trades recorded in `_history.json` for cooldown logic
- **State persistence:** JSON state file survives strategy restarts (same-day only)
- **Stale-feed guard + alert (2026-07-24):** option LTPs fetched in ONE batched `multiquotes` call with a short retry (fewer broker hits + transient-timeout resilience near the close). If quotes still stop succeeding for `FEED_STALE_SEC`, the position is effectively unprotected (PT/SL/breach on stale prices) — the guard fires a **TradeBhau alert** (`/telegram/notify`, on onset + throttled re-alerts + recovery) so you can manually **Close All**. Root cause is upstream (Zerodha data-API timeouts), so this hardens *awareness + resilience*, not prevention; the wings still cap max loss and the EOD square-off is a feed-independent clock.

### Design decisions — tested & rejected
**ER-based trend-exit — REJECTED 2026-07-03 (backtested, underperforms).**

*Idea (Mandar + Dinesh):* the straddle (short-vol) is hurt when a trend starts, while the EMA strategies profit from trends — opposite natures. Since they trade different underlyings (straddle = NIFTY, EMA = BANKNIFTY) we can't cross-trigger, so give the straddle its **own** trend detector: Kaufman **ER on NIFTY** (same metric Regime uses). A trend *starting* = **ER ≥ ~0.6** (the level at which Regime *enters*). On that signal, arm a tight profit-lock (APPE-like) — or hard-exit — to bank the good profit before the trend erodes it. *(Direction matters: HIGH ER = trend; ER < 0.4 = chop = the straddle's best zone, so exiting on low ER is backwards.)*

*Result — backtested 31 days (QTY 390, net of charges). Every ER variant lost to the breach-only baseline:*

| Config | Net | vs baseline |
|--------|----:|------------:|
| **baseline — PT25/SL50 + 0.55% breach only** | **+52,649** | — |
| ER-lock (3m ×20, trig 0.6, give-back 0.20) | +30,380 | −22,269 |
| ER-lock (3m ×10, trig 0.6, give-back 0.20) | +14,975 | −37,674 |
| ER-hard-exit (3m ×20, trig 0.6) | +31,862 | −20,787 |
| WRONG-DIR: exit at ER<0.4 | −7,138 | −59,787 |

*Why it fails:* ER≥0.6 fires far too often intraday — NIFTY nearly always has a 30–60 min efficient stretch (the 30-min ER tripped on 20 of 21 trade days). Each fire is an early exit that forfeits the slow theta decay the straddle earns into EOD. No ER setting *beats* the breach; the best it can do is approach baseline by firing rarely. The **0.55% breach is the better trend-guard** — it fires only on a real, sustained ±0.55% move, not on every efficient wiggle. (The WRONG-DIR run — exit at ER<0.4 — was worst of all, confirming that exiting on chop is exactly backwards.) **Conclusion: keep PT25/SL50 + 0.55% breach; do not add ER-protection.** Scan: `straddle_er_scan.py`.

**VIX floor, time-based profit bank, tighter entry gates — REJECTED 2026-07-16 (backtested, none beat baseline).**

*Idea (Mandar, 07-16):* harden the straddle further — raise win rate / cut loss / protect profit (APPE-analog). The gap (1.0%), opening-range-trend (ORB 0.5%) and VIX-ceiling (25) gates are **already live**, so tested only the untested levers: a **VIX floor** (skip ultra-low-VIX/thin-premium days), a **time-based profit bank** (exit if up ≥ X% after early afternoon), plus a light sweep tightening the existing gates.

*Result — backtested 40 days (QTY 390, net of charges). Every variant is a no-op or a loss vs the current baseline (+48,087, 17W/11L, worst −17,522):*

| Config | Net | vs baseline |
|--------|----:|------------:|
| **baseline (current live)** | **+48,087** | — |
| VIX floor 12 / 13 | +38,746 / +17,547 | −9,341 / −30,540 |
| bank 15% @13:00 | +43,115 | −4,972 |
| bank 20% @13:00 | +47,151 | −936 |
| tighter gap ≥0.75 | +33,626 | −14,461 |

*Why it fails:* low-VIX days are still net-**positive** (theta works even when premium is thin) — skipping them just deletes winners. The time-bank caps upside on days that would have run to PT/EOD, and no lever moves the worst day (−17,522, already a breach-cut day). Tightening the gap gate removes profitable days too. **Conclusion: the current config is already well-tuned; do not add a VIX floor, time-bank, or tighter gates.** Scan: `straddle_harden_scan.py`. *(Preliminary — 40 days, one vol regime; re-confirm at month-end.)*

### Key Files (Server)
- Strategy: `/root/data/openalgo/strategies/scripts/short_straddle_nifty_20260507020539.py`
- Event calendar: `/root/data/openalgo/strategies/scripts/event_calendar.json`
- State: `/root/data/openalgo/strategies/state/SHORT_STRADDLE_NIFTY_state.json`
- Trade history: `/root/data/openalgo/strategies/state/SHORT_STRADDLE_NIFTY_history.json`
- Logs: `/root/data/openalgo/log/strategies/short_straddle_nifty_*.log`

---

## 2. BANKNIFTY EMA(9/21) Crossover

**File:** `ema_crossover_banknifty.py` (server: `ema_crossover_banknifty_20260507020538.py`)

### Structure
- Trades **BANKNIFTY futures** (BANKNIFTY26MAY26FUT)
- Enters on EMA(9) crossing EMA(21) on **5-minute candles**
- Trades crossover **events** only (moment of crossing), not position (above/below)

### Position Sizing
| Parameter | Value |
|-----------|-------|
| Lot size | 30 |
| Quantity | 60 (2 lots) |
| Estimated margin | ~1.5–3.0 lakh INR |
| Product | MIS (intraday) |

### Entry Rules
- EMA(9) crosses above EMA(21) → **BUY**
- EMA(9) crosses below EMA(21) → **SELL**
- Volume filter: current candle volume > 1.2x SMA(20) volume
- Trades only during market hours

### Exit Rules
| Trigger | Action |
|---------|--------|
| Opposite crossover | Reverse position |
| Trailing stop-loss | Monitors via WebSocket LTP feed |
| EOD square-off | Close at 15:14 IST (before analyzer 15:15 cutoff) |
| Position sync | Detect manual exits from web UI |

### Safety Features
- **WebSocket auto-reconnect:** Outer while loop with 5-second retry on disconnect
- **State persistence:** JSON state file with position, entry price, trailing SL, peak price
- **Stale state detection:** Ignores state from previous day or different symbol
- **Volume filter:** Avoids false crossovers in low-volume periods

### Key Files (Server)
- Strategy: `/root/data/openalgo/strategies/scripts/ema_crossover_banknifty_20260507020538.py`
- State: `/root/data/openalgo/strategies/state/EMA_9_21_BANKNIFTY_state.json`
- Logs: `/root/data/openalgo/log/strategies/ema_crossover_banknifty_*.log`

### EMA Variations Under Comparison (5 options)

Section 2 above describes the **original** 5m strategy. Live-deployment history: original 5m (Opt 5)
→ **3m Option 1** on 2026-06-17 → **3m Option 4 (CURRENT ACTIVE) on 2026-06-18**
(`ema_crossover_banknifty_opt4_20260618.py`). Options 1/2/3/5 are descheduled/backtest references
(kept for rollback); they're compared on captured tick data via `backtest_ema_dev.py`. The table
lists only the differences — all 5 share the same rule framework below.

**Shared across all 5** (constant): entry = EMA cross + decisive **gap** + **close** on the signal
side of the fast EMA + **fast-EMA slope** in-direction + **volume > 1.5×SMA**; the same gate drives
reverse-exits; **APPE** profit-protect with size-aware give-back `G = 30·√peak·√(units/2)`; EOD
square-off 15:14; **daily breaker ₹5,000**; **QTY 60** (2 lots × 30); `FEED_MODE=quote`.

| # | Status | Entry TF | Trend filter | EMA | Gap gate | EMA-slope check | Volume SMA | Trailing SL | APPE arm |
|---|--------|----------|--------------|-----|----------|-----------------|------------|-------------|----------|
| **1** | was live 06-17→06-18, now descheduled | 3m | — | 9/21 | 0.01% (~6pt) | EMA9 now > EMA9 3 bars ago | SMA(10) ≈30min | **1.5×ATR(14)** | ₹4,000 (₹2k/lot) |
| 2 | backtest candidate | 2m | EMA9>EMA21 on 5m | 9/21 | 0.01% | EMA9 now > EMA9 3 bars ago | SMA(15) ≈30min | 1.5×ATR(14) | ₹4,000 (₹2k/lot) |
| 3 | backtest candidate | 2m | — | 9/21 | 0.01% | EMA9 now > EMA9 3 bars ago | SMA(15) ≈30min | 1.5×ATR(14) | ₹2,000 (₹1k/lot) |
| **4** | **🟢 LIVE — CURRENT ACTIVE (2026-06-18)** | 3m | — | **7/15** | 0.01% | EMA7 now > EMA7 3 bars ago | SMA(10) ≈30min | **1.5×ATR(14)** (pure, no floor) | ₹4,000 (₹2k/lot) |
| 5 | original (descheduled 2026-06-17) | 5m | — | 9/21 | 0.03% (~17pt) | EMA9 now > EMA9 1 bar ago | SMA(20) ≈100min | static 0.5% | ₹8,000 (₹4k/lot) |
| **R** | committed, **not yet deployed** — see §3 | 3m | **ER≥0.60/60min** *(entry trigger, replaces crossover)* | **5/13** (alignment dir only; no cross wait) | — | — | — | **ER-exit <0.40** (bar-close primary) + 0.5% backstop | **off** |

- **ATR trailing stop** (Options 1–4) = Wilder ATR(14) on the entry timeframe, distance =
  `ATR_MULT×ATR` (ratchet-only), **pure (no floor)** for all. A 100-pt floor (`ATR_FLOOR_PTS`) was
  trialled on Option 4 then **removed (2026-06-18)** — a floor sweep on the captured chop days showed
  it strictly worse (a wider stop just enlarges whipsaw losses when there's no trend to ride). The
  `ATR_FLOOR_PTS` knob is retained (default 0/off). See `ADAPTIVE_PROFIT_EXIT_DESIGN.md` §17.
- **Crossover confirmation window** — Options 1/2/3/5 enter only on the *cross candle* (decisive gap
  required at the cross). **Option 4** adds a **2-candle window** (`cross_confirm_bars=2`): a cross
  stays eligible for up to 2 candles after it, so a thin cross that turns decisive a candle or two
  later still trades — at the cost of re-admitting whipsaw risk (on the choppy 06-18 it took 2 such
  whipsaw losers while the others stayed flat). See `ADAPTIVE_PROFIT_EXIT_DESIGN.md` §18.
- **Run the comparison:** `BACKTEST_DATA_DIR=/root/data/openalgo/log/market_data_capture \`
  `.venv/bin/python strategies/scripts/backtest_ema_dev.py <YYYY-MM-DD>` → one table, all 5 rows
  (baseline immune to env overrides). ⚠ Single-day, cold-start EMAs → directional only; needs a
  trending captured day for a real verdict (the quiet 06-16/06-17 days gave ~0 trades for all).

---

## 3. BANKNIFTY EMA Trend-Regime Follower

**File:** `ema_regime_banknifty.py` (committed to `mock/strategies`; **not yet deployed to server**)
**Status:** Forward-test in Analyzer mode before live.

### Why a different approach
The EMA crossover strategy enters on the *moment* of a cross — which fires at a regime *transition* when
momentum is weakest. The regime follower waits for the regime to *confirm* itself (~45–60 min) and only
then enters in the direction the EMAs already point. This forgoes the first leg but avoids chop entirely:
no entry gate can save a bad entry — if the regime isn't established, the position will churn regardless
of the exit rule.

### Position Sizing
Identical to the EMA crossover variants.
| Parameter | Value |
|-----------|-------|
| Lot size | 30 |
| Quantity | 60 (2 lots) |
| Product | MIS (intraday) |
| Feed mode | quote (for volume capture) |

### Entry Logic
- Wait until **flat** and the trailing 60-min Efficiency Ratio (ER = \|net move\| / \|total path\| over completed 3m bars) reaches **ER ≥ 0.60**.
- On trigger: enter in the **current EMA(5/13) alignment direction** — BUY if EMA5 > EMA13, SELL otherwise. No crossover required; a cross fires before ER confirms and would give zero trades.
- Entry is deliberately lagged (~45 min after a trend starts). The regime filter skips all chop days (ER stays below gate); on trend days the position rides the bulk of the move.

| Parameter | Value | Config env var |
|-----------|-------|----------------|
| Entry timeframe | 3m candles (built from WS feed) | `CANDLE_TIMEFRAME=3m` |
| Fast EMA | 5 | `FAST_EMA=5` |
| Slow EMA | 13 | `SLOW_EMA=13` |
| ER gate | 0.60 | `ER_GATE=0.60` |
| ER window | 60 min (20 bars @ 3m) | `ER_WINDOW_MIN=60` |
| Trade direction | both | `TRADE_DIRECTION=BOTH` |

### Exit Logic
| Priority | Trigger | Default | Config env var |
|----------|---------|---------|----------------|
| 1 (primary) | **ER-exit** — at each bar-close, if rolling ER < 0.40, momentum collapsed → close at market | 0.40 | `ER_EXIT=0.40` |
| 2 (backstop) | Trailing SL 0.5% from peak (tick-driven) | 0.5% | `TRAILING_SL_PCT=0.5` |
| 3 | EMA alignment flip (reverse signal confirmed by ER gate) | — | — |
| 4 | EOD square-off | 15:14 IST | hardcoded |

APPE is **off** by default — it exits tick-by-tick on intra-trend P&L pullbacks, churning trend days.

### Backtest Results (15 days, 2026-06-09..06-25, 3m bars, 5/13 EMA, ER gate 0.60)

5 active trading days; 10 zero-trade chop days (regime gate correctly kept out).

| Exit mode | 15-day P&L | vs RIDE | Notes |
|-----------|-----------|---------|-------|
| RIDE: APPE+TSL (current crossover default) | +29,653 | baseline | APPE churns 06-12 trend |
| **ER-exit 0.40 only** | **+37,596** | **+7,943** | deployed config |
| ER-exit + APPE | +21,516 | −8,137 | APPE hurts on trend days |
| MFE-dynamic ER | +29,328 | −325 | misfires on brief adverse moves |

Worst single day: 2026-06-25 (−7,008): entry at intraday top (BUY 58,670, MFE only +5 pts). No exit rule fixes a near-zero MFE entry — the regime filter occasionally misfires.

### Key Files
- Strategy: `strategies/scripts/ema_regime_banknifty.py`
- Backtest flags: `--er-gate 0.60 --er-window-min 60 --er-exit 0.40 --tf 3 --fast 5 --slow 13`
- Context: `strategies/scripts/CONTEXT.md` (ER-exit section)
- State (when deployed): `/root/data/openalgo/strategies/state/EMA_REGIME_BANKNIFTY_state.json`

---

## Go-Based Strategies (PROPOSED — under evaluation, NOT decided)

**Status (2026-07-06): OPEN.** Whether to port **EMA Regime v1.0** + **Short Straddle v1.0** to Go is undecided — a deep analysis may well conclude we keep the working Python strategies as-is. This records the options + findings so far; to be tuned as we decide.

**Motivation:** consolidate with Dinesh's Go work + (marginal) performance. Two candidate architectures:

**A. Standalone Go (`manja`, Dinesh's current build)** — one Go binary talking *directly* to Zerodha Kite; own login, own WS feed, own systemd units.
- ✅ No Python hop; lowest latency.
- ❌ Reinvents what OpenAlgo already gives us — strategy start/stop/schedule/status, fund mgmt, reports — and duplicates infra (separate login/feed/ops).

**B. Go on the OpenAlgo platform (`openalgo-go` SDK + `execv` shim)** — the cleaner path if we go Go.
- Each Go strategy uses the **`openalgo-go` SDK** (a Go *client* for OpenAlgo's REST/WS API — `github.com/marketcalls/openalgo-go`, ~6 mo stale locally, `git pull` to update) for feed + orders → shares OpenAlgo's single broker session/feed + its order/fund/report layer.
- Managed by the `/python` host via a **~2-line Python `os.execv` shim**: the host launches `python -u shim.py`, which `execv`s the Go binary **in place** (same PID; inherits stdout/env/cwd), so start/stop/schedule/status/log-streaming/restore-on-restart all work **unchanged** — no OpenAlgo core changes. (The host hardcodes `venv/python -u <file>` at `python_strategy.py:540` and accepts only `.py`, so a bare Go binary is NOT managed without this shim.)

**Latency (the key question, settled):** in **B**, the Python OpenAlgo server IS in the I/O path (feed via WS-proxy + ZeroMQ; orders via REST) — Go does **not** remove it. But that overhead is ~single-digit ms, dwarfed by the broker round-trip, and **immaterial for these strategies** (EMA-Regime acts on 3-min bar closes; the straddle enters once at 09:35, monitors ~10 s). So latency is neither a reason to go Go nor a reason to fear it here. Only **A** removes Python from the path — solving a latency problem these strategies don't have.

**Perf reality:** for two *low-frequency* strategies Python is already fast enough; the Go win (compute/concurrency) is marginal. The real driver would be code-consolidation/stack preference — weigh against the port + integration effort.

**Prerequisites for B (confirm before committing):**
1. `openalgo-go` SDK feature-parity — must cover history, quotes, the straddle's multi-leg option order (`optionsmultiorder`), and WS feed subscription.
2. The Go binary must **log to stdout** — the host captures the subprocess's stdout for the `/python` live-log view.

**Recommendation (draft):** latency shouldn't drive this; the platform-reuse instinct (B over A) is sound; but for *these* strategies the least-effort, already-working option is to **keep the Python strategies** unless the Go consolidation benefit clearly justifies the port + shim + SDK work. **Decision pending deep analysis.**

---

## Capital Allocation

| Strategy | Allocated | Notes |
|----------|-----------|-------|
| NIFTY Iron Butterfly | ~1.0 lakh | Hedged, lower margin |
| BANKNIFTY EMA Crossover | ~3.0 lakh | Futures, higher margin |
| Reserve | ~1.0 lakh | Buffer for margin spikes |
| **Total** | **5.0 lakh** | Virtual (analyzer mode) |

---

## Deployment Workflow

1. Edit strategy files locally at `/home/mandar/data/programs/marketcalls/openalgo/strategies/scripts/`
2. Commit to `mock/strategies` branch in local git repo
3. Deploy via SCP: `scp <local_file> root@109.123.248.99:<server_path>`
4. Strategies auto-start via OpenAlgo scheduler at 9:15 AM IST daily
5. Zerodha authentication must be done manually before 9:15 AM IST each day

---

## Manual Override / Kill-Switch (live)

Exit a live position at any point-in-time from the OpenAlgo **Positions** page:
- **Close** (per row) flattens one symbol; **Close All** squares off everything. In live mode both send real MARKET square-off orders to the broker (`close_position_service.py` → `broker_module.close_all_positions`). Fills at market (small slippage).

Both strategies auto-detect a manual close via broker-positionbook reconciliation (`sync_position()` — commented *"detect manual exits via web UI"*) and reset to flat, so they never double-count. **But re-entry behaviour differs — this is the key thing:**

| Strategy | After a manual Close | Re-enters same day? |
|----------|----------------------|---------------------|
| **Straddle** | `sync` clears position; `entry_done_today` stays **True** (not reset by `_clear_position_state`) | **No** — done for the day |
| **Opt1 / EMA (& Regime)** | `sync` sets flat; the loop keeps evaluating signals | **Yes** — re-opens on the next crossover/regime signal |

**Hard override (exit AND stay out for the day):**
- **Straddle** → just **Close** it. It will not re-enter.
- **EMA (Opt1 / Regime)** → **Close the position AND Stop the strategy** (Python Strategy page → *Stop*; sets `manually_stopped`, which blocks re-entry and auto-restart). Closing alone is **not** enough — it re-arms on the next signal.

Notes: there is a few-second gap between your click and the strategy's next `sync` poll — harmless (it briefly monitors an already-flat position, then clears). `Close All` flattens all 4 straddle legs near-simultaneously.

**Reporting stays correct after a manual close (fixed 2026-07-20).** A manual Close-All / broker auto-square-off fill loses its strategy tag — the sandbox stamps it `AUTO_SQUARE_OFF` (verified live: paper Close-All produced 4 `AUTO_SQUARE_OFF`-tagged exit fills). Both the Telegram fill-alerts (`telegram_fill_subscriber`) and the EOD digest (`eod_summary`) now **re-attribute** such fills to the strategy that opened that symbol the same day (when the owner is unique) — so a manually-closed straddle still shows a correct 🔴 EXIT alert, the realized-P&L block, and one clean line in the EOD digest (not a phantom `AUTO_SQUARE_OFF` line). Caveat: BANKNIFTY-fut is shared by both EMA strategies, so a square-off there can't be uniquely split — it stays under `AUTO_SQUARE_OFF` (a non-issue for the straddle-only live plan; unique option symbols resolve cleanly).

**EXIT alerts carry realized P&L (added 2026-07-20; margin added 2026-07-30).** On the fill that fully closes a position, the EXIT alert appends a consolidated block — `Utilised margin / Gross / Charges (Zerodha) / Net / Return on margin` — EMA on each exit, straddle once on the final leg (dedup), so it summarises all 4 legs together. Charges use the same `charges.py` rate card as the EOD digest, and utilised margin uses the same defined-risk basis (iron-fly = wing width × qty − credit; futures = entry notional × 10%), so the alert and the digest always agree.

**Feed-stale TradeBhau alerts (straddle 2026-07-24, EMA 2026-07-30).** All three strategies now push a Telegram alert when their market-data feed goes stale — on onset, throttled re-alerts (`TG_ALERT_INTERVAL`, default 120s) while still stale, and on recovery. The message names the strategy and says whether a position is open and unprotected, so the decision to hit **Close All** is yours to make in real time. Note the two feeds are independent: the straddle polls **REST option quotes**, the EMA strategies consume the **WS tick feed** — one can stall while the other is fine.

---

## Operational Timers (systemd)

Four systemd timers keep the trading server hands-off (all `Mon..Fri`, `Asia/Kolkata`):

| Timer | Schedule (IST) | Purpose |
|-------|----------------|---------|
| `openalgo-restart.timer` | 09:05 | Restart openalgo pre-market (fresh scheduler + broker session) |
| `openalgo-eod-summary.timer` | 15:31 | TradeBhau EOD per-strategy P&L digest → Telegram (`eod_summary.py`) |
| `openalgo-capture-trade-data.timer` | 15:35 | Archive the day's intraday option-chain data (`~/data/zerodha/trade-data`) for backtesting |
| `openalgo-backtest-eval.timer` | 15:45 | Append day's EMA-option rows + rebuild cross-strategy comparison CSVs |

Post-close ordering is deliberate: **15:31** digest reads the day's trades → **15:35** capture archives the chain data → **15:45** eval backtests on it.

**Why the 09:05 restart?** A fresh pre-market restart avoids APScheduler's `ThreadPoolExecutor` "shutdown-after-~2-days" death (scheduler logs `"all checks passed, starting"` at 09:15 IST but never spawns the subprocess; observed May 26 & 29 2026) and clears overnight drift.

**⚠️ Known issue (found 2026-07-17): the 09:05 restart lands _before_ the ~09:15 daily Zerodha login**, so openalgo's WebSocket broker adapter boots **unauthenticated** and the WS feed comes up flapping (stall → reconnect every few minutes) until a manual post-login restart. REST paths (e.g. the straddle's quote polling) are unaffected — they authenticate per call; only the WS-fed EMA strategies are hit. Daily ordering: `~08:55` feed config → `09:05` restart (pre-auth) → `~09:15` login → `09:15` strategies start.

**Decision (2026-07-20): keep the 09:05 restart.** Retiming it *after* login was rejected — it would land at/after the 09:15 strategy start and could interrupt an active EMA entry. Since "before strategies" (09:15) and "after login" (~09:15) collide, retiming can't solve it. Mitigation options (one still to be chosen): (a) **log in before 09:05** — zero-code, makes the restart boot authenticated; (b) **broker-adapter reconnect-on-auth** code fix — keeps current login time, more work, deferred; (c) status-quo — the strategies' own WS auto-reconnect self-recovers, do a manual `systemctl restart openalgo` on a bad day. Regime candle-persistence (2026-07-20) now makes such a manual restart safe — warmup/ER-window survive it.

**Setup:** run `setup_systemd_timers.sh` on the server with `OPENALGO_API_KEY` set (idempotent — safe to re-run). Note: `openalgo-eod-summary.timer` was added manually on 2026-07-11 and may not yet be in the script — fold it in when convenient.

```bash
OPENALGO_API_KEY=<key> ./strategies/scripts/setup_systemd_timers.sh
```

**Inspection commands:**

```bash
systemctl list-timers --no-pager
journalctl -u openalgo-restart.service --since today --no-pager
journalctl -u openalgo-capture-trade-data.service --since today --no-pager
systemctl start openalgo-capture-trade-data.service   # manual on-demand capture
```

Captured data lands in `/root/data/zerodha/trade-data/YYYY-MM-DD/`. Holidays produce empty/incomplete directories (script logs `no data` warnings, exits 0).

---

## Trading Results

| Date | Straddle | EMA Crossover | Notes |
|------|----------|---------------|-------|
| May 8 | Failed (bugs) | +5,262 (manual exit) | Expiry format, lot size bugs |
| May 11 | +2,798 (1 lot debug) | No trade | Debug mode test |
| May 12 | -52,065 (9 lots, SL hit) | No trade | Expiry day, PE exploded |
| May 13 | +4,621 (9 lots) | No trade | Recovered from -15k dip |
| May 14 | +39 (4 lots, OTM4) | No trade (insufficient funds) | Flat day, hedge ate 96% of profit; EMA crossover blocked by margin |
| May 15 | EXIT FAILED at 15:15 (3 lots, OTM8) | SELL filled, settled by catch-up bug | Catch-up processor killed 3/5 positions; EOD exit blocked by analyzer 15:15 cutoff; strategy hung post-market |

---

## Changelog

### May 14 — Hedge width & profit target tuning

**Problem:** OTM4 hedge wings (200 points) cost 58% of gross premium. On calm days, hedge theta decay nearly matches short leg decay, wiping out profits. May 14 backtest showed naked straddle earned +5,265 but iron butterfly only +195 (hedge absorbed 96%).

**Changes:**
| Setting | Before | After | Rationale |
|---------|--------|-------|-----------|
| Hedge offset | OTM4 (200pts) | OTM8 (400pts) | Reduces hedge cost from 58% to 31% of gross; retains more theta profit |
| Profit target | 60% of net premium | 25% of net premium | 60% almost never hit intraday; 25% is realistic theta capture |
| Lots | 4 | 3 | Free margin for EMA crossover (4 lots used ~2L, blocking BNKF futures) |

**Trade-off:** OTM8 has higher max loss (~400pts spread vs 200pts) but significantly better daily P&L on range-bound days. Stop-loss at 50% still caps adverse moves.

### May 15 — State file path fix + analyzer positionbook bug

**Bug 1 (fixed): State file path with special characters.**
OpenAlgo scheduler injects the web UI display name as `STRATEGY_NAME` env var (e.g., `EMA 9/21 Crossover - BankNifty 5min`). The `/` in `9/21` created an invalid subdirectory path, causing state save to fail silently. EMA crossover could not persist state across restarts.

**Fix:** Both strategies now sanitize `STRATEGY_NAME` → `STRATEGY_TAG` (replacing `/` and spaces with `_`) before using it in file paths. Applied to `short_straddle_nifty.py` and `ema_crossover_banknifty.py`.

**Bug 2 (fixed): Catch-up processor kills reopened positions on web UI login.**
On May 15, all 5 orders filled at 09:35 IST. Positions ran normally until 12:32 IST, when a web UI login triggered the `catch_up_processor.catch_up_mis_squareoff()`. This function settles "stale MIS" positions where `created_at < today`. However, 3 of 5 position rows were originally created on previous days and later reopened today by the execution engine. The catch-up processor only checked `created_at` (original row creation) and ignored that the positions were actively traded today.

**Impact:** 3 positions force-settled mid-day: NIFTY 23750CE (short), NIFTY 23350PE (hedge), BANKNIFTY FUT. Margin released, P&L locked at settlement LTP. EMA crossover's position sync detected the missing position and reset to FLAT. Straddle continued monitoring on internal state (unaffected operationally but 2 of 4 legs invisible in positionbook).

**Fix:** Added `SandboxPositions.updated_at < today_start` to the filter in `sandbox/catch_up_processor.py:63`. The execution engine's reopen path commits via ORM, which bumps `updated_at` to today. The daily PnL reset uses raw SQL to avoid bumping `updated_at`. So reopened positions have today's `updated_at` and are excluded from catch-up settlement. Deployed to server (takes effect on next web UI login, no restart needed).

### May 15 — EOD square-off timing + strategy termination

**Bug 3 (fixed): Analyzer blocks MIS orders at exactly 15:15 IST.**
Both strategies attempted EOD square-off at 15:15 IST, but the analyzer's MIS auto-squareoff runs at the same time and rejects new orders. The straddle's EXIT orders failed with "EXIT FAILED" and the strategy hung indefinitely post-market (still running at 22:00+ IST).

**Fix (both strategies):**
- Moved EOD square-off from 15:15 to **15:14 IST** (1 minute before the analyzer cutoff)
- Added **post-squareoff termination**: strategies now set `running = False` and exit cleanly ~5 minutes after squareoff time if no position remains
- Straddle: `SQUAREOFF_MINUTE` default changed from `"15"` to `"14"`, termination at squareoff+5
- EMA crossover: EOD check changed from `>= 15` to `>= 14`, termination at minute >= 19

---

## Event Calendar Dates (2026)

Straddle skips entry on these dates:

| Date | Event |
|------|-------|
| Jun 5 | RBI MPC announcement |
| Jun 10 | US CPI release |
| Jun 17 | FOMC rate decision |
| Jul 14 | US CPI release |
| Jul 29 | FOMC rate decision |
| Aug 5 | RBI MPC announcement |
| Aug 12 | US CPI release |
| Sep 11 | US CPI release |
| Sep 16 | FOMC rate decision |
| Oct 7 | RBI MPC announcement |
| Oct 14 | US CPI release |
| Oct 28 | FOMC rate decision |
| Nov 10 | US CPI release |
| Dec 4 | RBI MPC announcement |
| Dec 9 | FOMC rate decision |
| Dec 10 | US CPI release |
