#!/bin/bash
set -e

REPO="https://github.com/monkbuddy62/plant-timelapse-pi.git"
INSTALL_DIR="/home/pato/plant-timelapse"

echo "Cloning plant-timelapse-pi..."
git clone "$REPO" "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "Installing Python dependencies..."
pip3 install -r requirements.txt

echo "Installing systemd service..."
sudo cp plant-timelapse.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable plant-timelapse
sudo systemctl start plant-timelapse

echo "Done! Service status:"
sudo systemctl status plant-timelapse --no-pager
