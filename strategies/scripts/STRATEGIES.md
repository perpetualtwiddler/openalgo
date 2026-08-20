# Trading Strategies — OpenAlgo Paper Trading

**Platform:** OpenAlgo on algo.oftenuncertain.net (109.123.248.99)
**Mode:** Analyzer (sandbox) with 5,00,000 INR virtual capital
**Paper trading period:** May 2026
**Broker:** Zerodha (daily auth required before 9:15 AM IST)

---

## Open TODOs / Backlog

Tracked here so nothing slips (most recent context first).

| # | Item | Priority | Notes |
|---|------|----------|-------|
| 1 | **Month-end report → Aug-1 straddle go-live decision** | HIGH (~Jul 31) | Decide straddle-only LIVE (real money, 390 qty, ~₹2L) on data: win rate, worst-day, **slippage estimate**, margin fit, + re-run the hardening scan on the full month. |
| 2 | **Live-slippage measurement** | HIGH (go-live prep) | On the first live days, log fill-price vs LTP per fill → true live edge (paper P&L is an optimistic ceiling; matters most for the thin-edge straddle). |
| 3 | **Egress firewall allowlist (server hardening)** | LOW (no urgency — audit found nothing leaking) | Restrict outbound 443 to **`api.kite.trade`, `kite.zerodha.com`, `api.telegram.org`** + OS essentials (DNS, apt/NTP); default-deny the rest. Converts "we audited and found no exfiltration" into "nothing else is *possible*", and covers the two gaps a code audit can't: (a) a future malicious/compromised **dependency** in `.venv` beaconing independently of OpenAlgo's code, (b) obfuscated exfil a keyword grep wouldn't catch. **Stage carefully — a wrong rule silently breaks order placement; test outside market hours** and re-run the zero-cost order-path test (unfillable LIMIT + cancel) afterwards to confirm the broker path still works. Note Cloudflare-fronted Zerodha IPs rotate, so allowlist by hostname/ipset with refresh, not a static IP. Audit basis: see "Security audit 2026-08-04" below. |
| 4 | **Extend option-chain capture to the NEXT weekly expiry** | MEDIUM (blocks validating the expiry-day roll) | Capture stores only the front series, so expiry-day 7-DTE trades cannot be backtested at all — `2026-08-11` has 22 files, all `NIFTY11AUG26`. Add the next weekly so a replay becomes possible; until then the roll is measured live only, via `trade_journal.csv` (`series_code`, `dte`). |
| 5 | **Populate `market_holidays[]` in `event_calendar.json`** | LOW | Deliberately left EMPTY — the dates were not verified against the NSE circular, and a wrong one would move a skip onto the wrong session. Only matters when a `next_session` roll lands beside a holiday; weekends are handled in code, and the `[EVENT]` log always prints the release date it resolved from, so a bad roll is visible. |
| 6 | **Reconcile the 2026-08-10 ₹50.45 charge gap** | LOW | Fill-derived net +₹1,981.26 vs the broker reading of +₹1,930.81 recorded that day. Not a clean ₹20 multiple, so not just fill-counting. Needs that day's contract note. |
| ~~7~~ | ~~**Schedule the EOD jobs**~~ | **DONE 2026-08-14** | `openalgo-trade-journal.timer` created (15:22 IST Mon-Fri: archive_tradebook then trade_journal), and `exit_timing_eval.py` appended to `openalgo-backtest-eval.service` (15:45, after the 15:35 capture). Test-fired: `Result: success`. Units now checked in at `deploy/systemd/` with a README — they were previously server-only, so a rebuild would have lost the whole pipeline. |
| 7c | **DEFERRED by Mandar 2026-08-19 — revisit later, not now.** Move `OPENALGO_API_KEY` out of `openalgo-capture-trade-data.service`** | **HIGH (security)** | Found 2026-08-14 while checking in the units: a **real API key** sat in a **world-readable (0644)** unit, also exposed via `systemctl show` to any local user, and it can place real orders — SEBI static-IP whitelisting is no defence since the whitelisted IP *is* this box. Tightened to 0640 immediately (closes file-read, NOT `systemctl show`). **Root cause: `setup_systemd_timers.sh` itself** — it requires `OPENALGO_API_KEY` (line 42) and writes it into the unit, so tightening the unit alone is NOT a fix: the next run of that script re-introduces the leak. Real fix: drop the var entirely and read the key from the DB via `get_api_key_for_tradingview('admin')` as eod_summary / trade_journal / archive_tradebook already do — none of them needs a key in a unit — and remove the requirement from the setup script. Interim: `EnvironmentFile=` at 0600. **Deferral note:** the risk assessment below is unchanged — the key is still readable via `systemctl show` to any local user and can place real orders. Mandar's call is that server access already implies full control (single-user, self-hosted), so this is a defence-in-depth item rather than an open door. Do not re-raise it each session; revisit when convenient or if the box ever gets another user. |
| 7b | **Collect composition-at-decision data for the theta-vs-vega exit hypothesis** | MEDIUM | See "Hypothesis: composition-aware exit" below. Needs ≥15–20 paired observations before any rule change. Cheap to gather — log the durable/vega split at a fixed checkpoint each day. |
| ~~12~~ | ~~**Move the straddle EOD square-off 15:01 → 15:00**~~ | **DONE 2026-08-19** | Mandar's call — one more minute of buffer before the 15:15 close. `SQUAREOFF_MINUTE` 1 → 0. All three coupled changes made: (a) `exit_timing_eval.py` `CANDIDATES[0]` → `15:00`, with `15:01` **kept in the list** so the nine days already traded at 15:01 stay comparable and the move can be measured rather than assumed (`--all` re-derives the new column for every captured day); (b) doc references updated, historical ones deliberately left at 15:01; (c) the minute arithmetic replaced by `_squareoff_at(now, offset)`. **The off-by-one was not what this entry predicted:** the `now.hour == SQUAREOFF_HOUR` guard meant `minute >= −2` could never leak outside hour 15, so nothing was mislabelled. The real defect was the opposite — the window could not cross an hour boundary, so the intended 14:59–15:01 warning started at 15:00 and **14:59 never qualified**. `schedule_stop` 15:20 re-verified: still 20 min after the exit. |
| 8 | **Opt1: log history-fetch failures** | LOW | Zerodha `/history` "Server disconnected" flakiness is silently skipped; add a logged warning (+ optional retry). |
| 10 | **Growth model — revisit the net-monthly-return assumption** | MEDIUM | `log/straddle-income-growth-analysis.xlsx` (generator `strategies/scripts/growth_model_xlsx.py`) hangs entirely on one input: the NET monthly return. Live data is 5 days / +₹2,702 — far too little to annualise. Revisit once ~30 live days exist, and sanity-check the 5%/yr lot-value growth against actual NIFTY margin drift. See "Investment Growth / Compounding Model" below. |
| 11 | **Ingress allowlist on `/api/v1` (Caddy)** | **HIGH (security)** | `algo.oftenuncertain.net` reverse-proxies to gunicorn with **no IP allowlist and no auth matcher**, so `/api/v1` is reachable from anywhere on the internet and the OpenAlgo API key alone authorises **real order placement**. SEBI static-IP whitelisting is NO defence — the order originates from this server, which IS the whitelisted IP (see CLAUDE.md: "attacks routed THROUGH the OpenAlgo server are still viable"). Our strategies all call `127.0.0.1` locally and Chartink has 0 configured strategies, so nothing appears to need remote access — **verify that before restricting**. Distinct from #3, which is egress. |
| 9 | **Go-strategies port decision** (openalgo-go vs manja vs keep-Python) | LOW | Draft in "Go-Based Strategies (PROPOSED)" below; no decision needed yet. |

### ⚠️ Manual-exit unwind order — buy back the SHORTS first, then sell the wings

Observed twice on 2026-08-14 during a manual Zerodha exit: **two orders were REJECTED**, both
wing SELLs, and both when attempted while the short straddle was still open.

```
5.  23950 PE SELL   rejected   <- wing sold first, shorts still open
6.  24350 PE BUY    complete   <- short closed
7.  24350 CE BUY    complete   <- short closed
8.  24750 CE SELL   rejected   <- wing again, still too early
9.  24750 CE SELL   complete   <- retried after shorts were flat
10. 23950 PE SELL   complete
```

**Why:** selling a long wing while short the ATM options destroys the hedge benefit. For a
moment the position reads as a *naked* short to Zerodha's RMS, required margin spikes past
available cash, and the order is refused. Once the shorts are bought back the wings sell
freely.

Our code already gets this right — `[EXIT]` closes CE then PE (BUY) before HEDGE CE / HEDGE PE
(SELL), the mirror of entry placing BUY hedges before SELL shorts (`options_multiorder_service.py`
orders legs 3,4 then 1,2) so there is never a transient naked-straddle margin spike. **Only the
manual path can trip over this.** Rejections are free (brokerage is per *executed* order), so the
cost is a failed click, not money — but under time pressure near the close it could mean sitting
in a position you meant to be out of.

### 💡 Hypothesis: composition-aware exit (theta vs vega) — NOT implemented, needs data

Surfaced by Mandar's 2026-08-14 manual exit, which beat every systematic alternative. The idea:
**bank early when the day's gain is mostly vega, hold when it is mostly theta.**

Rationale — the two are not equally durable. Theta is earned and permanent; vega is a
mark-to-market on an opinion and fully reversible. We can now measure the split live by
back-solving each leg's IV from its entry fill and repricing at current spot/time: the gap
between that counterfactual and actual P&L *is* the vega contribution.

Evidence from 08-14 (n=1, do not act on this yet):

| | |
|---|---|
| net at the 13:05 manual exit | **+₹1,641.73** (real fills) |
| composition at that moment | 85% vega, durable theta only ~₹269 |
| best available by holding | +₹2,038 at 13:38 (3-minute window) |
| hold to 15:01 | +₹783 |
| worst point after the exit | **−₹473 at 13:45** — a ₹2,511 swing in 7 minutes |
| breach band touched? | **No** (high 24,402 vs trigger 24,484) — the strategy's own stop would NOT have helped |

The 13:38→13:45 collapse came on a 45-point index move, far too small to explain 24 points of
straddle repricing through delta — it was an IV spike. So the risk that materialised was
precisely the one the composition metric flags.

Why this differs from the **already-rejected** time-based profit bank (hardening scan 2026-07-16,
negative over 40 days): that rule conditioned on *time*, this conditions on *composition*, which
we did not measure then. Also note my own base-rate analysis argued for holding — mean +₹259,
median +₹418 over the 14 captured days that were >+₹1,200 at 13:00 — and was wrong here, because
a P&L threshold cannot distinguish a gain built on theta from one built on rented vega.

**Before implementing:** needs ≥15–20 observations pairing composition-at-decision against
hold-to-close outcome. Until then it is one good call, not an edge. Risk of acting early is
classic overfitting to a vivid single day.

### 📊 Live status notifications — periodic + `/stradstatus` (added 2026-08-20)

Every 30 min while positioned, plus on demand. Content: NET (with gross and charges), spot vs strike and breach room, ATM IV now vs entry, the **composition split**, the projection to the square-off, and how far an armed target is *in vol terms*.

**Suppress-if-unchanged.** A push is skipped unless NET moved ≥₹400 or IV ≥0.15pp since the *last sent* one (compared against last-sent, not last-checked, so a slow drift still eventually reports). The reason is not politeness: a half-hourly heartbeat that says nothing trains you to ignore the channel, and a real breach alert then arrives into a channel you have learned to mute. `STATUS_NOTIFY_MIN=0` disables the periodic push; on-demand keeps working. **Silent on a no-trade day** — it only fires while positioned.

**On-demand is a file handshake, not a second implementation.** The bot lives in gunicorn and cannot reach the strategy subprocess's memory (same constraint as `/stradexit`). `/stradstatus` writes `log/straddle_status_request.json`; the strategy notices within one monitor pass (≤5s) and sends the message itself, so on-demand and periodic are byte-identical from one code path. Requests older than 120s are ignored, so one written while flat cannot fire against a later session. Named `/stradstatus` because `/status` and `/pnl` are already taken.

**The `← DRIVING` marker is load-bearing.** Listing theta beside vega without saying which owns the day teaches the wrong intuition: measured at 5–7 DTE, theta is ~₹67–116 per *hour* while 0.10pp of IV is ~₹195. A bare theta figure invites waiting for a clock that is nearly irrelevant. The message marks whichever leg moved more of the day's gross.

**`straddle_analytics.py` — one implementation of the greeks.** Black-Scholes and the IV bisection had already drifted (strategy 80 iterations, `status_check.py` 90) and the composition split existed *only* in `status_check`, not the strategy. A third copy for this feature was not acceptable: if an alert's greeks diverge from the exit logic's, the notification confidently reports a position the strategy does not believe it holds. The strategy's `_bs`/`_implied_vol` now delegate, signatures unchanged. `test_analytics_equiv.py` pins the refactor against a golden reference captured *before* it — projection golden point, ceiling, band and both primitives reproduce **exactly**.

Suites: `test_analytics_equiv.py` (13), `test_status_push.py` (17 — suppression, handshake replay-once, stale-request, on-demand overriding suppression, and four failure-isolation cases proving a formatting slip cannot stop the loop that enforces PT/SL/breach), `test_stradexit_time.py` (38). All green.

### ⏰ `/stradexit time HH:MM` — move today's square-off (added 2026-08-20)

`/stradexit time 15:10` · `/stradexit time default` · `/stradexit time` (report). Stored as `squareoff_time` in the same day-stamped command file, so like every other field it evaporates at midnight and **cannot leak into a later session**.

**Capped at 15:12, and the strategy re-validates independently of Telegram.** The session ends **15:15**, so an exit initiated at 15:15:00 has no market; a clean 4-leg exit measured ~7s and a transient failure needs a full retry cycle (8s × 2). We also run `PRODUCT=MIS`, so the broker has its own auto-square-off — holding to the bell hands our exit to it at market prices, and **Zerodha's current F&O MIS cut-off is still unconfirmed** (open item). The cap is enforced in BOTH places because the command file is hand-editable JSON: the process that actually places the exit has to be the one that refuses an unexecutable time.

Every exit-timing decision now routes through `self._squareoff_at()` rather than the module-level function, so the EOD exit, the 2-minute "near square-off" window and the payoff projection can never disagree about when we intend to be flat. The heartbeat/entry-alert label renders `15:10*` — the trailing asterisk means overridden for today.

**Do not read this as evidence that exiting later is better.** Paired same-day differences over the 50 replayed days: 15:05 vs 15:00 **−₹55/day** (t=−0.71), 15:10 vs 15:00 **+₹55/day** (t=+0.61), 15:14 vs 15:00 **+₹51/day** (t=+0.56) — all CIs straddle zero, better on only 28–29 of 50 days, and the worst days for holding later run **−₹1,287 to −₹1,924**. (Those are old-session days, so they don't transfer cleanly either way.) On 2026-08-20, the day that motivated the command, holding to the best post-15:00 minute (15:07) would have recovered **₹829 of an ₹1,881 loss** — real, but it would still have been a loss, not a break-even. This is a **discretionary tool for days you are reading actively**, not a default to drift toward.

Regression suite: `test_stradexit_time.py` — 38 assertions, the first block re-asserting every pre-existing `/stradexit` behaviour (numeric slots independent, `0` clears both, comma-formatted values, bare report writes nothing, non-numeric rejected) because this added new surface to a command that closes real-money positions. It also asserts the bot's and the strategy's caps agree, since they read env separately rather than importing each other.

### 🔒 Where the trade data lives — a separate LOCAL-ONLY repo (set up 2026-08-19)

**`mkds-openalgo` is a PUBLIC fork of `marketcalls/openalgo`** (verified 2026-08-19: `isPrivate:false`). The branch name `mock/strategies` reads private; the repository is not. So the live trading record is version-controlled **outside** this repo, at `../trading-ledger` — a git repo with **no remote, by design**. `ledger_snapshot.sh` refuses to run if a remote ever appears, because the point is that this data has no publication path.

What is in it, and why: `trade_journal.csv` · `exit_timing.csv` · `margin.csv` · `slippage.csv` · `trade_analytics.xlsx` · `opening_cash.txt` · `tradebook/*.json` · `strategies/*straddle*.log`.

**Two of those are irreplaceable, and one is on a clock:**
- `tradebook/*.json` — the broker tradebook API is **current-day only**. A day not archived on its own trading date is gone. (This is why 2026-08-06 is permanently `low` confidence.)
- `strategies/*straddle*.log` — **rotate off the server after ~7–10 days**, and are the only source for `mfe`/`mae` and the `[ENTRY] NIFTY spot` line. When the ledger was first created the oldest surviving log was **08-10**: the 08-06 and 08-07 logs had already aged out and are permanently lost. Do not let this go long between snapshots.

Everything else is derivable from those two given the scripts here. Refresh with `sync_from_server.sh` (server → `openalgo/log`) then `ledger_snapshot.sh` (→ ledger, and commit). Monthly is fine for the CSVs; the rotating logs are the reason not to stretch it much further.

**What stays out of this (public) repo:** `log/*.csv` is gitignored repo-wide, and `trade_analytics.xlsx` is deliberately never added — its Data sheet carries 53 columns of per-leg fills, broker margins and slippage, and its Projection corpus formula embeds the account's opening balance. `opening_cash.txt` is gitignored here for the same reason. Per-day *net* P&L already appears throughout this document, which is a pre-existing exposure Mandar is aware of. The ledger is history on one disk, **not** a backup — an off-box copy is still an open decision.

### Security audit 2026-08-04 — does OpenAlgo leak our trade/strategy data?

Asked before trusting the platform with real money. **Finding: no evidence of any data going anywhere except Zerodha (broker) and Telegram (our own TradeBhau bot, which we configured).**

- **No telemetry/analytics/error-reporting** anywhere — no Sentry / Mixpanel / PostHog / Segment / GA / Amplitude in the Python backend or `frontend/package.json`.
- **`openalgo.in` appears 61× but in ZERO executable requests** — all comments and doc links. No update / version / license phone-home either.
- **Frontend loads no external scripts** — `dist/index.html` references only local assets, no CDN at load. `cdn.plot.ly` exists in the bundle solely as the default `topojsonURL` for *geographic* maps (never rendered by our charts, and would carry no trade data regardless).
- **Strategy info is local-only** — `openalgo.db` / `strategy_configs.json`; "upload strategy" is a local file upload to our own server, not a registration with any remote service.
- **All 30+ broker hosts** exist in the repo but only the configured broker's module is loaded (Zerodha).
- **The two outbound `POST` sites are benign:** `blueprints/chartink.py` posts to `BASE_URL` = **our own** API (`HOST_SERVER`/loopback) and has 0 configured strategies; the Flow HTTP node posts to a **user-supplied** URL from `node_data` and the `flows` table doesn't exist here.
- **Runtime evidence:** a 60-second sample of all outbound sockets from the openalgo processes showed exactly one endpoint — `149.154.166.110` = `api.telegram.org`. Zerodha (`104.16.x.x`) appears only when broker calls are actually made.

**Limits of this audit (stated honestly):** static grep + a 60s runtime sample would not catch deliberately obfuscated exfiltration (encoded hostnames, DNS tunnelling); and it covered **OpenAlgo's own code, not the dependency tree** — a compromised transitive package in `.venv` could beacon independently. Those two gaps are exactly what TODO #3 (egress allowlist) closes structurally. Also note the WhatsApp feature relies on an unofficial Rust client (`wars`) — unused by us, but it would be an unaudited binary path if ever enabled.

### Recently completed

- **Pre-auth WS mitigation — RESOLVED via option (a) "log in before 09:05" (validated 2026-07-28).** The 09:05 restart previously booted *before* the ~09:15 login, so the WS broker adapter came up unauthenticated (flapping feed; on 07-27 it even killed the straddle's 09:35 entry with `Incorrect api_key`). Retiming the restart was rejected — 09:16 would land at/after the 09:15 strategy start and could interrupt a live entry. With an early login: **0 auth errors, clean feed 09:15→09:52**. Residual mid-session stalls are the separate upstream Zerodha data-API flakiness (now alerted, see below).
- **Full-history backtest CSV rebuild (2026-07-27)** — and it caught the straddle backtest engine running **stale vs live** (195 qty / no breach exit); fixed to 390 + 0.55% and cross-validated against `straddle_harden_scan.py` (identical 34 trades, BREACH:14/EOD:20). STRADDLE net 19,590 → 60,746. Produced the like-for-like table and the backtest-vs-live reconciliation.
- **Feed-stale TradeBhau alert extended to Opt1 + Regime (2026-07-30)** — the 07-24 alert work had been **straddle-only** (REST option quotes) while the EMA strategies' WS-feed guard was log-only, so the 07-28 09:52 WS stall was silent. Both now push a TradeBhau alert (onset → throttled re-alert → recovery) naming the strategy and whether a position is open/unprotected.
- **Consolidated *Utilised margin* in the straddle's per-trade notification (2026-07-30)** — the consolidated block that follows the 4 leg alerts now shows utilised margin (+ return on margin) for the whole structure, using the same defined-risk basis as the EOD digest (verified identical: ~₹71,838 on 07-24).

---

## 1. NIFTY Iron Butterfly (Short Straddle + OTM Hedge)

**File:** `short_straddle_nifty.py` (server: `short_straddle_nifty_20260507020539.py`)

### Structure
- **SELL** ATM CE + ATM PE (collect premium)
- **BUY** OTM8 CE + OTM8 PE (~400 points out, cap max loss)
- Product: MIS (intraday), auto square-off before deadline

### Position Sizing
| Parameter | Value |
|-----------|-------|
| Lot size | 65 (verified vs broker master contract 2026-07-02) |
| Lots | 6 (doubled from 3 on 2026-07-02) |
| Quantity | 390 per leg |
| Estimated margin | ~80,000-110,000 INR (2x, hedged) |

### Entry Rules
Entry at **9:35 AM IST** (delayed from 9:20 to let opening noise settle). All checks must pass in order:

| # | Filter | Default | Config Env Var |
|---|--------|---------|----------------|
| 1 | Consecutive SL cooldown (2 days) | ON | `CONSECUTIVE_SL_LIMIT=2` |
| 2 | Expiry day skip (gamma risk) | ON | `SKIP_EXPIRY_DAY=true` |
| 3 | Event calendar (RBI, FOMC, CPI) | ON | `SKIP_EVENT_DAYS=true` |
| 4 | Gap open > 1% | ON | `GAP_THRESHOLD_PCT=1.0` |
| 5 | India VIX > 25 | ON | `VIX_THRESHOLD=25.0` |
| 6 | ORB trend breakout > 0.5% | ON | `ORB_BREAKOUT_PCT=0.5` |

### Exit Rules
| Trigger | Threshold | Action |
|---------|-----------|--------|
| Profit target | +25% of net premium | Close all 4 legs |
| Short-strike breach | NIFTY moves ≥0.55% from entry ATM (directional-move cut) | Close all 4 legs |
| Stop-loss | -50% of net premium | Close all 4 legs |
| EOD square-off | 15:14 IST | Close all 4 legs (before analyzer 15:15 cutoff) |
| Position sync | Every 5 seconds | Detect manual/system exits |

At entry the log prints a **breach map** (PE-wing / breach-lo / ATM=max-profit / breach-hi / CE-wing) and every P&L heartbeat shows live `NIFTY <spot> → breach <lo>/<hi>` alongside the `[short/hedge]` split. Added 2026-07-01 (breach) / 2026-07-02 (map + display + ATM-centering fix).

### Safety Features
- **Iron butterfly hedge:** OTM8 wings (~400pts) cap maximum loss; wider wings retain more theta profit on calm days
- **Event calendar:** `event_calendar.json` with 16 confirmed high-volatility dates (Jun–Dec 2026) covering RBI MPC, FOMC, US CPI. Each entry declares `impact: same_session | next_session` — US releases land after the NSE close, so the session that reacts is the NEXT trading day (see "Event-calendar dating" below)
- **Consecutive SL cooldown:** Pauses after 2 straight stop-loss days
- **Trade history:** Last 30 trades recorded in `_history.json` for cooldown logic
- **State persistence:** JSON state file survives strategy restarts (same-day only)
- **Stale-feed guard + alert (2026-07-24):** option LTPs fetched in ONE batched `multiquotes` call with a short retry (fewer broker hits + transient-timeout resilience near the close). If quotes still stop succeeding for `FEED_STALE_SEC`, the position is effectively unprotected (PT/SL/breach on stale prices) — the guard fires a **TradeBhau alert** (`/telegram/notify`, on onset + throttled re-alerts + recovery) so you can manually **Close All**. Root cause is upstream (Zerodha data-API timeouts), so this hardens *awareness + resilience*, not prevention; the wings still cap max loss and the EOD square-off is a feed-independent clock.

### Design decisions — tested & rejected
**ER-based trend-exit — REJECTED 2026-07-03 (backtested, underperforms).**

*Idea (Mandar + Dinesh):* the straddle (short-vol) is hurt when a trend starts, while the EMA strategies profit from trends — opposite natures. Since they trade different underlyings (straddle = NIFTY, EMA = BANKNIFTY) we can't cross-trigger, so give the straddle its **own** trend detector: Kaufman **ER on NIFTY** (same metric Regime uses). A trend *starting* = **ER ≥ ~0.6** (the level at which Regime *enters*). On that signal, arm a tight profit-lock (APPE-like) — or hard-exit — to bank the good profit before the trend erodes it. *(Direction matters: HIGH ER = trend; ER < 0.4 = chop = the straddle's best zone, so exiting on low ER is backwards.)*

*Result — backtested 31 days (QTY 390, net of charges). Every ER variant lost to the breach-only baseline:*

| Config | Net | vs baseline |
|--------|----:|------------:|
| **baseline — PT25/SL50 + 0.55% breach only** | **+52,649** | — |
| ER-lock (3m ×20, trig 0.6, give-back 0.20) | +30,380 | −22,269 |
| ER-lock (3m ×10, trig 0.6, give-back 0.20) | +14,975 | −37,674 |
| ER-hard-exit (3m ×20, trig 0.6) | +31,862 | −20,787 |
| WRONG-DIR: exit at ER<0.4 | −7,138 | −59,787 |

*Why it fails:* ER≥0.6 fires far too often intraday — NIFTY nearly always has a 30–60 min efficient stretch (the 30-min ER tripped on 20 of 21 trade days). Each fire is an early exit that forfeits the slow theta decay the straddle earns into EOD. No ER setting *beats* the breach; the best it can do is approach baseline by firing rarely. The **0.55% breach is the better trend-guard** — it fires only on a real, sustained ±0.55% move, not on every efficient wiggle. (The WRONG-DIR run — exit at ER<0.4 — was worst of all, confirming that exiting on chop is exactly backwards.) **Conclusion: keep PT25/SL50 + 0.55% breach; do not add ER-protection.** Scan: `straddle_er_scan.py`.

**VIX floor, time-based profit bank, tighter entry gates — REJECTED 2026-07-16 (backtested, none beat baseline).**

*Idea (Mandar, 07-16):* harden the straddle further — raise win rate / cut loss / protect profit (APPE-analog). The gap (1.0%), opening-range-trend (ORB 0.5%) and VIX-ceiling (25) gates are **already live**, so tested only the untested levers: a **VIX floor** (skip ultra-low-VIX/thin-premium days), a **time-based profit bank** (exit if up ≥ X% after early afternoon), plus a light sweep tightening the existing gates.

*Result — backtested 40 days (QTY 390, net of charges). Every variant is a no-op or a loss vs the current baseline (+48,087, 17W/11L, worst −17,522):*

| Config | Net | vs baseline |
|--------|----:|------------:|
| **baseline (current live)** | **+48,087** | — |
| VIX floor 12 / 13 | +38,746 / +17,547 | −9,341 / −30,540 |
| bank 15% @13:00 | +43,115 | −4,972 |
| bank 20% @13:00 | +47,151 | −936 |
| tighter gap ≥0.75 | +33,626 | −14,461 |

*Why it fails:* low-VIX days are still net-**positive** (theta works even when premium is thin) — skipping them just deletes winners. The time-bank caps upside on days that would have run to PT/EOD, and no lever moves the worst day (−17,522, already a breach-cut day). Tightening the gap gate removes profitable days too. **Conclusion: the current config is already well-tuned; do not add a VIX floor, time-bank, or tighter gates.** Scan: `straddle_harden_scan.py`. *(Preliminary — 40 days, one vol regime; re-confirm at month-end.)*

### Key Files (Server)
- Strategy: `/root/data/openalgo/strategies/scripts/short_straddle_nifty_20260507020539.py`
- Event calendar: `/root/data/openalgo/strategies/scripts/event_calendar.json`
- State: `/root/data/openalgo/strategies/state/SHORT_STRADDLE_NIFTY_state.json`
- Trade history: `/root/data/openalgo/strategies/state/SHORT_STRADDLE_NIFTY_history.json`
- Logs: `/root/data/openalgo/log/strategies/short_straddle_nifty_*.log`

---

## 2. BANKNIFTY EMA(9/21) Crossover

**File:** `ema_crossover_banknifty.py` (server: `ema_crossover_banknifty_20260507020538.py`)

### Structure
- Trades **BANKNIFTY futures** (BANKNIFTY26MAY26FUT)
- Enters on EMA(9) crossing EMA(21) on **5-minute candles**
- Trades crossover **events** only (moment of crossing), not position (above/below)

### Position Sizing
| Parameter | Value |
|-----------|-------|
| Lot size | 30 |
| Quantity | 60 (2 lots) |
| Estimated margin | ~1.5–3.0 lakh INR |
| Product | MIS (intraday) |

### Entry Rules
- EMA(9) crosses above EMA(21) → **BUY**
- EMA(9) crosses below EMA(21) → **SELL**
- Volume filter: current candle volume > 1.2x SMA(20) volume
- Trades only during market hours

### Exit Rules
| Trigger | Action |
|---------|--------|
| Opposite crossover | Reverse position |
| Trailing stop-loss | Monitors via WebSocket LTP feed |
| EOD square-off | Close at 15:14 IST (before analyzer 15:15 cutoff) |
| Position sync | Detect manual exits from web UI |

### Safety Features
- **WebSocket auto-reconnect:** Outer while loop with 5-second retry on disconnect
- **State persistence:** JSON state file with position, entry price, trailing SL, peak price
- **Stale state detection:** Ignores state from previous day or different symbol
- **Volume filter:** Avoids false crossovers in low-volume periods

### Key Files (Server)
- Strategy: `/root/data/openalgo/strategies/scripts/ema_crossover_banknifty_20260507020538.py`
- State: `/root/data/openalgo/strategies/state/EMA_9_21_BANKNIFTY_state.json`
- Logs: `/root/data/openalgo/log/strategies/ema_crossover_banknifty_*.log`

### EMA Variations Under Comparison (5 options)

Section 2 above describes the **original** 5m strategy. Live-deployment history: original 5m (Opt 5)
→ **3m Option 1** on 2026-06-17 → **3m Option 4 (CURRENT ACTIVE) on 2026-06-18**
(`ema_crossover_banknifty_opt4_20260618.py`). Options 1/2/3/5 are descheduled/backtest references
(kept for rollback); they're compared on captured tick data via `backtest_ema_dev.py`. The table
lists only the differences — all 5 share the same rule framework below.

**Shared across all 5** (constant): entry = EMA cross + decisive **gap** + **close** on the signal
side of the fast EMA + **fast-EMA slope** in-direction + **volume > 1.5×SMA**; the same gate drives
reverse-exits; **APPE** profit-protect with size-aware give-back `G = 30·√peak·√(units/2)`; EOD
square-off 15:14; **daily breaker ₹5,000**; **QTY 60** (2 lots × 30); `FEED_MODE=quote`.

| # | Status | Entry TF | Trend filter | EMA | Gap gate | EMA-slope check | Volume SMA | Trailing SL | APPE arm |
|---|--------|----------|--------------|-----|----------|-----------------|------------|-------------|----------|
| **1** | was live 06-17→06-18, now descheduled | 3m | — | 9/21 | 0.01% (~6pt) | EMA9 now > EMA9 3 bars ago | SMA(10) ≈30min | **1.5×ATR(14)** | ₹4,000 (₹2k/lot) |
| 2 | backtest candidate | 2m | EMA9>EMA21 on 5m | 9/21 | 0.01% | EMA9 now > EMA9 3 bars ago | SMA(15) ≈30min | 1.5×ATR(14) | ₹4,000 (₹2k/lot) |
| 3 | backtest candidate | 2m | — | 9/21 | 0.01% | EMA9 now > EMA9 3 bars ago | SMA(15) ≈30min | 1.5×ATR(14) | ₹2,000 (₹1k/lot) |
| **4** | **🟢 LIVE — CURRENT ACTIVE (2026-06-18)** | 3m | — | **7/15** | 0.01% | EMA7 now > EMA7 3 bars ago | SMA(10) ≈30min | **1.5×ATR(14)** (pure, no floor) | ₹4,000 (₹2k/lot) |
| 5 | original (descheduled 2026-06-17) | 5m | — | 9/21 | 0.03% (~17pt) | EMA9 now > EMA9 1 bar ago | SMA(20) ≈100min | static 0.5% | ₹8,000 (₹4k/lot) |
| **R** | committed, **not yet deployed** — see §3 | 3m | **ER≥0.60/60min** *(entry trigger, replaces crossover)* | **5/13** (alignment dir only; no cross wait) | — | — | — | **ER-exit <0.40** (bar-close primary) + 0.5% backstop | **off** |

- **ATR trailing stop** (Options 1–4) = Wilder ATR(14) on the entry timeframe, distance =
  `ATR_MULT×ATR` (ratchet-only), **pure (no floor)** for all. A 100-pt floor (`ATR_FLOOR_PTS`) was
  trialled on Option 4 then **removed (2026-06-18)** — a floor sweep on the captured chop days showed
  it strictly worse (a wider stop just enlarges whipsaw losses when there's no trend to ride). The
  `ATR_FLOOR_PTS` knob is retained (default 0/off). See `ADAPTIVE_PROFIT_EXIT_DESIGN.md` §17.
- **Crossover confirmation window** — Options 1/2/3/5 enter only on the *cross candle* (decisive gap
  required at the cross). **Option 4** adds a **2-candle window** (`cross_confirm_bars=2`): a cross
  stays eligible for up to 2 candles after it, so a thin cross that turns decisive a candle or two
  later still trades — at the cost of re-admitting whipsaw risk (on the choppy 06-18 it took 2 such
  whipsaw losers while the others stayed flat). See `ADAPTIVE_PROFIT_EXIT_DESIGN.md` §18.
- **Run the comparison:** `BACKTEST_DATA_DIR=/root/data/openalgo/log/market_data_capture \`
  `.venv/bin/python strategies/scripts/backtest_ema_dev.py <YYYY-MM-DD>` → one table, all 5 rows
  (baseline immune to env overrides). ⚠ Single-day, cold-start EMAs → directional only; needs a
  trending captured day for a real verdict (the quiet 06-16/06-17 days gave ~0 trades for all).

---

## 3. BANKNIFTY EMA Trend-Regime Follower

**File:** `ema_regime_banknifty.py` (committed to `mock/strategies`; **not yet deployed to server**)
**Status:** Forward-test in Analyzer mode before live.

### Why a different approach
The EMA crossover strategy enters on the *moment* of a cross — which fires at a regime *transition* when
momentum is weakest. The regime follower waits for the regime to *confirm* itself (~45–60 min) and only
then enters in the direction the EMAs already point. This forgoes the first leg but avoids chop entirely:
no entry gate can save a bad entry — if the regime isn't established, the position will churn regardless
of the exit rule.

### Position Sizing
Identical to the EMA crossover variants.
| Parameter | Value |
|-----------|-------|
| Lot size | 30 |
| Quantity | 60 (2 lots) |
| Product | MIS (intraday) |
| Feed mode | quote (for volume capture) |

### Entry Logic
- Wait until **flat** and the trailing 60-min Efficiency Ratio (ER = \|net move\| / \|total path\| over completed 3m bars) reaches **ER ≥ 0.60**.
- On trigger: enter in the **current EMA(5/13) alignment direction** — BUY if EMA5 > EMA13, SELL otherwise. No crossover required; a cross fires before ER confirms and would give zero trades.
- Entry is deliberately lagged (~45 min after a trend starts). The regime filter skips all chop days (ER stays below gate); on trend days the position rides the bulk of the move.

| Parameter | Value | Config env var |
|-----------|-------|----------------|
| Entry timeframe | 3m candles (built from WS feed) | `CANDLE_TIMEFRAME=3m` |
| Fast EMA | 5 | `FAST_EMA=5` |
| Slow EMA | 13 | `SLOW_EMA=13` |
| ER gate | 0.60 | `ER_GATE=0.60` |
| ER window | 60 min (20 bars @ 3m) | `ER_WINDOW_MIN=60` |
| Trade direction | both | `TRADE_DIRECTION=BOTH` |

### Exit Logic
| Priority | Trigger | Default | Config env var |
|----------|---------|---------|----------------|
| 1 (primary) | **ER-exit** — at each bar-close, if rolling ER < 0.40, momentum collapsed → close at market | 0.40 | `ER_EXIT=0.40` |
| 2 (backstop) | Trailing SL 0.5% from peak (tick-driven) | 0.5% | `TRAILING_SL_PCT=0.5` |
| 3 | EMA alignment flip (reverse signal confirmed by ER gate) | — | — |
| 4 | EOD square-off | 15:14 IST | hardcoded |

APPE is **off** by default — it exits tick-by-tick on intra-trend P&L pullbacks, churning trend days.

### Backtest Results (15 days, 2026-06-09..06-25, 3m bars, 5/13 EMA, ER gate 0.60)

5 active trading days; 10 zero-trade chop days (regime gate correctly kept out).

| Exit mode | 15-day P&L | vs RIDE | Notes |
|-----------|-----------|---------|-------|
| RIDE: APPE+TSL (current crossover default) | +29,653 | baseline | APPE churns 06-12 trend |
| **ER-exit 0.40 only** | **+37,596** | **+7,943** | deployed config |
| ER-exit + APPE | +21,516 | −8,137 | APPE hurts on trend days |
| MFE-dynamic ER | +29,328 | −325 | misfires on brief adverse moves |

Worst single day: 2026-06-25 (−7,008): entry at intraday top (BUY 58,670, MFE only +5 pts). No exit rule fixes a near-zero MFE entry — the regime filter occasionally misfires.

### Key Files
- Strategy: `strategies/scripts/ema_regime_banknifty.py`
- Backtest flags: `--er-gate 0.60 --er-window-min 60 --er-exit 0.40 --tf 3 --fast 5 --slow 13`
- Context: `strategies/scripts/CONTEXT.md` (ER-exit section)
- State (when deployed): `/root/data/openalgo/strategies/state/EMA_REGIME_BANKNIFTY_state.json`

---

## Go-Based Strategies (PROPOSED — under evaluation, NOT decided)

**Status (2026-07-06): OPEN.** Whether to port **EMA Regime v1.0** + **Short Straddle v1.0** to Go is undecided — a deep analysis may well conclude we keep the working Python strategies as-is. This records the options + findings so far; to be tuned as we decide.

**Motivation:** consolidate with Dinesh's Go work + (marginal) performance. Two candidate architectures:

**A. Standalone Go (`manja`, Dinesh's current build)** — one Go binary talking *directly* to Zerodha Kite; own login, own WS feed, own systemd units.
- ✅ No Python hop; lowest latency.
- ❌ Reinvents what OpenAlgo already gives us — strategy start/stop/schedule/status, fund mgmt, reports — and duplicates infra (separate login/feed/ops).

**B. Go on the OpenAlgo platform (`openalgo-go` SDK + `execv` shim)** — the cleaner path if we go Go.
- Each Go strategy uses the **`openalgo-go` SDK** (a Go *client* for OpenAlgo's REST/WS API — `github.com/marketcalls/openalgo-go`, ~6 mo stale locally, `git pull` to update) for feed + orders → shares OpenAlgo's single broker session/feed + its order/fund/report layer.
- Managed by the `/python` host via a **~2-line Python `os.execv` shim**: the host launches `python -u shim.py`, which `execv`s the Go binary **in place** (same PID; inherits stdout/env/cwd), so start/stop/schedule/status/log-streaming/restore-on-restart all work **unchanged** — no OpenAlgo core changes. (The host hardcodes `venv/python -u <file>` at `python_strategy.py:540` and accepts only `.py`, so a bare Go binary is NOT managed without this shim.)

**Latency (the key question, settled):** in **B**, the Python OpenAlgo server IS in the I/O path (feed via WS-proxy + ZeroMQ; orders via REST) — Go does **not** remove it. But that overhead is ~single-digit ms, dwarfed by the broker round-trip, and **immaterial for these strategies** (EMA-Regime acts on 3-min bar closes; the straddle enters once at 09:35, monitors ~10 s). So latency is neither a reason to go Go nor a reason to fear it here. Only **A** removes Python from the path — solving a latency problem these strategies don't have.

**Perf reality:** for two *low-frequency* strategies Python is already fast enough; the Go win (compute/concurrency) is marginal. The real driver would be code-consolidation/stack preference — weigh against the port + integration effort.

**Prerequisites for B (confirm before committing):**
1. `openalgo-go` SDK feature-parity — must cover history, quotes, the straddle's multi-leg option order (`optionsmultiorder`), and WS feed subscription.
2. The Go binary must **log to stdout** — the host captures the subprocess's stdout for the `/python` live-log view.

**Recommendation (draft):** latency shouldn't drive this; the platform-reuse instinct (B over A) is sound; but for *these* strategies the least-effort, already-working option is to **keep the Python strategies** unless the Go consolidation benefit clearly justifies the port + shim + SDK work. **Decision pending deep analysis.**

---

## Capital Allocation

| Strategy | Allocated | Notes |
|----------|-----------|-------|
| NIFTY Iron Butterfly | ~1.0 lakh | Hedged, lower margin |
| BANKNIFTY EMA Crossover | ~3.0 lakh | Futures, higher margin |
| Reserve | ~1.0 lakh | Buffer for margin spikes |
| **Total** | **5.0 lakh** | Virtual (analyzer mode) |

---

## Trade Journal — `log/trade_journal.csv` field reference

**The durable per-day record of live trading.** One row per trading day, 52 columns, written
automatically by `openalgo-trade-journal.timer` at **15:22 IST** (archive tradebook → build row).
Generator: `strategies/scripts/trade_journal.py`.

- `--report` prints the human table · `--backfill` rebuilds every day · `--print <date>` dumps one row
- **`--stats`** is the post-market read-out (standing daily task, agreed 2026-08-17 — deliberately
  NOT scheduled; it is shared conversationally after market hours). It prints average margin
  blocked, total net, period and mean daily return on margin, best/worst day, win rate and charge
  drag — and pairs them with the **t-statistic, 95% bands and profit concentration**, because a few
  days of a short-vol strategy can look like edge purely from a short tail. A mean under ~2 standard
  errors from zero has not been measured, however good it looks.
- **The server copy is authoritative** — the strategy, the timer and the capture all run there.
  Pull it down with `strategies/scripts/sync_from_server.sh` (which also fetches `exit_timing.csv`,
  `margin.csv`, `slippage.csv` and the `tradebook/` archives). Nothing syncs automatically:
  a server cron cannot push into WSL, and a local timer would miss any day the laptop is off.
  Found 2026-08-17 — the local copy had drifted to 5 rows/48 cols while the server had 6/52.

Values in the examples below are the **real 2026-08-17 row**, so the caveats are concrete.

### Identity & context

| # | Column | Meaning | Source | Notes |
|---|---|---|---|---|
| 1 | `date` | trading date, `YYYY-MM-DD` | filename | IST |
| 2 | `weekday` | `Mon`…`Fri` | derived | |
| 3 | `series_code` | option series actually sold, e.g. `NIFTY18AUG26` | **traded symbols** | Authoritative. Not the log's "next expiry" line, which disagrees on an expiry-day roll |
| 4 | `expiry` | `18-AUG-26` | derived from `series_code` | |
| 5 | `dte` | calendar days to expiry **at entry** | derived from `series_code` | `1` on 08-17. Drives gamma/vega character — see the DTE note below |
| 6 | `lots` | lots per leg | `[INIT]` log | `2` while live-constrained |
| 7 | `qty` | contracts per leg = lots × 65 | `[INIT]` log | `130` |

### Entry conditions (why we traded, and where)

| # | Column | Meaning | Source | Notes |
|---|---|---|---|---|
| 8–10 | `orb_low` `orb_high` `orb_range` | 15-min opening range and its width | `[TREND]` log | The trend gate: entry is blocked on a >0.5% breakout |
| 11 | `spot_entry` | NIFTY at the entry decision | `[ENTRY]` log | `24289.75` |
| 12 | `atm_strike` | strike sold | entry symbols | `24300` |
| 13 | `wing_width` | points from ATM to each wing | entry symbols | `400` (OTM8) |

### Leg prices — all eight fills

| # | Column | Meaning | Source | Notes |
|---|---|---|---|---|
| 14–17 | `ce_entry` `ce_exit` `pe_entry` `pe_exit` | the two SHORT legs | slippage.csv (entry) + `[EXIT]` log, or the **tradebook archive** | |
| 18–21 | `hce_entry` `hce_exit` `hpe_entry` `hpe_exit` | the two LONG wings | same | |
| 22 | `straddle_entry` | `ce_entry + pe_entry` | computed | `128.95` — the number to watch intraday; each point ≈ ₹130 at 2 lots |
| 23 | `straddle_exit` | `ce_exit + pe_exit` | computed | `133.40` — rose, i.e. the shorts got *more* expensive |

On a **manual Zerodha exit** no `[EXIT]` lines exist, so exit prices are recovered from
`log/tradebook/<date>.json` — closing side inferred per leg (shorts close on BUY, wings on SELL),
partial fills volume-weighted. That is what keeps 08-14 at `high` confidence.

### Premium & capital — the two margin numbers are NOT interchangeable

| # | Column | Meaning | Source | Notes |
|---|---|---|---|---|
| 24 | `gross_premium` | premium taken on the short legs | log | `16,764` |
| 25 | `hedge_cost` | paid for both wings | log | `787` |
| 26 | `premium_collected` | `gross_premium − hedge_cost` | log | `15,977` — the denominator for `net_pct_of_premium` |
| 27 | `margin_blocked` | **capital the broker actually blocked**, snapshotted at entry | `margin.csv` (broker `utiliseddebits`) | `164,287.20`. Cannot be recovered later — by 15:31 the position is flat and it is back to 0 |
| 28 | `max_risk_defined` | `wing_width × qty − premium` = worst case if the index runs past a wing | computed | `36,023` |

**⚠️ These differ by ~4.6× and confusing them corrupts every return calculation.** Exchange margin
is SPAN + exposure, and exposure is ~2% of the SHORT legs' notional — it does **not** shrink with
the wings. `roi_on_margin_pct` therefore uses `margin_blocked`. The Telegram alerts got this wrong
until commit `abee981c` (2026-08-17), labelling defined risk as "utilised margin" and inflating
return-on-margin ~5× on winning days. **The journal was never affected.**

### Risk band & outcome

| # | Column | Meaning | Source | Notes |
|---|---|---|---|---|
| 29–30 | `breach_lo` `breach_hi` | `atm_strike ± 0.55%` — the directional stop | computed | `24,166 / 24,434` |
| 31 | `spot_exit` | last NIFTY seen in the log | log samples | `24,333` |
| 32 | `breached` | `Y`/`N` — did spot ever leave the band? | min/max of logged samples | `N`. From ~5s samples, so a brief intra-sample spike could be missed |

### Execution quality

| # | Column | Meaning | Source | Notes |
|---|---|---|---|---|
| 33–34 | `entry_time` `exit_time` | IST | log | `09:35:01` → `15:01:08` |
| 35 | `exit_reason` | `EOD_SQUAREOFF` · `BREACH` · `PROFIT_TARGET` · `STOPLOSS` · `TG_TARGET` · `TG_STOP` · `MANUAL_ZERODHA` · `ENTRY_PARTIAL_FAILURE` | log | |
| 36 | `n_orders` | **executed** orders — the brokerage basis | tradebook order ids | `8`. Zerodha bills ₹20 per ORDER |
| 37 | `n_fills` | fills, which can exceed orders | tradebook rows | `8` today; **11 on 08-14** (three legs partial-filled 65+65) |
| 38 | `slip_entry` | ₹ vs a **post-fill** quote | slippage.csv | `−13.0` (favourable). Reference is taken *after* the fill so it already contains our own market impact → treat as a **conservative lower bound** |
| 39 | `slip_exit` | ₹ vs the **exact decision LTP** | slippage.csv | `+97.5`. This one is exact |

`n_orders` vs `n_fills` is not cosmetic: billing fills instead of orders overstated charges by
₹23.60 on 08-13 and ₹70.80 on 08-14.

### `/stradexit` — the discretionary layer

| # | Column | Meaning | Source | Notes |
|---|---|---|---|---|
| 40 | `tg_target_net` | **last** armed take-profit (NET ₹) | `[STRADEXIT]` log | `1200`. Intermediate values (08-17: 2,000 → 1,660 → 1,300 → 1,200) live only in the strategy log |
| 41 | `tg_stop_net` | last armed stop (NET ₹) | same | blank = never armed |
| 42 | `tg_armed_at` | **first** arm of the day | same | `11:15:44` |
| 43 | `tg_fired` | `Y` fired · `N` armed but never hit · **blank = nothing armed** | same | `N`. Blank is meaningful — no discretionary call was made |

### Intraday path

| # | Column | Meaning | Source | Notes |
|---|---|---|---|---|
| 44 | `mfe` | max favourable excursion, **GROSS** | strategy's ~5s samples | `+1,268` |
| 45 | `mae` | max adverse excursion, **GROSS** | same | `−2,899` |

**⚠️ `mfe`/`mae` are gross and sample-based — do not compare them directly to `net_pnl`.** They come
from the strategy's own mark-to-market loop, not from fills, so they carry no charges and no spread.
Their value is showing what the day *offered*: 08-13 peaked at **+1,443** and closed **−416**, which
is the single strongest argument for `/stradexit` existing at all.

### P&L

| # | Column | Meaning | Source | Notes |
|---|---|---|---|---|
| 46 | `gross_pnl` | realised P&L before charges | fills | `−734.50`. **Reconciles exactly to the broker's `m2mrealized`** — verified 08-13, 08-14, 08-17 |
| 47 | `charges` | Zerodha round-trip | `charges.py`, per ORDER | `230.73`. Brokerage ₹20/order + STT 0.15% sell-side + txn 0.03553% + SEBI + stamp + 18% GST; STT and stamp round to the rupee |
| 48 | `net_pnl` | `gross_pnl − charges` | computed | `−965.23` ← **the number that matters** |
| 49 | `roi_on_margin_pct` | `net_pnl / margin_blocked × 100` | computed | `−0.588`. **The correct input for the growth model** |
| 50 | `net_pct_of_premium` | `net_pnl / premium_collected × 100` | computed | `−6.04` |

### Provenance — read this before trusting a row

| # | Column | Meaning |
|---|---|---|
| 51 | `confidence` | `high` = every fill price recovered and gross reconciles to the broker · `medium` = gross from a broker reading taken that day, fills unrecoverable · `low` = reconstructed from a screen value, treat as an estimate |
| 52 | `notes` | why a row is not `high`, and where its numbers came from |

Currently **2 of 6 rows are not fill-verified**: `2026-08-06` (`low` — manual exit before
`archive_tradebook.py` existed, so the tradebook had rolled) and `2026-08-07` (`medium` — kt-quotes
outage killed both PE legs; the log's own "Total P&L −15,840" is a 0.00-entry-price artefact and
must be ignored). Every day from 08-17 onward should be `high`, because the archiver now runs daily.

### Reading the ledger — what the columns say together

**DTE changes the whole character of a day.** Compare 08-14 (4 DTE) with 08-17 (1 DTE): premium fell
22,626 → 15,977 because there is less time value to sell, while `max_risk_defined` *rose* 29,374 →
36,023 (the wings stay 400 points out). Risk-to-premium went 1.30 → 2.25. At 1 DTE theta is fastest
but gamma is vicious — 08-17 closed just **34 points** off the strike and still lost.

**Charge drag is dominated by a fixed cost.** `charges` barely moves with premium (₹231 vs ₹250),
because ₹160 of it is 8 × ₹20 brokerage regardless of size. So drag as a share of premium worsens on
thin days: 0.99% (08-13) → 1.11% (08-14) → 1.44% (08-17). This is the strongest argument for
eventually sizing up, and it is measured rather than assumed.

**`mfe` vs `net_pnl` is the discretionary-exit scoreboard.** Where `mfe` is large and `net_pnl` small
or negative, the day *offered* money we did not take.

### Validating the data — `validate_journal.py`

Analytics built on a wrong column produce confidently wrong conclusions, so every derivable
field is re-derived from its INDEPENDENT source and compared:

```
python strategies/scripts/validate_journal.py            # all rows
python strategies/scripts/validate_journal.py 2026-08-17 # one date
```

Run it **on the server** — the strategy logs live only there, so a local run cannot check
`mfe`/`mae` and reports them as skipped. Result 2026-08-17: **105 passed · 1 mismatch · 9 skipped.**

The one mismatch is benign and expected: `2026-08-06 premium = gross − hedge` reads 29,608 in the
CSV against 29,607 recomputed. The strategy logs `Gross premium`, `Hedge cost` and
`Net premium collected` each rounded **independently** from unrounded floats, so
35,581 − 5,974 = 29,607 while the true subtraction rounds to 29,608. Both are individually
correct; a **₹1 rounding artefact**, not a data error.

Skips are honest rather than passes: no tradebook archive before 2026-08-14 (the archiver did not
exist), no monitor samples on 2026-08-07 (position closed in 3 seconds), and rotated logs on
2026-08-06/07. An unverifiable field must never look verified.

### ⚠️ Strategy logs rotate — the journal becomes the sole record

Only ~6 straddle logs survive at any time (verified 2026-08-17: 08-10 onward; 08-06 and 08-07 were
already gone). So for older days the journal is the **only** surviving source, and its log-derived
fields (`mfe`, `mae`, `orb_*`, `spot_exit`, `entry_time`, the `tg_*` set) can no longer be
re-derived or re-validated. This is a durability fact, not a bug — and it is exactly why
`archive_tradebook.py` exists for the fill-level data.

`--backfill` is safe against this: `upsert()` reads the existing CSV and merges, so a day whose log
has aged out is **preserved, not dropped**. Verified empirically — a full backfill leaves all rows
present and 08-06/08-07 byte-identical. (Do not confuse this with `exit_timing_eval.py`, whose
`append_rows(..., rebuild=True)` genuinely does rewrite from scratch.)

---

## Telegram Control Channel (TradeBhau)

**Bot:** `@TradeBhau_bot` · **Linked account:** Mandar, telegram_id `8695581038` (from @userinfobot)
Commands available: `/status /positions /holdings /funds /pnl /orderbook /tradebook /quote /chart /menu`
and the **action** ones — `/closeall` (2-step inline confirm, incl. "Close all + Stop strategies"),
`/stoppython`, `/mode`.

### How a command actually travels (e.g. `/holdings`)

```
① phone ──► Telegram servers            "/holdings"        ✗ no OpenAlgo key
② Telegram ──► our server               delivered down the long-poll getUpdates
                                        connection WE opened outbound;
                                        authenticated by the BOT TOKEN     ✗ no OpenAlgo key
③ inside gunicorn                       allowlist gate → auth gate → Fernet-decrypt key (RAM only)
④ gunicorn ──► 127.0.0.1:5000           POST /api/v1/holdings {"apikey": …}
                                        ★ the ONLY hop the API key travels — loopback,
                                          never leaves the kernel
⑤ OpenAlgo ──► Zerodha                  broker session token, not our API key
⑥ reply back up                         rendered text → Telegram → phone   ✗ no OpenAlgo key
```

**The OpenAlgo API key is never sent to Telegram.** It exists in exactly three places: Fernet-encrypted
at rest in `telegram_users` (PBKDF2-HMAC-SHA256, 100k iterations, keyed off `API_KEY_PEPPER`), decrypted
in gunicorn RAM, and on the loopback wire at ④.

**⚠️ Do NOT use `/link <api_key> <host_url>`.** That is the upstream flow, and it puts the key in a chat
message — transiting Telegram's servers and persisting in **cloud** chat history (bot chats cannot be
Secret Chats, so no E2E; Telegram holds the keys). The handler has no `delete_message`, so it stays there.
Link **server-side** instead — `/link` merely ends by calling `create_or_update_telegram_user(...)`, so a
direct call produces an identical row without the exposure. Replicate its validation: build a client, call
`funds()`, and require `status == "success"` — do NOT require non-empty data, or a valid key gets rejected
on a non-trading day.

Why `/link` exists at all: `host_url` and `encrypted_api_key` are stored **per user**, i.e. the bot is
written so different Telegram users could each point at their own OpenAlgo server. Our deployment is the
degenerate case — bot and instance in the same process, one user, host always `127.0.0.1:5000` — so the
step is redundant for us.

### Access control — `TELEGRAM_ALLOWED_IDS` (added 2026-08-15)

Upstream's only gate is *"is this telegram_id present in `telegram_users`?"* — **not** *"is it MINE?"*.
There is no allowlist and no cap on linked users, so **anyone holding a valid OpenAlgo API key could
`/link` their own Telegram account and drive this live account, including `/closeall`.** Access was
possession-of-a-secret, not identity — and that key was readable via `systemctl show` (backlog 7c).

`TELEGRAM_ALLOWED_IDS` (in `.env`, documented in `.sample.env`) pins the bot to specific numeric IDs.
Implemented as a **single global `TypeHandler` at `group=-1`**, so it runs before every handler and covers
all commands *and* inline-button callbacks from one place rather than 18 edits that could drift.
Unauthorized senders are dropped **silently** — replying would confirm to a stranger that the bot is live —
with a WARNING logged so attempts are visible. Parsing fails **OPEN** on unset/garbage input, so an `.env`
typo cannot lock the owner out.

**⚠️ `.env` is gitignored — `TELEGRAM_ALLOWED_IDS` must be re-set on any rebuild**, same class of
server-only state as the `EVENTLET_NO_GREENDNS` drop-in. Verify after restart with:
`grep -a "allowlist ACTIVE" log/openalgo_$(date +%F).log`

### What this does and does not protect

| Threat | Covered? |
|---|---|
| Someone steals the API key and links **their own** Telegram | ✅ refused by the allowlist |
| Someone controls **your** Telegram account | ❌ same identity — rely on Telegram 2FA |
| Someone uses the API key **directly** against the public API | ❌ different door — see backlog #11 |

**Trade data does reach Telegram either way.** Replies — positions, P&L, funds, orderbook, holdings — transit
and persist in Telegram cloud history. That is the deliberate exception recorded in the 04-Aug security audit;
commands widen it beyond the alert/EOD-digest baseline. The *key* is what we keep off Telegram entirely.

---

### `/stradexit` — manually armed NET P&L exit (live 2026-08-15)

Arm a take-profit and/or a stop on the running straddle from Telegram; the strategy exits all
four legs the moment it crosses.

```
/stradexit 2000     arm take-profit at NET +₹2,000
/stradexit -3000    arm stop at NET −₹3,000
/stradexit 0        clear BOTH
/stradexit          (no argument) REPORT current state — what is armed, when it was
                    armed, and whether a position is open to watch. Reporting via the
                    bare command matters because the only other way to check would be
                    to send a value, which changes the thing you are inspecting.
```

The two slots are **independent** — `+2000` then `−3000` leaves both armed; re-arming one side
leaves the other untouched. **NET**, not gross: thresholds are converted with the same
`charges.py` model the EOD digest and journal use, so "exit at +2000" means the same number
everywhere and matches the growth model, which is denominated in net.

**Day-scoped by design.** The payload carries a date; anything not today is ignored, so a
forgotten target cannot arm itself tomorrow. Extending to multi-day later is one check.

**How it crosses the process boundary.** The bot runs inside gunicorn; the straddle is a
separate subprocess. The command is a small JSON file (`log/straddle_command.json`) that the
monitor loop re-reads on its existing 5s pass — chosen over a DB row or ZeroMQ because it needs
no new dependency, survives an openalgo restart mid-session (an armed target is not lost), and
leaves an auditable artefact. Re-parsed only when mtime moves.

**Ordering.** Checked FIRST, ahead of PT / breach / SL, so it fires earliest and the exit reason
is unambiguous (`TG_TARGET` / `TG_STOP`). All existing rules remain as backstops.

**Armed before entry?** The monitor loop idles while unpositioned, so a target sent at 09:20 is
picked up on the first pass after the 09:35 fill. You get the bot's ack immediately and the
strategy's confirmation at entry.

**Fails safe.** A malformed file leaves existing rules untouched; a wrong-signed value is refused
rather than obeyed (a negative "target" would fire instantly); if charges cannot be computed the
check falls back to gross rather than silently disarming an armed stop.

Journal columns `tg_target_net`, `tg_stop_net`, `tg_armed_at`, `tg_fired` record every arming and
whether it fired — blank on days nothing was armed, which is itself meaningful (no discretionary
call made).

#### ⚠️ Evidence: do NOT use this as a standing rule

48-day sweep, modelling today's rules as the baseline (total **+34,106**, 34/48 wins, worst −5,972):

| profit target | total net | vs base | wins | | loss cap | total net | vs base | worst |
|---|---|---|---|---|---|---|---|---|
| +750 | +19,840 | **−14,266** | 42/48 | | −1,000 | +15,111 | **−18,995** | −1,890 |
| +1,500 | +24,067 | −10,038 | 36/48 | | −2,000 | +27,924 | −6,181 | −2,759 |
| +2,000 | +30,420 | −3,685 | 35/48 | | −3,000 | +27,956 | −6,150 | −3,382 |
| +2,500 | +34,176 | +71 | 35/48 | | **−4,000** | **+35,875** | **+1,769** | −4,203 |

**Profit targets buy win rate and pay for it in money.** A +₹750 trigger lifts the win rate from
71% to 87.5% while destroying 42% of total profit — textbook right-tail truncation, and the exact
trap that *feels* better while making less. **Tight loss caps are worse than they look** — −₹1,000
halves the worst day but costs ₹18,995 by cutting positions that would have recovered.

**The one evidence-supported always-on setting is a WIDE loss cap** (~−4,000): fired once in 48
days, improved the total, capped the worst day. Everything else belongs on discretion.

This also sharpens the 14-Aug lesson: that exit worked because **85% of the gain was vega**, not
because a number was hit. No fixed rupee threshold reproduces that judgement — which is precisely
why blind application loses money. See "Hypothesis: composition-aware exit" above.

*Caveat on the sweep: 1-minute bar closes (absolute levels optimistic, comparisons valid), 44 of
48 days are pre-03-Aug old-session, and it tests always-on rules — NOT the discretionary arming
this feature is built for. A re-read on a **geometric/compounded** basis is still owed: arithmetic
totals ignore volatility drag and the sequence risk that the growth model's annual withdrawals
introduce, both of which favour variance reduction.*

---

## Investment Growth / Compounding Model (capital planning)

**📄 Spreadsheet:** `log/straddle-income-growth-analysis.xlsx`
**Full path (local):** `/home/mandar/data/programs/marketcalls/openalgo/log/straddle-income-growth-analysis.xlsx`
**Generator:** `strategies/scripts/growth_model_xlsx.py` — re-run it to rebuild the file from scratch
(`python3 strategies/scripts/growth_model_xlsx.py`; env overrides `XLSX_OUT`, `XLSX_MAXY`, `XLSX_YEARS`).
**Built:** 2026-08-14. Not a forecast — a planning envelope. Read the limitations before quoting any number from it.

### What it answers

Given ₹5,00,000 deployed as whole straddle lots, how does the corpus compound over N years if a sustained
net monthly return is reinvested into additional lots — and how differently do modest differences in that
monthly return end up? Every output cell is a **live Excel formula** reading named input cells, so any input
change recomputes the whole workbook. Nothing is precomputed.

**Sheets:** `Inputs` (all yellow cells editable) · `Summary` (headline + one row per year per scenario) ·
`Engine A–D` (monthly workings, 360 rows each) · `Assumptions`.

### The deduction waterfall — this is the part people get wrong

```
deployed capital × monthly return     ← the % you enter is ALREADY NET of brokerage/STT/slippage
  − govt tax          (quarterly)
  − annual withdrawal (once a year)
  = retained, compounds into next month's deployed capital
```

Nothing else is subtracted anywhere. The Summary column is deliberately named
**"TRADING PROFIT (net of charges, pre-tax)"** rather than "gross", because "gross" would wrongly imply
transaction costs are still to come.

**⚠️ The single most abusable input.** Live experience at 2 lots has charges running **28–43% of gross**
trading profit (see the trade journal). So a raw strategy gross of 8%/month is roughly a **5%** entry here.
Typing 8% is an assertion that costs are already cleared — and the whole workbook hangs on that one number.

### Model mechanics

| Rule | Why it matters |
|---|---|
| **Returns accrue on DEPLOYED capital only** — `profit = lots × lot_value × monthly_return` | Cash insufficient for a whole extra lot sits **idle earning nothing**. This is why the corpus grows in steps, not smoothly. |
| **Reinvestment is lumpy** — `lots = INT(corpus / lot_value)`, recomputed monthly | Matches how the straddle actually scales: a lot is added only when its whole margin is available. |
| **Lot value rises yearly** — `lot_value₀ × (1+g)^(year−1)`, default **5%/yr** | Margin per lot grows with NIFTY and premium levels, so the bar for adding a lot keeps rising. Set `g=0%` to isolate this drag — it is larger than most people expect. |
| **Starting lots derived**, not entered — `INT(5,00,000 / 83,333) = 6` | Keeps the brief's ₹83,333/lot consistent with the ₹5L corpus. |

### Government tax

New regime only, **quarterly deduction** (confirmed with Mandar 2026-08-14). The supplied effective rates
already contain the 30% base slab plus surcharge and cess:

| Annual income | Surcharge | Effective (NEW) — **used** | Effective (OLD) — reference |
|---|---|---|---|
| Up to ₹50 L | 0% | **31.20%** | 31.20% |
| ₹50 L – ₹1 Cr | 10% | **34.32%** | 34.32% |
| ₹1 Cr – ₹2 Cr | 15% | **35.88%** | 35.88% |
| ₹2 Cr – ₹5 Cr | 25% | **39.00%** | 39.00% |
| Above ₹5 Cr | 25% / 37% | **39.00%** | 42.744% |

The slab is picked from **annualised** year-to-date profit (`YTD × 12 / month-in-year`), the way advance tax
is estimated in practice — so a growing book climbs into higher surcharge bands over time. Verified live:
Scenario C sits at 31.20% through month 36 and reaches 39.00% by month 72.

**Quarterly vs Annually is not cosmetic** — a toggle exists on `Inputs`. Quarterly removes money from the
corpus four times a year instead of once, so it compounds *less*. Switching to Annually flatters the result;
Quarterly is the realistic default and what ships.

### Annual withdrawal — `MIN(10% of profit, ₹1 crore)`

Actual formula (Engine column Q, month 12 of each year):

```excel
=IF($A16>NYears*12,"",
   IF($D16=12, MIN(WdrPct*IF(WdrBase="Post-tax profit",$K16-$P16,$K16), WdrCap), 0))
```

`WdrPct`=10%, `WdrCap`=₹1,00,00,000, `WdrBase` defaults to **Post-tax profit** (`$K−$P` = profit YTD minus
tax YTD) — you withdraw from what is actually yours. Both arms verified to bind: in Scenario C the 10% arm
governs years 1–8, and the **₹1 Cr cap takes over at year 9** once post-tax profit passes ₹10 Cr. In
Scenario A the cap never binds within 10 years.

**⚠️ Consequence worth understanding:** the cap is a *compounding accelerator* at the top end. In Scenario C
years 9–10 it leaves ₹6.2 Cr and ₹22.7 Cr respectively **inside** the corpus that a pure 10% rule would have
withdrawn. Much of why the high-return scenarios balloon is the cap preventing proportional drawdown exactly
when the numbers get large.

Note `MIN` vs `MAX`: "10% or ₹1 crore, whichever is less" is a **ceiling**. Had it meant a withdrawal *floor*
(`MAX`), it would pull ₹1 Cr out of a ₹5 L corpus in year 1 and destroy it. `MIN` is intended.

### Headline at defaults (Sep-2026 → 10 years, ₹5 L, lot ₹83,333 growing 5%/yr)

| Scenario | Monthly (net) | Year-10 corpus | Lots |
|---|---|---|---|
| D (custom) | 4.25% | ₹1.12 Cr | 86 |
| A | 5.00% | ₹1.91 Cr | 147 |
| B | 8.00% | ₹14.60 Cr | 1,129 |
| C | 10.00% | ₹61.73 Cr | 4,774 |

**The point of the exercise:** doubling the monthly return 5% → 10% multiplies the 10-year outcome **~32×**.
Even 4.25% → 5.00% — just 0.75 pp a month — is a **1.7×** difference over a decade.

### What this model does NOT do (read before believing the top rows)

- **No losing months.** A constant positive monthly return is a planning tool. The live straddle already has
  a losing day (13-Aug-2026, −₹686) and a worst backtested day of **−₹17,522**. Treat every figure as an
  upper envelope.
- **No liquidity or position-limit ceiling.** Scenario C's 4,774 lots is ~₹6 Cr of margin and ~3.1 lakh
  contracts — far beyond what the NIFTY chain absorbs, and slippage grows with size. The top scenarios are
  arithmetic, not plans.
- **No margin-spike buffer, no drawdown path.** Corpus is assumed fully deployable into whole lots.
- **Returns are on capital, not risk-adjusted.** Nothing here says the strategy *can* sustain the rate typed in.

### Verification (and the bug it caught)

The workbook was validated by **recalculating it** with a pure-Python Excel engine (`formulas`, in a throwaway
venv) and comparing every Summary cell against an independent Python re-implementation of the same model:
**200 value checks matched to the paisa** across all four scenarios, plus the Years guard blanking unused rows,
the headline tracking the `NYears` input, tax-slab escalation, and the withdrawal cap crossover.

**This is not ceremony — it caught a fatal defect.** The guarded formulas were initially written without a
leading `=`, so every computed cell would have opened in Excel as **literal text**: a completely dead
workbook that looked fine to static inspection. Lesson: an openpyxl-generated workbook is unverified until
something actually evaluates the formulas. openpyxl writes no cached values, so Excel/LibreOffice compute on
open — but a tool that merely *reads* the file (pandas, openpyxl itself) sees formula strings, not numbers.

---

## Deployment Workflow

1. Edit strategy files locally at `/home/mandar/data/programs/marketcalls/openalgo/strategies/scripts/`
2. Commit to `mock/strategies` branch in local git repo
3. Deploy via SCP: `scp <local_file> root@109.123.248.99:<server_path>`
4. Strategies auto-start via OpenAlgo scheduler at 9:15 AM IST daily
5. Zerodha authentication must be done manually before 9:15 AM IST each day

---

## Manual Override / Kill-Switch (live)

Exit a live position at any point-in-time from the OpenAlgo **Positions** page:
- **Close** (per row) flattens one symbol; **Close All** squares off everything. In live mode both send real MARKET square-off orders to the broker (`close_position_service.py` → `broker_module.close_all_positions`). Fills at market (small slippage).

Both strategies auto-detect a manual close via broker-positionbook reconciliation (`sync_position()` — commented *"detect manual exits via web UI"*) and reset to flat, so they never double-count. **But re-entry behaviour differs — this is the key thing:**

| Strategy | After a manual Close | Re-enters same day? |
|----------|----------------------|---------------------|
| **Straddle** | `sync` clears position; `entry_done_today` stays **True** (not reset by `_clear_position_state`) | **No** — done for the day |
| **Opt1 / EMA (& Regime)** | `sync` sets flat; the loop keeps evaluating signals | **Yes** — re-opens on the next crossover/regime signal |

**Hard override (exit AND stay out for the day):**
- **Straddle** → just **Close** it. It will not re-enter.
- **EMA (Opt1 / Regime)** → **Close the position AND Stop the strategy** (Python Strategy page → *Stop*; sets `manually_stopped`, which blocks re-entry and auto-restart). Closing alone is **not** enough — it re-arms on the next signal.

Notes: there is a few-second gap between your click and the strategy's next `sync` poll — harmless (it briefly monitors an already-flat position, then clears). `Close All` flattens all 4 straddle legs near-simultaneously.

**Reporting stays correct after a manual close (fixed 2026-07-20).** A manual Close-All / broker auto-square-off fill loses its strategy tag — the sandbox stamps it `AUTO_SQUARE_OFF` (verified live: paper Close-All produced 4 `AUTO_SQUARE_OFF`-tagged exit fills). Both the Telegram fill-alerts (`telegram_fill_subscriber`) and the EOD digest (`eod_summary`) now **re-attribute** such fills to the strategy that opened that symbol the same day (when the owner is unique) — so a manually-closed straddle still shows a correct 🔴 EXIT alert, the realized-P&L block, and one clean line in the EOD digest (not a phantom `AUTO_SQUARE_OFF` line). Caveat: BANKNIFTY-fut is shared by both EMA strategies, so a square-off there can't be uniquely split — it stays under `AUTO_SQUARE_OFF` (a non-issue for the straddle-only live plan; unique option symbols resolve cleanly).

**EXIT alerts carry realized P&L (added 2026-07-20; margin added 2026-07-30).** On the fill that fully closes a position, the EXIT alert appends a consolidated block — `Utilised margin / Gross / Charges (Zerodha) / Net / Return on margin` — EMA on each exit, straddle once on the final leg (dedup), so it summarises all 4 legs together. Charges use the same `charges.py` rate card as the EOD digest, and utilised margin uses the same defined-risk basis (iron-fly = wing width × qty − credit; futures = entry notional × 10%), so the alert and the digest always agree.

**🔴 GO-LIVE DAY 1 (2026-08-03) — two blockers, no trade, no loss.** The 09:35 entry was rejected on all 4 legs. Nothing filled, 0 open positions, ₹0 at risk; the `ENTRY_PARTIAL_FAILURE` path correctly confirmed flat. Root causes, in the order they surfaced:

1. **IPv6 egress vs the whitelisted static IP — FIXED.** Zerodha returned `403 IP (2a02:c207:2328:2993::1) is not allowed to place orders`. The server is dual-stack; the static IP registered with Zerodha (SEBI mandate) is the **IPv4** `109.123.248.99`, but outbound broker calls were leaving over IPv6. Fixing `/etc/gai.conf` (`precedence ::ffff:0:0/96 100`) was **not enough** — OpenAlgo runs under **eventlet, whose `greendns` resolver bypasses glibc `getaddrinfo` and therefore ignores gai.conf entirely**. A standalone `python`/`httpx`/`http.client` probe showed IPv4 while gunicorn still held an IPv6 socket — that discrepancy is the tell. Fix: systemd drop-in `/etc/systemd/system/openalgo.service.d/ipv4.conf` setting **`EVENTLET_NO_GREENDNS=yes`** (server-only, not in git — recreate on rebuild). Verified with `ss -tnp`: broker connections now `ESTAB 109.123.248.99 → 104.16.x.x`. *This is the same IPv6-preference trap as the earlier Caddy issue on this box.*
2. **NFO segment not activated — BLOCKING.** With the IP fixed, Zerodha returned `NFO is disabled for your account`. F&O is an account-level permission (activate at console.zerodha.com → segment activation; needs income proof, ~24–48h). Requested 2026-08-03. **No live options order is possible until this completes** — paper trading never surfaced it because it never touched the broker.

**Also learned day 1 — real margin is ~4x our estimate.** Zerodha's own `/margins/basket` (hedge benefit applied) for the 390-qty fly: **SPAN ₹1,51,449 + exposure ₹3,84,129 = ₹4,86,639** — vs the ~₹1–1.3L we had assumed from the sandbox model and our defined-risk formula (neither is exchange SPAN+exposure). **Exposure margin is ~2% of the SHORT legs' notional and does NOT shrink with the wings**, so it — not SPAN — caps our size. At ₹2.0L funded, the max is **2 lots / 130 qty (₹1,62,188)**; 3 lots needs ₹2.43L. Live therefore starts at 2 lots. Note charges/slippage don't scale down with size (brokerage is a flat ₹20/order), so at 130 qty charges are ~1.0% of premium vs ~0.5% at 390 — the small size is structurally less efficient.

**Now verified against real broker data** (superseding the earlier "still unverified" note): the live-tradebook branch of the EOD digest, the live per-trade alert path, and `charges.py` — the 2026-08-06 contract note came in at **₹284.91** vs our model's ₹285.20 unrounded, and the whole ₹0.29 gap was STT/stamp rounding, now handled. One reconciliation remains open: 2026-08-10 shows **₹50.45** between the fill-derived net (+₹1,981.26) and the broker reading taken that day (+₹1,930.81); it is not a clean multiple of ₹20, so it is not merely fill-counting. Pending that day's contract note.

**🐛 Brokerage was billed per FILL instead of per ORDER (fixed 2026-08-13).** Zerodha charges a flat **₹20 per executed order**, and a single 130-qty order can arrive as several fills. On 2026-08-13 the 24300CE exit filled as **65 + 65 under one order id**, so the broker tradebook showed 9 fills for 8 orders and `charges_from_fills()` billed ₹180 of brokerage instead of ₹160 — overstating that day's loss by **₹23.60** (₹20 + GST) and pushing the TradeBhau digest to −₹709 when the truth was −₹685.52. `charges.py::_group_orders()` now collapses fills by `orderid`, `eod_summary.py` threads the id through (it was silently dropping it), and the digest prints `8 orders / 9 fills` so a partial fill is visible rather than quietly inflating the charge line. The futures branch had a subtler version of the same flaw — `min(0.03%, ₹20)` was assessed per tranche instead of per order, which *understates* cost near the cap. This matters more as we size up: partial fills get likelier at 4–6 lots.

**📅 Event-calendar dating was wrong for every US event (fixed 2026-08-13).** A US release lands *after* the NSE close, so the Indian session that can react to it is the **next** one:

| Event | Release | IST | NSE open? | Reacting session |
|---|---|---|---|---|
| RBI MPC | ~10:00 IST | 10:00 | yes | same day |
| US CPI | 08:30 ET | 18:00 | no | **next trading day** |
| FOMC | 14:00 ET | 23:30 | no | **next trading day** |

We had been skipping the release date itself — a session that closed *before the data existed* — and then trading the session that absorbed it. 2026-08-13 is the live example: we skipped the calm 12-Aug anticipation day, traded the 13-Aug reaction, and lost ₹686 on an MFE +1,443 / MAE −2,886 chop. **12 of the 16 calendar entries were mis-dated.** Each entry now declares `impact: same_session | next_session`; `next_session` rolls forward over weekends and `market_holidays`, and a missing/misspelled value degrades to `same_session` (the old behaviour) rather than silently relocating a skip. The count of skipped days per event is **unchanged at one** — the skip moved, it did not multiply.

**⚠️ Open question: is the event gate worth keeping at all?** Replaying every US event inside the 45-day capture window says both candidate days *beat* the baseline:

| | mean net |
|---|---|
| event day (D), n=3 | **+₹700** |
| next session (D+1), n=4 incl. today | **+₹1,011** |
| all 45 captured days | +₹579 |

So the gate looks like pure cost, and the corrected version aims our one skip at the historically better day. Kept ON regardless, for two reasons: n=4 cannot detect the rare gap day that a short-vol filter exists to dodge (short-straddle P&L is left-skewed — many small wins, rare large losses), and one wing-capped loss (−₹24.7k) erases ~12 average winners. `SKIP_EVENT_DAYS=false` settles it whenever we want to test the other side; the journal now measures the cost either way.

**🔄 Expiry day now trades NEXT week's series instead of skipping (live 2026-08-13).** Weekly expiry is Tuesday, so skipping cost ~4–5 sessions/month — roughly **25% of our trade count**. We still never sell the expiring series (near-zero extrinsic, explosive gamma); `EXPIRY_DAY_USE_NEXT_WEEK=true` rolls to the next weekly (~7 DTE), which has **lower** gamma than our usual 1–5 DTE. Every other rule is unchanged and verified expiry-agnostic — in particular the breach band is `abs(spot − entry_atm) >= entry_atm × 0.55%`, a function of the entry strike and live spot only, with nothing about the series in it. `is_expiry_day()` and `get_expiry()` read one shared `_expiries()` so the gate and the order path cannot disagree (a disagreement would sell 0-DTE while the log claimed otherwise); if the broker returns no next series it skips rather than falling back. Two honest caveats: **(a) unvalidated** — our option-chain capture stores only the FRONT expiry, so there is no next-week data on any past expiry day to replay (see backlog); **(b)** `PROFIT_TARGET_PCT`/`STOPLOSS_PCT` are percentages *of premium*, and 7-DTE premium is larger, so both thresholds silently become bigger rupee numbers. Measuring live at 2 lots with the wings capping risk. First live test: **Tue 18-Aug 2026**.

**⏰ Square-off moved 15:14 → 15:01 (2026-08-10) — NSE shortened the session.** Regular trading in stocks and F&O now ends **15:15** (was 15:30), effective **2026-08-03**. The old 15:14 exit had ~16 min of headroom before a 15:30 close; against a 15:15 close it left **~50 seconds**. That is not survivable: a clean 4-leg exit already takes ~7s (measured 2026-08-10, 15:14:03→15:14:10), and a transient failure needs a full retry cycle (8s × 2). 15:01 restores ~14 min of buffer.

*Measured support (2026-08-10):* the **15:00–15:15 window was the most volatile of the session** — avg NIFTY 1m range **9.6 vs 5.8 midday** (max 18.2). Our 15:14 exit fired inside a **16.7-range minute** and paid **₹169 of exit slippage — 5× the entry's ₹32**. Same-day counterfactual: exiting ~15:00 would have netted **~₹260 more**.

*Be honest about what the history does and doesn't say.* A 45-day backfill (`exit_timing_eval.py`, 130 qty) gives 15:01 mean **+₹579** vs 15:10 **+₹686** / 15:14 **+₹675** — i.e. the old data mildly argues **against** the earlier exit on P&L (though 15:01's median is better: +₹1,030 vs +₹921). **But every one of those days is from the OLD 15:30-close session**, where 15:01–15:14 was ordinary afternoon trading rather than the closing scramble — so its timing conclusions do not transfer. (The same caveat retro-invalidates the earlier 55-day study that had favoured 15:05.) **15:01 therefore rested on the safety argument, not a P&L argument** — and so does the 15:00 that replaced it.

**⏰ Then 15:01 → 15:00 (2026-08-19), Mandar's call.** The forfeited minute is worth essentially nothing in theta (~0.1% of a session's decay at 1 DTE, less at 7) while 15:00–15:01 sits one minute deeper into the most volatile window of the session — the same window that cost ₹169 of slippage on 2026-08-10. A round `:00` also removes a class of hour-boundary bugs from minute arithmetic; `_squareoff_at()` now does real datetime arithmetic and, in the process, fixed a pre-existing one (the 2-minute "near square-off" warning never covered 14:59, because the `hour ==` guard truncated the window at the hour edge). **`15:01` stays a candidate in `exit_timing_eval.py`** — nine days were traded at it, and dropping the column would forfeit the only baseline against which this change can be judged.

*Measured, so we don't have to assume:* the paired 15:00-minus-15:01 difference over the 50 replayed days is **+₹46/day (t = 0.97, 95% CI −47 to +140)** — indistinguishable from zero, and it falls to **+₹19** once the three largest single-day swings are removed. 15:00 also shows the least-bad worst day (−6,585 vs −7,479), but that is one day, not evidence. So: **the change is P&L-neutral and rests entirely on the safety and robustness argument.** Note too that 15:10 (+662) and 15:14 (+658) still carry the highest means here — the old data continues to argue mildly against exiting early, and continues to be **old-session data** that does not transfer. Nothing in this table is a reason to move the exit later.

*How we settle it:* `exit_timing_eval.py` replays each captured day's position out to 15:00 / 15:01 / 15:05 / 15:10 / 15:14 and appends the comparison to `log/exit_timing.csv` (`--all` rebuilds, `--report` prints the verdict). Run it daily; after ~20 post-change days re-read `--report` and decide on **new-session** evidence. Bar closes aren't real fills, so absolute levels run optimistic — use it to *compare* exit times within a day, not to reconcile against broker P&L.

**Entry retry — why it exists (live day 2, 2026-08-07). A ₹3,366 lesson.** Zerodha's quote API failed mid-order (`Failed to fetch LTP for NIFTY … kt-quotes`, `underlying_ltp: None`), so 2 of the 4 legs could not even be resolved into strikes. The safety path did its job — it flattened the 2 legs that filled within ~5s, leaving 0 open (a half-built fly is a naked short CE, genuinely dangerous). But `entry_done_today` is set *before* the attempt, so the whole day was forfeited at a realised cost of **−₹248.79** (gross −104 + charges −144.79).

The true cost only became visible by measuring the counterfactual. Reconstructed from the 2 **actual fills** plus 09:35/15:14 bars for the other legs: the trade would have run to **gross +₹3,393 (+11.7% of premium), net +₹3,117** — both short legs collapsing together (CE 142→114, PE 121→105) on a textbook IV-crush day, NIFTY finishing 44 pts from the strike. **Opportunity cost ≈ ₹3,366.** The strategy's day-selection was *correct* (all gates passed, right strike, ideal outcome); only the plumbing failed, for a few seconds.

Hence `ENTRY_RETRIES` (2) / `ENTRY_RETRY_SEC` (8): re-attempt inside the 09:35–09:39 window, but **only** for clearly technical failures. `_is_transient()` is deliberately fail-safe — an unrecognised message counts as PERMANENT, because a missed day is far cheaper than re-firing orders into a real rejection (margin / freeze / not-allowed / IP-blocked). Verified against 11 cases incl. the exact 08-07 failure. Each failed attempt has already flattened, so retries start from flat. *Sizing footnote: the same misfire at 6 lots would have cost ~₹600 realised — finding this at 2 lots was cheap.*

**Slippage logging (added 2026-08-01, live-prep).** MARKET orders cross the spread on 8 option legs a day, and that drag — not any strategy tweak — is the biggest unknown in whether the ~₹1,700/day paper edge survives live. Every leg fill appends a row to `log/slippage.csv`: `date,time,strategy,phase,symbol,action,qty,fill_price,ref_price,ref_source,slip_per_unit,slip_rupees`. Cost is signed as money LOST (SELL → `ref − fill`; BUY → `fill − ref`). Two reference sources, deliberately labelled because they differ in quality:
- `decision-ltp` (**exit, exact**) — the monitor loop stops polling once `exit_in_progress` is set, so the stored LTPs are frozen at the prices the exit decision was actually made on: *"we decided to exit at X, we got Y."*
- `post-fill-quote` (**entry, lower bound**) — the leg symbols only exist after the order returns (the API resolves ATM/OTM8), so the reference is snapshotted just *after* the fills. It therefore lags submission and already contains our own market impact — treat entry slippage as conservative.

Logging is best-effort and fully guarded, and runs only *after* orders complete, so it can never delay or break an entry/exit. Analyse with `log/slippage.csv` after a few live days; a plausible half-spread (0.2–0.6/unit) already implies ~₹1,200/day, i.e. a large fraction of the modelled edge.

**⚠️ LIVE-mode notifications (reworked 2026-08-01 — required for go-live).** The enriched per-trade alerts originally hooked the EventBus topic `sandbox.order_filled`, which **only exists in analyze mode** — there is **no live fill event**, and live trades are **not persisted locally** (they live in the broker's tradebook). So in live the enriched alerts would simply never have fired. Fixes:

- **The straddle now emits its OWN consolidated alerts** (`_notify_entry` / `_notify_exit`), from the strategy process using its own fill prices and P&L — identical behaviour in live and analyze. One ENTRY message (all legs + premium + utilised margin + breach band) and one EXIT message (reason, gross / charges / net, margin, return on margin, % of premium). The sandbox fill-subscriber now **skips self-alerting strategies** (`SELF_ALERTING`) so analyze mode doesn't double-alert.
- **The EOD digest is mode-aware** — analyze reads `sandbox_trades` (per-strategy tags); live reads the **broker tradebook**. Two honest live limits: the broker tradebook has **no strategy tag** (trades are attributed by instrument — NIFTY options → straddle, the only live strategy; anything else surfaces as an explicit `LIVE untagged` line rather than being silently merged), and it only covers the **current day** (past dates can't be rebuilt).
- **Telegram Markdown**: interpolated names/reasons are sanitised (`_md`). A lone `_` — e.g. reason `EOD_SQUAREOFF` or the default `STRATEGY_NAME` — breaks Telegram's legacy-Markdown parser, which silently re-sends the message as plain text and drops all bold.
- **Still to verify on the first live day:** the broker-tradebook branch has only been exercised against the analyze-routed service (a Saturday has no live trades), so its real response shape is unconfirmed until Monday. The EMA strategies still rely on the sandbox subscriber for per-trade alerts — fine while they're stopped, but they'd need the same self-alerting treatment before either goes live.

**Feed-stale TradeBhau alerts (straddle 2026-07-24, EMA 2026-07-30).** All three strategies now push a Telegram alert when their market-data feed goes stale — on onset, throttled re-alerts (`TG_ALERT_INTERVAL`, default 120s) while still stale, and on recovery. The message names the strategy and says whether a position is open and unprotected, so the decision to hit **Close All** is yours to make in real time. Note the two feeds are independent: the straddle polls **REST option quotes**, the EMA strategies consume the **WS tick feed** — one can stall while the other is fine.

---

## Operational Timers (systemd)

**Five** systemd timers keep the trading server hands-off (all `Mon..Fri`, `Asia/Kolkata`). Reference copies of every unit are checked in at **`deploy/systemd/`** with a README — they used to exist *only* on the server, so a rebuild would have silently lost the entire schedule.

| Timer | Schedule (IST) | Purpose |
|-------|----------------|---------|
| `openalgo-restart.timer` | 09:05 | Restart openalgo pre-market (fresh scheduler + broker session). **This is what loads new strategy code.** |
| `openalgo-trade-journal.timer` | **15:22** | `archive_tradebook.py` **then** `trade_journal.py`, in ONE unit so the order cannot race (added 2026-08-14) |
| `openalgo-eod-summary.timer` | 15:31 | TradeBhau EOD per-strategy P&L digest → Telegram (`eod_summary.py`) |
| `openalgo-capture-trade-data.timer` | 15:35 | Archive the day's intraday option-chain data (`~/data/zerodha/trade-data`) for backtesting |
| `openalgo-backtest-eval.timer` | 15:45 | EMA-option rows + comparison CSVs + **`exit_timing_eval.py`** |

Post-close ordering is **load-bearing, not cosmetic** — each constraint was learned by breaking it:

1. **15:22 archive must run ON the trading day.** The Zerodha tradebook API returns the **current day only**; once the date rolls, fill-level truth is gone permanently. A day closed by hand writes no `[EXIT]` lines to our log, so without this archive it can never be fill-verified — which is why `2026-08-06` sits at `low` confidence in the ledger forever.
2. **15:22 is after the strategy's 15:20 `schedule_stop`**, so the log `trade_journal.py` parses for MFE/MAE and exit fills is final.
3. **`exit_timing_eval` must run after the 15:35 capture** it replays. Running it at 15:30 on 2026-08-14 found no files and **skipped silently** — a quiet failure, which is the dangerous kind.

**Why the 09:05 restart?** A fresh pre-market restart avoids APScheduler's `ThreadPoolExecutor` "shutdown-after-~2-days" death (scheduler logs `"all checks passed, starting"` at 09:15 IST but never spawns the subprocess; observed May 26 & 29 2026) and clears overnight drift.

**⚠️ Known issue (found 2026-07-17): the 09:05 restart lands _before_ the ~09:15 daily Zerodha login**, so openalgo's WebSocket broker adapter boots **unauthenticated** and the WS feed comes up flapping (stall → reconnect every few minutes) until a manual post-login restart. REST paths (e.g. the straddle's quote polling) are unaffected — they authenticate per call; only the WS-fed EMA strategies are hit. Daily ordering: `~08:55` feed config → `09:05` restart (pre-auth) → `~09:15` login → `09:15` strategies start.

**Decision (2026-07-20): keep the 09:05 restart.** Retiming it *after* login was rejected — it would land at/after the 09:15 strategy start and could interrupt an active EMA entry. Since "before strategies" (09:15) and "after login" (~09:15) collide, retiming can't solve it. Mitigation options (one still to be chosen): (a) **log in before 09:05** — zero-code, makes the restart boot authenticated; (b) **broker-adapter reconnect-on-auth** code fix — keeps current login time, more work, deferred; (c) status-quo — the strategies' own WS auto-reconnect self-recovers, do a manual `systemctl restart openalgo` on a bad day. Regime candle-persistence (2026-07-20) now makes such a manual restart safe — warmup/ER-window survive it.

**Setup:** `setup_systemd_timers.sh` provisions the older timers. ⚠️ **It requires `OPENALGO_API_KEY` and bakes it into a unit file — see backlog 7c; re-running it will re-leak the key** until that is fixed. `openalgo-eod-summary.timer` (2026-07-11) and `openalgo-trade-journal.timer` (2026-08-14) were added manually and are not in the script; the checked-in copies under `deploy/systemd/` are the reference.

```bash
OPENALGO_API_KEY=<key> ./strategies/scripts/setup_systemd_timers.sh
```

**Inspection commands:**

```bash
systemctl list-timers --no-pager
journalctl -u openalgo-restart.service --since today --no-pager
journalctl -u openalgo-capture-trade-data.service --since today --no-pager
systemctl start openalgo-capture-trade-data.service   # manual on-demand capture
```

Captured data lands in `/root/data/zerodha/trade-data/YYYY-MM-DD/`. Holidays produce empty/incomplete directories (script logs `no data` warnings, exits 0).

---

## Trading Results

| Date | Straddle | EMA Crossover | Notes |
|------|----------|---------------|-------|
| May 8 | Failed (bugs) | +5,262 (manual exit) | Expiry format, lot size bugs |
| May 11 | +2,798 (1 lot debug) | No trade | Debug mode test |
| May 12 | -52,065 (9 lots, SL hit) | No trade | Expiry day, PE exploded |
| May 13 | +4,621 (9 lots) | No trade | Recovered from -15k dip |
| May 14 | +39 (4 lots, OTM4) | No trade (insufficient funds) | Flat day, hedge ate 96% of profit; EMA crossover blocked by margin |
| May 15 | EXIT FAILED at 15:15 (3 lots, OTM8) | SELL filled, settled by catch-up bug | Catch-up processor killed 3/5 positions; EOD exit blocked by analyzer 15:15 cutoff; strategy hung post-market |

---

## Changelog

### May 14 — Hedge width & profit target tuning

**Problem:** OTM4 hedge wings (200 points) cost 58% of gross premium. On calm days, hedge theta decay nearly matches short leg decay, wiping out profits. May 14 backtest showed naked straddle earned +5,265 but iron butterfly only +195 (hedge absorbed 96%).

**Changes:**
| Setting | Before | After | Rationale |
|---------|--------|-------|-----------|
| Hedge offset | OTM4 (200pts) | OTM8 (400pts) | Reduces hedge cost from 58% to 31% of gross; retains more theta profit |
| Profit target | 60% of net premium | 25% of net premium | 60% almost never hit intraday; 25% is realistic theta capture |
| Lots | 4 | 3 | Free margin for EMA crossover (4 lots used ~2L, blocking BNKF futures) |

**Trade-off:** OTM8 has higher max loss (~400pts spread vs 200pts) but significantly better daily P&L on range-bound days. Stop-loss at 50% still caps adverse moves.

### May 15 — State file path fix + analyzer positionbook bug

**Bug 1 (fixed): State file path with special characters.**
OpenAlgo scheduler injects the web UI display name as `STRATEGY_NAME` env var (e.g., `EMA 9/21 Crossover - BankNifty 5min`). The `/` in `9/21` created an invalid subdirectory path, causing state save to fail silently. EMA crossover could not persist state across restarts.

**Fix:** Both strategies now sanitize `STRATEGY_NAME` → `STRATEGY_TAG` (replacing `/` and spaces with `_`) before using it in file paths. Applied to `short_straddle_nifty.py` and `ema_crossover_banknifty.py`.

**Bug 2 (fixed): Catch-up processor kills reopened positions on web UI login.**
On May 15, all 5 orders filled at 09:35 IST. Positions ran normally until 12:32 IST, when a web UI login triggered the `catch_up_processor.catch_up_mis_squareoff()`. This function settles "stale MIS" positions where `created_at < today`. However, 3 of 5 position rows were originally created on previous days and later reopened today by the execution engine. The catch-up processor only checked `created_at` (original row creation) and ignored that the positions were actively traded today.

**Impact:** 3 positions force-settled mid-day: NIFTY 23750CE (short), NIFTY 23350PE (hedge), BANKNIFTY FUT. Margin released, P&L locked at settlement LTP. EMA crossover's position sync detected the missing position and reset to FLAT. Straddle continued monitoring on internal state (unaffected operationally but 2 of 4 legs invisible in positionbook).

**Fix:** Added `SandboxPositions.updated_at < today_start` to the filter in `sandbox/catch_up_processor.py:63`. The execution engine's reopen path commits via ORM, which bumps `updated_at` to today. The daily PnL reset uses raw SQL to avoid bumping `updated_at`. So reopened positions have today's `updated_at` and are excluded from catch-up settlement. Deployed to server (takes effect on next web UI login, no restart needed).

### May 15 — EOD square-off timing + strategy termination

**Bug 3 (fixed): Analyzer blocks MIS orders at exactly 15:15 IST.**
Both strategies attempted EOD square-off at 15:15 IST, but the analyzer's MIS auto-squareoff runs at the same time and rejects new orders. The straddle's EXIT orders failed with "EXIT FAILED" and the strategy hung indefinitely post-market (still running at 22:00+ IST).

**Fix (both strategies):**
- Moved EOD square-off from 15:15 to **15:14 IST** (1 minute before the analyzer cutoff)
- Added **post-squareoff termination**: strategies now set `running = False` and exit cleanly ~5 minutes after squareoff time if no position remains
- Straddle: `SQUAREOFF_MINUTE` default changed from `"15"` to `"14"`, termination at squareoff+5
- EMA crossover: EOD check changed from `>= 15` to `>= 14`, termination at minute >= 19

---

## Event Calendar Dates (2026)

Straddle skips entry on these dates:

| Date | Event |
|------|-------|
| Jun 5 | RBI MPC announcement |
| Jun 10 | US CPI release |
| Jun 17 | FOMC rate decision |
| Jul 14 | US CPI release |
| Jul 29 | FOMC rate decision |
| Aug 5 | RBI MPC announcement |
| Aug 12 | US CPI release |
| Sep 11 | US CPI release |
| Sep 16 | FOMC rate decision |
| Oct 7 | RBI MPC announcement |
| Oct 14 | US CPI release |
| Oct 28 | FOMC rate decision |
| Nov 10 | US CPI release |
| Dec 4 | RBI MPC announcement |
| Dec 9 | FOMC rate decision |
| Dec 10 | US CPI release |
