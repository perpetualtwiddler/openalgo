#!/usr/bin/env bash
# bt_daily.sh — pull one trading day's BANKNIFTY-future capture from the deployed
# server and run the standard EMA backtest battery on it.
#
# Battery:
#   A. LIVE config (9/21 @ 5m, default ~17pt gate) — what the deployed strategy does.
#   B. 8/17 @ 2m and 3m at the default gate — does the faster config trade at the live gate?
#   C. 8/17 @ 3m, close-confirmed vs early-entry at a relaxed gap (GAP pts) — the early question.
#   D. (optional, SWEEP=1) gap-gate sweep for 8/17 @ 3m.
#
# Usage:
#   strategies/scripts/bt_daily.sh [YYYY-MM-DD]      # default: today (IST)
#   GAP=5 strategies/scripts/bt_daily.sh 2026-06-17  # relaxed-gap value for section C
#   SWEEP=1 strategies/scripts/bt_daily.sh 2026-06-17
#
# Env overrides: MDBT_SERVER, MDBT_REMOTE_DIR, BACKTEST_DATA_DIR.
# Each section prints the GRAND SUMMARY (PRICE-ONLY | VOL-FILTER). For full per-trade
# detail, run backtest_ticks.py directly with the same flags.
set -uo pipefail

DATE="${1:-$(TZ=Asia/Kolkata date +%F)}"
SERVER="${MDBT_SERVER:-root@offramp.oftenuncertain.net}"
REMOTE_DIR="${MDBT_REMOTE_DIR:-/root/data/openalgo/log/market_data_capture}"
LOCAL_DIR="${BACKTEST_DATA_DIR:-/home/dksha/ptwiddler/backtestdata}"
GAP="${GAP:-3}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

# 1. Pull the day's capture if not already local.
f="$LOCAL_DIR/$DATE/normalized_market_data.jsonl"
if [ ! -f "$f" ]; then
  echo "Pulling $DATE from $SERVER ..."
  mkdir -p "$LOCAL_DIR/$DATE"
  if ! scp -q "$SERVER:$REMOTE_DIR/$DATE/normalized_market_data.jsonl" "$f"; then
    echo "ERROR: could not fetch capture for $DATE (market closed for the day yet? wrong date?)" >&2
    rmdir "$LOCAL_DIR/$DATE" 2>/dev/null
    exit 1
  fi
fi
echo "=== Backtest battery for $DATE ($(wc -l < "$f") ticks) ==="
echo

run() {  # run "<label>" <backtest_ticks flags...>
  local label="$1"; shift
  echo "## $label"
  BACKTEST_DATA_DIR="$LOCAL_DIR" uv run python strategies/scripts/backtest_ticks.py "$DATE" "$@" 2>&1 \
    | sed -n '/GRAND SUMMARY/,$p' | grep -E "config|@ "
  echo
}

# A. Live deployed config.
run "LIVE 9/21 @ 5m (default ~17pt gate)" --tf 5 --fast 9 --slow 21 --warmup 9 --vol-sma 20

# B. Faster 8/17 configs at the live gate.
run "8/17 @ 2m (default gate)" --tf 2 --fast 8 --slow 17 --warmup 9 --vol-sma 10
run "8/17 @ 3m (default gate)" --tf 3 --fast 8 --slow 17 --warmup 9 --vol-sma 10

# C. Early-entry vs close-confirmed at a relaxed gap.
run "8/17 @ 3m  gap ${GAP}pts  CLOSE" --tf 3 --fast 8 --slow 17 --warmup 9 --vol-sma 10 --gap-gate "$GAP"
run "8/17 @ 3m  gap ${GAP}pts  EARLY" --tf 3 --fast 8 --slow 17 --warmup 9 --vol-sma 10 --gap-gate "$GAP" --early-entry

# D. Optional gap-gate sweep for 8/17 @ 3m.
if [ "${SWEEP:-0}" = "1" ]; then
  echo "## 8/17 @ 3m gap-gate sweep (pts): close-confirmed"
  for g in 17 9 6 3 0; do
    run "  gap ${g}pts" --tf 3 --fast 8 --slow 17 --warmup 9 --vol-sma 10 --gap-gate "$g"
  done
fi
