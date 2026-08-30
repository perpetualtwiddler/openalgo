#!/usr/bin/env python3
"""exit_level_sweep.py — what stop and what profit target, measured across every captured day.

WHY. On 2026-08-28 the armed -Rs1,500 stop fired at 10:17, at what turned out to be the single
worst minute of the day; holding to the ordinary 15:00 exit would have turned -Rs1,512 into
+Rs1,500. That is a Rs3,012 error from one parameter. The level was never measured -- it was
picked at runtime. -Rs1,500 is 0.79 standard deviations of our daily range, i.e. INSIDE normal
noise, and it fired on 6 of 14 live days with 2 of those 6 wrong.

So: replay every captured day minute by minute and test a grid of (stop, target) pairs. The
first rule to trigger wins -- target, stop, the 0.55% breach guard, or the 15:00 square-off.

READ THE CAVEATS BEFORE ACTING ON A NUMBER:
  * A 6x6 grid over ~40 days WILL produce a best cell, and that cell is probably noise. The
    useful output is which REGIONS are consistently decent, not the maximum. Treat any single
    winner with suspicion, especially if its neighbours are much worse -- that is the signature
    of a fitted artefact, not an edge.
  * Bar closes are not fills. Absolute rupees run optimistic (no spread crossed, and our live
    entry slippage alone has been Rs182-208 on recent days). Compare LEVELS, not totals.
  * 0-DTE days are excluded: we never sell the expiring series, and leaving them in badly
    distorted the wing-width sweep before I caught it.
  * Charges recomputed at every candidate exit (STT moves with the exit premium).

Usage:  python exit_level_sweep.py [--per-lot] [--csv out.csv]
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
QTY, LOTS = 130, 2
WING, ENTRY_T, EXIT_T, BREACH_PCT = 400, "09:35", "15:00", 0.55
STOPS = [None, -1500, -2000, -2500, -3000, -4000]
TARGETS = [None, 750, 1000, 1500, 2000, 2500]
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


def load_day(day):
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
    return idx, legs, (exp - datetime.strptime(day, "%Y-%m-%d").date()).days


def build(idx, legs):
    """-> (need, entry, sign, premium) or None."""
    atm = int(round(idx[ENTRY_T] / 100.0)) * 100
    need = [(atm, "CE"), (atm, "PE"), (atm + WING, "CE"), (atm - WING, "PE")]
    if any(k not in legs or ENTRY_T not in legs[k] for k in need):
        return None
    entry = {k: legs[k][ENTRY_T] for k in need}
    sign = {need[0]: -1, need[1]: -1, need[2]: +1, need[3]: +1}
    prem = sum(-sign[k] * entry[k] for k in need) * QTY
    return (atm, need, entry, sign, prem) if prem > 0 else None


def net_at(need, entry, sign, marks):
    gross = sum(sign[k] * QTY * (marks[k] - entry[k]) for k in need)
    f = []
    for k in need:
        short = sign[k] < 0
        f += [{"action": "SELL" if short else "BUY", "quantity": QTY,
               "price": entry[k], "orderid": f"i{k}"},
              {"action": "BUY" if short else "SELL", "quantity": QTY,
               "price": marks[k], "orderid": f"o{k}"}]
    return gross - chg.charges_from_fills(f, True)


def run_day(idx, legs, stop, target):
    b = build(idx, legs)
    if not b:
        return None
    atm, need, entry, sign, prem = b
    band = atm * BREACH_PCT / 100
    times = sorted(t for t in idx if ENTRY_T < t <= EXIT_T)
    for t in times:
        if any(t not in legs[k] for k in need):
            continue
        marks = {k: legs[k][t] for k in need}
        n = net_at(need, entry, sign, marks)
        if target is not None and n >= target:
            return {"net": n, "reason": "TARGET", "t": t}
        if stop is not None and n <= stop:
            return {"net": n, "reason": "STOP", "t": t}
        if abs(idx[t] - atm) >= band:
            return {"net": n, "reason": "BREACH", "t": t}
    for t in reversed(times):
        if all(t in legs[k] for k in need):
            return {"net": net_at(need, entry, sign, {k: legs[k][t] for k in need}),
                    "reason": "EOD", "t": t}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-lot", action="store_true")
    ap.add_argument("--csv")
    a = ap.parse_args()
    div = LOTS if a.per_lot else 1
    unit = "per LOT" if a.per_lot else f"per trade ({LOTS} lots)"

    days = []
    for d in sorted(x for x in os.listdir(DATA_DIR) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x)):
        got = load_day(d)
        if got and got[2] >= 1:                      # exclude 0 DTE — we never trade it
            days.append((d, *got))
    print(f"\n  === exit-level sweep · {len(days)} replayable days (0-DTE excluded) · {unit} ===")
    print(f"  entry {ENTRY_T} · ATM +/-{WING} wings · breach {BREACH_PCT}% · EOD {EXIT_T}")
    print("  bar closes, NOT fills — compare LEVELS, not absolute rupees\n")

    rows, grid = [], {}
    for stop in STOPS:
        for tgt in TARGETS:
            res = []
            for d, idx, legs, dte in days:
                r = run_day(idx, legs, stop, tgt)
                if r:
                    res.append(r)
                    rows.append({"date": d, "dte": dte, "stop": stop, "target": tgt, **r})
            if res:
                nets = [r["net"] / div for r in res]
                grid[(stop, tgt)] = {
                    "n": len(nets), "mean": st.mean(nets), "median": st.median(nets),
                    "worst": min(nets), "win": 100 * sum(1 for x in nets if x > 0) / len(nets),
                    "stopped": sum(1 for r in res if r["reason"] == "STOP"),
                    "targeted": sum(1 for r in res if r["reason"] == "TARGET")}

    def lbl(v, w=6):
        return f"{'none':>{w}}" if v is None else f"{v:>{w},}"
    print("  MEAN NET per day, by stop (rows) x target (cols):")
    print("   stop  \\ tgt " + "".join(lbl(t, 9) for t in TARGETS))
    for stop in STOPS:
        line = f"   {lbl(stop)}    "
        for t in TARGETS:
            g = grid.get((stop, t))
            line += f"{g['mean']:>+9,.0f}" if g else f"{'-':>9}"
        cur = "   <- live had -1,500" if stop == -1500 else ""
        print(line + cur)
    print("\n  WORST DAY, same layout (the tail you are buying protection against):")
    print("   stop  \\ tgt " + "".join(lbl(t, 9) for t in TARGETS))
    for stop in STOPS:
        line = f"   {lbl(stop)}    "
        for t in TARGETS:
            g = grid.get((stop, t))
            line += f"{g['worst']:>+9,.0f}" if g else f"{'-':>9}"
        print(line)

    base = grid.get((None, None))
    print(f"\n  BASELINE (no stop, no target — breach + EOD only): mean {base['mean']:+,.0f} · "
          f"median {base['median']:+,.0f} · worst {base['worst']:+,.0f} · win {base['win']:.0f}%")
    ranked = sorted(grid.items(), key=lambda kv: kv[1]["mean"], reverse=True)[:6]
    print("\n  top 6 by mean — REMEMBER these are 36 cells on ~40 days; the winner is likely noise:")
    print("     stop    target      mean    median     worst   win%   stopped/targeted")
    for (s, t), g in ranked:
        print(f"   {lbl(s)}  {lbl(t,8)}  {g['mean']:>+8,.0f}  {g['median']:>+8,.0f}  "
              f"{g['worst']:>+8,.0f}   {g['win']:>3.0f}%      {g['stopped']}/{g['targeted']}")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        print(f"\n  rows -> {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
