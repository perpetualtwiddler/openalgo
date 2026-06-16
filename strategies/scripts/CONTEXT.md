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
  daemon, OWN Zerodha WS (zero trading-latency impact), NIFTY 50 quotes → slim
  `{capture_ts,data}` JSONL at `/root/data/mkdskite/data/<date>/`. Reuses the token file
  (splits composite), resolves symbols via Zerodha's instruments dump (NO OpenAlgo SymToken dep),
  polls for token at 09:00, hot-swaps on mid-day token rotation. systemd `mkdskite.timer` Mon–Fri
  09:00 IST → service self-exits 15:30. `Profile` abstraction ready for a future scoped F&O profile.

## Backtesting (`strategies/scripts/`)
- **`backtest_ticks.py`** — replays a day's capture tick-by-tick; resamples to any timeframe;
  exits replayed tick-by-tick, **parity-verified vs live** (`test/test_appe_tsl_parity.py`, 56/56).
  Flags: `--tf --fast --slow --warmup --vol-sma --vol-mult --reverse-confirm-pct
  --gap-gate <pts> --early-entry`. Runs PRICE-ONLY (gate minus volume) vs VOL-FILTER side by side.
  On quote days shows `[real vol]`; LTP-only days fall back to a (useless, near-constant) tick-count proxy.
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

## Open items / next steps
1. Push `mock/strategies` to `origin` (github perpetualtwiddler/openalgo) — several commits ahead.
2. Set a remote for `mkdskite.git` and push.
3. **Accumulate multi-day captures** (esp. trend days) then run `bt_daily.sh` across them — the
   real evaluation of 8/17 (2m vs 3m), close-vs-early, gap-gate, volume on/off.
4. Later: add the scoped **F&O profile** to mkdskite (index futures/options, full mode for OI).
