#!/usr/bin/env python3
"""build_generic_csv.py — assemble the GENERIC cross-strategy comparison CSV (CSV#2):
one row per (date x strategy x variant), covering EMA crossover variants, the EMA
regime follower, and the short straddle — all from the validated backtest engines
+ the existing EMA history CSV. Strategy-agnostic schema so any strategy fits.

Sources:
  EMA_CROSSOVER  reshaped from options_history.csv (the EMA-specific CSV#1)
  EMA_REGIME     backtest_ticks.simulate_day with the regime config (5/13, ER 0.60/0.40)
  STRADDLE       backtest_straddle_history.simulate_day over ~/data/zerodha/trade-data

Usage (on server):
  cd /root/data/openalgo/strategies/scripts
  BACKTEST_DATA_DIR=/root/data/openalgo/log/market_data_capture \
    /root/data/openalgo/.venv/bin/python build_generic_csv.py --out strategies_comparison.csv
"""
import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import backtest_ticks as bt
import backtest_straddle_history as bs
import charges as chg

COLS = ["date", "regime_label", "strategy", "variant", "timeframe", "params",
        "trades", "wins", "losses", "gross_pnl", "charges", "net_pnl", "exit_breakdown", "notes"]

# EMA crossover + regime trade BANKNIFTY futures. Notional is ~constant over the window, so a
# representative price gives accurate per-trade charges (STT 0.05% sell-side dominates).
REP_FUT_PRICE, FUT_QTY = 57800.0, 60
FUT_RT_CHARGE = chg.futures_roundtrip(REP_FUT_PRICE, FUT_QTY)  # ~Rs2,008 per futures round-trip

# Regime config = the validated CLI run: --tf 3 --fast 5 --slow 13 --er-gate 0.60
# --er-window-min 60 --er-exit 0.40 (all other er/mfe flags at their CLI defaults).
REGIME_CFG = {
    "tf": 3, "fast": 5, "slow": 13, "warmup": 9, "vol_sma": 10, "vol_mult": 1.5,
    "reverse_confirm_pct": bt.REVERSE_CONFIRM_PCT, "gap_gate": -1.0, "early": False,
    "tp_per_lot": None, "sl_per_lot": None,
    "er_gate": 0.60, "er_window_min": 60, "er_exit": 0.40, "er_appe": False,
    "er_tsl": False, "er_tsl_wide_er": 0.70, "er_dynamic": False,
    "mfe_scale": 150.0, "mfe_trail_frac": 0.30, "mfe_trail_min": 15.0, "er_exit_high": None,
}


def _exits(trades):
    return ";".join(f"{k}:{v}" for k, v in sorted(Counter(t["reason"] for t in trades).items()))


# Map the backtester's variation names -> our Opt1..Opt5 labels (Opt5 = original 9/21 5m).
def _opt_label(option):
    o = option.strip()
    if o.startswith("BASELINE"):
        return "Opt5 EMA 9/21 5m (original)"
    for pfx, lbl in (("VAR1", "Opt1 EMA 9/21 3m (LIVE)"), ("VAR2", "Opt2 EMA 9/21 2m+5mTrend"),
                     ("VAR3", "Opt3 EMA 9/21 2m"), ("VAR4", "Opt4 EMA 7/15 3m")):
        if o.startswith(pfx):
            return lbl
    return o


def ema_rows(hist_csv):
    p = Path(hist_csv)
    if not p.exists():
        return []
    out = []
    for r in csv.DictReader(open(p)):
        t = int(r.get("trades", "0") or 0)
        gross = float(r.get("pnl", "0") or 0)
        ch = round(t * FUT_RT_CHARGE)
        out.append({
            "date": r["date"], "regime_label": r.get("regime", ""),
            "strategy": "EMA_CROSSOVER", "variant": _opt_label(r.get("option", "")),
            "timeframe": r.get("tf", ""),
            "params": f"ema{r.get('ema','')};gap{r.get('gap_pct','')}%;"
                      f"slope{r.get('slope_bars','')};volSMA{r.get('vol_sma','')};"
                      f"tsl{r.get('tsl','')};arm{r.get('arm','')}",
            "trades": t, "wins": r.get("W", "0"), "losses": r.get("L", "0"),
            "gross_pnl": round(gross), "charges": ch, "net_pnl": round(gross - ch),
            "exit_breakdown": r.get("exits", ""), "notes": r.get("notes", ""),
        })
    return out


def regime_rows():
    out = []
    days = sorted(p.name for p in bt.DATA_DIR.iterdir() if p.is_dir())
    for day in days:
        ticks = bt.load_ticks(day)
        if not ticks:
            continue
        trades, _ = bt.simulate_day(ticks, REGIME_CFG, False)
        total = sum(t["pnl"] for t in trades)
        w = sum(1 for t in trades if t["pnl"] > 0)
        ch = round(len(trades) * FUT_RT_CHARGE)
        out.append({
            "date": day, "regime_label": "", "strategy": "EMA_REGIME",
            "variant": "5/13 ER0.60/0.40 (LIVE)", "timeframe": "3m",
            "params": "er_gate0.60;er_window60min;er_exit0.40;5/13EMA",
            "trades": len(trades), "wins": w, "losses": len(trades) - w,
            "gross_pnl": round(total), "charges": ch, "net_pnl": round(total - ch),
            "exit_breakdown": _exits(trades),
            "notes": "" if trades else "no-trade (ER gate kept out)",
        })
    return out


def straddle_rows():
    root = bs.DATA_DIR
    days = sorted(p for p in root.iterdir() if p.is_dir() and (p / "metadata.json").exists())
    prev_close, last = {}, None
    for d in days:
        m = json.load(open(d / "metadata.json"))
        prev_close[m["date"]] = last
        last = float(m.get("nifty_close") or 0) or last
    out = []
    for d in days:
        date = json.load(open(d / "metadata.json"))["date"]
        r = bs.simulate_day(d, prev_close.get(date))
        if r["traded"]:
            net = r["net_pnl"]
            out.append({
                "date": r["date"], "regime_label": "", "strategy": "STRADDLE",
                "variant": "iron_butterfly", "timeframe": "-",
                "params": "ATM;OTM8;PT25%;SL50%;entry0935",
                "trades": 1, "wins": 1 if net > 0 else 0,
                "losses": 0 if net > 0 else 1,
                "gross_pnl": r["pnl"], "charges": r["charges"], "net_pnl": net,
                "exit_breakdown": f"{r['exit_reason']}:1",
                "notes": f"premium {r['net_premium']}; {r['pnl_pct']}%",
            })
        else:
            out.append({
                "date": r["date"], "regime_label": "", "strategy": "STRADDLE",
                "variant": "iron_butterfly", "timeframe": "-",
                "params": "ATM;OTM8;PT25%;SL50%;entry0935",
                "trades": 0, "wins": 0, "losses": 0,
                "gross_pnl": 0, "charges": 0, "net_pnl": 0,
                "exit_breakdown": "", "notes": f"skip:{r['reason']}",
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ema-csv", default="/root/data/openalgo/strategies/scripts/options_history.csv")
    ap.add_argument("--capture-dir", default="/root/data/openalgo/log/market_data_capture")
    ap.add_argument("--trade-data-dir", default="/root/data/zerodha/trade-data")
    ap.add_argument("--out", default="strategies_comparison.csv")
    args = ap.parse_args()

    bt.DATA_DIR = Path(args.capture_dir)
    bs.DATA_DIR = Path(args.trade_data_dir)

    rows = ema_rows(args.ema_csv) + regime_rows() + straddle_rows()
    order = {"EMA_CROSSOVER": 0, "EMA_REGIME": 1, "STRADDLE": 2}
    rows.sort(key=lambda r: (r["date"], order.get(r["strategy"], 9), str(r.get("variant"))))

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)

    agg = defaultdict(lambda: [0.0, 0.0, 0.0, 0])  # gross, charges, net, trades
    for r in rows:
        try:
            agg[r["strategy"]][0] += float(r["gross_pnl"])
            agg[r["strategy"]][1] += float(r["charges"])
            agg[r["strategy"]][2] += float(r["net_pnl"])
            agg[r["strategy"]][3] += int(r["trades"])
        except (ValueError, TypeError):
            pass
    print(f"wrote {len(rows)} rows -> {args.out}\n")
    print(f"{'strategy':<16}{'gross':>11}{'charges':>10}{'net':>11}{'trades':>8}")
    for s in ("EMA_CROSSOVER", "EMA_REGIME", "STRADDLE"):
        g, c, n, t = agg[s]
        print(f"{s:<16}{g:>+11,.0f}{c:>10,.0f}{n:>+11,.0f}{t:>8}")


if __name__ == "__main__":
    main()
