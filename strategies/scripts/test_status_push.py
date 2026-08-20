"""End-to-end test of the status push: periodic suppression, on-demand handshake, isolation."""
import json, os, sys, time
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, "/root/data/openalgo"); sys.path.insert(0, "/root/data/openalgo/strategies/scripts")
import short_straddle_nifty as ss
import straddle_analytics as sa

P = [0, 0]
def ck(n, c, d=""):
    P[0 if c else 1] += 1
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"   {d}" if d and not c else ""))

req = Path("/tmp/status_req.json")
if req.exists(): req.unlink()
ss.STATUS_REQUEST_FILE = req

def bot(marks, now, tp=None):
    b = ss.ShortStraddleBot.__new__(ss.ShortStraddleBot)
    b.atm_strike, b.traded_expiry, b.traded_dte = 24200, "25-AUG-26", 5
    b.entry_spot, b.entry_ts = 24198.20, datetime(2026,8,20,9,35,1)
    b.ce_symbol, b.ce_entry_price, b.ce_ltp = "NIFTY25AUG2624200CE", 132.25, marks[0]
    b.pe_symbol, b.pe_entry_price, b.pe_ltp = "NIFTY25AUG2624200PE", 75.35, marks[1]
    b.hedge_ce_symbol, b.hedge_ce_price, b.hedge_ce_ltp = "NIFTY25AUG2624600CE", 12.30, marks[2]
    b.hedge_pe_symbol, b.hedge_pe_price, b.hedge_pe_ltp = "NIFTY25AUG2623800PE", 8.40, marks[3]
    b.tg_target_net, b.tg_stop_net, b.tg_squareoff = tp, None, None
    b._status_next = b._status_prev = b._status_req_mtime = None
    b.exit_in_progress = False
    b.sent = []; b._tg_notify = lambda m: b.sent.append(m)
    return b

M1 = (141.75, 60.25, 10.20, 7.20)     # 12:08 real marks, net ~+41
M2 = (172.85, 50.55, 14.00, 6.95)     # 12:50 real marks, net ~-2,282
T1 = datetime(2026,8,20,12,8,46)

print("=== A. periodic push ===")
b = bot(M1, T1, tp=1100)
b._maybe_status(T1, 24212.90, 41.0)
ck("first push of the day always sends", len(b.sent) == 1, b.sent)
ck("  ... contains NET, IV, DRIVING, projection",
   all(k in b.sent[0] for k in ("NET", "IV ", "DRIVING", "golden")), b.sent[:1])
ck("  ... next push scheduled +30min",
   b._status_next == T1 + timedelta(minutes=30), b._status_next)

b._maybe_status(T1 + timedelta(minutes=5), 24212.90, 41.0)
ck("not due yet -> no send", len(b.sent) == 1, len(b.sent))

t2 = T1 + timedelta(minutes=31)
b._maybe_status(t2, 24212.90, 60.0)          # net moved only 19
ck("due but UNCHANGED -> suppressed", len(b.sent) == 1, len(b.sent))
b2 = bot(M2, t2, tp=900); b2._status_prev = {"net": 41.0, "iv": 0.0874}
b2._status_next = t2
b2._maybe_status(t2, 24241.90, -2282.0)      # net moved 2,323
ck("due and MATERIALLY moved -> sends", len(b2.sent) == 1, len(b2.sent))
ck("  ... spike message warns there is no profitable spot",
   "no profitable spot" in b2.sent[0], b2.sent[:1])

print("\n=== B. on-demand handshake ===")
b3 = bot(M1, T1, tp=1100)
b3._status_next = T1 + timedelta(minutes=30)     # not due
req.write_text(json.dumps({"requested_at": datetime.now().isoformat(), "source": "test"}))
b3._maybe_status(T1, 24212.90, 41.0)
ck("request answered even though not due", len(b3.sent) == 1, len(b3.sent))
b3._maybe_status(T1, 24212.90, 41.0)
ck("same request served only ONCE", len(b3.sent) == 1, len(b3.sent))
req.write_text(json.dumps({"requested_at": datetime.now().isoformat(), "source": "test"}))
os.utime(req, (time.time() - 600, time.time() - 600))     # 10 min old
b3._status_req_mtime = None
b3._maybe_status(T1, 24212.90, 41.0)
ck("STALE request (>120s) ignored", len(b3.sent) == 1, len(b3.sent))

print("\n=== C. on-demand overrides suppression ===")
b4 = bot(M1, T1, tp=1100)
b4._status_prev = {"net": 41.0, "iv": 0.0874}      # identical -> would suppress
b4._status_next = T1 + timedelta(minutes=30)
req.write_text(json.dumps({"requested_at": datetime.now().isoformat(), "source": "test"}))
b4._status_req_mtime = None
b4._maybe_status(T1, 24212.90, 41.0)
ck("on-demand ignores the suppress gate", len(b4.sent) == 1, len(b4.sent))

print("\n=== D. failure isolation — must never raise into the monitor loop ===")
b5 = bot(M1, T1); b5.traded_expiry = None
b5._maybe_status(T1, 24212.90, 41.0)
ck("missing expiry -> no send, no exception", len(b5.sent) == 0)
b6 = bot(M1, T1); b6.ce_ltp = None
b6._maybe_status(T1, 24212.90, 41.0)
ck("missing LTP -> no send, no exception", len(b6.sent) == 0)
b7 = bot(M1, T1); b7._tg_notify = lambda m: (_ for _ in ()).throw(RuntimeError("telegram down"))
b7._maybe_status(T1, 24212.90, 41.0)
ck("telegram failure swallowed (loop survives)", True)
b8 = bot(M1, T1); b8.exit_in_progress = True
# the call site guards on this; assert the guard exists in source
src = open("/root/data/openalgo/strategies/scripts/short_straddle_nifty.py").read()
ck("call site guarded by `not self.exit_in_progress`",
   "if not self.exit_in_progress:\n                self._maybe_status" in src)

print("\n=== E. STATUS_NOTIFY_MIN=0 disables the periodic push only ===")
if req.exists(): req.unlink()      # clear the leftover request from section C
ss.STATUS_NOTIFY_MIN = 0
b9 = bot(M1, T1); b9._maybe_status(T1, 24212.90, 41.0)
ck("periodic disabled -> silent", len(b9.sent) == 0, len(b9.sent))
req.write_text(json.dumps({"requested_at": datetime.now().isoformat(), "source": "test"}))
b9._status_req_mtime = None
b9._maybe_status(T1, 24212.90, 41.0)
ck("  ... but on-demand STILL works", len(b9.sent) == 1, len(b9.sent))
ss.STATUS_NOTIFY_MIN = 30

print(f"\n  ════ {P[0]} passed · {P[1]} FAILED ════")
sys.exit(1 if P[1] else 0)
