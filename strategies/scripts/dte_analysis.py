#!/usr/bin/env python3
"""dte_analysis.py — should we trade 1 DTE at all? (backlog #17)

THE QUESTION. August: four 1-DTE days lost Rs6,185 while the other eleven made Rs1,304. But
n=4, and one of those four (2026-08-10, +Rs1,981) was our best day ever. Four days cannot
distinguish "1 DTE is structurally bad" from "four days went badly", so replay every captured
day under OUR ACTUAL RULES and segment by DTE.

Rules replayed: 09:35 entry, ATM short straddle + /-400 wings, breach exit at 0.55% of the
entry ATM, otherwise the 15:00 square-off. No stop and no target -- those are separate
variables and including them would confound the DTE question with an exit-rule question.

WHAT TO LOOK FOR, because "lower mean" and "higher variance" imply different actions:
  * if 1 DTE has a genuinely LOWER MEAN -> consider skipping it
  * if the mean is similar but VARIANCE is much higher -> the answer is smaller size, not
    skipping; the edge is intact and only the risk per rupee deployed has changed
  * if the difference is inside noise -> keep trading it and stop theorising

Same caveats as every sweep here: bar closes are not fills (absolute levels optimistic, and
our live entry slippage alone ran Rs169-208 in late August), 0 DTE is excluded because we
never sell the expiring series, and the capture's 100-point strike grid means some days are
skipped rather than approximated.

Usage:  python dte_analysis.py [--csv out.csv]
"""
import argparse
import csv as _csv
import glob
import math
import os
import re
import statistics as st
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import charges as chg  # noqa: E402

DATA_DIR = os.getenv("BACKTEST_TRADE_DATA", "/root/data/zerodha/trade-data")
QTY, WING, ENTRY_T, EXIT_T, BREACH_PCT = 130, 400, "09:35", "15:00", 0.55
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def series(path):
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


def replay(day):
    idx = series(f"{DATA_DIR}/{day}/nifty_index_1m.csv")
    if not idx or ENTRY_T not in idx:
        return None
    legs, exp = {}, None
    for p in glob.glob(f"{DATA_DIR}/{day}/options/*_1m.csv"):
        m = re.match(r"(NIFTY\d{2}[A-Z]{3}\d{2})(\d{5})(CE|PE)_1m\.csv", os.path.basename(p))
        if not m:
            continue
        if exp is None:
            e = re.match(r"NIFTY(\d{2})([A-Z]{3})(\d{2})", m.group(1))
            exp = datetime(2000 + int(e.group(3)), MONTHS[e.group(2)], int(e.group(1))).date()
        s = series(p)
        if s:
            legs[(int(m.group(2)), m.group(3))] = s
    if not legs or exp is None:
        return None
    dte = (exp - datetime.strptime(day, "%Y-%m-%d").date()).days
    atm = int(round(idx[ENTRY_T] / 100.0)) * 100
    need = [(atm, "CE"), (atm, "PE"), (atm + WING, "CE"), (atm - WING, "PE")]
    if any(k not in legs or ENTRY_T not in legs[k] for k in need):
        return None
    entry = {k: legs[k][ENTRY_T] for k in need}
    sign = {need[0]: -1, need[1]: -1, need[2]: +1, need[3]: +1}
    prem = sum(-sign[k] * entry[k] for k in need) * QTY
    if prem <= 0:
        return None

    def net(marks):
        g = sum(sign[k] * QTY * (marks[k] - entry[k]) for k in need)
        f = []
        for k in need:
            sh = sign[k] < 0
            f += [{"action": "SELL" if sh else "BUY", "quantity": QTY,
                   "price": entry[k], "orderid": f"i{k}"},
                  {"action": "BUY" if sh else "SELL", "quantity": QTY,
                   "price": marks[k], "orderid": f"o{k}"}]
        return g - chg.charges_from_fills(f, True)

    band = atm * BREACH_PCT / 100
    mfe = mae = None
    exit_t, reason = None, "EOD"
    for t in sorted(x for x in idx if ENTRY_T < x <= EXIT_T):
        if any(t not in legs[k] for k in need):
            continue
        n = net({k: legs[k][t] for k in need})
        mfe = n if mfe is None else max(mfe, n)
        mae = n if mae is None else min(mae, n)
        if abs(idx[t] - atm) >= band:
            exit_t, reason = t, "BREACH"
            break
        exit_t = t
    if exit_t is None:
        return None
    return {"date": day, "dte": dte, "net": net({k: legs[k][exit_t] for k in need}),
            "reason": reason, "premium": prem, "mfe": mfe, "mae": mae,
            "range": (mfe - mae) if mfe is not None else 0}


def welch(a, b):
    ma, mb = st.mean(a), st.mean(b)
    va, vb = st.variance(a) / len(a), st.variance(b) / len(b)
    t = (ma - mb) / math.sqrt(va + vb)
    return ma - mb, t


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--csv"); a = ap.parse_args()
    rows = []
    for d in sorted(x for x in os.listdir(DATA_DIR) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x)):
        r = replay(d)
        if r and r["dte"] >= 1:
            rows.append(r)
    print(f"\n  === DTE analysis · {len(rows)} replayable days (0 DTE excluded) · {QTY} qty ===")
    print(f"  09:35 entry · ATM +/-{WING} · breach {BREACH_PCT}% · else {EXIT_T} · no stop, no target\n")
    print("   DTE   n     mean    median    stdev     worst      best   win%  breach%  mean|MFE-MAE|")
    buckets = {}
    for dte in sorted({r["dte"] for r in rows}):
        g = [r for r in rows if r["dte"] == dte]
        buckets[dte] = g
        n = [r["net"] for r in g]
        print(f"   {dte:>3} {len(n):>3} {st.mean(n):>+8,.0f} {st.median(n):>+9,.0f} "
              f"{(st.stdev(n) if len(n)>1 else 0):>8,.0f} {min(n):>+9,.0f} {max(n):>+9,.0f}"
              f"  {100*sum(1 for x in n if x>0)/len(n):>4.0f}%   {100*sum(1 for r in g if r['reason']=='BREACH')/len(g):>4.0f}%"
              f"      {st.mean([r['range'] for r in g]):>8,.0f}")
    one = [r["net"] for r in rows if r["dte"] == 1]
    rest = [r["net"] for r in rows if r["dte"] >= 2]
    if one and rest:
        d, t = welch(one, rest)
        print(f"\n  1 DTE (n={len(one)}) vs 2+ DTE (n={len(rest)}):")
        print(f"   mean {st.mean(one):+,.0f} vs {st.mean(rest):+,.0f}   difference {d:+,.0f}   "
              f"Welch t = {t:+.2f}   -> {'SIGNIFICANT' if abs(t)>2 else 'NOT significant'}")
        print(f"   stdev {st.stdev(one):,.0f} vs {st.stdev(rest):,.0f}   "
              f"-> 1 DTE is {st.stdev(one)/st.stdev(rest):.2f}x as volatile")
        print(f"   worst {min(one):+,.0f} vs {min(rest):+,.0f}")
        sharpe1 = st.mean(one)/st.stdev(one) if st.stdev(one) else 0
        sharpeR = st.mean(rest)/st.stdev(rest) if st.stdev(rest) else 0
        print(f"   return per unit of risk: {sharpe1:+.3f} vs {sharpeR:+.3f}")
        half = st.mean(one)/2
        print(f"\n   if we HALVED size on 1 DTE: mean {half:+,.0f}/day, worst {min(one)/2:+,.0f}, "
              f"stdev {st.stdev(one)/2:,.0f}")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        print(f"\n  rows -> {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
