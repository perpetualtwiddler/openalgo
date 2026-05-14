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
LOTS = int(os.getenv("LOTS", "4"))
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
HEDGE_OFFSET = os.getenv("HEDGE_OFFSET", "OTM4")

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
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "60"))    # exit at 60% profit
STOPLOSS_PCT = float(os.getenv("STOPLOSS_PCT", "50"))            # exit at 50% loss

# Consecutive SL cooldown — skip entry after N consecutive SL days
CONSECUTIVE_SL_LIMIT = int(os.getenv("CONSECUTIVE_SL_LIMIT", "2"))

# Square-off time
SQUAREOFF_HOUR = int(os.getenv("SQUAREOFF_HOUR", "15"))
SQUAREOFF_MINUTE = int(os.getenv("SQUAREOFF_MINUTE", "15"))

# P&L check interval (seconds)
PNL_CHECK_INTERVAL = int(os.getenv("PNL_CHECK_INTERVAL", "5"))

STRATEGY_NAME = os.getenv("STRATEGY_NAME", "SHORT_STRADDLE_NIFTY")

STATE_DIR = Path(os.getenv("STATE_DIR", "/root/data/openalgo/strategies/state"))
STATE_FILE = STATE_DIR / f"{STRATEGY_NAME}_state.json"
HISTORY_FILE = STATE_DIR / f"{STRATEGY_NAME}_history.json"


# =============================================================================
# BOT
# =============================================================================

class ShortStraddleBot:
    def __init__(self):
        self.client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
        self.running = True
        self.stop_event = threading.Event()

        # Position state
        self.is_positioned = False
        self.ce_symbol = None
        self.pe_symbol = None
        self.ce_entry_price = 0.0
        self.pe_entry_price = 0.0
        self.total_premium = 0.0

        # Hedge leg state (iron butterfly)
        self.hedge_ce_symbol = None
        self.hedge_pe_symbol = None
        self.hedge_ce_price = 0.0
        self.hedge_pe_price = 0.0

        # Real-time LTP tracking
        self.ce_ltp = 0.0
        self.pe_ltp = 0.0
        self.exit_in_progress = False
        self.entry_done_today = False

        self.load_state()

        print(f"[INIT] {STRATEGY_NAME}")
        mode = "Iron Butterfly" if ENABLE_HEDGE else "ATM Straddle"
        print(f"[INIT] {UNDERLYING} {mode} | {LOTS} lot(s) x {LOT_SIZE} = {QUANTITY} qty")
        print(f"[INIT] Entry: {ENTRY_HOUR:02d}:{ENTRY_MINUTE:02d} IST | Exit: {SQUAREOFF_HOUR:02d}:{SQUAREOFF_MINUTE:02d} IST")
        print(f"[INIT] VIX threshold: {'disabled' if SKIP_VIX_CHECK else f'< {VIX_THRESHOLD}'}")
        print(f"[INIT] Hedge: {'ON — ' + HEDGE_OFFSET + ' wings (iron butterfly)' if ENABLE_HEDGE else 'OFF (naked straddle)'}")
        print(f"[INIT] Skip expiry day: {'yes' if SKIP_EXPIRY_DAY else 'no'}")
        print(f"[INIT] Profit target: {PROFIT_TARGET_PCT}% | Stop-loss: {STOPLOSS_PCT}%")
        if self.is_positioned:
            print(f"[INIT] Resumed position — CE: {self.ce_symbol} @ {self.ce_entry_price:.2f} | PE: {self.pe_symbol} @ {self.pe_entry_price:.2f}")

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
            }
            STATE_FILE.write_text(json.dumps(state))
            print(f"[STATE] Saved: positioned={self.is_positioned}")
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
            self.is_positioned = state.get("is_positioned", False)
            self.ce_symbol = state.get("ce_symbol")
            self.pe_symbol = state.get("pe_symbol")
            self.ce_entry_price = state.get("ce_entry_price", 0.0)
            self.pe_entry_price = state.get("pe_entry_price", 0.0)
            self.total_premium = state.get("total_premium", 0.0)
            self.entry_done_today = state.get("entry_done_today", False)
            self.hedge_ce_symbol = state.get("hedge_ce_symbol")
            self.hedge_pe_symbol = state.get("hedge_pe_symbol")
            self.hedge_ce_price = state.get("hedge_ce_price", 0.0)
            self.hedge_pe_price = state.get("hedge_pe_price", 0.0)
            if self.ce_entry_price > 0:
                self.ce_ltp = self.ce_entry_price
            if self.pe_entry_price > 0:
                self.pe_ltp = self.pe_entry_price
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
            print(f"[HISTORY ERROR] {e}")

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
                print(f"[COOLDOWN] Last {CONSECUTIVE_SL_LIMIT} trades were SL hits ({dates}) — skipping today")
                return True
            return False
        except Exception as e:
            print(f"[COOLDOWN ERROR] {e}")
            return False

    # -------------------------------------------------------------------------
    # VIX Check
    # -------------------------------------------------------------------------

    def check_vix(self):
        if SKIP_VIX_CHECK:
            print("[VIX] Check disabled — proceeding with entry")
            return True
        try:
            resp = self.client.quotes(symbol="INDIAVIX", exchange="NSE_INDEX")
            if resp.get("status") == "success":
                vix = float(resp["data"].get("ltp", 0))
                print(f"[VIX] India VIX: {vix:.2f} | Threshold: {VIX_THRESHOLD}")
                if vix > VIX_THRESHOLD:
                    print(f"[VIX] Too high ({vix:.2f} > {VIX_THRESHOLD}) — skipping entry today")
                    return False
                return True
            else:
                print(f"[VIX] Could not fetch VIX: {resp} — proceeding with caution")
                return True
        except Exception as e:
            print(f"[VIX ERROR] {e} — proceeding with caution")
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
                        print(f"[EXPIRY] Today ({today}) is expiry day — skipping straddle (gamma risk)")
                        return True
                    print(f"[EXPIRY] Next expiry: {nearest} | Today: {today} — not expiry day")
            return False
        except Exception as e:
            print(f"[EXPIRY CHECK ERROR] {e} — proceeding with caution")
            return False

    # -------------------------------------------------------------------------
    # Event calendar check
    # -------------------------------------------------------------------------

    def is_event_day(self):
        if not SKIP_EVENT_DAYS:
            return False
        try:
            if not EVENT_CALENDAR_FILE.exists():
                print(f"[EVENT] Calendar not found: {EVENT_CALENDAR_FILE}")
                return False
            cal = json.loads(EVENT_CALENDAR_FILE.read_text())
            today = datetime.now().strftime("%Y-%m-%d")
            for entry in cal.get("events", []):
                if entry.get("date") == today:
                    print(f"[EVENT] Today is a high-volatility event day: {entry.get('event')} — skipping straddle")
                    return True
            print(f"[EVENT] No events today ({today}) — proceeding")
            return False
        except Exception as e:
            print(f"[EVENT CHECK ERROR] {e} — proceeding with caution")
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
                print(f"[GAP] Could not fetch quote: {resp} — proceeding")
                return False
            data = resp.get("data", {})
            ltp = float(data.get("ltp", 0))
            prev_close = float(data.get("close", 0) or data.get("prev_close", 0))
            if prev_close <= 0:
                print("[GAP] Previous close not available — proceeding")
                return False
            gap_pct = abs(ltp - prev_close) / prev_close * 100
            direction = "UP" if ltp > prev_close else "DOWN"
            print(f"[GAP] {UNDERLYING}: {prev_close:.2f} → {ltp:.2f} ({direction} {gap_pct:.2f}%) | Threshold: {GAP_THRESHOLD_PCT}%")
            if gap_pct >= GAP_THRESHOLD_PCT:
                print(f"[GAP] Gap {gap_pct:.2f}% exceeds {GAP_THRESHOLD_PCT}% — skipping straddle")
                return True
            return False
        except Exception as e:
            print(f"[GAP CHECK ERROR] {e} — proceeding with caution")
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
                print("[TREND] Not enough intraday data — proceeding")
                return False

            orb_candles = data.head(ORB_MINUTES // 5)

            if len(orb_candles) < 1:
                print("[TREND] No ORB candles found — proceeding")
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
            print(f"[TREND] ORB({ORB_MINUTES}m): {orb_low:.2f} — {orb_high:.2f} (range {orb_range:.0f}pts)")
            print(f"[TREND] Price: {current_price:.2f} | {status} range | Breakout: {breakout_pct:.2f}% | Threshold: {ORB_BREAKOUT_PCT}%")

            if breakout_pct >= ORB_BREAKOUT_PCT:
                print(f"[TREND] ORB breakout {breakout_pct:.2f}% — trend day likely, skipping straddle")
                return True
            return False
        except Exception as e:
            print(f"[TREND CHECK ERROR] {e} — proceeding with caution")
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
                    print(f"[EXPIRY] Nearest: {nearest} -> {expiry_formatted}")
                    return expiry_formatted
            print(f"[EXPIRY] Could not fetch: {resp}")
        except Exception as e:
            print(f"[EXPIRY ERROR] {e}")
        return None

    def place_straddle(self):
        expiry = self.get_expiry()
        if not expiry:
            print("[ENTRY] Cannot determine expiry — aborting")
            return False

        try:
            quote = self.client.quotes(symbol=UNDERLYING, exchange=INDEX_EXCHANGE)
            if quote.get("status") == "success":
                spot = float(quote["data"].get("ltp", 0))
                print(f"[ENTRY] {UNDERLYING} spot: {spot:.2f}")
            else:
                print(f"[ENTRY] Could not fetch spot: {quote}")
                return False
        except Exception as e:
            print(f"[ENTRY ERROR] Spot fetch: {e}")
            return False

        mode = "iron butterfly" if ENABLE_HEDGE else "short straddle"
        print(f"[ENTRY] Placing ATM {mode} — expiry {expiry}, qty {QUANTITY}")

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

            print(f"[ENTRY] Response: {resp}")

            if resp.get("status") != "success":
                print(f"[ENTRY FAILED] {resp}")
                return False

            results = resp.get("results", [])
            expected_legs = 4 if ENABLE_HEDGE else 2
            if len(results) < expected_legs:
                print(f"[ENTRY] Unexpected response (expected {expected_legs} legs): {resp}")
                return False

            # Match SELL results by option_type
            sell_results = [r for r in results if r.get("action") == "SELL"]
            ce_result = next((r for r in sell_results if r.get("option_type") == "CE"), sell_results[0])
            pe_result = next((r for r in sell_results if r.get("option_type") == "PE"), sell_results[1])
            self.ce_symbol = ce_result.get("symbol")
            self.pe_symbol = pe_result.get("symbol")

            print(f"[ENTRY] CE SELL: {self.ce_symbol} | Order: {ce_result.get('orderid')}")
            print(f"[ENTRY] PE SELL: {self.pe_symbol} | Order: {pe_result.get('orderid')}")

            if ENABLE_HEDGE:
                buy_results = [r for r in results if r.get("action") == "BUY"]
                hedge_ce = next((r for r in buy_results if r.get("option_type") == "CE"), buy_results[0])
                hedge_pe = next((r for r in buy_results if r.get("option_type") == "PE"), buy_results[1])
                self.hedge_ce_symbol = hedge_ce.get("symbol")
                self.hedge_pe_symbol = hedge_pe.get("symbol")
                print(f"[HEDGE] CE BUY:  {self.hedge_ce_symbol} | Order: {hedge_ce.get('orderid')}")
                print(f"[HEDGE] PE BUY:  {self.hedge_pe_symbol} | Order: {hedge_pe.get('orderid')}")

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
                    self.hedge_ce_price = hce_price or 0.0
                    self.hedge_pe_price = hpe_price or 0.0
                    hedge_cost = (self.hedge_ce_price + self.hedge_pe_price) * QUANTITY

                self.total_premium = gross_premium - hedge_cost
                self.is_positioned = True
                self.ce_ltp = ce_price
                self.pe_ltp = pe_price

                print("=" * 65)
                mode = "IRON BUTTERFLY" if ENABLE_HEDGE else "STRADDLE"
                print(f"  {mode} POSITIONED")
                print("=" * 65)
                print(f"  CE SELL: {self.ce_symbol} @ {ce_price:.2f}")
                print(f"  PE SELL: {self.pe_symbol} @ {pe_price:.2f}")
                if ENABLE_HEDGE:
                    print(f"  CE BUY:  {self.hedge_ce_symbol} @ {self.hedge_ce_price:.2f}")
                    print(f"  PE BUY:  {self.hedge_pe_symbol} @ {self.hedge_pe_price:.2f}")
                    print(f"  Gross premium: {gross_premium:.0f} | Hedge cost: {hedge_cost:.0f}")
                print(f"  Net premium collected: {self.total_premium:.0f}")
                print(f"  Profit target ({PROFIT_TARGET_PCT}%): +{self.total_premium * PROFIT_TARGET_PCT / 100:.0f}")
                print(f"  Stop-loss ({STOPLOSS_PCT}%): -{self.total_premium * STOPLOSS_PCT / 100:.0f}")
                print("=" * 65)
                self.save_state()
                return True
            else:
                print("[ENTRY] Could not confirm fill prices — check order book")
                self.is_positioned = True
                return True

        except Exception as e:
            print(f"[ENTRY ERROR] {e}")
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
                        print(f"[ORDER] {d.get('order_status')}: {d.get('status_message', '')}")
                        return None
            except Exception as e:
                print(f"[ORDER STATUS ERROR] {e}")
        return None

    # -------------------------------------------------------------------------
    # Position sync — detect manual exits via web UI
    # -------------------------------------------------------------------------

    def sync_position(self):
        try:
            resp = self.client.positionbook()
            if resp.get("status") != "success":
                return
            positions = resp.get("data", [])
            held_symbols = set()
            for p in positions:
                if p.get("product") == PRODUCT and int(p.get("quantity", 0)) != 0:
                    held_symbols.add(p.get("symbol"))

            ce_gone = self.ce_symbol and self.ce_symbol not in held_symbols
            pe_gone = self.pe_symbol and self.pe_symbol not in held_symbols

            if ce_gone and pe_gone:
                now = datetime.now()
                near_squareoff = (now.hour > SQUAREOFF_HOUR or
                                  (now.hour == SQUAREOFF_HOUR and now.minute >= SQUAREOFF_MINUTE - 2))
                reason = "EOD auto square-off" if near_squareoff else "manual exit?"
                print(f"\n[SYNC] Both legs gone ({reason}) — resetting")
                self.is_positioned = False
                self.ce_symbol = None
                self.pe_symbol = None
                self.hedge_ce_symbol = None
                self.hedge_pe_symbol = None
                self.exit_in_progress = False
                self.clear_state()
        except Exception as e:
            print(f"[SYNC ERROR] {e}")

    # -------------------------------------------------------------------------
    # P&L Monitor
    # -------------------------------------------------------------------------

    def monitor_pnl(self):
        print("[MONITOR] P&L monitoring started")

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
                    print(f"\n[EOD] {SQUAREOFF_HOUR:02d}:{SQUAREOFF_MINUTE:02d} — closing straddle")
                    self.close_straddle("EOD_SQUAREOFF")
                continue

            # Fetch current option LTPs
            try:
                if self.ce_symbol:
                    ce_quote = self.client.quotes(symbol=self.ce_symbol, exchange="NFO")
                    if ce_quote.get("status") == "success":
                        self.ce_ltp = float(ce_quote["data"].get("ltp", self.ce_ltp))

                if self.pe_symbol:
                    pe_quote = self.client.quotes(symbol=self.pe_symbol, exchange="NFO")
                    if pe_quote.get("status") == "success":
                        self.pe_ltp = float(pe_quote["data"].get("ltp", self.pe_ltp))
            except Exception as e:
                print(f"\n[QUOTE ERROR] {e}")
                time.sleep(PNL_CHECK_INTERVAL)
                continue

            # Short position P&L: profit when prices DROP from entry
            ce_pnl = (self.ce_entry_price - self.ce_ltp) * QUANTITY
            pe_pnl = (self.pe_entry_price - self.pe_ltp) * QUANTITY
            total_pnl = ce_pnl + pe_pnl

            pnl_pct = (total_pnl / self.total_premium * 100) if self.total_premium > 0 else 0
            sign = "+" if total_pnl > 0 else ""

            print(
                f"\r[{now.strftime('%H:%M:%S')}] "
                f"CE: {self.ce_ltp:.2f} (entry {self.ce_entry_price:.2f}) | "
                f"PE: {self.pe_ltp:.2f} (entry {self.pe_entry_price:.2f}) | "
                f"P&L: {sign}{total_pnl:.0f} ({sign}{pnl_pct:.1f}%)    ",
                end="",
            )

            # Profit target hit
            if pnl_pct >= PROFIT_TARGET_PCT and not self.exit_in_progress:
                self.exit_in_progress = True
                print(f"\n[TARGET] Profit {pnl_pct:.1f}% >= {PROFIT_TARGET_PCT}% — closing straddle")
                threading.Thread(target=self.close_straddle, args=("PROFIT_TARGET",), daemon=True).start()

            # Stop-loss hit (% of premium)
            elif pnl_pct <= -STOPLOSS_PCT and not self.exit_in_progress:
                self.exit_in_progress = True
                print(f"\n[STOPLOSS] Loss {pnl_pct:.1f}% exceeds -{STOPLOSS_PCT}% — closing straddle")
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
        print(f"\n[EXIT] Closing {mode} — reason: {reason}")

        ce_exit = pe_exit = None

        if self.ce_symbol:
            try:
                resp = self.client.placeorder(
                    strategy=STRATEGY_NAME, symbol=self.ce_symbol, exchange="NFO",
                    action="BUY", quantity=QUANTITY, price_type="MARKET", product=PRODUCT,
                )
                if resp.get("status") == "success":
                    ce_exit = self._get_fill_price(resp.get("orderid"))
                    print(f"[EXIT] CE closed: {self.ce_symbol} @ {ce_exit or 'pending'}")
                else:
                    print(f"[EXIT CE FAILED] {resp}")
            except Exception as e:
                print(f"[EXIT CE ERROR] {e}")

        if self.pe_symbol:
            try:
                resp = self.client.placeorder(
                    strategy=STRATEGY_NAME, symbol=self.pe_symbol, exchange="NFO",
                    action="BUY", quantity=QUANTITY, price_type="MARKET", product=PRODUCT,
                )
                if resp.get("status") == "success":
                    pe_exit = self._get_fill_price(resp.get("orderid"))
                    print(f"[EXIT] PE closed: {self.pe_symbol} @ {pe_exit or 'pending'}")
                else:
                    print(f"[EXIT PE FAILED] {resp}")
            except Exception as e:
                print(f"[EXIT PE ERROR] {e}")

        if ENABLE_HEDGE:
            for sym, label in [(self.hedge_ce_symbol, "HEDGE CE"), (self.hedge_pe_symbol, "HEDGE PE")]:
                if sym:
                    try:
                        resp = self.client.placeorder(
                            strategy=STRATEGY_NAME, symbol=sym, exchange="NFO",
                            action="SELL", quantity=QUANTITY, price_type="MARKET", product=PRODUCT,
                        )
                        if resp.get("status") == "success":
                            hprice = self._get_fill_price(resp.get("orderid"))
                            print(f"[EXIT] {label} closed: {sym} @ {hprice or 'pending'}")
                        else:
                            print(f"[EXIT {label} FAILED] {resp}")
                    except Exception as e:
                        print(f"[EXIT {label} ERROR] {e}")

        ce_pnl = (self.ce_entry_price - (ce_exit or self.ce_ltp)) * QUANTITY
        pe_pnl = (self.pe_entry_price - (pe_exit or self.pe_ltp)) * QUANTITY
        total_pnl = ce_pnl + pe_pnl
        sign = "+" if total_pnl > 0 else ""

        print("=" * 65)
        print(f"  {mode.upper()} CLOSED")
        print("=" * 65)
        print(f"  Reason: {reason}")
        print(f"  CE: sold @ {self.ce_entry_price:.2f}, bought @ {ce_exit or self.ce_ltp:.2f} -> {'+' if ce_pnl > 0 else ''}{ce_pnl:.0f}")
        print(f"  PE: sold @ {self.pe_entry_price:.2f}, bought @ {pe_exit or self.pe_ltp:.2f} -> {'+' if pe_pnl > 0 else ''}{pe_pnl:.0f}")
        prem_pct = total_pnl / self.total_premium * 100 if self.total_premium > 0 else 0
        print(f"  Total P&L: {sign}{total_pnl:.0f} ({sign}{prem_pct:.1f}% of premium)")
        print("=" * 65)

        self.is_positioned = False
        self.ce_symbol = None
        self.pe_symbol = None
        self.hedge_ce_symbol = None
        self.hedge_pe_symbol = None
        self.exit_in_progress = False
        self.record_trade(reason, total_pnl)
        self.clear_state()

    # -------------------------------------------------------------------------
    # Main Loop
    # -------------------------------------------------------------------------

    def run(self):
        print("=" * 65)
        mode = "IRON BUTTERFLY" if ENABLE_HEDGE else "SHORT STRADDLE"
        print(f"  9:20 AM {mode} — {UNDERLYING}")
        print(f"  {LOTS} lot(s) x {LOT_SIZE} = {QUANTITY} qty | Product: {PRODUCT}")
        if ENABLE_HEDGE:
            print(f"  Hedge: {HEDGE_OFFSET} wings (capped max loss)")
        print(f"  VIX threshold: {'disabled' if SKIP_VIX_CHECK else f'< {VIX_THRESHOLD}'}")
        print(f"  Skip expiry day: {'yes' if SKIP_EXPIRY_DAY else 'no'}")
        print(f"  Profit target: {PROFIT_TARGET_PCT}% | Stop-loss: {STOPLOSS_PCT}%")
        print(f"  Entry: {ENTRY_HOUR:02d}:{ENTRY_MINUTE:02d} | Exit: {SQUAREOFF_HOUR:02d}:{SQUAREOFF_MINUTE:02d}")
        print("=" * 65)
        print("Waiting for entry time...")

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
                        print("[SKIP] Consecutive SL cooldown — no trade today")
                    elif self.is_expiry_day():
                        print("[SKIP] Expiry day — no trade today")
                    elif self.is_event_day():
                        print("[SKIP] Event day — no trade today")
                    elif self.check_gap_open():
                        print("[SKIP] Gap open too large — no trade today")
                    elif not self.check_vix():
                        print("[SKIP] VIX too high — no trade today")
                    elif self.check_trend():
                        print("[SKIP] Trend day (ORB breakout) — no trade today")
                    else:
                        self.place_straddle()

                if now.hour >= 16:
                    self.entry_done_today = False

                time.sleep(1)

        except KeyboardInterrupt:
            print("\n\n[SHUTDOWN] Stopping bot...")
            self.running = False
            self.stop_event.set()
            if self.is_positioned and not self.exit_in_progress:
                self.close_straddle("SHUTDOWN")
            monitor_t.join(timeout=5)
            print("[SHUTDOWN] Done.")


if __name__ == "__main__":
    bot = ShortStraddleBot()
    bot.run()
