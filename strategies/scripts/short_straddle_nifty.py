#!/usr/bin/env python
"""
9:20 AM Short Straddle — NIFTY Index Options
=============================================
Sells ATM Call + Put at 9:20 AM, monitors P&L for early exit.

Entry    : 09:20 IST — Sell ATM CE + PE (MIS, 75 qty each)
Condition: India VIX < threshold (default 25%)
Monitor  : If total P&L > 60% of premium collected → early exit
Stop-loss: If total loss > configured % of premium → exit
Auto Exit: 15:15 IST square-off (before MIS deadline)

Run standalone:
    export OPENALGO_API_KEY="your-api-key"
    python short_straddle_nifty.py

Run via OpenAlgo /python strategy runner:
    Upload this file, set exchange=NFO, schedule 09:15-15:20 Mon-Fri.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta
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

UNDERLYING = os.getenv("UNDERLYING", "NIFTY")
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NFO"))
INDEX_EXCHANGE = os.getenv("INDEX_EXCHANGE", "NSE_INDEX")
LOT_SIZE = int(os.getenv("LOT_SIZE", "65"))
LOTS = int(os.getenv("LOTS", "2"))          # LIVE-CONSTRAINED 2026-08-03: 6 lots needs Rs4.87L of
# real margin (Zerodha /margins/basket, hedge benefit already applied: SPAN 1.51L + exposure 3.84L)
# but only Rs2.0L is funded, so live starts at 2 lots (130 qty, ~Rs1.62L). Exposure margin is ~2% of
# the SHORT legs' notional and does NOT shrink with the wings — that, not SPAN, is the binding limit.
# Paper ran 6 lots because sandbox margin is a simplified model, not the exchange's SPAN+exposure.
QUANTITY = LOT_SIZE * LOTS
PRODUCT = os.getenv("PRODUCT", "MIS")

# Trend filter — skip if opening range breakout signals a trend day
SKIP_TREND_DAY = os.getenv("SKIP_TREND_DAY", "true").lower() == "true"
ORB_MINUTES = int(os.getenv("ORB_MINUTES", "15"))
ORB_BREAKOUT_PCT = float(os.getenv("ORB_BREAKOUT_PCT", "0.5"))

# Gap-open filter — skip if index gaps > threshold from previous close
SKIP_GAP_OPEN = os.getenv("SKIP_GAP_OPEN", "true").lower() == "true"
GAP_THRESHOLD_PCT = float(os.getenv("GAP_THRESHOLD_PCT", "1.0"))

# OTM hedge — converts naked straddle to iron butterfly
ENABLE_HEDGE = os.getenv("ENABLE_HEDGE", "true").lower() == "true"
HEDGE_OFFSET = os.getenv("HEDGE_OFFSET", "OTM8")

# Entry time (HH:MM in IST)
ENTRY_HOUR = int(os.getenv("ENTRY_HOUR", "9"))
ENTRY_MINUTE = int(os.getenv("ENTRY_MINUTE", "35"))

# VIX threshold — skip entry if India VIX > this value
VIX_THRESHOLD = float(os.getenv("VIX_THRESHOLD", "25.0"))
SKIP_VIX_CHECK = os.getenv("SKIP_VIX_CHECK", "false").lower() == "true"

# Skip trading on expiry day (gamma risk)
SKIP_EXPIRY_DAY = os.getenv("SKIP_EXPIRY_DAY", "true").lower() == "true"

# Skip trading on high-volatility event days (RBI, FOMC, CPI, etc.)
SKIP_EVENT_DAYS = os.getenv("SKIP_EVENT_DAYS", "true").lower() == "true"
EVENT_CALENDAR_FILE = Path(os.getenv("EVENT_CALENDAR_FILE",
    str(Path(__file__).parent / "event_calendar.json")))

# P&L targets (as % of total premium collected)
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "25"))    # exit at 25% profit
STOPLOSS_PCT = float(os.getenv("STOPLOSS_PCT", "50"))            # exit at 50% loss
# Short-strike breach: exit the iron fly if the underlying moves >= BREACH_PCT% from the entry
# ATM (directional-move stop — cuts earlier/tighter than the % P&L stop). 0 disables. OUR-SERVER.
BREACH_PCT = float(os.getenv("BREACH_PCT", "0.55"))             # 0.55% of ATM ~= 131pt @ 23900

# Consecutive SL cooldown — skip entry after N consecutive SL days
CONSECUTIVE_SL_LIMIT = int(os.getenv("CONSECUTIVE_SL_LIMIT", "2"))

# Square-off time. MOVED 15:14 -> 15:01 on 2026-08-10 after NSE shortened the session:
# regular trading in stocks and F&O now ends 15:15 (was 15:30), effective 2026-08-03. The old
# 15:14 exit had ~16 min of headroom before a 15:30 close; against a 15:15 close it left ~50
# SECONDS, and a clean 4-leg exit already takes ~7s (2026-08-10) while a transient failure needs
# a full retry cycle (8s x2). 15:01 restores ~14 min of buffer.
# Measured support: on 2026-08-10 the 15:00-15:15 window was the MOST volatile of the session
# (avg NIFTY 1m range 9.6 vs 5.8 midday, max 18.2); our 15:14 exit landed inside a 16.7-range
# minute and paid Rs169 of exit slippage (5x the entry's Rs32). Same-day counterfactual: exiting
# ~15:00 would have netted ~Rs260 MORE than 15:14. Note the earlier 55-day tail study that
# favoured 15:05 was fitted to the OLD 15:30-close session, so its timing no longer transfers —
# the closing turbulence has shifted ~15 min earlier.
SQUAREOFF_HOUR = int(os.getenv("SQUAREOFF_HOUR", "15"))
SQUAREOFF_MINUTE = int(os.getenv("SQUAREOFF_MINUTE", "1"))

# P&L check interval (seconds)
PNL_CHECK_INTERVAL = int(os.getenv("PNL_CHECK_INTERVAL", "5"))

# Feed-health guard: if option quotes stop succeeding for this long while positioned, PT/SL are
# evaluating on stale prices (position effectively unprotected) — log ERROR so we can intervene.
FEED_STALE_SEC = float(os.getenv("FEED_STALE_SEC", "60"))
# Re-emit the STALE ERROR line every N seconds while quotes are still failing, so the warning
# does not get buried under hours of PNL output in long-running logs.
FEED_STALE_REWARN_SEC = float(os.getenv("FEED_STALE_REWARN_SEC", "60"))

# Quote-fetch resilience: batch the legs into ONE multiquotes call (fewer broker hits near the
# close than 4 separate quotes), and retry a failed fetch a few times with a short backoff to
# ride out transient broker-API timeouts (root cause of the 2026-07-24 stale window).
QUOTE_RETRIES = int(os.getenv("QUOTE_RETRIES", "2"))          # extra attempts after the first
QUOTE_RETRY_SEC = float(os.getenv("QUOTE_RETRY_SEC", "1.0"))  # backoff between attempts

# ENTRY retry (added 2026-08-07 after a live miss): on 08-07 Zerodha's quote API failed mid-order
# ("Failed to fetch LTP for NIFTY ... kt-quotes"), so 2 of 4 legs could not even be resolved into
# symbols. The safety path correctly flattened the 2 that filled, but the whole day was then
# forfeited (entry_done_today is set before the attempt) at a cost of Rs249. A transient broker
# hiccup should not cost a trading day, so we re-attempt — but ONLY for clearly TECHNICAL
# failures, never for genuine rejections (margin / freeze / not-allowed), where retrying would
# just repeat a real problem. Retries stay inside the 09:35-09:39 entry window.
ENTRY_RETRIES = int(os.getenv("ENTRY_RETRIES", "2"))          # extra attempts after the first
ENTRY_RETRY_SEC = float(os.getenv("ENTRY_RETRY_SEC", "8"))    # pause for the broker to recover

# Failure-message classification. Retry ONLY on these; anything unmatched is treated as
# permanent (fail-safe: a missed day is cheaper than blindly repeating a real rejection).
_TRANSIENT_PAT = ("failed to fetch ltp", "error fetching quotes", "api request failed",
                  "request failed", "kt-quotes", "expecting value", "timed out", "timeout",
                  "connection", "temporarily", "try again", "502", "503", "504", "gateway")
_PERMANENT_PAT = ("margin", "insufficient", "not allowed", "disabled", "blocked", "rejected",
                  "freeze", "frozen", "banned", "circuit", "limit exceeded", "not permitted")
# On feed-stale, alert the user on Telegram (TradeBhau) so they can manually Close-All — the
# quote feed going stale can't be prevented (upstream broker), but awareness can. username is the
# linked OpenAlgo account; TG_ALERT_INTERVAL throttles re-alerts during a long stale window.
ALERT_TG_USER = os.getenv("ALERT_TG_USER", "admin")
TG_ALERT_INTERVAL = float(os.getenv("TG_ALERT_INTERVAL", "120"))


def log_error(msg):
    """Emit a clearly-marked, flushed ERROR line for abnormal conditions (greppable)."""
    print(f"\n[ERROR] [{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def log(msg, end="\n"):
    """Timestamped, append-mode log — no raw prints outside logging helpers."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  {msg}", end=end, flush=True)

STRATEGY_NAME = os.getenv("STRATEGY_NAME", "SHORT_STRADDLE_NIFTY")
STRATEGY_TAG = STRATEGY_NAME.replace("/", "_").replace(" ", "_")

STATE_DIR = Path(os.getenv("STATE_DIR", "/root/data/openalgo/strategies/state"))
STATE_FILE = STATE_DIR / f"{STRATEGY_TAG}_state.json"
HISTORY_FILE = STATE_DIR / f"{STRATEGY_TAG}_history.json"

# Slippage log — append-only CSV, one row per leg fill. MARKET orders cross the spread, and
# with 8 option legs a day that drag is the biggest unknown in whether the paper edge survives
# live. Records fill price vs a reference price so the real cost can be measured.
SLIPPAGE_LOG = Path(os.getenv("SLIPPAGE_LOG", "/root/data/openalgo/log/slippage.csv"))
# Margin actually blocked by the broker, snapshotted AT ENTRY. It cannot be read later: by
# EOD-digest time (15:31) the position is closed and the margin already released (utiliseddebits
# back to 0). Our defined-risk formula (wing width x qty - credit) is the position's MAX LOSS,
# which is a different and much smaller number than exchange SPAN+exposure — on 2026-08-06 the
# formula said 22,392 while the broker actually blocked 1,48,271.
MARGIN_LOG = Path(os.getenv("MARGIN_LOG", "/root/data/openalgo/log/margin.csv"))


# =============================================================================
# BOT
# =============================================================================

class ShortStraddleBot:
    def __init__(self):
        self.client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
        self.running = True
        self.stop_event = threading.Event()

        # Feed-health (quote-poll) tracking
        self.last_quote_ts = None   # time.monotonic() of last successful option quote
        self.feed_stale = False
        self.last_stale_warn_ts = 0.0  # time.monotonic() of last STALE log (rate-limits re-warns)
        self.last_tg_alert_ts = 0.0    # time.monotonic() of last Telegram stale-alert (throttle)

        # Position state
        self.is_positioned = False
        self.ce_symbol = None
        self.pe_symbol = None
        self.ce_entry_price = 0.0
        self.pe_entry_price = 0.0
        self.total_premium = 0.0
        self.atm_strike = None          # entry ATM strike — reference for the short-strike breach

        # Hedge leg state (iron butterfly)
        self.hedge_ce_symbol = None
        self.hedge_pe_symbol = None
        self.hedge_ce_price = 0.0
        self.hedge_pe_price = 0.0

        # Real-time LTP tracking
        self.ce_ltp = 0.0
        self.pe_ltp = 0.0
        self.hedge_ce_ltp = 0.0
        self.hedge_pe_ltp = 0.0
        self.exit_in_progress = False
        self.entry_done_today = False
        self._entry_retryable = False

        self.load_state()

        log(f"[INIT] {STRATEGY_NAME}")
        mode = "Iron Butterfly" if ENABLE_HEDGE else "ATM Straddle"
        log(f"[INIT] {UNDERLYING} {mode} | {LOTS} lot(s) x {LOT_SIZE} = {QUANTITY} qty")
        log(f"[INIT] Entry: {ENTRY_HOUR:02d}:{ENTRY_MINUTE:02d} IST | Exit: {SQUAREOFF_HOUR:02d}:{SQUAREOFF_MINUTE:02d} IST")
        log(f"[INIT] VIX threshold: {'disabled' if SKIP_VIX_CHECK else f'< {VIX_THRESHOLD}'}")
        log(f"[INIT] Hedge: {'ON — ' + HEDGE_OFFSET + ' wings (iron butterfly)' if ENABLE_HEDGE else 'OFF (naked straddle)'}")
        log(f"[INIT] Skip expiry day: {'yes' if SKIP_EXPIRY_DAY else 'no'}")
        log(f"[INIT] Profit target: {PROFIT_TARGET_PCT}% | Stop-loss: {STOPLOSS_PCT}%")
        if BREACH_PCT:
            log(f"[INIT] Short-strike breach: exit if {UNDERLYING} moves ≥{BREACH_PCT}% from entry ATM (directional-move cut)")
        if self.is_positioned:
            log(f"[INIT] Resumed position — CE: {self.ce_symbol} @ {self.ce_entry_price:.2f} | PE: {self.pe_symbol} @ {self.pe_entry_price:.2f}")

    # -------------------------------------------------------------------------
    # State persistence
    # -------------------------------------------------------------------------

    def save_state(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            state = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "is_positioned": self.is_positioned,
                "ce_symbol": self.ce_symbol,
                "pe_symbol": self.pe_symbol,
                "ce_entry_price": self.ce_entry_price,
                "pe_entry_price": self.pe_entry_price,
                "total_premium": self.total_premium,
                "entry_done_today": self.entry_done_today,
                "hedge_ce_symbol": self.hedge_ce_symbol,
                "hedge_pe_symbol": self.hedge_pe_symbol,
                "hedge_ce_price": self.hedge_ce_price,
                "hedge_pe_price": self.hedge_pe_price,
                "atm_strike": self.atm_strike,
            }
            STATE_FILE.write_text(json.dumps(state))
            log(f"[STATE] Saved: positioned={self.is_positioned}")
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
            self.is_positioned = state.get("is_positioned", False)
            self.ce_symbol = state.get("ce_symbol")
            self.pe_symbol = state.get("pe_symbol")
            self.ce_entry_price = state.get("ce_entry_price", 0.0)
            self.pe_entry_price = state.get("pe_entry_price", 0.0)
            self.total_premium = state.get("total_premium", 0.0)
            self.entry_done_today = state.get("entry_done_today", False)
            self.hedge_ce_symbol = state.get("hedge_ce_symbol")
            self.hedge_pe_symbol = state.get("hedge_pe_symbol")
            self.atm_strike = state.get("atm_strike")
            self.hedge_ce_price = state.get("hedge_ce_price", 0.0)
            self.hedge_pe_price = state.get("hedge_pe_price", 0.0)
            if self.ce_entry_price > 0:
                self.ce_ltp = self.ce_entry_price
            if self.pe_entry_price > 0:
                self.pe_ltp = self.pe_entry_price
            if self.hedge_ce_price > 0:
                self.hedge_ce_ltp = self.hedge_ce_price
            if self.hedge_pe_price > 0:
                self.hedge_pe_ltp = self.hedge_pe_price
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
    # Trade history — tracks exit reasons across days
    # -------------------------------------------------------------------------

    def record_trade(self, reason, pnl):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            history = []
            if HISTORY_FILE.exists():
                history = json.loads(HISTORY_FILE.read_text())
            history.append({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "reason": reason,
                "pnl": round(pnl, 2),
            })
            history = history[-30:]
            HISTORY_FILE.write_text(json.dumps(history))
        except Exception as e:
            log(f"[HISTORY ERROR] {e}")

    def check_consecutive_sl(self):
        if CONSECUTIVE_SL_LIMIT <= 0:
            return False
        try:
            if not HISTORY_FILE.exists():
                return False
            history = json.loads(HISTORY_FILE.read_text())
            recent = history[-CONSECUTIVE_SL_LIMIT:]
            if len(recent) < CONSECUTIVE_SL_LIMIT:
                return False
            all_sl = all(t.get("reason") == "STOPLOSS" for t in recent)
            if all_sl:
                dates = [t.get("date") for t in recent]
                log(f"[COOLDOWN] Last {CONSECUTIVE_SL_LIMIT} trades were SL hits ({dates}) — skipping today")
                return True
            return False
        except Exception as e:
            log(f"[COOLDOWN ERROR] {e}")
            return False

    # -------------------------------------------------------------------------
    # VIX Check
    # -------------------------------------------------------------------------

    def check_vix(self):
        if SKIP_VIX_CHECK:
            log("[VIX] Check disabled — proceeding with entry")
            return True
        try:
            resp = self.client.quotes(symbol="INDIAVIX", exchange="NSE_INDEX")
            if resp.get("status") == "success":
                vix = float(resp["data"].get("ltp", 0))
                log(f"[VIX] India VIX: {vix:.2f} | Threshold: {VIX_THRESHOLD}")
                if vix > VIX_THRESHOLD:
                    log(f"[VIX] Too high ({vix:.2f} > {VIX_THRESHOLD}) — skipping entry today")
                    return False
                return True
            else:
                log(f"[VIX] Could not fetch VIX: {resp} — proceeding with caution")
                return True
        except Exception as e:
            log(f"[VIX ERROR] {e} — proceeding with caution")
            return True

    # -------------------------------------------------------------------------
    # Expiry-day check
    # -------------------------------------------------------------------------

    def is_expiry_day(self):
        if not SKIP_EXPIRY_DAY:
            return False
        try:
            resp = self.client.expiry(symbol=UNDERLYING, exchange=EXCHANGE, instrumenttype="options")
            if resp.get("status") == "success":
                expiries = resp.get("data", [])
                if expiries:
                    nearest = expiries[0]  # "DD-MMM-YY" format
                    expiry_date = datetime.strptime(nearest, "%d-%b-%y").date()
                    today = datetime.now().date()
                    if expiry_date == today:
                        log(f"[EXPIRY] Today ({today}) is expiry day — skipping straddle (gamma risk)")
                        return True
                    log(f"[EXPIRY] Next expiry: {nearest} | Today: {today} — not expiry day")
            return False
        except Exception as e:
            log(f"[EXPIRY CHECK ERROR] {e} — proceeding with caution")
            return False

    # -------------------------------------------------------------------------
    # Event calendar check
    # -------------------------------------------------------------------------

    def is_event_day(self):
        if not SKIP_EVENT_DAYS:
            return False
        try:
            if not EVENT_CALENDAR_FILE.exists():
                log(f"[EVENT] Calendar not found: {EVENT_CALENDAR_FILE}")
                return False
            cal = json.loads(EVENT_CALENDAR_FILE.read_text())
            today = datetime.now().strftime("%Y-%m-%d")
            for entry in cal.get("events", []):
                if entry.get("date") == today:
                    log(f"[EVENT] Today is a high-volatility event day: {entry.get('event')} — skipping straddle")
                    return True
            log(f"[EVENT] No events today ({today}) — proceeding")
            return False
        except Exception as e:
            log(f"[EVENT CHECK ERROR] {e} — proceeding with caution")
            return False

    # -------------------------------------------------------------------------
    # Gap-open check
    # -------------------------------------------------------------------------

    def check_gap_open(self):
        if not SKIP_GAP_OPEN:
            return False
        try:
            resp = self.client.quotes(symbol=UNDERLYING, exchange=INDEX_EXCHANGE)
            if resp.get("status") != "success":
                log(f"[GAP] Could not fetch quote: {resp} — proceeding")
                return False
            data = resp.get("data", {})
            ltp = float(data.get("ltp", 0))
            prev_close = float(data.get("close", 0) or data.get("prev_close", 0))
            if prev_close <= 0:
                log("[GAP] Previous close not available — proceeding")
                return False
            gap_pct = abs(ltp - prev_close) / prev_close * 100
            direction = "UP" if ltp > prev_close else "DOWN"
            log(f"[GAP] {UNDERLYING}: {prev_close:.2f} → {ltp:.2f} ({direction} {gap_pct:.2f}%) | Threshold: {GAP_THRESHOLD_PCT}%")
            if gap_pct >= GAP_THRESHOLD_PCT:
                log(f"[GAP] Gap {gap_pct:.2f}% exceeds {GAP_THRESHOLD_PCT}% — skipping straddle")
                return True
            return False
        except Exception as e:
            log(f"[GAP CHECK ERROR] {e} — proceeding with caution")
            return False

    # -------------------------------------------------------------------------
    # Trend filter — Opening Range Breakout
    # -------------------------------------------------------------------------

    def check_trend(self):
        if not SKIP_TREND_DAY:
            return False
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            data = self.client.history(
                symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="5m",
                start_date=today, end_date=today,
            )
            if data is None or len(data) < 2:
                log("[TREND] Not enough intraday data — proceeding")
                return False

            orb_candles = data.head(ORB_MINUTES // 5)

            if len(orb_candles) < 1:
                log("[TREND] No ORB candles found — proceeding")
                return False

            orb_high = float(orb_candles["high"].max())
            orb_low = float(orb_candles["low"].min())
            orb_range = orb_high - orb_low
            orb_mid = (orb_high + orb_low) / 2

            current_price = float(data.iloc[-1]["close"])

            breakout_up = (current_price - orb_high) / orb_mid * 100 if current_price > orb_high else 0
            breakout_down = (orb_low - current_price) / orb_mid * 100 if current_price < orb_low else 0
            breakout_pct = max(breakout_up, breakout_down)

            status = "ABOVE" if breakout_up > 0 else "BELOW" if breakout_down > 0 else "INSIDE"
            log(f"[TREND] ORB({ORB_MINUTES}m): {orb_low:.2f} — {orb_high:.2f} (range {orb_range:.0f}pts)")
            log(f"[TREND] Price: {current_price:.2f} | {status} range | Breakout: {breakout_pct:.2f}% | Threshold: {ORB_BREAKOUT_PCT}%")

            if breakout_pct >= ORB_BREAKOUT_PCT:
                log(f"[TREND] ORB breakout {breakout_pct:.2f}% — trend day likely, skipping straddle")
                return True
            return False
        except Exception as e:
            log(f"[TREND CHECK ERROR] {e} — proceeding with caution")
            return False

    # -------------------------------------------------------------------------
    # Entry
    # -------------------------------------------------------------------------

    def get_expiry(self):
        try:
            resp = self.client.expiry(symbol=UNDERLYING, exchange=EXCHANGE, instrumenttype="options")
            if resp.get("status") == "success":
                expiries = resp.get("data", [])
                if expiries:
                    nearest = expiries[0]
                    # API returns "DD-MMM-YY" but optionsmultiorder expects "DDMMMYY"
                    expiry_formatted = nearest.replace("-", "")
                    log(f"[EXPIRY] Nearest: {nearest} -> {expiry_formatted}")
                    return expiry_formatted
            log(f"[EXPIRY] Could not fetch: {resp}")
        except Exception as e:
            log(f"[EXPIRY ERROR] {e}")
        return None

    def place_straddle(self):
        expiry = self.get_expiry()
        if not expiry:
            log("[ENTRY] Cannot determine expiry — aborting")
            return False

        try:
            quote = self.client.quotes(symbol=UNDERLYING, exchange=INDEX_EXCHANGE)
            if quote.get("status") == "success":
                spot = float(quote["data"].get("ltp", 0))
                self.atm_strike = round(spot / 50) * 50   # provisional; overwritten with the ACTUAL sold strike after fill
                log(f"[ENTRY] {UNDERLYING} spot: {spot:.2f} | breach guard ±{BREACH_PCT}% (map drawn after fill)")
            else:
                log_error(f"ENTRY aborted — could not fetch {UNDERLYING} spot (feed issue): {quote}")
                self._entry_retryable = True
                return False
        except Exception as e:
            log_error(f"ENTRY aborted — spot fetch exception (feed issue): {e}")
            self._entry_retryable = True
            return False

        mode = "iron butterfly" if ENABLE_HEDGE else "short straddle"
        self._entry_retryable = False   # set True only for clearly transient broker failures
        log(f"[ENTRY] Placing ATM {mode} — expiry {expiry}, qty {QUANTITY}")

        legs = [
            {"offset": "ATM", "option_type": "CE", "action": "SELL",
             "quantity": QUANTITY, "product": PRODUCT},
            {"offset": "ATM", "option_type": "PE", "action": "SELL",
             "quantity": QUANTITY, "product": PRODUCT},
        ]
        if ENABLE_HEDGE:
            legs.extend([
                {"offset": HEDGE_OFFSET, "option_type": "CE", "action": "BUY",
                 "quantity": QUANTITY, "product": PRODUCT},
                {"offset": HEDGE_OFFSET, "option_type": "PE", "action": "BUY",
                 "quantity": QUANTITY, "product": PRODUCT},
            ])

        try:
            resp = self.client.optionsmultiorder(
                strategy=STRATEGY_NAME,
                underlying=UNDERLYING,
                exchange=INDEX_EXCHANGE,
                expiry_date=expiry,
                legs=legs,
            )

            log(f"[ENTRY] Response: {resp}")

            if resp.get("status") != "success":
                log_error(f"ENTRY straddle order REJECTED/failed — no position taken: {resp}")
                self._entry_retryable = self._is_transient(resp.get("message") or resp)
                return False

            results = resp.get("results", [])
            expected_legs = 4 if ENABLE_HEDGE else 2
            if len(results) < expected_legs:
                log_error(f"ENTRY straddle returned {len(results)}/{expected_legs} legs — partial fill risk: {resp}")
                self._entry_retryable = self._is_transient(results)
                self._capture_leg_symbols(results)
                if self._has_any_tracked_symbol():
                    self.is_positioned = True
                    self.close_straddle("ENTRY_PARTIAL_FAILURE")
                return False

            self._capture_leg_symbols(results)
            failed_results = [r for r in results if r.get("status") != "success"]
            if failed_results:
                log_error(f"ENTRY straddle had failed leg(s) — flattening any successful legs: {failed_results}")
                self._entry_retryable = self._is_transient(failed_results)
                if self._has_any_tracked_symbol():
                    self.is_positioned = True
                    self.close_straddle("ENTRY_PARTIAL_FAILURE")
                return False

            # Match SELL results by option_type
            sell_results = [r for r in results if r.get("action") == "SELL"]
            buy_results = [r for r in results if r.get("action") == "BUY"]
            if len(sell_results) < 2 or (ENABLE_HEDGE and len(buy_results) < 2):
                log_error(f"ENTRY straddle response missing expected leg metadata — flattening any successful legs: {resp}")
                if self._has_any_tracked_symbol():
                    self.is_positioned = True
                    self.close_straddle("ENTRY_PARTIAL_FAILURE")
                return False
            ce_result = next((r for r in sell_results if r.get("option_type") == "CE"), sell_results[0])
            pe_result = next((r for r in sell_results if r.get("option_type") == "PE"), sell_results[1])

            log(f"[ENTRY] CE SELL: {self.ce_symbol} | Order: {ce_result.get('orderid')}")
            log(f"[ENTRY] PE SELL: {self.pe_symbol} | Order: {pe_result.get('orderid')}")

            if ENABLE_HEDGE:
                hedge_ce = next((r for r in buy_results if r.get("option_type") == "CE"), buy_results[0])
                hedge_pe = next((r for r in buy_results if r.get("option_type") == "PE"), buy_results[1])
                log(f"[HEDGE] CE BUY:  {self.hedge_ce_symbol} | Order: {hedge_ce.get('orderid')}")
                log(f"[HEDGE] PE BUY:  {self.hedge_pe_symbol} | Order: {hedge_pe.get('orderid')}")

            time.sleep(3)

            ce_price = self._get_fill_price(ce_result.get("orderid"))
            pe_price = self._get_fill_price(pe_result.get("orderid"))

            if ce_price and pe_price:
                self.ce_entry_price = ce_price
                self.pe_entry_price = pe_price
                gross_premium = (ce_price + pe_price) * QUANTITY

                hedge_cost = 0.0
                if ENABLE_HEDGE:
                    hce_price = self._get_fill_price(hedge_ce.get("orderid"))
                    hpe_price = self._get_fill_price(hedge_pe.get("orderid"))
                    if not hce_price or not hpe_price:
                        log_error("ENTRY hedge fill prices not confirmed — flattening iron fly instead of monitoring incomplete hedge state")
                        self.is_positioned = True
                        self.save_state()
                        self.close_straddle("ENTRY_HEDGE_UNCONFIRMED")
                        return False
                    self.hedge_ce_price = hce_price or 0.0
                    self.hedge_pe_price = hpe_price or 0.0
                    hedge_cost = (self.hedge_ce_price + self.hedge_pe_price) * QUANTITY

                self.total_premium = gross_premium - hedge_cost
                self.is_positioned = True
                self.ce_ltp = ce_price
                self.pe_ltp = pe_price

                log("=" * 65)
                mode = "IRON BUTTERFLY" if ENABLE_HEDGE else "STRADDLE"
                log(f"  {mode} POSITIONED")
                log("=" * 65)
                log(f"  CE SELL: {self.ce_symbol} @ {ce_price:.2f}")
                log(f"  PE SELL: {self.pe_symbol} @ {pe_price:.2f}")
                if ENABLE_HEDGE:
                    log(f"  CE BUY:  {self.hedge_ce_symbol} @ {self.hedge_ce_price:.2f}")
                    log(f"  PE BUY:  {self.hedge_pe_symbol} @ {self.hedge_pe_price:.2f}")
                    log(f"  Gross premium: {gross_premium:.0f} | Hedge cost: {hedge_cost:.0f}")
                log(f"  Net premium collected: {self.total_premium:.0f}")
                log(f"  Profit target ({PROFIT_TARGET_PCT}%): +{self.total_premium * PROFIT_TARGET_PCT / 100:.0f}")
                log(f"  Stop-loss ({STOPLOSS_PCT}%): -{self.total_premium * STOPLOSS_PCT / 100:.0f}")
                # --- short-strike breach: re-center on the ACTUAL sold ATM strike + draw the map ---
                if BREACH_PCT and self.atm_strike:
                    import re
                    def _strike(sym):
                        m = re.search(r"\d{2}[A-Z]{3}\d{2}(\d+)(?:CE|PE)$", sym or "")
                        return int(m.group(1)) if m else None
                    _a = _strike(self.ce_symbol)
                    if _a:
                        self.atm_strike = _a          # authoritative: the strike we actually sold
                    _band = self.atm_strike * BREACH_PCT / 100
                    _lo, _hi = self.atm_strike - _band, self.atm_strike + _band
                    _wlo = _strike(self.hedge_pe_symbol) or (self.atm_strike - 400)
                    _whi = _strike(self.hedge_ce_symbol) or (self.atm_strike + 400)
                    log("  " + "-" * 61)
                    log(f"  Straddle map — breach exits if {UNDERLYING} leaves the band (ATM ±{BREACH_PCT}% ≈ ±{_band:.0f} pts):")
                    log(f"    PE-wing         breach↓         ATM=max-profit         ↓breach         CE-wing")
                    log(f"    BUY {_wlo:.0f} ···· {_lo:.0f} ═══════ {self.atm_strike:.0f} ═══════ {_hi:.0f} ···· BUY {_whi:.0f}")
                    log(f"    → EXIT the fly if {UNDERLYING} < {_lo:.0f}  or  > {_hi:.0f}   (PT +{PROFIT_TARGET_PCT:.0f}% / SL -{STOPLOSS_PCT:.0f}% = backstops)")
                log("=" * 65)
                self.save_state()
                self._notify_entry()
                # Slippage: snapshot the legs right AFTER the fills (we can't do it before —
                # the API resolves ATM/OTM8 into symbols only when the order returns). Taken
                # post-fill so it never delays entry; the reference is therefore a moment
                # later than submission and already includes our own market impact, so treat
                # entry slippage as a conservative LOWER bound. (Exit refs are exact.)
                _ref = self._quote_snapshot([self.ce_symbol, self.pe_symbol,
                                             self.hedge_ce_symbol, self.hedge_pe_symbol])
                self._log_slippage("ENTRY", [
                    (self.ce_symbol, "SELL", self.ce_entry_price, _ref.get(self.ce_symbol), "post-fill-quote"),
                    (self.pe_symbol, "SELL", self.pe_entry_price, _ref.get(self.pe_symbol), "post-fill-quote"),
                ] + ([
                    (self.hedge_ce_symbol, "BUY", self.hedge_ce_price, _ref.get(self.hedge_ce_symbol), "post-fill-quote"),
                    (self.hedge_pe_symbol, "BUY", self.hedge_pe_price, _ref.get(self.hedge_pe_symbol), "post-fill-quote"),
                ] if ENABLE_HEDGE else []))
                self._record_margin()
                return True
            else:
                log_error("ENTRY fill prices not confirmed — flattening any live legs instead of monitoring with invalid P&L thresholds")
                self.is_positioned = True
                self.save_state()
                self.close_straddle("ENTRY_UNCONFIRMED")
                return False

        except Exception as e:
            log(f"[ENTRY ERROR] {e}")
            return False

    def _get_fill_price(self, order_id):
        if not order_id:
            return None
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

    def _capture_leg_symbols(self, results):
        sell_results = [r for r in results if r.get("action") == "SELL"]
        buy_results = [r for r in results if r.get("action") == "BUY"]

        ce_result = next((r for r in sell_results if r.get("option_type") == "CE"), None)
        pe_result = next((r for r in sell_results if r.get("option_type") == "PE"), None)
        if ce_result:
            self.ce_symbol = ce_result.get("symbol")
        if pe_result:
            self.pe_symbol = pe_result.get("symbol")

        if ENABLE_HEDGE:
            hedge_ce = next((r for r in buy_results if r.get("option_type") == "CE"), None)
            hedge_pe = next((r for r in buy_results if r.get("option_type") == "PE"), None)
            if hedge_ce:
                self.hedge_ce_symbol = hedge_ce.get("symbol")
            if hedge_pe:
                self.hedge_pe_symbol = hedge_pe.get("symbol")

    def _tracked_symbols(self):
        return [s for s in [self.ce_symbol, self.pe_symbol, self.hedge_ce_symbol, self.hedge_pe_symbol] if s]

    def _has_any_tracked_symbol(self):
        return any(self._tracked_symbols())

    def get_open_quantities(self):
        try:
            resp = self.client.positionbook()
            if resp.get("status") != "success":
                return None
            quantities = {}
            tracked = set(self._tracked_symbols())
            for p in resp.get("data", []):
                symbol = p.get("symbol")
                if symbol in tracked and p.get("product") == PRODUCT:
                    quantities[symbol] = quantities.get(symbol, 0) + int(p.get("quantity", 0))
            return quantities
        except Exception as e:
            log(f"[SYNC ERROR] Position snapshot failed: {e}")
            return None

    def _tracked_legs_open(self, open_quantities=None):
        quantities = open_quantities if open_quantities is not None else self.get_open_quantities()
        if quantities is None:
            return None
        return {symbol: qty for symbol, qty in quantities.items() if qty != 0}

    def _clear_position_state(self):
        self.is_positioned = False
        self.ce_symbol = None
        self.pe_symbol = None
        self.hedge_ce_symbol = None
        self.hedge_pe_symbol = None
        self.ce_entry_price = 0.0
        self.pe_entry_price = 0.0
        self.total_premium = 0.0
        self.hedge_ce_price = 0.0
        self.hedge_pe_price = 0.0
        self.ce_ltp = 0.0
        self.pe_ltp = 0.0
        self.hedge_ce_ltp = 0.0
        self.hedge_pe_ltp = 0.0
        self.exit_in_progress = False
        self.clear_state()

    def _flatten_leg(self, symbol, label, fallback_action, open_quantities):
        if not symbol:
            return None

        qty = None if open_quantities is None else open_quantities.get(symbol, 0)
        if qty == 0:
            log(f"[EXIT] {label} already flat: {symbol}")
            return None

        action = fallback_action
        quantity = QUANTITY
        if qty is not None:
            action = "BUY" if qty < 0 else "SELL"
            quantity = abs(qty)

        try:
            resp = self.client.placeorder(
                strategy=STRATEGY_NAME, symbol=symbol, exchange="NFO",
                action=action, quantity=quantity, price_type="MARKET", product=PRODUCT,
            )
            if resp.get("status") == "success":
                price = self._get_fill_price(resp.get("orderid"))
                log(f"[EXIT] {label} closed: {symbol} {action} {quantity} @ {price or 'pending'}")
                return price
            log_error(f"EXIT {label} leg FAILED — {symbol} MAY STILL BE OPEN: {resp}")
        except Exception as e:
            log_error(f"EXIT {label} leg exception — {symbol} MAY STILL BE OPEN: {e}")
        return None

    # -------------------------------------------------------------------------
    # Position sync — detect manual exits via web UI
    # -------------------------------------------------------------------------

    def sync_position(self):
        try:
            open_quantities = self.get_open_quantities()
            if open_quantities is None:
                return
            open_legs = self._tracked_legs_open(open_quantities)

            if not open_legs:
                now = datetime.now()
                near_squareoff = (now.hour > SQUAREOFF_HOUR or
                                  (now.hour == SQUAREOFF_HOUR and now.minute >= SQUAREOFF_MINUTE - 2))
                reason = "EOD auto square-off" if near_squareoff else "manual exit?"
                log(f"\n[SYNC] All tracked legs flat ({reason}) — resetting")
                self._clear_position_state()
            else:
                missing = [symbol for symbol in self._tracked_symbols() if open_quantities.get(symbol, 0) == 0]
                if missing:
                    log(f"\n[SYNC] Some tracked legs are already flat: {missing}; remaining open: {open_legs}")
        except Exception as e:
            log(f"[SYNC ERROR] {e}")

    # -------------------------------------------------------------------------
    # P&L Monitor
    # -------------------------------------------------------------------------

    def _tg_notify(self, message):
        """Best-effort TradeBhau alert via the OpenAlgo /telegram/notify API. Fire-and-forget
        on a daemon thread; swallows all errors — must NEVER disrupt the monitor loop or PT/SL."""
        def _send():
            try:
                import httpx

                httpx.post(
                    f"{API_HOST}/api/v1/telegram/notify",
                    json={"apikey": API_KEY, "username": ALERT_TG_USER, "message": message},
                    timeout=8,
                )
            except Exception as e:
                log_error(f"Telegram notify failed (non-fatal): {e}")

        threading.Thread(target=_send, daemon=True).start()

    # -------------------------------------------------------------------------
    # Trade notifications (mode-agnostic — work in LIVE and analyze alike)
    # -------------------------------------------------------------------------
    #
    # These are emitted BY THE STRATEGY rather than by the sandbox fill-event
    # subscriber, because that event (`sandbox.order_filled`) only exists in
    # analyze mode — there is no live fill event, and live trades are not
    # persisted locally (they live in the broker's tradebook). Emitting here uses
    # the strategy's own fill prices and P&L, so it behaves identically in both
    # modes and is the authoritative source for the numbers.

    def _log_slippage(self, phase, legs):
        """Append per-leg slippage rows. `legs` = [(symbol, action, fill, ref, ref_src)].

        cost = money LOST to crossing the spread, in ₹ (positive = worse than reference):
          SELL -> ref - fill   (sold below reference)
          BUY  -> fill - ref   (paid above reference)
        Best-effort and fully guarded — this is measurement only and must never affect
        trading. Called AFTER the orders are done, so it can't delay an entry or exit.
        """
        try:
            import csv as _csv

            SLIPPAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
            new = not SLIPPAGE_LOG.exists()
            now = datetime.now()
            total = 0.0
            with open(SLIPPAGE_LOG, "a", newline="") as f:
                w = _csv.writer(f)
                if new:
                    w.writerow(["date", "time", "strategy", "phase", "symbol", "action",
                                "qty", "fill_price", "ref_price", "ref_source",
                                "slip_per_unit", "slip_rupees"])
                written = 0
                for sym, action, fill, ref, ref_src in legs:
                    if not sym or not fill or not ref:
                        continue
                    written += 1
                    per = (ref - fill) if action.upper() == "SELL" else (fill - ref)
                    cost = per * QUANTITY
                    total += cost
                    w.writerow([now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
                                STRATEGY_NAME, phase, sym, action.upper(), QUANTITY,
                                f"{fill:.2f}", f"{ref:.2f}", ref_src,
                                f"{per:.4f}", f"{cost:.2f}"])
            if written:
                log(f"[SLIPPAGE] {phase}: {total:+.0f} INR across {written} leg(s) "
                    f"(ref={legs[0][4] if legs else 'n/a'})")
            else:
                # e.g. an aborted entry: no fills and/or no reference price -> nothing to measure
                log(f"[SLIPPAGE] {phase}: 0 legs logged (no fill/reference available)")
        except Exception as e:
            log(f"[SLIPPAGE] log skipped (non-fatal): {e}")

    @staticmethod
    def _is_transient(results):
        """True only if EVERY failure looks like a broker/technical hiccup worth retrying.

        Fail-safe by design: an unrecognised message counts as PERMANENT. Missing a day is
        cheaper than re-firing orders into a real rejection (margin, freeze, not-allowed).
        """
        msgs = []
        if isinstance(results, (list, tuple)):
            for r in results:
                msgs.append(str(r.get("message", "")) if isinstance(r, dict) else str(r))
        else:
            msgs.append(str(results))
        msgs = [m.lower() for m in msgs if m]
        if not msgs:
            return False
        for m in msgs:
            if any(p in m for p in _PERMANENT_PAT):
                return False
            if not any(p in m for p in _TRANSIENT_PAT):
                return False      # unrecognised -> treat as permanent
        return True

    def _record_margin(self):
        """Snapshot the margin the broker ACTUALLY blocked, right after entry -> margin.csv.

        Must happen now: by EOD-digest time the position is flat and utiliseddebits is back to
        0, so this can never be recovered retrospectively. Caveat: utiliseddebits is an
        ACCOUNT-level figure — with only the straddle live it is this position's margin, but if
        a second strategy ever trades live concurrently the number would be the combined total.
        """
        try:
            import csv as _csv

            d = (self.client.funds().get("data") or {})
            used = float(d.get("utiliseddebits") or 0)
            if used <= 0:
                return
            MARGIN_LOG.parent.mkdir(parents=True, exist_ok=True)
            new = not MARGIN_LOG.exists()
            with open(MARGIN_LOG, "a", newline="") as f:
                w = _csv.writer(f)
                if new:
                    w.writerow(["date", "strategy", "margin_blocked", "premium", "qty"])
                w.writerow([datetime.now().strftime("%Y-%m-%d"), STRATEGY_NAME,
                            f"{used:.2f}", f"{self.total_premium:.2f}", QUANTITY])
            log(f"[MARGIN] broker blocked ~Rs{used:,.0f} "
                f"(vs defined max-loss Rs{self._utilised_margin() or 0:,.0f})")
        except Exception as e:
            log(f"[MARGIN] snapshot skipped (non-fatal): {e}")

    def _quote_snapshot(self, symbols):
        """LTPs for the given legs in one batched call. Returns {symbol: ltp}."""
        try:
            resp = self.client.multiquotes(
                symbols=[{"symbol": s, "exchange": "NFO"} for s in symbols if s]
            )
            if resp.get("status") != "success":
                return {}
            out = {}
            for item in (resp.get("results") or []):
                sym = item.get("symbol")
                ltp = (item.get("data") or {}).get("ltp")
                if sym and ltp is not None:
                    out[sym] = float(ltp)
            return out
        except Exception:
            return {}

    @staticmethod
    def _md(s):
        """Make interpolated text safe for Telegram's legacy-Markdown parser. A lone `_`
        (e.g. reason 'EOD_SQUAREOFF', or the default STRATEGY_NAME 'SHORT_STRADDLE_NIFTY')
        is read as an italic marker and makes the whole message fail to parse — Telegram
        then re-sends it as plain text, silently dropping every bold marker."""
        return str(s).replace("_", " ")

    @staticmethod
    def _strike_of(sym):
        import re

        m = re.search(r"\d{2}[A-Z]{3}\d{2}(\d+)(?:CE|PE)$", sym or "")
        return int(m.group(1)) if m else None

    def _wing_width(self):
        """Points between the sold ATM and a bought wing, from the ACTUAL leg symbols."""
        if not ENABLE_HEDGE:
            return None
        a_ce, a_pe = self._strike_of(self.ce_symbol), self._strike_of(self.pe_symbol)
        w_ce, w_pe = self._strike_of(self.hedge_ce_symbol), self._strike_of(self.hedge_pe_symbol)
        try:
            return max(abs(w_ce - a_ce), abs(a_pe - w_pe))
        except TypeError:
            return None

    def _utilised_margin(self):
        """Defined-risk margin for the fly = wing width x qty - net credit. Same basis as
        eod_summary / the fill-alert block, so every report agrees."""
        w = self._wing_width()
        if not w or self.total_premium is None:
            return None
        return w * QUANTITY - self.total_premium

    def _roundtrip_charges(self, exits):
        """Zerodha charges for the full 8-leg round trip, from actual entry+exit prices."""
        try:
            import charges as chg

            entries = [
                {"action": "SELL", "quantity": QUANTITY, "price": self.ce_entry_price},
                {"action": "SELL", "quantity": QUANTITY, "price": self.pe_entry_price},
            ]
            if ENABLE_HEDGE:
                entries += [
                    {"action": "BUY", "quantity": QUANTITY, "price": self.hedge_ce_price},
                    {"action": "BUY", "quantity": QUANTITY, "price": self.hedge_pe_price},
                ]
            return chg.charges_from_fills(entries + exits, True)
        except Exception as e:
            log(f"[NOTIFY] charge calc skipped (non-fatal): {e}")
            return None

    def _notify_entry(self):
        """Consolidated ENTRY alert — one message covering all legs."""
        try:
            margin = self._utilised_margin()
            lines = [
                f"🟢 *{self._md(STRATEGY_NAME)}* — POSITIONED",
                f"{'Iron butterfly' if ENABLE_HEDGE else 'Short straddle'} · {UNDERLYING} · "
                f"{LOTS} lot(s) x {LOT_SIZE} = {QUANTITY} qty",
                "─────────────────────",
                f"SELL {self.ce_symbol} @ ₹{self.ce_entry_price:,.2f}",
                f"SELL {self.pe_symbol} @ ₹{self.pe_entry_price:,.2f}",
            ]
            if ENABLE_HEDGE:
                lines += [
                    f"BUY  {self.hedge_ce_symbol} @ ₹{self.hedge_ce_price:,.2f}",
                    f"BUY  {self.hedge_pe_symbol} @ ₹{self.hedge_pe_price:,.2f}",
                ]
            lines.append(f"Premium collected: ₹{self.total_premium:,.0f}")
            if margin:
                lines.append(f"Utilised margin: ~₹{margin:,.0f}")
            if BREACH_PCT and self.atm_strike:
                b = self.atm_strike * BREACH_PCT / 100
                lines.append(
                    f"Breach band: {self.atm_strike - b:,.0f} / {self.atm_strike + b:,.0f}"
                    f"  (ATM {self.atm_strike:,.0f} ±{BREACH_PCT}%)"
                )
            lines.append(
                f"Targets: PT +{PROFIT_TARGET_PCT:.0f}% / SL −{STOPLOSS_PCT:.0f}% · "
                f"EOD {SQUAREOFF_HOUR:02d}:{SQUAREOFF_MINUTE:02d}"
            )
            self._tg_notify("\n".join(lines))
        except Exception as e:
            log(f"[NOTIFY] entry alert skipped (non-fatal): {e}")

    def _notify_exit(self, reason, ce_exit, pe_exit, hce_exit, hpe_exit, total_pnl):
        """Consolidated EXIT alert — realized P&L for the whole structure."""
        try:
            exits = [
                {"action": "BUY", "quantity": QUANTITY, "price": ce_exit},
                {"action": "BUY", "quantity": QUANTITY, "price": pe_exit},
            ]
            if ENABLE_HEDGE:
                exits += [
                    {"action": "SELL", "quantity": QUANTITY, "price": hce_exit},
                    {"action": "SELL", "quantity": QUANTITY, "price": hpe_exit},
                ]
            charges = self._roundtrip_charges(exits)
            net = total_pnl - charges if charges is not None else None
            margin = self._utilised_margin()
            emoji = "🔴" if total_pnl < 0 else "🟢"
            n_legs = 4 if ENABLE_HEDGE else 2
            lines = [
                f"{emoji} *{self._md(STRATEGY_NAME)}* — CLOSED ({self._md(reason)})",
                f"{n_legs} legs · {QUANTITY} qty",
                "─────────────────────",
                f"CE: sold ₹{self.ce_entry_price:,.2f} → bought ₹{ce_exit:,.2f}",
                f"PE: sold ₹{self.pe_entry_price:,.2f} → bought ₹{pe_exit:,.2f}",
            ]
            if margin:
                lines.append(f"Utilised margin: ~₹{margin:,.0f}")
            lines.append(f"Gross: {'+' if total_pnl >= 0 else '−'}₹{abs(total_pnl):,.0f}")
            if charges is not None:
                lines.append(f"Charges: −₹{charges:,.0f}  (Zerodha)")
                lines.append(f"Net: *{'+' if net >= 0 else '−'}₹{abs(net):,.0f}*")
                if margin:
                    lines.append(f"Return on margin: {net / margin * 100:+.1f}%")
            if self.total_premium:
                lines.append(f"({total_pnl / self.total_premium * 100:+.1f}% of premium)")
            self._tg_notify("\n".join(lines))
        except Exception as e:
            log(f"[NOTIFY] exit alert skipped (non-fatal): {e}")

    def monitor_pnl(self):
        log("[MONITOR] P&L monitoring started")

        while not self.stop_event.is_set():
            if not self.is_positioned or self.exit_in_progress:
                time.sleep(PNL_CHECK_INTERVAL)
                continue

            self.sync_position()
            if not self.is_positioned:
                continue

            now = datetime.now()

            # Time-based square-off
            if now.hour > SQUAREOFF_HOUR or (now.hour == SQUAREOFF_HOUR and now.minute >= SQUAREOFF_MINUTE):
                if self.is_positioned and not self.exit_in_progress:
                    self.exit_in_progress = True
                    log(f"\n[EOD] {SQUAREOFF_HOUR:02d}:{SQUAREOFF_MINUTE:02d} — closing straddle")
                    self.close_straddle("EOD_SQUAREOFF")
                continue

            # Fetch current option LTPs — ONE batched multiquotes call for all legs (fewer broker
            # hits near the close), retried a few times to ride out transient timeouts. Any leg
            # not returned keeps its last value; last_quote_ts advances only when ALL legs are
            # fresh (so the stale-guard below fires if the feed is genuinely down).
            legs = [
                s for s in (
                    self.ce_symbol, self.pe_symbol,
                    self.hedge_ce_symbol if ENABLE_HEDGE else None,
                    self.hedge_pe_symbol if ENABLE_HEDGE else None,
                ) if s
            ]
            got = {}
            for attempt in range(QUOTE_RETRIES + 1):
                try:
                    resp = self.client.multiquotes(
                        symbols=[{"symbol": s, "exchange": "NFO"} for s in legs]
                    )
                    if resp.get("status") == "success":
                        # Response shape: {"status","results":[{"symbol","exchange","data":{"ltp",..}}]}
                        for item in (resp.get("results") or []):
                            sym = item.get("symbol")
                            ltp = (item.get("data") or {}).get("ltp")
                            if sym and ltp is not None:
                                got[sym] = float(ltp)
                        if all(s in got for s in legs):
                            break
                except Exception as e:
                    if attempt == 0:
                        log_error(f"Option multiquotes exception: {e}")
                if attempt < QUOTE_RETRIES:
                    time.sleep(QUOTE_RETRY_SEC)

            if self.ce_symbol in got:
                self.ce_ltp = got[self.ce_symbol]
            if self.pe_symbol in got:
                self.pe_ltp = got[self.pe_symbol]
            if ENABLE_HEDGE:
                if self.hedge_ce_symbol in got:
                    self.hedge_ce_ltp = got[self.hedge_ce_symbol]
                if self.hedge_pe_symbol in got:
                    self.hedge_pe_ltp = got[self.hedge_pe_symbol]

            if legs and all(s in got for s in legs):
                self.last_quote_ts = time.monotonic()
                if self.feed_stale:
                    log("\n[FEED] Recovered — option quotes resuming")
                    self.feed_stale = False
                    self.last_stale_warn_ts = 0.0
                    self._tg_notify("✅ Straddle — option-quote feed RECOVERED; monitoring resumed.")

            # Feed-health: PT/SL rely on fresh quotes — if none for FEED_STALE_SEC, position is unprotected.
            # Re-warn every FEED_STALE_REWARN_SEC so the issue stays visible in long-running logs.
            age = None if self.last_quote_ts is None else (time.monotonic() - self.last_quote_ts)
            if age is not None and age > FEED_STALE_SEC:
                now_mono = time.monotonic()
                if not self.feed_stale or (now_mono - self.last_stale_warn_ts) >= FEED_STALE_REWARN_SEC:
                    onset = not self.feed_stale
                    self.feed_stale = True
                    self.last_stale_warn_ts = now_mono
                    log_error(f"Option-quote feed STALE (no successful quote for {age:.0f}s, >{FEED_STALE_SEC:.0f}s) "
                              f"— PT/SL evaluating on stale prices; straddle position is effectively UNPROTECTED. "
                              f"Consider manual square-off.")
                    # Alert the user on Telegram — on onset immediately, then throttled re-alerts
                    # while still stale (so a missed onset ping gets a follow-up).
                    if onset or (now_mono - self.last_tg_alert_ts) >= TG_ALERT_INTERVAL:
                        self.last_tg_alert_ts = now_mono
                        self._tg_notify(
                            f"⚠️ *Straddle feed STALE* (~{age:.0f}s) — option quotes not updating; "
                            f"PT/SL/breach are on stale prices and the position is effectively "
                            f"UNPROTECTED. Consider a manual Close All."
                        )

            # Short legs P&L: profit when prices DROP from entry
            ce_pnl = (self.ce_entry_price - self.ce_ltp) * QUANTITY
            pe_pnl = (self.pe_entry_price - self.pe_ltp) * QUANTITY
            short_pnl = ce_pnl + pe_pnl

            # Hedge legs P&L: profit when prices RISE from entry (we bought them)
            hedge_pnl = 0.0
            if ENABLE_HEDGE:
                hedge_pnl = ((self.hedge_ce_ltp - self.hedge_ce_price) +
                             (self.hedge_pe_ltp - self.hedge_pe_price)) * QUANTITY

            total_pnl = short_pnl + hedge_pnl

            pnl_pct = (total_pnl / self.total_premium * 100) if self.total_premium > 0 else 0
            sign = "+" if total_pnl > 0 else ""

            # Short-strike breach — cut if the underlying moved beyond the band (directional-move stop).
            # Computed BEFORE the heartbeat so the line can show live spot vs breach levels.
            breach_spot, breached = 0.0, False
            if BREACH_PCT and self.atm_strike:
                try:
                    _sq = self.client.quotes(symbol=UNDERLYING, exchange=INDEX_EXCHANGE)
                    breach_spot = float(_sq["data"].get("ltp", 0)) if _sq.get("status") == "success" else 0.0
                except Exception:
                    breach_spot = 0.0
                if breach_spot and not self.exit_in_progress and abs(breach_spot - self.atm_strike) >= self.atm_strike * BREACH_PCT / 100:
                    breached = True
            breach_str = ""
            if BREACH_PCT and self.atm_strike:
                _b = self.atm_strike * BREACH_PCT / 100
                _lo, _hi = self.atm_strike - _b, self.atm_strike + _b
                breach_str = (f" | {UNDERLYING} {breach_spot:.0f} → breach {_lo:.0f}/{_hi:.0f}"
                              if breach_spot else f" | breach {_lo:.0f}/{_hi:.0f}")

            log(
                f"\r[{now.strftime('%H:%M:%S')}] "
                f"CE: {self.ce_ltp:.2f} | PE: {self.pe_ltp:.2f} | "
                f"Net P&L: {sign}{total_pnl:.0f} ({sign}{pnl_pct:.1f}%)"
                f"{f' [short:{short_pnl:+.0f} hedge:{hedge_pnl:+.0f}]' if ENABLE_HEDGE else ''}"
                f"{breach_str}    ",
                end="",
            )

            # Profit target hit
            if pnl_pct >= PROFIT_TARGET_PCT and not self.exit_in_progress:
                self.exit_in_progress = True
                log(f"\n[TARGET] Profit {pnl_pct:.1f}% >= {PROFIT_TARGET_PCT}% — closing straddle")
                threading.Thread(target=self.close_straddle, args=("PROFIT_TARGET",), daemon=True).start()

            # Short-strike breach — directional-move cut (tighter/earlier than the % stop)
            elif breached:
                self.exit_in_progress = True
                log(f"\n[BREACH] {UNDERLYING} {breach_spot:.0f} moved ≥{BREACH_PCT:.2f}% from ATM {self.atm_strike:.0f} — closing straddle")
                threading.Thread(target=self.close_straddle, args=("BREACH",), daemon=True).start()

            # Stop-loss hit (% of premium) — backstop
            elif pnl_pct <= -STOPLOSS_PCT and not self.exit_in_progress:
                self.exit_in_progress = True
                log(f"\n[STOPLOSS] Loss {pnl_pct:.1f}% exceeds -{STOPLOSS_PCT}% — closing straddle")
                threading.Thread(target=self.close_straddle, args=("STOPLOSS",), daemon=True).start()


            time.sleep(PNL_CHECK_INTERVAL)

    # -------------------------------------------------------------------------
    # Exit
    # -------------------------------------------------------------------------

    def close_straddle(self, reason="Manual"):
        if not self.is_positioned:
            self.exit_in_progress = False
            return

        mode = "iron butterfly" if ENABLE_HEDGE else "straddle"
        log(f"\n[EXIT] Closing {mode} — reason: {reason}")

        open_quantities = self.get_open_quantities()
        open_legs = self._tracked_legs_open(open_quantities)
        if open_legs == {} and reason.startswith("ENTRY_") and self._has_any_tracked_symbol():
            for _ in range(3):
                time.sleep(1)
                open_quantities = self.get_open_quantities()
                open_legs = self._tracked_legs_open(open_quantities)
                if open_legs:
                    break
        if open_legs == {}:
            log("[EXIT] Positionbook already confirms all tracked legs are flat")
            self.record_trade(reason, 0.0)
            self._clear_position_state()
            return

        ce_exit = pe_exit = hedge_ce_exit = hedge_pe_exit = None

        ce_exit = self._flatten_leg(self.ce_symbol, "CE", "BUY", open_quantities)
        pe_exit = self._flatten_leg(self.pe_symbol, "PE", "BUY", open_quantities)

        if ENABLE_HEDGE:
            hedge_ce_exit = self._flatten_leg(self.hedge_ce_symbol, "HEDGE CE", "SELL", open_quantities)
            hedge_pe_exit = self._flatten_leg(self.hedge_pe_symbol, "HEDGE PE", "SELL", open_quantities)

        confirmed_quantities = self.get_open_quantities()
        remaining_open = self._tracked_legs_open(confirmed_quantities)
        if remaining_open is None:
            log_error("EXIT could not confirm final positionbook state — preserving strategy state for retry/manual intervention")
            self.exit_in_progress = False
            self.save_state()
            return
        if remaining_open:
            log_error(f"EXIT incomplete — remaining open legs: {remaining_open}; preserving state for retry/manual intervention")
            self.exit_in_progress = False
            self.save_state()
            return

        ce_pnl = (self.ce_entry_price - (ce_exit or self.ce_ltp)) * QUANTITY
        pe_pnl = (self.pe_entry_price - (pe_exit or self.pe_ltp)) * QUANTITY
        hedge_pnl = 0.0
        if ENABLE_HEDGE:
            hedge_pnl = (((hedge_ce_exit or self.hedge_ce_ltp) - self.hedge_ce_price) +
                         ((hedge_pe_exit or self.hedge_pe_ltp) - self.hedge_pe_price)) * QUANTITY
        total_pnl = ce_pnl + pe_pnl + hedge_pnl
        sign = "+" if total_pnl > 0 else ""

        log("=" * 65)
        log(f"  {mode.upper()} CLOSED")
        log("=" * 65)
        log(f"  Reason: {reason}")
        log(f"  CE: sold @ {self.ce_entry_price:.2f}, bought @ {ce_exit or self.ce_ltp:.2f} -> {'+' if ce_pnl > 0 else ''}{ce_pnl:.0f}")
        log(f"  PE: sold @ {self.pe_entry_price:.2f}, bought @ {pe_exit or self.pe_ltp:.2f} -> {'+' if pe_pnl > 0 else ''}{pe_pnl:.0f}")
        if ENABLE_HEDGE:
            log(f"  Hedge P&L: {'+' if hedge_pnl > 0 else ''}{hedge_pnl:.0f}")
        prem_pct = total_pnl / self.total_premium * 100 if self.total_premium > 0 else 0
        log(f"  Total P&L: {sign}{total_pnl:.0f} ({sign}{prem_pct:.1f}% of premium)")
        log("=" * 65)

        # Slippage: the monitor loop stops polling once exit_in_progress is set, so the stored
        # LTPs are frozen at the prices the exit DECISION was made on — an exact reference for
        # "we decided to exit at X, we actually got Y". Only legs that really filled are logged.
        self._log_slippage("EXIT", [
            (self.ce_symbol, "BUY", ce_exit, self.ce_ltp, "decision-ltp"),
            (self.pe_symbol, "BUY", pe_exit, self.pe_ltp, "decision-ltp"),
        ] + ([
            (self.hedge_ce_symbol, "SELL", hedge_ce_exit, self.hedge_ce_ltp, "decision-ltp"),
            (self.hedge_pe_symbol, "SELL", hedge_pe_exit, self.hedge_pe_ltp, "decision-ltp"),
        ] if ENABLE_HEDGE else []))

        # Alert BEFORE clearing state — _notify_exit reads entry prices/premium/strikes.
        self._notify_exit(reason, ce_exit or self.ce_ltp, pe_exit or self.pe_ltp,
                          hedge_ce_exit if ENABLE_HEDGE else None,
                          hedge_pe_exit if ENABLE_HEDGE else None, total_pnl)
        self.record_trade(reason, total_pnl)
        self._clear_position_state()

    # -------------------------------------------------------------------------
    # Main Loop
    # -------------------------------------------------------------------------

    def run(self):
        log("=" * 65)
        mode = "IRON BUTTERFLY" if ENABLE_HEDGE else "SHORT STRADDLE"
        log(f"  9:20 AM {mode} — {UNDERLYING}")
        log(f"  {LOTS} lot(s) x {LOT_SIZE} = {QUANTITY} qty | Product: {PRODUCT}")
        if ENABLE_HEDGE:
            log(f"  Hedge: {HEDGE_OFFSET} wings (capped max loss)")
        log(f"  VIX threshold: {'disabled' if SKIP_VIX_CHECK else f'< {VIX_THRESHOLD}'}")
        log(f"  Skip expiry day: {'yes' if SKIP_EXPIRY_DAY else 'no'}")
        log(f"  Profit target: {PROFIT_TARGET_PCT}% | Stop-loss: {STOPLOSS_PCT}%")
        log(f"  Entry: {ENTRY_HOUR:02d}:{ENTRY_MINUTE:02d} | Exit: {SQUAREOFF_HOUR:02d}:{SQUAREOFF_MINUTE:02d}")
        log("=" * 65)

        # Startup heads-up: surface the deterministic no-trade reasons NOW (consecutive-SL
        # cooldown, expiry day, event day are all knowable at init) instead of only at the
        # entry-time check. Logging only — the authoritative decision still runs in the entry
        # window below, where the live-data gates (gap / VIX / trend) are also evaluated.
        try:
            if self.check_consecutive_sl():
                _startup_skip = "consecutive-SL cooldown"
            elif self.is_expiry_day():
                _startup_skip = "expiry day (gamma risk)"
            elif self.is_event_day():
                _startup_skip = "high-volatility event day"
            else:
                _startup_skip = None
            if _startup_skip:
                log(f"[INIT] *** NO TRADE TODAY — {_startup_skip}. Straddle will skip the "
                      f"{ENTRY_HOUR:02d}:{ENTRY_MINUTE:02d} entry. ***")
            else:
                log(f"[INIT] No startup-known blocker — entry still subject to live "
                      f"gap/VIX/trend checks at {ENTRY_HOUR:02d}:{ENTRY_MINUTE:02d}.")
        except Exception as _e:
            log(f"[INIT] startup no-trade pre-check skipped (non-fatal): {_e}")

        log("Waiting for entry time...")

        monitor_t = threading.Thread(target=self.monitor_pnl, daemon=True)
        monitor_t.start()

        try:
            while self.running:
                now = datetime.now()

                if (not self.entry_done_today
                        and not self.is_positioned
                        and now.hour == ENTRY_HOUR
                        and now.minute >= ENTRY_MINUTE
                        and now.minute < ENTRY_MINUTE + 5):

                    self.entry_done_today = True

                    if self.check_consecutive_sl():
                        log("[SKIP] Consecutive SL cooldown — no trade today")
                    elif self.is_expiry_day():
                        log("[SKIP] Expiry day — no trade today")
                    elif self.is_event_day():
                        log("[SKIP] Event day — no trade today")
                    elif self.check_gap_open():
                        log("[SKIP] Gap open too large — no trade today")
                    elif not self.check_vix():
                        log("[SKIP] VIX too high — no trade today")
                    elif self.check_trend():
                        log("[SKIP] Trend day (ORB breakout) — no trade today")
                    else:
                        # Re-attempt on transient broker failures only, and only while still
                        # inside the entry window. Each failed attempt has already flattened
                        # any legs that filled, so every retry starts from a clean flat state.
                        for _try in range(ENTRY_RETRIES + 1):
                            if self.place_straddle():
                                break
                            if not self._entry_retryable:
                                log("[ENTRY] failure is not transient — no retry")
                                break
                            if _try >= ENTRY_RETRIES:
                                log_error(f"[ENTRY] still failing after {ENTRY_RETRIES + 1} "
                                          f"attempts — giving up for today")
                                break
                            _n = datetime.now()
                            if not (_n.hour == ENTRY_HOUR and _n.minute < ENTRY_MINUTE + 5):
                                log_error("[ENTRY] entry window closed — abandoning retries")
                                break
                            log(f"[ENTRY] transient broker failure — retry "
                                f"{_try + 1}/{ENTRY_RETRIES} in {ENTRY_RETRY_SEC:.0f}s")
                            time.sleep(ENTRY_RETRY_SEC)

                if now.hour >= 16:
                    self.entry_done_today = False

                if (now.hour > SQUAREOFF_HOUR or
                        (now.hour == SQUAREOFF_HOUR and now.minute >= SQUAREOFF_MINUTE + 5)):
                    if not self.is_positioned:
                        log("\n[EOD] Post-squareoff — strategy finished for the day.")
                        self.running = False
                        self.stop_event.set()
                        break

                time.sleep(1)

        except KeyboardInterrupt:
            log("\n\n[SHUTDOWN] Stopping bot...")
            self.running = False
            self.stop_event.set()
            if self.is_positioned and not self.exit_in_progress:
                self.close_straddle("SHUTDOWN")
            monitor_t.join(timeout=5)
            log("[SHUTDOWN] Done.")


if __name__ == "__main__":
    bot = ShortStraddleBot()
    bot.run()
