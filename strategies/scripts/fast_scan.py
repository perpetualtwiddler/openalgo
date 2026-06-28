#!/usr/bin/env python3
"""fast_scan.py — test FAST (low-latency) trend-onset signals per day.

The 60-min ER gate is clean but lags. This probes signals that can fire within a
bar or two, to see whether any flags 06-12's afternoon launch WITHOUT also firing
on chop days' fake thrusts:

  run    longest streak of consecutive same-direction bar closes (and when the
         first streak of length>=RUN_TRIG occurred — the earliest a "N bars in a
         row" trigger could fire that day).
  rngX   largest single-bar range as a multiple of the day's median bar range
         (range expansion / thrust), and its clock time.
  extX   max |close - EMA_slow| as a multiple of ATR(14) (how far price stretched
         from the mean — momentum vs oscillation), and its time.

Read it as: does 06-12 stand out on these, or do chop days produce the same?
"""
import argparse
import statistics
from datetime import timedelta

from backtest_ticks import DATA_DIR, load_ticks


def build_bars(ticks, tf_min):
    open_dt = ticks[0][1].replace(hour=9, minute=15, second=0, microsecond=0)
    bars, cur = [], None
    o = h = l = c = None
    for _, dt, ltp, _ in ticks:
        idx = int((dt - open_dt).total_seconds() // (tf_min * 60))
        if cur is None:
            cur, o, h, l, c = idx, ltp, ltp, ltp, ltp
        elif idx != cur:
            bars.append((open_dt + timedelta(minutes=tf_min * cur), o, h, l, c))
            cur, o, h, l, c = idx, ltp, ltp, ltp, ltp
        h, l, c = max(h, ltp), min(l, ltp), ltp
    if cur is not None:
        bars.append((open_dt + timedelta(minutes=tf_min * cur), o, h, l, c))
    return bars


def atr(bars, period=14):
    """Wilder ATR series aligned to bars[1:]."""
    trs = []
    for i in range(1, len(bars)):
        _, _, h, l, _ = bars[i]
        pc = bars[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    out = [None] * (period - 1)
    a = sum(trs[:period]) / period
    out.append(a)
    for v in trs[period:]:
        a = (a * (period - 1) + v) / period
        out.append(a)
    return out  # aligned to bars[1:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", type=int, default=3)
    ap.add_argument("--slow", type=int, default=13)
    ap.add_argument("--run-trig", type=int, default=4,
                    help="streak length whose first occurrence time we report")
    args = ap.parse_args()
    sa = 2 / (args.slow + 1)

    days = sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir())
    print(f"Fast trend-onset scan — {args.tf}m bars | run-trigger={args.run_trig} bars\n")
    print(f"{'day':<12}{'maxRun':>7}{'1st>=trig':>10}{'rngX':>6}{'@rngX':>7}"
          f"{'extX':>6}{'@extX':>7}")
    print("-" * 55)
    for day in days:
        ticks = load_ticks(day)
        if not ticks:
            continue
        bars = build_bars(ticks, args.tf)
        if len(bars) < 20:
            continue
        closes = [b[4] for b in bars]

        # consecutive same-direction close runs
        max_run = run = 1
        prev_sign = 0
        first_trig = None
        for i in range(1, len(bars)):
            d = closes[i] - closes[i - 1]
            sign = 1 if d > 0 else (-1 if d < 0 else 0)
            if sign != 0 and sign == prev_sign:
                run += 1
            else:
                run = 1
            prev_sign = sign if sign != 0 else prev_sign
            if run >= args.run_trig and first_trig is None:
                first_trig = bars[i][0]
            max_run = max(max_run, run)

        # range expansion vs median bar range
        ranges = [b[2] - b[3] for b in bars]
        med = statistics.median(ranges) or 1.0
        rngx = max(ranges) / med
        rngx_t = bars[ranges.index(max(ranges))][0]

        # extension from slow EMA, in ATR units
        atrs = atr(bars, 14)
        ema = closes[0]
        extx, extx_t = 0.0, None
        if atrs:
            for i in range(1, len(bars)):
                ema = sa * closes[i] + (1 - sa) * ema
                a = atrs[i - 1]
                if a and a > 0:
                    e = abs(closes[i] - ema) / a
                    if e > extx:
                        extx, extx_t = e, bars[i][0]
        ft = first_trig.strftime("%H:%M") if first_trig else "  -  "
        rt = rngx_t.strftime("%H:%M")
        et = extx_t.strftime("%H:%M") if extx_t else "  -  "
        print(f"{day:<12}{max_run:>7}{ft:>10}{rngx:>6.1f}{rt:>7}{extx:>6.1f}{et:>7}")


if __name__ == "__main__":
    main()
