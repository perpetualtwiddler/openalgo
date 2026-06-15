"""Parity tests: backtester exit logic vs the live EMA-crossover strategy.

Verifies that `backtest_ticks.py::Position.step` (the offline tick backtester's
trailing-SL + APPE exit evaluation) makes the SAME exit decision, tick-for-tick,
as the live `ema_crossover_banknifty.py::EMACrossoverBot.on_ltp_update`.

Approach: drive the REAL live method (with a controllable monotonic clock and a
synchronous, stubbed place_exit) and the backtester's Position.step over identical
synthetic tick streams, asserting identical (exit-tick-index, exit-reason).

Both modules are loaded by path so the test doesn't depend on the strategy being
importable as a package. The live module's `time` and `threading` references are
replaced on the freshly-loaded module object only (no global side effects).
"""

import collections
import contextlib
import importlib.util
import io
import pathlib
import random
import time as _real_time
import types

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "strategies" / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


live = _load("_live_ema_under_test", "ema_crossover_banknifty.py")
bt = _load("_bt_ticks_under_test", "backtest_ticks.py")


class _Clock:
    """Controllable stand-in for the `time` module: monotonic() returns whatever
    we set; everything else falls through to the real time module."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def __getattr__(self, n):
        return getattr(_real_time, n)


class _SyncThread:
    """Runs the target inline instead of on a background thread, so a stubbed
    place_exit records its reason deterministically before on_ltp_update returns."""

    def __init__(self, target=None, args=(), daemon=None):
        self._t, self._a = target, args

    def start(self):
        if self._t:
            self._t(*self._a)


_clock = _Clock()
live.time = _clock
live.threading = types.SimpleNamespace(Thread=_SyncThread)


# Constants that must agree for the offline replay to mean anything. (live name, bt name)
_CONST_PAIRS = [
    ("TRAILING_SL_PCT", "TRAILING_SL_PCT"), ("TIGHT_TSL_THRESHOLD", "TIGHT_TSL_THRESHOLD"),
    ("TIGHT_TSL_PCT", "TIGHT_TSL_PCT"), ("TIGHT_TSL_ENABLED", "TIGHT_TSL_ENABLED"),
    ("PROFIT_ARM_THRESHOLD", "PROFIT_ARM_THRESHOLD"),
    ("GIVEBACK_K", "GIVEBACK_K"), ("TREND_WINDOW_SEC", "TREND_WINDOW_SEC"),
    ("TREND_CONFIRM_SEC", "TREND_CONFIRM_SEC"), ("HARD_MULT", "HARD_MULT"),
    ("GIVEBACK_REF_UNITS", "GIVEBACK_REF_UNITS"), ("REVERSE_CONFIRM_PCT", "REVERSE_CONFIRM_PCT"),
    ("QUANTITY", "QTY"),
]


def _run_live(direction, entry, ticks):
    """Replay ticks through the real live on_ltp_update; return (idx, reason) or None."""
    bot = live.EMACrossoverBot.__new__(live.EMACrossoverBot)
    bot.symbol = "TEST"
    bot.position = direction
    bot.entry_price = entry
    bot.peak_price = entry
    pct = live.TRAILING_SL_PCT
    bot.trailing_sl = (round(entry * (1 - pct / 100), 2) if direction == "BUY"
                       else round(entry * (1 + pct / 100), 2))
    bot.ltp = None
    bot.exit_in_progress = False
    bot.ws_alive = True
    bot.feed_stale = False
    bot.last_tick_ts = None
    bot.last_stale_warn_ts = 0.0
    bot.appe_peak = 0.0
    bot.appe_armed = False
    bot.appe_breach_start = None
    bot.pnl_window = collections.deque()
    bot.shadow_reverse = None  # §14 shadow attr (log-only); on_ltp_update reads it each tick
    exits = []
    bot.place_exit = lambda reason="Manual": exits.append(reason)
    with contextlib.redirect_stdout(io.StringIO()):
        for i, (t, ltp) in enumerate(ticks):
            _clock.t = t
            bot.on_ltp_update({"type": "market_data", "symbol": "TEST", "data": {"ltp": ltp}})
            if exits:
                return (i, exits[0])
    return None


def _run_bt(direction, entry, ticks):
    """Replay ticks through the backtester Position.step; return (idx, reason) or None."""
    pos = bt.Position(direction, entry, None)
    for i, (t, ltp) in enumerate(ticks):
        res = pos.step(ltp, t)
        if res is not None:
            return (i, res[0])
    return None


_E = 55000.0

# Crafted scenarios that pin specific exit branches. Each: (label, dir, entry, ticks, expect)
_CRAFTED = [
    # APPE never arms (peak unrealized 6000 < 8000); flat 0.5% TSL off peak 55100
    # = 54824.5, so the pullback must drop below that (TIGHT_TSL is gated OFF now).
    ("tsl_only", "BUY", _E,
     [(0, 55000), (1, 55050), (2, 55100), (3, 55080), (4, 55020), (5, 54800)],
     "TRAILING_SL"),
    # APPE_HARD on a tick where the trailing-SL is ALSO hit — proves APPE wins (live ordering)
    ("appe_hard_coincident_with_tsl", "BUY", _E,
     [(0, 55000), (1, 55333), (2, 55180)],
     "APPE_HARD"),
]


def _ratchet_ticks():
    # Peak first (u=15000 -> armed), then a slow decline held in the narrow band
    # between the hard-exit floor (~55127.5) and the trailing-SL (~55111.9), long
    # enough for the 90s slope window + 30s confirm without tripping HARD/TSL first.
    ticks = [(0, 55250.0)]
    for t in range(1, 220):
        ticks.append((t, round(55188.0 - (t - 1) * (58.0 / 218), 1)))
    return ticks


_CRAFTED.append(("appe_ratchet", "BUY", _E, _ratchet_ticks(), "APPE_RATCHET"))


def _make_walk(seed, drift, n=320):
    rnd = random.Random(seed)
    price = 55000.0
    ticks = [(0, price)]
    for i in range(1, n):
        price += rnd.gauss(drift, 9.0)
        ticks.append((i, round(price, 1)))
    return ticks


_WALKS = [
    (1000 + s, "BUY" if s % 2 == 0 else "SELL", [0.0, 0.6, -0.6, 1.2, -1.2][s % 5])
    for s in range(40)
]


@pytest.mark.parametrize("lname, bname", _CONST_PAIRS)
def test_constants_match(lname, bname):
    """Backtester constants must equal the live strategy's defaults (no drift)."""
    assert getattr(live, lname) == getattr(bt, bname), (
        f"constant drift: live.{lname}={getattr(live, lname)} != bt.{bname}={getattr(bt, bname)}"
    )


@pytest.mark.parametrize("label, direction, entry, ticks, expect", _CRAFTED,
                         ids=[c[0] for c in _CRAFTED])
def test_crafted_scenarios(label, direction, entry, ticks, expect):
    """Each crafted scenario: live and backtester agree AND hit the expected branch."""
    lv = _run_live(direction, entry, ticks)
    bv = _run_bt(direction, entry, ticks)
    assert lv == bv, f"{label}: live={lv} bt={bv}"
    assert lv is not None and lv[1] == expect, f"{label}: expected {expect}, got {lv}"


@pytest.mark.parametrize("seed, direction, drift", _WALKS,
                         ids=[f"walk{s}-{d}-{dr:+}" for s, d, dr in _WALKS])
def test_random_walk_parity(seed, direction, drift):
    """Over randomized price walks, live and backtester make identical exit decisions."""
    ticks = _make_walk(seed, drift)
    assert _run_live(direction, _E, ticks) == _run_bt(direction, _E, ticks)


def test_all_exit_branches_covered():
    """Guard: the suite must actually exercise every exit branch, else parity is hollow."""
    reasons = set()
    for _, direction, entry, ticks, _ in _CRAFTED:
        r = _run_bt(direction, entry, ticks)
        reasons.add(r[1] if r else "NO_EXIT")
    for seed, direction, drift in _WALKS:
        r = _run_bt(direction, _E, _make_walk(seed, drift))
        reasons.add(r[1] if r else "NO_EXIT")
    assert {"TRAILING_SL", "APPE_HARD", "APPE_RATCHET", "NO_EXIT"} <= reasons, (
        f"branches not all exercised: {reasons}"
    )
