#!/usr/bin/env python
"""
Daily trade data capture — saves intraday candles for backtesting.
Run after market close (~15:30 IST) to archive the day's data before
expired contracts get delisted from Zerodha master contracts.

Usage:
    export OPENALGO_API_KEY="your-api-key"
    python capture_trade_data.py              # captures today
    python capture_trade_data.py 2026-05-14   # captures specific date
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from openalgo import api

client = api(
    api_key=os.getenv("OPENALGO_API_KEY"),
    host=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"),
)

DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
BASE_DIR = Path(os.getenv("TRADE_DATA_DIR", "/root/data/zerodha/trade-data"))
DAY_DIR = BASE_DIR / DATE
OPTIONS_DIR = DAY_DIR / "options"

NIFTY_STRIKE_STEP = 50
BANKNIFTY_STRIKE_STEP = 100
HEDGE_OFFSETS = [4, 5, 6, 8, 10]  # OTM offsets to capture


def save_csv(data, filepath):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(filepath)
    print(f"  saved {filepath.name} ({len(data)} rows)")


def fetch_history(symbol, exchange, interval, date):
    data = client.history(
        symbol=symbol, exchange=exchange, interval=interval,
        start_date=date, end_date=date,
    )
    if data is None or isinstance(data, dict):
        print(f"  WARNING: no data for {symbol} ({exchange} {interval}): {data}")
        return None
    if len(data) == 0:
        print(f"  WARNING: empty data for {symbol} ({exchange} {interval})")
        return None
    return data


def get_atm_strike(spot, step):
    return round(spot / step) * step


def get_nearest_expiry():
    resp = client.expiry(symbol="NIFTY", exchange="NFO", instrumenttype="options")
    if resp.get("status") != "success":
        print(f"  ERROR: cannot fetch NIFTY expiry: {resp}")
        return None, None
    expiries = resp.get("data", [])
    if not expiries:
        return None, None
    nearest = expiries[0]
    exp_date = datetime.strptime(nearest, "%d-%b-%y")
    exp_tag = exp_date.strftime("%d%b%y").upper()
    return nearest, exp_tag


def get_banknifty_fut_symbol():
    resp = client.expiry(symbol="BANKNIFTY", exchange="NFO", instrumenttype="futures")
    if resp.get("status") != "success":
        print(f"  WARNING: cannot fetch BANKNIFTY futures expiry: {resp}")
        return None
    expiries = resp.get("data", [])
    if not expiries:
        return None
    nearest = expiries[0]
    exp_date = datetime.strptime(nearest, "%d-%b-%y")
    exp_tag = exp_date.strftime("%d%b%y").upper()
    symbol = f"BANKNIFTY{exp_tag}FUT"
    print(f"  BANKNIFTY futures symbol: {symbol} (expiry {nearest})")
    return symbol


def capture_index_data():
    print("\n--- NIFTY Index (1m) ---")
    data = fetch_history("NIFTY", "NSE_INDEX", "1m", DATE)
    if data is not None:
        save_csv(data, DAY_DIR / "nifty_index_1m.csv")
    return data

    print("\n--- NIFTY Index (5m) ---")
    data5 = fetch_history("NIFTY", "NSE_INDEX", "5m", DATE)
    if data5 is not None:
        save_csv(data5, DAY_DIR / "nifty_index_5m.csv")


def capture_vix():
    print("\n--- India VIX (5m) ---")
    data = fetch_history("INDIAVIX", "NSE_INDEX", "5m", DATE)
    if data is not None:
        save_csv(data, DAY_DIR / "india_vix_5m.csv")
    return data


def capture_banknifty():
    print("\n--- BANKNIFTY Futures (5m) ---")
    sym = get_banknifty_fut_symbol()
    if not sym:
        return None
    data = fetch_history(sym, "NFO", "5m", DATE)
    if data is not None:
        save_csv(data, DAY_DIR / "banknifty_fut_5m.csv")

    print("\n--- BANKNIFTY Futures (1m) ---")
    data1m = fetch_history(sym, "NFO", "1m", DATE)
    if data1m is not None:
        save_csv(data1m, DAY_DIR / "banknifty_fut_1m.csv")
    return data


def capture_nifty_options(nifty_data, expiry_tag):
    if nifty_data is None or expiry_tag is None:
        print("\n--- Skipping NIFTY options (no index data or expiry) ---")
        return {}

    entry_candles = nifty_data[nifty_data.index.strftime("%H:%M") == "09:35"]
    if len(entry_candles) == 0:
        entry_candles = nifty_data[nifty_data.index.strftime("%H:%M") == "09:20"]
    if len(entry_candles) == 0:
        spot = float(nifty_data.iloc[0]["close"])
    else:
        spot = float(entry_candles.iloc[0]["close"])

    atm = get_atm_strike(spot, NIFTY_STRIKE_STEP)
    print(f"\n--- NIFTY Options (1m) | Spot: {spot:.2f} | ATM: {atm} ---")

    strikes_to_capture = set()
    strikes_to_capture.add(atm)
    for offset in HEDGE_OFFSETS:
        strikes_to_capture.add(atm + offset * NIFTY_STRIKE_STEP)
        strikes_to_capture.add(atm - offset * NIFTY_STRIKE_STEP)

    captured = {}
    for strike in sorted(strikes_to_capture):
        for otype in ["CE", "PE"]:
            sym = f"NIFTY{expiry_tag}{strike}{otype}"
            data = fetch_history(sym, "NFO", "1m", DATE)
            if data is not None:
                save_csv(data, OPTIONS_DIR / f"{sym}_1m.csv")
                captured[sym] = len(data)

    return captured


def capture_metadata(nifty_data, expiry_raw, expiry_tag, options_captured):
    print("\n--- Metadata ---")
    metadata = {
        "date": DATE,
        "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "nifty_expiry": expiry_raw,
        "nifty_expiry_tag": expiry_tag,
    }

    if nifty_data is not None and len(nifty_data) > 0:
        entry_candles = nifty_data[nifty_data.index.strftime("%H:%M") == "09:35"]
        if len(entry_candles) > 0:
            spot = float(entry_candles.iloc[0]["close"])
        else:
            spot = float(nifty_data.iloc[0]["close"])

        atm = get_atm_strike(spot, NIFTY_STRIKE_STEP)
        metadata["nifty_spot_at_entry"] = spot
        metadata["atm_strike"] = atm
        metadata["nifty_open"] = float(nifty_data.iloc[0]["open"])
        metadata["nifty_high"] = float(nifty_data["high"].max())
        metadata["nifty_low"] = float(nifty_data["low"].min())
        metadata["nifty_close"] = float(nifty_data.iloc[-1]["close"])

    # VIX at open
    try:
        vix_data = fetch_history("INDIAVIX", "NSE_INDEX", "5m", DATE)
        if vix_data is not None and len(vix_data) > 0:
            metadata["vix_open"] = float(vix_data.iloc[0]["open"])
            metadata["vix_close"] = float(vix_data.iloc[-1]["close"])
            metadata["vix_high"] = float(vix_data["high"].max())
            metadata["vix_low"] = float(vix_data["low"].min())
    except Exception:
        pass

    metadata["options_captured"] = list(options_captured.keys()) if options_captured else []
    metadata["hedge_offsets_saved"] = HEDGE_OFFSETS

    meta_path = DAY_DIR / "metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2))
    print(f"  saved metadata.json")

    for k, v in metadata.items():
        if k != "options_captured":
            print(f"    {k}: {v}")
    print(f"    options_captured: {len(metadata.get('options_captured', []))} symbols")


def main():
    print(f"{'='*60}")
    print(f"  TRADE DATA CAPTURE — {DATE}")
    print(f"{'='*60}")
    print(f"  Output: {DAY_DIR}")

    DAY_DIR.mkdir(parents=True, exist_ok=True)
    OPTIONS_DIR.mkdir(parents=True, exist_ok=True)

    nifty_data = capture_index_data()
    capture_vix()
    capture_banknifty()

    expiry_raw, expiry_tag = get_nearest_expiry()
    print(f"  NIFTY nearest expiry: {expiry_raw} -> {expiry_tag}")

    options_captured = capture_nifty_options(nifty_data, expiry_tag)

    capture_metadata(nifty_data, expiry_raw, expiry_tag, options_captured)

    total_files = sum(1 for _ in DAY_DIR.rglob("*.csv"))
    print(f"\n{'='*60}")
    print(f"  DONE — {total_files} CSV files + metadata.json")
    print(f"  Location: {DAY_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
