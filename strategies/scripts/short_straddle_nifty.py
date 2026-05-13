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

UNDERLYING = os.getenv("UNDERLYING", "NIFTY")
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NFO"))
INDEX_EXCHANGE = os.getenv("INDEX_EXCHANGE", "NSE_INDEX")
LOT_SIZE = int(os.getenv("LOT_SIZE", "65"))
LOTS = int(os.getenv("LOTS", "4"))
QUANTITY = LOT_SIZE * LOTS
PRODUCT = os.getenv("PRODUCT", "MIS")

# Entry time (HH:MM in IST)
ENTRY_HOUR = int(os.getenv("ENTRY_HOUR", "9"))
ENTRY_MINUTE = int(os.getenv("ENTRY_MINUTE", "20"))

# VIX threshold — skip entry if India VIX > this value
VIX_THRESHOLD = float(os.getenv("VIX_THRESHOLD", "25.0"))
SKIP_VIX_CHECK = os.getenv("SKIP_VIX_CHECK", "false").lower() == "true"

# Skip trading on expiry day (gamma risk)
SKIP_EXPIRY_DAY = os.getenv("SKIP_EXPIRY_DAY", "true").lower() == "true"

# P&L targets (as % of total premium collected)
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "60"))    # exit at 60% profit
STOPLOSS_PCT = float(os.getenv("STOPLOSS_PCT", "60"))            # exit at 100% loss

# Square-off time
SQUAREOFF_HOUR = int(os.getenv("SQUAREOFF_HOUR", "15"))
SQUAREOFF_MINUTE = int(os.getenv("SQUAREOFF_MINUTE", "15"))

# P&L check interval (seconds)
PNL_CHECK_INTERVAL = int(os.getenv("PNL_CHECK_INTERVAL", "5"))

STRATEGY_NAME = os.getenv("STRATEGY_NAME", "SHORT_STRADDLE_NIFTY")

STATE_DIR = Path(os.getenv("STATE_DIR", "/root/data/openalgo/strategies/state"))
STATE_FILE = STATE_DIR / f"{STRATEGY_NAME}_state.json"


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

        # Real-time LTP tracking
        self.ce_ltp = 0.0
        self.pe_ltp = 0.0
        self.exit_in_progress = False
        self.entry_done_today = False

        self.load_state()

        print(f"[INIT] {STRATEGY_NAME}")
        print(f"[INIT] {UNDERLYING} ATM Straddle | {LOTS} lot(s) x {LOT_SIZE} = {QUANTITY} qty")
        print(f"[INIT] Entry: {ENTRY_HOUR:02d}:{ENTRY_MINUTE:02d} IST | Exit: {SQUAREOFF_HOUR:02d}:{SQUAREOFF_MINUTE:02d} IST")
        print(f"[INIT] VIX threshold: {'disabled' if SKIP_VIX_CHECK else f'< {VIX_THRESHOLD}'}")
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

        print(f"[ENTRY] Placing ATM short straddle — expiry {expiry}, qty {QUANTITY}")

        try:
            resp = self.client.optionsmultiorder(
                strategy=STRATEGY_NAME,
                underlying=UNDERLYING,
                exchange=INDEX_EXCHANGE,
                expiry_date=expiry,
                legs=[
                    {
                        "offset": "ATM",
                        "option_type": "CE",
                        "action": "SELL",
                        "quantity": QUANTITY,
                        "product": PRODUCT,
                    },
                    {
                        "offset": "ATM",
                        "option_type": "PE",
                        "action": "SELL",
                        "quantity": QUANTITY,
                        "product": PRODUCT,
                    },
                ],
            )

            print(f"[ENTRY] Response: {resp}")

            if resp.get("status") != "success":
                print(f"[ENTRY FAILED] {resp}")
                return False

            results = resp.get("results", [])
            if len(results) < 2:
                print(f"[ENTRY] Unexpected response: {resp}")
                return False

            # Match results by option_type since order may be rearranged
            ce_result = next((r for r in results if r.get("option_type") == "CE"), results[0])
            pe_result = next((r for r in results if r.get("option_type") == "PE"), results[1])
            self.ce_symbol = ce_result.get("symbol")
            self.pe_symbol = pe_result.get("symbol")

            print(f"[ENTRY] CE: {self.ce_symbol} | Order: {ce_result.get('orderid')}")
            print(f"[ENTRY] PE: {self.pe_symbol} | Order: {pe_result.get('orderid')}")

            time.sleep(3)

            ce_price = self._get_fill_price(ce_result.get("orderid"))
            pe_price = self._get_fill_price(pe_result.get("orderid"))

            if ce_price and pe_price:
                self.ce_entry_price = ce_price
                self.pe_entry_price = pe_price
                self.total_premium = (ce_price + pe_price) * QUANTITY
                self.is_positioned = True
                self.ce_ltp = ce_price
                self.pe_ltp = pe_price

                print("=" * 65)
                print("  STRADDLE POSITIONED")
                print("=" * 65)
                print(f"  CE: {self.ce_symbol} SELL @ {ce_price:.2f}")
                print(f"  PE: {self.pe_symbol} SELL @ {pe_price:.2f}")
                print(f"  Total premium collected: {self.total_premium:.0f}")
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

            # Stop-loss hit
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

        print(f"\n[EXIT] Closing straddle — reason: {reason}")

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

        ce_pnl = (self.ce_entry_price - (ce_exit or self.ce_ltp)) * QUANTITY
        pe_pnl = (self.pe_entry_price - (pe_exit or self.pe_ltp)) * QUANTITY
        total_pnl = ce_pnl + pe_pnl
        sign = "+" if total_pnl > 0 else ""

        print("=" * 65)
        print("  STRADDLE CLOSED")
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
        self.exit_in_progress = False
        self.clear_state()

    # -------------------------------------------------------------------------
    # Main Loop
    # -------------------------------------------------------------------------

    def run(self):
        print("=" * 65)
        print(f"  9:20 AM SHORT STRADDLE — {UNDERLYING}")
        print(f"  {LOTS} lot(s) x {LOT_SIZE} = {QUANTITY} qty | Product: {PRODUCT}")
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

                    if self.is_expiry_day():
                        print("[SKIP] Expiry day — no trade today")
                    elif not self.check_vix():
                        print("[SKIP] VIX too high — no trade today")
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
