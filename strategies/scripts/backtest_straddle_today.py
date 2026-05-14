#!/usr/bin/env python
"""
Backtest today's iron butterfly trade using 1-minute option data.
Fetches ATM CE/PE + OTM4 hedge prices, simulates entry at 9:35,
then tracks P&L with profit target (60%) and stop-loss (50%) exits.
"""
import os
import sys
from datetime import datetime

from openalgo import api

client = api(
    api_key=os.getenv("OPENALGO_API_KEY"),
    host=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"),
)

DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
LOTS = 3
LOT_SIZE = 65
QUANTITY = LOTS * LOT_SIZE  # 195
PROFIT_TARGET_PCT = 25.0
STOPLOSS_PCT = 50.0
ENTRY_TIME = "09:35"
SQUAREOFF_TIME = "15:15"

# --- Step 1: get NIFTY spot at entry to find ATM strike ---
print(f"=== IRON BUTTERFLY BACKTEST — {DATE} ===\n")

nifty = client.history(
    symbol="NIFTY", exchange="NSE_INDEX", interval="1m",
    start_date=DATE, end_date=DATE,
)
if nifty is None or len(nifty) == 0:
    print("ERROR: No NIFTY data for", DATE)
    sys.exit(1)

entry_candles = nifty[nifty.index.strftime("%H:%M") == ENTRY_TIME]
if len(entry_candles) == 0:
    print(f"ERROR: No candle at {ENTRY_TIME}")
    sys.exit(1)

spot_at_entry = float(entry_candles.iloc[0]["close"])
atm_strike = round(spot_at_entry / 50) * 50
otm_ce_strike = atm_strike + 400  # OTM8 = 8 strikes x 50pts
otm_pe_strike = atm_strike - 400

print(f"NIFTY spot at {ENTRY_TIME}: {spot_at_entry:.2f}")
print(f"ATM strike: {atm_strike}")
print(f"OTM4 CE hedge: {otm_ce_strike} | OTM4 PE hedge: {otm_pe_strike}")

# --- Step 2: get nearest expiry ---
resp = client.expiry(symbol="NIFTY", exchange="NFO", instrumenttype="options")
if resp.get("status") != "success":
    print(f"ERROR: Cannot fetch expiry: {resp}")
    sys.exit(1)
expiries = resp["data"]
nearest_expiry = expiries[0]  # "DD-MMM-YY"
expiry_tag = datetime.strptime(nearest_expiry, "%d-%b-%y").strftime("%d%b%y").upper()

# Build option symbols — format: NIFTY19MAY2623400CE
expiry_fmt = datetime.strptime(nearest_expiry, "%d-%b-%y").strftime("%d%b%y").upper()
# Actually the symbol format is: NIFTY{DD}{MON}{YY}{STRIKE}{CE/PE}
# From logs we saw: NIFTY19MAY2623400CE — let's derive from expiry
exp_date = datetime.strptime(nearest_expiry, "%d-%b-%y")
exp_str = exp_date.strftime("%d%b%y").upper()  # 19MAY26

ce_symbol = f"NIFTY{exp_str}{atm_strike}CE"
pe_symbol = f"NIFTY{exp_str}{atm_strike}PE"
hedge_ce_symbol = f"NIFTY{exp_str}{otm_ce_strike}CE"
hedge_pe_symbol = f"NIFTY{exp_str}{otm_pe_strike}PE"

print(f"Expiry: {nearest_expiry}")
print(f"SELL CE: {ce_symbol}")
print(f"SELL PE: {pe_symbol}")
print(f"BUY  CE: {hedge_ce_symbol}")
print(f"BUY  PE: {hedge_pe_symbol}")

# --- Step 3: fetch 1m option data ---
print(f"\nFetching 1-minute option data...")

def fetch(symbol):
    data = client.history(
        symbol=symbol, exchange="NFO", interval="1m",
        start_date=DATE, end_date=DATE,
    )
    if data is None or len(data) == 0:
        print(f"  WARNING: No data for {symbol}")
        return None
    print(f"  {symbol}: {len(data)} candles")
    return data

ce_data = fetch(ce_symbol)
pe_data = fetch(pe_symbol)
hce_data = fetch(hedge_ce_symbol)
hpe_data = fetch(hedge_pe_symbol)

if ce_data is None or pe_data is None:
    print("ERROR: Cannot fetch ATM option data — aborting")
    sys.exit(1)

# --- Step 4: simulate ---
print(f"\n{'='*65}")
print(f"  SIMULATION")
print(f"{'='*65}")

# Find entry prices at 9:35
def price_at(data, time_str):
    matches = data[data.index.strftime("%H:%M") == time_str]
    if len(matches) > 0:
        return float(matches.iloc[0]["close"])
    return None

ce_entry = price_at(ce_data, ENTRY_TIME)
pe_entry = price_at(pe_data, ENTRY_TIME)
hce_entry = price_at(hce_data, ENTRY_TIME) if hce_data is not None else 0
hpe_entry = price_at(hpe_data, ENTRY_TIME) if hpe_data is not None else 0

if ce_entry is None or pe_entry is None:
    print(f"ERROR: No option price at {ENTRY_TIME}")
    sys.exit(1)

gross_premium = (ce_entry + pe_entry) * QUANTITY
hedge_cost = (hce_entry + hpe_entry) * QUANTITY
net_premium = gross_premium - hedge_cost

print(f"  Entry at {ENTRY_TIME}:")
print(f"    SELL CE @ {ce_entry:.2f} | SELL PE @ {pe_entry:.2f}")
print(f"    BUY  CE @ {hce_entry:.2f} | BUY  PE @ {hpe_entry:.2f}")
print(f"    Gross premium: {gross_premium:,.0f} | Hedge cost: {hedge_cost:,.0f}")
print(f"    Net premium: {net_premium:,.0f}")
print(f"    Profit target (+{PROFIT_TARGET_PCT}%): +{net_premium * PROFIT_TARGET_PCT / 100:,.0f}")
print(f"    Stop-loss (-{STOPLOSS_PCT}%): -{net_premium * STOPLOSS_PCT / 100:,.0f}")

# Walk through minute-by-minute
exit_time = None
exit_reason = None
exit_pnl = None
peak_pnl = float("-inf")
trough_pnl = float("inf")

# Get common timestamps after entry
entry_idx = ce_data.index.get_indexer(
    ce_data[ce_data.index.strftime("%H:%M") == ENTRY_TIME].index
)
if len(entry_idx) == 0 or entry_idx[0] == -1:
    print("ERROR: Cannot locate entry candle index")
    sys.exit(1)

start_pos = entry_idx[0]

print(f"\n  Minute-by-minute P&L (key moments):")

for i in range(start_pos, len(ce_data)):
    ts = ce_data.index[i]
    time_str = ts.strftime("%H:%M")

    ce_now = float(ce_data.iloc[i]["close"])
    pe_now = float(pe_data.iloc[i]["close"]) if i < len(pe_data) else pe_entry

    hce_now = 0.0
    hpe_now = 0.0
    if hce_data is not None and i < len(hce_data):
        hce_now = float(hce_data.iloc[i]["close"])
    if hpe_data is not None and i < len(hpe_data):
        hpe_now = float(hpe_data.iloc[i]["close"])

    short_pnl = ((ce_entry - ce_now) + (pe_entry - pe_now)) * QUANTITY
    hedge_pnl = ((hce_now - hce_entry) + (hpe_now - hpe_entry)) * QUANTITY
    total_pnl = short_pnl + hedge_pnl
    pnl_pct = total_pnl / net_premium * 100 if net_premium > 0 else 0

    if total_pnl > peak_pnl:
        peak_pnl = total_pnl
        peak_time = time_str
    if total_pnl < trough_pnl:
        trough_pnl = total_pnl
        trough_time = time_str

    # Print at key moments: every 30 min, or at extremes
    if time_str.endswith(":00") or time_str.endswith(":30") or time_str == ENTRY_TIME:
        sign = "+" if total_pnl >= 0 else ""
        print(f"    {time_str} | CE:{ce_now:.2f} PE:{pe_now:.2f} | Net P&L: {sign}{total_pnl:,.0f} ({sign}{pnl_pct:.1f}%)")

    # Check profit target
    if pnl_pct >= PROFIT_TARGET_PCT and exit_reason is None:
        exit_time = time_str
        exit_reason = "PROFIT_TARGET"
        exit_pnl = total_pnl
        exit_pct = pnl_pct
        break

    # Check stop-loss
    if pnl_pct <= -STOPLOSS_PCT and exit_reason is None:
        exit_time = time_str
        exit_reason = "STOPLOSS"
        exit_pnl = total_pnl
        exit_pct = pnl_pct
        break

    # Check squareoff time
    sq_h, sq_m = 15, 15
    if ts.hour > sq_h or (ts.hour == sq_h and ts.minute >= sq_m):
        exit_time = time_str
        exit_reason = "EOD_SQUAREOFF"
        exit_pnl = total_pnl
        exit_pct = pnl_pct
        break

# If we ran out of data without hitting any exit
if exit_reason is None:
    last_ts = ce_data.index[-1]
    exit_time = last_ts.strftime("%H:%M")
    ce_last = float(ce_data.iloc[-1]["close"])
    pe_last = float(pe_data.iloc[-1]["close"]) if len(pe_data) > 0 else pe_entry
    hce_last = float(hce_data.iloc[-1]["close"]) if hce_data is not None and len(hce_data) > 0 else 0
    hpe_last = float(hpe_data.iloc[-1]["close"]) if hpe_data is not None and len(hpe_data) > 0 else 0
    short_pnl = ((ce_entry - ce_last) + (pe_entry - pe_last)) * QUANTITY
    hedge_pnl = ((hce_last - hce_entry) + (hpe_last - hpe_entry)) * QUANTITY
    exit_pnl = short_pnl + hedge_pnl
    exit_pct = exit_pnl / net_premium * 100 if net_premium > 0 else 0
    exit_reason = "END_OF_DATA"

# --- Step 5: Results ---
sign = "+" if exit_pnl >= 0 else ""
pk_sign = "+" if peak_pnl >= 0 else ""
tr_sign = "+" if trough_pnl >= 0 else ""

print(f"\n{'='*65}")
print(f"  BACKTEST RESULT — {DATE}")
print(f"{'='*65}")
print(f"  Entry:  {ENTRY_TIME} | ATM strike {atm_strike}")
print(f"  Exit:   {exit_time} | Reason: {exit_reason}")
print(f"  P&L:    {sign}{exit_pnl:,.0f} INR ({sign}{exit_pct:.1f}% of premium)")
print(f"  Peak:   {pk_sign}{peak_pnl:,.0f} at {peak_time}")
print(f"  Trough: {tr_sign}{trough_pnl:,.0f} at {trough_time}")
print(f"  Config: {LOTS} lots x {LOT_SIZE} = {QUANTITY} qty | PT:{PROFIT_TARGET_PCT}% SL:{STOPLOSS_PCT}%")
print(f"{'='*65}")
