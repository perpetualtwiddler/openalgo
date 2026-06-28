#!/usr/bin/env python3
"""regime_scan.py — measure intraday "trend vs chop" per captured day.

Goal: find a metric, computable EARLY in the session, that flags a trend day
(e.g. 2026-06-12) and rejects choppy days (06-16/06-17), so a strategy can decide
each morning whether to trade at all.

For every local capture day it builds 3-min bars and reports, at several morning
cutoffs (and full day), four candidate regime gauges:

  ER      Kaufman Efficiency Ratio = |net move| / |total path|, range 0..1.
          ~1 = clean trend, ~0 = round-trip chop. The most direct "trendiness".
  ADX     Wilder ADX(14) — classic trend-strength; >25 conventionally = trending.
  Xs      EMA(5/13) crossover count so far — chop throws many, trend few.
  net/rng |close-open| / (high-low) over the window — how much of the range was
          kept as net displacement.

Usage:  uv run python strategies/scripts/regime_scan.py [--tf 3] [--fast 5 --slow 13]
        BACKTEST_DATA_DIR overrides the capture dir (default as in backtest_ticks).
"""
import argparse
from datetime import timedelta

from backtest_ticks import DATA_DIR, load_ticks

CUTOFFS = ["10:15", "10:45", "11:15", "11:45", "EOD"]


def build_bars(ticks, tf_min):
    """Return [(start_dt, o, h, l, c)] for tf_min bars, aligned to 09:15."""
    open_dt = ticks[0][1].replace(hour=9, minute=15, second=0, microsecond=0)
    bars = []
    cur = None
    o = h = l = c = None
    for _, dt, ltp, _ in ticks:
        idx = int((dt - open_dt).total_seconds() // (tf_min * 60))
        if cur is None:
            cur = idx
            o = h = l = c = ltp
        elif idx != cur:
            bars.append((open_dt + timedelta(minutes=tf_min * cur), o, h, l, c))
            cur = idx
            o = h = l = c = ltp
        h = max(h, ltp)
        l = min(l, ltp)
        c = ltp
    if cur is not None:
        bars.append((open_dt + timedelta(minutes=tf_min * cur), o, h, l, c))
    return bars


def efficiency_ratio(closes):
    if len(closes) < 2:
        return 0.0
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return net / path if path > 0 else 0.0


def adx(bars, period=14):
    """Wilder ADX on (start,o,h,l,c) bars. Returns latest ADX or None if too short."""
    if len(bars) < 2 * period:
        return None
    trs, pdm, ndm = [], [], []
    for i in range(1, len(bars)):
        _, _, h, l, c = bars[i]
        _, _, ph, pl, pc = bars[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)

    def wilder(seq):
        sm = [sum(seq[:period])]
        for v in seq[period:]:
            sm.append(sm[-1] - sm[-1] / period + v)
        return sm

    str_, spdm, sndm = wilder(trs), wilder(pdm), wilder(ndm)
    dxs = []
    for tr, p, n in zip(str_, spdm, sndm):
        if tr == 0:
            continue
        pdi, ndi = 100 * p / tr, 100 * n / tr
        s = pdi + ndi
        if s > 0:
            dxs.append(100 * abs(pdi - ndi) / s)
    if len(dxs) < period:
        return None
    a = sum(dxs[:period]) / period
    for v in dxs[period:]:
        a = (a * (period - 1) + v) / period
    return a


def crossovers(closes, fast, slow):
    if len(closes) < 2:
        return 0
    fa, sa = 2 / (fast + 1), 2 / (slow + 1)
    ef = es = closes[0]
    prev = 0
    n = 0
    for c in closes[1:]:
        ef = fa * c + (1 - fa) * ef
        es = sa * c + (1 - sa) * es
        sign = 1 if ef > es else (-1 if ef < es else 0)
        if sign and prev and sign != prev:
            n += 1
        if sign:
            prev = sign
    return n


def hhmm(dt):
    return dt.strftime("%H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", type=int, default=3)
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=13)
    ap.add_argument("--roll", type=int, default=10,
                    help="trailing-window bars for the rolling-ER profile (10 @3m = 30min)")
    args = ap.parse_args()

    days = sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir())
    print(f"Regime scan — {args.tf}m bars, EMA({args.fast}/{args.slow}) crossovers | dir={DATA_DIR}")
    print("Metrics at each cutoff: ER | ADX | Xs  (… full-day net/range at right)\n")
    hdr = f"{'day':<12}"
    for cut in CUTOFFS:
        hdr += f"{'ER@'+cut:>9}{'ADX':>6}{'Xs':>4}"
    hdr += f"{'netPts':>9}{'rngPts':>8}{'net/rng':>9}"
    print(hdr)
    print("-" * len(hdr))

    for day in days:
        ticks = load_ticks(day)
        if not ticks:
            continue
        bars = build_bars(ticks, args.tf)
        open_dt = bars[0][0]
        row = f"{day:<12}"
        for cut in CUTOFFS:
            if cut == "EOD":
                wb = bars
            else:
                h, m = map(int, cut.split(":"))
                limit = open_dt.replace(hour=h, minute=m)
                wb = [b for b in bars if b[0] <= limit]
            if len(wb) < 2:
                row += f"{'-':>9}{'-':>6}{'-':>4}"
                continue
            closes = [b[4] for b in wb]
            er = efficiency_ratio(closes)
            a = adx(wb, 14)
            xs = crossovers(closes, args.fast, args.slow)
            astr = f"{a:>5.0f}" if a is not None else "   - "
            row += f"{er:>9.2f}{astr:>6}{xs:>4d}"
        # full-day net/range
        closes = [b[4] for b in bars]
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]
        net = abs(closes[-1] - closes[0])
        rng = max(highs) - min(lows)
        row += f"{net:>9.0f}{rng:>8.0f}{(net/rng if rng else 0):>9.2f}"
        print(row)

    # ---- rolling-ER profile: when (if ever) does each day become trendy? ----
    W = args.roll
    print(f"\nRolling ER over trailing {W} bars ({W*args.tf}min). For each day: the max "
          f"rolling-ER\nreached in the MORNING (<12:00) vs AFTERNOON (>=12:00), and the "
          f"clock time of the\nday's single highest rolling-ER reading. A pure-chop day "
          f"never gets trendy; a\n'quiet-morning/trend-afternoon' day spikes late.\n")
    print(f"{'day':<12}{'morn maxER':>12}{'noon maxER':>12}{'peak ER':>9}{'peak time':>11}")
    print("-" * 56)
    for day in days:
        ticks = load_ticks(day)
        if not ticks:
            continue
        bars = build_bars(ticks, args.tf)
        if len(bars) < W + 1:
            continue
        morn = noon = 0.0
        peak = 0.0
        peak_t = None
        for i in range(W, len(bars)):
            win = [b[4] for b in bars[i - W:i + 1]]
            er = efficiency_ratio(win)
            t = bars[i][0]
            if t.hour < 12:
                morn = max(morn, er)
            else:
                noon = max(noon, er)
            if er > peak:
                peak, peak_t = er, t
        print(f"{day:<12}{morn:>12.2f}{noon:>12.2f}{peak:>9.2f}{hhmm(peak_t):>11}")


if __name__ == "__main__":
    main()
