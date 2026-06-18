#!/usr/bin/env python
"""
EMA Trend-Regime Follower — BANKNIFTY 3-Minute  (ER-confirmed)
==============================================================
A trend-REGIME follower, not an EMA-crossover bot. Backtesting (8 days, 2026-06-09..18)
showed the crossover entry churns on chop and no exit rule fixes a bad entry; gating
ENTRY on a trend-regime filter does. See strategies/scripts/CONTEXT.md and the
[[banknifty-regime-detection]] memory.

Candles are BUILT LOCALLY from the WS quote feed (no broker history-API dependency for
signals). CANDLE_TIMEFRAME (env) sets the bar duration — "2m" / "3m" / "5m".

Entry  : when FLAT and the trailing-ER_WINDOW_MIN Kaufman Efficiency Ratio (ER = |net move| /
         |total path|, computed on completed locally-built candles) >= ER_GATE, enter in the
         CURRENT EMA-alignment direction (EMA_fast > EMA_slow -> BUY, else SELL).
         No crossover wait: a cross fires at a regime transition (ER still low); ER only
         confirms a trend ~window-length later, when no fresh cross exists. ER is a
         TRIGGER, not a filter. This is a deliberately LAGGING, confirmation-based entry
         (~45 min after a trend starts) — it forgoes the first leg to skip all the chop.
Exit   : RIDE — let winners run. APPE adaptive profit-protection OR trailing-SL 0.5% OR
         reverse (EMA alignment flips against the position) OR EOD 15:14 — first to fire.
         NO tight stop: a tight stop chops you out on normal trend pullbacks (proven worse
         in backtest); it was only ever a patch for an unfiltered entry.
Product: MIS (intraday, broker auto square-off ~15:15)

*** FORWARD-TEST IN ANALYZER MODE FIRST ***
The current edge rests on only 2 trend days / 4 trades — proof-of-concept, NOT a validated
edge. Deploy with OpenAlgo **Analyzer (Analyze) mode ENABLED** (/analyzer or Settings) so
every order is SIMULATED in the sandbox and NOTHING is sent to the broker. The strategy code
is identical in live vs analyzer mode — the mode is a server-side toggle. Run it paper for
several days (especially trend days) and confirm the ER band holds before considering live.

NOTE: candles come from the live feed only — a mid-day restart rebuilds the ER window from
scratch, so no signals fire for ~ER_WINDOW_MIN after a (re)start. On a normal 09:15 start the
first signal is possible ~max(SLOW_EMA, ER bars) candles in (≈60-75 min).

Run via OpenAlgo /python strategy runner: upload, set exchange=NFO, schedule 09:15-15:20 Mon-Fri.
"""

import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from openalgo import api

# =============================================================================
# CONFIGURATION
# =============================================================================

API_KEY = os.getenv("OPENALGO_API_KEY", "your-api-key")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST', '127.0.0.1')}:{os.getenv('WEBSOCKET_PORT', '8765')}"
)

# Quote mode so the WS bus carries traded volume (recorded for backtesting). The strategy
# itself only needs LTP; quote is a superset that still includes ltp. FEED_MODE=ltp reverts.
FEED_MODE = os.getenv("FEED_MODE", "quote").strip().lower()

UNDERLYING = os.getenv("SYMBOL", "BANKNIFTY")
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NFO"))
QUANTITY = int(os.getenv("QUANTITY", "60"))       # 2 lots x 30 units
LOT_SIZE = int(os.getenv("LOT_SIZE", "30"))       # BANKNIFTY futures lot size
PRODUCT = os.getenv("PRODUCT", "MIS")

# --- Trend-regime entry ---
FAST_EMA = int(os.getenv("FAST_EMA", "5"))
SLOW_EMA = int(os.getenv("SLOW_EMA", "13"))
# Candle duration BUILT LOCALLY from the WS quote feed: "2m" / "3m" / "5m".
CANDLE_TIMEFRAME = os.getenv("CANDLE_TIMEFRAME", "3m")
# ER trend-regime gate. Backtest band 0.55-0.65 cleanly separated trend from chop (0 red
# days); 0.60 = working default (captures both trend days with a buffer from the 0.50 leak).
ER_GATE = float(os.getenv("ER_GATE", "0.60"))
ER_WINDOW_MIN = int(os.getenv("ER_WINDOW_MIN", "60"))  # trailing window for ER, minutes

# --- Exits (RIDE: let winners run; NO tight stop) ---
TRAILING_SL_PCT = float(os.getenv("TRAILING_SL_PCT", "0.5"))  # 0.5% trailing stop

# Adaptive Profit-Protection Exit (APPE) — trails the P&L curve. Same engine/tuning as the
# crossover bot. Arm scales with size: ARM_PER_LOT (₹4,000) per 30u lot -> ₹8,000 @ 60u.
APPE_ENABLED = os.getenv("APPE_ENABLED", "true").lower() == "true"
ARM_PER_LOT = float(os.getenv("ARM_PER_LOT", "4000"))
_arm_override = os.getenv("PROFIT_ARM_THRESHOLD")
PROFIT_ARM_THRESHOLD = float(_arm_override) if _arm_override else ARM_PER_LOT * (QUANTITY / LOT_SIZE)
GIVEBACK_K = float(os.getenv("GIVEBACK_K", "30"))            # give-back budget G = k·√P_max
GIVEBACK_REF_UNITS = float(os.getenv("GIVEBACK_REF_UNITS", "2"))  # √-scale anchor (2 lots = factor 1)
TREND_WINDOW_SEC = float(os.getenv("TREND_WINDOW_SEC", "180"))   # slope lookback
TREND_CONFIRM_SEC = float(os.getenv("TREND_CONFIRM_SEC", "30"))  # breach confirm-and-hold
HARD_MULT = float(os.getenv("HARD_MULT", "2.0"))            # catastrophic give-back multiple

# Feed-health guard: if no WS ticks for this long during market hours, the feed is stale —
# trailing-SL & APPE (tick-driven) are inactive, so we refuse new entries rather than trade blind.
FEED_STALE_SEC = float(os.getenv("FEED_STALE_SEC", "60"))
FEED_STALE_REWARN_SEC = float(os.getenv("FEED_STALE_REWARN_SEC", "60"))

TRADE_DIRECTION = os.getenv("TRADE_DIRECTION", "BOTH")
SIGNAL_CHECK_INTERVAL = int(os.getenv("SIGNAL_CHECK_INTERVAL", "10"))
MAX_LOSS_PER_DAY = float(os.getenv("MAX_LOSS_PER_DAY", "5000"))  # daily circuit breaker

STRATEGY_NAME = os.getenv("STRATEGY_NAME", "EMA_REGIME_BANKNIFTY")
STRATEGY_TAG = STRATEGY_NAME.replace("/", "_").replace(" ", "_")

STATE_DIR = Path(os.getenv("STATE_DIR", "/root/data/openalgo/strategies/state"))
STATE_FILE = STATE_DIR / f"{STRATEGY_TAG}_state.json"


def log_error(msg):
    print(f"\n[ERROR] [{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  {msg}", flush=True)


def efficiency_ratio(closes):
    """Kaufman Efficiency Ratio = |net move| / |total path| over a close series, 0..1.
    ~1 = clean trend, ~0 = round-trip chop."""
    if len(closes) < 2:
        return 0.0
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return net / path if path > 0 else 0.0


def _ema(vals, span):
    """Final EMA value over a list of closes (adjust=False seeding, EMA = α·v + (1-α)·prev)."""
    a = 2.0 / (span + 1)
    e = vals[0]
    for v in vals[1:]:
        e = a * v + (1 - a) * e
    return e


def _tf_minutes(tf):
    try:
        return max(1, int(str(tf).lower().rstrip("m")))
    except ValueError:
        return 3


def resolve_futures_symbol(client, underlying, exchange):
    """Fetch nearest expiry and build the futures symbol (e.g. BANKNIFTY26MAY26FUT)."""
    resp = client.expiry(symbol=underlying, exchange=exchange, instrumenttype="futures")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Cannot fetch expiry for {underlying}: {resp}")
    nearest = resp["data"][0]
    day, mon, yr = nearest.split("-")
    symbol = f"{underlying}{day}{mon}{yr}FUT"
    log(f"[SYMBOL] Resolved {underlying} -> {symbol} (expiry {nearest})")
    return symbol


# =============================================================================
# BOT
# =============================================================================

class EMARegimeBot:
    def __init__(self):
        self.client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
        self.symbol = resolve_futures_symbol(self.client, UNDERLYING, EXCHANGE)
        self.position = None       # "BUY" / "SELL" / None
        self.entry_price = 0.0
        self.trailing_sl = 0.0
        self.peak_price = 0.0
        self.ltp = None
        self.exit_in_progress = False
        self.entry_pending = False
        self.pending_entry_signal = None
        self.pending_entry_order_id = None
        self.pending_exit_order_id = None
        self.pending_exit_reason = None
        self.running = True
        self.stop_event = threading.Event()
        self.daily_pnl = 0.0
        self.trade_count = 0
        # APPE state (persists across same-day restarts; breach timer + slope window rebuild live)
        self.appe_peak = 0.0
        self.appe_armed = False
        self.appe_breach_start = None
        self.pnl_window = deque()
        self.last_tick_ts = None
        self.feed_stale = False
        self.last_stale_warn_ts = 0.0
        self.ws_alive = False
        self.tf_min = _tf_minutes(CANDLE_TIMEFRAME)
        self.er_bars = max(2, ER_WINDOW_MIN // self.tf_min)  # completed bars in the ER window
        # --- candles built locally from the WS quote feed (no history-API dependency) ---
        self.bar_lock = threading.Lock()
        self.session_open_dt = None           # 09:15 anchor for time-bucketing
        self.cur_bar_idx = None
        self.cur_bar_close = None
        # completed-bar closes; keep enough history to settle EMA(SLOW) before the ER window fills
        self.bar_closes = deque(maxlen=self.er_bars + 3 * SLOW_EMA + 5)
        self.instrument = [{"exchange": EXCHANGE, "symbol": self.symbol}]

        self.load_state()

        log(f"[INIT] {STRATEGY_NAME}")
        log(f"[INIT] {self.symbol} on {EXCHANGE} | EMA({FAST_EMA}/{SLOW_EMA}) | {CANDLE_TIMEFRAME}")
        log(f"[INIT] Entry: trend-regime TRIGGER — flat + ER≥{ER_GATE:g} over {ER_WINDOW_MIN}min "
            f"({self.er_bars} bars) → enter in EMA-alignment direction (no crossover wait)")
        log(f"[INIT] Candles built LOCALLY from the {FEED_MODE} feed — {self.tf_min}m bars; "
            f"~{(max(SLOW_EMA, self.er_bars) + 2) * self.tf_min}min warmup before the first signal")
        log(f"[INIT] Exit (RIDE): APPE OR trailing-SL {TRAILING_SL_PCT}% OR alignment-flip reverse "
            f"OR EOD 15:14 — NO tight stop")
        if APPE_ENABLED:
            log(f"[INIT] APPE on: arm≥₹{PROFIT_ARM_THRESHOLD:.0f} "
                f"(₹{ARM_PER_LOT:.0f}×{QUANTITY/LOT_SIZE:g} lots) | "
                f"G={GIVEBACK_K:g}·√peak·√({QUANTITY/LOT_SIZE:g}u/{GIVEBACK_REF_UNITS:g}) | "
                f"trend {TREND_WINDOW_SEC:.0f}s | confirm {TREND_CONFIRM_SEC:.0f}s | hard ×{HARD_MULT:g}")
        else:
            log("[INIT] APPE off — price trailing-SL only")
        log(f"[INIT] Qty: {QUANTITY} | Product: {PRODUCT} | Direction: {TRADE_DIRECTION} | "
            f"Feed: {FEED_MODE} | Max daily loss: {MAX_LOSS_PER_DAY}")
        log("[INIT] *** Run in ANALYZER MODE for forward-testing — orders are simulated, not live ***")
        if self.position:
            log(f"[INIT] Resumed {self.position} @ {self.entry_price:.2f} | TSL: {self.trailing_sl:.2f} | Peak: {self.peak_price:.2f}")

    # -------------------------------------------------------------------------
    # State persistence
    # -------------------------------------------------------------------------

    def save_state(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            state = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "symbol": self.symbol,
                "position": self.position,
                "entry_price": self.entry_price,
                "trailing_sl": self.trailing_sl,
                "peak_price": self.peak_price,
                "daily_pnl": self.daily_pnl,
                "trade_count": self.trade_count,
                "appe_peak": self.appe_peak,
                "appe_armed": self.appe_armed,
            }
            STATE_FILE.write_text(json.dumps(state))
            log(f"[STATE] Saved: {self.position} @ {self.entry_price:.2f}")
        except Exception as e:
            log(f"[STATE ERROR] Save failed: {e}")

    def load_state(self):
        try:
            if not STATE_FILE.exists():
                return
            state = json.loads(STATE_FILE.read_text())
            if state.get("date") != datetime.now().strftime("%Y-%m-%d"):
                log("[STATE] Stale state from previous day — ignoring")
                self.clear_state()
                return
            if state.get("symbol") != self.symbol:
                log(f"[STATE] Symbol mismatch ({state.get('symbol')} vs {self.symbol}) — ignoring")
                self.clear_state()
                return
            self.position = state.get("position")
            self.entry_price = state.get("entry_price", 0.0)
            self.trailing_sl = state.get("trailing_sl", 0.0)
            self.peak_price = state.get("peak_price", 0.0)
            self.daily_pnl = state.get("daily_pnl", 0.0)
            self.trade_count = state.get("trade_count", 0)
            self.appe_peak = state.get("appe_peak", 0.0)
            self.appe_armed = state.get("appe_armed", False)
        except Exception as e:
            log(f"[STATE ERROR] Load failed: {e}")

    def clear_state(self):
        try:
            if STATE_FILE.exists():
                STATE_FILE.unlink()
                log("[STATE] Cleared")
        except Exception as e:
            log(f"[STATE ERROR] Clear failed: {e}")

    # -------------------------------------------------------------------------
    # WebSocket — real-time price + trailing stop-loss + APPE
    # -------------------------------------------------------------------------

    def on_ltp_update(self, data):
        if data.get("type") != "market_data" or data.get("symbol") != self.symbol:
            return

        self.last_tick_ts = time.monotonic()
        self.ltp = float(data["data"]["ltp"])
        if not self.ws_alive:
            self.ws_alive = True
            log(f"[WS] FIRST TICK RECEIVED for {self.symbol} @ {self.ltp:.2f} — pipeline confirmed live")
        if self.feed_stale:
            log("[FEED] Recovered — market-data ticks resumed")
            self.feed_stale = False
            self.last_stale_warn_ts = 0.0

        # Build local candles from the tick stream — every tick, whether flat or in a trade.
        self._update_bar(self.ltp)

        if not self.position or self.exit_in_progress:
            return

        # Update trailing stop-loss (only tightens, never widens).
        if self.position == "BUY":
            unrealized = (self.ltp - self.entry_price) * QUANTITY
            if self.ltp > self.peak_price:
                self.peak_price = self.ltp
                new_tsl = round(self.peak_price * (1 - TRAILING_SL_PCT / 100), 2)
                if new_tsl > self.trailing_sl:
                    self.trailing_sl = new_tsl
            hit_sl = self.ltp <= self.trailing_sl
        else:
            unrealized = (self.entry_price - self.ltp) * QUANTITY
            if self.ltp < self.peak_price:
                self.peak_price = self.ltp
                new_tsl = round(self.peak_price * (1 + TRAILING_SL_PCT / 100), 2)
                if new_tsl < self.trailing_sl:
                    self.trailing_sl = new_tsl
            hit_sl = self.ltp >= self.trailing_sl

        sign = "+" if unrealized > 0 else ""
        log(f"[LTP] {self.ltp:.2f} | {self.position} @ {self.entry_price:.2f} | "
            f"P&L: {sign}{unrealized:.0f} | TSL: {self.trailing_sl:.2f} | Peak: {self.peak_price:.2f}")

        # APPE — adaptive profit-protection exit, first-to-fire vs the price trail.
        if not self.exit_in_progress:
            appe_reason = self._appe_evaluate(unrealized, time.monotonic())
            if appe_reason:
                self.exit_in_progress = True
                threading.Thread(target=self.place_exit, args=(appe_reason,), daemon=True).start()
                return

        if hit_sl and not self.exit_in_progress:
            self.exit_in_progress = True
            log(f"[ALERT] Trailing SL hit at {self.ltp:.2f} (SL was {self.trailing_sl:.2f})")
            threading.Thread(target=self.place_exit, args=("TRAILING_SL",), daemon=True).start()

    # -------------------------------------------------------------------------
    # Local candle construction (from the WS quote feed)
    # -------------------------------------------------------------------------

    def _update_bar(self, ltp):
        """Aggregate ticks into CANDLE_TIMEFRAME bars (close-only), aligned to 09:15. Appends a
        completed bar's close to self.bar_closes when the time bucket rolls over. Thread-safe:
        called on the WS thread while check_signal() reads bar_closes on the strategy thread."""
        now = datetime.now()
        with self.bar_lock:
            day_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
            if self.session_open_dt != day_open:        # first tick / new day → reset bars
                self.session_open_dt = day_open
                self.cur_bar_idx = None
                self.cur_bar_close = None
                self.bar_closes.clear()
            if now < self.session_open_dt:
                return                                  # pre-open ticks — don't build bars
            idx = int((now - self.session_open_dt).total_seconds() // (self.tf_min * 60))
            if self.cur_bar_idx is None:
                self.cur_bar_idx = idx
            elif idx != self.cur_bar_idx:               # bucket rolled over → prior bar closed
                self.bar_closes.append(self.cur_bar_close)
                self.cur_bar_idx = idx
                log(f"[BAR] {self.tf_min}m close={self.cur_bar_close:.2f} | {len(self.bar_closes)} completed")
            self.cur_bar_close = ltp                    # latest price = forming bar's close

    # -------------------------------------------------------------------------
    # APPE — Adaptive Profit-Protection Exit
    # -------------------------------------------------------------------------

    def _feed_age(self):
        return None if self.last_tick_ts is None else (time.monotonic() - self.last_tick_ts)

    def _feed_ok(self):
        age = self._feed_age()
        return age is not None and age <= FEED_STALE_SEC

    def _check_feed_health(self):
        if self._feed_ok():
            return
        now = time.monotonic()
        if self.feed_stale and (now - self.last_stale_warn_ts) < FEED_STALE_REWARN_SEC:
            return
        self.feed_stale = True
        self.last_stale_warn_ts = now
        age = self._feed_age()
        age_str = "no ticks yet" if age is None else f"no ticks for {age:.0f}s"
        ws_status = "" if self.ws_alive else " (WS has NEVER delivered a tick this run — likely a misconfigured WEBSOCKET_URL or broken WS proxy)"
        log_error(
            f"Market-data feed STALE ({age_str}, >{FEED_STALE_SEC:.0f}s){ws_status} — "
            f"trailing-SL & APPE are INACTIVE; blocking new entries"
            + ("; a POSITION IS OPEN AND UNPROTECTED — consider manual square-off." if self.position else ".")
        )

    def _reset_appe(self):
        self.appe_peak = 0.0
        self.appe_armed = False
        self.appe_breach_start = None
        self.pnl_window.clear()

    def _pnl_slope_negative(self):
        pts = self.pnl_window
        if len(pts) < 5:
            return False
        span = pts[-1][0] - pts[0][0]
        if span < TREND_WINDOW_SEC * 0.5:
            return False
        n = len(pts)
        t0 = pts[0][0]
        xs = [p[0] - t0 for p in pts]
        ys = [p[1] for p in pts]
        mx = sum(xs) / n
        my = sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0:
            return False
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False)) / den
        return slope < 0

    def _appe_evaluate(self, unrealized, now):
        """Return 'APPE_HARD' / 'APPE_RATCHET' / None."""
        if not APPE_ENABLED:
            return None

        if unrealized > self.appe_peak:
            self.appe_peak = unrealized
        self.pnl_window.append((now, unrealized))
        cutoff = now - TREND_WINDOW_SEC
        while self.pnl_window and self.pnl_window[0][0] < cutoff:
            self.pnl_window.popleft()

        if not self.appe_armed:
            if self.appe_peak >= PROFIT_ARM_THRESHOLD:
                self.appe_armed = True
                log(f"[APPE] Armed — peak ₹{self.appe_peak:.0f} ≥ arm ₹{PROFIT_ARM_THRESHOLD:.0f}")
            else:
                return None

        _units_factor = math.sqrt((QUANTITY / LOT_SIZE) / GIVEBACK_REF_UNITS)
        budget = GIVEBACK_K * math.sqrt(max(self.appe_peak, 0.0)) * _units_factor
        floor = self.appe_peak - budget
        giveback = self.appe_peak - unrealized

        if giveback >= HARD_MULT * budget:
            log(f"[APPE] HARD exit — give-back ₹{giveback:.0f} ≥ {HARD_MULT:g}×budget ₹{budget:.0f} "
                f"(peak ₹{self.appe_peak:.0f}, U ₹{unrealized:.0f})")
            return "APPE_HARD"

        if unrealized < floor:
            if self._pnl_slope_negative():
                if self.appe_breach_start is None:
                    self.appe_breach_start = now
                    log(f"[APPE] Breach floor ₹{floor:.0f} (U ₹{unrealized:.0f}, peak ₹{self.appe_peak:.0f}) "
                        f"+ trend down — confirming {TREND_CONFIRM_SEC:.0f}s...")
                elif now - self.appe_breach_start >= TREND_CONFIRM_SEC:
                    log(f"[APPE] Confirmed (held {now - self.appe_breach_start:.0f}s) — exit @ U ₹{unrealized:.0f}")
                    return "APPE_RATCHET"
            else:
                self.appe_breach_start = None
        else:
            self.appe_breach_start = None
        return None

    def start_websocket(self):
        while not self.stop_event.is_set():
            try:
                self.client.connect()
                if FEED_MODE == "quote":
                    self.client.subscribe_quote(self.instrument, on_data_received=self.on_ltp_update)
                else:
                    self.client.subscribe_ltp(self.instrument, on_data_received=self.on_ltp_update)
                log(f"[WS] Subscribed to {self.symbol} ({FEED_MODE} mode) — waiting for first tick...")
                while not self.stop_event.is_set():
                    time.sleep(1)
            except Exception as e:
                log_error(f"WebSocket connection error: {e}")
            finally:
                try:
                    if FEED_MODE == "quote":
                        self.client.unsubscribe_quote(self.instrument)
                    else:
                        self.client.unsubscribe_ltp(self.instrument)
                    self.client.disconnect()
                except Exception:
                    pass
            if not self.stop_event.is_set():
                log("[WS] Reconnecting in 5s...")
                time.sleep(5)

    # -------------------------------------------------------------------------
    # Trend-regime signal (on locally-built candles)
    # -------------------------------------------------------------------------

    def check_signal(self):
        """Trend-regime trigger on the LOCALLY-BUILT candles (self.bar_closes). Returns the
        desired DIRECTION ('BUY'/'SELL') when the trailing-window ER confirms a trend
        (>= ER_GATE), else None.

        Used by the loop in both flat and in-position branches:
          • FLAT + ER≥gate          -> returns alignment dir  -> loop ENTERS.
          • IN POSITION + ER≥gate    -> returns alignment dir; if it flipped vs the open
            position the loop REVERSES (close + re-enter); if unchanged the loop holds.
          • ER<gate                  -> None: no new entry; an open position rides on APPE/TSL.
        Direction = current EMA alignment (EMA_fast vs EMA_slow on the completed bars); no
        crossover is required (crosses fire before ER confirms — see module docstring)."""
        if not self._feed_ok():
            age = self._feed_age()
            age_str = "no ticks yet" if age is None else f"no ticks for {age:.0f}s"
            log(f"[SIGNAL CHECK SKIPPED] Feed stale ({age_str}) — not evaluating regime without live ticks")
            return None

        with self.bar_lock:
            closes = list(self.bar_closes)   # completed bars only; the forming bar is excluded

        need = max(SLOW_EMA, self.er_bars) + 2
        if len(closes) < need:
            log(f"[REGIME CHECK] warming up — {len(closes)}/{need} completed {self.tf_min}m bars")
            return None

        ema_fast = _ema(closes, FAST_EMA)
        ema_slow = _ema(closes, SLOW_EMA)
        er = efficiency_ratio(closes[-(self.er_bars + 1):])
        align = "BUY" if ema_fast > ema_slow else "SELL"

        log(f"[REGIME CHECK] ER({ER_WINDOW_MIN}min)={er:.2f} vs gate {ER_GATE:g} | "
            f"EMA({FAST_EMA})={ema_fast:.1f} {'>' if align == 'BUY' else '<'} "
            f"EMA({SLOW_EMA})={ema_slow:.1f} → {align} | "
            f"{'TREND — armed' if er >= ER_GATE else 'chop — standing down'}")

        if er < ER_GATE:
            return None
        if align == "BUY" and TRADE_DIRECTION in ("LONG", "BOTH"):
            return "BUY"
        if align == "SELL" and TRADE_DIRECTION in ("SHORT", "BOTH"):
            return "SELL"
        return None

    # -------------------------------------------------------------------------
    # Order Execution
    # -------------------------------------------------------------------------

    def get_fill_price(self, order_id):
        for _ in range(5):
            time.sleep(2)
            try:
                resp = self.client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
                if resp.get("status") == "success":
                    d = resp.get("data", {})
                    if d.get("order_status") == "complete":
                        price = float(d.get("average_price", 0))
                        if price > 0:
                            return price
                    elif d.get("order_status") in ("rejected", "cancelled"):
                        log(f"[ORDER] {d.get('order_status')}: {d.get('status_message', '')}")
                        return None
            except Exception as e:
                log(f"[ORDER STATUS ERROR] {e}")
        return None

    def get_position_snapshot(self):
        try:
            resp = self.client.positionbook()
            if resp.get("status") != "success":
                return None
            net_qty = 0
            avg_price = 0.0
            for p in resp.get("data", []):
                if p.get("symbol") == self.symbol and p.get("product") == PRODUCT:
                    qty = int(p.get("quantity", 0))
                    net_qty += qty
                    if qty != 0:
                        avg_price = float(p.get("average_price", 0) or avg_price)
            return {"net_qty": net_qty, "average_price": avg_price}
        except Exception as e:
            log(f"[SYNC ERROR] Position snapshot failed: {e}")
            return None

    def initialize_position_from_snapshot(self, signal, snapshot):
        price = snapshot.get("average_price") or self.ltp
        if not price:
            log_error(f"Cannot initialize {signal} state — position exists but no average price/LTP available")
            return False
        self.position = signal
        self.entry_price = float(price)
        self.peak_price = self.entry_price
        self._reset_appe()
        if signal == "BUY":
            self.trailing_sl = round(self.entry_price * (1 - TRAILING_SL_PCT / 100), 2)
        else:
            self.trailing_sl = round(self.entry_price * (1 + TRAILING_SL_PCT / 100), 2)
        self.entry_pending = False
        self.pending_entry_signal = None
        self.pending_entry_order_id = None
        self.exit_in_progress = False
        self.trade_count += 1
        self.save_state()
        log(f"[ENTRY] Reconciled {signal} position @ {self.entry_price:.2f} | TSL: {self.trailing_sl:.2f}")
        return True

    def reconcile_pending_entry(self):
        if not self.entry_pending or not self.pending_entry_signal:
            return
        snapshot = self.get_position_snapshot()
        if snapshot is None:
            return
        net_qty = snapshot.get("net_qty", 0)
        expected_side = 1 if self.pending_entry_signal == "BUY" else -1
        if net_qty * expected_side > 0:
            self.initialize_position_from_snapshot(self.pending_entry_signal, snapshot)
            return

        if self.pending_entry_order_id:
            try:
                resp = self.client.orderstatus(order_id=self.pending_entry_order_id, strategy=STRATEGY_NAME)
                if resp.get("status") == "success":
                    status = resp.get("data", {}).get("order_status")
                    if status in ("rejected", "cancelled"):
                        log_error(f"Pending entry {self.pending_entry_order_id} ended as {status}; clearing pending entry")
                        self.entry_pending = False
                        self.pending_entry_signal = None
                        self.pending_entry_order_id = None
                        self.exit_in_progress = False
            except Exception as e:
                log(f"[ENTRY RECONCILE ERROR] {e}")

    def reconcile_pending_exit(self):
        if not self.pending_exit_order_id:
            return
        snapshot = self.get_position_snapshot()
        if snapshot is None:
            return
        if snapshot.get("net_qty", 0) == 0:
            log(f"[EXIT] Confirmed flat after {self.pending_exit_reason or 'pending exit'}")
            self.position = None
            self.entry_price = 0.0
            self.trailing_sl = 0.0
            self.peak_price = 0.0
            self._reset_appe()
            self.exit_in_progress = False
            self.pending_exit_order_id = None
            self.pending_exit_reason = None
            self.save_state()
            return

        try:
            resp = self.client.orderstatus(order_id=self.pending_exit_order_id, strategy=STRATEGY_NAME)
            if resp.get("status") == "success":
                status = resp.get("data", {}).get("order_status")
                if status in ("rejected", "cancelled"):
                    log_error(f"Pending exit {self.pending_exit_order_id} ended as {status}; position still open")
                    self.pending_exit_order_id = None
                    self.pending_exit_reason = None
                    self.exit_in_progress = False
        except Exception as e:
            log(f"[EXIT RECONCILE ERROR] {e}")

    def place_entry(self, signal):
        if self.daily_pnl <= -MAX_LOSS_PER_DAY:
            log(f"[CIRCUIT BREAKER] Daily loss {self.daily_pnl:.0f} exceeds limit {MAX_LOSS_PER_DAY} — no new trades")
            return False

        if not self._feed_ok():
            age = self._feed_age()
            age_str = "no ticks yet" if age is None else f"no ticks for {age:.0f}s"
            log_error(f"Skipping {signal} entry — market-data feed stale ({age_str}); "
                      f"refusing to trade blind without trailing-SL/APPE protection.")
            return False

        if self.position and self.position != signal:
            log(f"[REVERSE] Closing {self.position} before entering {signal}")
            self.place_exit("REVERSE_SIGNAL")
            time.sleep(1)

        if self.position:
            return False

        log(f"[ENTRY] Placing {signal} for {QUANTITY} qty of {self.symbol}")
        try:
            resp = self.client.placeorder(
                strategy=STRATEGY_NAME, symbol=self.symbol, exchange=EXCHANGE,
                action=signal, quantity=QUANTITY, price_type="MARKET", product=PRODUCT,
            )
            if resp.get("status") == "success":
                order_id = resp.get("orderid")
                log(f"[ENTRY] Order placed: {order_id}")
                price = self.get_fill_price(order_id)
                if price:
                    self.position = signal
                    self.entry_price = price
                    self.peak_price = price
                    self._reset_appe()
                    if signal == "BUY":
                        self.trailing_sl = round(price * (1 - TRAILING_SL_PCT / 100), 2)
                    else:
                        self.trailing_sl = round(price * (1 + TRAILING_SL_PCT / 100), 2)
                    self.exit_in_progress = False
                    self.trade_count += 1
                    self.save_state()
                    log(f"[ENTRY] Filled @ {price:.2f} | TSL: {self.trailing_sl:.2f} | Trade #{self.trade_count}")
                    return True
                log_error(f"ENTRY {signal} placed (order {order_id}) but fill price NOT confirmed — reconciling via positionbook.")
                self.entry_pending = True
                self.pending_entry_signal = signal
                self.pending_entry_order_id = order_id
                self.exit_in_progress = True
                self.reconcile_pending_entry()
            else:
                log_error(f"ENTRY {signal} order REJECTED/failed: {resp}")
        except Exception as e:
            log_error(f"ENTRY {signal} exception: {e}")
        return False

    def place_exit(self, reason="Manual"):
        if not self.position:
            self.exit_in_progress = False
            return

        # Snapshot BEFORE placing — sync_position runs concurrently and resets state once
        # the position closes; without the snapshot the P&L below reads entry_price=0.
        position = self.position
        entry_price = self.entry_price

        exit_action = "SELL" if position == "BUY" else "BUY"
        log(f"[EXIT] Closing {position} — reason: {reason}")

        try:
            resp = self.client.placeorder(
                strategy=STRATEGY_NAME, symbol=self.symbol, exchange=EXCHANGE,
                action=exit_action, quantity=QUANTITY, price_type="MARKET", product=PRODUCT,
            )
            if resp.get("status") == "success":
                order_id = resp.get("orderid")
                exit_price = self.get_fill_price(order_id)
                if exit_price:
                    if position == "BUY":
                        pnl = (exit_price - entry_price) * QUANTITY
                    else:
                        pnl = (entry_price - exit_price) * QUANTITY
                    self.daily_pnl += pnl
                    sign = "+" if pnl > 0 else ""
                    log(f"[EXIT] Filled @ {exit_price:.2f} | P&L: {sign}{pnl:.0f} | Day total: {self.daily_pnl:.0f}")
                else:
                    log_error(f"EXIT {position} ({reason}) placed (order {order_id}) but fill NOT confirmed — keeping pending until positionbook confirms flat.")
                    self.pending_exit_order_id = order_id
                    self.pending_exit_reason = reason
                    self.reconcile_pending_exit()
                    return

                self.pending_exit_order_id = order_id
                self.pending_exit_reason = reason
                self.reconcile_pending_exit()
            else:
                log_error(f"EXIT {position} ({reason}) order REJECTED/failed — POSITION MAY STILL BE OPEN: {resp}")
                self.exit_in_progress = False
        except Exception as e:
            log_error(f"EXIT {position} ({reason}) exception — POSITION MAY STILL BE OPEN: {e}")
            self.exit_in_progress = False

    # -------------------------------------------------------------------------
    # Position sync — detect manual exits via web UI
    # -------------------------------------------------------------------------

    def sync_position(self):
        try:
            resp = self.client.positionbook()
            if resp.get("status") != "success":
                return
            net_qty = 0
            for p in resp.get("data", []):
                if p.get("symbol") == self.symbol and p.get("product") == PRODUCT:
                    net_qty += int(p.get("quantity", 0))
            if net_qty == 0 and self.position:
                log(f"[SYNC] Position gone (manual exit?) — resetting from {self.position}")
                self.position = None
                self.entry_price = 0.0
                self.trailing_sl = 0.0
                self.peak_price = 0.0
                self._reset_appe()
                self.exit_in_progress = False
                self.save_state()
        except Exception as e:
            log(f"[SYNC ERROR] {e}")

    # -------------------------------------------------------------------------
    # Strategy Loop
    # -------------------------------------------------------------------------

    def strategy_loop(self):
        log("[STRATEGY] Loop started")
        while not self.stop_event.is_set():
            try:
                now = datetime.now()

                if now.hour < 9 or (now.hour == 9 and now.minute < 15):
                    time.sleep(30)
                    continue
                if now.hour >= 15 and now.minute >= 14:
                    self.reconcile_pending_entry()
                    self.reconcile_pending_exit()
                    if self.position and not self.exit_in_progress:
                        log("[EOD] 15:14 — closing position for end of day")
                        self.place_exit("EOD_SQUAREOFF")
                    if not self.position and not self.exit_in_progress:
                        self.clear_state()
                    if now.minute >= 19 and not self.position and not self.exit_in_progress:
                        log("[EOD] Post-squareoff — strategy finished for the day.")
                        self.running = False
                        self.stop_event.set()
                        return
                    if now.minute >= 19:
                        log_error("EOD exit still pending or position still open — keeping strategy alive for retry/manual intervention")
                    time.sleep(60)
                    continue

                if self.daily_pnl <= -MAX_LOSS_PER_DAY:
                    if self.position and not self.exit_in_progress:
                        self.place_exit("DAILY_LOSS_LIMIT")
                    log(f"[PAUSED] Daily loss limit hit: {self.daily_pnl:.0f}")
                    time.sleep(60)
                    continue

                self._check_feed_health()
                self.reconcile_pending_entry()
                self.reconcile_pending_exit()

                if self.position:
                    self.sync_position()

                if not self.position and not self.exit_in_progress:
                    signal = self.check_signal()
                    if signal:
                        self.place_entry(signal)
                elif self.position and not self.exit_in_progress:
                    signal = self.check_signal()
                    if signal and signal != self.position:
                        # Trend regime confirmed in the OPPOSITE direction (alignment flipped).
                        self.exit_in_progress = True
                        # If the TSL is already breached at poll time, the WS thread lost the
                        # race — exit as TRAILING_SL and skip the reverse re-entry to avoid
                        # compounding the loss.
                        tsl_hit = (
                            self.ltp is not None and self.trailing_sl > 0 and (
                                (self.position == "BUY" and self.ltp <= self.trailing_sl) or
                                (self.position == "SELL" and self.ltp >= self.trailing_sl)
                            )
                        )
                        if tsl_hit:
                            log(f"[SIGNAL] Reverse but TSL already breached "
                                f"(LTP {self.ltp:.2f} vs TSL {self.trailing_sl:.2f}) — "
                                f"exiting as TRAILING_SL, skipping reverse entry")
                            self.place_exit("TRAILING_SL")
                        else:
                            self.place_exit("REVERSE_SIGNAL")
                            time.sleep(1)
                            self.place_entry(signal)

                time.sleep(SIGNAL_CHECK_INTERVAL)

            except Exception as e:
                log_error(f"Strategy loop exception: {e}")
                time.sleep(10)

    # -------------------------------------------------------------------------
    # Run
    # -------------------------------------------------------------------------

    def run(self):
        log("=" * 65)
        log(f"  EMA TREND-REGIME FOLLOWER — {self.symbol} {CANDLE_TIMEFRAME}")
        log(f"  Entry: flat + ER≥{ER_GATE:g}/{ER_WINDOW_MIN}min → EMA({FAST_EMA}/{SLOW_EMA}) alignment dir")
        log(f"  Exit (RIDE): APPE / trailing-SL {TRAILING_SL_PCT}% / alignment-flip / EOD")
        log(f"  Direction: {TRADE_DIRECTION} | Qty: {QUANTITY} | Product: {PRODUCT}")
        log("  *** ANALYZER MODE recommended — forward-test on paper first ***")
        log("=" * 65)

        ws_t = threading.Thread(target=self.start_websocket, daemon=True)
        ws_t.start()
        time.sleep(2)

        strat_t = threading.Thread(target=self.strategy_loop, daemon=True)
        strat_t.start()

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            log("[SHUTDOWN] Stopping bot...")
            self.running = False
            self.stop_event.set()
            if self.position and not self.exit_in_progress:
                self.place_exit("SHUTDOWN")
            ws_t.join(timeout=5)
            strat_t.join(timeout=5)
            log(f"[SHUTDOWN] Done. Trades: {self.trade_count} | Day P&L: {self.daily_pnl:.0f}")


if __name__ == "__main__":
    bot = EMARegimeBot()
    bot.run()
