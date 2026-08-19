#!/usr/bin/env bash
# ledger_snapshot.sh — snapshot the live trading record into a LOCAL-ONLY git repo.
#
# WHY A SEPARATE REPO. The openalgo repo's remote (`mkds-openalgo`) is a PUBLIC fork of
# marketcalls/openalgo, so trade data must never be committed there — `log/*.csv` is gitignored
# repo-wide for exactly that reason, and trade_analytics.xlsx embeds per-leg fill prices, broker
# margins and (via the Projection corpus formula) the account's opening balance. This repo gives
# the same version history and diffs with no publication path: it has NO REMOTE, by design, and
# this script refuses to run if one ever appears.
#
# WHAT IT CAPTURES, and why each one matters:
#   trade_journal.csv    the durable per-day ledger — the thing everything else is derived from
#   exit_timing.csv      the 15:00/15:01/15:05/15:10/15:14 replay comparison
#   margin.csv           broker-actual margin snapshotted at entry (not our defined-risk figure)
#   slippage.csv         fill vs reference, per leg per phase
#   trade_analytics.xlsx the analysis workbook (regenerable, but cheap to keep versioned)
#   opening_cash.txt     account opening balance — untracked in openalgo on purpose
#   tradebook/*.json     THE ONLY fill-level record that survives; the broker API is current-day
#                        only, so a day not archived on its own date is gone forever
#   strategies/*straddle*  the strategy's own logs. These ROTATE OFF THE SERVER after ~7-10 days
#                        and are the only source for mfe/mae and the [ENTRY] spot line. Nothing
#                        else can reconstruct them. This is the most time-critical thing here.
#
# Usage:  ./strategies/scripts/ledger_snapshot.sh            # pull from server + commit
#         SKIP_SERVER=1 ./strategies/scripts/ledger_snapshot.sh   # local artefacts only
set -euo pipefail

SERVER="${SERVER:-root@109.123.248.99}"
REMOTE_DIR="${REMOTE_DIR:-/root/data/openalgo}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LEDGER="${LEDGER:-$(cd "${HERE}/.." && pwd)/trading-ledger}"

if [[ -d "${LEDGER}/.git" ]]; then
  # The whole point of this repo is that it cannot be published. Enforce it rather than trust it.
  if [[ -n "$(git -C "${LEDGER}" remote)" ]]; then
    echo "REFUSING: ${LEDGER} has a git remote ($(git -C "${LEDGER}" remote | tr '\n' ' '))." >&2
    echo "This repo holds fill-level trade data and an account balance. It is meant to have no" >&2
    echo "remote. Remove it (git -C ${LEDGER} remote remove <name>) or point LEDGER elsewhere." >&2
    exit 1
  fi
else
  echo "  creating local-only ledger repo at ${LEDGER}"
  mkdir -p "${LEDGER}"
  git -C "${LEDGER}" init -q
  git -C "${LEDGER}" config user.name  "$(git -C "${HERE}" config user.name  || echo mandar)"
  git -C "${LEDGER}" config user.email "$(git -C "${HERE}" config user.email || echo mandar@localhost)"
fi

mkdir -p "${LEDGER}/log/tradebook" "${LEDGER}/log/strategies"

# --- the time-critical pull: rotating server-side logs -------------------------------------
if [[ "${SKIP_SERVER:-0}" != "1" ]]; then
  echo "  pulling rotating straddle logs from ${SERVER} (these age out server-side)"
  rsync -a --ignore-existing \
    "${SERVER}:${REMOTE_DIR}/log/strategies/"*straddle* "${LEDGER}/log/strategies/" 2>/dev/null \
    || echo "   (no straddle logs retrieved — check the server or use SKIP_SERVER=1)"
  rsync -a "${SERVER}:${REMOTE_DIR}/log/tradebook/" "${LEDGER}/log/tradebook/" 2>/dev/null || true
fi

# --- local artefacts ----------------------------------------------------------------------
for f in trade_journal.csv exit_timing.csv margin.csv slippage.csv trade_analytics.xlsx \
         opening_cash.txt; do
  [[ -f "${HERE}/log/${f}" ]] && cp -p "${HERE}/log/${f}" "${LEDGER}/log/${f}"
done
[[ -d "${HERE}/log/tradebook" ]] && cp -pn "${HERE}/log/tradebook/"*.json "${LEDGER}/log/tradebook/" 2>/dev/null || true

if [[ ! -f "${LEDGER}/README.md" ]]; then
  cat > "${LEDGER}/README.md" << 'MD'
# trading-ledger — local-only history of the live NIFTY short-straddle

**This repo has no remote, and must never get one.** It contains per-leg fill prices, broker
margin figures, slippage, and the account's opening balance. The openalgo repo it accompanies
pushes to a **public** fork of `marketcalls/openalgo`, which is why this data lives here instead.
`ledger_snapshot.sh` refuses to run if a remote is ever added.

## What is irreplaceable here

- `log/tradebook/*.json` — the broker tradebook API is **current-day only**. A day not archived
  on its own trading date cannot be recovered. This is why 2026-08-06 is permanently `low`
  confidence in the journal.
- `log/strategies/*straddle*` — the strategy's own logs **rotate off the server after ~7-10
  days** and are the only source for `mfe`/`mae` and the `[ENTRY] NIFTY spot` line. Nothing can
  reconstruct them once gone.

Everything else (`trade_journal.csv`, `trade_analytics.xlsx`) is derivable from those two, given
the scripts in the openalgo repo.

## Refresh

    cd ../openalgo && ./strategies/scripts/sync_from_server.sh     # server -> openalgo/log
    ./strategies/scripts/ledger_snapshot.sh                        # -> here, and commit

Monthly is enough for the CSVs. The rotating logs are the reason not to leave it much longer.

## This is history, not a backup

One disk. Pair it with an off-box copy when convenient — that decision is still open.
MD
fi

cd "${LEDGER}"
git add -A
if git diff --cached --quiet; then
  echo "  nothing changed — no commit"
else
  git diff --cached --stat | tail -12
  git commit -q -m "ledger snapshot $(TZ=Asia/Kolkata date +%Y-%m-%d)

$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M %Z'). Local-only repo, no remote by design.
Journal rows: $(( $(wc -l < log/trade_journal.csv) - 1 )) · tradebook archives: $(ls log/tradebook/*.json 2>/dev/null | wc -l) · strategy logs: $(ls log/strategies/*straddle* 2>/dev/null | wc -l)"
  echo "  committed: $(git log --oneline -1)"
fi
echo "  remotes: $(git remote | tr '\n' ' ')(none expected)"
