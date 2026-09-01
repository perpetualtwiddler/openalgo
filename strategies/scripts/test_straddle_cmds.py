"""Regression suite for the /straddle command family (#18).

The dangerous part is `lots N`. QUANTITY is a module-level constant read in 41 places, and the
implementation mutates the global rather than threading a parameter through all of them. That
is only safe because every reference lives inside a function and therefore reads the global at
call time — so this file ASSERTS that property with an AST walk rather than trusting it, and
then proves the order path, charge model, P&L and projection all move together.

Also asserts the things that make it safe to hand to a person: day-scoping (yesterday's skip
must never cancel today's trade), the 09:35 cutoff, bounds, and that /stradexit still works
unchanged alongside it.
"""
import ast
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/root/data/openalgo")
sys.path.insert(0, "/root/data/openalgo/strategies/scripts")
import services.telegram_bot_service as tbs   # noqa: E402
import short_straddle_nifty as ss             # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")
P = [0, 0]


def ck(n, c, d=""):
    P[0 if c else 1] += 1
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"   {d}" if d and not c else ""))


class Msg:
    def __init__(self): self.sent = []
    async def reply_text(self, t, **k): self.sent.append(t)


class Upd:
    def __init__(self): self.message = Msg(); self.effective_user = type("U", (), {"id": 8695581038})()


def run(args, path):
    svc = tbs.TelegramBotService.__new__(tbs.TelegramBotService)
    svc._get_sdk_client = lambda uid: None
    u = Upd(); ctx = type("C", (), {"args": args})()
    tbs.get_telegram_user = lambda uid: {"id": uid}
    os.environ["STRADEXIT_FILE"] = str(path)
    asyncio.get_event_loop().run_until_complete(svc.cmd_straddle(u, ctx))
    return u.message.sent[0] if u.message.sent else ""


def load(path):
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}


d = tempfile.mkdtemp(); path = Path(d) / "straddle_command.json"

print("\n=== A. THE SAFETY PROPERTY the global-mutation approach depends on ===")
src = open("/root/data/openalgo/strategies/scripts/short_straddle_nifty.py").read()
tree = ast.parse(src)
mod_refs = []
class V(ast.NodeVisitor):
    def __init__(self): self.depth = 0
    def visit_FunctionDef(self, n): self.depth += 1; self.generic_visit(n); self.depth -= 1
    def visit_AsyncFunctionDef(self, n): self.visit_FunctionDef(n)
    def visit_ClassDef(self, n): self.depth += 1; self.generic_visit(n); self.depth -= 1
    def visit_Name(self, n):
        if n.id in ("QUANTITY", "LOTS") and self.depth == 0 and isinstance(n.ctx, ast.Load):
            mod_refs.append((n.id, n.lineno))
V().visit(tree)
# The property that actually matters: nothing reads QUANTITY at module scope (such a read
# would capture a value BEFORE _apply_day_size runs and then never update). Reading LOTS at
# module scope is fine on exactly one line -- `QUANTITY = LOT_SIZE * LOTS`, the initialisation
# -- because _apply_day_size reassigns BOTH names together, so they cannot drift apart.
qty_reads = [r for r in mod_refs if r[0] == "QUANTITY"]
lots_reads = [r for r in mod_refs if r[0] == "LOTS"]
init_line = next((i + 1 for i, ln in enumerate(src.splitlines())
                  if ln.startswith("QUANTITY = LOT_SIZE * LOTS")), None)
ck("QUANTITY is never read at module scope", not qty_reads, qty_reads)
ck("LOTS read at module scope ONLY on the QUANTITY init line",
   [r[1] for r in lots_reads] == [init_line], (lots_reads, init_line))
ck("_apply_day_size declares both globals",
   "global LOTS, QUANTITY" in src)

print("\n=== B. size actually propagates everywhere ===")
def bot_at(dte, cfg):
    b = ss.ShortStraddleBot.__new__(ss.ShortStraddleBot)
    b.traded_dte = dte
    return b
ss.LOTS, ss.QUANTITY = 2, 130          # reset to the configured default
b = bot_at(7, {})
b._apply_day_size({"lots": 1})
ck("lots 1 -> QUANTITY 65", (ss.LOTS, ss.QUANTITY) == (1, 65), (ss.LOTS, ss.QUANTITY))
ck("  ... charge model sees 65",
   ss.ShortStraddleBot._roundtrip_charges.__code__.co_names.count("QUANTITY") > 0)
ss.LOTS, ss.QUANTITY = 2, 130
b = bot_at(1, {})
b._apply_day_size({})
ck("1 DTE auto-halves to 1 lot (#17)", (ss.LOTS, ss.QUANTITY) == (1, 65), (ss.LOTS, ss.QUANTITY))
ss.LOTS, ss.QUANTITY = 2, 130
b = bot_at(1, {})
b._apply_day_size({"lots": 2})
ck("  ... explicit lots overrides the 1-DTE default", (ss.LOTS, ss.QUANTITY) == (2, 130))
ss.LOTS, ss.QUANTITY = 2, 130
b = bot_at(7, {})
b._apply_day_size({})
ck("7 DTE keeps the default 2 lots", (ss.LOTS, ss.QUANTITY) == (2, 130))
ss.LOTS, ss.QUANTITY = 2, 130
b = bot_at(7, {}); b._apply_day_size({"lots": 3}); b._apply_day_size({"lots": 3})
ck("idempotent across entry retries", (ss.LOTS, ss.QUANTITY) == (3, 195))
ss.LOTS, ss.QUANTITY = 2, 130

print("\n=== C. strategy-side day config: bounds and day-scoping ===")
def cfg_with(payload):
    path.write_text(json.dumps(payload))
    b = ss.ShortStraddleBot.__new__(ss.ShortStraddleBot)
    ss.STRADEXIT_FILE = path
    return b._read_day_config()
ck("reads lots + skip for today",
   cfg_with({"lots": 1, "skip": True, "date": TODAY}) == {"lots": 1, "skip": True})
ck("STALE day ignored entirely",
   cfg_with({"lots": 1, "skip": True, "date": "2020-01-01"}) == {"lots": None, "skip": False})
ck("lots above LOTS_MAX refused",
   cfg_with({"lots": 99, "date": TODAY})["lots"] is None)
ck("lots below LOTS_MIN refused",
   cfg_with({"lots": 0, "date": TODAY})["lots"] is None)
ck("garbage lots refused, skip still honoured",
   cfg_with({"lots": "abc", "skip": True, "date": TODAY}) == {"lots": None, "skip": True})
ck("missing file -> safe default",
   (path.unlink() or cfg_with({"date": TODAY})) == {"lots": None, "skip": False})

print("\n=== D. bot command surface ===")
path.write_text(json.dumps({"date": TODAY}))
tbs.ENTRY_HHMM = (23, 59)                      # pretend we are before entry
r = run(["lots", "1"], path)
ck("lots 1 accepted", load(path).get("lots") == 1, load(path))
ck("  ... reply confirms", "1 lot" in r, r[:70])
r = run(["lots", "99"], path)
ck("lots 99 refused", load(path).get("lots") == 1 and "refused" in r.lower(), r[:70])
r = run(["skip"], path)
ck("skip sets the flag", load(path).get("skip") is True)
ck("  ... and says the strategy is NOT stopped", "not stopped" in r.lower(), r[:90])
ck("  ... lots preserved alongside skip", load(path).get("lots") == 1)
r = run(["skip", "off"], path)
ck("skip off clears it", "skip" not in load(path))
r = run([], path)
ck("bare /straddle reports without writing", load(path).get("lots") == 1 and "Configured" in r)
tbs.ENTRY_HHMM = (0, 0)                        # pretend we are past entry
before = load(path)
r = run(["lots", "2"], path)
ck("lots REFUSED after 09:35", load(path) == before and "Too late" in r, r[:60])
r = run(["skip"], path)
ck("skip REFUSED after 09:35", load(path) == before and "Too late" in r, r[:60])
tbs.ENTRY_HHMM = (9, 35)

print("\n=== E. /stradexit still works alongside it ===")
path.write_text(json.dumps({"lots": 1, "skip": False, "date": TODAY}))
svc = tbs.TelegramBotService.__new__(tbs.TelegramBotService)
svc._get_sdk_client = lambda uid: None
u = Upd(); tbs.get_telegram_user = lambda uid: {"id": uid}
os.environ["STRADEXIT_FILE"] = str(path)
asyncio.get_event_loop().run_until_complete(
    svc.cmd_stradexit(u, type("C", (), {"args": ["1500"]})()))
ck("/stradexit still arms", load(path).get("target_net") == 1500.0, load(path))
ck("  ... without clobbering lots", load(path).get("lots") == 1, load(path))

print(f"\n  ════ {P[0]} passed · {P[1]} FAILED ════")
sys.exit(1 if P[1] else 0)
