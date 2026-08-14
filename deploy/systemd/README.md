# systemd units for the trading server (algo.oftenuncertain.net)

Reference copies of the units that drive the live trading day. They live on the server at
`/etc/systemd/system/` and were previously **only** there — meaning a rebuild would have
silently lost the entire EOD pipeline. They are checked in here so the schedule is
recoverable and reviewable, not so it can be deployed blind: **copy them to the server by
hand**, then `systemctl daemon-reload && systemctl enable --now <timer>`.

## ⚠️ One file is redacted — do not commit a key here

`openalgo-capture-trade-data.service` carries a real `OPENALGO_API_KEY` on the server. The
copy in this directory has it replaced with `__REDACTED__`. Before committing any change to
these files, re-run:

```bash
grep -rnoE "[A-Fa-f0-9]{32,}" deploy/systemd/
```

Found 2026-08-14: that key was in a **world-readable** (0644) unit *and* exposed through
`systemctl show` to any local user — and it can place real orders. SEBI static-IP whitelisting
offers no protection, because the whitelisted IP *is* this machine. Permissions were tightened
to 0640 immediately, which closes the file-read vector but **not** `systemctl show`.

**Proper fix (open TODO):** either move the key to an `EnvironmentFile=/etc/openalgo/capture.env`
at 0600 — `systemctl show` then reveals only the path — or, better, delete the env var entirely
and have `capture_trade_data.py` read the key from the DB via
`get_api_key_for_tradingview("admin")`, which is what `eod_summary.py`, `trade_journal.py` and
`archive_tradebook.py` already do. None of those needs a key in a unit file.

## The daily chain (all times IST; units pin `Asia/Kolkata`)

| Time | Unit | What it does |
|---|---|---|
| 09:05 | `openalgo-restart` | Pre-market restart. **This is what loads new strategy code.** Deliberately before the ~09:15 broker login so the WS adapter comes up authenticated. |
| 09:15 | *(APScheduler, not systemd)* | Strategy subprocesses spawn; straddle enters 09:35, exits 15:01. |
| 15:22 | `openalgo-trade-journal` | **archive_tradebook.py, then trade_journal.py.** |
| 15:31 | `openalgo-eod-summary` | TradeBhau per-strategy P&L digest → Telegram. |
| 15:35 | `openalgo-capture-trade-data` | Archives the day's option chains / index / VIX. |
| 15:45 | `openalgo-backtest-eval` | EMA eval + comparison CSV + `exit_timing_eval.py`. |

## Ordering is load-bearing — three constraints, each learned the hard way

**`archive_tradebook.py` must run ON the trading day.** The Zerodha tradebook API returns the
current day only. Once the date rolls, fill-level truth is gone permanently. A day closed by
hand writes no `[EXIT]` lines to our strategy log, so without the archive that day can never be
fill-verified — which is why `2026-08-06` sits at `low` confidence in the ledger forever, its
P&L transcribed off a screen.

**`trade_journal` must run after the strategy stops (15:20).** It parses the strategy log for
MFE/MAE and exit fills; 15:22 guarantees the log is final.

**`exit_timing_eval` must run after the 15:35 chain capture.** It replays the day off those
files. Running it at 15:30 on 2026-08-14 found nothing and silently skipped — the failure is
quiet, which is what makes it dangerous.

## Not in this directory

`openalgo.service.d/ipv4.conf` **is** here and matters: it sets `EVENTLET_NO_GREENDNS=yes`.
Without it, eventlet's greendns bypasses glibc `getaddrinfo`, ignores `/etc/gai.conf`, and
egress goes out over IPv6 — which Zerodha rejects with
`IP (2a02:...) is not allowed`, because the registered static IP is IPv4. Recreate it on any
rebuild or the broker connection fails in a way that looks like an auth problem.

Note the server shell runs **CEST**, not IST, so `systemctl list-timers` prints CEST. The units
themselves pin `Asia/Kolkata`, so they fire correctly; verify with
`systemctl show <timer> -p NextElapseUSecRealtime --value` under `TZ=Asia/Kolkata`.
