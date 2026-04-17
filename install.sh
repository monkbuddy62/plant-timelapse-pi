#!/bin/bash
set -e

REPO="https://github.com/monkbuddy62/plant-timelapse-pi.git"
REAL_USER="${SUDO_USER:-$USER}"
INSTALL_DIR="/home/$REAL_USER/plant-timelapse"

echo "Cloning plant-timelapse-pi into $INSTALL_DIR..."
git clone "$REPO" "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "Creating virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -r requirements.txt

echo "Installing systemd service..."
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|__USER__|$REAL_USER|g" plant-timelapse.service | sudo tee /etc/systemd/system/plant-timelapse.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable plant-timelapse
sudo systemctl start plant-timelapse

echo "Done! Service status:"
sudo systemctl status plant-timelapse --no-pager
