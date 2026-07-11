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
import sqlite3
from datetime import datetime, timedelta, timezone

from services.telegram_alert_service import telegram_alert_service
from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SANDBOX_DB = os.path.join(_APP_ROOT, "db", "sandbox.db")

# Index-futures NRML margin ≈ 10% of notional (approx; matches eod_summary.py).
FUT_MARGIN_PCT = 0.10


def _is_option(symbol):
    return symbol.endswith("CE") or symbol.endswith("PE")


def _rupees(x):
    return f"₹{abs(x):,.0f}"


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
    strategy = event.strategy or ""

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
    return "\n".join(lines), label


def on_sandbox_order_filled(event):
    """Send an enriched ENTRY/EXIT + margin alert for a sandbox fill."""
    try:
        message, label = _build_message(event)
        telegram_alert_service.send_broadcast_alert(message)
        logger.info(
            f"[telegram-fill] {label} alert sent: {event.strategy} "
            f"{event.action} {event.symbol} x{event.quantity}"
        )
    except Exception as e:
        # Never let an alert failure break the event bus / order execution.
        logger.debug(f"telegram fill alert failed: {e}")
