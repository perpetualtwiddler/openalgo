#!/usr/bin/env python
"""Quick simulation of today's EMA crossover if it had executed."""
import os
from openalgo import api

client = api(
    api_key=os.getenv("OPENALGO_API_KEY"),
    host=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"),
)

data = client.history(
    symbol="BANKNIFTY26MAY26FUT", exchange="NFO", interval="5m",
    start_date="2026-05-14", end_date="2026-05-14",
)
data["ema9"] = data["close"].ewm(span=9, adjust=False).mean()
data["ema21"] = data["close"].ewm(span=21, adjust=False).mean()

crossovers = []
for i in range(1, len(data)):
    pf, ps = data.iloc[i-1]["ema9"], data.iloc[i-1]["ema21"]
    cf, cs = data.iloc[i]["ema9"], data.iloc[i]["ema21"]
    if pf < ps and cf >= cs:
        crossovers.append(("BUY", data.index[i], float(data.iloc[i]["close"])))
    elif pf >= ps and cf < cs:
        crossovers.append(("SELL", data.index[i], float(data.iloc[i]["close"])))

print("=== ALL CROSSOVERS TODAY ===")
for sig, ts, price in crossovers:
    print("  %s at %s | Price: %.2f" % (sig, ts.strftime("%H:%M"), price))

if crossovers and crossovers[0][0] == "BUY":
    bp, bt = crossovers[0][2], crossovers[0][1]
    sp, st, etype = None, None, None
    for sig, ts, price in crossovers[1:]:
        if sig == "SELL":
            sp, st, etype = price, ts, "SELL crossover"
            break
    if not sp:
        sp = float(data.iloc[-1]["close"])
        st = data.index[-1]
        etype = "EOD"

    pnl = (sp - bp) * 60
    sign = "+" if pnl >= 0 else ""
    print()
    print("=== SIMULATED RESULT (60 qty BUY) ===")
    print("  Entry: BUY @ %.2f at %s" % (bp, bt.strftime("%H:%M")))
    print("  Exit:  %s @ %.2f at %s" % (etype, sp, st.strftime("%H:%M")))
    print("  P&L:   %s%.0f INR" % (sign, pnl))

    ae = data[(data.index >= bt) & (data.index <= st)]
    peak = float(ae["high"].max())
    low = float(ae["low"].min())
    print("  Peak: %.2f (+%.0f)" % (peak, (peak - bp) * 60))
    print("  Low:  %.2f (%.0f)" % (low, (low - bp) * 60))
