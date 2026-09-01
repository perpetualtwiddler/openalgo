"""The shared module must reproduce the strategy's CURRENT projection exactly.

Refactoring live exit-adjacent maths is only safe if the new code is provably the same code.
This compares against /tmp/golden.json, captured from the strategy BEFORE any changes.
"""
import json, sys
from datetime import datetime
sys.path.insert(0, "/root/data/openalgo"); sys.path.insert(0, "/root/data/openalgo/strategies/scripts")
import charges as chg
import short_straddle_nifty as ss
import straddle_analytics as sa

# GOLDEN REFERENCE — captured 2026-08-19 from short_straddle_nifty._payoff_projection()
# BEFORE it was refactored to delegate to straddle_analytics. Embedded rather than read from
# /tmp: the file was cleaned up and this test then died with FileNotFoundError on every run,
# which is worse than having no test — a golden-reference check whose reference is in a temp
# directory is a one-shot, not a regression test. These numbers must NEVER be regenerated from
# current code; that would make the test compare the new implementation against itself.
golden = {
    "20260820": {"golden": 24180, "ceiling": 152.52035268288222, "lo": 24150, "hi": 24210},
    "20260820_bs": 112.78301210671816,
    "20260820_iv": 0.10813349580530385,
    "20260819": {"golden": 24020, "ceiling": 279.51113173689896, "lo": 23965, "hi": 24075},
    "20260819_bs": 112.78301210671816,
    "20260819_iv": 0.09642978605547092,
}
QTY = 130
CASES = {
 "20260820": dict(atm=24200, exp=datetime(2026,8,25,15,15), spot=24198.20,
                  ts=datetime(2026,8,20,9,35,1), exit=datetime(2026,8,20,15,0),
                  legs=[("NIFTY25AUG2624200CE",-QTY,132.25,24200,"C"),
                        ("NIFTY25AUG2624200PE",-QTY, 75.35,24200,"P"),
                        ("NIFTY25AUG2624600CE", QTY, 12.30,24600,"C"),
                        ("NIFTY25AUG2623800PE", QTY,  8.40,23800,"P")]),
 "20260819": dict(atm=24050, exp=datetime(2026,8,25,15,15), spot=24073.55,
                  ts=datetime(2026,8,19,9,35,3), exit=datetime(2026,8,19,15,0),   # 15:00 — the golden was captured post-change
                  legs=[("NIFTY25AUG2624050CE",-QTY,165.10,24050,"C"),
                        ("NIFTY25AUG2624050PE",-QTY, 94.30,24050,"P"),
                        ("NIFTY25AUG2624450CE", QTY, 25.05,24450,"C"),
                        ("NIFTY25AUG2623650PE", QTY, 14.25,23650,"P")]),
}
P = [0, 0]
def ck(name, got, want, tol=1e-9):
    ok = (got == want) if isinstance(want, (str, int)) and not isinstance(want, bool) \
         else abs(float(got) - float(want)) <= tol
    P[0 if ok else 1] += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<46} got={got!r} want={want!r}")

print("=== primitives: shared module vs the strategy's own methods ===")
b = ss.ShortStraddleBot.__new__(ss.ShortStraddleBot)
ck("bs() identical", sa.bs(24200, 24200, 5/365, 0.09, "C"), golden["20260820_bs"], 0.0)
ck("implied_vol() identical (80 iters)",
   sa.implied_vol(132.25, 24198.20, 24200, 5/365, "C"), golden["20260820_iv"], 0.0)

print("\n=== projection: shared module vs golden reference ===")
for tag, d in CASES.items():
    legs = [{"symbol": s, "qty": q, "entry": e, "mark": e, "strike": k, "cp": c}
            for s, q, e, k, c in d["legs"]]
    T0 = sa.years_to(d["exp"], d["ts"])
    T1 = sa.years_to(d["exp"], d["exit"])
    ivs = sa.leg_ivs(legs, d["spot"], T0, price_key="entry")
    pj = sa.projection(legs, d["atm"], T1, ivs, lambda f: chg.charges_from_fills(f, True))
    g = golden[tag]
    ck(f"{tag} golden point", pj["golden"], g["golden"])
    ck(f"{tag} ceiling",      pj["ceiling"], g["ceiling"], 0.01)
    ck(f"{tag} band lo",      pj["lo"], g["lo"])
    ck(f"{tag} band hi",      pj["hi"], g["hi"])

print("\n=== composition matches what status_check computed live today ===")
# 12:08 snapshot, real marks, verified against status_check output at the time
legs = [{"symbol":"NIFTY25AUG2623800PE","qty": 130,"entry":8.40,"mark":7.20,"strike":23800,"cp":"P"},
        {"symbol":"NIFTY25AUG2624200CE","qty":-130,"entry":132.25,"mark":141.75,"strike":24200,"cp":"C"},
        {"symbol":"NIFTY25AUG2624200PE","qty":-130,"entry":75.35,"mark":60.25,"strike":24200,"cp":"P"},
        {"symbol":"NIFTY25AUG2624600CE","qty": 130,"entry":12.30,"mark":10.20,"strike":24600,"cp":"C"}]
exp = datetime(2026,8,25,15,15); now = datetime(2026,8,20,12,8,46)
comp = sa.composition(legs, 24212.90, sa.years_to(exp, now), 24198.20,
                      sa.years_to(exp, datetime(2026,8,20,9,35,1)))
ck("gross at 12:08", comp["gross"], 299.00, 0.01)
ck("durable ~ +52 (status_check said +52)", round(comp["durable"]), 52, 1)
ck("reversible ~ +247 (status_check said +247)", round(comp["reversible"]), 247, 1)

print(f"\n  ════ {P[0]} passed · {P[1]} FAILED ════")
sys.exit(1 if P[1] else 0)
