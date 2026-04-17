#!/bin/bash
set -e

REPO="https://github.com/monkbuddy62/plant-timelapse-pi.git"
INSTALL_DIR="$HOME/plant-timelapse"

echo "Cloning plant-timelapse-pi..."
git clone "$REPO" "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "Installing Python dependencies..."
pip3 install -r requirements.txt

echo "Installing systemd service..."
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|__USER__|$USER|g" plant-timelapse.service | sudo tee /etc/systemd/system/plant-timelapse.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable plant-timelapse
sudo systemctl start plant-timelapse

echo "Done! Service status:"
sudo systemctl status plant-timelapse --no-pager
