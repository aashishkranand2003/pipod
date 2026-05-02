# PiPod

PiPod is a touchscreen YouTube Music player for a Raspberry Pi Zero 2W with a 3.5-inch SPI display.

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

## Installation

1. Update the Pi and enable SPI.

	```bash
	sudo apt update
	sudo apt full-upgrade -y
	sudo reboot
	```

	Then run `sudo raspi-config`, open `Interface Options`, enable `SPI`, finish, and reboot.

2. Install the system packages.

	```bash
	sudo apt install -y git python3 python3-venv python3-pip vlc libvlc-bin alsa-utils
	```

3. Clone this repository onto the Pi.

	```bash
	cd /home/pi
	git clone https://github.com/aashishkranand2003/pipod.git
	cd pipod
	```

4. Create and activate a virtual environment in the project root.

	```bash
	python3 -m venv .venv
	source .venv/bin/activate
	pip install --upgrade pip
	pip install -r requirements.txt
	```

5. Make the launcher executable.

	```bash
	chmod +x start.sh
	```

## Touch Calibration

Run the calibration tool once after the display and touchscreen are working.

```bash
source .venv/bin/activate
python3 calibrate.py
```

The calibration is saved to `~/touch_cal.json` and is loaded automatically by `rpi.py` on startup.

## Manual Run

```bash
cd /home/pi/pipod
source .venv/bin/activate
./start.sh
```

## Autostart With systemd

The included `app.service` is a template. Its absolute paths must match your install location before you enable it.

Edit `app.service` so that these paths point at your real project directory and virtual environment:

- `WorkingDirectory`
- `Environment=PATH`
- `ExecStart`

Then install and enable the service.

```bash
sudo cp app.service /etc/systemd/system/app.service
sudo systemctl daemon-reload
sudo systemctl enable app.service
sudo systemctl start app.service
```

Check status and logs with:

```bash
sudo systemctl status app.service
sudo journalctl -u app.service -f
```

## Troubleshooting

Restart the service:

```bash
sudo systemctl restart app.service
```

If VLC bindings are missing or `vlc.Instance()` fails, reinstall `python-vlc` inside the virtual environment:

```bash
source .venv/bin/activate
pip uninstall -y python-vlc
pip install python-vlc
```

If touch input does not line up with the display, rerun `calibrate.py` and confirm the saved bounds in `~/touch_cal.json`.

## Notes

- The app uses the framebuffer directly and does not require X11 or Wayland.
- Touch calibration is loaded from `~/touch_cal.json`, with `~/touch_conf.json` taking priority if present.
- Double-tap the album art to toggle the screen on and off.