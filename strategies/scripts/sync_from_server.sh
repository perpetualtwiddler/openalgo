#!/usr/bin/env bash
# sync_from_server.sh — pull the live trading artefacts from the server to this local repo.
#
# WHY THIS EXISTS: the strategy, the 15:22 journal timer and the 15:35 capture all run ON THE
# SERVER, so every artefact is written there. The local repo is a convenience copy for analysis
# and for git. Nothing syncs automatically — a server-side cron cannot push into WSL (no inbound
# route) and a local timer would miss any day the laptop is off. So this is deliberately a
# manual pull: run it after the EOD chain finishes (>= 15:45 IST) or any time you want to look
# at the numbers locally.
#
# Found 2026-08-17: the local trade_journal.csv had silently drifted to 5 rows / 48 columns
# while the server had 6 / 52 — the scheduled job had been working perfectly, but only there.
#
# Usage:   ./strategies/scripts/sync_from_server.sh
#          SERVER=root@1.2.3.4 ./strategies/scripts/sync_from_server.sh
set -euo pipefail

SERVER="${SERVER:-root@109.123.248.99}"
REMOTE="${REMOTE:-/root/data/openalgo}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "  pulling from ${SERVER}:${REMOTE}  ->  ${HERE}"

# The per-day ledger (the one that matters most) plus the inputs it is derived from, so a
# local --backfill can reproduce it.
FILES=(
  "log/trade_journal.csv"      # durable per-day record, one row per trading day
  "log/exit_timing.csv"        # 15:00/15:01/15:05/15:10/15:14 replay comparison
  "log/margin.csv"             # broker-actual margin snapshotted at entry
  "log/slippage.csv"           # fill vs reference, per leg per phase
)
mkdir -p "${HERE}/log/tradebook"
for f in "${FILES[@]}"; do
  before=""
  [[ -f "${HERE}/${f}" ]] && before="$(wc -l < "${HERE}/${f}" | tr -d ' ')"
  if scp -q "${SERVER}:${REMOTE}/${f}" "${HERE}/${f}" 2>/dev/null; then
    after="$(wc -l < "${HERE}/${f}" | tr -d ' ')"
    printf "   %-26s %s -> %s lines\n" "$(basename "${f}")" "${before:-none}" "${after}"
  else
    printf "   %-26s (absent on server, skipped)\n" "$(basename "${f}")"
  fi
done

# Tradebook archives are one small JSON per day and the ONLY fill-level record that survives
# (the broker API is current-day only), so mirror the whole directory.
if scp -q "${SERVER}:${REMOTE}/log/tradebook/*.json" "${HERE}/log/tradebook/" 2>/dev/null; then
  echo "   tradebook/                 $(ls -1 "${HERE}/log/tradebook"/*.json 2>/dev/null | wc -l | tr -d ' ') day(s)"
fi

echo
echo "  journal now:"
python3 - "$HERE" <<'PY'
import csv, sys, os
p = os.path.join(sys.argv[1], "log", "trade_journal.csv")
rows = list(csv.DictReader(open(p)))
tot = sum(float(r["net_pnl"] or 0) for r in rows)
print(f"   {len(rows)} rows, {len(rows[0])} columns, net {tot:+,.2f}")
print(f"   latest: {rows[-1]['date']}  net {rows[-1]['net_pnl']}  ({rows[-1]['confidence']})")
PY
