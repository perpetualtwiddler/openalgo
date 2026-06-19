#!/usr/bin/env python
"""
EMA(7/15) Crossover Strategy — BANKNIFTY 3-Minute (Option 4)
============================================================
Buys/sells BANKNIFTY futures on EMA crossover with volume confirmation.
Distinct 3-min 7/15 variant (run side-by-side; own name/state/logs).

Entry : EMA(7) crosses EMA(15) on 3-min candles, within a 2-candle confirmation window
Gate  : decisive gap ≥0.01% + close vs EMA7 + EMA7(now)>EMA7(3 bars ago) slope + volume>1.5×SMA(10)
Exit  : APPE adaptive profit-protection (arm ₹2,000/lot = ₹4,000) OR dynamic
        pure ATR trailing stop (1.5×ATR(14)) OR reverse crossover — first to fire wins
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

# Feed mode: "quote" subscribes in Quote mode so the WS bus carries traded VOLUME,
# which websocket_proxy/market_data_recorder.py persists for backtesting. The strategy
# itself only needs LTP, and quote payloads are a superset that still include ltp, so
# this is safe to leave on. Set FEED_MODE=ltp to revert to the lighter LTP-only feed.
FEED_MODE = os.getenv("FEED_MODE", "quote").strip().lower()

UNDERLYING = os.getenv("SYMBOL", "BANKNIFTY")
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NFO"))
QUANTITY = int(os.getenv("QUANTITY", "60"))       # 2 lots x 30 units
LOT_SIZE = int(os.getenv("LOT_SIZE", "30"))       # BANKNIFTY futures lot size (confirmed 30)
PRODUCT = os.getenv("PRODUCT", "MIS")

FAST_EMA = int(os.getenv("FAST_EMA", "7"))       # Opt4: 7/15 EMA pair
SLOW_EMA = int(os.getenv("SLOW_EMA", "15"))
SLOPE_BARS = int(os.getenv("SLOPE_BARS", "3"))   # Opt4: EMA7(now) vs EMA7(3 bars ago)
CROSS_CONFIRM_BARS = int(os.getenv("CROSS_CONFIRM_BARS", "2"))  # Opt4: 2-candle confirmation window
CANDLE_TIMEFRAME = os.getenv("CANDLE_TIMEFRAME", "3m")   # Opt4: 3-minute candles
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "3"))

VOLUME_FILTER_MULT = float(os.getenv("VOLUME_FILTER_MULT", "1.5"))
VOLUME_SMA_PERIOD = int(os.getenv("VOLUME_SMA_PERIOD", "10"))   # Opt4: 10×3m ≈ 30-min volume SMA

TRAILING_SL_PCT = float(os.getenv("TRAILING_SL_PCT", "0.5"))  # 0.5%
TIGHT_TSL_THRESHOLD = float(os.getenv("TIGHT_TSL_THRESHOLD", "5000"))  # ₹ unrealized profit at which TSL tightens
TIGHT_TSL_PCT = float(os.getenv("TIGHT_TSL_PCT", "0.25"))             # tighter trailing % once threshold crossed
# TODO (TIGHT_TSL — disabled for now, 2026-06-16): keep Dinesh's tighten-the-TSL implementation
# but DO NOT change the trailing % — TSL stays the original 0.5% (TRAILING_SL_PCT) regardless of
# profit. Whether tightening to TIGHT_TSL_PCT once profit ≥ TIGHT_TSL_THRESHOLD is net-beneficial
# is unproven and it interacts with the APPE arm (₹8k) — validate on tick-replay / forward data
# first. Flip TIGHT_TSL_ENABLED=true to activate. See ADAPTIVE_PROFIT_EXIT_DESIGN.md §16.
TIGHT_TSL_ENABLED = os.getenv("TIGHT_TSL_ENABLED", "false").lower() == "true"

# --- Dynamic ATR-based trailing stop (TSL tuning) ---
# TSL_MODE="atr": trailing distance = ATR_MULT × ATR(ATR_PERIOD) in POINTS, recomputed each
# completed candle — the stop widens on volatile days and tightens on quiet ones. ATR(14) on a
# 3m chart = 42 min of recent range. "percent": classic TRAILING_SL_PCT of price (default —
# unchanged live behaviour). The stop still only RATCHETS (never loosens): a mid-trade ATR spike
# won't widen an already-tightened stop, but a high ATR at entry sets the wide initial distance.
# TIGHT_TSL applies to percent mode only. Validate on tick-replay before enabling live.
# See ADAPTIVE_PROFIT_EXIT_DESIGN.md §17.
TSL_MODE = os.getenv("TSL_MODE", "atr").strip().lower()   # Opt4: pure 1.5×ATR(14) trailing stop ("percent" | "atr")
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_MULT = float(os.getenv("ATR_MULT", "1.5"))

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
ARM_PER_LOT = float(os.getenv("ARM_PER_LOT", "2000"))        # Opt4: APPE arm ₹2,000/lot (₹4,000 @ 60 qty)
_arm_override = os.getenv("PROFIT_ARM_THRESHOLD")
PROFIT_ARM_THRESHOLD = (
    float(_arm_override) if _arm_override else ARM_PER_LOT * (QUANTITY / LOT_SIZE)
)  # ₹ profit before APPE arms (Gate 1)
GIVEBACK_K = float(os.getenv("GIVEBACK_K", "30"))             # give-back budget G = k·√P_max (Gate 2)
# Size-aware scaling: G = k·√P_max·√(units/UNITS_REF). Give-back is a points/volatility property,
# so it grows only √-fast with position size (not linearly). UNITS_REF=2 anchors to the 60-qty
# calibration → factor 1 at 2 units (no change to current behaviour). See design doc §4/§5.
GIVEBACK_REF_UNITS = float(os.getenv("GIVEBACK_REF_UNITS", "2"))
TREND_WINDOW_SEC = float(os.getenv("TREND_WINDOW_SEC", "180"))    # slope lookback (Gate 3a)
TREND_CONFIRM_SEC = float(os.getenv("TREND_CONFIRM_SEC", "30"))   # breach hold / patience (Gate 3b)
HARD_MULT = float(os.getenv("HARD_MULT", "2.0"))             # catastrophic give-back multiple (Gate 4)
# Reverse-signal confirmation SHADOW (design doc §14) — LOG-ONLY, does not change trading.
# On a reverse-signal exit, records what a "confirm the reverse only on price follow-through"
# filter WOULD have decided, to gather real-fidelity evidence on this rare event over time.
REVERSE_CONFIRM_PCT = float(os.getenv("REVERSE_CONFIRM_PCT", "0.0001"))  # Opt4: gap gate 0.01% (~5.7 BANKNIFTY pts) at the cross

# Feed-health guard: if no WS ticks arrive for this long during market hours, the feed is
# considered stale — trailing-SL & APPE (both tick-driven) are inactive, so we log ERROR and
# refuse new entries rather than trade blind (June 3: 0 ticks all day -> unprotected -20,760).
FEED_STALE_SEC = float(os.getenv("FEED_STALE_SEC", "60"))
# Re-emit the STALE ERROR line every N seconds while ticks are still missing, so the warning
# does not get buried under hours of [SIGNAL CHECK] noise in long-running logs.
FEED_STALE_REWARN_SEC = float(os.getenv("FEED_STALE_REWARN_SEC", "60"))
# Feed watchdog: if the WS delivered ticks this run then goes SILENT this long, force a WS
# reconnect (the silent mid-session stall raises no exception, so the normal reconnect never
# fires). 0 disables. Should be > FEED_STALE_SEC so the health-guard warns first.
WS_WATCHDOG_SEC = float(os.getenv("WS_WATCHDOG_SEC", "120"))

TRADE_DIRECTION = os.getenv("TRADE_DIRECTION", "BOTH")
SIGNAL_CHECK_INTERVAL = int(os.getenv("SIGNAL_CHECK_INTERVAL", "10"))

MAX_LOSS_PER_DAY = float(os.getenv("MAX_LOSS_PER_DAY", "5000"))  # daily circuit breaker

STRATEGY_NAME = os.getenv("STRATEGY_NAME", "EMA_7_15_BANKNIFTY_3M_OPT4")
STRATEGY_TAG = STRATEGY_NAME.replace("/", "_").replace(" ", "_")

STATE_DIR = Path(os.getenv("STATE_DIR", "/root/data/openalgo/strategies/state"))
STATE_FILE = STATE_DIR / f"{STRATEGY_TAG}_state.json"

def log_error(msg):
    """Emit a clearly-marked, flushed ERROR line for abnormal conditions (greppable)."""
    print(f"\n[ERROR] [{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def log(msg):
    """Timestamped, append-mode log — no \r, no overwriting, safe outside a TTY."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  {msg}", flush=True)


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

class EMACrossoverBot:
    def __init__(self):
        self.client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
        self.symbol = resolve_futures_symbol(self.client, UNDERLYING, EXCHANGE)
        self.position = None       # "BUY" or "SELL" or None
        self.entry_price = 0.0
        self.trailing_sl = 0.0
        self.peak_price = 0.0      # tracks best price for trailing SL
        self.atr = None            # latest ATR(ATR_PERIOD) in points (completed candle); ATR-TSL mode
        self._last_signal_msg = None  # de-dup [SIGNAL CHECK] log: only emit when content changes
        self._candle_seq = 0          # per-run candle counter (logged as #N to differentiate candles)
        self._candle_marker = None    # detects a new completed candle (its EMAs change at close)
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
        # APPE state (profit-protection trailing exit). appe_peak/appe_armed persist across
        # same-day restarts; breach timer + slope window rebuild from live ticks.
        self.appe_peak = 0.0           # P_max — peak unrealized profit this trade
        self.appe_armed = False
        self.appe_breach_start = None  # time.monotonic() when the floor breach began
        self.pnl_window = deque()      # (monotonic_ts, unrealized) over TREND_WINDOW_SEC
        self.last_tick_ts = None       # time.monotonic() of last on_ltp_update (feed-health)
        self.feed_stale = False        # True while the WS tick feed is stale
        self.shadow_reverse = None     # §14 reverse-confirm SHADOW (log-only); set on a reverse exit
        self.last_stale_warn_ts = 0.0  # time.monotonic() of last STALE log (rate-limits re-warns)
        self.ws_alive = False          # set True on FIRST tick received — distinct from SDK's
                                       # optimistic "[WS] Connected" log which fires on TCP
                                       # connect, before the WebSocket handshake completes.
                                       # First-tick is the only proof the pipeline works.
        self.instrument = [{"exchange": EXCHANGE, "symbol": self.symbol}]

        self.load_state()

        log(f"[INIT] {STRATEGY_NAME}")
        log(f"[INIT] {self.symbol} on {EXCHANGE} | EMA({FAST_EMA}/{SLOW_EMA}) | {CANDLE_TIMEFRAME}")
        log(f"[INIT] Volume filter: >{VOLUME_FILTER_MULT}x SMA({VOLUME_SMA_PERIOD})")
        log(f"[INIT] Signal gate (entry & reverse): cross + |EMA{FAST_EMA}−EMA{SLOW_EMA}|≥{REVERSE_CONFIRM_PCT*100:.2f}% "
              f"+ close vs EMA{FAST_EMA} + EMA{FAST_EMA} slope({SLOPE_BARS}b) + volume")
        if TSL_MODE == "atr":
            log(f"[INIT] Trailing SL: ATR mode — {ATR_MULT}× ATR({ATR_PERIOD}) on {CANDLE_TIMEFRAME} "
                f"(dynamic, ratchet-only) | Max daily loss: {MAX_LOSS_PER_DAY}")
        else:
            log(f"[INIT] Trailing SL: {TRAILING_SL_PCT}% (static) | Max daily loss: {MAX_LOSS_PER_DAY}")
        if APPE_ENABLED:
            log(f"[INIT] APPE on: arm≥₹{PROFIT_ARM_THRESHOLD:.0f} "
                  f"(₹{ARM_PER_LOT:.0f}×{QUANTITY/LOT_SIZE:g} lots) | "
                  f"G={GIVEBACK_K:g}·√peak·√({QUANTITY/LOT_SIZE:g}u/{GIVEBACK_REF_UNITS:g}) | "
                  f"trend {TREND_WINDOW_SEC:.0f}s | confirm {TREND_CONFIRM_SEC:.0f}s | hard ×{HARD_MULT:g}")
        else:
            log("[INIT] APPE off — price trailing-SL only")
        log(f"[INIT] Qty: {QUANTITY} | Product: {PRODUCT} | Direction: {TRADE_DIRECTION} | Feed: {FEED_MODE}")
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
    # WebSocket — real-time price + trailing stop-loss
    # -------------------------------------------------------------------------

    def _trail_distance(self, unrealized):
        """Trailing-stop distance in POINTS from the peak. ATR mode = ATR_MULT × ATR(14)
        (volatility-proportional); percent mode = peak_price × pct/100 (TIGHT-aware).
        Falls back to percent if ATR isn't ready yet (e.g. before enough candles)."""
        if TSL_MODE == "atr" and self.atr and self.atr > 0:
            return ATR_MULT * self.atr
        pct = TIGHT_TSL_PCT if (TIGHT_TSL_ENABLED and unrealized >= TIGHT_TSL_THRESHOLD) else TRAILING_SL_PCT
        return self.peak_price * pct / 100.0

    def _initial_trailing_sl(self, signal, price):
        """Initial stop at entry: price −/+ trailing distance (ATR at entry, else percent)."""
        if TSL_MODE == "atr" and self.atr and self.atr > 0:
            dist = ATR_MULT * self.atr
        else:
            dist = price * TRAILING_SL_PCT / 100.0
        return round(price - dist, 2) if signal == "BUY" else round(price + dist, 2)

    def on_ltp_update(self, data):
        if data.get("type") != "market_data" or data.get("symbol") != self.symbol:
            return

        self.last_tick_ts = time.monotonic()   # feed-health heartbeat
        self.ltp = float(data["data"]["ltp"])
        if not self.ws_alive:
            # First tick ever — definitive end-to-end proof the WS pipeline works.
            # Anything before this point (including the SDK's optimistic "[WS] Connected"
            # log) is just TCP/handshake success and tells us nothing about real data flow.
            self.ws_alive = True
            log(f"[WS] FIRST TICK RECEIVED for {self.symbol} @ {self.ltp:.2f} — pipeline confirmed live")
        if self.feed_stale:
            log("[FEED] Recovered — market-data ticks resumed")
            self.feed_stale = False
            self.last_stale_warn_ts = 0.0

        # §14 reverse-confirm SHADOW — resolve any pending shadow each tick (log-only, no trade change).
        # Placed before the flat-position return so it keeps tracking after the reverse exit / while paused.
        if self.shadow_reverse is not None:
            try:
                self._resolve_shadow(self.ltp)
            except Exception:
                pass

        if not self.position or self.exit_in_progress:
            return

        # Update trailing stop-loss.
        # Unrealized is computed first so the tightening threshold check can
        # use it immediately. Once profit crosses TIGHT_TSL_THRESHOLD the TSL
        # switches to TIGHT_TSL_PCT and never widens back.
        if self.position == "BUY":
            unrealized = (self.ltp - self.entry_price) * QUANTITY
            if self.ltp > self.peak_price:
                self.peak_price = self.ltp
            new_tsl = round(self.peak_price - self._trail_distance(unrealized), 2)
            if new_tsl > self.trailing_sl:  # only tighten, never widen
                self.trailing_sl = new_tsl
            hit_sl = self.ltp <= self.trailing_sl
        else:
            unrealized = (self.entry_price - self.ltp) * QUANTITY
            if self.ltp < self.peak_price:
                self.peak_price = self.ltp
            new_tsl = round(self.peak_price + self._trail_distance(unrealized), 2)
            if new_tsl < self.trailing_sl:  # only tighten, never widen
                self.trailing_sl = new_tsl
            hit_sl = self.ltp >= self.trailing_sl

        sign = "+" if unrealized > 0 else ""
        log(
            f"[LTP] {self.ltp:.2f} | {self.position} @ {self.entry_price:.2f} | "
            f"P&L: {sign}{unrealized:.0f} | TSL: {self.trailing_sl:.2f} | Peak: {self.peak_price:.2f}"
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
            log(f"[ALERT] Trailing SL hit at {self.ltp:.2f} (SL was {self.trailing_sl:.2f})")
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
        """During market hours, log ERROR periodically while ticks have stopped.

        Re-emits every FEED_STALE_REWARN_SEC so the warning stays visible in long
        logs (a one-shot log line gets buried under hours of [SIGNAL CHECK] output).
        """
        if self._feed_ok():
            return
        now = time.monotonic()
        if self.feed_stale and (now - self.last_stale_warn_ts) < FEED_STALE_REWARN_SEC:
            return  # already stale and re-warn window not yet elapsed
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
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False)) / den
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
                log(f"[APPE] Armed — peak ₹{self.appe_peak:.0f} ≥ arm ₹{PROFIT_ARM_THRESHOLD:.0f}")
            else:
                return None

        # size-aware: √-scale the budget by units so give-back-in-points grows √-fast, not linearly
        _units_factor = math.sqrt((QUANTITY / LOT_SIZE) / GIVEBACK_REF_UNITS)
        budget = GIVEBACK_K * math.sqrt(max(self.appe_peak, 0.0)) * _units_factor
        floor = self.appe_peak - budget
        giveback = self.appe_peak - unrealized

        # Gate 4 — catastrophic give-back: exit immediately, skip confirmation
        if giveback >= HARD_MULT * budget:
            log(f"[APPE] HARD exit — give-back ₹{giveback:.0f} ≥ {HARD_MULT:g}×budget ₹{budget:.0f} "
                  f"(peak ₹{self.appe_peak:.0f}, U ₹{unrealized:.0f})")
            return "APPE_HARD"

        # Gate 2 — breached the protective floor?
        if unrealized < floor:
            # Gate 3a — only if the profit curve is genuinely drifting down
            if self._pnl_slope_negative():
                # Gate 3b — confirm-and-hold for TREND_CONFIRM_SEC
                if self.appe_breach_start is None:
                    self.appe_breach_start = now
                    log(f"[APPE] Breach floor ₹{floor:.0f} (U ₹{unrealized:.0f}, peak ₹{self.appe_peak:.0f}) "
                          f"+ trend down — confirming {TREND_CONFIRM_SEC:.0f}s...")
                elif now - self.appe_breach_start >= TREND_CONFIRM_SEC:
                    log(f"[APPE] Confirmed (held {now - self.appe_breach_start:.0f}s) — exit @ U ₹{unrealized:.0f}")
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
                # Quote mode makes the WS bus carry traded volume (recorded for backtesting);
                # the quote payload is a superset of LTP, so on_ltp_update still reads ltp.
                if FEED_MODE == "quote":
                    self.client.subscribe_quote(self.instrument, on_data_received=self.on_ltp_update)
                else:
                    self.client.subscribe_ltp(self.instrument, on_data_received=self.on_ltp_update)
                # NOTE: the SDK logs its own "[WS] Connected" line on TCP connect, BEFORE
                # the WebSocket handshake or any data flow. That line lies when the WS
                # proxy is unreachable. We do NOT mirror it — the only honest "alive"
                # signal is FIRST TICK RECEIVED, logged from on_ltp_update().
                log(f"[WS] Subscribed to {self.symbol} ({FEED_MODE} mode) — waiting for first tick (see [WS] FIRST TICK RECEIVED)...")
                while not self.stop_event.is_set():
                    time.sleep(1)
                    # Feed watchdog: a live feed that goes silent > WS_WATCHDOG_SEC is the mid-session
                    # stall (no exception → normal reconnect never fires). Break to force a fresh
                    # connect+resubscribe via the outer loop, self-healing the stall (preserves position).
                    age = self._feed_age()
                    if WS_WATCHDOG_SEC and self.ws_alive and age is not None and age > WS_WATCHDOG_SEC:
                        log_error(f"[WS WATCHDOG] no ticks for {age:.0f}s (>{WS_WATCHDOG_SEC:.0f}s) after a live feed "
                                  f"— forcing WS reconnect to self-heal the stall")
                        break
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
            log(f"[DATA ERROR] {e}")
        return None

    def check_signal(self, df):
        # Feed-health gate: if the WS tick feed is stale, skip signal evaluation entirely.
        # This keeps the log visibly broken when the feed is broken — otherwise [SIGNAL CHECK]
        # lines look identical to healthy operation while trailing-SL/APPE are dead.
        if not self._feed_ok():
            age = self._feed_age()
            age_str = "no ticks yet" if age is None else f"no ticks for {age:.0f}s"
            log(f"[SIGNAL CHECK SKIPPED] Feed stale ({age_str}) — not evaluating crossover without live ticks")
            return None

        if df is None or len(df) < SLOW_EMA + VOLUME_SMA_PERIOD:
            return None

        df = df.copy()
        df["ema_fast"] = df["close"].ewm(span=FAST_EMA, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=SLOW_EMA, adjust=False).mean()
        df["vol_sma"] = df["volume"].rolling(window=VOLUME_SMA_PERIOD).mean()

        # ATR(ATR_PERIOD) in points for the dynamic ATR trailing stop (TSL_MODE="atr").
        # Wilder's RMA of True Range; read off the completed candle (iloc[-2]), consistent
        # with the EMA reads. Computed only when needed and when OHLC is present.
        if TSL_MODE == "atr" and {"high", "low"}.issubset(df.columns):
            prev_close = df["close"].shift(1)
            tr = pd.concat([df["high"] - df["low"],
                            (df["high"] - prev_close).abs(),
                            (df["low"] - prev_close).abs()], axis=1).max(axis=1)
            df["atr"] = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()

        prev = df.iloc[-3]
        last = df.iloc[-2]   # completed candle (not partial)
        if TSL_MODE == "atr" and "atr" in df.columns:
            self.atr = float(last["atr"])

        # Candle counter: bump when a new completed candle appears (its EMAs/close change at
        # close — stable within a candle across intraday refetches). Logged as #N so consecutive
        # candles are easy to tell apart during log analysis.
        _marker = (last["ema_fast"], last["ema_slow"], last["close"])
        if _marker != self._candle_marker:
            self._candle_seq += 1
            self._candle_marker = _marker

        # Crossover-noise gates (design doc §14/§16) — a bare cross isn't enough. A signal needs:
        #   1. the EMA cross (on the completed candle)
        #   2. a DECISIVE gap: |EMA9-EMA21| >= REVERSE_CONFIRM_PCT x close (~0.05% = ~28 BANKNIFTY pts)
        #   3. price leading the move: close > EMA9 (long) / close < EMA9 (short)
        #   4. EMA9 momentum: EMA9 rising (long) / falling (short), candle-over-candle
        #   5. volume confirmation: volume > VOLUME_FILTER_MULT x SMA  (existing)
        # Same gate drives entries AND reverse-exits (a reverse is just an opposite-direction signal).
        vol_ok = last["volume"] > VOLUME_FILTER_MULT * last["vol_sma"] if last["vol_sma"] > 0 else False
        gap = last["ema_fast"] - last["ema_slow"]                 # >0 bullish, <0 bearish
        min_gap = REVERSE_CONFIRM_PCT * last["close"]             # ~0.05% of price (~28 pts)
        slope9 = last["ema_fast"] - df["ema_fast"].iloc[-(2 + SLOPE_BARS)]   # EMA9 now vs SLOPE_BARS bars ago

        atr_str = (f" | ATR({ATR_PERIOD}): {self.atr:.0f} (TSL {ATR_MULT}×={ATR_MULT*self.atr:.0f}pt)"
                   if (TSL_MODE == "atr" and self.atr) else "")
        sig_msg = (
            f"[SIGNAL CHECK] #{self._candle_seq} | EMA({FAST_EMA}): {last['ema_fast']:.2f} | EMA({SLOW_EMA}): {last['ema_slow']:.2f} | "
            f"gap: {gap:+.1f} (min ±{min_gap:.0f}) | close: {last['close']:.2f} | slope({SLOPE_BARS}b): {slope9:+.1f} | "
            f"Vol: {last['volume']:.0f} vs {VOLUME_FILTER_MULT}x SMA: {last['vol_sma'] * VOLUME_FILTER_MULT:.0f} | "
            f"Vol OK: {vol_ok}{atr_str}"
        )
        # De-dup: only emit when the line content changes (new candle / volume / Vol-OK / ATR).
        # Avoids dozens of identical lines per candle so a crossover is easy to spot.
        if sig_msg != self._last_signal_msg:
            log(sig_msg)
            self._last_signal_msg = sig_msg

        # Crossover detection with optional N-candle confirmation window (CROSS_CONFIRM_BARS).
        # Default 0 = cross must be on the completed candle (original §16 behaviour). With N>0, scan
        # back from the completed candle; the most-recent cross within N candles stays eligible, so a
        # thin cross that turns decisive a candle or two later can still trade (gap/close/slope/vol
        # below are still required on the CURRENT completed candle). Stateless — recomputed each cycle.
        bull_cross = bear_cross = False
        for _k in range(CROSS_CONFIRM_BARS + 1):
            _c = df.iloc[-(2 + _k)]
            _p = df.iloc[-(3 + _k)]
            if _p["ema_fast"] <= _p["ema_slow"] and _c["ema_fast"] > _c["ema_slow"]:
                bull_cross = True; break          # most-recent cross is bullish
            if _p["ema_fast"] >= _p["ema_slow"] and _c["ema_fast"] < _c["ema_slow"]:
                bear_cross = True; break          # most-recent cross is bearish

        # Bullish: cross + decisive gap + close above EMA9 + EMA9 rising + volume
        if bull_cross and TRADE_DIRECTION in ("LONG", "BOTH"):
            fails = []
            if gap < min_gap:                 fails.append(f"thin gap {gap:.1f}<{min_gap:.0f}")
            if last["close"] <= last["ema_fast"]: fails.append("close≤EMA9")
            if slope9 <= 0:                   fails.append(f"EMA9 slope {slope9:+.1f}≤0")
            if not vol_ok:                    fails.append("volume")
            if not fails:
                log("[SIGNAL] BUY — cross + gap≥0.05% + close>EMA9 + EMA9 rising + volume")
                return "BUY"
            log(f"[SIGNAL] BUY crossover REJECTED — {', '.join(fails)}")

        # Bearish: cross + decisive gap + close below EMA9 + EMA9 falling + volume
        if bear_cross and TRADE_DIRECTION in ("SHORT", "BOTH"):
            fails = []
            if -gap < min_gap:                fails.append(f"thin gap {gap:.1f}, |gap|<{min_gap:.0f}")
            if last["close"] >= last["ema_fast"]: fails.append("close≥EMA9")
            if slope9 >= 0:                   fails.append(f"EMA9 slope {slope9:+.1f}≥0")
            if not vol_ok:                    fails.append("volume")
            if not fails:
                log("[SIGNAL] SELL — cross + gap≥0.05% + close<EMA9 + EMA9 falling + volume")
                return "SELL"
            log(f"[SIGNAL] SELL crossover REJECTED — {', '.join(fails)}")

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
        self.trailing_sl = self._initial_trailing_sl(signal, self.entry_price)
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
                    self.trailing_sl = self._initial_trailing_sl(signal, price)
                    self.exit_in_progress = False
                    self.trade_count += 1
                    self.save_state()
                    log(f"[ENTRY] Filled @ {price:.2f} | TSL: {self.trailing_sl:.2f} | Trade #{self.trade_count}")
                    return True
                log_error(f"ENTRY {signal} placed (order {order_id}) but fill price NOT confirmed — reconciling via positionbook before trading further.")
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

        # Snapshot position state BEFORE placing order. The sync_position poll
        # runs concurrently and resets self.position / self.entry_price the
        # moment the analyzer reflects the closed position. Without the snapshot,
        # the P&L calculation below reads entry_price=0 and produces garbage
        # like -3,302,640 which then poisons daily_pnl and trips the daily-loss
        # circuit breaker for the rest of the day.
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
                    log_error(f"EXIT {position} ({reason}) placed (order {order_id}) but fill NOT confirmed — keeping position pending until positionbook confirms flat.")
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
            positions = resp.get("data", [])
            net_qty = 0
            for p in positions:
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

                # Feed-health watchdog (market hours): logs ERROR if ticks have stopped
                self._check_feed_health()

                self.reconcile_pending_entry()
                self.reconcile_pending_exit()

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
                        # If the TSL is already breached at poll time, the
                        # WebSocket thread lost the race to set exit_in_progress.
                        # Exit as TRAILING_SL and skip the reverse entry to
                        # avoid compounding the loss with an immediate re-entry.
                        tsl_hit = (
                            self.ltp is not None and self.trailing_sl > 0 and (
                                (self.position == "BUY" and self.ltp <= self.trailing_sl) or
                                (self.position == "SELL" and self.ltp >= self.trailing_sl)
                            )
                        )
                        if tsl_hit:
                            log(f"[SIGNAL] Reverse signal but TSL already breached "
                                  f"(LTP {self.ltp:.2f} vs TSL {self.trailing_sl:.2f}) — "
                                  f"exiting as TRAILING_SL, skipping reverse entry")
                            self.place_exit("TRAILING_SL")
                        else:
                            # §14 SHADOW (log-only): the live reverse happens HERE (loop), not in
                            # place_entry — arm before the exit, snapshotting the pre-exit state.
                            try:
                                self._arm_shadow_reverse(self.position, self.entry_price, self.ltp)
                            except Exception:
                                pass
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
        log(f"  EMA({FAST_EMA}/{SLOW_EMA}) CROSSOVER — {self.symbol} {CANDLE_TIMEFRAME}")
        log(f"  Volume filter: >{VOLUME_FILTER_MULT}x SMA({VOLUME_SMA_PERIOD})")
        log(f"  Trailing SL: {TRAILING_SL_PCT}% | Max daily loss: {MAX_LOSS_PER_DAY}")
        log(f"  Direction: {TRADE_DIRECTION} | Qty: {QUANTITY} | Product: {PRODUCT}")
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
    bot = EMACrossoverBot()
    bot.run()
