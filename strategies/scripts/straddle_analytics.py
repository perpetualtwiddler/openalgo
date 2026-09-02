#!/usr/bin/env python3
"""straddle_analytics.py — ONE implementation of the iron-butterfly maths.

WHY THIS EXISTS. Black-Scholes and the implied-vol bisection were living in two places
(short_straddle_nifty.py and status_check.py) and had already drifted: the strategy bisected
80 times, status_check 90. Harmless at double precision, but it is the same class of defect
as a gate and an order path disagreeing about the expiry — and the composition split, the
single most useful diagnostic we have, existed ONLY in status_check and not in the strategy
at all. Adding a Telegram status push would have made it a third copy of the greeks.

The danger is specific, not aesthetic: if an alert's greeks drift from the exit logic's
greeks, the notification confidently reports a position the strategy does not believe it has.
So everything that prices a leg lives here, and the strategy, status_check.py and the
Telegram bot all import it.

Canonical choices, matched to what the strategy ALREADY did so the refactor is a no-op:
  * 80 bisection iterations, bracket [1e-4, 5.0]  (status_check used 90 — standardised down)
  * r = 6.5% flat
  * P&L on a signed position is qty * (mark - entry): a short (qty<0) gains when the mark falls

A LEG is a plain dict so no caller needs a class:
    {"symbol": str, "qty": int (SIGNED), "entry": float, "mark": float,
     "strike": float, "cp": "C" | "P"}
"""
import math

RISK_FREE = 0.065
IV_ITERATIONS = 80          # matches short_straddle_nifty._implied_vol exactly
IV_BRACKET = (1e-4, 5.0)
YEAR_SECONDS = 365 * 24 * 3600


# ──────────────────────────────────────────────────────────── pricing primitives
def _nd(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S, K, T, sigma, cp, r=RISK_FREE):
    """Black-Scholes price. Below expiry or zero vol, falls back to intrinsic."""
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if cp == "C" else (K - S))
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if cp == "C":
        return S * _nd(d1) - K * math.exp(-r * T) * _nd(d2)
    return K * math.exp(-r * T) * _nd(-d2) - S * _nd(-d1)


def implied_vol(price, S, K, T, cp, r=RISK_FREE):
    """Bisect for the vol reproducing `price`. Fixed iterations — no convergence branch,
    so the result is deterministic and identical across callers."""
    lo, hi = IV_BRACKET
    for _ in range(IV_ITERATIONS):
        mid = (lo + hi) / 2
        if bs(S, K, T, mid, cp, r) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def years_to(exp, when):
    return (exp - when).total_seconds() / YEAR_SECONDS


# ──────────────────────────────────────────────────────────── position maths
def leg_ivs(legs, spot, T, price_key="mark", r=RISK_FREE):
    """Back-solve each leg's own IV from `price_key` at (spot, T)."""
    return {lg["symbol"]: implied_vol(lg[price_key], spot, lg["strike"], T, lg["cp"], r)
            for lg in legs}


def atm_iv(legs, ivs):
    """Mean IV of the two SHORT legs — the conventional 'ATM IV' for a butterfly."""
    sh = [lg for lg in legs if lg["qty"] < 0]
    return sum(ivs[lg["symbol"]] for lg in sh) / len(sh) if sh else None


def gross_at(legs, marks):
    """Signed-position P&L: qty * (mark - entry), so a short gains as the mark falls."""
    return sum(lg["qty"] * (marks[lg["symbol"]] - lg["entry"]) for lg in legs)


def _roundtrip_fills(legs, marks):
    """Entry+exit fills, one synthetic order id per leg-side, for the charge model.
    charges.py bills per ORDER, and one order per leg-side is how we actually trade."""
    out = []
    for lg in legs:
        q, short = abs(lg["qty"]), lg["qty"] < 0
        out += [{"action": "SELL" if short else "BUY", "quantity": q,
                 "price": lg["entry"], "orderid": f"i{lg['symbol']}"},
                {"action": "BUY" if short else "SELL", "quantity": q,
                 "price": marks[lg["symbol"]], "orderid": f"o{lg['symbol']}"}]
    return out


def net_at(legs, marks, charges_fn):
    """Gross minus the full round-trip charge, recomputed AT these marks.

    Recomputed rather than held constant because STT is levied on sell-side premium and
    therefore moves with the exit price. Comparing a projected gross against a realised net
    would flatter every projection.
    """
    return gross_at(legs, marks) - charges_fn(_roundtrip_fills(legs, marks))


def price_all(legs, S, T, ivs, r=RISK_FREE):
    return {lg["symbol"]: bs(S, lg["strike"], T, ivs[lg["symbol"]], lg["cp"], r)
            for lg in legs}


def composition(legs, spot, T_now, entry_spot, T_entry, marks=None):
    """Split the CURRENT gross into durable (theta+delta) and reversible (vega).

    Durable = what today's spot and remaining time would be worth if IV had never moved,
    priced at each leg's OWN entry-implied vol. Whatever is left is the vol move, which can
    hand itself straight back. On multi-day DTE this is usually most of the P&L, which is why
    a big unrealised gain at 6 DTE is not the same thing as a banked one.
    """
    marks = marks or {lg["symbol"]: lg["mark"] for lg in legs}
    iv_e = leg_ivs(legs, entry_spot, T_entry, price_key="entry")
    durable = sum(lg["qty"] * (bs(spot, lg["strike"], T_now, iv_e[lg["symbol"]], lg["cp"])
                               - lg["entry"]) for lg in legs)
    total = gross_at(legs, marks)
    return {"durable": durable, "reversible": total - durable, "gross": total, "entry_ivs": iv_e}


def ladder(legs, centre, T_exit, ivs, charges_fn, offsets=(150, 100, 50, 0, -50, -100, -150)):
    """[(spot, net)] at T_exit for each offset from `centre`, IV held flat."""
    return [(centre + o, net_at(legs, price_all(legs, centre + o, T_exit, ivs), charges_fn))
            for o in offsets]


def projection(legs, atm, T_exit, ivs, charges_fn, span=400, step=5):
    """Golden point, ceiling and the NET>0 band at T_exit, IV held flat.

    A map of where the day PAYS, not a forecast — vega moves it well before the index does.
    """
    grid = [(S, net_at(legs, price_all(legs, S, T_exit, ivs), charges_fn))
            for S in range(int(atm) - span, int(atm) + span + 1, step)]
    if not grid:
        return None
    best = max(grid, key=lambda kv: kv[1])
    pos = [S for S, v in grid if v > 0]
    return {"golden": best[0], "ceiling": best[1],
            "lo": min(pos) if pos else None, "hi": max(pos) if pos else None}


def vega_per_pp(legs, spot, T, ivs, charges_fn, bump=0.01):
    """Rupees of NET per 1 percentage point of IV (positive = we gain when IV FALLS)."""
    down = {s: max(v - bump, 1e-6) for s, v in ivs.items()}
    return (net_at(legs, price_all(legs, spot, T, down), charges_fn)
            - net_at(legs, price_all(legs, spot, T, ivs), charges_fn))


def theta_per_hour(legs, spot, T, ivs, charges_fn):
    """Rupees of NET per hour of pure time decay at this spot and IV."""
    T2 = max(T - 1.0 / (365 * 24), 1e-9)
    return (net_at(legs, price_all(legs, spot, T2, ivs), charges_fn)
            - net_at(legs, price_all(legs, spot, T, ivs), charges_fn))


# ──────────────────────────────────────────────────────────── Telegram formatting
def pit_snapshot(*, now, spot, atm, dte, breach_lo, breach_hi, legs, charges_fn,
                 exp, exit_at, entry_spot, entry_ts, armed_target=None, armed_stop=None):
    """Every point-in-time variable we compute, as a flat dict.

    Exists so the Telegram message and the PIT log are the SAME numbers rather than two
    computations that can drift. The message renders a subset; the CSV keeps all of it.

    This is the dataset for backlog #7b — a dynamic exit rule is a function from PIT state to
    action, and fitting one needs paired observations of (state, what the day went on to do).
    Until now the push computed 15 variables and logged 2, so every checkpoint was thrown away.
    """
    T_now = years_to(exp, now)
    T_exit = years_to(exp, exit_at)
    T_entry = years_to(exp, entry_ts)
    marks = {lg["symbol"]: lg["mark"] for lg in legs}
    ivs = leg_ivs(legs, spot, T_now)
    g = gross_at(legs, marks)
    ch = charges_fn(_roundtrip_fills(legs, marks))
    comp = composition(legs, spot, T_now, entry_spot, T_entry, marks)
    iv_now, iv_ent = atm_iv(legs, ivs), atm_iv(legs, comp["entry_ivs"])
    pj = projection(legs, atm, T_exit, ivs, charges_fn)
    base = net_at(legs, price_all(legs, spot, T_exit, ivs), charges_fn)
    bumped = net_at(legs, price_all(legs, spot + 20, T_exit, ivs), charges_fn)
    return {
        "date": f"{now:%Y-%m-%d}", "time": f"{now:%H:%M:%S}", "dte": dte,
        "spot": round(spot, 2), "atm": atm, "spot_minus_atm": round(spot - atm, 2),
        "breach_room": round(min(abs(spot - breach_lo), abs(spot - breach_hi)), 1),
        "gross": round(g, 2), "charges": round(ch, 2), "net": round(g - ch, 2),
        "iv_now": round(iv_now * 100, 3) if iv_now else "",
        "iv_entry": round(iv_ent * 100, 3) if iv_ent else "",
        "iv_delta_pp": round((iv_now - iv_ent) * 100, 3) if (iv_now and iv_ent) else "",
        "durable": round(comp["durable"], 2), "reversible": round(comp["reversible"], 2),
        "vega_per_pp": round(abs(vega_per_pp(legs, spot, T_now, ivs, charges_fn)), 1),
        "theta_per_hr": round(theta_per_hour(legs, spot, T_now, ivs, charges_fn), 1),
        "rupees_per_point": round(abs(bumped - base) / 20, 2),
        "ceiling": round(pj["ceiling"], 1) if pj else "",
        "golden": pj["golden"] if pj else "", "band_lo": pj["lo"] if pj else "",
        "band_hi": pj["hi"] if pj else "",
        "hours_to_exit": round((exit_at - now).total_seconds() / 3600, 2),
        "armed_target": armed_target or "", "armed_stop": armed_stop or "",
    }


def format_status(*, now, spot, atm, dte, breach_lo, breach_hi, legs, charges_fn,
                  exp, exit_at, entry_spot, entry_ts, armed_target=None, armed_stop=None,
                  mfe_net=None, mae_net=None, md=lambda s: s):
    """The periodic / on-demand status message. Compact by design — it is read on a phone.

    The `DRIVING` marker is load-bearing. Listing theta next to vega without saying which one
    owns the day teaches the wrong intuition: measured at 5-7 DTE, theta is ~Rs67-116/HOUR
    while 0.10pp of IV is ~Rs195. Someone reading a bare theta figure would naturally wait for
    the clock, when the clock is nearly irrelevant.
    """
    T_now = years_to(exp, now)
    T_exit = years_to(exp, exit_at)
    T_entry = years_to(exp, entry_ts)
    marks = {lg["symbol"]: lg["mark"] for lg in legs}
    ivs = leg_ivs(legs, spot, T_now)
    g = gross_at(legs, marks)
    ch = charges_fn(_roundtrip_fills(legs, marks))
    n = g - ch
    comp = composition(legs, spot, T_now, entry_spot, T_entry, marks)
    iv_now, iv_ent = atm_iv(legs, ivs), atm_iv(legs, comp["entry_ivs"])
    vpp = vega_per_pp(legs, spot, T_now, ivs, charges_fn)
    tph = theta_per_hour(legs, spot, T_now, ivs, charges_fn)

    room = min(abs(spot - breach_lo), abs(spot - breach_hi))
    L = [f"📊 *Straddle* · {now:%H:%M} · {dte} DTE",
         f"NIFTY {spot:,.2f}  ({spot - atm:+.0f} from {atm:,.0f} · {room:.0f} pts to breach)",
         "",
         f"*NET {n:+,.0f}*   (gross {g:+,.0f} · charges −{ch:,.0f})",
         ""]
    if iv_now is not None and iv_ent is not None:
        L.append(f"IV {iv_now * 100:.2f}%  (entry {iv_ent * 100:.2f}%, "
                 f"{(iv_now - iv_ent) * 100:+.2f}pp)")
    # whichever lever moved more of today's gross gets the marker
    drive_vega = abs(comp["reversible"]) >= abs(comp["durable"])
    L.append(f"├ vega    {comp['reversible']:+,.0f}   ≈₹{abs(vpp):,.0f} per 1pp"
             + ("   ← DRIVING" if drive_vega else ""))
    L.append(f"└ theta+Δ {comp['durable']:+,.0f}   ≈₹{tph:,.0f}/hr"
             + ("" if drive_vega else "   ← DRIVING"))

    pj = projection(legs, atm, T_exit, ivs, charges_fn)
    if pj:
        L += ["", f"To the {exit_at:%H:%M} exit, IV flat:",
              f"  golden {pj['golden']:,.0f} → ceiling {pj['ceiling']:+,.0f}"]
        if pj["lo"] and pj["hi"]:
            L.append(f"  profit band {pj['lo']:,.0f}–{pj['hi']:,.0f} "
                     f"({pj['hi'] - pj['lo']:,.0f} pts)")
        else:
            L.append("  ⚠ no profitable spot at this IV")
    try:
        adv = exit_advice(legs=legs, spot=spot, atm=atm, dte=dte, T_now=T_now, T_exit=T_exit,
                          ivs=ivs, charges_fn=charges_fn,
                          breach_lo=breach_lo, breach_hi=breach_hi)
        L += format_exit_advice(adv, dte, md)
    except Exception:
        pass          # advice is a nicety; never let it cost us the status message

    if armed_target or armed_stop:
        bits = []
        if armed_target:
            gap = armed_target - n
            need = f" — needs {gap:+,.0f}" if gap > 0 else " — reached"
            if gap > 0 and vpp:
                need += f" (≈{gap / abs(vpp):.2f}pp of IV)"
            bits.append(f"TP net {armed_target:+,.0f}{need}")
        if armed_stop:
            bits.append(f"SL net {armed_stop:+,.0f}")
        L += [""] + [f"Armed: {b}" for b in bits]
    if mfe_net is not None and mae_net is not None:
        L.append(f"Today: MFE {mfe_net:+,.0f} net · MAE {mae_net:+,.0f} net")
    L.append(md("_projection assumes IV holds — vega moves it_"))
    return "\n".join(L)


def exit_advice(*, legs, spot, atm, dte, T_now, T_exit, ivs, charges_fn,
                breach_lo, breach_hi, fill_shortfall=70.0, stop_frac=0.60):
    """Point-in-time exit guidance (#19) — what today can actually pay, and where to cut.

    Deliberately shows a LADDER rather than one number. A single "suggested target" would be a
    fabrication: the first draft of this computed `ceiling x 0.8 + shortfall` and produced +550
    on a day whose IV-flat ceiling was +628 and which went on to pay +841 because vol fell.
    Showing what each level REQUIRES lets the reader price the assumption instead of inheriting
    mine.

    The stop is expressed in NIFTY POINTS, as a fraction of the breach band, because a fixed
    rupee stop is a moving spot threshold: Rs2,000 is ~68 points at 1 DTE and ~151 at 7 DTE, so
    the same number is a hair-trigger on one day and unreachable on another. Measured 2026-08-31
    after a -Rs1,500 stop fired on an 18-point move.

    At low DTE it recommends NO stop and half size instead, per backlog #17: on the seven
    replayed 1-DTE days every stop level fired on 3-4 of 7 and was wrong 2-3 times, because
    rupees-per-point is highest there. The precaution that survives the data is size.
    """
    rows = []
    for shift, label in ((0.0, "IV flat"), (-0.0025, "IV -0.25pp"), (-0.005, "IV -0.50pp")):
        iv2 = {k: max(v + shift, 1e-6) for k, v in ivs.items()}
        pj = projection(legs, atm, T_exit, iv2, charges_fn)
        rows.append((label, pj["ceiling"] if pj else None))

    band_pts = (breach_hi - breach_lo) / 2
    stop_pts = stop_frac * band_pts

    # The stop is priced AT the stop distance, on the RUNNING curve (T_now) -- not by
    # extrapolating a local slope, and not on the exit curve. Both were wrong in the first
    # version (found live 2026-09-02):
    #   * the payoff is CONVEX, and the slope was sampled 20 pts from the golden point where
    #     the curve is flattest, so a straight line from there understated the loss at 79 pts
    #     by ~1.9x (-348 quoted vs -666 real). An arm at the quoted figure would have fired at
    #     ~45 points of movement instead of 79 -- back inside the noise band that #16 exists
    #     to avoid.
    #   * /stradexit compares the RUNNING net, so the stop must be evaluated at T_now. The
    #     take-profit ladder is about net at the square-off and correctly uses T_exit.
    # Takes the WORSE of the two directions, because a stop must hold on either side.
    now_base = net_at(legs, price_all(legs, spot, T_now, ivs), charges_fn)
    up = net_at(legs, price_all(legs, spot + stop_pts, T_now, ivs), charges_fn)
    dn = net_at(legs, price_all(legs, spot - stop_pts, T_now, ivs), charges_fn)
    stop_rupees = min(up, dn)

    # Still report a per-point figure for context, but measure it over the WHOLE stop distance
    # rather than a 20-point tangent, so it is an average gradient and not a peak-local one.
    per_pt = abs(stop_rupees - now_base) / stop_pts if stop_pts else 1e-9
    return {"ladder": rows, "per_pt": per_pt, "stop_pts": stop_pts,
            "stop_rupees": stop_rupees, "now_net": now_base,
            "shortfall": fill_shortfall, "low_dte": dte is not None and dte <= 2}


def format_exit_advice(adv, dte, md=lambda s: s):
    """Render exit_advice() for Telegram. Kept separate so the numbers can be tested
    without parsing a message."""
    L = ["", "💡 *Suggested exits*", f"Reachable by the square-off, pinned at the golden point:"]
    for label, ceil in adv["ladder"]:
        L.append(f"   {label:<11} → {ceil:+,.0f}" if ceil is not None else f"   {label:<11} → n/a")
    flat = adv["ladder"][0][1]
    if flat is not None:
        L.append(f"➜ arm ~₹{adv['shortfall']:.0f} ABOVE what you want banked "
                 f"(measured fill shortfall)")
        if flat <= 0:
            L.append("⚠ no profitable spot at this IV — theta alone will not get there")
    if adv["low_dte"]:
        L += [f"➜ *No stop* at {dte} DTE — a rupee stop is only "
              f"~{2000/adv['per_pt']:.0f} pts here and fires on noise.",
              "   Use half size instead (backlog #17)."]
    else:
        L.append(f"➜ Stop ≈ *{adv['stop_rupees']:+,.0f}*  "
                 f"(net if spot moves {adv['stop_pts']:.0f} pts — 60% of the breach band)")
    L.append(md(f"_avg ₹{adv['per_pt']:,.0f}/NIFTY pt over that distance · {dte} DTE · "
                f"IV held flat_"))
    return L


def material_change(prev, cur, net_delta=400.0, iv_delta_pp=0.15):
    """Suppress-if-unchanged gate for the periodic push.

    Returns True when the position has moved enough to be worth a message. Thresholds are on
    NET rupees and IV, not on time, because a half-hourly heartbeat that says nothing trains
    you to ignore the channel — and a real breach alert then arrives into a muted channel.
    `prev` is None on the first push of the day, which always sends.
    """
    if prev is None:
        return True
    if abs(cur["net"] - prev["net"]) >= net_delta:
        return True
    if (cur.get("iv") is not None and prev.get("iv") is not None
            and abs(cur["iv"] - prev["iv"]) * 100 >= iv_delta_pp):
        return True
    return False
