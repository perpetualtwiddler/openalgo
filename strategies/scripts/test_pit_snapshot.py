"""PIT logging: no regression, and the CSV agrees with the message."""
import csv, os, sys, tempfile
from datetime import datetime
from pathlib import Path
sys.path.insert(0,"/root/data/openalgo"); sys.path.insert(0,"/root/data/openalgo/strategies/scripts")
import charges as chg, straddle_analytics as sa, short_straddle_nifty as ss
CH=lambda f: chg.charges_from_fills(f,True)
P=[0,0]
def ck(n,c,d=""):
    P[0 if c else 1]+=1
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"   {d}" if d and not c else ""))

legs=[{"symbol":"23650PE","qty": 130,"entry":17.55,"mark":16.90,"strike":23650,"cp":"P"},
      {"symbol":"24050CE","qty":-130,"entry":170.40,"mark":153.85,"strike":24050,"cp":"C"},
      {"symbol":"24050PE","qty":-130,"entry":101.20,"mark":107.30,"strike":24050,"cp":"P"},
      {"symbol":"24450CE","qty": 130,"entry":25.70,"mark":20.75,"strike":24450,"cp":"C"}]
kw=dict(now=datetime(2026,9,1,11,30), spot=24064.95, atm=24050, dte=7,
        breach_lo=23917.7, breach_hi=24182.3, legs=legs, charges_fn=CH,
        exp=datetime(2026,9,8,15,15), exit_at=datetime(2026,9,1,15,0),
        entry_spot=24065.30, entry_ts=datetime(2026,9,1,9,35), armed_target=900)

print("\n=== A. the CSV row and the MESSAGE must agree (no second computation drifting) ===")
row=sa.pit_snapshot(**kw)
msg="\n".join(sa.format_status(**kw))if isinstance(sa.format_status(**kw),list) else sa.format_status(**kw)
ck("net matches the message", f"{row['net']:+,.0f}".replace("+","") in msg.replace("+","") or
   f"{row['net']:+,.0f}" in msg, (row['net'], [l for l in msg.split(chr(10)) if 'NET' in l]))
ck("IV matches", f"{row['iv_now']:.2f}" in msg, (row['iv_now'], [l for l in msg.split(chr(10)) if 'IV ' in l][:1]))
ck("durable matches", f"{row['durable']:+,.0f}" in msg, row['durable'])
ck("reversible matches", f"{row['reversible']:+,.0f}" in msg, row['reversible'])
ck("ceiling matches", f"{row['ceiling']:+,.0f}" in msg, row['ceiling'])

print("\n=== B. the row itself is sane and complete ===")
ck("pit_snapshot returns 25 fields", len(row)==25, (len(row), list(row)))
ck("no None/blank in the numeric core",
   all(row[k] not in (None,"") for k in ("net","gross","charges","iv_now","durable",
        "reversible","vega_per_pp","theta_per_hr","rupees_per_point","ceiling")), row)
ck("gross - charges == net", abs((row["gross"]-row["charges"])-row["net"])<0.02,
   (row["gross"],row["charges"],row["net"]))
ck("armed_target carried through", row["armed_target"]==900)

print("\n=== C. _log_pit writes a valid CSV and appends ===")
tmp=Path(tempfile.mkdtemp())/"pit.csv"
ss.PIT_CSV=tmp
b=ss.ShortStraddleBot.__new__(ss.ShortStraddleBot)
b.atm_strike, b.traded_expiry, b.traded_dte = 24050, "08-SEP-26", 7
b.entry_spot, b.entry_ts = 24065.30, datetime(2026,9,1,9,35)
b.ce_symbol,b.ce_entry_price,b.ce_ltp = "NIFTY08SEP2624050CE",170.40,153.85
b.pe_symbol,b.pe_entry_price,b.pe_ltp = "NIFTY08SEP2624050PE",101.20,107.30
b.hedge_ce_symbol,b.hedge_ce_price,b.hedge_ce_ltp = "NIFTY08SEP2624450CE",25.70,20.75
b.hedge_pe_symbol,b.hedge_pe_price,b.hedge_pe_ltp = "NIFTY08SEP2623650PE",17.55,16.90
b.tg_target_net,b.tg_stop_net,b.tg_squareoff = 900,None,None
for kind in ("periodic","suppressed","on-demand"):
    b._log_pit(datetime(2026,9,1,11,30), 24064.95, 347.0, kind)
rows=list(csv.DictReader(open(tmp)))
ck("3 rows appended with one header", len(rows)==3, len(rows))
ck("CSV row has 27 cols (25 + kind + lots)", len(rows[0])==27, len(rows[0]))
ck("kind recorded for each", [r["kind"] for r in rows]==["periodic","suppressed","on-demand"],
   [r["kind"] for r in rows])
ck("lots recorded", all(r["lots"]=="2" for r in rows), [r["lots"] for r in rows])
ck("SUPPRESSED checks are captured too (the point of it)",
   any(r["kind"]=="suppressed" for r in rows))

print("\n=== D. it cannot break the monitor loop ===")
b.traded_expiry=None
b._log_pit(datetime(2026,9,1,11,30), 24064.95, 347.0, "periodic")
ck("missing expiry -> no row, no exception", len(list(csv.DictReader(open(tmp))))==3)
b.traded_expiry="08-SEP-26"; ss.PIT_CSV=Path("/nonexistent-dir-xyz/pit.csv")
b._log_pit(datetime(2026,9,1,11,30), 24064.95, 347.0, "periodic")
ck("unwritable path -> swallowed", True)
# ---------------------------------------------------------------------------------------
# Regression for #23: the quoted stop must equal the ACTUAL net at the stop distance.
# The first implementation linearised a convex payoff from a tangent taken 20 pts out, beside
# the golden point where the curve is flattest, and understated the loss at 79 pts by ~1.9x
# (-348 quoted vs -666 real). It also priced the stop on the exit-time curve when /stradexit
# compares the RUNNING net. This is the property any linearisation breaks.
print("\n=== E. #23 — the stop must be PRICED at the distance, not extrapolated ===")
adv = sa.exit_advice(legs=legs, spot=kw["spot"], atm=kw["atm"], dte=kw["dte"],
                     T_now=sa.years_to(kw["exp"], kw["now"]),
                     T_exit=sa.years_to(kw["exp"], kw["exit_at"]),
                     ivs=sa.leg_ivs(legs, kw["spot"], sa.years_to(kw["exp"], kw["now"])),
                     charges_fn=CH, breach_lo=kw["breach_lo"], breach_hi=kw["breach_hi"])
T_now = sa.years_to(kw["exp"], kw["now"])
ivs_now = sa.leg_ivs(legs, kw["spot"], T_now)
sp = adv["stop_pts"]
up = sa.net_at(legs, sa.price_all(legs, kw["spot"] + sp, T_now, ivs_now), CH)
dn = sa.net_at(legs, sa.price_all(legs, kw["spot"] - sp, T_now, ivs_now), CH)
ck("stop == actual net at +/-stop_pts (worse side)",
   abs(adv["stop_rupees"] - min(up, dn)) < 1.0, (adv["stop_rupees"], min(up, dn)))
ck("stop is priced on the RUNNING curve, not the exit curve",
   abs(adv["stop_rupees"] - min(up, dn)) < abs(adv["stop_rupees"] - min(
       sa.net_at(legs, sa.price_all(legs, kw["spot"] + sp, sa.years_to(kw["exp"], kw["exit_at"]), ivs_now), CH),
       sa.net_at(legs, sa.price_all(legs, kw["spot"] - sp, sa.years_to(kw["exp"], kw["exit_at"]), ivs_now), CH))) + 1e-9)
base_now = sa.net_at(legs, sa.price_all(legs, kw["spot"], T_now, ivs_now), CH)
tangent = abs(sa.net_at(legs, sa.price_all(legs, kw["spot"] + 20, T_now, ivs_now), CH) - base_now) / 20
# The tangent method only fails BADLY near the golden point, where the payoff is flattest.
# 65 pts away (as in this fixture) it happens to be a decent approximation, so asserting the
# two methods differ HERE would be position-dependent and meaningless. Test it where the bug
# actually lived: place spot ON the golden point and show the tangent understates materially
# while ours does not. This is the case that shipped wrong on 2026-09-02.
pj_g = sa.projection(legs, kw["atm"], sa.years_to(kw["exp"], kw["exit_at"]), ivs_now, CH)
g_spot = float(pj_g["golden"])
ivs_g = sa.leg_ivs(legs, g_spot, T_now)
adv_g = sa.exit_advice(legs=legs, spot=g_spot, atm=kw["atm"], dte=kw["dte"], T_now=T_now,
                       T_exit=sa.years_to(kw["exp"], kw["exit_at"]), ivs=ivs_g, charges_fn=CH,
                       breach_lo=kw["breach_lo"], breach_hi=kw["breach_hi"])
g_base = sa.net_at(legs, sa.price_all(legs, g_spot, T_now, ivs_g), CH)
g_tan = abs(sa.net_at(legs, sa.price_all(legs, g_spot + 20, T_now, ivs_g), CH) - g_base) / 20
g_up = sa.net_at(legs, sa.price_all(legs, g_spot + adv_g["stop_pts"], T_now, ivs_g), CH)
g_dn = sa.net_at(legs, sa.price_all(legs, g_spot - adv_g["stop_pts"], T_now, ivs_g), CH)
g_real = min(g_up, g_dn)
ck("AT the golden point, stop is still the real net", abs(adv_g["stop_rupees"] - g_real) < 1.0,
   (adv_g["stop_rupees"], g_real))
ck("  ... and the old tangent method WOULD have understated it materially",
   abs(g_real - g_base) > 1.4 * abs(g_tan * adv_g["stop_pts"]),
   (f"real move {g_real - g_base:+.0f}", f"tangent {-g_tan * adv_g['stop_pts']:+.0f}"))
ck("stop_pts is 60% of the breach half-band",
   abs(sp - 0.60 * (kw["breach_hi"] - kw["breach_lo"]) / 2) < 0.01, sp)
print(f"\n  ════ {P[0]} passed · {P[1]} FAILED ════")
sys.exit(1 if P[1] else 0)
