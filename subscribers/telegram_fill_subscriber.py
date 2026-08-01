"""Telegram alert on sandbox order FILL — enriched with ENTRY/EXIT + margin.

Hooks `sandbox.order_filled` (the sandbox execution engine's fill event, which
carries the actual execution price *and* the strategy tag) rather than
`order.placed`, because:
  - it fires when the trade actually happens (MARKET orders carry no price at
    placement time), and
  - it lets us label ENTRY vs EXIT from the per-strategy position transition and
    show the ₹ margin (futures) / premium (options) that trade involved.

In analyze (paper) mode the plain `order.placed` telegram alert is suppressed
(see telegram_subscriber.on_order_placed) so the user gets exactly one, richer
alert per fill. Live-mode alerts still flow through the plain path unchanged.
"""

import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from services.telegram_alert_service import telegram_alert_service
from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SANDBOX_DB = os.path.join(_APP_ROOT, "db", "sandbox.db")
# charges.py lives with the strategy scripts (not on the app import path).
_CHARGES_PY = os.path.join(_APP_ROOT, "strategies", "scripts", "charges.py")

# Index-futures NRML margin ≈ 10% of notional (approx; matches eod_summary.py).
FUT_MARGIN_PCT = 0.10

# Once-per-close dedup for the realized-P&L block: the straddle closes 4 legs via
# 4 near-simultaneous fill events — only the first to observe the position fully
# flat reports its P&L; siblings skip. Keyed (strategy, day, fills_count_today).
# Resets naturally each day via the 09:05 process restart.
_pnl_reported = set()
_pnl_lock = threading.Lock()
_charges_mod = None

# Strategies that emit their OWN trade alerts (mode-agnostic, from the strategy process) are
# skipped here — otherwise analyze mode would double-alert. This subscriber only ever fires in
# analyze mode (it hooks a sandbox event), so anything that must also work LIVE has to
# self-alert; the straddle does. Matched as a case-insensitive substring of the strategy tag.
SELF_ALERTING = ("straddle",)


def _is_option(symbol):
    return symbol.endswith("CE") or symbol.endswith("PE")


# Strike = the digits after the DDMMMYY expiry and before CE/PE in an option symbol.
_STRIKE_RE = re.compile(r"\d{2}[A-Z]{3}\d{2}(\d+)(?:CE|PE)$")


def _parse_strike(sym):
    m = _STRIKE_RE.search(sym)
    return int(m.group(1)) if m else None


def _ironfly_margin(entry_fills, credit):
    """Utilised margin for a hedged iron-fly ≈ its defined max loss:
    wing_width × leg_qty − net_credit. Mirrors eod_summary._ironfly_margin so the
    per-trade alert and the EOD digest report the SAME number. None if unparseable
    (e.g. a naked/unhedged structure, where a single leg's margin isn't meaningful).
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
        width = max(abs(buy["CE"] - sell["CE"]), abs(sell["PE"] - buy["PE"]))
        return width * sell["q"] - credit
    except (TypeError, KeyError, ValueError):
        return None


def _rupees(x):
    return f"₹{abs(x):,.0f}"


def _signed_rupees(x):
    return f"{'+' if x >= 0 else '−'}₹{abs(x):,.0f}"


def _charges():
    """Lazily load the shared Zerodha charges model from strategies/scripts/charges.py
    via an explicit file path (avoids polluting sys.path with the strategy dir)."""
    global _charges_mod
    if _charges_mod is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("straddle_charges", _CHARGES_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _charges_mod = mod
    return _charges_mod


# A manual "Close All" / broker auto-square-off fill loses the strategy tag — the
# sandbox stamps it AUTO_SQUARE_OFF (or blank). These get re-attributed to the
# strategy that opened the matching symbol that day (when that owner is unique).
_SQUAREOFF_TAGS = {"AUTO_SQUARE_OFF", "", None}


def _day_for(con, tradeid):
    r = con.execute(
        "SELECT trade_timestamp FROM sandbox_trades WHERE tradeid=?", (tradeid,)
    ).fetchone()
    return r[0][:10] if r else datetime.now(IST).strftime("%Y-%m-%d")


def _resolve_strategy(symbol, tradeid, raw):
    """If the fill carries a real strategy tag, keep it. If it's a square-off/untagged
    fill, re-attribute to the strategy that opened this symbol today — but only when
    exactly one strategy owns the symbol (shared symbols stay unresolved)."""
    if raw and raw not in _SQUAREOFF_TAGS:
        return raw
    con = sqlite3.connect(f"file:{_SANDBOX_DB}?mode=ro", uri=True)
    try:
        day = _day_for(con, tradeid)
        tags = con.execute(
            "SELECT DISTINCT strategy FROM sandbox_trades "
            "WHERE symbol=? AND substr(trade_timestamp,1,10)=? "
            "AND strategy IS NOT NULL AND strategy NOT IN ('AUTO_SQUARE_OFF','')",
            (symbol, day),
        ).fetchall()
    finally:
        con.close()
    owners = [t[0] for t in tags]
    return owners[0] if len(owners) == 1 else (raw or "")


def _strategy_pnl_today(strategy, tradeid):
    """(fully_flat, gross, charges, net, n_fills) for `strategy`'s position today, or None.

    Includes the strategy's own tagged fills PLUS square-off/untagged fills for symbols
    UNIQUELY owned by it that day — so a manual Close-All / broker auto-square-off (which
    loses the tag) is still attributed. Shared symbols (BANKNIFTY-fut, used by both EMA
    strategies) stay tag-only to avoid mis-splitting an ambiguous square-off.
    """
    con = sqlite3.connect(f"file:{_SANDBOX_DB}?mode=ro", uri=True)
    try:
        day = _day_for(con, tradeid)
        rows = con.execute(
            "SELECT symbol, action, quantity, CAST(price AS FLOAT), strategy "
            "FROM sandbox_trades WHERE substr(trade_timestamp,1,10)=? "
            "ORDER BY trade_timestamp",
            (day,),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return None
    owners = {}
    for sym, _a, _q, _p, tag in rows:
        if tag and tag not in _SQUAREOFF_TAGS:
            owners.setdefault(sym, set()).add(tag)
    unique_syms = {s for s, o in owners.items() if o == {strategy}}

    by_sym = {}
    fills = []
    for sym, action, qty, price, tag in rows:
        if not (tag == strategy or (tag in _SQUAREOFF_TAGS and sym in unique_syms)):
            continue
        act = (action or "").upper()
        d = by_sym.setdefault(sym, {"net": 0, "bv": 0.0, "sv": 0.0})
        if act == "BUY":
            d["net"] += qty
            d["bv"] += qty * price
        else:
            d["net"] -= qty
            d["sv"] += qty * price
        fills.append({"action": act, "quantity": qty, "price": price, "symbol": sym})
    if not fills:
        return None
    fully_flat = all(v["net"] == 0 for v in by_sym.values())
    gross = sum(v["sv"] - v["bv"] for v in by_sym.values())
    is_opt = any(_is_option(s) for s in by_sym)
    try:
        charges = _charges().charges_from_fills(fills, is_opt)
    except Exception:
        charges = 0.0

    # Consolidated utilised margin for the whole structure. Options (iron-fly) =
    # defined max loss from the ENTRY legs (fills are chronological, so the opening
    # legs are the first half of a flat round-trip). Futures = margin on the entry
    # notional. Same basis as the EOD digest.
    margin = None
    entry = fills[: len(fills) // 2] if fully_flat and len(fills) >= 2 else []
    if is_opt:
        if entry:
            credit = sum(f["quantity"] * f["price"] for f in entry if f["action"] == "SELL") - sum(
                f["quantity"] * f["price"] for f in entry if f["action"] == "BUY"
            )
            margin = _ironfly_margin(entry, credit)
    elif entry:
        margin = sum(f["quantity"] * f["price"] for f in entry) * FUT_MARGIN_PCT

    return fully_flat, gross, charges, gross - charges, len(fills), margin


def _position_before(strategy, symbol, tradeid):
    """Net signed qty for (strategy, symbol) from fills STRICTLY BEFORE this one
    on the same day — i.e. the position that existed just before this trade.

    Keyed off the current fill's own timestamp (looked up by tradeid), not just
    its tradeid: in production later fills don't exist yet, but this also stays
    correct if the whole day's fills are present (replay / reprocessing).
    """
    con = sqlite3.connect(f"file:{_SANDBOX_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT trade_timestamp FROM sandbox_trades WHERE tradeid=?",
            (tradeid,),
        ).fetchone()
        if row:
            cur_ts = row[0]
            rows = con.execute(
                "SELECT action, quantity FROM sandbox_trades "
                "WHERE strategy=? AND symbol=? AND substr(trade_timestamp,1,10)=? "
                "AND trade_timestamp < ?",
                (strategy, symbol, cur_ts[:10], cur_ts),
            ).fetchall()
        else:
            # Current fill not yet persisted — everything today is "before".
            day = datetime.now(IST).strftime("%Y-%m-%d")
            rows = con.execute(
                "SELECT action, quantity FROM sandbox_trades "
                "WHERE strategy=? AND symbol=? AND substr(trade_timestamp,1,10)=?",
                (strategy, symbol, day),
            ).fetchall()
    finally:
        con.close()
    return sum(q if a.upper() == "BUY" else -q for a, q in rows)


def _build_message(event):
    symbol = event.symbol
    action = (event.action or "").upper()
    qty = int(event.quantity)
    price = float(event.price or 0)
    # Re-attribute square-off/untagged fills to the owning strategy (manual Close-All etc.).
    strategy = _resolve_strategy(symbol, event.tradeid, event.strategy)

    # ENTRY vs EXIT from the position transition (exposure grows = entry).
    try:
        before = _position_before(strategy, symbol, event.tradeid)
    except Exception:
        before = None
    signed = qty if action == "BUY" else -qty
    if before is None:
        label, emoji = "TRADE", "🔵"
    elif abs(before + signed) >= abs(before):
        label, emoji = "ENTRY", "🟢"
    else:
        label, emoji = "EXIT", "🔴"

    # Capital line: futures -> margin; option leg -> premium cashflow.
    value = qty * price
    if _is_option(symbol):
        cap = (
            f"Premium received: {_rupees(value)}"
            if action == "SELL"
            else f"Premium paid: {_rupees(value)}"
        )
    else:
        margin = value * FUT_MARGIN_PCT
        verb = {"ENTRY": "blocked", "EXIT": "freed"}.get(label, "used")
        cap = f"Margin {verb}: ~{_rupees(margin)}"

    ts = datetime.now(IST).strftime("%H:%M:%S")
    lines = [f"📊 *Trade Filled* — {emoji} *{label}*"]
    if strategy:
        lines.append(f"Strategy: *{strategy}*")
    lines += [
        "🔬 ANALYZE MODE",
        "─────────────────────",
        f"Symbol: `{symbol}`",
        f"Action: {action}",
        f"Quantity: {qty}",
        f"Fill Price: ₹{price:,.2f}",
        cap,
        f"Order ID: `{event.orderid}`",
        f"⏰ Time: {ts}",
    ]
    return "\n".join(lines), label, strategy


def on_sandbox_order_filled(event):
    """Send an enriched ENTRY/EXIT + margin alert for a sandbox fill."""
    try:
        message, label, strategy = _build_message(event)
        if any(s in (strategy or "").lower() for s in SELF_ALERTING):
            logger.debug(f"[telegram-fill] skipped (self-alerting strategy): {strategy}")
            return
        # On the fill that fully closes the position, append realized P&L
        # (gross / Zerodha charges / net). EMA: every exit closes it. Straddle:
        # only the leg that observes all legs flat, reported once (dedup).
        # `strategy` is the resolved owner (square-off fills re-attributed).
        if label == "EXIT":
            try:
                pnl = _strategy_pnl_today(strategy, event.tradeid)
            except Exception:
                pnl = None
            if pnl and pnl[0]:  # fully_flat
                _, gross, charges, net, n_fills, margin = pnl
                key = (strategy, datetime.now(IST).strftime("%Y-%m-%d"), n_fills)
                with _pnl_lock:
                    first = key not in _pnl_reported
                    if first:
                        _pnl_reported.add(key)
                if first:
                    legs_txt = f" ({n_fills // 2} legs)" if n_fills > 2 else ""
                    message += f"\n──── Position closed{legs_txt} · realized P&L ────"
                    if margin:
                        # consolidated across all legs of the structure
                        message += f"\nUtilised margin: ~{_rupees(margin)}"
                    message += (
                        f"\nGross: {_signed_rupees(gross)}"
                        f"\nCharges: −{_rupees(charges)}  (Zerodha)"
                        f"\nNet: *{_signed_rupees(net)}*"
                    )
                    if margin:
                        message += f"\nReturn on margin: {net / margin * 100:+.1f}%"
        telegram_alert_service.send_broadcast_alert(message)
        logger.info(
            f"[telegram-fill] {label} alert sent: {strategy} "
            f"{event.action} {event.symbol} x{event.quantity}"
        )
    except Exception as e:
        # Never let an alert failure break the event bus / order execution.
        logger.debug(f"telegram fill alert failed: {e}")
