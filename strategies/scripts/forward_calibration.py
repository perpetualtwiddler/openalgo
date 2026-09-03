#!/usr/bin/env python3
"""forward_calibration.py — is forward_model.py's distribution honest?

A forward model that is merely plausible is worthless; it has to be CALIBRATED. The test:
for each captured day, predict the outcome distribution at a decision time, then find which
percentile of that distribution the REAL outcome landed in. If the model is honest those
percentiles are uniform across days — mean near 50%, about 90% of them inside the 5-95
band, about half above the median. Any drift away from that measures a specific defect:

  * mean percentile > 50%  ->  the model is PESSIMISTIC (reality keeps beating it)
  * coverage < 90%         ->  the distribution is TOO NARROW (overconfident)

No look-ahead: each day is scored only against afternoons that preceded it, and the pool is
sliced by date rather than shuffled.

This is what condemned the flat-IV variant. Measured at the 13:00 decision point on days
with DTE >= 1:

    model                        n   mean pctile   inside 5-95   above median
    flat IV                     39         69.3%           46%            72%
    joint resample              39         49.4%           87%            46%
    joint resample, DTE +-2     39         49.1%           87%            49%
    target                                 50.0%           90%            50%

Flat IV is wrong in both ways at once, and worst at 4-6 DTE where we actually trade
(mean percentile 70.3%, coverage 45%). Resampling the (spot shape, IV change) pair fixes
both. Conditioning the pool on DTE was tried and rejected: it changed nothing, so the
simpler model stands.

Usage:
    python forward_calibration.py                # full table
    python forward_calibration.py --at 12:00     # a different decision point
    python forward_calibration.py --detail       # per-day rows as well
"""
import datetime as dt
import glob
import os
import statistics as st
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.normpath(os.path.join(SCRIPT_DIR, "..", "..")))

import straddle_analytics as sa
import forward_model as fm

MIN_HISTORY = 25          # days of pool before a day becomes testable
MIN_POOL = 20             # refuse to resample from fewer afternoons than this


def _flat(dd, pairs, t0):
    """Baseline: same engine, IV pinned. Kept so the comparison is apples to apples."""
    return fm.forward(dd, [(lo, hi, cl, 0.0, d) for lo, hi, cl, _iv, d in pairs], t0)


def _joint(dd, pairs, t0, window=None):
    use = pairs
    if window is not None:
        own = fm.dte_of(dd)
        use = [p for p in pairs if abs(p[4] - own) <= window] or pairs
    return fm.forward(dd, use, t0)


def run(t0="13:00", detail=False):
    dates = sorted(os.path.basename(p) for p in glob.glob(os.path.join(fm.DATA, "2026-*")))
    pool = fm.build_pool(dates)
    print(f"  captured days {len(dates)} · usable afternoons in the pool {len(pool)} · "
          f"decision point {t0}\n")
    variants = {"flat IV (baseline)": lambda dd, p: _flat(dd, p, t0),
                "joint resample": lambda dd, p: _joint(dd, p, t0),
                "joint resample, DTE +-2": lambda dd, p: _joint(dd, p, t0, 2)}
    pct = {k: [] for k in variants}
    by_dte = {}
    rows = []
    for i, date in enumerate(dates):
        if i < MIN_HISTORY:
            continue
        dd = fm.day_data(date)
        if not dd or fm.dte_of(dd) <= 0:
            continue
        actual = fm.actual_outcome(dd, t0)
        if not actual:
            continue
        pairs = [pool[d] for d in dates[:i] if d in pool]
        if len(pairs) < MIN_POOL:
            continue
        row = {"date": date, "dte": fm.dte_of(dd), "actual": actual[0], "how": actual[1]}
        for name, fn in variants.items():
            f = fn(dd, pairs)
            if not f:
                continue
            outcomes = f[0]
            p = sum(1 for x in outcomes if x < actual[0]) / len(outcomes) * 100
            pct[name].append(p)
            row[name] = p
            if name == "joint resample":
                row["p05"], row["p50"], row["p95"] = (
                    outcomes[int(0.05 * len(outcomes))],
                    outcomes[len(outcomes) // 2],
                    outcomes[int(0.95 * len(outcomes))])
        iv0, iv1 = fm.atm_iv_at(dd, t0), fm.atm_iv_at(dd, fm.SQUAREOFF)
        if iv0 is not None and iv1 is not None:
            row["d_iv"] = (iv1 - iv0) * 100
            by_dte.setdefault(row["dte"], []).append(row["d_iv"])
        rows.append(row)

    if detail:
        print(f"  {'date':<12}{'DTE':>4}{'p05':>9}{'p50':>9}{'p95':>9}{'ACTUAL':>9}"
              f"{'pctile':>8}{'dIV':>7}  how")
        for r in rows:
            print(f"  {r['date']:<12}{r['dte']:>4}{r.get('p05', 0):>+9,.0f}"
                  f"{r.get('p50', 0):>+9,.0f}{r.get('p95', 0):>+9,.0f}{r['actual']:>+9,.0f}"
                  f"{r.get('joint resample', float('nan')):>7.0f}%"
                  f"{r.get('d_iv', float('nan')):>+7.2f}  {r['how']}")
        print()

    print(f"  {'model':<26}{'n':>4}{'mean pctile':>13}{'inside 5-95':>13}{'above median':>14}")
    print(f"  {'-' * 70}")
    for name in variants:
        s = pct[name]
        if not s:
            continue
        inside = sum(1 for x in s if 5 <= x <= 95)
        above = sum(1 for x in s if x > 50)
        print(f"  {name:<26}{len(s):>4}{st.mean(s):>12.1f}%"
              f"{inside / len(s) * 100:>12.0f}%{above / len(s) * 100:>13.0f}%")
    print(f"  {'target':<26}{'':>4}{50.0:>12.1f}%{90:>12.0f}%{50:>13.0f}%")

    print(f"\n  intraday IV drift {t0} -> {fm.SQUAREOFF} (the reason flat IV fails):")
    print(f"  {'DTE':>4}{'n':>4}{'mean':>9}{'median':>9}{'negative':>10}")
    pooled = []
    for k in sorted(by_dte):
        v = by_dte[k]
        pooled += v if k >= 4 else []
        print(f"  {k:>4}{len(v):>4}{st.mean(v):>+9.2f}{st.median(v):>+9.2f}"
              f"{f'{sum(1 for x in v if x < 0)}/{len(v)}':>10}")
    if pooled:
        print(f"  DTE 4-6 pooled: n={len(pooled)} median {st.median(pooled):+.2f}pp "
              f"~ Rs{abs(st.median(pooled)) * 1900:,.0f} at vega Rs1,900/pp "
              f"(a whole day's net theta is ~Rs100)")
    return 0


if __name__ == "__main__":
    argv = sys.argv
    at = argv[argv.index("--at") + 1] if "--at" in argv else "13:00"
    sys.exit(run(at, "--detail" in argv))
