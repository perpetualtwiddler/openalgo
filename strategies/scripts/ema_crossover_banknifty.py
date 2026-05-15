#!/usr/bin/env python
"""
EMA(9/21) Crossover Strategy — BANKNIFTY 5-Minute
==================================================
Buys/sells BANKNIFTY futures on EMA crossover with volume confirmation.

Entry : EMA(9) crosses EMA(21) on 5-min candles
Filter: Volume > 1.5x SMA(20) of volume
Exit  : Trailing stop-loss 0.5% OR reverse crossover signal
Product: MIS (intraday, auto square-off by broker at 3:15 PM)

Run standalone:
    export OPENALGO_API_KEY="your-api-key"
    python ema_crossover_banknifty.py

Run via OpenAlgo /python strategy runner:
    Upload this file, set exchange=NFO, schedule 09:15-15:15 Mon-Fri.
"""

import json
import os
import threading
import time
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
PRODUCT = os.getenv("PRODUCT", "MIS")

FAST_EMA = int(os.getenv("FAST_EMA", "9"))
SLOW_EMA = int(os.getenv("SLOW_EMA", "21"))
CANDLE_TIMEFRAME = os.getenv("CANDLE_TIMEFRAME", "5m")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "3"))

VOLUME_FILTER_MULT = float(os.getenv("VOLUME_FILTER_MULT", "1.5"))
VOLUME_SMA_PERIOD = int(os.getenv("VOLUME_SMA_PERIOD", "20"))

TRAILING_SL_PCT = float(os.getenv("TRAILING_SL_PCT", "0.5"))  # 0.5%

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
        self.instrument = [{"exchange": EXCHANGE, "symbol": self.symbol}]

        self.load_state()

        print(f"[INIT] {STRATEGY_NAME}")
        print(f"[INIT] {self.symbol} on {EXCHANGE} | EMA({FAST_EMA}/{SLOW_EMA}) | {CANDLE_TIMEFRAME}")
        print(f"[INIT] Volume filter: >{VOLUME_FILTER_MULT}x SMA({VOLUME_SMA_PERIOD})")
        print(f"[INIT] Trailing SL: {TRAILING_SL_PCT}% | Max daily loss: {MAX_LOSS_PER_DAY}")
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

        self.ltp = float(data["data"]["ltp"])
        now = datetime.now().strftime("%H:%M:%S")

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

        if hit_sl and not self.exit_in_progress:
            self.exit_in_progress = True
            print(f"\n[ALERT] Trailing SL hit at {self.ltp:.2f} (SL was {self.trailing_sl:.2f})")
            threading.Thread(target=self.place_exit, args=("TRAILING_SL",), daemon=True).start()

    def start_websocket(self):
        while not self.stop_event.is_set():
            try:
                self.client.connect()
                self.client.subscribe_ltp(self.instrument, on_data_received=self.on_ltp_update)
                print(f"[WS] Connected — monitoring {self.symbol}")
                while not self.stop_event.is_set():
                    time.sleep(1)
            except Exception as e:
                print(f"\n[WS ERROR] {e}")
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

        if self.position and self.position != signal:
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
                    if signal == "BUY":
                        self.trailing_sl = round(price * (1 - TRAILING_SL_PCT / 100), 2)
                    else:
                        self.trailing_sl = round(price * (1 + TRAILING_SL_PCT / 100), 2)
                    self.exit_in_progress = False
                    self.trade_count += 1
                    self.save_state()
                    print(f"[ENTRY] Filled @ {price:.2f} | TSL: {self.trailing_sl:.2f} | Trade #{self.trade_count}")
                    return True
                print("[ENTRY] Could not confirm fill price")
            else:
                print(f"[ENTRY FAILED] {resp}")
        except Exception as e:
            print(f"[ENTRY ERROR] {e}")
        return False

    def place_exit(self, reason="Manual"):
        if not self.position:
            self.exit_in_progress = False
            return

        exit_action = "SELL" if self.position == "BUY" else "BUY"
        print(f"\n[EXIT] Closing {self.position} — reason: {reason}")

        try:
            resp = self.client.placeorder(
                strategy=STRATEGY_NAME, symbol=self.symbol, exchange=EXCHANGE,
                action=exit_action, quantity=QUANTITY, price_type="MARKET", product=PRODUCT,
            )
            if resp.get("status") == "success":
                order_id = resp.get("orderid")
                exit_price = self.get_fill_price(order_id)
                if exit_price:
                    if self.position == "BUY":
                        pnl = (exit_price - self.entry_price) * QUANTITY
                    else:
                        pnl = (self.entry_price - exit_price) * QUANTITY
                    self.daily_pnl += pnl
                    sign = "+" if pnl > 0 else ""
                    print(f"[EXIT] Filled @ {exit_price:.2f} | P&L: {sign}{pnl:.0f} | Day total: {self.daily_pnl:.0f}")
                else:
                    print("[EXIT] Order placed but could not confirm fill")

                self.position = None
                self.entry_price = 0.0
                self.trailing_sl = 0.0
                self.peak_price = 0.0
                self.exit_in_progress = False
                self.save_state()
            else:
                print(f"[EXIT FAILED] {resp}")
                self.exit_in_progress = False
        except Exception as e:
            print(f"[EXIT ERROR] {e}")
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
                print(f"\n[STRATEGY ERROR] {e}")
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
