#!/usr/bin/env python
"""
Dev backtester — compare EMA-crossover rule variations on captured tick data.
================================================================================
Reuses the live-faithful tick replayer (backtest_ticks.py): its load_ticks,
Position (APPE + trailing-SL, per-tick exits) and simulate_day. For each
variation we override the two per-variation globals it reads — TRAILING_SL_PCT
and PROFIT_ARM_THRESHOLD — then run. Var2 (5m trend + 2m entry) needs a
multi-timeframe pass, added here (simulate_mtf) reusing the same Position + gate.

Variations (vs the deployed baseline; "rest same" = same gap-gate 0.03%, close>EMA9,
EMA9 slope, volume>1.5xSMA, reverse logic, APPE give-back G, daily breaker):
  BASELINE : 5m, APPE arm ₹4,000/lot (₹8k @60q), TSL 0.5%, vol SMA(20)
  VAR1     : 3m, arm ₹2,000/lot (₹4k),  TSL 0.25%, vol SMA(33)   (~100-min vol baseline)
  VAR2     : 2m entry gated by 5m trend (EMA9 vs EMA21); arm/TSL = baseline; vol SMA(50)
  VAR3     : 2m, arm ₹1,000/lot (₹2k),  TSL 0.25%, vol SMA(50)
Volume SMA period scaled to keep a ~100-min baseline across timeframes; mult 1.5x.

Usage:  BACKTEST_DATA_DIR=/root/data/openalgo/log/market_data_capture \
        python backtest_ema_dev.py [YYYY-MM-DD]
NOTE: single-day tick capture means EMAs COLD-START (no prior-day warmup like
live's LOOKBACK_DAYS=3) — early-day signals are unreliable; treat as directional.
"""
import csv
import os
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_ticks as bt  # the live-faithful engine

bt.DATA_DIR = Path(os.getenv("BACKTEST_DATA_DIR", "/root/data/openalgo/log/market_data_capture"))


def set_globals(tsl_pct, arm_per_lot):
    """Override the two per-variation globals the Position class reads (dynamic lookup)."""
    bt.TRAILING_SL_PCT = tsl_pct
    bt.PROFIT_ARM_THRESHOLD = arm_per_lot * (bt.QTY / bt.LOT_SIZE)


def simulate_mtf(ticks, cfg):
    """Var2: 2m entry, gated by the 5m EMA9-vs-EMA21 trend. Mirrors bt.simulate_day's
    gate + per-tick exits, with an added 5m trend stream. Returns trades list."""
    e_tf, t_tf = cfg["tf"], cfg["trend_tf"]
    fa, sa = 2 / (cfg["fast"] + 1), 2 / (cfg["slow"] + 1)
    vol_n, vmult, warmup, rcp = cfg["vol_sma"], cfg["vol_mult"], cfg["warmup"], cfg["reverse_confirm_pct"]
    t_warm = cfg.get("trend_warmup", warmup)
    has_vol = any(t[3] for t in ticks)
    open_dt = ticks[0][1].replace(hour=9, minute=15, second=0, microsecond=0)
    bidx = lambda dt, tf: int((dt - open_dt).total_seconds() // (tf * 60))

    # entry-tf (2m) state
    e_idx = e_c = e_h = e_l = e_ef = e_es = e_pf = e_ps = None
    e_ticks = 0; e_cum = None; e_prevcum = ticks[0][3] if has_vol else 0.0; e_bars = 0
    vol_series = deque(maxlen=vol_n)
    slope_bars = cfg.get("slope_bars", 1)
    ef_hist = deque(maxlen=slope_bars + 1)   # EMA9 history for the slope_bars-bar slope
    e_atr = None; e_prevclose = None         # Wilder ATR on the entry timeframe
    # trend-tf (5m) state
    t_idx = t_c = t_ef = t_es = None; t_bars = 0; trend = 0  # +1 up / -1 down / 0 unknown
    pending = None; pos = None; trades = []

    def close_trade(p, xp, xdt, reason):
        pnl = ((xp - p.entry_price) if p.direction == "BUY" else (p.entry_price - xp)) * bt.QTY
        trades.append({"dir": p.direction, "entry_dt": p.entry_dt, "entry_price": p.entry_price,
                       "exit_dt": xdt, "exit_price": xp, "reason": reason, "pnl": pnl})

    def finalize_trend():
        nonlocal t_ef, t_es, t_bars, trend
        if t_ef is None:
            t_ef = t_es = t_c
        else:
            t_ef = fa * t_c + (1 - fa) * t_ef
            t_es = sa * t_c + (1 - sa) * t_es
        t_bars += 1
        if t_bars >= t_warm:
            trend = 1 if t_ef > t_es else -1

    def finalize_entry():
        nonlocal e_ef, e_es, e_pf, e_ps, e_bars, e_prevcum, pending, e_atr, e_prevclose
        e_pf, e_ps = e_ef, e_es
        if e_ef is None:
            e_ef = e_es = e_c
        else:
            e_ef = fa * e_c + (1 - fa) * e_ef
            e_es = sa * e_c + (1 - sa) * e_es
        ef_hist.append(e_ef)
        # Wilder ATR on the entry timeframe (TR uses this bar's H/L + prev close)
        tr = (e_h - e_l) if e_prevclose is None else max(
            e_h - e_l, abs(e_h - e_prevclose), abs(e_l - e_prevclose))
        e_atr = tr if e_atr is None else e_atr + (tr - e_atr) / bt.ATR_PERIOD
        e_prevclose = e_c
        if has_vol:
            cur = max(0.0, (e_cum - e_prevcum)) if e_cum is not None else 0.0
            if e_cum is not None:
                e_prevcum = e_cum
        else:
            cur = e_ticks
        vol_series.append(cur); e_bars += 1
        if e_pf is None or e_bars < warmup:
            return
        if cfg.get("no_vol"):
            vok = True
        elif len(vol_series) >= vol_n:
            vsma = sum(vol_series) / len(vol_series)
            vok = cur > vmult * vsma if vsma > 0 else False
        else:
            vok = False
        gap = e_ef - e_es; min_gap = rcp * e_c
        slope_ref = ef_hist[0] if len(ef_hist) > slope_bars else e_pf
        slope = e_ef - slope_ref                 # EMA9 now vs slope_bars bars ago
        bull = e_pf <= e_ps and e_ef > e_es
        bear = e_pf >= e_ps and e_ef < e_es
        if bull and gap >= min_gap and e_c > e_ef and slope > 0 and vok and trend == 1:
            pending = "BUY"
        elif bear and -gap >= min_gap and e_c < e_ef and slope < 0 and vok and trend == -1:
            pending = "SELL"

    for t_sec, dt, ltp, cum in ticks:
        if dt.hour > bt.EOD_HOUR or (dt.hour == bt.EOD_HOUR and dt.minute >= bt.EOD_MIN):
            if pos is not None:
                close_trade(pos, ltp, dt, "EOD"); pos = None
            break
        # 5m trend bars
        ti = bidx(dt, t_tf)
        if t_idx is None:
            t_idx = ti
        elif ti != t_idx:
            finalize_trend(); t_idx = ti
        t_c = ltp
        # 2m entry bars
        ei = bidx(dt, e_tf)
        if e_idx is None:
            e_idx = ei; e_c = e_h = e_l = ltp; e_ticks = 0; e_cum = cum
        elif ei != e_idx:
            finalize_entry(); e_idx = ei; e_c = e_h = e_l = ltp; e_ticks = 0; e_cum = cum
        e_c = ltp; e_h = max(e_h, ltp); e_l = min(e_l, ltp); e_ticks += 1
        if cum is not None:
            e_cum = cum
        # act on pending signal (entry / reverse)
        if pending is not None:
            if pos is not None and pos.direction != pending:
                close_trade(pos, ltp, dt, "REVERSE"); pos = None
            if pos is None:
                pos = bt.Position(pending, ltp, dt, entry_atr=e_atr)
            pending = None
        # per-tick exits (APPE-first then TSL, via the shared Position)
        if pos is not None:
            pos.trail_atr = e_atr   # track latest entry-tf ATR (ATR-mode TSL)
            res = pos.step(ltp, t_sec)
            if res is not None:
                reason, xp = res
                close_trade(pos, xp, dt, reason); pos = None

    if pos is not None:
        last = ticks[-1]
        close_trade(pos, last[2], last[1], "END_OF_DATA")
    return trades


def summarize(trades):
    total = sum(t["pnl"] for t in trades)
    w = sum(1 for t in trades if t["pnl"] > 0)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return total, len(trades), w, len(trades) - w, reasons


# label, kind, cfg, tsl%(percent-mode fallback), arm_per_lot, tsl_mode
# Options: ATR-TSL (1.5xATR(14)) + 3-bar EMA9 slope + gap 0.01% + 30-min vol SMA.
# Baseline = current live (5m, static 0.5%, 1-bar slope, 0.03% gap, 100-min vol).
VARIATIONS = [
    ("BASELINE 5m (live)", "single",
     {"tf": 5, "fast": 9, "slow": 21, "warmup": 9, "vol_sma": 20, "vol_mult": 1.5,
      "reverse_confirm_pct": 0.0003, "slope_bars": 1},
     0.5, 4000, "percent"),
    ("VAR1 3m/arm₹2k/ATR-TSL/gap0.01/slope3", "single",
     {"tf": 3, "fast": 9, "slow": 21, "warmup": 9, "vol_sma": 10, "vol_mult": 1.5,
      "reverse_confirm_pct": 0.0001, "slope_bars": 3},
     0.25, 2000, "atr"),
    ("VAR2 2m+5mTrend/arm₹2k/ATR-TSL/gap0.01/slope3", "mtf",
     {"tf": 2, "trend_tf": 5, "fast": 9, "slow": 21, "warmup": 9, "vol_sma": 15, "vol_mult": 1.5,
      "reverse_confirm_pct": 0.0001, "slope_bars": 3},
     0.25, 2000, "atr"),
    ("VAR3 2m/arm₹1k/ATR-TSL/gap0.01/slope3", "single",
     {"tf": 2, "fast": 9, "slow": 21, "warmup": 9, "vol_sma": 15, "vol_mult": 1.5,
      "reverse_confirm_pct": 0.0001, "slope_bars": 3},
     0.25, 1000, "atr"),
    # Option 4 (EMA 7/15 on 3m): fast pair + 2-candle crossover CONFIRMATION WINDOW
    # (cross_confirm_bars=2) — a cross stays eligible up to 2 candles after it. TSL = PURE
    # 1.5xATR(14), NO floor (the 100-pt floor was removed 2026-06-18 — the floor sweep showed
    # it strictly worse on chop). Slope = EMA7(now) vs EMA7(3 bars ago). Arm 2k/lot (=Rs4,000).
    ("VAR4 7/15 3m/arm₹2k/pureATR/gap0.01/slope3/confirm2", "single",
     {"tf": 3, "fast": 7, "slow": 15, "warmup": 9, "vol_sma": 10, "vol_mult": 1.5,
      "reverse_confirm_pct": 0.0001, "slope_bars": 3, "cross_confirm_bars": 2},
     0.25, 2000, "atr"),
]


def main():
    # args: positional date (default 2026-06-16) + optional "--csv [path]" to append a history row per option
    argv = sys.argv[1:]
    csv_path = None
    if "--csv" in argv:
        i = argv.index("--csv")
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            csv_path = argv[i + 1]; del argv[i:i + 2]
        else:
            csv_path = "options_history.csv"; del argv[i:i + 1]
    date = next((a for a in argv if not a.startswith("--")), datetime.now(bt.IST).strftime("%Y-%m-%d"))
    ticks = bt.load_ticks(date)
    print(f"date={date} | ticks={len(ticks)} | QTY={bt.QTY} | DATA_DIR={bt.DATA_DIR}")
    if not ticks:
        print("NO DATA"); return
    ltps = [t[2] for t in ticks]
    rng_pct = (max(ltps) - min(ltps)) / ltps[0] * 100 if ltps and ltps[0] else 0.0
    regime = "chop" if rng_pct < 0.8 else ("trend" if rng_pct > 1.5 else "mixed")  # day-range heuristic
    # Optional gap-gate override (all variations). The live default 0.03% is
    # calibrated for 5m; faster timeframes have thinner cross-gaps, so set a
    # lower RCP to exercise/compare them (e.g. RCP=0.00012 ~= 0.012%).
    rcp_ovr = os.getenv("RCP")
    if rcp_ovr:
        rcp_ovr = float(rcp_ovr)
        print(f"** GAP-GATE OVERRIDE: reverse_confirm_pct = {rcp_ovr*100:.4f}% (all variations) **")
    # Volume-SMA mode: "short" swaps the ~100-min period for a ~30-min local SMA
    # (the long SMA is inflated by the high-volume open -> over-rejects midday crosses).
    vsma_mode = os.getenv("VSMA")
    SHORT_VSMA = {5: 10, 3: 10, 2: 15}  # ~50/30/30 min
    if vsma_mode == "short":
        print(f"** VOLUME-SMA OVERRIDE: short/local SMA {SHORT_VSMA} (~30-50 min) **")
    no_vol = os.getenv("NOVOL") == "1"
    if no_vol:
        print("** VOLUME FILTER REMOVED: entry = cross + gap + close/slope only **")
    rows = []
    for label, kind, cfg, tsl, arm, tsl_mode in VARIATIONS:
        # The live baseline row is the fixed reference: never apply the gap/volume
        # overrides to it — it must always reflect the true deployed config.
        is_base = label.startswith("BASELINE")
        if rcp_ovr and not is_base:
            cfg = {**cfg, "reverse_confirm_pct": rcp_ovr}
        if vsma_mode == "short" and not is_base:
            cfg = {**cfg, "vol_sma": SHORT_VSMA.get(cfg["tf"], cfg["vol_sma"])}
        if no_vol:
            cfg = {**cfg, "no_vol": True}
        bt.TSL_MODE = tsl_mode            # per-variation: "atr" (1.5xATR(14)) or "percent"
        bt.ATR_FLOOR_PTS = cfg.get("atr_floor", 0.0)   # per-variation ATR-trail floor in pts (0 = none)
        set_globals(tsl, arm)
        trades = bt.simulate_day(ticks, cfg, not no_vol)[0] if kind == "single" else simulate_mtf(ticks, cfg)
        total, n, w, l, reasons = summarize(trades)
        rows.append((label, cfg, tsl, arm, total, n, w, l, reasons, tsl_mode))
        armv = arm * (bt.QTY / bt.LOT_SIZE)
        floor = cfg.get("atr_floor", 0.0)
        tsl_str = (f"ATR×{bt.ATR_MULT}" + (f"≥{floor:.0f}pt" if floor else "")) if tsl_mode == "atr" else f"{tsl}%"
        print(f"\n{'='*74}\n  {label} | tf={cfg['tf']}m | APPE arm ₹{armv:.0f} | TSL {tsl_str} | "
              f"volSMA({cfg['vol_sma']}≈{cfg['vol_sma']*cfg['tf']}min) | gap {cfg['reverse_confirm_pct']*100:.3f}% | "
              f"slope {cfg.get('slope_bars',1)}-bar\n{'='*74}")
        bt.print_trades(trades)
        print(f"      exits: {reasons}")
    print(f"\n{'='*74}\n  COMPARISON — {date} (1 day, cold-start EMAs → DIRECTIONAL only)\n{'='*74}")
    print(f"  {'variation':<26}{'P&L ₹':>11}{'trades':>8}{'W':>4}{'L':>4}  exits")
    for label, cfg, tsl, arm, total, n, w, l, reasons, _tm in rows:
        ex = ",".join(f"{k}:{v}" for k, v in reasons.items())
        print(f"  {label:<26}{total:>+11,.0f}{n:>8}{w:>4}{l:>4}  {ex}")

    # --csv: append one history row per option (idempotent — skips a date already present)
    if csv_path:
        cols = ["date", "regime", "option", "tf", "ema", "gap_pct", "slope_bars", "vol_sma",
                "tsl", "arm", "trades", "W", "L", "pnl", "exits", "notes"]
        existing = set()
        if os.path.exists(csv_path):
            with open(csv_path, newline="") as f:
                existing = {r.get("date") for r in csv.DictReader(f)}
        if date in existing:
            print(f"\n  [CSV] {date} already in {csv_path} — not appending (avoid dups)")
        else:
            new_file = not os.path.exists(csv_path)
            with open(csv_path, "a", newline="") as f:
                wri = csv.writer(f)
                if new_file:
                    wri.writerow(cols)
                for label, cfg, tsl, arm, total, n, w, l, reasons, tm in rows:
                    floor = cfg.get("atr_floor", 0.0)
                    tsl_desc = (f"ATR{bt.ATR_MULT:g}x" + (f">={floor:.0f}pt" if floor else "")) if tm == "atr" else f"{tsl:g}%"
                    ex = ";".join(f"{k}:{v}" for k, v in reasons.items())
                    wri.writerow([date, regime, label, f"{cfg['tf']}m", f"{cfg['fast']}/{cfg['slow']}",
                                  f"{cfg['reverse_confirm_pct']*100:.3f}", cfg.get("slope_bars", 1),
                                  cfg["vol_sma"], tsl_desc, f"{arm*(bt.QTY/bt.LOT_SIZE):.0f}",
                                  n, w, l, f"{total:.0f}", ex, ""])
            print(f"\n  [CSV] appended {len(rows)} rows for {date} (regime={regime}, range {rng_pct:.2f}%) -> {csv_path}")


if __name__ == "__main__":
    main()
