#!/usr/bin/env python3
"""status_check.py — read the live straddle's status. Snapshot, scheduled, or watching.

WHY THIS EXISTS. Every intraday check used to be a hand-edited copy of the previous day's
scratchpad script with today's ATM, entry time and expiry patched in by sed. That broke twice:
once on a stale ATM, and once on a wait loop that compared `date +%H%M` against an IST target
while the server shell runs CEST -- so an 11:00 check silently never fired and was only caught
by Mandar asking at 10:40. This derives EVERYTHING observable from live broker state, imports
the strategy's own constants for everything that is not observable, and forces IST internally.

Two invariants it is built around:

  * NOTHING about today is hardcoded. ATM, wings, expiry, quantity, entry prices and entry time
    all come from the broker (position book + trade book). There is no value to patch, so there
    is no value to forget to patch.
  * The tool cannot disagree with the strategy. BREACH_PCT, the square-off time and the PT/SL
    thresholds are imported from short_straddle_nifty rather than re-declared -- the same reason
    is_expiry_day() and get_expiry() share one _expiries().

A partly-unwound position (0 < legs < 4) is reported as UNWINDING and never alarmed on. A
half-closed fly prices as a naked short for as long as the remaining legs are open, so its
"net" is meaningless -- an earlier watcher read a 2-leg snapshot as -599 and tripped a loss
alarm on a trade that was in fact closing at +825.

Usage:
    status_check.py                    one snapshot now
    status_check.py --at 11:00         sleep until 11:00 IST, then one snapshot
    status_check.py --watch 20         poll every 20s until square-off
    status_check.py --watch 20 --until 14:30
    status_check.py --watch 20 --alert-below 0 --alert-above 1500
Exit: 0 normal, 2 if an --alert threshold was crossed (so a caller can notice).
"""
import argparse
import glob
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
for pth in (REPO_ROOT, SCRIPT_DIR):
    if pth not in sys.path:
        sys.path.insert(0, pth)

import charges as chg                                    # noqa: E402
import short_straddle_nifty as ss                        # noqa: E402  (import-safe: __main__ guarded)
from openalgo import api                                 # noqa: E402
from database.auth_db import get_api_key_for_tradingview  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
RISK_FREE = float(os.getenv("RISK_FREE", "0.065"))
LOG_GLOB = os.path.join(REPO_ROOT, "log", "strategies", "*straddle*_{ymd}_*")
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def now_ist():
    """Naive datetime in IST. Never datetime.now() -- an ad-hoc SSH shell here is CEST."""
    return datetime.now(IST).replace(tzinfo=None)


# ---------------------------------------------------------------- Black-Scholes / IV
def _n(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S, K, T, sigma, cp):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if cp == "C" else (K - S))
    d1 = (math.log(S / K) + (RISK_FREE + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if cp == "C":
        return S * _n(d1) - K * math.exp(-RISK_FREE * T) * _n(d2)
    return K * math.exp(-RISK_FREE * T) * _n(-d2) - S * _n(-d1)


def implied_vol(price, S, K, T, cp):
    lo, hi = 1e-4, 5.0
    for _ in range(90):
        mid = (lo + hi) / 2
        if bs(S, K, T, mid, cp) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------- symbol parsing
SYM_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<dd>\d{2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})"
                    r"(?P<strike>\d+(?:\.\d+)?)(?P<cp>CE|PE)$")


def parse_symbol(sym):
    m = SYM_RE.match(sym or "")
    if not m:
        return None
    mon = MONTHS.get(m.group("mon"))
    if not mon:
        return None
    return {"root": m.group("root"), "strike": float(m.group("strike")),
            "cp": m.group("cp")[0],
            "expiry": datetime(2000 + int(m.group("yy")), mon, int(m.group("dd")), 15, 15)}


# ---------------------------------------------------------------- live state
class Snapshot:
    """Everything the check needs, derived from the broker rather than declared."""

    def __init__(self, client):
        self.c = client
        self.t = now_ist()
        self.legs = []           # dicts: symbol, qty(signed), entry, ltp, pnl, strike, cp, expiry
        self.spot = None
        self.err = None
        self._load()

    def _load(self):
        try:
            pb = self.c.positionbook().get("data") or []
        except Exception as e:
            self.err = f"positionbook failed: {e}"
            return
        for p in pb:
            q = int(float(p.get("quantity") or 0))
            if q == 0:
                continue
            meta = parse_symbol(p.get("symbol") or "")
            if not meta:
                continue
            self.legs.append({"symbol": p["symbol"], "qty": q,
                              "entry": float(p.get("average_price") or 0),
                              "ltp": float(p.get("ltp") or 0),
                              "pnl": float(p.get("pnl") or 0), **meta})
        self.legs.sort(key=lambda x: x["symbol"])
        try:
            self.spot = float((self.c.quotes(symbol=ss.SYMBOL if hasattr(ss, "SYMBOL") else "NIFTY",
                                             exchange="NSE_INDEX").get("data") or {}).get("ltp") or 0)
        except Exception:
            self.spot = None

    # -- derived structure -------------------------------------------------
    @property
    def shorts(self):
        return [x for x in self.legs if x["qty"] < 0]

    @property
    def atm(self):
        s = self.shorts
        return s[0]["strike"] if s else (self.legs[0]["strike"] if self.legs else None)

    @property
    def qty(self):
        return abs(self.legs[0]["qty"]) if self.legs else 0

    @property
    def expiry(self):
        return self.legs[0]["expiry"] if self.legs else None

    @property
    def state(self):
        n = len(self.legs)
        if self.err:
            return "ERROR"
        if n == 0:
            return "FLAT"
        return "OPEN" if n >= 4 else "UNWINDING"

    # -- money ------------------------------------------------------------
    def _fills(self, marks=None):
        marks = marks or {x["symbol"]: x["ltp"] for x in self.legs}
        f = []
        for x in self.legs:
            q, short = abs(x["qty"]), x["qty"] < 0
            f += [{"action": "SELL" if short else "BUY", "quantity": q,
                   "price": x["entry"], "orderid": f"i{x['symbol']}"},
                  {"action": "BUY" if short else "SELL", "quantity": q,
                   "price": marks[x["symbol"]], "orderid": f"o{x['symbol']}"}]
        return f

    def gross(self, marks=None):
        if marks is None:
            return sum(x["pnl"] for x in self.legs)
        # signed position * (mark - entry): a short gains when the mark falls
        return sum(math.copysign(1, x["qty"]) * abs(x["qty"]) * (marks[x["symbol"]] - x["entry"])
                   for x in self.legs)

    def charges(self, marks=None):
        return chg.charges_from_fills(self._fills(marks), True)

    def net(self, marks=None):
        return self.gross(marks) - self.charges(marks)


def entry_time(client, day):
    """Earliest fill timestamp today -- the entry instant, straight from the broker."""
    try:
        tb = client.tradebook().get("data") or []
    except Exception:
        return None
    ts = []
    for t in tb:
        raw = (t.get("timestamp") or "").strip()
        m = re.search(r"(\d{2}):(\d{2}):(\d{2})", raw)
        if m:
            ts.append(day.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                  second=int(m.group(3)), microsecond=0))
    return min(ts) if ts else None


def log_lines(day):
    """All of today's strategy logs, oldest first.

    Merged rather than files[-1]: restarting openalgo inside the schedule window respawns the
    strategy and opens a fresh log, so the newest file can be an almost-empty stub. Reading
    only that one would silently blank the MFE/MAE line and the [STRADEXIT]/[EXIT] tail --
    i.e. the status check would look fine while telling you nothing. Seen 2026-08-20.
    """
    out = []
    for f in sorted(glob.glob(LOG_GLOB.format(ymd=day.strftime("%Y%m%d")))):
        with open(f, "rb") as fh:
            out.append(fh.read().decode("utf-8", "replace").replace("\r", "\n"))
    return "\n".join(out)


def stradexit_state(today):
    """The command file, plus whether the STRATEGY will actually honour it.

    The file persists across days; `_read_stradexit()` is day-scoped and disarms anything not
    dated today. Reporting the raw payload as "armed" would therefore be a lie on any day after
    it was written -- and a dangerous one, since it invites trading on a target that cannot
    fire. Return the staleness alongside the payload so the caller must state it.
    """
    try:
        d = json.loads(ss.STRADEXIT_FILE.read_text())
    except Exception:
        return None
    if not d:
        return None
    d["_stale"] = d.get("date") != today.strftime("%Y-%m-%d")
    return d


# ---------------------------------------------------------------- report
def band(atm):
    b = atm * ss.BREACH_PCT / 100
    return atm - b, atm + b


def render(sn, client, verbose=True):
    """Print one status block. Returns net (float) or None when it is not meaningful."""
    t = sn.t
    print(f"\n  ═══ {t:%H:%M:%S} IST · {sn.state} ═══")

    if sn.state == "ERROR":
        print(f"   !! {sn.err}")
        return None

    raw = log_lines(t)

    if sn.state == "FLAT":
        print("   no open legs — flat.")
        for pat, lbl in ((r"\[EXIT\][^\n]*", "EXIT"), (r"\[STRADEXIT\][^\n]*", "STRADEXIT"),
                         (r"\[EOD\][^\n]*", "EOD"), (r"\[BREACH\][^\n]*", "BREACH")):
            for m in re.findall(pat, raw)[-2:]:
                print(f"   · {lbl}: {m.strip()[:112]}")
        return None

    atm, qty, exp = sn.atm, sn.qty, sn.expiry
    dte = (exp.date() - t.date()).days
    lo, hi = band(atm)
    if sn.spot:
        room = min(abs(sn.spot - lo), abs(sn.spot - hi))
        print(f"   NIFTY {sn.spot:,.2f}  ({sn.spot - atm:+.0f} from {atm:,.0f} | "
              f"breach {lo:,.0f}/{hi:,.0f} | {room:.0f} pts room | {dte} DTE)")

    for x in sn.legs:
        print(f"    {x['symbol']:<24} {x['qty']:>5}  {x['entry']:>7.2f} -> {x['ltp']:>7.2f}  "
              f"{x['pnl']:>+9.2f}")

    if sn.state == "UNWINDING":
        print(f"\n   ⏳ {len(sn.legs)}/4 legs open — position is CLOSING. P&L on a partial fly is")
        print("      meaningless (it prices as a naked short), so no net/alert is computed.")
        return None

    g, ch = sn.gross(), sn.charges()
    se = sum(x["entry"] for x in sn.shorts)
    sv = sum(x["ltp"] for x in sn.shorts)
    print(f"\n   short straddle {se:.2f} -> {sv:.2f}  ({sv - se:+.2f} pts)")
    print(f"   GROSS {g:>+10,.2f}   charges -{ch:,.2f}   NET {g - ch:>+10,.2f}")

    if not verbose or not sn.spot:
        return g - ch

    # ---- composition: back-solve entry IV, reprice at today's spot/time -------------
    et = entry_time(client, t)
    Tn = (exp - t).total_seconds() / (365 * 24 * 3600)
    if et and Tn > 0:
        Te = (exp - et).total_seconds() / (365 * 24 * 3600)
        # entry IV needs the entry spot; take it from the log, else fall back to today's ATM
        m = re.search(r"\[ENTRY\] NIFTY spot: ([\d.]+)", raw)
        entry_spot = float(m.group(1)) if m else atm
        iv_e = {x["symbol"]: implied_vol(x["entry"], entry_spot, x["strike"], Te, x["cp"])
                for x in sn.legs}
        iv_n = {x["symbol"]: implied_vol(x["ltp"], sn.spot, x["strike"], Tn, x["cp"])
                for x in sn.legs}
        ce = next((x for x in sn.shorts if x["cp"] == "C"), None)
        pe = next((x for x in sn.shorts if x["cp"] == "P"), None)
        if ce and pe:
            print(f"   ATM IV {(iv_n[ce['symbol']] + iv_n[pe['symbol']]) / 2 * 100:.2f}%"
                  f"  (entry {(iv_e[ce['symbol']] + iv_e[pe['symbol']]) / 2 * 100:.2f}%)")
        # durable = what today's spot/time would be worth if IV had never moved
        durable = sum(math.copysign(1, x["qty"]) * abs(x["qty"]) *
                      (bs(sn.spot, x["strike"], Tn, iv_e[x["symbol"]], x["cp"]) - x["entry"])
                      for x in sn.legs)
        print(f"\n   --- COMPOSITION ({dte} DTE) ---")
        print(f"   durable (theta+delta) : {durable:>+9,.0f}")
        print(f"   reversible (vega)     : {g - durable:>+9,.0f}")

        # ---- payoff ladder at the strategy's own square-off time --------------------
        ex = ss._squareoff_at(t)
        Tx = (exp - ex).total_seconds() / (365 * 24 * 3600)
        if Tx > 0:
            print(f"\n   === net at the {ex:%H:%M} square-off, IV as now ===")
            best = None
            for S in [atm + k for k in (150, 100, 50, 0, -50, -100, -150)]:
                marks = {x["symbol"]: bs(S, x["strike"], Tx, iv_n[x["symbol"]], x["cp"])
                         for x in sn.legs}
                tot = sn.net(marks)
                if best is None or tot > best[1]:
                    best = (S, tot)
                tag = " <- ATM/golden" if S == atm else (" <- spot now" if abs(S - sn.spot) < 25 else "")
                print(f"   {S:>9,.0f}  {tot:>+10,.0f}{tag}")
            print(f"   ceiling {best[1]:+,.0f} at {best[0]:,.0f}")

    # ---- armed exits and anything the strategy shouted -----------------------------
    sx = stradexit_state(t)
    if sx and sx.pop("_stale"):
        print(f"\n   /stradexit: NOT ARMED — file is dated {sx.get('date')}, not today."
              f" The strategy ignores it (day-scoped). Send a fresh /stradexit to arm.")
    elif sx:
        tgt, stp = sx.get("target_net"), sx.get("stop_net")
        bits = [f"take-profit net {tgt:+,.0f}" for _ in (1,) if tgt] + \
               [f"stop net {stp:+,.0f}" for _ in (1,) if stp]
        print(f"\n   /stradexit ARMED today: {' · '.join(bits) or 'nothing (both cleared)'}")
    print(f"   targets PT +{ss.PROFIT_TARGET_PCT:.0f}% / SL -{ss.STOPLOSS_PCT:.0f}%")
    # The strategy logs "Gross P&L: +N" per monitor pass, so these are GROSS excursions.
    # Netting them needs the round-trip charge, which is what /stradexit actually compares
    # against -- printing a gross MFE beside an armed NET target invites a wrong read.
    pn = [int(x) for x in re.findall(r"(?:Gross|Net) P&L: ([-+]?\d+)", raw)]
    if pn:
        ch = sn.charges()
        print(f"   today (log samples): MFE {max(pn):+,} gross (~{max(pn)-ch:+,.0f} net)"
              f"  ·  MAE {min(pn):+,} gross (~{min(pn)-ch:+,.0f} net)")
    for pat, lbl in ((r"\[STRADEXIT\][^\n]*", "STRADEXIT"), (r"\[BREACH\][^\n]*", "BREACH"),
                     (r"\[FEED[^\n]*", "FEED"), (r"\[ERROR\][^\n]*", "ERROR")):
        for m in re.findall(pat, raw)[-2:]:
            print(f"   !! {lbl}: {m.strip()[:112]}")
    return g - ch


# ---------------------------------------------------------------- entry point
def sleep_until(hhmm):
    tgt = datetime.strptime(hhmm, "%H:%M").time()
    while True:
        n = now_ist()
        if n.time() >= tgt:
            return
        wait = min(30, max(1, (datetime.combine(n.date(), tgt) - n).total_seconds()))
        time.sleep(wait)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", metavar="HH:MM", help="sleep until this IST time, then one snapshot")
    ap.add_argument("--watch", type=int, metavar="SEC", help="poll every SEC until --until")
    ap.add_argument("--until", metavar="HH:MM", help="stop watching at this IST time")
    ap.add_argument("--alert-below", type=float)
    ap.add_argument("--alert-above", type=float)
    ap.add_argument("--quiet", action="store_true", help="one line per poll while watching")
    a = ap.parse_args(argv)

    client = api(api_key=get_api_key_for_tradingview("admin"),
                 host=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"))

    if a.at:
        print(f"  waiting for {a.at} IST (now {now_ist():%H:%M:%S} IST)…")
        sleep_until(a.at)

    if not a.watch:
        render(Snapshot(client), client)
        return 0

    stop = a.until or f"{ss.SQUAREOFF_HOUR:02d}:{ss.SQUAREOFF_MINUTE:02d}"
    stop_t = datetime.strptime(stop, "%H:%M").time()
    print(f"  watching every {a.watch}s until {stop} IST"
          f"{f' · alert <{a.alert_below:+,.0f}' if a.alert_below is not None else ''}"
          f"{f' · alert >{a.alert_above:+,.0f}' if a.alert_above is not None else ''}")
    peak = trough = None
    hit = False
    while now_ist().time() < stop_t:
        sn = Snapshot(client)
        net = render(sn, client, verbose=not a.quiet) if not a.quiet else None
        if a.quiet:
            net = sn.net() if sn.state == "OPEN" else None
            flag = "" if net is None else ("" if not (
                (a.alert_below is not None and net <= a.alert_below) or
                (a.alert_above is not None and net >= a.alert_above)) else "  ***")
            print(f"  {sn.t:%H:%M:%S}  {sn.state:<9} legs {len(sn.legs)}  "
                  f"{'' if net is None else f'net {net:>+8,.0f}'}"
                  f"{'' if peak is None else f'  (peak {peak:>+,.0f} trough {trough:>+,.0f})'}{flag}")
        # thresholds only ever consulted on a COMPLETE fly -- see module docstring
        if net is not None and sn.state == "OPEN":
            peak = net if peak is None else max(peak, net)
            trough = net if trough is None else min(trough, net)
            if ((a.alert_below is not None and net <= a.alert_below) or
                    (a.alert_above is not None and net >= a.alert_above)):
                print(f"\n  ==== ⚠  NET {net:+,.0f} crossed an alert threshold ====")
                render(sn, client)
                hit = True
                break
        if sn.state == "FLAT" and peak is not None:
            print("\n  position closed — stopping watch.")
            break
        time.sleep(a.watch)
    if peak is not None:
        print(f"\n  watch done · NET peak {peak:+,.0f}  trough {trough:+,.0f}")
    return 2 if hit else 0


if __name__ == "__main__":
    sys.exit(main())
