#!/usr/bin/env python
"""
Offline backtester — runs both strategies against locally captured trade data.
No API calls needed. Reads CSVs from /root/data/zerodha/trade-data/<date>/.

Usage:
    python backtest_offline.py 2026-05-14
    python backtest_offline.py 2026-05-14 --straddle-only
    python backtest_offline.py 2026-05-14 --ema-only
"""
import json
import os
import sys
from pathlib import Path

import pandas as pd

DATE = sys.argv[1] if len(sys.argv) > 1 else None
if not DATE:
    print("Usage: python backtest_offline.py <YYYY-MM-DD> [--straddle-only] [--ema-only]")
    sys.exit(1)

FLAGS = set(sys.argv[2:])
RUN_STRADDLE = "--ema-only" not in FLAGS
RUN_EMA = "--straddle-only" not in FLAGS

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("TRADE_DATA_DIR", SCRIPT_DIR / "backtest_data")) / DATE

if not BASE_DIR.exists():
    print(f"ERROR: No data for {DATE} at {BASE_DIR}")
    sys.exit(1)

META_FILE = BASE_DIR / "metadata.json"
if not META_FILE.exists():
    print(f"ERROR: No metadata.json in {BASE_DIR}")
    sys.exit(1)

meta = json.loads(META_FILE.read_text())
print(f"{'='*65}")
print(f"  OFFLINE BACKTEST — {DATE}")
print(f"{'='*65}")
print(f"  NIFTY: open {meta.get('nifty_open')} | high {meta.get('nifty_high')} "
      f"| low {meta.get('nifty_low')} | close {meta.get('nifty_close')}")
print(f"  VIX: open {meta.get('vix_open')} | close {meta.get('vix_close')}")
print(f"  ATM strike: {meta.get('atm_strike')} | Expiry: {meta.get('nifty_expiry')}")


def load_csv(filepath):
    if not filepath.exists():
        return None
    df = pd.read_csv(filepath, index_col=0, parse_dates=True)
    return df


def price_at(df, time_str):
    matches = df[df.index.strftime("%H:%M") == time_str]
    if len(matches) > 0:
        return float(matches.iloc[0]["close"])
    return None


# =========================================================================
# 1. IRON BUTTERFLY BACKTEST
# =========================================================================
def run_straddle_backtest():
    print(f"\n{'='*65}")
    print(f"  STRATEGY 1: IRON BUTTERFLY (Short Straddle + OTM Hedge)")
    print(f"{'='*65}")

    LOTS = 3
    LOT_SIZE = 65
    QTY = LOTS * LOT_SIZE
    PROFIT_TARGET_PCT = 25.0
    STOPLOSS_PCT = 50.0
    ENTRY_TIME = "09:35"
    SQUAREOFF_TIME = "15:14"

    atm = meta.get("atm_strike")
    expiry_tag = meta.get("nifty_expiry_tag")

    # Try different hedge widths
    for offset, pts in [(4, 200), (8, 400)]:
        ce_sym = f"NIFTY{expiry_tag}{atm}CE"
        pe_sym = f"NIFTY{expiry_tag}{atm}PE"
        hce_sym = f"NIFTY{expiry_tag}{atm + pts}CE"
        hpe_sym = f"NIFTY{expiry_tag}{atm - pts}PE"

        ce = load_csv(BASE_DIR / "options" / f"{ce_sym}_1m.csv")
        pe = load_csv(BASE_DIR / "options" / f"{pe_sym}_1m.csv")
        hce = load_csv(BASE_DIR / "options" / f"{hce_sym}_1m.csv")
        hpe = load_csv(BASE_DIR / "options" / f"{hpe_sym}_1m.csv")

        if any(d is None for d in [ce, pe, hce, hpe]):
            missing = [s for s, d in [(ce_sym, ce), (pe_sym, pe), (hce_sym, hce), (hpe_sym, hpe)] if d is None]
            print(f"\n  OTM{offset} ({pts}pts): SKIP — missing data: {missing}")
            continue

        ce_e = price_at(ce, ENTRY_TIME)
        pe_e = price_at(pe, ENTRY_TIME)
        hce_e = price_at(hce, ENTRY_TIME)
        hpe_e = price_at(hpe, ENTRY_TIME)

        if any(v is None for v in [ce_e, pe_e, hce_e, hpe_e]):
            print(f"\n  OTM{offset} ({pts}pts): SKIP — no price at {ENTRY_TIME}")
            continue

        gross = (ce_e + pe_e) * QTY
        hedge_cost = (hce_e + hpe_e) * QTY
        net_prem = gross - hedge_cost
        hedge_pct = hedge_cost / gross * 100

        print(f"\n  --- OTM{offset} ({pts}pts) | {QTY} qty | PT:{PROFIT_TARGET_PCT}% SL:{STOPLOSS_PCT}% ---")
        print(f"  SELL CE @ {ce_e:.2f} | SELL PE @ {pe_e:.2f}")
        print(f"  BUY  CE @ {hce_e:.2f} | BUY  PE @ {hpe_e:.2f}")
        print(f"  Gross: {gross:,.0f} | Hedge: {hedge_cost:,.0f} ({hedge_pct:.0f}%) | Net: {net_prem:,.0f}")
        print(f"  Target: +{net_prem * PROFIT_TARGET_PCT / 100:,.0f} | SL: -{net_prem * STOPLOSS_PCT / 100:,.0f}")

        # Walk minute by minute
        entry_idx = ce[ce.index.strftime("%H:%M") == ENTRY_TIME].index
        if len(entry_idx) == 0:
            continue
        start_pos = ce.index.get_loc(entry_idx[0])

        exit_time = None
        exit_reason = None
        exit_pnl = None
        peak_pnl = float("-inf")
        peak_time = ""
        trough_pnl = float("inf")
        trough_time = ""

        for i in range(start_pos, len(ce)):
            ts = ce.index[i]
            t = ts.strftime("%H:%M")

            ce_now = float(ce.iloc[i]["close"])
            pe_now = float(pe.iloc[i]["close"]) if i < len(pe) else pe_e
            hce_now = float(hce.iloc[i]["close"]) if i < len(hce) else hce_e
            hpe_now = float(hpe.iloc[i]["close"]) if i < len(hpe) else hpe_e

            short_pnl = ((ce_e - ce_now) + (pe_e - pe_now)) * QTY
            hedge_pnl = ((hce_now - hce_e) + (hpe_now - hpe_e)) * QTY
            total_pnl = short_pnl + hedge_pnl
            pnl_pct = total_pnl / net_prem * 100 if net_prem > 0 else 0

            if total_pnl > peak_pnl:
                peak_pnl = total_pnl
                peak_time = t
            if total_pnl < trough_pnl:
                trough_pnl = total_pnl
                trough_time = t

            if pnl_pct >= PROFIT_TARGET_PCT:
                exit_time, exit_reason, exit_pnl, exit_pct = t, "PROFIT_TARGET", total_pnl, pnl_pct
                break
            if pnl_pct <= -STOPLOSS_PCT:
                exit_time, exit_reason, exit_pnl, exit_pct = t, "STOPLOSS", total_pnl, pnl_pct
                break
            if ts.hour > 15 or (ts.hour == 15 and ts.minute >= 14):
                exit_time, exit_reason, exit_pnl, exit_pct = t, "EOD_SQUAREOFF", total_pnl, pnl_pct
                break

        if exit_reason is None:
            t = ce.index[-1].strftime("%H:%M")
            ce_last = float(ce.iloc[-1]["close"])
            pe_last = float(pe.iloc[-1]["close"])
            hce_last = float(hce.iloc[-1]["close"])
            hpe_last = float(hpe.iloc[-1]["close"])
            sp = ((ce_e - ce_last) + (pe_e - pe_last)) * QTY
            hp = ((hce_last - hce_e) + (hpe_last - hpe_e)) * QTY
            exit_pnl = sp + hp
            exit_pct = exit_pnl / net_prem * 100 if net_prem > 0 else 0
            exit_time, exit_reason = t, "END_OF_DATA"

        sign = "+" if exit_pnl >= 0 else ""
        pk = "+" if peak_pnl >= 0 else ""
        tr = "+" if trough_pnl >= 0 else ""
        print(f"  Exit: {exit_time} | {exit_reason} | P&L: {sign}{exit_pnl:,.0f} ({sign}{exit_pct:.1f}%)")
        print(f"  Peak: {pk}{peak_pnl:,.0f} at {peak_time} | Trough: {tr}{trough_pnl:,.0f} at {trough_time}")


# =========================================================================
# 2. EMA CROSSOVER BACKTEST
# =========================================================================
def run_ema_backtest():
    print(f"\n{'='*65}")
    print(f"  STRATEGY 2: BANKNIFTY EMA(9/21) CROSSOVER")
    print(f"{'='*65}")

    QTY = 60  # 2 lots x 30
    EMA_FAST = 9
    EMA_SLOW = 21
    VOL_MULT = 1.5
    VOL_SMA = 20
    TRAILING_SL_PCT = 0.5

    data_today = load_csv(BASE_DIR / "banknifty_fut_5m.csv")
    if data_today is None:
        print("  SKIP — no BANKNIFTY futures data")
        return

    # Load previous day's data for EMA warmup (live strategy fetches multi-day history)
    from datetime import datetime, timedelta
    prev_date = (datetime.strptime(DATE, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    warmup_dir = SCRIPT_DIR / "backtest_data" / prev_date
    prev_data = load_csv(warmup_dir / "banknifty_fut_5m.csv") if warmup_dir.exists() else None
    warmup_candles = 0
    if prev_data is not None:
        data = pd.concat([prev_data, data_today])
        warmup_candles = len(prev_data)
    else:
        data = data_today

    data["ema9"] = data["close"].ewm(span=EMA_FAST, adjust=False).mean()
    data["ema21"] = data["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    data["vol_sma"] = data["volume"].rolling(window=VOL_SMA).mean()

    warmup_note = f" (+ {warmup_candles} warmup from {prev_date})" if warmup_candles else " (NO warmup — EMAs cold-start!)"
    print(f"  Data: {len(data_today)} candles (5m){warmup_note}")
    print(f"  EMA({EMA_FAST}/{EMA_SLOW}) | Qty: {QTY} | Vol filter: >{VOL_MULT}x SMA({VOL_SMA})")
    print(f"  Trailing SL: {TRAILING_SL_PCT}%")

    # Find crossovers on the target date only (warmup feeds EMA accuracy)
    today_mask = data.index >= f"{DATE} 09:15"
    today_indices = data.index[today_mask]
    all_crossovers = []
    for ts in today_indices:
        idx = data.index.get_loc(ts)
        if idx < 1:
            continue
        pf = data.iloc[idx - 1]["ema9"]
        ps = data.iloc[idx - 1]["ema21"]
        cf = data.iloc[idx]["ema9"]
        cs = data.iloc[idx]["ema21"]
        vol = float(data.iloc[idx]["volume"])
        vol_sma = float(data.iloc[idx]["vol_sma"]) if pd.notna(data.iloc[idx]["vol_sma"]) else 0
        vol_ok = vol > VOL_MULT * vol_sma if vol_sma > 0 else False
        if pf < ps and cf >= cs:
            all_crossovers.append(("BUY", ts, float(data.iloc[idx]["close"]), vol, vol_sma, vol_ok))
        elif pf >= ps and cf < cs:
            all_crossovers.append(("SELL", ts, float(data.iloc[idx]["close"]), vol, vol_sma, vol_ok))

    if not all_crossovers:
        print("  No crossovers detected — no trades")
        return

    print(f"\n  All Crossovers (with volume check):")
    for sig, ts, price, vol, vsma, vok in all_crossovers:
        status = "PASS" if vok else "REJECTED"
        ratio = vol / vsma if vsma > 0 else 0
        print(f"    {sig} at {ts.strftime('%H:%M')} | Price: {price:.2f} | Vol: {vol:.0f} vs {VOL_MULT}x SMA: {vsma * VOL_MULT:.0f} ({ratio:.2f}x) -> {status}")

    # Use only today's candles for trade simulation
    data_sim = data[data.index >= f"{DATE} 09:15"]

    # --- Run A: WITHOUT volume filter ---
    print(f"\n  {'─'*60}")
    print(f"  RUN A: WITHOUT volume filter (all crossovers traded)")
    print(f"  {'─'*60}")
    _run_ema_trades([(s, t, p) for s, t, p, _, _, _ in all_crossovers], data_sim, QTY, TRAILING_SL_PCT)

    # --- Run B: WITH volume filter ---
    filtered = [(s, t, p) for s, t, p, _, _, vok in all_crossovers if vok]
    print(f"\n  {'─'*60}")
    print(f"  RUN B: WITH volume filter (>{VOL_MULT}x SMA({VOL_SMA}))")
    print(f"  {'─'*60}")
    if not filtered:
        print("    No crossovers passed volume filter — no trades")
    else:
        _run_ema_trades(filtered, data_sim, QTY, TRAILING_SL_PCT)


def _run_ema_trades(crossovers, data, qty, trailing_sl_pct):
    trades = []
    position = None  # (direction, entry_price, entry_time, peak_price, trailing_sl)

    def close_position(pos, exit_price, exit_time_str, reason):
        entry_dir, entry_price, entry_time, _, _ = pos
        if entry_dir == "BUY":
            pnl = (exit_price - entry_price) * qty
        else:
            pnl = (entry_price - exit_price) * qty
        trade = {
            "entry": entry_dir,
            "entry_time": entry_time.strftime("%H:%M"),
            "entry_price": entry_price,
            "exit_time": exit_time_str,
            "exit_price": exit_price,
            "pnl": pnl,
            "reason": reason,
        }
        trades.append(trade)
        sign = "+" if pnl >= 0 else ""
        print(f"    {entry_dir} @ {entry_price:.2f} ({entry_time.strftime('%H:%M')}) "
              f"-> {reason} @ {exit_price:.2f} ({exit_time_str}) | P&L: {sign}{pnl:,.0f}")
        return pnl

    # Walk candle by candle to handle trailing SL between crossovers
    cross_idx = 0
    cross_times = {c[1]: c for c in crossovers}

    for i in range(1, len(data)):
        ts = data.index[i]
        row = data.iloc[i]

        # EOD check at 15:14
        if ts.hour > 15 or (ts.hour == 15 and ts.minute >= 14):
            if position:
                close_position(position, float(row["close"]), ts.strftime("%H:%M"), "EOD")
                position = None
            break

        # Trailing SL check
        if position:
            d, ep, et, peak, sl = position
            if d == "BUY":
                if float(row["high"]) > peak:
                    peak = float(row["high"])
                    sl = round(peak * (1 - trailing_sl_pct / 100), 2)
                    position = (d, ep, et, peak, sl)
                if float(row["low"]) <= sl:
                    close_position(position, sl, ts.strftime("%H:%M"), "TRAILING_SL")
                    position = None
            else:
                if float(row["low"]) < peak:
                    peak = float(row["low"])
                    sl = round(peak * (1 + trailing_sl_pct / 100), 2)
                    position = (d, ep, et, peak, sl)
                if float(row["high"]) >= sl:
                    close_position(position, sl, ts.strftime("%H:%M"), "TRAILING_SL")
                    position = None

        # Crossover signal at this candle?
        if ts in cross_times:
            sig, _, price = cross_times[ts]
            if position and position[0] != sig:
                close_position(position, price, ts.strftime("%H:%M"), "REVERSE")
                position = None
            if not position:
                ep = price
                peak = ep
                if sig == "BUY":
                    sl = round(ep * (1 - trailing_sl_pct / 100), 2)
                else:
                    sl = round(ep * (1 + trailing_sl_pct / 100), 2)
                position = (sig, ep, ts, peak, sl)

    # Close any remaining position at last candle
    if position:
        last = data.iloc[-1]
        close_position(position, float(last["close"]), data.index[-1].strftime("%H:%M"), "END_OF_DATA")
        position = None

    if not trades:
        print("    No trades")
        return

    total = sum(t["pnl"] for t in trades)
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] <= 0]
    sign = "+" if total >= 0 else ""
    print(f"\n    Summary: {sign}{total:,.0f} INR | {len(trades)} trade(s) | W:{len(winners)} L:{len(losers)}")


# =========================================================================
# MAIN
# =========================================================================
if RUN_STRADDLE:
    run_straddle_backtest()

if RUN_EMA:
    run_ema_backtest()

print(f"\n{'='*65}")
print(f"  Data source: {BASE_DIR} (offline)")
print(f"{'='*65}")
