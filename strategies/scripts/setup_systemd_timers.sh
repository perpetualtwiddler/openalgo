#!/bin/bash
set -euo pipefail

# =============================================================================
# OpenAlgo Operational Systemd Timers
# =============================================================================
# Installs two systemd timers used to keep paper-trading runs hands-off:
#
#   1. openalgo-restart.timer            — daily 08:00 IST restart of openalgo
#      Why: APScheduler's ThreadPoolExecutor enters a "shutdown" state after
#      ~2 days of uptime and stops launching scheduled strategies (the
#      "all checks passed, starting" log fires but no subprocess spawns).
#      Restarting daily at 08:00 IST — 75 minutes before market open and
#      after MASTER_CONTRACT_CUTOFF_TIME — keeps the scheduler healthy.
#
#   2. openalgo-capture-trade-data.timer — Mon-Fri 15:35 IST trade data capture
#      Why: archive intraday 1m/5m candles for NIFTY, BANKNIFTY, VIX, and
#      22 ATM/OTM option strikes into /root/data/zerodha/trade-data/<date>/
#      Used by backtest_offline.py for offline replay of fixes & tuning.
#      15:35 IST gives a 5-minute buffer past 15:30 market close so the
#      final candles are settled.
#
# Usage:
#   On the OpenAlgo server, as root:
#     export OPENALGO_API_KEY=<your-api-key>
#     ./setup_systemd_timers.sh
#
#   Or pass the key inline:
#     OPENALGO_API_KEY=<key> ./setup_systemd_timers.sh
#
# Re-running is safe — files are overwritten and timers re-enabled idempotently.
# =============================================================================

INSTALL_DIR="${INSTALL_DIR:-/root/data/openalgo}"
API_KEY="${OPENALGO_API_KEY:-}"

if [[ -z "$API_KEY" ]]; then
    echo "ERROR: OPENALGO_API_KEY env var is required" >&2
    echo "Generate at https://<your-domain>/apikey then re-run:" >&2
    echo "  OPENALGO_API_KEY=<key> $0" >&2
    exit 1
fi

if [[ ! -d "$INSTALL_DIR" ]]; then
    echo "ERROR: Install dir not found: $INSTALL_DIR" >&2
    exit 1
fi

echo "Installing systemd timers (install dir: $INSTALL_DIR)..."

# --- Timer 1: daily restart -------------------------------------------------

cat > /etc/systemd/system/openalgo-restart.service << EOF
[Unit]
Description=Daily restart of OpenAlgo (workaround for APScheduler executor shutdown)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart openalgo
EOF

cat > /etc/systemd/system/openalgo-restart.timer << 'EOF'
[Unit]
Description=Restart OpenAlgo daily at 08:00 IST

[Timer]
OnCalendar=*-*-* 08:00:00 Asia/Kolkata
Persistent=true
Unit=openalgo-restart.service

[Install]
WantedBy=timers.target
EOF

# --- Timer 2: daily trade data capture --------------------------------------

cat > /etc/systemd/system/openalgo-capture-trade-data.service << EOF
[Unit]
Description=Daily capture of trade data for backtest archive
After=openalgo.service

[Service]
Type=oneshot
WorkingDirectory=$INSTALL_DIR
Environment=OPENALGO_API_KEY=$API_KEY
Environment=OPENALGO_HOST=http://127.0.0.1:5000
Environment=TZ=Asia/Kolkata
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/strategies/scripts/capture_trade_data.py
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/openalgo-capture-trade-data.timer << 'EOF'
[Unit]
Description=Capture trade data daily at 15:35 IST (Mon-Fri)

[Timer]
OnCalendar=Mon..Fri *-*-* 15:35:00 Asia/Kolkata
Persistent=true
Unit=openalgo-capture-trade-data.service

[Install]
WantedBy=timers.target
EOF

# --- Reload + enable --------------------------------------------------------

systemctl daemon-reload
systemctl enable --now openalgo-restart.timer
systemctl enable --now openalgo-capture-trade-data.timer

echo ""
echo "Both timers enabled. Next scheduled runs:"
systemctl list-timers openalgo-restart.timer openalgo-capture-trade-data.timer --no-pager

echo ""
echo "Useful commands:"
echo "  systemctl list-timers --no-pager"
echo "  journalctl -u openalgo-restart.service --since today --no-pager"
echo "  journalctl -u openalgo-capture-trade-data.service --since today --no-pager"
echo "  systemctl start openalgo-capture-trade-data.service  # manual on-demand capture"
