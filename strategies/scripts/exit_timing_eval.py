#!/usr/bin/env python3
"""exit_timing_eval.py — was exiting early right? Measure it daily instead of assuming.

We moved the straddle's square-off 15:14 -> 15:01 on 2026-08-10, then 15:01 -> 15:00 on
2026-08-19, after NSE shortened the
session (regular stock/F&O trading now ends 15:15, was 15:30). The safety case is clear-cut,
but the P&L case is not: the earlier exit forfeits some theta while dodging the closing
turbulence. So each day we replay the SAME position out to several candidate exit times off
the captured 1-minute option chain, and append the comparison to a CSV.

Why not trust the earlier 55-day study? It was fitted to the OLD 15:30-close session, where
the scramble sat in 15:15-15:30. With a 15:15 close that turbulence shifts ~15 min earlier,
so the old timing conclusions do not transfer. This rebuilds the evidence on the NEW session.

Entry is always 09:35 (the strategy's entry) at the 1m bar close, and each row is one day:
    date, atm, premium, net@15:00, net@15:01, net@15:05, net@15:10, net@15:14, best_time, best_net
Bar closes are NOT real fills, so absolute levels run optimistic (no spread crossed) — the
numbers are for COMPARING exit times on the same day, not for reconciling to broker P&L.

Usage:
    python exit_timing_eval.py                 # today, append to the CSV
    python exit_timing_eval.py 2026-08-10      # a specific date
    python exit_timing_eval.py --all           # rebuild every captured day from scratch
    python exit_timing_eval.py --report        # print the accumulated verdict
"""
import csv
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, SCRIPT_DIR)
import charges as chg  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DATA_DIR = os.getenv("BACKTEST_TRADE_DATA", "/root/data/zerodha/trade-data")
OUT_CSV = os.getenv("EXIT_TIMING_CSV", os.path.join(REPO_ROOT, "log", "exit_timing.csv"))

QTY = int(os.getenv("QUANTITY_EVAL", "130"))      # live size
ENTRY_HHMM = "09:35"
# CANDIDATES[0] is the LIVE square-off — the report and chosen_vs_best both key off it.
# 15:01 stays in the list on purpose: nine days were traded at 15:01 before the 2026-08-19
# move to 15:00, and keeping the column lets us measure the change instead of assuming it.
CANDIDATES = ["15:00", "15:01", "15:05", "15:10", "15:14"]
HEDGE_PTS = 400
COLS = ["date", "atm", "premium"] + [f"net_{t.replace(':', '')}" for t in CANDIDATES] + \
       ["best_time", "best_net", "chosen_vs_best"]


def _series(path):
    if not os.path.exists(path):
        return {}
    return {r["timestamp"][11:16]: float(r["close"]) for r in csv.DictReader(open(path))}


def evaluate(day_dir):
    """Return a row dict for one captured day, or None if it isn't evaluable."""
    meta_p = os.path.join(day_dir, "metadata.json")
    if not os.path.exists(meta_p):
        return None
    m = json.load(open(meta_p))
    atm, tag, date = m.get("atm_strike"), m.get("nifty_expiry_tag"), m.get("date")
    if not atm or not tag:
        return None
    # skip expiry day — the strategy doesn't trade it
    try:
        if datetime.strptime(m["nifty_expiry"], "%d-%b-%y").strftime("%Y-%m-%d") == date:
            return None
    except Exception:
        pass
    opt = os.path.join(day_dir, "options")
    legs = {
        "ce": _series(os.path.join(opt, f"NIFTY{tag}{atm}CE_1m.csv")),
        "pe": _series(os.path.join(opt, f"NIFTY{tag}{atm}PE_1m.csv")),
        "hce": _series(os.path.join(opt, f"NIFTY{tag}{atm + HEDGE_PTS}CE_1m.csv")),
        "hpe": _series(os.path.join(opt, f"NIFTY{tag}{atm - HEDGE_PTS}PE_1m.csv")),
    }
    if not all(d.get(ENTRY_HHMM) for d in legs.values()):
        return None
    e = {k: d[ENTRY_HHMM] for k, d in legs.items()}
    premium = (e["ce"] + e["pe"] - e["hce"] - e["hpe"]) * QTY

    row = {"date": date, "atm": atm, "premium": round(premium)}
    nets = {}
    for t in CANDIDATES:
        if not all(t in d for d in legs.values()):
            row[f"net_{t.replace(':', '')}" ] = ""
            continue
        x = {k: d[t] for k, d in legs.items()}
        gross = ((e["ce"] - x["ce"]) + (e["pe"] - x["pe"])
                 + (x["hce"] - e["hce"]) + (x["hpe"] - e["hpe"])) * QTY
        fills = [{"action": "SELL", "quantity": QTY, "price": e["ce"]},
                 {"action": "SELL", "quantity": QTY, "price": e["pe"]},
                 {"action": "BUY", "quantity": QTY, "price": e["hce"]},
                 {"action": "BUY", "quantity": QTY, "price": e["hpe"]},
                 {"action": "BUY", "quantity": QTY, "price": x["ce"]},
                 {"action": "BUY", "quantity": QTY, "price": x["pe"]},
                 {"action": "SELL", "quantity": QTY, "price": x["hce"]},
                 {"action": "SELL", "quantity": QTY, "price": x["hpe"]}]
        net = gross - chg.charges_from_fills(fills, True)
        nets[t] = net
        row[f"net_{t.replace(':', '')}" ] = round(net)
    if not nets:
        return None
    best_t = max(nets, key=nets.get)
    row["best_time"] = best_t
    row["best_net"] = round(nets[best_t])
    # what our CHOSEN exit (first candidate) gave up versus the best available
    row["chosen_vs_best"] = round(nets.get(CANDIDATES[0], 0) - nets[best_t])
    return row


def append_rows(rows, rebuild=False):
    existing = {}
    if not rebuild and os.path.exists(OUT_CSV):
        for r in csv.DictReader(open(OUT_CSV)):
            existing[r["date"]] = r
    for r in rows:
        existing[r["date"]] = r
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for d in sorted(existing):
            w.writerow({k: existing[d].get(k, "") for k in COLS})
    return len(existing)


def report():
    if not os.path.exists(OUT_CSV):
        print("no data yet")
        return
    rows = list(csv.DictReader(open(OUT_CSV)))
    print(f"  exit-timing comparison — {len(rows)} days, {QTY} qty, entry {ENTRY_HHMM}\n")
    print(f"  {'exit':<8}{'mean':>10}{'median':>10}{'best-on':>9}{'worst day':>12}")
    for t in CANDIDATES:
        k = f"net_{t.replace(':', '')}"
        vals = [float(r[k]) for r in rows if r.get(k) not in ("", None)]
        if not vals:
            continue
        best_on = sum(1 for r in rows if r.get("best_time") == t)
        print(f"  {t:<8}{statistics.mean(vals):>+10,.0f}{statistics.median(vals):>+10,.0f}"
              f"{best_on:>9}{min(vals):>+12,.0f}")
    gaps = [float(r["chosen_vs_best"]) for r in rows if r.get("chosen_vs_best") not in ("", None)]
    if gaps:
        print(f"\n  our {CANDIDATES[0]} exit vs the best-in-hindsight time:")
        print(f"    mean give-up {statistics.mean(gaps):+,.0f}  median {statistics.median(gaps):+,.0f}")
        print("    (negative = hindsight's best exit beat ours; 0 = ours WAS best)")


def main(argv):
    if "--report" in argv:
        report()
        return
    if "--all" in argv:
        rows = []
        for d in sorted(os.listdir(DATA_DIR)):
            r = evaluate(os.path.join(DATA_DIR, d))
            if r:
                rows.append(r)
        n = append_rows(rows, rebuild=True)
        print(f"rebuilt {len(rows)} evaluable days -> {OUT_CSV} ({n} rows)")
        report()
        return
    dates = [a for a in argv[1:] if not a.startswith("--")]
    date = dates[0] if dates else datetime.now(IST).strftime("%Y-%m-%d")
    r = evaluate(os.path.join(DATA_DIR, date))
    if not r:
        print(f"[skip] {date}: not evaluable (no capture / expiry / missing legs)")
        return
    append_rows([r])
    print(f"[ok] {date}: " + "  ".join(
        f"{t}={r[f'net_{t.replace(':', '')}']}" for t in CANDIDATES) +
        f"  best={r['best_time']}  vs_best={r['chosen_vs_best']}")


if __name__ == "__main__":
    main(sys.argv)
