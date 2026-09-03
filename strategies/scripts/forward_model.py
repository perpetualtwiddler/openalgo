#!/usr/bin/env python3
"""forward_model.py — what can still happen to the open fly, in rupees.

WHY THIS EXISTS. A short straddle cannot be exited well on backward-looking numbers.
Distance travelled, profit given back, vol already realised — all of it is already in the
mark, so conditioning on it is the disposition effect with extra steps. The only two
quantities an exit decision needs are: how much can the net STILL change, and how much can
we STILL earn. This module answers the first in rupees.

HOW. Take the position as it stands, then replay real historical afternoons over it. For
each prior day we keep a PAIR — (low, high, close) of its 13:00->15:00 move as fractions,
together with the ATM IV change over the same window. Applying a pair means: shift spot by
its shape, shift IV by its IV change, check whether our breach guard would have fired, and
reprice all four legs at the square-off. Do that over every prior day and the result is a
distribution of rupee outcomes.

WHY PAIRS AND NOT A FLAT IV. Measured over 39 days (13:00 decision point, DTE>=1, scored
only against days that preceded each one):

    model                        n   mean pctile   inside 5-95   above median
    flat IV                     39         69.3%           46%            72%
    joint resample              39         49.4%           87%            46%
    target                                 50.0%           90%            50%

Holding IV flat is biased AND overconfident: the real outcome beat its median on 72% of
days and fell outside its 5-95 band on more than half of them. The cause is that vega is ~Rs1,900/pp
while IV moves 0.3pp on an ordinary afternoon, so a flat-IV model sources all its spread
from spot alone. Resampling the pair fixes both at once, and fixes them with no fitted
parameter — the pairs are drawn, not tuned. Matching on DTE was tried and added nothing
(49.1% vs 49.4%, same coverage), so it is deliberately not done.

The drift being systematic is not an accident: at 4-6 DTE, IV fell between 13:00 and 15:00
on 22 of 29 days, median -0.29pp, worth ~Rs550 against a whole day's net theta of ~Rs100.
The gap/VIX/ORB entry filters select calm mornings and calm mornings bleed vol.

DATA. /root/data/zerodha/trade-data/<date>/ — metadata.json (ATM, expiry, spot at entry),
nifty_index_1m.csv, india_vix_5m.csv, options/<symbol>_1m.csv at ATM+-500. 75 days from
2026-05-14. Two known gaps, both confirmed by this module's own cross-checks:
  * The capture takes the EXPIRING weekly, so on roll days it holds a different series than
    the strategy traded (2026-08-18, 08-25, 09-01). That is backlog #4.
  * Capture and strategy read spot at slightly different instants and round to different
    strikes when spot sits near a midpoint (2026-08-06, 08-10, 08-13).
So 12 of our 18 traded days are replayable; all 74 are usable for testing the MODEL, which
builds its fly from the capture's own ATM and expiry consistently.

Usage:
    python forward_model.py 2026-09-03              # distribution at 13:00
    python forward_model.py 2026-09-03 --at 12:53
"""
import csv
import datetime as dt
import glob
import json
import os
import statistics as st
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.normpath(os.path.join(SCRIPT_DIR, "..", "..")))

import charges as chg
import straddle_analytics as sa

DATA = os.getenv("TRADE_DATA_DIR", "/root/data/zerodha/trade-data")
WING, LOT_SIZE, LOTS = 400, 65, 2
QTY = LOT_SIZE * LOTS
BREACH_PCT = 0.55                 # the strategy's own guard, +-% of the ATM strike
FILL_SHORTFALL = 70.0             # measured: /stradexit marks vs where fills actually land
SQUAREOFF = "15:00"
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

_charges = lambda fills: chg.charges_from_fills(fills, True)


# ─────────────────────────────────────────────────────────────── loading
def _series(path, ohlc=False):
    """HH:MM -> close, or -> (high, low, close). Empty dict if absent."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for row in csv.DictReader(fh):
            try:
                key = row["timestamp"][11:16]
                out[key] = ((float(row["high"]), float(row["low"]), float(row["close"]))
                            if ohlc else float(row["close"]))
            except (TypeError, ValueError, KeyError):
                continue
    return out


def day_data(date):
    """Everything needed to rebuild and mark the fly for one captured date, or None."""
    meta_path = os.path.join(DATA, date, "metadata.json")
    if not os.path.exists(meta_path):
        return None
    meta = json.load(open(meta_path))
    atm, tag = meta.get("atm_strike"), meta.get("nifty_expiry_tag")
    if not atm or not tag:
        return None
    dd, mon, yy = meta["nifty_expiry"].split("-")          # '08-SEP-26'
    expiry = dt.datetime(2000 + int(yy), MONTHS[mon], int(dd), 15, 15)
    spec = [(f"NIFTY{tag}{atm}CE",        -QTY, atm,         "C"),
            (f"NIFTY{tag}{atm}PE",        -QTY, atm,         "P"),
            (f"NIFTY{tag}{atm + WING}CE",  QTY, atm + WING,  "C"),
            (f"NIFTY{tag}{atm - WING}PE",  QTY, atm - WING,  "P")]
    marks = {}
    for sym, _q, _k, _cp in spec:
        s = _series(os.path.join(DATA, date, "options", f"{sym}_1m.csv"))
        if not s:
            return None                                     # a missing leg is fatal
        marks[sym] = s
    spot = _series(os.path.join(DATA, date, "nifty_index_1m.csv"), ohlc=True)
    if not spot:
        return None
    return {"date": date, "atm": atm, "expiry": expiry, "spec": spec,
            "marks": marks, "spot": spot, "meta": meta}


def legs_at(dd, hhmm, entry="09:35"):
    """The four legs with entry and current mark, or None if any price is missing."""
    out = []
    for sym, qty, strike, cp in dd["spec"]:
        e, m = dd["marks"][sym].get(entry), dd["marks"][sym].get(hhmm)
        if e is None or m is None:
            return None
        out.append({"symbol": sym, "qty": qty, "entry": e, "mark": m,
                    "strike": strike, "cp": cp})
    return out


def dte_of(dd):
    return (dd["expiry"].date() - dt.date.fromisoformat(dd["date"])).days


def _breach_band(dd):
    return dd["atm"] * (1 - BREACH_PCT / 100), dd["atm"] * (1 + BREACH_PCT / 100)


def _T(dd, hhmm):
    return sa.years_to(dd["expiry"],
                       dt.datetime.strptime(f"{dd['date']} {hhmm}", "%Y-%m-%d %H:%M"))


def atm_iv_at(dd, hhmm):
    legs = legs_at(dd, hhmm)
    T = _T(dd, hhmm)
    if not legs or hhmm not in dd["spot"] or T <= 0:
        return None
    return sa.atm_iv(legs, sa.leg_ivs(legs, dd["spot"][hhmm][2], T))


# ─────────────────────────────────────────────────── the truth, for scoring
def actual_outcome(dd, t0="13:00", t_exit=SQUAREOFF):
    """What the position REALLY did from t0, under the strategy's own exit rules.

    Available even on days the live strategy exited early on a target, because the capture
    holds prices for the whole session — which is what makes an unbiased score possible.
    """
    legs = legs_at(dd, t0)
    if legs is None:
        return None
    blo, bhi = _breach_band(dd)
    for t in sorted(t for t in dd["spot"] if t0 < t <= t_exit):
        _hi, _lo, _cl = dd["spot"][t]
        if _lo <= blo or _hi >= bhi:
            marks = {s: dd["marks"][s].get(t) for s, *_ in dd["spec"]}
            if any(v is None for v in marks.values()):
                continue
            return sa.net_at(legs, marks, _charges) - FILL_SHORTFALL, "BREACH", t
    marks = {s: dd["marks"][s].get(t_exit) for s, *_ in dd["spec"]}
    if any(v is None for v in marks.values()):
        return None
    return sa.net_at(legs, marks, _charges), t_exit, t_exit


# ───────────────────────────────────────────────────────── the path pool
def pair_for(date, t0="13:00", t1=SQUAREOFF):
    """One prior day as (low%, high%, close%, dIV_pp, dte) — the unit of resampling.

    DTE 0 is refused: as T->0 the backed-out IV explodes (mean +19.7pp across our 0-DTE
    captures), which is an artefact of the inversion, not a vol event. Feeding those into
    the pool would poison every draw.
    """
    dd = day_data(date)
    if not dd or dte_of(dd) <= 0 or t0 not in dd["spot"]:
        return None
    base = dd["spot"][t0][2]
    window = [v for t, v in dd["spot"].items() if t0 < t <= t1]
    if len(window) < 60 or not base:
        return None
    iv0, iv1 = atm_iv_at(dd, t0), atm_iv_at(dd, t1)
    if iv0 is None or iv1 is None:
        return None
    lo = min(v[1] for v in window)
    hi = max(v[0] for v in window)
    close = [v for t, v in sorted(dd["spot"].items()) if t <= t1][-1][2]
    return ((lo - base) / base, (hi - base) / base, (close - base) / base,
            (iv1 - iv0) * 100, dte_of(dd))


def build_pool(dates):
    """{date: pair} for every date that yields one. Caller slices it to avoid look-ahead."""
    return {d: p for d in dates for p in [pair_for(d)] if p}


# ─────────────────────────────────────────────────────────── the engine
def forward(dd, pairs, t0="13:00"):
    """Distribution of rupee outcomes from t0, one entry per resampled afternoon.

    Returns (sorted_outcomes, legs, spot, ivs) or None.
    """
    legs = legs_at(dd, t0)
    if legs is None:
        return None
    spot = dd["spot"][t0][2]
    T_now, T_exit = _T(dd, t0), _T(dd, SQUAREOFF)
    if T_now <= 0:
        return None
    ivs = sa.leg_ivs(legs, spot, T_now)
    blo, bhi = _breach_band(dd)
    outcomes = []
    for rlo, rhi, rclose, div_pp, _dte in pairs:
        lo_s, hi_s = spot * (1 + rlo), spot * (1 + rhi)
        if lo_s <= blo or hi_s >= bhi:
            # The guard fires at an unknown earlier time, so only part of the day's IV
            # drift has elapsed. Use IV as-now: breaches arrive with IV rising, not
            # falling, so crediting the drift here would flatter the estimate.
            level = blo if lo_s <= blo else bhi
            outcomes.append(sa.net_at(legs, sa.price_all(legs, level, T_now, ivs),
                                      _charges) - FILL_SHORTFALL)
        else:
            shifted = {k: max(v + div_pp / 100.0, 1e-6) for k, v in ivs.items()}
            outcomes.append(sa.net_at(legs, sa.price_all(legs, spot * (1 + rclose),
                                                         T_exit, shifted), _charges))
    return sorted(outcomes), legs, spot, ivs


def summarise(outcomes):
    n = len(outcomes)
    q = lambda f: outcomes[min(int(f * n), n - 1)]
    return {"n": n, "p05": q(0.05), "p25": q(0.25), "median": q(0.50),
            "p75": q(0.75), "p95": q(0.95), "mean": st.mean(outcomes),
            "positive_pct": sum(1 for x in outcomes if x > 0) / n * 100}


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip().split("Usage:")[-1])
        return 2
    date = argv[1]
    t0 = argv[argv.index("--at") + 1] if "--at" in argv else "13:00"
    dd = day_data(date)
    if not dd:
        print(f"  no usable capture for {date}")
        return 1
    dates = sorted(os.path.basename(p) for p in glob.glob(os.path.join(DATA, "2026-*")))
    prior = [d for d in dates if d < date]
    pool = build_pool(prior)
    if len(pool) < 20:
        print(f"  only {len(pool)} prior afternoons available — too few to resample")
        return 1
    f = forward(dd, list(pool.values()), t0)
    if not f:
        print(f"  cannot mark {date} at {t0}")
        return 1
    outcomes, legs, spot, ivs = f
    net_now = sa.net_at(legs, {l["symbol"]: l["mark"] for l in legs}, _charges)
    s = summarise(outcomes)
    theta = sa.theta_per_hour(legs, spot, _T(dd, t0), ivs, lambda _f: 0)
    hours = (dt.datetime.strptime(SQUAREOFF, "%H:%M")
             - dt.datetime.strptime(t0, "%H:%M")).total_seconds() / 3600
    print(f"  {date} {t0} · {dte_of(dd)} DTE · spot {spot:,.0f} · ATM {dd['atm']:,} · "
          f"IV {sa.atm_iv(legs, ivs) * 100:.2f}%")
    print(f"  net now {net_now:+,.0f}   (sunk — not a decision input)")
    print(f"  forward, over {s['n']} prior afternoons:")
    for k in ("p05", "p25", "median", "p75", "p95"):
        print(f"     {k:<7}{s[k]:>+10,.0f}")
    print(f"     ends positive  {s['positive_pct']:>6.0f}%")
    print(f"  still earnable by waiting: Rs{theta * hours:,.0f}  "
          f"({theta:,.0f}/hr x {hours:.1f}h)")
    act = actual_outcome(dd, t0)
    if act:
        pct = sum(1 for x in outcomes if x < act[0]) / s["n"] * 100
        print(f"  ACTUAL {act[0]:+,.0f} via {act[1]} — the {pct:.0f}th percentile of the above")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
