#!/bin/bash
set -euo pipefail

# =============================================================================
# OpenAlgo Operational Systemd Timers
# =============================================================================
# Installs the operational systemd timer(s) used to keep paper-trading runs
# hands-off:
#
#   1. openalgo-restart.timer            — Mon-Fri 09:05 IST restart of openalgo
#      Why: the WebSocket proxy/adapter does NOT deliver ticks across the daily
#      ~03:00 IST Zerodha token expiry + morning re-auth. It happens whether the
#      proxy cold-starts in the no-session window OR runs continuously through
#      the expiry: the 09:15 subscribe succeeds (no 403) but 0 ticks flow all
#      day. The only reliable fix is a fresh restart AFTER the daily login.
#      So restart at 09:05 IST — after the manual Zerodha auth (~08:42) and 10
#      minutes before the 09:15 strategy start — so strategies launch onto a
#      healthy, post-auth feed. Mon-Fri only (no weekend auth/trading).
#      History: this timer was originally 08:00 (an APScheduler-executor
#      workaround, since fixed via DebugExecutor); 08:00 was BEFORE auth and
#      caused the dead feed, so it was moved to 09:05 (post-auth) on 2026-06-08.
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

# --- Timer 1: daily restart (post-auth, pre-strategy) -----------------------

cat > /etc/systemd/system/openalgo-restart.service << EOF
[Unit]
Description=Restart OpenAlgo after daily Zerodha auth (WS tick-delivery stall fix)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart openalgo
EOF

cat > /etc/systemd/system/openalgo-restart.timer << 'EOF'
[Unit]
Description=Restart OpenAlgo at 09:05 IST Mon-Fri (post-auth, pre-09:15 strategies)

[Timer]
OnCalendar=Mon..Fri *-*-* 09:05:00 Asia/Kolkata
Persistent=false
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
