#!/usr/bin/env python3
"""backtest_straddle_history.py — replay the live iron-butterfly straddle over the
captured option-chain history in ~/data/zerodha/trade-data, day by day.

Self-contained: reads ONLY the per-day capture folders (no broker API), so it can
run offline. Mirrors the LIVE short_straddle_nifty params + gates:

  entry 09:35 | ATM CE+PE SELL + OTM8 (±400) CE+PE BUY (iron butterfly)
  3 lots x 65 = 195 qty | profit target +25% / stop-loss -50% of net premium
  gates: skip expiry day | |gap| < 1.0% | India VIX < 25 | ORB(15m) breakout < 0.5%
  exit: PROFIT_TARGET / STOPLOSS / EOD 15:14

Per-day capture folder layout (date/):
  metadata.json            date, nifty_expiry(_tag), atm_strike, nifty_open/close, vix_*
  nifty_index_1m.csv       index OHLC (gap, ORB)
  india_vix_5m.csv         VIX OHLC (VIX gate)
  options/NIFTY<tag><K><CE|PE>_1m.csv   per-leg OHLC

Usage:  python backtest_straddle_history.py [--data-dir DIR] [--csv OUT.csv]
"""
import argparse
import csv as csvmod
import json
import os
from datetime import datetime
from pathlib import Path

import charges as chg

LOT_SIZE, LOTS = 65, 3
QTY = LOT_SIZE * LOTS                 # 195
PROFIT_TARGET_PCT, STOPLOSS_PCT = 25.0, 50.0
ENTRY_HHMM, EOD_HHMM = "09:35", "15:14"
HEDGE_OFFSET_PTS = 400               # OTM8 = 8 strikes x 50pt
GAP_THRESHOLD_PCT, VIX_THRESHOLD = 1.0, 25.0
ORB_MIN, ORB_BREAKOUT_PCT = 15, 0.5

DATA_DIR = Path(os.getenv("BACKTEST_TRADE_DATA", "/root/data/zerodha/trade-data"))


def _series(path):
    """Read a 1m/5m OHLC csv -> {'HH:MM': close} plus an ordered list of (hhmm, row)."""
    if not path.exists():
        return {}, []
    out, rows = {}, []
    with open(path) as f:
        for r in csvmod.DictReader(f):
            ts = r["timestamp"][11:16]      # 'YYYY-MM-DD HH:MM:SS+...' -> 'HH:MM'
            out[ts] = float(r["close"])
            rows.append((ts, r))
    return out, rows


def _at_or_after(series_rows, hhmm):
    for ts, r in series_rows:
        if ts >= hhmm:
            return ts, r
    return None, None


def simulate_day(day_dir, prev_close):
    """Return a dict result for one capture day (or a skip reason)."""
    meta = json.load(open(day_dir / "metadata.json"))
    date = meta["date"]
    if not meta.get("atm_strike") or not meta.get("nifty_expiry_tag"):
        return {"date": date, "traded": False, "reason": "no-entry (holiday/partial capture)"}
    tag = meta["nifty_expiry_tag"]
    atm = int(meta["atm_strike"])

    # --- gate: skip expiry day ---
    exp = datetime.strptime(meta["nifty_expiry"], "%d-%b-%y").strftime("%Y-%m-%d")
    if exp == date:
        return {"date": date, "traded": False, "reason": "expiry-day"}

    idx_close, idx_rows = _series(day_dir / "nifty_index_1m.csv")
    if not idx_rows:
        return {"date": date, "traded": False, "reason": "no-index-data"}

    # --- gate: gap (today open vs prev session close) ---
    nopen = float(meta.get("nifty_open") or idx_rows[0][1]["open"])
    if prev_close:
        gap_pct = abs(nopen - prev_close) / prev_close * 100
        if gap_pct >= GAP_THRESHOLD_PCT:
            return {"date": date, "traded": False, "reason": f"gap {gap_pct:.2f}%"}

    # --- gate: VIX < threshold (at/just after entry) ---
    vix_close, vix_rows = _series(day_dir / "india_vix_5m.csv")
    _, vrow = _at_or_after(vix_rows, ENTRY_HHMM)
    vix = float(vrow["close"]) if vrow else float(meta.get("vix_open", 0))
    if vix and vix >= VIX_THRESHOLD:
        return {"date": date, "traded": False, "reason": f"VIX {vix:.1f}"}

    # --- gate: ORB(15m) breakout (09:15..09:30) ---
    orb = [r for ts, r in idx_rows if "09:15" <= ts < "09:30"]
    if orb:
        oh = max(float(r["high"]) for r in orb)
        ol = min(float(r["low"]) for r in orb)
        _, erow = _at_or_after(idx_rows, ENTRY_HHMM)
        px = float(erow["close"]) if erow else nopen
        bo = max((px - oh) / oh, (ol - px) / ol) * 100 if oh and ol else 0
        if bo > ORB_BREAKOUT_PCT:
            return {"date": date, "traded": False, "reason": f"ORB breakout {bo:.2f}%"}

    # --- build the four legs ---
    def leg(strike, opt):
        return _series(day_dir / "options" / f"NIFTY{tag}{strike}{opt}_1m.csv")
    ce_c, _ = leg(atm, "CE")
    pe_c, _ = leg(atm, "PE")
    hce_c, _ = leg(atm + HEDGE_OFFSET_PTS, "CE")
    hpe_c, _ = leg(atm - HEDGE_OFFSET_PTS, "PE")
    legs = {"ce": ce_c, "pe": pe_c, "hce": hce_c, "hpe": hpe_c}
    if not all(d.get(ENTRY_HHMM) for d in legs.values()):
        return {"date": date, "traded": False, "reason": "missing-leg-data@entry"}

    e = {k: d[ENTRY_HHMM] for k, d in legs.items()}
    net_premium = (e["ce"] + e["pe"] - e["hce"] - e["hpe"]) * QTY

    # --- walk minutes 09:36..15:14, P&L = short decay + hedge change ---
    minutes = sorted(t for t in ce_c if ENTRY_HHMM < t <= EOD_HHMM)
    pt = net_premium * PROFIT_TARGET_PCT / 100
    sl = net_premium * STOPLOSS_PCT / 100
    final_pnl, exit_reason, exit_t = None, "EOD", EOD_HHMM
    peak = trough = 0.0
    for t in minutes:
        if not all(t in d for d in legs.values()):
            continue
        short_pnl = ((e["ce"] - ce_c[t]) + (e["pe"] - pe_c[t])) * QTY
        hedge_pnl = ((hce_c[t] - e["hce"]) + (hpe_c[t] - e["hpe"])) * QTY
        pnl = short_pnl + hedge_pnl
        peak, trough = max(peak, pnl), min(trough, pnl)
        if pnl >= pt:
            final_pnl, exit_reason, exit_t = pnl, "PROFIT_TARGET", t; break
        if pnl <= -sl:
            final_pnl, exit_reason, exit_t = pnl, "STOPLOSS", t; break
        final_pnl = pnl
    if final_pnl is None:
        final_pnl = 0.0
    rt_charges = chg.options_iron_butterfly_roundtrip(e["ce"], e["pe"], e["hce"], e["hpe"], QTY)
    return {"date": date, "traded": True, "atm": atm, "net_premium": round(net_premium),
            "pnl": round(final_pnl), "charges": round(rt_charges), "net_pnl": round(final_pnl - rt_charges),
            "pnl_pct": round(final_pnl / net_premium * 100, 1) if net_premium else 0,
            "exit_reason": exit_reason, "exit_t": exit_t,
            "peak": round(peak), "trough": round(trough)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--csv", default=None, help="append per-day rows to this generic CSV")
    args = ap.parse_args()
    root = Path(args.data_dir)
    days = sorted(p for p in root.iterdir() if p.is_dir() and (p / "metadata.json").exists())

    # prev-close map for the gap gate
    prev_close = {}
    last = None
    for d in days:
        m = json.load(open(d / "metadata.json"))
        prev_close[m["date"]] = last
        last = float(m.get("nifty_close") or 0) or last

    print(f"Straddle backtest (iron butterfly, live params) | dir={root} | {len(days)} days\n")
    print(f"{'date':<12}{'result':>10}{'premium':>9}{'P&L':>9}{'%':>7}  exit / skip-reason")
    print("-" * 74)
    rows, total, traded_n, wins = [], 0, 0, 0
    for d in days:
        r = simulate_day(d, prev_close.get(json.load(open(d / 'metadata.json'))["date"]))
        if r["traded"]:
            total += r["pnl"]; traded_n += 1; wins += 1 if r["pnl"] > 0 else 0
            print(f"{r['date']:<12}{'TRADE':>10}{r['net_premium']:>9}{r['pnl']:>+9}{r['pnl_pct']:>7.1f}  "
                  f"{r['exit_reason']}@{r['exit_t']} (peak {r['peak']:+}, trough {r['trough']:+})")
        else:
            print(f"{r['date']:<12}{'skip':>10}{'-':>9}{'-':>9}{'-':>7}  {r['reason']}")
        rows.append(r)
    print("-" * 74)
    L = traded_n - wins
    print(f"TOTAL: {total:+} INR | {traded_n} trades (W:{wins} L:{L}) | {len(days)-traded_n} skips")

    if args.csv:
        write_generic_csv(args.csv, rows)
        print(f"\nappended {traded_n} straddle rows -> {args.csv}")


def write_generic_csv(path, rows):
    """Append straddle rows to the generic cross-strategy CSV (idempotent per date+strategy+variant)."""
    cols = ["date", "regime_label", "strategy", "variant", "timeframe", "params",
            "trades", "wins", "losses", "pnl", "exit_breakdown", "notes"]
    existing = set()
    p = Path(path)
    if p.exists():
        with open(p) as f:
            for row in csvmod.DictReader(f):
                existing.add((row["date"], row["strategy"], row["variant"]))
    newrows = []
    for r in rows:
        if not r["traded"]:
            key = (r["date"], "STRADDLE", "iron_butterfly")
            if key in existing:
                continue
            newrows.append({"date": r["date"], "regime_label": "", "strategy": "STRADDLE",
                            "variant": "iron_butterfly", "timeframe": "-",
                            "params": "ATM;OTM8;PT25;SL50;0935", "trades": 0, "wins": 0,
                            "losses": 0, "pnl": 0, "exit_breakdown": "",
                            "notes": f"skip:{r['reason']}"})
            continue
        key = (r["date"], "STRADDLE", "iron_butterfly")
        if key in existing:
            continue
        newrows.append({"date": r["date"], "regime_label": "", "strategy": "STRADDLE",
                        "variant": "iron_butterfly", "timeframe": "-",
                        "params": "ATM;OTM8;PT25;SL50;0935", "trades": 1,
                        "wins": 1 if r["pnl"] > 0 else 0, "losses": 0 if r["pnl"] > 0 else 1,
                        "pnl": r["pnl"], "exit_breakdown": f"{r['exit_reason']}:1",
                        "notes": f"premium {r['net_premium']}; {r['pnl_pct']}%"})
    write_header = not p.exists()
    with open(p, "a", newline="") as f:
        w = csvmod.DictWriter(f, fieldnames=cols)
        if write_header:
            w.writeheader()
        w.writerows(newrows)


if __name__ == "__main__":
    main()
