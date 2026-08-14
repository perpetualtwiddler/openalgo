#!/usr/bin/env python3
"""trade_journal.py — one durable CSV record per live straddle trading day.

Why this exists: the per-day facts we care about are scattered across four places
(the strategy log, margin.csv, slippage.csv, and the broker's order/trade book),
and three of those are transient. The broker tradebook is CURRENT-DAY ONLY, so a
day not captured before midnight is gone. The strategy log survives but is noisy
and, on failure days, actively wrong (see the 2026-08-07 note below). This script
distils each day into one row and appends it to a CSV that we keep forever.

CHARGES — the important subtlety. Zerodha bills brokerage at Rs20 per *executed
order*, NOT per fill. A single 130-qty order that the exchange fills in two 65-qty
tranches is ONE order = Rs20. eod_summary.py reads the tradebook and multiplies
fills by Rs20, which overstates cost whenever a leg partial-fills (first observed
2026-08-13: 9 fills for 8 orders, overstating the day's loss by Rs23.60). This
script counts ORDERS, so its charges are the defensible ones.

CONFIDENCE — every row carries a `confidence` column, because the historical days
are not equally knowable:
  high   — every fill price recovered; gross reconciles to broker m2mrealized
  medium — gross known from a broker reading taken on the day, fills unrecoverable
  low    — reconstructed from a manual/partial exit; treat as an estimate

Usage:
    python trade_journal.py                    # today, append
    python trade_journal.py 2026-08-13         # a specific date, append
    python trade_journal.py --backfill         # rebuild all known live days
    python trade_journal.py --print 2026-08-13 # one row to stdout, no write
    python trade_journal.py --report           # human-readable summary + totals

Env:
    TRADE_JOURNAL_CSV   output path (default <repo>/log/trade_journal.csv)
"""
import csv
import glob
import json
import os
import re
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)

import charges as chg  # noqa: E402

LOG_GLOB = os.path.join(REPO_ROOT, "log", "strategies", "*straddle*_%s_*")
MARGIN_CSV = os.path.join(REPO_ROOT, "log", "margin.csv")
SLIPPAGE_CSV = os.path.join(REPO_ROOT, "log", "slippage.csv")
OUT_CSV = os.getenv("TRADE_JOURNAL_CSV", os.path.join(REPO_ROOT, "log", "trade_journal.csv"))

BREACH_PCT = 0.55

COLS = [
    "date", "weekday", "series_code", "expiry", "dte", "lots", "qty",
    "orb_low", "orb_high", "orb_range",
    "spot_entry", "atm_strike", "wing_width",
    "ce_entry", "ce_exit", "pe_entry", "pe_exit",
    "hce_entry", "hce_exit", "hpe_entry", "hpe_exit",
    "straddle_entry", "straddle_exit",
    "gross_premium", "hedge_cost", "premium_collected",
    "margin_blocked", "max_risk_defined",
    "breach_lo", "breach_hi", "spot_exit", "breached",
    "entry_time", "exit_time", "exit_reason",
    "n_orders", "n_fills", "slip_entry", "slip_exit",
    "mfe", "mae",
    "gross_pnl", "charges", "net_pnl",
    "roi_on_margin_pct", "net_pct_of_premium",
    "confidence", "notes",
]

# Facts that cannot be recovered from any surviving artefact. Each entry records
# WHERE the number came from, so a future reader can judge it.
OVERRIDES = {
    # Exited by hand in the Zerodha terminal at ~15:12, before our 15:14 square-off,
    # so no [EXIT] fills were ever written to our log. Gross is the P&L Zerodha
    # displayed; charges are therefore backed out, not modelled.
    "2026-08-06": {
        "exit_reason": "MANUAL_ZERODHA",
        "exit_time": "15:12:33",
        "gross_pnl": 299.00,
        "charges": 285.20,
        "net_pnl": 13.80,
        "n_orders": 8,
        "confidence": "low",
        "notes": "manual exit in Zerodha terminal; exit fills absent from log; "
                 "gross per Zerodha screen, charges backed out",
    },
    # Broker quote API (kt-quotes) failed mid-basket: the two PE legs never placed.
    # The strategy correctly flattened the two CE legs ~3s later. The log's
    # "Total P&L: -15840" is WRONG — it used a 0.00 entry price for the CE it never
    # captured. Real result came from the broker reading taken that day.
    "2026-08-07": {
        "exit_reason": "ENTRY_PARTIAL_FAILURE",
        "exit_time": "09:35:18",
        "gross_pnl": -248.79,
        "charges": 0.0,
        "net_pnl": -248.79,
        "n_orders": 4,
        "premium_collected": "",
        "confidence": "medium",
        "notes": "PE legs failed (kt-quotes outage); CE legs flattened in 3s; "
                 "log P&L -15840 is a 0.00-entry-price artefact, ignore it; "
                 "figure is the broker reading taken on the day",
    },
}


def _rd(path):
    return list(csv.DictReader(open(path))) if os.path.exists(path) else []


TRADEBOOK_DIR = os.path.join(REPO_ROOT, "log", "tradebook")


def _exit_from_tradebook(date, short_syms):
    """Exit prices from an archived broker tradebook, for days our log never recorded them.

    A MANUAL exit (closed by hand in the Zerodha terminal) writes no [EXIT] lines, so the
    log-parsing path yields nothing — that is why 2026-08-06 is stuck at `low` confidence.
    The broker tradebook has the real fills, but it is CURRENT-DAY ONLY, so it must be
    archived on the day (see archive_tradebook.py) or the day is lost forever.

    Closing side is inferred from the leg's role: we SELL the ATM shorts at entry, so their
    exit fills are the BUYs; the wings are bought at entry, so their exit fills are the SELLs.
    Multiple fills per leg are volume-weighted — and `n_orders` counts distinct order ids,
    never fills, because Zerodha bills brokerage per order (2026-08-14: 11 fills, 8 orders).

    Returns (exit_prices, n_orders, n_fills) or (None, 0, 0).
    """
    p = os.path.join(TRADEBOOK_DIR, f"{date}.json")
    if not os.path.exists(p):
        return None, 0, 0
    tb = json.load(open(p)).get("trades") or []
    if not tb:
        return None, 0, 0
    per = {}
    oids = set()
    for t in tb:
        sym = t.get("symbol") or ""
        act = (t.get("action") or "").upper()
        qty = abs(int(float(t.get("quantity") or 0)))
        px = float(t.get("average_price") or t.get("price") or 0)
        if t.get("orderid"):
            oids.add(str(t["orderid"]))
        closing = "BUY" if sym in short_syms else "SELL"
        if act != closing:
            continue                          # this is the opening fill, not the exit
        d = per.setdefault(sym, [0, 0.0])
        d[0] += qty
        d[1] += qty * px
    ex = {s: round(v / q, 2) for s, (q, v) in per.items() if q}
    return (ex or None), len(oids), len(tb)


def charges_per_order(fills, n_orders):
    """Zerodha options charges for a position expressed as one net fill per leg-side.

    Delegates the rate card to charges.py so there is a single home for it. Brokerage is
    the only component that depends on how fills group into orders (Rs20 each); STT, txn,
    stamp, SEBI and GST depend purely on turnover, which grouping cannot change. So we
    stamp synthetic order ids to make the distinct-id count exactly `n_orders` — which
    keeps this correct when a leg partial-filled and the real order count differs from
    the number of rows we hold (2026-08-14: 11 broker fills, 8 orders, 8 net leg-sides).
    """
    n = max(1, int(n_orders))
    tagged = [dict(f, orderid=f"o{i % n}") for i, f in enumerate(fills)]
    return chg.charges_from_fills(tagged, True)


def _f(m, i=1, cast=float):
    return cast(m.group(i)) if m else ""


def build(date):
    """Return one row dict for `date`, or None if the strategy never entered."""
    paths = sorted(glob.glob(LOG_GLOB % date.replace("-", "")))
    if not paths:
        return None
    raw = open(paths[-1], "rb").read().decode("utf-8", "replace").replace("\r", "\n")

    r = {c: "" for c in COLS}
    r["date"] = date
    r["weekday"] = datetime.strptime(date, "%Y-%m-%d").strftime("%a")

    m = re.search(r"\[INIT\] NIFTY Iron Butterfly \| (\d+) lot\(s\) x (\d+) = (\d+) qty", raw)
    if m:
        r["lots"], r["qty"] = int(m.group(1)), int(m.group(3))
    # Provisional: the nearest expiry the strategy saw at startup. On an expiry day where
    # EXPIRY_DAY_USE_NEXT_WEEK rolls us forward, this is NOT what we sold — the traded
    # symbols below overwrite it. Kept as the fallback for days with no parsable fills.
    m = re.search(r"\[EXPIRY\] Next expiry: ([0-9]{2}-[A-Z]{3}-[0-9]{2})", raw)
    if m:
        r["expiry"] = m.group(1)
        try:
            r["dte"] = (datetime.strptime(m.group(1), "%d-%b-%y")
                        - datetime.strptime(date, "%Y-%m-%d")).days
        except ValueError:
            pass
    m = re.search(r"\[TREND\] ORB\(15m\): ([\d.]+) — ([\d.]+) \(range (\d+)pts\)", raw)
    if m:
        r["orb_low"], r["orb_high"], r["orb_range"] = m.group(1), m.group(2), int(m.group(3))
    m = re.search(r"\[ENTRY\] NIFTY spot: ([\d.]+)", raw)
    if not m:
        return None                      # never attempted an entry: not a trade day
    r["spot_entry"] = float(m.group(1))
    m = re.search(r"\[ENTRY\] Placing ATM iron butterfly — expiry (\w+), qty (\d+)", raw)
    m2 = re.search(r"\[ENTRY\] Response:.*?'symbol': 'NIFTY\w*?(\d{5})CE'", raw)
    if m2:
        r["atm_strike"] = int(m2.group(1))
    # entry timestamp
    m = re.search(r"([\d-]{10}) ([\d:]{8})\s+\[ENTRY\] Placing", raw)
    if m:
        r["entry_time"] = m.group(2)

    # ---- premium block
    m = re.search(r"Gross premium: (\d+) \| Hedge cost: (\d+)", raw)
    if m:
        r["gross_premium"], r["hedge_cost"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Net premium collected: (\d+)", raw)
    if m:
        r["premium_collected"] = int(m.group(1))

    # ---- margin
    for row in _rd(MARGIN_CSV):
        if row["date"] == date:
            r["margin_blocked"] = round(float(row["margin_blocked"]), 2)
    m = re.search(r"vs defined max-loss Rs([\d,]+)", raw)
    if m:
        r["max_risk_defined"] = int(m.group(1).replace(",", ""))

    # ---- strikes / wing width from the 4 entry symbols
    syms = re.findall(r"'symbol': '(NIFTY\w+?(\d{5})(CE|PE))'", raw)
    strikes = sorted({int(s[1]) for s in syms})
    if r["atm_strike"] and len(strikes) >= 2:
        r["wing_width"] = max(strikes) - r["atm_strike"]

    # The series we ACTUALLY sold, e.g. 'NIFTY11AUG26' — the symbol prefix with the strike
    # and CE/PE trimmed. This is authoritative over the [EXPIRY] log line, which reports the
    # nearest expiry at startup and therefore disagrees on any expiry-day roll. Recompute
    # expiry/dte from it so a 0-DTE roll shows dte=7, not dte=0.
    if syms:
        sym, strike, opt = syms[0]
        r["series_code"] = sym[: len(sym) - len(strike) - len(opt)]
        tag = r["series_code"][len("NIFTY"):]              # '11AUG26'
        try:
            exp = datetime.strptime(tag, "%d%b%y")
            r["expiry"] = exp.strftime("%d-%b-%y").upper()
            r["dte"] = (exp - datetime.strptime(date, "%Y-%m-%d")).days
        except ValueError:
            pass

    # ---- breach band (ATM +/- BREACH_PCT), as the strategy computes it
    if r["atm_strike"]:
        d = r["atm_strike"] * BREACH_PCT / 100.0
        r["breach_lo"], r["breach_hi"] = round(r["atm_strike"] - d), round(r["atm_strike"] + d)

    # ---- fills: entry from slippage.csv (records real fill_price), exit from log
    slip = [s for s in _rd(SLIPPAGE_CSV) if s["date"] == date]
    ent = {s["symbol"]: float(s["fill_price"]) for s in slip if s["phase"] == "ENTRY"}
    r["slip_entry"] = round(sum(float(s["slip_rupees"]) for s in slip if s["phase"] == "ENTRY"), 2) or ""
    r["slip_exit"] = round(sum(float(s["slip_rupees"]) for s in slip if s["phase"] == "EXIT"), 2) or ""

    ex = {}
    for lbl, sym, px in re.findall(
            r"\[EXIT\] (CE|PE|HEDGE CE|HEDGE PE) closed: (\S+) (?:BUY|SELL) \d+ @ ([\d.]+)", raw):
        ex[sym] = float(px)
    m = re.search(r"\[EXIT\] Closing iron butterfly — reason: ([A-Z_]+)", raw)
    if m:
        r["exit_reason"] = m.group(1)
    ts = re.findall(r"([\d:]{8})\s+\[EXIT\] \S+ closed", raw)
    if ts:
        r["exit_time"] = ts[-1]

    def leg(kind, opt):
        """kind: 'atm' or 'wing'."""
        for s, k, o in syms:
            st = int(k)
            is_atm = st == r["atm_strike"]
            if o == opt and (is_atm if kind == "atm" else not is_atm):
                return s
        return None

    pairs = [("ce", leg("atm", "CE")), ("pe", leg("atm", "PE")),
             ("hce", leg("wing", "CE")), ("hpe", leg("wing", "PE"))]

    # No [EXIT] lines in the log => closed outside our code (manual Zerodha exit). Fall back
    # to the archived broker tradebook, which carries the real fills. Keeps such a day at
    # `high` confidence instead of the guesswork that left 2026-08-06 at `low`.
    tb_orders = tb_fills = 0
    if not ex:
        shorts = {s for k, s in pairs[:2] if s}
        tb_ex, tb_orders, tb_fills = _exit_from_tradebook(date, shorts)
        if tb_ex:
            ex = tb_ex
            r["exit_reason"] = r["exit_reason"] or "MANUAL_ZERODHA"
            r["notes"] = ("exit fills recovered from the archived broker tradebook "
                          "(no [EXIT] lines — closed manually); "
                          f"{tb_orders} orders / {tb_fills} fills")
    for key, sym in pairs:
        if sym:
            r[f"{key}_entry"] = ent.get(sym, "")
            r[f"{key}_exit"] = ex.get(sym, "")
    if r["ce_entry"] != "" and r["pe_entry"] != "":
        r["straddle_entry"] = round(r["ce_entry"] + r["pe_entry"], 2)
    if r["ce_exit"] != "" and r["pe_exit"] != "":
        r["straddle_exit"] = round(r["ce_exit"] + r["pe_exit"], 2)

    # ---- last observed spot + MFE/MAE from the monitor lines
    spots = re.findall(r"NIFTY (\d{5})(?:\s|$)", raw)
    if spots:
        r["spot_exit"] = int(spots[-1])
    pnls = [int(x) for x in re.findall(r"Net P&L: ([-+]?\d+)", raw)]
    if pnls:
        r["mfe"], r["mae"] = max(pnls), min(pnls)
    if r["breach_lo"] and r["spot_exit"]:
        lo = min(int(s) for s in spots)
        hi = max(int(s) for s in spots)
        r["breached"] = "Y" if (lo <= r["breach_lo"] or hi >= r["breach_hi"]) else "N"

    # ---- P&L from fills (per-order charges)
    fills = []
    for key, sym in pairs:
        if not sym:
            continue
        e, x = r[f"{key}_entry"], r[f"{key}_exit"]
        if e == "" or x == "":
            continue
        short = key in ("ce", "pe")
        q = r["qty"] or 130
        fills.append({"action": "SELL" if short else "BUY", "quantity": q, "price": e})
        fills.append({"action": "BUY" if short else "SELL", "quantity": q, "price": x})
    if len(fills) == 8:
        gross = sum((1 if f["action"] == "SELL" else -1) * f["quantity"] * f["price"] for f in fills)
        ch = charges_per_order(fills, r["n_orders"] if isinstance(r["n_orders"], int) else 8)
        # 8 legs => 8 orders, unless the archived tradebook shows real fill counts
        # (a leg can partial-fill; brokerage still follows ORDERS).
        r["n_orders"] = tb_orders or 8
        r["n_fills"] = tb_fills or 8
        r["gross_pnl"] = round(gross, 2)
        r["charges"] = round(ch, 2)
        r["net_pnl"] = round(gross - ch, 2)
        r["confidence"] = "high"

    # ---- apply documented overrides last: they describe irrecoverable days
    r.update(OVERRIDES.get(date, {}))

    if r["net_pnl"] != "" and r["margin_blocked"]:
        r["roi_on_margin_pct"] = round(100.0 * float(r["net_pnl"]) / float(r["margin_blocked"]), 3)
    if r["net_pnl"] != "" and r["premium_collected"]:
        r["net_pct_of_premium"] = round(100.0 * float(r["net_pnl"]) / float(r["premium_collected"]), 2)
    return r


def upsert(rows):
    have = {x["date"]: x for x in _rd(OUT_CSV)}
    for x in rows:
        have[x["date"]] = x
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for d in sorted(have):
            w.writerow({c: have[d].get(c, "") for c in COLS})
    return len(have)


def report():
    rows = _rd(OUT_CSV)
    if not rows:
        print("no journal yet")
        return
    print(f"  === STRADDLE TRADE JOURNAL — {len(rows)} trade days ===\n")
    print(f"  {'date':<12}{'series':<14}{'dte':>4}{'atm':>7}{'prem':>8}{'gross':>10}{'chg':>8}"
          f"{'net':>10}{'ROM%':>7}{'MFE':>8}{'MAE':>8}  {'exit':<22}{'conf'}")
    tn = tg = tc = 0.0
    for x in rows:
        n = float(x["net_pnl"] or 0); g = float(x["gross_pnl"] or 0); c = float(x["charges"] or 0)
        tn += n; tg += g; tc += c
        print(f"  {x['date']:<12}{x.get('series_code') or '-':<14}{x['dte'] or '?':>4}{x['atm_strike'] or '-':>7}"
              f"{x['premium_collected'] or '-':>8}{g:>+10,.0f}{c:>8,.0f}{n:>+10,.0f}"
              f"{(x['roi_on_margin_pct'] or '-'):>7}{x['mfe'] or '-':>8}{x['mae'] or '-':>8}"
              f"  {(x['exit_reason'] or '-'):<22}{x['confidence']}")
    wins = sum(1 for x in rows if float(x["net_pnl"] or 0) > 0)
    print(f"\n  {'TOTAL':<12}{'':<14}{'':>4}{'':>7}{'':>8}{tg:>+10,.0f}{tc:>8,.0f}{tn:>+10,.0f}")
    print(f"  win rate {wins}/{len(rows)}   avg net {tn/len(rows):+,.2f}   "
          f"charge drag {tc:,.2f} ({100*tc/max(tg,1e-9):.1f}% of gross)" if tg > 0 else
          f"  win rate {wins}/{len(rows)}   avg net {tn/len(rows):+,.2f}   charges {tc:,.2f}")
    lows = [x["date"] for x in rows if x["confidence"] != "high"]
    if lows:
        print(f"  NOTE: {len(lows)} row(s) not fill-verified: {', '.join(lows)}")


def main(argv):
    if "--report" in argv:
        return report()
    if "--backfill" in argv:
        dates = sorted({os.path.basename(p).split("_")[-3] for p in
                        glob.glob(os.path.join(REPO_ROOT, "log", "strategies", "*straddle*_2026*"))})
        rows = []
        for d in dates:
            if len(d) != 8:
                continue
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            row = build(iso)
            if row:
                rows.append(row)
                print(f"  [ok] {iso}  net {row['net_pnl']}  ({row['confidence']})")
            else:
                print(f"  [skip] {iso} — no entry")
        print(f"\n  journal now has {upsert(rows)} rows -> {OUT_CSV}")
        return
    pr = "--print" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    date = args[0] if args else datetime.now().strftime("%Y-%m-%d")
    row = build(date)
    if not row:
        print(f"  [skip] {date} — no entry that day")
        return
    if pr:
        w = csv.DictWriter(sys.stdout, fieldnames=COLS)
        w.writeheader()
        w.writerow(row)
        return
    upsert([row])
    print(f"  [ok] {date}  gross {row['gross_pnl']}  charges {row['charges']}  "
          f"net {row['net_pnl']}  ({row['confidence']}) -> {OUT_CSV}")


if __name__ == "__main__":
    main(sys.argv)
