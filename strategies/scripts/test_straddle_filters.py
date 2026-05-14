#!/usr/bin/env python
"""
Test script for short straddle iron butterfly filters.
Run on the trading server during market hours to verify each filter works.

Usage:
    export OPENALGO_API_KEY="your-api-key"
    python test_straddle_filters.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from openalgo import api

API_KEY = os.getenv("OPENALGO_API_KEY", "your-api-key")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
client = api(api_key=API_KEY, host=HOST)

PASS = 0
FAIL = 0


def test(name, fn):
    global PASS, FAIL
    print(f"\n{'='*60}")
    print(f"  TEST: {name}")
    print(f"{'='*60}")
    try:
        result = fn()
        if result:
            PASS += 1
            print(f"  -> PASS")
        else:
            FAIL += 1
            print(f"  -> FAIL")
    except Exception as e:
        FAIL += 1
        print(f"  -> ERROR: {e}")


# -------------------------------------------------------------------------
# 1. VIX Check
# -------------------------------------------------------------------------
def test_vix_check():
    resp = client.quotes(symbol="INDIAVIX", exchange="NSE_INDEX")
    print(f"  Response: {resp}")
    if resp.get("status") != "success":
        print("  Could not fetch VIX")
        return False
    vix = float(resp["data"].get("ltp", 0))
    print(f"  India VIX: {vix:.2f}")
    return vix > 0


# -------------------------------------------------------------------------
# 2. Expiry Fetch + Expiry Day Check
# -------------------------------------------------------------------------
def test_expiry():
    resp = client.expiry(symbol="NIFTY", exchange="NFO", instrumenttype="options")
    print(f"  Response status: {resp.get('status')}")
    if resp.get("status") != "success":
        print(f"  Failed: {resp}")
        return False
    expiries = resp.get("data", [])
    if not expiries:
        print("  No expiries returned")
        return False
    nearest = expiries[0]
    expiry_formatted = nearest.replace("-", "")
    print(f"  Nearest expiry: {nearest} -> {expiry_formatted}")

    expiry_date = datetime.strptime(nearest, "%d-%b-%y").date()
    today = datetime.now().date()
    is_expiry = expiry_date == today
    print(f"  Today ({today}) is expiry day: {is_expiry}")
    return True


# -------------------------------------------------------------------------
# 3. Gap-Open Check
# -------------------------------------------------------------------------
def test_gap_open():
    resp = client.quotes(symbol="NIFTY", exchange="NSE_INDEX")
    print(f"  Response status: {resp.get('status')}")
    if resp.get("status") != "success":
        print(f"  Failed: {resp}")
        return False
    data = resp.get("data", {})
    ltp = float(data.get("ltp", 0))
    prev_close = float(data.get("close", 0) or data.get("prev_close", 0))
    print(f"  LTP: {ltp}, Previous close: {prev_close}")
    if prev_close <= 0:
        print("  Previous close not available — checking available fields:")
        print(f"  Quote fields: {list(data.keys())}")
        return False
    gap_pct = abs(ltp - prev_close) / prev_close * 100
    direction = "UP" if ltp > prev_close else "DOWN"
    print(f"  Gap: {direction} {gap_pct:.2f}%")
    return True


# -------------------------------------------------------------------------
# 4. ORB Trend Filter (History API)
# -------------------------------------------------------------------------
def test_orb_trend():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"  Fetching 5m candles for {today}...")
    data = client.history(
        symbol="NIFTY", exchange="NSE_INDEX", interval="5m",
        start_date=today, end_date=today,
    )
    if data is None or len(data) < 2:
        print(f"  Not enough data returned: {type(data)}, len={0 if data is None else len(data)}")
        return False

    print(f"  Candles returned: {len(data)}")
    print(f"  Columns: {list(data.columns)}")
    print(f"  First candle: {data.iloc[0].to_dict()}")
    print(f"  Index type: {type(data.index[0])}")

    orb_candles = data.head(3)  # first 15 min (3 x 5min)
    orb_high = float(orb_candles["high"].max())
    orb_low = float(orb_candles["low"].min())
    orb_mid = (orb_high + orb_low) / 2
    current = float(data.iloc[-1]["close"])

    breakout_up = (current - orb_high) / orb_mid * 100 if current > orb_high else 0
    breakout_down = (orb_low - current) / orb_mid * 100 if current < orb_low else 0
    breakout_pct = max(breakout_up, breakout_down)
    status = "ABOVE" if breakout_up > 0 else "BELOW" if breakout_down > 0 else "INSIDE"

    print(f"  ORB(15m): {orb_low:.2f} — {orb_high:.2f}")
    print(f"  Current: {current:.2f} | {status} | Breakout: {breakout_pct:.2f}%")
    return True


# -------------------------------------------------------------------------
# 5. Event Calendar
# -------------------------------------------------------------------------
def test_event_calendar():
    cal_path = Path(__file__).parent / "event_calendar.json"
    print(f"  Calendar path: {cal_path}")
    if not cal_path.exists():
        print("  File not found!")
        return False
    cal = json.loads(cal_path.read_text())
    events = cal.get("events", [])
    print(f"  Events loaded: {len(events)}")
    today = datetime.now().strftime("%Y-%m-%d")
    match = [e for e in events if e.get("date") == today]
    if match:
        print(f"  TODAY IS EVENT DAY: {match[0].get('event')}")
    else:
        print(f"  No event today ({today})")
    # Validate all dates parse correctly
    for e in events:
        try:
            datetime.strptime(e["date"], "%Y-%m-%d")
        except ValueError:
            print(f"  Invalid date format: {e}")
            return False
    print("  All dates valid")
    return True


# -------------------------------------------------------------------------
# 6. Iron Butterfly — 4-leg optionsmultiorder
# -------------------------------------------------------------------------
def test_iron_butterfly():
    resp = client.expiry(symbol="NIFTY", exchange="NFO", instrumenttype="options")
    if resp.get("status") != "success":
        print(f"  Cannot fetch expiry: {resp}")
        return False
    expiry = resp["data"][0].replace("-", "")
    print(f"  Expiry: {expiry}")

    qty = 65  # 1 lot only for test
    legs = [
        {"offset": "ATM", "option_type": "CE", "action": "SELL",
         "quantity": qty, "product": "MIS"},
        {"offset": "ATM", "option_type": "PE", "action": "SELL",
         "quantity": qty, "product": "MIS"},
        {"offset": "OTM4", "option_type": "CE", "action": "BUY",
         "quantity": qty, "product": "MIS"},
        {"offset": "OTM4", "option_type": "PE", "action": "BUY",
         "quantity": qty, "product": "MIS"},
    ]

    print(f"  Placing 4-leg iron butterfly (1 lot = {qty} qty)...")
    resp = client.optionsmultiorder(
        strategy="TEST_IRON_FLY",
        underlying="NIFTY",
        exchange="NSE_INDEX",
        expiry_date=expiry,
        legs=legs,
    )
    print(f"  Response: {resp}")

    if resp.get("status") != "success":
        print(f"  Order failed: {resp}")
        return False

    results = resp.get("results", [])
    print(f"  Legs returned: {len(results)}")
    if len(results) != 4:
        print(f"  Expected 4 legs, got {len(results)}")
        return False

    sell_legs = [r for r in results if r.get("action") == "SELL"]
    buy_legs = [r for r in results if r.get("action") == "BUY"]
    print(f"  SELL legs: {len(sell_legs)} | BUY legs: {len(buy_legs)}")

    for r in results:
        status = r.get("status")
        symbol = r.get("symbol")
        action = r.get("action")
        otype = r.get("option_type")
        oid = r.get("orderid")
        err = r.get("message", "")
        print(f"  {action} {otype}: {symbol} | Status: {status} | Order: {oid} {err}")

    all_success = all(r.get("status") == "success" for r in results)
    if not all_success:
        print("  WARNING: Not all legs succeeded!")
        return False

    return True


# -------------------------------------------------------------------------
# 7. Consecutive SL — History file read/write
# -------------------------------------------------------------------------
def test_consecutive_sl():
    tmp_dir = Path(tempfile.mkdtemp())
    hist_file = tmp_dir / "test_history.json"

    # Write fake history with 2 consecutive SL
    history = [
        {"date": "2026-05-11", "reason": "STOPLOSS", "pnl": -5000},
        {"date": "2026-05-12", "reason": "STOPLOSS", "pnl": -52065},
    ]
    hist_file.write_text(json.dumps(history))
    print(f"  Wrote test history: {history}")

    # Read back and check
    loaded = json.loads(hist_file.read_text())
    recent = loaded[-2:]
    all_sl = all(t.get("reason") == "STOPLOSS" for t in recent)
    print(f"  Last 2 trades all SL: {all_sl}")
    if not all_sl:
        return False

    # Test with a profit in between — should NOT trigger cooldown
    history.append({"date": "2026-05-13", "reason": "PROFIT_TARGET", "pnl": 4621})
    hist_file.write_text(json.dumps(history))
    loaded = json.loads(hist_file.read_text())
    recent = loaded[-2:]
    all_sl = all(t.get("reason") == "STOPLOSS" for t in recent)
    print(f"  After profit day, last 2 all SL: {all_sl} (should be False)")
    if all_sl:
        return False

    # Cleanup
    hist_file.unlink()
    tmp_dir.rmdir()
    return True


# -------------------------------------------------------------------------
# 8. Position Sync — positionbook API
# -------------------------------------------------------------------------
def test_positionbook():
    resp = client.positionbook()
    print(f"  Response status: {resp.get('status')}")
    if resp.get("status") != "success":
        print(f"  Failed: {resp}")
        return False
    positions = resp.get("data", [])
    print(f"  Open positions: {len(positions)}")
    for p in positions:
        sym = p.get("symbol")
        qty = p.get("quantity")
        prd = p.get("product")
        print(f"    {sym} | qty: {qty} | product: {prd}")
    return True


# -------------------------------------------------------------------------
# Run all tests
# -------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Testing straddle filters — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Server: {HOST}")

    test("VIX Check", test_vix_check)
    test("Expiry Fetch + Expiry Day", test_expiry)
    test("Gap-Open Check", test_gap_open)
    test("ORB Trend Filter (History)", test_orb_trend)
    test("Event Calendar", test_event_calendar)
    test("Iron Butterfly (4-leg order)", test_iron_butterfly)
    test("Consecutive SL Logic", test_consecutive_sl)
    test("Position Sync (positionbook)", test_positionbook)

    print(f"\n{'='*60}")
    print(f"  RESULTS: {PASS} passed, {FAIL} failed out of {PASS+FAIL}")
    print(f"{'='*60}")

    if FAIL > 0:
        sys.exit(1)
