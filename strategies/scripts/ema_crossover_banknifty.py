#!/usr/bin/env python
"""
EMA(9/21) Crossover Strategy — BANKNIFTY 5-Minute
==================================================
Buys/sells BANKNIFTY futures on EMA crossover with volume confirmation.

Entry : EMA(9) crosses EMA(21) on 5-min candles
Filter: Volume > 1.5x SMA(20) of volume
Exit  : APPE adaptive profit-protection (trails the P&L curve) OR trailing
        stop-loss 0.5% OR reverse crossover signal — first to fire wins
Product: MIS (intraday, auto square-off by broker at 3:15 PM)

Run standalone:
    export OPENALGO_API_KEY="your-api-key"
    python ema_crossover_banknifty.py

Run via OpenAlgo /python strategy runner:
    Upload this file, set exchange=NFO, schedule 09:15-15:15 Mon-Fri.
"""

import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from openalgo import api

# =============================================================================
# CONFIGURATION
# =============================================================================

API_KEY = os.getenv("OPENALGO_API_KEY", "your-api-key")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST', '127.0.0.1')}:{os.getenv('WEBSOCKET_PORT', '8765')}"
)

UNDERLYING = os.getenv("SYMBOL", "BANKNIFTY")
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NFO"))
QUANTITY = int(os.getenv("QUANTITY", "60"))       # 2 lots x 30 units
LOT_SIZE = int(os.getenv("LOT_SIZE", "30"))       # BANKNIFTY futures lot size (confirmed 30)
PRODUCT = os.getenv("PRODUCT", "MIS")

FAST_EMA = int(os.getenv("FAST_EMA", "9"))
SLOW_EMA = int(os.getenv("SLOW_EMA", "21"))
CANDLE_TIMEFRAME = os.getenv("CANDLE_TIMEFRAME", "5m")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "3"))

VOLUME_FILTER_MULT = float(os.getenv("VOLUME_FILTER_MULT", "1.5"))
VOLUME_SMA_PERIOD = int(os.getenv("VOLUME_SMA_PERIOD", "20"))

TRAILING_SL_PCT = float(os.getenv("TRAILING_SL_PCT", "0.5"))  # 0.5%

# --- Adaptive Profit-Protection Exit (APPE) — see ADAPTIVE_PROFIT_EXIT_DESIGN.md ---
# Trails the *profit curve* (not price): once profit peaks past the arm threshold, exit when it
# gives back a peak-scaled budget AND the smoothed P&L slope confirms a down-drift (held H sec).
APPE_ENABLED = os.getenv("APPE_ENABLED", "true").lower() == "true"
# Gate 1 arm threshold scales with position size so the required points-of-drift to
# arm stays constant regardless of lots traded. ARM_PER_LOT (₹4,000) per 30-unit lot:
#   30u -> ₹4,000 | 60u -> ₹8,000 | 90u -> ₹12,000  (all ≈133 BANKNIFTY pts to arm)
# Lowered from ₹5,000 to ₹4,000/lot ("HAVRATPANA CONTROL": cap profit expectations to
# keep gain/risk balanced and arm APPE sooner). At ₹5,000/lot the ₹10,000 arm on 60u
# just missed real peaks that then round-tripped to a loss — e.g. Jun 8: a +9,504 peak
# never armed and exited TSL at -7,188; a replay showed an ₹8,000 arm would have booked
# ~+5,220. Set an explicit PROFIT_ARM_THRESHOLD env to override the per-lot formula.
ARM_PER_LOT = float(os.getenv("ARM_PER_LOT", "4000"))        # APPE arm ₹ per lot of LOT_SIZE
_arm_override = os.getenv("PROFIT_ARM_THRESHOLD")
PROFIT_ARM_THRESHOLD = (
    float(_arm_override) if _arm_override else ARM_PER_LOT * (QUANTITY / LOT_SIZE)
)  # ₹ profit before APPE arms (Gate 1)
GIVEBACK_K = float(os.getenv("GIVEBACK_K", "30"))             # give-back budget G = k·√P_max (Gate 2)
TREND_WINDOW_SEC = float(os.getenv("TREND_WINDOW_SEC", "180"))    # slope lookback (Gate 3a)
TREND_CONFIRM_SEC = float(os.getenv("TREND_CONFIRM_SEC", "30"))   # breach hold / patience (Gate 3b)
HARD_MULT = float(os.getenv("HARD_MULT", "2.0"))             # catastrophic give-back multiple (Gate 4)
# Reverse-signal confirmation SHADOW (design doc §14) — LOG-ONLY, does not change trading.
# On a reverse-signal exit, records what a "confirm the reverse only on price follow-through"
# filter WOULD have decided, to gather real-fidelity evidence on this rare event over time.
REVERSE_CONFIRM_PCT = float(os.getenv("REVERSE_CONFIRM_PCT", "0.0005"))  # 0.05% (~28 BANKNIFTY pts)

# Feed-health guard: if no WS ticks arrive for this long during market hours, the feed is
# considered stale — trailing-SL & APPE (both tick-driven) are inactive, so we log ERROR and
# refuse new entries rather than trade blind (June 3: 0 ticks all day -> unprotected -20,760).
FEED_STALE_SEC = float(os.getenv("FEED_STALE_SEC", "60"))


def log_error(msg):
    """Emit a clearly-marked, flushed ERROR line for abnormal conditions (greppable)."""
    print(f"\n[ERROR] [{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

TRADE_DIRECTION = os.getenv("TRADE_DIRECTION", "BOTH")
SIGNAL_CHECK_INTERVAL = int(os.getenv("SIGNAL_CHECK_INTERVAL", "10"))

MAX_LOSS_PER_DAY = float(os.getenv("MAX_LOSS_PER_DAY", "5000"))  # daily circuit breaker

STRATEGY_NAME = os.getenv("STRATEGY_NAME", "EMA_9_21_BANKNIFTY")
STRATEGY_TAG = STRATEGY_NAME.replace("/", "_").replace(" ", "_")

STATE_DIR = Path(os.getenv("STATE_DIR", "/root/data/openalgo/strategies/state"))
STATE_FILE = STATE_DIR / f"{STRATEGY_TAG}_state.json"


def resolve_futures_symbol(client, underlying, exchange):
    """Fetch nearest expiry and build the futures symbol (e.g. BANKNIFTY26MAY26FUT)."""
    resp = client.expiry(symbol=underlying, exchange=exchange, instrumenttype="futures")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Cannot fetch expiry for {underlying}: {resp}")
    nearest = resp["data"][0]
    day, mon, yr = nearest.split("-")
    symbol = f"{underlying}{day}{mon}{yr}FUT"
    print(f"[SYMBOL] Resolved {underlying} -> {symbol} (expiry {nearest})")
    return symbol


# =============================================================================
# BOT
# =============================================================================

class EMACrossoverBot:
    def __init__(self):
        self.client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
        self.symbol = resolve_futures_symbol(self.client, UNDERLYING, EXCHANGE)
        self.position = None       # "BUY" or "SELL" or None
        self.entry_price = 0.0
        self.trailing_sl = 0.0
        self.peak_price = 0.0      # tracks best price for trailing SL
        self.ltp = None
        self.exit_in_progress = False
        self.running = True
        self.stop_event = threading.Event()
        self.daily_pnl = 0.0
        self.trade_count = 0
        # APPE state (profit-protection trailing exit). appe_peak/appe_armed persist across
        # same-day restarts; breach timer + slope window rebuild from live ticks.
        self.appe_peak = 0.0           # P_max — peak unrealized profit this trade
        self.appe_armed = False
        self.appe_breach_start = None  # time.monotonic() when the floor breach began
        self.pnl_window = deque()      # (monotonic_ts, unrealized) over TREND_WINDOW_SEC
        self.last_tick_ts = None       # time.monotonic() of last on_ltp_update (feed-health)
        self.feed_stale = False        # True while the WS tick feed is stale
        self.shadow_reverse = None     # §14 reverse-confirm SHADOW (log-only); set on a reverse exit
        self.instrument = [{"exchange": EXCHANGE, "symbol": self.symbol}]

        self.load_state()

        print(f"[INIT] {STRATEGY_NAME}")
        print(f"[INIT] {self.symbol} on {EXCHANGE} | EMA({FAST_EMA}/{SLOW_EMA}) | {CANDLE_TIMEFRAME}")
        print(f"[INIT] Volume filter: >{VOLUME_FILTER_MULT}x SMA({VOLUME_SMA_PERIOD})")
        print(f"[INIT] Trailing SL: {TRAILING_SL_PCT}% | Max daily loss: {MAX_LOSS_PER_DAY}")
        if APPE_ENABLED:
            print(f"[INIT] APPE on: arm≥₹{PROFIT_ARM_THRESHOLD:.0f} "
                  f"(₹{ARM_PER_LOT:.0f}×{QUANTITY/LOT_SIZE:g} lots) | G={GIVEBACK_K:g}·√peak | "
                  f"trend {TREND_WINDOW_SEC:.0f}s | confirm {TREND_CONFIRM_SEC:.0f}s | hard ×{HARD_MULT:g}")
        else:
            print("[INIT] APPE off — price trailing-SL only")
        print(f"[INIT] Qty: {QUANTITY} | Product: {PRODUCT} | Direction: {TRADE_DIRECTION}")
        if self.position:
            print(f"[INIT] Resumed {self.position} @ {self.entry_price:.2f} | TSL: {self.trailing_sl:.2f} | Peak: {self.peak_price:.2f}")

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
            print(f"[STATE] Saved: {self.position} @ {self.entry_price:.2f}")
        except Exception as e:
            print(f"[STATE ERROR] Save failed: {e}")

    def load_state(self):
        try:
            if not STATE_FILE.exists():
                return
            state = json.loads(STATE_FILE.read_text())
            if state.get("date") != datetime.now().strftime("%Y-%m-%d"):
                print("[STATE] Stale state from previous day — ignoring")
                self.clear_state()
                return
            if state.get("symbol") != self.symbol:
                print(f"[STATE] Symbol mismatch ({state.get('symbol')} vs {self.symbol}) — ignoring")
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
            print(f"[STATE ERROR] Load failed: {e}")

    def clear_state(self):
        try:
            if STATE_FILE.exists():
                STATE_FILE.unlink()
                print("[STATE] Cleared")
        except Exception as e:
            print(f"[STATE ERROR] Clear failed: {e}")

    # -------------------------------------------------------------------------
    # WebSocket — real-time price + trailing stop-loss
    # -------------------------------------------------------------------------

    def on_ltp_update(self, data):
        if data.get("type") != "market_data" or data.get("symbol") != self.symbol:
            return

        self.last_tick_ts = time.monotonic()   # feed-health heartbeat
        if self.feed_stale:
            print(f"\n[FEED] Recovered — market-data ticks resumed")
            self.feed_stale = False

        self.ltp = float(data["data"]["ltp"])
        now = datetime.now().strftime("%H:%M:%S")

        # §14 reverse-confirm SHADOW — resolve any pending shadow each tick (log-only, no trade change).
        # Placed before the flat-position return so it keeps tracking after the reverse exit / while paused.
        if self.shadow_reverse is not None:
            try:
                self._resolve_shadow(self.ltp)
            except Exception:
                pass

        if not self.position or self.exit_in_progress:
            print(f"\r[{now}] LTP: {self.ltp:.2f} | No position | Day P&L: {self.daily_pnl:.2f}    ", end="")
            return

        # Update trailing stop-loss
        if self.position == "BUY":
            if self.ltp > self.peak_price:
                self.peak_price = self.ltp
                self.trailing_sl = round(self.peak_price * (1 - TRAILING_SL_PCT / 100), 2)
            unrealized = (self.ltp - self.entry_price) * QUANTITY
            hit_sl = self.ltp <= self.trailing_sl
        else:
            if self.ltp < self.peak_price:
                self.peak_price = self.ltp
                self.trailing_sl = round(self.peak_price * (1 + TRAILING_SL_PCT / 100), 2)
            unrealized = (self.entry_price - self.ltp) * QUANTITY
            hit_sl = self.ltp >= self.trailing_sl

        sign = "+" if unrealized > 0 else ""
        print(
            f"\r[{now}] LTP: {self.ltp:.2f} | {self.position} @ {self.entry_price:.2f} | "
            f"P&L: {sign}{unrealized:.0f} | TSL: {self.trailing_sl:.2f} | Peak: {self.peak_price:.2f}    ",
            end="",
        )

        # APPE — adaptive profit-protection exit. First-to-fire vs the price trail below.
        if not self.exit_in_progress:
            appe_reason = self._appe_evaluate(unrealized, time.monotonic())
            if appe_reason:
                self.exit_in_progress = True
                threading.Thread(target=self.place_exit, args=(appe_reason,), daemon=True).start()
                return

        if hit_sl and not self.exit_in_progress:
            self.exit_in_progress = True
            print(f"\n[ALERT] Trailing SL hit at {self.ltp:.2f} (SL was {self.trailing_sl:.2f})")
            threading.Thread(target=self.place_exit, args=("TRAILING_SL",), daemon=True).start()

    # -------------------------------------------------------------------------
    # APPE — Adaptive Profit-Protection Exit (see ADAPTIVE_PROFIT_EXIT_DESIGN.md)
    # -------------------------------------------------------------------------

    def _feed_age(self):
        """Seconds since the last WS tick, or None if none received yet."""
        return None if self.last_tick_ts is None else (time.monotonic() - self.last_tick_ts)

    def _feed_ok(self):
        age = self._feed_age()
        return age is not None and age <= FEED_STALE_SEC

    def _check_feed_health(self):
        """During market hours, log ERROR (once per stale episode) if ticks have stopped."""
        if self._feed_ok():
            return
        if not self.feed_stale:
            self.feed_stale = True
            age = self._feed_age()
            age_str = "no ticks yet" if age is None else f"no ticks for {age:.0f}s"
            log_error(
                f"Market-data feed STALE ({age_str}, >{FEED_STALE_SEC:.0f}s) — trailing-SL & "
                f"APPE are INACTIVE; blocking new entries"
                + ("; a POSITION IS OPEN AND UNPROTECTED — consider manual square-off." if self.position else ".")
            )

    def _reset_appe(self):
        self.appe_peak = 0.0
        self.appe_armed = False
        self.appe_breach_start = None
        self.pnl_window.clear()

    def _pnl_slope_negative(self):
        """Linear-regression slope of unrealized P&L over the window; True if drifting down (Gate 3a)."""
        pts = self.pnl_window
        if len(pts) < 5:
            return False
        span = pts[-1][0] - pts[0][0]
        if span < TREND_WINDOW_SEC * 0.5:
            return False  # window doesn't yet cover enough time — don't act without evidence
        n = len(pts)
        t0 = pts[0][0]
        xs = [p[0] - t0 for p in pts]
        ys = [p[1] for p in pts]
        mx = sum(xs) / n
        my = sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0:
            return False
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
        return slope < 0

    def _appe_evaluate(self, unrealized, now):
        """Return an exit reason ('APPE_HARD' / 'APPE_RATCHET') or None. Design doc §4."""
        if not APPE_ENABLED:
            return None

        # Track peak (MFE) and feed/trim the slope window every tick
        if unrealized > self.appe_peak:
            self.appe_peak = unrealized
        self.pnl_window.append((now, unrealized))
        cutoff = now - TREND_WINDOW_SEC
        while self.pnl_window and self.pnl_window[0][0] < cutoff:
            self.pnl_window.popleft()

        # Gate 1 — arm
        if not self.appe_armed:
            if self.appe_peak >= PROFIT_ARM_THRESHOLD:
                self.appe_armed = True
                print(f"\n[APPE] Armed — peak ₹{self.appe_peak:.0f} ≥ arm ₹{PROFIT_ARM_THRESHOLD:.0f}")
            else:
                return None

        budget = GIVEBACK_K * math.sqrt(max(self.appe_peak, 0.0))
        floor = self.appe_peak - budget
        giveback = self.appe_peak - unrealized

        # Gate 4 — catastrophic give-back: exit immediately, skip confirmation
        if giveback >= HARD_MULT * budget:
            print(f"\n[APPE] HARD exit — give-back ₹{giveback:.0f} ≥ {HARD_MULT:g}×budget ₹{budget:.0f} "
                  f"(peak ₹{self.appe_peak:.0f}, U ₹{unrealized:.0f})")
            return "APPE_HARD"

        # Gate 2 — breached the protective floor?
        if unrealized < floor:
            # Gate 3a — only if the profit curve is genuinely drifting down
            if self._pnl_slope_negative():
                # Gate 3b — confirm-and-hold for TREND_CONFIRM_SEC
                if self.appe_breach_start is None:
                    self.appe_breach_start = now
                    print(f"\n[APPE] Breach floor ₹{floor:.0f} (U ₹{unrealized:.0f}, peak ₹{self.appe_peak:.0f}) "
                          f"+ trend down — confirming {TREND_CONFIRM_SEC:.0f}s...")
                elif now - self.appe_breach_start >= TREND_CONFIRM_SEC:
                    print(f"\n[APPE] Confirmed (held {now - self.appe_breach_start:.0f}s) — exit @ U ₹{unrealized:.0f}")
                    return "APPE_RATCHET"
            else:
                # below floor but trend not down (a dip in an up-leg) — don't arm the timer
                self.appe_breach_start = None
        else:
            # recovered above floor — cancel any pending confirmation
            self.appe_breach_start = None

        return None

    # ----- §14 reverse-signal confirmation SHADOW (log-only; never changes trading) -----
    def _arm_shadow_reverse(self, position, entry_price, trigger_price):
        """Called when a reverse-signal exit fires. Records the confirm-stop the §14 filter
        would have used, so the next tick(s) can log whether the reverse followed through."""
        stop = (trigger_price * (1 - REVERSE_CONFIRM_PCT) if position == "BUY"
                else trigger_price * (1 + REVERSE_CONFIRM_PCT))
        self.shadow_reverse = {"dir": position, "entry": entry_price,
                               "trigger": trigger_price, "stop": round(stop, 2)}
        print(f"\n[SHADOW] §14 armed — reverse fired @ {trigger_price:.2f}; confirm-stop @ {stop:.2f} "
              f"(−{REVERSE_CONFIRM_PCT * 100:.2f}% follow-through). Tracking CONFIRM vs NOISE (log-only).")

    def _resolve_shadow(self, price):
        """Each tick: did price follow through to the confirm-stop (filter would also exit) or not
        (filter would have HELD)? Resolves on confirm-stop hit or at EOD. Pure logging."""
        sr = self.shadow_reverse
        if sr is None:
            return
        d, entry, trig, stop = sr["dir"], sr["entry"], sr["trigger"], sr["stop"]
        rpnl = (trig - entry) * QUANTITY if d == "BUY" else (entry - trig) * QUANTITY
        confirmed = price <= stop if d == "BUY" else price >= stop
        if confirmed:
            spnl = (stop - entry) * QUANTITY if d == "BUY" else (entry - stop) * QUANTITY
            print(f"\n[SHADOW] §14 CONFIRMED — price hit confirm-stop {stop:.2f}; filter would have exited "
                  f"~₹{spnl:.0f} (vs actual reverse ~₹{rpnl:.0f}). Filter ≈ no edge this time.")
            self.shadow_reverse = None
            return
        now = datetime.now()
        if now.hour > 15 or (now.hour == 15 and now.minute >= 14):
            hpnl = (price - entry) * QUANTITY if d == "BUY" else (entry - price) * QUANTITY
            print(f"\n[SHADOW] §14 HELD — never reached confirm-stop {stop:.2f}; reverse was noise. "
                  f"Hold-to-EOD mark ~₹{hpnl:.0f} (vs actual reverse ~₹{rpnl:.0f}). Filter would have AVOIDED the exit.")
            self.shadow_reverse = None

    def start_websocket(self):
        while not self.stop_event.is_set():
            try:
                self.client.connect()
                self.client.subscribe_ltp(self.instrument, on_data_received=self.on_ltp_update)
                print(f"[WS] Connected — monitoring {self.symbol}")
                while not self.stop_event.is_set():
                    time.sleep(1)
            except Exception as e:
                log_error(f"WebSocket connection error: {e}")
            finally:
                try:
                    self.client.unsubscribe_ltp(self.instrument)
                    self.client.disconnect()
                except Exception:
                    pass
            if not self.stop_event.is_set():
                print("[WS] Reconnecting in 5s...")
                time.sleep(5)

    # -------------------------------------------------------------------------
    # Data + Signal
    # -------------------------------------------------------------------------

    def get_data(self):
        try:
            end = datetime.now()
            start = end - timedelta(days=LOOKBACK_DAYS)
            data = self.client.history(
                symbol=self.symbol, exchange=EXCHANGE, interval=CANDLE_TIMEFRAME,
                start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d"),
            )
            if data is not None and len(data) > 0:
                return data
        except Exception as e:
            print(f"\n[DATA ERROR] {e}")
        return None

    def check_signal(self, df):
        if df is None or len(df) < SLOW_EMA + VOLUME_SMA_PERIOD:
            return None

        df = df.copy()
        df["ema_fast"] = df["close"].ewm(span=FAST_EMA, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=SLOW_EMA, adjust=False).mean()
        df["vol_sma"] = df["volume"].rolling(window=VOLUME_SMA_PERIOD).mean()

        prev = df.iloc[-3]
        last = df.iloc[-2]   # completed candle (not partial)

        vol_ok = last["volume"] > VOLUME_FILTER_MULT * last["vol_sma"] if last["vol_sma"] > 0 else False

        print(
            f"\n[SIGNAL CHECK] EMA({FAST_EMA}): {last['ema_fast']:.2f} | "
            f"EMA({SLOW_EMA}): {last['ema_slow']:.2f} | "
            f"Vol: {last['volume']:.0f} vs {VOLUME_FILTER_MULT}x SMA: {last['vol_sma'] * VOLUME_FILTER_MULT:.0f} | "
            f"Vol OK: {vol_ok}"
        )

        # Bullish crossover
        if prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
            if vol_ok and TRADE_DIRECTION in ("LONG", "BOTH"):
                print("[SIGNAL] BUY — EMA fast crossed above slow with volume confirmation")
                return "BUY"
            elif not vol_ok:
                print("[SIGNAL] BUY crossover detected but volume filter not met — skipping")

        # Bearish crossover
        if prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
            if vol_ok and TRADE_DIRECTION in ("SHORT", "BOTH"):
                print("[SIGNAL] SELL — EMA fast crossed below slow with volume confirmation")
                return "SELL"
            elif not vol_ok:
                print("[SIGNAL] SELL crossover detected but volume filter not met — skipping")

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
                        print(f"[ORDER] {d.get('order_status')}: {d.get('status_message', '')}")
                        return None
            except Exception as e:
                print(f"[ORDER STATUS ERROR] {e}")
        return None

    def place_entry(self, signal):
        if self.daily_pnl <= -MAX_LOSS_PER_DAY:
            print(f"[CIRCUIT BREAKER] Daily loss {self.daily_pnl:.0f} exceeds limit {MAX_LOSS_PER_DAY} — no new trades")
            return False

        # Feed-health guard: never enter without a live tick feed (no trailing-SL/APPE otherwise)
        if not self._feed_ok():
            age = self._feed_age()
            age_str = "no ticks yet" if age is None else f"no ticks for {age:.0f}s"
            log_error(f"Skipping {signal} entry — market-data feed stale ({age_str}); "
                      f"refusing to trade blind without trailing-SL/APPE protection.")
            return False

        if self.position and self.position != signal:
            # §14 SHADOW (log-only): record what the reverse-confirm filter would have done.
            # Snapshot entry_price now — place_exit() resets it. Never affects the exit below.
            try:
                self._arm_shadow_reverse(self.position, self.entry_price, self.ltp)
            except Exception:
                pass
            print(f"[REVERSE] Closing {self.position} before entering {signal}")
            self.place_exit("REVERSE_SIGNAL")
            time.sleep(1)

        if self.position:
            return False

        print(f"\n[ENTRY] Placing {signal} for {QUANTITY} qty of {self.symbol}")
        try:
            resp = self.client.placeorder(
                strategy=STRATEGY_NAME, symbol=self.symbol, exchange=EXCHANGE,
                action=signal, quantity=QUANTITY, price_type="MARKET", product=PRODUCT,
            )
            if resp.get("status") == "success":
                order_id = resp.get("orderid")
                print(f"[ENTRY] Order placed: {order_id}")
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
                    print(f"[ENTRY] Filled @ {price:.2f} | TSL: {self.trailing_sl:.2f} | Trade #{self.trade_count}")
                    return True
                log_error(f"ENTRY {signal} placed (order {order_id}) but fill price NOT confirmed — position state uncertain.")
            else:
                log_error(f"ENTRY {signal} order REJECTED/failed: {resp}")
        except Exception as e:
            log_error(f"ENTRY {signal} exception: {e}")
        return False

    def place_exit(self, reason="Manual"):
        if not self.position:
            self.exit_in_progress = False
            return

        # Snapshot position state BEFORE placing order. The sync_position poll
        # runs concurrently and resets self.position / self.entry_price the
        # moment the analyzer reflects the closed position. Without the snapshot,
        # the P&L calculation below reads entry_price=0 and produces garbage
        # like -3,302,640 which then poisons daily_pnl and trips the daily-loss
        # circuit breaker for the rest of the day.
        position = self.position
        entry_price = self.entry_price

        exit_action = "SELL" if position == "BUY" else "BUY"
        print(f"\n[EXIT] Closing {position} — reason: {reason}")

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
                    print(f"[EXIT] Filled @ {exit_price:.2f} | P&L: {sign}{pnl:.0f} | Day total: {self.daily_pnl:.0f}")
                else:
                    log_error(f"EXIT {position} ({reason}) placed (order {order_id}) but fill NOT confirmed — verify square-off manually.")

                self.position = None
                self.entry_price = 0.0
                self.trailing_sl = 0.0
                self.peak_price = 0.0
                self._reset_appe()
                self.exit_in_progress = False
                self.save_state()
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
            positions = resp.get("data", [])
            net_qty = 0
            for p in positions:
                if p.get("symbol") == self.symbol and p.get("product") == PRODUCT:
                    net_qty += int(p.get("quantity", 0))
            if net_qty == 0 and self.position:
                print(f"\n[SYNC] Position gone (manual exit?) — resetting from {self.position}")
                self.position = None
                self.entry_price = 0.0
                self.trailing_sl = 0.0
                self.peak_price = 0.0
                self._reset_appe()
                self.exit_in_progress = False
                self.save_state()
        except Exception as e:
            print(f"[SYNC ERROR] {e}")

    # -------------------------------------------------------------------------
    # Strategy Loop
    # -------------------------------------------------------------------------

    def strategy_loop(self):
        print("[STRATEGY] Loop started")
        while not self.stop_event.is_set():
            try:
                now = datetime.now()

                if now.hour < 9 or (now.hour == 9 and now.minute < 15):
                    time.sleep(30)
                    continue
                if now.hour >= 15 and now.minute >= 14:
                    if self.position:
                        print("\n[EOD] 15:14 — closing position for end of day")
                        self.place_exit("EOD_SQUAREOFF")
                    self.clear_state()
                    if now.minute >= 19:
                        print(f"\n[EOD] Post-squareoff — strategy finished for the day.")
                        self.running = False
                        self.stop_event.set()
                        return
                    time.sleep(60)
                    continue

                if self.daily_pnl <= -MAX_LOSS_PER_DAY:
                    if self.position:
                        self.place_exit("DAILY_LOSS_LIMIT")
                    print(f"\r[PAUSED] Daily loss limit hit: {self.daily_pnl:.0f}    ", end="")
                    time.sleep(60)
                    continue

                # Feed-health watchdog (market hours): logs ERROR if ticks have stopped
                self._check_feed_health()

                if self.position:
                    self.sync_position()

                if not self.position and not self.exit_in_progress:
                    df = self.get_data()
                    signal = self.check_signal(df)
                    if signal:
                        self.place_entry(signal)
                elif self.position and not self.exit_in_progress:
                    df = self.get_data()
                    signal = self.check_signal(df)
                    if signal and signal != self.position:
                        self.exit_in_progress = True
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
        print("=" * 65)
        print(f"  EMA({FAST_EMA}/{SLOW_EMA}) CROSSOVER — {self.symbol} {CANDLE_TIMEFRAME}")
        print(f"  Volume filter: >{VOLUME_FILTER_MULT}x SMA({VOLUME_SMA_PERIOD})")
        print(f"  Trailing SL: {TRAILING_SL_PCT}% | Max daily loss: {MAX_LOSS_PER_DAY}")
        print(f"  Direction: {TRADE_DIRECTION} | Qty: {QUANTITY} | Product: {PRODUCT}")
        print("=" * 65)

        ws_t = threading.Thread(target=self.start_websocket, daemon=True)
        ws_t.start()
        time.sleep(2)

        strat_t = threading.Thread(target=self.strategy_loop, daemon=True)
        strat_t.start()

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n[SHUTDOWN] Stopping bot...")
            self.running = False
            self.stop_event.set()
            if self.position and not self.exit_in_progress:
                self.place_exit("SHUTDOWN")
            ws_t.join(timeout=5)
            strat_t.join(timeout=5)
            print(f"[SHUTDOWN] Done. Trades: {self.trade_count} | Day P&L: {self.daily_pnl:.0f}")


if __name__ == "__main__":
    bot = EMACrossoverBot()
    bot.run()
