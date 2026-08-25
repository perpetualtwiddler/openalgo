#!/usr/bin/env python3
"""wing_width_sweep.py — backlog #14: is a fixed 400-point wing right across DTE?

THE QUESTION. Our wings are a fixed +/-400 points whatever the regime. Over four sessions
risk:premium ran 0.79 -> 2.56, purely mechanically: max_risk = width x qty - premium, so as
premium falls with vol and DTE the same envelope becomes a worse bargain. Then on 2026-08-24
at 1 DTE the wings contributed +Rs338 against an Rs11,115 loss on the short PE -- at 1 DTE a
strike 400 points out has almost no delta and almost no value, which is also why it only cost
Rs656 to buy. Cheap wings and useless wings are the same fact from two sides.

But narrowing them is not obviously right either. The wings held 08-24 to 14% of defined worst
case, and (measured 08-25) they are what makes our net vol convexity FAVOURABLE (+168) instead
of adverse. They also eat theta: at 7 DTE they bleed Rs46/hr of the Rs95/hr the shorts earn.

So: replay every captured day at several widths and let the data speak, segmented BY DTE.

MODEL AND ITS LIMITS -- read before trusting a number:
  * Entry 09:35 close, exit 15:00 close, from captured 1-minute option candles. Bar closes are
    NOT fills, so absolute levels run optimistic (no spread crossed). Use this to COMPARE
    widths within a day, never to reconcile against broker P&L.
  * Breach exit modelled at our live 0.55% of entry ATM, checked on the 1m index series.
  * PT/SL (25%/50% of premium) deliberately NOT modelled: neither has ever fired in 12 live
    days, and both scale with premium, which changes with width -- including them would let
    the exit rule vary with the thing being tested.
  * The capture grid is 100-point strikes, so ATM is rounded to 100 where live rounds to 50,
    and a day whose ATM+/-width is missing from the grid is SKIPPED, not approximated.
  * Charges via charges.py, recomputed per width (STT moves with premium).

Usage:  python wing_width_sweep.py [--widths 200,300,400,500] [--csv out.csv]
"""
import argparse
import csv as _csv
import glob
import os
import re
import statistics as st
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import charges as chg  # noqa: E402

DATA_DIR = os.getenv("BACKTEST_TRADE_DATA", "/root/data/zerodha/trade-data")
QTY = int(os.getenv("SWEEP_QTY", "130"))
ENTRY_T, EXIT_T = "09:35", "15:00"
BREACH_PCT = 0.55
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def series(path):
    """{HH:MM: close} from a 1-minute csv."""
    out = {}
    try:
        for row in _csv.DictReader(open(path)):
            t = next((row[k] for k in row if "time" in k.lower() or "date" in k.lower()), "")
            m = re.search(r"(\d{2}):(\d{2})", str(t))
            c = next((row[k] for k in row if k.lower() in ("close", "c")), None)
            if m and c:
                out[f"{m.group(1)}:{m.group(2)}"] = float(c)
    except Exception:
        return {}
    return out


def expiry_of(sym):
    m = re.match(r"NIFTY(\d{2})([A-Z]{3})(\d{2})", sym)
    if not m:
        return None
    return datetime(2000 + int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).date()


def load_day(day):
    """-> (index series, {(strike, cp): series}, dte) or None."""
    idx = series(f"{DATA_DIR}/{day}/nifty_index_1m.csv")
    if not idx or ENTRY_T not in idx:
        return None
    legs, exp = {}, None
    for p in glob.glob(f"{DATA_DIR}/{day}/options/*_1m.csv"):
        b = os.path.basename(p)
        m = re.match(r"(NIFTY\d{2}[A-Z]{3}\d{2})(\d{5})(CE|PE)_1m\.csv", b)
        if not m:
            continue
        exp = exp or expiry_of(m.group(1))
        s = series(p)
        if s:
            legs[(int(m.group(2)), m.group(3))] = s
    if not legs or not exp:
        return None
    dte = (exp - datetime.strptime(day, "%Y-%m-%d").date()).days
    return idx, legs, dte


def replay(idx, legs, width, atm):
    """One day at one width -> dict, or None if the grid lacks a leg."""
    need = [(atm, "CE"), (atm, "PE"), (atm + width, "CE"), (atm - width, "PE")]
    if any(k not in legs for k in need):
        return None
    if any(ENTRY_T not in legs[k] for k in need):
        return None
    entry = {k: legs[k][ENTRY_T] for k in need}
    prem = (entry[(atm, "CE")] + entry[(atm, "PE")]
            - entry[(atm + width, "CE")] - entry[(atm - width, "PE")]) * QTY
    if prem <= 0:
        return None

    # walk forward: breach exit at 0.55% of the entry ATM, else the 15:00 close
    b = atm * BREACH_PCT / 100
    exit_t, reason = EXIT_T, "EOD"
    for t in sorted(x for x in idx if ENTRY_T < x <= EXIT_T):
        if abs(idx[t] - atm) >= b:
            exit_t, reason = t, "BREACH"
            break
    if any(exit_t not in legs[k] for k in need):
        exit_t, reason = EXIT_T, "EOD"
        if any(exit_t not in legs[k] for k in need):
            return None
    ex = {k: legs[k][exit_t] for k in need}

    sign = {(atm, "CE"): -1, (atm, "PE"): -1, (atm + width, "CE"): +1, (atm - width, "PE"): +1}
    gross = sum(sign[k] * QTY * (ex[k] - entry[k]) for k in need)
    fills = []
    for k in need:
        short = sign[k] < 0
        fills += [{"action": "SELL" if short else "BUY", "quantity": QTY,
                   "price": entry[k], "orderid": f"i{k}"},
                  {"action": "BUY" if short else "SELL", "quantity": QTY,
                   "price": ex[k], "orderid": f"o{k}"}]
    ch = chg.charges_from_fills(fills, True)
    max_risk = width * QTY - prem
    return {"net": gross - ch, "gross": gross, "charges": ch, "premium": prem,
            "max_risk": max_risk, "rp": max_risk / prem, "reason": reason,
            "hedge_cost": (entry[(atm + width, "CE")] + entry[(atm - width, "PE")]) * QTY}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--widths", default="200,300,400,500")
    ap.add_argument("--csv")
    a = ap.parse_args()
    widths = [int(x) for x in a.widths.split(",")]

    days = sorted(d for d in os.listdir(DATA_DIR) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    rows, skipped = [], 0
    for day in days:
        d = load_day(day)
        if not d:
            skipped += 1
            continue
        idx, legs, dte = d
        atm = int(round(idx[ENTRY_T] / 100.0)) * 100      # capture grid is 100-point
        for w in widths:
            r = replay(idx, legs, w, atm)
            if r:
                rows.append({"date": day, "dte": dte, "width": w, **r})

    if not rows:
        print("  no replayable days"); return 1
    print(f"\n  === wing-width sweep · {len({r['date'] for r in rows})} replayable days "
          f"of {len(days)} captured ({skipped} unloadable) · {QTY} qty ===")
    print(f"  entry {ENTRY_T} -> exit {EXIT_T} or breach at {BREACH_PCT}% · bar closes, not fills\n")

    def block(sub, label):
        if not sub:
            return
        print(f"  {label}")
        print("   width   n   mean net   median    worst     best   win%   risk:prem  hedge cost  breach%")
        for w in widths:
            g = [r for r in sub if r["width"] == w]
            if not g:
                continue
            nets = [r["net"] for r in g]
            mark = "  <- live" if w == 400 else ""
            print(f"   {w:>5} {len(g):>3}  {st.mean(nets):>+9,.0f} {st.median(nets):>+8,.0f} "
                  f"{min(nets):>+8,.0f} {max(nets):>+8,.0f}  {100*sum(1 for x in nets if x>0)/len(nets):>4.0f}%"
                  f"   {st.mean([r['rp'] for r in g]):>7.2f}   {st.mean([r['hedge_cost'] for r in g]):>9,.0f}"
                  f"   {100*sum(1 for r in g if r['reason']=='BREACH')/len(g):>5.0f}%{mark}")
        print()

    block(rows, "ALL DAYS")
    for lo, hi, lbl in ((0, 2, "LOW DTE (0-2) — where the 400pt wing looked useless"),
                        (3, 5, "MID DTE (3-5)"),
                        (6, 99, "HIGH DTE (6+)")):
        block([r for r in rows if lo <= r["dte"] <= hi], lbl)

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
        print(f"  rows -> {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
