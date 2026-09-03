#!/usr/bin/env python3
"""archive_tradebook.py — snapshot today's broker tradebook to disk before it vanishes.

The Zerodha tradebook API returns the CURRENT DAY ONLY. Once the date rolls over, the
fill-level truth for that session is gone permanently — no API call can recover it. That
is not a theoretical loss: 2026-08-06 was closed by hand in the Zerodha terminal, our
strategy log therefore recorded no [EXIT] fills, and by the time we needed them the
tradebook had rolled. That day is still the one `low` confidence row in trade_journal.csv,
its P&L reconstructed from a number read off a screen rather than from fills.

Running this daily makes every day fill-verifiable regardless of HOW it was closed —
strategy square-off, breach exit, or a manual click. trade_journal.py reads these archives
as its exit-price fallback (see _legs_from_tradebook).

Stores the raw broker payload, unmodified, plus two counts. `n_orders` is the number of
distinct order ids among the FILLS — brokerage is billed per ORDER, not per fill, and a leg
can partial-fill (2026-08-14: 11 fills / 8 orders; billing the fills would have overstated
charges by Rs70.80). `n_orderbook_rows` is the orderbook's own row count, kept separately
because a divergence flags rows that never filled or a session closed by hand.

Usage:
    python archive_tradebook.py            # today -> log/tradebook/YYYY-MM-DD.json
    python archive_tradebook.py --print    # also print a per-leg summary

Idempotent: re-running the same day overwrites with a fresh snapshot. Safe to schedule
alongside eod_summary; a day with no trades writes a file with an empty trades list, which
is itself useful evidence that nothing filled.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = os.getenv("TRADEBOOK_DIR", os.path.join(REPO_ROOT, "log", "tradebook"))


def main(argv):
    import openalgo
    from database.auth_db import get_api_key_for_tradingview

    date = datetime.now(IST).strftime("%Y-%m-%d")
    client = openalgo.api(api_key=get_api_key_for_tradingview("admin"),
                          host="http://127.0.0.1:5000")

    trades = client.tradebook().get("data") or []
    ob = client.orderbook().get("data") or {}
    orders = (ob.get("orders") if isinstance(ob, dict) else ob) or []

    # Brokerage is billed per ORDER, so the count that matters is the number of distinct
    # order ids AMONG THE FILLS — not the orderbook's row count. The two diverge whenever
    # the orderbook holds rows that never filled (rejects, cancels) or that the strategy
    # did not place: on a day closed by hand in the Zerodha app the manual exits appear as
    # extra rows, which is why 2026-08-14 and 2026-09-03 were archived as 10 orders when
    # only 8 were billable. charges._group_orders() groups by orderid for this same reason,
    # so P&L was never affected — but this file is the only surviving fill-level record,
    # and a reader taking n_orders at face value would over-bill brokerage by Rs20/order.
    billable = {t.get("orderid") or t.get("order_id") for t in trades}
    billable.discard(None)
    billable.discard("")
    # If the payload carries no order ids at all, fall back to one-order-per-fill — the same
    # convention charges._group_orders() uses, so the two never disagree. Writing 0 here
    # would silently zero the brokerage a future reader computes from this file.
    n_billable = len(billable) if billable else len(trades)

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{date}.json")
    payload = {
        "date": date,
        "captured_at": datetime.now(IST).isoformat(),
        "n_orders": n_billable,
        "n_fills": len(trades),
        # Kept because a divergence from n_orders is a useful tell: unfilled/rejected rows,
        # or a session closed manually rather than by the strategy.
        "n_orderbook_rows": len(orders),
        "trades": trades,
    }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"[ok] {date}: archived {len(trades)} fills / {n_billable} billable orders "
          f"({len(orders)} orderbook rows) -> {path}")

    if "--print" in argv:
        legs = {}
        for t in trades:
            sym = t.get("symbol", "?")
            sign = -1 if (t.get("action") or "").upper() == "BUY" else 1
            qty = abs(int(float(t.get("quantity") or 0)))
            px = float(t.get("average_price") or 0)
            legs[sym] = legs.get(sym, 0.0) + sign * qty * px
        for sym, v in sorted(legs.items()):
            print(f"   {sym:<24} {v:>+12,.2f}")
        if legs:
            print(f"   {'GROSS':<24} {sum(legs.values()):>+12,.2f}")


if __name__ == "__main__":
    main(sys.argv)
