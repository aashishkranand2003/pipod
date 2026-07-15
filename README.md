# PiPod

PiPod is a touchscreen YouTube Music player(with bluetooth audio support) for a Raspberry Pi Zero 2W with a 3.5-inch SPI display.

## Project Files

- `rpi.py` - main application
- `calibrate.py` - touchscreen calibration tool
- `start.sh` - manual launcher
- `app.service` - systemd service example
- `requirements.txt` - Python dependencies

## Requirements

- Raspberry Pi OS with SPI enabled
- A 3.5-inch SPI framebuffer display on `/dev/fb1`
- A supported touchscreen device exposed through `evdev`
- Python 3, `venv`, `pip`
- System packages: `git`, `vlc`, `libvlc-bin`, `alsa-utils`

If you are using the LCDWiki display driver, install and configure it first, then rotate the panel if needed.

## What you need

- Raspberry Pi Zero 2W
- Raspberry Pi OS Lite 64-bit
- 3.5-inch SPI TFT display that exposes `/dev/fb1`
- Touch controller supported by `evdev`
- USB DAC or Bluetooth audio output
- NetworkManager for Wi-Fi control (`nmcli`)
- BlueZ for Bluetooth control (`bluetoothctl`)

## 1. Prepare Raspberry Pi OS

Install Raspberry Pi OS Lite 64-bit, boot once, then update the system:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Open raspi-config and enable SPI:

```bash
sudo raspi-config
```

Recommended settings:

- Boot to console or console autologin
- Enable SPI
- Keep GPU memory low unless your display overlay needs more

## 2. Enable the SPI display

Edit the firmware config:

```bash
sudo nano /boot/firmware/config.txt
```

Make sure SPI is enabled and add the overlay for your display. The exact overlay depends on your panel, but the important part is that the panel appears as `/dev/fb1`.

Example:

```ini
dtparam=spi=on
dtoverlay=piscreen,speed=20000000
```

## 3. Install dependencies

Install the system packages first:

```bash
sudo apt install -y \
  python3 python3-pip \
  python3-pygame python3-numpy python3-requests python3-evdev python3-vlc \
  vlc libvlc-dev \
  network-manager \
  bluez bluetooth \
  pulseaudio pulseaudio-utils pulseaudio-module-bluetooth \
  git
```

Then install the Python packages used by the script:

```bash
python3 -m pip install --break-system-packages \
  ytmusicapi yt-dlp
```

If your image already has `python3-vlc`, `python3-pygame`, and `python3-evdev`, keep using the distro packages. That is usually the easiest path on the Pi Zero 2W.

## 4. Configure audio

The script uses PulseAudio commands through `pactl` and will route output to the best available sink. For that to work cleanly:

```bash
sudo usermod -aG audio,bluetooth,video $USER
```

If PulseAudio is not already active for your user, log out and back in once after installing it.

Useful checks:

```bash
pactl list short sinks
bluetoothctl list
nmcli device
```

## 5. Install the script

Copy the script somewhere convenient, for example:

```bash
mkdir -p ~/music-player
cp rpi.py ~/music-player/
cd ~/music-player
```

You can keep the filename as `rpi.py` or rename it if you prefer. The script does not depend on a special working directory.

## 6. Touch calibration

On startup the script looks for:

1. `~/touch_conf.json`
2. `~/touch_cal.json`

Use `touch_conf.json` for a manual override, or `touch_cal.json` for the normal calibration output.

Example file:

```json
{
  "TOUCH_X_MIN": 213,
  "TOUCH_X_MAX": 3884,
  "TOUCH_Y_MIN": 733,
  "TOUCH_Y_MAX": 3826
}
```

If touch coordinates feel wrong, temporarily set `TAP_DEBUG = True` in the script and use the red dots to tune the values.

## 7. Run it

From the folder containing the script:

```bash
python3 rpi.py
```

The UI will open directly on the framebuffer. No desktop session is required.

## 8. Optional boot start

If you want it to launch automatically, use a user service after you have confirmed manual startup works.

Create this file:

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/music-player.service
```

Example service:

```ini
[Unit]
Description=Pi Zero 2W Music Player
After=default.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/pi/music-player/rpi.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

Enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable music-player.service
systemctl --user start music-player.service
```

If you use a different username, change `/home/pi/music-player/rpi.py` to your actual home path.

## How it works

- Tap the search bar to open the on-screen keyboard and search YouTube Music.
- Use the Bluetooth button to scan, pair, and connect devices.
- Use the Wi-Fi button to scan and connect through NetworkManager.
- Double-tap the thumbnail to sleep the display and double-tap again to wake it.
- The player prefetches the next tracks to keep playback smooth.

## Troubleshooting

If the display stays blank, confirm that `/dev/fb1` exists and that your overlay matches the panel.

If touch does not respond correctly, verify the device with `evtest` and add calibration values to `~/touch_cal.json`.

If audio does not switch, check `pactl list short sinks` and make sure the USB DAC or Bluetooth sink is visible.

If Wi-Fi controls fail, confirm that `nmcli` works and that NetworkManager is installed and running.

If Bluetooth pairing fails, make sure `bluetoothctl` works and that BlueZ is installed.

For YouTube stream issues, update `yt-dlp`:

```bash
python3 -m pip install --upgrade yt-dlp
```

## Notes

- The app is designed for Raspberry Pi Zero 2W performance levels.
- The render loop is capped at 30 FPS.
- The script is written to run without X11 or Wayland.

## Improvements left

- Onboard Wifi connection, problem in connecting with other wifi connections through onscreen user interface.
