"""Regression suite for /stradexit after adding the `time` sub-command.

The point of this file is the FIRST section: every pre-existing usage must behave exactly as
before. The `time` slot is new surface area on a command that closes real money positions, so
the numeric slots, 0-to-clear and the bare report are all re-asserted rather than assumed.
"""
import asyncio, json, os, sys, tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/root/data/openalgo")
sys.path.insert(0, "/root/data/openalgo/strategies/scripts")

import services.telegram_bot_service as tbs
import short_straddle_nifty as ss

TODAY = datetime.now().strftime("%Y-%m-%d")
P = [0, 0]


class Msg:
    def __init__(self): self.sent = []
    async def reply_text(self, text, **kw): self.sent.append(text)


class Upd:
    def __init__(self, uid=8695581038):
        self.message = Msg()
        self.effective_user = type("U", (), {"id": uid})()


def run(args, path, svc=None):
    """Invoke cmd_stradexit exactly as python-telegram-bot would."""
    svc = svc or tbs.TelegramBotService.__new__(tbs.TelegramBotService)
    svc._get_sdk_client = lambda uid: None
    u = Upd()
    ctx = type("C", (), {"args": args})()
    tbs.get_telegram_user = lambda uid: {"id": uid}
    os.environ["STRADEXIT_FILE"] = str(path)
    asyncio.get_event_loop().run_until_complete(svc.cmd_stradexit(u, ctx))
    return u.message.sent[0] if u.message.sent else ""


def load(path):
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}


def check(name, cond, detail=""):
    P[0 if cond else 1] += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))


d = tempfile.mkdtemp()
path = Path(d) / "straddle_command.json"

print("\n=== A. PRE-EXISTING BEHAVIOUR — must be byte-identical to before ===")
r = run(["2000"], path); j = load(path)
check("/stradexit 2000 arms target", j.get("target_net") == 2000.0, j)
check("  ... does not touch stop", j.get("stop_net") is None, j)
check("  ... day-stamped", j.get("date") == TODAY, j)
check("  ... reply says armed", "armed" in r.lower(), r[:60])

r = run(["-3000"], path); j = load(path)
check("/stradexit -3000 arms stop", j.get("stop_net") == -3000.0, j)
check("  ... PRESERVES the target (independent slots)", j.get("target_net") == 2000.0, j)

r = run(["1,250"], path); j = load(path)
check("comma-formatted value still parses", j.get("target_net") == 1250.0, j)

r = run(["0"], path); j = load(path)
check("/stradexit 0 clears BOTH", j.get("target_net") is None and j.get("stop_net") is None, j)
check("  ... reply says cancelled", "cancel" in r.lower(), r[:60])

r = run(["abc"], path)
check("non-numeric rejected", "not a number" in r, r[:60])

run(["800"], path)
before = load(path)
r = run([], path)
check("bare /stradexit reports, writes nothing", load(path) == before, "file changed")
check("  ... shows the armed target", "800" in r, r[:80])

print("\n=== B. NEW `time` SUB-COMMAND ===")
r = run(["time", "15:10"], path); j = load(path)
check("time 15:10 sets squareoff_time", j.get("squareoff_time") == "15:10", j)
check("  ... does NOT disturb the armed target", j.get("target_net") == 800.0, j)
check("  ... reply confirms", "15:10" in r, r[:80])

r = run(["time"], path)
check("bare `time` reports without writing", load(path).get("squareoff_time") == "15:10", r[:60])
check("  ... and is NOT parsed as a number", "not a number" not in r, r[:80])

r = run(["time", "15:20"], path); j = load(path)
check("time 15:20 REFUSED (past 15:12 cap)", "refused" in r.lower(), r[:80])
check("  ... file unchanged by the refusal", j.get("squareoff_time") == "15:10", j)

r = run(["time", "09:00"], path)
check("time 09:00 refused (before 09:40)", "refused" in r.lower(), r[:80])

r = run(["time", "banana"], path)
check("time banana rejected", "not a time" in r, r[:80])

r = run(["time", "default"], path); j = load(path)
check("time default clears the override", "squareoff_time" not in j, j)
check("  ... reply uses the word 'default'", "default" in r.lower(), r[:60])
check("  ... target STILL armed after clearing time", j.get("target_net") == 800.0, j)

r = run(["time", "off"], path)
check("'off' still accepted as an alias", "default" in r.lower(), r[:60])

print("\n=== C. STRATEGY SIDE — does the override actually move the exit? ===")
bot = ss.ShortStraddleBot.__new__(ss.ShortStraddleBot)
bot.tg_cmd_mtime = None; bot.tg_target_net = bot.tg_stop_net = None
bot.tg_squareoff = None; bot.tg_armed_log = []
ss.STRADEXIT_FILE = path

now = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0)
check("default square-off is 15:00",
      bot._squareoff_at(now).strftime("%H:%M") == "15:00", bot._squareoff_at(now))

path.write_text(json.dumps({"squareoff_time": "15:10", "date": TODAY, "source": "test"}))
bot.tg_cmd_mtime = None; bot._read_stradexit()
check("strategy reads the override", bot.tg_squareoff == (15, 10), bot.tg_squareoff)
check("  ... _squareoff_at honours it",
      bot._squareoff_at(now).strftime("%H:%M") == "15:10", bot._squareoff_at(now))
check("  ... offsets shift with it (-2 warn window)",
      bot._squareoff_at(now, -2).strftime("%H:%M") == "15:08", bot._squareoff_at(now, -2))
check("  ... label marks it as overridden", bot._squareoff_label() == "15:10*", bot._squareoff_label())

path.write_text(json.dumps({"squareoff_time": "15:40", "date": TODAY, "source": "test"}))
bot.tg_cmd_mtime = None; bot.tg_squareoff = None; bot._read_stradexit()
check("strategy INDEPENDENTLY refuses a hand-edited 15:40", bot.tg_squareoff is None, bot.tg_squareoff)
check("  ... and falls back to 15:00",
      bot._squareoff_at(now).strftime("%H:%M") == "15:00", bot._squareoff_at(now))

path.write_text(json.dumps({"squareoff_time": "15:10", "target_net": 900.0,
                            "date": "2020-01-01", "source": "test"}))
bot.tg_cmd_mtime = None; bot.tg_squareoff = (15, 10); bot.tg_target_net = 900.0
bot._read_stradexit()
check("STALE day clears the time override", bot.tg_squareoff is None, bot.tg_squareoff)
check("  ... and clears the numeric slots too", bot.tg_target_net is None, bot.tg_target_net)
check("  ... exit reverts to 15:00",
      bot._squareoff_at(now).strftime("%H:%M") == "15:00", bot._squareoff_at(now))

print("\n=== D. CONSTANTS AGREE ACROSS THE TWO PROCESSES ===")
check("LATEST matches", tbs.SQUAREOFF_LATEST == ss.SQUAREOFF_LATEST,
      f"{tbs.SQUAREOFF_LATEST} vs {ss.SQUAREOFF_LATEST}")
check("EARLIEST matches", tbs.SQUAREOFF_EARLIEST == ss.SQUAREOFF_EARLIEST,
      f"{tbs.SQUAREOFF_EARLIEST} vs {ss.SQUAREOFF_EARLIEST}")
check("default label matches strategy",
      tbs.DEFAULT_SQUAREOFF == f"{ss.SQUAREOFF_HOUR:02d}:{ss.SQUAREOFF_MINUTE:02d}",
      f"{tbs.DEFAULT_SQUAREOFF} vs {ss.SQUAREOFF_HOUR:02d}:{ss.SQUAREOFF_MINUTE:02d}")

print(f"\n  ════ {P[0]} passed · {P[1]} FAILED ════")
sys.exit(1 if P[1] else 0)
