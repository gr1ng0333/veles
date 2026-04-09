#!/bin/bash
set -e

echo "=== Stopping old processes ==="
screen -S veles_new -X quit 2>/dev/null || true
screen -S veles -X quit 2>/dev/null || true
# Kill only veles-related python processes, not other bots
pkill -f "python3 colab_launcher.py" 2>/dev/null || true
sleep 2

echo "=== Removing watchdog cron ==="
crontab -l 2>/dev/null | grep -v "watchdog" | crontab - 2>/dev/null || true
rm -f /opt/veles/watchdog.sh

echo "=== Installing systemd unit ==="
cp /opt/veles/deploy/veles.service /etc/systemd/system/veles.service
systemctl daemon-reload
systemctl enable veles
systemctl start veles

echo ""
echo "=== Veles service status ==="
systemctl status veles --no-pager -l
echo ""
echo "=== View logs ==="
echo "  journalctl -u veles -f"
echo "  journalctl -u veles --since '5 min ago'"
