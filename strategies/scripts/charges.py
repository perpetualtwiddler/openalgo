#!/usr/bin/env python3
"""charges.py — Zerodha transaction-cost model (per the rate-card screenshot, 2026)
used to convert GROSS backtest P&L to NET (after-cost) earnings.

A "round-trip" = 1 entry + 1 exit (what the backtests count as one trade).
All functions return TOTAL charges in ₹ for one round-trip.

Rates (from the screenshot — adjust here if Zerodha changes them):
  F&O Futures : brokerage 0.03% or ₹20/order (lower); STT 0.05% sell-side (notional);
                txn NSE 0.00183%; SEBI ₹10/cr; stamp 0.002% buy-side; GST 18% on
                (brokerage+SEBI+txn).
  F&O Options : brokerage flat ₹20/order; STT 0.15% sell-side (premium);
                txn NSE 0.03553% (premium); SEBI ₹10/cr; stamp 0.003% buy-side;
                GST 18% on (brokerage+SEBI+txn).
"""

GST = 0.18
SEBI_PER_CR = 10.0  # ₹10 per ₹1 crore turnover


def futures_roundtrip(price, qty):
    """BANKNIFTY/index-futures round-trip charges. STT (0.05% sell-side notional) dominates."""
    notional = price * qty          # per side (entry≈exit, both ~price)
    turnover = 2 * notional         # buy + sell
    brokerage = 2 * min(0.0003 * notional, 20.0)   # 0.03% or ₹20/order (lower), 2 orders
    stt = 0.0005 * notional                          # 0.05% sell side
    txn = 0.0000183 * turnover                       # NSE 0.00183%
    sebi = SEBI_PER_CR * turnover / 1e7              # ₹10/crore
    stamp = 0.00002 * notional                        # 0.002% buy side
    gst = GST * (brokerage + sebi + txn)
    return brokerage + stt + txn + sebi + stamp + gst


def options_iron_butterfly_roundtrip(atm_ce, atm_pe, hedge_ce, hedge_pe, qty, n_orders=8):
    """Iron-butterfly round-trip charges from the ENTRY leg premiums (₹/share).
    8 orders = 4 legs × (entry + exit). Exit premiums approximated ≈ entry for the
    small non-STT terms; STT base = the sell-side premium. Charges here are small
    vs P&L, so this approximation is immaterial to the net ranking."""
    legs_prem = (atm_ce + atm_pe + hedge_ce + hedge_pe) * qty   # one side's total premium
    brokerage = n_orders * 20.0                                  # flat ₹20/order
    # sell side over the round trip: entry ATM sell + exit hedge sell ≈ all 4 legs once
    stt = 0.0015 * legs_prem
    turnover = 2 * legs_prem                                     # entry + exit premium turnover
    txn = 0.0003553 * turnover                                   # NSE 0.03553% on premium
    sebi = SEBI_PER_CR * turnover / 1e7
    stamp = 0.00003 * legs_prem                                   # 0.003% buy side
    gst = GST * (brokerage + sebi + txn)
    return brokerage + stt + txn + sebi + stamp + gst


def charges_from_fills(fills, is_options):
    """Exact Zerodha charges from ACTUAL executed fills (for the live EOD summary).

    fills: iterable of dicts each with 'action' ('BUY'/'SELL'), 'quantity', 'price'.
    is_options: True for NFO options (straddle), False for index futures (EMA legs).

    Unlike the *_roundtrip() helpers (which assume a symmetric entry≈exit backtest
    trade), this applies the rate card per real fill, so it handles asymmetric
    prices, multiple round-trips in a day, and any leg count correctly. Same rate
    card, so it reconciles with the backtest helpers on a clean symmetric day.
    """
    buy_val = sum(f["quantity"] * f["price"] for f in fills if f["action"].upper() == "BUY")
    sell_val = sum(f["quantity"] * f["price"] for f in fills if f["action"].upper() == "SELL")
    turnover = buy_val + sell_val

    if is_options:
        brokerage = len(fills) * 20.0                 # flat ₹20/order
        stt = 0.0015 * sell_val                        # 0.15% sell-side premium
        txn = 0.0003553 * turnover                     # NSE 0.03553% on premium
        stamp = 0.00003 * buy_val                      # 0.003% buy side
    else:
        # futures: 0.03% or ₹20/order (lower), charged per order
        brokerage = sum(min(0.0003 * f["quantity"] * f["price"], 20.0) for f in fills)
        stt = 0.0005 * sell_val                        # 0.05% sell-side notional
        txn = 0.0000183 * turnover                     # NSE 0.00183%
        stamp = 0.00002 * buy_val                      # 0.002% buy side

    sebi = SEBI_PER_CR * turnover / 1e7                # ₹10/crore
    gst = GST * (brokerage + sebi + txn)
    # Zerodha bills STT and stamp duty ROUNDED TO THE NEAREST RUPEE. Verified against the real
    # contract note for 2026-08-06: our unrounded model gave 285.20 vs 284.91 actual, and the
    # entire 0.29 gap was these two lines (STT 61.07 vs 61.00, stamp 1.21 vs 1.00). Every other
    # component matched to the paisa.
    return brokerage + round(stt) + txn + sebi + round(stamp) + gst


if __name__ == "__main__":
    # quick reference print
    f = futures_roundtrip(57800, 60)
    o = options_iron_butterfly_roundtrip(135, 120, 20, 17, 195)
    print(f"futures round-trip @57800x60 (notional ~Rs{57800*60:,}/side): Rs{f:,.0f}")
    print(f"options iron-butterfly round-trip (ATM~255, hedge~37, 195q): Rs{o:,.0f}")
