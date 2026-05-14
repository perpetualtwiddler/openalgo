# Trading Strategies — OpenAlgo Paper Trading

**Platform:** OpenAlgo on algo.oftenuncertain.net (109.123.248.99)
**Mode:** Analyzer (sandbox) with 5,00,000 INR virtual capital
**Paper trading period:** May 2026
**Broker:** Zerodha (daily auth required before 9:15 AM IST)

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
| Lot size | 65 |
| Lots | 3 |
| Quantity | 195 per leg |
| Estimated margin | ~40,000-55,000 INR |

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
| Stop-loss | -50% of net premium | Close all 4 legs |
| EOD square-off | 15:15 IST | Close all 4 legs |
| Position sync | Every 5 seconds | Detect manual/system exits |

### Safety Features
- **Iron butterfly hedge:** OTM8 wings (~400pts) cap maximum loss; wider wings retain more theta profit on calm days
- **Event calendar:** `event_calendar.json` with 16 confirmed high-volatility dates (Jun–Dec 2026) covering RBI MPC, FOMC, US CPI
- **Consecutive SL cooldown:** Pauses after 2 straight stop-loss days
- **Trade history:** Last 30 trades recorded in `_history.json` for cooldown logic
- **State persistence:** JSON state file survives strategy restarts (same-day only)

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
| EOD square-off | Close before MIS deadline |
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
2. Commit to `mandar/strategies` branch in local git repo
3. Deploy via SCP: `scp <local_file> root@109.123.248.99:<server_path>`
4. Strategies auto-start via OpenAlgo scheduler at 9:15 AM IST daily
5. Zerodha authentication must be done manually before 9:15 AM IST each day

---

## Trading Results

| Date | Straddle | EMA Crossover | Notes |
|------|----------|---------------|-------|
| May 8 | Failed (bugs) | +5,262 (manual exit) | Expiry format, lot size bugs |
| May 11 | +2,798 (1 lot debug) | No trade | Debug mode test |
| May 12 | -52,065 (9 lots, SL hit) | No trade | Expiry day, PE exploded |
| May 13 | +4,621 (9 lots) | No trade | Recovered from -15k dip |
| May 14 | +39 (4 lots, OTM4) | No trade (insufficient funds) | Flat day, hedge ate 96% of profit; EMA crossover blocked by margin |

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
