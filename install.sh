#!/bin/bash
set -e

REPO="https://github.com/monkbuddy62/plant-timelapse-pi.git"
INSTALL_DIR="$HOME/plant-timelapse"

echo "Cloning plant-timelapse-pi into $INSTALL_DIR..."
git clone "$REPO" "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "Installing system dependencies..."
sudo apt-get install -y python3-venv

echo "Creating virtual environment..."
python3 -m venv "$INSTALL_DIR/venv" --system-site-packages
"$INSTALL_DIR/venv/bin/pip" install -r requirements.txt

echo "Installing systemd service..."
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|__USER__|$USER|g" plant-timelapse.service | sudo tee /etc/systemd/system/plant-timelapse.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable plant-timelapse
sudo systemctl start plant-timelapse

echo "Done! Service status:"
sudo systemctl status plant-timelapse --no-pager
