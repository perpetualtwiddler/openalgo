#!/usr/bin/env python
"""
Tick-based offline backtester for the EMA-crossover BANKNIFTY strategy.
=======================================================================
Consumes raw LTP tick capture (normalized_market_data.jsonl), resamples to a
configurable candle timeframe, and replays the strategy FRESH each day with the
trailing-SL and APPE exits evaluated TICK-BY-TICK (not on bar closes) — the
high-fidelity advantage of having ~1 tick/sec data.

Data layout (one file per session):
    <DATA_DIR>/<YYYY-MM-DD>/normalized_market_data.jsonl

Each line: {"data": {"ltp": <float>, "timestamp": <epoch_ms>, "symbol": ...}}
Entry gate mirrors live check_signal: a cross also needs a decisive EMA gap
(|EMA9-EMA21| >= REVERSE_CONFIRM_PCT x close), price leading (close vs EMA9), and
EMA9 momentum (slope). Two variants run side by side:
    - PRICE-ONLY : the structural gate (cross + gap + close + slope) WITHOUT volume.
    - VOL-FILTER : the strategy's volume-confirmation gate. Source is auto-detected
                   PER DAY:
                     * "real vol"        — Quote-mode captures carry cumulative
                       traded volume; per-bar volume is diffed across bar boundaries.
                     * "tick-count proxy" — LTP-only captures have no volume, so
                       ticks-per-bar is used as a WEAK proxy (the LTP feed cadence is
                       near-constant, so this rarely clears the filter — directional
                       only). Captures from 2026-06-15 onward use Quote mode.

Usage:
    python backtest_ticks.py                 # all days, default configs
    python backtest_ticks.py 2026-06-12      # single day
    python backtest_ticks.py --tf 2 --fast 8 --slow 17 --warmup 9   # one custom config

Modeling choices (all adjustable via CLI/env):
    - Crossover detected on a COMPLETED bar (mirrors live iloc[-2] vs iloc[-3]).
    - Entry filled at the FIRST TICK after the bar closes (≈ MARKET order).
    - Exits (trailing-SL + APPE) evaluated on every tick with real timestamps.
    - 4 days is a SANITY-CHECK sample, not a statistically significant one.
"""
import argparse
import json
import math
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
DATA_DIR = Path(os.getenv("BACKTEST_DATA_DIR", "/home/dksha/ptwiddler/backtestdata"))

# --- Strategy constants (mirror ema_crossover_banknifty.py defaults) ---
QTY = int(os.getenv("QUANTITY", "60"))
LOT_SIZE = int(os.getenv("LOT_SIZE", "30"))
TRAILING_SL_PCT = 0.5
TIGHT_TSL_THRESHOLD = 5000.0
TIGHT_TSL_PCT = 0.25
APPE_ENABLED = True
ARM_PER_LOT = 4000.0  # mirror live "HAVRATPANA" tuning (lowered from 5000)
PROFIT_ARM_THRESHOLD = ARM_PER_LOT * (QTY / LOT_SIZE)  # ₹8,000 at 60 qty
GIVEBACK_REF_UNITS = 2.0      # size-aware G anchor: factor 1.0 at 2 lots (60 qty)
REVERSE_CONFIRM_PCT = 0.0003  # min |EMA9-EMA21| gap at the cross (~0.03%) — entry gate
GIVEBACK_K = 30.0
TREND_WINDOW_SEC = 180.0
TREND_CONFIRM_SEC = 30.0
HARD_MULT = 2.0
EOD_HOUR, EOD_MIN = 15, 14


def load_ticks(date_str):
    """Return [(t_sec_float, ist_dt, ltp)] sorted by time for a session."""
    path = DATA_DIR / date_str / "normalized_market_data.jsonl"
    if not path.exists():
        return None
    ticks = []
    with open(path) as fh:
        for line in fh:
            try:
                o = json.loads(line)
                d = o["data"]
                ms = int(d["timestamp"])
                ltp = float(d["ltp"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            # Quote-mode payloads carry cumulative day volume; LTP-mode payloads
            # have no "volume" key (-> None). Per-bar volume is derived by diffing
            # this cumulative figure across bar boundaries in simulate_day().
            vol = d.get("volume")
            try:
                cum_vol = float(vol) if vol is not None else None
            except (TypeError, ValueError):
                cum_vol = None
            t_sec = ms / 1000.0
            ist_dt = datetime.fromtimestamp(t_sec, IST)
            ticks.append((t_sec, ist_dt, ltp, cum_vol))
    ticks.sort(key=lambda x: x[0])
    return ticks


# =============================================================================
# APPE — faithful port of ema_crossover_banknifty.py::_appe_evaluate
# =============================================================================
class Position:
    def __init__(self, direction, entry_price, entry_dt):
        self.direction = direction          # "BUY" / "SELL"
        self.entry_price = entry_price
        self.entry_dt = entry_dt
        self.peak_price = entry_price        # for trailing SL
        if direction == "BUY":
            self.trailing_sl = round(entry_price * (1 - TRAILING_SL_PCT / 100), 2)
        else:
            self.trailing_sl = round(entry_price * (1 + TRAILING_SL_PCT / 100), 2)
        # APPE state
        self.appe_peak = 0.0
        self.appe_armed = False
        self.appe_breach_start = None
        self.pnl_window = deque()            # (t_sec, unrealized)

    def unrealized(self, ltp):
        if self.direction == "BUY":
            return (ltp - self.entry_price) * QTY
        return (self.entry_price - ltp) * QTY

    def update_trailing(self, ltp):
        """Update TSL with tightening; return True if SL is hit at this tick."""
        u = self.unrealized(ltp)
        if self.direction == "BUY":
            if ltp > self.peak_price:
                self.peak_price = ltp
                pct = TIGHT_TSL_PCT if u >= TIGHT_TSL_THRESHOLD else TRAILING_SL_PCT
                new = round(self.peak_price * (1 - pct / 100), 2)
                if new > self.trailing_sl:
                    self.trailing_sl = new
            return ltp <= self.trailing_sl
        else:
            if ltp < self.peak_price:
                self.peak_price = ltp
                pct = TIGHT_TSL_PCT if u >= TIGHT_TSL_THRESHOLD else TRAILING_SL_PCT
                new = round(self.peak_price * (1 + pct / 100), 2)
                if new < self.trailing_sl:
                    self.trailing_sl = new
            return ltp >= self.trailing_sl

    def _slope_negative(self):
        pts = self.pnl_window
        if len(pts) < 5:
            return False
        span = pts[-1][0] - pts[0][0]
        if span < TREND_WINDOW_SEC * 0.5:
            return False
        n = len(pts)
        t0 = pts[0][0]
        xs = [p[0] - t0 for p in pts]
        ys = [p[1] for p in pts]
        mx = sum(xs) / n
        my = sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0:
            return False
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
        return slope < 0

    def appe_evaluate(self, ltp, now):
        """Return 'APPE_HARD' / 'APPE_RATCHET' / None (mirrors live gates 1-4)."""
        if not APPE_ENABLED:
            return None
        u = self.unrealized(ltp)
        if u > self.appe_peak:
            self.appe_peak = u
        self.pnl_window.append((now, u))
        cutoff = now - TREND_WINDOW_SEC
        while self.pnl_window and self.pnl_window[0][0] < cutoff:
            self.pnl_window.popleft()

        if not self.appe_armed:
            if self.appe_peak >= PROFIT_ARM_THRESHOLD:
                self.appe_armed = True
            else:
                return None

        # size-aware: √-scale the budget by units (mirror live _appe_evaluate)
        _units_factor = math.sqrt((QTY / LOT_SIZE) / GIVEBACK_REF_UNITS)
        budget = GIVEBACK_K * math.sqrt(max(self.appe_peak, 0.0)) * _units_factor
        floor = self.appe_peak - budget
        giveback = self.appe_peak - u

        if giveback >= HARD_MULT * budget:
            return "APPE_HARD"

        if u < floor:
            if self._slope_negative():
                if self.appe_breach_start is None:
                    self.appe_breach_start = now
                elif now - self.appe_breach_start >= TREND_CONFIRM_SEC:
                    return "APPE_RATCHET"
            else:
                self.appe_breach_start = None
        else:
            self.appe_breach_start = None
        return None

    def step(self, ltp, now):
        """Per-tick exit evaluation, mirroring live on_ltp_update ordering exactly:
        update the trailing level first, then evaluate APPE (which fires FIRST if it
        triggers), then fall back to the trailing-SL. APPE is evaluated on every tick
        regardless of whether the SL is hit. Returns (reason, exit_price) or None.

        Exit price model: APPE exits at the tick ltp (live places a MARKET order at
        that moment); TRAILING_SL exits at the SL level. These are modeling choices —
        live's actual broker fill cannot be reproduced offline.
        """
        hit_sl = self.update_trailing(ltp)
        reason = self.appe_evaluate(ltp, now)
        if reason:
            return reason, ltp
        if hit_sl:
            return "TRAILING_SL", self.trailing_sl
        return None


# =============================================================================
# Single-day simulation
# =============================================================================
def simulate_day(ticks, cfg, use_vol_filter):
    """Walk ticks: aggregate bars, detect crossovers on bar close, replay exits
    per tick. Returns (trades, volume_source) where volume_source is "real"
    (per-bar volume diffed from cumulative quote volume) or "tickcount" (proxy)."""
    tf = cfg["tf"]
    fast_a = 2 / (cfg["fast"] + 1)
    slow_a = 2 / (cfg["slow"] + 1)
    vol_sma_n = cfg["vol_sma"]
    vol_mult = cfg["vol_mult"]
    warmup = cfg["warmup"]
    rcp = cfg.get("reverse_confirm_pct", REVERSE_CONFIRM_PCT)

    # Real traded volume is available only when the capture carries a cumulative
    # "volume" field (Quote mode). LTP-only days fall back to a tick-count proxy.
    has_volume = any(t[3] for t in ticks)
    volume_source = "real" if has_volume else "tickcount"

    open_dt = ticks[0][1].replace(hour=9, minute=15, second=0, microsecond=0)

    # Bar aggregation state
    cur_bar_idx = None
    bar_o = bar_h = bar_l = bar_c = None
    bar_ticks = 0
    bar_cum_vol = None            # latest cumulative volume seen in the current bar
    prev_bar_cum_vol = ticks[0][3] if has_volume else 0.0  # baseline = first tick's cum
    bars_done = 0                 # completed bars this day
    ema_fast = ema_slow = None
    prev_fast = prev_slow = None
    vol_series = deque(maxlen=vol_sma_n)  # per-bar volume (real) or tick-count (proxy)

    pending_signal = None         # "BUY"/"SELL" to act on next tick
    pos = None
    trades = []

    def bar_index(dt):
        return int((dt - open_dt).total_seconds() // (tf * 60))

    def close_trade(p, exit_price, exit_dt, reason):
        pnl = ((exit_price - p.entry_price) if p.direction == "BUY"
               else (p.entry_price - exit_price)) * QTY
        trades.append({
            "dir": p.direction, "entry_dt": p.entry_dt, "entry_price": p.entry_price,
            "exit_dt": exit_dt, "exit_price": exit_price, "reason": reason, "pnl": pnl,
        })

    def finalize_bar():
        """Called when a bar completes; updates EMAs + detects crossover."""
        nonlocal ema_fast, ema_slow, prev_fast, prev_slow, bars_done
        nonlocal pending_signal, prev_bar_cum_vol
        prev_fast, prev_slow = ema_fast, ema_slow
        if ema_fast is None:
            ema_fast, ema_slow = bar_c, bar_c
        else:
            ema_fast = fast_a * bar_c + (1 - fast_a) * ema_fast
            ema_slow = slow_a * bar_c + (1 - slow_a) * ema_slow

        # Per-bar volume: diff cumulative quote volume across the bar boundary;
        # fall back to tick-count when no real volume exists.
        if volume_source == "real":
            cur_vol = max(0.0, (bar_cum_vol - prev_bar_cum_vol)) if bar_cum_vol is not None else 0.0
            if bar_cum_vol is not None:
                prev_bar_cum_vol = bar_cum_vol
        else:
            cur_vol = bar_ticks
        vol_series.append(cur_vol)
        bars_done += 1

        # Need a prior EMA value and to be past warmup gate
        if prev_fast is None or bars_done < warmup:
            return

        # Volume filter on the just-closed bar (cur_vol is vol_series[-1])
        vol_ok = True
        if use_vol_filter:
            if len(vol_series) >= vol_sma_n:
                vsma = sum(vol_series) / len(vol_series)
                vol_ok = cur_vol > vol_mult * vsma if vsma > 0 else False
            else:
                vol_ok = False  # not enough history for the baseline yet

        # Advanced entry gate (mirror live check_signal §14/§16): a bare cross is not
        # enough — require a decisive EMA gap, price leading (close vs EMA9), EMA9
        # momentum, and (when use_vol_filter) volume. PRICE-ONLY keeps the structural
        # gates and drops only the volume condition, to isolate volume's marginal effect.
        gap = ema_fast - ema_slow
        min_gap = rcp * bar_c
        slope9 = ema_fast - prev_fast
        vol_pass = vol_ok if use_vol_filter else True
        bull_cross = prev_fast <= prev_slow and ema_fast > ema_slow
        bear_cross = prev_fast >= prev_slow and ema_fast < ema_slow
        if bull_cross and gap >= min_gap and bar_c > ema_fast and slope9 > 0 and vol_pass:
            pending_signal = "BUY"
        elif bear_cross and -gap >= min_gap and bar_c < ema_fast and slope9 < 0 and vol_pass:
            pending_signal = "SELL"

    for t_sec, dt, ltp, cum_vol in ticks:
        # ---- EOD square-off ----
        if dt.hour > EOD_HOUR or (dt.hour == EOD_HOUR and dt.minute >= EOD_MIN):
            if pos is not None:
                close_trade(pos, ltp, dt, "EOD")
                pos = None
            break

        # ---- bar bucketing ----
        idx = bar_index(dt)
        if cur_bar_idx is None:
            cur_bar_idx = idx
            bar_o = bar_h = bar_l = bar_c = ltp
            bar_ticks = 0
            bar_cum_vol = cum_vol
        elif idx != cur_bar_idx:
            finalize_bar()                # close the bar we were building
            cur_bar_idx = idx
            bar_o = bar_h = bar_l = bar_c = ltp
            bar_ticks = 0
            bar_cum_vol = cum_vol
        bar_h = max(bar_h, ltp)
        bar_l = min(bar_l, ltp)
        bar_c = ltp
        bar_ticks += 1
        if cum_vol is not None:
            bar_cum_vol = cum_vol

        # ---- act on a pending signal (entry / reverse) at this tick ----
        if pending_signal is not None:
            if pos is not None and pos.direction != pending_signal:
                close_trade(pos, ltp, dt, "REVERSE")
                pos = None
            if pos is None:
                pos = Position(pending_signal, ltp, dt)
            pending_signal = None

        # ---- per-tick exit evaluation (APPE-first, then trailing-SL — see step()) ----
        if pos is not None:
            res = pos.step(ltp, t_sec)
            if res is not None:
                reason, exit_price = res
                close_trade(pos, exit_price, dt, reason)
                pos = None

    # End of data with open position (shouldn't happen — EOD breaks first)
    if pos is not None:
        last = ticks[-1]
        close_trade(pos, last[2], last[1], "END_OF_DATA")  # last[2]=ltp, last[1]=dt
    return trades, volume_source


# =============================================================================
# Reporting
# =============================================================================
def print_trades(trades):
    if not trades:
        print("      (no trades)")
        return 0
    total = 0
    for t in trades:
        s = "+" if t["pnl"] >= 0 else ""
        total += t["pnl"]
        print(f"      {t['dir']:<4} {t['entry_dt']:%H:%M} @ {t['entry_price']:>9.1f} -> "
              f"{t['reason']:<12} {t['exit_dt']:%H:%M} @ {t['exit_price']:>9.1f} | {s}{t['pnl']:>9,.0f}")
    w = sum(1 for t in trades if t["pnl"] > 0)
    ss = "+" if total >= 0 else ""
    print(f"      Total: {ss}{total:,.0f} INR | {len(trades)} trade(s) | W:{w} L:{len(trades)-w}")
    return total


_SOURCE_TAG = {"real": "real vol", "tickcount": "tick-count proxy"}


def run(dates, configs):
    grand = {}  # (cfg_label, variant) -> total
    n = len(dates)
    for cfg in configs:
        label = f"{cfg['fast']}/{cfg['slow']} @ {cfg['tf']}m (warmup {cfg['warmup']})"
        print(f"\n{'='*72}\n  CONFIG: {label}\n{'='*72}")
        # Variant 2 uses the volume filter; the per-day source (real cumulative
        # volume vs tick-count proxy) is detected and labelled per day.
        for variant, use_filter in [("PRICE-ONLY", False), ("VOL-FILTER", True)]:
            hdr = "" if not use_filter else f"(>{cfg['vol_mult']:g}x SMA({cfg['vol_sma']}); source per day)"
            print(f"\n  --- {variant} {hdr} ---")
            agg = 0
            for date_str in dates:
                ticks = load_ticks(date_str)
                if not ticks:
                    print(f"    {date_str}: no data")
                    continue
                trades, src = simulate_day(ticks, cfg, use_filter)
                tag = f" [{_SOURCE_TAG[src]}]" if use_filter else ""
                print(f"    {date_str}:{tag}")
                agg += print_trades(trades)
            ss = "+" if agg >= 0 else ""
            print(f"\n    ►► {variant} {n}-day total: {ss}{agg:,.0f} INR")
            grand[(label, variant)] = agg

    print(f"\n{'='*72}\n  GRAND SUMMARY (₹, {n} days — sanity-check sample, not significant)\n{'='*72}")
    print(f"  {'config':<28}{'PRICE-ONLY':>16}{'VOL-FILTER':>18}")
    for cfg in configs:
        label = f"{cfg['fast']}/{cfg['slow']} @ {cfg['tf']}m (warmup {cfg['warmup']})"
        po = grand.get((label, "PRICE-ONLY"), 0)
        vf = grand.get((label, "VOL-FILTER"), 0)
        print(f"  {label:<28}{po:>+16,.0f}{vf:>+18,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="single YYYY-MM-DD (default: all days)")
    ap.add_argument("--tf", type=int, help="timeframe minutes (custom single config)")
    ap.add_argument("--fast", type=int)
    ap.add_argument("--slow", type=int)
    ap.add_argument("--warmup", type=int, default=9, help="warmup candles to skip")
    ap.add_argument("--vol-sma", type=int)
    ap.add_argument("--vol-mult", type=float, default=1.5)
    ap.add_argument("--reverse-confirm-pct", type=float, default=REVERSE_CONFIRM_PCT,
                    help="min |EMA9-EMA21| gap at the cross as a fraction of price "
                         "(default mirrors live; lower it for faster timeframes)")
    args = ap.parse_args()

    if args.date:
        dates = [args.date]
    else:
        dates = sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir())

    rcp = args.reverse_confirm_pct
    if args.tf and args.fast and args.slow:
        configs = [{"tf": args.tf, "fast": args.fast, "slow": args.slow,
                    "warmup": args.warmup,
                    "vol_sma": args.vol_sma or 10, "vol_mult": args.vol_mult,
                    "reverse_confirm_pct": rcp}]
    else:
        configs = [
            {"tf": 2, "fast": 8, "slow": 17, "warmup": 9, "vol_sma": 10, "vol_mult": 1.5,
             "reverse_confirm_pct": rcp},
            {"tf": 5, "fast": 9, "slow": 21, "warmup": 9, "vol_sma": 20, "vol_mult": 1.5,
             "reverse_confirm_pct": rcp},
        ]

    print(f"Data dir: {DATA_DIR} | days: {', '.join(dates)}")
    print(f"QTY={QTY} | TSL {TRAILING_SL_PCT}%→{TIGHT_TSL_PCT}%@₹{TIGHT_TSL_THRESHOLD:.0f} | "
          f"APPE arm ₹{PROFIT_ARM_THRESHOLD:.0f}, G={GIVEBACK_K:g}√peak | "
          f"gap gate ≥{rcp*100:.4f}% (~{rcp*57000:.0f} pts @57k)")
    run(dates, configs)


if __name__ == "__main__":
    main()
