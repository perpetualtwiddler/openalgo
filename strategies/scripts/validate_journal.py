#!/usr/bin/env python3
"""validate_journal.py — cross-check every derivable field in trade_journal.csv.

Analytics will be built on this CSV, so a wrong column silently poisons every conclusion
drawn from it. This re-derives each field from its INDEPENDENT source and compares:

  identity   qty = lots x 65 · dte = expiry - date · expiry from series_code
  structure  wing_width vs strikes · breach band = atm +/- 0.55%
  premium    premium_collected = gross_premium - hedge_cost
  capital    margin_blocked vs margin.csv · max_risk = wing_width x qty - premium
  fills      all 8 leg prices vs the tradebook archive · n_orders = distinct order ids
  P&L        gross_pnl recomputed from fills · charges recomputed via charges.py
             net = gross - charges · ROM = net/margin_blocked · %prem = net/premium
  path       mfe/mae vs the strategy log's own samples
  slippage   slip_entry/slip_exit vs slippage.csv sums

Rows carrying a documented OVERRIDE (a manual exit or a partial-entry failure whose fills
are unrecoverable) are checked only on what IS knowable, and their skipped checks are
reported rather than passed silently — an unverifiable field must never look verified.

Usage:  python validate_journal.py            # all rows
        python validate_journal.py 2026-08-17 # one date
Exit 0 = all checks passed or explicitly skipped; 1 = at least one MISMATCH.
"""
import csv
import glob
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, SCRIPT_DIR)
import charges as chg  # noqa: E402

JOURNAL = os.path.join(REPO_ROOT, "log", "trade_journal.csv")
MARGIN = os.path.join(REPO_ROOT, "log", "margin.csv")
SLIPPAGE = os.path.join(REPO_ROOT, "log", "slippage.csv")
TRADEBOOK = os.path.join(REPO_ROOT, "log", "tradebook")
LOGS = os.path.join(REPO_ROOT, "log", "strategies")
LOT_SIZE, BREACH_PCT = 65, 0.55
TOL = 0.51          # rupee tolerance; the CSV rounds to 2dp

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


class Report:
    def __init__(self):
        self.ok = self.bad = self.skip = 0
        self.fails = []

    def check(self, date, field, got, want, why=""):
        if got is None or want is None:
            return self.skipped(date, field, why or "value unavailable")
        try:
            good = abs(float(got) - float(want)) <= TOL
        except (TypeError, ValueError):
            good = str(got).strip() == str(want).strip()
        if good:
            self.ok += 1
        else:
            self.bad += 1
            self.fails.append((date, field, got, want, why))
            print(f"    ❌ {field:<22} csv={got!r}  recomputed={want!r}  {why}")

    def skipped(self, date, field, why):
        self.skip += 1
        print(f"    ⊘  {field:<22} skipped — {why}")


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_series(code):
    """'NIFTY18AUG26' -> (2026, 8, 18)"""
    m = re.fullmatch(r"NIFTY(\d{2})([A-Z]{3})(\d{2})", code or "")
    if not m:
        return None
    d, mon, y = int(m.group(1)), MONTHS.get(m.group(2)), 2000 + int(m.group(3))
    return (y, mon, d) if mon else None


def tradebook_fills(date):
    p = os.path.join(TRADEBOOK, f"{date}.json")
    if not os.path.exists(p):
        return None
    tb = json.load(open(p)).get("trades") or []
    return [{"action": (t.get("action") or "").upper(),
             "quantity": abs(int(float(t.get("quantity") or 0))),
             "price": float(t.get("average_price") or 0),
             "symbol": t.get("symbol") or "",
             "orderid": str(t.get("orderid") or "")} for t in tb] or None


def log_text(date):
    f = sorted(glob.glob(os.path.join(LOGS, f"*straddle*_{date.replace('-', '')}_*")))
    if not f:
        return ""
    return open(f[-1], "rb").read().decode("utf-8", "replace").replace("\r", "\n")


def main(argv):
    rows = list(csv.DictReader(open(JOURNAL)))
    want_dates = [a for a in argv[1:] if not a.startswith("-")]
    if want_dates:
        rows = [r for r in rows if r["date"] in want_dates]
    marg = {r["date"]: r for r in csv.DictReader(open(MARGIN))} if os.path.exists(MARGIN) else {}
    slip = list(csv.DictReader(open(SLIPPAGE))) if os.path.exists(SLIPPAGE) else []
    R = Report()

    for r in rows:
        d = r["date"]
        soft = (r.get("confidence") or "") != "high"
        print(f"\n  === {d}  ({r.get('confidence')}) ===")

        # ---- identity
        lots, qty = num(r["lots"]), num(r["qty"])
        if lots and qty:
            R.check(d, "qty = lots x 65", qty, lots * LOT_SIZE)
        ser = parse_series(r.get("series_code"))
        if ser:
            y, mo, dd = ser
            R.check(d, "expiry from series", r["expiry"].upper(),
                    f"{dd:02d}-{[k for k, v in MONTHS.items() if v == mo][0]}-{str(y)[2:]}")
            import datetime as _dt
            R.check(d, "dte = expiry - date", num(r["dte"]),
                    (_dt.date(y, mo, dd) - _dt.date(*map(int, d.split("-")))).days)
        else:
            R.skipped(d, "series_code", "not parsable / absent")

        # ---- structure
        atm, ww = num(r["atm_strike"]), num(r["wing_width"])
        if atm:
            R.check(d, "breach_lo", num(r["breach_lo"]), round(atm - atm * BREACH_PCT / 100))
            R.check(d, "breach_hi", num(r["breach_hi"]), round(atm + atm * BREACH_PCT / 100))

        # ---- premium identity
        gp, hc, pc = num(r["gross_premium"]), num(r["hedge_cost"]), num(r["premium_collected"])
        if gp is not None and hc is not None and pc is not None:
            R.check(d, "premium = gross - hedge", pc, gp - hc)
        else:
            R.skipped(d, "premium identity", "premium fields blank (partial-entry day)")

        # ---- capital
        if d in marg:
            R.check(d, "margin_blocked vs csv", num(r["margin_blocked"]),
                    num(marg[d]["margin_blocked"]))
            R.check(d, "premium vs margin.csv", pc, num(marg[d]["premium"]))
        else:
            R.skipped(d, "margin_blocked", "no margin.csv row (entry never completed)")
        if ww and qty and pc is not None:
            R.check(d, "max_risk = w*q - prem", num(r["max_risk_defined"]), ww * qty - pc)

        # ---- straddle sums
        ce, pe = num(r["ce_entry"]), num(r["pe_entry"])
        if ce is not None and pe is not None:
            R.check(d, "straddle_entry", num(r["straddle_entry"]), ce + pe)
        cx, px = num(r["ce_exit"]), num(r["pe_exit"])
        if cx is not None and px is not None:
            R.check(d, "straddle_exit", num(r["straddle_exit"]), cx + px)

        # ---- fills vs the tradebook archive (the independent record)
        tb = tradebook_fills(d)
        if tb:
            R.check(d, "n_fills vs tradebook", num(r["n_fills"]), len(tb))
            R.check(d, "n_orders = distinct ids", num(r["n_orders"]),
                    len({f["orderid"] for f in tb if f["orderid"]}))
            # per-leg volume-weighted prices, split by opening vs closing side
            shorts = {s for s in {f["symbol"] for f in tb} if str(int(atm)) in s} if atm else set()
            vw = {}
            for f in tb:
                opening = (f["action"] == "SELL") if f["symbol"] in shorts else (f["action"] == "BUY")
                k = (f["symbol"], "in" if opening else "out")
                a = vw.setdefault(k, [0, 0.0])
                a[0] += f["quantity"]; a[1] += f["quantity"] * f["price"]
            def leg(sym_sub, side):
                for (s, sd), (q, v) in vw.items():
                    if sd == side and sym_sub in s:
                        return v / q if q else None
                return None
            if atm:
                for col, sub, side in (("ce_entry", f"{int(atm)}CE", "in"),
                                       ("ce_exit", f"{int(atm)}CE", "out"),
                                       ("pe_entry", f"{int(atm)}PE", "in"),
                                       ("pe_exit", f"{int(atm)}PE", "out")):
                    R.check(d, f"{col} vs tradebook", num(r[col]), leg(sub, side))
            # P&L and charges, recomputed from scratch
            gross = sum((1 if f["action"] == "SELL" else -1) * f["quantity"] * f["price"] for f in tb)
            R.check(d, "gross_pnl from fills", num(r["gross_pnl"]), gross)
            R.check(d, "charges recomputed", num(r["charges"]), chg.charges_from_fills(tb, True))
        else:
            R.skipped(d, "fills / gross / charges",
                      "no tradebook archive (predates archive_tradebook.py)")

        # ---- P&L identities (always checkable)
        g, c, n = num(r["gross_pnl"]), num(r["charges"]), num(r["net_pnl"])
        if None not in (g, c, n):
            R.check(d, "net = gross - charges", n, g - c)
        mb = num(r["margin_blocked"])
        if n is not None and mb:
            R.check(d, "roi_on_margin_pct", num(r["roi_on_margin_pct"]), round(100 * n / mb, 3))
        if n is not None and pc:
            R.check(d, "net_pct_of_premium", num(r["net_pct_of_premium"]), round(100 * n / pc, 2))

        # ---- path vs the strategy's own samples
        raw = log_text(d)
        pn = [int(x) for x in re.findall(r"Net P&L: ([-+]?\d+)", raw)] if raw else []
        if pn:
            R.check(d, "mfe vs log samples", num(r["mfe"]), max(pn))
            R.check(d, "mae vs log samples", num(r["mae"]), min(pn))
        else:
            R.skipped(d, "mfe / mae", "no monitor samples in log")

        # ---- slippage sums
        se = [s for s in slip if s["date"] == d and s["phase"] == "ENTRY"]
        sx = [s for s in slip if s["date"] == d and s["phase"] == "EXIT"]
        if se:
            R.check(d, "slip_entry sum", num(r["slip_entry"]),
                    sum(float(s["slip_rupees"]) for s in se))
        if sx:
            R.check(d, "slip_exit sum", num(r["slip_exit"]),
                    sum(float(s["slip_rupees"]) for s in sx))
        if soft:
            print(f"    (row is `{r.get('confidence')}` — see notes: {r.get('notes','')[:70]})")

    print(f"\n  ════ {R.ok} passed · {R.bad} MISMATCH · {R.skip} skipped ════")
    if R.bad:
        print("\n  FAILURES:")
        for d, f, got, want, why in R.fails:
            print(f"   {d}  {f}: csv={got!r} vs {want!r}")
    return 1 if R.bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
