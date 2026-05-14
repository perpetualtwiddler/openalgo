#!/usr/bin/env python
"""Analyze iron butterfly hedge cost vs naked straddle for a given day."""
import os
import sys
from openalgo import api

client = api(
    api_key=os.getenv("OPENALGO_API_KEY"),
    host=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"),
)

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-14"
QTY = 260  # 4 lots (live)
ENTRY_TIME = "09:35"

symbols = {
    "ce_short": "NIFTY19MAY2623550CE",
    "pe_short": "NIFTY19MAY2623550PE",
    "ce_hedge": "NIFTY19MAY2623750CE",
    "pe_hedge": "NIFTY19MAY2623350PE",
}

data = {}
for k, sym in symbols.items():
    d = client.history(symbol=sym, exchange="NFO", interval="1m",
                       start_date=DATE, end_date=DATE)
    if d is None or isinstance(d, dict):
        print(f"ERROR: No data for {sym}: {d}")
        sys.exit(1)
    data[k] = d
    print(f"{k}: {sym} -- {len(d)} candles")


def at(df, t):
    m = df[df.index.strftime("%H:%M") == t]
    return float(m.iloc[0]["close"]) if len(m) > 0 else None


ce_e = at(data["ce_short"], ENTRY_TIME)
pe_e = at(data["pe_short"], ENTRY_TIME)
hce_e = at(data["ce_hedge"], ENTRY_TIME)
hpe_e = at(data["pe_hedge"], ENTRY_TIME)

gross = ce_e + pe_e
hedge = hce_e + hpe_e
net = gross - hedge
hedge_pct = hedge / gross * 100

print(f"\n{'='*70}")
print(f"  HEDGE COST ANALYSIS -- {DATE}")
print(f"{'='*70}")
print(f"  SELL CE: {ce_e:.2f} | SELL PE: {pe_e:.2f} | Gross/unit: {gross:.2f}")
print(f"  BUY  CE: {hce_e:.2f} | BUY  PE: {hpe_e:.2f} | Hedge/unit: {hedge:.2f}")
print(f"  Net premium/unit: {net:.2f}")
print(f"  Hedge cost: {hedge_pct:.1f}% of gross premium")
print(f"  With {QTY} qty: Gross={gross*QTY:,.0f} | Hedge={hedge*QTY:,.0f} | Net={net*QTY:,.0f}")

# Walk through the day: naked vs iron butterfly
print(f"\n{'='*70}")
print(f"  NAKED STRADDLE vs IRON BUTTERFLY (throughout the day)")
print(f"{'='*70}")
fmt = "  {:<6} {:>8} {:>8} {:>12} {:>8} {:>8} {:>12} {:>10}"
print(fmt.format("Time", "CE", "PE", "Naked P&L", "HedCE", "HedPE", "IB P&L", "Hedge Drag"))
print("  " + "-" * 66)

times = ["09:35", "10:00", "10:30", "11:00", "11:30", "12:00",
         "12:30", "13:00", "13:30", "14:00", "14:30", "15:00", "15:15"]

for t in times:
    ce_now = at(data["ce_short"], t)
    pe_now = at(data["pe_short"], t)
    hce_now = at(data["ce_hedge"], t)
    hpe_now = at(data["pe_hedge"], t)
    if any(v is None for v in [ce_now, pe_now, hce_now, hpe_now]):
        continue

    short_pnl = ((ce_e - ce_now) + (pe_e - pe_now)) * QTY
    hedge_pnl = ((hce_now - hce_e) + (hpe_now - hpe_e)) * QTY
    ib_pnl = short_pnl + hedge_pnl
    drag = hedge_pnl

    print(fmt.format(
        t,
        f"{ce_now:.1f}",
        f"{pe_now:.1f}",
        f"{short_pnl:+,.0f}",
        f"{hce_now:.1f}",
        f"{hpe_now:.1f}",
        f"{ib_pnl:+,.0f}",
        f"{drag:+,.0f}",
    ))

# Final comparison
print(f"\n{'='*70}")
print(f"  WHAT-IF: DIFFERENT HEDGE WIDTHS")
print(f"{'='*70}")

offsets = [
    ("OTM4 (200pts)", "NIFTY19MAY2623750CE", "NIFTY19MAY2623350PE"),
    ("OTM5 (250pts)", "NIFTY19MAY2623800CE", "NIFTY19MAY2623300PE"),
    ("OTM6 (300pts)", "NIFTY19MAY2623850CE", "NIFTY19MAY2623250PE"),
    ("OTM8 (400pts)", "NIFTY19MAY2623950CE", "NIFTY19MAY2623150PE"),
]

ce_exit = at(data["ce_short"], "15:15") or at(data["ce_short"], "15:14")
pe_exit = at(data["pe_short"], "15:15") or at(data["pe_short"], "15:14")
naked_pnl = ((ce_e - ce_exit) + (pe_e - pe_exit)) * QTY

print(f"  Naked straddle EOD P&L: {naked_pnl:+,.0f} INR\n")

for name, ce_sym, pe_sym in offsets:
    try:
        hce = client.history(symbol=ce_sym, exchange="NFO", interval="1m",
                             start_date=DATE, end_date=DATE)
        hpe = client.history(symbol=pe_sym, exchange="NFO", interval="1m",
                             start_date=DATE, end_date=DATE)
        if hce is None or isinstance(hce, dict) or hpe is None or isinstance(hpe, dict):
            print(f"  {name}: no data")
            continue
        hce_entry = at(hce, ENTRY_TIME)
        hpe_entry = at(hpe, ENTRY_TIME)
        hce_exit_p = at(hce, "15:15") or at(hce, "15:14")
        hpe_exit_p = at(hpe, "15:15") or at(hpe, "15:14")
        if not all([hce_entry, hpe_entry, hce_exit_p, hpe_exit_p]):
            print(f"  {name}: missing price data")
            continue

        cost = hce_entry + hpe_entry
        cost_pct = cost / gross * 100
        net_prem = (gross - cost) * QTY
        h_pnl = ((hce_exit_p - hce_entry) + (hpe_exit_p - hpe_entry)) * QTY
        ib_pnl = naked_pnl + h_pnl
        print(f"  {name}: hedge {hce_entry:.1f}+{hpe_entry:.1f} = {cost:.1f} "
              f"({cost_pct:.0f}% of gross) | Net prem: {net_prem:,.0f} | "
              f"EOD P&L: {ib_pnl:+,.0f} | Drag: {h_pnl:+,.0f}")
    except Exception as e:
        print(f"  {name}: {e}")

print(f"\n{'='*70}")
print(f"  SUMMARY")
print(f"{'='*70}")
ib_final = naked_pnl + ((at(data["ce_hedge"], "15:15") or at(data["ce_hedge"], "15:14")) - hce_e +
                         (at(data["pe_hedge"], "15:15") or at(data["pe_hedge"], "15:14")) - hpe_e) * QTY
print(f"  Naked straddle P&L:    {naked_pnl:+,.0f} INR")
print(f"  Iron butterfly P&L:    {ib_final:+,.0f} INR  (OTM4)")
if naked_pnl > 0:
    print(f"  Hedge absorbed {abs(ib_final - naked_pnl) / naked_pnl * 100:.0f}% of naked profit")
print(f"  Hedge protects on bad days (May 12 SL would have been capped)")
print(f"{'='*70}")
