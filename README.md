# Raspberry Pi Zero 2W Setup

This project uses `calibrate.py` for touch calibration and `rpi.py` as the main app. After calibration, `rpi.py` should start automatically on boot.

## Files

- `rpi.py` - main app
- `calibrate.py` - touch calibration tool
- `start.sh` - simple launcher for manual testing
- `rec-app.service` - systemd service for boot startup
- `requirements.txt` - Python dependencies

## Fresh Raspberry Pi Setup

1. Flash Raspberry Pi OS Lite 64-bit to the SD card using Raspberry Pi Imager.
2. In Imager, enable SSH and set Wi-Fi, username, and password.
3. Boot the Pi Zero 2W and update it:

```sh
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

4. Enable SPI for the 3.5" display:

```sh
sudo raspi-config
```

Go to Interface Options, then enable SPI and reboot.

## 3.5-inch LCDWiki display setup


### 1) Install the driver

The LCDWiki instructions use the GoodTFT driver package:

```sh
sudo apt install -y git python3 python3-venv python3-pip build-essential
cd /home/pi
git clone <YOUR_GITHUB_REPO_URL>
sudo rm -rf LCD-show
git clone https://github.com/goodtft/LCD-show.git
chmod -R 755 LCD-show
cd LCD-show
sudo ./LCD35-show
```

Wait for the Pi to reboot. After that, the screen should come up on the 3.5" display.

### 2) Rotate the screen if needed

If the image or touch orientation is wrong, LCDWiki says to use the rotate script after the driver is installed:

```sh
cd LCD-show
sudo ./rotate.sh 90
```

You can change `90` to `0`, `90`, `180`, or `270` depending on the way the screen is mounted.

### 3) Calibrate touch

After the display is working, run the touch calibration tool once:

```sh
mkdir music
cd music
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python3 calibrate.py
```

This writes `~/touch_cal.json`. The app reads that file automatically, so you only need to rerun calibration if the display is rotated or touch drifts.


## Start `rpi.py` on boot

1. Make the launcher executable:

```sh
chmod +x start.sh
```

2. Install the systemd service:

```sh
sudo cp rec-app.service /etc/systemd/system/app.service
sudo systemctl daemon-reload
sudo systemctl enable app.service
sudo systemctl start app.service
```

3. Check the service if needed:

```sh
sudo journalctl -u app.service -f
```

## Publish to GitHub

Use your GitHub account `aashishkranand2003` and push to a repository you create under that account.

Example:

```sh
git init
git add .
git commit -m "Initial Raspberry Pi Zero 2W music player"
git branch -M main
git remote add origin https://github.com/aashishkranand2003/<your-repo-name>.git
git push -u origin main
```

## Recommended path

For easier systemd setup, move the project to a path without spaces, such as:

```sh
/home/pi
```

## Manual start

If you want to test it manually:

```sh
./start.sh
```
