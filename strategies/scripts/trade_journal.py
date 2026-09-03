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
    python trade_journal.py --stats            # post-market performance read-out

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
    "tg_target_net", "tg_stop_net", "tg_armed_at", "tg_fired",
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


def _legs_from_tradebook(date, short_syms):
    """Volume-weighted entry AND exit prices per leg from the archived broker tradebook.

    PREFERRED over log-parsed prices whenever an archive exists. The strategy log records ONE
    price per leg, but a leg can fill in several tranches at DIFFERENT prices — on 2026-08-18
    the 24250 PE bought back as 65 @ 127.35 + 65 @ 127.40 (VWAP 127.375) while the log said
    127.35, understating charges-inclusive gross by Rs3.25. The broker fills are the truth.

    Opening side is inferred from the leg's role: ATM shorts open on SELL and close on BUY;
    the wings open on BUY and close on SELL.

    Also returns the timestamp of the LAST closing fill. A manual close writes no [EXIT]
    lines, so the log-parsing path leaves exit_time blank on exactly the days #21 needs it
    most — those are the "reviewed the ladder and chose" baseline. The fills carry the real
    time (2026-09-03: 15:08:38-15:08:50), so take it from here when the log has none.

    Returns (entry_prices, exit_prices, n_orders, n_fills, exit_time) — empty if no archive.
    """
    p = os.path.join(TRADEBOOK_DIR, f"{date}.json")
    if not os.path.exists(p):
        return {}, {}, 0, 0, ""
    tb = json.load(open(p)).get("trades") or []
    if not tb:
        return {}, {}, 0, 0, ""
    acc = {}
    oids = set()
    out_ts = []
    for t in tb:
        sym = t.get("symbol") or ""
        act = (t.get("action") or "").upper()
        qty = abs(int(float(t.get("quantity") or 0)))
        px = float(t.get("average_price") or t.get("price") or 0)
        if t.get("orderid"):
            oids.add(str(t["orderid"]))
        opening = (act == "SELL") if sym in short_syms else (act == "BUY")
        if not opening:
            ts = str(t.get("timestamp") or t.get("fill_timestamp") or "")
            # Guard the format rather than trust it: a max() over mixed or malformed
            # strings would silently pick a nonsense "latest".
            if re.fullmatch(r"\d{2}:\d{2}:\d{2}", ts):
                out_ts.append(ts)
        d = acc.setdefault((sym, "in" if opening else "out"), [0, 0.0])
        d[0] += qty
        d[1] += qty * px
    ent = {s: round(v / q, 3) for (s, side), (q, v) in acc.items() if side == "in" and q}
    ex = {s: round(v / q, 3) for (s, side), (q, v) in acc.items() if side == "out" and q}
    return ent, ex, len(oids), len(tb), (max(out_ts) if out_ts else "")


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
    # CONCATENATE every log for the day, oldest first -- do NOT just take paths[-1].
    # One trading day can produce several logs: any openalgo restart while the strategy is
    # inside its schedule window makes APScheduler respawn the subprocess, which opens a
    # fresh timestamped file. Found 2026-08-20, when a 15:13 restart (schedule_stop 15:20)
    # created a 1.7KB log alongside the real 563KB session log; taking the newest found no
    # [ENTRY] and the day was silently skipped with exit status 0 -- a hole in the durable
    # ledger, from a service restart. Unattended-upgrades has restarted openalgo mid-market
    # before, so this is not a one-off. Joined in file order, which is chronological because
    # the names carry HHMMSS; the regexes below all take the FIRST entry / LAST exit, so an
    # empty tail segment is harmless and a genuine mid-day restart still parses.
    chunks = []
    for pth in paths:
        with open(pth, "rb") as fh:
            chunks.append(fh.read().decode("utf-8", "replace").replace("\r", "\n"))
    raw = "\n".join(chunks)
    if len(paths) > 1:
        sizes = ", ".join(f"{os.path.basename(x).split('_')[-2]}:{os.path.getsize(x)}B"
                          for x in paths)
        print(f"  [note] {date} has {len(paths)} strategy logs, merged ({sizes})")

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
    # `\d+(?:\.\d+)?` parses BOTH the pre-2026-08-25 integer logs and the 2dp ones that
    # replaced them. Stored as 2dp FLOATS, not ints: these three are one quantity split in
    # two, and truncating each independently broke `premium = gross - hedge` by Rs1 on
    # 2026-08-25 -- validate_journal.py was right to call it. int() would also raise on a
    # decimal string, so the cast has to change with the format.
    m = re.search(r"Gross premium: (\d+(?:\.\d+)?) \| Hedge cost: (\d+(?:\.\d+)?)", raw)
    if m:
        r["gross_premium"] = round(float(m.group(1)), 2)
        r["hedge_cost"] = round(float(m.group(2)), 2)
    m = re.search(r"Net premium collected: (\d+(?:\.\d+)?)", raw)
    if m:
        r["premium_collected"] = round(float(m.group(1)), 2)

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
    # `or ""` would turn a genuinely ZERO slippage into a blank, and blank means "unknown"
    # in this CSV, not "zero". 2026-08-18 filled with exactly Rs0 entry slippage — that is a
    # result worth recording, not a gap. Only write blank when there are no rows at all.
    _se = [s for s in slip if s["phase"] == "ENTRY"]
    _sx = [s for s in slip if s["phase"] == "EXIT"]
    r["slip_entry"] = round(sum(float(s["slip_rupees"]) for s in _se), 2) if _se else ""
    r["slip_exit"] = round(sum(float(s["slip_rupees"]) for s in _sx), 2) if _sx else ""

    ex = {}
    for lbl, sym, px in re.findall(
            r"\[EXIT\] (CE|PE|HEDGE CE|HEDGE PE) closed: (\S+) (?:BUY|SELL) \d+ @ ([\d.]+)", raw):
        ex[sym] = float(px)
    m = re.search(r"\[EXIT\] Closing iron butterfly — reason: ([A-Z_]+)", raw)
    if m:
        r["exit_reason"] = m.group(1)

    # ---- /stradexit: what was armed from Telegram, and did it fire?
    # Blank on days before the feature existed, and on days nothing was armed — that is
    # meaningful (no discretionary call made), not missing data.
    # LONGEST ALTERNATIVE FIRST. Python re is first-match-wins, not longest-match, so with the
    # bare "take-profit …" branch listed ahead of the combined "take-profit … · stop …" branch
    # it matched the PREFIX and silently truncated the stop away — tg_stop_net came out blank on
    # every day both sides were armed (found 2026-08-27, when the log plainly read
    # "take-profit at NET +Rs900 · stop at NET -1,500" and the journal recorded no stop).
    arms = re.findall(r"([\d:]{8})\s+\[STRADEXIT\] "
                      r"(take-profit at NET \+Rs[\d,]+ · stop at NET [-+][\d,]+"
                      r"|take-profit at NET \+Rs[\d,]+"
                      r"|stop at NET [-+][\d,]+"
                      r"|DISARMED[^\n]*)", raw)
    if arms:
        r["tg_armed_at"] = arms[0][0]
        last = arms[-1][1]
        mt = re.search(r"take-profit at NET \+Rs([\d,]+)", last)
        ms = re.search(r"stop at NET ([-+][\d,]+)", last)
        r["tg_target_net"] = mt.group(1).replace(",", "") if mt else ""
        r["tg_stop_net"] = ms.group(1).replace(",", "") if ms else ""
    r["tg_fired"] = "Y" if re.search(r"\[STRADEXIT\] net [-+][\d,]+ [<>]= armed", raw) else (
        "N" if arms else "")
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

    # PREFER the archived broker tradebook for leg prices: it is volume-weighted across
    # partial fills, whereas the log records a single price per leg. Only fall back to the
    # log/slippage values when no archive exists (days before archive_tradebook.py).
    tb_orders = tb_fills = 0
    shorts = {s for k, s in pairs[:2] if s}
    tb_ent, tb_ex, tb_orders, tb_fills, tb_exit_at = _legs_from_tradebook(date, shorts)
    if tb_ex:
        manual = not ex                       # no [EXIT] lines => closed outside our code
        if tb_ent:
            ent = {**ent, **tb_ent}
        ex = {**ex, **tb_ex}
        if not r.get("exit_time") and tb_exit_at:
            r["exit_time"] = tb_exit_at       # manual close: the fills are the only record
        if manual:
            r["exit_reason"] = r["exit_reason"] or "MANUAL_ZERODHA"
            r["notes"] = ("exit fills recovered from the archived broker tradebook "
                          "(no [EXIT] lines — closed manually); "
                          f"{tb_orders} orders / {tb_fills} fills")
        elif tb_fills != tb_orders:
            r["notes"] = (f"leg prices volume-weighted from the tradebook "
                          f"({tb_fills} fills / {tb_orders} orders — a leg partial-filled)")
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
    # Accept BOTH labels: the heartbeat was relabelled "Gross P&L" on 2026-08-18
    # (it always WAS gross; the old "Net P&L" label caused a misread of a
    # /stradexit near-miss), but logs written before that still say "Net P&L".
    pnls = [int(x) for x in re.findall(r"(?:Gross|Net) P&L: ([-+]?\d+)", raw)]
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


def stats():
    """Post-market performance read-out — the daily summary Mandar asked for (2026-08-17).

    Deliberately prints the headline return TOGETHER WITH its own uncertainty. A few days of a
    short-vol strategy can look like edge purely from a short tail, so the t-statistic and the
    profit-concentration line sit right next to the return rather than being buried: a mean
    under ~2 standard errors from zero has not been measured yet, however pleasant it looks.

    Return is on margin_blocked (broker-actual), never max_risk_defined — they differ ~4.6x.
    """
    import statistics as st

    rows = _rd(OUT_CSV)
    if not rows:
        print("  no journal yet")
        return
    nets = [float(r["net_pnl"] or 0) for r in rows]
    mgs = [float(r["margin_blocked"]) for r in rows if float(r["margin_blocked"] or 0)]
    roms = [100 * float(r["net_pnl"] or 0) / float(r["margin_blocked"])
            for r in rows if float(r["margin_blocked"] or 0)]
    if not mgs:
        print("  no margin figures yet")
        return
    tot, avg = sum(nets), st.mean(mgs)
    print(f"  === STRADDLE LIVE PERFORMANCE — {len(rows)} traded days, through {rows[-1]['date']} ===\n")
    print(f"  average margin blocked, per traded day : Rs{avg:,.2f}   "
          f"(range Rs{min(mgs):,.0f} - Rs{max(mgs):,.0f})")
    print(f"  total net over the live era            : Rs{tot:+,.2f}")
    print(f"  period return on avg margin           : {100 * tot / avg:+.2f}%  over {len(rows)} traded days")
    print(f"  mean DAILY return on margin           : {st.mean(roms):+.3f}%   (median {st.median(roms):+.3f}%)")
    print(f"  best day {max(roms):+.3f}%  ·  worst day {min(roms):+.3f}%")
    tgr = sum(float(r["gross_pnl"] or 0) for r in rows)
    tc = sum(float(r["charges"] or 0) for r in rows)
    print(f"  win rate {sum(1 for n in nets if n > 0)}/{len(nets)}  ·  charges Rs{tc:,.2f}"
          + (f" ({100 * tc / tgr:.1f}% of gross)" if tgr > 0 else ""))
    if len(roms) >= 2:
        sd = st.stdev(roms); se = sd / len(roms) ** 0.5
        t = st.mean(roms) / se if se else 0.0
        print(f"\n  --- is it measured yet? ---")
        print(f"  stdev {sd:.3f}%  std-error {se:.3f}%  t = {t:.2f}  "
              f"({'NOT yet distinguishable from zero' if abs(t) < 2 else 'clears the usual t~2 bar'})")
        print(f"  95% band, daily   : {st.mean(roms) - 2 * se:+.3f}%  to  {st.mean(roms) + 2 * se:+.3f}%")
        print(f"  95% band, monthly : {(st.mean(roms) - 2 * se) * 20:+.1f}%  to  "
              f"{(st.mean(roms) + 2 * se) * 20:+.1f}%   (x20 trading days)")
    if len(nets) > 2 and tot:
        top2 = sorted(nets)[-2:]
        print(f"  profit concentration: top 2 days = Rs{sum(top2):+,.0f} of Rs{tot:+,.0f} "
              f"({100 * sum(top2) / tot:.0f}%) · without them Rs{tot - sum(top2):+,.0f}")
    print(f"\n  naive monthly run-rate: {100 * tot / avg / len(rows) * 20:+.2f}% — the growth model's "
          f"scenarios start at 4.25%. Arithmetic, not a forecast.")


def main(argv):
    if "--stats" in argv:
        return stats()
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
