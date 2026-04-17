# Plant Timelapse Pi

A Raspberry Pi camera app for long-running plant timelapses. Captures frames on a configurable interval, compiles them into MP4 segments as it goes, and serves a live-streaming web UI for monitoring and control.

## Features

- **Live camera stream** in the browser (MJPEG)
- **Daylight-only capture** — automatically pauses at night and resumes at sunrise, based on your GPS coordinates
- **Dark frame skipping** — discards frames below a brightness threshold
- **Rolling segments** — compiles frames into video segments every N hours so you never lose footage to a crash
- **Watchdog** — if the capture thread dies for any reason, it restarts automatically within 30 seconds
- **Image controls** — brightness, contrast, saturation, sharpness, white balance
- **Scheduler** — set a future start/stop time for unattended recording
- **Size on disk** — live readout for the running timelapse and all saved ones

## Requirements

- Raspberry Pi with Camera Module (tested with Pi Camera v2/v3)
- Raspberry Pi OS (64-bit recommended; SPI display support requires 32-bit)
- Python 3.8+
- `ffmpeg` installed (`sudo apt install ffmpeg`)
- `picamera2` library (pre-installed on current Raspberry Pi OS)

## Installation

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/monkbuddy62/plant-timelapse-pi/main/install.sh)
```

This clones the repo, creates a virtualenv, installs Python dependencies, and registers a systemd service that starts on boot.

Then open `http://<pi-ip>:5000` in your browser.

## Manual setup

```bash
git clone https://github.com/monkbuddy62/plant-timelapse-pi.git
cd plant-timelapse-pi
python3 -m venv venv
venv/bin/pip install -r requirements.txt
sudo apt install ffmpeg
python3 app.py        # foreground, Ctrl-C to stop
```

## Updating

```bash
cd ~/plant-timelapse
git pull
sudo systemctl restart plant-timelapse
```

The build number is shown in the top-right corner of the UI — confirm it bumped after restart.

## Daylight window

Enable **Capture during daylight window only** when starting a timelapse. Enter your latitude/longitude and optional offsets (e.g. start 90 min after sunrise, stop 90 min before sunset). The window is computed daily using [astral](https://astral.readthedocs.io/) and the browser's timezone, so it works correctly regardless of the Pi's system timezone.

## Service commands

```bash
sudo systemctl status plant-timelapse
sudo systemctl restart plant-timelapse
sudo systemctl stop plant-timelapse
journalctl -u plant-timelapse -f   # live logs
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TIMELAPSES_DIR` | `./timelapses` | Where session directories are stored |
| `STREAM_WIDTH` | `1280` | Live stream resolution width |
| `STREAM_HEIGHT` | `720` | Live stream resolution height |
| `STREAM_FPS` | `10` | Live stream frame rate |

## Debug endpoint

`GET /api/debug/daylight` — returns Pi clock, corrected clock, clock offset, computed sunrise/sunset window, and whether the current time is inside the window. Useful for diagnosing daylight capture issues.
