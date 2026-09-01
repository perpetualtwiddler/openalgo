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
print(f"\n  ════ {P[0]} passed · {P[1]} FAILED ════")
sys.exit(1 if P[1] else 0)
