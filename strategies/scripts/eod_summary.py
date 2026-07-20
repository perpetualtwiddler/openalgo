#!/usr/bin/env python3
"""eod_summary.py — End-of-day per-strategy P&L digest, pushed to Telegram (TradeBhau).

Source of truth = the Sandbox (paper) engine's executed fills (`sandbox_trades`),
which carry a per-strategy tag. This is the ONLY source that separates strategies
that share a symbol (Opt1 and Regime both trade BANKNIFTY futures — position-level
P&L would merge them; the trade-level `strategy` tag does not).

Per strategy we net fills per symbol into a flat round-trip P&L, apply the exact
Zerodha rate card (charges.py:charges_from_fills), and report:
    capital committed | gross P&L | charges | net P&L
plus an aggregate TOTAL row.

Usage:
    python eod_summary.py                 # today (IST), DRY-RUN (prints, no send)
    python eod_summary.py 2026-07-10      # a specific date, dry-run
    python eod_summary.py --send          # today, and BROADCAST to linked Telegram users
    python eod_summary.py 2026-07-10 --send
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
# Script dir for `charges`; repo root so `services.*` imports work under any cwd
# (the scheduled job runs from an arbitrary directory).
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)
from charges import charges_from_fills  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = os.path.join(REPO_ROOT, "db", "sandbox.db")

# Index-futures NRML margin ≈ 10% of notional (SPAN+exposure, approx). Paper
# trading runs against ₹1 Cr sandbox capital so this is only a glance figure.
FUT_MARGIN_PCT = 0.10

# Strike = the digits after the DDMMMYY expiry and before CE/PE in an option symbol.
_STRIKE_RE = re.compile(r"\d{2}[A-Z]{3}\d{2}(\d+)(?:CE|PE)$")

# Map the sandbox strategy tag -> (short display label, sort order).
# Unknown tags fall through with their raw name at the end.
LABELS = {
    "EMA 9/21 Crossover - BankNifty 3min (Opt1)": ("Opt1 · EMA 9/21 3m BANKNIFTY-fut", 1),
    "EMA Regime Follower - BankNifty 3min": ("Regime · EMA regime BANKNIFTY-fut", 2),
    "9:20 AM Short Straddle - Nifty ATM": ("Straddle · NIFTY iron-fly", 3),
}


def _rupees(x, signed=True):
    """₹ with thousands separators; +/- sign for P&L, plain for magnitudes."""
    if signed:
        sign = "+" if x >= 0 else "−"  # real minus sign
        return f"{sign}₹{abs(x):,.0f}"
    return f"₹{abs(x):,.0f}"


def is_option_symbol(sym):
    return sym.endswith("CE") or sym.endswith("PE")


def _parse_strike(sym):
    m = _STRIKE_RE.search(sym)
    return int(m.group(1)) if m else None


def _peak_qty(fills):
    """Peak absolute concurrent position from an ordered fill list.

    Uses running net position (not cumulative turnover), so N sequential
    round-trips in a day report one position's size, not N×.
    """
    run = pk = 0
    for f in fills:
        run += f["quantity"] if f["action"] == "BUY" else -f["quantity"]
        pk = max(pk, abs(run))
    return pk


def _ironfly_margin(entry_fills, credit):
    """Defined-risk margin ≈ max loss = wing_width × leg_qty − net_credit.

    entry_fills = the opening legs (short ATM CE/PE sold, long OTM CE/PE bought).
    Returns None if strikes can't be parsed (falls back to 'n/a' in display).
    """
    try:
        sell = {"CE": None, "PE": None, "q": 0}
        buy = {"CE": None, "PE": None}
        for f in entry_fills:
            side = "CE" if f["symbol"].endswith("CE") else "PE"
            k = _parse_strike(f["symbol"])
            if f["action"] == "SELL":
                sell[side] = k
                sell["q"] = max(sell["q"], f["quantity"])
            else:
                buy[side] = k
        ce_w = abs(buy["CE"] - sell["CE"])
        pe_w = abs(sell["PE"] - buy["PE"])
        width = max(ce_w, pe_w)
        return width * sell["q"] - credit
    except (TypeError, KeyError):
        return None


def load_fills(date_str):
    import sqlite3

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT strategy, symbol, exchange, action, quantity, "
        "CAST(price AS FLOAT) AS price, trade_timestamp "
        "FROM sandbox_trades WHERE substr(trade_timestamp,1,10)=? "
        "ORDER BY trade_timestamp",
        (date_str,),
    ).fetchall()
    con.close()
    return rows


def compute(fills):
    """Return per-strategy result dicts."""
    # A manual Close-All / broker auto-square-off fill loses its strategy tag (stamped
    # AUTO_SQUARE_OFF / blank). Re-attribute it to the strategy that opened that symbol
    # today, when the owner is unique — else leave it as its own line (honest for the
    # shared BANKNIFTY-fut case where it can't be split between Opt1/Regime).
    squareoff = {"AUTO_SQUARE_OFF", "", None}
    owners = {}
    for r in fills:
        tag = r["strategy"]
        if tag and tag not in squareoff:
            owners.setdefault(r["symbol"], set()).add(tag)

    def _owner(r):
        tag = r["strategy"]
        if tag and tag not in squareoff:
            return tag
        o = owners.get(r["symbol"], set())
        return next(iter(o)) if len(o) == 1 else (tag or "AUTO_SQUARE_OFF")

    strat = {}
    for r in fills:
        strat.setdefault(_owner(r), []).append(
            {
                "action": r["action"].upper(),
                "quantity": int(r["quantity"]),
                "price": float(r["price"]),
                "symbol": r["symbol"],
            }
        )

    results = []
    for name, fl in strat.items():
        opts = any(is_option_symbol(f["symbol"]) for f in fl)

        # Net per symbol -> flat round-trip realized P&L (direction-agnostic),
        # keeping each symbol's fills in order for peak-position math.
        by_sym = {}
        for f in fl:
            d = by_sym.setdefault(
                f["symbol"], {"fills": [], "bq": 0, "bv": 0.0, "sq": 0, "sv": 0.0}
            )
            d["fills"].append(f)
            if f["action"] == "BUY":
                d["bq"] += f["quantity"]
                d["bv"] += f["quantity"] * f["price"]
            else:
                d["sq"] += f["quantity"]
                d["sv"] += f["quantity"] * f["price"]

        gross = sum(d["sv"] - d["bv"] for d in by_sym.values())
        open_pos = [(s, d["bq"] - d["sq"]) for s, d in by_sym.items() if d["bq"] != d["sq"]]

        charges = charges_from_fills(fl, opts)
        net = gross - charges

        premium = None
        if opts:
            # Net credit taken at entry (first half of fills, chronological).
            entry = fl[: len(fl) // 2]
            premium = sum(
                f["quantity"] * f["price"] for f in entry if f["action"] == "SELL"
            ) - sum(f["quantity"] * f["price"] for f in entry if f["action"] == "BUY")
            margin = _ironfly_margin(entry, premium)
        else:
            # Peak-position notional × margin %, summed across symbols.
            margin = 0.0
            for d in by_sym.values():
                pk = _peak_qty(d["fills"])
                denom = d["bq"] + d["sq"]
                avg_price = (d["bv"] + d["sv"]) / denom if denom else 0.0
                margin += pk * avg_price * FUT_MARGIN_PCT

        label, order = LABELS.get(name, (name, 99))
        results.append(
            {
                "label": label,
                "order": order,
                "opts": opts,
                "gross": gross,
                "charges": charges,
                "net": net,
                "margin": margin,
                "premium": premium,
                "nfills": len(fl),
                "open_pos": open_pos,
            }
        )

    results.sort(key=lambda r: r["order"])
    return results


def format_message(date_str, results):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    header = d.strftime("%a %d %b %Y")
    lines = ["\U0001f4ca *TradeBhau — EOD Summary*", f"_{header}_", ""]

    if not results:
        lines.append("No strategy trades today.")
        return "\n".join(lines)

    tot_gross = tot_charges = tot_net = tot_margin = 0.0
    for r in results:
        lines.append(f"*{r['label']}*")
        margin_txt = f"~{_rupees(r['margin'], signed=False)}" if r["margin"] else "n/a"
        lines.append(f"Margin blocked: {margin_txt}  ({r['nfills']} fills)")
        if r["premium"] is not None:
            lines.append(f"Premium collected: {_rupees(r['premium'], signed=False)}")
        lines.append(f"Gross: {_rupees(r['gross'])}")
        lines.append(f"Charges: −{_rupees(r['charges'], signed=False)}")
        lines.append(f"Net: *{_rupees(r['net'])}*")
        if r["open_pos"]:
            carried = ", ".join(f"{s} {q:+d}" for s, q in r["open_pos"])
            lines.append(f"⚠️ open carry (P&L excl.): {carried}")
        lines.append("")
        tot_gross += r["gross"]
        tot_charges += r["charges"]
        tot_net += r["net"]
        tot_margin += r["margin"] or 0.0

    lines.append("━" * 13)
    lines.append("*TOTAL*")
    lines.append(f"Capital deployed: ~{_rupees(tot_margin, signed=False)}")
    lines.append(f"Gross: {_rupees(tot_gross)}")
    lines.append(f"Charges: −{_rupees(tot_charges, signed=False)}")
    lines.append(f"Net: *{_rupees(tot_net)}*")
    return "\n".join(lines)


def main(argv):
    send = "--send" in argv
    dates = [a for a in argv[1:] if not a.startswith("--")]
    date_str = dates[0] if dates else datetime.now(IST).strftime("%Y-%m-%d")

    fills = load_fills(date_str)
    results = compute(fills)
    message = format_message(date_str, results)

    print(message)
    print()

    if send:
        if not results:
            # Market holiday / no strategy traded — don't spam an empty digest.
            print(f"[skip] no trades on {date_str}; nothing broadcast")
            return
        from services.telegram_alert_service import telegram_alert_service

        telegram_alert_service.send_broadcast_alert(message)
        print(f"[sent] broadcast to linked Telegram users for {date_str}")
    else:
        print(f"[dry-run] no message sent (add --send to broadcast) for {date_str}")


if __name__ == "__main__":
    main(sys.argv)
