#!/usr/bin/env python3
"""test_analytics_xlsx.py — recalculate log/trade_analytics.xlsx and check every headline figure.

WHY A RECALCULATION TEST AND NOT AN INSPECTION. openpyxl writes formulas as strings; it does
not evaluate them. So a workbook can be structurally perfect and arithmetically wrong, and
reading the file back tells you nothing. Two real defects were caught only by evaluating:

  * straddle-income-growth-analysis.xlsx once had ~600 cells missing the leading '=' — they
    would have opened in Excel as literal TEXT. Structure looked fine.
  * this workbook's Projection taxes MAX(0, profit); an earlier version of THIS check applied
    the rate unconditionally and so disagreed on 2026-08-24, our first negative projection
    month. The workbook was right and the check was wrong — which is exactly why the check
    now lives in the repo instead of a scratchpad, where that fix would have been lost.

Every expectation is recomputed from log/trade_journal.csv independently of the workbook, so
this is a genuine cross-check rather than the sheet agreeing with itself.

Requires the `formulas` package (heavy, and not a runtime dependency of anything else):
    python -m venv .venv-xl && .venv-xl/bin/pip install formulas openpyxl
    .venv-xl/bin/python strategies/scripts/test_analytics_xlsx.py

If `formulas` is absent the run SKIPS — loudly, and saying the analytics were not verified.
An unverified workbook must never look verified.
"""
import csv
import math
import os
import shutil
import statistics as st
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
JOURNAL = os.getenv("TRADE_JOURNAL_CSV", os.path.join(REPO, "log", "trade_journal.csv"))
XLSX = os.getenv("ANALYTICS_XLSX", os.path.join(REPO, "log", "trade_analytics.xlsx"))
TAX_RATE = 0.3120          # must match the Projection's default "Income tax" input
OPENING_CASH_FALLBACK = 0.0

PASS, FAIL = [], []


def ck(name, got, want, tol=1e-6):
    try:
        ok = abs(float(got) - float(want)) <= tol
    except (TypeError, ValueError):
        ok = str(got) == str(want)
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name:<34} sheet={got}  expected={want}")


def opening_cash():
    v = os.getenv("OPENING_CASH")
    if not v:
        f = os.path.join(REPO, "log", "opening_cash.txt")
        if os.path.exists(f):
            v = open(f).read().strip()
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return OPENING_CASH_FALLBACK


def main():
    try:
        import formulas
    except ImportError:
        print("\n  " + "=" * 66)
        print("  SKIPPED — `formulas` is not installed, so trade_analytics.xlsx was")
        print("  NOT VERIFIED. Its numbers are unchecked, not confirmed.")
        print("    python -m venv .venv-xl && .venv-xl/bin/pip install formulas openpyxl")
        print("  " + "=" * 66 + "\n")
        return 3

    if not (os.path.exists(JOURNAL) and os.path.exists(XLSX)):
        print(f"  missing input: {JOURNAL if not os.path.exists(JOURNAL) else XLSX}")
        return 1

    rows = list(csv.DictReader(open(JOURNAL)))
    net = sum(float(r["net_pnl"] or 0) for r in rows)
    gross = sum(float(r["gross_pnl"] or 0) for r in rows)
    mgs = [float(r["margin_blocked"]) for r in rows if float(r["margin_blocked"] or 0)]
    roms = [float(r["roi_on_margin_pct"]) for r in rows if r["roi_on_margin_pct"] not in ("", None)]
    lots = st.mean([float(r["lots"]) for r in rows])
    avg_margin = st.mean(mgs)
    lotv = avg_margin / lots
    roi = net / avg_margin / len(rows) * 20          # naive monthly run-rate
    corpus = opening_cash() + net

    # copy beside the journal so the engine's relative refs resolve the same way
    tmp = os.path.join(tempfile.mkdtemp(), "ta.xlsx")
    shutil.copy(XLSX, tmp)
    sol = formulas.ExcelModel().loads(tmp).finish().calculate()
    base = f"'[{os.path.basename(tmp)}]"

    def cell(sheet, ref):
        v = sol[f"{base}{sheet.upper()}'!{ref}"].value
        try:
            return v[0, 0]
        except Exception:
            return v

    print(f"\n  === recalculating {os.path.basename(XLSX)} · {len(rows)} journal rows ===\n")
    print("  --- Live Status vs independently computed CSV values ---")
    ck("traded days", cell("Live Status", "B5"), len(rows))
    ck("gross total", cell("Live Status", "B6"), gross, 0.01)
    ck("NET total", cell("Live Status", "B8"), net, 0.01)
    ck("avg margin", cell("Live Status", "B14"), avg_margin, 0.01)
    ck("margin per lot", cell("Live Status", "B16"), lotv, 0.01)
    ck("period ROM", cell("Live Status", "B17"), net / avg_margin)
    ck("mean daily ROM", cell("Live Status", "B18"), st.mean(roms) / 100)
    ck("median daily ROM", cell("Live Status", "B19"), st.median(roms) / 100)
    ck("naive monthly", cell("Live Status", "B21"), roi)
    ck("win rate", cell("Live Status", "B25"),
       sum(1 for r in rows if float(r["net_pnl"] or 0) > 0) / len(rows))
    ck("stdev daily", cell("Live Status", "B33"), st.stdev(roms) / 100)
    ck("t-statistic", cell("Live Status", "B35"),
       st.mean(roms) / (st.stdev(roms) / math.sqrt(len(roms))), 1e-3)
    top2 = sorted(float(r["net_pnl"] or 0) for r in rows)[-2:]
    ck("top-2 concentration", cell("Live Status", "B39"), sum(top2) / net)

    print("\n  --- Monthly + equity curve ---")
    ck("month net", cell("Monthly", "F5"), net, 0.01)
    ck("month days", cell("Monthly", "B5"), len(rows))
    ck("final cumulative", cell("Chart · Equity", f"C{4 + len(rows)}"), net, 0.01)

    print("\n  --- Projection, month 1 ---")
    n_lots = int(corpus / lotv)
    deployed = n_lots * lotv
    profit = deployed * roi
    # The sheet taxes MAX(0, profit): a LOSS month carries NO tax credit. Applying the rate
    # unconditionally here is the bug this file exists to stop recurring (2026-08-24).
    closing = corpus + profit - max(0.0, profit) * TAX_RATE
    ck("corpus = open + SUM(net)", cell("Projection", "B5"), corpus, 0.01)
    ck("margin/lot anchor", cell("Projection", "B6"), lotv, 1.0)
    ck("monthly ROI anchor", cell("Projection", "B7"), roi)
    ck("m1 lots", cell("Projection", "E19"), n_lots)
    ck("m1 deployed", cell("Projection", "F19"), deployed, 0.01)
    ck("m1 net profit", cell("Projection", "G19"), profit, 0.01)
    ck("m1 income tax (0 on a loss)", cell("Projection", "H19"),
       max(0.0, profit) * TAX_RATE, 0.01)
    ck("m1 closing", cell("Projection", "J19"), closing, 0.01)

    print(f"\n  ════ {len(PASS)} passed · {len(FAIL)} FAILED ════")
    if FAIL:
        print("  FAILED:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
