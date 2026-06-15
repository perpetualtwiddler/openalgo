# Adaptive Profit-Protection Exit (APPE) — Design Document

**Status:** DRAFT for review — not yet implemented
**Author:** Mandar + Claude
**Date:** 2026-05-31
**Applies to:** `ema_crossover_banknifty.py` first; `short_straddle_nifty.py` later
**Related:** [STRATEGIES.md](STRATEGIES.md)

---

## 1. Motivation — the May 29 EMA trade

| Event | Price | Unrealized P&L (60 qty) |
|-------|-------|--------------------------|
| Entry (SELL) | 55,150.00 | 0 |
| Peak (price low ~12:39) | 54,757.20 | **+23,568 (MFE)** |
| Trailing-SL exit (14:46) | 55,044.00 | **+6,360 (kept)** |
| **Given back** | | **−17,208 (73% of peak)** |

We captured only **27% of the maximum profit the trade ever showed**. The position made
+23.5K, then sine-waved its way down (lower highs, not a straight drop) over ~2 hours and we
exited near the bottom of that retrace.

### Root cause — the current trail is on PRICE, not on P&L

The live exit is a **0.5% trailing stop on the instrument price** ([ema_crossover_banknifty.py:180](ema_crossover_banknifty.py#L180)):

```
SELL:  trailing_sl = peak_low_price × (1 + 0.5/100)
exit when LTP ≥ trailing_sl
```

On BANKNIFTY at ~55,000, **0.5% = 275 points = ₹16,500** of give-back room for a 60-qty
position — and that room is **constant regardless of how much profit is on the table**. Whether
the trade is up ₹2K or up ₹23K, the price trail allows the same ~₹16.5K retrace before exiting.
That is the bug we are fixing: **the give-back tolerance should be a function of the profit
already earned, not a fixed % of the instrument price.**

---

## 2. Goal and honest framing

**Goal:** Capture a *high fraction* of the peak unrealized profit (MFE) with *high probability*,
by exiting when the profit curve has *confirmed* it is rolling over — while still riding through
normal up-down noise when the trade is fundamentally still climbing.

**What this is NOT:** We are **not** trying to exit at the exact top. That is impossible in real
time — see §3. We deliberately trade a little upside for a lot of downside protection.

> **Mandar's question:** *"We don't know if 23K was the best profit. Can't be guessed manually or
> automatically at that point. Correct?"*
> **Answer: Correct.** Identifying the global maximum of a live, noisy series without lookahead is
> provably impossible (it is an *optimal stopping* problem — see §3). So we don't chase the top; we
> protect a large, **already-earned** fraction of it.

---

## 3. This is a known problem — established math

We are not the first. The relevant, battle-tested concepts (trading and beyond):

| Concept | Field | What we borrow |
|---------|-------|----------------|
| **Maximum Favorable Excursion (MFE)** | Trading (J. Sweeney) | `P_max` = the peak unrealized profit a trade ever showed. Exit rules of the form "give back at most X% of MFE" are standard. |
| **Drawdown from peak** | Portfolio mgmt | `D(t) = P_max − U(t)` — our core trigger variable. |
| **Chandelier Exit** | Trading (C. LeBeau) | Trail from the peak by a *volatility-scaled* distance, not a fixed %. Inspires scaling the give-back. |
| **Ratchet / give-back stop** | Trend-following | Lock in a growing fraction of profit as the peak climbs. |
| **Early-stopping "patience"** | Machine learning | Monitor a noisy improving metric; keep `best`; stop when no new best for `patience` steps **and** the trend is down. **This is exactly our problem** — `P_max` = `best`, our confirm window = `patience`. |
| **CUSUM change-point detection** | Statistics (Page, 1954) | The canonical test for "has an up-drifting process flipped to down-drifting?" Strong candidate for the trend gate (v2). |
| **Optimal stopping / secretary problem** | Probability | Formalizes why you cannot reliably catch the global max online. Sets honest expectations. |

The design below is essentially **MFE give-back (ratchet) + a trend-confirmation gate** — the
trading-specific combination of the rows above, expressed on the **P&L curve** instead of price.

---

## 4. The algorithm

We monitor the **unrealized P&L curve** `U(t)` of the open position (we already compute this every
tick). Define:

```
U(t)      = current unrealized P&L (₹)
P_max(t)  = running max of U up to now          # the MFE
D(t)      = P_max(t) − U(t)                      # give-back / drawdown from peak
```

Three gates, evaluated in order. **All required** to exit via APPE:

### Gate 1 — Arming threshold (have we earned enough to start protecting?)

```
armed = (P_max ≥ A)          # A = PROFIT_ARM_THRESHOLD, default ₹10,000
```

Below `A`, APPE does nothing — the existing exits (price trailing-SL, reverse signal, EOD) govern.
This stops us choking off young trades that haven't developed. Mandar's "watch starts at 10K."

### Gate 2 — Give-back budget exceeded (the ratchet)

Exit candidate when the position has surrendered more than its **peak-scaled** budget:

```
D(t) ≥ G(P_max)
```

where `G` is the allowed give-back. We want **two simultaneous properties**:
- allow *more absolute* give-back at higher peaks (don't exit on small wiggles when up ₹30K), **but**
- protect a *larger fraction* at higher peaks (₹30K is rare and precious).

A **concave** budget delivers both. Three candidate forms (pick one during review):

| Form | Formula | Give-back @10K / @23.5K / @30K | Fraction kept @23.5K |
|------|---------|-------------------------------|----------------------|
| **(a) Linear** | `G = base + s·(P_max − A)`, base=3000, s=0.30 | 3,000 / 7,074 / 9,000 | 70% |
| **(b) Square-root** | `G = k·√P_max`, k=30 | 3,000 / 4,607 / 5,196 | 80% |
| **(c) Tiered %** | lock 60/70/80% in bands [A,2A),[2A,3A),[3A,∞) | 4,000 / 7,068 / 6,000 | 70% |

All three put the May 29 exit in the **₹16.5K–19K** zone — matching Mandar's "could have exited at
17–18K" intuition. **Square-root (b) is the most principled** (smoothly protects a rising fraction,
no tier discontinuities) and is the recommended default, but it is one config flip to switch.

**Size-aware refinement (units scaling, 2026-06-12).** `G = k·√P_max` is in *rupees* and ignores
position size, so the give-back room *in points* (`G / QTY`) shrinks as units grow — at a fixed rupee
peak a 4-unit (120-qty) book would exit on a ~25-pt wiggle vs ~50 pts for 2 units. Give-back is
fundamentally a *price-volatility (points)* property, so scale it **sub-linearly** with units:

```
G = k · √P_max · √(units / UNITS_REF)   |   units = QUANTITY / LOT_SIZE,  k = 30,  UNITS_REF = 2
```

`UNITS_REF = 2` anchors to the current 60-qty calibration (factor = 1 → the §5 table is unchanged).
This is exactly equivalent to a **points-native** budget `G_pts = k_pts·√(P_max/QTY)` with
`k_pts = 30/√60 ≈ 3.873` — i.e. the give-back *in points* depends only on **how far price ran, not on
how many units are held** (size-correct), and the rupee value then scales linearly with QTY. Doubling
units lifts G by only **×√2** (4 units → ×1.41), never ×2. The arm threshold already scales *linearly*
with units (₹4,000 × units, §4 Gate 1); the give-back scales by **√units** — the two are deliberately
different (arm = "earned enough to protect", a rupee bar; give-back = "noise tolerance", a points/vol
property).

### Gate 3 — Trend confirmation (is it *genuinely* rolling over, not just one dip?)

This is Mandar's key point: *"when profit is trending down, do NOT exit immediately — confirm it."*
We require the profit curve to be **actually drifting down**, not just momentarily dipping. Two
sub-mechanisms, used together:

**3a. Smoothed slope gate.** Maintain a short EMA of `U(t)` and require its slope over the last
`W` seconds to be negative:

```
slope = (EMA_U(now) − EMA_U(now − W)) / W
trend_down = slope < 0           # W = TREND_WINDOW_SEC, default 180s (3 min)
```

If the smoothed curve is still rising (the bumpy-but-upward sine wave), suppress the exit even if a
single tick breached the give-back budget.

**3b. Confirm-and-hold (patience).** Once Gates 2 + 3a both hold, start a timer. Only fire the exit
if the breach persists for `H` seconds (the "patience" window). If `U` recovers back above the
floor `P_max − G` within `H`, cancel and reset. Rides through a single spike.

```
if (D ≥ G(P_max)) and trend_down:
        start/continue breach timer
        if breach_held ≥ H:  →  EXIT (reason = APPE_RATCHET)
else:
        reset breach timer
```

`H = TREND_CONFIRM_SEC`, default 30s.

### Gate 4 — Catastrophic override (bypass confirmation on a violent reversal)

If the give-back is *extreme* (a fast V-reversal), don't wait for slope/patience — exit now:

```
if D(t) ≥ G(P_max) × HARD_MULT:   →  EXIT immediately (reason = APPE_HARD)
HARD_MULT default = 2.0
```

---

## 5. Worked examples across scenarios

All examples use the recommended defaults: **size-aware square-root budget**
`G = 30·√P_max·√(units/2)`, arm `A = ₹4,000 × units`, trend window `W=180s`, patience `H=30s`,
hard multiple `2.0`.

**Reference (a) — at the current 2 units (60 qty)**: factor `√(2/2)=1`, so `G = 30·√P_max` exactly
(this table is unchanged by the size-aware refinement):

| `P_max` (peak ₹) | `G = 30·√P_max` | `floor` (exit if U drops below, when confirmed) | fraction kept |
|------------------|------------------|--------------------------------------------------|---------------|
| 10,000 | 3,000 | 7,000 | 70% |
| 12,000 | 3,286 | 8,714 | 73% |
| 16,000 | 3,795 | 12,205 | 76% |
| 20,000 | 4,243 | 15,757 | 79% |
| 23,568 | 4,606 | 18,962 | 80% |
| 30,000 | 5,196 | 24,804 | 83% |

Floor protects a **rising fraction** as the peak climbs (70% → 83%) — the "10K vs 30K peak should
differ" requirement.

**Reference (b) — units scaling at a fixed P_max = ₹10,000** (G lifts by `√(units/2)`, sub-linear):

| units (qty) | factor `√(units/2)` | `G` (₹) | give-back in **points** (`G/QTY`) |
|-------------|---------------------|---------|-----------------------------------|
| 1 (30) | 0.71 | 2,121 | 70.7 |
| 2 (60) | 1.00 | 3,000 | 50.0 |
| 4 (120) | 1.41 | 4,243 | 35.4 |
| 6 (180) | 1.73 | 5,196 | 28.9 |

(Linear scaling would give 4 units G=6,000; √-scaling gives 4,243 — more room than the
size-blind 3,000, but not double.)

**Reference (c) — points-native (size-independent)**: give-back depends only on the **price
excursion** `E` (pts), via `G_pts = 3.873·√E`; rupees = `G_pts × QTY`. Same numbers as (a)/(b), just
expressed in points so the size cancels out:

| peak excursion `E` (pts) | `G_pts = 3.873·√E` | `floor` (pts) | fraction kept |
|--------------------------|--------------------|---------------|---------------|
| 100 | 38.7 | 61.3 | 61% |
| 167 | 50.0 | 117 | 70% |
| 200 | 54.8 | 145 | 73% |
| 300 | 67.1 | 233 | 78% |
| 400 | 77.5 | 322 | 81% |

(`E=167 pts × 60 qty = ₹10,020 ≈` the ₹10,000 row of table (a) — they're the same budget, one in
rupees, one in points.)

---

### Scenario A — the big rollover (May 29 replayed) → APPE EXITS, big win

The actual trade. `U` = unrealized profit; columns show each gate's state.

| Time | U (₹) | P_max | D=give-back | floor | slope | breach timer | decision |
|------|-------|-------|-------------|-------|-------|--------------|----------|
| 11:50 | 9,800 | 9,800 | — | — | + | — | not armed (P_max<10K) |
| 12:10 | 14,200 | 14,200 | 0 | 10,650 | + | — | armed; U≫floor, hold |
| 12:39 | **23,568** | 23,568 | 0 | 18,962 | + | — | new peak; hold |
| 13:20 | 20,400 | 23,568 | 3,168 | 18,962 | − | — | above floor, hold |
| 13:45 | 18,700 | 23,568 | 4,868 | 18,962 | − | **start (0s)** | breach + slope↓ → arm timer |
| 13:45:20 | 19,400 | 23,568 | 4,168 | 18,962 | − | **cancel** | recovered above floor — was a wiggle |
| 14:05 | 18,400 | 23,568 | 5,168 | 18,962 | − | **start (0s)** | breach again |
| 14:05:30 | 18,500 | 23,568 | 5,068 | 18,962 | − | **held 30s** | **→ EXIT @ ~₹18.5K (APPE_RATCHET)** |

**Capture ≈ ₹18.5K vs ₹6.36K actually kept — ~₹12K better.** Note row 13:45: the first dip below the
floor was a *wiggle* — the patience timer canceled when U bounced back. The second breach (14:05)
held the full 30s → real exit. This is Gates 2+3 working together.

---

### Scenario B — bumpy-but-climbing → APPE correctly HOLDS (the key behaviour)

This is the case Mandar stressed: profit dips repeatedly but is *fundamentally still climbing*.
APPE must **not** panic-exit. A naive "exit if profit drops ₹2K from peak" would bail at 11:10 and
miss the run.

| Time | U (₹) | P_max | D | floor | breach? | decision |
|------|-------|-------|---|-------|---------|----------|
| 11:00 | 10,000 | 10,000 | 0 | 7,000 | no | armed |
| 11:05 | 12,000 | 12,000 | 0 | 8,714 | no | new peak |
| 11:10 | 10,800 | 12,000 | 1,200 | 8,714 | **no** (D<G, U>floor) | hold — dip absorbed |
| 11:15 | 14,000 | 14,000 | 0 | 10,450 | no | new peak |
| 11:20 | 11,600 | 14,000 | 2,400 | 10,450 | **no** | hold — still above floor |
| 11:30 | 16,500 | 16,500 | 0 | 12,705 | no | new peak |
| 11:45 | 19,000 | 19,000 | 0 | 15,180 | no | climbing |
| … | → eventually rolls over and exits via Scenario-A logic at a much higher floor |

**The point:** every dip stayed *above the peak-scaled floor*, so APPE rode the whole climb. The
ratchet only tightens as the peak rises — it never forces an exit just because profit oscillates.

---

### Scenario C — small trade, never arms → APPE stays DORMANT

| Time | U (₹) | P_max | armed? | decision |
|------|-------|-------|--------|----------|
| 10:20 | 4,000 | 4,000 | no | APPE dormant |
| 10:40 | 7,200 | 7,200 | no | APPE dormant (peak < ₹10K) |
| 11:10 | 3,500 | 7,200 | no | still dormant |
| 11:30 | — | — | — | exits via **existing** 0.5% price-trail / reverse-signal / EOD |

Below the ₹10K arm threshold APPE does nothing — the current exits govern, exactly as today. This
prevents APPE from meddling with small/young trades.

---

### Scenario D — violent V-reversal → HARD OVERRIDE fires (skips patience)

A fast crash where waiting the 30s patience would surrender too much. Hard floor =
`P_max − 2·G = 20,000 − 8,486 = ₹11,514`.

| Time | U (₹) | P_max | D | floor (1×G) | hard floor (2×G) | breach timer | decision |
|------|-------|-------|---|-------------|------------------|--------------|----------|
| 13:00 | 20,000 | 20,000 | 0 | 15,757 | 11,514 | — | armed, peak |
| 13:00:20 | 14,000 | 20,000 | 6,000 | 15,757 | 11,514 | **start (0s)** | normal breach (G < D < 2·G) → arm the 30s patience timer |
| 13:00:40 | 10,800 | 20,000 | 9,200 | 15,757 | 11,514 | running (~20s) → **pre-empted** | `D ≥ 2·G` → **Gate 4 EXIT NOW (APPE_HARD)** |

The "start timer" at 13:00:20 is the **same Gate 3b patience timer** as in Scenarios A and E — at that
instant it was a normal breach (`D` between `G` and `2·G`), so APPE began the 30s confirm wait. But
20s later the collapse deepened past `2·G`, so the **hard override (Gate 4) pre-empted the still-running
timer** and exited immediately at ~₹10.8K rather than sitting through the remaining ~10s while the
position bled further. That short-circuit is the entire purpose of Gate 4.

---

### Scenario E — low peak just above arm → small budget, still protects ~70%

Shows the budget scales sensibly at the *bottom* of the armed range.

| Time | U (₹) | P_max | D | floor | hard floor (2×G) | breach timer | decision |
|------|-------|-------|---|-------|------------------|--------------|----------|
| 14:30 | 10,500 | 10,500 | 0 | 7,431 | 4,362 | — | armed (G=3,069) |
| 14:45 | 8,000 | 10,500 | 2,500 | 7,431 | 4,362 | — | hold (above floor) |
| 14:55:00 | 7,200 | 10,500 | 3,300 | 7,431 | 4,362 | **start (0s)** | breach + slope↓ → arm timer (D<2·G, so **not** a hard exit) |
| 14:55:30 | 7,150 | 10,500 | 3,350 | 7,431 | 4,362 | **held 30s** | **→ EXIT @ ~₹7.2K (APPE_RATCHET)** |

**It is NOT an immediate exit.** `D=3,300` exceeds the budget `G=3,069` (Gate 2) but is far below the
hard floor `2·G=6,138` (Gate 4), so the normal **30s patience window applies** — identical to
Scenario A's real breach at 14:05. Only Scenario D (where `D ≥ 2·G`) skips the wait. Net result: a
modest ₹10.5K peak is still protected at ~₹7.4K (70%) rather than round-tripping to zero.

---

> ⚠ All five are illustrative hand-replays to show gate behaviour. Real intra-minute wiggle is finer
> than our 1-min captured data, and one good case is not evidence. Everything must be validated
> across the full archive (May 14, 15, 25, 26, 27, 29) before trusting — see §8.

---

## 6. Parameters (proposed env vars)

| Env var | Default | Meaning |
|---------|---------|---------|
| `PROFIT_ARM_THRESHOLD` | `10000` | Gate 1 — ₹ profit before APPE activates |
| `GIVEBACK_MODE` | `sqrt` | `sqrt` \| `linear` \| `tiered` |
| `GIVEBACK_K` | `30` | √-mode coefficient (`G = k·√P_max`) |
| `GIVEBACK_BASE` / `GIVEBACK_SLOPE` | `3000` / `0.30` | linear-mode params |
| `TREND_WINDOW_SEC` | `180` | Gate 3a — slope lookback |
| `TREND_CONFIRM_SEC` | `30` | Gate 3b — breach hold ("patience") |
| `HARD_MULT` | `2.0` | Gate 4 — catastrophic give-back multiple |
| `APPE_ENABLED` | `true` | master on/off (fall back to current price-trail) |

All tunable without code change, consistent with how the strategies are already configured.

---

## 7. Interaction with existing exits

APPE is an **additional, profit-side** exit. It does **not** replace:

- **Price trailing-SL (0.5%)** — still active. Below the arm threshold it is the only profit
  protection. Above it, APPE's floor will almost always be tighter (kicks in first). Keep both;
  whichever triggers first wins.
- **Reverse-signal exit** — unchanged (EMA flips → close+reverse).
- **EOD 15:14 square-off** — unchanged.
- **Daily loss limit (₹5,000)** — unchanged (and see §9 — APPE must use the *snapshot* P&L, not the
  sync-corrupted `daily_pnl`, per the May 29 race-condition fix).

### Current exit criteria — both strategies (for reference)

**EMA Crossover** ([ema_crossover_banknifty.py](ema_crossover_banknifty.py)):
| Exit | Trigger | Type |
|------|---------|------|
| Trailing SL | LTP retraces 0.5% from peak **price** | profit-protect (price-based) |
| Reverse signal | opposite EMA(9/21) crossover + volume | signal |
| Daily loss limit | cumulative day P&L ≤ −₹5,000 | risk (blocks new trades) |
| EOD | 15:14 IST | time |

**Short Straddle** ([short_straddle_nifty.py](short_straddle_nifty.py)):
| Exit | Trigger | Type |
|------|---------|------|
| Profit target | net P&L ≥ **+25%** of net premium | fixed profit |
| Stop-loss | net P&L ≤ **−50%** of net premium | fixed risk |
| EOD | 15:14 IST | time |
| Position sync | legs vanish from positionbook | reconciliation |

Note the straddle uses a **fixed % profit target** — it would also benefit from APPE-style ratcheting
(it left money on calm days, took full loss on trend days). Deferred — see §11.

---

## 8. Validation plan (before live)

1. Extend `backtest_offline.py` with an APPE simulator that replays `U(t)` from the captured 1-min
   (ideally tick) data and reports, per day: MFE, actual-exit P&L, APPE-exit P&L, and capture %.
2. Run across **all** archived days, not just May 29. Report aggregate capture % and worst-case
   "APPE exited too early" days (where it gave up a subsequent higher peak).
3. Grid-search `A`, `k`/mode, `W`, `H` for the best *risk-adjusted* capture (not just raw ₹ — also
   count how often APPE turned a winner into a smaller winner that then would have grown).
4. Only deploy params that beat the current 0.5% price-trail on the archive **and** are robust to
   ±20% perturbation (no overfit).

⚠ **1-min candle data understates intra-minute wiggle.** APPE reacts to sub-minute moves live.
Backtest on captured data is directional guidance, not exact — flag this in results.

---

## 9. Implementation notes / gotchas

- **Use snapshot P&L, not shared state.** Per the May 29 sync race ([STRATEGIES.md](STRATEGIES.md)
  changelog), the exit path must compute `U` from a snapshot of `entry_price`, never read
  mid-flight values the sync thread may zero out.
- **Single exit guard.** APPE must respect `exit_in_progress` so it can't double-fire with the
  price-trail or reverse-signal exit.
- **State persistence.** `P_max`, arm status, and breach-timer state should survive a strategy
  restart within the same day (write to the JSON state file) so a 08:00-restart / mid-day stop-start
  doesn't reset protection on an open position.
- **Compute cost.** EMA-of-P&L and a rolling slope are O(1) per tick — negligible.

---

## 10. Adaptive Loss Exit (ALE) — loss-side companion

APPE protects *profit*. ALE is its mirror on the *loss* side. **Status: design only — to be built
after APPE is proven live.** Captured here so it isn't lost.

### 10.1 The gap it closes

Investigation on 2026-05-31 confirmed: **there is no per-trade rupee stop-loss today.** The EMA
strategy's `MAX_LOSS_PER_DAY` (₹5,000) is a **cumulative-realized circuit breaker**, NOT a per-position
stop. It sums P&L from *closed* trades (`daily_pnl += pnl` in `place_exit`) and (a) blocks new entries
([place_entry:302](ema_crossover_banknifty.py#L302)) and (b) closes an open position only *after*
realized losses already crossed −₹5K ([strategy loop:444](ema_crossover_banknifty.py#L444)). It does
**not** watch an open position's *unrealized* P&L.

Consequence: a single open trade can run to −₹8K / −₹10K unrealized and nothing rupee-denominated
stops it. The only implicit stop is the 0.5% price trailing-SL — which at entry sits 0.5% adverse =
~275 pts on BANKNIFTY = **~₹16.5K** before it fires. (This same gap is why the May 29 race froze the
day: the bogus −3,302,640 hit `daily_pnl`, tripping the circuit breaker despite the real +₹6,360.)

### 10.2 Design principle — the loss side must be FASTER than the profit side

A deliberate asymmetry with APPE:

| | Profit side (APPE) | Loss side (ALE) |
|---|---|---|
| Risk of acting too soon | give up further upside | (minor) cut a trade that might recover |
| Risk of acting too late | give back earned profit | **loss deepens — expensive** |
| Therefore | patience is good (ride wiggles) | **cut fast; patience is dangerous** |

So ALE's backstop is **immediate, no patience window**. Trend-confirmation, if used, may only make it
exit *earlier* than the hard stop — never *delay* past it.

### 10.3 The controls

**(1) Hard per-trade stop — `MAX_LOSS_PER_TRADE` (primary, build first).**
A configurable rupee cap on the open position's unrealized loss. Immediate exit, no confirmation —
mirrors APPE's Gate 4, not its Gate 3.

```
if unrealized_pnl <= -MAX_LOSS_PER_TRADE:   →  EXIT now (reason = MAX_LOSS_PER_TRADE)
```

**(2) Trend-aware early cut (optional, add later).**
For losses approaching but below the hard stop, reuse APPE's slope machinery to cut *earlier*:

```
alert = -LOSS_ALERT  (mirror of arm threshold, e.g. -3,000)
if loss ≥ alert AND slope confirmed adverse (held H sec):  →  EXIT early (~-3K to -4K)
elif loss ≥ alert AND slope recovering:                    →  HOLD (dip may recover)
# the hard stop (1) remains the non-negotiable backstop regardless
```

### 10.4 Worked example — BANKNIFTY SELL @ 55,150, `MAX_LOSS_PER_TRADE = 6,000`

`6,000 / 60 qty = 100 pts` → hard stop at price 55,250.

| Protection | Fires at (price) | Loss at exit |
|------------|------------------|--------------|
| Current implicit 0.5% trail (at entry) | 55,425 (+275 pts) | **−₹16,545** |
| Proposed `MAX_LOSS_PER_TRADE` ₹6K | 55,250 (+100 pts) | **−₹6,000** |

The rupee stop fires at 100 pts instead of 275 — cutting the loss at ₹6K instead of ₹16.5K. That delta
is the gap quantified.

### 10.5 Interaction & coherence

| Control | Scope | Fires |
|---------|-------|-------|
| `MAX_LOSS_PER_TRADE` (new) | single open trade, unrealized | immediate at −₹6K |
| Adaptive early-cut (new, optional) | single open trade, unrealized | ~−₹3–4K on confirmed adverse drift |
| `MAX_LOSS_PER_DAY` (exists) | cumulative realized | blocks new trades after −₹5K realized |
| 0.5% price trailing-SL (exists) | single trade | binds once trade is in profit |

⚠ Set thresholds coherently: if `MAX_LOSS_PER_TRADE` (₹6K) > `MAX_LOSS_PER_DAY` (₹5K), one trade can
exceed the daily cap (which then just blocks further trading). Choose them together — e.g.
daily ≈ 2× per-trade.

### 10.6 Near-term vs later

- **Near-term win (low risk):** just add the **hard `MAX_LOSS_PER_TRADE`** — one env var, one
  immediate-exit check. Closes the safety gap on its own.
- **Later refinement:** the trend-aware early-cut layer, once APPE has validated the slope/patience
  machinery in production.

---

## 11. TODO / future

- [ ] **Build ALE (§10).** Start with the hard `MAX_LOSS_PER_TRADE`; add the trend-aware early-cut
  after APPE is proven.
- [ ] **Straddle exits — loss side first, APPE only weakly applicable.** Analysis 2026-05-31: APPE's
  premise (big MFE-then-give-back) is a *directional* phenomenon. The short straddle earns from theta
  (slow grind-up, small round-trip); what actually costs it is a *directional move* = a loss, not
  giving back profit. Concretely, on May 29 the straddle peaked at only **+₹2,311** (trough −₹5,236,
  EOD −₹2,457) — it would **never have armed** APPE's ₹10K threshold. Across our days, straddle profit
  peaks are mostly single-digit thousands, below where a profit-ratchet activates.
  - **Priority: the straddle's loss side** (ALE / a better directional stop) — that is what hurt it
    (May 12 −52K, May 29 −2.5K), not profit give-back.
  - **APPE on straddle = low-priority refinement, not a port.** If done, it would convert the fixed
    25%-of-premium target into a ratchet, and needs full recalibration: much lower arm threshold,
    give-back as **% of net premium** (not the √-rupee budget), and **less patience** — because for a
    straddle a falling P&L signals a directional bleed, so profit-protect (APPE) and loss-avoid (ALE)
    converge into the same fast exit. **→ Full design proposal now drafted in §13 (2026-06-08); still
    design-only, calibration/implementation pending the loss-side priority + archive data.**
- [ ] **CUSUM trend gate (v2).** Replace the simple slope gate (3a) with a CUSUM change-point
  detector for earlier, statistically-grounded rollover detection.
- [ ] **Volatility scaling.** Tie the give-back budget `G` to live VIX / ATR (Chandelier-style) so
  it widens on volatile days and tightens on calm ones.
- [ ] **Time-of-day factor** (deferred from §12 Q5). Tighten the give-back budget in the last ~30 min
  before EOD (15:14), since a late-day retrace has little runway to recover. Evaluate after v1.
- [ ] **Scale-aware arm threshold.** Express `PROFIT_ARM_THRESHOLD` as R-multiples or points rather
  than a bare rupee constant, so it stays correct across different qty / instrument price.

---

## 12. Open questions for review

1. **Give-back form** — go with √ (recommended), linear, or tiered? (§4 Gate 2)
2. **Arm threshold** — is ₹10,000 right for a 60-qty BANKNIFTY position, or scale it to position size?
3. **Trend window / patience** — 180s / 30s reasonable, or do we want faster (more captures, more
   false exits) vs slower (fewer false exits, more give-back)?
4. Should APPE, once armed, **fully replace** the 0.5% price trail, or keep both with first-to-fire?
5. Do we want a **time-of-day factor** (e.g., tighten the budget in the last 30 min before EOD, since
   there's less runway to recover)?

### Decisions (2026-05-31)

1. **Give-back form → √ (square-root) budget**, `G = k·√P_max`, default `k=30`. Confirmed.
2. **Arm threshold → ₹15,000 to start** (≈250 BANKNIFTY pts ≈ 2.5R if per-trade risk ~₹6K), up from
   the initial ₹10K. ₹10K was too low — it would protect young trades; ₹15K marks a genuinely good
   (2.5×-risk) winner. Below ~₹16K still beats the loose 0.5% trail, so ₹15K still catches medium
   winners. **Two riders:** (a) express it scale-aware (R-multiples or points), NOT a bare rupee
   constant, so it stays correct if qty/price changes; (b) this is the **#1 parameter to sweep** in
   the backtest — grid-search ₹10K–20K across the archive before locking it.
3. **Trend window / patience → keep 180s / 30s for now**, validate empirically in the backtest first;
   tune only if the data argues for it.
4. **Price trail → keep both.** APPE + the existing 0.5% price trail run together, first-to-fire wins.
   Do not remove the price trail.
5. **Time-of-day factor → deferred to TODO** (see §11). Evaluate later; not in v1.

*Note: the §4 default and §5 worked examples still show A=₹10,000 for illustration/continuity. The
agreed starting default is **A=₹15,000** per decision #2; examples will be recomputed if/when we
finalize after the backtest sweep.*

*Update (2026-06-08): EMA arm changed to a per-lot formula — `ARM_PER_LOT=₹4,000` × lots, so a
60-qty BANKNIFTY position arms at **₹8,000** ("HAVRATPANA CONTROL": cap gain expectations, arm
sooner). The ₹15,000 in decision #2 predates the proportional-arm + live-tuning approach.*

---

## 13. APPE for the Short Straddle — design proposal

**Status: DESIGN ONLY (2026-06-08). Not implemented.** This elevates the §11 TODO bullet into a
concrete design to discuss and calibrate later. Read alongside **§11** (why it has been low-priority)
and **§10/ALE** — for a straddle the profit-protect and loss-avoid exits are tightly coupled (§13.4).

### 13.1 Why the EMA APPE does not port directly

EMA APPE assumes a *directional* trade with a large MFE-then-give-back in **absolute rupees**, and
uses a √-rupee budget `G = 30·√P_max`. The straddle breaks several of those assumptions:

| Aspect | EMA crossover | Short straddle (iron butterfly) |
|---|---|---|
| Direction | directional (BUY/SELL future) | market-neutral (sell ATM CE+PE, OTM8 wings) |
| Profit source | trend move | theta decay (slow grind) + IV drop |
| Profit scale | large ₹ swings | small, bounded by net premium; peaks often single-digit ₹thousands (§11: May 29 peaked +₹2,311) |
| P&L feed | WS ticks (sub-second) | REST quote poll, every `PNL_CHECK_INTERVAL` = 5s |
| A falling P&L means | giving back a directional gain | usually a **directional bleed = the start of a loss** |
| Natural scale | rupees / points | **% of net premium collected** (`PROFIT_TARGET_PCT`/`STOPLOSS_PCT` are already %) |

Consequence: the √-rupee budget is wrong here. At P_max = ₹2,311, `30·√2311 ≈ ₹1,442` is a **62%
give-back budget** — useless. The straddle needs a budget in **% of net premium**, a **low arm**,
and **less patience**.

### 13.2 Reframing the four gates for the straddle

Let `NP` = net premium collected at entry (the strategy already tracks `total_premium`). Work in
percentage-of-premium space, where the existing targets live (PT = +25%, SL = −50%):
```
u%(t)  = 100 · U(t) / NP            # current P&L as % of premium
pmax%  = max over t of u%(t)        # peak P&L %
```

- **Gate 1 — Arm (`STRADDLE_ARM_PCT`, % of NP).** Activate once `pmax% ≥ arm`. Because peaks are
  small and the +25% PT is rarely reached, the arm must be **low** — proposal **+10–12%** of NP, to
  be set from the actual `pmax%` distribution in the archive (§13.5 Q1), not guessed.
- **Gate 2 — Give-back budget (`G_s`, % of NP), a *percentage* ratchet (not √-rupee):**
  ```
  giveback% = pmax% − u%(t)
  exit candidate when  giveback% ≥ G_s(pmax%)
  ```
  Two candidate forms (decide on data): **(a) flat fraction** `G_s = f · pmax%` (e.g. f = 0.4 →
  protect 60% of the best %); **(b) concave-in-%** (mirror the EMA √ idea in %-space so larger %
  winners are protected proportionally tighter).
- **Gate 3 — Trend confirm, but FASTER.** Reuse the slope + patience machinery, recalibrated for the
  5s poll cadence (a 180s window is only ~36 samples; 30s confirm only ~6). Proposal: shorter than
  EMA — `W ≈ 90–120s`, `H ≈ 10–15s` — because for a straddle a sustained falling P&L *is* the adverse
  signal, so less patience is warranted.
- **Gate 4 — Hard override.** Even more important here: a fast V-shaped give-back on a straddle is a
  directional spike = real short-gamma risk. `giveback% ≥ HARD_MULT · G_s` → exit immediately.

### 13.3 Interaction with existing straddle exits

APPE ratchets **within** the existing band; it changes nothing at the edges:

| Exit | Level | Role once APPE is added |
|---|---|---|
| Profit target | +25% of NP | unchanged — the top cap |
| **APPE ratchet (new)** | trails within `[arm, +25%]` | books a reached-but-sub-PT profit (e.g. hit +16%, rolling over → exit ~+10%) instead of riding back to 0 / into loss |
| Stop-loss | −50% of NP | unchanged — loss backstop |
| EOD 15:14 | time | unchanged |
| Position sync | legs vanish | unchanged |

Today the straddle is **all-or-nothing**: it either tags +25% or rides the open P&L to EOD/SL. APPE
turns a partial winner that rolls over into a booked partial gain — the EMA lesson, in %-space.

### 13.4 The straddle-specific coupling: APPE ≈ ALE here

For the EMA, APPE (profit side) and ALE (loss side, §10) are distinct mechanisms. For a straddle they
**converge**: a P&L falling from a profit peak and a P&L deepening into a loss are the *same* event —
a directional move against short gamma. So the straddle should likely have **one coherent
"directional-bleed exit"** (a P&L ratchet that simply spans positive→negative), not two independent
APPE + ALE systems bolted on. **This is the central design decision to settle before building.**

### 13.5 Open questions (resolve before implementing)

1. **Data first.** Extract per-day `pmax%` and the full `u%(t)` curve for the straddle from the
   archive; set the arm and `G_s` from the real distribution. (Most straddle days never reach +25%.)
2. **Give-back form** — flat-fraction (a) vs concave-% (b)?
3. **Arm level** — +10% / +12% of NP? (scale-invariant across premium sizes, unlike a bare ₹ arm).
4. **Window / patience** for the 5s poll cadence — how much faster than EMA's 180s / 30s?
5. **Unify with ALE?** Per §13.4, one directional-bleed ratchet spanning profit→loss, vs separate
   APPE + `MAX_LOSS_PER_TRADE`?
6. **Hedge effect** — does the iron-butterfly's capped tail change where the ratchet should sit
   relative to a naked straddle?
7. **Priority** — §11 still holds that the straddle's **loss side** is what actually hurt it
   (May 12 −52K, May 29 −2.5K). Sequence this design behind the straddle loss stop unless the
   archive data shows meaningful sub-PT profit give-back to recover.

---

## 14. Reverse-signal confirmation filter (EMA whipsaw guard)

**Status: DESIGN ONLY (2026-06-12). Not implemented.** Applies to the **EMA crossover** strategy's
reverse-signal exit. Needs an archive backtest before going live.

### 14.1 The problem — Jun 12, 2026

EMA went BUY @ 56,000 (09:20). At 12:45 a **bearish 9/21 crossover** fired with volume — but the
cross was **hair-thin**: EMA(9) 55,905.21 vs EMA(21) 55,905.89, a **0.68-point** separation, while
the position was only **−154 pts (−0.27%)** underwater (still inside the 0.5% trailing-SL). The
reverse-exit fired anyway → closed @ 55,846.60 = **−₹9,204**, which then tripped the ₹5,000 daily
circuit breaker and **locked the strategy out for the day**. Price then **recovered to 56,898 by
EOD** — the long was ultimately right. Held, it would have made **~+₹53,880**. A noise-level cross at
a local dip cost ~₹63k of swing and the whole afternoon.

### 14.2 The rule

Don't exit on the *crossover itself*. **Book that a reverse fired, then require price follow-through
to confirm it:**

```
On a reverse crossover (e.g. EMA9 crosses below EMA21 while long):
    arm a confirm-stop at  trigger_price − REVERSE_CONFIRM_PTS     (long; mirror for short)
    REVERSE_CONFIRM_PTS = REVERSE_CONFIRM_PCT × price   (≈ 0.05% ≈ 28 BANKNIFTY pts)
    if price reaches the confirm-stop:        EXIT (reason = REVERSE_CONFIRMED)
    if price re-crosses back (gap flips bullish) before the stop:  CANCEL — it was noise
```

- The **28 pts is BANKNIFTY-future index points** (0.05% of ~56,000), i.e. **₹1,680 at 60 qty** — it
  is the *price follow-through* buffer, NOT the EMA separation and NOT the rupee loss at exit.
- The **0.5% trailing-SL stays as the hard backstop** for a fast crash/gap that reaches it before a
  reverse-cross+confirm can engage.

### 14.3 Worked comparison — all anchored to the real Jun 12 trade (BUY @ 56,000, 60 qty)

`0.5% TSL = 55,720` · reverse cross @ `55,846.60` · confirm-stop @ `55,846.60 − 28 = 55,818.60`

| Case | What price does after the reverse cross | Exit (with rule) | Exit px | P&L |
|------|------------------------------------------|------------------|---------|-----|
| **baseline (current rule)** | exits *at* the cross, no confirmation | reverse (immediate) | 55,846.6 | **−9,204** |
| **A. Noise / whipsaw — JUN 12 REAL** | dips to 55,845 then recovers to 56,898 | none → held to EOD | 56,898 | **+53,880** |
| **B. Confirmed downtrend** | follows through −28 to 55,818.6 | `REVERSE_CONFIRMED` | 55,818.6 | **−10,884** |
| **C. Fast crash / gap** | drops to the TSL before a cross+confirm engages | 0.5% TSL backstop | 55,720 | **−16,800** |

Read it as: the filter **saves the whole whipsaw** in case A (−9,204 → +53,880); **costs an extra
₹1,680** vs the baseline in case B (the confirmation buffer, 28 pts × 60); and is **unchanged** in
case C (the TSL is the floor either way). Net value = (whipsaw days saved) − (₹1,680 × confirmed
days). Only an archive backtest decides if that's positive.

### 14.4 Parameters / open questions
- `REVERSE_CONFIRM_PCT` default ~**0.05%** (≈28 BANKNIFTY pts) — sweep in backtest.
- Reference for the confirm-stop: the **reverse-trigger price** (armed at the cross, cancelled on a
  bullish re-cross). Alternative: trail it if a new lower cross prints.
- Should a **time cap** also disarm it (e.g. if not confirmed within N minutes, drop the pending
  reverse)?
- **Backtest** across the archive — count whipsaw-saved days vs the ₹1,680-per-confirmed-day cost,
  and check it never converts a TSL-day into something worse. (Includes Jun 12 as a known win.)

### 14.5 Backtest verdict + SHADOW logger (2026-06-12)

The offline 5m backtest **cannot reliably evaluate this filter.** Reverses are rare (only **1 of 16**
archived days had one), and the filter's edge lives in a **~28-pt band** — but the backtest's
signal-price fidelity is **~30 pts** (5m-candle close vs tick fill, + 1-day EMA warmup vs the live
multi-day history). On Jun 12 that error flips the verdict: backtest detected the reverse @ 55,875
(confirm-stop 55,847) and the low 55,845 *just* clips it → "CONFIRMED, filter loses ₹1,676"; live
fired the reverse @ 55,846 (confirm-stop 55,818), same low 55,845 *misses* it → "HELD, filter saves
~₹63k." **Opposite conclusions from a 29-pt signal error ≈ the 28-pt threshold.** You can't measure a
28-pt effect with a 30-pt-error ruler.

So instead of trusting the backtest, a **SHADOW logger is implemented in
`ema_crossover_banknifty.py`** (commit on mock/strategies): on every *live* reverse-signal exit it
logs `[SHADOW] §14 armed …` with the confirm-stop, then each tick logs whether price **CONFIRMED**
(hit the −0.05% follow-through → filter ≈ no edge) or **HELD** (never confirmed by EOD → filter would
have avoided the exit, with the hold-to-EOD mark). **Log-only — it never changes trading.** Since
reverses are rare, this accrues perfect-fidelity evidence over the coming weeks at zero risk; revisit
the implement decision once we have several real `[SHADOW]` data points. `REVERSE_CONFIRM_PCT` env
tunes the buffer (default 0.05%).

---

## 15. Daily breaker ↔ per-trade risk coherence (follow-up)

**Status: design / follow-up (2026-06-12). Queued behind §14 — do not change the breaker before §14
is live.**

**The incoherence (quantified at 60 qty).** The 0.5% trailing-SL's worst-case loss is
`280 pts × 60 = ₹16,800` — about **3× the ₹5,000 daily breaker**. A losing trade only stays under
₹5k if it first ran **+197 pts favorable** (so the TSL trails to within 83 pts = ₹5k of entry).
**Any trade that reverses before going ~+197 pts in profit → its single TSL loss > ₹5k → breaker
trips → no more EMA trades that day.** So the breaker is effectively "stop after the first real
losing trade." Per-trade risk ≫ daily cap — the §10.5 gap, now with numbers.

**Why we can't just raise it now.** The breaker interacts with the reverse-signal **flip**. On a
reverse day a higher breaker lets the *losing middle flip* through before halting:

| Daily limit | June 12 outcome |
|---|---|
| ₹5,000 (now) | blocks the flip after trade 1 → **−9,204** |
| ₹10,000 | allows the losing SELL flip, blocks the recovery → **−14,304** (worse!) |
| ≥₹15,000 | survives both flips, reaches the recovery BUY → **≈ +43,700** |

So ₹10k is the *worst* spot, and the breaker can't be tuned until the flip behaviour is fixed.

**Sequencing (complementary fixes):**
1. **§14 reverse-confirm filter** removes the noise-flip cascade.
2. **Then** raise the breaker. A TSL exit goes *flat* (it does not flip), so the next crossover is a
   clean fresh entry — giving the "2nd shot after a TSL loss" without the flip gotcha.

**Plan:**
- Keep **₹5,000** until §14 is validated + live.
- Then raise the daily breaker to **~₹15,000** (coherent with the ₹16,800 per-trade TSL) so one TSL
  loss doesn't lock out the day.
- **Better**: pair with a §10 **ALE per-trade rupee stop** (cap each trade at ~₹X, daily ≈ 2×X) for
  full coherence — but keep that stop **loose enough not to whipsaw** (June 12 needed ~280 pts of
  room; a tight ₹6k/100-pt stop would have ejected that winner at −6k), so it needs the backtest.
- **Ideal**: set the breaker from the *actual* TSL-loss distribution once logs/shadow give real data.

---

## 16. Master TODO / open-items checklist

Single tracker so nothing is lost. (Older brainstorm ideas live in §11; this is the actionable list.)
Tags: `[live]` deployed · `[shadow]` log-only data-gathering · `[design]` not built · `[blocked]` waiting on another item.

**EMA crossover**
- [x] `[live]` APPE arm → proportional **₹4,000/lot** (₹8,000 @ 60 qty) — "HAVRATPANA CONTROL"
- [x] `[live]` APPE give-back **G size-aware** `√(units/2)` (§4/§5) — no-op at 60 qty
- [ ] `[shadow]` **§14 reverse-confirm filter** — implement once ≥3–5 real `[SHADOW]` events confirm value
- [ ] `[design]` **Extend §14 min-separation to ENTRY crosses** — require EMA(9)−EMA(21) ≥ ~0.05% (≈28 pts) *at the cross* to enter, not just to reverse; a thin cross + a one-off volume blip fakes out (Jun 15: a **+7.94-pt** cross w/ a volume blip → −₹7k+ trade on a low-volume drift). Shadow-log entry-cross separation first to size the threshold.
- [ ] `[design][blocked on §14]` **§15 breaker raise** to ~₹15k so one TSL loss doesn't end the day
- [ ] `[design]` **§10 ALE** per-trade rupee stop (`MAX_LOSS_PER_TRADE`) + coherent daily — keep loose; backtest first
- [ ] `[design]` Reverse-confirm `REVERSE_CONFIRM_PCT` value — tune from shadow data, not the offline backtest (§14.5)

**Short straddle**
- [ ] `[live]` startup no-trade banner — done
- [ ] `[design]` **§13 APPE for straddle** (%-of-premium framing) — revisit after EMA APPE reviewed live
- [ ] `[design]` **Straddle loss-side stop** — §11 priority; its real pain is directional moves, not give-back

**Validation / infra**
- [ ] Backtest fidelity — offline 5m can't resolve sub-~30-pt effects (§14.5); use tick replay or live shadow for §14/§15 tuning
- [ ] Multi-day APPE arm sweep (₹4k vs ₹5k) — optional, once a faithful backtest exists
- [x] `[live]` 09:05 post-auth restart timer · pre-market apt upgrades · feed-health guard + ERROR logging
