#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/jarvis-ui.service"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: jarvis-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing J.A.R.V.I.S UI service..."
echo "Make sure you've edited jarvis-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable jarvis-ui
sudo systemctl start jarvis-ui
sudo systemctl status jarvis-ui
