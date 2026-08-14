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
as its exit-price fallback (see _exit_from_tradebook).

Stores the raw broker payload, unmodified, plus the order count — because brokerage is
billed per ORDER, not per fill, and a leg can partial-fill (2026-08-14: 11 fills / 8
orders; billing the fills would have overstated charges by Rs70.80).

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

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{date}.json")
    payload = {
        "date": date,
        "captured_at": datetime.now(IST).isoformat(),
        "n_orders": len(orders),
        "n_fills": len(trades),
        "trades": trades,
    }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"[ok] {date}: archived {len(trades)} fills / {len(orders)} orders -> {path}")

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
